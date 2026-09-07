"""The two spellings of the builder's wire, and what keeps them the same.

The builder service refuses to install the `equivalent` package and the
gateway image installs nothing of the services, so neither side can
import the other's contract: services/builder/contract.py says what the
builder accepts and answers, equivalent/gateway/backend_client.py says
what the gateway sends, and equivalent/components/answers.py says what a
check reads back. Only a test can hold them up against each other, and
this is it -- the same job test_layout_parity.py does for the code
directory's layout.

Four things have to hold, and each fails differently in production. A
request body the builder's model rejects is a 422 that reads to a session
as its own mistake. A response field a check reads and the builder never
writes is a default read as a measurement: zero kernels, no compiles,
nothing skipped. A fake whose methods have drifted from the client's
leaves every component test passing against a builder that no longer
exists. And a check is typed against a protocol rather than the client,
so a protocol that has drifted from the client would say a check may ask
for something no builder answers.
"""
from __future__ import annotations

import inspect
import json
from dataclasses import MISSING, fields as dataclass_fields

import httpx
import pytest

from equivalent.components import answers
from equivalent.gateway import backend_client as gateway
from equivalent.tests.fakes import BUILDER_ENDPOINTS, FakeBuilder
from services.builder import contract

# One call per endpoint, with an argument in every position, so the body
# the client builds is the whole body and not the half a default fills in.
CALLS = {
    "build": lambda c: c.build(
        "attempt-1", [{"path": "src/mod.f90", "b64": ""}], "Makefile",
        [{"role": "replay", "target": "replay", "executable": "replay"}],
        "nvfortran", ["-O2"], ["-lm"], ["**/*.f90"],
    ),
    "run": lambda c: c.run(
        "attempt-1", "replay", {"case0000": {"field": ""}},
        notify=None, mandatory=True, profile=True,
    ),
    "capture": lambda c: c.capture("attempt-1", "gen_reference", ["100", "5000"], "visible"),
    "sanitize": lambda c: c.sanitize(
        "attempt-1", "replay", {"case0000": {"field": ""}}, ["memcheck", "racecheck"],
    ),
    "properties": lambda c: c.properties(
        "attempt-1", "replay", "harness/properties.py", {"case0000": {"field": ""}}, 7, 25,
    ),
    "mutate": lambda c: c.mutate(
        "attempt-1", "Makefile", {"target": "replay", "executable": "replay"},
        ["src/mod.f90"], {"case0000": {"inputs": {}, "outputs": {}}}, {"field": {"abs": 0.0}},
        "nvfortran", ["-O2"], ["-lm"], ["**/*.f90"], jobs=2, limit=5,
    ),
    "time": lambda c: c.time(
        "attempt-1", "whole_program", ["512"], {"OMP_NUM_THREADS": "8"}, ["field.npy"],
        repeats=3, budget_s=120, expected_outputs={"field.npy": ""},
    ),
}

# Which builder response each gateway response is the reading half of.
# The two sides of a pair are deliberately spelled with the same name.
PAIRS = [
    (answers.BuildResponse, contract.BuildResponse),
    (answers.RunResponse, contract.RunResponse),
    (answers.CaptureResponse, contract.CaptureResponse),
    (answers.SanitizeResponse, contract.SanitizeResponse),
    (answers.PropertiesResponse, contract.PropertiesResponse),
    (answers.MutateResponse, contract.MutateResponse),
    (answers.TimeResponse, contract.TimeResponse),
    (answers.ArtifactsResponse, contract.ArtifactsResponse),
    (answers.HealthResponse, contract.HealthResponse),
]
PAIR_IDS = [read.__name__ for read, _ in PAIRS]


def _sent(call) -> tuple[str, dict]:
    """The path and the JSON body one client call puts on the wire."""
    recorded = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    client = gateway.BuilderClient(httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://builder",
    ))
    call(client)
    return recorded[0]


@pytest.mark.parametrize("endpoint", sorted(CALLS))
def test_every_request_the_client_sends_is_one_the_builder_accepts(endpoint):
    request_model, _ = contract.ENDPOINTS[endpoint]

    path, body = _sent(CALLS[endpoint])

    assert path == f"/v1/{endpoint}"
    request_model.model_validate(body)


