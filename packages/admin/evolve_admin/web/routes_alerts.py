"""HTTP routes for the Alerts → Subscriptions tab — Phase A4.

Routes that the (Phase B) UI tab consumes:

  GET  /api/alerts/subscriptions
       Full catalog with operator preferences overlaid, grouped by
       category. Includes channel info, digest_hour_local, and a
       dispatcher_enabled diagnostic flag. Events whose producer
       source is disabled (alerts.<source>.enabled = false) are
       filtered out — there's no point exposing a subscription whose
       underlying signal isn't firing.

  POST /api/alerts/subscriptions
       Update one or more subscriptions. Body either:
         {"event_key": "...", "enabled"?: bool, "frequency"?: str}
       or for batch updates:
         {"updates": [{...}, {...}, ...]}
       Returns the resolved subscription state for the touched events.

  POST /api/alerts/subscriptions/test
       Body: {"event_key": "..."}
       Renders the event's body_template with its catalog sample_payload,
       prepends a clear test marker, and dispatches via the existing
       dispatcher (without catalog_event so subscription gating doesn't
       block the test). Returns rendered text + DispatchResult.

  POST /api/alerts/subscriptions/reset
       Wipes operator overrides; everything reverts to catalog defaults.
       Returns the post-reset full-catalog payload (same shape as GET).

  GET  /api/alerts/dispatcher-log
       Tail of dispatcher.jsonl (actual sends; deferred-to-digest rows
       live in dispatcher-queued.jsonl behind ``queued=1``). Powers
       the Recent Messages list. Historic ``result==failed`` entries are
       filtered out so the messages feed is clean immediately after the
       failures-log split lands.

  GET  /api/alerts/delivery-failures
       Dispatcher Health data for the panel above the Messages list:
       transport failures (delivery-failures.jsonl) + silent
       non-delivery (no_recipient, from the suppressed log) + a
       digest-pipeline snapshot, rolled up into summary.healthy.

See ``internal/spec-alert-subscriptions-2026-05-10.md`` for design.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, Response
from flask.typing import ResponseReturnValue

from ..alerts import catalog as _cat
from ..alerts import dispatcher as _disp
from ..alerts import subscriptions as _subs
from ..config import load_network

logger = logging.getLogger(__name__)


def register_alerts_subscription_routes(app: Flask, network_path: Path) -> None:
    """Register the four /api/alerts/subscriptions* routes."""

    def _shared_dir() -> Path:
        return Path(
            load_network(network_path).get("sharedDir", "/Users/Shared/evolve")
        )

    def _network() -> dict[str, Any]:
        return load_network(network_path)

    # ── GET /api/alerts/subscriptions ─────────────────────────────────────

    @app.get("/api/alerts/subscriptions")
    def api_alerts_subscriptions_get() -> Response:
        """Return the 13 operator-facing Subscription groups (Phase 2).

        Shape:
          {
            "channel":  "telegram",          # or null
            "chat_id":  "12345",             # or null
            "digest_hour_local": 8,
            "dispatcher_enabled": true,
            "pwa_push_enabled": false,
            "subscriptions": [
              {
                "id":                 "security_findings",
                "label":              "Security findings",
                "description":        "...",
                "enabled":            true,       # the group on/off (the toggle)
                "is_safety_critical": true,       # → confirm-warning on mute (chip 2)
                "is_overridden":      false,
                "default_enabled":    true,
                "member_event_count": 5,
                "member_event_keys":  ["security.audit_finding", ...],
                "events": [                       # member event detail (frequency etc.)
                  {
                    "key": "security.audit_finding",
                    "label": "New audit finding",
                    "subscription_id": "security_findings",
                    "severity": "error",
                    "default_frequency": "immediate",
                    "allowed_frequencies": [...],
                    "subscription": {"enabled": true, "frequency": "immediate", ...}
                  },
                  ...
                ]
              },
              ...
            ],
            "event_index": {"security.audit_finding": "security_findings", ...}
          }

        ``event_index`` maps every (source-enabled) member event key to its
        group id — the Messages-tab provenance (chip 2) resolves a
        dispatch row's ``catalog_event`` to its group through it.
        """
        return jsonify(_build_subscriptions_payload(_shared_dir(), _network()))

    # ── POST /api/alerts/subscriptions ────────────────────────────────────

    @app.post("/api/alerts/subscriptions")
    def api_alerts_subscriptions_post() -> ResponseReturnValue:
        """Persist operator-set subscriptions.

        Phase 2: the operator surface is the 13-Subscription **group**.
        Toggling a group gates every member event.

        Group toggle (preferred):
            {"subscription_id": "...", "enabled": bool}
        Batch of group toggles:
            {"updates": [{"subscription_id": "...", "enabled": bool}, ...]}

        Per-event ``frequency`` is still operator-tunable (digest cadence
        is event-shaped); a per-event override that carries ONLY a
        ``frequency`` (no ``enabled``) is still accepted for back-compat:
            {"event_key": "...", "frequency": "..."}

        A per-event ``enabled`` is NO LONGER the on/off surface — the
        group is. An update carrying ``event_key`` + ``enabled`` is
        rejected with guidance to toggle the group instead, so the UI and
        any stale caller fail loud rather than silently writing a toggle
        that no longer gates.
        """
        body = request.get_json(silent=True) or {}
        if "updates" in body:
            updates = body["updates"]
            if not isinstance(updates, list):
                return jsonify({"error": "updates must be a list"}), 400
        elif "subscription_id" in body or "event_key" in body:
            updates = [body]
        else:
            return jsonify({
                "error": (
                    "body must include subscription_id (group toggle) or "
                    "event_key (per-event frequency) — single or in updates[]"
                ),
            }), 400

        groups_resolved = []
        events_resolved = []
        sd = _shared_dir()
        for u in updates:
            if not isinstance(u, dict):
                return jsonify({"error": "each update must be a dict"}), 400

            # ── Group toggle ─────────────────────────────────────────────
            sub_id = u.get("subscription_id")
            if sub_id:
                enabled = u.get("enabled")
                if not isinstance(enabled, bool):
                    return jsonify({
                        "error": "group toggle requires enabled: bool",
                        "subscription_id": sub_id,
                    }), 400
                try:
                    group = _subs.write_subscription_group(
                        sd, sub_id, enabled=enabled,
                    )
                except KeyError as exc:
                    return jsonify({
                        "error": str(exc), "subscription_id": sub_id,
                    }), 404
                groups_resolved.append(_group_view(group))
                continue

            # ── Per-event frequency (on/off no longer accepted here) ─────
            event_key = u.get("event_key")
            if not event_key:
                return jsonify({
                    "error": "update missing subscription_id or event_key",
                }), 400
            if "enabled" in u:
                return jsonify({
                    "error": (
                        "per-event enabled is no longer the on/off surface — "
                        "toggle the event's subscription group instead"
                    ),
                    "event_key": event_key,
                }), 400
            try:
                sub = _subs.write_subscription(
                    sd, event_key,
                    enabled=None,
                    frequency=u.get("frequency"),
                )
            except KeyError as exc:
                return jsonify({"error": str(exc), "event_key": event_key}), 404
            except ValueError as exc:
                return jsonify({"error": str(exc), "event_key": event_key}), 400
            events_resolved.append(_subscription_view(sub))

        return jsonify({
            "updated_groups": groups_resolved,
            "updated_events": events_resolved,
        })

    # ── POST /api/alerts/subscriptions/test ───────────────────────────────

    @app.post("/api/alerts/subscriptions/test")
    def api_alerts_subscriptions_test() -> Response:
        """Fire a sample message for the event_key.

        Renders the catalog event using its built-in sample_payload (so
        the operator sees realistic content), prepends a clear test
        marker, and pushes through the dispatcher *without* passing
        catalog_event — subscription gating doesn't apply to tests.

        The source-level toggle (alerts.<source>.enabled) DOES still
        gate the test, since it operates one layer below subscriptions.
        Operators turning off a source-level toggle as a kill-switch
        get a SUPPRESSED_DISABLED back, which is correct.
        """
        body = request.get_json(silent=True) or {}
        event_key = body.get("event_key")
        if not event_key:
            return jsonify({"error": "body must include event_key"}), 400

        entry = _cat.by_key(event_key)
        if entry is None:
            return jsonify({"error": "unknown event_key", "event_key": event_key}), 404

        try:
            rendered = _cat.render_event(entry, entry.sample_payload)
        except KeyError as exc:
            return jsonify({
                "error": f"sample_payload missing key {exc}",
                "event_key": event_key,
            }), 500

        marker = "[Subscriptions tab — test message]\n\n"
        message = marker + rendered

        outcome = _disp.send(
            shared_dir=_shared_dir(),
            network=_network(),
            source=entry.producer_source,
            message=message,
            severity=entry.severity,
            dedup_key=f"test/{event_key}/{datetime.now(timezone.utc).timestamp():.0f}",
        )

        return jsonify({
            "rendered": rendered,
            "result": outcome.result.value,
            "channel": outcome.channel,
            "error": outcome.error,
        })

    # ── POST /api/alerts/subscriptions/reset ──────────────────────────────

    @app.post("/api/alerts/subscriptions/reset")
    def api_alerts_subscriptions_reset() -> Response:
        sd = _shared_dir()
        _subs.reset(sd)
        return jsonify(_build_subscriptions_payload(sd, _network()))

    # ── POST /api/alerts/digest-hour ──────────────────────────────────────

    @app.get("/api/alerts/dispatcher-log")
    def api_alerts_dispatcher_log() -> Response:
        """Return recent entries from the dispatcher's log for in-UI display.

        Powers Reports → Subscriptions → Messages. Reads
        ``{shared_dir}/alerts/dispatcher.jsonl`` so the operator can see
        what actually went out without grepping logs on the mini.

        Transient ``result=="failed"`` deliveries live in a separate file
        (``delivery-failures.jsonl``) surfaced on the Dispatcher Health
        panel — and historic failed rows that pre-date the split may still
        be in ``dispatcher.jsonl``. We filter ``failed`` out here so the
        Messages list isn't polluted with will-retry noise. Terminal
        ``result=="dead_letter"`` rows (permanently-undeliverable messages
        the dispatcher gave up on) are deliberately NOT filtered — they are
        the operator's ground-truth record that a send was lost, and would
        otherwise leave the feed looking idle during a delivery outage.

        ``result=="deferred"`` gets the same treatment for the same reason.
        A deferred message is queued for a future digest, so it is not
        "what actually got sent"; it now lands in its own
        ``dispatcher-queued.jsonl`` lane. Rows written BEFORE that split
        are still sitting in ``dispatcher.jsonl``, so the sent stream is
        post-filtered here too — without it, months of deferral history
        (287 of 292 rows on the live pod on 2026-08-25) leak back into the
        default feed. When ``queued=1`` those historic rows are kept
        instead of dropped, so the queued lane shows its full history and
        not just what landed after the split.

        Query params:
            limit      — max records to return (default 100, max 500)
            suppressed — "1" to include the suppressed lane (default off)
            queued     — "1" to include the queued (deferred-to-digest)
                         lane (default off)
        """
        from evolve_admin.alerts.dispatcher import iter_log_records_recent
        try:
            limit = max(1, min(500, int(request.args.get("limit", 100))))
        except (TypeError, ValueError):
            limit = 100
        include_suppressed = request.args.get("suppressed") in ("1", "true", "yes")
        include_queued = request.args.get("queued") in ("1", "true", "yes")
        shared = _shared_dir()
        filenames = ["dispatcher.jsonl"]
        if include_queued:
            filenames.append("dispatcher-queued.jsonl")
        if include_suppressed:
            filenames.append("dispatcher-suppressed.jsonl")

        records: list[dict[str, Any]] = []
        # 4× headroom on the per-stream cap mirrors the previous tail-
        # readlines() approach — leaves room for the post-filter
        # ``result=="failed"`` exclusion so a stream that happens to
        # carry historic failures still surfaces some Messages.
        #
        # iter_log_records_recent reads the TAIL of each day file (newest day
        # first), not the head — so on a high-volume day (e.g. the ACL-flap
        # storm: 8k+ lines in a single day file) the cap collects the most
        # RECENT records, not the oldest ~400 of the newest day. The earlier
        # head-reader (iter_log_records) hit the cap on early-morning records
        # and broke before reaching anything recent, leaving the Messages feed
        # stale ("newest 19h ago" while sends were 2 min old).
        per_stream_cap = limit * 4
        for filename in filenames:
            count = 0
            for rec in iter_log_records_recent(
                shared, filename, max_records=per_stream_cap
            ):
                result = rec.get("result")
                # Drop historic FAILED entries from the messages feed —
                # they live in delivery-failures.jsonl post-split.
                if result == "failed":
                    continue
                # Same post-split treatment for DEFERRED: it belongs to
                # dispatcher-queued.jsonl now, but pre-split rows are still
                # in dispatcher.jsonl. Keep them only when the caller asked
                # for the queued lane, so ``queued=1`` surfaces the lane's
                # history rather than only post-split rows.
                if result == "deferred" and not include_queued:
                    continue
                records.append(rec)
                count += 1
                if count >= per_stream_cap:
                    break
        # Newest first
        records.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return jsonify({"records": records[:limit]})

    # ── GET /api/alerts/delivery-failures ─────────────────────────────────

    @app.get("/api/alerts/delivery-failures")
    def api_alerts_delivery_failures() -> Response:
        """Return Dispatcher Health data: transport failures + silent
        non-delivery + digest-pipeline state.

        The panel above the Recent Messages list is the one place an
        operator looks to learn whether alert delivery is working, so it
        must surface every way an alert can fail to reach them — not just
        upstream transport errors:

          * ``records`` / ``summary.by_channel`` — failed sends (telegram
            HTTP 4xx, slack rate-limit, openclaw subprocess error) from
            ``{shared_dir}/alerts/delivery-failures.jsonl``.
          * ``summary.non_delivery`` — ``no_recipient`` outcomes. These
            live in the *suppressed* log (NO_RECIPIENT is a suppressed
            result) but, unlike disabled/cooldown/identical, they are NOT
            an intended mute: the operator subscribed, the dispatcher
            tried, and no recipient was resolvable. Read from the
            suppressed log here rather than re-routing it, so other
            suppressed-log readers are unaffected.
          * ``summary.digest`` — digest-pipeline queue depth + last-flush
            age + a ``stuck`` flag (a snapshot, not windowed). A queue
            that isn't draining is the unambiguous "digests aren't
            flowing" signal (the 2026-06-12 incident: 142 daily events
            stuck, zero flushes ever, panel all-green).

        ``summary.healthy`` is the single roll-up the UI keys its green vs
        degraded state off of: no transport failures AND no non-delivery
        AND no stuck digest in the window/snapshot.

        Same auth gating as ``/api/alerts/dispatcher-log`` — both are
        Flask routes registered on the same app, which Flask-Login (or
        whatever guard the server applies via before_request) covers
        uniformly across all admin-UI endpoints.

        Query params:
            limit        — max records to return (default 100, max 500)
            window_hours — only include failures within the last N hours
                           (default 24, max 168 = one week)
        """
        from datetime import datetime, timedelta, timezone
        from evolve_admin.alerts.dispatcher import iter_log_records
        from evolve_admin.alerts.digest_dispatcher import digest_health
        from evolve_admin.alerts.rate_breaker import breaker_health
        from evolve_admin.alerts.flapper import flap_health

        try:
            limit = max(1, min(500, int(request.args.get("limit", 100))))
        except (TypeError, ValueError):
            limit = 100
        try:
            window_hours = max(1, min(168, int(request.args.get("window_hours", 24))))
        except (TypeError, ValueError):
            window_hours = 24

        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        shared = _shared_dir()
        per_stream_cap = limit * 4

        def _window_tail(filename: str, keep) -> list[dict[str, Any]]:
            """Newest-first, window-filtered records from a log stream that
            satisfy ``keep(rec)``."""
            out: list[dict[str, Any]] = []
            for rec in iter_log_records(shared, filename, since=cutoff):
                if not keep(rec):
                    continue
                ts_raw = rec.get("ts")
                if ts_raw:
                    try:
                        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                        if ts < cutoff:
                            continue
                    except (ValueError, TypeError):
                        pass
                out.append(rec)
                if len(out) >= per_stream_cap:
                    break
            out.sort(key=lambda r: r.get("ts", ""), reverse=True)
            return out[:limit]

        # ── Transport failures (delivery-failures.jsonl) ──────────────────
        records = _window_tail("delivery-failures.jsonl", lambda r: True)

        # Aggregate by channel; most_recent_ts is the newest failure across
        # all channels. The UI's compact form uses this; the row list is for
        # the expanded "View failures" view.
        by_channel: dict[str, dict[str, Any]] = {}
        for r in records:
            ch = r.get("channel") or "unknown"
            slot = by_channel.setdefault(ch, {"channel": ch, "count": 0, "sample_error": None})
            slot["count"] += 1
            if not slot["sample_error"] and r.get("error"):
                slot["sample_error"] = r["error"]
        channel_summary = sorted(
            by_channel.values(), key=lambda s: s["count"], reverse=True,
        )

        # ── Silent non-delivery (no_recipient, from the suppressed log) ───
        nd_records = _window_tail(
            "dispatcher-suppressed.jsonl",
            lambda r: r.get("result") == "no_recipient",
        )
        by_source: dict[str, dict[str, Any]] = {}
        for r in nd_records:
            src = r.get("source") or "unknown"
            slot = by_source.setdefault(src, {"source": src, "count": 0, "sample_error": None})
            slot["count"] += 1
            if not slot["sample_error"] and r.get("error"):
                slot["sample_error"] = r["error"]
        non_delivery = {
            "total":          len(nd_records),
            "most_recent_ts": nd_records[0]["ts"] if nd_records else None,
            "by_source":      sorted(
                by_source.values(), key=lambda s: s["count"], reverse=True,
            ),
            "records":        nd_records,
        }

        # ── Digest pipeline (point-in-time snapshot, not windowed) ────────
        digest = digest_health(shared)

        # ── Global rate breaker (point-in-time snapshot) ──────────────────
        # Surfaces normal / batching / storm so a quiet phone is explained
        # by visible state, not silent suppression. Storm/batching is the
        # backstop working as designed — it does NOT flip ``healthy`` to
        # false (that's reserved for delivery FAILURES), but it IS shown.
        rate_breaker = breaker_health(shared)

        # ── Recurring-flapper demotions (point-in-time snapshot) ──────────
        # Signatures auto-demoted to the daily digest because they oscillate
        # multiple times a day (the multi-hour-flap layer above grace). Shown
        # so a condition that stopped paging is explained by visible state,
        # not silent suppression. Like the rate breaker, demotion is the
        # backstop working as designed — it does NOT flip ``healthy`` to false.
        flap_demotions = flap_health(shared)

        summary = {
            "total":             len(records),
            "window_hours":      window_hours,
            "most_recent_ts":    records[0]["ts"] if records else None,
            "by_channel":        channel_summary,
            "non_delivery":      non_delivery,
            "digest":            digest,
            "rate_breaker":      rate_breaker,
            "flap_demotions":    flap_demotions,
            "healthy": (
                len(records) == 0
                and non_delivery["total"] == 0
                and not digest["stuck"]
            ),
        }
        return jsonify({"records": records, "summary": summary})

    @app.post("/api/alerts/digest-hour")
    def api_alerts_digest_hour_post() -> Response:
        """Set the local hour at which the digest flusher fires.

        Body: ``{"hour": <0..23>}``. Interpreted via ``network.timezone``
        by ``digest_dispatcher.flush_if_gated``. Takes effect on the
        next hourly tick — no daemon restart required.
        """
        body = request.get_json(silent=True) or {}
        raw = body.get("hour")
        if raw is None:
            return jsonify({"error": "body must include hour"}), 400
        try:
            hour = int(raw)
        except (TypeError, ValueError):
            return jsonify({"error": "hour must be an integer 0..23"}), 400
        try:
            applied = _subs.write_digest_hour_local(_shared_dir(), hour)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"digest_hour_local": applied})

    # ── Independent security-alert channel (opt-in) ───────────────────────
    #
    # Replaces the setup wizard's old forced "create a second Telegram bot
    # for security alerts" step with a purely additive Settings control.
    # When set, audit.py's CRITICAL-alert direct-Telegram fallback
    # (``_send_security_alert``) uses these dedicated credentials instead of
    # the main bot's token, so security alerts survive even if the main
    # bot's credential is broken. When unset, the main-token fallback is
    # used — the control is opt-in.
    #
    # The two files live under ``{shared_dir}/keystore/``, which is owned by
    # the ``evolve`` user that the admin server runs as. So a plain write +
    # ``os.chmod(0o600)`` is sufficient — the /tmp-staging + ``sudo /bin/cp``
    # dance documented in CLAUDE.md is only for *bot*-owned config files. The
    # token is token-bearing → 0600, and is NEVER echoed back to the client
    # (the GET status reports presence + the non-secret chat id only).

    def _sec_alert_paths() -> "tuple[Path, Path]":
        sd = _shared_dir()
        return (
            sd / "keystore" / "security-alert-token",
            sd / "keystore" / "security-alert-chat-id",
        )

    def _sec_alert_status() -> dict[str, Any]:
        tok_path, chat_path = _sec_alert_paths()
        configured = tok_path.exists() and chat_path.exists()
        chat_id = None
        if configured:
            try:
                chat_id = chat_path.read_text().strip() or None
            except OSError:
                chat_id = None
        return {"securityAlertConfigured": configured, "chatId": chat_id}

    @app.get("/api/config/security-alert")
    def api_config_security_alert_get() -> Response:
        """Report whether a dedicated security-alert channel is configured.

        Returns ``{securityAlertConfigured, chatId}`` only — never the
        token. The settings UI renders a masked "configured" state from
        this.
        """
        return jsonify(_sec_alert_status())

    @app.post("/api/config/security-alert")
    def api_config_security_alert_post() -> ResponseReturnValue:
        """Set / clear / test the opt-in independent security-alert channel.

        Body: ``{"op": "set"|"clear"|"test", "token"?: str, "chatId"?: str}``

          set:   write the token + chat-id keystore files (mode 0600) so
                 audit.py's CRITICAL direct-Telegram fallback uses the
                 dedicated bot instead of the main one.
          clear: delete both files → revert to the main-token fallback.
          test:  getMe-validate a token (the posted one, or the stored one
                 when omitted) without persisting anything.
        """
        body = request.get_json(silent=True) or {}
        op = (body.get("op") or "").strip().lower()
        tok_path, chat_path = _sec_alert_paths()

        if op == "set":
            token = (body.get("token") or "").strip()
            chat_id = (body.get("chatId") or "").strip()
            if not token or not chat_id:
                return jsonify({"error": "token and chatId are both required"}), 400
            tok_path.parent.mkdir(parents=True, exist_ok=True)
            tok_path.write_text(token)
            chat_path.write_text(chat_id)
            # Tighten both to 0600 — token-bearing, multi-user box. The
            # keystore dir is evolve-owned so os.chmod (no sudo) works.
            for p in (tok_path, chat_path):
                try:
                    os.chmod(p, 0o600)
                except OSError as exc:
                    logger.warning(
                        "security-alert: could not chmod 0600 on %s: %s", p, exc)
            return jsonify({"ok": True, **_sec_alert_status()})

        if op == "clear":
            for p in (tok_path, chat_path):
                try:
                    p.unlink()
                except FileNotFoundError:
                    continue  # already absent — that file is effectively cleared
                except OSError as exc:
                    return jsonify({"error": f"could not clear {p.name}: {exc}"}), 500
            return jsonify({"ok": True, **_sec_alert_status()})

        if op == "test":
            token = (body.get("token") or "").strip()
            if not token:
                # Fall back to the stored token so "Test connection" works on
                # an already-saved channel without re-typing the secret.
                try:
                    token = tok_path.read_text().strip()
                except OSError:
                    token = ""
            if not token:
                return jsonify({"error": "no token to test — enter one or save first"}), 400
            username = _telegram_getme(token)
            if username:
                return jsonify({"ok": True, "username": username})
            return jsonify({"ok": False, "error": "Telegram getMe failed — check the token"}), 400

        return jsonify({"error": f"unknown op {op!r}; expected set|clear|test"}), 400


# ── View builders ──────────────────────────────────────────────────────────


def _telegram_getme(token: str) -> "str | None":
    """Validate a Telegram bot token via getMe; return the bot username.

    Reuses ``setup_wizard._test_telegram_token`` when importable so the Bot
    API call lives in one place; falls back to a minimal inline check if the
    (rich-dependent) wizard module can't be imported in this context.
    """
    try:
        from evolve_admin.setup_wizard import _test_telegram_token
        return _test_telegram_token(token)
    except Exception as exc:
        logger.debug(
            "security-alert: setup_wizard._test_telegram_token unavailable, "
            "falling back to inline getMe: %s", exc)
    import json as _json
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/getMe", timeout=10,
        ) as resp:
            data = _json.loads(resp.read())
        if data.get("ok"):
            return data["result"].get("username")
    except Exception as exc:
        logger.debug("security-alert: Telegram getMe failed: %s", exc)
    return None


def _build_subscriptions_payload(
    shared_dir: Path, network: dict[str, Any]
) -> dict[str, Any]:
    """Construct the full GET /api/alerts/subscriptions response.

    Phase 2: returns the **13 operator-facing Subscription groups**, not
    the ~65 per-event toggles. Each group carries its on/off state,
    safety-critical flag, and its member events (so chip 2 can render the
    group with its contents and the Messages-tab provenance can resolve
    an event → group).

    A group is hidden entirely when EVERY member event's producer source
    is disabled (alerts.<source>.enabled = false) — same "don't show a
    subscription whose underlying signal can't fire" principle as the old
    per-event filter, lifted to the group. Member events whose source is
    off are dropped from the group's ``events`` list but the group still
    shows if any member can fire.

    ``event_index`` maps every (source-enabled) member event key → its
    group id, so the Messages tab can stamp provenance without re-deriving
    the mapping client-side.
    """
    alerts_cfg = (network or {}).get("alerts", {}) or {}

    # Cache per-source enabled state — repeating the lookup per event would
    # be N×member reads of the same config value.
    source_enabled_cache: dict[str, bool] = {}
    def _source_on(source: str) -> bool:
        if source not in source_enabled_cache:
            source_enabled_cache[source] = _disp._read_source_enabled(
                shared_dir, source
            )
        return source_enabled_cache[source]

    groups = []
    event_index: dict[str, str] = {}
    for group in _subs.read_all_subscription_groups(shared_dir):
        events = []
        for key in group.member_event_keys:
            entry = _cat.by_key(key)
            if entry is None:
                continue
            if not _source_on(entry.producer_source):
                continue
            sub = _subs.read_subscription(shared_dir, key)
            assert sub is not None  # member keys come from the catalog
            events.append(_event_view(entry, sub))
            event_index[key] = group.id
        if not events:
            # Every member's source is off — hide the group header too.
            continue
        groups.append(_group_view(group, events=events))

    return {
        "channel":             alerts_cfg.get("channel"),
        "chat_id":             alerts_cfg.get("chatId"),
        "digest_hour_local":   _subs.read_digest_hour_local(shared_dir),
        "dispatcher_enabled":  _disp._read_dispatcher_enabled(shared_dir),
        # The Subscriptions UI's "Send to → PWA Push" toggle renders its
        # checked state from this field (alerts-extended.js _alSubsRender).
        # Without it the toggle always painted OFF on (re)load even after
        # the operator enabled push or registered a device — and re-toggling
        # the apparently-off control silently disabled their own fanout. The
        # value is also served by GET /api/push/subscriptions, but that
        # payload feeds the devices list, not this toggle.
        "pwa_push_enabled":    _subs.read_pwa_push_enabled(shared_dir),
        # Phase 2 primary surface: the 13 groups.
        "subscriptions":       groups,
        # event_key → subscription_id, for Messages-tab provenance (chip 2).
        "event_index":         event_index,
    }


def _group_view(
    group: _subs.SubscriptionGroup,
    *,
    events: "list[dict[str, Any]] | None" = None,
) -> dict[str, Any]:
    """Serialize a Subscription group. ``events`` (the source-filtered
    member views) is included only for the GET payload; the POST echo
    omits it (caller doesn't need the full member render)."""
    view: dict[str, Any] = {
        "id":                  group.id,
        "label":               group.label,
        "description":         group.description,
        "enabled":             group.enabled,
        "is_safety_critical":  group.is_safety_critical,
        "is_overridden":       group.is_overridden,
        "default_enabled":     group.default_enabled,
        "member_event_count":  len(group.member_event_keys),
        "member_event_keys":   list(group.member_event_keys),
    }
    if events is not None:
        view["events"] = events
    return view


def _event_view(entry: _cat.CatalogEvent, sub: _subs.Subscription) -> dict[str, Any]:
    return {
        "key":                  entry.key,
        "label":                entry.label,
        "description":          entry.description,
        "severity":             entry.severity.value,
        "is_safety_critical":   entry.is_safety_critical,
        "subscription_id":      entry.subscription,
        "default_enabled":      entry.default_enabled,
        "default_frequency":    entry.default_frequency.value,
        "allowed_frequencies":  [
            {"key": f.value, "label": _cat.FREQUENCY_LABELS[f]}
            for f in entry.allowed_frequencies
        ],
        "subscription":         _subscription_view(sub),
    }


def _subscription_view(sub: _subs.Subscription) -> dict[str, Any]:
    return {
        "event_key":     sub.event_key,
        "enabled":       sub.enabled,
        "frequency":     sub.frequency.value,
        "is_overridden": sub.is_overridden,
    }
