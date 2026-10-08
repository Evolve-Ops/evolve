"""tests/test_bot_log_signal_catalog.py — coverage tests for the
bot_log_monitor pattern catalog: the two recurring shapes added in this
PR (provider_override_unauthorized, unknown_provider_model) plus the
uncategorized-errors catch-all.

Background. The 2026-06-04 review of Maintenance → Status surfaced two
bots logging recurring errors that bot_log_signal didn't recognize:

  - bot A: "provider/model override is not authorized for this plugin
    subagent run." — plugin subagent permission shape.
  - bot B: "Unknown model: anthropic/claude-haiku-4-5 ... no matching
    models.providers[\"anthropic\"].models[] entry. Add ..." — the
    documented provider-registry incident class (2026-06-03).

The Status page rendered both because it dumps recent_errors[] verbatim,
but no Signal fired and the Alerts page was blind. These tests pin:

  1. Both new patterns match the live error shapes.
  2. The uncategorized catch-all fires when ≥2 lines have no pattern.
  3. The catch-all stays quiet when all lines matched a specific pattern
     (otherwise we'd double-report).
  4. Adding a future specific pattern naturally retires the catch-all
     for that shape — proved via the matched-indices flow.

Bot id placeholder follows docs/PLACEHOLDER_NAMING.md.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import bot_log_signal as bls  # noqa: E402
from signals import store as signals_store  # noqa: E402


def _write_status(shared_dir: Path, bot_id: str, recent_errors: list[str]) -> None:
    p = shared_dir / "status" / f"{bot_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"bot_id": bot_id, "recent_errors": recent_errors}))


def _firing(shared_dir: Path, signal_type: str | None = None) -> list:
    sigs = list(signals_store.iter_active(shared_dir, producer="bot_log_monitor"))
    if signal_type is None:
        return sigs
    return [s for s in sigs if s.type == signal_type]


# ── Specific pattern: provider_override_unauthorized ────────────────────────


def test_provider_override_unauthorized_fires_on_live_error_text():
    """Reproduces the live error shape from the 2026-06-04 Maintenance →
    Status review (plugin-subagent permission failure)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        _write_status(td, "team_bot_a", [
            "[6h ago] 2026-06-04T04:32:56.188-07:00 [plugins] Evolve: LLM "
            "tier classification failed, using keyword fallback: Error: "
            "provider/model override is not authorized for this plugin "
            "subagent run.",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])

        sigs = _firing(td, "provider_override_unauthorized")
        assert len(sigs) == 1, (
            "provider_override_unauthorized pattern did not fire on the "
            "live error shape — the needle 'provider/model override is "
            "not authorized' must match"
        )
        sig = sigs[0]
        assert sig.bot_id == "team_bot_a"
        assert "plugin subagent blocked" in sig.title
        assert sig.details["vector"] == "operations"
        assert sig.details["magnitude"] == 2
        assert "what_it_means" in sig.details
        assert "fix_steps" in sig.details


def test_provider_override_unauthorized_dedups_within_run():
    """Two matching lines from the same bot produce one signal — the
    LLM tier classification AND outcome extraction failures both hit
    this pattern in the live data."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        _write_status(td, "team_bot_a", [
            "[plugins] Evolve: LLM tier classification failed, using "
            "keyword fallback: Error: provider/model override is not "
            "authorized for this plugin subagent run.",
            "[plugins] Evolve: LLM outcome extraction failed, using "
            "fallback: Error: provider/model override is not authorized "
            "for this plugin subagent run.",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])
        sigs = _firing(td, "provider_override_unauthorized")
        assert len(sigs) == 1, (
            "two matching lines must dedup to one signal — same bot, "
            "same type"
        )


# ── Specific pattern: unknown_provider_model ────────────────────────────────


def test_unknown_provider_model_fires_on_live_error_text():
    """Reproduces the live FailoverError shape — the 2026-06-03 provider
    registry incident class."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        _write_status(td, "team_bot_a", [
            "[26m ago] [diagnostic] lane task error: "
            "error=\"FailoverError: Unknown model: "
            "anthropic/claude-haiku-4-5. Found "
            "agents.defaults.models[\"anthropic/claude-haiku-4-5\"], "
            "but no matching models.providers[\"anthropic\"].models[] "
            "entry. Add { \"id\": \"claude-haiku-4-5\" } to "
            "models.providers[\"anthropic\"].models[] to register "
            "this provider model.\"",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])

        sigs = _firing(td, "unknown_provider_model")
        assert len(sigs) == 1, (
            "unknown_provider_model pattern did not fire — both needles "
            "('Unknown model:' AND 'models.providers') must match"
        )
        sig = sigs[0]
        assert sig.bot_id == "team_bot_a"
        # magnitude=3 because every request through this path errors
        # (structural, not transient).
        assert sig.details["vector"] == "operations"
        assert sig.details["magnitude"] == 3
        # Operator docs link this to the 2026-06-03 incident class.
        assert "provider-registry" in sig.details["what_it_means"].lower()


