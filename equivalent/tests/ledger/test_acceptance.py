"""Which requirements a region is judged by, and when the property one joins them.

Everything else on the two lists is fixed. The property requirement is
the one that depends on the code: a code that declares a module of
invariants has to pass it, and a code that declares none has nothing to
pass, so the list has to be read with that code's manifest in hand.
"""
import pytest

from equivalent.ledger.acceptance import (
    ACCEPTANCE_REQUIREMENTS,
    CONDITIONAL_REQUIREMENTS,
    ONBOARDING,
    ONBOARDING_REQUIREMENTS,
    PORTING,
    acceptance_requirements,
    requirements_for,
)
from equivalent.ledger.status import compute_status
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.manifest.schema import load_manifest
from equivalent.tests.fakes import write_program

PROPERTY = "regression/property"


def _manifest(tmp_path, *, properties):
    return load_manifest(write_program(tmp_path, properties=properties) / "manifest.yaml")


def _types(requirements):
    return [req.predicate_type for req in requirements]


def test_a_code_that_declares_properties_has_to_pass_them(tmp_path):
    requirements = acceptance_requirements(_manifest(tmp_path, properties=True))

    assert PROPERTY in _types(requirements)
    assert _types(requirements)[: len(ACCEPTANCE_REQUIREMENTS)] == _types(ACCEPTANCE_REQUIREMENTS)


def test_a_code_that_declares_none_is_judged_by_the_fixed_list_alone(tmp_path):
    requirements = acceptance_requirements(_manifest(tmp_path, properties=False))

    assert requirements == ACCEPTANCE_REQUIREMENTS
    assert PROPERTY not in _types(requirements)


def test_a_reader_with_no_manifest_reads_the_fixed_list():
    # The ledger CLI can be pointed at a bare directory, where there is no
    # code to ask. Reporting the property requirement there would say a
    # region is unfinished when nothing knows whether it needs one.
    assert acceptance_requirements(None) == ACCEPTANCE_REQUIREMENTS
    assert requirements_for(PORTING) == ACCEPTANCE_REQUIREMENTS


def test_the_conditional_requirement_names_the_action_that_would_produce_it():
    (requirement,) = CONDITIONAL_REQUIREMENTS

    assert requirement.predicate_type == PROPERTY
    assert requirement.subject_kind == "tree"
    assert requirement.producing_action == "property_check"


def test_onboarding_is_unaffected_by_a_codes_properties(tmp_path):
    assert requirements_for(ONBOARDING, _manifest(tmp_path, properties=True)) == (
        ONBOARDING_REQUIREMENTS
    )


def test_an_unknown_phase_is_refused_by_name():
    with pytest.raises(ValueError) as excinfo:
        requirements_for("porting-ish")

    assert "porting-ish" in str(excinfo.value)


def test_status_shows_the_property_requirement_only_for_a_code_that_has_one(tmp_path):
    store = LedgerStore(tmp_path / "region")
    tree = Subject(kind="tree", sha256="b" * 64)

    with_properties = compute_status(
        store, requirements_for(PORTING, _manifest(tmp_path / "with", properties=True)),
        PORTING, tree=tree,
    )
    without = compute_status(
        store, requirements_for(PORTING, _manifest(tmp_path / "without", properties=False)),
        PORTING, tree=tree,
    )

    rows = [row["predicateType"] for row in with_properties["rows"]]
    assert PROPERTY in rows
    assert [row for row in with_properties["rows"] if row["predicateType"] == PROPERTY][0][
        "producing_action"
    ] == "property_check"
    assert PROPERTY not in [row["predicateType"] for row in without["rows"]]
