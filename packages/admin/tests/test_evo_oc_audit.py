"""tests/test_evo_oc_audit.py — V2.4-3: ``evo security`` / ``evo, what can my bot do?``

Covers:
  1. subcommands.parse() — "what can my bot do?" phrase aliases route to "security"
  2. subcommands.lookup() — "security" subcommand resolves correctly
  3. handlers/oc_audit.render() — happy path with OC audit data
  4. handlers/oc_audit.render() — oc_cli unavailable (graceful fallback)
  5. handlers/oc_audit.render() — OC returns non-dict (graceful fallback)
  6. handlers/oc_audit.render() — score + findings rendering
  7. handlers/oc_audit.render() — findings are normalized (severity demotion)
  8. handlers/oc_audit.render() — all-quiet case (no non-info findings)
  9. Full dispatch integration: "evo, what can my bot do?" → DispatchResult

V2.4-3 design note: the handler replaces the Safety-summary lookup (cut in v2.2
per feedback_safety_summary_less_useful_than_audit.md). It consults the OpenClaw
security audit instead — every finding is a measured result, not an architectural
claim. No LLM calls; no new endpoints; reuses oc_cli.oc_command.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _make_raw_audit(findings=None, ts_ms=0) -> dict:
    """Build a plausible OpenClaw security audit --json response."""
    return {
        "summary": {
            "critical": sum(1 for f in (findings or []) if f.get("severity") == "critical"),
            "warn": sum(1 for f in (findings or []) if f.get("severity") in ("warn", "warning")),
            "info": sum(1 for f in (findings or []) if f.get("severity") == "info"),
        },
        "findings": findings or [],
        "ts": ts_ms,
    }


def _make_finding(
    check_id="tools.exec.test",
    title="A finding",
    severity="warn",
    remediation="",
) -> dict:
    return {
        "checkId": check_id,
        "title": title,
        "severity": severity,
        "remediation": remediation,
    }


def _call_render(bot_id="team_bot_a", raw_audit=None, oc_available=True):
    """Call the oc_audit handler with a mocked runtime.security_audit()."""
    from evolve_admin.evo.handlers.oc_audit import render

    network = {"sharedDir": "/tmp/test-evolve"}
    mock_oc = MagicMock()

    if oc_available and raw_audit is not None:
        mock_oc.security_audit.return_value = raw_audit
    elif oc_available:
        mock_oc.security_audit.return_value = _make_raw_audit()
    else:
        mock_oc = None

    with patch(
        "evolve_admin.evo.handlers.oc_audit._get_runtime",
        return_value=mock_oc,
    ):
        return render(role="primary", bot_id=bot_id, args="", network=network)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Parser — phrase aliases
# ─────────────────────────────────────────────────────────────────────────────


class TestPhraseAliases:
    def test_what_can_my_bot_do_question_mark(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo, what can my bot do?")
        assert result is not None
        assert result.subcommand == "security"
        assert not result.is_bare

    def test_what_can_my_bot_do_no_comma(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo what can my bot do")
        assert result is not None
        assert result.subcommand == "security"

    def test_what_can_this_bot_do(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo what can this bot do")
        assert result is not None
        assert result.subcommand == "security"

    def test_what_can_the_bot_do(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo what can the bot do")
        assert result is not None
        assert result.subcommand == "security"

    def test_what_can_it_do(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo what can it do")
        assert result is not None
        assert result.subcommand == "security"

    def test_uppercase_variant(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("Evo, What Can My Bot Do?")
        assert result is not None
        assert result.subcommand == "security"

    def test_evo_security_single_token(self):
        """Direct `evo security` works via single-token lookup."""
        from evolve_admin.evo.subcommands import parse

        result = parse("evo security")
        assert result is not None
        assert result.subcommand == "security"

    def test_existing_phrases_unaffected(self):
        """The new aliases must not shadow the existing 'what's on this week' phrase."""
        from evolve_admin.evo.subcommands import parse

        result = parse("evo, what's on this week?")
        assert result is not None
        assert result.subcommand == "summary"

    def test_existing_help_unaffected(self):
        from evolve_admin.evo.subcommands import parse

        result = parse("evo help")
        assert result is not None
        assert result.subcommand == "help"

    def test_partial_phrase_does_not_route(self):
        """Prefix match must not fire — only the exact phrase triggers."""
        from evolve_admin.evo.subcommands import parse

        # "what can my bot" is not in the exact list
        result = parse("evo what can my bot")
        assert result is not None
        assert result.subcommand != "security"

    def test_what_can_my_bot_do_with_more_words_does_not_route(self):
        """Extra words after the phrase must not match."""
        from evolve_admin.evo.subcommands import parse

        result = parse("evo what can my bot do today")
        assert result is not None
        assert result.subcommand != "security"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Subcommand registry
