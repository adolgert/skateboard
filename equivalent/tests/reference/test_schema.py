"""The reviewed contract that says what an onboarded program is compared against.

Nothing in the submitted tree may change any of this: the contract and
the snapshot beside it belong to the reviewer. So the loader refuses
anything it cannot mean, and every refusal names the section it was
reading -- the person who sees the message is the one editing the file.
"""
import pytest
import yaml

from equivalent.reference.schema import load_reference


def _contract(tmp_path, change=None):
    """A contract and the snapshot it names, with one thing changed."""
    root = tmp_path / "original"
    root.mkdir(exist_ok=True)
    (root / "Makefile").write_text("original:\n\t$(FC) kernel.f90 -o original\n")
    (root / "kernel.f90").write_text("program original\nprint *, 42\nend program\n")
    raw = {
        "version": 1,
        "provenance": "reviewed pristine upstream revision 123",
        "source": {"root": "original", "patterns": ["*.f90"]},
        "build": {"makefile": "Makefile", "target": "original", "executable": "original"},
        "runs": [{
            "name": "odd-grid",
            "original_args": ["13", "7"],
            "candidate_args": ["13", "7"],
            "outputs": [{
                "original": "answer.npy", "candidate": "field.npy",
                "comparison": "array_exact",
            }],
        }],
    }
    if change is not None:
        change(raw)
    path = tmp_path / "reference.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def test_a_contract_reads_as_the_runs_the_comparison_walks(tmp_path):
    reference = load_reference(_contract(tmp_path))

    run = reference.runs[0]
    assert run["name"] == "odd-grid"
    assert run["original_args"] == ("13", "7")
    assert run["env"] == {}
    assert run["budget_s"] == 300
    assert run["outputs"][0]["comparison"] == "array_exact"


def test_a_run_that_says_nothing_about_its_budget_gets_the_reviewed_default(tmp_path):
    reference = load_reference(_contract(tmp_path, lambda raw: raw["runs"][0].update(budget_s=12)))

    assert reference.runs[0]["budget_s"] == 12


@pytest.mark.parametrize("where, change", [
    ("source", lambda raw: raw["source"].pop("patterns")),
    ("build", lambda raw: raw["build"].update(target="-not-a-target")),
    ("run 0", lambda raw: raw["runs"][0].update(original_args="13 7")),
    ("output 0", lambda raw: raw["runs"][0]["outputs"][0].update(comparison="anything")),
])
def test_a_refusal_names_the_section_it_was_reading(tmp_path, where, change):
    with pytest.raises(ValueError, match=where):
        load_reference(_contract(tmp_path, change))


def test_a_band_on_a_comparison_that_has_no_band_is_refused(tmp_path):
    # A tolerance beside an exact comparison is a reviewer believing a
    # band applies where none does, which is exactly the mistake that
    # would let two programs disagree and still be called equivalent.
    def loosen(raw):
        raw["runs"][0]["outputs"][0]["tolerance"] = {"abs": 1.0, "rel": 0.0, "ulp": 0}

    with pytest.raises(ValueError, match="tolerance"):
        load_reference(_contract(tmp_path, loosen))
