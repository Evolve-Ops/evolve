"""tests/test_alerts_dispatcher.py — alert dispatcher Phase 1.

Pins the contract the source-side migrations (Phase 3+) will rely on:

  - master switch and per-source enable both gate dispatch
  - per-dedup_key cooldown suppresses repeat fires
  - failed subprocess does NOT update cooldown state (so the next tick
    can retry — duplicates are worse than a missed alert)
  - recipient resolution: explicit override → network.alerts → bots.evolve
  - audit log captures every call; suppression log captures only
    suppressed/no-recipient outcomes
  - state file uses atomic write (we don't pin atomicity directly, but we
    pin that consecutive calls observe each other's writes)

We monkeypatch ``_dispatch_via_openclaw`` in the dispatcher module so no
real subprocess fires; the contract under test is the surrounding
gating, state, and logging — not openclaw itself.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """tmp shared_dir + a fake openclaw subprocess + a default-recipient network."""
    from evolve_admin.alerts import dispatcher

    shared = tmp_path / "evolve"
    shared.mkdir()

    sent: list[tuple[str, str, str]] = []   # [(channel, chat_id, message)]
    next_result = {"ok": True, "error": None}

    def _fake_dispatch(channel, chat_id, message, gateway_port=None):
        if next_result["ok"]:
            sent.append((channel, chat_id, message))
            return True, None
        return False, next_result["error"] or "fake failure"

    monkeypatch.setattr(dispatcher, "_dispatch_via_openclaw", _fake_dispatch)

    network = {
        "alerts": {"channel": "telegram", "chatId": "12345"},
        "bots": {},
    }
    return {
        "shared": shared,
        "network": network,
        "dispatcher": dispatcher,
        "sent": sent,
        "next_result": next_result,
    }


def _force_immediate(shared, key):
    """Pin an operator IMMEDIATE override for ``key``.

    Workstream D1 moved most events to a digest default, so these
    send-mechanism tests (footer, cooldown floor, payload render, legacy
    fallback, failure classification) — which assert an immediate SENT and
    are not about the default cadence — set an explicit immediate override
    to exercise the immediate path independently of the catalog default."""
    from evolve_admin.alerts import subscriptions as _subs
    from evolve_admin.alerts.catalog import Frequency
    _subs.write_subscription(shared, key, frequency=Frequency.IMMEDIATE)


def _read_log(shared, name):
    """Read records from a dispatcher log stream (post-rotation layout).

    The dispatcher writes to ``alerts/<basename>/<YYYY-MM-DD>.jsonl``.
    We glob every daily file plus the legacy flat path so tests pin
    behavior regardless of layout — the same way the production reader
    (``dispatcher.iter_log_records``) does.
    """
    basename = name[:-len(".jsonl")] if name.endswith(".jsonl") else name
    records: list[dict] = []
    log_dir = shared / "alerts" / basename
    if log_dir.is_dir():
        for p in sorted(log_dir.glob("*.jsonl"), key=lambda x: x.stem):
            records.extend(
                json.loads(line)
                for line in p.read_text().splitlines()
                if line
            )
    flat = shared / "alerts" / name
    if flat.is_file():
        records.extend(
            json.loads(line)
            for line in flat.read_text().splitlines()
            if line
        )
    return records


# ── Master switch + per-source enable ──────────────────────────────────────


def test_send_dispatches_when_enabled(env):
    out = env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="hello",
    )
    assert out.result == env["dispatcher"].DispatchResult.SENT
    assert env["sent"] == [("telegram", "12345", "hello")]
    assert _read_log(env["shared"], "dispatcher.jsonl")[-1]["result"] == "sent"


def test_unbound_send_records_a_binding_violation(env):
    """Bind-complete contract (spec-subscription-completeness-2026-06-24
    workstream A, approved 2026-06-28): a dispatch with no resolvable
    catalog_event is a contract violation. The old escape hatch silently
    skipped subscription gating on catalog_event=None; now the dispatcher
    records the violation to its own stream so the unbound send is visible.

    The producer-side ratchets (test_alerts_signal_notifier) keep this from
    ever happening for alerts/ producers; this pins the runtime backstop —
    AND that we fail OPEN (the message still delivers, never silently
    dropped)."""
    out = env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="legacy unbound message",
        catalog_event=None,
    )
    # Fail open: the message still delivers (a missed binding degrades to
    # ungated delivery, never to silence).
    assert out.result == env["dispatcher"].DispatchResult.SENT
    assert env["sent"] == [("telegram", "12345", "legacy unbound message")]
    # …and the violation is recorded on its own stream (not the counted
    # suppressed log).
    violations = _read_log(env["shared"], "dispatcher-binding-violations.jsonl")
    assert len(violations) == 1
    assert violations[0]["result"] == "binding_violation_catalog_event_none"
    assert violations[0]["source"] == "audit"
    # A bound send leaves the violations stream empty.
    env["sent"].clear()
    env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="bound message",
        catalog_event="security.audit_finding",
    )
    assert len(
        _read_log(env["shared"], "dispatcher-binding-violations.jsonl")
    ) == 1  # unchanged — the bound send added nothing


def test_master_switch_off_suppresses(env, monkeypatch):
    monkeypatch.setattr(
        env["dispatcher"], "_read_dispatcher_enabled", lambda _sd: False
    )
    out = env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="hello",
    )
    assert out.result == env["dispatcher"].DispatchResult.SUPPRESSED_DISABLED
    assert env["sent"] == []
    suppressed = _read_log(env["shared"], "dispatcher-suppressed.jsonl")
    assert len(suppressed) == 1
    assert suppressed[0]["result"] == "suppressed_disabled"


def test_per_source_disable_suppresses(env, monkeypatch):
    real = env["dispatcher"]._read_source_enabled
    monkeypatch.setattr(
        env["dispatcher"], "_read_source_enabled",
        lambda sd, source: False if source == "audit" else real(sd, source),
    )
    out = env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="hello",
    )
    assert out.result == env["dispatcher"].DispatchResult.SUPPRESSED_DISABLED
    assert "source_disabled:audit" in (out.error or "")
    assert env["sent"] == []


# ── Cooldown ───────────────────────────────────────────────────────────────


def test_cooldown_suppresses_repeat_within_window(env):
    d = env["dispatcher"]
    t0 = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    out1 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="first",
        dedup_key="audit/team_bot_a/loopback", cooldown_seconds=600,
        now=t0,
    )
    out2 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="second",
        dedup_key="audit/team_bot_a/loopback", cooldown_seconds=600,
        now=t0 + timedelta(seconds=300),
    )
    assert out1.result == d.DispatchResult.SENT
    assert out2.result == d.DispatchResult.SUPPRESSED_COOLDOWN
    assert len(env["sent"]) == 1


def test_cooldown_expires_allows_resend(env):
    d = env["dispatcher"]
    t0 = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="first",
        dedup_key="audit/team_bot_a/loopback", cooldown_seconds=600,
        now=t0,
    )
    out = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="after-cooldown",
        dedup_key="audit/team_bot_a/loopback", cooldown_seconds=600,
        now=t0 + timedelta(seconds=601),
    )
    assert out.result == d.DispatchResult.SENT
    assert len(env["sent"]) == 2


def test_cooldown_zero_never_suppresses(env):
    d = env["dispatcher"]
    t0 = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(3):
        out = d.send(
            shared_dir=env["shared"], network=env["network"],
            source="forge_engine", message=f"m{i}",
            dedup_key=f"forge/job-{i}", cooldown_seconds=0,
            now=t0 + timedelta(seconds=i),
        )
        assert out.result == d.DispatchResult.SENT
    assert len(env["sent"]) == 3


def test_no_dedup_key_skips_cooldown(env):
    d = env["dispatcher"]
    for _ in range(2):
        out = d.send(
            shared_dir=env["shared"], network=env["network"],
            source="audit", message="x",
            cooldown_seconds=600,   # cooldown set, but dedup_key=None bypasses
        )
        assert out.result == d.DispatchResult.SENT
    assert len(env["sent"]) == 2


def test_failed_dispatch_records_cooldown(env):
    """Failed dispatch records cooldown so a broken send path can't spam.

    Background: the openclaw CLI sometimes hangs in shutdown for ~30 min
    after delivering a message, tripping our 60s subprocess timeout. The
    original behavior — only record cooldown on SENT — meant every retry
    within the dedup window bypassed the gate, generating dozens of
    duplicate FAILED entries per hour (and often duplicate deliveries,
    when the underlying send actually worked). Treating cooldown as
    "this dedup_key was attempted recently" trades one rare correctness
    case (transient blip that resolves before cooldown elapses) for a
    much better failure mode. See commit 099f66fe.
    """
    d = env["dispatcher"]
    env["next_result"]["ok"] = False
    env["next_result"]["error"] = "fake failure"
    t0 = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)

    out1 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="first",
        dedup_key="audit/team_bot_a/loopback", cooldown_seconds=600,
        now=t0,
    )
    assert out1.result == d.DispatchResult.FAILED

    # Even with the subprocess recovered, the next send within the
    # cooldown window must be suppressed — the prior attempt counts.
    env["next_result"]["ok"] = True
    out2 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="retry",
        dedup_key="audit/team_bot_a/loopback", cooldown_seconds=600,
        now=t0 + timedelta(seconds=30),
    )
    assert out2.result == d.DispatchResult.SUPPRESSED_COOLDOWN

    # Past the cooldown window, the retry goes through.
    out3 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="retry-later",
        dedup_key="audit/team_bot_a/loopback", cooldown_seconds=600,
        now=t0 + timedelta(seconds=601),
    )
    assert out3.result == d.DispatchResult.SENT


# ── Recipient resolution ───────────────────────────────────────────────────


def test_recipient_override_used_directly(env):
    d = env["dispatcher"]
    out = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="m",
        recipient_override=("discord", "user-99"),
    )
    assert out.result == d.DispatchResult.SENT
    assert env["sent"] == [("discord", "user-99", "m")]


def test_recipient_falls_back_to_evolve_external_id(env):
    d = env["dispatcher"]
    network = {
        "alerts": {},  # no channel/chatId set
        "bots": {
            "evolve": {
                "primary_user": {"external_ids": {"telegram": "t-987"}}
            }
        },
    }
    out = d.send(
        shared_dir=env["shared"], network=network,
        source="audit", message="m",
    )
    assert out.result == d.DispatchResult.SENT
    assert env["sent"] == [("telegram", "t-987", "m")]


def test_recipient_explicit_primary_channel_overrides_priority(env):
    d = env["dispatcher"]
    network = {
        "alerts": {},
        "bots": {
            "evolve": {
                "primary_channel": "discord",
                "primary_user": {
                    "external_ids": {
                        "telegram": "t-1",   # priority 1 by default
                        "discord": "d-2",
                    }
                },
            }
        },
    }
    out = d.send(
        shared_dir=env["shared"], network=network,
        source="audit", message="m",
    )
    assert out.result == d.DispatchResult.SENT
    assert env["sent"][0][0] == "discord"


def test_no_recipient_returns_no_recipient(env):
    d = env["dispatcher"]
    network = {"alerts": {}, "bots": {"evolve": {}}}
    out = d.send(
        shared_dir=env["shared"], network=network,
        source="audit", message="m",
    )
    assert out.result == d.DispatchResult.NO_RECIPIENT
    assert env["sent"] == []
    suppressed = _read_log(env["shared"], "dispatcher-suppressed.jsonl")
    assert len(suppressed) == 1
    assert suppressed[0]["result"] == "no_recipient"


# ── Audit log ──────────────────────────────────────────────────────────────


def test_audit_log_captures_severity_and_dedup_key(env):
    d = env["dispatcher"]
    d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="x" * 300,
        severity=d.Severity.ERROR,
        dedup_key="audit/team_bot_a/check.foo",
    )
    rec = _read_log(env["shared"], "dispatcher.jsonl")[-1]
    assert rec["severity"] == "error"
    assert rec["dedup_key"] == "audit/team_bot_a/check.foo"
    assert rec["channel"] == "telegram"
    assert rec["chat_id"] == "12345"
    # excerpt is truncated with an ellipsis
    assert rec["message_excerpt"].endswith("…")
    assert len(rec["message_excerpt"]) <= 241


def test_failed_dispatch_logged_to_delivery_failures_with_error(env):
    """Failed sends land in delivery-failures.jsonl, NOT dispatcher.jsonl.

    PWA-polish split: the Recent Messages list reads dispatcher.jsonl;
    routing failed sends to a separate file keeps raw HTTP error
    payloads from polluting the operator's "what alerts went out"
    feed. They surface separately on the Dispatcher Health panel.
    """
    d = env["dispatcher"]
    env["next_result"]["ok"] = False
    env["next_result"]["error"] = "telegram api 403"
    out = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="m",
    )
    assert out.result == d.DispatchResult.FAILED
    main = _read_log(env["shared"], "dispatcher.jsonl")
    failures = _read_log(env["shared"], "delivery-failures.jsonl")
    suppressed = _read_log(env["shared"], "dispatcher-suppressed.jsonl")
    # No failed entries in the main feed — that's what protects the
    # Recent Messages list from noise.
    assert main == []
    assert len(failures) == 1 and failures[0]["result"] == "failed"
    assert failures[0]["error"].startswith("telegram api")
    assert suppressed == []


def test_successful_dispatch_logged_to_main_not_delivery_failures(env):
    """Successful sends continue to land in dispatcher.jsonl; the
    delivery-failures lane stays empty. The Recent Messages list
    surfaces these without any failure-payload contamination."""
    d = env["dispatcher"]
    out = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="success",
    )
    assert out.result == d.DispatchResult.SENT
    main = _read_log(env["shared"], "dispatcher.jsonl")
    failures = _read_log(env["shared"], "delivery-failures.jsonl")
    assert len(main) == 1 and main[0]["result"] == "sent"
    assert failures == []


def test_failed_dispatch_via_every_failure_code_path_lands_in_delivery_failures(
    env, monkeypatch,
):
    """Defence against the silent-failure-mode warning in the PR brief:
    'the dispatcher emits a FAILED result via multiple code paths' —
    every one of them must route to delivery-failures.jsonl, not just
    the most common one.

    Currently three call sites produce FAILED in dispatcher.py:
      1. catalog_event render fails AND no fallback message provided
      2. send() called with no message and no renderable payload
      3. subprocess delivery returns ok=False (telegram_send_fail,
         slack_send_fail, openclaw subprocess error, etc.)

    All three are covered by other tests in this module — this one
    pins the routing invariant directly by exercising each call site
    and asserting the file split.
    """
    d = env["dispatcher"]

    # (1) catalog render fails with no fallback.
    out1 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="update_watcher",
        catalog_event="not.a.real.event",
        payload={"x": 1},   # forces the catalog-render path
    )
    assert out1.result == d.DispatchResult.FAILED

    # (2) no message, no payload.
    out2 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit",
    )
    assert out2.result == d.DispatchResult.FAILED

    # (3) subprocess delivery fails — separately from (1)/(2) this is
    # the noisy-in-practice case: telegram returns HTTP 400.
    env["next_result"]["ok"] = False
    env["next_result"]["error"] = "telegram http 400: bad parse"
    out3 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="m",
    )
    assert out3.result == d.DispatchResult.FAILED

    # All three landed in delivery-failures.jsonl — none in dispatcher.jsonl,
    # none in dispatcher-suppressed.jsonl.
    failures = _read_log(env["shared"], "delivery-failures.jsonl")
    assert {f["result"] for f in failures} == {"failed"}
    assert len(failures) == 3
    main = _read_log(env["shared"], "dispatcher.jsonl")
    assert not any(r.get("result") == "failed" for r in main)
    suppressed = _read_log(env["shared"], "dispatcher-suppressed.jsonl")
    assert not any(r.get("result") == "failed" for r in suppressed)


# ── State file ─────────────────────────────────────────────────────────────


def test_state_file_persists_across_calls(env, tmp_path):
    """Cooldown state must survive a fresh dispatcher import — i.e. it
    actually reads from disk, not from in-process memory."""
    d = env["dispatcher"]
    t0 = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="m",
        dedup_key="audit/team_bot_a/loopback", cooldown_seconds=600,
        now=t0,
    )
    state_path = env["shared"] / "alerts" / "dispatcher-state.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state["version"] == 1
    assert "audit::audit/team_bot_a/loopback" in state["last_dispatch"]


def test_known_sources_includes_signal_notifier(env):
    sources = set(env["dispatcher"].known_sources())
    assert "signal_notifier" in sources
    assert "audit" in sources
    assert "spend_alert" in sources


def test_known_sources_includes_cost_watchdog(env):
    """Regression for 2026-05-24: cost_watchdog was emitting alerts but
    wasn't registered in _DEFAULT_SOURCE_ENABLED / _DEFAULT_SOURCE_COOLDOWN_SECONDS,
    so it inherited the fallback (enabled=True, cooldown=0). The producer's
    dedup_key embeds {today}, so cooldown=0 meant every hourly cost_watchdog
    tick re-fired the same heartbeat-bloat alert for hours."""
    sources = set(env["dispatcher"].known_sources())
    assert "cost_watchdog" in sources

    # The cooldown table is the second half of the registration; both
    # must agree or operators get inconsistent defaults.
    from evolve_admin.alerts.dispatcher import (
        _DEFAULT_SOURCE_COOLDOWN_SECONDS,
    )
    assert "cost_watchdog" in _DEFAULT_SOURCE_COOLDOWN_SECONDS
    # 24h matches the producer's daily dedup_key shape.
    assert _DEFAULT_SOURCE_COOLDOWN_SECONDS["cost_watchdog"] == 86_400


def test_every_dispatching_source_is_registered():
    """Structural guard: every ``source=`` string passed to ``dispatcher.send``
    must be registered in BOTH ``_DEFAULT_SOURCE_ENABLED`` and
    ``_DEFAULT_SOURCE_COOLDOWN_SECONDS``.

    An unregistered source silently inherits ``enabled=True, cooldown=0``,
    which is almost never what the producer author intended — and the
    cost_watchdog spam of 2026-05-24 is exactly the failure mode.

    Iterates the catalog's ``producer_source`` field. Producers that only
    write Signals (their chat push runs through signal_notifier under
    ``source="signal_notifier"``) are explicitly listed in
    ``_SIGNAL_ONLY_PRODUCERS`` below and are exempt from the check.
    """
    from evolve_admin.alerts import catalog as cat
    from evolve_admin.alerts.dispatcher import (
        _DEFAULT_SOURCE_ENABLED, _DEFAULT_SOURCE_COOLDOWN_SECONDS,
    )

    # Producers that exist in the catalog (so the operator-facing UI
    # can render their subscriptions) but never call dispatcher.send
    # directly — their Signal is forwarded by signal_notifier, which
    # supplies source="signal_notifier" at dispatch time. These don't
    # need their own enable/cooldown entries because the signal_notifier
    # entries gate them.
    _SIGNAL_ONLY_PRODUCERS = frozenset({
        "oc_cli",   # signals.cli_misinvocation; signal_notifier handles push
        # Subscription-completeness keystone (spec-subscription-completeness-
        # 2026-06-24): these producers emit via signals.store.observe() only;
        # chat delivery flows through signal_notifier under their bespoke
        # catalog events, so they never call dispatcher.send directly.
        "content_scan",        # system.identity_doc_missing
        "alerts_loop_monitor", # meta.alert_repeat_loop / meta.dispatcher_health
        "forge_cost_guard",    # cost.forge_session_cap
        # Bespoke catalog entries added 2026-06-03 (follow-up to the
        # PR #2064 signal_notifier allowlist broadening). Each producer
        # writes only to the Signal store; chat dispatch flows through
        # signal_notifier with source="signal_notifier" + catalog_event=
        # the bespoke key, so per-event subscription gating works without
        # the producer ever calling dispatcher.send directly.
        "plugin_monitor",          # system.plugin_health_issue
        "exec_outcome_watchdog",   # system.exec_outcome_failure
        "stuck_proposal_monitor",  # system.stuck_proposal
        "session_cost_monitor",    # cost.session_budget_exceeded
        # v20 — app structural verifier (spec-app-coherence-and-
        # reconciliation-2026-06-05.md §17.3 + Q32). Findings are written
        # by audit_poller.py via signals.store.observe() — never via
        # dispatcher.send directly. Chat delivery flows through
        # signal_notifier with source="signal_notifier" and catalog_event=
        # system.app_scheduled_work_failure (for openclaw_cron_* assertions)
        # or security.audit_finding (for the rest).
        "app_structural_verifier",
        # Agent-invoked app-script failures (the "(agent) failed" exec chip):
        # app_script_failure_audit emits via signals.store.observe() only;
        # chat delivery flows through signal_notifier under the bespoke
        # system.app_script_failure catalog event. Spec: docs/spec-agent-
        # freelance-bypass-2026-06-05.md.
        "app_script_failure_audit",
        # U2.1 — proactive-delivery monitor (spec-proactive-delivery-
        # monitor-2026-06-10.md §10.1-2): emits via signals.store.observe()
        # only; chat delivery flows through signal_notifier under its two
        # bespoke catalog events (system.app_delivery_missed /
        # system.app_delivery_unmeasurable).
        "delivery_monitor",
        # U4.1 — autonomy ladder (spec-autonomy-ladder-2026-06-10.md §3.4):
        # the permission monitor emits via signals.store.observe() only;
        # chat delivery flows through signal_notifier under
        # security.autonomy_posture_drift / security.autonomy_review.
        "permission_monitor",
        # META:deploy C3 — OpenClaw surface drift-diff. update_watcher emits via
        # signals.store.observe() only; chat delivery flows through
        # signal_notifier under updates.openclaw_surface_drift. Never calls
        # dispatcher.send directly (update_watcher's *other* events — openclaw_
        # available/blocked etc. — use the dispatcher under source="update_watcher",
        # a distinct producer, so this Signal-only producer stays exempt here).
        "oc_surface_drift",
        # Digest-default classification (D4, spec-subscription-digest-default-
        # 2026-06-28): four producers reclassified out of the loud
        # meta.unclassified catch-all. Each emits via signals.store.observe()
        # only; chat delivery flows through signal_notifier under its new
        # DAILY_DIGEST catalog event — none calls dispatcher.send directly.
        "deploy_drift_monitor",   # updates.version_skew
        "session_economics",      # cost.session_economics
        "code_quality_monitor",   # meta.dev_health
        "cascade_audit",          # system.plugin_telemetry_silent
    })

    used_sources = {
        e.producer_source for e in cat.CATALOG
        if e.producer_source not in _SIGNAL_ONLY_PRODUCERS
    }
    unregistered_enabled = used_sources - set(_DEFAULT_SOURCE_ENABLED)
    unregistered_cooldown = used_sources - set(_DEFAULT_SOURCE_COOLDOWN_SECONDS)

    assert not unregistered_enabled, (
        f"catalog producer_sources missing from _DEFAULT_SOURCE_ENABLED: "
        f"{sorted(unregistered_enabled)}. Add them to dispatcher.py "
        f"with an explicit default. (If the producer is Signal-store-only "
        f"and doesn't call dispatcher.send directly, add it to "
        f"_SIGNAL_ONLY_PRODUCERS in this test instead.)"
    )
    assert not unregistered_cooldown, (
        f"catalog producer_sources missing from _DEFAULT_SOURCE_COOLDOWN_SECONDS: "
        f"{sorted(unregistered_cooldown)}. Add them with a cooldown "
        f"that matches the producer's dedup_key cadence (24h for daily-"
        f"keyed sources, lower for per-event-unique keys)."
    )


def test_cooldown_suppresses_same_dedup_key_within_window():
    """End-to-end behavioral test of the cost_watchdog fix: two sends
    with the same dedup_key within the source cooldown window — the
    second is suppressed (SUPPRESSED_COOLDOWN).

    Reproduces what the operator saw on 2026-05-24: the same
    cost_watchdog/heartbeat_session_bloat/security_bot/<sid>/<today> dedup_key
    fired every hour. After this fix, only the first send goes through;
    subsequent same-day sends suppress."""
    import tempfile as _tf
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch
    from evolve_admin.alerts import dispatcher as d

    with _tf.TemporaryDirectory() as tmp:
        shared = Path(tmp)
        network = {"alerts": {"channel": "telegram", "chatId": "12345"}}
        # Stub the actual chat send so the test doesn't try the network.
        with patch.object(d, "_dispatch_via_telegram_http",
                          return_value=(True, None)):
            t0 = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
            out1 = d.send(
                shared_dir=shared, network=network,
                source="cost_watchdog", message="first",
                dedup_key="cost_watchdog/bloat/security_bot/abc/2026-05-24",
                now=t0,
            )
            assert out1.result == d.DispatchResult.SENT

            # Same dedup_key, 1h later — well inside the 24h cost_watchdog cooldown.
            out2 = d.send(
                shared_dir=shared, network=network,
                source="cost_watchdog", message="second",
                dedup_key="cost_watchdog/bloat/security_bot/abc/2026-05-24",
                now=t0 + timedelta(hours=1),
            )
            assert out2.result == d.DispatchResult.SUPPRESSED_COOLDOWN

            # Different dedup_key (new day) — fresh window, allowed.
            out3 = d.send(
                shared_dir=shared, network=network,
                source="cost_watchdog", message="next day",
                dedup_key="cost_watchdog/bloat/security_bot/abc/2026-05-25",
                now=t0 + timedelta(days=1, hours=1),
            )
            assert out3.result == d.DispatchResult.SENT


def test_subprocess_call_pins_cwd_tmp(monkeypatch):
    """openclaw is Node; if a caller invokes the dispatcher from a
    directory the running user can't stat, Node aborts with EACCES
    before reaching main(). Pin that the subprocess.Popen sets cwd=/tmp.

    This protection used to live per-source (audit, pod_report, heal).
    Phase 3 consolidates it once in the dispatcher. The dispatcher
    switched from subprocess.run to subprocess.Popen so it can stream
    output and kill the process group on timeout — but the cwd pin
    still has to hold.
    """
    from evolve_admin.alerts import dispatcher as d
    import subprocess

    captured: dict = {}

    class FakeProc:
        pid = 12345
        stdout = None
        stderr = None

        def poll(self):
            self.returncode = 0
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def fake_popen(args, **kw):
        captured.update(kw)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    # killpg/getpgid call into the fake pid — stub them so the cleanup
    # path doesn't raise on a process that doesn't exist.
    import os as _os
    monkeypatch.setattr(_os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(_os, "killpg", lambda pgid, sig: None)
    # select.select would choke on the fake proc's None stdout/stderr;
    # return empty fds so the read loop falls through to proc.poll() and
    # exits cleanly.
    import select as _select
    monkeypatch.setattr(_select, "select", lambda r, w, x, t: ([], [], []))

    ok, err = d._dispatch_via_openclaw("telegram", "12345", "msg")
    assert ok is True and err is None
    assert captured.get("cwd") == "/tmp", (
        f"dispatcher subprocess.Popen must set cwd=/tmp; got cwd={captured.get('cwd')!r}"
    )


# ── Phase 2: schema lookup ─────────────────────────────────────────────────


def test_config_lookup_returns_stock_default_when_file_absent(tmp_path):
    """When the schema entry exists but no operator override is stored,
    lookup returns the schema's stock_default. Confirms Phase 1
    dispatcher reads through Phase 2 schema entries cleanly.
    """
    from evolve_admin.alerts._config_lookup import lookup

    # alerts.dispatcher_enabled has stock_default=True in the schema.
    # lookup must prefer the schema default over the fallback we pass.
    val = lookup(tmp_path, "alerts.dispatcher_enabled", default="bad-fallback")
    assert val is True

    # alerts.apply.cooldown_seconds has stock_default=0 — confirm falsy
    # values aren't accidentally treated as "not found" by the lookup
    # helper. (Previously this anchored on signal_notifier.enabled=False;
    # that key flipped default-on per Phase 7 of the alert-notifier spec.
    # Then on alerts.review.cooldown_seconds, until review.py's TunableKeys
    # retired 2026-08-14 with the reviewer itself.)
    val = lookup(tmp_path, "alerts.apply.cooldown_seconds", default=-1)
    assert val == 0

    # alerts.audit.cooldown_seconds has stock_default=86400 — confirm
    # int defaults round-trip and lookup doesn't coerce.
    val = lookup(tmp_path, "alerts.audit.cooldown_seconds", default=-1)
    assert val == 86400


def test_config_lookup_falls_back_for_unknown_path(tmp_path):
    """Defends against typos and pre-Phase-2 callers: an unknown schema
    path returns the caller's default rather than raising."""
    from evolve_admin.alerts._config_lookup import lookup

    val = lookup(tmp_path, "alerts.no_such_source.enabled", default="fallback")
    assert val == "fallback"


