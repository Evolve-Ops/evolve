"""The L2 authorized-change gate, one source at a time.

Spec: internal/spec-drift-alert-taxonomy-2026-06-26.md (L2). The gate answers
one question — "is this change explained by a known authorized event?" — and
every test here is a case of that question with a known right answer.

The through-the-dispatcher differential pair (the same sudoers change paging
or not paging depending only on whether a deploy record accounts for it) lives
in test_drift_authorization_differential.py, because it exercises the real
page path rather than the gate in isolation.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import drift_authorization as da  # noqa: E402


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def shared(tmp_path):
    d = tmp_path / "evolve"
    d.mkdir()
    return d


# ── Source: deploy / version bump ─────────────────────────────────────────────


def _per_bot_stamp(shared: Path, bot_id: str, at: datetime,
                   version: str = "2026.0901.3999") -> None:
    (shared / "install.json").write_text(json.dumps({
        "version": version,
        "bot_versions": {bot_id: {"version": version, "deployed_at": _iso(at)}},
    }))


def test_a_bots_own_deploy_stamp_inside_the_window_explains_its_change(shared):
    _per_bot_stamp(shared, "team-bot-a", NOW - timedelta(minutes=20))
    found = da.explain(
        da.DriftChange(kind=da.KIND_SCRIPT_INVENTORY, bot_id="team-bot-a",
                       target="/home/team-bot-a/.openclaw/workspace"),
        shared, now=NOW, memo=False,
    )
    assert found is not None
    assert found.source == da.SOURCE_DEPLOY
    assert "2026.0901.3999" in found.evidence


def test_a_deploy_stamp_outside_the_window_explains_nothing(shared):
    _per_bot_stamp(shared, "team-bot-a", NOW - da.DEPLOY_WINDOW - timedelta(minutes=1))
    assert da.explain(
        da.DriftChange(kind=da.KIND_SCRIPT_INVENTORY, bot_id="team-bot-a"),
        shared, now=NOW, memo=False,
    ) is None


def test_a_future_stamp_does_not_hold_the_window_open(shared):
    """Clock skew, or a hand-edited record, must not authorize anything.

    Without the future-stamp rejection a single record dated next year would
    explain every drift on the pod forever — the cheapest possible way to
    silence the whole security surface.
    """
    _per_bot_stamp(shared, "team-bot-a", NOW + timedelta(days=365), "2027.0101.1")
    assert da.explain(
        da.DriftChange(kind=da.KIND_SCRIPT_INVENTORY, bot_id="team-bot-a"),
        shared, now=NOW, memo=False,
    ) is None


def test_a_per_bot_deploy_stamp_explains_that_bot_and_not_another(shared):
    _per_bot_stamp(shared, "team-bot-a", NOW - timedelta(minutes=5))
    for kind in (da.KIND_SCRIPT_INVENTORY, da.KIND_IDENTITY_FILE, da.KIND_POD_PERMS):
        mine = da.explain(
            da.DriftChange(kind=kind, bot_id="team-bot-a"),
            shared, now=NOW, memo=False,
        )
        assert mine is not None and "team-bot-a" in mine.evidence, kind

        assert da.explain(
            da.DriftChange(kind=kind, bot_id="team-bot-b"),
            shared, now=NOW, memo=False,
        ) is None, kind


def test_pod_wide_deploy_stamps_never_explain_one_bots_change(shared):
    """``installed_at`` is rewritten on EVERY ``evolve-admin deploy <bot>``,
    and the release pointer moves the whole fleet at once. Crediting either
    would let a deploy of bot X open a six-hour window in which bot Y's
    AGENTS.md / SOUL.md / HEARTBEAT.md may change unremarked — and the memo
    would then keep that verdict by content hash for a year."""
    (shared / "install.json").write_text(json.dumps({
        "version": "2026.0901.3999",
        "installed_at": _iso(NOW),
        "bot_versions": {
            "team-bot-x": {"version": "2026.0901.3999",
                           "deployed_at": _iso(NOW - timedelta(minutes=1))},
        },
    }))
    (shared / "release.json").write_text(json.dumps({
        "stable": {"sha": "a" * 40, "version": "2026.0901.3999",
                   "promoted_at": _iso(NOW - timedelta(minutes=1))},
    }))
    for kind in (da.KIND_IDENTITY_FILE, da.KIND_SCRIPT_INVENTORY, da.KIND_POD_PERMS):
        assert da.explain(
            da.DriftChange(kind=kind, bot_id="team-bot-y",
                           target="/home/team-bot-y/.openclaw/workspace/AGENTS.md",
                           content_hash="b" * 64, keys=("AGENTS.md",)),
            shared, now=NOW, memo=False,
        ) is None, kind
    # A pod-wide target carries no bot, so a deploy explains nothing there.
    assert da.explain(
        da.DriftChange(kind=da.KIND_POD_PERMS, target=str(shared / "proposals")),
        shared, now=NOW, memo=False,
    ) is None


# ── Source: Evolve self-update (the proposal pipeline) ────────────────────────


def _applied_proposal(shared: Path, bot_id: str, at: datetime, *,
                      path: str | None, title: str = "add a tone note",
                      subdir: str = "applied", history: bool = True) -> None:
    """What ``arbiter.apply`` leaves behind for a SoulEdit / AgentsAppend:
    the applier's ``details`` under ``provenance.signals._apply_details`` and
    the history entry that moved the proposal to ``applied``."""
    d = shared / "proposals" / subdir
    d.mkdir(parents=True, exist_ok=True)
    details = {"path": path, "operation": "append_section"} if path else {}
    (d / f"{bot_id}-p1.json").write_text(json.dumps({
        "id": "p1", "bot_id": bot_id, "title": title, "status": subdir,
        "provenance": {"signals": {"_apply_details": details}},
        "history": [
            {"from_status": "approved_user", "to_status": "applied",
             "at": _iso(at), "actor": "arbiter", "reason": "applied"},
        ] if history else [],
    }))


def _apply_result(shared: Path, bot_id: str, record: dict) -> None:
    """A record in the retired apply daemon's ``apply-results/``."""
    results = shared / "proposals" / "apply-results"
    results.mkdir(parents=True, exist_ok=True)
    (results / f"{bot_id}-p1.json").write_text(json.dumps(record))


