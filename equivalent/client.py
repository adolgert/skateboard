"""A thin client for the gateway's four endpoints.

Trust role: none. Whatever this module gets wrong shows up as a normal
request failure, not as a false claim -- the gateway itself still decides
everything. This exists so the CLI and, later, the pi extension call the
same four things the same way.
"""
from __future__ import annotations

import httpx

# One /run call spans the whole check behind it -- a compile, a sanitizer
# pass, or repeated timed runs -- so a caller waits as long as the gateway
# itself is willing to wait on its builder (equivalent.gateway.backend_client).
TIMEOUT = httpx.Timeout(connect=10.0, read=1800.0, write=60.0, pool=10.0)

# The caller's own id for the tool call behind a request. Optional: it is
# what lines a session transcript up with the request log call by call.
TOOL_CALL_HEADER = "X-Tool-Call-Id"


def _call_header(tool_call_id: str | None) -> dict:
    """The tool-call header, or nothing at all when there is no id to send."""
    return {TOOL_CALL_HEADER: tool_call_id} if tool_call_id is not None else {}


class GatewayClient:
    def __init__(self, http: httpx.Client):
        self._http = http

    def table(self, region: str) -> list:
        """The actions this region may run -- which are the actions of its phase."""
        r = self._http.get("/table", params={"region": region})
        r.raise_for_status()
        return r.json()

    def status(self, region: str) -> dict:
        r = self._http.get("/status", params={"region": region})
        r.raise_for_status()
        return r.json()

    def submit(self, region: str, tool_call_id: str | None = None) -> dict:
        """The gateway reads the region's own configured working copy; a
        submit names only which region is being submitted.

        `tool_call_id` is for a caller that is a model's tool call and
        wants its request log line to name that call. A caller driving
        the gateway directly leaves it out.
        """
        r = self._http.post("/submit", json={"region": region}, headers=_call_header(tool_call_id))
        r.raise_for_status()
        return r.json()

    def run(self, action: str, region: str, config: dict | None = None, tool_call_id: str | None = None) -> dict:
        r = self._http.post(
            "/run",
            json={"action": action, "region": region, "config": config or {}},
            headers=_call_header(tool_call_id),
        )
        r.raise_for_status()
        return r.json()


def connect(base_url: str, token: str, session_id: str, model_id: str) -> GatewayClient:
    headers = {"Authorization": f"Bearer {token}", "X-Session-Id": session_id, "X-Model-Id": model_id}
    return GatewayClient(httpx.Client(base_url=base_url, headers=headers, timeout=TIMEOUT))
