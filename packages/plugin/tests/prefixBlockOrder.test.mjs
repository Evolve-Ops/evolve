/**
 * The block-order pin — internal/finding-cost-forensics-power-bot-2026-09-04
 * §2 lever 3, via internal/spec-context-observability-2026-07-30.md.
 *
 * What is pinned:
 *   1. The concatenation order itself, literally. Reordering the blocks is a
 *      cache decision, so it may only happen by editing this list.
 *   2. The head/tail split: every block that varies between turns is in the
 *      tail, and the tail is a SUFFIX of the order (not a hole in the middle).
 *   3. The head is byte-identical across two compiles separated by a
 *      simulated twenty-minute gap, when only tail inputs changed — the
 *      failing shape the finding measured (a ~45k prefix re-warmed per turn
 *      on turns 10-30 minutes apart).
 *   4. costDowngrade — the most volatile block, present on some runs and
 *      absent on the next — is LAST, not first. It led the pre-2026-09 array
 *      literal, which is the worst available position for it.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/prefixBlockOrder.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  EVOLVE_PREFIX_BLOCK_ORDER,
  EVOLVE_PREFIX_TAIL_BLOCKS,
  composeEvolvePrefix,
  NarrativeStableCache,
  StickyBlockCache,
} from "../dist/observer/BlockStability.js";

const TWENTY_MINUTES_MS = 20 * 60 * 1000;

/**
 * The scheduled-turn tool-discipline block. Fixed per session KIND: the same
 * bytes on every scheduled turn for the life of a plugin version, and absent
 * entirely on user turns. That is why it sits in the head (position 2) rather
 * than the tail — see BlockStability.ts item 2.
 */
const TOOL_DISCIPLINE =
  "[SCHEDULED-TURN TOOL DISCIPLINE]\n  • read before write";

/** The blocks a settled session produces, most-stable first. */
function stableBlocks() {
  return {
    capabilities: "[INSTALLED CAPABILITIES]\n  • skill: crossplay-coach",
    toolDiscipline: TOOL_DISCIPLINE,
    digest: "Roster-verified contacts (AUTHORITATIVE …):\n  • Dana Lopez  @dana",
    narrative: "[CURRENT POD REPORT …]\n\nAll eleven bots healthy.",
    speaker: "SPEAKER (this turn):\n  telegram:1\n  role=admin",
    costDowngrade: "",
  };
}

test("the concatenation order is the contract, literally", () => {
  assert.deepEqual(
    [...EVOLVE_PREFIX_BLOCK_ORDER],
    ["capabilities", "toolDiscipline", "digest", "narrative", "speaker", "costDowngrade"],
    "changing this order changes what the prompt cache can re-read; " +
    "edit deliberately, with the reasoning in BlockStability.ts",
  );
});

test("the tail is a suffix of the order, not a hole in the middle", () => {
  const order = [...EVOLVE_PREFIX_BLOCK_ORDER];
  const suffix = order.slice(order.length - EVOLVE_PREFIX_TAIL_BLOCKS.length);
  assert.deepEqual(suffix, [...EVOLVE_PREFIX_TAIL_BLOCKS]);
});

test("blocks are emitted in contract order regardless of key order", () => {
  const b = stableBlocks();
  // Deliberately built in the OLD (pre-2026-09) order to prove the caller's
  // key order does not decide the wire order.
  const composed = composeEvolvePrefix({
    costDowngrade: "[EVOLVE COST DOWNGRADE] …",
    digest: b.digest,
    toolDiscipline: b.toolDiscipline,
    capabilities: b.capabilities,
    narrative: b.narrative,
    speaker: b.speaker,
  });
  assert.deepEqual(composed.present, [
    "capabilities", "toolDiscipline", "digest", "narrative", "speaker",
    "costDowngrade",
  ]);
  assert.ok(
    composed.text.indexOf(b.capabilities) < composed.text.indexOf(b.speaker),
    "capabilities precedes speaker",
  );
  assert.ok(
    composed.text.endsWith("[EVOLVE COST DOWNGRADE] …"),
    "the most volatile block is last, not first",
  );
});

