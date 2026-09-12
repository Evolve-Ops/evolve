"""Unit tests for tools/meta-inflight.

`tools/meta-inflight` is the dispatch-time "who's already on this?" overlap check
(META:substrate Initiative 6). Before any `/launch` or `/design` spawn, it scans the
non-terminal chips in every ledger, the open fleet PRs, and (when supplied) live
`[META:*]` sessions, then matches them against the work's aspect + keywords + scope
globs and ranks the overlaps by match strength.

These tests pin the build rules against crafted fixtures so a rule can't silently
drift: the terminal-chip exclusion, the scope/keyword/aspect match tiers + ranking,
the "aspect-only doesn't flood a specific query" surface rule, cross-aspect file
collisions surfacing, the fleet-PR filter (`claude/*` branch OR `[META:<id>]` title),
PR↔chip dedup, session parsing, and the offline CLI/JSON path. The `gh` call is never
exercised — PRs and sessions are injected, the ledgers come from a tmp fixture dir.

The tool is an extensionless script under tools/, so we load it by path (it in turn
loads its sibling tools/meta-queue the same way, to reuse the ledger resolver).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

_TOOL = Path(__file__).resolve().parents[3] / "tools" / "meta-inflight"


def _load_tool():
    loader = importlib.machinery.SourceFileLoader("meta_inflight", str(_TOOL))
    spec = importlib.util.spec_from_loader("meta_inflight", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meta_inflight"] = mod
    loader.exec_module(mod)
    return mod


mi = _load_tool()


# ── fixture ledgers ────────────────────────────────────────────────────────
# Two aspects. `alpha` exercises every chip bucket + a backlog item; `beta` shares
# one file with `alpha` (the cross-aspect file collision) and one unrelated chip.

LEDGER_ALPHA = {
    "aspect": "alpha",
    "updated": "2026-06-15",
    "chips": [
        {"id": "D1", "title": "dispatch-time collision check", "bucket": "dispatched",
         "task_id": "task_aaa", "pr": None,
         "scope": ["tools/meta-inflight", "internal/spec-substrate-2026-06-15.md"],
         "note": "the I6 build"},
        {"id": "O1", "title": "queue projection tweak", "bucket": "open_green",
         "pr": 3010, "scope": ["tools/meta-queue"]},
        {"id": "S1", "title": "stalled usage edit", "bucket": "stalled", "pr": 3011,
         "scope": ["packages/admin/web/usage.js"]},
        {"id": "B1", "title": "blocked dep", "bucket": "blocked", "pr": None,
         "scope": ["docs/unrelated.md"]},
        # Terminal — MUST never surface even though it touches tools/meta-inflight.
        {"id": "DONE1", "title": "done meta-inflight chip", "bucket": "done",
         "pr": 3000, "scope": ["tools/meta-inflight"]},
        {"id": "MERGED1", "title": "merged queue chip", "bucket": "merged",
         "pr": 2999, "scope": ["tools/meta-queue"]},
    ],
    "backlog": [
        "B-next inflight backlog idea touching collision detection",
        "unrelated backlog item",
    ],
}

LEDGER_BETA = {
    "aspect": "beta",
    "updated": "2026-06-16",
    "chips": [
        # Cross-aspect file collision with alpha S1 (same file, different aspect).
        {"id": "X1", "title": "beta editing usage page", "bucket": "dispatched",
         "pr": 3012, "scope": ["packages/admin/web/usage.js"]},
        {"id": "Y1", "title": "beta server work", "bucket": "dispatched", "pr": 3013,
         "scope": ["packages/server/foo.py"]},
    ],
}


def _write_fixture(tmp_path):
    d = tmp_path / "meta-state"
    d.mkdir()
    (d / "alpha.json").write_text(json.dumps(LEDGER_ALPHA))
    (d / "beta.json").write_text(json.dumps(LEDGER_BETA))
    (d / "_README.md").write_text("not a ledger")  # must be ignored
    return d


def _ledgers(tmp_path):
    return mi.load_ledgers(_write_fixture(tmp_path))


def _ids(overlaps):
    """A stable handle per overlap: chip/session id, PR number, or backlog title."""
    out = []
    for o in overlaps:
        out.append(o.get("id") or o.get("pr") or o.get("title"))
    return out


# ── glob overlap unit ─────────────────────────────────────────────────────────


def test_globs_overlap_tiers():
    assert mi.globs_overlap("tools/meta-inflight", "tools/meta-inflight") == "strong"
    assert mi.globs_overlap("tools/meta-inflight", "tools/meta-*") == "strong"
    assert mi.globs_overlap("packages/admin/web/usage.js", "packages/admin/web/**") == "strong"
    # glob-vs-glob, same wildcard dir, different extension → soft dir overlap.
    assert mi.globs_overlap("packages/admin/web/*.css", "packages/admin/web/*.js") == "dir"
    # two distinct concrete files → not a collision.
    assert mi.globs_overlap("docs/a.md", "tools/b") == ""
    assert mi.globs_overlap("", "tools/x") == ""


def test_aspect_from_title():
    assert mi._aspect_from_title("[META:substrate] do a thing") == "substrate"
    assert mi._aspect_from_title("META platform") == "platform"
    assert mi._aspect_from_title("[META:model-tiers] x") == "model-tiers"
    assert mi._aspect_from_title("chore(deps): bump cryptography") is None


# ── candidate collection ───────────────────────────────────────────────────────


def test_chip_candidates_exclude_terminal_include_backlog(tmp_path):
    cands = mi.iter_chip_candidates(_ledgers(tmp_path))
    ids = {c.get("id") for c in cands if c["kind"] == "chip"}
    assert {"D1", "O1", "S1", "B1", "X1", "Y1"} == ids  # DONE1 / MERGED1 excluded
    backlog = [c for c in cands if c["kind"] == "backlog"]
    assert len(backlog) == 2


def test_pr_fleet_filter_and_scope():
    prs = [
        {"number": 3007, "title": "[META:platform] W10-D capstone",
         "headRefName": "pr-w10d-capstone", "isDraft": False,
         "files": [{"path": "packages/server/deploy.py"}]},
        {"number": 3010, "title": "[META:alpha] queue projection tweak",
         "headRefName": "claude/foo", "isDraft": False,
         "files": [{"path": "tools/meta-queue"}]},
        {"number": 2911, "title": "chore(deps): bump cryptography",
         "headRefName": "dependabot/uv/cryptography", "isDraft": False,
         "files": [{"path": "uv.lock"}]},
    ]
    cands = mi.pr_candidates(prs)
    # dependabot PR (non-claude branch, no [META:] title) is NOT a fleet PR.
    assert [c["pr"] for c in cands] == [3007, 3010]
    assert cands[0]["aspect"] == "platform"
    assert cands[0]["scope"] == ["packages/server/deploy.py"]
    assert cands[1]["aspect"] == "alpha"


def test_pr_deduped_against_chip(tmp_path):
    """A PR whose number a chip already carries is dropped (the chip row is richer)."""
    ledgers = _ledgers(tmp_path)
    prs = [{"number": 3010, "title": "[META:alpha] queue projection tweak",
            "headRefName": "claude/foo", "files": [{"path": "tools/meta-queue"}]},
           {"number": 3007, "title": "[META:platform] capstone",
            "headRefName": "claude/cap", "files": [{"path": "packages/server/deploy.py"}]}]
    cands = mi.assemble_candidates(ledgers, prs, None)
    pr_nums = [c["pr"] for c in cands if c["kind"] == "pr"]
    assert pr_nums == [3007]  # 3010 is O1's PR → deduped to the chip


def test_session_candidates():
    cands = mi.session_candidates([
        {"title": "META alpha", "id": "s1"},
        "[META:beta] design pass",
        {"title": "random session"},
        "",  # skipped
    ])
    assert [c["aspect"] for c in cands] == ["alpha", "beta", None]
    assert cands[0]["task_id"] == "s1"


# ── matching / ranking / surface rule ───────────────────────────────────────────


def test_specific_query_ranks_by_strength(tmp_path):
    """aspect+keywords+scope: scope overlaps rank above a keyword-only backlog hit;
    aspect-only chips (S1/B1) are suppressed because the query is specific."""
    ledgers = _ledgers(tmp_path)
    query = mi.make_query(aspect="alpha", keywords="collision",
                          scope="tools/meta-inflight,tools/meta-*")
    overlaps = mi.find_overlaps(mi.assemble_candidates(ledgers, [], None), query)
    ids = _ids(overlaps)
    # D1 (scope+aspect+kw) > O1 (scope+aspect) > backlog (keyword only).
    assert ids[0] == "D1"
    assert ids[1] == "O1"
    assert overlaps[0]["tier"] == "scope"
    assert overlaps[0]["score"] > overlaps[1]["score"]
    backlog = [o for o in overlaps if o["kind"] == "backlog"]
    assert len(backlog) == 1 and backlog[0]["tier"] == "keyword"
    # aspect-only chips never surface on a specific query.
    assert "S1" not in ids and "B1" not in ids
    # terminal chips never surface.
    assert "DONE1" not in ids and "MERGED1" not in ids


def test_cross_aspect_file_collision_surfaces(tmp_path):
    """A scope-glob query surfaces the SAME file in a DIFFERENT aspect — the collision
    that mid-flight invisibility used to hide until the PR."""
    ledgers = _ledgers(tmp_path)
    query = mi.make_query(aspect="alpha", scope="packages/admin/web/usage.js")
    overlaps = mi.find_overlaps(mi.assemble_candidates(ledgers, [], None), query)
    ids = _ids(overlaps)
    assert "S1" in ids  # same aspect, same file
    assert "X1" in ids  # DIFFERENT aspect (beta), same file → cross-aspect collision
    assert "Y1" not in ids  # beta, unrelated file
    # same-aspect file match (score 120) outranks the cross-aspect one (score 100).
    assert ids.index("S1") < ids.index("X1")
    for o in overlaps:
        assert o["tier"] == "scope"


def test_aspect_only_query_lists_inflight_for_that_aspect(tmp_path):
    """With no keywords/scope, an aspect-only query answers 'what's running in beta?'"""
    ledgers = _ledgers(tmp_path)
    query = mi.make_query(aspect="beta")
    overlaps = mi.find_overlaps(mi.assemble_candidates(ledgers, [], None), query)
    ids = set(_ids(overlaps))
    assert ids == {"X1", "Y1"}  # both beta chips; no alpha chips
    assert all(o["tier"] == "aspect" for o in overlaps)


