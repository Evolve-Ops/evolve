/**
 * Tests for CostLedger schema v2 — PR E enrichment.
 *
 * Pins:
 *   1. buildCostEvent emits schema_version: 2 with all v2 fields present
 *      (timestamp_local, user_id, user_display_name, channel_id, channel_kind).
 *   2. Missing optional inputs land as null (consistent JSON shape so
 *      downstream readers don't have to branch on presence).
 *   3. Caller-provided values pass through unchanged.
 *
 * The actual user/channel resolution happens in Python
 * (cost_event_converter.py reads the authoritative turn-collector
 * record); the TS path is the fallback writer + the schema contract.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/costLedger.schemaV2.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  buildCostEvent,
  COST_EVENT_SCHEMA_VERSION,
} from "../dist/observer/CostLedger.js";

const FIXED_NOW = new Date("2026-05-29T00:52:00.000Z");


function baseInput(overrides = {}) {
  return {
    botId: "team-bot-a",
    sessionId: "3d5cde22-1111-2222-3333-444455556666",
    triggerKind: "user_turn",
    model: "claude-sonnet-4-6",
    provider: "anthropic",
    inputTokens: 1000,
    outputTokens: 200,
    cacheReadTokens: 5000,
    cacheWriteTokens: 0,
    costUsd: 0.45,
    ...overrides,
  };
}


test("COST_EVENT_SCHEMA_VERSION is 2", () => {
  assert.equal(COST_EVENT_SCHEMA_VERSION, 2);
});


test("buildCostEvent emits schema v2 with v1 keys preserved", () => {
  const ev = buildCostEvent(baseInput(), FIXED_NOW);
  assert.equal(ev.schema_version, 2);
  assert.equal(ev.type, "cost_event");
  assert.equal(ev.ts, "2026-05-29T00:52:00.000Z");
  assert.equal(ev.bot_id, "team-bot-a");
  assert.equal(ev.session_id, "3d5cde22-1111-2222-3333-444455556666");
  assert.equal(ev.trigger_kind, "user_turn");
  assert.equal(ev.model, "claude-sonnet-4-6");
  assert.equal(ev.provider, "anthropic");
  assert.equal(ev.cost_usd, 0.45);
  // v1 token fields unchanged.
  assert.equal(ev.input_tokens, 1000);
  assert.equal(ev.output_tokens, 200);
  assert.equal(ev.cache_read_tokens, 5000);
  assert.equal(ev.cache_write_tokens, 0);
});


test("v2 fields default to null when caller omits them", () => {
  const ev = buildCostEvent(baseInput(), FIXED_NOW);
  // All present, all null — consistent shape so readers don't branch.
  assert.equal(ev.timestamp_local, null);
  assert.equal(ev.user_id, null);
  assert.equal(ev.user_display_name, null);
  assert.equal(ev.channel_id, null);
  assert.equal(ev.channel_kind, null);
});


test("v2 fields pass through when caller supplies them", () => {
  const ev = buildCostEvent(
    baseInput({
      timestampLocal: "2026-05-28T17:52:00-07:00",
      userId: "U0518A544N5",
      userDisplayName: "Peter",
      channelId: "D0AKX41HELU",
      channelKind: "slack_dm",
    }),
    FIXED_NOW,
  );
  assert.equal(ev.timestamp_local, "2026-05-28T17:52:00-07:00");
  assert.equal(ev.user_id, "U0518A544N5");
  assert.equal(ev.user_display_name, "Peter");
  assert.equal(ev.channel_id, "D0AKX41HELU");
  assert.equal(ev.channel_kind, "slack_dm");
});


test("explicitly-null v2 inputs are emitted as null (not undefined)", () => {
  // Distinguishes "caller intentionally cleared" from "key absent." Both
  // paths must land as null in the JSON so downstream readers don't see
  // undefined keys after JSON.stringify().
  const ev = buildCostEvent(
    baseInput({
      timestampLocal: null,
      userId: null,
      userDisplayName: null,
      channelId: null,
      channelKind: null,
    }),
    FIXED_NOW,
  );
  for (const k of [
    "timestamp_local", "user_id", "user_display_name",
    "channel_id", "channel_kind",
  ]) {
    assert.equal(ev[k], null, `${k} should be null, got ${ev[k]}`);
    assert.ok(k in ev, `${k} must be present in the emitted record`);
  }
});


test("internal channel_kind for heartbeat-style cost events", () => {
  // Heartbeats / cron / subagent — channel_kind = "internal" means
  // bot-to-bot, no external user. The renderer treats this specially.
  const ev = buildCostEvent(
    baseInput({
      triggerKind: "heartbeat",
      channelKind: "internal",
      userId: null,
      channelId: null,
    }),
    FIXED_NOW,
  );
  assert.equal(ev.trigger_kind, "heartbeat");
  assert.equal(ev.channel_kind, "internal");
  assert.equal(ev.user_id, null);
  assert.equal(ev.channel_id, null);
});
