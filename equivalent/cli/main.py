"""ledger: print what's in a region's ledger directory.

Reads claims.jsonl/requests.jsonl directly. Does not need the gateway
running.

Trust role: none over the ledger (it only reads), but this is what a
person looks at to judge a region, so output that misrenders a verdict
or hides a missing claim misleads the reviewer even though the ledger
itself is right.

Naming a region directory is enough for everything the ledger records on
its own. It is not enough to say which tree the region is sitting on
right now, because that lives in the gateway's git repository rather
than in the ledger; without it, `status` reports the tree of the last
claim that was filed. Passing `--config` (the gateway's own
configuration file) and `--region-id` instead reads the repository too,
and then the tree shown here is the same one the gateway's status
endpoint reports.

Both readers use the one configuration loader, so there is no second
description of where a region's files live. What differs is the machine:
the paths inside the file are the paths of whoever reads it. Run this
against a copy of the file whose paths resolve on this machine -- the
host's state directories rather than the container's mount points.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from equivalent.gateway.config import load_gateway_config
from equivalent.gateway.submit import current_tree_and_frozen
from equivalent.ledger.status import compute_history, compute_status
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject

from . import render

CONFIG_HELP = "gateway configuration file, read with --region-id to show the repository's current tree"


def _add_region_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("region_dir", nargs="?", default=None)
    parser.add_argument("--config", default=None, help=CONFIG_HELP)
    parser.add_argument("--region-id", default=None, help="which region in --config to read")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="current tree, claims present, claims missing")
    _add_region_arguments(p_status)
    p_status.add_argument("--region", default=None, help="display name (default: the directory name)")
    p_status.add_argument("--json", action="store_true")

    p_history = sub.add_parser("history", help="every tree seen, in order, with its claims")
    _add_region_arguments(p_history)
    p_history.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="one claim, full detail")
    p_show.add_argument("region_dir")
    p_show.add_argument("claim_id")

    p_requests = sub.add_parser("requests", help="the request log as a timeline")
    p_requests.add_argument("region_dir")

    return parser


def _open_region(parser: argparse.ArgumentParser, args):
    """The store to read, the current tree and frozen subjects if they are knowable, and a display name.

    The tree and frozen subjects come back as None when only a directory
    was named: the ledger alone cannot say what the region's current tree
    is, and guessing is the status computation's own documented fallback.
    """
    named_config = args.config is not None or args.region_id is not None
    if named_config and (args.config is None or args.region_id is None):
        parser.error("--config and --region-id go together; give both or neither")
    if named_config and args.region_dir is not None:
        parser.error("give either a region directory or --config with --region-id, not both")
    if not named_config and args.region_dir is None:
        parser.error("name a region directory, or --config with --region-id")

    if args.region_dir is not None:
        return LedgerStore(args.region_dir), None, None, Path(args.region_dir).name

    config = load_gateway_config(args.config)
    cfg = config.regions.get(args.region_id)
    if cfg is None:
        parser.error(f"no region '{args.region_id}' in {args.config}; it has {sorted(config.regions)}")
    store = LedgerStore(cfg.ledger_dir)
    tree_sha, frozen_sha = current_tree_and_frozen(cfg.repo_dir, cfg.region_id, store, cfg.spec_path)
    return (
        store,
        Subject(kind="tree", sha256=tree_sha),
        Subject(kind="frozen", sha256=frozen_sha),
        cfg.region_id,
    )


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        store, tree, frozen, name = _open_region(parser, args)
        status = compute_status(store, tree=tree, frozen=frozen)
        if args.json:
            print(json.dumps(status, indent=2, sort_keys=True))
        else:
            print(render.render_status(status, args.region or name), end="")
        return 0

    if args.command == "history":
        store, _, _, _ = _open_region(parser, args)
        history = compute_history(store)
        if args.json:
            print(json.dumps(history, indent=2, sort_keys=True))
        else:
            print(render.render_history(history), end="")
        return 0

    store = LedgerStore(args.region_dir)

    if args.command == "show":
        claim = store.get_claim(args.claim_id)
        if claim is None:
            print(f"no such claim: {args.claim_id}", file=sys.stderr)
            return 1
        print(render.render_claim(claim), end="")
        return 0

    if args.command == "requests":
        print(render.render_requests(store.all_requests()), end="")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