test("head survives a 20-minute gap when only the tail changed", () => {
  // Turn 1: settled session, no cost downgrade.
  const first = composeEvolvePrefix(stableBlocks());

  // Twenty minutes later — past the 5-minute cache window the finding
  // measured, and past the digest's 3-minute and the capability block's
  // 15-minute TTLs, so both renderers re-ran. Their INPUTS did not change,
  // so their bytes did not either; what changed is who is speaking and that
  // a cost breaker re-routed this run.
  const second = composeEvolvePrefix({
    ...stableBlocks(),
    speaker: "SPEAKER (this turn):\n  telegram:2\n  role=participant",
    costDowngrade: "[EVOLVE COST DOWNGRADE] … routed to a cheaper model …",
  });

  assert.equal(second.head, first.head, "the stable region is byte-identical");
  assert.notEqual(second.tail, first.tail, "the tail is what moved");
  assert.ok(second.text.startsWith(first.head), "the head is still the prefix");
});

test("the tail is the ONLY region whose bytes may differ between compiles", () => {
  const first = composeEvolvePrefix(stableBlocks());
  const second = composeEvolvePrefix({
    ...stableBlocks(),
    speaker: "SPEAKER (this turn):\n  telegram:9\n  role=contact",
  });
  // Strip the tail from both and the remainder must be identical bytes.
  const stripTail = (c) => c.text.slice(0, c.text.length - c.tail.length);
  assert.equal(stripTail(second), stripTail(first));
});

test("a re-rendered but unchanged block does not move the head", () => {
  // The two stability caches, driven across the same simulated gap: the
  // narrative regenerated with a bumped generated_at and identical prose,
  // and the capability renderer's TTL lapsed and re-rendered the same text.
  const narrative = new NarrativeStableCache();
  const caps = new StickyBlockCache(15 * 60 * 1000, 24 * 60 * 60 * 1000);

  const t0 = 1_000_000;
  const capsText = "[INSTALLED CAPABILITIES]\n  • skill: crossplay-coach";
  const first = composeEvolvePrefix({
    capabilities: caps.storeSuccess(capsText, t0),
    narrative: narrative.render("All eleven bots healthy.", "2026-09-04T20:00:00Z"),
    speaker: "SPEAKER (this turn):\n  telegram:1\n  role=admin",
  });

  const t1 = t0 + TWENTY_MINUTES_MS;
  assert.equal(caps.getFresh(t1), null, "the 15m TTL has lapsed by now");
  const second = composeEvolvePrefix({
    capabilities: caps.storeSuccess(capsText, t1),
    narrative: narrative.render("All eleven bots healthy.", "2026-09-04T20:18:00Z"),
    speaker: "SPEAKER (this turn):\n  telegram:1\n  role=admin",
  });

  assert.equal(second.head, first.head);
  assert.equal(second.text, first.text, "nothing meaningful changed, so nothing did");
});

test("absent, empty and whitespace-only blocks all read as absent", () => {
  // Whitespace-only is the case the doc always promised and the filter did
  // not implement: `joinBlocks` tested `s.length > 0`, so a renderer that
  // intermittently returned "   " contributed invisible bytes AND their
  // separators — churning the prefix the rest of this file exists to keep
  // byte-stable. Both `joinBlocks` and `present` now test `s.trim()`.
  const composed = composeEvolvePrefix({
    capabilities: "A",
    digest: "",
    narrative: "   ",
    speaker: "B",
  });
  assert.deepEqual(composed.present, ["capabilities", "speaker"]);
  assert.equal(composed.text, "A\n\nB");
  assert.equal(composed.head, "A");
  assert.equal(composed.tail, "B");
});

