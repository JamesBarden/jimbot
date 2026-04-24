"""
Session-persistent opponent modelling.

Per-hand observations roll up into per-opponent aggregate stats that survive
across hands within a single bot run (and optionally across sessions via the
JSON dump written at shutdown).

Stats tracked per opponent:
  hands_observed       total hands where the opponent was seated
  hands_vpip           hands where they put money in voluntarily preflop
                       (i.e. called/raised, ignoring forced blinds)
  hands_pfr            hands where they raised preflop
  three_bet_ops        hands where they faced a raise preflop (had a 3bet opportunity)
  three_bets           hands where they raised over an existing raise preflop
  flops_seen           hands where they saw a flop (denominator for cbet stats)
  cbet_faced           times they faced a flop c-bet from the PFA
  fold_to_cbet         times they folded to that c-bet
  postflop_bets        sum of their bets+raises on flop/turn/river
  postflop_calls       sum of their calls on flop/turn/river

Derived:
  vpip  = hands_vpip / hands_observed       (looseness)
  pfr   = hands_pfr  / hands_observed       (aggression preflop)
  3bet% = three_bets / three_bet_ops        (preflop aggression vs raises)
  af    = postflop_bets / max(postflop_calls, 1)   (postflop aggression)
  fold_cbet% = fold_to_cbet / cbet_faced

Tier mapping derived live from VPIP/PFR:
  VPIP < 15           → 'tight'
  15 ≤ VPIP ≤ 30      → 'medium' if PFR close to VPIP else 'tight'
  30 < VPIP ≤ 50      → 'medium' or 'wide' based on AF
  VPIP > 50           → 'wide'
  AF > 3.5            → bump one tier looser (aggressive)
  hands_observed < 8  → 'random' (insufficient data)
"""
from dataclasses import dataclass, field, asdict
from typing import Optional
import json
import os


SAMPLE_THRESHOLD = 8   # below this many hands, tier = 'random'


@dataclass
class OpponentProfile:
    username: str
    hands_observed:  int = 0
    hands_vpip:      int = 0
    hands_pfr:       int = 0
    three_bet_ops:   int = 0
    three_bets:      int = 0
    flops_seen:      int = 0
    cbet_faced:      int = 0
    fold_to_cbet:    int = 0
    postflop_bets:   int = 0
    postflop_calls:  int = 0

    # ── derived ──────────────────────────────────────────────────────────────

    @property
    def vpip(self) -> float:
        return self.hands_vpip / max(1, self.hands_observed)

    @property
    def pfr(self) -> float:
        return self.hands_pfr / max(1, self.hands_observed)

    @property
    def three_bet_rate(self) -> float:
        return self.three_bets / max(1, self.three_bet_ops)

    @property
    def af(self) -> float:
        return self.postflop_bets / max(1, self.postflop_calls)

    @property
    def fold_cbet_rate(self) -> float:
        return self.fold_to_cbet / max(1, self.cbet_faced)

    def tier(self) -> str:
        """Map session stats to one of the engine's 5 tiers."""
        if self.hands_observed < SAMPLE_THRESHOLD:
            return "random"

        vp = self.vpip
        if   vp < 0.15:   base = "tight"
        elif vp < 0.30:   base = "medium" if self.pfr >= vp * 0.6 else "tight"
        elif vp < 0.50:   base = "medium" if self.af  <= 2.0      else "wide"
        else:             base = "wide"

        # Very aggressive opponents slide one tier looser in practice
        if self.af > 3.5 and base != "wide":
            base = "wide"

        return base

    def short(self) -> str:
        return (f"{self.username}: {self.hands_observed}h  "
                f"VPIP {self.vpip:.0%}  PFR {self.pfr:.0%}  "
                f"AF {self.af:.1f}  → {self.tier()}")


