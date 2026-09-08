"""Document metadata + fact extraction, followed by grounding verification against the raw page text."""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any

from rapidfuzz import fuzz

from . import config, db, normalize
from .llm import complete_json
from .pdf import Chunk
from .schemas import DOC_META_SCHEMA, DOC_META_SYSTEM, EXTRACT_SYSTEM, FACT_SCHEMA


async def extract_doc_meta(doc_id: str, filename: str, pages: list[str]) -> dict[str, Any]:
    head = "\n\n".join(f"[[page {i}]]\n{t[:6000]}" for i, t in enumerate(pages[:4], start=1) if t)
    tail = f"[[page {len(pages)}]]\n{pages[-1][:3000]}" if len(pages) > 4 and pages[-1] else ""
    user = f"Filename: {filename}\nTotal pages: {len(pages)}\n\n=== FIRST PAGES ===\n{head}\n\n=== LAST PAGE ===\n{tail}"
    try:
        meta = await complete_json(DOC_META_SYSTEM, user, DOC_META_SCHEMA, tier="fast", purpose="doc_meta", doc_id=doc_id)
    except Exception as e:  # noqa: BLE001 - metadata is helpful, not essential
        meta = {"title": filename, "publisher": "", "doc_type": "other", "publication_date": "",
                "period_covered_start": "", "period_covered_end": "", "fiscal_year_convention": "",
                "default_currency": "", "default_scale": "none", "primary_subject": "", "data_vintage_notes": "",
                "summary": f"(metadata extraction failed: {e})"}
    return meta


def doc_context(meta: dict[str, Any]) -> str:
    keys = ["title", "publisher", "doc_type", "publication_date", "period_covered_start", "period_covered_end",
            "fiscal_year_convention", "default_currency", "default_scale", "primary_subject", "data_vintage_notes"]
    return "\n".join(f"- {k}: {meta.get(k) or '(unknown)'}" for k in keys)


async def extract_chunk(chunk: Chunk, meta: dict[str, Any]) -> list[dict[str, Any]]:
    system = EXTRACT_SYSTEM.format(max_facts=config.MAX_FACTS_PER_CHUNK, doc_context=doc_context(meta))
    user = f"Extract facts from pages {chunk.page_start}-{chunk.page_end}.\n\n{chunk.text}"
    data = await complete_json(system, user, FACT_SCHEMA, tier="fast", purpose="extract", doc_id=chunk.doc_id)
    facts = data.get("facts") or []
    for f in facts:
        f["chunk_id"] = chunk.id
        f["_chunk_pages"] = (chunk.page_start, chunk.page_end)
    return facts


# --- grounding -------------------------------------------------------------------------------------------------------
_ws = re.compile(r"\s+")
_tok = re.compile(r"\d[\d,.]*\d|\d|[A-Za-z]{4,}")


def _norm_map(text: str) -> tuple[str, list[int]]:
    """Lower-case, whitespace-collapsed copy of text plus a map from normalised index -> original index."""
    out, idx = [], []
    prev_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_space:
                continue
            prev_space = True
            out.append(" "); idx.append(i)
        else:
            prev_space = False
            low = ch.lower()
            out.append(low if len(low) == 1 else ch); idx.append(i)
    idx.append(len(text))
    return "".join(out), idx


def _token_window(quote: str, text: str) -> tuple[float, int, int] | None:
    """Slide-deck fallback: every number in the quote must be on the page and >=80 % of all tokens must sit inside
    one window of <= 800 chars. Returns (score, start, end) - the window is what gets highlighted."""
    toks = _tok.findall(quote)
    if len(toks) < 2:
        return None
    low = text.lower()
    positions: list[tuple[int, int]] = []
    missing_numbers = 0
    for t in toks:
        i = low.find(t.lower())
        if i < 0:
            if t[0].isdigit():
                missing_numbers += 1
            continue
        positions.append((i, i + len(t)))
    if missing_numbers or len(positions) < max(2, int(0.8 * len(toks))):
        return None
    positions.sort()
    # choose the densest window: for each start, extend while window <= 800 chars
    best = None
    for a in range(len(positions)):
        b = a
        while b + 1 < len(positions) and positions[b + 1][1] - positions[a][0] <= 800:
            b += 1
        n = b - a + 1
        if best is None or n > best[0]:
            best = (n, positions[a][0], positions[b][1])
    if best is None or best[0] < max(2, int(0.8 * len(toks))):
        return None
    return 100.0 * best[0] / len(toks), best[1], best[2]


