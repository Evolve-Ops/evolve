/**
 * RecordApplicationTool — Manifest reflex.
 *
 * Bots call record_application() in the same turn they ship something
 * app-shaped (a script, a cron, a tracker). Without it the application
 * registry drifts from on-disk reality and the nightly scanner has to
 * reconcile after the fact.
 *
 * The tool appends a JSONL row to /Users/<bot_id>/.openclaw/workspace/evolve/
 * manifest-reflex-queue.jsonl. The bot has natural write access there as
 * itself; the manifest_reflex_runner (running as root → admin user) picks
 * up rows on its cycle and lands them as real ApplicationManifests under
 * {shared_dir}/applications/{bot_id}/.
 *
 * Append uses POSIX O_APPEND, which guarantees atomicity for writes
 * ≤ PIPE_BUF (4096 bytes). Rows are well under that — no flock needed for
 * the append path. The runner takes a flock when it rewrites the queue
 * to drop applied rows, mirroring the defer_queue contract.
 *
 * Schema is mirrored verbatim in packages/analyzer/manifest_reflex_queue.py
 * (ReflexRow). If you change one, change both.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import * as crypto from "node:crypto";
import { Type } from "@sinclair/typebox";
const SCHEMA_VERSION = 1;
const STATUS_PENDING = "pending";
// app_id slug rules: lowercase alphanumeric + hyphens, 3-48 chars.
// Matches the implicit convention scanner.py and forge use for manifest filenames.
const APP_ID_PATTERN = /^[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$/;
export const RecordApplicationToolParamsSchema = Type.Object({
    app_id: Type.String({
        description: "Slug for this application (lowercase alphanumeric + hyphens, " +
            "3–48 chars, e.g. 'protein-tracker'). For an existing app, pass the " +
            "same id and set update=true.",
    }),
    name: Type.Optional(Type.String({
        description: "Human-readable name (e.g. 'Protein Tracker'). Defaults to a " +
            "title-cased app_id if omitted.",
    })),
    purpose: Type.Optional(Type.String({
        description: "1–2 sentences on what this app does and for whom. The forge " +
            "uses this verbatim in dashboards; write it for an operator " +
            "reading the registry six months from now.",
    })),
    files: Type.Optional(Type.Array(Type.String(), {
        description: "All scripts, data files, and assets that belong to this app. " +
            "Paths may be workspace-relative (e.g. 'workspace/ops/tools/" +
            "protein.py') or absolute. Crons that run a script should ALSO " +
            "list that script here.",
    })),
    crons: Type.Optional(Type.Array(Type.Object({
        schedule: Type.String({
            description: "Cron schedule string ('0 21 * * *') or @-keyword ('@daily').",
        }),
        script: Type.String({
            description: "Path to the script the cron runs. Should also appear in `files`.",
        }),
    }), {
        description: "Cron entries the bot installed for this app. List them here even " +
            "if they're already in your crontab — the runner reconciles.",
    })),
    inputs: Type.Optional(Type.Array(Type.String(), {
        description: "What the app consumes (e.g. 'user voice messages', 'tasks.json'). " +
            "Free-form short phrases.",
    })),
    outputs: Type.Optional(Type.Array(Type.String(), {
        description: "What the app produces (e.g. 'daily summary message', 'log entry'). " +
            "Free-form short phrases.",
    })),
    test_command: Type.Optional(Type.String({
        description: "Optional shell command to run the app's test suite from the " +
            "workspace root. Leave empty if there's nothing to run yet — " +
            "forge can backfill testgen later.",
    })),
    update: Type.Optional(Type.Boolean({
        description: "Set true when amending an existing manifest with the same " +
            "app_id (e.g. you added a new file). The runner merges into " +
            "the existing manifest instead of replacing it. Default false.",
    })),
});
const DESCRIPTION = [
    "Record an application manifest for something durable you just built.",
    "",
    "Call this in the SAME turn you write a persistent script, install a",
    "cron, or create a tracker file the user will rely on across sessions.",
    "Without it, the pod's application registry drifts and your work won't",
    "be tested, monitored, or surfaced in the gallery.",
    "",
    "When NOT to call: one-off scratch output, draft text, or files in /tmp.",
    "Edits inside an already-manifested app's files use update=true with the",
    "existing app_id, not a new manifest.",
    "",
    "Returns a reflex_id; the manifest lands in {shared_dir}/applications/",
    "{bot_id}/{app_id}.json on the next runner cycle (≤ 60s).",
].join("\n");
function botEvolveDir(botId) {
    // Mirrors defer_queue.bot_evolve_dir — using the same env override means
    // a single setting redirects every bot-side queue in tests.
    const override = process.env.EVOLVE_DEFER_HOME_OVERRIDE;
    if (override)
        return path.join(override, botId);
    return `/Users/${botId}/.openclaw/workspace/evolve`;
}
function queuePath(botId) {
    return path.join(botEvolveDir(botId), "manifest-reflex-queue.jsonl");
}
function nowIsoSeconds() {
    return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
}
function titleCase(slug) {
    return slug
        .split("-")
        .map((s) => (s ? s[0].toUpperCase() + s.slice(1) : s))
        .join(" ");
}
function validateParams(params) {
    const { app_id, files, crons } = params;
    if (typeof app_id !== "string" || !APP_ID_PATTERN.test(app_id)) {
        return {
            ok: false,
            error: `'app_id' must be lowercase alphanumeric + hyphens, 3–48 chars, ` +
                `starting and ending with an alphanumeric — got ${JSON.stringify(app_id)}.`,
        };
    }
    if (files !== undefined) {
        if (!Array.isArray(files))
            return { ok: false, error: "'files' must be an array of strings." };
        for (const f of files) {
            if (typeof f !== "string" || f.trim().length === 0) {
                return { ok: false, error: "'files' entries must be non-empty strings." };
            }
        }
    }
    if (crons !== undefined) {
        if (!Array.isArray(crons))
            return { ok: false, error: "'crons' must be an array." };
        for (const c of crons) {
            if (!c || typeof c !== "object" ||
                typeof c.schedule !== "string" || c.schedule.trim() === "" ||
                typeof c.script !== "string" || c.script.trim() === "") {
                return {
                    ok: false,
                    error: "'crons' entries must be {schedule, script} with both non-empty strings.",
                };
            }
        }
    }
    return { ok: true };
}
function buildRow(params, ctx) {
    return {
        reflex_id: crypto.randomUUID().replace(/-/g, ""),
        bot_id: ctx.botId,
        created_at: nowIsoSeconds(),
        app_id: params.app_id,
        name: (params.name && params.name.trim()) || titleCase(params.app_id),
        purpose: (params.purpose ?? "").trim(),
        session_id: ctx.sessionId,
        session_key: ctx.sessionKey,
        files: (params.files ?? []).map((f) => f.trim()).filter((f) => f.length > 0),
        crons: (params.crons ?? []).map((c) => ({
            schedule: c.schedule.trim(),
            script: c.script.trim(),
        })),
        inputs: (params.inputs ?? []).map((s) => s.trim()).filter((s) => s.length > 0),
        outputs: (params.outputs ?? []).map((s) => s.trim()).filter((s) => s.length > 0),
        test_command: (params.test_command ?? "").trim(),
        update: params.update === true,
        status: STATUS_PENDING,
        applied_at: null,
        result: null,
        schema_version: SCHEMA_VERSION,
    };
}
function appendRow(row) {
    const dir = botEvolveDir(row.bot_id);
    fs.mkdirSync(dir, { recursive: true });
    const file = queuePath(row.bot_id);
    fs.appendFileSync(file, JSON.stringify(row) + "\n", { flag: "a" });
    ensureQueueFileGroupWritable(file);
}
// Group-rw (0660) so the evolve manifest-reflex-runner — a separate, non-root
// service user — can read + rewrite this bot-owned queue. On Linux a file
// created at the bot's umask (0600) zeroes the POSIX-ACL mask, capping the
// inherited u:evolve ACE to nothing → the runner hit EACCES on the round-6 live
// fresh install (W10-G). 0660 keeps the mask wide enough for the cross-user ACE.
// Best-effort: a no-op once the runner owns the file (after a rewrite).
function ensureQueueFileGroupWritable(file) {
    try {
        fs.chmodSync(file, 0o660);
    }
    catch {
        /* not the owner — the owner-side write path sets the mode */
    }
}
/**
 * Build the record_application tool definition. Same factory shape as
 * createDeferToolFactory: the gateway passes a per-call ctx with sessionId
 * / sessionKey / etc, and we bake in config.botId at registration time.
 */
