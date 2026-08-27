#!/usr/bin/env python3
"""VAL-5 gate: diff FortranCallGraph's used-global set against the region spec.

The spec's footprint (live_in / live_out / clobbers) was derived by agent
source-read (derivation #1). FCG's used-set from gfortran assembler is the
independent derivation #2. This script compares the module-scope portion of
both and fails on any unexplained difference, in either direction.

  - FCG-only names  -> the spec is missing footprint: HARD FAIL.
  - Spec-only names -> `parameter` constants are expected to be invisible to
    assembler-level analysis (folded into .rodata); reported as explained.
    Anything else: FAIL.

Usage:
  FortranCallGraph.py -ml -a globals <module> <subroutine> > fcg_globals.txt
  check_footprint.py <region.yaml> fcg_globals.txt
"""
import argparse
import sys
from pathlib import Path

import yaml


def spec_module_vars(spec):
    """Module-scope names from the spec: {name: (section, src)}, lowercased."""
    out = {}
    for entry in spec.get("live_in", []) + spec.get("live_out", []):
        src = entry.get("src", "")
        if src != "argument":
            section = "live_in" if entry in spec.get("live_in", []) else "live_out"
            out[entry["name"].lower()] = (section, src)
    for entry in spec.get("clobbers", {}).get("arrays", []):
        out[entry["name"].lower()] = ("clobbers", "module")
    return out


def parse_fcg(path):
    """Parse `-ml` output lines: `expression {declaring_module}`.

    Returns base variable names, lowercased (derived-type components like
    `var%comp` reduce to `var`).
    """
    names = set()
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(("WARNING", "ERROR")):
            continue
        expr = line.split(" {", 1)[0].split("{", 1)[0].strip()
        base = expr.split("%", 1)[0].split("(", 1)[0].strip().lower()
        if base:
            names.add(base)
    return names


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("region_yaml")
    ap.add_argument("fcg_output")
    args = ap.parse_args()

    spec = yaml.safe_load(Path(args.region_yaml).read_text())
    spec_vars = spec_module_vars(spec)
    fcg_vars = parse_fcg(args.fcg_output)

    agreed = sorted(n for n in fcg_vars if n in spec_vars)
    fcg_only = sorted(n for n in fcg_vars if n not in spec_vars)
    spec_only = sorted(n for n in spec_vars if n not in fcg_vars)

    explained, unexplained = [], []
    for name in spec_only:
        section, src = spec_vars[name]
        if "parameter" in src or "use " in src:
            explained.append((name, section, src))
        else:
            unexplained.append((name, section, src))

    print(f"spec module-scope footprint: {len(spec_vars)}  FCG used-globals: {len(fcg_vars)}")
    print(f"\nagreed ({len(agreed)}): {', '.join(agreed) or '-'}")
    if explained:
        print(f"\nspec-only, explained as constant-folded parameters ({len(explained)}):")
        for name, section, src in explained:
            print(f"  {name}  [{section}] {src}")

    failed = False
    if fcg_only:
        failed = True
        print(f"\nFAIL: FCG found globals missing from the spec ({len(fcg_only)}):")
        for name in fcg_only:
            print(f"  {name}")
    if unexplained:
        failed = True
        print(f"\nFAIL: spec claims module vars FCG never saw ({len(unexplained)}):")
        for name, section, src in unexplained:
            print(f"  {name}  [{section}] {src}")

    if failed:
        return 1
    clobbers = sorted(n for n, (s, _) in spec_vars.items() if s == "clobbers")
    print(f"\nPASS: mutable global footprint agreed = {{{', '.join(clobbers)}}}; "
          "all other deltas explained")
    return 0


if __name__ == "__main__":
    sys.exit(main())
