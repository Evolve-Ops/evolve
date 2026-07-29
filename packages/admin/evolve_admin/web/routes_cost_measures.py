"""HTTP routes for the Cost Measures admin pillar.

GET  /api/bot/openclaw-cost            cost-relevant openclaw.json settings per bot
PATCH /api/bot/openclaw-cost           partial-update cost settings per bot
GET  /api/bot/cost-efficiency-score    per-bot cost efficiency composite score
GET  /api/bot/app-bootstrap-footprint  per-app bootstrap-footprint breakdown
GET  /api/cost-optimization/tiles      tile-row data for the Cost Optimization page
GET  /api/cost-profiles                list all cost profiles (built-in + custom)
POST /api/cost-profiles                save current bot settings as a named custom profile
DELETE /api/cost-profiles/<name>       delete a custom profile
PATCH /api/bot/apply-profile           apply a named profile to a single bot
POST /api/bots/apply-profile           apply a named profile to ALL bots
GET  /api/cost-measures/caps           spending caps config + active enforcement flags
POST /api/cost-measures/caps           update spending caps
GET  /api/cost-measures/caps/status    active enforcement flags
GET  /api/cost-measures/runaway        runaway session monitor results
POST /api/cost-measures/runaway/reset  reset a runaway session flag
GET  /api/analytics/turns              top cost turns for the Turn Audit table
GET  /api/analytics/turns/<turn_id>    per-turn drilldown for the Turn Audit drawer
GET  /api/cost-measures/forensics/spikes        budget spike forensics
GET  /api/cost-measures/forensics/trigger-rollup trigger-kind cost rollup
GET  /api/cost-measures/forensics/app-rollup    per-application cost rollup
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request, Response

from ..config import load_network, bot_home as _bot_home
from ..telemetry import get_logger

_log = get_logger("web.routes_cost_measures")

# ── Analyzer import helper (lazy import — keeps laziness + avoids cycles) ──

def _import_analyzer(mod: str):
    import importlib
    return importlib.import_module(mod)

def register_cost_measures_routes(app: Flask, network_path: Path) -> None:
    """Register /api/cost-measures/* routes for the Cost Measures admin pillar.

    Covers:
      - OpenClaw cost settings read/write per bot
      - Cost efficiency score per bot
      - Cost profile templates (built-in + custom)
      - Spending caps config + active enforcement flags
      - Runaway session monitor
      - Turn cost audit
    """

    def _shared() -> Path:
        cfg = load_network(network_path)
        return Path(cfg.get("sharedDir", "/Users/Shared/evolve"))

    def _members() -> list[str]:
        cfg = load_network(network_path)
        return cfg.get("members", [])

    # ── OpenClaw cost settings ─────────────────────────��──────────

    @app.get("/api/bot/openclaw-cost")
    def api_bot_openclaw_cost_get():
        """Return cost-relevant settings for a bot.

        ?bot=admin_bot  (defaults to primary bot)

        Combines two storage tiers into a single response so the Context
        & Session matrix on Cost Optimization can render every row from
        one fetch:
          - openclaw.json fields: heartbeat, contextPruning, compaction,
            bootstrapTotalMaxChars, session.reset.idleMinutes
          - BE-config field: cache_retention (Anthropic prompt-cache TTL)
        """
        bot_id = request.args.get("bot")
        network = load_network(network_path)
        if not bot_id:
            primary = network.get("primary", "")
            members = network.get("members", [])
            bot_id = primary or (members[0] if members else None)
        if not bot_id:
            return jsonify({"error": "No bot specified"}), 400

        try:
            cp = _import_analyzer("cost_profiles")
        except ImportError as e:
            return jsonify({"error": f"cost_profiles module unavailable: {e}"}), 500

        settings = cp.read_openclaw_cost_settings(bot_id)
        if settings is None:
            return jsonify({"error": f"Could not read openclaw.json for {bot_id}"}), 404

        # Merge cache_retention from BE config. Best-effort: a BE config
        # failure leaves the field as None — UI shows "use OC default."
        try:
            from better_engine_config import load as _be_load  # type: ignore
            be = _be_load(_shared())
            settings["cache_retention"] = be.per_bot_cache_retention(bot_id)
        except Exception:
            settings["cache_retention"] = None

        return jsonify({
            "bot_id": bot_id,
            "source": "openclaw.json+be-config",
            "settings": settings,
        })

    @app.patch("/api/bot/openclaw-cost")
    def api_bot_openclaw_cost_patch():
        """Partial-update cost settings for a bot.

        ?bot=admin_bot
        Body: { heartbeat: {...}, contextPruning: {...}, cache_retention: "long", ... }

        cache_retention is routed to BE config (not openclaw.json); all
        other top-level fields go through write_openclaw_cost_settings.
        """
        bot_id = request.args.get("bot")
        network = load_network(network_path)
        if not bot_id:
            primary = network.get("primary", "")
            members = network.get("members", [])
            bot_id = primary or (members[0] if members else None)
        if not bot_id:
            return jsonify({"error": "No bot specified"}), 400

        body = request.get_json()
        if not body:
            return jsonify({"error": "JSON body required"}), 400

        try:
            cp = _import_analyzer("cost_profiles")
        except ImportError as e:
            return jsonify({"error": f"cost_profiles module unavailable: {e}"}), 500

        # Split BE-config fields out of the payload before the openclaw.json
        # write. Mirror cost_profiles.BE_CONFIG_PROFILE_FIELDS so both write
        # paths use the same routing rule.
        be_fields = set(getattr(cp, "BE_CONFIG_PROFILE_FIELDS", ("cache_retention",)))
        oc_body = {k: v for k, v in body.items() if k not in be_fields}
        be_body = {k: v for k, v in body.items() if k in be_fields}

        # ?force=1 skips the preflight gate (see
        # cost_profiles.preflight_heartbeat_combination). Any forced write
        # emits a `cost_setting_forced` Signal so we have a paper trail —
        # the PATCH endpoint does NOT audit-log, so this is the only
        # observable side-channel for the override.
        force_raw = (request.args.get("force") or "").strip().lower()
        force = force_raw in ("1", "true", "yes")

        if oc_body:
            ok, err = cp.write_openclaw_cost_settings(bot_id, oc_body, force=force)
            if not ok:
                # Preflight rejections are 400 (client should change the body
                # or pass ?force=1); other write failures stay 500.
                status = 400 if err and "no valid use case" in err else 500
                return jsonify({"ok": False, "error": err}), status
            if force:
                # Only emit the override Signal when the write WOULD have
                # tripped the gate without force. force=1 on a benign body
                # is a no-op — no need to alarm.
                hb_in_body = (oc_body or {}).get("heartbeat") or {}
                if hb_in_body.get("lightContext") is False:
                    every = hb_in_body.get("every")
                    every_secs = cp._parse_every_to_seconds(every)
                    if every_secs is not None and every_secs >= 60 * 60:
                        cp.emit_cost_setting_forced_signal(
                            bot_id, oc_body, _shared(),
                        )
            # Repair config integrity after external write — openclaw validates
            # the schema on every command and rejects configs with unrecognized
            # root-level keys (e.g. compaction, contextPruning). doctor --fix
            # migrates legacy keys and drops any genuinely unrecognized in the
            # current schema.
            from runtime.agent_runtime import get_runtime
            get_runtime().doctor_fix(bot_id)

        if be_body:
            try:
                from better_engine_config import load as _be_load, save as _be_save  # type: ignore
                be = _be_load(_shared())
                if "cache_retention" in be_body:
                    be.set_per_bot_cache_retention(bot_id, be_body["cache_retention"])
                _be_save(be, _shared())
            except Exception as exc:  # noqa: BLE001
                return jsonify({"ok": False, "error": f"BE config write failed: {exc}"}), 500

        # Re-read both tiers for the response so the client sees canonical
        # state without a follow-up GET.
        new_settings = cp.read_openclaw_cost_settings(bot_id) or {}
        if new_settings:
            cp.save_cost_snapshot(bot_id, new_settings, _shared())
        try:
            from better_engine_config import load as _be_load  # type: ignore
            be = _be_load(_shared())
            new_settings["cache_retention"] = be.per_bot_cache_retention(bot_id)
        except Exception:
            new_settings["cache_retention"] = None
        return jsonify({"ok": True, "bot_id": bot_id, "settings": new_settings})

    # ── Cost efficiency score ─────────────────────────────────────

    @app.get("/api/bot/cost-score")
    def api_bot_cost_score():
        """Return cost efficiency score (0-100) for a bot.
        ?bot=admin_bot&days=7
        """
        bot_id = request.args.get("bot")
        days = int(request.args.get("days", 7))
        network = load_network(network_path)
        if not bot_id:
            primary = network.get("primary", "")
            members = network.get("members", [])
            bot_id = primary or (members[0] if members else None)
        if not bot_id:
            return jsonify({"error": "No bot specified"}), 400

        try:
            cp = _import_analyzer("cost_profiles")
            sm = _import_analyzer("session_monitor")
        except ImportError as e:
            return jsonify({"error": f"analyzer modules unavailable: {e}"}), 500

        settings = cp.read_openclaw_cost_settings(bot_id)
        shared = _shared()
        score = sm.compute_cost_score(shared, bot_id, settings, days=days)
        return jsonify(score)

    # ── App bootstrap footprint chip ──────────────────────────────
    # Per-bot measurement of how much each installed app contributes
    # to per-turn system-prompt injection. Backs both the chip on the
    # bot detail page and the four `app_*` verifier checks. Info-only
    # during calibration — thresholds are reported, not enforced.
    # See principle-apps-minimize-bootstrap-cost.md.

    @app.get("/api/bot/app-bootstrap-footprint")
    def api_bot_app_bootstrap_footprint():
        """Return per-app + per-bot bootstrap-footprint breakdown.

        ?bot=<id>  (defaults to primary / first member)

        Always returns a 200 with the same shape; an unreachable
        manifests dir surfaces as `error: "..."` on the body so the
        chip can render "no data" instead of disappearing.
        """
        bot_id = request.args.get("bot")
        network = load_network(network_path)
        if not bot_id:
            primary = network.get("primary", "")
            members = network.get("members", [])
            bot_id = primary or (members[0] if members else None)
        if not bot_id:
            return jsonify({"error": "No bot specified"}), 400

        try:
            fp = _import_analyzer("app_bootstrap_footprint")
        except ImportError as e:
            return jsonify({"error": f"analyzer module unavailable: {e}"}), 500

        return jsonify(fp.compute_app_bootstrap_footprint(bot_id))

    # ── Cost Optimization tile row ────────────────────────────────
    # Per-bot tile data for the top of the Cost Optimization page.
    # Composes existing computations (cost_score, tile_metrics chips,
    # cost rollups, breakers / proposals / signals state) into a
    # single tile shape: grade, spend, model mix, chip row. Mirrors
    # the Usage tile pattern but anchored on optimization posture
    # rather than raw spend.

    @app.get("/api/cost-optimization/tiles")
    def api_cost_optimization_tiles():
        """Return tile-row data for the Cost Optimization page.

        Response::

          {
            "tiles": [
              {
                "bot_id": "admin_bot",
                "grade": "A", "score": 93,
                "config_score": 60, "config_max": 60,
                "behavior_score": 33, "behavior_max": 40,
                "spend": {"usd_28d": 87.93, "usd_prior_28d": 1289.13,
                          "delta_pct_28d": -93.2},
                "model_mix": {
                  "total_turns": 4321, "total_cost": 87.93,
                  "by_tier": [
                    {"tier": "tier3", "turns": 3400, "cost": 12.0,
                     "turn_share": 0.79, "cost_share": 0.14,
                     "dominant_model": "anthropic/claude-haiku-4-5"},
                    ...
                  ]
                },
                "chips": [
                  {"id": "cost_spike", "severity": "warn",
                   "label": "cost spike", "detail": "..."},
                  {"id": "cascade_live", "severity": "info",
                   "label": "cascade", "detail": "treatment group ..."}
                ]
              },
              ...
            ]
          }

        Bot order matches network.json's bots dict iteration order so
        the row stays stable across reloads. Filtering / sorting is the
        UI's job.
        """
        try:
            cot = _import_analyzer("cost_opt_tiles")
            cp = _import_analyzer("cost_profiles")
        except ImportError as e:
            return jsonify({"error": f"analyzer modules unavailable: {e}"}), 500

        network = load_network(network_path)
        bots_cfg = network.get("bots") or {}
        if not bots_cfg:
            bot_ids = network.get("members") or []
            bots_cfg = {bid: {"role": "member"} for bid in bot_ids}
        bot_ids = list(bots_cfg.keys())

        def _bot_data_for(bid: str) -> dict:
            spec = bots_cfg.get(bid) or {}
            return {
                "role": spec.get("role") or "member",
                "live_data": {},  # tiles run offline; no gateway probe data
            }

        def _settings_for(bid: str) -> dict:
            try:
                return cp.read_openclaw_cost_settings(bid) or {}
            except Exception:
                return {}

        try:
            tiles = cot.build_all_tiles(
                shared_dir=_shared(),
                bot_ids=bot_ids,
                network=network,
                bot_data_resolver=_bot_data_for,
                settings_resolver=_settings_for,
            )
        except Exception as e:
            return jsonify({
                "error": f"tile assembly failed: {type(e).__name__}: {e}",
            }), 500

        return jsonify({"tiles": tiles})

    # ── Cost profiles ──────────────────────────────────────────────

    @app.get("/api/cost-profiles")
    def api_cost_profiles_list():
        """List all cost profiles (built-in + custom)."""
        try:
            cp = _import_analyzer("cost_profiles")
        except ImportError as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"profiles": cp.list_profiles(_shared())})

    @app.post("/api/cost-profiles")
    def api_cost_profiles_save():
        """Save current bot settings as a named custom profile.
        Body: { name: "my-profile", bot_id: "admin_bot" }
        """
        body = request.get_json() or {}
        name = body.get("name", "").strip()
        bot_id = body.get("bot_id")
        if not name:
            return jsonify({"error": "name required"}), 400

        try:
            cp = _import_analyzer("cost_profiles")
        except ImportError as e:
            return jsonify({"error": str(e)}), 500

        settings = cp.read_openclaw_cost_settings(bot_id) if bot_id else {}
        profile = cp.save_as_profile(name, settings or {}, _shared())
        return jsonify({"ok": True, "profile": profile})

    @app.delete("/api/cost-profiles/<name>")
    def api_cost_profiles_delete(name: str):
        """Delete a custom profile (cannot delete built-ins)."""
        try:
            cp = _import_analyzer("cost_profiles")
        except ImportError as e:
            return jsonify({"error": str(e)}), 500
        deleted = cp.delete_custom_profile(name, _shared())
        if not deleted:
            return jsonify({"error": f"Profile '{name}' not found or is a built-in"}), 404
        return jsonify({"ok": True, "deleted": name})

    @app.patch("/api/bot/apply-profile")
    def api_bot_apply_profile():
        """Apply a named profile to a single bot.
        ?bot=admin_bot  Body: { profile: "conservative" }
        """
        bot_id = request.args.get("bot")
        body = request.get_json() or {}
        profile_name = body.get("profile", "").strip()
        if not bot_id or not profile_name:
            return jsonify({"error": "bot and profile required"}), 400

        try:
            cp = _import_analyzer("cost_profiles")
        except ImportError as e:
            return jsonify({"error": str(e)}), 500

        result = cp.apply_profile_to_bot(profile_name, bot_id, _shared())
        status = 200 if result["ok"] else 500
        return jsonify(result), status

    @app.post("/api/bots/apply-profile")
    def api_bots_apply_profile_all():
        """Apply a named profile to ALL bots in the pod.
        Body: { profile: "conservative" }
        """
        body = request.get_json() or {}
        profile_name = body.get("profile", "").strip()
        if not profile_name:
            return jsonify({"error": "profile required"}), 400

        try:
            cp = _import_analyzer("cost_profiles")
        except ImportError as e:
            return jsonify({"error": str(e)}), 500

        members = _members()
        results = [cp.apply_profile_to_bot(profile_name, bot_id, _shared()) for bot_id in members]
        failed = [r for r in results if not r["ok"]]
        return jsonify({
            "profile": profile_name,
            "applied_to": [r["bot_id"] for r in results if r["ok"]],
            "failed": [{"bot_id": r["bot_id"], "error": r["error"]} for r in failed],
        })

    # ── Spending caps ─────────────────────────────────────────────

    @app.get("/api/spend-caps")
    def api_spend_caps_get():
        """Return spending caps config + active enforcement + warning state.

        Response shape:
          config                       — daily/weekly thresholds + caps + action
          active_enforcement           — {bot: flag} for bots currently enforced
          cap_warnings                 — {bot: {spend, cap, pct}} for bots at
                                         >=80% of daily cap but not yet enforced
          recent_enforcement_history   — {bot: trip_count} for bots that tripped
                                         a cap in the last 7d (excluding the
                                         active-today set, which appears in
                                         active_enforcement)
          active_breakers              — {bot: {tripped_at, reason}} for bots
                                         whose L1 cost breaker is currently up
                                         (read from {shared}/breakers/<bot>/cost.json).
                                         A breaker can be tripped without a
                                         spend-cap enforcement flag (e.g. bloat
                                         detector, manual trip), and vice versa,
                                         so the summary band consumes both.
        """
        try:
            sc = _import_analyzer("spend_caps")
        except ImportError as e:
            return jsonify({"error": str(e)}), 500

        network = load_network(network_path)
        shared = _shared()
        config = sc.get_caps_config(network, shared)
        members = _members()
        active = sc.get_all_active_enforcement(shared, members)
        cap_warnings = sc.get_cap_warnings(shared, members, config.get("dailySpendCapUsd"))
        recent_history = sc.get_recent_enforcement_history(shared, members)
        active_breakers = sc.get_active_breakers(shared, members)
        return jsonify({
            "config": config,
            "active_enforcement": active,
            "cap_warnings": cap_warnings,
            "recent_enforcement_history": recent_history,
            "active_breakers": active_breakers,
        })

    @app.patch("/api/spend-caps")
    def api_spend_caps_patch():
        """Update spending cap thresholds and enforcement action.

        Post-Phase-8 of the 2026-06 cost-cap normalization: writes to
        better-engine-config pod_defaults (the canonical store) and ALSO
        mirrors to network.json::thresholds for back-compat. The next
        cost_caps_normalize migration pass strips the network.json
        mirror after confirming BE config has the value.

        New code should prefer ``POST /api/arbiter/pod-defaults`` which
        speaks the spec field names directly.

        Body: { dailySpendCapUsd: 15.0, spendCapAction: "downgrade-tier", ... }
        """
        body = request.get_json() or {}
        try:
            sc = _import_analyzer("spend_caps")
        except ImportError as e:
            return jsonify({"error": str(e)}), 500

        # Mirror to BE config pod_defaults — spec-name remapping.
        try:
            from better_engine_config import (  # type: ignore
                load as load_be_config,
                save as save_be_config,
            )
            shared_dir = _shared()
            be = load_be_config(shared_dir)
            pod_budget = be.pod_defaults.setdefault("budget", {})
            if "dailySpendAlertUsd" in body and body["dailySpendAlertUsd"] is not None:
                pod_budget["per_bot_daily_warn_usd"] = float(body["dailySpendAlertUsd"])
            if "dailySpendCapUsd" in body:
                if body["dailySpendCapUsd"] is None:
                    pod_budget.pop("per_bot_daily_hard_usd", None)
                else:
                    pod_budget["per_bot_daily_hard_usd"] = float(body["dailySpendCapUsd"])
            if "weeklySpendAlertUsd" in body:
                if body["weeklySpendAlertUsd"] is None:
                    pod_budget.pop("pod_weekly_warn_usd", None)
                else:
                    pod_budget["pod_weekly_warn_usd"] = float(body["weeklySpendAlertUsd"])
            action = body.get("spendCapAction")
            hard = pod_budget.get("per_bot_daily_hard_usd")
            if action == "downgrade-tier" and isinstance(hard, (int, float)):
                pod_budget["tier_downgrade_usd"] = float(hard)
            elif action == "suspend-bot" and isinstance(hard, (int, float)):
                pod_budget["l2_breaker_usd"] = float(hard)
            save_be_config(be, shared_dir)
        except Exception as exc:
            # Mirror failures don't block the legacy write — best-effort.
            _log.warning("spend-caps PATCH: BE config mirror failed: %s", exc)

        try:
            sc.save_caps_config(network_path, body)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

        network = load_network(network_path)
        return jsonify({"ok": True, "config": sc.get_caps_config(network)})

    @app.post("/api/spend-caps/<bot_id>/clear")
    def api_spend_caps_clear(bot_id: str):
        """Manually clear an active enforcement flag for a bot."""
        try:
            sc = _import_analyzer("spend_caps")
        except ImportError as e:
            return jsonify({"error": str(e)}), 500

        cleared = sc.clear_enforcement(_shared(), bot_id)
        if not cleared:
            return jsonify({"ok": False, "message": f"No active enforcement found for {bot_id}"}), 404
        return jsonify({"ok": True, "bot_id": bot_id, "cleared": True})

    # ── Runaway session monitor ───────────────────────────────────

    @app.get("/api/analytics/sessions/runaway")
    def api_sessions_runaway():
        """Detect sessions with abnormally large context token totals.
        ?bot=admin_bot&days=7&threshold=100000
        """
        bot_id = request.args.get("bot")
        days = int(request.args.get("days", 7))
        threshold = int(request.args.get("threshold", 100_000))
        network = load_network(network_path)
        threshold_from_cfg = network.get("thresholds", {}).get("maxSessionContextTokens", 100_000)
        threshold = threshold or threshold_from_cfg

        try:
            sm = _import_analyzer("session_monitor")
        except ImportError as e:
            return jsonify({"error": str(e)}), 500

        shared = _shared()
        bots = [bot_id] if bot_id else _members()
        result = {}
        for bid in bots:
            result[bid] = sm.detect_runaway_sessions(shared, bid, threshold, days)
        return jsonify(result)

    # ── Turn cost audit ────────────────────��──────────────────────

    @app.get("/api/analytics/turns/audit")
    def api_turns_audit():
        """Return turns across all bots, sorted by ts (default) or cost.
        ?bot=all&days=7&limit=25&sort=ts|cost
        """
        bot_id = request.args.get("bot")
        days = int(request.args.get("days", 7))
        limit = min(int(request.args.get("limit", 25)), 100)
        sort_by = request.args.get("sort", "ts")
        if sort_by not in ("ts", "cost"):
            sort_by = "ts"

        try:
            sm = _import_analyzer("session_monitor")
        except ImportError as e:
            return jsonify({"error": str(e)}), 500

        shared = _shared()
        bots = [bot_id] if bot_id and bot_id != "all" else _members()
        turns = sm.get_top_cost_turns(shared, bots, days=days, limit=limit, sort_by=sort_by)
        return jsonify({"turns": turns, "total": len(turns), "days": days, "sort": sort_by})

    @app.get("/api/analytics/turns/<turn_id>")
    def api_turn_detail(turn_id: str):
        """Per-turn drilldown for the Turn Audit drawer.

        ?bot=<bot_id> is required — turn_id alone isn't unique across bots.
        Returns the full turn_annotation, joined cost_event records, the
        turns-{date}.jsonl row (source/channel), and a best-effort OC
        transcript slice. See packages/analyzer/turn_detail.py for the
        payload shape.
        """
        bot_id = request.args.get("bot", "").strip()
        if not bot_id:
            return jsonify({"error": "bot query param required"}), 400
        members = set(_members())
        from primary_bot import primary_bot_id as _primary_bot_id  # type: ignore
        # Resolve the primary directly; an unresolved primary (None) is a real
        # misconfig and must not be papered over with the literal "evolve",
        # which would wrongly 404 the primary on an "evo"-primary pod.
        _primary_id = _primary_bot_id(load_network(network_path))
        if bot_id not in members and bot_id != _primary_id:
            return jsonify({"error": f"unknown bot {bot_id!r}"}), 404

        try:
            td = _import_analyzer("turn_detail")
        except ImportError as e:
            return jsonify({"error": str(e)}), 500

        try:
            bot_home = _bot_home(bot_id)
        except Exception:
            bot_home = None

        result = td.get_turn_detail(_shared(), bot_id, turn_id, bot_home=bot_home)
        if "error" in result:
            return jsonify(result), 404
        return jsonify(result)

    # ── Budget Hawk v2 forensics ───────────────────────────────────
    # Backed by cost_event records in the annotations JSONL. One record per
    # billable LLM call, with trigger_kind + cache_state lineage so the UI
    # can answer "what did that cost, and why?" instead of just "how much?".

    @app.get("/api/cost-measures/forensics/spikes")
    def api_forensics_spikes():
        """Top N single cost events (by cost_usd desc) across bots.

        Query params:
            bot:    specific bot id, or "all" (default)
            days:   lookback window (default 7, max 30)
            limit:  max rows returned (default 20, max 100)

        Each row is the full cost_event record — trigger_kind, cache_state,
        session_id, model, tokens, cost_usd — so the UI can render without
        a second round-trip.
        """
        bot_arg = request.args.get("bot", "all")
        days = max(1, min(int(request.args.get("days", 7)), 30))
        limit = max(1, min(int(request.args.get("limit", 20)), 100))

        try:
            ledger = _import_analyzer("cost_ledger")
        except ImportError as e:
            return jsonify({"error": f"cost_ledger unavailable: {e}"}), 500

        shared = _shared()
        if bot_arg == "all":
            bots = ledger.discover_bot_ids(shared) or _members()
        else:
            bots = [bot_arg]

        # Pull events from each bot, then take the global top-N. Fine for
        # typical volumes (thousands of events per week per bot); if this
        # gets hot we switch to a streaming heapq merge.
        all_events: list[dict] = []
        for bid in bots:
            try:
                all_events.extend(
                    list(ledger.read_events(bid, days=days, shared_dir=shared))
                )
            except Exception as exc:  # noqa: BLE001 — tolerant read path
                _log.warning("forensics.spikes: failed to read %s: %s", bid, exc)
                continue

        spikes = ledger.top_spikes(all_events, n=limit)
        return jsonify({
            "spikes": spikes,
            "total_events_scanned": len(all_events),
            "bots": bots,
            "days": days,
            "limit": limit,
        })

    @app.get("/api/cost-measures/forensics/trigger-rollup")
    def api_forensics_trigger_rollup():
        """Aggregate cost by trigger_kind across bots.

        Answers "how much of our spend is user turns vs heartbeats vs
        summarizer vs classifier?" — which is the single most useful number
        for diagnosing waste category.
        """
        bot_arg = request.args.get("bot", "all")
        days = max(1, min(int(request.args.get("days", 7)), 30))

        try:
            ledger = _import_analyzer("cost_ledger")
        except ImportError as e:
            return jsonify({"error": f"cost_ledger unavailable: {e}"}), 500

        shared = _shared()
        if bot_arg == "all":
            bots = ledger.discover_bot_ids(shared) or _members()
        else:
            bots = [bot_arg]

        per_bot: dict[str, dict] = {}
        combined: list[dict] = []
        for bid in bots:
            try:
                events = list(ledger.read_events(bid, days=days, shared_dir=shared))
            except Exception as exc:  # noqa: BLE001
                _log.warning("forensics.trigger-rollup: failed to read %s: %s", bid, exc)
                continue
            per_bot[bid] = ledger.rollup_per_trigger_kind(events)
            combined.extend(events)

        return jsonify({
            "combined": ledger.rollup_per_trigger_kind(combined),
            "per_bot": per_bot,
            "bots": bots,
            "days": days,
        })

    @app.get("/api/cost-measures/forensics/app-rollup")
    def api_forensics_app_rollup():
        """Aggregate cost by application tag across bots.

        Joins cost_event records to session_summary.applications_invoked
        (populated by SessionSummarizer's keyword detection + any
        deployment-specific patterns in network.json applicationPatterns).
        Costs are split evenly across apps a session invoked; sessions
        with no detected apps land under "(unattributed)" so operators
        can see how much of their spend isn't yet tagged.
        """
        bot_arg = request.args.get("bot", "all")
        days = max(1, min(int(request.args.get("days", 7)), 30))

        try:
            ledger = _import_analyzer("cost_ledger")
            access = _import_analyzer("observations.access")
        except ImportError as e:
            return jsonify({"error": f"cost_ledger / access unavailable: {e}"}), 500

        shared = _shared()
        if bot_arg == "all":
            bots = ledger.discover_bot_ids(shared) or _members()
        else:
            bots = [bot_arg]

        per_bot: dict[str, dict] = {}
        combined_events: list[dict] = []
        combined_summaries: list[dict] = []
        for bid in bots:
            try:
                events = list(
                    ledger.read_events(bid, days=days, shared_dir=shared)
                )
                # Pull session_summaries from the same window so the join works.
                summaries = list(
                    access.window(bid, days=days, shared_dir=shared)
                           .session_summaries()
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("forensics.app-rollup: failed to read %s: %s", bid, exc)
                continue
            per_bot[bid] = ledger.rollup_per_application(events, summaries)
            combined_events.extend(events)
            combined_summaries.extend(summaries)

        return jsonify({
            "combined": ledger.rollup_per_application(combined_events, combined_summaries),
            "per_bot": per_bot,
            "bots": bots,
            "days": days,
        })

