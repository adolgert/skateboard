#!/usr/bin/env python3
"""Brings one code in from a bare baseline, against the running gateway.

Trust role: none. It plays the part an onboarding session's model would
play, holding the same token the agent holds and able to do nothing the
agent could not do. Everything it prints came back from the gateway.
What it is for is to answer, in one run, whether a deployment can take a
code that has only a name and a source tree all the way to ONBOARDED --
the eight checks, in order, each one's evidence coming from the one
before it.

The part it plays is deliberately small. The tsunami baseline already
holds the makefile, the replay driver, the capture program, and the
tolerance policy that a real onboarding session would have to write; the
one thing this writes is the manifest that names them, taken from the
code's own checked-in manifest so that what is submitted is a file a
person reviewed.

It stops at the manifest for the same reason: promoting is a person's
step. The last thing it prints is the command that person runs.

It runs inside a container on the agent's network, because the gateway
is reachable only from there.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from equivalent.cli.promote import BASELINE_DIR, MANIFEST_NAME, in_tree_manifest_text
from equivalent.client import connect
from equivalent.manifest.schema import IN_TREE_MANIFEST

# The eight checks, in the order each one's evidence is produced. The
# gateway refuses any of them asked for early, so the order is not a
# convenience here: it is the shape of the session.
CHECKS = (
    "manifest_check",
    "harness_build",
    "harness_capture",
    "harness_replay",
    "harness_determinism",
    "harness_timing",
    "harness_self_check",
    "harness_property",
)


class Surprised(Exception):
    """The gateway answered something this walkthrough did not expect."""


def heading(number: int, text: str) -> None:
    print(f"\n[{number}] {text}", flush=True)


def expect(condition, message: str) -> None:
    if not condition:
        raise Surprised(message)


def print_status(status: dict) -> None:
    print(f"    tree      {status['tree']}")
    for row in status["rows"]:
        detail = row.get("verdict", "")
        print(f"    {row['status']:8} {row['predicateType']}{' ' + detail if detail else ''}")
    print(f"    accepted  {status['accepted']}")


def reset_to_baseline(working: Path, baseline: Path) -> tuple:
    """Make the working copy be the code's bare baseline, and say what changed.

    An onboarding session starts from the tree as it is checked in and
    nothing else, so this both lays the baseline down and removes
    whatever else was in the working copy -- a previous session's port,
    a region spec from a porting walkthrough. The working copy is shared
    with the agent's own sessions, so what went is printed.
    """
    wanted = {
        path.relative_to(baseline): path
        for path in sorted(baseline.rglob("*")) if path.is_file()
    }
    removed = []
    for path in sorted(working.rglob("*")):
        if path.is_file() and path.relative_to(working) not in wanted:
            path.unlink()
            removed.append(str(path.relative_to(working)))
    for relative, source in wanted.items():
        destination = working / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return sorted(wanted), removed


def walk(client, region: str, code: str, working: Path, programs: Path, config: str) -> None:
    code_dir = programs / code

    heading(1, f"status, before anything has been submitted for {region}")
    print_status(client.status(region))

    heading(2, f"the working copy starts as the bare baseline of {code}")
    laid, removed = reset_to_baseline(working, code_dir / BASELINE_DIR)
    print(f"    baseline  {len(laid)} files")
    for path in removed:
        print(f"    removed   {path}")

    heading(3, f"write the manifest the tree carries while it is being brought in ({IN_TREE_MANIFEST})")
    manifest = working / IN_TREE_MANIFEST
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(in_tree_manifest_text((code_dir / MANIFEST_NAME).read_text()))
    print(f"    wrote     {IN_TREE_MANIFEST}, saying its source root is the tree itself")

    heading(4, "submit the whole tree")
    receipt = client.submit(region)
    print(f"    tree      {receipt['tree']}")
    print(f"    committed {receipt['committed']}")
    expect(
        not receipt["rejected"],
        f"the onboarding strategy turned files away: {receipt['rejected']}",
    )

    heading(5, "the eight checks, in the order their evidence is produced")
    for action in CHECKS:
        body = client.run(action, region)
        if body.get("refused"):
            missing = ", ".join(item["predicateType"] for item in body["missing"])
            raise Surprised(f"{action} was refused, missing {missing}")
        if "error" in body:
            raise Surprised(f"{action} answered an error: {body['error']}")
        print(f"    {action}: {body.get('verdict')} ({body.get('claim_id')})")
        expect(body.get("verdict") == "pass", f"{action} did not pass")

    heading(6, "status again: every onboarding requirement met")
    status = client.status(region)
    print_status(status)
    expect(status["accepted"], "the code is not onboarded after every check passed")

    heading(7, "what a person does next, after reading the claims")
    print(f"    ledger status --config {config} --region-id {region}")
    print(f"    ledger promote --config {config} --region-id {region}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="onboard_walkthrough",
        description="bring one code in from a bare baseline against a running gateway",
    )
    parser.add_argument("--url", default=os.environ.get("EQUIVALENT_GATEWAY_URL", "http://gateway:8000"))
    parser.add_argument("--token", default=os.environ.get("EQUIVALENT_TOKEN", ""))
    parser.add_argument("--region", default="tsunami:onboard", help="the onboarding region to drive")
    parser.add_argument("--code", default=os.environ.get("EQUIVALENT_CODE", "tsunami"))
    parser.add_argument("--working", default="/working", help="the agent's working copy")
    parser.add_argument("--programs", default="/programs", help="the tree holding one directory per code")
    parser.add_argument(
        "--config", default="deploy/state/gateway.host.yaml",
        help="the host's own copy of the configuration, named in the commands printed at the end",
    )
    parser.add_argument("--session-id", default="onboard-walkthrough")
    parser.add_argument("--model-id", default="none")
    args = parser.parse_args(argv)

    if not args.token:
        print("no token: pass --token or set EQUIVALENT_TOKEN", file=sys.stderr)
        return 2

    client = connect(args.url, args.token, args.session_id, args.model_id)
    try:
        walk(client, args.region, args.code, Path(args.working), Path(args.programs), args.config)
    except Surprised as surprise:
        print(f"\nSTOPPED: {surprise}", file=sys.stderr)
        return 1

    print(f"\nregion {args.region} reached ONBOARDED; promoting is the person's step")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
