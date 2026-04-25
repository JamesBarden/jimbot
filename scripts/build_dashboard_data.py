#!/usr/bin/env python3
"""
Aggregate Logs/*.{jsonl,csv} → docs/data/metrics.json for the dashboard.

Reads every decisions_*.jsonl and hands_*.csv in the Logs/ directory, joins
them by session_id (filename stamp) + hand_id, anonymizes opponent names
via anonymize.anon(), rolls up per-session / per-version / per-street
metrics, and writes the result as a single JSON file the static dashboard
loads via fetch().

Run after each session (or manually) to refresh the dashboard data:

    python3 scripts/build_dashboard_data.py

Output: docs/data/metrics.json. Committed to the repo; powers the GH Pages site.
"""
from __future__ import annotations
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from glob import glob
from statistics import mean

HERE     = os.path.dirname(os.path.abspath(__file__))
ROOT     = os.path.dirname(HERE)
LOG_DIR  = os.path.join(ROOT, "Logs")
OUT_DIR  = os.path.join(ROOT, "docs", "data")
OUT_FP   = os.path.join(OUT_DIR, "metrics.json")

# Real opponent usernames are committed to the dashboard data; anonymization
# was removed so the dashboard can show actual handles. If you ever want
# anonymization back, re-import `from anonymize import anon` and wrap
# `_session_rollup` opponent counters in anon().


# ── file discovery ──────────────────────────────────────────────────────────

def _session_stamps() -> list[str]:
    """Return sorted list of session_id stamps that have BOTH jsonl + csv."""
    stamps = set()
    for fp in glob(os.path.join(LOG_DIR, "decisions_*.jsonl")):
        stamps.add(os.path.basename(fp)[len("decisions_"):-len(".jsonl")])
    valid = []
    for s in sorted(stamps):
        if os.path.isfile(os.path.join(LOG_DIR, f"hands_{s}.csv")):
            valid.append(s)
    return valid


def _load_session(stamp: str) -> tuple[list[dict], list[dict]]:
    dec_fp = os.path.join(LOG_DIR, f"decisions_{stamp}.jsonl")
    hnd_fp = os.path.join(LOG_DIR, f"hands_{stamp}.csv")
    decisions = []
    with open(dec_fp) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                decisions.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    with open(hnd_fp) as f:
        hands = list(csv.DictReader(f))
    return decisions, hands


# ── casting helpers (CSV fields arrive as strings) ──────────────────────────

def _f(v, default=0.0) -> float:
    try:    return float(v)
    except (ValueError, TypeError): return default

def _i(v, default=0) -> int:
    try:    return int(v)
    except (ValueError, TypeError):
        try: return int(float(v))
        except (ValueError, TypeError): return default


# ── per-session rollup ──────────────────────────────────────────────────────

def _parse_villain_names(raw: str) -> list[str]:
    if not raw:
        return []
    return [n for n in raw.split(",") if n]


def _action_from_summary(s: str) -> str:
    """Extract the first action token from a street summary like 'raise/check'."""
    if not s or s == "none":
        return "none"
    return s.split("/", 1)[0]


