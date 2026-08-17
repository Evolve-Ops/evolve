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
import { EVOLVE_APP_INTEGRITY_VERSION } from "./types.js";
const MAX_SUMMARY_CHARS = 500;
/**
 * Field names the shell/exec runtimes use for the command string in args.
 * Deliberately narrow — the OC bash tool and Codex exec both use ``command``;
 * the rest are close synonyms. Generic fields like ``input`` / ``code`` are
 * EXCLUDED so a non-shell tool whose payload happens to mention an app path is
 * never even considered.
 */
const COMMAND_STRING_FIELDS = ["command", "cmd", "shellCommand", "commandLine"];
/** Field names that may carry the command as an argv array. */
const COMMAND_ARRAY_FIELDS = ["command", "argv"];
/**
 * Extract the command string from a tool call's ``args``. Returns null when no
 * command-shaped field is present (→ the middleware treats it as a non-shell
 * tool and passes through). Total; never throws.
 */
export function extractCommand(args) {
    if (typeof args === "string")
        return args.trim() ? args : null;
    if (!args || typeof args !== "object")
        return null;
    const o = args;
    for (const k of COMMAND_STRING_FIELDS) {
        const v = o[k];
        if (typeof v === "string" && v.trim())
            return v;
    }
    for (const k of COMMAND_ARRAY_FIELDS) {
        const v = o[k];
        if (Array.isArray(v) && v.length > 0 && v.every((x) => typeof x === "string")) {
            return v.join(" ");
        }
    }
    return null;
}
/** Concatenate the text blocks of a result. Non-text blocks are ignored. */
export function textOf(result) {
    if (!result || !Array.isArray(result.content))
        return "";
    const parts = [];
    for (const b of result.content) {
        if (b && b.type === "text") {
            const t = b.text;
            if (typeof t === "string")
                parts.push(t);
        }
    }
    return parts.join("\n");
}
/**
 * Strip ANSI escapes and control characters, collapse whitespace, and clamp to
 * a short tail. Used to render an honest-but-bounded failure reason — never a
 * full raw traceback, and safe against non-UTF8 / control-char noise on stderr.
 */