def test_dispatcher_compiled_defaults_match_schema():
    """Compile-time defaults in dispatcher.py must match the schema's
    stock_defaults. Drift here means operators see one default in the
    admin UI and a different one in the dispatcher's behavior."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "admin"))
    from evolve_admin.alerts import dispatcher
    from evolve_admin.config_sandbox.schema import SCHEMA

    by_path = {e.path: e for e in SCHEMA}

    assert by_path["alerts.dispatcher_enabled"].stock_default is dispatcher._DEFAULT_DISPATCHER_ENABLED

    for source, expected in dispatcher._DEFAULT_SOURCE_ENABLED.items():
        entry = by_path.get(f"alerts.{source}.enabled")
        assert entry is not None, f"missing schema entry for alerts.{source}.enabled"
        assert entry.stock_default == expected, (
            f"alerts.{source}.enabled drift — schema={entry.stock_default} "
            f"dispatcher={expected}"
        )

    for source, expected in dispatcher._DEFAULT_SOURCE_COOLDOWN_SECONDS.items():
        entry = by_path.get(f"alerts.{source}.cooldown_seconds")
        assert entry is not None, f"missing schema entry for alerts.{source}.cooldown_seconds"
        assert entry.stock_default == expected, (
            f"alerts.{source}.cooldown_seconds drift — schema={entry.stock_default} "
            f"dispatcher={expected}"
        )


# ── Phase A2: catalog-event gating ─────────────────────────────────────────


def test_catalog_event_default_subscription_passes_through(env):
    """Catalog defaults match what we want today (default_enabled=True for
    most events, IMMEDIATE frequency). A caller passing catalog_event for
    such an event sees the same SENT outcome as a non-catalog caller.

    The dispatcher also appends the standard "subscription:" footer when
    catalog_event is set — gives operators a grep anchor on the
    configure tab. Idempotent: catalog-rendered messages already carry
    the footer from render_event and won't double up (covered separately).
    """
    _force_immediate(env["shared"], "security.audit_finding")
    out = env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="hello",
        catalog_event="security.audit_finding",
    )
    assert out.result == env["dispatcher"].DispatchResult.SENT
    assert out.catalog_event == "security.audit_finding"
    assert env["sent"] == [
        ("telegram", "12345", "hello\n\nsubscription: security.audit_finding")
    ]


def test_dispatcher_footer_idempotent_when_message_already_has_one(env):
    """If the caller's message already contains "subscription: <key>"
    (because they went through render_event upstream), the dispatcher
    must not append a second copy."""
    _force_immediate(env["shared"], "security.audit_finding")
    pre_rendered = (
        "🛡️ Audit finding on team_bot_a\n\nsubscription: security.audit_finding"
    )
    out = env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message=pre_rendered,
        catalog_event="security.audit_finding",
    )
    assert out.result == env["dispatcher"].DispatchResult.SENT
    sent_msgs = [m for _, _, m in env["sent"]]
    assert sent_msgs == [pre_rendered]  # exact match — no doubling
    assert sent_msgs[0].count("subscription:") == 1


def test_dispatcher_footer_skipped_without_catalog_event(env):
    """Legacy callers that pass only ``message=`` (no catalog_event)
    have no subscription to link to — leave the message untouched."""
    out = env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="raw legacy message",
    )
    assert out.result == env["dispatcher"].DispatchResult.SENT
    assert env["sent"] == [("telegram", "12345", "raw legacy message")]


def test_catalog_event_unknown_logs_warning_but_proceeds(env):
    """Typo'd event keys must not block delivery — log a warning to the
    suppression file and fall through to source-level checks."""
    out = env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="hello",
        catalog_event="not.a.real.event",
    )
    assert out.result == env["dispatcher"].DispatchResult.SENT
    suppressed = _read_log(env["shared"], "dispatcher-suppressed.jsonl")
    warnings = [r for r in suppressed if r.get("result") == "warning_unknown_catalog_event"]
    assert len(warnings) == 1
    assert warnings[0]["catalog_event"] == "not.a.real.event"


def test_cve_finding_no_longer_logs_unknown_catalog_warning(env):
    """Bug 4 / R5: security_cve_scan dispatches
    catalog_event='security.cve_finding'. With the catalog entry + source
    registration in place, a real CVE send must SEND and must NOT write a
    warning_unknown_catalog_event row (the symptom that flagged the gap).
    Fail-open is preserved: the default subscription is enabled, so the
    finding still reaches the operator."""
    out = env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="security_cve_scan",
        message="🔴 Security finding — CVE-2026-12345",
        catalog_event="security.cve_finding",
        dedup_key="security_cve_scan/abc123",
    )
    assert out.result == env["dispatcher"].DispatchResult.SENT
    suppressed = _read_log(env["shared"], "dispatcher-suppressed.jsonl")
    cve_warnings = [
        r for r in suppressed
        if r.get("result") == "warning_unknown_catalog_event"
        and r.get("catalog_event") == "security.cve_finding"
    ]
    assert cve_warnings == []
    assert env["sent"] == [
        ("telegram", "12345",
         "🔴 Security finding — CVE-2026-12345"
         "\n\nsubscription: security.cve_finding"),
    ]


def test_subscription_group_default_off_blocks_send(env, monkeypatch):
    """Phase 2: the upstream_tracking GROUP defaults disabled (dev-profile).
    A caller passing one of its member events gets SUPPRESSED_DISABLED with
    a clear reason even though the source-level toggle is on — gating is
    now resolved through the event's Subscription group."""
    out = env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="update_watcher", message="upstream issue resolved",
        catalog_event="updates.upstream_issue_resolved",
    )
    assert out.result == env["dispatcher"].DispatchResult.SUPPRESSED_DISABLED
    assert "subscription_off:updates.upstream_issue_resolved" in (out.error or "")
    assert env["sent"] == []


