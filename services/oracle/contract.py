"""What the oracle answers with, once, so a reader and the gateway agree.

Trust role: these three answers are the only quantitative statements
anybody gets about whether a port reproduced the code's captured
answers. The gateway turns them into claims, so a key an error path
forgot to write would read downstream as "this was not compared" when
the truth is "this was not reported". Declaring the shapes here, and
serving every route through them, is what keeps the two sides of that
wire the same shape.

The held-out dataset's answer carries no per-case detail, by design and
not by omission: `per_case` is absent from a held-out reply rather than
empty, so nothing quantitative about a held-out case can leak upstream
through the shape itself.

This module is part of the sealed image, which installs numpy, yaml and
the web server and nothing else of this project, so it imports nothing
from `equivalent`.
"""
from pydantic import BaseModel

# One case's arrays as they travel: {variable: base64 of its .npy file}.
Arrays = dict[str, str]


class PolicyResponse(BaseModel):
    """Which tolerance policy this oracle judges by, and who is judging."""

    policy_version: str
    # The hash of the policy file itself, which every verdict carries so a
    # reader can tell which bands decided it.
    policy_sha256: str
    # The hash of every trusted input that can change a verdict: this
    # service's source, the comparator, the policy, and the captures.
    oracle_identity: str


class HoldoutInputsResponse(BaseModel):
    """The held-out inputs, served once at acceptance -- never the answers."""

    dataset: str
    cases: dict[str, Arrays]


class CompareResponse(BaseModel):
    """One dataset's verdict, and for the visible set the detail behind it."""

    verdict: str          # "pass" or "fail"
    dataset: str
    policy_sha256: str
    oracle_identity: str
    # Per case: what the comparator said about every variable. Present for
    # the visible dataset, which the agent is allowed to read, and absent
    # for the held-out one.
    per_case: dict | None = None
