/**
 * DirectoryTools — the bot-facing directory tools: ``directory_lookup`` (read, Phase 3a)
 * and ``directory_upsert`` (write, Phase 3b).
 *
 * Spec: internal/spec-user-directory-2026-06-22.md §5 items 1–2 + §10 invariants. Together
 * they let a bot resolve, and now RECORD, one of its OWN Persons (an admitted user OR a
 * saved contact) by ``@handle`` / name / email / id — its messaging identities, contact
 * emails, and contact attributes — but NEVER the private behavioral profile (Phase 4
 * gates that separately) and NEVER anyone's authority (roles / membership / admission).
 *
 * These GENERALIZE / REPLACE the charter's planned ``roster_lookup``: a roster user is
 * just a Person *with* membership, a contact is the same record *without* it, so one pair
 * of tools covers both populations (spec §1 scope decision).
 *
 *   bot agent → directory_lookup({ref})            (read)
 *     └─ POST /api/directory/lookup {ref}           over the admin-daemon UNIX SOCKET
 *   bot agent → directory_upsert({ref, emails?, …}) (write — Phase 3b)
 *     └─ POST /api/directory/upsert {ref, …}        over the admin-daemon UNIX SOCKET
 *          └─ server binds the calling bot from the socket PEER UID (never a request
 *             field), resolves through resolve_person / resolve_identity (THE one read
 *             path — admin & bot can't diverge), stamps writes ``bot-asserted`` (never
 *             from the body), and refuses any authority field.
 *
 * Why the unix socket + peer-uid binding (not TCP, not a botId arg) — identical to
 * GoogleTools: the kernel-reported peer uid is the cross-bot-safe identity primitive. A
 * bot literally cannot present another bot's uid, so both tools are scoped to the caller's
 * own directory by construction. The plugin sends NO botId; a ref that matches nobody in
 * this bot's directory (including another bot's person) resolves to ``null`` (lookup) or a
 * fresh contact in the *caller's own* store (upsert) — never another bot's data.
 *
 * Registered unconditionally on every bot (like RosterTools) — the server is the security
 * boundary; these tools carry no privilege of their own. Every failure mode (HTTP error,
 * daemon unavailable, bad input) returns a NON-throwing tool envelope: a directory fault
 * must never crash the gateway turn.
 */
import { Static } from "@sinclair/typebox";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
import { AdminSocketRequest, AdminSocketResponse } from "../util/adminSocket.js";
/**
 * Transport seam for tests. Real callers omit it and get the live unix-socket
 * call; tests pass a stub that returns canned responses without a daemon. Mirrors
 * RosterTools' ``RosterTransport``.
 */
export type DirectoryTransport = (req: AdminSocketRequest) => Promise<AdminSocketResponse>;
interface DirectoryToolConfig {
    /** This bot's shared dir — the admin-daemon socket lives at {sharedDir}/admin-daemon.sock. */
    readonly sharedDir: string;
    /** This bot's id — for diagnostics only; identity is bound server-side by peer uid. */
    readonly botId: string;
    /** Per-call socket override (tests). Real callers omit it. */
    readonly socketPath?: string;
    /** Transport override for tests. Real callers omit it (live socket). */
    readonly transport?: DirectoryTransport;
}
export declare const DirectoryLookupParamsSchema: import("@sinclair/typebox").TObject<{
    ref: import("@sinclair/typebox").TString;
}>;
export type DirectoryLookupParams = Static<typeof DirectoryLookupParamsSchema>;
/**
 * Build the ``directory_lookup`` tool factory.
 *
 * Each call POSTs ``{ref}`` to ``/api/directory/lookup`` over the unix socket; the server
 * binds identity from the peer uid and returns ``{person}`` (sanitized) or
 * ``{person: null}``. A socket-unavailable condition surfaces as a clean tool error, not
 * a crash — and never throws into the agent loop.
 */
export declare function createDirectoryLookupToolFactory(config: DirectoryToolConfig, logger: PluginLogger): (_ctx: Record<string, unknown>) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        ref: import("@sinclair/typebox").TString;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        isError?: boolean | undefined;
        content: {
            type: "text";
            text: string;
        }[];
    }>;
};
export declare const DirectoryUpsertParamsSchema: import("@sinclair/typebox").TObject<{
    ref: import("@sinclair/typebox").TString;
    emails: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TArray<import("@sinclair/typebox").TObject<{
        addr: import("@sinclair/typebox").TString;
        rank: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"primary">, import("@sinclair/typebox").TLiteral<"secondary">]>>;
    }>>>;
    contact: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TRecord<import("@sinclair/typebox").TString, import("@sinclair/typebox").TString>>;
    identities: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TArray<import("@sinclair/typebox").TObject<{
        platform: import("@sinclair/typebox").TString;
        id: import("@sinclair/typebox").TString;
        handle: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    }>>>;
    names: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TObject<{
        display: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        goes_by: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        legal: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
    }>>;
}>;
export type DirectoryUpsertParams = Static<typeof DirectoryUpsertParamsSchema>;
/**
 * Build the ``directory_upsert`` write-tool factory.
 *
 * POSTs the provided directory-owned fields to ``/api/directory/upsert`` over the unix
 * socket. The SERVER binds the acting bot from the peer uid, stamps the write
 * ``bot-asserted`` (never from the body), refuses any authority field, and returns
 * ``{ok, created, person}``. As with the lookup, every error path returns a non-throwing
 * envelope — a write fault must never break the turn.
 */
export declare function createDirectoryUpsertToolFactory(config: DirectoryToolConfig, logger: PluginLogger): (_ctx: Record<string, unknown>) => {
    name: string;
    description: string;
    parameters: import("@sinclair/typebox").TObject<{
        ref: import("@sinclair/typebox").TString;
        emails: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TArray<import("@sinclair/typebox").TObject<{
            addr: import("@sinclair/typebox").TString;
            rank: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TUnion<[import("@sinclair/typebox").TLiteral<"primary">, import("@sinclair/typebox").TLiteral<"secondary">]>>;
        }>>>;
        contact: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TRecord<import("@sinclair/typebox").TString, import("@sinclair/typebox").TString>>;
        identities: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TArray<import("@sinclair/typebox").TObject<{
            platform: import("@sinclair/typebox").TString;
            id: import("@sinclair/typebox").TString;
            handle: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        }>>>;
        names: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TObject<{
            display: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
            goes_by: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
            legal: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TString>;
        }>>;
    }>;
    execute(_toolCallId: string, rawParams: unknown): Promise<{
        isError?: boolean | undefined;
        content: {
            type: "text";
            text: string;
        }[];
    }>;
};
export {};
//# sourceMappingURL=DirectoryTools.d.ts.map