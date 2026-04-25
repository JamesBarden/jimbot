"""
PokerNow bot — main game loop.

Usage:
    python main.py <game_url>

The browser opens visibly so you can log in, buy in, and take a seat.
Once you're seated and ready, press ENTER in the terminal and the bot takes over.
Press Ctrl+C at any time to stop.

Persistence during a session:
  HandContext      — per-hand memory (our actions, villain actions, PFA)
  ProfileRegistry  — per-opponent session stats (VPIP/PFR/AF/cbet%)
  SessionLogger    — decisions_*.jsonl, hands_*.csv, console_*.log
"""
import argparse
import asyncio
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from playwright.async_api import async_playwright

import scraper
import engine
import actions
from hand_tracker import HandTracker
from hand_context import HandContext
from opponent_profiles import ProfileRegistry
from session_logger import SessionLogger
from ledger_fetch import fetch_ledger

POLL_INTERVAL = 1.0   # seconds between state checks
ACTION_DELAY  = 1.2   # seconds to wait before acting (looks more human)
DEBUG         = True  # set False to silence diagnostic messages
DEPLOY_TIMEOUT = 60   # seconds — upper bound on autodeploy network operations

HERE     = os.path.dirname(os.path.abspath(__file__))
LOG_DIR  = os.path.join(HERE, "Logs")


def dbg(msg: str):
    if DEBUG:
        print(f"  [dbg] {msg}")


def _blend_tier(profile_tier: str, hand_tier: str) -> str:
    """
    Blend a session-level profile tier with the current-hand dynamic tier.
    Strategy: take whichever is tighter (smaller index), so preflop aggression
    still tightens us up even against an otherwise-loose regular.
    """
    ordered = ["premium", "tight", "medium", "wide", "random"]
    try:
        return ordered[min(ordered.index(profile_tier), ordered.index(hand_tier))]
    except ValueError:
        return hand_tier


def _autodeploy():
    """
    Rebuild the dashboard data and push to origin. Swallow all errors so a
    network/auth hiccup at shutdown never breaks the user's session.
    """
    if os.environ.get("JIMBOT_NO_DEPLOY"):
        print("[deploy] skipped — JIMBOT_NO_DEPLOY is set")
        return
    script = os.path.join(HERE, "scripts", "deploy_session.py")
    if not os.path.isfile(script):
        return
    try:
        print("[deploy] auto-deploying dashboard ...")
        result = subprocess.run(
            [sys.executable, script],
            cwd=HERE,
            timeout=DEPLOY_TIMEOUT,
        )
        if result.returncode != 0:
            print(f"[deploy] WARN: deploy exited with code {result.returncode} — "
                  f"run manually: python3 scripts/deploy_session.py")
    except subprocess.TimeoutExpired:
        print(f"[deploy] WARN: deploy timed out (>{DEPLOY_TIMEOUT}s) — "
              f"check network/auth, then run: python3 scripts/deploy_session.py")
    except Exception as e:
        print(f"[deploy] WARN: auto-deploy failed ({e}) — "
              f"run manually: python3 scripts/deploy_session.py")


