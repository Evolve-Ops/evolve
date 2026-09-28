/**
 * Tests for `shouldRetagHeartbeatSource` — the defensive workaround for
 * openclaw/openclaw#84825 (OC loses isHeartbeat across sub-runs).
 *
 * Uses Node's built-in `node:test` runner (no external test deps).
 * Imports from `dist/` because the plugin's tsconfig hasn't enabled
 * ts-node and the rest of the codebase is JS-after-build.
 *
 * Run from this dir (after a plugin build):
 *   node --test tests/sourceClassifier.heartbeatRetag.test.mjs
 *
 * The dataset block at the bottom replays the smoking-gun shape from
 * the 2026-05-21 security_bot repro and asserts the rollup matches the
 * "after fix" counts called out in the incident doc — 17 heartbeat-
 * channel sessions classify as 157 heartbeat turns + 0 human (not
 * 10 + 151 as today), and the 1 real Telegram session stays human.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { shouldRetagHeartbeatSource } from "../dist/breakers/sourceClassifier.js";

test("re-tags the canonical sub-run: channel=heartbeat, source=human, earlier turn was heartbeat", () => {
  assert.equal(
    shouldRetagHeartbeatSource({
      channel: "heartbeat",
      currentSource: "human",
      sessionTriggeredByHeartbeat: true,
    }),
    true,
  );
});

test("re-tags when current source is 'user' (the other observed drift label)", () => {
  assert.equal(
    shouldRetagHeartbeatSource({
      channel: "heartbeat",
      currentSource: "user",
      sessionTriggeredByHeartbeat: true,
    }),
    true,
  );
});

test("idempotent: does not re-tag when current source is already 'heartbeat'", () => {
  assert.equal(
    shouldRetagHeartbeatSource({
      channel: "heartbeat",
      currentSource: "heartbeat",
      sessionTriggeredByHeartbeat: true,
    }),
    false,
  );
});

test("fail-safe: never re-tags if the session was not previously seen as heartbeat", () => {
  // A session whose channel happens to read 'heartbeat' but that
  // was never observed with source==heartbeat is left alone.
  assert.equal(
    shouldRetagHeartbeatSource({
      channel: "heartbeat",
      currentSource: "human",
      sessionTriggeredByHeartbeat: false,
    }),
    false,
  );
});

test("only re-tags on heartbeat-channel turns: telegram/slack/cron/unknown stay put", () => {
  for (const channel of ["telegram", "slack", "cron", "unknown", "", null]) {
    assert.equal(
      shouldRetagHeartbeatSource({
        channel,
        currentSource: "human",
        sessionTriggeredByHeartbeat: true,
      }),
      false,
      `channel=${JSON.stringify(channel)} should not re-tag`,
    );
  }
});

test("case-insensitive on channel + current source", () => {
  assert.equal(
    shouldRetagHeartbeatSource({
      channel: "HEARTBEAT",
      currentSource: "HUMAN",
      sessionTriggeredByHeartbeat: true,
    }),
    true,
  );
  assert.equal(
    shouldRetagHeartbeatSource({
      channel: "heartbeat",
      currentSource: "Heartbeat",
      sessionTriggeredByHeartbeat: true,
    }),
    false,
  );
});

test("missing/null fields default to 'no re-tag' (fail-safe)", () => {
  assert.equal(
    shouldRetagHeartbeatSource({
      sessionTriggeredByHeartbeat: true,
    }),
    false,
  );
  assert.equal(
    shouldRetagHeartbeatSource({
      channel: null,
      currentSource: null,
      sessionTriggeredByHeartbeat: true,
    }),
    false,
  );
});

test("smoking-gun replay: 17 heartbeat-channel sessions + 1 real telegram session", () => {
  // Mimics the 2026-05-21 security_bot shape. Each heartbeat session is a
  // burst of 9-10 turns: the first records source=heartbeat (because
  // OC still has isHeartbeat set on the parent run), the rest drift
  // to source=human. The real Telegram session has 4 turns, all
  // source=human, channel=telegram.
  const turns = [];
  const HEARTBEAT_SESSION_COUNT = 17;
  const TURNS_PER_HEARTBEAT_SESSION = [9, 9, 9, 10, 9, 10, 9, 9, 10, 9, 9, 9, 9, 10, 9, 9, 9];
  // sum = 157 — matches the incident report.
  let totalHeartbeatTurns = 0;
  for (let s = 0; s < HEARTBEAT_SESSION_COUNT; s++) {
    const n = TURNS_PER_HEARTBEAT_SESSION[s];
    totalHeartbeatTurns += n;
    const sessionId = `hb-${s}`;
    for (let t = 0; t < n; t++) {
      turns.push({
        sessionId,
        channel: "heartbeat",
        sourceRaw: t === 0 ? "heartbeat" : "human",
      });
    }
  }
  assert.equal(totalHeartbeatTurns, 157, "test fixture sanity: 17 hb sessions × ~9-10 turns = 157");

  // The real Telegram session — 4 turns, all human, never re-tagged.
  const telegramTurns = 4;
  for (let t = 0; t < telegramTurns; t++) {
    turns.push({
      sessionId: "b19b995e-1",
      channel: "1260193629",  // numeric Telegram chat ID
      sourceRaw: "human",
    });
  }

  // Replay through the plugin's two-step pipeline:
  //   1. Track sessions ever observed with source=="heartbeat"
  //   2. For each turn, decide whether to re-tag
  const heartbeatSessions = new Set();
  const finalSources = [];
  for (const turn of turns) {
    if (turn.sourceRaw === "heartbeat") {
      heartbeatSessions.add(turn.sessionId);
    }
    const retag = shouldRetagHeartbeatSource({
      channel: turn.channel,
      currentSource: turn.sourceRaw,
      sessionTriggeredByHeartbeat: heartbeatSessions.has(turn.sessionId),
    });
    finalSources.push(retag ? "heartbeat" : turn.sourceRaw);
  }

  // After the fix: every heartbeat-channel turn reads source=heartbeat.
  let postFixHeartbeat = 0;
  let postFixHuman = 0;
  for (let i = 0; i < turns.length; i++) {
    const t = turns[i];
    const finalSource = finalSources[i];
    if (t.channel === "heartbeat") {
      assert.equal(finalSource, "heartbeat", `hb-channel turn should re-tag (session=${t.sessionId}, raw=${t.sourceRaw})`);
      postFixHeartbeat++;
    } else {
      assert.equal(finalSource, "human", `telegram turn must stay human (session=${t.sessionId})`);
      postFixHuman++;
    }
  }

  assert.equal(postFixHeartbeat, 157, "after fix: 157 heartbeat turns");
  assert.equal(postFixHuman, 4, "after fix: 4 human turns (the real Telegram session)");

  // Sanity-check what the naive (no-fix) rollup would have produced —
  // these are the misleading numbers from the incident doc.
  let preFixHeartbeat = 0;
  let preFixHuman = 0;
  for (const t of turns) {
    if (t.sourceRaw === "heartbeat") preFixHeartbeat++;
    else preFixHuman++;
  }
  assert.equal(preFixHeartbeat, 17, "pre-fix shape: 17 heartbeat turns (1 per hb-session)");
  assert.equal(preFixHuman, 144, "pre-fix shape: 140 hb sub-runs mislabeled + 4 real telegram = 144 'human'");
});
