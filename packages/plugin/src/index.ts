/**
 * Evolve — OpenClaw RSI Plugin
 * Entry point
 *
 * Registers:
 *  - Turn observer hooks (annotate every turn with quality signals)
 *  - HTTP routes (status, metrics, dashboard)
 */

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { EvolveConfig, resolveConfig } from "./config.js";
import { TurnObserver } from "./observer/TurnObserver.js";
import { registerApiRoutes } from "./api/routes.js";
import { createDeferToolFactory } from "./tools/DeferTool.js";
import { createRecordApplicationToolFactory } from "./tools/RecordApplicationTool.js";
import { createSetTierToolFactory } from "./tools/SetTierTool.js";
import {
  createHelpReadToolFactory,
  createHelpSearchToolFactory,
  createSubmitIntakeToolFactory,
} from "./tools/PrimaryBotTools.js";
import { createPodStateToolFactory } from "./tools/PodStateTools.js";
import {
  createRosterSetRoleToolFactory,
  createRosterBlockToolFactory,
  createRosterUnblockToolFactory,
  createChannelSetNewcomerModeToolFactory,
} from "./tools/RosterTools.js";
import { adminDaemonSocketPath } from "./util/adminSocket.js";
import {
  createGoogleToolFactories,
  googleConfiguredForBot,
} from "./tools/GoogleTools.js";
import {
  createDirectoryLookupToolFactory,
  createDirectoryUpsertToolFactory,
} from "./tools/DirectoryTools.js";
import { registerAppIntegrityMiddleware } from "./integrity/AppIntegrityMiddleware.js";
import { configureAppAttribution } from "./apps/AppAttribution.js";
import { registerToolCallGate } from "./integrity/ToolCallGate.js";
import { ToolFootprint } from "./observer/ToolFootprint.js";
import { createExpandAppToolFactory } from "./tools/ExpandAppTool.js";

// Guards against register() being invoked twice on the *same* api instance.
//
// History:
//   - 2026-04-21 (bug 002): OC invoked the entry twice ~1s apart at startup,
//     doubling observer hooks, collector service, and HTTP routes. Original
//     fix was a module-level `_registered` boolean that skipped the 2nd call.
//   - 2026-05-03: OC 2026.4.29 evidently calls `register()` periodically (or
//     per-session) on *new* api instances over the lifetime of the process —
//     admin_bot's gateway log shows "Evolve register() called twice" appearing
//     ~4 minutes after each fresh start. The global boolean meant every
//     subsequent api instance had its hooks SKIPPED, so no plugin handlers
//     ran for channel-driven turns. Embedded-run traces showed `hooks:0ms`,
//     and zero `BetterEngine` / `Evolve diag` lines fired post-startup.
//
// New approach: track which api objects have already been registered using a
// WeakSet. Same-instance double-init still no-ops (preserves the April fix);
// new-instance registrations attach hooks correctly (fixes the May regression).
const _registeredApis = new WeakSet<object>();

