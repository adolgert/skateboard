"""A thin client for the gateway's four endpoints.

Trust role: none. Whatever this module gets wrong shows up as a normal
request failure, not as a false claim -- the gateway itself still decides
everything. This exists so the CLI and, later, the pi extension call the
same four things the same way.
"""
from __future__ import annotations

import httpx


class GatewayClient:
    def __init__(self, http: httpx.Client):
        self._http = http

    def table(self) -> list:
        r = self._http.get("/table")
        r.raise_for_status()
        return r.json()

    def status(self, region: str) -> dict:
        r = self._http.get("/status", params={"region": region})
        r.raise_for_status()
        return r.json()

    def submit(self, region: str, working_copy_dir: str) -> dict:
        r = self._http.post("/submit", json={"region": region, "working_copy_dir": working_copy_dir})
        r.raise_for_status()
        return r.json()

    def run(self, action: str, region: str, config: dict | None = None) -> dict:
        r = self._http.post("/run", json={"action": action, "region": region, "config": config or {}})
        r.raise_for_status()
        return r.json()


def connect(base_url: str, token: str, session_id: str, model_id: str) -> GatewayClient:
    headers = {"Authorization": f"Bearer {token}", "X-Session-Id": session_id, "X-Model-Id": model_id}
    return GatewayClient(httpx.Client(base_url=base_url, headers=headers))