def test_subscription_group_toggle_off_blocks_member_send(env, monkeypatch):
    """Disabling a group at the group level suppresses every member event."""
    from evolve_admin.alerts import subscriptions as _subs
    _subs.write_subscription_group(env["shared"], "cost_warnings", enabled=False)
    out = env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="spend_alert", message="admin_bot: $5.96 over $5.00",
        catalog_event="cost.daily_threshold",
        dedup_key="spend_alert/threshold/admin_bot/x",
    )
    assert out.result == env["dispatcher"].DispatchResult.SUPPRESSED_DISABLED
    assert "subscription_off:cost.daily_threshold" in (out.error or "")
    assert env["sent"] == []


def test_catalog_event_daily_digest_defers_to_queue(env, monkeypatch):
    """system.manifest_validation_failed defaults to DAILY_DIGEST. The
    dispatcher queues the event to digest-pending/daily.jsonl and
    returns DEFERRED."""
    import json as _json
    out = env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="validate", message="manifest validation failed: admin_bot",
        catalog_event="system.manifest_validation_failed",
    )
    assert out.result == env["dispatcher"].DispatchResult.DEFERRED
    assert out.catalog_event == "system.manifest_validation_failed"
    assert env["sent"] == []  # no chat push yet — flush daemon (TBD) drains
    queue = env["shared"] / "alerts" / "digest-pending" / "daily.jsonl"
    assert queue.exists()
    lines = [_json.loads(ln) for ln in queue.read_text().splitlines() if ln]
    assert len(lines) == 1
    assert lines[0]["catalog_event"] == "system.manifest_validation_failed"
    assert "manifest validation failed" in lines[0]["message"]
    # Audit log records the deferred outcome too.
    main = _read_log(env["shared"], "dispatcher.jsonl")
    assert main[-1]["result"] == "deferred"


