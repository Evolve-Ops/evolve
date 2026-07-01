/**
 * toolOutcome — pure helpers that read the REAL outcome of a recognized
 * app-script tool result and build the honest structured replacement.
 *
 * Every function here is pure and total (never throws, no I/O) so the fail-to-
 * truth guarantee is exhaustively testable. The middleware wraps these and, on
 * any surprise, passes the original result through untouched.
 *
 * Failure detection is conservative-by-construction. The high-stakes action —
 * replacing the model-visible content with a "couldn't do that" message —
 * requires a POSITIVE failure signal (host ``isError``, a parsed non-zero exit
 * code, or an explicit error status in ``details``). Absent any such signal the
 * outcome is "ok" (success), and absent the ability to read anything at all the
 * outcome is "unknown" → the middleware leaves the result untouched. A
 * success-looking stdout body can therefore NEVER override a non-zero exit
 * code: the exit code wins.
 */
import type { OpenClawAgentToolResult, AgentToolResultMiddlewareEvent } from "./types.js";
export type OutcomeKind = "ok" | "failed" | "unknown";
export interface ReadOutcome {
    kind: OutcomeKind;
    /** Parsed exit code, or null when none could be read. */
    exitCode: number | null;
    /**
     * Fuller sanitized failure reason (≤500 chars) for ``details`` /
     * operators — NOT shown verbatim as model narration. "" unless failed.
     */
    errorSummary: string;
    /**
     * The proximate error — the LAST non-empty line of stderr/output, sanitized
     * and clamped short. This is the only failure text put in the model-visible
     * content; a multi-line raw traceback never reaches the model. "" unless
     * failed.
     */
    errorLine: string;
}
/**
 * Extract the command string from a tool call's ``args``. Returns null when no
 * command-shaped field is present (→ the middleware treats it as a non-shell
 * tool and passes through). Total; never throws.
 */
export declare function extractCommand(args: unknown): string | null;
/** Concatenate the text blocks of a result. Non-text blocks are ignored. */
export declare function textOf(result: OpenClawAgentToolResult | undefined | null): string;
/**
 * Strip ANSI escapes and control characters, collapse whitespace, and clamp to
 * a short tail. Used to render an honest-but-bounded failure reason — never a
 * full raw traceback, and safe against non-UTF8 / control-char noise on stderr.
 */
export declare function sanitizeReason(raw: unknown, maxChars?: number): string;
/**
 * The proximate error: the LAST non-empty line of a (possibly multi-line)
 * reason source, sanitized and clamped short. Used for model-visible content so
 * a full multi-line traceback never reaches the model — only its trailing
 * concise error (e.g. "KeyError: 'q'").
 */
export declare function lastMeaningfulLine(raw: unknown, maxChars?: number): string;
/**
 * Read the real outcome of a tool result. Signal priority (fail toward truth;
 * only a HOST-authoritative signal can certify success):
 *   1. host error — ``event.isError === true`` or ``details.status==="error"``
 *      / truthy ``details.error`` → failed (dominates even a 0 exit code).
 *   2. host-authoritative exit code (``details.exitCode`` & kin): 0 ⇒ ok,
 *      non-zero ⇒ failed.
 *   3. no authoritative signal, but a NON-ZERO exit marker in the output text
 *      ⇒ failed (text may only ever ESCALATE to failed).
 *   4. otherwise ⇒ "unknown" — no authoritative signal and no failure evidence;
 *      the middleware passes the raw result through UNTOUCHED.
 *
 * Two invariants this encodes:
 *   - There is NO path from a non-zero exit (or a host error) to "ok".
 *   - Success is certified ONLY by a host-authoritative zero exit code —
 *     never by script-/model-visible stdout text, and never by the mere
 *     absence of a signal. A crashed script whose only evidence is buffered
 *     stdout therefore resolves to "unknown" (passthrough), not "ok".
 */
export declare function readOutcome(event: AgentToolResultMiddlewareEvent): ReadOutcome;
/**
 * Build the model-visible FAILURE result for a recognized app script. The raw
 * traceback is NOT included — the content is a clean plain-language message; the
 * sanitized technical reason rides in ``details`` for telemetry/operators.
 */
export declare function buildFailureResult(appId: string, script: string, outcome: ReadOutcome): OpenClawAgentToolResult;
/**
 * Build the SUCCESS result for a recognized app script. Non-destructive: the
 * original content is preserved VERBATIM (the real output is the payload) and
 * only the structured ``evolve_app_integrity`` marker is merged into
 * ``details``. Returns null when the merge can't be done safely (original
 * ``details`` is a non-object/array) — the caller then passes through untouched
 * rather than risk clobbering an unexpected ``details`` shape.
 */
export declare function buildSuccessResult(appId: string, script: string, outcome: ReadOutcome, original: OpenClawAgentToolResult): OpenClawAgentToolResult | null;
//# sourceMappingURL=toolOutcome.d.ts.map