# from __future__ import annotations

# import argparse
# import csv
# import hashlib
# import re
# import sys
# import time
# from pathlib import Path

# import pdfplumber

# from base_parser import BaseParser, ParsedDocument, TableRef

# MIN_TEXT_PAGE_RATIO = 0.5
# MIN_CHARS_PER_PAGE = 20


# class PDFParser(BaseParser):
#     name = "PDFParser"

#     def parse(
#         self,
#         file_path: str | Path,
#         company: str | None = None,
#         doc_type: str | None = None,
#         extract_tables: bool = True,
#     ) -> ParsedDocument:

#         file_path = Path(file_path)

#         try:
#             with pdfplumber.open(file_path) as pdf:

#                 page_texts = []
#                 tables = []
#                 table_counter = 0
#                 non_empty_pages = 0
#                 total_pages = len(pdf.pages)

#                 for page in pdf.pages:

#                     text = page.extract_text() or ""

#                     if len(text.strip()) >= MIN_CHARS_PER_PAGE:
#                         non_empty_pages += 1

#                     if extract_tables:
#                         for table in page.extract_tables():

#                             if not table or not any(
#                                 any((c or "").strip() for c in row)
#                                 for row in table
#                             ):
#                                 continue

#                             table_counter += 1
#                             table_id = f"table_{table_counter}"

#                             csv_path = self.table_output_dir / f"{file_path.stem}__{table_id}.csv"

#                             with open(csv_path, "w", newline="", encoding="utf-8") as f:
#                                 writer = csv.writer(f)
#                                 writer.writerows(
#                                     [[(c or "").strip() for c in row] for row in table]
#                                 )

#                             tables.append(
#                                 TableRef(
#                                     table_id=table_id,
#                                     csv_path=str(csv_path),
#                                     n_rows=len(table),
#                                     n_cols=max(len(r) for r in table),
#                                 )
#                             )

#                             text += f"\n[TABLE:{table_id}]\n"

#                     page_texts.append(text)

#                 full_text = self._normalize("\n\n".join(page_texts))

#                 status = "ok"

#                 if total_pages == 0:
#                     status = "error"

#                 elif total_pages > 0 and (non_empty_pages / total_pages) < MIN_TEXT_PAGE_RATIO:
#                     status = "ocr_required"

#                 return ParsedDocument(
#                     source_file=str(file_path),
#                     company=company,
#                     doc_type=doc_type,
#                     parser_used=self.name,
#                     status=status,
#                     raw_text=full_text,
#                     tables=tables,
#                 ).finalize()

#         except Exception as e:
#             return self._error_doc(file_path, e)

#     def _normalize(self, text: str) -> str:
#         text = text.replace("\xa0", " ").replace("\r", "")
#         text = re.sub(r"[ \t]+", " ", text)
#         text = "\n".join(line.strip() for line in text.split("\n"))
#         text = re.sub(r"\n{3,}", "\n\n", text)
#         return text.strip()


# def file_hash(path: Path) -> str:
#     h = hashlib.sha256()
#     h.update(path.read_bytes())
#     return h.hexdigest()


# def discover_company_pdfs(root: Path) -> dict[str, list[Path]]:
#     companies: dict[str, list[Path]] = {}

#     if not root.exists():
#         return companies

#     for sub in sorted(p for p in root.iterdir() if p.is_dir()):
#         files = sorted(sub.rglob("*.pdf"))
#         if files:
#             companies[sub.name] = files

#     if not companies:
#         flat_files = sorted(root.glob("*.pdf"))
#         for f in flat_files:
#             companies.setdefault(f.stem, []).append(f)

#     return companies


# def dedupe_files(
#     companies: dict[str, list[Path]]
# ) -> tuple[dict[str, list[Path]], list[tuple[str, Path, Path]]]:
#     seen: dict[str, Path] = {}
#     duplicates: list[tuple[str, Path, Path]] = []
#     deduped: dict[str, list[Path]] = {}

#     for company, files in companies.items():
#         kept = []
#         for f in files:
#             h = file_hash(f)
#             if h in seen:
#                 duplicates.append((company, f, seen[h]))
#                 continue
#             seen[h] = f
#             kept.append(f)
#         if kept:
#             deduped[company] = kept

#     return deduped, duplicates


# def main():
#     ap = argparse.ArgumentParser()
#     ap.add_argument("--root", default="data/01_raw/Sustainability Reports")
#     ap.add_argument("--out", default="data/raw_text/pdf_text")
#     ap.add_argument("--tables", default="data/tables/pdf_table")
#     ap.add_argument("--all", action="store_true")
#     ap.add_argument("--min-companies", type=int, default=5)
#     ap.add_argument("--num-companies", type=int, default=5)
#     args = ap.parse_args()