test("a block that turns whitespace-only does not change a single byte", () => {
  // The regression this buys: an intermittently-blank block must not be able
  // to break the cache for everything after it.
  const withBlock = composeEvolvePrefix({
    capabilities: "A", narrative: "   ", speaker: "B",
  });
  const withoutBlock = composeEvolvePrefix({ capabilities: "A", speaker: "B" });
  assert.equal(withBlock.text, withoutBlock.text);
  assert.equal(withBlock.head, withoutBlock.head);
  assert.deepEqual(withBlock.present, withoutBlock.present);
});

test("whitespace INSIDE a non-blank block is preserved verbatim", () => {
  // "absent" is about blocks, never about their bytes: trimming a real
  // block's content would change what the model is told.
  const composed = composeEvolvePrefix({ capabilities: "  A  \n" });
  assert.equal(composed.text, "  A  \n");
  assert.deepEqual(composed.present, ["capabilities"]);
});

test("no blocks at all composes to the empty string", () => {
  const composed = composeEvolvePrefix({});
  assert.equal(composed.text, "");
  assert.equal(composed.head, "");
  assert.equal(composed.tail, "");
  assert.deepEqual(composed.present, []);
});

test("toolDiscipline sits in the head, immediately after capabilities", () => {
  // The slot decision, pinned where a future edit has to meet it: the block
  // is fixed per session kind, so it belongs with the stable blocks and not
  // in the tail. Changing this is a cache decision — edit deliberately.
  const order = [...EVOLVE_PREFIX_BLOCK_ORDER];
  assert.equal(order[1], "toolDiscipline");
  assert.equal(order[order.indexOf("toolDiscipline") - 1], "capabilities");
  assert.ok(
    order.indexOf("toolDiscipline") < order.indexOf("digest"),
    "it precedes digest, whose 3-minute TTL churns more often",
  );
  assert.ok(
    !EVOLVE_PREFIX_TAIL_BLOCKS.includes("toolDiscipline"),
    "it is not a tail block",
  );

  const composed = composeEvolvePrefix(stableBlocks());
  assert.ok(
    composed.head.includes(TOOL_DISCIPLINE),
    "and it composes into the head region, not the tail",
  );
});

test("toolDiscipline is byte-stable across a 20-minute gap", () => {
  // A scheduled session compiled twice, twenty minutes apart, with the tail
  // moving underneath it. The block's bytes are a function of the session
  // KIND alone, so the head must not move.
  const first = composeEvolvePrefix(stableBlocks());
  const second = composeEvolvePrefix({
    ...stableBlocks(),
    speaker: "SPEAKER (this turn):\n  telegram:2\n  role=participant",
    costDowngrade: "[EVOLVE COST DOWNGRADE] … routed to a cheaper model …",
  });
  assert.equal(second.head, first.head, "the stable region is byte-identical");
  assert.ok(first.head.includes(TOOL_DISCIPLINE));
  assert.equal(
    first.head.indexOf(TOOL_DISCIPLINE) > first.head.indexOf("[INSTALLED CAPABILITIES]"),
    true,
    "after capabilities",
  );
});

test("a user-turn session drops toolDiscipline without churning the rest", () => {
  // The presence rule does the work: absent on user turns means no block and
  // no separator, so the bytes AFTER it are the bytes a session without the
  // block would have produced anyway. Absence must not be a churn source.
  const scheduled = composeEvolvePrefix(stableBlocks());
  const userTurn = composeEvolvePrefix({ ...stableBlocks(), toolDiscipline: "" });
  const omitted = { ...stableBlocks() };
  delete omitted.toolDiscipline;

  assert.deepEqual(
    userTurn.present,
    ["capabilities", "digest", "narrative", "speaker"],
    "absent on user turns",
  );
  assert.equal(
    userTurn.text,
    composeEvolvePrefix(omitted).text,
    "empty and omitted are the same bytes",
  );
  assert.ok(scheduled.text.includes(TOOL_DISCIPLINE));
  assert.ok(!userTurn.text.includes(TOOL_DISCIPLINE));
  assert.ok(
    userTurn.text.endsWith(userTurn.tail),
    "and the tail is still the suffix",
  );
});
