/**
 * Tests for OutwardActionLedger — bot-side outward-action telemetry
 * (autonomy ladder Phase B, spec-autonomy-ladder §1.3 / OQ-3).
 *
 * Focus: the pure extractor (dual payload shapes, result matching,
 * non-MCP filtering) and the writer's record shape + privacy contract
 * (names and ids only — no tool input ever lands in the ledger).
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/outwardActionLedger.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  OutwardActionLedger,
  extractMcpToolCalls,
  parseMcpToolName,
} from "../dist/observer/OutwardActionLedger.js";

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

// ── parseMcpToolName ─────────────────────────────────────────────────────────

test("parseMcpToolName splits server and tool", () => {
  assert.deepEqual(parseMcpToolName("mcp__google_workspace__send_gmail_message"), {
    integration_id: "google_workspace",
    tool_name: "send_gmail_message",
  });
});

test("parseMcpToolName rejects non-MCP and malformed names", () => {
  assert.equal(parseMcpToolName("exec"), null);
  assert.equal(parseMcpToolName("mcp__"), null);
  assert.equal(parseMcpToolName("mcp__noseparator"), null);
  assert.equal(parseMcpToolName("mcp____tool"), null);
  assert.equal(parseMcpToolName(42), null);
  assert.equal(parseMcpToolName(undefined), null);
});

// ── extractMcpToolCalls ──────────────────────────────────────────────────────

test("extract: anthropic-style tool_use with matched tool_result", () => {
  const messages = [
    {
      role: "assistant",
      content: [
        { type: "text", text: "sending" },
        { type: "tool_use", id: "t1", name: "mcp__google_workspace__send_gmail_message", input: { to: "x@y.z" } },
        { type: "tool_use", id: "t2", name: "mcp__google_workspace__search_gmail_messages" },
        { type: "tool_use", id: "t3", name: "read_file" },
      ],
    },
    {
      role: "user",
      content: [
        { type: "tool_result", tool_use_id: "t1", is_error: false },
        { type: "tool_result", tool_use_id: "t2", is_error: true },
      ],
    },
  ];
  const calls = extractMcpToolCalls(messages);
  assert.deepEqual(calls, [
    { integration_id: "google_workspace", tool_name: "send_gmail_message", result: "ok", call_id: "t1" },
    { integration_id: "google_workspace", tool_name: "search_gmail_messages", result: "error", call_id: "t2" },
  ]);
});

test("extract: openai-style top-level tool_calls", () => {
  const messages = [
    {
      role: "assistant",
      tool_calls: [
        { id: "c1", function: { name: "mcp__github__create_issue", arguments: "{}" } },
        { id: "c2", function: { name: "shell" } },
      ],
    },
  ];
  const calls = extractMcpToolCalls(messages);
  assert.deepEqual(calls, [
    { integration_id: "github", tool_name: "create_issue", result: "unknown", call_id: "c1" },
  ]);
});

test("extract: shape-tolerant on garbage payloads", () => {
  assert.deepEqual(extractMcpToolCalls(undefined), []);
  assert.deepEqual(extractMcpToolCalls("nope"), []);
  assert.deepEqual(extractMcpToolCalls([null, 7, { content: "str" }]), []);
});

// ── Writer ───────────────────────────────────────────────────────────────────

test("recordTurn appends name-only records to the dated ledger file", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "oal-"));
  const logger = fakeLogger();
  const ledger = new OutwardActionLedger(
    { sharedDir: tmp, botId: "team-bot-c" },
    logger,
  );
  const messages = [
    {
      role: "assistant",
      content: [
        { type: "tool_use", id: "t1", name: "mcp__google_workspace__send_gmail_message", input: { to: "secret@example.com", body: "SECRET" } },
      ],
    },
    { role: "user", content: [{ type: "tool_result", tool_use_id: "t1" }] },
  ];
  ledger.recordTurn(messages, "sess-1", "turn-1");

  const day = new Date().toISOString().slice(0, 10);
  const file = path.join(tmp, "team-bot-c", "outward-actions", `actions-${day}.jsonl`);
  const lines = fs.readFileSync(file, "utf8").trim().split("\n");
  assert.equal(lines.length, 1);
  const rec = JSON.parse(lines[0]);
  assert.equal(rec.integration_id, "google_workspace");
  assert.equal(rec.tool_name, "send_gmail_message");
  assert.equal(rec.result, "ok");
  assert.equal(rec.call_id, "t1");
  assert.equal(rec.session_id, "sess-1");
  assert.equal(rec.turn_id, "turn-1");
  // Privacy contract: tool input never lands in the ledger.
  assert.ok(!lines[0].includes("secret@example.com"));
  assert.ok(!lines[0].includes("SECRET"));

  // Second turn appends, never truncates.
  ledger.recordTurn(messages, "sess-1", "turn-2");
  assert.equal(fs.readFileSync(file, "utf8").trim().split("\n").length, 2);
});

test("recordTurn with no MCP calls writes nothing", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "oal-"));
  const ledger = new OutwardActionLedger(
    { sharedDir: tmp, botId: "team-bot-c" },
    fakeLogger(),
  );
  ledger.recordTurn([{ role: "assistant", content: [{ type: "text", text: "hi" }] }], "s", "t");
  assert.ok(!fs.existsSync(path.join(tmp, "team-bot-c", "outward-actions")));
});

test("recordTurn never throws on unwritable shared dir", () => {
  const ledger = new OutwardActionLedger(
    { sharedDir: "/nonexistent-root-for-test/zz", botId: "team-bot-c" },
    fakeLogger(),
  );
  ledger.recordTurn(
    [{ role: "assistant", content: [{ type: "tool_use", id: "a", name: "mcp__github__create_issue" }] }],
    "s", "t",
  );
});
