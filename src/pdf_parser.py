from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import os
import multiprocessing as mp
import re
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pdfplumber

try:
    import psutil
except ImportError:
    psutil = None

from base_parser import ParsedDocument


MIN_PAGE_CHARS = 20
OCR_MIN_NONSPACE_CHARS = 500
PARSE_INDEX_FIELDS = [
    "ticker",
    "pdf_file",
    "source_pdf",
    "parsed_text_file",
    "page_map_file",
    "status",
    "error_message",
    "page_count",
    "char_count",
    "table_count",
    "content_hash",
    "parsed_at",
    "quality_flags",
    "possible_wrong_doc_type",
    "readable_word_count",
    "readable_word_ratio",
    "chars_per_page",
    "garbled_char_count",
]
PAGE_MAP_FIELDS = ["page", "char_start", "char_end", "char_count"]

SEC_10K_MARKERS = [
    r"\bFORM\s+10-K\b",
    r"\bUNITED\s+STATES\s+SECURITIES\s+AND\s+EXCHANGE\s+COMMISSION\b",
    r"\bAnnual\s+Report\s+Pursuant\s+to\s+Section\s+13\s+or\s+15\(d\)\b",
    r"\bItem\s+1A\.?\s+Risk\s+Factors\b",
    r"\bItem\s+7\.?\s+Management'?s\s+Discussion\s+and\s+Analysis\b",
]
SEC_10K_ITEM_MARKERS = [
    r"\bItem\s+1\.?\s+Business\b",
    r"\bItem\s+1A\.?\s+Risk\s+Factors\b",
    r"\bItem\s+7\.?\s+Management'?s\s+Discussion\s+and\s+Analysis\b",
    r"\bItem\s+8\.?\s+Financial\s+Statements\b",
]
GARBLED_SEQUENCES = ["ï¿½", "Ã¢â‚¬", "Ã‚", "ï¿½?", "�"]


def mem() -> float:
    if psutil is None:
        return 0.0
    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)


def display_path(path: str | Path) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def count_garbled_chars(text: str) -> int:
    return sum(text.count(sequence) for sequence in GARBLED_SEQUENCES)


def is_possible_10k(text: str) -> bool:
    opening = text[:15000]
    first_pages = text[:5000]

    has_form_10k = bool(re.search(r"\bFORM\s+10-K\b", first_pages, flags=re.IGNORECASE))
    has_sec_header = bool(
        re.search(
            r"\bUNITED\s+STATES\s+SECURITIES\s+AND\s+EXCHANGE\s+COMMISSION\b",
            first_pages,
            flags=re.IGNORECASE,
        )
    )
    has_annual_report_pursuant = bool(
        re.search(
            r"\bAnnual\s+Report\s+Pursuant\s+to\s+Section\s+13\s+or\s+15\(d\)\b",
            first_pages,
            flags=re.IGNORECASE,
        )
    )
    sec_item_count = sum(
        1
        for pattern in SEC_10K_ITEM_MARKERS
        if re.search(pattern, opening, flags=re.IGNORECASE)
    )

    return has_form_10k and (has_sec_header or has_annual_report_pursuant or sec_item_count >= 2)


def text_quality_metrics(text: str, page_count: int, char_count: int) -> dict:
    total_wordish_tokens = len(re.findall(r"\S+", text))
    readable_word_count = len(re.findall(r"\b[A-Za-z]{3,}\b", text))
    readable_word_ratio = (
        readable_word_count / total_wordish_tokens
        if total_wordish_tokens
        else 0.0
    )
    chars_per_page = char_count / page_count if page_count else 0.0
    garbled_char_count = count_garbled_chars(text)
    possible_wrong_doc_type = is_possible_10k(text)

    flags = []
    if possible_wrong_doc_type:
        flags.append("possible_10k")
    if garbled_char_count > 100 or (
        char_count > 0 and garbled_char_count / char_count > 0.005
    ):
        flags.append("garbled_text")
    if char_count >= OCR_MIN_NONSPACE_CHARS and readable_word_ratio < 0.45:
        flags.append("low_readable_word_ratio")
    if page_count >= 5 and chars_per_page < 250:
        flags.append("low_text_per_page")

    return {
        "quality_flags": "|".join(flags),
        "possible_wrong_doc_type": "true" if possible_wrong_doc_type else "false",
        "readable_word_count": readable_word_count,
        "readable_word_ratio": f"{readable_word_ratio:.4f}",
        "chars_per_page": f"{chars_per_page:.1f}",
        "garbled_char_count": garbled_char_count,
    }


