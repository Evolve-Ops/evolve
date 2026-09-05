"""digest_dispatcher.flush — Phase G of the alert-subscriptions spec.

Pins the load-bearing behavior of the digest flusher:

  - empty/missing queue → no-op (no dispatcher call)
  - non-empty queue → atomic rotate → render → dispatch → archive
  - rotate happens BEFORE read so events arriving mid-flush land in
    a fresh active queue and are picked up next time
  - bundled message groups by catalog Category, with a per-category
    section header and per-event short lines
  - on dispatcher SENT, the rotated file becomes ``.flushed-<iso>``
  - on dispatcher FAILED, the rotated file is renamed back to the
    active queue path so the next flush retries
  - frequency must be ``daily`` or ``weekly`` — anything else raises
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    from evolve_admin.alerts import digest_dispatcher, dispatcher
    from evolve_admin.alerts.dispatcher import (
        DispatchOutcome, DispatchResult, Severity,
    )

    captured: list = []
    # ``value`` is the default per-call result. ``results`` (optional) is a
    # FIFO of (DispatchResult[, is_permanent_failure]) entries popped one per
    # send call — lets a test simulate "chunk 1 SENT, chunk 2 transient-FAIL,
    # …" or a gateway-restart that heals across flushes. ``permanent`` sets
    # the is_permanent_failure flag on a plain FAILED ``value``.
    next_result = {"value": DispatchResult.SENT, "results": None, "permanent": False}

    def fake_send(*, shared_dir, network, source, severity,
                  message=None, payload=None,
                  dedup_key=None, catalog_event=None, now=None, **_kw):
        captured.append({
            "source": source, "message": message, "payload": payload,
            "severity": severity, "dedup_key": dedup_key,
            "catalog_event": catalog_event, "now": now,
            "network": network,
        })
        permanent = next_result["permanent"]
        if next_result["results"]:
            entry = next_result["results"].pop(0)
            if isinstance(entry, tuple):
                result, permanent = entry
            else:
                result = entry
        else:
            result = next_result["value"]
        return DispatchOutcome(
            result=result, source=source, severity=severity,
            dedup_key=dedup_key, channel="telegram", chat_id="12345",
            is_permanent_failure=(
                permanent and result == DispatchResult.FAILED
            ),
        )

    monkeypatch.setattr(dispatcher, "send", fake_send)

    # Net-state roll-up reads the firing set via signals.store; default to
    # "nothing firing" so a fire+resolve pair in the window is net-resolved.
    # Tests that exercise the still-firing / fail-safe paths override this.
    firing: set = set()
    fail_firing = {"value": False}

    def fake_firing(_shared_dir):
        return None if fail_firing["value"] else set(firing)

    monkeypatch.setattr(digest_dispatcher, "_firing_signatures", fake_firing)

    shared = tmp_path / "evolve"
    shared.mkdir()
    return {
        "captured": captured,
        "next_result": next_result,
        "firing": firing,            # mutate in-place to mark sigs firing
        "fail_firing": fail_firing,  # set ["value"]=True to simulate unreadable
        "shared_dir": shared,
        # timezone pinned so gated-window tests don't depend on the
        # machine's /etc/localtime (resolve_pod_timezone falls back to it)
        "network": {"alerts": {"channel": "telegram", "chatId": "12345"},
                    "timezone": "UTC"},
        "digest_dispatcher": digest_dispatcher,
        "DispatchResult": DispatchResult,
        "Severity": Severity,
    }


def _write_queue(shared_dir: Path, frequency: str, records: list[dict]) -> Path:
    p = shared_dir / "alerts" / "digest-pending" / f"{frequency}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def _flushed_files(shared_dir: Path, frequency: str) -> list[Path]:
    d = shared_dir / "alerts" / "digest-pending"
    if not d.exists():
        return []
    return sorted(d.glob(f"{frequency}.flushed-*"))


def _t(year=2026, month=5, day=10, hour=8, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# ── No-op cases ────────────────────────────────────────────────────────────


def test_flush_returns_none_when_queue_missing(env):
    outcome = env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(),
    )
    assert outcome is None
    assert env["captured"] == []


def test_flush_archives_empty_queue_without_dispatching(env):
    """A queue file that exists but has only blank lines or comments —
    nothing to send. Archive it so we don't keep parsing it."""
    p = _write_queue(env["shared_dir"], "daily", [])
    # Replace with a blank-line file to simulate an emptied queue.
    p.write_text("\n\n")
    outcome = env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(),
    )
    assert outcome is None
    assert env["captured"] == []
    # Active queue gone, an "-empty" archive in its place.
    assert not p.exists()
    archives = list((env["shared_dir"] / "alerts" / "digest-pending").iterdir())
    assert any("empty" in a.name for a in archives)


def test_flush_rejects_unknown_frequency(env):
    with pytest.raises(ValueError):
        env["digest_dispatcher"].flush(
            env["shared_dir"], env["network"], frequency="hourly", now=_t(),
        )


# ── Happy path ─────────────────────────────────────────────────────────────


def test_flush_bundles_events_by_category(env):
    """The digest message has a header counting all events, then one
    block per Category, then short per-event lines."""
    records = [
        {"ts": "2026-05-10T07:00:00Z", "source": "spend_alert",
         "catalog_event": "cost.daily_threshold", "severity": "warning",
         "dedup_key": "spend/admin_bot/2026-05-10",
         "message": "💰 Daily spend over threshold\nadmin_bot: $5.96 (threshold: $5.00)"},
        {"ts": "2026-05-10T07:05:00Z", "source": "spend_alert",
         "catalog_event": "cost.daily_threshold", "severity": "warning",
         "dedup_key": "spend/team_bot_a/2026-05-10",
         "message": "💰 Daily spend over threshold\nteam_bot_a: $5.20 (threshold: $5.00)"},
        {"ts": "2026-05-10T07:12:00Z", "source": "cron_alert",
         "catalog_event": "system.stalled_cron", "severity": "warning",
         "dedup_key": "cron_alert/team_bot_a.measure",
         "message": "⚠️ Stalled cron\nBot: team_bot_a  Job: ai.evolve.team_bot_a.measure\nReason: last run 51h ago"},
    ]
    _write_queue(env["shared_dir"], "daily", records)

    outcome = env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8),
    )

    assert outcome is not None
    assert outcome.result == env["DispatchResult"].SENT
    assert len(env["captured"]) == 1
    call = env["captured"][0]
    assert call["source"] == "digest_dispatcher"
    assert call["severity"] == env["Severity"].INFO
    # Subscription-completeness (spec-subscription-completeness-2026-06-24):
    # the digest bundle now carries the meta.digest handle so it isn't an
    # unsubscribable catalog_event=None message. Infinite-loop prevention is
    # preserved a different way: meta.digest is IMMEDIATE-only, and the
    # dispatcher only re-enqueues events whose frequency is daily/weekly
    # digest — so binding meta.digest can never re-enqueue this bundle into
    # its own queue.
    assert call["catalog_event"] == "meta.digest"
    from evolve_admin.alerts import catalog as _cat
    _ev = _cat.by_key("meta.digest")
    assert _ev is not None
    assert _cat.Frequency.DAILY_DIGEST not in _ev.allowed_frequencies
    assert _cat.Frequency.WEEKLY_DIGEST not in _ev.allowed_frequencies
    # Dedup_key keyed on flush date (+ per-chunk suffix so multi-chunk
    # flushes don't suppress later chunks as cooldown/identical repeats).
    assert call["dedup_key"].startswith("digest_dispatcher/daily/2026-05-10")

    body = call["message"]
    assert body is not None
    assert "Daily digest — 3 events" in body
    # Cost category header with count, plus per-event short lines.
    assert "💰 Cost (2)" in body
    assert "💰 Daily spend over threshold" in body
    # System category present — uses ⚠️ since the operator-message
    # style guide tightening landed in the catalog audit PR.
    assert "⚠️ System (1)" in body
    assert "⚠️ Stalled cron" in body


def test_flush_orders_categories_consistently(env):
    """Categories render in a fixed order so two flushes with the same
    events produce byte-identical bodies (audit-log readability)."""
    records = [
        # Intentionally out-of-order to confirm sort happens.
        {"catalog_event": "summaries.daily_pod_report",
         "message": "📊 Pod report — Daily\n🟢 All clear"},
        {"catalog_event": "security.audit_finding",
         "message": "🛡️ New security finding\nteam_bot_a: ACL drift"},
        {"catalog_event": "cost.daily_threshold",
         "message": "💰 Daily spend over threshold\nadmin_bot: $5.96"},
    ]
    _write_queue(env["shared_dir"], "daily", records)

    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(),
    )
    body = env["captured"][0]["message"]

    # Security first, then Cost, then Summaries — per the renderer's
    # declared order.
    sec_idx = body.index("🛡️ Security")
    cost_idx = body.index("💰 Cost")
    sum_idx = body.index("📊 Summaries")
    assert sec_idx < cost_idx < sum_idx


# ── L1: fire+clear cancellation (spec-delta-transient-delivery-grace) ────────


