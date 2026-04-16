# scripts/chunk.py
import json
import re
from pathlib import Path
from tqdm import tqdm

TEXT_DIR = Path("data/texts")
CHUNK_DIR = Path("data/chunks")
CHUNK_DIR.mkdir(parents=True, exist_ok=True)

CHUNK_SIZE_WORDS = 300   # target chunk size
OVERLAP_WORDS = 50        # overlap between consecutive chunks


# NCERT section heading patterns — these signal topic boundaries
HEADING_PATTERNS = [
    r"^\d+\.\d+\s+[A-Z]",          # e.g. "1.2 Chemical Reactions"
    r"^[A-Z][A-Z\s]{5,}$",          # ALL CAPS headings
    r"^\d+\.\s+[A-Z]",              # e.g. "1. Introduction"
    r"^Activity\s+\d+",             # Activity blocks
    r"^Example\s+\d+",              # Worked examples
    r"^SUMMARY",
    r"^KEY TERMS",
    r"^EXERCISES",
]


def is_heading(line: str) -> bool:
    for pattern in HEADING_PATTERNS:
        if re.match(pattern, line.strip()):
            return True
    return False


def extract_page_number(text_block: str) -> int:
    """Extract [PAGE:N] tag from a block."""
    match = re.search(r"\[PAGE:(\d+)\]", text_block)
    return int(match.group(1)) if match else 0


def split_into_paragraphs(text: str) -> list[dict]:
    """
    Split text into paragraphs, tagging each with its page number
    and whether it starts a new section (heading detected).
    """
    paragraphs = []
    current_page = 1

    # Split on double newlines (paragraph breaks) and page tags
    blocks = re.split(r"\n\n+", text)

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # Update page tracker
        page_match = re.search(r"\[PAGE:(\d+)\]", block)
        if page_match:
            current_page = int(page_match.group(1))
            block = re.sub(r"\[PAGE:\d+\]\n?", "", block).strip()

        if not block:
            continue

        paragraphs.append({
            "text": block,
            "page": current_page,
            "is_heading": is_heading(block),
        })

    return paragraphs


def words(text: str) -> list[str]:
    return text.split()


def chunk_paragraphs(paragraphs: list[dict], metadata: dict) -> list[dict]:
    """
    Sliding window chunker that:
    - Respects heading boundaries (starts a new chunk at each heading)
    - Targets CHUNK_SIZE_WORDS words per chunk
    - Adds OVERLAP_WORDS words from the end of the previous chunk
    """
    chunks = []
    chunk_id = 0

    current_words = []
    current_page = 1
    current_topic = "Introduction"
    overlap_buffer = []  # last OVERLAP_WORDS words of previous chunk

    def flush_chunk():
        nonlocal chunk_id, overlap_buffer
        if not current_words:
            return
        text = " ".join(current_words)
        chunks.append({
            "chunk_id": f"{metadata['subject']}_{metadata['board']}_{metadata['class']}_chunk{chunk_id:04d}",
            "text": text,
            "word_count": len(current_words),
            "page_num": current_page,
            "topic": current_topic,
            "chapter": metadata.get("chapter", ""),
            "subject": metadata["subject"],
            "board": metadata["board"],
            "class": metadata["class"],
        })
        # Save last OVERLAP_WORDS for next chunk's beginning
        overlap_buffer = current_words[-OVERLAP_WORDS:] if len(current_words) > OVERLAP_WORDS else current_words[:]
        chunk_id += 1

    for para in paragraphs:
        para_words = words(para["text"])

        # Heading → flush current chunk and start fresh (no overlap at topic boundary)
        if para["is_heading"]:
            flush_chunk()
            current_topic = para["text"][:80]  # use heading text as topic name
            current_words = para_words[:]
            current_page = para["page"]
            continue

        # Would adding this paragraph exceed our target size?
        if len(current_words) + len(para_words) > CHUNK_SIZE_WORDS and current_words:
            flush_chunk()
            # Start new chunk with overlap from previous
            current_words = overlap_buffer[:] + para_words
        else:
            current_words.extend(para_words)

        current_page = para["page"]

    # Flush any remaining content
    flush_chunk()

    return chunks


def chunk_file(txt_path: Path) -> list[dict]:
    text = txt_path.read_text(encoding="utf-8")
    parts = txt_path.stem.split("_")
    metadata = {
        "subject": parts[0] if len(parts) > 0 else "unknown",
        "board": parts[1] if len(parts) > 1 else "cbse",
        "class": parts[2] if len(parts) > 2 else "class10",
        "chapter": "",  # fill in manually or parse from filename
    }

    paragraphs = split_into_paragraphs(text)
    chunks = chunk_paragraphs(paragraphs, metadata)
    return chunks


def chunk_all():
    txt_files = list(TEXT_DIR.glob("*.txt"))
    if not txt_files:
        print(f"No .txt files in {TEXT_DIR}. Run parse_pdf.py first.")
        return

    total_chunks = 0
    for txt_path in txt_files:
        print(f"Chunking: {txt_path.name}")
        chunks = chunk_file(txt_path)

        output_path = CHUNK_DIR / f"{txt_path.stem}_chunks.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(chunks, f, ensure_ascii=False, indent=2)

        print(f"  {len(chunks)} chunks → {output_path}")
        total_chunks += len(chunks)

    print(f"\nTotal chunks across all books: {total_chunks}")
    print(f"Estimated Qdrant storage: ~{total_chunks * 4:.0f} KB")


if __name__ == "__main__":
    chunk_all()