export default definePluginEntry({
  id: "evolve",
  name: "Evolve",
  description: "Pod-wide adaptation plugin for OpenClaw bot networks",

  register(api) {
    const config: EvolveConfig = resolveConfig(api.pluginConfig, api.config);

    if (_registeredApis.has(api as object)) {
      api.logger.warn(
        `Evolve register() called twice on same api instance — skipping duplicate init (bot=${config.botId})`
      );
      return;
    }
    _registeredApis.add(api as object);

    api.logger.info(
      `Evolve starting — bot=${config.botId} role=${config.role} network=${config.networkId} tier=${config.tier}`
    );

    // ── Tier gate ─────────────────────────────────────────────────────────────
    // tier=off opts a bot out of plugin-side integration entirely. The bot is
    // still visible to pod-level monitoring (gateway health, repo puller,
    // analyzer daemons that read external state), but no Evolve hook fires
    // inside its process and no plugin tool is registered.
    if (!config.capabilities.observer) {
      api.logger.info(
        `Evolve: tier=${config.tier} — skipping plugin-side integration for ${config.botId}`
      );
      return;
    }

    // ── Tool footprint (overhead-budget A1/B2) ────────────────────────────────
    // Transparently wraps api.registerTool so every registered tool's schema
    // weight is recorded, then flush() at the end of init writes
    // {shared}/{bot}/context-footprint.json — the per-bot ground truth that
    // reconciles static factory weights with measured per-tier prefixes.
    const toolFootprint = new ToolFootprint({
      sharedDir: config.sharedDir, botId: config.botId,
      tier: config.tier, logger: api.logger,
    });
    toolFootprint.wrap(api);

    // ── Turn observer ─────────────────────────────────────────────────────────
    // Hooks into every completed turn and writes a structured annotation
    // alongside the standard turns log. Capture-side hooks (llm_output,
    // agent_end, session_end) always register at tier ≥ monitor; injection
    // sites (session_start, before_model_resolve, before_agent_run) self-gate
    // on capabilities inside TurnObserver.
    const observer = new TurnObserver(config, api.logger, api);
    observer.register(api);

    // ── App attribution registry (AL-1.1) ─────────────────────────────────────
    // Wires the per-bot conflict-ledger destination + logger for the
    // run/session-scoped attribution stash (apps/AppAttribution.ts). The three
    // explicit sources (expand_app, script-integrity middleware, Layer C
    // intercept) record into it; TurnObserver resolves it into the turn
    // annotation's app_* fields. Read-only observation — nothing here can
    // block a tool call or throw into a turn.
    configureAppAttribution(
      { sharedDir: config.sharedDir, botId: config.botId }, api.logger,
    );

    // ── HTTP routes ───────────────────────────────────────────────────────────
    // /evolve/status  — health check, current session scores
    // /evolve/metrics — structured metrics for the analyzer
    // /evolve/        — dashboard (primary only)
    registerApiRoutes(api, config);

    // ── App execution-integrity harness (just-works §2.2) ─────────────────────
    // An awaited, pre-model tool-result middleware: when a tool call ran a
    // KNOWN app's script, it substitutes an HONEST structured outcome (clean
    // failure message on non-zero exit; preserved output on success) before the
    // model can confabulate one. Pure passthrough for every non-app tool
    // result. Registered for every active-tier bot — it's a fleet-wide
    // integrity guarantee, not a role/feature surface. No-ops on a gateway too
    // old to expose the middleware seam (needs OpenClaw v2026.6.6+).
    // Spec: docs/spec-app-invocation-just-works-2026-06-29.md §2.2.
    registerAppIntegrityMiddleware(api, config, api.logger);

    // ── Layer-2 per-role tool-call gate (below-LLM authorization) ─────────────
    // Registers OpenClaw's before_tool_call hook (fleet OC v2026.6.11): per tool
    // call, resolves the SPEAKER's role for the turn and BLOCKS the call when
    // their capabilities don't permit that tool. Enforced below the LLM, so a
    // jailbreak / prompt-injection cannot invoke a tool the speaker's role
    // forbids. Covers sensitive plugin tools (roster_*, channel config, outbound
    // Google writes) AND gateway built-ins (bash/exec/apply_patch/file-write).
    //
    // Fail-CLOSED: an unresolved speaker or any evaluation error blocks a gated
    // tool (in enforce mode). Ungated / read tools always pass.
    //
    // OBSERVE-ONLY by default (config.layer2Enforce=false): computes + records
    // every would-block but always allows, so an operator can see enforcement's
    // effect before arming it via network.json ``layer2.enforce: true``.
    // Merging is non-arming. Registered for every active-tier bot — enforcement
    // authorization is a fleet-wide guarantee, not a role/feature surface.
    // Design: docs/design-layer2-tool-loading-filter-2026-07-16.md.
    registerToolCallGate(api, config, api.logger);

    // ── Defer tool (Continuity Engine v2) ─────────────────────────────────────
    // Bots call defer() when they commit to acting later. Tool appends a row
    // to the bot's defer-queue.jsonl; defer_runner picks up due rows and
    // dispatches an agent turn to deliver the follow-up. tier=manage and
    // below skip this — the bot won't have the tool in its toolset, so it
    // can't make time-deferred commitments in the first place.
    if (config.capabilities.deferTool) {
      api.registerTool(createDeferToolFactory({ botId: config.botId }, api.logger));
    }

    // ── session.set_tier MCP tool (cascade Phase 2; spec § 4.1) ──────────────
    // Bots call this when the user explicitly asks for more or less
    // reasoning capability. Counterpart to the admin-UI tier chip
    // (PR #1629) for messaging-app surfaces. Calls into the SAME
    // ModelRouter.setUserTier primitive — no parallel mechanism.
    // Gated on modelRouting capability since it only matters when
    // routing is in play.
    if (config.capabilities.modelRouting) {
      api.registerTool(
        createSetTierToolFactory(
          { botId: config.botId, modelRouter: observer.getModelRouter() },
          api.logger,
        ),
      );
    }

    // ── Record-application tool (Manifest Reflex) ─────────────────────────────
    // Bots call record_application() in the same turn they ship something
    // app-shaped (script, cron, tracker). Tool appends a row to the bot's
    // manifest-reflex-queue.jsonl; the manifest_reflex_runner picks up rows
    // and lands real ApplicationManifests under {shared_dir}/applications/.
    // tier=monitor and below skip — those bots aren't expected to author
    // new apps and we don't want extra tool surface there.
    if (config.capabilities.recordApplicationTool) {
      api.registerTool(
        createRecordApplicationToolFactory({ botId: config.botId }, api.logger),
      );
    }

    // ── Primary-bot interface tools ───────────────────────────────────────────
    // Bundle 2B of docs/spec-primary-bot-interface-2026-05-14.md. Only the
    // primary bot gets these — member bots don't talk to admin about Evolve
    // internals and shouldn't have an intake / help surface.
    //
    //   evolve_help_search — BM25 over the help index built by Bundle 2A.
    //   evolve_help_read   — full markdown body of one help doc.
    //   submit_intake      — capture (and optionally promote-to-GitHub) a
    //                        bug / feature / question filed against Evolve.
    //
    // All three call back into the admin daemon over its UNIX SOCKET (not
    // loopback TCP :5050): admin auth is ON by default (#2621), so a cookieless
    // TCP RPC 401s; the unix socket is exempted + peer-uid bound server-side
    // (#3265 / #3263 / #3267). The socket path is platform-keyed off the
    // resolved sharedDir (/Users/Shared/evolve on macOS, /var/lib/evolve on
    // Linux) — the macOS-shaped default never resolves on a Linux pod. Gating
    // on role === "primary" mirrors how getBetterSurface() decides the
    // recommendations surface.
    const adminSocketPath = adminDaemonSocketPath(config.sharedDir);
    if (config.role === "primary") {
      api.registerTool(createHelpSearchToolFactory(api.logger, adminSocketPath));
      api.registerTool(createHelpReadToolFactory(api.logger, adminSocketPath));
      api.registerTool(
        createSubmitIntakeToolFactory(
          { botId: config.botId, socketPath: adminSocketPath }, api.logger,
        ),
      );

      // ── Pod-state read tool (Bundle 3, consolidated by overhead-budget
      // B2 v2) ────────────────────────────────────────────────────────────────
      // ONE query-enum tool replacing the eight per-endpoint read tools —
      // eight schemas rode in every primary prompt for what is a single
      // read surface. Grounded reads against {shared_dir}/* via
      // /api/primary/state/*, over the same admin-daemon unix socket.
      // Spec: docs/spec-primary-bot-interface-2026-05-14.md §5;
      // docs/spec-evolve-overhead-budget-2026-07-31.md §B2.
      api.registerTool(createPodStateToolFactory(api.logger, adminSocketPath));
    }

    // ── Roster admin tools (Phase C.2 / Path B) ────────────────────────────
    // Spec: docs/spec-user-roster-and-roles-2026-06-07.md §10.
    //
    // Lets a primary_user (or pod admin) chatting with their bot say things
    // like "block @alice" and have the bot mutate the roster. Registered
    // unconditionally on every bot — the admin daemon's X-Requester-Identity
    // capability check (Phase C.1) decides whether to apply or refuse.
    //
    // DM-only in v1: extracting the sender id from group sessionKeys requires
    // a separate plumbing path (group chat_id != user_id). Group admin still
    // works via Path C (evo cross-bot) shipped in Phase C.1.
    //
    // Reuses ``adminSocketPath`` (computed above) — the same platform-keyed
    // admin-daemon socket the primary-bot tools use.
    api.registerTool(
      createRosterSetRoleToolFactory(
        { botId: config.botId, socketPath: adminSocketPath }, api.logger,
      ),
    );
    api.registerTool(
      createRosterBlockToolFactory(
        { botId: config.botId, socketPath: adminSocketPath }, api.logger,
      ),
    );
    api.registerTool(
      createRosterUnblockToolFactory(
        { botId: config.botId, socketPath: adminSocketPath }, api.logger,
      ),
    );
    api.registerTool(
      createChannelSetNewcomerModeToolFactory(
        { botId: config.botId, socketPath: adminSocketPath }, api.logger,
      ),
    );

    // ── Directory lookup tool (user-directory Phase 3a) ─────────────────────
    // Spec: docs/spec-user-directory-2026-06-22.md §5 item 1.
    //
    // Lets the bot resolve one of its OWN Persons — admitted users AND saved
    // contacts — by @handle/name/email/id, so it stops hand-typing IDs/emails.
    // Generalizes the charter's planned roster_lookup (a roster user is a Person
    // with membership; a contact is one without). Registered unconditionally
    // (like RosterTools): the server binds the bot from the socket peer uid and
    // resolves through resolve_person — the one read path — so the lookup is
    // scoped to this bot's directory by construction and the tool carries no
    // privilege of its own. The private behavioral profile is never exposed.
    api.registerTool(
      createDirectoryLookupToolFactory(
        { sharedDir: config.sharedDir, botId: config.botId }, api.logger,
      ),
    );

    // ── Directory upsert tool (user-directory Phase 3b — the WRITE half) ─────
    // Spec: docs/spec-user-directory-2026-06-22.md §5 item 2.
    //
    // Lets the bot RECORD a person's identity + contact data (emails, messaging
    // IDs, names, contact attrs) into the canonical store instead of its USER.md
    // prose (the §0 address-book bug). The server stamps every write
    // provenance:"bot-asserted" (hardcoded, never from the body), refuses any
    // authority field (role/membership/admission/capabilities/profile_ref), and
    // never lets a bot-asserted write clobber an operator-verified one — so the
    // bot asserts WHO someone is and HOW to reach them, never WHAT they may do
    // (invariants #2/#3). Same peer-uid binding as the lookup: a write can only
    // ever land in the caller's own directory.
    api.registerTool(
      createDirectoryUpsertToolFactory(
        { sharedDir: config.sharedDir, botId: config.botId }, api.logger,
      ),
    );

    // ── App capability index: expand_app (Tier-2 on-demand disclosure) ──────
    // Spec: docs/spec-app-invocation-just-works-2026-06-29.md §2.1 (recognition).
    //
    // The bot's AGENTS.md carries an always-on Tier-1 menu (one terse line per
    // installed app, each ending → expand_app("<id>")); this tool is the Tier-2
    // half — it pulls one app's full command surface into context ON DEMAND when
    // the model engages that app, so the heavy per-app detail never sits in every
    // turn's budget. Read-only: it reveals how to use an app, never runs one and
    // never forces a choice (the "just works, not rigid" contract).
    //
    // Registered unconditionally (like DirectoryTools) — the server is the security
    // boundary: it binds the acting bot from the socket peer uid and scopes the
    // lookup to that bot's own apps, so the tool carries no privilege of its own.
    // Reuses ``adminSocketPath`` (computed above) — the same platform-keyed
    // admin-daemon socket the other bot tools use.
    api.registerTool(
      createExpandAppToolFactory(
        { sharedDir: config.sharedDir, botId: config.botId, socketPath: adminSocketPath },
        api.logger,
      ),
    );

    // ── Curated Google tools (Gmail / Calendar / Drive) ─────────────────────
    // Spec: docs/spec-google-integration-architecture-2026-06-20.md (P1).
    //
    // Gated PER-BOT on a configured google_integration (the wizard is the
    // opt-in) — NOT tier-gated, NOT fleet-wide. A bot with no Google config
    // registers ZERO Google tools (least-privilege by construction). The gate
    // reads the bot's mode synchronously from network.json so registration
    // stays synchronous (OC collects the toolset at session start).
    //
    // Each tool proxies to the admin daemon's /api/google/call over the unix
    // socket; the SERVER binds the acting bot from the socket peer uid (never
    // a request field) and re-checks the config — so this gate is visibility
    // only, and a bot can act only as itself. Credentials never reach the bot.
    const googleMode = googleConfiguredForBot(config.sharedDir, config.botId);
    if (googleMode) {
      const factories = createGoogleToolFactories(
        { sharedDir: config.sharedDir, botId: config.botId }, api.logger,
      );
      for (const factory of factories) {
        api.registerTool(factory);
      }
      api.logger.info(
        `Evolve: registered ${factories.length} Google tools for ` +
        `${config.botId} (mode=${googleMode})`,
      );
    }

    // Registration complete — write the per-bot tool footprint (never throws).
    toolFootprint.flush();
  },
});
