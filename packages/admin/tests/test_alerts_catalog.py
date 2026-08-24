"""Phase A1 — alert subscription catalog.

The catalog is the single source of truth for what operator-facing
events the Evolve install can notify about. Tests pin:

  - integrity invariants (unique keys, key-prefix matches category,
    Frequency.OFF always allowed, default frequency in allowed set,
    safety-critical events have severity ≥ warning)
  - the renderer formats body templates and conditionally appends
    action lines for each of the three modes (bot / ui / cli)
  - the catalog covers every emitter the spec inventory found, and
    none of its producer_sources are typos
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.alerts import catalog as cat  # noqa: E402
from evolve_admin.alerts.catalog import (  # noqa: E402
    ActionOffer, Category, Frequency,
    bot_action, cli_action, ui_action, render_event,
)
from evolve_admin.alerts.dispatcher import Severity  # noqa: E402


# ── Integrity invariants ────────────────────────────────────────────────────


def test_keys_are_unique():
    keys = [e.key for e in cat.CATALOG]
    assert len(keys) == len(set(keys)), (
        f"duplicate keys: {[k for k in keys if keys.count(k) > 1]}"
    )


def test_key_starts_with_category_value():
    """The key prefix encodes the category — `cost.daily_threshold` etc.
    Tested at __post_init__ already, but pin the contract here too."""
    for e in cat.CATALOG:
        prefix = f"{e.category.value}."
        assert e.key.startswith(prefix), (
            f"event {e.key!r} category={e.category.value} key prefix mismatch"
        )


def test_default_frequency_is_in_allowed():
    for e in cat.CATALOG:
        assert e.default_frequency in e.allowed_frequencies, (
            f"event {e.key}: default {e.default_frequency} not in {e.allowed_frequencies}"
        )


def test_off_and_deprecated_frequencies_are_not_allowed():
    """Mute goes through the enabled flag; OFF / ONCE_PER_*_MAX are
    deprecated and must never appear in allowed_frequencies.

    Was previously the inverse check (OFF must be present); flipped
    when the frequency vocabulary was tightened — see PR for Phase G."""
    deprecated = {
        Frequency.OFF, Frequency.ONCE_PER_DAY_MAX, Frequency.ONCE_PER_WEEK_MAX,
    }
    for e in cat.CATALOG:
        bad = deprecated & set(e.allowed_frequencies)
        assert not bad, (
            f"event {e.key} lists deprecated frequencies in "
            f"allowed_frequencies: {sorted(f.value for f in bad)}"
        )


def test_safety_critical_events_have_meaningful_severity():
    """is_safety_critical implies the event is something the operator
    really shouldn't blindly mute. Pin that we only flag events whose
    severity warrants the warning."""
    for e in cat.CATALOG:
        if e.is_safety_critical:
            assert e.severity in (Severity.CRITICAL, Severity.ERROR, Severity.WARNING), (
                f"safety-critical event {e.key} has severity={e.severity} — should be ≥ warning"
            )


def test_all_categories_have_at_least_one_event():
    """Every Category in the enum must show up in the catalog,
    otherwise we have a UI section with nothing in it."""
    used = {e.category for e in cat.CATALOG}
    for c in Category:
        assert c in used, f"Category.{c.name} has no events in CATALOG"


def test_categories_match_display_order():
    assert set(cat.CATEGORY_DISPLAY_ORDER) == set(Category), (
        "CATEGORY_DISPLAY_ORDER must list every Category exactly once"
    )
    assert len(cat.CATEGORY_DISPLAY_ORDER) == len(set(cat.CATEGORY_DISPLAY_ORDER))


def test_every_category_has_a_label():
    for c in Category:
        assert c in cat.CATEGORY_LABELS, f"Category.{c.name} missing label"
        emoji, name = cat.CATEGORY_LABELS[c]
        assert emoji and name


def test_every_frequency_has_a_label():
    for f in Frequency:
        assert f in cat.FREQUENCY_LABELS, f"Frequency.{f.name} missing label"


# ── ActionOffer construction ────────────────────────────────────────────────


def test_action_modes_require_their_field():
    with pytest.raises(ValueError, match="bot_command"):
        ActionOffer(mode="bot", text_template="x")
    with pytest.raises(ValueError, match="ui_breadcrumb"):
        ActionOffer(mode="ui", text_template="x")
    with pytest.raises(ValueError, match="cli_command"):
        ActionOffer(mode="cli", text_template="x")
    with pytest.raises(ValueError, match="bot/ui/cli"):
        ActionOffer(mode="other", text_template="x", bot_command="y")


def test_helper_constructors_use_defaults():
    """bot_action/ui_action/cli_action should use the canonical text
    templates so most catalog entries don't have to repeat them."""
    b = bot_action("update openclaw")
    assert b.mode == "bot"
    assert b.bot_command == "update openclaw"
    assert "I'll take care of it" in b.text_template

    u = ui_action("Cost")
    assert u.mode == "ui"
    assert u.ui_breadcrumb == "Cost"
    assert "Open admin UI" in u.text_template

    c = cli_action("ls /tmp")
    assert c.mode == "cli"
    assert c.cli_command == "ls /tmp"
    assert "Run:" in c.text_template


