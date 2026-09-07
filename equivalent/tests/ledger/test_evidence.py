"""Which build a later claim has to have been reached on top of."""
import pytest

from equivalent.ledger.acceptance import ACCEPTANCE_REQUIREMENTS, PORTING
from equivalent.ledger.artifacts import build_binary_materials, decode_build_records
from equivalent.ledger.evidence import (
    BUILD_PREDICATE,
    FOUNDATION_PREDICATES,
    NO_CURRENT_BUILD,
    NO_CURRENT_TIMING_CLAIMS,
    performance_materials,
    required_materials_by_predicate,
    timing_claim_material,
)
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.ledger.table import ACTION_TABLE
from equivalent.tests.fakes import built


def test_dependent_predicates_require_the_current_build_binary_cohort(tmp_path):
    store = LedgerStore(tmp_path / "ledger")
    tree = Subject(kind="tree", sha256="1" * 64)
    core = (Subject(kind="strategy", sha256="2" * 64),)
    store.record_claim(
        [tree], "build/replay",
        Predicate(
            tool="builder", version="0.1", configHash="cfg", verdict="pass",
            detail={
                "attempt_id": "attempt",
                "targets": {"replay": {
                    "executable": "replay", "sha256": "3" * 64, "size": 10,
                }},
            },
        ),
        core, "session",
    )

    contexts = required_materials_by_predicate(
        store, ACCEPTANCE_REQUIREMENTS, PORTING, tree, core,
    )

    assert "build/replay" not in contexts
    assert Subject(kind="binary", sha256="3" * 64) in contexts["gpu/executed"]
    assert contexts["sanitize/initcheck"] == contexts["gpu/executed"]


def test_missing_build_uses_a_sentinel_that_no_legacy_claim_can_satisfy(tmp_path):
    store = LedgerStore(tmp_path / "ledger")
    tree = Subject(kind="tree", sha256="1" * 64)

    contexts = required_materials_by_predicate(
        store, ACCEPTANCE_REQUIREMENTS, PORTING, tree, (),
    )

    assert NO_CURRENT_BUILD in contexts["gpu/executed"]


def test_performance_uses_the_exact_baseline_and_current_build_cohort_timing_claims(tmp_path):
    store = LedgerStore(tmp_path / "ledger")
    tree = Subject(kind="tree", sha256="1" * 64)
    baseline = Subject(kind="tree", sha256="2" * 64)
    other_baseline = Subject(kind="tree", sha256="3" * 64)
    current_binary = Subject(kind="binary", sha256="4" * 64)
    old_binary = Subject(kind="binary", sha256="5" * 64)

    baseline_claim = store.record_claim(
        [baseline], "timing/baseline",
        Predicate(tool="builder", version="0.1", configHash="cfg", verdict="pass", detail={}),
        (), "session",
    )
    store.record_claim(
        [other_baseline], "timing/baseline",
        Predicate(tool="builder", version="0.1", configHash="cfg", verdict="pass", detail={}),
        (), "session",
    )
    current_port = store.record_claim(
        [tree], "timing/port",
        Predicate(tool="builder", version="0.1", configHash="cfg", verdict="pass", detail={}),
        (current_binary,), "session",
    )
    # A newer observation on an obsolete binary cannot replace the current
    # cohort's timing input to a fresh performance verdict.
    store.record_claim(
        [tree], "timing/port",
        Predicate(tool="builder", version="0.1", configHash="cfg", verdict="pass", detail={}),
        (old_binary,), "session",
    )

    assert performance_materials(
        store, tree, baseline, (), port_materials=(current_binary,),
    ) == (timing_claim_material(current_port), timing_claim_material(baseline_claim))
    # Without a trusted baseline source, a reader must not choose an
    # unrelated historical baseline claim by timestamp.
    assert performance_materials(
        store, tree, None, (), port_materials=(current_binary,),
    ) == (NO_CURRENT_TIMING_CLAIMS,)

    # A newer failed baseline timing action retires the comparison just as
    # a failed port timing action does.
    store.record_claim(
        [baseline], "timing/baseline",
        Predicate(tool="builder", version="0.1", configHash="cfg", verdict="fail", detail={}),
        (), "session",
    )
    assert performance_materials(
        store, tree, baseline, (), port_materials=(current_binary,),
    ) == (NO_CURRENT_TIMING_CLAIMS,)

    # A newer failed port timing action also retires the otherwise-current
    # observation and makes performance acceptance unavailable.
    store.record_claim(
        [tree], "timing/port",
        Predicate(tool="builder", version="0.1", configHash="cfg", verdict="fail", detail={}),
        (current_binary,), "session",
    )
    assert performance_materials(
        store, tree, baseline, (), port_materials=(current_binary,),
    ) == (NO_CURRENT_TIMING_CLAIMS,)


