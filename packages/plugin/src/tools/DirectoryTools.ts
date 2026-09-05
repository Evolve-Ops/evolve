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

import { join } from "node:path";

import { Type, Static } from "@sinclair/typebox";

import type { PluginLogger } from "openclaw/plugin-sdk/types";

import {
  AdminSocketRequest,
  AdminSocketResponse,
  AdminSocketUnavailable,
  adminSocketRequest,
} from "../util/adminSocket.js";


const SOCKET_NAME = "admin-daemon.sock";


/**
 * Transport seam for tests. Real callers omit it and get the live unix-socket
 * call; tests pass a stub that returns canned responses without a daemon. Mirrors
 * RosterTools' ``RosterTransport``.
 */
export type DirectoryTransport =
  (req: AdminSocketRequest) => Promise<AdminSocketResponse>;


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


export const DirectoryLookupParamsSchema = Type.Object({
  ref: Type.String({
    description:
      "Who to look up — a person's @handle, full or display name, email address, or " +
      "stable person id. Resolves ONLY people in this bot's own directory (its admitted " +
      "users and saved contacts). Returns their messaging IDs, contact emails, and role.",
  }),
});
export type DirectoryLookupParams = Static<typeof DirectoryLookupParamsSchema>;


function textResult(text: string, isError = false) {
  return {
    content: [{ type: "text" as const, text }],
    ...(isError ? { isError: true } : {}),
  };
}


/**
 * Build the ``directory_lookup`` tool factory.
 *
 * Each call POSTs ``{ref}`` to ``/api/directory/lookup`` over the unix socket; the server
 * binds identity from the peer uid and returns ``{person}`` (sanitized) or
 * ``{person: null}``. A socket-unavailable condition surfaces as a clean tool error, not
 * a crash — and never throws into the agent loop.
 */
export function createDirectoryLookupToolFactory(
  config: DirectoryToolConfig,
  logger: PluginLogger,
) {
  const socketPath = config.socketPath ?? join(config.sharedDir, SOCKET_NAME);
  const transport: DirectoryTransport = config.transport ?? adminSocketRequest;

  return (_ctx: Record<string, unknown>) => ({
    name: "directory_lookup",
    description:
      "Look up a person in THIS bot's user directory — its admitted users and saved " +
      "contacts — by @handle, name, email, or id. Returns their messaging IDs, contact " +
      "emails, and role. Use it instead of guessing or hand-typing someone's id/email: " +
      "the directory is the source of truth. It does not expose anyone's private " +
      "behavioral profile, and it can only see this bot's own contacts.",
    parameters: DirectoryLookupParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const params = (rawParams ?? {}) as DirectoryLookupParams;
      const ref = typeof params.ref === "string" ? params.ref.trim() : "";
      if (!ref) {
        return textResult(
          "Provide who to look up — a name, @handle, email, or id.", true);
      }
      try {
        const res = await transport({
          method: "POST",
          path: "/api/directory/lookup",
          body: { ref },
          socketPath,
        });
        if (res.status === 200) {
          const person =
            (res.body && typeof res.body === "object"
              ? (res.body as Record<string, unknown>).person
              : null) ?? null;
          if (!person) {
            return textResult(
              `No one in this bot's directory matches ${JSON.stringify(ref)}. ` +
              "They may not be a registered user or saved contact yet.");
          }
          return textResult(JSON.stringify(person));
        }
        const detail =
          (res.body && typeof res.body === "object" &&
            ("detail" in res.body || "error" in res.body))
            ? String(
              (res.body as Record<string, unknown>).detail ??
              (res.body as Record<string, unknown>).error ?? "")
            : `HTTP ${res.status}`;
        logger.warn(`directory_lookup failed (${res.status}): ${detail}`);
        return textResult(`directory_lookup failed: ${detail}`, true);
      } catch (err) {
        if (err instanceof AdminSocketUnavailable) {
          logger.warn(`directory_lookup — admin daemon unavailable: ${err.message}`);
          return textResult(
            "The directory is unavailable right now (admin daemon unreachable). " +
            "Try again in a moment.", true);
        }
        const msg = err instanceof Error ? err.message : String(err);
        logger.error(`directory_lookup unexpected error: ${msg}`);
        return textResult(`directory_lookup error: ${msg}`, true);
      }
    },
  });
}


// ── directory_upsert (write — Phase 3b) ─────────────────────────────────────


const UpsertEmailSchema = Type.Object({
  addr: Type.String({ description: "The email address." }),
  rank: Type.Optional(Type.Union(
    [Type.Literal("primary"), Type.Literal("secondary")],
    {
      description:
        "'primary' for the person's main address (at most one), else 'secondary'. " +
        "Defaults to 'secondary'.",
    })),
});

const UpsertIdentitySchema = Type.Object({
  platform: Type.String({ description: "Messaging platform, e.g. 'slack' or 'telegram'." }),
  id: Type.String({ description: "The person's stable id on that platform." }),
  handle: Type.Optional(Type.String({ description: "Their @handle on that platform." })),
});