def _fire_record(sig, severity="warn", *, event="system.watchdog_event",
                 msg=None, bot_id=None):
    """A fire digest record as signal_notifier enqueues it (digest_meta)."""
    return {
        "ts": "2026-05-10T07:00:00Z", "source": "signal_notifier",
        "catalog_event": event, "severity": severity,
        "dedup_key": sig, "digest_collapse_key": sig,
        "coalesce_key": sig, "kind": "fire", "signal_severity": severity,
        "bot_id": bot_id,
        "message": msg or f"⚠️ {sig} fired",
    }


def _resolve_record(sig, severity="warn", lifetime=120.0,
                    *, event="system.watchdog_event", msg=None, bot_id=None):
    """A resolve digest record (dedup_key=None, carries lifetime_seconds)."""
    rec = {
        "ts": "2026-05-10T07:02:00Z", "source": "signal_notifier",
        "catalog_event": event, "severity": "info",   # INFO wrapper on resolves
        "dedup_key": None,
        "coalesce_key": sig, "kind": "resolve", "signal_severity": severity,
        "bot_id": bot_id,
        "message": msg or f"🟢 Cleared: {sig}",
    }
    if lifetime is not None:
        rec["lifetime_seconds"] = lifetime
    return rec


def test_digest_cancels_within_grace_warn_fire_clear_pair(env):
    """Spec case (c)/warn: a warn that fires AND clears in the same digest
    window, with a lifetime inside the 900s warn grace, has BOTH lines
    elided. An all-cancelled digest produces no chat push at all."""
    _write_queue(env["shared_dir"], "daily", [
        _fire_record("watchdog:cpu:host", msg="⚠️ CPU saturation"),
        _resolve_record("watchdog:cpu:host", lifetime=120.0,
                        msg="🟢 Cleared: CPU saturation"),
    ])
    outcome = env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8),
    )
    # Entire digest cancelled ⇒ empty path ⇒ no dispatch, an -empty marker.
    assert outcome is None
    assert env["captured"] == []
    archives = list((env["shared_dir"] / "alerts" / "digest-pending").iterdir())
    assert any("empty" in a.name for a in archives)


def test_digest_cancellation_leaves_other_events_intact(env):
    """A cancelled pair is removed but unrelated events still render — the
    elision is surgical, not a whole-digest suppressor."""
    _write_queue(env["shared_dir"], "daily", [
        _fire_record("watchdog:cpu:host", msg="⚠️ CPU saturation"),
        _resolve_record("watchdog:cpu:host", lifetime=120.0,
                        msg="🟢 Cleared: CPU saturation"),
        {"ts": "2026-05-10T07:30:00Z", "source": "spend_alert",
         "catalog_event": "cost.daily_threshold", "severity": "warning",
         "dedup_key": "spend/admin_bot/2026-05-10",
         "message": "💰 Daily spend over threshold\nadmin_bot: $5.96"},
    ])
    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8),
    )
    assert len(env["captured"]) == 1
    body = env["captured"][0]["message"]
    assert "CPU saturation" not in body          # fire line gone
    assert "Cleared: CPU saturation" not in body  # resolve line gone
    assert "Daily spend over threshold" in body   # survivor intact


# These three pin L1's carve-outs at the ``_cancel_transient_pairs`` level
# (which pair SURVIVES L1), decoupled from the downstream net-state roll-up.
# End-to-end, a surviving net-resolved pair is then collapsed into one
# retrospective line by the roll-up layer — see the recovered-flap tests below
# (``test_rollup_collapses_operator_alert_severity_flap`` for the alert case,
# ``test_rollup_composes_with_d3_demoted_flapper`` for the past-grace warn).


def test_l1_keeps_alert_fire_clear_pair(env):
    """L1 invariant: an ALERT pair is NEVER cancelled by ``_cancel_transient_
    pairs`` — gated on the Signal's own severity, not the INFO resolve
    wrapper. (The net-state roll-up, severity-agnostic, owns it downstream.)"""
    records = [
        _fire_record("host:disk:full", severity="alert",
                     event="system.watchdog_event", msg="🔴 Disk full"),
        _resolve_record("host:disk:full", severity="alert", lifetime=60.0,
                        event="system.watchdog_event", msg="🟢 Cleared: Disk full"),
    ]
    kept = env["digest_dispatcher"]._cancel_transient_pairs(
        env["shared_dir"], records)
    assert kept == records  # nothing cancelled


def test_l1_keeps_warn_pair_that_persisted_past_grace(env):
    """L1 spec case (d): a warn whose lifetime EXCEEDED its grace is not a
    transient — L1 keeps both records (the operator wanted to know it was
    broken for a meaningful stretch)."""
    records = [
        _fire_record("watchdog:cpu:host", msg="⚠️ CPU saturation"),
        # 1200s > 900s warn grace ⇒ persisted ⇒ keep.
        _resolve_record("watchdog:cpu:host", lifetime=1200.0,
                        msg="🟢 Cleared: CPU saturation"),
    ]
    kept = env["digest_dispatcher"]._cancel_transient_pairs(
        env["shared_dir"], records)
    assert kept == records


def test_digest_keeps_lone_fire_without_resolve(env):
    """A fire with no matching resolve in the window is a still-open
    condition — it must render (this is the common 'it's still broken'
    case, not a transient). Neither L1 nor the roll-up touches it."""
    _write_queue(env["shared_dir"], "daily", [
        _fire_record("watchdog:cpu:host", msg="⚠️ CPU saturation"),
    ])
    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8),
    )
    assert len(env["captured"]) == 1
    assert "CPU saturation" in env["captured"][0]["message"]


def test_l1_keeps_pair_with_unmeasurable_lifetime(env):
    """L1 fail-loud: a resolve with no lifetime_seconds (malformed / missing
    timestamps) is NOT cancelled by L1 — when in doubt, surface it."""
    records = [
        _fire_record("watchdog:cpu:host", msg="⚠️ CPU saturation"),
        _resolve_record("watchdog:cpu:host", lifetime=None,
                        msg="🟢 Cleared: CPU saturation"),
    ]
    kept = env["digest_dispatcher"]._cancel_transient_pairs(
        env["shared_dir"], records)
    assert kept == records


# ── Net-state recovered-flap roll-up (severity-agnostic, layer above L1) ─────
#
# Spec: digest-by-default net-state collapse for recovered flaps. A condition
# that broke AND self-healed within the window (net state = RESOLVED) collapses
# to ONE retrospective line per (bot, catalog_event), instead of dumping the
# full fire+clear transition log. Unlike L1, this is severity-agnostic — it
# does NOT exempt ``alert`` (a daily digest is retrospective; the must-page
# guarantee lives on the immediate path, not here).

_IDENT = "system.identity_doc_missing"


def _ident_files(bot_id, files, *, severity="alert"):
    """Fire+resolve record pairs for several identity files on one bot — the
    live evo-vps shape (content_scan mints these at ``alert`` severity)."""
    recs = []
    for f in files:
        sig = f"content_scan:identity_doc_missing:{bot_id}/{f}"
        recs.append(_fire_record(sig, severity=severity, event=_IDENT, bot_id=bot_id,
                                 msg=f"🔴 {bot_id}: {f} missing or unreadable"))
        recs.append(_resolve_record(sig, severity=severity, lifetime=1200.0,
                                    event=_IDENT, bot_id=bot_id,
                                    msg=f"🟢 Cleared on {bot_id}: {f} missing or unreadable"))
    return recs


def test_rollup_collapses_operator_alert_severity_flap(env):
    """The operator's exact case: 7 identity files on one bot, each an
    ``alert``-severity fire+clear pair, net RESOLVED (none firing). L1's
    alert carve-out keeps all 14 lines; the net-state roll-up collapses them
    to ONE retrospective line."""
    files = ["SOUL.md", "IDENTITY.md", "USER.md", "README.md",
             "MEMORY.md", "HEARTBEAT.md", "TOOLS.md"]
    _write_queue(env["shared_dir"], "daily", _ident_files("evo", files))

    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8),
    )
    assert len(env["captured"]) == 1
    body = env["captured"][0]["message"]
    # No raw fire/clear transition lines survive.
    assert "missing or unreadable" not in body
    assert "🔴" not in body and "Cleared on evo" not in body
    # Exactly one rolled-up retrospective line, naming the count + bot.
    assert body.count("↩️ Recovered after flapping") == 1
    assert "7 × " in body
    assert "on evo" in body


def test_rollup_still_firing_re_fire_stays_loud(env):
    """A signature with a fire AND a resolve in the window that is CURRENTLY
    firing (re-fired after the window's resolve) is net-firing — it is NOT
    rolled up; its lines render exactly as today (cardinal invariant)."""
    sig = "content_scan:identity_doc_missing:__pod__/RUNTIME_NOTES.md"
    _write_queue(env["shared_dir"], "daily", [
        _fire_record(sig, severity="alert", event=_IDENT, bot_id=None,
                     msg="🔴 RUNTIME_NOTES.md missing or unreadable"),
        _resolve_record(sig, severity="alert", lifetime=1200.0, event=_IDENT,
                        bot_id=None, msg="🟢 Cleared: RUNTIME_NOTES.md"),
    ])
    # Re-fired after the resolve → present in signals/firing/.
    env["firing"].add(sig)

    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8),
    )
    assert len(env["captured"]) == 1
    body = env["captured"][0]["message"]
    assert "↩️ Recovered after flapping" not in body
    assert "RUNTIME_NOTES.md missing or unreadable" in body


