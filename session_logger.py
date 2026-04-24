"""
Session-level logging: persistent artifacts from a bot run.

Writes three streams to the Logs/ directory (dated per session):
  decisions_YYYY-MM-DD.jsonl — one JSON line per engine.decide() call
  hands_YYYY-MM-DD.csv      — one row per completed hand (with stack delta)
  console_YYYY-MM-DD.log    — raw tee of everything printed to stdout

Also accumulates in-memory session stats (hands played, bb_delta, wins,
showdown rate) and prints a running summary after each hand.

All writes are line-buffered and opened in append mode so a mid-session
crash still leaves a usable file.
"""
import csv
import json
import os
import sys
from datetime import datetime
from typing import Any, Optional


def _read_version() -> str:
    """Read VERSION from repo root. Returns '?.?.?' on failure so logs still flow."""
    here    = os.path.dirname(os.path.abspath(__file__))
    vfile   = os.path.join(here, "VERSION")
    if not os.path.isfile(vfile):
        return "0.0.0"
    try:
        return open(vfile).read().strip() or "0.0.0"
    except Exception:
        return "0.0.0"


class _Tee:
    """File-like object that mirrors writes to the real stdout and a file."""
    def __init__(self, real_stdout, log_file):
        self._real = real_stdout
        self._file = log_file

    def write(self, data):
        self._real.write(data)
        try:
            self._file.write(data)
            self._file.flush()
        except Exception:
            pass

    def flush(self):
        self._real.flush()
        try:
            self._file.flush()
        except Exception:
            pass


class SessionLogger:
    def __init__(self, log_dir: str):
        os.makedirs(log_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.log_dir      = log_dir
        self.version      = _read_version()
        self.session_id   = stamp
        self.decisions_fp = os.path.join(log_dir, f"decisions_{stamp}.jsonl")
        self.hands_fp     = os.path.join(log_dir, f"hands_{stamp}.csv")
        self.console_fp   = os.path.join(log_dir, f"console_{stamp}.log")

        self._decisions   = open(self.decisions_fp, "a", buffering=1)
        self._console     = open(self.console_fp,   "a", buffering=1)
        self._real_stdout = sys.stdout
        sys.stdout = _Tee(sys.stdout, self._console)

        # CSV: write header once
        self._hands_file   = open(self.hands_fp, "a", buffering=1, newline="")
        self._hands_writer = csv.writer(self._hands_file)
        if os.path.getsize(self.hands_fp) == 0:
            self._hands_writer.writerow([
                "timestamp", "version", "session_id", "hand_id", "position",
                "hole_cards", "final_board",
                "preflop_action", "flop_action", "turn_action", "river_action",
                "pot_final", "stack_start", "stack_end", "stack_delta",
                "bb", "bb_delta", "reached_street", "num_opponents",
                "villain_tier_end", "villain_names",
            ])

        # Session counters
        self.hands              = 0
        self.bb_delta_total     = 0.0
        self.hands_to_showdown  = 0   # heuristic: hands that reached river
        self.hands_won          = 0   # stack delta > 0
        self.session_start      = datetime.now()

        print(f"[logger] version={self.version}  session={self.session_id}")
        print(f"[logger] decisions → {self.decisions_fp}")
        print(f"[logger] hands     → {self.hands_fp}")
        print(f"[logger] console   → {self.console_fp}")

    # ── decision stream ──────────────────────────────────────────────────────

    def log_decision(self, payload: dict[str, Any]):
        """Append one decision record as JSONL."""
        payload = {
            "ts":         datetime.now().isoformat(timespec="seconds"),
            "version":    self.version,
            "session_id": self.session_id,
            **payload,
        }
        self._decisions.write(json.dumps(payload, default=str) + "\n")

    # ── hand stream ──────────────────────────────────────────────────────────

    def log_hand(self, row: dict[str, Any]):
        """Append one completed-hand row as CSV, update session counters."""
        bb           = row.get("bb") or 0.0
        stack_delta  = row.get("stack_delta") or 0.0
        bb_delta     = (stack_delta / bb) if bb > 0 else 0.0
        row["bb_delta"] = round(bb_delta, 3)

        self._hands_writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            self.version,
            self.session_id,
            row.get("hand_id"),
            row.get("position"),
            row.get("hole_cards"),
            row.get("final_board"),
            row.get("preflop_action"),
            row.get("flop_action"),
            row.get("turn_action"),
            row.get("river_action"),
            row.get("pot_final"),
            row.get("stack_start"),
            row.get("stack_end"),
            stack_delta,
            bb,
            bb_delta,
            row.get("reached_street"),
            row.get("num_opponents"),
            row.get("villain_tier_end"),
            row.get("villain_names"),
        ])

        self.hands          += 1
        self.bb_delta_total += bb_delta
        if row.get("reached_street") == "river":
            self.hands_to_showdown += 1
        if stack_delta > 0:
            self.hands_won += 1

        self._print_summary()

    # ── summary ──────────────────────────────────────────────────────────────

    def _print_summary(self):
        h    = self.hands
        elapsed = (datetime.now() - self.session_start).total_seconds() / 60
        bb100   = (self.bb_delta_total / h * 100) if h > 0 else 0
        wsd     = (self.hands_to_showdown / h * 100) if h > 0 else 0
        wr      = (self.hands_won / h * 100) if h > 0 else 0
        print(
            f"[session] hands={h}  elapsed={elapsed:.0f}m  "
            f"bb_delta={self.bb_delta_total:+.1f}  bb/100={bb100:+.1f}  "
            f"wtsd={wsd:.0f}%  won={wr:.0f}%"
        )

    # ── lifecycle ────────────────────────────────────────────────────────────

    def close(self):
        try:
            sys.stdout = self._real_stdout
            self._decisions.close()
            self._hands_file.close()
            self._console.close()
        except Exception:
            pass
