"""Where a code's files sit once it has been brought in, and the one line
that tells a manifest which of those places it is describing.

Trust role: these names are a contract between programs that never call
each other. Promoting writes the manifest, the baseline, the datasets
and the captures under a code's directory; the deployment mounts that
directory; the seed script reads the manifest out of it; and the sealed
oracle image reads the captures out of a copy of it. Two of those
spelling a directory differently would leave a deployment reading an
empty answer rather than reporting a missing one, so the spellings live
here and every reader that can import this package takes them from here.

The same file describes a code from two places. Beside the code, the
manifest's source root is the `baseline` directory next to it; inside a
tree being onboarded, it is the tree itself. The rewrite between the two
replaces one line rather than round-tripping the YAML, because a
manifest is a file a person wrote and will read again: loading and
dumping it would silently discard every comment in it and re-order what
is left. A manifest written any other way -- the root on a flow mapping,
two `root:` keys, a comment on the same line -- is refused rather than
guessed at, because a wrong guess would point a manifest at a tree that
is not the one beside it.
"""
from __future__ import annotations

import re

from equivalent.manifest.schema import IN_TREE_MANIFEST, IN_TREE_SOURCE_ROOT

# What a code's own manifest is called beside its directory. The
# deployment's configuration may name any path under `programs`, but this
# is where promoting puts one, and it is what the seed script looks for.
MANIFEST_NAME = "manifest.yaml"
# What the promoted manifest says its source root is: the directory the
# tree is written into, beside the manifest.
BASELINE_DIR = "baseline"
# Where a code keeps the answers a port is compared against, under its
# own directory. The oracle spells this too, in its own words: it cannot
# import this package, and that is the price of it being sealed.
CAPTURES_DIR = "captures"
# Where a code keeps the datasets a region may name, under its own
# directory. One spelling, so the deployment and the promotion agree.
DATASETS_DIR = "datasets"

# The `source:` mapping of a manifest, and a line inside it saying what
# the source root is.
SOURCE_KEY = re.compile(r"^source:\s*(#.*)?$")
TOP_LEVEL = re.compile(r"^\S")


def _root_line(value: str) -> re.Pattern:
    """A line of a manifest that says the source root is this, and nothing else."""
    return re.compile(rf"^(\s*)root:\s*{re.escape(value)}\s*$")


def _source_block(lines: list[str], expected: str) -> range:
    """The lines the manifest's top-level `source:` mapping spans."""
    start = None
    for number, line in enumerate(lines):
        if start is None:
            if SOURCE_KEY.match(line):
                start = number + 1
        elif line.strip() and TOP_LEVEL.match(line):
            return range(start, number)
    if start is None:
        raise ValueError(
            f"the manifest at {IN_TREE_MANIFEST} has no `source:` section written as a "
            f"block of its own; promoting rewrites one line, so spell it as `source:` "
            f"with `root: {expected}` on a line of its own beneath it"
        )
    return range(start, len(lines))


def _rewrite_source_root(text: str, expected: str, replacement: str) -> str:
    """The same manifest with its source root changed, and everything else untouched.

    Exactly one line inside `source:` may say the root, and it may say
    nothing else.
    """
    lines = text.splitlines(keepends=True)
    pattern = _root_line(expected)
    found = [n for n in _source_block(lines, expected) if pattern.match(lines[n])]
    if len(found) != 1:
        raise ValueError(
            f"the manifest at {IN_TREE_MANIFEST} has {len(found)} lines inside `source:` "
            f"saying `root: {expected}`, and promoting rewrites exactly one; spell it as "
            f"`root: {expected}` on a line of its own, with any comment on the line above"
        )
    indent = pattern.match(lines[found[0]]).group(1)
    lines[found[0]] = f"{indent}root: {replacement}\n"
    return "".join(lines)


def promoted_manifest_text(tree_text: str) -> str:
    """The tree's manifest as it is written beside the code: source root `baseline`."""
    return _rewrite_source_root(tree_text, IN_TREE_SOURCE_ROOT, BASELINE_DIR)


def in_tree_manifest_text(promoted_text: str) -> str:
    """The code's manifest as a tree being onboarded carries it: source root `.`.

    The other direction of the same rewrite, for whoever is writing the
    manifest into a working copy: it is the same file with the same
    comments, saying that its source is the tree it sits in.
    """
    return _rewrite_source_root(promoted_text, BASELINE_DIR, IN_TREE_SOURCE_ROOT)
