"""tests/test_app_scan_status_tools.py — diagnostic + rescan tools.

Two tools shipped after a 2026-05-20 transcript where evo defaulted
to ``find /Users/...`` shell snippets when asked about a persistent
``scan_needed`` chip on the primary bot:

  * ``pod_state.app_scan_status(bot_id)`` — read-tier diagnostic.
    Returns the canonical .scan-status.json's existence, mtime,
    parsed contents, and a structured chip_firing_reason mirroring
    the chip rule from ``analyzer.tile_metrics``.

  * ``action.bot.rescan_apps(bot_id)`` — write_safe wrapper around
    ``POST /api/applications/scan?bot=X``. Same code the
    Applications page Run Scan button uses.

These tests stub the filesystem + the admin server HTTP for the
respective tools.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import time
from io import BytesIO
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))


def _seed_network(tmp_path: Path, shared_dir: Path | None = None,
                  admin_base_url: str = "http://test-host:5050") -> Path:
    """Write a minimal network.json with sharedDir pointed at tmp_path
    (or an explicit override). Returns the path."""
    sd = shared_dir or tmp_path
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "networkId": "test-pod",
        "sharedDir": str(sd),
        "adminBaseUrl": admin_base_url,
        "bots": {
            "evolve": {"role": "primary", "port": 19030},
            "team_bot_a": {"role": "member", "port": 18789},
        },
    }))
    return p


def _seed_scan_status(
    shared_dir: Path, bot_id: str, *,
    mtime_age_days: float = 0.0,
    contents: dict | None = None,
) -> Path:
    """Write a fake .scan-status.json with a controllable mtime age."""
    apps_dir = shared_dir / "applications" / bot_id
    apps_dir.mkdir(parents=True, exist_ok=True)
    status_path = apps_dir / ".scan-status.json"
    payload = contents or {
        "phase": 4, "phase_total": 4, "phase_name": "done",
        "found": 1, "manifests_created": 1, "manifests_total": 1,
    }
    status_path.write_text(json.dumps(payload))
    if mtime_age_days > 0:
        now = time.time()
        past = now - (mtime_age_days * 86400)
        import os
        os.utime(status_path, (past, past))
    return status_path


# ── pod_state.app_scan_status ────────────────────────────────────────────────


def test_scan_status_file_missing_chip_firing(tmp_path):
    """Canonical file doesn't exist → chip_firing=True with the
    'never scanned' explanation that names the stamping step."""
    from evolve_admin.evo.tools import pod_state_app_scan_status
    network_path = _seed_network(tmp_path)
    # Seed an apps dir with one manifest so apps_total > 0 (the chip
    # only fires when the bot has apps to scan).
    (tmp_path / "applications" / "evolve").mkdir(parents=True, exist_ok=True)
    (tmp_path / "applications" / "evolve" / "some_app.json").write_text("{}")

    result = pod_state_app_scan_status._handler(
        bot_id="evolve", network_path=network_path,
    )
    assert result["ok"] is True
    assert result["file_exists"] is False
    assert result["mtime"] is None
    assert result["days_old"] is None
    assert result["chip_firing_reason"]["chip_firing"] is True
    # The explanation should call out the stamping step explicitly so
    # the model can answer 'why is the chip still on'.
    assert "missing" in result["chip_firing_reason"]["explanation"].lower()
    assert "stamping" in result["chip_firing_reason"]["explanation"].lower()


def _seed_app_manifest(shared_dir: Path, bot_id: str, name: str = "demo_app") -> None:
    """Place a fake app manifest in the bot's applications dir so
    apps_total>0 (the chip rule's precondition)."""
    apps_dir = shared_dir / "applications" / bot_id
    apps_dir.mkdir(parents=True, exist_ok=True)
    (apps_dir / f"{name}.json").write_text("{}")


def test_scan_status_recent_file_chip_not_firing(tmp_path):
    """File exists, freshly written → chip should NOT be firing.
    Operator-visible: 'the chip might be a stale tile cache — page
    refresh should clear it'."""
    from evolve_admin.evo.tools import pod_state_app_scan_status
    network_path = _seed_network(tmp_path)
    _seed_app_manifest(tmp_path, "evolve")
    _seed_scan_status(tmp_path, "evolve", mtime_age_days=0.0)

    result = pod_state_app_scan_status._handler(
        bot_id="evolve", network_path=network_path,
    )
    assert result["ok"] is True
    assert result["file_exists"] is True
    assert result["chip_firing_reason"]["chip_firing"] is False


def test_scan_status_stale_file_chip_firing(tmp_path):
    """File exists but >30 days old → chip fires with the
    'last scanned Nd ago' detail. (Requires apps_total>0 — the chip
    rule's precondition.)"""
    from evolve_admin.evo.tools import pod_state_app_scan_status
    network_path = _seed_network(tmp_path)
    _seed_app_manifest(tmp_path, "evolve")
    _seed_scan_status(tmp_path, "evolve", mtime_age_days=45.0)

    result = pod_state_app_scan_status._handler(
        bot_id="evolve", network_path=network_path,
    )
    assert result["ok"] is True
    assert result["file_exists"] is True
    assert result["days_old"] is not None and result["days_old"] >= 30
    assert result["chip_firing_reason"]["chip_firing"] is True
    assert "days ago" in result["chip_firing_reason"]["explanation"]


def test_scan_status_returns_parsed_contents(tmp_path):
    """The parsed JSON contents come back so the model can quote
    scanner-reported phase/found/etc. without re-reading the file."""
    from evolve_admin.evo.tools import pod_state_app_scan_status
    network_path = _seed_network(tmp_path)
    _seed_scan_status(tmp_path, "evolve", contents={
        "phase": 4, "phase_name": "done", "found": 7,
        "manifests_created": 7, "manifests_total": 7,
    })

    result = pod_state_app_scan_status._handler(
        bot_id="evolve", network_path=network_path,
    )
    assert result["contents"]["found"] == 7
    assert result["contents"]["phase_name"] == "done"


def test_scan_status_no_apps_chip_does_not_fire(tmp_path):
    """When the bot has no apps to scan, the chip should not fire
    regardless of file existence — the chip-rule has an apps_total>0
    precondition in tile_metrics."""
    from evolve_admin.evo.tools import pod_state_app_scan_status
    network_path = _seed_network(tmp_path)
    # Don't seed any apps directory.

    result = pod_state_app_scan_status._handler(
        bot_id="evolve", network_path=network_path,
    )
    assert result["ok"] is True
    assert result["apps_total"] == 0
    assert result["chip_firing_reason"]["chip_firing"] is False
    assert "no apps" in result["chip_firing_reason"]["explanation"].lower()


def test_scan_status_missing_network_path_clear_error():
    from evolve_admin.evo.tools import pod_state_app_scan_status
    result = pod_state_app_scan_status._handler(bot_id="evolve")
    assert result["ok"] is False
    assert "network_path" in result["error"].lower()


def test_scan_status_empty_bot_id_rejected(tmp_path):
    from evolve_admin.evo.tools import pod_state_app_scan_status
    network_path = _seed_network(tmp_path)
    result = pod_state_app_scan_status._handler(
        bot_id="", network_path=network_path,
    )
    assert result["ok"] is False
    assert "bot_id" in result["error"]


def test_scan_status_threshold_agrees_with_tile_metrics():
    """The chip-rule threshold must agree with tile_metrics. If the
    analyzer constant changes, this fails — catching the drift.

    Reads the value directly from the analyzer source file rather
    than importing it, so the check runs even when analyzer isn't on
    sys.path in the test env. The grep-based approach is acceptable
    here because the constant is a top-level integer literal whose
    shape is unlikely to drift away from ``APPS_UNTESTED_DAYS = <int>``.
    """
    from evolve_admin.evo.tools import pod_state_app_scan_status as t
    import re
    analyzer_src = (
        _ADMIN_PKG.parent / "analyzer" / "tile_metrics.py"
    )
    if not analyzer_src.exists():
        pytest.skip("analyzer/tile_metrics.py not found in this layout")
    body = analyzer_src.read_text(encoding="utf-8")
    m = re.search(r"^APPS_UNTESTED_DAYS\s*=\s*(\d+)", body, re.MULTILINE)
    assert m, (
        "APPS_UNTESTED_DAYS top-level assignment not found in "
        "tile_metrics.py — either the constant moved or its shape "
        "changed. Update this drift check."
    )
    upstream = int(m.group(1))
    assert t._APPS_UNTESTED_DAYS == upstream, (
        f"pod_state.app_scan_status threshold ({t._APPS_UNTESTED_DAYS}) "
        f"is out of sync with tile_metrics.APPS_UNTESTED_DAYS "
        f"({upstream}). Update _APPS_UNTESTED_DAYS to match."
    )


def test_scan_status_tool_registered():
    from evolve_admin.evo.tools import lookup
    t = lookup("pod_state.app_scan_status")
    assert t is not None
    assert t.risk_tier.name == "READ"


# ── action.bot.rescan_apps ──────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_rescan_apps_posts_to_admin_server(monkeypatch, tmp_path):
    """Happy path: POSTs to the admin endpoint with the right bot_id
    query param + returns the scan status from the admin server."""
    from evolve_admin.evo.tools import action_bot
    network_path = _seed_network(tmp_path)

    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResp(json.dumps({"status": "started", "quick": False}))

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    result = action_bot._rescan_apps_handler(
        network_path=network_path, bot_id="evolve",
        reason="operator says chip persists",
    )
    assert result["ok"] is True
    assert result["bot_id"] == "evolve"
    assert result["status"] == "started"
    assert captured["url"] == "http://test-host:5050/api/applications/scan?bot=evolve"
    assert captured["method"] == "POST"
    # verify_via points at the scan-status tool so the model can poll
    # for completion.
    assert result["verify_via"]["tool"] == "pod_state.app_scan_status"
    assert result["verify_via"]["args"]["bot_id"] == "evolve"


def test_rescan_apps_already_running_is_ok(monkeypatch, tmp_path):
    """Admin server reports a scan is already in progress → that's
    not an error from the tool's POV; the operator's intent (scan
    running) is satisfied."""
    from evolve_admin.evo.tools import action_bot
    network_path = _seed_network(tmp_path)

    import urllib.request
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp(json.dumps({"status": "already_running"})),
    )

    result = action_bot._rescan_apps_handler(
        network_path=network_path, bot_id="evolve",
    )
    assert result["ok"] is True
    assert result["status"] == "already_running"


