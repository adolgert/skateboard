"""What the builder's directory for one set of actions is called.

Trust role: the builder keeps a workspace on disk per attempt id and
never checks a tree hash itself (services/builder/stages.py), so the name
is the whole of what says two actions ran against the same build. A check
that timed a program in one workspace while the build it describes
happened in another would be measuring a binary nobody submitted, and
nothing later in the ledger could see that. Deriving the name from what
the actions have in common -- the region, the tree, and, where two builds
of one tree exist, the strategy -- means no caller has to remember one,
and two callers cannot disagree about it.

These are the components' own, not the tree's: they name a directory on
the builder, and only a check ever asks for one. If the builder loses a
workspace (a container restart), re-running the build re-creates it under
the same name.
"""
from __future__ import annotations

import hashlib
import re


def attempt_id_for(region_id: str, tree_sha: str) -> str:
    """A stable workspace key the builder can reuse across build/run/sanitize/time.

    Deriving the id from (region, tree) means every action against the
    same tree reuses the same workspace without the gateway needing to
    remember anything extra.
    """
    safe_region = re.sub(r"[^A-Za-z0-9._-]", "-", region_id)[:48]
    region_digest = hashlib.sha256(region_id.encode("utf-8")).hexdigest()[:16]
    return f"{safe_region}-{region_digest}-{tree_sha}"


def attempt_id_for_strategy(region_id: str, tree_sha: str, strategy_name: str) -> str:
    """The workspace key for building one tree with one named strategy.

    Onboarding builds the same tree twice, once per strategy, and each
    build needs its own workspace on the builder -- two builds sharing one
    would leave the second reading the first's object files. Every later
    onboarding step that wants one of those builds derives the same key
    from the same three things rather than being handed it.
    """
    safe_strategy = re.sub(r"[^A-Za-z0-9._-]", "-", strategy_name)[:32]
    strategy_digest = hashlib.sha256(strategy_name.encode("utf-8")).hexdigest()[:12]
    return f"{attempt_id_for(region_id, tree_sha)}-{safe_strategy}-{strategy_digest}"


def attempt_id_for_tree(region_id: str, purpose: str, tree_sha: str,
                        strategy_name: str | None = None) -> str:
    """The workspace for a tree of this region's that is not its own submission.

    A region's checks build the tree that was submitted; two of them
    build something else in the same region's name -- the pristine
    baseline a port is measured against, and the reviewed original an
    onboarding is compared with. Each needs a workspace the region's own
    build will never write into, and saying what the workspace is for is
    what keeps them apart.
    """
    named = f"{region_id}-{purpose}"
    if strategy_name is None:
        return attempt_id_for(named, tree_sha)
    return attempt_id_for_strategy(named, tree_sha, strategy_name)