# ── Renderer ────────────────────────────────────────────────────────────────


def test_render_event_no_action_returns_body_only():
    """Informational events (action=None) render to the body, no action line."""
    e = cat.by_key("cost.weekly_summary")
    assert e is not None
    out = render_event(e, {
        "iso_week": "2026-W19",
        "total": 42.50,
        "per_bot_breakdown": "admin_bot: $20  team_bot_a: $22.50",
    })
    assert "Weekly spend (2026-W19)" in out
    assert "Total: $42.50" in out
    assert "admin_bot: $20" in out
    # No action line.
    assert "Open admin UI" not in out
    assert "Run:" not in out
    assert "Reply" not in out


def test_render_event_ui_action():
    e = cat.by_key("cost.daily_threshold")
    assert e is not None
    out = render_event(e, {
        "bot_id": "admin_bot",
        "amount": 5.96,
        "threshold": 5.00,
    })
    assert "admin_bot's daily spend hit $5.96" in out
    assert "Threshold: $5.00" in out
    # Action line is still present (just no longer at the very end now
    # that the subscription footer follows it).
    assert "Open admin UI → Cost." in out


def test_render_event_appends_subscription_footer():
    """Every rendered event ends with a "subscription: <key>" footer
    so operators can grep the configure page for the toggle that
    produced the message in their chat. Footer is the absolute last
    line, separated by a blank line from the body / action."""
    e = cat.by_key("cost.daily_threshold")
    assert e is not None
    out = render_event(e, {
        "bot_id": "admin_bot",
        "amount": 5.96,
        "threshold": 5.00,
    })
    assert out.endswith("\n\nsubscription: cost.daily_threshold")

    # Action-less event also gets the footer.
    e2 = cat.by_key("summaries.daily_pod_report")
    assert e2 is not None
    out2 = render_event(e2, {"label": "Daily", "summary": "🟢 All clear"})
    assert out2.endswith("\n\nsubscription: summaries.daily_pod_report")


# ── HTML safety (PR 2 of dispatcher-safety rework) ──────────────────────────


def test_html_escape_handles_dangerous_chars():
    """The HTML escape boundary must cover ``<``, ``>``, ``&`` — the
    three special chars under Telegram's HTML parse mode. Everything
    else passes through, including underscores and dots that broke
    legacy Markdown."""
    from evolve_admin.alerts.catalog import html_escape

    assert html_escape("<script>alert('x')</script>") == \
        "&lt;script&gt;alert('x')&lt;/script&gt;"
    assert html_escape("A & B") == "A &amp; B"
    # Underscores and dots — the chars that tripped legacy Markdown —
    # are NOT special in HTML mode and pass through.
    assert html_escape("security.audit_finding") == "security.audit_finding"
    # None and non-string values coerce safely.
    assert html_escape(None) == ""
    assert html_escape(42) == "42"
    assert html_escape(5.96) == "5.96"


def test_render_event_escapes_dangerous_payload_values():
    """A payload value containing ``<``, ``>``, ``&`` must be escaped
    in the rendered output so Telegram's HTML parser doesn't choke
    (or worse, treat it as an HTML tag). The literal template text
    stays untouched — catalog authors can use ``<b>``, ``<i>``,
    ``<code>`` for intentional formatting."""
    e = cat.by_key("system.gateway_autorestart_failed")
    assert e is not None
    out = render_event(e, {
        "bot_id": "<bot>",
        "port": 18789,
    })
    # The dangerous bot_id is escaped.
    assert "&lt;bot&gt;" in out
    assert "<bot>'s gateway" not in out


