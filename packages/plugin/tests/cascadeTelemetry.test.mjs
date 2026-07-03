/**
 * Tests for CascadeTelemetry — hot-path Opik span emitter.
 *
 * Focus: the Opik-shaped span object built by buildSpan(). The actual
 * I/O write is best-effort and not asserted here (covered by integration
 * once Phase 1 lands on a real pod).
 *
 * Spec: docs/spec-tier-cascade-2026-05-26.md § 3.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/cascadeTelemetry.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { CascadeTelemetry } from "../dist/observer/CascadeTelemetry.js";

// Tiny logger fake — captures messages so we can assert on them.
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
  return fs.mkdtempSync(path.join(os.tmpdir(), "cascade-tel-test-"));
}

const T0 = new Date("2026-05-26T12:00:00Z");
const T1 = new Date("2026-05-26T12:00:01.500Z");

// ── buildSpan ────────────────────────────────────────────────────────────────

test("buildSpan: basic shape — required Opik fields", () => {
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier2",
    tierIntended: "tier2",
    tierChosenBy: "classifier",
  });

  assert.equal(span.name, "bot_session_turn");
  assert.equal(span.type, "general");
  assert.equal(span.start_time, T0.toISOString());
  assert.equal(span.end_time, T1.toISOString());
  assert.equal(span.bot_id, "team_bot_a");
  assert.equal(span.producer, "cascade_telemetry");
  assert.equal(span.trace_id, "sess-1");
  assert.equal(span.span_id, "sess-1:1");
});

test("buildSpan: attributes carry session_id + turn_index + tier", () => {
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 3,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier3",
    tierIntended: "tier3",
    tierChosenBy: "spend_cap",
  });

  assert.equal(span.attributes.session_id, "sess-1");
  assert.equal(span.attributes.turn_index, 3);
  assert.equal(span.attributes["cascade.tier_used"], "tier3");
  assert.equal(span.attributes["cascade.tier_intended"], "tier3");
  assert.equal(span.attributes["cascade.tier_divergent"], false);
  assert.equal(span.attributes["cascade.tier_chosen_by"], "spend_cap");
});

test("buildSpan: tier_used=null is preserved, NOT divergent", () => {
  // When ModelRouter.getTierForModel returns null ("we don't know what tier
  // this model belongs to"), TurnObserver passes null straight through —
  // it must NOT fall back to tierIntended (that would silently substitute
  // intent for truth, the F8 failure pattern the longest-match rewrite
  // existed to prevent). Span records null as the literal value.
  // tier_divergent is FALSE here because divergence requires "knew both,
  // they differed" — unknown isn't divergence, it's its own state that
  // downstream consumers detect via tier_used IS NULL queries.
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: null,
    tierIntended: "tier2",
    tierChosenBy: "classifier",
  });

  assert.equal(span.attributes["cascade.tier_used"], null);
  assert.equal(span.attributes["cascade.tier_intended"], "tier2");
  assert.equal(span.attributes["cascade.tier_divergent"], false);
  // Don't add a tier:* tag when tier_used is unknown — would falsely
  // attribute to no tier.
  assert.ok(!span.tags.some((t) => t.startsWith("tier:")));
});

test("buildSpan: tier divergence flagged when used != intended", () => {
  // The canonical failure-F8 scenario: cascade INTENDED tier3 (classifier said
  // maintenance), but OC silently ran tier2 anyway (override dropped). The
  // span records truth (tier_used=tier2) AND intent (tier_intended=tier3),
  // and flags the divergence so the audit layer sees it.
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier2",       // what actually ran
    tierIntended: "tier3",    // what cascade wanted
    tierChosenBy: "classifier",
  });

  assert.equal(span.attributes["cascade.tier_used"], "tier2");
  assert.equal(span.attributes["cascade.tier_intended"], "tier3");
  assert.equal(span.attributes["cascade.tier_divergent"], true);
  assert.ok(span.tags.includes("tier_divergent"));
});

test("buildSpan: trigger_kind populated when provided", () => {
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier3",
    tierIntended: "tier3",
    tierChosenBy: "classifier",
    triggerKind: "heartbeat",
  });
  assert.equal(span.attributes["cascade.trigger_kind"], "heartbeat");
  assert.ok(span.tags.includes("trigger:heartbeat"));
});

test("buildSpan: trigger_kind omitted when not provided", () => {
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier3",
    tierIntended: "tier3",
    tierChosenBy: "classifier",
  });
  assert.equal(span.attributes["cascade.trigger_kind"], undefined);
});

test("buildSpan: tags include tier and source-of-choice", () => {
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier1",
    tierChosenBy: "user_request",
  });

  assert.ok(Array.isArray(span.tags));
  assert.ok(span.tags.includes("cascade"));
  assert.ok(span.tags.includes("turn"));
  assert.ok(span.tags.includes("tier:tier1"));
  assert.ok(span.tags.includes("user_chose_tier"));
});

test("buildSpan: struggle signal flattens to dot-notation attributes", () => {
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier2",
    tierIntended: "tier2",
    tierChosenBy: "classifier",
    struggle: {
      score: 0.42,
      features: {
        tool_error_count: 0.25,
        tool_retry_count: 0.10,
        restart_markers: 0,
        clarification_loops: 0,
        tokens_per_progress: 0.07,
      },
      raw: {
        tool_error_count: 2,
        tool_retry_count: 1,
        restart_markers: 0,
        clarification_loops: 0,
        tokens_per_progress: 14000,
      },
    },
  });

  assert.equal(span.attributes["cascade.struggle.score"], 0.42);
  assert.equal(span.attributes["cascade.struggle.features.tool_error_count"], 0.25);
  assert.equal(span.attributes["cascade.struggle.features.tool_retry_count"], 0.10);
  assert.equal(span.attributes["cascade.struggle.raw.tool_error_count"], 2);
  assert.equal(span.attributes["cascade.struggle.raw.tokens_per_progress"], 14000);
});

test("buildSpan: payload_drift flag carried in attributes (round-3 review #1)", () => {
  // When the struggle detector returned null due to payload drift,
  // the span must record both score=null AND the drift reason.
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier2",
    tierIntended: "tier2",
    tierChosenBy: "classifier",
    struggle: {
      score: null,
      features: {
        tool_error_count: 0, tool_retry_count: 0,
        restart_markers: 0, clarification_loops: 0, tokens_per_progress: 0,
      },
      raw: {
        tool_error_count: 0, tool_retry_count: 0,
        restart_markers: 0, clarification_loops: 0, tokens_per_progress: 0,
      },
      payload_drift: "no_messages",
    },
  });
  assert.equal(span.attributes["cascade.struggle.score"], null);
  assert.equal(span.attributes["cascade.struggle.payload_drift"], "no_messages");
});

test("buildSpan: holdout cohort tag + variant (spec § 2.3 Component 5)", () => {
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());

  // Non-holdout session: holdout=false, variant="A" (production)
  const prod = t.buildSpan({
    sessionId: "s1", turnIndex: 1, startedAt: T0, endedAt: T1,
    tierUsed: "tier2", tierIntended: "tier2", tierChosenBy: "classifier",
    holdout: false,
  });
  assert.equal(prod.attributes["cascade.holdout"], false);
  assert.equal(prod.attributes["cascade.variant"], "A");

  // Holdout session: holdout=true, variant defaults to "baseline"
  const baseline = t.buildSpan({
    sessionId: "s2", turnIndex: 1, startedAt: T0, endedAt: T1,
    tierUsed: "tier2", tierIntended: "tier2", tierChosenBy: "classifier",
    holdout: true,
  });
  assert.equal(baseline.attributes["cascade.holdout"], true);
  assert.equal(baseline.attributes["cascade.variant"], "baseline");
});

test("buildSpan: holdout defaults to false when not passed", () => {
  // Spec invariant: cascade.holdout is ALWAYS present, defaults to false.
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "s1", turnIndex: 1, startedAt: T0, endedAt: T1,
    tierUsed: "tier2", tierIntended: "tier2", tierChosenBy: "classifier",
  });
  assert.equal(span.attributes["cascade.holdout"], false);
  assert.equal(span.attributes["cascade.variant"], "A");
});

test("buildSpan: variant override honored", () => {
  // Phase 4+ shadow A/B can pass custom variant (e.g. "B" for a shadow arm).
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "s1", turnIndex: 1, startedAt: T0, endedAt: T1,
    tierUsed: "tier2", tierIntended: "tier2", tierChosenBy: "classifier",
    variant: "B",
  });
  assert.equal(span.attributes["cascade.variant"], "B");
});

test("buildSpan: shadow-verdict attributes (Phase 2 shadow mode)", () => {
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "s1", turnIndex: 1, startedAt: T0, endedAt: T1,
    tierUsed: "tier2", tierIntended: "tier2", tierChosenBy: "classifier",
    shadowVerdictTier: "tier3",
    shadowVerdictEscalationEvent: "deescalated",
    shadowVerdictAskHintEmitted: false,
    shadowVerdictDisagrees: true,
  });
  assert.equal(span.attributes["cascade.shadow_verdict.tier"], "tier3");
  assert.equal(span.attributes["cascade.shadow_verdict.escalation_event"], "deescalated");
  assert.equal(span.attributes["cascade.shadow_verdict.disagrees"], true);
  // ask_hint_emitted=false means the attribute is NOT set (conditional add)
  assert.equal(span.attributes["cascade.shadow_verdict.ask_hint_emitted"], undefined);
  assert.ok(span.tags.includes("shadow_disagrees"));
});

test("buildSpan: shadow_verdict ask_hint tag fires when bot would ask user", () => {
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "s1", turnIndex: 1, startedAt: T0, endedAt: T1,
    tierUsed: "tier2", tierIntended: "tier2", tierChosenBy: "classifier",
    shadowVerdictTier: "tier2",
    shadowVerdictEscalationEvent: "held",
    shadowVerdictAskHintEmitted: true,
    shadowVerdictDisagrees: false,
  });
  assert.equal(span.attributes["cascade.shadow_verdict.ask_hint_emitted"], true);
  assert.ok(span.tags.includes("shadow_ask_hint"));
});

test("buildSpan: shadow_verdict omitted when not passed (back-compat)", () => {
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "s1", turnIndex: 1, startedAt: T0, endedAt: T1,
    tierUsed: "tier2", tierIntended: "tier2", tierChosenBy: "classifier",
  });
  // No shadow_verdict.* attributes when not passed.
  assert.equal(span.attributes["cascade.shadow_verdict.tier"], undefined);
  assert.equal(span.attributes["cascade.shadow_verdict.disagrees"], undefined);
  assert.ok(!span.tags.includes("shadow_disagrees"));
  assert.ok(!span.tags.includes("shadow_ask_hint"));
});

test("buildSpan: omits struggle attributes when no struggle passed", () => {
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier2",
    tierChosenBy: "classifier",
  });

  assert.equal(span.attributes["cascade.struggle.score"], undefined);
});

test("buildSpan: usage block populated from token counts", () => {
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier2",
    tierChosenBy: "classifier",
    inputTokens: 1000,
    outputTokens: 250,
    cacheReadTokens: 800,
    cacheWriteTokens: 0,
    costUsd: 0.045,
    model: "claude-sonnet-4-6",
    provider: "anthropic",
  });

  assert.equal(span.usage.input_tokens, 1000);
  assert.equal(span.usage.output_tokens, 250);
  assert.equal(span.usage.cache_read_tokens, 800);
  assert.equal(span.usage.cache_write_tokens, 0);
  assert.equal(span.total_cost, 0.045);
  assert.equal(span.model, "claude-sonnet-4-6");
  assert.equal(span.provider, "anthropic");
});

test("buildSpan: error_info populated only when error present", () => {
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());

  const ok = t.buildSpan({
    sessionId: "s", turnIndex: 1, startedAt: T0, endedAt: T1,
    tierUsed: "tier2", tierChosenBy: "classifier",
  });
  assert.equal(ok.error_info, null);

  const bad = t.buildSpan({
    sessionId: "s", turnIndex: 1, startedAt: T0, endedAt: T1,
    tierUsed: "tier2", tierChosenBy: "classifier",
    error: { message: "rate-limited", code: "429" },
  });
  assert.equal(bad.error_info.message, "rate-limited");
  assert.equal(bad.error_info.code, "429");
});

// ── cascade.success — added 2026-06-06 (PR #2296 sibling) ─────────────────

test("buildSpan: cascade.success=true is written when OC reports success", () => {
  // Audit consumers need the raw per-turn success bit to compute
  // "routed to tier1 ∩ succeeded" vs "routed to tier1 ∩ failed" without
  // recomputing from the annotation JSONL. The plumbing was already in
  // place (TurnObserver passes input.success through); this pin ensures
  // the writer doesn't silently drop it.
  const t = new CascadeTelemetry(
    { sharedDir: "/tmp/x", botId: "team_bot_a" },
    fakeLogger(),
  );
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier2",
    tierIntended: "tier2",
    tierChosenBy: "classifier",
    success: true,
  });
  assert.equal(span.attributes["cascade.success"], true);
});

test("buildSpan: cascade.success=false is preserved (the load-bearing case)", () => {
  // The struggle-detector floor at 0.5 only kicks in on success=false
  // (StruggleDetector.ts:424-433). If the writer collapsed false to
  // "not present" via truthiness, the audit layer would see every
  // turn as success=true and the cascade-correlation analysis would
  // be blind to the entire failure population — exactly the data we
  // need to fix the detector with.
  const t = new CascadeTelemetry(
    { sharedDir: "/tmp/x", botId: "team_bot_a" },
    fakeLogger(),
  );
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier2",
    tierIntended: "tier2",
    tierChosenBy: "classifier",
    success: false,
  });
  assert.equal(span.attributes["cascade.success"], false);
  // Specifically NOT absent — the difference matters for downstream
  // schema readers that distinguish "field missing" from "field=false".
  assert.equal("cascade.success" in span.attributes, true);
});

test("buildSpan: cascade.success omitted when not provided (back-compat)", () => {
  // Older agent_end paths may not surface a success field. Don't fabricate
  // a value — leave the attribute absent so downstream readers can
  // distinguish "OC didn't tell us" from "OC said false".
  const t = new CascadeTelemetry(
    { sharedDir: "/tmp/x", botId: "team_bot_a" },
    fakeLogger(),
  );
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier2",
    tierIntended: "tier2",
    tierChosenBy: "classifier",
  });
  assert.equal("cascade.success" in span.attributes, false);
});

// ── cascade.preflight.* — added 2026-06-06 (Preflight Intent Router Phase 1) ──

test("buildSpan: preflight attributes written when router ran (abstain-only)", () => {
  // Phase 1 ships abstain-only — every user_turn span where the router
  // ran gets `cascade.preflight.layer="abstain"`. This pin proves the
  // wiring before Phase 2 turns on the regex layer.
  const t = new CascadeTelemetry(
    { sharedDir: "/tmp/x", botId: "team_bot_a" },
    fakeLogger(),
  );
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier2",
    tierIntended: "tier2",
    tierChosenBy: "classifier",
    preflight: {
      tier: null,
      reason: "abstain",
      layer: "abstain",
      confidence: 0,
      latency_ms: 0.5,
    },
  });
  assert.equal(span.attributes["cascade.preflight.tier"], null);
  assert.equal(span.attributes["cascade.preflight.reason"], "abstain");
  assert.equal(span.attributes["cascade.preflight.layer"], "abstain");
  assert.equal(span.attributes["cascade.preflight.confidence"], 0);
  assert.equal(span.attributes["cascade.preflight.latency_ms"], 0.5);
});

test("buildSpan: preflight tier1 decision (Phase 2+ smoke)", () => {
  // Validates that when the router DOES make a real decision, the tier
  // surfaces honestly on the span. The audit detector reads
  // `cascade.preflight.tier` vs `cascade.tier_used` to compute
  // agreement / disagreement rates.
  const t = new CascadeTelemetry(
    { sharedDir: "/tmp/x", botId: "team_bot_a" },
    fakeLogger(),
  );
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier1",
    tierIntended: "tier1",
    tierChosenBy: "preflight",
    preflight: {
      tier: "tier1",
      reason: "regex:design_word",
      layer: "regex",
      confidence: 1.0,
      latency_ms: 0.2,
    },
  });
  assert.equal(span.attributes["cascade.preflight.tier"], "tier1");
  assert.equal(span.attributes["cascade.preflight.layer"], "regex");
  assert.equal(span.attributes["cascade.tier_chosen_by"], "preflight");
});

test("buildSpan: preflight attributes omitted when router didn't run", () => {
  // Heartbeat / cron / subagent turns — router gate skips them, so the
  // span should NOT carry preflight attributes (distinct from
  // layer="abstain" which means router ran but had no opinion).
  const t = new CascadeTelemetry(
    { sharedDir: "/tmp/x", botId: "team_bot_a" },
    fakeLogger(),
  );
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier3",
    tierIntended: "tier3",
    tierChosenBy: "classifier",
    // No preflight field at all
  });
  assert.equal("cascade.preflight.tier" in span.attributes, false);
  assert.equal("cascade.preflight.layer" in span.attributes, false);
});

test("buildSpan: legacy session_class lands in attributes for read-compat", () => {
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier2",
    tierChosenBy: "classifier",
    legacySessionClass: "productive",
  });

  assert.equal(span.attributes["legacy.session_class"], "productive");
});

test("buildSpan: span is JSON-serializable", () => {
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier2",
    tierIntended: "tier2",
    tierChosenBy: "classifier",
    struggle: {
      score: 0.42,
      features: {
        tool_error_count: 0.25, tool_retry_count: 0.10,
        restart_markers: 0, clarification_loops: 0, tokens_per_progress: 0.07,
      },
      raw: {
        tool_error_count: 2, tool_retry_count: 1,
        restart_markers: 0, clarification_loops: 0, tokens_per_progress: 14000,
      },
    },
  });

  const json = JSON.stringify(span);
  const round = JSON.parse(json);
  assert.equal(round.name, "bot_session_turn");
  assert.equal(round.attributes["cascade.struggle.score"], 0.42);
});

// ── recordTurnSpan — I/O integration ─────────────────────────────────────────

test("recordTurnSpan: writes one JSONL line at the expected path", () => {
  const dir = tmpDir();
  const t = new CascadeTelemetry({ sharedDir: dir, botId: "team_bot_a" }, fakeLogger());

  t.recordTurnSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier2",
    tierChosenBy: "classifier",
  });

  const filePath = path.join(dir, "team_bot_a", "spans", "spans-2026-05-26.jsonl");
  assert.ok(fs.existsSync(filePath), `file not at ${filePath}`);
  const content = fs.readFileSync(filePath, "utf-8");
  assert.ok(content.endsWith("\n"), "file should end with newline");
  const lines = content.trim().split("\n");
  assert.equal(lines.length, 1);
  const span = JSON.parse(lines[0]);
  assert.equal(span.name, "bot_session_turn");
  assert.equal(span.bot_id, "team_bot_a");
});

test("recordTurnSpan: appends multiple turns to the same daily file", () => {
  const dir = tmpDir();
  const t = new CascadeTelemetry({ sharedDir: dir, botId: "team_bot_a" }, fakeLogger());

  for (let i = 1; i <= 3; i++) {
    t.recordTurnSpan({
      sessionId: "sess-1",
      turnIndex: i,
      startedAt: T0,
      endedAt: T1,
      tierUsed: "tier2",
      tierChosenBy: "classifier",
    });
  }

  const filePath = path.join(dir, "team_bot_a", "spans", "spans-2026-05-26.jsonl");
  const lines = fs.readFileSync(filePath, "utf-8").trim().split("\n");
  assert.equal(lines.length, 3);
  const turns = lines.map((l) => JSON.parse(l).attributes.turn_index);
  assert.deepEqual(turns, [1, 2, 3]);
});

test("recordTurnSpan: bucket date follows endedAt UTC, not local clock", () => {
  const dir = tmpDir();
  const t = new CascadeTelemetry({ sharedDir: dir, botId: "team_bot_a" }, fakeLogger());

  // Span ending 2026-05-27 in UTC even if local timezone says 2026-05-26.
  const lateNightStart = new Date("2026-05-27T00:30:00Z");
  const lateNightEnd = new Date("2026-05-27T00:30:01Z");

  t.recordTurnSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: lateNightStart,
    endedAt: lateNightEnd,
    tierUsed: "tier3",
    tierChosenBy: "classifier",
  });

  assert.ok(fs.existsSync(path.join(dir, "team_bot_a", "spans", "spans-2026-05-27.jsonl")));
  assert.ok(!fs.existsSync(path.join(dir, "team_bot_a", "spans", "spans-2026-05-26.jsonl")));
});

test("recordTurnSpan: best-effort — does not throw on permission error", () => {
  // Use a path that can't be created (under /proc which is read-only on
  // Linux; on macOS, /System is similar). We use a non-existent root
  // that mkdir can't reach — this gives ENOENT or EACCES depending on
  // platform. Either way the call should silently swallow.
  const logger = fakeLogger();
  const t = new CascadeTelemetry({ sharedDir: "/var/empty/cannot-write-here", botId: "team_bot_a" }, logger);

  // Should not throw.
  t.recordTurnSpan({
    sessionId: "sess-1",
    turnIndex: 1,
    startedAt: T0,
    endedAt: T1,
    tierUsed: "tier2",
    tierChosenBy: "classifier",
  });

  // No assertion on logger contents — different platforms emit different
  // error codes; the only invariant is "didn't throw."
  assert.ok(true);
});

// ── Code-review regression tests ─────────────────────────────────────────────
// Two cross-module contract bugs found by independent review of the
// Phase 2 shadow-wiring PR. The bare attribute names below are the
// ones the audit-layer Labeler + pressure_watchdog actually read; the
// "shadow_verdict.*" prefixed variants are Phase 2 only.

test("buildSpan: cascade.escalation_event mirror — watchdog reads bare name", () => {
  // BLOCKER from code review: pressure_watchdog.py reads
  // `cascade.escalation_event` but only the shadow_verdict.* prefixed
  // form was being written. Without the mirror, escalations_in_15min /
  // escalation_storm flags can never fire in Phase 2.
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "s1", turnIndex: 1, startedAt: T0, endedAt: T1,
    tierUsed: "tier1", tierIntended: "tier2", tierChosenBy: "classifier",
    shadowVerdictTier: "tier1",
    shadowVerdictEscalationEvent: "escalated",
    shadowVerdictDisagrees: true,
  });
  // Both the shadow-prefixed AND the bare names must be set so Phase 2
  // analyzers (which read the bare form) work without Phase 3 changes.
  assert.equal(span.attributes["cascade.shadow_verdict.escalation_event"], "escalated");
  assert.equal(span.attributes["cascade.escalation_event"], "escalated");
});

test("buildSpan: cascade.escalation_event absent when shadow verdict not passed", () => {
  // Bare mirror must NOT appear when no shadow verdict — back-compat
  // with downstream readers that filter on attribute presence.
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "s1", turnIndex: 1, startedAt: T0, endedAt: T1,
    tierUsed: "tier2", tierIntended: "tier2", tierChosenBy: "classifier",
  });
  assert.equal(span.attributes["cascade.escalation_event"], undefined);
  assert.equal(span.attributes["cascade.shadow_verdict.escalation_event"], undefined);
});

test("buildSpan: consent_source written when present (labeler attribution)", () => {
  // BLOCKER from code review: labeler.py reads `cascade.consent_source`
  // but CascadeTelemetry was never writing it. Signal #1 (UI-chip
  // override) attribution would see undefined for every span.
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  const span = t.buildSpan({
    sessionId: "s1", turnIndex: 1, startedAt: T0, endedAt: T1,
    tierUsed: "tier1", tierIntended: "tier1", tierChosenBy: "user_request",
    consentSource: "ui_chip",
  });
  assert.equal(span.attributes["cascade.consent_source"], "ui_chip");
});

test("buildSpan: consent_source absent when null/undefined", () => {
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  // Null is the documented "no override" value from getConsentSource().
  const span1 = t.buildSpan({
    sessionId: "s1", turnIndex: 1, startedAt: T0, endedAt: T1,
    tierUsed: "tier2", tierIntended: "tier2", tierChosenBy: "classifier",
    consentSource: null,
  });
  assert.equal(span1.attributes["cascade.consent_source"], undefined);
  // Undefined (field omitted entirely) must also be a no-op.
  const span2 = t.buildSpan({
    sessionId: "s1", turnIndex: 1, startedAt: T0, endedAt: T1,
    tierUsed: "tier2", tierIntended: "tier2", tierChosenBy: "classifier",
  });
  assert.equal(span2.attributes["cascade.consent_source"], undefined);
});

test("buildSpan: each consent_source value round-trips", () => {
  // The labeler discriminates by exact string value (ui_chip vs
  // ask_hint_agreed vs bot_initiated). Make sure each survives.
  const t = new CascadeTelemetry({ sharedDir: "/tmp/x", botId: "team_bot_a" }, fakeLogger());
  for (const cs of ["ui_chip", "ask_hint_agreed", "bot_initiated"]) {
    const span = t.buildSpan({
      sessionId: "s1", turnIndex: 1, startedAt: T0, endedAt: T1,
      tierUsed: "tier1", tierIntended: "tier1", tierChosenBy: "user_request",
      consentSource: cs,
    });
    assert.equal(span.attributes["cascade.consent_source"], cs);
  }
});