class ProfileRegistry:
    """Session-wide map of username → OpponentProfile, with JSON persistence."""

    def __init__(self, persist_path: Optional[str] = None):
        self._profiles: dict[str, OpponentProfile] = {}
        self._persist_path = persist_path
        if persist_path and os.path.isfile(persist_path):
            try:
                with open(persist_path, "r") as f:
                    for name, data in json.load(f).items():
                        self._profiles[name] = OpponentProfile(**data)
                print(f"[profiles] loaded {len(self._profiles)} profiles from {persist_path}")
            except Exception as e:
                print(f"[profiles] WARN: could not load {persist_path}: {e}")

    def get(self, username: str) -> OpponentProfile:
        if username not in self._profiles:
            self._profiles[username] = OpponentProfile(username=username)
        return self._profiles[username]

    def tier_for(self, username: Optional[str], fallback: str = "random") -> str:
        if not username:
            return fallback
        return self.get(username).tier()

    def all(self) -> list[OpponentProfile]:
        return list(self._profiles.values())

    def save(self):
        if not self._persist_path:
            return
        try:
            with open(self._persist_path, "w") as f:
                json.dump(
                    {p.username: asdict(p) for p in self._profiles.values()},
                    f, indent=2,
                )
            print(f"[profiles] saved {len(self._profiles)} profiles → {self._persist_path}")
        except Exception as e:
            print(f"[profiles] WARN: could not save: {e}")

    def print_summary(self):
        if not self._profiles:
            return
        print("─" * 62)
        print("Opponent profiles (this session):")
        for p in sorted(self._profiles.values(), key=lambda x: -x.hands_observed):
            print(f"  {p.short()}")
        print("─" * 62)

    # ── roll a completed HandContext into each opponent's stats ──────────────

    def ingest_hand(self, ctx) -> None:
        """
        Increment stats for every opponent seated at this hand.
        Uses only what the state-delta inference in HandContext observed — so
        stats for multi-way pots are approximate (we can't tell which specific
        opponent did what from state deltas alone).

        For each opponent we can credit only coarse observations:
          - hands_observed: always +1
          - flops_seen: +1 if the hand reached the flop with them still in
          - cbet/postflop stats: only credited to the single opponent in
            heads-up pots (num_opponents_at_start == 1)
          - vpip/pfr: only inferred in HU pots where we can attribute the
            non-hero action to the one remaining villain
        """
        if not ctx.villain_names:
            return

        hu = len(ctx.villain_names) == 1
        pfa_villain = ctx.preflop_aggressor == "villain"

        for name in ctx.villain_names:
            p = self.get(name)
            p.hands_observed += 1

            if ctx.reached_street in ("flop", "turn", "river"):
                p.flops_seen += 1

            if not hu:
                # Multi-way: no attribution beyond hand count / flop_seen
                continue

            # Heads-up inference below
            pre = ctx.streets["preflop"]
            # Preflop VPIP — villain put money in voluntarily: either they
            # raised (hero sees to_call > bb) or they called a hero raise.
            if pfa_villain or (ctx.preflop_aggressor == "hero" and not pre.villain_bet
                               and ctx.reached_street != "preflop"):
                p.hands_vpip += 1
            if pfa_villain:
                p.hands_pfr += 1
                # If hero also raised preflop, villain faced a 3bet opportunity
                if pre.hero_bet:
                    p.three_bet_ops += 1
                    # (3bet itself would be a second villain raise — not tracked
                    #  from state deltas alone; leave 0)

            # C-bet stats (flop only): hero was PFA and bet the flop.
            if pfa_villain is False and ctx.hero_cbet("flop"):
                flop = ctx.streets["flop"]
                p.cbet_faced += 1
                # Villain folded to cbet if hand ended on the flop and hero bet
                if ctx.reached_street == "flop":
                    p.fold_to_cbet += 1

            # Postflop aggression — approximate from villain_bet flags
            for s in ("flop", "turn", "river"):
                rec = ctx.streets[s]
                if rec.villain_bet:
                    p.postflop_bets += 1
                if rec.hero_bet and not rec.villain_bet and rec.villain_checks > 0:
                    p.postflop_calls += 1   # they check-called or checked behind