def test_the_cohort_is_read_out_of_what_a_passing_build_really_answered():
    # The cohort every later claim is judged against is decoded from the
    # build predicate's specified detail layout. A build claim carries the
    # builder's own answer: one entry per target role, each naming the files
    # it produced. Reading that shape wrongly would leave an empty cohort,
    # which quietly retires every claim resting on the build.
    answer = built({
        "flags": ["-O2"],
        "targets": [{"role": "replay", "target": "replay", "executable": "replay"}],
    })

    cohort = build_binary_materials(
        "build/replay", {"attempt_id": "attempt", "targets": answer.targets},
    )

    assert [subject.sha256 for subject in cohort] == [answer.targets["replay"]["sha256"]]


def test_incidental_nested_artifact_keys_do_not_become_build_evidence():
    detail = {
        "attempt_id": "attempt",
        "targets": {"replay": {
            "executable": "replay", "sha256": "3" * 64, "size": 10,
        }},
        "diagnostic": {
            "targets": {"spoof": {
                "executable": "spoof", "sha256": "4" * 64, "size": 20,
            }},
            "executable_identity": {"sha256": "5" * 64},
        },
    }

    assert build_binary_materials("build/replay", detail) == (
        Subject(kind="binary", sha256="3" * 64),
    )


def test_a_partly_malformed_onboarding_build_is_not_restorable():
    detail = {"strategies": {
        "cpu_reference": {
            "attempt_id": "one",
            "targets": {"replay": {
                "executable": "replay", "sha256": "3" * 64, "size": 10,
            }},
        },
        "stdpar_managed": "diagnostic text is not a build entry",
    }}

    assert decode_build_records("harness/builds", detail) == ()
    assert build_binary_materials("harness/builds", detail) == ()


def test_runtime_companions_join_the_build_binary_cohort_and_legacy_targets_still_decode():
    legacy = {
        "attempt_id": "old", "targets": {"replay": {
            "executable": "replay", "sha256": "1" * 64, "size": 10,
        }},
    }
    current = {
        "attempt_id": "new", "targets": {"replay": {
            "executable": "replay", "sha256": "1" * 64, "size": 10,
            "runtime_artifacts": [
                {"path": "lib/kernel.so", "kind": "shared_library",
                 "sha256": "2" * 64, "size": 20},
                {"path": "modules/kernel.ptx", "kind": "gpu_module",
                 "sha256": "3" * 64, "size": 30},
            ],
        }},
    }

    assert [m.sha256 for m in build_binary_materials("build/replay", legacy)] == ["1" * 64]
    assert [m.sha256 for m in build_binary_materials("build/replay", current)] == [
        "1" * 64, "2" * 64, "3" * 64,
    ]


@pytest.mark.parametrize("path", ["../kernel.ptx", "/tmp/kernel.ptx"])
def test_a_build_claim_with_an_unsafe_runtime_artifact_path_fails_closed(path):
    detail = {
        "attempt_id": "new", "targets": {"replay": {
            "executable": "replay", "sha256": "1" * 64, "size": 10,
            "runtime_artifacts": [{
                "path": path, "kind": "gpu_module", "sha256": "2" * 64, "size": 20,
            }],
        }},
    }

    assert decode_build_records("build/replay", detail) == ()
    assert build_binary_materials("build/replay", detail) == ()


def _emitted_without_a_build() -> set:
    """Predicates from action rows that rest on nothing a build produced.

    A row qualifies when everything it requires already qualifies and
    none of it is a build, so the build actions themselves qualify (they
    rest on the analyzer or the manifest) while the first action after a
    build does not.
    """
    builds = set(BUILD_PREDICATE.values())
    found: set = set()
    changed = True
    while changed:
        changed = False
        for row in ACTION_TABLE:
            if not row.emits or set(row.emits) <= found:
                continue
            needed = {predicate for predicate, _ in row.requires}
            if needed <= found - builds:
                found |= set(row.emits)
                changed = True
    return found


def test_the_foundation_list_is_what_the_action_table_says_rests_on_no_build():
    # Written out where it is read rather than derived, so a reader can
    # see which claims are exempt; held to the table here so the two
    # cannot drift apart.
    assert FOUNDATION_PREDICATES == _emitted_without_a_build()
