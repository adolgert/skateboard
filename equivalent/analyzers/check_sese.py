#!/usr/bin/env python3
"""Mechanical SESE check for a region spec.

SESE = single-entry, single-exit.
Scans the anchor's line range and the closure line ranges for control-flow
constructs that would break single-entry/single-exit or contradict the spec's
absent_obstructions: goto, early return, entry, stop/error stop.

A RETURN whose next non-blank, non-comment line is an END statement is the
subroutine's terminal exit, reported as OK rather than a violation.

Trust role: this is the analyzer a strategy names, and its verdict becomes a
claim; its `src_files` becomes the region's allow-list, so every file it
returns is a file the agent may then edit. Returning a file the spec did not
list, or passing a spec whose region is not single-entry/single-exit, would
unfreeze code nobody agreed to unfreeze. It reads a tree the gateway
materialized and writes nothing.

A spec this cannot make sense of -- no `files:` list, an anchor or a callee in
a file the spec does not list, a file that has to be scanned and is not in the
tree -- is a `fail` verdict rather than an error, because a malformed spec is a
fact about the submission and belongs in the ledger like any other.

Usage: check_sese.py <region.yaml> [--repo-root DIR] [--json]
Exit 0 iff no violations.

--json prints a single machine-readable object instead of the human report
(same verdict, same exit code) -- this is the contract a caller like
equivalent/components/sese_check.py parses; the default text output is
unchanged from when this lived under tools/, and is what that directory's
README examples show.
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


def scan(lines, lo, hi, label, file):
    violations, notes = [], []
    for i in range(lo - 1, min(hi, len(lines))):
        code = code_part(lines[i])
        for m in KEYWORDS.finditer(code):
            kw = re.sub(r"\s+", " ", m.group(1).lower())
            item = {
                "label": label, "file": file, "line": i + 1,
                "keyword": kw, "text": lines[i].strip(),
            }
            if kw == "return" and END_STMT.match(next_code_line(lines, i)):
                item["note"] = "terminal RETURN (ok)"
                notes.append(item)
            else:
                violations.append(item)
    return violations, notes


def spec_problems(spec: dict) -> list[dict]:
    """Ways the spec contradicts itself, each as one violation with a reason.

    The `files:` list is what the region may edit. Everything the analyzer
    reads has to be on it, so a spec that scans a file it does not list is
    asking for an allow-list that does not cover its own region.
    """
    files = spec.get("files")
    if not isinstance(files, list) or not files:
        return [{"reason": "the spec has no files: list naming every file the region may edit"}]

    anchor_file = (spec.get("anchor") or {}).get("file")
    problems = []
    if anchor_file is None:
        problems.append({"reason": "the spec's anchor names no file"})
    elif anchor_file not in files:
        problems.append({"reason": f"the anchor's file {anchor_file} is not in the spec's files: list"})

    for callee in spec.get("closure", {}).get("callees", []):
        callee_file = callee.get("file", anchor_file)
        if callee_file is not None and callee_file not in files:
            problems.append({
                "reason": f"callee {callee['name']} is in {callee_file}, which is not in the spec's files: list",
            })
    return problems


def ranges_to_scan(spec: dict) -> list[tuple[str, int, int, str]]:
    """(file, first line, last line, label) for the anchor and every callee.

    A callee that names no file of its own is in the anchor's file, which
    is where a region that spans one file keeps all of them.
    """
    anchor = spec["anchor"]
    anchor_file = anchor["file"]
    m = re.search(r"@(\d+)-(\d+)", anchor["pst_node"])
    ranges = [(anchor_file, int(m.group(1)), int(m.group(2)), anchor.get("entry_symbol", "anchor"))]
    for callee in spec.get("closure", {}).get("callees", []):
        lo, hi = parse_range(callee["lines"])
        ranges.append((callee.get("file", anchor_file), lo, hi, callee["name"]))
    return ranges


def analyze(region_yaml: Path, repo_root: Path) -> dict:
    """Run the SESE control-flow scan and return a structured result.

    Shared by the human-readable CLI path and --json so both report the
    same thing.
    """
    spec = yaml.safe_load(region_yaml.read_text())
    anchor_file = (spec.get("anchor") or {}).get("file")

    problems = spec_problems(spec)
    if problems:
        return _result(anchor_file, spec.get("files"), [], 0, problems, [])

    ranges = ranges_to_scan(spec)
    lines_by_file = {}
    missing = []
    for file in sorted({file for file, _, _, _ in ranges}):
        path = repo_root / file
        if path.is_file():
            lines_by_file[file] = path.read_text().splitlines()
        else:
            missing.append({"reason": f"{file} has lines to scan but is not in the tree"})
    if missing:
        return _result(anchor_file, spec["files"], ranges, 0, missing, [])

    all_violations, all_notes = [], []
    for file, lo, hi, label in ranges:
        v, n = scan(lines_by_file[file], lo, hi, label, file)
        all_violations += v
        all_notes += n

    total_lines = sum(hi - lo + 1 for _, lo, hi, _ in ranges)
    return _result(anchor_file, spec["files"], ranges, total_lines, all_violations, all_notes)


def _result(anchor_file, files, ranges, total_lines, violations, notes) -> dict:
    return {
        "anchor_file": anchor_file,
        "src_files": sorted(set(files or [])),
        "range_count": len(ranges),
        "total_lines": total_lines,
        "violations": violations,
        "notes": notes,
        "verdict": "fail" if violations else "pass",
    }


def describe(item: dict) -> str:
    """One violation or note as a line of the human report.

    A control-flow finding names where it is; a finding about the spec
    itself has no line to name, so it says what is wrong instead.
    """
    if "reason" in item:
        return f"  spec: {item['reason']}"
    if "note" in item:
        return f"  {item['label']}:{item['line']}: {item['note']}: {item['text']}"
    return f"  {item['label']}:{item['line']}: {item['keyword'].upper()}: {item['text']}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("region_yaml")
    ap.add_argument("--repo-root", default=".", help="the tree the spec's paths are relative to")
    ap.add_argument("--json", action="store_true", help="print one machine-readable object instead of the human report")
    args = ap.parse_args()

    result = analyze(Path(args.region_yaml), Path(args.repo_root))

    if args.json:
        print(json.dumps(result))
        return 0 if result["verdict"] == "pass" else 1

    name = Path(result["anchor_file"] or "").name
    print(f"VAL-1 SESE check: {name}, {result['range_count']} ranges, {result['total_lines']} lines")
    for note in result["notes"]:
        print(describe(note))
    if result["violations"]:
        print(f"FAIL: {len(result['violations'])} violation(s)")
        for v in result["violations"]:
            print(describe(v))
        return 1
    print("PASS: no goto / early return / entry / stop in region or closure")
    return 0


if __name__ == "__main__":
    sys.exit(main())
