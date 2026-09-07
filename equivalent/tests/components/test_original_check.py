"""Onboarding must agree with a reference it did not create itself."""
import base64
from pathlib import Path

import numpy as np
import pytest
import yaml

from equivalent.capture import npy
from equivalent.components import original_check
from equivalent.gateway.submit import init_baseline_repo
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.reference.schema import load_reference, fingerprint_reference
from equivalent.strategy.schema import load_strategy
from equivalent.tests.fakes import FakeBuilder, write_tree

STRATEGIES = Path(__file__).resolve().parents[2] / "strategy" / "files"


def reference(tmp_path):
    root = tmp_path / "original"
    root.mkdir()
    (root / "Makefile").write_text("original:\n\t$(FC) $(FFLAGS) kernel.f90 -o original\n")
    (root / "kernel.f90").write_text("program original\nprint *, 42\nend program\n")
    path = tmp_path / "reference.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1, "provenance": "reviewed pristine upstream revision 123",
        "source": {"root": "original", "patterns": ["*.f90"]},
        "build": {"makefile": "Makefile", "target": "original", "executable": "original"},
        "runs": [{"name": "odd-grid", "original_args": ["13", "7"],
                  "candidate_args": ["13", "7"], "outputs": [
                      {"original": "answer.npy", "candidate": "field.npy", "comparison": "array_exact"}]}],
    }))
    return path


class ReferenceBuilder(FakeBuilder):
    """Both programs may be internally repeatable while disagreeing with one another."""
    wrong_candidate = False
    drift = False
    incomplete = False

    def time(self, attempt_id, executable, args, env, outputs, repeats=5, budget_s=300):
        original = "-original-" in attempt_id
        value = 42.0 if original or not self.wrong_candidate else -42.0
        written = [{name: base64.b64encode(npy.encode(np.array([value + (i if self.drift else 0)]))).decode()
                    for name in outputs} for i in range(repeats)]
        if self.incomplete:
            written = written[:1]
        return {"ok": True, "outputs": written, "runs_s": [0.1] * len(written)}


def check(tmp_path, builder, path):
    repo = tmp_path / "repo"
    init_baseline_repo(repo, write_tree(tmp_path / "seed"))
    return original_check.check(LedgerStore(tmp_path / "ledger"), Subject("tree", "a" * 64),
                                repo, "main", "new:onboard", "a" * 64,
                                load_strategy(STRATEGIES / "cpu_reference.yaml"), builder, path)


def test_independent_original_comparison_records_outputs(tmp_path):
    result = check(tmp_path, ReferenceBuilder(), reference(tmp_path))
    assert result["verdict"] == "pass"
    compared = result["detail"]["runs"][0]["outputs"][0]
    assert compared["original_artifacts"] == compared["candidate_artifacts"]
    assert result["detail"]["reference_sha256"]


def test_self_consistent_but_wrong_onboarded_program_fails(tmp_path):
    builder = ReferenceBuilder()
    builder.wrong_candidate = True
    result = check(tmp_path, builder, reference(tmp_path))
    assert result["verdict"] == "fail"
    comparison = result["detail"]["runs"][0]["outputs"][0]
    assert comparison["deterministic"] is True
    assert comparison["comparison_result"]["pass"] is False


@pytest.mark.parametrize("failure", ["drift", "incomplete"])
def test_nonrepeatable_or_incomplete_reference_fails(tmp_path, failure):
    builder = ReferenceBuilder()
    setattr(builder, failure, True)
    assert check(tmp_path, builder, reference(tmp_path))["verdict"] == "fail"


def test_no_reference_cannot_establish_onboarding(tmp_path):
    result = check(tmp_path, ReferenceBuilder(), None)
    assert result["verdict"] == "fail"
    assert "original_reference" in result["detail"]["problems"][0]


def test_reference_identity_covers_contract_and_source(tmp_path):
    path = reference(tmp_path)
    before = fingerprint_reference(path)
    source = tmp_path / "original" / "kernel.f90"
    source.write_text(source.read_text().replace("42", "43"))
    after_source = fingerprint_reference(path)
    assert after_source != before
    path.write_text(path.read_text().replace("13", "15"))
    assert fingerprint_reference(path) != after_source


def test_reference_rejects_source_links(tmp_path):
    path = reference(tmp_path)
    (tmp_path / "original" / "extra.f90").symlink_to(tmp_path / "original" / "kernel.f90")
    with pytest.raises(ValueError, match="symlink"):
        load_reference(path)


@pytest.mark.parametrize("change", [
    lambda raw: raw.update(runs=[]),
    lambda raw: raw["runs"][0].update(outputs=[]),
    lambda raw: raw["runs"][0].update(budget_s=0),
    lambda raw: raw["runs"][0]["outputs"][0].update(candidate="../escape"),
    lambda raw: raw["runs"][0]["outputs"][0].update(comparison="anything"),
])
def test_incomplete_or_unsafe_contract_is_rejected(tmp_path, change):
    path = reference(tmp_path)
    raw = yaml.safe_load(path.read_text())
    change(raw)
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError):
        load_reference(path)


def test_nan_cannot_establish_original_agreement():
    data = npy.encode(np.array([np.nan]))
    assert not original_check._comparison(data, data, {"comparison": "array_exact"})["pass"]
