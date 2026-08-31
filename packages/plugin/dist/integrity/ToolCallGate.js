/**
 * ToolCallGate — Layer-2 per-role tool-loading enforcement (below the LLM).
 *
 * Spec: internal/spec-user-roster-and-roles-2026-06-07.md §8 (Layer 2).
 * Design: internal/design-layer2-tool-loading-filter-2026-07-16.md.
 * Audit: internal/audit-r1a-enforcement-matrix-2026-06-30.md (G-N1 — "the
 *        load-bearing gap": post-admission authorization is not enforced
 *        below the LLM).
 *
 * ── The guarantee ────────────────────────────────────────────────────────────
 * Admission (Layer 1) already fail-closes below the LLM: an un-allowlisted
 * identity never reaches the model. But an ADMITTED speaker — whatever their
 * role — was offered the full tool surface, so a prompt-injection or jailbreak
 * could drive a sensitive tool (roster-mutate, channel-config, outbound Google
 * writes, or the gateway's built-in ``bash``/``exec``/``apply_patch``/file-write)
 * regardless of the LLM's judgment.
 *
 * This gate registers OpenClaw's ``before_tool_call`` hook (fleet OC v2026.6.11)
 * and, PER TOOL CALL, resolves the SPEAKER's role for the current turn and
 * BLOCKS the call when the speaker's capabilities do not permit that tool. The
 * decision runs in-process before the tool executes — a jailbreak cannot invoke
 * a tool the speaker's role forbids, because the block happens below the LLM.
 *
 * ── Fail-CLOSED decision table (for a tool in the capability→tool table) ──────
 *   speaker resolves + has the required capability   → ALLOW  (void)
 *   speaker resolves + lacks the required capability  → DENY
 *   speaker UNRESOLVED (no captured sender / no id)   → DENY   (fail-closed)
 *   any unexpected error while evaluating a gated call → DENY  (fail-closed)
 * A tool NOT in the table is always ALLOWED (documented residual). Read /
 * benign tools (defer, pod_status, list_signals, directory/gmail READ tools)
 * are intentionally ungated.
 *
 * ── Safe rollout: OBSERVE-ONLY by default ────────────────────────────────────
 * A below-LLM blocker can break legitimate tool use on first deploy. The gate
 * ships behind ``config.layer2Enforce`` (network.json ``layer2.enforce``),
 * DEFAULT false:
 *   - observe (default): compute the decision; when it WOULD block, LOG it and
 *     append a record to the per-bot decision ledger, then ALLOW (return void).
 *   - enforce (flag true): actually return ``{ block: true, blockReason }``.
 * The mode is recorded on every entry so the operator can see what enforcement
 * would do before arming it. Merging this code is non-arming.
 *
 * ── Two enforcement domains ──────────────────────────────────────────────────
 *   1. Plugin-registered tools (roster_*, channel_set_newcomer_mode, gmail_send,
 *      calendar_create_event, drive_write_file, …) — named in the table.
 *   2. Gateway BUILT-IN tools (bash/exec/apply_patch/file_write/write/edit) —
 *      these come from the base gateway, not the plugin, and are the real
 *      ``bot.code.modify`` blast radius. Gated here by name + by the
 *      host-authoritative ``toolKind === "code_mode_exec"`` discriminator.
 *
 * Reuses (does NOT reinvent): ``senderRegistry.getSender`` (sender captured in
 * before_agent_run) + ``roleResolver.resolveSpeakerRole`` (fail-closed to
 * participant/[] on any read error). This module holds no role logic of its own.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { getSender } from "../util/senderRegistry.js";
import { resolveSpeakerRole } from "../util/roleResolver.js";
/**
 * DEFAULT-DENY classification (adversarial-review hardening, 2026-07-17).
 *
 * The original gate was a NAME DENYLIST: "gate these N exec names, allow
 * everything else." Over an UNCONTROLLED upstream tool registry that is
 * fail-OPEN by construction — a renamed / aliased / newly-shipped exec tool, an
 * Anthropic-family editor (``str_replace_based_edit_tool``, ``text_editor``,
 * ``shell``, ``run_command``…), or any MCP tool the denylist never heard of
 * sails straight through. The review flagged this as the load-bearing hole.
 *
 * The model is now INVERTED to default-deny:
 *   1. SENSITIVE_CAPABILITY_BY_TOOL — the KNOWN sensitive PLUGIN-native tools,
 *      each pinned to its exact required capability (roster / channel / Google
 *      writes / directory-write / tier-change). These are bare plugin names.
 *   2. KNOWN_SAFE_TOOLS — a POSITIVE allowlist of the plugin's own read/benign
 *      tools. Only a tool on this list is treated as ungated.
 *   3. Everything else → DEFAULT-DENY to ``bot.code.modify`` (the highest
 *      blast-radius capability). Unknown exec tools, unknown MCP tools, future
 *      gateway built-ins, renamed editors — all fail CLOSED without having to
 *      be enumerated. ``toolKind === "code_mode_exec"`` is an ADDITIONAL
 *      positive signal for the same class.
 *
 * Net effect: a participant (``[]``) is blocked from ANYTHING not on the safe
 * allowlist when armed; an admin (``*``) / a role holding ``bot.code.modify``
 * is allowed. The safe allowlist is tuned via the observe-only soak: every
 * unclassified tool that falls through to default-deny is drift-logged (see
 * ``logToolDrift``) so the operator can see which real tools to add.
 */
