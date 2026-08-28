"""Thin clients for the builder and oracle services, matching their real
HTTP contracts (demo/builder/app.py, demo/oracle/app.py) exactly.

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

    def build(self, attempt_id: str, files: list[dict], profile: str,
              flags: list[str] | None = None, link_flags: list[str] | None = None) -> dict:
        r = self._http.post("/v1/build", json={
            "attempt_id": attempt_id, "source": {"files": files}, "profile": profile,
            "flags": flags, "link_flags": link_flags,
        })
        r.raise_for_status()
        return r.json()

    def run(self, attempt_id: str, profile: str, cases: dict, mandatory: bool = False) -> dict:
        r = self._http.post("/v1/run", json={"attempt_id": attempt_id, "profile": profile, "cases": cases, "mandatory": mandatory})
        r.raise_for_status()
        return r.json()

    def sanitize(self, attempt_id: str, profile: str, cases: dict, tools: list[str]) -> dict:
        r = self._http.post("/v1/sanitize", json={"attempt_id": attempt_id, "profile": profile, "cases": cases, "tools": tools})
        r.raise_for_status()
        return r.json()

    def time(self, attempt_id: str, repeats: int = 5) -> dict:
        r = self._http.post("/v1/time", json={"attempt_id": attempt_id, "repeats": repeats})
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
        r = self._http.get("/v1/dataset/holdout/inputs")
        r.raise_for_status()
        return r.json()

    def compare(self, dataset: str, outputs: dict, attempt_id: str = "unknown") -> dict:
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
