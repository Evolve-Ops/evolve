"""ALPHA-2 — the surface may not assert what the pod did not do.

Audit: ``internal/audit-alpha-journey-2026-08.md`` §4.1/§4.2, findings B2a, U2,
U5. Three findings of one shape — *scan provenance never reaches the reader* —
so the fix is one primitive (``applications.scan_provenance``) and every empty
state / summary / row branching on it.

What these pin is the HONESTY contract, because that is the half a later edit
can break with nothing going red:

  * four states, and ``unreadable`` never collapses into ``never_scanned``
    (docs/principle-tri-state-status.md — "we could not tell" is not "we looked
    and there was nothing")
  * the operator-facing strings never contain a field name or a reason key
    (docs/principle-plex-test.md), and every degraded state carries a remedy
    (docs/principle-alerts-explain-and-remediate.md)
  * a draft that scores 100 and cannot be offered says WHY, and the reason is
    computed beside the constants that produce it — never re-derived downstream
  * ``MIN_DIMENSIONS_FOR_OFFER`` / ``OFFER_THRESHOLD`` are untouched: this chip
    explains the gate, it does not move it
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import scan_provenance as sp  # noqa: E402
from evolve_admin.applications import pod_apps  # noqa: E402
from evolve_admin.applications import app_readiness as ar  # noqa: E402


# Every string this module puts on screen is checked against these. A reason
# key or a Python identifier reaching an operator is the Plex-test failure the
# audit's U1 already found elsewhere in this surface.
FORBIDDEN_ON_SCREEN = (
    "llm_degraded", "no_llm_provider_key", "missing_api_key", "scan_error",
    "definition_status", "eligible_to_offer", "MIN_DIMENSIONS", "OFFER_THRESHOLD",
    "never_scanned", "unreadable", "dimensions_measured",
)


def _assert_operator_language(*strings: str | None) -> None:
    for s in strings:
        if not s:
            continue
        for bad in FORBIDDEN_ON_SCREEN:
            assert bad not in s, f"{bad!r} is a field name and reached the operator: {s!r}"


# ── classify: the four states ────────────────────────────────────────────────


def test_missing_status_file_is_never_scanned_not_ok():
    prov = sp.classify("bot-a", None, sp.READ_MISSING)
    assert prov.state == sp.STATE_NEVER_SCANNED
    assert prov.scanned is False
    assert prov.last_scan_at is None


def test_unreadable_status_is_its_own_state_not_never_scanned():
    """The distinction U5's empty state did not have.

    A pod whose ACLs are wrong must not render as a pod nobody has scanned —
    the operator would be told to run a scan that is already running fine.
    """
    prov = sp.classify("bot-a", None, sp.READ_DENIED)
    assert prov.state == sp.STATE_UNREADABLE
    assert prov.state != sp.STATE_NEVER_SCANNED
    assert prov.scanned is False
    assert prov.note and prov.remedy
    _assert_operator_language(prov.note, prov.remedy)


def test_clean_done_status_is_ok_and_carries_its_timestamp():
    prov = sp.classify("bot-a", {
        "status": "done", "updated_at": "2026-08-23T09:00:00Z",
        "manifests_total_returned": 3,
    }, sp.READ_OK)
    assert prov.state == sp.STATE_OK
    assert prov.degraded is False
    assert prov.scanned is True
    assert prov.last_scan_at == "2026-08-23T09:00:00Z"


def test_llm_degraded_status_is_degraded_with_a_reason_and_a_remedy():
    """B2a: the field the scanner has always written, now read.

    ``scanner.py`` stamps ``llm_degraded`` / ``llm_degraded_reason`` when no
    provider key resolves. A repo-wide grep found no reader, so a pod on the
    Anthropic MAX-subscription setup the install docs advertise was told a full
    scan ran and found nothing.
    """
    prov = sp.classify("bot-a", {
        "status": "done", "updated_at": "2026-08-23T09:00:00Z",
        "llm_degraded": True, "llm_degraded_reason": "no_llm_provider_key",
    }, sp.READ_OK)
    assert prov.state == sp.STATE_DEGRADED
    assert prov.degraded is True
    assert prov.scanned is True, "a degraded scan still ran — it is not 'never scanned'"
    assert prov.reason == "no_llm_provider_key"
    assert prov.note and prov.remedy
    # The remedy has to name a place the operator can go (P-alerts §2).
    assert "Credentials" in prov.remedy
    _assert_operator_language(prov.note, prov.remedy)


def test_a_scan_killed_partway_is_not_ok():
    """The B2 regression an independent review caught.

    ``scanner._write_status`` stamps the file at EVERY phase and only the final
    write carries ``status: "done"``; there is no early return between. So a
    process killed at Phase 2 leaves a phase-2 record on disk indefinitely — the
    admin server clears a stale one only when a NEW scan starts. Classifying it
    ``ok`` put "everything the scanner found has been vouched for" under a scan
    that never finished, which is the exact class this module exists to close.
    """
    prov = sp.classify("bot-a", {
        "phase": 2, "phase_total": 8, "phase_name": "llm",
        "updated_at": "2026-08-01T00:00:00Z",
    }, sp.READ_OK)
    assert prov.state == sp.STATE_DEGRADED
    assert prov.state != sp.STATE_OK
    assert prov.reason == sp.REASON_UNFINISHED
    assert prov.last_scan_at == "2026-08-01T00:00:00Z"
    _assert_operator_language(prov.note, prov.remedy)


def test_not_finishing_outranks_a_degrade_flag_set_on_the_way_down():
    """A run that stopped may ALSO have flipped the degrade flag on its way
    down; "we do not know it finished" is the more actionable half."""
    prov = sp.classify("bot-a", {
        "phase": 2, "llm_degraded": True,
        "llm_degraded_reason": "no_llm_provider_key",
    }, sp.READ_OK)
    assert prov.reason == sp.REASON_UNFINISHED


def test_an_errored_record_is_degraded_not_ok():
    """The scanner replaced its terminal error status with the degrade path,
    but an old file can still carry one and it is not a completed scan."""
    prov = sp.classify("bot-a", {"status": "error", "error_kind": "boom"},
                       sp.READ_OK)
    assert prov.state == sp.STATE_DEGRADED
    assert prov.reason == sp.REASON_UNFINISHED


def test_a_running_scan_is_not_never_scanned_and_not_ok():
    """A scan in flight and a scan that died look identical on disk.

    The file cannot tell them apart, so the copy covers both rather than
    picking one — but neither is "a scan ran and skipped nothing".
    """
    prov = sp.classify("bot-a", {"status": "running", "phase": 2,
                                 "updated_at": "2026-08-23T09:00:00Z"}, sp.READ_OK)
    assert prov.state == sp.STATE_DEGRADED
    assert prov.state != sp.STATE_NEVER_SCANNED
    assert "still be running" in (prov.note or "")


def test_a_reason_key_we_have_no_copy_for_gets_generic_copy_never_the_key():
    prov = sp.classify("bot-a", {"status": "done", "llm_degraded": True,
                                 "llm_degraded_reason": "some_future_reason"},
                       sp.READ_OK)
    assert prov.state == sp.STATE_DEGRADED
    assert prov.reason == "some_future_reason"
    assert "some_future_reason" not in (prov.note or "")
    assert "some_future_reason" not in (prov.remedy or "")
    assert prov.note and prov.remedy


def test_every_known_reason_carries_operator_language_and_a_remedy():
    for key in sp.DEGRADED_REASONS:
        note, remedy = sp.explain_reason(key)
        assert note and remedy, key
        _assert_operator_language(note, remedy)


# ── read_scan_status: the filesystem tri-state ───────────────────────────────


def test_read_missing_file_is_missing_not_denied(tmp_path):
    status, state = sp.read_scan_status(tmp_path / "nope" / ".scan-status.json")
    assert (status, state) == (None, sp.READ_MISSING)


def test_read_good_file_round_trips(tmp_path):
    p = tmp_path / ".scan-status.json"
    p.write_text(json.dumps({"status": "done", "llm_degraded": True}))
    status, state = sp.read_scan_status(p)
    assert state == sp.READ_OK
    assert status["llm_degraded"] is True


def test_read_malformed_file_is_denied_not_missing(tmp_path):
    """A corrupt record is "we could not tell", never "nobody has scanned"."""
    p = tmp_path / ".scan-status.json"
    p.write_text("{not json")
    status, state = sp.read_scan_status(p)
    assert (status, state) == (None, sp.READ_DENIED)


def test_read_json_that_is_not_an_object_is_denied(tmp_path):
    p = tmp_path / ".scan-status.json"
    p.write_text("[1, 2, 3]")
    assert sp.read_scan_status(p) == (None, sp.READ_DENIED)


# ── summarize: the pod-wide rollup ───────────────────────────────────────────


def _prov(bot: str, state_source):
    return sp.classify(bot, *state_source)


def test_summary_groups_one_reason_shared_by_many_bots_into_one_line():
    degraded = ({"status": "done", "llm_degraded": True,
                 "llm_degraded_reason": "no_llm_provider_key"}, sp.READ_OK)
    rows = [_prov(f"bot-{i}", degraded) for i in "abc"]
    summary = sp.summarize(rows)
    assert summary["counts"][sp.STATE_DEGRADED] == 3
    assert summary["degraded_bots"] == ["bot-a", "bot-b", "bot-c"]
    assert len(summary["degraded_reasons"]) == 1, (
        "five bots sharing one missing key must produce one banner line, not five"
    )
    assert summary["degraded_reasons"][0]["bots"] == ["bot-a", "bot-b", "bot-c"]


def test_summary_any_scanned_is_false_when_only_unreadable_bots_exist():
    """An unreadable bot must not be counted as evidence that a scan ran."""
    summary = sp.summarize([sp.classify("bot-a", None, sp.READ_DENIED)])
    assert summary["any_scanned"] is False
    assert summary["counts"][sp.STATE_UNREADABLE] == 1
    assert summary["counts"][sp.STATE_NEVER_SCANNED] == 0


def test_summary_any_scanned_is_true_when_one_bot_scanned_degraded():
    summary = sp.summarize([
        sp.classify("bot-a", None, sp.READ_MISSING),
        sp.classify("bot-b", {"status": "done", "llm_degraded": True}, sp.READ_OK),
    ])
    assert summary["any_scanned"] is True


def test_summary_last_scan_at_is_none_when_nothing_ever_scanned():
    summary = sp.summarize([sp.classify("bot-a", None, sp.READ_MISSING)])
    assert summary["last_scan_at"] is None
    assert summary["total"] == 1


def test_summary_counts_cover_every_declared_state():
    summary = sp.summarize([])
    assert set(summary["counts"]) == set(sp.SCAN_STATES)


# ── provenance_for: the path the writer actually uses ────────────────────────


def test_provenance_for_reads_through_applications_dir(tmp_path, monkeypatch):
    """The reader resolves through ``manifest.applications_dir``.

    That is what makes writer and reader agree on the smart-sync path, where
    ``scan_workspace_pipeline`` is called with ``output_dir=None`` and places
    the file via the same call. Stated as "the reader calls applications_dir"
    rather than "writer and reader always agree", because the older
    ``POST /api/applications/scan`` route passes an explicit ``output_dir``
    and the two can diverge on a bot with a relocated workspace — an
    independent review caught the overclaim in the first version of this
    docstring.
    """
    manifests = tmp_path / "bot-a" / ".openclaw" / "workspace" / "manifests"
    manifests.mkdir(parents=True)
    monkeypatch.setattr(
        "evolve_admin.applications.manifest.applications_dir",
        lambda shared, bot: tmp_path / bot / ".openclaw" / "workspace" / "manifests",
    )
    assert sp.provenance_for("bot-a", tmp_path).state == sp.STATE_NEVER_SCANNED

    (manifests / sp.SCAN_STATUS_FILENAME).write_text(json.dumps({
        "status": "done", "updated_at": "2026-08-23T10:00:00Z",
        "llm_degraded": True, "llm_degraded_reason": "no_llm_provider_key",
    }))
    prov = sp.provenance_for("bot-a", tmp_path)
    assert prov.state == sp.STATE_DEGRADED
    assert prov.last_scan_at == "2026-08-23T10:00:00Z"


def test_a_missing_file_under_a_missing_workspace_is_could_not_tell(tmp_path, monkeypatch):
    """A resolver that lands on the wrong path must not read as "never scanned".

    ``manifest.applications_dir``'s last-resort fallback keys on the bot_id
    rather than the OS account and hardcodes a macOS home, so a bot whose
    account name differs — or any bot on a Linux pod reaching that fallback —
    would otherwise be reported as never scanned when it has been scanned.
    """
    monkeypatch.setattr(
        "evolve_admin.applications.manifest.applications_dir",
        lambda shared, bot: tmp_path / "no-such-home" / "workspace" / "manifests",
    )
    prov = sp.provenance_for("bot-a", tmp_path)
    assert prov.state == sp.STATE_UNREADABLE
    assert prov.reason == sp.REASON_NO_WORKSPACE
    assert "workspace" in (prov.note or "")
    # And the remedy is the one that fits THIS cause — not "re-deploy to repair
    # file permissions", which would not help.
    assert "permission" not in (prov.remedy or "").lower()


def test_a_real_workspace_with_no_status_file_is_still_never_scanned(tmp_path, monkeypatch):
    """The guard above must not swallow the genuine first-open case."""
    manifests = tmp_path / "bot-a" / "workspace" / "manifests"
    manifests.mkdir(parents=True)
    monkeypatch.setattr(
        "evolve_admin.applications.manifest.applications_dir",
        lambda shared, bot: manifests,
    )
    assert sp.provenance_for("bot-a", tmp_path).state == sp.STATE_NEVER_SCANNED


# ── build_discovered carries it ──────────────────────────────────────────────


def _draft(stem: str) -> tuple[str, dict]:
    return stem, {
        "id": stem, "name": "Draft Thing", "definition_status": "discovered",
        "identity": {"purpose": "Files the notes. Second sentence."},
        "schema_version": 30,
        "files": [{"path": "notes.py", "layer": "code"}],
    }


def test_discovered_payload_carries_a_block_for_every_bot_not_just_drafted_ones(tmp_path):
    """U5: "has anything ever scanned team-bot-c?" is unaskable from a draft list."""
    payload = pod_apps.build_discovered(
        ["bot-a", "bot-b", "bot-c"],
        read_manifests=lambda b: [_draft("notes")] if b == "bot-a" else [],
        shared_dir=tmp_path,
        read_provenance=lambda b: sp.classify(
            b, {"status": "done"} if b == "bot-a" else None,
            sp.READ_OK if b == "bot-a" else sp.READ_MISSING,
        ),  # bot-a: a completed scan; bot-b/bot-c: never visited
    )
    assert [s["bot_id"] for s in payload["scans"]] == ["bot-a", "bot-b", "bot-c"]
    assert payload["scan_summary"]["counts"][sp.STATE_NEVER_SCANNED] == 2
    assert payload["scan_summary"]["any_scanned"] is True


def test_discovered_payload_reports_no_provenance_rather_than_a_healthy_guess(tmp_path):
    """Omitting the seam must not fabricate an ``ok``."""
    payload = pod_apps.build_discovered(
        ["bot-a"], read_manifests=lambda b: [], shared_dir=tmp_path,
    )
    assert payload["scans"] is None
    assert payload["scan_summary"] is None


def test_a_reader_that_raises_becomes_unreadable_not_a_dropped_bot(tmp_path):
    """Shrinking the denominator would make a broken bot look like no bot."""
    def boom(bot_id):
        raise RuntimeError("acl")

    payload = pod_apps.build_discovered(
        ["bot-a", "bot-b"], read_manifests=lambda b: [], shared_dir=tmp_path,
        read_provenance=boom,
    )
    assert len(payload["scans"]) == 2
    assert {s["state"] for s in payload["scans"]} == {sp.STATE_UNREADABLE}
    assert payload["scan_summary"]["any_scanned"] is False


# ── U2: the readiness blocker ────────────────────────────────────────────────


def test_this_chip_moves_no_readiness_constant():
    """ALPHA-2 explains the gate. ALPHA-4 removes it on merit.

    Lowering either number to make a draft eligible is the vacuity trap
    ``app_promotion_sweep``'s docstring names explicitly. Pinned so a later
    "just make the demo work" edit has to argue with a test.
    """
    assert ar.MIN_DIMENSIONS_FOR_OFFER == 2
    assert ar.OFFER_THRESHOLD == 75
    assert ar.EMERGING_THRESHOLD == 35


def test_a_hundred_that_cannot_be_offered_says_why():
    """The exact audit U2 row: 100 / ready / not offered, with no reason."""
    readiness = ar.score_readiness({"files": [{"path": "brief.py", "layer": "code"}]})
    assert readiness.score == 100 and readiness.band == "ready"
    assert readiness.eligible_to_offer is False
    blockers = readiness.offer_blockers
    assert blockers, "a ready-but-unofferable draft with no stated blocker is U2"
    assert blockers[0]["key"] == ar.BLOCKER_NEEDS_MEASURES
    assert blockers[0]["short"] == "needs another measure"
    _assert_operator_language(blockers[0]["short"], blockers[0]["text"])


def test_blocker_text_is_built_from_the_constants_not_hardcoded(monkeypatch):
    """The renderer must never keep its own copy of the threshold.

    Sweeping ``min_dimensions`` has to move the sentence — if it does not, the
    number is written down twice somewhere and one copy will drift.
    """
    readiness = ar.score_readiness(
        {"files": [{"path": "brief.py", "layer": "code"}]}, min_dimensions=3,
    )
    text = readiness.offer_blockers[0]["text"]
    assert "at least 3" in text
    assert readiness.offer_blockers[0]["short"] == "needs 2 more measures"
    assert readiness.to_dict()["min_dimensions"] == 3


def test_a_low_score_reports_the_threshold_blocker_first():
    readiness = ar.score_readiness({"files": [{"path": "notes.txt", "layer": "data"}]})
    assert readiness.score is not None and readiness.score < ar.OFFER_THRESHOLD
    keys = [b["key"] for b in readiness.offer_blockers]
    assert keys[0] == ar.BLOCKER_BELOW_THRESHOLD
    assert ar.BLOCKER_NEEDS_MEASURES in keys, (
        "both blockers are true for this draft and both must be reported"
    )


def test_an_eligible_draft_has_no_blockers():
    readiness = ar.score_readiness(
        {"files": [{"path": "brief.py", "layer": "code"}]}, min_dimensions=1,
    )
    assert readiness.eligible_to_offer is True
    assert readiness.offer_blockers == ()
    assert readiness.to_dict()["offer_blockers"] == []


def test_blockers_survive_the_json_round_trip_the_route_makes():
    payload = ar.readiness_payload({"files": [{"path": "brief.py", "layer": "code"}]})
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped["offer_blockers"][0]["key"] == ar.BLOCKER_NEEDS_MEASURES


@pytest.mark.parametrize("manifest", [{}, {"files": []}])
def test_a_draft_with_no_evidence_still_reports_a_blocker(manifest):
    readiness = ar.score_readiness(manifest)
    assert readiness.eligible_to_offer is False
    assert readiness.offer_blockers, "every unofferable draft must state a reason"


def test_provenance_covers_the_bots_sync_visits_not_the_manifest_holders(tmp_path):
    """Every sentence built from provenance ends in "run Sync".

    ``build_discovered``'s ``bots`` appends the ``evolve`` service account
    because it can hold a manifest; ``POST /api/applications/sync/pod``
    iterates ``network.bots``, which does not contain it. Naming ``evolve`` in
    a "has never been scanned — run Sync" line would point the operator at
    something Sync will never visit.
    """
    payload = pod_apps.build_discovered(
        ["bot-a", "evolve"],
        read_manifests=lambda b: [],
        shared_dir=tmp_path,
        read_provenance=lambda b: sp.classify(b, None, sp.READ_MISSING),
        scan_bots=["bot-a"],
    )
    assert [s["bot_id"] for s in payload["scans"]] == ["bot-a"]
    assert payload["scan_summary"]["total"] == 1
    # The filter dropdown still offers every bot — only the scan story narrows.
    assert payload["bots"] == ["bot-a", "evolve"]


def test_a_planned_bot_is_not_reported_as_unscanned(tmp_path, monkeypatch):
    """A planned bot is a reserved NAME — no account, no home, no workspace.

    It can never have been scanned and never will be until it is provisioned,
    so "has never been scanned — run Sync" would be a permanent instruction
    that permanently changes nothing.
    """
    import json as _json
    from flask import Flask
    from evolve_admin.web import server as _server
    from evolve_admin.web.routes_apps import register_apps_routes

    network = tmp_path / "network.json"
    network.write_text(_json.dumps({
        "sharedDir": str(tmp_path),
        "members": ["real-bot"],
        "bots": {"real-bot": {"user": "real-bot"},
                 "planned-bot": {"purpose": "will file the invoices"}},
    }))
    monkeypatch.setattr(
        _server, "resolve_bot_paths",
        lambda bid, user=None: {"workspace": str(tmp_path / bid / "workspace")},
    )
    monkeypatch.setattr(_server, "_resolve_bot_user", lambda bid, *a, **kw: bid)
    monkeypatch.setattr(
        "evolve_admin.applications.manifest.applications_dir",
        lambda shared, bot: tmp_path / bot / "workspace" / "manifests",
    )
    (tmp_path / "real-bot" / "workspace" / "manifests").mkdir(parents=True)

    app = Flask(__name__)
    register_apps_routes(app, network)
    app.testing = True
    body = app.test_client().get("/api/apps/discovered").get_json()
    assert [s["bot_id"] for s in body["scans"]] == ["real-bot"]
    assert "planned-bot" not in _json.dumps(body["scan_summary"])


# ── The scanner↔reader boundary (pass-2 finding F5) ─────────────────────────
#
# Two mutations of ``scanner.py``'s terminal write survived the whole suite:
# renaming ``status``/``done`` (every pod would read "did not finish" forever)
# and renaming ``llm_degraded`` (B2a regresses silently to the pre-chip
# behaviour). Both are one-word edits in a file this chip does not own, which is
# exactly why the literals need pinning from THIS side. These tests drive the
# real ``scanner._write_status`` and feed its output to the real ``classify``,
# so the two modules agree by execution rather than by two matching guesses.


def _write_via_scanner(tmp_path, phase, extra=None):
    from evolve_admin.applications.scanner import _write_status

    path = tmp_path / sp.SCAN_STATUS_FILENAME
    _write_status(path, phase, extra=extra)
    return sp.read_scan_status(path)


def test_the_scanners_terminal_write_classifies_as_ok(tmp_path):
    """``done_extra``'s real shape, straight out of ``scan_workspace_pipeline``."""
    status, read_state = _write_via_scanner(
        tmp_path, 4, {"status": "done", "manifests_total_returned": 3})
    assert read_state == sp.READ_OK
    assert sp.classify("bot-a", status, read_state).state == sp.STATE_OK


