import json
from pathlib import Path

from equivalent.cli.main import main
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.tests.fakes import write_program


def test_status_command_runs_end_to_end(tmp_path, capsys):
    region_dir = tmp_path / "ch04-step"
    store = LedgerStore(region_dir)
    store.record_claim(
        [Subject(kind="tree", sha256="a" * 64)], "build/replay",
        Predicate(tool="t", version="0.1", configHash="cfg", verdict="pass", detail={}),
        [], "sess-1",
    )

    rc = main(["status", str(region_dir)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "ch04-step" in out
    assert "build/replay" in out
    assert "c-0001" in out


def test_show_command_reports_missing_claim(tmp_path, capsys):
    region_dir = tmp_path / "region"
    LedgerStore(region_dir)  # creates the (empty) region directory

    rc = main(["show", str(region_dir), "c-9999"])
    err = capsys.readouterr().err

    assert rc == 1
    assert "c-9999" in err


def test_history_command_runs_end_to_end(tmp_path, capsys):
    region_dir = tmp_path / "region"
    store = LedgerStore(region_dir)
    for n, sha in enumerate(("a" * 64, "b" * 64), start=1):
        store.record_claim(
            [Subject(kind="tree", sha256=sha)], "build/replay",
            Predicate(tool="t", version="0.1", configHash=f"cfg-{n}", verdict="pass", detail={}),
            [], "sess-1",
        )

    rc = main(["history", str(region_dir)])
    out = capsys.readouterr().out

    assert rc == 0
    assert out.count("tree ") == 2
    assert "c-0001" in out and "c-0002" in out


def test_requests_command_runs_end_to_end(tmp_path, capsys):
    from equivalent.ledger.records import RequestLogLine

    region_dir = tmp_path / "region"
    store = LedgerStore(region_dir)
    store.append_request(RequestLogLine(
        ts="2026-01-01T00:00:00Z", session="sess-1", model="m", endpoint="run",
        action="build_replay", region="ch04:step", tree="a" * 64,
        config_hash=None, outcome="refused",
        missing=({"predicateType": "sese/verified"},),
    ))

    rc = main(["requests", str(region_dir)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "build_replay" in out
    assert "refused" in out


def test_status_json_flag_produces_parseable_json(tmp_path, capsys):
    region_dir = tmp_path / "region"
    LedgerStore(region_dir)

    rc = main(["status", str(region_dir), "--json"])
    out = capsys.readouterr().out

    parsed = json.loads(out)
    assert parsed["accepted"] is False
    assert rc == 0


def test_status_with_a_configuration_file_shows_the_tree_the_gateway_shows(tmp_path, capsys):
    # Naming only a ledger directory, the CLI can report the tree of the
    # last claim and no more. Given the gateway's own configuration file
    # it reads the repository too, and then both answers are the same
    # tree -- which is the point of reading the one configuration loader
    # from both sides.
    import yaml
    from fastapi.testclient import TestClient

    from equivalent.gateway.app import create_app
    from equivalent.gateway.config import load_gateway_config

    strategies = Path(__file__).resolve().parents[2] / "strategy" / "files"
    spec_path = "notes/regions/ch04-step.sese.yaml"
    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "mod_kernel.f90").write_text("subroutine step\nend subroutine\n")
    (tmp_path / "working" / "notes" / "regions").mkdir(parents=True)
    (tmp_path / "working" / spec_path).write_text("region: ch04:step\n")

    programs = write_program(tmp_path).parent
    config_path = tmp_path / "gateway.yaml"
    config_path.write_text(yaml.safe_dump({
        "version": 1,
        "paths": {
            "repo": str(tmp_path / "repo"),
            "ledger_root": str(tmp_path / "ledger"),
            "working_copy": str(tmp_path / "working"),
            "programs": str(programs),
            "strategies": str(strategies),
            "seed": str(seed),
        },
        "codes": {"tsunami": {"manifest": "tsunami/manifest.yaml"}},
        "regions": {"ch04:step": {
            "code": "tsunami", "spec_path": spec_path, "strategy": "stdpar_managed",
            "baseline_strategy": "cpu_reference",
        }},
    }))

    config = load_gateway_config(config_path, seed_if_empty=True)
    client = TestClient(create_app(config.regions, "test-token"))
    headers = {"Authorization": "Bearer test-token", "X-Session-Id": "sess-1", "X-Model-Id": "m"}
    client.post("/submit", json={"region": "ch04:step"}, headers=headers)
    from_gateway = client.get("/status", params={"region": "ch04:step"}, headers=headers).json()

    rc = main(["status", "--config", str(config_path), "--region-id", "ch04:step", "--json"])
    from_cli = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert from_cli == from_gateway


def test_session_command_runs_end_to_end(tmp_path, capsys):
    import yaml

    from equivalent.gateway.submit import init_baseline_repo
    from equivalent.ledger.records import RequestLogLine

    strategies = Path(__file__).resolve().parents[2] / "strategy" / "files"
    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "mod_kernel.f90").write_text("subroutine step\nend subroutine\n")
    baseline = init_baseline_repo(tmp_path / "repo", seed)
    (tmp_path / "working").mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    programs = write_program(tmp_path).parent
    config_path = tmp_path / "gateway.yaml"
    config_path.write_text(yaml.safe_dump({
        "version": 1,
        "paths": {
            "repo": str(tmp_path / "repo"),
            "ledger_root": str(tmp_path / "ledger"),
            "working_copy": str(tmp_path / "working"),
            "programs": str(programs),
            "strategies": str(strategies),
            "sessions": str(sessions),
        },
        "codes": {"tsunami": {"manifest": "tsunami/manifest.yaml"}},
        "regions": {"ch04:step": {"code": "tsunami",
                                  "spec_path": "notes/regions/ch04-step.sese.yaml",
                                  "strategy": "stdpar_managed",
                                  "baseline_strategy": "cpu_reference"}},
    }))

    store = LedgerStore(tmp_path / "ledger" / baseline / "ch04-step")
    store.append_request(RequestLogLine(
        ts="2026-01-01T00:00:00Z", session="sess-1", model="m", endpoint="run", action="sese_check",
        region="ch04:step", tree="a" * 64, config_hash="cfg", outcome="claim", claim_id="c-0001",
        tool_call_id="tool:1:aaa",
    ))

    rc = main(["session", "sess-1", "--config", str(config_path), "--region-id", "ch04:step", "--json"])
    parsed = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert [row["who"] for row in parsed["timeline"]] == ["sese_check"]
    assert parsed["unmatched_requests"][0]["tool_call_id"] == "tool:1:aaa"
    assert parsed["summary"]["time_to_acceptance"] == "not accepted"