def test_render_event_preserves_format_specs_under_html_mode():
    """Catalog templates use numeric format specs like ``${amount:.2f}``.
    The HTML-escaping Formatter must apply the spec FIRST (which
    requires the value to still be numeric), THEN escape — otherwise
    pre-stringifying breaks the format spec with ValueError."""
    e = cat.by_key("cost.daily_threshold")
    assert e is not None
    out = render_event(e, {
        "bot_id": "admin_bot",
        "amount": 5.96,
        "threshold": 5.00,
    })
    # 5.96 formats as "5.96" (two-decimal spec).
    assert "$5.96" in out
    assert "$5.00" in out


def test_render_event_resolves_cli_command_placeholders():
    """cli_command may contain `{placeholder}`s pulled from payload.

    Under HTML mode (PR 2 of the dispatcher-safety rework) the default
    CLI text template now wraps the command in ``<code>...</code>`` so
    Telegram renders it in a monospace font. The legacy markdown
    backticks were ambiguous (an unbalanced one broke the parser);
    HTML's ``<code>`` has unambiguous open/close semantics."""
    e = cat.by_key("system.gateway_autorestart_failed")
    assert e is not None
    out = render_event(e, {"bot_id": "team_bot_a", "port": 18789})
    assert "team_bot_a's gateway is still down" in out
    assert "port 18789" in out
    # Action line wraps the resolved cli_command in <code>...</code>.
    assert "Run: <code>sudo launchctl kickstart -k system/ai.openclaw.team_bot_a-gateway</code>" in out


def test_sudoers_refresh_failed_is_immediate_with_recovery_command():
    """The sudoers-refresh-failed event must deliver IMMEDIATELY, not via
    digest. Routing repo_puller_sudoers to system.watchdog_event (DAILY_DIGEST)
    left a firing signal undelivered for 24h+ (deliveries: []); dormant sudo
    grants silently break features, so the operator needs a prompt ping with
    the one-command recovery."""
    e = cat.by_key("system.sudoers_refresh_failed")
    assert e is not None
    # The core fix: immediate delivery, never deferrable to digest.
    assert e.default_frequency == Frequency.IMMEDIATE
    assert Frequency.DAILY_DIGEST not in e.allowed_frequencies
    assert e.default_enabled is True
    # Carries the exact recovery command the operator must run.
    assert e.action is not None and e.action.mode == "cli"
    out = render_event(e, {"title": "repo-puller sudoers auto-refresh failed"})
    assert "repo-puller sudoers auto-refresh failed" in out
    assert "Run: <code>sudo evolve-admin refresh-sudoers</code>" in out


def test_render_event_openclaw_available_recommends_upgrade_wrapper():
    """The "safe" variant fires when update_watcher's preflight passed.
    Recommend the wrapper that handles npm install + LaunchAgent
    cleanup + gateway restarts; never recommend raw `npm install -g`."""
    e = cat.by_key("updates.openclaw_available")
    assert e is not None
    out = render_event(e, {
        "new_version": "2026.5.7",
        "current_version": "2026.4.29",
    })
    assert "🔄 OpenClaw 2026.5.7 is available" in out
    assert "Passed the safe-upgrade preflight" in out
    assert "Run: <code>sudo evolve-admin menu upgrade</code>" in out
    assert "npm install -g openclaw" not in out


def test_render_event_openclaw_blocked_carries_blocker_summary():
    """The "blocked" variant fires when preflight found blockers. The
    rendered message must surface the blocker_summary up front and point
    at the safe-upgrade preflight (pinned to the new version) for the
    full report — that's where the remediation lives."""
    e = cat.by_key("updates.openclaw_blocked")
    assert e is not None
    out = render_event(e, {
        "new_version": "2026.5.7",
        "current_version": "2026.4.29",
        "blocker_summary": (
            "Blockers:\n"
            "  • Node 25.9 below required 26.0\n"
            "  • rogue ai.openclaw.gateway.plist in team_bot_a's LaunchAgents"
        ),
    })
    assert "⚠️ OpenClaw 2026.5.7 is available" in out
    assert "Evolve needs work first" in out
    assert "Node 25.9 below required 26.0" in out
    assert "rogue ai.openclaw.gateway.plist" in out
    assert "Full preflight report: <code>sudo evolve-admin menu safe-upgrade --target 2026.5.7</code>" in out


# ── Coverage of the spec inventory ──────────────────────────────────────────


