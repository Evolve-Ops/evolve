/**
 * The nine checks that make up the OpenClaw compatibility contract.
 *
 * Each one is an assumption Evolve makes about OpenClaw that a point
 * release has already broken, or could break the same way. Each names the
 * incident document that motivated it, so a red row in the matrix reads as
 * "this is the thing that took the pod down last time".
 *
 * All checks are pure over `OcProbe`, which is why the same code runs
 * against a real installed OpenClaw (the nightly matrix, CI) and against
 * the fakes in `tests/contract.checks.test.mjs` — one fake behaves like a
 * healthy runtime, one reproduces the 2026-09-07 incident. A check that
 * cannot be answered returns `skip`, and a run with any skip is not a pass:
 * "we could not tell" must never read as "validated".
 */
import type { CliResult, ContractCheck } from "./types.js";
/**
 * True when the CLI rejected the invocation because the subcommand or flag
 * does not exist — as opposed to running and reporting a normal condition
 * (no gateway, empty list). The distinction decides fail-vs-pass on the
 * surface checks, so it is deliberately conservative: only unmistakable
 * "I don't know that" wording counts.
 */
export declare function looksLikeUnknownCommand(r: CliResult): boolean;
/** Slice the source of one `api.on("<hook>", …)` registration out of a file. */
export declare function hookHandlerSource(source: string, hook: string): string | null;
/** The contract, in the order a report renders it. */
export declare const CONTRACT_CHECKS: readonly ContractCheck[];
//# sourceMappingURL=checks.d.ts.map