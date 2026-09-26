/**
 * Network API routes for the dashboard.
 *
 * GET  /evolve/api/network            — Current network config
 * GET  /evolve/api/scoreboard         — Latest scoreboard JSON
 * GET  /evolve/api/proposals          — Pending proposals
 * POST /evolve/api/proposals/:id/approve
 * POST /evolve/api/proposals/:id/reject  (body: {reason, note})
 * POST /evolve/api/proposals/:id/defer
 * POST /evolve/api/network/bots       — Add a bot
 * DEL  /evolve/api/network/bots/:id   — Remove a bot
 * PATCH /evolve/api/network/config    — Update thresholds/alerts
 */
import type { EvolveConfig } from "../config.js";
export declare function registerNetworkRoutes(api: any, config: EvolveConfig): void;
//# sourceMappingURL=networkRoutes.d.ts.map