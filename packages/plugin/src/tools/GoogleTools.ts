/**
 * GoogleTools — the bot-facing curated Google tool surface (P1).
 *
 * Spec: internal/spec-google-integration-architecture-2026-06-20.md (§4.1 delivery
 * shape). Registers the 15 curated Gmail/Calendar/Drive tools on a bot whose
 * `google_integration` is configured, each proxying to the admin daemon's
 * bot-facing route:
 *
 *     bot agent  →  gmail_list_messages / drive_search / ...  (this plugin)
 *       └─ POST /api/google/call {tool, args}  over the admin-daemon UNIX SOCKET
 *            └─ admin server binds the calling bot from the socket PEER UID
 *               (never from a request field), loads creds as that bot, runs the
 *               curated tool, returns results. Creds never leave the evolve user.
 *
 * Why the unix socket (not TCP :5050 like PodStateTools): the route binds the
 * bot's identity from the kernel-reported peer uid of the socket connection.
 * That is the cross-bot-safe identity primitive — a bot literally cannot
 * present another bot's uid. The plugin therefore carries NO botId in the
 * request; the server derives it. (See web/google_bot_routes.py.)
 *
 * Eligibility is decided synchronously at register() time by
 * `googleConfiguredForBot` (reads the bot's google_integration from
 * network.json) — a bot with no Google config registers ZERO tools.
 */

import { request as httpRequest } from "http";
import { readFileSync } from "fs";
import { join } from "path";

import { Type } from "@sinclair/typebox";

import type { PluginLogger } from "openclaw/plugin-sdk/types";

const SOCKET_NAME = "admin-daemon.sock";
const ADMIN_TIMEOUT_MS = 30_000;

const SUPPORTED_MODES = new Set(["service_account_dwd", "free_gmail_oauth"]);

interface AdminResponse {
  status: number;
  body: any;
}

/**
 * POST JSON to the admin daemon over its unix socket. The socket carries the
 * caller's peer uid, which the server maps to this bot — so no identity is
 * sent in the body.
 */
function adminSocketPost(
  socketPath: string,
  urlPath: string,
  payload: unknown,
): Promise<AdminResponse> {
  return new Promise((resolve, reject) => {
    const data = Buffer.from(JSON.stringify(payload ?? {}), "utf8");
    const req = httpRequest(
      {
        socketPath,
        path: urlPath,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": String(data.length),
          Accept: "application/json",
        },
      },
      (res) => {
        let buf = "";
        res.on("data", (chunk: Buffer) => {
          buf += chunk.toString();
        });
        res.on("end", () => {
          let parsed: any = null;
          if (buf) {
            try {
              parsed = JSON.parse(buf);
            } catch {
              parsed = { raw: buf };
            }
          }
          resolve({ status: res.statusCode ?? 0, body: parsed });
        });
      },
    );
    req.on("error", (err: Error) => reject(err));
    req.setTimeout(ADMIN_TIMEOUT_MS, () => {
      req.destroy(new Error("admin request timed out"));
    });
    req.write(data);
    req.end();
  });
}

function textResult(text: string, isError = false) {
  return {
    content: [{ type: "text" as const, text }],
    ...(isError ? { isError: true } : {}),
  };
}

/**
 * Read this bot's google_integration mode from network.json, synchronously.
 *
 * The eligibility gate runs at register() time (which must be synchronous —
 * OC collects the toolset at session start), so we read the world-readable,
 * secret-free network.json directly rather than awaiting an HTTP probe. The
 * SERVER remains the security boundary (it re-checks config + binds identity);
 * this is only the visibility gate. Returns null on any read/parse error or
 * an unconfigured / unsupported mode — fail-closed (no tools).
 */
export function googleConfiguredForBot(
  sharedDir: string,
  botId: string,
): string | null {
  try {
    const raw = readFileSync(join(sharedDir, "network.json"), "utf8");
    const net = JSON.parse(raw);
    const gi = net?.bots?.[botId]?.google_integration;
    const mode = gi?.mode;
    return typeof mode === "string" && SUPPORTED_MODES.has(mode) ? mode : null;
  } catch {
    return null;
  }
}