def _identity(bot_id: str, fname: str, content_hash: str = "") -> da.DriftChange:
    return da.DriftChange(kind=da.KIND_IDENTITY_FILE, bot_id=bot_id,
                          target=f"/x/{fname}", content_hash=content_hash,
                          keys=(fname,))


def test_an_applied_proposal_explains_the_file_its_applier_wrote(shared):
    """The AgentsAppend applier (#3881) delegates to SoulEdit, which records
    the path it wrote — that record is what accounts for AGENTS.md moving."""
    _applied_proposal(shared, "team-bot-a", NOW - timedelta(hours=2),
                      path="/home/team-bot-a/.openclaw/workspace/AGENTS.md")
    found = da.explain(_identity("team-bot-a", "AGENTS.md"), shared, now=NOW, memo=False)
    assert found is not None
    assert found.source == da.SOURCE_SELF_UPDATE
    assert "add a tone note" in found.evidence


def test_a_closed_out_proposal_still_explains_the_file_it_wrote(shared):
    """A claim-less AgentsAppend closes out on apply and lands in archived/."""
    _applied_proposal(shared, "team-bot-a", NOW - timedelta(hours=2),
                      path="/home/team-bot-a/.openclaw/workspace/AGENTS.md",
                      subdir="archived")
    found = da.explain(_identity("team-bot-a", "AGENTS.md"), shared, now=NOW, memo=False)
    assert found is not None and found.source == da.SOURCE_SELF_UPDATE


def test_an_applied_proposal_does_not_explain_a_file_it_never_touched(shared):
    _applied_proposal(shared, "team-bot-a", NOW - timedelta(hours=2),
                      path="/home/team-bot-a/.openclaw/workspace/AGENTS.md")
    assert da.explain(_identity("team-bot-a", "SOUL.md"), shared, now=NOW, memo=False) is None
    # ...nor the same file on another bot.
    assert da.explain(_identity("team-bot-b", "AGENTS.md"), shared, now=NOW, memo=False) is None


def test_a_real_apply_result_declares_config_keys_and_never_an_identity_file(shared):
    """The record shape production actually wrote: ``proposed_change`` keyed
    by dotted openclaw.json paths. It declares ``agents`` — not SOUL.md — so
    a routine routing change on bot X does not account for X's SOUL.md."""
    _apply_result(shared, "team-bot-a", {
        "status": "applied",
        "applied_at": _iso(NOW - timedelta(hours=2)),
        "title": "route mechanical turns to the cheap rung",
        "proposed_change": {"agents.defaultModel": "cheap-rung"},
    })
    assert da.applied_config_keys(json.loads(
        (shared / "proposals" / "apply-results" / "team-bot-a-p1.json").read_text()
    )) == {"agents"}
    assert da.explain(_identity("team-bot-a", "SOUL.md"), shared, now=NOW, memo=False) is None
    # The declaration does answer a question about the key it named.
    assert da._self_update_events(shared, "team-bot-a", ("agents",), NOW)


