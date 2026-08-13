import fitz  # PyMuPDF
from pathlib import Path
import hashlib

seen_hashes = set()

def extract_images_from_pdf(pdf_path, output_dir):
    """Extract all embedded images from a PDF, save with page reference."""
    doc = fitz.open(pdf_path)
    company = pdf_path.parent.name
    pdf_stem = pdf_path.stem
    
    company_out = output_dir / company
    company_out.mkdir(parents=True, exist_ok=True)
    
    results = []
    total_images = 0
    
    for page_num in range(len(doc)):
        page = doc[page_num]
        image_list = page.get_images(full=True)
        
        for img_index, img in enumerate(image_list):
            xref = img[0]
            base_image = doc.extract_image(xref)
            image_bytes = base_image["ext"]  # file extension like 'png', 'jpeg'
            image_data = base_image["image"]
            width = base_image.get("width", 0)
            height = base_image.get("height", 0)

            # Skip exact duplicate images (same content, e.g. repeated logo/watermark)
            img_hash = hashlib.md5(image_data).hexdigest()
            if img_hash in seen_hashes:
                continue
            seen_hashes.add(img_hash)

            # Skip tiny decorative images (spacers, tracking pixels, icons)
            MIN_DIMENSION = 100
            if width < MIN_DIMENSION or height < MIN_DIMENSION:
                continue
            
            out_filename = f"{pdf_stem}__page{page_num+1}__img{img_index}.{image_bytes}"
            out_path = company_out / out_filename
            
            with open(out_path, "wb") as f:
                f.write(image_data)
            
            results.append({
                "company": company,
                "pdf": pdf_stem,
                "page": page_num + 1,
                "image_index": img_index,
                "filename": out_filename,
                "width": base_image.get("width"),
                "height": base_image.get("height"),
                "size_bytes": len(image_data),
            })
            total_images += 1
    
    doc.close()
    return results, total_images

def main():
    input_dir = Path("data/01_raw/sustainability")
    output_dir = Path("data/06_esg_images")
    
    pdf_files = list(input_dir.rglob("*.pdf"))
    print(f"Found {len(pdf_files)} PDFs to process")
    
    all_results = []
    for pdf_path in pdf_files:
        print(f"\nProcessing: {pdf_path.name}")
        results, count = extract_images_from_pdf(pdf_path, output_dir)
        print(f"  Extracted {count} images")
        all_results.extend(results)
    
    print(f"\nTotal images extracted: {len(all_results)}")
    
    import csv
    if all_results:
        index_path = Path("data/00_reference/esg_images_index.csv")
        with open(index_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=all_results[0].keys())
            writer.writeheader()
            writer.writerows(all_results)
        print(f"Index saved to {index_path}")

if __name__ == "__main__":
    main()