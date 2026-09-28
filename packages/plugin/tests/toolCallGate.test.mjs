/**
 * Tests for the Layer-2 before_tool_call gate — codebase-review 1.1 + the
 * adversarial-review hardening (2026-07-17).
 *
 * Drives the handler produced by makeBeforeToolCallHandler with synthetic
 * before_tool_call events, exercising the fail-CLOSED decision table in BOTH
 * observe (default) and enforce modes, PLUS the hardening the review flagged:
 *
 *   1. Under-privileged speaker (participant, []) calling a gated tool →
 *      enforce ⇒ { block: true }; observe ⇒ void (would-block recorded).
 *   2. Privileged speaker (admin / has the capability) → allowed (void) in both.
 *   3. UNRESOLVED runId (no captured sender) → gated tool DENIED in enforce.
 *   4. Known-SAFE tool (defer / a read tool) → always allowed regardless of role.
 *   5. Gateway built-in (bash / apply_patch) → default-denied to bot.code.modify.
 *   6. DEFAULT-DENY: an unknown exec-shaped tool → blocked for participant when
 *      armed, allowed for admin (the name-denylist→allowlist inversion).
 *   7. MCP-namespaced names normalize (mcp__x__bash, mcp__gmail__send).
 *   8. Case variants (Bash / BASH) still classify (fail-closed via default-deny).
 *   9. The exception path fail-CLOSES ({block:true}) when evaluation throws.
 *  10. Non-boolean enforce stays observe-only (fail-safe, loud warning).
 *  11. Platform fail-closed: a null-platform sender does NOT fall back to the
 *      telegram id-space — it denies as participant.
 *  12. Version gate + first-fire liveness latch.
 *  13. Sender capture works at tier=monitor (armed-on-non-full fix).
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/toolCallGate.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  makeBeforeToolCallHandler,
  requiredCapabilityFor,
  classifyTool,
  normalizeToolName,
  evaluateGatedCall,
  SENSITIVE_CAPABILITY_BY_TOOL,
  KNOWN_SAFE_TOOLS,
  detectOcVersion,
  enforceAllowedByVersion,
  beforeToolCallHasFired,
  checkGateLivenessOnToolActivity,
  registerToolCallGate,
  _resetGateStateForTests,
} from "../dist/integrity/ToolCallGate.js";
import { captureSender, getSender, _resetForTests } from "../dist/util/senderRegistry.js";
import { resolveConfig, resolveLayer2Enforce } from "../dist/config.js";
import { TurnObserver } from "../dist/observer/TurnObserver.js";

const BOT = "atlas";

function mkTmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "toolgate-"));
}

function writeNetwork(sharedDir, network) {
  fs.mkdirSync(sharedDir, { recursive: true });
  fs.writeFileSync(path.join(sharedDir, "network.json"), JSON.stringify(network, null, 2));
}

function writeOverlay(sharedDir, botId, overlay) {
  const dir = path.join(sharedDir, "rosters");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, `${botId}.json`), JSON.stringify(overlay, null, 2));
}

/** A logger that records calls so we can assert warn/error fired. */
function recordingLogger() {
  const calls = { info: [], warn: [], error: [], debug: [] };
  return {
    _calls: calls,
    info: (m) => calls.info.push(m),
    warn: (m) => calls.warn.push(m),
    error: (m) => calls.error.push(m),
    debug: (m) => calls.debug.push(m),
  };
}

/** Build a config the handler accepts. */
function mkConfig(sharedDir, { enforce }) {
  return { botId: BOT, sharedDir, layer2Enforce: enforce, layer2EnforceWarning: null };
}

/** Read the per-bot decision ledger rows for today (or [] if none). */
function readLedger(sharedDir) {
  const dir = path.join(sharedDir, BOT, "layer2-gate");
  if (!fs.existsSync(dir)) return [];
  const rows = [];
  for (const f of fs.readdirSync(dir)) {
    if (!f.endsWith(".jsonl")) continue;
    const text = fs.readFileSync(path.join(dir, f), "utf8");
    for (const line of text.split("\n")) {
      if (line.trim()) rows.push(JSON.parse(line));
    }
  }
  return rows;
}

test.beforeEach(() => {
  _resetForTests();
  _resetGateStateForTests();
});

