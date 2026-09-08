"""Cross-document linking: find facts that talk about the same thing, then decide how they relate.

Two stages, on purpose:
  1. Candidate generation is cheap and recall-oriented: same normalised attribute key OR embedding similarity of
     "subject | attribute" above a threshold (local ONNX model, no API).
  2. Judgement is precision-oriented: numeric pairs with identical unit + period + basis are settled by arithmetic
     (deterministic, explainable); everything else goes to the model with both quotes and both documents' metadata,
     which is what lets it say "different fiscal year" or "provisional vs revised" instead of "contradiction".
"""
from __future__ import annotations

import time
import uuid
from collections import defaultdict
from typing import Any

import numpy as np

from . import config, db
from .llm import complete_json
from .schemas import ADJUDICATE_SYSTEM, RELATION_SCHEMA

_embedder = None


def embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding
        _embedder = TextEmbedding(model_name=config.EMBED_MODEL, cache_dir=str(config.DATA_DIR / "models"))
    return _embedder


def fact_text(row: dict[str, Any]) -> str:
    return f"{row['subject']} | {row['attribute']}"


def embed_texts(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)
    vecs = np.asarray(list(embedder().embed(texts, batch_size=64)), dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.maximum(norms, 1e-9)


def to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def _load(where: str, params: tuple) -> tuple[list[dict[str, Any]], np.ndarray]:
    with db._lock:  # noqa: SLF001 - need the raw blob column
        rows = db.get().execute(f"SELECT * FROM facts WHERE {where}", params).fetchall()
    facts, vecs = [], []
    for r in rows:
        d = dict(r)
        blob = d.pop("embedding", None)
        facts.append(d)
        vecs.append(np.frombuffer(blob, dtype=np.float32) if blob else np.zeros(384, dtype=np.float32))
    return facts, (np.vstack(vecs) if vecs else np.zeros((0, 384), dtype=np.float32))


# --- deterministic judgement ------------------------------------------------------------------------------------------
def _same_period(a: dict, b: dict) -> bool:
    return bool(a["period_start"] and a["period_end"]) and a["period_start"] == b["period_start"] and a["period_end"] == b["period_end"]


def _same_basis(a: dict, b: dict) -> bool:
    x, y = (a.get("basis") or "").lower(), (b.get("basis") or "").lower()
    return x == y or not x or not y


def deterministic(a: dict, b: dict) -> dict[str, Any] | None:
    """Only speaks when everything that could explain a difference is identical; then arithmetic decides."""
    if a["kind"] != "numeric" or b["kind"] != "numeric":
        return None
    if a["value_norm"] is None or b["value_norm"] is None or (a["unit"] or "") != (b["unit"] or ""):
        return None
    if a["attribute_key"] != b["attribute_key"] or not _same_period(a, b) or not _same_basis(a, b):
        return None
    if (a.get("scope") or "").lower() != (b.get("scope") or "").lower():
        return None
    x, y = a["value_norm"], b["value_norm"]
    denom = max(abs(x), abs(y), 1e-9)
    delta = abs(x - y) / denom
    if delta <= config.NUMERIC_TOLERANCE:
        why = (f"Same attribute key '{a['attribute_key']}', same period {a['period_start']}..{a['period_end']}, same unit "
               f"{a['unit'] or '-'}; values {a['value_text']} ({a['scale']}) and {b['value_text']} ({b['scale']}) agree within "
               f"{delta:.2%} after scale normalisation.")
        return {"type": "corroborates", "reconciliation": "", "confidence": 0.9, "reasoning": why, "method": "deterministic"}
    return None


# --- LLM judgement ---------------------------------------------------------------------------------------------------
def _doc_label(meta: dict[str, Any], filename: str) -> str:
    t = (meta or {}).get("title") or filename
    bits = [t, (meta or {}).get("doc_type") or "", f"published {(meta or {}).get('publication_date') or '?'}"]
    return " · ".join(b for b in bits if b)


def _fmt_fact(f: dict[str, Any], tag: str) -> str:
    val = f["value_text"]
    if f["kind"] == "numeric":
        val = f"{f['value_text']} {f['unit'] or ''} {'' if f['scale'] == 'none' else f['scale']}".strip()
    period = f["period_label"] or "-"
    if f["period_start"]:
        period += f" ({f['period_start']}..{f['period_end']})"
    return (f"- id={f['id']} | doc={tag} | page {f['page_no']}\n"
            f"  subject: {f['subject']} | attribute: {f['attribute']} | value: {val} | period: {period}"
            f" | basis: {f['basis'] or '-'} | scope: {f['scope'] or '-'}\n"
            f"  quote: \"{f['quote'][:300]}\"")


async def adjudicate(groups: list[list[dict[str, Any]]], docs: dict[str, dict[str, Any]], doc_id: str) -> list[dict[str, Any]]:
    tags = {d: f"D{i + 1}" for i, d in enumerate(sorted({f['doc_id'] for g in groups for f in g}))}
    legend = "\n".join(
        f"{tags[d]}: {_doc_label(docs[d].get('meta') or {}, docs[d]['filename'])}"
        + (f"\n    period covered: {(docs[d].get('meta') or {}).get('period_covered_start') or '?'}..{(docs[d].get('meta') or {}).get('period_covered_end') or '?'}" if docs[d].get('meta') else "")
        + (f"\n    year convention: {(docs[d].get('meta') or {}).get('fiscal_year_convention')}" if (docs[d].get('meta') or {}).get('fiscal_year_convention') else "")
        + (f"\n    vintage notes: {(docs[d].get('meta') or {}).get('data_vintage_notes')}" if (docs[d].get('meta') or {}).get('data_vintage_notes') else "")
        for d in tags)
    body = []
    for gi, g in enumerate(groups, start=1):
        body.append(f"### Group {gi}\n" + "\n".join(_fmt_fact(f, tags[f['doc_id']]) for f in g))
    user = f"Documents:\n{legend}\n\nFact groups (compare facts within a group, across documents only):\n\n" + "\n\n".join(body)
    data = await complete_json(ADJUDICATE_SYSTEM, user, RELATION_SCHEMA, tier="smart", purpose="adjudicate", doc_id=doc_id)
    by_id = {f["id"]: f for g in groups for f in g}
    out = []
    for r in data.get("relations") or []:
        a, b = by_id.get(r.get("a")), by_id.get(r.get("b"))
        if not a or not b or a["id"] == b["id"] or a["doc_id"] == b["doc_id"]:
            continue
        if r.get("type") not in ("corroborates", "contradicts", "reconciled", "unrelated"):
            continue
        out.append({"a": a, "b": b, "type": r["type"], "reconciliation": r.get("reconciliation") or "",
                    "confidence": float(r.get("confidence") or 0), "reasoning": r.get("reasoning") or "", "method": "llm"})
    return out


def _store(rels: list[dict[str, Any]], replace: bool) -> int:
    n = 0
    with db.tx() as conn:
        for r in rels:
            a, b = sorted((r["a"]["id"], r["b"]["id"]))
            verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
            cur = conn.execute(
                f"{verb} INTO relations (id,fact_a,fact_b,type,reconciliation,confidence,reasoning,method,cross_document,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex[:12], a, b, r["type"], r["reconciliation"] or None, r["confidence"], r["reasoning"],
                 r["method"], int(r["a"]["doc_id"] != r["b"]["doc_id"]), time.time()))
            n += cur.rowcount
    return n


