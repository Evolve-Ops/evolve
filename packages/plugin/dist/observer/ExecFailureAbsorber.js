/**
 * ExecFailureAbsorber — channel hygiene for OpenClaw's raw exec-failure
 * trailers (design: internal/design-exec-failure-hygiene-2026-08-31.md, A1).
 *
 * The invariant: a channel message about an internal failure is either
 * explained-and-actionable in user language, or absorbed — raw
 * `⚠️ 🛠️ Exec failed: …` trailers never reach a user channel. Absorbed is
 * not vanished: every match is appended to the per-bot ledger at
 * {sharedDir}/{botId}/exec-failures/exec-failures-YYYY-MM-DD.jsonl, which
 * the admin-side `bot_exec_failures` Signal producer aggregates (A2).
 *
 * Seam (verified against openclaw/openclaw main AND the deployed
 * 2026.7.1-2 dist, 2026-08-31): the `message_sending` plugin hook fires in
 * the shared outbound delivery pipeline (`applyMessageSendingHook`,
 * src/infra/outbound/deliver-hooks.ts, invoked per-payload from
 * deliver-prepare) for BOTH channel-bound exec-failure flavors:
 *   - the host-composed tool-error warning (`buildFailureWarning`,
 *     src/agents/embedded-agent-runner/run/tool-error-warning.ts) pushed as
 *     its own reply payload when a run ends on a tool failure with no
 *     user-facing reply — the exact family that reached Telegram on
 *     2026-08-31; and
 *   - the exec-approval follow-up direct sends + heartbeat-relayed
 *     notify-on-exit events (both route through `sendMessage` →
 *     `sendDurableMessageBatchCore` → deliver-prepare).
 * A `{cancel: true}` result suppresses the payload
 * (`cancelled_by_message_sending_hook`); a `{content}` result rewrites it.
 * The hook is fail-open host-side (a thrown handler logs and delivery
 * proceeds), and it is CHANNEL-facing only — the model-visible tool result
 * is untouched (A4: the bot still sees and adapts to its own failures).
 *
 * OBSERVE-ONLY by default: every match is ledgered as `would_absorb` /
 * `would_strip` but the message is delivered unchanged. Set the plugin
 * config `execFailureAbsorb: true` (exact boolean, layer2-style fail-safe
 * arming) to actually absorb. Merging is non-arming.
 *
 * SECOND FAMILY (2026-09-09) — OC's unclassified agent-run-failure banner,
 * `⚠️ Agent run failed (model: <provider/model>).`. Same invariant, same
 * ledger, but a different arming rule: it absorbs on SCHEDULED sessions
 * (heartbeat / cron) without the knob, and is delivered unchanged
 * everywhere else. See {@link isUnclassifiedRunFailureLine} for why the
 * bare form specifically is unfit for a channel, and `absorbRunFailure`
 * for why this family does not need `execFailureAbsorb`.
 */
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { classifySessionKind } from "../tools/ToolProfiles.js";
/**
 * The matched line families, ported from OpenClaw's own scrub regexes in
 * src/shared/text/assistant-visible-text.ts (INTERNAL_COMPACT_FAILURE_
 * TRACE_LINE_RE) plus the notify-on-exit summary composed by
 * maybeNotifyOnExit (src/agents/bash-tools.exec-runtime.ts) and the
 * process-diagnostic branch of buildFailureWarning. Vendoring OC's own
 * patterns keeps us tracking their format; a drift upstream shows up as
 * a trailer in the ledgerless channel again, not as a false absorb.
 */
