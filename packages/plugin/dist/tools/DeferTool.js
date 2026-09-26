/**
 * DeferTool — Continuity Engine v2.
 *
 * Bots call defer() when they commit to acting later (after a delay, when an
 * event happens, on a schedule). Without it the bot has no persistence between
 * turns and the promise is silently lost — which is the failure mode v1 of CE
 * tried to detect after the fact and v2 just lets the bot prevent.
 *
 * The tool appends a JSONL row to /Users/<bot_id>/.openclaw/workspace/evolve/
 * defer-queue.jsonl. The bot has natural write access there as itself; the
 * defer_runner (running as the evolve user) picks it up via the read+write ACL
 * grant on workspace/evolve/ established by deploy.py's set_evolve_read_acl().
 *
 * Append uses POSIX O_APPEND, which guarantees atomicity for writes ≤ PIPE_BUF
 * (4096 bytes) — well above any defer row. No flock needed for the append path
 * at our throughput; the runner takes a flock when it rewrites the queue file
 * to drop fired rows, which is safe because rewrite is via tempfile + rename.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import * as crypto from "node:crypto";
import { Type } from "@sinclair/typebox";
const SCHEMA_VERSION = 1;
const MODE_MESSAGE = "message";
const MODE_ACTION = "action";
const STATUS_PENDING = "pending";
export const DeferToolParamsSchema = Type.Object({
    due_at: Type.String({
        description: "When to fire, as an ISO 8601 UTC timestamp (e.g. '2026-05-05T13:04:00Z'). " +
            "You will be told the current time in your system context — compute the " +
            "absolute target time yourself rather than using relative phrasing.",
    }),
    message: Type.Optional(Type.String({
        description: "Literal text to deliver to the user when fired. Use this when you " +
            "already know exactly what to say (e.g. 'My favorite color is blue'). " +
            "Mutually exclusive with 'action'.",
    })),
    action: Type.Optional(Type.String({
        description: "Instruction for a follow-up turn that needs work later (e.g. 'check " +
            "build status, summarize result'). The agent will run this when fired " +
            "and reply to the user. Mutually exclusive with 'message'.",
    })),
});
const DESCRIPTION = [
    "Schedule a future message to send to the user.",
    "",
    "You have NO persistence between turns and cannot wait, sleep, or run",
    "background work. If you commit to acting later — after a delay, after an",
    "event, on a schedule — you MUST call this tool to schedule the follow-up.",
    "Without it, your commitment will silently fail.",
    "",
    "Use 'message' for follow-ups whose content you already know (e.g. 'tell me",
    "your favorite color in 20 minutes' → schedule a literal answer).",
    "Use 'action' for follow-ups that need work later (e.g. 'let me know when",
    "the build finishes' → schedule a check-and-respond).",
    "",
    "Returns a defer_id and the absolute fires_at timestamp.",
].join("\n");
function botEvolveDir(botId) {
    const override = process.env.EVOLVE_DEFER_HOME_OVERRIDE;
    if (override)
        return path.join(override, botId);
    return `/Users/${botId}/.openclaw/workspace/evolve`;
}
function queuePath(botId) {
    return path.join(botEvolveDir(botId), "defer-queue.jsonl");
}
function nowIsoSeconds() {
    // ISO 8601 with seconds precision, UTC. Matches defer_queue.py output.
    return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
}
function parseIsoUtc(value) {
    // Accept both 'Z' and explicit +HH:MM offsets.
    const d = new Date(value);
    return Number.isFinite(d.getTime()) ? d : null;
}
function validateParams(params) {
    const { due_at, message, action } = params;
    if (!message && !action) {
        return { ok: false, error: "Provide exactly one of 'message' or 'action'." };
    }
    if (message && action) {
        return { ok: false, error: "'message' and 'action' are mutually exclusive — provide one, not both." };
    }
    if (message !== undefined && message.trim().length === 0) {
        return { ok: false, error: "'message' must be non-empty when provided." };
    }
    if (action !== undefined && action.trim().length === 0) {
        return { ok: false, error: "'action' must be non-empty when provided." };
    }
    const target = parseIsoUtc(due_at);
    if (!target) {
        return { ok: false, error: `'due_at' is not a parseable ISO 8601 timestamp: ${due_at}` };
    }
    const now = Date.now();
    // Allow up to 5s in the past — the bot computes due_at from "current time
    // is X" in the system prompt and a tiny clock drift shouldn't be fatal.
    // Anything more than 5s past is almost certainly a model error (e.g. the
    // bot reused yesterday's date).
    const MAX_PAST_MS = 5 * 1000;
    if (target.getTime() < now - MAX_PAST_MS) {
        return {
            ok: false,
            error: `'due_at' (${due_at}) is in the past — defer needs a future time. ` +
                `If you meant 'right now', set due_at to a few seconds from now.`,
        };
    }
    // Sanity cap: 90 days. Longer-horizon recurrence belongs in a different
    // mechanism; this prevents accidental year-out timestamps.
    const MAX_FUTURE_MS = 90 * 24 * 60 * 60 * 1000;
    if (target.getTime() - now > MAX_FUTURE_MS) {
        return { ok: false, error: "'due_at' must be within 90 days." };
    }
    // Clamp tiny past values forward so the runner sees a future or now-ish
    // timestamp. Keeps the row well-formed even when the bot's arithmetic is
    // off by a few hundred ms.
    const fires_at = new Date(Math.max(target.getTime(), now));
    return { ok: true, fires_at_iso: fires_at.toISOString().replace(/\.\d{3}Z$/, "+00:00") };
}
function buildRow(params, fires_at_iso, ctx) {
    const mode = params.message ? MODE_MESSAGE : MODE_ACTION;
    return {
        defer_id: crypto.randomUUID().replace(/-/g, ""),
        bot_id: ctx.botId,
        channel_id: ctx.channelId,
        session_id: ctx.sessionId,
        session_key: ctx.sessionKey,
        fires_at: fires_at_iso,
        created_at: nowIsoSeconds(),
        mode,
        message: mode === MODE_MESSAGE ? params.message ?? null : null,
        action: mode === MODE_ACTION ? params.action ?? null : null,
        status: STATUS_PENDING,
        fired_at: null,
        result: null,
        schema_version: SCHEMA_VERSION,
    };
}
function appendRow(row) {
    const dir = botEvolveDir(row.bot_id);
    fs.mkdirSync(dir, { recursive: true });
    const file = queuePath(row.bot_id);
    // O_APPEND is atomic for writes up to PIPE_BUF on POSIX. Our row is well
    // under 4096 bytes; no flock needed for the append path.
    fs.appendFileSync(file, JSON.stringify(row) + "\n", { flag: "a" });
    ensureQueueFileGroupWritable(file);
}
// Group-rw (0660) so the evolve defer-runner — a separate, non-root service
// user — can read + rewrite this bot-owned queue. On Linux a file created at
// the bot's umask (0600) zeroes the POSIX-ACL mask, which caps the inherited
// u:evolve ACE to nothing → the runner hit EACCES on the round-6 live fresh
// install (W10-G). 0660 keeps the mask wide enough for the cross-user ACE.
// Best-effort: a no-op on a file the runner already took ownership of (a
// rewrite makes it evolve-owned) and on platforms where chmod is irrelevant.
function ensureQueueFileGroupWritable(file) {
    try {
        fs.chmodSync(file, 0o660);
    }
    catch {
        /* not the owner — the owner-side write path sets the mode */
    }
}
/**
 * Build the defer tool definition. Returned as an SDK tool factory: the
 * gateway calls the factory per-tool-call with a trusted context (sessionKey,
 * channelId, agentId) we can use to bind the row to its origin.
 *
 * Pass this directly to api.registerTool().
 */
