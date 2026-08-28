"""check_sese.py's --json contract, and a regression guard on its existing
human-readable default output -- that output is cited as real validation
evidence for n4pes (notes/regions/n4_umn_pes.yaml), so it must not shift
underneath anyone relying on it.

Run as a subprocess in every test here, deliberately: this is exactly how
equivalent/components/sese_check.py calls it, and tools/regionharness is
not an importable package.
"""
import json
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = PROJECT_ROOT / "tools" / "regionharness" / "check_sese.py"

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

SPEC = """\
region: ch04:step
anchor:
  file: src/mod_kernel.f90
  pst_node: "step@3-{hi}"
  entry_symbol: step
"""


def _write_region(tmp_path, source, hi):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod_kernel.f90").write_text(source)
    spec = tmp_path / "region.yaml"
    spec.write_text(SPEC.format(hi=hi))
    return spec


def _run(spec, repo_root, *extra):
    return subprocess.run(
        ["python3", str(SCRIPT), str(spec), "--repo-root", str(repo_root), *extra],
        capture_output=True, text=True,
    )


def test_json_pass_on_clean_control_flow(tmp_path):
    spec = _write_region(tmp_path, CLEAN_SOURCE, hi=5)

    r = _run(spec, tmp_path, "--json")

    assert r.returncode == 0
    body = json.loads(r.stdout)
    assert body["verdict"] == "pass"
    assert body["violations"] == []
    assert body["src_file"] == "src/mod_kernel.f90"


def test_json_fail_names_the_goto(tmp_path):
    spec = _write_region(tmp_path, GOTO_SOURCE, hi=8)

    r = _run(spec, tmp_path, "--json")

    assert r.returncode == 1
    body = json.loads(r.stdout)
    assert body["verdict"] == "fail"
    assert len(body["violations"]) == 1
    v = body["violations"][0]
    assert v["keyword"] == "goto"
    assert v["line"] == 5


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
