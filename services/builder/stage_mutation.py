"""Build and score generated source mutants against captured cases."""
import base64
import glob
import multiprocessing
import os
import shutil
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from . import compile_log, mutate as mutants
from .case_io import OUTPUT_SUFFIX, _case_problem, _write_case
from .contract import MutateResponse
from .stage_build import _make
from .stage_config import HARNESS
from .workspace import ExecutionFailed

MUTATE_TIMEOUT_S = 300
MUTATE_CEILING_S = 1500
MUTATE_START_METHOD = None
DEFAULT_MUTATE_JOBS = 4

def _output_arrays(case_dir) -> dict:
    """Every output file one replay left in a case directory, as arrays.

    The files say for themselves what they hold, so this reads them the
    way the oracle reads a capture: nothing here is told a variable name
    or an element type.
    """
    arrays = {}
    for path in sorted(glob.glob(os.path.join(case_dir, f"*{OUTPUT_SUFFIX}"))):
        variable = os.path.basename(path)[: -len(OUTPUT_SUFFIX)]
        arrays[variable] = np.load(path, allow_pickle=False)
    return arrays


def _write_mutated(mutant_dir, mutant) -> None:
    """The mutant's own copy of the file, with its one line changed."""
    path = os.path.join(mutant_dir, mutant.file)
    with open(path) as source:
        lines = source.read().split("\n")
    lines[mutant.line - 1] = mutant.mutated
    with open(path, "w") as out:
        out.write("\n".join(lines))


def _remaining(deadline) -> float:
    """What is left of one mutant's budget, never zero or negative."""
    return max(1.0, deadline - time.monotonic())


def score_mutant(job) -> dict:
    """Build one mutant, replay every case through it, and say what happened.

    Runs in a worker process, so everything it needs is in `job` and
    everything it answers with is in the returned row -- including the
    workspace, which carries the policy the commands are run under, since
    a worker that was started rather than forked inherits nothing from
    this module. The mutant is built through the same make invocation the
    tree was, watched the same way, and its executable is identified
    before the first case and reidentified after each, so a verdict is
    about the binary that was built. Its directory is a copy of the tree
    that already built, so `make` rebuilds only what the changed file
    forces -- and it is removed again unless the verdict is one a person
    has to read the source of.
    """
    workspace = job["workspace"]
    mutant = job["mutant"]
    mutant_dir = job["mutant_dir"]
    deadline = time.monotonic() + job["timeout"]
    try:
        shutil.rmtree(mutant_dir, ignore_errors=True)
        shutil.copytree(workspace.tree_dir, mutant_dir, symlinks=True)
        workspace.prepare_writable_job_subtree(mutant_dir)
        _write_mutated(mutant_dir, mutant)

        try:
            made = _make(
                workspace, cwd=mutant_dir, makefile=job["makefile"],
                targets=[job["target"]], env=job["env"], timeout=_remaining(deadline),
                compiler=job["compiler"], flags=job["flags"],
                source_patterns=job["source_patterns"], harness_dir=job["harness_dir"],
                log_path=job["log_path"], what="the mutant's build",
            )
        except ExecutionFailed as exc:
            mutant.status = mutants.BUILD_FAIL
            mutant.note = (
                f"the mutant's build did not finish within {job['timeout']} seconds"
                if exc.timed_out else str(exc)
            )
            return mutant.as_result()
        replay = os.path.join(mutant_dir, job["executable"])
        if made.problem is not None:
            mutant.status = mutants.BUILD_FAIL
            mutant.note = made.problem
            return mutant.as_result()
        if made.returncode != 0 or not os.path.isfile(replay):
            mutant.status = mutants.BUILD_FAIL
            mutant.note = (
                made.output.strip()[-1500:] or "make left no executable"
            )
            return mutant.as_result()
        if not compile_log.flags_reached_every_compile(made.compiles):
            # A mutant built with other flags than the port faces is not a
            # statement about this gate, so it is not scored as one.
            mutant.status = mutants.BUILD_FAIL
            mutant.note = "the strategy's flags did not reach the mutant's compiles"
            return mutant.as_result()
        identity = workspace.identity_of(replay)

        for name in job["cases"]:
            case_dir = os.path.join(mutant_dir, "cases", name)
            shutil.rmtree(case_dir, ignore_errors=True)
            shutil.copytree(os.path.join(job["inputs_root"], name), case_dir)
            workspace.prepare_writable_job_subtree(case_dir)
            try:
                replayed = workspace.execute(
                    [replay, case_dir], cwd=mutant_dir, timeout=_remaining(deadline),
                    gpu=True, what=f"the mutant's replay of case '{name}'",
                )
            except ExecutionFailed as exc:
                mutant.status = mutants.RUNTIME_FAIL
                mutant.note = (
                    f"case '{name}': the replay did not finish in time"
                    if exc.timed_out else f"case '{name}': {exc}"
                )
                return mutant.as_result()
            if replayed.returncode != 0:
                mutant.status = mutants.RUNTIME_FAIL
                mutant.note = (
                    f"case '{name}': exit {replayed.returncode}: "
                    f"{_last_line(replayed.stdout + replayed.stderr)}"
                )
                return mutant.as_result()
            if not workspace.matches(replay, identity):
                mutant.status = mutants.RUNTIME_FAIL
                mutant.note = f"case '{name}': the mutant changed while it was being measured"
                return mutant.as_result()

            try:
                expected = _output_arrays(os.path.join(job["refs_root"], name))
                got = _output_arrays(case_dir)
            except ValueError as exc:
                mutant.status = mutants.KILLED
                mutant.note = f"case '{name}': the replay wrote a file that is not an array ({exc})"
                return mutant.as_result()
            status, note = mutants.classify(expected, got, job["bands"])
            if status != mutants.EQUIVALENT:
                mutant.status, mutant.note = status, f"case '{name}': {note}"
                return mutant.as_result()

        mutant.status, mutant.note = mutants.EQUIVALENT, "no output changed in any case"
        return mutant.as_result()
    finally:
        if mutant.status not in mutants.KEEP_DIRECTORY:
            shutil.rmtree(mutant_dir, ignore_errors=True)


