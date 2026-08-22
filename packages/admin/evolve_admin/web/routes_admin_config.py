"""HTTP routes for the model-config + catalog admin surface.

``/api/admin/config/*``  per-bot model config (catalog / tiers / tier-mode /
                         routing / fallback / cascade / user-tier-override),
                         plus the POD default tier-definitions editor and the
                         easy-setup wizard.
``/api/admin/models/*``  full per-bot catalog get/replace.
``/api/models/*``        model freshness (status / check / advisory dismiss-reset),
                         credentialed listings (read / refresh), free-text model
                         validation, and one-click / bulk tier updates.

Split out of ``routes_admin.py``'s ``register_admin_routes`` closure — 4.1b
Increment 1 (config + models), the cold/non-privileged surface — per the
strategy memo ``docs/design-routes-admin-decomposition-2026-06-12.md`` (Option
A: a sibling ``register_*_routes(app, network_path)`` module mirroring the ~11
that already exist; NOT Blueprints, NOT a ctx object). Pure code-motion: no
route added/removed/renamed/re-pathed/re-method-ed; no request/response shape
or status-code change.

§1.3 monkeypatch-at-call-time invariant (memo): handlers reach patchable
helpers through ``_module._NAME`` (``_module = sys.modules[
"evolve_admin.web.server"]``) at call time, so test monkeypatches on
``server._NAME`` are honored. The helpers this surface touches that way are
``server._audit_log_entry`` and ``server._resolve_bot_user`` — NOT imported as
module-level names here (that would shadow the patch).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from evolve_config import CANONICAL_SHARED_DIR  # type: ignore

from flask import Flask, jsonify, request, Response

from . import tier_write_guard as _tier_guard
from ..config import load_network
from ..telemetry import get_logger

_log = get_logger("web.routes_admin_config")


def register_admin_config_routes(app: Flask, network_path: Path) -> None:
    # Late-bound server-module handle (memo §1.3): handlers reach patchable
    # helpers via ``_module._NAME`` at call time so test monkeypatches on
    # ``server._NAME`` are respected. Derived inside the function (mirroring
    # ``routes_admin.register_admin_routes``) so importing this module never
    # requires ``server`` to be in ``sys.modules`` yet.
    _module = sys.modules["evolve_admin.web.server"]

    # 1-line late-bound shim shared by the config region (here) and the keys
    # region (still in routes_admin.py). The canonical logic is
    # ``server._resolve_bot_user``; this only threads this app's
    # ``network_path`` and stays late-bound through ``_module`` so test
    # monkeypatches of ``server._resolve_bot_user`` are respected. Recreated
    # verbatim (not imported) so the moved call sites stay byte-identical.
    def _resolve_user(bot_id: str) -> str:
        return _module._resolve_bot_user(bot_id, network_path)

    def _reject_unknown_bot(bot_id: object):
        """Return ``(jsonify, 400)`` if bot_id isn't registered in
        network.json::bots, else None. Cheap, fail-loud guard for the
        ``/api/admin/config/<bot_id>/...`` family: without it an unknown
        bot_id flows through to ``oc_full_config_set`` and crashes on
        ``sudo: unknown user <X>`` (seen 2026-06-03 when a batch enabled
        cascade on "forge", a feature name, not a bot).

        Also applied to the three routes that take ``bot_id`` from the request
        BODY, which relied solely on the oc_cli-side guard until #3566 audit
        C-2 (and that guard was fail-open on an unreadable roster). A
        body-derived bot_id need not be a string, so the type is checked here
        rather than at each call site."""
        if not isinstance(bot_id, str):
            return jsonify({
                "error": f"bot_id must be a string, got {type(bot_id).__name__}",
            }), 400
        try:
            known = (load_network(network_path).get("bots") or {})
        except Exception:
            known = {}
        if bot_id in known:
            return None
        return jsonify({
            "error": (
                f"unknown bot_id {bot_id!r}; not registered in "
                f"network.json::bots (known: {sorted(known.keys())})"
            ),
        }), 400

    @app.get("/api/admin/models/<bot_id>")
    def api_admin_get_models(bot_id: str) -> "Response | tuple[Response, int]":
        """Return {primary, fallback_order, catalog} for bot via oc_model_get (full openclaw.json read).
        Uses oc_model.py run as bot user — gets complete catalog from agents.defaults.models.
        """
        rej = _reject_unknown_bot(bot_id)
        if rej is not None:
            return rej
        try:
            from runtime.agent_runtime import get_runtime
            _rt = get_runtime()
            _oc_model_get_m = _rt.model_get
            result = _oc_model_get_m(bot_id) or {}
            return jsonify({
                "bot": bot_id,
                "primary": result.get("primary", ""),
                "fallback_order": result.get("fallback_order", []),
                "catalog": result.get("catalog", []),
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.put("/api/admin/models/<bot_id>")
    def api_admin_set_models(bot_id: str) -> "Response | tuple[Response, int]":
        """Full catalog replace (used by model management panel, not tier editor).

        ``oc_model_set`` — unlike ``oc_full_config_set_with_error`` — has no
        roster check of its own, so without the guard an unregistered bot_id
        reached ``sudo -H -u <bot_id> … oc_model.py models set …`` and this
        route answered 200 (#3566 audit C-2).
        """
        rej = _reject_unknown_bot(bot_id)
        if rej is not None:
            return rej
        body = request.get_json() or {}
        if not body.get("confirm"):
            return jsonify({"error": "confirm required"}), 400
        catalog = body.get("catalog", [])
        if not catalog:
            return jsonify({"error": "catalog required"}), 400
        from runtime.agent_runtime import get_runtime
        _rt = get_runtime()
        _oms_full = _rt.model_set
        # catalog[0] = primary, catalog[1:] = fallbacks
        ok = _oms_full(bot_id, catalog[0], catalog[1:])
        if not ok:
            return jsonify({"error": "oc_model_set failed — check server logs"}), 500
        _module._audit_log_entry("models.set", bot_id, {"catalog": catalog}, oc_keys={"agents"})
        return jsonify({"ok": True, "bot": bot_id, "primary": catalog[0], "fallbacks": catalog[1:]})

    # ── Full per-bot config (tiers + routing + fallback) ──────────────────────

    @app.get("/api/admin/config/<bot_id>")
    def api_admin_config_get(bot_id: str) -> Response:
        """Return full Evolve model config for a bot from its openclaw.json.

        Response keys: bot, primary, fallbacks, catalog, tiers, routing,
        fallbackMode, tierCascade, generatedFallbacks, keyStatus.
        """
        rej = _reject_unknown_bot(bot_id)
        if rej is not None:
            return rej
        from runtime.agent_runtime import get_runtime
        _rt = get_runtime()
        _cfg_get = _rt.full_config_get
        _omg = _rt.model_get
        _keys_get = _rt.keys_get
        config = _cfg_get(bot_id)
        if not config:
            # Graceful fallback: synthesize a minimal config from oc_model_get
            # so the UI renders something rather than an error page.
            basic = _omg(bot_id) or {}
            primary = basic.get("primary", "")
            fallback_order = basic.get("fallback_order", [])
            # Synthesize catalog: oc_model_get's "catalog" key is usually empty
            # because OpenClaw stores no dedicated catalog — primary+fallbacks IS the catalog.
            catalog = basic.get("catalog") or (
                ([primary] if primary else []) + fallback_order
            )
            config = {
                "bot": bot_id,
                "primary": primary,
                "fallbacks": fallback_order,
                "catalog": catalog,
                "tiers": {},
                "routing": {
                    "enabled": True,
                    "maintenanceTier": "tier3",
                    "backgroundTier": "tier3",
                    "ambiguousTier": None,
                    "confidenceThreshold": 0.65,
                },
                "fallbackMode": "static",
                "tierCascade": ["tier2", "tier3", "tier1"],
                "generatedFallbacks": fallback_order,
            }

        # Attach key status by provider (derived from catalog model IDs)
        key_status: dict = {}
        keys_data = _keys_get(bot_id) or {}
        for provider, pdata in (keys_data.get("keys") or {}).items():
            has_key = bool(pdata.get("api_key") or pdata.get("token"))
            key_status[provider] = has_key

        config["keyStatus"] = key_status

        # Per-bot role resolution view (spec §Addendum3.C): the merged
        # (defaults ← pod ← bot) resolution for THIS bot, tagged with rank /
        # rungCount / costClass / availability. Lets the per-bot Tier
        # Definitions panel render the rank meter, the Max row, and gray any
        # role that is unavailable on this bot. Best-effort — a resolution
        # failure must not break the config GET (the panel falls back to a
        # numeral-free static render).
        try:
            net = load_network(network_path)
            from primary_bot import resolve_roles_with_provenance  # type: ignore
            credentialed = _bot_providers_with_keys(bot_id)
            config["roles"] = resolve_roles_with_provenance(
                net, bot_id, credentialed_providers=credentialed,
            )
        except Exception as exc:
            _log.warning(
                "config GET: role resolution failed for %s, panel "
                "falls back to static render: %s", bot_id, exc,
            )
            config["roles"] = {}

        # Custom-vs-default state for the per-bot tier toggle (spec §Addendum 5).
        # Decided from the bot's RAW evolve-tiers.json (presence of a non-empty
        # ``rungs`` array), NEVER from the merged ``roles`` view above — the
        # merge always carries values from the code/pod defaults, so it can
        # never report "this bot defines nothing". Best-effort: a read failure
        # falls back to Use-defaults (the safe, non-editing state).
        try:
            net = load_network(network_path)
            from primary_bot import bot_has_custom_tiers  # type: ignore
            config["customTiers"] = bot_has_custom_tiers(net, bot_id)
        except Exception as exc:
            _log.warning(
                "config GET: custom-tier detection failed for %s, "
                "defaulting to use-defaults: %s", bot_id, exc,
            )
            config["customTiers"] = False

        return jsonify(config)

    @app.put("/api/admin/config/<bot_id>/catalog")
    def api_admin_config_set_catalog(bot_id: str) -> Response:
        """Replace the model catalog (agents.defaults.models) for a bot."""
        rej = _reject_unknown_bot(bot_id)
        if rej is not None:
            return rej
        body = request.get_json() or {}
        catalog = body.get("catalog")
        if catalog is None:
            return jsonify({"error": "catalog required"}), 400
        from runtime.agent_runtime import get_runtime
        _rt = get_runtime()
        _cfg_set = _rt.full_config_set
        result = _cfg_set(bot_id, {"catalog": catalog})
        if not result:
            return jsonify({"error": "write failed — check server logs"}), 500
        _module._audit_log_entry("config.catalog.set", bot_id, {"catalog": catalog}, oc_keys={"agents"})
        return jsonify({"ok": True, "bot": bot_id, "catalog": result.get("catalog", catalog)})

    @app.put("/api/admin/config/<bot_id>/tiers")
    def api_admin_config_set_tiers(bot_id: str) -> "Response | tuple[Response, int]":
        """Replace agents.defaults.model.tiers and recompute flat fallback list.

        Auto-reconciles the catalog first (a tier model absent from the catalog
        is silently dropped by OC at runtime — the team_bot_a drift, 2026-05-28);
        `catalog_auto_added` in the response reports what was added.
        """
        rej = _reject_unknown_bot(bot_id)
        if rej is not None:
            return rej
        body = request.get_json() or {}
        tiers = body.get("tiers")
        if tiers is None:
            return jsonify({"error": "tiers required"}), 400
        from ..model_catalog import reconcile_catalog, validate_tiers_shape
        from runtime.agent_runtime import get_runtime
        _rt = get_runtime()
        _cfg_get = _rt.full_config_get
        _cfg_set_err = _rt.full_config_set_with_error

        # Reconcile only auto-adds models for providers the bot is credentialed
        # for, so a missing key stays a graceful "silent drop" not a fatal
        # "no API key" (the trap Seed defaults avoids).
        current = _cfg_get(bot_id)
        current_catalog = (current or {}).get("catalog") or []
        # Shape-validate BEFORE anything is written (#3566 audit C-3), scoped to
        # the tiers this PUT MODIFIES (#3592) — whole-payload strictness also
        # rejects the SPA's own read-modify-write, since the GET echoes back
        # whatever junk is on disk. Baseline = the tiers GET handed the UI, and
        # None (a failed read) validates every tier strictly.
        current_tiers = (current or {}).get("tiers")
        shape_err = validate_tiers_shape(tiers, current_tiers=current_tiers)
        if shape_err is not None:
            return jsonify({"error": shape_err}), 400
        credentialed = _bot_providers_with_keys(bot_id)

        # Merged role resolutions so reconcile also adds role-resolved DEFAULT
        # models (max → claude-fable-5) the tiers doc never names. Best-effort:
        # a resolution failure must not block the tier write.
        resolved_role_models: dict = {}
        try:
            from primary_bot import resolve_roles_with_provenance  # type: ignore
            roles_view = resolve_roles_with_provenance(
                load_network(network_path), bot_id, credentialed_providers=credentialed,
            )
            resolved_role_models = {
                role: (info or {}).get("resolvedModel")
                for role, info in (roles_view or {}).items()
                if (info or {}).get("resolvedModel")
            }
        except Exception as exc:
            _log.warning("tiers reconcile: role resolution failed for %s: %s", bot_id, exc)

        reconciled = reconcile_catalog(
            current_catalog, tiers, credentialed_providers=credentialed,
            resolved_role_models=resolved_role_models,
            current_tiers=current_tiers,  # same baseline; reconcile re-validates
        )

        updates: dict = {"tiers": tiers}
        if not reconciled.unchanged:
            updates["catalog"] = reconciled.new_catalog

        result, write_err = _cfg_set_err(bot_id, updates)
        if not result:
            # Rung-collision (two roles → one rung, different models; judge is
            # decoupled per spec §Addendum 16, but a hand-authored roles map can
            # still collide): the writer refuses an impossible fold rather than
            # last-writer-wins — operator-fixable input → 409. tier_write_guard.
            collision = _tier_guard.collision_message(bot_id, write_err)
            if collision:
                return jsonify({"error": collision}), 409
            # Surface oc_model.py's structured error (the admin daemon stderr it
            # otherwise points at isn't visible in the UI). Same as PR #1725.
            err_suffix = f": {write_err}" if write_err else " (check admin-ui.err.log)"
            return jsonify({
                "error": f"reconcile write failed for {bot_id}{err_suffix}",
            }), 500

        # Truthfulness guard: a truthy result is NOT proof the change landed —
        # verify every requested model is present in the post-write synthesized
        # tiers (mirror of the freshness fix). 500 on silent non-persist.
        missing = _tier_guard.tiers_did_not_land(tiers, result.get("tiers"))
        if missing:
            return jsonify({
                "error": (
                    f"write reported success but {missing} did not persist to "
                    f"{bot_id}'s tiers — the change was not saved"
                ),
            }), 500
        _module._audit_log_entry("config.tiers.set", bot_id, {
            "tiers": tiers,
            "catalog_auto_added": reconciled.added_from_tiers,
            "catalog_auto_added_roles": reconciled.added_from_roles,
            "skipped_uncredentialed": reconciled.skipped_uncredentialed,
        }, oc_keys={"agents"})
        return jsonify({
            "ok": True, "bot": bot_id,
            "tiers": result.get("tiers"),
            "generatedFallbacks": result.get("generatedFallbacks", []),
            "catalog": result.get("catalog"),
            "catalog_auto_added": reconciled.added_from_tiers,
            "catalog_auto_added_roles": reconciled.added_from_roles,
            "skipped_uncredentialed": reconciled.skipped_uncredentialed,
        })

    @app.put("/api/admin/config/<bot_id>/tier-mode")
    def api_admin_config_set_tier_mode(bot_id: str) -> "Response | tuple[Response, int]":
        """Flip a bot between Use-pod-defaults and Custom tier definitions.

        Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §Addendum 5 — the
        per-bot toggle that replaces the Phase 7 per-tier provenance control.
        **Strict all-or-nothing**: a bot is either Custom (its own full
        rungs/roles) or Use-defaults (no per-bot rungs/roles, inherits the
        merged pod/code default).

        Body: ``{"mode": "custom" | "default"}``.

        - ``custom`` — materialize a per-bot override SEEDED from the bot's
          current resolved view (so nothing changes on flip except the bot
          becomes editable). The seed (full merged rungs/roles) is computed
          server-side via ``primary_bot.materialize_bot_tier_override`` — no
          model literals cross the wire — and written wholesale through the
          existing safe tiers path (``oc_model._save_tiers_file``).
        - ``default`` — clear the bot's per-bot rungs/roles (write empties),
          so the merge inherits the pod/code default again.

        Resolution logic is unchanged; this is pure materialize/clear via the
        keyed merge that already drives the gateway.
        """
        rej = _reject_unknown_bot(bot_id)
        if rej is not None:
            return rej
        body = request.get_json() or {}
        mode = body.get("mode")
        if mode not in ("custom", "default"):
            return jsonify({
                "error": "mode must be 'custom' or 'default'",
            }), 400

        from runtime.agent_runtime import get_runtime
        _rt = get_runtime()
        _cfg_set_err = _rt.full_config_set_with_error

        if mode == "custom":
            # Seed the per-bot override from the merged resolution. Computed
            # server-side so the rungs/roles (with model IDs) never round-trip
            # through the browser — keeps the provider-literal invariant on the
            # JS side and guarantees the seed matches what the gateway routes.
            try:
                net = load_network(network_path)
                from primary_bot import materialize_bot_tier_override  # type: ignore
                seed = materialize_bot_tier_override(net, bot_id)
            except Exception as exc:
                _log.warning(
                    "tier-mode custom: materialize failed for %s: %s",
                    bot_id, exc,
                )
                return jsonify({
                    "error": f"could not materialize defaults for {bot_id}: {exc}",
                }), 500
            if not seed.get("rungs"):
                return jsonify({
                    "error": (
                        f"no resolvable tier definitions to seed {bot_id} from "
                        "— check the pod/code default catalog"
                    ),
                }), 500
            updates = dict(seed)
            # Lifecycle rule 1 (spec-model-auto-upgrade §Scope): flipping to
            # Custom inherits the pod's CURRENT auto-upgrade block as a literal
            # seed — customizing rungs must not silently change whether the bot
            # rides the latest version. No pod block → seed nothing (the bot
            # resolves to the code default, same as the pod did).
            _pod_au = (net.get("models") or {}).get("autoUpgrade")
            if isinstance(_pod_au, dict) and _pod_au:
                updates["autoUpgrade"] = dict(_pod_au)
        else:
            # Reset: empty rungs/roles clear the keys (the wholesale-write path
            # in oc_model drops an empty list/dict), so the file carries no
            # per-bot tier definitions and the merge inherits the default.
            # autoUpgrade rides along (lifecycle rule 2): back to following the
            # pod IN FULL, auto-upgrade posture included.
            updates = {"rungs": [], "roles": {}, "roleCaps": {}, "autoUpgrade": {}}

        result, write_err = _cfg_set_err(bot_id, updates)
        if not result:
            err_suffix = f": {write_err}" if write_err else " (check admin-ui.err.log)"
            return jsonify({
                "error": f"tier-mode {mode} write failed for {bot_id}{err_suffix}",
            }), 500
        _module._audit_log_entry(
            "config.tier_mode.set", bot_id,
            {"mode": mode}, oc_keys={"agents"},
        )
        return jsonify({"ok": True, "bot": bot_id, "mode": mode})

    # ── POD "Default tier definitions" editor (spec §Addendum 5) ──────────────
    #
    # The pod layer (``network.json::models.rungs/roles/roleCaps``) sits between
    # the code DEFAULT_MODEL_CATALOG and each bot's evolve-tiers.json. These two
    # routes back the POD-tab editor:
    #   GET  — the effective default cluster + per-role provenance (default/pod)
    #          + whether a pod override currently exists.
    #   PUT  — validate (judge provider-diversity invariant) + write the pod
    #          layer via evolve_config._patch_network_json (atomic, splices
    #          ONLY the given key path so models.embedding is preserved).
    #          ``mode: "reset"`` clears the pod layer back to the code default.

    @app.get("/api/admin/config/pod/models")
    def api_admin_config_pod_models_get() -> "Response | tuple[Response, int]":
        """Effective POD-default rungs/roles + per-role provenance for the editor.

        Returns the ``defaults ← pod`` merged cluster (what a Use-defaults bot
        inherits), each role tagged ``default`` (from the code catalog) or
        ``pod`` (an operator override materialized it), plus ``podConfigured``
        so the UI knows whether "Reset to Evolve defaults" has anything to clear.
        """
        try:
            net = load_network(network_path)
            from primary_bot import pod_default_catalog_view  # type: ignore
            # Credential-match the picker's recommended (code-default) cluster to
            # the pod's credentialed providers so it never stars a model no bot
            # in the pod can run (spec §Addendum 6).
            view = pod_default_catalog_view(
                net, credentialed_providers=_pod_credentialed_providers(net),
            )
        except Exception as exc:
            _log.warning("pod models GET failed: %s", exc)
            return jsonify({"error": f"could not load pod default catalog: {exc}"}), 500
        return jsonify(view)

    @app.put("/api/admin/config/pod/models")
    def api_admin_config_pod_models_set() -> "Response | tuple[Response, int]":
        """Write (or reset) the POD default tier definitions.

        Body (one of):
          - ``{"mode": "reset"}`` — clear the pod layer (remove
            ``models.rungs/roles/roleCaps`` from network.json) so the merge
            falls back to the code ``DEFAULT_MODEL_CATALOG``.
          - ``{"rungs": [...], "roles": {...}, "roleCaps"?: {...}}`` —
            materialize/replace the pod default. Validated for the judge
            provider-diversity invariant (and non-empty ladder roles) BEFORE
            write — an invalid candidate is rejected, never persisted.

        Writes go through ``evolve_config._patch_network_json`` at
        ``["models","rungs"]`` / ``["models","roles"]`` / ``["models","roleCaps"]``
        granularity — each call splices ONE child of ``models``, so a sibling
        ``models.embedding`` block is preserved untouched. No provider/model
        literals in this logic; the rungs/roles arrive from the client and are
        validated structurally.
        """
        body = request.get_json() or {}
        mode = body.get("mode")

        try:
            from evolve_config import _patch_network_json  # type: ignore
            from primary_bot import canonicalize_and_validate_pod_catalog  # type: ignore
        except Exception as exc:
            return jsonify({"error": f"analyzer module not importable: {exc}"}), 500

        if mode == "reset":
            # Clear the pod layer: drop rungs/roles/roleCaps from models, leaving
            # any models.embedding block intact. Read-modify-write the models
            # object so siblings survive; patch the whole models key with the
            # tier keys removed.
            try:
                net = load_network(network_path)
                models_block = net.get("models")
                models_block = dict(models_block) if isinstance(models_block, dict) else {}
                for k in ("rungs", "roles", "roleCaps"):
                    models_block.pop(k, None)
                _patch_network_json(network_path, ["models"], models_block)
            except Exception as exc:
                _log.warning("pod models reset failed: %s", exc)
                return jsonify({"error": f"pod default reset failed: {exc}"}), 500
            # network.json edit — no openclaw.json keys touched (oc_keys unset).
            _module._audit_log_entry("config.pod_models.reset", "pod", {})
            return jsonify({"ok": True, "mode": "reset"})

        # Materialize / replace the pod default.
        rungs = body.get("rungs")
        roles = body.get("roles")
        role_caps = body.get("roleCaps")
        if not isinstance(rungs, list) or not isinstance(roles, dict):
            return jsonify({
                "error": "body must be {mode:'reset'} or {rungs:[...], roles:{...}}",
            }), 400

        candidate: dict[str, Any] = {"rungs": rungs, "roles": roles}
        if isinstance(role_caps, dict):
            candidate["roleCaps"] = role_caps
        # Server-side single enforcement point: canonicalize rung ids + backfill
        # costClass, then validate (spec Addendum 8 §D — see helper docstring).
        candidate, err = canonicalize_and_validate_pod_catalog(candidate)
        if err:
            return jsonify({"error": f"invalid pod default catalog: {err}"}), 400
        rungs = candidate["rungs"]
        roles = candidate["roles"]

        # Splice each tier key separately so models.embedding (a sibling) is
        # never clobbered. roleCaps is optional — write it only when provided.
        try:
            _patch_network_json(network_path, ["models", "rungs"], rungs)
            _patch_network_json(network_path, ["models", "roles"], roles)
            if isinstance(role_caps, dict):
                _patch_network_json(network_path, ["models", "roleCaps"], role_caps)
        except Exception as exc:
            _log.warning("pod models write failed: %s", exc)
            return jsonify({"error": f"pod default write failed: {exc}"}), 500
        # network.json edit — no openclaw.json keys touched (oc_keys unset).
        _module._audit_log_entry(
            "config.pod_models.set", "pod",
            {"rung_count": len(rungs), "roles": sorted(roles.keys())},
        )
        return jsonify({"ok": True, "mode": "set"})

    @app.put("/api/admin/config/<bot_id>/routing")
    def api_admin_config_set_routing(bot_id: str) -> Response:
        """Merge a routing update into the bot's evolve-tiers.json routing block.

        The body is validated BEFORE anything is persisted (#3566 audit E-3) —
        a malformed body is a 400 and never reaches the file. It used to be
        written verbatim, which wedged ``oc_model.get_routing`` forever on a
        non-dict value; the repairing PUT then still returned 500 because the
        throw happened in the post-save read-back.

        Semantics are PARTIAL MERGE, not wholesale replace, matching the
        sibling ``cascade`` / ``userTierOverride`` blocks. The admin SPA's
        routing card always posts the full block, so for it the merge only
        differs in one respect (below); what it buys is that no partial write
        can silently clear ``routing.enabled=false`` — the OC-2026.7
        kill-switch that every consumer defaults absent-to-enabled.

        The merge is per-SLOT: ``maintenanceTier`` and ``maintenanceRole`` are
        one slot (likewise background / ambiguous), so writing one evicts the
        other. The plugin resolves ``maintenanceRole ?? maintenanceTier``, so a
        naive key-wise merge would let a stale role written by
        ``migrate-model-roles`` permanently shadow the operator's card edit.
        Sending both halves of a slot in one body is a 400.
        """
        rej = _reject_unknown_bot(bot_id)
        if rej is not None:
            return rej
        body = request.get_json() or {}
        routing = body.get("routing")
        if routing is None:
            return jsonify({"error": "routing required"}), 400
        from oc_model import validate_routing_update  # type: ignore
        shape_err = validate_routing_update(routing)
        if shape_err:
            return jsonify({"error": shape_err}), 400
        from runtime.agent_runtime import get_runtime
        _rt = get_runtime()
        _cfg_set = _rt.full_config_set
        result = _cfg_set(bot_id, {"routing": routing})
        if not result:
            return jsonify({"error": "write failed — check server logs"}), 500
        # Log the RESULTING block, not the requested partial — under merge
        # semantics the request no longer describes the resulting state, and an
        # audit entry that doesn't is worse than useless.
        _module._audit_log_entry(
            "config.routing.set", bot_id,
            {"routing": result.get("routing"), "requested": routing},
            oc_keys={"agents"},
        )
        return jsonify({"ok": True, "bot": bot_id, "routing": result.get("routing")})

    @app.put("/api/admin/config/<bot_id>/fallback")
    def api_admin_config_set_fallback(bot_id: str) -> Response:
        """Set fallbackMode and/or tierCascade; recomputes flat fallback list."""
        rej = _reject_unknown_bot(bot_id)
        if rej is not None:
            return rej
        body = request.get_json() or {}
        updates: dict = {}
        if "fallbackMode" in body:
            updates["fallbackMode"] = body["fallbackMode"]
        if "tierCascade" in body:
            updates["tierCascade"] = body["tierCascade"]
        if not updates:
            return jsonify({"error": "fallbackMode or tierCascade required"}), 400
        from runtime.agent_runtime import get_runtime
        _rt = get_runtime()
        _cfg_set = _rt.full_config_set
        result = _cfg_set(bot_id, updates)
        if not result:
            return jsonify({"error": "write failed — check server logs"}), 500
        _module._audit_log_entry("config.fallback.set", bot_id, updates, oc_keys={"agents"})
        return jsonify({
            "ok": True, "bot": bot_id,
            "fallbackMode": result.get("fallbackMode"),
            "tierCascade": result.get("tierCascade"),
            "generatedFallbacks": result.get("generatedFallbacks", []),
        })

    @app.put("/api/admin/config/<bot_id>/cascade")
    def api_admin_config_set_cascade(bot_id: str) -> Response:
        """Toggle the tier-cascade controller for this bot.

        Body: ``{"enabled": bool}``.

        Writes ``cascade.enabled`` into the bot's ``evolve-tiers.json``
        (via oc_full_config_set's cascade key — added 2026-05-28). The
        plugin's ModelRouter reads this on every ``before_model_resolve``
        and routes per the CascadeController's verdict when ``enabled``
        is true (Phase-3 cutover behavior), otherwise keeps verdicts
        shadow-only (Phase-2 default).

        The operator (or a future generator) flips this once per-bot
        shadow telemetry has been graded. Used to roll cascade out
        progressively — flip on the treatment bots, leave a control
        group on the classifier, compare cost / struggle / completion
        rates over a week.

        Audited to ``audit-log.jsonl`` as ``config.cascade.set`` so the
        treatment/control assignment is reconstructable from the log
        if the operator needs to revisit which bots got flipped when.
        """
        rej = _reject_unknown_bot(bot_id)
        if rej is not None:
            return rej
        body = request.get_json() or {}
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return jsonify({
                "error": "enabled (bool) required",
            }), 400
        from runtime.agent_runtime import get_runtime
        _rt = get_runtime()
        _cfg_set = _rt.full_config_set
        result = _cfg_set(bot_id, {"cascade": {"enabled": enabled}})
        if not result:
            return jsonify({
                "error": "write failed — check server logs",
            }), 500
        _module._audit_log_entry(
            "config.cascade.set", bot_id,
            {"enabled": enabled}, oc_keys={"agents"},
        )
        return jsonify({
            "ok": True, "bot": bot_id,
            "cascade": result.get("cascade") or {"enabled": enabled},
        })

    @app.put("/api/admin/config/<bot_id>/user-tier-override")
    def api_admin_config_set_user_tier_override(bot_id: str) -> Response:
        """Update the per-bot ``userTierOverride`` block (audit #69 Phase A).

        Body: any subset of::

            {
              "enabled": bool,            # show chip / accept session_set_tier
              "dailyCap": int (0–100),    # tier1 per-day cap (PR #1780)
              "allowBotInitiated": bool,  # tier1 self-escalation (PR #1780)
              "defaultTier": str          # "auto" | "fast" | "standard" | "power"
                                         # NOTE: "max" is rejected here — it is
                                         # pull-only; use evo tier-default max
                                         # for a per-USER default.
            }

        Writes to ``~/.openclaw/evolve-tiers.json::userTierOverride`` via
        ``oc_full_config_set`` — partial-merge semantics. Sending one key
        leaves the others untouched, so the AI Optimization page can ship
        the picker as a standalone dropdown without coupling to the
        existing dailyCap CLI / chip surfaces.

        The plugin's ``ModelRouter._resolveOperatorDefaultTier`` reads
        ``defaultTier`` on every turn and routes user-turn / ambiguous
        sessions to the chosen tier before falling through to OC's
        primary. Slots in the precedence ladder below the user override
        (chip + ``session_set_tier``) and below the cascade verdict —
        both still win when set.

        Audited as ``config.user_tier_override.set`` with
        ``oc_keys={"tiers"}`` so heal.py's drift detector credits this
        endpoint as the writer (not as foreign drift). Same pattern as
        the cascade endpoint above.
        """
        rej = _reject_unknown_bot(bot_id)
        if rej is not None:
            return rej
        body = request.get_json() or {}
        # Whitelist + lightweight validation. The deeper schema check
        # happens in oc_model.json_full_config_set, which silently drops
        # unknown keys — but rejecting at the endpoint gives the operator
        # a clear 400 rather than a silent no-op.
        allowed = {"enabled", "dailyCap", "allowBotInitiated", "defaultTier"}
        unknown = set(body.keys()) - allowed
        if unknown:
            return jsonify({
                "error": f"unknown field(s): {sorted(unknown)}; allowed: {sorted(allowed)}",
            }), 400
        if not body:
            return jsonify({"error": "at least one field required"}), 400

        if "enabled" in body and not isinstance(body["enabled"], bool):
            return jsonify({"error": "enabled must be bool"}), 400
        if "allowBotInitiated" in body and not isinstance(body["allowBotInitiated"], bool):
            return jsonify({"error": "allowBotInitiated must be bool"}), 400
        if "dailyCap" in body:
            cap = body["dailyCap"]
            if not isinstance(cap, int) or isinstance(cap, bool) or not (0 <= cap <= 100):
                return jsonify({"error": "dailyCap must be int 0-100"}), 400
        if "defaultTier" in body:
            choice = body["defaultTier"]
            allowed_tiers = {"auto", "fast", "standard", "power"}
            if not isinstance(choice, str) or choice not in allowed_tiers:
                # max is pull-only: per-user defaults may be max (via evo
                # tier-default) but the operator BOT-WIDE default cannot be
                # max — ModelRouter blocks classifier/maintenance escalation
                # to Fable (spec-model-rungs-and-roles §max semantics #4).
                extra = (
                    " ('max' is pull-only: use 'evo tier-default max' to set "
                    "YOUR personal default, but the bot-wide default must not be max)"
                    if choice == "max" else ""
                )
                return jsonify({
                    "error": (
                        f"defaultTier must be one of {sorted(allowed_tiers)}{extra}"
                    ),
                }), 400

        from runtime.agent_runtime import get_runtime
        _rt = get_runtime()
        _cfg_set_err = _rt.full_config_set_with_error
        result, write_err = _cfg_set_err(
            bot_id, {"userTierOverride": body},
        )
        if not result:
            err_suffix = f": {write_err}" if write_err else " (check admin-ui.err.log)"
            return jsonify({
                "error": f"user-tier-override write failed for {bot_id}{err_suffix}",
            }), 500
        _module._audit_log_entry(
            "config.user_tier_override.set", bot_id,
            body, oc_keys={"tiers"},
        )
        return jsonify({
            "ok": True, "bot": bot_id,
            "userTierOverride": result.get("userTierOverride") or {},
        })

    # ── Model freshness (best-in-class recommendations) ─────────────────────

    # Credential-presence readers live in the model_freshness_drift helper
    # (this file is a frozen no-growth hot-hazard).
    from .model_freshness_drift import bot_providers_and_source as _bot_providers_and_source

    def _bot_providers_with_keys(bot_id: str) -> set:
        return _bot_providers_and_source(bot_id)[0]

    def _attach_diversity_metadata(summary: dict, network: dict) -> dict:
        """Filter dismissed diversity advisories and attach per-provider
        borrow_candidates so the UI can render "Copy from <bot>" buttons
        without a second round-trip.

        Mutates `summary` in place and returns it.
        """
        from model_registry import load_dismissals
        from .wizard_routes import borrow_candidates as _borrow_candidates

        shared_dir = Path(network.get("sharedDir", CANONICAL_SHARED_DIR))
        dismissals = load_dismissals(shared_dir)
        dismissed_bots = set(
            (dismissals.get("diversity") or {}).keys()
            if isinstance(dismissals.get("diversity"), dict) else []
        )

        raw = summary.get("diversity_advisories") or []
        filtered: list[dict] = []
        candidate_cache: dict[str, list[dict]] = {}
        for advisory in raw:
            bot_id = advisory.get("bot_id")
            if not bot_id or bot_id in dismissed_bots:
                continue
            # Compute borrow candidates per suggested provider, excluding the
            # bot itself as a source.
            by_provider: dict[str, list[dict]] = {}
            for provider in (advisory.get("suggested_providers") or []):
                if provider not in candidate_cache:
                    candidate_cache[provider] = _borrow_candidates(
                        provider, network,
                    )
                by_provider[provider] = [
                    c for c in candidate_cache[provider]
                    if c.get("bot_id") != bot_id
                ]
            advisory_with_candidates = dict(advisory)
            advisory_with_candidates["borrow_candidates"] = by_provider
            filtered.append(advisory_with_candidates)

        summary["diversity_advisories"] = filtered
        summary["diversity_count"] = len(filtered)
        summary["diversity_dismissed_bots"] = sorted(dismissed_bots)
        return summary

    @app.get("/api/models/freshness-status")
    def api_models_freshness_status() -> Response:
        """Return the persisted summary of the most recent freshness check.
        Empty {} if no check has run yet.
        """
        from model_registry import load_last_check
        cfg = load_network(network_path)
        shared_dir = Path(cfg.get("sharedDir", CANONICAL_SHARED_DIR))
        summary = load_last_check(shared_dir)
        if summary:
            _attach_diversity_metadata(summary, cfg)
        return jsonify(summary)

    def _pod_credentialed_providers(net: dict) -> set:
        """Union of providers any bot in the pod holds a key for. This is the
        credentialed set the validated picker draws from — derived from
        auth-profiles (no provider-name literals), so a new provider needs no
        code change here.
        """
        providers: set = set()
        for bot_id in (net.get("bots") or {}).keys():
            try:
                providers.update(_bot_providers_with_keys(bot_id))
            except Exception as exc:  # one unreadable bot must not blank the set
                _log.warning(
                    "listings: could not read providers for %s: %s",
                    bot_id, exc,
                )
        return providers

    def _pod_llm_providers(net: dict, cache: dict, credentialed: set) -> set:
        """The LLM-capable, credentialed provider set the easy-setup wizard
        ranks (spec §Addendum 7 #12).

        An "LLM provider" is one the pod can actually route a chat turn to:
        either it names a model in a catalog rung cluster
        (``primary_bot.llm_providers_from_catalog`` — anthropic/openai/google/xai
        today), OR it lists at least one chat-capable model in discovery
        (``model_discovery.llm_providers_from_listings`` — picks up a
        credentialed DeepSeek that isn't yet in any cluster). The union,
        intersected with the credentialed set, EXCLUDES non-LLM providers
        (Brave, Runway) with no provider-name literal — both inputs are
        data-derived.
        """
        import model_discovery as _model_discovery
        from primary_bot import (  # type: ignore
            pod_default_catalog_view,
            llm_providers_from_catalog,
        )
        catalog = pod_default_catalog_view(net)
        from_catalog = llm_providers_from_catalog(catalog)
        from_listings = _model_discovery.llm_providers_from_listings(
            cache, providers=credentialed,
        )
        return {p for p in credentialed if p in from_catalog or p in from_listings}

    def _filter_listings_to_credentialed(cache: dict, net: dict) -> dict:
        """Return the cache document filtered to credentialed providers only.

        The cache may carry providers later de-credentialed; the picker must
        only offer providers the pod can actually reach. Degraded entries are
        likewise filtered to the credentialed set.

        ``llm_providers`` is the LLM-capable subset the easy-setup wizard ranks
        (Brave/Runway excluded, DeepSeek included if it lists chat models) — the
        client builds its provider-order from THIS, not the raw credentialed set.
        """
        credentialed = _pod_credentialed_providers(net)
        providers_in = (cache.get("providers") or {})
        degraded_in = (cache.get("degraded") or [])
        return {
            "refreshed_at": cache.get("refreshed_at"),
            "providers": {
                p: models for p, models in providers_in.items()
                if p in credentialed
            },
            "degraded": [
                d for d in degraded_in
                if d.get("provider") in credentialed
            ],
            "credentialed_providers": sorted(credentialed),
            "llm_providers": sorted(_pod_llm_providers(net, cache, credentialed)),
        }

    @app.get("/api/models/listings")
    def api_models_listings() -> Response:
        """Return the cached provider model-listings, filtered to credentialed
        providers — the validated picker's candidate source (Phase 9).

        Empty providers + a null refreshed_at means no discovery run has
        populated the cache yet (the UI offers a "Refresh" to build it).
        """
        import model_discovery as _model_discovery

        net = load_network(network_path)
        shared_dir = Path(net.get("sharedDir", CANONICAL_SHARED_DIR))
        cache = _model_discovery.read_listings_cache(shared_dir)
        if cache is None:
            cache = {"refreshed_at": None, "providers": {}, "degraded": []}
        return jsonify(_filter_listings_to_credentialed(cache, net))

    @app.post("/api/models/listings/refresh")
    def api_models_listings_refresh() -> Response:
        """Trigger a fresh provider enumeration and rewrite the listings cache.

        Reuses the same ``run_discovery`` path the "Check now" button uses —
        the cache write rides that run (no extra provider calls beyond the one
        listing call per credentialed provider). Operator-triggered only; the
        daily sweep keeps the cache warm otherwise.
        """
        import model_discovery as _model_discovery

        net = load_network(network_path)
        shared_dir = Path(net.get("sharedDir", CANONICAL_SHARED_DIR))
        bots = list((net.get("bots") or {}).keys())
        try:
            from runtime.agent_runtime import get_runtime
            _cfg_get_full = get_runtime().full_config_get
            bot_users = [_resolve_user(b) for b in bots]
            bot_cfgs = {b: (_cfg_get_full(b) or {}) for b in bots}
            _model_discovery.run_discovery(
                network=net,
                bot_users=bot_users,
                bot_configs=bot_cfgs,
                shared_dir=shared_dir,
            )
        except Exception as exc:
            _log.warning("listings: refresh enumeration failed: %s", exc)
            return jsonify({
                "ok": False,
                "error": f"listings refresh failed: {exc}",
            }), 502
        cache = _model_discovery.read_listings_cache(shared_dir) or {
            "refreshed_at": None, "providers": {}, "degraded": [],
        }
        out = _filter_listings_to_credentialed(cache, net)
        out["ok"] = True
        return jsonify(out)

    @app.post("/api/models/validate-model")
    def api_models_validate_model() -> "Response | tuple[Response, int]":
        """Validate a free-text model id against the cached listings (the
        picker's "add anyway" path). An optional ``bot_id`` scopes the candidate
        set to that bot's credentialed providers (fail-open to the pod-wide
        union) so a bot can't free-type a model from a provider it can't run.
        """
        import model_discovery as _model_discovery
        from ..model_catalog import scope_credentialed_to_bot
        body = request.get_json(silent=True) or {}
        typed = (body.get("model") or "").strip()
        # Third body-derived bot_id (#3566 audit C-2): it flows to
        # ``oc_keys_get``, which has no roster guard, so an unregistered value
        # reached ``sudo -H -u <bot_id> … oc_keys.py``. NOT a 400 — unlike the
        # write routes, this endpoint's documented contract is to FALL OPEN to
        # the pod-wide union for a bot it can't scope to
        # (test_validate_unknown_bot_falls_open_to_pod_wide), so drop the scope
        # rather than reject. isinstance = a non-string body falls open too.
        scope_bot = body.get("bot_id")
        scope_bot = scope_bot.strip() if isinstance(scope_bot, str) else ""
        if scope_bot and _reject_unknown_bot(scope_bot) is not None:
            scope_bot = ""
        net = load_network(network_path)
        shared_dir = Path(net.get("sharedDir", CANONICAL_SHARED_DIR))
        cache = _model_discovery.read_listings_cache(shared_dir)
        if cache is None:
            return jsonify({"ok": False, "suggestion": None,
                "reason": "no listings cached yet — refresh first"})
        credentialed = scope_credentialed_to_bot(
            scope_bot,
            _bot_providers_with_keys, lambda: _pod_credentialed_providers(net))
        result = _model_discovery.validate_against_listing(
            typed, cache, providers=credentialed)
        return jsonify(result)

    @app.post("/api/models/check-freshness")
    def api_models_check_freshness() -> Response:
        """Scan every bot's tier config; compare against RECOMMENDED registry.
        Returns and persists a summary {checked_at, advisory_count, advisories[]}.
        """
        from runtime.agent_runtime import get_runtime
        _rt = get_runtime()
        _cfg_get = _rt.full_config_get
        from model_registry import (
            check_bot_freshness, check_bot_diversity,
            save_last_check, RECOMMENDED,
        )

        from ..model_catalog import find_catalog_drift
        from dataclasses import asdict as _asdict

        net = load_network(network_path)
        shared_dir = Path(net.get("sharedDir", CANONICAL_SHARED_DIR))
        bots = list(net.get("bots", {}).keys())

        all_advisories: list = []
        all_diversity: list = []
        all_providers: set = set()
        all_drift: list = []   # CatalogDriftFinding list
        redundant_bots: list = []  # Custom bots resolving == pod default (§Add.5)
        bot_sev_input: dict = {}   # {bot_id: (tiers, providers)} for §C severity
        cred_sources: dict = {}    # {bot_id: source} for the loud-fail guard
        # Redundant-config advisory (spec §Addendum 5 "Migration / cleanup"):
        # a Custom bot whose explicit config resolves equal-after-merge to the
        # pod default — clearing it changes nothing. Best-effort per bot.
        try:
            from primary_bot import bot_redundant_vs_default  # type: ignore
        except Exception:
            bot_redundant_vs_default = None  # type: ignore
        for bot_id in bots:
            if bot_redundant_vs_default is not None:
                try:
                    if bot_redundant_vs_default(net, bot_id):
                        redundant_bots.append({"bot_id": bot_id})
                except Exception as exc:
                    _log.warning(
                        "freshness: redundant-config check failed for %s: %s",
                        bot_id, exc,
                    )
            cfg = _cfg_get(bot_id) or {}
            tiers = cfg.get("tiers") or {}
            catalog = cfg.get("catalog") or []
            providers, cred_sources[bot_id] = _bot_providers_and_source(bot_id)
            all_providers.update(providers)
            bot_sev_input[bot_id] = (tiers, providers)
            all_advisories.extend(check_bot_freshness(bot_id, tiers, providers))
            # Soft "consider adding a second provider" advisory — fired when
            # the bot has only one credentialed LLM provider. Used by the UI
            # to suggest copying a key from another bot or adding manually.
            diversity = check_bot_diversity(bot_id, providers)
            if diversity is not None:
                all_diversity.append(diversity)
            # Catalog drift findings — surface "tier references model X
            # but X isn't in catalog" and "provider Y credentialed but
            # RECOMMENDED missing from catalog". Team_bot_a's google/xai/openai
            # situation falls in here. Added 2026-05-28.
            #
            # Default-coverage gap (spec §Addendum3.D): a role may resolve to a
            # code-default model the bot's tiers doc never names (max →
            # claude-fable-5), so it is invisible to the tier-walk above and a
            # Max pull dies at the OC layer with no advisory. Compute the merged
            # role resolutions and pass {role: resolvedModel} so find_catalog_drift
            # can flag any that fall outside the catalog. Best-effort: a
            # resolution failure must not break the freshness check.
            resolved_role_models: dict = {}
            try:
                from primary_bot import resolve_roles_with_provenance  # type: ignore
                roles_view = resolve_roles_with_provenance(
                    net, bot_id, credentialed_providers=providers,
                )
                resolved_role_models = {
                    role: (info or {}).get("resolvedModel")
                    for role, info in (roles_view or {}).items()
                    if (info or {}).get("resolvedModel")
                }
            except Exception as exc:
                # Honest fail-open: empty resolved_role_models disables ONLY
                # the Type-3 (role_resolves_outside_catalog) drift pass for
                # this bot — the tier-walk findings still run. Log so the
                # operator can tell "no role drift" from "role drift
                # detection never ran". Mirrors the discovery fail-open below.
                _log.warning(
                    "freshness: role resolution failed for %s, Type-3 "
                    "(role-resolves-outside-catalog) drift detection "
                    "skipped for this bot: %s", bot_id, exc,
                )
                resolved_role_models = {}
            for finding in find_catalog_drift(
                bot_id, catalog, tiers, providers,
                resolved_role_models=resolved_role_models,
            ):
                all_drift.append(_asdict(finding))

        # ── Discovery-based freshness (Phase 4) ─────────────────────────────
        # The legacy advisories above are list-matching against RECOMMENDED;
        # discovery enumerates each provider's LIVE listing and diffs it
        # against the pod's rungs. Crucially, a failed listing call lands in
        # ``degraded_providers`` with a reason — so the on-demand check never
        # silently reads "all current" when discovery couldn't actually run
        # (the silent-monitor-drift lesson). The daily generator sweep is the
        # primary path (emits Signals + Proposals); this on-demand block keeps
        # the admin "Check now" button honest.
        discovery_block: dict = {}
        try:
            import model_discovery as _model_discovery

            bot_users = [_resolve_user(b) for b in bots]
            bot_cfgs = {b: (_cfg_get(b) or {}) for b in bots}
            disc = _model_discovery.run_discovery(
                network=net,
                bot_users=bot_users,
                bot_configs=bot_cfgs,
                shared_dir=shared_dir,
            )
            discovery_block = disc.to_dict()
        except Exception as exc:  # never let discovery break the legacy check
            discovery_block = {
                "error": f"discovery failed to run: {exc}",
                "is_degraded": True,
                "all_current": False,
            }

        summary = save_last_check(
            shared_dir, all_advisories, all_providers,
            diversity_advisories=all_diversity,
            discovery=discovery_block,
        )
        # Cred-gap drift: borrow_candidates (§B) + hard-break/dormant split (§C).
        from .model_freshness_drift import (
            attach_credential_store_unreadable,
            attach_drift_borrow_candidates,
            attach_tier_severity,
        )
        attach_drift_borrow_candidates(all_drift, net)
        summary["drift_findings"] = all_drift   # side-car (not persisted by save_last_check)
        summary["drift_count"] = len(all_drift)
        attach_tier_severity(summary, net, bot_sev_input)
        # Loud-fail guard: source="none" becomes a visible advisory (see helper).
        attach_credential_store_unreadable(summary, cred_sources, bots)
        # Redundant-config advisory side-car (spec §Addendum 5): Custom bots
        # whose config resolves equal to the pod default. Derived, no auto-action.
        summary["redundant_bots"] = redundant_bots
        summary["redundant_count"] = len(redundant_bots)
        _attach_diversity_metadata(summary, net)
        _module._audit_log_entry(
            "models.freshness.check", "pod",
            {
                "advisory_count": len(all_advisories),
                "drift_count": len(all_drift),
                "diversity_count": len(all_diversity),
                "bots_checked": bots,
            },
        )
        # Echo registry version so the UI can show "registry last updated …"
        summary["registry"] = {
            "providers": sorted(RECOMMENDED.keys()),
        }
        return jsonify(summary)

    @app.post("/api/models/freshness-advisory/dismiss")
    def api_models_freshness_advisory_dismiss() -> Response:
        """Dismiss a soft advisory permanently (until reset).

        Body: {type: "diversity", key: "<bot_id>"}.
        Currently only "diversity" is supported; the key is the bot_id.
        """
        from model_registry import dismiss_advisory
        body = request.get_json() or {}
        atype = (body.get("type") or "").strip()
        key = (body.get("key") or "").strip()
        if atype not in {"diversity"}:
            return jsonify({"error": "type must be one of: diversity"}), 400
        if not key:
            return jsonify({"error": "key required"}), 400
        net = load_network(network_path)
        shared_dir = Path(net.get("sharedDir", CANONICAL_SHARED_DIR))
        dismissals = dismiss_advisory(shared_dir, atype, key)
        _module._audit_log_entry(
            "models.freshness.advisory_dismiss", key,
            {"type": atype},
        )
        return jsonify({"ok": True, "dismissals": dismissals})

    @app.post("/api/models/freshness-advisory/reset")
    def api_models_freshness_advisory_reset() -> Response:
        """Clear dismissals. Body: {type?: "diversity"} (omit = clear all)."""
        from model_registry import reset_dismissals
        body = request.get_json() or {}
        atype = (body.get("type") or "").strip() or None
        if atype is not None and atype not in {"diversity"}:
            return jsonify({"error": "type must be one of: diversity"}), 400
        net = load_network(network_path)
        shared_dir = Path(net.get("sharedDir", CANONICAL_SHARED_DIR))
        dismissals = reset_dismissals(shared_dir, atype)
        _module._audit_log_entry(
            "models.freshness.advisory_reset", "pod",
            {"type": atype or "all"},
        )
        return jsonify({"ok": True, "dismissals": dismissals})

    def _check_recommendation_match(provider: str, tier: str, new_model: str) -> "str | None":
        """Return an error string if ``new_model`` isn't the current recommendation
        for (provider, tier); else None.

        The historical implementation read the static ``RECOMMENDED`` dict
        directly. That works UNTIL OC's live signal map advances ahead of
        the static fallback — at which point the freshness check (which
        consults the OC alias map via :func:`resolve_current_model`) emits
        "claude-opus-4-8" as the recommendation, the operator clicks Update,
        and this validator rejects it because ``RECOMMENDED`` still says
        "claude-opus-4-7". The 2026-06-07 incident.

        Both freshness-check and update validation must consult the SAME
        resolver, so the recommendation the operator sees in the UI is
        guaranteed to be the recommendation the writer accepts.
        """
        try:
            from model_registry import resolve_current_model
            rec_model, _src, _ver, _rel = resolve_current_model(provider, tier)
        except KeyError:
            return f"no recommendation exists for {provider}/{tier}"
        if rec_model != new_model:
            return (
                f"model {new_model!r} is not the current recommendation for "
                f"{provider}/{tier} (recommended: {rec_model})"
            )
        return None

    @app.post("/api/models/update-tier")
    def api_models_update_tier() -> Response:
        """One-click update of a bot's tier to the recommended model.

        Body: {bot_id, tier, provider, model}.
        Replaces any same-provider entry in the tier with the new model
        (or appends if no same-provider entry exists), preserving other
        providers' models in that tier.
        """
        body = request.get_json() or {}
        bot_id = body.get("bot_id")
        tier = body.get("tier")
        provider = (body.get("provider") or "").lower()
        new_model = body.get("model")

        if not all([bot_id, tier, provider, new_model]):
            return jsonify({"error": "bot_id, tier, provider, model are all required"}), 400
        # Body-derived bot_id: same roster guard the URL-path routes get
        # (#3566 audit C-2) — the oc_cli-side check skipped itself entirely
        # when network.json was unreadable.
        rej = _reject_unknown_bot(bot_id)
        if rej is not None:
            return rej

        from runtime.agent_runtime import get_runtime
        _rt = get_runtime()
        _cfg_get = _rt.full_config_get
        _cfg_set = _rt.full_config_set_with_error

        # Validate against the resolver the freshness check uses — see
        # _check_recommendation_match for the rationale (must match check).
        err = _check_recommendation_match(provider, tier, new_model)
        if err is not None:
            return jsonify({"error": err}), 400

        cfg = _cfg_get(bot_id)
        if not cfg:
            return jsonify({"error": f"could not read config for bot {bot_id!r}"}), 404

        # Send ONLY this tier (a sibling sharing a rung would clobber it) and
        # verify it landed before reporting success — see model_tier_apply.
        from .model_tier_apply import model_landed, record_swap, stage_tier_model, tier_models
        prev_models = tier_models(cfg, tier)  # BEFORE the write — the undo target
        tier_entry = stage_tier_model((cfg.get("tiers") or {}).get(tier), provider, new_model)

        # OC's catalog (agents.defaults.models) must also carry the model (else OC silently falls back); keep the prior model as a safe fallback.
        existing_catalog = list(cfg.get("catalog") or [])
        catalog_changed = new_model not in existing_catalog
        if catalog_changed:
            existing_catalog.append(new_model)

        updates = {"tiers": {tier: tier_entry}}
        if catalog_changed:
            updates["catalog"] = existing_catalog

        result, set_err = _cfg_set(bot_id, updates)
        if not result:
            return jsonify({"error": f"write failed: {set_err or 'check server logs'}"}), 500
        landed = tier_models(result, tier)
        if not model_landed(result, tier, new_model):
            err_resp = jsonify({"error": (
                f"write reported success but {new_model} is not present in "
                f"{bot_id} {tier} after the write — the change did not persist "
                f"(landed models: {landed})"
            )})
            err_resp.status_code = 500
            return err_resp

        record_swap(network_path, bot_id, tier, provider, prev_models, landed, source="admin_ui_single")
        _module._audit_log_entry("models.tier.update", bot_id, {
            "tier": tier, "provider": provider, "new_model": new_model,
            "catalog_added": catalog_changed, "previous_models": prev_models,
        })
        return jsonify({
            "ok": True,
            "bot": bot_id,
            "tier": tier,
            "models": landed,
            "catalog_added": catalog_changed,
            "catalog": result.get("catalog", existing_catalog),
            "generatedFallbacks": result.get("generatedFallbacks", []),
        })

    @app.post("/api/models/update-tier-bulk")
    def api_models_update_tier_bulk() -> Response:
        """Apply a batch of freshness advisories in one pass.

        Body: {updates: [{bot_id, tier, provider, model}, ...]}

        The single-shot ``/api/models/update-tier`` does one
        ``oc_full_config_get`` + ``oc_full_config_set`` per advisory —
        each of which subprocess-execs ``sudo -u {user} python3
        oc_model.py``. With a freshly-checked pod that's 50+ advisories,
        sequential client-side iteration takes ~2 minutes.

        This bulk path GROUPS by bot_id, then per bot: one get,
        apply every change in-memory, one set. For a typical pod
        post-Check-Now (8 bots × ~6 advisories each) that's 8 reads
        + 8 writes instead of 96 reads + 96 writes — under 30 seconds.

        Per-update validation matches the single endpoint (each model
        must match the current ``RECOMMENDED`` entry for its
        provider/tier). Per-update failures don't abort the whole
        batch — failed entries appear in ``results[].error`` and the
        rest of the bot's changes still apply.
        """
        body = request.get_json() or {}
        updates = body.get("updates") or []
        if not isinstance(updates, list) or not updates:
            return jsonify({"error": "updates: non-empty list required"}), 400

        from runtime.agent_runtime import get_runtime
        _rt = get_runtime()
        _cfg_get = _rt.full_config_get
        _cfg_set = _rt.full_config_set_with_error

        # Validate every update up front. We reject the whole batch on
        # any malformed entry — partial validation would leave callers
        # guessing which entries were rejected vs which actually wrote.
        # Once validation passes we still allow per-bot writes to fail
        # independently (a bot whose config can't be read shouldn't
        # block updates to other bots).
        normalized: list[dict] = []
        for i, u in enumerate(updates):
            if not isinstance(u, dict):
                return jsonify({"error": f"updates[{i}]: must be an object"}), 400
            bot_id = u.get("bot_id")
            tier = u.get("tier")
            provider = (u.get("provider") or "").lower()
            new_model = u.get("model")
            if not all([bot_id, tier, provider, new_model]):
                return jsonify({"error": f"updates[{i}]: bot_id, tier, provider, model all required"}), 400
            # Body-derived bot_id — same roster guard as the URL-path routes
            # (#3566 audit C-2). Up-front like the rest of this loop, so the
            # caller isn't left guessing which entries wrote.
            rej = _reject_unknown_bot(bot_id)
            if rej is not None:
                rej_body, rej_status = rej
                return jsonify({
                    "error": f"updates[{i}]: {rej_body.get_json()['error']}",
                }), rej_status
            err = _check_recommendation_match(provider, tier, new_model)
            if err is not None:
                return jsonify({"error": f"updates[{i}]: {err}"}), 400
            normalized.append({
                "bot_id": bot_id, "tier": tier,
                "provider": provider, "model": new_model,
            })

        # Group by bot_id, preserving each bot's first-seen order so the
        # response is deterministic for tests.
        by_bot: dict[str, list[dict]] = {}
        bot_order: list[str] = []
        for u in normalized:
            bot_id = u["bot_id"]
            if bot_id not in by_bot:
                by_bot[bot_id] = []
                bot_order.append(bot_id)
            by_bot[bot_id].append(u)

        results: list[dict] = []
        applied = 0
        catalog_added_total = 0

        for bot_id in bot_order:
            bot_updates = by_bot[bot_id]
            cfg = _cfg_get(bot_id)
            if not cfg:
                # Whole-bot failure: emit one result per update for this
                # bot with the same error so the UI can show per-row
                # status without inferring it.
                for u in bot_updates:
                    results.append({
                        "bot_id": bot_id, "tier": u["tier"],
                        "provider": u["provider"], "success": False,
                        "error": "could not read bot config",
                    })
                continue

            # Stage ONLY the tiers this bot's updates touch — never the full
            # synthesized dict (a sibling sharing a rung would clobber the edit;
            # see model_tier_apply). Same-tier updates stack on the prior stage.
            from .model_tier_apply import model_landed, record_swap, stage_tier_model, tier_models
            prev_by_tier = {u["tier"]: tier_models(cfg, u["tier"]) for u in bot_updates}
            src_tiers = dict(cfg.get("tiers") or {})
            catalog = list(cfg.get("catalog") or [])
            catalog_added_for_bot: list[str] = []
            changed_tiers: dict[str, dict] = {}

            for u in bot_updates:
                tier = u["tier"]
                base = changed_tiers.get(tier) or src_tiers.get(tier)
                changed_tiers[tier] = stage_tier_model(base, u["provider"], u["model"])
                if u["model"] not in catalog:
                    catalog.append(u["model"])
                    catalog_added_for_bot.append(u["model"])

            write_result, write_err = _cfg_set(
                bot_id, {"tiers": changed_tiers, "catalog": catalog}
            )
            if not write_result:
                for u in bot_updates:
                    results.append({
                        "bot_id": bot_id, "tier": u["tier"],
                        "provider": u["provider"], "success": False,
                        "error": write_err or "write failed",
                    })
                continue

            # Truthfulness guard: verify each update against the post-write
            # tiers — a row that didn't take (rung clobber / silent non-persist)
            # is reported as a failure, not a false success.
            bot_applied = 0
            for u in bot_updates:
                tier = u["tier"]
                if model_landed(write_result, tier, u["model"]):
                    record_swap(network_path, bot_id, tier, u["provider"], prev_by_tier.get(tier),
                                tier_models(write_result, tier), source="admin_ui_bulk")
                    results.append({"bot_id": bot_id, "tier": tier,
                                    "provider": u["provider"], "success": True})
                    applied += 1
                    bot_applied += 1
                else:
                    results.append({
                        "bot_id": bot_id, "tier": tier,
                        "provider": u["provider"], "success": False,
                        "error": (
                            f"{u['model']} not present in {tier} after write — "
                            f"did not persist"
                        ),
                    })
            if bot_applied:
                catalog_added_total += len(catalog_added_for_bot)

            if bot_applied:
                _module._audit_log_entry(
                    "models.tier.update.bulk", bot_id,
                    {
                        "update_count": len(bot_updates),
                        "applied": bot_applied,
                        "tiers": sorted({u["tier"] for u in bot_updates}),
                        "providers": sorted({u["provider"] for u in bot_updates}),
                        "catalog_added": catalog_added_for_bot,
                    },
                )

        failed = len(results) - applied
        return jsonify({
            "ok": failed == 0,
            "applied": applied,
            "failed": failed,
            "total": len(updates),
            "bots_touched": len(bot_order),
            "catalog_added_count": catalog_added_total,
            "results": results,
        })

    # Easy-setup wizard — moved to its own register module when the
    # auto-upgrade toggle landed (this file is no-growth capped). Registered
    # here, after the two credentialed-provider closures exist, so there is
    # exactly one implementation of each.
    from .routes_easy_setup import register_easy_setup_routes
    register_easy_setup_routes(
        app, network_path,
        pod_credentialed_providers=_pod_credentialed_providers,
        bot_providers_with_keys=_bot_providers_with_keys,
    )
