/**
 * Regression test for the 2026-06-07 preflight session-key bug.
 *
 * BUG: TurnObserver stored the preflight decision in the local map
 * keyed by `sessionKey` (OC envelope key, e.g.
 * "agent:main:telegram:direct:<chatId>"), but the agent_end span writer
 * looked it up by `sessionId` (UUID). They never matched — the decision
 * was stored and immediately orphaned. Every span ended up with
 * `cascade.preflight.layer` absent, indistinguishable from "preflight
 * never ran." Confirmed on the live pod via one bot's spans on
 * 2026-06-07: 0 of 20 spans showed preflight attributes despite the
 * gateway running freshly-built Phase 3 plugin code.
 *
 * FIX: TurnObserver-local map keyed by sessionId (matches sessionTurns
 * + sessionLlmData conventions); ModelRouter map stays keyed by
 * sessionKey (matches sessionCascadeVerdicts + how OC calls
 * resolveModelOverride(sessionKey)). Both keys derived in
 * handleBeforeModelResolve, one for each consumer.
 *
 * This test is a source-grep guard. The bug is structurally hard to
 * surface in a unit test because handleBeforeModelResolve is a
 * heavy-dependency method (it accesses BetterEngine, ModelRouter,
 * etc.). A grep against the compiled dist file pins the contract
 * cheaply and would catch a future "let's unify these keys" edit
 * that re-introduces the mismatch in the wrong direction.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/preflightSessionKeyConsistency.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const TURN_OBSERVER_SRC = path.join(
  __dirname, "..", "src", "observer", "TurnObserver.ts",
);

const src = fs.readFileSync(TURN_OBSERVER_SRC, "utf8");

test("preflight: local map storage uses sessionId (NOT sessionKey)", () => {
  // The storage site at before_model_resolve MUST key by sessionId.
  // A future regression that switches it to sessionKey breaks the
  // agent_end-side span writer that uses sessionId for lookups.
  const localSetMatch = src.match(
    /this\._sessionPreflightDecisions\.set\(\s*sessionId\s*,/,
  );
  assert.ok(
    localSetMatch,
    "Expected TurnObserver to store preflight decision keyed by sessionId. " +
    "If you changed the storage key, also update the agent_end-side lookup OR " +
    "this test (after confirming the new key is what agent_end reads).",
  );
  // Conversely: storing under sessionKey would be the 2026-06-07 bug.
  // This negative assert catches a half-revert where someone changes
  // storage back to sessionKey without updating the lookup.
  const badPattern = /this\._sessionPreflightDecisions\.set\(\s*sessionKey\s*,/;
  assert.ok(
    !badPattern.test(src),
    "Found `_sessionPreflightDecisions.set(sessionKey, ...)` — this is the " +
    "2026-06-07 regression. The agent_end lookup uses sessionId; storing " +
    "under sessionKey leaves the decision permanently orphaned.",
  );
});

test("preflight: agent_end lookup uses sessionId (matches local storage)", () => {
  const localGetMatch = src.match(
    /this\._sessionPreflightDecisions\.get\(\s*sessionId/,
  );
  assert.ok(
    localGetMatch,
    "Expected agent_end span writer to look up preflight decision by " +
    "sessionId. Symmetric requirement to the storage test above.",
  );
});

// ── Related fix 2026-06-08: chosen_by attribution lookups must use sessionKey ──
//
// Second occurrence of the same bug class. The kitchen-TV cost incident
// (2026-06-07) traced to TurnObserver calling
//   `this.modelRouter.getLastDecisionDriver(sessionId)`
// at agent_end time. ModelRouter's sessionLastDecisionDriver map (along
// with sessionUserTiers and sessionConsentSources) is keyed by
// sessionKey — the same convention this whole consistency-check file
// pins. So the lookup with sessionId returned null for every session,
// _computeChosenBy fell through to the legacy heuristic, and every
// pre-flight-driven decision was misattributed as `tier_chosen_by:
// "default"`.
//
// Downstream effect: the `preflight_over_escalation` audit Signal
// counts decisions by tier_chosen_by == "preflight". When that tag
// is wrong, the Signal can never fire — the audit layer thinks
// preflight never ran at all. Two bugs in series silently blinded
// the cost-incident detection path.

test("chosen_by: getLastDecisionDriver call uses sessionKey (NOT sessionId)", () => {
  // The ModelRouter map is sessionKey-keyed. Looking up by sessionId
  // returns null for every session, which causes _computeChosenBy to
  // fall through to its legacy heuristic and stamp "default" on every
  // preflight-driven span.
  const good = /getLastDecisionDriver\(\s*ctxSessionKey/;
  const bad = /getLastDecisionDriver\(\s*sessionId\s*\)/;
  assert.ok(
    good.test(src),
    "Expected TurnObserver to call getLastDecisionDriver(ctxSessionKey). " +
    "If you changed this back to sessionId, the chosen_by attribution " +
    "is broken (kitchen-TV cost incident 2026-06-07).",
  );
  assert.ok(
    !bad.test(src),
    "Found `getLastDecisionDriver(sessionId)` — this is the 2026-06-07 " +
    "attribution bug. ModelRouter's map is sessionKey-keyed; sessionId " +
    "always returns null and the span misattributes to 'default'.",
  );
});

test("chosen_by: getUserTier call uses sessionKey (NOT sessionId)", () => {
  // Same map convention. getUserTier reads sessionUserTiers which is
  // populated by the routing path keyed by sessionKey.
  const good = /getUserTier\(\s*ctxSessionKey/;
  const bad = /getUserTier\(\s*sessionId\s*\)/;
  assert.ok(
    good.test(src),
    "Expected TurnObserver to call getUserTier(ctxSessionKey).",
  );
  assert.ok(
    !bad.test(src),
    "Found `getUserTier(sessionId)` — same sessionKey vs sessionId " +
    "bug class as the 2026-06-07 kitchen-TV incident.",
  );
});

test("chosen_by: getConsentSource call uses sessionKey (NOT sessionId)", () => {
  // Same again — sessionConsentSources is populated alongside
  // sessionUserTiers in the routing path, both keyed by sessionKey.
  const good = /getConsentSource\(\s*ctxSessionKey/;
  const bad = /getConsentSource\(\s*sessionId\s*\)/;
  assert.ok(
    good.test(src),
    "Expected TurnObserver to call getConsentSource(ctxSessionKey).",
  );
  assert.ok(
    !bad.test(src),
    "Found `getConsentSource(sessionId)` — same sessionKey vs sessionId " +
    "bug class.",
  );
});

test("preflight: ModelRouter map storage uses sessionKey", () => {
  // ModelRouter's per-session maps (sessionUserTiers, sessionCascadeVerdicts,
  // etc.) are all keyed by sessionKey because OC calls
  // resolveModelOverride(sessionKey) — the preflight slot follows the
  // same convention. Test pins the asymmetry: ModelRouter side =
  // sessionKey, TurnObserver-local side = sessionId.
  const mrSetMatch = src.match(
    /this\.modelRouter\.setSessionPreflightDecision\(\s*sessionKey\s*,/,
  );
  assert.ok(
    mrSetMatch,
    "Expected ModelRouter.setSessionPreflightDecision to be called with " +
    "sessionKey at before_model_resolve. Other ModelRouter session-state " +
    "maps follow this convention; the preflight one must too.",
  );
});

test("preflight: cleanup deletes from BOTH maps on agent_end", () => {
  // Defensive: a stale decision in either map could leak into a future
  // turn if before_model_resolve fails to fire (e.g., hook unregistered
  // mid-session). The cleanup at agent_end must clear both.
  const localDeleteMatch = src.match(
    /this\._sessionPreflightDecisions\.delete\(\s*sessionId/,
  );
  assert.ok(
    localDeleteMatch,
    "Expected agent_end cleanup to delete from local map by sessionId.",
  );
  // The ModelRouter map is keyed by a sessionKey-derived identifier
  // (storage was under sessionKey; clearing by sessionId would silently
  // leak). Accept any identifier whose name ends in "Key" (sessionKey,
  // cleanupSessionKey, _sessionKey) — the literal symbol may differ
  // depending on local-variable choice, but the suffix is the contract.
  const mrClearMatch = src.match(
    /this\.modelRouter\.setSessionPreflightDecision\(\s*[A-Za-z_]*[Kk]ey\s*,\s*null/,
  );
  assert.ok(
    mrClearMatch,
    "Expected agent_end cleanup to clear ModelRouter's preflight slot " +
    "via a *Key-suffixed identifier (not sessionId). Storage is keyed by " +
    "sessionKey; clearing by sessionId silently leaks the decision.",
  );
});
