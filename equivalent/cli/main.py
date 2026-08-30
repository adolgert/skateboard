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

`session` needs the configuration file for a second reason: the agent's
own transcripts are written somewhere the ledger directory does not
name, and the configuration file is where that directory is written down
once for every tool that reads it.

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
from equivalent.ledger.acceptance import PORTING, requirements_for
from equivalent.ledger.status import compute_history, compute_status
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.strategy.schema import load_strategy

from . import promote as promote_module
from . import render, session

CONFIG_HELP = "gateway configuration file, read with --region-id to show the repository's current tree"
SESSION_CONFIG_HELP = "gateway configuration file; it names both the ledger and the sessions directory"


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

    p_promote = sub.add_parser(
        "promote",
        help="copy what an onboarding session proved into the code's own directory",
    )
    p_promote.add_argument("--config", required=True, help=CONFIG_HELP)
    p_promote.add_argument(
        "--region-id", required=True, help="which onboarding region in --config to promote",
    )
    p_promote.add_argument(
        "--programs", default=None,
        help="where the code's directory is written (default: the config's paths.programs)",
    )
    p_promote.add_argument(
        "--replace", action="store_true",
        help="empty the destinations that already hold something, rather than refusing",
    )

    p_session = sub.add_parser(
        "session", help="one agent session beside the request log it produced",
    )
    p_session.add_argument("session_id", help="the session to read, as the request log spells it")
    p_session.add_argument("--config", required=True, help=SESSION_CONFIG_HELP)
    p_session.add_argument("--region-id", required=True, help="which region in --config to read")
    p_session.add_argument("--json", action="store_true")

    return parser


def _named_region(parser: argparse.ArgumentParser, config_path, region_id):
    """The one region a --config/--region-id pair names, and the whole configuration around it."""
    config = load_gateway_config(config_path)
    cfg = config.regions.get(region_id)
    if cfg is None:
        parser.error(f"no region '{region_id}' in {config_path}; it has {sorted(config.regions)}")
    return config, cfg


def _open_region(parser: argparse.ArgumentParser, args):
    """The store, the current tree and frozen subjects if knowable, the phase, a display name, and the code.

    The tree and frozen subjects come back as None when only a directory
    was named: the ledger alone cannot say what the region's current tree
    is, and guessing is the status computation's own documented fallback.
    A directory named on its own says nothing about the phase either, and
    a ledger directory holding a port's claims is by far the common case,
    so that reading is what a bare directory gets. It says nothing about
    the code either, so there is no manifest to read the property
    requirement out of, and the fixed acceptance list is what it is judged
    by.
    """
    named_config = args.config is not None or args.region_id is not None
    if named_config and (args.config is None or args.region_id is None):
        parser.error("--config and --region-id go together; give both or neither")
    if named_config and args.region_dir is not None:
        parser.error("give either a region directory or --config with --region-id, not both")
    if not named_config and args.region_dir is None:
        parser.error("name a region directory, or --config with --region-id")

    if args.region_dir is not None:
        return LedgerStore(args.region_dir), None, None, PORTING, Path(args.region_dir).name, None

    _, cfg = _named_region(parser, args.config, args.region_id)
    store = LedgerStore(cfg.ledger_dir)
    tree_sha, frozen_sha = current_tree_and_frozen(
        cfg.repo_dir, cfg.region_id, store, cfg.spec_path, cfg.phase,
        load_strategy(cfg.strategy_path),
    )
    return (
        store,
        Subject(kind="tree", sha256=tree_sha),
        Subject(kind="frozen", sha256=frozen_sha),
        cfg.phase,
        cfg.region_id,
        cfg.manifest,
    )


def _run_session(parser: argparse.ArgumentParser, args) -> int:
    """Print one session's timeline and summary.

    A missing transcript is not an error. The gateway's own log is the
    half that matters, and a session driven from the command line rather
    than by an agent leaves no transcript at all -- so the timeline is
    printed one-sided, with the first line saying which half is absent.
    """
    config, cfg = _named_region(parser, args.config, args.region_id)
    if config.paths.sessions is None:
        print("this deployment names no sessions directory", file=sys.stderr)
        return 1

    store = LedgerStore(cfg.ledger_dir)
    requests = [line for line in store.all_requests() if line.session == args.session_id]

    path = session.find_session_file(config.paths.sessions, args.session_id)
    events = []
    note = None
    if path is None:
        note = (
            f"no session file for {args.session_id} in {config.paths.sessions}; "
            f"showing the request log alone"
        )
    else:
        _, _, events = session.read_session(path)

    joined = session.join(events, requests, session.claim_verdicts(store))
    summary = session.summarize(store, args.session_id, requests, events, joined, cfg.phase)

    if args.json:
        print(json.dumps({
            "note": note,
            "timeline": [row.to_dict() for row in joined.rows],
            "unmatched_calls": [event.to_dict() for event in joined.unmatched_calls],
            "unmatched_requests": [line.to_dict() for line in joined.unmatched_requests],
            "summary": summary.to_dict(),
        }, indent=2, sort_keys=True))
        return 0

    if note is not None:
        print(note)
    print(render.render_timeline(joined), end="")
    print()
    print(render.render_session_summary(summary), end="")
    return 0


def _run_promote(parser: argparse.ArgumentParser, args) -> int:
    """Promote one onboarding region, or say why it was refused.

    A refusal is the expected answer to a session that is not finished or
    a working copy that has moved on, so it is a message and a non-zero
    exit rather than a traceback.
    """
    config, cfg = _named_region(parser, args.config, args.region_id)
    try:
        lines = promote_module.promote(
            config, cfg, programs=args.programs, replace=args.replace,
        )
    except promote_module.PromoteRefused as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        store, tree, frozen, phase, name, manifest = _open_region(parser, args)
        status = compute_status(
            store, requirements_for(phase, manifest), phase, tree=tree, frozen=frozen,
        )
        if args.json:
            print(json.dumps(status, indent=2, sort_keys=True))
        else:
            print(render.render_status(status, args.region or name), end="")
        return 0

    if args.command == "history":
        store, _, _, _, _, _ = _open_region(parser, args)
        history = compute_history(store)
        if args.json:
            print(json.dumps(history, indent=2, sort_keys=True))
        else:
            print(render.render_history(history), end="")
        return 0

    if args.command == "promote":
        return _run_promote(parser, args)

    if args.command == "session":
        return _run_session(parser, args)

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
