"""Thin clients for the builder and oracle services, matching their real
HTTP contracts (services/builder/app.py, services/oracle/app.py) exactly.

Trust role: none -- these carry bytes between the gateway and two
services that are themselves trusted for what they measure (builder) or
what they know (oracle). Nothing here decides pass or fail; the
equivalent/components/*.py modules that call these do that, from what
comes back.

What comes back is a typed answer rather than a bare dictionary. The
builder writes one shape per endpoint and the gateway reads part of it;
saying which part here, once, is what stops a component from asking for
a key the builder never writes and reading the resulting None as a
measurement. It is also where an answer that cannot be read at all --
a count that is not a whole number, a table that is not a table -- stops
being anything: that is a fault on the harness's side, so it raises
ComponentError and no claim is filed, rather than becoming a verdict
that reads as "this port is wrong".

The builder spells the same shapes in services/builder/contract.py. The
services refuse to install this package, so neither file can import the
other; equivalent/tests/test_builder_parity.py is what notices when the
two drift apart.

Components take a client object rather than a URL so tests can pass a
fake with the same methods and no network, subprocess, or GPU involved --
unlike sese_check's check_sese.py, nvfortran/compute-sanitizer/a GPU
aren't available in this development environment at all.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import ClassVar

import httpx

from equivalent.components.errors import ComponentError

# A build, a sanitizer pass, or five timed runs of the full program can
# each take minutes; httpx's default of five seconds per read would cut
# the first real timing call off. The builder bounds each of its own
# subprocesses at five minutes, so this is a ceiling on a whole action,
# not a per-run figure.
TIMEOUT = httpx.Timeout(connect=10.0, read=1800.0, write=60.0, pool=10.0)


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
class BuilderAnswer:
    """The part of one builder response the gateway reads.

    Every field has a default, so an answer that omitted a key is read as
    the empty thing rather than as None turning up three modules later.
    Anything the builder writes and the gateway does not read is dropped
    here; adding a field is how a component starts reading one.
    """

    # What the answer is called in the message when it cannot be read,
    # and -- where the wire carries a `stage` -- what that stage must say.
    KIND: ClassVar[str] = "builder"
    STAGE: ClassVar[str | None] = None
    # Fields that must be whole numbers, tables, or arrays for the answer
    # to mean anything at all.
    COUNTS: ClassVar[tuple[str, ...]] = ()
    OPTIONAL_COUNTS: ClassVar[tuple[str, ...]] = ()
    TABLES: ClassVar[tuple[str, ...]] = ()
    LISTS: ClassVar[tuple[str, ...]] = ()

    ok: bool = False

    @classmethod
    def parse(cls, data):
        """One JSON body as this answer, or ComponentError if it is not one."""
        if not isinstance(data, dict):
            raise ComponentError(f"the builder's {cls.KIND} answer is not an object")
        stage = data.get("stage")
        if cls.STAGE is not None and stage is not None and stage != cls.STAGE:
            raise ComponentError(
                f"the builder answered stage {stage!r} where {cls.STAGE!r} was asked for"
            )
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    def __post_init__(self):
        if not isinstance(self.ok, bool):
            raise ComponentError(
                f"the builder's {self.KIND} answer gives no yes-or-no outcome: {self.ok!r}"
            )
        for name in self.COUNTS:
            whole_number(self.KIND, name, getattr(self, name))
        for name in self.OPTIONAL_COUNTS:
            whole_number(self.KIND, name, getattr(self, name), optional=True)
        for name in self.TABLES:
            if not isinstance(getattr(self, name), dict):
                raise ComponentError(f"the builder's {self.KIND} answer has no {name} table")
        for name in self.LISTS:
            if not isinstance(getattr(self, name), list):
                raise ComponentError(f"the builder's {self.KIND} answer has no list of {name}")

    def as_dict(self) -> dict:
        """The answer as a claim can carry it, which a dataclass cannot be."""
        return asdict(self)


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
class ArtifactsResponse(BuilderAnswer):
    KIND = "artifacts"
    TABLES = ("executables",)

    # relative path -> {sha256, size, role, verified}
    executables: dict = field(default_factory=dict)
    executor_identity: str | None = None


@dataclass(frozen=True)
class HealthResponse(BuilderAnswer):
    KIND = "health"
    TABLES = ("tools", "python_modules")

    # Keyed exactly as a strategy's `required_tools` spells them, so the
    # gateway can compare the two without translating.
    tools: dict = field(default_factory=dict)
    python_modules: dict = field(default_factory=dict)
    executor_identity: str | None = None


class BuilderClient:
    def __init__(self, http: httpx.Client):
        self._http = http

    def _get(self, path: str, answer):
        r = self._http.get(path)
        r.raise_for_status()
        return answer.parse(r.json())

    def _post(self, path: str, body: dict, answer):
        r = self._http.post(path, json=body)
        r.raise_for_status()
        return answer.parse(r.json())

    def healthz(self) -> HealthResponse:
        """What this builder can run, tool by tool."""
        return self._get("/healthz", HealthResponse)

    def artifacts(self, attempt_id: str) -> ArtifactsResponse:
        """Reverified executable identities retained by the builder supervisor."""
        return self._get(f"/v1/artifacts/{attempt_id}", ArtifactsResponse)

    def build(self, attempt_id: str, tree: list[dict], makefile: str, targets: list[dict],
              compiler: str, flags: list[str], link_flags: list[str],
              source_patterns: list[str]) -> BuildResponse:
        """Build one tree with its own makefile.

        `tree` is the whole tracked tree as [{"path", "b64"}]; `targets`
        is [{"role", "target", "executable"}] from the code's manifest.
        The compiler and the flags come from the strategy file, and
        `source_patterns` is what the code calls its own source, which is
        how the builder can say whether anything else was compiled.
        """
        return self._post("/v1/build", {
            "attempt_id": attempt_id, "tree": tree, "makefile": makefile,
            "targets": targets, "compiler": compiler, "flags": flags,
            "link_flags": link_flags, "source_patterns": source_patterns,
        }, BuildResponse)

    def run(self, attempt_id: str, executable: str, cases: dict,
            notify: str | None = None, mandatory: bool = False) -> RunResponse:
        """Replay every case through the manifest's replay executable.

        `cases` is {name: {variable: base64 of its .npy file}}, and the
        outputs come back in the same shape. The .npy file says what type
        and shape each array is, so nothing on the wire repeats it.
        `notify` is the strategy's device proof.
        """
        return self._post("/v1/run", {
            "attempt_id": attempt_id, "executable": executable, "cases": cases,
            "notify": notify, "mandatory": mandatory,
        }, RunResponse)

    def capture(self, attempt_id: str, executable: str, args: list[str],
                run_name: str) -> CaptureResponse:
        """Run the code's capture program once and bring back the dataset it wrote.

        `args` are the dataset's own, from the manifest; the directory the
        program writes into is the builder's to name, and `run_name` is
        what it calls it. The cases come back as
        {case: {"inputs": {variable: b64 npy}, "outputs": {...}}}.
        """
        return self._post("/v1/capture", {
            "attempt_id": attempt_id, "executable": executable, "args": args,
            "run_name": run_name,
        }, CaptureResponse)

    def sanitize(self, attempt_id: str, executable: str, cases: dict,
                 tools: list[str]) -> SanitizeResponse:
        """Run each sanitizer over each case. `cases` is shaped as for run()."""
        return self._post("/v1/sanitize", {
            "attempt_id": attempt_id, "executable": executable, "cases": cases, "tools": tools,
        }, SanitizeResponse)

    def properties(self, attempt_id: str, executable: str, module: str, cases: dict,
                   seed: int, max_examples: int) -> PropertiesResponse:
        """Run the code's own module of invariants against its replay binary.

        `module` is the path the manifest names, relative to the tree
        root; `cases` is shaped as for run() and becomes the corpus the
        properties draw from. The seed and the example count go out so
        that the claim can say what search was made.
        """
        return self._post("/v1/properties", {
            "attempt_id": attempt_id, "executable": executable, "module": module,
            "cases": cases, "seed": seed, "max_examples": max_examples,
        }, PropertiesResponse)

    def mutate(self, attempt_id: str, makefile: str, replay_target: dict, files: list[str],
               cases: dict, bands: dict, compiler: str, flags: list[str],
               link_flags: list[str], source_patterns: list[str],
               jobs: int | None = None, limit: int | None = None) -> MutateResponse:
        """Mutate the region's own files and score each mutant against the captures.

        `files` are the paths the manifest says implement the region;
        `cases` is a stored capture set, {name: {"inputs": {...},
        "outputs": {...}}}; `bands` is the code's tolerance policy per
        output variable. What comes back is one verdict per mutant, not
        the outputs any of them wrote.
        """
        return self._post("/v1/mutate", {
            "attempt_id": attempt_id, "makefile": makefile, "replay_target": replay_target,
            "files": files, "cases": cases, "bands": bands, "compiler": compiler,
            "flags": flags, "link_flags": link_flags, "source_patterns": source_patterns,
            "jobs": jobs, "limit": limit,
        }, MutateResponse)

    def time(self, attempt_id: str, executable: str, args: list[str], env: dict,
             outputs: list[str], repeats: int = 5, budget_s: int = 300,
             expected_outputs: dict[str, str] | None = None) -> TimeResponse:
        """Time the manifest's timing executable and collect the files it declares.

        The declared files come back as one set per run, in run order, so
        a caller can ask whether every run wrote the same thing.
        """
        return self._post("/v1/time", {
            "attempt_id": attempt_id, "executable": executable, "args": args, "env": env,
            "outputs": outputs, "repeats": repeats, "budget_s": budget_s,
            "expected_outputs": expected_outputs,
        }, TimeResponse)


class OracleClient:
    def __init__(self, http: httpx.Client):
        self._http = http

    def policy(self) -> dict:
        r = self._http.get("/v1/policy")
        r.raise_for_status()
        return r.json()

    def holdout_inputs(self) -> dict:
        """{"dataset": "holdout", "cases": {name: {variable: base64 npy}}} -- inputs only."""
        r = self._http.get("/v1/dataset/holdout/inputs")
        r.raise_for_status()
        return r.json()

    def compare(self, dataset: str, outputs: dict, attempt_id: str = "unknown") -> dict:
        """Judge one dataset's outputs, shaped {case: {variable: base64 npy}}."""
        r = self._http.post("/v1/compare", json={"attempt_id": attempt_id, "dataset": dataset, "outputs": outputs})
        r.raise_for_status()
        return r.json()


def connect_builder(base_url: str, token: str) -> BuilderClient:
    return BuilderClient(httpx.Client(
        base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT,
    ))


def connect_oracle(base_url: str, token: str) -> OracleClient:
    return OracleClient(httpx.Client(
        base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT,
    ))
