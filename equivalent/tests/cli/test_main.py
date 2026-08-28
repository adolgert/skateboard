import json
from pathlib import Path

from equivalent.cli.main import main
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject


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

    config_path = tmp_path / "gateway.yaml"
    config_path.write_text(yaml.safe_dump({
        "version": 1,
        "paths": {
            "repo": str(tmp_path / "repo"),
            "ledger_root": str(tmp_path / "ledger"),
            "working_copy": str(tmp_path / "working"),
            "strategies": str(strategies),
            "seed": str(seed),
        },
        "regions": {"ch04:step": {"spec_path": spec_path, "strategy": "stdpar_managed"}},
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
