# Skateboard

A firewalled harness in which an AI agent ports Fortran to the GPU under
assurances a reviewer can check: an oracle it cannot edit, gates it cannot
weaken, and an append-only ledger recording every claim.

The agent works in a container with the compilers and the GPU, and nothing it
does there is evidence. To make progress it submits its edit to the **gateway**,
the one service it can reach. The gateway holds its own copy of the code, runs
each check itself — the analyzer, the **builder**, the **oracle** — and writes
the verdict to the **ledger** as a claim naming the exact tree, the strategy,
the code's manifest, and the session that asked. It refuses a check whose
preconditions are not yet claimed. When every required claim is present on one
tree, the port is accepted, and a reviewer reads the ledger instead of the
transcript. A code the harness has never seen is brought in the same way: an
**onboarding** session writes that code's build, drivers, and description, and
each of those has a check that says whether it is right.

- **[`docs/pi-install.md`](docs/pi-install.md)** — from a fresh checkout to a
  running stack and a session.
- **[`docs/pi-users-manual.md`](docs/pi-users-manual.md)** — what a porting
  session looks like, gate by gate, and how to read what comes back.
- **[`docs/onboarding.md`](docs/onboarding.md)** — how a new Fortran code
  becomes one whose regions can be ported.
- **[`docs/generalize-plan.md`](docs/generalize-plan.md)** — the plan that made
  the gateway path work for more than one code, and what remains open.
- **`docs/skateboard.tex`** — why this is the smallest artifact that rolls end
  to end (the "skateboard", not a wheel of the eventual car).
- **`docs/architecture.tex`** — the component and service architecture and the
  trust firewall, with diagrams.

What each directory holds:

- **`programs/`** — one directory per code: its manifest, tracked baseline
  sources, region specs, datasets, and reference captures.
- **`services/`** — the builder (the only container with the GPU, which builds
  a submitted tree with that tree's own makefile) and the oracle (sealed,
  holding the recorded answers and the tolerance policy).
- **`equivalent/`** — the gateway, the ledger, the checks it dispatches, the
  analyzer, the capture format, the strategy and manifest readers, and the
  `ledger` command line.
- **`deploy/`** — the running stack: the session container, the gateway, and
  the two services above, on isolated Docker networks, with the scripts that
  start, check, and reset it. See [`deploy/README.md`](deploy/README.md).
- **`pi-extension/`** — the session-side extension that turns each gateway
  action into a tool. It decides nothing.
- **`tools/`** — standalone instruments: `fmutate` (Fortran mutation testing)
  and `regionharness` (the earlier Serialbox capture-replay work).
- **`docs/`** — the documents above, plus the write-ups of the recorded runs.
- **`experiments/`** — ledger exports from past campaigns, with what each one
  showed.

First run, kept as history: Claude ported the ch04 tsunami kernel to the GPU on
the first attempt, passed every gate including a held-out dataset, at roughly a
12× speedup over the best CPU build, with no human intervention.

The `codes/tsunami` example (Curcic, *Modern Fortran*) is an external dependency
and is not vendored here; `programs/tsunami/baseline` holds the specific sources
it ports.