def test_the_legacy_action_taken_shape_declares_its_one_key(shared):
    _apply_result(shared, "team-bot-a", {
        "success": True, "bot_id": "team-bot-a",
        "applied_at": _iso(NOW - timedelta(hours=2)),
        "action_taken": "set_agents.defaultModel",
    })
    assert da._self_update_events(shared, "team-bot-a", ("agents",), NOW)
    assert not da._self_update_events(shared, "team-bot-a", ("SOUL.md",), NOW)


def test_an_apply_result_that_declares_nothing_explains_nothing(shared):
    """The reviewer's finding on #3953: an undeclared record used to explain
    EVERY key, and the memo then kept that verdict for a year."""
    _apply_result(shared, "team-bot-a", {})
    assert da.explain(_identity("team-bot-a", "SOUL.md", "a" * 64), shared, now=NOW) is None
    assert not (shared / "security" / "drift-explained.json").exists()
    # A dated but undeclared record is no better than an empty one.
    _apply_result(shared, "team-bot-a", {
        "applied_at": _iso(NOW - timedelta(hours=2)), "title": "something",
    })
    assert da._self_update_events(shared, "team-bot-a", ("agents", "SOUL.md"), NOW) == []
    assert da.explain(_identity("team-bot-a", "SOUL.md", "a" * 64), shared, now=NOW) is None
    assert not (shared / "security" / "drift-explained.json").exists()


def test_a_record_without_an_applied_stamp_explains_nothing(shared):
    """No ``applied_at`` means no event — the file's mtime is not a stamp
    anyone signed, and the directory is writable by more than the applier."""
    _apply_result(shared, "team-bot-a", {
        "status": "applied",
        "proposed_change": {"agents.defaultModel": "cheap-rung"},
    })
    assert da._self_update_events(shared, "team-bot-a", ("agents",), NOW) == []
    # Same for an arbiter record whose history never reached ``applied``.
    _applied_proposal(shared, "team-bot-a", NOW - timedelta(hours=2),
                      path="/home/team-bot-a/.openclaw/workspace/SOUL.md",
                      history=False)
    assert da.explain(_identity("team-bot-a", "SOUL.md", "a" * 64), shared, now=NOW) is None
    assert not (shared / "security" / "drift-explained.json").exists()


def test_an_applied_proposal_outside_the_window_explains_nothing(shared):
    _applied_proposal(shared, "team-bot-a", NOW - da.SELF_UPDATE_WINDOW - timedelta(hours=1),
                      path="/home/team-bot-a/.openclaw/workspace/AGENTS.md")
    assert da.explain(_identity("team-bot-a", "AGENTS.md"), shared, now=NOW, memo=False) is None


def test_a_self_update_with_no_keys_to_match_explains_nothing(shared):
    _applied_proposal(shared, "team-bot-a", NOW - timedelta(hours=2),
                      path="/home/team-bot-a/.openclaw/workspace/AGENTS.md")
    assert da._self_update_events(shared, "team-bot-a", (), NOW) == []


# ── The operator approval source, and why there isn't one ────────────────────


def test_no_operator_surface_can_name_a_file(shared):
    """Why ``operator_intent`` is not in the allow-set, proved rather than
    asserted.

    The gate serves drift about FILES. Both surfaces that record an operator
    approval are keyed to openclaw.json config paths, so neither can name one
    — and ``config_intent`` does not merely hold that convention, it RAISES.
    This is the fact the whole decision rests on, so it is executed here
    rather than described in a comment: if a future change makes a filename
    a legal ``field_path``, this test goes red and the source becomes worth
    reconsidering.
    """
    from evolve_admin import config_intent

    for name in ("AGENTS.md", "SOUL.md", "HEARTBEAT.md",
                 "procedures/security-cve-scan.md", ".zshrc"):
        with pytest.raises(ValueError):
            config_intent._validate_field_path(name)

    # A real config path is accepted, so the rejection above is about the
    # SHAPE of a filename and not about the validator refusing everything.
    config_intent._validate_field_path("agents.defaults.model")


