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
import { Static } from "@sinclair/typebox";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
import { AdminSocketRequest, AdminSocketResponse } from "../util/adminSocket.js";
export declare const RECORDS_TOOL_VERBS: readonly ["list", "get", "put", "delete", "history", "append"];
export type RecordsVerb = (typeof RECORDS_TOOL_VERBS)[number];
/**
 * The contract row each tool verb registers under (app contract v1). The
 * five table verbs are `records.<verb>`; `append` is the ledger's own verb.
 */
export declare const RECORDS_CONTRACT_VERBS: readonly ["list", "get", "put", "delete", "history", "ledger.append"];
export declare const MAX_LIST_ROWS = 50;
export declare const MAX_LIST_CHARS = 4000;
export declare const DAEMON_UNREACHABLE_REFUSAL: string;
export type RecordsTransport = (req: AdminSocketRequest) => Promise<AdminSocketResponse>;
/**
 * The app ids this bot DECLARES that carry a `store:` block (D-AD8: `app` is
 * declared, not discovered). Read from the bot's own manifests; the store block
 * lives on the app's Spec under `{sharedDir}/apps/specs/<id>.json`. Synchronous
 * and total — registration is synchronous, and an unreadable file declares
 * nothing. The daemon re-checks the same declaration on every call; this gate
 * only decides whether the tool is shown at all.
 */
export declare function declaredStoreApps(sharedDir: string, botId: string, workspaceRoot?: string): string[];
export interface RecordsToolConfig {
    readonly sharedDir: string;
    /** Diagnostics only; identity is bound server-side. */
    readonly botId: string;
    readonly socketPath?: string;
    readonly transport?: RecordsTransport;
    /** The declared app ids, listed in the tool description. */
    readonly declaredApps?: readonly string[];
}
export declare const RecordsParamsSchema: import("@sinclair/typebox").TObject<{
    verb: import("@sinclair/typebox").TUnion<import("@sinclair/typebox").TLiteral<"list" | "get" | "put" | "delete" | "history" | "append">[]>;
    app: import("@sinclair/typebox").TString;
    table: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    key: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnknown>;
    row: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TRecord<import("@sinclair/typebox").TString, import("@sinclair/typebox").TUnknown>>;
    filter: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TRecord<import("@sinclair/typebox").TString, import("@sinclair/typebox").TUnknown>>;
    sort: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    entry: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TObject<{
        thing_id: import("@sinclair/typebox").TString;
        kind: import("@sinclair/typebox").TString;
        at: import("@sinclair/typebox").TString;
        by: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        amount: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TNumber>;
        counterparty: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        note: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    }>>;
    instance: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
}>;
export type RecordsParams = Static<typeof RecordsParamsSchema>;
/** A `list` response as compact lines, bounded by both caps. */
export declare function renderRecordsList(payload: Record<string, unknown>): string;
/** The request each verb makes; a string is a local refusal (no daemon call). */
export declare function buildRecordsRequest(p: RecordsParams): {
    path: string;
    body: Record<string, unknown>;
} | string;
/**
 * Build the `records` tool factory. Every failure returns a NON-throwing
 * envelope: a records fault must never break the turn the user is having.
 */
export declare function createRecordsToolFactory(config: RecordsToolConfig, logger: PluginLogger): (_ctx: Record<string, unknown>) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        verb: import("@sinclair/typebox").TUnion<import("@sinclair/typebox").TLiteral<"list" | "get" | "put" | "delete" | "history" | "append">[]>;
        app: import("@sinclair/typebox").TString;
        table: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        key: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnknown>;
        row: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TRecord<import("@sinclair/typebox").TString, import("@sinclair/typebox").TUnknown>>;
        filter: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TRecord<import("@sinclair/typebox").TString, import("@sinclair/typebox").TUnknown>>;
        sort: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        entry: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TObject<{
            thing_id: import("@sinclair/typebox").TString;
            kind: import("@sinclair/typebox").TString;
            at: import("@sinclair/typebox").TString;
            by: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
            amount: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TNumber>;
            counterparty: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
            note: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        }>>;
        instance: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        isError?: boolean | undefined;
        content: {
            type: "text";
            text: string;
        }[];
    }>;
};
//# sourceMappingURL=records.d.ts.map