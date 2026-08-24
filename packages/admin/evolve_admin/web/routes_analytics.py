"""HTTP routes for the analytics surface.

GET  /api/analytics/metrics            metrics time series per bot
GET  /api/analytics/cost               cost breakdown per bot
GET  /api/analytics/sessions           session list per bot
GET  /api/analytics/sessions/<id>      single session detail
GET  /api/analytics/activity           activity rhythm (turn rate / topics)
GET  /api/analytics/cache-economics    cache hit/miss economics
GET  /api/analytics/ttl-recommendation TTL recommendation per bot
GET  /api/analytics/curated-sessions   curated session list
GET  /api/analytics/turns              top-cost turns for the Turn Audit table
GET  /api/analytics/usage/by-app       per-app usage rollup, split by attribution grade
GET  /api/analytics/turns/<turn_id>    per-turn drilldown
GET  /api/modules                      module listing per bot
POST /api/bots/<bot_id>/continuity-engine/enable   enable continuity engine
POST /api/bots/<bot_id>/continuity-engine/disable  disable continuity engine
... and related /api/analytics/* /api/modules/* endpoints.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, Response

from ..config import load_network, save_network, bot_home as _bot_home
from .server import _list_manifests_as_bot, resolve_bot_paths
from ..telemetry import get_logger
from .http_errors import error_response

_log = get_logger("web.routes_analytics")

# ── Analyzer import helper (evolve-analyzer is an installed dependency) ──
def _import_analyzer(mod: str):
    import importlib
    return importlib.import_module(mod)


# ── Usage name enrichment (cache-only; no live Slack/Telegram calls) ─────────
def _short_id(raw: str, keep: int = 6) -> str:
    """Shorten an opaque platform id for display. The full id rides a ``title=``
    tooltip + the raw ``user_id`` field, so nothing is lost."""
    s = str(raw or "")
    return s if len(s) <= keep + 1 else s[:keep] + "…"


def _fallback_user_label(platform: str, user_id: str) -> str:
    """A categorized label for an UNresolved user — never a bare opaque id.
    e.g. ``"Slack user · U0AV…"`` / ``"Telegram chat · 126019…"``."""
    short = _short_id(user_id)
    if platform == "slack":
        return f"Slack user · {short}"
    if platform == "telegram":
        return f"Telegram chat · {short}"
    if platform in ("", "unknown"):
        return f"User · {short}"
    return f"{platform[:1].upper()}{platform[1:]} user · {short}"


def _pod_admitted_index(
    network: dict,
) -> "tuple[set[tuple[str, str]], set[str]]":
    """Pod-wide set of ``(platform, external_id)`` admitted to AT LEAST ONE bot,
    plus the set of platforms whose admission was actually readable.

    A user is "admitted somewhere" iff their ``(platform, id)`` appears in any
    bot's per-channel allowFrom, OR they are a pod admin, OR they are a bot's
    primary owner (the last two are admitted by definition). The marker is
    POD-WIDE because the Usage report is pod-wide while admission is per-bot —
    so a user admitted to even one bot must clear the marker everywhere.

    Reuses the SAME allowFrom reader the canonical roster join uses
    (``roster_resolver._default_allowfrom_reader`` → the bot-home file +
    ``sudo /bin/cat`` fallback), so this index and the Users page agree on who
    is admitted. Reads are local files only — no live API, so this is safe on
    the (otherwise cache-only) render path.

    The second return value is the set of platforms that gate marking — a
    platform is in it ONLY when at least one bot's allowFrom file for that
    platform was actually read (a non-None result — present, even if empty).
    The caller marks a By-User row "not admitted" only when its platform is in
    that set; otherwise the marker is suppressed for that row.

    Pod-admins/owners populate the admitted set (so they clear the marker) but
    do NOT add to the readable-platform set: they come from the always-readable
    network.json and say nothing about whether the *allowFrom* membership —
    where most admitted users live — could be read.

    Why per-platform gating: the allowFrom files sit in a mode-700
    ``credentials/`` dir reachable only via the ``sudo /bin/cat`` grant, which
    can go dormant (refresh-sudoers is manual). When it does, allowFrom reads
    fail and the affected platform stays OUT of the readable set, so its users
    are not marked during that window. A missed marker is benign; falsely
    marking an admitted user (the harmful direction) is avoided.
    """
    from ..roster_resolver import ROSTER_CHANNELS, _default_allowfrom_reader
    from .. import roster_overlay as ro

    admitted: set[tuple[str, str]] = set()
    readable_platforms: set[str] = set()

    # Pod admins + per-bot primary owners — admitted everywhere by definition.
    for ch in ROSTER_CHANNELS:
        for aid in ro._pod_admin_ids_for(network, ch):
            admitted.add((ch, aid))

    for bot_id in (network.get("bots") or {}):
        for ch in ROSTER_CHANNELS:
            for pid in ro._primary_owner_ids_for(network, bot_id, ch):
                admitted.add((ch, pid))
        # Per-channel allowFrom membership — the read that gates the marker.
        try:
            reader = _default_allowfrom_reader(network, bot_id)
        except Exception:  # noqa: BLE001 - reader build is best-effort
            continue
        for ch in ROSTER_CHANNELS:
            try:
                allow = reader(ch)
            except Exception:  # noqa: BLE001 - per-channel read is best-effort
                allow = None
            if allow is None:
                continue  # file missing/unreadable — not "readable empty"
            readable_platforms.add(ch)  # a readable (possibly empty) allowFrom
            for aid in allow:
                if aid:
                    admitted.add((ch, aid))

    return admitted, readable_platforms


def _enrich_usage_names(summary: dict, network: dict, shared_dir: Path) -> dict:
    """Attach cache-only display names to the Usage payload's By User + By
    Channel rows, mutating in place and returning the summary.

    Reads are CACHE-ONLY — ``roster_resolver.resolve_display_name`` reads
    ``network.json`` (``pod.admins.resolved_names`` / ``names`` / bot primary)
    and ``{shared_dir}/identity_cache`` only, never a live Slack/Telegram API
    call at render time (the operator's background-cache decision; the live
    populate path is the Users-page sweep, not this render). The route is the
    right layer for this: the analyzer stays import-clean of admin, while
    ``network`` + ``shared_dir`` + the resolver are all available here.
    """
    from ..roster_resolver import resolve_display_name
    from ..evo.conversation_resolver import cached_conversation
    from usage_analytics import _infer_platform

    # Pod-wide admission index (local-file read; computed once per render, not
    # per row) for the "not admitted" marker. ``readable_platforms`` is the set
    # of platforms whose allowFrom was actually read — only those rows are
    # markable (a platform whose admission couldn't be read is suppressed).
    admitted_ids, readable_platforms = _pod_admitted_index(network)

    # By User — resolve each real person; fall back to a categorized label.
    # ``instance`` (the dominant bot) is the bot_id the resolver keys its
    # bot-primary lookup under; the resolved_names / identity_cache /
    # users-cache steps are pod-level, so a missing instance only skips the
    # bot-primary step.
    for row in summary.get("by_user") or []:
        platform = row.get("platform") or "unknown"
        user_id = row.get("user_id") or "?"
        bot_id = row.get("instance") or ""
        name: "str | None" = None
        source = "allowlist_only"
        if user_id and user_id != "?":
            name, source = resolve_display_name(
                network, bot_id, platform, user_id, shared_dir)
        row["display_name"] = name
        row["name_source"] = source
        row["label"] = name or _fallback_user_label(platform, user_id)
        # Pod-wide "not admitted" marker: this person is admitted to NO bot in
        # the pod. Only set when THIS platform's allowFrom was readable (else
        # suppressed, so a read failure never paints its users unadmitted). A
        # user admitted to even one bot is never marked.
        row["unadmitted"] = bool(
            platform in readable_platforms
            and user_id and user_id != "?"
            and (platform, user_id) not in admitted_ids
        )

    # By Channel — system rows keep their category as the label. Real
    # conversations resolve to a human label via the CACHE-ONLY consumer API
    # (Bite 3, #2990's conversation-name cache). Precedence per row:
    #   1. A Slack user id (U…/W…) that landed in the channel column — a
    #      person's own/self-DM id used as a conversation. Resolve it as a
    #      USER (not a conversation) → "DM · <name>". This is the operator's
    #      "U0PLKKXV0 should show by user name" case.
    #   2. cached_conversation(network, platform, channel) — covers D…→
    #      "DM · peer", C/G/c/g→"#channel"/"Group DM", Telegram group→title.
    #      A negative or absent entry returns no usable label → fall through.
    #   3. Secondary fallback for Slack DMs (D…): the USER cache (Bite 1) may
    #      carry the peer's name even when the conversation cache is cold.
    #   4. Raw id (unchanged from today's fallback).
    # The lookups are cache-only — resolve_display_name reads network.json +
    # the identity cache; cached_conversation reads pod.conversations.resolved.
    # Neither makes a live Slack/Telegram call. The cache is warmed in the
    # background (see _kick_conversation_warm + the resolver's CLI sweep).
    for row in summary.get("by_channel") or []:
        if row.get("system"):
            row.setdefault("platform", "system")
            row.setdefault("label", row.get("channel"))
            continue
        channel = row.get("channel") or ""
        platform = _infer_platform(channel)
        # ``is_dm`` keeps its Bite 1 definition (Slack D-prefixed only) — a
        # U/W self-DM id is not a D-channel, so it stays is_dm=False even
        # though its label renders "DM · …".
        is_dm = platform == "slack" and channel[:1] in ("D", "d")
        label = channel
        if platform == "slack" and channel[:1] in ("U", "u", "W", "w"):
            # 1. Slack user id shown as a channel — resolve as a person.
            name, _src = resolve_display_name(
                network, "", platform, channel, shared_dir)
            if name:
                label = f"DM · {name}"
        else:
            # 2. Cache-only conversation-name lookup.
            entry = cached_conversation(network, platform, channel)
            entry_label = entry.get("label") if entry else None
            if entry_label:
                label = entry_label
            elif is_dm:
                # 3. Secondary fallback: the user cache may name the DM peer
                #    (keyed by the D-channel id) even with a cold conv cache.
                name, _src = resolve_display_name(
                    network, "", platform, channel, shared_dir)
                if name:
                    label = f"DM · {name}"
        row["platform"] = platform
        row["is_dm"] = is_dm
        row["label"] = label
    return summary


# ── Background conversation-cache warm (render stays cache-only) ──────────────
#
# The render path above is CACHE-ONLY. Something must populate
# ``pod.conversations.resolved`` or every By-Channel row is a cache miss → raw
# id. We warm it in the background on a Usage page load: a daemon thread runs
# ``warm_conversation_cache`` (which DOES make live Slack/Telegram calls — never
# on the render path) and persists any new resolutions. The kick is NON-blocking
# — it returns immediately so the page response is never delayed. A non-blocking
# lock + min-interval throttle keep a polled page from piling up warm threads.
#
# This is the render-coupled warm: the data is only consumed by this page, so
# warming on its load is perfectly targeted. The standalone CLI sweep
# (``python3 -m evolve_admin.evo.conversation_resolver``) remains available for a
# one-time / cron warm without a page load.
_CONV_WARM_LOCK = threading.Lock()
# ``last_started`` is a ``time.monotonic()`` stamp, or None for "never kicked".
# None (not 0.0) is the sentinel on purpose: ``time.monotonic()``'s epoch is
# arbitrary and is often < _CONV_WARM_MIN_INTERVAL_S early in a process, so a
# 0.0 sentinel would make ``now - 0.0 < interval`` true and wrongly throttle the
# FIRST kick for the first few minutes after an admin-server restart.
_CONV_WARM_STATE: dict[str, "float | None"] = {"last_started": None}
_CONV_WARM_MIN_INTERVAL_S = 300.0   # don't re-kick more than once per 5 min
_CONV_WARM_MAX_SECONDS = 20.0       # wall-clock budget INSIDE the worker thread


def _kick_conversation_warm(network_path: Path, days: int) -> None:
    """Fire-and-forget, NON-blocking warm of the conversation-name cache.

    Spawns a daemon thread that loads a FRESH ``network`` (so it never races
    the dict the response is serializing), runs ``warm_conversation_cache``
    under a wall-clock budget, and ``save_network``s iff anything resolved.
    Returns to the caller immediately; the render is unaffected.

    Throttled (skip if kicked < ``_CONV_WARM_MIN_INTERVAL_S`` ago) and
    serialized (a non-blocking lock skips the kick when a warm is already in
    flight), so page polling can't spawn overlapping warm threads.
    """
    now = time.monotonic()
    last = _CONV_WARM_STATE["last_started"]
    if last is not None and now - last < _CONV_WARM_MIN_INTERVAL_S:
        return
    if not _CONV_WARM_LOCK.acquire(blocking=False):
        return  # a warm is already running
    _CONV_WARM_STATE["last_started"] = now

    def _run() -> None:
        try:
            from ..evo.conversation_resolver import warm_conversation_cache
            net = load_network(network_path)
            if warm_conversation_cache(
                net, days=days, max_seconds=_CONV_WARM_MAX_SECONDS,
                network_path=str(network_path),
            ):
                save_network(net, network_path)
        except Exception as e:  # noqa: BLE001 — best-effort cache warm
            _log.warning("conversation cache warm kick failed: %s", e)
        finally:
            _CONV_WARM_LOCK.release()

    try:
        threading.Thread(
            target=_run, name="conv-warm-kick", daemon=True,
        ).start()
    except Exception as e:  # noqa: BLE001 — e.g. thread exhaustion
        # ``_run`` never ran, so its finally won't release the lock we hold.
        # Release here so the next kick isn't permanently wedged.
        _CONV_WARM_LOCK.release()
        _log.warning("conversation cache warm kick could not start: %s", e)


# ── Background user-name-cache warm (render stays cache-only) ─────────────────
#
# The user-name twin of the conversation warm above. The Usage By-User render
# resolves names CACHE-ONLY (``resolve_display_name`` → ``pod.users.resolved``
# among its sources); this warm is what populates that cache. A Usage page load
# kicks a daemon thread that runs ``user_resolver.warm_user_cache`` (which DOES
# make live Slack/Telegram/Discord calls — never on the render path) and
# persists any new resolutions. Same fire-and-forget contract as the
# conversation warm: non-blocking, throttled, serialized, with its OWN lock +
# state so the two warms don't contend. The standalone CLI sweep
# (``python3 -m evolve_admin.evo.user_resolver``) remains available for a cron
# warm without a page load.
_USER_WARM_LOCK = threading.Lock()
_USER_WARM_STATE: dict[str, "float | None"] = {"last_started": None}
_USER_WARM_MIN_INTERVAL_S = 300.0   # don't re-kick more than once per 5 min
_USER_WARM_MAX_SECONDS = 20.0       # wall-clock budget INSIDE the worker thread


def _kick_user_warm(network_path: Path, days: int) -> None:
    """Fire-and-forget, NON-blocking warm of the user-name cache.

    Mirrors ``_kick_conversation_warm``: spawns a daemon thread that loads a
    FRESH ``network``, runs ``warm_user_cache`` under a wall-clock budget, and
    ``save_network``s iff anything resolved. Returns immediately; the render is
    unaffected. Throttled (skip if kicked < interval ago) and serialized (a
    non-blocking lock skips overlapping kicks).

    The warm horizon is floored at 30 days (the Users-page lookback) even when
    the page is viewed with a shorter ``days`` window, so the cache covers the
    full set the operator can see on Users — a superset of the By-User report.
    """
    now = time.monotonic()
    last = _USER_WARM_STATE["last_started"]
    if last is not None and now - last < _USER_WARM_MIN_INTERVAL_S:
        return
    if not _USER_WARM_LOCK.acquire(blocking=False):
        return  # a warm is already running
    _USER_WARM_STATE["last_started"] = now

    def _run() -> None:
        try:
            from ..evo.user_resolver import warm_user_cache
            net = load_network(network_path)
            if warm_user_cache(
                net, days=max(days, 30), max_seconds=_USER_WARM_MAX_SECONDS,
            ):
                save_network(net, network_path)
        except Exception as e:  # noqa: BLE001 — best-effort cache warm
            _log.warning("user cache warm kick failed: %s", e)
        finally:
            _USER_WARM_LOCK.release()

    try:
        threading.Thread(
            target=_run, name="user-warm-kick", daemon=True,
        ).start()
    except Exception as e:  # noqa: BLE001 — e.g. thread exhaustion
        # ``_run`` never ran, so its finally won't release the lock we hold.
        _USER_WARM_LOCK.release()
        _log.warning("user cache warm kick could not start: %s", e)


def _read_jsonl(path: Path, user: str | None = None) -> list[dict]:
    """Read a JSONL file, return list of dicts (skip bad lines).

    If ``user`` is given and direct read raises PermissionError, retries via
    ``sudo /bin/cat`` as root (matches /etc/sudoers.d/evolve grant).
    Deliberately does NOT call path.exists() first — that call can itself raise
    PermissionError when the parent directory is not accessible by the server
    user, which would propagate as an unhandled 500.  Instead we rely on the
    exception handlers below.
    """
    text: str | None = None
    try:
        text = path.read_text()
    except PermissionError:
        if user:
            import subprocess as _sp
            r = _sp.run(["sudo", "/bin/cat", str(path)],
                        capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                text = r.stdout
    except OSError:
        return []
    if text is None:
        return []
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            pass
    return records


def _days_ago(days: int):
    from datetime import date, timedelta
    return date.today() - timedelta(days=days)


def _spend_alert_status(shared_dir: Path, network: dict) -> dict:
    """Return spend alert status for the /api/analytics/cost endpoint."""
    import datetime as _dt
    alerts_cfg = network.get("alerts", {})
    threshold = float(
        alerts_cfg.get("spendThresholdUSD")
        or network.get("thresholds", {}).get("dailySpendAlertUsd", 5.0)
    )
    today_str = _dt.date.today().isoformat()

    alerts_dir = shared_dir / "alerts"
    alerts_sent_today = 0
    last_checked: str | None = None
    last_checked_epoch: int | None = None

    if alerts_dir.exists():
        for fp in alerts_dir.glob(f"spend-*-{today_str}.flag"):
            try:
                data = json.loads(fp.read_text())
                alerts_sent_today += 1
                alerted_at = data.get("alerted_at", "")
                if alerted_at and (last_checked is None or alerted_at > last_checked):
                    last_checked = alerted_at
            except Exception:
                alerts_sent_today += 1  # count even if unreadable

    if last_checked:
        try:
            dt = _dt.datetime.fromisoformat(last_checked.replace("Z", "+00:00"))
            last_checked_epoch = int(dt.timestamp())
        except Exception:
            pass

    return {
        "last_checked": last_checked,
        "last_checked_epoch": last_checked_epoch,
        "alerts_sent_today": alerts_sent_today,
        "current_threshold": threshold,
    }


def _attach_perf_block(
    payload: dict,
    summary: dict,
    shared: Path,
    days: int,
    network: dict,
    pricing_cache: dict | None,
    listings_cache: dict | None,
) -> None:
    """Merge the v2 per-model performance block onto an assembled economics payload.

    Reads cascade turn-spans for the window and joins per-model performance
    (latency / struggle / success / error + coverage) onto the cost rows by the
    SAME #2889 identity, so each ``row["perf"]`` lines up with its cost figures.
    Also emits a sibling ``performance`` map (keyed by identity, so a model with
    spans but no cost row is still reachable) and a pod-level
    ``perf_coverage_pct``.

    **Additive + fail-safe.** Cost is over ALL turns; perf is over the
    span-covered SUBSET (~49% coverage) — kept as a distinct block so the UI
    never conflates them. On ANY error (span EACCES, malformed file, a reader
    change) the cost payload is left intact and perf degrades to "no data" —
    the live v1.5 UI must not break because the fragile span store hiccupped.
    Mutates ``payload`` in place; called BEFORE ``filter_economics`` so the
    ``perf`` keys survive its row copies."""
    rows = payload.get("rows") or []
    try:
        from observability.session_rollup import iter_turn_spans
        from model_economics import resolve_bare_to_provider
        from model_performance import assemble_model_performance

        # Resolve bare→provider the SAME way the cost rows did (#2889), so a
        # perf row joins its cost row even when the gateway logged the model
        # both qualified and bare.
        bare_to_provider = resolve_bare_to_provider(
            summary, network=network,
            pricing_cache=pricing_cache, listings_cache=listings_cache,
        )
        # Per-identity total turns = the merged cost rows' turn counts (each row
        # is exactly one identity) — the denominator for per-model coverage_pct.
        total_turns_by_identity = {
            ((r.get("provider") or "").lower(), r.get("model_id")): int(r.get("turns") or 0)
            for r in rows
        }
        now = datetime.now(timezone.utc)
        spans = list(
            iter_turn_spans(shared, since=now - timedelta(days=days), until=now)
        )
        perf = assemble_model_performance(
            spans, total_turns_by_identity,
            total_turns=int(payload.get("total_turns") or 0),
            bare_to_provider=bare_to_provider,
        )
        by_ident = {
            ((e.get("provider") or "").lower(), e.get("model_id")): e
            for e in perf["by_identity"].values()
        }
        for r in rows:
            r["perf"] = by_ident.get(
                ((r.get("provider") or "").lower(), r.get("model_id"))
            )
        payload["performance"] = perf["by_identity"]
        payload["perf_coverage_pct"] = perf["perf_coverage_pct"]
        payload["perf_total_spans"] = perf["total_spans"]
    except Exception as _e:  # noqa: BLE001 — additive perf must never 500 cost
        _log.warning("model-economics perf block failed (cost payload intact): %s", _e)
        for r in rows:
            r.setdefault("perf", None)
        payload.setdefault("performance", {})
        payload.setdefault("perf_coverage_pct", None)
        payload.setdefault("perf_total_spans", 0)


def register_analytics_routes(app: Flask, network_path: Path) -> None:
    """Register all /api/analytics/* and /api/modules routes on the Flask app."""
    from datetime import date, timedelta

    def _shared() -> Path:
        cfg = load_network(network_path)
        return Path(cfg.get("sharedDir", "/Users/Shared/evolve"))

    def _members() -> list[str]:
        cfg = load_network(network_path)
        return cfg.get("members", [])

    def _oc_members() -> list[str]:
        """Members list including the evolve bot (which also runs the plugin)."""
        m = _members()
        return m + (["evolve"] if "evolve" not in m else [])

    # ── Analytics: metrics time series ──────────────────────────
    @app.get("/api/analytics/metrics")
    def api_analytics_metrics():
        bot_id = request.args.get("bot")
        try:
            days = int(request.args.get("days", 30))
        except (ValueError, TypeError):
            days = 30
        shared = _shared()
        bots = [bot_id] if bot_id else _oc_members()
        cutoff = date.today() - timedelta(days=days)
        result = {}
        for bot in bots:
            rows = []
            metrics_dir = shared / "metrics"
            if not metrics_dir.exists():
                result[bot] = []
                continue
            for day_dir in sorted(metrics_dir.iterdir()):
                if not day_dir.is_dir() or day_dir.name < str(cutoff):
                    continue
                mf = day_dir / f"{bot}.json"
                if not mf.exists():
                    continue
                try:
                    m = json.loads(mf.read_text())
                    rows.append({
                        "date": day_dir.name,
                        "session_count": m.get("session_count", 0),
                        "productive_sessions": m.get("productive_sessions", 0),
                        "maintenance_sessions": m.get("maintenance_sessions", 0),
                        "maintenance_ratio": round(m.get("maintenance_ratio", 0), 3),
                        "resolution_rate": round(
                            m.get("first_response_resolutions", 0) / max(m.get("session_count", 0), 1), 3
                        ),
                        "ambiguous_sessions": m.get("ambiguous_sessions", 0),
                        "first_response_resolutions": m.get("first_response_resolutions", 0),
                        "correction_count": m.get("correction_count", 0),
                        "efficiency_flag_count": m.get("efficiency_flag_count", 0),
                        "top_maintenance_signals": m.get("top_maintenance_signals", []),
                        "unexpected_billing_turns": m.get("unexpected_billing_turns", 0),
                        "application_usage": m.get("application_usage", {}),
                        "app_usage": m.get("app_usage", {}),
                    })
                except Exception:
                    pass
            result[bot] = rows
        return jsonify(result)

    @app.post("/api/analytics/measure/run")
    def api_analytics_measure_run():
        """Run measure.py for today (and optionally yesterday) so charts update without waiting for the 1am cron."""
        shared = _shared()
        bots_arg = request.json.get("bots") if request.is_json and request.json else None
        bots = bots_arg if isinstance(bots_arg, list) else _oc_members()
        target_dates = [date.today()]
        results = {}
        try:
            measure_mod = _import_analyzer("measure")
        except Exception as e:
            return jsonify({"error": f"Could not import measure module: {e}"}), 500
        for target_date in target_dates:
            for bot_id in bots:
                key = f"{bot_id}/{target_date}"
                try:
                    metrics = measure_mod.measure_bot(bot_id, str(shared), target_date)
                    measure_mod.write_metrics(metrics, str(shared), bot_id, target_date)
                    results[key] = {"status": metrics.get("status"), "sessions": metrics.get("session_count", 0)}
                except Exception as e:
                    results[key] = {"error": str(e)}
        return jsonify({"ok": True, "results": results})

    # ── Analytics: sessions browser ─────────────────────────────
    @app.get("/api/analytics/sessions")
    def api_analytics_sessions():
        bot_id = request.args.get("bot")
        try:
            days = int(request.args.get("days", 7))
        except (ValueError, TypeError):
            days = 7
        # Legacy filters kept for backward compatibility — the Sessions
        # page no longer surfaces them (Slice 8 of Sessions redesign), but
        # callers passing them still work.
        class_filter = request.args.get("class")  # productive/maintenance/ambiguous
        corrections_only = request.args.get("corrections") == "true"
        efficiency_only = request.args.get("efficiency") == "true"
        # Slice 8 filters — replace the inference-based ones above.
        multi_turn_only = request.args.get("multi_turn") == "true"
        cache_invalidated_only = request.args.get("cache_invalidated") == "true"
        try:
            limit = min(int(request.args.get("limit", 25)), 100)
            offset = int(request.args.get("offset", 0))
        except (ValueError, TypeError):
            limit, offset = 25, 0
        shared = _shared()
        bots = [bot_id] if bot_id else _oc_members()
        cutoff = date.today() - timedelta(days=days)
        sessions = []
        for bot in bots:
            ann_dir = shared / "annotations" / bot
            if not ann_dir.exists():
                continue
            for ann_file in sorted(ann_dir.glob("*.jsonl"), reverse=True):
                file_date = ann_file.stem  # YYYY-MM-DD
                if file_date < str(cutoff):
                    continue
                try:
                    summaries_in_file: list[dict] = []
                    turn_groups: dict[str, list[dict]] = {}  # session_id → turns (fallback)
                    with open(ann_file) as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                            except Exception:
                                continue
                            rtype = rec.get("type")
                            if rtype == "session_summary":
                                summaries_in_file.append(rec)
                            elif rtype == "turn_annotation":
                                sid = rec.get("session_id")
                                if sid:
                                    turn_groups.setdefault(sid, []).append(rec)

                    if summaries_in_file:
                        # Primary path: use session_summary records
                        for rec in summaries_in_file:
                            sc = rec.get("session_class") or rec.get("tier") or ""
                            if class_filter and sc != class_filter:
                                continue
                            if corrections_only and not rec.get("correction_count", 0):
                                continue
                            if efficiency_only and not rec.get("efficiency_flag"):
                                continue
                            sessions.append({
                                "session_id": rec.get("session_id", ""),
                                "date": file_date,
                                "bot_id": bot,
                                "session_class": sc,
                                "tier_confidence": rec.get("tier_confidence"),
                                "turn_count": rec.get("turn_count", 0),
                                "complexity": rec.get("complexity"),
                                "correction_count": rec.get("correction_count", 0),
                                "efficiency_flag": rec.get("efficiency_flag", False),
                                "first_response_resolution": rec.get("first_response_resolution", False),
                                "applications_invoked": rec.get("applications_invoked", []),
                                "promises_made": rec.get("promises_made", []),
                                "outcome": rec.get("outcome", ""),
                                "total_input_tokens": rec.get("total_input_tokens", 0),
                                "total_output_tokens": rec.get("total_output_tokens", 0),
                                "ts": rec.get("ts", ""),
                            })
                    elif turn_groups:
                        # Fallback: build synthetic session records from turn_annotation
                        # groups (handles older plugin versions without session_summary).
                        for sid, turns in turn_groups.items():
                            t_prod  = sum(1 for t in turns if t.get("session_class") == "productive")
                            t_maint = sum(1 for t in turns if t.get("session_class") == "maintenance")
                            if t_prod > t_maint:
                                sc = "productive"
                            elif t_maint > t_prod:
                                sc = "maintenance"
                            else:
                                sc = "ambiguous"
                            if class_filter and sc != class_filter:
                                continue
                            corr = sum(1 for t in turns if t.get("correction_detected"))
                            if corrections_only and not corr:
                                continue
                            if efficiency_only:
                                continue  # efficiency_flag not available in turn_annotations
                            ts_vals = [t.get("ts", "") for t in turns if t.get("ts")]
                            sessions.append({
                                "session_id": sid,
                                "date": file_date,
                                "bot_id": bot,
                                "session_class": sc,
                                "tier_confidence": None,
                                "turn_count": len(turns),
                                "complexity": None,
                                "correction_count": corr,
                                "efficiency_flag": False,
                                "first_response_resolution": len(turns) == 1,
                                "applications_invoked": [],
                                "promises_made": [],
                                "outcome": "",
                                "total_input_tokens": sum(t.get("input_tokens", 0) for t in turns),
                                "total_output_tokens": sum(t.get("output_tokens", 0) for t in turns),
                                "ts": max(ts_vals) if ts_vals else "",
                                "_fallback": True,  # no session_summary found; data from turn_annotations
                            })
                except Exception as e:
                    _log.warning("sessions: error reading %s: %s", ann_file, e)

        # ── Slice 8 enrichment: cache_state breakdown + cost + peak prompt ──
        # Read cost_events once per (bot, window), build a session_id →
        # enrichment map, then join. This is the data the Slice 8 column
        # rework needs (cache chip, cost, context) and the
        # cache_invalidated_only filter operates on.
        try:
            from cost_ledger import read_events as _read_cost_events
        except ImportError:
            _read_cost_events = None
        enrichment: dict[str, dict] = {}
        if _read_cost_events is not None:
            now_dt = datetime.now(timezone.utc)
            for b in bots:
                try:
                    for e in _read_cost_events(b, days=days, shared_dir=shared, now=now_dt):
                        sid = e.get("session_id") or ""
                        if not sid:
                            continue
                        slot = enrichment.setdefault(
                            sid,
                            {
                                "cache_state_counts": {
                                    "warm": 0, "invalidated": 0,
                                    "fresh": 0, "unknown": 0,
                                },
                                "total_cost_usd": 0.0,
                                "peak_prompt_tokens": 0,
                            },
                        )
                        state = e.get("cache_state") or "unknown"
                        if state in slot["cache_state_counts"]:
                            slot["cache_state_counts"][state] += 1
                        else:
                            slot["cache_state_counts"]["unknown"] += 1
                        slot["total_cost_usd"] += float(e.get("cost_usd", 0.0) or 0.0)
                        prompt = (
                            int(e.get("input_tokens", 0) or 0)
                            + int(e.get("cache_read_tokens", 0) or 0)
                            + int(e.get("cache_write_tokens", 0) or 0)
                        )
                        if prompt > slot["peak_prompt_tokens"]:
                            slot["peak_prompt_tokens"] = prompt
                except Exception:
                    continue

        for s in sessions:
            enr = enrichment.get(s["session_id"])
            if enr:
                s["cache_state_counts"] = enr["cache_state_counts"]
                s["total_cost_usd"] = round(enr["total_cost_usd"], 4)
                s["peak_prompt_tokens"] = enr["peak_prompt_tokens"]
            else:
                s["cache_state_counts"] = {
                    "warm": 0, "invalidated": 0, "fresh": 0, "unknown": 0,
                }
                s["total_cost_usd"] = 0.0
                s["peak_prompt_tokens"] = 0

        # ── Slice 8 post-filters (applied AFTER enrichment) ──────────────
        if multi_turn_only:
            sessions = [s for s in sessions if s.get("turn_count", 0) >= 2]
        if cache_invalidated_only:
            sessions = [
                s for s in sessions
                if s.get("cache_state_counts", {}).get("invalidated", 0) > 0
            ]

        # Sort by date + ts descending
        sessions.sort(key=lambda s: (s["date"], s["ts"]), reverse=True)
        total = len(sessions)
        page = sessions[offset: offset + limit]
        return jsonify({"sessions": page, "total": total, "offset": offset, "limit": limit})

    @app.get("/api/analytics/sessions/debug")
    def api_analytics_sessions_debug():
        """Diagnostic endpoint: lists annotation files and their record-type counts."""
        shared = _shared()
        bots = _oc_members()
        cutoff = date.today() - timedelta(days=7)
        result = []
        for bot in bots:
            ann_dir = shared / "annotations" / bot
            if not ann_dir.exists():
                result.append({"bot": bot, "ann_dir": str(ann_dir), "exists": False})
                continue
            files = []
            for ann_file in sorted(ann_dir.glob("*.jsonl"), reverse=True)[:10]:
                file_date = ann_file.stem
                in_range = file_date >= str(cutoff)
                counts: dict[str, int] = {}
                error = None
                try:
                    with open(ann_file) as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                                rtype = rec.get("type", "unknown")
                                counts[rtype] = counts.get(rtype, 0) + 1
                            except Exception:
                                counts["_parse_error"] = counts.get("_parse_error", 0) + 1
                except Exception as e:
                    error = str(e)
                files.append({"file": ann_file.name, "in_range": in_range,
                              "counts": counts, "error": error})
            result.append({"bot": bot, "ann_dir": str(ann_dir), "exists": True, "files": files})
        return jsonify(result)

    # ── Analytics: cost ─────────────────────────────────────────
    @app.get("/api/analytics/cost")
    def api_analytics_cost():
        days = int(request.args.get("days", 30))
        shared = _shared()
        cutoff = date.today() - timedelta(days=days)
        # Daily cost per bot — from pre-aggregated cost/*.json files (written by cost.py)
        daily: dict[str, dict] = {}
        cost_dir = shared / "cost"
        for jf in sorted(cost_dir.glob("*.json")) if cost_dir.exists() else []:
            d = jf.stem[:10]
            if d < str(cutoff): continue
            try:
                data = json.loads(jf.read_text())
                daily[d] = data
            except Exception:
                pass
        # Fallback: if no pre-aggregated files exist, compute daily cost on-the-fly
        # from turn JSONL files using the same source as the Usage tab.
        if not daily:
            try:
                from usage_analytics import load_turns, compute_summary
                turns = load_turns(None, days=days, network_path=str(network_path))
                summary = compute_summary(turns)
                for entry in summary.get("by_date", []):
                    d = entry.get("date", "")
                    if d and d >= str(cutoff):
                        daily[d] = {"total_cost": entry.get("total_cost", 0)}
            except Exception:
                pass
        # Tier usage totals
        tier_totals: dict[str, dict[str, int]] = {}
        tier_dir = cost_dir / "tier-usage"
        for bot_dir in (tier_dir.iterdir() if tier_dir.exists() else []):
            if not bot_dir.is_dir(): continue
            bot = bot_dir.name
            tier_totals[bot] = {"tier0": 0, "tier1": 0, "tier2": 0, "tier3": 0}
            for jf in bot_dir.glob("*.jsonl"):
                if jf.stem < str(cutoff): continue
                for rec in _read_jsonl(jf):
                    t = rec.get("tier", "")
                    if t in tier_totals[bot]:
                        tier_totals[bot][t] += 1
        # Spend alert status
        spend_alerts = _spend_alert_status(shared, load_network(network_path))
        return jsonify({
            "daily": daily,
            "tier_usage": tier_totals,
            "spend_alerts": spend_alerts,
            "billing_mode": "api_key",  # MAX subscription ended April 4, 2026
        })

    # ── Analytics: cache economics (Sessions redesign Slice 3) ────────
    @app.get("/api/analytics/cache-economics")
    def api_analytics_cache_economics():
        """Decision-support data for the prompt-cache TTL + structure knobs.

        Returns four aggregations over cost_event JSONL for one bot (or all
        bots when ``bot`` is omitted/empty) across the trailing window:

          - cache_health_by_day: stacked counts of {warm, invalidated,
            fresh, unknown} events per day. Powers the cache-health area
            chart. Directly readable against session_economics thresholds.
          - token_totals: cache_read / cache_write / fresh_input / output
            sums across the window. The honest unit — operator weights to
            dollars using their model's published prices.
          - total_cost_usd: sum of cost_usd across the window (already
            computed per-event upstream, so this is just an addition).
          - cost_per_session: histogram + top-N expensive sessions. The
            "go read these" surface that aggregate stats can't deliver.
          - context_trajectory_by_day: mean (input + cache_read +
            cache_write) tokens per event per day. Catches conversation
            bloat that the cache-state signals don't see directly.

        Headline summary (invalidated_ratio, realized_hit_rate) is computed
        over the same cache-participating set the session_economics monitor
        uses, so the operator can sanity-check the Signal that surfaces in
        the Alerts page from the same data shown here.
        """
        from cost_ledger import read_events

        bot_id = request.args.get("bot") or ""
        try:
            days = int(request.args.get("days", 30))
        except (ValueError, TypeError):
            days = 30
        shared = _shared()
        now_dt = datetime.now(timezone.utc)
        bots = [bot_id] if bot_id else _oc_members()

        # ── Pull events ───────────────────────────────────────────────────
        all_events: list[dict] = []
        for b in bots:
            try:
                all_events.extend(
                    list(read_events(b, days=days, shared_dir=shared, now=now_dt))
                )
            except Exception:
                continue

        # ── 1) Cache health by day ────────────────────────────────────────
        # {date: {warm: int, invalidated: int, fresh: int, unknown: int}}
        cache_by_day: dict[str, dict[str, int]] = {}
        for e in all_events:
            ts = e.get("ts") or ""
            d = ts[:10] if isinstance(ts, str) and len(ts) >= 10 else ""
            if not d:
                continue
            state = e.get("cache_state") or "unknown"
            slot = cache_by_day.setdefault(
                d, {"warm": 0, "invalidated": 0, "fresh": 0, "unknown": 0}
            )
            if state in slot:
                slot[state] += 1
            else:
                slot["unknown"] += 1
        cache_health_by_day = [
            {"date": d, **counts}
            for d, counts in sorted(cache_by_day.items())
        ]

        # ── 2) Token totals + 3) total cost ───────────────────────────────
        cache_read_tokens = 0
        cache_write_tokens = 0
        fresh_input_tokens = 0  # input_tokens summed across all events
        output_tokens = 0
        total_cost_usd = 0.0
        for e in all_events:
            cache_read_tokens += int(e.get("cache_read_tokens", 0) or 0)
            cache_write_tokens += int(e.get("cache_write_tokens", 0) or 0)
            fresh_input_tokens += int(e.get("input_tokens", 0) or 0)
            output_tokens += int(e.get("output_tokens", 0) or 0)
            total_cost_usd += float(e.get("cost_usd", 0.0) or 0.0)

        # ── 4) Cost-per-session: histogram + top-N + percentiles ──────────
        # Inline aggregation rather than cost_ledger.rollup_per_session
        # because we want bot_id on every row (cross-bot view).
        sess_acc: dict[str, dict] = {}
        for e in all_events:
            sid = e.get("session_id") or ""
            if not sid:
                continue
            slot = sess_acc.setdefault(
                sid,
                {
                    "session_id": sid,
                    "bot_id": e.get("bot_id") or "",
                    "cost_usd": 0.0,
                    "event_count": 0,
                    "trigger_kinds": set(),
                    "first_ts": "",
                    "last_ts": "",
                },
            )
            slot["cost_usd"] += float(e.get("cost_usd", 0.0) or 0.0)
            slot["event_count"] += 1
            kind = e.get("trigger_kind") or "unknown"
            slot["trigger_kinds"].add(kind)
            ts = e.get("ts") or ""
            if isinstance(ts, str) and ts:
                if not slot["first_ts"] or ts < slot["first_ts"]:
                    slot["first_ts"] = ts
                if not slot["last_ts"] or ts > slot["last_ts"]:
                    slot["last_ts"] = ts
        session_rows: list[dict] = [
            {
                "session_id": slot["session_id"],
                "bot_id": slot["bot_id"],
                "cost_usd": round(slot["cost_usd"], 4),
                "event_count": slot["event_count"],
                "trigger_kinds": sorted(slot["trigger_kinds"]),
                "first_ts": slot["first_ts"],
                "last_ts": slot["last_ts"],
            }
            for slot in sess_acc.values()
        ]
        session_count = len(session_rows)
        costs_sorted = sorted(r["cost_usd"] for r in session_rows)
        median_usd = costs_sorted[session_count // 2] if session_count else 0.0
        p95_usd = (
            costs_sorted[int(session_count * 0.95)]
            if session_count
            else 0.0
        )

        # Histogram bins. Log-ish bins chosen because session-cost
        # distributions are typically heavy-tailed.
        _BINS: list[tuple[str, float, float]] = [
            ("<$0.01",       0.0,     0.01),
            ("$0.01-$0.05",  0.01,    0.05),
            ("$0.05-$0.25",  0.05,    0.25),
            ("$0.25-$1",     0.25,    1.0),
            ("$1-$5",        1.0,     5.0),
            (">$5",          5.0,     float("inf")),
        ]
        bin_counts = [0] * len(_BINS)
        for c in costs_sorted:
            for i, (_, lo, hi) in enumerate(_BINS):
                if lo <= c < hi:
                    bin_counts[i] += 1
                    break
        histogram = [
            {
                "label": label,
                "lower_usd": lo if lo != float("inf") else None,
                "upper_usd": hi if hi != float("inf") else None,
                "count": bin_counts[i],
            }
            for i, (label, lo, hi) in enumerate(_BINS)
        ]
        top_n = sorted(session_rows, key=lambda r: -r["cost_usd"])[:10]

        # ── 5) Context trajectory ──────────────────────────────────────────
        # Avg (input + cache_read + cache_write) tokens per event per day.
        ctx_by_day: dict[str, dict[str, int]] = {}
        for e in all_events:
            ts = e.get("ts") or ""
            d = ts[:10] if isinstance(ts, str) and len(ts) >= 10 else ""
            if not d:
                continue
            prompt_tokens = (
                int(e.get("input_tokens", 0) or 0)
                + int(e.get("cache_read_tokens", 0) or 0)
                + int(e.get("cache_write_tokens", 0) or 0)
            )
            slot = ctx_by_day.setdefault(d, {"prompt_tokens": 0, "event_count": 0})
            slot["prompt_tokens"] += prompt_tokens
            slot["event_count"] += 1
        context_trajectory_by_day = [
            {
                "date": d,
                "avg_prompt_tokens": round(
                    slot["prompt_tokens"] / max(slot["event_count"], 1)
                ),
                "event_count": slot["event_count"],
            }
            for d, slot in sorted(ctx_by_day.items())
        ]

        # ── 6) Headline summary ───────────────────────────────────────────
        # Computed over the cache-participating set (warm + invalidated),
        # matching session_economics so the alert and the page agree.
        participating = sum(
            1
            for e in all_events
            if (e.get("cache_state") or "") in ("warm", "invalidated")
        )
        invalidated = sum(
            1
            for e in all_events
            if (e.get("cache_state") or "") == "invalidated"
        )
        invalidated_ratio = (
            invalidated / participating if participating else 0.0
        )
        # Hit rate denominator: cache_read + cache_write + input over
        # participating events only.
        hr_read = 0
        hr_write = 0
        hr_input = 0
        for e in all_events:
            if (e.get("cache_state") or "") not in ("warm", "invalidated"):
                continue
            hr_read += int(e.get("cache_read_tokens", 0) or 0)
            hr_write += int(e.get("cache_write_tokens", 0) or 0)
            hr_input += int(e.get("input_tokens", 0) or 0)
        hr_total = hr_read + hr_write + hr_input
        realized_hit_rate = hr_read / hr_total if hr_total else 0.0

        return jsonify({
            "bot_id": bot_id,
            "window_days": days,
            "cache_health_by_day": cache_health_by_day,
            "token_totals": {
                "cache_read": cache_read_tokens,
                "cache_write": cache_write_tokens,
                "fresh_input": fresh_input_tokens,
                "output": output_tokens,
            },
            "total_cost_usd": round(total_cost_usd, 4),
            "cost_per_session": {
                "histogram": histogram,
                "top_n": top_n,
                "session_count": session_count,
                "median_usd": round(median_usd, 4),
                "p95_usd": round(p95_usd, 4),
            },
            "context_trajectory_by_day": context_trajectory_by_day,
            "summary": {
                "invalidated_ratio": round(invalidated_ratio, 4),
                "realized_hit_rate": round(realized_hit_rate, 4),
                "participating_events": participating,
                "total_events": len(all_events),
            },
        })

    # ── Analytics: activity rhythm (Sessions redesign Slice 6) ────────
    @app.get("/api/analytics/activity-rhythm")
    def api_analytics_activity_rhythm():
        """When does the pod (or one bot) actually live?

        Three rhythm-of-use aggregations over cost_event JSONL:

          - time_of_day_heatmap: 24×7 grid of event counts. Each cell is
            (hour-of-day, weekday) in UTC. Useful to see whether a bot
            has a coherent daily rhythm, when traffic peaks, and whether
            it has gone unusually quiet.
          - daily_session_counts: distinct session_ids per day, sorted.
            A session is "active on a day" if any of its cost_events
            land on that date. Useful for spotting growth, abandonment,
            and bursty days.
          - inter_turn_gap_histogram: distribution of seconds between
            consecutive user_turn cost_events within the same session.
            Direct input to the TTL question — TTL must outlive the
            gap for cacheRead savings to materialize. Median and p95
            included in the response for quick cross-checks against
            session_economics.cache_invalidation_elevated.

        Inter-turn gaps are computed over user_turn events only because
        heartbeats and subagents don't represent user-perceptible pauses
        between conversational turns.

        UTC is used throughout — the heatmap shows the pod's actual
        firing rhythm relative to a stable reference. Future work could
        add timezone selection to align with operator/user local time.
        """
        from cost_ledger import read_events

        bot_id = request.args.get("bot") or ""
        try:
            days = int(request.args.get("days", 30))
        except (ValueError, TypeError):
            days = 30
        shared = _shared()
        now_dt = datetime.now(timezone.utc)
        bots = [bot_id] if bot_id else _oc_members()

        # ── Pull events ───────────────────────────────────────────────────
        all_events: list[dict] = []
        for b in bots:
            try:
                all_events.extend(
                    list(read_events(b, days=days, shared_dir=shared, now=now_dt))
                )
            except Exception:
                continue

        # ── 1) Time-of-day heatmap ────────────────────────────────────────
        # matrix[hour][weekday] where weekday 0=Monday ... 6=Sunday
        # (Python's datetime.weekday() convention; the frontend renders
        # with Monday-first column labels for the same reason).
        matrix = [[0] * 7 for _ in range(24)]
        max_count = 0
        for e in all_events:
            ts = e.get("ts")
            if not isinstance(ts, str):
                continue
            try:
                # fromisoformat handles "...Z" only on 3.11+; trailing "Z"
                # is the most common shape but we defensively replace it.
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            h = dt.hour
            d = dt.weekday()
            matrix[h][d] += 1
            if matrix[h][d] > max_count:
                max_count = matrix[h][d]

        # ── 2) Daily session counts ───────────────────────────────────────
        # date -> set of session_ids
        sessions_by_day: dict[str, set[str]] = {}
        for e in all_events:
            ts = e.get("ts") or ""
            d = ts[:10] if isinstance(ts, str) and len(ts) >= 10 else ""
            sid = e.get("session_id") or ""
            if not d or not sid:
                continue
            sessions_by_day.setdefault(d, set()).add(sid)
        daily_session_counts = [
            {"date": d, "session_count": len(sids)}
            for d, sids in sorted(sessions_by_day.items())
        ]

        # ── 3) Inter-turn gap histogram ───────────────────────────────────
        # Filter to user_turn events only, group by session, sort by ts,
        # compute consecutive deltas in seconds.
        by_session_user_turns: dict[str, list[datetime]] = {}
        for e in all_events:
            if (e.get("trigger_kind") or "") != "user_turn":
                continue
            sid = e.get("session_id") or ""
            ts = e.get("ts")
            if not sid or not isinstance(ts, str):
                continue
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            by_session_user_turns.setdefault(sid, []).append(dt)

        gaps_seconds: list[float] = []
        for sid, dts in by_session_user_turns.items():
            if len(dts) < 2:
                continue
            dts.sort()
            for i in range(1, len(dts)):
                delta = (dts[i] - dts[i - 1]).total_seconds()
                if delta < 0:
                    continue  # skip clock-skew artifacts
                gaps_seconds.append(delta)

        _GAP_BINS: list[tuple[str, float, float]] = [
            ("< 30s",     0.0,     30.0),
            ("30s-2m",    30.0,    120.0),
            ("2-5m",      120.0,   300.0),
            ("5-15m",     300.0,   900.0),
            ("15m-1h",    900.0,   3600.0),
            ("1-4h",      3600.0,  14400.0),
            ("4-24h",     14400.0, 86400.0),
            ("> 24h",     86400.0, float("inf")),
        ]
        gap_bin_counts = [0] * len(_GAP_BINS)
        for g in gaps_seconds:
            for i, (_, lo, hi) in enumerate(_GAP_BINS):
                if lo <= g < hi:
                    gap_bin_counts[i] += 1
                    break
        gap_histogram = [
            {
                "label": label,
                "lower_seconds": lo,
                "upper_seconds": hi if hi != float("inf") else None,
                "count": gap_bin_counts[i],
            }
            for i, (label, lo, hi) in enumerate(_GAP_BINS)
        ]
        # Median + p95 directly from the gap array (not the histogram —
        # histogram bins lose precision around the boundaries).
        gaps_sorted = sorted(gaps_seconds)
        n_gaps = len(gaps_sorted)
        median_seconds = (
            gaps_sorted[n_gaps // 2] if n_gaps else 0.0
        )
        p95_seconds = (
            gaps_sorted[int(n_gaps * 0.95)] if n_gaps else 0.0
        )

        return jsonify({
            "bot_id": bot_id,
            "window_days": days,
            "time_of_day_heatmap": {
                "matrix": matrix,
                "max_count": max_count,
                "total_events": len(all_events),
            },
            "daily_session_counts": daily_session_counts,
            "inter_turn_gap_histogram": {
                "bins": gap_histogram,
                "median_seconds": median_seconds,
                "p95_seconds": p95_seconds,
                "gap_count": len(gaps_seconds),
            },
        })

    # ── Analytics: curated sessions (Sessions redesign Slice 7) ─────────
    @app.get("/api/analytics/curated-sessions")
    def api_analytics_curated_sessions():
        """The "go read these" surface — a handful of sessions worth the operator's eyes.

        The Sessions page produces thousands of sessions per window;
        aggregate metrics alone don't tell anyone which to investigate.
        This endpoint applies ground-truth curation rules (no inference,
        no NLP) and surfaces a handful per category. Categories:

          most_expensive    — top cost_usd per session
          longest_by_turns  — top event_count per session
          biggest_context   — top peak prompt tokens within a session
          most_invalidated  — top invalidation ratio (with a min-events
                              floor so trivially-small sessions don't
                              pollute the list)

        Each session can appear under multiple categories — that's
        informative, not a bug; the same session showing up as "most
        expensive" AND "most invalidated" tells a coherent story.

        Categories from the spec that need data we don't yet have are
        deliberately deferred:
          - new-user sessions: no user_id on cost_events
          - new-tool invocations: real tool tagging is Phase 2
            instrumentation work
          - errored sessions: no error field on cost_events
          - long-user-message sessions: no message content
          - returning-user-after-gap: no user_id on cost_events
        """
        from cost_ledger import read_events

        bot_id = request.args.get("bot") or ""
        try:
            days = int(request.args.get("days", 30))
        except (ValueError, TypeError):
            days = 30
        shared = _shared()
        now_dt = datetime.now(timezone.utc)
        bots = [bot_id] if bot_id else _oc_members()

        # ── Aggregate per-session ─────────────────────────────────────────
        # Inline rollup so we have all four metrics in a single pass.
        # peak_prompt_tokens is per-event max within the session — captures
        # the moment of greatest context bloat, which is what the operator
        # actually wants to inspect.
        sess: dict[str, dict] = {}
        for b in bots:
            try:
                events = list(read_events(b, days=days, shared_dir=shared, now=now_dt))
            except Exception:
                continue
            for e in events:
                sid = e.get("session_id") or ""
                if not sid:
                    continue
                slot = sess.setdefault(
                    sid,
                    {
                        "session_id": sid,
                        "bot_id": e.get("bot_id") or "",
                        "cost_usd": 0.0,
                        "event_count": 0,
                        "peak_prompt_tokens": 0,
                        "cache_participating_count": 0,
                        "invalidated_count": 0,
                        "trigger_kinds": set(),
                        "first_ts": "",
                        "last_ts": "",
                    },
                )
                slot["cost_usd"] += float(e.get("cost_usd", 0.0) or 0.0)
                slot["event_count"] += 1
                prompt_toks = (
                    int(e.get("input_tokens", 0) or 0)
                    + int(e.get("cache_read_tokens", 0) or 0)
                    + int(e.get("cache_write_tokens", 0) or 0)
                )
                if prompt_toks > slot["peak_prompt_tokens"]:
                    slot["peak_prompt_tokens"] = prompt_toks
                state = e.get("cache_state") or ""
                if state in ("warm", "invalidated"):
                    slot["cache_participating_count"] += 1
                    if state == "invalidated":
                        slot["invalidated_count"] += 1
                kind = e.get("trigger_kind") or "unknown"
                slot["trigger_kinds"].add(kind)
                ts = e.get("ts") or ""
                if isinstance(ts, str) and ts:
                    if not slot["first_ts"] or ts < slot["first_ts"]:
                        slot["first_ts"] = ts
                    if not slot["last_ts"] or ts > slot["last_ts"]:
                        slot["last_ts"] = ts

        rows = list(sess.values())
        # Freeze trigger_kinds set → sorted list for JSON-friendliness
        for r in rows:
            r["trigger_kinds"] = sorted(r["trigger_kinds"])
            r["cost_usd"] = round(r["cost_usd"], 4)

        # ── Per-category curation ─────────────────────────────────────────
        # Min-events floor for invalidation: a 1-event session with 1
        # invalidation would be 100% but informationally useless. ≥5
        # cache-participating events ensures the ratio is meaningful.
        _MIN_PARTICIPATING_FOR_INVAL = 5
        _PER_CATEGORY_LIMIT = 2  # 4 categories × 2 each = up to 8 entries

        def _make_entry(category: str, label: str, row: dict, *, metric_value, metric_label, rationale: str) -> dict:
            return {
                "category": category,
                "label": label,
                "session_id": row["session_id"],
                "bot_id": row["bot_id"],
                "cost_usd": row["cost_usd"],
                "event_count": row["event_count"],
                "peak_prompt_tokens": row["peak_prompt_tokens"],
                "trigger_kinds": row["trigger_kinds"],
                "first_ts": row["first_ts"],
                "last_ts": row["last_ts"],
                "metric_value": metric_value,
                "metric_label": metric_label,
                "rationale": rationale,
            }

        curated: list[dict] = []

        # 1) Most expensive
        for r in sorted(rows, key=lambda r: -r["cost_usd"])[:_PER_CATEGORY_LIMIT]:
            if r["cost_usd"] <= 0:
                break
            curated.append(_make_entry(
                "most_expensive",
                "Most expensive",
                r,
                metric_value=r["cost_usd"],
                metric_label=f"${r['cost_usd']:.2f}",
                rationale="Top cost in window",
            ))

        # 2) Longest by turns (event_count)
        for r in sorted(rows, key=lambda r: -r["event_count"])[:_PER_CATEGORY_LIMIT]:
            if r["event_count"] <= 1:
                break  # 1-event sessions aren't "long"
            curated.append(_make_entry(
                "longest_by_turns",
                "Longest by turns",
                r,
                metric_value=r["event_count"],
                metric_label=f"{r['event_count']} events",
                rationale="Highest event count in window",
            ))

        # 3) Biggest context
        for r in sorted(rows, key=lambda r: -r["peak_prompt_tokens"])[:_PER_CATEGORY_LIMIT]:
            if r["peak_prompt_tokens"] <= 0:
                break
            curated.append(_make_entry(
                "biggest_context",
                "Biggest context",
                r,
                metric_value=r["peak_prompt_tokens"],
                metric_label=f"{r['peak_prompt_tokens']:,} tokens peak",
                rationale="Largest single-turn prompt in window",
            ))

        # 4) Most cache-inefficient (with min-events floor)
        eligible = [
            r for r in rows
            if r["cache_participating_count"] >= _MIN_PARTICIPATING_FOR_INVAL
        ]
        def _inval_ratio(r):
            p = r["cache_participating_count"]
            return r["invalidated_count"] / p if p else 0.0
        for r in sorted(eligible, key=lambda r: -_inval_ratio(r))[:_PER_CATEGORY_LIMIT]:
            ratio = _inval_ratio(r)
            if ratio <= 0:
                break
            curated.append(_make_entry(
                "most_invalidated",
                "Highest cache invalidation",
                r,
                metric_value=round(ratio, 4),
                metric_label=f"{ratio*100:.0f}% invalidated",
                rationale=(
                    f"{r['invalidated_count']}/{r['cache_participating_count']} "
                    f"cached turns re-paid cacheWrite"
                ),
            ))

        return jsonify({
            "bot_id": bot_id,
            "window_days": days,
            "session_count": len(rows),
            "curated": curated,
        })

    # ── Analytics: cache-economics recommendation (Sessions redesign Slice 9) ─
    #
    # Read the bot's two cache-adjacent knobs + the observed gap distribution
    # and invalidation rate, and emit one concrete recommendation. This is the
    # synthesis step the operator was missing: thousands of numbers on the
    # page, no single answer to "what should I change?"
    #
    # THE TWO KNOBS ARE NOT INTERCHANGEABLE — the v1 implementation of this
    # endpoint conflated them, diagnosing cache invalidation correctly and then
    # prescribing `contextPruning.ttl`, which does not control cache lifetime
    # at all. Ground truth from the installed OpenClaw:
    #
    #   cache_retention  (→ agents.defaults.models.<id>.params.cacheRetention)
    #       THE cache-lifetime knob. resolveAnthropicEphemeralCacheControl in
    #       provider-stream-shared-*.js maps "long" → cache_control.ttl = "1h"
    #       and anything else → {type:"ephemeral"}, i.e. Anthropic's 5-minute
    #       default. Those are the only two lifetimes Anthropic offers. Raising
    #       this is the ONLY way to stop turns arriving after cache expiry.
    #
    #   contextPruning.ttl  (→ agents.defaults.contextPruning.ttl)
    #       Gates WHEN PRUNING MAY RUN, nothing else. contextPruningExtension
    #       in attempt.model-diagnostic-events-*.js:
    #           if (ttlMs > 0 && Date.now() - lastTouch < ttlMs) return;
    #       i.e. "don't prune while the cache is still warm" — pruning rewrites
    #       the prefix and would throw away a live cache. Changing this cannot
    #       move the invalidation rate by a single point.
    #
    # So: invalidation is a cache_retention question, and contextPruning.ttl is
    # only ever a coherence question against the cache TTL already configured.
    # Same misconception produced the contextPruning.ttl "4h" default fixed in
    # PR #3497; see cost_profiles.MAX_COHERENT_PRUNE_TTL_SECONDS.

    # Effective prompt-cache lifetime per cache_retention value. Anthropic
    # offers exactly two. ``None`` = unset, which inherits OC's default.
    _CACHE_TTL_SECONDS: dict[str | None, int] = {"long": 3600, "short": 300, None: 300}
    _CACHE_RETENTION_LABEL: dict[str | None, str] = {
        "long": "long (1h)", "short": "short (5m)", None: "unset (5m default)",
    }

    # Price multipliers on the model's base input rate. Anthropic charges a
    # premium to WRITE a cache entry, scaled by how long it must live, and a
    # flat discount to READ one:
    #     5-minute write = 1.25x base    1-hour write = 2.00x base
    #     cache read     = 0.10x base    (same for both lifetimes)
    _CACHE_WRITE_MULT: dict[str, float] = {"short": 1.25, "long": 2.00}
    _CACHE_READ_MULT = 0.10

    # Break-even for `cache_retention: "long"`. With T total turns over the
    # window, W_s writes under a 5-minute cache and W_l writes under a 1-hour
    # cache (every non-write turn is a read):
    #     short cost = W_s * 1.25 + (T - W_s) * 0.10
    #     long  cost = W_l * 2.00 + (T - W_l) * 0.10
    # long is cheaper  ⟺  W_s / W_l > (2.00 - 0.10) / (1.25 - 0.10) = 1.652…
    #
    # In words: the 1-hour cache only pays for itself when the prefix would
    # otherwise be re-written more than ~1.6 times per hour-long window. A bot
    # whose turns are HOURS apart — a 2h heartbeat, say — breaks a 1-hour cache
    # just as reliably as a 5-minute one, so W_s == W_l, the ratio is 1.0, and
    # "long" buys nothing while charging 2.00x instead of 1.25x on every single
    # write. Never recommend "long" for those bots.
    _LONG_RETENTION_BREAKEVEN = (
        (_CACHE_WRITE_MULT["long"] - _CACHE_READ_MULT)
        / (_CACHE_WRITE_MULT["short"] - _CACHE_READ_MULT)
    )  # ≈ 1.652

    # Invalidation rate above which the cache is worth acting on at all.
    _INVALIDATION_ELEVATED = 0.15
    # Minimum inter-request gaps before the break-even arithmetic is
    # trustworthy. Below this we can see invalidation but cannot size the
    # economics, and recommending "long" blind is how a heartbeat bot ends up
    # paying the 2.00x premium for nothing.
    _MIN_GAPS_FOR_ECONOMICS = 10

    def _parse_ttl_seconds(s: str | None) -> int | None:
        """Parse a TTL string like '4h', '30m', '90s' into seconds.

        Returns None on anything unparseable — caller distinguishes
        "couldn't read config" from "config was 0" via the None check.
        """
        if not isinstance(s, str):
            return None
        s = s.strip().lower()
        if not s:
            return None
        try:
            if s.endswith("ms"):
                return int(float(s[:-2]) / 1000)
            if s.endswith("s"):
                return int(float(s[:-1]))
            if s.endswith("m"):
                return int(float(s[:-1]) * 60)
            if s.endswith("h"):
                return int(float(s[:-1]) * 3600)
            if s.endswith("d"):
                return int(float(s[:-1]) * 86400)
            # Bare number → assume seconds
            return int(float(s))
        except (ValueError, TypeError):
            return None

    def _format_ttl(seconds: int) -> str:
        """Inverse of _parse_ttl_seconds — an OC-parseable duration literal.

        The value round-trips into ``contextPruning.ttl``, so it must be a
        string OC accepts, not a prose duration. Use ``_fmt_dur`` for anything
        that only has to read well.
        """
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        if seconds < 86400:
            return f"{seconds // 3600}h"
        return f"{seconds // 86400}d"

    def _fmt_dur(seconds: float) -> str:
        """Human-readable duration. Used in reasoning strings.

        Distinct from _format_ttl: accepts arbitrary durations (gap
        medians, p95s) without claiming to be a standard tier label.
        """
        s = int(round(seconds))
        if s < 60:
            return f"{s}s"
        if s < 3600:
            m = s // 60
            r = s - m * 60
            return f"{m}m" if r == 0 else f"{m}m {r}s"
        if s < 86400:
            h = s // 3600
            m = (s - h * 3600) // 60
            return f"{h}h" if m == 0 else f"{h}h {m}m"
        d = s // 86400
        h = (s - d * 86400) // 3600
        return f"{d}d" if h == 0 else f"{d}d {h}h"

    def _read_cache_config(bot_id: str) -> dict | None:
        """Read BOTH cache-adjacent knobs from the bot's openclaw.json.

        openclaw.json is the DEPLOYED truth — what the bot is actually running
        while the cost events we're about to analyze were produced. The write
        surface for cache_retention is elsewhere (better-engine-config.json
        ``bots.<bot>.budget.per_bot_cache_retention``, fanned out to every
        Anthropic model's ``params.cacheRetention`` at deploy), but reading
        intent instead of deployed state would let a not-yet-deployed override
        silently explain away invalidation the bot is still paying for.

        Admin runs as the evolve user; the per-bot .openclaw/ directory has
        read ACL via deploy.set_evolve_read_acl, so a direct read works
        without sudo.

        Returns None only when openclaw.json itself is unreadable — the caller
        treats that as "couldn't determine, hold". Otherwise a dict:

          prune_ttl        contextPruning.ttl as configured, or None
          prune_active     True iff contextPruning.mode == "cache-ttl", i.e.
                           the ttl is live rather than inert. Mirrors
                           test_cost_profiles_prune_cache_coherence._prune_ttl.
          cache_retention  "short" / "long" as deployed, or None if unset
        """
        try:
            net = load_network(network_path)
            path = _bot_home(bot_id, net) / ".openclaw" / "openclaw.json"
            data = json.loads(path.read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        agents = data.get("agents") or {}
        defaults = agents.get("defaults") or {}
        cp = defaults.get("contextPruning") or {}
        ttl = cp.get("ttl")

        # cacheRetention is fanned out per-model; the materializer writes the
        # same value to every Anthropic model, so the first one we find is
        # representative. A non-Anthropic model never carries the field.
        retention: str | None = None
        models = defaults.get("models")
        if isinstance(models, dict):
            for model_cfg in models.values():
                if not isinstance(model_cfg, dict):
                    continue
                params = model_cfg.get("params")
                if not isinstance(params, dict):
                    continue
                value = params.get("cacheRetention")
                if value in ("short", "long"):
                    retention = value
                    break

        return {
            "prune_ttl": ttl if isinstance(ttl, str) and ttl else None,
            "prune_active": cp.get("mode") == "cache-ttl",
            "cache_retention": retention,
        }

    # Anthropic Sonnet base input rate. Sonnet is the modal model on a typical
    # pod; using it as the baseline accepts ~30% error for heavy-Haiku bots but
    # stays in the right order of magnitude. Rendered to the operator as
    # "~$X/mo" — they read it as a magnitude, not a quote. The 1.25x / 2.00x /
    # 0.10x multipliers above land this on $3.75 / $6.00 / $0.30 per MTok.
    _USD_PER_TOKEN_BASE = 3.00 / 1_000_000

    def _estimate_retention_monthly_savings(
        *,
        cache_write_tokens_total: int,
        rewrite_factor: float,
        window_days: int,
    ) -> float:
        """USD/month delta from switching a bot from "short" to "long" retention.

        ``rewrite_factor`` is W_s/W_l — how many times the prefix is re-written
        under the 5-minute cache for each write a 1-hour cache would still have
        to pay. Holding the average prefix size constant, the observed cacheWrite
        volume W redistributes as: a fraction 1/rewrite_factor stays a write (at
        the dearer 2.00x rate), and the rest converts to reads at 0.10x. So, in
        units of the base input price:

            short = W * 1.25
            long  = W * (1/f) * 2.00  +  W * (1 - 1/f) * 0.10

        and the saving is W * [1.15 - 1.90/f], which is positive exactly when
        f > 1.652 — the same break-even the recommendation gates on. A negative
        return is meaningful and is NOT clamped: it is what a "long" bot with
        hours-apart turns is losing, and it is the number the ``lower`` verdict
        reports back.
        """
        if cache_write_tokens_total <= 0 or window_days <= 0 or rewrite_factor <= 0:
            return 0.0
        short_mult = _CACHE_WRITE_MULT["short"]
        long_mult = (
            _CACHE_WRITE_MULT["long"] / rewrite_factor
            + _CACHE_READ_MULT * (1.0 - 1.0 / rewrite_factor)
        )
        window_savings = (
            cache_write_tokens_total * (short_mult - long_mult) * _USD_PER_TOKEN_BASE
        )
        return window_savings * (30.0 / window_days)

    def _max_coherent_prune_ttl() -> int:
        """Anthropic's longest cache TTL, from the module that owns the bound.

        cost_profiles.MAX_COHERENT_PRUNE_TTL_SECONDS is the single source of
        truth (PR #3497, enforced over every built-in profile and deploy.py's
        constant by test_cost_profiles_prune_cache_coherence.py). Imported
        rather than re-declared so the two can't drift; the literal fallback
        only covers an analyzer-import failure at request time.
        """
        try:
            return int(_import_analyzer("cost_profiles").MAX_COHERENT_PRUNE_TTL_SECONDS)
        except Exception:
            return _CACHE_TTL_SECONDS["long"]

    def _cache_verdict_for_bot(
        bot_id: str,
        events: list[dict],
        *,
        window_days: int,
        cache_config: dict | None,
        shared_dir: Path | None = None,
    ) -> dict:
        """Apply the recommendation decision tree to one bot's data.

        Decision order (first match wins):

          1. Not enough data → 'not_enough_data', no target.
          2. openclaw.json unreadable → 'hold', low confidence.
          3. Invalidation elevated, cache on the 5-minute default, and the
             break-even arithmetic favours a 1-hour cache
             → raise ``cache_retention`` to "long". THE cache-lifetime knob.
          4. Cache already on "long" but the break-even says it can't pay for
             itself → lower ``cache_retention`` to "short". A bot whose turns
             are hours apart breaks a 1h cache as reliably as a 5m one and is
             paying 2.00x per write instead of 1.25x for nothing.
          5. ``contextPruning.ttl`` incoherent against the cache TTL the bot is
             ACTUALLY configured for → raise/lower it to match. This knob never
             moves the invalidation rate; it is only ever a coherence fix.
          6. Otherwise → 'hold'.

        Note the asymmetry in rule 5. Pruning BELOW the cache TTL is always
        wrong — `cache-ttl` mode exists to avoid rewriting a live prefix, and a
        short ttl defeats it, discarding a cache the bot just paid to write. Too
        HIGH is only flagged past cost_profiles.MAX_COHERENT_PRUNE_TTL_SECONDS,
        the bound PR #3497 established, because the shipped default (prune 1h
        over the 5-minute default cache) sits legitimately inside that band:
        the operator's likely next move is rule 3, which extends the cache to
        1h and makes that default exactly matched. Flagging it would have every
        deployed bot arguing with its own default on first page load.

        Optional ``shared_dir`` enables motivating-signal-id lookup so
        the frontend can offer inline snooze that resolves both the
        recommendation and the underlying Signal in one click.
        """
        # ── Gap distributions ────────────────────────────────────────────────
        # TWO series, deliberately:
        #
        #   user-turn gaps    how long a human waits before replying. Kept for
        #                     operator-facing context and confidence sizing.
        #   request gaps      every billable request in a session, heartbeats
        #                     included. THIS is the series the cache sees — a
        #                     heartbeat re-touches the prefix and resets the
        #                     clock exactly like a user turn does — so it is
        #                     what the break-even arithmetic runs on. v1 sized
        #                     everything off user turns alone, which is why
        #                     heartbeat-driven bots read as "sparse data".
        def _session_timestamps(user_turns_only: bool) -> dict[str, list[datetime]]:
            by_session: dict[str, list[datetime]] = {}
            for e in events:
                if user_turns_only and (e.get("trigger_kind") or "") != "user_turn":
                    continue
                sid = e.get("session_id") or ""
                ts = e.get("ts")
                if not sid or not isinstance(ts, str):
                    continue
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                by_session.setdefault(sid, []).append(dt)
            return by_session

        def _gaps(by_session: dict[str, list[datetime]]) -> list[float]:
            out: list[float] = []
            for dts in by_session.values():
                if len(dts) < 2:
                    continue
                dts.sort()
                for i in range(1, len(dts)):
                    delta = (dts[i] - dts[i - 1]).total_seconds()
                    if delta >= 0:
                        out.append(delta)
            out.sort()
            return out

        def _pct(sorted_gaps: list[float], q: float) -> float:
            if not sorted_gaps:
                return 0.0
            idx = min(int(len(sorted_gaps) * q), len(sorted_gaps) - 1)
            return sorted_gaps[idx]

        gaps = _gaps(_session_timestamps(user_turns_only=True))
        gap_count = len(gaps)
        p50_gap = _pct(gaps, 0.5)
        p95_gap = _pct(gaps, 0.95)

        request_sessions = _session_timestamps(user_turns_only=False)
        req_gaps = _gaps(request_sessions)
        req_gap_count = len(req_gaps)
        p50_req_gap = _pct(req_gaps, 0.5)
        p95_req_gap = _pct(req_gaps, 0.95)

        # ── Break-even arithmetic ────────────────────────────────────────────
        # A cacheWrite happens on the first request of a session, and again
        # after every gap that outlives the cache. Counting those two ways
        # gives the write volume under each retention setting; their ratio is
        # what decides whether the 1-hour cache pays for itself.
        session_count = len(request_sessions)
        short_ttl = _CACHE_TTL_SECONDS["short"]
        long_ttl = _CACHE_TTL_SECONDS["long"]
        writes_short = session_count + sum(1 for g in req_gaps if g > short_ttl)
        writes_long = session_count + sum(1 for g in req_gaps if g > long_ttl)
        rewrite_factor = (writes_short / writes_long) if writes_long else 1.0

        # ── Cache-state aggregates over the same window ──────────────────────
        participating = invalidated = warm = 0
        cache_write_on_invalidated = 0
        cache_write_total = 0
        for e in events:
            cache_write_total += int(e.get("cache_write_tokens", 0) or 0)
            state = e.get("cache_state") or ""
            if state not in ("warm", "invalidated"):
                continue
            participating += 1
            if state == "warm":
                warm += 1
            else:
                invalidated += 1
                cache_write_on_invalidated += int(e.get("cache_write_tokens", 0) or 0)
        invalidated_ratio = invalidated / participating if participating else 0.0
        warm_ratio = warm / participating if participating else 0.0

        metrics = {
            "p50_gap_seconds": round(p50_gap, 1),
            "p95_gap_seconds": round(p95_gap, 1),
            "gap_count": gap_count,
            "p50_request_gap_seconds": round(p50_req_gap, 1),
            "p95_request_gap_seconds": round(p95_req_gap, 1),
            "request_gap_count": req_gap_count,
            "session_count": session_count,
            "writes_under_short_cache": writes_short,
            "writes_under_long_cache": writes_long,
            "rewrite_factor": round(rewrite_factor, 3),
            "long_retention_breakeven": round(_LONG_RETENTION_BREAKEVEN, 3),
            "participating_count": participating,
            "invalidated_count": invalidated,
            "invalidated_ratio": round(invalidated_ratio, 4),
            "warm_ratio": round(warm_ratio, 4),
            "cache_write_tokens_total": cache_write_total,
            "cache_write_tokens_on_invalidated": cache_write_on_invalidated,
        }

        prune_ttl_str = (cache_config or {}).get("prune_ttl")
        prune_ttl_seconds = _parse_ttl_seconds(prune_ttl_str)
        retention = (cache_config or {}).get("cache_retention")
        effective_retention = retention or "short"
        cache_ttl_seconds = _CACHE_TTL_SECONDS[retention]

        # Look up matching firing Signal (used by the frontend snooze button).
        # Cache-state recommendations are the synthesis of the underlying
        # session_economics signals, so snoozing the recommendation should
        # snooze the signal it derives from. We pick the most recently
        # observed cache_invalidation_elevated for this bot (preferred) or
        # cache_hit_rate_low as a fallback.
        motivating_signal_id: str | None = None
        if shared_dir is not None:
            try:
                from signals import store as _signals_store
                best_ts = ""
                for sig in _signals_store.iter_active(
                    shared_dir,
                    producer="session_economics",
                    bot_id=bot_id,
                    state="firing",
                ):
                    if sig.type not in ("cache_invalidation_elevated", "cache_hit_rate_low"):
                        continue
                    if (sig.last_observed_at or "") > best_ts:
                        best_ts = sig.last_observed_at or ""
                        motivating_signal_id = sig.id
            except Exception:
                motivating_signal_id = None

        def _rec(
            *,
            verdict: str,
            knob: str | None = None,
            current_setting: str | None = None,
            target_setting: str | None = None,
            target_value: str | None = None,
            confidence: str,
            reasoning: str,
            savings: float | None = None,
            target_ttl_seconds: int | None = None,
        ) -> dict:
            """Assemble one recommendation.

            ``knob`` names WHICH setting to change and is the field the UI
            renders from — a recommendation that doesn't say which knob it
            means is exactly the bug this endpoint shipped with.
            ``target_ttl``/``target_ttl_seconds`` stay null unless the
            recommendation really is about contextPruning.ttl, so a client
            can't mistake a cache-retention call for a pruning one.
            """
            return {
                "bot_id": bot_id,
                "verdict": verdict,
                "knob": knob,
                "current_setting": current_setting,
                "target_setting": target_setting,
                "target_value": target_value,
                # contextPruning.ttl, as configured. Reported on every verdict
                # because it is context; only a targeted one sets target_ttl.
                "current_ttl": prune_ttl_str,
                "current_ttl_seconds": prune_ttl_seconds,
                "target_ttl": (
                    _format_ttl(target_ttl_seconds) if target_ttl_seconds is not None else None
                ),
                "target_ttl_seconds": target_ttl_seconds,
                # The prompt-cache knob and the lifetime it resolves to.
                "cache_retention": retention,
                "effective_cache_retention": effective_retention,
                "cache_ttl_seconds": cache_ttl_seconds,
                "confidence": confidence,
                "reasoning": reasoning,
                "estimated_monthly_savings_usd": (
                    round(savings, 2) if savings is not None else None
                ),
                "motivating_signal_id": motivating_signal_id,
                "metrics": metrics,
            }

        # ── 1. Confidence-gated bail-out for thin data ───────────────────────
        # Gap-count alone does NOT gate: heartbeat-heavy bots have plenty of
        # cache_state signal, and (since we now measure request gaps rather
        # than user-turn gaps) plenty of gap signal too.
        gap_density = max(gap_count, req_gap_count)
        if window_days < 3 or (participating < 50 and gap_density < 50):
            return _rec(
                verdict="not_enough_data",
                confidence="low",
                reasoning=(
                    f"Only {gap_density} inter-request gaps and {participating} "
                    f"cache-participating events over {window_days}d. Need at least "
                    f"50 of either for a recommendation."
                ),
            )

        # ── 2. Config unreadable ─────────────────────────────────────────────
        if cache_config is None:
            return _rec(
                verdict="hold",
                confidence="low",
                reasoning=(
                    "Could not read openclaw.json — bot may not be deployed. Both the "
                    "prompt-cache setting (agents.defaults.models.*.params.cacheRetention) "
                    "and the pruning ttl live there, so neither can be assessed."
                ),
            )

        # Confidence is "high" only when we have BOTH cache density (for the
        # invalidation signal) AND request density (for the break-even).
        if window_days >= 7 and gap_density >= 200 and participating >= 200:
            confidence = "high"
        elif gap_density >= 10 or participating >= 100:
            confidence = "medium"
        else:
            confidence = "low"

        have_economics = req_gap_count >= _MIN_GAPS_FOR_ECONOMICS
        factor_str = f"{rewrite_factor:.1f}x"
        # Two decimals: the break-even is 1.65, and rounding it to "1.7x" in
        # operator-facing text overstates the bar a bot has to clear.
        breakeven_str = f"{_LONG_RETENTION_BREAKEVEN:.2f}x"

        # A follow-on note when the pruning ttl would be left INCOHERENT by the
        # cache change we're recommending. Deliberately the same asymmetry as
        # rule 5 — a prune ttl sitting between the cache TTL and the 1h ceiling
        # is the shipped default's own position, and nagging about it here
        # would re-open through a side door the argument rule 5 declines to
        # have. Only "prunes while warm" and "above every possible cache" get a
        # note; both are wrong under any configuration.
        def _prune_followon(new_cache_ttl: int) -> str:
            if not (cache_config or {}).get("prune_active") or prune_ttl_seconds is None:
                return ""
            if prune_ttl_seconds < new_cache_ttl:
                return (
                    f" Once that lands, raise contextPruning.ttl from "
                    f"{_fmt_dur(prune_ttl_seconds)} to {_fmt_dur(new_cache_ttl)} — "
                    f"otherwise pruning fires while the longer cache is still warm "
                    f"and throws it away."
                )
            if prune_ttl_seconds > _max_coherent_prune_ttl():
                return (
                    f" Separately, contextPruning.ttl is {_fmt_dur(prune_ttl_seconds)}, "
                    f"longer than any cache Anthropic offers — lower it to "
                    f"{_fmt_dur(new_cache_ttl)}."
                )
            return ""

        # ── 3. Raise cache_retention → "long" ────────────────────────────────
        # The actual fix for cache invalidation. This is the ONLY knob that
        # changes how long a cached prefix lives.
        if (
            effective_retention == "short"
            and invalidated_ratio > _INVALIDATION_ELEVATED
            and have_economics
            and p95_req_gap > short_ttl
            and rewrite_factor > _LONG_RETENTION_BREAKEVEN
        ):
            savings = _estimate_retention_monthly_savings(
                cache_write_tokens_total=cache_write_total,
                rewrite_factor=rewrite_factor,
                window_days=window_days,
            )
            return _rec(
                verdict="raise",
                knob="cache_retention",
                current_setting=_CACHE_RETENTION_LABEL[retention],
                target_setting=_CACHE_RETENTION_LABEL["long"],
                target_value="long",
                confidence=confidence,
                savings=savings,
                reasoning=(
                    f"{int(invalidated_ratio * 100)}% of cached turns are invalidated "
                    f"({invalidated} of {participating}). The prompt cache is on "
                    f"Anthropic's 5-minute default, but p95 gap between requests is "
                    f"{_fmt_dur(p95_req_gap)} (median {_fmt_dur(p50_req_gap)}), so most "
                    f"turns arrive after it has expired and pay a full cacheWrite again. "
                    f"Set cache_retention to \"long\" — it maps to cache_control.ttl=\"1h\" "
                    f"and is the only setting that changes cache lifetime "
                    f"(contextPruning.ttl does not; it only gates when pruning may run). "
                    f"Economics: a 1h cache costs {_CACHE_WRITE_MULT['long']:.2f}x base per "
                    f"write vs {_CACHE_WRITE_MULT['short']:.2f}x for 5m, so it only wins "
                    f"above ~{breakeven_str} re-writes per hour-window; this bot is at "
                    f"{factor_str} ({writes_short} writes under a 5m cache vs "
                    f"{writes_long} under a 1h one). Apply on Cost Optimization → "
                    f"Prompt-cache TTL (better-engine-config.json::bots.{bot_id}"
                    f".budget.per_bot_cache_retention)." + _prune_followon(long_ttl)
                ),
            )

        # ── 4. Lower cache_retention → "short" ───────────────────────────────
        # The 1-hour cache is not free. A bot whose requests are hours apart
        # breaks it as reliably as the 5-minute one and pays 2.00x on every
        # write for the privilege.
        if (
            effective_retention == "long"
            and have_economics
            and rewrite_factor <= _LONG_RETENTION_BREAKEVEN
        ):
            savings = _estimate_retention_monthly_savings(
                cache_write_tokens_total=cache_write_total,
                rewrite_factor=rewrite_factor,
                window_days=window_days,
            )
            return _rec(
                verdict="lower",
                knob="cache_retention",
                current_setting=_CACHE_RETENTION_LABEL["long"],
                target_setting=_CACHE_RETENTION_LABEL["short"],
                target_value="short",
                confidence=confidence,
                # savings is the *loss* under "long"; negate to report the gain
                # from switching back.
                savings=-savings,
                reasoning=(
                    f"Cache retention is \"long\" (1h), but requests are "
                    f"{_fmt_dur(p50_req_gap)} apart at the median and "
                    f"{_fmt_dur(p95_req_gap)} at p95, so a 1-hour cache expires almost as "
                    f"often as a 5-minute one here: {writes_long} writes under a 1h cache "
                    f"vs {writes_short} under a 5m one, a {factor_str} re-write factor, "
                    f"below the ~{breakeven_str} break-even. Long retention bills "
                    f"{_CACHE_WRITE_MULT['long']:.2f}x base on every cacheWrite instead of "
                    f"{_CACHE_WRITE_MULT['short']:.2f}x, so this bot is paying the premium "
                    f"without converting writes into reads. Set cache_retention to "
                    f"\"short\"." + _prune_followon(short_ttl)
                ),
            )

        # ── 5. contextPruning.ttl coherence ──────────────────────────────────
        # Never a cache-lifetime fix — purely "does the prune clock agree with
        # the cache clock this bot is actually configured for?"
        prune_active = bool((cache_config or {}).get("prune_active"))
        if prune_active and prune_ttl_seconds is not None:
            if prune_ttl_seconds < cache_ttl_seconds:
                return _rec(
                    verdict="raise",
                    knob="context_pruning_ttl",
                    current_setting=_fmt_dur(prune_ttl_seconds),
                    target_setting=_fmt_dur(cache_ttl_seconds),
                    target_value=_format_ttl(cache_ttl_seconds),
                    target_ttl_seconds=cache_ttl_seconds,
                    confidence=confidence,
                    reasoning=(
                        f"contextPruning.ttl is {_fmt_dur(prune_ttl_seconds)}, below the "
                        f"{_fmt_dur(cache_ttl_seconds)} prompt cache this bot is configured "
                        f"for (cache_retention: {effective_retention!r}). `cache-ttl` "
                        f"pruning waits for the cache to go cold precisely so it does not "
                        f"rewrite a live prefix — a ttl below the cache TTL prunes while "
                        f"the cache is still warm and discards a prefix the bot just paid "
                        f"to write. Set it to {_fmt_dur(cache_ttl_seconds)} so pruning "
                        f"happens exactly when the cache dies and the rewrite is free. "
                        f"This is a coherence fix, not a cache-lifetime change: "
                        f"invalidation is at {int(invalidated_ratio * 100)}% and only "
                        f"cache_retention moves that number."
                    ),
                )
            if prune_ttl_seconds > _max_coherent_prune_ttl():
                return _rec(
                    verdict="lower",
                    knob="context_pruning_ttl",
                    current_setting=_fmt_dur(prune_ttl_seconds),
                    target_setting=_fmt_dur(cache_ttl_seconds),
                    target_value=_format_ttl(cache_ttl_seconds),
                    target_ttl_seconds=cache_ttl_seconds,
                    confidence=confidence,
                    reasoning=(
                        f"contextPruning.ttl is {_fmt_dur(prune_ttl_seconds)}, longer than "
                        f"any prompt cache Anthropic offers (1h maximum). Pruning never "
                        f"fires during the window between cache death and prune "
                        f"eligibility, so tool results ride untrimmed and every cache "
                        f"expiry re-writes that bulk at "
                        f"{_CACHE_WRITE_MULT[effective_retention]:.2f}x input price. Set it "
                        f"to {_fmt_dur(cache_ttl_seconds)} to match this bot's "
                        f"cache_retention ({effective_retention!r}). This is a coherence "
                        f"fix, not a cache-lifetime change: invalidation is at "
                        f"{int(invalidated_ratio * 100)}% and only cache_retention moves "
                        f"that number."
                    ),
                )

        # ── 6. Hold ──────────────────────────────────────────────────────────
        if effective_retention == "short" and not have_economics:
            cache_note = (
                f"Only {req_gap_count} inter-request gaps — not enough to size whether "
                f"a 1-hour cache would pay for itself, so cache_retention is left alone."
            )
        elif effective_retention == "short":
            cache_note = (
                f"A 1-hour cache would not pay for itself here: {factor_str} re-write "
                f"factor against a ~{breakeven_str} break-even, so \"short\" is correct."
            )
        else:
            cache_note = (
                f"\"long\" retention is earning its {_CACHE_WRITE_MULT['long']:.2f}x write "
                f"premium: {factor_str} re-write factor against a ~{breakeven_str} "
                f"break-even."
            )
        if not prune_active or prune_ttl_seconds is None:
            prune_note = " Pruning is off, so contextPruning.ttl is inert."
        elif prune_ttl_seconds == cache_ttl_seconds:
            prune_note = (
                f" contextPruning.ttl {_fmt_dur(prune_ttl_seconds)} matches the cache "
                f"exactly, so pruning happens when the rewrite is already free."
            )
        else:
            prune_note = (
                f" contextPruning.ttl {_fmt_dur(prune_ttl_seconds)} is above the "
                f"{_fmt_dur(cache_ttl_seconds)} cache but within the 1h bound, which is "
                f"where the shipped default sits — tightening it to "
                f"{_fmt_dur(cache_ttl_seconds)} would trim sooner at no cache cost, but "
                f"it is not costing anything a cache change wouldn't also fix."
            )
        # Don't lead with "well-matched" when invalidation is high. A bot whose
        # requests are hours apart invalidates constantly and there is nothing
        # to be done about it — saying the cache fits the traffic would read as
        # the endpoint not having noticed.
        lead = (
            f"Invalidation is {int(invalidated_ratio * 100)}%, but no setting change "
            f"would improve it"
            if invalidated_ratio > _INVALIDATION_ELEVATED
            else "Cache is well-matched to observed traffic"
        )
        return _rec(
            verdict="hold",
            confidence=confidence,
            reasoning=(
                f"{lead} (p50 request gap {_fmt_dur(p50_req_gap)}, p95 "
                f"{_fmt_dur(p95_req_gap)}, {int(warm_ratio * 100)}% warm). "
                f"{cache_note}{prune_note} No change recommended."
            ),
        )

    @app.get("/api/analytics/ttl-recommendation")
    def api_analytics_ttl_recommendation():
        """Per-bot cache recommendation derived from observed cache + gap data.

        When ``bot`` is set, returns one recommendation for that bot.
        When omitted, iterates all members + evolve and returns a list.

        Each entry names the knob it means in ``knob`` — ``cache_retention``
        (prompt-cache lifetime) or ``context_pruning_ttl`` (when pruning may
        run). The path stays ``/ttl-recommendation`` for client compatibility.
        """
        from cost_ledger import read_events

        bot_id = request.args.get("bot") or ""
        try:
            days = int(request.args.get("days", 30))
        except (ValueError, TypeError):
            days = 30
        shared = _shared()
        now_dt = datetime.now(timezone.utc)
        target_bots = [bot_id] if bot_id else _oc_members()

        recommendations: list[dict] = []
        for b in target_bots:
            try:
                events = list(read_events(b, days=days, shared_dir=shared, now=now_dt))
            except Exception:
                events = []
            verdict = _cache_verdict_for_bot(
                b, events,
                window_days=days,
                cache_config=_read_cache_config(b),
                shared_dir=shared,
            )
            recommendations.append(verdict)

        return jsonify({
            "bot_id": bot_id,
            "window_days": days,
            "recommendations": recommendations,
        })

    # ── Analytics: applications ────────────────────────────────
    @app.get("/api/analytics/applications")
    def api_analytics_applications():
        bot_id = request.args.get("bot")
        shared = _shared()
        bots = [bot_id] if bot_id else _members()
        result = {}

        # Pre-load usage-stats.json per bot (written daily by usage_logger.py).
        # This is the file-mtime FOOTPRINT (maintenance signal), NOT usage —
        # the card labels it as such and never derives Active/Inactive from it.
        # Keyed: {bot → {app_id → stats_dict}}
        usage_by_bot: dict[str, dict] = {}
        for bot in bots:
            usage_path = shared / bot / "recommendations" / "usage-stats.json"
            if usage_path.exists():
                try:
                    usage_by_bot[bot] = json.loads(usage_path.read_text()).get("apps", {})
                except Exception:
                    pass

        # Pre-load the per-app REAL usage rollup (AL-1.3) — turns + cost the
        # app actually served, from the plugin's turn annotations. This is the
        # PRIMARY usage signal; the mtime footprint above stays as the
        # fallback for bots with no attributed turns (design §8). Grades are
        # carried through per-tile exactly as written — `total` is
        # scheduled+explicit, `inferred` stays separate, and the tile shows
        # the pod's unattributed share so a low per-app number can be read
        # as "not attributed yet" rather than "not used".
        # Keyed: {bot → {app_id → 7d entry}}
        app_usage_by_bot: dict[str, dict] = {}
        app_usage_coverage: dict[str, dict] = {}
        try:
            _uba = _import_analyzer("usage_by_app")
        except Exception:
            _uba = None
        if _uba is not None:
            for bot in bots:
                rollup = _uba.load_usage_by_app(shared, bot)
                if not rollup:
                    continue
                app_usage_by_bot[bot] = {
                    app_id: entry.get("d7") or {}
                    for app_id, entry in (rollup.get("apps") or {}).items()
                }
                for app_id, entry in (rollup.get("apps") or {}).items():
                    app_usage_by_bot[bot][app_id]["last_seen_ts"] = entry.get("last_seen_ts")
                app_usage_coverage[bot] = {
                    **((rollup.get("coverage") or {}).get("d7") or {}),
                    "attributed_primary": _uba.has_attributed_turns(rollup, "d7"),
                }

        # Pre-load the REAL per-app delivery signal: most-recent
        # delivery_monitor outcome per app (did the scheduled delivery reach
        # the user?). Read-only fold over the ledger; absent for apps with no
        # scheduled, user-facing delivery — the card stays honest ("usage not
        # measured") rather than inferring activity from file edits.
        # Keyed: {bot → {app_id → status_dict}}
        delivery_by_bot: dict[str, dict] = {}
        try:
            from delivery_status import latest_status_by_app as _delivery_status
        except Exception:
            _delivery_status = None  # type: ignore[assignment]
        if _delivery_status is not None:
            for bot in bots:
                try:
                    delivery_by_bot[bot] = _delivery_status(shared, bot)
                except Exception:
                    delivery_by_bot[bot] = {}

        # Pre-load the execution-integrity harness coverage set per bot (Bite
        # 2a, spec-app-invocation-just-works §2.2/§3). The plugin middleware
        # (#3362) writes {shared}/{bot}/app-integrity-coverage.json listing the
        # apps whose script failures it intercepts pre-model — for those apps
        # the "May misreport failures" badge is false by construction and must
        # clear. Read once per bot; the reader fails toward the EMPTY set on any
        # absence/staleness/schema drift, so a badge only ever clears on PROVEN
        # coverage (the arc's load-bearing safety property). Keyed: {bot → set}.
        coverage_by_bot: dict[str, set[str]] = {}
        try:
            from ..applications.app_integrity_coverage import load_covered_ids
        except Exception:
            load_covered_ids = None  # type: ignore[assignment]
        if load_covered_ids is not None:
            for bot in bots:
                try:
                    coverage_by_bot[bot] = load_covered_ids(shared, bot)
                except Exception:
                    coverage_by_bot[bot] = set()

        for bot in bots:
            caps = []
            bot_usage = usage_by_bot.get(bot, {})
            bot_app_usage = app_usage_by_bot.get(bot, {})
            bot_usage_coverage = app_usage_coverage.get(bot)
            bot_delivery = delivery_by_bot.get(bot, {})
            manifest_paths = _list_manifests_as_bot(bot)
            for mf_path in manifest_paths:
                try:
                    try:
                        manifest = json.loads(Path(mf_path).read_text())
                    except PermissionError:
                        r = subprocess.run(
                            ["sudo", "/bin/cat", mf_path],
                            capture_output=True, text=True, timeout=5,
                        )
                        if r.returncode != 0:
                            continue
                        manifest = json.loads(r.stdout)
                    # v7-arc Instances don't carry name/description/tags — those
                    # live in the bound Spec. Hydrate so the UI gets a v13-shaped
                    # manifest dict with real names + descriptions.
                    #
                    # Resolve the harness coverage key BEFORE hydrate. The
                    # middleware keys coverage off the RAW manifest's app-id.
                    # identity: see applications.app_identity.resolve_app_id —
                    # AL-1.4a put the canonical app_id at the head, so the order
                    # is app_id → pkg_id → id → spec_id → instance_id (and since
                    # #3681 the app_id FIELD only wins when it is a conforming
                    # slug). _resolve_app_id below is that resolver, imported
                    # through app_integrity_coverage — its documented
                    # coverage-side re-export, not a second copy.
                    # Hydration rewrites id/pkg_id for v7-arc instances (sets
                    # id=instance_id, pkg_id=spec_id), so resolving post-hydrate
                    # would compute a DIFFERENT key than the plugin wrote and
                    # silently miss the match. Raw v7-arc instances carry no
                    # top-level pkg_id/id/spec_id, so both sides key on
                    # instance_id. Captured here, consumed in the freelance block.
                    try:
                        from ..applications.app_integrity_coverage import (
                            resolve_app_id as _resolve_app_id,
                        )
                        _raw_app_id = _resolve_app_id(manifest)
                    except Exception:
                        _raw_app_id = ""
                    # Compute spec drift BEFORE hydrate so the badge can show
                    # version delta — hydrate preserves provenance, but we want
                    # to keep the drift call adjacent to the v7-arc branch.
                    spec_drift_info = {
                        "kind": "unknown", "current": None, "latest": None,
                        "versions_behind": None,
                    }
                    if manifest.get("manifest_shape") == "v7-arc":
                        from ..applications.manifest import hydrate_v7_arc_instance
                        from ..applications.spec_drift import instance_drift_status
                        try:
                            spec_drift_info = instance_drift_status(manifest, shared)
                        except Exception:
                            # Defensive — drift compute must not break the endpoint.
                            pass
                        manifest = hydrate_v7_arc_instance(manifest, shared)
                    cap_id = manifest.get("id", Path(mf_path).stem)
                    # ── Defined/Discovered provenance + drift narrative ───────
                    # (spec §9, Bites 1+3 → Bite-4 tile surface). The endpoints
                    # write back by FILENAME STEM, which can differ from the
                    # internal `id` above (gallery v7-arc-pre apps), so the tile
                    # carries the stem explicitly (§9.7 finding #5). Both
                    # `definition_status` and `drift_log` are Instance fields
                    # that survive hydrate (dict(instance) copy), so reading the
                    # hydrated manifest here is correct for both shapes. Absent
                    # definition_status reads as "discovered" (the inert
                    # default + fleet-migration value).
                    manifest_stem = Path(mf_path).stem
                    definition_status = (
                        str(manifest.get("definition_status") or "").strip().lower()
                        or "discovered"
                    )
                    try:
                        from ..applications.drift_classifier import (
                            count_unreviewed_drift, iter_scanner_drift,
                        )
                        unreviewed_drift_count = count_unreviewed_drift(manifest)
                        # Latest 25 entries keep the grid payload light; the
                        # badge count is exact regardless of this cap.
                        drift_entries = iter_scanner_drift(manifest)[:25]
                    except Exception:
                        unreviewed_drift_count = 0
                        drift_entries = []
                    # ── Promotion readiness (AL-1.6b; design §7.1) ────────
                    # Only discovered apps are promotion candidates, so a
                    # defined one carries None rather than a stale number the
                    # tile would have to learn to ignore. The scorer is pure
                    # over this dict — no I/O, no clock — so it adds nothing
                    # to the per-manifest cost of this loop. Guarded locally
                    # like the drift block above because the whole body sits
                    # in a broad `except: pass` that would silently drop the
                    # app from the grid.
                    readiness = None
                    if definition_status != "defined":
                        try:
                            from ..applications.app_readiness import readiness_payload
                            readiness = readiness_payload(manifest)
                        except Exception:
                            readiness = None
                    # Latest-test-result lookup removed 2026-06-08 along with
                    # the rest of the app-test surface.
                    # Support both old evidence dict and new top-level evidence_files list
                    evidence = manifest.get("evidence", {})
                    evidence_files = manifest.get("evidence_files") or (
                        evidence.get("files", []) if isinstance(evidence, dict) else []
                    )
                    # Support both old flat satisfaction_score and new nested satisfaction dict
                    sat = manifest.get("satisfaction") or {}
                    satisfaction_score = (
                        manifest.get("satisfaction_score")
                        if manifest.get("satisfaction_score") is not None
                        else sat.get("score")
                    )
                    # Merge per-app activity stats from usage_logger.py.
                    # Activity is measured via mtime sweep over each manifest's
                    # evidence paths — fields are None when usage_logger.py hasn't run.
                    app_usage = bot_usage.get(cap_id, {})
                    # AL-1.3 per-app rollup (7d). Grades stay separate:
                    # `usage_turns_7d` / `usage_cost_7d` are the deterministic
                    # total (scheduled + explicit); inferred rides in its own
                    # field so the tile can badge it as inferred, never as
                    # fact. All None when the rollup hasn't run for this bot.
                    # Joined on the manifest id. identity: see
                    # applications.app_identity.resolve_app_id — `cap_id` is
                    # deliberately the POST-hydrate `id`, which is what this
                    # tile and every other map on it are keyed by; the raw
                    # resolver answer is captured separately above as
                    # `_raw_app_id` because hydration rewrites id/pkg_id. The
                    # annotation the rollup is keyed by is the RAW answer, so
                    # an app whose two answers differ still shows "not
                    # measured" here rather than a wrong number — the join is
                    # deliberately exact, never fuzzy. Re-keying it is AL-1.5
                    # work (design §3: instance identity collapses onto the
                    # Spec's app_id), NOT a behavior-neutral reader sweep.
                    app_rollup = bot_app_usage.get(cap_id) or {}
                    _rollup_total = app_rollup.get("total") or {}
                    _rollup_inferred = app_rollup.get("inferred") or {}
                    crons = manifest.get("crons") or []
                    # Behavioral-run aggregate computation removed 2026-06-08
                    # — app-test surface killed.
                    # Count of files actually registered to this app (v5+ files
                    # list for legacy; realized_files for v7-arc). Distinct
                    # from usage_total_files, which only populates after
                    # usage_logger.py has run. The tile uses files_count to
                    # show "scan pending" instead of "No evidence files" when
                    # the manifest has registered files but usage data is stale.
                    files_count = len(
                        manifest.get("files")
                        or manifest.get("realized_files")
                        or []
                    )
                    # Coherence + reconciliation chip summary (spec §10).
                    # Pure-Python derivation from the manifest's existing
                    # coherence/reconciliation blocks; populated since PR 4
                    # (Pass A) + PR 6 (orphan handling). Pre-coherence
                    # manifests just look "ok" because there are no findings.
                    try:
                        from ..applications.coherence_actions import (
                            coherence_summary,
                        )
                        cd_summary = coherence_summary(manifest)
                    except Exception:
                        cd_summary = {}
                    # Agent-freelance-bypass posture (spec-agent-freelance-
                    # bypass-phase2-2026-06-06). The Apps grid renders a
                    # reliability chip ("May misreport failures") + a
                    # "Make reliable" action off this payload. Two firing cases,
                    # both for apps still in agent_invokes mode:
                    #   1. severity "warning" — at-risk-shaped (a chat trigger
                    #      runs a bot-local script) with NO structured triggers.
                    #      A script failure can leak a raw "(agent) failed" line
                    #      or confabulate a fake success. These can't be
                    #      one-click migrated (no triggers to wire), so the modal
                    #      explains what's needed; can_migrate stays False.
                    #   2. can_migrate — has structured triggers that the pure
                    #      migrator can wire to plugin_intercept (resolvable
                    #      script + a registered stdout_protocol). The
                    #      "Make reliable" button actually works here.
                    # `can_migrate` is a dry-run of the (pure, no-I/O) migrator.
                    # Defensive: a validator/migrator error must never break the
                    # endpoint; every other app stays None so the field is cheap.
                    freelance = None
                    try:
                        from ..applications.bot_guidance_freelance_validator import (
                            validate_bot_guidance,
                        )
                        _bg = validate_bot_guidance(manifest)
                        _warn = _bg.get("severity") == "warning" and _bg.get("is_at_risk")
                        _mode = _bg.get("invocation_mode") or "agent_invokes"
                        _can_migrate = False
                        if _mode == "agent_invokes":
                            try:
                                from ..applications.plugin_intercept_migration import (
                                    migrate_to_plugin_intercept,
                                )
                                _mig = migrate_to_plugin_intercept(manifest)
                                _can_migrate = bool(_mig.get("ok") and _mig.get("changed"))
                            except Exception:
                                _can_migrate = False
                        # Harness-aware (Bite 2a): is this app's script failure
                        # path PROVABLY covered by the integrity middleware? If so
                        # the "May misreport failures" condition is false by
                        # construction — the middleware substitutes an honest
                        # failure result pre-model — so the badge must clear.
                        # id_is_covered is conservative: a covered==unknown app
                        # (missing/stale/schema-drifted coverage file, or an
                        # id not in the covered set) returns False → badge KEPT.
                        try:
                            from ..applications.app_integrity_coverage import (
                                id_is_covered,
                            )
                            _covered = id_is_covered(
                                _raw_app_id, coverage_by_bot.get(bot, set())
                            )
                        except Exception:
                            _covered = False
                        if _warn or _can_migrate:
                            # severity stays the validator's own value (honest);
                            # the chip fires on warning OR can_migrate. A
                            # migratable-but-not-prose-at-risk app is severity
                            # "info" + can_migrate True — not a fake "warning".
                            # integrity_covered rides along so the UI (apps.js
                            # _freelanceMigrateChip) is the visible clear point:
                            # it suppresses the badge iff coverage is proven.
                            freelance = {
                                "severity": _bg.get("severity"),
                                "invocation_mode": _mode,
                                "is_at_risk": bool(_bg.get("is_at_risk")),
                                "can_migrate": _can_migrate,
                                "integrity_covered": _covered,
                                "message": _bg.get("message", ""),
                            }
                    except Exception:
                        freelance = None
                    # Canonical identity source for the Apps tile. The detail/Edit
                    # modal (apps.js viewManifest) renders `identity.purpose`; the
                    # tile renders this `description` field. Prefer
                    # `identity.purpose` FIRST so the two surfaces show ONE
                    # canonical string — a stale/contaminated top-level
                    # `description` (e.g. atlas-article-capture's "Member
                    # Management" residue from the #3095/#3108 conflation
                    # incident) no longer diverges from the purpose the detail
                    # view shows. Falls back to the legacy top-level `description`
                    # when no identity.purpose exists (v7-arc Instances,
                    # un-migrated legacy manifests). `identity` is schema-typed as
                    # a dict, but guard non-dict values defensively — this render
                    # path must never raise and drop a real app from the grid.
                    _identity = manifest.get("identity")
                    _purpose = (
                        _identity.get("purpose", "")
                        if isinstance(_identity, dict) else ""
                    )
                    caps.append({
                        "id": cap_id,
                        "name": manifest.get("name", cap_id),
                        "description": _purpose or manifest.get("description", ""),
                        "priority": manifest.get("priority", "feature"),
                        "status": manifest.get("status", "draft"),
                        "source": manifest.get("source", "detected"),
                        "confidence": manifest.get("confidence"),
                        "evidence_files": evidence_files,
                        "files_count": files_count,
                        "satisfaction_score": satisfaction_score,
                        "created_at": manifest.get("created_at", ""),
                        "usage_last_modified_ts":     app_usage.get("last_modified_ts"),
                        "usage_days_since_modified":  app_usage.get("days_since_modified"),
                        "usage_active_files_30d":     app_usage.get("active_files_30d"),
                        "usage_active_files_60d":     app_usage.get("active_files_60d"),
                        "usage_total_files":          app_usage.get("total_files"),
                        "usage_evidence_present":     app_usage.get("evidence_present"),
                        # ── Real per-app usage (AL-1.3) — turns this app
                        # actually served, from the attribution rollup.
                        # None (not 0) when unmeasured: the tile must say
                        # "not measured yet", never "$0.00 spent".
                        "usage_turns_7d":             _rollup_total.get("turns"),
                        "usage_cost_7d":              _rollup_total.get("cost_estimated"),
                        "usage_inferred_turns_7d":    _rollup_inferred.get("turns"),
                        "usage_inferred_cost_7d":     _rollup_inferred.get("cost_estimated"),
                        "usage_last_seen_ts":         app_rollup.get("last_seen_ts"),
                        # Pod-level honesty companion: what share of this
                        # bot's turns carried NO app attribution at all. A
                        # small per-app number next to a large unattributed
                        # share means "we can't see it yet", not "unused".
                        "usage_attribution_coverage": bot_usage_coverage,
                        # Real delivery signal (delivery_monitor ledger): the
                        # most-recent classified outcome for this app's
                        # scheduled, user-facing deliveries, or None when the
                        # monitor has no window recorded (fresh / non-scheduled
                        # / unmonitorable mechanism). The card renders this as
                        # the PRIMARY activity line; footprint above is muted
                        # secondary. See delivery_status.latest_status_by_app.
                        "delivery":                   bot_delivery.get(cap_id),
                        # Lifecycle fields
                        "has_crons": len(crons) > 0,
                        # Companion activity signals — the UI's "No evidence
                        # files" warning uses these to avoid nagging on
                        # cron / scheduled / heartbeat-driven apps that
                        # legitimately have no evidence_files entries.
                        # Added 2026-06-08 after team-bot-c's Apps tile flagged
                        # cron-driven system-health-monitoring + security-bot-
                        # monitoring as fileless.
                        "has_scheduled_actions": len(
                            manifest.get("scheduled_actions") or []
                        ) > 0,
                        # Phase 4.5 materialize failures (audit slate S2):
                        # count of scheduled_actions[] whose most recent
                        # forge install attempt failed (install_error
                        # stamped by forge_engine, cleared on success). The
                        # card renders a "schedule not live" warning off
                        # this — without it a partially-materialized app is
                        # indistinguishable from a healthy one until the
                        # delivery monitor's first missed window (never,
                        # for non-user-facing actions).
                        "scheduled_install_errors": sum(
                            1 for a in (manifest.get("scheduled_actions") or [])
                            if isinstance(a, dict) and a.get("install_error")
                        ),
                        "has_heartbeat_evidence": bool(
                            manifest.get("heartbeat_evidence") or {}
                        ),
                        # identity: see applications.app_identity.resolve_app_id
                        # — an API response COLUMN carried next to "id" above,
                        # so the tile can show the gallery package a v7-arc app
                        # is bound to. Two fields, two questions.
                        "pkg_id":    manifest.get("pkg_id", ""),
                        # Defined/Discovered lifecycle (spec §9). The tile shows
                        # a provenance chip + promote/demote control off these.
                        # `manifest_stem` is the identifier the definition
                        # endpoints expect on write (§9.7 finding #5) — pass it,
                        # NOT `id`. drift_log is the slim scanner-drift list for
                        # the unreviewed-changes panel; the badge counts
                        # unreviewed_drift_count (0 for discovered apps, which
                        # accrue no narrative).
                        "definition_status":     definition_status,
                        # Promotion readiness for a discovered app (AL-1.6b).
                        # None for a defined app and for any manifest the
                        # scorer declined — the tile renders nothing in both
                        # cases rather than inventing a zero.
                        "readiness":             readiness,
                        "manifest_stem":         manifest_stem,
                        "unreviewed_drift_count": unreviewed_drift_count,
                        "drift_log":             drift_entries,
                        # v25 purpose/fit label (app-scan classifier, PR #2899).
                        # "application" | "capability". INERT DEFAULT
                        # "application" — a manifest without the key (legacy /
                        # un-classified) or with any unrecognized value stays an
                        # application, so the Apps grid never hides a real app.
                        # Only a confident "capability" verdict routes a manifest
                        # off the grid (apps.js renderCapabilities). For v7-arc
                        # the field rides on the Instance and survives hydrate.
                        "app_kind":  manifest.get("app_kind") or "application",
                        # Agent-freelance-bypass nudge (spec-agent-freelance-
                        # bypass-phase2). Present only for at-risk-shaped
                        # manifests still in agent_invokes mode (severity
                        # "warning"); None otherwise. apps.js renders a
                        # "migrate to plugin_intercept" chip off this.
                        "freelance": freelance,
                        # App-test fields (test_cadence, test_exemption_reason,
                        # smoke_*, behavior_*, last_test_*, tests_total) were
                        # removed 2026-06-08 — surface killed.
                        # Spec drift (S3c). kind: none|drift|downgrade|
                        # spec_missing|unknown. UI shows a badge for drift /
                        # spec_missing; hides 'none' and 'unknown'.
                        "spec_drift_kind":      spec_drift_info["kind"],
                        "current_spec_version": spec_drift_info["current"],
                        "latest_spec_version":  spec_drift_info["latest"],
                        "spec_versions_behind": spec_drift_info["versions_behind"],
                        # Coherence + Drift (spec §10–§11). See cap-card
                        # chip rendering in index.html.
                        **cd_summary,
                    })
                except Exception:
                    pass
            result[bot] = caps
        return jsonify(result)

    # ── Recommendations: list ──────────────────────────────────
    @app.get("/api/bots/<bot_id>/recommendations")
    def api_recommendations_list(bot_id: str):
        """Return active recommendations for a bot.

        Query params:
          status   — comma-separated; default "new,surfaced"
          category — single category filter; default all
        """
        shared = _shared()
        status_param = request.args.get("status", "new,surfaced")
        statuses = [s.strip() for s in status_param.split(",") if s.strip()]
        category = request.args.get("category") or None

        try:
            recs_mod = _import_analyzer("recommendations")
            recs = recs_mod.load_recommendations(
                bot_id, shared, status=statuses, category=category
            )
        except Exception as e:
            return jsonify({"error": str(e), "recommendations": [], "summary": {}}), 500

        # Build summary counts by category
        by_category: dict[str, int] = {}
        for r in recs:
            cat = r.get("category", "unknown")
            by_category[cat] = by_category.get(cat, 0) + 1

        return jsonify({
            "bot_id": bot_id,
            "recommendations": recs,
            "summary": {
                "total": len(recs),
                "by_category": by_category,
            },
        })

    # ── Recommendations: status update ────────────────────────
    @app.post("/api/bots/<bot_id>/recommendations/<rec_id>/status")
    def api_recommendations_status(bot_id: str, rec_id: str):
        """Update the status of a recommendation (e.g. dismiss or accept).

        Body: {"status": "dismissed"|"accepted", "note": "optional reason"}
        """
        shared = _shared()
        body = request.get_json(silent=True) or {}
        new_status = body.get("status", "")
        note = body.get("note")

        valid_statuses = {"new", "surfaced", "accepted", "dismissed"}
        if new_status not in valid_statuses:
            return jsonify({"error": f"Invalid status '{new_status}'; must be one of {sorted(valid_statuses)}"}), 400

        try:
            recs_mod = _import_analyzer("recommendations")
            updated = recs_mod.update_recommendation_status(
                bot_id, rec_id, new_status, shared, note=note
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500

        if updated is None:
            return jsonify({"error": f"Recommendation '{rec_id}' not found"}), 404

        return jsonify(updated)

    # ── Analytics: proposal funnel + per-detector counts ──────────────────
    # Both endpoints walked the legacy v1 subdirs (pending / reviewed /
    # approved / deployed / rejected) and read the v1 schema fields
    # (pattern_key, etc.). With v1 writers gone, they returned mostly-empty
    # data. The Analytics page now derives the funnel from
    # /api/arbiter/proposals statuses and the per-generator chart from
    # /api/arbiter/generators track records — both client-side, so the
    # endpoints are gone entirely.

    # ── Analytics: turns ────────────────────────────────────────
    @app.get("/api/analytics/turns")
    def api_analytics_turns():
        bot_id = request.args.get("bot")
        days = int(request.args.get("days", 30))
        cfg = load_network(network_path)
        bots = cfg.get("bots", {})
        members = cfg.get("members", [])

        if bot_id:
            if bot_id not in bots and bot_id not in members:
                return jsonify({"error": "Unknown bot"}), 404
            bot_list = [bot_id]
        else:
            bot_list = list(bots.keys()) if bots else list(members)

        cutoff = date.today() - timedelta(days=days)
        result = {}

        for bid in bot_list:
            # Check all candidate dirs (shared primary, workspace/memory fallback, legacy home).
            # Mirrors what usage_analytics.py does so the Turns tab finds data regardless
            # of whether the bot is on the new shared-dir path or the old workspace path.
            paths_info = resolve_bot_paths(bid)
            candidate_dirs = [Path(p) for p in paths_info.get("turns_dir_candidates", [])]
            # Deduplicate while preserving order
            seen: set[Path] = set()
            turns_dirs: list[Path] = []
            for p in candidate_dirs:
                if p not in seen:
                    seen.add(p)
                    turns_dirs.append(p)

            summary: dict[str, Any] = {
                "turns": 0, "cost": 0.0,
                "input_tokens": 0, "output_tokens": 0,
                "token_turns": 0, "api_key_turns": 0,
            }
            daily: dict[str, dict] = {}
            by_source: dict[str, int] = {}
            by_model: dict[str, int] = {}
            by_channel: dict[str, int] = {}

            cur = cutoff
            today = date.today()
            while cur <= today:
                d_str = cur.isoformat()
                seen_records: set[str] = set()
                for turns_dir in turns_dirs:
                    fname = turns_dir / f"turns-{d_str}.jsonl"
                    for rec in _read_jsonl(fname, user=bid):
                        # Deduplicate across dirs by (ts, session_id) if available
                        dedup_key = f"{rec.get('ts','')}:{rec.get('session_id','')}"
                        if dedup_key in seen_records:
                            continue
                        seen_records.add(dedup_key)

                        cost = float(rec.get("cost", 0))
                        auth_mode = rec.get("auth_mode", "")
                        source = rec.get("source", "") or "unknown"
                        model = rec.get("model", "") or "unknown"
                        channel = rec.get("channel", "") or "unknown"

                        summary["turns"] += 1
                        summary["cost"] += cost
                        summary["input_tokens"] += int(rec.get("input_tokens", 0))
                        summary["output_tokens"] += int(rec.get("output_tokens", 0))
                        if auth_mode == "token":
                            summary["token_turns"] += 1
                        elif auth_mode == "api_key":
                            summary["api_key_turns"] += 1

                        if d_str not in daily:
                            daily[d_str] = {
                                "date": d_str, "turns": 0, "cost": 0.0,
                                "token_turns": 0, "api_key_turns": 0,
                                "cache_write_tokens": 0,
                            }
                        daily[d_str]["turns"] += 1
                        daily[d_str]["cost"] += cost
                        daily[d_str]["cache_write_tokens"] += int(rec.get("cache_write_tokens", 0))
                        if auth_mode == "token":
                            daily[d_str]["token_turns"] += 1
                        elif auth_mode == "api_key":
                            daily[d_str]["api_key_turns"] += 1

                        by_source[source] = by_source.get(source, 0) + 1
                        by_model[model] = by_model.get(model, 0) + 1
                        by_channel[channel] = by_channel.get(channel, 0) + 1

                cur = cur + timedelta(days=1)

            summary["cost"] = round(summary["cost"], 6)
            for ds in daily:
                daily[ds]["cost"] = round(daily[ds]["cost"], 6)

            result[bid] = {
                "summary": summary,
                "daily": [daily[ds] for ds in sorted(daily)],
                "by_source": by_source,
                "by_model": by_model,
                "by_channel": by_channel,
            }

        return jsonify(result)

    @app.get("/api/analytics/turns/raw")
    def api_analytics_turns_raw():
        bot_id = request.args.get("bot")
        date_str = request.args.get("date", "")
        limit = min(int(request.args.get("limit", 100)), 500)
        offset = int(request.args.get("offset", 0))

        cfg = load_network(network_path)
        bots = cfg.get("bots", {})
        members = cfg.get("members", [])

        if not bot_id or (bot_id not in bots and bot_id not in members):
            return jsonify({"error": "valid bot required"}), 400

        if not date_str:
            date_str = date.today().isoformat()

        try:
            date.fromisoformat(date_str)
        except ValueError:
            return jsonify({"error": "invalid date format"}), 400

        paths_info = resolve_bot_paths(bot_id)
        candidate_dirs = [Path(p) for p in paths_info.get("turns_dir_candidates", [])]

        all_turns: list[dict] = []
        seen_keys: set[str] = set()
        for turns_dir in candidate_dirs:
            fname = turns_dir / f"turns-{date_str}.jsonl"
            for rec in _read_jsonl(fname, user=bot_id):
                dedup_key = f"{rec.get('ts','')}:{rec.get('session_id','')}"
                if dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    all_turns.append(rec)

        total = len(all_turns)
        return jsonify({"turns": all_turns[offset:offset + limit], "total": total})

    # ── Analytics: usage (turn-based, ported from ocadmin) ────────
    @app.get("/api/analytics/usage")
    def api_analytics_usage():
        """
        Full usage & cost summary from turn JSONL files.
        Backed by usage_analytics.py (ported from openclaw-admin.py).

        ?bot=admin_bot|team_bot_a|security_bot|all  (default: all)
        ?days=7|30|90              (default: 7)
        ?channel=telegram          (optional filter)
        ?source=human|cron|subagent (optional filter)
        """
        import io as _io, csv as _csv
        from usage_analytics import load_turns, compute_summary

        bot = request.args.get("bot")
        days = min(int(request.args.get("days", 7)), 365)
        channel = request.args.get("channel") or None
        source  = request.args.get("source")  or None
        bot_id  = None if bot in (None, "", "all") else bot

        try:
            turns = load_turns(
                bot_id,
                days=days,
                channel_filter=channel,
                source_filter=source,
                network_path=str(network_path),
            )
            summary = compute_summary(turns)
            # Cache-only name enrichment: By User rows get a resolved display
            # name (or a categorized fallback); By Channel rows get a cached
            # conversation label ("#channel" / "DM · <name>" / Group DM /
            # Telegram title), the raw id otherwise. No live Slack/Telegram
            # calls on this render path.
            network = load_network(network_path)
            summary = _enrich_usage_names(summary, network, _shared())
            # Fire-and-forget background warms so the conversation + user
            # name caches self-heal without a manual CLI run. NON-blocking —
            # each spawns a daemon thread and returns immediately; the render
            # above already ran cache-only.
            _kick_conversation_warm(network_path, days)
            _kick_user_warm(network_path, days)
            return jsonify(summary)
        except Exception as e:
            return error_response(e)

    # ── Analytics: usage by app (AL-1.3) ──────────────────────────
    @app.get("/api/analytics/usage/by-app")
    def api_analytics_usage_by_app():
        """Per-app usage rollup, per bot, split by attribution grade.

        Reads ``{shared}/{bot}/usage-by-app.json`` (written daily by
        ``analyzer/usage_by_app.py``). The response is a thin re-shape of
        that file — this route deliberately does NOT re-derive, merge, or
        collapse anything, because the honesty contract lives in the file
        (internal/design-app-attribution-2026-08-15.md §3):

          * ``total`` per app is scheduled + explicit; ``inferred`` rides
            beside it and is never folded in.
          * ``unattributed`` and its ``coverage`` share are returned for
            EVERY bot, including bots with no attributed turns at all. A
            client that hides them is misreporting coverage.

        ``fallback`` names the bots for which the rollup has no attributed
        turns yet — those tiles fall back to usage_logger's mtime
        footprint (``usage-stats.json``), the pre-AL-1.3 structural
        inference, and the client must label them as such.

        ?bot=<bot_id>  (default: every member + evolve)
        ?window=d1|d7|d30  (default: d7 — the window the tiles render)
        """
        window = request.args.get("window") or "d7"
        if window not in ("d1", "d7", "d30"):
            return jsonify({"error": "window must be d1, d7 or d30"}), 400
        bot_id = request.args.get("bot")
        shared = _shared()
        bots = [bot_id] if bot_id else _oc_members()

        try:
            uba = _import_analyzer("usage_by_app")
        except Exception as exc:  # analyzer package missing — say so
            return jsonify({"error": f"usage_by_app unavailable: {exc}"}), 500

        out: dict[str, Any] = {}
        fallback: list[str] = []
        for bot in bots:
            payload = uba.load_usage_by_app(shared, bot)
            if not payload:
                # No rollup file yet: "not measured", never zero usage.
                out[bot] = {"measured": False, "apps": [], "unattributed": None,
                            "coverage": None, "evolve_overhead": None,
                            "generated_at": None}
                fallback.append(bot)
                continue
            apps = [
                {
                    "app_id": app_id,
                    "last_seen_ts": entry.get("last_seen_ts"),
                    "first_seen_ts": entry.get("first_seen_ts"),
                    **(entry.get(window) or {}),
                }
                for app_id, entry in (payload.get("apps") or {}).items()
            ]
            apps.sort(
                key=lambda a: (a.get("total") or {}).get("cost_estimated") or 0.0,
                reverse=True,
            )
            out[bot] = {
                "measured": True,
                "generated_at": payload.get("generated_at"),
                "apps": apps,
                "unattributed": (payload.get("unattributed") or {}).get(window),
                "evolve_overhead": (payload.get("evolve_overhead") or {}).get(window),
                "coverage": (payload.get("coverage") or {}).get(window),
            }
            if not uba.has_attributed_turns(payload, window):
                fallback.append(bot)

        return jsonify({
            "window": window,
            "bots": out,
            # Bots whose per-app numbers still come from the mtime sweep.
            "usage_stats_fallback_bots": fallback,
        })

    @app.get("/api/analytics/model-economics")
    def api_analytics_model_economics():
        """Pod-wide, model-centric cost + performance leaderboard (transpose of Cost).

        One row per model used across the WHOLE pod, default-sorted by $/turn
        desc, plus the configured-but-unused remainder of the catalog.

        Thin route: reuses usage_analytics.compute_summary (the same by_model /
        by_model_by_audience the Cost page consumes), joins list price + band,
        and lets model_economics.assemble_model_economics do the enrichment.
        v1.5 collapses sub-series to one row per model identity and adds the
        bot×model matrix + band/role rollups + audience re-split (all additive).

        v2 (additive) merges a per-model `perf` block onto each row from the
        cascade turn-spans (latency p50/p95, struggle_avg, success_rate,
        error_rate) plus a pod-level `perf_coverage_pct`. Cost is over ALL
        turns; perf is over the span-covered SUBSET (~49% coverage) — kept as a
        distinct block, never conflated. See _attach_perf_block (fail-safe: a
        span-store hiccup degrades perf to null, the cost payload stays intact).

        ?days=7|30|90  (default: 30 — a pod-wide lens wants a wider window than
                        the per-bot Cost page's 7)
        Facet filters (spec v1.5 §5):
          ?provider=<id>     filter to one provider
          ?band=low|medium|high|premium
          ?audience=human|auto   re-split the audience-safe metrics
                                 (Eff. cost/1k is nulled per-audience)
          ?bot=<bot_id>      LOAD-time re-scope (load_turns(bot_id=…)); the whole
                             payload + matrix then reflect that single bot
        """
        from usage_analytics import load_turns, compute_summary
        from model_pricing import read_pricing_cache
        from model_discovery import (
            read_listings_cache, discover_credentialed_providers,
        )
        from model_economics import (
            assemble_model_economics, filter_economics, normalize_audience,
        )

        days = min(int(request.args.get("days", 30)), 365)
        provider = request.args.get("provider") or None
        band = request.args.get("band") or None
        audience = request.args.get("audience") or None
        bot = request.args.get("bot")
        bot_id = None if bot in (None, "", "all") else bot
        try:
            shared = _shared()
            network = load_network(network_path)
            # Pod-wide by default (bot_id=None pools every bot's turns); ?bot=
            # re-scopes to one bot. turns also feed the (bot × model) matrix.
            turns = load_turns(bot_id, days=days, network_path=str(network_path))
            summary = compute_summary(turns)
            pricing_cache = read_pricing_cache(shared)
            # Listings cache feeds the bare-key → provider resolution (one of the
            # data sources that collapse a model the gateway logged both
            # qualified and bare onto a single identity).
            listings_cache = read_listings_cache(shared)
            # Pod-wide credentialed-provider set (uncredentialed-catalog honesty):
            # which providers any bot actually holds an api_key for. Data-derived
            # from auth-profiles, no provider literals. _oc_members() includes the
            # evolve bot (whose turns also appear in usage), so a key held only by
            # evolve is not missed — broader membership keeps us from FALSE-flagging
            # a credentialed model. Fail-open to None on any read failure: assemble
            # then flags nothing (never false-flag the catalog on a transient miss).
            try:
                credentialed_providers = (
                    discover_credentialed_providers(_oc_members()) or None
                )
            except Exception:
                credentialed_providers = None
            payload = assemble_model_economics(
                summary, pricing_cache=pricing_cache, network=network, turns=turns,
                listings_cache=listings_cache,
                credentialed_providers=credentialed_providers,
            )
            # ── v2 performance block (additive, span-covered SUBSET) ──────────
            # Merge a per-model `perf` block onto each economics row by identity,
            # plus pod-level `perf_coverage_pct`. Cost is over ALL turns; perf is
            # over the cascade-span subset (~49% coverage), so the two never get
            # conflated — perf rides as a sibling block the UI badges separately.
            # Wrapped so any span-reader hiccup (EACCES, malformed file) degrades
            # to "no perf data" and NEVER breaks the v1.5 cost payload.
            _attach_perf_block(
                payload, summary, shared, days, network,
                pricing_cache, listings_cache,
            )
            if any(v not in (None, "", "all") for v in (provider, band, audience)):
                payload = filter_economics(
                    payload, network=network,
                    provider=provider, band=band, audience=audience,
                )
            payload["days"] = days
            payload["filters"] = {
                "provider": provider if provider not in (None, "", "all") else None,
                "band": band if band not in (None, "", "all") else None,
                "bot": bot_id,
                "audience": normalize_audience(audience),
            }
            return jsonify(payload)
        except Exception as e:
            return error_response(e)

    @app.get("/api/analytics/usage/export")
    def api_analytics_usage_export():
        """Export turn records as CSV."""
        import io as _io, csv as _csv
        from usage_analytics import load_turns

        bot  = request.args.get("bot")
        days = min(int(request.args.get("days", 30)), 365)
        bot_id = None if bot in (None, "", "all") else bot

        try:
            turns = load_turns(bot_id, days=days, network_path=str(network_path))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

        turns.sort(key=lambda t: t.get("ts", ""))
        fields = ["ts", "instance", "channel", "user_id", "model",
                  "source", "auth_mode", "cost", "input_tokens", "output_tokens",
                  "cache_read_tokens", "cache_write_tokens", "session_id"]
        output = _io.StringIO()
        writer = _csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(turns)
        fname = f"turns-{bot or 'all'}-{days}d.csv"
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={fname}"},
        )

    # ── Analytics: usage by API key ──────────────────────────────
    @app.get("/api/analytics/usage/keys")
    def api_analytics_usage_keys():
        """
        List all registered API keys with 7-day cost summary.

        Returns each key from the keystore registry with:
          - metadata (provider, scope, description, assigned bots)
          - total_cost and total_turns over the last 7 days
          - last_synced timestamp per bot (null if never synced)

        No query parameters.
        """
        try:
            from usage_analytics import load_turns, compute_summary
            from cost import Keystore as _Keystore
        except ImportError as _e:
            return jsonify({"error": f"analyzer import failed: {_e}"}), 500

        try:
            shared = _shared()
            all_bots = _members()
            ks = _Keystore(shared)
            registry = ks.load_registry()
            sync_log = ks.load_sync_log()

            result = []
            for key_name, key_meta in registry.get("keys", {}).items():
                provider = key_meta.get("provider", "unknown")
                scope_bots = key_meta.get("bots")          # None = all bots
                assigned = scope_bots if scope_bots is not None else all_bots

                all_turns: list[dict] = []
                bot_errors: dict[str, str] = {}
                for bot_id in assigned:
                    try:
                        turns = load_turns(
                            bot_id,
                            days=7,
                            network_path=str(network_path),
                        )
                        all_turns.extend(
                            t for t in turns
                            if (t.get("model") or "").startswith(f"{provider}/")
                        )
                    except Exception as exc:
                        bot_errors[bot_id] = str(exc)

                summary = compute_summary(all_turns)
                entry: dict = {
                    "key_name": key_name,
                    "provider": provider,
                    "scope": key_meta.get("scope"),
                    "description": key_meta.get("description", ""),
                    "assigned_bots": assigned,
                    "last_rotated": key_meta.get("last_rotated"),
                    "registered_at": key_meta.get("registered_at"),
                    # null for bots that exist but have never been synced
                    "last_synced_by_bot": {b: sync_log.get(b) for b in assigned},
                    "total_turns": summary["total_turns"],
                    "total_cost": summary["total_cost"],
                }
                if bot_errors:
                    entry["bot_errors"] = bot_errors
                result.append(entry)

            result.sort(key=lambda k: k["total_cost"], reverse=True)
            return jsonify({"keys": result})
        except Exception as e:
            return error_response(e)

    @app.get("/api/analytics/usage/by-key")
    def api_analytics_usage_by_key():
        """
        Full usage summary for a single API key across all bots currently
        assigned to it.

        ?key=<key_name>             (required — name from keystore registry)
        ?days=7|30|90               (default: 7, max: 365)
        ?channel=telegram           (optional filter)
        ?source=human|cron|subagent (optional filter)

        Attribution: turns are included when the bot is currently assigned
        this key in the keystore AND the turn's model provider matches.
        Note: key rotation history is not stored, so if a key was rotated
        historical turns all attribute to the current key assignment.
        """
        try:
            from usage_analytics import load_turns, compute_summary
            from cost import Keystore as _Keystore
        except ImportError as _e:
            return jsonify({"error": f"analyzer import failed: {_e}"}), 500

        key_name = request.args.get("key", "").strip()
        try:
            days = max(1, min(int(request.args.get("days", 7)), 365))
        except (ValueError, TypeError):
            return jsonify({"error": "days must be an integer between 1 and 365"}), 400
        channel = request.args.get("channel") or None
        source = request.args.get("source") or None

        if not key_name:
            return jsonify({"error": "key parameter is required"}), 400

        try:
            shared = _shared()
            all_bots = _members()
            ks = _Keystore(shared)
            key_meta = ks.get_key_entry(key_name)
            if key_meta is None:
                return jsonify({"error": f"key '{key_name}' not found in keystore"}), 404

            provider = key_meta.get("provider", "unknown")
            scope_bots = key_meta.get("bots")          # None = all bots
            assigned = scope_bots if scope_bots is not None else all_bots

            all_turns: list[dict] = []
            bot_errors: dict[str, str] = {}
            for bot_id in assigned:
                try:
                    turns = load_turns(
                        bot_id,
                        days=days,
                        channel_filter=channel,
                        source_filter=source,
                        network_path=str(network_path),
                    )
                    all_turns.extend(
                        t for t in turns
                        if (t.get("model") or "").startswith(f"{provider}/")
                    )
                except Exception as exc:
                    bot_errors[bot_id] = str(exc)

            summary = compute_summary(all_turns)

            # Per-bot breakdown (in addition to the standard by_model / by_date)
            by_bot: dict[str, dict] = {}
            for t in all_turns:
                bot = t.get("instance") or "unknown"
                if bot not in by_bot:
                    by_bot[bot] = {"bot": bot, "turns": 0, "cost": 0.0}
                by_bot[bot]["turns"] += 1
                by_bot[bot]["cost"] += float(t.get("cost") or 0)

            summary.update({
                "key_name": key_name,
                "provider": provider,
                "scope": key_meta.get("scope"),
                "description": key_meta.get("description", ""),
                "assigned_bots": assigned,
                "last_rotated": key_meta.get("last_rotated"),
                "by_bot": sorted(by_bot.values(), key=lambda x: x["cost"], reverse=True),
                "attribution_note": (
                    "Turns attributed by current key assignment + provider match. "
                    "Key rotation history is not tracked."
                ),
            })
            if bot_errors:
                summary["bot_errors"] = bot_errors

            return jsonify(summary)
        except Exception as e:
            return error_response(e)

    # ── Analytics: context bloat ─────────────────────────────────
    @app.get("/api/analytics/bloat")
    def api_analytics_bloat():
        """
        Detect sessions with disproportionately large contexts.
        Uses session_monitor.compute_session_stats() for JSONL parsing; applies
        class-aware thresholds against max_input_tokens (peak single-turn context).
        """
        sm = _import_analyzer("session_monitor")
        compute_session_stats = sm.compute_session_stats

        shared = _shared()
        bots = _oc_members()
        result = {}

        _THRESHOLDS = [
            # (session_class_or_None, min_tokens, severity)
            (None,            500_000, "critical"),
            ("maintenance",   100_000, "critical"),
            ("maintenance",    50_000, "warning"),
            ("ambiguous",     150_000, "warning"),
        ]

        for bot in bots:
            all_sessions = compute_session_stats(shared, bot, days=30)
            if not all_sessions:
                result[bot] = {
                    "bloated_sessions": [],
                    "total_sessions_checked": 0,
                    "bloat_rate": 0,
                    "note": "No data yet — annotations accumulating",
                }
                continue

            bloated = []
            for s in all_sessions:
                max_t = s.get("max_input_tokens", 0)
                sc = s.get("session_class") or "ambiguous"
                severity = None
                for cls, threshold, sev in _THRESHOLDS:
                    if cls is not None and cls != sc:
                        continue
                    if max_t > threshold:
                        severity = sev
                        break
                if severity:
                    bloated.append({
                        "session_id": s["session_id"],
                        "session_type": sc,
                        "max_context_tokens": max_t,
                        "total_input_tokens": s.get("total_input_tokens", 0),
                        "turn_count": s.get("turn_count", 0),
                        "total_cost": s.get("total_cost", 0.0),
                        "severity": severity,
                        "recommendation": "Past sessions can't be retroactively compacted — set compaction.mode='safeguard' on Cost Measures to prevent future bloat",
                    })

            total_checked = len(all_sessions)
            bloated.sort(key=lambda x: -x["max_context_tokens"])
            entry: dict = {
                "bloated_sessions": bloated,
                "total_sessions_checked": total_checked,
                "bloat_rate": round(len(bloated) / total_checked, 4) if total_checked else 0,
            }
            if total_checked == 0:
                entry["note"] = "No data yet — annotations accumulating"
            result[bot] = entry

        return jsonify(result)

    # ── Modules API ─────────────────────────────────────────────
    @app.get("/api/modules")
    def api_modules_get():
        from evolve_config import get_modules
        cfg = load_network(network_path)
        return jsonify(get_modules(cfg))

    @app.post("/api/modules/<module>/enable")
    def api_module_enable(module: str):
        from evolve_config import set_module_enabled
        set_module_enabled(network_path, module, True)
        return jsonify({"ok": True, "module": module, "enabled": True})

    @app.post("/api/modules/<module>/disable")
    def api_module_disable(module: str):
        from evolve_config import set_module_enabled
        set_module_enabled(network_path, module, False)
        return jsonify({"ok": True, "module": module, "enabled": False})

    @app.post("/api/modules/<module>/tune")
    def api_module_tune(module: str):
        from evolve_config import set_module_setting
        body = request.get_json() or {}
        key = body.get("key")
        value = body.get("value")
        if not key:
            return jsonify({"error": "key required"}), 400
        set_module_setting(network_path, module, key, value)
        return jsonify({"ok": True, "module": module, "key": key, "value": value})

    @app.post("/api/modules/detector/<name>/enable")
    def api_detector_enable(name: str):
        from evolve_config import set_detector_enabled
        set_detector_enabled(network_path, name, True)
        return jsonify({"ok": True, "detector": name, "enabled": True})

    @app.post("/api/modules/detector/<name>/disable")
    def api_detector_disable(name: str):
        from evolve_config import set_detector_enabled
        set_detector_enabled(network_path, name, False)
        return jsonify({"ok": True, "detector": name, "enabled": False})

    # ── Per-bot Continuity Engine opt-out ──────────────────────────────────
    # The pod-wide module stays "always-on" (noToggle in the Modules tab);
    # this exposes a per-bot off switch for bots that shouldn't make
    # time-deferred promises (read-only bots, security bots).
    def _bot_exists(bot_id: str) -> bool:
        net = load_network(network_path)
        # The primary bot always "exists" even if it isn't enumerated under
        # bots/members on some pod shapes. Resolve it via primary_bot_id rather
        # than hardcoding "evolve" — on an "evo"-primary pod the literal would
        # both wrongly admit a nonexistent "evolve" and wrongly 404 "evo".
        from primary_bot import primary_bot_id as _pbid  # type: ignore
        if bot_id == _pbid(net):
            return True
        return bot_id in (net.get("bots") or {}) or bot_id in (net.get("members") or [])

    @app.post("/api/bots/<bot_id>/continuity-engine/enable")
    def api_bot_continuity_engine_enable(bot_id: str):
        if not _bot_exists(bot_id):
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404
        from evolve_config import set_bot_module_enabled
        set_bot_module_enabled(network_path, bot_id, "continuity_engine", True)
        return jsonify({"ok": True, "botId": bot_id, "module": "continuity_engine", "enabled": True})

    @app.post("/api/bots/<bot_id>/continuity-engine/disable")
    def api_bot_continuity_engine_disable(bot_id: str):
        if not _bot_exists(bot_id):
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404
        from evolve_config import set_bot_module_enabled
        set_bot_module_enabled(network_path, bot_id, "continuity_engine", False)
        return jsonify({"ok": True, "botId": bot_id, "module": "continuity_engine", "enabled": False})

