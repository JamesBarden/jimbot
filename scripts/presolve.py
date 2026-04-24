"""
Pre-compute GTO flop strategies for a set of representative spots using TexasSolver.

Usage:
  python3 scripts/presolve.py [--threads N] [--accuracy F] [--iters N]

Outputs one JSON file per spot to solutions/raw/.
Run scripts/build_lookup.py afterwards to compile these into the runtime table.

TexasSolver binary must be at:  ./TexasSolver/console_solver
Download from: https://github.com/bupticybee/TexasSolver/releases
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Spot definitions ─────────────────────────────────────────────────────────

# Representative boards: one per texture category used by hand_classifier.board_texture()
BOARDS = {
    "dry_rainbow_high":        "Ah,7d,2c",
    "dry_rainbow_mid":         "9h,5d,2c",
    "dry_rainbow_low":         "7h,4d,2c",
    "dry_twotone_high":        "Kh,Qd,3h",
    "dry_twotone_mid":         "9h,5d,3h",
    "dry_monotone_high":       "Kh,9h,4h",
    "dry_monotone_mid":        "8h,5h,2h",
    "connected_rainbow_high":  "Jh,Td,9c",
    "connected_rainbow_mid":   "8h,7d,6c",
    "connected_twotone_high":  "Jh,Td,9h",
    "paired_rainbow_high":     "Ah,As,7d",
    "paired_rainbow_mid":      "7h,7d,2c",
}

# SPR (stack-to-pot) buckets — pot is always 100 to simplify bet-size fractions.
# Stack = effective stack remaining (not counting pot).
SPR_BUCKETS = {
    "low":    200,   # SPR ~2
    "medium": 700,   # SPR ~7
    "high":  1500,   # SPR ~15
}

# Villain ranges (will be used as OOP range; hero is IP with a wide range)
VILLAIN_RANGES = {
    "tight": (
        "AA,KK,QQ,JJ,TT,99,88,77,"
        "AKs,AQs,AJs,ATs,KQs,QJs,JTs,"
        "AKo,AQo,AJo,KQo"
    ),
    "medium": (
        "AA,KK,QQ,JJ,TT,99,88,77,66,55,"
        "AKs,AQs,AJs,ATs,A9s,A8s,KQs,KJs,KTs,QJs,QTs,JTs,T9s,98s,87s,76s,65s,"
        "AKo,AQo,AJo,ATo,KQo,KJo,QJo,JTo"
    ),
    "wide": (
        "AA,KK,QQ,JJ,TT,99,88,77,66,55,44,33,22,"
        "AKs,AQs,AJs,ATs,A9s,A8s,A7s,A6s,A5s,A4s,A3s,A2s,"
        "KQs,KJs,KTs,K9s,K8s,QJs,QTs,Q9s,JTs,J9s,J8s,T9s,T8s,98s,97s,87s,86s,76s,75s,65s,64s,54s,53s,43s,"
        "AKo,AQo,AJo,ATo,A9o,KQo,KJo,KTo,QJo,QTo,JTo,T9o,98o,87o,76o,65o"
    ),
}

# Hero range (IP): broad range representing all hands we could hold
HERO_RANGE = (
    "AA,KK,QQ,JJ,TT,99,88,77,66,55,44,33,22,"
    "AKs,AQs,AJs,ATs,A9s,A8s,A7s,A6s,A5s,A4s,A3s,A2s,"
    "KQs,KJs,KTs,K9s,K8s,K7s,K6s,K5s,K4s,K3s,K2s,"
    "QJs,QTs,Q9s,Q8s,Q7s,JTs,J9s,J8s,T9s,T8s,98s,97s,87s,86s,76s,75s,65s,64s,54s,53s,43s,"
    "AKo,AQo,AJo,ATo,A9o,A8o,A7o,A6o,A5o,A4o,A3o,A2o,"
    "KQo,KJo,KTo,K9o,K8o,K7o,K6o,QJo,QTo,Q9o,JTo,J9o,T9o,98o,87o,76o,65o,54o"
)

# ── Config builder ────────────────────────────────────────────────────────────

def make_config(board: str, stack: int, villain_range: str,
                tmp_json: str, threads: int, accuracy: float, iters: int) -> str:
    """Return a TexasSolver command-file string for one spot.

    IMPORTANT: tmp_json must be a path with NO spaces — TexasSolver splits
    dump_result on whitespace. We always write to /tmp/ and move afterwards.

    TexasSolver v0.2.0 console syntax uses commas between ALL set_bet_sizes params:
      set_bet_sizes <position>,<street>,<action>,<size1>,<size2>,...
    """
    lines = [
        f"set_pot 100",
        f"set_effective_stack {stack}",
        f"set_board {board}",
        f"set_range_ip {HERO_RANGE}",
        f"set_range_oop {villain_range}",
        # Bet sizes: 50% and 100% pot for both players, all streets
        "set_bet_sizes ip,flop,bet,0.5,1",
        "set_bet_sizes ip,turn,bet,0.5,1",
        "set_bet_sizes ip,river,bet,0.5,1",
        "set_bet_sizes oop,flop,bet,0.5,1",
        "set_bet_sizes oop,turn,bet,0.5,1",
        "set_bet_sizes oop,river,bet,0.5,1",
        # Raise sizes: 2.5× the bet for both players, all streets
        "set_bet_sizes ip,flop,raise,2.5",
        "set_bet_sizes ip,turn,raise,2.5",
        "set_bet_sizes ip,river,raise,2.5",
        "set_bet_sizes oop,flop,raise,2.5",
        "set_bet_sizes oop,turn,raise,2.5",
        "set_bet_sizes oop,river,raise,2.5",
        f"set_allin_threshold 0.67",
        f"set_thread_num {threads}",
        f"set_accuracy {accuracy}",
        f"set_max_iteration {iters}",
        f"set_dump_rounds 1",     # flop strategy only — keeps JSON small
        f"build_tree",
        f"start_solve",
        f"dump_result {tmp_json}",   # space-free /tmp path
    ]
    return "\n".join(lines)


# ── Worker ───────────────────────────────────────────────────────────────────

def _solve_spot(spot_id, board, stack, vil_range, solver_bin, raw_dir,
                solver_threads, accuracy, iters):
    """
    Solve one spot in a worker thread. Returns (spot_id, status, elapsed, size_kb).
    status is one of: 'skip', 'ok', 'fail'

    Each worker runs its own single-threaded solver process — safe under
    Rosetta 2. Run multiple workers via --parallel for process-level speedup.
    """
    out_json = os.path.join(raw_dir, f"{spot_id}.json")
    if os.path.isfile(out_json):
        return spot_id, "skip", 0, 0

    # /tmp paths have no spaces — TexasSolver splits dump_result on whitespace
    tmp_json = f"/tmp/txsolver_{spot_id}.json"
    cfg_file = f"/tmp/txsolver_{spot_id}.txt"

    config = make_config(board, stack, vil_range, tmp_json,
                         solver_threads, accuracy, iters)
    with open(cfg_file, "w") as f:
        f.write(config)

    t0 = time.time()
    result = subprocess.run([solver_bin, "-i", cfg_file],
                            capture_output=True, text=True)
    elapsed = time.time() - t0

    if result.returncode != 0:
        err = (result.stderr or "")[-300:]
        return spot_id, f"fail:{err}", elapsed, 0

    if not os.path.isfile(tmp_json):
        return spot_id, "fail:no_json", elapsed, 0

    shutil.move(tmp_json, out_json)
    return spot_id, "ok", elapsed, os.path.getsize(out_json) // 1024


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pre-compute GTO flop strategies")
    parser.add_argument("--threads",  type=int,   default=1,
                        help="solver threads per instance (default 1 — multi-thread "
                             "crashes on Apple Silicon with Rosetta 2 x86 binary; "
                             "use --parallel instead for speedup)")
    parser.add_argument("--parallel", type=int,   default=4,
                        help="number of solver instances to run simultaneously "
                             "(default 4 for ARM64 native binary; reduce if RAM is tight — "
                             "each instance uses ~2-4GB RAM at iters=50)")
    parser.add_argument("--accuracy", type=float, default=0.5,
                        help="convergence accuracy (lower = faster; 0.5 reaches "
                             "<0.5%% exploitability in ~3 min per spot)")
    parser.add_argument("--iters",    type=int,   default=50,
                        help="max CFR iterations")
    parser.add_argument("--dry-run",  action="store_true",
                        help="print configs without running the solver")
    args = parser.parse_args()

    script_dir  = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    # Prefer native ARM64 binary (no Rosetta overhead, safe multi-threading via --parallel)
    arm64_bin   = os.path.join(project_dir, "TexasSolver", "console_solver_arm64")
    x86_bin     = os.path.join(project_dir, "TexasSolver", "console_solver")
    solver_bin  = arm64_bin if os.path.isfile(arm64_bin) else x86_bin
    raw_dir     = os.path.join(project_dir, "solutions", "raw")
    os.makedirs(raw_dir, exist_ok=True)

    if not args.dry_run and not os.path.isfile(solver_bin):
        print(f"ERROR: TexasSolver binary not found. Expected at:")
        print(f"  {arm64_bin}  (native ARM64 — preferred)")
        print(f"  {x86_bin}  (x86 Rosetta fallback)")
        print("Download from: https://github.com/bupticybee/TexasSolver/releases")
        sys.exit(1)

    if not args.dry_run:
        arch = "arm64" if solver_bin == arm64_bin else "x86 (Rosetta)"
        print(f"Using solver binary: {solver_bin}  [{arch}]")

    # Build the full work list
    spots = [
        (f"{bk}__{sk}__{vk}", board, stack, vil_range)
        for bk, board in BOARDS.items()
        for sk, stack in SPR_BUCKETS.items()
        for vk, vil_range in VILLAIN_RANGES.items()
    ]
    total = len(spots)

    if args.dry_run:
        for spot_id, board, stack, vil_range in spots:
            tmp_json = f"/tmp/txsolver_{spot_id}.json"
            print(f"\n=== {spot_id} ===")
            print(make_config(board, stack, vil_range, tmp_json,
                              args.threads, args.accuracy, args.iters))
        return

    print(f"Solving {total} spots  |  {args.parallel} parallel workers  "
          f"|  {args.threads} solver thread(s) each  |  max {args.iters} iters")
    print()

    completed = skipped = failed = 0
    start_all = time.time()

    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        future_to_spot = {
            pool.submit(_solve_spot,
                        spot_id, board, stack, vil_range,
                        solver_bin, raw_dir,
                        args.threads, args.accuracy, args.iters): spot_id
            for spot_id, board, stack, vil_range in spots
        }

        for future in as_completed(future_to_spot):
            spot_id, status, elapsed, size_kb = future.result()
            completed += 1

            if status == "skip":
                skipped += 1
                print(f"  [{completed}/{total}] SKIP  {spot_id}")
            elif status == "ok":
                rate = (completed - skipped) / ((time.time() - start_all) / 60)
                remaining = (total - completed) / rate if rate > 0 else 0
                print(f"  [{completed}/{total}] ok    {spot_id}  "
                      f"{elapsed:.0f}s  {size_kb}KB  "
                      f"(~{remaining:.0f} min left)")
            else:
                failed += 1
                print(f"  [{completed}/{total}] FAIL  {spot_id}  {elapsed:.0f}s")
                print(f"    {status}")

    elapsed_total = (time.time() - start_all) / 60
    solved = completed - skipped - failed
    print(f"\nFinished in {elapsed_total:.0f} min — "
          f"{solved} solved, {skipped} skipped, {failed} failed")
    if solved + skipped > 0:
        print("Run:  python3 scripts/build_lookup.py")


if __name__ == "__main__":
    main()
