/**
 * The OpenClaw compatibility contract — one test per assumption.
 *
 * Every check is exercised twice: once against a fake OpenClaw that
 * honours the assumption (expect PASS) and once against a fake that
 * reproduces the behaviour from the 2026-09-07 incident (expect FAIL). A
 * check that only ever passes is not a contract — it is a comment.
 *
 * Incidents: internal/finding-tier-router-self-call-loop-2026-09-07.md,
 * internal/design-oc-upgrade-safety-2026-09-08.md §2 row 3.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/contract.checks.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { CONTRACT_CHECKS, runContract } from "../dist/contract/run.js";
import { looksLikeUnknownCommand, hookHandlerSource } from "../dist/contract/checks.js";
import {
  makeProbe,
  resultFor,
  UNGUARDED_TURN_OBSERVER,
  LATE_GUARD_TURN_OBSERVER,
  DEPLOY_WITHOUT_ACCEPT,
} from "./contract.fakes.mjs";

const OPTS = { evolveVersion: "0.1.0" };

/** Run one check by id against a probe. */
async function check(id, probe) {
  const c = CONTRACT_CHECKS.find((x) => x.id === id);
  assert.ok(c, `no such check: ${id}`);
  return c.run(probe);
}

test("every check names the incident that motivated it", () => {
  for (const c of CONTRACT_CHECKS) {
    assert.match(c.incident, /^internal\/.+\.md$/, `${c.id} must cite an internal/ document`);
    assert.ok(c.title.length > 10, `${c.id} needs an operator-readable title`);
  }
});

test("check ids are unique and kebab-case", () => {
  const ids = CONTRACT_CHECKS.map((c) => c.id);
  assert.equal(new Set(ids).size, ids.length);
  for (const id of ids) assert.match(id, /^[a-z0-9]+(-[a-z0-9]+)*$/);
});

// ── 1. subagent-reentry-guarded ─────────────────────────────────────────────

test("subagent-reentry-guarded passes when the guard precedes the router", async () => {
  const r = await check("subagent-reentry-guarded", makeProbe());
  assert.equal(r.status, "pass");
});

test("subagent-reentry-guarded fails on the 2026-09-07 source (no guard)", async () => {
  const probe = makeProbe({
    sources: { "packages/plugin/src/observer/TurnObserver.ts": UNGUARDED_TURN_OBSERVER },
  });
  const r = await check("subagent-reentry-guarded", probe);
  assert.equal(r.status, "fail");
  assert.match(r.detail, /classifyEvolveSubagentKey/);
});

test("subagent-reentry-guarded fails when the guard runs after the router", async () => {
  const probe = makeProbe({
    sources: { "packages/plugin/src/observer/TurnObserver.ts": LATE_GUARD_TURN_OBSERVER },
  });
  const r = await check("subagent-reentry-guarded", probe);
  assert.equal(r.status, "fail");
  assert.match(r.detail, /AFTER/);
});

test("subagent-reentry-guarded skips when the source is unreadable", async () => {
  const probe = makeProbe({ sources: { "packages/plugin/src/observer/TurnObserver.ts": undefined } });
  const r = await check("subagent-reentry-guarded", probe);
  assert.equal(r.status, "skip");
});

// ── 2. silent-token-stays-silent ────────────────────────────────────────────

test("silent-token-stays-silent passes on a runtime that knows the sentinel", async () => {
  const r = await check("silent-token-stays-silent", makeProbe());
  assert.equal(r.status, "pass");
});

test("silent-token-stays-silent fails when the sentinel is gone", async () => {
  const probe = makeProbe({ symbols: ["isSilentCommentaryProgressText"] });
  const r = await check("silent-token-stays-silent", probe);
  assert.equal(r.status, "fail");
  assert.match(r.detail, /NO_REPLY/);
});

test("silent-token-stays-silent fails when room_event inbound is rejected", async () => {
  const probe = makeProbe({
    validation: { ok: false, messages: ["messages.groupChat.unmentionedInbound: invalid value 'room_event'"] },
  });
  const r = await check("silent-token-stays-silent", probe);
  assert.equal(r.status, "fail");
  assert.match(r.detail, /room_event/);
});

// ── 3. evolve-config-keys-accepted ──────────────────────────────────────────

test("evolve-config-keys-accepted passes on a clean validator", async () => {
  const r = await check("evolve-config-keys-accepted", makeProbe());
  assert.equal(r.status, "pass");
});