def _ledger_net_bb(stamp: str, hands: list[dict]) -> float | None:
    """
    Authoritative session P&L (in BB) from Logs/ledger_<stamp>.csv.

    The ledger CSV is the only source that captures hands the bot missed
    (mid-session reloads, busts on un-recorded hands, etc).  When it is
    present we use it instead of the sum-of-bb_delta approximation.

    Conversion: ledger values are in chips; hands.csv stack/bb values are
    in displayed dollars.  We derive chips-per-dollar from the bot's
    first recorded hand: ratio = ledger.buy_in / first_hand.stack_start
    (rounded to nearest int — typically 100 in cents-mode, 1 otherwise).
    Then bb_chips = bb_dollars * ratio, and net_bb = ledger.net / bb_chips.

    Returns None when:
      - no ledger CSV for this session
      - no recorded hands (can't derive the chip ratio)
      - jimbot row missing from the ledger
      - any field is unparseable
    """
    fp = os.path.join(LOG_DIR, f"ledger_{stamp}.csv")
    if not os.path.isfile(fp) or not hands:
        return None
    bb_dollars  = _f(hands[0].get("bb"))
    first_stack = _f(hands[0].get("stack_start"))
    if bb_dollars <= 0 or first_stack <= 0:
        return None
    try:
        with open(fp) as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return None
    bot = next((r for r in rows
                if (r.get("player_nickname") or "").strip().strip('"') == "jimbot"),
               None)
    if not bot:
        return None
    try:
        buy_in_chips = float(bot.get("buy_in") or 0)
        net_chips    = float(bot.get("net")    or 0)
    except ValueError:
        return None
    if buy_in_chips <= 0:
        return None
    # Snap to one of pokernow's two real ratios: 1 (no cents mode) or 100
    # (cents mode).  Naively rounding the raw ratio yields 101 for the
    # common case where first_stack_start is already post-SB (e.g. 9.95
    # instead of the true 10.00 buy-in), which shifts every result by ~1%.
    raw_ratio        = buy_in_chips / first_stack
    chips_per_dollar = 100 if raw_ratio > 10 else 1
    chips_per_bb     = bb_dollars * chips_per_dollar
    if chips_per_bb <= 0:
        return None
    return round(net_chips / chips_per_bb, 2)


def _effective_pnl(s: dict) -> float:
    """Use ledger P&L when present, otherwise the bb_delta-sum approximation."""
    v = s.get("ledger_net_bb")
    return v if v is not None else _f(s.get("session_pnl_bb"))


def _stack_change_bb(hands: list) -> float:
    """
    Final stack minus initial stack, in BB.

    This is an APPROXIMATION of session P&L.  It's wrong when:
      - The bot misses hand boundaries (stack jumps go uncounted)
      - The user rebuys mid-session (jumps look like wins)
    The pokernow ledger panel is the only authoritative source.  v1.6 will
    scrape it; until then, treat this number as "rough" and verify against
    the in-game ledger.
    """
    if not hands:
        return 0.0
    bb_val = _f(hands[0].get("bb"))
    if bb_val <= 0:
        return 0.0
    first = _f(hands[0].get("stack_start"))
    last  = _f(hands[-1].get("stack_end"))
    return (last - first) / bb_val


def _stack_jumps(hands: list) -> tuple:
    """
    Count untracked stack jumps between recorded hands.  Positive jump =
    likely a missed-win hand or a manual rebuy (we can't distinguish from
    state-delta alone).  Returns (total_jumps_bb, jump_count) — informational
    only, not used for P&L.
    """
    total = 0.0
    count = 0
    for i in range(1, len(hands)):
        prev_end  = _f(hands[i-1].get("stack_end"))
        cur_start = _f(hands[i].get("stack_start"))
        bb_val    = _f(hands[i].get("bb"))
        if bb_val <= 0:
            continue
        gap = cur_start - prev_end
        if abs(gap) > bb_val * 3:    # only count meaningful jumps
            total += gap / bb_val
            count += 1
    return total, count


