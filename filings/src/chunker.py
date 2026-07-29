import csv
import shutil
from pathlib import Path

import tiktoken
import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config

CHUNK_SIZE = 500
OVERLAP = 50
ENCODING = "cl100k_base"

INPUT_DIR = config.SECTIONS_10K_DIR
OUTPUT_DIR = config.CHUNKS_10K_DIR
CHUNKS_INDEX = config.CHUNKS_INDEX_CSV
CHUNKABLE_FINAL = config.CHUNKABLE_10K_SECTIONS_FINAL_TXT
CHUNKABLE_OLD = config.CHUNKABLE_10K_SECTIONS_TXT


def chunk_text(text, encoder, chunk_size=CHUNK_SIZE, overlap=OVERLAP):
    tokens = encoder.encode(text)
    chunks = []
    start = 0

    while start < len(tokens):
        end = min(start + chunk_size, len(tokens))
        chunk_tokens = tokens[start:end]
        chunks.append(encoder.decode(chunk_tokens))

        if end == len(tokens):
            break

        start = end - overlap

    return chunks


def load_chunkable_names():
    chunkable_file = CHUNKABLE_FINAL if CHUNKABLE_FINAL.exists() else CHUNKABLE_OLD

    if not chunkable_file.exists():
        print("No chunkable list found. Processing all section files.")
        return None

    names = set()
    with open(chunkable_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                names.add(Path(line).name)

    print(f"Using chunkable list: {len(names)} sections from {chunkable_file}")
    return names


def parse_section_filename(section_file):
    parts = section_file.stem.split("__")
    if len(parts) < 4:
        return None

    return {
        "company": parts[0],
        "doc_type": parts[1],
        "accession": parts[2],
        "section": "__".join(parts[3:]),
    }


def main():
    if not INPUT_DIR.exists():
        raise FileNotFoundError(f"Input directory not found: {INPUT_DIR}")

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if CHUNKS_INDEX.exists():
        CHUNKS_INDEX.unlink()

    encoder = tiktoken.get_encoding(ENCODING)
    chunkable_names = load_chunkable_names()

    section_files = sorted(INPUT_DIR.rglob("*.txt"))
    print(f"Found {len(section_files)} section files under {INPUT_DIR}")

    all_results = []
    total_chunks = 0
    skipped = 0
    bad_names = 0

    for section_file in section_files:
        if "FULL_DOCUMENT_FALLBACK" in section_file.name:
            skipped += 1
            continue

        if chunkable_names is not None and section_file.name not in chunkable_names:
            skipped += 1
            continue

        meta = parse_section_filename(section_file)
        if meta is None:
            bad_names += 1
            skipped += 1
            continue

        text = section_file.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            skipped += 1
            continue

        # Skip tiny standalone sections.
        # DB QA requires chunks to be between 50 and 600 tokens.
        section_token_count = len(encoder.encode(text))
        if section_token_count < 50:
            skipped += 1
            continue

        chunks = chunk_text(text, encoder)

        for i, chunk in enumerate(chunks):
            token_count = len(encoder.encode(chunk))
            chunk_id = f"{section_file.stem}__chunk_{i:04d}"
            chunk_file = OUTPUT_DIR / f"{chunk_id}.txt"
            chunk_file.write_text(chunk, encoding="utf-8")

            all_results.append({
                "chunk_id": chunk_id,
                "company": meta["company"],
                "doc_type": meta["doc_type"],
                "accession": meta["accession"],
                "section": meta["section"],
                "chunk_index": i,
                "token_count": token_count,
                "char_count": len(chunk),
                "file": str(chunk_file),
            })

            total_chunks += 1

        if total_chunks % 5000 < len(chunks):
            print(f"Progress: {total_chunks} chunks created...")

    CHUNKS_INDEX.parent.mkdir(parents=True, exist_ok=True)
    with open(CHUNKS_INDEX, "w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "chunk_id",
            "company",
            "doc_type",
            "accession",
            "section",
            "chunk_index",
            "token_count",
            "char_count",
            "file",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_results)

    print()
    print(f"Done. {total_chunks} chunks created.")
    print(f"Skipped sections: {skipped}")
    print(f"Bad filename sections: {bad_names}")
    print(f"Index saved to: {CHUNKS_INDEX}")
    print(f"Chunks saved to: {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
