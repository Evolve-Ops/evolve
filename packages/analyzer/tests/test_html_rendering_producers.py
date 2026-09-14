r"""tests/test_html_rendering_producers.py — Markdown→HTML producer fix.

Three analyzer-side producers were generating Telegram messages with
Markdown ``*bold*`` / ``\`code\``` markers, but the dispatcher switched
to ``parse_mode="HTML"`` per the dispatcher-safety rework (PR #1393).
Under HTML mode, ``*X*`` renders as literal asterisks — the bug
surfaced by the daily-report screenshot on 2026-05-22.

Producers under test:

  * ``analyzer/report.py:build_report`` — daily pod report
    (Reports → Subscriptions → Messages, ``reports.adhoc_analyzer``).
  * ``analyzer/heal.py`` watchdog "Gateway Instability" alert.
  * ``analyzer/audit.py`` CRITICAL findings batch.

The pin: each rendered output must (a) contain ``<b>...</b>`` for
emphasis, (b) NOT contain Markdown ``*X*`` for the same content, and
(c) pass the dispatcher's HTML validator so Telegram parse_mode=HTML
doesn't 400 on send.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _path in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# ── Shared helpers ─────────────────────────────────────────────────────────


def _validate_html(message: str) -> tuple[bool, str | None]:
    """Re-export the dispatcher's HTML validator so analyzer-side tests
    can assert against the same shape Telegram will accept on the wire."""
    from evolve_admin.alerts.dispatcher import validate_html_message
    return validate_html_message(message)


def _assert_no_markdown_bold(message: str, *, context: str) -> None:
    """Pin that the producer doesn't emit Markdown ``*X*`` for bold.

    Allow standalone ``*`` characters (inside URLs, error messages,
    chip details) — only flag the matched-pair pattern that Markdown
    interprets as bold. The dispatcher renders interpolated values
    via html_escape, which is a no-op for literal ``*``, so a single
    asterisk in a payload string survives unchanged and that's fine.
    """
    # Markdown-bold pattern: two asterisks delimiting a non-empty run
    # of non-asterisk, non-newline characters. Same syntactic shape
    # Telegram's Markdown parser used to consume — and now ignores under
    # HTML mode, leaving the asterisks visible.
    pattern = re.compile(r"\*[^*\n]+\*")
    matches = pattern.findall(message)
    assert not matches, (
        f"{context} emits Markdown-bold ``*X*`` under HTML mode:\n"
        f"  matches: {matches!r}\n"
        f"  full message:\n{message}"
    )


# ── pod_report (consolidated daily report, 2026-06-05) ─────────────────────
#
# These tests replaced the equivalent ``report.build_report`` tests when
# the per-bot daily digest was merged into pod_report. Each test pins
# one invariant carried over from the old daily Bot digest:
#
#   * HTML rendering uses <b>...</b>, not Markdown
#   * Per-bot chips are filtered by ``digest_tier`` ∈
#     {critical_now, today_news}; tile_only chips are dropped
#   * The pod usage line renders at the top with optional ↑/↓ delta
#   * New-recommendations queue line surfaces proposals created since
#     ``state.json::last_run_iso``; first-run shows nothing
#   * The "🟢 All clear · ..." empty-state was retired
#   * Bot ids are HTML-escaped


def _pod_report_module():
    """Import the pod_report producer the same way the launchd job does."""
    import importlib
    import sys as _sys
    if "pod_report" in _sys.modules:
        return importlib.reload(_sys.modules["pod_report"])
    return importlib.import_module("pod_report")


def _stub_tile(monkeypatch, fake_tile):
    """Monkeypatch tile_metrics.compute_tile_data (used by pod_report's
    new bot-chip collector) so tests don't need real shared_dir state."""
    pod_report = _pod_report_module()
    monkeypatch.setattr(pod_report, "_compute_bot_chips_safely",
                        lambda shared_dir, bot_id, spec, network:
                            fake_tile(bot_id).get("health_chips") or [])
    # The pod-usage helper reads from metrics snapshots; stub it to
    # return a deterministic header line for these tests.
    monkeypatch.setattr(pod_report, "render_pod_usage_line",
                        lambda *a, **kw: "Pod: 287 turns 2026-06-04, $4.83 ($21.18 7d)")
    # The audit + gateway + metrics-outage collectors read shared_dir
    # files; stub them out so the test focuses on chip rendering.
    monkeypatch.setattr(pod_report, "collect_broken",
                        lambda *a, **kw: [])
    monkeypatch.setattr(pod_report, "collect_trending",
                        lambda *a, **kw: [])
    monkeypatch.setattr(pod_report, "collect_queue",
                        lambda *a, **kw: [])
    # New-recs by default: none. Tests that need recs override this.
    monkeypatch.setattr(pod_report, "collect_new_recommendations",
                        lambda *a, **kw: [])
    monkeypatch.setattr(pod_report, "_empty_state_summary",
                        lambda *a, **kw: "")
    monkeypatch.setattr(pod_report, "_find_last_non_green",
                        lambda *a, **kw: None)
    return pod_report


