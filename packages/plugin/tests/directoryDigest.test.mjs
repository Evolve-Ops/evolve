/**
 * Tests for renderDirectoryDigestBlock — the per-turn directory digest renderer
 * (user-directory Phase 3a).
 *
 * The pure formatter: given the {persons, total, shown, cap} payload the admin daemon
 * returns from GET /api/directory/digest, render the size-bounded block injected into the
 * LLM's system prompt. Covers content, the USER.md-authoritative framing, the size-bound
 * overflow line, and the empty case (so the caller injects nothing).
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/directoryDigest.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { renderDirectoryDigestBlock } from "../dist/util/roleResolver.js";


function row(over) {
  return {
    person_id: "pers_0000000000000001",
    display: "Dana Lopez",
    handle: "@dana",
    identity: "telegram:70000001",
    primary_email: "dana@example.net",
    role: "participant",
    ...over,
  };
}


test("empty digest renders nothing (caller injects nothing)", () => {
  assert.equal(renderDirectoryDigestBlock({ persons: [], total: 0, shown: 0, cap: 25 }), "");
  assert.equal(renderDirectoryDigestBlock(null), "");
  assert.equal(renderDirectoryDigestBlock(undefined), "");
});


test("block is framed as authoritative over USER.md", () => {
  const out = renderDirectoryDigestBlock({
    persons: [row()], total: 1, shown: 1, cap: 25,
  });
  assert.match(out, /AUTHORITATIVE/);
  assert.match(out, /USER\.md/);
  // The instruction must tell the model NOT to invent/hand-type ids.
  assert.match(out, /do not invent or hand-type/i);
});


test("a person line carries display, handle, identity, email, role", () => {
  const out = renderDirectoryDigestBlock({
    persons: [row()], total: 1, shown: 1, cap: 25,
  });
  assert.match(out, /Dana Lopez/);
  assert.match(out, /@dana/);
  assert.match(out, /\[telegram:70000001\]/);
  assert.match(out, /dana@example\.net/);
  assert.match(out, /participant/);
});


test("missing optional fields degrade gracefully (no empty brackets / dangling email)", () => {
  const out = renderDirectoryDigestBlock({
    persons: [row({ handle: "", primary_email: "", identity: "telegram:5" })],
    total: 1, shown: 1, cap: 25,
  });
  assert.match(out, /Dana Lopez/);
  assert.doesNotMatch(out, /@dana/);     // handle omitted
  assert.doesNotMatch(out, /\[\]/);       // never an empty identity bracket
});


test("a contact (no membership) renders role 'contact'", () => {
  const out = renderDirectoryDigestBlock({
    persons: [row({ role: "contact", display: "Ivy Brooks", handle: "" })],
    total: 1, shown: 1, cap: 25,
  });
  assert.match(out, /Ivy Brooks.*· contact/);
});


test("overflow line appears when total exceeds shown rows", () => {
  const persons = [row({ person_id: "pers_1" }), row({ person_id: "pers_2" })];
  const out = renderDirectoryDigestBlock({ persons, total: 12, shown: 2, cap: 2 });
  // 12 total, 2 shown → +10 more, and it points at the lookup tool.
  assert.match(out, /\+10 more/);
  assert.match(out, /directory_lookup/);
});


test("no overflow line when everyone is shown", () => {
  const out = renderDirectoryDigestBlock({
    persons: [row()], total: 1, shown: 1, cap: 25,
  });
  assert.doesNotMatch(out, /more not shown/);
});


test("overflow is computed from rows actually present (defensive against bad shown)", () => {
  // Server says shown:5 but only sent 2 rows — overflow must reflect the 2 real rows.
  const persons = [row({ person_id: "a" }), row({ person_id: "b" })];
  const out = renderDirectoryDigestBlock({ persons, total: 7, shown: 5, cap: 5 });
  assert.match(out, /\+5 more/);  // 7 - 2 present, not 7 - 5
});
