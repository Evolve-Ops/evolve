/**
 * The Evolve-authored surfaces the contract asserts against a target
 * OpenClaw. Each constant is a claim about what Evolve WRITES or PASSES —
 * change Evolve's behaviour and you change these, deliberately, in the same
 * PR.
 */

/**
 * The `openclaw.json` key set Evolve authors on every bot.
 *
 * Mirrors what ``deploy.ensure_plugin_config`` writes (packages/admin/
 * evolve_admin/deploy.py): the agent model block, the plugin entry and its
 * subagent/hooks grants, the local plugin load path, the gateway block, the
 * exec-security block, and the group-chat inbound mode. The 2026-09-07
 * upgrade invalidated every bot's config at once because four of these keys
 * had been retired without Evolve noticing; validating this literal against
 * a candidate is the check that would have said so first.
 *
 * It is deliberately a whole config, not a key list: `config validate`
 * answers about a document, and a key that is only invalid *in context*
 * (a retired marker under an `agents.entries` shape, say) is exactly the
 * class that broke.
 *
 * `pluginLoadPath` is injected rather than hardcoded because the target
 * validates that the path EXISTS. The first real CI run pointed it at the
 * pod's `/Users/Shared/evolve/plugin`, got "plugin path not found" back, and
 * reported it as a retired key — an environmental fact wearing a
 * compatibility verdict's clothes. Callers pass a directory the probe owns.
 */
export function evolveAuthoredConfig(pluginLoadPath: string): Record<string, unknown> {
  return {
    agents: {
      defaults: {
        model: { primary: "anthropic/claude-sonnet-5", fallbacks: [] },
        thinkingDefault: "off",
      },
    },
    gateway: {
      mode: "local",
      bind: "loopback",
      auth: { mode: "token", token: "0".repeat(64) },
      trustedProxies: [],
    },
    // `security` is one of deny | allowlist | full — `_infer_exec_policy` picks
    // per bot, defaulting to "full" since the 2026-05-25 pivot; `ask` is
    // "on-miss" for every non-deny posture. Written as the target's validator
    // spells them: the contract's first real run rejected an invented
    // "approval" here, which would have been a false red forever.
    tools: {
      exec: { security: "full", ask: "on-miss" },
    },
    // The silent-reply half of the 2026-09-07 incident: a bare NO_REPLY only
    // stays silent when unmentioned group inbound is delivered as a room event.
    messages: {
      groupChat: { unmentionedInbound: "room_event" },
    },
    plugins: {
      load: { paths: [pluginLoadPath] },
      entries: {
        evolve: {
          enabled: true,
          config: { dashboardEnabled: false },
          subagent: { allowModelOverride: true },
          hooks: { allowConversationAccess: true },
        },
      },
    },
  };
}

/**
 * The top-level keys the config above claims. A validator complaint that
 * names one of these is a contract failure Evolve owns; a complaint about
 * anything else is the operator's config, not Evolve's.
 */
export const EVOLVE_AUTHORED_KEYS: readonly string[] = [
  "agents.defaults.model",
  "agents.defaults.thinkingDefault",
  "gateway.mode",
  "gateway.bind",
  "gateway.auth",
  "gateway.trustedProxies",
  "tools.exec.security",
  "tools.exec.ask",
  "messages.groupChat.unmentionedInbound",
  "plugins.load.paths",
  "plugins.entries.evolve",
];

/**
 * The flags Evolve passes to ``openclaw plugins install``.
 *
 * ``-l`` is the local-directory install the deploy uses
 * (deploy._install_plugin_for_bot); ``--force`` is the channel-package
 * install (oc_neutralize.install_externalized_plugin);
 * ``--accept-capabilities`` became mandatory for a NON-INTERACTIVE install
 * in 2026.9 — a piped `y` is not consent, and the deploy silently skipped
 * the plugin on all nine bots because of it (incident item 7f).
 */
export const EVOLVE_PLUGIN_INSTALL_FLAGS: readonly string[] = [
  "-l",
  "--force",
  "--accept-capabilities",
];

/**
 * Hook-context fields the plugin reads. `TurnObserver` degrades silently
 * when one disappears — the 2026.4.29 two-arg change made `sessionKey`
 * undefined on every channel turn and hid the whole Better Engine path for
 * ~12 days before anyone noticed.
 */
export const REQUIRED_HOOK_CTX_FIELDS: readonly string[] = [
  "sessionKey",
  "runId",
  "trigger",
  "workspaceDir",
  "modelId",
];