def test_unknown_provider_model_needs_both_needles():
    """Half the shape — only 'Unknown model:' without 'models.providers'
    — could be any of many upstream OC errors. We require both so the
    pattern stays specific."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        _write_status(td, "team_bot_a", [
            "[diagnostic] error: Unknown model: anthropic/claude-haiku-4-5 "
            "(but no registry hint here)",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])
        sigs = _firing(td, "unknown_provider_model")
        assert len(sigs) == 0, (
            "pattern fired on incomplete shape — 'models.providers' "
            "needle must be required too, else we false-positive on "
            "generic FailoverError lines"
        )


# ── Specific pattern: plugin_load_failure ───────────────────────────────────


def test_plugin_load_failure_fires_on_live_error_text():
    """Reproduces the live error shapes seen on Maintenance → Status when
    the evolve-plugin bundle is out of sync (2026-06-08): both the
    `[plugins] evolve failed to load from ...` shape and the
    `[gateway] [plugins] failed to load plugin: ...` shape match."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        _write_status(td, "team_bot_a", [
            "[1h ago] 2026-06-08T00:18:14.261-07:00 [plugins] evolve "
            "failed to load from /Users/Shared/evolve-plugin/dist/index.js: "
            "Error: Cannot find module './observer/TurnObserver.js'",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])

        sigs = _firing(td, "plugin_load_failure")
        assert len(sigs) == 1, (
            "plugin_load_failure did not fire on the live error shape — "
            "needles '[plugins]' AND 'failed to load' must both match"
        )
        sig = sigs[0]
        assert sig.bot_id == "team_bot_a"
        assert "plugin failed to load" in sig.title
        # Structural failure — bot still answers but capabilities dropped.
        assert sig.details["vector"] == "operations"
        assert sig.details["magnitude"] == 3
        assert "what_it_means" in sig.details
        assert "fix_steps" in sig.details


def test_plugin_load_failure_matches_gateway_prefixed_shape():
    """The gateway re-logs the same failure with a `[gateway] [plugins]
    failed to load plugin: ...` prefix. Both shapes appear in the same
    recent_errors window and must dedup to one signal."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        _write_status(td, "team_bot_a", [
            "[plugins] evolve failed to load from "
            "/Users/Shared/evolve-plugin/dist/index.js: Error: "
            "Cannot find module './observer/TurnObserver.js'",
            "[gateway] [plugins] failed to load plugin: Error: "
            "Cannot find module './observer/TurnObserver.js'",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])
        sigs = _firing(td, "plugin_load_failure")
        assert len(sigs) == 1, (
            "two matching lines (same bot, same error) must dedup to one "
            "signal — both the [plugins] and [gateway] [plugins] shapes "
            "hit the same pattern"
        )


def test_plugin_load_failure_needs_both_needles():
    """Plenty of `[plugins]` error lines are NOT load failures (runtime
    errors raised from inside an already-loaded plugin, like the
    provider-override case). Without the `failed to load` needle the
    pattern would absorb those and double-report with their specific
    catalog entry."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        _write_status(td, "team_bot_a", [
            "[plugins] Evolve: LLM tier classification failed, using "
            "keyword fallback: Error: provider/model override is not "
            "authorized for this plugin subagent run.",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])
        # The provider-override pattern fires (live shape) …
        assert len(_firing(td, "provider_override_unauthorized")) == 1
        # … and plugin_load_failure stays quiet.
        assert _firing(td, "plugin_load_failure") == [], (
            "pattern fired on a runtime error from inside a loaded "
            "plugin — 'failed to load' needle must be required so it "
            "only catches startup load failures"
        )


