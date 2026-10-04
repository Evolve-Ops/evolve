/**
 * records — the six app records verbs (D-AD2, D-AD3).
 *
 * Design: `internal/design-app-records-layer-2026-09-26.md` §2.1 (the app
 * store) and §2.3 (ledger semantics). Brief: `app-store-and-ledger-verbs`.
 *
 * An app that declares `store:` in its spec gets one SQLite file per instance
 * under the pod's shared dir, written by ONE process: the admin daemon. This
 * tool is the bot's only way in, and it is nothing but a request shaper — one
 * daemon call per verb over the admin-daemon unix socket, no local state, no
 * file access, no fallback. Daemon unreachable ⇒ the tool refuses and NOTHING
 * was written (the board's posture; a fallback would stay inducible by killing
 * the socket).
 *
 * IDENTITY AND AUDIENCE are the daemon's. It binds the calling bot from the
 * socket's peer uid — this file never sends a bot id — and checks that bot's
 * binding to the app instance before it opens the file. A bot whose role on an
 * app is `user` is refused `delete` there; the refusal comes back as a typed
 * `{error, code}` and is shown as-is.
 *
 * CONTEXT ECONOMY (CE-2/CE-3). ONE tool with a verb enum — the five `records.*`
 * verbs plus `append` (contract row `ledger.append`) — and `list` output is
 * compact lines bounded by {@link MAX_LIST_ROWS} / {@link MAX_LIST_CHARS}.
 */
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { Type } from "@sinclair/typebox";
import { appIdOf } from "../apps/appIdentity.js";
import { defaultWorkspaceRoot } from "../integrity/appScriptRegistry.js";
import { AdminSocketUnavailable, adminSocketRequest, } from "../util/adminSocket.js";
const SOCKET_NAME = "admin-daemon.sock";
/** Root of the bot-facing records API. No bot id: identity is the peer uid. */
const RECORDS_BASE = "/api/records-bot";
export const RECORDS_TOOL_VERBS = ["list", "get", "put", "delete", "history", "append"];
/**
 * The contract row each tool verb registers under (app contract v1). The
 * five table verbs are `records.<verb>`; `append` is the ledger's own verb.
 */
export const RECORDS_CONTRACT_VERBS = [
    "list", "get", "put", "delete", "history", "ledger.append",
];
export const MAX_LIST_ROWS = 50;
export const MAX_LIST_CHARS = 4000;
const MAX_VALUE_CHARS = 80;
export const DAEMON_UNREACHABLE_REFUSAL = "App records are unavailable right now — the Evolve admin daemon is not " +
    "reachable, and an app's records can only be read or written through it. " +
    "NOTHING was changed. Say so plainly and try again later; do not keep the " +
    "records anywhere else in the meantime.";
/**
 * The app ids this bot DECLARES that carry a `store:` block (D-AD8: `app` is
 * declared, not discovered). Read from the bot's own manifests; the store block
 * lives on the app's Spec under `{sharedDir}/apps/specs/<id>.json`. Synchronous
 * and total — registration is synchronous, and an unreadable file declares
 * nothing. The daemon re-checks the same declaration on every call; this gate
 * only decides whether the tool is shown at all.
 */
