# Two runs, extracted

Slide-ready material from the campaigns recorded in `../experiments/`. One port
that **compiled, ran on the GPU, and computed the wrong physics** (qwen2.5:14b),
and one that was **accepted at 8.9× the best CPU baseline** (Claude Sonnet 5).

Full source for each is in `examples/`; the excerpts below are trimmed for slides.

## Provenance

| | qwen attempt 4 | Sonnet attempt 2 |
|---|---|---|
| Campaign | `qwen20` (20-attempt study) | `run1` (model matrix) |
| Ledger row | `qwen20-0004` | `run1-0003` |
| Ledger file | `../experiments/qwen-20-2026-07-30.csv` | `../experiments/model-matrix-2026-07-30.csv` |
| Source recovered from | `demo_repo` volume, branch `attempt/qwen-stdpar_managed-4` | `/work/sonnet-stdpar_managed-2/src` in the stopped `demo-builder-1` container |
| `src_sha` | `b0e2cc0abacc` | `eb274ac87ea1` |
| Oracle policy | `414a8ca97a15` | `414a8ca97a15` (same) |

Both were extracted on 2026-07-31. The Sonnet source had **no durable home** —
`init_repo()` wiped `demo_repo` when the qwen campaign started, so the only
surviving copy was the stopped builder container's writable layer, which one
`docker compose down` would have destroyed. The files in `examples/` are now the
record.

## The baseline being ported

`demo/work/src/mod_kernel.f90` — whole-array Fortran, runs on the CPU:

```fortran
subroutine step(h, u)
  real(real32), intent(inout) :: h(:), u(:)

  ! momentum: uses old u and old h
  u = u - (u * diff_centered(u) + g * diff_centered(h)) / dx * dt

  ! continuity: uses the just-updated u and old h
  h = h - diff_centered(u * (hmean + h)) / dx * dt

end subroutine step
```

with the stencil (`mod_diff.f90`), 2nd-order centered, periodic:

```fortran
dx(1)       = x(2) - x(im)
dx(im)      = x(1) - x(im-1)
dx(2:im-1)  = x(3:im) - x(1:im-2)
dx = 0.5 * dx
```

The agent may edit **only** `src/mod_kernel.f90`. Note the continuity line: the
centered difference is taken of the whole **flux** `u*(hmean+h)`, so the forward
term is `u(i+1)*(hmean+h(i+1))` — forward velocity paired with forward height.
That pairing is where qwen fails.

---

# Example 1 — qwen2.5:14b, attempt 4: compiles, runs on the GPU, wrong answer

**Ledger row `qwen20-0004`:**

| build | device_proof | kernels_launched | memcheck | racecheck | initcheck | compare_visible | verdict |
|---|---|---|---|---|---|---|---|
| pass | pass | 10 | pass | pass | pass | **fail** | **fail** |

Every gate that a compiler or a sanitizer can enforce passed. The oracle is what
stopped it. No timing was recorded — the harness never reaches the performance
stage on a port that fails correctness.

**The generated kernel** (`examples/qwen-attempt04-mod_kernel.f90`):

```fortran
subroutine step(h, u)
  real(real32), intent(inout) :: h(:), u(:)

  integer(int32) :: n, i
  real(real32) :: du(size(u)), dh(size(h))
  n = size(u)

  ! momentum: uses old u and old h
  do concurrent (i = 1:n)
    if (i == 1) then
      du(i) = u(2) - u(n)
    else if (i == n) then
      du(i) = u(1) - u(n-1)
    else
      du(i) = u(i+1) - u(i-1)
    end if
    du(i) = 0.5 * du(i)
  end do

  u = u - (u * du + g * diff_centered(h)) / dx * dt

  ! continuity: uses the just-updated u and old h
  do concurrent (i = 1:n)
    if (i == 1) then
      dh(i) = u(1) * (hmean + h(2)) - u(n) * (hmean + h(n))
    else if (i == n) then
      dh(i) = u(n) * (hmean + h(1)) - u(n-1) * (hmean + h(n-1))
    else
      dh(i) = u(i) * (hmean + h(i+1)) - u(i-1) * (hmean + h(i-1))
    end if
    dh(i) = 0.5 * dh(i)
  end do

  h = h - dh / dx * dt

end subroutine step
```

