"""Starting the gateway the way a deployment starts it: from the environment."""
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from equivalent.gateway.main import build_app_from_env
from equivalent.gateway.submit import baseline_commit
from equivalent.tests.fakes import FakeBuilder, write_program

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Session-Id": "sess-1", "X-Model-Id": "claude-sonnet-5"}
STRATEGIES_DIR = Path(__file__).resolve().parents[2] / "strategy" / "files"
SPEC_PATH = "notes/regions/ch04-step.sese.yaml"

SOURCE = """\
module mod_kernel
contains
subroutine step(a, b)
  real :: a, b
  a = a + b
end subroutine step
end module mod_kernel
"""

SPEC = (
    "region: ch04:step\n"
    "anchor:\n"
    "  file: src/mod_kernel.f90\n"
    '  pst_node: "step@3-5"\n'
    "  entry_symbol: step\n"
)


def _strategies_requiring(tmp_path, extra_tool):
    """A strategies directory whose one strategy also requires `extra_tool`."""
    d = yaml.safe_load((STRATEGIES_DIR / "stdpar_managed.yaml").read_text())
    d["required_tools"].append(extra_tool)
    directory = tmp_path / "strategies"
    directory.mkdir()
    (directory / "stdpar_managed.yaml").write_text(yaml.safe_dump(d))
    return directory


def _deployment(tmp_path, monkeypatch, token=TOKEN, strategies=STRATEGIES_DIR, **extra_env):
    """Directories and a configuration file, named by the environment, with nothing seeded yet."""
    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "mod_kernel.f90").write_text(SOURCE)
    (seed / "notes" / "regions").mkdir(parents=True)
    (seed / SPEC_PATH).write_text(SPEC)

    working = tmp_path / "working"
    (working / "notes" / "regions").mkdir(parents=True)
    (working / SPEC_PATH).write_text(SPEC)

    programs = write_program(tmp_path).parent
    config_path = tmp_path / "gateway.yaml"
    config_path.write_text(yaml.safe_dump({
        "version": 1,
        "paths": {
            "repo": str(tmp_path / "repo"),
            "ledger_root": str(tmp_path / "ledger"),
            "working_copy": str(working),
            "programs": str(programs),
            "strategies": str(strategies),
            "seed": str(seed),
        },
        "codes": {"tsunami": {"manifest": "tsunami/manifest.yaml"}},
        "regions": {"ch04:step": {
            "code": "tsunami", "spec_path": SPEC_PATH, "strategy": "stdpar_managed",
        }},
    }))

    monkeypatch.setenv("EQUIVALENT_CONFIG", str(config_path))
    monkeypatch.setenv("EQUIVALENT_TOKEN", token)
    for name, value in extra_env.items():
        monkeypatch.setenv(name, value)
    for name in ("EQUIVALENT_BUILDER_URL", "EQUIVALENT_ORACLE_URL", "EQUIVALENT_BACKEND_TOKEN"):
        if name not in extra_env:
            monkeypatch.delenv(name, raising=False)
    return tmp_path / "repo"


def _commit_count(repo_dir) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "main"], cwd=repo_dir, capture_output=True, text=True, check=True,
    )
    return int(out.stdout.strip())


def test_the_first_start_seeds_the_repository_and_a_second_start_leaves_it_alone(tmp_path, monkeypatch):
    repo = _deployment(tmp_path, monkeypatch)

    build_app_from_env()
    first = baseline_commit(repo)

    build_app_from_env()

    assert baseline_commit(repo) == first
    assert _commit_count(repo) == 1


def test_the_gateway_it_builds_serves_the_table_and_still_wants_the_token(tmp_path, monkeypatch):
    _deployment(tmp_path, monkeypatch)
    client = TestClient(build_app_from_env())

    assert client.get("/table", headers=HEADERS).status_code == 200
    assert client.get("/table").status_code == 401


def test_with_no_builder_or_oracle_configured_the_analyzer_side_still_works(tmp_path, monkeypatch):
    # A deployment can bring the gateway up before the builder and oracle
    # are reachable: everything that needs only the repository and the
    # ledger works, and an action that needs a service says so instead of
    # failing the request.
    _deployment(tmp_path, monkeypatch)
    client = TestClient(build_app_from_env())

    assert client.get("/status", params={"region": "ch04:step"}, headers=HEADERS).status_code == 200

    receipt = client.post("/submit", json={"region": "ch04:step"}, headers=HEADERS)
    assert receipt.status_code == 200

    sese = client.post(
        "/run", json={"action": "sese_check", "region": "ch04:step", "config": {}}, headers=HEADERS,
    ).json()
    assert sese["verdict"] == "pass"

    build = client.post(
        "/run", json={"action": "build_replay", "region": "ch04:step", "config": {}}, headers=HEADERS,
    )
    assert build.status_code == 200
    assert build.json() == {"error": "builder not configured"}


def test_the_ledger_lands_under_the_baseline_commit_and_the_region_id(tmp_path, monkeypatch):
    repo = _deployment(tmp_path, monkeypatch)
    client = TestClient(build_app_from_env())

    client.post("/submit", json={"region": "ch04:step"}, headers=HEADERS)

    region_dir = tmp_path / "ledger" / baseline_commit(repo) / "ch04-step"
    assert (region_dir / "requests.jsonl").is_file()


@pytest.mark.parametrize("token", ["", "   "])
def test_a_missing_or_empty_token_stops_startup_and_names_the_variable(tmp_path, monkeypatch, token):
    _deployment(tmp_path, monkeypatch, token=token)

    with pytest.raises(ValueError) as excinfo:
        build_app_from_env()

    assert "EQUIVALENT_TOKEN" in str(excinfo.value)


def test_a_missing_config_variable_names_the_variable(tmp_path, monkeypatch):
    _deployment(tmp_path, monkeypatch)
    monkeypatch.delenv("EQUIVALENT_CONFIG")

    with pytest.raises(ValueError) as excinfo:
        build_app_from_env()

    assert "EQUIVALENT_CONFIG" in str(excinfo.value)


def test_startup_accepts_a_builder_that_has_every_required_tool(tmp_path, monkeypatch):
    _deployment(tmp_path, monkeypatch)

    app = build_app_from_env(builder=FakeBuilder())

    assert TestClient(app).get("/table", headers=HEADERS).status_code == 200


def test_a_required_tool_the_builder_lacks_stops_startup_and_names_it(tmp_path, monkeypatch):
    # The strategy says which tools the build needs. The builder is the
    # thing that would run them, so it is asked before the first request
    # rather than after an action fails in a way nobody can read.
    _deployment(tmp_path, monkeypatch, strategies=_strategies_requiring(tmp_path, "flang"))
    builder = FakeBuilder()

    with pytest.raises(ValueError) as excinfo:
        build_app_from_env(builder=builder)

    message = str(excinfo.value)
    assert "flang" in message
    assert "stdpar_managed" in message


def test_a_builder_that_does_not_answer_stops_startup(tmp_path, monkeypatch):
    # Nothing on this port, so the client's connection is refused at once.
    _deployment(tmp_path, monkeypatch, EQUIVALENT_BUILDER_URL="http://127.0.0.1:1")

    with pytest.raises(ValueError) as excinfo:
        build_app_from_env()

    assert "/healthz" in str(excinfo.value)