// Seed network with a pod-admin (telegram:999) and primary_user (telegram:500).
function seedFixture(sharedDir) {
  writeNetwork(sharedDir, {
    pod: { admins: { external_ids: { telegram: ["999"] } } },
    bots: { [BOT]: { primary_user: { external_ids: { telegram: "500" } } } },
  });
  writeOverlay(sharedDir, BOT, { identities: {}, blocked: {} });
}

// ── The capability→tool table sanity ─────────────────────────────────────────

test("sensitive table maps the KNOWN sensitive plugin tools to the right capabilities", () => {
  assert.equal(requiredCapabilityFor("roster_set_role"), "bot.roster.mutate");
  assert.equal(requiredCapabilityFor("roster_block"), "bot.roster.mutate");
  assert.equal(requiredCapabilityFor("roster_unblock"), "bot.roster.mutate");
  assert.equal(requiredCapabilityFor("channel_set_newcomer_mode"), "bot.channel.config");
  assert.equal(requiredCapabilityFor("gmail_send"), "bot.send_external");
  assert.equal(requiredCapabilityFor("calendar_create_event"), "bot.send_external");
  assert.equal(requiredCapabilityFor("drive_write_file"), "bot.send_external");
  // These are POSITIVELY classified via the table, not merely default-denied.
  assert.equal(classifyTool("roster_set_role").via, "table");
  assert.equal(classifyTool("gmail_send").via, "table");
});

test("known-safe READ / benign tools are ungated (no capability required)", () => {
  for (const t of [
    "defer", "record_application", "submit_intake", "expand_app",
    "evolve_help_search", "evolve_help_read",
    "pod_state",
    "directory_lookup",
    "gmail_list_messages", "gmail_get_message", "gmail_list_labels",
    "calendar_list_events", "drive_list_files", "drive_read_file", "drive_search",
  ]) {
    assert.equal(requiredCapabilityFor(t), undefined, `${t} should be ungated (safe)`);
    assert.equal(classifyTool(t).via, "safe", `${t} should classify via the safe allowlist`);
  }
});

// ── DEFAULT-DENY inversion (the review's CRITICAL finding) ────────────────────

test("gateway built-in exec names default-deny to bot.code.modify (no denylist needed)", () => {
  for (const t of [
    "bash", "exec", "apply_patch", "file_write", "write", "edit",
    // Anthropic-family editors + arbitrary future names — all fail CLOSED:
    "str_replace_based_edit_tool", "text_editor", "shell", "run_command",
    "some_brand_new_exec_tool_2027",
  ]) {
    assert.equal(requiredCapabilityFor(t), "bot.code.modify", `${t} must default-deny`);
    assert.equal(classifyTool(t).via, "default_deny", `${t} via default_deny`);
  }
});

test("an UNKNOWN tool (not safe, not sensitive) default-denies even without a toolKind", () => {
  assert.equal(requiredCapabilityFor("some_aliased_exec", undefined), "bot.code.modify");
  assert.equal(classifyTool("some_aliased_exec").via, "default_deny");
});

test("directory_upsert and session_set_tier are GATED (write tools; audit MEDIUM)", () => {
  assert.equal(requiredCapabilityFor("directory_upsert"), "bot.config.modify");
  assert.equal(requiredCapabilityFor("session_set_tier"), "bot.config.modify");
  assert.equal(classifyTool("directory_upsert").via, "table");
  assert.equal(classifyTool("session_set_tier").via, "table");
});

test("toolKind=code_mode_exec is gated (positive signal, wins over safe allowlist)", () => {
  assert.equal(requiredCapabilityFor("some_aliased_exec", "code_mode_exec"), "bot.code.modify");
  assert.equal(classifyTool("some_aliased_exec", "code_mode_exec").via, "toolKind");
  // even a name that would otherwise be "safe" is code-gated if the host says so
  assert.equal(requiredCapabilityFor("pod_state", "code_mode_exec"), "bot.code.modify");
});

test("case variants (Bash / BASH) still classify — fail-closed via default-deny", () => {
  assert.equal(requiredCapabilityFor("Bash"), "bot.code.modify");
  assert.equal(requiredCapabilityFor("BASH"), "bot.code.modify");
  // a case-variant of a SAFE tool does NOT widen the safe set (fail-closed)
  assert.equal(requiredCapabilityFor("POD_STATUS"), "bot.code.modify");
  assert.equal(classifyTool("POD_STATUS").via, "default_deny");
});