def _build_pod_report(monkeypatch, *, members, fake_tile=None,
                      new_recs=None, pod_usage="Pod: 287 turns 2026-06-04, $4.83"):
    """Run pod_report.run_report against stubbed collectors and return
    the rendered (text, overall, structured) tuple."""
    fake_tile = fake_tile or (lambda bot_id: {"health_chips": []})
    pod_report = _stub_tile(monkeypatch, fake_tile)
    if pod_usage is not None:
        monkeypatch.setattr(pod_report, "render_pod_usage_line",
                            lambda *a, **kw: pod_usage)
    if new_recs is not None:
        from pod_report import ReportLine
        monkeypatch.setattr(
            pod_report, "collect_new_recommendations",
            lambda *a, **kw: [ReportLine(
                bucket="queue", severity="info",
                text=new_recs, deeplink="/admin/improvements",
                signal_type="new_recommendations",
            )] if new_recs else [],
        )
    from pathlib import Path
    from datetime import datetime, timezone
    return pod_report.run_report(
        Path("/tmp/nonexistent"), members, pod_report.DEFAULT_OVERRIDES,
        label="Daily",
        now=datetime(2026, 6, 5, 8, 0, tzinfo=timezone.utc),
        config={"bots": {b: {"role": "member"} for b in members}},
    )


def test_pod_report_pod_usage_line_at_top(monkeypatch):
    """The Pod usage line is the first body line — the operator's
    fastest-skim header for the consolidated daily report."""
    text, _, _ = _build_pod_report(
        monkeypatch, members=["team_bot_a"],
        pod_usage="Pod: 287 turns 2026-06-04, $4.83 ($21.18 7d, ↑49% vs prior week)",
    )
    assert text.startswith("Pod: 287 turns 2026-06-04"), text


