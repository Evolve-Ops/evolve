"""tests/test_audit_finding_kind.py — event-vs-posture taxonomy (R-3).

Design: internal/design-security-alert-fatigue-2026-08-31.md §2.3. Every
``level="critical"`` audit finding must carry an EXPLICIT per-check
classification:

  * ``"event"``   — active compromise / credential exposure; page now.
  * ``"posture"`` — standing configuration violation; page once on
                    discovery, then board/digest.

Three layers are pinned here:

1. ``Finding.__post_init__`` rejects an unclassified critical at
   construction time (runtime floor — no critical can exist unclassified).
2. An AST completeness sweep over audit.py: every ``Finding(...)`` call
   site that can produce a critical passes ``finding_kind=`` explicitly
   (static floor — a new emit site fails this test even if no unit test
   ever constructs it).
3. ``_emit_signals_from_findings`` propagates the classification into the
   mirrored Signal's ``details.finding_kind`` so the Alerts page and
   downstream delivery policy can distinguish the two.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── 1. Construction-time validation ──────────────────────────────────────────


def test_critical_without_finding_kind_raises():
    with pytest.raises(ValueError, match="finding_kind"):
        audit.Finding(
            level="critical", category="machine", bot_id=None,
            message="unclassified critical",
        )


def test_critical_with_bogus_finding_kind_raises():
    with pytest.raises(ValueError, match="finding_kind"):
        audit.Finding(
            level="critical", category="machine", bot_id=None,
            message="bogus kind", finding_kind="urgent",
        )


def test_critical_accepts_event_and_posture():
    for kind in ("event", "posture"):
        f = audit.Finding(
            level="critical", category="machine", bot_id=None,
            message=f"classified {kind}", finding_kind=kind,
        )
        assert f.finding_kind == kind


def test_non_critical_defaults_to_unclassified():
    for level in ("warn", "ok", "skipped"):
        f = audit.Finding(
            level=level, category="config", bot_id="team_bot_a",
            message=f"{level} finding",
        )
        assert f.finding_kind == ""


def test_non_critical_rejects_bogus_kind():
    with pytest.raises(ValueError, match="finding_kind"):
        audit.Finding(
            level="warn", category="config", bot_id="team_bot_a",
            message="warn with bogus kind", finding_kind="urgent",
        )


# ── 2. AST completeness — every critical emit site is classified ─────────────


def _finding_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Finding"
        ):
            yield node


def test_every_critical_emit_site_passes_finding_kind_explicitly():
    """Static sweep of audit.py: any ``Finding(...)`` call whose ``level``
    is the literal ``"critical"`` OR a runtime variable (the cron-health
    sites compute ``level = "critical" if ... else "warn"``) must pass a
    ``finding_kind=`` keyword at the call site. This is what makes the
    classification an explicit per-check decision rather than a default —
    a new critical emit site fails here (and at __post_init__) until its
    author chooses event or posture."""
    src = (_ANALYZER_DIR / "audit.py").read_text()
    tree = ast.parse(src)
    unclassified = []
    positional = []
    for call in _finding_calls(tree):
        # Positional construction would let a "critical" slip past the
        # kwarg inspection below (co-sign note) — __post_init__ still
        # catches it at runtime, but keep the static floor complete by
        # requiring keyword-only construction in audit.py.
        if call.args:
            positional.append(call.lineno)
            continue
        kwargs = {k.arg: k.value for k in call.keywords if k.arg}
        level = kwargs.get("level")
        if level is None:
            continue  # not a level-carrying construction (e.g. lint fixture)
        if isinstance(level, ast.Constant):
            if level.value != "critical":
                continue  # statically non-critical
        # A non-constant level can be "critical" at runtime → must classify.
        if "finding_kind" not in kwargs:
            unclassified.append(call.lineno)
    assert not positional, (
        "audit.py Finding constructions must use keyword arguments so the "
        f"level/finding_kind sweep can see them; positional at: {positional}"
    )
    assert not unclassified, (
        "audit.py critical-capable Finding call sites missing an explicit "
        f"finding_kind= classification at lines: {unclassified}. Classify "
        "each as 'event' (active compromise / credential exposure — page "
        "now) or 'posture' (standing config violation — page once, then "
        "board). When genuinely ambiguous, choose 'event'."
    )


def test_both_kinds_are_in_use():
    """Sanity: the taxonomy is real — audit.py uses both classifications
    (all-event would mean the posture split never happened; all-posture
    would mean nothing pages)."""
    src = (_ANALYZER_DIR / "audit.py").read_text()
    kinds = set()
    for call in _finding_calls(ast.parse(src)):
        for k in call.keywords:
            if k.arg == "finding_kind" and isinstance(k.value, ast.Constant):
                if k.value.value:
                    kinds.add(k.value.value)
            elif k.arg == "finding_kind" and isinstance(k.value, ast.IfExp):
                for side in (k.value.body, k.value.orelse):
                    if isinstance(side, ast.Constant) and side.value:
                        kinds.add(side.value)
    assert kinds == {"event", "posture"}


# ── 3. Propagation into the mirrored Signal ──────────────────────────────────


def _emit(tmp_path, criticals, warns=()):
    audit._emit_signals_from_findings(list(criticals), list(warns), tmp_path)
    return list(signals_store.iter_active(tmp_path, producer="audit"))


def test_posture_classification_lands_in_signal_details(tmp_path):
    f = audit.Finding(
        level="critical", category="machine", bot_id=None,
        message="🔴 CRITICAL: SSH PasswordAuthentication is enabled",
        finding_kind="posture",
    )
    sigs = _emit(tmp_path, [f])
    assert len(sigs) == 1
    assert sigs[0].details["finding_kind"] == "posture"


def test_event_classification_lands_in_signal_details(tmp_path):
    f = audit.Finding(
        level="critical", category="identity", bot_id="team_bot_a",
        message="team_bot_a: Telegram bot token found in workspace file: x.md",
        finding_kind="event",
    )
    sigs = _emit(tmp_path, [f])
    assert len(sigs) == 1
    assert sigs[0].details["finding_kind"] == "event"


def test_unclassified_warn_signal_carries_no_finding_kind_key(tmp_path):
    w = audit.Finding(
        level="warn", category="config", bot_id="team_bot_a",
        message="team_bot_a: port mismatch",
    )
    sigs = _emit(tmp_path, [], [w])
    assert len(sigs) == 1
    assert "finding_kind" not in sigs[0].details
