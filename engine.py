"""
Poker decision engine.

Preflop:  Chen formula scores hole cards on a 0-1 scale.
Postflop: Range-weighted Monte Carlo equity — instead of dealing opponents
          random hands, we sample from an inferred range (e.g. "tight" if
          they made a large raise preflop). This gives a much more accurate
          equity estimate than pure random sampling.

Tuning all decision thresholds: see the THRESHOLDS section below.
"""
import random
from treys import Card as TreysCard, Evaluator

from state import Card, GameState
from ranges import get_combos

_evaluator = Evaluator()


# ---------------------------------------------------------------------------
# THRESHOLDS — edit these to change how aggressive the bot plays
# ---------------------------------------------------------------------------

# Preflop (Chen score, 0-1 scale)
PREFLOP_RAISE_THRESHOLD = 0.55   # raise if Chen >= this (AA/KK/QQ/AK/AQs etc.)
PREFLOP_CALL_THRESHOLD  = 0.30   # call if Chen >= this, else fold

# Postflop (Monte Carlo equity, 0-1 scale)
POSTFLOP_RAISE_THRESHOLD = 0.60  # raise if equity >= this
POSTFLOP_CALL_EDGE       = 0.05  # call if equity > pot_odds + this margin

# Raise sizing
PREFLOP_RAISE_BB_MULT = 3        # open-raise to N × big blind preflop
POSTFLOP_BET_FRACTION = 0.75     # bet this fraction of pot on postflop raises

# Monte Carlo simulation count — lower = faster, higher = more accurate
MONTE_CARLO_SIMS = 1500
# ---------------------------------------------------------------------------


def _to_treys(card: Card) -> int:
    return TreysCard.new(card.to_treys())


def _build_deck() -> list[int]:
    ranks = "23456789TJQKA"
    suits = "shdc"
    return [TreysCard.new(r + s) for r in ranks for s in suits]


def monte_carlo_equity(
    hole_cards: list[Card],
    board_cards: list[Card],
    num_opponents: int,
    opponent_tier: str = "random",
    num_sims: int = MONTE_CARLO_SIMS,
) -> float:
    """
    Estimate win probability by simulation against a ranged opponent.

    opponent_tier: one of "premium", "tight", "medium", "wide", "random".
      Controls which hands the opponent is assumed to hold. A tighter tier
      means the opponent has stronger hands on average, which lowers our
      equity estimate and makes the bot play more conservatively.

    For each trial:
      1. Sample an opponent hand from the filtered range (no card conflicts).
      2. Fill remaining board cards randomly from the leftover deck.
      3. Evaluate all hands with treys. Ties count as half a win.

    Falls back to 0.5 if no valid trials could be run.
    """
    our_treys   = [_to_treys(c) for c in hole_cards]
    board_treys = [_to_treys(c) for c in board_cards]
    known       = set(our_treys + board_treys)

    # Opponent hand pool filtered against known cards
    opp_pool = get_combos(opponent_tier, known)
    if not opp_pool:
        opp_pool = get_combos("random", known)   # safety fallback

    # Remaining deck for board runouts (excludes known cards AND opponent hands
    # — we re-filter per trial below)
    full_deck = [c for c in _build_deck() if c not in known]

    wins  = 0
    valid = 0

    for _ in range(num_sims):
        # Pick a random opponent hand from the range
        opp_hand = list(random.choice(opp_pool))

        # Build the runout deck: full deck minus the opponent's two cards
        opp_set  = set(opp_hand)
        run_deck = [c for c in full_deck if c not in opp_set]

        need = 5 - len(board_cards)
        if len(run_deck) < need:
            continue

        random.shuffle(run_deck)
        sim_board = board_treys + run_deck[:need]

        our_score = _evaluator.evaluate(sim_board, our_treys)
        opp_score = _evaluator.evaluate(sim_board, opp_hand)

        # treys scores: lower is better (1 = Royal Flush, 7462 = worst)
        if our_score < opp_score:
            wins += 1
        elif our_score == opp_score:
            wins += 0.5   # chop

        valid += 1

    return wins / valid if valid else 0.5


# ---------- Chen formula (preflop only) ----------

_RANK_ORDER = "23456789TJQKA"

def _chen_score(hole_cards: list[Card]) -> float:
    """
    Scores a two-card starting hand using the Chen formula, normalised to [0, 1].

    The raw Chen formula assigns integer points based on:
      - Highest card rank (A=10, K=8, Q=7, J=6, T=5, others = rank/2)
      - Pair bonus (double base score, minimum 5)
      - Suited bonus (+2)
      - Connector / gap penalty (0 gap = +1, 1 gap = -1, 2 = -2, 3 = -4, 4+ = -5)
      - Low connector bonus (both cards < 9, gap <= 1: +1)

    Max raw score ≈ 20 (AA), so we divide by 20 to get a 0-1 scale.
    """
    c1, c2 = hole_cards
    r1 = _RANK_ORDER.index(c1.rank)
    r2 = _RANK_ORDER.index(c2.rank)
    if r1 < r2:
        r1, r2 = r2, r1   # ensure r1 is the higher rank index

    base = {12: 10, 11: 8, 10: 7, 9: 6, 8: 5}.get(r1, r1 / 2 + 1)

    if r1 == r2:
        score = max(base * 2, 5)
    else:
        score = base
        gap = r1 - r2 - 1
        if c1.suit == c2.suit:
            score += 2
        score += {0: 1, 1: -1, 2: -2, 3: -4}.get(gap, -5)
        if r1 < 9 and gap <= 1:
            score += 1

    return min(max(score / 20.0, 0.0), 1.0)


# ---------- decision ----------

def decide(state: GameState, opponent_tier: str = "random") -> tuple[str, int]:
    """
    Evaluate the current game state and return an action.

    opponent_tier: range tier inferred by HandTracker — passed through to
      monte_carlo_equity so simulations use a realistic opponent hand pool.

    Returns one of:
      ('fold',  0)
      ('check', 0)
      ('call',  to_call_amount)
      ('raise', raise_to_amount)
    """
    bb = state.big_blind if state.big_blind > 0 else 0.01

    if state.phase == "preflop":
        equity           = _chen_score(state.hole_cards)
        strong_threshold = PREFLOP_RAISE_THRESHOLD
        call_threshold   = PREFLOP_CALL_THRESHOLD
    else:
        equity           = monte_carlo_equity(
            state.hole_cards, state.board_cards,
            state.num_opponents, opponent_tier=opponent_tier,
        )
        strong_threshold = POSTFLOP_RAISE_THRESHOLD
        call_threshold   = state.pot_odds() + POSTFLOP_CALL_EDGE

    print(
        f"  equity={equity:.2f}  pot_odds={state.pot_odds():.2f}  "
        f"range={opponent_tier}  raise>={strong_threshold:.2f}  call>={call_threshold:.2f}"
    )

    if equity >= strong_threshold:
        if state.phase == "preflop":
            raise_to = bb * PREFLOP_RAISE_BB_MULT
        else:
            raise_to = max(round(state.pot * POSTFLOP_BET_FRACTION, 2), bb * 2)
        # Poker rule: must raise to at least 2× the current bet (min-raise)
        min_legal_raise = state.to_call * 2 if state.to_call > 0 else bb * 2
        raise_to = max(raise_to, min_legal_raise)
        raise_to = round(min(raise_to, state.my_stack), 2)
        return ("raise", raise_to)

    if equity >= call_threshold:
        return ("check", 0) if state.can_check else ("call", state.to_call)

    return ("check", 0) if state.can_check else ("fold", 0)