#     root = Path(args.root)

#     if not root.exists():
#         print(f"Folder not found: {root.resolve()}")
#         sys.exit(1)

#     out_dir = Path(args.out)
#     table_dir = Path(args.tables)
#     out_dir.mkdir(parents=True, exist_ok=True)
#     table_dir.mkdir(parents=True, exist_ok=True)
#     log_dir = Path("logs")
#     log_dir.mkdir(parents=True, exist_ok=True)
#     error_log = log_dir / "parse_errors.log"

#     parser = PDFParser(table_output_dir=table_dir)

#     companies = discover_company_pdfs(root)
#     companies, duplicates = dedupe_files(companies)

#     if duplicates:
#         print(f"Removed {len(duplicates)} duplicate file(s):")
#         for company, dup, original in duplicates:
#             print(f"  {company}: {dup.name} is a duplicate of {original}")

#     if len(companies) < args.min_companies:
#         print(f"Only found {len(companies)} companies, need >= {args.min_companies}.")

#     if args.num_companies and len(companies) > args.num_companies:
#         selected_names = sorted(companies.keys())[: args.num_companies]
#         companies = {name: companies[name] for name in selected_names}

#     print(f"Running on {len(companies)} companies: {list(companies.keys())}\n")

#     results = []
#     n_errors = 0
#     n_total = 0

#     for company, files in companies.items():
#         targets = files if args.all else files[:1]
#         for f in targets:
#             n_total += 1
#             t0 = time.time()
#             try:
#                 doc = parser.parse(f, company=company, doc_type="ESG/10-K-fallback")
#                 elapsed = time.time() - t0
#                 results.append({
#                     "company": company,
#                     "file": f.name,
#                     "status": doc.status,
#                     "chars": doc.char_count,
#                     "tables": doc.table_count,
#                     "time_s": round(elapsed, 2),
#                     "error": doc.error_message,
#                 })

#                 if doc.status in ("ok", "ocr_required"):
#                     out_path = out_dir / f"{company}__{f.stem}.txt"
#                     out_path.write_text(doc.raw_text, encoding="utf-8")
#                 else:
#                     n_errors += 1
#                     with open(error_log, "a", encoding="utf-8") as log:
#                         log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{company}\t{f}\t{doc.status}\t{doc.error_message}\n")

#             except Exception as e:
#                 n_errors += 1
#                 results.append({
#                     "company": company,
#                     "file": f.name,
#                     "status": "EXCEPTION",
#                     "chars": 0,
#                     "tables": 0,
#                     "time_s": round(time.time() - t0, 2),
#                     "error": f"{type(e).__name__}: {e}",
#                 })
#                 with open(error_log, "a", encoding="utf-8") as log:
#                     log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{company}\t{f}\tEXCEPTION\t{type(e).__name__}: {e}\n")

#     print(f"{'company':<22}{'file':<35}{'status':<14}{'chars':<10}{'tables':<8}{'time(s)':<8}")
#     print("-" * 100)
#     n_ok = 0
#     n_ocr = 0
#     for r in results:
#         print(f"{r['company']:<22}{r['file'][:33]:<35}{r['status']:<14}{r['chars']:<10}{r['tables']:<8}{r['time_s']:<8}")
#         if r["status"] == "ok":
#             n_ok += 1
#         elif r["status"] == "ocr_required":
#             n_ocr += 1
#             print("    -> scanned/no-text PDF correctly flagged, did NOT crash")
#         elif r["error"]:
#             print(f"    -> {r['error']}")

#     print("-" * 100)
#     print(f"OK: {n_ok}  |  OCR_REQUIRED: {n_ocr}  |  ERROR/EXCEPTION: {n_errors}  |  Companies tested: {len(companies)}")
#     print(f"\nExtracted text written to: {out_dir.resolve()}")
#     print(f"Extracted tables written to: {table_dir.resolve()}")

#     if n_total > 0 and (n_errors / n_total) >= 0.05:
#         sys.exit(1)


# if __name__ == "__main__":
#     main()


from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
import time
from pathlib import Path

import pdfplumber

from base_parser import BaseParser, ParsedDocument, TableRef

MIN_TEXT_PAGE_RATIO = 0.5
MIN_CHARS_PER_PAGE = 20

# Threshold below which an .htm/.html 10-K "parse" is treated as failed/garbled,
# triggering a fallback to the PDF version of the same filing.
MIN_HTML_CHARS = 2000


