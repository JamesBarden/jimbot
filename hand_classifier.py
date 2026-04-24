"""
Classifies hole cards relative to the current board into a hand class string.

Hand classes (strongest to weakest):
  monster        Straight flush, four of a kind
  full_house     Full house where our hole cards contributed (board not already a FH)
  flush          Made flush using both hole cards (2-card flush)
  straight       Made straight using both hole cards (2-card straight)
  set            Three of a kind — pocket pair + one board card (well disguised)
  trips          Three of a kind — one hole card + board pair (less disguised)
  two_pair       Two pair; also 1-card flush or 1-card straight (board did most work)
  overpair       Pocket pair above all board cards
  top_pair_top   Top pair, top kicker (kicker beats all other board ranks)
  top_pair_weak  Top pair, weak kicker
  middle_pair    Pair with second-highest board card
  bottom_pair    Pair with lowest board card
  combo_draw     12+ outs (e.g. flush draw + open-ended straight draw)
  draw           8-9 outs (flush draw or open-ended straight draw)
  weak_draw      4 outs (gutshot only)
  air            Overcards, no meaningful draw; also board trips/straight/flush/FH
                 (board already provides the hand — hole cards add nothing unique)
"""

from treys import Card as TreysCard, Evaluator
from state import Card

_ev = Evaluator()
_RANKS = "23456789TJQKA"


def _t(c: Card) -> int:
    return TreysCard.new(c.to_treys())


def classify(hole_cards: list[Card], board_cards: list[Card]) -> str:
    """Return hand class string given hole cards and 3–5 board cards."""
    if len(board_cards) < 3:
        return "preflop"

    score = _ev.evaluate([_t(c) for c in board_cards], [_t(c) for c in hole_cards])
    cat = _ev.get_rank_class(score)

    if cat == 1:  return "monster"     # straight flush
    if cat == 2:  return "monster"     # four of a kind
    if cat == 3:  return _classify_full_house(hole_cards, board_cards)
    if cat == 4:  return _classify_flush(hole_cards, board_cards)
    if cat == 5:  return _classify_straight(hole_cards, board_cards)
    if cat == 6:  return _classify_set(hole_cards, board_cards)
    if cat == 7:  return "two_pair"
    if cat == 8:  return _classify_pair(hole_cards, board_cards)
    return _classify_air(hole_cards, board_cards)


# ── pair classification ──────────────────────────────────────────────────────

def _classify_pair(hole: list[Card], board: list[Card]) -> str:
    ri = _RANKS.index
    h = [c.rank for c in hole]
    b = [c.rank for c in board]
    board_sorted = sorted((ri(r) for r in b), reverse=True)

    # Pocket pair
    if h[0] == h[1]:
        pp = ri(h[0])
        if pp > board_sorted[0]:   # above ALL board cards — true overpair
            return "overpair"
        # One or more overcards: no longer an overpair regardless of how close.
        # (KK on A-x-y, JJ on Q-x-y, etc. all get middle/bottom pair treatment.)
        if pp > board_sorted[2]:
            return "middle_pair"
        return "bottom_pair"

    # Paired a board card
    for i, hc in enumerate(hole):
        if hc.rank in b:
            hit = ri(hc.rank)
            other = hole[1 - i]
            kicker = ri(other.rank)
            if hit == board_sorted[0]:          # top pair
                non_top = [x for x in board_sorted if x != hit]
                if all(kicker > x for x in non_top):
                    return "top_pair_top"
                return "top_pair_weak"
            if hit == board_sorted[1]:
                return "middle_pair"
            return "bottom_pair"

    return "air"


# ── set / trips / board-trips ─────────────────────────────────────────────────

def _classify_set(hole: list[Card], board: list[Card]) -> str:
    """
    Distinguish three cases when the evaluator finds three-of-a-kind:
      set   — pocket pair + one matching board card (well disguised)
      trips — one hole card matches a board pair (less disguised, opponents see the pair)
      air   — board itself holds all three copies (hole cards add nothing unique)
    """
    all_cards = hole + board
    rank_counts: dict = {}
    for c in all_cards:
        rank_counts[c.rank] = rank_counts.get(c.rank, 0) + 1
    trips_rank = next((r for r, cnt in rank_counts.items() if cnt >= 3), None)
    if trips_rank is None:
        return "set"
    hole_matches = sum(1 for c in hole if c.rank == trips_rank)
    if hole_matches == 2:  return "set"
    if hole_matches == 1:  return "trips"
    return "air"


# ── full house ────────────────────────────────────────────────────────────────

def _classify_full_house(hole: list[Card], board: list[Card]) -> str:
    """
    When the 5-card board itself is already a full house (trips + pair on board),
    everyone plays that same holding — classify as air.
    Otherwise our hole cards genuinely contributed.
    """
    if len(board) == 5:
        bc: dict = {}
        for c in board:
            bc[c.rank] = bc.get(c.rank, 0) + 1
        trips = [r for r, n in bc.items() if n >= 3]
        if trips:
            pairs = [r for r, n in bc.items() if n >= 2 and r != trips[0]]
            if pairs:
                return "air"
    return "full_house"


