"""Errors the gateway's domain services can report to an HTTP adapter."""
from __future__ import annotations

class GatewayError(Exception):
    """A request failure whose HTTP representation is part of the API contract.

    Gateway services do not import a web framework.  The FastAPI adapter turns
    this into ``HTTPException`` at the edge, while callers outside HTTP can use
    the same status and message directly.
    """

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
