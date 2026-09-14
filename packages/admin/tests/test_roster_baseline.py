"""Unit tests for evolve_admin.roster_baseline — the expected-state store for
group-allowlist drift detection (R1a PR3).

Covers the snapshot/normalize/round-trip contract and the two fail-safe
guarantees the drift monitor relies on:

  * ``read_current_allowlists`` returns the ``UNREADABLE`` sentinel (not ``{}``)
    when openclaw.json can't be read — so a blind monitor tick never reads as a
    clean/empty allowlist.
  * The admin-write snapshot (``snapshot_from_config``) is byte-identical to a
    fresh live read of the same config through ``read_current_allowlists`` — so
    an admin approve/revoke can never false-positive as drift.

Placeholder-only data per docs/PLACEHOLDER_NAMING.md: bot ``team_bot_a``, opaque
Slack ids like ``U0AAAAAAAA``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin import roster_baseline as rb  # noqa: E402

_BOT = "team_bot_a"


def _cfg(**channels) -> dict:
    return {"channels": channels}


def _allowlist(*ids: str) -> dict:
    return {"groupPolicy": "allowlist", "allowFrom": list(ids)}


# ── normalize ──────────────────────────────────────────────────────────────


def test_normalize_sorts_and_drops_empty():
    got = rb._normalize({"slack": ["U0B", "U0A", "U0A"], "telegram": []})
    assert got == {"slack": ["U0A", "U0B"]}  # deduped, sorted, empty dropped


# ── write/read round-trip ───────────────────────────────────────────────────


def test_write_then_read_round_trip(tmp_path):
    rb.write_baseline(tmp_path, _BOT, {"slack": ["U0B", "U0A"]}, source="admin_write")
    assert rb.read_baseline(tmp_path, _BOT) == {"slack": ["U0A", "U0B"]}
    # Provenance + shape persisted.
    raw = json.loads(rb.baseline_path(tmp_path, _BOT).read_text())
    assert raw["source"] == "admin_write"
    assert raw["bot_id"] == _BOT


def test_read_missing_baseline_is_none(tmp_path):
    assert rb.read_baseline(tmp_path, _BOT) is None


def test_read_corrupt_baseline_is_none(tmp_path):
    p = rb.baseline_path(tmp_path, _BOT)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json")
    assert rb.read_baseline(tmp_path, _BOT) is None


def test_write_is_atomic_no_tmp_left_behind(tmp_path):
    rb.write_baseline(tmp_path, _BOT, {"slack": ["U0A"]}, source="monitor_seed")
    leftover = list(rb.baselines_dir(tmp_path).glob(".*tmp"))
    assert leftover == []


# ── snapshot_from_config ────────────────────────────────────────────────────


def test_snapshot_from_config_reads_effective_list():
    cfg = _cfg(slack=_allowlist("U0B", "U0A"))
    snap = rb.snapshot_from_config({}, _BOT, cfg)
    assert snap == {"slack": ["U0A", "U0B"]}


def test_snapshot_omits_non_allowlist_channels():
    cfg = _cfg(
        slack=_allowlist("U0A"),
        telegram={"groupPolicy": "open"},          # not a managed list
        discord={"groupPolicy": "allowlist", "allowFrom": []},  # gated but empty
    )
    snap = rb.snapshot_from_config({}, _BOT, cfg)
    assert snap == {"slack": ["U0A"]}


# ── read_current_allowlists — fail-safe sentinel ────────────────────────────


def test_read_current_unreadable_returns_sentinel():
    # oc_reader returning None models an unreadable openclaw.json (missing /
    # EACCES even via the sudo-cat fallback).
    got = rb.read_current_allowlists({}, _BOT, oc_reader=lambda: None)
    assert got is rb.UNREADABLE


def test_read_current_empty_config_is_empty_dict_not_sentinel():
    got = rb.read_current_allowlists({}, _BOT, oc_reader=lambda: {})
    assert got == {}
    assert got is not rb.UNREADABLE


def test_read_current_returns_normalized_snapshot():
    cfg = _cfg(slack=_allowlist("U0B", "U0A"))
    got = rb.read_current_allowlists({}, _BOT, oc_reader=lambda: cfg)
    assert got == {"slack": ["U0A", "U0B"]}


# ── symmetry: admin-write snapshot == live read (no false positives) ────────


def test_snapshot_and_live_read_are_identical():
    """The admin write path records via snapshot_from_config; the monitor reads
    via read_current_allowlists. Both must run the same seam over the same
    config so a legitimate admin approve/revoke never reads as drift."""
    cfg = _cfg(
        slack=_allowlist("U0A", "U0C"),
        telegram={"groupPolicy": "allowlist", "groupAllowFrom": ["U0Z"]},
    )
    snap = rb.snapshot_from_config({}, _BOT, cfg)
    live = rb.read_current_allowlists({}, _BOT, oc_reader=lambda: cfg)
    assert snap == live


# ── record_baseline_from_config — derives shared_dir ────────────────────────


def test_record_baseline_from_config_writes_admin_write_source(tmp_path):
    network = {"sharedDir": str(tmp_path)}
    cfg = _cfg(slack=_allowlist("U0A"))
    rb.record_baseline_from_config(network, _BOT, cfg)
    assert rb.read_baseline(tmp_path, _BOT) == {"slack": ["U0A"]}
    raw = json.loads(rb.baseline_path(tmp_path, _BOT).read_text())
    assert raw["source"] == "admin_write"