# ── Specific pattern: provider_auth_failover ────────────────────────────────


def test_provider_auth_failover_fires_on_live_error_text():
    """Reproduces the live error shape from the 2026-06-09 alert root-cause
    audit (a pod bot's google/gemini PAT expired). Before this pattern
    existed the line fell through to uncategorized_errors and the
    actionable rotation step was hidden."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        live_line = (
            "embedded run failover decision: "
            "runId=49482650-aaaa-bbbb-cccc-ddddddddeeee stage=assistant "
            "decision=fallback_model reason=auth "
            "from=google/gemini-3.1-pro-preview "
            "profile=sha256:a94403a4f9d0"
        )
        _write_status(td, "team_bot_a", [
            live_line,
            "FailoverError: Authentication failed (provider returned "
            "HTTP 401). Your provider token may have expired ...",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])

        sigs = _firing(td, "provider_auth_failover")
        assert len(sigs) == 1, (
            "provider_auth_failover did not fire on the live error shape "
            "— needles 'failover decision' AND 'reason=auth' must both "
            "match"
        )
        sig = sigs[0]
        assert sig.bot_id == "team_bot_a"
        assert "provider auth failed" in sig.title
        # Degraded bot but still responding on fallback → magnitude=2.
        assert sig.details["vector"] == "operations"
        assert sig.details["magnitude"] == 2
        assert "what_it_means" in sig.details
        assert "fix_steps" in sig.details
        # The matched_line carries enough context for the operator to
        # identify the failing provider (`from=<provider>/<model>`).
        assert "from=google/gemini" in sig.details["matched_line"]


def test_provider_auth_failover_displaces_uncategorized_catch_all():
    """Before this pattern existed the live line fell into the
    uncategorized catch-all and the actionable rotation step was hidden
    behind a generic 'errors not yet pattern-matched' title. This pins
    that the new pattern absorbs the line so it no longer leaks into
    uncategorized — proves the catalog comment ('as specific patterns
    get added, this signal naturally retires') holds for this shape."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        _write_status(td, "team_bot_a", [
            "embedded run failover decision: runId=49482650-... "
            "stage=assistant decision=fallback_model reason=auth "
            "from=google/gemini-3.1-pro-preview "
            "profile=sha256:a94403a4f9d0",
            "FailoverError: Authentication failed (provider returned "
            "HTTP 401). Your provider token may have expired ...",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])

        # Specific pattern fires.
        assert len(_firing(td, "provider_auth_failover")) == 1
        # And the catch-all stays quiet: the failover-decision line is
        # absorbed by the new pattern (matched_indices); the bare
        # FailoverError line on its own is below the threshold (1 < 2).
        assert _firing(td, "uncategorized_errors") == [], (
            "uncategorized fired — the failover-decision line must be "
            "absorbed by the new specific pattern so the actionable "
            "rotation step isn't hidden behind a generic title"
        )


