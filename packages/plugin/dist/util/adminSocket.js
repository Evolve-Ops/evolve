/**
 * Unix-socket HTTP client for the admin daemon.
 *
 * Phase C.2 of docs/spec-user-roster-and-roles-2026-06-07.md. Lets
 * plugin-registered tools (roster_set_role, roster_block, etc.) call the
 * admin-daemon's typed roster API at ``{sharedDir}/admin-daemon.sock``
 * with an ``X-Requester-Identity`` header carrying the message sender's
 * stable_id, so the daemon's capability check applies to the right
 * principal.
 *
 * The socket lives under the pod's shared dir, which is platform-keyed:
 * ``/Users/Shared/evolve`` on macOS, ``/var/lib/evolve`` on Linux. Callers
 * derive the path from their resolved ``sharedDir`` via
 * ``adminDaemonSocketPath`` rather than relying on the macOS-shaped
 * ``DEFAULT_ADMIN_DAEMON_SOCKET`` fallback — the latter never resolves on
 * a Linux pod.
 *
 * Node's built-in ``http`` module already supports unix sockets via the
 * ``socketPath`` option — no third-party deps needed. Auth is the peer-
 * credential check on the unix socket (per evo-account-separation): the
 * fact that the calling process's effective uid is in the daemon's
 * trusted-user list IS the connection auth. The header layer is a
 * different concern — it tells the daemon *which user* triggered the
 * request, so the daemon can resolve their role and check capabilities.
 *
 * The plugin runs as the bot's macOS user (e.g. ``atlas``), and the
 * admin daemon's trusted-user allowlist includes the bot users (per
 * the unix_socket_server peer-credential check). So the socket call
 * succeeds for the connection itself; the header check then governs
 * whether the action is allowed.
 */
import { request as httpRequest } from "node:http";
import { join as joinPath } from "node:path";
/**
 * macOS-shaped fallback socket path. Only correct on macOS pods; on Linux
 * the shared dir is ``/var/lib/evolve``. Prefer ``adminDaemonSocketPath``
 * with the plugin's resolved ``sharedDir`` so the path matches what the
 * admin daemon actually binds on this platform.
 */
export const DEFAULT_ADMIN_DAEMON_SOCKET = "/Users/Shared/evolve/admin-daemon.sock";
/**
 * Resolve the admin-daemon socket path from the pod's shared dir.
 *
 * The admin daemon binds ``{sharedDir}/admin-daemon.sock`` (see
 * ``evolve_admin.web.unix_socket_server``). ``config.sharedDir`` is
 * platform-keyed by ``resolveConfig``, so this yields the right path on
 * both macOS (``/Users/Shared/evolve``) and Linux (``/var/lib/evolve``).
 */
export function adminDaemonSocketPath(sharedDir) {
    return joinPath(sharedDir, "admin-daemon.sock");
}
export class AdminSocketUnavailable extends Error {
    constructor(message) {
        super(message);
        this.name = "AdminSocketUnavailable";
    }
}
/**
 * Issue a single HTTP request to the admin daemon over its unix socket.
 *
 * Rejects with ``AdminSocketUnavailable`` when the socket isn't reachable
 * (file missing, daemon down, perms denied) so callers can return a
 * helpful "admin daemon unavailable" envelope to the LLM instead of a
 * generic exception.
 *
 * HTTP error statuses (4xx/5xx) are returned as ``{status, body}`` —
 * the caller decides what to do (e.g. 403 → "forbidden", 400 →
 * "validation error", etc.). Only connection-level failures throw.
 */
export async function adminSocketRequest(req) {
    return new Promise((resolve, reject) => {
        const bodyJSON = req.body !== undefined ? JSON.stringify(req.body) : "";
        const headers = {
            // Cosmetic; the daemon doesn't validate Host on the unix socket.
            Host: "admin-daemon",
            ...(req.headers ?? {}),
        };
        if (bodyJSON) {
            headers["Content-Type"] = headers["Content-Type"] ?? "application/json";
            headers["Content-Length"] = String(Buffer.byteLength(bodyJSON, "utf-8"));
        }
        const reqOpts = {
            socketPath: req.socketPath ?? DEFAULT_ADMIN_DAEMON_SOCKET,
            method: req.method,
            path: req.path,
            headers,
            timeout: req.timeoutMs ?? 10_000,
        };
        const r = httpRequest(reqOpts, (res) => {
            let chunks = "";
            res.setEncoding("utf-8");
            res.on("data", (chunk) => {
                chunks += chunk;
            });
            res.on("end", () => {
                let parsed = null;
                if (chunks.length > 0) {
                    try {
                        parsed = JSON.parse(chunks);
                    }
                    catch {
                        // Non-JSON (e.g. Flask's default 500 HTML) — surface as string
                        // so the caller can include it in the error envelope.
                        parsed = chunks;
                    }
                }
                resolve({ status: res.statusCode ?? 0, body: parsed });
            });
            res.on("error", (err) => reject(new AdminSocketUnavailable(`response error from ${req.method} ${req.path}: ${err.message}`)));
        });
        r.on("error", (err) => {
            // ENOENT / ECONNREFUSED / EACCES on the socket map to "daemon
            // unavailable" — distinguish so callers can return a helpful
            // message rather than a generic error.
            if (err.code === "ENOENT" ||
                err.code === "ECONNREFUSED" ||
                err.code === "EACCES") {
                reject(new AdminSocketUnavailable(`cannot reach admin daemon at ${reqOpts.socketPath}: ${err.message}`));
            }
            else {
                reject(err);
            }
        });
        r.on("timeout", () => {
            r.destroy();
            reject(new AdminSocketUnavailable(`timeout calling ${req.method} ${req.path}`));
        });
        if (bodyJSON)
            r.write(bodyJSON);
        r.end();
    });
}
/**
 * Extract a Telegram numeric user_id from an OC sessionKey string.
 *
 * Telegram sessions key by chat_id, which for a 1:1 DM equals the user's
 * numeric id, and for a group/supergroup is a negative integer. The
 * existing Phase 2 Layer C interceptor extracts the same value via
 * ``sessionKey.match(/:(-?\d+)$/)``.
 *
 * For Path B (primary_user mutates the roster from chat), we want the
 * SENDER's id — which IS the chat_id for telegram_dm but is NOT the
 * chat_id for telegram_group (the group's id != any user's id). The
 * caller must know which surface they're on.
 *
 * Returns null when the sessionKey doesn't match the expected shape.
 */
export function extractTelegramIdFromSessionKey(sessionKey) {
    if (!sessionKey)
        return null;
    const m = String(sessionKey).match(/:(-?\d+)$/);
    return m ? m[1] : null;
}
//# sourceMappingURL=adminSocket.js.map