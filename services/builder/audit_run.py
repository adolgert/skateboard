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


def _argv(line: str):
    match = EXEC.search(line.strip())
    if not match:
        return None
    try:
        path = ast.literal_eval('"' + match.group(1) + '"')
        return {"path": path, "argv": list(ast.literal_eval("[" + match.group(2) + "]"))}
    except (ValueError, SyntaxError):
        return None


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "--" or len(argv) == 1:
        return 64
    directory = "/run/evidence"
    os.chmod(directory, 0o700)
    trace = os.path.join(directory, "exec.trace")
    command = [
        "strace", "-qq", "-f", "-s", "131072", "-e", "trace=execve",
        "-o", trace, "/usr/bin/setpriv", "--reuid=65532", "--regid=65532",
        "--clear-groups", "--", *argv[1:],
    ]
    ran = subprocess.run(command, capture_output=True, text=True)
    sys.stdout.write(ran.stdout)
    sys.stderr.write(ran.stderr)
    executions = []
    if os.path.exists(trace):
        with open(trace, encoding="utf-8", errors="replace") as source:
            executions = [parsed for line in source if (parsed := _argv(line))]
    with open(os.path.join(directory, "result.json"), "w", encoding="utf-8") as out:
        json.dump({"ok": os.path.exists(trace), "executions": executions}, out)
    return ran.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
