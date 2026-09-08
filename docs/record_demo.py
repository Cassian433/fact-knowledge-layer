"""Record the demo video with Playwright (no human at the keyboard).

    uv run python docs/record_demo.py [base_url] [pdf_to_upload]

Produces docs/demo/segments/*.webm (one per segment) and docs/demo/demo.mp4 (concatenated, captions burned in as an
on-page banner). Segment 1 uploads the PDF and shows processing start; segment 2 tours facts / evidence / the four
cases while the document processes in the background; segment 3 returns to the finished document and its relations.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8801"
PDF = Path(sys.argv[2] if len(sys.argv) > 2 else "data/demo/delhivery-q4fy25-press-release.pdf")
OUT = Path(__file__).parent / "demo"
SEG = OUT / "segments"
VIEW = {"width": 1400, "height": 900}

CAPTION_CSS = """
#demo-cap { position: fixed; left: 0; right: 0; bottom: 0; z-index: 9999; background: rgba(8,10,14,.92);
  color: #f2f4f8; font: 20px/1.35 -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 14px 26px;
  border-top: 1px solid #2a3040; letter-spacing: .1px; }
#demo-cap b { color: #5b9cff; font-weight: 600; margin-right: 10px; }
"""


async def caption(pg, step: str, text: str) -> None:
    await pg.evaluate(
        """([step, text, css]) => {
            if (!document.getElementById('demo-cap-css')) { const s = document.createElement('style'); s.id='demo-cap-css'; s.textContent = css; document.head.appendChild(s); }
            let c = document.getElementById('demo-cap');
            if (!c) { c = document.createElement('div'); c.id = 'demo-cap'; document.body.appendChild(c); }
            c.innerHTML = '<b>' + step + '</b>' + text;
        }""",
        [step, text, CAPTION_CSS],
    )


async def tab(pg, name: str) -> None:
    await pg.click(f'nav button[data-tab="{name}"]')
    await pg.wait_for_timeout(600)


async def scroll_to(pg, selector: str, index: int = 0) -> None:
    els = await pg.query_selector_all(selector)
    if len(els) > index:
        await els[index].scroll_into_view_if_needed()
        await pg.evaluate("window.scrollBy(0, -70)")


async def segment(ctx_factory, name: str, fn) -> Path:
    ctx = await ctx_factory()
    pg = await ctx.new_page()
    await pg.goto(BASE)
    await pg.wait_for_timeout(800)
    await fn(pg)
    path = await pg.video.path()
    await ctx.close()
    dest = SEG / f"{name}.webm"
    shutil.move(path, dest)
    return dest


async def main() -> None:
    SEG.mkdir(parents=True, exist_ok=True)
    for old in SEG.glob("*.webm"):
        old.unlink()
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        async def ctx_factory():
            return await browser.new_context(viewport=VIEW, record_video_dir=str(SEG), record_video_size=VIEW)

        # ---- segment 1: documents + upload (~35 s) -------------------------------------------------------------
        async def seg1(pg):
            await caption(pg, "Fact Knowledge Layer", "Six PDFs already in the layer: three Delhivery filings (2022 prospectus, FY24 annual report, Q4 FY24 deck) and three Indian macro reports (Economic Survey, RBI Annual Report, IMF Article IV). Header: 3,702 facts, 97 % grounded, 2,300 cross-document relations.")
            await pg.wait_for_timeout(5500)
            await caption(pg, "Document metadata", "Each document got LLM-derived metadata: publisher, publication date, period covered and — crucially — how it labels years, so \"FY24\" resolves to 1 Apr 2023 – 31 Mar 2024 for this document.")
            await scroll_to(pg, "#docs .card", 1)
            await pg.wait_for_timeout(5500)
            await pg.evaluate("window.scrollTo(0,0)")
            await caption(pg, "Upload a new PDF", "Uploading a document the system has never seen: Delhivery's 3-page Q4 FY25 results press release (May 2025).")
            await pg.wait_for_timeout(2500)
            await pg.set_input_files("#file", str(PDF))
            await pg.wait_for_timeout(3500)
            await caption(pg, "Processing", "Metadata → page-anchored chunks → fact extraction with a strict JSON schema → every quote verified against the page text → linked against all existing facts. Incremental: nothing is rebuilt.")
            await scroll_to(pg, "#docs .card", 6)
            await pg.wait_for_timeout(9000)

        await segment(ctx_factory, "01-upload", seg1)
        t_upload = time.time()

        # ---- segment 2: facts, evidence, showcase (~110 s) ------------------------------------------------------
        async def seg2(pg):
            await tab(pg, "facts")
            await caption(pg, "Facts", "A fact = subject · attribute · value + unit + scale · period (label and ISO dates) · basis / scope — and a verbatim quote from a page. 3,700 facts from 511 pages.")
            await pg.fill("#fq", "revenue from services")
            await pg.click("#fgo")
            await pg.wait_for_timeout(6000)
            rows = await pg.query_selector_all("#ftable tbody tr.clickable")
            target = None
            for r in rows:
                txt = (await r.inner_text()).lower()
                if "81,415" in txt or "8,142" in txt:
                    target = r
                    break
            if target is None and rows:
                target = rows[0]
            if target:
                await target.click(force=True)
                await pg.wait_for_timeout(1200)
                await caption(pg, "Evidence", "Grounding is verified, not trusted: the quote is located on the stored page text and highlighted at its character offsets. Tiers: exact · fuzzy · window · unverified. 97.5 % of facts are grounded.")
                await pg.wait_for_timeout(8000)
                await caption(pg, "Timeline", "The same attribute across every document and period — prospectus, annual report, deck — with each row's relation to this fact: corroborates, reconciled by period or scope, contradicts.")
                tl = await pg.query_selector("#timeline")
                if tl:
                    await tl.scroll_into_view_if_needed(); await pg.evaluate("document.querySelector('#modal').scrollBy(0, -60)")
                await pg.wait_for_timeout(9000)
                await pg.keyboard.press("Escape")
                await pg.wait_for_timeout(600)

            await tab(pg, "showcase")
            await pg.wait_for_timeout(2500)
            await caption(pg, "Case 1 — corroborated, expressed differently", "The annual report says ₹81,415.38 million; the deck says ₹8,142 crore. The judge does the unit arithmetic (81,415 Mn = 8,141.5 Cr), checks the period, and calls it corroborates.")
            await pg.wait_for_timeout(1500)
            # find the 81,415 vs 8,142 card if visible
            cards = await pg.query_selector_all("#show .rel.corroborates")
            for c in cards:
                if "81,415" in (await c.inner_text()):
                    await c.scroll_into_view_if_needed(); await pg.evaluate("window.scrollBy(0, -70)")
                    break
            await pg.wait_for_timeout(13000)

            await caption(pg, "Case 2 — a genuine contradiction", "Corporate office PIN 122002 in the prospectus vs 122001 on one page of the annual report. The judge notices the annual report's other pages say 122002 and calls it a typo — and says what would settle it.")
            await scroll_to(pg, "#show h2", 1)
            await pg.wait_for_timeout(15000)

            await caption(pg, "Case 3 — apparent contradiction, explained", "Real GDP growth FY25: 6.4 % (Economic Survey, Jan 2025, First Advance Estimate) vs 6.5 % (IMF, Nov 2025, outturn) → reconciled by data vintage. Others: definition (4.4 % vs 4.5 % 'IMF definition'), scope (segment vs total), unit, period.")
            await scroll_to(pg, "#show h2", 2)
            await pg.wait_for_timeout(15000)
            await pg.evaluate("window.scrollBy(0, 700)")
            await pg.wait_for_timeout(6000)

            await caption(pg, "Case 4 — failures the system caught", "Quotes that are correct but not contiguous (two-column layouts, slide tiles) — the biggest failure class, diagnosed here and fixed: 12.7 % → 2.5 % unverified. Also: page numbers corrected, numbers missing from their own quote, false candidates the judge rejected.")
            await scroll_to(pg, "#show h2", 3)
            await pg.wait_for_timeout(15000)
            await pg.evaluate("window.scrollBy(0, 900)")
            await pg.wait_for_timeout(6000)

        await segment(ctx_factory, "02-tour", seg2)

        # wait for the uploaded document to finish (poll the API), cap at 6 minutes
        import json, urllib.request
        deadline = time.time() + 360
        while time.time() < deadline:
            docs = json.loads(urllib.request.urlopen(f"{BASE}/api/documents").read())["documents"]
            new = [d for d in docs if d["filename"] == PDF.name]
            if new and new[0]["status"] in ("done", "failed"):
                break
            await asyncio.sleep(5)
        print(f"processing took {time.time() - t_upload:.0f}s")

        # ---- segment 3: the new document, its relations, stats (~35 s) ------------------------------------------
        async def seg3(pg):
            await pg.wait_for_timeout(1000)
            await caption(pg, "The new document is done", "40 facts, all grounded, ~150 relations: its FY24 comparatives corroborate the annual report and the deck (three settled by arithmetic alone); its FY25 figures are reconciled to FY24 by period.")
            await scroll_to(pg, "#docs .card", 6)
            await pg.wait_for_timeout(7000)
            await tab(pg, "relations")
            sel = await pg.query_selector("#rdoc")
            opts = await sel.query_selector_all("option")
            for o in opts:
                if "Q4 FY25" in (await o.inner_text()) or "Press Release" in (await o.inner_text()):
                    await sel.select_option(value=await o.get_attribute("value"))
                    break
            await pg.select_option("#rtype", "corroborates")
            await pg.click("#rgo")
            await pg.wait_for_timeout(1500)
            await caption(pg, "Relations of the new document", "Every relation shows both quotes, both documents, and the judge's reasoning — verifiable against the evidence.")
            await pg.wait_for_timeout(9000)
            await pg.evaluate("window.scrollBy(0, 600)")
            await pg.wait_for_timeout(5000)
            await tab(pg, "stats")
            await caption(pg, "Stats", "Everything is logged: grounding quality, relation counts by kind, and every model call with latency. Code, README and the four cases: see the repository.")
            await pg.wait_for_timeout(7000)

        await segment(ctx_factory, "03-result", seg3)
        await browser.close()

    # ---- concatenate + convert -----------------------------------------------------------------------------------
    segs = sorted(SEG.glob("*.webm"))
    lst = OUT / "segments.txt"
    lst.write_text("".join(f"file '{s.resolve()}'\n" for s in segs))
    final = OUT / "demo.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-an", str(final)], check=True)
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(final)],
                         capture_output=True, text=True).stdout.strip()
    print(f"wrote {final} ({float(dur):.0f}s) from {len(segs)} segments")


asyncio.run(main())
