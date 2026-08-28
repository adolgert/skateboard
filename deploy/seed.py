#!/usr/bin/env python3
"""Writes the baseline the gateway's repository starts from.

Trust role: this decides what "the baseline" is. Every frozen-set hash,
every timing/baseline claim, and every diff a reviewer reads is relative
to these bytes, so a seed that quietly picked up an extra file -- or a
stale one -- would make every later claim describe a starting point
nobody chose.

The baseline is exactly the files git tracks under the demonstration
work tree, read out of the commit rather than copied off disk. Copying
the directory would sweep in build products: that directory is also
where the Fortran is compiled by hand, so it accumulates .mod files and
a binary that are not part of the baseline and would change its hash
from machine to machine.

    python3 deploy/seed.py <out_dir>
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

# The tracked directory the baseline is taken from, and the commit it is
# read at. Both are deliberately fixed: a seed is not a place to choose
# between versions of the code.
BASELINE_DIR = "demo/work"
BASELINE_REF = "HEAD"


def _git(repo_root, *args) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, check=True,
    ).stdout


def baseline_paths(repo_root) -> list[str]:
    """The tracked files under the baseline directory, named relative to it."""
    listing = _git(
        repo_root, "ls-tree", "-r", "--name-only", BASELINE_REF, "--", BASELINE_DIR,
    ).decode("utf-8")
    prefix = f"{BASELINE_DIR}/"
    return sorted(
        line[len(prefix):] for line in listing.splitlines()
        if line.startswith(prefix)
    )


def write_seed(repo_root, out_dir) -> list[str]:
    """Write the baseline into `out_dir`; return the paths written, relative to it.

    Existing files are overwritten and nothing is removed, so running
    this twice over the same directory is safe and leaves anything a
    person put there alone.
    """
    repo_root = Path(repo_root)
    out_dir = Path(out_dir)
    written = []
    for relative in baseline_paths(repo_root):
        content = _git(repo_root, "show", f"{BASELINE_REF}:{BASELINE_DIR}/{relative}")
        destination = out_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        written.append(relative)
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="seed", description="write the baseline files the gateway's repository starts from",
    )
    parser.add_argument("out_dir", help="directory to write the baseline into")
    parser.add_argument(
        "--repo-root", default=str(Path(__file__).resolve().parents[1]),
        help="the git checkout the baseline is read from",
    )
    args = parser.parse_args(argv)

    for relative in write_seed(args.repo_root, args.out_dir):
        print(f"{args.out_dir}/{relative}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