def _last_line(text) -> str:
    lines = [line for line in (text or "").strip().split("\n") if line.strip()]
    return lines[-1][:200] if lines else ""


def _mutation_corpus(workspace, cases) -> tuple:
    """The cases laid out on disk once: inputs to replay, outputs to score against.

    Every mutant gets its own copy of the inputs, so the directory written
    here is read and never run in. The captured outputs are written beside
    them as the files they already are -- each says for itself what it
    holds -- and are what every mutant is compared with.
    """
    # Everything an earlier mutation run of this attempt left, including
    # the directories it kept for a reader: they belong to a run whose
    # answer has already been read, and keeping them would make it look
    # as though this run had produced them.
    shutil.rmtree(workspace.path("mutants"), ignore_errors=True)
    inputs_root = workspace.path("mutants", ".inputs")
    refs_root = workspace.path("mutants", ".refs")
    for name, case in cases.items():
        _write_case(os.path.join(inputs_root, name), case.get("inputs", {}))
        reference = os.path.join(refs_root, name)
        os.makedirs(reference, exist_ok=True)
        for variable, encoded in case.get("outputs", {}).items():
            try:
                data = base64.b64decode(encoded)
            except (ValueError, TypeError):
                raise ValueError(
                    f"case '{name}' output {variable!r} did not arrive as base64 array bytes"
                ) from None
            with open(os.path.join(reference, f"{variable}{OUTPUT_SUFFIX}"), "wb") as out:
                out.write(data)
    return inputs_root, refs_root


