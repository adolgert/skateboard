"""Every call a check makes to a backend, made the same way each time.

Trust role: a check answers with a verdict about the code, and a backend
that could not be reached has said nothing about the code at all. Every
call here turns a transport failure into a ComponentError for exactly
that reason: read as a verdict it would file a claim saying a port is
wrong when all that was wrong was a request. One rule, one sentence, so
that no check can be lenient about it by accident.

The calls are here rather than in each check because more than one check
makes most of them -- the same driver is run by a port's check and by the
onboarding check that qualifies it, and the same capture program by the
check that stores a dataset and the one that asks for it again. What
comes back means different things to each of them, and each of them
judges it; how it is asked for is one thing.

The client is passed in rather than the context: a check hands over the
backend it was given, and nothing here reads anything else about the
request.

The one call that does not follow the rule is the held-out comparison in
regression.py, which withholds the diagnostic instead of repeating it:
during a held-out run the words a backend puts in an error are
influenced by the submitted code.
"""
from __future__ import annotations

from .answers import Builder, Oracle
from .errors import ComponentError


def _asked(who: str, endpoint: str, call, *args, **kwargs):
    """One backend call, or the error that it never happened."""
    try:
        return call(*args, **kwargs)
    except Exception as exc:
        raise ComponentError(f"{who} /v1/{endpoint} call failed: {exc}") from exc


def build(builder: Builder, attempt_id: str, tree: list, makefile: str, targets: list,
          compiler: str, flags, link_flags, source_patterns):
    """Build one tree with its own makefile, under one strategy's compiler."""
    return _asked(
        "builder", "build", builder.build, attempt_id, tree, makefile, targets,
        compiler, list(flags), list(link_flags), list(source_patterns),
    )


def replay(builder: Builder, attempt_id: str, executable: str, cases: dict,
           *, notify=None, mandatory: bool = False):
    """Run the replay driver over a set of cases in one builder workspace.

    `notify` and `mandatory` are the strategy's device proof, asked for
    only where whether anything offloaded is the question: a harness
    replay is two halves of one CPU harness agreeing.
    """
    return _asked(
        "builder", "run", builder.run, attempt_id, executable, cases,
        notify=notify, mandatory=mandatory,
    )


def capture(builder: Builder, attempt_id: str, executable: str, args, dataset: str):
    """Run the capture program for one dataset, into a directory of its own."""
    return _asked(
        "builder", "capture", builder.capture, attempt_id, executable, list(args), dataset,
    )


def sanitize(builder: Builder, attempt_id: str, executable: str, cases: dict, tools):
    """Run each named sanitizer over each case."""
    return _asked(
        "builder", "sanitize", builder.sanitize, attempt_id, executable, cases, list(tools),
    )


def properties(builder: Builder, attempt_id: str, executable: str, module: str,
               cases: dict, seed: int, max_examples: int):
    """Run the code's own module of invariants against its replay binary."""
    return _asked(
        "builder", "properties", builder.properties, attempt_id, executable, module,
        cases, seed, max_examples,
    )


def mutate(builder: Builder, attempt_id: str, makefile: str, replay_target: dict, files,
           cases: dict, bands: dict, compiler: str, flags, link_flags, source_patterns,
           *, limit=None):
    """Mutate the region's own files and score each mutant against the captures."""
    return _asked(
        "builder", "mutate", builder.mutate, attempt_id, makefile, replay_target,
        list(files), cases, bands, compiler, list(flags), list(link_flags),
        list(source_patterns), limit=limit,
    )


def time(builder: Builder, attempt_id: str, executable: str, args, env: dict, outputs,
         repeats: int, budget_s: int):
    """Time the code's own program and collect the files it declares."""
    return _asked(
        "builder", "time", builder.time, attempt_id, executable, list(args), dict(env),
        list(outputs), repeats, budget_s,
    )


def compare(oracle: Oracle, dataset: str, outputs: dict):
    """Ask the oracle to judge one dataset's outputs against its own answers."""
    return _asked("oracle", "compare", oracle.compare, dataset=dataset, outputs=outputs)
