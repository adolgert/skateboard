"""check_sese's --json contract, and a regression guard on its existing
human-readable default output -- that output is cited as real validation
evidence for a CoarseAIR region, so it must not shift underneath anyone
relying on it.

Run as a subprocess in every test here, deliberately: this is exactly how
equivalent/components/sese_check.py calls it.
"""
import json
import subprocess

MODULE = "equivalent.analyzers.check_sese"

CLEAN_SOURCE = """\
module mod_kernel
contains
subroutine step(a, b)
  real :: a, b
  a = a + b
end subroutine step
end module mod_kernel
"""

GOTO_SOURCE = """\
module mod_kernel
contains
subroutine step(a, b)
  real :: a, b
  if (h > 0) goto 10
  a = a + b
10 continue
end subroutine step
end module mod_kernel
"""

CLEAN_DIFF_SOURCE = """\
module mod_diff
contains
pure function diff(x) result(dx)
  real :: x(:), dx(size(x))
  dx = x
end function diff
end module mod_diff
"""

GOTO_DIFF_SOURCE = """\
module mod_diff
contains
function diff(x) result(dx)
  real :: x(:), dx(size(x))
  if (size(x) < 2) goto 20
  dx = x
20 continue
end function diff
end module mod_diff
"""

ONE_FILE_SPEC = """\
region: ch04:step
files:
  - src/mod_kernel.f90
anchor:
  file: src/mod_kernel.f90
  pst_node: "step@3-{hi}"
  entry_symbol: step
"""


def _write_source(tmp_path, path, source):
    full = tmp_path / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(source)


def _write_spec(tmp_path, text):
    spec = tmp_path / "region.yaml"
    spec.write_text(text)
    return spec


def _write_region(tmp_path, source, hi):
    _write_source(tmp_path, "src/mod_kernel.f90", source)
    return _write_spec(tmp_path, ONE_FILE_SPEC.format(hi=hi))


def _run(spec, repo_root, *extra):
    return subprocess.run(
        ["python3", "-m", MODULE, str(spec), "--repo-root", str(repo_root), *extra],
        capture_output=True, text=True,
    )


def _json(spec, repo_root):
    r = _run(spec, repo_root, "--json")
    return r.returncode, json.loads(r.stdout)


def test_json_pass_on_clean_control_flow(tmp_path):
    spec = _write_region(tmp_path, CLEAN_SOURCE, hi=5)

    code, body = _json(spec, tmp_path)

    assert code == 0
    assert body["verdict"] == "pass"
    assert body["violations"] == []
    assert body["src_files"] == ["src/mod_kernel.f90"]


def test_json_fail_names_the_goto(tmp_path):
    spec = _write_region(tmp_path, GOTO_SOURCE, hi=8)

    code, body = _json(spec, tmp_path)

    assert code == 1
    assert body["verdict"] == "fail"
    assert len(body["violations"]) == 1
    v = body["violations"][0]
    assert v["keyword"] == "goto"
    assert v["line"] == 5
    assert v["file"] == "src/mod_kernel.f90"


def test_a_spec_listing_two_files_reports_both(tmp_path):
    _write_source(tmp_path, "src/mod_kernel.f90", CLEAN_SOURCE)
    _write_source(tmp_path, "src/mod_diff.f90", CLEAN_DIFF_SOURCE)
    spec = _write_spec(tmp_path, """\
region: ch04:step-stencil
files:
  - src/mod_kernel.f90
  - src/mod_diff.f90
anchor:
  file: src/mod_kernel.f90
  pst_node: "step@3-5"
  entry_symbol: step
closure:
  callees:
    - name: diff
      file: src/mod_diff.f90
      lines: "3-6"
""")

    code, body = _json(spec, tmp_path)

    assert code == 0
    assert body["verdict"] == "pass"
    assert body["src_files"] == ["src/mod_diff.f90", "src/mod_kernel.f90"]
    assert body["range_count"] == 2


def test_a_file_the_region_may_create_need_not_exist_in_the_tree(tmp_path):
    # `files:` says what the region may edit. A path nothing has written
    # yet is a legitimate entry: the port may be the thing that creates it.
    _write_source(tmp_path, "src/mod_kernel.f90", CLEAN_SOURCE)
    spec = _write_spec(tmp_path, """\
region: ch04:step
files:
  - src/mod_kernel.f90
  - src/mod_stencil.f90
anchor:
  file: src/mod_kernel.f90
  pst_node: "step@3-5"
  entry_symbol: step
""")

    code, body = _json(spec, tmp_path)

    assert code == 0
    assert body["src_files"] == ["src/mod_kernel.f90", "src/mod_stencil.f90"]


def test_a_callee_in_a_second_file_with_a_goto_fails_and_names_that_file(tmp_path):
    _write_source(tmp_path, "src/mod_kernel.f90", CLEAN_SOURCE)
    _write_source(tmp_path, "src/mod_diff.f90", GOTO_DIFF_SOURCE)
    spec = _write_spec(tmp_path, """\
region: ch04:step-stencil
files:
  - src/mod_kernel.f90
  - src/mod_diff.f90
anchor:
  file: src/mod_kernel.f90
  pst_node: "step@3-5"
  entry_symbol: step
closure:
  callees:
    - name: diff
      file: src/mod_diff.f90
      lines: "3-8"
""")

    code, body = _json(spec, tmp_path)

    assert code == 1
    assert body["verdict"] == "fail"
    [violation] = body["violations"]
    assert violation["file"] == "src/mod_diff.f90"
    assert violation["line"] == 5
    assert violation["keyword"] == "goto"
    assert violation["label"] == "diff"