/**
 * KNOWN sensitive PLUGIN-native tools → the single capability each requires.
 * Bare plugin tool names (never namespaced), enumerated from ``index.ts``
 * registration. Capability ids are the 8 built-ins shared with the Python side
 * (capabilities.py) and the TS role resolver
 * (roleResolver.DEFAULT_ROLE_CAPABILITIES). This table is NOT the whole gated
 * set — anything not here and not in KNOWN_SAFE_TOOLS is default-denied.
 */
export const SENSITIVE_CAPABILITY_BY_TOOL = Object.freeze({
    // ── bot.roster.mutate — roster admin (mirror ENDPOINT_CAPABILITIES) ──
    roster_set_role: "bot.roster.mutate",
    roster_block: "bot.roster.mutate",
    roster_unblock: "bot.roster.mutate",
    // ── bot.channel.config — per-channel newcomer/engagement config ──
    channel_set_newcomer_mode: "bot.channel.config",
    // ── bot.send_external — outbound / mutating Google tools (reads stay safe) ──
    gmail_send: "bot.send_external",
    gmail_label_message: "bot.send_external",
    gmail_archive_message: "bot.send_external",
    gmail_mark_read: "bot.send_external",
    gmail_mark_unread: "bot.send_external",
    gmail_delete_message: "bot.send_external",
    gmail_trash_message: "bot.send_external",
    calendar_create_event: "bot.send_external",
    drive_write_file: "bot.send_external",
    // ── bot.config.modify — persistent per-bot state writes (audit MEDIUM) ──
    // directory_upsert writes the canonical identity/contact directory; it refuses
    // authority fields (role/membership) so it is NOT a roster mutation, but it IS
    // a durable write to bot-scoped state → bot.config.modify (held by admin +
    // primary_user, not participants). session_set_tier changes a user's per-bot
    // model tier — also a durable per-bot config change → bot.config.modify. Both
    // were UNGATED before hardening; the review flagged them as writes.
    directory_upsert: "bot.config.modify",
    session_set_tier: "bot.config.modify",
});
/**
 * POSITIVE allowlist of KNOWN-SAFE read/benign tools. A tool here (after MCP
 * normalization) is treated as ungated. Enumerated from ``index.ts`` — the
 * plugin's own read tools + benign self-report reflexes. Classified read-vs-write
 * honestly: every WRITE / OUTBOUND / MUTATING tool is in the sensitive table or
 * left to default-deny, NEVER here.
 *
 * A tool NOT in this set and NOT in the sensitive table is default-denied to
 * ``bot.code.modify`` — so adding a benign tool here is how you STOP over-gating
 * it, and the observe-only drift log tells you which ones need adding.
 */
