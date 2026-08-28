# Porting a region with pi: user's manual

This manual is for a person who sits at a `pi` session and ports one
region of Fortran code to the GPU with help from a model. It explains
what the pieces are, what the session looks like from start to
acceptance, and how to read what comes back. It assumes the services are
running and you are logged in; `pi-install.md` covers that.

## Why this setup exists

When a model edits code and then reports that the edit works, you have
only the model's word for it. The model ran the compiler and the tests
itself, in its own environment, and it summarized the results for you.
If it made a mistake, or described the results too kindly, nothing in
the transcript would show that.

This setup separates doing the work from judging the work. The model
edits a working copy and can try anything it likes there — it has the
same compilers and the same GPU — but nothing it does locally counts as
evidence. To make progress it must submit its edit to a separate
service, the gateway, which runs each check itself and writes the result
to an append-only record, the ledger. The gateway also refuses to run a
check before the checks it depends on have passed. When the ledger shows
every required check passing on one version of the code, the port is
accepted.

The result is that you, and anyone who reviews the port later, can read
the ledger instead of trusting the transcript. Every claim in it names
the exact version of the code it applies to, the check that produced it,
the strategy that was in force, and the session that asked for it. And
because the gateway also logs every request with the id of the tool call
that made it, the transcript and the ledger can be laid side by side
afterwards, call by call.

## The pieces

**The region.** A port is done one region at a time. A region is a small
part of the program with one way in and one way out, named in a spec
file. The session is configured for one region; today that is
`ch04:step`, the time-step routine of a shallow-water solver.

**The working copy.** `/working` inside the session: a directory the
model can edit freely, which starts as a copy of the baseline code. The
model's ordinary tools (`read`, `edit`, `write`, `bash`) work here, and
`nvfortran` is on the path, so compiling and running locally is allowed
and useful. It produces no evidence.

**The gateway.** The one service the session can reach. It holds its own
copy of the code, reads the working copy when asked to submit, runs
checks, and writes the ledger. The model cannot reach the builder, the
reference data, or the ledger directly.

**The ledger.** One directory per region containing every claim ever
recorded and a log of every request the gateway received. Nothing in it
is ever changed or deleted. You can read it at any time with the
`ledger` command described near the end of this manual.

**The strategy.** A configuration file, chosen by a person before the
session, that fixes the porting approach: which files may change at
most, which compiler and flags are used, how GPU execution is proven,
and which sanitizers run. The model cannot change it. Every claim
records which strategy was in effect. A region names a second one, the
baseline strategy, which is what the unmodified code is compiled with
when a speedup is measured.

**The manifest.** A configuration file per code, also fixed before the
session, saying what the code is: which tree, which makefile and build
targets, which variables the region reads and writes, which datasets,
and what the timing run is. It is why the harness needs to be taught
nothing about a code in order to build and run it. Every claim records
it too.

**The spec.** A short YAML file, `notes/regions/ch04-step.sese.yaml` in
the working copy, that names the region: which source file, which
subroutine, and which lines. The model writes it, at the start of the
session, and it is the first thing submitted.

**The allow-list.** The set of files the region is permitted to change.
Files outside it are frozen. Before the spec has been checked, only the
spec file itself is on the list. Once the analyzer has passed the spec,
the list is the spec file plus every file the spec lists. The strategy
sets a ceiling on this (`src/*.f90` and the spec directory), so a spec
cannot unfreeze anything outside it.

## Starting a session

`pi` starts inside the agent container with the extension loaded. The
extension connects to the gateway, fetches the list of actions, and
registers one tool for each, plus `submit` and `status`. One line
confirms it:

    equivalent: registered 10 tools for region ch04:step

If a configuration error is reported instead, the session cannot reach
the gateway; that is a deployment problem, not something to fix from
inside the session.

You talk to the model in plain language and it calls the tools. None of
the tools take arguments: `submit` sends whatever is in the working
copy, and each check runs against whatever was last submitted. You can
also type `/status` yourself at any time to see the region's state
without asking the model.

