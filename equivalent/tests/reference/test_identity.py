"""What the reviewed original's own digest covers, and what it refuses.

The comparison a claim records is against one reference, named by a
digest -- so the digest has to change when anything the comparison would
read changes: the contract, and the source it snapshots. A reference that
could be edited without changing its name would let a later claim read as
a comparison against something that was never compared.
"""
from __future__ import annotations

import pytest
import yaml

from equivalent.reference.schema import fingerprint_reference, load_reference


def _reference(tmp_path):
    """A reviewed contract and the program snapshot it names."""
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
                      {"original": "answer.npy", "candidate": "field.npy",
                       "comparison": "array_exact"}]}],
    }))
    return path


def test_reference_identity_covers_contract_and_source(tmp_path):
    path = _reference(tmp_path)
    before = fingerprint_reference(path)
    source = tmp_path / "original" / "kernel.f90"
    source.write_text(source.read_text().replace("42", "43"))
    after_source = fingerprint_reference(path)
    assert after_source != before
    path.write_text(path.read_text().replace("13", "15"))
    assert fingerprint_reference(path) != after_source


def test_reference_rejects_source_links(tmp_path):
    path = _reference(tmp_path)
    (tmp_path / "original" / "extra.f90").symlink_to(tmp_path / "original" / "kernel.f90")
    with pytest.raises(ValueError, match="symlink"):
        load_reference(path)


@pytest.mark.parametrize("change", [
    lambda raw: raw.update(runs=[]),
    lambda raw: raw["runs"][0].update(outputs=[]),
    lambda raw: raw["runs"][0].update(budget_s=0),
    lambda raw: raw["runs"][0]["outputs"][0].update(candidate="../escape"),
])
def test_incomplete_or_unsafe_contract_is_rejected(tmp_path, change):
    path = _reference(tmp_path)
    raw = yaml.safe_load(path.read_text())
    change(raw)
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError):
        load_reference(path)
