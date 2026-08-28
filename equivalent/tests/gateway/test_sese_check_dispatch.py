"""POST /run dispatching to the real sese_check component.

test_run.py already covers refusal/duplicate logic against
synthetic claims recorded directly on the store; these tests exercise the
real analyzer dispatch end to end, through the HTTP layer.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from equivalent.gateway.app import create_app
from equivalent.gateway.regions import RegionConfig
from equivalent.gateway.submit import init_baseline_repo
from equivalent.ledger.store import LedgerStore

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Session-Id": "sess-1", "X-Model-Id": "claude-sonnet-5"}
STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategy" / "files" / "stdpar_managed.yaml"
SPEC_PATH = "notes/regions/ch04-step.sese.yaml"

CLEAN_SOURCE = """\
module mod_kernel
contains
subroutine step(h, u)
  real :: h, u
  h = h + u
end subroutine step
end module mod_kernel
"""

GOTO_SOURCE = """\
module mod_kernel
contains
subroutine step(h, u)
  real :: h, u
  if (h > 0) goto 10
  h = h + u
10 continue
end subroutine step
end module mod_kernel
"""


def _spec(hi):
    return (
        "region: ch04:step\n"
        "anchor:\n"
        "  file: src/mod_kernel.f90\n"
        f'  pst_node: "step@3-{hi}"\n'
        "  entry_symbol: step\n"
    )


def _client(tmp_path, source, hi):
    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "mod_kernel.f90").write_text(source)
    (seed / "notes" / "regions").mkdir(parents=True)
    (seed / SPEC_PATH).write_text(_spec(hi))

    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, seed)
    working = tmp_path / "working"
    working.mkdir()
    cfg = RegionConfig(
        region_id="ch04:step", repo_dir=repo_dir, spec_path=SPEC_PATH,
        ledger_dir=tmp_path / "ledger", strategy_path=STRATEGY_PATH,
        working_copy_dir=working,
    )
    store = LedgerStore(cfg.ledger_dir)
    client = TestClient(create_app({cfg.region_id: cfg}, TOKEN))
    return client, cfg, store


def _run_sese_check(client, cfg):
    return client.post(
        "/run", json={"action": "sese_check", "region": cfg.region_id, "config": {}}, headers=HEADERS,
    ).json()


def test_pass_clears_the_sese_verified_row_in_status_immediately(tmp_path):
    client, cfg, store = _client(tmp_path, CLEAN_SOURCE, hi=5)

    before = client.get("/status", params={"region": cfg.region_id}, headers=HEADERS).json()
    row_before = next(r for r in before["rows"] if r["predicateType"] == "sese/verified")
    assert row_before["status"] == "missing"

    result = _run_sese_check(client, cfg)
    assert result["verdict"] == "pass"

    after = client.get("/status", params={"region": cfg.region_id}, headers=HEADERS).json()
    row_after = next(r for r in after["rows"] if r["predicateType"] == "sese/verified")
    assert row_after["status"] == "present"
    assert row_after["claim_id"] == result["claim_id"]


def test_fail_files_against_the_current_frozen_value(tmp_path):
    client, cfg, store = _client(tmp_path, GOTO_SOURCE, hi=8)

    status_before = client.get("/status", params={"region": cfg.region_id}, headers=HEADERS).json()

    result = _run_sese_check(client, cfg)
    assert result["verdict"] == "fail"

    claim = store.get_claim(result["claim_id"])
    assert claim.subject[0].sha256 == status_before["frozen"]


def test_calling_sese_check_twice_returns_the_same_claim(tmp_path):
    client, cfg, store = _client(tmp_path, CLEAN_SOURCE, hi=5)

    first = _run_sese_check(client, cfg)
    second = _run_sese_check(client, cfg)

    assert second["claim_id"] == first["claim_id"]
    assert len(store.all_claims()) == 1
    assert store.all_requests()[-1].outcome == "duplicate"


def test_pass_writes_exactly_one_claim_outcome_request_line(tmp_path):
    client, cfg, store = _client(tmp_path, CLEAN_SOURCE, hi=5)

    _run_sese_check(client, cfg)

    requests = store.all_requests()
    assert len(requests) == 1
    assert requests[0].outcome == "claim"
    assert requests[0].claim_id is not None