# Every producer_source the catalog references. New producers (introduced
# by this spec) are explicitly listed so a typo can't slip in.
_EXPECTED_PRODUCERS = frozenset({
    # Already routed through the dispatcher pre-spec
    "audit", "spend_alert", "pod_report", "cron_alert",
    "forge_engine", "signal_notifier",
    # Sources that exist but are not yet routed through the dispatcher
    # (Phase C migration; see spec §9)
    # "review" retired 2026-08-14 with review.py (#3641).
    "heal", "cost", "analyze", "apply", "outcome",
    "report", "test_runner", "validate", "weekly_review", "repo_puller",
    # Sunday per-bot 7d-horizon trend digest, sibling to `report`'s
    # daily (`reports.adhoc_analyzer`). 7d-trend chips were dropped
    # from the daily by PR #1996; this producer surfaces them weekly.
    "weekly_bot_trends",
    # New producers introduced by the subscription spec
    "update_watcher",
    # CLI-misinvocation Signal producer (oc_cli.oc_command inline guard
    # — fires when an evolve caller invokes openclaw with the wrong
    # argument shape; internal/diagnosis-spend-alert-openclaw-flag-2026-05-21.md)
    "oc_cli",
    # Power feature (developer profile): gh-CLI poller for new activity
    # on issues we filed against upstream repos.
    "upstream_issues_watcher",
    # OpenClaw surface drift-diff (META:deploy C3): update_watcher snapshots a
    # candidate OC release's surface and diffs it against the installed one →
    # updates.openclaw_surface_drift. Signal-store-only producer (see
    # _SIGNAL_ONLY_PRODUCERS in test_alerts_dispatcher); never direct dispatch.
    "oc_surface_drift",
    # cost_watchdog detectors that opt into direct catalog dispatch (the
    # heartbeat_session_bloat detector — Signal store + Telegram on
    # the same tick, sibling to spend_alert's burst path).
    "cost_watchdog",
    # L1 cost breaker auto-trip runner (cost.breaker_tripped event).
    # Wired 2026-05-28 after the Security_bot trip incident showed the prior
    # plain-text WARNING dispatch was indistinguishable from routine alerts.
    "breakers_runner",
    # Producers added 2026-06-03 alongside the signal_notifier allowlist
    # broadening (PR #2064) and a follow-up that gave them bespoke
    # catalog entries so per-event subscription gating works:
    #   • plugin_monitor → system.plugin_health_issue
    #   • exec_outcome_watchdog → system.exec_outcome_failure
    #   • stuck_proposal_monitor → system.stuck_proposal
    #   • session_cost_monitor → cost.session_budget_exceeded
    "plugin_monitor",
    "exec_outcome_watchdog",
    "stuck_proposal_monitor",
    "session_cost_monitor",
    # Post-wrap app-install outcomes (U1 activation fix, 2026-06-11):
    # pack-driver failures after the add-bot wrap + briefing
    # auto-activation receipts (system.app_install_failed,
    # decisions.briefing_activated).
    "app_install",
    # v20 — app structural verifier. Activated by spec-app-coherence-and-
    # reconciliation-2026-06-05.md §17.3 + Q32. Catches scheduled-work
    # failures via the openclaw cron run-status check (the load-bearing
    # operator-value-on-day-1 assertion). Tier-2 findings ingested by
    # audit_poller with producer="app_structural_verifier"; catalog routes
    # openclaw_cron_* findings to system.app_scheduled_work_failure,
    # everything else to security.audit_finding.
    "app_structural_verifier",
    # Agent-invoked app scripts that fail at runtime (the "(agent) failed"
    # exec chip leaking into chat) → system.app_script_failure. Sibling
    # daily transcript-walk producer to agent_bypass_audit; routed via
    # signal_notifier (NOT direct dispatch — must never join the deny-list).
    # Spec: internal/spec-agent-freelance-bypass-2026-06-05.md.
    "app_script_failure_audit",
    # U2.1 — proactive-delivery monitor (spec-proactive-delivery-monitor-
    # 2026-06-10.md §7 + §10). Per-window delivery outcomes for scheduled
    # user-facing apps: system.app_delivery_missed +
    # system.app_delivery_unmeasurable, both routed via signal_notifier
    # (NOT direct dispatch — the producer must never join the deny-list).
    "delivery_monitor",
    # Wave-5 of the 2026-06-11 delivery P0 — post-OC-upgrade send-surface
    # verification (`evolve-admin menu upgrade` re-probes the message-send
    # CLI right after the install). system.send_surface_broken, routed via
    # signal_notifier. The *source* of the same name in the dispatcher
    # registry is the success-path canary message, not this Signal — the
    # producer must never join the deny-list.
    "send_surface_probe",
    # U4.1 — autonomy ladder (spec-autonomy-ladder-2026-06-10.md §3.4).
    # security.autonomy_posture_drift + security.autonomy_review, routed
    # via signal_notifier (NOT direct dispatch — the producer must never
    # join the deny-list). Producer is the existing permission monitor,
    # which already reads all three permission surfaces on this cadence.
    "permission_monitor",
    # Daily CVE scan (security-cve-scan/finalize.py → security.cve_finding).
    # Pre-renders its own operator message and dispatches with
    # source="security_cve_scan"; fail-open via a direct-Telegram fallback
    # if the admin package is broken (internal/review-reports-subscriptions-
    # 2026-06-12.md Bug 4 / R5).
    "security_cve_scan",
    # Subscription-completeness keystone (spec-subscription-completeness-
    # 2026-06-24): producers bound so every dispatched message carries a
    # catalog_event (no catalog_event=None reaches a channel).
    #   • content_scan        → system.identity_doc_missing (USER/IDENTITY/
    #                           README/SOUL/MEMORY/HEARTBEAT/TOOLS missing)
    #   • alerts_loop_monitor → meta.alert_repeat_loop + meta.dispatcher_health
    #   • alert_rate_breaker  → meta.storm_mode (the 🌊 storm/heartbeat notice;
    #                           already a registered direct dispatcher source)
    #   • digest_dispatcher   → meta.digest (the digest-bundle envelope)
    # (send_surface_probe → meta.send_probe and signal_notifier →
    # meta.unclassified reuse already-listed producers.)
    "content_scan",
    "alerts_loop_monitor",
    "alert_rate_breaker",
    "digest_dispatcher",
    # forge_cost_guard already emits forge cap Signals routed by
    # signal_notifier to cost.forge_session_cap; the catalog entry for that
    # key was missing (mapper referenced an unknown key → warning + fallback
    # to source-level gating). Added with the totality pass so the operator
    # gets a real per-event subscription handle.
    "forge_cost_guard",
    # Digest-default classification (D4, spec-subscription-digest-default-
    # 2026-06-28): four producers that previously fell through to the loud
    # meta.unclassified catch-all, now bound to real DAILY_DIGEST classes.
    #   • deploy_drift_monitor → updates.version_skew
    #   • session_economics    → cost.session_economics
    #   • code_quality_monitor → meta.dev_health
    #   • cascade_audit        → system.plugin_telemetry_silent
    "deploy_drift_monitor",
    "session_economics",
    "code_quality_monitor",
    "cascade_audit",
})


