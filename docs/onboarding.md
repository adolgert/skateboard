# Onboarding a code

How a Fortran code that the harness has never seen becomes one whose
regions can be ported through the gateway. Two people read this: the
person who owns the deployment, and the model working in the onboarding
session. The session's part is written so the model can follow it
directly; everything the model must produce has a check that says
whether it is right.

## What onboarding produces

A code lives under `programs/<code>/`. Before onboarding it holds:

    programs/<code>/manifest.yaml     version, name, source tree -- nothing else
    programs/<code>/baseline/         the code's files, tracked in git
    programs/<code>/NOTICE            where the files came from (optional)

After onboarding and promotion it holds the complete manifest, a
baseline that also carries `Makefile` and a `harness/` directory, the
visible inputs under `datasets/visible/`, and the answers under
`captures/`. The tree the session produces, minus its in-tree manifest,
*is* the new baseline.

The session writes, inside the working copy:

| file | what it is |
| --- | --- |
| `harness/manifest.yaml` | the code's description of itself (below) |
| `Makefile` | the build contract (below); may drive CMake or fpm |
| `harness/replay.f90` | reads one case, calls the region once, writes the outputs |
| `harness/capture.f90` (any name) | runs the code's own setup and dumps cases at the region's call site |
| `harness/tolerances.json` | the bands a port's outputs are compared within |
| `harness/properties.py` (optional) | invariants of the region, as Hypothesis properties |
| the code itself | refactored as needed so the region is a module procedure with an explicit interface |

## Before you start (the person)

### Is this code ready?

Check these before spending a session on the code. Each one is a reason
a session stalls if it is not true.

- **It builds unmodified with a compiler the builder has.** That is
  `nvfortran` from the NVIDIA HPC SDK, optionally reached through the
  code's own CMake or fpm build. Any external library it needs has to be
  one that image already holds.
- **It is deterministic.** The same build, run twice on the same input,
  writes the same bytes. Onboarding checks this twice -- once on the
  region and once on the whole program -- because a code that disagrees
  with itself cannot be the reference a port is compared against.
- **A region of it can be a procedure with an explicit interface**, with
  one way in and one way out: no `goto` out of it, no `return` before
  the end, no `stop`, and everything it reads and writes reachable as an
  argument or as a module variable a driver can set. It need not be that
  shape today -- refactoring it into that shape is part of onboarding --
  but the program's own behaviour has to survive the refactoring.
- **There is a run long enough to be worth timing and short enough to
  iterate on**: large enough that a GPU could matter, small enough that
  the CPU baseline finishes inside a budget you are willing to wait for
  twice.
- **There is behaviour to check the port against**: a validation case, a
  test suite, or an output the code is known to produce, so that a wrong
  port shows up as a wrong answer and not only as changed bits.

### Setting the deployment up

1. Put the code's files under `programs/<code>/baseline/` (a `src/`
   directory is conventional; the porting strategies allow edits under
   `src/`). Note where they came from in `NOTICE`.
2. Write the minimal `programs/<code>/manifest.yaml`:

       version: 1
       name: <code>
       source:
         root: baseline
         patterns: ["**/*.f90", "**/*.F90", "**/*.f08", "**/*.f03", "**/*.f", "**/*.F", "**/*.for", "**/*.inc", "Makefile", "**/*.mk"]

3. Write `deploy/gateway.<code>.yaml` (copy `gateway.tsunami.yaml` for the
   paths) naming the code and an onboarding region:

       codes:
         <code>:
           manifest: <code>/manifest.yaml
       regions:
         "<code>:onboard":
           code: <code>
           phase: onboarding
           strategy: onboarding
           baseline_strategy: cpu_reference

4. In `deploy/.env` set `EQUIVALENT_CODE=<code>` and
   `EQUIVALENT_REGION=<code>:onboard`, then `deploy/up.sh` and
   `deploy/pi.sh`. `EQUIVALENT_CODE` is what picks
   `gateway.<code>.yaml`, seeds the baseline from this code's tree, and
   builds the oracle around this code's directory: one deployment holds
   one code. The oracle image starts without captures, answering every
   comparison with the name of what is missing; that is expected until
   promotion.
