"""
Ledger DOM inspector — dumps the pokernow ledger modal so we can identify
selectors for a future ledger scraper, and so we can cross-reference
recorded session P&L against authoritative ledger numbers.

Usage:
    python3 inspector_ledger.py <game_url> [login_seconds] [ledger_seconds]

Flow (timer-based, no terminal interaction needed):
    1. Opens the game URL.
    2. Sleeps `login_seconds` (default 90) — log in / join the table while
       the timer counts down.  The script polls for `.table-player` and
       advances early once it sees you are seated.
    3. Sleeps `ledger_seconds` (default 30) — press L in the game window
       to open the ledger.  The script polls for ledger-shaped elements
       and advances early once one appears.
    4. Probes a list of likely selectors, prints what it found, and writes
       the modal HTML + full body HTML + summary to Logs/.

Output files (timestamped):
    Logs/ledger_modal_<stamp>.html   — innerHTML of the best-matching modal
    Logs/ledger_page_<stamp>.html    — innerHTML of <body> at dump time
    Logs/ledger_dump_<stamp>.txt     — human-readable summary of selectors
"""
import asyncio
import os
import sys
from datetime import datetime
from playwright.async_api import async_playwright


# Selectors most likely to wrap the ledger panel. Ordered most-specific → broad.
# We probe each, report counts, and pick the first one that wraps a
# non-trivial chunk of HTML as the "primary" target for the modal dump.
# (Earlier we picked the LEDGER tab BUTTON because it matched a generic
# [class*=ledger] query — hence the explicit ordering and minimum size.)
MODAL_CANDIDATES = [
    ".modal.log-modal .modal-body",
    ".modal.log-modal",
    ".modal-body",
    ".log-modal",
    ".game-ledger",
    ".ledger-modal",
    ".ledger-table",
    "[class*='Ledger']",
    ".modal-ctn",
    ".modal-content",
    ".modal",
    "[role='dialog']",
    ".dialog",
    ".popup",
    ".overlay",
    "[class*='ledger' i]",
]
# Minimum innerHTML size for a candidate to qualify as the "primary" dump.
# Anything smaller is almost certainly a button/icon and will be skipped.
MIN_PRIMARY_HTML = 200

# Inside the ledger we expect a table or list of rows with username + numbers.
ROW_CANDIDATES = [
    ".modal.log-modal .modal-body tr",
    ".modal.log-modal .modal-body tbody tr",
    ".modal.log-modal .modal-body li",
    ".modal.log-modal .modal-body [class*='row']",
    ".modal.log-modal .modal-body [class*='player']",
    ".modal.log-modal .modal-body [class*='ledger' i]",
    ".modal-body tr",
    ".modal-body li",
    "[class*='ledger' i] tr",
    "[class*='ledger' i] li",
]


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


