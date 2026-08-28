import hashlib

import pytest

from equivalent.ledger.subjects import (
    Subject,
    binary_subject,
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


def test_hash_files_reproduces_demo_src_sha_scheme():
    # The first demonstration harness's src_sha(): sha256 over sorted-by-path
    # (path, content) pairs, path bytes then content bytes, no separator,
    # truncated to 12 hex chars.
    def demo_src_sha(files):
        h = hashlib.sha256()
        for f in sorted(files, key=lambda x: x["path"]):
            h.update(f["path"].encode())
            h.update(f["content"].encode())
        return h.hexdigest()[:12]

    files = _files()
    assert hash_files(files)[:12] == demo_src_sha(files)


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


def test_subject_rejects_unknown_kind():
    with pytest.raises(ValueError):
        Subject(kind="not_a_kind", sha256="a" * 64)


def test_subject_round_trip_dict():
    s = tree_subject(_files())
    assert Subject.from_dict(s.to_dict()) == s
