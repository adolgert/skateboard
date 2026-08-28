# Porting a region with pi: user's manual

This manual is for a person who sits at a `pi` session and ports one
region of Fortran code to the GPU with help from a model. It explains
what the pieces are, what the workflow looks like, and how to read what
comes back. It assumes the containers, the gateway, and the extension
are already installed and configured.

## Why this setup exists

When a model edits code and then reports that the edit works, you have
only the model's word for it. The model ran the compiler and the tests
itself, in its own environment, and it summarized the results for you.
If it made a mistake, or described the results too kindly, nothing in
the transcript would show that.

This setup separates doing the work from judging the work. The model
edits a working copy and can try anything it likes there, but nothing it
does locally counts as evidence. To make progress it must send its edit
to a separate service, called the gateway, which runs each check itself
and writes the result to an append-only record, called the ledger. The
gateway also refuses to run a check before the checks it depends on have
passed. When the ledger shows every required check passing on one
version of the code, the port is accepted.

The result is that you, and anyone who reviews the port later, can read
the ledger instead of trusting the transcript. Every claim in it names
the exact version of the code it applies to, the check that produced it,
and the session that asked for it.

## The pieces

**The region.** A port is done one region at a time. A region is a small
part of the program, named in a spec file, with a defined entry and
exit. The session is configured for one region.

**The working copy.** A directory that the model can edit freely. It
starts as a copy of the baseline code. Compiling or running things here
is allowed and useful for exploration, but produces no evidence.

**The gateway.** The one service the session can reach. It holds its own
copy of the code, receives submissions, runs checks, and writes the
ledger. The model cannot reach the checkers, the reference data, or the
ledger directly.

**The ledger.** One directory per region containing every claim ever
recorded and a log of every request the gateway received. Nothing in it
is ever changed or deleted. You can read it at any time with the ledger
command described near the end of this manual.

**The strategy.** A configuration file, chosen by a person before the
session, that fixes the porting approach: which files may change, which
compiler and flags are used, and which checks apply. The model cannot
change it. Every claim records which strategy was in effect.

**The allow-list.** The set of files the region is permitted to change.
Files outside it are frozen. At the start of a region only the spec file
is on the list; the first check widens it to the region's own files.

## Starting a session

Start `pi` as usual. The extension connects to the gateway, fetches the
list of available actions, and registers one tool for each, plus a
`submit` tool and a `status` tool. A short message confirms this, for
example:

    equivalent: registered 10 tools for region ch04:step

If the message reports a configuration error instead, the three
environment variables `EQUIVALENT_GATEWAY_URL`,
`EQUIVALENT_GATEWAY_TOKEN`, and `EQUIVALENT_REGION` are not all set.
That is a deployment problem, not something to fix inside the session.

You talk to the model in plain language. The model calls the tools. You
can also type `/status` at any time to see the region's current state
yourself, without asking the model.

## The checks, in order

Each check depends on earlier ones. The gateway enforces the order, so
you do not need to; if the model calls a check too early, the gateway
refuses and says what is missing. The usual sequence is:

1. **`sese_check`** — The static analyzer confirms the region described
   in the spec file has clean control flow: one way in, one way out, no
   goto, no early return. Nothing else can run until this passes. A pass
   also widens the allow-list from the spec file alone to the region's
   source files, which is what makes editing possible.

2. **`submit`** — Not a check. This sends the working copy to the
   gateway. The gateway keeps only the files on the allow-list, lays
   them over a clean copy of the baseline, and answers with two
   identifiers: the *tree*, which names this exact version of the code,
   and the *frozen* value, which names everything that was not allowed
   to change. Every later claim is attached to one of these. The answer
   also warns about files that were sent but ignored, and about allowed
   files that were not sent.

3. **`build_replay`** — The gateway's builder compiles the submitted
   tree with the strategy's compiler flags and links it with a test
   harness. The claim records the exact flags used.

4. **`run_replay`** — The built program runs on recorded inputs, on the
   GPU. The gateway counts kernel launches. A program that runs but
   launches no GPU kernels fails this check, even if its output is
   correct, because the point of the port is that the work happens on
   the GPU.

