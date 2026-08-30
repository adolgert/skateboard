#!/usr/bin/env python3
"""fmutate -- source-level mutation testing for Fortran kernels behind a
capture-replay oracle.

The question it answers: if a port of this kernel were wrong, would the gate
notice? It generates single-token mutants of the kernel source, rebuilds the
replay driver against each, replays the capture corpus, and scores the result
with the *real* oracle comparator -- so the number it reports is a property of
the gate as configured, not of a model of the gate.

Three things make it more useful than a plain mutation score:

  * Coverage filtering. Mutants on lines the corpus never executes cannot be
    killed, so they are generated, marked UNCOVERED, and not built. What is
    left is a mutation score over reachable code, plus a list of dead surface.

  * The tolerance-blind gap. Every surviving mutant is scored twice, under the
    tolerance policy and under bitwise equality. Mutants that change the output
    bitwise but pass the tolerance policy are the ones the tolerance is hiding.
    That count -- not the mutation score -- is the metric to hold at zero when
    tolerances are recalibrated for a new compiler.

  * The checked-build rerun (--checked). Mutants that survive the release build
    are rebuilt with -fcheck=all and friends. Fortran array-section
    nonconformance is invisible at -O2 (the compiler takes the trip count from
    one side and the surviving arithmetic is the original arithmetic) but traps
    under runtime checks. A mutant killed only by the checked build is a fault
    class the release-mode gate cannot see.

Usage:
    fmutate.py tools/fmutate/targets/demo.json --checked
    fmutate.py <target.json> --list          # generate mutants, build nothing
    fmutate.py <target.json> --fail-on-gap   # nonzero exit if the gap > 0

See README.md for the target-file schema.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# mutation operators
# --------------------------------------------------------------------------- #

# Operator names follow the usual mutation-testing abbreviations:
#   AOR  arithmetic operator replacement       a + b   -> a - b
#   ROR  relational operator replacement       a .lt. b -> a .le. b
#   LCR  logical connector replacement         a .and. b -> a .or. b
#   CRP  constant replacement                  0.5 -> 1.0
#   SBR  section bound replacement             x(2:n-1) -> x(2:n-2)
#   SDL  statement deletion                    (comment the line out)
#
# SBR is the operator that matters most for a GPU port and is the one no
# off-the-shelf Fortran mutator has: it perturbs array-section bounds, which is
# exactly what gets rewritten when array syntax becomes an explicit loop nest.

_AOR = {"+": ["-", "*"], "-": ["+", "*"], "*": ["/", "+"], "/": ["*", "-"]}

_ROR = {
    ".lt.": [".le.", ".gt.", ".eq."],
    ".le.": [".lt.", ".ge.", ".eq."],
    ".gt.": [".ge.", ".lt.", ".eq."],
    ".ge.": [".gt.", ".le.", ".eq."],
    ".eq.": [".ne."],
    ".ne.": [".eq."],
    "<": ["<=", ">", "=="],
    "<=": ["<", ">=", "=="],
    ">": [">=", "<", "=="],
    ">=": [">", "<=", "=="],
    "==": ["/="],
    "/=": ["=="],
}

_LCR = {
    ".and.": [".or."],
    ".or.": [".and."],
    ".eqv.": [".neqv."],
    ".neqv.": [".eqv."],
}

# Lines that look like declarations, control flow, or scaffolding: never a
# useful deletion target, and deleting them mostly produces non-compiling
# mutants that cost a compile each.
_NO_DELETE = re.compile(
    r"^\s*(!|end\b|contains\b|implicit\b|use\b|module\b|program\b|subroutine\b"
    r"|function\b|interface\b|type\b|public\b|private\b|integer\b|real\b"
    r"|logical\b|character\b|complex\b|allocate\b|deallocate\b|if\b|do\b"
    r"|else\b|select\b|case\b|where\b|associate\b|block\b)",
    re.IGNORECASE,
)


@dataclass
class Mutant:
    """One single-token change to one line of one file."""

    mid: str
    file: str
    line: int  # 1-indexed
    op: str
    original: str
    mutated: str
    # filled in by scoring
    status: str = "PENDING"
    tol_killed: bool | None = None
    bit_killed: bool | None = None
    checked_killed: bool | None = None
    note: str = ""


def _strip_comment(line: str) -> str:
    """Return the code part of a line, with string literals blanked out.

    Fortran comments start at an unquoted '!'. Blanking string literals first
    keeps the operator regexes from mutating inside a format string.
    """
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(" " if ch != quote else ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            out.append(ch)
        elif ch == "!":
            break
        else:
            out.append(ch)
    return "".join(out)


def _is_continued(lines: list[str], i: int) -> bool:
    """True if line i is part of a multi-line statement (Fortran '&')."""
    if _strip_comment(lines[i]).rstrip().endswith("&"):
        return True
    j = i - 1
    while j >= 0:
        prev = _strip_comment(lines[j]).rstrip()
        if prev:
            return prev.endswith("&")
        j -= 1
    return False


def _sub_at(line: str, start: int, end: int, replacement: str) -> str:
    return line[:start] + replacement + line[end:]


def generate(text: str, fname: str) -> list[Mutant]:
    """Generate all single-token mutants of one Fortran source file."""
    lines = text.split("\n")
    out: list[Mutant] = []
    seen: set[tuple[int, str]] = set()

    def emit(op: str, i: int, mutated: str) -> None:
        key = (i, mutated)
        if key in seen or mutated == lines[i]:
            return
        seen.add(key)
        out.append(
            Mutant(
                mid=f"{Path(fname).stem}-{i + 1:04d}-{op}-{len(out):04d}",
                file=fname,
                line=i + 1,
                op=op,
                original=lines[i],
                mutated=mutated,
            )
        )

    for i, line in enumerate(lines):
        code = _strip_comment(line)
        if not code.strip():
            continue
        low = code.lower()

        # ---- ROR / LCR: dotted and symbolic forms -------------------------
        for table, opname in ((_ROR, "ROR"), (_LCR, "LCR")):
            for tok, reps in table.items():
                if tok.startswith("."):
                    pattern = re.escape(tok)
                else:
                    # symbolic operators: avoid matching inside '<=' etc. and
                    # avoid the '=' of an assignment
                    pattern = re.escape(tok) + r"(?![=<>/])"
                    if tok in ("<", ">"):
                        pattern = r"(?<![<>=/])" + pattern
                for m in re.finditer(pattern, low):
                    for rep in reps:
                        emit(opname, i, _sub_at(line, m.start(), m.end(), rep))

        # Everything below only makes sense inside an executable statement.
        if "=" not in code or "::" in code:
            continue

        # ---- SBR: array-section bound perturbation ------------------------
        # Matches the ':' of a section triplet and shifts the integer offset on
        # either side by +/-1, including introducing one where there is none.
        for m in re.finditer(r"([A-Za-z_]\w*)\s*([+-])\s*(\d+)", code):
            base, sign, num = m.group(1), m.group(2), int(m.group(3))
            # only inside parentheses -- i.e. plausibly a subscript
            if code.count("(", 0, m.start()) <= code.count(")", 0, m.start()):
                continue
            for delta in (-1, 1):
                val = num + delta if sign == "-" else num - delta
                if val < 0:
                    rep = f"{base} {'+' if sign == '-' else '-'} {abs(val)}"
                elif val == 0:
                    rep = base
                else:
                    rep = f"{base} {sign} {val}"
                emit("SBR", i, _sub_at(line, m.start(), m.end(), rep))

        # ---- AOR ----------------------------------------------------------
        for m in re.finditer(r"(?<=[\w\)\s])([+\-*/])(?=[\s\w\(])", code):
            s = m.start()
            if code[s : s + 2] == "**" or (s and code[s - 1] == "*"):
                continue  # exponentiation
            if code[s] in "+-" and s and code[s - 1] in "eEdD":
                continue  # exponent sign in a literal
            for rep in _AOR[m.group(1)]:
                emit("AOR", i, _sub_at(line, s, s + 1, rep))

        # ---- CRP ----------------------------------------------------------
        for m in re.finditer(r"(?<![\w.])(\d+\.\d*|\.\d+|\d+)(?![\w.])", code):
            lit = m.group(1)
            if "." in lit:
                v = float(lit)
                reps = [f"{v * 2:g}", f"{v / 2:g}", "0.0"]
            else:
                v = int(lit)
                reps = [str(v + 1), str(v - 1)]
            for rep in reps:
                if rep != lit:
                    emit("CRP", i, _sub_at(line, m.start(), m.end(), rep))

        # ---- SDL ----------------------------------------------------------
        if (
            not _NO_DELETE.match(line)
            and not _is_continued(lines, i)
            and re.match(r"^\s*[\w%]+[\w%\(\):,\s+\-*/]*=[^=]", code)
        ):
            emit("SDL", i, "! [fmutate SDL] " + line)

    return out


# --------------------------------------------------------------------------- #
# target configuration
# --------------------------------------------------------------------------- #


@dataclass
class Target:
    name: str
    src_dir: Path
    mutate: list[str]
    sources: list[str]
    extra_sources: list[Path]
    exe: str
    fc: str
    flags: list[str]
    coverage_flags: list[str]
    checked_flags: list[str]
    inputs: Path
    expected: Path
    case_glob: str
    input_files: list[str]
    variables: dict[str, str]
    comparator: Path
    tolerances: Path
    run_args: list[str] = field(default_factory=lambda: ["{case_dir}"])
    # How to read a corpus file that is not a .npy. A .npy says for itself
    # what it holds; a raw stream does not, so a target using one has to
    # name its element type here (e.g. "<f8").
    raw_dtype: str | None = None

    @staticmethod
    def load(path: Path) -> "Target":
        cfg = json.loads(Path(path).read_text())
        r = lambda p: (REPO / p).resolve()  # noqa: E731
        b, c, cmp_ = cfg["build"], cfg["corpus"], cfg["comparator"]
        return Target(
            name=cfg["name"],
            src_dir=r(cfg["src_dir"]),
            mutate=cfg["mutate"],
            sources=b["sources"],
            extra_sources=[r(p) for p in b.get("extra_sources", [])],
            exe=b.get("exe", "driver"),
            fc=b.get("fc", "gfortran"),
            flags=b["flags"],
            coverage_flags=b.get("coverage_flags", ["-O0", "-g", "--coverage"]),
            checked_flags=b.get("checked_flags", []),
            inputs=r(c["inputs"]),
            expected=r(c["expected"]),
            case_glob=c.get("case_glob", "case*"),
            input_files=c["input_files"],
            variables=c["variables"],
            comparator=r(cmp_["module"]),
            tolerances=r(cmp_["tolerances"]),
            run_args=b.get("run_args", ["{case_dir}"]),
            raw_dtype=c.get("raw_dtype"),
        )

    def cases(self) -> list[str]:
        return sorted(
            p.name
            for p in self.inputs.glob(self.case_glob)
            if p.is_dir() and (self.expected / p.name).is_dir()
        )


# --------------------------------------------------------------------------- #
# build & run
# --------------------------------------------------------------------------- #


def _stage(t: Target, workdir: Path, mutant: Mutant | None) -> list[str]:
    """Copy sources into workdir, applying `mutant` if given. Returns argv tail."""
    for f in t.sources:
        shutil.copy(t.src_dir / f, workdir / f)
    for p in t.extra_sources:
        shutil.copy(p, workdir / p.name)
    if mutant is not None:
        target = workdir / mutant.file
        lines = target.read_text().split("\n")
        lines[mutant.line - 1] = mutant.mutated
        target.write_text("\n".join(lines))
    return t.sources + [p.name for p in t.extra_sources]


def _build(t: Target, workdir: Path, flags: list[str], mutant: Mutant | None):
    srcs = _stage(t, workdir, mutant)
    cmd = [t.fc, *flags, "-J", str(workdir), "-o", str(workdir / t.exe), *srcs]
    p = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=300)
    return p.returncode == 0, p.stderr


def _read_array(t: Target, path: Path) -> np.ndarray:
    """One corpus file as an array.

    A .npy carries its own element type, shape and order. Anything else is
    a raw stream, and the target has to say what is in it.
    """
    if path.suffix == ".npy":
        return np.load(path, allow_pickle=False)
    if t.raw_dtype is None:
        raise ValueError(
            f"{path} is not a .npy and target '{t.name}' sets no corpus raw_dtype, "
            f"so there is nothing that says what the file holds"
        )
    return np.fromfile(path, dtype=t.raw_dtype)


def _replay_case(t: Target, workdir: Path, case: str):
    """Run the driver on one case. Returns (ok, note, {var: ndarray})."""
    run = workdir / "run" / case
    run.mkdir(parents=True, exist_ok=True)
    for f in t.input_files:
        shutil.copy(t.inputs / case / f, run / f)
    args = [a.format(case_dir=str(run)) for a in t.run_args]
    try:
        p = subprocess.run(
            [str(workdir / t.exe), *args],
            cwd=run,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False, "timeout", {}
    if p.returncode != 0:
        first = (p.stderr or p.stdout).strip().split("\n")[0][:120]
        return False, f"exit {p.returncode}: {first}", {}
    got = {}
    for var, fname in t.variables.items():
        f = run / fname
        if not f.exists():
            return False, f"missing output {fname}", {}
        got[var] = _read_array(t, f)
    return True, "", got


def _load_comparator(t: Target):
    spec = importlib.util.spec_from_file_location("fmutate_cmp", t.comparator)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tols = json.loads(t.tolerances.read_text())["variables"]
    return mod, tols


def _bitwise_equal(a: np.ndarray, b: np.ndarray) -> bool:
    """Identical bit patterns, whatever the element type is.

    Comparing the bytes rather than the values is what makes two NaNs
    equal and two zeros of opposite sign different, which is what "the
    mutant changed the answer at all" has to mean.
    """
    return a.shape == b.shape and a.dtype == b.dtype and a.tobytes() == b.tobytes()


# --------------------------------------------------------------------------- #
# scoring one mutant (runs in a worker process)
# --------------------------------------------------------------------------- #


def _score_one(args) -> Mutant:
    t, mutant, cases, want_checked, keep = args
    cmp_mod, tols = _load_comparator(t)
    ref = {
        c: {
            v: _read_array(t, t.expected / c / f)
            for v, f in t.variables.items()
        }
        for c in cases
    }

    wd = Path(tempfile.mkdtemp(prefix=f"fmut-{mutant.mid}-"))
    try:
        ok, err = _build(t, wd, t.flags, mutant)
        if not ok:
            mutant.status = "NOCOMPILE"
            mutant.note = err.strip().split("\n")[-1][:160]
            return mutant

        tol_killed = bit_killed = False
        for case in cases:
            ran, note, got = _replay_case(t, wd, case)
            if not ran:
                mutant.status = "KILLED"
                mutant.tol_killed = mutant.bit_killed = True
                mutant.note = note
                return mutant
            for var in t.variables:
                if not _bitwise_equal(ref[case][var], got[var]):
                    bit_killed = True
                res = cmp_mod.compare_variable(ref[case][var], got[var], tols[var])
                if not res["pass"]:
                    tol_killed = True
            if tol_killed and bit_killed:
                break

        mutant.tol_killed, mutant.bit_killed = tol_killed, bit_killed
        if tol_killed:
            mutant.status = "KILLED"
        elif bit_killed:
            mutant.status = "GAP"  # differs bitwise, tolerance says pass
        else:
            mutant.status = "SURVIVED"

        # Rebuild survivors with runtime checks: array-section nonconformance
        # is bit-invisible at -O2 but traps here.
        if want_checked and mutant.status == "SURVIVED" and t.checked_flags:
            cwd = Path(tempfile.mkdtemp(prefix=f"fmut-chk-{mutant.mid}-"))
            try:
                cok, _ = _build(t, cwd, t.checked_flags, mutant)
                if cok:
                    killed = False
                    for case in cases:
                        ran, note, got = _replay_case(t, cwd, case)
                        if not ran:
                            killed = True
                            mutant.note = f"checked build: {note}"
                            break
                    mutant.checked_killed = killed
                    if killed:
                        mutant.status = "SURVIVED-release/KILLED-checked"
            finally:
                shutil.rmtree(cwd, ignore_errors=True)
        return mutant
    finally:
        if not keep:
            shutil.rmtree(wd, ignore_errors=True)


# --------------------------------------------------------------------------- #
# coverage
# --------------------------------------------------------------------------- #


def coverage(t: Target, verbose: bool = False) -> dict[str, set[int]]:
    """Executed line numbers per mutated file, from one instrumented run.

    Note the gcov naming trap: compiling several sources in one command names
    the notes files '<exe>-<base>.gcno', so `gcov <base>.f90` fails outright.
    We glob for the .gcda and hand gcov that instead.
    """
    wd = Path(tempfile.mkdtemp(prefix="fmut-cov-"))
    try:
        ok, err = _build(t, wd, t.coverage_flags, None)
        if not ok:
            raise SystemExit(f"coverage build failed:\n{err}")
        for case in t.cases():
            ran, note, _ = _replay_case(t, wd, case)
            if not ran:
                raise SystemExit(f"coverage run failed on {case}: {note}")

        covered: dict[str, set[int]] = {f: set() for f in t.mutate}
        for f in t.mutate:
            gcda = list(wd.glob(f"*{Path(f).stem}.gcda"))
            if not gcda:
                print(f"  warning: no .gcda for {f}; treating all lines as covered")
                covered[f] = None  # type: ignore[assignment]
                continue
            subprocess.run(
                ["gcov", "-b", *[str(g) for g in gcda]],
                cwd=wd,
                capture_output=True,
                text=True,
            )
            gcov_file = wd / f"{f}.gcov"
            if not gcov_file.exists():
                covered[f] = None  # type: ignore[assignment]
                continue
            for row in gcov_file.read_text(errors="replace").split("\n"):
                m = re.match(r"\s*([#\-0-9=*]+)\s*:\s*(\d+):", row)
                if not m:
                    continue
                count, lineno = m.group(1), int(m.group(2))
                if lineno and count not in ("-", "#####", "====="):
                    covered[f].add(lineno)
        return covered
    finally:
        shutil.rmtree(wd, ignore_errors=True)


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def report(t: Target, mutants: list[Mutant], out_json: Path | None) -> int:
    skipped = [m for m in mutants if m.status == "PENDING"]  # --limit
    uncovered = [m for m in mutants if m.status == "UNCOVERED"]
    nocompile = [m for m in mutants if m.status == "NOCOMPILE"]
    scored = [
        m
        for m in mutants
        if m.status not in ("UNCOVERED", "NOCOMPILE", "PENDING")
    ]
    killed = [m for m in scored if m.tol_killed]
    gap = [m for m in scored if m.status == "GAP"]
    chk = [m for m in scored if m.status == "SURVIVED-release/KILLED-checked"]
    surv = [m for m in scored if m.status == "SURVIVED"]

    pct = (100.0 * len(killed) / len(scored)) if scored else 0.0
    print("\n" + "=" * 72)
    print(f"fmutate: {t.name}")
    print("=" * 72)
    print(f"  generated                       {len(mutants):5d}")
    print(f"  uncovered (not built)           {len(uncovered):5d}")
    if skipped:
        print(f"  skipped (--limit)               {len(skipped):5d}")
    print(f"  non-compiling                   {len(nocompile):5d}")
    print(f"  scored                          {len(scored):5d}")
    print()
    print(f"  KILLED by tolerance oracle      {len(killed):5d}   ({pct:.1f}%)")
    print(f"  TOLERANCE-BLIND GAP             {len(gap):5d}   <-- hold at 0")
    print(f"  survived release, killed by     {len(chk):5d}")
    print(f"    the checked build")
    print(f"  SURVIVED everything             {len(surv):5d}")

    def dump(title, ms, limit=40):
        if not ms:
            return
        print(f"\n--- {title} ({len(ms)}) ---")
        for m in ms[:limit]:
            note = f"  [{m.note}]" if m.note else ""
            print(f"  {m.file}:{m.line:<4} {m.op:4} {m.mutated.strip()[:76]}{note}")
        if len(ms) > limit:
            print(f"  ... and {len(ms) - limit} more")

    dump("TOLERANCE-BLIND GAP -- tolerance is hiding a real difference", gap)
    dump("killed ONLY by the checked build -- release gate cannot see these", chk)
    dump("survived everything -- equivalent, or a genuine oracle hole", surv)

    if uncovered:
        by_file: dict[str, set[int]] = {}
        for m in uncovered:
            by_file.setdefault(m.file, set()).add(m.line)
        print("\n--- uncovered lines (dead surface; mutants not built) ---")
        for f, ls in sorted(by_file.items()):
            print(f"  {f}: lines {', '.join(str(x) for x in sorted(ls))}")

    if out_json:
        out_json.write_text(json.dumps([asdict(m) for m in mutants], indent=2))
        print(f"\nwrote {out_json}")
    return len(gap)


# --------------------------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Mutation-test a Fortran kernel against its capture-replay oracle."
    )
    ap.add_argument("target", type=Path, help="target JSON (see targets/)")
    ap.add_argument("--list", action="store_true", help="list mutants, build nothing")
    ap.add_argument(
        "--checked",
        action="store_true",
        help="rebuild survivors with the target's checked_flags",
    )
    ap.add_argument(
        "--no-coverage",
        action="store_true",
        help="skip the coverage prepass (build every mutant)",
    )
    ap.add_argument("--ops", help="comma-separated operator whitelist, e.g. SBR,AOR")
    ap.add_argument("--limit", type=int, help="score at most N mutants (smoke test)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--json", type=Path, help="write per-mutant results here")
    ap.add_argument(
        "--fail-on-gap",
        action="store_true",
        help="exit nonzero if any mutant lands in the tolerance-blind gap",
    )
    ap.add_argument("--keep", action="store_true", help="keep mutant build dirs")
    args = ap.parse_args()

    t = Target.load(args.target)
    cases = t.cases()
    if not cases:
        raise SystemExit(f"no cases matched {t.case_glob} under {t.inputs}")
    print(f"target {t.name}: {len(cases)} cases, mutating {', '.join(t.mutate)}")

    mutants: list[Mutant] = []
    for f in t.mutate:
        mutants += generate((t.src_dir / f).read_text(), f)
    if args.ops:
        keep_ops = {o.strip().upper() for o in args.ops.split(",")}
        mutants = [m for m in mutants if m.op in keep_ops]
    print(f"generated {len(mutants)} mutants")

    if args.list:
        for m in mutants:
            print(f"  {m.mid:34} {m.file}:{m.line:<4} {m.op:4} {m.mutated.strip()[:70]}")
        return 0

    if not args.no_coverage:
        print("coverage prepass ...")
        cov = coverage(t)
        for m in mutants:
            lines = cov.get(m.file)
            if lines is not None and m.line not in lines:
                m.status = "UNCOVERED"
        n_unc = sum(1 for m in mutants if m.status == "UNCOVERED")
        print(f"  {n_unc} mutants on uncovered lines will not be built")

    todo = [m for m in mutants if m.status == "PENDING"]
    if args.limit:
        todo = todo[: args.limit]
    print(f"scoring {len(todo)} mutants on {args.jobs} workers ...")

    done = 0
    payload = [(t, m, cases, args.checked, args.keep) for m in todo]
    by_id = {m.mid: m for m in mutants}
    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        futures = [ex.submit(_score_one, p) for p in payload]
        for fut in as_completed(futures):
            res = fut.result()
            by_id[res.mid].__dict__.update(res.__dict__)
            done += 1
            print(
                f"  [{done:4d}/{len(todo)}] {res.file}:{res.line:<4} {res.op:4} "
                f"{res.status:32} {res.mutated.strip()[:56]}"
            )

    gap = report(t, mutants, args.json)
    return 1 if (args.fail_on_gap and gap) else 0


if __name__ == "__main__":
    sys.exit(main())
