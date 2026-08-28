#!/usr/bin/env python3
"""Writes the host's view of the gateway's configuration file.

Trust role: this decides which repository and which ledger the reading
tools on this machine open. It does not change what the gateway checks --
the gateway reads its own file -- but a wrong entry here points the
person at a different deployment from the one the agent is working in,
so what they review is not what happened.

The gateway's file names container mount points; the same deployment seen
from the host names directories. Only the values under `paths:` differ
between the two, so only those are rewritten, from a mapping of mount
point to host directory that the caller supplies. Everything else --
the version, every region, every field of every region -- is carried
across as it was read, so the two files cannot drift apart by being
typed twice.

    python3 deploy/hostconfig.py <gateway.yaml> <out.yaml> \\
        --mount /repo=/abs/state/repo --mount /ledger=/abs/state/ledger ... \\
        --sessions /abs/state/sessions
"""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml

HEADER = (
    "# Written by up.sh. The gateway reads deploy/gateway.yaml, in the container's\n"
    "# terms; this is the same deployment seen from this machine. Edit gateway.yaml\n"
    "# and re-run up.sh rather than editing this file.\n"
)

# Where the agent's session transcripts land. The gateway has no mount for
# them -- it never reads them -- so this key exists only in the host's copy,
# where the tools that join a ledger to a transcript look for it.
SESSIONS_KEY = "sessions"


def host_paths(container_paths: dict, mounts: dict) -> dict:
    """The `paths` block with each container mount point replaced by its host directory.

    A value with no entry in `mounts` is an error: silently leaving it as
    a container path would produce a file that looks usable and names a
    directory that does not exist here.
    """
    rewritten = {}
    for key, value in container_paths.items():
        if value not in mounts:
            raise ValueError(
                f"paths.{key} is {value!r}, which is not one of the mount points "
                f"this deployment knows: {sorted(mounts)}"
            )
        rewritten[key] = str(mounts[value])
    return rewritten


def host_config_text(gateway_yaml_path, mounts: dict, sessions_dir) -> str:
    """The full text of the host's copy, header comment included."""
    config = yaml.safe_load(Path(gateway_yaml_path).read_text())
    config["paths"] = {
        **host_paths(config["paths"], mounts),
        SESSIONS_KEY: str(sessions_dir),
    }
    return HEADER + yaml.safe_dump(config, sort_keys=False, default_flow_style=False)


def write_host_config(gateway_yaml_path, out_path, mounts: dict, sessions_dir) -> str:
    text = host_config_text(gateway_yaml_path, mounts, sessions_dir)
    Path(out_path).write_text(text)
    return text


def _mount(argument: str) -> tuple:
    mount_point, separator, host_dir = argument.partition("=")
    if not separator or not mount_point or not host_dir:
        raise argparse.ArgumentTypeError(
            f"--mount wants <container path>=<host path>, not {argument!r}"
        )
    return mount_point, host_dir


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="hostconfig",
        description="write the host's copy of the gateway configuration file",
    )
    parser.add_argument("gateway_yaml", help="the gateway's own configuration file")
    parser.add_argument("out_path", help="the file to write")
    parser.add_argument(
        "--mount", action="append", default=[], type=_mount, metavar="CONTAINER=HOST",
        help="one container mount point and the host directory behind it; repeatable",
    )
    parser.add_argument(
        "--sessions", required=True, help="the directory the session transcripts are written to",
    )
    args = parser.parse_args(argv)

    write_host_config(args.gateway_yaml, args.out_path, dict(args.mount), args.sessions)
    print(args.out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
