"""What a backend answered, in the shape a check reads it.

Trust role: none of these decide anything -- they are what the builder
and the oracle said, typed. Saying which fields a check may read, once
and here, is what stops a check from asking for a key a service never
writes and reading the resulting None as a measurement. It is also where
an answer that cannot be read at all -- a count that is not a whole
number, a table that is not a table, a verdict that is neither word --
stops being anything: that is a fault on the harness's side, so it raises
ComponentError and no claim is filed, rather than becoming a verdict that
reads as "this port is wrong".

Every field has a default, so an answer that omitted a key is read as the
empty thing rather than as None turning up three modules later.

These live beside the checks rather than beside the HTTP clients because
the checks are what read them: twelve of them do, and the clients only
hand them over. The services spell the same shapes in
services/builder/contract.py and services/oracle/contract.py. The
services refuse to install this package, so neither side can import the
other; equivalent/tests/test_builder_parity.py and
equivalent/tests/test_oracle_parity.py are what notice when the two
spellings drift apart.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from typing import ClassVar, Protocol

from equivalent.ledger.vocabulary import FAIL, PASS

from .errors import ComponentError

# What a digest field has to look like before anything is allowed to
# believe it names a file: a claim's material is built out of one.
DIGEST = re.compile(r"[0-9a-f]{64}")


def whole_number(kind: str, name: str, value, *, optional: bool = False):
    """One count from a builder answer, or the error that it is not one.

    A count says how many times something happened, so False is not zero
    and "3" is not three: a value that is not a whole number at or above
    zero is an answer nobody can read, and reading it as a small number
    would understate what ran.
    """
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ComponentError(
            f"the builder's {kind} answer gives no whole number of {name}: {value!r}"
        )
    return value


@dataclass(frozen=True)
class Answer:
    """The part of one service response the checks read.

    Anything a service writes and no check reads is dropped here; adding
    a field is how a check starts reading one.
    """

    # Who said it and what the answer is called, in the message when it
    # cannot be read.
    SPEAKER: ClassVar[str] = "builder"
    KIND: ClassVar[str] = "answer"
    # Fields that must be whole numbers, tables, lists, or digests for the
    # answer to mean anything at all.
    COUNTS: ClassVar[tuple[str, ...]] = ()
    OPTIONAL_COUNTS: ClassVar[tuple[str, ...]] = ()
    TABLES: ClassVar[tuple[str, ...]] = ()
    LISTS: ClassVar[tuple[str, ...]] = ()
    DIGESTS: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def parse(cls, data):
        """One JSON body as this answer, or ComponentError if it is not one."""
        if not isinstance(data, dict):
            raise ComponentError(f"the {cls.SPEAKER}'s {cls.KIND} answer is not an object")
        cls._check_envelope(data)
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    @classmethod
    def _check_envelope(cls, data: dict) -> None:
        """Whatever the body has to say before its fields are read at all."""

    def __post_init__(self):
        for name in self.COUNTS:
            whole_number(self.KIND, name, getattr(self, name))
        for name in self.OPTIONAL_COUNTS:
            whole_number(self.KIND, name, getattr(self, name), optional=True)
        for name in self.TABLES:
            if not isinstance(getattr(self, name), dict):
                raise ComponentError(
                    f"the {self.SPEAKER}'s {self.KIND} answer has no {name} table"
                )
        for name in self.LISTS:
            if not isinstance(getattr(self, name), list):
                raise ComponentError(
                    f"the {self.SPEAKER}'s {self.KIND} answer has no list of {name}"
                )
        for name in self.DIGESTS:
            value = getattr(self, name)
            if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
                raise ComponentError(
                    f"the {self.SPEAKER}'s {self.KIND} answer gives no {name}: {value!r}"
                )

    def as_dict(self) -> dict:
        """The answer as a claim can carry it, which a dataclass cannot be."""
        return asdict(self)


@dataclass(frozen=True)
class BuilderAnswer(Answer):
    """One builder response, which always says whether what was asked for ran."""

    SPEAKER = "builder"
    KIND = "builder"
    # Where the wire carries a `stage`, what that stage must say.
    STAGE: ClassVar[str | None] = None

    ok: bool = False

    @classmethod
    def _check_envelope(cls, data: dict) -> None:
        stage = data.get("stage")
        if cls.STAGE is not None and stage is not None and stage != cls.STAGE:
            raise ComponentError(
                f"the builder answered stage {stage!r} where {cls.STAGE!r} was asked for"
            )

    def __post_init__(self):
        if not isinstance(self.ok, bool):
            raise ComponentError(
                f"the builder's {self.KIND} answer gives no yes-or-no outcome: {self.ok!r}"
            )
        super().__post_init__()


@dataclass(frozen=True)
class BuildResponse(BuilderAnswer):
    KIND = STAGE = "build"
    TABLES = ("targets",)
    LISTS = ("compiles",)

    targets: dict = field(default_factory=dict)   # role -> {executable, built, sha256, size}
    compiles: list = field(default_factory=list)  # one record per compiler invocation
    flags: list = field(default_factory=list)
    # The two statements the compiler log exists to make.
    flags_reached_every_compile: bool = False
    compiled_only_tree_source: bool = False
    minfo_excerpt: str = ""       # the compiler's own account of what it offloaded
    missing_targets: list | None = None
    executor_identity: str | None = None
    image_id: str | None = None
    log_tail: str = ""


@dataclass(frozen=True)
class RunResponse(BuilderAnswer):
    KIND = STAGE = "run"
    COUNTS = ("kernels_launched",)
    TABLES = ("outputs",)
    LISTS = ("launches",)

    outputs: dict = field(default_factory=dict)   # case -> {variable: base64 npy}
    kernels_launched: int = 0
    # One entry per distinct launch source, so the claim says what ran and
    # not only how much of it ran.
    launches: list = field(default_factory=list)
    executable_identity: dict | None = None
    log_tail: str = ""


@dataclass(frozen=True)
class CaptureResponse(BuilderAnswer):
    KIND = STAGE = "capture"
    TABLES = ("cases",)

    cases: dict = field(default_factory=dict)     # case -> {"inputs": {...}, "outputs": {...}}
    executable_identity: dict | None = None
    stdout_tail: str = ""


@dataclass(frozen=True)
class SanitizeResponse(BuilderAnswer):
    KIND = STAGE = "sanitize"
    TABLES = ("per_tool",)

    per_tool: dict = field(default_factory=dict)  # tool -> {ok, errors, log_tail} or {ok, error}
    executable_identity: dict | None = None
    log_tail: str = ""

    def __post_init__(self):
        super().__post_init__()
        for tool, result in self.per_tool.items():
            if not isinstance(result, dict):
                raise ComponentError(
                    f"the builder's sanitize answer for {tool!r} is not an object"
                )
            if result.get("errors") is not None:
                whole_number("sanitize", f"{tool} errors", result["errors"])


@dataclass(frozen=True)
class PropertiesResponse(BuilderAnswer):
    KIND = STAGE = "properties"
    COUNTS = (
        "passed", "failed", "errors", "skipped", "deselected", "xfailed", "xpassed",
        "collected", "executed",
    )
    # None is an answer here: it says the protected trace observed nothing,
    # which is a failed property claim rather than an unreadable one.
    OPTIONAL_COUNTS = ("replays_observed",)

    # A property run is repeatable only if the claim says how it was drawn,
    # so both are echoed back and compared with what was asked for.
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

    def counts(self) -> dict:
        """The nine numbers pytest reported, which the verdict is read from."""
        return {name: getattr(self, name) for name in self.COUNTS}


@dataclass(frozen=True)
class MutateResponse(BuilderAnswer):
    KIND = STAGE = "mutate"
    COUNTS = ("generated", "scored")
    TABLES = ("counts",)
    LISTS = ("results", "kept_dirs")

    generated: int = 0
    scored: int = 0
    results: list = field(default_factory=list)   # one row per mutant and its verdict
    counts: dict = field(default_factory=dict)
    # The directories of the verdicts a person has to read the source of.
    kept_dirs: list = field(default_factory=list)
    log_tail: str = ""

    def __post_init__(self):
        super().__post_init__()
        for row in self.results:
            if not isinstance(row, dict):
                raise ComponentError("the builder's mutate answer holds a row that is not an object")


@dataclass(frozen=True)
class TimeResponse(BuilderAnswer):
    KIND = STAGE = "time"
    LISTS = ("runs_s", "outputs")

    runs_s: list = field(default_factory=list)
    outputs: list = field(default_factory=list)   # the declared files, collected once per run
    gpu_exclusive: bool | None = None
    executable_identity: dict | None = None
    log_tail: str = ""


@dataclass(frozen=True)
class ExecutableIdentity:
    """One executable the builder still holds, as it reverified it.

    Trust role: a build claim says a target was built and names its bytes;
    this is the builder's own later answer about the same file, asked for
    when a cached build is about to be trusted again. What "the same
    executable" means is written here once rather than as three key
    lookups at the place the two are compared.
    """

    executable: str
    sha256: str | None = None
    size: int | None = None
    verified: bool = False

    @classmethod
    def reported(cls, executable: str, reported) -> "ExecutableIdentity":
        """One entry of the builder's artifacts table, by the name it is under."""
        if not isinstance(reported, dict):
            raise ComponentError(
                f"the builder's artifacts answer for {executable!r} is not an object"
            )
        return cls(
            executable=executable,
            sha256=reported.get("sha256"),
            size=reported.get("size"),
            verified=reported.get("verified") is True,
        )

    def matches(self, target: dict) -> bool:
        """Whether this is the executable a build claim's target names.

        An entry the builder did not reverify matches nothing: it is no
        longer saying those bytes are there.
        """
        return (
            self.verified
            and self.sha256 == target.get("sha256")
            and self.size == target.get("size")
        )


