"""Reading the files git tracks at one ref, on behalf of every check that judges them.

A tree is what a claim is about, so what comes back here has to be the
submitted files byte for byte, and the manifest that describes them has
to be the one the tree carries.
"""
import base64
import json
from pathlib import Path

import pytest

import equivalent.tree
from equivalent.gateway.submit import init_baseline_repo
from equivalent.manifest.schema import IN_TREE_MANIFEST
from equivalent.tests.fakes import write_tree
from equivalent.tree import Tree, attempt_id_for

# Fixed by the unambiguous v2 file-set serialization.  Legacy evidence used a
# different hash and is intentionally invalid under evidence policy v2.
TEXT_BASELINE_TREE = "3bfd503c5274ffb2387534ad956f632f1a3d5630848b884f53ceeb2c2dc03361"

# A file in an encoding that is not UTF-8. A real code's tree has one --
# a namelist written on another machine, reference data next to the source.
LATIN1_BYTES = "! coefficient d'entrée\n".encode("latin-1")


def _write(root, path, content):
    p = Path(root) / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))


def _seed(root):
    _write(root, "src/mod_kernel.f90", "subroutine step\nend subroutine\n")
    _write(root, "Makefile", "all:\n\techo build\n")
    return root


def _repo(tmp_path, extra=None):
    seed = _seed(tmp_path / "seed")
    for path, content in (extra or {}).items():
        _write(seed, path, content)
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, seed)
    return repo_dir


def _onboarding_repo(tmp_path, drop_manifest: bool = False):
    seed = write_tree(tmp_path / "seed")
    if drop_manifest:
        (seed / IN_TREE_MANIFEST).unlink()
    repo = tmp_path / "repo"
    init_baseline_repo(repo, seed)
    return repo


def test_the_files_are_the_ones_the_baseline_holds(tmp_path):
    tree = Tree.baseline(_repo(tmp_path))

    assert tree.files == {
        "src/mod_kernel.f90": b"subroutine step\nend subroutine\n",
        "Makefile": b"all:\n\techo build\n",
    }


def test_an_all_text_baseline_hash_is_stable_under_v2_serialization(tmp_path):
    assert Tree.baseline(_repo(tmp_path)).sha == TEXT_BASELINE_TREE


def test_the_whole_tree_is_handed_to_the_builder_as_bytes(tmp_path):
    # The builder builds the tree with the tree's own makefile, which may
    # read a namelist or a data file no extension test would recognize --
    # so everything tracked goes, base64 because the request is JSON and
    # a real code's tree is not all UTF-8.
    files = Tree.baseline(_repo(tmp_path)).payload()

    assert [f["path"] for f in files] == ["Makefile", "src/mod_kernel.f90"]
    assert base64.b64decode(files[1]["b64"]) == b"subroutine step\nend subroutine\n"


def test_a_file_the_manifest_does_not_call_source_is_sent_anyway(tmp_path):
    # Which files the code calls source decides what the builder may
    # compile, not what it is given: a README costs nothing to carry, and
    # guessing wrong about a build input costs a build.
    repo_dir = _repo(tmp_path, {"README.md": "how to build this\n"})

    files = Tree.baseline(repo_dir).payload()

    assert "README.md" in [f["path"] for f in files]


def test_a_file_that_is_not_utf8_travels_unchanged(tmp_path):
    repo_dir = _repo(tmp_path, {"src/legacy.f90": LATIN1_BYTES})

    files = {f["path"]: base64.b64decode(f["b64"]) for f in Tree.baseline(repo_dir).payload()}

    assert files["src/legacy.f90"] == LATIN1_BYTES


def test_a_materialized_tree_holds_the_bytes_the_tree_holds(tmp_path):
    repo_dir = _repo(tmp_path, {"data/coeffs.nml": LATIN1_BYTES})

    with Tree.baseline(repo_dir).materialized() as scratch:
        assert (Path(scratch) / "data" / "coeffs.nml").read_bytes() == LATIN1_BYTES
        assert (Path(scratch) / "src" / "mod_kernel.f90").read_bytes() == (
            b"subroutine step\nend subroutine\n"
        )


def test_the_repository_is_read_once_however_often_the_files_are_asked_for(monkeypatch, tmp_path):
    # Reading a tree costs a git process per file. A check reads the
    # manifest, then the payload, then materializes the tree; walking the
    # repository once per question would be that cost several times over
    # for files that cannot have changed.
    tree = Tree.baseline(_repo(tmp_path))
    real_read = equivalent.tree._git_bytes
    calls = []

    def counted(*args, **kwargs):
        calls.append(args)
        return real_read(*args, **kwargs)

    monkeypatch.setattr(equivalent.tree, "_git_bytes", counted)

    first = tree.files
    reads = len(calls)
    assert tree.files is first
    assert tree.sha
    tree.payload()
    with tree.materialized():
        pass

    assert reads > 0
    assert len(calls) == reads


def test_the_manifest_the_tree_carries_is_what_comes_back(tmp_path):
    manifest = Tree.baseline(_onboarding_repo(tmp_path)).manifest()

    assert manifest.name == "tsunami"
    assert manifest.complete
    assert sorted(manifest.build.targets) == ["capture", "replay", "timing"]


def test_a_tree_whose_manifest_will_not_load_says_so(tmp_path):
    tree = Tree.baseline(_onboarding_repo(tmp_path, drop_manifest=True))

    with pytest.raises(ValueError) as caught:
        tree.manifest()

    assert IN_TREE_MANIFEST in str(caught.value)


def test_the_policy_the_manifest_names_is_read_from_the_same_copy_of_the_tree(tmp_path):
    tree = Tree.baseline(_onboarding_repo(tmp_path))

    manifest, policy_bytes = tree.manifest_and_policy()

    assert json.loads(policy_bytes)
    # The path the manifest carries still points at the policy after the
    # call that loaded it: the tree's copy of itself lives as long as the
    # tree does, so a caller holding one can go back to it.
    assert Path(manifest.tolerances).read_bytes() == policy_bytes


def test_attempt_ids_bind_the_full_tree_and_unambiguous_region_identity():
    common_prefix = "a" * 63
    first_tree = common_prefix + "1"
    second_tree = common_prefix + "2"

    first = attempt_id_for("code:a/b", first_tree)
    second = attempt_id_for("code:a?b", first_tree)

    assert first_tree in first
    assert attempt_id_for("code:a/b", second_tree) != first
    # Both ids have the same filesystem-safe spelling of the region, so
    # their region digest is what keeps their builder workspaces distinct.
    assert second != first