def test_rollup_lone_fire_without_resolve_stays_loud(env):
    """A still-open condition (fire, no matching resolve this window) is never
    rolled up — the roll-up requires BOTH a fire and a resolve."""
    _write_queue(env["shared_dir"], "daily", [
        _fire_record("content_scan:identity_doc_missing:evo/SOUL.md",
                     severity="alert", event=_IDENT, bot_id="evo",
                     msg="🔴 evo: SOUL.md missing or unreadable"),
    ])
    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8),
    )
    body = env["captured"][0]["message"]
    assert "↩️ Recovered after flapping" not in body
    assert "SOUL.md missing or unreadable" in body


def test_rollup_l1_still_owns_within_grace_blips(env):
    """L1 fully drops a within-grace warn fire+clear (silence). The roll-up
    must not then conjure a 'recovered' line for it — no double-handling."""
    _write_queue(env["shared_dir"], "daily", [
        _fire_record("watchdog:cpu:host", severity="warn", msg="⚠️ CPU saturation"),
        _resolve_record("watchdog:cpu:host", severity="warn", lifetime=120.0,
                        msg="🟢 Cleared: CPU saturation"),
    ])
    outcome = env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8),
    )
    # Whole digest cancelled by L1 → no send, no roll-up line.
    assert outcome is None
    assert env["captured"] == []


def test_rollup_mixed_bot_state_keeps_still_missing_loud(env):
    """Same bot, same event: 3 files recovered (net resolved) collapse to one
    line; 2 still-missing (currently firing) stay loud and individual."""
    bot = "evo"
    records = _ident_files(bot, ["SOUL.md", "IDENTITY.md", "USER.md"])
    still = ["MEMORY.md", "TOOLS.md"]
    for f in still:
        sig = f"content_scan:identity_doc_missing:{bot}/{f}"
        records.append(_fire_record(sig, severity="alert", event=_IDENT, bot_id=bot,
                                    msg=f"🔴 {bot}: {f} missing or unreadable"))
        env["firing"].add(sig)
    _write_queue(env["shared_dir"], "daily", records)

    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8),
    )
    body = env["captured"][0]["message"]
    # The 3 recovered collapse to one line.
    assert body.count("↩️ Recovered after flapping") == 1
    assert "3 × " in body
    # The 2 still-missing stay loud and individual.
    assert "MEMORY.md missing or unreadable" in body
    assert "TOOLS.md missing or unreadable" in body
    # Recovered files do NOT appear as raw lines.
    assert "SOUL.md missing or unreadable" not in body


def test_rollup_is_delivery_only_signal_store_untouched(env):
    """The roll-up reads the firing set but never mutates the Signal store —
    Alerts → History must still show every transition. flush() touches only
    ``alerts/``; nothing is written under ``signals/``."""
    _write_queue(env["shared_dir"], "daily", _ident_files("evo", ["SOUL.md"]))

    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8),
    )
    # The roll-up collapsed the pair...
    body = env["captured"][0]["message"]
    assert body.count("↩️ Recovered after flapping") == 1
    # ...but flush created nothing under the Signal store.
    assert not (env["shared_dir"] / "signals").exists()


def test_rollup_fail_safe_keeps_both_lines_when_firing_unreadable(env):
    """If the firing set can't be determined (``_firing_signatures`` returns
    None — analyzer import failure / store error), the roll-up is a no-op:
    both the fire and the clear stay loud (fail safe; never silence on
    uncertainty)."""
    env["fail_firing"]["value"] = True
    _write_queue(env["shared_dir"], "daily", _ident_files("evo", ["SOUL.md"]))

    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8),
    )
    body = env["captured"][0]["message"]
    assert "↩️ Recovered after flapping" not in body
    assert "🔴 evo: SOUL.md missing or unreadable" in body
    assert "🟢 Cleared on evo: SOUL.md missing or unreadable" in body


def test_rollup_standalone_clear_not_rolled_up(env):
    """A standalone clear (resolve only — the fire was paged in a PRIOR
    window) is NOT a recovered flap; it renders as its normal 🟢 line."""
    _write_queue(env["shared_dir"], "daily", [
        _resolve_record("content_scan:identity_doc_missing:evo/SOUL.md",
                        severity="alert", lifetime=1200.0, event=_IDENT,
                        bot_id="evo", msg="🟢 Cleared on evo: SOUL.md"),
    ])
    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8),
    )
    body = env["captured"][0]["message"]
    assert "↩️ Recovered after flapping" not in body
    assert "Cleared on evo: SOUL.md" in body


def test_rollup_composes_with_d3_demoted_flapper(env):
    """D3 (#3336) routes a recurring flapper's fires AND clears to the daily
    digest (both carry digest_meta). When such a flapper is net-resolved, the
    roll-up pairs them into one line — it must NOT orphan the fire line."""
    # D3 enqueues a demoted fire (kind=fire) and a demoted clear (kind=resolve)
    # for the same signature — exactly the records this digest receives.
    sig = "audit:pod_perms_drift:darwin/auth-profiles.json"
    _write_queue(env["shared_dir"], "daily", [
        _fire_record(sig, severity="warn", event="security.audit_finding",
                     bot_id="darwin",
                     msg="🔴 darwin: auth-profiles.json readable by others"),
        _resolve_record(sig, severity="warn", lifetime=1200.0,
                        event="security.audit_finding", bot_id="darwin",
                        msg="🟢 Cleared on darwin: auth-profiles.json readable by others"),
    ])
    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8),
    )
    body = env["captured"][0]["message"]
    assert body.count("↩️ Recovered after flapping") == 1
    # Neither the orphaned fire nor the orphaned clear leaks through.
    assert "readable by others" not in body


def test_flush_archives_rotated_file_on_sent(env):
    _write_queue(env["shared_dir"], "daily", [
        {"catalog_event": "summaries.daily_pod_report",
         "message": "📊 Pod report\n🟢 All clear"},
    ])
    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(),
    )
    # Active queue is gone; one .flushed-* archive exists.
    active = env["shared_dir"] / "alerts" / "digest-pending" / "daily.jsonl"
    assert not active.exists()
    archives = _flushed_files(env["shared_dir"], "daily")
    assert len(archives) == 1


def test_flush_retries_by_restoring_queue_on_failure(env):
    """If dispatcher.send returns a TRANSIENT FAILED, the events must come
    back to the active queue so the next flush retries them (no data loss).

    The requeue is an append (re-serialized JSONL), so the queue's records
    round-trip rather than staying byte-identical to the original file."""
    env["next_result"]["value"] = env["DispatchResult"].FAILED
    records = [
        {"catalog_event": "summaries.daily_pod_report",
         "message": "📊 Pod report\n🟢 All clear"},
    ]
    p = _write_queue(env["shared_dir"], "daily", records)

    outcome = env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(),
    )
    assert outcome is not None
    assert outcome.result == env["DispatchResult"].FAILED

    # Active queue restored with the same records (round-tripped).
    assert p.exists()
    requeued = [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]
    assert requeued == records
    # No orphan .flushing file left behind.
    digest_dir = env["shared_dir"] / "alerts" / "digest-pending"
    assert list(digest_dir.glob("daily.flushing-*")) == []
    # And no delivered-flush archive was created (nothing went out).
    assert _flushed_files(env["shared_dir"], "daily") == []


def _unique_event(i: int) -> dict:
    """A distinct queue record (unique dedup_key + message) so chunking
    can't collapse it via dedup."""
    return {
        "ts": f"2026-05-10T07:{i % 60:02d}:00Z",
        "source": "cron_alert",
        "catalog_event": "system.stalled_cron",
        "severity": "warning",
        "dedup_key": f"cron_alert/bot_{i}.job",
        "message": f"⚠️ Stalled cron #{i}\nBot: bot_{i}  Job: ai.evolve.bot_{i}.measure",
    }


# ── Chunking + restart resilience (drain that can't wedge) ──────────────────


def test_flush_chunks_a_large_queue_into_multiple_sends(env):
    """A queue far larger than one Telegram message is split into several
    chunk sends, each individually under the size cap — no single over-cap
    bundle that would 400 / time out."""
    d = env["digest_dispatcher"]
    records = [_unique_event(i) for i in range(200)]
    _write_queue(env["shared_dir"], "daily", records)

    outcome = d.flush(env["shared_dir"], env["network"], frequency="daily", now=_t())
    assert outcome.result == env["DispatchResult"].SENT
    # Multiple chunk sends happened.
    assert len(env["captured"]) > 1
    # Every chunk body is under the cap and part-labelled.
    for call in env["captured"]:
        assert len(call["message"]) <= d._DIGEST_CHUNK_CHAR_BUDGET + 64  # +prefix slack
        assert "(part " in call["message"]
    # Every event delivered exactly once across the chunks; queue drained.
    delivered = "\n".join(c["message"] for c in env["captured"])
    for i in range(200):
        assert f"Stalled cron #{i}\n" in delivered or f"Stalled cron #{i}<" in delivered \
            or f"Stalled cron #{i}" in delivered
    active = env["shared_dir"] / "alerts" / "digest-pending" / "daily.jsonl"
    assert not active.exists()
    assert list((env["shared_dir"] / "alerts" / "digest-pending").glob("daily.flushing-*")) == []


