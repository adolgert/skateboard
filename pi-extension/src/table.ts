/**
 * Trust role: none. This only turns the gateway's own precondition table
 * into tool descriptions; it decides nothing. A wrong description makes
 * the model call things in the wrong order, and the gateway's refusal is
 * what actually stops a premature call.
 */

export interface ActionRow {
  name: string;
  emits: string[];
  requires: [string, string][];
  deterministic: boolean;
  component: string | null;
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

export function describeRow(row: ActionRow): string {
  const produces = row.emits.length > 0 ? `Records: ${row.emits.join(", ")}.` : "Records no claim.";
  const lines = [`Runs '${row.name}' via ${row.component}.`, produces];
  const req = requiresLine(row);
  if (req) lines.push(req);
  return lines.join(" ");
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
