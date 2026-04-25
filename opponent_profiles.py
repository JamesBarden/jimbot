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
    hands_observed:  int = 0    # at table for this hand
    hands_vpip:      int = 0    # HU-only attribution
    hands_pfr:       int = 0    # HU-only attribution
    three_bet_ops:   int = 0    # HU-only
    three_bets:      int = 0    # HU-only
    flops_seen:      int = 0    # multiway-safe: present at start of flop
    turns_seen:      int = 0    # multiway-safe: present at start of turn
    rivers_seen:     int = 0    # multiway-safe: present at start of river
    cbet_faced:      int = 0    # multiway-safe: hero was PFA + bet flop, V was at flop start
    fold_to_cbet:    int = 0    # multiway-safe: faced cbet AND not at turn start
    postflop_bets:   int = 0    # HU-only
    postflop_calls:  int = 0    # HU-only
    hu_hands:        int = 0    # # of hands observed where we were heads-up
    multiway_hands:  int = 0    # # of hands observed in 3+ way pots

    # ── derived ──────────────────────────────────────────────────────────────

    @property
    def vpip(self) -> float:
        # Use HU-attributed sample only — multiway can't disambiguate which villain VPIP'd
        return self.hands_vpip / max(1, self.hu_hands)

    @property
    def pfr(self) -> float:
        return self.hands_pfr / max(1, self.hu_hands)

    @property
    def three_bet_rate(self) -> float:
        return self.three_bets / max(1, self.three_bet_ops)

    @property
    def af(self) -> float:
        return self.postflop_bets / max(1, self.postflop_calls)

    @property
    def fold_cbet_rate(self) -> float:
        return self.fold_to_cbet / max(1, self.cbet_faced)

    @property
    def saw_flop_rate(self) -> float:
        """Multiway-safe looseness proxy: how often this opponent reached a flop."""
        return self.flops_seen / max(1, self.hands_observed)

    def tier(self) -> str:
        """
        Map session stats to one of the engine's 5 tiers.

        Prefers VPIP from HU sample when we have ≥SAMPLE_THRESHOLD HU hands.
        Falls back to saw_flop_rate from the (multiway-inclusive) total sample
        when HU sample is too small but we've seen the player a lot multiway.
        """
        if self.hands_observed < SAMPLE_THRESHOLD:
            return "random"

        # If we have enough HU data, use the precise stats
        if self.hu_hands >= SAMPLE_THRESHOLD:
            vp = self.vpip
            if   vp < 0.15:   base = "tight"
            elif vp < 0.30:   base = "medium" if self.pfr >= vp * 0.6 else "tight"
            elif vp < 0.50:   base = "medium" if self.af  <= 2.0      else "wide"
            else:             base = "wide"
            if self.af > 3.5 and base != "wide":
                base = "wide"
            return base

        # Fall back to the multiway-safe looseness proxy
        sf = self.saw_flop_rate
        if sf < 0.20: return "tight"
        if sf < 0.40: return "medium"
        return "wide"

    def short(self) -> str:
        if self.hu_hands >= SAMPLE_THRESHOLD:
            return (f"{self.username}: {self.hands_observed}h "
                    f"({self.hu_hands}HU)  VPIP {self.vpip:.0%}  "
                    f"PFR {self.pfr:.0%}  AF {self.af:.1f}  → {self.tier()}")
        return (f"{self.username}: {self.hands_observed}h "
                f"({self.multiway_hands} multiway)  saw_flop {self.saw_flop_rate:.0%}  "
                f"→ {self.tier()}")


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
        Update each opponent's session-level stats from a completed hand.

        Two attribution paths:

        (1) Multiway-safe — credited to every opponent based on per-street
            presence snapshots in ctx.villains_per_street:
              - hands_observed
              - flops_seen / turns_seen / rivers_seen
              - cbet_faced (hero PFA + bet flop, villain at flop start)
              - fold_to_cbet (faced cbet AND not at turn start)

        (2) HU-only — credited only when len(villain_names) == 1 because we
            can't disambiguate from state deltas alone:
              - hands_vpip, hands_pfr
              - three_bet_ops, three_bets
              - postflop_bets / postflop_calls (drives AF)
        """
        if not ctx.villain_names:
            return

        hu              = len(ctx.villain_names) == 1
        pfa_villain     = ctx.preflop_aggressor == "villain"
        pfa_hero        = ctx.preflop_aggressor == "hero"
        hero_cbet_flop  = pfa_hero and ctx.hero_cbet("flop")

        flop_set        = ctx.villains_per_street.get("flop",  frozenset())
        turn_set        = ctx.villains_per_street.get("turn",  frozenset())
        river_set       = ctx.villains_per_street.get("river", frozenset())

        for name in ctx.villain_names:
            p = self.get(name)
            p.hands_observed += 1
            if hu:  p.hu_hands       += 1
            else:   p.multiway_hands += 1

            # ── Multiway-safe per-street presence ────────────────────────
            if name in flop_set:   p.flops_seen  += 1
            if name in turn_set:   p.turns_seen  += 1
            if name in river_set:  p.rivers_seen += 1

            # ── Multiway-safe c-bet stats ────────────────────────────────
            # Each villain present at flop start when hero c-bets faced the
            # same c-bet — attribution is unambiguous.
            if hero_cbet_flop and name in flop_set:
                p.cbet_faced += 1
                if name not in turn_set:
                    p.fold_to_cbet += 1

            # ── HU-only attribution below ───────────────────────────────
            if not hu:
                continue

            pre = ctx.streets["preflop"]
            # VPIP: villain put money in voluntarily
            if pfa_villain or (pfa_hero and not pre.villain_bet
                               and ctx.reached_street != "preflop"):
                p.hands_vpip += 1
            if pfa_villain:
                p.hands_pfr += 1
                if pre.hero_bet:
                    p.three_bet_ops += 1

            # Postflop aggression (HU-only)
            for s in ("flop", "turn", "river"):
                rec = ctx.streets[s]
                if rec.villain_bet:
                    p.postflop_bets += 1
                if rec.hero_bet and not rec.villain_bet and rec.villain_checks > 0:
                    p.postflop_calls += 1
