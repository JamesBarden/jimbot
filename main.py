"""
PokerNow bot — main game loop.

Usage:
    python main.py <game_url>

The browser opens visibly so you can log in, buy in, and take a seat.
Once you're seated and ready, press ENTER in the terminal and the bot takes over.
Press Ctrl+C at any time to stop.
"""
import asyncio
import random
import sys
import time
from playwright.async_api import async_playwright

import scraper
import engine
import actions
from hand_tracker import HandTracker

POLL_INTERVAL = 1.0   # seconds between state checks
ACTION_DELAY  = 1.2   # seconds to wait before acting (looks more human)
DEBUG         = True  # set False to silence diagnostic messages


def dbg(msg: str):
    if DEBUG:
        print(f"  [dbg] {msg}")


async def run(url: str):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=50)
        context = await browser.new_context()
        page = await context.new_page()

        print(f"Opening {url} ...")
        await page.goto(url)

        print("\n[Setup] Log in, buy in, and take a seat in the browser.")
        print("[Setup] Press ENTER here when you're seated and ready.\n")
        await asyncio.get_event_loop().run_in_executor(None, input)

        print("Bot is running. Press Ctrl+C to stop.\n")

        tracker = HandTracker()
        last_state_str   = ""
        last_acted_str   = None
        hands_since_refresh = 0
        last_hole_key    = None
        last_change_time = time.time()
        last_heartbeat   = time.time()
        none_streak      = 0       # consecutive polls returning None
        REFRESH_EVERY    = 30
        STUCK_TIMEOUT    = 30
        HEARTBEAT_EVERY  = 10      # print "still alive" if idle this long

        while True:
            try:
                state = await scraper.get_game_state(page)

                if state is None:
                    none_streak += 1
                    idle = time.time() - last_change_time

                    # Periodic heartbeat so you can see the bot is alive
                    if time.time() - last_heartbeat > HEARTBEAT_EVERY:
                        dbg(f"waiting — no valid state for {idle:.0f}s "
                            f"(None streak: {none_streak})")
                        last_heartbeat = time.time()

                    if idle > STUCK_TIMEOUT:
                        print(f"[stuck] No valid state for {STUCK_TIMEOUT}s — reloading.")
                        await page.reload()
                        await page.wait_for_load_state("networkidle")
                        last_change_time = time.time()
                        last_heartbeat   = time.time()
                        last_state_str   = ""
                        last_acted_str   = None
                        none_streak      = 0
                        tracker = HandTracker()
                        await asyncio.sleep(3)
                    else:
                        await asyncio.sleep(POLL_INTERVAL)
                    continue

                none_streak = 0

                # Detect new hand
                hole_key = tuple(str(c) for c in state.hole_cards)
                if hole_key != last_hole_key:
                    dbg(f"new hand detected: {' '.join(hole_key)}")
                    last_hole_key  = hole_key
                    last_acted_str = None
                    hands_since_refresh += 1
                    if hands_since_refresh >= REFRESH_EVERY:
                        print(f"[refresh] {hands_since_refresh} hands played — reloading to clear DOM.")
                        await page.reload()
                        await page.wait_for_load_state("networkidle")
                        hands_since_refresh = 0
                        last_state_str   = ""
                        last_acted_str   = None
                        last_change_time = time.time()
                        tracker = HandTracker()
                        await asyncio.sleep(3)
                        continue

                opponent_tier = tracker.update(state)

                state_str = str(state)
                if state_str != last_state_str:
                    print(state_str)
                    last_state_str   = state_str
                    last_change_time = time.time()
                    last_heartbeat   = time.time()
                elif time.time() - last_change_time > STUCK_TIMEOUT:
                    print(f"[stuck] State frozen for {STUCK_TIMEOUT}s — reloading.")
                    await page.reload()
                    await page.wait_for_load_state("networkidle")
                    last_change_time = time.time()
                    last_heartbeat   = time.time()
                    last_state_str   = ""
                    last_acted_str   = None
                    tracker = HandTracker()
                    await asyncio.sleep(3)
                    continue

                if not state.is_my_turn:
                    if time.time() - last_heartbeat > HEARTBEAT_EVERY:
                        dbg(f"not our turn — watching ({time.time()-last_change_time:.0f}s since last change)")
                        last_heartbeat = time.time()
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                if state_str == last_acted_str:
                    dbg("already acted on this state — waiting for DOM to update")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                dbg(f"our turn detected — waiting before acting")
                await asyncio.sleep(ACTION_DELAY + random.uniform(-0.4, 1.2))

                # Re-read right before acting
                state = await scraper.get_game_state(page)
                if state is None:
                    dbg("state became None after delay — skipping")
                    continue
                if not state.is_my_turn:
                    dbg("no longer our turn after delay — skipping")
                    continue

                opponent_tier = tracker.update(state)
                action, amount = engine.decide(state, opponent_tier=opponent_tier)
                await actions.execute(page, action, amount, pot=state.pot, my_stack=state.my_stack)

                last_acted_str = state_str
                last_heartbeat = time.time()
                await asyncio.sleep(POLL_INTERVAL)

            except KeyboardInterrupt:
                print("\nStopping bot.")
                break
            except Exception as e:
                print(f"[loop error] {e}")
                import traceback
                if DEBUG:
                    traceback.print_exc()
                await asyncio.sleep(POLL_INTERVAL)

        await browser.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python main.py <pokernow_game_url>")
        sys.exit(1)
    asyncio.run(run(sys.argv[1]))