def test_a_callee_with_no_file_of_its_own_is_read_from_the_anchors_file(tmp_path):
    _write_source(tmp_path, "src/mod_kernel.f90", GOTO_SOURCE)
    spec = _write_spec(tmp_path, """\
region: ch04:step
files:
  - src/mod_kernel.f90
anchor:
  file: src/mod_kernel.f90
  pst_node: "step@3-4"
  entry_symbol: step
closure:
  callees:
    - name: tail
      lines: "5-8"
""")

    code, body = _json(spec, tmp_path)

    assert code == 1
    [violation] = body["violations"]
    assert violation["file"] == "src/mod_kernel.f90"
    assert violation["label"] == "tail"


def test_a_spec_with_no_files_list_is_a_failed_verdict_not_a_crash(tmp_path):
    # A malformed spec is a fact about the submission, so it comes back as
    # a verdict the caller can record, not an exception nobody can file.
    _write_source(tmp_path, "src/mod_kernel.f90", CLEAN_SOURCE)
    spec = _write_spec(tmp_path, """\
region: ch04:step
anchor:
  file: src/mod_kernel.f90
  pst_node: "step@3-5"
  entry_symbol: step
""")

    code, body = _json(spec, tmp_path)

    assert code == 1
    assert body["verdict"] == "fail"
    assert body["src_files"] == []
    [violation] = body["violations"]
    assert "files" in violation["reason"]


def test_an_anchor_file_the_spec_does_not_list_fails_and_names_it(tmp_path):
    _write_source(tmp_path, "src/mod_kernel.f90", CLEAN_SOURCE)
    _write_source(tmp_path, "src/mod_diff.f90", CLEAN_DIFF_SOURCE)
    spec = _write_spec(tmp_path, """\
region: ch04:step
files:
  - src/mod_diff.f90
anchor:
  file: src/mod_kernel.f90
  pst_node: "step@3-5"
  entry_symbol: step
""")

    code, body = _json(spec, tmp_path)

    assert code == 1
    assert body["verdict"] == "fail"
    [violation] = body["violations"]
    assert "src/mod_kernel.f90" in violation["reason"]


def test_a_callee_file_the_spec_does_not_list_fails_and_names_it(tmp_path):
    _write_source(tmp_path, "src/mod_kernel.f90", CLEAN_SOURCE)
    _write_source(tmp_path, "src/mod_diff.f90", CLEAN_DIFF_SOURCE)
    spec = _write_spec(tmp_path, """\
region: ch04:step
files:
  - src/mod_kernel.f90
anchor:
  file: src/mod_kernel.f90
  pst_node: "step@3-5"
  entry_symbol: step
closure:
  callees:
    - name: diff
      file: src/mod_diff.f90
      lines: "3-6"
""")

    code, body = _json(spec, tmp_path)

    assert code == 1
    [violation] = body["violations"]
    assert "src/mod_diff.f90" in violation["reason"]


def test_a_listed_file_that_must_be_scanned_but_is_not_there_is_a_verdict(tmp_path):
    # The anchor's own file has to exist to have line ranges in it. When it
    # does not, that is still a spec that does not describe this tree --
    # a verdict, not an infrastructure failure.
    spec = _write_spec(tmp_path, ONE_FILE_SPEC.format(hi=5))

    code, body = _json(spec, tmp_path)

    assert code == 1
    assert body["verdict"] == "fail"
    [violation] = body["violations"]
    assert "src/mod_kernel.f90" in violation["reason"]


def test_default_human_output_is_unchanged_on_pass(tmp_path):
    spec = _write_region(tmp_path, CLEAN_SOURCE, hi=5)

    r = _run(spec, tmp_path)

    assert r.returncode == 0
    assert r.stdout == (
        "VAL-1 SESE check: mod_kernel.f90, 1 ranges, 3 lines\n"
        "PASS: no goto / early return / entry / stop in region or closure\n"
    )


def test_default_human_output_is_unchanged_on_fail(tmp_path):
    spec = _write_region(tmp_path, GOTO_SOURCE, hi=8)

    r = _run(spec, tmp_path)

    assert r.returncode == 1
    assert r.stdout == (
        "VAL-1 SESE check: mod_kernel.f90, 1 ranges, 6 lines\n"
        "FAIL: 1 violation(s)\n"
        "  step:5: GOTO: if (h > 0) goto 10\n"
    )


def test_the_human_output_says_what_is_wrong_with_a_malformed_spec(tmp_path):
    _write_source(tmp_path, "src/mod_kernel.f90", CLEAN_SOURCE)
    spec = _write_spec(tmp_path, """\
region: ch04:step
anchor:
  file: src/mod_kernel.f90
  pst_node: "step@3-5"
  entry_symbol: step
""")

    r = _run(spec, tmp_path)

    assert r.returncode == 1
    assert "FAIL: 1 violation(s)" in r.stdout
    assert "files" in r.stdout
