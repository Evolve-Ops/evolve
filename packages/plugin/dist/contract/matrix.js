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
import { failingIds } from "./run.js";
/** The table header the file carries, and the marker `upsertRow` anchors on. */
export const MATRIX_HEADER = [
    "| OpenClaw | Evolve plugin | Result | Failing contract checks | Checked |",
    "|---|---|---|---|---|",
].join("\n");
function cell(s) {
    // A pipe inside a cell would split the row; ids and versions never contain
    // one, but the detail-free format is only safe if we prove it.
    return s.replace(/\|/g, "/");
}
/** Render one row of the matrix table. */
export function formatRow(row) {
    const failing = row.failing.length ? row.failing.join(", ") : "—";
    return `| ${cell(row.ocVersion)} | ${cell(row.evolveVersion)} | ${row.result} | ${cell(failing)} | ${cell(row.checkedAt)} |`;
}
/** Parse one row back, or null when the line is not a data row. */
export function parseRow(line) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("|") || /^\|[\s|:-]+\|$/.test(trimmed))
        return null;
    const cells = trimmed.slice(1, trimmed.endsWith("|") ? -1 : undefined).split("|").map((c) => c.trim());
    if (cells.length < 5)
        return null;
    const [ocVersion, evolveVersion, result, failing, checkedAt] = cells;
    if (result !== "pass" && result !== "fail")
        return null;
    return {
        ocVersion,
        evolveVersion,
        result,
        failing: failing === "—" || failing === "" ? [] : failing.split(",").map((s) => s.trim()).filter(Boolean),
        checkedAt,
    };
}
/** Derive the row a completed run publishes. */
export function rowForRun(run) {
    return {
        ocVersion: run.ocVersion ?? "unknown",
        evolveVersion: run.evolveVersion,
        result: run.ok ? "pass" : "fail",
        failing: run.ok ? [] : failingIds(run),
        checkedAt: run.startedAt.slice(0, 10),
    };
}
/**
 * Insert or replace `row` in `markdown`, keyed on (OpenClaw × Evolve
 * plugin). Rows stay in the order they were first published, so the file
 * reads as a history rather than re-sorting under every nightly.
 */
export function upsertRow(markdown, row) {
    const lines = markdown.split("\n");
    const rendered = formatRow(row);
    let lastRowIdx = -1;
    for (let i = 0; i < lines.length; i++) {
        const existing = parseRow(lines[i]);
        if (!existing)
            continue;
        lastRowIdx = i;
        if (existing.ocVersion === row.ocVersion && existing.evolveVersion === row.evolveVersion) {
            lines[i] = rendered;
            return lines.join("\n");
        }
    }
    if (lastRowIdx >= 0) {
        lines.splice(lastRowIdx + 1, 0, rendered);
        return lines.join("\n");
    }
    const headerIdx = lines.findIndex((l) => l.trim().startsWith("|---"));
    if (headerIdx >= 0) {
        lines.splice(headerIdx + 1, 0, rendered);
        return lines.join("\n");
    }
    return `${markdown.replace(/\s*$/, "")}\n\n${MATRIX_HEADER}\n${rendered}\n`;
}
//# sourceMappingURL=matrix.js.map