export function createDeferToolFactory(config, logger) {
    return (ctx) => {
        // Diag at factory invocation — confirms the gateway is calling the
        // factory per-tool-call (or per-session) and shows what context
        // fields are populated. Useful for the first-time integration test.
        logger.debug(`defer: factory invoked agentId=${ctx.agentId ?? "?"} ` +
            `sessionId=${ctx.sessionId ?? "?"} ` +
            `sessionKey=${ctx.sessionKey ?? "?"} ` +
            `channel=${ctx.messageChannel ?? "?"}`);
        return {
            name: "defer",
            description: DESCRIPTION,
            parameters: DeferToolParamsSchema,
            async execute(_toolCallId, rawParams) {
                const params = rawParams;
                const v = validateParams(params);
                if (!v.ok) {
                    return {
                        content: [{ type: "text", text: `defer: ${v.error}` }],
                        isError: true,
                    };
                }
                // The runner uses ctx.sessionId (UUID) for `openclaw agent
                // --session-id`. ctx.sessionKey is the routing-style id and is
                // diagnostic-only — the CLI rejects that format. Warn loudly if
                // sessionId is missing, since without it the runner cannot dispatch.
                if (!ctx.sessionId) {
                    logger.warn("defer: ctx.sessionId (UUID) is undefined — runner will be unable " +
                        "to dispatch this row. The CLI's --session-id wants the ephemeral " +
                        "session UUID, not the routing-style sessionKey.");
                }
                // Always use config.botId (the OS user / bot identity established at
                // plugin registration time). NOT ctx.agentId — that's the OC routing
                // identity for multi-agent setups (typically "main"), and using it
                // would map the queue path to /Users/main/.openclaw/... which doesn't
                // exist. This was a real bug caught on first integration test.
                if (!config.botId || config.botId === "unknown") {
                    logger.warn(`defer: config.botId looks unset (${JSON.stringify(config.botId)}) — queue ` +
                        "path will be wrong. Check plugin pluginConfig.botId at registration.");
                }
                const row = buildRow(params, v.fires_at_iso, {
                    botId: config.botId,
                    channelId: ctx.messageChannel ?? null,
                    sessionId: ctx.sessionId ?? null,
                    sessionKey: ctx.sessionKey ?? null,
                });
                try {
                    appendRow(row);
                }
                catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    logger.warn(`defer: failed to append row to queue: ${msg}`);
                    return {
                        content: [{ type: "text", text: `defer: failed to enqueue — ${msg}` }],
                        isError: true,
                    };
                }
                logger.info(`defer: queued ${row.defer_id.slice(0, 8)} mode=${row.mode} ` +
                    `fires_at=${row.fires_at} session_id=${row.session_id ?? "<none>"}`);
                return {
                    content: [
                        {
                            type: "text",
                            text: JSON.stringify({
                                defer_id: row.defer_id,
                                fires_at: row.fires_at,
                                mode: row.mode,
                            }),
                        },
                    ],
                };
            },
        };
    };
}
//# sourceMappingURL=DeferTool.js.map