def test_provider_auth_failover_needs_both_needles():
    """A bare 'failover decision' line without `reason=auth` is the
    normal cost/quality-driven failover path, not an auth problem.
    Requiring both needles keeps the pattern specific to credential
    rotation."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        _write_status(td, "team_bot_a", [
            "embedded run failover decision: stage=assistant "
            "decision=fallback_model reason=rate_limit "
            "from=anthropic/claude-opus-4-8",
            "embedded run failover decision: stage=assistant "
            "decision=fallback_model reason=rate_limit "
            "from=anthropic/claude-opus-4-8",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])
        assert _firing(td, "provider_auth_failover") == [], (
            "provider_auth_failover fired on a non-auth failover — "
            "'reason=auth' needle must be required so the pattern only "
            "matches credential failures"
        )


# ── Catch-all: uncategorized_errors ─────────────────────────────────────────


def test_uncategorized_fires_when_threshold_unmatched_lines_present():
    """Two unmatched lines → one uncategorized signal. Keeps unknown
    error shapes from going silent on the Alerts page."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        _write_status(td, "team_bot_a", [
            "Some weird upstream error nobody has a pattern for yet",
            "Another mystery line from a brand new OC version",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])

        sigs = _firing(td, "uncategorized_errors")
        assert len(sigs) == 1
        sig = sigs[0]
        assert sig.bot_id == "team_bot_a"
        # Title shows the count so the operator can see at-a-glance how
        # many distinct unknowns are pending categorization.
        assert "2 gateway error" in sig.title
        # Sample lines + counters available to the operator on expand.
        assert sig.details["unmatched_count"] == 2
        assert sig.details["matched_count"] == 0
        assert sig.details["total_recent_errors"] == 2
        assert len(sig.details["sample_lines"]) == 2
        assert "Some weird upstream" in sig.details["sample_lines"][0]


def test_uncategorized_quiet_below_threshold():
    """One unmatched line is plausibly a transient blip — don't fire.
    Mirrors the _TOOL_FAILURE_THRESHOLD=2 convention."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        _write_status(td, "team_bot_a", [
            "Solo mystery error",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])
        assert _firing(td, "uncategorized_errors") == []


def test_uncategorized_quiet_when_all_lines_matched_specific_patterns():
    """If every recent_errors line matches a specific catalog pattern,
    the catch-all stays silent — otherwise we'd report every error
    twice (once with a real title + fix steps, once as 'uncategorized')."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        _write_status(td, "team_bot_a", [
            "MAX auth failed — falling back to metered billing",
            "provider/model override is not authorized for this plugin "
            "subagent run.",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])

        # Both specific patterns fire.
        assert len(_firing(td, "max_auth_failure")) == 1
        assert len(_firing(td, "provider_override_unauthorized")) == 1
        # Catch-all stays quiet — no unmatched lines.
        assert _firing(td, "uncategorized_errors") == [], (
            "uncategorized fired even though every line matched a "
            "specific pattern — matched_indices tracking must mark "
            "lines covered by specific patterns as not-uncategorized"
        )


def test_uncategorized_quiet_when_tool_failures_present_below_threshold():
    """A single [tools] message failed line is below the tool catch-all
    threshold but still 'matched' by that needle — must not bleed into
    the uncategorized catch-all."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        _write_status(td, "team_bot_a", [
            "[tools] message failed once",
            "Another single unrelated error line",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])

        # Tool-failure catch-all is below threshold (1 < 2) → no fire.
        assert _firing(td, "tool_delivery_failing") == []
        # Uncategorized is also below threshold because only ONE line
        # (the second) is genuinely unmatched.
        assert _firing(td, "uncategorized_errors") == [], (
            "[tools] message failed line bled into uncategorized — "
            "matched_indices must mark tool-failure lines as covered "
            "regardless of whether the tool-failure signal fired"
        )


def test_uncategorized_mixed_with_specific_pattern():
    """Real-world: one known shape + multiple unknown shapes. The
    specific pattern fires AND the catch-all fires for the unknowns,
    independently."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        _write_status(td, "team_bot_a", [
            "MAX auth failed",
            "weird upstream OC error A",
            "weird upstream OC error B",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])

        assert len(_firing(td, "max_auth_failure")) == 1
        uncat = _firing(td, "uncategorized_errors")
        assert len(uncat) == 1
        # Only the 2 weird lines count as unmatched.
        assert uncat[0].details["unmatched_count"] == 2
        assert uncat[0].details["matched_count"] == 1