// ── MCP / namespaced normalization (the review's CRITICAL finding #2) ─────────

test("normalizeToolName strips the mcp__<server>__ envelope, keeps tool underscores", () => {
  assert.equal(normalizeToolName("mcp__x__bash"), "bash");
  assert.equal(normalizeToolName("mcp__gmail__send"), "send");
  assert.equal(normalizeToolName("mcp__gmail__send_message"), "send_message");
  assert.equal(normalizeToolName("mcp__evolve__roster_set_role"), "roster_set_role");
  assert.equal(normalizeToolName("roster_set_role"), "roster_set_role");
});

test("MCP-namespaced sensitive tool classifies to its capability after normalization", () => {
  assert.equal(requiredCapabilityFor("mcp__evolve__roster_set_role"), "bot.roster.mutate");
  assert.equal(requiredCapabilityFor("mcp__google__gmail_send"), "bot.send_external");
});

test("MCP names that don't classify default-deny (mcp__x__bash, mcp__gmail__send)", () => {
  // mcp__x__bash → bash → not safe, not sensitive → default-deny
  assert.equal(requiredCapabilityFor("mcp__x__bash"), "bot.code.modify");
  assert.equal(classifyTool("mcp__x__bash").via, "default_deny");
  // mcp__gmail__send → "send" (NOT gmail_send) → default-deny (fail-closed)
  assert.equal(requiredCapabilityFor("mcp__gmail__send"), "bot.code.modify");
  assert.equal(classifyTool("mcp__gmail__send").via, "default_deny");
});

// ── 1. Under-privileged speaker calling a gated tool ─────────────────────────

test("participant calling roster_set_role → BLOCKED in enforce mode", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  captureSender("run-1", { senderId: "111", platform: "telegram" }); // participant
  const logger = recordingLogger();
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: true }), logger);

  const res = handler({ toolName: "roster_set_role", params: {}, runId: "run-1" });
  assert.ok(res && res.block === true, "expected block:true");
  assert.match(res.blockReason, /bot\.roster\.mutate/);
  const ledger = readLedger(sharedDir);
  assert.equal(ledger.length, 1);
  assert.equal(ledger[0].action, "block");
  assert.equal(ledger[0].mode, "enforce");
  assert.equal(ledger[0].role, "participant");
});

test("participant calling roster_set_role → OBSERVE mode allows but records would_block", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  captureSender("run-2", { senderId: "111", platform: "telegram" }); // participant
  const logger = recordingLogger();
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: false }), logger);

  const res = handler({ toolName: "roster_set_role", params: {}, runId: "run-2" });
  assert.equal(res, undefined, "observe mode must allow (return void)");
  const ledger = readLedger(sharedDir);
  assert.equal(ledger.length, 1);
  assert.equal(ledger[0].action, "would_block");
  assert.equal(ledger[0].mode, "observe");
  assert.equal(ledger[0].sender_resolved, true);
  assert.ok(logger._calls.warn.some((m) => /would block/.test(m)));
});

// ── 2. Privileged speaker → allowed in both modes ────────────────────────────

test("admin calling roster_set_role → ALLOWED (void) in enforce mode", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  captureSender("run-3", { senderId: "999", platform: "telegram" }); // pod-admin
  const logger = recordingLogger();
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: true }), logger);

  const res = handler({ toolName: "roster_set_role", params: {}, runId: "run-3" });
  assert.equal(res, undefined, "authorized speaker must be allowed");
  assert.equal(readLedger(sharedDir).length, 0, "no would-block recorded for an allowed call");
});

test("primary_user calling gmail_send (has bot.send_external) → ALLOWED", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  captureSender("run-4", { senderId: "500", platform: "telegram" }); // primary_user
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: true }), recordingLogger());
  const res = handler({ toolName: "gmail_send", params: {}, runId: "run-4" });
  assert.equal(res, undefined);
});