5. Open the session with a message that points at this document and
   states the region, for example:

       Read /docs/onboarding.md, then read the code under /working. We are
       onboarding kh2d. The region is one full time step of the Kelvin-
       Helmholtz solver: the CFL scan, the three sweeps, and the periodic
       fills, taking Q, Nx, Ny, ng, dx, dy, cfl, dt_max and returning Q
       and dt. Time it at 512 by 512. Work through the checks in the
       document's order and tell me when status says ONBOARDED.

6. Decide the region before the session starts, and tell the model:
   which routine, which arguments go in and come out, and roughly what
   size the timing run should be. Region choice is a person's judgement.
   The region must be a single-entry, single-exit procedure -- no
   `goto`, no `return` before the end, no `stop` -- and everything it
   reads and writes must be an argument or a module variable the driver
   can set. If it is not that today, refactoring it into that shape is
   part of onboarding.

## The session (the model)

You have the tools `submit`, `status`, and the eight onboarding checks.
Every check is filed as a claim on the tree you last submitted; `status`
lists which claims the current tree has and which it lacks. A check that
refuses tells you which claim it needs first. A check that fails tells
you why in its detail. Work in this order, submitting after every change
to the tree -- it is one order that satisfies every check's own
preconditions, and the gateway enforces those, not the numbering:

1. Make the region a module procedure with an explicit interface, if it
   is not one. Read the code first. Keep the program's behaviour
   identical: the program's own output at the timing size is compared
   later, and the whole-program run must still pass its own assertions.
