"""tests/test_security_chat_context.py — server-side Security-page pack.

Covers :mod:`evolve_admin.web.security_chat_context` and the page-
reshape hook in :mod:`evolve_admin.web.home_chat_routes`. The
underlying motivation is the 2026-06-04 Security-page chat timeout:
the client-built pack was too sparse, so evo fanned out into
``pod_state.audit`` per affected bot and accumulated past 270s. The
server-side rebuild coalesces findings across bots and inlines the
detail evo would have to fetch anyway, so the model can answer
inline. See ``issues/features/feature-2026-06-04-002…``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin.web import security_chat_context as _scc  # noqa: E402
from evolve_admin.evo import proxy as _proxy  # noqa: E402


# The real downstream cap is the proxy's rendered-body cap. The pack
# is rendered (NOT JSON-encoded) into the <page-context> XML block,
# then truncated to ``_SUMMARY_MAX_CHARS``. Aim for comfortably
# under that on a worst-case pod.
_RENDERED_CAP = _proxy._SUMMARY_MAX_CHARS  # 4000 at time of writing


# ─────────────────────────────────────────────────────────────────────────────
# Fixture helpers
# ─────────────────────────────────────────────────────────────────────────────


def _finding(severity: str, message: str, category: str = "config",
             remediation: str = "") -> dict:
    """Build an audit-cache-shaped finding dict."""
    return {
        "severity": severity,
        "category": category,
        "message": message,
        "recommendation": remediation,
    }


def _bot_result(score: int, findings: list[dict]) -> dict:
    """Build an audit-cache-shaped per-bot result."""
    return {
        "score": score,
        "findings": findings,
        "warned": sum(1 for f in findings if f["severity"] in ("warning", "warn")),
        "critical": sum(1 for f in findings if f["severity"] == "critical"),
        "info": sum(1 for f in findings if f["severity"] == "info"),
        "generated_at": "2026-06-04T20:00:00Z",
        "run_at": "2026-06-04T20:00:00Z",
    }


def _signal(*, signature: str, title: str, producer: str,
            bot_id: str, occurrence_count: int = 1,
            severity_vector: str = "operations",
            severity_magnitude: int = 2,
            signal_type: str = "") -> dict:
    """Build a Signal.to_dict()-shaped dict."""
    return {
        "state": "firing",
        "signature": signature,
        "title": title,
        "producer": producer,
        "bot_id": bot_id,
        "occurrence_count": occurrence_count,
        "severity_framework": {
            "vector": severity_vector,
            "magnitude": severity_magnitude,
        },
        "type": signal_type,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Coalescing — the central claim of the reshape
# ─────────────────────────────────────────────────────────────────────────────


def test_findings_coalesced_across_bots_with_bot_list(tmp_path):
    """Same advisory across 3 bots → ONE entry with `bots: [a, b, c]`."""
    advisory = "Plugin index includes unpinned npm specs"
    audit = {
        "bot-a":    _bot_result(85, [_finding("warning", advisory, remediation="Pin to a version")]),
        "bot-b":  _bot_result(85, [_finding("warning", advisory, remediation="Pin to a version")]),
        "bot-c": _bot_result(85, [_finding("warning", advisory, remediation="Pin to a version")]),
    }
    state = _scc.build_security_state(
        tmp_path, audit_data_override=audit, firing_signals_override=[],
    )
    matches = [f for f in state["findings"] if f["title"] == advisory]
    assert len(matches) == 1, "advisory should appear ONCE coalesced"
    assert matches[0]["bots"] == ["bot-a", "bot-b", "bot-c"]
    assert matches[0]["count"] == 3
    assert matches[0]["remediation"] == "Pin to a version"


def test_findings_with_distinct_messages_are_not_coalesced(tmp_path):
    audit = {
        "bot-a": _bot_result(85, [_finding("critical", "Auth-profiles writable by group")]),
        "bot-b": _bot_result(85, [_finding("critical", "Gateway bind exposes loopback to 0.0.0.0")]),
    }
    state = _scc.build_security_state(
        tmp_path, audit_data_override=audit, firing_signals_override=[],
    )
    titles = sorted(f["title"] for f in state["findings"])
    assert titles == [
        "Auth-profiles writable by group",
        "Gateway bind exposes loopback to 0.0.0.0",
    ]


def test_findings_sorted_critical_then_warn_then_count(tmp_path):
    audit = {
        "a": _bot_result(60, [
            _finding("warning", "warn-shared", remediation="r1"),
            _finding("critical", "crit-shared", remediation="r2"),
        ]),
        "b": _bot_result(60, [
            _finding("warning", "warn-shared", remediation="r1"),
            _finding("warning", "warn-unique-to-b", remediation="r3"),
        ]),
    }
    state = _scc.build_security_state(
        tmp_path, audit_data_override=audit, firing_signals_override=[],
    )
    titles_in_order = [f["title"] for f in state["findings"]]
    # critical first
    assert titles_in_order[0] == "crit-shared"
    # then warnings, with shared (count=2) before unique (count=1)
    assert titles_in_order[1] == "warn-shared"
    assert titles_in_order[2] == "warn-unique-to-b"


def test_severity_alias_warn_normalized_to_warning(tmp_path):
    """Some producers emit `warn`, others `warning`; the pack canonicalizes."""
    audit = {
        "bot-a":  _bot_result(70, [_finding("warn", "x", remediation="")]),
        "bot-b": _bot_result(70, [_finding("warning", "x", remediation="")]),
    }
    state = _scc.build_security_state(
        tmp_path, audit_data_override=audit, firing_signals_override=[],
    )
    # Both rows are the same severity-after-normalization, so they coalesce.
    assert len(state["findings"]) == 1
    assert state["findings"][0]["severity"] == "warning"
    assert state["findings"][0]["bots"] == ["bot-a", "bot-b"]


# ─────────────────────────────────────────────────────────────────────────────
# Inline detail — the second pillar
# ─────────────────────────────────────────────────────────────────────────────


def test_remediation_inlined_per_coalesced_finding(tmp_path):
    audit = {
        "bot-a": _bot_result(85, [_finding(
            "warning", "Plugin index includes unpinned npm specs",
            remediation="Pin each plugin to a specific version in openclaw.json under plugins.installs[].",
        )]),
    }
    state = _scc.build_security_state(
        tmp_path, audit_data_override=audit, firing_signals_override=[],
    )
    assert state["findings"][0]["remediation"].startswith(
        "Pin each plugin to a specific version"
    )


def test_signal_detail_inlined_producer_and_occurrence(tmp_path):
    signals = [
        _signal(
            signature="cost.spike:bot-a",
            title="Cost spike on bot-a (4× baseline)",
            producer="cost_watchdog",
            bot_id="bot-a",
            occurrence_count=12,
            severity_vector="cost",
            severity_magnitude=3,
            signal_type="cost.spike",
        ),
    ]
    state = _scc.build_security_state(
        tmp_path, audit_data_override={}, firing_signals_override=signals,
    )
    sig = state["firing_signals"][0]
    assert sig["signature"] == "cost.spike:bot-a"
    assert sig["producer"] == "cost_watchdog"
    assert sig["occurrence_count"] == 12
    assert sig["severity_vector"] == "cost"
    assert sig["severity_magnitude"] == 3
    assert sig["bot_id"] == "bot-a"
    assert sig["type"] == "cost.spike"


def test_signals_sorted_by_severity_then_occurrence(tmp_path):
    signals = [
        _signal(signature="a", title="low-cost", producer="x", bot_id="bot-a",
                occurrence_count=1, severity_magnitude=1),
        _signal(signature="b", title="high-cost-many", producer="x", bot_id="bot-b",
                occurrence_count=20, severity_magnitude=3),
        _signal(signature="c", title="high-cost-few", producer="x", bot_id="bot-c",
                occurrence_count=2, severity_magnitude=3),
    ]
    state = _scc.build_security_state(
        tmp_path, audit_data_override={}, firing_signals_override=signals,
    )
    order = [s["title"] for s in state["firing_signals"]]
    assert order == ["high-cost-many", "high-cost-few", "low-cost"]


# ─────────────────────────────────────────────────────────────────────────────
# Robustness — unavailable, empty, drift hint shapes
# ─────────────────────────────────────────────────────────────────────────────


def test_unavailable_bot_surfaces_inline_with_error_type(tmp_path):
    audit = {
        "bot-a": _bot_result(85, []),
        "broken": {"unavailable": True, "error_type": "config_invalid",
                   "error": "Config invalid: …"},
    }
    state = _scc.build_security_state(
        tmp_path, audit_data_override=audit, firing_signals_override=[],
    )
    rows = {r["bot_id"]: r for r in state["per_bot"]}
    assert rows["broken"]["unavailable"] is True
    assert rows["broken"]["error_type"] == "config_invalid"
    # Available bot is normal-shaped.
    assert rows["bot-a"]["score"] == 85
    assert "unavailable" not in rows["bot-a"]


def test_empty_inputs_produce_safe_empty_pack(tmp_path):
    state = _scc.build_security_state(
        tmp_path, audit_data_override={}, firing_signals_override=[],
    )
    assert state["per_bot"] == []
    assert state["findings"] == []
    assert state["firing_signals"] == []
    assert state["backup_drift"] == []
    assert state["counts"] == {
        "critical": 0, "warn": 0, "info": 0,
        "drifted_bots": 0, "firing_signals": 0,
    }
    assert "Security audit data not yet loaded" in state["headline"]


def test_backup_drift_hint_accepts_joined_string_form(tmp_path):
    """The client packer joins drifted_keys into a comma-separated
    string before sending. The server input shim must accept that."""
    drift_hint = [{
        "bot_id": "bot-b",
        "drifted_keys": "channels.slack.policy.block_replies, plugins.installs",
        "stale_backup": False,
        "last_backup_at": "2026-06-03T10:00:00Z",
    }]
    state = _scc.build_security_state(
        tmp_path, audit_data_override={}, firing_signals_override=[],
        backup_drift_hint=drift_hint,
    )
    assert state["backup_drift"][0]["drifted_keys"] == [
        "channels.slack.policy.block_replies",
        "plugins.installs",
    ]


def test_backup_drift_hint_accepts_list_form(tmp_path):
    drift_hint = [{
        "bot_id": "bot-b",
        "drifted_keys": ["a", "b", "c"],
    }]
    state = _scc.build_security_state(
        tmp_path, audit_data_override={}, firing_signals_override=[],
        backup_drift_hint=drift_hint,
    )
    assert state["backup_drift"][0]["drifted_keys"] == ["a", "b", "c"]


def test_active_subtab_backups_drives_headline(tmp_path):
    state = _scc.build_security_state(
        tmp_path, audit_data_override={}, firing_signals_override=[],
        active_subtab="backups",
        backup_drift_hint=[{
            "bot_id": "bot-b",
            "drifted_keys": ["a"],
        }],
    )
    assert "Backups subtab" in state["headline"]
    assert "1 bot(s) have config drift" in state["headline"]


# ─────────────────────────────────────────────────────────────────────────────
# Size guard — the whole point of the reshape is to fit inside the cap
# ─────────────────────────────────────────────────────────────────────────────


def test_worst_case_pack_stays_under_page_state_cap(tmp_path):
    """Stress: 10 bots × 8 findings each + 12 firing signals + 6 drift
    entries. Should still serialize under the 1500-char downstream cap."""
    big_advisories = [
        "Plugin index includes unpinned npm specs",
        "Auth-profiles writable by group",
        "Gateway bind exposes loopback through reverse proxy",
        "Primary model below recommended tier",
        "Tool exec policy not scoped to allowlist",
        "Cron caps not configured for app dispatcher",
        "Hook conversation access not opted in",
        "Embedding provider not configured",
    ]
    audit = {}
    for i in range(10):
        bot = f"bot-{i}"
        audit[bot] = _bot_result(
            70 - i,
            [_finding(
                "critical" if j % 4 == 0 else "warning" if j % 2 == 0 else "info",
                t,
                remediation=f"Fix step {j}: open openclaw.json under bots.{bot} and …",
            ) for j, t in enumerate(big_advisories)],
        )
    signals = [
        _signal(
            signature=f"sig:{i}", title=f"Active issue {i}",
            producer="security_warden", bot_id=f"bot-{i % 10}",
            occurrence_count=i + 1, severity_magnitude=3 if i < 3 else 2,
        )
        for i in range(12)
    ]
    drift_hint = [
        {"bot_id": f"bot-{i}", "drifted_keys": ["plugins.installs", "channels.x"]}
        for i in range(6)
    ]
    state = _scc.build_security_state(
        tmp_path, audit_data_override=audit, firing_signals_override=signals,
        backup_drift_hint=drift_hint,
    )
    rendered = _proxy._render_summary_body(state)
    assert len(rendered) < _RENDERED_CAP, (
        f"pack renders to {len(rendered)} chars, exceeds {_RENDERED_CAP}-char cap"
    )
    # And confirm we did elide — otherwise the cap isn't being defended.
    assert state["elided_findings_count"] > 0 or state["elided_signals_count"] > 0
    # The rendered body must surface the per-bot scoreboard, the
    # coalesced findings list, and the firing-signals list — otherwise
    # the reshape isn't paying for itself.
    assert "Per-bot scoreboard:" in rendered
    assert "Findings coalesced across bots" in rendered
    assert "Firing signals" in rendered


# ─────────────────────────────────────────────────────────────────────────────
# Wiring — _maybe_reshape_page_context dispatch
# ─────────────────────────────────────────────────────────────────────────────


def test_maybe_reshape_passes_through_non_security_pages(tmp_path):
    from evolve_admin.web import home_chat_routes as _hcr
    pc_in = {
        "page_id": "integrations-keys",
        "page_label": "Plugins",
        "summary": {"headline": "Plugins page"},
    }
    # network_path doesn't matter — non-security page short-circuits.
    pc_out = _hcr._maybe_reshape_page_context(pc_in, tmp_path / "fake-network.json")
    assert pc_out is pc_in, "non-registered page should pass through unchanged"


def test_maybe_reshape_swaps_summary_for_security(tmp_path, monkeypatch):
    """Security page → server-built summary replaces the client one."""
    from evolve_admin.web import home_chat_routes as _hcr
    network_json = tmp_path / "network.json"
    network_json.write_text(json.dumps({
        "sharedDir": str(tmp_path),
        "bots": {},
    }))
    # Stub the builder so we can assert it's called and see its return.
    sentinel = {"headline": "server-built", "from_test": True}
    calls: list[tuple] = []

    def _stub_build(shared_dir, **kw):
        calls.append((shared_dir, kw))
        return sentinel

    monkeypatch.setattr(
        "evolve_admin.web.security_chat_context.build_security_state",
        _stub_build,
    )
    pc_in = {
        "page_id": "security",
        "page_label": "Security",
        "summary": {
            "headline": "client-built (sparse)",
            "active_subtab": "audit",
            "backup_drift_items": [],
        },
    }
    pc_out = _hcr._maybe_reshape_page_context(pc_in, network_json)
    assert pc_out is not pc_in, "should return a new dict, not mutate"
    assert pc_out["summary"] == sentinel
    # Verify client hints threaded through to the builder.
    assert calls, "builder should have been called"
    _shared_dir_arg, kwargs = calls[0]
    assert kwargs["active_subtab"] == "audit"
    assert kwargs["backup_drift_hint"] == []


def test_maybe_reshape_handles_missing_summary(tmp_path, monkeypatch):
    """When the client sends page_id=security with no summary at all
    (legacy clients, or a chatty load), the reshape still runs with
    None hints rather than crashing."""
    from evolve_admin.web import home_chat_routes as _hcr
    network_json = tmp_path / "network.json"
    network_json.write_text(json.dumps({"sharedDir": str(tmp_path), "bots": {}}))

    monkeypatch.setattr(
        "evolve_admin.web.security_chat_context.build_security_state",
        lambda shared_dir, **kw: {"headline": "ok"},
    )
    pc_out = _hcr._maybe_reshape_page_context(
        {"page_id": "security"}, network_json,
    )
    assert pc_out["summary"] == {"headline": "ok"}