export const KNOWN_SAFE_TOOLS = Object.freeze(new Set([
    // Continuity / reflex (benign appends to the bot's own queues)
    "defer",
    "record_application",
    "submit_intake",
    // App capability disclosure (read-only)
    "expand_app",
    // Help surface (read)
    "evolve_help_search",
    "evolve_help_read",
    // Pod-state reads (Bundle 3) — all read-only projections over shared state
    // Consolidated pod-state read tool (overhead-budget B2 v2) — replaces
    // pod_status/list_signals/list_proposals/recent_watchdog/spend_rollup/
    // recent_turns/describe_bot/list_audits. Read-only by construction.
    "pod_state",
    // Directory READ (the WRITE half, directory_upsert, is gated above)
    "directory_lookup",
    // Google READ tools (the WRITE / mutating Google tools are gated above)
    "gmail_list_messages",
    "gmail_get_message",
    "gmail_list_labels",
    "calendar_list_events",
    "drive_list_files",
    "drive_read_file",
    "drive_search",
]));
/**
 * Host-authoritative tool-KIND → capability. OC tags code-execution tools that
 * intentionally share a name with ``toolKind: "code_mode_exec"``. This is an
 * ADDITIONAL positive signal for the code.modify class (default-deny already
 * catches unknown names; this pins the capability precisely when the host tells
 * us the kind, and wins over the safe allowlist — a code_mode_exec tool is
 * NEVER treated as safe even if its bare name happens to collide with one).
 */
export const CAPABILITY_BY_TOOL_KIND = Object.freeze({
    code_mode_exec: "bot.code.modify",
});
/** The capability the default-deny fall-through assigns. */
const DEFAULT_DENY_CAPABILITY = "bot.code.modify";
/**
 * Normalize a possibly host-namespaced tool name to the bare name the tables
 * key on. MCP tools arrive as ``mcp__<server>__<tool>`` (e.g.
 * ``mcp__gmail__send``, ``mcp__x__bash``); other hosts may prefix similarly.
 * Strip the ``mcp__<server>__`` envelope so the bare ``<tool>`` matches the
 * tables. A name that still does not classify falls through to default-deny.
 *
 * NB: only the LEADING ``mcp__<server>__`` segment is stripped (server names do
 * not contain ``__``); the remaining tool name keeps its own underscores
 * (``mcp__gmail__send_message`` → ``send_message``). We do NOT lowercase — the
 * allowlist match stays case-EXACT so a case-variant (``POD_STATUS``) can never
 * widen the safe set; it simply default-denies (fail-closed).
 */
export function normalizeToolName(raw) {
    const name = String(raw ?? "");
    // Strip a single leading mcp__<server>__ prefix (non-greedy up to the 2nd __).
    const m = /^mcp__.+?__(.+)$/.exec(name);
    return m ? m[1] : name;
}
/**
 * Classify a tool call under the default-deny model. Pure + total — never
 * throws, so the handler can classify before any IO and fail-close on a later
 * error. Order is fail-closed-biased: sensitive table → host toolKind →
 * known-safe allowlist → default-deny.
 */
export function classifyTool(toolName, toolKind) {
    const rawName = String(toolName ?? "");
    const normalizedName = normalizeToolName(rawName);
    // 1. KNOWN sensitive plugin tool → its exact capability.
    if (normalizedName &&
        Object.prototype.hasOwnProperty.call(SENSITIVE_CAPABILITY_BY_TOOL, normalizedName)) {
        return {
            normalizedName,
            rawName,
            requiredCapability: SENSITIVE_CAPABILITY_BY_TOOL[normalizedName],
            via: "table",
        };
    }
    // 2. Host-authoritative code-exec kind → bot.code.modify (wins over safe).
    const kind = String(toolKind ?? "");
    if (kind && Object.prototype.hasOwnProperty.call(CAPABILITY_BY_TOOL_KIND, kind)) {
        return {
            normalizedName,
            rawName,
            requiredCapability: CAPABILITY_BY_TOOL_KIND[kind],
            via: "toolKind",
        };
    }
    // 3. KNOWN-SAFE allowlist → ungated.
    if (normalizedName && KNOWN_SAFE_TOOLS.has(normalizedName)) {
        return { normalizedName, rawName, requiredCapability: undefined, via: "safe" };
    }
    // 4. DEFAULT-DENY — anything not positively classified requires the highest
    //    capability. Unknown exec tools, unknown MCP tools, future built-ins,
    //    renamed editors all land here → fail CLOSED.
    return {
        normalizedName,
        rawName,
        requiredCapability: DEFAULT_DENY_CAPABILITY,
        via: "default_deny",
    };
}
/**
 * The capability a call requires, or ``undefined`` when the tool is known-safe.
 * Thin wrapper over ``classifyTool`` (kept for the table-sanity tests and any
 * caller that only needs the capability). Under the default-deny model this
 * returns a capability for EVERY tool that is not on the known-safe allowlist.
 */
