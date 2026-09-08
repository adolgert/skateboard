"""Running the capture program the tree carries, and judging what it wrote.

These read as the statement of what "the harness captures" means: every
dataset the manifest declares gets a run of the code's own capture
program, every case it wrote holds exactly the variables the region
declares in the types it declares, and the two datasets a port is judged
by are not the same run twice. What passes is stored, so the checks
after this one compare against the bytes this one approved.
"""
from __future__ import annotations

import base64
from functools import partial

import pytest

from equivalent.capture import npy
from equivalent.components import harness_capture
from equivalent.components.errors import ComponentError
from equivalent.components.workspaces import attempt_id_for_strategy
from equivalent.ledger import capture_sets
from equivalent.components.answers import CaptureResponse
from equivalent.tests.fakes import FakeBuilder, captured, captured_cases, write_tree

REGION = "tsunami:onboarding"
VISIBLE_ARGS = ["100", "5000", "25", "0.02"]
HOLDOUT_ARGS = ["100", "5000", "60", "0.01"]


def _check(harness, seed=None, builder=None):
    """The capture check, with what it packed filed the way the gateway files it."""
    harness.repo(seed)
    result = harness_capture.check(
        harness.context(region_id=REGION, builder=builder or harness.builder), {},
    )
    harness.keep(result)
    return result


def test_every_declared_dataset_is_captured_and_stored(harness):
    result = _check(harness)

    assert result.verdict == "pass"
    datasets = result.detail["datasets"]
    assert sorted(datasets) == ["holdout", "visible"]
    assert datasets["visible"]["cases"] == 2
    # Each set is in the ledger under the subject the claim names, and
    # comes back as the cases that were captured.
    stored = capture_sets.load_capture_set(harness.store, datasets["visible"]["capture_set"])
    assert sorted(stored) == ["case0000", "case0001"]
    assert sorted(stored["case0000"]["inputs"]) == ["field", "flux"]


def test_each_dataset_is_captured_with_its_own_arguments(harness):
    builder = harness.builder

    _check(harness)

    calls = {call["run_name"]: call for call in builder.capture_calls}
    assert sorted(calls) == ["holdout", "visible"]
    assert calls["visible"]["args"] == VISIBLE_ARGS
    assert calls["holdout"]["args"] == HOLDOUT_ARGS
    # The capture program is the one the manifest names, built in the
    # baseline strategy's own workspace.
    assert calls["visible"]["executable"] == "gen_reference"
    assert calls["visible"]["attempt_id"] == attempt_id_for_strategy(
        REGION, harness.tree.sha, "cpu_reference",
    )


def test_two_datasets_that_are_the_same_run_hold_nothing_back(harness):
    # A capture program that ignores its arguments writes the held-out
    # cases the agent can already see.
    same = captured_cases(VISIBLE_ARGS)
    builder = FakeBuilder(capture=partial(captured, by_args={
        tuple(VISIBLE_ARGS): same, tuple(HOLDOUT_ARGS): same,
    }))

    result = _check(harness, builder=builder)

    assert result.verdict == "fail"
    assert "visible" in result.detail["problems"][0]
    assert "holdout" in result.detail["problems"][0]


def test_a_case_missing_a_declared_output_fails_naming_it(harness):
    cases = captured_cases(HOLDOUT_ARGS)
    del cases["case0001"]["outputs"]["flux"]
    builder = FakeBuilder(capture=partial(captured, by_args={tuple(HOLDOUT_ARGS): cases}))

    result = _check(harness, builder=builder)

    assert result.verdict == "fail"
    problem = "\n".join(result.detail["problems"])
    assert "holdout" in problem and "case0001" in problem and "flux" in problem


def test_a_case_holding_a_variable_the_region_does_not_declare_fails_naming_it(harness):
    cases = captured_cases(VISIBLE_ARGS)
    cases["case0000"]["inputs"]["scratch"] = cases["case0000"]["inputs"]["field"]
    builder = FakeBuilder(capture=partial(captured, by_args={tuple(VISIBLE_ARGS): cases}))

    result = _check(harness, builder=builder)

    assert result.verdict == "fail"
    assert "scratch" in "\n".join(result.detail["problems"])


def test_an_array_of_the_wrong_element_type_fails_naming_the_variable(harness):
    cases = captured_cases(VISIBLE_ARGS)
    wrong = npy.decode(base64.b64decode(cases["case0000"]["inputs"]["field"])).astype("<f8")
    cases["case0000"]["inputs"]["field"] = base64.b64encode(npy.encode(wrong)).decode()
    builder = FakeBuilder(capture=partial(captured, by_args={tuple(VISIBLE_ARGS): cases}))

    result = _check(harness, builder=builder)

    assert result.verdict == "fail"
    assert "field" in "\n".join(result.detail["problems"])


def test_a_dataset_with_no_cases_at_all_fails_naming_the_dataset(harness):
    builder = FakeBuilder(capture=CaptureResponse(
        ok=False, stdout_tail="the capture run wrote no case directory",
    ))

    result = _check(harness, builder=builder)

    assert result.verdict == "fail"
    assert "visible" in "\n".join(result.detail["problems"])


def test_nothing_is_stored_when_a_dataset_is_refused(harness):
    # A capture set is what every later comparison is made against, so a
    # set that failed its own check must not be sitting in the ledger for
    # something to compare against later.
    cases = captured_cases(VISIBLE_ARGS)
    del cases["case0000"]["outputs"]["field"]
    builder = FakeBuilder(capture=partial(captured, by_args={tuple(VISIBLE_ARGS): cases}))

    result = _check(harness, builder=builder)

    assert result.verdict == "fail"
    assert list(harness.store.capture_sets_dir.iterdir()) == []


def test_a_code_that_declares_no_capture_target_is_told_so(harness):
    import yaml

    from equivalent.tests.fakes import in_tree_manifest
    manifest = in_tree_manifest()
    manifest["build"] = {
        **manifest["build"],
        "targets": {
            role: spec for role, spec in manifest["build"]["targets"].items() if role != "capture"
        },
    }
    seed = write_tree(harness.tmp_path / "seed")
    (seed / "harness" / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    result = _check(harness, seed)

    assert result.verdict == "fail"
    assert "capture" in result.detail["problems"][0]


def test_a_builder_that_cannot_be_reached_is_an_error_not_a_verdict(harness):
    class UnreachableBuilder(FakeBuilder):
        def capture(self, *args, **kwargs):
            raise RuntimeError("connection refused")

    harness.builder = UnreachableBuilder()

    with pytest.raises(ComponentError):
        _check(harness)