@dataclass(frozen=True)
class ArtifactsResponse(BuilderAnswer):
    KIND = "artifacts"
    TABLES = ("executables",)

    # relative path -> what the builder reverified about that file
    executables: dict = field(default_factory=dict)
    executor_identity: str | None = None

    def __post_init__(self):
        super().__post_init__()
        object.__setattr__(self, "executables", {
            name: entry if isinstance(entry, ExecutableIdentity)
            else ExecutableIdentity.reported(name, entry)
            for name, entry in self.executables.items()
        })


@dataclass(frozen=True)
class HealthResponse(BuilderAnswer):
    KIND = "health"
    TABLES = ("tools", "python_modules")

    # Keyed exactly as a strategy's `required_tools` spells them, so the
    # gateway can compare the two without translating.
    tools: dict = field(default_factory=dict)
    python_modules: dict = field(default_factory=dict)
    executor_identity: str | None = None


@dataclass(frozen=True)
class OracleAnswer(Answer):
    """One oracle response. The oracle answers questions, not requests, so
    nothing here says whether something ran -- only what it knows."""

    SPEAKER = "oracle"
    KIND = "oracle"


@dataclass(frozen=True)
class PolicyResponse(OracleAnswer):
    """Which bands the oracle judges by, and which oracle this is.

    The identity is what a region is pinned to after review, so an answer
    that names none, or names something that is not a digest, is an
    oracle nobody can pin -- unreadable rather than unwelcome.
    """

    KIND = "policy"
    DIGESTS = ("policy_sha256", "oracle_identity")

    policy_sha256: str = ""
    oracle_identity: str = ""


