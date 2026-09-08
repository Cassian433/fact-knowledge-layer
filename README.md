# Fact Knowledge Layer

Upload PDFs → get **facts**, each pinned to a **verbatim quote on a page**, and **cross-document relations** that say
whether two facts *corroborate*, *contradict*, or only *appear* to contradict until you look at the period, scope,
unit or data vintage — with the reasoning written out so a reader can check it against the two quotes.

Built for the Superjoin VIT 2026 engineering-intern assignment. Runs locally: Python, SQLite, a small vanilla-JS UI,
and an LLM behind a one-function interface.

> **Video demo:** _link goes here_ (≤ 3 min: a PDF being processed, then the four required cases)

---

## Setup and run

Requirements: Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and an LLM backend (see below).

```bash
git clone <this repo> && cd factlayer
uv sync                                   # installs everything into .venv (PyMuPDF, FastAPI, fastembed, anthropic ...)

# choose ONE backend (see "LLM backend" below)
export FACTLAYER_LLM_BACKEND=anthropic    # official Anthropic SDK - needs ANTHROPIC_API_KEY (or `ant auth login`)
#   or
export FACTLAYER_LLM_BACKEND=claude_cli   # shells out to a logged-in Claude Code CLI (`claude -p`) - no key in the repo

uv run python -m factlayer.cli serve --port 8801      # UI + API at http://127.0.0.1:8801
```

Then drop PDFs onto the **Documents** tab. Each document is processed in the background (progress is live) and
linked against everything already in the layer. Or use the CLI:

```bash
uv run python -m factlayer.cli ingest path/to/a.pdf path/to/b.pdf   # processes in order, prints counts
uv run python -m factlayer.cli stats                                 # facts / relations / model calls + spend
uv run python -m factlayer.cli reground                              # re-run quote verification (no model calls)
```

The first run downloads a ~130 MB sentence-embedding model (`BAAI/bge-small-en-v1.5`, runs on CPU) into `data/models/`.
Everything the system knows lives in one SQLite file, `data/factlayer.db`; delete it to start over.

### LLM backend

| `FACTLAYER_LLM_BACKEND` | what it does | when to use |
|---|---|---|
| `anthropic` | `anthropic` SDK, structured outputs (`output_config.format = json_schema`), streaming, prompt caching on the system prompt | you have an API key — the normal way to run this |
| `claude_cli` (default) | one `claude -p --json-schema … --tools ""` subprocess per call, using whatever login the Claude Code CLI has | what I used for the demo; nothing to configure if Claude Code is installed and logged in |

Both backends implement the same `complete_json(system, user, schema) -> dict` contract, so nothing else in the code
knows which one is running. Two model tiers are used: `FACTLAYER_MODEL_FAST` for bulk extraction (default Sonnet) and
`FACTLAYER_MODEL_SMART` for cross-document judgement (default Opus). Every call is logged to the `llm_calls` table with
latency and reported cost — visible on the **Stats** tab.

Other knobs (all optional, via env or `.env`): `FACTLAYER_LLM_CONCURRENCY` (default 6), `FACTLAYER_CHUNK_CHARS`
(18 000), `FACTLAYER_MAX_FACTS_PER_CHUNK` (40), `FACTLAYER_SIM_THRESHOLD` (0.82), `FACTLAYER_NUMERIC_TOLERANCE` (0.01).

### API

| method | path | purpose |
|---|---|---|
| `POST` | `/api/documents` (multipart `files`) | upload one or more PDFs; returns doc ids, processing starts immediately |
| `GET` | `/api/documents`, `/api/documents/{id}` | status, progress, LLM-derived document metadata, counts |
| `GET` | `/api/documents/{id}/pages/{n}`, `/api/documents/{id}/pdf` | raw page text (what quotes are verified against) / the PDF |
| `DELETE` | `/api/documents/{id}` | remove a document with its facts and relations |
| `GET` | `/api/facts?q=&doc_id=&kind=&grounding=&flagged=` | search facts |
| `GET` | `/api/facts/{id}` | one fact + page text + quote offsets + its relations |
| `GET` | `/api/relations?type=&reconciliation=&doc_id=&method=&q=` | corroborates / contradicts / reconciled / unrelated |
| `GET` | `/api/showcase` | auto-picked examples of the four required cases + every failure the system caught |
| `GET` | `/api/stats` | counts, grounding quality, model calls and spend |

---

## Approach

### The shape of a fact