async def _wait_until(page, selector: str, max_seconds: int) -> bool:
    """Poll `selector` once a second until found or budget elapses. True if found."""
    for _ in range(max_seconds):
        try:
            if await page.locator(selector).count() > 0:
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def inspect(url: str, login_secs: int, ledger_secs: int):
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Logs")
    os.makedirs(log_dir, exist_ok=True)
    stamp     = _ts()
    modal_fp  = os.path.join(log_dir, f"ledger_modal_{stamp}.html")
    page_fp   = os.path.join(log_dir, f"ledger_page_{stamp}.html")
    summary_fp = os.path.join(log_dir, f"ledger_dump_{stamp}.txt")
    summary_lines: list[str] = []

    def log(line: str = ""):
        print(line, flush=True)
        summary_lines.append(line)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=50)
        page = await browser.new_page()
        await page.goto(url)

        log(f">>> Browser open. Log in / join the table within {login_secs}s.")
        seated = await _wait_until(page, ".table-player", login_secs)
        log(f">>> {'detected .table-player' if seated else 'login window expired'} — moving on")

        log(f">>> Now press L in the game window. Polling for modal for {ledger_secs}s.")
        opened = await _wait_until(page, ".modal.log-modal", ledger_secs)
        log(f">>> {'detected modal' if opened else 'modal poll expired'} — switching to Ledger tab")

        # The modal defaults to the "Full Log" tab. Click the Ledger tab so
        # the body re-fetches and renders the ledger view.
        try:
            tab = page.locator(".log-modal-tab.ledger-button")
            if await tab.count() > 0:
                await tab.first.click()
                log(">>> clicked .log-modal-tab.ledger-button")
            else:
                log(">>> WARN: ledger tab button not found")
        except Exception as e:
            log(f">>> WARN: failed to click ledger tab: {e}")

        # Pokernow fetches the ledger asynchronously: the modal-body shows
        # "Loading..." until the server responds.  Poll for that text to
        # disappear OR for actual table rows to appear.  Wait up to 20s.
        for i in range(20):
            try:
                body = page.locator(".modal.log-modal .modal-body")
                if await body.count() == 0:
                    await asyncio.sleep(1)
                    continue
                txt = (await body.first.inner_text()).strip()
                rows = await page.locator(".modal.log-modal .modal-body tr, .modal.log-modal .modal-body li").count()
                # Heuristic: "Loading" string is gone OR we already see rows
                if rows > 0 or "loading" not in txt.lower():
                    log(f">>> ledger ready after {i+1}s (rows={rows}, body chars={len(txt)})")
                    break
            except Exception as e:
                log(f">>> poll error: {e}")
            await asyncio.sleep(1)
        else:
            log(">>> ledger still loading after 20s — dumping current state anyway")

        await asyncio.sleep(1)

        log(f"===== LEDGER DUMP {stamp} =====")
        log(f"url = {url}")
        log("")

        # ── modal probe ────────────────────────────────────────────────────
        log("===== MODAL CANDIDATES =====")
        primary_html = None
        primary_sel  = None
        for sel in MODAL_CANDIDATES:
            try:
                el  = page.locator(sel)
                cnt = await el.count()
            except Exception as e:
                log(f"  {sel!r:40s} ERROR {e}")
                continue
            if cnt == 0:
                log(f"  {sel!r:40s} not found")
                continue
            classes = await el.first.get_attribute("class") or ""
            log(f"  {sel!r:40s} count={cnt}  first.class={classes!r}")
            if primary_html is None:
                try:
                    candidate = await el.first.inner_html()
                    if len(candidate) >= MIN_PRIMARY_HTML:
                        primary_html = candidate
                        primary_sel  = sel
                    else:
                        log(f"    skipped as primary: only {len(candidate)} chars")
                except Exception as e:
                    log(f"    failed to read inner_html: {e}")
        log("")

        # ── row probe ──────────────────────────────────────────────────────
        log("===== ROW / PLAYER-LINE CANDIDATES =====")
        sample_row_text: list[str] = []
        for sel in ROW_CANDIDATES:
            try:
                el  = page.locator(sel)
                cnt = await el.count()
            except Exception as e:
                log(f"  {sel!r:55s} ERROR {e}")
                continue
            if cnt == 0:
                log(f"  {sel!r:55s} not found")
                continue
            log(f"  {sel!r:55s} count={cnt}")
            for i in range(min(cnt, 25)):
                try:
                    txt = (await el.nth(i).inner_text()).strip().replace("\n", " | ")
                except Exception:
                    continue
                if txt:
                    log(f"    [{i}] {txt!r}")
                    sample_row_text.append(txt)
        log("")

        # ── any element with 'ledger' in its class, anywhere ──────────────
        log("===== ANY [class*=ledger] ELEMENTS =====")
        try:
            el  = page.locator("[class*='ledger' i]")
            cnt = await el.count()
            log(f"  total elements with 'ledger' in class: {cnt}")
            for i in range(min(cnt, 30)):
                cls = await el.nth(i).get_attribute("class") or ""
                tag = await el.nth(i).evaluate("e => e.tagName")
                txt = (await el.nth(i).inner_text()).strip().replace("\n", " | ")[:120]
                log(f"  [{i}] <{tag.lower()}> class={cls!r} text={txt!r}")
        except Exception as e:
            log(f"  ERROR {e}")
        log("")

        # ── headings / labels in the modal (helps identify columns) ───────
        log("===== HEADINGS / LABELS NEAR LEDGER =====")
        for sel in ["h1", "h2", "h3", "th", "[class*='title' i]", "[class*='header' i]"]:
            try:
                el  = page.locator(sel)
                cnt = await el.count()
            except Exception:
                continue
            for i in range(min(cnt, 30)):
                try:
                    txt = (await el.nth(i).inner_text()).strip()
                except Exception:
                    continue
                if txt and ("ledger" in txt.lower() or "buy" in txt.lower()
                            or "net" in txt.lower() or "stack" in txt.lower()
                            or "name" in txt.lower() or "in" in txt.lower()
                            or "out" in txt.lower()):
                    log(f"  {sel!r:30s} [{i}] {txt!r}")
        log("")

        # ── dump primary modal HTML ───────────────────────────────────────
        if primary_html is not None:
            with open(modal_fp, "w") as f:
                f.write(f"<!-- selector: {primary_sel} -->\n")
                f.write(primary_html)
            log(f"[written] modal HTML  ({len(primary_html)} chars) → {modal_fp}")
        else:
            log("[warn] no modal candidate matched — see page HTML dump")

        # ── dump full body HTML as a fallback ─────────────────────────────
        try:
            body_html = await page.locator("body").inner_html()
            with open(page_fp, "w") as f:
                f.write(body_html)
            log(f"[written] body  HTML  ({len(body_html)} chars) → {page_fp}")
        except Exception as e:
            log(f"[warn] failed to dump body HTML: {e}")

        # ── persist summary ───────────────────────────────────────────────
        with open(summary_fp, "w") as f:
            f.write("\n".join(summary_lines) + "\n")
        print(f"\n[written] summary → {summary_fp}")

        log("\nDone. Closing browser.")
        await browser.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 inspector_ledger.py <pokernow_game_url> [login_secs] [ledger_secs]")
        sys.exit(1)
    login_s  = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    ledger_s = int(sys.argv[3]) if len(sys.argv) > 3 else 30
    asyncio.run(inspect(sys.argv[1], login_s, ledger_s))
