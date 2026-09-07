"""Property-test dataset preparation and execution."""
import os
import re
import shutil
import sys

from .case_io import _case_problem, _write_dataset
from .contract import PropertiesResponse
from .stage_config import HARNESS
from .workspace import ExecutionFailed

PROPERTIES_TIMEOUT_S = 900
PYTHON = sys.executable or "python3"

COUNT_PATTERN = re.compile(
    r"(\d+)\s+(passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b"
)


def pytest_counts(text) -> dict:
    """{passed, failed, errors} as pytest's summary reported them.

    The last figure for each word wins: pytest writes its summary at the
    end, and a failing test's own captured output can hold anything.
    """
    counts = {
        "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
        "deselected": 0, "xfailed": 0, "xpassed": 0,
    }
    for match in COUNT_PATTERN.finditer(text):
        word = match.group(2)
        counts["errors" if word.startswith("error") else word] = int(match.group(1))
    counts["collected"] = sum(
        counts[name] for name in ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")
    )
    counts["executed"] = sum(
        counts[name] for name in ("passed", "failed", "errors", "xfailed", "xpassed")
    )
    return counts


def properties(workspace, executable, module, cases, seed, max_examples,
               *, harness_dir=HARNESS, timeout=PROPERTIES_TIMEOUT_S) -> PropertiesResponse:
    """Run the code's own module of invariants against its replay binary.

    The module is a pytest file inside the tree, named by the code's
    manifest. It is run with the baked property library on PYTHONPATH and
    told, through the environment, which executable to invoke, which cases
    to draw from, where to write them, which seed to use, and how many
    examples to draw -- so the module itself names none of those.

    The seed and the example count come back with the counts pytest
    reported, because a property run is only repeatable if the claim says
    what it was: the same seed searches the same way, and a different one
    is a different search rather than a repeat.
    """
    drawn = {"seed": seed, "max_examples": max_examples}
    problem = _case_problem(cases)
    if problem is not None:
        return PropertiesResponse.failure(problem, **drawn)
    replay, identity = workspace.executable(executable)
    if replay is None:
        return PropertiesResponse.failure(
            f"the tree holds no executable '{executable}'; build it first", **drawn,
        )

    module_path = workspace.in_tree(module)
    if module_path is None or not os.path.isfile(module_path):
        where = "does not stay inside the tree" if module_path is None else "is not in the tree"
        return PropertiesResponse.failure(
            f"the properties module '{module}' {where}", **drawn,
        )

    try:
        cases_dir = _write_dataset(workspace.path("property_cases"), cases)
    except ValueError as exc:
        return PropertiesResponse.failure(str(exc), **drawn)
    scratch = workspace.path("property_scratch")
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch, exist_ok=True)

    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(harness_dir), *([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])]
        ),
        "HARNESS_REPLAY": replay,
        "HARNESS_CASES": cases_dir,
        "HARNESS_SCRATCH": scratch,
        "HARNESS_SEED": str(seed),
        "HARNESS_MAX_EXAMPLES": str(max_examples),
    }
    # -p no:cacheprovider: the tree is a submission, not a checkout, and a
    # .pytest_cache written into it would be a file nobody sent.
    command = [PYTHON, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short", module_path]
    workspace.prepare_job_files()
    audited = workspace.records_executions
    try:
        job = workspace.execute(
            command, cwd=workspace.tree_dir, env=env, timeout=timeout, gpu=True,
            mode="properties", what="the property run",
        )
    except ExecutionFailed as exc:
        return PropertiesResponse.failure(str(exc), **drawn)

    output = job.stdout + job.stderr
    counts = pytest_counts(output)
    replays_observed = None
    if audited:
        if not job.evidence or job.evidence.get("ok") is not True:
            return PropertiesResponse.failure(
                "protected replay execution evidence was unavailable", **drawn, **counts,
            )
        replay_job_path = "/job/tree/" + os.path.relpath(
            replay, workspace.tree_dir,
        ).replace(os.sep, "/")
        replays_observed = sum(
            1 for entry in job.evidence.get("executions", [])
            if isinstance(entry, dict) and entry.get("path") == replay_job_path
        )
        if replays_observed == 0:
            return PropertiesResponse.failure(
                "protected exec tracing observed no replay invocation",
                **drawn, **counts, replays_observed=0, executable_identity=identity,
            )
    if not workspace.matches(replay, identity):
        return PropertiesResponse.failure(
            "the executable changed while it was being measured",
            **drawn, **counts, executable_identity=identity,
        )
    return PropertiesResponse(
        ok=job.returncode == 0, **drawn, **counts,
        replays_observed=replays_observed,
        counts_source="pytest summary emitted by the submitted property process",
        executable_identity=identity,
        # Long enough to hold Hypothesis's minimized falsifying example,
        # which is the whole value of a failed property run.
        log_tail=output[-4000:],
    )