def test_pod_report_filters_bot_chips_by_digest_tier(monkeypatch):
    """``collect_bot_chips`` only forwards chips with ``digest_tier`` ∈
    {critical_now, today_news}. tile_only chips (standing setup, 7d
    aggregates) are dropped so they don't repeat in the daily report."""
    def fake_tile(bot_id):
        if bot_id != "security_bot":
            return {"health_chips": []}
        return {"health_chips": [
            {"id": "breaker_tripped", "severity": "critical",
             "label": "breaker tripped",
             "detail": "L1 cost breaker active",
             "horizon": "ongoing", "digest_tier": "critical_now"},
            {"id": "cost_spike", "severity": "warn", "label": "cost spike",
             "detail": "$5 today vs $1 yesterday",
             "horizon": "today", "digest_tier": "today_news"},
            # tile_only — must NOT appear in the rendered report
            {"id": "pair_whatsapp", "severity": "warn", "label": "Pair WhatsApp ID",
             "detail": "open pairing wizard",
             "horizon": "ongoing", "digest_tier": "tile_only"},
            {"id": "cost_spike", "severity": "warn", "label": "cost spike",
             "detail": "$7.87 vs $0.97 prior 7d",
             "horizon": "7d", "digest_tier": "tile_only"},
            # Missing digest_tier defaults to tile_only — DROP
            {"id": "legacy", "severity": "warn",
             "label": "legacy chip", "detail": "no digest_tier"},
            # gateway_down is handled by pod_report.collect_broken and
            # is explicitly skipped by collect_bot_chips to avoid
            # double-counting
            {"id": "gateway_down", "severity": "critical",
             "label": "gateway down", "detail": "unreachable",
             "horizon": "ongoing", "digest_tier": "critical_now"},
        ]}

    # We need real collect_bot_chips here — undo the stub from _stub_tile.
    pod_report = _pod_report_module()
    monkeypatch.setattr(pod_report, "_compute_bot_chips_safely",
                        lambda shared_dir, bot_id, spec, network:
                            fake_tile(bot_id).get("health_chips") or [])
    monkeypatch.setattr(pod_report, "render_pod_usage_line", lambda *a, **kw: "")
    monkeypatch.setattr(pod_report, "collect_broken", lambda *a, **kw: [])
    monkeypatch.setattr(pod_report, "collect_trending", lambda *a, **kw: [])
    monkeypatch.setattr(pod_report, "collect_queue", lambda *a, **kw: [])
    monkeypatch.setattr(pod_report, "collect_new_recommendations",
                        lambda *a, **kw: [])
    monkeypatch.setattr(pod_report, "_empty_state_summary", lambda *a, **kw: "")
    monkeypatch.setattr(pod_report, "_find_last_non_green", lambda *a, **kw: None)

    from datetime import datetime, timezone
    from pathlib import Path
    text, _, _ = pod_report.run_report(
        Path("/tmp/nonexistent"), ["security_bot"], pod_report.DEFAULT_OVERRIDES,
        label="Daily",
        now=datetime(2026, 6, 5, 8, 0, tzinfo=timezone.utc),
        config={"bots": {"security_bot": {"role": "member"}}},
    )

    # critical_now + today_news survive
    assert "breaker tripped" in text, text
    assert "today vs $1" in text, text
    # tile_only and unmarked-default chips DROPPED
    assert "Pair WhatsApp" not in text, text
    assert "prior 7d" not in text, text
    assert "legacy chip" not in text, text
    # gateway_down is excluded because pod_report covers it elsewhere
    assert "gateway down" not in text, text


def test_pod_report_renders_html_bold_not_markdown(monkeypatch):
    """No Markdown asterisks in the rendered body — Telegram HTML
    mode would render them as literal characters."""
    text, _, _ = _build_pod_report(monkeypatch, members=["team_bot_a"])
    _assert_no_markdown_bold(text, context="pod_report.run_report")


def test_pod_report_no_all_clear_empty_state(monkeypatch):
    """The "🟢 All clear · {empty_summary}" line was retired 2026-06-05
    (operator principle: silence means clear; listing clears is noise).
    Quiet runs return a body that may be the pod-usage line alone."""
    text, overall, _ = _build_pod_report(
        monkeypatch, members=["team_bot_a"],
        pod_usage="Pod: 12 turns 2026-06-04, $0.10",
    )
    assert "🟢 All clear" not in text
    assert "All clear" not in text
    # Overall is still "green" — that classification feeds the admin
    # UI's status pill but isn't rendered as a body line anymore.
    assert overall == "green"


def test_pod_report_new_recommendations_line(monkeypatch):
    """When ``collect_new_recommendations`` returns a line, it appears
    in the rendered body under the queue (📋) emoji."""
    text, _, _ = _build_pod_report(
        monkeypatch, members=["team_bot_a"],
        new_recs="New recommendations: team_bot_b: rotate API key (90d), admin_bot: lower cap +1 more",
    )
    assert "📋 New recommendations:" in text
    assert "team_bot_b: rotate API key (90d)" in text
    assert "+1 more" in text


