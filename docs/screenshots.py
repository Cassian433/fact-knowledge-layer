"""Capture README screenshots from a running server:  uv run python docs/screenshots.py [base_url]"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8801"
OUT = Path(__file__).parent / "screenshots"


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1400, "height": 900}, device_scale_factor=1)
        await pg.goto(f"{BASE}/#documents"); await pg.wait_for_timeout(1500)
        await pg.screenshot(path=OUT / "documents.png")

        await pg.goto(f"{BASE}/#facts"); await pg.wait_for_timeout(800)
        await pg.fill("#fq", "revenue from services"); await pg.click("#fgo"); await pg.wait_for_timeout(1200)
        await pg.screenshot(path=OUT / "facts.png")
        rows = await pg.query_selector_all("#ftable tbody tr.clickable")
        if rows:
            await rows[0].click(force=True); await pg.wait_for_timeout(1200)
            await pg.screenshot(path=OUT / "evidence.png")
            await pg.keyboard.press("Escape")

        await pg.goto(f"{BASE}/#showcase"); await pg.wait_for_timeout(3000)
        await pg.screenshot(path=OUT / "showcase-case1.png")
        for i, name in enumerate(["case2", "case3", "case4"], start=1):
            heads = await pg.query_selector_all("#show h2")
            if len(heads) > i:
                await heads[i].scroll_into_view_if_needed(); await pg.wait_for_timeout(400)
                await pg.screenshot(path=OUT / f"showcase-{name}.png")

        await pg.goto(f"{BASE}/#relations"); await pg.wait_for_timeout(800)
        await pg.select_option("#rtype", "reconciled"); await pg.click("#rgo"); await pg.wait_for_timeout(1500)
        await pg.screenshot(path=OUT / "relations.png")

        await pg.goto(f"{BASE}/#stats"); await pg.wait_for_timeout(1200)
        await pg.screenshot(path=OUT / "stats.png")
        await b.close()
    print("wrote", sorted(p.name for p in OUT.glob("*.png")))


asyncio.run(main())
