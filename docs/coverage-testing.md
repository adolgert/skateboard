# Coverage-Based Testing for Fortran 2008

Research memo. The question behind it: when an AI agent hands back a ported kernel and the
capture-replay oracle says PASS, *how much should we believe it?* Coverage metrics are the
usual answer, and the plain ones (line, branch) are the weakest members of a family that
runs from "did the code execute" all the way to "would a fault have been observed." This
surveys that family, says what is actually implementable for Fortran 2008 today, and
reports a measurement run against `demo/`.

Section 4 is the measurement. It is the part specific to this repository; sections 1-3 and
5-11 are the survey.

---

## 1. The adequacy ladder

Every technique here answers a different question. Ordering them makes the tradeoffs legible:

| Rung | Metric | Question answered | Cost |
|---|---|---|---|
| 0 | Line / statement coverage | Was the code *executed*? | ~free |
| 1 | Branch / MC/DC | Were the *decisions* exercised? | cheap |
| 2 | Checked coverage | Did executed code *influence an assertion*? | dynamic slicing |
| 3 | Observable MC/DC | Could a fault here have *propagated* to an output? | taint/tag propagation |
| 4 | Mutation testing | Do injected faults actually get *caught*? | N × test-suite runtime |
| 5 | Fault injection | Do *hardware-realistic* faults get caught? | N × runtime, statistical |

Rungs 0-1 measure the test *inputs*. Rungs 2-5 measure the test *oracle*. That distinction
is the whole point for a porting project: a capture-replay suite can have 100% line coverage
and still accept a wrong kernel, because coverage never looks at the comparison step.