def test_pod_report_escapes_bot_ids_with_angle_brackets(monkeypatch):
    """A pathological bot_id with <>& must survive Telegram's HTML
    parser. Per-bot chip rendering goes through pod_report's chip
    pipeline, but the report body itself is plain text + emoji + the
    structured chip text. The catalog body_template renders the title
    line. HTML escaping happens via the dispatcher; verify the chip
    text doesn't break when included verbatim."""
    def fake_tile(bot_id):
        return {"health_chips": [
            {"id": "breaker_tripped", "severity": "critical",
             "label": "breaker tripped", "detail": "active",
             "horizon": "ongoing", "digest_tier": "critical_now"},
        ]}

    pod_report = _pod_report_module()
    monkeypatch.setattr(pod_report, "_compute_bot_chips_safely",
                        lambda shared_dir, bot_id, spec, network:
                            fake_tile(bot_id).get("health_chips") or [])
    monkeypatch.setattr(pod_report, "render_pod_usage_line", lambda *a, **kw: "")
    monkeypatch.setattr(pod_report, "collect_broken", lambda *a, **kw: [])
    monkeypatch.setattr(pod_report, "collect_trending", lambda *a, **kw: [])
    monkeypatch.setattr(pod_report, "collect_queue", lambda *a, **kw: [])
    monkeypatch.setattr(pod_report, "collect_new_recommendations",
                        lambda *a, **kw: [])
    monkeypatch.setattr(pod_report, "_empty_state_summary", lambda *a, **kw: "")
    monkeypatch.setattr(pod_report, "_find_last_non_green", lambda *a, **kw: None)

    from datetime import datetime, timezone
    from pathlib import Path
    bad_id = "weird<bot>name"
    text, _, _ = pod_report.run_report(
        Path("/tmp/nonexistent"), [bad_id], pod_report.DEFAULT_OVERRIDES,
        label="Daily",
        now=datetime(2026, 6, 5, 8, 0, tzinfo=timezone.utc),
        config={"bots": {bad_id: {"role": "member"}}},
    )
    # Chip text includes the raw bot_id — escaping happens at dispatch
    # time. The test pins the body literal so we know the chip line
    # was emitted; the dispatcher.send catalog-render path escapes it
    # before Telegram sees it (see test_render_event_escapes_dangerous_
    # payload_values in test_alerts_catalog.py).
    assert bad_id in text or "weird&lt;bot&gt;name" in text


# Sentinel — old "report" symbol intentionally absent; if a future
# import tries to bring it back we want a clear failure to surface
# the consolidation.
def test_pod_report_legacy_report_module_is_gone():
    """``packages/analyzer/report.py`` was retired 2026-06-05; the
    consolidated daily report lives entirely in pod_report.py. This
    guard catches accidental re-introduction."""
    import importlib
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("report")



# ── weekly_bot_trends.build_report ──────────────────────────────────────────


def _trend_chip(*, id: str, label: str, detail: str, horizon: str,
                severity: str = "warn") -> dict:
    """Construct a chip the way tile_metrics emits one, including the
    ``horizon`` field PR #1996 added so weekly_bot_trends can filter on
    it. Fixtures stay self-contained so the test doesn't need to wait on
    upstream chip changes — it pins the filter contract directly.
    """
    return {
        "id": id, "label": label, "detail": detail,
        "severity": severity, "horizon": horizon,
    }


def _tile(*, turns_7d=0, turns_prior=0, usd_7d=0.0, usd_prior=0.0, chips=None):
    """Build the subset of a tile_metrics block weekly_bot_trends reads:
    current + prior 7d windows for turns/cost, plus health_chips. The
    redesigned producer (2026-06-18) computes week-over-week deltas, so
    fixtures must carry the ``*_prior_7d`` fields the bare-checkmark
    version ignored."""
    return {
        "health_chips": chips or [],
        "cost": {"usd_7d": usd_7d, "usd_prior_7d": usd_prior},
        "activity": {"turns_7d": turns_7d, "turns_prior_7d": turns_prior},
    }


