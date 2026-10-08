/**
 * Running the contract, and reading its verdict.
 *
 * `ok` is the single bit the Update card and the upgrade preflight consume:
 * a version is offered only when a recorded run against it says `ok`. It is
 * deliberately strict — every check must PASS. A `skip` means the contract
 * could not answer, and "could not answer" must read as "not yet validated",
 * never as a green light. Fail-safe in the direction that costs an operator
 * a day of waiting rather than a day of recovery.
 */

import { CONTRACT_CHECKS } from "./checks.js";
import type { CheckResult, ContractRun, OcProbe } from "./types.js";

export { CONTRACT_CHECKS };

export interface RunOptions {
  /** Restrict the run to these check ids (a debugging affordance, not a gate). */
  only?: readonly string[];
  /** The Evolve plugin version these assumptions belong to. */
  evolveVersion: string;
  /** Injected clock, so the JSON is reproducible in tests. */
  now?: () => Date;
  /** Injected monotonic ms, so durationMs is reproducible in tests. */
  elapsed?: () => number;
}

/** Run every check against `probe` and aggregate one `ContractRun`. */
export async function runContract(probe: OcProbe, opts: RunOptions): Promise<ContractRun> {
  const now = opts.now ?? (() => new Date());
  const elapsed = opts.elapsed ?? (() => Date.now());
  const startedAt = now().toISOString();
  const t0 = elapsed();

  const selected = opts.only && opts.only.length
    ? CONTRACT_CHECKS.filter((c) => opts.only?.includes(c.id))
    : CONTRACT_CHECKS;

  const results: CheckResult[] = [];
  for (const check of selected) {
    try {
      results.push(await check.run(probe));
    } catch (err) {
      // A check that throws is a check that could not answer. It must not
      // take the whole run down (the nightly has to produce a row either
      // way), and it must not read as a pass.
      results.push({
        id: check.id,
        title: check.title,
        incident: check.incident,
        status: "skip",
        detail: `the check raised: ${err instanceof Error ? err.message : String(err)}`,
        evidence: {},
      });
    }
  }

  let ocVersion: string | null = null;
  try {
    ocVersion = await probe.version();
  } catch {
    ocVersion = null;
  }

  return {
    ocVersion,
    evolveVersion: opts.evolveVersion,
    startedAt,
    durationMs: Math.max(0, elapsed() - t0),
    ok: results.length > 0 && results.every((r) => r.status === "pass"),
    results,
  };
}

/** Ids of every check that did not pass, in report order. */
export function failingIds(run: ContractRun): string[] {
  return run.results.filter((r) => r.status !== "pass").map((r) => r.id);
}

/** A one-line summary an operator can read in a banner. */
export function summarize(run: ContractRun): string {
  const failed = run.results.filter((r) => r.status === "fail").length;
  const skipped = run.results.filter((r) => r.status === "skip").length;
  if (run.ok) return `all ${run.results.length} contract checks passed`;
  const parts: string[] = [];
  if (failed) parts.push(`${failed} failed`);
  if (skipped) parts.push(`${skipped} could not be checked`);
  return parts.join(", ") || "no checks ran";
}

/** Render the run as a fixed-width table for a terminal or a CI log. */
export function renderTable(run: ContractRun): string {
  const width = Math.max(4, ...run.results.map((r) => r.id.length));
  const icon = { pass: "PASS", fail: "FAIL", skip: "SKIP" } as const;
  const lines = [
    `OpenClaw ${run.ocVersion ?? "(unknown)"} · Evolve plugin ${run.evolveVersion} · ${summarize(run)}`,
    "",
    ...run.results.map((r) => `  ${icon[r.status]}  ${r.id.padEnd(width)}  ${r.detail}`),
  ];
  return lines.join("\n");
}