The documents decide what a fact is; the schema only fixes *how* one is written down:

```
subject      "Delhivery Limited" / "India" / a person / a segment
attribute    "Revenue from operations", "Headline CPI inflation", "Registered office address", "Board position"
value        value_text as written  +  value_num, unit, scale  →  value_norm (scale applied, e.g. crore → ×1e7)
period       label as written ("FY24", "Q4 FY24", "2024-25", "as at 31 March 2024") + ISO start/end + type
basis/scope  consolidated | standalone | provisional | advance estimate | projection | segment | constant prices …
evidence     verbatim quote, page number, character offsets on that page, grounding tier, verification flags
```

`basis`/`scope`/`period` are first-class because they are exactly the things that turn an apparent contradiction into
a reconcilable difference. A "revenue" number without its period and basis is not comparable to anything.

### Pipeline (per document)

```
PDF ──PyMuPDF──▶ page texts ──▶ page-anchored chunks (~18k chars, "[[page N]]" markers)
      │
      ├─▶ 1. document metadata (1 LLM call on first pages + last page)
      │       publisher, type, publication date, period covered, FISCAL-YEAR CONVENTION, currency/scale, vintage notes
      │
      ├─▶ 2. fact extraction (1 LLM call per chunk, concurrent, structured output against FACT_SCHEMA)
      │       the document metadata is in the prompt, so "FY24" resolves to 2023-04-01..2024-03-31 for this document
      │
      ├─▶ 3. grounding (no LLM) — every quote is searched for on the cited page (and neighbours):
      │       exact → whitespace/case-insensitive → fuzzy alignment (≥85) → token window → unverified
      │       + checks: the number in the fact must appear in the quote; wrong page numbers are corrected and flagged
      │
      ├─▶ 4. normalisation (no LLM) — units (Rs/₹/INR → INR), scales, attribute slug, fallback period parser
      │
      ├─▶ 5. embeddings (local) of "subject | attribute" for candidate matching
      │
      └─▶ 6. linking against every fact already in the layer (incremental — earlier documents are not reprocessed)
              a. candidates: same attribute slug OR cosine ≥ 0.82, same kind, other documents only, ≤ 8 per fact
              b. deterministic judge: identical unit + period + basis + scope → arithmetic decides "corroborates"
              c. LLM judge (smart tier): groups of candidates with both documents' metadata → corroborates /
                 contradicts / reconciled(kind) / unrelated, with 2–4 sentences of reasoning per pair
```

### Decisions and trade-offs

**Grounding is verified, not trusted.** The model is asked for a verbatim quote, and then the quote is *found* in the
stored page text. Only facts whose evidence was located are shown as grounded; the UI highlights the located span on
the page. This caught the most common failure in the dataset (see Case 4): slide decks lay KPI tiles side by side, so
the text extractor emits all the numbers on one line and all the labels on the next — the model's quote is "right" but
not contiguous. A token-window tier handles that honestly (all numbers present, ≥80 % of tokens inside one ≤800-char
window → grounding = `window`, flagged `quote_not_contiguous`) instead of either rejecting good facts or pretending
the quote was exact.

**Two-stage linking, cheap recall then expensive precision.** Embedding similarity on `subject | attribute` plus an
exact slug match finds candidates for pennies; only candidate groups go to the model. A deterministic judge settles
the easy numeric cases (same period/basis/unit, values within 1 %) with a reasoning string that shows the arithmetic;
its verdicts are stored as `method = deterministic` so you can see which relations never needed a model. The model
judge sees the *documents'* metadata (publication date, period covered, fiscal-year convention, vintage notes) next to
the facts — that context is what lets it say "provisional estimate in a May 2025 report vs. the figure the IMF used in
November 2025" rather than "contradiction".

**Structured outputs everywhere.** Every model call is constrained by a JSON schema (`--json-schema` on the CLI,
`output_config.format` on the API). There is no JSON repair code because there are no JSON failures; the interesting
failures are semantic, and those are what the grounding checks and the `unrelated` verdicts surface.

**Per-document fiscal conventions instead of a global one.** The metadata step asks each document how it labels
years. The Delhivery filings and the Indian institutional reports use April–March; the IMF report mixes fiscal years
written as "FY2024/25" with calendar-year tables. Resolving labels to ISO ranges per document is what makes period
comparison across publishers possible at all; a rule-based parser (`normalize.parse_period`) fills in when the model
leaves the dates blank, and marks the fact `period_parsed_by_rule`.