5. **`sanitize`** — The GPU memory and race checkers run over the
   program. This produces three results at once: memcheck, racecheck,
   and initcheck. The first two are required for acceptance; initcheck
   is recorded for information.

6. **`regression_visible`** — The outputs from the run are compared
   against reference outputs, for the set of test cases the session is
   allowed to see. The result includes a per-case breakdown.

7. **`regression_holdout`** — The same comparison against a second set
   of test cases that the session never sees. The answer is pass or fail
   only, with no detail, so a port cannot be tuned to the held-out
   cases. The full comparison stays in the ledger for a person to read.

8. **`time_port`** — The ported program is timed. This is the last gate
   before acceptance.

9. **`time_baseline`** — The original, unmodified program is timed for
   comparison. This has no prerequisites and can run at any point; once
   per region is enough, because it measures the baseline, not the port.

Both timing actions accept an optional `repeats` setting that controls
how many timed runs are made.

When every required check has passed on one tree, `status` reports
`ACCEPTED` for that tree. There is no separate accept action; acceptance
is a fact about the ledger, not a step someone performs.

## Editing and re-checking

Porting is rarely one pass. The normal loop is: the model edits the
working copy, submits, and re-runs the checks that the edit invalidated.

The rules for what an edit invalidates are simple. Every check except
`sese_check` is attached to the tree, so any submitted change to the
code means those checks must run again on the new tree. `sese_check` is
attached to the frozen value instead, so it survives edits to the
region's own files and does not need to be repeated after each change.

If the model asks for a check that already ran on the same tree with the
same settings, the gateway returns the recorded result instead of
running the check again. This is by design: repeating an identical
request cannot produce a fresh chance at a different verdict.

## Reading what comes back

**A pass or fail** looks like this in the transcript:

    build_replay: pass (c-0007)

The identifier in parentheses is the claim id. You can look up the full
record for any claim id in the ledger.

**A refusal** means the check's prerequisites are not met. It is a
normal answer, not an error, and it says exactly what to do:

    refused: 'build_replay' requires:
      - sese/verified is missing; run sese_check to produce it.

The model reads this and is expected to run the missing check. If the
prerequisite exists but failed, the refusal names the failing claim, so
you can tell "never ran" apart from "ran and failed".

**A failure** is a real verdict: the check ran and the code did not meet
it. The detail explains why, for example the compiler log for a build
failure or the list of violations for a control-flow failure. The fix is
to edit, submit, and run the check again. Failed claims stay in the
ledger; they are part of the history, not something to erase.

**An error** means the check itself could not run, for example because
the builder service is not reachable. No claim is recorded for an error.
Errors are infrastructure problems for the person to fix, not something
the model can edit its way around.

## Watching from outside the session

You do not have to go through the model to see where things stand. The
`/status` command inside `pi` prints the current tree, each required
check with its verdict and claim id, each missing check with the action
that would produce it, and whether the region is accepted.

From a shell with access to the ledger directory, the ledger command
reads the same records directly:

    python -m equivalent.cli.main status   <ledger-dir>   # current state
    python -m equivalent.cli.main history  <ledger-dir>   # every tree, with its claims
    python -m equivalent.cli.main show     <ledger-dir> c-0007   # one claim, full detail
    python -m equivalent.cli.main requests <ledger-dir>   # every request, as a timeline

`show` always prints the complete record, including detail that was
withheld from the session, because the ledger is for people. The
request log includes refused and repeated requests, each tagged with the
session that made it, so you can reconstruct exactly what a session
asked for and when.

## Things worth knowing

- Work done in the working copy is invisible to the gateway until it is
  submitted. If the model reports success but `status` shows checks
  missing, the evidence does not exist yet.
- Only files on the allow-list reach the gateway. The submit answer
  names anything that was sent and ignored. If an intended change keeps
  being ignored, the file is outside the allow-list, which usually means
  the change does not belong in this region.
- The gateway's builder keeps its compiled work between checks. If the
  builder is restarted between a build and a later check, that check may
  report that the binary is missing. Running `build_replay` again
  rebuilds it; nothing is lost, because the code itself is in the
  gateway's repository.
- Timing runs are not repeatable in the way other checks are. Asking to
  time again always runs again, and the newest measurement is the one
  that counts.
