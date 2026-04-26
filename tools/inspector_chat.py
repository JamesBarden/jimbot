"""
Chat composer DOM inspector — captures the live HTML of the chat input
that appears after clicking `.chat-new-message-button`. The composer
only renders on demand, so the static body dump from inspector_ledger.py
never sees it.

Usage:
    python3 inspector_chat.py <game_url> [login_seconds]

Flow (timer-based):
    1. Opens the game URL.
    2. Sleeps `login_seconds` (default 90) — log in / sit at the table.
       The script polls for `.table-player` and advances early once seated.
    3. Clicks `.chat-new-message-button` to open the composer (falls
       back to keypress 'm' if the button isn't found).
    4. Waits a beat for the composer to render, then probes a list of
       candidate input selectors and dumps:
         Logs/chat_dump_<stamp>.txt   — selector probe summary
         Logs/chat_modal_<stamp>.html — innerHTML of .chat-container
         Logs/chat_page_<stamp>.html  — full <body> innerHTML
"""
from __future__ import annotations
import asyncio
import os
import sys
from datetime import datetime
from playwright.async_api import async_playwright

INPUT_CANDIDATES = [
    "textarea.chat-input",
    "input.chat-input",
    ".chat-container textarea",
    ".chat-container input[type='text']",
    ".chat textarea",
    ".chat input[type='text']",
    "textarea[placeholder*='message' i]",
    "input[placeholder*='message' i]",
    "[class*='chat'] textarea",
    "[class*='chat'] input[type='text']",
    "[class*='message-input' i]",
    "[class*='new-message' i] textarea",
    "[class*='new-message' i] input",
    "form.chat-new-message-form textarea",
    "form.chat-new-message-form input",
]


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


async def _wait_until(page, selector: str, max_seconds: int) -> bool:
    for _ in range(max_seconds):
        try:
            if await page.locator(selector).count() > 0:
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def inspect(url: str, login_secs: int):
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Logs")
    os.makedirs(log_dir, exist_ok=True)
    stamp        = _ts()
    summary_fp   = os.path.join(log_dir, f"chat_dump_{stamp}.txt")
    chat_html_fp = os.path.join(log_dir, f"chat_modal_{stamp}.html")
    body_html_fp = os.path.join(log_dir, f"chat_page_{stamp}.html")
    summary: list[str] = []

    def log(line: str = ""):
        print(line, flush=True)
        summary.append(line)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=50)
        page = await browser.new_page()
        await page.goto(url)

        log(f">>> Browser open. Log in / sit at the table within {login_secs}s.")
        seated = await _wait_until(page, ".table-player", login_secs)
        log(f">>> {'detected .table-player' if seated else 'login window expired'} — opening chat composer")

        # Open the composer.
        clicked = False
        try:
            btn = page.locator(".chat-new-message-button")
            if await btn.count() > 0:
                await btn.first.click()
                clicked = True
                log(">>> clicked .chat-new-message-button")
            else:
                log(">>> .chat-new-message-button not found — falling back to keypress 'm'")
                await page.keyboard.press("m")
        except Exception as e:
            log(f">>> WARN: open failed ({e})")

        # Give the composer a generous beat to render — the bot's 250ms
        # may have been the slow-open the user described.
        await asyncio.sleep(1.5)

        log("")
        log(f"===== CHAT DUMP {stamp} =====")
        log(f"url = {url}")
        log(f"clicked button = {clicked}")
        log("")

        # ── input candidate probe ─────────────────────────────────────────
        log("===== INPUT CANDIDATES =====")
        any_match = False
        for sel in INPUT_CANDIDATES:
            try:
                el  = page.locator(sel)
                cnt = await el.count()
            except Exception as e:
                log(f"  {sel!r:55s} ERROR {e}")
                continue
            if cnt == 0:
                log(f"  {sel!r:55s} not found")
                continue
            visible = False
            tag     = "?"
            cls     = ""
            try:
                visible = await el.first.is_visible()
                tag     = await el.first.evaluate("e => e.tagName")
                cls     = await el.first.get_attribute("class") or ""
            except Exception:
                pass
            log(f"  {sel!r:55s} count={cnt} visible={visible}  <{tag.lower()}> class={cls!r}")
            any_match = any_match or (cnt > 0 and visible)
        log("")

        # ── any element with 'chat' in its class ──────────────────────────
        log("===== ANY [class*=chat] ELEMENTS =====")
        try:
            el  = page.locator("[class*='chat' i]")
            cnt = await el.count()
            log(f"  total elements with 'chat' in class: {cnt}")
            for i in range(min(cnt, 60)):
                try:
                    cls = await el.nth(i).get_attribute("class") or ""
                    tag = await el.nth(i).evaluate("e => e.tagName")
                    txt = (await el.nth(i).inner_text()).strip().replace("\n", " | ")[:80]
                except Exception:
                    continue
                log(f"  [{i:2d}] <{tag.lower()}> class={cls!r}  text={txt!r}")
        except Exception as e:
            log(f"  ERROR {e}")
        log("")

        # ── any visible <textarea> or <input type=text> on the page ──────
        log("===== ALL TEXTAREAS / TEXT INPUTS =====")
        for sel in ["textarea", "input[type='text']", "input:not([type])", "[contenteditable]"]:
            try:
                el  = page.locator(sel)
                cnt = await el.count()
            except Exception:
                continue
            log(f"  {sel!r}  count={cnt}")
            for i in range(min(cnt, 10)):
                try:
                    cls = await el.nth(i).get_attribute("class") or ""
                    pl  = await el.nth(i).get_attribute("placeholder") or ""
                    vis = await el.nth(i).is_visible()
                    log(f"    [{i}] visible={vis} class={cls!r} placeholder={pl!r}")
                except Exception:
                    continue
        log("")

        # ── dump container HTML ──────────────────────────────────────────
        try:
            chat_html = await page.locator(".chat-container").first.inner_html()
            with open(chat_html_fp, "w") as f:
                f.write(chat_html)
            log(f"[written] chat-container HTML ({len(chat_html)} chars) → {chat_html_fp}")
        except Exception as e:
            log(f"[warn] no .chat-container: {e}")

        try:
            body_html = await page.locator("body").inner_html()
            with open(body_html_fp, "w") as f:
                f.write(body_html)
            log(f"[written] body HTML           ({len(body_html)} chars) → {body_html_fp}")
        except Exception as e:
            log(f"[warn] no body: {e}")

        with open(summary_fp, "w") as f:
            f.write("\n".join(summary) + "\n")
        log(f"[written] summary             → {summary_fp}")

        log("\nDone. Closing browser.")
        await browser.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 inspector_chat.py <pokernow_game_url> [login_secs]")
        sys.exit(1)
    login_s = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    asyncio.run(inspect(sys.argv[1], login_s))
