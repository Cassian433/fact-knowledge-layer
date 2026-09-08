"""FastAPI surface: upload PDFs, watch them process, browse facts / evidence / relations, and the showcase view."""
from __future__ import annotations

import json
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, pipeline

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.get()
    # Uploads that never started are re-queued; anything mid-flight belongs to whichever process is running it.
    for row in db.q("SELECT id FROM documents WHERE status = 'queued' ORDER BY created_at"):
        pipeline.worker.submit(row["id"])
    pipeline.worker.start()
    yield


app = FastAPI(title="Fact Knowledge Layer", lifespan=lifespan)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


# --- documents --------------------------------------------------------------------------------------------------------
@app.post("/api/documents")
async def upload(files: list[UploadFile], replace: bool = False) -> dict[str, Any]:
    out = []
    for f in files:
        if not (f.filename or "").lower().endswith(".pdf"):
            out.append({"filename": f.filename, "error": "only PDF files are accepted"})
            continue
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", dir=config.UPLOAD_DIR) as tmp:
            shutil.copyfileobj(f.file, tmp)
            tmp_path = Path(tmp.name)
        try:
            doc_id, is_new = pipeline.register(tmp_path, f.filename or tmp_path.name, replace=replace)
        finally:
            tmp_path.unlink(missing_ok=True)
        if is_new:
            pipeline.worker.submit(doc_id)
        out.append({"filename": f.filename, "doc_id": doc_id, "queued": is_new})
    return {"documents": out}


def _doc_counts() -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for r in db.q("SELECT doc_id, COUNT(*) n, SUM(grounding='unverified') unverified FROM facts GROUP BY doc_id"):
        counts[r["doc_id"]] = {"facts": r["n"], "unverified": r["unverified"] or 0}
    for r in db.q("SELECT f.doc_id d, COUNT(*) n FROM relations r JOIN facts f ON f.id IN (r.fact_a, r.fact_b) GROUP BY f.doc_id"):
        counts.setdefault(r["d"], {}).update(relations=r["n"])
    return counts


@app.get("/api/documents")
def list_documents() -> dict[str, Any]:
    docs = db.q("SELECT * FROM documents ORDER BY created_at")
    counts = _doc_counts()
    for d in docs:
        d.update(counts.get(d["id"], {}))
        d.setdefault("facts", 0); d.setdefault("relations", 0); d.setdefault("unverified", 0)
    return {"documents": docs, "processing": pipeline.worker.current}


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str) -> dict[str, Any]:
    d = db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not d:
        raise HTTPException(404, "no such document")
    d.update(_doc_counts().get(doc_id, {}))
    return d


@app.delete("/api/documents/{doc_id}")
def remove_document(doc_id: str) -> dict[str, Any]:
    if not db.one("SELECT id FROM documents WHERE id=?", (doc_id,)):
        raise HTTPException(404, "no such document")
    pipeline.delete_document(doc_id)
    return {"deleted": doc_id}


@app.get("/api/documents/{doc_id}/pages/{page_no}")
def get_page(doc_id: str, page_no: int) -> dict[str, Any]:
    r = db.one("SELECT text FROM pages WHERE doc_id=? AND page_no=?", (doc_id, page_no))
    if not r:
        raise HTTPException(404, "no such page")
    return {"doc_id": doc_id, "page_no": page_no, "text": r["text"]}


@app.get("/api/documents/{doc_id}/pdf")
def get_pdf(doc_id: str) -> FileResponse:
    p = config.UPLOAD_DIR / f"{doc_id}.pdf"
    if not p.exists():
        raise HTTPException(404, "pdf not stored")
    return FileResponse(p, media_type="application/pdf")


# --- facts ------------------------------------------------------------------------------------------------------------
FACT_COLS = ("f.*, d.filename AS doc_filename, json_extract(d.meta,'$.title') AS doc_title, "
             "json_extract(d.meta,'$.publication_date') AS doc_date")


