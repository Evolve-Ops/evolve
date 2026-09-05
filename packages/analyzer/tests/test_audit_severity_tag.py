"""tests/test_audit_severity_tag.py — audit retrofit to severity framework.

Verifies the (vector, magnitude) tag emitted on audit Signals matches
the calibration in internal/spec-severity-framework-2026-05-18.md and that
the severity-framework resolver picks it up correctly.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402
import severity as sev  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── Tag mapping ──────────────────────────────────────────────────────────────


def _finding(category: str, level: str, **extra) -> audit.Finding:
    return audit.Finding(
        level=level,
        category=category,
        bot_id=extra.get("bot_id", "team_bot_a"),
        message=extra.get("message", f"{category} {level} on team_bot_a"),
        detail=extra.get("detail", ""),
        # Criticals require an explicit event-vs-posture classification (R-3).
        finding_kind=extra.get(
            "finding_kind", "event" if level == "critical" else ""
        ),
    )


def test_identity_warn_low_magnitude_security():
    v, m = audit._audit_severity_tag(_finding("identity", "warn"))
    assert v == "security"
    assert m == 1


def test_identity_critical_high_magnitude():
    v, m = audit._audit_severity_tag(_finding("identity", "critical"))
    assert v == "security"
    assert m == 3


def test_machine_critical_promoted_to_magnitude_4():
    """Machine criticals (firewall off, sudoers tamper) are the biggest
    audit findings — magnitude 4 by default."""
    v, m = audit._audit_severity_tag(_finding("machine", "critical"))
    assert v == "security"
    assert m == 4


def test_proposal_warn_is_quality():
    v, m = audit._audit_severity_tag(_finding("proposal", "warn"))
    assert v == "quality"
    assert m == 1


def test_unknown_category_falls_back_to_operations():
    f = audit.Finding(level="warn", category="something_new", bot_id="team_bot_a", message="x")
    v, m = audit._audit_severity_tag(f)
    assert v == "operations"
    assert m == 2   # warn default when no (cat, level) cell matches


# ── End-to-end: audit emit → signal store carries the tag ────────────────────


def test_emit_writes_vector_and_magnitude_in_details(tmp_path):
    warns = [_finding("identity", "warn", message="zshrc unreadable on team_bot_a")]
    audit._emit_signals_from_findings([], warns, tmp_path)
    sigs = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.details.get("vector") == "security"
    assert sig.details.get("magnitude") == 1


def test_resolver_picks_up_emitted_tag(tmp_path):
    """Round-trip: emit → store → resolve_severity reads explicit tag."""
    criticals = [_finding("machine", "critical", message="firewall is disabled")]
    audit._emit_signals_from_findings(criticals, [], tmp_path)
    sigs = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert len(sigs) == 1
    rating = sev.resolve_severity(sigs[0].to_dict())
    assert rating.vector == "security"
    assert rating.magnitude == 4


def test_machine_critical_pod_wide_active_leads_narrative(tmp_path):
    """An active machine-level critical (e.g., firewall off) should clear
    the lead-narrative threshold so it surfaces at the top of Home."""
    finding = audit.Finding(
        level="critical", finding_kind="posture", category="machine", bot_id=None,
        message="firewall disabled pod-wide",
    )
    audit._emit_signals_from_findings([finding], [], tmp_path)
    sigs = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert len(sigs) == 1
    rating = sev.resolve_severity(sigs[0].to_dict())
    score = sev.compose_priority(rating, scope="pod", is_active_outage=True)
    assert sev.priority_bucket(score) == "lead"


def test_warn_advisory_lands_in_small_bucket(tmp_path):
    """A typical audit warn (script inventory drift, etc.) should not
    crowd the main narrative — magnitude 1 × bot scope = 1.0 → small."""
    f = _finding("identity", "warn", message="script inventory drift +3 new")
    audit._emit_signals_from_findings([], [f], tmp_path)
    sigs = list(signals_store.iter_active(tmp_path, producer="audit"))
    assert len(sigs) == 1
    rating = sev.resolve_severity(sigs[0].to_dict())
    score = sev.compose_priority(rating, scope="bot")
    assert sev.priority_bucket(score) == "small"