def test_legacy_once_per_day_max_override_resolves_to_digest(env):
    """An operator's saved ``once_per_day_max`` override migrates to
    ``daily_digest`` at read time. The catalog-driven send path therefore
    treats it as a digest enqueue (returns DEFERRED), not a 24h cooldown
    floor.

    Pin reflects the Phase G vocabulary cleanup: ONCE_PER_DAY_MAX is a
    legacy enum value; new subscriptions cannot save it; existing saved
    values resolve to DAILY_DIGEST via LEGACY_FREQUENCY_MIGRATION.
    """
    from datetime import datetime, timezone
    from evolve_admin.alerts import subscriptions as subs
    from evolve_admin.alerts.catalog import Frequency
    d = env["dispatcher"]

    # Simulate a legacy on-disk override directly (write_subscription
    # rejects the legacy value, as it should — this drops it in raw to
    # mirror an older operator save).
    subs_path = env["shared"] / "alerts" / "subscriptions.json"
    subs_path.parent.mkdir(parents=True, exist_ok=True)
    subs_path.write_text(
        '{"version": 1, "subscriptions": '
        '{"cost.daily_threshold": {"enabled": true, "frequency": "once_per_day_max"}}}'
    )

    # read_subscription must migrate the value.
    resolved = subs.read_subscription(env["shared"], "cost.daily_threshold")
    assert resolved is not None
    assert resolved.frequency == Frequency.DAILY_DIGEST

    # And a send through the catalog routes to the digest enqueue (DEFERRED).
    t0 = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    out = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="spend_alert", message="first",
        dedup_key="spend_alert/threshold/admin_bot",
        catalog_event="cost.daily_threshold",
        now=t0,
    )
    assert out.result == d.DispatchResult.DEFERRED


def test_catalog_event_immediate_does_not_force_floor(env):
    """For frequency=IMMEDIATE, the catalog imposes no extra cooldown —
    the source's chosen cooldown stands."""
    from datetime import datetime, timedelta, timezone
    d = env["dispatcher"]
    t0 = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    # security.audit_finding allows IMMEDIATE; pin the operator override so
    # the floor test runs on the immediate path (the catalog default is now
    # daily_digest after workstream D1).
    _force_immediate(env["shared"], "security.audit_finding")
    out1 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="first",
        dedup_key="audit/team_bot_a/loopback",
        cooldown_seconds=60,    # short source cooldown
        catalog_event="security.audit_finding",
        now=t0,
    )
    out2 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="second",
        dedup_key="audit/team_bot_a/loopback",
        cooldown_seconds=60,
        catalog_event="security.audit_finding",
        now=t0 + timedelta(seconds=120),  # past the source cooldown
    )
    assert out1.result == d.DispatchResult.SENT
    assert out2.result == d.DispatchResult.SENT


def test_catalog_event_recorded_in_audit_log(env):
    """The dispatch audit log carries catalog_event so forensics can
    reconstruct which subscription gated each push."""
    env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="hello",
        catalog_event="security.audit_finding",
    )
    rec = _read_log(env["shared"], "dispatcher.jsonl")[-1]
    assert rec["catalog_event"] == "security.audit_finding"


# ── Phase F: catalog-side rendering via payload ────────────────────────────


def test_payload_with_catalog_event_renders_via_catalog(env):
    """When the caller passes payload + catalog_event, the dispatcher
    renders the message via the catalog's body_template + action. The
    operator's message comes from a single template, not source-side
    code — that's the whole Phase F refresh."""
    _force_immediate(env["shared"], "updates.openclaw_available")
    out = env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="update_watcher",
        catalog_event="updates.openclaw_available",
        payload={"new_version": "2026.5.7", "current_version": "2026.4.29"},
    )
    assert out.result == env["dispatcher"].DispatchResult.SENT
    _channel, _chat, sent_msg = env["sent"][0]
    # Body lines from the catalog body_template.
    assert "🔄 OpenClaw 2026.5.7 is available" in sent_msg
    assert "Passed the safe-upgrade preflight" in sent_msg
    # Catalog's action line — the "safe" variant points at the upgrade
    # wrapper directly (preflight already passed at watcher-side).
    # PR 2: command is wrapped in <code>...</code> for HTML parse mode.
    assert "Run: <code>sudo evolve-admin menu upgrade</code>" in sent_msg
    assert "npm install -g openclaw" not in sent_msg


def test_payload_missing_placeholder_falls_back_to_legacy_message(env):
    """If the payload is missing a placeholder the body_template
    requires, the dispatcher silently falls back to the caller's
    legacy ``message`` rather than crashing the dispatch. The
    subscription footer is still appended (catalog_event was set)."""
    _force_immediate(env["shared"], "updates.openclaw_available")
    out = env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="update_watcher",
        message="🔄 legacy fallback message",
        catalog_event="updates.openclaw_available",
        payload={"new_version": "2026.5.7"},   # missing current_version!
    )
    assert out.result == env["dispatcher"].DispatchResult.SENT
    _channel, _chat, sent_msg = env["sent"][0]
    assert sent_msg.startswith("🔄 legacy fallback message")
    assert sent_msg.endswith("subscription: updates.openclaw_available")


def test_payload_unknown_catalog_event_falls_back_to_legacy(env):
    """Typo'd event_key + payload — the catalog can't render so we
    fall back to the legacy message. (Unknown event_key already logs
    a warning to dispatcher-suppressed.jsonl in the Phase A2 path.)

    The dispatcher still appends the subscription footer because
    catalog_event was set; the operator can grep for the (typo'd)
    key to find where the alert came from.
    """
    out = env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="update_watcher",
        message="🔄 legacy fallback",
        catalog_event="not.a.real.event",
        payload={"new_version": "x"},
    )
    assert out.result == env["dispatcher"].DispatchResult.SENT
    _channel, _chat, sent_msg = env["sent"][0]
    assert sent_msg.startswith("🔄 legacy fallback")
    assert sent_msg.endswith("subscription: not.a.real.event")


def test_payload_no_message_no_render_fails_cleanly(env):
    """Edge case: caller passes payload only (no legacy message),
    catalog_event is unknown so rendering fails. We can't fall back
    — return FAILED with a clear error rather than crash."""
    out = env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="update_watcher",
        catalog_event="not.a.real.event",
        payload={"new_version": "x"},
    )
    assert out.result == env["dispatcher"].DispatchResult.FAILED
    assert "catalog render failed" in (out.error or "")
    # Nothing dispatched.
    assert env["sent"] == []


def test_legacy_message_still_works_without_payload(env):
    """Phase F is backwards-compatible: callers passing pre-rendered
    message (no payload) continue to work. Tested here to pin that
    Phase D-migrated sources don't regress when Phase F lands.

    The body stays untouched; the dispatcher appends the standard
    subscription footer because catalog_event is set.
    """
    _force_immediate(env["shared"], "security.audit_finding")
    out = env["dispatcher"].send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="legacy audit message",
        catalog_event="security.audit_finding",
    )
    assert out.result == env["dispatcher"].DispatchResult.SENT
    _channel, _chat, sent_msg = env["sent"][0]
    assert sent_msg.startswith("legacy audit message")
    assert sent_msg.endswith("subscription: security.audit_finding")


# ── Permanent vs transient failure classification ────────────────────────


def test_is_permanent_failure_classifies_http_4xx_as_permanent():
    """HTTP 4xx-class errors (parse error, bad chat_id, revoked token)
    are permanent — retrying the same message body fails the same way.
    signal_notifier reads this to avoid per-minute retry loops on
    un-sendable messages (real-world: 2026-05-21 catalog footer broke
    Telegram Markdown parsing → every minute, FAILED, no progress)."""
    from evolve_admin.alerts.dispatcher import _is_permanent_failure

    # Real error string format from _dispatch_via_telegram_http.
    assert _is_permanent_failure(
        "telegram http 400: {\"ok\":false,\"error_code\":400,"
        "\"description\":\"Bad Request: can't parse entities\"}"
    ) is True
    assert _is_permanent_failure("telegram http 401: Unauthorized") is True
    assert _is_permanent_failure("telegram http 403: Forbidden") is True
    assert _is_permanent_failure("telegram http 404: chat not found") is True


def test_is_permanent_failure_treats_5xx_and_network_as_transient():
    """5xx, network errors, and timeouts are transient — the operator's
    next tick gets to retry."""
    from evolve_admin.alerts.dispatcher import _is_permanent_failure

    assert _is_permanent_failure("telegram http 500: Internal Server Error") is False
    assert _is_permanent_failure("telegram http 502: Bad Gateway") is False
    assert _is_permanent_failure("telegram network error: timed out") is False
    assert _is_permanent_failure("telegram send error: connection reset") is False
    assert _is_permanent_failure(None) is False
    assert _is_permanent_failure("") is False


