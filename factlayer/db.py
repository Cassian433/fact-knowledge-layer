"""SQLite storage. Plain sqlite3 + JSON columns: small, inspectable, zero setup.

Tables
  documents   one row per ingested PDF (+ LLM-derived metadata used as reasoning context)
  pages       raw page text, the ground truth every quote is checked against
  facts       one row per extracted fact, always pointing at (doc, page) with a verbatim quote
  relations   one row per (fact, fact) judgement: corroborates / contradicts / reconciled / unrelated
  llm_calls   every model call with cost + latency, for honesty about what the system spent
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  filename TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  page_count INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',      -- queued | extracting | linking | done | failed
  progress TEXT,                              -- free-text progress line for the UI
  error TEXT,
  meta JSON,                                  -- publisher, doc_type, dates, fiscal convention ...
  created_at REAL NOT NULL,
  finished_at REAL
);
CREATE TABLE IF NOT EXISTS pages (
  doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  page_no INTEGER NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY (doc_id, page_no)
);
CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY,
  doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  page_no INTEGER NOT NULL,
  chunk_id TEXT,
  kind TEXT NOT NULL,                         -- numeric | statement
  subject TEXT NOT NULL,
  attribute TEXT NOT NULL,
  attribute_key TEXT NOT NULL,                -- normalised slug used for blocking
  value_text TEXT NOT NULL,                   -- value as written / the statement itself
  value_num REAL,                             -- parsed number (as stated, before scale)
  unit TEXT,                                  -- INR, USD, %, persons, ...
  scale TEXT,                                 -- crore | lakh | million | billion | thousand | none
  value_norm REAL,                            -- value_num * scale multiplier (comparable across docs)
  period_label TEXT,
  period_start TEXT,                          -- ISO date
  period_end TEXT,
  period_type TEXT,                           -- fiscal_year | quarter | calendar_year | point_in_time | range | none
  basis TEXT,                                 -- consolidated | standalone | provisional | estimate | projection | actual | ...
  scope TEXT,                                 -- any other qualifier the doc attaches
  quote TEXT NOT NULL,
  quote_start INTEGER,
  quote_end INTEGER,
  grounding TEXT NOT NULL,                    -- exact | fuzzy | unverified
  grounding_score REAL,
  flags JSON,                                 -- list[str] of problems found during verification
  confidence REAL,
  embedding BLOB,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_doc ON facts(doc_id);
CREATE INDEX IF NOT EXISTS facts_key ON facts(attribute_key);
CREATE TABLE IF NOT EXISTS relations (
  id TEXT PRIMARY KEY,
  fact_a TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
  fact_b TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
  type TEXT NOT NULL,                         -- corroborates | contradicts | reconciled | unrelated
  reconciliation TEXT,                        -- period | scope | unit | rounding | vintage | estimate_vs_actual | definition | other
  confidence REAL,
  reasoning TEXT,
  method TEXT NOT NULL,                       -- deterministic | llm
  cross_document INTEGER NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE (fact_a, fact_b)
);
CREATE INDEX IF NOT EXISTS relations_type ON relations(type);
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id TEXT,
  purpose TEXT NOT NULL,                      -- doc_meta | extract | adjudicate
  model TEXT,
  ok INTEGER NOT NULL,
  duration_ms INTEGER,
  cost_usd REAL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  error TEXT,
  created_at REAL NOT NULL
);
"""

_lock = threading.RLock()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_conn: sqlite3.Connection | None = None


def get() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect()
        _conn.executescript(SCHEMA)
    return _conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Serialised write transaction (sqlite is single-writer; the pipeline is multi-threaded)."""
    with _lock:
        conn = get()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def q(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    with _lock:
        rows = get().execute(sql, params).fetchall()
    return [_row(r) for r in rows]


def one(sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    for k in ("meta", "flags"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    d.pop("embedding", None)
    return d


def log_llm_call(doc_id: str | None, purpose: str, model: str | None, ok: bool, duration_ms: int | None,
                 cost_usd: float | None, input_tokens: int | None, output_tokens: int | None,
                 error: str | None = None) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO llm_calls (doc_id,purpose,model,ok,duration_ms,cost_usd,input_tokens,output_tokens,error,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (doc_id, purpose, model, int(ok), duration_ms, cost_usd, input_tokens, output_tokens, error, time.time()),
        )


def set_doc_status(doc_id: str, status: str | None = None, progress: str | None = None, error: str | None = None,
                   finished: bool = False) -> None:
    sets, params = [], []
    if status is not None:
        sets.append("status=?"); params.append(status)
    if progress is not None:
        sets.append("progress=?"); params.append(progress)
    if error is not None:
        sets.append("error=?"); params.append(error)
    if finished:
        sets.append("finished_at=?"); params.append(time.time())
    if not sets:
        return
    params.append(doc_id)
    with tx() as conn:
        conn.execute(f"UPDATE documents SET {', '.join(sets)} WHERE id=?", params)