test("primary_user calling apply_patch (LACKS bot.code.modify) → BLOCKED in enforce", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  captureSender("run-4b", { senderId: "500", platform: "telegram" }); // primary_user
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: true }), recordingLogger());
  const res = handler({ toolName: "apply_patch", params: {}, runId: "run-4b" });
  assert.ok(res && res.block === true, "primary_user lacks code.modify → blocked");
  assert.match(res.blockReason, /bot\.code\.modify/);
});

// ── DEFAULT-DENY end-to-end: unknown exec-shaped tool ────────────────────────

test("participant calling an UNKNOWN exec-shaped tool → BLOCKED when armed", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  captureSender("run-dd", { senderId: "111", platform: "telegram" }); // participant
  const logger = recordingLogger();
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: true }), logger);
  const res = handler({ toolName: "str_replace_based_edit_tool", params: {}, runId: "run-dd" });
  assert.ok(res && res.block === true, "unknown editor must default-deny for a participant");
  assert.match(res.blockReason, /bot\.code\.modify/);
  // drift should be logged so the operator can tune the allowlist
  assert.ok(logger._calls.warn.some((m) => /DRIFT/.test(m)), "expected a drift warning");
});

test("admin calling an UNKNOWN exec-shaped tool → ALLOWED (has bot.code.modify)", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  captureSender("run-dd2", { senderId: "999", platform: "telegram" }); // admin
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: true }), recordingLogger());
  const res = handler({ toolName: "some_brand_new_tool", params: {}, runId: "run-dd2" });
  assert.equal(res, undefined, "admin has * → default-deny cap is satisfied");
});

// ── 3. UNRESOLVED runId → fail-closed deny ───────────────────────────────────

test("no captured sender (unknown runId) → gated tool DENIED in enforce (fail-closed)", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  const logger = recordingLogger();
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: true }), logger);

  const res = handler({ toolName: "roster_block", params: {}, runId: "ghost-run" });
  assert.ok(res && res.block === true, "unresolved speaker must be blocked");
  assert.match(res.blockReason, /could not be identified|fail-closed/);
  const ledger = readLedger(sharedDir);
  assert.equal(ledger.length, 1);
  assert.equal(ledger[0].sender_resolved, false);
  assert.equal(ledger[0].role, null);
});

test("no captured sender → observe mode allows but records the fail-closed would_block", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: false }), recordingLogger());
  const res = handler({ toolName: "roster_block", params: {}, runId: "ghost-run-2" });
  assert.equal(res, undefined, "observe mode allows");
  const ledger = readLedger(sharedDir);
  assert.equal(ledger[0].action, "would_block");
  assert.equal(ledger[0].sender_resolved, false);
});

// ── 11. Platform fail-closed (null platform must NOT alias to telegram) ───────

test("sender with UNNORMALIZED platform → gated tool DENIED (no telegram fallback)", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  // senderId 999 is a telegram ADMIN — but the platform did not normalize
  // (captureSender stores platform=null for an unrecognized channel type). The
  // old code aliased to "telegram" and would have resolved this as admin →
  // ALLOWED. Fail-closed: resolve as participant → deny.
  captureSender("run-plat", { senderId: "999", platform: "web" }); // "web" → null
  assert.equal(getSender("run-plat").platform, null, "web must not normalize");
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: true }), recordingLogger());
  const res = handler({ toolName: "roster_set_role", params: {}, runId: "run-plat" });
  assert.ok(res && res.block === true, "null-platform admin-id must NOT be allowed");
  assert.match(res.blockReason, /no recognized|no fallback|fail-closed/);
});

// ── 4. Known-safe tool → always allowed regardless of role ───────────────────

test("safe tool (defer) → allowed even for a participant in enforce mode", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  captureSender("run-5", { senderId: "111", platform: "telegram" }); // participant
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: true }), recordingLogger());
  assert.equal(handler({ toolName: "defer", params: {}, runId: "run-5" }), undefined);
  assert.equal(handler({ toolName: "pod_state", params: {}, runId: "run-5" }), undefined);
  assert.equal(handler({ toolName: "gmail_list_messages", params: {}, runId: "run-5" }), undefined);
  assert.equal(readLedger(sharedDir).length, 0, "safe tools never touch the ledger");
});

test("safe tool with NO captured sender → still allowed (never blocked)", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: true }), recordingLogger());
  assert.equal(handler({ toolName: "pod_state", params: {}, runId: "nobody" }), undefined);
});