def test_failed_outcome_carries_permanent_classification(env, monkeypatch):
    """The dispatcher's FAILED outcome includes is_permanent_failure so
    callers (signal_notifier) can distinguish permanent vs transient
    without parsing error strings themselves."""
    # Force the telegram path to return a 400-class failure.
    d = env["dispatcher"]
    monkeypatch.setattr(d, "_dispatch_via_telegram_http",
                        lambda chat_id, message: (False, "telegram http 400: parse error"))
    monkeypatch.setattr(d, "_dispatch_via_openclaw",
                        lambda *a, **kw: (False, "telegram http 400: parse error"))

    _force_immediate(env["shared"], "security.audit_finding")
    out = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="signal_notifier", message="anything",
        catalog_event="security.audit_finding",
    )
    assert out.result == d.DispatchResult.FAILED
    assert out.is_permanent_failure is True


# ── Gateway-restart resilience: retry/backoff + dead-letter ────────────────


def test_is_transient_gateway_error_classifier():
    """Transient transport errors (gateway timeout / WS) are retryable;
    permanent failures and unknown errors are not."""
    from evolve_admin.alerts.dispatcher import _is_transient_gateway_error as t

    assert t("GatewayTransportError: gateway timeout after 10000ms") is True
    assert t("openclaw message send timed out after 60s") is True
    assert t("connection refused") is True
    assert t("openclaw binary not found on PATH") is True
    # Permanent failures are never transient (no point retrying).
    assert t("telegram http 400: bad parse") is False
    assert t("Telegram send failed: chat not found") is False
    # Unknown errors stay non-retryable so a novel permanent error can't loop.
    assert t("some unrecognized error") is False
    assert t(None) is False