export function createRecordApplicationToolFactory(config, logger) {
    return (ctx) => {
        return {
            name: "record_application",
            description: DESCRIPTION,
            parameters: RecordApplicationToolParamsSchema,
            async execute(_toolCallId, rawParams) {
                const params = rawParams;
                const v = validateParams(params);
                if (!v.ok) {
                    return {
                        content: [{ type: "text", text: `record_application: ${v.error}` }],
                        isError: true,
                    };
                }
                if (!config.botId || config.botId === "unknown") {
                    logger.warn(`record_application: config.botId looks unset (${JSON.stringify(config.botId)}) — ` +
                        "queue path will be wrong. Check plugin pluginConfig.botId at registration.");
                }
                const row = buildRow(params, {
                    botId: config.botId,
                    sessionId: ctx.sessionId ?? null,
                    sessionKey: ctx.sessionKey ?? null,
                });
                try {
                    appendRow(row);
                }
                catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    logger.warn(`record_application: failed to enqueue row: ${msg}`);
                    return {
                        content: [
                            { type: "text", text: `record_application: failed to enqueue — ${msg}` },
                        ],
                        isError: true,
                    };
                }
                logger.info(`record_application: queued ${row.reflex_id.slice(0, 8)} app=${row.app_id} ` +
                    `update=${row.update} files=${row.files.length} crons=${row.crons.length}`);
                return {
                    content: [
                        {
                            type: "text",
                            text: JSON.stringify({
                                reflex_id: row.reflex_id,
                                app_id: row.app_id,
                                update: row.update,
                            }),
                        },
                    ],
                };
            },
        };
    };
}
//# sourceMappingURL=RecordApplicationTool.js.map