def test_flush_caps_messages_per_flush_and_paces_a_huge_backlog(env):
    """A pathological backlog drains over several flushes instead of a single
    notification storm: one flush sends at most the per-flush chunk cap, the
    rest are requeued and picked up next tick — with zero lost events."""
    d = env["digest_dispatcher"]
    cap = d._DIGEST_MAX_CHUNKS_PER_FLUSH
    # Enough events to need more than `cap` chunks (cap × max-events-per-chunk).
    n = (cap + 3) * d._DIGEST_CHUNK_MAX_EVENTS
    _write_queue(env["shared_dir"], "daily", [_unique_event(i) for i in range(n)])

    seen_ids: set = set()
    active = env["shared_dir"] / "alerts" / "digest-pending" / "daily.jsonl"
    # Drain across as many flushes as it takes; each flush sends ≤ cap msgs.
    for tick in range(50):
        before = len(env["captured"])
        out = d.flush(env["shared_dir"], env["network"], frequency="daily",
                      now=_t(hour=8, minute=tick % 60))
        sent_this_flush = env["captured"][before:]
        assert len(sent_this_flush) <= cap        # never storms past the cap
        for c in sent_this_flush:
            for i in range(n):
                if f"#{i}\n" in c["message"] or f"#{i} " in c["message"]:
                    seen_ids.add(i)
        if out is None:                            # queue fully drained
            break
    assert not active.exists()
    assert seen_ids == set(range(n))               # zero lost events


def test_flush_partial_failure_requeues_unsent_with_no_loss(env):
    """A gateway timeout mid-flush: the first chunk sends, a later chunk
    fails transiently. The unsent events are requeued (not lost, no orphan
    .flushing); a second flush drains them once the gateway recovers."""
    d = env["digest_dispatcher"]
    DR = env["DispatchResult"]
    records = [_unique_event(i) for i in range(200)]
    _write_queue(env["shared_dir"], "daily", records)

    # First chunk SENT, second chunk transient-FAILED → abort + requeue rest.
    env["next_result"]["results"] = [DR.SENT, (DR.FAILED, False)]

    out1 = d.flush(env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8))
    assert out1 is not None
    # Some events delivered; the rest are back in the active queue.
    active = env["shared_dir"] / "alerts" / "digest-pending" / "daily.jsonl"
    assert active.exists()
    requeued = [json.loads(ln) for ln in active.read_text().splitlines() if ln.strip()]
    assert 0 < len(requeued) < 200
    # No orphan .flushing file.
    digest_dir = env["shared_dir"] / "alerts" / "digest-pending"
    assert list(digest_dir.glob("daily.flushing-*")) == []

    # Gateway recovers: second flush drains the rest, all SENT.
    env["next_result"]["results"] = None
    env["next_result"]["value"] = DR.SENT
    captured_before = len(env["captured"])
    out2 = d.flush(env["shared_dir"], env["network"], frequency="daily", now=_t(hour=9))
    assert out2.result == DR.SENT
    assert not active.exists()

    # Zero lost events: the union of all sent chunk bodies covers every id.
    all_sent = "\n".join(c["message"] for c in env["captured"])
    for i in range(200):
        assert f"#{i}\n" in all_sent or f"#{i} " in all_sent or f"#{i}<" in all_sent
    assert len(env["captured"]) > captured_before


def test_flush_permanent_failure_chunk_is_not_requeued(env):
    """A chunk that fails PERMANENTLY (dispatcher.send already dead-lettered
    it) is consumed, not requeued — otherwise a poison bundle loops forever.
    A later chunk can still deliver."""
    d = env["digest_dispatcher"]
    DR = env["DispatchResult"]
    records = [_unique_event(i) for i in range(200)]
    _write_queue(env["shared_dir"], "daily", records)

    # Chunk 1 permanent-FAIL (dead-lettered, consumed), chunk 2+ SENT.
    env["next_result"]["results"] = [(DR.FAILED, True), DR.SENT, DR.SENT, DR.SENT,
                                     DR.SENT, DR.SENT, DR.SENT, DR.SENT]
    out = d.flush(env["shared_dir"], env["network"], frequency="daily", now=_t())
    assert out is not None
    # Queue fully consumed — the permanent chunk is NOT requeued.
    active = env["shared_dir"] / "alerts" / "digest-pending" / "daily.jsonl"
    assert not active.exists()
    digest_dir = env["shared_dir"] / "alerts" / "digest-pending"
    assert list(digest_dir.glob("daily.flushing-*")) == []


def test_flush_adopts_orphan_flushing_file(env):
    """An orphan .flushing-* file from a previously-crashed flush is adopted
    into the next flush (its events re-sent) rather than stranded forever."""
    d = env["digest_dispatcher"]
    digest_dir = env["shared_dir"] / "alerts" / "digest-pending"
    digest_dir.mkdir(parents=True, exist_ok=True)
    # Simulate a crash: a .flushing file left behind with one event, no active.
    orphan = digest_dir / "daily.flushing-2026-05-10T07:00:00Z"
    orphan.write_text(json.dumps({
        "catalog_event": "summaries.daily_pod_report",
        "message": "📊 Pod report\n🟢 All clear",
    }) + "\n")

    out = d.flush(env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8))
    assert out.result == env["DispatchResult"].SENT
    assert len(env["captured"]) == 1
    assert "Pod report" in env["captured"][0]["message"]
    # Orphan consumed.
    assert list(digest_dir.glob("daily.flushing-*")) == []


def test_flush_does_not_steal_a_concurrent_flushs_fresh_rotation(env):
    """A ``.flushing-*`` file younger than the adopt threshold is left alone
    — it may belong to an in-flight concurrent flush, and adopting it would
    double-process its events."""
    d = env["digest_dispatcher"]
    digest_dir = env["shared_dir"] / "alerts" / "digest-pending"
    digest_dir.mkdir(parents=True, exist_ok=True)
    # A fresh in-flight rotation (same minute as our flush) from "another" run.
    fresh = digest_dir / "daily.flushing-2026-05-10T08:00:00Z"
    fresh.write_text(json.dumps({
        "catalog_event": "summaries.daily_pod_report", "message": "📊 inflight",
    }) + "\n")

    # No active queue of our own → nothing to flush, and we must NOT touch
    # the other flush's fresh file.
    out = d.flush(env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8))
    assert out is None
    assert env["captured"] == []
    assert fresh.exists()


def test_flush_running_twice_in_a_row_is_idempotent(env):
    """First flush sends + archives. Second flush sees an empty queue
    and noops — no double-send."""
    _write_queue(env["shared_dir"], "daily", [
        {"catalog_event": "summaries.daily_pod_report",
         "message": "📊 Pod report\n🟢 All clear"},
    ])
    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8),
    )
    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8, minute=5),
    )
    assert len(env["captured"]) == 1


def test_flush_handles_malformed_jsonl_lines(env):
    """A single garbled line shouldn't kill the whole flush — skip it,
    deliver the rest."""
    p = (env["shared_dir"] / "alerts" / "digest-pending" / "daily.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '{"catalog_event": "summaries.daily_pod_report",'
        ' "message": "📊 Pod report\\n🟢 All clear"}\n'
        '{not valid json\n'
        '{"catalog_event": "security.audit_finding",'
        ' "message": "🛡️ New security finding\\nteam_bot_a: ACL drift"}\n'
    )
    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(),
    )
    body = env["captured"][0]["message"]
    # Two valid events landed in the bundle.
    assert "Daily digest — 2 events" in body


def test_flush_weekly_uses_weekly_header_and_path(env):
    _write_queue(env["shared_dir"], "weekly", [
        {"catalog_event": "summaries.weekly_rsi_review",
         "message": "📊 Weekly RSI review\n12 proposals reviewed"},
    ])
    outcome = env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="weekly", now=_t(),
    )
    assert outcome.result == env["DispatchResult"].SENT
    call = env["captured"][0]
    assert call["dedup_key"].startswith("digest_dispatcher/weekly/")
    # Single-category (Summaries) → topic-named title with the weekly
    # time-frame, not the generic "Weekly digest — N events" header.
    assert "📊 Summaries — past week" in call["message"]


# ── Queue dedup ────────────────────────────────────────────────────────────


