"""Export the knowledge layer to a static site (GitHub Pages): same UI, answered from JSON files, read-only.

    uv run python docs/export_static.py [out_dir]     # default: site/

Writes index.html (the real UI + static-api.js), data/*.json, data/pages/<doc>.json and pdf/<doc>.pdf.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from factlayer import api, config, db  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "site")
STATIC = Path(__file__).resolve().parent.parent / "factlayer" / "static"

STATIC_API_JS = r"""
// Static backend for the Fact Knowledge Layer UI: the same /api/... paths, answered from exported JSON.
(() => {
  const cache = {};
  const load = async (name) => {
    if (!cache[name]) cache[name] = fetch(`data/${name}.json`).then(r => { if (!r.ok) throw new Error(`missing data/${name}.json`); return r.json(); });
    return cache[name];
  };
  const like = (s, q) => (s || '').toString().toLowerCase().includes(q.toLowerCase());
  let factIndex = null;
  const facts = async () => { const f = await load('facts'); if (!factIndex) { factIndex = new Map(f.map(x => [x.id, x])); } return f; };
  const attach = async (rels) => { await facts(); return rels.map(r => ({ ...r, a: factIndex.get(r.fact_a), b: factIndex.get(r.fact_b) })).filter(r => r.a && r.b); };

  window.STATIC_PDF = id => `pdf/${id}.pdf`;
  window.STATIC_API = async (path, opts) => {
    if (opts && opts.method && opts.method !== 'GET') throw new Error('read-only static demo');
    const url = new URL(path, location.href); const p = url.pathname.replace(/^.*\/api\//, '/api/'); const q = url.searchParams;
    let m;
    if (p === '/api/stats') return load('stats');
    if (p === '/api/documents') return { documents: await load('documents'), processing: null };
    if (p === '/api/showcase') return load('showcase');
    if ((m = p.match(/^\/api\/documents\/([^/]+)\/pages\/(\d+)$/))) { const pages = await load(`pages/${m[1]}`); return { doc_id: m[1], page_no: +m[2], text: pages[m[2]] || '' }; }
    if ((m = p.match(/^\/api\/documents\/([^/]+)$/))) { const d = (await load('documents')).find(x => x.id === m[1]); if (!d) throw new Error('no such document'); return d; }
    if ((m = p.match(/^\/api\/facts\/([^/]+)\/timeline$/))) {
      const all = await facts(); const f = factIndex.get(m[1]); if (!f) throw new Error('no such fact');
      const rels = (await load('relations')).filter(r => r.type !== 'unrelated' && (r.fact_a === f.id || r.fact_b === f.id));
      const relBy = {}; rels.forEach(r => { relBy[r.fact_a === f.id ? r.fact_b : r.fact_a] = { type: r.type, reconciliation: r.reconciliation, confidence: r.confidence, id: r.id }; });
      const rows = new Map(all.filter(x => x.attribute_key === f.attribute_key && x.kind === f.kind).slice(0, 200).map(x => [x.id, x]));
      Object.keys(relBy).forEach(id => { if (!rows.has(id) && factIndex.get(id)) rows.set(id, factIndex.get(id)); });
      const out = [...rows.values()].map(x => ({ ...x, relation: relBy[x.id] || null, is_anchor: x.id === f.id }));
      out.sort((a, b) => ((a.period_start || '9999') + (a.period_end || '') + (a.doc_date || '')).localeCompare((b.period_start || '9999') + (b.period_end || '') + (b.doc_date || '')));
      return { anchor: f.id, attribute_key: f.attribute_key, facts: out };
    }
    if ((m = p.match(/^\/api\/facts\/([^/]+)$/))) {
      await facts(); const f = factIndex.get(m[1]); if (!f) throw new Error('no such fact');
      const pages = await load(`pages/${f.doc_id}`);
      const rels = (await load('relations')).filter(r => r.fact_a === f.id || r.fact_b === f.id).sort((a, b) => b.confidence - a.confidence);
      return { ...f, page_text: pages[f.page_no] || '', relations: await attach(rels) };
    }
    if (p === '/api/facts') {
      let rows = await facts();
      const doc = q.get('doc_id'), kind = q.get('kind'), g = q.get('grounding'), fl = q.get('flagged'), ak = q.get('attribute_key'), s = q.get('q');
      if (doc) rows = rows.filter(f => f.doc_id === doc);
      if (kind) rows = rows.filter(f => f.kind === kind);
      if (g) rows = rows.filter(f => f.grounding === g);
      if (ak) rows = rows.filter(f => f.attribute_key === ak);
      if (fl === 'true') rows = rows.filter(f => (f.flags || []).length); if (fl === 'false') rows = rows.filter(f => !(f.flags || []).length);
      if (s) rows = rows.filter(f => like(f.subject, s) || like(f.attribute, s) || like(f.value_text, s) || like(f.quote, s) || like(f.period_label, s));
      const limit = +(q.get('limit') || 100), offset = +(q.get('offset') || 0);
      return { total: rows.length, facts: rows.slice(offset, offset + limit) };
    }
    if (p === '/api/relations') {
      let rows = await load('relations'); await facts();
      const t = q.get('type'), rk = q.get('reconciliation'), doc = q.get('doc_id'), me = q.get('method'), s = q.get('q');
      rows = t ? rows.filter(r => r.type === t) : rows.filter(r => r.type !== 'unrelated');
      if (rk) rows = rows.filter(r => r.reconciliation === rk);
      if (me) rows = rows.filter(r => r.method === me);
      if (doc) rows = rows.filter(r => { const a = factIndex.get(r.fact_a), b = factIndex.get(r.fact_b); return (a && a.doc_id === doc) || (b && b.doc_id === doc); });
      if (s) rows = rows.filter(r => { const a = factIndex.get(r.fact_a) || {}, b = factIndex.get(r.fact_b) || {}; return like(a.subject, s) || like(a.attribute, s) || like(b.attribute, s) || like(r.reasoning, s); });
      rows = [...rows].sort((a, b) => b.confidence - a.confidence || a.created_at - b.created_at);
      const limit = +(q.get('limit') || 200), offset = +(q.get('offset') || 0);
      return { total: rows.length, relations: await attach(rows.slice(offset, offset + limit)) };
    }
    throw new Error('not available in the static demo: ' + p);
  };
})();
"""


def dump(name: str, obj) -> None:
    path = OUT / "data" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def main() -> None:
    db.get()
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "data" / "pages").mkdir(parents=True)
    (OUT / "pdf").mkdir(parents=True)

    stats = api.stats()
    stats["config"]["read_only"] = True
    dump("stats", stats)
    dump("documents", api.list_documents()["documents"])
    dump("showcase", api.showcase())
    dump("facts", db.q(f"SELECT {api.FACT_COLS} FROM facts f JOIN documents d ON d.id=f.doc_id ORDER BY f.doc_id, f.page_no, f.quote_start"))
    dump("relations", db.q("SELECT * FROM relations"))
    for d in db.q("SELECT id FROM documents"):
        pages = {r["page_no"]: r["text"] for r in db.q("SELECT page_no, text FROM pages WHERE doc_id=?", (d["id"],))}
        dump(f"pages/{d['id']}", pages)
        pdf = config.UPLOAD_DIR / f"{d['id']}.pdf"
        if pdf.exists():
            shutil.copyfile(pdf, OUT / "pdf" / f"{d['id']}.pdf")

    html = (STATIC / "index.html").read_text()
    html = html.replace("<script>\nconst $ =", '<script src="static-api.js"></script>\n<script>\nconst $ =', 1)
    assert 'static-api.js' in html
    (OUT / "index.html").write_text(html)
    (OUT / "static-api.js").write_text(STATIC_API_JS)
    (OUT / ".nojekyll").write_text("")
    total = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"wrote {OUT}/ ({total / 1e6:.1f} MB): {len(list(OUT.rglob('*.json')))} json, {len(list((OUT / 'pdf').glob('*.pdf')))} pdfs")


if __name__ == "__main__":
    main()
