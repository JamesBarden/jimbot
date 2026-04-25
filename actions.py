"""
Executes poker actions via Playwright button clicks / input.
"""
import asyncio
from playwright.async_api import Page


async def dismiss_raise_panel(page: Page):
    """Close the raise panel if it's still open (e.g. after a failed raise)."""
    panel = page.locator(".raise-controller-form")
    if await panel.count() > 0 and await panel.is_visible():
        # Press Escape to close, or click outside the panel
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.1)


async def fold(page: Page):
    await dismiss_raise_panel(page)
    await page.click("button.fold")


async def check(page: Page):
    await dismiss_raise_panel(page)
    await page.click("button.check")


async def call(page: Page):
    await dismiss_raise_panel(page)
    await page.click("button.call")


# Confirmed via inspector.py: raise form has input[type="text"] inside .raise-bet-value
SEL_RAISE_INPUT  = ".raise-bet-value input[type='text']"
SEL_RAISE_SUBMIT = ".raise-controller-form input[type='submit']"

# Preset buttons confirmed: Min Raise / 1/2 Pot / 3/4 Pot / Pot / All In
# Indexed 0-4 — used as a fallback if the text input can't be found.
_PRESET_LABELS = ["min raise", "1/2 pot", "3/4 pot", "pot", "all in"]


async def raise_amount(page: Page, amount: int, pot: int = 0, my_stack: int = 0):
    """
    Open the raise panel, type a custom amount, and submit.

    If the text input isn't found, falls back to clicking the semantically
    closest preset button (Min Raise / 1/2 Pot / 3/4 Pot / Pot / All In).
    If raise is disabled entirely, falls back to call.
    """
    raise_btn = page.locator("button.raise")

    if not await raise_btn.is_enabled():
        call_btn = page.locator("button.call")
        if await call_btn.count() > 0 and await call_btn.is_enabled():
            await call_btn.click()
        return

    await raise_btn.click()
    await asyncio.sleep(0.2)

    # Primary path: type custom amount into the text input
    input_el = page.locator(SEL_RAISE_INPUT)
    if await input_el.count() > 0:
        # triple_click reliably selects all text in any input (Meta+a can fail in
        # React inputs and leave the cursor inside existing text, causing the typed
        # number to be appended rather than replacing — the 10× sizing bug).
        await input_el.click(click_count=3)   # select-all via triple-click
        await input_el.press_sequentially(f"{amount:.2f}", delay=30)
    else:
        await _click_preset(page, amount, pot, my_stack)

    await asyncio.sleep(0.15)
    submit = page.locator(SEL_RAISE_SUBMIT)
    if await submit.count() > 0 and await submit.is_enabled():
        await submit.click(timeout=5000)
    else:
        # Amount was rejected by the site (below min raise) — fall back to preset
        print("  [raise] amount rejected by site, falling back to Min Raise")
        await _click_preset_by_label(page, "min raise")
        await asyncio.sleep(0.1)
        if await submit.count() > 0 and await submit.is_enabled():
            await submit.click(timeout=5000)


async def _click_preset_by_label(page: Page, label: str):
    """Click a preset button by its exact label (case-insensitive)."""
    btns = page.locator(".default-bet-buttons button")
    for i in range(await btns.count()):
        if (await btns.nth(i).inner_text()).strip().lower() == label:
            await btns.nth(i).click()
            return


async def _click_preset(page: Page, amount: int, pot: int, my_stack: int):
    """
    Choose the most appropriate preset button given our target raise amount.

    Preset thresholds (approximate):
      All In   — amount >= 95% of stack
      Pot      — amount >= 85% of pot
      3/4 Pot  — amount >= 60% of pot
      1/2 Pot  — amount >= 35% of pot
      Min Raise — everything else
    """
    btns = page.locator(".default-bet-buttons button")
    if await btns.count() == 0:
        return

    if my_stack > 0 and amount >= my_stack * 0.95:
        label = "all in"
    elif pot > 0 and amount >= pot * 0.85:
        label = "pot"
    elif pot > 0 and amount >= pot * 0.60:
        label = "3/4 pot"
    elif pot > 0 and amount >= pot * 0.35:
        label = "1/2 pot"
    else:
        label = "min raise"

    for i in range(await btns.count()):
        if (await btns.nth(i).inner_text()).strip().lower() == label:
            await btns.nth(i).click()
            return

    # If label not found for some reason, click the first button
    await btns.nth(0).click()


# Chat composer selectors aren't fully nailed (the input only renders
# once the composer opens) — try the likely candidates and bail quietly
# if none match. Keep the list ordered most-specific → broad.
_CHAT_INPUT_SELECTORS = [
    "textarea.chat-input",
    ".chat-container textarea",
    ".chat textarea",
    "textarea[placeholder*='message' i]",
    ".chat-container input[type='text']",
    "[class*='chat'] textarea",
]


async def send_chat_message(page: Page, message: str) -> bool:
    """
    Best-effort send of a chat message. Returns True on success.

    Flow: click `.chat-new-message-button` to open the composer (falls
    back to keypress 'm' if the button isn't found), type the message
    into whichever input selector matches first, press Enter to send.

    Never raises — chat is cosmetic, not load-bearing. Failures are
    logged but the main loop continues.
    """
    try:
        btn = page.locator(".chat-new-message-button")
        if await btn.count() > 0:
            await btn.first.click()
        else:
            await page.keyboard.press("m")
        await asyncio.sleep(0.25)

        for sel in _CHAT_INPUT_SELECTORS:
            inp = page.locator(sel)
            if await inp.count() == 0:
                continue
            if not await inp.first.is_visible():
                continue
            await inp.first.fill(message)
            await page.keyboard.press("Enter")
            return True
        print(f"[chat] WARN: could not locate chat input — message {message!r} not sent")
        return False
    except Exception as e:
        print(f"[chat] WARN: send failed ({e})")
        return False


async def execute(page: Page, action: str, amount: int = 0, pot: int = 0, my_stack: int = 0):
    print(f"  → {action.upper()}" + (f" {amount}" if amount else ""))
    if action == "fold":
        await fold(page)
    elif action == "check":
        await check(page)
    elif action == "call":
        await call(page)
    elif action == "raise":
        await raise_amount(page, amount, pot=pot, my_stack=my_stack)