# ── straight ──────────────────────────────────────────────────────────────────

def _classify_straight(hole: list[Card], board: list[Card]) -> str:
    """
    Count how many hole cards fall in the best (highest) straight window.
      0 — board straight: everyone plays it → air
      1 — one-card completion of a 4-board straight → two_pair (made but weak)
      2 — both hole cards contribute → straight
    """
    all_ridx = {_RANKS.index(c.rank) for c in hole + board}
    if 12 in all_ridx:
        all_ridx = all_ridx | {-1}
    hole_ridx = {_RANKS.index(c.rank) for c in hole}
    hole_alow = hole_ridx | ({-1} if 12 in hole_ridx else set())

    for lo in range(8, -2, -1):          # highest window first
        window = set(range(lo, lo + 5))
        if len(window & all_ridx) >= 5:
            contrib = len(window & hole_alow)
            if contrib == 0:  return "air"
            if contrib == 1:  return "two_pair"
            return "straight"
    return "straight"


# ── flush ─────────────────────────────────────────────────────────────────────

def _classify_flush(hole: list[Card], board: list[Card]) -> str:
    """
    Count how many hole cards are of the flush suit.
      0 — board flush: everyone plays it → air
      1 — one-card flush (board provides 4 of the suit) → two_pair (vulnerable)
      2 — two-card flush → flush
    """
    sc: dict = {}
    for c in hole + board:
        sc[c.suit] = sc.get(c.suit, 0) + 1
    flush_suit = max(sc, key=sc.get)
    hole_count = sum(1 for c in hole if c.suit == flush_suit)
    if hole_count == 0:  return "air"
    if hole_count == 1:  return "two_pair"
    return "flush"


# ── draw / air classification ────────────────────────────────────────────────

def _has_flush_draw(hole: list[Card], board: list[Card]) -> bool:
    for suit in "shdc":
        in_hole  = sum(1 for c in hole  if c.suit == suit)
        in_board = sum(1 for c in board if c.suit == suit)
        if in_hole >= 1 and in_hole + in_board >= 4:
            return True
    return False


def _straight_draw_outs(hole: list[Card], board: list[Card]) -> int:
    """
    Return total straight-draw outs across ALL valid windows.

    Counts unique missing ranks — so a double-ended draw like KQJT correctly
    returns 8 (4 aces + 4 nines), not just 4.  Wheel draws (A-low) included.
    """
    all_ranks_set = {_RANKS.index(c.rank) for c in hole + board}
    hole_ranks    = {_RANKS.index(c.rank) for c in hole}

    # A can act as low (-1) for wheel draws
    if 12 in all_ranks_set:
        all_ranks_set = all_ranks_set | {-1}
    hole_with_alow = hole_ranks | ({-1} if 12 in hole_ranks else set())

    missing_ranks: set[int] = set()
    for lo in range(-1, 9):
        window  = set(range(lo, lo + 5))
        missing = window - all_ranks_set
        if len(missing) != 1:
            continue
        if not (hole_with_alow & window):
            continue
        m      = next(iter(missing))
        actual = 12 if m == -1 else m        # -1 encodes A-low
        missing_ranks.add(actual)

    total = 0
    for rank in missing_ranks:
        total += 4 - sum(1 for c in board if _RANKS.index(c.rank) == rank)
    return total


def _classify_air(hole: list[Card], board: list[Card]) -> str:
    flush  = _has_flush_draw(hole, board)
    s_outs = _straight_draw_outs(hole, board)

    if flush and s_outs >= 8:  return "combo_draw"
    if flush or  s_outs >= 8:  return "draw"
    if s_outs >= 4:            return "weak_draw"
    return "air"


# ── board texture ────────────────────────────────────────────────────────────

def board_texture(board_cards: list[Card]) -> str:
    """
    Classify the first three board cards into one of several texture strings.

    Format: {paired|connected|dry}_{rainbow|twotone|monotone}_{high|mid|low}

    Examples:
      A72r  → dry_rainbow_high
      JT9r  → connected_rainbow_high
      752h2 → paired_rainbow_mid   (ignore 4th/5th card)
      Kh9h4h → dry_monotone_high
    """
    flop = board_cards[:3]
    ranks = [c.rank for c in flop]
    suits = [c.suit for c in flop]
    ri = _RANKS.index
    rank_idx = sorted((ri(r) for r in ranks), reverse=True)

    # Flush texture
    unique_suits = len(set(suits))
    if   unique_suits == 1: flush_t = "monotone"
    elif unique_suits == 2: flush_t = "twotone"
    else:                   flush_t = "rainbow"

    # Paired / trips
    unique_ranks = len(set(ranks))
    if unique_ranks < 3:
        height_t = "high" if rank_idx[0] >= _RANKS.index("T") else \
                   "mid"  if rank_idx[0] >= _RANKS.index("6") else "low"
        return f"paired_{flush_t}_{height_t}"

    # Connectivity: span of top to bottom rank
    span = rank_idx[0] - rank_idx[2]
    conn_t = "connected" if span <= 4 else "dry"

    # Height based on top card
    top = rank_idx[0]
    height_t = "high" if top >= _RANKS.index("T") else \
               "mid"  if top >= _RANKS.index("6") else "low"

    return f"{conn_t}_{flush_t}_{height_t}"