def test_the_gate_registers_no_operator_source(shared):
    """The residue B1 left behind, pinned.

    ``shell_config`` was retired because its only source could never fire.
    ``identity_file`` kept that same source registered until 2026-09-02 — it
    could not fire there either, for exactly the same reason, and a test
    asserted it worked by hand-writing a sidecar the writer would have
    rejected. Neither the source nor its reader exists now.
    """
    assert not hasattr(da, "SOURCE_OPERATOR_INTENT")
    assert not hasattr(da, "_operator_events")
    assert not hasattr(da, "audit_log_entries")
    for kind, sources in da._KIND_SOURCES.items():
        assert "operator_intent" not in sources, (
            f"{kind} registers a source with nothing that can answer it"
        )
    # The same invariant for the kind B1 retired, held here as well as in
    # test_shell_config_is_outside_the_gate_entirely. Re-registering it under
    # ANY source — including a reachable one like deploy — is the thing to
    # catch, and duplicating the assertion is what stops it disappearing with
    # a single test again.
    assert da.KIND_SHELL_CONFIG not in da._KIND_SOURCES


def test_shell_config_is_outside_the_gate_entirely(shared):
    """The B1 re-registration guard, restored.

    ``shell_config`` is deliberately not registered: no Evolve code path
    writes a bot's ``.zshrc`` and no operator surface can name the file, so
    no allow-set for it could fire. It stays a posture finding in
    ``audit_shell_config`` and :func:`explain` answers None through the
    unregistered-kind path.

    NOTE ON THE FIXTURE, because removing this test once already cost the
    guard: the audit-log record below is deliberately IMPOSSIBLE — no writer
    can put a filename in ``oc_keys``. That is the point. It is a hostile
    input here ("even handed a record production could not write, the answer
    is still None"), not a claim that the path is reachable. An impossible
    record proving SUPPRESSION is sound; one proving EXPLANATION is the
    asserted-but-never-executed bug. This is the first kind. Do not delete it
    for looking like the second.
    """
    assert da.KIND_SHELL_CONFIG not in da._KIND_SOURCES

    # A same-minute deploy of this very bot, which WOULD explain a registered
    # per-bot kind — so the None below is the unregistered kind, not an
    # absent record.
    (shared / "install.json").write_text(json.dumps({
        "version": "2026.0901.3999",
        "bot_versions": {
            "team-bot-a": {"version": "2026.0901.3999",
                           "deployed_at": _iso(NOW - timedelta(minutes=1))},
        },
    }))
    (shared / "audit-log.jsonl").write_text(json.dumps({
        "timestamp": _iso(NOW - timedelta(hours=3)),
        "bot_id": "team-bot-a",
        "action": "save_model_settings",
        "actor": "pod-admin-user",
        "oc_keys": [".zshrc"],
    }) + "\n")

    change = da.DriftChange(kind=da.KIND_SHELL_CONFIG, bot_id="team-bot-a",
                            target="/home/team-bot-a/.zshrc",
                            content_hash="f" * 64, keys=(".zshrc",))
    assert da.explain(change, shared, now=NOW) is None
    # And an unexplained verdict writes no memo, so a later run re-asks.
    assert not (shared / "security" / "drift-explained.json").exists()


def test_an_audit_log_record_does_not_explain_an_identity_file(shared):
    """The behaviour that changed, from the outside.

    An admin-surface record naming the file — which production cannot write,
    but which the old test hand-wrote — no longer moves the verdict, because
    nothing reads that log any more.
    """
    (shared / "audit-log.jsonl").write_text(json.dumps({
        "timestamp": _iso(NOW - timedelta(hours=3)),
        "bot_id": "team-bot-a",
        "action": "models.set",
        "actor": "pod-admin-user",
        "oc_keys": ["AGENTS.md"],
    }) + "\n")
    (shared / "config_intents").mkdir()
    (shared / "config_intents" / "team-bot-a.json").write_text(json.dumps({
        "bot_id": "team-bot-a",
        "intents": [{
            "field_path": "AGENTS.md",
            "value": "custom",
            "reason": "hand-tuned intro paragraph",
            "set_at": _iso(NOW - timedelta(days=30)),
        }],
    }))
    assert da.explain(
        da.DriftChange(kind=da.KIND_IDENTITY_FILE, bot_id="team-bot-a",
                       target="/x/AGENTS.md", keys=("AGENTS.md",)),
        shared, now=NOW, memo=False,
    ) is None


# ── Source: the sudoers content match ─────────────────────────────────────────


def _write_marker(shared: Path, content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    state = shared / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "sudoers-installed.sha256").write_text(digest + "\n")
    return digest