# ─────────────────────────────────────────────────────────────────────────────


class TestSubcommandRegistry:
    def test_lookup_security_by_name(self):
        from evolve_admin.evo.subcommands import lookup

        sc = lookup("security")
        assert sc is not None
        assert sc.name == "security"
        assert "oc_audit" in sc.handler

    def test_security_available_to_primary(self):
        from evolve_admin.evo.subcommands import lookup

        sc = lookup("security")
        assert sc is not None
        assert "primary" in sc.available_to

    def test_security_in_primary_subcommands(self):
        from evolve_admin.evo.subcommands import subcommands_for_role

        names = {sc.name for sc in subcommands_for_role("primary")}
        assert "security" in names

    def test_security_in_admin_subcommands(self):
        """Admin is a superset of primary — must also see 'security'."""
        from evolve_admin.evo.subcommands import subcommands_for_role

        names = {sc.name for sc in subcommands_for_role("admin")}
        assert "security" in names


# ─────────────────────────────────────────────────────────────────────────────
# 3. Handler — happy path
# ─────────────────────────────────────────────────────────────────────────────


class TestOcAuditHandlerHappyPath:
    def test_returns_dispatch_result(self):
        from evolve_admin.evo.dispatch import DispatchResult

        r = _call_render()
        assert isinstance(r, DispatchResult)

    def test_subcommand_is_security(self):
        r = _call_render()
        assert r.subcommand == "security"

    def test_mode_is_speak(self):
        r = _call_render()
        assert r.mode == "speak"

    def test_direct_send_message_is_set(self):
        r = _call_render()
        assert r.direct_send_message is not None
        assert len(r.direct_send_message) > 0

    def test_system_append_has_verbatim_preamble(self):
        r = _call_render()
        assert r.system_append is not None
        assert "verbatim" in r.system_append.lower()
        assert "IMPORTANT" in r.system_append

    def test_header_contains_bot_id(self):
        r = _call_render(bot_id="admin_bot")
        body = r.direct_send_message or ""
        assert "admin_bot" in body

    def test_header_contains_score(self):
        raw = _make_raw_audit(findings=[])
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        # Score line present: "Audit score: XX/100"
        assert "Audit score:" in body
        assert "/100" in body

    def test_no_findings_all_quiet(self):
        raw = _make_raw_audit(findings=[])
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        assert "No active issues" in body

    def test_pointers_to_admin_pages(self):
        """Response must mention both Skills and Security pages."""
        raw = _make_raw_audit(findings=[])
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        assert "Skills" in body
        assert "Security" in body


# ─────────────────────────────────────────────────────────────────────────────
# 4. Handler — oc_cli unavailable
# ─────────────────────────────────────────────────────────────────────────────


class TestOcCliUnavailable:
    def test_graceful_fallback_when_oc_cli_missing(self):
        r = _call_render(oc_available=False)
        body = r.direct_send_message or ""
        assert "Security page" in body or "admin dashboard" in body

    def test_subcommand_still_security(self):
        r = _call_render(oc_available=False)
        assert r.subcommand == "security"

    def test_mode_still_speak(self):
        r = _call_render(oc_available=False)
        assert r.mode == "speak"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Handler — OC returns non-dict
# ─────────────────────────────────────────────────────────────────────────────


class TestOcReturnsNonDict:
    def test_fallback_when_oc_returns_none(self):
        from evolve_admin.evo.handlers.oc_audit import render
        from unittest.mock import MagicMock, patch

        mock_oc = MagicMock()
        mock_oc.command.return_value = None  # no result

        with patch("evolve_admin.evo.handlers.oc_audit._get_runtime",
                   return_value=mock_oc):
            r = render(role="primary", bot_id="team_bot_a", args="",
                       network={"sharedDir": "/tmp"})

        body = r.direct_send_message or ""
        assert "Security page" in body or "admin dashboard" in body

    def test_fallback_when_oc_raises(self):
        from evolve_admin.evo.handlers.oc_audit import render
        from unittest.mock import MagicMock, patch

        mock_oc = MagicMock()
        mock_oc.command.side_effect = RuntimeError("timed out")

        with patch("evolve_admin.evo.handlers.oc_audit._get_runtime",
                   return_value=mock_oc):
            r = render(role="primary", bot_id="team_bot_a", args="",
                       network={"sharedDir": "/tmp"})

        body = r.direct_send_message or ""
        # Should include the error message in user-friendly form
        assert "Security page" in body or "problem" in body


