"""tests/test_alerts_grace.py — per-severity delivery grace substrate (L3).

Spec: internal/spec-delta-transient-delivery-grace-2026-06-26.md.

Pins the shared grace policy that both signal_notifier and digest_dispatcher
consult so "within grace" means the same thing on the real-time and digest
paths:

  - defaults (alert 0, warn/info 900s) with a partial-override merge
  - the alert clamp (no config can delay a critical) — the invariant
  - within_grace boolean edges (strict <, alert always False)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))


def _shared(tmp_path, overrides=None):
    shared = tmp_path / "evolve"
    shared.mkdir()
    if overrides is not None:
        (shared / "better-engine-config.json").write_text(
            json.dumps({"pod_defaults": {"alerts": overrides}})
        )
    return shared


def test_defaults_when_no_config(tmp_path):
    from evolve_admin.alerts import grace
    shared = _shared(tmp_path)
    table = grace.read_grace_seconds_by_severity(shared)
    assert table == {"alert": 0, "warn": 900, "info": 900}


def test_partial_override_keeps_other_defaults(tmp_path):
    from evolve_admin.alerts import grace
    shared = _shared(tmp_path, {"grace_seconds_by_severity": {"warn": 300}})
    table = grace.read_grace_seconds_by_severity(shared)
    assert table["warn"] == 300        # overridden
    assert table["info"] == 900        # default preserved
    assert table["alert"] == 0


def test_alert_clamp_overrides_any_config(tmp_path):
    """The invariant: no operator value can delay a critical."""
    from evolve_admin.alerts import grace
    shared = _shared(tmp_path, {"grace_seconds_by_severity": {"alert": 99999}})
    table = grace.read_grace_seconds_by_severity(shared)
    assert table["alert"] == 0
    assert grace.grace_for_severity(shared, "alert") == 0
    assert grace.within_grace(shared, "alert", 0.0) is False
    assert grace.within_grace(shared, "alert", 100000.0) is False


def test_invalid_values_fall_back_to_default(tmp_path):
    from evolve_admin.alerts import grace
    shared = _shared(tmp_path, {"grace_seconds_by_severity": {
        "warn": "lots", "info": -5,
    }})
    table = grace.read_grace_seconds_by_severity(shared)
    assert table["warn"] == 900   # non-int → default
    assert table["info"] == 900   # negative → default


def test_within_grace_edges(tmp_path):
    from evolve_admin.alerts import grace
    shared = _shared(tmp_path)
    # warn grace = 900: strictly inside is within; at/over is not.
    assert grace.within_grace(shared, "warn", 899.0) is True
    assert grace.within_grace(shared, "warn", 900.0) is False
    assert grace.within_grace(shared, "warn", 901.0) is False


def test_unknown_severity_uses_warn_shape_not_clamp(tmp_path):
    """An unrecognized severity is graced like a warn (never zeroed like a
    known alert) — we only clamp a KNOWN alert."""
    from evolve_admin.alerts import grace
    shared = _shared(tmp_path)
    assert grace.grace_for_severity(shared, "weird") == 900
    assert grace.within_grace(shared, "weird", 100.0) is True