def ground(fact: dict[str, Any], pages: list[str]) -> None:
    """Locate the quote in the cited page (or its neighbours). Mutates fact: page, quote_start/end, grounding, flags.

    Tiers: exact substring > case/whitespace-insensitive > fuzzy alignment (>= 85) > token window (numbers all present,
    text not contiguous - typical for multi-column slides) > unverified."""
    flags: list[str] = [f for f in (fact.get("flags") or []) if not f.startswith(("page_corrected", "quote_", "value_not", "numeric_without", "low_conf", "empty_quote"))]
    quote = (fact.get("quote") or "").strip()
    page_no = int(fact.get("page") or 0)
    lo, hi = fact.get("_chunk_pages", (page_no, page_no))
    candidates = [page_no] + [p for p in range(lo, hi + 1) if p != page_no] + [page_no - 1, page_no + 1]
    seen: set[int] = set()
    best: tuple[float, int, int, int, str] | None = None  # score, page, start, end, tier
    if not quote:
        flags.append("empty_quote")
    for p in candidates:
        if p < 1 or p > len(pages) or p in seen:
            continue
        seen.add(p)
        text = pages[p - 1]
        if not text or not quote:
            continue
        idx = text.find(quote)
        if idx >= 0:
            best = (100.0, p, idx, idx + len(quote), "exact")
            break
        nt, imap = _norm_map(text)
        nq, _ = _norm_map(quote)
        idx = nt.find(nq)
        if idx >= 0:
            best = (99.0, p, imap[idx], imap[idx + len(nq)], "exact")
            break
        if len(quote) >= 20:
            al = fuzz.partial_ratio_alignment(quote, text)
            if al and al.score >= 85 and (best is None or (best[4] != "fuzzy" or al.score > best[0])):
                if best is None or best[4] == "window" or al.score > best[0]:
                    best = (float(al.score), p, al.dest_start, al.dest_end, "fuzzy")
                if al.score >= 97:
                    break
        if best is None or best[4] == "window":
            tw = _token_window(quote, text)
            if tw and (best is None or tw[0] > best[0]):
                best = (tw[0], p, tw[1], tw[2], "window")
    if best is None:
        fact["grounding"], fact["grounding_score"] = "unverified", 0.0
        fact["quote_start"] = fact["quote_end"] = None
        flags.append("quote_not_found")
    else:
        score, p, s, e, tier = best
        if p != page_no:
            flags.append(f"page_corrected:{page_no}->{p}")
            fact["page"] = p
        fact["grounding"] = tier
        fact["grounding_score"] = score
        fact["quote_start"], fact["quote_end"] = s, e
        if tier == "window":
            flags.append("quote_not_contiguous")
    if fact.get("kind") == "numeric":
        v = fact.get("value_num")
        if v is None:
            flags.append("numeric_without_value")
        elif quote and not normalize.number_in_text(float(v), quote):
            flags.append("value_not_in_quote")
    if (fact.get("confidence") or 1.0) < 0.7:
        flags.append("low_confidence")
    fact["flags"] = flags


def reground_all() -> dict[str, int]:
    """Re-run grounding for every stored fact (no model calls). Used after improving the matcher."""
    counts: dict[str, int] = {}
    for doc in db.q("SELECT id FROM documents"):
        pages = [r["text"] for r in db.q("SELECT text FROM pages WHERE doc_id=? ORDER BY page_no", (doc["id"],))]
        facts = db.q("SELECT id, page_no, chunk_id, kind, value_num, quote, confidence, flags FROM facts WHERE doc_id=?", (doc["id"],))
        updates = []
        for f in facts:
            m = re.match(r".*:(\d+)-(\d+)", f["chunk_id"] or "")
            fact = {"page": f["page_no"], "kind": f["kind"], "value_num": f["value_num"], "quote": f["quote"],
                    "confidence": f["confidence"], "flags": f.get("flags") or [],
                    "_chunk_pages": (int(m.group(1)), int(m.group(2))) if m else (f["page_no"], f["page_no"])}
            ground(fact, pages)
            counts[fact["grounding"]] = counts.get(fact["grounding"], 0) + 1
            updates.append((fact["page"], fact["quote_start"], fact["quote_end"], fact["grounding"], fact["grounding_score"],
                            json.dumps(fact["flags"]), f["id"]))
        with db.tx() as conn:
            conn.executemany("UPDATE facts SET page_no=?, quote_start=?, quote_end=?, grounding=?, grounding_score=?, flags=? WHERE id=?", updates)
    return counts