class PDFParser(BaseParser):
    """
    Extracts full text (and optionally tables) from PDFs using pdfplumber.

    Used both for:
      - ESG / sustainability report PDFs (primary source for that doc type)
      - 10-K PDFs, as a fallback when a company's .htm 10-K fails to parse cleanly

    Never raises on malformed/scanned PDFs: empty or near-empty extractions are
    detected and flagged with status "ocr_required" instead of crashing or
    silently returning garbage.
    """

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
            with pdfplumber.open(file_path) as pdf:

                page_texts = []
                tables = []
                table_counter = 0
                non_empty_pages = 0
                total_pages = len(pdf.pages)

                for page in pdf.pages:

                    text = page.extract_text() or ""

                    if len(text.strip()) >= MIN_CHARS_PER_PAGE:
                        non_empty_pages += 1

                    if extract_tables:
                        for table in page.extract_tables():

                            if not table or not any(
                                any((c or "").strip() for c in row)
                                for row in table
                            ):
                                continue

                            table_counter += 1
                            table_id = f"table_{table_counter}"

                            csv_path = self.table_output_dir / f"{file_path.stem}__{table_id}.csv"

                            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                                writer = csv.writer(f)
                                writer.writerows(
                                    [[(c or "").strip() for c in row] for row in table]
                                )

                            tables.append(
                                TableRef(
                                    table_id=table_id,
                                    csv_path=str(csv_path),
                                    n_rows=len(table),
                                    n_cols=max(len(r) for r in table),
                                )
                            )

                            text += f"\n[TABLE:{table_id}]\n"

                    page_texts.append(text)

                full_text = self._normalize("\n\n".join(page_texts))

                status = "ok"

                if total_pages == 0:
                    status = "error"

                elif total_pages > 0 and (non_empty_pages / total_pages) < MIN_TEXT_PAGE_RATIO:
                    status = "ocr_required"

                return ParsedDocument(
                    source_file=str(file_path),
                    company=company,
                    doc_type=doc_type,
                    parser_used=self.name,
                    status=status,
                    raw_text=full_text,
                    tables=tables,
                ).finalize()

        except Exception as e:
            return self._error_doc(file_path, e)

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


# ---------------------------------------------------------------------------
# 10-K handling: prefer the .htm filing, fall back to the PDF version of the
# same filing if the HTML parse comes back empty/garbled.
# ---------------------------------------------------------------------------

def htm_extract_text(path: Path) -> str:
    """Best-effort plain-text extraction from a 10-K .htm/.html filing."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        # bs4 not installed -> treat as an unreadable htm so we fall back to PDF
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
    """
    Heuristic check for whether an .htm 10-K parsed "cleanly": returns
    (is_clean, extracted_text). A parse is considered unclean/failed if it
    yields too little text (e.g. XBRL-only stub, broken markup, JS-rendered
    shell page with no real content).
    """
    text = htm_extract_text(path)
    return len(text) >= min_chars, text


def discover_10k_filings(root: Path) -> dict[str, dict[str, Path | None]]:
    """
    For each company subfolder under `root`, find the primary .htm/.html 10-K
    filing and a companion PDF version (if one exists) to use as a fallback.

    Returns: {company: {"htm": Path | None, "pdf": Path | None}}
    """
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
    """
    Decide whether to use the .htm 10-K or fall back to its PDF counterpart.

    Returns (source_type, path, htm_text) where source_type is one of:
      "htm"          - the .htm filing parsed cleanly, use its extracted text directly
      "pdf_fallback" - the .htm was missing or parsed dirty, use the PDF via PDFParser
      "missing"      - neither a usable .htm nor a .pdf was found for this company
    """
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
    ap.add_argument("--all", action="store_true",
                     help="Process every ESG PDF per company, not just the first.")
    ap.add_argument("--skip-10k", action="store_true",
                     help="Skip 10-K processing and only run ESG report parsing.")
    ap.add_argument("--min-companies", type=int, default=5)
    ap.add_argument("--num-companies", type=int, default=5)
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

    # ---------------- ESG / sustainability report PDFs ----------------

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

    for company, files in companies.items():
        targets = files if args.all else files[:1]
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

    # ---------------- 10-K filings: htm-first, PDF fallback ----------------

    if not args.skip_10k:
        tenk_root = Path(args.tenk_root)
        filings = discover_10k_filings(tenk_root)

        if not filings:
            print(f"\nNo 10-K filings found under: {tenk_root.resolve()} (skipping 10-K stage)")
        else:
            # Keep to the same set of companies already selected above when possible,
            # otherwise just process whatever 10-K filings were found.
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

                # source_type == "pdf_fallback"
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

    # ---------------- report ----------------

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