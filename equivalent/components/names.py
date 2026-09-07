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
"""
from __future__ import annotations

from equivalent.manifest.schema import REQUIRED_DATASETS

# The manifest roles: the program a timing run measures, the driver that
# replays one case, and the program that writes a dataset.
TIMING_ROLE = "timing"
REPLAY_ROLE = "replay"
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

# What a timing claim's detail calls the set of program outputs it left
# behind, which is what a port's own program run is compared against.
PROGRAM_SET_KEY = "program_set"