def test_weekly_bot_trends_filters_to_7d_chips(monkeypatch):
    """Only ``horizon: "7d"`` chips reach the weekly digest. ``today``
    and ``ongoing`` chips are the daily report's territory; they must
    not appear here, or a Monday cost spike would re-fire in both the
    Monday daily and the Sunday weekly (the duplicate-alert bug PR
    #1996 set out to prevent)."""
    import weekly_bot_trends

    def fake_tile(*, shared_dir, bot_id, bot_data, network):
        if bot_id == "team_bot_a":
            return _tile(
                turns_7d=199, turns_prior=190, usd_7d=25.44, usd_prior=11.40,
                chips=[
                    # 7d trend — should appear in the weekly.
                    _trend_chip(
                        id="cost_spike", label="cost spike",
                        detail="$25.44 vs $11.40 prior 7d", horizon="7d",
                    ),
                    # today chip — daily territory, must be filtered out.
                    _trend_chip(
                        id="cost_spike", label="cost spike",
                        detail="$12.10 today vs $3.40 yesterday",
                        horizon="today",
                    ),
                    # ongoing chip — daily territory, must be filtered out.
                    _trend_chip(
                        id="scan_needed", label="scan needed",
                        detail="last scanned 9d ago", horizon="ongoing",
                    ),
                ],
            )
        # admin_bot: flat, no chips → steady (collapses to a count line).
        return _tile(turns_7d=50, turns_prior=50, usd_7d=2.0, usd_prior=2.0)

    monkeypatch.setattr(weekly_bot_trends, "compute_tile_data", fake_tile)
    network = {"bots": {"team_bot_a": {"role": "member"},
                        "admin_bot": {"role": "member"}}}
    message, should_send = weekly_bot_trends.build_report(
        shared_dir="/tmp/nonexistent", network=network,
    )
    assert should_send

    # The 7d chip is on the line under team_bot_a.
    assert "$25.44 vs $11.40 prior 7d" in message
    # The today + ongoing chips are gone — verify their distinctive
    # detail strings did not survive into the rendered message.
    assert "today vs $3.40 yesterday" not in message
    assert "scan needed" not in message
    assert "last scanned 9d ago" not in message

    # admin_bot is steady — collapsed into the count line, not named.
    assert "Movers:" in message
    assert "1 other steady" in message
    assert "admin_bot" not in message


def test_weekly_bot_trends_silent_when_nothing_moves(monkeypatch):
    """No mover (no material WoW delta, no 7d chip) → no message. The
    weekly intentionally does not fire a green "all clear" just to confirm
    nothing is wrong (operator-message-style.md: "silence is default")."""
    import weekly_bot_trends

    def fake_tile(*, shared_dir, bot_id, bot_data, network):
        # Only ongoing chips (filtered) and flat turns/cost → not a mover.
        return _tile(
            turns_7d=50, turns_prior=50, usd_7d=5.0, usd_prior=5.0,
            chips=[_trend_chip(id="scan_needed", label="scan needed",
                               detail="last scanned 9d ago", horizon="ongoing")],
        )

    monkeypatch.setattr(weekly_bot_trends, "compute_tile_data", fake_tile)
    network = {"bots": {"team_bot_a": {"role": "member"},
                        "admin_bot": {"role": "member"}}}
    _msg, should_send = weekly_bot_trends.build_report(
        shared_dir="/tmp/nonexistent", network=network,
    )
    assert should_send is False