def build_raw_text_and_page_spans(pages: list[tuple[int, str]]) -> tuple[str, list[dict]]:
    parts: list[str] = []
    spans: list[dict] = []
    cursor = 0

    for page_number, text in pages:
        if parts:
            parts.append("\n")
            cursor += 1

        start = cursor
        parts.append(text)
        cursor += len(text)
        spans.append(
            {
                "page": page_number,
                "char_start": start,
                "char_end": cursor,
                "char_count": len(text),
            }
        )

    return "".join(parts), spans


class PDFParser:
    name = "pdfplumber"

    def parse(self, file_path, company=None, **kwargs):
        file_path = Path(file_path)
        log_pages = kwargs.get("log_pages", False)
        pages_text: list[tuple[int, str]] = []
        page_count = 0
        table_count = 0

        with pdfplumber.open(file_path) as pdf:
            page_count = len(pdf.pages)

            for i in range(page_count):
                if log_pages:
                    print(f"[{company}] Page {i + 1} RAM {mem():.2f}MB", flush=True)
                page = None
                text = ""

                try:
                    page = pdf.pages[i]
                    text = page.extract_text_simple() or ""

                    if len(text.strip()) > MIN_PAGE_CHARS:
                        pages_text.append((i + 1, text))

                    try:
                        table_count += len(page.find_tables() or [])
                    except Exception:
                        pass

                except Exception as e:
                    print(f"fail page {i + 1}: {e}", flush=True)

                finally:
                    if page is not None:
                        # pdfplumber keeps page/layout caches alive for the life
                        # of the PDF object. Clear them so large PDF batches stay
                        # bounded inside each short-lived worker process.
                        try:
                            page.flush_cache()
                        except AttributeError:
                            for attr in (
                                "_objects",
                                "_layout",
                                "_chars",
                                "_lines",
                                "_rects",
                                "_curves",
                                "_images",
                            ):
                                if hasattr(page, attr):
                                    try:
                                        delattr(page, attr)
                                    except AttributeError:
                                        pass

                    if hasattr(pdf, "_pages") and pdf._pages is not None:
                        pdf._pages[i] = None

                    page = None
                    text = None

                    if (i + 1) % 10 == 0:
                        gc.collect()

        raw_text, page_spans = build_raw_text_and_page_spans(pages_text)
        doc = ParsedDocument(
            source_file=str(file_path),
            company=company,
            doc_type="sustainability",
            parser_used=self.name,
            raw_text=raw_text,
        ).finalize()
        doc.page_count = page_count
        doc.table_count = table_count
        doc.page_spans = page_spans
        return doc


def discover(root: str | Path, ticker: str | None = None) -> dict[str, list[Path]]:
    root = Path(root)
    out: dict[str, list[Path]] = {}

    if not root.exists():
        return out

    ticker_dirs = [root / ticker.upper()] if ticker else [p for p in root.iterdir() if p.is_dir()]
    for ticker_dir in ticker_dirs:
        if not ticker_dir.exists() or not ticker_dir.is_dir():
            continue
        pdfs = sorted(ticker_dir.glob("*.pdf"))
        if pdfs:
            out[ticker_dir.name.upper()] = pdfs

    return out


def _set_memory_limit(max_mb=2048):
    try:
        import resource
    except ImportError:
        return

    try:
        resource.setrlimit(
            resource.RLIMIT_AS,
            (max_mb * 1024 * 1024, resource.RLIM_INFINITY),
        )
    except (OSError, ValueError):
        return


