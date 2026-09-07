# Documentation guide

Documentation reviewed against the implementation on 2026-09-06.

## Current guidance

| Document | Use it for |
| --- | --- |
| [Coworker handoff](coworker-handoff.md) | Start here when applying Skateboard to a new codebase; supported scope and review obligations |
| [Evidence contract](evidence-contract.md) | What each claim establishes, its dependencies, and its limits |
| [Installation](pi-install.md) | Host setup and starting a deployment |
| [Onboarding](onboarding.md) | Build, capture, replay, independent original comparison, and promotion |
| [Porting manual](pi-users-manual.md) | Agent tools, preconditions, status, and reviewer commands |
| [Deployment README](../deploy/README.md) | Containers, scripts, isolation, and machine qualification |
| [Architecture](architecture.pdf) ([source](architecture.tex)) | Current service and evidence architecture, with proposed extensions distinguished |
| [Original-reference example](examples/original-reference.yaml) | A reviewed contract for comparing an onboarded program with its preserved original |

The [2026-09-06 qualification record](builder-qualification-2026-09-06.md)
identifies a tested CPU deployment image. It reports GPU qualification as
unavailable; it does not qualify another machine or another image.

## Historical records and research

These documents explain earlier experiments and design decisions. Their old
commands, claim formats, acceptance labels, and tool descriptions are not the
current operating procedure.

| Document | Status |
| --- | --- |
| [Pre-gateway inventory](inventory.md) | Snapshot at commit `ba5a627`, before the claim ledger existed |
| [Per-code dependencies](per-code-dependencies.md) | 2026-08-28 audit; its closing resolution section is updated separately |
| [Generalization plan](generalize-plan.md) | Original plan and implementation notes; current progress is noted at the top |
| [Coverage and mutation research](coverage-testing.md) | Research memo and an earlier mutation experiment; includes a current repository-status note |
| [Original scope proposal](skateboard.pdf) ([source](skateboard.tex)) | Three-month proposal, annotated with current implementation status |
| [Example session](example-session.md) | Historical transcript excerpt, not a runnable checklist or verified acceptance record |
| [Extracted runs](run_examples.md) | Annotated source from the July 2026 campaigns |
| [Early trials](early_trials.pdf) ([source](early_trials.tex)) | July 2026 batch-harness results, corrected against the retained evidence |
| [Experiment ledgers](../experiments/README.md) | Historical CSV records; their acceptance labels predate the current evidence policy |

`notes/` is maintained separately and excluded from Git. References to it in
historical documents provide provenance; a coworker should not need it to
follow the current handoff.

## Rebuilding the PDFs

From the repository root, with a LaTeX installation that includes the packages
used by the sources:

```sh
latexmk -cd -pdf -interaction=nonstopmode -halt-on-error docs/architecture.tex docs/skateboard.tex docs/early_trials.tex
```

Keep each PDF in step with its `.tex` source when changing these documents.