def _session_rollup(stamp: str, decisions: list[dict], hands: list[dict]) -> dict:
    version = (hands[0]["version"] if hands else
               (decisions[0].get("version", "unknown") if decisions else "unknown"))
    date    = stamp[:10]

    # Per-hand counters for hero stats
    n        = len(hands)
    bb_total = sum(_f(h.get("bb_delta")) for h in hands)
    wins     = sum(1 for h in hands if _f(h.get("stack_delta")) > 0)
    wtsd     = sum(1 for h in hands if h.get("reached_street") == "river")
    flopped  = sum(1 for h in hands if h.get("reached_street") in ("flop", "turn", "river"))

    # Hero preflop activity — VPIP = non-fold preflop action; PFR = raise
    pre_actions   = [_action_from_summary(h.get("preflop_action", "")) for h in hands]
    hero_vpip     = sum(1 for a in pre_actions if a in ("call", "raise"))
    hero_pfr      = sum(1 for a in pre_actions if a == "raise")

    # C-bet proxy: was PFR and first flop action was 'raise' (= bet since no bet was facing)
    cbet_opps = 0
    cbets     = 0
    for h in hands:
        if _action_from_summary(h.get("preflop_action", "")) != "raise":
            continue   # only PFR hands get cbet opportunity
        if h.get("reached_street") not in ("flop", "turn", "river"):
            continue
        cbet_opps += 1
        if _action_from_summary(h.get("flop_action", "")) == "raise":
            cbets += 1

    # Decision-source counts (from JSONL)
    source_counts: Counter = Counter()
    phase_counts:  Counter = Counter()
    phase_action:  dict = defaultdict(lambda: Counter())  # phase → action Counter
    roll_values:   list  = []
    for d in decisions:
        source_counts[d.get("source", "?")] += 1
        ph = d.get("phase", "?")
        phase_counts[ph] += 1
        phase_action[ph][d.get("action", "?")] += 1
        roll_values.append(_f(d.get("roll")))

    # Opponent list — real usernames straight through
    opponents_seen = Counter()
    for h in hands:
        for name in _parse_villain_names(h.get("villain_names", "")):
            opponents_seen[name] += 1

    # Cumulative P&L series — one point per hand in order
    pnl = []
    running = 0.0
    for h in hands:
        running += _f(h.get("bb_delta"))
        pnl.append({"hand_id": h.get("hand_id"), "bb_delta": round(_f(h.get("bb_delta")), 3),
                    "cumulative": round(running, 3)})

    stack_change_bb       = _stack_change_bb(hands)
    untracked_jumps_bb, untracked_jumps_count = _stack_jumps(hands)
    ledger_bb             = _ledger_net_bb(stamp, hands)

    # Reconcile the cumulative chart against ledger truth.  The sum of
    # per-hand bb_delta misses entire un-recorded hands (mid-session
    # reloads, busts that didn't tick the loop) and so will not equal the
    # ledger NET.  Append a synthetic "untracked" point for the
    # difference so the chart's endpoint matches the headline number.
    if ledger_bb is not None:
        missing = ledger_bb - bb_total
        if abs(missing) > 0.5:
            running += missing
            pnl.append({"hand_id": "untracked", "bb_delta": round(missing, 3),
                        "cumulative": round(running, 3)})

    return {
        "session_id":       stamp,
        "date":             date,
        "version":          version,
        "hands":            n,
        # session_pnl_bb is the SUM of per-hand bb_deltas — chip flow from
        # recorded hands only.  Under-counts when the bot missed winning
        # hands and over-counts when it missed losing hands.  Kept on the
        # record for diagnostic comparison; ledger_net_bb (when present)
        # is the authoritative number the dashboard headlines use.
        "session_pnl_bb":   round(bb_total, 2),
        "ledger_net_bb":    ledger_bb,
        "bb_per_100":       round((ledger_bb if ledger_bb is not None else bb_total) / n * 100, 2) if n > 0 else 0,
        # Diagnostic only: stack-change view (last_end - first_start) and
        # the untracked-jump count.  When these differ from session_pnl_bb
        # by a lot, the bot missed hand boundaries this session — verify
        # against the ledger.
        "stack_change_bb":       round(stack_change_bb, 2),
        "untracked_jumps_bb":    round(untracked_jumps_bb, 2),
        "untracked_jumps_count": untracked_jumps_count,
        "winrate":          round(wins / n, 3) if n > 0 else 0,
        "wtsd":             round(wtsd / n, 3) if n > 0 else 0,
        "flopped_rate":     round(flopped / n, 3) if n > 0 else 0,
        "hero_vpip":        round(hero_vpip / n, 3) if n > 0 else 0,
        "hero_pfr":         round(hero_pfr / n, 3) if n > 0 else 0,
        "hero_cbet":        round(cbets / cbet_opps, 3) if cbet_opps else 0,
        "cbet_opps":        cbet_opps,
        "source_counts":    dict(source_counts),
        "phase_counts":     dict(phase_counts),
        "phase_action":     {p: dict(c) for p, c in phase_action.items()},
        "opponents":        dict(opponents_seen),
        "pnl_series":       pnl,
        "decisions_total":  len(decisions),
    }


# ── cross-session aggregation ───────────────────────────────────────────────