def test_producer_sources_are_known():
    actual = cat.known_producer_sources()
    unknown = actual - _EXPECTED_PRODUCERS
    assert not unknown, (
        f"catalog references unknown producer_sources: {sorted(unknown)} — "
        f"add to _EXPECTED_PRODUCERS or fix the typo"
    )


def test_cve_finding_event_is_cataloged_and_registered():
    """Bug 4 / R5 (internal/review-reports-subscriptions-2026-06-12.md):
    security_cve_scan dispatches catalog_event='security.cve_finding'.
    Before this entry existed the dispatcher logged a
    warning_unknown_catalog_event for every CVE batch and the operator
    had no Subscriptions toggle.

    Pin the contract: the event resolves, sits under Security, maps to
    the security_cve_scan producer, defaults on + safety-critical
    (confirm-before-mute), is immediate-only (never deferrable), and the
    source is a registered dispatcher source in the security-shaped
    STATE_PERSISTS category (content-hashed dedup, like audit)."""
    from evolve_admin.alerts import dispatcher as disp

    e = cat.by_key("security.cve_finding")
    assert e is not None
    assert e.category is Category.SECURITY
    assert e.producer_source == "security_cve_scan"
    # Default-on + safety-critical → delivers by default, confirm-before-mute.
    assert e.default_enabled is True
    assert e.is_safety_critical is True
    # Security-critical: immediate only, never deferrable to a digest.
    assert e.default_frequency == Frequency.IMMEDIATE
    assert Frequency.DAILY_DIGEST not in e.allowed_frequencies
    # The source is a registered dispatcher source in the security-shaped
    # STATE_PERSISTS category (content-hashed dedup, same as audit).
    assert disp._DEFAULT_SOURCE_ENABLED.get("security_cve_scan") is True
    assert (
        disp._DEFAULT_SOURCE_CATEGORY.get("security_cve_scan")
        is disp.SourceCategory.STATE_PERSISTS
    )
    # Sample payload renders cleanly with an approved critical header.
    out = render_event(e, e.sample_payload)
    assert "Security finding — CVE-2026-12345" in out
    assert out.lstrip().startswith("🔴 ")


