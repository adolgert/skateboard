"""Loads a region's visible capture set from disk.

The layout a visible dataset directory holds: `<dir>/cases.json`
({"cases": [name, ...]}) plus `<dir>/<name>/h_in.bin` and
`<dir>/<name>/u_in.bin` per case.

Trust role: these bytes are the inputs every replay runs against. Loading
the wrong directory, or mangling a case, makes gpu/executed and
regression/visible claims describe a run of the wrong data.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path


def load_visible_cases(dataset_dir) -> dict:
    dataset_dir = Path(dataset_dir)
    names = json.loads((dataset_dir / "cases.json").read_text())["cases"]
    return {
        name: {
            "h_in": base64.b64encode((dataset_dir / name / "h_in.bin").read_bytes()).decode(),
            "u_in": base64.b64encode((dataset_dir / name / "u_in.bin").read_bytes()).decode(),
        }
        for name in names
    }