def test_transient_gateway_error_retried_then_succeeds(env, monkeypatch):
    """A send that hits a gateway-restart window (transient timeout) is
    retried across the restart and eventually delivers — instead of the
    single-attempt FAILED that froze the Messages feed."""
    d = env["dispatcher"]
    # Force the gateway (openclaw) path: telegram-HTTP fast path unavailable.
    monkeypatch.setattr(d, "_dispatch_via_telegram_http",
                        lambda chat_id, message: (False, "no-telegram-token: test"))
    calls = {"n": 0}

    def flaky(channel, chat_id, message, gateway_port=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return False, "GatewayTransportError: gateway timeout after 10000ms"
        return True, None

    monkeypatch.setattr(d, "_dispatch_via_openclaw", flaky)
    slept: list = []
    monkeypatch.setattr(d, "_retry_sleep", lambda s: slept.append(s))

    out = d.send(shared_dir=env["shared"], network=env["network"],
                 source="audit", message="hello")
    assert out.result == d.DispatchResult.SENT
    assert calls["n"] == 3            # two transient fails, third succeeds
    assert len(slept) == 2            # backoff between the three attempts
    # Recorded as a normal SENT in the Messages feed — feed stays live.
    main = _read_log(env["shared"], "dispatcher.jsonl")
    assert main[-1]["result"] == "sent"


def test_permanent_failure_not_retried(env, monkeypatch):
    """A permanent failure (chat not found) is not retried — burning the
    backoff budget on a futile send is pure latency."""
    d = env["dispatcher"]
    monkeypatch.setattr(d, "_dispatch_via_telegram_http",
                        lambda chat_id, message: (False, "no-telegram-token: test"))
    calls = {"n": 0}

    def perm(channel, chat_id, message, gateway_port=None):
        calls["n"] += 1
        return False, "Telegram send failed: chat not found"

    monkeypatch.setattr(d, "_dispatch_via_openclaw", perm)
    slept: list = []
    monkeypatch.setattr(d, "_retry_sleep", lambda s: slept.append(s))

    out = d.send(shared_dir=env["shared"], network=env["network"],
                 source="audit", message="hello")
    assert out.result == d.DispatchResult.FAILED
    assert out.is_permanent_failure is True
    assert calls["n"] == 1            # no retries on a permanent failure
    assert slept == []


def test_permanent_failure_is_dead_lettered_and_surfaced(env, monkeypatch):
    """A permanently-undeliverable message is preserved in the dead-letter
    store (full body) AND surfaced in the Recent Messages feed as a
    dead_letter row — never silently dropped. The transport failure still
    lands on the Health panel (delivery-failures.jsonl)."""
    d = env["dispatcher"]
    monkeypatch.setattr(d, "_dispatch_via_telegram_http",
                        lambda chat_id, message: (False, "no-telegram-token: test"))
    monkeypatch.setattr(d, "_dispatch_via_openclaw",
                        lambda *a, **kw: (False, "Telegram send failed: chat not found"))

    out = d.send(shared_dir=env["shared"], network=env["network"],
                 source="audit", message="undeliverable body")
    assert out.result == d.DispatchResult.FAILED

    dead = _read_log(env["shared"], "dead-letter.jsonl")
    assert len(dead) == 1
    assert dead[0]["result"] == "dead_letter"
    assert dead[0]["message"] == "undeliverable body"   # FULL body preserved
    # Surfaced in the Messages feed as a terminal dead_letter row…
    main = _read_log(env["shared"], "dispatcher.jsonl")
    assert any(r.get("result") == "dead_letter" for r in main)
    # …and NOT mis-labelled as a plain sent/failed there.
    assert not any(r.get("result") == "failed" for r in main)
    # Health panel still gets the transport failure.
    failures = _read_log(env["shared"], "delivery-failures.jsonl")
    assert len(failures) == 1 and failures[0]["result"] == "failed"


def test_transient_failure_is_not_dead_lettered(env, monkeypatch):
    """A transient failure (retries exhausted this tick) is NOT dead-lettered
    — the producer's next tick / digest requeue retries it. It stays on the
    Health panel only, out of the Messages feed."""
    d = env["dispatcher"]
    monkeypatch.setattr(d, "_dispatch_via_telegram_http",
                        lambda chat_id, message: (False, "no-telegram-token: test"))
    monkeypatch.setattr(d, "_dispatch_via_openclaw",
                        lambda *a, **kw: (False, "GatewayTransportError: gateway timeout after 10000ms"))
    monkeypatch.setattr(d, "_retry_sleep", lambda s: None)

    out = d.send(shared_dir=env["shared"], network=env["network"],
                 source="audit", message="will retry later")
    assert out.result == d.DispatchResult.FAILED
    assert out.is_permanent_failure is False
    assert _read_log(env["shared"], "dead-letter.jsonl") == []
    main = _read_log(env["shared"], "dispatcher.jsonl")
    assert not any(r.get("result") in ("failed", "dead_letter") for r in main)
    failures = _read_log(env["shared"], "delivery-failures.jsonl")
    assert len(failures) == 1 and failures[0]["result"] == "failed"


def test_telegram_http_send_uses_html_parse_mode():
    """PR 2 of the dispatcher-safety rework re-introduced parse_mode,
    switching from legacy Markdown to HTML. HTML mode's special set is
    just ``<``, ``>``, ``&`` — well-defined open/close semantics, no
    ambiguous pairing. Producers (catalog.render_event,
    signal_notifier._render_*, etc.) escape interpolated values via
    ``catalog.html_escape``; literal template text in catalog
    body_template may contain raw HTML tags."""
    import inspect
    from evolve_admin.alerts import dispatcher as d
    src = inspect.getsource(d._dispatch_via_telegram_http)
    assert '"parse_mode": "HTML"' in src
    # Defense — confirm the legacy modes are NOT back.
    assert '"parse_mode": "Markdown"' not in src
    assert '"parse_mode": "MarkdownV2"' not in src


def test_neither_message_nor_payload_fails():
    """The dispatcher needs SOMETHING to send. Calling with neither
    message nor a renderable payload returns FAILED instead of
    crashing."""
    from evolve_admin.alerts import dispatcher as d
    import tempfile as _tf
    with _tf.TemporaryDirectory() as tmp:
        out = d.send(
            shared_dir=Path(tmp),
            network={"alerts": {"channel": "telegram", "chatId": "12345"}},
            source="audit",
            # no message, no payload
        )
        assert out.result == d.DispatchResult.FAILED
        assert "without message or renderable payload" in (out.error or "")


# ── HTML validation + dry-run (PR 3 of dispatcher-safety rework) ───────────


def test_validate_html_accepts_plain_text():
    """Plain text with no tags is valid HTML."""
    from evolve_admin.alerts.dispatcher import validate_html_message

    ok, err = validate_html_message("just plain text with emojis 🟢 and dots.")
    assert ok
    assert err is None


def test_validate_html_accepts_telegram_tags():
    """Telegram's supported tag set passes."""
    from evolve_admin.alerts.dispatcher import validate_html_message

    cases = [
        "<b>bold</b>",
        "<i>italic</i> and <code>code</code>",
        "Multi-line\n<pre>code block</pre>",
        "<blockquote>quote</blockquote>",
        "<b>nested <i>tags</i></b>",
        '<a href="https://example.com">link</a>',
        '<pre><code class="python">print(1)</code></pre>',
    ]
    for msg in cases:
        ok, err = validate_html_message(msg)
        assert ok, f"{msg!r} unexpectedly invalid: {err}"


def test_validate_html_rejects_unsupported_tags():
    """Tags Telegram doesn't accept (<script>, <div>, etc.) fail."""
    from evolve_admin.alerts.dispatcher import validate_html_message

    ok, err = validate_html_message("<script>alert(1)</script>")
    assert not ok
    assert "script" in (err or "")

    ok, err = validate_html_message("<div>content</div>")
    assert not ok
    assert "div" in (err or "")


def test_validate_html_rejects_unbalanced_tags():
    """Unclosed tags fail — same failure mode that broke legacy
    Markdown's unbalanced underscores, just caught earlier."""
    from evolve_admin.alerts.dispatcher import validate_html_message

    ok, err = validate_html_message("<b>never closed")
    assert not ok
    assert "<b>" in (err or "")

    ok, err = validate_html_message("</b>orphan close")
    assert not ok
    assert "</b>" in (err or "")

    ok, err = validate_html_message("<b><i>mismatched</b></i>")
    assert not ok


def test_dry_run_renders_catalog_event_without_sending():
    """dry_run returns the would-be-sent text + a validation verdict,
    skipping network / state / subscription checks. Useful for CI
    + ad-hoc operator debugging."""
    from evolve_admin.alerts import dispatcher as d

    result = d.dry_run(
        source="update_watcher",
        catalog_event="updates.openclaw_available",
        payload={"new_version": "2026.5.7", "current_version": "2026.4.29"},
    )
    assert result.ok, f"unexpected validation error: {result.error}"
    assert result.catalog_event == "updates.openclaw_available"
    # Final message should include the catalog body + action + footer.
    assert "🔄 OpenClaw 2026.5.7 is available" in result.message
    assert "<code>sudo evolve-admin menu upgrade</code>" in result.message
    assert result.message.endswith("subscription: updates.openclaw_available")


def test_dry_run_flags_invalid_html_in_caller_message():
    """When a producer passes ``message=`` directly with broken HTML,
    dry_run returns ok=False so CI can catch it."""
    from evolve_admin.alerts import dispatcher as d

    result = d.dry_run(source="audit", message="<b>unclosed bold")
    assert not result.ok
    assert "<b>" in (result.error or "")


def test_dry_run_flags_missing_message_and_payload():
    from evolve_admin.alerts import dispatcher as d

    result = d.dry_run(source="audit")
    assert not result.ok
    assert "no message or renderable payload" in (result.error or "")


def test_every_catalog_event_renders_valid_html():
    """Lint every catalog event's sample render against Telegram's
    HTML parse mode. Catches a future template change that introduces
    an unsupported tag or unbalanced markup BEFORE it ships and hits
    production at the per-minute signal_notifier cadence.

    This is the offline guardrail. The full real-Telegram round-trip
    lives in ``test_every_catalog_event_actually_sends_to_telegram``
    and is gated on a sandbox bot token (skipped without credentials).
    """
    from evolve_admin.alerts import catalog as cat
    from evolve_admin.alerts.dispatcher import dry_run

    failures: list[str] = []
    for event in cat.CATALOG:
        result = dry_run(
            source=event.producer_source,
            catalog_event=event.key,
            payload=event.sample_payload,
        )
        if not result.ok:
            failures.append(
                f"{event.key}: {result.error}\n---\n{result.message}\n---"
            )
    assert not failures, (
        f"{len(failures)} catalog events render invalid HTML:\n\n"
        + "\n\n".join(failures)
    )


# ── Real Telegram round-trip (gated on sandbox credentials) ────────────────
#
# Set TELEGRAM_SMOKE_TOKEN + TELEGRAM_SMOKE_CHAT_ID to enable this
# test in CI. The test sends every catalog event's sample render to
# the chat — confirms Telegram's actual parser accepts what our
# offline validator accepts. Catches edge cases (attribute parsing,
# entity quirks) that pure-Python validation misses.

@pytest.mark.skipif(
    not (
        os.environ.get("TELEGRAM_SMOKE_TOKEN")
        and os.environ.get("TELEGRAM_SMOKE_CHAT_ID")
    ),
    reason="real Telegram round-trip requires TELEGRAM_SMOKE_TOKEN + TELEGRAM_SMOKE_CHAT_ID env vars",
)
def test_every_catalog_event_actually_sends_to_telegram():
    """Real-Telegram smoke: render each catalog event's sample payload
    and POST it to a sandbox chat. Any HTTP 400 reveals a parse
    problem the offline validator missed."""
    import json as _json
    import urllib.request
    import urllib.error
    from evolve_admin.alerts import catalog as cat
    from evolve_admin.alerts.dispatcher import dry_run

    token = os.environ["TELEGRAM_SMOKE_TOKEN"]
    chat_id = os.environ["TELEGRAM_SMOKE_CHAT_ID"]
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"

    failures: list[str] = []
    for event in cat.CATALOG:
        result = dry_run(
            source=event.producer_source,
            catalog_event=event.key,
            payload=event.sample_payload,
        )
        # The offline validator should already have caught issues, but
        # if it did we still want the round-trip to confirm.
        assert result.ok, (
            f"offline validator rejected {event.key} — fix that first: {result.error}"
        )

        payload = _json.dumps({
            "chat_id": chat_id,
            "text": (
                f"[CI smoke {event.key}]\n\n" + result.message
            ),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            api_url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = _json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                failures.append(f"{event.key}: {body.get('description')}")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
            failures.append(f"{event.key}: HTTP {e.code} — {err_body}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{event.key}: {type(exc).__name__}: {exc}")

    assert not failures, (
        f"{len(failures)} catalog events failed the Telegram round-trip:\n"
        + "\n".join(failures)
    )


# ── Source-category framework (spec-alert-no-repeat-2026-06-01.md) ─────────
#
# Pins the falsifiable invariant: for any (source, dedup_key, body_hash)
# tuple, the dispatcher fires at most once per 24h unless the source
# is declared SourceCategory.STATE_TRACKED.


def test_every_source_has_a_declared_category():
    """Every source registered in ``_DEFAULT_SOURCE_ENABLED`` must also
    appear in ``_DEFAULT_SOURCE_CATEGORY``. Adding a new source without
    picking a category should fail CI — the category is the load-bearing
    declaration that anchors identical-content suppression.
    """
    from evolve_admin.alerts.dispatcher import (
        _DEFAULT_SOURCE_ENABLED, _DEFAULT_SOURCE_CATEGORY,
    )
    missing = set(_DEFAULT_SOURCE_ENABLED) - set(_DEFAULT_SOURCE_CATEGORY)
    assert not missing, (
        f"sources missing a category declaration: {sorted(missing)}. "
        f"Add to _DEFAULT_SOURCE_CATEGORY in dispatcher.py — pick "
        f"STATE_PERSISTS (condition persists across ticks), "
        f"PER_EVENT_UNIQUE (events naturally cycle), or "
        f"STATE_TRACKED (source owns its own dedup; requires comment "
        f"justification)."
    )


def test_category_default_cooldown_matches_compiled_table():
    """The derived ``_DEFAULT_SOURCE_COOLDOWN_SECONDS`` must equal the
    category's default cooldown for every source. Catches drift if a
    future PR hand-edits one without updating the other.
    """
    from evolve_admin.alerts.dispatcher import (
        _DEFAULT_SOURCE_CATEGORY, _DEFAULT_SOURCE_COOLDOWN_SECONDS,
        _CATEGORY_DEFAULT_COOLDOWN_SECONDS,
    )
    for source, category in _DEFAULT_SOURCE_CATEGORY.items():
        expected = _CATEGORY_DEFAULT_COOLDOWN_SECONDS[category]
        actual = _DEFAULT_SOURCE_COOLDOWN_SECONDS.get(source)
        assert actual == expected, (
            f"{source}: category={category.value} expects "
            f"cooldown={expected}, table has {actual}"
        )


def test_signal_notifier_is_the_only_state_tracked_source():
    """Pin that STATE_TRACKED stays a deliberate exception, not a
    convenience escape hatch. Adding a second STATE_TRACKED source
    requires updating this assertion plus a justifying comment on
    the new entry in ``_DEFAULT_SOURCE_CATEGORY``.

    The category exists for signal_notifier because it tracks
    ``alerted_for_signal_id`` per signature in ``notifier-state.json``
    and re-announces deliberately when audit cycles produce fresh
    Signal ids with the same signature. Anything new in the category
    needs to clear the same bar.
    """
    from evolve_admin.alerts.dispatcher import (
        _DEFAULT_SOURCE_CATEGORY, SourceCategory,
    )
    state_tracked = {
        s for s, c in _DEFAULT_SOURCE_CATEGORY.items()
        if c == SourceCategory.STATE_TRACKED
    }
    assert state_tracked == {"signal_notifier"}, (
        f"STATE_TRACKED set changed: {sorted(state_tracked)}. "
        f"Adding a STATE_TRACKED source opts it OUT of the 24h "
        f"identical-content floor, which is the spec's only safety "
        f"net against the 2026-05-31 gateway-spam pattern. "
        f"Update this test deliberately and add a justifying comment "
        f"on the new category entry."
    )


def test_soak_send_probe_source_registered_and_default_off():
    """[META:deploy] The canary soak send-probe gets its OWN dispatcher
    source, decoupled from the post-OC-upgrade ``send_surface_probe`` proof
    (which stays default-ON and operator-visible). The soak source is
    DEFAULT-OFF: a healthy soak must be silent. Registered in all three
    maps so the category + schema-drift tests stay green.
    """
    from evolve_admin.alerts.dispatcher import (
        _DEFAULT_SOURCE_ENABLED, _DEFAULT_SOURCE_CATEGORY,
        _DEFAULT_SOURCE_COOLDOWN_SECONDS, SourceCategory,
    )
    # Registered in every map.
    assert "soak_send_probe" in _DEFAULT_SOURCE_ENABLED
    assert "soak_send_probe" in _DEFAULT_SOURCE_CATEGORY
    assert "soak_send_probe" in _DEFAULT_SOURCE_COOLDOWN_SECONDS
    # Default-OFF — the operator sees nothing on a healthy soak.
    assert _DEFAULT_SOURCE_ENABLED["soak_send_probe"] is False
    # Distinct source from the post-OC-upgrade proof, which stays ON.
    assert _DEFAULT_SOURCE_ENABLED["send_surface_probe"] is True
    # Per-candidate events → PER_EVENT_UNIQUE (0s cooldown).
    assert _DEFAULT_SOURCE_CATEGORY["soak_send_probe"] is SourceCategory.PER_EVENT_UNIQUE
    assert _DEFAULT_SOURCE_COOLDOWN_SECONDS["soak_send_probe"] == 0


def test_identical_content_suppressed_within_24h_for_state_persists(env):
    """Regression test for the 2026-05-31 gateway-spam pattern: a heal-style
    STATE_PERSISTS source firing the same message every ~12 min while
    the underlying condition persists. Same (source, dedup_key, body)
    inside 24h → SUPPRESSED_IDENTICAL on the second attempt; past
    24h → resumes sending."""
    d = env["dispatcher"]
    t0 = datetime(2026, 5, 31, 18, 0, 0, tzinfo=timezone.utc)
    # heal is STATE_PERSISTS post-spec.
    out1 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="heal", message="🔴 team_bot_c's gateway is still down — auto-restart failed",
        dedup_key="heal/gateway_autorestart_failed/team_bot_c", now=t0,
    )
    assert out1.result == d.DispatchResult.SENT

    # 12 min later — what the operator actually saw on 2026-05-31.
    out2 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="heal", message="🔴 team_bot_c's gateway is still down — auto-restart failed",
        dedup_key="heal/gateway_autorestart_failed/team_bot_c",
        now=t0 + timedelta(minutes=12),
    )
    assert out2.result == d.DispatchResult.SUPPRESSED_IDENTICAL
    assert "identical_within_24h" in (out2.error or "")
    assert len(env["sent"]) == 1

    # Past 24h — identical-content floor lifts; cooldown also up.
    out3 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="heal", message="🔴 team_bot_c's gateway is still down — auto-restart failed",
        dedup_key="heal/gateway_autorestart_failed/team_bot_c",
        now=t0 + timedelta(hours=24, seconds=1),
    )
    assert out3.result == d.DispatchResult.SENT
    assert len(env["sent"]) == 2


def test_identical_content_floor_overrides_operator_short_cooldown(env):
    """An operator who sets ``alerts.heal.cooldown_seconds = 60`` (e.g.,
    to debug a flap) still doesn't see the same identical message every
    minute. The 24h identical-content floor wins. *Different* bodies
    with the same dedup_key still fire at the operator-set cadence.
    """
    d = env["dispatcher"]
    t0 = datetime(2026, 5, 31, 18, 0, 0, tzinfo=timezone.utc)
    out1 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="heal", message="identical body",
        dedup_key="heal/gateway/bot_x",
        cooldown_seconds=60,  # operator tightened
        now=t0,
    )
    assert out1.result == d.DispatchResult.SENT

    # 5 min later, same body → floor wins despite the 60s cooldown.
    out2 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="heal", message="identical body",
        dedup_key="heal/gateway/bot_x",
        cooldown_seconds=60,
        now=t0 + timedelta(minutes=5),
    )
    assert out2.result == d.DispatchResult.SUPPRESSED_IDENTICAL

    # 5 min later, *different* body → operator's 60s cooldown is the
    # only gate (and it's expired) → SENT.
    out3 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="heal", message="different body — new error code",
        dedup_key="heal/gateway/bot_x",
        cooldown_seconds=60,
        now=t0 + timedelta(minutes=10),
    )
    assert out3.result == d.DispatchResult.SENT
    assert len(env["sent"]) == 2