def test_every_pre_spec_emitter_has_a_catalog_entry():
    """Each emitter found in spec § 2.2 inventory must map to at least
    one catalog entry, otherwise the migration plan can't route it.

    Note: ``report`` (the legacy per-bot digest producer) was retired
    2026-06-05 when its content was merged into ``pod_report`` — the
    daily message that operators receive is now a single pod-wide
    report. The legacy catalog key ``summaries.daily_bot_digest`` is
    in ``LEGACY_KEY_MIGRATION`` for any saved subscription overrides.

    ``review`` was dropped 2026-08-14: #3641 deleted review.py, and its
    two catalog rows retired with the rest of that reviewer's advertising
    surfaces. Unlike ``report`` it got NO ``LEGACY_KEY_MIGRATION`` entry —
    there is no same-class survivor to inherit a saved override (see the
    note on that table in catalog.py).
    """
    must_be_covered = {
        "repo_puller", "analyze", "apply",
        "cost", "heal",
        "outcome",
        # test_runner retired 2026-06-08 with the app-test surface
        "validate", "weekly_review",
    }
    actual = cat.known_producer_sources()
    missing = must_be_covered - actual
    assert not missing, (
        f"unmigrated emitters {sorted(missing)} have no catalog entry — "
        f"can't route them through subscriptions"
    )


# ── Operator message style guide enforcement ───────────────────────────────


# The approved set per docs/operator-message-style.md. Catalog event
# body_templates MUST start with exactly one of these (followed by a
# space). Any other leading emoji is a guide violation and a CI failure.
APPROVED_HEADER_EMOJIS = (
    "🔴",   # critical
    "⚠️",   # warning
    "🟢",   # recovery
    "🛡️",   # security (non-critical)
    "💰",   # cost (non-critical)
    "📊",   # summary
    "📋",   # decision
    "🔄",   # update
    "🔧",   # maintenance / ad-hoc
    "⚡",   # breaker tripped (system-state change — distinct from 🔴 alert)
)

# Disallowed historical emoji that drifted in before the style guide.
# Listed explicitly so the failure message is descriptive when one
# leaks back into the catalog.
# 2026-05-28: ⚡ promoted from disallowed → approved specifically for
# cost.breaker_tripped. Rationale: a breaker trip is a system-state
# change (auto-turns paused, exec passthrough degraded) distinct from
# generic 🔴 critical alerts. The circuit-breaker visual semantic
# matches the mental model. Scope is narrow — only breaker-trip
# events use ⚡; other emergencies still use 🔴.
DISALLOWED_HEADER_EMOJIS = ("🚨", "🟡", "⏰", "🚧", "🔽")


def test_body_templates_start_with_approved_emoji():
    """The first header line every operator sees starts with one of
    the approved emojis. Checked against the *rendered* sample —
    body_templates with `{emoji}` placeholders are fine as long as
    the payload they're rendered with picks an approved value.

    This is the forcing function for docs/operator-message-style.md;
    new catalog events that violate the rule fail CI.
    """
    failures: list[str] = []
    for e in cat.CATALOG:
        try:
            rendered = render_event(e, e.sample_payload).lstrip()
        except Exception as exc:
            failures.append(f"{e.key!r} sample render failed: {exc}")
            continue
        if any(rendered.startswith(emj + " ") or rendered.startswith(emj + "\n")
               for emj in APPROVED_HEADER_EMOJIS):
            continue
        offender = next(
            (emj for emj in DISALLOWED_HEADER_EMOJIS if rendered.startswith(emj)),
            rendered[:4],
        )
        failures.append(f"{e.key!r} rendered header starts with {offender!r}")
    assert not failures, (
        "Catalog body_template emoji violations:\n  - "
        + "\n  - ".join(failures)
        + "\n\nApproved set: "
        + " ".join(APPROVED_HEADER_EMOJIS)
        + "\n\nSee docs/operator-message-style.md § Headers."
    )


