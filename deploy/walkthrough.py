#!/usr/bin/env python3
"""Drives one region from nothing to accepted, against the running gateway.

Trust role: none. It is a person's own client, holding the same token the
agent holds, and it can do nothing the agent could not do. Everything it
prints came back from the gateway; it decides nothing itself. What it is
for is to answer, in one run, whether a freshly started deployment
actually works end to end -- the refusal, the analyzer, the builder, the
oracle, the timing, and a failing attempt landing as a claim rather than
an error.

It is a runner, not a test suite: the steps are in order, each one prints
what it did, and the first surprise stops it with a non-zero exit.

It runs inside a container on the agent's network, because the gateway is
reachable only from there.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from equivalent.client import connect

SPEC_PATH = "notes/regions/ch04-step.sese.yaml"
KERNEL_PATH = "src/mod_kernel.f90"
# Where a code keeps its checked-in region specs, under its own directory
# in the mounted programs tree. The file is named for the region, with the
# colon that a region id carries replaced by a dash -- the same spelling
# the gateway uses for a region's branch and its ledger directory.
REGIONS_DIR = "regions"
SPEC_SUFFIX = ".sese.yaml"

# A port that passes every gate. This is the attempt that was accepted in the
# recorded campaign: it materializes the mass flux before differencing it, so
# the index pairing the physics depends on cannot drift.
GOOD_KERNEL = "sonnet-attempt02-mod_kernel.f90"

# A port that does not compile, chosen over the other recorded failures on
# purpose. The alternatives fail later and less cleanly: the codestral attempts
# compile without a single warning and fail on the device-proof or on the
# physics, which makes them a poor illustration of a build claim. This one
# scopes its wrap-around temporaries in a `block` inside `do concurrent`, which
# the GPU-offload compiler rejects outright -- so the failure is a build
# verdict, recorded as a claim with an id, and not an infrastructure error.
BAD_KERNEL = "sonnet-attempt01-mod_kernel.f90"

# In order, after the region has a passing analyzer verdict and a submitted
# port. Each one's preconditions are the claims the ones before it filed.
GATES = (
    "build_replay",
    "run_replay",
    "sanitize",
    "regression_visible",
    "regression_holdout",
    "time_port",
    "time_baseline",
)


class Surprised(Exception):
    """The gateway answered something this walkthrough did not expect."""


def heading(number: int, text: str) -> None:
    print(f"\n[{number}] {text}", flush=True)


def expect(condition, message: str) -> None:
    if not condition:
        raise Surprised(message)


def verdicts(body: dict) -> list[tuple[str, str, str]]:
    """(predicate type, verdict, claim id) for a /run answer that filed claims."""
    if "claims" in body:
        return [(c["predicateType"], c["verdict"], c["claim_id"]) for c in body["claims"]]
    if "verdict" in body:
        return [("", body["verdict"], body["claim_id"])]
    return []


def report(action: str, body: dict) -> list[tuple[str, str, str]]:
    if body.get("refused"):
        missing = ", ".join(item["predicateType"] for item in body["missing"])
        print(f"    {action}: refused, missing {missing}")
        return []
    if "error" in body:
        print(f"    {action}: error {body['error']}")
        return []
    filed = verdicts(body)
    for predicate_type, verdict, claim_id in filed:
        label = predicate_type or action
        print(f"    {action}: {label} {verdict} ({claim_id})")
    return filed


def print_status(status: dict) -> None:
    print(f"    tree      {status['tree']}")
    print(f"    frozen    {status['frozen']}")
    for row in status["rows"]:
        detail = row.get("verdict", "")
        print(f"    {row['status']:8} {row['predicateType']}{' ' + detail if detail else ''}")
    print(f"    accepted  {status['accepted']}")


def spec_source(programs: Path, code: str, region: str) -> Path:
    """The checked-in region spec this walkthrough lays into the working copy.

    It is copied rather than written from a template here so that what a
    session analyzes is the file a person reviewed, not a second copy of
    it that can drift -- an inline template is exactly how the two came
    to disagree about the region's line range before.
    """
    return programs / code / REGIONS_DIR / f"{region.replace(':', '-')}{SPEC_SUFFIX}"


def walk(client, region: str, working: Path, examples: Path, spec: Path) -> None:
    heading(1, "status, before this walkthrough has submitted anything")
    status = client.status(region)
    print_status(status)
    fresh = not any(
        row["predicateType"] == "sese/verified" and row["status"] == "present"
        for row in status["rows"]
    )

    heading(2, "a gate asked for too early is refused, not run")
    if fresh:
        body = client.run("build_replay", region)
        report("build_replay", body)
        expect(body.get("refused"), "build_replay ran without any evidence behind it")
        expect(
            any(item["predicateType"] == "sese/verified" for item in body["missing"]),
            "the refusal did not name the analyzer verdict as what is missing",
        )
    else:
        # The region already has an analyzer verdict, so this gate would
        # not be premature. The rest still runs: a repeated deterministic
        # check comes back as the claim already filed, and the timing
        # checks run again.
        print("    skipped: the region already has evidence; start from down.sh --reset to see the refusal")

    heading(3, f"copy the region spec into the working copy and submit it ({spec.name})")
    spec_file = working / SPEC_PATH
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(spec, spec_file)
    receipt = client.submit(region)
    print(f"    tree      {receipt['tree']}")
    print(f"    frozen    {receipt['frozen']}")
    print(f"    committed {receipt['committed']}")

    heading(4, "the analyzer runs and widens the region's allow-list")
    body = client.run("sese_check", region)
    report("sese_check", body)
    expect(body.get("verdict") == "pass", "the analyzer did not pass on the baseline kernel")

    heading(5, f"submit a port that should pass every gate ({GOOD_KERNEL})")
    shutil.copyfile(examples / GOOD_KERNEL, working / KERNEL_PATH)
    receipt = client.submit(region)
    print(f"    tree      {receipt['tree']}")
    for rejected in receipt["rejected"]:
        print(f"    ignored   {rejected['path']} ({rejected['reason']})")
    for action in GATES:
        body = client.run(action, region)
        filed = report(action, body)
        expect(filed, f"{action} filed no claim")
        for _, verdict, _ in filed:
            expect(verdict == "pass", f"{action} did not pass")

    heading(6, "status again: every requirement met")
    status = client.status(region)
    print_status(status)
    expect(status["accepted"], "the region is not accepted after every gate passed")

    heading(7, f"a port that does not compile is a claim, not an error ({BAD_KERNEL})")
    shutil.copyfile(examples / BAD_KERNEL, working / KERNEL_PATH)
    receipt = client.submit(region)
    print(f"    tree      {receipt['tree']}")
    body = client.run("build_replay", region)
    filed = report("build_replay", body)
    expect(filed, "the failed build produced no claim")
    predicate_type, verdict, claim_id = filed[0]
    expect(verdict == "fail", f"the build reported {verdict}, not a failure")
    expect(bool(claim_id), "the failed build's claim has no id")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="walkthrough", description="drive one region end to end against a running gateway",
    )
    parser.add_argument("--url", default=os.environ.get("EQUIVALENT_GATEWAY_URL", "http://gateway:8000"))
    parser.add_argument("--token", default=os.environ.get("EQUIVALENT_TOKEN", ""))
    parser.add_argument("--region", default=os.environ.get("EQUIVALENT_REGION", "ch04:step"))
    parser.add_argument("--code", default=os.environ.get("EQUIVALENT_CODE", "tsunami"))
    parser.add_argument("--working", default="/working", help="the agent's working copy")
    parser.add_argument("--programs", default="/programs", help="the tree holding one directory per code")
    parser.add_argument("--examples", default="/examples", help="directory holding the recorded ports")
    parser.add_argument("--session-id", default="walkthrough")
    parser.add_argument("--model-id", default="none")
    args = parser.parse_args(argv)

    if not args.token:
        print("no token: pass --token or set EQUIVALENT_TOKEN", file=sys.stderr)
        return 2

    spec = spec_source(Path(args.programs), args.code, args.region)
    if not spec.is_file():
        print(f"no region spec at {spec}", file=sys.stderr)
        return 2

    client = connect(args.url, args.token, args.session_id, args.model_id)
    try:
        walk(client, args.region, Path(args.working), Path(args.examples), spec)
    except Surprised as surprise:
        print(f"\nSTOPPED: {surprise}", file=sys.stderr)
        return 1

    print(f"\nall seven steps behaved as expected for region {args.region}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