@dataclass(frozen=True)
class HoldoutInputsResponse(OracleAnswer):
    """The held-out inputs, and nothing the answers could be read out of."""

    KIND = "holdout inputs"
    TABLES = ("cases",)

    cases: dict = field(default_factory=dict)     # case -> {variable: base64 npy}


@dataclass(frozen=True)
class CompareResponse(OracleAnswer):
    """One dataset judged. `per_case` is empty for the held-out dataset by
    the oracle's own design, so a check must not read an empty one as
    agreement."""

    KIND = "compare"
    DIGESTS = ("policy_sha256",)
    TABLES = ("per_case",)

    verdict: str = ""
    policy_sha256: str = ""
    per_case: dict = field(default_factory=dict)  # case -> {"pass": bool, ...}

    def __post_init__(self):
        super().__post_init__()
        if self.verdict not in (PASS, FAIL):
            raise ComponentError(
                f"the oracle's compare answer gives no verdict: {self.verdict!r}"
            )

    def cases_that_failed(self) -> list:
        """The visible cases the oracle put outside the bands, in name order."""
        return [name for name in sorted(self.per_case) if not self.per_case[name].get(PASS)]


class Builder(Protocol):
    """What a check may ask the builder to do.

    The gateway hands a check its real HTTP client and a test hands it a
    fake; this is the whole of what either has to answer, so a check
    cannot quietly start depending on something only one of them has.
    """

    def build(self, attempt_id: str, tree: list[dict], makefile: str, targets: list[dict],
              compiler: str, flags: list[str], link_flags: list[str],
              source_patterns: list[str]) -> BuildResponse: ...

    def run(self, attempt_id: str, executable: str, cases: dict,
            notify: str | None = None, mandatory: bool = False) -> RunResponse: ...

    def capture(self, attempt_id: str, executable: str, args: list[str],
                run_name: str) -> CaptureResponse: ...

    def sanitize(self, attempt_id: str, executable: str, cases: dict,
                 tools: list[str]) -> SanitizeResponse: ...

    def properties(self, attempt_id: str, executable: str, module: str, cases: dict,
                   seed: int, max_examples: int) -> PropertiesResponse: ...

    def mutate(self, attempt_id: str, makefile: str, replay_target: dict, files: list[str],
               cases: dict, bands: dict, compiler: str, flags: list[str],
               link_flags: list[str], source_patterns: list[str],
               jobs: int | None = None, limit: int | None = None) -> MutateResponse: ...

    def time(self, attempt_id: str, executable: str, args: list[str], env: dict,
             outputs: list[str], repeats: int = 5, budget_s: int = 300,
             expected_outputs: dict[str, str] | None = None) -> TimeResponse: ...


class Oracle(Protocol):
    """What a check may ask the oracle. It is asked, never told."""

    def policy(self) -> PolicyResponse: ...

    def holdout_inputs(self) -> HoldoutInputsResponse: ...

    def compare(self, dataset: str, outputs: dict,
                attempt_id: str = "unknown") -> CompareResponse: ...
