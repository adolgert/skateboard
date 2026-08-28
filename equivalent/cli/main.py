"""ledger: print what's in a region's ledger directory.

Reads claims.jsonl/requests.jsonl directly. Does not need the gateway
running.

Trust role: none over the ledger (it only reads), but this is what a
person looks at to judge a region, so output that misrenders a verdict
or hides a missing claim misleads the reviewer even though the ledger
itself is right.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from equivalent.ledger.status import compute_history, compute_status
from equivalent.ledger.store import LedgerStore

from . import render


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="current tree, claims present, claims missing")
    p_status.add_argument("region_dir")
    p_status.add_argument("--region", default=None, help="display name (default: the directory name)")
    p_status.add_argument("--json", action="store_true")

    p_history = sub.add_parser("history", help="every tree seen, in order, with its claims")
    p_history.add_argument("region_dir")
    p_history.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="one claim, full detail")
    p_show.add_argument("region_dir")
    p_show.add_argument("claim_id")

    p_requests = sub.add_parser("requests", help="the request log as a timeline")
    p_requests.add_argument("region_dir")

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    store = LedgerStore(args.region_dir)

    if args.command == "status":
        status = compute_status(store)
        if args.json:
            print(json.dumps(status, indent=2, sort_keys=True))
        else:
            region = args.region or Path(args.region_dir).name
            print(render.render_status(status, region), end="")
        return 0

    if args.command == "history":
        history = compute_history(store)
        if args.json:
            print(json.dumps(history, indent=2, sort_keys=True))
        else:
            print(render.render_history(history), end="")
        return 0

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