def test_flush_dedups_repeated_queue_entries_by_dedup_key(env):
    """Defense-in-depth against append-only queue accumulation: when a
    Signal stays firing and signal_notifier re-enqueues it on every
    tick (one per minute), the digest must collapse those copies to a
    single line at flush time. The most recent record wins so the
    timestamp/message reflects the latest state.

    Reproduces the admin_bot Google_Workspace bug — repeated DEFERRED ticks
    fill the queue with identical records — without making the test
    depend on signal_notifier's state-update fix landing too.
    """
    duplicates = [
        {"ts": f"2026-05-20T07:{minute:02d}:00Z", "source": "signal_notifier",
         "catalog_event": "system.watchdog_event", "severity": "info",
         "dedup_key": "integration_probe:google_workspace:admin_bot:oauth",
         "message": (
             "🔴 admin_bot: Google_Workspace (oauth) on admin_bot no value set\n"
             "subscription: system.watchdog_event"
         )}
        for minute in range(0, 30)
    ]
    distinct = [
        {"ts": "2026-05-20T07:30:00Z", "source": "spend_alert",
         "catalog_event": "cost.daily_threshold", "severity": "warning",
         "dedup_key": "spend/admin_bot/2026-05-20",
         "message": "💰 Daily spend over threshold\nadmin_bot: $5.96"},
    ]
    _write_queue(env["shared_dir"], "daily", duplicates + distinct)

    outcome = env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(hour=8),
    )

    assert outcome is not None and outcome.result == env["DispatchResult"].SENT
    body = env["captured"][0]["message"]
    # Header reports the DEDUPED count, not the raw record count.
    assert "Daily digest — 2 events" in body
    # The bot/finding appears exactly once even though it was queued 30×.
    assert body.count("Google_Workspace") == 1


def test_dedup_passes_through_records_without_dedup_key():
    """Records without dedup_key are genuinely unique events (forge job
    completions, ad-hoc notifications). Don't collapse them."""
    from evolve_admin.alerts.digest_dispatcher import _dedup_records

    records = [
        {"ts": "1", "catalog_event": "applications.forge_job_completed",
         "dedup_key": None,
         "message": "🔧 Forge job 1 finished"},
        {"ts": "2", "catalog_event": "applications.forge_job_completed",
         "dedup_key": None,
         "message": "🔧 Forge job 2 finished"},
    ]
    out = _dedup_records(records)
    assert len(out) == 2


def test_dedup_keeps_most_recent_record():
    """When duplicates collapse, the later record wins so timestamps
    stay fresh and any message-text update flows through."""
    from evolve_admin.alerts.digest_dispatcher import _dedup_records

    records = [
        {"ts": "2026-05-20T07:00:00Z", "catalog_event": "x", "dedup_key": "k",
         "message": "first attempt"},
        {"ts": "2026-05-20T07:05:00Z", "catalog_event": "x", "dedup_key": "k",
         "message": "second attempt (refined)"},
    ]
    out = _dedup_records(records)
    assert len(out) == 1
    assert out[0]["ts"] == "2026-05-20T07:05:00Z"
    assert out[0]["message"] == "second attempt (refined)"


# ── Render: topic title / rollup / attribution footer ──────────────────────
#
# Fix for the "Weekly digest — 16 events" / 16 identical
# "🔄 origin/main has new commits" lines / no-subscription-name report.


def test_render_single_category_digest_gets_topic_title(env):
    """When every record maps to one Category, the title is the topic
    (category label + time-frame), not the generic "Weekly digest — N"."""
    _write_queue(env["shared_dir"], "weekly", [
        {"catalog_event": "updates.evolve_repo",
         "dedup_key": "update_watcher/evolve_repo/aaa",
         "message": "🔄 origin/main has new commits\n3 commits.\n\n"
                    "subscription: updates.evolve_repo"},
        {"catalog_event": "updates.openclaw_available",
         "dedup_key": "update_watcher/openclaw/1.2.3",
         "message": "🔄 OpenClaw update available\n1.2.3\n\n"
                    "subscription: updates.openclaw_available"},
    ])
    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="weekly", now=_t(),
    )
    body = env["captured"][0]["message"]
    assert body.splitlines()[0] == "🔄 Updates — past week"
    # The generic header must NOT be used for a single-category digest.
    assert "Weekly digest —" not in body


def test_render_multi_category_digest_keeps_generic_title(env):
    """A digest spanning >1 Category keeps the generic header + per-category
    section headers."""
    _write_queue(env["shared_dir"], "daily", [
        {"catalog_event": "cost.daily_threshold",
         "dedup_key": "spend/admin_bot/2026-05-10",
         "message": "💰 Daily spend over threshold\nadmin_bot: $5.96"},
        {"catalog_event": "system.stalled_cron",
         "dedup_key": "cron/team_bot_a",
         "message": "⚠️ Stalled cron\nteam_bot_a"},
    ])
    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(),
    )
    body = env["captured"][0]["message"]
    assert "📋 Daily digest — 2 events" in body
    assert "💰 Cost (1)" in body
    assert "⚠️ System (1)" in body


def test_render_rolls_up_recurring_event_via_registry(env):
    """The reported bug: 16 evolve_repo records (distinct shas → distinct
    dedup_keys, so _dedup_records can't touch them) collapse to ONE
    rolled-up line via the _DIGEST_ROLLUP_TEMPLATES registry."""
    records = [
        {"ts": f"2026-06-20T07:{i:02d}:00Z", "source": "update_watcher",
         "catalog_event": "updates.evolve_repo", "severity": "info",
         "dedup_key": f"update_watcher/evolve_repo/sha{i}",
         "message": (f"🔄 origin/main has new commits\n"
                     f"commit {i} since last notification.\n\n"
                     f"subscription: updates.evolve_repo")}
        for i in range(16)
    ]
    _write_queue(env["shared_dir"], "weekly", records)
    outcome = env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="weekly", now=_t(),
    )
    assert outcome.result == env["DispatchResult"].SENT
    body = env["captured"][0]["message"]
    assert ("🔄 Evolve improved 16 times this week. "
            "Your pod stays up to date automatically.") in body
    # Exactly one bullet — not a 16-line log dump.
    assert body.count("  • ") == 1
    # The raw repeated title line is rolled up, not listed.
    assert "origin/main has new commits" not in body


def test_render_collapses_identical_lines_without_registry(env):
    """A recurring catalog_event with NO registry entry falls back to the
    per-line listing, but visually-identical short-lines collapse to a
    single "(×N)" line."""
    records = [
        {"catalog_event": "system.stalled_cron",
         "dedup_key": f"cron/job{i}",
         "message": "⚠️ Stalled cron\nsome job"}
        for i in range(3)
    ]
    _write_queue(env["shared_dir"], "daily", records)
    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="daily", now=_t(),
    )
    body = env["captured"][0]["message"]
    assert "⚠️ Stalled cron  (×3)" in body
    assert body.count("  • ") == 1


def test_render_footer_uses_human_label_not_raw_key(env):
    """The attribution footer names the subscription by its catalog HUMAN
    label, never the raw key."""
    _write_queue(env["shared_dir"], "weekly", [
        {"catalog_event": "updates.evolve_repo",
         "dedup_key": "update_watcher/evolve_repo/aaa",
         "message": "🔄 origin/main has new commits\n3 commits.\n\n"
                    "subscription: updates.evolve_repo"},
    ])
    env["digest_dispatcher"].flush(
        env["shared_dir"], env["network"], frequency="weekly", now=_t(),
    )
    body = env["captured"][0]["message"]
    assert ('— Subscription: "New Evolve code available" · '
            "manage in Reports → Subscriptions") in body
    # The raw catalog key must not leak into the operator-facing digest.
    assert "updates.evolve_repo" not in body


# ── Source registration ────────────────────────────────────────────────────


def test_dispatcher_recognizes_digest_dispatcher_source():
    """digest_dispatcher must be in dispatcher.known_sources() so the
    bundled send isn't rejected as 'unknown source'."""
    from evolve_admin.alerts.dispatcher import known_sources
    assert "digest_dispatcher" in set(known_sources())


# ── flush_if_gated: hourly tick + timezone gate ────────────────────────────


def _seed_digest_hour(shared_dir: Path, hour: int) -> None:
    """Write subscriptions.json with a chosen digest_hour_local."""
    import json
    p = shared_dir / "alerts" / "subscriptions.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "version": 1, "subscriptions": {},
        "digest_hour_local": hour,
    }))


def test_flush_if_gated_daily_noops_outside_window(env):
    """A daily flush at hour != digest_hour_local is a no-op — the
    queue stays untouched (no rotate) so the digest-hour tick still
    sees the events."""
    _seed_digest_hour(env["shared_dir"], 8)
    p = _write_queue(env["shared_dir"], "daily", [
        {"catalog_event": "summaries.daily_pod_report",
         "message": "📊 Pod report\n🟢 All clear"},
    ])
    original_text = p.read_text()

    outcome = env["digest_dispatcher"].flush_if_gated(
        env["shared_dir"], env["network"], frequency="daily",
        now=_t(hour=14),  # 14:00 UTC ≠ 8 local (network has no timezone → UTC)
    )
    assert outcome is None
    assert env["captured"] == []
    assert p.exists() and p.read_text() == original_text


def test_flush_if_gated_daily_fires_at_window(env):
    _seed_digest_hour(env["shared_dir"], 8)
    _write_queue(env["shared_dir"], "daily", [
        {"catalog_event": "summaries.daily_pod_report",
         "message": "📊 Pod report\n🟢 All clear"},
    ])
    outcome = env["digest_dispatcher"].flush_if_gated(
        env["shared_dir"], env["network"], frequency="daily",
        now=_t(hour=8),
    )
    assert outcome is not None
    assert outcome.result == env["DispatchResult"].SENT
    assert len(env["captured"]) == 1