export function declaredStoreApps(sharedDir, botId, workspaceRoot) {
    const root = workspaceRoot?.trim() ? workspaceRoot : defaultWorkspaceRoot(botId);
    const dir = join(root, "manifests");
    let names;
    try {
        names = readdirSync(dir);
    }
    catch {
        return [];
    }
    const out = new Set();
    for (const name of names.sort()) {
        if (!name.endsWith(".json") || name.startsWith(".") || name.startsWith("_"))
            continue;
        try {
            const m = JSON.parse(readFileSync(join(dir, name), "utf8"));
            const prov = (m.provenance ?? {});
            const ids = [appIdOf(m), typeof prov.spec_id === "string" ? prov.spec_id.trim() : ""];
            for (const id of ids) {
                if (!id || id === "unknown" || id.includes("/") || id.includes("..") || id.includes("."))
                    continue;
                const specPath = join(sharedDir, "apps", "specs", `${id}.json`);
                if (!existsSync(specPath))
                    continue;
                const spec = JSON.parse(readFileSync(specPath, "utf8"));
                if (spec.store && typeof spec.store === "object")
                    out.add(id);
            }
        }
        catch { /* unreadable ⇒ declares nothing */ }
    }
    return [...out];
}
export const RecordsParamsSchema = Type.Object({
    verb: Type.Union(RECORDS_TOOL_VERBS.map((v) => Type.Literal(v)), {
        description: "list=rows of a table; get=one row + its derived fields; put=insert or " +
            "replace by key; delete=remove a row; history=a row's changes + events; " +
            "append=record an event in the app's ledger.",
    }),
    app: Type.String({ description: "The app id." }),
    table: Type.Optional(Type.String({ description: "Table name (all verbs but append)." })),
    key: Type.Optional(Type.Unknown({
        description: "get/delete/history: the key value, or {column: value} for a multi-column key.",
    })),
    row: Type.Optional(Type.Record(Type.String(), Type.Unknown(), {
        description: "put: the whole row — every declared column you want kept.",
    })),
    filter: Type.Optional(Type.Record(Type.String(), Type.Unknown(), {
        description: "list: {column: value} or {column: {gte|lte|gt|lt: value}}. Declared columns only.",
    })),
    sort: Type.Optional(Type.String({ description: "list: a column; prefix '-' for descending." })),
    entry: Type.Optional(Type.Object({
        thing_id: Type.String(),
        kind: Type.String({ description: "One of the app's declared kinds." }),
        at: Type.String({ description: "ISO date or datetime." }),
        by: Type.Optional(Type.String()),
        amount: Type.Optional(Type.Number()),
        counterparty: Type.Optional(Type.String()),
        note: Type.Optional(Type.String()),
    }, { description: "append: the event." })),
    instance: Type.Optional(Type.String({
        description: "Only for a pod-scoped app: which instance.",
    })),
});
const RECORDS_DESCRIPTION = [
    "Read and write an installed app's records — the SAME rows the user sees in",
    "Evolve. Never keep an app's records anywhere else. Things live in tables;",
    "what happened to them (bought, sold, lent, returned) is appended to the",
    "app's ledger with verb=append, and `get` derives status/holder from it.",
].join(" ");
function textResult(text, isError = false) {
    return {
        content: [{ type: "text", text }],
        ...(isError ? { isError: true } : {}),
    };
}
function short(value) {
    const s = typeof value === "string" ? value : JSON.stringify(value);
    return s === undefined ? "" : s.length > MAX_VALUE_CHARS
        ? `${s.slice(0, MAX_VALUE_CHARS - 1)}…` : s;
}
function renderRow(row) {
    return Object.entries(row)
        .filter(([, v]) => v !== null && v !== undefined)
        .map(([k, v]) => `${k}=${short(v)}`)
        .join(" · ");
}
/** A `list` response as compact lines, bounded by both caps. */
export function renderRecordsList(payload) {
    const rows = Array.isArray(payload.rows) ? payload.rows : [];
    const total = typeof payload.total === "number" ? payload.total : rows.length;
    if (rows.length === 0)
        return total === 0 ? "No rows." : "No rows matched that filter.";
    const lines = [];
    let chars = 0;
    for (const row of rows) {
        if (lines.length >= MAX_LIST_ROWS)
            break;
        const line = renderRow(row);
        if (chars + line.length + 1 > MAX_LIST_CHARS)
            break;
        lines.push(line);
        chars += line.length + 1;
    }
    const head = `${total} row${total === 1 ? "" : "s"}; showing ${lines.length}.`;
    const tail = lines.length < total
        ? `\n… ${total - lines.length} more not shown — narrow it with filter.` : "";
    return `${head}\n${lines.join("\n")}${tail}`;
}
function renderResult(verb, payload) {
    switch (verb) {
        case "list":
            return renderRecordsList(payload);
        case "get": {
            const row = renderRow((payload.row ?? {}));
            const rollups = renderRow((payload.rollups ?? {}));
            return rollups ? `${row}\nderived: ${rollups}` : row;
        }
        case "put":
            return `put (${String(payload.op ?? "ok")}): ${renderRow((payload.row ?? {}))}`;
        case "delete":
            return `deleted ${short(payload.deleted)}`;
        case "history": {
            const revs = Array.isArray(payload.revisions) ? payload.revisions : [];
            const entries = Array.isArray(payload.entries) ? payload.entries : [];
            const lines = [
                ...revs.slice(-10).map((r) => `${String(r.at)} ${String(r.op)} by ${String(r.by)}`),
                ...entries.slice(-20).map((e) => `${String(e.at)} ${String(e.kind)}${e.counterparty ? ` ↔ ${String(e.counterparty)}` : ""}` +
                    `${typeof e.amount === "number" ? ` ${e.amount}` : ""}${e.note ? ` — ${short(e.note)}` : ""}`),
            ];
            return lines.length ? lines.join("\n") : "No history.";
        }
        case "append": {
            const e = (payload.entry ?? {});
            return `recorded: ${String(e.kind)} for ${String(e.thing_id)} at ${String(e.at)}`;
        }
    }
}
/** The request each verb makes; a string is a local refusal (no daemon call). */
export function buildRecordsRequest(p) {
    const app = (p.app ?? "").trim();
    if (!app)
        return "records: `app` is required.";
    const base = `${RECORDS_BASE}/${encodeURIComponent(app)}`;
    const body = {};
    if (p.instance)
        body.instance = p.instance;
    if (p.verb === "append") {
        if (!p.entry)
            return "records append: `entry` is required ({thing_id, kind, at, …}).";
        return { path: `${base}/ledger/append`, body: { ...body, entry: p.entry } };
    }
    const table = (p.table ?? "").trim();
    if (!table)
        return `records ${p.verb}: \`table\` is required.`;
    body.table = table;
    switch (p.verb) {
        case "list":
            if (p.filter !== undefined)
                body.filter = p.filter;
            if (p.sort)
                body.sort = p.sort;
            body.limit = MAX_LIST_ROWS;
            return { path: `${base}/list`, body };
        case "put":
            if (!p.row)
                return "records put: `row` is required.";
            return { path: `${base}/put`, body: { ...body, row: p.row } };
        case "get":
        case "delete":
        case "history":
            if (p.key === undefined || p.key === null || p.key === "") {
                return `records ${p.verb}: \`key\` is required.`;
            }
            return { path: `${base}/${p.verb}`, body: { ...body, key: p.key } };
        default:
            return `records: unknown verb '${String(p.verb)}'. One of ${RECORDS_TOOL_VERBS.join(", ")}.`;
    }
}
/**
 * Build the `records` tool factory. Every failure returns a NON-throwing
 * envelope: a records fault must never break the turn the user is having.
 */