def _print_review_brief():
    """
    Run scripts/review_session.py inline so the user sees the post-session
    brief (top winning/losing hands, decision-source breakdown, ASK-CLAUDE
    footer) without having to run a second command. Errors are swallowed —
    review brief is a nice-to-have, not load-bearing.
    """
    script = os.path.join(HERE, "scripts", "review_session.py")
    if not os.path.isfile(script):
        return
    print()
    print("=" * 72)
    print("SESSION REVIEW BRIEF (paste below into Claude Code for tuning advice)")
    print("=" * 72)
    try:
        subprocess.run(
            [sys.executable, script],
            cwd=HERE,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        print("[review] WARN: review script timed out — run manually: "
              "python3 scripts/review_session.py")
    except Exception as e:
        print(f"[review] WARN: review failed ({e}) — "
              f"run manually: python3 scripts/review_session.py")


async def run(url: str, no_deploy: bool = False):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=50)
        context_browser = await browser.new_context()
        page = await context_browser.new_page()

        print(f"Opening {url} ...")
        await page.goto(url)

        print("\n[Setup] Log in, buy in, and take a seat in the browser.")
        print("[Setup] Press ENTER here when you're seated and ready.\n")
        await asyncio.get_event_loop().run_in_executor(None, input)

        print("Bot is running. Press Ctrl+C to stop.\n")

        # Session-level objects
        logger      = SessionLogger(LOG_DIR, game_url=url)
        profile_path = os.path.join(LOG_DIR, "profiles.json")
        profiles    = ProfileRegistry(persist_path=profile_path)

        tracker         = HandTracker()
        hand_ctx: HandContext | None = None
        last_state      = None          # most recent non-None GameState (for hand finalize)
        last_state_str  = ""
        last_acted_str  = None
        hands_since_refresh = 0
        last_hole_key   = None
        last_change_time = time.time()
        last_heartbeat  = time.time()
        none_streak     = 0
        REFRESH_EVERY   = 30
        STUCK_TIMEOUT   = 30
        HEARTBEAT_EVERY = 10

        async def finalize_hand(current_state):
            """Called when a new hand is detected — closes out the previous one."""
            nonlocal hand_ctx
            if hand_ctx is None or current_state is None:
                return
            row = hand_ctx.finalize(
                end_stack=current_state.my_stack,
                final_board=(last_state.board_cards if last_state else []),
                final_pot=(last_state.pot if last_state else 0.0),
                villain_tier_end=tracker.current_tier,
            )
            logger.log_hand(row)
            profiles.ingest_hand(hand_ctx)
            # Sportsmanship: type "nhnh" in chat after every winning hand.
            try:
                if float(row.get("stack_delta") or 0) > 0:
                    await actions.send_chat_message(page, "nhnh")
            except Exception as e:
                print(f"[chat] WARN: post-win send skipped ({e})")
            hand_ctx = None

        try:
            while True:
                try:
                    state = await scraper.get_game_state(page)

                    if state is None:
                        none_streak += 1
                        idle = time.time() - last_change_time

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
                            hand_ctx = None
                            await asyncio.sleep(3)
                        else:
                            await asyncio.sleep(POLL_INTERVAL)
                        continue

                    none_streak = 0

                    # New-hand detection — close out previous context, start a new one
                    hole_key = tuple(str(c) for c in state.hole_cards)
                    if hole_key != last_hole_key:
                        dbg(f"new hand detected: {' '.join(hole_key)}")
                        await finalize_hand(state)

                        hand_ctx = HandContext(
                            hand_id       = datetime.now().strftime("%H%M%S") + "_" + "".join(hole_key),
                            hole_cards    = hole_key,
                            start_stack   = state.my_stack,
                            bb            = state.big_blind,
                            position      = state.position,
                            num_opponents = state.num_opponents,
                            villain_names = tuple(state.villain_names),
                        )

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
                            hand_ctx = None
                            await asyncio.sleep(3)
                            continue

                    # Feed observation to hand context even when it's not our turn,
                    # so villain activity on earlier streets is inferred correctly.
                    if hand_ctx is not None:
                        hand_ctx.observe(state)

                    opponent_tier = tracker.update(state)
                    # Blend in session-level profile if heads-up against a known name
                    if state.num_opponents == 1 and state.villain_names:
                        prof_tier = profiles.tier_for(state.villain_names[0])
                        opponent_tier = _blend_tier(prof_tier, opponent_tier)

                    last_state = state

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
                        hand_ctx = None
                        await asyncio.sleep(3)
                        continue

                    if not state.is_my_turn:
                        if time.time() - last_heartbeat > HEARTBEAT_EVERY:
                            dbg(f"not our turn — watching "
                                f"({time.time()-last_change_time:.0f}s since last change)")
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

                    if hand_ctx is not None:
                        hand_ctx.observe(state)
                    opponent_tier = tracker.update(state)
                    if state.num_opponents == 1 and state.villain_names:
                        prof_tier = profiles.tier_for(state.villain_names[0])
                        opponent_tier = _blend_tier(prof_tier, opponent_tier)

                    action, amount = engine.decide(
                        state,
                        opponent_tier = opponent_tier,
                        context       = hand_ctx,
                        decision_sink = logger.log_decision,
                    )
                    if hand_ctx is not None:
                        hand_ctx.record_hero(state.phase, action, amount)

                    await actions.execute(page, action, amount,
                                          pot=state.pot, my_stack=state.my_stack)

                    last_state     = state
                    last_acted_str = state_str
                    last_heartbeat = time.time()
                    await asyncio.sleep(POLL_INTERVAL)

                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    print(f"[loop error] {e}")
                    import traceback
                    if DEBUG:
                        traceback.print_exc()
                    await asyncio.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\nStopping bot.")

        finally:
            # Close out the in-progress hand if there is one
            if hand_ctx is not None and last_state is not None:
                await finalize_hand(last_state)
            hands_played = logger.hands
            profiles.print_summary()
            profiles.save()
            logger.close()

            # Pull the authoritative ledger CSV before the dashboard build
            # so per-session P&L can be sourced from it instead of the
            # error-prone sum-of-bb_delta approximation. Errors are
            # swallowed — ledger fetch is opportunistic.
            if hands_played > 0:
                ledger_dest = os.path.join(LOG_DIR, f"ledger_{logger.session_id}.csv")
                try:
                    if fetch_ledger(url, ledger_dest):
                        print(f"[ledger] saved → {ledger_dest}")
                except Exception as e:
                    print(f"[ledger] WARN: save failed ({e})")

            # Auto-deploy: only meaningful if there's new data and flags allow
            if hands_played == 0:
                print("[deploy] no hands played this session — skipping")
            elif no_deploy:
                print("[deploy] skipped — --no-deploy flag was set")
            else:
                _autodeploy()

            # Print review brief inline so user can paste it into Claude Code
            if hands_played > 0:
                _print_review_brief()

            # Bound browser-close so a stuck Playwright process can't hang us
            print("[shutdown] closing browser ...")
            try:
                await asyncio.wait_for(browser.close(), timeout=10.0)
            except asyncio.TimeoutError:
                print("[shutdown] browser close timed out (>10s) — force-exiting")
            except Exception as e:
                print(f"[shutdown] browser close error: {e}")
            print(f"[session] ended — {hands_played} hands played")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="PokerNow bot (Jimbot)")
    ap.add_argument("url", help="PokerNow game URL")
    ap.add_argument("--no-deploy", action="store_true",
                    help="skip auto-deploy of dashboard data on session end")
    args = ap.parse_args()
    asyncio.run(run(args.url, no_deploy=args.no_deploy))
