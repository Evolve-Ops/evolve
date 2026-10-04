/**
 * HTTP Routes — registers per-bot gateway routes by delegating to
 * networkRoutes. The dashboard + structured API live there.
 *
 * Earlier this file also registered:
 *   - GET /evolve/status  — read {shared}/metrics/{botId}-{today}.json
 *   - GET /evolve/metrics — same path
 *
 * Both were dead-on-arrival as METRICS endpoints. measure.py writes
 * `{shared}/metrics/<YYYY-MM-DD>/<bot_id>.json` (dated subdir + bot
 * file); the old endpoints read `{shared}/metrics/<bot_id>-<today>.json`
 * (flat layout). Two layouts, zero overlap. The METRICS code was
 * correctly removed in the 2026-05-28 cross-system audit (#1709).
 *
 * But `/evolve/status` also served a second purpose the audit missed:
 * a LIVENESS BEACON for `_probe_evolve_plugin_loaded` in
 * `packages/admin/evolve_admin/health.py`. The probe checks any one of
 * `{bot_id, plugin_version, status}` to confirm the plugin handler
 * actually ran (vs. the gateway's SPA fallback HTML). Removing the
 * endpoint silently regressed the Maintenance → Evolve Plugin panel:
 * every bot started showing `plugin_loaded: not loaded` (8/8 on the
 * 2026-05-29 deploy). Restored below as a MINIMAL liveness route —
 * no file I/O, no metrics, just the keys the probe needs. Keep this
 * route alive: it has an external consumer (health.py:_probe_evolve_plugin_loaded).
 */
import { registerNetworkRoutes } from "./networkRoutes.js";
// Sync this with the plugin's package.json version when it bumps; the
// probe only reads it for the operator-facing CheckResult message.
const PLUGIN_VERSION = "0.1.0";
export function registerApiRoutes(api, config) {
    // Network management + dashboard API routes
    registerNetworkRoutes(api, config);
    // Liveness beacon — consumed by admin health.py:_probe_evolve_plugin_loaded
    // to distinguish "plugin running" from "gateway up but plugin failed to
    // register." Deliberately minimal: any file I/O or shared-state read here
    // would risk re-introducing the metrics-layout drift that prompted #1709.
    api.registerHttpRoute({
        method: "GET",
        auth: "plugin",
        path: "/evolve/status",
        handler: async (_req, res) => {
            res.setHeader("Content-Type", "application/json");
            res.statusCode = 200;
            res.end(JSON.stringify({
                bot_id: config.botId,
                plugin_version: PLUGIN_VERSION,
                status: "loaded",
            }));
            return true;
        },
    });
}
//# sourceMappingURL=routes.js.map