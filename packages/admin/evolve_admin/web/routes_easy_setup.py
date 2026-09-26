"""The easy-setup wizard + auto-upgrade toggle routes (spec §Addendum 6 #2,
spec-model-auto-upgrade-2026-07-30 §Scope).

Moved out of ``routes_admin_config.py`` (no-growth-capped file) when the
auto-upgrade toggle landed (spec-model-auto-upgrade-2026-07-30). Same
``register_*_routes`` shape as its siblings; the two credentialed-provider
helpers stay closures in ``routes_admin_config`` and are passed in, so there
is exactly one implementation of each.

§1.3 monkeypatch-at-call-time invariant: handlers reach patchable helpers via
``sys.modules["evolve_admin.web.server"]`` at call time (``_audit_log_entry``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

from evolve_config import CANONICAL_SHARED_DIR  # type: ignore

from flask import Flask, jsonify, request, Response

from ..config import load_network
from .routes_shared import tier_write_oc_keys as _tier_oc_keys
from ..telemetry import get_logger

_log = get_logger("web.routes_easy_setup")


def register_easy_setup_routes(
    app: Flask,
    network_path: Path,
    *,
    pod_credentialed_providers: Callable[[dict], set],
    bot_providers_with_keys: Callable[[str], set],
) -> None:
    """Register ``POST /api/models/easy-setup``.

    The 90% path. One button on the POD default editor and each bot tab:
    "Easy setup" → a ranked provider-order preference → server-side reorders
    every role's DEFAULT cluster by that preference and writes it through the
    SAME safe paths the manual editors use (pod → ``_patch_network_json`` at
    models.* granularity so embedding survives; bot → the tier-mode "custom"
    materialize seam seeded with the wizard result). The compute is the pure
    ``primary_bot.compute_easy_setup_catalog`` — no provider/model literals
    here; the only literal home stays ``DEFAULT_MODEL_CATALOG``.
    """
    _module = sys.modules["evolve_admin.web.server"]

    def _easy_setup_compute(scope: str, provider_order: list) -> "tuple[dict, str | None]":
        """Easy-setup catalog for a scope, filtered to the scope's keys: pod = pod-wide
        union (a template); a bot scope uses its OWN keys, else it seeds a model the bot
        can't run (the stale xai/grok-in-a-non-xAI-bot bug). Preview + write share it."""
        from primary_bot import easy_setup_catalog_for  # type: ignore
        from ..model_catalog import scope_credentialed_to_bot
        net = load_network(network_path)
        cred = pod_credentialed_providers(net) if scope == "pod" else scope_credentialed_to_bot(scope, bot_providers_with_keys, lambda: pod_credentialed_providers(net))
        return easy_setup_catalog_for(net, provider_order, credentialed_providers=cred)

    @app.post("/api/models/easy-setup")
    def api_models_easy_setup() -> "Response | tuple[Response, int]":
        """Easy-setup: reorder every tier's default cluster by a provider
        preference, then write through the existing safe path for the scope.

        Body: ``{"scope": "pod" | "<bot_id>", "provider_order": [<provider>...],
        "preview"?: bool, "auto_upgrade_enabled"?: bool}``.

        - ``preview: true`` — compute + validate only; returns the resulting
          ``{rungs, roles, roleCaps}`` plus the auto-upgrade extras (toggle
          prefill / family stems / pod governance — see ``easy_setup_auto``).
          No write.
        - otherwise — compute, validate, then write. ``scope == "pod"`` splices
          ``models.rungs/roles/roleCaps`` via ``_patch_network_json`` (embedding
          preserved); a bot scope flips the bot to Custom and writes the wizard
          result wholesale through the tiers safe-write seam. A bool
          ``auto_upgrade_enabled`` also updates the scope's ``autoUpgrade``
          block (spec-model-auto-upgrade-2026-07-30 §Config shape; omitted/null
          → untouched; configuration only until the Phase 3 apply path ships).

        Resolution logic is unchanged — this only seeds config the operator could
        have built by hand, ordered by their stated preference.
        """
        body = request.get_json(silent=True) or {}
        scope = body.get("scope")
        provider_order = body.get("provider_order")
        preview = bool(body.get("preview"))
        if not isinstance(scope, str) or not scope:
            return jsonify({"error": "scope must be 'pod' or a bot id"}), 400
        if not isinstance(provider_order, list) or not all(
            isinstance(p, str) and p for p in provider_order
        ):
            return jsonify({"error": "provider_order must be a list of provider strings"}), 400
        auto_upgrade_enabled = body.get("auto_upgrade_enabled")
        if auto_upgrade_enabled is not None and not isinstance(auto_upgrade_enabled, bool):
            # A string "true" is not a decision to change the pod's model
            # config (matches model_auto_upgrade._coerce_policy_block).
            return jsonify({"error": "auto_upgrade_enabled must be a bool"}), 400

        net = load_network(network_path)
        if scope != "pod" and scope not in (net.get("bots") or {}):
            return jsonify({"error": f"unknown bot: {scope}"}), 404

        try:
            catalog, err = _easy_setup_compute(scope, provider_order)
        except Exception as exc:
            _log.warning("easy-setup compute failed (scope=%s): %s", scope, exc)
            return jsonify({"error": f"easy-setup compute failed: {exc}"}), 500
        if err:
            return jsonify({"error": f"easy-setup produced an invalid catalog: {err}"}), 400

        if preview:
            from .easy_setup_auto import preview_extras
            return jsonify({
                "ok": True, "scope": scope, "preview": True, "catalog": catalog,
                **preview_extras(net, scope, catalog),
            })

        rungs = catalog.get("rungs") or []
        roles = catalog.get("roles") or {}
        role_caps = catalog.get("roleCaps")

        _audit_extra = (
            {} if auto_upgrade_enabled is None
            else {"auto_upgrade_enabled": auto_upgrade_enabled}
        )
        if scope == "pod":
            # Same safe path as the manual pod-models PUT: splice each tier key
            # separately so a sibling models.embedding block is preserved.
            try:
                from evolve_config import _patch_network_json  # type: ignore
                _patch_network_json(network_path, ["models", "rungs"], rungs)
                _patch_network_json(network_path, ["models", "roles"], roles)
                if isinstance(role_caps, dict):
                    _patch_network_json(network_path, ["models", "roleCaps"], role_caps)
                if auto_upgrade_enabled is not None:
                    from .easy_setup_auto import pod_auto_upgrade_block
                    _patch_network_json(
                        network_path, ["models", "autoUpgrade"],
                        pod_auto_upgrade_block(net, auto_upgrade_enabled),
                    )
            except Exception as exc:
                _log.warning("easy-setup pod write failed: %s", exc)
                return jsonify({"error": f"easy-setup pod write failed: {exc}"}), 500
            _module._audit_log_entry(
                "config.easy_setup.set", "pod",
                {"provider_order": provider_order, "rung_count": len(rungs),
                 **_audit_extra},
            )
            return jsonify({"ok": True, "scope": "pod", "catalog": catalog})

        # Bot scope: flip to Custom and seed with the wizard result. Same seam
        # the tier-mode "custom" materialize uses — full rungs/roles written
        # wholesale, so the bot now carries its own tiers (intended).
        #
        # The pod's auto-upgrade block rides along automatically: ``oc_cli``
        # injects it on any write that leaves non-empty ``rungs``, and the
        # writer carries it across the not-Custom → Custom flip (lifecycle rule
        # 1 — ``model_auto_upgrade.bot_policy`` does NOT give a Custom bot the
        # pod's ``enabled``, so without the carry this write silently turned
        # auto-upgrade off; #3566 audit E-1). Nothing to do here, but do NOT
        # "simplify" by dropping rungs from the payload shape the seam keys on.
        updates: dict[str, Any] = {"rungs": rungs, "roles": roles}
        updates["roleCaps"] = role_caps if isinstance(role_caps, dict) else {}
        if auto_upgrade_enabled is not None:
            # oc_model partial-merges into the bot's existing autoUpgrade block.
            updates["autoUpgrade"] = {"enabled": auto_upgrade_enabled}
        from runtime.agent_runtime import get_runtime
        _rt = get_runtime()
        result, write_err = _rt.full_config_set_with_error(scope, updates)
        if not result:
            err_suffix = f": {write_err}" if write_err else " (check admin-ui.err.log)"
            return jsonify({
                "error": f"easy-setup write failed for {scope}{err_suffix}",
            }), 500
        _module._audit_log_entry(
            "config.easy_setup.set", scope,
            {"provider_order": provider_order, "rung_count": len(rungs),
             **_audit_extra},
            oc_keys=_tier_oc_keys(result, {"agents"}),
        )
        return jsonify({"ok": True, "scope": scope, "catalog": catalog})

    # ── Top-level auto-upgrade toggle (spec-model-auto-upgrade §Scope) ────────
    # The toggle lives ON the tier-definition cards (pod defaults card + each
    # Custom bot's tier card), not inside the easy-setup modal — these two
    # routes back it. GET is the card's state (policy + governance + family
    # map for the consolidated "family · latest" rows); PUT flips `enabled`
    # for a scope through the same safe write paths easy-setup uses.

    @app.get("/api/models/auto-upgrade")
    def api_models_auto_upgrade_get() -> "Response | tuple[Response, int]":
        """Current auto-upgrade posture for ``?scope=pod|<bot_id>`` (default
        pod): the resolved policy, whether a bot scope owns its toggle
        (``custom_tiers``), the pod-scope governance split, and the
        qualified-id → family-stem map the tier editors use to consolidate
        pinned versions into one "family · latest" row."""
        scope = request.args.get("scope") or "pod"
        net = load_network(network_path)
        if scope != "pod" and scope not in (net.get("bots") or {}):
            return jsonify({"error": f"unknown bot: {scope}"}), 404
        from .easy_setup_auto import scope_auto_upgrade_state
        shared_dir = Path(net.get("sharedDir", CANONICAL_SHARED_DIR))
        try:
            return jsonify(scope_auto_upgrade_state(net, scope, shared_dir))
        except Exception as exc:
            _log.warning("auto-upgrade GET failed (scope=%s): %s", scope, exc)
            return jsonify({"error": f"could not resolve auto-upgrade state: {exc}"}), 500

    @app.put("/api/models/auto-upgrade")
    def api_models_auto_upgrade_set() -> "Response | tuple[Response, int]":
        """Flip auto-upgrade for a scope: ``{"scope": "pod"|"<bot_id>",
        "enabled": bool}``.

        Pod scope merges ``enabled`` into ``network.json::models.autoUpgrade``
        (subordinate knobs survive — :func:`easy_setup_auto.pod_auto_upgrade_block`).
        A bot scope must be Custom (spec §Scope: a Use-defaults bot follows the
        pod toggle and has none of its own) and partial-merges
        ``{"autoUpgrade": {"enabled": ...}}`` into its tiers doc via the same
        config-set seam the tier editors write through.
        """
        body = request.get_json(silent=True) or {}
        scope = body.get("scope")
        enabled = body.get("enabled")
        if not isinstance(scope, str) or not scope:
            return jsonify({"error": "scope must be 'pod' or a bot id"}), 400
        if not isinstance(enabled, bool):
            # A string "true" is not a decision to change the pod's model
            # config (matches model_auto_upgrade._coerce_policy_block).
            return jsonify({"error": "enabled must be a bool"}), 400

        net = load_network(network_path)
        if scope == "pod":
            try:
                from evolve_config import _patch_network_json  # type: ignore
                from .easy_setup_auto import pod_auto_upgrade_block
                _patch_network_json(
                    network_path, ["models", "autoUpgrade"],
                    pod_auto_upgrade_block(net, enabled),
                )
            except Exception as exc:
                _log.warning("auto-upgrade pod write failed: %s", exc)
                return jsonify({"error": f"auto-upgrade pod write failed: {exc}"}), 500
            _module._audit_log_entry(
                "config.auto_upgrade.set", "pod", {"enabled": enabled},
            )
            return jsonify({"ok": True, "scope": "pod", "enabled": enabled})

        if scope not in (net.get("bots") or {}):
            return jsonify({"error": f"unknown bot: {scope}"}), 404
        try:
            from primary_bot import bot_has_custom_tiers  # type: ignore
            is_custom = bot_has_custom_tiers(net, scope)
        except Exception as exc:
            return jsonify({"error": f"could not read tier mode for {scope}: {exc}"}), 500
        if not is_custom:
            return jsonify({
                "error": (
                    f"{scope} follows the pod defaults — its auto-upgrade "
                    "setting rides the pod toggle (switch the bot to Custom "
                    "to give it its own)"
                ),
            }), 400
        from runtime.agent_runtime import get_runtime
        result, write_err = get_runtime().full_config_set_with_error(
            scope, {"autoUpgrade": {"enabled": enabled}},
        )
        if not result:
            err_suffix = f": {write_err}" if write_err else " (check admin-ui.err.log)"
            return jsonify({
                "error": f"auto-upgrade write failed for {scope}{err_suffix}",
            }), 500
        _module._audit_log_entry(
            "config.auto_upgrade.set", scope, {"enabled": enabled},
            oc_keys=_tier_oc_keys(result, {"agents"}),
        )
        return jsonify({"ok": True, "scope": scope, "enabled": enabled})
