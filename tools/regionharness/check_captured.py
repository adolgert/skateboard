#!/usr/bin/env python3
"""VAL-2 gate: captured field set vs the region spec's footprint.

For every captured case directory, asserts:
  - input fields  == spec live_in with src: argument (exactly, no extras)
  - output fields == spec live_out + clobbers.arrays  (exactly, no extras)

An extra captured field would mean the instrumentation serializes state outside
the spec footprint; a missing one means a capture failed silently. The strong
half of VAL-2 (no *uncaptured* state influences the region) is witnessed by the
replay harness itself: it initializes only the spec footprint and reproduces
outputs bitwise.

Usage: check_captured.py <region.yaml> <data_dir>   # e.g. ftgdata/ftg_n4pes_test
"""
import json
import sys
from pathlib import Path

import yaml


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    spec = yaml.safe_load(Path(sys.argv[1]).read_text())
    data = Path(sys.argv[2])
    entry = spec["anchor"]["entry_symbol"]

    want_in = {f["name"].lower() for f in spec.get("live_in", [])
               if f.get("src") == "argument"}
    want_out = {f["name"].lower() for f in spec.get("live_out", [])}
    want_out |= {f["name"].lower() for f in spec.get("clobbers", {}).get("arrays", [])}

    cases = sorted(d for d in data.iterdir() if d.is_dir() and d.name.startswith("r"))
    if not cases:
        print(f"FAIL: no case directories under {data}")
        return 1

    bad = 0
    for case in cases:
        for stage, want in (("input", want_in), ("output", want_out)):
            meta = case / stage / f"MetaData-{entry}.json"
            if not meta.exists():
                print(f"FAIL: {case.name}/{stage}: missing {meta.name}")
                bad += 1
                continue
            got = {k.lower() for k in json.loads(meta.read_text())["field_map"]}
            if got != want:
                bad += 1
                extra, missing = got - want, want - got
                print(f"FAIL: {case.name}/{stage}: extra={sorted(extra) or '-'} "
                      f"missing={sorted(missing) or '-'}")

    print(f"checked {len(cases)} cases: input == {sorted(want_in)}, "
          f"output == {sorted(want_out)}")
    if bad:
        print(f"FAIL: {bad} stage mismatch(es)")
        return 1
    print("PASS: every captured field set matches the spec footprint exactly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
