"""Building the running gateway out of its environment.

Trust role: this is where the deployment's own choices -- which
configuration file, which bearer token, which builder and oracle -- turn
into the reference monitor. A token read from the wrong place, or a
builder URL pointing somewhere else, would let claims be filed from
outside the deployment. Nothing else in the package reads the
environment: `create_app` is handed everything it needs, so a test builds
a gateway without setting a single variable.

The variables:

===========================  ================================================
`EQUIVALENT_CONFIG`          path to the configuration file (required)
`EQUIVALENT_TOKEN`           bearer token the agent and CLI present (required)
`EQUIVALENT_BUILDER_URL`     builder service, e.g. http://builder:9090
`EQUIVALENT_ORACLE_URL`      oracle service, e.g. http://oracle:7070
`EQUIVALENT_BACKEND_TOKEN`   token for those two (defaults to EQUIVALENT_TOKEN)
`EQUIVALENT_PORT`            port `serve()` listens on (default 8000)
===========================  ================================================

Leaving the builder or oracle URL out is allowed and useful: the gateway
still serves the table, status, submits, and the analyzer's own check,
and the actions that need a service it hasn't been given answer with the
same "not configured" response they already give.
"""
from __future__ import annotations

import os

from fastapi import FastAPI

from equivalent.gateway.app import create_app
from equivalent.gateway.backend_client import connect_builder, connect_oracle
from equivalent.gateway.config import load_gateway_config

CONFIG_VAR = "EQUIVALENT_CONFIG"
TOKEN_VAR = "EQUIVALENT_TOKEN"
BUILDER_URL_VAR = "EQUIVALENT_BUILDER_URL"
ORACLE_URL_VAR = "EQUIVALENT_ORACLE_URL"
BACKEND_TOKEN_VAR = "EQUIVALENT_BACKEND_TOKEN"
PORT_VAR = "EQUIVALENT_PORT"
DEFAULT_PORT = 8000


def _required(env, name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is not set (or is empty); the gateway will not start without it")
    return value


def build_app_from_env(env=None) -> FastAPI:
    """The configured gateway, or an error naming what the environment is missing.

    Starting a second time against the same directories is deliberately
    uneventful: the repository is seeded only when it does not exist yet,
    so the baseline commit -- and with it every region's ledger directory
    -- stays the same across restarts.
    """
    env = os.environ if env is None else env
    config_path = _required(env, CONFIG_VAR)
    token = _required(env, TOKEN_VAR)

    config = load_gateway_config(config_path, seed_if_empty=True)
    print(f"gateway: baseline commit {config.baseline_commit} in {config.paths.repo}", flush=True)
    print(f"gateway: regions {sorted(config.regions)}", flush=True)

    backend_token = env.get(BACKEND_TOKEN_VAR) or token
    builder_url = env.get(BUILDER_URL_VAR)
    oracle_url = env.get(ORACLE_URL_VAR)
    for name, url in ((BUILDER_URL_VAR, builder_url), (ORACLE_URL_VAR, oracle_url)):
        print(f"gateway: {name}={url or 'unset (its actions will report not configured)'}", flush=True)

    return create_app(
        config.regions,
        token,
        builder=connect_builder(builder_url, backend_token) if builder_url else None,
        oracle=connect_oracle(oracle_url, backend_token) if oracle_url else None,
    )


def app() -> FastAPI:
    """The application factory uvicorn is pointed at.

    It is a function, not a module-level FastAPI instance, so that
    importing this module -- which the tests do -- reads no environment
    and builds nothing. Run it as::

        uvicorn equivalent.gateway.main:app --factory
    """
    return build_app_from_env()


def serve() -> None:
    """The `equivalent-gateway` command: uvicorn on every interface."""
    import uvicorn

    uvicorn.run(
        "equivalent.gateway.main:app",
        factory=True,
        host="0.0.0.0",
        port=int(os.environ.get(PORT_VAR, DEFAULT_PORT)),
    )


if __name__ == "__main__":
    serve()