export function requiredCapabilityFor(toolName, toolKind) {
    return classifyTool(toolName, toolKind).requiredCapability;
}
function shortRun(runId) {
    return runId ? String(runId).slice(0, 8) : "none";
}
/**
 * Resolve the speaker for ``runId`` and decide whether they may call a tool
 * requiring ``requiredCap``. Reuses the shared sender registry + role resolver;
 * holds no role logic of its own. Never throws in normal operation
 * (resolveSpeakerRole is itself fail-closed); the caller additionally wraps this
 * in a fail-closed try/catch.
 */
export function evaluateGatedCall(config, requiredCap, runId) {
    const sender = getSender(runId);
    if (!sender || !sender.senderId) {
        // UNRESOLVED speaker → treat as no capabilities → deny (fail-closed).
        return {
            allowed: false,
            senderResolved: false,
            role: null,
            resolvedBy: null,
            senderId: null,
            platform: null,
            reason: `Evolve Layer-2 gate: the speaker for this turn could not be identified ` +
                `(runId=${shortRun(runId)}); a tool requiring '${requiredCap}' is blocked ` +
                `(fail-closed).`,
        };
    }
    // Use the sender's REAL platform (captured in before_agent_run) so the overlay
    // resolves the correct (platform, id) key off-Telegram (audit R1a G-N2).
    //
    // Platform fail-CLOSED (audit MEDIUM): captureSender normalizes the channel
    // type to one of the four roster platforms or ``null``. When it did NOT
    // normalize (null), we must NOT fall back to "telegram" — telegram aliases
    // onto the PRIVILEGED telegram id-space, so a null-platform sender whose id
    // happens to collide with a telegram admin/primary id would resolve as
    // admin/primary and be ALLOWED. Instead, deny as participant/[] for a gated
    // call: we identified a sender but cannot safely key their authorization.
    if (!sender.platform) {
        return {
            allowed: false,
            senderResolved: true,
            role: "participant",
            resolvedBy: "platform_unresolved",
            senderId: sender.senderId,
            platform: null,
            reason: `Evolve Layer-2 gate: speaker id=${sender.senderId} has no recognized ` +
                `platform (channel type did not normalize); a tool requiring ` +
                `'${requiredCap}' is blocked (fail-closed — no fallback to the telegram ` +
                `id-space).`,
        };
    }
    const platform = sender.platform;
    const resolution = resolveSpeakerRole(config.botId, platform, sender.senderId, {
        sharedDir: config.sharedDir,
    });
    const hasCap = resolution.capabilities.includes(requiredCap);
    return {
        allowed: hasCap,
        senderResolved: true,
        role: resolution.role,
        resolvedBy: resolution.resolvedBy,
        senderId: sender.senderId,
        platform,
        reason: hasCap
            ? ""
            : `Evolve Layer-2 gate: speaker ${platform}:${sender.senderId} has role ` +
                `'${resolution.role}', which lacks the '${requiredCap}' capability this ` +
                `tool requires — blocked.`,
    };
}
/**
 * Best-effort append of a gate decision to the per-bot decision ledger:
 *
 *   {sharedDir}/{botId}/layer2-gate/decisions-YYYY-MM-DD.jsonl
 *
 * This is the plugin's established "signal path": the plugin writes a per-bot
 * JSONL ledger under sharedDir (the OutwardActionLedger convention) that the
 * evolve-side Python can later read and project into the Signal store for the
 * Alerts page. UTC-dated, append-only, one JSON object per line. Any IO error
 * is swallowed — a telemetry write must never block or crash the tool-call path.
 */
