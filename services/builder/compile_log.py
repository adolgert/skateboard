"""Reading the compiler log, which is the whole proof that a build is honest.

Trust role: the builder runs a Makefile that came in with the submission,
so nothing about the compiler command line is fixed in advance. What
makes that safe is this file. The shim the Makefile is handed as its
compiler writes one JSON line per invocation; these functions turn that
log into two statements a claim can carry: every compile received the
strategy's flags, and every file compiled came from the submitted tree.
If this reader were wrong -- a flag counted as present when it was not,
a file from outside the tree passed over -- a port could be accepted for
a binary built some other way entirely, from code nobody reviewed.

It is deliberately a pure function of the log text, so it can be read and
tested without a compiler.

The source-pattern rule below is a second copy of the one in the
gateway's manifest reader. The builder image installs no Python package
of this project, so the rule cannot be imported across the boundary; a
test holds both copies to the same table of paths.
"""
from __future__ import annotations

import fnmatch
import json
import os

# Where the builder keeps its own trusted Fortran, which a tree is meant
# to compile against: it is what the capture format is written with, so
# a compile naming a file from here is not reaching outside the tree.
HARNESS = "/opt/harness"

# Extensions a Fortran compiler treats as source. Compared lower-cased,
# so ".F90" and ".f90" are one entry. An argument with any other
# extension is a flag, an object file, or a library -- not something the
# person reviewing this build needs to see listed as compiled code.
SOURCE_EXTENSIONS = (".f90", ".f08", ".f03", ".f", ".for")

# How each compiler spells "put the .mod files here". The Makefile takes
# the spelling as MODFLAG so that one Makefile serves both; the builder
# fills it in from the compiler the strategy names. A compiler not in
# this table gets no MODFLAG and the tree's own default applies.
MODULE_FLAG = {"nvfortran": "-module", "gfortran": "-J"}


def module_flag(compiler: str) -> str | None:
    """How this compiler wants the module output directory named, or None."""
    return MODULE_FLAG.get(os.path.basename(compiler))


def _normalized(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _matches(path: str, pattern: str) -> bool:
    """Does one source pattern cover this path.

    The same rule the code manifest's own reader applies, copied because
    the builder image cannot import it: matching ignores case, a leading
    "**/" means "at any depth, including none", and elsewhere "*"
    already crosses "/".
    """
    lowered = _normalized(path).lower()
    pattern = pattern.lower()
    if pattern.startswith("**/"):
        rest = pattern[3:]
        return fnmatch.fnmatchcase(lowered, rest) or fnmatch.fnmatchcase(lowered, f"*/{rest}")
    return fnmatch.fnmatchcase(lowered, pattern)


def is_tree_source(path: str, source_patterns) -> bool:
    """Does this code's own source-pattern list cover this path.

    The build stage asks it of every file a compile named; the mutation
    stage asks it of every file the manifest says implements the region,
    because a file the code does not call its own source is not one a
    port would be judged on.
    """
    return any(_matches(path, pattern) for pattern in source_patterns)


def _is_source(argument: str, cwd: str) -> bool:
    """Is this argument a Fortran file that is really on disk.

    Both halves matter. The extension alone would count "-o replay.f90",
    which names an output; being on disk alone would count a text file
    the build reads for some other reason.
    """
    if not any(argument.lower().endswith(ext) for ext in SOURCE_EXTENSIONS):
        return False
    return os.path.isfile(os.path.join(cwd, argument))


def _under(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory + os.sep)


def _display(path: str, tree_root: str) -> str:
    """A path named the way the tree names it, so a claim reads in its terms."""
    if _under(path, tree_root):
        return os.path.relpath(path, tree_root)
    return path


def compile_records(
    log_text: str, tree_dir, flags, source_patterns, *, harness_dir=HARNESS,
) -> list[dict]:
    """One record per compiler invocation the shim logged.

    Each record is::

        {"argv": [...], "cwd": str, "inputs": [...], "output": str | None,
         "has_flags": bool, "outside": [...]}

    `inputs` are the Fortran sources that invocation compiled, named
    relative to the tree root where they are inside it. `has_flags` says
    every one of the strategy's flags appeared on that command line.
    `outside` names the inputs that are not the tree's own source: a file
    from another directory, or a file in the tree that the code's own
    source patterns do not cover. Files under the harness directory are
    the builder's own and are never listed.

    A line the shim wrote that is not JSON raises ValueError: a log that
    cannot be read is not the same thing as a build that compiled
    nothing, and the caller must not treat it as one.
    """
    tree_root = os.path.realpath(str(tree_dir))
    harness_root = os.path.realpath(str(harness_dir))
    flags = list(flags)
    patterns = list(source_patterns)

    records = []
    for number, line in enumerate(log_text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"compiler log line {number} is not JSON: {line[:200]}") from exc
        argv = [str(a) for a in entry["argv"]]
        cwd = entry["cwd"]

        inputs = []
        outside = []
        for argument in argv:
            if not _is_source(argument, cwd):
                continue
            real = os.path.realpath(os.path.join(cwd, argument))
            shown = _display(real, tree_root)
            inputs.append(shown)
            if _under(real, harness_root):
                continue
            if not _under(real, tree_root) or not any(_matches(shown, p) for p in patterns):
                outside.append(shown)

        records.append({
            "argv": argv,
            "cwd": _display(os.path.realpath(cwd), tree_root),
            "inputs": inputs,
            "output": argv[argv.index("-o") + 1] if "-o" in argv[:-1] else None,
            "has_flags": all(flag in argv for flag in flags),
            "outside": outside,
        })
    return records


def _compiles(records) -> list[dict]:
    """The invocations that compiled Fortran, as against a link or a probe."""
    return [record for record in records if record["inputs"]]


def flags_reached_every_compile(records) -> bool:
    """Did the strategy's flags reach every compile, and was there one at all.

    A build whose log holds no compile is not a build that obeyed the
    strategy: it is one that never called the compiler the builder handed
    it, which is exactly what this check exists to catch.
    """
    compiles = _compiles(records)
    return bool(compiles) and all(record["has_flags"] for record in compiles)


def compiles_without_flags(records) -> list[list]:
    """The command lines that compiled Fortran without the strategy's flags."""
    return [record["argv"] for record in _compiles(records) if not record["has_flags"]]


def compiled_only_tree_source(records) -> bool:
    return not any(record["outside"] for record in records)


def files_outside_tree(records) -> list[str]:
    """Every compiled file that was not the submitted tree's own source, once each."""
    seen = []
    for record in records:
        for path in record["outside"]:
            if path not in seen:
                seen.append(path)
    return seen
