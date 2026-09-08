"""PDF -> page texts -> page-anchored chunks. PyMuPDF only; no OCR (scanned PDFs are flagged, not guessed)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from . import config


@dataclass
class Chunk:
    id: str
    doc_id: str
    page_start: int
    page_end: int
    text: str  # pages joined, each prefixed with a "[[page N]]" marker the model must cite


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


_ws = re.compile(r"[ \t ]+")
_nl = re.compile(r"\n{3,}")


def clean(text: str) -> str:
    text = text.replace("\r", "\n").replace("\x0c", "\n")
    text = _ws.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _nl.sub("\n\n", text).strip()


def extract_pages(path: Path) -> list[str]:
    """1-based physical page order. `sort=True` gives reading order, which matters for two-column reports."""
    out: list[str] = []
    with pymupdf.open(path) as doc:
        for page in doc:
            out.append(clean(page.get_text("text", sort=True)))
    return out


def make_chunks(doc_id: str, pages: list[str], max_chars: int = config.CHUNK_CHARS) -> list[Chunk]:
    """Pack consecutive pages up to max_chars. A single oversized page is split on paragraph boundaries."""
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_len = 0
    start = 1
    last = 0  # last non-empty page packed into buf

    def flush(end: int) -> None:
        nonlocal buf, buf_len, start
        if buf:
            end = min(end, last) if last >= start else end
            chunks.append(Chunk(f"{doc_id}:{start}-{end}", doc_id, start, end, "\n\n".join(buf)))
        buf, buf_len = [], 0
        start = end + 1

    for i, text in enumerate(pages, start=1):
        if not text:
            if not buf:
                start = i + 1
            continue
        if len(text) > max_chars:
            flush(i - 1)
            for j, part in enumerate(_split(text, max_chars)):
                chunks.append(Chunk(f"{doc_id}:{i}-{i}#{j}", doc_id, i, i, f"[[page {i}]]\n{part}"))
            start = i + 1
            continue
        if buf and buf_len + len(text) > max_chars:
            flush(i - 1)
            start = i
        buf.append(f"[[page {i}]]\n{text}")
        buf_len += len(text)
        last = i
    flush(len(pages))
    return chunks


def _split(text: str, max_chars: int) -> list[str]:
    parts, cur = [], ""
    for para in text.split("\n\n"):
        if cur and len(cur) + len(para) + 2 > max_chars:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
        while len(cur) > max_chars:  # pathological single paragraph (e.g. a giant table)
            parts.append(cur[:max_chars])
            cur = cur[max_chars:]
    if cur:
        parts.append(cur)
    return parts
