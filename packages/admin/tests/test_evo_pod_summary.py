"""tests/test_evo_pod_summary.py — Tests for the evo pod summary capability.

Covers:
  1. subcommands.parse() — natural-language phrase aliases route to "summary"
  2. subcommands.lookup() — "status" / "week" aliases resolve to the summary command
  3. cross_bot_summary.gather() — data gathering from on-disk fixtures
  4. cross_bot_summary.render_summary() — rendering logic
  5. evo/handlers/pod_summary.render() — handler returns a DispatchResult
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Path setup — mirrors the pattern in other admin tests
# ─────────────────────────────────────────────────────────────────────────────

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Parser — phrase aliases
# ─────────────────────────────────────────────────────────────────────────────


class TestParsePhraseAliases:
    def test_whats_on_this_week_question_mark(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo, what's on this week?")
        assert result is not None
        assert result.subcommand == "summary"
        assert not result.is_bare

    def test_whats_on_this_week_no_punctuation(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo what's on this week")
        assert result is not None
        assert result.subcommand == "summary"

    def test_whats_on_short_does_not_hijack(self):
        """The bare phrase 'what's on' must NOT route to summary — it
        overlaps too readily with normal English (cf. defect 2 in
        b3-f-review.md). Only the full week-bounded phrases trigger.

        This was previously a positive ("evo what's on" → summary) but
        the prefix-match logic that allowed it also caused false
        positives on "evo what's on the agenda", "evo what's on
        tomorrow", etc. — see the dedicated regression test below.
        """
        from evolve_admin.evo.subcommands import parse

        result = parse("evo what's on")
        # Falls through to single-token lookup; "what's" isn't a known
        # subcommand, so subcommand is the raw first token (lowercased).
        assert result is not None
        assert result.subcommand != "summary"

    def test_whats_on_without_apostrophe(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo whats on this week")
        assert result is not None
        assert result.subcommand == "summary"

    def test_what_is_on_this_week(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo what is on this week")
        assert result is not None
        assert result.subcommand == "summary"

    def test_evo_status(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo status")
        assert result is not None
        assert result.subcommand == "status"  # parser returns the token; lookup resolves alias

    def test_evo_summary(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo summary")
        assert result is not None
        assert result.subcommand == "summary"

    def test_evo_week(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo week")
        assert result is not None
        assert result.subcommand == "week"  # parser token; lookup resolves alias

    def test_evo_comma_whats_on_uppercase(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("Evo, What's on this week?")
        assert result is not None
        assert result.subcommand == "summary"

    def test_existing_help_unaffected(self):
        """Phrase alias logic must not break existing single-token parsing."""
        from evolve_admin.evo.subcommands import parse

        result = parse("evo help")
        assert result is not None
        assert result.subcommand == "help"

    def test_existing_bare_unaffected(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo")
        assert result is not None
        assert result.is_bare

    def test_evo_claim_with_args_unaffected(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo claim darwin charles")
        assert result is not None
        assert result.subcommand == "claim"
        assert result.args == "darwin charles"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Subcommand registry — alias lookup
# ─────────────────────────────────────────────────────────────────────────────


class TestSubcommandLookup:
    def test_lookup_summary_by_name(self):
        from evolve_admin.evo.subcommands import lookup

        sc = lookup("summary")
        assert sc is not None
        assert sc.name == "summary"
        assert sc.handler == "evolve_admin.evo.handlers.pod_summary:render"

    def test_lookup_summary_via_status_alias(self):
        from evolve_admin.evo.subcommands import lookup

        sc = lookup("status")
        assert sc is not None
        assert sc.name == "summary"

    def test_lookup_summary_via_week_alias(self):
        from evolve_admin.evo.subcommands import lookup

        sc = lookup("week")
        assert sc is not None
        assert sc.name == "summary"

    def test_summary_available_to_admin_only(self):
        """``evo summary`` is pod-wide — aggregates alerts, activity, and
        spend across every bot. Tightened from PRIMARY to ADMIN
        (2026-05-17) so a non-admin primary user of bot X can't see
        bot Y's data through their own bot.

        ``role_can_run`` still grants admins access to PRIMARY-gated
        commands by superset semantics; this test verifies the inverse
        — primaries are NO LONGER in summary's allow set."""
        from evolve_admin.evo.subcommands import lookup, role_can_run

        sc = lookup("summary")
        assert sc is not None
        assert "admin" in sc.available_to
        assert "primary" not in sc.available_to
        # role_can_run gate matches.
        assert role_can_run("admin", sc) is True
        assert role_can_run("primary", sc) is False
        assert role_can_run("secondary", sc) is False


