import fitz  # PyMuPDF
import pdfplumber
import re
from pathlib import Path

PDF_DIR = Path("data/pdfs")
TEXT_DIR = Path("data/texts")
TEXT_DIR.mkdir(parents=True, exist_ok=True)

# Configuration
MARGIN_PERCENT = 0.07  # Ignore top/bottom 7% for headers/footers

def extract_tables_with_plumber(pdf_path: Path, page_num: int) -> str:
    """Uses pdfplumber to find and extract tables as text-based grids."""
    table_text = ""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            # pdfplumber uses 0-based indexing, so we subtract 1
            page = pdf.pages[page_num - 1]
            tables = page.extract_tables()

            for table in tables:
                table_text += "\n[TABLE START]\n"
                for row in table:
                    # Clean up None values and join cells with a pipe |
                    clean_row = [str(cell).replace('\n', ' ') if cell else "" for cell in row]
                    table_text += "| " + " | ".join(clean_row) + " |\n"
                table_text += "[TABLE END]\n"
    except Exception as e:
        print(f"    Warning: Could not extract tables on page {page_num}: {e}")

    return table_text

def parse_pdf(pdf_path: Path) -> str:
    """Extract text and tables using a hybrid lightweight approach."""
    doc = fitz.open(pdf_path)
    full_text = []

    for page_num, page in enumerate(doc, start=1):
        page_height = page.rect.height
        content_top = page_height * MARGIN_PERCENT
        content_bottom = page_height * (1 - MARGIN_PERCENT)

        # 1. Extract Tables using pdfplumber (more accurate for grids)
        tables = extract_tables_with_plumber(pdf_path, page_num)

        # 2. Extract Text using PyMuPDF (faster for general content)
        page_dict = page.get_text("dict")
        page_content = []
        all_sizes = []

        # First pass: find average font size for heading detection
        for block in page_dict["blocks"]:
            if "lines" in block:
                for line in block["lines"]:
                    for span in line["spans"]:
                        all_sizes.append(span["size"])

        avg_size = sum(all_sizes) / len(all_sizes) if all_sizes else 12
        heading_threshold = avg_size * 1.2

        # Second pass: process blocks
        for block in page_dict["blocks"]:
            if block["type"] != 0: continue # Skip images/drawings

            bbox = block["bbox"]
            block_center_y = (bbox[1] + bbox[3]) / 2

            # Skip header/footer zones
            if block_center_y < content_top or block_center_y > content_bottom:
                continue

            block_parts = []
            is_heading = False

            for line in block["lines"]:
                for span in line["spans"]:
                    text = span["text"].strip()
                    if not text: continue

                    # Detect headings
                    if span["size"] > heading_threshold:
                        is_heading = True

                    # Basic noise filtering
                    if re.fullmatch(r"[-_=\s]+", text): continue
                    if re.search(r"www\.ncert\.nic\.in|NCERT", text, re.IGNORECASE) and len(text) < 40:
                        continue

                    block_parts.append(text)

            if block_parts:
                combined = " ".join(block_parts)
                if is_heading:
                    page_content.append(f"\n### {combined} ###\n")
                else:
                    page_content.append(combined)

        # Combine text and tables for the page
        page_output = "\n".join(page_content)
        if tables:
            # Insert tables after the text for that page
            page_output += f"\n{tables}"

        if page_output.strip():
            full_text.append(f"[PAGE:{page_num}]\n{page_output}")

    doc.close()
    return "\n\n".join(full_text)

def parse_all_pdfs():
    """Main loop to parse all PDFs in the directory."""
    pdf_files = list(PDF_DIR.glob("*.pdf"))
    if not pdf_files:
        print(f"No PDFs found in {PDF_DIR}.")
        return

    for pdf_path in pdf_files:
        print(f"Parsing: {pdf_path.name}")
        try:
            text = parse_pdf(pdf_path)
            output_path = TEXT_DIR / f"{pdf_path.stem}.txt"
            output_path.write_text(text, encoding="utf-8")
            print(f"  Saved to {output_path} ({len(text):,} chars)")
        except Exception as e:
            print(f"  Error parsing {pdf_path.name}: {e}")

if __name__ == "__main__":
    parse_all_pdfs()
    print("\nDone. Check data/texts/ for output.")
