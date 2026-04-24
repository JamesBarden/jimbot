"""
Inspect the structure of a raw TexasSolver JSON output file.

Usage:
  python3 scripts/inspect_output.py solutions/raw/<spot>.json

Prints the top-level keys, the root node structure, and a sample of
combo → strategy data so you can verify the parser in build_lookup.py
works for your solver version.
"""

import json
import sys
import os


def describe(obj, depth=0, max_depth=4, max_list=5):
    prefix = "  " * depth
    if depth > max_depth:
        return prefix + "..."
    if isinstance(obj, dict):
        lines = [prefix + "{"]
        for k, v in list(obj.items())[:max_list]:
            lines.append(f"{prefix}  {repr(k)}: {describe(v, depth+1, max_depth, max_list)}")
        if len(obj) > max_list:
            lines.append(f"{prefix}  ... ({len(obj)} keys total)")
        lines.append(prefix + "}")
        return "\n".join(lines)
    if isinstance(obj, list):
        if not obj:
            return prefix + "[]"
        sample = obj[:max_list]
        inner = ", ".join(describe(x, 0, 1, 3) for x in sample)
        more = f"  ...+{len(obj)-max_list}" if len(obj) > max_list else ""
        return f"[{inner}{more}] (len={len(obj)})"
    return repr(obj)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/inspect_output.py <json_file>")
        sys.exit(1)

    path = sys.argv[1]
    if not os.path.isfile(path):
        print(f"File not found: {path}")
        sys.exit(1)

    print(f"Loading {path} ...")
    with open(path) as f:
        data = json.load(f)

    print("\n=== Top-level structure ===")
    print(describe(data, max_depth=3, max_list=10))

    # Try to find root node strategy
    print("\n=== Attempting to locate root strategy ===")
    _find_strategy(data, path=[])


def _find_strategy(node, path, found=None):
    if found is None:
        found = [False]
    if found[0]:
        return

    if isinstance(node, dict):
        keys = set(node.keys())

        # Look for a node with strategy + combos/player
        if "strategy" in keys:
            found[0] = True
            print(f"  Found 'strategy' at path: {' → '.join(str(p) for p in path) or 'root'}")
            for k in ("player", "actions", "combos"):
                if k in node:
                    val = node[k]
                    if isinstance(val, list) and len(val) > 5:
                        print(f"  {k}: {val[:5]} ... (len={len(val)})")
                    else:
                        print(f"  {k}: {val}")
            strat = node["strategy"]
            if isinstance(strat, list) and strat:
                print(f"  strategy: len={len(strat)}, first entry={strat[0]}")
            return

        for k, v in node.items():
            _find_strategy(v, path + [k], found)
            if found[0]:
                return

    elif isinstance(node, list):
        for i, item in enumerate(node[:3]):
            _find_strategy(item, path + [i], found)
            if found[0]:
                return


if __name__ == "__main__":
    main()