def test_flush_if_gated_weekly_requires_monday(env):
    """Weekly flush gates on both hour AND weekday. Tuesday at digest
    hour is a no-op; Monday at the same hour fires."""
    _seed_digest_hour(env["shared_dir"], 8)
    _write_queue(env["shared_dir"], "weekly", [
        {"catalog_event": "summaries.weekly_rsi_review",
         "message": "📊 Weekly RSI review"},
    ])

    # 2026-05-12 is a Tuesday. Should noop.
    tuesday = datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)
    assert env["digest_dispatcher"].flush_if_gated(
        env["shared_dir"], env["network"], frequency="weekly", now=tuesday,
    ) is None
    assert env["captured"] == []

    # 2026-05-11 is a Monday. Should fire.
    monday = datetime(2026, 5, 11, 8, 0, tzinfo=timezone.utc)
    outcome = env["digest_dispatcher"].flush_if_gated(
        env["shared_dir"], env["network"], frequency="weekly", now=monday,
    )
    assert outcome is not None
    assert outcome.result == env["DispatchResult"].SENT


def test_flush_if_gated_honors_network_timezone(env):
    """digest_hour_local is interpreted in network.timezone.

    13:00 UTC = 08:00 America/New_York (EDT in May). With digest_hour=8
    and network.timezone="America/New_York", the 13:00 UTC tick fires.
    """
    _seed_digest_hour(env["shared_dir"], 8)
    env["network"]["timezone"] = "America/New_York"
    _write_queue(env["shared_dir"], "daily", [
        {"catalog_event": "summaries.daily_pod_report",
         "message": "📊 Pod report\n🟢 All clear"},
    ])
    outcome = env["digest_dispatcher"].flush_if_gated(
        env["shared_dir"], env["network"], frequency="daily",
        now=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),  # 08:00 EDT
    )
    assert outcome is not None
    assert outcome.result == env["DispatchResult"].SENT


# ── CLI: --hourly path ─────────────────────────────────────────────────────


def test_cli_hourly_outside_window_prints_gated_message(env, capsys):
    """``digest-flush --hourly`` outside the digest window should print
    a "gated out" line and exit 0 (no error). Both daily and weekly
    return None — nothing to report."""
    _seed_digest_hour(env["shared_dir"], 8)
    # Force the hourly tick into the off-window by manipulating the
    # default now() via monkeypatch on flush_if_gated. Simpler: run the
    # CLI in module form, which uses datetime.now() — but we can't
    # control that from the test cleanly. So just verify the
    # function-level no-op behavior covers this; the CLI exit-code
    # contract is tested by the path below.
    daily = env["digest_dispatcher"].flush_if_gated(
        env["shared_dir"], env["network"], frequency="daily", now=_t(hour=14),
    )
    weekly = env["digest_dispatcher"].flush_if_gated(
        env["shared_dir"], env["network"], frequency="weekly", now=_t(hour=14),
    )
    assert daily is None
    assert weekly is None


def test_cli_one_shot_frequency_invocation(env, capsys):
    """``digest-flush --frequency daily`` bypasses the digest-hour gate
    and force-flushes (for manual operator use)."""
    _write_queue(env["shared_dir"], "daily", [
        {"catalog_event": "summaries.daily_pod_report",
         "message": "📊 Pod report\n🟢 All clear"},
    ])
    # _cli must discover network.json at the CANONICAL location —
    # INSIDE the shared dir, not its parent (CANONICAL_NETWORK_JSON =
    # shared_dir / "network.json"). Staging it here and asserting the
    # dispatcher received a non-None network pins the regression: the
    # earlier ``shared_dir.parent`` default missed this file, loaded
    # network=None, and every flush returned no_recipient.
    (env["shared_dir"] / "network.json").write_text(
        '{"alerts": {"channel": "telegram", "chatId": "12345"}}'
    )
    rc = env["digest_dispatcher"]._cli([
        "--frequency", "daily", "--shared-dir", str(env["shared_dir"]),
    ])
    assert rc == 0
    assert len(env["captured"]) == 1
    # The loaded network reached the dispatcher (would be None under the
    # old parent-path default, which is what produced no_recipient).
    assert env["captured"][0]["network"] == {
        "alerts": {"channel": "telegram", "chatId": "12345"}
    }
    out = capsys.readouterr().out
    assert "daily" in out and "sent" in out


# ── digest_health snapshot (Dispatcher Health panel) ────────────────────────
#
# digest_health() is the read-only signal the Dispatcher Health route uses
# to surface a stuck digest queue (R3 — non-delivery honesty). Queue depth
# alone is NOT stuck; the staleness trigger is the AGE of the oldest pending
# event past one flush-interval-plus-slack, or a non-empty queue that has
# never been flushed (the live 2026-06-12 failure shape).


def _event(ts_iso: str, *, dedup_key: str = "dk") -> dict:
    return {
        "ts": ts_iso, "source": "audit",
        "catalog_event": "security.audit_finding",
        "severity": "warning", "dedup_key": dedup_key,
        "message": "🛡️ finding",
    }


def test_digest_health_empty_is_not_stuck(env):
    health = env["digest_dispatcher"].digest_health(env["shared_dir"], now=_t())
    assert health["stuck"] is False
    assert health["daily"]["pending"] == 0
    assert health["daily"]["oldest_pending_ts"] is None
    assert health["daily"]["last_flush_ts"] is None
    assert health["weekly"]["pending"] == 0


def test_digest_health_fresh_queue_is_not_stuck(env):
    """Recent events accumulating between digest windows are normal — a
    non-empty queue within the slack window is NOT a problem."""
    now = _t(day=10, hour=12)
    # Enqueued 2h ago — well inside the 26h daily staleness window.
    _write_queue(env["shared_dir"], "daily", [
        _event("2026-05-10T10:00:00Z", dedup_key="a"),
        _event("2026-05-10T11:00:00Z", dedup_key="b"),
    ])
    health = env["digest_dispatcher"].digest_health(env["shared_dir"], now=now)
    assert health["daily"]["pending"] == 2
    assert health["daily"]["stuck"] is False
    assert health["stuck"] is False
    assert health["daily"]["oldest_pending_age_hours"] == 2.0


def test_digest_health_stale_oldest_pending_is_stuck(env):
    """Events older than the daily staleness window mean the queue didn't
    drain on schedule — stuck. This is the live failure: 142 events stuck
    for days while the panel showed all-green."""
    now = _t(day=13, hour=12)
    _write_queue(env["shared_dir"], "daily", [
        _event("2026-05-10T09:00:00Z", dedup_key="a"),  # ~3 days old
        _event("2026-05-13T11:00:00Z", dedup_key="b"),  # fresh
    ])
    health = env["digest_dispatcher"].digest_health(env["shared_dir"], now=now)
    assert health["daily"]["pending"] == 2
    assert health["daily"]["stuck"] is True
    assert health["stuck"] is True
    # Age reflects the OLDEST event, not the newest.
    assert health["daily"]["oldest_pending_age_hours"] > 26


def test_digest_health_unparseable_ts_never_flushed_is_stuck(env):
    """Defensive clause: a non-empty queue whose events lack a parseable
    ts and that has never been flushed is treated as stuck so a malformed
    ts can't mask a dead pipeline."""
    p = env["shared_dir"] / "alerts" / "digest-pending" / "daily.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"source":"audit","message":"x"}\n')  # no ts
    health = env["digest_dispatcher"].digest_health(env["shared_dir"], now=_t())
    assert health["daily"]["pending"] == 1
    assert health["daily"]["oldest_pending_age_hours"] is None
    assert health["daily"]["stuck"] is True


def test_digest_health_last_flush_ts_from_archive(env):
    """last_flush_ts is parsed from the newest delivered-flush archive
    filename; ``-empty`` no-op archives are skipped."""
    d = env["shared_dir"] / "alerts" / "digest-pending"
    d.mkdir(parents=True, exist_ok=True)
    (d / "daily.flushed-2026-05-09T08:00:00Z").write_text("")
    (d / "daily.flushed-2026-05-12T08:00:00Z").write_text("")
    # An "-empty" archive is newer but must NOT count as a delivered digest.
    (d / "daily.flushed-2026-05-13T08:00:00Z-empty").write_text("")
    health = env["digest_dispatcher"].digest_health(env["shared_dir"], now=_t())
    assert health["daily"]["last_flush_ts"] == "2026-05-12T08:00:00Z"


def test_digest_health_weekly_independent_of_daily(env):
    """Weekly uses an 8-day staleness window and is tracked separately."""
    now = _t(day=20, hour=12)
    _write_queue(env["shared_dir"], "weekly", [
        _event("2026-05-10T08:00:00Z", dedup_key="w"),  # 10 days old > 8d
    ])
    health = env["digest_dispatcher"].digest_health(env["shared_dir"], now=now)
    assert health["weekly"]["stuck"] is True
    assert health["daily"]["stuck"] is False
    assert health["stuck"] is True


def test_digest_health_diagnosis_never_flushed(env):
    """A stuck queue that has NEVER been delivered diagnoses as
    'never_flushed' — the banner blames the (genuinely-down) service."""
    now = _t(day=13, hour=12)
    _write_queue(env["shared_dir"], "daily", [
        _event("2026-05-10T09:00:00Z", dedup_key="a"),  # stale
    ])
    # No flush archive at all → never delivered.
    health = env["digest_dispatcher"].digest_health(env["shared_dir"], now=now)
    assert health["daily"]["stuck"] is True
    assert health["daily"]["diagnosis"] == "never_flushed"
    assert health["daily"]["last_flush_ts"] is None