def _aggregate(sessions: list) -> dict:
    if not sessions:
        return {"sessions": [], "overall": {}, "versions": [], "generated_at": datetime.now().isoformat(timespec="seconds")}

    total_hands  = sum(s["hands"] for s in sessions)
    total_pnl    = sum(_effective_pnl(s) for s in sessions)
    winning_s    = sum(1 for s in sessions if _effective_pnl(s) > 0)
    losing_s     = sum(1 for s in sessions if _effective_pnl(s) < 0)
    ledgered_s   = sum(1 for s in sessions if s.get("ledger_net_bb") is not None)

    # Per-version rollup
    by_ver = defaultdict(list)
    for s in sessions:
        by_ver[s["version"]].append(s)
    versions = []
    for ver, ss in sorted(by_ver.items(), key=lambda kv: kv[0]):
        h_sum    = sum(s["hands"] for s in ss)
        pnl_sum  = sum(_effective_pnl(s) for s in ss)
        versions.append({
            "version":    ver,
            "sessions":   len(ss),
            "hands":      h_sum,
            "session_pnl_bb": round(pnl_sum, 2),
            "bb_per_100": round(pnl_sum / h_sum * 100, 2) if h_sum else 0,
            "winrate":    round(mean(s["winrate"] for s in ss), 3) if ss else 0,
            "wtsd":       round(mean(s["wtsd"]    for s in ss), 3) if ss else 0,
            "hero_vpip":  round(mean(s["hero_vpip"] for s in ss), 3) if ss else 0,
            "hero_pfr":   round(mean(s["hero_pfr"]  for s in ss), 3) if ss else 0,
            "hero_cbet":  round(mean(s["hero_cbet"] for s in ss), 3) if ss else 0,
        })

    # Flatten global source / phase / action counts across all sessions
    src_total:   Counter = Counter()
    phase_total: Counter = Counter()
    phase_act:   dict    = defaultdict(lambda: Counter())
    for s in sessions:
        src_total.update(s["source_counts"])
        phase_total.update(s["phase_counts"])
        for p, actions in s["phase_action"].items():
            phase_act[p].update(actions)

    # Top opponents across all sessions (anon keys)
    opps: Counter = Counter()
    for s in sessions:
        opps.update(s["opponents"])
    top_opps = [{"name": k, "hands": v} for k, v in opps.most_common(20)]

    overall = {
        "hands":           total_hands,
        # session_pnl_bb at this level is "best available P&L" — ledger
        # NET when we have it, otherwise the sum-of-bb_delta fallback.
        # The dashboard JS reads this field directly; ledgered_sessions
        # tells it how many sessions are ledger-verified.
        "session_pnl_bb":   round(total_pnl, 2),
        "bb_per_100":       round(total_pnl / total_hands * 100, 2) if total_hands else 0,
        "ledgered_sessions": ledgered_s,
        "sessions":        len(sessions),
        "winning_sessions": winning_s,
        "losing_sessions":  losing_s,
        "source_counts":   dict(src_total),
        "phase_counts":    dict(phase_total),
        "phase_action":    {p: dict(c) for p, c in phase_act.items()},
        "top_opponents":   top_opps,
        "hero_vpip_avg":   round(mean(s["hero_vpip"] for s in sessions), 3),
        "hero_pfr_avg":    round(mean(s["hero_pfr"] for s in sessions), 3),
        "hero_cbet_avg":   round(mean(s["hero_cbet"] for s in sessions), 3),
        "wtsd_avg":        round(mean(s["wtsd"] for s in sessions), 3),
    }

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "overall":      overall,
        "versions":     versions,
        "sessions":     sessions,
    }


# ── main ────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    stamps = _session_stamps()
    if not stamps:
        print(f"[build] no sessions found in {LOG_DIR} — writing empty metrics")
        payload = _aggregate([])
    else:
        sessions = []
        for s in stamps:
            try:
                dec, hnd = _load_session(s)
                sessions.append(_session_rollup(s, dec, hnd))
            except Exception as e:
                print(f"[build] WARN: skipping {s} ({e})")
        payload = _aggregate(sessions)
        print(f"[build] aggregated {len(sessions)} sessions, "
              f"{payload['overall'].get('hands', 0)} hands total")

    with open(OUT_FP, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"[build] wrote {OUT_FP}  ({os.path.getsize(OUT_FP) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