# --- orchestration ----------------------------------------------------------------------------------------------------
async def link_document(doc_id: str) -> dict[str, int]:
    new, new_vecs = _load("doc_id=?", (doc_id,))
    old, old_vecs = _load("doc_id<>?", (doc_id,))
    stats = {"new_facts": len(new), "candidates": 0, "groups": 0, "llm_relations": 0, "deterministic_relations": 0}
    if not new or not old:
        return stats
    docs = {d["id"]: d for d in db.q("SELECT id, filename, meta FROM documents")}
    old_by_key: dict[str, list[int]] = defaultdict(list)
    for j, f in enumerate(old):
        old_by_key[f["attribute_key"]].append(j)
    sims = new_vecs @ old_vecs.T  # (n_new, n_old)

    groups: list[list[dict[str, Any]]] = []
    det_rels: list[dict[str, Any]] = []
    # pairs already judged in an earlier (possibly interrupted) run are not sent to the model again
    pairs_seen: set[tuple[str, str]] = {
        tuple(sorted((r["fact_a"], r["fact_b"])))
        for r in db.q("SELECT fact_a, fact_b FROM relations r JOIN facts f ON f.id = r.fact_a OR f.id = r.fact_b WHERE f.doc_id=?", (doc_id,))
    }
    stats["already_judged"] = len(pairs_seen)
    for i, f in enumerate(new):
        order = np.argsort(-sims[i])
        cand: dict[int, float] = {}
        for j in order[: config.MAX_NEIGHBOURS * 3]:
            if sims[i, j] < config.SIM_THRESHOLD:
                break
            if old[j]["kind"] == f["kind"]:
                cand[int(j)] = float(sims[i, j])
            if len(cand) >= config.MAX_NEIGHBOURS:
                break
        for j in old_by_key.get(f["attribute_key"], []):
            if old[j]["kind"] == f["kind"]:
                cand.setdefault(j, float(sims[i, j]))
        if not cand:
            continue
        ranked = sorted(cand.items(), key=lambda kv: -kv[1])[: config.MAX_CLUSTER - 1]
        members = [old[j] for j, _ in ranked]
        stats["candidates"] += len(members)
        undecided = []
        for m in members:
            key = tuple(sorted((f["id"], m["id"])))
            if key in pairs_seen:
                continue
            pairs_seen.add(key)
            d = deterministic(f, m)
            if d:
                det_rels.append({"a": f, "b": m, **d})
            undecided.append(m)  # the model still sees it: it may add reasoning or overrule
        if undecided:
            groups.append([f] + undecided)
    stats["groups"] = len(groups)
    db.set_doc_status(doc_id, progress=f"linking: {stats['candidates']} candidate pairs in {len(groups)} groups")

    # Batch several groups per call to amortise the per-call overhead; keep each call to ~40 facts.
    batches: list[list[list[dict[str, Any]]]] = []
    cur, size = [], 0
    for g in groups:
        if cur and size + len(g) > 40:
            batches.append(cur)
            cur, size = [], 0
        cur.append(g)
        size += len(g)
    if cur:
        batches.append(cur)

    import asyncio
    done = 0

    async def run(batch):
        nonlocal done
        try:
            rels = await adjudicate(batch, docs, doc_id)
        except Exception as e:  # noqa: BLE001
            rels = []
            db.set_doc_status(doc_id, progress=f"linking: batch failed ({str(e)[:80]})")
        stats["llm_relations"] += _store(rels, replace=True)
        done += 1
        db.set_doc_status(doc_id, progress=f"linking: {done}/{len(batches)} batches, {stats['llm_relations']} relations")

    await asyncio.gather(*(run(b) for b in batches))
    stats["deterministic_relations"] = _store(det_rels, replace=False)
    return stats