def test_weekly_bot_trends_uses_html_bold_not_markdown(monkeypatch):
    """Header + section labels use ``<b>...</b>``; no Markdown
    asterisks survive. Validates against the dispatcher's HTML parser
    — same gate the daily report's renderer respects.
    """
    import weekly_bot_trends

    def fake_tile(*, shared_dir, bot_id, bot_data, network):
        if bot_id == "team_bot_a":
            return _tile(
                turns_7d=199, turns_prior=120, usd_7d=25.44, usd_prior=11.40,
                chips=[_trend_chip(
                    id="high_correction", label="pushback",
                    detail="14% of turns this week — typical <10%",
                    horizon="7d",
                )],
            )
        return _tile(turns_7d=10, turns_prior=10, usd_7d=1.0, usd_prior=1.0)

    monkeypatch.setattr(weekly_bot_trends, "compute_tile_data", fake_tile)
    network = {"bots": {"team_bot_a": {"role": "member"},
                        "admin_bot": {"role": "member"}}}
    message, should_send = weekly_bot_trends.build_report(
        shared_dir="/tmp/nonexistent", network=network,
    )
    assert should_send

    assert "<b>Bot trends</b>" in message
    assert "<b>Movers:</b>" in message
    assert "<b>team_bot_a</b>" in message

    _assert_no_markdown_bold(message, context="weekly_bot_trends.build_report")

    ok, err = _validate_html(message)
    assert ok, (
        f"weekly_bot_trends HTML validation failed: {err}\n"
        f"message:\n{message}"
    )


def test_weekly_bot_trends_renders_wow_deltas_and_pod_rollup(monkeypatch):
    """The redesign's headline: a pod rollup line with WoW deltas + a
    biggest-mover callout, and per-bot mover lines carrying ↑/↓ deltas.
    This is the content the old bare-checkmark report threw away."""
    import weekly_bot_trends

    def fake_tile(*, shared_dir, bot_id, bot_data, network):
        if bot_id == "team_bot_a":
            return _tile(turns_7d=396, turns_prior=392, usd_7d=30.08,
                         usd_prior=13.43)
        if bot_id == "team_bot_b":
            return _tile(turns_7d=210, turns_prior=152, usd_7d=8.20,
                         usd_prior=7.10)
        return _tile(turns_7d=40, turns_prior=40, usd_7d=2.0, usd_prior=2.0)

    monkeypatch.setattr(weekly_bot_trends, "compute_tile_data", fake_tile)
    network = {"bots": {"team_bot_a": {"role": "member"},
                        "team_bot_b": {"role": "member"},
                        "team_bot_c": {"role": "member"}}}
    message, should_send = weekly_bot_trends.build_report(
        shared_dir="/tmp/nonexistent", network=network,
    )
    assert should_send, message

    # Pod rollup: totals + WoW deltas + the "vs prior 7d" anchor.
    assert "Pod:" in message
    assert "vs prior 7d" in message
    # team_bot_a's cost more than doubled → biggest mover, by cost.
    assert "Biggest mover: team_bot_a (cost ↑" in message, message
    # team_bot_a (no prior-window chip) is still a mover via its cost
    # delta and carries a ↑ token on its line.
    assert "<b>team_bot_a</b>" in message
    assert "↑" in message
    # team_bot_c is flat → collapsed into the steady count, not named.
    assert "1 other steady" in message
    assert "team_bot_c" not in message


def test_weekly_bot_trends_mover_by_delta_without_chip(monkeypatch):
    """A bot with no 7d chip but a material cost/turns swing is still a
    mover — the report is about trends, not just chips."""
    import weekly_bot_trends

    def fake_tile(*, shared_dir, bot_id, bot_data, network):
        # +38% turns, well past the 10%/20-turn floor; no chips.
        return _tile(turns_7d=210, turns_prior=152, usd_7d=8.2, usd_prior=7.1)

    monkeypatch.setattr(weekly_bot_trends, "compute_tile_data", fake_tile)
    network = {"bots": {"team_bot_b": {"role": "member"}}}
    message, should_send = weekly_bot_trends.build_report(
        shared_dir="/tmp/nonexistent", network=network,
    )
    assert should_send
    assert "<b>team_bot_b</b>" in message
    assert "↑38%" in message


