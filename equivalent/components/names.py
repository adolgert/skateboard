"""The words the checks and the code's own files agree on.

Trust role: two checks that spell the same thing differently are two
checks measuring different things while appearing to measure one. The
role of the program a timing run measures, the two halves of the
tolerance policy, the dataset a port is judged by -- each of these is
read out of a manifest or a policy file by more than one check, so each
is written here once.

The dataset names come from the manifest schema rather than being spelled
again: which datasets a code must declare is the manifest's statement
about itself, and this only says which of them the checks single out.
The words a claim is written in come from the ledger, which is where a
claim is stored.
"""
from __future__ import annotations

import json

from equivalent.ledger import vocabulary
from equivalent.manifest.schema import REQUIRED_BUILD_TARGET, REQUIRED_DATASETS

# What a timing claim calls the set of program outputs it left behind is
# the ledger's word, said again here so a check reads every name it needs
# from one place.
PROGRAM_SET_KEY = vocabulary.PROGRAM_SET_KEY

# The manifest roles: the program a timing run measures, the driver that
# replays one case, and the program that writes a dataset. Every manifest
# must offer the replay driver, which is why the schema names that one.
TIMING_ROLE = "timing"
REPLAY_ROLE = REQUIRED_BUILD_TARGET
CAPTURE_ROLE = "capture"

# The two datasets a port is judged by. The visible one is what the agent
# can see; the held-out one is what it cannot. A code may declare more,
# and nothing is held back in those.
VISIBLE, HOLDOUT = REQUIRED_DATASETS

# The two band maps a tolerance policy holds: one per region output
# variable, and one per file the timing run writes. They are separate
# because they band separate measurements -- one call of the region, and a
# whole run of the program -- and a band calibrated for one says nothing
# about the other.
VARIABLE_BANDS = "variables"
FILE_BANDS = "files"


def bands(policy_bytes: bytes) -> tuple[dict, dict]:
    """A tolerance policy's two band maps, or why it is not a policy.

    Three checks read this file: the one that judges it while a code is
    being brought in, the one that scores mutants within it, and the one
    that compares a port's program outputs under it. Read three ways they
    could disagree about what a policy is, and a port would be judged
    within bands its own code was never checked against.

    Raises ValueError, which says only that the file does not read as a
    policy. Whether that is a verdict about the code or a fault on the
    harness's side depends on who wrote the file, so the caller decides.
    """
    try:
        policy = json.loads(policy_bytes)
    except ValueError as exc:
        raise ValueError(f"the tolerance file does not read as JSON: {exc}") from None
    if not isinstance(policy, dict):
        raise ValueError("the tolerance file is not a policy")
    for section, what in ((VARIABLE_BANDS, "output variable"), (FILE_BANDS, "timing output")):
        if not isinstance(policy.get(section), dict):
            raise ValueError(
                f"the tolerance file has no '{section}' map naming a band per {what}"
            )
    return policy[VARIABLE_BANDS], policy[FILE_BANDS]
