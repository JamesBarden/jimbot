# Jimbot

A Python bot that plays Texas Hold'em on [pokernow.com](https://www.pokernow.com) against human opponents. It uses **Playwright** for browser automation, **DOM scraping** for game state, and a **multi-layer GTO-inspired decision engine** that applies pre-solved solver data on the flop, position-aware GTO range tables preflop, and pseudo-GTO heuristics on the turn and river.

**📊 [Performance dashboard →](https://jamesbarden.github.io/jimbot/)** — cumulative BB, session-by-session breakdown, and version-comparison charts, updated from the aggregated session logs.

---

## Versioning

The bot is versioned in the `VERSION` file (semver-like `MAJOR.MINOR`, starting at `1.0`). Every session log records the version it ran under so the dashboard can compare performance across deployments.

- `1.0 → 1.1` on any non-trivial code change (default)
- `1.x → 2.0` on a breaking/structural overhaul
- `1.3 → 1.3.1` (patch) on tiny fixes

Workflow for Claude and humans alike:

```bash
# After a code change:
python3 scripts/bump_version.py minor   # or major / patch
git add -A
git commit -m "Jimbot v$(cat VERSION): <brief summary>"
git push

# After a play session:
#   nothing — main.py auto-deploys the dashboard at shutdown.
#   Use --no-deploy to skip when iterating locally.
python3 main.py <url> --no-deploy
```

See `CLAUDE.md` for the full workflow.

---

## Table of contents

1. [Quick start](#quick-start)
2. [Architecture overview](#architecture-overview)
3. [Module map](#module-map)
4. [Decision engine](#decision-engine)
   - [Decision priority](#decision-priority)
   - [Mixed-strategy randomisation](#mixed-strategy-randomisation)
   - [Preflop: GTO range tables](#preflop-gto-range-tables)
   - [Flop: TexasSolver lookup](#flop-texassolver-lookup)
   - [Turn: pseudo-GTO heuristic](#turn-pseudo-gto-heuristic)
   - [River: pseudo-GTO heuristic](#river-pseudo-gto-heuristic)
   - [Monte Carlo fallback](#monte-carlo-fallback)
   - [Bet sizing](#bet-sizing)
   - [Decision log](#decision-log)
5. [Hand and board classification](#hand-and-board-classification)
   - [Hand classes](#hand-classes)
   - [Flop board texture](#flop-board-texture)
   - [Turn card texture](#turn-card-texture)
   - [River card texture](#river-card-texture)
6. [Opponent range modelling](#opponent-range-modelling)
7. [Game loop](#game-loop)
8. [DOM scraping](#dom-scraping)
9. [Action execution](#action-execution)
10. [GTO solver setup (optional but recommended)](#gto-solver-setup)
11. [Tuning the bot](#tuning-the-bot)
12. [DOM inspector](#dom-inspector)
13. [Known limitations](#known-limitations)
14. [Files at a glance](#files-at-a-glance)

---

## Quick start

```bash
# 1. Install Python dependencies
pip3 install --user -r requirements.txt

# 2. Install the Chromium browser Playwright will control
python3 -m playwright install chromium

# 3. Launch the bot
python3 main.py https://www.pokernow.com/games/<game-id>
```

A visible Chromium window opens. You handle login, buy-in, and taking a seat yourself. Once seated, press **ENTER** in the terminal — the bot takes over all decisions. Press **Ctrl+C** to stop.

---

## Architecture overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                             pokernow.com DOM                                │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │  Playwright async reads
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  scraper.py  →  GameState                                                   │
│  hole cards, board, pot, stack, blinds, to_call, phase, can_check,         │
│  position (BTN/CO/HJ/SB/BB/UTG), num_opponents, is_my_turn                │
└──────────────────┬────────────────────────────────────────────────────────┘
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
┌───────────────┐    ┌──────────────────────────────────────────────────────┐
│ hand_tracker  │    │                    engine.py                          │
│   .update()   │───▶│  decide(state, opponent_tier)                         │
│  → tier str   │    │                                                        │
└───────────────┘    │  preflop  → preflop_ranges (GTO tables)               │
                     │  flop     → solver_lookup  (TexasSolver, if available) │
                     │  turn     → turn_heuristic (pseudo-GTO, 5 layers)      │
                     │  river    → river_heuristic (pseudo-GTO, 5 layers)     │
                     │  fallback → monte_carlo_equity                          │
                     │                                                        │
                     │  sizing   → bet_sizing (4 sizes, board wetness)        │
                     └──────────────────────┬───────────────────────────────┘
                                            │  ('raise', N) / ('call', N)
                                            │  ('check', 0) / ('fold', 0)
                                            ▼
                          ┌─────────────────────────────────┐
                          │  actions.py → Playwright clicks  │
                          └─────────────────────────────────┘
```

---

## Module map

```
jimbot/
├── main.py              entry point — poll loop, stuck detection, hand counter
├── scraper.py           DOM → GameState (all Playwright reads)
├── state.py             Card and GameState dataclasses; pot_odds() helper
├── engine.py            GameState → (action, amount); routes to sub-modules
│
├── preflop_ranges.py    Position-aware GTO open and 3bet/call/fold tables
├── solver_lookup.py     Loads solutions/lookup.pkl; O(1) flop strategy queries
├── turn_heuristic.py    Turn pseudo-GTO: 5-layer frequency computation
├── river_heuristic.py   River pseudo-GTO: same structure, river-specific tables
├── bet_sizing.py        4-size bet/raise sizing with board wetness adjustment
│
├── hand_classifier.py   classify() hand classes; board texture classifiers
├── hand_tracker.py      Infers opponent range tier from state transitions
├── ranges.py            Hand range definitions; combo expansion for Monte Carlo
│
├── actions.py           (action, amount) → fold/check/call/raise_amount clicks
├── inspector.py         Dev tool: dumps live DOM to verify/update selectors
│
└── scripts/
    ├── presolve.py      Runs TexasSolver for all 108 representative flop spots
    ├── build_lookup.py  Parses solver output → solutions/lookup.pkl
    └── inspect_output.py  Prints raw solver JSON structure for debugging
```

---

## Decision engine

`engine.py` is the sole decision-maker. The entry point is:

```python
action, amount = engine.decide(state, opponent_tier=opponent_tier)
```

Returns one of:

| Value | Meaning |
|---|---|
| `('fold', 0)` | Fold |
| `('check', 0)` | Check (free) |
| `('call', N)` | Call N chips |
| `('raise', N)` | Raise/bet to N chips total |

### Decision priority

The engine tries each source in order and stops at the first hit:

```
1. Flop solver lookup   — pre-computed TexasSolver strategies (flop only)
2. Preflop GTO tables   — position-aware range matrices (preflop only)
3. Turn heuristic       — pseudo-GTO 5-layer model (turn only)
4. River heuristic      — pseudo-GTO 5-layer model (river only)
5. Monte Carlo          — range-weighted equity simulation (rare fallback)
```

In normal play, Monte Carlo fires only if the flop solver lookup misses (board texture / SPR combination not in the 108 pre-solved spots). All other streets are always covered by sources 2–4.

---

### Mixed-strategy randomisation

Every decision is **probabilistic**, not deterministic. Each source produces a frequency triple `(raise_freq, call_freq, fold_freq)` that sums to 1.0. A single random roll then selects the action:

```
roll = random.random()              # uniform [0, 1)

if roll < raise_freq:               → RAISE
elif roll < raise_freq + call_freq: → CALL (or CHECK if can_check)
else:                               → FOLD (or CHECK if can_check)
```

`can_check` is always preferred over folding: when the check button is available (no bet to face), fold_freq is zeroed and the residual moves to call/check.

This means the **same hand in the same spot plays differently each session**. An opponent watching for patterns can observe that you raise AKo UTG sometimes, call sometimes — the exact frequency matches the GTO table — but they cannot predict the specific action. This is what makes the strategy unexploitable in the game-theoretic sense.

---

### Preflop: GTO range tables

**File:** `preflop_ranges.py`

Preflop decisions use hand-crafted GTO-inspired frequency matrices for 6-max 100BB cash games, one per position. The Chen formula has been replaced entirely.

#### Two scenarios

**RFI (raise first in):** we are the first to put in a raise. Options are raise or fold — no limping.

**VS_RAISE:** someone has already raised (to_call > big_blind). Options are 3-bet, flat-call, or fold.

```python
facing_raise = state.to_call > state.big_blind
raise_freq, call_freq, fold_freq = preflop_ranges.lookup(
    hole_cards, position, facing_raise
)
```

#### Hand key

Hands are normalised to a canonical string before lookup:

```
AA, KK, QQ, …          pocket pairs
AKs, AQs, KQs, …       suited hands (higher rank first)
AKo, AQo, KQo, …       offsuit hands
```

Unlisted hands default to pure fold `(0.0, 0.0, 1.0)`.

#### RFI tables — opening frequencies by position

Each position table maps hand → raise_freq. Call_freq is always 0 (no limping).

| Position | Example opens | Range width |
|---|---|---|
| UTG | AA–99, AKs–ATs, AKo–AQo, KQs–JTs | ~14% |
| HJ | Adds 88–55, more Ax suited, K9s, suited connectors | ~20% |
| CO | Adds 44–33, Ax through A2s, K8s, connectors to 65s | ~27% |
| BTN | Near-complete steal range, almost all playable hands | ~45% |
| SB | Similar to BTN (completes against BB) | ~48% |
| BB | Never opens (no RFI) | — |

Frequencies are fractional for mixed strategies. Example:

```python
_BTN = {
    "QQ": 1.0,   # always raise
    "77": 0.8,   # raise 80%, fold 20%
    "A5o": 0.7,  # raise 70%, fold 30%
    "43s": 0.4,  # raise 40%, fold 60%
}
```

#### VS_RAISE tables — facing a raise, by position

Each position table maps hand → (3bet_freq, call_freq). Fold_freq = 1 − 3bet − call.

**Positional calling ranges:**

- **BTN** has the widest flatting range (IP postflop): `99–22` pure call, `JTs/T9s/98s/87s` pure call, most Kx-suited call
- **BB** has the widest defending range overall (good price, closes action): calls `A2o–A9o`, `K7s+`, all pairs, most suited connectors
- **SB** is 3bet-or-fold only — flatting OOP in a heads-up pot is a significant postflop disadvantage
- **UTG** calls conservatively: `JJ–88` (mostly call), `AJs/KQs` (mostly call), `QJs–T9s` (pure call)

Example: BTN facing a raise with `JTs`:
```python
_BTN_VR["JTs"] = (0.0, 0.9)  →  0% 3bet, 90% call, 10% fold
```

#### BB option (can_check)

When BB has the option to check preflop (everyone folded or limped), fold_freq is zeroed and the residual is redistributed — the BB always uses the free look.

---

### Flop: TexasSolver lookup

**File:** `solver_lookup.py` | Requires: `solutions/lookup.pkl`

On the flop the engine first queries a pre-computed strategy table built from actual solver output. This is the strongest decision source.

#### Lookup key

```python
(board_texture, spr_bucket, villain_tier, facing_bet)
```

| Dimension | Values | How determined |
|---|---|---|
| `board_texture` | 12 strings, e.g. `dry_rainbow_high` | `board_texture()` on board[:3] |
| `spr_bucket` | `low` / `medium` / `high` | stack / pot: <4 / 4–10 / 10+ |
| `villain_tier` | `tight` / `medium` / `wide` | mapped from hand_tracker's 5 tiers |
| `facing_bet` | `True` / `False` | `to_call > 0` |

The table covers 108 spots (12 × 3 × 3 combinations, with facing_bet extending each). Within each spot, strategies are indexed by **hand class** — all combos in the same hand class share one averaged frequency triple.

#### Fallback chain on miss

1. Relax villain tier: try `medium`, then `wide`, then `tight`
2. Relax hand class: find the nearest class by strength rank
3. If still nothing: return `None` → engine falls back to Monte Carlo

#### Villain tier mapping

The hand_tracker produces 5 tiers; the solver was built for 3:

```
premium → tight
tight   → tight
medium  → medium
wide    → wide
random  → wide
```

---

### Turn: pseudo-GTO heuristic

**File:** `turn_heuristic.py`

When no solver data exists for the turn, the engine applies a 5-layer frequency model. Layers are applied in order; each adjusts the frequencies produced by the layer before it.

#### Inputs

```python
turn_heuristic.query(
    hole_cards,    # list[Card]
    board_cards,   # list[Card], length 4
    position,      # 'BTN', 'CO', 'HJ', 'SB', 'BB', 'UTG', etc.
    villain_tier,  # 'premium', 'tight', 'medium', 'wide', 'random'
    facing_bet,    # True if to_call > 0
    spr,           # effective stack / pot
    bet_fraction,  # to_call / pot  (0 if not facing a bet)
)
→ (raise_freq, call_freq, fold_freq)
```

#### Layer 1 — Base frequencies (hand class × turn texture)

**Not facing a bet (`_FREE` table):**
Look up `bet_freq = _FREE[hand_class][turn_texture]`. This is the probability of betting (raising); check probability is `1 - bet_freq`; fold is always 0.

Example values:

| Hand class | blank | flush_complete | overcard |
|---|---|---|---|
| monster | 0.95 | 0.95 | 0.95 |
| set | 0.75 | 0.60 | 0.75 |
| top_pair_top | 0.60 | 0.40 | 0.40 |
| draw | 0.35 | 0.20 | 0.30 |
| air | 0.20 | 0.15 | 0.15 |

**Facing a bet (`_FACED` table):**
Look up `(raise_freq, call_freq, fold_freq)` directly.

Example values for blank turn:

| Hand class | Raise | Call | Fold |
|---|---|---|---|
| monster | 60% | 40% | 0% |
| set | 45% | 55% | 0% |
| top_pair_top | 20% | 65% | 15% |
| middle_pair | 5% | 45% | 50% |
| air | 10% | 20% | 70% |

Draw-completing turns (flush_complete, straight_complete) significantly increase fold frequency for made-hand categories like two_pair and straight, which are now partially beaten.

#### Layer 2 — Position adjustment

```
IP  (BTN / CO / HJ):  raise_freq += 0.05
OOP (all others):     raise_freq -= 0.05
```

IP players have informational advantage: they act last, can bluff more credibly, and extract more value.

#### Layer 3 — SPR adjustment

```
SPR < 4  (short stack):   raise_freq += 0.08,  fold_freq -= 0.08
SPR > 10 (deep stack):    raise_freq -= 0.05,  fold_freq += 0.05
SPR 4–10 (normal):        no adjustment
```

Short-stacked situations favour committing; deep-stacked situations favour caution since mistakes cost more chips.

#### Layer 4 — Villain tier (marginal hands only)

Applied only to: `two_pair, overpair, top_pair_top, top_pair_weak, middle_pair, draw, combo_draw`

```python
_VILLAIN_FOLD_ADJ = {
    "premium": +0.10,   # tighten significantly vs strong ranges
    "tight":   +0.06,
    "medium":   0.00,   # baseline
    "wide":    -0.06,   # loosen vs wide/weak ranges
    "random":  -0.03,
}
fold_freq += _VILLAIN_FOLD_ADJ[villain_tier]
```

Strong hands (monster through set) are unaffected — you never fold a set to any villain. Air is unaffected — the bluff/fold decision there is more about bet sizing and board texture than the villain's raw range width.

#### Layer 5 — Villain bet sizing

Only applied when `facing_bet=True`. Classifies `bet_fraction = to_call / pot`:

| Tier | Threshold | Strategic implication |
|---|---|---|
| `small` | ≤ 0.35× pot | Villain is blocking or betting thin — defend wider, pot odds are good |
| `medium` | 0.35–0.75× pot | Standard sizing — baseline frequencies apply |
| `large` | 0.75–1.15× pot | More polarised range — fold marginal hands more |
| `overbet` | > 1.15× pot | Highly polarised — fold almost everything except strong hands |

Adjustments:

```
_FOLD_SENSITIVE hands (two_pair, overpair, top pairs, pairs, draws):
    fold_freq *= fold_mult   (0.60 / 1.00 / 1.40 / 1.85 for small/medium/large/overbet)

air:
    fold_freq *= fold_mult_air   (0.82 / 1.00 / 1.20 / 1.45)
    raise_freq += raise_adj_air  (0.00 / 0.00 / -0.05 / -0.10)
    (bluff-raising becomes less viable vs large bets)

_STRONG hands (monster, full_house, flush, straight, set):
    raise_freq += raise_adj_strong  (-0.05 / 0.00 / +0.05 / +0.12)
    (slow-play vs small bets; press value vs large bets)
```

#### Final normalisation

After all 5 layers: `call_freq = max(0, 1 − raise_freq − fold_freq)`, then the triple is normalised to sum exactly to 1.0. For non-facing-bet spots, fold_freq is forced to 0 before normalisation.

---

### River: pseudo-GTO heuristic

**File:** `river_heuristic.py`

Identical 5-layer structure to the turn heuristic with these key differences:

#### Differences from turn

**No semi-bluffs.** Draws have resolved. A hand classified as `draw` or `combo_draw` on the river is effectively a busted draw — the tables reflect near-zero call/raise frequencies for these classes when facing a bet.

**Higher fold frequencies on marginal hands.** There are no future streets to improve. Calling with middle pair on the river is purely a showdown decision. The `_FACED` base frequencies are more aggressive about folding marginal holdings.

**Air bluffs at 20–25%.** River betting strategy is polarised: value bets and bluffs, with very little middle ground. The `_FREE[air]` values are ~20–22% to reflect a balanced bluffing frequency.

**Trips board texture.** The river can pair a card that was already paired on the turn board, creating a `trips_board` texture. This is dangerous for made hands (full house possibilities increase) and is handled as a separate column.

**SPR thresholds adjusted.** SPR on the river is typically low. The short-stack threshold is `< 2` (not 4 as on the turn); the deep threshold is `> 8`.

---

### Monte Carlo fallback

**File:** `engine.py` → `monte_carlo_equity()`

Used only when the flop solver lookup misses. Estimates win probability by simulation:

```
for each of 1500 trials:
  1. Sample one opponent hand from their inferred range
     (filtered against all known cards)
  2. Randomly complete the board from the remaining deck
  3. Evaluate both hands with the treys Evaluator
  4. wins += 1.0 (win) / 0.5 (chop) / 0.0 (loss)

equity = wins / valid_trials
```

From equity, action frequencies are computed using a blend zone around each threshold:

```
raise_freq = clamp((equity - POSTFLOP_RAISE_THRESHOLD) / BLEND_WIDTH)
fold_freq  = clamp((call_threshold - equity) / BLEND_WIDTH)
call_freq  = 1 - raise_freq - fold_freq

POSTFLOP_RAISE_THRESHOLD = 0.60
POSTFLOP_CALL_EDGE       = 0.05   (call if equity > pot_odds + this)
BLEND_WIDTH              = 0.08   (zone over which actions mix near each threshold)
```

The blend zone prevents cliff edges: at exactly `equity = 0.60` the bot raises 50% of the time, not 100%. At `equity = 0.68+` it raises 100%.

---

### Bet sizing

**File:** `bet_sizing.py`

When the action is raise/bet, the engine samples a pot fraction from `{0.33, 0.66, 1.00, 1.50}` rather than using a fixed size.

#### How a size is chosen

1. Look up a weight tuple `(w33, w66, w100, w150)` for the current hand class and scenario (betting vs raising a bet):

| Hand class | 33% | 66% | 100% | 150% | Logic |
|---|---|---|---|---|---|
| monster / full_house | 5 | 20 | 45 | 30 | Build pot; occasional overbet |
| flush / straight / set | 5–10 | 30 | 42–45 | 15–20 | Strong value |
| two_pair / overpair | 15 | 45–50 | 25–30 | 10 | Merged; protection sizing |
| top_pair_top | 20 | 50 | 22 | 8 | Thin value; go smaller |
| top_pair_weak / mid pair | 35–55 | 35–45 | 8–15 | 2–5 | Block bet or pot control |
| **air** | **15** | **25** | **30** | **30** | **Polarised: block or overbet bluff** |
| combo_draw / draw | 10–15 | 35–40 | 35–42 | 10–20 | Semi-bluff; charge draws |

Raising a bet (re-raise) uses a shifted table that pushes weights toward larger sizes, because re-raising is inherently a more polarised action.

2. Apply a **board wetness multiplier** element-wise before normalising:

| Wetness | 33% mult | 66% mult | 100% mult | 150% mult | How determined |
|---|---|---|---|---|---|
| `wet` | 0.50 | 0.85 | 1.20 | 1.60 | monotone or connected flop; draw-completing turn/river |
| `semi_wet` | 0.80 | 1.00 | 1.10 | 1.15 | twotone flop |
| `dry` | 1.40 | 1.10 | 0.85 | 0.55 | rainbow, unconnected |

Wet boards need larger bets to charge draws. Dry boards can use smaller bets to extract thin value.

3. Sample from the normalised distribution with a random roll.

#### Preflop sizing

Preflop uses BB multiples (pot fractions are not meaningful before the flop):

- **Open raises:** sample from `{2.0×, 2.5×, 3.0×, 3.5×}` BB with position-based weights. BTN leans toward 2–2.5×; UTG leans toward 3–3.5×.
- **3bets:** sample from `{3×, 3.5×, 4×}` the original raise amount. Weights: 35% / 40% / 25%.

Both are enforced to `max(result, min_legal_raise)` and capped at `my_stack`.

---

### Decision log

Every decision prints a bordered box to the terminal showing the full reasoning chain:

```
┌──────────────────────────────────────────────────────────────┐
│  DECISION  [TURN]                                            │
│    Hand   As  Kh                                             │
│    Board  7d  2c  Jh  |  Qs                                  │
│    Pos    BTN  (IP)                                          │
│    Stack  150.00   Pot 40.00   ToCall 16.00   SPR 3.8        │
│    Odds   pot_odds=28.6%   can_check=False                   │
│    Villain  tier=tight   opponents=1                         │
│  ────────────────────────────────────────────────────────────│
│    [turn]  card=Qs  texture=overcard                         │
│            hand_class=top_pair_top  facing_bet=True          │
│            bet=0.40×pot (medium)                             │
│            IP=True   SPR=3.8(medium)   villain=tight         │
│    Heuristic  R=25%  C=55%  F=20%                            │
│  ────────────────────────────────────────────────────────────│
│    Source  turn_heuristic                                     │
│    Freq    RAISE=25%   CALL=55%   FOLD=20%                   │
│    Roll    0.6123  →  CALL  (0.6123 in [0.2500, 0.8000))     │
│    Sizing  [raising]  hand=top_pair_top  board=dry            │
│            33%=22%  66%=47%  100%=25%  150%=6%               │
│            roll=0.4412  →  66% pot                           │
│    Action  ► CALL  16.00                                      │
└──────────────────────────────────────────────────────────────┘
```

The sizing block only appears when the action is RAISE.

---

## Hand and board classification

**File:** `hand_classifier.py`

### Hand classes

`classify(hole_cards, board_cards)` evaluates the 7-card hand with the treys library and maps the result to one of 15 classes:

| Class | Description |
|---|---|
| `monster` | Straight flush or four of a kind |
| `full_house` | Full house |
| `flush` | Made flush |
| `straight` | Made straight |
| `set` | Three of a kind (set or trips) |
| `two_pair` | Two pair |
| `overpair` | Pocket pair above all board cards (or above all but one) |
| `top_pair_top` | Top pair with kicker that beats all other board ranks |
| `top_pair_weak` | Top pair with a weaker kicker |
| `middle_pair` | Paired with the second-highest board card |
| `bottom_pair` | Paired with the lowest board card |
| `combo_draw` | 12+ outs — flush draw + open-ended straight draw |
| `draw` | 8–9 outs — flush draw or open-ended straight draw |
| `weak_draw` | 4 outs — gutshot straight draw only |
| `air` | No pair, no meaningful draw |

For draw classification, straight outs are counted across all 5-card windows including the A-low wheel. A double-ended draw (e.g. KQJT needing A or 9) correctly counts 8 outs, not 4.

### Flop board texture

`board_texture(board_cards[:3])` classifies the first three community cards into a string of the form `{connectivity}_{flush_texture}_{height}`:

```
connectivity:   connected  (span of top to bottom card ≤ 4)
                paired     (a rank appears twice)
                dry        (span > 4, no pair)

flush_texture:  monotone   (all 3 same suit)
                twotone    (exactly 2 same suit)
                rainbow    (all different suits)

height:         high       (top card ≥ Ten)
                mid        (top card 6–9)
                low        (top card ≤ 5)
```

Examples: `dry_rainbow_high` (A72r), `connected_twotone_mid` (9♥8♥7d), `paired_monotone_high` (KK♣J♣)

### Turn card texture

`turn_card_texture(board_cards)` classifies `board_cards[3]` relative to `board_cards[:3]`:

| Texture | Condition |
|---|---|
| `pair_board` | Turn card ranks the same as a flop card |
| `flush_complete` | Flop had exactly 2 of one suit; turn is the third |
| `straight_complete` | Turn creates 4+ connected cards where flop had exactly 3 |
| `overcard` | Turn card ranks above all three flop cards |
| `blank` | None of the above |

### River card texture

`river_card_texture(board_cards)` classifies `board_cards[4]` relative to `board_cards[:4]`:

| Texture | Condition |
|---|---|
| `trips_board` | River cards a rank that already appeared twice in the prior 4 cards |
| `pair_board` | River cards any rank from the prior 4 (not already a pair) |
| `flush_complete` | 3+ of one suit in prior 4 cards; river is the same suit |
| `straight_complete` | River fills a 4-card draw to a made straight |
| `overcard` | River ranks above all four prior board cards |
| `blank` | None of the above |

---

## Opponent range modelling

**File:** `hand_tracker.py`

`HandTracker` observes state snapshots across a hand (not an action stream) and infers a range tier, which is passed to the engine so Monte Carlo and the heuristics respond appropriately.

### The five tiers

| Tier | Approx % | Representative hands |
|---|---|---|
| `premium` | ~5% | AA–TT, AKs, AKo, AQs, AJs, ATs |
| `tight` | ~20% | + 99–77, KQs–KTs, QJs, AJo, ATo, KQo |
| `medium` | ~40% | + all pairs, A2s–A9s, K5s–K9s, suited connectors |
| `wide` | ~65% | + K2s–K4s, weaker suited hands, A2o–A5o |
| `random` | 100% | All 169 canonical hands |

### Preflop inference

On the first preflop observation, the tier is set from `to_call / big_blind`:

```
ratio ≥ 6  →  "premium"   (3-bet or shove)
ratio ≥ 3  →  "tight"     (standard open raise)
ratio ≥ 1  →  "medium"    (min-raise)
ratio  = 0 →  "wide"      (limped or no action)
```

### Postflop tightening

Each street where the opponent bets into us (`to_call > 0`) tightens the tier one step toward `premium`. Applied once per street only.

```
random → wide → medium → tight → premium
```

A villain who raised preflop (`tight`) and then bets the flop becomes `premium` for Monte Carlo sampling on the turn.

---

## Game loop

`main.py` runs an `asyncio` poll loop every 1.0 second.

**Per tick:**
1. `scraper.get_game_state()` — read DOM; `None` = between hands
2. Detect new hand (hole card key change) → reset tracker, increment counter
3. `tracker.update(state)` → current opponent tier
4. Print state if changed; check stuck timeout (30 s)
5. If not our turn: sleep and continue
6. If already acted on this exact state: skip (prevents double-acting)
7. Sleep `ACTION_DELAY + jitter` (1.2 s base + −0.4..+1.2 s random) — looks human
8. Re-read state; verify still our turn
9. `engine.decide()` → `actions.execute()`
10. Record acted state; sleep

**Stuck recovery:**
- State `None` for > 30 s → `page.reload()`
- State unchanged for > 30 s → `page.reload()`

**Routine reload:** every 30 hands to prevent DOM memory growth.

---

## DOM scraping

All game state comes from `scraper.py` using Playwright's async locator API.

### Key selectors

| Variable | Selector | Reads |
|---|---|---|
| `SEL_TURN` | `.table-player.you-player.decision-current` | Our turn indicator |
| `SEL_HOLE_CARDS` | `.you-player .card` | Our two hole cards |
| `SEL_BOARD_CARDS` | `.table-cards .card` | Community cards |
| `SEL_MY_STACK` | `.table-player.you-player .table-player-stack` | Our stack |
| `SEL_POT_MAIN` | `.table-pot-size .main-value .chips-value` | Pot from closed streets |
| `SEL_POT_ADDON` | `.table-pot-size .add-on .chips-value` | Current-street bets |
| `SEL_BLINDS` | `.blind-value .chips-value` | `[0]`=SB, `[1]`=BB |
| `SEL_CALL_BTN` | `button.call` | Call amount; text distinguishes `"CALL 120"` vs `"BET 20"` |
| `SEL_DEALER_BTN` | `.dealer-button-ctn` | Has class `dealer-position-N` |

### Position detection

`get_position()` determines our seat position relative to the dealer button:

1. Find the dealer element's `dealer-position-N` class → dealer seat number
2. Find our seat's `table-player-N` class → our seat number
3. Collect all active seat numbers; sort ascending = clockwise order
4. Compute `offset = (our_index − dealer_index) % total_seats`
5. Map offset to position name:
   - 0 = BTN, 1 = SB, 2 = BB
   - 3 = UTG (6p) / CO (4p), 4 = HJ (6p) / CO (5p), 5 = CO (6p)

Returns `"unknown"` if the dealer button is between hands or animations are in progress.

### `to_call` vs `bet` distinction

pokernow's call button shows `"BET 20"` when no one has opened and the payment is optional, vs `"CALL 120"` when a bet is outstanding. The scraper returns `to_call=0` for the BET case — the engine will decide whether to open-bet via the raise path.

---

## Action execution

`actions.py` translates decisions into browser interactions.

```
execute(page, action, amount, pot, my_stack)
  ├── fold   → close raise panel → click button.fold
  ├── check  → close raise panel → click button.check
  ├── call   → close raise panel → click button.call
  └── raise  → raise_amount(page, amount, pot, my_stack)
```

### Raise panel sequence

```
1. Verify button.raise is enabled
   NO  → fall back to button.call

2. Click button.raise to open the panel

3. Find text input: .raise-bet-value input[type='text']
   FOUND →  Meta+A (select all) + press_sequentially(str(amount))
            Triggers React onChange so the site validates the number.
   NOT FOUND → jump to preset button fallback

4. Click submit: .raise-controller-form input[type='submit']
   DISABLED (amount rejected) → click "Min Raise" preset → retry submit
```

### Preset button fallback

If the text input isn't found or the amount is below the site minimum:

| Condition | Preset |
|---|---|
| amount ≥ 95% of stack | All In |
| amount ≥ 85% of pot | Pot |
| amount ≥ 60% of pot | 3/4 Pot |
| amount ≥ 35% of pot | 1/2 Pot |
| otherwise | Min Raise |

---

## GTO solver setup

The flop solver lookup is **optional** — without it, the Monte Carlo fallback handles the flop. With it, flop decisions are based on actual solved strategies.

```bash
# 1. Download TexasSolver binary
#    https://github.com/bupticybee/TexasSolver/releases
#    Unzip into jimbot/TexasSolver/ and chmod +x TexasSolver/console_solver
#    (ARM64 Mac: use the native arm64 binary at TexasSolver/console_solver_arm64)

# 2. Run the solver across all 108 representative flop spots (~2 hours at --parallel 2)
python3 scripts/presolve.py

# 3. Compile raw solver output into the runtime lookup table
python3 scripts/build_lookup.py
```

The presolve script covers 108 spots: 12 board textures × 3 SPR buckets × 3 villain tiers. On ARM64 Macs, it auto-detects and uses the native binary to avoid Rosetta 2 overhead.

---

## Tuning the bot

Thresholds live at the top of `engine.py`. All others are in their respective module files.

### engine.py constants

```python
# Monte Carlo equity thresholds (postflop fallback only)
POSTFLOP_RAISE_THRESHOLD = 0.60  # raise if equity ≥ this
POSTFLOP_CALL_EDGE       = 0.05  # call if equity > pot_odds + this margin
BLEND_WIDTH              = 0.08  # ramp width around each threshold

# SPR adjustment
SPR_DEEP_THRESHOLD = 10.0   # SPR above which raise threshold increases
SPR_RAISE_ADJUST   = 0.04   # how much to increase it

# Simulation count
MONTE_CARLO_SIMS = 1500     # ±2% accuracy; 500 = ±4% but faster
```

### bet_sizing.py constants

The weight tables `_BET_WEIGHTS`, `_RAISE_WEIGHTS`, and `_WETNESS_MOD` are all at the top of the file and documented inline.

### preflop_ranges.py

The `_RFI` and `_VS_RAISE` tables in this file define the complete preflop strategy. Each hand entry is `(raise_freq, call_freq)` or just `raise_freq` — edit individual hands or whole position tables to change preflop behaviour.

---

## DOM inspector

Run this before your first session or after a pokernow site update to verify all selectors are still correct:

```bash
python3 inspector.py https://www.pokernow.com/games/<game-id>
```

The inspector prints every seat element's classes and stack, all card inner HTML, pot element values, and action button states. When prompted, manually open the raise panel in the browser and press ENTER — this dumps the raise form inputs so you can verify `SEL_RAISE_INPUT` and `SEL_RAISE_SUBMIT` in `actions.py`.

---

## Known limitations

**Opponent modeling is one-dimensional.** The hand tracker infers range tier from raise size and subsequent betting. It doesn't track fold-to-cbet rates, aggression frequency, showdown tendencies, or adapt within a session to observed patterns. A player who adjusts their style mid-session will not be re-read correctly.

**Villain bet sizing reads are turn/river only.** On the flop, the solver lookup produces strategies for its pre-defined bet sizes and doesn't receive the actual villain bet fraction. On turn and river, the heuristic does react to bet sizing (small / medium / large / overbet tiers), but the flop is handled by pre-computed frequencies.

**No multi-street planning.** Each street decision is made in isolation. The bot doesn't consider what range it's representing on later streets, doesn't plan for implied odds, and doesn't build a coherent story across streets. A thinking opponent can exploit this by noticing action sequences that don't make sense with any consistent range.

**Multi-way pot equity overstated.** The Monte Carlo fallback simulates one opponent regardless of `num_opponents`. In multi-way pots, actual equity is lower because you need to beat all opponents simultaneously, not just one.

**Fixed raise input selector fragility.** pokernow's raise panel UI has changed in past site updates. If raises aren't executing, run `inspector.py`, open the raise panel manually, and verify `SEL_RAISE_INPUT` and `SEL_RAISE_SUBMIT` in `actions.py`.

---

## Files at a glance

### `state.py`
`Card(rank, suit)` — single playing card. `Card.from_pokernow()` maps pokernow's raw strings including Unicode suit symbols to the `s/h/d/c` format treys expects. `GameState` snapshot — all fields needed for one decision. `pot_odds()` → `to_call / (pot + to_call)`.

### `scraper.py`
All DOM reads. `_parse_chips()` handles commas, K suffix, currency symbols, and decimal cents. `get_game_state()` assembles a complete `GameState` or returns `None`. Position detection via dealer button DOM pattern.

### `engine.py`
Single entry point `decide(state, opponent_tier)`. Routes to sub-modules by street. Applies mixed-strategy random roll. Computes raise amount via `bet_sizing`. Prints the full decision box to stdout.

### `preflop_ranges.py`
Two table families: `_RFI` (raise-first-in, one per position) and `_VS_RAISE` (facing a raise, one per position). `lookup()` returns `(raise_freq, call_freq, fold_freq)`. `hand_key()` normalises hole cards to a canonical string.

### `solver_lookup.py`
Loads `solutions/lookup.pkl` once at startup. `query()` converts the current state to a lookup key and returns a frequency triple, or `None` on miss. Falls back through villain tier relaxation and hand class nearest-neighbor before returning `None`.

### `turn_heuristic.py` / `river_heuristic.py`
Pure frequency tables (`_FREE`, `_FACED`) plus 4 adjustment tables (`_VILLAIN_FOLD_ADJ`, `_TIER_ADJ`, etc.). `query()` applies all 5 layers and returns a normalised `(raise_freq, call_freq, fold_freq)`. No IO, no randomness — that lives in `engine.py`.

### `bet_sizing.py`
`pick_bet_fraction(hand_class, board_cards, facing_bet)` — samples a pot fraction from {0.33, 0.66, 1.00, 1.50} and returns `(fraction, log_lines)`. `pick_preflop_size(position, facing_raise, to_call, bb)` — samples a BB multiple and returns `(amount, log_lines)`.

### `hand_classifier.py`
`classify()` — evaluates the 7-card hand and maps to a hand class string. `board_texture()` — flop texture string. `turn_card_texture()` / `river_card_texture()` — street card textures. All pure functions, no state.

### `hand_tracker.py`
`HandTracker` — stateful; maintains preflop tier and per-street tightening. `update(state)` returns the current tier string. Reset automatically when a new hand is detected.

### `ranges.py`
Five range tier definitions as canonical hand tuples. Expanded at module load into specific treys int combos. `get_combos(tier, exclude)` returns the filtered combo pool for Monte Carlo sampling.

### `actions.py`
`execute()` dispatches fold/check/call/raise. `raise_amount()` tries text input then preset buttons. All actions call `dismiss_raise_panel()` first.

### `main.py`
`run()` is the main async loop. Configurable constants: `POLL_INTERVAL=1.0`, `ACTION_DELAY=1.2`, `REFRESH_EVERY=30`, `STUCK_TIMEOUT=30`.
