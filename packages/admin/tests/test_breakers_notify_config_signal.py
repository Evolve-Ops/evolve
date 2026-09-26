"""Tests for breaker-notify-survives-openclaw-config-validation item 4:
a notify path that cannot start fires a Signal, not just a log line.

Pins:
  - ``_is_config_validation_refusal`` classifies the exact CLI refusal text
    the 2026-09-20 incident recorded, and nothing else.
  - A dispatcher stub returning that refusal text → the NotifyResult is
    ``not_delivered`` AND a firing Signal is observed (pod-scoped, dedup'd
    by signature).
  - The next round, once a notice actually delivers, auto-resolves the
    Signal — a mere retry that still fails does NOT resolve it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, str(_path))

from evolve_admin import breakers_enforce  # noqa: E402

# The exact detail text the 2026-09-20 incident recorded on notify_trip /
# notify_reset rows (internal/incident-post-mortem-2026-09-20-image-turn-
# cache-thrash-and-poll-loop.md §4).
_RECORDED_REFUSAL = (
    "OpenClaw config is invalid … `meta`: Unrecognized key `lastTouchedAt`; "
    "`agents.defaults.contextPruning`: Unrecognized key `keepLastAssistants`; "
    "`plugins`: Unrecognized key `bundledDiscovery`"
)


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "evolve"
    (sd / "signals" / "firing").mkdir(parents=True)
    (sd / "signals" / "snoozed").mkdir(parents=True)
    (sd / "signals" / "archived").mkdir(parents=True)
    (sd / "breakers" / "log").mkdir(parents=True)
    return sd


@pytest.fixture
def network() -> dict:
    return {
        "bots": {
            "team_bot_a": {
                "user": "team_bot_a", "port": 19001,
                "primary_user": {"external_ids": {"telegram": "chat-a"}},
            },
        },
    }


@pytest.fixture
def mock_dispatch(monkeypatch: pytest.MonkeyPatch):
    """Like test_breakers_notify's fixture, but the outcome is settable to
    an arbitrary (ok, error) pair per call — needed to hand back the exact
    CLI refusal text."""
    outcomes: list[tuple[bool, str | None]] = []

    def _fake(channel, chat_id, message, gateway_port=None, **_kwargs):
        return outcomes.pop(0) if outcomes else (True, None)

    from evolve_admin.alerts import dispatcher
    monkeypatch.setattr(dispatcher, "_dispatch_via_openclaw", _fake)
    monkeypatch.setattr(
        dispatcher, "_dispatch_via_telegram_http",
        lambda chat_id, message: (False, "no-telegram-token: test"),
    )
    return outcomes


# ─── classifier ─────────────────────────────────────────────────────────────


def test_classifier_matches_the_recorded_refusal():
    assert breakers_enforce._is_config_validation_refusal(_RECORDED_REFUSAL) is True


@pytest.mark.parametrize("error", [
    None, "", "openclaw message send timed out after 60s",
    "GatewayTransportError: gateway timeout after 10000ms",
    "telegram http 400: chat not found",
])
def test_classifier_does_not_match_other_failures(error):
    assert breakers_enforce._is_config_validation_refusal(error) is False


# ─── firing ──────────────────────────────────────────────────────────────


def test_refused_notify_is_not_delivered_and_fires_a_signal(
    network, shared_dir, mock_dispatch,
):
    mock_dispatch.append((False, _RECORDED_REFUSAL))

    notifications = breakers_enforce._notify_user_channels(
        bots=[("team_bot_a", "team_bot_a")], action="trip",
        network=network, message="halted", dry_run=False,
    )
    assert notifications[0].delivery == "not_delivered"

    breakers_enforce._record_notify_delivery(
        shared_dir=shared_dir, breaker_type="full", notifications=notifications,
    )

    firing = list((shared_dir / "signals" / "firing").iterdir())
    assert len(firing) == 1
    sig = json.loads(firing[0].read_text())
    assert sig["producer"] == "breakers_enforce"
    assert sig["type"] == "notify_config_invalid"
    assert sig["scope"] == "pod"
    assert sig["severity"] == "alert"
    assert "cannot be delivered" in sig["title"]
    assert "team_bot_a" in sig["details"]["affected_bots"]

    # Delivery-ledger row still lands as before — item 4 ADDS the Signal,
    # it doesn't replace the existing forensics.
    log_lines = (shared_dir / "breakers" / "log").glob("*.jsonl")
    rows = [json.loads(line) for f in log_lines for line in f.read_text().splitlines()]
    assert any(r["delivery"] == "not_delivered" for r in rows)


def test_a_non_config_failure_does_not_fire_the_signal(
    network, shared_dir, mock_dispatch,
):
    """Isolation: an unrelated failure (timeout, bad chat) must not be
    misclassified as the config-validation refusal."""
    mock_dispatch.append((False, "openclaw message send timed out after 60s"))

    notifications = breakers_enforce._notify_user_channels(
        bots=[("team_bot_a", "team_bot_a")], action="trip",
        network=network, message="halted", dry_run=False,
    )
    breakers_enforce._record_notify_delivery(
        shared_dir=shared_dir, breaker_type="full", notifications=notifications,
    )
    assert list((shared_dir / "signals" / "firing").iterdir()) == []


def test_signal_resolves_once_a_notice_delivers_again(
    network, shared_dir, mock_dispatch,
):
    # Round 1: refused → firing Signal.
    mock_dispatch.append((False, _RECORDED_REFUSAL))
    n1 = breakers_enforce._notify_user_channels(
        bots=[("team_bot_a", "team_bot_a")], action="trip",
        network=network, message="halted", dry_run=False,
    )
    breakers_enforce._record_notify_delivery(
        shared_dir=shared_dir, breaker_type="full", notifications=n1,
    )
    assert len(list((shared_dir / "signals" / "firing").iterdir())) == 1

    # Round 2: still refused (a bare retry) → Signal must NOT resolve.
    mock_dispatch.append((False, _RECORDED_REFUSAL))
    n2 = breakers_enforce._notify_user_channels(
        bots=[("team_bot_a", "team_bot_a")], action="reset",
        network=network, message="back", dry_run=False,
    )
    breakers_enforce._record_notify_delivery(
        shared_dir=shared_dir, breaker_type="full", notifications=n2,
    )
    assert len(list((shared_dir / "signals" / "firing").iterdir())) == 1

    # Round 3: delivered (e.g. the config was fixed) → auto-resolve.
    mock_dispatch.append((True, None))
    n3 = breakers_enforce._notify_user_channels(
        bots=[("team_bot_a", "team_bot_a")], action="reset",
        network=network, message="back", dry_run=False,
    )
    breakers_enforce._record_notify_delivery(
        shared_dir=shared_dir, breaker_type="full", notifications=n3,
    )
    assert list((shared_dir / "signals" / "firing").iterdir()) == []
    archived = list((shared_dir / "signals" / "archived").iterdir())
    assert len(archived) == 1
    sig = json.loads(archived[0].read_text())
    assert sig["state"] == "resolved"
    assert sig["type"] == "notify_config_invalid"


def test_no_signals_package_does_not_break_delivery_recording(
    network, shared_dir, mock_dispatch, monkeypatch,
):
    """If the signals package can't be imported, the notify path (and its
    existing delivery-ledger forensics) still works — best-effort, per the
    same discipline openclaw_config_validator already follows."""
    import importlib as _importlib

    real_import_module = _importlib.import_module

    def _boom(name, *a, **kw):
        if name in ("signals.store", "schema.signal"):
            raise ImportError("simulated: signals package unavailable")
        return real_import_module(name, *a, **kw)

    monkeypatch.setattr(breakers_enforce.importlib, "import_module", _boom)

    mock_dispatch.append((False, _RECORDED_REFUSAL))
    notifications = breakers_enforce._notify_user_channels(
        bots=[("team_bot_a", "team_bot_a")], action="trip",
        network=network, message="halted", dry_run=False,
    )
    # Must not raise.
    breakers_enforce._record_notify_delivery(
        shared_dir=shared_dir, breaker_type="full", notifications=notifications,
    )
    log_lines = (shared_dir / "breakers" / "log").glob("*.jsonl")
    rows = [json.loads(line) for f in log_lines for line in f.read_text().splitlines()]
    assert any(r["delivery"] == "not_delivered" for r in rows)
