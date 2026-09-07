#!/usr/bin/env python3
"""Mechanical SESE check for a region spec.

SESE = single-entry, single-exit.
Scans the anchor's line range and the closure line ranges for control-flow
constructs that would break single-entry/single-exit or contradict the spec's
absent_obstructions: goto, early return, entry, stop/error stop.

A RETURN whose next logical statement is the enclosing procedure's own END
statement is its terminal exit, reported as OK rather than a violation.

This lightweight analyzer supports full named subroutines and functions. It
resolves their current line ranges from the procedure names in the spec, so a
candidate may insert or remove lines without leaving a stale SESE claim. It
does not claim to prove arbitrary line selections or structured blocks; those
need a Fortran parser and control-flow graph and are rejected explicitly.

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
from pathlib import Path, PurePosixPath

import yaml

KEYWORDS = re.compile(r"\b(go\s*to|return|entry|error\s+stop|stop)\b", re.IGNORECASE)
PROC_START = re.compile(
    r"^\s*(?!end\b).*?\b(subroutine|function)\s+([a-z_]\w*)\b", re.IGNORECASE,
)
PROC_END = re.compile(
    r"^\s*end\s*(subroutine|function)\b(?:\s+([a-z_]\w*))?", re.IGNORECASE,
)

STRING = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")


def code_part(line: str) -> str:
    """Strip string literals, then the trailing ! comment."""
    no_str = STRING.sub("''", line)
    return no_str.split("!", 1)[0]


def parse_range(spec: str):
    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", str(spec))
    if not match:
        raise ValueError(f"invalid line range {spec!r}; expected FIRST-LAST")
    return int(match.group(1)), int(match.group(2))


def logical_statements(lines):
    """Free-form Fortran logical statements with their physical line spans.

    Joining continuations before searching prevents `ret&` / `&urn` and
    `go&` / `&to` from evading the control-flow scan. Strings have already
    been blanked by code_part, so an ampersand inside a string is harmless.
    """
    statements = []
    pending = None
    for number, original in enumerate(lines, 1):
        code = code_part(original).rstrip()
        if pending is not None and not code.strip():
            pending["text"].append(original.strip())
            continue
        if pending is not None:
            continuation = code.lstrip()
            if continuation.startswith("&"):
                continuation = continuation[1:]
            continued = continuation.endswith("&")
            if continued:
                continuation = continuation[:-1]
            pending["code"] += continuation
            pending["end"] = number
            pending["text"].append(original.strip())
            if not continued:
                statements.append(pending)
                pending = None
            continue
        if not code.strip():
            continue
        continued = code.endswith("&")
        if continued:
            code = code[:-1]
        item = {"start": number, "end": number, "code": code,
                "text": [original.strip()]}
        if continued:
            pending = item
        else:
            statements.append(item)
    if pending is not None:
        pending["unterminated_continuation"] = True
        statements.append(pending)
    return statements


def procedures(lines):
    """Named procedure declarations and their matching END statements."""
    statements = logical_statements(lines)
    found = []
    for index, statement in enumerate(statements):
        start = None if PROC_END.match(statement["code"]) else PROC_START.match(statement["code"])
        if not start:
            continue
        depth = 1
        for later in statements[index + 1:]:
            end = PROC_END.match(later["code"])
            if not end and PROC_START.match(later["code"]):
                depth += 1
            if end:
                depth -= 1
                if depth == 0:
                    end_name = end.group(2)
                    if (
                        end.group(1).casefold() != start.group(1).casefold()
                        or (end_name and end_name.casefold() != start.group(2).casefold())
                    ):
                        break
                    found.append({
                        "kind": start.group(1).lower(), "name": start.group(2),
                        "lo": statement["start"], "hi": later["end"],
                        "end_statement": later["start"],
                    })
                    break
    return found


def scan(lines, lo, hi, label, file, procedure_end):
    violations, notes = [], []
    statements = logical_statements(lines)
    selected = [s for s in statements if s["start"] >= lo and s["end"] <= hi]
    by_start = {s["start"]: i for i, s in enumerate(statements)}
    for statement in selected:
        code = statement["code"]
        for m in KEYWORDS.finditer(code):
            kw = re.sub(r"\s+", " ", m.group(1).lower())
            item = {
                "label": label, "file": file, "line": statement["start"],
                "keyword": kw, "text": " ".join(statement["text"]),
            }
            position = by_start[statement["start"]]
            following = statements[position + 1] if position + 1 < len(statements) else None
            is_plain_return = bool(re.fullmatch(r"\s*return(?:\s+\d+)?\s*", code, re.IGNORECASE))
            is_procedure_end = (
                following is not None
                and following["start"] == procedure_end
                and PROC_END.match(following["code"])
            )
            if kw == "return" and is_plain_return and is_procedure_end:
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
    if not isinstance(spec, dict):
        return [{"reason": "the region specification is not a mapping"}]
    files = spec.get("files")
    if not isinstance(files, list) or not files:
        return [{"reason": "the spec has no files: list naming every file the region may edit"}]

    problems = []
    for file in files:
        if not _safe_relative_path(file):
            problems.append({
                "reason": f"source path {file!r} must be a canonical relative path inside the tree"
            })
    if len([file for file in files if isinstance(file, str)]) != len(set(
        file for file in files if isinstance(file, str)
    )):
        problems.append({"reason": "the spec's files: list contains a duplicate path"})

    anchor_value = spec.get("anchor")
    if not isinstance(anchor_value, dict):
        problems.append({"reason": "the spec's anchor is not a mapping"})
        return problems
    anchor_file = anchor_value.get("file")
    if anchor_file is None:
        problems.append({"reason": "the spec's anchor names no file"})
    elif anchor_file not in files:
        problems.append({"reason": f"the anchor's file {anchor_file} is not in the spec's files: list"})

    closure = spec.get("closure") or {}
    if not isinstance(closure, dict):
        problems.append({"reason": "the spec's closure is not a mapping"})
        return problems
    callees = closure.get("callees", [])
    if not isinstance(callees, list):
        problems.append({"reason": "the spec's closure.callees is not a list"})
        return problems
    for callee in callees:
        if not isinstance(callee, dict) or "name" not in callee:
            problems.append({"reason": "a closure callee is not a mapping with a name"})
            continue
        callee_file = callee.get("file", anchor_file)
        if callee_file is not None and callee_file not in files:
            problems.append({
                "reason": f"callee {callee['name']} is in {callee_file}, which is not in the spec's files: list",
            })
    return problems


def _safe_relative_path(value) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        str(path) == value
        and not path.is_absolute()
        and all(part not in ("", ".", "..") for part in path.parts)
    )


def ranges_to_scan(spec: dict) -> list[dict]:
    """(file, first line, last line, label) for the anchor and every callee.

    A callee that names no file of its own is in the anchor's file, which
    is where a region that spans one file keeps all of them.
    """
    anchor = spec["anchor"]
    anchor_file = anchor["file"]
    node = anchor.get("pst_node", "")
    m = re.fullmatch(r"\s*(.+?)@(\d+)-(\d+)\s*", str(node))
    if not m:
        raise ValueError("the anchor pst_node must end in @FIRST-LAST")
    label = anchor.get("entry_symbol")
    if not isinstance(label, str) or not label:
        raise ValueError("the anchor names no entry_symbol")
    if m.group(1).casefold() != label.casefold():
        raise ValueError(
            "only a full named procedure is supported: pst_node must name entry_symbol exactly"
        )
    ranges = [{"file": anchor_file, "lo": int(m.group(2)), "hi": int(m.group(3)),
               "label": label, "symbol": label}]
    for callee in spec.get("closure", {}).get("callees", []):
        lo, hi = parse_range(callee["lines"])
        ranges.append({"file": callee.get("file", anchor_file), "lo": lo, "hi": hi,
                       "label": callee["name"], "symbol": callee["name"]})
    return ranges


def analyze(region_yaml: Path, repo_root: Path) -> dict:
    """Run the SESE control-flow scan and return a structured result.

    Shared by the human-readable CLI path and --json so both report the
    same thing.
    """
    try:
        spec = yaml.safe_load(region_yaml.read_text())
    except (OSError, yaml.YAMLError) as exc:
        return _result(None, [], [], 0, [{"reason": f"cannot read the region spec: {exc}"}], [])
    anchor_value = spec.get("anchor") if isinstance(spec, dict) else None
    anchor_file = anchor_value.get("file") if isinstance(anchor_value, dict) else None

    problems = spec_problems(spec)
    if problems:
        files = spec.get("files") if isinstance(spec, dict) else []
        return _result(anchor_file, files, [], 0, problems, [])

    try:
        declared_ranges = ranges_to_scan(spec)
    except (KeyError, TypeError, ValueError) as exc:
        return _result(anchor_file, spec["files"], [], 0, [{"reason": str(exc)}], [])
    lines_by_file = {}
    missing = []
    for file in sorted({item["file"] for item in declared_ranges}):
        path = repo_root / file
        if path.is_file():
            lines_by_file[file] = path.read_text().splitlines()
        else:
            missing.append({"reason": f"{file} has lines to scan but is not in the tree"})
    if missing:
        return _result(anchor_file, spec["files"], declared_ranges, 0, missing, [])

    ranges = []
    resolved_ranges = []
    resolution_problems = []
    procedures_by_file = {file: procedures(lines) for file, lines in lines_by_file.items()}
    for declared in declared_ranges:
        file = declared["file"]
        lo, hi = declared["lo"], declared["hi"]
        line_count = len(lines_by_file[file])
        if lo < 1 or hi < lo or hi > line_count:
            resolution_problems.append({
                "reason": (
                    f"{declared['label']}'s range {lo}-{hi} is invalid for {file}, "
                    f"which has {line_count} lines"
                ),
                "file": file, "label": declared["label"],
            })
            continue
        matches = [
            proc for proc in procedures_by_file[file]
            if proc["name"].casefold() == declared["symbol"].casefold()
        ]
        if len(matches) != 1:
            reason = (
                f"named procedure '{declared['symbol']}' was not found in {file}"
                if not matches else
                f"named procedure '{declared['symbol']}' is ambiguous in {file}"
            )
            resolution_problems.append({
                "reason": reason, "file": file, "label": declared["label"],
            })
            continue
        proc = matches[0]
        # Preserve a range that deliberately includes or omits only the END
        # line. Any other mismatch means edits shifted or lengthened the named
        # procedure, so its current complete range is authoritative.
        aligned = lo == proc["lo"] and hi in (proc["hi"], proc["hi"] - 1)
        actual_lo, actual_hi = (lo, hi) if aligned else (proc["lo"], proc["hi"])
        ranges.append((file, actual_lo, actual_hi, declared["label"], proc["end_statement"]))
        resolved_ranges.append({
            "file": file, "label": declared["label"],
            "declared": [lo, hi], "actual": [actual_lo, actual_hi],
            "procedure": {"kind": proc["kind"], "name": proc["name"],
                          "range": [proc["lo"], proc["hi"]]},
        })
    if resolution_problems:
        return _result(
            anchor_file, spec["files"], declared_ranges, 0,
            resolution_problems, [], resolved_ranges,
        )

    all_violations, all_notes = [], []
    for file, lo, hi, label, procedure_end in ranges:
        v, n = scan(lines_by_file[file], lo, hi, label, file, procedure_end)
        all_violations += v
        all_notes += n

    total_lines = sum(hi - lo + 1 for _, lo, hi, _, _ in ranges)
    return _result(
        anchor_file, spec["files"], ranges, total_lines,
        all_violations, all_notes, resolved_ranges,
    )


def _result(anchor_file, files, ranges, total_lines, violations, notes,
            resolved_ranges=None) -> dict:
    return {
        "anchor_file": anchor_file,
        "src_files": sorted(set(file for file in (files or []) if isinstance(file, str))),
        "range_count": len(ranges),
        "total_lines": total_lines,
        "violations": violations,
        "notes": notes,
        "resolved_ranges": resolved_ranges or [],
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
