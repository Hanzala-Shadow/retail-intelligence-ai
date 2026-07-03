from __future__ import annotations

import argparse
import gc
import time
import os
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import psutil
import pdfplumber

from base_parser import ParsedDocument


def mem():
    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)


MIN_CHARS = 20


class PDFParser:

    def parse(self, file_path, company=None, **kwargs):

        file_path = Path(file_path)
        pages_text = []

        with pdfplumber.open(file_path) as pdf:

            for i in range(len(pdf.pages)):

                print(f"[{company}] Page {i+1} RAM {mem():.2f}MB", flush=True)

                try:
                    page = pdf.pages[i]
                    text = page.extract_text_simple() or ""

                    if len(text.strip()) > MIN_CHARS:
                        pages_text.append(text)

                except Exception as e:
                    print(f"fail page {i+1}: {e}")

                finally:
                    # pdfplumber caches every Page object (and its parsed
                    # layout/chars) inside pdf._pages for the life of the
                    # PDF object. Local del/gc does nothing until we clear
                    # that cache ourselves.
                    try:
                        page.flush_cache()
                    except AttributeError:
                        # Older pdfplumber: no flush_cache(), so clear the
                        # known cached attrs manually.
                        for attr in ("_objects", "_layout", "_chars",
                                     "_lines", "_rects", "_curves", "_images"):
                            if hasattr(page, attr):
                                try:
                                    delattr(page, attr)
                                except AttributeError:
                                    pass
                    # Drop pdfplumber's own reference to this page too.
                    if hasattr(pdf, "_pages") and pdf._pages is not None:
                        pdf._pages[i] = None

                    page = None
                    text = None

                    if (i + 1) % 10 == 0:
                        gc.collect()

        return ParsedDocument(
            source_file=str(file_path),
            company=company,
            raw_text="\n".join(pages_text),
        ).finalize()


def discover(root):
    root = Path(root)
    out = {}

    for d in root.iterdir():
        if d.is_dir():
            pdfs = list(d.glob("*.pdf"))
            if pdfs:
                out[d.name] = pdfs

    return out


def _set_memory_limit(max_mb=2048):
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (max_mb * 1024 * 1024, resource.RLIM_INFINITY))


def _parse_one(args):
    _set_memory_limit(2048)  # worker gets killed with MemoryError past this, not the whole box
    """Runs in a fresh worker process. All memory (pdfplumber page cache
    AND pdfminer's shared PDFResourceManager, which is what actually leaks
    on image/font-heavy PDFs like CROX2) is reclaimed by the OS the moment
    this worker process exits -- no manual cleanup can substitute for that.
    """
    file_path, company = args
    p = PDFParser()
    try:
        doc = p.parse(file_path, company=company)
        return (company, file_path.name, len(doc.raw_text), None)
    except Exception as e:
        return (company, file_path.name, 0, f"{type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/test")
    ap.add_argument("--num-companies", type=int, default=2)
    ap.add_argument(
        "--workers", type=int, default=1,
        help="parallel worker processes (each PDF still gets its own "
             "process lifetime regardless of this value)"
    )
    args = ap.parse_args()

    data = discover(args.root)
    data = dict(list(data.items())[: args.num_companies])

    jobs = [(f, c) for c, files in data.items() for f in files]

    # maxtasksperchild=1 is the important part: each worker process is
    # killed and replaced after ONE pdf, so pdfminer's resource manager
    # (fonts/images/XObjects) can never accumulate across files.
    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=args.workers,
        mp_context=ctx,
        max_tasks_per_child=1,
    ) as pool:
        futures = [pool.submit(_parse_one, job) for job in jobs]
        for fut in as_completed(futures):
            company, fname, nchars, err = fut.result()
            print(f"\n==== {company} {fname}")
            if err:
                print("FAILED:", err)
            else:
                print("DONE", nchars)


if __name__ == "__main__":
    main()
