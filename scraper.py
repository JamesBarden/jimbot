"""
Reads game state from a live pokernow page via Playwright.

Selectors sourced from pokernow's rendered React DOM, cross-referenced against
https://github.com/EsawAdhana/pokernow-bot/blob/master/src/ui.ts

If pokernow updates their CSS classes, run inspector.py to find the new ones.
"""
import re
from typing import Optional
from playwright.async_api import Page
from state import Card, GameState

# --- DOM selectors ---------------------------------------------------------
# Update these if pokernow changes their class names.

SEL_TURN          = ".table-player.you-player.decision-current" # pokernow adds this class when action is on us
SEL_CHECK_BTN     = "button.check"
SEL_CALL_BTN      = "button.call"
SEL_FOLD_BTN      = "button.fold"
SEL_RAISE_BTN     = "button.raise"
SEL_HOLE_CARDS    = ".you-player .card"                         # our two hole cards
SEL_BOARD_CARDS   = ".table-cards .card"                        # 0, 3, 4, or 5 community cards
SEL_CARD_RANK     = ".value"                                    # child of a .card element
SEL_CARD_SUIT     = ".suit:not(.sub-suit)"                      # each card has two .suit spans; sub-suit is decorative
SEL_MY_STACK      = ".table-player.you-player .table-player-stack"
SEL_POT_MAIN      = ".table-pot-size .main-value .chips-value"  # chips from closed streets
SEL_POT_ADDON     = ".table-pot-size .add-on .chips-value"      # chips bet on the current street
SEL_BLINDS        = ".blind-value .chips-value"                 # [0]=small blind, [1]=big blind
SEL_OTHER_PLAYERS = ".table-player:not(.you-player):not(.table-player-seat)"
SEL_PLAYER_STACK  = ".table-player-stack"
# ---------------------------------------------------------------------------


def _parse_chips(text: str) -> float:
    """
    Parse a chip/dollar amount string into a float.

    Handles commas ('1,200'), K suffix ('1.5K'), currency symbols ('$10.00'),
    and decimal cents ('0.20'). Returns a float so that dollar-denominated
    games (e.g. $0.10/$0.20 blinds) don't lose precision.
    """
    if not text:
        return 0.0
    cleaned = re.sub(r"[^0-9.]", "", text.strip())
    try:
        val = float(cleaned)
        if "k" in text.lower():
            val *= 1000
        return round(val, 2)
    except ValueError:
        return 0.0


async def is_my_turn(page: Page) -> bool:
    """pokernow adds the 'decision-current' class to our seat element when it's our turn."""
    return await page.locator(SEL_TURN).count() > 0


async def can_check(page: Page) -> bool:
    btn = page.locator(SEL_CHECK_BTN)
    if await btn.count() == 0:
        return False
    return await btn.is_enabled()


async def _parse_card_element(page: Page, locator) -> Card:
    """Extract rank and suit text from a single .card DOM element."""
    raw_rank = (await locator.locator(SEL_CARD_RANK).inner_text(timeout=1000)).strip()
    raw_suit = (await locator.locator(SEL_CARD_SUIT).inner_text(timeout=1000)).strip()
    return Card.from_pokernow(raw_rank, raw_suit)


async def get_hole_cards(page: Page) -> list:
    """Return our two hole cards, or an empty list if they aren't visible yet."""
    cards = page.locator(SEL_HOLE_CARDS)
    result = []
    for i in range(await cards.count()):
        try:
            result.append(await _parse_card_element(page, cards.nth(i)))
        except Exception:
            pass
    return result


async def get_board_cards(page: Page) -> list:
    """Return 0 (preflop), 3 (flop), 4 (turn), or 5 (river) community cards."""
    cards = page.locator(SEL_BOARD_CARDS)
    result = []
    for i in range(await cards.count()):
        try:
            result.append(await _parse_card_element(page, cards.nth(i)))
        except Exception:
            pass
    return result