// ── 5. Gateway built-in with a speaker lacking bot.code.modify ───────────────

test("participant calling built-in bash → BLOCKED in enforce mode", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  captureSender("run-6", { senderId: "111", platform: "telegram" }); // participant
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: true }), recordingLogger());
  const res = handler({ toolName: "bash", params: { command: "rm -rf /" }, runId: "run-6" });
  assert.ok(res && res.block === true, "participant must not run bash");
  assert.match(res.blockReason, /bot\.code\.modify/);
});

test("participant calling an aliased exec via toolKind=code_mode_exec → BLOCKED in enforce", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  captureSender("run-7", { senderId: "111", platform: "telegram" }); // participant
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: true }), recordingLogger());
  const res = handler({
    toolName: "renamed_code_tool",
    params: {},
    toolKind: "code_mode_exec",
    runId: "run-7",
  });
  assert.ok(res && res.block === true, "kind-gated exec must be blocked");
  assert.match(res.blockReason, /bot\.code\.modify/);
});

test("admin calling built-in apply_patch → ALLOWED (has bot.code.modify)", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  captureSender("run-8", { senderId: "999", platform: "telegram" }); // admin
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: true }), recordingLogger());
  assert.equal(handler({ toolName: "apply_patch", params: {}, runId: "run-8" }), undefined);
});

test("mcp__x__bash from a participant → BLOCKED in enforce (normalization + default-deny)", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  captureSender("run-mcp", { senderId: "111", platform: "telegram" }); // participant
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: true }), recordingLogger());
  const res = handler({ toolName: "mcp__x__bash", params: {}, runId: "run-mcp" });
  assert.ok(res && res.block === true, "namespaced bash must be blocked");
  assert.match(res.blockReason, /bot\.code\.modify/);
});

// ── 9. Exception path fail-CLOSES ────────────────────────────────────────────

test("evaluation throwing → BLOCKED in enforce (fail-closed error_block)", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  captureSender("run-err", { senderId: "111", platform: "telegram" });
  const logger = recordingLogger();
  // sharedDir as a non-string makes resolveSpeakerRole's path.join throw,
  // exercising the handler's fail-closed catch for a KNOWN-gated call.
  const badConfig = { botId: BOT, sharedDir: {}, layer2Enforce: true, layer2EnforceWarning: null };
  const handler = makeBeforeToolCallHandler(badConfig, logger);
  const res = handler({ toolName: "roster_set_role", params: {}, runId: "run-err" });
  assert.ok(res && res.block === true, "internal error on a gated call must fail-closed block");
  assert.ok(logger._calls.error.some((m) => /internal error/.test(m)));
});

test("evaluation throwing → observe mode records error_block but ALLOWS", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  captureSender("run-err2", { senderId: "111", platform: "telegram" });
  const badConfig = { botId: BOT, sharedDir: {}, layer2Enforce: false, layer2EnforceWarning: null };
  const handler = makeBeforeToolCallHandler(badConfig, recordingLogger());
  const res = handler({ toolName: "roster_set_role", params: {}, runId: "run-err2" });
  assert.equal(res, undefined, "observe mode allows even on internal error");
});

// ── Context-arg fallback (event may carry runId on ctx, not event) ────────────

test("resolves runId from ctx when event.runId is absent", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  captureSender("run-ctx", { senderId: "111", platform: "telegram" }); // participant
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: true }), recordingLogger());
  const res = handler({ toolName: "roster_set_role", params: {} }, { runId: "run-ctx", toolName: "roster_set_role" });
  assert.ok(res && res.block === true);
});

// ── evaluateGatedCall unit (fail-closed on unresolved) ───────────────────────

test("evaluateGatedCall: unresolved runId → allowed=false, senderResolved=false", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  const ev = evaluateGatedCall({ botId: BOT, sharedDir }, "bot.roster.mutate", "missing");
  assert.equal(ev.allowed, false);
  assert.equal(ev.senderResolved, false);
  assert.equal(ev.role, null);
});

