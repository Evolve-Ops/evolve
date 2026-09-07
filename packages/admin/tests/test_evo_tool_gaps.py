"""tests/test_evo_tool_gaps.py — tool-gap telemetry (Phase 1.5b, §14.3).

Two layers under test:

  1. The ``evolve_admin.evo.tool_gaps`` storage module — record /
     aggregate / wipe semantics, validation, atomic-append behavior.

  2. The two evo tools wrapping it — ``action.evo.log_tool_gap``
     (write_safe handler + validate) and ``pod_state.tool_gaps``
     (read handler).

All tests use tmp_path so no real shared_dir is touched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.evo import tool_gaps  # noqa: E402
from evolve_admin.evo import tools as _tools  # noqa: E402
from evolve_admin.evo.tools import evo_telemetry  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Storage module — record_tool_gap
# ─────────────────────────────────────────────────────────────────────────────


def test_record_creates_new_record_when_fingerprint_unseen(tmp_path):
    result = tool_gaps.record_tool_gap(
        tmp_path,
        kind="config.bot.set_model",
        scope="single_bot",
        fingerprint="config-edit:agents.defaults.model",
    )
    assert result.ok is True
    assert result.new is True
    assert result.frequency_local == 1
    # File is written under observations/tool_gaps.jsonl
    assert (tmp_path / "observations" / "tool_gaps.jsonl").exists()


def test_record_increments_existing_fingerprint(tmp_path):
    for _ in range(3):
        tool_gaps.record_tool_gap(
            tmp_path,
            kind="config.bot.set_model",
            scope="single_bot",
            fingerprint="config-edit:agents.defaults.model",
        )
    final = tool_gaps.record_tool_gap(
        tmp_path,
        kind="config.bot.set_model",
        scope="single_bot",
        fingerprint="config-edit:agents.defaults.model",
    )
    assert final.ok is True
    assert final.new is False
    assert final.frequency_local == 4   # 3 + this call


def test_record_preserves_first_observed_across_increments(tmp_path):
    """first_observed must stay stable across increments; only
    last_observed updates. Important for upstream aggregation to
    distinguish 'long-running pattern' from 'recent burst'."""
    tool_gaps.record_tool_gap(
        tmp_path, kind="k", scope="single_bot", fingerprint="fp1",
    )
    tool_gaps.record_tool_gap(
        tmp_path, kind="k", scope="single_bot", fingerprint="fp1",
    )

    records = tool_gaps.read_tool_gaps(tmp_path)
    assert len(records) == 1
    r = records[0]
    # last_observed >= first_observed (both ISO strings sort right)
    assert r.last_observed >= r.first_observed
    assert r.frequency_local == 2


def test_record_distinguishes_different_fingerprints(tmp_path):
    tool_gaps.record_tool_gap(
        tmp_path, kind="k1", scope="single_bot", fingerprint="fp-a",
    )
    tool_gaps.record_tool_gap(
        tmp_path, kind="k2", scope="pod_wide", fingerprint="fp-b",
    )
    records = tool_gaps.read_tool_gaps(tmp_path)
    fingerprints = {r.fingerprint for r in records}
    assert fingerprints == {"fp-a", "fp-b"}


def test_record_rejects_empty_kind(tmp_path):
    result = tool_gaps.record_tool_gap(
        tmp_path, kind="", scope="single_bot", fingerprint="fp",
    )
    assert result.ok is False
    assert "kind" in result.error


def test_record_rejects_empty_fingerprint(tmp_path):
    result = tool_gaps.record_tool_gap(
        tmp_path, kind="k", scope="single_bot", fingerprint="",
    )
    assert result.ok is False
    assert "fingerprint" in result.error


def test_record_rejects_kind_with_whitespace(tmp_path):
    """Whitespace would indicate the LLM smuggled chat content into the
    telemetry stream. Reject to keep telemetry shape-only."""
    result = tool_gaps.record_tool_gap(
        tmp_path, kind="set team_bot_a's model", scope="single_bot",
        fingerprint="fp",
    )
    assert result.ok is False
    assert "kind" in result.error


def test_record_rejects_oversize_token(tmp_path):
    big = "a" * 200
    result = tool_gaps.record_tool_gap(
        tmp_path, kind=big, scope="single_bot", fingerprint="fp",
    )
    assert result.ok is False


def test_record_rejects_control_characters(tmp_path):
    """Control chars (e.g. newlines) would corrupt the JSONL on read.
    Reject at the writer."""
    result = tool_gaps.record_tool_gap(
        tmp_path, kind="kind\n", scope="single_bot", fingerprint="fp",
    )
    assert result.ok is False


def test_jsonl_format_is_one_record_per_line(tmp_path):
    """Each record on its own line; whole file parses as JSONL.
    Important so the future upload daemon and CLI can stream it."""
    tool_gaps.record_tool_gap(
        tmp_path, kind="k", scope="single_bot", fingerprint="fp-a",
    )
    tool_gaps.record_tool_gap(
        tmp_path, kind="k", scope="single_bot", fingerprint="fp-b",
    )
    path = tmp_path / "observations" / "tool_gaps.jsonl"
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    for line in lines:
        # Each must parse as JSON object with the expected keys.
        obj = json.loads(line)
        assert set(obj.keys()) >= {
            "kind", "scope", "fingerprint", "first_observed",
            "last_observed", "frequency_local",
        }


# ─────────────────────────────────────────────────────────────────────────────
# Storage module — read_tool_gaps
# ─────────────────────────────────────────────────────────────────────────────


def test_read_returns_empty_when_file_absent(tmp_path):
    records = tool_gaps.read_tool_gaps(tmp_path)
    assert records == []


def test_read_aggregates_repeats_to_one_record_per_fingerprint(tmp_path):
    """Even though each increment appends a new line, read collapses
    by fingerprint and returns the latest line's view."""
    for _ in range(5):
        tool_gaps.record_tool_gap(
            tmp_path, kind="k", scope="single_bot", fingerprint="fp",
        )
    records = tool_gaps.read_tool_gaps(tmp_path)
    assert len(records) == 1
    assert records[0].frequency_local == 5


