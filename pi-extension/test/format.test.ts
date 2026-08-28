import { describe, expect, it } from "vitest";

import { refusalText, runResultText, submitResultText } from "../src/format.js";

describe("runResultText", () => {
  it("renders a refusal listing every missing claim and its producing action, verbatim", () => {
    const body = {
      refused: true as const,
      action: "build_replay",
      tree: "abc123",
      missing: [{ predicateType: "sese/verified", status: "missing" as const, producing_action: "sese_check" }],
    };
    const text = runResultText("build_replay", body);
    expect(text).toContain("sese/verified");
    expect(text).toContain("sese_check");
    expect(text).toBe(refusalText(body));
  });

  it("renders a pass claim with its verdict and claim id", () => {
    const body = { claim_id: "c-1", verdict: "pass", detail: {} };
    expect(runResultText("sese_check", body)).toBe("sese_check: pass (c-1)");
  });

  it("renders a component error plainly", () => {
    const body = { error: "builder not configured" };
    expect(runResultText("build_replay", body)).toBe("error: builder not configured");
  });

  it("renders a multi-claim sanitize response, one line per tool", () => {
    const body = {
      claims: [
        { predicateType: "sanitize/memcheck", claim_id: "c-2", verdict: "pass", detail: {} },
        { predicateType: "sanitize/racecheck", claim_id: "c-3", verdict: "pass", detail: {} },
      ],
    };
    expect(runResultText("sanitize", body)).toBe("sanitize/memcheck: pass (c-2)\nsanitize/racecheck: pass (c-3)");
  });
});

describe("submitResultText", () => {
  it("names ignored files in the receipt, in the gateway's {path, reason} shape", () => {
    const body = {
      tree: "T3",
      frozen: "F1",
      rejected: [{ path: "Makefile", reason: "not_allowed" }],
      committed: true,
    };
    expect(submitResultText(body)).toBe("submitted -> tree T3, frozen F1. ignored: Makefile (not_allowed).");
  });

  it("says nothing about ignored files when none were", () => {
    const body = { tree: "T1", frozen: "F0", rejected: [], not_sent: [], committed: true };
    expect(submitResultText(body)).toBe("submitted -> tree T1, frozen F0.");
  });

  it("warns about allowed files the agent did not send", () => {
    const body = {
      tree: "T2",
      frozen: "F0",
      rejected: [],
      not_sent: ["src/mod_kernel.f90"],
      committed: false,
    };
    expect(submitResultText(body)).toBe(
      "submitted -> tree T2, frozen F0. allowed but not sent: src/mod_kernel.f90.",
    );
  });
});