test("evolve-config-keys-accepted fails and names each retired key", async () => {
  const probe = makeProbe({
    validation: {
      ok: false,
      messages: [
        "Removed retired agents.entries.*.default markers",
        "tools.exec.ask: unknown property",
        "plugins.load.paths: retired, use plugins.entries",
      ],
    },
  });
  const r = await check("evolve-config-keys-accepted", probe);
  assert.equal(r.status, "fail");
  assert.match(r.detail, /tools\.exec\.ask/);
  assert.match(r.detail, /plugins\.load\.paths/);
});

test("evolve-config-keys-accepted names the key even when the validator pretty-prints", async () => {
  // The target emits JSON, so the offending path and its message are on
  // DIFFERENT lines. The first real CI run failed here with an empty flagged
  // list and only a generic "exited non-zero" to show the operator.
  const probe = makeProbe({
    validation: {
      ok: false,
      messages: [],
      raw: [
        "{",
        '  "valid": false,',
        '  "issues": [',
        "    {",
        '      "path": "tools.exec.security",',
        '      "message": "Invalid option: expected one of \\"deny\\"|\\"allowlist\\"|\\"full\\""',
        "    }",
        "  ]",
        "}",
      ].join("\n"),
    },
  });
  const r = await check("evolve-config-keys-accepted", probe);
  assert.equal(r.status, "fail");
  assert.match(r.detail, /tools\.exec\.security/);
});

test("evolve-config-keys-accepted stages the config Evolve actually writes", async () => {
  const probe = makeProbe();
  await check("evolve-config-keys-accepted", probe);
  const cfg = probe.staged.at(-1);
  assert.equal(cfg.plugins.entries.evolve.enabled, true);
  assert.equal(cfg.messages.groupChat.unmentionedInbound, "room_event");
  // Values, not just keys: an invented enum value is a false red forever.
  // deploy.py's _infer_exec_policy writes deny | allowlist | full.
  assert.ok(["deny", "allowlist", "full"].includes(cfg.tools.exec.security));
  // plugins.load.paths points at a directory the PROBE owns. The target
  // validates that the path exists, and the first real CI run reported the
  // pod's own plugin dir being absent on a runner as a retired key.
  assert.match(cfg.plugins.load.paths[0], /evolve-plugin$/);
  assert.ok(!cfg.plugins.load.paths[0].startsWith("/Users/Shared"));
});

// ── 4. before-agent-reply-shortcircuit ──────────────────────────────────────

test("before-agent-reply-shortcircuit passes when both sides register it", async () => {
  const r = await check("before-agent-reply-shortcircuit", makeProbe());
  assert.equal(r.status, "pass");
});

test("before-agent-reply-shortcircuit fails when the target stops dispatching it", async () => {
  const probe = makeProbe({ symbols: ["NO_REPLY", "before_agent_reply"] });
  const r = await check("before-agent-reply-shortcircuit", probe);
  assert.equal(r.status, "fail");
  assert.match(r.detail, /second, model-authored bubble/);
});

// ── 5. hook-ctx-fields-present ──────────────────────────────────────────────

test("hook-ctx-fields-present passes when every field is named", async () => {
  const r = await check("hook-ctx-fields-present", makeProbe());
  assert.equal(r.status, "pass");
});

test("hook-ctx-fields-present fails and names the missing field", async () => {
  const probe = makeProbe({ symbols: ["sessionKey", "runId", "trigger", "modelId"] });
  const r = await check("hook-ctx-fields-present", probe);
  assert.equal(r.status, "fail");
  assert.match(r.detail, /workspaceDir/);
});

test("hook-ctx-fields-present skips when the package cannot be read", async () => {
  const r = await check("hook-ctx-fields-present", makeProbe({ runtimeAvailable: false }));
  assert.equal(r.status, "skip");
});

// ── 6. plugin-install-flags-accepted ────────────────────────────────────────

test("plugin-install-flags-accepted passes when the flags line up both ways", async () => {
  const r = await check("plugin-install-flags-accepted", makeProbe());
  assert.equal(r.status, "pass");
});

test("plugin-install-flags-accepted fails when the target drops a flag Evolve passes", async () => {
  const probe = makeProbe({
    cli: { "plugins install --help": { code: 0, stdout: "Usage:\n  -l, --local <dir>\n  --accept-capabilities\n", stderr: "" } },
  });
  const r = await check("plugin-install-flags-accepted", probe);
  assert.equal(r.status, "fail");
  assert.match(r.detail, /--force/);
});

test("plugin-install-flags-accepted fails when deploy.py omits --accept-capabilities", async () => {
  const probe = makeProbe({
    sources: { "packages/admin/evolve_admin/deploy.py": DEPLOY_WITHOUT_ACCEPT },
  });
  const r = await check("plugin-install-flags-accepted", probe);
  assert.equal(r.status, "fail");
  assert.match(r.detail, /piped `y` is not consent/);
});

