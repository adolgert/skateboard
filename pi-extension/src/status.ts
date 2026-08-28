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
  rows: StatusRow[];
  accepted: boolean;
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
  lines.push(body.accepted ? "ACCEPTED" : "not accepted");
  return lines.join("\n");
}