export function writeGateDecisionLedger(config, rec, logger) {
    try {
        const dir = path.join(config.sharedDir, config.botId, "layer2-gate");
        fs.mkdirSync(dir, { recursive: true });
        const day = rec.ts.slice(0, 10); // YYYY-MM-DD (UTC, from toISOString)
        const file = path.join(dir, `decisions-${day}.jsonl`);
        fs.appendFileSync(file, JSON.stringify(rec) + "\n");
    }
    catch (err) {
        try {
            logger.debug(`Evolve Layer-2 gate: ledger append failed (continuing): ${err}`);
        }
        catch {
            /* logging must never throw out of the hot path */
        }
    }
}
// ── Drift logging (default-deny allowlist tuning) ────────────────────────────
// When a tool falls through to DEFAULT-DENY it is unclassified — neither a known
// sensitive tool nor on the safe allowlist. During the observe-only soak the
// operator needs to SEE these so they can add genuinely-benign ones to
// KNOWN_SAFE_TOOLS before arming. Logged once per (bot, tool) to avoid per-turn
// spam, regardless of the eventual allow/deny outcome (an admin-allowed
// unclassified tool is still allowlist-tuning signal).
const _driftSeen = new Set();
function logToolDrift(config, toolName, toolKind, logger) {
    const key = `${config.botId}::${toolName}`;
    if (_driftSeen.has(key))
        return;
    _driftSeen.add(key);
    try {
        logger.warn(`Evolve Layer-2 gate: DRIFT — tool '${toolName}'` +
            `${toolKind ? ` kind='${toolKind}'` : ""} is not on the known-safe ` +
            `allowlist nor in the sensitive capability table; DEFAULT-DENIED to ` +
            `'${DEFAULT_DENY_CAPABILITY}'. If benign, add it to KNOWN_SAFE_TOOLS; if ` +
            `sensitive, map it explicitly. (bot=${config.botId})`);
    }
    catch {
        /* logging must never throw out of the hot path */
    }
}
// ── Liveness latch (armed-but-not-live detection) ────────────────────────────
// Registering the before_tool_call hook does NOT prove it FIRES — an older
// gateway may accept api.on for any event name and silently never invoke it.
// A first-fire latch lets the operator tell "armed & live" from "armed but
// silently dead": the gate handler sets the latch on its first invocation, and
// an independent path that observes REAL tool activity (the tool-result
// middleware, which fires for every tool) cross-checks — if enforcement is
// armed but no before_tool_call has ever fired despite a tool actually running,
// we emit a prominent one-shot ERROR.
let _beforeToolCallEverFired = false;
let _armedButDeadWarned = false;
/** Called at the top of every handler invocation — the positive fire signal. */
export function noteBeforeToolCallFired() {
    _beforeToolCallEverFired = true;
}
/** True once the before_tool_call handler has fired at least once. */
export function beforeToolCallHasFired() {
    return _beforeToolCallEverFired;
}
/**
 * Cross-check for the tool-result middleware to call on observed tool activity.
 * If enforcement is armed for this bot but before_tool_call has NEVER fired,
 * the gate is armed-but-dead (the tool ran without the gate seeing it) — emit a
 * one-shot ERROR. No-op when observe-only or when the gate is provably live.
 */
export function checkGateLivenessOnToolActivity(config, logger) {
    if (!config.layer2Enforce)
        return; // observe-only — nothing armed to be dead
    if (_beforeToolCallEverFired)
        return; // provably live
    if (_armedButDeadWarned)
        return; // one-shot
    _armedButDeadWarned = true;
    try {
        logger.error(`Evolve Layer-2 gate: ARMED BUT NOT LIVE — layer2.enforce is true for ` +
            `${config.botId}, a tool just executed, yet before_tool_call has NEVER ` +
            `fired. Enforcement is silently NOT happening (the gateway may not ` +
            `support before_tool_call — needs OC >= ${OC_MIN_VERSION_FOR_HOOK}). ` +
            `Tools are running UNGATED. Verify the gateway version.`);
    }
    catch {
        /* never throw out of the hot path */
    }
}
/** Test helper — reset the drift + liveness module state between tests. */
export function _resetGateStateForTests() {
    _driftSeen.clear();
    _beforeToolCallEverFired = false;
    _armedButDeadWarned = false;
}
// ── OC version gate for arming (positive liveness pre-check) ──────────────────
// before_tool_call first fires on OC v2026.6.11. If we can POSITIVELY determine
// the gateway is older, we refuse to honor enforce=true (stay observe + log
// ERROR) rather than run armed-but-dead. When the version can't be discovered
// we do NOT refuse on that basis alone (can't prove it's too old) — the
// first-fire latch above is the runtime backstop for that case.
export const OC_MIN_VERSION_FOR_HOOK = "2026.6.11";
/** Parse a dotted numeric version ("2026.6.11") into a comparable tuple. */
function parseVersion(v) {
    const m = /(\d+)\.(\d+)\.(\d+)/.exec(String(v ?? ""));
    if (!m)
        return null;
    return [Number(m[1]), Number(m[2]), Number(m[3])];
}
/** a < b ? (both parsed tuples). */
function versionLt(a, b) {
    for (let i = 0; i < 3; i++) {
        if ((a[i] ?? 0) !== (b[i] ?? 0))
            return (a[i] ?? 0) < (b[i] ?? 0);
    }
    return false;
}
/**
 * Best-effort discovery of the running gateway's OC version. There is no typed
 * accessor (the SDK types the gateway config as an opaque bag), so we probe the
 * few places a version realistically surfaces. Returns the raw version string
 * or ``null`` when it can't be determined.
 */
