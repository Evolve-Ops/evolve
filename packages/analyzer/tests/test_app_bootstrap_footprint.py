"""Tests for the per-bot bootstrap-footprint measurement.

Pure function over manifests + INSTALLED_APPS.md. The chip + API
endpoint surface this verbatim, so the shape of the returned dict is
load-bearing — if you change a field name here, change the UI consumer
at index.html:_cmRenderAppFootprint at the same time.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

import app_bootstrap_footprint as fp  # noqa: E402


# ── Helpers / fixtures ──────────────────────────────────────────────────────


def _write_manifest(manifests_dir: Path, app_id: str, **overrides) -> None:
    manifests_dir.mkdir(parents=True, exist_ok=True)
    m = {
        "id": app_id,
        "name": app_id,
        "display_name": app_id.replace("-", " ").title(),
        "bot_guidance": [{"audience": "main", "text": "default guidance"}],
    }
    m.update(overrides)
    (manifests_dir / f"{app_id}.json").write_text(json.dumps(m))


def _setup_bot(tmp_path: Path, bot_id: str = "atlas") -> Path:
    """Materialize the standard bot-side workspace layout."""
    workspace = tmp_path / bot_id / ".openclaw" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _patch_bot_home(tmp_path: Path, bot_id: str = "atlas"):
    """Redirect the module's `_bot_home` to a tmp path so we don't have to
    monkey-patch the global evolve_config."""
    return patch.object(fp, "_bot_home", lambda _bid: tmp_path / _bid)


# ── _bot_guidance_bytes ─────────────────────────────────────────────────────


def test_bot_guidance_string_form_counted() -> None:
    assert fp._bot_guidance_bytes({"bot_guidance": "xxx"}) == 3


def test_bot_guidance_list_of_dicts_sums_text_field() -> None:
    bg = [{"text": "abcdef"}, {"text": "12345"}]
    assert fp._bot_guidance_bytes({"bot_guidance": bg}) == 11


def test_bot_guidance_missing_is_zero() -> None:
    assert fp._bot_guidance_bytes({}) == 0


def test_bot_guidance_unknown_shape_falls_back_to_json() -> None:
    """When a list entry isn't the {audience, text, ...} shape, the
    counter falls back to the JSON-encoded form OF THAT ENTRY (not the
    whole list) — over-count is the safe direction."""
    entry = {"different_key": "abc"}
    expected = len(json.dumps(entry).encode())
    assert fp._bot_guidance_bytes({"bot_guidance": [entry]}) == expected


# ── _split_installed_apps_md ────────────────────────────────────────────────


def test_split_installed_apps_md_basic() -> None:
    text = """# Installed Apps

## Atlas — Daily Digest
Some description.

