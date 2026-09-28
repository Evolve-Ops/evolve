"""Tests for secret_redaction — the file-level wrapper around
``generators.security_warden.baseline.redacted_snapshot`` used by
backup.py and heal.py.

The JSON-walker semantics (which keys redact, regex patterns, hash
markers, idempotence) are covered in ``test_security_warden_baseline.py``.
This file tests:

  1. The ``redact_file`` file-roundtrip contract.
  2. The drift-comparison invariant that heal.py relies on: applying
     ``redact_json`` to both sides before equality-diffing means
     "identical secrets" → "no drift" and "rotated secret" → "drift".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from secret_redaction import (
    is_redacted,
    redact_file,
    redact_json,
)


# ── redact_file file-roundtrip ────────────────────────────────────────────────


def test_redact_file_rewrites_when_secrets_present(tmp_path: Path):
    p = tmp_path / "openclaw.json"
    p.write_text(json.dumps({
        "transports": {"telegram": {"botToken": "real-live-token-abcdefghijk"}},
        "meta": {"lastTouchedVersion": "1.0"},
    }))
    changed, reason = redact_file(p)
    assert changed and reason == "rewritten"
    reloaded = json.loads(p.read_text())
    assert is_redacted(reloaded["transports"]["telegram"]["botToken"])
    assert reloaded["meta"] == {"lastTouchedVersion": "1.0"}


def test_redact_file_noop_on_clean_input(tmp_path: Path):
    p = tmp_path / "clean.json"
    contents = {"version": "1.0", "name": "bot"}
    p.write_text(json.dumps(contents))
    mtime_before = p.stat().st_mtime_ns
    changed, reason = redact_file(p)
    assert not changed and reason == "no-secrets"
    assert p.stat().st_mtime_ns == mtime_before  # not touched


def test_redact_file_idempotent_on_already_redacted(tmp_path: Path):
    p = tmp_path / "already.json"
    p.write_text(json.dumps({
        "transports": {"telegram": {"botToken": "<REDACTED:sha256:abcd1234>"}},
    }))
    changed, reason = redact_file(p)
    assert not changed and reason == "no-secrets"


def test_redact_file_handles_invalid_json(tmp_path: Path):
    p = tmp_path / "broken.json"
    p.write_text("{not valid")
    changed, reason = redact_file(p)
    assert not changed
    assert "not JSON" in reason


def test_redact_file_handles_missing(tmp_path: Path):
    p = tmp_path / "nope.json"
    changed, reason = redact_file(p)
    assert not changed
    assert "read failed" in reason


# ── Heal.py drift-comparison invariant ────────────────────────────────────────


def test_redacted_snapshot_equals_redacted_live_when_secrets_match():
    """heal.py applies redact_json to both sides; identical underlying
    secrets must yield identical structures → no spurious drift."""
    snapshot = redact_json({
        "transports": {
            "telegram": {"botToken": "T-1"},
            "slack": {"botToken": "xoxb-live-2"},
        },
        "version": "1.0",
    })
    live = redact_json({
        "transports": {
            "telegram": {"botToken": "T-1"},
            "slack": {"botToken": "xoxb-live-2"},
        },
        "version": "1.0",
    })
    assert snapshot == live


def test_redacted_snapshot_differs_after_rotation():
    """After a real rotation, redacted views differ → heal.py fires."""
    snapshot = redact_json({"transports": {"telegram": {"botToken": "T-old"}}})
    live = redact_json({"transports": {"telegram": {"botToken": "T-NEW"}}})
    assert snapshot != live


def test_historical_unredacted_backup_shows_drift_against_redacted_live():
    """Pre-fix backups stored cleartext; post-fix live side gets markers.
    The two differ → drift is reported, which "accept as baseline" resolves
    by writing a new redacted snapshot. This confirms we don't silently
    suppress real drift from pre-existing plain-text backups."""
    old_backup_raw = {"transports": {"telegram": {"botToken": "T-original"}}}
    live_with_same_token = {"transports": {"telegram": {"botToken": "T-original"}}}

    committed = old_backup_raw  # pre-fix: not redacted
    live_redacted = redact_json(live_with_same_token)

    # They look different because one has cleartext, the other has a marker.
    assert committed != live_redacted
    # The marker IS is_redacted; the cleartext isn't.
    assert is_redacted(live_redacted["transports"]["telegram"]["botToken"])
    assert not is_redacted(committed["transports"]["telegram"]["botToken"])
