import { describe, expect, it } from "vitest";

import { renderStatus, type StatusBody } from "../src/status.js";

describe("renderStatus", () => {
  it("prints each present claim's verdict and claim id, and ACCEPTED when accepted", () => {
    const body: StatusBody = {
      tree: "T4",
      frozen: "F1",
      phase: "porting",
      accepted: true,
      rows: [{ predicateType: "sese/verified", status: "present", verdict: "pass", claim_id: "c-31" }],
    };
    const text = renderStatus(body);
    expect(text).toContain("sese/verified");
    expect(text).toContain("pass");
    expect(text).toContain("c-31");
    expect(text.trim().endsWith("ACCEPTED")).toBe(true);
  });

  it("names the producing action for a missing claim and prints not accepted", () => {
    const body: StatusBody = {
      tree: "T4",
      frozen: "F1",
      phase: "porting",
      accepted: false,
      rows: [{ predicateType: "gpu/executed", status: "missing", producing_action: "run_replay" }],
    };
    const text = renderStatus(body);
    expect(text).toContain("run_replay");
    expect(text.trim().endsWith("not accepted")).toBe(true);
  });

  it("shows a requirement whose check ran and failed as a fail with its claim id", () => {
    // The gateway reports this row as "missing" -- it is an unmet
    // requirement -- but it carries the failing claim. Printing it as
    // "missing" would say the check never ran, and hide the id that has
    // the reason in it.
    const body: StatusBody = {
      tree: "T4",
      frozen: "F1",
      phase: "onboarding",
      accepted: false,
      rows: [{
        predicateType: "harness/self_check",
        status: "missing",
        verdict: "fail",
        claim_id: "c-0007",
        producing_action: "harness_self_check",
      }],
    };

    const line = renderStatus(body).split("\n")[1];

    expect(line).toBe("  harness/self_check  fail  c-0007  (fix and run harness_self_check again)");
    expect(line).not.toContain("missing");
  });

  it("says ONBOARDED for a region that is bringing a code in, not ACCEPTED", () => {
    const body: StatusBody = {
      tree: "T4",
      frozen: "F1",
      phase: "onboarding",
      accepted: true,
      rows: [{ predicateType: "manifest/valid", status: "present", verdict: "pass", claim_id: "c-1" }],
    };
    const text = renderStatus(body);
    expect(text.trim().endsWith("ONBOARDED")).toBe(true);
    expect(text).not.toContain("ACCEPTED");
  });
});