// ── Tool catalog ──────────────────────────────────────────────────────────────
// Each entry mirrors a curated tool in evolve_admin/google_service.py. The
// `name` is what the bot's agent calls; the server dispatches on the same name.
// Parameter schemas are deliberately permissive (the server does the real
// validation) but describe the arguments so the agent uses them correctly.

const Str = (description: string) => Type.Optional(Type.String({ description }));
const StrReq = (description: string) => Type.String({ description });
const IntOpt = (description: string) => Type.Optional(Type.Integer({ description }));

interface GoogleToolDef {
  name: string;
  description: string;
  params: ReturnType<typeof Type.Object>;
}

const TOOL_DEFS: GoogleToolDef[] = [
  // ── Gmail read ──
  {
    name: "gmail_list_messages",
    description:
      "List messages in your Gmail mailbox. Optional `q` (Gmail search, e.g. " +
      "\"from:alice newer_than:7d\"), `label_ids`, `max_results` (default 25).",
    params: Type.Object({
      q: Str("Gmail search query, e.g. 'from:alice newer_than:7d'"),
      label_ids: Type.Optional(Type.Array(Type.String(), {
        description: "Gmail label IDs to filter on, e.g. ['INBOX','UNREAD']",
      })),
      max_results: IntOpt("Max messages to return (default 25, max 500)"),
    }),
  },
  {
    name: "gmail_get_message",
    description: "Fetch one Gmail message with headers and decoded body.",
    params: Type.Object({ id: StrReq("Gmail message id (from gmail_list_messages)") }),
  },
  {
    name: "gmail_list_labels",
    description:
      "List Gmail labels (system + user) with their ids — use this to learn " +
      "label ids before labeling/archiving.",
    params: Type.Object({}),
  },
  // ── Gmail write / modify ──
  {
    name: "gmail_send",
    description:
      "Send an email from your mailbox. Requires `to`, `subject`, `body`. " +
      "Optional `cc`, `bcc`, `attachments`.",
    params: Type.Object({
      to: Type.Union([Type.String(), Type.Array(Type.String())], {
        description: "Recipient address(es)",
      }),
      subject: StrReq("Email subject"),
      body: StrReq("Plain-text body"),
      cc: Type.Optional(Type.Union([Type.String(), Type.Array(Type.String())])),
      bcc: Type.Optional(Type.Union([Type.String(), Type.Array(Type.String())])),
      attachments: Type.Optional(Type.Array(Type.String(), {
        description:
          "File paths inside your workspace, absolute or workspace-relative " +
          "(outside paths refused); 20MB total.",
      })),
    }),
  },
  {
    name: "gmail_label_message",
    description:
      "Add and/or remove labels on a message. At least one of `add_label_ids` " +
      "/ `remove_label_ids` is required (use gmail_list_labels for ids).",
    params: Type.Object({
      id: StrReq("Gmail message id"),
      add_label_ids: Type.Optional(Type.Array(Type.String())),
      remove_label_ids: Type.Optional(Type.Array(Type.String())),
    }),
  },
  {
    name: "gmail_archive_message",
    description: "Archive a message (remove it from the inbox).",
    params: Type.Object({ id: StrReq("Gmail message id") }),
  },
  {
    name: "gmail_mark_read",
    description: "Mark a message as read.",
    params: Type.Object({ id: StrReq("Gmail message id") }),
  },
  {
    name: "gmail_mark_unread",
    description: "Mark a message as unread.",
    params: Type.Object({ id: StrReq("Gmail message id") }),
  },
  {
    name: "gmail_delete_message",
    description:
      "PERMANENTLY delete a message (not Trash — unrecoverable). Requires " +
      "`confirm: true`. For a recoverable delete, use gmail_trash_message.",
    params: Type.Object({
      id: StrReq("Gmail message id"),
      confirm: Type.Boolean({ description: "Must be true to delete" }),
    }),
  },
  {
    name: "gmail_trash_message",
    description:
      "Move a message to Trash — recoverable (~30 days, then purged). This " +
      "is the recoverable delete; gmail_label_message will not apply the " +
      "TRASH label. Requires `confirm: true`.",
    params: Type.Object({
      id: StrReq("Gmail message id"),
      confirm: Type.Boolean({ description: "Must be true to trash" }),
    }),
  },
  // ── Calendar ──
  {
    name: "calendar_list_events",
    description:
      "List upcoming calendar events. Optional `calendar_id` (default " +
      "'primary'), `time_min`/`time_max` (ISO 8601), `q`, `max_results`.",
    params: Type.Object({
      calendar_id: Str("Calendar id (default 'primary')"),
      time_min: Str("ISO 8601 lower bound (default now)"),
      time_max: Str("ISO 8601 upper bound"),
      q: Str("Free-text search"),
      max_results: IntOpt("Max events (default 25)"),
    }),
  },
  {
    name: "calendar_create_event",
    description:
      "Create a calendar event. Requires `summary`, `start`, `end` (ISO 8601 " +
      "with timezone). Optional `description`, `location`, `calendar_id`, `attendees`.",
    params: Type.Object({
      summary: StrReq("Event title"),
      start: StrReq("ISO 8601 start with timezone, e.g. 2026-06-15T18:00:00-07:00"),
      end: StrReq("ISO 8601 end with timezone"),
      description: Str("Event description"),
      location: Str("Event location"),
      calendar_id: Str("Calendar id (default 'primary')"),
      attendees: Type.Optional(Type.Array(Type.String(), {
        description: "Attendee email addresses",
      })),
    }),
  },
  // ── Drive ──
  {
    name: "drive_list_files",
    description:
      "List Drive files you own or are shared on (drive.file scope). Optional " +
      "`q` (Drive query), `page_size`, `order_by`.",
    params: Type.Object({
      q: Str("Drive query, e.g. \"mimeType='application/pdf'\""),
      page_size: IntOpt("Max files (default 25)"),
      order_by: Str("e.g. 'modifiedTime desc'"),
    }),
  },
  {
    name: "drive_read_file",
    description:
      "Fetch a Drive file's content (text decoded; binary as base64). Google-" +
      "native Docs/Sheets/Slides are not yet supported.",
    params: Type.Object({
      id: StrReq("Drive file id (from drive_list_files)"),
      max_bytes: IntOpt("Max bytes to load (default 1 MiB, max 10 MiB)"),
    }),
  },
  {
    name: "drive_write_file",
    description:
      "Create a file in your Drive. Requires `name`, `content`. Optional " +
      "`mime_type`, `parent_folder_id`.",
    params: Type.Object({
      name: StrReq("File name"),
      content: StrReq("File body (text)"),
      mime_type: Str("MIME type (default text/plain)"),
      parent_folder_id: Str("Parent Drive folder id"),
    }),
  },
  {
    name: "drive_search",
    description:
      "Search across ALL Drive files visible to you (drive.readonly scope) — " +
      "broader than drive_list_files. Optional `q`, `page_size`, `order_by`.",
    params: Type.Object({
      q: Str("Drive query, e.g. \"name contains 'invoice'\""),
      page_size: IntOpt("Max files (default 25)"),
      order_by: Str("e.g. 'modifiedTime desc'"),
    }),
  },
];