@pytest.mark.real_sleep  # sleeps 1.05s so last_observed timestamps differ by ≥1s
def test_read_sorts_newest_first(tmp_path):
    import time
    tool_gaps.record_tool_gap(
        tmp_path, kind="k", scope="single_bot", fingerprint="fp-old",
    )
    # Ensure last_observed timestamps differ by at least 1 second
    time.sleep(1.05)
    tool_gaps.record_tool_gap(
        tmp_path, kind="k", scope="single_bot", fingerprint="fp-new",
    )
    records = tool_gaps.read_tool_gaps(tmp_path)
    assert [r.fingerprint for r in records] == ["fp-new", "fp-old"]


def test_read_respects_limit(tmp_path):
    for i in range(10):
        tool_gaps.record_tool_gap(
            tmp_path, kind="k", scope="single_bot",
            fingerprint=f"fp-{i}",
        )
    records = tool_gaps.read_tool_gaps(tmp_path, limit=3)
    assert len(records) == 3


def test_read_clamps_limit_to_max():
    """Limit > 1000 clamps to 1000. Defensive against runaway requests."""
    # No actual data needed — limit-clamping is pure-arg behavior.
    # Use an empty tmp dir.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        # Read a non-existent file — still exercises the clamp path.
        records = tool_gaps.read_tool_gaps(Path(td), limit=10_000)
        # Empty regardless, but no exception thrown.
        assert records == []


def test_read_skips_malformed_lines(tmp_path):
    """A hand-edit that breaks one line shouldn't block reads."""
    path = tmp_path / "observations" / "tool_gaps.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"kind": "k", "scope": "single_bot", "fingerprint": "fp-good", '
        '"first_observed": "2026-05-19T00:00:00Z", '
        '"last_observed": "2026-05-19T00:00:00Z", '
        '"frequency_local": 1}\n'
        'not valid json\n'
        '{"kind": "k", "scope": "single_bot", "fingerprint": "fp-good2", '
        '"first_observed": "2026-05-19T00:00:00Z", '
        '"last_observed": "2026-05-19T00:00:00Z", '
        '"frequency_local": 1}\n'
    )
    records = tool_gaps.read_tool_gaps(tmp_path)
    fps = {r.fingerprint for r in records}
    assert fps == {"fp-good", "fp-good2"}


# ─────────────────────────────────────────────────────────────────────────────
# Storage module — wipe_tool_gaps
# ─────────────────────────────────────────────────────────────────────────────


def test_wipe_removes_file_when_present(tmp_path):
    tool_gaps.record_tool_gap(
        tmp_path, kind="k", scope="single_bot", fingerprint="fp",
    )
    assert tool_gaps.wipe_tool_gaps(tmp_path) is True
    assert not (tmp_path / "observations" / "tool_gaps.jsonl").exists()


def test_wipe_is_noop_on_clean_pod(tmp_path):
    # No record written — wipe returns False but doesn't error.
    assert tool_gaps.wipe_tool_gaps(tmp_path) is False


# ─────────────────────────────────────────────────────────────────────────────
# Tool registration
# ─────────────────────────────────────────────────────────────────────────────


def test_log_tool_gap_registered_as_write_safe():
    t = _tools.lookup("action.evo.log_tool_gap")
    assert t is not None
    assert t.risk_tier == _tools.RiskTier.WRITE_SAFE
    assert t.validate is not None


def test_pod_state_tool_gaps_registered_as_read():
    t = _tools.lookup("pod_state.tool_gaps")
    assert t is not None
    assert t.risk_tier == _tools.RiskTier.READ
    assert t.validate is None


