"""End-to-end ingestion of one PDF. Documents are processed one at a time (a single worker queue) so that
linking always sees a consistent set of already-ingested facts; chunks within a document run concurrently."""
from __future__ import annotations

import asyncio
import json
import shutil
import time
import traceback
from pathlib import Path
from typing import Any

from . import config, db, extract, link, pdf


def register(path: Path, original_name: str, replace: bool = False) -> tuple[str, bool]:
    """Store the PDF + its page text, return (doc_id, is_new). Same content twice -> same document."""
    db.get()
    sha = pdf.sha256_file(path)
    doc_id = sha[:12]
    existing = db.one("SELECT id, status FROM documents WHERE id=?", (doc_id,))
    if existing and not replace and existing["status"] != "failed":
        return doc_id, False
    if existing:
        delete_document(doc_id)
    dest = config.UPLOAD_DIR / f"{doc_id}.pdf"
    if path.resolve() != dest.resolve():
        shutil.copyfile(path, dest)
    pages = pdf.extract_pages(dest)
    with db.tx() as conn:
        conn.execute("INSERT INTO documents (id, filename, sha256, page_count, status, progress, created_at) VALUES (?,?,?,?,?,?,?)",
                     (doc_id, original_name, sha, len(pages), "queued", "queued", time.time()))
        conn.executemany("INSERT INTO pages (doc_id, page_no, text) VALUES (?,?,?)",
                         [(doc_id, i, t) for i, t in enumerate(pages, start=1)])
    return doc_id, True


def delete_document(doc_id: str) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))  # cascades to pages, facts, relations
    p = config.UPLOAD_DIR / f"{doc_id}.pdf"
    if p.exists():
        p.unlink()


async def process(doc_id: str) -> dict[str, Any]:
    doc = db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not doc:
        raise ValueError(f"unknown document {doc_id}")
    pages = [r["text"] for r in db.q("SELECT text FROM pages WHERE doc_id=? ORDER BY page_no", (doc_id,))]
    t0 = time.time()
    try:
        db.set_doc_status(doc_id, status="extracting", progress="reading document metadata")
        meta = await extract.extract_doc_meta(doc_id, doc["filename"], pages)
        text_chars = sum(len(p) for p in pages)
        if text_chars < 200 * max(1, len(pages)) // 4:
            meta["warnings"] = ["very little extractable text - scanned PDF? OCR is not implemented"]
        with db.tx() as conn:
            conn.execute("UPDATE documents SET meta=? WHERE id=?", (json.dumps(meta), doc_id))
        chunks = pdf.make_chunks(doc_id, pages)
        db.set_doc_status(doc_id, progress=f"extracting: 0/{len(chunks)} chunks")
        rows = await extract.extract_document(doc_id, doc["filename"], pages, chunks, meta)
        db.set_doc_status(doc_id, progress=f"embedding {len(rows)} facts")
        vecs = await asyncio.to_thread(link.embed_texts, [link.fact_text(r) for r in rows])
        extract.insert_facts(rows, [link.to_blob(v) for v in vecs])
        with db.tx() as conn:
            conn.execute("UPDATE documents SET meta=? WHERE id=?", (json.dumps(meta), doc_id))
        db.set_doc_status(doc_id, status="linking", progress="linking against existing facts")
        stats = await link.link_document(doc_id)
        meta["link_stats"] = stats
        meta["processing_seconds"] = round(time.time() - t0, 1)
        with db.tx() as conn:
            conn.execute("UPDATE documents SET meta=? WHERE id=?", (json.dumps(meta), doc_id))
        db.set_doc_status(doc_id, status="done", progress=f"done: {len(rows)} facts, {stats.get('llm_relations', 0) + stats.get('deterministic_relations', 0)} relations", finished=True)
        return {"doc_id": doc_id, "facts": len(rows), **stats}
    except Exception as e:  # noqa: BLE001
        db.set_doc_status(doc_id, status="failed", progress="failed", error=f"{e}\n{traceback.format_exc()[-1500:]}", finished=True)
        raise


class Worker:
    """Single background queue used by the API: uploads return immediately, processing happens in order."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.current: str | None = None

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    def submit(self, doc_id: str) -> None:
        self.start()
        self.queue.put_nowait(doc_id)

    async def _run(self) -> None:
        while True:
            doc_id = await self.queue.get()
            self.current = doc_id
            try:
                await process(doc_id)
            except Exception:  # noqa: BLE001 - already recorded on the document row
                pass
            finally:
                self.current = None
                self.queue.task_done()


worker = Worker()
