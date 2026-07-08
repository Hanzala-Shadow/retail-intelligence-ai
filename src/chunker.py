import csv
import tiktoken
from pathlib import Path

# Chunking settings
CHUNK_SIZE = 500      # tokens per chunk
OVERLAP = 50          # tokens overlap between chunks
ENCODING = 'cl100k_base'  # works for GPT-4 and Claude

def chunk_text(text, encoder, chunk_size=CHUNK_SIZE, overlap=OVERLAP):
    """Split text into overlapping chunks of roughly chunk_size tokens."""
    tokens = encoder.encode(text)
    chunks = []
    start = 0
    
    while start < len(tokens):
        end = min(start + chunk_size, len(tokens))
        chunk_tokens = tokens[start:end]
        chunk_text = encoder.decode(chunk_tokens)
        chunks.append(chunk_text)
        
        if end == len(tokens):
            break
        start = end - overlap
    
    return chunks

def main():
    # Proper directory routes matching project structure
    input_dir = Path('data/03_sections')
    output_dir = Path('data/04_chunks/10k')
    output_dir.mkdir(parents=True, exist_ok=True)

    encoder = tiktoken.get_encoding(ENCODING)
    
    # Use Hanzala's final chunkable list
    chunkable_file = Path('reports/chunkable_10k_sections_final.txt')
    if not chunkable_file.exists():
        # Fallback to original chunkable list
        chunkable_file = Path('reports/chunkable_10k_sections.txt')
    
    if chunkable_file.exists():
        raw_lines = open(chunkable_file, encoding='utf-8').read().strip().split('\n')
        chunkable = set(Path(l.strip()).name for l in raw_lines if l.strip())
        print(f"Using chunkable list: {len(chunkable)} sections from {chunkable_file.name}")
    else:
        chunkable = None
        print("No chunkable list found — processing all sections")

    section_files = list(input_dir.rglob('*.txt'))
    print(f"Found {len(section_files)} section files locally")

    all_results = []
    total_chunks = 0
    skipped = 0

    for section_file in section_files:
        # Skip fallback sections
        if 'FULL_DOCUMENT_FALLBACK' in section_file.name:
            skipped += 1
            continue
        
        # Skip if not in chunkable list
        if chunkable and section_file.name not in chunkable:
            skipped += 1
            continue

        text = section_file.read_text(encoding='utf-8')
        if not text.strip():
            skipped += 1
            continue

        # Parse filename to get metadata
        parts = section_file.stem.split('__')
        company = parts[0] if len(parts) > 0 else 'UNKNOWN'
        doc_type = parts[1] if len(parts) > 1 else 'UNKNOWN'
        accession = parts[2] if len(parts) > 2 else 'UNKNOWN'
        section = parts[3] if len(parts) > 3 else 'UNKNOWN'

        # Chunk the text
        chunks = chunk_text(text, encoder)

        # Save each chunk
        for i, chunk in enumerate(chunks):
            chunk_id = f"{section_file.stem}__chunk_{i:04d}"
            chunk_file = output_dir / f"{chunk_id}.txt"
            chunk_file.write_text(chunk, encoding='utf-8')

            all_results.append({
                'chunk_id': chunk_id,
                'company': company,
                'doc_type': doc_type,
                'accession': accession,
                'section': section,
                'chunk_index': i,
                'token_count': len(encoder.encode(chunk)),
                'char_count': len(chunk),
                'file': str(chunk_file)
            })
            total_chunks += 1

        print(f"  {section_file.stem}: {len(chunks)} chunks")

    # Save index
    index_path = Path('data/00_reference/chunks_index.csv')
    with open(index_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'chunk_id', 'company', 'doc_type', 'accession',
            'section', 'chunk_index', 'token_count', 'char_count', 'file'
        ])
        writer.writeheader()
        writer.writerows(all_results)

    print(f"\nDone! {total_chunks} total chunks created.")
    print(f"Skipped: {skipped} sections")
    print(f"Index saved to {index_path}")
    print(f"Chunks saved to {output_dir.resolve()}")

if __name__ == '__main__':
    main()