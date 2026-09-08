"""Service-stage tests explicitly opt out of the production execution policy."""
import pytest

from services.builder import stage_runtime, stages, workspace


@pytest.fixture(autouse=True)
def in_process_jobs(monkeypatch):
    """Run every stage's commands here, with none of the boundary a deployment has.

    Set for every test in this directory, so a test that forgot to say
    which policy it wanted cannot reach the disposable-container one and
    start talking to a container daemon.
    """
    monkeypatch.setattr(stage_runtime, "POLICY", workspace.InProcessJobs())


@pytest.fixture
def attempt(tmp_path):
    """One attempt's workspace, under the directory this test owns."""
    return stages.workspace_for("attempt-1", work_root=tmp_path)
