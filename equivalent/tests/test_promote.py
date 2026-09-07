"""Promoting what an onboarding session proved into the code's own directory.

Every test builds a real deployment in a temporary directory -- a
repository holding the tree, a ledger holding the claims that tree
earned, a working copy that matches it, and a directory to write the
promoted code into -- and then runs the command line, because what is
being asked is whether a person following the printed instructions ends
up with a code directory the rest of the harness can read.
"""
from pathlib import Path

import yaml

from equivalent.capture import npy
from equivalent.cli.main import main
from equivalent.ledger.acceptance import requirements_for
from equivalent.ledger.capture_sets import store_capture_set
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.manifest.schema import IN_TREE_MANIFEST, load_manifest
from equivalent.region.deployment import load_gateway_config
from equivalent.region.evidence import evidence_materials_for
from equivalent.tree import Tree, baseline_commit, init_baseline_repo
from equivalent.tests.fakes import (
    fixture_arrays,
    in_tree_manifest,
    stepped,
    timing_array,
    write_program,
    write_tree,
)

CODE = "tsunami"
REGION = "tsunami:onboard"
# The region's ledger directory, as the gateway spells it.
REGION_SLUG = "tsunami-onboard"
STRATEGY_DIR = Path(__file__).resolve().parents[1] / "strategy" / "files"
# The claims that carry a capture set, and what the set is called there.
CAPTURED = "harness/captured"
TIMED = "harness/times"
# The one file the timing run of the fixture's program writes.
TIMING_OUTPUT = "field.npy"


def _cases(offset: int) -> dict:
    """One dataset of one case, as a capture set holds it: arrays in and out."""
    inputs = fixture_arrays(offset)
    return {"case0000": {"inputs": inputs, "outputs": stepped(inputs)}}


def _program_case() -> dict:
    """The one case a timing run leaves: the files the program wrote, as arrays."""
    name = TIMING_OUTPUT[: -len(npy.INPUT_SUFFIX)]
    return {"program": {"inputs": {}, "outputs": {name: timing_array(TIMING_OUTPUT)}}}


def _write_claims(store: LedgerStore, tree, details: dict, omit: str = "", materials=()) -> None:
    """One passing claim per onboarding requirement, in the order they are asked for."""
    binary = Subject(kind="binary", sha256="d" * 64)
    details = {"harness/builds": {"targets": {"replay": {"sha256": binary.sha256}}}, **details}
    for requirement in requirements_for("onboarding"):
        if requirement.predicate_type == omit:
            continue
        store.record_claim(
            [tree], requirement.predicate_type,
            Predicate(
                tool="t", version="0.1", configHash="cfg", verdict="pass",
                detail=details.get(requirement.predicate_type, {}),
            ),
            (*materials, binary), "sess-1",
        )


def _write_config(tmp_path: Path, programs: Path, phase: str = "onboarding") -> Path:
    """The deployment's own configuration file, in this machine's paths."""
    region = {
        "code": CODE, "phase": phase,
        "strategy": "onboarding" if phase == "onboarding" else "stdpar_managed",
        "baseline_strategy": "cpu_reference",
        "executor_identity": "b" * 64,
    }
    if phase == "porting":
        region["spec_path"] = "notes/regions/ch04-step.sese.yaml"
    config = {
        "version": 1,
        "paths": {
            "repo": str(tmp_path / "repo"),
            "ledger_root": str(tmp_path / "ledger"),
            "working_copy": str(tmp_path / "working"),
            "programs": str(programs),
            "strategies": str(STRATEGY_DIR),
        },
        "codes": {CODE: {"manifest": f"{CODE}/manifest.yaml"}},
        "regions": {REGION: region},
    }
    path = tmp_path / "gateway.host.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    return path


