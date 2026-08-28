"""Thin clients for the builder and oracle services, matching their real
HTTP contracts (services/builder/app.py, services/oracle/app.py) exactly.

Trust role: none -- these carry bytes between the gateway and two
services that are themselves trusted for what they measure (builder) or
what they know (oracle). Nothing here decides pass or fail; the
equivalent/components/*.py modules that call these do that, from what
comes back.

Components take a client object rather than a URL so tests can pass a
fake with the same methods and no network, subprocess, or GPU involved --
unlike sese_check's check_sese.py, nvfortran/compute-sanitizer/a GPU
aren't available in this development environment at all.
"""
from __future__ import annotations

import httpx

# A build, a sanitizer pass, or five timed runs of the full program can
# each take minutes; httpx's default of five seconds per read would cut
# the first real timing call off. The builder bounds each of its own
# subprocesses at five minutes, so this is a ceiling on a whole action,
# not a per-run figure.
TIMEOUT = httpx.Timeout(connect=10.0, read=1800.0, write=60.0, pool=10.0)


class BuilderClient:
    def __init__(self, http: httpx.Client):
        self._http = http

    def healthz(self) -> dict:
        """{"ok": bool, "tools": {name: present}, ...} -- what this builder can run."""
        r = self._http.get("/healthz")
        r.raise_for_status()
        return r.json()

    def build(self, attempt_id: str, tree: list[dict], makefile: str, targets: list[dict],
              compiler: str, flags: list[str], link_flags: list[str],
              source_patterns: list[str]) -> dict:
        """Build one tree with its own makefile.

        `tree` is the whole tracked tree as [{"path", "b64"}]; `targets`
        is [{"role", "target", "executable"}] from the code's manifest.
        The compiler and the flags come from the strategy file, and
        `source_patterns` is what the code calls its own source, which is
        how the builder can say whether anything else was compiled.
        """
        r = self._http.post("/v1/build", json={
            "attempt_id": attempt_id, "tree": tree, "makefile": makefile,
            "targets": targets, "compiler": compiler, "flags": flags,
            "link_flags": link_flags, "source_patterns": source_patterns,
        })
        r.raise_for_status()
        return r.json()

    def run(self, attempt_id: str, executable: str, cases: dict,
            notify: str | None = None, mandatory: bool = False) -> dict:
        """Replay every case through the manifest's replay executable.

        `cases` is {name: {variable: base64 of its .npy file}}, and the
        outputs come back in the same shape. The .npy file says what type
        and shape each array is, so nothing on the wire repeats it.
        `notify` is the strategy's device proof.
        """
        r = self._http.post("/v1/run", json={
            "attempt_id": attempt_id, "executable": executable, "cases": cases,
            "notify": notify, "mandatory": mandatory,
        })
        r.raise_for_status()
        return r.json()

    def capture(self, attempt_id: str, executable: str, args: list[str],
                run_name: str) -> dict:
        """Run the code's capture program once and bring back the dataset it wrote.

        `args` are the dataset's own, from the manifest; the directory the
        program writes into is the builder's to name, and `run_name` is
        what it calls it. The cases come back as
        {case: {"inputs": {variable: b64 npy}, "outputs": {...}}}.
        """
        r = self._http.post("/v1/capture", json={
            "attempt_id": attempt_id, "executable": executable, "args": args,
            "run_name": run_name,
        })
        r.raise_for_status()
        return r.json()

    def sanitize(self, attempt_id: str, executable: str, cases: dict, tools: list[str]) -> dict:
        """Run each sanitizer over each case. `cases` is shaped as for run()."""
        r = self._http.post("/v1/sanitize", json={
            "attempt_id": attempt_id, "executable": executable, "cases": cases, "tools": tools,
        })
        r.raise_for_status()
        return r.json()

    def properties(self, attempt_id: str, executable: str, module: str, cases: dict,
                   seed: int, max_examples: int) -> dict:
        """Run the code's own module of invariants against its replay binary.

        `module` is the path the manifest names, relative to the tree
        root; `cases` is shaped as for run() and becomes the corpus the
        properties draw from. The seed and the example count go out so
        that the claim can say what search was made.
        """
        r = self._http.post("/v1/properties", json={
            "attempt_id": attempt_id, "executable": executable, "module": module,
            "cases": cases, "seed": seed, "max_examples": max_examples,
        })
        r.raise_for_status()
        return r.json()

    def time(self, attempt_id: str, executable: str, args: list[str], env: dict,
             outputs: list[str], repeats: int = 5, budget_s: int = 300) -> dict:
        """Time the manifest's timing executable and collect the files it declares.

        The declared files come back as one set per run, in run order, so
        a caller can ask whether every run wrote the same thing.
        """
        r = self._http.post("/v1/time", json={
            "attempt_id": attempt_id, "executable": executable, "args": args, "env": env,
            "outputs": outputs, "repeats": repeats, "budget_s": budget_s,
        })
        r.raise_for_status()
        return r.json()


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