def test_uncategorized_sweep_resolves_when_condition_clears():
    """Run 1: 2 unknown lines fire the catch-all. Run 2: lines drop out
    of recent_errors → sweep_resolve archives the signal so the Alerts
    page clears automatically once the condition is gone."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        # Run 1 — catch-all fires.
        _write_status(td, "team_bot_a", [
            "transient OC error A",
            "transient OC error B",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])
        assert len(_firing(td, "uncategorized_errors")) == 1

        # Run 2 — recent_errors empty. Catch-all sweep-resolves.
        _write_status(td, "team_bot_a", [])
        bls.emit_bot_log_signals(td, ["team_bot_a"])
        assert _firing(td, "uncategorized_errors") == [], (
            "uncategorized should sweep-resolve when no unmatched lines "
            "remain — keeps the Alerts page in sync with the live state"
        )


def test_uncategorized_sample_lines_capped_and_deduped():
    """The sample-lines list is capped (so a runaway error storm doesn't
    bloat the stored Signal) and de-duped (so 5 copies of the same
    error don't fill the cap, hiding distinct shapes)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        # 3 copies of one shape + 5 distinct shapes = 8 lines, but
        # de-duped should yield 6 distinct, capped to 5.
        _write_status(td, "team_bot_a", [
            "duplicate error shape A",
            "duplicate error shape A",
            "duplicate error shape A",
            "shape B",
            "shape C",
            "shape D",
            "shape E",
            "shape F",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])

        sigs = _firing(td, "uncategorized_errors")
        assert len(sigs) == 1
        sig = sigs[0]
        # All 8 lines count as unmatched.
        assert sig.details["unmatched_count"] == 8
        samples = sig.details["sample_lines"]
        # Capped at the producer's sample limit.
        assert len(samples) == bls._UNCATEGORIZED_SAMPLE_CAP
        # First sample is the duplicate (first in input), but it only
        # appears once because the de-dupe walked in order.
        assert samples[0] == "duplicate error shape A"
        assert samples.count("duplicate error shape A") == 1


def test_uncategorized_line_truncation_bounds_details_size():
    """A 4 KB error line should be truncated in the stored sample so the
    Signal JSON stays bounded; the full line is still on the bot's
    Maintenance → Status page."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        huge_line = "FailoverError: " + ("x" * 4000)
        _write_status(td, "team_bot_a", [
            huge_line,
            "second uncategorized line so the threshold is met",
        ])
        bls.emit_bot_log_signals(td, ["team_bot_a"])

        sigs = _firing(td, "uncategorized_errors")
        assert len(sigs) == 1
        samples = sigs[0].details["sample_lines"]
        # First sample is the huge line, truncated to the producer's cap.
        assert len(samples[0]) == bls._UNCATEGORIZED_LINE_TRUNCATE


# ── Catalog-shape sanity (lint-style) ───────────────────────────────────────


def test_every_specific_pattern_has_operator_docs():
    """Operator docs (what_it_means + fix_steps) are what makes the
    Alerts row useful when an operator sees it cold. Every signal_type
    in _PATTERNS must have an _OPERATOR_DOCS entry — including the new
    ones added in this PR."""
    pattern_types = {p.signal_type for p in bls._PATTERNS}
    pattern_types.add("tool_delivery_failing")  # catch-all from emit_bot_log_signals
    pattern_types.add("uncategorized_errors")   # uncategorized catch-all
    missing = pattern_types - set(bls._OPERATOR_DOCS.keys())
    assert not missing, (
        f"missing operator docs for signal_type(s): {missing}. "
        "Every Signal must carry what_it_means + fix_steps so the "
        "Alerts row is actionable when the operator sees it cold."
    )


def test_every_signal_type_has_severity_tag():
    """Severity-framework tags drive the priority bucket on Home + Alerts
    sort. Untagged producers fall through to (operations, 2) per
    _severity_tag's default, but the policy decision should be explicit."""
    pattern_types = {p.signal_type for p in bls._PATTERNS}
    pattern_types.add("tool_delivery_failing")
    pattern_types.add("uncategorized_errors")
    missing = pattern_types - set(bls._SEVERITY_TAG.keys())
    assert not missing, (
        f"missing severity tags for signal_type(s): {missing}. "
        "Add explicit (vector, magnitude) to _SEVERITY_TAG so the "
        "policy choice is visible in version control."
    )
