/**
 * Trust role: none. A thin HTTP client for the gateway's four endpoints
 * (the same four `equivalent/client.py` calls on the Python side). Every
 * decision still happens in the gateway; this just shapes the request.
 */

import type { GatewayConfig } from "./config.js";
import type { ActionRow } from "./table.js";

export interface SessionContext {
  sessionManager: { getSessionId(): string };
  model?: { id: string };
}

function authHeaders(config: GatewayConfig): Record<string, string> {
  return { Authorization: `Bearer ${config.token}` };
}

function sessionHeaders(config: GatewayConfig, ctx: SessionContext): Record<string, string> {
  return {
    ...authHeaders(config),
    "Content-Type": "application/json",
    "X-Session-Id": ctx.sessionManager.getSessionId(),
    "X-Model-Id": ctx.model?.id ?? "unknown",
  };
}

export async function fetchTable(config: GatewayConfig): Promise<ActionRow[]> {
  const res = await fetch(`${config.url}/table`, { headers: authHeaders(config) });
  return (await res.json()) as ActionRow[];
}

export async function fetchStatus(config: GatewayConfig): Promise<unknown> {
  const res = await fetch(`${config.url}/status?region=${encodeURIComponent(config.region)}`, {
    headers: authHeaders(config),
  });
  return res.json();
}

export async function postSubmit(
  config: GatewayConfig,
  workingCopyDir: string,
  ctx: SessionContext,
): Promise<unknown> {
  const res = await fetch(`${config.url}/submit`, {
    method: "POST",
    headers: sessionHeaders(config, ctx),
    body: JSON.stringify({ region: config.region, working_copy_dir: workingCopyDir }),
  });
  return res.json();
}

export async function postRun(config: GatewayConfig, action: string, ctx: SessionContext): Promise<unknown> {
  const res = await fetch(`${config.url}/run`, {
    method: "POST",
    headers: sessionHeaders(config, ctx),
    body: JSON.stringify({ action, region: config.region, config: {} }),
  });
  return res.json();
}