It reads well. The comments are correct, the loop structure is right, the
periodic wrap-around is handled explicitly, the momentum update genuinely does
precede the continuity update, and it offloads.

**The bug — one index, in the forward term of the mass flux:**

| | interior | `i = 1` | `i = n` |
|---|---|---|---|
| reference | `u(i+1)*(hmean+h(i+1))` | `u(2)*(hmean+h(2))` | `u(1)*(hmean+h(1))` |
| qwen | `u(i)`&nbsp;`*(hmean+h(i+1))` | `u(1)*(hmean+h(2))` | `u(n)*(hmean+h(1))` |

The height index advances; the velocity index does not. The backward term
`u(i-1)*(hmean+h(i-1))` is correct in all three branches, so the error is not a
uniform shift that might cancel — it is an asymmetric stencil that solves a
different equation.

**Slide framing — why this is the whole argument:**

- The **momentum** equation is right, so `u` matches the reference to ~5e-7
  relative — inside tolerance.
- The **continuity** equation is wrong, so `h` is off by up to ~1.8e-2 absolute
  and ~68 relative where the true height crosses zero.
- The output is still smooth, still plausible, still a wave. Nothing about
  looking at it says "wrong."
- Attempts 4 through 20 — seventeen consecutive tries — all compiled, all
  launched kernels, all failed the oracle. The comparator told the model every
  time that `u` passed and `h` failed. It never localised the bug.