def mutate(workspace, *, makefile, replay_target, files, cases, bands, compiler, flags,
           link_flags, source_patterns, jobs=None, limit=None,
           harness_dir=HARNESS, timeout=MUTATE_TIMEOUT_S,
           ceiling=MUTATE_CEILING_S) -> MutateResponse:
    """Score every mutant of the region's own files against the captured answers.

    This is the harness asking about itself: if a port of this region were
    wrong, would the gate notice? Each mutant is one changed token in one
    of the files the manifest says implement the region. It is built with
    the same makefile, compiler, flags and shim as the tree it came from,
    replayed on every case it is given, and compared with the captured
    outputs by the same comparator and the same bands a port is judged by
    -- so what comes back is a property of this gate as configured, not of
    a model of it.

    `replay_target` is the manifest's replay target, {"target",
    "executable"}: what `make` is asked for, and what it must leave behind.
    `cases` is {name: {"inputs": {variable: b64 npy}, "outputs": {...}}},
    the visible capture set. `bands` is the tolerance policy's band per
    output variable.

    There is no coverage prepass. gcov belongs to one compiler and the
    compiler here is whichever the strategy names, so a mutant on a line
    the cases never execute is built, run, and comes back as a survivor
    like any other. Reading the survivors is the point: some are
    equivalent code, and some are region the captured inputs never reach.

    Each result is one mutant and its verdict; the directories of the two
    verdicts a person has to read the source of are kept and named.
    """
    problem = _case_problem(cases, ("inputs", "outputs"))
    if problem is not None:
        return MutateResponse.failure(problem)
    if not os.path.isdir(workspace.tree_dir):
        return MutateResponse.failure(
            f"there is no built tree for attempt '{workspace.attempt_id}'; "
            f"build it before mutating it"
        )

    generated = []
    for relative in files:
        path = workspace.in_tree(relative)
        if path is None or not os.path.isfile(path):
            where = "does not stay inside the tree" if path is None else "is not in the tree"
            return MutateResponse.failure(f"the region file '{relative}' {where}")
        if not compile_log.is_tree_source(relative, source_patterns):
            return MutateResponse.failure(
                f"the region file '{relative}' is not one this code calls its own source, "
                f"so mutating it would say nothing about a port of it"
            )
        with open(path, errors="replace") as source:
            generated.extend(mutants.generate(source.read(), relative))

    todo = generated[: int(limit)] if limit else list(generated)
    if not todo:
        return MutateResponse(
            ok=True, generated=len(generated),
            log_tail="no mutant was generated from the region's files",
        )

    try:
        inputs_root, refs_root = _mutation_corpus(workspace, cases)
    except ValueError as exc:
        return MutateResponse.failure(str(exc))
    payloads = []
    for mutant in todo:
        # One shim log per mutant: mutants are built at the same time, and
        # a log they shared would say that some other mutant's compile was
        # this one's.
        mutant_dir = workspace.path("mutants", mutant.mid)
        # Keeping the shim log inside the worker's directory also keeps the
        # executor's minimal volume mount inside it.  A sibling must never be
        # visible to, or made writable by, this job.
        log_path = os.path.join(mutant_dir, ".compiler.jsonl")
        env = workspace.build_env(compiler, flags, link_flags, log_path, harness_dir)
        payloads.append({
            "mutant": mutant,
            "workspace": workspace,
            "mutant_dir": mutant_dir,
            "makefile": makefile,
            "target": replay_target["target"],
            "executable": replay_target["executable"],
            "env": env,
            "log_path": log_path,
            "compiler": compiler,
            "flags": list(flags),
            "source_patterns": list(source_patterns),
            "harness_dir": harness_dir,
            "inputs_root": inputs_root,
            "refs_root": refs_root,
            "cases": sorted(cases),
            "bands": bands,
            "timeout": timeout,
        })

    workers = int(jobs) if jobs else min(DEFAULT_MUTATE_JOBS, os.cpu_count() or 1)
    results = _score_all(payloads, workers, ceiling)
    counts = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return MutateResponse(
        ok=True, generated=len(generated), scored=len(results),
        results=results, counts=counts,
        kept_dirs=[
            workspace.path("mutants", row["id"]) for row in results
            if row["status"] in mutants.KEEP_DIRECTORY
        ],
    )


def _score_all(payloads, workers: int, ceiling) -> list:
    """Every mutant scored, in a pool of workers, inside one overall ceiling.

    Results come back in whatever order the workers finish; they are put
    back into the order the mutants were generated, so a reader of the
    claim walks the file from top to bottom. Anything the ceiling cut off
    is in that list too, saying so, rather than quietly absent.
    """
    by_id = {payload["mutant"].mid: payload["mutant"] for payload in payloads}
    scored = {}
    context = multiprocessing.get_context(MUTATE_START_METHOD) if MUTATE_START_METHOD else None
    pool = ProcessPoolExecutor(max_workers=max(1, workers), mp_context=context)
    deadline = time.monotonic() + ceiling
    try:
        futures = {pool.submit(score_mutant, payload): payload for payload in payloads}
        for future in list(futures):
            try:
                row = future.result(timeout=_remaining(deadline))
            except TimeoutError:
                break
            except Exception as exc:  # a worker that died is not a scored mutant
                mutant = futures[future]["mutant"]
                mutant.status, mutant.note = mutants.RUNTIME_FAIL, f"the scoring run failed: {exc}"
                row = mutant.as_result()
            scored[row["id"]] = row
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    rows = []
    for mid, mutant in by_id.items():
        if mid in scored:
            rows.append(scored[mid])
            continue
        mutant.status = mutants.SKIPPED
        mutant.note = "the mutation run reached its overall ceiling before this mutant"
        rows.append(mutant.as_result())
    return rows