def finalize(fact: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """Normalise units/scale/period and produce the DB row (without embedding)."""
    scale = normalize.scale_norm(fact.get("scale"))
    unit = normalize.unit_norm(fact.get("unit"))
    vnum = fact.get("value_num")
    try:
        vnum = float(vnum) if vnum is not None else None
    except (TypeError, ValueError):
        vnum = None
    start, end, ptype = fact.get("period_start") or "", fact.get("period_end") or "", fact.get("period_type") or "none"
    if (not start or not end) and fact.get("period_label"):
        parsed = normalize.parse_period(fact["period_label"], normalize.fy_start_month_from_convention(meta.get("fiscal_year_convention", "")))
        if parsed:
            start, end, ptype = parsed[0] or start, parsed[1] or end, parsed[2] if ptype == "none" else ptype
            fact.setdefault("flags", []).append("period_parsed_by_rule")
    return {
        "id": uuid.uuid4().hex[:12],
        "doc_id": fact["doc_id"],
        "page_no": int(fact.get("page") or 0),
        "chunk_id": fact.get("chunk_id"),
        "kind": fact.get("kind") or ("numeric" if vnum is not None else "statement"),
        "subject": (fact.get("subject") or "").strip() or meta.get("primary_subject") or "unknown",
        "attribute": (fact.get("attribute") or "").strip() or "unknown",
        "attribute_key": normalize.attribute_key(fact.get("attribute") or ""),
        "value_text": (fact.get("value_text") or "").strip(),
        "value_num": vnum,
        "unit": unit,
        "scale": scale,
        "value_norm": normalize.value_norm(vnum, scale),
        "period_label": fact.get("period_label") or "",
        "period_start": start,
        "period_end": end,
        "period_type": ptype,
        "basis": (fact.get("basis") or "").strip(),
        "scope": (fact.get("scope") or "").strip(),
        "quote": (fact.get("quote") or "").strip(),
        "quote_start": fact.get("quote_start"),
        "quote_end": fact.get("quote_end"),
        "grounding": fact.get("grounding") or "unverified",
        "grounding_score": fact.get("grounding_score"),
        "flags": json.dumps(fact.get("flags") or []),
        "confidence": fact.get("confidence"),
        "created_at": time.time(),
    }


def insert_facts(rows: list[dict[str, Any]], embeddings: list[bytes]) -> None:
    cols = ["id", "doc_id", "page_no", "chunk_id", "kind", "subject", "attribute", "attribute_key", "value_text",
            "value_num", "unit", "scale", "value_norm", "period_label", "period_start", "period_end", "period_type",
            "basis", "scope", "quote", "quote_start", "quote_end", "grounding", "grounding_score", "flags",
            "confidence", "embedding", "created_at"]
    with db.tx() as conn:
        conn.executemany(
            f"INSERT INTO facts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [tuple(r.get(c) if c != "embedding" else emb for c in cols) for r, emb in zip(rows, embeddings)],
        )


async def extract_document(doc_id: str, filename: str, pages: list[str], chunks: list[Chunk],
                           meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Run all chunks concurrently (bounded by the LLM semaphore), ground + normalise, return DB rows."""
    done = 0
    total = len(chunks)
    rows: list[dict[str, Any]] = []
    failed_chunks: list[str] = []

    async def run(chunk: Chunk) -> None:
        nonlocal done
        try:
            facts = await extract_chunk(chunk, meta)
        except Exception as e:  # noqa: BLE001 - a failed chunk must not sink the document
            failed_chunks.append(f"{chunk.id}: {e}")
            facts = []
        for f in facts:
            f["doc_id"] = doc_id
            ground(f, pages)
            rows.append(finalize(f, meta))
        done += 1
        db.set_doc_status(doc_id, progress=f"extracting: {done}/{total} chunks, {len(rows)} facts")

    await asyncio.gather(*(run(c) for c in chunks))
    if failed_chunks:
        meta.setdefault("extraction_failures", []).extend(failed_chunks)
    rows.sort(key=lambda r: (r["page_no"], r["quote_start"] if r["quote_start"] is not None else 1 << 30))
    return rows
