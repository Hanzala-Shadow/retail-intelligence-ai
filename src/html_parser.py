from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup, Comment

from base_parser import BaseParser, ParsedDocument, TableRef

ITEM_HEADING_RE = re.compile(
    r"""^\s*item\s*(\d{1,2}[A-Za-z]?)\s*[\.\:\-\u2013\u2014]?\s*(.*)$""",
    re.IGNORECASE | re.VERBOSE,
)

NON_CONTENT_TAGS = ["script", "style", "noscript"]

MIN_TEXT_CHARS = 2000


class HTMLParser(BaseParser):
    name = "HTMLParser"

    def parse(
        self,
        file_path: str | Path,
        company: str | None = None,
        doc_type: str | None = "10-K",
        encoding: str = "utf-8",
    ) -> ParsedDocument:

        file_path = Path(file_path)

        try:
            raw_html = self._read(file_path, encoding)
            soup = BeautifulSoup(raw_html, "lxml")

            self._strip_non_content(soup)

            tables = self._extract_tables(soup, file_path)

            text = soup.get_text(separator="\n")
            text = self._normalize(text)
            text = self._preserve_items(text)

            doc = ParsedDocument(
                source_file=str(file_path),
                company=company,
                doc_type=doc_type,
                parser_used=self.name,
                status="ok",
                raw_text=text,
                tables=tables,
            )

            return doc.finalize()

        except Exception:
            return ParsedDocument(
                source_file=str(file_path),
                company=company,
                doc_type=doc_type,
                parser_used=self.name,
                status="error",
                raw_text="",
                tables=[],
            ).finalize()

    def _read(self, file_path: Path, encoding: str) -> str:
        return file_path.read_text(encoding=encoding, errors="replace")

    def _strip_non_content(self, soup: BeautifulSoup) -> None:
        for tag in NON_CONTENT_TAGS:
            for t in soup.find_all(tag):
                t.decompose()

        for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
            c.extract()

    def _extract_tables(self, soup: BeautifulSoup, file_path: Path) -> list[TableRef]:
        """
        Locate <table> elements, save each as a CSV, and replace it in the
        soup with a [TABLE:id] placeholder.

        CSV naming matches pdf_parser.py's pattern exactly:
            {file_path.stem}__{table_id}.csv
        This keeps tables from different source files from colliding in a
        shared table_output_dir, and keeps both parsers' table artifacts
        addressable the same way.
        """
        tables = []
        table_counter = 0

        for table in soup.find_all("table"):
            rows = []

            for tr in table.find_all("tr"):
                cells = tr.find_all(["td", "th"])
                row = [self._clean(c.get_text()) for c in cells]
                if row:
                    rows.append(row)

            if not rows:
                table.decompose()
                continue

            table_counter += 1
            table_id = f"table_{table_counter}"

            csv_path = self.table_output_dir / f"{file_path.stem}__{table_id}.csv"

            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(
                    f,
                    lineterminator="\n",
                )
                writer.writerows(rows)

            tables.append(
                TableRef(
                    table_id=table_id,
                    csv_path=str(csv_path),
                    n_rows=len(rows),
                    n_cols=max(len(r) for r in rows),
                )
            )

            # Preserve the table's original text line structure.
            # SEC filings frequently use tables for page layout. Collapsing
            # each row into one line can bury Item headings inside long
            # narrative lines, so retain cell-level newlines for splitting.
            readable_text = table.get_text(
                separator="\n",
                strip=True,
            )
            readable_text = self._normalize(readable_text)

            replacement_parts = [f"[TABLE:{table_id}]"]

            if readable_text:
                replacement_parts.append(readable_text)

            replacement = "\n".join(replacement_parts)
            table.replace_with(f"\n{replacement}\n")

        return tables

    def _clean(self, text: str) -> str:
        return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()

    def _normalize(self, text: str) -> str:
        text = text.replace("\xa0", " ")
        text = re.sub(r"[ \t]+", " ", text)
        text = "\n".join(line.strip() for line in text.split("\n"))
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _preserve_items(self, text: str) -> str:
        out = []
        for line in text.split("\n"):
            m = ITEM_HEADING_RE.match(line)
            if m and len(line) < 200:
                if out and out[-1] != "":
                    out.append("")
                num = m.group(1)
                title = m.group(2).strip()
                out.append(f"Item {num}. {title}".strip())
                out.append("")
            else:
                out.append(line)
        return "\n".join(out).strip()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def discover_company_files(root: Path) -> dict[str, list[Path]]:
    if not root.exists():
        print(f"Folder not found: {root.resolve()}")
        sys.exit(1)

    companies: dict[str, list[Path]] = {}

    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        files = sorted(list(sub.rglob("*.htm")) + list(sub.rglob("*.html")))
        if files:
            companies[sub.name] = files

    if not companies:
        flat_files = sorted(list(root.glob("*.htm")) + list(root.glob("*.html")))
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/01_raw/10-K filings")
    ap.add_argument("--out", default="data/raw_text/html_text")
    ap.add_argument("--tables", default="data/tables/html_table")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--min-companies", type=int, default=5)
    ap.add_argument("--num-companies", type=int, default=5)
    args = ap.parse_args()

    root = Path(args.root)
    out_dir = Path(args.out)
    table_dir = Path(args.tables)

    out_dir.mkdir(parents=True, exist_ok=True)
    table_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    error_log = log_dir / "parse_errors.log"

    parser = HTMLParser(table_output_dir=table_dir)

    companies = discover_company_files(root)
    companies, duplicates = dedupe_files(companies)

    if duplicates:
        print(f"Removed {len(duplicates)} duplicate file(s):")
        for company, dup, original in duplicates:
            print(f"  {company}: {dup.name} is a duplicate of {original}")

    if len(companies) < args.min_companies:
        print(f"Only found {len(companies)} companies under {root}, need >= {args.min_companies}.")

    if args.num_companies and len(companies) > args.num_companies:
        selected_names = sorted(companies.keys())[: args.num_companies]
        companies = {name: companies[name] for name in selected_names}

    print(f"Running on {len(companies)} companies: {list(companies.keys())}\n")

    results = []
    n_errors = 0
    n_total = 0
    doc_type = "10-K"

    for company, files in companies.items():
        targets = files
        for f in targets:
            n_total += 1
            t0 = time.time()
            try:
                doc = parser.parse(f, company=company, doc_type=doc_type)
                elapsed = time.time() - t0
                results.append({
                    "company": company,
                    "file": f.name,
                    "doc_type": doc_type,
                    "status": doc.status,
                    "chars": doc.char_count,
                    "tables": doc.table_count,
                    "time_s": round(elapsed, 2),
                    "error": doc.error_message,
                })

                if doc.status == "ok" and doc.char_count > 0:
                    
                    out_path = out_dir / f"{company}__{doc_type}__{f.stem}.txt"
                    out_path.write_text(doc.raw_text, encoding="utf-8")
                    if doc.char_count < MIN_TEXT_CHARS:
                        print(f"Warning: {company}/{f.name} produced a suspiciously thin parse ({doc.char_count} chars)")
                else:
                    n_errors += 1
                    with open(error_log, "a", encoding="utf-8") as log:
                        log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{company}\t{f}\t{doc.status}\t{doc.error_message}\n")

            except Exception as e:
                n_errors += 1
                results.append({
                    "company": company,
                    "file": f.name,
                    "doc_type": doc_type,
                    "status": "EXCEPTION",
                    "chars": 0,
                    "tables": 0,
                    "time_s": round(time.time() - t0, 2),
                    "error": f"{type(e).__name__}: {e}",
                })
                with open(error_log, "a", encoding="utf-8") as log:
                    log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{company}\t{f}\tEXCEPTION\t{type(e).__name__}: {e}\n")

    print(f"{'company':<22}{'type':<16}{'file':<30}{'status':<14}{'chars':<10}{'tables':<8}{'time(s)':<8}")
    print("-" * 110)
    for r in results:
        print(f"{r['company']:<22}{r['doc_type']:<16}{r['file'][:28]:<30}{r['status']:<14}{r['chars']:<10}{r['tables']:<8}{r['time_s']:<8}")
        if r["status"] != "ok" and r["error"]:
            print(f"    -> {r['error']}")

    print("-" * 110)
    n_ok = n_total - n_errors
    print(f"OK: {n_ok}  |  PROBLEM: {n_errors}  |  Companies tested: {len(companies)}")
    print(f"\nExtracted text written to: {out_dir.resolve()}")
    print(f"Extracted tables written to: {table_dir.resolve()}")

    if n_total > 0 and (n_errors / n_total) >= 0.05:
        sys.exit(1)


if __name__ == "__main__":
    main()