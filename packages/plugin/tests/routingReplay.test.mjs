/**
 * Replay proof for the routing rule (D-OH2 —
 * internal/decision-evolve-overhead-2026-09-07.md).
 *
 * THE STANDING RULE: nothing in the overhead work changes which model
 * answers a turn. This test is the evidence for that claim, not a
 * restatement of it.
 *
 * Method: take a fortnight of one bot's turns in the shape
 * ``TurnObserver.writeTurnToShared`` writes them (source, channel,
 * model), replay ``decideRoutingRule`` over each, and compare the rung
 * the rule picks against the rung the model that ACTUALLY answered
 * belongs to. Where the rule defers (``role: null`` — a user turn with
 * no confident prior) the comparison uses the bot's primary, because
 * that is what the ladder below the rule produces.
 *
 * Two assertions carry the claim:
 *
 *   1. Every clock- or system-fired turn agrees, exactly. These are the
 *      turns the rule actually decides, and the whole measurable
 *      benefit of the retired machinery (~$3-4/day, decision §2) lives
 *      in this row.
 *
 *   2. No user turn is DOWNGRADED. A disagreement where the recorded
 *      rung is ABOVE the primary is an operator pull, a cascade
 *      escalation, or a breaker — all of which sit above the rule in
 *      ``_resolveModelAndTier`` and are untouched by this work, and none
 *      of which the turns file records a driver for. A disagreement in
 *      the other direction would mean the rule silently moved a
 *      person's turn to a cheaper model, which is the one outcome the
 *      standing rule forbids.
 *
 * The agreement table is printed so the PR body can quote it.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/routingReplay.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import { decideRoutingRule } from "../dist/observer/routingRule.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE = JSON.parse(
  fs.readFileSync(path.join(HERE, "fixtures", "routing-replay.json"), "utf8"),
);

// ── Local mirrors of the two hot-path helpers ───────────────────────────────

/**
 * ``TurnObserver.inferTriggerKind``, over a recorded turn. Kept local
 * (it is not exported) and deliberately minimal — the fixture only
 * carries source/channel, which is what the live call reads first.
 */
function inferTriggerKind(source, channel) {
  const src = String(source ?? "").toLowerCase();
  const ch = String(channel ?? "").toLowerCase();
  if (src === "human" || src === "user") return "user_turn";
  if (src === "heartbeat") return "heartbeat";
  if (src === "cron") return "cron_app";
  if (src === "subagent") return "subagent";
  if (["summarizer", "classifier", "task_extractor", "fallback"].includes(src)) return src;
  if (ch === "heartbeat") return "heartbeat";
  if (ch === "cron-event" || ch === "cron") return "cron_app";
  if (ch === "subagent" || ch === "exec-event") return "subagent";
  return "unknown";
}

/** ``ModelRouter.getRoleForModel``, over the fixture's rung config. */
function roleForModel(model, models) {
  const rungs = Object.fromEntries((models.rungs ?? []).map((r) => [r.id, r.models ?? []]));
  for (const [role, slug] of Object.entries(models.roles ?? {})) {
    if ((rungs[slug] ?? []).some((m) => m.toLowerCase() === String(model).toLowerCase())) {
      return role;
    }
  }
  return null;
}

const ROLE_ORDER = { fast: 0, standard: 1, power: 2, max: 3 };

// ── The replay ──────────────────────────────────────────────────────────────

function replay() {
  const { turns, models, primaryRole } = FIXTURE;
  const bySource = new Map();
  const disagreements = [];

  for (const turn of turns) {
    const triggerKind = inferTriggerKind(turn.source, turn.channel);
    const decision = decideRoutingRule({
      triggerKind,
      surface: turn.channel,
      // The fixture bot has no learned prior — this is the pre-prior
      // world, which is exactly the one the standing rule is about.
      prior: null,
      backgroundRole: models.routing?.backgroundRole,
      maintenanceRole: models.routing?.maintenanceRole,
    });
    const ruleRole = decision.role ?? primaryRole;
    const recordedRole = roleForModel(turn.model, models);

    const row = bySource.get(turn.source) ?? { total: 0, agree: 0, driver: decision.driver };
    row.total += 1;
    if (ruleRole === recordedRole) row.agree += 1;
    else {
      disagreements.push({
        source: turn.source,
        channel: turn.channel,
        ts: turn.ts,
        recordedRole,
        ruleRole,
        driver: decision.driver,
        direction: ROLE_ORDER[recordedRole] > ROLE_ORDER[ruleRole] ? "above" : "below",
      });
    }
    bySource.set(turn.source, row);
  }
  return { bySource, disagreements };
}