export function detectOcVersion(api) {
    const candidates = [
        api?.openclawVersion,
        api?.version,
        api?.config?.openclawVersion,
        api?.config?.version,
        api?.config?.gatewayVersion,
        api?.config?.ocVersion,
        typeof process !== "undefined" ? process.env?.OPENCLAW_VERSION : undefined,
        typeof process !== "undefined" ? process.env?.OC_VERSION : undefined,
    ];
    for (const c of candidates) {
        if (typeof c === "string" && parseVersion(c))
            return c;
    }
    return null;
}
/**
 * Decide whether it is SAFE to honor enforce=true given the discovered gateway
 * version. Returns true (honor) when the version is unknown OR >= the min; false
 * (refuse, stay observe) when it is KNOWN and below the min. Logs an ERROR on a
 * refusal so the operator sees why arming was downgraded.
 */
export function enforceAllowedByVersion(api, config, logger) {
    const raw = detectOcVersion(api);
    if (!raw)
        return true; // undiscoverable → don't refuse on version alone
    const got = parseVersion(raw);
    const min = parseVersion(OC_MIN_VERSION_FOR_HOOK);
    if (got && versionLt(got, min)) {
        logger.error(`Evolve Layer-2 gate: REFUSING to arm — gateway OC version ${raw} is below ` +
            `${OC_MIN_VERSION_FOR_HOOK}, which first fires before_tool_call. Staying ` +
            `OBSERVE-ONLY so enforcement is not silently dead. Upgrade the gateway ` +
            `to arm. (bot=${config.botId})`);
        return false;
    }
    return true;
}
/**
 * Build the ``before_tool_call`` handler. Exported separately from registration
 * so tests can drive it directly with synthetic events. Returns:
 *   - ``void``               → allow the call unchanged (ungated, or authorized,
 *                              or observe-mode would-block)
 *   - ``{ block: true, … }`` → veto the call (enforce-mode deny / error)
 */