def test_the_scanners_degraded_terminal_write_classifies_as_degraded(tmp_path):
    """The exact keys ``scanner.py`` sets when no provider key resolves."""
    status, read_state = _write_via_scanner(tmp_path, 4, {
        "status": "done", "manifests_total_returned": 0,
        "llm_degraded": True, "llm_degraded_reason": "no_llm_provider_key",
    })
    prov = sp.classify("bot-a", status, read_state)
    assert prov.state == sp.STATE_DEGRADED
    assert prov.reason == sp.REASON_NO_LLM_KEY
    assert prov.note in [n for n, _ in sp.DEGRADED_REASONS.values()]


def test_a_mid_scan_write_by_the_real_scanner_is_not_ok(tmp_path):
    """A phase write carries no ``status`` key at all — that is the invariant
    ``ok``-requires-terminal rests on, pinned against the real writer."""
    status, read_state = _write_via_scanner(tmp_path, 2)
    assert "status" not in status, (
        "scanner._write_status grew a status key on phase writes — the "
        "terminality test in classify() is now measuring something else"
    )
    assert sp.classify("bot-a", status, read_state).state == sp.STATE_DEGRADED


def test_the_quick_scan_reason_the_scanner_records_has_copy_here(tmp_path):
    """``--no-llm`` by request completes cleanly but skips the pass that
    recognises apps; without copy for it, a finished quick scan let Discovered
    claim everything on the pod had been vouched for (pass-2 finding F2)."""
    status, read_state = _write_via_scanner(tmp_path, 4, {
        "status": "done", "llm_degraded": True,
        "llm_degraded_reason": sp.REASON_STRUCTURAL_BY_REQUEST,
    })
    prov = sp.classify("bot-a", status, read_state)
    assert prov.state == sp.STATE_DEGRADED
    assert prov.reason == sp.REASON_STRUCTURAL_BY_REQUEST
    assert prov.note != sp.GENERIC_DEGRADED[0], "no copy written for this reason"
    _assert_operator_language(prov.note, prov.remedy)