**SQLite + one process.** Facts, pages, relations, embeddings and the model-call log are five tables in one file. It is
trivially inspectable (`sqlite3 data/factlayer.db`), incremental by construction (a new document only links against
what exists), and more than enough for tens of thousands of facts. A graph database would add a dependency without
adding a capability at this size; the relations table *is* the graph.

**What I deliberately did not do:** OCR (scanned pages are detected and flagged, not guessed at), table-structure
recovery (the model reads tables as text, which works for totals and headline rows but not for dense statements —
see Limitations), and any document-specific rule. Nothing in the code knows about revenue, inflation, Delhivery or
the RBI; the prompts describe *kinds* of facts (headline figures, roles, dates, addresses) and let the document supply
the rest.

### AI tools used

- **Claude** (Sonnet for extraction, Opus for judgement) is the runtime model, called through the Claude Code CLI in
  print mode during development and demo; the Anthropic SDK backend is the intended production path.
- **Claude Code** wrote most of this code with me driving: architecture, prompts, the grounding tiers and the UI were
  iterated on live against the starter documents. The commits are honest about that.
- `fastembed` (`BAAI/bge-small-en-v1.5`, ONNX on CPU) for candidate matching — no embedding API needed.

---

## Results on the starter dataset

_Filled in from the actual run — see the Showcase tab for the live version._

{{RESULTS}}

## The four cases

{{CASES}}

---

## Limitations and next steps

**What does not work well yet**

- **Dense financial statements.** A 100-page annual-report excerpt yields ~1 200 facts, but from a note like "Trade
  receivables" the model reads a few numbers, not the whole table, and column headers (FY24 vs FY23) are sometimes
  mis-assigned when the text extractor interleaves columns. Table-structure recovery (PyMuPDF `find_tables` or a layout
  model) feeding a table-aware prompt is the obvious next step.
- **Candidate recall is bounded by embeddings on `subject | attribute`.** "Revenue from services" and "Revenue from
  operations" match (0.93); "Gross fiscal deficit" and "Fiscal balance" do — but a fact described as "net loss" in one
  document and "loss for the year" in another can sit below the 0.82 threshold. Lowering the threshold trades model
  calls for recall; a better fix is a learned or LLM-produced canonical attribute name per fact.
- **Judgement is per group, not global.** Each group is a new fact plus ≤ 9 candidates. Chains ("A corroborates B,
  B contradicts C") are visible in the UI but never reasoned about together; the next step is clustering into
  per-attribute timelines (all values of "Revenue from operations" across periods and documents) and judging the
  timeline as one object, which also gives an obvious place for revision tracking.
- **Statements are compared more loosely than numbers.** Non-numeric facts (roles, addresses) have no deterministic
  judge and depend entirely on the model; the false-match filter ("unrelated") works but adds model calls.
- **Throughput.** With the CLI backend each extraction call is a subprocess and ~60–100 s for an 18k-char chunk;
  a 100-page report takes ~10 minutes at concurrency 8. The SDK backend with prompt caching and Batch API would
  be several times cheaper and faster for bulk ingestion.
- **No OCR, no images.** Charts on slides are invisible to the system; scanned pages are flagged, not read.

**Next**

1. Attribute canonicalisation: a small "attribute registry" that grows as new kinds of facts appear (the brownie-point
   evolving schema), with the model mapping each new attribute to an existing canonical one or minting a new one.
2. Timeline objects per (subject, canonical attribute): every value with its period and vintage, so revisions
   (advance → provisional → revised estimates) become a first-class story instead of many pairwise relations.
3. Table-aware extraction for financial statements and statistical annexes.
4. Human feedback on relations (confirm / reject) stored back into the DB and used as few-shot examples for the judge.
5. Batch API + caching for the Anthropic backend; re-use the document metadata + system prompt as a cached prefix.

## Additional notes

- Nothing document-specific is hard-coded: filenames, schemas and rules are the same for the Delhivery filings, the
  macro reports, and whatever you upload next. The Showcase tab is a ranking over the relations table, not a curated
  list.
- Credentials never enter the repo: the `claude_cli` backend uses the CLI's own login; the `anthropic` backend reads
  the standard environment/profile.
- Costs shown on the Stats tab are the values *reported by the CLI* for each call; with the subscription-backed CLI
  they are notional, and they include the CLI's own system prompt tokens.
