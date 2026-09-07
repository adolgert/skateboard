"""Real CPU experiment on a third code, including a seeded anisotropy error.

This test's runner executes only this repository-owned fixture. Production
candidate execution uses the disposable builder; this is a comparison-contract
integration test, not evidence about sandbox or GPU operation.
"""
import base64
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from equivalent.components import original_check
from equivalent.gateway.submit import attempt_id_for_strategy, init_baseline_repo
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.strategy.schema import Language, load_strategy
from equivalent.tests.fakes import in_tree_manifest, write_tree

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "heat_step.f90"
STRATEGY = Path(__file__).resolve().parents[2] / "strategy" / "files" / "cpu_reference.yaml"


class FixtureRunner:
    def __init__(self, root):
        self.root = root

    def build(self, attempt_id, tree, makefile, targets, compiler, flags, link_flags, source_patterns):
        workspace = self.root / attempt_id
        workspace.mkdir(parents=True)
        for entry in tree:
            path = workspace / entry["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(entry["b64"]))
        subprocess.run([compiler, *flags, "heat.f90", "-o", targets[0]["executable"]],
                       cwd=workspace, check=True, capture_output=True, timeout=30)
        return {"ok": True, "flags_reached_every_compile": True, "compiled_only_tree_source": True}

    def time(self, attempt_id, executable, args, env, outputs, repeats, budget_s):
        workspace = self.root / attempt_id
        result = []
        for _ in range(repeats):
            subprocess.run([str(workspace / executable), *args], cwd=workspace, check=True,
                           capture_output=True, timeout=budget_s)
            result.append({o: base64.b64encode((workspace / o).read_bytes()).decode() for o in outputs})
        return {"ok": True, "runs_s": [0.01] * repeats, "outputs": result}


@pytest.mark.skipif(shutil.which("gfortran") is None, reason="requires a host gfortran compiler")
@pytest.mark.parametrize("wrong", [False, True], ids=["unchanged-program", "wrong-grid-spacing"])
def test_actual_fortran_original_and_onboarded_program(tmp_path, wrong):
    original = tmp_path / "original"
    original.mkdir()
    source = FIXTURE.read_bytes()
    (original / "heat.f90").write_bytes(source)
    (original / "Makefile").write_text("heat:\n\t$(FC) $(FFLAGS) heat.f90 -o heat\n")
    contract = tmp_path / "original.yaml"
    contract.write_text(yaml.safe_dump({
        "version": 1, "provenance": "repository-owned finite-difference fixture before onboarding",
        "source": {"root": "original", "patterns": ["*.f90"]},
        "build": {"makefile": "Makefile", "target": "heat", "executable": "heat"},
        "runs": [{"name": f"grid-{nx}-{ny}", "original_args": [str(nx), str(ny)],
                  "candidate_args": [str(nx), str(ny)], "outputs": [
                      {"original": "answer.txt", "candidate": "answer.txt", "comparison": "bytes"}]}
                 for nx, ny in ((13, 7), (4, 11))],
    }))
    strategy = replace(load_strategy(STRATEGY),
                       languages={"fortran": Language("gfortran", ("-O0", "-fcheck=all"))})
    builder = FixtureRunner(tmp_path / "jobs")
    candidate = source.replace(b"/dy**2", b"/dx**2") if wrong else source
    attempt = attempt_id_for_strategy("heat:onboard", "a" * 64, strategy.name)
    builder.build(attempt, [{"path": "heat.f90", "b64": base64.b64encode(candidate).decode()}],
                  "Makefile", [{"executable": "whole_program"}], "gfortran", ["-O0", "-fcheck=all"], [], ["*.f90"])
    repo = tmp_path / "repo"
    init_baseline_repo(repo, write_tree(tmp_path / "seed", in_tree_manifest()))
    result = original_check.check(LedgerStore(tmp_path / "ledger"), Subject("tree", "a" * 64),
                                  repo, "main", "heat:onboard", "a" * 64, strategy, builder, contract)
    assert result["verdict"] == ("fail" if wrong else "pass")
    assert len(result["detail"]["runs"]) == 2
    assert all(run["pass"] is (not wrong) for run in result["detail"]["runs"])
