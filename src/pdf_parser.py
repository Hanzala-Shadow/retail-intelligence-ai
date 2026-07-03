from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
import time
from pathlib import Path

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError
from pdfplumber.utils.exceptions import PdfminerException, MalformedPDFException

from base_parser import BaseParser, ParsedDocument, TableRef

MIN_TEXT_PAGE_RATIO = 0.5
MIN_CHARS_PER_PAGE = 20


MIN_HTML_CHARS = 2000

NOISE_TABLE_MAX_ROWS = 2
NOISE_TABLE_MAX_COLS = 1


Row = list[str]
Table = list[Row]


def _norm(cell) -> str:
    """Coerce a cell to a whitespace-stripped string; None -> ''."""
    if cell is None:
        return ""
    return str(cell).strip()


def strip_whitespace(rows: Table) -> Table:
    return [[_norm(c) for c in row] for row in rows]


def _pad_to_width(rows: Table, width: int) -> Table:
    return [row + [""] * (width - len(row)) for row in rows]


def remove_duplicate_header_rows(rows: Table) -> Table:
    """
    Tables that visually span a page break, or a layout artifact, frequently
    repeat the header row mid-table. Treat row 0 as the header and drop any
    later row that matches it, case-insensitively, after whitespace
    normalization.
    """
    if len(rows) < 2:
        return rows

    header_key = tuple(c.lower() for c in rows[0])
    out = [rows[0]]
    for row in rows[1:]:
        row_key = tuple(c.lower() for c in row)
        if row_key == header_key:
            continue
        out.append(row)
    return out


def drop_empty_rows(rows: Table) -> Table:
    """Drop any row (including a possible header) where every cell is empty."""
    return [row for row in rows if any(c != "" for c in row)]


def drop_empty_columns(rows: Table) -> Table:
    """
    Drop any column where every *data* value (i.e. every row except the
    header, row 0) is empty. The header cell's own text doesn't save a
    column — a column with a header label but no populated data below it is
    exactly the "header-only" case we want gone.
    """
    if not rows:
        return rows

    n_cols = len(rows[0])
    data_rows = rows[1:]

    if not data_rows:
        return rows

    keep_cols = [
        i for i in range(n_cols)
        if any(row[i] != "" for row in data_rows)
    ]

    if not keep_cols:
        return [[]] * len(rows)

    return [[row[i] for i in keep_cols] for row in rows]


def clean_table(rows: Table) -> Table | None:
    """
    Clean a raw table (list of rows, each a list of cell strings/None) and
    return the cleaned table, or None if nothing usable is left.
    """
    if not rows:
        return None

    rows = strip_whitespace(rows)

    n_cols = max((len(r) for r in rows), default=0)
    if n_cols == 0:
        return None
    rows = _pad_to_width(rows, n_cols)

    rows = remove_duplicate_header_rows(rows)
    rows = drop_empty_rows(rows)

    if len(rows) < 2:
        return None

    rows = drop_empty_columns(rows)

    if not rows or not rows[0] or len(rows) < 2:
        return None

    return rows