def test_weekly_bot_trends_floor_suppresses_tiny_absolute(monkeypatch):
    """A huge percentage on a trivial absolute change ($0.10 → $0.40,
    +300%) must NOT make a bot a mover — the absolute floor guards
    against percent-noise on near-zero baselines."""
    import weekly_bot_trends

    def fake_tile(*, shared_dir, bot_id, bot_data, network):
        return _tile(turns_7d=3, turns_prior=1, usd_7d=0.40, usd_prior=0.10)

    monkeypatch.setattr(weekly_bot_trends, "compute_tile_data", fake_tile)
    network = {"bots": {"quiet_bot": {"role": "member"}}}
    _msg, should_send = weekly_bot_trends.build_report(
        shared_dir="/tmp/nonexistent", network=network,
    )
    assert should_send is False


def test_weekly_bot_trends_escapes_bot_ids_with_angle_brackets(monkeypatch):
    """A bot_id with ``<>&`` survives the HTML parser instead of
    breaking the message — same shape as the daily report's
    escaping test (PR #1393 dispatcher-safety rework)."""
    import weekly_bot_trends

    def fake_tile(*, shared_dir, bot_id, bot_data, network):
        return _tile(
            turns_7d=199, turns_prior=120, usd_7d=25.44, usd_prior=11.40,
            chips=[_trend_chip(
                id="cost_spike", label="cost spike",
                detail="$25.44 vs $11.40 prior 7d", horizon="7d",
            )],
        )

    monkeypatch.setattr(weekly_bot_trends, "compute_tile_data", fake_tile)
    network = {"bots": {"weird<bot>name": {"role": "member"}}}
    message, _ = weekly_bot_trends.build_report(
        shared_dir="/tmp/nonexistent", network=network,
    )
    assert "weird&lt;bot&gt;name" in message
    assert "<bot>" not in message
    ok, err = _validate_html(message)
    assert ok, f"escaped-bot-id message must still validate: {err}"


def test_weekly_bot_trends_chip_missing_horizon_is_excluded(monkeypatch):
    """Defensive: a chip without a ``horizon`` field is treated as
    not-7d (excluded). With flat turns/cost the bot has no other reason
    to be a mover, so the malformed chip leaking would be the only way
    it could send — pinning should_send=False proves the exclusion."""
    import weekly_bot_trends

    def fake_tile(*, shared_dir, bot_id, bot_data, network):
        return _tile(
            turns_7d=50, turns_prior=50, usd_7d=5.0, usd_prior=5.0,
            chips=[
                # No "horizon" key at all.
                {"id": "cost_spike", "label": "cost spike",
                 "detail": "$5.00 vs $5.00 prior 7d", "severity": "warn"},
            ],
        )

    monkeypatch.setattr(weekly_bot_trends, "compute_tile_data", fake_tile)
    network = {"bots": {"team_bot_a": {"role": "member"}}}
    _msg, should_send = weekly_bot_trends.build_report(
        shared_dir="/tmp/nonexistent", network=network,
    )
    assert should_send is False


def test_weekly_bot_trends_sentinel_blocks_second_send_same_week(tmp_path):
    """The ISO-week sentinel records a send and reports the same week as
    already-sent — the producer-level guard against launchd make-up
    fires and manual re-runs (2026-06-18 triple-fire)."""
    import weekly_bot_trends

    sd = str(tmp_path)
    assert weekly_bot_trends._already_sent_this_week(sd, "2026-W25") is False
    weekly_bot_trends._mark_sent_this_week(sd, "2026-W25")
    assert weekly_bot_trends._already_sent_this_week(sd, "2026-W25") is True
    # A different week is not blocked.
    assert weekly_bot_trends._already_sent_this_week(sd, "2026-W26") is False


def test_weekly_bot_trends_sentinel_fails_open_on_missing_dir(tmp_path):
    """Unreadable/absent sentinel → treated as not-yet-sent (fail open):
    a rare duplicate beats a silently-suppressed weekly."""
    import weekly_bot_trends

    missing = tmp_path / "does" / "not" / "exist"
    assert weekly_bot_trends._already_sent_this_week(str(missing), "2026-W25") is False


