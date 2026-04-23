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

**`scraper.py`** assembles `GameState` from the live DOM. Returns `None` between hands. The two guards that cause a `None` return mid-hand: fewer than 2 hole cards visible (deal animation), or board card count not in {0, 3, 4, 5} (community card animation mid-frame). Pot is the sum of two separate DOM elements: `main-value` (closed streets) + `add-on` (current street). The call button text distinguishes `"CALL 120"` (forced, `to_call=120`) from `"BET 20"` (optional open-bet, `to_call=0`).

**`engine.py`** makes all decisions. Preflop uses the Chen formula (normalised 0–1); postflop uses range-weighted Monte Carlo (1500 trials). All tuning constants are at the top of this file: `PREFLOP_RAISE_THRESHOLD`, `PREFLOP_CALL_THRESHOLD`, `POSTFLOP_RAISE_THRESHOLD`, `POSTFLOP_CALL_EDGE`, `POSTFLOP_BET_FRACTION`, `MONTE_CARLO_SIMS`, `PREFLOP_RAISE_BB_MULT`. The engine always checks `can_check` before folding — a free check is always preferred over a fold.

**`hand_tracker.py`** infers opponent range from state transitions (not an action stream). Preflop: `to_call / big_blind` ratio maps to one of five tiers (`premium`/`tight`/`medium`/`wide`/`random`). Postflop: each street where `to_call > 0` tightens the tier one step toward `premium`. The tier is reset at the start of each new hand.

**`ranges.py`** defines five hand range tiers as sets of canonical `(high_rank, low_rank, suited)` tuples. These are expanded into full specific-combo lists (treys int pairs) at module load time. `get_combos(tier, exclude)` filters out cards already on the table before returning the pool for Monte Carlo sampling.

**`actions.py`** handles raise panel interaction: clicks `button.raise` → types amount into `.raise-bet-value input[type='text']` (using `Meta+a` + `press_sequentially` to trigger React's onChange) → submits. Falls back to the closest preset button if the text input isn't found. Falls back to Min Raise if the submitted amount is rejected by the site.

## Key fragility: DOM selectors

pokernow is a React app and class names change between site updates. All selectors live in `scraper.py` at the top of the file. If the bot stops reading cards, pot, or buttons correctly, run `inspector.py` to dump the live DOM and compare against the selectors. The raise input selectors (`SEL_RAISE_INPUT`, `SEL_RAISE_SUBMIT`) are in `actions.py` and are separately fragile — the inspector prompts you to open the raise panel manually so it can dump those too.
