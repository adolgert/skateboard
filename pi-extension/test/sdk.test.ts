import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createAgentSession, DefaultResourceLoader, getAgentDir, SessionManager } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const EXTENSION_PATH = path.join(__dirname, "..", "src", "extension.ts");

const ENV = {
  EQUIVALENT_GATEWAY_URL: "http://gateway.local",
  EQUIVALENT_GATEWAY_TOKEN: "test-token",
  EQUIVALENT_REGION: "ch04:step",
};

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

describe("a real pi session: session files land in the configured session directory", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    for (const key of Object.keys(ENV)) delete process.env[key];
  });

  it("writes the extension's tool call into the on-disk session file", async () => {
    Object.assign(process.env, ENV);

    const table = [
      { name: "sese_check", emits: ["sese/verified"], requires: [], deterministic: true, component: "analyzer:check_sese" },
    ];
    const status = { tree: null, frozen: null, rows: [], accepted: false };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string | URL) => {
        const href = String(url);
        if (href.includes("/table")) return jsonResponse(table);
        if (href.includes("/status")) return jsonResponse(status);
        throw new Error(`unexpected fetch in sdk test: ${href}`);
      }),
    );

    const faux = fauxProvider();
    faux.setResponses([fauxAssistantMessage([fauxToolCall("status", {})]), fauxAssistantMessage("done")]);

    const sessionDir = mkdtempSync(path.join(tmpdir(), "equivalent-pi-sessions-"));
    const loader = new DefaultResourceLoader({
      cwd: process.cwd(),
      agentDir: getAgentDir(),
      additionalExtensionPaths: [EXTENSION_PATH],
      extensionFactories: [(pi: any) => pi.registerProvider(faux.provider)],
    });
    await loader.reload();

    const { session } = await createAgentSession({
      model: faux.getModel(),
      resourceLoader: loader,
      sessionManager: SessionManager.create(process.cwd(), sessionDir),
    });

    try {
      await session.prompt("check the region's status");

      const sessionFile = session.sessionFile;
      expect(sessionFile).toBeTruthy();
      const contents = readFileSync(sessionFile as string, "utf8");
      expect(contents).toContain('"type":"toolCall"');
      expect(contents).toContain('"name":"status"');
      expect(contents).toContain('"role":"toolResult"');
      expect(contents).toContain('"toolName":"status"');
    } finally {
      session.dispose();
    }
  }, 20_000);
});
