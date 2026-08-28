"""Fake builder/oracle clients for the component and dispatch tests.

Unlike check_sese.py (cheap, pure Python, safe to run for real in
tests), the builder needs nvfortran/compute-sanitizer/a GPU and the
oracle needs its baked capture data -- none of which exist in this
development environment. These fakes match the real services' response
shapes (demo/builder/app.py, demo/oracle/app.py) exactly, so the gateway
dispatch code under test is exercised the same way it would be against
the real thing; only what's inside the box differs.
"""
from __future__ import annotations


class FakeBuilder:
    def __init__(self):
        self.build_calls = []
        self.run_calls = []
        self.sanitize_calls = []
        self.time_calls = []
        self.build_ok = True
        self.run_ok = True
        self.run_kernels = 4
        self.run_launches = [["src/mod_kernel.f90", "step", "42"]]
        self.sanitize_ok = True
        self.time_ok = True
        self.runs_s = [0.21, 0.20, 0.22]
        # The executable names the real builder reports on, all present.
        # A test that wants a builder missing something drops a key here.
        self.tools = {
            name: True for name in
            ("nvfortran", "compute-sanitizer", "nsys", "make", "cmake", "gfortran")
        }

    def healthz(self):
        return {"ok": True, "tools": dict(self.tools)}

    def build(self, attempt_id, files, profile, flags=None, link_flags=None):
        self.build_calls.append({
            "attempt_id": attempt_id, "files": files, "profile": profile,
            "flags": flags, "link_flags": link_flags,
        })
        # Like the real builder: echo back what would have reached the
        # compiler (explicit flags when given, else the profile's).
        used_flags = list(flags or ["-O2", "-profile-default"]) + list(link_flags or [])
        if not self.build_ok:
            return {"ok": False, "stage": "build", "target": "replay", "flags": used_flags, "log_tail": "compile error"}
        return {"ok": True, "stage": "build", "profile": profile, "flags": used_flags,
                "minfo_excerpt": "Generating Tesla code", "log_tail": ""}

    def run(self, attempt_id, profile, cases, mandatory=False):
        self.run_calls.append({"attempt_id": attempt_id, "profile": profile, "cases": cases, "mandatory": mandatory})
        if not self.run_ok:
            return {"ok": False, "stage": "run", "log_tail": "runtime crash"}
        outputs = {name: {"h": "aGVsbG8=", "u": "d29ybGQ="} for name in cases}
        return {"ok": True, "stage": "run", "outputs": outputs, "kernels_launched": self.run_kernels,
                "launches": self.run_launches, "log_tail": ""}

    def sanitize(self, attempt_id, profile, cases, tools):
        self.sanitize_calls.append({"attempt_id": attempt_id, "profile": profile, "cases": cases, "tools": tools})
        per_tool = {t: {"ok": self.sanitize_ok, "errors": 0 if self.sanitize_ok else 3, "log_tail": ""} for t in tools}
        return {"ok": self.sanitize_ok, "stage": "sanitize", "per_tool": per_tool}

    def time(self, attempt_id, repeats=5):
        self.time_calls.append({"attempt_id": attempt_id, "repeats": repeats})
        if not self.time_ok:
            return {"ok": False, "stage": "time", "log_tail": "tsunami binary not built"}
        return {"ok": True, "stage": "time", "runs_s": self.runs_s, "gpu_exclusive": True, "diagnostic": ""}


class FakeOracle:
    def __init__(self):
        self.compare_calls = []
        self.visible_verdict = "pass"
        self.holdout_verdict = "pass"

    def policy(self):
        return {"policy_version": "1", "policy_sha256": "policyabc"}

    def holdout_inputs(self):
        return {"dataset": "holdout", "cases": {"hcase0": {"h_in": "aGk=", "u_in": "aGk="}}}

    def compare(self, dataset, outputs, attempt_id="unknown"):
        self.compare_calls.append({"dataset": dataset, "outputs": outputs, "attempt_id": attempt_id})
        verdict = self.visible_verdict if dataset == "visible" else self.holdout_verdict
        resp = {"verdict": verdict, "dataset": dataset, "policy_sha256": "policyabc"}
        if dataset == "visible":
            resp["per_case"] = {name: {"pass": verdict == "pass"} for name in outputs}
        return resp
