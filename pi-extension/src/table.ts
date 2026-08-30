/**
 * Trust role: none. This only turns the gateway's own precondition table
 * into tool descriptions; it decides nothing. A wrong description makes
 * the model call things in the wrong order, and the gateway's refusal is
 * what actually stops a premature call.
 */

import { Type, type TObject } from "typebox";

/** What one setting is worth: the gateway sends the wording, not this file. */
export interface ConfigParam {
  type: string;
  description: string;
}

export interface ActionRow {
  name: string;
  emits: string[];
  requires: [string, string][];
  deterministic: boolean;
  component: string | null;
  // The settings POST /run takes for this action, and what each one
  // means. Optional here because a row served by a gateway that predates
  // them arrives without them, in which case the action takes none.
  config_keys?: string[];
  config_params?: Record<string, ConfigParam>;
}

const SUBJECT_PHRASE: Record<string, string> = {
  tree: "the submitted tree",
  frozen: "this region",
  capture_set: "the capture set",
  strategy: "the strategy",
  binary: "the binary",
  outputs: "the outputs",
};

function subjectPhrase(kind: string): string {
  return SUBJECT_PHRASE[kind] ?? kind;
}

export function requiresLine(row: ActionRow): string | undefined {
  if (row.requires.length === 0) return undefined;
  const parts = row.requires.map(
    ([predicateType, subjectKind]) => `${predicateType} = pass for ${subjectPhrase(subjectKind)}`,
  );
  return `Requires: ${parts.join("; ")}.`;
}

/**
 * The settings this action takes, keyed as POST /run wants them. A key
 * the gateway declared but did not describe is left out: without a type
 * there is nothing to offer the model, and the gateway would reject a
 * guess anyway.
 */
export function configParams(row: ActionRow): Record<string, ConfigParam> {
  const described = row.config_params ?? {};
  const params: Record<string, ConfigParam> = {};
  for (const key of row.config_keys ?? []) {
    const spec = described[key];
    if (spec) params[key] = spec;
  }
  return params;
}

export function settingsLine(row: ActionRow): string | undefined {
  const entries = Object.entries(configParams(row));
  if (entries.length === 0) return undefined;
  const parts = entries.map(([key, spec]) => `${key} (${spec.type}) -- ${spec.description}`);
  return `Optional settings: ${parts.join(" ")}`;
}

export function describeRow(row: ActionRow): string {
  const produces = row.emits.length > 0 ? `Records: ${row.emits.join(", ")}.` : "Records no claim.";
  const lines = [`Runs '${row.name}' via ${row.component}.`, produces];
  const req = requiresLine(row);
  if (req) lines.push(req);
  const settings = settingsLine(row);
  if (settings) lines.push(settings);
  return lines.join(" ");
}

/**
 * The tool's parameters are the row's own settings: whatever the gateway
 * says this action takes, offered with the gateway's own wording, so the
 * two cannot drift. An action that takes none gets an empty object, as
 * before.
 */
export function toolParameters(row: ActionRow): TObject {
  const properties: Record<string, ReturnType<typeof Type.Optional>> = {};
  for (const [key, spec] of Object.entries(configParams(row))) {
    if (spec.type !== "integer") continue;
    properties[key] = Type.Optional(Type.Integer({ description: spec.description }));
  }
  return Type.Object(properties);
}

/**
 * What to send as the run's config: the declared settings the model
 * filled in, and nothing else. The gateway hashes this config to spot a
 * repeat of the same request, so anything it did not declare has no
 * business in it.
 */
export function runConfig(row: ActionRow, params: unknown): Record<string, unknown> {
  const given = (params ?? {}) as Record<string, unknown>;
  const config: Record<string, unknown> = {};
  for (const key of Object.keys(configParams(row))) {
    if (given[key] !== undefined) config[key] = given[key];
  }
  return config;
}

/**
 * Rows with no component (only "accept") have nothing for POST /run to
 * dispatch to -- the gateway 400s rather than refusing normally. GET
 * /status already reports acceptance, so these rows are excluded here
 * rather than given a tool that always errors.
 */
export function callableRows(table: ActionRow[]): ActionRow[] {
  return table.filter((row) => row.component !== null);
}