class PDFParser(BaseParser):

    name = "PDFParser"

    def parse(
        self,
        file_path: str | Path,
        company: str | None = None,
        doc_type: str | None = None,
        extract_tables: bool = True,
    ) -> ParsedDocument:

        file_path = Path(file_path)
        try:
            pdf = pdfplumber.open(file_path)
        except (PDFPasswordIncorrect, PDFSyntaxError, PdfminerException, MalformedPDFException) as e:
            
            root_cause = e.__context__ if e.__context__ is not None else e

            if isinstance(root_cause, PDFPasswordIncorrect):
                status, reason = "encrypted", "PDF is password-protected; cannot open without a password."
            elif isinstance(root_cause, PDFSyntaxError):
                status, reason = "corrupt", f"Malformed/corrupt PDF structure: {root_cause}"
            else:
                status, reason = "corrupt", f"Could not open PDF ({type(root_cause).__name__}): {root_cause}"

            return ParsedDocument(
                source_file=str(file_path),
                company=company,
                doc_type=doc_type,
                parser_used=self.name,
                status=status,
                raw_text="",
                tables=[],
                error_message=reason,
            ).finalize()
        except Exception as e:
            return self._error_doc(file_path, e)

        try:
            with pdf:

                page_texts = []
                tables = []
                table_counter = 0
                non_empty_pages = 0
                page_errors = []
                total_pages = len(pdf.pages)

                for page_num, page in enumerate(pdf.pages, start=1):
                    try:
                        text = page.extract_text() or ""
                    except Exception as e:
                        page_errors.append((page_num, f"{type(e).__name__}: {e}"))
                        page_texts.append("")
                        continue

                    if len(text.strip()) >= MIN_CHARS_PER_PAGE:
                        non_empty_pages += 1

                    if extract_tables:
                        try:
                            raw_tables = page.extract_tables()
                        except Exception as e:
                            raw_tables = []
                            page_errors.append((page_num, f"table extraction failed: {type(e).__name__}: {e}"))

                        for table in raw_tables:

                            if not table or not any(
                                any((c or "").strip() for c in row)
                                for row in table
                            ):
                                continue

                            table_counter += 1
                            table_id = f"table_{table_counter}"

                            cleaned = clean_table(table)

                            if cleaned is None:
                                self._log_empty_table(company, file_path, table_id)
                                continue

                            n_rows = len(cleaned)
                            n_cols = max(len(r) for r in cleaned)

                            if n_rows <= NOISE_TABLE_MAX_ROWS and n_cols <= NOISE_TABLE_MAX_COLS:
                                self._log_noise_table(company, file_path, table_id, n_rows, n_cols)
                                continue

                            csv_path = self.table_output_dir / f"{file_path.stem}__{table_id}.csv"

                            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                                writer = csv.writer(f)
                                writer.writerows(cleaned)

                            tables.append(
                                TableRef(
                                    table_id=table_id,
                                    csv_path=str(csv_path),
                                    n_rows=n_rows,
                                    n_cols=n_cols,
                                )
                            )

                            text += f"\n[TABLE:{table_id}]\n"

                    page_texts.append(text)

                full_text = self._normalize("\n\n".join(page_texts))

                if page_errors:
                    self._log_page_errors(company, file_path, page_errors)

                status = "ok"
                error_message = None

                if total_pages == 0:
                    status = "error"
                    error_message = "PDF opened but reports zero pages."

                elif total_pages > 0 and (non_empty_pages / total_pages) < MIN_TEXT_PAGE_RATIO:
                    status = "ocr_required"

                if page_errors and status == "ok":
                    error_message = f"{len(page_errors)}/{total_pages} page(s) had extraction errors (partial result); see logs/parse_errors.log"

                return ParsedDocument(
                    source_file=str(file_path),
                    company=company,
                    doc_type=doc_type,
                    parser_used=self.name,
                    status=status,
                    raw_text=full_text,
                    tables=tables,
                    error_message=error_message,
                ).finalize()

        except Exception as e:
            return self._error_doc(file_path, e)

    def _log_empty_table(self, company: str | None, file_path: Path, table_id: str) -> None:
        log_dir = Path("logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "empty_tables.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{company}\t{file_path}\t{table_id}\tempty_table\n")

    def _log_noise_table(self, company: str | None, file_path: Path, table_id: str, n_rows: int, n_cols: int) -> None:
        log_dir = Path("logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "empty_tables.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{company}\t{file_path}\t{table_id}\tnoise_table({n_rows}x{n_cols})\n")

    def _log_page_errors(self, company: str | None, file_path: Path, page_errors: list[tuple[int, str]]) -> None:
        log_dir = Path("logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "parse_errors.log"
        with open(log_path, "a", encoding="utf-8") as f:
            for page_num, msg in page_errors:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{company}\t{file_path}\tpage_{page_num}\t{msg}\n")

    def _normalize(self, text: str) -> str:
        text = text.replace("\xa0", " ").replace("\r", "")
        text = re.sub(r"[ \t]+", " ", text)
        text = "\n".join(line.strip() for line in text.split("\n"))
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def discover_company_pdfs(root: Path) -> dict[str, list[Path]]:
    """Discover ESG/sustainability report PDFs, grouped by company subfolder."""
    companies: dict[str, list[Path]] = {}

    if not root.exists():
        return companies

    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        files = sorted(sub.rglob("*.pdf"))
        if files:
            companies[sub.name] = files

    if not companies:
        flat_files = sorted(root.glob("*.pdf"))
        for f in flat_files:
            companies.setdefault(f.stem, []).append(f)

    return companies


def dedupe_files(
    companies: dict[str, list[Path]]
) -> tuple[dict[str, list[Path]], list[tuple[str, Path, Path]]]:
    seen: dict[str, Path] = {}
    duplicates: list[tuple[str, Path, Path]] = []
    deduped: dict[str, list[Path]] = {}

    for company, files in companies.items():
        kept = []
        for f in files:
            h = file_hash(f)
            if h in seen:
                duplicates.append((company, f, seen[h]))
                continue
            seen[h] = f
            kept.append(f)
        if kept:
            deduped[company] = kept

    return deduped, duplicates


def htm_extract_text(path: Path) -> str:
    """Best-effort plain-text extraction from a 10-K .htm/.html filing."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:

        return ""

    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n") if line.strip())
    return text.strip()


def htm_parse_is_clean(path: Path, min_chars: int = MIN_HTML_CHARS) -> tuple[bool, str]:

    text = htm_extract_text(path)
    return len(text) >= min_chars, text


def discover_10k_filings(root: Path) -> dict[str, dict[str, Path | None]]:

    filings: dict[str, dict[str, Path | None]] = {}

    if not root.exists():
        return filings

    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        htm_files = sorted(sub.rglob("*.htm")) + sorted(sub.rglob("*.html"))
        pdf_files = sorted(sub.rglob("*.pdf"))
        if htm_files or pdf_files:
            filings[sub.name] = {
                "htm": htm_files[0] if htm_files else None,
                "pdf": pdf_files[0] if pdf_files else None,
            }

    return filings


def resolve_10k_source(info: dict[str, Path | None]) -> tuple[str, Path | None, str]:

    htm_path = info.get("htm")
    pdf_path = info.get("pdf")

    if htm_path and htm_path.exists():
        is_clean, text = htm_parse_is_clean(htm_path)
        if is_clean:
            return "htm", htm_path, text

    if pdf_path and pdf_path.exists():
        return "pdf_fallback", pdf_path, ""

    return "missing", None, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/01_raw/Sustainability Reports",
                     help="Root folder of ESG/sustainability report PDFs (company subfolders).")
    ap.add_argument("--tenk-root", default="data/01_raw/10-K",
                     help="Root folder of 10-K filings (company subfolders with .htm and/or .pdf).")
    ap.add_argument("--out", default="data/raw_text/pdf_text")
    ap.add_argument("--tables", default="data/tables/pdf_table")
    ap.add_argument("--first-only", action="store_true",
                     help="Process only the first ESG PDF found per company folder "
                          "(alphabetical), instead of every PDF/year under it. "
                          "Default: process every PDF per company — use this only "
                          "for a quick smoke test.")
    ap.add_argument("--skip-10k", action="store_true",
                     help="Skip 10-K processing and only run ESG report parsing.")
    ap.add_argument("--min-companies", type=int, default=5)
    ap.add_argument(
        "--num-companies",
        type=int,
        default=None,
        help="Limit the run to the first N companies (alphabetical). "
             "Default: no limit — every discovered company is processed. "
             "Useful for a quick smoke test before a full-scale run.",
    )
    args = ap.parse_args()

    root = Path(args.root)

    if not root.exists():
        print(f"Folder not found: {root.resolve()}")
        sys.exit(1)

    out_dir = Path(args.out)
    table_dir = Path(args.tables)
    out_dir.mkdir(parents=True, exist_ok=True)
    table_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    error_log = log_dir / "parse_errors.log"

    parser = PDFParser(table_output_dir=table_dir)

    companies = discover_company_pdfs(root)
    companies, duplicates = dedupe_files(companies)

    if duplicates:
        print(f"Removed {len(duplicates)} duplicate file(s):")
        for company, dup, original in duplicates:
            print(f"  {company}: {dup.name} is a duplicate of {original}")

    if len(companies) < args.min_companies:
        print(f"Only found {len(companies)} companies, need >= {args.min_companies}.")

    if args.num_companies and len(companies) > args.num_companies:
        selected_names = sorted(companies.keys())[: args.num_companies]
        companies = {name: companies[name] for name in selected_names}

    print(f"Running on {len(companies)} companies (ESG reports): {list(companies.keys())}\n")

    results = []
    n_errors = 0
    n_total = 0

    n_pdfs = sum(len(files) for files in companies.values())
    print(f"({n_pdfs} PDF file(s) total across those companies)\n" if not args.first_only
          else "(--first-only set: processing just one PDF per company)\n")

    for company, files in companies.items():
        targets = files[:1] if args.first_only else files
        for f in targets:
            n_total += 1
            t0 = time.time()
            try:
                doc = parser.parse(f, company=company, doc_type="ESG")
                elapsed = time.time() - t0
                results.append({
                    "company": company,
                    "file": f.name,
                    "doc_type": "ESG",
                    "status": doc.status,
                    "chars": doc.char_count,
                    "tables": doc.table_count,
                    "time_s": round(elapsed, 2),
                    "error": doc.error_message,
                })

                if doc.status in ("ok", "ocr_required"):
                    out_path = out_dir / f"{company}__ESG__{f.stem}.txt"
                    out_path.write_text(doc.raw_text, encoding="utf-8")
                else:
                    n_errors += 1
                    with open(error_log, "a", encoding="utf-8") as log:
                        log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{company}\t{f}\t{doc.status}\t{doc.error_message}\n")

            except Exception as e:
                n_errors += 1
                results.append({
                    "company": company,
                    "file": f.name,
                    "doc_type": "ESG",
                    "status": "EXCEPTION",
                    "chars": 0,
                    "tables": 0,
                    "time_s": round(time.time() - t0, 2),
                    "error": f"{type(e).__name__}: {e}",
                })
                with open(error_log, "a", encoding="utf-8") as log:
                    log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{company}\t{f}\tEXCEPTION\t{type(e).__name__}: {e}\n")

    if not args.skip_10k:
        tenk_root = Path(args.tenk_root)
        filings = discover_10k_filings(tenk_root)

        if not filings:
            print(f"\nNo 10-K filings found under: {tenk_root.resolve()} (skipping 10-K stage)")
        else:
            if companies:
                filings = {c: filings[c] for c in companies if c in filings} or filings

            print(f"\nRunning on {len(filings)} companies (10-K filings): {list(filings.keys())}\n")

            for company, info in filings.items():
                n_total += 1
                t0 = time.time()
                source_type, path, htm_text = resolve_10k_source(info)

                if source_type == "missing":
                    n_errors += 1
                    results.append({
                        "company": company,
                        "file": "",
                        "doc_type": "10-K",
                        "status": "error",
                        "chars": 0,
                        "tables": 0,
                        "time_s": round(time.time() - t0, 2),
                        "error": "no usable .htm or .pdf 10-K found",
                    })
                    with open(error_log, "a", encoding="utf-8") as log:
                        log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{company}\t-\tmissing\tno usable .htm or .pdf 10-K found\n")
                    continue

                if source_type == "htm":
                    elapsed = time.time() - t0
                    out_path = out_dir / f"{company}__10-K__{path.stem}.txt"
                    out_path.write_text(htm_text, encoding="utf-8")
                    results.append({
                        "company": company,
                        "file": path.name,
                        "doc_type": "10-K",
                        "status": "ok_htm",
                        "chars": len(htm_text),
                        "tables": 0,
                        "time_s": round(elapsed, 2),
                        "error": None,
                    })
                    continue

                try:
                    doc = parser.parse(path, company=company, doc_type="10-K-fallback")
                    elapsed = time.time() - t0
                    results.append({
                        "company": company,
                        "file": path.name,
                        "doc_type": "10-K-fallback",
                        "status": doc.status,
                        "chars": doc.char_count,
                        "tables": doc.table_count,
                        "time_s": round(elapsed, 2),
                        "error": doc.error_message,
                    })

                    if doc.status in ("ok", "ocr_required"):
                        out_path = out_dir / f"{company}__10-K-fallback__{path.stem}.txt"
                        out_path.write_text(doc.raw_text, encoding="utf-8")
                    else:
                        n_errors += 1
                        with open(error_log, "a", encoding="utf-8") as log:
                            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{company}\t{path}\t{doc.status}\t{doc.error_message}\n")

                except Exception as e:
                    n_errors += 1
                    results.append({
                        "company": company,
                        "file": path.name,
                        "doc_type": "10-K-fallback",
                        "status": "EXCEPTION",
                        "chars": 0,
                        "tables": 0,
                        "time_s": round(time.time() - t0, 2),
                        "error": f"{type(e).__name__}: {e}",
                    })
                    with open(error_log, "a", encoding="utf-8") as log:
                        log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{company}\t{path}\tEXCEPTION\t{type(e).__name__}: {e}\n")

    print(f"\n{'company':<22}{'type':<16}{'file':<30}{'status':<14}{'chars':<10}{'tables':<8}{'time(s)':<8}")
    print("-" * 110)
    n_ok = 0
    n_ocr = 0
    for r in results:
        print(f"{r['company']:<22}{r['doc_type']:<16}{r['file'][:28]:<30}{r['status']:<14}{r['chars']:<10}{r['tables']:<8}{r['time_s']:<8}")
        if r["status"] in ("ok", "ok_htm"):
            n_ok += 1
        elif r["status"] == "ocr_required":
            n_ocr += 1
            print("    -> scanned/no-text PDF correctly flagged, did NOT crash")
        elif r["status"] == "encrypted":
            print(f"    -> password-protected, skipped: {r['error']}")
        elif r["status"] == "corrupt":
            print(f"    -> malformed/corrupt PDF, skipped: {r['error']}")
        elif r["error"]:
            print(f"    -> {r['error']}")

    print("-" * 110)
    print(f"OK: {n_ok}  |  OCR_REQUIRED: {n_ocr}  |  ERROR/EXCEPTION: {n_errors}  |  Records processed: {n_total}")
    print(f"\nExtracted text written to: {out_dir.resolve()}")
    print(f"Extracted tables written to: {table_dir.resolve()}")

    if n_total > 0 and (n_errors / n_total) >= 0.05:
        sys.exit(1)


if __name__ == "__main__":
    main()