export function createRecordsToolFactory(config, logger) {
    const socketPath = config.socketPath ?? join(config.sharedDir, SOCKET_NAME);
    const transport = config.transport ?? adminSocketRequest;
    return (_ctx) => ({
        name: "records",
        description: config.declaredApps?.length
            ? `${RECORDS_DESCRIPTION} Apps you can use: ${config.declaredApps.join(", ")}.`
            : RECORDS_DESCRIPTION,
        parameters: RecordsParamsSchema,
        async execute(_toolCallId, rawParams) {
            const params = (rawParams ?? {});
            const req = buildRecordsRequest(params);
            if (typeof req === "string")
                return textResult(req, true);
            try {
                const res = await transport({
                    method: "POST", path: req.path, body: req.body, socketPath,
                });
                const payload = (res.body && typeof res.body === "object"
                    ? res.body : {});
                if (res.status >= 200 && res.status < 300) {
                    return textResult(renderResult(params.verb, payload));
                }
                const code = payload.code ? ` [${String(payload.code)}]` : "";
                const detail = String(payload.error ?? `HTTP ${res.status}`);
                logger.warn(`records ${params.verb} refused (${res.status})${code}: ${detail}`);
                return textResult(`records ${params.verb} refused${code}: ${detail} Nothing was changed.`, true);
            }
            catch (err) {
                if (err instanceof AdminSocketUnavailable) {
                    logger.warn(`records ${params.verb} — admin daemon unavailable: ${err.message}`);
                    return textResult(DAEMON_UNREACHABLE_REFUSAL, true);
                }
                const msg = err instanceof Error ? err.message : String(err);
                logger.error(`records ${params.verb} unexpected error: ${msg}`);
                return textResult(`records ${params.verb} error: ${msg}`, true);
            }
        },
    });
}
//# sourceMappingURL=records.js.map