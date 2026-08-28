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

When there is a builder, startup asks it what it has -- executables, and
the modules a property run imports -- and refuses to serve if a strategy
needs something it lacks, so a deployment that could never pass its own
gates says so before the first request.
"""
from __future__ import annotations

import os

from fastapi import FastAPI

from equivalent.gateway.app import create_app
from equivalent.gateway.backend_client import connect_builder, connect_oracle
from equivalent.gateway.config import load_gateway_config
from equivalent.strategy.schema import load_strategy

CONFIG_VAR = "EQUIVALENT_CONFIG"
TOKEN_VAR = "EQUIVALENT_TOKEN"
BUILDER_URL_VAR = "EQUIVALENT_BUILDER_URL"
ORACLE_URL_VAR = "EQUIVALENT_ORACLE_URL"
BACKEND_TOKEN_VAR = "EQUIVALENT_BACKEND_TOKEN"
PORT_VAR = "EQUIVALENT_PORT"
DEFAULT_PORT = 8000

# How a strategy asks for something the builder imports rather than
# something it runs. `python:pytest` is answered from the builder's
# python_modules report; anything without the prefix is an executable and
# is answered from its tools report. Two ways of looking, because a module
# on the interpreter's path and a binary on PATH are not the same thing.
MODULE_PREFIX = "python:"


def _required(env, name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is not set (or is empty); the gateway will not start without it")
    return value


def check_required_tools(regions, builder) -> None:
    """Stop startup if a region's strategy needs something the builder lacks.

    The strategy names the tools its build and its gates need; the builder
    is the machine that would run them. Comparing the two here means a
    deployment whose builder image is missing a compiler says so on the
    first line of its log, instead of accepting requests and failing them
    one at a time in a way that reads like the agent's fault.

    An entry spelled `python:<module>` is not an executable but something
    the builder's own interpreter has to import -- pytest and Hypothesis,
    which the property check runs a code's invariants with. The builder
    reports those separately, and they are looked up separately.
    """
    try:
        report = builder.healthz()
    except Exception as exc:
        raise ValueError(
            f"the builder did not answer /healthz, so its tools could not be checked "
            f"against the strategies: {exc}"
        ) from exc
    present = report.get("tools", {})
    importable = report.get("python_modules", {})

    def has(tool: str) -> bool:
        if tool.startswith(MODULE_PREFIX):
            return bool(importable.get(tool[len(MODULE_PREFIX):]))
        return bool(present.get(tool))

    def available() -> list:
        return sorted(
            [name for name, have in present.items() if have]
            + [f"{MODULE_PREFIX}{name}" for name, have in importable.items() if have]
        )

    for region_id in sorted(regions):
        region = regions[region_id]
        # Both strategies a region names get run on this builder: the one
        # its port is compiled with, and the one its baseline is.
        for path in (region.strategy_path, region.baseline_strategy_path):
            strategy = load_strategy(path)
            missing = [tool for tool in strategy.required_tools if not has(tool)]
            if missing:
                raise ValueError(
                    f"strategy '{strategy.name}' (region '{region_id}') requires {missing}, "
                    f"which the builder does not have; it reports {available()}"
                )


def build_app_from_env(env=None, *, builder=None, oracle=None) -> FastAPI:
    """The configured gateway, or an error naming what the environment is missing.

    Starting a second time against the same directories is deliberately
    uneventful: the repository is seeded only when it does not exist yet,
    so the baseline commit -- and with it every region's ledger directory
    -- stays the same across restarts.

    `builder` and `oracle` are normally built from the URLs in the
    environment. A caller may hand in a client instead, which is how a
    test starts a gateway without a builder container to talk to.
    """
    env = os.environ if env is None else env
    config_path = _required(env, CONFIG_VAR)
    token = _required(env, TOKEN_VAR)

    config = load_gateway_config(config_path, seed_if_empty=True)
    print(f"gateway: baseline commit {config.baseline_commit} in {config.paths.repo}", flush=True)
    print(f"gateway: codes {sorted(config.codes)}", flush=True)
    print(f"gateway: regions {sorted(config.regions)}", flush=True)

    backend_token = env.get(BACKEND_TOKEN_VAR) or token
    builder_url = env.get(BUILDER_URL_VAR)
    oracle_url = env.get(ORACLE_URL_VAR)
    for name, url in ((BUILDER_URL_VAR, builder_url), (ORACLE_URL_VAR, oracle_url)):
        print(f"gateway: {name}={url or 'unset (its actions will report not configured)'}", flush=True)

    if builder is None and builder_url:
        builder = connect_builder(builder_url, backend_token)
    if oracle is None and oracle_url:
        oracle = connect_oracle(oracle_url, backend_token)

    # Only when there is a builder to ask. A gateway brought up without
    # one serves the analyzer side and answers "builder not configured"
    # for everything else, and has nothing to check the strategies against.
    if builder is not None:
        check_required_tools(config.regions, builder)

    return create_app(config.regions, token, builder=builder, oracle=oracle)


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