test("replay: the rule agrees with recorded routing on every clock-fired turn", () => {
  const { bySource } = replay();
  for (const [source, row] of bySource) {
    if (source === "user") continue;
    assert.equal(
      row.agree, row.total,
      `${source}: ${row.agree}/${row.total} agree — a clock- or system-fired ` +
      `turn is decided by the rule alone, so every one of them must match`,
    );
  }
});

test("replay: no user turn is downgraded by the rule", () => {
  const { disagreements } = replay();
  const downgrades = disagreements.filter(
    (d) => d.source === "user" && d.direction === "below",
  );
  assert.deepEqual(
    downgrades, [],
    "the rule must never move a person's turn to a cheaper model — " +
    "that is the standing rule, stated as an assertion",
  );
});

test("replay: every disagreement is a rung ABOVE the primary, and explained", () => {
  const { disagreements } = replay();
  for (const d of disagreements) {
    assert.equal(d.direction, "above");
    assert.equal(
      d.driver, "primary",
      "a disagreement can only happen on a turn the rule declined to " +
      "decide — anything else would mean the rule itself moved it",
    );
  }
});

test("replay: prints the agreement table", () => {
  const { bySource, disagreements } = replay();
  const lines = [
    "",
    `Replay: ${FIXTURE.turns.length} turns over ${FIXTURE.windowDays} days ` +
    `(fixture bot ${FIXTURE.botId}, primary=${FIXTURE.primaryRole})`,
    "",
    "| source    | turns | rule driver | agreement |",
    "|-----------|-------|-------------|-----------|",
  ];
  for (const [source, row] of [...bySource].sort()) {
    const pct = ((row.agree / row.total) * 100).toFixed(1);
    lines.push(
      `| ${source.padEnd(9)} | ${String(row.total).padStart(5)} | ` +
      `${row.driver.padEnd(11)} | ${row.agree}/${row.total} (${pct}%) |`,
    );
  }
  lines.push("");
  if (disagreements.length === 0) {
    lines.push("Disagreements: none.");
  } else {
    lines.push(`Disagreements (${disagreements.length}), with the reason:`);
    for (const d of disagreements) {
      lines.push(
        `  ${d.ts}  ${d.source}/${d.channel}: recorded ${d.recordedRole}, ` +
        `rule ${d.ruleRole} — the rule declined to decide this turn ` +
        `(driver=${d.driver}); a rung ABOVE the primary means an operator ` +
        `pull, a cascade escalation or a breaker, all of which sit above ` +
        `the rule in the ladder and are untouched.`,
      );
    }
  }
  console.log(lines.join("\n"));

  // The table is the deliverable; this pins the shape so a fixture that
  // silently loses its auto rows cannot make the proof vacuous.
  assert.ok(bySource.has("heartbeat"), "the corpus must contain heartbeats");
  assert.ok(bySource.has("cron"), "the corpus must contain cron turns");
  assert.ok(bySource.has("user"), "the corpus must contain user turns");
});

// ── Evolve-internal model calls per user turn ───────────────────────────────

test("replay: the rule makes zero model calls on the hot path", () => {
  // decideRoutingRule is pure — no fs, no network, no subagent. This
  // pins the property that the whole decision rests on: the routing
  // answer for every turn above was produced without Evolve calling a
  // model, which is the "0 on the hot path" target in the brief.
  const src = fs.readFileSync(
    path.join(HERE, "..", "src", "observer", "routingRule.ts"), "utf8",
  );
  // Import lines are the honest test: a pure function cannot reach a
  // model or the filesystem without importing something that can.
  const imports = src
    .split("\n")
    .filter((l) => /^\s*import\b/.test(l))
    .join("\n");
  assert.equal(
    imports.trim(), "",
    `routingRule.ts must import nothing — it decides from its arguments, ` +
    `with no I/O and no model call. Found:\n${imports}`,
  );
  for (const forbidden of ["runPinnedSubagent(", "waitForRun", "fetch(", "readFileSync"]) {
    assert.ok(
      !src.includes(forbidden),
      `routingRule.ts must not call ${forbidden}`,
    );
  }
});
