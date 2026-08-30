# fmutate

Source-level mutation testing for Fortran kernels that sit behind a
capture-replay oracle.

It answers the question the acceptance gate cannot answer about itself: *if a
port of this kernel were wrong, would the gate notice?* It injects single-token
faults into the kernel source, rebuilds the replay driver against each one,
replays the capture corpus, and scores the outcome with the **real** oracle
comparator and the **real** tolerance file. The number it reports is therefore a
property of the gate as configured, not of a model of the gate.

Background and the survey that motivated the design: [`docs/coverage-testing.md`](../../docs/coverage-testing.md).

The operator tables and the generator here are also the harness's own: they are
carried over, operator for operator, into `services/builder/mutate.py`, which
runs inside the builder as the `harness_self_check` step of onboarding a code.
That step asks the same question about a code being brought in, with the tree's
own makefile and the bands the code proposes, and returns per-mutant verdicts
rather than outputs. This tool is the standalone one: it runs on the host, with
gfortran, and it is where a coverage prepass and a checked rebuild live.

## Why not an existing tool

There isn't one. [Mothra](https://fortranwiki.org/fortran/show/Mutation+testing+frameworks)
is FORTRAN 77 and unobtainable; [universalmutator](https://github.com/agroce/universalmutator)
will mutate Fortran but its regex rules have no concept of an array section,
which is the construct that matters most for a GPU port. Mutating LLVM IR (Mull)
does not help either: by the time flang has lowered an array assignment, the
section extents are a single computed trip count, so the mutants that matter
cannot be expressed.

## Usage

```bash
python3 tools/fmutate/fmutate.py tools/fmutate/targets/tsunami.json --checked
```

Useful flags:

| Flag | Effect |
|---|---|
| `--list` | Generate and print mutants; build nothing. Fast way to review operator behaviour on a new target. |
| `--checked` | Rebuild release-survivors with the target's `checked_flags`. **Use this.** See below. |
| `--ops SBR,AOR` | Restrict to named operators. |
| `--limit N` | Score only the first N mutants (smoke test). |
| `--no-coverage` | Skip the coverage prepass and build every mutant. |
| `-j N` | Worker processes (default: CPU count). |
| `--json FILE` | Per-mutant results for trend tracking. |
| `--fail-on-gap` | Exit nonzero if the tolerance-blind gap is not zero. For CI. |

Requires `gfortran`, `gcov`, `numpy`.

## The three numbers it reports

**Mutation score over covered code.** Mutants on lines the corpus never
executes are generated, marked `UNCOVERED`, and never built — they cannot be
killed, so counting them only dilutes the score. The uncovered set is reported
separately as dead surface, which is a coverage finding in its own right.

**The tolerance-blind gap.** Every mutant is scored twice: under the tolerance
policy and under bitwise equality. A mutant that changes output bitwise but
passes the tolerance policy is one the tolerance is hiding. **This, not the
mutation score, is the metric to watch.** It gives a principled ratchet for
recalibration: when tolerances are widened for a new compiler (the pending
`nvfortran` recalibration in `programs/tsunami/baseline/harness/tolerances.json`),
widen them only as far as the gap stays at zero. `--fail-on-gap` enforces that in CI.

**Killed only by the checked build.** With `--checked`, survivors are rebuilt
with `-fcheck=all -finit-real=snan -ffpe-trap=...` and replayed again. Anything
that dies there is a fault the release-mode gate provably cannot see. On the
tsunami target this is 12 mutants, all of them array-section bound errors in the
interior stencil of `diff_centered`:

```
dx(2:im-1) = x(3:im) - x(1:im-2)     ! original
dx(2:im+1) = x(3:im) - x(1:im-2)     ! mutant: bit-identical output at -O2
```

The sections stop conforming, gfortran takes the trip count from one side, and
the arithmetic that survives is the original arithmetic — so there is no
difference for any comparator to detect. Under `-fcheck=all` every one of them
traps at line 22. This is not a hypothetical fault class: array-section bound
arithmetic is precisely what gets rewritten when array syntax becomes an
explicit `do concurrent` or `!$omp target teams distribute` loop, and off-by-one
bounds are the characteristic failure mode.

## Mutation operators

| Op | Meaning | Example |
|---|---|---|
| `AOR` | arithmetic operator replacement | `a + b` → `a - b` |
| `ROR` | relational operator replacement | `a .lt. b` → `a .le. b`; also `<`, `>=`, `==`, … |
| `LCR` | logical connector replacement | `.and.` → `.or.`, `.eqv.` → `.neqv.` |
| `CRP` | constant replacement | `0.5` → `1.0`, `0.0`; `2` → `3`, `1` |
| `SBR` | **section bound replacement** | `x(2:n-1)` → `x(2:n-2)` |
| `SDL` | statement deletion | comment the assignment out |

`SBR` is the one worth the trouble. It perturbs the integer offset in a
subscript by ±1, including collapsing it away entirely, and it is the operator
that surfaces the checked-build class above. Exponentiation (`**`), exponent
signs in literals, declarations, and continuation lines are excluded; string
literals and comments are blanked before matching so format strings are never
mutated.

The generator is regex-based, like universalmutator, and so inherits the same
limitation: it does not parse Fortran. Non-compiling mutants are reported
separately and cost one compile each, which is cheap relative to being wrong
about what a parser would have done with Fortran 2008.

## Target files

A target describes one kernel and its corpus. Paths are relative to the repo
root. See `targets/tsunami.json`.

```jsonc
{
  "name": "human-readable label",
  "src_dir": "programs/tsunami/baseline/src", // where the kernel sources live
  "mutate": ["mod_kernel.f90"],         // which of them to mutate
  "build": {
    "fc": "gfortran",
    "sources": ["mod_params.f90", "..."],        // compiled from src_dir, in order
    "extra_sources": ["services/builder/harness/npy_io.f90",
                      "programs/tsunami/baseline/harness/replay.f90"], // harness + the code's driver
    "exe": "replay",
    "run_args": ["{case_dir}"],                  // {case_dir} is substituted
    "flags":          ["-O2", "..."],            // release build (the gate's build)
    "coverage_flags": ["-O0", "--coverage", "..."],
    "checked_flags":  ["-fcheck=all", "-finit-real=snan", "..."]
  },
  "corpus": {
    "inputs":   "programs/tsunami/datasets/visible",   // per-case input dirs
    "expected": "programs/tsunami/captures/visible",   // per-case reference dirs
    "case_glob": "case[0-9]*",
    "input_files": ["h.npy", "u.npy"],           // copied into the case dir
    "variables": { "h": "h.out.npy", "u": "u.out.npy" },
    "raw_dtype": "<f8"                           // only for corpora that are not .npy
  },
  "comparator": {
    "module":     "equivalent/capture/compare.py",       // must expose compare_variable()
    "tolerances": "programs/tsunami/baseline/harness/tolerances.json"  // must have a "variables" key
  }
}
```

The driver contract is the harness's own: the driver is invoked as
`driver <case_dir>`, reads `input_files` from that directory, and writes the
files named in `variables` back into it. Every code carries its own driver
implementing it, in its tree -- `programs/tsunami/baseline/harness/replay.f90`
is the one above -- compiled against `npy_io.f90`, which is the harness's and is
baked into the builder image. Any kernel wrapped that way can be targeted
without changing this tool.

Corpus files ending in `.npy` carry their own element type, shape, and element
order, and are read as they are. A target whose corpus is raw streams instead
has to say what is in them with `raw_dtype`; without it, such a file is
refused rather than guessed at.

`case_glob` is deliberately `case[0-9]*` rather than `case*`: the latter also
matches `cases.json`.

## Interpreting a survivor

A mutant that survives everything is one of three things, in decreasing order of
how much you should worry:

1. **An oracle hole.** The suite genuinely cannot distinguish a wrong kernel.
2. **An input-selection gap.** The mutant is equivalent *for this corpus only*.
   Both survivors on the tsunami target are this: `/ dx` → `* dx` is undetectable
   because `mod_params.f90` sets `dx = 1.0`. No oracle strengthening reaches it;
   only a capture case with `dx /= 1.0` does.
3. **A genuinely equivalent mutant.** Unavoidable; typically 10–40% of mutants
   in the literature, though coverage filtering and `--checked` remove most of
   the ones that appear here.

The tool cannot tell these apart. Reading the survivor list is the point of
running it.

## Cost

Each mutant is one compile plus one replay per case, run in parallel across
workers. The tsunami target (93 mutants, 23 filtered as uncovered, 5 cases,
`--checked`) takes a couple of minutes on a workstation. Cost scales with kernel
size, so keep targets scoped to the port boundary rather than whole programs —
which is the same scoping the capture-replay harness already imposes.