[A Brief Survey on Oracle-based Test Adequacy Metrics](https://arxiv.org/pdf/2212.06118)
is the best single map of rungs 2-4.

---

## 2. Rungs 0-1: what actually works on Fortran today

The honest summary is that Fortran structural coverage is a solved-but-shabby problem, and
the tooling is worse than it was ten years ago.

**gfortran + gcov.** Works, is the only fully free option, and is what I used in §4.
Compile with `--coverage` (`-fprofile-arcs -ftest-coverage`), run, then `gcov`. Two
practical traps:

- *The `.gcno` naming trap.* If you compile many sources in one `gfortran -o replay a.f90
  b.f90 ...` command, the notes files are named `replay-a.gcno`, not `a.gcno`, and plain
  `gcov a.f90` fails with "cannot open notes file". You must run `gcov replay-a.gcda`. This
  bit me on the first attempt in §4 and it is worth encoding in whatever CI target you add.
- *Branch counts are CFG arcs, not `if` statements.* [gcovr's FAQ](https://gcovr.com/en/stable/faq.html)
  is explicit that 100% branch coverage is unreachable for most programs. For Fortran this
  is much worse than for C, because array-section assignments, `allocatable` handling, and
  I/O statements all generate hidden arcs. In §4, `mod_kernel.f90` shows 18 "branches" in a
  4-line subroutine that contains no conditional at all. **Treat gfortran branch coverage
  as noise; use line coverage and get your decision-level signal from rungs 3-4 instead.**

**Intel.** `ifort -prof-gen=srcpos` + `profmerge` + `codecov` produced good HTML reports,
but per [Intel's own forum](https://community.intel.com/t5/Intel-Fortran-Compiler/Code-coverage-using-ifx/m-p/1506041),
code coverage was **not** carried over to `ifx`, and `ifort` is discontinued. If you were
relying on this, it is a dead end.

**NAG (`nagfor`).** Strong on runtime checking and standards conformance
([QMUL's writeup](https://blog.hpc.qmul.ac.uk/checking-nagfor/) is a good tour) but I found
no coverage-instrumentation feature. Its value here is different and real — see §4's
finding about `-fcheck=all`.

**flang (LLVM).** Since LLVM 20 the driver is just `flang`
([LLVM blog](https://blog.llvm.org/posts/2025-03-11-flang-new/)). It lowers Fortran →
HLFIR → FIR → LLVM IR. It does not currently expose a mature `--coverage` story, but
because it emits ordinary LLVM IR, **the entire LLVM instrumentation stack is in principle
reachable from Fortran**: SanitizerCoverage, DataFlowSanitizer, and the IR-level mutation
and fault-injection tools in §§6-8. This is the single most important structural fact in
this memo. Everything interesting for rungs 3-5 routes through flang.

**GPU offload is a coverage blind spot.** No coverage tool instruments OpenMP `target`
regions or `do concurrent` offloaded to the device. Nsight Systems/Compute give you
*profiling* (was the kernel launched, how long did it take) and not *coverage* (which lines
in the kernel ran). For this project's purposes the practical substitute is: keep the CPU
baseline and the GPU port compiled from the same source, measure coverage on the CPU build,
and use the device-proof check (already in `builder/stages.py`) to confirm the GPU path is
the one actually executing at runtime.

---

## 3. Rung 4: mutation testing

### 3.1 What exists for Fortran

Very little, and the history is discouraging.

- **Mothra** (Offutt et al., late 1980s) was a full mutation system for FORTRAN 77, complete
  with [a custom interpreter for mutant execution](https://dl.acm.org/doi/10.1145/29650.29669)
  and a [language system](https://onlinelibrary.wiley.com/doi/abs/10.1002/spe.4380210704).
  It is the only entry on the [Fortran Wiki mutation page](https://fortranwiki.org/fortran/show/Mutation+testing+frameworks),
  and it is F77-only and unavailable in practice.
- **universalmutator** ([Groce et al., ICSE 2018 demo](https://mir.cs.illinois.edu/marinov/publications/GroceETAL18UniversalMutator.pdf),
  [repo](https://github.com/agroce/universalmutator)) is the one live tool that will mutate
  Fortran today. It ships a `fortran.rules` file covering arithmetic operators, the dotted
  relationals (`.lt. .le. .gt. .ge. .eq. .ne.` in both cases), and the dotted logicals
  (`.and. .or. .eqv. .neqv.`). Being regex-based, it needs no parser and does not care that
  your code is Fortran 2008 — but it also has no idea what an array section is, which
  matters a great deal (see §4.3). Its `comby` mode produces fewer invalid mutants.
- Nothing else. There is no Fortran equivalent of PIT/Stryker. **If you want mutation
  testing on this project you are building the harness.** The good news from §4 is that the
  harness is roughly 150 lines of Python because the replay driver already exists.

### 3.2 The numerical-code problems

Mutation testing was designed for discrete programs, and scientific code breaks two of its
assumptions.

*Equivalent mutants.* Between [10% and 40% of mutants are typically equivalent](https://stryker-mutator.io/docs/mutation-testing-elements/equivalent-mutants/),
and determining equivalence is undecidable in general. Floating-point code adds a species
that discrete code does not have: mutants that are *numerically* but not *syntactically*
equivalent, e.g. a precision degradation that stays inside the test's tolerance band. Work
on [approximate transformations as mutation operators](https://users.ece.utexas.edu/~gligoric/papers/HaririETAL18ApproximateTransformationsAsMutationOperators.pdf)
and [semantic mutation of floating-point comparison](https://www.researchgate.net/publication/254034800_Semantic_Mutation_Analysis_of_Floating-Point_Comparison)
treats this deliberately.

*The oracle problem.* You often cannot say what the right answer is. Hook and Kelly's
**mutation sensitivity testing** is the direct response: instead of a binary killed/survived
verdict, measure the *relative error* the mutant induces in the output, which sidesteps the
need for an exact oracle. Their finding that a handful of tests detects most faults, and
that randomly selected inputs often beat hand-picked ones, is worth taking seriously when
choosing capture points. See [Investigating test selection techniques using Hook's mutation
sensitivity testing](https://www.researchgate.net/publication/220308213_Investigating_test_selection_techniques_for_scientific_software_using_Hook's_mutation_sensitivity_testing).

A very recent line of work builds a [semantic mutation metric for metamorphic relation
adequacy](https://arxiv.org/pdf/2605.17437), scoring mutants by numerical divergence rather
than categorical kill — the natural fusion of mutation testing with §10.

### 3.3 Making it affordable

Mutation testing is N compile-and-run cycles. Standard cost reductions, in rough order of
value for this project:

1. **Coverage filtering.** Never run a mutant in code the suite does not execute — it cannot
   possibly be killed. [Reducing mutation costs through uncovered mutants](https://onlinelibrary.wiley.com/doi/abs/10.1002/stvr.1534).
   In §4 this alone would have cut the campaign by 25%.
2. **Mutant schemata** (Untch): inject all mutants at once behind a runtime switch, compile
   once. This is exactly what [Mull](https://mull.readthedocs.io/en/latest/HowMullWorks.html)
   does at the bitcode level.
3. **Split-stream execution**: start each mutant from its mutation point rather than program
   start. [Reported ~3.5× over schemata when done on LLVM IR](https://arxiv.org/pdf/2210.17215).
4. **Selective mutation / sampling** — cheap, blunt, effective.
5. **Predictive mutation testing**: ML models predicting killed/survived without executing.
   Attractive on paper; [known methodological pitfalls](https://arxiv.org/pdf/2005.11532),
   chiefly that models mostly learn coverage. I would not build on this yet.

For a kernel-sized target (the shape this project always works in) the campaign is cheap
enough that (1) plus brute force is sufficient.

---

## 4. Measurement: mutation testing the `demo/` oracle

I ran a real campaign against this repository rather than describing one. The harness is
committed as [`tools/fmutate`](../tools/fmutate/README.md):

```bash
python3 tools/fmutate/fmutate.py tools/fmutate/targets/demo.json --checked
```

**Setup.** Mutants generated over `demo/work/src/mod_kernel.f90` and `mod_diff.f90` using
six operators — arithmetic (AOR), relational (ROR), logical (LCR), constant (CRP), **array
section bound (SBR)**, and statement deletion (SDL). Each mutant: rebuild
`capture/replay.f90` against the mutated module, replay the 5 visible cases, and compare to
`oracle/captures/visible` using the real `oracle/compare.py` and `tolerances.json`, scored
both under the tolerance policy and under bitwise equality. Mutants on lines a coverage
prepass shows are never executed are not built at all.

**Results.**

```
generated                          93
uncovered (not built)              23
non-compiling                       0
scored                             70

KILLED by tolerance oracle         56   (80.0%)
TOLERANCE-BLIND GAP                 0
survived release, killed by        12
  the checked build
SURVIVED everything                 2
```

Three findings, in ascending order of importance.

### 4.1 The tolerance policy costs nothing in fault detection

The tolerance-blind gap is **zero**: every mutant that changed the output bitwise also
violated the tolerance policy. Given `tolerances.json` currently carries `observed_cpu_spread
= 0` and floors of `abs 1e-6 / rel 1e-5 / ulp 16`, the floors are not masking any fault this
operator set can produce. That is a genuinely good result and worth recording as a baseline —
**when you recalibrate against `nvfortran`, re-run this campaign.** The number that matters
is not the tolerance value, it is whether loosening the tolerance moves the tolerance-blind
gap off zero. That gives you a principled ratchet: *tolerance may be loosened only as far as
the gap stays at 0.*

### 4.2 A quarter of the mutants are unkillable, and coverage says why

23 of 93 mutants land in `diff_upwind`, which `step` never calls. gcov confirms it:

```
File 'mod_diff.f90'   Lines executed:58.33% of 12
    #####:   33:    im = size(x)
    #####:   35:    dx(2:im) = x(2:im) - x(1:im-1)
File 'mod_kernel.f90' Lines executed:100.00% of 4
```

This is the concrete argument for rung 0 even in a project that has rung 4: coverage does
not tell you the suite is good, it tells you which mutants are not worth building. It also
flags `diff_upwind` as untested surface area that a porting agent could silently break.
`fmutate` does the filtering automatically and reports the dead lines separately.

### 4.3 Shape-nonconforming array sections are invisible at `-O2` — and this is the real risk

Twelve survivors are in *covered* code, all on one line — the interior stencil of
`diff_centered`:

```fortran
dx(2:im-1) = x(3:im) - x(1:im-2)     ! original
```

Mutants like `dx(2:im+1) = ...`, `dx(2:im) = ...`, `dx(2:im-2) = ...`, and
`x(1:im+2)` all produce output **bit-identical to the original** under `gfortran -O2`. The
LHS and RHS array sections no longer conform, gfortran silently takes the trip count from one
side, and the arithmetic that survives is the original arithmetic. The oracle cannot see a
difference because there is no difference to see.

Rebuilding the same mutants with runtime checks changes the verdict completely:

```
MUTANT: dx(2:im+1) = x(3:im) - x(1:im-2)
  release (-O2)                          ran ok; h_out sha=184b92724aed   <-- identical
  checked (-fcheck=all -finit-real=snan) RUNTIME-ERROR at line 22
```

All twelve. **`-fcheck=all` converts twelve silent equivalent mutants into twelve kills.**
(My first prototype found eight; adding a dedicated section-bound operator — `SBR` in
`fmutate` — found four more. The operator set is the binding constraint on what mutation
testing can tell you, which is the argument in §3.1 against regex tools that have no notion
of an array section.)

The implication for this project is direct and not hypothetical: array-section bound
arithmetic is precisely what an LLM rewrites when it converts an array-syntax stencil into
an explicit `do concurrent` or `!$omp target teams distribute` loop, and off-by-one bounds
are its characteristic failure mode. A mutant class the oracle provably cannot detect at
`-O2` is a fault class the gate provably cannot detect at `-O2`.

**Recommendation:** add a checked-build gate — compile the ported kernel a second time with
`-fcheck=all -finit-real=snan -ffpe-trap=invalid,zero,overflow` and replay the corpus under
it, in addition to the release build. It costs one extra compile and one extra replay per
attempt, and it closes the largest hole this campaign found. `nagfor` is the stricter option
if you want a second opinion.

### 4.4 The two remaining survivors are a test-input problem

```fortran
u = u - (u * diff_centered(u) + g * diff_centered(h)) * dx * dt   ! mutant, original: / dx
```

survives because `mod_params.f90` sets `dx = 1.0`, so multiply and divide coincide. This is a
true equivalent mutant *for this test suite only* — a dimensional error that becomes real the
moment anyone changes the grid spacing. Neither coverage nor a stronger oracle catches it;
only a different input does. **Recommendation:** generate one capture case with `dx /= 1.0`
(and ideally `hmean`, `dt` perturbed). Cheap, and it kills a mutant class that nothing else
in the ladder reaches.

This is Hook and Kelly's finding reproduced in miniature: input selection, not oracle
strength, was the binding constraint for these two.

---

## 5. Rung 2: checked coverage (Schuler & Zeller)

**Idea.** Ordinary coverage counts executed statements. Checked coverage counts only those
executed statements that lie in the *backward dynamic slice* of a value an oracle actually
checks. Statements that run but cannot influence any assertion are excluded. Schuler and
Zeller found it a [more sensitive indicator of oracle quality than mutation testing](https://www.st.cs.uni-saarland.de/publications/files/schuler-icst-2011.pdf),
at lower cost ([journal version](https://www.st.cs.uni-saarland.de/publications/files/schuler-stvr-2013.pdf)).

**Why it is attractive here.** It is a direct answer to "is the capture-replay oracle
checking enough?" — precisely this project's trust question — and unlike mutation testing it
is *one* instrumented run, not N.

**The obstacle.** The original implementation is a Java bytecode dynamic slicer
(JavaSlicer). **There is no dynamic slicer for Fortran.** No off-the-shelf path exists.

**How to approximate it for Fortran.** Two routes, both through LLVM:

1. **DataFlowSanitizer.** `-fsanitize=dataflow` associates taint labels with memory and
   propagates them through computation
   ([design doc](https://clang.llvm.org/docs/DataFlowSanitizerDesign.html)). Label each
   captured input array element, run the kernel, and read the labels on the compared outputs.
   The set of labels reaching a checked output *is* an over-approximation of the checked
   slice. This is forward taint rather than backward slicing, which makes it the same
   approximation Whalen's OMC/DC uses (§6) — so one instrumentation buys both rungs.
   Caveat: dfsan is a Clang-driver feature; wiring it to flang-produced IR means running the
   pass manually over the `.ll`, and dfsan's ABI-list mechanism will need entries for the
   Fortran runtime. Non-trivial, but it is the only credible route.
2. **Cheap structural proxy.** For a kernel with an explicit captured-state boundary — which
   this project always constructs — checked coverage degenerates to something you can compute
   from the §manifest in `refactor.md`: a written variable that is not in the compared output
   set is *unchecked*, full stop. Comparing the manifest's write-set against the oracle's
   compared-variable set is a five-line check that catches the important case (kernel writes
   module state that nobody compares) with none of the machinery. **Do this first.**

Related and easier to steal: [State Field Coverage](https://arxiv.org/html/2510.03071v1)
measures how much of the object state an oracle inspects — for Fortran, "how much of the
post-call module/COMMON state does the comparator look at."

---

## 6. Rung 3: observability-based coverage

**Idea.** MC/DC-adequate suites frequently execute the faulty code but the fault never
reaches an output — it is masked. Whalen et al. added a non-masking-path requirement, giving
[Observable MC/DC](https://greg4cr.github.io/pdf/13omcdc.pdf) (ICSE 2013). Implementation is
by *tagging semantics*: tag each atomic condition, propagate tags forward through the
program, and require that a tag reaches a monitored variable. Reported up to **88% more
faults detected** than MC/DC at the same nominal structural coverage.

**Relevance to a GPU port.** Masking is the exact phenomenon behind §4.3 and §4.4 — the
fault is executed, and its effect never reaches `h_out`/`u_out`. OMC/DC is the metric that
would have predicted those survivors without running a 93-mutant campaign.

**Fortran path.** There is no OMC/DC tool for Fortran. The tagging-semantics implementation
is, again, forward taint propagation — the same dfsan machinery as §5. If you build one
instrumentation, build this one: *tag each captured input element, and report which output
elements carry which tags.* For a stencil kernel the expected answer is structurally known
(output element `i` should carry tags from inputs `i-1, i, i+1`), which makes it a
**stencil-shape verification** as well as a coverage metric — it would catch a ported kernel
that quietly changed the stencil footprint, which is a plausible LLM error and one the
tolerance comparator only catches if the numbers happen to differ enough.

That last point is, I think, the highest-value novel idea in this memo for your pipeline.

---

## 7. Rung 5: fault injection

Fault injection was developed to answer a *resilience* question (will a cosmic-ray bit flip
corrupt my answer?) rather than a *test adequacy* question. But the machinery is the same as
mutation testing with a different fault model, and for GPU code it is the only mature
IR/binary-level fault tooling that exists.

**CPU / LLVM IR:**

- **LLFI** ([UBC](https://blogs.ubc.ca/karthik/files/2015/07/qrs-camera-ready.pdf)) —
  instruments LLVM IR with injection functions at candidate program points; faults trace back
  to source. The most-used academic tool. Successor **LLTFI**
  ([repo](https://github.com/DependableSystemsLab/LLTFI)) extends it to ML frameworks.
- **REFINE** ([paper](https://pure.qub.ac.uk/files/135663187/main.pdf),
  [repo](https://github.com/ggeorgakoudis/REFINE)) — extends the compiler itself with
  injection flags rather than post-hoc instrumenting; better accuracy and portability, and
  notably faster. If you pick one, pick this one.
- **FlipIt** ([paper](https://lukeo.cs.illinois.edu/files/2014_CaOlSn_FlipIt.pdf)) — an LLVM
  pass built specifically for **HPC and MPI** applications, with compile-time enumeration of
  injection sites and runtime activation from a user-supplied distribution. The closest fit
  to a Fortran HPC code by intent.
- **KULFI**, **PINFI** (binary-level, more accurate than LLFI), **Approxilyzer** (instruction-
  level resiliency analysis) round out the space.

**GPU:**

- **SASSIFI** ([NVlabs](https://github.com/NVlabs/sassifi)) — SASS-level injection, instruments
  all dynamic kernels.
- **NVBitFI** ([NVlabs](https://github.com/NVlabs/nvbitfi), [IEEE](https://ieeexplore.ieee.org/abstract/document/9505068))
  — the current tool. Injects into destination registers of dynamic thread-instructions,
  works on Volta/Turing and later, works with **pre-compiled libraries**, and instruments a
  single chosen kernel so it is much faster. This is the one that would apply to a ported
  kernel.

**Does any of this apply to Fortran?** LLFI/REFINE/FlipIt operate on LLVM IR, so yes —
*through flang*. I have not verified anyone has done this, and the Fortran runtime's
ABI-list handling is the likely friction point. NVBitFI operates on SASS and is therefore
**source-language agnostic**: it would work on an `nvfortran`- or flang-compiled OpenMP
target kernel today, with no Fortran-specific work at all.

**Honest assessment of value for this project.** As a *resilience* study, low priority. As a
*mutant surrogate for GPU code*, genuinely interesting: NVBitFI gives you a way to perturb
the GPU kernel's execution and ask whether the capture-replay oracle notices — i.e. an
adequacy measurement on the ported side of the port, which mutation testing of the Fortran
source cannot give you because the mutation happens before the compiler makes its decisions.

---

## 8. IR-level mutation

**Mull** ([docs](https://mull.readthedocs.io/en/latest/HowMullWorks.html),
[paper](https://arxiv.org/pdf/1908.01540), [libirm](https://github.com/mull-project/libirm))
mutates LLVM bitcode, hides every mutant behind a conditional flag, compiles **once**, and
selects mutants at runtime via LLVM JIT — mutant schemata done properly. Because it works on
IR, it is nominally language-agnostic: "any language that compiles to LLVM IR."

**The Fortran opportunity.** flang emits LLVM IR. Mull on flang bitcode is the only
realistic route to a *scalable* mutation tool for Fortran 2008, and would sidestep
universalmutator's regex fragility entirely.

**The Fortran obstacles, in order of severity:**

1. *Mutant→source mapping.* Mull reports mutations at IR instructions and maps back through
   debug info. Fortran array-section assignments lower into loops with compiler-generated
   temporaries; the mapping will be poor. A survived mutant you cannot locate in source is
   nearly useless for a human reviewer.
2. *Test-framework integration.* Mull expects to drive a test runner. Your "test" is
   `replay` + a Python comparator, so you would be using Mull's custom-runner path.
3. *Semantic level.* IR mutation cannot express the mutants that matter most here. §4.3's
   array-section bound mutants **do not exist at LLVM IR level** — by then the trip count is
   a single computed integer, and the nonconformance the source-level mutant created has
   already been resolved by the front end. This is the deep argument for source-level
   mutation of Fortran despite its inconvenience.

**The interesting frontier** is mutating **FIR/HLFIR**, flang's MLIR dialects, rather than
LLVM IR. HLFIR still represents Fortran-level concepts — variable allocation, array
assignment, intrinsics ([background](https://arxiv.org/pdf/2409.18824)) — so array-section
mutants *are* expressible there, and MLIR gives you a real rewriting framework. I know of no
one who has done this. It is a publishable-sized piece of work, not a weekend, but it is the
technically correct answer to "how should Fortran mutation testing work in 2026."

---

## 9. SMT and symbolic execution

**The promise.** Symbolic execution generates inputs that reach hard-to-reach states. Applied
to mutation testing, **SEMu** ([paper](https://arxiv.org/pdf/2001.02941)) uses KLEE to
reason about *state infection* (mutant diverges) and *propagation* (divergence reaches
output) to generate tests that kill stubborn mutants — exactly the §4.3/§4.4 survivors.
The infection/propagation framing is also precisely the OMC/DC observability condition,
expressed in constraints rather than tags.

**The reality for floating-point HPC code.** This is where I would set expectations low.

- Fortran has no symbolic execution front end. KLEE consumes LLVM bitcode, so flang is again
  the theoretical route, but I found no report of anyone running KLEE on flang output.
- Floating-point constraint solving is [the well-known bottleneck](https://srg.doc.ic.ac.uk/files/papers/klee-n-version-fp-ase-17.pdf):
  SMT solvers supporting FP theories are extremely slow, and [surveys of the state of the
  art](https://zbchen.github.io/files/apsec2022-2.pdf) conclude that even with Z3 + dReal +
  JFS hybrids, scaling is poor. A 5000-step time-stepping loop over a 100-point grid is far
  outside what any of this handles.
- Path explosion in stencil loops is severe and the loops are the whole program.

**Where SMT *is* worth using here, concretely:** not on the kernel's floating-point
semantics, but on its **integer index arithmetic**. The §4.3 mutants are index-bound faults,
and index arithmetic is linear integer arithmetic — the easiest thing an SMT solver does.
A checker that extracts array-section bounds from the source (fparser2 already gives you the
parse tree per `refactor.md`) and asks Z3 "are LHS and RHS section extents equal for all
`im >= 3`?" would have flagged all twelve survivors **statically, in milliseconds, with no
execution at all**. That is a far better cost/benefit than symbolic execution of the
numerics, and it composes with the manifest tooling already planned.

Equivalent-mutant detection via constraints ([Using Constraints for Equivalent Mutant
Detection](https://arxiv.org/pdf/1207.2234)) is the other tractable SMT application:
it would have identified the `dx = 1.0` mutants in §4.4 as conditionally equivalent.

---

## 10. Metamorphic testing (the missing rung)

Not on the user's list, but it is the standard answer to the oracle problem in scientific
computing and it interlocks with everything above. Instead of asking "is this output
correct," assert *relations* between outputs of related runs: [Metamorphic testing of
programs on partial differential equations](https://i.cs.hku.hk/~tse/Papers/2000s/jqpdeTR.pdf)
is the canonical case study; [Yan et al. 2025](https://onlinelibrary.wiley.com/doi/10.1002/stvr.1912)
is the current state for elliptic PDE solvers; [hierarchical MRs](https://homepages.uc.edu/~niunn/papers/SE4Science18.pdf)
organizes them.

For `demo/`'s shallow-water kernel the relations are immediate and cheap:

- **Translation invariance.** Periodic BCs + uniform grid ⇒ circularly shifting `h,u` must
  circularly shift the output identically. *This kills every mutant in §4.3* — a broken
  interior-stencil bound breaks translation equivariance even when it does not break the
  captured case.
- **Mass conservation.** `sum(h)` is invariant per tile; the driver already prints it.
- **Tiling invariance.** Already exploited by `tsunami.f90`'s `num_tiles`: N tiles must give
  exactly the 1-tile answer. Promote it from a smoke check to an oracle.
- **Scaling/dimensional relations.** Doubling `dx` with the appropriate `dt` scaling — the
  relation that catches §4.4.

MRs are the cheapest strong oracle available to this project, they need no reference data,
and unlike capture-replay they remain valid when the numbers legitimately change. LLMs are
also good at *proposing* them: [Variable Discovery with LLMs for Metamorphic Testing of
Scientific Software](https://link.springer.com/chapter/10.1007/978-3-031-35995-8_23).

---

## 11. LLMs

Three distinct roles, with different maturity.

**Mutant generation.** [A Comprehensive Study on LLMs for Mutation Testing](https://arxiv.org/html/2406.09843v3)
is the reference result: LLM mutants are more diverse and **behaviourally closer to real
bugs**, giving higher fault-detection rates than traditional operators — but worse on
non-compilability (+36.1pp), duplication (+13.1pp), and equivalence (+4.2pp). For Fortran
specifically, where no operator-based tool exists, an LLM prompted with "produce a plausible
GPU-porting bug in this kernel" is arguably the *best available* mutant generator, and the
non-compilability penalty is free to filter — `gfortran` rejects it and you move on. This is
also the mutant class you most want: mutants drawn from the distribution of *actual porting
errors*, not from a 1980s operator catalogue.

**Test/oracle generation.** [Mutation Testing via Iterative LLM-Driven Scientific
Debugging](https://arxiv.org/abs/2503.08182) and [MUTGEN](https://arxiv.org/abs/2506.02954)
both hypothesize-then-refine tests until stubborn mutants die — LLMs outperform classical
generators on fault detection at higher compute cost. HPC-specific:
[HPCAgentTester](https://arxiv.org/abs/2511.10860) uses a Recipe Agent + Test Agent critique
loop to generate OpenMP/MPI unit tests, reporting materially better compilation and
correctness rates than a single LLM.

**MR discovery** — see §10.

**The caution specific to this repository.** Using an LLM to generate the mutants that
validate an LLM-produced port creates a correlated blind spot: the model that cannot imagine
a fault also cannot write it. Keep an operator-based (mechanical, boring, uncorrelated)
mutant set as the floor and treat LLM mutants as an *additive* layer. And keep the mutation
harness on the oracle side of the trust boundary — the same reason `builder/stages.py` owns
the compile commands.

---

## 12. Recommended program for this repository

Ordered by value/effort, with what each buys.

**Tier 1 — do now, hours each**

1. **Checked-build gate.** Second replay of the corpus under `-fcheck=all -finit-real=snan
   -ffpe-trap=invalid,zero,overflow`. Buys: the twelve-mutant class in §4.3 that is currently
   undetectable. *Highest value item in this memo.*
2. **gcov on the replay corpus, in CI.** Buys: `refactor.md` step 3's "trust metric"; catches
   dead surface like `diff_upwind`; remember the `.gcda` naming trap. Fail the build on a
   coverage drop, not on an absolute threshold.
3. **A capture case with `dx /= 1.0`.** Buys: §4.4's dimensional-error class.
4. **Write-set vs compared-set check** (§5 route 2). Buys: a five-line approximation of
   checked coverage; catches "kernel writes state nobody compares."

**Tier 2 — days**

5. ~~Promote the mutation harness from §4 into `tools/`~~ — **done**, see
   [`tools/fmutate`](../tools/fmutate/README.md). Remaining work: wire it into CI per port
   attempt with `--fail-on-gap`, and add targets for the other codes under `codes/`. Track
   the **tolerance-blind gap** as the headline metric, not mutation score.
6. **Metamorphic relations** (§10): translation invariance, mass conservation, tiling. Buys:
   a strong oracle that needs no reference data and kills the §4.3 class independently of the
   compiler-flags fix.
7. **SMT section-conformance checker** (§9): fparser2 → Z3 over integer bounds. Buys:
   static, instant detection of the same class, at the point the agent writes the code rather
   than after the build.

**Tier 3 — research-scale, do if the project's goal is a publishable method**

8. **dfsan-based observability/tag propagation over flang IR** (§§5-6), used as a
   stencil-footprint verifier. The most novel item here.
9. **NVBitFI on the ported GPU kernel** (§7) — adequacy measurement on the device side.
10. **HLFIR-level mutation via MLIR** (§8) — the technically correct Fortran mutation tool
    that does not exist yet.

**What I would not do:** symbolic execution of the floating-point numerics (§9); predictive
mutation testing (§3.3); anything depending on `ifort` code coverage (§2).

---

## Sources

Adequacy metrics and oracles
- [A Brief Survey on Oracle-based Test Adequacy Metrics](https://arxiv.org/pdf/2212.06118)
- [Assessing Oracle Quality with Checked Coverage — Schuler & Zeller, ICST 2011](https://www.st.cs.uni-saarland.de/publications/files/schuler-icst-2011.pdf)
- [Checked coverage: an indicator for oracle quality — STVR 2013](https://www.st.cs.uni-saarland.de/publications/files/schuler-stvr-2013.pdf)
- [Observable Modified Condition/Decision Coverage — Whalen et al., ICSE 2013](https://greg4cr.github.io/pdf/13omcdc.pdf)
- [State Field Coverage: A Metric for Oracle Quality](https://arxiv.org/html/2510.03071v1)

Mutation testing
- [Fortran Wiki: Mutation testing frameworks](https://fortranwiki.org/fortran/show/Mutation+testing+frameworks)
- [A Fortran 77 interpreter for mutation analysis (Mothra)](https://dl.acm.org/doi/10.1145/29650.29669) · [A Fortran language system for mutation-based software testing](https://onlinelibrary.wiley.com/doi/abs/10.1002/spe.4380210704)
- [universalmutator — ICSE 2018 demo](https://mir.cs.illinois.edu/marinov/publications/GroceETAL18UniversalMutator.pdf) · [repo](https://github.com/agroce/universalmutator)
- [Mull it over: mutation testing based on LLVM](https://arxiv.org/pdf/1908.01540) · [How Mull works](https://mull.readthedocs.io/en/latest/HowMullWorks.html) · [libirm](https://github.com/mull-project/libirm)
- [Mutation Testing Optimisations using the Clang Front-end](https://arxiv.org/pdf/2210.17215)
- [Reducing mutation costs through uncovered mutants](https://onlinelibrary.wiley.com/doi/abs/10.1002/stvr.1534)
- [The Threat to the Validity of Predictive Mutation Testing](https://arxiv.org/pdf/2005.11532)
- [Equivalent mutants (Stryker)](https://stryker-mutator.io/docs/mutation-testing-elements/equivalent-mutants/) · [Using Constraints for Equivalent Mutant Detection](https://arxiv.org/pdf/1207.2234)
- [Approximate Transformations as Mutation Operators](https://users.ece.utexas.edu/~gligoric/papers/HaririETAL18ApproximateTransformationsAsMutationOperators.pdf)
- [Investigating test selection using Hook's mutation sensitivity testing](https://www.researchgate.net/publication/220308213_Investigating_test_selection_techniques_for_scientific_software_using_Hook's_mutation_sensitivity_testing)
- [An automated OpenMP mutation testing framework (MUPPET)](https://web.cs.ucdavis.edu/~rubio/includes/parco24.pdf)

Fault injection
- [LLFI: An Intermediate Code-Level Fault Injection Tool](https://blogs.ubc.ca/karthik/files/2015/07/qrs-camera-ready.pdf) · [LLTFI](https://github.com/DependableSystemsLab/LLTFI)
- [REFINE: Realistic Fault Injection via Compiler-based Instrumentation](https://pure.qub.ac.uk/files/135663187/main.pdf) · [repo](https://github.com/ggeorgakoudis/REFINE)
- [FlipIt: An LLVM Based Fault Injector for HPC](https://lukeo.cs.illinois.edu/files/2014_CaOlSn_FlipIt.pdf)
- [SASSIFI](https://github.com/NVlabs/sassifi) · [NVBitFI](https://github.com/NVlabs/nvbitfi) · [NVBitFI paper](https://ieeexplore.ieee.org/abstract/document/9505068)

Symbolic execution / SMT
- [Killing Stubborn Mutants with Symbolic Execution (SEMu)](https://arxiv.org/pdf/2001.02941)
- [Floating-Point Symbolic Execution: A Case Study in N-Version Programming](https://srg.doc.ic.ac.uk/files/papers/klee-n-version-fp-ase-17.pdf)
- [Symbolic Execution of Floating-point Programs: How far are we?](https://zbchen.github.io/files/apsec2022-2.pdf)

LLMs
- [A Comprehensive Study on LLMs for Mutation Testing](https://arxiv.org/html/2406.09843v3)
- [Mutation Testing via Iterative LLM-Driven Scientific Debugging](https://arxiv.org/abs/2503.08182) · [MUTGEN](https://arxiv.org/abs/2506.02954)
- [HPCAgentTester: Multi-Agent LLM for HPC Unit Test Generation](https://arxiv.org/abs/2511.10860)
- [Variable Discovery with LLMs for Metamorphic Testing of Scientific Software](https://link.springer.com/chapter/10.1007/978-3-031-35995-8_23)

Metamorphic testing
- [Metamorphic testing of programs on PDEs](https://i.cs.hku.hk/~tse/Papers/2000s/jqpdeTR.pdf)
- [Metamorphic Testing on Scientific Programs for Second-Order Elliptic PDEs (2025)](https://onlinelibrary.wiley.com/doi/10.1002/stvr.1912)
- [Hierarchical Metamorphic Relations for Testing Scientific Software](https://homepages.uc.edu/~niunn/papers/SE4Science18.pdf)
- [A semantic mutation metric for metamorphic relation adequacy](https://arxiv.org/pdf/2605.17437)

Fortran tooling
- [LLVM Fortran Levels Up: Goodbye flang-new, Hello flang](https://blog.llvm.org/posts/2025-03-11-flang-new/) · [Fully integrating Flang with standard MLIR](https://arxiv.org/pdf/2409.18824)
- [DataFlowSanitizer design](https://clang.llvm.org/docs/DataFlowSanitizerDesign.html)
- [gcovr FAQ (branch coverage caveats)](https://gcovr.com/en/stable/faq.html)
- [Intel: code coverage not available in ifx](https://community.intel.com/t5/Intel-Fortran-Compiler/Code-coverage-using-ifx/m-p/1506041) · [Intel Code Coverage Tool](https://www.intel.com/content/www/us/en/docs/fortran-compiler/developer-guide-reference/2023-0/code-coverage-tool.html)
- [pFUnit](https://github.com/Goddard-Fortran-Ecosystem/pFUnit) · [test-drive](https://github.com/fortran-lang/test-drive)