def _write_text(out_path: Path, text: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")


def _write_page_map(out_path: Path, page_spans: list[dict]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PAGE_MAP_FIELDS)
        writer.writeheader()
        writer.writerows(page_spans)


def _parse_one(args):
    _set_memory_limit(2048)
    file_path, ticker, out_root, log_pages = args
    file_path = Path(file_path)
    out_root = Path(out_root)
    out_file = out_root / ticker / f"{file_path.stem}.txt"
    page_map_file = out_root / ticker / f"{file_path.stem}.pages.csv"
    parser = PDFParser()

    try:
        doc = parser.parse(file_path, company=ticker, log_pages=log_pages)
        nonspace_chars = len("".join(doc.raw_text.split()))
        status = "parsed" if nonspace_chars >= OCR_MIN_NONSPACE_CHARS else "ocr_required"

        _write_text(out_file, doc.raw_text)
        _write_page_map(page_map_file, getattr(doc, "page_spans", []))
        quality = text_quality_metrics(
            doc.raw_text,
            getattr(doc, "page_count", 0),
            doc.char_count,
        )

        return {
            "ticker": ticker,
            "pdf_file": file_path.name,
            "source_pdf": display_path(file_path),
            "parsed_text_file": display_path(out_file),
            "page_map_file": display_path(page_map_file),
            "status": status,
            "error_message": "",
            "page_count": getattr(doc, "page_count", 0),
            "char_count": doc.char_count,
            "table_count": doc.table_count,
            "content_hash": doc.content_hash
            or hashlib.sha256(doc.raw_text.encode("utf-8", "ignore")).hexdigest(),
            "parsed_at": doc.parsed_at,
            **quality,
        }

    except Exception as e:
        return {
            "ticker": ticker,
            "pdf_file": file_path.name,
            "source_pdf": display_path(file_path),
            "parsed_text_file": "",
            "page_map_file": "",
            "status": "failed",
            "error_message": f"{type(e).__name__}: {e}",
            "page_count": 0,
            "char_count": 0,
            "table_count": 0,
            "content_hash": "",
            "parsed_at": "",
            "quality_flags": "",
            "possible_wrong_doc_type": "false",
            "readable_word_count": 0,
            "readable_word_ratio": "0.0000",
            "chars_per_page": "0.0",
            "garbled_char_count": 0,
        }


def read_existing_index(index_path: Path) -> list[dict]:
    if not index_path.exists():
        return []

    with index_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [{field: row.get(field, "") for field in PARSE_INDEX_FIELDS} for row in reader]


def write_index(index_path: Path, rows: list[dict]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r.get("ticker", ""), r.get("pdf_file", "")))

    with index_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PARSE_INDEX_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def upsert_index_rows(index_path: Path, new_rows: list[dict], replace_all: bool) -> None:
    if replace_all:
        rows = new_rows
    else:
        existing = read_existing_index(index_path)
        replace_keys = {
            (row.get("ticker", ""), row.get("pdf_file", ""), row.get("source_pdf", ""))
            for row in new_rows
        }
        rows = [
            row
            for row in existing
            if (row.get("ticker", ""), row.get("pdf_file", ""), row.get("source_pdf", ""))
            not in replace_keys
        ]
        rows.extend(new_rows)

    write_index(index_path, rows)


def run(
    root: str | Path,
    out: str | Path,
    index: str | Path,
    ticker: str | None = None,
    workers: int = 1,
    num_companies: int | None = None,
    log_pages: bool = False,
) -> list[dict]:
    data = discover(root, ticker=ticker)
    if num_companies is not None and ticker is None:
        data = dict(list(sorted(data.items()))[:num_companies])

    jobs = [
        (pdf, ticker_name, Path(out), log_pages)
        for ticker_name, files in sorted(data.items())
        for pdf in files
    ]
    print(f"Found {len(jobs)} ESG PDF(s) under {root}")

    results: list[dict] = []
    if not jobs:
        upsert_index_rows(Path(index), results, replace_all=ticker is None)
        print(f"Index saved to: {Path(index)}")
        return results

    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=max(1, workers),
        mp_context=ctx,
        max_tasks_per_child=1,
    ) as pool:
        futures = [pool.submit(_parse_one, job) for job in jobs]
        for fut in as_completed(futures):
            row = fut.result()
            results.append(row)
            print()
            print(f"==== {row['ticker']} {row['pdf_file']}")
            if row["status"] == "failed":
                print("FAILED:", row["error_message"])
            else:
                print(f"{row['status'].upper()} {row['char_count']} chars")

    upsert_index_rows(Path(index), results, replace_all=ticker is None)
    print(f"Index saved to: {Path(index)}")
    return results


def main():
    ap = argparse.ArgumentParser(description="Parse ESG/sustainability PDFs to text.")
    ap.add_argument("--root", default="data/01_raw/sustainability")
    ap.add_argument("--out", default="data/02_interim/esg_text")
    ap.add_argument("--index", default="data/00_reference/esg_parse_index.csv")
    ap.add_argument("--ticker", default=None, help="Process one ticker folder, e.g. GAP")
    ap.add_argument(
        "--num-companies",
        type=int,
        default=None,
        help="Optional limit for compatibility with earlier parser smoke tests.",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Parallel worker processes. Each PDF still gets its own process "
            "lifetime to release pdfplumber/pdfminer memory."
        ),
    )
    ap.add_argument("--log-pages", action="store_true", help="Print per-page parser progress and RAM usage.")
    args = ap.parse_args()

    run(
        root=args.root,
        out=args.out,
        index=args.index,
        ticker=args.ticker.upper() if args.ticker else None,
        workers=args.workers,
        num_companies=args.num_companies,
        log_pages=args.log_pages,
    )


if __name__ == "__main__":
    main()