def _deployment(tmp_path, *, manifest_text=None, omit: str = "", phase: str = "onboarding") -> dict:
    """A deployment whose onboarding region has reached the end of its checks.

    `manifest_text` writes the tree's manifest as given rather than as the
    fixture spells it, `omit` leaves out one requirement's claim, and
    `phase` says which kind of region the configuration describes -- which
    is how each test asks for the one thing it is about.
    """
    seed = write_tree(tmp_path / "seed")
    working = write_tree(tmp_path / "working")
    if manifest_text is not None:
        for root in (seed, working):
            (root / IN_TREE_MANIFEST).write_text(manifest_text)
    repo = tmp_path / "repo"
    init_baseline_repo(repo, seed)
    programs = write_program(tmp_path, CODE, minimal=phase == "onboarding").parent
    config_path = _write_config(tmp_path, programs, phase)
    materials = evidence_materials_for(load_gateway_config(config_path).regions[REGION])

    store = LedgerStore(tmp_path / "ledger" / baseline_commit(repo) / REGION_SLUG)
    tree = Subject(kind="tree", sha256=Tree.baseline(repo).sha)
    visible = store_capture_set(store, "visible", _cases(0)).sha256
    holdout = store_capture_set(store, "holdout", _cases(50)).sha256
    program = store_capture_set(store, "program", _program_case()).sha256
    _write_claims(store, tree, {
        CAPTURED: {"datasets": {
            "visible": {"cases": 1, "capture_set": visible},
            "holdout": {"cases": 1, "capture_set": holdout},
        }},
        TIMED: {"datasets": {"program": {"cases": 1, "capture_set": program}}},
    }, omit=omit, materials=materials)

    return {
        "config": config_path,
        "working": tmp_path / "working",
        "destination": tmp_path / "promoted",
    }


def _promote(deployment, *extra) -> int:
    return main([
        "promote", "--config", str(deployment["config"]), "--region-id", REGION,
        "--programs", str(deployment["destination"]), *extra,
    ])


def test_promote_writes_the_layout_a_code_directory_holds(tmp_path, capsys):
    deployment = _deployment(tmp_path)

    rc = _promote(deployment)
    out = capsys.readouterr().out
    code_dir = deployment["destination"] / CODE

    assert rc == 0
    assert (code_dir / "manifest.yaml").is_file()
    assert (code_dir / "baseline" / "Makefile").is_file()
    assert (code_dir / "baseline" / "src" / "mod_kernel.f90").is_file()
    # The manifest leaves the tree: beside the code it is the code's own
    # description, and inside the tree it would be a second copy.
    assert not (code_dir / "baseline" / IN_TREE_MANIFEST).exists()
    assert (code_dir / "datasets" / "visible" / npy.CASES_FILE).is_file()
    for name in ("visible", "holdout"):
        assert (code_dir / "captures" / name / npy.CASES_FILE).is_file()
    # Not the program's own outputs. They are megabytes at a real timing
    # size, and what a port is compared against is the deployment's own
    # baseline timing run, which stays in the region's ledger.
    assert not (code_dir / "captures" / "program").exists()
    # And the person is told what to do with what was written.
    assert f"git add {code_dir}" in out


