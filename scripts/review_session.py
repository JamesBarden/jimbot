#!/usr/bin/env python3
"""
Post-session review brief.

Reads the most recent decisions_*.jsonl + hands_*.csv from Logs/ and prints
a condensed report suitable for pasting into Claude Code (or any chat LLM)
to get targeted tuning advice on heuristic tables.

No network calls, no API keys — this is just log formatting.

Usage:
  python3 scripts/review_session.py                   # latest session in Logs/
  python3 scripts/review_session.py --session 2026-04-24_143022
  python3 scripts/review_session.py --top 20          # top N winning / losing hands
"""
# Defer annotation evaluation so `str | None` and `list[dict]` parse on py3.9.
from __future__ import annotations
import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from glob import glob

HERE    = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(os.path.dirname(HERE), "Logs")


def _find_session(stamp: str | None) -> tuple[str, str] | None:
    """Return (decisions_path, hands_path) for the requested stamp or the latest."""
    if stamp:
        dec = os.path.join(LOG_DIR, f"decisions_{stamp}.jsonl")
        hnd = os.path.join(LOG_DIR, f"hands_{stamp}.csv")
        if not (os.path.isfile(dec) and os.path.isfile(hnd)):
            return None
        return dec, hnd
    # latest: pick the newest decisions file, then match its stamp
    candidates = sorted(glob(os.path.join(LOG_DIR, "decisions_*.jsonl")))
    if not candidates:
        return None
    dec = candidates[-1]
    stamp = os.path.basename(dec)[len("decisions_"):-len(".jsonl")]
    hnd = os.path.join(LOG_DIR, f"hands_{stamp}.csv")
    if not os.path.isfile(hnd):
        hnd = ""  # tolerated: decisions but no finalized hands yet
    return dec, hnd


