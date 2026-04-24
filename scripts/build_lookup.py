"""
Parse raw TexasSolver JSON outputs and build the runtime lookup table.

TexasSolver JSON structure (root = OOP acts first on flop):
  root (player=1, OOP)
    → children["CHECK"] (player=0, IP)   ← IP bets or checks when OOP checked
    → children["BET X"] (player=0, IP)   ← IP calls/raises/folds when OOP bet

For each spot this script extracts TWO strategy tables:
  facing_check  — IP's bet/check frequencies (when no bet is facing)
  facing_bet    — IP's call/raise/fold frequencies (when a bet is facing)

Combos in the JSON are 4-char strings: rank+suit+rank+suit  e.g. "Ac3c", "KhQd"
Strategy values are [p_action0, p_action1, ...] for each combo.

Output: solutions/lookup.pkl

Run after:  python3 scripts/presolve.py
"""

import json
import os
import pickle
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from state import Card
from hand_classifier import classify
from scripts.presolve import BOARDS


# ── Combo parsing ─────────────────────────────────────────────────────────────

def _parse_combo(s: str) -> tuple:
    """'Ac3c' → (Card('A','c'), Card('3','c'))"""
    return Card(rank=s[0], suit=s[1]), Card(rank=s[2], suit=s[3])


def _board_from_key(board_key: str) -> list:
    """Look up the board string for a texture key and parse it into Card list."""
    board_str = BOARDS.get(board_key)
    if board_str is None:
        return []
    cards = []
    for token in board_str.split(","):
        token = token.strip()
        if len(token) >= 2:
            cards.append(Card(rank=token[:-1].upper(), suit=token[-1].lower()))
    return cards


# ── Action categorisation ─────────────────────────────────────────────────────

def _cat(action: str) -> str:
    a = action.upper()
    if a.startswith("BET") or a.startswith("RAISE") or a.startswith("ALLIN"):
        return "raise"
    if a.startswith("CALL") or a.startswith("CHECK"):
        return "call"
    if a.startswith("FOLD"):
        return "fold"
    return "other"


# ── Strategy extraction ───────────────────────────────────────────────────────

def _extract_class_freqs(
    strat_dict: dict,
    actions: list,
    board_cards: list,
) -> dict:
    """
    Given a TexasSolver strategy dict {combo_str: [p_action, ...]},
    classify each combo by hand class and average (raise, call, fold) per class.

    Returns {hand_class: (raise_freq, call_freq, fold_freq)}.
    """
    categories = [_cat(a) for a in actions]
    board_set = {c.rank + c.suit for c in board_cards}

    buckets: dict[str, list] = {}

    for combo_str, probs in strat_dict.items():
        if len(combo_str) != 4:
            continue
        # Skip combos that conflict with board cards
        c1_str, c2_str = combo_str[:2], combo_str[2:]
        if c1_str in board_set or c2_str in board_set:
            continue

        c1, c2 = _parse_combo(combo_str)
        hand_class = classify([c1, c2], board_cards)

        raise_f = call_f = fold_f = 0.0
        for cat, p in zip(categories, probs):
            if   cat == "raise": raise_f += p
            elif cat == "call":  call_f  += p
            elif cat == "fold":  fold_f  += p

        total = raise_f + call_f + fold_f
        if total > 0:
            raise_f /= total
            call_f  /= total
            fold_f  /= total

        buckets.setdefault(hand_class, []).append((raise_f, call_f, fold_f))

    result = {}
    for hc, entries in buckets.items():
        n = len(entries)
        result[hc] = (
            sum(e[0] for e in entries) / n,
            sum(e[1] for e in entries) / n,
            sum(e[2] for e in entries) / n,
        )
    return result


def _avg_dicts(dicts: list) -> dict:
    """Average a list of {hand_class: (r,c,f)} dicts into one."""
    combined: dict[str, list] = {}
    for d in dicts:
        for hc, triple in d.items():
            combined.setdefault(hc, []).append(triple)
    result = {}
    for hc, triples in combined.items():
        n = len(triples)
        result[hc] = (
            sum(t[0] for t in triples) / n,
            sum(t[1] for t in triples) / n,
            sum(t[2] for t in triples) / n,
        )
    return result


# ── Spot ID parsing ───────────────────────────────────────────────────────────
# Filename: {board_texture}__{spr_bucket}__{villain_tier}.json

def _parse_spot_id(filename: str) -> Optional[tuple]:
    parts = filename.replace(".json", "").split("__")
    return tuple(parts) if len(parts) == 3 else None


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    script_dir  = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    raw_dir     = os.path.join(project_dir, "solutions", "raw")
    out_path    = os.path.join(project_dir, "solutions", "lookup.pkl")

    json_files = [f for f in os.listdir(raw_dir) if f.endswith(".json")]
    if not json_files:
        print(f"No JSON files in {raw_dir}. Run  python3 scripts/presolve.py  first.")
        sys.exit(1)

    # lookup[(board_texture, spr_bucket, villain_tier, facing_bet)]
    #   → {hand_class: (raise_freq, call_freq, fold_freq)}
    lookup: dict = {}
    errors = 0

    for filename in sorted(json_files):
        spot = _parse_spot_id(filename)
        if spot is None:
            print(f"SKIP (bad filename): {filename}")
            continue

        board_texture, spr_bucket, villain_tier = spot
        board_cards = _board_from_key(board_texture)
        if not board_cards:
            print(f"SKIP (unknown board texture key): {filename}")
            continue

        path = os.path.join(raw_dir, filename)
        print(f"Processing {filename} ...", end=" ", flush=True)

        try:
            with open(path) as f:
                tree = json.load(f)
        except json.JSONDecodeError as e:
            print(f"JSON error: {e}")
            errors += 1
            continue

        # Validate root is OOP (player 1)
        if tree.get("player") != 1:
            print(f"unexpected root player={tree.get('player')} — expected 1 (OOP)")
            errors += 1
            continue

        children = tree.get("childrens", {})

        # ── facing_check: IP strategy when OOP checked ────────────────────
        facing_check_strats = []
        if "CHECK" in children:
            node = children["CHECK"]
            actions = node.get("strategy", {}).get("actions", [])
            strat   = node.get("strategy", {}).get("strategy", {})
            if strat:
                facing_check_strats.append(_extract_class_freqs(strat, actions, board_cards))

        # ── facing_bet: IP strategy when OOP bet (average all bet sizes) ──
        facing_bet_strats = []
        for action_key, node in children.items():
            if not action_key.upper().startswith("BET"):
                continue
            actions = node.get("strategy", {}).get("actions", [])
            strat   = node.get("strategy", {}).get("strategy", {})
            if strat:
                facing_bet_strats.append(_extract_class_freqs(strat, actions, board_cards))

        if not facing_check_strats and not facing_bet_strats:
            print("no IP strategy nodes found — run inspect_output.py to debug")
            errors += 1
            continue

        # Store entries
        counts = []
        if facing_check_strats:
            d = _avg_dicts(facing_check_strats)
            lookup[(board_texture, spr_bucket, villain_tier, False)] = d
            counts.append(f"check:{len(d)}")
        if facing_bet_strats:
            d = _avg_dicts(facing_bet_strats)
            lookup[(board_texture, spr_bucket, villain_tier, True)] = d
            counts.append(f"bet:{len(d)}")

        print(f"ok  ({', '.join(counts)} hand classes)")

    with open(out_path, "wb") as f:
        pickle.dump(lookup, f)

    print(f"\nLookup table written to {out_path}")
    print(f"  {len(lookup)} entries  |  {errors} errors")


if __name__ == "__main__":
    main()
