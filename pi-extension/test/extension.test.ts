import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import equivalentExtension from "../src/extension.js";

const ENV = {
  EQUIVALENT_GATEWAY_URL: "http://gateway.local",
  EQUIVALENT_GATEWAY_TOKEN: "test-token",
  EQUIVALENT_REGION: "ch04:step",
};

function fakePi() {
  const tools = new Map<string, any>();
  const commands = new Map<string, any>();
  const handlers = new Map<string, any>();
  return {
    tools,
    commands,
    handlers,
    on(event: string, handler: any) {
      handlers.set(event, handler);
    },
    registerTool(def: any) {
      tools.set(def.name, def);
    },
    registerCommand(name: string, def: any) {
      commands.set(name, def);
    },
  };
}

function fakeCtx() {
  return {
    ui: { notify: vi.fn() },
    sessionManager: { getSessionId: () => "sess-1" },
    model: { id: "claude-sonnet-5" },
  };
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

describe("equivalentExtension", () => {
  beforeEach(() => {
    Object.assign(process.env, ENV);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    for (const key of Object.keys(ENV)) delete process.env[key];
  });

  it("registers one tool per callable row plus submit and status, skipping accept", async () => {
    const table = [
      { name: "sese_check", emits: ["sese/verified"], requires: [], deterministic: true, component: "analyzer:check_sese" },
      { name: "accept", emits: [], requires: [], deterministic: true, component: null },
    ];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(table)));

    const pi = fakePi();
    equivalentExtension(pi as any);
    await pi.handlers.get("session_start")({}, fakeCtx());

    expect([...pi.tools.keys()].sort()).toEqual(["claim", "sese_check", "status", "submit"]);
  });

  it("reads one claim by the id it is given and renders the verdict with its detail", async () => {
    const table = [
      { name: "sese_check", emits: ["sese/verified"], requires: [], deterministic: true, component: "analyzer:check_sese" },
    ];
    const claim = {
      claim_id: "c-0007",
      predicateType: "harness/self_check",
      verdict: "fail",
      subject: [{ kind: "tree", sha256: "a".repeat(64) }],
      materials: [],
      detail: { reason: "no injected fault was caught" },
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(table))
      .mockResolvedValueOnce(jsonResponse(claim));
    vi.stubGlobal("fetch", fetchMock);

    const pi = fakePi();
    equivalentExtension(pi as any);
    const ctx = fakeCtx();
    await pi.handlers.get("session_start")({}, ctx);

    const tool = pi.tools.get("claim");
    expect(tool.parameters.required).toEqual(["claim_id"]);
    expect(tool.parameters.properties.claim_id.type).toBe("string");

    const result = await tool.execute("call-9", { claim_id: "c-0007" }, undefined, undefined, ctx);

    expect(String(fetchMock.mock.calls[1][0])).toContain("/claims/c-0007");
    expect(fetchMock.mock.calls[1][1].headers["X-Tool-Call-Id"]).toBe("call-9");
    expect(result.content[0].text).toContain("harness/self_check: fail (c-0007)");
    expect(result.content[0].text).toContain("no injected fault was caught");
  });

  it("says what the gateway said when the claim id is not one of this region's", async () => {
    const table = [
      { name: "sese_check", emits: ["sese/verified"], requires: [], deterministic: true, component: "analyzer:check_sese" },
    ];
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(table))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "unknown claim: c-9999" }), {
          status: 404,
          headers: { "content-type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const pi = fakePi();
    equivalentExtension(pi as any);
    const ctx = fakeCtx();
    await pi.handlers.get("session_start")({}, ctx);

    const result = await pi.tools.get("claim").execute(
      "call-9", { claim_id: "c-9999" }, undefined, undefined, ctx,
    );

    expect(result.content[0].text).toBe("error: unknown claim: c-9999");
  });

  it("a refusal from /run becomes the tool's result text, missing claims verbatim", async () => {
    const table = [
      {
        name: "build_replay",
        emits: ["build/replay"],
        requires: [["sese/verified", "frozen"]],
        deterministic: true,
        component: "builder:/v1/build",
      },
    ];
    const refusal = {
      refused: true,
      action: "build_replay",
      tree: "T1",
      missing: [{ predicateType: "sese/verified", status: "missing", producing_action: "sese_check" }],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(table))
      .mockResolvedValueOnce(jsonResponse(refusal));
    vi.stubGlobal("fetch", fetchMock);

    const pi = fakePi();
    equivalentExtension(pi as any);
    await pi.handlers.get("session_start")({}, fakeCtx());

    const result = await pi.tools.get("build_replay").execute("call-1", {}, undefined, undefined, fakeCtx());
    expect(result.content[0].text).toContain("sese/verified");
    expect(result.content[0].text).toContain("sese_check");
  });

  it("sends the session id, model id, and tool call id on every /run and /submit call", async () => {
    const table = [
      { name: "sese_check", emits: ["sese/verified"], requires: [], deterministic: true, component: "analyzer:check_sese" },
    ];
    const claim = { claim_id: "c-1", verdict: "pass", detail: {} };
    const receipt = { tree: "T1", frozen: "F0", rejected: [], committed: true };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(table))
      .mockResolvedValueOnce(jsonResponse(claim))
      .mockResolvedValueOnce(jsonResponse(receipt));
    vi.stubGlobal("fetch", fetchMock);

    const pi = fakePi();
    equivalentExtension(pi as any);
    const ctx = fakeCtx();
    await pi.handlers.get("session_start")({}, ctx);

    await pi.tools.get("sese_check").execute("tool:1787913327226:mu24lznsjv", {}, undefined, undefined, ctx);
    await pi.tools.get("submit").execute("tool:1787913327226:zfppu5ar7hf", {}, undefined, undefined, ctx);

    const runHeaders = fetchMock.mock.calls[1][1].headers;
    const submitHeaders = fetchMock.mock.calls[2][1].headers;
    expect(runHeaders["X-Session-Id"]).toBe("sess-1");
    expect(runHeaders["X-Model-Id"]).toBe("claude-sonnet-5");
    expect(runHeaders["X-Tool-Call-Id"]).toBe("tool:1787913327226:mu24lznsjv");
    expect(submitHeaders["X-Session-Id"]).toBe("sess-1");
    expect(submitHeaders["X-Model-Id"]).toBe("claude-sonnet-5");
    expect(submitHeaders["X-Tool-Call-Id"]).toBe("tool:1787913327226:zfppu5ar7hf");
  });

  it("leaves the tool call id off /table and /status, which the gateway does not log", async () => {
    const table = [
      { name: "sese_check", emits: ["sese/verified"], requires: [], deterministic: true, component: "analyzer:check_sese" },
    ];
    const status = { tree: "T1", frozen: "F0", rows: [], accepted: false };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(table))
      .mockResolvedValueOnce(jsonResponse(status));
    vi.stubGlobal("fetch", fetchMock);

    const pi = fakePi();
    equivalentExtension(pi as any);
    const ctx = fakeCtx();
    await pi.handlers.get("session_start")({}, ctx);
    await pi.tools.get("status").execute("tool:1787913327226:jvzns42um", {}, undefined, undefined, ctx);

    const tableHeaders = fetchMock.mock.calls[0][1].headers;
    const statusHeaders = fetchMock.mock.calls[1][1].headers;
    expect(tableHeaders["X-Tool-Call-Id"]).toBeUndefined();
    expect(statusHeaders["X-Tool-Call-Id"]).toBeUndefined();
  });

  it("offers a row's settings as tool parameters and sends what the model set", async () => {
    const table = [
      {
        name: "time_port",
        emits: ["timing/port"],
        requires: [],
        deterministic: false,
        component: "builder:/v1/time",
        config_keys: ["repeats"],
        config_params: {
          repeats: { type: "integer", description: "How many timed runs to make. Five when left out." },
        },
      },
    ];
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(table))
      .mockResolvedValueOnce(jsonResponse({ claim_id: "c-1", verdict: "pass", detail: {} }));
    vi.stubGlobal("fetch", fetchMock);

    const pi = fakePi();
    equivalentExtension(pi as any);
    const ctx = fakeCtx();
    await pi.handlers.get("session_start")({}, ctx);

    const tool = pi.tools.get("time_port");
    expect(tool.parameters.properties.repeats).toEqual({
      type: "integer",
      description: "How many timed runs to make. Five when left out.",
    });
    expect(tool.parameters.required).toBeUndefined();
    expect(tool.description).toContain("repeats");

    await tool.execute("call-1", { repeats: 3 }, undefined, undefined, ctx);

    expect(JSON.parse(fetchMock.mock.calls[1][1].body).config).toEqual({ repeats: 3 });
  });

  it("a row that takes no settings keeps an empty parameter object and an empty config", async () => {
    const table = [
      {
        name: "sese_check",
        emits: ["sese/verified"],
        requires: [],
        deterministic: true,
        component: "analyzer:check_sese",
        config_keys: [],
        config_params: {},
      },
    ];
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(table))
      .mockResolvedValueOnce(jsonResponse({ claim_id: "c-1", verdict: "pass", detail: {} }));
    vi.stubGlobal("fetch", fetchMock);

    const pi = fakePi();
    equivalentExtension(pi as any);
    const ctx = fakeCtx();
    await pi.handlers.get("session_start")({}, ctx);

    expect(pi.tools.get("sese_check").parameters.properties ?? {}).toEqual({});
    await pi.tools.get("sese_check").execute("call-1", {}, undefined, undefined, ctx);

    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      action: "sese_check", region: "ch04:step", config: {},
    });
  });

  it("submits the region alone -- no path the gateway would then read", async () => {
    const table = [
      { name: "sese_check", emits: ["sese/verified"], requires: [], deterministic: true, component: "analyzer:check_sese" },
    ];
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(table))
      .mockResolvedValueOnce(jsonResponse({ tree: "T1", frozen: "F0", rejected: [], committed: true }));
    vi.stubGlobal("fetch", fetchMock);

    const pi = fakePi();
    equivalentExtension(pi as any);
    const ctx = fakeCtx();
    await pi.handlers.get("session_start")({}, ctx);
    await pi.tools.get("submit").execute("call-1", {}, undefined, undefined, ctx);

    expect(pi.tools.get("submit").parameters.properties ?? {}).toEqual({});
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ region: "ch04:step" });
  });
});