## Task Manager — Flat
Other text.
"""
    sections = fp._split_installed_apps_md(text)
    assert "Atlas — Daily Digest" in sections
    assert "Task Manager — Flat" in sections


def test_split_installed_apps_md_empty_returns_empty() -> None:
    assert fp._split_installed_apps_md("") == {}


def test_split_installed_apps_md_no_headings_returns_empty() -> None:
    assert fp._split_installed_apps_md("just some text") == {}


# ── compute_app_bootstrap_footprint — happy path ────────────────────────────


def test_no_apps_installed_returns_zero(tmp_path: Path) -> None:
    _setup_bot(tmp_path)
    with _patch_bot_home(tmp_path):
        r = fp.compute_app_bootstrap_footprint("atlas")
    assert r["bot_id"] == "atlas"
    assert r["app_count"] == 0
    assert r["total_bytes"] == 0
    assert r["apps"] == []
    assert r["error"] is None
    # Thresholds always populated so UI can render the helper text.
    assert "per_bot_aggregate_warn_bytes" in r["thresholds"]


def test_single_app_with_bot_guidance(tmp_path: Path) -> None:
    workspace = _setup_bot(tmp_path)
    _write_manifest(
        workspace / "manifests", "test-app",
        bot_guidance=[{"text": "abcdef"}],
    )
    with _patch_bot_home(tmp_path):
        r = fp.compute_app_bootstrap_footprint("atlas")
    assert r["app_count"] == 1
    assert r["apps"][0]["id"] == "test-app"
    assert r["apps"][0]["bot_guidance_bytes"] == 6
    assert r["total_bytes"] >= 6


def test_multiple_apps_sorted_by_size(tmp_path: Path) -> None:
    workspace = _setup_bot(tmp_path)
    _write_manifest(
        workspace / "manifests", "small-app",
        bot_guidance=[{"text": "x" * 50}],
    )
    _write_manifest(
        workspace / "manifests", "big-app",
        bot_guidance=[{"text": "x" * 500}],
    )
    with _patch_bot_home(tmp_path):
        r = fp.compute_app_bootstrap_footprint("atlas")
    assert r["app_count"] == 2
    # Largest first — UI scans top-down.
    assert r["apps"][0]["id"] == "big-app"
    assert r["apps"][1]["id"] == "small-app"


def test_bot_level_verdict_warn_at_aggregate(tmp_path: Path) -> None:
    workspace = _setup_bot(tmp_path)
    # Push aggregate past the warn threshold (10 KB) but under the alert
    # threshold (25 KB) — 15 apps × ~1000 bytes guidance each.
    for i in range(15):
        _write_manifest(
            workspace / "manifests", f"app-{i}",
            bot_guidance=[{"text": "x" * 800}],
        )
    with _patch_bot_home(tmp_path):
        r = fp.compute_app_bootstrap_footprint("atlas")
    assert r["total_bytes"] > fp.THRESHOLD_PER_BOT_AGGREGATE_BYTES
    assert r["verdict"] in ("warn", "alert")


def test_per_app_verdict_info_at_bot_guidance_threshold(tmp_path: Path) -> None:
    workspace = _setup_bot(tmp_path)
    _write_manifest(
        workspace / "manifests", "oversized",
        bot_guidance=[{"text": "x" * (fp.THRESHOLD_BOT_GUIDANCE_BYTES + 200)}],
    )
    with _patch_bot_home(tmp_path):
        r = fp.compute_app_bootstrap_footprint("atlas")
    app = r["apps"][0]
    assert app["bot_guidance_bytes"] > fp.THRESHOLD_BOT_GUIDANCE_BYTES
    assert app["verdict"] in ("info", "warn")


def test_unreadable_manifests_dir_returns_error(tmp_path: Path) -> None:
    """No bot home, no manifests dir — result still returns successfully
    so the chip can render 'no data' rather than disappearing."""
    with _patch_bot_home(tmp_path):
        # 'missing-bot' has no workspace materialized.
        r = fp.compute_app_bootstrap_footprint("missing-bot")
    # Either empty or error-flagged — both are acceptable; UI handles both.
    assert r["bot_id"] == "missing-bot"
    assert r["app_count"] == 0


# ── transport_kind / transport_hint ─────────────────────────────────────────
#
# The per-app row carries the transport-choice classification so the chip
# can surface the same hint as app_audit_structural's calibration-family
# checks — inline next to the bytes that the hint would save, instead of
# in an info-severity Signal the operator has to toggle visible.


def test_transport_kind_heartbeat() -> None:
    m = {"heartbeat_evidence": {"section_anchors": [{"label": "x"}]}}
    assert fp._transport_kind(m) == "heartbeat"


def test_transport_kind_subagent_wins_over_cron_when_both_declared() -> None:
    # invocation_mode is the most specific declaration when present.
    m = {"invocation_mode": "subagent", "crons": [{"id": "x"}]}
    assert fp._transport_kind(m) == "subagent"


def test_transport_kind_cron() -> None:
    m = {"crons": [{"id": "x", "schedule": "0 9 * * *"}]}
    assert fp._transport_kind(m) == "cron"


def test_transport_kind_scheduled_actions_is_cron_shaped() -> None:
    m = {"scheduled_actions": [{"id": "morning"}]}
    assert fp._transport_kind(m) == "cron"


def test_transport_kind_unknown_when_no_producer_declared() -> None:
    assert fp._transport_kind({}) == "unknown"


def test_transport_hint_could_be_cron_when_heartbeat_with_no_llm_intent() -> None:
    m = {"heartbeat_evidence": {"section_anchors": [{"label": "x"}]}}
    assert fp._transport_hint(m) == "could_be_cron"


def test_transport_hint_none_when_heartbeat_has_llm_intent() -> None:
    m = {
        "heartbeat_evidence": {"section_anchors": [{"label": "x"}]},
        "recursive_llm": {"purposes": [{"id": "classify"}]},
    }
    assert fp._transport_hint(m) is None


def test_transport_hint_could_be_subagent_when_user_routed_cli_with_llm_intent() -> None:
    m = {
        "recursive_llm": {"purposes": [{"id": "answer"}]},
        "interface_contract": {"cli": [{"command": "atlas:run"}]},
        "invocation_mode": "main",
        "usage": {"model": "user-initiated"},
    }
    assert fp._transport_hint(m) == "could_be_subagent"


def test_transport_hint_none_when_invocation_mode_is_subagent() -> None:
    m = {
        "recursive_llm": {"purposes": [{"id": "answer"}]},
        "interface_contract": {"cli": [{"command": "atlas:run"}]},
        "invocation_mode": "subagent",
        "usage": {"model": "user-initiated"},
    }
    assert fp._transport_hint(m) is None


def test_transport_hint_none_for_scheduled_usage_model() -> None:
    # Scheduled apps don't get user-initiated CLI invocations, so the
    # subagent hint doesn't apply even with CLI + LLM intent declared.
    m = {
        "recursive_llm": {"purposes": [{"id": "answer"}]},
        "interface_contract": {"cli": [{"command": "atlas:run"}]},
        "invocation_mode": "main",
        "usage": {"model": "scheduled"},
    }
    assert fp._transport_hint(m) is None


def test_transport_hint_handles_identity_nested_usage_block() -> None:
    # Some manifests nest usage under identity; the helper must tolerate
    # both placements (mirrors app_registry._usage_block).
    m = {
        "recursive_llm": {"purposes": [{"id": "answer"}]},
        "interface_contract": {"cli": [{"command": "atlas:run"}]},
        "invocation_mode": "main",
        "identity": {"usage": {"model": "user-initiated"}},
    }
    assert fp._transport_hint(m) == "could_be_subagent"


def test_compute_includes_transport_fields_per_app(tmp_path: Path) -> None:
    workspace = _setup_bot(tmp_path)
    _write_manifest(
        workspace / "manifests", "cron-eligible-app",
        heartbeat_evidence={"section_anchors": [{"label": "x"}]},
    )
    _write_manifest(
        workspace / "manifests", "honest-cron",
        crons=[{"id": "x", "schedule": "0 9 * * *"}],
    )
    with _patch_bot_home(tmp_path):
        r = fp.compute_app_bootstrap_footprint("atlas")
    by_id = {a["id"]: a for a in r["apps"]}
    assert by_id["cron-eligible-app"]["transport_kind"] == "heartbeat"
    assert by_id["cron-eligible-app"]["transport_hint"] == "could_be_cron"
    assert by_id["honest-cron"]["transport_kind"] == "cron"
    assert by_id["honest-cron"]["transport_hint"] is None