test("SENSITIVE_CAPABILITY_BY_TOOL is frozen (immutable source of truth)", () => {
  assert.throws(() => {
    // @ts-ignore — intentional mutation attempt
    SENSITIVE_CAPABILITY_BY_TOOL.some_new_tool = "bot.code.modify";
  });
  assert.ok(KNOWN_SAFE_TOOLS.has("pod_state"));
});

// ── 10. Non-boolean enforce stays observe-only (fail-safe + loud) ────────────

test("resolveLayer2Enforce: strict true arms; anything else stays observe", () => {
  assert.deepEqual(resolveLayer2Enforce({ layer2: { enforce: true } }), { armed: true, warning: null });
  assert.deepEqual(resolveLayer2Enforce({ layer2Enforce: true }), { armed: true, warning: null });
  assert.deepEqual(resolveLayer2Enforce({ layer2: { enforce: false } }), { armed: false, warning: null });
  assert.deepEqual(resolveLayer2Enforce({}), { armed: false, warning: null });
});

test("resolveLayer2Enforce: PRESENT-but-non-boolean stays observe AND warns", () => {
  for (const v of ["true", 1, "yes", 0, "false", null]) {
    const r = resolveLayer2Enforce({ layer2: { enforce: v } });
    assert.equal(r.armed, false, `enforce=${JSON.stringify(v)} must NOT arm`);
    assert.ok(typeof r.warning === "string" && r.warning.length > 0,
      `enforce=${JSON.stringify(v)} must produce a warning`);
  }
});

test("resolveConfig: non-boolean enforce → layer2Enforce false + layer2EnforceWarning set", () => {
  const cfg = resolveConfig({ botId: BOT, sharedDir: "/tmp/x", layer2: { enforce: "true" } }, {});
  assert.equal(cfg.layer2Enforce, false);
  assert.ok(typeof cfg.layer2EnforceWarning === "string" && cfg.layer2EnforceWarning.length > 0);
});

test("registerToolCallGate surfaces the non-boolean warning on the logger", () => {
  const logger = recordingLogger();
  const api = { on: () => {}, config: {} };
  registerToolCallGate(api, mkConfigWithWarning(), logger);
  assert.ok(logger._calls.warn.some((m) => /NON-BOOLEAN/.test(m)));
});
function mkConfigWithWarning() {
  return {
    botId: BOT, sharedDir: mkTmpDir(), layer2Enforce: false,
    layer2EnforceWarning: "Evolve Layer-2 gate: layer2.enforce is set to a NON-BOOLEAN value (\"true\"); refusing to arm.",
  };
}

// ── 12. Version gate + first-fire liveness latch ─────────────────────────────

test("detectOcVersion discovers a version from api.config", () => {
  assert.equal(detectOcVersion({ config: { version: "2026.6.11" } }), "2026.6.11");
  assert.equal(detectOcVersion({ config: { openclawVersion: "2026.7.0" } }), "2026.7.0");
  assert.equal(detectOcVersion({ config: {} }), null);
});

test("enforceAllowedByVersion: below 2026.6.11 → refuse (false) + ERROR; >= → true; unknown → true", () => {
  const logger = recordingLogger();
  assert.equal(enforceAllowedByVersion({ config: { version: "2026.6.10" } }, { botId: BOT }, logger), false);
  assert.ok(logger._calls.error.some((m) => /REFUSING to arm/.test(m)));
  assert.equal(enforceAllowedByVersion({ config: { version: "2026.6.11" } }, { botId: BOT }, recordingLogger()), true);
  assert.equal(enforceAllowedByVersion({ config: { version: "2026.7.0" } }, { botId: BOT }, recordingLogger()), true);
  assert.equal(enforceAllowedByVersion({ config: {} }, { botId: BOT }, recordingLogger()), true); // unknown
});

test("registerToolCallGate downgrades enforce→observe on a too-old gateway", async () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  const logger = recordingLogger();
  let registered = null;
  const api = { on: (_e, h) => { registered = h; }, config: { version: "2026.6.10" } };
  registerToolCallGate(api, { botId: BOT, sharedDir, layer2Enforce: true, layer2EnforceWarning: null }, logger);
  assert.ok(logger._calls.error.some((m) => /REFUSING to arm/.test(m)));
  // the registration log must reflect observe-only, not ENFORCE
  assert.ok(logger._calls.info.some((m) => /observe-only/.test(m)));
  // and the handler it registered must NOT block a participant (it was downgraded).
  // (registerToolCallGate wraps the handler in an async fn → await the Promise.)
  captureSender("run-ver", { senderId: "111", platform: "telegram" });
  const res = await registered({ toolName: "roster_set_role", params: {}, runId: "run-ver" });
  assert.equal(res, undefined, "downgraded gate must observe, not block");
});