def river_card_texture(board_cards: list[Card]) -> str:
    """
    Classify the river card (board_cards[4]) relative to the 4-card board (board_cards[:4]).

    Returns one of:
      'flush_complete'    — river is the 4th or 5th card of one suit (flush now possible)
      'straight_complete' — river fills a 4-card straight draw into a made straight
      'trips_board'       — river pairs a card that was already paired on the turn board
      'pair_board'        — river pairs one of the unpaired board cards
      'overcard'          — river card ranks above all four prior board cards
      'blank'             — low-impact card
    """
    if len(board_cards) < 5:
        return "blank"

    prior  = board_cards[:4]
    river  = board_cards[4]
    ri     = _RANKS.index

    prior_ranks = [c.rank for c in prior]
    prior_ridx  = [ri(r) for r in prior_ranks]
    river_ridx  = ri(river.rank)

    # Trips board: river pairs a rank that already appeared twice in prior 4 cards
    rank_counts = {}
    for r in prior_ranks:
        rank_counts[r] = rank_counts.get(r, 0) + 1
    if rank_counts.get(river.rank, 0) >= 2:
        return "trips_board"

    # Pair board: river pairs any card in the prior 4
    if river.rank in prior_ranks:
        return "pair_board"

    # Flush complete: 3 or 4 of one suit in prior board + river is the same suit
    prior_suit_counts: dict = {}
    for c in prior:
        prior_suit_counts[c.suit] = prior_suit_counts.get(c.suit, 0) + 1
    if prior_suit_counts.get(river.suit, 0) >= 3:
        return "flush_complete"

    # Straight complete: river fills a 4-card draw to a straight
    all_ridx = set(prior_ridx) | {river_ridx}
    if 12 in all_ridx:
        all_ridx = all_ridx | {-1}
    prior_set = set(prior_ridx)
    if 12 in prior_set:
        prior_set = prior_set | {-1}
    for lo in range(-1, 9):
        window = set(range(lo, lo + 5))
        hits_total = len(window & all_ridx)
        hits_prior = len(window & prior_set)
        if hits_total >= 5 and hits_prior == 4 and (river_ridx in window or (river_ridx == 12 and -1 in window)):
            return "straight_complete"

    # Overcard: river ranks above all four prior cards
    if river_ridx > max(prior_ridx):
        return "overcard"

    return "blank"


def turn_card_texture(board_cards: list[Card]) -> str:
    """
    Classify the turn card (board_cards[3]) relative to the flop (board_cards[:3]).

    Returns one of:
      'flush_complete'    — third suited card arrives (flop had two of that suit)
      'straight_complete' — turn fills a 3-card straight draw into 4+ connected ranks
      'pair_board'        — turn card pairs one of the three flop cards
      'overcard'          — turn card ranks above all three flop cards
      'blank'             — none of the above (low-impact card)
    """
    if len(board_cards) < 4:
        return "blank"

    flop  = board_cards[:3]
    turn  = board_cards[3]
    ri    = _RANKS.index

    flop_ranks = [c.rank for c in flop]
    flop_ridx  = {ri(r) for r in flop_ranks}
    turn_ridx  = ri(turn.rank)

    # Pair board: turn pairs a flop card
    if turn.rank in flop_ranks:
        return "pair_board"

    # Flush complete: flop had exactly 2 of one suit, turn is the third
    flop_suit_counts: dict = {}
    for c in flop:
        flop_suit_counts[c.suit] = flop_suit_counts.get(c.suit, 0) + 1
    if flop_suit_counts.get(turn.suit, 0) == 2:
        return "flush_complete"

    # Straight complete: turn card creates a 4-card (or better) straight
    # where the flop contributed exactly 3 of those cards
    all_ridx = flop_ridx | {turn_ridx}
    # A-low wheel: treat A as -1
    if 12 in all_ridx:
        all_ridx = all_ridx | {-1}
    for lo in range(-1, 9):
        window = set(range(lo, lo + 5))
        hits_total = len(window & all_ridx)
        hits_flop  = len(window & (flop_ridx | ({-1} if 12 in flop_ridx else set())))
        # Turn fills a 3-card draw into 4+ connected
        if hits_total >= 4 and hits_flop == 3 and (turn_ridx in window or (turn_ridx == 12 and -1 in window)):
            return "straight_complete"

    # Overcard: turn is higher than every flop card
    if turn_ridx > max(ri(r) for r in flop_ranks):
        return "overcard"

    return "blank"