def _load_jsonl(path: str) -> list[dict]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _load_csv(path: str) -> list[dict]:
    if not path or not os.path.isfile(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def _fmt_hand_row(h: dict) -> str:
    return (f"[{h['hand_id']}]  {h['hole_cards']}  pos={h['position']}  "
            f"vs {h['villain_names']}  "
            f"reached={h['reached_street']}  "
            f"actions: pre={h['preflop_action']}/flop={h['flop_action']}"
            f"/turn={h['turn_action']}/river={h['river_action']}  "
            f"Δ={h['stack_delta']} ({h['bb_delta']}bb)")


def _fmt_decision(d: dict) -> str:
    src      = d.get("source", "?")
    phase    = d.get("phase", "?")
    hole     = d.get("hole", "??")
    board    = d.get("board", "") or "-"
    pot      = d.get("pot", 0)
    to_call  = d.get("to_call", 0)
    hc       = d.get("hand_class", "?")
    r        = d.get("raise_freq", 0) or 0
    c        = d.get("call_freq", 0)  or 0
    f        = d.get("fold_freq", 0)  or 0
    act      = d.get("action", "?")
    amt      = d.get("amount", 0) or 0
    ctx      = d.get("context") or {}
    pfa      = ctx.get("pfa") or "-"
    barrel   = ctx.get("double_barrel_spot", False)
    return (f"  [{phase.upper()}] {hole} on {board}  pot={pot} toCall={to_call}  "
            f"{hc} [{src}]  R={r:.0%}/C={c:.0%}/F={f:.0%}  → {act} {amt}  "
            f"(PFA={pfa}{' BARREL' if barrel else ''})")


# ── report sections ──────────────────────────────────────────────────────────

def _session_summary(hands: list[dict]) -> list[str]:
    out = ["SESSION SUMMARY"]
    if not hands:
        out.append("  (no finalized hands in this session)")
        return out
    n          = len(hands)
    deltas     = [float(h["stack_delta"] or 0) for h in hands]
    bb_deltas  = [float(h["bb_delta"]    or 0) for h in hands]
    showdowns  = sum(1 for h in hands if h.get("reached_street") == "river")
    wins       = sum(1 for d in deltas if d > 0)
    total_bb   = sum(bb_deltas)
    out.append(f"  hands={n}  bb/100={total_bb/n*100:+.1f}  "
               f"wtsd={showdowns/n:.0%}  won={wins/n:.0%}  "
               f"total_bb={total_bb:+.1f}")
    if hands:
        best  = max(hands, key=lambda h: float(h["bb_delta"] or 0))
        worst = min(hands, key=lambda h: float(h["bb_delta"] or 0))
        out.append(f"  biggest win:  {best['bb_delta']}bb  "
                   f"({best['hole_cards']} vs {best['villain_names']})")
        out.append(f"  biggest loss: {worst['bb_delta']}bb  "
                   f"({worst['hole_cards']} vs {worst['villain_names']})")
    return out


def _decision_breakdown(decisions: list[dict]) -> list[str]:
    out = ["DECISIONS BY SOURCE"]
    if not decisions:
        out.append("  (no decisions logged)")
        return out
    counts = Counter(d.get("source", "?") for d in decisions)
    total  = len(decisions)
    for src, cnt in counts.most_common():
        out.append(f"  {src:<20} {cnt:>5}  ({cnt/total:.0%})")
    return out


def _top_hands(hands: list[dict], decisions: list[dict], top_n: int,
               reverse: bool, label: str) -> list[str]:
    out = [label]
    if not hands:
        return out + ["  (no hands)"]
    dec_by_hand: dict[str, list[dict]] = defaultdict(list)
    for d in decisions:
        hid = d.get("hand_id")
        if hid:
            dec_by_hand[hid].append(d)
    sorted_hands = sorted(hands,
                          key=lambda h: float(h.get("bb_delta") or 0),
                          reverse=reverse)
    for h in sorted_hands[:top_n]:
        out.append(_fmt_hand_row(h))
        for d in dec_by_hand.get(h["hand_id"], []):
            out.append(_fmt_decision(d))
        out.append("")
    return out


def _unusual_decisions(decisions: list[dict], top_n: int = 15) -> list[str]:
    """Flag decisions where the roll was near a boundary, solver missed, or
    the bot took an action against a very strong heuristic lean (>80%)."""
    out = ["UNUSUAL / COINFLIP DECISIONS (likely tuning targets)"]
    flagged: list[tuple[float, dict, str]] = []
    for d in decisions:
        reasons = []
        src  = d.get("source", "")
        roll = d.get("roll", 0) or 0
        r    = d.get("raise_freq", 0) or 0
        c    = d.get("call_freq",  0) or 0
        f    = d.get("fold_freq",  0) or 0
        act  = d.get("action", "")
        if src == "monte_carlo":
            reasons.append("MC fallback (solver miss)")
        # Strong lean but we rolled the minority action
        if r >= 0.70 and act != "raise":       reasons.append(f"R={r:.0%} but {act}")
        if f >= 0.80 and act != "fold":        reasons.append(f"F={f:.0%} but {act}")
        if c >= 0.70 and act not in ("call", "check"):
            reasons.append(f"C={c:.0%} but {act}")
        # Very close to a threshold — frequency tuning would flip the action
        max_freq = max(r, c, f)
        if max_freq < 0.45:
            reasons.append(f"coinflip (max freq {max_freq:.0%})")
        if reasons:
            # priority = how unusual; MC miss first, then extreme leans, then coinflips
            priority = (0 if "MC fallback" in reasons[0] else
                        1 if "but" in reasons[0] else 2) - max_freq
            flagged.append((priority, d, "; ".join(reasons)))
    flagged.sort(key=lambda x: x[0])
    if not flagged:
        out.append("  (none)")
        return out
    for _, d, why in flagged[:top_n]:
        out.append(f"  {why}")
        out.append(_fmt_decision(d))
        out.append("")
    return out


def _ask_claude() -> list[str]:
    return [
        "",
        "━" * 72,
        "ASK CLAUDE (paste from SESSION SUMMARY down to here):",
        "",
        "  Which of the highlighted decisions look like leaks?",
        "  For each leak, tell me exactly which parameter to tune and by how",
        "  much. Reference specific files and line-level table entries:",
        "    - preflop_ranges.py  (_RFI / _VS_RAISE)",
        "    - turn_heuristic.py  (_FREE / _FACED / _TIER_ADJ / _spr_adj)",
        "    - river_heuristic.py (same as turn + trips_board column)",
        "    - bet_sizing.py      (_BET_WEIGHTS / _RAISE_WEIGHTS / _WETNESS_MOD)",
        "    - engine.py          (POSTFLOP_RAISE_THRESHOLD, SPR_DEEP_THRESHOLD, jam guard)",
        "",
        "  Do NOT suggest structural rewrites — only concrete numeric changes",
        "  to existing tables or thresholds.",
        "━" * 72,
    ]


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", help="stamp like 2026-04-24_143022 (default: latest)")
    ap.add_argument("--top", type=int, default=10,
                    help="how many winning/losing hands to show (default 10)")
    args = ap.parse_args()

    found = _find_session(args.session)
    if not found:
        print(f"[review] no session found in {LOG_DIR}", file=sys.stderr)
        sys.exit(1)
    dec_path, hnd_path = found
    print(f"[review] decisions: {dec_path}")
    print(f"[review] hands:     {hnd_path or '(no hands.csv yet)'}")
    print()

    decisions = _load_jsonl(dec_path)
    hands     = _load_csv(hnd_path)

    for section in (
        _session_summary(hands),
        [""],
        _decision_breakdown(decisions),
        [""],
        _top_hands(hands, decisions, args.top, reverse=False, label="TOP LOSING HANDS (with linked decisions)"),
        _top_hands(hands, decisions, args.top, reverse=True,  label="TOP WINNING HANDS (with linked decisions)"),
        _unusual_decisions(decisions),
        _ask_claude(),
    ):
        print("\n".join(section))


if __name__ == "__main__":
    main()