def test_disallowed_emojis_absent_from_sample_payloads():
    """Sample payloads MUST NOT smuggle disallowed emoji either —
    they're what the 'Test' button renders to the operator, so the
    discipline applies there too."""
    failures: list[str] = []
    for e in cat.CATALOG:
        for k, v in (e.sample_payload or {}).items():
            if not isinstance(v, str):
                continue
            for bad in DISALLOWED_HEADER_EMOJIS:
                if bad in v:
                    failures.append(f"{e.key!r}.sample_payload[{k!r}] contains {bad!r}")
    assert not failures, (
        "Sample payload emoji violations:\n  - " + "\n  - ".join(failures)
    )


def test_cli_actions_reference_real_commands():
    """Every cli_action that starts with 'evolve-admin' must use a subcommand
    that actually exists in the Click CLI. This catches typos and commands that
    were removed or renamed after the catalog entry was written."""
    import sys as _sys
    _admin_pkg = Path(__file__).parent.parent
    if str(_admin_pkg) not in _sys.path:
        _sys.path.insert(0, str(_admin_pkg))
    from evolve_admin.cli import main as _cli_main

    valid_top_level = set(_cli_main.commands.keys())

    failures: list[str] = []
    for e in cat.CATALOG:
        if e.action is None or e.action.mode != "cli":
            continue
        cmd = e.action.cli_command or ""
        # Strip leading "sudo " and optional flags before "evolve-admin"
        tokens = cmd.replace("sudo ", "").split()
        if not tokens or tokens[0] != "evolve-admin":
            continue
        if len(tokens) < 2:
            failures.append(f"{e.key!r}: cli_command has no subcommand: {cmd!r}")
            continue
        subcommand = tokens[1]
        if subcommand not in valid_top_level:
            failures.append(
                f"{e.key!r}: cli_command uses unknown subcommand {subcommand!r} "
                f"(valid: {sorted(valid_top_level)})"
            )
    assert not failures, (
        "catalog cli_action commands reference non-existent evolve-admin subcommands:\n  - "
        + "\n  - ".join(failures)
    )


def test_lookup_helpers():
    e = cat.by_key("cost.daily_threshold")
    assert e is not None and e.label == "Daily spend over soft threshold"

    assert cat.by_key("nonexistent.event") is None

    sec = cat.by_category(Category.SECURITY)
    assert all(e.category == Category.SECURITY for e in sec)
    assert len(sec) >= 4   # spec § 3 declares 4 security events

    keys = cat.all_keys()
    assert "security.audit_finding" in keys
    assert len(keys) == len(set(keys))


# ── default_frequency derivation guardrail (workstream D1) ──────────────────
# Spec: internal/spec-subscription-digest-default-2026-06-28.md.
#
# The catalog reserves IMMEDIATE for must-act events and defaults everything
# else to the operator's daily digest. default_frequency is DERIVED from a
# single rule (catalog.derive_default_frequency); these tests pin that every
# entry already matches the rule (so a new event can't silently ship a loud
# default) and that the 2026-06-28 flood set collapses to the digest.


def test_default_frequency_matches_derivation_rule():
    """Every catalog entry's default_frequency equals what the single
    derivation rule produces for it. A new event that hand-picks IMMEDIATE
    without earning it (CRITICAL / is_safety_critical / MUST_PAGE_ALLOWLIST)
    fails here — the guardrail against re-introducing the notification flood."""
    drifted = []
    for e in cat.CATALOG:
        want = cat.derive_default_frequency(
            key=e.key,
            severity=e.severity,
            is_safety_critical=e.is_safety_critical,
            allowed_frequencies=e.allowed_frequencies,
        )
        if e.default_frequency != want:
            drifted.append((e.key, e.default_frequency.value, want.value))
    assert not drifted, (
        "default_frequency disagrees with derive_default_frequency for: "
        + "; ".join(f"{k}: catalog={cur} rule={want}" for k, cur, want in drifted)
    )


def test_derived_default_is_always_an_allowed_frequency():
    """The derivation can never produce a default outside an event's
    allowed_frequencies (would trip CatalogEvent.__post_init__)."""
    for e in cat.CATALOG:
        want = cat.derive_default_frequency(
            key=e.key,
            severity=e.severity,
            is_safety_critical=e.is_safety_critical,
            allowed_frequencies=e.allowed_frequencies,
        )
        assert want in e.allowed_frequencies, (
            f"{e.key}: derived {want} not in allowed {e.allowed_frequencies}"
        )