def test_the_writers_terminal_literals_are_the_readers_constants():
    """The keys ``scan_workspace_pipeline`` stamps ARE the ones read here.

    The tests above prove ``classify`` handles the shape correctly; they cannot
    prove the scanner still writes that shape, because they hand the shape in
    themselves. Renaming ``status``/``done`` or ``llm_degraded`` inside
    ``done_extra`` therefore passed the whole suite while breaking every pod —
    a permanent amber banner in the first case, a silent B2a regression in the
    second. A pass-2 mutation sweep found both.

    Source inspection rather than execution, stated plainly as the compromise
    it is: ``scan_workspace_pipeline`` needs a real workspace, a network config
    and an LLM seam, which is a pod, and this suite runs without one. What it
    buys is that the literals are compared against THIS module's constants, so
    a rename on either side of the boundary fails rather than drifting.
    """
    import inspect
    from evolve_admin.applications import scanner

    src = inspect.getsource(scanner.scan_workspace_pipeline)
    assert f'"status": "{sp.STATUS_DONE}"' in src, (
        "the scanner's terminal write no longer stamps the key classify() "
        "requires for STATE_OK — every bot would read 'did not finish'"
    )
    assert 'done_extra["llm_degraded"]' in src, (
        "the scanner no longer stamps llm_degraded — audit B2a regresses "
        "silently to the pre-chip behaviour"
    )
    assert 'done_extra["llm_degraded_reason"]' in src
    assert f'"{sp.REASON_NO_LLM_KEY}"' in src, (
        "the scanner's no-key reason and this module's copy table have drifted"
    )
    assert f'None if use_llm else "{sp.REASON_STRUCTURAL_BY_REQUEST}"' in src, (
        "scan_workspace_pipeline no longer records that a --no-llm run was "
        "structural by request; a completed quick scan will read as a full one"
    )


