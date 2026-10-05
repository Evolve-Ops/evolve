"""tests/test_negative_only_pins.py — the criterion has to survive the ways it
was got wrong.

`tools/negative-only-pins` reports test subjects that nothing pins positively:
a subject whose every assertion is `is None` / `=== undefined` passes just as
well against a version that resolves NOTHING. The criterion is easy to state
and, as 2026-09-17 demonstrated three times in one afternoon, easy to
implement wrongly:

  draft 1 (an ad-hoc regex)  saw only calls written INSIDE the assertion, so
      `const r = f(); assert.deepEqual(r, {...})` was invisible → NINE
      findings in the plugin suite, EIGHT of them wrong.
  draft 2  attributed negatives to the test file's own fixture builders
      (`mkConfig`, `fakeRegistry`) — true, and useless: a helper is not a
      control.
  draft 3  followed bindings one hop, so `const h = makeHandler(); const out =
      h(ev); assert.ok(out.result)` still read as unpinned.

Each of those is a test below. The point is not the script — it is that a
criterion nobody can re-derive correctly has to be checked in WITH the cases
that broke it, or the next run produces another list of nine.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

_TOOL = Path(__file__).resolve().parents[3] / "tools" / "negative-only-pins"


def _load():
    loader = importlib.machinery.SourceFileLoader("negative_only_pins", str(_TOOL))
    spec = importlib.util.spec_from_loader("negative_only_pins", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["negative_only_pins"] = mod
    loader.exec_module(mod)
    return mod


nop = _load()


def _js(tmp_path: Path, body: str) -> list[dict]:
    p = tmp_path / "x.test.mjs"
    p.write_text(body, encoding="utf-8")
    return nop.survey([p], nop._scan_js)


def _subjects(rows) -> set[str]:
    return {r["subject"] for r in rows}


HEADER = 'import { subject, other } from "../dist/thing.js";\n'


def test_a_subject_with_only_null_assertions_is_reported(tmp_path):
    rows = _js(tmp_path, HEADER + '''
test("refuses junk", () => {
  assert.equal(subject("junk"), null);
});
''')
    assert _subjects(rows) == {"subject"}
    assert rows[0]["negative_tests"] == 1


def test_a_positive_written_inline_clears_it(tmp_path):
    rows = _js(tmp_path, HEADER + '''
test("refuses junk", () => {
  assert.equal(subject("junk"), null);
});
test("resolves a real one", () => {
  assert.equal(subject("real"), "answer");
});
''')
    assert _subjects(rows) == set()


def test_a_positive_through_a_result_variable_clears_it(tmp_path):
    """Draft 1's defect, and the suite's dominant style: the call is on one
    line and the assertion is on the next."""
    rows = _js(tmp_path, HEADER + '''
test("refuses junk", () => {
  assert.equal(subject("junk"), null);
});
test("resolves a real one", () => {
  const result = subject("real");
  assert.deepEqual(result, { role: "power" });
});
''')
    assert _subjects(rows) == set()


def test_a_positive_two_hops_away_clears_it(tmp_path):
    """Draft 3's defect: a factory, its product, and an assertion on the
    product's field."""
    rows = _js(tmp_path, HEADER + '''
test("void passthrough", () => {
  const h = subject(registry());
  assert.equal(h(ev), undefined);
});
test("replaces the result", () => {
  const h = subject(registry());
  const out = h(ev);
  assert.ok(out && out.result);
});
''')
    assert _subjects(rows) == set()


def test_a_test_local_helper_is_never_a_finding(tmp_path):
    """Draft 2's defect. `mkConfig` is a fixture builder, not a control, and
    nobody should add an assertion about one."""
    rows = _js(tmp_path, HEADER + '''
function mkConfig(x) { return { x }; }
test("refuses", () => {
  assert.equal(subject(mkConfig(1)), null);
});
test("resolves", () => {
  assert.equal(subject(mkConfig(2)), "answer");
});
''')
    assert _subjects(rows) == set()


def test_an_aliased_import_is_the_same_subject(tmp_path):
    """The suite imports one function under a leading underscore in one file.
    Two spellings of one name must not read as one pinned and one not."""
    p1 = tmp_path / "a.test.mjs"
    p1.write_text('import { subject } from "../dist/thing.js";\n'
                  'test("refuses", () => { assert.equal(subject("junk"), null); });\n',
                  encoding="utf-8")
    p2 = tmp_path / "b.test.mjs"
    p2.write_text('import { subject as _subject } from "../dist/thing.js";\n'
                  'test("resolves", () => { assert.equal(_subject("real"), "answer"); });\n',
                  encoding="utf-8")
    assert _subjects(nop.survey([p1, p2], nop._scan_js)) == set()


def test_a_constructor_is_not_a_subject(tmp_path):
    """`new Thing(...)` yields an instance; what a test pins is what its
    METHODS answer. Reporting the class was the last of the three artifacts."""
    rows = _js(tmp_path, 'import { Thing } from "../dist/thing.js";\n' + '''
test("refuses", () => {
  const t = new Thing({});
  assert.equal(t.handle("junk"), null);
});
''')
    assert _subjects(rows) == set()


def test_a_name_from_a_non_relative_import_is_not_a_subject(tmp_path):
    """`assert` itself, and anything else out of node:/site-packages, is not
    the code under test."""
    rows = _js(tmp_path, 'import assert from "node:assert/strict";\n' + '''
test("refuses", () => {
  assert.equal(assert.somethingOdd("x"), null);
});
''')
    assert _subjects(rows) == set()


def test_the_repo_itself_has_no_unpinned_plugin_subject(tmp_path):
    """The live claim, checked rather than asserted in prose: after
    `priorRoleForSurface` gained its positive controls, the plugin suite has
    none. If this ever reds, read the named subject's tests — a hit is a
    question, not a verdict."""
    tests = sorted((_TOOL.parent.parent / "packages" / "plugin" / "tests").glob("*.mjs"))
    assert tests, "no plugin tests found — the scan would be vacuously green"
    rows = nop.survey(tests, nop._scan_js)
    assert rows == [], "subjects pinned only negatively: %s" % _subjects(rows)