def test_rescan_apps_unknown_bot_400(tmp_path):
    """Bot not in network.json → validate-style error from the
    handler before any HTTP call fires."""
    from evolve_admin.evo.tools import action_bot
    network_path = _seed_network(tmp_path)
    result = action_bot._rescan_apps_handler(
        network_path=network_path, bot_id="ghost",
    )
    assert result["ok"] is False
    assert "not registered" in result["error"]


def test_rescan_apps_admin_unreachable(monkeypatch, tmp_path):
    """Network drop / admin restart in the middle → URLError →
    structured error with the URL so the operator can sanity-check
    config."""
    from evolve_admin.evo.tools import action_bot
    network_path = _seed_network(tmp_path)

    def _refuse(req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _refuse)

    result = action_bot._rescan_apps_handler(
        network_path=network_path, bot_id="evolve",
    )
    assert result["ok"] is False
    assert "unreachable" in result["error"].lower()


def test_rescan_apps_validate():
    """Validate is the third-gate check — same pattern as other
    action tools. Returns ok=True for known bots, ok=False otherwise."""
    from evolve_admin.evo.tools import action_bot
    tmp = Path("/tmp/test_rescan_validate")
    tmp.mkdir(exist_ok=True)
    network_path = _seed_network(tmp)
    assert action_bot._rescan_apps_validate(
        network_path=network_path, bot_id="evolve",
    )["ok"] is True
    assert action_bot._rescan_apps_validate(
        network_path=network_path, bot_id="",
    )["ok"] is False
    assert action_bot._rescan_apps_validate(
        network_path=network_path, bot_id="ghost",
    )["ok"] is False


def test_rescan_apps_tool_registered():
    from evolve_admin.evo.tools import lookup
    t = lookup("action.bot.rescan_apps")
    assert t is not None
    assert t.risk_tier.name == "WRITE_SAFE"
    assert t.validate is not None
