/**
 * Plugin tool-schema char budgets (overhead-budget Phase D1).
 *
 * Every registered tool's {name, description, parameters} JSON rides in the
 * bot's prompt on every turn (~2.8 chars/token for schemas, calibrated in
 * spec-evolve-overhead-budget §B2). This generalizes podStateTool.test.mjs's
 * B2 regression guard from one tool to the whole surface:
 *
 *   1. EVERY factory's tool def fits a per-tool char budget — no single tool
 *      quietly becomes the next 2,772-char record_application.
 *   2. The per-tier totals (which factories register at monitor/manage/full,
 *      derived from the REAL TIERS capability table in dist/config.js and the
 *      registration gates in src/index.ts) are frozen at their measured
 *      2026-08-01 values, plus the primary-only and Google add-on bundles.
 *
 * The registration profile below MUST mirror src/index.ts — if a new factory
 * is registered there, add it here (the totals will red anyway, which is the
 * point: new tool weight is priced, not silent).
 *
 * Budgets are ceilings frozen at current measured values. A PR that grows one
 * either diets the schema back under, or raises the constant IN THE DIFF with
 * a justification for the extra per-turn weight (the whole-repo counterpart is
 * tools/context-budget-ratchet). Numbers only ratchet down for free.
 *
 * Uses the dist-import approach (B2 pricing pass): run from packages/plugin
 * after `npm run build` (CI's plugin job builds first):
 *   node --test tests/toolSchemaBudget.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { TIERS } from "../dist/config.js";
import { createDeferToolFactory } from "../dist/tools/DeferTool.js";
import { createSetTierToolFactory } from "../dist/tools/SetTierTool.js";
import { createRecordApplicationToolFactory } from "../dist/tools/RecordApplicationTool.js";
import {
  createHelpSearchToolFactory,
  createHelpReadToolFactory,
  createSubmitIntakeToolFactory,
} from "../dist/tools/PrimaryBotTools.js";
import { createPodStateToolFactory } from "../dist/tools/PodStateTools.js";
import {
  createRosterSetRoleToolFactory,
  createRosterBlockToolFactory,
  createRosterUnblockToolFactory,
  createChannelSetNewcomerModeToolFactory,
} from "../dist/tools/RosterTools.js";
import {
  createDirectoryLookupToolFactory,
  createDirectoryUpsertToolFactory,
} from "../dist/tools/DirectoryTools.js";
import { createExpandAppToolFactory } from "../dist/tools/ExpandAppTool.js";
import { createGoogleToolFactories } from "../dist/tools/GoogleTools.js";

// ── Budgets (chars; measured 2026-08-01, spec-evolve-overhead-budget D1) ────

// Per-tool ceiling — the podStateTool guard's number, now applied to every
// factory. Current worst: record_application at 2,772.
const PER_TOOL_BUDGET = 3000;

// Per-tier member-bot totals (no Google, non-primary): the factories
// registered unconditionally for an active tier, plus the tier's
// capability-gated ones (TIERS × the src/index.ts registration gates).
// 2026-08-03: manage/full +763 — session_set_tier grew a `scope` param
// (standing per-user default, G4 of the spec-user-tier-control 2026-08-03
// addendum). ~273 tokens/turn at 2.8 chars/token, only on modelRouting
// tiers; the scope-routing rule must live in the schema for the bot to
// route "always ..." intent to the standing write instead of a session pin.
const TIER_TOTAL_BUDGET = { monitor: 8296, manage: 13893, full: 15347 };

// Role/config add-on bundles, budgeted separately so growth is attributed to
// the surface that pays for it (only primaries carry the primary bundle; only
// Google-configured bots carry the Google bundle — spec §A3 measured it as
// the dominant member-bot schema cost).
const PRIMARY_BUNDLE_BUDGET = 6207;
// 2026-08-31: 6534 → 6811 (+277) — gmail_send grew an `attachments` param
// (#3905: array-of-paths schema + the workspace-confinement/20MB wording the
// bot needs to route attach intent safely). ~99 tokens/turn at 2.8
// chars/token, Google-configured bots only. #3905 shipped src without a
// rebuilt dist, so CI (which measures the committed dist) never saw the
// growth while every deployed pod (which compiles src) already carried it;
// #3909 committed the rebuilt dist and priced it at 6811.
// 2026-08-31: 6811 → 6724 (−87) — dieted the duplication: the constraint
// was stated in both the tool description and the param description; it now
// lives once, on the param, and the tool description just names the
// optional params.
const GOOGLE_BUNDLE_BUDGET = 6724;

// ── Instantiate every factory exactly as src/index.ts registers it ──────────

const quiet = { warn() {}, info() {}, debug() {}, error() {} };
const sock = "/tmp/evolve-schema-budget-test.sock";
const cfg = { sharedDir: "/tmp", botId: "budget-test-bot" };

function chars(def) {
  // The same payload ToolFootprint records and podStateTool.test.mjs guards.
  return JSON.stringify({
    name: def.name,
    description: def.description,
    parameters: def.parameters,
  }).length;
}

const def = (factory) => factory({});

// Registered for every active-tier bot (see src/index.ts: RosterTools,
// DirectoryTools, ExpandAppTool are unconditional once the plugin is live).
const alwaysOn = [
  def(createRosterSetRoleToolFactory({ botId: cfg.botId, socketPath: sock }, quiet)),
  def(createRosterBlockToolFactory({ botId: cfg.botId, socketPath: sock }, quiet)),
  def(createRosterUnblockToolFactory({ botId: cfg.botId, socketPath: sock }, quiet)),
  def(createChannelSetNewcomerModeToolFactory({ botId: cfg.botId, socketPath: sock }, quiet)),
  def(createDirectoryLookupToolFactory(cfg, quiet)),
  def(createDirectoryUpsertToolFactory(cfg, quiet)),
  def(createExpandAppToolFactory({ ...cfg, socketPath: sock }, quiet)),
];

// Capability-gated (src/index.ts gates each on config.capabilities.<key>).
const gated = {
  modelRouting: def(createSetTierToolFactory({ botId: cfg.botId, modelRouter: {} }, quiet)),
  recordApplicationTool: def(createRecordApplicationToolFactory({ botId: cfg.botId }, quiet)),
  deferTool: def(createDeferToolFactory({ botId: cfg.botId }, quiet)),
};

// Primary-role-only bundle.
const primaryBundle = [
  def(createHelpSearchToolFactory(quiet, sock)),
  def(createHelpReadToolFactory(quiet, sock)),
  def(createSubmitIntakeToolFactory({ botId: cfg.botId, socketPath: sock }, quiet)),
  def(createPodStateToolFactory(quiet, sock)),
];

// Google-config-gated bundle.
const googleBundle = createGoogleToolFactories(cfg, quiet).map(def);

const allDefs = [
  ...alwaysOn,
  ...Object.values(gated),
  ...primaryBundle,
  ...googleBundle,
];

const sum = (defs) => defs.reduce((n, d) => n + chars(d), 0);

function memberTierTotal(tier) {
  const caps = TIERS[tier];
  let total = sum(alwaysOn);
  for (const [capability, toolDef] of Object.entries(gated)) {
    if (caps[capability]) total += chars(toolDef);
  }
  return total;
}

// ── The budgets ─────────────────────────────────────────────────────────────

test("every tool def fits the per-tool schema budget", () => {
  const over = allDefs
    .map((d) => ({ name: d.name, chars: chars(d) }))
    .filter((t) => t.chars > PER_TOOL_BUDGET);
  assert.deepEqual(
    over,
    [],
    `tool schema(s) over the ${PER_TOOL_BUDGET}-char per-tool budget: ` +
      `${JSON.stringify(over)}. Diet the description/params (constraints stay, ` +
      "narrative goes) rather than raising the budget.",
  );
});

test("per-tier member totals stay within their frozen budgets", () => {
  for (const tier of ["monitor", "manage", "full"]) {
    const total = memberTierTotal(tier);
    assert.ok(
      total <= TIER_TOTAL_BUDGET[tier],
      `tier=${tier} member tool-schema total is ${total} chars — over its ` +
        `${TIER_TOTAL_BUDGET[tier]}-char budget. A new/regrown tool must be ` +
        "priced: diet a schema, or raise the budget in this diff with the " +
        "per-turn cost justified (spec-evolve-overhead-budget D1).",
    );
  }
});

test("primary-only bundle stays within its frozen budget", () => {
  const total = sum(primaryBundle);
  assert.ok(
    total <= PRIMARY_BUNDLE_BUDGET,
    `primary bundle is ${total} chars — over its ${PRIMARY_BUNDLE_BUDGET}-char ` +
      "budget (every primary prompt carries this on top of the tier total).",
  );
});

test("Google bundle stays within its frozen budget", () => {
  const total = sum(googleBundle);
  assert.ok(
    total <= GOOGLE_BUNDLE_BUDGET,
    `Google bundle is ${total} chars — over its ${GOOGLE_BUNDLE_BUDGET}-char ` +
      "budget (spec §A3: the dominant schema cost on a Google-configured bot).",
  );
});

test("tier capability table still drives the gated set", () => {
  // If TIERS gains a capability that src/index.ts uses to gate a NEW factory,
  // this file must learn it — fail loudly instead of silently under-counting.
  const known = new Set([
    "observer", "injectPodConduct", "injectKeywords",
    "modelRouting", "deferTool", "recordApplicationTool",
  ]);
  for (const caps of Object.values(TIERS)) {
    for (const key of Object.keys(caps)) {
      assert.ok(
        known.has(key),
        `unknown tier capability '${key}' — if it gates a tool factory in ` +
          "src/index.ts, register it in this test's `gated` profile.",
      );
    }
  }
});