## A session from start to acceptance

What follows is the shape of a complete port. The gateway enforces the
order, so you do not have to; if the model calls a check too early it is
refused and told what is missing, and a capable model reads that and
does the right thing. Your job is to state the goal, watch, and step in
when the model is stuck or wrong.

### 1. Write the spec and have it checked

Ask the model to write the region spec. It needs to read the source to
find the line range. For `ch04:step` the file is:

    region: ch04:step
    files:
      - src/mod_kernel.f90
    anchor:
      file: src/mod_kernel.f90
      pst_node: "step@34-43"
      entry_symbol: step

`files` is every path the region may edit, and it is what the allow-list
is built from. The anchor's file has to be one of them. A path that does
not exist yet is allowed: a port that splits a routine into a new module
lists that module here and then writes it.

`pst_node` is the subroutine name and the inclusive line range of its
body, from `subroutine step` through `end subroutine step`. A spec can
also list callees whose bodies the analyzer should scan, under
`closure: {callees: [{name: ..., file: ..., lines: "lo-hi"}]}`. A callee
that names no `file` is in the anchor's file; one that names another
file must have that file in `files` too. The `ch04:step-stencil` region
is `ch04:step` written that way: it lists `src/mod_diff.f90` as well and
scans `diff_centered` there, so a port may change both files.

Then the model calls `submit`, and then `sese_check`. The analyzer
scans the named lines for anything that would break single-entry,
single-exit control flow: `goto`, an early `return`, `entry`, `stop`. A
pass widens the allow-list to include every file the spec lists, which
is what makes editing possible. The answer looks like:

    sese_check: pass (c-0001)

Nothing else can run until this exists.

A note on trust: the model chose the line range. A range that covers
nothing would pass trivially. The spec is in the submitted tree, so you
can read it, and the ledger records the tree; but the analyzer's verdict
is only as meaningful as the range it was given.

### 2. Port the kernel

Ask the model to port `step` to the GPU. The strategy `stdpar_managed`
compiles with `-stdpar=gpu`, so the expected shape is `do concurrent`
loops. The model can compile and run locally to try things; encourage
it to, since a local compile failure costs nothing and a submitted one
becomes a permanent `fail` claim.

### 3. Submit

`submit` reads the working copy, keeps only the files on the allow-list,
lays them over a clean copy of the baseline, and commits the result in
the gateway's repository. It answers with two identifiers:

    submitted -> tree d7c8d7867dea..., frozen 401d465b2259....
      ignored: Makefile (not_allowed), src/mod_diff.f90 (not_allowed), ...

The *tree* names this exact version of the code. The *frozen* value
names everything that was not allowed to change. Every later claim is
attached to one of these. The `ignored` list is the baseline files that
were in the working copy but not on the allow-list; seeing the other
source files there is normal, since the working copy holds the whole
program. If a file the model meant to change is in that list, the change
is outside the region.

Submitting the same content twice yields the same tree and makes no new
commit.

### 4. Run the checks

In order, each depending on the one before:

**`build_replay`** — The builder builds the submitted tree by running
the tree's own makefile, with the strategy's compiler and flags. It
knows nothing else about the code: which makefile, which targets, and
which programs those targets must leave behind all come from the code's
manifest.

The compiler the makefile is handed is a shim that writes down every
invocation before running the real one, so the claim can say more than
"it compiled". It records every compiler command line, and two checks
come out of that log: the strategy's flags reached every compile, and
every file compiled was the tree's own source. A build that succeeded
while ignoring the flags, or that reached outside the tree, is a `fail`
naming the command line or the file. A compile error is a `fail`
carrying the compiler's messages.

**`run_replay`** — The built program runs on recorded inputs, on the
GPU, with the driver's kernel-launch notifications turned on. The
gateway counts launches. A program that runs but launches no kernels
fails, even if its output is right, because the point of the port is
that the work moved to the GPU.

