"""POST /run dispatching to the real sese_check component.

test_run.py already covers refusal/duplicate logic against
synthetic claims recorded directly on the store; these tests exercise the
real analyzer dispatch end to end, through the HTTP layer.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from equivalent.gateway.app import create_app
from equivalent.gateway.regions import RegionConfig
from equivalent.gateway.submit import frozen_for_allow_globs, init_baseline_repo
from equivalent.ledger.acceptance import PORTING
from equivalent.ledger.store import LedgerStore
from equivalent.manifest.schema import load_manifest
from equivalent.tests.fakes import write_program

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Session-Id": "sess-1", "X-Model-Id": "claude-sonnet-5"}
STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategy" / "files" / "stdpar_managed.yaml"
BASELINE_STRATEGY_PATH = STRATEGY_PATH.parent / "cpu_reference.yaml"
SPEC_PATH = "notes/regions/ch04-step.sese.yaml"

CLEAN_SOURCE = """\
module mod_kernel
contains
subroutine step(a, b)
  real :: a, b
  a = a + b
end subroutine step
end module mod_kernel
"""

GOTO_SOURCE = """\
module mod_kernel
contains
subroutine step(a, b)
  real :: a, b
  if (h > 0) goto 10
  a = a + b
10 continue
end subroutine step
end module mod_kernel
"""

DIFF_SOURCE = """\
module mod_diff
contains
pure function diff(x) result(dx)
  real :: x(:), dx(size(x))
  dx = x
end function diff
end module mod_diff
"""

# The region spans two files: the anchor's own, and the module holding the
# stencil it calls.
REGION_FILES = ["src/mod_kernel.f90", "src/mod_diff.f90"]


def _spec(hi):
    return (
        "region: ch04:step\n"
        "files:\n"
        "  - src/mod_kernel.f90\n"
        "  - src/mod_diff.f90\n"
        "anchor:\n"
        "  file: src/mod_kernel.f90\n"
        f'  pst_node: "step@3-{hi}"\n'
        "  entry_symbol: step\n"
        "closure:\n"
        "  callees:\n"
        "    - name: diff\n"
        "      file: src/mod_diff.f90\n"
        '      lines: "3-6"\n'
    )


def _client(tmp_path, source, hi):
    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "mod_kernel.f90").write_text(source)
    (seed / "src" / "mod_diff.f90").write_text(DIFF_SOURCE)
    (seed / "notes" / "regions").mkdir(parents=True)
    (seed / SPEC_PATH).write_text(_spec(hi))

    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, seed)
    working = tmp_path / "working"
    working.mkdir()
    cfg = RegionConfig(
        region_id="ch04:step", code="tsunami", phase=PORTING, repo_dir=repo_dir,
        spec_path=SPEC_PATH,
        ledger_dir=tmp_path / "ledger", strategy_path=STRATEGY_PATH,
        baseline_strategy_path=BASELINE_STRATEGY_PATH,
        working_copy_dir=working,
        manifest=load_manifest(write_program(tmp_path) / "manifest.yaml"),
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


def test_a_pass_unfreezes_every_file_the_spec_lists(tmp_path):
    client, cfg, store = _client(tmp_path, CLEAN_SOURCE, hi=5)

    result = _run_sese_check(client, cfg)
    assert result["verdict"] == "pass"

    claim = store.get_claim(result["claim_id"])
    assert claim.predicate.detail["file_list"] == sorted(REGION_FILES)

    # The frozen value the claim is filed against is the one that leaves
    # both listed files and the spec out -- not the narrower list that
    # would still freeze the second file.
    both_unfrozen = frozen_for_allow_globs(cfg.repo_dir, [*REGION_FILES, SPEC_PATH])
    only_the_anchor = frozen_for_allow_globs(cfg.repo_dir, ["src/mod_kernel.f90", SPEC_PATH])
    assert claim.subject[0].sha256 == both_unfrozen
    assert both_unfrozen != only_the_anchor

    after = client.get("/status", params={"region": cfg.region_id}, headers=HEADERS).json()
    assert after["frozen"] == both_unfrozen


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


def test_the_claim_line_names_the_tool_call_that_asked_for_it(tmp_path):
    client, cfg, store = _client(tmp_path, CLEAN_SOURCE, hi=5)

    client.post(
        "/run", json={"action": "sese_check", "region": cfg.region_id, "config": {}},
        headers={**HEADERS, "X-Tool-Call-Id": "tool:1787913327226:mu24lznsjv"},
    )

    line = store.all_requests()[0]
    assert line.outcome == "claim"
    assert line.tool_call_id == "tool:1787913327226:mu24lznsjv"
