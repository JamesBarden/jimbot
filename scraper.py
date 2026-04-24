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
SEL_DEALER_BTN    = ".dealer-button-ctn"   # has class 'dealer-position-N' matching table-player-N
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
    for token in text.split()[1:]:   # scan left-to-right; handles "CALL 6.43 (all in)"
        val = _parse_chips(token)
        if val > 0:
            return val
    return 0


async def get_my_stack(page: Page) -> int:
    el = page.locator(SEL_MY_STACK)
    if await el.count() == 0:
        return 0
    return _parse_chips(await el.inner_text(timeout=1000))


async def get_pot(page: Page) -> float:
    """
    Total pot from the main-value element.

    PokerNow previously split the display into main-value (closed streets) +
    add-on (current street). The current DOM has both pointing to the same total,
    so only main-value is used to avoid the 2× double-count bug.
    """
    main_el = page.locator(SEL_POT_MAIN)
    if await main_el.count() > 0:
        return _parse_chips(await main_el.first.inner_text(timeout=1000))
    return 0.0


async def get_phase(page: Page) -> str:
    """Infer street from how many community cards are visible."""
    count = await page.locator(SEL_BOARD_CARDS).count()
    return {0: "preflop", 3: "flop", 4: "turn", 5: "river"}.get(count, "preflop")


def _seat_num(classes: str) -> Optional[int]:
    """Extract the seat number from 'table-player-N' in a class string."""
    for cls in classes.split():
        if cls.startswith("table-player-") and cls != "table-player-seat":
            try:
                return int(cls.split("-")[-1])
            except ValueError:
                pass
    return None


async def get_position(page: Page) -> str:
    """
    Determine our seat position relative to the dealer button.

    pokernow marks the dealer with a floating .dealer-button-ctn element that
    has a class 'dealer-position-N', where N matches the 'table-player-N'
    class on the corresponding player seat. Seat numbers are sorted to derive
    clockwise order; offset from dealer → position name.

    Returns 'unknown' if detection fails (e.g. between hands, animations).
    """
    # 1. Find which seat number has the dealer button
    dealer_el = page.locator(SEL_DEALER_BTN)
    if await dealer_el.count() == 0:
        return "unknown"
    dealer_classes = await dealer_el.first.get_attribute("class") or ""
    dealer_seat_num = None
    for cls in dealer_classes.split():
        if cls.startswith("dealer-position-"):
            try:
                dealer_seat_num = int(cls.split("-")[-1])
            except ValueError:
                pass
    if dealer_seat_num is None:
        return "unknown"

    # 2. Find our seat number
    our_el = page.locator(".table-player.you-player")
    if await our_el.count() == 0:
        return "unknown"
    our_seat_num = _seat_num(await our_el.first.get_attribute("class") or "")
    if our_seat_num is None:
        return "unknown"

    # 3. Collect all active seat numbers; sorted order = clockwise around table
    all_seats = page.locator(".table-player:not(.table-player-seat)")
    seat_nums = []
    for i in range(await all_seats.count()):
        n = _seat_num(await all_seats.nth(i).get_attribute("class") or "")
        if n is not None:
            seat_nums.append(n)
    seat_nums = sorted(set(seat_nums))
    total = len(seat_nums)
    if total == 0:
        return "unknown"

    # 4. Clockwise offset from dealer to us
    try:
        offset = (seat_nums.index(our_seat_num) - seat_nums.index(dealer_seat_num)) % total
    except ValueError:
        return "unknown"

    # 5. Map offset to position name
    if total <= 2:
        return "BTN" if offset == 0 else "BB"
    pos = {0: "BTN", 1: "SB", 2: "BB"}
    if total >= 6:
        pos.update({3: "UTG", 4: "HJ", 5: "CO"})
    elif total == 5:
        pos.update({3: "UTG", 4: "CO"})
    elif total == 4:
        pos[3] = "CO"
    return pos.get(offset, "MP")


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
            position=await get_position(page),
        )
    except Exception as e:
        print(f"[scraper] error reading state: {e}")
        return None
