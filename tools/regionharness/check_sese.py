#!/usr/bin/env python3
"""Mechanical SESE check for a region spec.

SESE = single-entry, single-exit.
Scans the anchor's line range and the closure line ranges for control-flow
constructs that would break single-entry/single-exit or contradict the spec's
absent_obstructions: goto, early return, entry, stop/error stop.

A RETURN whose next non-blank, non-comment line is an END statement is the
subroutine's terminal exit, reported as OK rather than a violation.

Usage: check_sese.py <region.yaml> [--repo-root DIR] [--json]
Exit 0 iff no violations.

--json prints a single machine-readable object instead of the human report
(same verdict, same exit code) -- this is the contract a caller like
equivalent/components/sese_check.py parses; the default text output is
unchanged and is what tools/regionharness/README.md's examples show.
"""
import argparse
import json
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
            item = {"label": label, "line": i + 1, "keyword": kw, "text": lines[i].strip()}
            if kw == "return" and END_STMT.match(next_code_line(lines, i)):
                item["note"] = "terminal RETURN (ok)"
                notes.append(item)
            else:
                violations.append(item)
    return violations, notes


def analyze(region_yaml: Path, repo_root: Path) -> dict:
    """Run the SESE control-flow scan and return a structured result.

    Shared by the human-readable CLI path and --json so both report the
    same thing.
    """
    spec = yaml.safe_load(region_yaml.read_text())
    anchor = spec["anchor"]
    src_path = repo_root / anchor["file"]
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

    return {
        "src_file": anchor["file"],
        "range_count": len(ranges),
        "total_lines": sum(hi - lo + 1 for lo, hi, _ in ranges),
        "violations": all_violations,
        "notes": all_notes,
        "verdict": "fail" if all_violations else "pass",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("region_yaml")
    ap.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--json", action="store_true", help="print one machine-readable object instead of the human report")
    args = ap.parse_args()

    result = analyze(Path(args.region_yaml), Path(args.repo_root))

    if args.json:
        print(json.dumps(result))
        return 0 if result["verdict"] == "pass" else 1

    print(f"VAL-1 SESE check: {Path(result['src_file']).name}, {result['range_count']} ranges, {result['total_lines']} lines")
    for note in result["notes"]:
        print(f"  {note['label']}:{note['line']}: {note['note']}: {note['text']}")
    if result["violations"]:
        print(f"FAIL: {len(result['violations'])} violation(s)")
        for v in result["violations"]:
            print(f"  {v['label']}:{v['line']}: {v['keyword'].upper()}: {v['text']}")
        return 1
    print("PASS: no goto / early return / entry / stop in region or closure")
    return 0


if __name__ == "__main__":
    sys.exit(main())
