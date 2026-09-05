"""The falsifier: the SAME sudoers change, explained and unexplained.

This is the proof of both halves of the mandate — "not over-alerting, but not
missing major incursions" — in one fixture. Two runs of
``audit_evolve_sudoers`` see the identical hash change against the identical
stale baseline. The only difference between them is whether a record exists
that accounts for the new content:

  * with a matching install record → no critical finding, no page
  * without one                    → a CRITICAL finding classified ``event``,
                                     delivered IMMEDIATE through the real
                                     page dispatcher

Neither half is meaningful alone. A gate that suppresses everything passes the
first; a gate that suppresses nothing passes the second. Only the pair shows
the gate discriminating.

The delivery half runs against the REAL dispatcher — only the wire send is
stubbed — for the same reason ``test_audit_critical_page_routing.py`` does:
what matters is the actual catalog → subscription → frequency resolution, not
a mock of it.

Spec: internal/spec-drift-alert-taxonomy-2026-06-26.md (L2).
Design: internal/design-security-alert-fatigue-2026-08-31.md (R-3 finding_kind).
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import audit  # noqa: E402
import drift_authorization as da  # noqa: E402


# The change under test: a grant is added to the file the baseline was taken
# from. Byte-for-byte the same in both halves of the pair.
BASELINE_SUDOERS = (
    "Defaults:evolve secure_path=/usr/bin:/bin:/usr/sbin:/sbin\n"
    "evolve ALL=(root) NOPASSWD: /bin/cat /etc/sudoers.d/evolve\n"
)
CHANGED_SUDOERS = BASELINE_SUDOERS + (
    "evolve ALL=(root) NOPASSWD: /bin/chmod 600 /home/*/.openclaw/openclaw.json\n"
)

BASELINE_HASH = hashlib.sha256(BASELINE_SUDOERS.encode()).hexdigest()
CHANGED_HASH = hashlib.sha256(CHANGED_SUDOERS.encode()).hexdigest()


@pytest.fixture
def pod(tmp_path, monkeypatch):
    """A pod whose live sudoers has changed and whose baseline has not.

    ``sha256_sudo`` is stubbed because the real one shells out to
    ``sudo /bin/cat`` against the host's own ``/etc/sudoers.d/evolve``; the
    stub is the ONLY thing standing in for the live file, and it returns the
    same hash in both halves of the pair.

    ``_sudoers_render_hash`` is stubbed to unavailable so the pair turns on
    the install marker alone. The render tier has its own coverage in
    test_drift_authorization.py; mixing the two would let the pair pass for
    the wrong reason.
    """
    shared = tmp_path / "evolve"
    (shared / "security").mkdir(parents=True)
    (shared / "security" / "sudoers-evolve.sha256").write_text(BASELINE_HASH)

    monkeypatch.setattr(audit, "sha256_sudo", lambda path: CHANGED_HASH)
    monkeypatch.setattr(audit, "_lint_sudoers_content", lambda path: [])
    monkeypatch.setattr(da, "_sudoers_render_hash", lambda: None)
    return shared


def _record_the_install(shared: Path, content: str) -> None:
    """What a successful root ``refresh-sudoers`` leaves behind."""
    state = shared / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "sudoers-installed.sha256").write_text(
        hashlib.sha256(content.encode()).hexdigest() + "\n"
    )


def _criticals(findings):
    return [f for f in findings if f.level == "critical"]


# ── Half one: explained ───────────────────────────────────────────────────────


def test_with_a_matching_install_record_the_change_is_not_a_finding(pod):
    _record_the_install(pod, CHANGED_SUDOERS)

    findings = audit.audit_evolve_sudoers(pod, {})

    assert _criticals(findings) == [], (
        "Evolve's own installer wrote this exact file — reporting it as a "
        "critical security finding is the cry-wolf this gate exists to stop"
    )
    explained = [f for f in findings if "changed since baseline" in f.message]
    assert len(explained) == 1
    assert explained[0].level == "ok"
    assert "installer" in explained[0].message


# ── Half two: unexplained ─────────────────────────────────────────────────────


def test_without_one_the_same_change_is_a_critical_event(pod):
    # The marker records the file Evolve installed. The live file is not it.
    _record_the_install(pod, BASELINE_SUDOERS)

    findings = audit.audit_evolve_sudoers(pod, {})

    criticals = _criticals(findings)
    assert len(criticals) == 1, "an unexplained change to the grants file must fire"
    finding = criticals[0]
    assert finding.finding_kind == "event", (
        "an unexplained privilege change is an event, not a standing posture "
        "violation — it says somebody may be acting right now"
    )
    assert finding.category == "config"
    assert "no authorized change explains it" in finding.message


def test_the_pair_differs_only_in_the_record(pod):
    """Same live file, same baseline, opposite outcomes.

    If a future change made both halves agree, one of them is wrong and this
    test says so — which is the point of keeping the pair in one place.
    """
    _record_the_install(pod, CHANGED_SUDOERS)
    explained = audit.audit_evolve_sudoers(pod, {})

    _record_the_install(pod, BASELINE_SUDOERS)
    # The memo must not carry the first half's verdict into the second: the
    # first half was explained by a record that no longer holds, and the memo
    # is keyed on content, so it WOULD carry over. That is correct behaviour
    # for a real pod (the content was once accounted for) and wrong for this
    # fixture, which is asking what the live sources say. Clear it.
    (pod / "security" / "drift-explained.json").unlink(missing_ok=True)
    unexplained = audit.audit_evolve_sudoers(pod, {})

    assert _criticals(explained) == []
    assert len(_criticals(unexplained)) == 1


# ── The unexplained half reaches the operator ─────────────────────────────────


def test_the_unexplained_finding_pages_immediately(pod, monkeypatch):
    """Through the REAL dispatcher, on a default-configured pod.

    ``security.audit_critical`` is CRITICAL / IMMEDIATE-only, so the page
    lands on the channel rather than in tomorrow's digest. The wire check is
    the guard — the return value alone would still pass if the message were
    routed to a digest queue (#3930 maps DEFERRED onto delivered).
    """
    from evolve_admin.alerts import dispatcher

    wire: list[tuple[str, str]] = []
    monkeypatch.setattr(
        dispatcher, "_dispatch_via_telegram_http",
        lambda chat_id, message: (wire.append((chat_id, message)), (True, None))[1],
    )

    _record_the_install(pod, BASELINE_SUDOERS)
    finding = _criticals(audit.audit_evolve_sudoers(pod, {}))[0]

    config = {
        "alerts": {"channel": "telegram", "chatId": "12345"},
        "sharedDir": str(pod),
    }
    message = (
        "🔴 <b>Evolve Security Audit — CRITICAL Findings</b>\n\n"
        f"• {audit._html_escape(finding.message)}"
    )
    assert audit._send_via_dispatcher(message, pod, config) == audit._DISPATCH_DELIVERED
    assert wire, (
        "an unexplained change to the grants file did not reach the operator"
    )
    assert "no authorized change explains it" in wire[0][1]
    assert not (pod / "alerts" / "digest-pending").exists(), (
        "the one honest tamper alert was queued for a digest instead of paging"
    )


def test_the_explained_half_produces_nothing_to_page(pod, monkeypatch):
    """The other side of the same delivery claim: an explained change never
    reaches the batch at all, so there is nothing for the dispatcher to send."""
    from evolve_admin.alerts import dispatcher

    wire: list[tuple[str, str]] = []
    monkeypatch.setattr(
        dispatcher, "_dispatch_via_telegram_http",
        lambda chat_id, message: (wire.append((chat_id, message)), (True, None))[1],
    )

    _record_the_install(pod, CHANGED_SUDOERS)
    assert _criticals(audit.audit_evolve_sudoers(pod, {})) == []
    assert wire == []


# ── The edr must-always-page floor ────────────────────────────────────────────


def test_the_unexplained_finding_is_neither_flap_gated_nor_settle_gated(pod):
    """The floor: an unauthorized change to a permission file pages on cycle
    one, on a fresh pod, every time.

    ``_is_flap_prone_perm_finding`` covers warn-level perm findings only and
    ``_is_identity_mismatch_finding`` matches the two backup-comparison
    message shapes, so neither claims this finding; and the settle gate never
    withholds alert level. Pinned here rather than assumed, because the floor
    is the half of the mandate that a quieting change would break silently.
    """
    _record_the_install(pod, BASELINE_SUDOERS)
    finding = _criticals(audit.audit_evolve_sudoers(pod, {}))[0]

    assert not audit._is_flap_prone_perm_finding(finding)
    assert not audit._is_identity_mismatch_finding(finding)

    from signals import settle_gate
    assert not settle_gate.should_withhold(
        pod, severity="alert", transient=False,
    )


# ── The identity-file pair ────────────────────────────────────────────────────
#
# Same SOUL.md change on the same bot, twice. The only difference is what the
# self-update record DECLARED: once the file itself (an approved SoulEdit —
# AgentsAppend delegates to the same applier, which records the path it
# wrote), once an unrelated config key (a routine model-routing change, the
# record shape the retired apply daemon actually wrote). The first is
# explained; the second is not, and the memo does not get written for it.

import json  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

IDENTITY_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
SOUL_HASH = "f" * 64


def _soul_change() -> da.DriftChange:
    return da.DriftChange(
        kind=da.KIND_IDENTITY_FILE, bot_id="team-bot-a",
        target="/home/team-bot-a/.openclaw/workspace/SOUL.md",
        content_hash=SOUL_HASH, keys=("SOUL.md",),
    )


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_an_approved_soul_edit(shared: Path) -> None:
    d = shared / "proposals" / "applied"
    d.mkdir(parents=True, exist_ok=True)
    (d / "team-bot-a-soul.json").write_text(json.dumps({
        "id": "soul", "bot_id": "team-bot-a", "status": "applied",
        "title": "soften the greeting",
        "provenance": {"signals": {"_apply_details": {
            "path": "/home/team-bot-a/.openclaw/workspace/SOUL.md",
            "operation": "append_section",
        }}},
        "history": [{"from_status": "approved_user", "to_status": "applied",
                     "at": _stamp(IDENTITY_NOW - timedelta(hours=2)),
                     "actor": "arbiter", "reason": "applied"}],
    }))


def _record_a_routing_change(shared: Path) -> None:
    d = shared / "proposals" / "apply-results"
    d.mkdir(parents=True, exist_ok=True)
    (d / "team-bot-a-routing.json").write_text(json.dumps({
        "status": "applied",
        "applied_at": _stamp(IDENTITY_NOW - timedelta(hours=2)),
        "title": "route mechanical turns to the cheap rung",
        "proposed_change": {"agents.defaultModel": "cheap-rung"},
    }))


def test_a_soul_edit_that_declared_the_file_explains_the_soul_change(pod):
    _record_an_approved_soul_edit(pod)
    found = da.explain(_soul_change(), pod, now=IDENTITY_NOW)
    assert found is not None and found.source == da.SOURCE_SELF_UPDATE
    assert "soften the greeting" in found.evidence


def test_a_routing_change_on_the_same_bot_does_not_explain_it(pod):
    _record_a_routing_change(pod)
    assert da.explain(_soul_change(), pod, now=IDENTITY_NOW) is None
    # ...and nothing is remembered, so the 24h window cannot become a year.
    assert not (pod / "security" / "drift-explained.json").exists()
    assert da.explain(_soul_change(), pod, now=IDENTITY_NOW + timedelta(days=30)) is None


def test_the_identity_pair_differs_only_in_what_was_declared(pod):
    _record_an_approved_soul_edit(pod)
    explained = da.explain(_soul_change(), pod, now=IDENTITY_NOW)

    (pod / "proposals" / "applied" / "team-bot-a-soul.json").unlink()
    (pod / "security" / "drift-explained.json").unlink(missing_ok=True)
    _record_a_routing_change(pod)
    unexplained = da.explain(_soul_change(), pod, now=IDENTITY_NOW)

    assert explained is not None
    assert unexplained is None