def test_no_overlap_is_clean(tmp_path):
    d = _write_fixture(tmp_path)
    query = mi.make_query(aspect="zeta", keywords="nonexistent", scope="nowhere/**")
    result = mi.run(d, query, "x/y", None, scan_prs=False, pr_limit=10)
    assert result["count"] == 0
    text = mi.render_text(result)
    assert "✅ No in-flight overlap found." in text


# ── dispatch-claim registry (the fourth signal; spec §17 / Initiative 12, B4) ─────
# A claim is a just-dispatched chip with NO PR yet and NOTHING in a ledger yet — the
# invisible mid-flight window the claim registry exists to close. The hook writes it as
# {aspect, title, time, ttl_seconds, ...}; meta-inflight matches on aspect + title kw.

# now-epoch anchor for deterministic TTL tests (never call time.time() in a test).
_NOW = 1_800_000_000


def _write_claims(tmp_path, claims):
    """Write a list of claim dicts to a claims/ dir; return the dir path."""
    cdir = tmp_path / "claims"
    cdir.mkdir()
    for i, c in enumerate(claims):
        (cdir / ("claim-%02d.json" % i)).write_text(json.dumps(c))
    return cdir


def test_active_claim_surfaces_as_overlap(tmp_path):
    """An ACTIVE claim whose title keyword-overlaps the query surfaces — this is the
    no-PR-yet, not-in-a-ledger-yet work the other signals can't see."""
    cdir = _write_claims(tmp_path, [
        {"aspect": "alpha", "title": "dispatch-claim collision layer build",
         "time": _NOW - 60, "ttl_seconds": 14400},
    ])
    claims = mi.load_claims(cdir, now_epoch=_NOW)
    assert len(claims) == 1
    cands = mi.claim_candidates(claims)
    query = mi.make_query(aspect="alpha", keywords="collision")
    overlaps = mi.find_overlaps(cands, query)
    assert len(overlaps) == 1
    o = overlaps[0]
    assert o["kind"] == "claim"
    assert o["bucket"] == "claimed"
    assert "collision" in "".join(o["reasons"])


