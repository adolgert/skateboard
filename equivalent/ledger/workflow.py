"""The workflow catalog shared by checks, ledger readers, and user interfaces.

Start here to add an action or change its evidence policy. Each action
owns its emitted predicates, prerequisites, phase, reuse policy, tool,
and acceptance role. The acceptance lists, public action table, predicate
registry, and handler metadata are projections of these definitions.
Python callables stay in gateway.dispatch so this catalog remains usable
without the gateway or components installed.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

ONBOARDING = "onboarding"
PORTING = "porting"
PHASES = (ONBOARDING, PORTING)
FINISHED_WORD = {ONBOARDING: "ONBOARDED", PORTING: "ACCEPTED"}


class DetailLevel(Enum):
    VERDICT_ONLY = "verdict_only"
    FULL = "full"


class AcceptanceRole(Enum):
    REQUIRED = "required"
    PROPERTIES = "properties"
    NONE = "none"


@dataclass(frozen=True)
class PredicateDefinition:
    name: str
    description: str
    agent_detail: DetailLevel = DetailLevel.FULL


@dataclass(frozen=True)
class ActionDefinition:
    name: str
    phase: str
    component: str
    tool: str
    predicates: tuple[PredicateDefinition, ...]
    requires: tuple[str, ...] = ()
    deterministic: bool = True
    subject_kind: str = "tree"
    acceptance: AcceptanceRole = AcceptanceRole.REQUIRED
    config_keys: tuple[str, ...] = ()
    needs: tuple[str, ...] = ()

    @property
    def emits(self) -> tuple[str, ...]:
        return tuple(predicate.name for predicate in self.predicates)


ACTIONS = (
    ActionDefinition(
        name='sese_check', phase=PORTING,
        component='analyzer:check_sese', tool='sese_check',
        predicates=(
            PredicateDefinition('sese/verified',
                'Static analyzer checks the Fortran anchor and declared Fortran callees for '
                'goto, early return, entry, and stop, on the candidate set. Explicitly declared '
                'opaque foreign sources are not analyzed. Does not check that '
                "the spec's declared footprint matches the code -- that needs real static-analysis "
                "tooling this repository doesn't yet run generically; see "
                'equivalent/components/sese_check.py.'
            ),
        ),
    ),
    ActionDefinition(
        name='build_replay', phase=PORTING,
        component='builder:/v1/build', tool='builder',
        predicates=(
            PredicateDefinition('build/replay',
                "The replay harness compiles against the strategy's flags, on the tree."
            ),
        ),
        requires=('sese/verified',),
        needs=('builder',),
    ),
    ActionDefinition(
        name='run_replay', phase=PORTING,
        component='builder:/v1/run', tool='builder',
        predicates=(
            PredicateDefinition('gpu/executed',
                'The device-proof mechanism observed at least one kernel launch, on the tree.'
            ),
        ),
        requires=('build/replay',),
        needs=('builder',),
    ),
    ActionDefinition(
        name='sanitize', phase=PORTING,
        component='builder:/v1/sanitize', tool='compute-sanitizer',
        predicates=(
            PredicateDefinition('sanitize/memcheck',
                'compute-sanitizer memcheck ran clean, on the tree.'
            ),
            PredicateDefinition('sanitize/racecheck',
                'compute-sanitizer racecheck ran clean, on the tree.'
            ),
            PredicateDefinition('sanitize/initcheck',
                'compute-sanitizer initcheck ran clean, on the tree.'
            ),
        ),
        requires=('gpu/executed',),
        needs=('builder',),
    ),
    ActionDefinition(
        name='regression_visible', phase=PORTING,
        component='oracle:/v1/compare', tool='oracle',
        predicates=(
            PredicateDefinition('regression/visible',
                'Oracle comparison against the visible capture set, on the tree; detail is the '
                'per-case breakdown.'
            ),
        ),
        requires=('sanitize/memcheck', 'sanitize/racecheck', 'sanitize/initcheck', 'gpu/executed'),
        needs=('oracle',),
    ),
    ActionDefinition(
        name='property_check', phase=PORTING,
        component='builder:/v1/properties', tool='builder',
        predicates=(
            PredicateDefinition('regression/property',
                "The code's own property module passed, on the tree, at the recorded seed. Where "
                'the regression checks compare a port against captured answers, this one searches '
                'for an input on which an invariant the code states does not hold; detail is the '
                'seed, how many examples were drawn, and what the run printed.'
            ),
        ),
        requires=('regression/visible',),
        acceptance=AcceptanceRole.PROPERTIES,
        config_keys=('seed', 'max_examples'),
        needs=('builder',),
    ),
    ActionDefinition(
        name='regression_holdout', phase=PORTING,
        component='oracle:/v1/compare', tool='oracle',
        predicates=(
            PredicateDefinition('regression/holdout',
                'Oracle comparison against the held-out capture set, on the tree; no per-case '
                'detail ever leaves the oracle.',
                agent_detail=DetailLevel.VERDICT_ONLY,
            ),
        ),
        requires=('regression/visible',),
        needs=('builder', 'oracle'),
    ),
    ActionDefinition(
        name='program_regression', phase=PORTING,
        component='builder:/v1/time', tool='builder',
        predicates=(
            PredicateDefinition('program/regression',
                "The code's own program, run at the size its manifest declares, wrote what the "
                "baseline program wrote, under the code's tolerance policy; detail is the "
                'per-output breakdown. On the tree.'
            ),
        ),
        requires=('regression/holdout', 'timing/baseline'),
        needs=('builder',),
    ),
    ActionDefinition(
        name='time_port', phase=PORTING,
        component='builder:/v1/time', tool='builder',
        predicates=(
            PredicateDefinition('timing/port',
                "End-to-end isolated-job wall-clock timing of the ported binary, on the tree "
                "(including workspace and container setup and teardown). Every timed repetition's "
                "program outputs are compared again with the baseline program's, under the code's "
                'tolerance policy: program/regression compares one run, and this is the only check '
                'that runs the program more than once, so a port whose answers drift between runs '
                'at timing size is caught here.'
            ),
        ),
        requires=('program/regression',),
        deterministic=False,
        config_keys=('repeats',),
        needs=('builder',),
    ),
    ActionDefinition(
        name='time_baseline', phase=PORTING,
        component='builder:/v1/time', tool='builder',
        predicates=(
            PredicateDefinition('timing/baseline',
                'End-to-end isolated-job wall-clock timing of the pristine baseline build, on the '
                'baseline tree (including workspace and container setup and teardown); its '
                "program's own outputs are stored as the capture set a port's program run is "
                'compared against.'
            ),
        ),
        deterministic=False,
        subject_kind='baseline_tree',
        acceptance=AcceptanceRole.NONE,
        config_keys=('repeats',),
        needs=('builder',),
    ),
    ActionDefinition(
        name='performance_check', phase=PORTING,
        component='gateway:performance', tool='performance_check',
        predicates=(
            PredicateDefinition('performance/speedup',
                "The port's median end-to-end isolated-job wall-clock time meets the manifest's required "
                "speedup over the baseline, using at least five samples from each recorded timing "
                "claim. This is a same-builder comparative threshold, not a statistical confidence "
                'or cross-machine performance guarantee; GPU exclusivity is not asserted. On the tree.'
            ),
        ),
        requires=('timing/port', 'timing/baseline'),
    ),
    ActionDefinition(
        name='manifest_check', phase=ONBOARDING,
        component='gateway:manifest_check', tool='manifest_check',
        predicates=(
            PredicateDefinition('manifest/valid',
                'The manifest the tree carries describes the code completely: every path it names '
                'is in the tree, every floating-point output has a tolerance band, and the two '
                'datasets are different runs. On the tree.'
            ),
        ),
    ),
    ActionDefinition(
        name='harness_build', phase=ONBOARDING,
        component='builder:/v1/build', tool='builder',
        predicates=(
            PredicateDefinition('harness/builds',
                "Every target the tree's manifest declares builds under both the baseline strategy "
                "and the port strategy, with each strategy's flags proven to have reached every "
                'compile. On the tree.'
            ),
        ),
        requires=('manifest/valid',),
        needs=('builder',),
    ),
    ActionDefinition(
        name='harness_capture', phase=ONBOARDING,
        component='builder:/v1/capture', tool='builder',
        predicates=(
            PredicateDefinition('harness/captured',
                'The capture program wrote every dataset the manifest declares; each case holds '
                'exactly the variables the region declares, of the declared type and rank; and the '
                'visible and held-out inputs are not the same run. The sets it wrote are stored in '
                "the ledger and named in this claim's materials. On the tree."
            ),
        ),
        requires=('harness/builds',),
        needs=('builder',),
    ),
    ActionDefinition(
        name='harness_replay', phase=ONBOARDING,
        component='builder:/v1/run', tool='builder',
        predicates=(
            PredicateDefinition('harness/replays',
                'The replay driver, built the way the baseline is built, reproduces every captured '
                'output bitwise from the captured inputs. On the tree.'
            ),
        ),
        requires=('harness/captured',),
        needs=('builder',),
    ),
    ActionDefinition(
        name='harness_determinism', phase=ONBOARDING,
        component='builder:/v1/capture', tool='builder',
        predicates=(
            PredicateDefinition('harness/deterministic',
                'Capturing each dataset again writes the set already stored, and replaying the '
                'visible inputs twice writes the same outputs twice. On the tree.'
            ),
        ),
        requires=('harness/replays', 'harness/captured'),
        needs=('builder',),
    ),
    ActionDefinition(
        name='harness_timing', phase=ONBOARDING,
        component='builder:/v1/time', tool='builder',
        predicates=(
            PredicateDefinition('harness/times',
                'The timing program runs twice inside its declared budget and writes the same '
                "declared outputs both times; the last run's outputs are stored as the code's "
                'program capture set. On the tree.'
            ),
        ),
        requires=('harness/builds',),
        needs=('builder',),
    ),
    ActionDefinition(
        name='harness_original', phase=ONBOARDING,
        component='builder:/v1/build+/v1/time', tool='builder',
        predicates=(
            PredicateDefinition('harness/original',
                'The onboarded program and an independently reviewed pre-onboarding snapshot '
                'produce equivalent declared outputs for every reference run. On the tree.'
            ),
        ),
        requires=('harness/builds', 'harness/times'),
        needs=('builder',),
    ),
    ActionDefinition(
        name='harness_self_check', phase=ONBOARDING,
        component='builder:/v1/mutate', tool='builder',
        predicates=(
            PredicateDefinition('harness/self_check',
                'Single-token faults injected into the files the manifest says implement the region '
                'are built and replayed the way the baseline is, and scored against the captured '
                "answers with the code's own tolerance bands: at least one is caught, none changes "
                'an answer the bands then let through, and every generated mutant is classified '
                'without skips or runtime failures. Unchanged-output mutants are listed as review '
                'obligations, not asserted to be equivalent. On the tree.'
            ),
        ),
        requires=('harness/replays', 'harness/captured'),
        config_keys=('limit',),
        needs=('builder',),
    ),
    ActionDefinition(
        name='harness_property', phase=ONBOARDING,
        component='builder:/v1/properties', tool='builder',
        predicates=(
            PredicateDefinition('harness/properties',
                "The code's own module of invariants passes against the baseline build, at the "
                'recorded seed, with at least one executed passing test and no skipped tests. A '
                'code that declares none files this claim too, saying so, because stating no '
                'invariants is something the code says about itself. On the tree.'
            ),
        ),
        requires=('harness/replays', 'harness/captured'),
        config_keys=('seed', 'max_examples'),
        needs=('builder',),
    ),
)


def _validate(actions: tuple[ActionDefinition, ...]) -> None:
    names = [action.name for action in actions]
    emitted = [name for action in actions for name in action.emits]
    if len(set(names)) != len(names) or len(set(emitted)) != len(emitted):
        raise ValueError("workflow action and predicate names must be unique")
    producers = {name: action for action in actions for name in action.emits}
    for action in actions:
        if action.phase not in PHASES or not action.predicates:
            raise ValueError(f"invalid phase or empty predicate list for {action.name}")
        if action.subject_kind not in {"tree", "frozen", "baseline_tree"}:
            raise ValueError(f"unknown subject kind for {action.name}: {action.subject_kind}")
        for required in action.requires:
            if required not in producers:
                raise ValueError(f"{action.name} requires unknown predicate {required}")
            if producers[required].phase != action.phase:
                raise ValueError(f"{action.name} requires evidence from another phase: {required}")
    # A prerequisite cycle makes its actions permanently unreachable even
    # though the Python modules that describe them may import cleanly.
    visited, visiting = set(), set()

    def visit(action: ActionDefinition) -> None:
        if action.name in visiting:
            raise ValueError(f"workflow prerequisite cycle at {action.name}")
        if action.name in visited:
            return
        visiting.add(action.name)
        for required in action.requires:
            visit(producers[required])
        visiting.remove(action.name)
        visited.add(action.name)

    for action in actions:
        visit(action)


_validate(ACTIONS)
ACTION_BY_NAME = {action.name: action for action in ACTIONS}
PRODUCERS = {name: action.name for action in ACTIONS for name in action.emits}
SUBJECT_KIND_OF = {name: action.subject_kind for action in ACTIONS for name in action.emits}