def test_must_page_rule_keeps_critical_and_allowlist_immediate():
    """The three IMMEDIATE triggers each force IMMEDIATE on their own:
    CRITICAL severity, is_safety_critical, and MUST_PAGE_ALLOWLIST membership.
    World-readable-creds findings carry CRITICAL severity, so a CRITICAL
    catalog entry must stay immediate even though sibling ERROR audit
    findings now digest."""
    # Every catalog entry that the rule routes to IMMEDIATE must be must-act.
    for e in cat.CATALOG:
        if e.default_frequency is Frequency.IMMEDIATE:
            is_must_act = (
                e.severity is Severity.CRITICAL
                or e.is_safety_critical
                or e.key in cat.MUST_PAGE_ALLOWLIST
            )
            # The remaining IMMEDIATE survivors are events that declared
            # themselves non-deferrable (allowed_frequencies == (IMMEDIATE,)).
            immediate_only = e.allowed_frequencies == (Frequency.IMMEDIATE,)
            assert is_must_act or immediate_only, (
                f"{e.key} defaults to IMMEDIATE but is neither must-act nor "
                f"immediate-only — it should digest"
            )
    # Direct rule spot-checks: each trigger forces IMMEDIATE independently.
    assert cat.derive_default_frequency(
        key="x.critical", severity=Severity.CRITICAL,
        is_safety_critical=False,
        allowed_frequencies=(Frequency.IMMEDIATE, Frequency.DAILY_DIGEST),
    ) is Frequency.IMMEDIATE
    assert cat.derive_default_frequency(
        key="x.safety", severity=Severity.ERROR, is_safety_critical=True,
        allowed_frequencies=(Frequency.IMMEDIATE, Frequency.DAILY_DIGEST),
    ) is Frequency.IMMEDIATE
    assert cat.derive_default_frequency(
        key="cost.hard_cap_hit", severity=Severity.WARNING,
        is_safety_critical=False,
        allowed_frequencies=(Frequency.IMMEDIATE, Frequency.DAILY_DIGEST),
    ) is Frequency.IMMEDIATE
    # A plain ERROR/WARNING event with a digest option falls through to digest.
    assert cat.derive_default_frequency(
        key="x.benign", severity=Severity.ERROR, is_safety_critical=False,
        allowed_frequencies=(Frequency.IMMEDIATE, Frequency.DAILY_DIGEST),
    ) is Frequency.DAILY_DIGEST


def test_2026_06_28_flood_set_collapses_to_digest():
    """The 2026-06-28 evo-vps flood: ~40 pushes, one actionable. The benign
    bulk — security.audit_finding (ERROR), security.config_drift, and the
    meta.unclassified catch-all — must now default to the daily digest so a
    repeat of that day stays a single bundled message, not a flood.

    The lone genuinely-actionable class on that pod was a CRITICAL-severity
    finding; the catalog's CRITICAL security event (security.cve_finding)
    stays IMMEDIATE, confirming the must-act path is preserved."""
    flood_to_digest = (
        "security.audit_finding",
        "security.config_drift",
        "meta.unclassified",
    )
    for key in flood_to_digest:
        e = cat.by_key(key)
        assert e is not None, key
        assert e.default_frequency is Frequency.DAILY_DIGEST, (
            f"{key} must default to the daily digest after workstream D1, "
            f"got {e.default_frequency}"
        )
        # The operator can still raise any of these back to immediate.
        assert Frequency.IMMEDIATE in e.allowed_frequencies, key

    # Must-act security findings are untouched — a CRITICAL finding still pages.
    cve = cat.by_key("security.cve_finding")
    assert cve is not None
    assert cve.severity is Severity.CRITICAL
    assert cve.default_frequency is Frequency.IMMEDIATE


def test_config_drift_and_audit_finding_stay_group_safety_critical():
    """Dropping the per-event is_safety_critical flag from the two flood
    events (so the rule can digest them) must NOT weaken the mute warning:
    both still belong to the safety-critical security_findings group, which
    is what drives confirm-before-mute and the pod_state projection."""
    from evolve_admin.alerts import subscriptions_catalog as subcat
    for key in ("security.audit_finding", "security.config_drift"):
        e = cat.by_key(key)
        assert e is not None
        assert e.is_safety_critical is False, (
            f"{key} keeps its per-event flag — it should rely on the group"
        )
        group = subcat.by_id(e.subscription)
        assert group is not None and group.is_safety_critical is True, (
            f"{key}'s group must stay safety-critical so muting still warns"
        )
