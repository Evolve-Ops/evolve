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
import type { ContractRun, OcProbe } from "./types.js";
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
export declare function runContract(probe: OcProbe, opts: RunOptions): Promise<ContractRun>;
/** Ids of every check that did not pass, in report order. */
export declare function failingIds(run: ContractRun): string[];
/** A one-line summary an operator can read in a banner. */
export declare function summarize(run: ContractRun): string;
/** Render the run as a fixed-width table for a terminal or a CI log. */
export declare function renderTable(run: ContractRun): string;
//# sourceMappingURL=run.d.ts.map