def test_state_tracked_source_skips_identical_content_floor(env):
    """signal_notifier is STATE_TRACKED — same (dedup_key, body) inside
    24h is allowed because the source owns dedup elsewhere
    (alerted_for_signal_id tracking in notifier-state.json). The
    dispatcher's cooldown is the only gate here.

    This pins that the spec doesn't accidentally break signal_notifier's
    legitimate re-announce-on-fresh-Signal-id behavior."""
    d = env["dispatcher"]
    t0 = datetime(2026, 5, 31, 18, 0, 0, tzinfo=timezone.utc)
    sig = "pod_health:gateway:team_bot_a"
    body = "🔴 team_bot_a: Gateway probe failed"

    out1 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="signal_notifier", message=body,
        dedup_key=sig, cooldown_seconds=600,
        now=t0,
    )
    assert out1.result == d.DispatchResult.SENT

    # Past the 600s cooldown but well inside 24h — would be
    # SUPPRESSED_IDENTICAL for a STATE_PERSISTS source. Here it
    # passes because signal_notifier is STATE_TRACKED.
    out2 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="signal_notifier", message=body,
        dedup_key=sig, cooldown_seconds=600,
        now=t0 + timedelta(seconds=601),
    )
    assert out2.result == d.DispatchResult.SENT
    assert len(env["sent"]) == 2


def test_recovery_announcement_bypasses_identical_floor(env):
    """``dedup_key=None`` is the signal_notifier recovery path — push
    immediately with no dedup tracking. The floor must not silence
    "🟢 X is back up" messages even when the body happens to match a
    prior alert (unlikely but possible)."""
    d = env["dispatcher"]
    t0 = datetime(2026, 5, 31, 18, 0, 0, tzinfo=timezone.utc)
    msg = "🟢 team_bot_c: gateway back up"

    # Three identical recovery messages within 24h — all sent.
    for offset in (0, 60, 3600):
        out = d.send(
            shared_dir=env["shared"], network=env["network"],
            source="signal_notifier", message=msg,
            dedup_key=None,
            now=t0 + timedelta(seconds=offset),
        )
        assert out.result == d.DispatchResult.SENT
    assert len(env["sent"]) == 3


def test_body_hash_change_clears_identical_suppression(env):
    """Same dedup_key, *different* body, inside 24h → no
    SUPPRESSED_IDENTICAL. Cooldown still applies if configured.
    Pins that the suppression is content-keyed, not key-only."""
    d = env["dispatcher"]
    t0 = datetime(2026, 5, 31, 18, 0, 0, tzinfo=timezone.utc)

    out1 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="heal", message="error A",
        dedup_key="heal/gateway/bot_x",
        cooldown_seconds=60,
        now=t0,
    )
    assert out1.result == d.DispatchResult.SENT

    # Different body, past cooldown → SENT (not SUPPRESSED_IDENTICAL).
    out2 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="heal", message="error B (new symptom)",
        dedup_key="heal/gateway/bot_x",
        cooldown_seconds=60,
        now=t0 + timedelta(seconds=61),
    )
    assert out2.result == d.DispatchResult.SENT
    assert len(env["sent"]) == 2


def test_legacy_state_file_without_body_hash_does_not_suppress(env):
    """Pod mid-upgrade: ``dispatcher-state.json`` contains entries
    written by the pre-spec code, so they have no ``body_hash`` field.
    A new send with matching dedup_key must proceed (no
    SUPPRESSED_IDENTICAL) — failing open so an upgrade doesn't silence
    alerts. Cooldown still gates as before."""
    d = env["dispatcher"]
    t0 = datetime(2026, 5, 31, 18, 0, 0, tzinfo=timezone.utc)

    # Hand-write a legacy state entry (no body_hash).
    state_dir = env["shared"] / "alerts"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "dispatcher-state.json"
    state_path.write_text(json.dumps({
        "version": 1,
        "last_dispatch": {
            "heal::heal/legacy_entry": {
                "ts": (t0 - timedelta(hours=1)).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "result": "sent",
                # NOTE: no body_hash field — pre-spec writer
            }
        }
    }))

    # Send with identical body to what a legacy hash would have been —
    # must NOT be SUPPRESSED_IDENTICAL (failing open is the spec).
    # Use cooldown=0 to isolate the identical-content path.
    out = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="heal", message="identical body",
        dedup_key="heal/legacy_entry",
        cooldown_seconds=0,
        now=t0,
    )
    assert out.result == d.DispatchResult.SENT


def test_failed_dispatch_records_body_hash_for_retry_suppression(env):
    """A FAILED attempt records body_hash too, so a permanently-failing
    identical message can't bypass the 24h floor by virtue of never
    having succeeded. The same defensive logic the cooldown already
    uses for FAILED, extended to the identical-content check."""
    d = env["dispatcher"]
    env["next_result"]["ok"] = False
    env["next_result"]["error"] = "telegram http 400: bad parse"
    t0 = datetime(2026, 5, 31, 18, 0, 0, tzinfo=timezone.utc)

    out1 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="heal", message="identical broken body",
        dedup_key="heal/gateway/bot_x",
        cooldown_seconds=0,
        now=t0,
    )
    assert out1.result == d.DispatchResult.FAILED

    # Subprocess "recovers"; an identical body within 24h still gets
    # suppressed by the floor, even though the prior attempt failed.
    env["next_result"]["ok"] = True
    out2 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="heal", message="identical broken body",
        dedup_key="heal/gateway/bot_x",
        cooldown_seconds=0,
        now=t0 + timedelta(minutes=1),
    )
    assert out2.result == d.DispatchResult.SUPPRESSED_IDENTICAL
    # And no successful chat send happened — only the failed first attempt.
    assert env["sent"] == []


def test_per_event_unique_source_safety_net(env):
    """PER_EVENT_UNIQUE sources have dedup_keys that should naturally
    cycle per event — so the 24h floor is essentially a no-op in
    practice. But if a producer bug causes the same (dedup_key, body)
    to repeat, the floor catches it.

    This pins that the safety net actually works, not just that the
    happy-path categorization is correct."""
    d = env["dispatcher"]
    t0 = datetime(2026, 5, 31, 18, 0, 0, tzinfo=timezone.utc)
    out1 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="forge_engine", message="duplicate body",
        dedup_key="forge/job-42",  # would normally never repeat
        now=t0,
    )
    out2 = d.send(
        shared_dir=env["shared"], network=env["network"],
        source="forge_engine", message="duplicate body",
        dedup_key="forge/job-42",
        now=t0 + timedelta(hours=12),
    )
    assert out1.result == d.DispatchResult.SENT
    assert out2.result == d.DispatchResult.SUPPRESSED_IDENTICAL


def test_suppressed_identical_routes_to_suppressed_log(env):
    """SUPPRESSED_IDENTICAL outcomes land in dispatcher-suppressed.jsonl,
    not the main feed and not the delivery-failures lane. Matches the
    routing of other suppression results (cooldown / disabled)."""
    d = env["dispatcher"]
    t0 = datetime(2026, 5, 31, 18, 0, 0, tzinfo=timezone.utc)
    d.send(
        shared_dir=env["shared"], network=env["network"],
        source="heal", message="same body",
        dedup_key="heal/gateway/bot_x",
        now=t0,
    )
    d.send(
        shared_dir=env["shared"], network=env["network"],
        source="heal", message="same body",
        dedup_key="heal/gateway/bot_x",
        now=t0 + timedelta(minutes=5),
    )
    main = _read_log(env["shared"], "dispatcher.jsonl")
    failures = _read_log(env["shared"], "delivery-failures.jsonl")
    suppressed = _read_log(env["shared"], "dispatcher-suppressed.jsonl")
    assert len(main) == 1 and main[0]["result"] == "sent"
    assert failures == []
    identical = [r for r in suppressed if r["result"] == "suppressed_identical"]
    assert len(identical) == 1
    assert identical[0]["source"] == "heal"
    assert identical[0]["dedup_key"] == "heal/gateway/bot_x"


def test_state_file_persists_body_hash(env):
    """The state file must round-trip body_hash so the floor survives
    a fresh dispatcher import. Otherwise the in-memory check would
    work but the on-disk state would silently lose its content key
    after every Python process restart."""
    d = env["dispatcher"]
    t0 = datetime(2026, 5, 31, 18, 0, 0, tzinfo=timezone.utc)
    d.send(
        shared_dir=env["shared"], network=env["network"],
        source="heal", message="round-trip body",
        dedup_key="heal/state_persist_check",
        now=t0,
    )
    state = json.loads(
        (env["shared"] / "alerts" / "dispatcher-state.json").read_text()
    )
    entry = state["last_dispatch"]["heal::heal/state_persist_check"]
    assert "body_hash" in entry
    assert len(entry["body_hash"]) == 16   # truncated sha256 prefix


def test_principle_holds_for_every_state_persists_source(env):
    """Falsifiable assertion swept across every declared STATE_PERSISTS
    source: identical (dedup_key, body) within 24h → SUPPRESSED_IDENTICAL.

    Strongest guarantee in the spec — if any STATE_PERSISTS source
    repeats identical content inside the floor, this fails."""
    from evolve_admin.alerts.dispatcher import (
        _DEFAULT_SOURCE_CATEGORY, SourceCategory,
    )
    d = env["dispatcher"]
    t0 = datetime(2026, 5, 31, 18, 0, 0, tzinfo=timezone.utc)
    state_persists_sources = [
        s for s, c in _DEFAULT_SOURCE_CATEGORY.items()
        if c == SourceCategory.STATE_PERSISTS
    ]
    # Sanity: the spec lists at least the original noisy sources here.
    for must_include in ("heal", "audit", "spend_alert", "cost_watchdog"):
        assert must_include in state_persists_sources

    for i, source in enumerate(state_persists_sources):
        # Each source uses an isolated dedup_key so they don't shadow each other.
        dk = f"{source}/principle_test/marker_{i}"
        body = f"identical body for {source}"

        # Stagger the source pairs across the simulated day so different
        # source ts entries don't share clock state in confusing ways.
        ts_base = t0 + timedelta(seconds=i * 10)
        out1 = d.send(
            shared_dir=env["shared"], network=env["network"],
            source=source, message=body,
            dedup_key=dk, now=ts_base,
        )
        out2 = d.send(
            shared_dir=env["shared"], network=env["network"],
            source=source, message=body,
            dedup_key=dk, now=ts_base + timedelta(hours=12),
        )
        assert out1.result == d.DispatchResult.SENT, (
            f"{source}: first send blocked unexpectedly ({out1.result.value})"
        )
        assert out2.result == d.DispatchResult.SUPPRESSED_IDENTICAL, (
            f"{source}: STATE_PERSISTS principle violated — identical "
            f"(dedup_key, body) at 12h was {out2.result.value}, expected "
            f"SUPPRESSED_IDENTICAL"
        )


