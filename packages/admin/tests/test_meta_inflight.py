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
