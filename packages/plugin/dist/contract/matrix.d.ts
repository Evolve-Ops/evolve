/**
 * `internal/oc-compatibility.md` — the published compatibility matrix.
 *
 * One row per (OpenClaw version × Evolve plugin version): the verdict, the
 * checks that failed, and when the run happened. The nightly job appends or
 * replaces a row; the Update card and the upgrade preflight read the same
 * facts out of the recorded JSON run, not out of this file — the matrix is
 * the human-readable publication of it, and this module exists so the two
 * cannot drift into different formats.
 *
 * Round-trip is a contract of its own: `parseRow(formatRow(r)) ≍ r`, which
 * is what lets `upsertRow` replace an existing row for a version instead of
 * appending a second, contradictory one.
 */
import type { ContractRun } from "./types.js";
/** A single matrix row. */
export interface MatrixRow {
    ocVersion: string;
    evolveVersion: string;
    result: "pass" | "fail";
    failing: string[];
    checkedAt: string;
}
/** The table header the file carries, and the marker `upsertRow` anchors on. */
export declare const MATRIX_HEADER: string;
/** Render one row of the matrix table. */
export declare function formatRow(row: MatrixRow): string;
/** Parse one row back, or null when the line is not a data row. */
export declare function parseRow(line: string): MatrixRow | null;
/** Derive the row a completed run publishes. */
export declare function rowForRun(run: ContractRun): MatrixRow;
/**
 * Insert or replace `row` in `markdown`, keyed on (OpenClaw × Evolve
 * plugin). Rows stay in the order they were first published, so the file
 * reads as a history rather than re-sorting under every nightly.
 */
export declare function upsertRow(markdown: string, row: MatrixRow): string;
//# sourceMappingURL=matrix.d.ts.map