// ── 7. doctor-preserves-model-refs ──────────────────────────────────────────

test("doctor-preserves-model-refs passes on an empty change plan", async () => {
  const r = await check("doctor-preserves-model-refs", makeProbe());
  assert.equal(r.status, "pass");
});

test("doctor-preserves-model-refs fails when doctor plans a model rewrite", async () => {
  const probe = makeProbe({
    cli: {
      "doctor --json": {
        code: 0,
        stdout: '{"changes":[{"path":"agents.defaults.model.primary","from":"a","to":"b"}]}',
        stderr: "",
      },
    },
  });
  const r = await check("doctor-preserves-model-refs", probe);
  assert.equal(r.status, "fail");
  assert.match(r.detail, /which model answers/);
});

test("doctor-preserves-model-refs never passes a write flag", async () => {
  const seen = [];
  const base = makeProbe();
  const probe = { ...base, cli: async (args) => { seen.push(args); return base.cli(args); } };
  await check("doctor-preserves-model-refs", probe);
  for (const args of seen) assert.ok(!args.includes("--fix"), `contract invoked doctor with --fix: ${args}`);
});

// ── 8. channel-plugin-versions-match ────────────────────────────────────────

test("channel-plugin-versions-match passes on an empty, parseable inventory", async () => {
  const r = await check("channel-plugin-versions-match", makeProbe());
  assert.equal(r.status, "pass");
});

test("channel-plugin-versions-match fails on a package off the runtime line", async () => {
  const probe = makeProbe({
    cli: {
      "plugins list --json": {
        code: 0,
        stdout: '[{"id":"@openclaw/slack","version":"2026.7.1"}]',
        stderr: "",
      },
    },
  });
  const r = await check("channel-plugin-versions-match", probe);
  assert.equal(r.status, "fail");
  assert.match(r.detail, /@openclaw\/slack@2026\.7\.1/);
});

test("channel-plugin-versions-match fails when the inventory surface is gone", async () => {
  const probe = makeProbe({
    cli: { "plugins list --json": { code: 1, stdout: "", stderr: "error: unknown command 'list'" } },
  });
  const r = await check("channel-plugin-versions-match", probe);
  assert.equal(r.status, "fail");
});

// ── 9. gateway-status-deep-surface ──────────────────────────────────────────

test("gateway-status-deep-surface passes on a 'not running' answer", async () => {
  const r = await check("gateway-status-deep-surface", makeProbe());
  assert.equal(r.status, "pass");
});

test("gateway-status-deep-surface fails when the subcommand is gone", async () => {
  const probe = makeProbe({
    cli: { "gateway status --deep --json": { code: 1, stdout: "", stderr: "error: unrecognized option '--deep'" } },
  });
  const r = await check("gateway-status-deep-surface", probe);
  assert.equal(r.status, "fail");
});

// ── helpers ─────────────────────────────────────────────────────────────────

test("looksLikeUnknownCommand distinguishes a missing surface from a normal answer", () => {
  assert.equal(looksLikeUnknownCommand({ code: 1, stdout: "", stderr: "unknown command 'gateway'" }), true);
  assert.equal(looksLikeUnknownCommand({ code: 3, stdout: '{"running":false}', stderr: "" }), false);
  assert.equal(looksLikeUnknownCommand({ code: 1, stdout: "", stderr: "gateway is not running" }), false);
});

test("hookHandlerSource slices one registration, not the next", () => {
  const slice = hookHandlerSource(
    'api.on("before_model_resolve", a);\napi.on("llm_output", b);',
    "before_model_resolve",
  );
  assert.ok(slice.includes("before_model_resolve"));
  assert.ok(!slice.includes("llm_output"));
  assert.equal(hookHandlerSource("nothing here", "before_model_resolve"), null);
});

// ── the run as a whole ──────────────────────────────────────────────────────

test("a healthy runtime passes the whole contract", async () => {
  const run = await runContract(makeProbe(), OPTS);
  assert.equal(run.ok, true, `failing: ${JSON.stringify(run.results.filter((r) => r.status !== "pass"), null, 2)}`);
  assert.equal(run.results.length, CONTRACT_CHECKS.length);
  assert.equal(run.ocVersion, "2026.9.2");
});

test("the 2026-09-07 incident runtime fails the contract on the router guard", async () => {
  const probe = makeProbe({
    sources: { "packages/plugin/src/observer/TurnObserver.ts": UNGUARDED_TURN_OBSERVER },
  });
  const run = await runContract(probe, OPTS);
  assert.equal(run.ok, false);
  assert.equal(resultFor(run, "subagent-reentry-guarded").status, "fail");
});
