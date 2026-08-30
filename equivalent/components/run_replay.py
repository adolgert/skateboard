"""Wraps the builder's /v1/run against the visible cases.

Trust role: what this returns becomes the gpu/executed claim, including
the device proof -- if the kernel count check here were wrong, a
CPU-only build would count as having run on the GPU.

It is also where the replay driver's outputs are checked against what the
code's manifest declares the region produces. The builder returns
whatever files the driver wrote, without knowing what to expect; this is
the step that says a declared output is missing, or came back as the
wrong element type or rank. The oracle would catch a wrong type later,
but not a driver that wrote nothing at all -- there would be nothing left
to compare and no claim saying why.

The visible outputs are stored in this claim's own detail -- regression_visible
reads them back from here rather than re-running the replay binary a
second time for the same cases, matching what
the first demonstration harness already did (it reused the same
in-memory `runr["outputs"]`).
"""
from __future__ import annotations

import base64

from equivalent.capture import npy
from equivalent.gateway.submit import attempt_id_for
from equivalent.manifest.schema import Manifest
from equivalent.strategy.schema import Strategy

from .errors import ComponentError


def _output_problems(manifest: Manifest, outputs: dict) -> list:
    """Everything wrong with what the replay wrote, one line each.

    A variable the submission holds and the manifest does not declare is
    not a problem here: the oracle reports those, and a driver writing a
    scratch file beside its outputs is not a reason to fail a run.
    """
    problems = []
    for case, arrays in sorted(outputs.items()):
        for variable in manifest.interface.outputs:
            encoded = arrays.get(variable.name)
            if encoded is None:
                problems.append(
                    f"case '{case}': the replay wrote no output for declared "
                    f"variable '{variable.name}'"
                )
                continue
            try:
                npy.check(npy.decode(base64.b64decode(encoded)), variable)
            except ValueError as exc:
                problems.append(f"case '{case}': {exc}")
    return problems


def check(region_id: str, tree_sha: str, strategy: Strategy, manifest: Manifest,
          visible_cases: dict, builder) -> dict:
    if not visible_cases:
        raise ComponentError("no visible dataset configured for this region")

    replay = manifest.build.targets["replay"]
    attempt_id = attempt_id_for(region_id, tree_sha)
    try:
        resp = builder.run(
            attempt_id, replay.executable, visible_cases,
            notify=strategy.device_proof.notify, mandatory=strategy.device_proof.mandatory,
        )
    except Exception as exc:
        raise ComponentError(f"builder /v1/run call failed: {exc}") from exc

    if not resp.get("ok"):
        return {"verdict": "fail", "detail": {"log_tail": resp.get("log_tail", "")}}

    problems = _output_problems(manifest, resp.get("outputs", {}))
    if problems:
        return {
            "verdict": "fail",
            "detail": {
                "outputs_rejected": problems,
                "hint": f"the replay driver must write every output code "
                        f"'{manifest.name}' declares, with the declared type and rank",
            },
        }

    kernels = resp.get("kernels_launched", 0)
    if kernels <= 0:
        return {
            "verdict": "fail",
            "detail": {
                "kernels_launched": 0,
                "hint": "code compiled but no GPU kernel launched; loops must be do concurrent / omp target for nvfortran to offload them",
            },
        }
    return {
        "verdict": "pass",
        "detail": {
            "kernels_launched": kernels,
            # Where the builder's runtime said the launches came from --
            # file, function, and line, one entry per distinct source
            # line. A reviewer reads this against the region's own code.
            "launches": resp.get("launches", []),
            "outputs": resp["outputs"],
        },
    }
