"""What a check made and the gateway may keep.

Trust role: a check never writes to a region's ledger. It packs what it
produced -- a dataset of arrays, a blob of bytes -- into a place of its
own, names it by the hash of its content, and hands that back with its
verdict. The gateway is the one that files it, and only for a verdict it
is about to record. So a set that was packed and then not kept is never
something a later comparison can find, which is the whole point of
separating the two: a check that fails must leave nothing behind for a
check that follows to compare against.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PackedSet:
    """A dataset written into a directory of its own, named by its content.

    `kind` is the subject kind the sha names ("capture_set"), `name` is
    what the manifest calls the dataset and is deliberately not part of
    the hash, and `directory` is a temporary place that goes away with
    this value unless the gateway keeps it.
    """

    kind: str
    name: str
    sha256: str
    directory: Path


@dataclass(frozen=True)
class PackedArtifact:
    """One blob a check wants kept, named by the hash of its own bytes."""

    sha256: str
    data: bytes
