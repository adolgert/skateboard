/**
 * Trust role: none. Renders GET /status for a person; the gateway's
 * `accepted` field is the actual decision.
 */

export interface StatusRow {
  predicateType: string;
  status: "present" | "missing";
  verdict?: string;
  claim_id?: string;
  producing_action?: string;
}

export interface StatusBody {
  tree: string | null;
  frozen: string | null;
  phase: string;
  rows: StatusRow[];
  accepted: boolean;
}

/**
 * The word for a region that has met every requirement of its phase.
 * They differ because they mean different things: an onboarded code is
 * ready for a person to review and promote, an accepted port is ready to
 * merge.
 */
const FINISHED_WORD: Record<string, string> = {
  onboarding: "ONBOARDED",
  porting: "ACCEPTED",
};

function finishedWord(phase: string): string {
  return FINISHED_WORD[phase] ?? "ACCEPTED";
}

export function renderStatus(body: StatusBody): string {
  const lines: string[] = [];
  lines.push(`tree ${body.tree ?? "(none)"}  frozen ${body.frozen ?? "(none)"}`);
  for (const row of body.rows) {
    if (row.status === "present") {
      lines.push(`  ${row.predicateType}  ${row.verdict}  ${row.claim_id}`);
    } else {
      lines.push(`  ${row.predicateType}  missing  (run ${row.producing_action})`);
    }
  }
  const word = finishedWord(body.phase);
  lines.push(body.accepted ? word : `not ${word.toLowerCase()}`);
  return lines.join("\n");
}
