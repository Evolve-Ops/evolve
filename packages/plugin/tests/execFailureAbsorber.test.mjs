/**
 * Tests for ExecFailureAbsorber — the message_sending channel-hygiene hook
 * (design: internal/design-exec-failure-hygiene-2026-08-31.md, A1).
 *
 * Focus: the match family (ported from OpenClaw's own scrub regexes), the
 * observe-only vs armed behavior split, ledger rows, code-fence immunity,
 * and fail-open on ledger errors.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/execFailureAbsorber.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  ExecFailureAbsorber,
  decideAbsorb,
  isExecFailureTrailerLine,
} from "../dist/observer/ExecFailureAbsorber.js";

function fakeLogger() {
  const records = { debug: [], info: [], warn: [], error: [] };
  return {
    debug: (m) => records.debug.push(m),
    info: (m) => records.info.push(m),
    warn: (m) => records.warn.push(m),
    error: (m) => records.error.push(m),
    records,
  };
}

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "exec-absorb-test-"));
}

function makeAbsorber({ armed = false, sharedDir = tmpDir(), warning = null } = {}) {
  const logger = fakeLogger();
  const absorber = new ExecFailureAbsorber(
    { sharedDir, botId: "testbot", armed, armedWarning: warning },
    logger,
  );
  return { absorber, logger, sharedDir };
}

function readLedger(sharedDir) {
  const dir = path.join(sharedDir, "testbot", "exec-failures");
  if (!fs.existsSync(dir)) return [];
  const rows = [];
  for (const f of fs.readdirSync(dir).sort()) {
    for (const line of fs.readFileSync(path.join(dir, f), "utf8").split("\n")) {
      if (line.trim()) rows.push(JSON.parse(line));
    }
  }
  return rows;
}

const CTX = { channelId: "telegram", accountId: "default", sessionKey: "agent:main:tg" };

// ── Match family (vendored from OC's assistant-visible-text patterns) ────────

test("matches the incident trailer family", () => {
  const lines = [
    "⚠️ 🛠️ Exec failed: `python3 -c ...` (exit 1)",
    "⚠️ 🛠️ Exec failed (exit 1)",
    "⚠️ 🛠️ Bash failed: command not found",
    "⚠️ 🛠️ `git push` (agent) failed: denied",
    "> ⚠️ 🛠️ Exec failed: quoted-reply variant",
    "⚠️ 🧰 Process (0f3a2b1c) failed (exit 1): tail text",
    "⚠️ 🧰 Process (0f3a2b1c) failed (timed out waiting for output).",
    "Exec failed (0f3a2b1c, exit 1) :: EACCES: permission denied",
    "Exec failed (0f3a2b1c, code 127)",
    "Exec failed (0f3a2b1c, signal SIGKILL) :: killed",
  ];
  for (const line of lines) {
    assert.equal(isExecFailureTrailerLine(line), true, `should match: ${line}`);
  }
});

test("does not match ordinary content or near-misses", () => {
  const lines = [
    "The deploy failed, let me look into it.",
    "⚠️ Heads up: the backup disk is nearly full", // warning emoji, not a trailer
    "Exec completed (0f3a2b1c, exit 0) :: done", // success flavor — deliver
    "I ran the script and it printed 'Exec failed' in its logs.", // prose, no parens shape
    "🛠️ Working on it…", // progress line without failure
    "",
  ];
  for (const line of lines) {
    assert.equal(isExecFailureTrailerLine(line), false, `should NOT match: ${line}`);
  }
});

test("decideAbsorb: pure trailer payload absorbs fully", () => {
  const d = decideAbsorb("⚠️ 🛠️ Exec failed: `ls` (exit 1)");
  assert.ok(d);
  assert.equal(d.remaining, null);
  assert.equal(d.matched.length, 1);
});

test("decideAbsorb: mixed content strips only trailer lines", () => {
  const d = decideAbsorb(
    "Here's the summary you asked for.\n⚠️ 🛠️ Exec failed: `grep` (exit 2)\nAll done.",
  );
  assert.ok(d);
  assert.equal(d.remaining, "Here's the summary you asked for.\nAll done.");
  assert.equal(d.matched.length, 1);
});

test("decideAbsorb: trailer inside a code fence is untouched", () => {
  const content =
    "You asked what this means:\n```\n⚠️ 🛠️ Exec failed: `ls` (exit 1)\n```\nIt's a tool error.";
  assert.equal(decideAbsorb(content), null);
});

test("decideAbsorb: no match returns null", () => {
  assert.equal(decideAbsorb("Just a normal reply."), null);
});

// ── Observe-only (default) behavior ──────────────────────────────────────────

test("observe-only: trailer delivered unchanged, ledgered as would_absorb", () => {
  const { absorber, sharedDir } = makeAbsorber({ armed: false });
  const result = absorber.handleMessageSending(
    { to: "12345", content: "⚠️ 🛠️ Exec failed: `ls` (exit 1)" },
    CTX,
  );
  assert.equal(result, undefined); // deliver unchanged
  const rows = readLedger(sharedDir);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].action, "would_absorb");
  assert.equal(rows[0].armed, false);
  assert.equal(rows[0].bot_id, "testbot");
  assert.equal(rows[0].channel, "telegram");
  assert.equal(rows[0].full_content, "⚠️ 🛠️ Exec failed: `ls` (exit 1)");
});

test("observe-only: mixed content ledgered as would_strip, delivered unchanged", () => {
  const { absorber, sharedDir } = makeAbsorber({ armed: false });
  const result = absorber.handleMessageSending(
    { to: "12345", content: "hello\n⚠️ 🛠️ Bash failed (exit 1)" },
    CTX,
  );
  assert.equal(result, undefined);
  const rows = readLedger(sharedDir);
  assert.equal(rows[0].action, "would_strip");
  assert.equal(rows[0].full_content, undefined); // only full payloads recorded verbatim
  assert.deepEqual(rows[0].matched_lines, ["⚠️ 🛠️ Bash failed (exit 1)"]);
});

// ── Armed behavior ───────────────────────────────────────────────────────────

test("armed: pure trailer payload is cancelled", () => {
  const { absorber, sharedDir } = makeAbsorber({ armed: true });
  const result = absorber.handleMessageSending(
    { to: "12345", content: "⚠️ 🛠️ Exec failed: `python3 -c ...` (exit 1)" },
    CTX,
  );
  assert.deepEqual(result, {
    cancel: true,
    cancelReason: "evolve_exec_failure_absorbed",
  });
  const rows = readLedger(sharedDir);
  assert.equal(rows[0].action, "absorbed");
  assert.equal(rows[0].armed, true);
});

test("armed: mixed content is rewritten without the trailer", () => {
  const { absorber, sharedDir } = makeAbsorber({ armed: true });
  const result = absorber.handleMessageSending(
    { to: "12345", content: "Done with the report.\n⚠️ 🛠️ Exec failed: `mv` (exit 1)" },
    CTX,
  );
  assert.deepEqual(result, { content: "Done with the report." });
  assert.equal(readLedger(sharedDir)[0].action, "stripped");
});

test("armed: non-matching content passes through with no ledger row", () => {
  const { absorber, sharedDir } = makeAbsorber({ armed: true });
  const result = absorber.handleMessageSending(
    { to: "12345", content: "Here's your weekly summary. Everything ran fine." },
    CTX,
  );
  assert.equal(result, undefined);
  assert.equal(readLedger(sharedDir).length, 0);
});

// ── Fail-open ────────────────────────────────────────────────────────────────

test("missing/odd event shapes never throw", () => {
  const { absorber } = makeAbsorber({ armed: true });
  assert.equal(absorber.handleMessageSending({}, CTX), undefined);
  assert.equal(absorber.handleMessageSending({ content: undefined }, {}), undefined);
  assert.equal(absorber.handleMessageSending({ content: 42 }, undefined), undefined);
});

test("armed + ledger write failure refuses to absorb (absorbed ≠ vanished)", () => {
  // Point sharedDir at a path under a FILE so mkdir fails: with no record
  // possible, an armed absorber must deliver unchanged rather than vanish
  // the failure with no trace anywhere.
  const base = tmpDir();
  const blocker = path.join(base, "not-a-dir");
  fs.writeFileSync(blocker, "x");
  const { absorber, logger } = makeAbsorber({ armed: true, sharedDir: blocker });
  const result = absorber.handleMessageSending(
    { to: "12345", content: "⚠️ 🛠️ Exec failed (exit 1)" },
    CTX,
  );
  assert.equal(result, undefined); // delivered unchanged
  assert.match(logger.records.warn.join("\n"), /delivering the trailer unchanged/);
});

test("matches trailers whose emoji lost the VS16 variation selector", () => {
  // ⚠ U+26A0 and 🛠 U+1F6E0 without U+FE0F (an NFKC-normalizing channel
  // adapter or upstream re-render could drop the selector).
  const bare = "⚠ \u{1F6E0} Exec failed: `ls` (exit 1)";
  assert.equal(isExecFailureTrailerLine(bare), true);
  const { absorber } = makeAbsorber({ armed: true });
  const result = absorber.handleMessageSending({ to: "1", content: bare }, CTX);
  assert.equal(result?.cancel, true);
});

test("indented notify-on-exit line still reaches the matcher (pre-filter is a superset)", () => {
  const { absorber } = makeAbsorber({ armed: true });
  const result = absorber.handleMessageSending(
    { to: "1", content: "Background jobs:\n  Exec failed (0f3a2b1c, exit 1) :: EACCES" },
    CTX,
  );
  assert.deepEqual(result, { content: "Background jobs:" });
});

test("pre-filter hit with failure prose but no trailer ledgers a hash-only near-miss", () => {
  const { absorber, sharedDir } = makeAbsorber({ armed: false });
  const content = "🛠️ The deploy failed — I'm retrying it now.";
  const result = absorber.handleMessageSending({ to: "1", content }, CTX);
  assert.equal(result, undefined);
  const rows = readLedger(sharedDir);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].action, "near_miss");
  assert.equal(typeof rows[0].content_sha256, "string");
  assert.equal(rows[0].content_chars, content.length);
  assert.equal(rows[0].matched_lines, undefined);
  // Never the text itself.
  assert.ok(!JSON.stringify(rows[0]).includes("deploy failed"));
});

test("non-failure tool-progress prose produces no near-miss row", () => {
  const { absorber, sharedDir } = makeAbsorber({ armed: false });
  const result = absorber.handleMessageSending(
    { to: "1", content: "🛠️ Working on the report…" },
    CTX,
  );
  assert.equal(result, undefined);
  assert.equal(readLedger(sharedDir).length, 0);
});

// ── Registration ─────────────────────────────────────────────────────────────

test("register wires message_sending and logs the mode", () => {
  const { absorber, logger } = makeAbsorber({ armed: false });
  const hooks = {};
  const api = { on: (name, fn) => (hooks[name] = fn) };
  absorber.register(api);
  assert.equal(typeof hooks.message_sending, "function");
  assert.match(logger.records.info.join("\n"), /observe-only/);
});

test("register surfaces the non-boolean arming warning loudly", () => {
  const { absorber, logger } = makeAbsorber({
    armed: false,
    warning: "Evolve exec-failure absorber: refusing to arm",
  });
  absorber.register({ on: () => {} });
  assert.match(logger.records.warn.join("\n"), /refusing to arm/);
});