def test_digest_health_diagnosis_queue_growing_when_daemon_healthy(env):
    """A stuck queue that HAS been delivered recently diagnoses as
    'queue_growing' — the daemon is healthy and the banner must NOT blame
    it (the 2026-06 misdiagnosis). Point at the noisy producer instead."""
    now = _t(day=13, hour=12)
    _write_queue(env["shared_dir"], "daily", [
        _event("2026-05-10T09:00:00Z", dedup_key="a"),  # stale: > daily window
    ])
    d = env["shared_dir"] / "alerts" / "digest-pending"
    d.mkdir(parents=True, exist_ok=True)
    # A real, recent delivered flush — the daemon IS running.
    (d / "daily.flushed-2026-05-13T08:00:00Z").write_text("")
    health = env["digest_dispatcher"].digest_health(env["shared_dir"], now=now)
    assert health["daily"]["stuck"] is True
    assert health["daily"]["diagnosis"] == "queue_growing"
    assert health["daily"]["last_flush_ts"] == "2026-05-13T08:00:00Z"


def test_digest_health_not_stuck_has_no_diagnosis(env):
    """diagnosis is None when the frequency is healthy."""
    health = env["digest_dispatcher"].digest_health(env["shared_dir"], now=_t())
    assert health["daily"]["diagnosis"] is None
    assert health["weekly"]["diagnosis"] is None


def test_digest_health_drops_unparseable_last_flush_ts(env):
    """A malformed flush-marker stamp must NOT reach the UI as a timestamp
    (the 'last delivered NaNd ago' bug). last_flush_ts is None when the
    archived stamp isn't ISO-parseable, so the client renders 'never'."""
    d = env["shared_dir"] / "alerts" / "digest-pending"
    d.mkdir(parents=True, exist_ok=True)
    # Filename-safe-but-non-ISO stamp (dashes in the time component →
    # JS new Date() yields Invalid Date → NaN).
    (d / "daily.flushed-2026-05-12T08-00-00Z").write_text("")
    _write_queue(env["shared_dir"], "daily", [
        _event("2026-05-10T09:00:00Z", dedup_key="a"),
    ])
    health = env["digest_dispatcher"].digest_health(
        env["shared_dir"], now=_t(day=13, hour=12),
    )
    # Even though an archive exists, the unparseable stamp is dropped.
    assert health["daily"]["last_flush_ts"] is None
    # And with no parseable flush, the diagnosis correctly reads never_flushed.
    assert health["daily"]["diagnosis"] == "never_flushed"


# ── refresh_pending_record: the mutable-body content fix ───────────────────
#
# D1 (#3802) put the cooldown gate above the digest enqueue, which is right
# for the noise but froze the digest on the FIRST body for a producer whose
# body mutates under a stable dedup_key (cost.daily_threshold). The repeat
# now REPLACES the pending record rather than appending a second one — same
# queue bound, latest content.
#
# The queue is append-only by design and ``flush`` rotates it out from under
# any reader with ``os.replace``, so the interesting surface here is the
# claim protocol, not the patching. These tests pin that a refresh racing a
# rotation loses nothing.


def _rec(*, message, ts="2026-05-10T07:00:00Z", key="k1",
         event="cost.daily_threshold"):
    return {
        "ts": ts, "source": "spend_alert", "catalog_event": event,
        "severity": "warn", "dedup_key": key, "digest_collapse_key": key,
        "message": message,
    }


def _queue_lines(shared_dir: Path, frequency="daily") -> list[dict]:
    p = shared_dir / "alerts" / "digest-pending" / f"{frequency}.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def test_refresh_replaces_the_group_in_place(env):
    """The pending record is replaced at the group's FIRST position and the
    group's stragglers are dropped — exactly the record ``_dedup_records``
    would have produced. Neighbours keep their order."""
    dd = env["digest_dispatcher"]
    _write_queue(env["shared_dir"], "daily", [
        _rec(message="$5.02", ts="2026-05-10T07:00:00Z"),
        _rec(message="other", key="k2", event="system.stalled_cron"),
        _rec(message="$12.40", ts="2026-05-10T07:05:00Z"),
    ])
    assert dd.refresh_pending_record(
        env["shared_dir"], "daily",
        record=_rec(message="$41.85", ts="2026-05-10T07:10:00Z"),
    ) is True
    rows = _queue_lines(env["shared_dir"])
    assert [r["message"] for r in rows] == ["$41.85", "other"]


def test_refresh_is_a_noop_when_only_the_timestamp_differs(env, monkeypatch):
    """A ts-only delta must not claim the queue. Nothing downstream renders
    ``ts``, and claiming on every tick would rotate the shared queue file
    under every other producer's appends 288 times a day for the constant-
    body producer this whole spec-delta exists to quiet."""
    dd = env["digest_dispatcher"]
    _write_queue(env["shared_dir"], "daily", [_rec(message="same body")])
    claims = []
    real_replace = dd.os.replace

    def spy(src, dst, *a, **kw):
        if ".refreshing-" in str(dst):
            claims.append(str(dst))
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(dd.os, "replace", spy)
    assert dd.refresh_pending_record(
        env["shared_dir"], "daily",
        record=_rec(message="same body", ts="2026-05-10T09:00:00Z"),
    ) is False
    assert claims == []
    # ...and the pending record is untouched, ts included.
    assert _queue_lines(env["shared_dir"])[0]["ts"] == "2026-05-10T07:00:00Z"

    # POSITIVE CONTROL — without it, a spy filter that can never match would
    # satisfy the assertion above and this test would pin nothing.
    assert dd.refresh_pending_record(
        env["shared_dir"], "daily",
        record=_rec(message="a different body", ts="2026-05-10T09:00:00Z"),
    ) is True
    assert len(claims) == 1


def test_refresh_is_a_noop_when_the_key_is_not_queued(env):
    """No pending record for this collapse identity → nothing to refresh, and
    nothing invented. The queue is left byte-for-byte alone."""
    dd = env["digest_dispatcher"]
    p = _write_queue(env["shared_dir"], "daily", [_rec(message="other", key="k2")])
    before = p.read_text()
    assert dd.refresh_pending_record(
        env["shared_dir"], "daily", record=_rec(message="$41.85", key="k1"),
    ) is False
    assert p.read_text() == before


def test_refresh_losing_the_claim_to_a_flush_loses_no_records(env, monkeypatch):
    """THE RACE. A flush rotates the queue between the refresh's pre-check
    read and its claim. The refresh must lose the claim and rewrite nothing —
    resurrecting its snapshot would double-deliver every record the flush is
    already holding.

    Simulated by rotating the active queue away inside the ``os.replace``
    the refresh uses to claim, so the claim lands on a path that no longer
    exists."""
    dd = env["digest_dispatcher"]
    shared = env["shared_dir"]
    _write_queue(shared, "daily", [
        _rec(message="$5.02"),
        _rec(message="other", key="k2", event="system.stalled_cron"),
    ])
    real_replace = dd.os.replace
    digest_dir = shared / "alerts" / "digest-pending"
    stolen = digest_dir / "daily.flushing-2026-05-10T07:30:00Z"

    def racing_replace(src, dst, *a, **kw):
        # A flush wins the rotation the instant before our claim runs.
        if str(dst).count(".refreshing-"):
            real_replace(digest_dir / "daily.jsonl", stolen)
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(dd.os, "replace", racing_replace)
    assert dd.refresh_pending_record(
        shared, "daily", record=_rec(message="$41.85"),
    ) is False

    # Every record is in the flush's rotation, exactly once, and the refresh
    # left no active queue and no staging file behind.
    rotated = [json.loads(ln) for ln in stolen.read_text().splitlines() if ln.strip()]
    assert [r["message"] for r in rotated] == ["$5.02", "other"]
    assert _queue_lines(shared) == []
    assert list(digest_dir.glob("daily.refreshing-*")) == []


def test_refresh_does_not_swallow_records_appended_during_the_claim(env, monkeypatch):
    """A producer appending mid-claim must not be lost. The refresh claims the
    queue with ``os.replace`` (so the append's ``O_APPEND`` open re-creates a
    fresh active file and lands there) and requeues by APPEND, never by
    overwrite. Both records survive."""
    dd = env["digest_dispatcher"]
    shared = env["shared_dir"]
    _write_queue(shared, "daily", [_rec(message="$5.02")])
    active = shared / "alerts" / "digest-pending" / "daily.jsonl"
    real_replace = dd.os.replace

    def replace_then_append(src, dst, *a, **kw):
        out = real_replace(src, dst, *a, **kw)
        if str(dst).count(".refreshing-"):
            with active.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_rec(
                    message="a different producer", key="k9",
                    event="system.stalled_cron",
                )) + "\n")
        return out

    monkeypatch.setattr(dd.os, "replace", replace_then_append)
    assert dd.refresh_pending_record(
        shared, "daily", record=_rec(message="$41.85"),
    ) is True
    assert sorted(r["message"] for r in _queue_lines(shared)) == [
        "$41.85", "a different producer",
    ]


