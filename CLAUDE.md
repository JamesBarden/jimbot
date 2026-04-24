# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (one-time setup)
pip3 install --user -r requirements.txt
python3 -m playwright install chromium

# Run the bot
python3 main.py <pokernow_game_url>

# Run the DOM inspector (use this to verify/fix selectors after a pokernow site update)
python3 inspector.py <pokernow_game_url>
```

### GTO lookup table (one-time offline setup)

The bot uses pre-computed TexasSolver strategies for flop decisions when available.
Without the lookup table it falls back to Monte Carlo equity on the flop.
Turn and river always use their own heuristics regardless.

```bash
# 1. Download TexasSolver binary from: https://github.com/bupticybee/TexasSolver/releases
#    Unzip into pokernow-bot/TexasSolver/ and chmod +x TexasSolver/console_solver
#    (ARM64 Mac: native binary is at TexasSolver/console_solver_arm64)

# 2. Run the solver for all 108 representative flop spots (~2 hours at --parallel 2)
python3 scripts/presolve.py

# 3. Compile raw solver output into the runtime lookup table
python3 scripts/build_lookup.py

# 4. (Optional) Inspect raw solver output format
python3 scripts/inspect_output.py solutions/raw/<any_spot>.json
```

There is no test suite or linter configured.

## Architecture

The bot is a one-second poll loop with a linear pipeline per tick:

```
DOM (Playwright) → scraper.py → GameState → engine.py → (action, amount) → actions.py → clicks
                                                ↑
                                         hand_tracker.py
                                         (opponent_tier)
```

**`main.py`** owns the loop. It handles: detecting new hands (by comparing hole card keys), preventing double-acting on the same state (`last_acted_str`), adding human-like action delay with random jitter, reloading after 30 hands (DOM bloat prevention), and reloading on 30s stuck timeouts (no state change or all-None).

**`scraper.py`** assembles `GameState` from the live DOM. Returns `None` between hands. Two guards cause a `None` return mid-hand: fewer than 2 hole cards visible (deal animation), or board card count not in {0, 3, 4, 5} (community card animation mid-frame). Pot is the sum of two DOM elements: `main-value` (closed streets) + `add-on` (current street). The call button text distinguishes `"CALL 120"` (forced, `to_call=120`) from `"BET 20"` (optional, `to_call=0`). Position is detected by reading the dealer button's `dealer-position-N` class, finding our `table-player-N` class, sorting all seat numbers to determine clockwise order, and computing the offset.

**`engine.py`** makes all decisions. Decision priority per street:
1. **Flop** → `solver_lookup.py` (TexasSolver pre-computed strategies). On miss → Monte Carlo fallback.
2. **Preflop** → `preflop_ranges.py` (position-aware GTO tables, always hits).
3. **Turn** → `turn_heuristic.py` (5-layer pseudo-GTO model, always hits).
4. **River** → `river_heuristic.py` (5-layer pseudo-GTO model, always hits).
5. **Monte Carlo** → fallback only (flop solver miss or unexpected phase).

All decisions use mixed-strategy randomisation: each source returns `(raise_freq, call_freq, fold_freq)` and a random roll selects the action. Raise amounts are sampled from {33%, 66%, 100%, 150%} pot via `bet_sizing.py`. Preflop uses BB-multiple sizing instead. Every decision prints a full bordered log box showing all intermediate values.

**`preflop_ranges.py`** contains two table families per position: `_RFI` (open-raise frequencies) and `_VS_RAISE` (3bet/call/fold frequencies). BB has no RFI table. SB VS_RAISE is 3bet-or-fold only (no flatting OOP). All other positions have flatting ranges built in.

**`solver_lookup.py`** loads `solutions/lookup.pkl` at startup and answers flop queries in O(1). Key: `(board_texture, spr_bucket, villain_tier, facing_bet)`. Within each spot, strategies are indexed by hand class. Falls back through villain tier relaxation, then nearest-class by strength, before returning `None`. Maps hand_tracker's 5 tiers to the 3 the solver was built for: premium/tight→tight, medium→medium, wide/random→wide.

**`hand_classifier.py`** classifies hole cards against the board into one of 15 hand classes: `monster, full_house, flush, straight, set, two_pair, overpair, top_pair_top, top_pair_weak, middle_pair, bottom_pair, combo_draw, draw, weak_draw, air`. Also provides: `board_texture()` (flop texture string in format `{connectivity}_{flush}_{height}`), `turn_card_texture()` (classifies board[3] relative to board[:3]: pair_board / flush_complete / straight_complete / overcard / blank), `river_card_texture()` (classifies board[4] relative to board[:4]: trips_board / pair_board / flush_complete / straight_complete / overcard / blank).

**`turn_heuristic.py`** and **`river_heuristic.py`** each implement a 5-layer frequency model:
1. Base frequencies from `_FREE` (no bet facing) or `_FACED` (facing a bet) table, indexed by `(hand_class, texture)`
2. Position: ±5% raise for IP/OOP
3. SPR: short-stack commits more, deep-stack tightens
4. Villain tier: fold adjustment for marginal hands only
5. Bet sizing tier: fold multiplier and raise adjustment based on `to_call / pot` classified as small/medium/large/overbet

River differences from turn: no semi-bluffs (draws resolved), higher base fold on marginal hands, air bluffs at 20-25%, `trips_board` texture, SPR thresholds at <2 and >8 instead of <4 and >10.

**`bet_sizing.py`** provides `pick_bet_fraction(hand_class, board_cards, facing_bet)` and `pick_preflop_size(position, facing_raise, to_call, bb)`. Postflop samples from {0.33, 0.66, 1.00, 1.50} × pot using hand-class weight tables modified by board wetness. Air and monster/nutted hands are both weighted toward larger sizes (polarised sizing). Preflop samples from BB multiples by position.

**`hand_tracker.py`** infers opponent range from state transitions (not an action stream). Preflop: `to_call / big_blind` ratio maps to one of five tiers. Postflop: each street where `to_call > 0` tightens the tier one step toward `premium`. Reset at start of each new hand.

**`ranges.py`** defines five hand range tiers as sets of canonical `(high_rank, low_rank, suited)` tuples. Expanded into full specific-combo lists at module load. `get_combos(tier, exclude)` filters out known cards before returning the pool for Monte Carlo sampling.

**`actions.py`** handles raise panel interaction: clicks `button.raise` → types amount into `.raise-bet-value input[type='text']` (using `Meta+a` + `press_sequentially` to trigger React's onChange) → submits. Falls back to the closest preset button if the text input isn't found. Falls back to Min Raise if the submitted amount is rejected by the site.

**`scripts/presolve.py`** generates TexasSolver command files for 108 representative spots (12 board textures × 3 SPR buckets × 3 villain tiers) and runs the solver. Auto-detects ARM64 native binary. Outputs raw JSON to `solutions/raw/`. Skips spots already solved.

**`scripts/build_lookup.py`** parses TexasSolver JSON outputs, classifies each combo against the board into a hand class, averages action frequencies within each class, and writes `solutions/lookup.pkl`.

## Key fragility: DOM selectors

pokernow is a React app and class names change between site updates. All selectors live in `scraper.py` at the top of the file. If the bot stops reading cards, pot, or buttons correctly, run `inspector.py` to dump the live DOM and compare against the selectors. The raise input selectors (`SEL_RAISE_INPUT`, `SEL_RAISE_SUBMIT`) are in `actions.py` and are separately fragile — the inspector prompts you to open the raise panel manually so it can dump those too.
