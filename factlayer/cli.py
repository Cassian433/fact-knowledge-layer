"""Command line entry points.

  uv run python -m factlayer.cli ingest a.pdf b.pdf ...   # process PDFs in order, print a summary
  uv run python -m factlayer.cli serve [--port 8000]      # start the API + UI
  uv run python -m factlayer.cli stats                    # counts + model spend
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import db, pipeline


async def _ingest(paths: list[str], replace: bool) -> None:
    for p in paths:
        path = Path(p)
        if not path.exists():
            print(f"skip {p}: not found", file=sys.stderr)
            continue
        doc_id, is_new = pipeline.register(path, path.name, replace=replace)
        if not is_new:
            print(f"= {path.name}: already ingested as {doc_id}")
            continue
        print(f"> {path.name}: {doc_id}")
        try:
            res = await pipeline.process(doc_id)
            print(f"  facts={res['facts']} groups={res.get('groups', 0)} relations(llm)={res.get('llm_relations', 0)} relations(det)={res.get('deterministic_relations', 0)}")
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED: {e}", file=sys.stderr)


async def _relink(doc_ids: list[str]) -> None:
    from . import link
    if doc_ids == ["all"]:
        doc_ids = [d["id"] for d in db.q("SELECT id FROM documents WHERE status IN ('done','linking') ORDER BY created_at")]
    for doc_id in doc_ids:
        db.set_doc_status(doc_id, status="linking", progress="relinking")
        stats = await link.link_document(doc_id)
        n = db.one("SELECT COUNT(*) n FROM facts WHERE doc_id=?", (doc_id,))["n"]
        db.set_doc_status(doc_id, status="done", progress=f"done: {n} facts, relinked: {stats}", finished=True)
        print(doc_id, stats)


def _stats() -> None:
    for row in db.q("SELECT status, COUNT(*) n FROM documents GROUP BY status"):
        print(f"documents {row['status']}: {row['n']}")
    for row in db.q("SELECT kind, grounding, COUNT(*) n FROM facts GROUP BY kind, grounding"):
        print(f"facts {row['kind']}/{row['grounding']}: {row['n']}")
    for row in db.q("SELECT type, method, COUNT(*) n FROM relations GROUP BY type, method"):
        print(f"relations {row['type']}/{row['method']}: {row['n']}")
    for row in db.q("SELECT purpose, model, COUNT(*) n, SUM(ok) ok, ROUND(SUM(cost_usd),3) usd, ROUND(AVG(duration_ms)/1000,1) avg_s FROM llm_calls GROUP BY purpose, model"):
        print(f"llm {row['purpose']} {row['model']}: calls={row['n']} ok={row['ok']} usd={row['usd']} avg_s={row['avg_s']}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="factlayer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("paths", nargs="+")
    ing.add_argument("--replace", action="store_true", help="re-process a document that was ingested before")
    srv = sub.add_parser("serve")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    sub.add_parser("stats")
    sub.add_parser("reground", help="re-run quote grounding for all stored facts (no model calls)")
    rl = sub.add_parser("relink", help="(re)run cross-document linking for a document; pairs already judged are skipped")
    rl.add_argument("doc_ids", nargs="+", help="document ids, or 'all'")
    args = ap.parse_args()
    if args.cmd == "ingest":
        asyncio.run(_ingest(args.paths, args.replace))
    elif args.cmd == "serve":
        import uvicorn
        uvicorn.run("factlayer.api:app", host=args.host, port=args.port, reload=False)
    elif args.cmd == "stats":
        _stats()
    elif args.cmd == "reground":
        from . import extract
        print(extract.reground_all())
    elif args.cmd == "relink":
        asyncio.run(_relink(args.doc_ids))


if __name__ == "__main__":
    main()