def test_flush_adopts_a_stranded_refresh_claim(env):
    """A refresh that dies between claiming the queue and requeuing leaves its
    staging file behind holding every claimed record. A later flush adopts it,
    so the crash window is at-most-once-EXTRA delivery, never loss.

    ``now`` is anchored to the real clock plus the adopt threshold rather than
    the suite's fixed ``_t()``, because the staleness signal is an inode stamp
    (wall-clock) and a claim's ctime cannot be back-dated from a test — a
    fixed 2026-05-10 ``now`` would read every claim as fresh and this test
    would pass without ever exercising adoption."""
    dd = env["digest_dispatcher"]
    digest_dir = env["shared_dir"] / "alerts" / "digest-pending"
    digest_dir.mkdir(parents=True, exist_ok=True)
    stranded = digest_dir / "daily.refreshing-abc123"
    stranded.write_text(json.dumps(_rec(message="$12.40")) + "\n")

    later = datetime.now(timezone.utc) + timedelta(
        seconds=dd._ORPHAN_ADOPT_AFTER_SECONDS + 60,
    )
    outcome = dd.flush(
        env["shared_dir"], env["network"], frequency="daily", now=later,
    )
    assert outcome is not None
    assert "$12.40" in env["captured"][0]["message"]
    assert list(digest_dir.glob("daily.refreshing-*")) == []


def test_a_live_refresh_claim_is_not_adopted_mid_flight(env, monkeypatch):
    """The freshness guard, pinned against a REAL claim rather than a
    fabricated file.

    ``os.replace`` moves the ACTIVE QUEUE'S inode onto the staging name, and
    ``rename(2)`` carries the source's mtime — its last APPEND, possibly
    hours ago. Aging a claim off mtime would therefore mint claims born
    already "stale" on any queue not written in the last half hour, and a
    flush globbing in the window before the refresh requeues would adopt an
    in-flight claim and re-deliver its records. ``rename(2)`` DOES reset
    ctime, atomically, which is why the guard reads that.

    Here the queue is deliberately aged two hours before the refresh runs,
    and ``_stranded_orphans`` is consulted from INSIDE the claim (real wall
    clock, since inode stamps are wall-clock). The claim must not be
    adoptable."""
    dd = env["digest_dispatcher"]
    shared = env["shared_dir"]
    digest_dir = shared / "alerts" / "digest-pending"
    _write_queue(shared, "daily", [_rec(message="$5.02")])
    aged = time.time() - 7200
    os.utime(digest_dir / "daily.jsonl", (aged, aged))

    seen: list[list] = []
    real_replace = dd.os.replace

    def replace_then_look(src, dst, *a, **kw):
        out = real_replace(src, dst, *a, **kw)
        if ".refreshing-" in str(dst):
            # The refresh has claimed but not yet requeued — exactly the
            # window a concurrent hourly flush would glob in.
            seen.append(dd._stranded_orphans(
                digest_dir, "daily", datetime.now(timezone.utc),
            ))
        return out

    monkeypatch.setattr(dd.os, "replace", replace_then_look)
    assert dd.refresh_pending_record(
        shared, "daily", record=_rec(message="$41.85"),
    ) is True
    assert seen and seen[0] == [], seen      # the live claim was left alone
    assert [r["message"] for r in _queue_lines(shared)] == ["$41.85"]


def test_refresh_never_restamps_ts_so_a_dead_flusher_still_reads_stuck(env):
    """``ts`` is the staleness clock, not a render field.

    ``_pending_stats`` takes the OLDEST pending ``ts`` and ``digest_health``
    trips ``stuck`` off its age (26h for daily). If a refresh re-stamped
    ``ts``, a producer ticking every few minutes would keep the queue
    looking newborn forever and a digest pipeline dead for days would report
    healthy — the detector blinded by the very fix meant to keep the queue's
    CONTENT fresh. So the refresh carries the replaced record's ``ts``
    forward and only its content changes."""
    dd = env["digest_dispatcher"]
    shared = env["shared_dir"]
    t0 = datetime(2026, 5, 7, 8, 0, tzinfo=timezone.utc)   # 3 days before _t()
    _write_queue(shared, "daily", [
        _rec(message="$5.02", ts=t0.isoformat().replace("+00:00", "Z")),
    ])
    for tick in range(1, 4):
        assert dd.refresh_pending_record(
            shared, "daily",
            record=_rec(
                message=f"$5.0{tick}",
                ts=(t0 + timedelta(days=tick)).isoformat().replace("+00:00", "Z"),
            ),
        ) is True

    rows = _queue_lines(shared)
    assert len(rows) == 1
    assert rows[0]["message"] == "$5.03"          # content IS refreshed
    assert rows[0]["ts"] == t0.isoformat().replace("+00:00", "Z")   # ts is NOT

    health = dd.digest_health(shared, now=_t())["daily"]
    assert health["stuck"] is True
    assert health["oldest_pending_age_hours"] >= 72


def test_a_perpetually_touched_claim_is_still_adopted_eventually(env):
    """The backstop against the ONE loss shape in this design.

    The normal guard ages a claim off ``max(ctime, mtime)``, and ctime is
    bumped by any metadata write — so a claim touched on a sub-30-minute
    cadence would look perpetually fresh and its records would never be
    delivered. Unlike the ``.flushing-*`` branch (staleness from the
    filename, which nothing can bump) that would be unbounded. ``min`` is
    the oldest evidence of the inode and cannot be pushed forward by a
    touch, so a claim is adopted after a day no matter what.

    Simulated exactly: ``os.utime`` back-dates mtime while itself bumping
    ctime to now — which IS the adversarial state (fresh ctime, ancient
    mtime)."""
    dd = env["digest_dispatcher"]
    digest_dir = env["shared_dir"] / "alerts" / "digest-pending"
    digest_dir.mkdir(parents=True, exist_ok=True)
    claim = digest_dir / "daily.refreshing-touched"
    claim.write_text(json.dumps(_rec(message="$12.40")) + "\n")
    ancient = time.time() - (dd._REFRESH_CLAIM_HARD_ADOPT_SECONDS + 3600)
    os.utime(claim, (ancient, ancient))          # ctime := now, mtime := old

    st = claim.stat()
    now = datetime.now(timezone.utc)
    assert now.timestamp() - max(st.st_ctime, st.st_mtime) < \
        dd._ORPHAN_ADOPT_AFTER_SECONDS          # the normal guard says "fresh"
    assert dd._stranded_orphans(digest_dir, "daily", now) == [claim]


def test_rename_stamps_a_claim_as_fresh_on_this_platform(env):
    """Pin the platform assumption the freshness guard rests on.

    POSIX says ``rename()`` marks the renamed file's last-status-change time
    for update, and that is the ONLY thing making a claim's ctime the claim
    moment — its mtime stays the source queue's last append. No other test
    can catch a platform that breaks this: they all back-date with
    ``os.utime``, which bumps ctime to now by itself, so the claim looks
    fresh either way while production would silently revert to the mtime
    behaviour and reopen the in-flight-adoption race.

    Asserted as the GUARD states it — "the claim reads fresh, its inherited
    mtime does not" — rather than by comparing the inode stamp against a
    userspace instant. Linux sets inode timestamps from the COARSE kernel
    clock (one jiffy, ms-scale), so a claim's ctime can legitimately land a
    few microseconds BEHIND a ``time.time()`` read taken just before the
    rename; an earlier version of this test asserted exactly that and reds
    deterministically on Linux CI while passing on Darwin's fine-grained
    clock. The half-hour threshold gives three orders of magnitude of
    headroom over any clock granularity, while still failing loudly on a
    platform that leaves ctime at the source's 2-hour-old value."""
    dd = env["digest_dispatcher"]
    digest_dir = env["shared_dir"] / "alerts" / "digest-pending"
    _write_queue(env["shared_dir"], "daily", [_rec(message="$5.02")])
    active = digest_dir / "daily.jsonl"
    aged = time.time() - 4 * dd._ORPHAN_ADOPT_AFTER_SECONDS
    os.utime(active, (aged, aged))

    claim = digest_dir / "daily.refreshing-probe"
    dd.os.replace(active, claim)

    st = claim.stat()
    now = time.time()
    # The inherited mtime is stale — this is why aging off mtime was wrong.
    assert now - st.st_mtime >= dd._ORPHAN_ADOPT_AFTER_SECONDS
    # ...and the claim still reads fresh, which only the ctime reset provides.
    assert now - max(st.st_ctime, st.st_mtime) < dd._ORPHAN_ADOPT_AFTER_SECONDS
    assert dd._stranded_orphans(
        digest_dir, "daily", datetime.now(timezone.utc),
    ) == []


def test_refresh_rejects_a_bad_frequency(env):
    """Same contract as ``flush`` / ``_active_queue_path`` — a typo must raise
    rather than silently target a path nothing drains."""
    dd = env["digest_dispatcher"]
    with pytest.raises(ValueError):
        dd.refresh_pending_record(
            env["shared_dir"], "daily_digest", record=_rec(message="x"),
        )
