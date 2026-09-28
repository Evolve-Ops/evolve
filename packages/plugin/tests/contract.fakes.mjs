/**
 * Fake OcProbes for the contract tests.
 *
 * `healthyProbe()` behaves like an OpenClaw that honours every assumption
 * Evolve makes. `incidentProbe(overrides)` starts from that and reproduces
 * the 2026-09-07 breakages one at a time, so each contract check can be
 * shown to FAIL on the behaviour it exists to catch — a check that only
 * ever passes proves nothing.
 *
 * Not a `.test.mjs` file on purpose: node's default test discovery would
 * pick it up and report a suite with no tests.
 */

export const HEALTHY_TURN_OBSERVER = `
    api.on("before_model_resolve", async (event, ctx) => {
      const sessionKey = ctx.sessionKey;
      if (classifyEvolveSubagentKey(sessionKey) !== null) return {};
      await this.handleBeforeModelResolve(event, ctx, preflightRouter);
    });
    api.on("before_agent_reply", async (_event, ctx) => this.handleBeforeAgentReply(ctx?.runId));
`;

/** The source as it stood on 2026-09-07: no guard before the router. */
export const UNGUARDED_TURN_OBSERVER = `
    api.on("before_model_resolve", async (event, ctx) => {
      const sessionKey = ctx.sessionKey;
      await this.handleBeforeModelResolve(event, ctx, preflightRouter);
    });
    api.on("before_agent_reply", async (_event, ctx) => this.handleBeforeAgentReply(ctx?.runId));
`;

/** The guard present, but after the routing call it is meant to prevent. */
export const LATE_GUARD_TURN_OBSERVER = `
    api.on("before_model_resolve", async (event, ctx) => {
      await this.handleBeforeModelResolve(event, ctx, preflightRouter);
      if (classifyEvolveSubagentKey(ctx.sessionKey) !== null) return {};
    });
`;

export const DEPLOY_WITH_ACCEPT = `
    cmd = ["sudo", "-H", "-u", bot_user, _openclaw_bin(), "plugins", "install",
           "-l", str(PLUGIN_INSTALL_DIR), "--accept-capabilities"]
`;

export const DEPLOY_WITHOUT_ACCEPT = `
    cmd = ["sudo", "-H", "-u", bot_user, _openclaw_bin(), "plugins", "install",
           "-l", str(PLUGIN_INSTALL_DIR)]
`;

const HEALTHY_RUNTIME_SYMBOLS = [
  "NO_REPLY",
  "isSilentCommentaryProgressText",
  "before_agent_reply",
  "runBeforeAgentReply",
  "createGatewaySubagentRuntime",
  "sessionKey",
  "runId",
  "trigger",
  "workspaceDir",
  "modelId",
];

const HEALTHY_CLI = {
  "plugins install --help": {
    code: 0,
    stdout: "Usage: openclaw plugins install [options]\n  -l, --local <dir>\n  --force\n  --accept-capabilities\n",
    stderr: "",
  },
  "doctor --json": { code: 0, stdout: '{"changes":[]}', stderr: "" },
  "plugins list --json": { code: 0, stdout: "[]", stderr: "" },
  "gateway status --deep --json": { code: 3, stdout: '{"running":false}', stderr: "" },
};

const UNKNOWN_COMMAND = { code: 1, stdout: "", stderr: "error: unknown command 'x'" };

/**
 * Build a fake probe.
 *
 * @param {object} o
 * @param {string} [o.version]
 * @param {string[]} [o.symbols]        runtime symbols the fake "ships"
 * @param {object} [o.cli]              cli-key -> {code,stdout,stderr}
 * @param {object} [o.sources]          repo-relative path -> text (null to omit)
 * @param {object} [o.validation]       {ok, messages}
 * @param {boolean} [o.runtimeAvailable]
 * @param {boolean} [o.cliAvailable]
 */
export function makeProbe(o = {}) {
  const symbols = o.symbols ?? HEALTHY_RUNTIME_SYMBOLS;
  const cli = { ...HEALTHY_CLI, ...(o.cli ?? {}) };
  const sources = {
    "packages/plugin/src/observer/TurnObserver.ts": HEALTHY_TURN_OBSERVER,
    "packages/admin/evolve_admin/deploy.py": DEPLOY_WITH_ACCEPT,
    ...(o.sources ?? {}),
  };
  const validation = o.validation ?? { ok: true, messages: [] };
  const staged = [];
  return {
    staged,
    async version() {
      return o.version ?? "2026.9.2";
    },
    async cliAvailable() {
      return o.cliAvailable ?? true;
    },
    async cli(args) {
      const key = args.join(" ");
      return cli[key] ?? UNKNOWN_COMMAND;
    },
    async scratchDir(name) {
      return `/tmp/evolve-oc-contract-fake/${name}`;
    },
    async stageConfig(config) {
      staged.push(config);
    },
    async validateConfig(config) {
      staged.push(config);
      return {
        ok: validation.ok,
        messages: validation.messages,
        raw: validation.raw ?? validation.messages.join("\n"),
      };
    },
    async runtimeAvailable() {
      return o.runtimeAvailable ?? true;
    },
    async runtimeMentions(needle) {
      return symbols.includes(needle);
    },
    async evolveSource(relPath) {
      const v = sources[relPath];
      return v === undefined ? null : v;
    },
  };
}

/** Find one check's result in a run. */
export function resultFor(run, id) {
  const r = run.results.find((x) => x.id === id);
  if (!r) throw new Error(`no result for check ${id}`);
  return r;
}
