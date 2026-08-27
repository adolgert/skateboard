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


def test_status_json_flag_produces_parseable_json(tmp_path, capsys):
    region_dir = tmp_path / "region"
    LedgerStore(region_dir)

    rc = main(["status", str(region_dir), "--json"])
    out = capsys.readouterr().out

    import json
    parsed = json.loads(out)
    assert parsed["accepted"] is False
    assert rc == 0
