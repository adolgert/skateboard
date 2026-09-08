"""Reading a mapping somebody wrote by hand.

Trust role: the manifest, the strategy, and the reviewed original
reference are all files a person edits, and each of them decides
something a claim then stands on -- what gets compiled, what the code
is, what a port is compared against. The dangerous failure is not a file
that fails to load; it is a field quietly ignored because it was
misspelled, because then the file says one thing, the run does another,
and nothing says so. So every loader reads its mappings through here,
which refuses a missing field and an unknown key alike and names where
it was looking.

This lives in the ledger package because that is the one package every
loader already rests on, and putting it in any of them would make the
others import a file format they have nothing to do with. Nothing here
knows what any of those files mean.
"""
from __future__ import annotations


def check_keys(given, required, where: str, *, optional=(), allow_extra: bool = False) -> None:
    """Refuse this mapping unless it says exactly what it is allowed to say.

    `where` is the words a person needs to find the mistake: which file,
    and which part of it. `allow_extra` is for the one shape that takes
    names this reader cannot know in advance, such as a code's own
    dataset names.
    """
    if not isinstance(given, dict):
        raise ValueError(f"{where} is not a mapping")
    missing = [field for field in required if field not in given]
    if missing:
        raise ValueError(f"{where} missing field(s): {missing}")
    if allow_extra:
        return
    unknown = sorted(set(given) - set(required) - set(optional))
    if unknown:
        raise ValueError(
            f"{where} has unknown key(s): {unknown}; allowed: {sorted((*required, *optional))}"
        )