// Emoji written codepoint-explicit with the U+FE0F variation selector
// OPTIONAL: OC emits ⚠️/🛠️ with VS16 today, but a channel adapter that
// NFKC-normalizes (or an upstream re-render) could drop it — and a
// selector-anchored regex would then go silently blind fleet-wide.
const WARN = "\u26A0\uFE0F?"; // ⚠ + optional VS16
const TOOL = "\u{1F6E0}\uFE0F?"; // 🛠 + optional VS16
const TOOLBOX = "\u{1F9F0}\uFE0F?"; // 🧰 + optional VS16
// Host-composed compact failure warning: "⚠️ 🛠️ Exec failed: …",
// "⚠️ 🛠️ Bash failed (exit 1)", "⚠️ 🛠️ `cmd` (agent) failed: …".
const COMPACT_FAILURE_TRAILER_RE = new RegExp(`^(?:>\\s*)?${WARN}\\s*${TOOL}\\s+(?:(?:Exec|Bash)\\s+(?:failed|blocked)(?:(?:\\s+\\(exit\\s+-?\\d+\\))|(?:\\s*:[^\\r\\n]*))?|\\S[^\\r\\n]*\\s+\\(agent\\)\`{0,2}\\s+(?:failed|blocked)(?:\\s*:[^\\r\\n]*)?)\\s*$`, "iu");
// Process-diagnostic flavor of the same host warning:
// "⚠️ 🧰 Process (abc12345) failed (exit 1): …" / "… failed (timed out)…".
const PROCESS_FAILURE_TRAILER_RE = new RegExp(`^(?:>\\s*)?${WARN}\\s*${TOOLBOX}\\s*Process\\b[^\\r\\n]*\\s(?:failed|blocked)\\s*\\((?:exit\\s+-?\\d+|signal\\s+[^)]+|timed out[^)]*)\\)[^\\r\\n]*$`, "iu");
// Background notify-on-exit summary (model-relayed via heartbeat):
// "Exec failed (0f3a2b1c, exit 1) :: <tail>".
const NOTIFY_ON_EXIT_FAILED_RE = new RegExp(`^(?:>\\s*)?(?:${WARN}\\s*)?Exec failed \\([a-z0-9_-]{1,64}, (?:exit\\s+-?\\d+|code\\s+-?\\d+|signal\\s+[^)]+)\\)(?:\\s*::[^\\r\\n]*)?$`, "iu");
const TRAILER_RES = [
    COMPACT_FAILURE_TRAILER_RE,
    PROCESS_FAILURE_TRAILER_RE,
    NOTIFY_ON_EXIT_FAILED_RE,
];
/**
 * OC's UNCLASSIFIED assistant-request-failure banner:
 * "⚠️ Agent run failed (model: anthropic/claude-haiku-4-5)."
 *
 * `renderAssistantRequestFailureCopy` (embedded-agent-helpers, verified
 * against the deployed 2026.9.2 dist) reaches this bare form ONLY on its
 * last branch — no reason, no HTTP status, and a target it could not
 * classify (`providerRuntimeFailureKind: "unclassified"`). Every branch
 * above it renders an actionable sentence instead ("… (rate limit, HTTP
 * 429). This is usually temporary — try again shortly.", "Re-authenticate
 * the provider…"), and those are deliberately NOT matched here: they tell
 * the reader something true and actionable, which is the invariant's
 * "explained-and-actionable" arm.
 *
 * So the model id in the parens is a placeholder OC fell back to naming,
 * not a diagnosis — which is exactly what makes the bare form unfit for a
 * user channel: it reads as a provider outage on a turn where the real
 * cause was something else entirely (2026-09-09: a malformed tool call on
 * a team bot's heartbeat, `rawErrorPreview: "Provider completed tool call with
 * malformed JSON arguments"`).
 */
const UNCLASSIFIED_RUN_FAILURE_RE = new RegExp(`^(?:>\\s*)?${WARN}\\s*Agent run failed \\((?:model|provider):\\s*[^)\\r\\n]{1,200}\\)\\.$`, "iu");
/**
 * True when `line` is OC's unclassified agent-run-failure banner.
 *
 * Kept separate from {@link isExecFailureTrailerLine} because the two
 * families have different arming rules: exec trailers stay observe-only
 * behind `execFailureAbsorb`, while this one absorbs on scheduled sessions
 * as soon as it is deployed (see handleMessageSending).
 */
export function isUnclassifiedRunFailureLine(line) {
    const trimmed = line.trim();
    if (!trimmed)
        return false;
    return UNCLASSIFIED_RUN_FAILURE_RE.test(trimmed);
}
export function isExecFailureTrailerLine(line) {
    const trimmed = line.trim();
    if (!trimmed)
        return false;
    return TRAILER_RES.some((re) => re.test(trimmed));
}
/**
 * Scan `content` line-by-line, skipping fenced code blocks (a user asking
 * about a pasted trailer must not have their quote eaten — same semantics
 * as OC's own stripInternalTraceLines). Returns null when no line matched.
 *
 * `matches` selects the family; it defaults to the exec-trailer set so
 * existing callers are unchanged. Pass {@link isUnclassifiedRunFailureLine}
 * to scan for OC's unclassified agent-run-failure banner instead.
 */
