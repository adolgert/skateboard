"""Loads a region's visible capture set from disk, in the exact layout
demo/orchestrator/orchestrator.py's load_cases() already reads:
`<dir>/cases.json` ({"cases": [name, ...]}) plus `<dir>/<name>/h_in.bin`
and `<dir>/<name>/u_in.bin` per case.
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
