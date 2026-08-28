"""Shared exception for every component.

A component raises this when it cannot produce a real verdict
(a network failure, a crash, output it cannot parse, a
precondition it expected but didn't find) -- as opposed to running
successfully and reporting pass or fail, which is a real verdict and
becomes a claim. The gateway must never turn a ComponentError into a
claim; it logs the request outcome as "error" and returns {"error": ...}.
"""
from __future__ import annotations


class ComponentError(Exception):
    pass