def test_expired_claim_ignored_and_pruned(tmp_path):
    """A claim older than its ttl is neither loaded NOR left on disk (self-expiry)."""
    cdir = _write_claims(tmp_path, [
        {"aspect": "alpha", "title": "fresh claim", "time": _NOW - 10, "ttl_seconds": 3600},
        {"aspect": "alpha", "title": "stale claim", "time": _NOW - 99999, "ttl_seconds": 3600},
        {"aspect": "alpha", "title": "no ttl", "time": _NOW - 10},          # malformed → expired
    ])
    claims = mi.load_claims(cdir, now_epoch=_NOW, prune=True)
    titles = {c["title"] for c in claims}
    assert titles == {"fresh claim"}  # stale + malformed dropped
    # pruned from disk too — only the fresh one remains.
    remaining = sorted(p.name for p in cdir.glob("*.json"))
    assert len(remaining) == 1


def test_load_claims_no_prune_keeps_files(tmp_path):
    """prune=False ignores expired claims for matching but leaves the files on disk."""
    cdir = _write_claims(tmp_path, [
        {"aspect": "alpha", "title": "stale", "time": _NOW - 99999, "ttl_seconds": 3600},
    ])
    claims = mi.load_claims(cdir, now_epoch=_NOW, prune=False)
    assert claims == []
    assert len(list(cdir.glob("*.json"))) == 1  # not deleted


def test_load_claims_missing_dir_is_empty(tmp_path):
    """A missing claims dir degrades to [] — advisory check never blocks on FS state."""
    assert mi.load_claims(tmp_path / "nope", now_epoch=_NOW) == []


def test_run_counts_active_claims(tmp_path):
    d = _write_fixture(tmp_path)
    cdir = _write_claims(tmp_path, [
        {"aspect": "alpha", "title": "active dispatch collision claim",
         "time": _NOW - 5, "ttl_seconds": 3600},
    ])
    query = mi.make_query(aspect="alpha", keywords="collision")
    result = mi.run(d, query, "x/y", None, scan_prs=False, pr_limit=10,
                    claims_dir=cdir, now_epoch=_NOW)
    assert result["scanned"]["active_claims"] == 1
    ids = _ids(result["overlaps"])
    assert any(o["kind"] == "claim" for o in result["overlaps"])
    # the claim coexists with the ledger chip D1 (both keyword-match "collision").
    assert "D1" in ids


