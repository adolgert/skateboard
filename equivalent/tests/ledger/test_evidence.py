"""Which build a later claim has to have been reached on top of."""
from equivalent.ledger.acceptance import ACCEPTANCE_REQUIREMENTS, PORTING
from equivalent.ledger.artifacts import build_binary_materials, decode_build_records
from equivalent.ledger.evidence import (
    BUILD_PREDICATE,
    FOUNDATION_PREDICATES,
    NO_CURRENT_BUILD,
    required_materials_by_predicate,
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


def test_the_cohort_is_read_out_of_what_a_passing_build_really_answered():
    # The cohort every later claim is judged against is scraped out of a
    # build claim's detail, and what a build claim carries is the
    # builder's own answer: one entry per target role, each naming the
    # executable it produced. Reading that shape wrongly would leave an
    # empty cohort, which quietly retires every claim resting on the
    # build.
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