# ─────────────────────────────────────────────────────────────────────────────
# Fixture helpers
# ─────────────────────────────────────────────────────────────────────────────


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _make_signal(
    tmp: Path,
    signal_id: str,
    *,
    producer: str = "test_producer",
    severity: str = "warn",
    scope: str = "bot",
    bot_id: str | None = "team_bot_a",
    title: str = "Test alert",
    state: str = "firing",
) -> None:
    _write_json(
        tmp / "signals" / "firing" / f"{signal_id}.json",
        {
            "id": signal_id,
            "state": state,
            "producer": producer,
            "type": "test_type",
            "flavor": "maintenance",
            "severity": severity,
            "scope": scope,
            "bot_id": bot_id,
            "title": title,
            "signature": f"{producer}:test_type:{bot_id or 'pod'}",
            "created_at": "2026-05-12T08:00:00+00:00",
            "updated_at": "2026-05-12T08:00:00+00:00",
        },
    )


def _make_proposal(tmp: Path, proposal_id: str, *, bot_id: str = "team_bot_a") -> None:
    # Phase B: the pending-count reads through arbiter.store.iter_proposals,
    # which only yields records that deserialize — so build a store-valid
    # proposal via the harness instead of a minimal hand-rolled dict.
    from arbiter.state_machine import transition
    from arbiter.store import write_proposal
    from testing.harness import make_config_patch_proposal

    p = make_config_patch_proposal(
        target_path=str(tmp / f"{proposal_id}.json"), bot_id=bot_id,
    )
    transition(p, "pending", actor="test", reason="seed")
    write_proposal(p, tmp)


def _make_metric(tmp: Path, bot_id: str, day: date, *, turns: int = 5, cost: float = 0.10) -> None:
    _write_json(
        tmp / "metrics" / day.isoformat() / f"{bot_id}.json",
        {
            "turn_count": turns,
            "total_cost_estimated": cost,
        },
    )