# ── Date-partitioned log rotation (2026-06-01) ───────────────────────────────


def test_log_writes_land_in_date_partitioned_dir(env):
    """Successful sends append to ``alerts/dispatcher/<YYYY-MM-DD>.jsonl``,
    not the flat ``alerts/dispatcher.jsonl``. Anchored to the 2026-06-01
    disk-fillup incident: the previously-flat suppressed log reached
    306 MB / 803k lines in 22 days. Date partitioning makes a
    retention sweep possible."""
    d = env["dispatcher"]
    t0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="rotation check",
        now=t0,
    )
    day_file = env["shared"] / "alerts" / "dispatcher" / "2026-06-01.jsonl"
    flat_file = env["shared"] / "alerts" / "dispatcher.jsonl"
    assert day_file.is_file(), "expected date-partitioned daily file"
    assert not flat_file.exists(), (
        "dispatcher must not write to the legacy flat path"
    )


def test_log_writes_partition_by_event_day(env):
    """Two sends on different days land in their own daily files —
    proves the day comes from ``record["ts"]``, not wall-clock at flush."""
    d = env["dispatcher"]
    d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="day-A",
        now=datetime(2026, 5, 31, 23, 30, 0, tzinfo=timezone.utc),
    )
    d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="day-B",
        now=datetime(2026, 6, 1, 0, 30, 0, tzinfo=timezone.utc),
    )
    day_a = env["shared"] / "alerts" / "dispatcher" / "2026-05-31.jsonl"
    day_b = env["shared"] / "alerts" / "dispatcher" / "2026-06-01.jsonl"
    assert day_a.is_file() and day_b.is_file()
    assert "day-A" in day_a.read_text()
    assert "day-B" in day_b.read_text()


def test_suppressed_writes_partition_separately(env, monkeypatch):
    """Suppressed records land under ``alerts/dispatcher-suppressed/``
    not ``alerts/dispatcher/``. Pins the routing across log lanes —
    the 2026-06-01 incident was suppressed-stream only."""
    d = env["dispatcher"]
    monkeypatch.setattr(d, "_read_source_enabled", lambda _sd, _src: False)
    d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="should be suppressed",
        now=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    sup_dir = env["shared"] / "alerts" / "dispatcher-suppressed"
    main_dir = env["shared"] / "alerts" / "dispatcher"
    assert (sup_dir / "2026-06-01.jsonl").is_file()
    assert not main_dir.exists() or not any(main_dir.iterdir())


def test_digest_queue_files_stay_flat(env, monkeypatch):
    """``digest-pending/daily.jsonl`` and ``weekly.jsonl`` are queues
    drained by the digest_dispatcher daemon — they must NOT be
    date-partitioned (the daemon reads one canonical path)."""
    d = env["dispatcher"]
    # Force the catalog subscription to return daily_digest so the
    # dispatcher routes into the digest queue without a real catalog.
    monkeypatch.setattr(
        d, "_resolve_catalog_subscription",
        lambda _sd, _ev: (True, "daily_digest"),
    )
    d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="digest me",
        catalog_event="security.audit_finding",
        dedup_key="audit/digest-test",
        now=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    daily_queue = env["shared"] / "alerts" / "digest-pending" / "daily.jsonl"
    assert daily_queue.is_file()


def test_iter_log_records_yields_newest_day_first(env):
    """``iter_log_records`` returns records from newest day file first
    so a caller taking the first N entries gets the most recent N."""
    d = env["dispatcher"]
    d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="older",
        now=datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="newest",
        now=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    records = list(d.iter_log_records(env["shared"], "dispatcher.jsonl"))
    assert records[0]["message_excerpt"] == "newest"
    assert records[-1]["message_excerpt"] == "older"


def test_iter_log_records_includes_legacy_flat_file(env, tmp_path):
    """Pre-rotation deployments have a non-empty flat file. Readers
    must surface it during the transition so operators don't lose
    their dispatcher history overnight."""
    d = env["dispatcher"]
    alerts_dir = env["shared"] / "alerts"
    alerts_dir.mkdir(exist_ok=True)
    flat = alerts_dir / "dispatcher.jsonl"
    flat.write_text(
        json.dumps({
            "ts": "2026-05-15T10:00:00Z",
            "source": "audit",
            "result": "sent",
            "message_excerpt": "legacy-record",
        }) + "\n",
        encoding="utf-8",
    )
    # And a fresh date-partitioned record after the rotation lands.
    d.send(
        shared_dir=env["shared"], network=env["network"],
        source="audit", message="post-rotation",
        now=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    excerpts = {
        r.get("message_excerpt")
        for r in d.iter_log_records(env["shared"], "dispatcher.jsonl")
    }
    assert "legacy-record" in excerpts
    assert "post-rotation" in excerpts


# ── Permanent-failure classification (#3152: openclaw-path "chat not found") ──


def _classify(error):
    """Return (is_permanent, is_target_down) for a raw dispatch error string."""
    from evolve_admin.alerts import dispatcher
    return (
        dispatcher._is_permanent_failure(error),
        dispatcher._is_delivery_target_down(error),
    )


def test_openclaw_chat_not_found_classifies_permanent_and_target_down():
    """The live #3152 shape: the openclaw CLI path returns an unstructured
    error with NO "http 4" substring. Before the fix this was treated as
    transient and retried every 60s for hours, flooding on recovery. It
    must now classify permanent AND target-down."""
    err = (
        "Telegram send failed: chat not found (chat_id=-1002…). Likely: bot "
        "not started in DM, bot removed from group/channel, group migrated "
        "(new -100… id), or wrong bot token."
    )
    is_perm, is_down = _classify(err)
    assert is_perm is True
    assert is_down is True


def test_telegram_api_error_chat_not_found_is_permanent_under_http_200():
    """Telegram's sendMessage returns ok=false under HTTP 200 for chat not
    found, so the api-error string carries no "http 4" code. The explicit
    allowlist still catches it."""
    err = "telegram api error: Bad Request: chat not found"
    is_perm, is_down = _classify(err)
    assert is_perm is True
    assert is_down is True


def test_blocked_kicked_deactivated_token_shapes_are_target_down():
    for err in (
        "Telegram send failed: Forbidden: bot was blocked by the user",
        "Forbidden: bot was kicked from the supergroup chat",
        "Forbidden: user is deactivated",
        "Unauthorized",
        "error: invalid token",
        "Likely: wrong bot token",
    ):
        is_perm, is_down = _classify(err)
        assert is_perm is True, err
        assert is_down is True, err


def test_http_4xx_is_permanent_but_not_necessarily_target_down():
    """A bare HTTP 4xx (e.g. a 400 parse error on the message body) is
    permanent — retrying the same body won't help — but it is NOT a
    channel outage, so it must not trip the digest-on-recovery path."""
    err = "telegram http 400: can't parse entities: unexpected end tag"
    is_perm, is_down = _classify(err)
    assert is_perm is True
    assert is_down is False


def test_unknown_cli_error_stays_transient():
    """Conservative default: an UNKNOWN openclaw error stays transient so a
    recoverable blip is not wrongly suppressed as permanent."""
    for err in (
        "openclaw message send timed out after 60s",
        "telegram network error: [Errno 60] Operation timed out",
        "telegram http 503: service unavailable",
        "openclaw exit 1",
        "gateway connection refused",
    ):
        is_perm, is_down = _classify(err)
        assert is_perm is False, err
        assert is_down is False, err


def test_none_and_empty_error_is_not_permanent():
    assert _classify(None) == (False, False)
    assert _classify("") == (False, False)


def test_classification_is_case_insensitive():
    is_perm, is_down = _classify("TELEGRAM SEND FAILED: CHAT NOT FOUND")
    assert is_perm is True
    assert is_down is True


# ── iter_log_records_recent — tail reader for the Messages feed ──────────────
#
# Storm-day fix: iter_log_records walks each day file newest-day-first but
# from the START, so a caller that takes the first N gets the OLDEST N of the
# newest day. iter_log_records_recent reads the TAIL of each day file so the
# collected set is the most recent.


def _write_day_file(shared, basename, day, records):
    p = shared / "alerts" / basename / f"{day}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_recent_reader_returns_tail_not_head(env):
    from evolve_admin.alerts.dispatcher import (
        iter_log_records,
        iter_log_records_recent,
    )

    shared = env["shared"]
    day = "2099-04-01"
    recs = [
        {"ts": f"{day}T{i // 60 % 24:02d}:{i % 60:02d}:00Z",
         "result": "sent", "n": i}
        for i in range(500)
    ]
    _write_day_file(shared, "dispatcher", day, recs)

    # Head reader (old behavior): first 50 == earliest 50.
    head = list(iter_log_records(shared, "dispatcher.jsonl"))[:50]
    assert [r["n"] for r in head] == list(range(50))

    # Recent reader: the 50 collected are the LAST 50 written.
    recent = list(
        iter_log_records_recent(shared, "dispatcher.jsonl", max_records=50)
    )
    assert len(recent) == 50
    assert {r["n"] for r in recent} == set(range(450, 500))


def test_recent_reader_spans_day_files_newest_first(env):
    from evolve_admin.alerts.dispatcher import iter_log_records_recent

    shared = env["shared"]
    _write_day_file(
        shared, "dispatcher", "2099-04-01",
        [{"ts": "2099-04-01T10:00:00Z", "result": "sent", "n": i}
         for i in range(10)],
    )
    _write_day_file(
        shared, "dispatcher", "2099-04-02",
        [{"ts": "2099-04-02T10:00:00Z", "result": "sent", "n": 100 + i}
         for i in range(3)],
    )
    # Ask for 5: the 3 from the newest day, then the tail 2 of the older day.
    recent = list(
        iter_log_records_recent(shared, "dispatcher.jsonl", max_records=5)
    )
    assert len(recent) == 5
    ns = [r["n"] for r in recent]
    # Newest day first (100,101,102), then the TAIL of the older day (8,9).
    assert ns[:3] == [100, 101, 102]
    assert set(ns[3:]) == {8, 9}


def test_recent_reader_max_records_zero_yields_nothing(env):
    from evolve_admin.alerts.dispatcher import iter_log_records_recent

    shared = env["shared"]
    _write_day_file(
        shared, "dispatcher", "2099-04-01",
        [{"ts": "2099-04-01T10:00:00Z", "result": "sent", "n": 0}],
    )
    assert list(
        iter_log_records_recent(shared, "dispatcher.jsonl", max_records=0)
    ) == []


def test_recent_reader_reads_legacy_flat_file(env):
    from evolve_admin.alerts.dispatcher import iter_log_records_recent

    shared = env["shared"]
    flat = shared / "alerts" / "dispatcher.jsonl"
    flat.parent.mkdir(parents=True, exist_ok=True)
    with flat.open("w", encoding="utf-8") as f:
        for i in range(5):
            f.write(json.dumps(
                {"ts": f"2099-04-01T0{i}:00:00Z", "result": "sent", "n": i}
            ) + "\n")
    recent = list(
        iter_log_records_recent(shared, "dispatcher.jsonl", max_records=3)
    )
    # Tail of the legacy flat file.
    assert {r["n"] for r in recent} == {2, 3, 4}