> *(The 5e-7 / 1.8e-2 / 68 figures are quoted from `early_trials.tex`; the ledger
> CSV stores only per-attempt verdicts, not per-variable error magnitudes, so
> they are not independently re-derivable from the archived data. See "Two
> corrections" below.)*

---

# Example 2 — Claude Sonnet 5, attempt 2: accepted at 8.9×

**Ledger row `run1-0003`:**

| build | device_proof | kernels | memcheck | racecheck | initcheck | visible | holdout | port_s | speedup | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| pass | pass | 30 | pass | pass | pass | pass | **pass** | 1.2245 | **8.899×** | **ACCEPTED** |

Baselines for that campaign: best-CPU 10.896 s, naive-`stdpar` 7.950 s.

**The accepted kernel** (`examples/sonnet-attempt02-mod_kernel.f90`):

```fortran
subroutine step(h, u)
  real(real32), intent(inout) :: h(:), u(:)
  real(real32), allocatable :: du(:), dh(:), u_old(:), hu(:), duh(:)
  integer(int32) :: n, i, ip, im

  n = size(h)
  allocate(du(n), dh(n), u_old(n), hu(n), duh(n))

  ! Save old u, since the momentum update must use OLD u and OLD h only.
  do concurrent (i = 1:n)
    u_old(i) = u(i)
  end do

  ! --- momentum: uses old u and old h ------------------------------------
  do concurrent (i = 1:n)
    ip = mod(i, n) + 1
    im = mod(i - 2 + n, n) + 1
    du(i) = 0.5_real32 * (u_old(ip) - u_old(im))
    dh(i) = 0.5_real32 * (h(ip)     - h(im))
  end do

  do concurrent (i = 1:n)
    u(i) = u_old(i) - (u_old(i) * du(i) + g * dh(i)) / dx * dt
  end do

  ! --- continuity: uses the just-updated u and old h ---------------------
  do concurrent (i = 1:n)
    hu(i) = u(i) * (hmean + h(i))          ! form the flux FIRST
  end do

  do concurrent (i = 1:n)
    ip = mod(i, n) + 1
    im = mod(i - 2 + n, n) + 1
    duh(i) = 0.5_real32 * (hu(ip) - hu(im))  ! then difference it
  end do

  do concurrent (i = 1:n)
    h(i) = h(i) - duh(i) / dx * dt
  end do

end subroutine step
```

**What it does differently from qwen — the contrast slide:**

1. **Materialises the flux before differencing it.** `hu(i) = u(i)*(hmean+h(i))`
   in its own loop, then `hu(ip) - hu(im)`. The index pairing can't drift,
   because there is only one index. qwen fused the two steps and lost the
   pairing.
2. **`mod` arithmetic instead of `if` branches** for the periodic wrap — one
   expression covering interior and boundary, so there is no boundary case to
   get separately wrong. qwen wrote three branches and got the same bug in all
   three.
3. **Explicit `u_old` copy** to honour the "momentum uses OLD u" contract without
   relying on whole-array assignment semantics.
4. **Updates in place**, keeping the 2M-element managed arrays resident on the
   device. This is what Haiku's accepted-but-slow port got wrong: it kept whole
   array copies (`h_old = h`, `u = u_new`) that migrate host⇄device every step,
   landing at 0.13× — slower than the CPU.

**The repair loop — attempt 1 failed to build.** Sonnet's first attempt used a
`block` construct inside `do concurrent` to scope its wrap-around temporaries:

```fortran
do concurrent (i = 1:n)
  block
    integer(int32) :: ipp, imm
    ipp = i + 1
    if (ipp > n) ipp = 1
    imm = i - 1
    if (imm < 1) imm = n
    du(i) = 0.5_real32 * (u(ipp) - u(imm))
    dh(i) = 0.5_real32 * (h(ipp) - h(imm))
  end block
end do
```

nvfortran's `-stdpar=gpu` offload cannot compile a `block` inside `do
concurrent`. The build gate caught it, the structured compiler error went back to
the model, and attempt 2 replaced the construct with hoisted `ip`/`im` scalars
and `mod` arithmetic — and was accepted. Ledger row `run1-0002` (`build=fail`,
everything downstream `NA`) followed by `run1-0003` (ACCEPTED) is the loop
working, in two rows. Source: `examples/sonnet-attempt01-mod_kernel.f90`.

---

# Example 3 — codestral, attempt 2: edited the code, parallelised nothing

**Ledger row `codestral30-0002`** (`../experiments/codestral-30-2026-07-31.csv`):

| build | device_proof | kernels_launched | compare | verdict |
|---|---|---|---|---|
| pass | **fail** | **0** | not reached | fail |

This is the gate firing. The port compiles cleanly and would have gone to the
oracle — but it never ran on the GPU, so the harness stopped it first.

Proving a negative is awkward, so the case rests on three *positive* artifacts.

## Artifact 1 — the file was substantially rewritten

58 changed lines against the pristine baseline; `src_sha` `e29f7caa59a1` vs the
baseline kernel. The model did real work:

```fortran
subroutine step(h, u)
  real(real32), intent(inout) :: h(:), u(:)
  integer(int32) :: i, n, im
  real(real32) :: du_dx(size(u)), dh_dx(size(h))

  n = size(h)
  im = n - 1

  du_dx(1) = u(2) - u(im)
  du_dx(n) = u(1) - u(im-1)
  !$omp target teams distribute parallel do simd map(to: u, im, n) map(du_dx)
  do i = 2, im
    du_dx(i) = u(i+1) - u(i-1)
    dh_dx(i) = h(i+1) - h(i-1)
  end do
  !$omp end target teams distribute parallel do simd
  du_dx = 0.5 * du_dx
  dh_dx = 0.5 * dh_dx

  !$omp target teams distribute parallel do simd map(to: u, du_dx, dh_dx, g, dx, dt) map(u)
  do i = 1, n
    u(i) = u(i) - (u(i)*du_dx(i) + g*dh_dx(i)) / dx * dt
  end do
  !$omp end target teams distribute parallel do simd
  ...
```

Whole-array assignments broken into indexed loops, temporaries introduced,
periodic boundaries handled, three offload regions with explicit `map` clauses.
Its own NOTES line says: *"Added OpenMP offload directives to explicitly compute
the differences, and then update `u` and `h`."* It believes it produced a GPU
port. Full file: `examples/codestral-attempt02-mod_kernel.f90`.

## Artifact 2 — the compiler reports zero accelerator regions

Same compiler, same flags (`-O2 -stdpar=gpu -gpu=cc89,mem:managed -Minfo=accel`),
three different kernels. `-Minfo=accel` asks nvfortran to name every GPU region it
generates. Full output in `examples/codestral-attempt02-minfo-accel.txt`:

```
=== A. codestral attempt 2  (device_proof FAIL, kernels_launched = 0) ===
(no accelerator regions reported -- the compiler generated no GPU code for step)

=== B. codestral attempt 1  (device_proof pass, kernels_launched = 10) ===
step:
     34, Generating present(u(:),du_dx(:),dh_dx(:))
         Generating NVIDIA GPU code
         35, !$acc loop gang, vector(128) ! blockidx%x threadidx%x
     44, Generating present(du_dx(:),u(:),h(:))
         Generating NVIDIA GPU code
         45, !$acc loop gang, vector(128) ! blockidx%x threadidx%x

=== C. Sonnet accepted port (device_proof pass, kernels_launched = 30) ===
step:
     43, Generating NVIDIA GPU code
         43, Loop parallelized across CUDA thread blocks, CUDA threads(128)
     49, Generating NVIDIA GPU code
         49, Loop parallelized across CUDA thread blocks, CUDA threads(128)
     ...
```

The silence in (A) is not absence of evidence — the same invocation is
demonstrably willing to report regions, and does so for (B) and (C). For attempt 2
it has nothing to report.

## Artifact 3 — why: the directives are for a different build

`-stdpar=gpu` offloads `do concurrent`, and nvfortran also accepts `!$acc`. It
does **not** enable OpenMP target offload — that needs `-mp=gpu`, which is the
harness's *other* rung (`omp_target`). A controlled test on this exact compiler:

```fortran
subroutine acctest(a, n)        subroutine omptest(a, n)
  !$acc parallel loop             !$omp target teams distribute parallel do
  do i = 1, n                     do i = 1, n
    a(i) = a(i) * 2.0               a(i) = a(i) * 3.0
```
```
$ nvfortran -O2 -stdpar=gpu -gpu=cc89,mem:managed -Minfo=accel -c t.f90
acctest:
         Generating NVIDIA GPU code
          5, !$acc loop gang, vector(128) ! blockidx%x threadidx%x
                      <- omptest: nothing
```

`acctest` offloads; `omptest` produces no GPU code and no diagnostic. And array
syntax does not offload either:

```
$ nvfortran -O2 -stdpar=gpu ... -Minfo=accel -c arraysyntax_vs_doconc.f90
doconc:
     11, Generating NVIDIA GPU code
     11, Loop parallelized across CUDA thread blocks, CUDA threads(128)
                      <- arraysyntax: nothing
```

So every construct in attempt 2 — `!$omp target` regions, plain `do i = 1, n`
loops, whole-array assignments — runs on the **host**. There is no path by which
any of it reaches the device.

## What this example is for

The failure is not a syntax error and not a wrong number. The model chose a
GPU programming model the build does not enable, and **nvfortran accepted the file
without a single warning** — unknown `!$omp` directives are legal comments. Nothing
in the compile step distinguishes this from a successful port. It is only caught
because the harness runs the binary under `NVCOMPILER_ACC_NOTIFY=1`, counts kernel
launches, and requires the count to exceed zero.

Contrast with attempt 1, which made an adjacent guess — OpenACC instead of
OpenMP — that this profile *does* honour. Same class of error, opposite outcome:
attempt 1's loops genuinely ran on the GPU and it advanced to the oracle, where it
failed on physics instead. Two attempts apart, the difference between a real GPU
port and a CPU program wearing GPU annotations came down to which directive
dialect the model happened to reach for.

---

# Two corrections to `early_trials.tex`

Found while extracting; both are claims in the paper.

1. **"the error was byte-for-byte identical on attempt 4 and attempt 20"** —
   not accurate at the source level. The two files differ:

   ```diff
   -      du(i) = 0.5 * du(i)     ! attempt 4: scale inside the loop
   +    du = 0.5 * du             ! attempt 20: scale as a whole-array op
   ```

   The *bug* is identical — same off-by-one on the forward velocity index, same
   in all three branches. Only the placement of the 0.5 scaling moved. Suggested
   rewording: "the bug was identical on attempt 4 and attempt 20" or "textually
   unchanged apart from where the 0.5 factor was applied."

2. **"Sonnet/Opus updated in place"** (`experiments/README.md`, finding 3) —
   there is no Opus run anywhere in this repo: zero `opus` rows in either ledger
   CSV, no `attempt/opus-*` branch, no `/work/opus-*` workspace. `opus` is a
   registered key in `agent-runner/app.py` that was never exercised. Either drop
   the mention or run it.