2. Write `Makefile`, `harness/replay.f90`, the capture program,
   `harness/tolerances.json`, and `harness/manifest.yaml`. All five
   before the first check: the manifest names the tolerance file, and a
   manifest naming a file that is not in the tree does not load at all.
   Build by hand in your own container first
   (`make FC=nvfortran FFLAGS="-O2" MODFLAG=-module HARNESS=/opt/harness
   replay timing capture` -- `/opt/harness` holds `npy_io.f90` in your
   container as it does in the builder's).
3. `submit`, then `manifest_check`: the manifest loads, is complete,
   names only files the tree holds, gives every floating-point output a
   band and every timing output a band of its own, and makes the visible
   and held-out runs different runs. Fix what it names. Repeat until it
   passes.
4. `harness_build`: every target builds under both the region's baseline
   strategy and its port strategy, and the compiler log shows each
   strategy's flags on every compile and nothing compiled from outside
   the tree.
5. `harness_capture`: the capture program writes at least one case for
   every dataset the manifest declares; every case holds exactly the
   declared variables -- none missing, none extra -- at the declared
   type and rank; and the visible and held-out inputs are not the same
   arrays.
6. `harness_replay`: the replay driver reproduces every captured output
   bitwise from the captured inputs, built the way the baseline is
   built.
7. `harness_determinism`: capturing and replaying again gives the same
   bytes as what was stored.
8. `harness_timing`: the timing target runs twice, each run inside the
   budget the manifest declares, and writes the same declared `.npy`
   outputs both times.
9. `harness_self_check`: mutants of the files the manifest lists under
   `interface.files` are built and replayed the way the baseline is, and
   scored with the harness's own comparator against your bands. At least
   one mutant must be caught. A mutant that changes an output but stays
   *inside* the bands is the tolerance-blind gap and fails the check:
   the bands are what to change, not the check. Survivors -- mutants no
   output changed at all for -- are listed for the person and are not
   counted against you.
10. `harness_property` (optional module): the properties pass against
    the baseline build. If the code declares none, the check records
    that it declares none, which is still a claim.
11. `status` reports `ONBOARDED`. Stop; promotion is the person's step.

When a check fails, read its detail before changing anything. The detail
names the file, case, variable, or compile line it objected to.

### The manifest, in-tree form

`harness/manifest.yaml`, with every path relative to the tree root and
`root: .` on a line of its own (promotion rewrites that one line):

    version: 1
    name: <code>
    source:
      root: .
      patterns: [...]              # as the minimal manifest
    build:
      makefile: Makefile
      targets:
        replay:  {target: replay,  executable: replay}
        timing:  {target: timing,  executable: <program>}
        capture: {target: capture, executable: <capture program>}
    interface:
      module: <module holding the region>
      entry: <the region procedure>
      files: [src/<file>.f90, ...]  # the files that implement the region; a port edits these, the self-check mutates them
      inputs:
        - {name: <var>, dtype: f64, rank: 3}
        - {name: <scalar>, dtype: i32, rank: 0}
      outputs:
        - {name: <var>, dtype: f64, rank: 3}
    datasets:
      visible: {args: [...]}       # the capture program's arguments for the visible run
      holdout: {args: [...]}       # a different run; its inputs must differ
    timing:
      args: [...]                  # the timing program's arguments, e.g. the problem size
      outputs: [<file>.npy, ...]   # what the timing run writes; compared against the baseline's run
      budget_s: <seconds per run>
      env: {"NAME": "value"}       # optional; values are strings, so quote numbers
    tolerances: harness/tolerances.json
    properties: harness/properties.py   # or null

Types are `f32`, `f64`, `i32`, `i64`, `l` (logical); rank is 0 to 4. A
variable that is both read and written appears in both lists. What the
loader insists on, beyond the shapes above: `version` is 1; unknown keys
are refused rather than ignored; `interface.files` may not be empty and
every path in it, the makefile, the tolerance file, and the properties
module must exist in the tree; the `replay` build target is required and
the other roles are not; `visible` and `holdout` are required and a code
may declare further datasets beside them; `budget_s` is a positive
number; and `timing.env` values are written as strings, because the ones
that matter here look like numbers and must reach the program exactly as
written. A manifest either says only `version`, `name`, and `source` --
the minimal form, enough to seed a baseline and start a session -- or
says all six of the rest as well. Anything in between is refused, naming
what is absent.

### The build contract

The builder runs, in the tree root:

    make -f <makefile> <target>...

with these in the environment: `FC` (a shim that logs every compiler
invocation, then runs the strategy's compiler), `FFLAGS`, `LDFLAGS`,
`MODFLAG` (`-module` for nvfortran, `-J` for gfortran), and `HARNESS`
(the directory holding `npy_io.f90`). The Makefile must:

- pass `$(FFLAGS)` to every compile and `$(LDFLAGS)` to every link --
  the shim log is read afterwards, and a compile without the strategy's
  flags fails `harness_build`;
- compile only files from the tree and `$(HARNESS)/npy_io.f90`; a
  compile that reaches anywhere else fails the same check, naming the
  file;
- leave each target's executable at the path the manifest names, in the
  tree root -- `make` reporting success while leaving no executable is a
  failure that usually means the manifest and the rule disagree;
- provide `replay`, `timing`, and `capture` targets. A `clean` target is
  a convenience for hand builds; nothing in the harness runs it, because
  every build starts from a workspace written fresh from the submitted
  tree.

`MODFLAG` is filled in from the compiler the strategy names, and is
empty for a compiler the builder has no spelling for, so the rule that
uses it has to work either way. Never set `FFLAGS` with `=` in the
Makefile; `?=` gives a default for hand builds and lets the builder's
value through. If the code has its
own CMake or fpm build, the Makefile may drive it, provided the flags
still reach every compile (`-DCMAKE_Fortran_FLAGS="$(FFLAGS)"`,
`fpm build --flag "$(FFLAGS)"`).

### The replay contract

`replay <case_dir>`: for each declared input, `npy_load` the file
`<case_dir>/<name>.npy`; call the region entry exactly once; for each
declared output, `npy_save` to `<case_dir>/<name>.out.npy`. `npy_io`
gives `npy_load(path, x)` (allocatable `x`, shape from the file) and
`npy_save(path, x)` for the five types at ranks 0 to 4. NPY carries
shape but not lower bounds: an array whose lower bound is not 1 gets it
back when the driver passes it to a dummy declared with that bound, so
declare the dummy as the code does (`Q(:, 1-ng:, 1-ng:)`), and pass
the scalars the bounds depend on as inputs.

### The capture contract

`<capture program> <args...> <outdir>`: run the code's own setup with
the parameters in `args`, and at the region's call site, for some of the
calls, write `<outdir>/caseNNNN/` holding `case.json`
(`{"inputs": [names], "outputs": [names]}`), every input as
`<name>.npy` before the call, and every output as `<name>.out.npy`
after it. Five to twenty cases spread over the run is usual. The
visible and held-out runs are the same program with different
parameters; their inputs must differ.

### The timing contract

`<timing program> <args...>`, run in the tree root with `timing.env`
added to its environment, must finish within `budget_s` -- that is the
budget for one run, and onboarding makes two of them and requires the
declared outputs to be identical both times. Each file named in
`timing.outputs` must be written as NPY, and the check refuses a name
that does not end in `.npy`: the timing outputs are compared as arrays,
the same way the region's are.
The port's run is compared against the baseline's run file by file, so
make the size large enough that a GPU matters and small enough that the
CPU baseline finishes in the budget. A program that prints results
needs a driver, or a few lines at the end, that also writes them as
arrays.

### Tolerances

    {
      "policy_version": "<date and a word>",
      "acceptance": "an element passes if abs OR rel OR ulp is within band",
      "variables": {
        "<output>": {"abs": 1e-12, "rel": 1e-10, "ulp": 4}
      },
      "files": {
        "<file>.npy": {"abs": ..., "rel": ..., "ulp": ...}
      }
    }

`variables` bands one call of the region; `files` bands one
whole-program run and is keyed by the paths in `timing.outputs`. Every
`f32` or `f64` region output needs a `variables` entry and every timing
output a `files` entry, each saying all three of `abs`, `rel`, and
`ulp`; integers and logicals are compared exactly and are not banded.
A timing output needs a band whatever it holds, because nothing declares
its element type until it is read. `manifest_check` is what refuses a
policy that is missing one. Calibrate rather than guess: build the unported code
twice with different flags (`-O2` and `-O3 -ffast-math`, say), replay
the visible cases and run the program under both, measure the spread,
and set each band at several times the observed spread with a floor at
a few ULP. Record the method and the measurements in the file. The
self-check then tells you whether the bands are too loose.

### Properties (optional)

A pytest module that imports `harness_properties` and uses Hypothesis.
`@harness.settings()` goes above `@given`, as Hypothesis's own
`@settings` does:

    import harness_properties as harness
    from hypothesis import given, strategies as st

    CORPUS = harness.corpus()      # the visible cases' inputs, as arrays

    @st.composite
    def states(draw):
        case = CORPUS[draw(st.integers(0, len(CORPUS) - 1))]
        scale = draw(st.floats(0.9, 1.1, width=32))
        return {name: (a * scale).astype(a.dtype) for name, a in case.items()}

    @harness.settings()
    @given(states())
    def test_something(state):
        out = harness.run_replay(state)
        ...

The library is five names. `corpus()` returns the visible cases' inputs
as `[{variable: ndarray}, ...]`. `run_replay(inputs) -> outputs` runs
the replay binary once on those arrays in a fresh case directory and
returns every `<name>.out.npy` the driver left, as arrays; it raises
rather than returning if the driver fails or hangs. `settings()` is the
Hypothesis settings every property should share -- the run's example
count, no deadline, and the too-slow health check off, because every
example here starts a process. `max_examples()` and `seed()` are the two
numbers the run was given, for a module that needs to draw something of
its own.

Everything the library needs comes from the environment the builder
sets, so a property module names no path, no binary, and no seed. Good
properties: determinism, a conservation law within a measured tolerance,
a symmetry the discretisation has exactly. State in the module what each
property does and does not catch.

## After the session (the person)

Read the claims: `ledger status --config deploy/state/gateway.host.yaml
--region-id <code>:onboard`, and `ledger show` on anything whose detail
you want -- the self-check's survivors and the build's compile lines
especially. Read the working copy. Then:

    ledger promote --config deploy/state/gateway.host.yaml --region-id <code>:onboard

It refuses -- writing nothing -- unless the region is one being
onboarded, every onboarding check passed on the region's current tree,
and the working copy is that tree byte for byte, naming the first file
that differs. It also refuses if a destination under `programs/<code>/`
already holds something; `--replace` empties those first, and
`--programs <dir>` writes the whole result somewhere else, which is how
to compare a promotion against what is checked in. What it writes is the
manifest with its source root rewritten from the tree to the `baseline`
beside it, the tree minus that manifest as `baseline/`, the visible
inputs under `datasets/visible/`, and the answers -- the visible outputs
and the whole held-out set -- under `captures/`. The program's own
timing outputs are not promoted: a run at a real timing size writes
megabytes, and what a port is compared against is the deployment's own
`time_baseline` run, which stays in the region's ledger.

Then commit `programs/<code>/`, add a porting region
to `gateway.<code>.yaml` (`phase: porting`, a `spec_path` under
`notes/regions/`, `strategy: stdpar_managed`, `baseline_strategy:
cpu_reference`, `visible_dataset: visible`), set `EQUIVALENT_REGION` to
it, and run `deploy/down.sh` and `deploy/up.sh` so the oracle image is
rebuilt with the captures. Porting sessions then proceed as
`docs/pi-users-manual.md` describes.
