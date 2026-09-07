"""Dependency-free Fortran mutation operators shared by the service and CLI.

This module defines the mutation vocabulary and source generator. It uses only
the Python standard library so tools/fmutate/fmutate.py can import the exact
same implementation when run directly from a repository checkout.

The portable basename stem and ``[mutant: statement deleted]`` marker are the
canonical spellings. The old standalone copy used ``Path.stem`` and a different
SDL comment; both produced the same Fortran mutations, and sharing these
spellings also makes IDs and review output identical.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Operator names follow the usual mutation-testing abbreviations:
#   AOR  arithmetic operator replacement       a + b    -> a - b
#   ROR  relational operator replacement       a .lt. b -> a .le. b
#   LCR  logical connector replacement         a .and. b -> a .or. b
#   CRP  constant replacement                  0.5 -> 1.0
#   SBR  section bound replacement             x(2:n-1) -> x(2:n-2)
#   SDL  statement deletion                    (comment the line out)
#
# SBR is the one worth the trouble for a GPU port: it perturbs
# array-section bounds, which is exactly what gets rewritten when array
# syntax becomes an explicit loop nest, and off-by-one bounds are that
# rewrite's characteristic mistake.

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

# What a scored mutant can be, and what each verdict says about the
# harness rather than about the mutant:
#
#   KILLED       the harness noticed: some output left its band.
#   GAP          the output changed and every band let it through. This is
#                the tolerance-blind gap -- a wrong kernel this policy
#                would accept -- and it is the number to hold at zero.
#   EQUIVALENT   no output changed at all, bit for bit. Either the mutant
#                really computes the same thing, or the captured inputs
#                never reach it. Nothing about the bands can help; a
#                person reads the list.
#   BUILD_FAIL   the mutant is not Fortran, which the regex generator
#                cannot know in advance.
#   RUNTIME_FAIL the mutant built and the replay did not finish.
#   SKIPPED      the run reached its overall ceiling before this one.
KILLED = "KILLED"
GAP = "GAP"
EQUIVALENT = "EQUIVALENT"
BUILD_FAIL = "BUILD_FAIL"
RUNTIME_FAIL = "RUNTIME_FAIL"
SKIPPED = "SKIPPED"
PENDING = "PENDING"
STATUSES = (KILLED, GAP, EQUIVALENT, BUILD_FAIL, RUNTIME_FAIL, SKIPPED, PENDING)

# The statuses whose directory is kept after scoring: the two a person has
# to read the source of to understand.
KEEP_DIRECTORY = (GAP, EQUIVALENT)


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
    status: str = PENDING
    tol_killed: bool | None = None
    bit_killed: bool | None = None
    checked_killed: bool | None = None
    note: str = ""

    def as_result(self) -> dict:
        """One row of a mutation run's answer, in the words a claim uses."""
        return {
            "id": self.mid, "file": self.file, "line": self.line, "op": self.op,
            "original": self.original, "mutated": self.mutated,
            "status": self.status, "note": self.note,
        }


def _stem(path: str) -> str:
    """A file's name without its directories or its extension."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def _strip_comment(line: str) -> str:
    """The code part of a line, with string literals blanked out.

    Fortran comments start at an unquoted '!'. Blanking string literals
    first keeps the operator patterns from mutating inside a format
    string, where a '+' is a character and not an operator.
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
    """Is line i part of a multi-line statement (Fortran '&').

    Deleting half of one is a syntax error rather than a fault, so a
    continued line is never a deletion target.
    """
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
    """Every single-token mutant of one Fortran source file.

    `fname` is the path the tree spells the file with; it goes into each
    mutant's name and is what the writer of the mutated copy is given.
    """
    lines = text.split("\n")
    out: list[Mutant] = []
    seen: set[tuple[int, str]] = set()

    def emit(op: str, i: int, mutated: str) -> None:
        key = (i, mutated)
        if key in seen or mutated == lines[i]:
            return
        seen.add(key)
        out.append(Mutant(
            mid=f"{_stem(fname)}-{i + 1:04d}-{op}-{len(out):04d}",
            file=fname, line=i + 1, op=op, original=lines[i], mutated=mutated,
        ))

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
        # Matches the integer offset in a subscript and shifts it by +/-1,
        # including collapsing it away entirely.
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
            if code[s: s + 2] == "**" or (s and code[s - 1] == "*"):
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
            emit("SDL", i, "! [mutant: statement deleted] " + line)

    return out
