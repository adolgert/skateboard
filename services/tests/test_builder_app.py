"""What the HTTP shim hands to a stage, and what it promises to answer with.

The service invents nothing: every endpoint takes a request body the
gateway read from a hashed manifest and a hashed strategy file, and
passes those fields to the stage unchanged. A shim that dropped a field,
reordered two of them, or answered with a shape no reader expects would
make a claim describe a build or a run that was not the one asked for.
"""
import inspect

import pytest
from fastapi.testclient import TestClient

from services.builder import app as service
from services.builder import contract

TREE = [{"path": "Makefile", "b64": ""}]
TARGET = {"role": "replay", "target": "replay", "executable": "replay"}
CASES = {"case0000": {"h": "AAA="}}

# One request per endpoint, with every field the gateway sends, and what
# the stage behind it must then be called with.
CALLS = [
    (
        "build", contract.BuildResponse,
        {
            "attempt_id": "attempt-1", "tree": TREE, "makefile": "Makefile",
            "targets": [TARGET], "compiler": "nvfortran", "flags": ["-O2"],
            "link_flags": ["-lm"], "source_patterns": ["src/*.f90"],
        },
        {
            "tree": TREE, "makefile": "Makefile", "targets": [TARGET],
            "compiler": "nvfortran", "flags": ["-O2"], "link_flags": ["-lm"],
            "source_patterns": ["src/*.f90"],
        },
    ),
    (
        "run", contract.RunResponse,
        {
            "attempt_id": "attempt-1", "executable": "replay", "cases": CASES,
            "notify": "acc", "mandatory": True,
        },
        {"executable": "replay", "cases": CASES, "notify": "acc", "mandatory": True},
    ),
    (
        "capture", contract.CaptureResponse,
        {
            "attempt_id": "attempt-1", "executable": "gen_reference",
            "args": ["8"], "run_name": "visible",
        },
        {"executable": "gen_reference", "args": ["8"], "run_name": "visible"},
    ),
    (
        "sanitize", contract.SanitizeResponse,
        {
            "attempt_id": "attempt-1", "executable": "replay", "cases": CASES,
            "tools": ["memcheck"],
        },
        {"executable": "replay", "cases": CASES, "tools": ["memcheck"]},
    ),
    (
        "properties", contract.PropertiesResponse,
        {
            "attempt_id": "attempt-1", "executable": "replay",
            "module": "harness/properties.py", "cases": CASES,
            "seed": 7, "max_examples": 25,
        },
        {
            "executable": "replay", "module": "harness/properties.py",
            "cases": CASES, "seed": 7, "max_examples": 25,
        },
    ),
    (
        "mutate", contract.MutateResponse,
        {
            "attempt_id": "attempt-1", "makefile": "Makefile",
            "replay_target": {"target": "replay", "executable": "replay"},
            "files": ["src/mod_kernel.f90"],
            "cases": {"case0000": {"inputs": {"h": "AAA="}, "outputs": {"h": "AAA="}}},
            "bands": {"h": {"abs": 1e-6, "rel": 1e-6, "ulp": 4}},
            "compiler": "nvfortran", "flags": ["-O2"], "link_flags": [],
            "source_patterns": ["src/*.f90"], "jobs": 2, "limit": 10,
        },
        {
            "makefile": "Makefile",
            "replay_target": {"target": "replay", "executable": "replay"},
            "files": ["src/mod_kernel.f90"],
            "cases": {"case0000": {"inputs": {"h": "AAA="}, "outputs": {"h": "AAA="}}},
            "bands": {"h": {"abs": 1e-6, "rel": 1e-6, "ulp": 4}},
            "compiler": "nvfortran", "flags": ["-O2"], "link_flags": [],
            "source_patterns": ["src/*.f90"], "jobs": 2, "limit": 10,
        },
    ),
    (
        "time", contract.TimeResponse,
        {
            "attempt_id": "attempt-1", "executable": "whole_program",
            "args": ["20000"], "env": {"OMP_NUM_THREADS": "8"},
            "outputs": ["h.npy"], "repeats": 3, "budget_s": 120,
            "expected_outputs": {"h.npy": "AAA="},
        },
        {
            "executable": "whole_program", "args": ["20000"],
            "env": {"OMP_NUM_THREADS": "8"}, "outputs": ["h.npy"],
            "repeats": 3, "budget_s": 120, "expected_outputs": {"h.npy": "AAA="},
        },
    ),
]


@pytest.fixture
def client():
    return TestClient(service.app)


@pytest.mark.parametrize(
    "endpoint,response,body,expected", CALLS, ids=[call[0] for call in CALLS],
)
def test_each_endpoint_hands_its_stage_the_fields_it_was_sent(
    endpoint, response, body, expected, client, monkeypatch,
):
    recorded = {}

    def record(workspace, **fields):
        recorded["attempt_id"] = workspace.attempt_id
        recorded.update(fields)
        return response(ok=True)

    monkeypatch.setattr(service.stages, "time_run" if endpoint == "time" else endpoint, record)

    answer = client.post(f"/v1/{endpoint}", json=body)

    assert answer.status_code == 200
    assert recorded == {"attempt_id": "attempt-1", **expected}


@pytest.mark.parametrize(
    "endpoint,response", [(call[0], call[1]) for call in CALLS], ids=[call[0] for call in CALLS],
)
def test_each_endpoint_answers_in_the_shape_its_response_type_declares(
    endpoint, response, client, monkeypatch,
):
    # A stage that answered with a bare dictionary would otherwise reach a
    # reader unvalidated, and a key it forgot would look like a measurement
    # nobody made.
    monkeypatch.setattr(
        service.stages, "time_run" if endpoint == "time" else endpoint,
        lambda workspace, **fields: {"ok": True},
    )

    answer = client.post(f"/v1/{endpoint}", json=_body_of(endpoint))

    assert set(answer.json()) == set(response(ok=True).to_dict())


def _body_of(endpoint: str) -> dict:
    return next(body for name, _, body, _ in CALLS if name == endpoint)


def test_every_posted_endpoint_is_the_pair_the_contract_names():
    # The gateway names an endpoint by its path and reads the pair from
    # here, so a route the contract does not describe is one nothing
    # downstream knows how to read.
    posted = {}
    for route in service.app.routes:
        if "POST" not in getattr(route, "methods", ()):
            continue
        request_model = inspect.signature(route.endpoint).parameters["req"].annotation
        posted[route.path.rsplit("/", 1)[-1]] = (request_model, route.response_model)

    assert posted == contract.ENDPOINTS
