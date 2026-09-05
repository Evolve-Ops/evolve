/**
 * triggerProtocols — stdout protocol parsers for Layer C trigger interception.
 *
 * Phase 2.3 of the agent-freelance-bypass spec
 * (internal/spec-agent-freelance-bypass-phase2-2026-06-06.md). The TurnObserver's
 * Layer C interceptor runs an event_trigger's declared script and parses
 * the stdout per a code-registered protocol declared in the manifest's
 * ``event_triggers[].invocation.stdout_protocol`` field.
 *
 * Protocol parsers are intentionally code-registered (not config-driven):
 * a protocol is a tight contract between plugin and script, and config
 * drift between parser and emitter would be the exact silent-failure
 * mode this spec is meant to close. Adding a new protocol requires a
 * code change to this file AND the corresponding manifest schema enum
 * (docs/schemas/manifest-v7-spec.schema.json + the Python validator).
 *
 * Each parser returns ``ParsedReply``:
 *   * ``text``: what to direct-send to the channel (null → don't send;
 *               script intentionally chose silence)
 *   * ``outcome``: telemetry label, one of:
 *       "answered" | "rate_limited" | "budget_exceeded" | "refused"
 *       | "failed" | "silent"
 *
 * Parsing tolerates non-UTF8 noise, trailing whitespace, and ignores any
 * stdout before the first protocol line. If stdout is empty or no
 * protocol line is recognised, returns ``{text: null, outcome: "silent"}``
 * (the script chose not to reply). Callers can distinguish this from a
 * failure via ``exitCode``: non-zero exit + silent outcome = failure path
 * (caller posts ``fallback_text``); zero exit + silent outcome = clean
 * intentional silence (caller posts nothing).
 */
/**
 * The registered protocol names. Mirrors the JSON-schema enum at
 * docs/schemas/manifest-v7-spec.schema.json and the Python validator's
 * ``_KNOWN_STDOUT_PROTOCOLS`` frozenset. Keep these three locations in
 * sync — a test pins the cross-language equivalence.
 */
export const KNOWN_PROTOCOLS = [
    "atlas_research",
    "atlas_capture",
    "raw_text",
];
export function isKnownProtocol(name) {
    return typeof name === "string" && KNOWN_PROTOCOLS.includes(name);
}
/**
 * Parse stdout per protocol. Always returns a ParsedReply; never throws.
 * Caller branches on ``outcome`` + ``exitCode`` to decide:
 *   * outcome=answered/rate_limited/budget_exceeded/refused → direct-send ``text``
 *   * outcome=silent + exitCode=0 → intentional silence, don't send
 *   * outcome=silent + exitCode!=0 → script crashed, post fallback_text per on_failure
 *   * outcome=failed → ``text`` is the fallback reply per the protocol
 */