export function decideAbsorb(content, matches = isExecFailureTrailerLine) {
    const lines = content.split("\n");
    const matched = [];
    const kept = [];
    let inFence = false;
    for (const line of lines) {
        if (/^\s*(?:```|~~~)/.test(line)) {
            inFence = !inFence;
            kept.push(line);
            continue;
        }
        if (!inFence && matches(line)) {
            matched.push(line.trim());
            continue;
        }
        kept.push(line);
    }
    if (matched.length === 0)
        return null;
    const remainingText = kept.join("\n");
    return {
        matched,
        remaining: remainingText.trim().length > 0 ? remainingText : null,
    };
}
export class ExecFailureAbsorber {
    config;
    logger;
    dirInitialized = false;
    warnedEACCES = false;
    constructor(config, logger) {
        this.config = config;
        this.logger = logger;
    }
    register(api) {
        if (this.config.armedWarning) {
            // Loud refusal beats a "true" string that silently did nothing.
            this.logger.warn(this.config.armedWarning);
        }
        // Guarded like ToolCallGate's before_tool_call registration: a gateway
        // too old to know message_sending must not abort the REST of plugin
        // init (defer tool, session tools, … register after us in index.ts).
        // Fleet OC 2026.7.1-2 ships the hook (verified in the deployed dist).
        try {
            api.on("message_sending", (event, ctx) => this.handleMessageSending(event, ctx), { name: "evolve-exec-failure-absorber" });
        }
        catch (err) {
            this.logger.warn(`ExecFailureAbsorber: message_sending hook not supported by this ` +
                `gateway (${err}) — absorber INACTIVE` +
                (this.config.armed
                    ? "; execFailureAbsorb=true is armed but cannot take effect."
                    : "."));
            return;
        }
        this.logger.info(`Evolve exec-failure absorber registered (bot=${this.config.botId}, ` +
            `exec-trailer mode=${this.config.armed ? "ABSORB" : "observe-only"}; ` +
            `unclassified run-failure banners ABSORB on scheduled sessions)`);
    }
    /**
     * message_sending handler. Fail-open BY CONSTRUCTION: every path that is
     * not a confident match returns undefined (deliver unchanged), and any
     * internal error is caught and logged — hygiene must never eat a real
     * reply or block delivery.
     */
    handleMessageSending(event, ctx) {
        try {
            const content = typeof event?.content === "string" ? event.content : "";
            // Cheap substring pre-filter, a strict SUPERSET of the line regexes
            // (base emoji without the VS16, so selector loss can't skip it; both
            // realistic casings of the notify-on-exit lead). Keeps the common
            // no-trailer path to a few SIMD memchr scans, no regex.
            if (!content ||
                (!content.includes("\u{1F6E0}") && // 🛠 (compact failure family)
                    !content.includes("\u{1F9F0}") && // 🧰 (process flavor)
                    !content.includes("Exec failed (") &&
                    !content.includes("exec failed (") &&
                    !content.includes("Agent run failed (") && // unclassified banner
                    !content.includes("agent run failed ("))) {
                return undefined;
            }
            // ── Unclassified agent-run-failure banner (scheduled sessions) ────
            // Runs BEFORE the exec-trailer branch's near-miss fallthrough, and
            // on its own arming rule: absorbed on scheduled sessions as soon as
            // it deploys, rather than waiting on `execFailureAbsorb`.
            //
            // Why this family does not need the knob. The knob's documented
            // reason to exist is the design's own "known limit" — "a
            // user-addressed turn whose ONLY reply was the trailer gets no reply
            // at all" (design-exec-failure-hygiene-2026-08-31.md) — and the same
            // doc calls full absorption "correct for the incident class
            // (background runs the operator should never hear from)". The
            // scheduled gate below IS that class and excludes the hazard: nobody
            // is waiting on a heartbeat's reply. Everything else about the
            // module's posture is unchanged — the row is ledgered first and
            // absorption is conditioned on it landing, so this stays
            // absorbed-not-vanished and still reaches the operator through the
            // `bot_exec_failures` Signal.
            const runFailure = decideAbsorb(content, isUnclassifiedRunFailureLine);
            if (runFailure) {
                const result = this.absorbRunFailure(runFailure, content, event, ctx);
                if (result !== undefined)
                    return result;
            }
            const decision = decideAbsorb(content);
            if (!decision) {
                // Pre-filter hit but no line matched. If the payload also talks
                // about a failure, ledger a bounded near-miss marker (hash only —
                // the payload may be ordinary user prose). This is the
                // false-negative / upstream-format-drift signal the observe-only
                // period exists to collect; without it the ledger can only ever
                // demonstrate matches, never misses.
                if (!runFailure && /failed/i.test(content)) {
                    this.appendNearMiss(content, ctx);
                }
                return undefined;
            }
            const fullyAbsorbed = decision.remaining === null;
            const action = this.config.armed
                ? fullyAbsorbed
                    ? "absorbed"
                    : "stripped"
                : fullyAbsorbed
                    ? "would_absorb"
                    : "would_strip";
            const recorded = this.appendLedger({
                ts: new Date().toISOString(),
                bot_id: this.config.botId,
                action,
                armed: this.config.armed,
                channel: ctx?.channelId ?? null,
                account_id: ctx?.accountId ?? null,
                to: event?.to ?? null,
                session_key: ctx?.sessionKey ?? null,
                matched_lines: decision.matched,
                // Full payload only when we removed all of it — that is the record
                // an operator (or A3's explanation registry) will want verbatim.
                full_content: fullyAbsorbed ? content : undefined,
            });
            if (!this.config.armed)
                return undefined;
            if (!recorded) {
                // "Absorbed ≠ vanished" is the invariant: if the record could not
                // be persisted, refuse to absorb — deliver unchanged so the failure
                // stays observable SOMEWHERE (the channel) rather than nowhere.
                this.logger.warn("ExecFailureAbsorber: ledger append failed — delivering the " +
                    "trailer unchanged instead of absorbing without a record.");
                return undefined;
            }
            if (fullyAbsorbed) {
                return { cancel: true, cancelReason: "evolve_exec_failure_absorbed" };
            }
            return { content: decision.remaining };
        }
        catch (err) {
            this.logger.debug(`ExecFailureAbsorber: handler error (fail-open): ${err}`);
            return undefined;
        }
    }
    /**
     * Decide the unclassified-run-failure family for one payload.
     *
     * Returns the hook result when this family owns the outcome, or
     * `undefined` to let the caller carry on to the exec-trailer branch.
     *
     * The gate is `classifySessionKind(...).kind === "scheduled"` —
     * heartbeat / cron / scheduled keys, per SCHEDULED_TAGS. It is checked
     * against the session KEY and not the channel, which matters: the
     * failing heartbeat is delivered over a real Slack DM
     * (`agent:main:main:heartbeat` → channel `slack`), so a channel-based
     * test would read it as user traffic. classifySessionKind orders the
     * scheduled check ahead of its channel check for exactly this reason.
     *
     * A match on any OTHER kind — including a missing sessionKey, which
     * classifies as "other" — is ledgered as `would_absorb_unscheduled` and
     * DELIVERED. That is the fail-open direction (a person waiting on a
     * reply still hears something), and it is deliberately self-diagnosing:
     * if the banner keeps reaching a channel, the ledger says whether the
     * gate saw a session key at all rather than leaving a silent no-op.
     * Those rows sit outside the monitor's `_ACTIONS` set, like `near_miss`,
     * so they stay diagnostic and raise no Signal — nothing was suppressed,
     * so there is nothing the operator needs told.
     */
    absorbRunFailure(decision, content, event, ctx) {
        const fullyAbsorbed = decision.remaining === null;
        const kind = classifySessionKind(ctx?.sessionKey, null).kind;
        const scheduled = kind === "scheduled";
        const action = scheduled
            ? fullyAbsorbed
                ? "absorbed"
                : "stripped"
            : fullyAbsorbed
                ? "would_absorb_unscheduled"
                : "would_strip_unscheduled";
        const recorded = this.appendLedger({
            ts: new Date().toISOString(),
            bot_id: this.config.botId,
            action,
            // The exec knob does not gate this family; record what it was anyway
            // so a ledger row stays interpretable next to the exec-trailer rows.
            armed: this.config.armed,
            family: "unclassified_run_failure",
            session_kind: kind,
            channel: ctx?.channelId ?? null,
            account_id: ctx?.accountId ?? null,
            to: event?.to ?? null,
            session_key: ctx?.sessionKey ?? null,
            matched_lines: decision.matched,
            full_content: fullyAbsorbed ? content : undefined,
        });
        if (!scheduled) {
            this.logger.info(`ExecFailureAbsorber: unclassified run-failure banner on a ` +
                `${kind} session (${String(ctx?.sessionKey ?? "no session key")}) ` +
                `— delivering unchanged; only scheduled sessions are absorbed.`);
            return undefined;
        }
        if (!recorded) {
            // Same invariant as the exec branch: absorbed ≠ vanished, so a row
            // that could not be persisted means we must not absorb.
            this.logger.warn("ExecFailureAbsorber: ledger append failed — delivering the " +
                "unclassified run-failure banner unchanged instead of absorbing " +
                "without a record.");
            return undefined;
        }
        this.logger.info(`ExecFailureAbsorber: absorbed an unclassified run-failure banner on ` +
            `a scheduled session (${String(ctx?.sessionKey ?? "")}) — the ` +
            `failure is ledgered and surfaces via the bot_exec_failures Signal.`);
        if (fullyAbsorbed) {
            return { cancel: true, cancelReason: "evolve_run_failure_absorbed" };
        }
        return { content: decision.remaining };
    }
    // Near-miss rows are drift telemetry, not payload capture: bound the
    // volume (🛠️ tool-progress lines mentioning "failed" are legitimate
    // prose) and store only a content hash + length, never the text.
    nearMissCount = 0;
    static NEAR_MISS_MAX_PER_PROCESS = 50;
    appendNearMiss(content, ctx) {
        if (this.nearMissCount >= ExecFailureAbsorber.NEAR_MISS_MAX_PER_PROCESS) {
            return;
        }
        this.nearMissCount += 1;
        this.appendLedger({
            ts: new Date().toISOString(),
            bot_id: this.config.botId,
            action: "near_miss",
            armed: this.config.armed,
            channel: ctx?.channelId ?? null,
            content_sha256: createHash("sha256").update(content).digest("hex"),
            content_chars: content.length,
        });
    }
    /** Append one row to the per-bot exec-failures ledger (A1's "absorbed ≠
     *  vanished"). Same conventions as OutwardActionLedger: shared-dir path,
     *  date-sharded UTC filename, one-shot mkdir, EACCES warns once. Returns
     *  whether the row actually landed — armed absorption is CONDITIONED on
     *  it (see handleMessageSending), so the caller must know. */
    appendLedger(row) {
        const ledgerDir = path.join(this.config.sharedDir, this.config.botId, "exec-failures");
        if (!this.dirInitialized) {
            try {
                fs.mkdirSync(ledgerDir, { recursive: true });
                this.dirInitialized = true;
            }
            catch (err) {
                if (err?.code === "EACCES" && !this.warnedEACCES) {
                    this.warnedEACCES = true;
                    this.logger.warn(`ExecFailureAbsorber: cannot create ledger dir at ${ledgerDir}; ` +
                        `bot user lacks write on the parent. Run ` +
                        `'sudo evolve-admin deploy <bot>' on the pod host. ` +
                        `(Warning fires once per process.)`);
                }
                else if (err?.code !== "EACCES") {
                    this.logger.debug(`ExecFailureAbsorber: mkdir failed: ${err}`);
                }
                return false;
            }
        }
        const filePath = path.join(ledgerDir, `exec-failures-${new Date().toISOString().slice(0, 10)}.jsonl`);
        try {
            fs.appendFileSync(filePath, JSON.stringify(row) + "\n", { mode: 0o644 });
            return true;
        }
        catch (err) {
            if (err?.code === "ENOENT") {
                // Dir vanished after we cached its creation — recreate next time.
                this.dirInitialized = false;
            }
            if ((err?.code === "EACCES" || err?.code === "EPERM") && !this.warnedEACCES) {
                this.warnedEACCES = true;
                this.logger.warn(`ExecFailureAbsorber: ledger append denied at ${filePath} ` +
                    `(${err?.code}). Run 'sudo evolve-admin deploy <bot>' on the ` +
                    `pod host. (Warning fires once per process.)`);
            }
            else if (err?.code !== "EACCES" && err?.code !== "EPERM") {
                this.logger.debug(`ExecFailureAbsorber: ledger append failed: ${err}`);
            }
            return false;
        }
    }
}
//# sourceMappingURL=ExecFailureAbsorber.js.map