# ── The privileged-read fallback (pass-2 finding F6) ────────────────────────
#
# Two more surviving mutations: skipping the sudo fallback on PermissionError,
# and reporting a non-zero ``sudo cat`` as MISSING. Both collapse "not allowed"
# into "never scanned" — the precise conflation this module exists to prevent —
# and nothing exercised either branch.


def test_a_permission_denied_read_falls_through_to_the_privileged_one(tmp_path, monkeypatch):
    calls = {}

    def fake_read_text(self, *a, **kw):
        raise PermissionError(13, "denied")

    class _R:
        returncode = 0
        stdout = json.dumps({"status": "done", "updated_at": "2026-08-23T00:00:00Z"})

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        return _R()

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(sp.subprocess, "run", fake_run)
    status, read_state = sp.read_scan_status(tmp_path / sp.SCAN_STATUS_FILENAME)
    assert read_state == sp.READ_OK, "the sudo fallback was skipped entirely"
    assert status["status"] == "done"
    # CLAUDE.md: never `sudo -u <bot>` — the evolve user has no such grant.
    assert "-u" not in calls["argv"]
    assert calls["argv"][:2] == ["sudo", "-n"], calls["argv"]


def test_a_denied_privileged_read_is_could_not_tell_not_never_scanned(tmp_path, monkeypatch):
    """Without the grant we cannot tell "no such file" from "not allowed",
    and guessing the friendlier one resurrects the lie."""
    class _R:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(
        Path, "read_text", lambda self, *a, **kw: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(sp.subprocess, "run", lambda argv, **kw: _R())
    status, read_state = sp.read_scan_status(tmp_path / sp.SCAN_STATUS_FILENAME)
    assert (status, read_state) == (None, sp.READ_DENIED)
    assert sp.classify("bot-a", status, read_state).state == sp.STATE_UNREADABLE


def test_a_privileged_read_that_returns_non_json_is_denied(tmp_path, monkeypatch):
    class _R:
        returncode = 0
        stdout = "not json at all"

    monkeypatch.setattr(
        Path, "read_text", lambda self, *a, **kw: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(sp.subprocess, "run", lambda argv, **kw: _R())
    assert sp.read_scan_status(tmp_path / sp.SCAN_STATUS_FILENAME) == (None, sp.READ_DENIED)


def test_a_privileged_read_that_raises_is_denied(tmp_path, monkeypatch):
    monkeypatch.setattr(
        Path, "read_text", lambda self, *a, **kw: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(
        sp.subprocess, "run",
        lambda argv, **kw: (_ for _ in ()).throw(OSError("no sudo")))
    assert sp.read_scan_status(tmp_path / sp.SCAN_STATUS_FILENAME) == (None, sp.READ_DENIED)
