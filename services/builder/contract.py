"""What a caller may ask the builder for, and what every answer holds.

Trust role: the gateway turns these answers into claims about a build
and a run that a person is meant to be able to check later. A key that
an error path forgot to write is therefore not a cosmetic problem: it
reads downstream as "this was not measured" when the truth is "this was
not reported". So the shape of every response lives here, once, with a
default for every field and a failure constructor that fills the whole
shape in -- a stage cannot answer with less than it promised.

The request models are the other half of the same idea. Nothing the
builder runs is invented by the builder: the tree, the makefile, the
targets, the compiler, the flags and the executables all arrive in one
of these, having been read by the gateway from the code's hashed
manifest and the hashed strategy file.
"""
from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field


class TreeFile(BaseModel):
    path: str    # relative to the tree root, directories included
    b64: str     # the file's bytes; a tree holds namelists and data, not only text


class ReplayTarget(BaseModel):
    target: str      # what `make` is asked for
    executable: str  # what that target must leave in the tree, relative to its root


class BuildTarget(ReplayTarget):
    role: str        # what the manifest calls this target: replay, timing, capture


# One case's arrays as they travel: {variable: base64 of that variable's
# .npy file}. The file says what type and shape the array is, so nothing
# on the wire repeats it.
Arrays = dict[str, str]
# What a replay, a sanitizer or a property run is given: {case: its inputs}.
Cases = dict[str, Arrays]
# And what a capture produced: each case's inputs beside the answers the
# baseline wrote for them, keyed "inputs" and "outputs".
CapturedCases = dict[str, dict[str, Arrays]]
# The tolerance policy's band per output variable, as the policy file
# spells it. What is inside one band is deliberately left alone: `ulp` is
# a count of representable steps and `abs` and `rel` are real numbers,
# and a band whose integer had been widened to a float on the way in
# would be a different policy from the one the oracle checked.
Bands = dict[str, dict]


class BuildRequest(BaseModel):
    attempt_id: str
    # The WHOLE tracked tree, not a filtered source list: a code's build
    # reads include files, namelists, and small data files that no
    # extension test would recognize.
    tree: list[TreeFile]
    makefile: str
    targets: list[BuildTarget]
    compiler: str
    flags: list[str] = []
    link_flags: list[str] = []
    # What the code calls its own source, used to say whether the build
    # compiled anything the manifest never described.
    source_patterns: list[str] = []


class RunRequest(BaseModel):
    attempt_id: str
    executable: str  # the manifest's replay target
    # The file says what type and shape each array is; nothing else has to.
    cases: Cases
    # The strategy's device proof: which offload runtime should be asked
    # to announce its kernel launches, or none at all.
    notify: str | None = None
    mandatory: bool = False


class CaptureRequest(BaseModel):
    attempt_id: str
    executable: str  # the manifest's capture target
    # The dataset's own arguments, from the manifest. The directory to
    # write into is added after them by the builder, which is the one
    # thing the capture contract fixes.
    args: list[str] = []
    run_name: str  # what to call this run's output directory


class SanitizeRequest(BaseModel):
    attempt_id: str
    executable: str
    # One entry per case to sanitize. How many cases that is comes from the
    # gateway's hashed strategy file; the builder runs whatever it is sent.
    cases: Cases
    tools: list = ["memcheck", "racecheck"]


class PropertiesRequest(BaseModel):
    attempt_id: str
    executable: str  # the manifest's replay target, which the properties call
    module: str      # the manifest's properties module, relative to the tree root
    # The visible cases, which become the corpus the code's own properties
    # draw from.
    cases: Cases
    seed: int
    max_examples: int


class MutateRequest(BaseModel):
    attempt_id: str
    makefile: str
    # The manifest's replay target: what `make` is asked for, and what it
    # must leave behind for each mutant to be replayed.
    replay_target: ReplayTarget
    # The files the manifest says implement the region. Nothing here
    # decides which those are; the gateway read them from the manifest.
    files: list[str] = []
    # The visible capture set: the inputs are replayed and the outputs are
    # what each mutant is scored against.
    cases: CapturedCases = {}
    # What decides whether a changed answer was noticed.
    bands: Bands = {}
    compiler: str
    flags: list[str] = []
    link_flags: list[str] = []
    source_patterns: list[str] = []
    jobs: int | None = None
    limit: int | None = None


class TimeRequest(BaseModel):
    attempt_id: str
    executable: str  # the manifest's timing target
    args: list[str] = []
    # Values, not code: the manifest's own environment for a fair measurement.
    env: dict[str, str] = {}
    outputs: list[str] = []  # files the run must write, collected and returned
    repeats: int = Field(default=5, ge=1)
    budget_s: int = Field(default=300, ge=1)
    # Optional exact expected bytes, base64 encoded by output path.  This is
    # used when a trusted parent already holds a reference and wants every
    # measured repetition checked before the builder calls the timing valid.
    expected_outputs: dict[str, str] | None = None


