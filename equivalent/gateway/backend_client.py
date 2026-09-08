"""Thin clients for the builder and oracle services, matching their real
HTTP contracts (services/builder/app.py, services/oracle/app.py) exactly.

Trust role: none -- these carry bytes between the gateway and two
services that are themselves trusted for what they measure (builder) or
what they know (oracle). Nothing here decides pass or fail; the
equivalent/components/*.py modules that call these do that, from what
comes back.

What comes back is a typed answer rather than a bare dictionary, and the
answers live with the checks that read them
(equivalent/components/answers.py) rather than here: this module only
says what goes out on the wire and which answer each reply is.

Components take a client object rather than a URL so tests can pass a
fake with the same methods and no network, subprocess, or GPU involved --
unlike sese_check's check_sese.py, nvfortran/compute-sanitizer/a GPU
aren't available in this development environment at all.
"""
from __future__ import annotations

import httpx

from equivalent.components.answers import (
    ArtifactsResponse,
    BuildResponse,
    CaptureResponse,
    CompareResponse,
    HealthResponse,
    HoldoutInputsResponse,
    MutateResponse,
    PolicyResponse,
    PropertiesResponse,
    RunResponse,
    SanitizeResponse,
    TimeResponse,
)

# A build, a sanitizer pass, or five timed runs of the full program can
# each take minutes; httpx's default of five seconds per read would cut
# the first real timing call off. The builder bounds each of its own
# subprocesses at five minutes, so this is a ceiling on a whole action,
# not a per-run figure.
TIMEOUT = httpx.Timeout(connect=10.0, read=1800.0, write=60.0, pool=10.0)


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
              compiler: str | None, flags: list[str], link_flags: list[str],
              source_patterns: list[str], *, toolchains: dict | None = None,
              ) -> BuildResponse:
        """Build one tree with its own makefile.

        `tree` is the whole tracked tree as [{"path", "b64"}]; `targets`
        is [{"role", "target", "executable"}] from the code's manifest.
        The compiler and the flags come from the strategy file, and
        `source_patterns` is what the code calls its own source, which is
        how the builder can say whether anything else was compiled.
        """
        body = {
            "attempt_id": attempt_id, "tree": tree, "makefile": makefile,
            "targets": targets, "compiler": compiler, "flags": flags,
            "link_flags": link_flags, "source_patterns": source_patterns,
        }
        if toolchains is not None:
            body["toolchains"] = toolchains
        return self._post("/v1/build", body, BuildResponse)

    def run(self, attempt_id: str, executable: str, cases: dict,
            notify: str | None = None, mandatory: bool = False,
            profile: bool | None = None) -> RunResponse:
        """Replay every case through the manifest's replay executable.

        `cases` is {name: {variable: base64 of its .npy file}}, and the
        outputs come back in the same shape. The .npy file says what type
        and shape each array is, so nothing on the wire repeats it.
        `notify` is the strategy's device proof.
        """
        body = {
            "attempt_id": attempt_id, "executable": executable, "cases": cases,
            "notify": notify, "mandatory": mandatory,
        }
        if profile is not None:
            body["profile"] = profile
        return self._post("/v1/run", body, RunResponse)

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

    def _get(self, path: str, answer):
        r = self._http.get(path)
        r.raise_for_status()
        return answer.parse(r.json())

    def policy(self) -> PolicyResponse:
        """Which bands this oracle judges by, and which oracle it is."""
        return self._get("/v1/policy", PolicyResponse)

    def holdout_inputs(self) -> HoldoutInputsResponse:
        """The held-out cases as {variable: base64 npy} -- inputs only."""
        return self._get("/v1/dataset/holdout/inputs", HoldoutInputsResponse)

    def compare(self, dataset: str, outputs: dict,
                attempt_id: str = "unknown") -> CompareResponse:
        """Judge one dataset's outputs, shaped {case: {variable: base64 npy}}."""
        r = self._http.post("/v1/compare", json={
            "attempt_id": attempt_id, "dataset": dataset, "outputs": outputs,
        })
        r.raise_for_status()
        return CompareResponse.parse(r.json())


def connect_builder(base_url: str, token: str) -> BuilderClient:
    return BuilderClient(httpx.Client(
        base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT,
    ))


def connect_oracle(base_url: str, token: str) -> OracleClient:
    return OracleClient(httpx.Client(
        base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT,
    ))