# ─────────────────────────────────────────────────────────────────────────────
# 6. Handler — score and findings rendering
# ─────────────────────────────────────────────────────────────────────────────


class TestScoreAndFindingsRendering:
    def test_score_100_when_no_findings(self):
        raw = _make_raw_audit(findings=[])
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        assert "100/100" in body

    def test_score_reduced_by_warning(self):
        """One warning reduces from 100 by 5 → score 95."""
        raw = _make_raw_audit(findings=[
            _make_finding(severity="warn", title="A warning"),
        ])
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        assert "95/100" in body

    def test_score_reduced_by_critical(self):
        """One critical reduces from 100 by 20 → score 80."""
        raw = _make_raw_audit(findings=[
            _make_finding(
                check_id="auth.creds.test",
                severity="critical",
                title="Critical finding",
            ),
        ])
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        assert "80/100" in body

    def test_finding_title_appears_in_body(self):
        raw = _make_raw_audit(findings=[
            _make_finding(severity="warn", title="Token nearing expiry"),
        ])
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        assert "Token nearing expiry" in body

    def test_count_line_appears(self):
        raw = _make_raw_audit(findings=[
            _make_finding(severity="warn", title="Finding one"),
            _make_finding(severity="warn", title="Finding two"),
        ])
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        assert "2 things to look at" in body

    def test_singular_count_line(self):
        raw = _make_raw_audit(findings=[
            _make_finding(severity="warn", title="Only finding"),
        ])
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        assert "1 thing to look at" in body
        # Must NOT say "1 things"
        assert "1 things" not in body

    def test_critical_before_warning_in_output(self):
        """Critical findings appear before warnings."""
        raw = _make_raw_audit(findings=[
            _make_finding(check_id="models.x", severity="warn", title="Warning finding"),
            _make_finding(check_id="auth.x", severity="critical", title="Critical finding"),
        ])
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        assert body.index("Critical finding") < body.index("Warning finding")

    def test_info_findings_not_surfaced_as_issues(self):
        """Info-severity findings don't appear in the 'things to look at' section."""
        raw = _make_raw_audit(findings=[
            _make_finding(severity="info", title="Just an FYI"),
        ])
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        # Info-only → all-quiet path
        assert "No active issues" in body
        # The FYI title may or may not appear, but "things to look at" must not.
        assert "things to look at" not in body

    def test_max_eight_findings_shown(self):
        """At most 8 findings shown inline; overflow gets a count note."""
        findings = [
            _make_finding(
                check_id=f"config.test.{i}",
                severity="warn",
                title=f"Finding number {i}",
            )
            for i in range(12)
        ]
        raw = _make_raw_audit(findings=findings)
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        # Overflow line present
        assert "more" in body
        # Score accounts for all 12 warnings: 100 - 12*5 = 40
        assert "40/100" in body

    def test_recommendation_appears_in_finding(self):
        raw = _make_raw_audit(findings=[
            _make_finding(
                severity="warn",
                title="Token close to expiry",
                remediation="Rotate the token via the Integrations page.",
            ),
        ])
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        assert "Rotate the token" in body

    def test_score_label_excellent(self):
        raw = _make_raw_audit(findings=[])
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        assert "excellent" in body

    def test_score_label_needs_attention(self):
        """A mid-range score (10 warnings → 50/100) labels as 'needs attention'."""
        findings = [
            _make_finding(severity="warn", title=f"W{i}")
            for i in range(10)
        ]
        raw = _make_raw_audit(findings=findings)
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        # 100 - 10*5 = 50 → "needs attention" (50 < 55 threshold for "fair")
        assert "50/100" in body
        assert "needs attention" in body


# ─────────────────────────────────────────────────────────────────────────────
# 7. Handler — severity normalization
# ─────────────────────────────────────────────────────────────────────────────