class Response(BaseModel):
    """One stage's whole answer, successful or not.

    Every field has a default, so `failure` can fill the shape in from
    one sentence and a reader downstream never has to tell a key that is
    missing from a key that is false.
    """

    # Which field a failure's sentence goes in, because a stage that
    # collects a program's own output calls it that rather than a log.
    message_field: ClassVar[str | None] = "log_tail"

    ok: bool = False

    def to_dict(self) -> dict:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict):
        return cls.model_validate(data)

    @classmethod
    def failure(cls, message: str = "", **fields):
        """A failed answer that still carries every key the shape declares."""
        if message and cls.message_field:
            fields.setdefault(cls.message_field, message)
        return cls(ok=False, **fields)


class BuildResponse(Response):
    stage: str = "build"
    command: list[str] = []          # what `make` was actually asked for
    targets: dict = {}               # role -> {executable, built, sha256, size}
    compiles: list = []              # one record per compiler invocation
    compiler_audit: dict = {}        # who recorded those invocations, and how
    flags: list[str] = []
    link_flags: list[str] = []
    # The two statements the compiler log exists to make.
    flags_reached_every_compile: bool = False
    compiled_only_tree_source: bool = False
    minfo_excerpt: str = ""          # the compiler's own account of what it offloaded
    missing_targets: list[str] | None = None
    executor_identity: str | None = None
    image_id: str | None = None
    log_tail: str = ""


class RunResponse(Response):
    stage: str = "run"
    outputs: dict = {}               # case -> {variable: base64 npy the driver wrote}
    kernels_launched: int = 0
    # One entry per distinct launch source across every case, so the claim
    # says what ran and not only how much of it ran.
    launches: list = []
    profiler: str | None = None
    executable_identity: dict | None = None
    case: str | None = None          # which case a failure was in
    log_tail: str = ""


class CaptureResponse(Response):
    message_field: ClassVar[str | None] = "stdout_tail"

    stage: str = "capture"
    cases: dict = {}                 # case -> {"inputs": {...}, "outputs": {...}}
    executable_identity: dict | None = None
    stdout_tail: str = ""


class SanitizeResponse(Response):
    stage: str = "sanitize"
    per_tool: dict = {}              # tool -> {ok, errors, log_tail} or {ok: None, error}
    executable_identity: dict | None = None
    log_tail: str = ""


class PropertiesResponse(Response):
    stage: str = "properties"
    # A property run is repeatable only if the claim says how it was drawn.
    seed: int | None = None
    max_examples: int | None = None
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    deselected: int = 0
    xfailed: int = 0
    xpassed: int = 0
    collected: int = 0
    executed: int = 0
    replays_observed: int | None = None
    counts_source: str | None = None
    executable_identity: dict | None = None
    log_tail: str = ""


class MutateResponse(Response):
    stage: str = "mutate"
    generated: int = 0
    scored: int = 0
    results: list = []               # one row per mutant and its verdict
    counts: dict = {}
    # The directories of the verdicts a person has to read the source of.
    kept_dirs: list = []
    log_tail: str = ""


class TimeResponse(Response):
    stage: str = "time"
    runs_s: list = []
    outputs: list = []               # the declared files, collected once per run
    gpu_exclusive: bool | None = None
    executable_identity: dict | None = None
    stdout_tail: str = ""
    log_tail: str = ""


class ArtifactsResponse(Response):
    message_field: ClassVar[str | None] = None

    attempt_id: str = ""
    # relative path -> {sha256, size, role, verified}
    executables: dict = {}
    executor_identity: str | None = None
    image_id: str | None = None


class HealthResponse(Response):
    message_field: ClassVar[str | None] = None

    # Keyed exactly as a strategy's `required_tools` spells them, so the
    # gateway can compare the two without translating.
    tools: dict = {}
    python_modules: dict = {}
    isolation: dict = {}
    executor_identity: str | None = None


# Every endpoint's pair, for a caller that wants to name one by its path.
ENDPOINTS = {
    "build": (BuildRequest, BuildResponse),
    "run": (RunRequest, RunResponse),
    "capture": (CaptureRequest, CaptureResponse),
    "sanitize": (SanitizeRequest, SanitizeResponse),
    "properties": (PropertiesRequest, PropertiesResponse),
    "mutate": (MutateRequest, MutateResponse),
    "time": (TimeRequest, TimeResponse),
}