# ── heal.py watchdog alert ──────────────────────────────────────────────────


def test_heal_gateway_instability_source_uses_html():
    """The heal.py source no longer contains the Markdown-bold header
    or Markdown-code bot-id wrappers — every dispatcher-bound producer
    is HTML now."""
    heal_path = _ANALYZER_DIR / "heal.py"
    src = heal_path.read_text(encoding="utf-8")
    # Header must be HTML; the Markdown form must not appear at the
    # producer call site at all.
    assert "<b>Evolve: Gateway Instability</b>" in src, (
        "heal.py should emit HTML <b> for the Gateway Instability header"
    )
    assert "*Evolve: Gateway Instability*" not in src, (
        "heal.py should not retain the Markdown *X* form"
    )
    # The bot-id wrappers were ``` `{b}` ``` (Markdown code). HTML
    # equivalent is <code>{html_escape(b)}</code>.
    assert "<code>" in src and "_html_escape(b)" in src, (
        "heal.py should wrap bot ids with <code>{_html_escape(b)}</code>"
    )


def test_heal_constructed_watchdog_message_validates_as_html():
    """Construct a representative watchdog message the way heal.py
    does and pin that the dispatcher's HTML validator accepts it."""
    from heal import _html_escape

    ranked = [
        ("team_bot_a", {"gateway_down": 5, "restart_failed": 2,
                  "last_error": "connection refused"}),
        ("admin_bot", {"gateway_down": 3, "restart_failed": 0,
                    "last_error": None}),
    ]
    affected = [b for b, _ in ranked]
    window_hours = 24
    msg_lines = [
        "⚠️ <b>Evolve: Gateway Instability</b>",
        f"{len(affected)} bot(s) affected in last {window_hours}h:",
    ]
    for b, d in ranked:
        line = (
            f"  • <code>{_html_escape(b)}</code>: "
            f"{d['gateway_down']}× down, {d['restart_failed']} restart-fail"
        )
        if d.get("last_error"):
            line += f"\n    last error: {_html_escape(d['last_error'])}"
        msg_lines.append(line)
    msg_lines.append("Check the Alerts page in admin UI for details.")
    message = "\n".join(msg_lines)

    ok, err = _validate_html(message)
    assert ok, f"heal watchdog message must validate: {err}\nmessage:\n{message}"
    _assert_no_markdown_bold(message, context="heal watchdog message")


# ── audit.py CRITICAL alert ─────────────────────────────────────────────────


def test_audit_critical_source_uses_html():
    """The audit.py CRITICAL findings header is HTML now."""
    audit_path = _ANALYZER_DIR / "audit.py"
    src = audit_path.read_text(encoding="utf-8")
    assert "<b>Evolve Security Audit — CRITICAL Findings</b>" in src
    assert "*Evolve Security Audit — CRITICAL Findings*" not in src


def test_audit_constructed_critical_message_validates_as_html():
    """Reconstruct a CRITICAL batch the way audit.py builds it and
    verify the result passes HTML validation."""
    from audit import _html_escape

    class _Finding:
        def __init__(self, message: str) -> None:
            self.message = message

    criticals = [
        _Finding("team_bot_a: openclaw.json drift detected — gateway.auth removed"),
        _Finding("admin_bot: AGENTS.md missing required <ops> section"),
    ]
    lines = ["🔴 <b>Evolve Security Audit — CRITICAL Findings</b>", ""]
    for c in criticals:
        lines.append(f"• {_html_escape(c.message)}")
    message = "\n".join(lines)

    ok, err = _validate_html(message)
    assert ok, f"audit CRITICAL message must validate: {err}\nmessage:\n{message}"
    _assert_no_markdown_bold(message, context="audit CRITICAL message")
    # The <ops> in the finding text must be escaped — otherwise Telegram
    # sees an unsupported tag and 400s the send (the historic
    # parse-error bug class the dispatcher-safety rework targeted).
    assert "&lt;ops&gt;" in message
    assert "<ops>" not in message