def test_sudoers_matching_the_current_render_is_explained(shared, monkeypatch):
    rendered = "evolve ALL=(root) NOPASSWD: /bin/cat /etc/sudoers.d/evolve\n"
    monkeypatch.setattr(
        da, "_sudoers_render_hash",
        lambda: hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
    )
    found = da.explain(
        da.DriftChange(
            kind=da.KIND_SUDOERS_BASELINE, bot_id="evolve",
            target="/etc/sudoers.d/evolve",
            content_hash=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        ),
        shared, now=NOW, memo=False,
    )
    assert found is not None
    assert found.source == da.SOURCE_DEPLOY
    assert "this version of Evolve" in found.evidence


def test_sudoers_matching_the_install_marker_is_explained(shared, monkeypatch):
    """Second tier: the render is unavailable, but Evolve's own installer
    recorded writing exactly these bytes."""
    monkeypatch.setattr(da, "_sudoers_render_hash", lambda: None)
    digest = _write_marker(shared, "some installed grants\n")
    found = da.explain(
        da.DriftChange(kind=da.KIND_SUDOERS_BASELINE, bot_id="evolve",
                       target="/etc/sudoers.d/evolve", content_hash=digest),
        shared, now=NOW, memo=False,
    )
    assert found is not None
    assert found.source == da.SOURCE_DEPLOY
    assert "installer" in found.evidence


def test_a_hand_added_grant_matches_neither_tier(shared, monkeypatch):
    """The failure mode the whole gate exists for: the file is not what
    Evolve renders and not what its installer wrote."""
    rendered = "evolve ALL=(root) NOPASSWD: /bin/cat /etc/sudoers.d/evolve\n"
    monkeypatch.setattr(
        da, "_sudoers_render_hash",
        lambda: hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
    )
    _write_marker(shared, rendered)
    tampered = hashlib.sha256(
        (rendered + "evolve ALL=(ALL) NOPASSWD: ALL\n").encode("utf-8")
    ).hexdigest()
    assert da.explain(
        da.DriftChange(kind=da.KIND_SUDOERS_BASELINE, bot_id="evolve",
                       target="/etc/sudoers.d/evolve", content_hash=tampered),
        shared, now=NOW, memo=False,
    ) is None


def test_an_unavailable_render_with_no_marker_explains_nothing(shared, monkeypatch):
    """Cannot evaluate means cannot suppress."""
    monkeypatch.setattr(da, "_sudoers_render_hash", lambda: None)
    assert da.explain(
        da.DriftChange(kind=da.KIND_SUDOERS_BASELINE, bot_id="evolve",
                       target="/etc/sudoers.d/evolve", content_hash="f" * 64),
        shared, now=NOW, memo=False,
    ) is None


# ── Version skew is explained by definition ───────────────────────────────────


def test_version_skew_is_always_explained(shared):
    """A bot behind the admin server is Evolve shipping — it cannot be the
    security alert, on an empty pod with no records at all."""
    found = da.explain(
        da.DriftChange(kind=da.KIND_DEPLOY_VERSION), shared, now=NOW, memo=False,
    )
    assert found is not None
    assert found.source == da.SOURCE_BY_DEFINITION


# ── The allow-set is closed ───────────────────────────────────────────────────


def test_an_unregistered_kind_is_never_explained(shared):
    """A new producer that forgets to register its kind gets the loud
    answer, not the quiet one."""
    (shared / "install.json").write_text(json.dumps({
        "installed_at": _iso(NOW - timedelta(minutes=1)),
    }))
    assert da.explain(
        da.DriftChange(kind="something_new", bot_id="team-bot-a"),
        shared, now=NOW, memo=False,
    ) is None


def test_every_registered_kind_names_only_real_sources():
    known = {
        da.SOURCE_DEPLOY, da.SOURCE_SELF_UPDATE, da.SOURCE_BY_DEFINITION,
        da.SOURCE_OS_UPDATE,
    }
    for kind, sources in da._KIND_SOURCES.items():
        assert sources, f"{kind} has an empty allow-set"
        assert set(sources) <= known, f"{kind} names an unknown source"


# ── The explanation memo ──────────────────────────────────────────────────────