test("first-fire latch: beforeToolCallHasFired flips once the handler runs", () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  assert.equal(beforeToolCallHasFired(), false);
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: true }), recordingLogger());
  handler({ toolName: "defer", params: {}, runId: "x" }); // even a safe tool fires it
  assert.equal(beforeToolCallHasFired(), true);
});

test("checkGateLivenessOnToolActivity: armed + never-fired → one-shot ERROR", () => {
  const logger = recordingLogger();
  // armed, latch not fired (fresh reset in beforeEach)
  checkGateLivenessOnToolActivity({ botId: BOT, layer2Enforce: true }, logger);
  assert.equal(logger._calls.error.filter((m) => /ARMED BUT NOT LIVE/.test(m)).length, 1);
  // one-shot: a second call does not re-log
  checkGateLivenessOnToolActivity({ botId: BOT, layer2Enforce: true }, logger);
  assert.equal(logger._calls.error.filter((m) => /ARMED BUT NOT LIVE/.test(m)).length, 1);
});

test("checkGateLivenessOnToolActivity: silent when observe-only OR provably live", () => {
  const observeLogger = recordingLogger();
  checkGateLivenessOnToolActivity({ botId: BOT, layer2Enforce: false }, observeLogger);
  assert.equal(observeLogger._calls.error.length, 0, "observe-only never warns");

  // now make it live, then check under armed
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  const handler = makeBeforeToolCallHandler(mkConfig(sharedDir, { enforce: true }), recordingLogger());
  handler({ toolName: "defer", params: {}, runId: "y" }); // fires the latch
  const liveLogger = recordingLogger();
  checkGateLivenessOnToolActivity({ botId: BOT, layer2Enforce: true }, liveLogger);
  assert.equal(liveLogger._calls.error.length, 0, "a live gate does not warn");
});

// ── 13. Tier fix: sender capture works at tier=monitor (armed-on-non-full) ────

test("sender capture is registered + works at tier=monitor (decoupled from injectKeywords)", async () => {
  const sharedDir = mkTmpDir();
  seedFixture(sharedDir);
  const config = resolveConfig({ botId: BOT, sharedDir, tier: "monitor" }, {});
  assert.equal(config.capabilities.injectKeywords, false, "monitor tier has no keyword injection");
  assert.equal(config.capabilities.observer, true, "monitor tier is observer-active");

  const logger = recordingLogger();
  const handlers = {};
  const fakeApi = {
    on: (event, handler) => { handlers[event] = handler; },
    logger,
  };
  const observer = new TurnObserver(config, logger, fakeApi);
  observer.register(fakeApi);

  // The tier fix: before_agent_run MUST be registered even without injectKeywords.
  assert.ok(handlers["before_agent_run"], "before_agent_run must register at tier=monitor");

  // Invoking it must capture the sender so the Layer-2 gate can resolve them.
  const out = await handlers["before_agent_run"](
    { senderId: "999", channelId: "telegram" },
    { runId: "tier-run", channelId: "telegram" },
  );
  assert.deepEqual(out, { outcome: "pass" }, "capture-only path must PASS the turn");
  const captured = getSender("tier-run");
  assert.ok(captured, "sender must be captured at tier=monitor");
  assert.equal(captured.senderId, "999");
  assert.equal(captured.platform, "telegram");

  // End-to-end: an admin's gated call now RESOLVES (not fail-closed-denied)
  // because capture ran — the armed-on-non-full bug is fixed.
  const gate = makeBeforeToolCallHandler(
    { botId: BOT, sharedDir, layer2Enforce: true, layer2EnforceWarning: null },
    recordingLogger(),
  );
  assert.equal(
    gate({ toolName: "roster_set_role", params: {}, runId: "tier-run" }),
    undefined,
    "admin's gated call is allowed because the sender was captured at monitor tier",
  );
});