def test_run_without_claims_dir_reports_not_scanned(tmp_path):
    d = _write_fixture(tmp_path)
    query = mi.make_query(aspect="alpha", keywords="collision")
    result = mi.run(d, query, "x/y", None, scan_prs=False, pr_limit=10)
    assert result["scanned"]["active_claims"] is None
    assert "claims: not scanned" in mi.render_text(result)


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_json_offline(tmp_path, capsys):
    d = _write_fixture(tmp_path)
    rc = mi.main(["--aspect", "alpha", "--keywords", "collision",
                  "--scope", "tools/meta-inflight,tools/meta-*",
                  "--dir", str(d), "--no-prs", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    ids = [o.get("id") or o.get("title") for o in payload["overlaps"]]
    assert "D1" in ids and "O1" in ids
    assert payload["scanned"]["sessions"] is None  # not supplied
    assert "--no-prs" in payload["scanned"]["pr_note"]


def test_cli_requires_a_query():
    assert mi.main([]) == 2


def test_cli_bad_dir_is_usage_error(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert mi.main(["--aspect", "alpha", "--dir", str(missing)]) == 2


# ── registry state: absent / unreadable vs genuinely empty ───────────────────
#
# The claim signal is the only one that sees work with no PR and no ledger entry yet.
# It shipped 2026-07-01 and the hook that feeds it was never installed on the operator
# box, so `load_claims` globbed a directory that had never existed, Python returned []
# rather than raising, and every run for eight weeks reported "0 active claim(s)" — a
# confident all-clear from a signal that was structurally inert. Two sessions duplicated
# the same fleet-blocking fix underneath that all-clear on 2026-08-27.
#
# `load_claims` still degrades to [] (an advisory check must never block a dispatch on
# FS state — pinned by test_load_claims_missing_dir_is_empty above). These tests pin the
# REPORT's obligation instead: never state a count you did not take.


def test_probe_reports_absent_registry(tmp_path):
    assert mi.probe_claims_registry(tmp_path / "nope") == mi.CLAIMS_ABSENT


def test_probe_reports_ok_for_an_empty_but_present_registry(tmp_path):
    cdir = tmp_path / "claims"
    cdir.mkdir()
    assert mi.probe_claims_registry(cdir) == mi.CLAIMS_OK


def test_probe_reports_unreadable_registry(tmp_path):
    """A dir that exists but cannot be listed is NOT an all-clear. Guards the
    read-denied/write-allowed asymmetry: unreadable must never read as 'nothing here'."""
    cdir = tmp_path / "claims"
    cdir.mkdir()
    cdir.chmod(0o000)
    try:
        state = mi.probe_claims_registry(cdir)
    finally:
        cdir.chmod(0o755)  # always restore, or tmp_path cleanup fails
    if state == mi.CLAIMS_OK:            # running as root ignores the mode bits
        import pytest
        pytest.skip("cannot make a dir unreadable as this user")
    assert state == mi.CLAIMS_UNREADABLE


def test_run_absent_registry_reports_no_count_not_zero(tmp_path):
    """THE regression guard for the eight-week silent death: an absent registry must
    never surface as a counted zero."""
    d = _write_fixture(tmp_path)
    query = mi.make_query(aspect="alpha", keywords="collision")
    result = mi.run(d, query, "x/y", None, scan_prs=False, pr_limit=10,
                    claims_dir=tmp_path / "nope", now_epoch=_NOW)
    assert result["scanned"]["claims_registry"] == mi.CLAIMS_ABSENT
    assert result["scanned"]["active_claims"] is None
    text = mi.render_text(result)
    assert "0 active claim(s)" not in text
    assert "REGISTRY ABSENT" in text
    assert "meta-system-setup" in text          # the warning names the install


def test_run_empty_but_present_registry_is_an_honest_zero(tmp_path):
    """The other side of the same coin — a real empty registry SHOULD say zero, with
    no warning. Absent and empty are different facts and must render differently."""
    d = _write_fixture(tmp_path)
    cdir = tmp_path / "claims"
    cdir.mkdir()
    query = mi.make_query(aspect="alpha", keywords="collision")
    result = mi.run(d, query, "x/y", None, scan_prs=False, pr_limit=10,
                    claims_dir=cdir, now_epoch=_NOW)
    assert result["scanned"]["claims_registry"] == mi.CLAIMS_OK
    assert result["scanned"]["active_claims"] == 0
    text = mi.render_text(result)
    assert "0 active claim(s)" in text
    assert "REGISTRY ABSENT" not in text
    assert "⚠ dispatch-claim registry" not in text


def test_run_not_scanned_is_distinct_from_absent(tmp_path):
    """--no-claims is a deliberate opt-out, not a broken registry: no warning."""
    d = _write_fixture(tmp_path)
    query = mi.make_query(aspect="alpha", keywords="collision")
    result = mi.run(d, query, "x/y", None, scan_prs=False, pr_limit=10)
    assert result["scanned"]["claims_registry"] == mi.CLAIMS_NOT_SCANNED
    text = mi.render_text(result)
    assert "claims: not scanned" in text
    assert "REGISTRY ABSENT" not in text


def test_cli_json_carries_the_registry_state(tmp_path, capsys):
    """Machine consumers get the state too — a JSON reader must be able to tell an
    inert signal from a clean one without parsing prose."""
    d = _write_fixture(tmp_path)
    rc = mi.main(["--aspect", "alpha", "--keywords", "collision", "--dir", str(d),
                  "--no-prs", "--claims-dir", str(tmp_path / "nope"), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scanned"]["claims_registry"] == mi.CLAIMS_ABSENT
    assert payload["scanned"]["active_claims"] is None


def test_absent_warning_names_the_dir_actually_probed(tmp_path):
    """Under --claims-dir the warning must name the overridden path, not the default —
    naming the default sends the reader to fix a directory that was never consulted."""
    d = _write_fixture(tmp_path)
    probed = tmp_path / "custom-claims"
    query = mi.make_query(aspect="alpha", keywords="collision")
    result = mi.run(d, query, "x/y", None, scan_prs=False, pr_limit=10,
                    claims_dir=probed, now_epoch=_NOW)
    text = mi.render_text(result)
    assert str(probed) in text
    assert str(mi.DEFAULT_CLAIMS_DIR) not in text


# ── keyword splitting: the phrase form the dispatch procedure actually passes ──
# The duplicate check was INERT: `_as_list` split on commas only, so the quoted phrase
# `internal/meta-dispatch-procedure.md` step 3 tells every run to pass survived as ONE
# keyword and was substring-tested against candidate text, where it never appeared.
# Failure direction is the point — an unusable query and a clean lane both returned
# `count: 0`, so the check failed toward DISPATCHING a duplicate.


def test_phrase_and_comma_keyword_forms_are_identical():
    """The two documented conventions must normalize to the same keyword list."""
    phrase = mi.make_query(aspect="alpha", keywords="superseded pointer lifetime claim")
    comma = mi.make_query(aspect="alpha", keywords="superseded,pointer,lifetime,claim")
    assert phrase["keywords"] == comma["keywords"]
    assert phrase["keywords"] == ["superseded", "pointer", "lifetime", "claim"]
    # Mixed separators (a comma list whose segments carry spaces) flatten too.
    mixed = mi.make_query(aspect="alpha", keywords="superseded pointer, lifetime  claim")
    assert mixed["keywords"] == phrase["keywords"]


def test_phrase_query_finds_the_2026_09_02_regression_case(tmp_path):
    """Pinned to the live case that exposed the defect: the phrase query returned 0 while
    a single-keyword re-run returned the dispatched chip it should have blocked on."""
    d = tmp_path / "meta-state"
    d.mkdir()
    alpha = json.loads(json.dumps(LEDGER_ALPHA))  # deep copy: don't mutate the module fixture
    alpha["chips"].append({
        "id": "superseded-pointer-restates-falsified-claim",
        "title": "[META:substrate] a superseded pointer restates a falsified claim",
        "bucket": "dispatched", "task_id": "task_8260f561", "pr": None,
    })
    (d / "alpha.json").write_text(json.dumps(alpha))
    ledgers = mi.load_ledgers(d)
    query = mi.make_query(aspect="alpha",
                          keywords="superseded pointer lifetime claim falsified")
    overlaps = mi.find_overlaps(mi.assemble_candidates(ledgers, [], None), query)
    assert "superseded-pointer-restates-falsified-claim" in _ids(overlaps)


def test_scope_globs_are_not_whitespace_split():
    """`--scope` stays comma-only: a path glob may legitimately contain a space, and
    whitespace-splitting it would corrupt the query in the same silent way."""
    query = mi.make_query(aspect="alpha", scope="docs/my notes/**,tools/meta-*")
    assert query["scope"] == ["docs/my notes/**", "tools/meta-*"]


def test_phrase_keywords_carry_a_warning_and_clean_queries_do_not():
    """A phrase now WORKS, and says so — an older tool would have reported a silent 0."""
    warn = mi.keyword_phrase_warnings("superseded pointer lifetime claim falsified")
    assert len(warn) == 1
    assert "split on whitespace" in warn[0]
    assert "count: 0" in warn[0]  # names the failure a caller would otherwise not see
    # The documented comma form is not a drift signal, and neither is a single keyword.
    assert mi.keyword_phrase_warnings("superseded,pointer,falsified") == []
    assert mi.keyword_phrase_warnings("superseded") == []
    assert mi.keyword_phrase_warnings(None) == []


def test_cli_json_carries_warnings_without_changing_the_exit_code(tmp_path, capsys):
    """`warnings[]` in JSON + stderr for humans; exit stays 0 because callers treat
    non-zero as a hard stop and this is advisory."""
    d = _write_fixture(tmp_path)
    rc = mi.main(["--aspect", "alpha", "--keywords", "dispatch time collision",
                  "--dir", str(d), "--no-prs", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert len(payload["warnings"]) == 1
    assert "meta-inflight:" in captured.err
    # And the split made the query actually match.
    assert "D1" in [o.get("id") or o.get("title") for o in payload["overlaps"]]


def test_cli_json_has_no_warnings_on_the_comma_form(tmp_path, capsys):
    d = _write_fixture(tmp_path)
    rc = mi.main(["--aspect", "alpha", "--keywords", "collision",
                  "--dir", str(d), "--no-prs", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["warnings"] == []
    assert captured.err == ""


# ── keyword matching on TOKEN BOUNDARIES (not bare substrings) ─────────────────
# `kw in text` made `board` match "onboard" and `bind` match "bindings" over a corpus of
# long topical prose, so short common keywords matched almost everything. Measured
# 2026-09-02 on three real dispatch queries: 18 / 10 / 8 overlaps, ZERO of them on the
# actual subject. `internal/meta-dispatch-procedure.md` step 3 says "if count > 0, do NOT
# launch" — so under the repaired #3968 keyword split, that rule halted the lane on
# essentially every candidate.


def test_keyword_does_not_match_inside_a_longer_word():
    """The defect itself: `board` in onboard/dashboard/keyboard, `bind` in bindings."""
    for text in ("onboard github/brave cluster", "the dashboard strip", "keyboard"):
        assert mi.keyword_hits(text, ["board"]) == []
    assert mi.keyword_hits("TWO LEGACY-ID BINDINGS", ["bind"]) == []
    assert mi.keyword_hits("no binding to recover", ["bind"]) == []


def test_keyword_matches_itself_and_across_identifier_separators():
    """`-` / `.` / `/` are SEPARATORS, so a keyword still matches inside a hyphenated
    identifier — the mis-fire direction a blanket \\b fix would introduce."""
    assert mi.keyword_hits("restore board-f8 done entry", ["board"]) == ["board"]
    assert mi.keyword_hits("the board is scanned", ["board"]) == ["board"]
    assert mi.keyword_hits("tools/meta-inflight", ["meta-inflight"]) == ["meta-inflight"]
    assert mi.keyword_hits("tools/meta-inflight", ["inflight"]) == ["inflight"]
    assert mi.keyword_hits("a bind-time hook", ["bind"]) == ["bind"]


def test_underscored_identifier_matches_itself_and_is_not_split():
    """`_` IS a word char: `authorized_keys` matches itself, and the bare word `keys`
    does not match inside it (an identifier is one token, not two)."""
    text = "incursion detectors read ~/.ssh/authorized_keys at baseline"
    assert mi.keyword_hits(text, ["authorized_keys"]) == ["authorized_keys"]
    assert mi.keyword_hits(text, ["keys"]) == []


def test_keyword_with_non_word_edges_still_matches():
    """A blanket `\\b<kw>\\b` cannot anchor a keyword whose own edge is not a word char —
    `\\b#3967` never matches "PR #3967" — so the guards are applied per-edge."""
    assert mi.keyword_hits("landed in PR #3967 today", ["#3967"]) == ["#3967"]
    assert mi.keyword_hits("run it with --json please", ["--json"]) == ["--json"]


def test_keyword_hits_are_distinct_and_in_query_order():
    hits = mi.keyword_hits("board and tailnet and board again", ["tailnet", "board", "x"])
    assert hits == ["tailnet", "board"]


# ── the ACTIONABLE subset — the narrow number step 3 consumes ──────────────────
# `count` mixes a backlog row sharing a common word with a chip genuinely mid-flight on
# the work. These pin the split: `count` keeps its meaning (advisory, wide) and
# `actionable_count` is the only number fit for a "do not launch" rule.

# The 2026-09-02 corpus, verbatim in shape: the backlog prose that produced 34 of the 36
# measured hits, plus the chip whose NOTE (not title) enumerates other briefs' ids.
LEDGER_NOISE = {
    "aspect": "noise",
    "updated": "2026-09-02",
    "chips": [
        # Chip-tier noise, and the reason tier-gating alone would NOT have fixed this:
        # every keyword lands in the provenance `note`, which recites the lane state at
        # dispatch, while the title is about something else entirely.
        {"id": "lane-self-heal", "bucket": "dispatched", "pr": None,
         "title": "[META:substrate] a queued/ copy of an id the dispatcher already moved",
         "note": "PM lane. LANE STATE AT DISPATCH: incursion-detectors-baseline held; "
                 "launchd sweep queued; there is no binding to recover and no bind was "
                 "attempted; next-by-order was board-tool-chat-parity."},
    ],
    "backlog": [
        "Inc 3: onboard github/brave cluster; Inc 4: google-oauth",
        "D-T9 follow-up A — the Dashboard strip, a compact row on Overview",
        "TWO LEGACY-ID BINDINGS THAT ARE MIGRATIONS, NOT SWEEPS",
        "the modal still lacks Amend/Re-bind + Re-point verbs",
        "except-pass baseline drifted UP since 06-17",
        "weekly_review shares launchd-make-up exposure",
    ],
}


def _noise_ledgers(tmp_path):
    d = tmp_path / "meta-state"
    d.mkdir()
    (d / "noise.json").write_text(json.dumps(LEDGER_NOISE))
    return d


def _measured(tmp_path, aspect, keywords):
    d = _noise_ledgers(tmp_path)
    query = mi.make_query(aspect=aspect, keywords=keywords)
    return mi.run(d, query, "x/y", None, scan_prs=False, pr_limit=10)


def test_measured_query_1_reports_zero_actionable(tmp_path):
    """`--aspect reports --keywords "incursion,authorized_keys,baseline,detectors,launchd"`
    measured 18 overlaps, every one backlog-tier on `baseline` or `launchd`."""
    r = _measured(tmp_path, "reports",
                  "incursion,authorized_keys,baseline,detectors,launchd")
    assert r["actionable_count"] == 0
    assert r["count"] > 0                        # still advisory-visible, not hidden


def test_measured_query_2_reports_zero_actionable(tmp_path):
    """`--aspect apps --keywords "board,listener,tailnet,address"` measured 10 overlaps,
    `board` matching inside "onboard" and "dashboard"."""
    r = _measured(tmp_path, "apps", "board,listener,tailnet,address")
    assert r["actionable_count"] == 0
    # the substring hits are gone entirely now, not merely demoted
    titles = " ".join(o["title"] for o in r["overlaps"])
    assert "onboard github/brave" not in titles
    assert "Dashboard strip" not in titles


def test_measured_query_3_reports_zero_actionable(tmp_path):
    """`--aspect apps --keywords "tailnet,listener,bind"` measured 8 overlaps, every one
    on `bind` inside "bindings"/"binding". The rows that survive the boundary fix carry a
    genuine standalone "bind" (Re-bind, "no bind was attempted") on an unrelated subject —
    which is exactly why the boundary fix alone is not enough and the split is needed."""
    r = _measured(tmp_path, "apps", "tailnet,listener,bind")
    assert r["actionable_count"] == 0
    assert "TWO LEGACY-ID BINDINGS" not in " ".join(o["title"] for o in r["overlaps"])


def test_chip_keyword_hits_confined_to_its_note_are_not_actionable(tmp_path):
    """The chip-tier observation from the same runs: a chip whose keywords all land in
    its provenance `note` is someone WRITING ABOUT this work, not someone ON it."""
    r = _measured(tmp_path, "reports", "incursion,detectors,baseline")
    chips = [o for o in r["overlaps"] if o["id"] == "lane-self-heal"]
    assert len(chips) == 1 and chips[0]["actionable"] is False


def test_a_real_overlap_is_actionable_and_outranks_a_backlog_row(tmp_path):
    """The other direction: a chip whose TITLE carries the subject is actionable, is
    ranked first, and a backlog row matching the same words is not."""
    d = tmp_path / "meta-state"
    d.mkdir()
    led = json.loads(json.dumps(LEDGER_NOISE))
    led["chips"].append({
        "id": "real", "bucket": "dispatched", "pr": None,
        "title": "[META:apps] board listener resolves the tailnet address",
    })
    led["backlog"].append("a tailnet listener note in the backlog, board too")
    (d / "noise.json").write_text(json.dumps(led))
    query = mi.make_query(aspect="apps", keywords="board,listener,tailnet,address")
    r = mi.run(d, query, "x/y", None, scan_prs=False, pr_limit=10)
    assert r["actionable_count"] == 1
    assert r["overlaps"][0]["id"] == "real"      # actionable sorts to the top
    assert r["overlaps"][0]["actionable"] is True
    backlog = [o for o in r["overlaps"] if o["kind"] == "backlog"]
    assert backlog and all(o["actionable"] is False for o in backlog)


def test_scope_overlap_on_inflight_work_is_actionable_and_backlog_never_is(tmp_path):
    """A file-level collision is actionable on its own; a backlog row never is, whatever
    it matches — it is work written down, not work under way."""
    ledgers = _ledgers(tmp_path)
    query = mi.make_query(aspect="alpha", keywords="collision",
                          scope="tools/meta-inflight")
    overlaps = mi.find_overlaps(mi.assemble_candidates(ledgers, [], None), query)
    by_id = {(o.get("id") or o["title"]): o for o in overlaps}
    assert by_id["D1"]["actionable"] is True
    assert by_id["D1"]["tier"] == "scope"
    assert all(o["actionable"] is False for o in overlaps if o["kind"] == "backlog")


def test_single_keyword_query_needs_only_one_identity_hit(tmp_path):
    """The 2-keyword floor is clamped by the query's own length — a caller who narrows to
    one word is asserting it is distinctive, and must not get a structural 0."""
    ledgers = _ledgers(tmp_path)
    query = mi.make_query(aspect="alpha", keywords="collision")
    overlaps = mi.find_overlaps(mi.assemble_candidates(ledgers, [], None), query)
    assert any(o["actionable"] for o in overlaps if o.get("id") == "D1")


def test_aspect_only_query_is_never_actionable_and_says_so(tmp_path):
    """An aspect-only query cannot answer "is anyone on THIS?" — the 0 is structural, so
    it is reported with a warning rather than left to read as an all-clear."""
    d = _write_fixture(tmp_path)
    query = mi.make_query(aspect="beta")
    r = mi.run(d, query, "x/y", None, scan_prs=False, pr_limit=10)
    assert r["count"] > 0 and r["actionable_count"] == 0
    assert any("by construction" in w for w in r["warnings"])
    assert "by construction" in mi.render_text(r)


def test_render_separates_actionable_from_advisory(tmp_path):
    r = _measured(tmp_path, "apps", "tailnet,listener,bind")
    text = mi.render_text(r)
    assert "✅ No ACTIONABLE overlap" in text
    assert "advisory match(es)" in text
    assert "a shared word, not shared work" in text


def test_cli_json_carries_actionable_count(tmp_path, capsys):
    d = _noise_ledgers(tmp_path)
    rc = mi.main(["--aspect", "apps", "--keywords", "board,listener,tailnet,address",
                  "--dir", str(d), "--no-prs", "--json"])
    assert rc == 0                                # advisory: never a non-zero exit
    payload = json.loads(capsys.readouterr().out)
    assert payload["actionable_count"] == 0
    assert payload["count"] > 0
    assert all(o["actionable"] is False for o in payload["overlaps"])


# ── --self: the caller's own claim is not somebody else's work ─────────────────
# A real 2026-09-02 dispatch query's TOP hit was the claim written by the very session
# running the check. Nothing in a script's environment names the calling session, so the
# caller passes its own handle(s).


def _self_claims(tmp_path):
    return _write_claims(tmp_path, [
        {"aspect": "alpha", "title": "[META:alpha] dispatch collision layer build",
         "time": _NOW - 60, "ttl_seconds": 14400, "session": "sess-mine"},
        {"aspect": "alpha", "title": "[META:alpha] dispatch collision sibling build",
         "time": _NOW - 60, "ttl_seconds": 14400, "session": "sess-other"},
    ])


def test_self_claim_excluded_while_another_sessions_claim_is_not(tmp_path):
    cands = mi.claim_candidates(mi.load_claims(_self_claims(tmp_path), now_epoch=_NOW))
    query = mi.make_query(aspect="alpha", keywords="dispatch,collision")
    dropped = []
    overlaps = mi.find_overlaps(cands, query,
                                self_handles=mi.self_tokens("sess-mine"),
                                dropped_self=dropped)
    assert [o["task_id"] for o in overlaps] == ["sess-other"]
    assert len(dropped) == 1 and dropped[0]["task_id"] == "sess-mine"
    # and without --self both are reported, unchanged from before.
    assert len(mi.find_overlaps(cands, query)) == 2


def test_self_matches_branch_task_id_and_title_handles():
    cand = {"kind": "chip", "aspect": "a", "title": "t", "scope": [], "note": "",
            "pr": 4242, "task_id": "task_x", "branch": "claude/foo", "id": "chip-1"}
    for tok in ("chip-1", "task_x", "claude/foo", "4242", "t", "CLAUDE/FOO"):
        assert mi.is_self(cand, mi.self_tokens(tok)), tok
    assert not mi.is_self(cand, mi.self_tokens("something-else"))
    assert not mi.is_self(cand, mi.self_tokens(None))      # no --self ⇒ never self


def test_self_matches_a_branch_create_claims_branch_name(tmp_path):
    """`record-dispatch-claim.sh` writes a branch-create claim as
    `branch <name> (<name spaced>)`, so a session that just cut a branch can name it."""
    cdir = _write_claims(tmp_path, [
        {"aspect": "alpha", "title": "branch lane/restore-board-f8 (lane restore board f8)",
         "time": _NOW - 60, "ttl_seconds": 14400, "session": "sess-mine"},
    ])
    cands = mi.claim_candidates(mi.load_claims(cdir, now_epoch=_NOW))
    query = mi.make_query(aspect="alpha", keywords="restore,board")
    assert len(mi.find_overlaps(cands, query)) == 1
    assert mi.find_overlaps(
        cands, query, self_handles=mi.self_tokens("lane/restore-board-f8")) == []


def test_cli_self_flag_is_repeatable_and_reports_the_exclusion(tmp_path, capsys):
    d = _write_fixture(tmp_path)
    cdir = _self_claims(tmp_path)
    rc = mi.main(["--aspect", "alpha", "--keywords", "dispatch,collision",
                  "--dir", str(d), "--no-prs", "--claims-dir", str(cdir),
                  "--self", "sess-mine", "--self", "nothing-matches", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scanned"]["self_excluded"] == 1
    assert "sess-mine" not in json.dumps(payload["overlaps"])