class TestSeverityNormalization:
    def test_exec_security_full_warning_surfaces_as_warning(self):
        """The 'exec security=full' warning is no longer demoted — full mode
        is not the deploy baseline anymore (bots get deny or allowlist).
        A full-mode finding from the OC audit should reach the operator."""
        raw = _make_raw_audit(findings=[
            _make_finding(
                check_id="tools.exec.test",
                severity="warn",
                title='tools.exec.security="full" — consider using an allowlist',
                remediation="Set exec allowlist.",
            ),
        ])
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        # No longer demoted → shows up as a real warning, not all-quiet
        assert "No active issues" not in body

    def test_multi_user_prose_warning_demoted(self):
        """Generic multi-user advisory findings are demoted to info."""
        raw = _make_raw_audit(findings=[
            _make_finding(
                check_id="gateway.multi",
                severity="warn",
                title="Multi-user access detected — review shared config",
            ),
        ])
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        assert "No active issues" in body

    def test_real_warning_not_demoted(self):
        """A genuine warning that doesn't match demotion rules surfaces."""
        raw = _make_raw_audit(findings=[
            _make_finding(
                check_id="auth.token.test",
                severity="warn",
                title="API token nearing expiry",
            ),
        ])
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        assert "API token nearing expiry" in body
        # "1 thing to look at" (singular) — must not say "No active issues"
        assert "No active issues" not in body
        assert "thing to look at" in body

    def test_warn_normalized_from_oc_severity(self):
        """OC emits 'warn'; handler normalizes to 'warning' internally.
        Either way, the finding must surface as a non-info result."""
        raw = _make_raw_audit(findings=[
            _make_finding(
                check_id="auth.key.test",
                severity="warn",  # raw OC value
                title="Key rotation overdue",
            ),
        ])
        r = _call_render(raw_audit=raw)
        body = r.direct_send_message or ""
        assert "Key rotation overdue" in body


# ─────────────────────────────────────────────────────────────────────────────
# 8. Handler — score helper unit tests
# ─────────────────────────────────────────────────────────────────────────────


class TestScoreHelpers:
    def test_score_from_no_findings(self):
        from evolve_admin.evo.handlers.oc_audit import _score_from_findings

        assert _score_from_findings([]) == 100

    def test_score_floored_at_zero(self):
        from evolve_admin.evo.handlers.oc_audit import _score_from_findings

        # 6 crits × 20 = 120, but score is floored at 0
        findings = [{"severity": "critical"}] * 6
        assert _score_from_findings(findings) == 0

    def test_score_label_mapping(self):
        from evolve_admin.evo.handlers.oc_audit import _score_label

        assert _score_label(100) == "excellent"
        assert _score_label(90) == "excellent"
        assert _score_label(89) == "good"
        assert _score_label(75) == "good"
        assert _score_label(74) == "fair"
        assert _score_label(55) == "fair"
        assert _score_label(54) == "needs attention"
        assert _score_label(35) == "needs attention"
        assert _score_label(34) == "has issues"
        assert _score_label(0) == "has issues"


# ─────────────────────────────────────────────────────────────────────────────
# 9. Full dispatch integration
# ─────────────────────────────────────────────────────────────────────────────


class TestDispatchIntegration:
    def _dispatch(self, raw_text: str, raw_audit_result=None, oc_available=True):
        from evolve_admin.evo.dispatch import dispatch

        network = {"sharedDir": "/tmp/evolve-test"}
        mock_oc = MagicMock()
        if oc_available and raw_audit_result is not None:
            mock_oc.command.return_value = raw_audit_result
        elif oc_available:
            mock_oc.command.return_value = _make_raw_audit()
        else:
            mock_oc = None

        with patch(
            "evolve_admin.evo.handlers.oc_audit._get_runtime",
            return_value=mock_oc,
        ):
            return dispatch(
                network,
                bot_id="team_bot_a",
                channel="telegram",
                sender_external_id="12345",
                raw_text=raw_text,
            )

    def test_phrase_alias_routes_to_oc_audit(self):
        r = self._dispatch("evo, what can my bot do?")
        assert r.subcommand == "security"
        assert r.mode == "speak"

    def test_evo_security_routes_to_oc_audit(self):
        r = self._dispatch("evo security")
        assert r.subcommand == "security"

    def test_dispatch_result_has_direct_send_message(self):
        r = self._dispatch("evo, what can my bot do?")
        assert r.direct_send_message is not None
        # Must not include the "respond verbatim" preamble in the direct body
        assert "IMPORTANT" not in (r.direct_send_message or "")
        assert "verbatim" not in (r.direct_send_message or "").lower()

    def test_dispatch_result_has_system_append(self):
        r = self._dispatch("evo security")
        assert r.system_append is not None
        assert "verbatim" in (r.system_append or "").lower()

    def test_what_can_this_bot_do_variant(self):
        r = self._dispatch("evo what can this bot do")
        assert r.subcommand == "security"

    def test_unrelated_phrase_not_routed(self):
        """'evo what can you do' (not in the alias list) must not route to security."""
        r = self._dispatch("evo what can you do")
        assert r.subcommand != "security"
