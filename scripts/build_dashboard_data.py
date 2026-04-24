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

# Make 'anonymize' importable when running as a script
sys.path.insert(0, ROOT)
from anonymize import anon   # noqa: E402


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

    # Opponent list (anonymized)
    opponents_seen = Counter()
    for h in hands:
        for name in _parse_villain_names(h.get("villain_names", "")):
            opponents_seen[anon(name)] += 1

    # Cumulative P&L series — one point per hand in order
    pnl = []
    running = 0.0
    for h in hands:
        running += _f(h.get("bb_delta"))
        pnl.append({"hand_id": h.get("hand_id"), "bb_delta": round(_f(h.get("bb_delta")), 3),
                    "cumulative": round(running, 3)})

    return {
        "session_id":       stamp,
        "date":             date,
        "version":          version,
        "hands":            n,
        "total_bb":         round(bb_total, 2),
        "bb_per_100":       round(bb_total / n * 100, 2) if n > 0 else 0,
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

def _aggregate(sessions: list[dict]) -> dict:
    if not sessions:
        return {"sessions": [], "overall": {}, "versions": [], "generated_at": datetime.now().isoformat(timespec="seconds")}

    total_hands = sum(s["hands"] for s in sessions)
    total_bb    = sum(s["total_bb"] for s in sessions)
    winning_s   = sum(1 for s in sessions if s["total_bb"] > 0)
    losing_s    = sum(1 for s in sessions if s["total_bb"] < 0)

    # Per-version rollup
    by_ver: dict[str, list[dict]] = defaultdict(list)
    for s in sessions:
        by_ver[s["version"]].append(s)
    versions = []
    for ver, ss in sorted(by_ver.items(), key=lambda kv: kv[0]):
        h_sum   = sum(s["hands"] for s in ss)
        bb_sum  = sum(s["total_bb"] for s in ss)
        versions.append({
            "version":    ver,
            "sessions":   len(ss),
            "hands":      h_sum,
            "total_bb":   round(bb_sum, 2),
            "bb_per_100": round(bb_sum / h_sum * 100, 2) if h_sum else 0,
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
        "total_bb":        round(total_bb, 2),
        "bb_per_100":      round(total_bb / total_hands * 100, 2) if total_hands else 0,
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
