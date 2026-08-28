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
