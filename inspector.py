"""
DOM inspector — run this once to dump the live game HTML so you can verify
or refine selectors in scraper.py.

Usage:
    python inspector.py <game_url>

Opens the page, waits for you to log in, then prints a summary of
all player elements, card elements, pot/stack values it can find.
"""
import asyncio
import sys
from playwright.async_api import async_playwright


async def inspect(url: str):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=50)
        page = await browser.new_page()
        await page.goto(url)

        print("Log in / join the game, then press ENTER.")
        await asyncio.get_event_loop().run_in_executor(None, input)

        print("\n===== PLAYER SEATS =====")
        players = page.locator(".table-player")
        n = await players.count()
        for i in range(n):
            p = players.nth(i)
            classes = await p.get_attribute("class") or ""
            stack_el = p.locator(".table-player-stack")
            stack = (await stack_el.inner_text()).strip() if await stack_el.count() > 0 else "—"
            name_el = p.locator(".table-player-name, .username")
            name = (await name_el.inner_text()).strip() if await name_el.count() > 0 else "—"
            print(f"  [{i}] classes={classes!r:60s}  name={name!r}  stack={stack!r}")

        print("\n===== HOLE CARDS =====")
        hole = page.locator(".you-player .card")
        for i in range(await hole.count()):
            c = hole.nth(i)
            html = await c.inner_html()
            print(f"  card {i}: {html}")

        print("\n===== BOARD CARDS =====")
        board = page.locator(".table-cards .card")
        for i in range(await board.count()):
            c = board.nth(i)
            html = await c.inner_html()
            print(f"  card {i}: {html}")

        print("\n===== POT =====")
        for sel in [
            ".table-pot-size",
            ".table-pot-size .main-value .chips-value",
            ".table-pot-size .add-on .chips-value",
        ]:
            el = page.locator(sel)
            if await el.count() > 0:
                print(f"  {sel!r:55s} → {(await el.first.inner_text()).strip()!r}")

        print("\n===== BUTTONS =====")
        for sel in ["button.fold", "button.check", "button.call", "button.raise"]:
            el = page.locator(sel)
            if await el.count() > 0:
                text = (await el.first.inner_text()).strip()
                enabled = await el.first.is_enabled()
                print(f"  {sel!r:30s} text={text!r}  enabled={enabled}")

        print("\n===== RAISE PANEL (open it manually then press ENTER) =====")
        await asyncio.get_event_loop().run_in_executor(None, input)
        for sel in [
            ".raise-controller-form",
            ".raise-controller-form input",
            ".default-bet-buttons",
            ".default-bet-buttons button",
        ]:
            el = page.locator(sel)
            cnt = await el.count()
            if cnt > 0:
                for i in range(cnt):
                    html = await el.nth(i).inner_html()
                    print(f"  {sel!r}[{i}]: {html[:200]}")

        print("\nDone. Close the browser window to exit.")
        await page.wait_for_timeout(60_000)
        await browser.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python inspector.py <pokernow_game_url>")
        sys.exit(1)
    asyncio.run(inspect(sys.argv[1]))