def test_an_explanation_survives_its_window_for_the_same_content(shared):
    """The reason the memo exists: a proposal applied to AGENTS.md yesterday
    must not turn into a page once its window closes, for a change nobody
    made since."""
    _applied_proposal(shared, "team-bot-a", NOW - timedelta(hours=1),
                      path="/home/team-bot-a/.openclaw/workspace/AGENTS.md")
    change = _identity("team-bot-a", "AGENTS.md", "a" * 64)
    first = da.explain(change, shared, now=NOW)
    assert first is not None and first.source == da.SOURCE_SELF_UPDATE

    later = NOW + da.SELF_UPDATE_WINDOW + timedelta(days=1)
    remembered = da.explain(change, shared, now=later)
    assert remembered is not None
    assert remembered.source == da.SOURCE_REMEMBERED


def test_the_memo_does_not_carry_over_to_different_content(shared):
    """An attacker cannot wait out the window: new content is a new hash and
    therefore a fresh question."""
    _applied_proposal(shared, "team-bot-a", NOW - timedelta(hours=1),
                      path="/home/team-bot-a/.openclaw/workspace/AGENTS.md")
    da.explain(_identity("team-bot-a", "AGENTS.md", "a" * 64), shared, now=NOW)

    later = NOW + da.SELF_UPDATE_WINDOW + timedelta(days=1)
    assert da.explain(_identity("team-bot-a", "AGENTS.md", "b" * 64),
                      shared, now=later) is None


def test_nothing_is_memoised_without_a_content_hash(shared):
    """The permission checks have no content hash, so there is nothing to
    key a memo on and none is written."""
    _per_bot_stamp(shared, "team-bot-a", NOW - timedelta(minutes=5))
    change = da.DriftChange(kind=da.KIND_POD_PERMS, bot_id="team-bot-a",
                            target="/home/team-bot-a/.openclaw")
    assert da.explain(change, shared, now=NOW) is not None
    assert not (shared / "security" / "drift-explained.json").exists()


def test_a_never_explained_change_has_no_memo_to_fall_back_on(shared):
    change = _identity("team-bot-a", "AGENTS.md", "c" * 64)
    assert da.explain(change, shared, now=NOW) is None
    assert da.explain(change, shared, now=NOW + timedelta(days=1)) is None


# ── partition ─────────────────────────────────────────────────────────────────


def test_partition_splits_on_the_same_rule_as_explain(shared):
    _per_bot_stamp(shared, "team-bot-a", NOW - timedelta(minutes=5))
    explained, unexplained = da.partition([
        da.DriftChange(kind=da.KIND_POD_PERMS, bot_id="team-bot-a",
                       target="/home/team-bot-a/.openclaw"),
        # A different bot, with no deploy stamp and nothing else on record.
        _identity("team-bot-b", "AGENTS.md", "d" * 64),
    ], shared, now=NOW)
    assert [c.target for c, _e in explained] == ["/home/team-bot-a/.openclaw"]
    assert [c.bot_id for c in unexplained] == ["team-bot-b"]


# ── The operator report ───────────────────────────────────────────────────────


def test_the_report_separates_explained_from_unexplained(shared):
    """The read-only weekly answer to "did anything change that nobody
    accounts for?" — the explanation is a column, not a paragraph."""
    logs = shared / "logs"
    logs.mkdir()
    recent = _iso(NOW - timedelta(days=1))
    stale = _iso(NOW - timedelta(days=30))
    logs.joinpath("audit.log").write_text("\n".join([
        f"{recent} [audit] OK: evolve: sudoers changed since baseline — "
        "the file matches the one this version of Evolve sets up",
        f"{recent} [audit] CRITICAL: 🔴 CRITICAL: team-bot-a .zshrc hash "
        "changed since baseline — baseline=aaa current=bbb",
        f"{recent} [audit] OK: team-bot-a: openclaw.json OK",
        f"{stale} [audit] CRITICAL: 🔴 CRITICAL: team-bot-b .zshrc hash "
        "changed since baseline — old",
    ]) + "\n")

    rows = da.report_rows(shared, days=7, now=NOW)

    assert len(rows) == 2, "unrelated lines and lines outside the window drop"
    by_check = {r.check: r for r in rows}
    assert by_check["sudoers"].explained is True
    assert "this version of Evolve sets up" in by_check["sudoers"].explanation
    assert by_check["shell startup"].explained is False
    assert by_check["shell startup"].explanation == ""

    rendered = da.render_report(rows)
    assert rendered.index("UNEXPLAINED") < rendered.index("explained "), (
        "the one that needs the operator has to come first"
    )
    assert "1 with nothing on record to explain them" in rendered


def test_the_report_says_nothing_when_it_cannot_read_the_log(shared):
    assert da.report_rows(shared, days=7, now=NOW) == []
    assert "No drift findings" in da.render_report([])
