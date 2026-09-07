"""What varies between bringing a code in and porting one, said once.

Five things move together with the phase -- which manifest describes the
code, which strategies a build is asked under, what the builder's
workspace is called, which predicate the build claim is filed as, and
what shape that claim's detail takes -- and each was once spelled at
every call site that needed it. These say what each phase answers, and
that the checks and the gateway ask rather than decide for themselves.
"""
from __future__ import annotations

import pytest

from equivalent.components import backend, building, build_replay, harness_build, harness_replay, run_replay
from equivalent.components.phase import provenance_for
from equivalent.ledger.artifacts import decode_build_records
from equivalent.components.errors import ComponentError
from equivalent.components.names import TIMING_ROLE
from equivalent.manifest.schema import load_manifest
from equivalent.tests.components.conftest import (
    BASELINE_STRATEGY,
    Harness,
    PORT_STRATEGY,
    write_visible_dataset,
)
from equivalent.tests.fakes import FakeBuilder, write_program, write_tree
from equivalent.components.workspaces import attempt_id_for, attempt_id_for_strategy

REGION = "tsunami:onboarding"


def _promoted(harness):
    """A porting context: a manifest promoted beside the code, and a tree."""
    manifest = load_manifest(write_program(harness.tmp_path) / "manifest.yaml")
    harness.repo(write_tree(harness.tmp_path / "seed"))
    return harness.porting(region_id=REGION, manifest=manifest)


def _onboarding(harness):
    harness.repo(write_tree(harness.tmp_path / "seed"))
    return harness.context(region_id=REGION)


def test_a_port_is_judged_by_the_promoted_manifest_and_a_new_code_by_its_trees(harness, tmp_path):
    porting = _promoted(harness)
    onboarding = _onboarding(Harness(tmp_path / "onboarding"))

    assert porting.provenance.manifest() is porting.manifest
    assert onboarding.provenance.manifest().sha256 == onboarding.tree.manifest().sha256


def test_a_new_code_is_built_under_both_strategies_and_a_port_under_one(harness, tmp_path):
    porting = _promoted(harness)
    onboarding = _onboarding(Harness(tmp_path / "onboarding"))

    assert [one.name for one in porting.provenance.strategies()] == [PORT_STRATEGY]
    assert [one.name for one in onboarding.provenance.strategies()] == [
        BASELINE_STRATEGY, PORT_STRATEGY,
    ]


def test_a_new_codes_workspace_is_named_for_its_strategy_and_a_ports_is_not(harness, tmp_path):
    porting = _promoted(harness)
    onboarding = _onboarding(Harness(tmp_path / "onboarding"))

    assert porting.provenance.attempt_id() == attempt_id_for(REGION, porting.tree.sha)
    assert onboarding.provenance.attempt_id() == attempt_id_for_strategy(
        REGION, onboarding.tree.sha, BASELINE_STRATEGY,
    )


def test_the_two_phases_file_a_build_under_the_predicate_each_reads_it_by():
    assert provenance_for("porting").build_predicate == "build/replay"
    assert provenance_for("onboarding").build_predicate == "harness/builds"


def test_a_build_claims_detail_is_read_back_the_way_that_phase_wrote_it():
    porting = decode_build_records("build/replay",
        {"attempt_id": "one", "targets": {"replay": {
            "executable": "replay", "sha256": "a" * 64, "size": 1,
        }}},
    )
    onboarding = decode_build_records("harness/builds", {"strategies": {
        "cpu_reference": {"attempt_id": "one", "targets": {"replay": {
            "executable": "replay", "sha256": "a" * 64, "size": 1,
        }}},
        "stdpar_managed": {"attempt_id": "two", "targets": {"replay": {
            "executable": "replay", "sha256": "b" * 64, "size": 1,
        }}},
    }})

    assert [record.attempt_id for record in porting] == ["one"]
    assert [record.attempt_id for record in onboarding] == ["one", "two"]


def _without_timing_target(manifest):
    manifest.build.targets.pop(TIMING_ROLE)
    return manifest


def test_a_missing_target_is_the_new_codes_fault_and_the_harnesss_on_a_port(harness, tmp_path):
    # The manifest an onboarding session is judged by is the one it wrote,
    # so a target it does not declare is a verdict about that work. A port
    # is judged by a manifest promoted after review, so the same absence
    # means the harness is asking about a code nobody described that way.
    porting = _promoted(harness)
    onboarding = _onboarding(Harness(tmp_path / "onboarding"))

    target, refusal = onboarding.provenance.build_target(
        _without_timing_target(onboarding.provenance.manifest()), TIMING_ROLE, "nothing to run",
    )
    assert target is None
    assert refusal.verdict == "fail"
    assert "nothing to run" in refusal.reasons[0]

    with pytest.raises(ComponentError):
        porting.provenance.build_target(
            _without_timing_target(porting.manifest), TIMING_ROLE, "nothing to run",
        )


def test_both_phases_build_through_the_one_shared_build_verdict(harness, tmp_path, monkeypatch):
    asked = []

    def record(builder, attempt_id, tree, strategy, manifest):
        asked.append(strategy.name)
        raise AssertionError("stop here: what is being asked is who called")

    monkeypatch.setattr(building, "build_verdict", record)
    monkeypatch.setattr(build_replay, "build_verdict", record)
    porting = _promoted(harness)
    onboarding = _onboarding(Harness(tmp_path / "onboarding"))

    with pytest.raises(AssertionError):
        build_replay.check(porting, {})
    with pytest.raises(AssertionError):
        harness_build.check(onboarding, {})

    assert asked == [PORT_STRATEGY, BASELINE_STRATEGY]


def test_both_phases_run_the_replay_driver_through_the_one_shared_call(harness, tmp_path,
                                                                      monkeypatch):
    asked = []

    def record(builder, attempt_id, executable, cases, *, notify=None, mandatory=False,
               profile=None):
        asked.append(attempt_id)
        raise AssertionError("stop here: what is being asked is who called")

    monkeypatch.setattr(backend, "replay", record)

    porting = _promoted(harness)
    porting = harness.porting(
        region_id=REGION, manifest=porting.manifest, builder=FakeBuilder(),
        visible_dataset=write_visible_dataset(harness.tmp_path / "visible"),
    )

    # The onboarding replay compares against the sets a real capture run
    # approved, so it starts from one.
    onboarding_harness = Harness(tmp_path / "onboarding")
    onboarding_harness.captured()
    onboarding = onboarding_harness.context(region_id=REGION)

    with pytest.raises(AssertionError):
        run_replay.check(porting, {})
    with pytest.raises(AssertionError):
        harness_replay.check(onboarding, {})

    # One call, and each phase asked it for its own workspace.
    assert asked == [
        attempt_id_for(REGION, porting.tree.sha),
        attempt_id_for_strategy(REGION, onboarding.tree.sha, BASELINE_STRATEGY),
    ]
