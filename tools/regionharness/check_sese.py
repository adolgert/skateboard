#!/usr/bin/env python3
"""VAL-1 gate: mechanical SESE check for a region spec.

Scans the anchor's line range and the closure line ranges for control-flow
constructs that would break single-entry/single-exit or contradict the spec's
absent_obstructions: goto, early return, entry, stop/error stop.

A RETURN whose next non-blank, non-comment line is an END statement is the
subroutine's terminal exit, reported as OK rather than a violation.

Usage: check_sese.py <region.yaml> [--repo-root DIR]
Exit 0 iff no violations.
"""
import argparse
import re
import sys
from pathlib import Path

import yaml

KEYWORDS = re.compile(r"\b(go\s*to|return|entry|error\s+stop|stop)\b", re.IGNORECASE)
END_STMT = re.compile(r"^\s*end\b", re.IGNORECASE)

STRING = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")


def code_part(line: str) -> str:
    """Strip string literals, then the trailing ! comment."""
    no_str = STRING.sub("''", line)
    return no_str.split("!", 1)[0]


def parse_range(spec: str):
    a, b = spec.split("-")
    return int(a), int(b)


def next_code_line(lines, idx):
    for j in range(idx + 1, len(lines)):
        stripped = code_part(lines[j]).strip()
        if stripped:
            return stripped
    return ""


def scan(lines, lo, hi, label):
    violations, notes = [], []
    for i in range(lo - 1, min(hi, len(lines))):
        code = code_part(lines[i])
        for m in KEYWORDS.finditer(code):
            kw = re.sub(r"\s+", " ", m.group(1).lower())
            if kw == "return" and END_STMT.match(next_code_line(lines, i)):
                notes.append(f"  {label}:{i + 1}: terminal RETURN (ok): {lines[i].strip()}")
            else:
                violations.append(f"  {label}:{i + 1}: {kw.upper()}: {lines[i].strip()}")
    return violations, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("region_yaml")
    ap.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    args = ap.parse_args()

    spec = yaml.safe_load(Path(args.region_yaml).read_text())
    anchor = spec["anchor"]
    src_path = Path(args.repo_root) / anchor["file"]
    lines = src_path.read_text().splitlines()

    ranges = []
    m = re.search(r"@(\d+)-(\d+)", anchor["pst_node"])
    ranges.append((int(m.group(1)), int(m.group(2)), anchor.get("entry_symbol", "anchor")))
    for callee in spec.get("closure", {}).get("callees", []):
        lo, hi = parse_range(callee["lines"])
        ranges.append((lo, hi, callee["name"]))

    all_violations, all_notes = [], []
    for lo, hi, label in ranges:
        v, n = scan(lines, lo, hi, label)
        all_violations += v
        all_notes += n

    total = sum(hi - lo + 1 for lo, hi, _ in ranges)
    print(f"VAL-1 SESE check: {src_path.name}, {len(ranges)} ranges, {total} lines")
    for note in all_notes:
        print(note)
    if all_violations:
        print(f"FAIL: {len(all_violations)} violation(s)")
        for v in all_violations:
            print(v)
        return 1
    print("PASS: no goto / early return / entry / stop in region or closure")
    return 0


if __name__ == "__main__":
    sys.exit(main())
