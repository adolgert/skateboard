# Code map

The repository separates evidence policy, checks, HTTP transport, and execution.
Start with the owner below when changing a behavior.

| Functionality | Owner |
| --- | --- |
| Actions, emitted predicates, prerequisites, acceptance roles, receipt visibility | [`equivalent/ledger/workflow.py`](../equivalent/ledger/workflow.py) |
| Manifest-dependent acceptance requirements | [`equivalent/ledger/acceptance.py`](../equivalent/ledger/acceptance.py) |
| Public action table and request settings validation | [`equivalent/ledger/table.py`](../equivalent/ledger/table.py) |
| Action-to-Python-function binding | [`equivalent/gateway/dispatch.py`](../equivalent/gateway/dispatch.py) |
| HTTP schemas, authentication, routing, and execution locks | [`equivalent/gateway/app.py`](../equivalent/gateway/app.py) |
| Resolve, restore, gate, dispatch, and record a check request | [`equivalent/gateway/run.py`](../equivalent/gateway/run.py) |
| Backend identities and retained executable verification | [`equivalent/gateway/verification.py`](../equivalent/gateway/verification.py) |
| Inputs to a check; explicit verdict and artifact declarations | [`components/context.py`](../equivalent/components/context.py), [`components/result.py`](../equivalent/components/result.py) |
| Shared build recipes and verdicts | [`equivalent/components/building.py`](../equivalent/components/building.py) |
| Onboarding/porting provenance policy | [`equivalent/components/phase.py`](../equivalent/components/phase.py) |
| Typed executable records and stored build-claim codecs | [`equivalent/ledger/artifacts.py`](../equivalent/ledger/artifacts.py) |
| Ledger persistence, freshness, and status | [`ledger/store.py`](../equivalent/ledger/store.py), [`ledger/evidence.py`](../equivalent/ledger/evidence.py), [`ledger/status.py`](../equivalent/ledger/status.py) |
| Numerical comparison | [`equivalent/capture/compare.py`](../equivalent/capture/compare.py) |
| Builder HTTP API | [`services/builder/app.py`](../services/builder/app.py) |
| Compilation, replay/capture, sanitization, properties, mutation, timing | `services/builder/stage_build.py`, `stage_replay.py`, `stage_sanitizer.py`, `stage_property.py`, `stage_mutation.py`, `stage_timing.py` |
| Builder case files and workspace policy | [`builder/case_io.py`](../services/builder/case_io.py), [`builder/stage_runtime.py`](../services/builder/stage_runtime.py) |
| Pure Fortran mutation generator, shared with the standalone CLI | [`services/builder/mutation_source.py`](../services/builder/mutation_source.py) |

To add a check, define its policy once in `ledger/workflow.py`, implement its
`check(ctx, config)` in `components/`, and bind the callable in
`gateway/dispatch.py`. Acceptance lists, predicate metadata, producer names,
and handler policy are derived from the catalog. Declare measured executables
explicitly in the result; diagnostic dictionaries do not discover evidence.

The service images remain independent installations. They copy the canonical
numerical comparator into their images, and the builder ships the same mutation
generator the standalone tool imports. Wire-contract parity tests protect
independently defined service response types.

`equivalent/tests/test_imports.py` enforces package layering.
`equivalent/tests/test_module_imports.py` checks runtime module cycles, including
function-local imports, while separating `TYPE_CHECKING` imports. The workflow
catalog also rejects prerequisite cycles, which would make checks unreachable.
