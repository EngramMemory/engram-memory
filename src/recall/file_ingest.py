"""
File ingestion for engram-memory.
Extracts text from PDF, DOCX, Markdown, and plain text files,
chunks it, and returns chunks ready to store as memories.
"""
import os
import re
from typing import Iterator

CHUNK_SIZE = 500      # target chars per chunk
CHUNK_OVERLAP = 80   # overlap between chunks


def _chunk_text(text: str, source: str) -> list[dict]:
    """Split text into overlapping chunks with metadata."""
    # Normalize whitespace
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    paragraphs = re.split(r"\n\n+", text)

    chunks = []
    current = ""
    chunk_index = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) > CHUNK_SIZE and current:
            chunks.append({
                "text": current.strip(),
                "chunk_index": chunk_index,
                "source": source,
            })
            chunk_index += 1
            # keep overlap from end of current chunk
            current = current[-CHUNK_OVERLAP:] + "\n\n" + para
        else:
            current = (current + "\n\n" + para).strip() if current else para

    if current.strip():
        chunks.append({
            "text": current.strip(),
            "chunk_index": chunk_index,
            "source": source,
        })

    return chunks


def ingest_text(content: str, source: str) -> list[dict]:
    return _chunk_text(content, source)


def ingest_markdown(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        content = f.read()
    # Strip frontmatter
    content = re.sub(r"^---\n.*?\n---\n", "", content, flags=re.DOTALL)
    # Strip HTML tags
    content = re.sub(r"<[^>]+>", "", content)
    return _chunk_text(content, os.path.basename(path))


def ingest_plain(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        content = f.read()
    return _chunk_text(content, os.path.basename(path))


def ingest_pdf(path: str) -> list[dict]:
    try:
        import pypdf
        reader = pypdf.PdfReader(path)
        pages = []
        for page in reader.pages:
            text = page.extract_text() or ""
            if text.strip():
                pages.append(text)
        content = "\n\n".join(pages)
        return _chunk_text(content, os.path.basename(path))
    except ImportError:
        raise ImportError("pypdf is required for PDF ingestion: pip install pypdf")


def ingest_docx(path: str) -> list[dict]:
    try:
        import docx
        doc = docx.Document(path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        content = "\n\n".join(paragraphs)
        return _chunk_text(content, os.path.basename(path))
    except ImportError:
        raise ImportError("python-docx is required for DOCX ingestion: pip install python-docx")


def ingest_file(path: str) -> list[dict]:
    """Detect file type and extract chunks. Returns list of chunk dicts."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"File not found: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return ingest_pdf(path)
    elif ext == ".docx":
        return ingest_docx(path)
    elif ext in (".md", ".mdx"):
        return ingest_markdown(path)
    elif ext in (".txt", ".text", ".csv", ".json", ".yaml", ".yml"):
        return ingest_plain(path)
    else:
        # Try plain text as fallback
        return ingest_plain(path)
