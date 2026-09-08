"""Compute Sanitizer execution and summary parsing."""
import re

from .case_io import _case_problem, _write_case
from .contract import SanitizeResponse
from .workspace import ExecutionFailed

SANITIZE_TIMEOUT_S = 600

SANITIZER_SUMMARY = re.compile(
    r"^=+ (?:ERROR SUMMARY: (\d+) errors?"
    r"|RACECHECK SUMMARY: \d+ hazards? displayed \((\d+) errors?, \d+ warnings?\))",
    re.MULTILINE,
)


def sanitizer_errors(output: str) -> int | None:
    """How many errors the tool's own summary counts, or None if it wrote none.

    The count is read from the summary line rather than by looking for
    the word ERROR in the log: the summary line itself contains that
    word, so a clean run would otherwise count as one error. A tool that
    wrote no summary did not finish, and the caller treats that as a
    failure rather than as zero errors.
    """
    total = None
    for match in SANITIZER_SUMMARY.finditer(output):
        total = (total or 0) + int(match.group(1) or match.group(2))
    return total


def sanitize(workspace, executable, cases, tools, *, timeout=SANITIZE_TIMEOUT_S) -> SanitizeResponse:
    """Run every tool over every case, against the manifest's replay executable.

    The caller chooses how many cases to send; whether that is one or all
    of them is the strategy's decision, not this file's. There is still
    one entry per tool in the response: the error counts are summed over
    the cases and a tool fails if it failed on any of them, so a caller
    that asks for more cases gets a stricter verdict, not more verdicts.
    """
    problem = _case_problem(cases)
    if problem is not None:
        return SanitizeResponse.failure(problem)
    replay, identity = workspace.executable(executable)
    if replay is None:
        return SanitizeResponse.failure(
            f"the tree holds no executable '{executable}'; build it first"
        )

    per_tool = {}
    for tool in tools:
        errors = 0
        failed = False
        failing_log = ""
        last_log = ""
        unavailable = None
        for name, arrs in cases.items():
            try:
                cdir = _write_case(workspace.path("san", name), arrs)
            except ValueError as exc:
                failed = True
                failing_log = f"case '{name}': {exc}"
                break
            cmd = ["compute-sanitizer", "--tool", tool, "--error-exitcode", "1", replay, cdir]
            workspace.prepare_job_files()
            try:
                job = workspace.execute(
                    cmd, cwd=workspace.tree_dir, timeout=timeout, gpu=True,
                    what=f"the {tool} sanitizer on case '{name}'",
                )
            except ExecutionFailed as exc:
                # A tool that could not be run at all did not pass and did
                # not fail; the gateway is told which of the two it is.
                if exc.unavailable:
                    unavailable = str(exc)
                    break
                failed = True
                failing_log = str(exc)
                break
            output = job.stdout + job.stderr
            counted = sanitizer_errors(output)
            if counted is None:
                failed = True
                failing_log = f"the {tool} sanitizer wrote no summary line: {output[-1500:]}"
                break
            errors += counted
            if not workspace.matches(replay, identity):
                failed = True
                failing_log = "the executable changed while it was being measured"
                break
            last_log = output[-1500:]
            if job.returncode != 0 and not failed:
                failed = True
                failing_log = last_log
        if unavailable is not None:
            per_tool[tool] = {"ok": None, "error": unavailable}
        else:
            # The log of the first case that failed, so the reader sees the
            # failure rather than whatever the last case happened to print.
            per_tool[tool] = {"ok": not failed, "errors": errors, "log_tail": failing_log or last_log}
    return SanitizeResponse(
        ok=bool(per_tool) and all(t.get("ok") is True for t in per_tool.values()),
        per_tool=per_tool, executable_identity=identity,
    )
