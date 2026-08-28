import { describe, expect, it } from "vitest";

import { renderStatus, type StatusBody } from "../src/status.js";

describe("renderStatus", () => {
  it("prints each present claim's verdict and claim id, and ACCEPTED when accepted", () => {
    const body: StatusBody = {
      tree: "T4",
      frozen: "F1",
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
      accepted: false,
      rows: [{ predicateType: "gpu/executed", status: "missing", producing_action: "run_replay" }],
    };
    const text = renderStatus(body);
    expect(text).toContain("run_replay");
    expect(text.trim().endsWith("not accepted")).toBe(true);
  });
});
