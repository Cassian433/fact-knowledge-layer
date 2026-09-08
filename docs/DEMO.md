# Demo script (≤ 3 minutes)

Record at 1400×900 or larger, browser on `http://127.0.0.1:8801`. Have the six starter PDFs already processed
(`uv run python -m factlayer.cli ingest dataset/starter-datasets/*/*.pdf`) and the server running
(`uv run python -m factlayer.cli serve --port 8801`). Keep `data/demo/delhivery-q4fy25-press-release.pdf`
(3 pages, public: https://www.delhivery.com/uploads/2025/05/PressRelease_Q4FY25.pdf) on the desktop — it is **not**
one of the starter documents, so it doubles as the "works on a PDF you have never seen" proof.

| time | screen | say |
|---|---|---|
| 0:00 | **Documents** tab, six cards | "Six PDFs, two unrelated corpora — Delhivery filings and Indian macro reports. Every document got LLM-derived metadata: publisher, publication date, and *how it labels years* — that fiscal-year convention is what makes cross-document comparison possible." |
| 0:20 | drag `delhivery-q4fy25-press-release.pdf` onto the drop zone | "Uploading a document the system has never seen. It's chunked with page markers, facts are extracted with a strict JSON schema, every quote is verified against the page text, and then it's linked against everything already here — incrementally, nothing is rebuilt." (progress bar animates; we come back to it at the end) |
| 0:40 | **Facts** tab, search `revenue from services` | "Each fact is subject / attribute / value+unit+scale / period resolved to ISO dates / basis / scope — and a verbatim quote. Click one." |
| 0:55 | fact modal: annual report, ₹81,415 Mn, page highlighted | "The quote is highlighted on the stored page text at the exact character offsets — this is verified, not trusted. Grounding tiers: exact, fuzzy, window, unverified." |
| 1:10 | **Showcase** tab → **Case 1** | "Corroboration expressed differently: the annual report says ₹81,415 million, the earnings deck says ₹8,142 crore. The judge did the unit arithmetic — 81,415 million = 8,141.5 crore — same FY24 period, corroborates." |
| 1:30 | **Case 2** | "A likely contradiction: same subject, attribute, period and scope, values disagree, and the reasoning says exactly what it would check to settle it." *(pick the top card; read one sentence of the reasoning)* |
| 1:50 | **Case 3** | "Apparent contradictions explained by context — one example per reconciliation kind: period (FY24 vs Q4 FY24), vintage (advance estimate in January vs provisional in May vs the IMF's number in November), scope (consolidated vs segment), unit." |
| 2:15 | **Case 4** | "Failures the system caught itself: quotes that are correct but not contiguous because slide tiles are laid out in columns — handled with a token-window tier and flagged; page numbers the model got wrong and grounding corrected; numbers missing from their own quote; and false candidate matches the judge rejected as *unrelated*." |
| 2:40 | back to **Documents**: the press release is `done` | "The new document finished: 40 facts, ~150 relations — its FY24 comparatives corroborate the annual report and the deck (three settled by arithmetic alone), its FY25 figures are reconciled to FY24 by period." Click one relation. |
| 2:55 | **Stats** tab | "Everything is logged: grounding quality, relation counts, every model call with latency. Thanks." |

The press release takes ~3.5 minutes end to end (one extraction call, seven judgement batches), so either start recording, upload, and cut to the tour while it runs, or record the upload separately. Make sure it is **not** already in the layer (remove it from the Documents tab first). Fallbacks: if the upload has not finished by 2:40, show its progress line and say processing of a 100-page report
takes ~10 minutes at concurrency 12 with the CLI backend; the README has the timings.
