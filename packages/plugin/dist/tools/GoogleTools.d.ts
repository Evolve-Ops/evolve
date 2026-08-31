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
import type { PluginLogger } from "openclaw/plugin-sdk/types";
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
export declare function googleConfiguredForBot(sharedDir: string, botId: string): string | null;
/**
 * Build the curated Google tool factories for a configured bot.
 *
 * Each tool POSTs {tool, args} to /api/google/call over the unix socket. The
 * server binds identity from the peer uid — the plugin sends NO botId. A 403
 * `not_configured` (config changed out from under us) surfaces as a clean
 * tool error rather than a crash.
 */
export declare function createGoogleToolFactories(config: {
    sharedDir: string;
    botId: string;
}, logger: PluginLogger): Array<(ctx: Record<string, unknown>) => unknown>;
//# sourceMappingURL=GoogleTools.d.ts.map