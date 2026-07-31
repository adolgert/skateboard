# Skateboard

A minimum, firewalled harness that demonstrates an AI agent porting Fortran to
the GPU under professional assurances: an oracle it cannot edit, gates it cannot
weaken, and a ledger that records every decision.

- **`docs/skateboard.tex`** — why this is the smallest artifact that rolls end to
  end (the "skateboard", not a wheel of the eventual car).
- **`docs/architecture.tex`** — the concrete component/service architecture and
  the trust firewall, in plain language with diagrams.
- **`demo/`** — the working harness. Four services on three isolated Docker
  networks; the agent is the only untrusted component and can touch nothing but
  its patch. See [`demo/README.md`](demo/README.md) to run it.

First run: Claude ported the ch04 tsunami kernel to the GPU on the first attempt,
passed every gate including a held-out dataset, at roughly a 12× speedup over the
best CPU build, with zero human interventions.

The `codes/tsunami` example (Curcic, *Modern Fortran*) is an external dependency
and is not vendored here; `demo/` bundles the specific sources it ports.
