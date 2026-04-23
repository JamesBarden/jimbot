# pokernow-bot

A Python bot that plays Texas Hold'em on [pokernow.com](https://www.pokernow.com) against human opponents. It uses **Playwright** for browser automation, **DOM scraping** for game state, a **range-weighted Monte Carlo** equity engine for postflop decisions, and the **Chen formula** for preflop decisions.

---

## Table of contents

1. [Quick start](#quick-start)
2. [Architecture overview](#architecture-overview)
3. [Module map](#module-map)
4. [Decision engine](#decision-engine)
   - [Preflop: Chen formula](#preflop-chen-formula)
   - [Postflop: Monte Carlo equity](#postflop-monte-carlo-equity)
   - [Decision flowchart](#decision-flowchart)
5. [Opponent range modelling](#opponent-range-modelling)
   - [Range tiers](#range-tiers)
   - [Preflop inference](#preflop-inference)
   - [Postflop tightening](#postflop-tightening)
6. [Game loop](#game-loop)
   - [State machine](#state-machine)
   - [Stuck detection and recovery](#stuck-detection-and-recovery)
7. [DOM scraping](#dom-scraping)
   - [Selector reference](#selector-reference)
   - [State assembly](#state-assembly)
8. [Action execution](#action-execution)
9. [Tuning the bot](#tuning-the-bot)
10. [DOM inspector](#dom-inspector)
11. [Known limitations](#known-limitations)
12. [Files at a glance](#files-at-a-glance)

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

A visible Chromium window opens. You handle login, buy-in, and taking a seat yourself. Once you're seated press **ENTER** in the terminal — the bot takes over all decisions from that point on. Press **Ctrl+C** to stop.

---

## Architecture overview

The bot is a pipeline. Every second the scraper reads the live DOM, converts it into a plain Python dataclass, and hands it to the decision engine. The engine outputs a single action. The action module translates that into browser clicks.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            pokernow.com DOM                             │
│  .you-player .card   .table-cards .card   button.call   .table-pot-size │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │  Playwright locator reads (async)
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           scraper.py                                    │
│  get_game_state()  →  assembles hole cards, board, pot, stack,          │
│                        blinds, to_call, phase, is_my_turn, can_check    │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │  GameState dataclass
                               ▼
              ┌────────────────┴────────────────┐
              │                                 │
              ▼                                 ▼
┌─────────────────────────┐       ┌─────────────────────────────────────┐
│      hand_tracker.py    │       │             engine.py               │
│  HandTracker.update()   │──────▶│  decide(state, opponent_tier)       │
│  infers opponent range  │ tier  │  preflop  → Chen formula            │
│  from state transitions │       │  postflop → Monte Carlo equity      │
└─────────────────────────┘       └──────────────┬──────────────────────┘
                                                 │  ('raise', 240)
                                                 │  ('call',  0)
                                                 │  ('check', 0)
                                                 │  ('fold',  0)
                                                 ▼
                               ┌─────────────────────────────────────────┐
                               │              actions.py                 │
                               │  execute() → fold / check / call /      │
                               │             raise_amount()              │
                               │  raises: type amount → click preset btn │
                               └─────────────────────────────────────────┘
```

The **`ranges.py`** module is a static lookup table used by the engine's Monte Carlo simulation — it pre-computes all specific card combinations for five opponent tightness tiers.

---

## Module map

```
pokernow-bot/
├── main.py          ← entry point; owns the poll loop, stuck detection,
│                      periodic refresh, and action dispatch
├── scraper.py       ← DOM → GameState  (all Playwright reads live here)
├── state.py         ← Card and GameState dataclasses; pot_odds() helper
├── engine.py        ← GameState → (action, amount)
│                      Chen formula (preflop) + Monte Carlo (postflop)
├── hand_tracker.py  ← tracks opponent actions across a hand to infer range
├── ranges.py        ← hand range definitions and combo expansion
├── actions.py       ← (action, amount) → Playwright clicks
├── inspector.py     ← dev tool: dumps live DOM to verify/update selectors
└── requirements.txt
```

---

## Decision engine

`engine.py` contains the full decision logic. The entry point is:

```python
action, amount = engine.decide(state, opponent_tier=opponent_tier)
```

It returns one of:

| Return value | Meaning |
|---|---|
| `('fold', 0)` | Fold the hand |
| `('check', 0)` | Check (no cost) |
| `('call', N)` | Call — put N chips in |
| `('raise', N)` | Raise/bet to N chips total |

The decision path splits immediately on street:

```
is state.phase == "preflop"?
    YES → evaluate with Chen formula   (fast, deterministic)
    NO  → evaluate with Monte Carlo    (1500 simulations vs opponent range)
```

### Preflop: Chen formula

The **Chen formula** scores any two-card starting hand on a scale of roughly 0–20 using four factors, which the bot normalises to **0–1** by dividing by 20.

#### How the score is computed

```
1. Base score from highest card rank:
      A = 10,  K = 8,  Q = 7,  J = 6,  T = 5
      9 = 4.5, 8 = 4,  7 = 3.5, … (rank/2 + 1 for lower cards)

2. Pair bonus:
      If both cards have the same rank → score = max(base × 2, 5)
      (e.g. 22 gets 5, AA gets 20)

3. Suited bonus:
      If both cards share a suit → +2

4. Connectedness / gap penalty:
      0-gap  (connected, e.g. KQ) → +1
      1-gap  (e.g. KJ)            → −1
      2-gap  (e.g. KT)            → −2
      3-gap  (e.g. K9)            → −4
      4+ gap                      → −5

5. Low connector bonus:
      Both cards rank < 9  AND  gap ≤ 1  → +1
      (rewards small suited connectors like 87s, 76s)
```

#### Normalised score examples

| Hand | Raw Chen | Normalised | Default action |
|---|---|---|---|
| AA | 20 | 1.00 | Raise |
| KK | 16 | 0.80 | Raise |
| QQ | 14 | 0.70 | Raise |
| AKs | 12 | 0.60 | Raise |
| AKo | 10 | 0.50 | Raise |
| AQs | 10 | 0.50 | Raise |
| KQs | 9 | 0.45 | Call |
| JTs | 9 | 0.45 | Call |
| 99 | 10 | 0.50 | Raise |
| 55 | 10 | 0.50 | Raise |
| 87s | 7 | 0.35 | Call |
| 72o | −1 | 0.00 | Fold |

#### Thresholds

```python
PREFLOP_RAISE_THRESHOLD = 0.55   # raise if Chen score ≥ this
PREFLOP_CALL_THRESHOLD  = 0.30   # call if Chen score ≥ this, else fold
PREFLOP_RAISE_BB_MULT   = 3      # open-raise size = 3 × big blind
```

---

### Postflop: Monte Carlo equity

On the flop, turn, and river the bot estimates its **win probability** by simulation rather than formula, because postflop hand strength depends heavily on the board texture and opponent range.

#### Algorithm (per simulation trial)

```
for each of 1500 trials:

  1.  Sample one opponent hand from their inferred range
      (filtered so it doesn't use any cards already on the board or in our hand)

  2.  Build the "runout deck":
      full 52-card deck
      minus our hole cards
      minus board cards
      minus the sampled opponent hand

  3.  Randomly draw (5 − len(board)) cards to complete the board

  4.  Evaluate both hands using the `treys` Evaluator
      (lower score = better; 1 = Royal Flush, 7462 = worst hand)

  5.  if our_score < opp_score  →  wins += 1.0   (we win)
      if our_score == opp_score →  wins += 0.5   (chop)
      if our_score > opp_score  →  wins += 0.0   (we lose)

equity = wins / valid_trials   (valid_trials excludes any trials where
                                the deck ran short — rare edge case)
```

#### Why range-weighted and not random?

A naive simulation deals the opponent **any** two random cards. If an opponent raised 6× the big blind preflop, treating them as if they might hold 7-2o is inaccurate and causes the bot to overestimate its own equity. The `HandTracker` infers a **range tier** from observed bet sizes, and the simulation only samples opponent hands from that tier's combo pool.

Example: facing a preflop 3-bet, the opponent is classified `"premium"` (~5% of hands). The Monte Carlo sim then only deals them {AA, KK, QQ, JJ, TT, AKs, AKo, AQs, AJs, ATs} — a dramatically tighter pool — and equity falls accordingly, making the bot fold hands it would otherwise call.

#### Thresholds

```python
POSTFLOP_RAISE_THRESHOLD = 0.60   # raise if equity ≥ 60 %
POSTFLOP_CALL_EDGE       = 0.05   # call if equity > pot_odds + 5 %
POSTFLOP_BET_FRACTION    = 0.75   # bet 75% of pot when raising
MONTE_CARLO_SIMS         = 1500   # trials per decision (~100 ms)
```

**Pot odds** are computed as: `to_call / (pot + to_call)` — the minimum equity needed for a call to break even. Adding `POSTFLOP_CALL_EDGE` creates a small cushion so the bot doesn't call thin spots.

---

### Decision flowchart

```
┌─────────────────────────────────┐
│       decide(state, tier)       │
└────────────────┬────────────────┘
                 │
        ┌────────▼─────────┐
        │  state.phase      │
        │  == "preflop" ?   │
        └──┬────────────┬───┘
        YES│            │NO
           │            │
           ▼            ▼
   ┌───────────┐  ┌─────────────────────────────────┐
   │  Chen     │  │  Monte Carlo equity (1500 sims) │
   │  formula  │  │  vs opponent_tier range          │
   │  → 0–1    │  │  → 0.0–1.0                      │
   └─────┬─────┘  └───────────────┬─────────────────┘
         │                        │
         │   equity               │   equity
         ▼                        ▼
  ┌──────────────────────────────────────────────────────────┐
  │  equity ≥ strong_threshold?                              │
  │  preflop: 0.55    postflop: 0.60                         │
  └───────────────────────┬──────────────────────────────────┘
                       YES│                         │NO
                          ▼                         ▼
              ┌───────────────────┐    ┌────────────────────────────────────┐
              │  RAISE            │    │  equity ≥ call_threshold?          │
              │  preflop: 3×BB    │    │  preflop: 0.30                     │
              │  postflop: 75%pot │    │  postflop: pot_odds + 0.05         │
              └───────────────────┘    └────────────────────────────────────┘
                                                YES│                │NO
                                                   ▼                ▼
                                        ┌──────────────┐   ┌──────────────┐
                                        │ can_check?   │   │ can_check?   │
                                        └──┬───────┬───┘   └──┬───────┬───┘
                                        YES│       │NO     YES│       │NO
                                           ▼       ▼         ▼       ▼
                                        CHECK    CALL      CHECK    FOLD
```

> **can_check** is `True` when the check button is present and enabled — meaning no one has bet yet on this street and the call is free. The bot always prefers checking over folding when it doesn't have to pay.

---

## Opponent range modelling

`hand_tracker.py` observes state snapshots across a hand and infers which tier of hands the opponent is likely holding. This tier is passed to the Monte Carlo engine so opponent hands are sampled realistically.

### Range tiers

There are five tiers, defined in `ranges.py`. Each is a strict superset of the one above it.

| Tier | Approx % of hands | Representative hands | When assigned |
|---|---|---|---|
| `premium` | ~5% | AA, KK, QQ, JJ, TT, AKs, AKo, AQs, AJs, ATs | Opponent raised ≥6× BB |
| `tight` | ~20% | + 99–77, KQs, KJs, KTs, QJs, AJo, ATo, KQo | Opponent raised ≥3× BB |
| `medium` | ~40% | + all pairs, A2s–A9s, K5s–K9s, suited connectors down to 54s | Opponent raised ≥1× BB |
| `wide` | ~65% | + K2s–K4s, weaker suited hands, A2o–A4o, K7o–K9o | Opponent limped or no bet |
| `random` | 100% | All 169 canonical hand types | No information yet |

Each canonical hand type (e.g. `"AKs"`) is expanded into all specific card combos at startup — 4 combos for suited hands, 6 for pairs, 12 for offsuit — giving a pool of thousands of specific two-card combinations per tier.

### Preflop inference

On the preflop street, the `HandTracker` reads `to_call` relative to the big blind:

```
to_call / big_blind ≥ 6  →  "premium"   (3-bet or shove)
to_call / big_blind ≥ 3  →  "tight"     (standard open raise)
to_call / big_blind ≥ 1  →  "medium"    (min-raise or limp-raise)
to_call / big_blind == 0 →  "wide"      (limp or we opened, no re-raise)
```

This preflop tier is saved as the **baseline** for the rest of the hand.

### Postflop tightening

On each postflop street (flop, turn, river), if the opponent **bets into us** (`to_call > 0`), the current tier tightens by one step:

```
random → wide → medium → tight → premium
```

This is applied once per street — a second bet on the same street doesn't tighten further. The logic reflects the real-world observation that postflop aggression narrows an opponent's value range.

#### Example hand trace

```
Preflop:  to_call = 60, BB = 20  →  ratio = 3  →  tier = "tight"
Flop:     opponent checks            →  no change  →  tier = "tight"
Turn:     opponent bets, to_call>0  →  tighten     →  tier = "medium"
                                         ^^^^ wait — tightening goes UP
                                         tight → premium (one step tighter)
River:    opponent checks            →  no change  →  tier = "premium"
```

> Note: "tightening" moves toward `"premium"` (fewer, stronger hands). The tier ladder from loosest to tightest is: `random → wide → medium → tight → premium`.

---

## Game loop

`main.py` owns the outer loop. It polls the scraper, handles state changes, manages stuck detection, and coordinates periodic refreshes.

### State machine

```
                         ┌─────────────────┐
                         │   START / ENTER  │
                         └────────┬────────┘
                                  │
                         ┌────────▼────────┐
                    ┌────│  poll scraper   │◀──────────────────────────────┐
                    │    │  every 1.0 s    │                               │
                    │    └────────┬────────┘                               │
                    │             │                                         │
              state │             │ state returned                         │
              is    │      ┌──────▼──────┐                                 │
              None  │      │ new hand?   │ YES → reset last_acted_str      │
                    │      │ (hole key   │      increment hand counter      │
                    │      │  changed?)  │      if counter ≥ 30 → reload   │
                    │      └──────┬──────┘                                 │
                    │             │ NO                                      │
                    │      ┌──────▼──────┐                                 │
                    │      │ HandTracker │                                  │
                    │      │ .update()   │ returns opponent_tier           │
                    │      └──────┬──────┘                                 │
                    │             │                                         │
                    │      ┌──────▼──────┐                                 │
                    │      │ state_str   │ changed? → print + reset timer  │
                    │      │ changed?    │ same for >30s? → reload         │
                    │      └──────┬──────┘                                 │
                    │             │                                         │
                    │      ┌──────▼──────┐                                 │
                    │      │ is_my_turn? │ NO ──────────────────────────── ┘
                    │      └──────┬──────┘                                 │
                    │             │ YES                                     │
                    │      ┌──────▼──────┐                                 │
                    │      │ already     │ YES ─────────────────────────── ┘
                    │      │ acted on    │
                    │      │ this state? │
                    │      └──────┬──────┘
                    │             │ NO
                    │      ┌──────▼──────────────────────┐
                    │      │ sleep ACTION_DELAY (1.2s     │
                    │      │ + random jitter −0.4..+1.2s) │
                    │      └──────┬──────────────────────┘
                    │             │
                    │      ┌──────▼──────┐
                    │      │ re-read     │ still our turn?
                    │      │ state       │ NO ───────────────────────────── ┘
                    │      └──────┬──────┘
                    │             │ YES
                    │      ┌──────▼──────────────────────────────┐
                    │      │ engine.decide(state, opponent_tier) │
                    │      │ actions.execute(page, action, ...)  │
                    │      └──────┬──────────────────────────────┘
                    │             │
                    │      ┌──────▼──────┐
                    │      │ record      │
                    │      │ last_acted  │
                    │      └─────────────┘
                    │             │
                    └─────────────┴──────────────────────────────▶ loop
```

### Stuck detection and recovery

Two independent mechanisms detect when the bot has stalled:

| Condition | Trigger | Recovery |
|---|---|---|
| **State is None** for >30 s | The scraper can't read hole cards or stack — mid-hand animation, disconnection, or sitting out | `page.reload()`, reset all trackers |
| **State string unchanged** for >30 s | Our turn never arrived, or the DOM got into an unexpected state (e.g. raise panel frozen) | `page.reload()`, reset all trackers |

Additionally, every 30 hands the bot does a **routine reload** to prevent DOM memory growth from accumulating over a long session.

---

## DOM scraping

`scraper.py` reads all game state from the live pokernow DOM using Playwright's async locator API. Because pokernow is a React application, element classes can change between site updates — if scraping breaks, run `inspector.py` to verify selectors.

### Selector reference

| Variable | Selector | What it reads |
|---|---|---|
| `SEL_TURN` | `.table-player.you-player.decision-current` | Present when it's our turn |
| `SEL_CHECK_BTN` | `button.check` | Check button |
| `SEL_CALL_BTN` | `button.call` | Call button (also used for bet) |
| `SEL_FOLD_BTN` | `button.fold` | Fold button |
| `SEL_RAISE_BTN` | `button.raise` | Raise/bet button |
| `SEL_HOLE_CARDS` | `.you-player .card` | Our two hole cards |
| `SEL_BOARD_CARDS` | `.table-cards .card` | Community cards (0/3/4/5) |
| `SEL_CARD_RANK` | `.value` | Child span of `.card` with rank text |
| `SEL_CARD_SUIT` | `.suit:not(.sub-suit)` | Primary suit span (excludes decorative sub-suit) |
| `SEL_MY_STACK` | `.table-player.you-player .table-player-stack` | Our chip count |
| `SEL_POT_MAIN` | `.table-pot-size .main-value .chips-value` | Pot from closed streets |
| `SEL_POT_ADDON` | `.table-pot-size .add-on .chips-value` | Current-street bets |
| `SEL_BLINDS` | `.blind-value .chips-value` | [0]=small blind, [1]=big blind |
| `SEL_OTHER_PLAYERS` | `.table-player:not(.you-player):not(.table-player-seat)` | Opponent seats |

### State assembly

`get_game_state()` assembles a `GameState` in a single async call, with two early-exit guards:

1. If fewer than 2 hole cards are visible → return `None` (between hands)
2. If our stack is 0 → return `None` (transient between-hand state)

It also validates that the board card count is exactly 0, 3, 4, or 5 — a count of 1 or 2 means a card deal animation is mid-frame and the state would be incomplete.

**Pot calculation** is the sum of two separate DOM elements: `main-value` (chips already in from closed streets) and `add-on` (chips bet on the current street). pokernow keeps these split for display purposes; the bot adds them together.

**`to_call` detection** distinguishes between a true forced call and an optional bet:

```
button text = "CALL 120"  →  forced call, to_call = 120
button text = "BET 20"    →  no forced call, to_call = 0
                              (engine will decide whether to open-bet via raise path)
```

**Opponent counting** (`get_num_opponents`) excludes:
- Empty seats (`.table-player-seat` class)
- Folded players (`.fold` class on their seat element)
- Players with an empty stack display (sitting out)

Returns at least 1 to prevent division-by-zero in pot-odds math.

---

## Action execution

`actions.py` translates each engine decision into browser interactions.

```
execute(page, action, amount, pot, my_stack)
    │
    ├── "fold"   → dismiss_raise_panel() → page.click("button.fold")
    ├── "check"  → dismiss_raise_panel() → page.click("button.check")
    ├── "call"   → dismiss_raise_panel() → page.click("button.call")
    └── "raise"  → raise_amount(page, amount, pot, my_stack)
```

### Raise panel interaction

Submitting a custom raise amount goes through three stages:

```
1.  Check button.raise is enabled
    NO  →  fall back to clicking button.call (raise disallowed)
    YES →  click button.raise to open the panel

2.  Look for the text input (.raise-bet-value input[type='text'])
    FOUND →  click + Cmd+A (select all) + press_sequentially(str(amount))
             This triggers React's onChange handler so the UI validates the amount.
    NOT FOUND → fall back to preset button (see below)

3.  Click the submit button (.raise-controller-form input[type='submit'])
    DISABLED (amount rejected) →  click "Min Raise" preset + retry submit
```

### Preset fallback

If the text input isn't found, or the engine amount is below the site's minimum raise, the bot selects the **closest preset button** based on how the requested amount compares to the pot and stack:

| Condition | Preset chosen |
|---|---|
| amount ≥ 95% of stack | All In |
| amount ≥ 85% of pot | Pot |
| amount ≥ 60% of pot | 3/4 Pot |
| amount ≥ 35% of pot | 1/2 Pot |
| everything else | Min Raise |

---

## Tuning the bot

All thresholds are constants at the top of `engine.py` with inline comments. Edit them directly.

### Preflop aggressiveness

```python
PREFLOP_RAISE_THRESHOLD = 0.55   # raise if Chen score ≥ this
PREFLOP_CALL_THRESHOLD  = 0.30   # call if Chen score ≥ this (else fold)
PREFLOP_RAISE_BB_MULT   = 3      # open-raise size: N × big blind
```

- **Higher `PREFLOP_RAISE_THRESHOLD`** → bot raises fewer hands preflop (tighter open range)
- **Higher `PREFLOP_CALL_THRESHOLD`** → bot folds more hands it would have called (tighter calling range)
- **Higher `PREFLOP_RAISE_BB_MULT`** → larger open raises (more pressure, but gives up more when folded)

### Postflop aggressiveness

```python
POSTFLOP_RAISE_THRESHOLD = 0.60   # raise/bet if equity ≥ this
POSTFLOP_CALL_EDGE       = 0.05   # call if equity > pot_odds + this margin
POSTFLOP_BET_FRACTION    = 0.75   # bet this fraction of pot on raises
```

- **Lower `POSTFLOP_RAISE_THRESHOLD`** → bot bets with thinner value hands (more semi-bluffs)
- **Lower `POSTFLOP_CALL_EDGE`** → bot calls more marginal spots (looser calls)
- **`POSTFLOP_BET_FRACTION`** controls bet sizing — 0.5 is half-pot, 1.0 is pot

### Simulation speed vs accuracy

```python
MONTE_CARLO_SIMS = 1500
```

1,500 trials takes ~100 ms on a modern machine and is accurate to within ±2% equity. At 500 sims it's still within ±4% and much faster; at 5,000 it's within ±1% but noticeably slower per decision.

---

## DOM inspector

Before running the bot for the first time (or after a pokernow site update), verify that the selectors in `scraper.py` still match the live DOM:

```bash
python3 inspector.py https://www.pokernow.com/games/<game-id>
```

The inspector opens the page, waits for you to join, then prints:

- Every `.table-player` seat: classes, player name, stack value
- Each hole card and board card's raw inner HTML
- Pot element values for both main and add-on selectors
- Which action buttons are present and enabled

When prompted a second time, **manually open the raise panel** in the browser and press ENTER — this lets the inspector dump the raise form inputs so you can verify `SEL_RAISE_INPUT` and `SEL_RAISE_SUBMIT` in `actions.py`.

---

## Known limitations

**No position awareness.** The bot plays the same strategy from every position at the table. A significant EV improvement would be widening the raise range on the button and tightening it from under-the-gun, since late position provides informational advantage on every postflop street.

**Single opponent assumed.** The Monte Carlo simulation handles `num_opponents > 1` by sampling one opponent hand per trial. With multiple active opponents, true equity is lower because you need to beat *all* of them — the current model slightly overestimates equity in multi-way pots.

**No fold equity or bluffing model.** The bot only considers its raw equity against an opponent's range. It doesn't model the extra value of a bet that might fold out hands with equity (fold equity), so it will sometimes check hands that would profit from a semi-bluff.

**No multi-street planning.** Decisions are made independently on each street. The bot doesn't account for implied odds (calling a small bet to stack the opponent on a later street when a draw completes), nor does it plan its river range from flop decisions.

**Raise input selector fragility.** pokernow's raise panel UI has changed in past site updates. If raises aren't executing correctly, run `inspector.py`, open the raise panel manually, and verify `SEL_RAISE_INPUT` and `SEL_RAISE_SUBMIT` against what the inspector prints.

---

## Files at a glance

### `state.py`

Defines two dataclasses:

- **`Card(rank, suit)`** — a single playing card. `Card.from_pokernow()` maps pokernow's raw strings (including Unicode suit symbols ♠ ♥ ♦ ♣) to the single-character format (`s h d c`) that the `treys` library expects. `to_treys()` serialises to a two-character string like `"As"` or `"Td"`.

- **`GameState`** — a snapshot of all game-relevant information at a single point in time. `pot_odds()` computes `to_call / (pot + to_call)` — the minimum equity needed for a call to break even in expectation.

### `scraper.py`

All DOM reads go through Playwright's async `Page.locator()` API. `_parse_chips()` handles every chip format pokernow uses: plain integers, comma-separated thousands, `K` suffix, and decimal cents. `get_game_state()` is the single public entry point — it assembles a complete `GameState` or returns `None` if the state is unreadable.

### `engine.py`

`_chen_score()` implements the full Chen formula and normalises to [0, 1]. `monte_carlo_equity()` runs the range-weighted simulation described above. `decide()` routes to the correct evaluator based on street and applies the threshold logic from the [decision flowchart](#decision-flowchart).

### `hand_tracker.py`

`HandTracker` maintains state across a single hand: the preflop tier baseline, current tier after postflop tightening, which streets have been seen, and whether a bet occurred on the current street. `update()` is called once per decision cycle and always returns the current range tier string.

### `ranges.py`

Defines five range sets as collections of canonical hand type tuples `(high_rank, low_rank, suited)`. At module load time, all five ranges are **expanded into specific card combinations** (e.g. `"AKs"` → `{As Ks, Ah Kh, Ad Kd, Ac Kc}`) and converted to `treys` integer representations. `get_combos(tier, exclude)` returns the filtered combo list for a given tier, excluding any cards already visible on the table.

### `actions.py`

`execute()` dispatches to `fold()`, `check()`, `call()`, or `raise_amount()`. All action functions call `dismiss_raise_panel()` first to ensure any previously open raise form is closed before attempting a new click. `raise_amount()` tries the primary text-input path before falling back to preset buttons.

### `main.py`

The outer loop. Key constants at the top:

```python
POLL_INTERVAL   = 1.0   # seconds between state polls
ACTION_DELAY    = 1.2   # base delay before acting (plus random jitter)
DEBUG           = True  # print diagnostic messages
REFRESH_EVERY   = 30    # reload page every N hands (prevents DOM bloat)
STUCK_TIMEOUT   = 30    # seconds of no state change before reloading
HEARTBEAT_EVERY = 10    # seconds between "still alive" log messages
```