@app.get("/api/facts")
def list_facts(doc_id: str | None = None, q: str | None = None, kind: str | None = None, grounding: str | None = None,
               flagged: bool | None = None, attribute_key: str | None = None,
               limit: int = Query(100, le=1000), offset: int = 0) -> dict[str, Any]:
    where, params = ["1=1"], []
    if doc_id:
        where.append("f.doc_id=?"); params.append(doc_id)
    if kind:
        where.append("f.kind=?"); params.append(kind)
    if grounding:
        where.append("f.grounding=?"); params.append(grounding)
    if attribute_key:
        where.append("f.attribute_key=?"); params.append(attribute_key)
    if flagged is True:
        where.append("f.flags <> '[]'")
    if flagged is False:
        where.append("f.flags = '[]'")
    if q:
        like = f"%{q}%"
        where.append("(f.subject LIKE ? OR f.attribute LIKE ? OR f.value_text LIKE ? OR f.quote LIKE ? OR f.period_label LIKE ?)")
        params += [like] * 5
    sql_where = " AND ".join(where)
    total = db.one(f"SELECT COUNT(*) n FROM facts f WHERE {sql_where}", tuple(params))["n"]
    rows = db.q(f"SELECT {FACT_COLS} FROM facts f JOIN documents d ON d.id=f.doc_id WHERE {sql_where} "
                f"ORDER BY f.doc_id, f.page_no, f.quote_start LIMIT ? OFFSET ?", (*params, limit, offset))
    return {"total": total, "facts": rows}


def _relations_for(fact_ids: list[str]) -> list[dict[str, Any]]:
    if not fact_ids:
        return []
    marks = ",".join("?" * len(fact_ids))
    rels = db.q(f"SELECT * FROM relations WHERE fact_a IN ({marks}) OR fact_b IN ({marks}) ORDER BY confidence DESC",
                (*fact_ids, *fact_ids))
    return _attach_facts(rels)


