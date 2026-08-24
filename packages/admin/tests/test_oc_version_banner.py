"""GET /api/oc/version — the payload behind the "Update available" banner.

The banner is the only surface that tells an operator OpenClaw needs
upgrading, and (since internal/spec-oc-upgrade-from-ui-2026-07-28.md) the only
place they can act on it. Both halves turn on comparing two version strings
that arrive from different readers: the installed ``package.json`` and npm's
registry.

These tests pin the comparison itself, because getting it wrong is silent and
self-reinforcing:

* npm re-releases carry a ``-N`` tag (``2026.7.1-2``). Canonicalizing only the
  installed side reported a fully current host as "installed 2026.7.1, latest
  2026.7.1-2" — a permanent nag toward a version it was already running.
* The same mismatch made every safety report look stale seconds after it was
  written, and ``stale`` is what withholds the "Run upgrade now" button. So
  the phantom notice also hid the fix for itself.
* "The pod moved under this report" and "we can't read a version at all" both
  withhold the button, but only the first is worth re-checking — the payload
  keeps them distinct so the UI can say which one it is.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

import urllib.request as _ureq  # noqa: E402

from evolve_admin import safe_upgrade as su  # noqa: E402
from evolve_admin import upstream_version as uv  # noqa: E402
from evolve_admin.web import routes_maintenance as rm  # noqa: E402

REPORT_ID = "20260730T211513Z-d318b533"


class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._payload


@pytest.fixture
def banner(tmp_path, monkeypatch):
    """Return a callable: (installed, npm_latest, report) -> payload dict.

    Every leaf that touches the network, the filesystem, or a bot home is
    stubbed; what's left is exactly the version comparison under test.
    """
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "sharedDir": str(tmp_path / "shared"),
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "members": ["team_bot_a"],
    }))

    app = Flask(__name__)
    rm._register_maintenance_routes(app, network_path)
    client = app.test_client()

    monkeypatch.setattr(uv, "per_bot_versions", lambda *a, **k: {})
    monkeypatch.setattr(su, "inflight_report_id", lambda: None)

    def _call(installed: str | None, latest: str | None, report: dict | None = None):
        monkeypatch.setattr(uv, "installed_package_version", lambda *a, **k: installed)
        monkeypatch.setattr(
            _ureq, "urlopen", lambda *a, **k: _FakeResp({"version": latest})
        )
        monkeypatch.setattr(su, "load_latest_report", lambda **k: report)
        resp = client.get("/api/oc/version")
        assert resp.status_code == 200
        return resp.get_json()

    return _call


def _report(installed: str | None, resolved: str | None, ok: bool = True) -> dict:
    return {
        "report_id": REPORT_ID,
        "checked_at": "2026-07-30T21:15:22Z",
        "ok": ok,
        "summary": "All gates passed — safe to upgrade." if ok else "1 blocker",
        "current": {"installed_version": installed},
        "candidate": {"target_spec": "latest", "resolved_version": resolved},
    }


# ── The phantom update ───────────────────────────────────────────────────────


def test_npm_rerelease_tag_is_not_an_available_update(banner):
    """A host running the exact build npm calls latest has NO update.

    ``2026.7.1-2`` is what npm publishes for a re-release of ``2026.7.1``.
    Reporting the installed side as ``2026.7.1`` made this compare unequal
    forever — the nine-bots-all-⚠ state on a fully current pod.
    """
    d = banner("2026.7.1-2", "2026.7.1-2")
    assert d["installed"] == "2026.7.1-2"
    assert d["update_available"] is False
    assert d["update_count"] == 0


def test_a_real_update_is_still_reported(banner):
    d = banner("2026.7.1", "2026.7.1-2")
    assert d["update_available"] is True
    assert d["update_count"] == 1


def test_cosmetic_differences_are_not_an_update(banner):
    """A leading ``v`` on one side is not a new release."""
    assert banner("v2026.7.1-2", "2026.7.1-2")["update_available"] is False


def test_unreadable_installed_version_claims_no_update(banner):
    """Fail closed on the nag: an unknown installed version is not evidence
    that an upgrade is needed."""
    assert banner(None, "2026.7.1-2")["update_available"] is False


# ── Staleness — what withholds the "Run upgrade now" button ──────────────────


def test_a_matching_report_is_not_stale(banner):
    """The regression: a report written seconds ago, against the versions that
    are live right now, must not read as stale."""
    d = banner("2026.7.1-2", "2026.7.1-2", _report("2026.7.1-2", "2026.7.1-2"))
    assert d["safety_check"]["stale"] is False
    assert d["safety_check"]["stale_reason"] is None
    assert d["safety_check"]["ok"] is True


def test_report_written_before_an_out_of_band_upgrade_is_stale(banner):
    d = banner("2026.7.2", "2026.7.2", _report("2026.7.1-2", "2026.7.2"))
    assert d["safety_check"]["stale"] is True
    assert d["safety_check"]["stale_reason"] == "installed_changed"


def test_report_is_stale_when_npm_publishes_a_newer_candidate(banner):
    d = banner("2026.7.1", "2026.7.2", _report("2026.7.1", "2026.7.1-2"))
    assert d["safety_check"]["stale"] is True
    assert d["safety_check"]["stale_reason"] == "candidate_changed"


def test_unreadable_installed_version_is_distinguishable_from_stale(banner):
    """Both withhold the button; only one is worth re-checking. A report that
    recorded no installed version (the Linux-pod shape, where the reader used
    to look at a macOS-only path) says so rather than reading as 'stale'."""
    d = banner("2026.7.1-2", "2026.7.1-2", _report(None, "2026.7.1-2"))
    assert d["safety_check"]["stale"] is True
    assert d["safety_check"]["stale_reason"] == "installed_unknown"


def test_report_without_a_candidate_is_flagged_as_such(banner):
    d = banner("2026.7.1-2", "2026.7.1-2", _report("2026.7.1-2", None))
    assert d["safety_check"]["stale_reason"] == "candidate_unknown"


def test_no_report_yet_yields_no_safety_check(banner):
    assert banner("2026.7.1-2", "2026.7.1-2", None)["safety_check"] is None
