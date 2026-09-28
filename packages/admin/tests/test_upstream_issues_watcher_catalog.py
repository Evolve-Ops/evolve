"""Tests for upstream_issues_watcher catalog + dispatcher integration.

Pins:
  - Catalog has two events for this producer:
    ``updates.upstream_issue_activity`` (operator-facing, opt-in) and
    ``system.upstream_issues_watcher_auth`` (health Signal, on by default).
  - Dispatcher recognizes ``upstream_issues_watcher`` as a source so events
    actually route through subscriptions instead of being dropped.
  - Producer severity defaults to ``info``.
  - The activity event renders without crashing using the documented
    payload shape (the dispatch site's payload contract).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.alerts import catalog as cat  # noqa: E402
from evolve_admin.alerts import dispatcher as disp  # noqa: E402


def test_activity_event_present():
    e = cat.by_key("updates.upstream_issue_activity")
    assert e is not None
    assert e.producer_source == "upstream_issues_watcher"
    # Opt-in: standard installs shouldn't subscribe automatically.
    assert e.default_enabled is False


def test_auth_failure_event_present_and_default_on():
    e = cat.by_key("system.upstream_issues_watcher_auth")
    assert e is not None
    assert e.producer_source == "upstream_issues_watcher"
    # Auth-broken should be loud — operator can't reply to maintainers
    # if gh is wedged. Default-on subscription with an immediate cadence.
    assert e.default_enabled is True


def test_dispatcher_recognizes_upstream_source():
    """If the source isn't registered, dispatch silently drops events
    instead of routing them. Confirm registration."""
    assert "upstream_issues_watcher" in disp._DEFAULT_SOURCE_ENABLED
    assert disp._DEFAULT_SOURCE_ENABLED["upstream_issues_watcher"] is True
    # Cooldown=0 since dedup_keys are per-comment unique.
    assert disp._DEFAULT_SOURCE_COOLDOWN_SECONDS["upstream_issues_watcher"] == 0


def test_producer_severity_is_info():
    """Per producer_severity policy — informational, not warn."""
    sys.path.insert(0, str(_ANALYZER_PKG))
    from signals.producer_severity import default_severity  # noqa: E402
    assert default_severity("upstream_issues_watcher") == "info"


def test_activity_render_uses_canonical_payload_shape():
    """Render against the exact dispatch-side payload shape from the
    monitor — anchors the contract between producer and catalog."""
    e = cat.by_key("updates.upstream_issue_activity")
    payload = {
        "repo": "openclaw/openclaw",
        "issue": "#84820",
        "title": "Test title",
        "actor": "oc-maintainer",
        "event_kind": "new_comment",
        "body_short": "Test body excerpt",
        "url": "https://example/issue#comment-1",
    }
    rendered = cat.render_event(e, payload)
    # Each templated field appears at least once.
    assert "openclaw/openclaw" in rendered
    assert "#84820" in rendered
    assert "Test title" in rendered
    assert "oc-maintainer" in rendered
    assert "Test body excerpt" in rendered
    assert "https://example/issue#comment-1" in rendered
    # Subscription footer for grep-from-message-back-to-toggle.
    assert "subscription: updates.upstream_issue_activity" in rendered


def test_auth_failure_render_includes_remediation_command():
    """The auth-failure event's CLI action must literally name the command
    the operator needs to run — otherwise the alert is decorative."""
    e = cat.by_key("system.upstream_issues_watcher_auth")
    rendered = cat.render_event(e, {"message": "gh: not authenticated"})
    assert "gh auth login" in rendered
    assert "sudo -u evolve" in rendered