@pytest.mark.parametrize("endpoint", sorted(CALLS))
def test_the_client_sends_no_key_the_builder_would_ignore(endpoint):
    # A request model quietly drops what it does not declare, so a key the
    # client kept sending after the builder stopped reading it would look
    # like a setting that was honored.
    request_model, _ = contract.ENDPOINTS[endpoint]

    _, body = _sent(CALLS[endpoint])

    assert set(body) <= set(request_model.model_fields)


def test_build_client_preserves_runtime_artifact_declarations():
    target = {
        "role": "replay", "target": "replay", "executable": "replay",
        "runtime_artifacts": [{"path": "modules/kernel.ptx", "kind": "gpu_module"}],
    }

    _, body = _sent(lambda client: client.build(
        "attempt", [], "Makefile", [target], "nvfortran", [], [], ["src/*.f90"],
    ))

    assert body["targets"] == [target]
    contract.BuildRequest.model_validate(body)


@pytest.mark.parametrize("protocol,client", [
    (answers.Builder, gateway.BuilderClient),
    (answers.Oracle, gateway.OracleClient),
], ids=["Builder", "Oracle"])
def test_what_a_check_may_ask_a_backend_is_what_the_client_offers(protocol, client):
    # A check is handed something shaped like the protocol, and what it
    # really gets is this client or a fake of it. A protocol method the
    # client does not have is a call no backend would answer.
    for name, declared in inspect.getmembers(protocol, inspect.isfunction):
        if name.startswith("_"):
            continue
        offered = getattr(client, name, None)
        assert offered is not None, f"{client.__name__} has no {name}"
        assert (list(inspect.signature(declared).parameters)
                == list(inspect.signature(offered).parameters)), name


@pytest.mark.parametrize("read,written", PAIRS, ids=PAIR_IDS)
def test_every_field_the_gateway_reads_is_one_the_builder_writes(read, written):
    for field in dataclass_fields(read):
        assert field.name in written.model_fields, (
            f"{read.__name__} reads '{field.name}', which the builder's "
            f"{written.__name__} does not write"
        )


@pytest.mark.parametrize("read,written", PAIRS, ids=PAIR_IDS)
def test_a_field_the_builder_left_out_means_the_same_on_both_sides(read, written):
    # What a missing key means is the whole point of these defaults: a
    # gateway that read an absent kernel count as None and a builder that
    # wrote 0 would disagree about whether anything ran.
    for field in dataclass_fields(read):
        mine = field.default_factory() if field.default_factory is not MISSING else field.default
        theirs = written.model_fields[field.name].get_default(call_default_factory=True)
        assert mine == theirs, (
            f"{read.__name__}.{field.name} defaults to {mine!r} and the builder's "
            f"{written.__name__}.{field.name} to {theirs!r}"
        )


@pytest.mark.parametrize("read,written", PAIRS, ids=PAIR_IDS)
def test_the_stage_the_gateway_insists_on_is_the_one_the_builder_names(read, written):
    stage = written.model_fields.get("stage")
    assert read.STAGE == (None if stage is None else stage.get_default())


def test_the_fake_builders_configurable_answers_are_the_calls_it_makes():
    client_calls = {
        name for name, _ in inspect.getmembers(gateway.BuilderClient, inspect.isfunction)
        if not name.startswith("_")
    }

    assert set(BUILDER_ENDPOINTS) == client_calls


def test_the_fake_builder_answers_exactly_the_calls_the_client_makes():
    # A component test is only worth anything if the fake it runs against
    # takes what the real client takes; a renamed argument that only the
    # client learned about would leave every one of them passing.
    client_methods = {
        name: inspect.signature(method)
        for name, method in inspect.getmembers(gateway.BuilderClient, inspect.isfunction)
        if not name.startswith("_")
    }
    fake_methods = {
        name: inspect.signature(method)
        for name, method in inspect.getmembers(FakeBuilder, inspect.isfunction)
        if not name.startswith("_")
    }

    assert set(fake_methods) == set(client_methods)
    for name, signature in client_methods.items():
        assert list(fake_methods[name].parameters) == list(signature.parameters), name
