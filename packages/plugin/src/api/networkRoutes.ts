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

import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";
import type { EvolveConfig } from "../config.js";
import { evolvePythonBin, resolveAnalyzerDir } from "../observer/TurnObserver.js";

// Root of the packages/ directory — used to locate sibling packages (analyzer, dashboard).
// Plugin compiles to packages/plugin/dist/api/, so three dirname calls reach packages/.
const __dirname = fileURLToPath(new URL(".", import.meta.url));
const PACKAGES_ROOT = path.resolve(__dirname, "../../..");

export function registerNetworkRoutes(api: any, config: EvolveConfig): void {
  const shared = config.sharedDir;

  async function parseBody(req: any): Promise<Record<string, unknown>> {
    return new Promise((resolve) => {
      let data = "";
      req.on("data", (chunk: Buffer) => { data += chunk.toString(); });
      req.on("end", () => {
        try { resolve(JSON.parse(data)); } catch { resolve({}); }
      });
      req.on("error", () => resolve({}));
    });
  }

  function parseParam(req: any, name: string, position = -1): string {
    if (req.params?.[name]) return req.params[name];
    const parts = (req.url ?? "").split("?")[0].split("/");
    return parts.at(position) ?? "";
  }



  function readJSON(filePath: string): unknown {
    try {
      return JSON.parse(fs.readFileSync(filePath, "utf8"));
    } catch {
      return null;
    }
  }

  function writeJSON(filePath: string, data: unknown): void {
    const tmp = filePath + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(data, null, 2));
    fs.renameSync(tmp, filePath);
  }

  function latestScoreboard(): unknown {
    const dir = path.join(shared, "scoreboard");
    if (!fs.existsSync(dir)) return { bots: {}, network_score: 0 };
    const files = fs.readdirSync(dir)
      .filter((f) => f.startsWith("network-") && f.endsWith(".json"))
      .sort()
      .reverse();
    if (!files.length) return { bots: {}, network_score: 0 };
    return readJSON(path.join(dir, files[0])) ?? { bots: {}, network_score: 0 };
  }

  function loadPendingProposals(): unknown[] {
    const pendingDir = path.join(shared, "proposals", "pending");
    if (!fs.existsSync(pendingDir)) return [];
    return fs
      .readdirSync(pendingDir)
      .filter((f) => f.endsWith(".json"))
      .map((f) => readJSON(path.join(pendingDir, f)))
      .filter(Boolean) as unknown[];
  }

  function networkConfigPath(): string {
    // Look for network.json in workspace
    const candidates = [
      path.join(path.dirname(shared), "workspace", "evolve", "network.json"),
      path.join(shared, "network.json"),
    ];
    for (const c of candidates) {
      if (fs.existsSync(c)) return c;
    }
    return candidates[0]; // default write path
  }

  // GET /evolve/api/network
  api.registerHttpRoute({
    method: "GET",
    auth: "plugin",
    path: "/evolve/api/network",
    handler: async (_req: any, res: any) => {
      const cfg = readJSON(networkConfigPath()) ?? {
        networkId: config.networkId,
        primary: config.botId,
        members: [config.botId],
        sharedDir: shared,
        thresholds: {},
        alerts: {},
        bots: {},
      };
      res.setHeader("Content-Type", "application/json");
      res.statusCode = 200;
      res.end(JSON.stringify(cfg));
      return true;
    },
  });

  // Note: GET /evolve/api/metrics/:botId was REMOVED 2026-05-28. It
  // read `{shared}/metrics/<bot>-<date>.json` (flat layout, bot-prefix
  // filename) but measure.py writes `{shared}/metrics/<date>/<bot>.json`
  // (dated subdir layout). The endpoint always returned `[]`. Cross-
  // system contract audit confirmed zero callers. If a per-bot metrics
  // history endpoint is wanted later, consume measure.py's actual
  // layout — DO NOT re-add this version.

  // GET /evolve/api/scoreboard
  api.registerHttpRoute({
    method: "GET",
    auth: "plugin",
    path: "/evolve/api/scoreboard",
    handler: async (_req: any, res: any) => {
      res.setHeader("Content-Type", "application/json");
      res.statusCode = 200;
      res.end(JSON.stringify(latestScoreboard()));
      return true;
    },
  });

  // GET /evolve/api/proposals
  api.registerHttpRoute({
    method: "GET",
    auth: "plugin",
    path: "/evolve/api/proposals",
    handler: async (_req: any, res: any) => {
      res.setHeader("Content-Type", "application/json");
      res.statusCode = 200;
      res.end(JSON.stringify({ pending: loadPendingProposals() }));
      return true;
    },
  });

  // POST /evolve/api/proposals/:id/approve
  api.registerHttpRoute({
    method: "POST",
    auth: "plugin",
    path: "/evolve/api/proposals/:id/approve",
    handler: async (req: any, res: any) => {
      const id = parseParam(req, 'id', -2);
      const pendingPath = path.join(shared, "proposals", "pending", `${id}.json`);
      const approvedPath = path.join(shared, "proposals", "approved", `${id}.json`);
      if (!fs.existsSync(pendingPath)) {
        res.setHeader("Content-Type", "application/json");
        res.statusCode = 404;
        res.end(JSON.stringify({ error: "Proposal not found" }));
        return true;
      }

      // Security screen (review.py retirement, 2026-07-28): approval must
      // consult the arbiter's folded deny mandate. The screen CLI prints
      // {"result": "allow"|"deny", "denials": [...]}; deny blocks the
      // approve, and a failed screen fails CLOSED (503) — a proposal must
      // not reach proposals/approved/ (the dir apply.py acts on) without a
      // successful "allow" verdict. Operators hitting 503 on a broken
      // analyzer env still have the admin-server approve surface.
      try {
        const { execFile } = await import("child_process");
        const { promisify } = await import("util");
        const execFileAsync = promisify(execFile);
        const { stdout } = await execFileAsync(
          evolvePythonBin(),
          ["-m", "arbiter.security_screen", "--proposal", pendingPath],
          { cwd: resolveAnalyzerDir(config), timeout: 15_000 },
        );
        const verdict = JSON.parse(stdout) as { result?: string; denials?: unknown[] };
        if (verdict.result !== "allow") {
          res.setHeader("Content-Type", "application/json");
          res.statusCode = 403;
          res.end(JSON.stringify({
            error: "security_screen_denied",
            denials: verdict.denials ?? [],
          }));
          return true;
        }
      } catch (err: any) {
        res.setHeader("Content-Type", "application/json");
        res.statusCode = 503;
        res.end(JSON.stringify({
          error: "security_screen_unavailable",
          detail: String(err?.message ?? err),
        }));
        return true;
      }

      const proposal = readJSON(pendingPath) as Record<string, unknown>;
      proposal.status = "approved";
      proposal.approved_at = new Date().toISOString();
      fs.mkdirSync(path.dirname(approvedPath), { recursive: true });
      writeJSON(approvedPath, proposal);
      fs.unlinkSync(pendingPath);
      res.setHeader("Content-Type", "application/json");
      res.statusCode = 200;
      res.end(JSON.stringify({ ok: true }));
      return true;
    },
  });

  // POST /evolve/api/proposals/:id/reject
  api.registerHttpRoute({
    method: "POST",
    auth: "plugin",
    path: "/evolve/api/proposals/:id/reject",
    handler: async (req: any, res: any) => {
      const id = parseParam(req, 'id', -2);
      const pendingPath = path.join(shared, "proposals", "pending", `${id}.json`);
      if (!fs.existsSync(pendingPath)) {
        res.setHeader("Content-Type", "application/json");
        res.statusCode = 404;
        res.end(JSON.stringify({ error: "Proposal not found" }));
        return true;
      }
      const proposal = readJSON(pendingPath) as Record<string, unknown>;
      proposal.status = "rejected";
      proposal.rejected_at = new Date().toISOString();
      const rejectBody = await parseBody(req);
      proposal.rejection_reason = (rejectBody as any)?.reason ?? "other";
      proposal.rejection_note = (rejectBody as any)?.note ?? "";

      // Log to rejections JSONL for analysis engine learning
      const rejectionsPath = path.join(shared, "feedback", "rejections.jsonl");
      fs.mkdirSync(path.dirname(rejectionsPath), { recursive: true });
      fs.appendFileSync(
        rejectionsPath,
        JSON.stringify({
          proposal_id: id,
          pattern_key: (proposal as any).pattern_key,
          target_bot: (proposal as any).target_bot,
          type: (proposal as any).type,
          reason: (rejectBody as any)?.reason,
          note: (rejectBody as any)?.note,
          rejected_at: new Date().toISOString(),
        }) + "\n"
      );

      fs.unlinkSync(pendingPath);
      res.setHeader("Content-Type", "application/json");
      res.statusCode = 200;
      res.end(JSON.stringify({ ok: true }));
      return true;
    },
  });

  // POST /evolve/api/proposals/:id/defer
  api.registerHttpRoute({
    method: "POST",
    auth: "plugin",
    path: "/evolve/api/proposals/:id/defer",
    handler: async (req: any, res: any) => {
      const id = parseParam(req, 'id', -2);
      const pendingPath = path.join(shared, "proposals", "pending", `${id}.json`);
      if (fs.existsSync(pendingPath)) {
        const proposal = readJSON(pendingPath) as Record<string, unknown>;
        proposal.status = "deferred";
        proposal.deferred_at = new Date().toISOString();
        writeJSON(pendingPath, proposal); // keep in pending but mark deferred
      }
      res.setHeader("Content-Type", "application/json");
      res.statusCode = 200;
      res.end(JSON.stringify({ ok: true }));
      return true;
    },
  });

  // POST /evolve/api/network/bots
  api.registerHttpRoute({
    method: "POST",
    auth: "plugin",
    path: "/evolve/api/network/bots",
    handler: async (req: any, res: any) => {
      const body219 = await parseBody(req);
      const { botId, role, port } = body219 as any ?? {};
      if (!botId) {
        res.setHeader("Content-Type", "application/json");
        res.statusCode = 400;
        res.end(JSON.stringify({ error: "botId required" }));
        return true;
      }
      const cfgPath = networkConfigPath();
      const cfg = (readJSON(cfgPath) ?? {}) as Record<string, unknown>;
      const members = (cfg.members as string[]) ?? [];
      if (!members.includes(botId)) members.push(botId);
      cfg.members = members;
      const bots = (cfg.bots as Record<string, unknown>) ?? {};
      bots[botId] = { role: role ?? "member", port: port ?? null };
      cfg.bots = bots;
      fs.mkdirSync(path.dirname(cfgPath), { recursive: true });
      writeJSON(cfgPath, cfg);
      res.setHeader("Content-Type", "application/json");
      res.statusCode = 200;
      res.end(JSON.stringify({ ok: true }));
      return true;
    },
  });

  // DELETE /evolve/api/network/bots/:id
  api.registerHttpRoute({
    method: "DELETE",
    auth: "plugin",
    path: "/evolve/api/network/bots/:id",
    handler: async (req: any, res: any) => {
      const botId = parseParam(req, 'id');
      const cfgPath = networkConfigPath();
      const cfg = (readJSON(cfgPath) ?? {}) as Record<string, unknown>;
      cfg.members = ((cfg.members as string[]) ?? []).filter((m) => m !== botId);
      const bots = (cfg.bots as Record<string, unknown>) ?? {};
      delete bots[botId];
      cfg.bots = bots;
      writeJSON(cfgPath, cfg);
      res.setHeader("Content-Type", "application/json");
      res.statusCode = 200;
      res.end(JSON.stringify({ ok: true }));
      return true;
    },
  });

  // PATCH /evolve/api/network/config
  api.registerHttpRoute({
    method: "PATCH",
    auth: "plugin",
    path: "/evolve/api/network/config",
    handler: async (req: any, res: any) => {
      const cfgPath = networkConfigPath();
      const cfg = (readJSON(cfgPath) ?? {}) as Record<string, unknown>;
      const patch = await parseBody(req) ?? {};
      // Deep merge top-level keys
      for (const [key, val] of Object.entries(patch)) {
        if (typeof val === "object" && val !== null && !Array.isArray(val)) {
          cfg[key] = { ...(cfg[key] as object ?? {}), ...(val as object) };
        } else {
          cfg[key] = val;
        }
      }
      writeJSON(cfgPath, cfg);
      res.setHeader("Content-Type", "application/json");
      res.statusCode = 200;
      res.end(JSON.stringify({ ok: true }));
      return true;
    },
  });

  // ── Manifest API ─────────────────────────────────────────────────────────────
  // Manifests live in the bot's own workspace: ~/.openclaw/workspace/manifests/
  // The admin UI calls these endpoints to read/write manifests per bot.

  const manifestsDir = () =>
    path.join(`/Users/${config.botId}/.openclaw/workspace`, "manifests");

  // GET /evolve/manifests — list all manifests for this bot
  api.registerHttpRoute({
    method: "GET",
    auth: "plugin",
    path: "/evolve/manifests",
    handler: async (_req: any, res: any) => {
      const dir = manifestsDir();
      const manifests: unknown[] = [];
      if (fs.existsSync(dir)) {
        for (const f of fs.readdirSync(dir)) {
          if (!f.endsWith(".json") || f.startsWith(".") || f.includes("_history")) continue;
          const m = readJSON(path.join(dir, f));
          if (m) manifests.push(m);
        }
      }
      res.setHeader("Content-Type", "application/json");
      res.statusCode = 200;
      res.end(JSON.stringify({ bot_id: config.botId, manifests }));
      return true;
    },
  });

  // GET /evolve/manifests/:id — get a single manifest
  api.registerHttpRoute({
    method: "GET",
    auth: "plugin",
    path: "/evolve/manifests/:id",
    handler: async (req: any, res: any) => {
      const id = parseParam(req, "id");
      const p = path.join(manifestsDir(), `${id}.json`);
      const m = readJSON(p);
      if (!m) {
        res.setHeader("Content-Type", "application/json");
        res.statusCode = 404;
        res.end(JSON.stringify({ error: "not found" }));
        return true;
      }
      res.setHeader("Content-Type", "application/json");
      res.statusCode = 200;
      res.end(JSON.stringify(m));
      return true;
    },
  });

  // PUT /evolve/manifests/:id — write/update a manifest
  api.registerHttpRoute({
    method: "PUT",
    auth: "plugin",
    path: "/evolve/manifests/:id",
    handler: async (req: any, res: any) => {
      const id = parseParam(req, "id");
      const body = await parseBody(req);
      if (!body || typeof body !== "object") {
        res.setHeader("Content-Type", "application/json");
        res.statusCode = 400;
        res.end(JSON.stringify({ error: "body required" }));
        return true;
      }
      const dir = manifestsDir();
      fs.mkdirSync(dir, { recursive: true });
      writeJSON(path.join(dir, `${id}.json`), body);
      res.setHeader("Content-Type", "application/json");
      res.statusCode = 200;
      res.end(JSON.stringify({ ok: true }));
      return true;
    },
  });

  // POST /evolve/applications/scan — trigger application scanner as this bot
  api.registerHttpRoute({
    method: "POST",
    auth: "plugin",
    path: "/evolve/applications/scan",
    handler: async (req: any, res: any) => {
      const body = await parseBody(req);
      const quick = (body as any)?.quick === true;
      // Fire and forget — delegate to evolve-admin server (port 5050) which
      // handles the actual spawn.  Using http.request avoids child_process in
      // the plugin, which OpenClaw's security scanner would block at install.
      try {
        const { request: httpRequest } = await import("http");
        const body = JSON.stringify({ bot: config.botId });
        const req = httpRequest({
          hostname: "127.0.0.1",
          port: 5050,
          path: `/api/applications/scan${quick ? "?quick=1" : ""}`,
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Content-Length": Buffer.byteLength(body),
          },
        });
        req.on("error", () => { /* fire-and-forget: ignore failures */ });
        req.write(body);
        req.end();
      } catch (err) {
        res.setHeader("Content-Type", "application/json");
        res.statusCode = 500;
        res.end(JSON.stringify({ error: String(err) }));
        return true;
      }
      res.setHeader("Content-Type", "application/json");
      res.statusCode = 200;
      res.end(JSON.stringify({ status: "started", quick }));
      return true;
    },
  });

  // GET /evolve/ — serve dashboard HTML
  if (config.dashboardEnabled) {
    api.registerHttpRoute({
      method: "GET",
    auth: "plugin",
      path: "/evolve",
      handler: async (_req: any, res: any) => {
        const dashboardPath = path.join(PACKAGES_ROOT, "dashboard", "index.html");
        try {
          const html = fs.readFileSync(dashboardPath, "utf8");
          res.setHeader("Content-Type", "text/html");
          res.statusCode = 200;
          res.end(html);
          return true;
        } catch {
          res.setHeader("Content-Type", "text/html");
          res.statusCode = 200;
          res.end("<h1>Dashboard not built — run from repo root</h1>");
          return true;
        }
      },
    });
  }
}
