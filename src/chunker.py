import csv
import shutil
from pathlib import Path

import tiktoken

CHUNK_SIZE = 500
OVERLAP = 50
MIN_CHUNK_SIZE = 50
ENCODING = "cl100k_base"

INPUT_DIR = Path("data/03_sections/10k")
OUTPUT_DIR = Path("data/04_chunks/10k")
CHUNKS_INDEX = Path("data/00_reference/chunks_index.csv")
TEMP_INDEX = CHUNKS_INDEX.with_suffix(".csv.tmp")


def encode_text(encoder, text):
    return encoder.encode(
        text,
        disallowed_special=(),
    )


def decode_source_slice(
    source_tokens,
    start,
    end,
    encoder,
    maximum_tokens=CHUNK_SIZE,
):
    """Decode a source-token slice while enforcing stored token size.

    A tiktoken decode followed by encode can occasionally increase the
    token count. Reduce the source slice until the stored text re-encodes
    within the configured maximum.
    """
    while end > start:
        text = encoder.decode(source_tokens[start:end])
        stored_count = len(encode_text(encoder, text))

        if stored_count <= maximum_tokens:
            return text, stored_count, end

        end -= 1

    raise ValueError(
        f"Could not create a valid chunk from token position {start}"
    )


def chunk_text(
    text,
    encoder,
    chunk_size=CHUNK_SIZE,
    overlap=OVERLAP,
    minimum_tokens=MIN_CHUNK_SIZE,
):
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    if overlap < 0 or overlap >= chunk_size:
        raise ValueError(
            "overlap must satisfy 0 <= overlap < chunk_size"
        )

    source_tokens = encode_text(encoder, text)

    if len(source_tokens) < minimum_tokens:
        return []

    chunks = []
    start = 0
    source_length = len(source_tokens)

    while start < source_length:
        requested_end = min(
            start + chunk_size,
            source_length,
        )

        chunk, token_count, actual_end = decode_source_slice(
            source_tokens,
            start,
            requested_end,
            encoder,
            maximum_tokens=chunk_size,
        )

        chunks.append({
            "text": chunk,
            "token_count": token_count,
            "source_start": start,
            "source_end": actual_end,
        })

        if actual_end >= source_length:
            break

        next_start = actual_end - overlap

        if next_start <= start:
            next_start = start + 1

        start = next_start

    # This should rarely be needed because overlap normally guarantees
    # at least 51 source tokens in the final chunk. If normalization makes
    # the stored final chunk shorter, extend it backwards safely.
    if chunks and chunks[-1]["token_count"] < minimum_tokens:
        final = chunks[-1]
        adjusted_start = final["source_start"]

        while (
            final["token_count"] < minimum_tokens
            and adjusted_start > 0
        ):
            adjusted_start -= 1

            text_value, token_count, actual_end = (
                decode_source_slice(
                    source_tokens,
                    adjusted_start,
                    source_length,
                    encoder,
                    maximum_tokens=chunk_size,
                )
            )

            final = {
                "text": text_value,
                "token_count": token_count,
                "source_start": adjusted_start,
                "source_end": actual_end,
            }

        chunks[-1] = final

    for index, chunk in enumerate(chunks):
        token_count = len(
            encode_text(encoder, chunk["text"])
        )

        if token_count != chunk["token_count"]:
            raise ValueError(
                f"Chunk {index} token mismatch: "
                f"stored={chunk['token_count']} "
                f"actual={token_count}"
            )

        if not minimum_tokens <= token_count <= chunk_size:
            raise ValueError(
                f"Chunk {index} outside token range: "
                f"{token_count}"
            )

    return chunks


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
        raise FileNotFoundError(
            f"Input directory not found: {INPUT_DIR}"
        )

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CHUNKS_INDEX.parent.mkdir(parents=True, exist_ok=True)

    if TEMP_INDEX.exists():
        TEMP_INDEX.unlink()

    encoder = tiktoken.get_encoding(ENCODING)
    section_files = sorted(INPUT_DIR.rglob("*.txt"))

    print(
        f"Found {len(section_files)} section files "
        f"under {INPUT_DIR}"
    )
    print(
        "Policy: all non-fallback sections; "
        "skip sections under 50 tokens"
    )

    all_results = []
    seen_chunk_ids = set()

    total_chunks = 0
    chunked_sections = 0
    skipped_tiny = 0
    skipped_fallback = 0
    skipped_empty = 0
    bad_names = 0

    for section_file in section_files:
        if "FULL_DOCUMENT_FALLBACK" in section_file.name:
            skipped_fallback += 1
            continue

        meta = parse_section_filename(section_file)

        if meta is None:
            bad_names += 1
            continue

        text = section_file.read_text(
            encoding="utf-8",
            errors="replace",
        )

        if not text.strip():
            skipped_empty += 1
            continue

        section_token_count = len(
            encode_text(encoder, text)
        )

        if section_token_count < MIN_CHUNK_SIZE:
            skipped_tiny += 1
            continue

        chunks = chunk_text(text, encoder)

        if not chunks:
            raise RuntimeError(
                f"No chunks generated for eligible section: "
                f"{section_file}"
            )

        chunked_sections += 1

        for index, chunk in enumerate(chunks):
            chunk_id = (
                f"{section_file.stem}"
                f"__chunk_{index:04d}"
            )

            if chunk_id in seen_chunk_ids:
                raise RuntimeError(
                    f"Duplicate chunk ID: {chunk_id}"
                )

            seen_chunk_ids.add(chunk_id)

            token_count = len(
                encode_text(encoder, chunk["text"])
            )

            if not MIN_CHUNK_SIZE <= token_count <= CHUNK_SIZE:
                raise RuntimeError(
                    f"Invalid token count {token_count} "
                    f"for {chunk_id}"
                )

            chunk_file = OUTPUT_DIR / f"{chunk_id}.txt"
            chunk_file.write_text(
                chunk["text"],
                encoding="utf-8",
            )

            all_results.append({
                "chunk_id": chunk_id,
                "company": meta["company"],
                "doc_type": meta["doc_type"],
                "accession": meta["accession"],
                "section": meta["section"],
                "chunk_index": index,
                "token_count": token_count,
                "char_count": len(chunk["text"]),
                "file": str(chunk_file),
            })

            total_chunks += 1

        if total_chunks and total_chunks % 5000 < len(chunks):
            print(
                f"Progress: {total_chunks} chunks created"
            )

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

    with TEMP_INDEX.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(all_results)

    written_files = sum(
        1 for _ in OUTPUT_DIR.glob("*.txt")
    )

    if written_files != total_chunks:
        raise RuntimeError(
            f"Chunk file mismatch: "
            f"expected={total_chunks} "
            f"actual={written_files}"
        )

    if len(seen_chunk_ids) != total_chunks:
        raise RuntimeError(
            "Duplicate chunk IDs detected"
        )

    TEMP_INDEX.replace(CHUNKS_INDEX)

    token_counts = [
        int(row["token_count"])
        for row in all_results
    ]

    print()
    print("STRICT CHUNK REBUILD COMPLETE")
    print(f"Input sections: {len(section_files)}")
    print(f"Chunked sections: {chunked_sections}")
    print(f"Skipped tiny sections: {skipped_tiny}")
    print(f"Skipped fallbacks: {skipped_fallback}")
    print(f"Skipped empty sections: {skipped_empty}")
    print(f"Bad filenames: {bad_names}")
    print(f"Total chunks: {total_chunks}")
    print(f"Chunk files: {written_files}")
    print(f"Minimum tokens: {min(token_counts)}")
    print(f"Maximum tokens: {max(token_counts)}")
    print(
        "Average tokens: "
        f"{sum(token_counts) / len(token_counts):.2f}"
    )
    print(f"Index: {CHUNKS_INDEX}")
    print(f"Output: {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
