from pathlib import Path

from fastapi.testclient import TestClient

from equivalent.client import GatewayClient
from equivalent.gateway.app import create_app
from equivalent.gateway.regions import RegionConfig
from equivalent.gateway.submit import init_baseline_repo
from equivalent.manifest.schema import load_manifest
from equivalent.tests.fakes import write_program

TOKEN = "test-token"
STRATEGY_PATH = Path(__file__).resolve().parent.parent / "strategy" / "files" / "stdpar_managed.yaml"
BASELINE_STRATEGY_PATH = STRATEGY_PATH.parent / "cpu_reference.yaml"


def _client_pair(tmp_path):
    repo_dir = tmp_path / "repo"
    (tmp_path / "seed" / "src").mkdir(parents=True)
    (tmp_path / "seed" / "src" / "mod_kernel.f90").write_text("subroutine step\nend subroutine\n")
    init_baseline_repo(repo_dir, tmp_path / "seed")
    working = tmp_path / "working"
    working.mkdir()
    cfg = RegionConfig(
        region_id="ch04:step", repo_dir=repo_dir,
        spec_path="notes/regions/ch04-step.sese.yaml", ledger_dir=tmp_path / "ledger",
        strategy_path=STRATEGY_PATH, baseline_strategy_path=BASELINE_STRATEGY_PATH,
        working_copy_dir=working,
        manifest=load_manifest(write_program(tmp_path) / "manifest.yaml"),
    )
    app = create_app({cfg.region_id: cfg}, TOKEN)
    fastapi_client = TestClient(app)

    # TestClient is itself a usable httpx.Client (a sync-friendly ASGI
    # transport underneath), so GatewayClient can drive it directly.
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Session-Id": "sess-1", "X-Model-Id": "claude-sonnet-5"}
    driven = TestClient(app, headers=headers)
    return GatewayClient(driven), fastapi_client, cfg


def test_client_table_matches_calling_the_endpoint_directly(tmp_path):
    client, fastapi_client, cfg = _client_pair(tmp_path)

    assert client.table() == fastapi_client.get(
        "/table", headers={"Authorization": f"Bearer {TOKEN}"},
    ).json()


def test_client_status_matches_calling_the_endpoint_directly(tmp_path):
    client, fastapi_client, cfg = _client_pair(tmp_path)

    assert client.status(cfg.region_id) == fastapi_client.get(
        "/status", params={"region": cfg.region_id}, headers={"Authorization": f"Bearer {TOKEN}"},
    ).json()


def test_client_submit_matches_calling_the_endpoint_directly(tmp_path):
    client, fastapi_client, cfg = _client_pair(tmp_path)
    (cfg.working_copy_dir / "notes" / "regions").mkdir(parents=True)
    (cfg.working_copy_dir / "notes" / "regions" / "ch04-step.sese.yaml").write_text("region: ch04:step\n")

    result = client.submit(cfg.region_id)

    assert result["committed"] is True
    assert len(result["tree"]) == 64


def test_client_run_matches_calling_the_endpoint_directly(tmp_path):
    client, fastapi_client, cfg = _client_pair(tmp_path)

    result = client.run("build_replay", cfg.region_id, {})

    assert result["refused"] is True


def test_client_passes_the_caller_tool_call_id_through_to_the_request_log(tmp_path):
    from equivalent.ledger.store import LedgerStore

    client, fastapi_client, cfg = _client_pair(tmp_path)
    store = LedgerStore(cfg.ledger_dir)

    client.run("build_replay", cfg.region_id, {}, tool_call_id="tool:1:aaa")
    client.run("build_replay", cfg.region_id, {})

    assert [line.tool_call_id for line in store.all_requests()] == ["tool:1:aaa", None]