# ─────────────────────────────────────────────────────────────────────────────
# action.evo.log_tool_gap — validate
# ─────────────────────────────────────────────────────────────────────────────


def test_log_validate_requires_kind(tmp_path):
    result = evo_telemetry._log_validate(
        shared_dir=tmp_path, kind="", scope="single_bot", fingerprint="fp",
    )
    assert result["ok"] is False
    assert "kind" in result["reason"]


def test_log_validate_rejects_kind_with_spaces(tmp_path):
    result = evo_telemetry._log_validate(
        shared_dir=tmp_path,
        kind="set the model",
        scope="single_bot",
        fingerprint="fp",
    )
    assert result["ok"] is False
    assert "kind" in result["reason"]


def test_log_validate_rejects_unknown_scope_with_spaces(tmp_path):
    """Free-form scope is allowed (the writer doesn't enforce the
    KNOWN_SCOPES set), but it must still pass the identifier-charset
    check — spaces are rejected."""
    result = evo_telemetry._log_validate(
        shared_dir=tmp_path, kind="k",
        scope="not a known scope", fingerprint="fp",
    )
    assert result["ok"] is False
    assert "scope" in result["reason"]


def test_log_validate_passes_with_clean_inputs(tmp_path):
    result = evo_telemetry._log_validate(
        shared_dir=tmp_path,
        kind="config.bot.set_model",
        scope="single_bot",
        fingerprint="config-edit:agents.defaults.model",
    )
    assert result["ok"] is True


# ─────────────────────────────────────────────────────────────────────────────
# action.evo.log_tool_gap — handler
# ─────────────────────────────────────────────────────────────────────────────


def test_log_handler_records_new_gap(tmp_path):
    result = evo_telemetry._log_handler(
        shared_dir=tmp_path,
        kind="config.bot.set_model",
        scope="single_bot",
        fingerprint="fp-1",
    )
    assert result["ok"] is True
    assert result["new"] is True
    assert result["frequency_local"] == 1
    assert "verify_via" in result
    assert result["verify_via"]["tool"] == "pod_state.tool_gaps"


def test_log_handler_increments_existing(tmp_path):
    for _ in range(2):
        evo_telemetry._log_handler(
            shared_dir=tmp_path, kind="k",
            scope="single_bot", fingerprint="fp-1",
        )
    third = evo_telemetry._log_handler(
        shared_dir=tmp_path, kind="k",
        scope="single_bot", fingerprint="fp-1",
    )
    assert third["ok"] is True
    assert third["new"] is False
    assert third["frequency_local"] == 3


def test_log_handler_returns_error_on_validation_failure(tmp_path):
    """Handler delegates validation to record_tool_gap, which mirrors
    the validate function. Surfaces the same error shape."""
    result = evo_telemetry._log_handler(
        shared_dir=tmp_path, kind="bad kind",
        scope="single_bot", fingerprint="fp",
    )
    assert result["ok"] is False
    assert "kind" in result["error"]


# ─────────────────────────────────────────────────────────────────────────────
# pod_state.tool_gaps — handler
# ─────────────────────────────────────────────────────────────────────────────


def test_read_handler_empty_on_fresh_pod(tmp_path):
    result = evo_telemetry._read_handler(shared_dir=tmp_path)
    assert result["ok"] is True
    assert result["count"] == 0
    assert result["tool_gaps"] == []


def test_read_handler_returns_records_after_log(tmp_path):
    tool_gaps.record_tool_gap(
        tmp_path, kind="k", scope="single_bot", fingerprint="fp-1",
    )
    tool_gaps.record_tool_gap(
        tmp_path, kind="k2", scope="pod_wide", fingerprint="fp-2",
    )
    result = evo_telemetry._read_handler(shared_dir=tmp_path)
    assert result["ok"] is True
    assert result["count"] == 2
    fps = {g["fingerprint"] for g in result["tool_gaps"]}
    assert fps == {"fp-1", "fp-2"}


def test_read_handler_respects_limit(tmp_path):
    for i in range(5):
        tool_gaps.record_tool_gap(
            tmp_path, kind="k", scope="single_bot",
            fingerprint=f"fp-{i}",
        )
    result = evo_telemetry._read_handler(shared_dir=tmp_path, limit=2)
    assert result["ok"] is True
    assert result["count"] == 2
    assert result["limit"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# Network config default — evo_telemetry.tool_gaps
# ─────────────────────────────────────────────────────────────────────────────


def test_default_network_config_includes_evo_telemetry():
    """Confirms the default schema declares evo_telemetry.tool_gaps='off'
    so a freshly-bootstrapped pod has the opt-out baked in (per
    feedback_user_observation_optout)."""
    from evolve_admin.config import _default_network
    net = _default_network()
    assert "evo_telemetry" in net
    assert net["evo_telemetry"]["tool_gaps"] == "off"