const UpsertNamesSchema = Type.Object({
  display: Type.Optional(Type.String({ description: "How to refer to them (display name)." })),
  goes_by: Type.Optional(Type.String({ description: "A preferred short name / nickname." })),
  legal: Type.Optional(Type.String({ description: "Their full legal name, if known." })),
});

export const DirectoryUpsertParamsSchema = Type.Object({
  ref: Type.String({
    description:
      "Who to record — the @handle, name, email, or stable id of an existing person to " +
      "UPDATE, OR an email/identity for a NEW contact to create. Only ever touches this " +
      "bot's own directory.",
  }),
  emails: Type.Optional(Type.Array(UpsertEmailSchema, {
    description:
      "Contact email addresses to record (their inbox — distinct from your send-as " +
      "From address). At most one 'primary'.",
  })),
  contact: Type.Optional(Type.Record(Type.String(), Type.String(), {
    description:
      "Open contact attributes as a flat map of text values, e.g. " +
      "{\"phone\":\"+1…\",\"org\":\"Acme\",\"notes\":\"met at conf\"}.",
  })),
  identities: Type.Optional(Type.Array(UpsertIdentitySchema, {
    description: "Messaging identities (platform + id [+ handle]) to record for them.",
  })),
  names: Type.Optional(UpsertNamesSchema),
});
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
export function createDirectoryUpsertToolFactory(
  config: DirectoryToolConfig,
  logger: PluginLogger,
) {
  const socketPath = config.socketPath ?? join(config.sharedDir, SOCKET_NAME);
  const transport: DirectoryTransport = config.transport ?? adminSocketRequest;

  return (_ctx: Record<string, unknown>) => ({
    name: "directory_upsert",
    description:
      "Record or correct a person's identity and contact details in THIS bot's user " +
      "directory — their messaging IDs, contact emails (primary/secondary), names, and " +
      "contact attributes (phone, org, notes). This is the CANONICAL place to save such " +
      "details: put them here, NOT in USER.md or your own notes (prose there is not " +
      "authoritative, drifts, and your operator can't see or edit it). Pass `ref` to an " +
      "existing person to update them, or an email/identity to create a new contact " +
      "(someone you correspond with who isn't a registered user). It records WHO someone " +
      "is and HOW TO REACH them only — it can NEVER grant roles, capabilities, " +
      "membership, or admission (those are operator-gated), and it can only ever write " +
      "this bot's own directory.",
    parameters: DirectoryUpsertParamsSchema,
    async execute(_toolCallId: string, rawParams: unknown) {
      const params = (rawParams ?? {}) as DirectoryUpsertParams;
      const ref = typeof params.ref === "string" ? params.ref.trim() : "";
      if (!ref) {
        return textResult(
          "Provide `ref` — who to record (a name, @handle, email, or id).", true);
      }
      // Send only the fields the caller actually provided (no nulls); identity is
      // peer-uid bound, so NO botId is ever sent.
      const body: Record<string, unknown> = { ref };
      if (params.emails !== undefined) body.emails = params.emails;
      if (params.contact !== undefined) body.contact = params.contact;
      if (params.identities !== undefined) body.identities = params.identities;
      if (params.names !== undefined) body.names = params.names;

      try {
        const res = await transport({
          method: "POST",
          path: "/api/directory/upsert",
          body,
          socketPath,
        });
        if (res.status === 200) {
          const obj =
            res.body && typeof res.body === "object"
              ? (res.body as Record<string, unknown>)
              : {};
          const person = obj.person ?? null;
          const created = obj.created === true;
          if (!person) {
            // A clean success with nothing to show (e.g. a blocked/no-op target).
            return textResult("Directory updated.");
          }
          const verb = created ? "Created a new contact" : "Updated the directory entry";
          return textResult(`${verb}. Stored record: ${JSON.stringify(person)}`);
        }
        const detail =
          (res.body && typeof res.body === "object" &&
            ("detail" in res.body || "error" in res.body))
            ? String(
              (res.body as Record<string, unknown>).detail ??
              (res.body as Record<string, unknown>).error ?? "")
            : `HTTP ${res.status}`;
        logger.warn(`directory_upsert failed (${res.status}): ${detail}`);
        return textResult(`directory_upsert failed: ${detail}`, true);
      } catch (err) {
        if (err instanceof AdminSocketUnavailable) {
          logger.warn(`directory_upsert — admin daemon unavailable: ${err.message}`);
          return textResult(
            "The directory is unavailable right now (admin daemon unreachable). " +
            "Try again in a moment.", true);
        }
        const msg = err instanceof Error ? err.message : String(err);
        logger.error(`directory_upsert unexpected error: ${msg}`);
        return textResult(`directory_upsert error: ${msg}`, true);
      }
    },
  });
}