def _attach_facts(rels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = {r["fact_a"] for r in rels} | {r["fact_b"] for r in rels}
    if not ids:
        return rels
    marks = ",".join("?" * len(ids))
    facts = {f["id"]: f for f in db.q(f"SELECT {FACT_COLS} FROM facts f JOIN documents d ON d.id=f.doc_id WHERE f.id IN ({marks})", tuple(ids))}
    for r in rels:
        r["a"] = facts.get(r["fact_a"])
        r["b"] = facts.get(r["fact_b"])
    return [r for r in rels if r["a"] and r["b"]]


@app.get("/api/facts/{fact_id}")
def get_fact(fact_id: str) -> dict[str, Any]:
    f = db.one(f"SELECT {FACT_COLS} FROM facts f JOIN documents d ON d.id=f.doc_id WHERE f.id=?", (fact_id,))
    if not f:
        raise HTTPException(404, "no such fact")
    page = db.one("SELECT text FROM pages WHERE doc_id=? AND page_no=?", (f["doc_id"], f["page_no"]))
    f["page_text"] = page["text"] if page else ""
    f["relations"] = _relations_for([fact_id])
    return f


# --- relations --------------------------------------------------------------------------------------------------------
@app.get("/api/relations")
def list_relations(type: str | None = None, reconciliation: str | None = None, doc_id: str | None = None,
                   method: str | None = None, q: str | None = None, limit: int = Query(200, le=2000), offset: int = 0) -> dict[str, Any]:
    where, params = ["1=1"], []
    if type:
        where.append("r.type=?"); params.append(type)
    else:
        where.append("r.type <> 'unrelated'")  # false candidate matches are noise unless asked for explicitly
    if reconciliation:
        where.append("r.reconciliation=?"); params.append(reconciliation)
    if method:
        where.append("r.method=?"); params.append(method)
    if doc_id:
        where.append("(fa.doc_id=? OR fb.doc_id=?)"); params += [doc_id, doc_id]
    if q:
        like = f"%{q}%"
        where.append("(fa.subject LIKE ? OR fa.attribute LIKE ? OR fb.attribute LIKE ? OR r.reasoning LIKE ?)")
        params += [like] * 4
    sql_where = " AND ".join(where)
    base = "FROM relations r JOIN facts fa ON fa.id=r.fact_a JOIN facts fb ON fb.id=r.fact_b WHERE " + sql_where
    total = db.one(f"SELECT COUNT(*) n {base}", tuple(params))["n"]
    rels = db.q(f"SELECT r.* {base} ORDER BY r.confidence DESC, r.created_at LIMIT ? OFFSET ?", (*params, limit, offset))
    return {"total": total, "relations": _attach_facts(rels)}


# --- showcase / stats --------------------------------------------------------------------------------------------------
@app.get("/api/showcase")
def showcase() -> dict[str, Any]:
    """Auto-picked candidates for the four required cases. Ranking favours cross-document, high-confidence, well-grounded pairs
    whose surface forms differ (so 'expressed differently' is visible)."""
    def pick(sql_where: str, order: str, n: int) -> list[dict[str, Any]]:
        rels = db.q("SELECT r.* FROM relations r JOIN facts fa ON fa.id=r.fact_a JOIN facts fb ON fb.id=r.fact_b "
                    f"WHERE r.cross_document=1 AND fa.grounding<>'unverified' AND fb.grounding<>'unverified' AND {sql_where} "
                    f"ORDER BY {order} LIMIT ?", (n,))
        return _attach_facts(rels)

    corroborated = pick("r.type='corroborates' AND r.method='llm'",
                        "(fa.value_text<>fb.value_text OR fa.attribute<>fb.attribute) DESC, (fa.kind='statement') DESC, r.confidence DESC", 12)
    contradictions = pick("r.type='contradicts'", "r.confidence DESC", 12)
    reconciled = pick("r.type='reconciled'", "r.confidence DESC", 40)
    # one best example per reconciliation kind first, then the rest
    seen, ordered = set(), []
    for r in reconciled:
        if r["reconciliation"] not in seen:
            seen.add(r["reconciliation"]); ordered.append(r)
    ordered += [r for r in reconciled if r not in ordered]
    failures = {
        "quote_not_found": db.q(f"SELECT {FACT_COLS} FROM facts f JOIN documents d ON d.id=f.doc_id WHERE f.grounding='unverified' ORDER BY f.confidence DESC LIMIT 15"),
        "value_not_in_quote": db.q(f"SELECT {FACT_COLS} FROM facts f JOIN documents d ON d.id=f.doc_id WHERE f.flags LIKE '%value_not_in_quote%' LIMIT 15"),
        "page_corrected": db.q(f"SELECT {FACT_COLS} FROM facts f JOIN documents d ON d.id=f.doc_id WHERE f.flags LIKE '%page_corrected%' LIMIT 15"),
        "low_confidence": db.q(f"SELECT {FACT_COLS} FROM facts f JOIN documents d ON d.id=f.doc_id WHERE f.flags LIKE '%low_confidence%' ORDER BY f.confidence LIMIT 15"),
        "llm_errors": db.q("SELECT * FROM llm_calls WHERE ok=0 ORDER BY created_at DESC LIMIT 15"),
        "unrelated_but_matched": _attach_facts(db.q("SELECT * FROM relations WHERE type='unrelated' ORDER BY confidence DESC LIMIT 10")),
    }
    return {"corroborated": corroborated, "contradictions": contradictions, "reconciled": ordered[:15], "failures": failures}


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    return {
        "documents": db.q("SELECT status, COUNT(*) n FROM documents GROUP BY status"),
        "facts": db.one("SELECT COUNT(*) n, SUM(kind='numeric') numeric, SUM(kind='statement') statement, "
                        "SUM(grounding='exact') exact, SUM(grounding='fuzzy') fuzzy, SUM(grounding='unverified') unverified, "
                        "SUM(flags<>'[]') flagged FROM facts"),
        "relations": db.q("SELECT type, method, COUNT(*) n FROM relations GROUP BY type, method"),
        "reconciliations": db.q("SELECT reconciliation, COUNT(*) n FROM relations WHERE type='reconciled' GROUP BY reconciliation ORDER BY n DESC"),
        "llm": db.q("SELECT purpose, model, COUNT(*) calls, SUM(ok) ok, ROUND(SUM(cost_usd),3) usd, ROUND(AVG(duration_ms)/1000.0,1) avg_s, "
                    "SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens FROM llm_calls GROUP BY purpose, model"),
        "config": {"backend": config.LLM_BACKEND, "model_fast": config.MODEL_FAST, "model_smart": config.MODEL_SMART,
                   "chunk_chars": config.CHUNK_CHARS, "sim_threshold": config.SIM_THRESHOLD},
    }


app.mount("/static", StaticFiles(directory=STATIC), name="static")