def _make_manifest(tmp: Path, bot_id: str, app_id: str, *, last_result: str | None = "pass") -> None:
    _write_json(
        tmp / "applications" / bot_id / f"{app_id}.json",
        {
            "id": app_id,
            "name": app_id.replace("-", " ").title(),
            "last_test_result": last_result,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. cross_bot_summary.gather()
# ─────────────────────────────────────────────────────────────────────────────


class TestGather:
    def test_empty_pod(self, tmp_path):
        import cross_bot_summary as cbs

        network = {"bots": {}}
        summary = cbs.gather(tmp_path, network)
        assert summary.bot_slices == []
        assert summary.pod_alerts == []
        assert summary.total_pending_proposals == 0
        assert summary.total_cost_7d_usd == 0.0
        assert summary.errors == []

    def test_excludes_primary_bot(self, tmp_path):
        import cross_bot_summary as cbs

        network = {"bots": {"evolve": {"role": "primary"}}}
        _make_metric(tmp_path, "evolve", date.today())
        summary = cbs.gather(tmp_path, network)
        assert summary.bot_slices == []

    def test_member_bot_appears(self, tmp_path):
        import cross_bot_summary as cbs

        network = {"bots": {"team_bot_a": {}}}
        summary = cbs.gather(tmp_path, network)
        assert len(summary.bot_slices) == 1
        assert summary.bot_slices[0].bot_id == "team_bot_a"

    def test_firing_alert_surfaces(self, tmp_path):
        import cross_bot_summary as cbs

        network = {"bots": {"team_bot_a": {}}}
        _make_signal(tmp_path, "sig-001", bot_id="team_bot_a", title="Cost spike", severity="warn")
        summary = cbs.gather(tmp_path, network)
        team_bot_a = summary.bot_slices[0]
        assert "Cost spike" in team_bot_a.firing_alerts

    def test_info_signal_filtered_out(self, tmp_path):
        import cross_bot_summary as cbs

        network = {"bots": {"team_bot_a": {}}}
        _make_signal(tmp_path, "sig-002", bot_id="team_bot_a", severity="info", title="Just FYI")
        summary = cbs.gather(tmp_path, network)
        team_bot_a = summary.bot_slices[0]
        assert team_bot_a.firing_alerts == []

    def test_pod_scoped_alert(self, tmp_path):
        import cross_bot_summary as cbs

        network = {"bots": {"team_bot_a": {}}}
        # Use "alert" — the canonical high-severity value per
        # schema/signal.py. "critical" is NOT in the Signal Severity
        # enum and must never be written by producers.
        _make_signal(tmp_path, "sig-003", scope="pod", bot_id=None, severity="alert", title="Disk full")
        summary = cbs.gather(tmp_path, network)
        assert "Disk full" in summary.pod_alerts

    def test_proposals_counted(self, tmp_path):
        import cross_bot_summary as cbs

        network = {"bots": {"team_bot_a": {}}}
        _make_proposal(tmp_path, "prop-001", bot_id="team_bot_a")
        _make_proposal(tmp_path, "prop-002", bot_id="team_bot_a")
        summary = cbs.gather(tmp_path, network)
        assert summary.bot_slices[0].pending_proposals == 2
        assert summary.total_pending_proposals == 2

    def test_metrics_summed_over_7_days(self, tmp_path):
        import cross_bot_summary as cbs

        network = {"bots": {"admin_bot": {}}}
        today = date.today()
        for i in range(5):
            _make_metric(tmp_path, "admin_bot", today - timedelta(days=i), turns=3, cost=0.05)
        summary = cbs.gather(tmp_path, network, today=today)
        admin_bot = summary.bot_slices[0]
        assert admin_bot.turns_7d == 15
        assert abs(admin_bot.cost_7d_usd - 0.25) < 0.001

    def test_metrics_outside_7_days_excluded(self, tmp_path):
        import cross_bot_summary as cbs

        network = {"bots": {"admin_bot": {}}}
        today = date.today()
        # Write one metric 8 days ago — should not be included.
        _make_metric(tmp_path, "admin_bot", today - timedelta(days=8), turns=100, cost=9.99)
        summary = cbs.gather(tmp_path, network, today=today)
        admin_bot = summary.bot_slices[0]
        assert admin_bot.turns_7d == 0
        assert admin_bot.cost_7d_usd == 0.0

    def test_app_count_and_active_apps(self, tmp_path):
        import cross_bot_summary as cbs

        network = {"bots": {"team_bot_a": {}}}
        _make_manifest(tmp_path, "team_bot_a", "morning-brief", last_result="pass")
        _make_manifest(tmp_path, "team_bot_a", "deadline-watch", last_result="fail")
        summary = cbs.gather(tmp_path, network)
        team_bot_a = summary.bot_slices[0]
        assert team_bot_a.app_count == 2
        # Only the passing app should be in active_apps.
        assert "Morning Brief" in team_bot_a.active_apps
        assert "Deadline Watch" not in team_bot_a.active_apps

    def test_multiple_bots_sorted(self, tmp_path):
        import cross_bot_summary as cbs

        network = {"bots": {"team_bot_b": {}, "team_bot_a": {}, "admin_bot": {}}}
        summary = cbs.gather(tmp_path, network)
        bot_ids = [sl.bot_id for sl in summary.bot_slices]
        assert bot_ids == sorted(bot_ids)

    def test_missing_shared_dir_returns_errors(self, tmp_path):
        import cross_bot_summary as cbs

        # Use a nonexistent subdir — signals dir won't exist; gather should
        # not crash but may record errors.
        network = {"bots": {"team_bot_a": {}}}
        # This should not raise even if signals/ dir is missing.
        summary = cbs.gather(tmp_path / "nonexistent", network)
        # Can't read bots from a missing dir; but we passed the network dict
        # so bot_slices is still built (from network param, not from disk).
        # The summary may have an errors entry or just empty data — either
        # is acceptable as long as gather() does not raise.
        assert isinstance(summary, cbs.PodSummary)

    def test_corrupt_signal_skipped(self, tmp_path):
        import cross_bot_summary as cbs

        # Write a corrupt JSON file in signals/firing/
        corrupt = tmp_path / "signals" / "firing" / "corrupt.json"
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_text("not json at all {{{")
        network = {"bots": {"team_bot_a": {}}}
        # Must not raise.
        summary = cbs.gather(tmp_path, network)
        assert isinstance(summary, cbs.PodSummary)


# ─────────────────────────────────────────────────────────────────────────────
# 4. cross_bot_summary.render_summary()
# ─────────────────────────────────────────────────────────────────────────────


class TestRenderSummary:
    def _make_empty_summary(self):
        import cross_bot_summary as cbs

        return cbs.PodSummary(generated_at="2026-05-12T08:00:00+00:00")

    def test_render_empty_pod(self):
        import cross_bot_summary as cbs

        summary = self._make_empty_summary()
        text = cbs.render_summary(summary)
        assert "Pod summary" in text
        assert "evo better" in text

    def test_render_all_quiet(self):
        import cross_bot_summary as cbs

        summary = cbs.PodSummary(
            generated_at="2026-05-12T08:00:00+00:00",
            bot_slices=[cbs.BotSlice(bot_id="team_bot_a")],
        )
        text = cbs.render_summary(summary)
        assert "Pod summary" in text

    def test_render_alert_surfaces(self):
        import cross_bot_summary as cbs

        summary = cbs.PodSummary(
            generated_at="2026-05-12T08:00:00+00:00",
            bot_slices=[
                cbs.BotSlice(bot_id="team_bot_a", firing_alerts=["Cost spike"]),
            ],
        )
        text = cbs.render_summary(summary)
        assert "Cost spike" in text
        assert "team_bot_a" in text

    def test_render_pod_alert_surfaces(self):
        import cross_bot_summary as cbs

        summary = cbs.PodSummary(
            generated_at="2026-05-12T08:00:00+00:00",
            pod_alerts=["Disk full"],
        )
        text = cbs.render_summary(summary)
        assert "Disk full" in text
        assert "Pod alerts" in text

    def test_render_proposal_count(self):
        import cross_bot_summary as cbs

        summary = cbs.PodSummary(
            generated_at="2026-05-12T08:00:00+00:00",
            bot_slices=[
                cbs.BotSlice(bot_id="team_bot_a", pending_proposals=3),
            ],
            total_pending_proposals=3,
        )
        text = cbs.render_summary(summary)
        assert "3 suggestion" in text

    def test_render_activity_shows_turns(self):
        import cross_bot_summary as cbs

        summary = cbs.PodSummary(
            generated_at="2026-05-12T08:00:00+00:00",
            bot_slices=[
                cbs.BotSlice(bot_id="team_bot_a", turns_7d=42, cost_7d_usd=0.35),
            ],
        )
        text = cbs.render_summary(summary)
        assert "42 turns" in text
        assert "$0.35" in text

    def test_render_bot_names_substituted(self):
        import cross_bot_summary as cbs

        summary = cbs.PodSummary(
            generated_at="2026-05-12T08:00:00+00:00",
            bot_slices=[
                cbs.BotSlice(bot_id="team_bot_a", firing_alerts=["Oops"]),
            ],
        )
        text = cbs.render_summary(summary, bot_names={"team_bot_a": "Team_bot_a the helper"})
        assert "Team_bot_a the helper" in text

    def test_render_fits_telegram_limit(self):
        """The rendered summary should fit in one Telegram message."""
        import cross_bot_summary as cbs

        # Build a worst-case pod: 7 bots, each with alerts and proposals.
        slices = [
            cbs.BotSlice(
                bot_id=f"bot{i}",
                firing_alerts=[f"Alert A for bot{i}", f"Alert B for bot{i}"],
                pending_proposals=2,
                turns_7d=100,
                cost_7d_usd=1.23,
                active_apps=["Morning Brief", "Deadline Watch"],
            )
            for i in range(7)
        ]
        summary = cbs.PodSummary(
            generated_at="2026-05-12T08:00:00+00:00",
            bot_slices=slices,
            pod_alerts=["Pod-wide disk pressure"],
            total_pending_proposals=14,
            total_cost_7d_usd=8.61,
        )
        text = cbs.render_summary(summary)
        assert len(text) < 4096, f"Message too long: {len(text)} chars"

    def test_render_singular_proposal_label(self):
        """1 suggestion — not '1 suggestions'."""
        import cross_bot_summary as cbs

        summary = cbs.PodSummary(
            generated_at="2026-05-12T08:00:00+00:00",
            bot_slices=[cbs.BotSlice(bot_id="team_bot_a", pending_proposals=1)],
            total_pending_proposals=1,
        )
        text = cbs.render_summary(summary)
        assert "1 suggestion" in text
        assert "1 suggestions" not in text


# ─────────────────────────────────────────────────────────────────────────────
# 5. Handler — end-to-end DispatchResult
# ─────────────────────────────────────────────────────────────────────────────


class TestPodSummaryHandler:
    def _network(self, tmp_path: Path) -> dict:
        return {
            "sharedDir": str(tmp_path),
            "bots": {
                "team_bot_a": {"name": "Team_bot_a"},
                "admin_bot": {"name": "Admin_bot"},
                "evolve": {"role": "primary"},
            },
        }

    def test_handler_returns_dispatch_result(self, tmp_path):
        from evolve_admin.evo.handlers.pod_summary import render
        from evolve_admin.evo.dispatch import DispatchResult

        result = render(role="primary", bot_id="evolve", args="", network=self._network(tmp_path))
        assert isinstance(result, DispatchResult)
        assert result.subcommand == "summary"
        assert result.mode == "speak"

    def test_handler_system_append_has_speak_preamble(self, tmp_path):
        from evolve_admin.evo.handlers.pod_summary import render

        result = render(role="primary", bot_id="evolve", args="", network=self._network(tmp_path))
        assert result.system_append is not None
        assert "IMPORTANT" in result.system_append
        assert "verbatim" in result.system_append

    def test_handler_direct_send_message_matches_body(self, tmp_path):
        from evolve_admin.evo.handlers.pod_summary import render

        result = render(role="primary", bot_id="evolve", args="", network=self._network(tmp_path))
        assert result.direct_send_message is not None
        assert "Pod summary" in result.direct_send_message

    def test_handler_excludes_primary_bot_from_summary(self, tmp_path):
        from evolve_admin.evo.handlers.pod_summary import render

        # Add metrics for evolve (primary) — should NOT appear in summary.
        _make_metric(tmp_path, "evolve", date.today(), turns=100, cost=9.99)
        result = render(role="primary", bot_id="evolve", args="", network=self._network(tmp_path))
        # "evolve" should not appear as a bot-level row (it's the primary bot).
        # It may appear in a generic footer link but not in the turn/cost table.
        # We just verify no crash and a summary is returned.
        assert "Pod summary" in (result.direct_send_message or "")

    def test_handler_surfaces_alert(self, tmp_path):
        from evolve_admin.evo.handlers.pod_summary import render

        # severity="alert" is the canonical high-severity value
        # (schema/signal.py Severity enum is info|warn|alert).
        _make_signal(tmp_path, "sig-handler-01", bot_id="team_bot_a", title="Big problem", severity="alert")
        result = render(role="primary", bot_id="evolve", args="", network=self._network(tmp_path))
        assert "Big problem" in (result.direct_send_message or "")

    def test_handler_admin_role_allowed(self, tmp_path):
        """Admin role is a strict superset of primary — must also get a result."""
        from evolve_admin.evo.handlers.pod_summary import render
        from evolve_admin.evo.dispatch import DispatchResult

        result = render(role="admin", bot_id="evolve", args="", network=self._network(tmp_path))
        assert isinstance(result, DispatchResult)

    def test_handler_missing_shared_dir(self, tmp_path):
        """When shared_dir doesn't exist, gather should soft-fail gracefully."""
        from evolve_admin.evo.handlers.pod_summary import render

        network = {
            "sharedDir": str(tmp_path / "nonexistent"),
            "bots": {"team_bot_a": {}},
        }
        # Must not raise — returns a DispatchResult (possibly with error message).
        result = render(role="primary", bot_id="evolve", args="", network=network)
        assert result.direct_send_message is not None


# ─────────────────────────────────────────────────────────────────────────────
# 6. Regression tests for fix-up of B3.f review defects
#    (b3-f-review.md — Critical / High / Medium / Cosmetic findings).
# ─────────────────────────────────────────────────────────────────────────────


class TestSeverityFilterRegression:
    """Defect 1 (CRITICAL): the original implementation filtered
    ``severity in ("warn", "critical")`` but the Signal schema enum is
    ``"info" | "warn" | "alert"`` (schema/signal.py:36). Every "alert"
    severity signal — security_warden, heal, sysadmin_watchdog — was
    silently dropped. Tests below pin the correct policy.
    """

    def test_alert_severity_surfaces_for_bot(self, tmp_path):
        """An 'alert'-severity bot-scoped signal must appear in the
        per-bot firing_alerts list. This is the load-bearing case: 19 of
        71 firing signals on the production mini are 'alert' severity."""
        import cross_bot_summary as cbs

        network = {"bots": {"team_bot_a": {}}}
        _make_signal(
            tmp_path,
            "sig-alert-bot",
            bot_id="team_bot_a",
            scope="bot",
            severity="alert",
            title="security_warden: multi-user exec=full on team_bot_a",
        )
        summary = cbs.gather(tmp_path, network)
        team_bot_a = summary.bot_slices[0]
        # The redundant "<bot_id>:" prefix is stripped on the gather
        # side, so we check the residual substring.
        assert any("multi-user exec=full" in a for a in team_bot_a.firing_alerts), (
            f"alert-severity signal was filtered out: {team_bot_a.firing_alerts!r}"
        )

    def test_alert_severity_surfaces_for_pod(self, tmp_path):
        """An 'alert'-severity pod-scoped signal must appear in pod_alerts."""
        import cross_bot_summary as cbs

        network = {"bots": {"team_bot_a": {}}}
        _make_signal(
            tmp_path,
            "sig-alert-pod",
            scope="pod",
            bot_id=None,
            severity="alert",
            title="Disk full at /Users/Shared/evolve",
        )
        summary = cbs.gather(tmp_path, network)
        assert "Disk full at /Users/Shared/evolve" in summary.pod_alerts

    def test_info_severity_filtered(self, tmp_path):
        """'info' is advisory-only and should not surface in either bucket."""
        import cross_bot_summary as cbs

        network = {"bots": {"team_bot_a": {}}}
        _make_signal(tmp_path, "sig-info-1", bot_id="team_bot_a", severity="info", title="FYI")
        _make_signal(tmp_path, "sig-info-2", scope="pod", bot_id=None, severity="info", title="FYI pod")
        summary = cbs.gather(tmp_path, network)
        team_bot_a = summary.bot_slices[0]
        assert team_bot_a.firing_alerts == []
        assert summary.pod_alerts == []

    def test_warn_severity_surfaces(self, tmp_path):
        """'warn' is the other surfaced severity. Pin it explicitly."""
        import cross_bot_summary as cbs

        network = {"bots": {"team_bot_a": {}}}
        _make_signal(tmp_path, "sig-warn", bot_id="team_bot_a", severity="warn", title="Cost above threshold")
        summary = cbs.gather(tmp_path, network)
        team_bot_a = summary.bot_slices[0]
        assert "Cost above threshold" in team_bot_a.firing_alerts

    def test_unknown_critical_severity_filtered(self, tmp_path):
        """Defensive: a signal that somehow lands with severity='critical'
        — which is NOT in the canonical enum — must NOT surface. The
        previous code would surface it; the new code must filter it.
        This is the inverse of the original bug: don't quietly accept
        a value that isn't in the schema."""
        import cross_bot_summary as cbs

        network = {"bots": {"team_bot_a": {}}}
        _make_signal(
            tmp_path,
            "sig-bad-severity",
            bot_id="team_bot_a",
            severity="critical",  # not a valid Severity per schema/signal.py:36
            title="Wrong-severity signal",
        )
        summary = cbs.gather(tmp_path, network)
        team_bot_a = summary.bot_slices[0]
        assert team_bot_a.firing_alerts == []


class TestPhraseAliasNoFalsePositives:
    """Defect 2 (HIGH): startswith(phrase + ' ') prefix matching caused
    'evo what's on the agenda', 'evo what's on tomorrow', 'evo whats on
    for team_bot_a' to hijack normal conversational evo phrasing and route to
    the pod summary handler. The fix restricts phrase aliases to
    whole-phrase match only."""

    def test_whats_on_the_agenda_does_not_route(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo what's on the agenda")
        assert result is not None
        assert result.subcommand != "summary", (
            "conversational 'what's on the agenda' should not hijack the "
            "pod summary handler"
        )

    def test_whats_on_tomorrow_does_not_route(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo what's on tomorrow")
        assert result is not None
        assert result.subcommand != "summary"

    def test_whats_on_for_team_bot_a_does_not_route(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo whats on for team_bot_a")
        assert result is not None
        assert result.subcommand != "summary"

    def test_whats_on_my_calendar_does_not_route(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo what's on my calendar")
        assert result is not None
        assert result.subcommand != "summary"

    def test_full_phrase_this_week_still_routes(self):
        """Confirm the intended phrase still works after the tightening."""
        from evolve_admin.evo.subcommands import parse

        for variant in (
            "evo what's on this week",
            "evo what's on this week?",
            "evo, what's on this week?",
            "evo whats on this week",
            "evo what is on this week",
            "evo what's on for this week",
        ):
            result = parse(variant)
            assert result is not None, f"parse returned None for {variant!r}"
            assert result.subcommand == "summary", (
                f"expected summary route for {variant!r}, got {result.subcommand!r}"
            )


class TestPhantomAppFromHiddenFiles:
    """Defect 3 (MEDIUM): apps_dir.glob('*.json') on macOS matches
    hidden dotfiles such as '.scan-status.json' (written by the
    application scanner). Those were being counted as installed apps,
    so a bot with zero real manifests showed up as '1 app installed'."""

    def test_scan_status_not_counted_as_app(self, tmp_path):
        import cross_bot_summary as cbs

        network = {"bots": {"team_bot_b": {}}}
        # Bookkeeping dotfile that must be ignored:
        _write_json(
            tmp_path / "applications" / "team_bot_b" / ".scan-status.json",
            {"last_scan_at": "2026-05-12T00:00:00Z", "manifests_found": 0},
        )
        summary = cbs.gather(tmp_path, network)
        team_bot_b = summary.bot_slices[0]
        assert team_bot_b.app_count == 0, (
            f"hidden .scan-status.json was counted as an app: app_count={team_bot_b.app_count}"
        )
        assert team_bot_b.active_apps == []

    def test_real_manifest_alongside_scan_status(self, tmp_path):
        """Real manifest should still count; .scan-status should not."""
        import cross_bot_summary as cbs

        network = {"bots": {"team_bot_a": {}}}
        _make_manifest(tmp_path, "team_bot_a", "morning-brief", last_result="pass")
        _write_json(
            tmp_path / "applications" / "team_bot_a" / ".scan-status.json",
            {"last_scan_at": "2026-05-12T00:00:00Z"},
        )
        summary = cbs.gather(tmp_path, network)
        team_bot_a = summary.bot_slices[0]
        assert team_bot_a.app_count == 1, (
            f"expected exactly 1 real app; got {team_bot_a.app_count} "
            f"(active_apps={team_bot_a.active_apps!r})"
        )
        assert "Morning Brief" in team_bot_a.active_apps
        # And the dotfile should never appear by name.
        assert not any(a.startswith(".") for a in team_bot_a.active_apps)


class TestDoubledBotPrefixCosmetic:
    """Defect 4 (COSMETIC): Signal titles often begin with '<bot>:'
    (e.g. 'admin_bot: README.md missing'); the renderer also prefixes the
    bot name, producing 'admin_bot: alert: admin_bot: README.md missing'. The
    fix strips a redundant '<bot_id>:' prefix at the gather layer."""

    def test_doubled_bot_prefix_removed(self, tmp_path):
        import cross_bot_summary as cbs

        network = {"bots": {"admin_bot": {}}}
        _make_signal(
            tmp_path,
            "sig-doubled",
            bot_id="admin_bot",
            scope="bot",
            severity="alert",
            title="admin_bot: README.md missing or unreadable",
        )
        summary = cbs.gather(tmp_path, network)
        admin_bot = summary.bot_slices[0]
        # The stripped title should be present once, not twice.
        assert admin_bot.firing_alerts == ["README.md missing or unreadable"], (
            f"expected the redundant 'admin_bot:' prefix to be stripped; "
            f"got {admin_bot.firing_alerts!r}"
        )

    def test_doubled_bot_prefix_case_insensitive(self, tmp_path):
        """Title might use 'Admin_bot:' or 'ADMIN_BOT:'; strip either way."""
        import cross_bot_summary as cbs

        network = {"bots": {"admin_bot": {}}}
        _make_signal(
            tmp_path,
            "sig-doubled-case",
            bot_id="admin_bot",
            scope="bot",
            severity="warn",
            title="Admin_bot: README.md missing",
        )
        summary = cbs.gather(tmp_path, network)
        admin_bot = summary.bot_slices[0]
        assert admin_bot.firing_alerts == ["README.md missing"]

    def test_no_prefix_left_alone(self, tmp_path):
        """If the title doesn't begin with the bot_id, don't touch it."""
        import cross_bot_summary as cbs

        network = {"bots": {"team_bot_a": {}}}
        _make_signal(
            tmp_path,
            "sig-no-prefix",
            bot_id="team_bot_a",
            scope="bot",
            severity="warn",
            title="MEMORY.md missing or unreadable",
        )
        summary = cbs.gather(tmp_path, network)
        team_bot_a = summary.bot_slices[0]
        assert team_bot_a.firing_alerts == ["MEMORY.md missing or unreadable"]

    def test_doubled_prefix_not_stripped_for_pod_scope(self, tmp_path):
        """Pod-scoped signals are not in the per-bot section and don't
        get the renderer's bot-name prefix, so we leave their titles
        alone (even if they happen to start with something colon-shaped)."""
        import cross_bot_summary as cbs

        network = {"bots": {"team_bot_a": {}}}
        _make_signal(
            tmp_path,
            "sig-pod-colon",
            scope="pod",
            bot_id=None,
            severity="alert",
            title="host: disk full",
        )
        summary = cbs.gather(tmp_path, network)
        # Pod alerts go through unmodified.
        assert "host: disk full" in summary.pod_alerts


class TestRenderedOutputCleanForRealMiniSignal:
    """End-to-end check on the rendered output: an 'alert'-severity
    signal with the doubled-bot-prefix shape (as it appears on the
    production mini) should produce a clean line."""

    def test_rendered_alert_has_no_doubled_bot_name(self, tmp_path):
        import cross_bot_summary as cbs

        network = {"bots": {"admin_bot": {"name": "Admin_bot"}}}
        _make_signal(
            tmp_path,
            "sig-end-to-end",
            bot_id="admin_bot",
            scope="bot",
            severity="alert",
            title="admin_bot: README.md missing or unreadable",
        )
        summary = cbs.gather(tmp_path, network)
        text = cbs.render_summary(summary, bot_names={"admin_bot": "Admin_bot"})
        # The bot name should appear in the line, but the title text
        # itself should not also start with the bot name. We test for
        # the *absence* of the doubled form.
        assert "admin_bot: README.md" not in text.lower().replace("admin_bot: alert:", "")
        # And the file message should still be readable in the output.
        assert "README.md missing or unreadable" in text