async def get_big_blind(page: Page) -> int:
    """Read the big blind amount from the blinds display. Falls back to 20."""
    blinds = page.locator(SEL_BLINDS)
    count = await blinds.count()
    if count >= 2:
        return _parse_chips(await blinds.nth(1).inner_text(timeout=1000))
    if count == 1:
        return _parse_chips(await blinds.nth(0).inner_text(timeout=1000))
    return 20


async def get_to_call(page: Page) -> int:
    """
    Parse the forced call amount from the call button.

    pokernow labels this button 'CALL 120' when facing a bet, but 'BET 20'
    when no one has bet yet (i.e. check is also available). In the BET case
    the payment is voluntary, so we return 0 — the engine will decide whether
    to open-bet via the raise path.
    """
    btn = page.locator(SEL_CALL_BTN)
    if await btn.count() == 0:
        return 0
    text = (await btn.inner_text(timeout=1000)).strip()
    if not text.lower().startswith("call"):
        return 0   # 'BET 20' or similar — not a forced call
    parts = text.split()
    return _parse_chips(parts[-1]) if len(parts) >= 2 else 0


async def get_my_stack(page: Page) -> int:
    el = page.locator(SEL_MY_STACK)
    if await el.count() == 0:
        return 0
    return _parse_chips(await el.inner_text(timeout=1000))


async def get_pot(page: Page) -> int:
    """
    Total pot = main pot (chips from closed streets) + add-on (current street).
    pokernow splits these into two separate elements.
    """
    main = 0
    main_el = page.locator(SEL_POT_MAIN)
    if await main_el.count() > 0:
        main = _parse_chips(await main_el.inner_text(timeout=1000))

    addon = 0
    addon_el = page.locator(SEL_POT_ADDON)
    if await addon_el.count() > 0:
        addon = _parse_chips(await addon_el.inner_text(timeout=1000))

    return main + addon


async def get_phase(page: Page) -> str:
    """Infer street from how many community cards are visible."""
    count = await page.locator(SEL_BOARD_CARDS).count()
    return {0: "preflop", 3: "flop", 4: "turn", 5: "river"}.get(count, "preflop")


async def get_num_opponents(page: Page) -> int:
    """
    Count active (non-folded) opponents.

    Empty seats have the .table-player-seat class and are excluded by the
    selector. Folded players keep their seat but gain a .fold class.
    Returns at least 1 so the engine never divides by zero.
    """
    all_players = page.locator(SEL_OTHER_PLAYERS)
    active = 0
    for i in range(await all_players.count()):
        p = all_players.nth(i)
        classes = await p.get_attribute("class") or ""
        if "fold" in classes:
            continue
        stack_el = p.locator(SEL_PLAYER_STACK)
        if await stack_el.count() == 0:
            continue
        if not (await stack_el.inner_text(timeout=1000)).strip():
            continue
        active += 1
    return max(active, 1)


async def get_game_state(page: Page) -> Optional[GameState]:
    """
    Assemble a full GameState from the current DOM.
    Returns None between hands (when hole cards aren't dealt yet).
    """
    try:
        turn = await is_my_turn(page)
        hole = await get_hole_cards(page)

        if len(hole) < 2:
            return None

        my_stack = await get_my_stack(page)
        if my_stack == 0:
            return None   # transient between-hand state, ignore

        board = await get_board_cards(page)
        if len(board) not in (0, 3, 4, 5):
            return None   # impossible count (1 or 2) means animation is mid-frame

        return GameState(
            hole_cards=hole,
            board_cards=board,
            pot=await get_pot(page),
            to_call=await get_to_call(page),
            my_stack=my_stack,
            big_blind=await get_big_blind(page),
            num_opponents=await get_num_opponents(page),
            phase=await get_phase(page),
            is_my_turn=turn,
            can_check=await can_check(page),
        )
    except Exception as e:
        print(f"[scraper] error reading state: {e}")
        return None