**`sanitize`** — `compute-sanitizer` runs the program under memcheck,
racecheck, and initcheck. One call, three claims:

    sanitize/memcheck: pass (c-0004)
    sanitize/racecheck: pass (c-0005)
    sanitize/initcheck: pass (c-0006)

Memcheck and racecheck are required for acceptance; initcheck is
recorded for information.

**`regression_visible`** — The outputs recorded by `run_replay` are
compared against the reference outputs for the visible test cases,
under the oracle's tolerance policy. The answer includes the per-case
comparison, and the claim names the policy's hash.

**`regression_holdout`** — The same comparison on a second set of cases
the session never sees. The answer is pass or fail only, so a port
cannot be tuned to the held-out set. The full comparison stays in the
ledger for a person.

**`time_port`** — The code's own program is timed, five runs by default,
with the arguments and environment the manifest declares and a budget it
declares too. This is the last requirement for acceptance. The claim
records the flags, the run times, what the program was given, which
files it wrote and their digests, and whether the GPU was otherwise
idle, which on a shared workstation it usually is not.

**`time_baseline`** — The unmodified program is timed the same way, for
comparison, built with the region's baseline strategy: a strategy file
like any other, naming the compiler and flags the comparison floor is
compiled with. It has no prerequisites, runs against the baseline tree
rather than the submission, and is not required for acceptance; once
per region is enough.

The two timing checks accept a `repeats` setting; nothing else takes
any configuration.

### 5. Acceptance

When every required check has passed on one tree, `/status` ends with
`ACCEPTED`:

    tree d7c8d7867dea...  frozen 401d465b2259...
      sese/verified  pass  c-0001
      build/replay  pass  c-0002
      gpu/executed  pass  c-0003
      sanitize/memcheck  pass  c-0004
      sanitize/racecheck  pass  c-0005
      regression/visible  pass  c-0007
      regression/holdout  pass  c-0008
      timing/port  pass  c-0009
    ACCEPTED

There is no accept action. Acceptance is a fact about the ledger, not a
step someone performs, and the ledger's own `status` command reports the
same thing from outside the session.

## Editing and re-checking

Porting is rarely one pass. The loop is: the model edits, submits, and
re-runs the checks the edit invalidated.

Every check except `sese_check` is attached to the tree, so any
submitted change to the code means those checks run again on the new
tree. `sese_check` is attached to the frozen value instead — the set of files
that were not allowed to change — so it survives edits to the region's
own file. An edit to the spec does not invalidate it either; the
allow-list comes from the recorded claim, not from the spec as it is
now, so a changed spec has no effect until `sese_check` is run again.

If the model asks for a check that already ran on the same tree with the
same settings, the gateway answers with the recorded claim instead of
running again. Repeating an identical request cannot produce a fresh
chance at a different verdict. Timing is the exception: it always runs
again, and the newest measurement is the one that counts.

## Reading what comes back

**A pass or fail** is a claim:

    build_replay: pass (c-0007)

The id in parentheses names the record in the ledger. A `fail` is a real
verdict — the check ran and the code did not meet it — and the tool's
result carries the detail: the compiler log, the sanitizer's findings,
the cases that missed tolerance. The fix is to edit, submit, and run the
check again. Failed claims stay in the ledger; they are history, not
something to erase.

**A refusal** means the check's prerequisites are not met. It is a
normal answer, not an error, and it says what to do:

    refused: 'build_replay' requires:
      - sese/verified is missing; run sese_check to produce it.

If the prerequisite ran and failed, `/status` shows the failing claim's
id beside the requirement, so "never ran" and "ran and failed" are
distinguishable.

**An error** means the check itself could not run — the builder is
unreachable, or its compiled workspace is gone after a restart. No claim
is recorded. Errors are for the person to fix; the model cannot edit its
way around one.

## Watching from outside the session

`/status` inside `pi` shows the region's state. From a shell on the
host, the `ledger` command reads the same records directly, without
going through the gateway or the model. With `deploy/state/gateway.host.yaml`
as the configuration:

    ledger status   --config <yaml> --region-id ch04:step     # current tree, each requirement
    ledger history  --config <yaml> --region-id ch04:step     # every tree, with its claims
    ledger show     <ledger-dir> c-0007                       # one claim, full detail
    ledger requests <ledger-dir>                              # every request, in order
    ledger session  <session-id> --config <yaml> --region-id ch04:step

