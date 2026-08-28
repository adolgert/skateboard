import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchStatus, fetchTable } from "../src/gateway.js";
import type { GatewayConfig } from "../src/config.js";

const CONFIG: GatewayConfig = {
  url: "http://gateway.local",
  token: "test-token",
  region: "ch04:step",
};

function recordingFetch(body: unknown) {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string | URL) => {
      urls.push(String(url));
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return urls;
}

describe("the gateway client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("asks for the table of its own region -- the actions are the region's phase's", async () => {
    const urls = recordingFetch([]);

    await fetchTable(CONFIG);

    expect(urls).toEqual(["http://gateway.local/table?region=ch04%3Astep"]);
  });

  it("escapes the colon in a region id the same way for the table and the status", async () => {
    const urls = recordingFetch({});

    await fetchTable(CONFIG);
    await fetchStatus(CONFIG);

    expect(urls[0].split("?")[1]).toBe(urls[1].split("?")[1]);
  });
});