def test_rebuilt_binary_invalidates_offline_status_and_promotion(tmp_path, capsys):
    import json

    deployment = _deployment(tmp_path)
    cfg = load_gateway_config(deployment["config"]).regions[REGION]
    store = LedgerStore(tmp_path / "ledger" / baseline_commit(cfg.repo_dir) / REGION_SLUG)
    tree = Subject(kind="tree", sha256=Tree.baseline(cfg.repo_dir).sha)
    store.record_claim(
        [tree], "harness/builds",
        Predicate(tool="t", version="0.1", configHash="rebuilt", verdict="pass",
                  detail={"targets": {"replay": {"sha256": "e" * 64}}}),
        evidence_materials_for(cfg), "sess-2",
    )

    assert main(["status", "--config", str(deployment["config"]),
                 "--region-id", REGION, "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["accepted"] is False
    captured = next(row for row in status["rows"] if row["predicateType"] == CAPTURED)
    assert captured["evidence_status"] == "stale"
    assert _promote(deployment) == 1
    assert "harness/captured" in capsys.readouterr().err
    assert not deployment["destination"].exists()


def test_promotion_requires_a_reviewed_executor_pin(tmp_path, capsys):
    deployment = _deployment(tmp_path)
    path = deployment["config"]
    raw = yaml.safe_load(path.read_text())
    del raw["regions"][REGION]["executor_identity"]
    path.write_text(yaml.safe_dump(raw))
    assert _promote(deployment) == 1
    assert "executor_identity" in capsys.readouterr().err
    assert not deployment["destination"].exists()


def test_changed_executor_pin_invalidates_promotion(tmp_path, capsys):
    deployment = _deployment(tmp_path)
    path = deployment["config"]
    raw = yaml.safe_load(path.read_text())
    raw["regions"][REGION]["executor_identity"] = "c" * 64
    path.write_text(yaml.safe_dump(raw))
    assert _promote(deployment) == 1
    assert "not ONBOARDED" in capsys.readouterr().err
    assert not deployment["destination"].exists()


def test_the_promoted_manifest_describes_a_code_a_region_of_which_can_be_ported(tmp_path):
    deployment = _deployment(tmp_path)

    _promote(deployment)
    code_dir = deployment["destination"] / CODE
    manifest = load_manifest(code_dir / "manifest.yaml")

    assert manifest.complete
    # It says its source is the baseline beside it, not the tree it was
    # written in.
    assert manifest.source.root == code_dir / "baseline"


def test_the_promoted_datasets_are_the_capture_sets_the_claims_named(tmp_path):
    deployment = _deployment(tmp_path)
    visible, holdout = _cases(0), _cases(50)

    _promote(deployment)
    code_dir = deployment["destination"] / CODE
    seen = {
        "datasets/visible": npy.load_dataset(code_dir / "datasets" / "visible"),
        "captures/visible": npy.load_dataset(code_dir / "captures" / "visible"),
        "captures/holdout": npy.load_dataset(code_dir / "captures" / "holdout"),
    }

    # The visible set is split across the trust boundary: the agent's side
    # holds the inputs, the oracle's side the answers.
    assert list(seen["datasets/visible"]["case0000"]["inputs"]) == list(visible["case0000"]["inputs"])
    assert seen["datasets/visible"]["case0000"]["outputs"] == {}
    assert seen["captures/visible"]["case0000"]["inputs"] == {}
    for name, array in visible["case0000"]["outputs"].items():
        assert (seen["captures/visible"]["case0000"]["outputs"][name] == array).all()
    # The held-out set is whole, and never leaves the oracle's side.
    for half in ("inputs", "outputs"):
        for name, array in holdout["case0000"][half].items():
            assert (seen["captures/holdout"]["case0000"][half][name] == array).all()


def test_it_refuses_when_a_claim_the_region_needs_is_missing_and_names_it(tmp_path, capsys):
    deployment = _deployment(tmp_path, omit=TIMED)

    rc = _promote(deployment)
    err = capsys.readouterr().err

    assert rc == 1
    assert TIMED in err
    assert not (deployment["destination"] / CODE).exists()


def test_it_refuses_when_the_working_copy_is_not_the_tree_that_passed(tmp_path, capsys):
    deployment = _deployment(tmp_path)
    (deployment["working"] / "src" / "mod_kernel.f90").write_text("end module\n")

    rc = _promote(deployment)
    err = capsys.readouterr().err

    assert rc == 1
    # The person reviews the working copy, so the refusal names the file
    # they would have read that is not the file that passed.
    assert "src/mod_kernel.f90" in err
    assert not (deployment["destination"] / CODE).exists()


def test_it_refuses_to_write_over_what_is_already_there_unless_asked(tmp_path, capsys):
    deployment = _deployment(tmp_path)
    _promote(deployment)
    capsys.readouterr()

    refused = _promote(deployment)
    err = capsys.readouterr().err
    replaced = _promote(deployment, "--replace")
    out = capsys.readouterr().out

    assert refused == 1
    assert "baseline" in err and "--replace" in err
    assert replaced == 0
    # What was removed is said in files, so the person can see whether it
    # was the directory they meant.
    assert "removed" in out


def test_it_refuses_a_manifest_whose_source_root_is_not_a_line_of_its_own(tmp_path, capsys):
    # A manifest written all on one line is legal YAML and unreadable to a
    # rewrite that replaces one line, so it is turned away with the shape
    # it should have been written in.
    deployment = _deployment(
        tmp_path, manifest_text=yaml.safe_dump(in_tree_manifest(CODE), default_flow_style=True),
    )

    rc = _promote(deployment)
    err = capsys.readouterr().err

    assert rc == 1
    assert "root: ." in err


def test_it_refuses_a_region_that_is_not_being_onboarded(tmp_path, capsys):
    deployment = _deployment(tmp_path, phase="porting")

    rc = _promote(deployment)
    err = capsys.readouterr().err

    assert rc == 1
    assert "onboarding" in err