export function sanitizeReason(raw, maxChars = MAX_SUMMARY_CHARS) {
    let s;
    if (typeof raw === "string")
        s = raw;
    else if (raw == null)
        s = "";
    else {
        try {
            s = String(raw);
        }
        catch {
            s = "";
        }
    }
    // ANSI/CSI escape sequences: ESC [ ... final byte.
    // eslint-disable-next-line no-control-regex
    s = s.replace(/\x1b\[[0-9;?]*[ -\/]*[@-~]/g, " ");
    // C0 controls (0x00-0x1F), DEL, and C1 (0x7F-0x9F).
    // eslint-disable-next-line no-control-regex
    s = s.replace(/[\x00-\x1f\x7f-\x9f]/g, " ");
    s = s.replace(/�/g, " "); // U+FFFD replacement chars (non-UTF8 decode noise)
    s = s.replace(/\s+/g, " ").trim();
    if (s.length > maxChars) {
        // Keep the TAIL — the proximate error usually trails a traceback.
        s = "…" + s.slice(s.length - maxChars + 1);
    }
    return s;
}
/**
 * The proximate error: the LAST non-empty line of a (possibly multi-line)
 * reason source, sanitized and clamped short. Used for model-visible content so
 * a full multi-line traceback never reaches the model — only its trailing
 * concise error (e.g. "KeyError: 'q'").
 */
export function lastMeaningfulLine(raw, maxChars = 200) {
    let s;
    if (typeof raw === "string")
        s = raw;
    else if (raw == null)
        s = "";
    else {
        try {
            s = String(raw);
        }
        catch {
            s = "";
        }
    }
    const lines = s.split(/\r?\n/).map((l) => l.trim());
    let last = "";
    for (let i = lines.length - 1; i >= 0; i--) {
        if (lines[i]) {
            last = lines[i] ?? "";
            break;
        }
    }
    return sanitizeReason(last, maxChars);
}
function asRecord(v) {
    return v && typeof v === "object" && !Array.isArray(v)
        ? v
        : null;
}
/** Read a numeric exit-code-ish field from a details object. */
function exitCodeFromDetails(details) {
    for (const k of ["exitCode", "exit_code", "code", "returncode", "returnCode"]) {
        const v = details[k];
        if (typeof v === "number" && Number.isFinite(v))
            return v;
        if (typeof v === "string" && /^-?\d+$/.test(v.trim()))
            return parseInt(v, 10);
    }
    return null;
}
/**
 * Scan text for an explicit exit-code marker and return the last NON-ZERO code
 * found, or null. Deliberately asymmetric: a text marker may only ever
 * ESCALATE an outcome to "failed" — a ``exit code 0`` in script-/model-visible
 * stdout is IGNORED and can never certify success (a script controls its own
 * stdout, so trusting a self-reported zero would be a confabulation hole). Used
 * only as a failure safety-net when the host provides no authoritative numeric
 * exit code in ``details``.
 */
function lastNonZeroExitFromText(text) {
    if (!text)
        return null;
    const re = /\bexit(?:\s+code|code|\s+status)?\s*[:=]?\s*(-?\d{1,9})\b/gi;
    let m;
    let last = null;
    while ((m = re.exec(text)) !== null) {
        const n = parseInt(m[1] ?? "", 10);
        if (Number.isFinite(n) && n !== 0)
            last = n;
    }
    return last;
}
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
export function readOutcome(event) {
    const result = event?.result;
    const details = asRecord(result?.details);
    const text = textOf(result);
    // Sanitized failure reason: prefer an explicit stderr field, else the text tail.
    const stderr = details && typeof details["stderr"] === "string"
        ? details["stderr"]
        : "";
    const reasonSource = stderr.trim() ? stderr : text;
    // The host-AUTHORITATIVE exit code (from details only). A text-scraped code is
    // NEVER authoritative for success — see lastNonZeroExitFromText.
    const detailsCode = details ? exitCodeFromDetails(details) : null;
    // (1) A HOST error signal dominates — fail toward truth. ``isError`` and an
    // explicit ``details.status === "error"`` / truthy ``details.error`` are
    // taken as authoritative even if a competing exit code reads 0 (a
    // contradictory host shape must never resolve to "success").
    const hostError = event?.isError === true ||
        (details
            ? (typeof details["status"] === "string" &&
                details["status"].toLowerCase() === "error") ||
                (typeof details["error"] === "string" && details["error"].trim() !== "") ||
                details["error"] === true
            : false);
    if (hostError) {
        return failed(detailsCode ?? lastNonZeroExitFromText(text), reasonSource);
    }
    // (2) A host-authoritative exit code decides: non-zero ⇒ failed, zero ⇒ ok.
    // Only a number the HOST put in details certifies success.
    if (detailsCode !== null) {
        if (detailsCode === 0)
            return { kind: "ok", exitCode: 0, errorSummary: "", errorLine: "" };
        return failed(detailsCode, reasonSource);
    }
    // (3) No host-authoritative signal. A text marker may only ESCALATE to
    // failed (never certify success). A non-zero exit code anywhere in the output
    // is evidence of failure; trust it.
    const textFail = lastNonZeroExitFromText(text);
    if (textFail !== null)
        return failed(textFail, reasonSource);
    // (4) Genuinely unknown — NO authoritative signal and no failure evidence. We
    // do NOT certify success on the absence of a signal (that would let a crashed
    // script with only buffered stdout be stamped "ok"). Return "unknown"; the
    // middleware leaves the raw result UNTOUCHED so the model sees exactly what
    // happened, never a false "ok".
    return { kind: "unknown", exitCode: null, errorSummary: "", errorLine: "" };
}
function failed(exitCode, reasonSource) {
    return {
        kind: "failed",
        exitCode,
        errorSummary: sanitizeReason(reasonSource),
        errorLine: lastMeaningfulLine(reasonSource),
    };
}
// ── Building the honest structured result ────────────────────────────────────
/** Default plain-language lead, mirrors plugin_intercept's _DEFAULT_FALLBACK_TEXT. */
const FAILURE_LEAD = "Sorry — I couldn't complete that just now. Please try again in a few minutes.";
/**
 * Build the model-visible FAILURE result for a recognized app script. The raw
 * traceback is NOT included — the content is a clean plain-language message; the
 * sanitized technical reason rides in ``details`` for telemetry/operators.
 */
export function buildFailureResult(appId, script, outcome) {
    const codePart = outcome.exitCode !== null ? ` (exit ${outcome.exitCode})` : "";
    // Only the proximate error line goes in the model-visible content — never a
    // multi-line raw traceback. The fuller reason rides in details.error_summary.
    const reasonPart = outcome.errorLine ? ` Reason: ${outcome.errorLine}` : "";
    const text = `${FAILURE_LEAD}${codePart}${reasonPart}`;
    const integrity = {
        version: EVOLVE_APP_INTEGRITY_VERSION,
        status: "failed",
        app_id: appId,
        script,
        exit_code: outcome.exitCode,
        ...(outcome.errorSummary ? { error_summary: outcome.errorSummary } : {}),
    };
    return {
        content: [{ type: "text", text }],
        details: { evolve_app_integrity: integrity },
    };
}
/**
 * Build the SUCCESS result for a recognized app script. Non-destructive: the
 * original content is preserved VERBATIM (the real output is the payload) and
 * only the structured ``evolve_app_integrity`` marker is merged into
 * ``details``. Returns null when the merge can't be done safely (original
 * ``details`` is a non-object/array) — the caller then passes through untouched
 * rather than risk clobbering an unexpected ``details`` shape.
 */
export function buildSuccessResult(appId, script, outcome, original) {
    if (!original || !Array.isArray(original.content))
        return null;
    const existing = original.details;
    // Only merge into undefined/null or a plain object. A non-object details
    // (string/array/number) is left to pass through untouched.
    let mergedDetails;
    if (existing == null) {
        mergedDetails = {};
    }
    else if (asRecord(existing)) {
        mergedDetails = { ...existing };
    }
    else {
        return null;
    }
    const integrity = {
        version: EVOLVE_APP_INTEGRITY_VERSION,
        status: "ok",
        app_id: appId,
        script,
        exit_code: outcome.exitCode,
    };
    mergedDetails["evolve_app_integrity"] = integrity;
    return { content: original.content, details: mergedDetails };
}
//# sourceMappingURL=toolOutcome.js.map