export function parseProtocol(protocol, stdout) {
    switch (protocol) {
        case "atlas_research":
            return parseAtlasResearch(stdout);
        case "atlas_capture":
            return parseAtlasCapture(stdout);
        case "raw_text":
            return parseRawText(stdout);
        default:
            // Type-system guarantees exhaustiveness; runtime path for the
            // case where an unrecognised string slips through (defensive).
            return { text: null, outcome: "silent" };
    }
}
// ── raw_text — generic protocol for forge-generated script-backed apps.
//
// Unlike the atlas_* protocols (which carry a line-prefixed marker grammar),
// a raw_text script has no markers: it prints the reply it wants posted to
// stdout and exits 0. On failure it exits non-zero and writes diagnostics to
// STDERR (not stdout), so the failure path resolves to silent + non-zero exit
// and the caller posts fallback_text. This is the contract forge stamps onto
// newly built apps that route a chat trigger through a bot-local script
// (internal/spec-agent-freelance-bypass-phase2-2026-06-06.md) — it lets Layer C
// intercept a generic script without the manifest having to invent a bespoke
// stdout grammar.
//
//   (non-empty stdout) → post the trimmed stdout verbatim (outcome=answered).
//   (empty stdout)     → silent. Caller treats exit!=0 as failure (fallback),
//                        exit=0 as intentional silence.
function parseRawText(stdout) {
    const text = stdout.trim();
    if (!text)
        return { text: null, outcome: "silent" };
    return { text, outcome: "answered" };
}
// ── atlas_research — see docs/atlas-app-manifests/atlas-on-demand-research.json
//
// Script emits one of:
//   "RESEARCH_ANSWERED:<base64-encoded-reply>"
//     Decode base64 (UTF-8) and post.
//   "RESEARCH_RATE_LIMITED:<reply-text>"
//   "RESEARCH_BUDGET_EXCEEDED:<reply-text>"
//   "RESEARCH_REFUSED:<reply-text>"
//     Post the text after the colon.
//   "RESEARCH_FAILED"
//     Post the fallback_text from the manifest invocation block.
//   (empty stdout, exit=0)
//     Guard refused silently — don't post anything.
function parseAtlasResearch(stdout) {
    const lines = stdout.split(/\r?\n/);
    for (const rawLine of lines) {
        const line = rawLine.trim();
        if (!line)
            continue;
        if (line.startsWith("RESEARCH_ANSWERED:")) {
            const b64 = line.slice("RESEARCH_ANSWERED:".length).trim();
            let decoded;
            try {
                decoded = Buffer.from(b64, "base64").toString("utf8");
            }
            catch {
                return { text: null, outcome: "failed" };
            }
            if (!decoded) {
                return { text: null, outcome: "failed" };
            }
            return { text: decoded, outcome: "answered" };
        }
        if (line.startsWith("RESEARCH_RATE_LIMITED:")) {
            return {
                text: line.slice("RESEARCH_RATE_LIMITED:".length).trim(),
                outcome: "rate_limited",
            };
        }
        if (line.startsWith("RESEARCH_BUDGET_EXCEEDED:")) {
            return {
                text: line.slice("RESEARCH_BUDGET_EXCEEDED:".length).trim(),
                outcome: "budget_exceeded",
            };
        }
        if (line.startsWith("RESEARCH_REFUSED:")) {
            return {
                text: line.slice("RESEARCH_REFUSED:".length).trim(),
                outcome: "refused",
            };
        }
        if (line === "RESEARCH_FAILED") {
            return { text: null, outcome: "failed" };
        }
    }
    // No protocol line recognised → silent. Caller decides if that's
    // intentional (exit=0) or failure-mode (exit!=0, post fallback_text).
    return { text: null, outcome: "silent" };
}
// ── atlas_capture — see docs/atlas-app-manifests/atlas-article-capture.json
//
// Script emits one of:
//   "CAPTURE_ARCHIVED:<bucket>"
//   "CAPTURE_DEDUP:<bucket>"
//     Both are silent successes — operator-visible via bucket telemetry,
//     not user-visible. Don't post.
//   "CAPTURE_REFUSED:<reply-text>"
//     User-visible refusal (e.g. opt-out enforcement). Post text.
//   "CAPTURE_RATE_LIMITED:<reply-text>"
//     Post text.
//   "CAPTURE_FAILED"
//     Post fallback_text per the manifest.
//   (empty stdout, exit=0)
//     Silent — capture doesn't have a "no work to do" emit path; clean
//     empty means the URL didn't pass the guard.
function parseAtlasCapture(stdout) {
    const lines = stdout.split(/\r?\n/);
    for (const rawLine of lines) {
        const line = rawLine.trim();
        if (!line)
            continue;
        if (line.startsWith("CAPTURE_ARCHIVED:")) {
            // Silent success — telemetry only. The bucket is logged elsewhere.
            return { text: null, outcome: "silent" };
        }
        if (line.startsWith("CAPTURE_DEDUP:")) {
            return { text: null, outcome: "silent" };
        }
        if (line.startsWith("CAPTURE_REFUSED:")) {
            return {
                text: line.slice("CAPTURE_REFUSED:".length).trim(),
                outcome: "refused",
            };
        }
        if (line.startsWith("CAPTURE_RATE_LIMITED:")) {
            return {
                text: line.slice("CAPTURE_RATE_LIMITED:".length).trim(),
                outcome: "rate_limited",
            };
        }
        if (line === "CAPTURE_FAILED") {
            return { text: null, outcome: "failed" };
        }
    }
    return { text: null, outcome: "silent" };
}
//# sourceMappingURL=triggerProtocols.js.map