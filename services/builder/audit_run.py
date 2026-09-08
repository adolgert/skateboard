#!/usr/bin/env python3
"""Run a submitted build under a root-owned exec observer.

The submitted Makefile runs as uid 65532 and cannot read or replace the trace.
The supervisor uses this record instead of the agent-writable compiler shim log.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys


EXEC = re.compile(r'execve\("((?:[^"\\]|\\.)*)", \[(.*?)\], .*\)\s+=\s+0$')
EXEC_PATH = re.compile(r'execve\("((?:[^"\\]|\\.)*)"')
EXECVEAT_PATH = re.compile(r'execveat\([^,]+,\s*"((?:[^"\\]|\\.)*)"')
PREFIX = re.compile(r"^(?:\[pid\s+)?(\d+)\]?\s+")
CHDIR = re.compile(r'chdir\("((?:[^"\\]|\\.)*)"\)\s+=\s+0$')
FCHDIR = re.compile(r'fchdir\(\d+<((?:[^>\\]|\\.)*)>\)\s+=\s+0$')
FORK = re.compile(r"(?:clone|clone3|fork|vfork)\(.*\)\s+=\s+(\d+)$")
UNFINISHED = re.compile(r"^(\w+)\(.*<unfinished \.\.\.>$")
RESUMED = re.compile(r"^<\.\.\. (\w+) resumed>\)(.*)$")
RETURNED_PID = re.compile(r"=\s+(\d+)\s*$")


def _argv(line: str):
    match = EXEC.search(line.strip())
    if not match:
        return None
    try:
        path = ast.literal_eval('"' + match.group(1) + '"')
        return {"path": path, "argv": list(ast.literal_eval("[" + match.group(2) + "]"))}
    except (ValueError, SyntaxError):
        return None


def _literal(value: str):
    try:
        return ast.literal_eval('"' + value + '"')
    except (ValueError, SyntaxError):
        return None


def _unparsed_execution(event: str):
    """Identify a successful exec whose argv could not be represented.

    Dropping it would make a second, readable compiler invocation sufficient
    to hide an unaudited one. ``execveat`` is identified and refused here too:
    its directory-fd path semantics need more evidence than this collector
    currently records.
    """
    if re.search(r"\)\s+=\s+0$", event) is None:
        return None
    match = EXEC_PATH.search(event) or EXECVEAT_PATH.search(event)
    if match is None:
        return None
    path = _literal(match.group(1))
    if path is None:
        return None
    kind = "execveat" if event.startswith("execveat(") else "execve"
    return {
        "path": path,
        "argv": None,
        "audit_error": f"{kind} arguments could not be audited",
    }


def parse_trace(lines, initial_cwd: str) -> list[dict]:
    """Executions with the directory and parent process observed by strace.

    Make recipes commonly change directory before invoking a compiler. Tracking
    process creation and successful chdir calls keeps source paths attributable
    to the directory in which the compiler actually saw them. ``-yy`` annotates
    fchdir descriptors with their resolved path, which covers build tools that
    use directory file descriptors rather than chdir.
    """
    cwd_by_pid = {}
    parent_by_pid = {}
    changed_cwd = set()
    pending = {}
    executions = []
    for line in lines:
        prefix = PREFIX.match(line)
        if prefix is None:
            continue
        pid = int(prefix.group(1))
        event = line[prefix.end():].strip()
        cwd_by_pid.setdefault(pid, initial_cwd)

        resumed = RESUMED.match(event)
        if resumed is not None:
            syscall = resumed.group(1)
            unfinished = pending.pop(pid, None)
            if unfinished is None or unfinished[0] != syscall:
                continue
            if syscall in {"execve", "execveat"}:
                # The opening record contains the complete path and argv. The
                # resumed record supplies the closing parenthesis and result.
                event = unfinished[1].removesuffix("<unfinished ...>")
                event += ")" + resumed.group(2)
            elif syscall in {"clone", "clone3", "fork", "vfork"}:
                returned = RETURNED_PID.search(resumed.group(2))
                if returned is not None:
                    child = int(returned.group(1))
                    if child not in changed_cwd:
                        cwd_by_pid[child] = cwd_by_pid[pid]
                    parent_by_pid[child] = pid
                    for execution in executions:
                        if execution.get("pid") != child:
                            continue
                        execution["ppid"] = pid
                        if child not in changed_cwd:
                            execution["cwd"] = cwd_by_pid[pid]
                        else:
                            execution.setdefault(
                                "audit_error",
                                "working directory changed before process ancestry was observed",
                            )
                continue
            else:
                continue

        unfinished = UNFINISHED.match(event)
        if unfinished is not None:
            pending[pid] = (unfinished.group(1), event)
            continue

        fork = FORK.search(event)
        if fork is not None:
            child = int(fork.group(1))
            cwd_by_pid[child] = cwd_by_pid[pid]
            parent_by_pid[child] = pid
            continue

        changed = CHDIR.search(event)
        if changed is not None:
            destination = _literal(changed.group(1))
            if destination is not None:
                if not os.path.isabs(destination):
                    destination = os.path.join(cwd_by_pid[pid], destination)
                cwd_by_pid[pid] = os.path.normpath(destination)
                changed_cwd.add(pid)
            continue

        changed_fd = FCHDIR.search(event)
        if changed_fd is not None:
            destination = _literal(changed_fd.group(1))
            if destination is not None:
                cwd_by_pid[pid] = os.path.normpath(destination)
                changed_cwd.add(pid)
            continue

        execution = _argv(event)
        if execution is None:
            execution = _unparsed_execution(event)
        if execution is not None:
            executions.append({
                **execution,
                "cwd": cwd_by_pid[pid],
                "pid": pid,
                "ppid": parent_by_pid.get(pid),
            })
    return executions


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "--" or len(argv) == 1:
        return 64
    directory = "/run/evidence"
    os.chmod(directory, 0o700)
    trace = os.path.join(directory, "exec.trace")
    initial_cwd = os.getcwd()
    command = [
        "strace", "-qq", "-f", "-yy", "-s", "131072", "-e",
        "trace=%process,chdir,fchdir",
        "-o", trace, "/usr/bin/setpriv", "--reuid=65532", "--regid=65532",
        "--clear-groups", "--", *argv[1:],
    ]
    ran = subprocess.run(command, capture_output=True, text=True)
    sys.stdout.write(ran.stdout)
    sys.stderr.write(ran.stderr)
    executions = []
    if os.path.exists(trace):
        with open(trace, encoding="utf-8", errors="replace") as source:
            executions = parse_trace(source, initial_cwd)
    with open(os.path.join(directory, "result.json"), "w", encoding="utf-8") as out:
        json.dump({
            "ok": os.path.exists(trace), "initial_cwd": initial_cwd,
            "executions": executions,
        }, out)
    return ran.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