/**
 * Build the curated Google tool factories for a configured bot.
 *
 * Each tool POSTs {tool, args} to /api/google/call over the unix socket. The
 * server binds identity from the peer uid — the plugin sends NO botId. A 403
 * `not_configured` (config changed out from under us) surfaces as a clean
 * tool error rather than a crash.
 */
export function createGoogleToolFactories(
  config: { sharedDir: string; botId: string },
  logger: PluginLogger,
): Array<(ctx: Record<string, unknown>) => unknown> {
  const socketPath = join(config.sharedDir, SOCKET_NAME);

  return TOOL_DEFS.map((def) => (_ctx: Record<string, unknown>) => ({
    name: def.name,
    description: def.description,
    parameters: def.params,
    async execute(_toolCallId: string, rawParams: unknown) {
      const args = (rawParams ?? {}) as Record<string, unknown>;
      try {
        const res = await adminSocketPost(socketPath, "/api/google/call", {
          tool: def.name,
          args,
        });
        if (res.status === 200) {
          return textResult(JSON.stringify(res.body));
        }
        const err =
          (res.body && (res.body.detail || res.body.error)) ||
          `HTTP ${res.status}`;
        return textResult(`${def.name} failed: ${err}`, true);
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.warn(`${def.name}: admin request failed: ${msg}`);
        return textResult(`${def.name}: ${msg}`, true);
      }
    },
  }));
}