`show` prints the complete record, including detail withheld from the
session, because the ledger is for people. `requests` lists every
request the gateway received — refused and repeated ones too — with the
session that made it.

`session` is the review tool. It reads the gateway's request log and
`pi`'s transcript of the session and prints them as one timeline: what
you said, what the model said, each tool it called, and what the gateway
answered, paired call by call. A tool call in the transcript with no
matching request line, and a request line with no tool call, are both
reported rather than hidden. It ends with a summary: submits, refusals,
claims per check, failed verdicts, how many trees the session went
through, and the time from the first request to acceptance. The session
id is shown by `pi` and is the suffix of the transcript's filename under
`deploy/state/sessions`.

## Onboarding a code

Everything above is about porting a region of a code the harness already
knows. Bringing a new code in is a session of its own, and the region it
is configured for says so: a region has a **phase**, either `porting` or
`onboarding`, and the phase decides which tools the session gets and
what `status` asks for.

An onboarding session works on the whole tree. There is no spec and no
analyzer verdict, because no region has been chosen yet; the strategy
allows every path, and what keeps the work honest is that every claim is
filed against the tree that was submitted, and a person reads what
passed before any of it is promoted.

What the model writes during onboarding lives in the tree beside the
code: a makefile with the targets the harness builds, a replay driver, a
capture program, a tolerance policy, and a **manifest** at
`harness/manifest.yaml` that names all of them. The manifest is the
code's description of itself — its source tree, its build targets, the
region's variables and their types, the runs that make the visible and
held-out datasets, the timing run, and where the tolerance policy is.
Beside the code it is written once more, as `programs/<code>/manifest.yaml`;
a code that has not been onboarded yet has only the first three fields
of it, and a region cannot be ported until the rest exists.

The session is finished when six checks have passed on one tree, at
which point `status` ends with `ONBOARDED` rather than `ACCEPTED`:

| check | what it establishes |
| --- | --- |
| `manifest_check` | the manifest is there, complete, names only files the tree holds, gives every floating-point output a tolerance band, and has the visible and held-out runs differ |
| `harness_build` | every target the manifest declares builds under both the baseline strategy and the port strategy, with each strategy's flags proven to have reached every compile |
| `harness_capture` | the capture program writes every declared dataset, each case matching the declared interface, and the visible and held-out inputs differ |
| `harness_replay` | the replay driver reproduces the captured outputs bitwise from the captured inputs |
| `harness_determinism` | capturing and replaying again agrees bitwise with what was stored |
| `harness_timing` | the timing target runs twice inside its budget and writes the same outputs both times |

The datasets a capture writes are kept in the region's ledger, under a
name that is a hash of their own bytes, and every claim that was reached
by comparing against one names it. Promoting what passed — copying the
manifest and the datasets out of the tree and making the tree the new
baseline — is a person's step, not a gateway action, and is still to
come.

## Things worth knowing

- Work in the working copy is invisible to the gateway until submitted.
  If the model reports success but `/status` shows checks missing, the
  evidence does not exist yet.
- The model can compile and run in the working copy, and should. That
  is exploration; only submitted checks count.
- The spec decides which source files are unfrozen, and the model wrote
  the spec. Read it. The strategy caps what it can name, and submitting
  always starts from the baseline, so dropping a file from the spec
  drops the edits to it — but the choice of files is the model's, and it
  is visible in the tree.
- `status` calls leave no line in the request log; every other tool
  does. The `session` timeline shows them as unlogged, which is
  expected.
- The builder keeps compiled work between checks. After a builder
  restart, run `build_replay` again; the code is safe in the gateway's
  repository.
- Timing on a workstation that is also driving a display will report
  the GPU as not exclusively yours. The numbers are still recorded;
  read them knowing that.
