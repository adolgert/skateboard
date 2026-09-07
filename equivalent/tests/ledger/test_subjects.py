import hashlib

import pytest

from equivalent.ledger.subjects import (
    Subject,
    binary_subject,
    evidence_policy_subject,
    hash_bytes,
    hash_files,
    outputs_subject,
    strategy_subject,
    tree_subject,
)


def _files():
    return [
        {"path": "src/mod_a.f90", "content": "module a\nend module a\n"},
        {"path": "src/mod_b.f90", "content": "module b\nend module b\n"},
    ]


def test_hash_files_is_order_independent():
    forward = hash_files(_files())
    backward = hash_files(list(reversed(_files())))
    assert forward == backward


def test_hash_files_changes_with_one_byte():
    original = hash_files(_files())
    mutated = _files()
    mutated[1]["content"] = mutated[1]["content"].replace("end", "End")
    assert hash_files(mutated) != original


def test_normalization_strips_a_dot_slash_prefix_but_not_leading_dots():
    # lstrip("./") would collide ".gitignore" with "gitignore" -- two
    # distinct trees, one hash, which is exactly what this module must
    # never do.
    assert hash_files([{"path": ".gitignore", "content": "x\n"}]) != hash_files(
        [{"path": "gitignore", "content": "x\n"}]
    )
    assert hash_files([{"path": "./src/mod_a.f90", "content": "y\n"}]) == hash_files(
        [{"path": "src/mod_a.f90", "content": "y\n"}]
    )


def test_hash_files_frames_paths_and_contents_unambiguously():
    # Both sets fed exactly b"abc" to the legacy concatenation scheme.
    assert hash_files([{"path": "a", "content": "bc"}]) != hash_files(
        [{"path": "ab", "content": "c"}]
    )


def test_hash_files_rejects_duplicate_normalized_paths():
    with pytest.raises(ValueError, match="duplicate normalized paths"):
        hash_files([
            {"path": "src/a.f90", "content": "first"},
            {"path": "./src/a.f90", "content": "second"},
        ])


def test_strategy_subject_reproduces_oracle_policy_sha_scheme():
    # services/oracle/app.py's POLICY_SHA: plain sha256 of the raw file bytes.
    data = b'{"policy_version": 1, "variables": {}}'
    expected = hashlib.sha256(data).hexdigest()
    assert hash_bytes(data) == expected
    assert strategy_subject(data).sha256 == expected


def test_binary_subject_and_outputs_subject_are_deterministic():
    data = b"\x00\x01\x02binary-bytes"
    assert binary_subject(data).sha256 == binary_subject(data).sha256

    cases = {"case0000": {"field": b"aaa", "flux": b"bbb"},
             "case0001": {"field": b"AAA", "flux": b"BBB"}}
    a = outputs_subject(cases)
    b = outputs_subject(dict(reversed(list(cases.items()))))
    assert a.sha256 == b.sha256


def test_tree_subject_has_kind_tree():
    s = tree_subject(_files())
    assert s.kind == "tree"
    assert len(s.sha256) == 64


def test_evidence_policy_identifies_installed_trusted_code():
    first = evidence_policy_subject()
    assert first.kind == "evidence_policy"
    assert len(first.sha256) == 64
    assert evidence_policy_subject() == first


def test_subject_rejects_unknown_kind():
    with pytest.raises(ValueError):
        Subject(kind="not_a_kind", sha256="a" * 64)


@pytest.mark.parametrize("digest", ["abc", "A" * 64, "g" * 64, None])
def test_subject_rejects_malformed_sha256(digest):
    with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
        Subject(kind="tree", sha256=digest)


def test_subject_round_trip_dict():
    s = tree_subject(_files())
    assert Subject.from_dict(s.to_dict()) == s