export function makeBeforeToolCallHandler(config, logger) {
    return (event, ctx) => {
        // Positive liveness signal: the hook FIRED. Recorded first so the
        // armed-but-dead cross-check (checkGateLivenessOnToolActivity) can tell
        // "armed & live" from "armed but silently not enforcing."
        noteBeforeToolCallFired();
        // ``requiredCap`` is hoisted so the catch below knows whether the call was
        // gated (fail-CLOSED) or ungated (allow) if evaluation throws.
        let requiredCap;
        let toolName = "";
        let toolKind = null;
        let runId = null;
        try {
            toolName = String(event?.toolName ?? ctx?.toolName ?? "");
            toolKind = (event?.toolKind ?? ctx?.toolKind ?? null);
            runId = (event?.runId ?? ctx?.runId ?? null);
            const classification = classifyTool(toolName, toolKind);
            requiredCap = classification.requiredCapability;
            // Drift: surface any tool that only classified via default-deny, so the
            // observe-only soak reveals which real tools need the safe allowlist —
            // regardless of the eventual allow/deny outcome.
            if (classification.via === "default_deny") {
                logToolDrift(config, toolName, toolKind, logger);
            }
            if (!requiredCap)
                return; // known-safe tool → allow (void)
            const evaluation = evaluateGatedCall(config, requiredCap, runId);
            if (evaluation.allowed)
                return; // authorized → allow (void)
            // ── would-block ─────────────────────────────────────────────────────────
            const enforcing = config.layer2Enforce;
            const mode = enforcing ? "enforce" : "observe";
            const action = enforcing ? "block" : "would_block";
            writeGateDecisionLedger(config, {
                ts: new Date().toISOString(),
                bot_id: config.botId,
                mode,
                action,
                tool_name: toolName,
                tool_kind: toolKind,
                required_capability: requiredCap,
                role: evaluation.role,
                resolved_by: evaluation.resolvedBy,
                sender_id: evaluation.senderId,
                platform: evaluation.platform,
                sender_resolved: evaluation.senderResolved,
                run_id: runId,
                reason: evaluation.reason,
            }, logger);
            logger.warn(`Evolve Layer-2 gate [${mode}] ${enforcing ? "BLOCKED" : "would block"} ` +
                `tool='${toolName}'${toolKind ? ` kind='${toolKind}'` : ""} ` +
                `cap='${requiredCap}' ` +
                `speaker=${evaluation.senderResolved
                    ? `${evaluation.platform}:${evaluation.senderId} role='${evaluation.role}'`
                    : `UNRESOLVED(runId=${shortRun(runId)})`} ` +
                `bot=${config.botId}`);
            if (enforcing) {
                return { block: true, blockReason: evaluation.reason };
            }
            // observe-only default: recorded, but allow.
            return;
        }
        catch (err) {
            // Unexpected handler error. Fail CLOSED for a call we KNOW is gated; for an
            // ungated / unclassified call, allow (we must never block a call we could
            // not establish is gated).
            if (requiredCap) {
                const enforcing = config.layer2Enforce;
                try {
                    logger.error(`Evolve Layer-2 gate: internal error evaluating gated tool ` +
                        `'${toolName}' (cap='${requiredCap}') — ${enforcing ? "BLOCKING" : "would block"} ` +
                        `(fail-closed): ${err}`);
                    writeGateDecisionLedger(config, {
                        ts: new Date().toISOString(),
                        bot_id: config.botId,
                        mode: enforcing ? "enforce" : "observe",
                        action: "error_block",
                        tool_name: toolName,
                        tool_kind: toolKind,
                        required_capability: requiredCap,
                        role: null,
                        resolved_by: null,
                        sender_id: null,
                        platform: null,
                        sender_resolved: false,
                        run_id: runId,
                        reason: `Evolve Layer-2 gate: internal error resolving authorization (fail-closed): ${err}`,
                    }, logger);
                }
                catch {
                    /* never throw out of the fail-closed path */
                }
                if (enforcing) {
                    return {
                        block: true,
                        blockReason: "Evolve Layer-2 gate: could not resolve authorization for this tool; " +
                            "blocked (fail-closed).",
                    };
                }
                return; // observe-only: recorded, allow.
            }
            return; // ungated / unclassified → allow.
        }
    };
}
/**
 * Register the Layer-2 ``before_tool_call`` gate on the OpenClaw plugin api.
 * Registered the SAME way TurnObserver registers before_agent_run: ``api.on``.
 * No-op (with an info log) on a gateway too old to expose the hook, so the
 * plugin keeps loading on OC < v2026.6.11 — the gate simply stays inactive.
 */
export function registerToolCallGate(api, config, logger) {
    // LOUD surfacing of a non-boolean enforce value (fail-safe: stays observe).
    if (config.layer2EnforceWarning) {
        logger.warn(config.layer2EnforceWarning);
    }
    if (typeof api.on !== "function") {
        logger.info(`Evolve Layer-2 gate: gateway api.on unavailable — gate inactive for ${config.botId}`);
        return;
    }
    // Positive liveness PRE-CHECK: refuse to honor enforce=true on a gateway we
    // can prove is too old to fire before_tool_call (stay observe + log ERROR).
    // Undiscoverable version → don't refuse (the runtime first-fire latch backs
    // that case). Downgrade by building the handler with enforce forced false.
    const versionSafe = !config.layer2Enforce || enforceAllowedByVersion(api, config, logger);
    const effectiveConfig = config.layer2Enforce && !versionSafe
        ? { ...config, layer2Enforce: false }
        : config;
    const handler = makeBeforeToolCallHandler(effectiveConfig, logger);
    try {
        api.on("before_tool_call", async (event, ctx) => handler(event, ctx), { name: "evolve-before-tool-call-gate" });
        logger.info(`Evolve Layer-2 gate: before_tool_call registered for ${config.botId} ` +
            `(mode=${effectiveConfig.layer2Enforce ? "ENFORCE (armed)" : "observe-only"}; ` +
            `model=default-deny; ` +
            `${Object.keys(SENSITIVE_CAPABILITY_BY_TOOL).length} sensitive tools mapped, ` +
            `${KNOWN_SAFE_TOOLS.size} known-safe, all others default-denied to ` +
            `'${DEFAULT_DENY_CAPABILITY}')`);
    }
    catch (err) {
        logger.info(`Evolve Layer-2 gate: before_tool_call not supported by this gateway ` +
            `version — gate inactive for ${config.botId} (${err})`);
    }
}
//# sourceMappingURL=ToolCallGate.js.map