"""HTTP routes for the bot configuration surface.

GET  /api/bot                          primary bot openclaw.json
PATCH /api/bot                         partial-update openclaw.json
GET  /api/bot/model-status             per-bot model/routing status
POST /api/bot/model                    set model for a bot
GET  /api/bots/<bot_id>/config         raw openclaw.json for any bot
PATCH /api/bots/<bot_id>/config        update openclaw.json fields
GET  /api/bots/<bot_id>/config/diff    diff between current and deployed config
POST /api/bots/<bot_id>/config/push    push config changes to bot
GET  /api/bots/<bot_id>/embedding      embedding configuration
POST /api/bots/<bot_id>/embedding      update embedding configuration
GET  /api/bots/<bot_id>/heal           heal configuration
POST /api/bots/<bot_id>/heal           update heal configuration
... and related /api/bot/* /api/bots/* configuration endpoints.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, Response

from ..config import load_network, save_network, get_bot_user
from .routes_shared import _audit_log_entry
from .server import _write_json_sudo_fallback, resolve_bot_paths
from ..telemetry import get_logger

_log = get_logger("web.routes_bot_config")

def register_bot_config_routes(app: Flask, network_path: Path) -> None:
    """Register /api/bot/* routes for reading the primary bot's openclaw.json."""
    import copy

    def _primary_user() -> str | None:
        cfg = load_network(network_path)
        primary = cfg.get("primary", "")
        if not primary:
            members = cfg.get("members", [])
            if members:
                primary = members[0]
        if not primary:
            return None
        bots = cfg.get("bots", {})
        return bots.get(primary, {}).get("user") or primary

    def _mask_secrets(data: Any) -> Any:
        data = copy.deepcopy(data)
        secret_keys = {"token", "apikey", "api_key", "key", "secret", "password", "bottoken", "bot_token"}

        def _mask(obj: Any) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k.lower().replace("_", "") in secret_keys and isinstance(v, str) and v:
                        obj[k] = v[:4] + "***" if len(v) > 4 else "***"
                    else:
                        _mask(v)
            elif isinstance(obj, list):
                for item in obj:
                    _mask(item)

        _mask(data)
        return data

    def _read_oc_json(user: str) -> dict | None:
        oc_json = Path(f"/Users/{user}/.openclaw/openclaw.json")
        # Direct read (ACL gives evolve access); fallback to sudo /bin/cat as root
        try:
            return json.loads(oc_json.read_text())
        except PermissionError:
            pass
        except (json.JSONDecodeError, OSError):
            return None
        try:
            import subprocess as _sp
            r = _sp.run(["sudo", "/bin/cat", str(oc_json)],
                        capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except Exception:
            pass
        return None

    @app.get("/api/bot/config")
    def api_bot_config():
        bot_name = request.args.get("bot")
        user = _user_for_bot(bot_name) if bot_name else _primary_user()
        if not user:
            return jsonify({"error": "No primary bot configured"}), 404
        data = _read_oc_json(user)
        if data is None:
            return jsonify({"error": f"openclaw.json not found for user {user}"}), 404
        return jsonify(_mask_secrets(data))

    def _user_for_bot(bot_name: str | None) -> str | None:
        """Resolve bot name to system user. Falls back to bot_name if user key is null."""
        cfg = load_network(network_path)
        if bot_name:
            bots = cfg.get("bots", {})
            # user key may be null in network.json — fall back to bot_name as username
            return bots.get(bot_name, {}).get("user") or bot_name
        return _primary_user()

    @app.get("/api/bot/models")
    def api_bot_models():
        bot_name = request.args.get("bot")
        user = _user_for_bot(bot_name)
        if not user:
            return jsonify({"error": "No primary bot configured"}), 404
        data = _read_oc_json(user)
        if data is None:
            return jsonify({"error": "openclaw.json not found"}), 404
        agents = data.get("agents", {})
        defaults = agents.get("defaults", {})
        raw_model = defaults.get("model", "")
        # model may be a dict like {"primary": "model-name"} or a plain string
        if isinstance(raw_model, dict):
            primary_model = raw_model.get("primary") or next(iter(raw_model.values()), "") or ""
        else:
            primary_model = raw_model
        per_agent = {
            k: v.get("model")
            for k, v in agents.items()
            if k != "defaults" and isinstance(v, dict) and "model" in v
        }
        return jsonify({
            "primary": primary_model,
            "fallbacks": defaults.get("modelFallbacks", []),
            "per_agent": per_agent,
        })

    @app.get("/api/bot/agents-defaults")
    def api_bot_agents_defaults():
        """Return agents.defaults read directly from the bot's openclaw.json
        (the authoritative source). The openclaw CLI rejects configs carrying
        Evolve-specific fields, so we never round-trip through it here.
        Used for tier assignments, compaction config, and fallback chain.
        """
        bot_name = request.args.get("bot")
        if not bot_name:
            # Use first bot in network as default
            bots = list(load_network(network_path).get("bots", {}).keys())
            bot_name = bots[0] if bots else None
        if not bot_name:
            return jsonify({"error": "No bot specified"}), 400
        # Read directly from openclaw.json — skip the CLI entirely.
        # The openclaw CLI rejects configs that contain Evolve-specific fields
        # (agents.defaults.model.tiers, routing, etc.) with "Config invalid",
        # so we cannot use `openclaw config get agents.defaults` once those
        # fields are written. Direct file read is always correct here.
        user = _user_for_bot(bot_name)
        data = _read_oc_json(user) or {}
        defaults = data.get("agents", {}).get("defaults", {})
        return jsonify(defaults)

    @app.get("/api/bot/compaction")
    def api_bot_compaction():
        bot_name = request.args.get("bot")
        user = _user_for_bot(bot_name)
        if not user:
            return jsonify({"error": "No primary bot configured"}), 404
        data = _read_oc_json(user)
        if data is None:
            return jsonify({"error": "openclaw.json not found"}), 404
        agents = data.get("agents", {})
        defaults = agents.get("defaults", {})
        return jsonify(defaults.get("compaction", {}))

    @app.get("/api/bot/routing")
    def api_bot_routing():
        """Return current tier-routing config.
        tier_options always contains tier1, tier2, tier3 with models arrays.
        Resolution after the 2026-05-25 simplification:
          1. per-bot evolve-tiers.json via bot_tier_models() (the file
             AI Optimization writes through oc_model.py config set)
          2. per-bot openclaw.json via oc_model_get (primary → tier2,
             fallback_order → tier3) — for bots that haven't been
             touched by AI Optimization
        catalog from oc_model_get is returned as a top-level key.

        The pre-2026-05-25 ``network.json::models.tiers`` "global defaults"
        seed layer was retired (#1541); the network.json::models
        .tier_assignments read path was retired (#1544) when it became
        clear no UI flow had written there since the AI Optimization
        rewrite. Today: one writer, one reader, one file.
        """
        bot_name = request.args.get("bot")
        net = load_network(network_path)
        models_cfg = net.get("models", {})
        routing = models_cfg.get("routing", {})

        _tier_names = {"tier1": "Premium", "tier2": "Workhorse", "tier3": "Grunt"}

        # Always start with all three tiers (tier1 defaults to empty)
        tier_options: dict = {
            "tier1": {"models": [], "name": "Premium", "description": "Premium tier (optional)", "primary": ""},
            "tier2": {"models": [], "name": "Workhorse", "description": "Workhorse tier", "primary": ""},
            "tier3": {"models": [], "name": "Grunt", "description": "Grunt tier", "primary": ""},
        }

        # Fetch per-bot model data via the runtime seam (reads full openclaw.json)
        oc_m = None
        catalog: list = []
        if bot_name:
            try:
                from runtime.agent_runtime import get_runtime
                oc_m = get_runtime().model_get(bot_name)
            except Exception:
                pass

            if isinstance(oc_m, dict):
                catalog = oc_m.get("catalog", [])
                primary_model = oc_m.get("primary", "")
                fallback_models = oc_m.get("fallback_order", [])

                # Derive tier2/tier3 from openclaw.json (overrides global tiers)
                if primary_model:
                    tier_options["tier2"]["models"] = [primary_model]
                    tier_options["tier2"]["primary"] = primary_model.replace("anthropic/", "")
                    tier_options["tier2"]["source"] = "openclaw.json agents.defaults.model.primary"
                if fallback_models:
                    tier_options["tier3"]["models"] = fallback_models
                    tier_options["tier3"]["primary"] = fallback_models[0].replace("anthropic/", "")
                    tier_options["tier3"]["source"] = "openclaw.json agents.defaults.model.fallbacks"
            else:
                # Last-resort: direct file read
                user = _user_for_bot(bot_name)
                oc_data = _read_oc_json(user) or {}
                defaults = oc_data.get("agents", {}).get("defaults", {})
                model_cfg = defaults.get("model", {})
                if isinstance(model_cfg, dict):
                    primary_model = model_cfg.get("primary", "")
                    fallback_models = model_cfg.get("fallbacks", [])
                    if primary_model:
                        tier_options["tier2"]["models"] = [primary_model]
                        tier_options["tier2"]["primary"] = primary_model.replace("anthropic/", "")
                    if fallback_models:
                        tier_options["tier3"]["models"] = fallback_models
                        tier_options["tier3"]["primary"] = fallback_models[0].replace("anthropic/", "")
                    # Synthesize catalog from primary + fallbacks (agents.defaults.models may be absent)
                    seen: set = set()
                    for m in ([primary_model] if primary_model else []) + fallback_models:
                        if m and m not in seen:
                            seen.add(m)
                            catalog.append(m)
                elif model_cfg:
                    tier_options["tier2"]["models"] = [str(model_cfg)]
                    tier_options["tier2"]["primary"] = str(model_cfg).replace("anthropic/", "")
                    catalog = [str(model_cfg)]

            # Apply per-bot tier definitions from evolve-tiers.json
            # (highest priority — the canonical AI Optimization source).
            # bot_tier_models reads ~/.openclaw/evolve-tiers.json for the
            # given bot; falls through to the openclaw.json-derived
            # tier2/tier3 set above when nothing is configured there.
            try:
                from primary_bot import bot_tier_models as _btm  # type: ignore
            except ImportError:
                _btm = None  # type: ignore
            if _btm is not None:
                for tid in ("tier1", "tier2", "tier3"):
                    assigned = _btm(net, bot_name, tid)
                    if assigned:
                        tier_options[tid]["models"] = assigned
                        tier_options[tid]["primary"] = (assigned[0] if assigned else "").replace("anthropic/", "")
                        tier_options[tid]["source"] = "evolve-tiers.json"

        # Ensure primary field is always set from models[0] if missing
        for tid, tobj in tier_options.items():
            if not tobj.get("primary") and tobj.get("models"):
                tobj["primary"] = tobj["models"][0].replace("anthropic/", "")
            tobj["name"] = _tier_names.get(tid, tid)

        return jsonify({
            "enabled": routing.get("enabled", False),
            "productiveTier": routing.get("productiveTier", "tier2"),
            "maintenanceTier": routing.get("maintenanceTier", "tier3"),
            "backgroundTier": routing.get("backgroundTier", "tier3"),
            "ambiguousTier": routing.get("ambiguousTier", "tier2"),
            "tier_options": tier_options,
            "catalog": catalog,
            "source": "network.json models.routing",
        })

    @app.get("/api/classifier/keywords")
    def api_classifier_keywords() -> Response:
        """Return the keyword vocabulary the session classifier uses.

        Reads TIER1_KEYWORDS / TIER2_KEYWORDS / CORRECTION_PATTERNS from
        plugin/src/observer/TierClassifier.ts (the runtime source of
        truth) and the calibration deltas at
        {sharedDir}/calibration/classifier.json. The `bot=` query param
        is accepted for forward compatibility but is ignored today —
        calibration is pod-wide; per-bot calibration is a planned
        follow-up.
        """
        import re
        repo_root = Path(__file__).resolve().parents[4]
        ts_path = repo_root / "packages" / "plugin" / "src" / "observer" / "TierClassifier.ts"

        def _parse_array_literal(name: str, src: str) -> list[str]:
            # Match: const NAME...= [ ... ];   OR   export const NAME...= [ ... ];
            m = re.search(
                rf"(?:export\s+)?const\s+{re.escape(name)}\s*:\s*string\[\]\s*=\s*\[(.*?)\];",
                src, re.DOTALL,
            )
            if not m:
                return []
            body = m.group(1)
            # Strip line comments, then collect "..."-quoted strings
            body = re.sub(r"//[^\n]*", "", body)
            return re.findall(r'"((?:[^"\\]|\\.)*)"', body)

        try:
            ts_src = ts_path.read_text()
        except Exception as e:
            return jsonify({"error": f"TierClassifier.ts not readable at {ts_path}: {e}"}), 500

        productive = _parse_array_literal("TIER1_KEYWORDS", ts_src)
        maintenance = _parse_array_literal("TIER2_KEYWORDS", ts_src)
        correction = _parse_array_literal("CORRECTION_PATTERNS", ts_src)

        # Calibration deltas — today pod-wide, per-bot is planned
        net = load_network(network_path)
        _shared = Path(net.get("sharedDir", "/Users/Shared/evolve"))
        cal_path = _shared / "calibration" / "classifier.json"
        calibration: dict = {}
        try:
            calibration = (json.loads(cal_path.read_text()) or {}).get("classifier", {})
        except Exception:
            calibration = {}

        # Also surface the (legacy, hand-edited) network.json classifierHints
        hints = net.get("classifierHints") or {}

        return jsonify({
            "base": {
                "productive": productive,
                "maintenance": maintenance,
                "correction": correction,
            },
            "calibration": {
                "productive_add": calibration.get("productive_keywords_add") or [],
                "productive_remove": calibration.get("productive_keywords_remove") or [],
                "maintenance_add": calibration.get("maintenance_keywords_add") or [],
                "maintenance_remove": calibration.get("maintenance_keywords_remove") or [],
                "correction_add": calibration.get("correction_patterns_add") or [],
                "correction_remove": calibration.get("correction_patterns_remove") or [],
            },
            "network_hints": {
                "productive_extra": hints.get("productive_extra") or [],
                "maintenance_extra": hints.get("maintenance_extra") or [],
            },
            "scope": "pod-wide",
            "calibration_writer_implemented": False,
            "source_file": "packages/plugin/src/observer/TierClassifier.ts",
        })

    @app.get("/api/admin/engine-tier-override")
    def api_admin_engine_tier_override_get() -> Response:
        """Read the pod-wide engine-default-tier override.

        Returns ``{"engine_default_tier": "tier3" | null}``. When null,
        engine-side (no bot_id) ``resolve_tier`` calls honor the caller's
        requested tier as before. When set, all engine calls collapse
        to that tier — cost-control knob for background Evolve LLM
        work that shouldn't burn tier1 budget.

        Storage: ``network.json::cascade.engine_default_tier``.
        """
        net = load_network(network_path)
        cascade = net.get("cascade") if isinstance(net, dict) else None
        forced = None
        if isinstance(cascade, dict):
            v = cascade.get("engine_default_tier")
            if isinstance(v, str) and v in ("tier0", "tier1", "tier2", "tier3"):
                forced = v
        return jsonify({"engine_default_tier": forced})

    @app.put("/api/admin/engine-tier-override")
    def api_admin_engine_tier_override_put() -> Response:
        """Set or clear the engine-default-tier override.

        Body: ``{"engine_default_tier": "tier3"}`` to set, or
        ``{"engine_default_tier": null}`` to clear.

        Validates the value is null or one of the four tier IDs. Writes
        network.json atomically.
        """
        body = request.get_json(silent=True) or {}
        new_val = body.get("engine_default_tier")
        if new_val is not None and new_val not in (
            "tier0", "tier1", "tier2", "tier3",
        ):
            return jsonify({
                "error": f"engine_default_tier must be null or tier0/tier1/tier2/tier3 — got {new_val!r}",
            }), 400
        net = load_network(network_path)
        if not isinstance(net.get("cascade"), dict):
            net["cascade"] = {}
        if new_val is None:
            net["cascade"].pop("engine_default_tier", None)
        else:
            net["cascade"]["engine_default_tier"] = new_val
        save_network(net, network_path)
        return jsonify({"engine_default_tier": new_val, "saved": True})

    @app.get("/api/models/tier-resolution")
    def api_models_tier_resolution() -> Response:
        """Tier resolution preview for engine background LLM calls.

        Engine code (analyzer / scanner / help-bot / spec-extractor)
        calls ``resolve_tier(tier, network_config)`` without a bot_id;
        that resolver uses the primary bot's per-tier models from
        ``~/.openclaw/evolve-tiers.json`` (where AI Optimization → Save
        Tiers writes), falling back to credential-aware derived defaults
        (2026-06-07: ``derive_default_tiers(available_providers)``)
        when the primary bot has no per-tier config. The pre-2026-06-07
        ``DEFAULT_TIERS`` constant remains only as a last-resort safety
        net when no auth-profiles are readable.

        Per tier ``source`` is one of:
          - ``"primary bot tier_assignments"`` — the primary bot's
            per-tier model list saved by AI Optimization (highest);
            historical label kept for UI compatibility, even though the
            actual storage is evolve-tiers.json, not the retired
            network.json::models.tier_assignments
          - ``"DEFAULT_TIERS"`` — hardcoded fallback

        Note: the pre-2026-05-25 ``network.json::models.tiers`` pod-wide
        override layer was retired (#1541). The
        ``network.json::models.tier_assignments`` read path was retired
        (#1544) when it became clear no UI flow had written there since
        the AI Optimization rewrite — the Tier Resolution card showed
        every tier as ``default`` even when operators had configured
        non-default models. The ``pod_override`` field this endpoint
        used to return is gone with the layer.
        """
        try:
            from models import (  # type: ignore
                DEFAULT_TIERS,
                derive_default_tiers,
                engine_default_tier_from_network,
            )
            from primary_bot import primary_bot_id, primary_bot_tier_models  # type: ignore
        except Exception as e:
            return jsonify({"error": f"analyzer module not importable: {e}"}), 500

        net = load_network(network_path)
        primary_id = primary_bot_id(net)

        # Discover which LLM providers the primary bot has credentials
        # for (engine-side calls run with the primary bot's identity).
        # Used to derive credential-aware defaults when the primary
        # bot has no per-tier config — protects pods that lack an
        # Anthropic credential from the old DEFAULT_TIERS' pinned
        # anthropic/* picks. Fail-empty on any error → falls back to
        # the hardcoded DEFAULT_TIERS via derive_default_tiers().
        available_providers: set[str] = set()
        # Raw credentialed-provider set (presence of a usable key, by provider
        # field — NOT filtered against a provider-name literal). Passed to
        # resolve_roles_with_provenance, which intersects it against the
        # LLM-capable set DERIVED from the catalog rungs (spec §Addendum3.B —
        # three-homes rule; no _KNOWN_LLM_PROVIDERS literal in availability
        # logic). ``None`` means "could not read credentials" → availability
        # fails open (no role spuriously grayed).
        credentialed_providers: set[str] | None = None
        try:
            if primary_id:
                from evolve_admin.provisioning import _read_auth_profile_providers  # type: ignore
                user = get_bot_user(primary_id, net)
                all_providers = _read_auth_profile_providers(user)
                credentialed_providers = {str(p).lower() for p in all_providers}
                # Legacy derive_default_tiers still consumes the literal-filtered
                # set (it predates the catalog-derived LLM-capable set).
                from models import _KNOWN_LLM_PROVIDERS  # type: ignore
                available_providers = {p for p in credentialed_providers if p in _KNOWN_LLM_PROVIDERS}
        except Exception:
            available_providers = set()
            credentialed_providers = None

        derived_defaults = derive_default_tiers(available_providers)

        # Engine-default-tier override — when set, the engine-side
        # resolve_tier() collapses all requests to this tier. Surface
        # the override status alongside the per-tier resolution so the
        # UI can show "tier1 — Power — claude-opus-4-7 (overridden to
        # tier3 for engine calls)" with full context.
        forced_engine_tier = engine_default_tier_from_network(net)

        result: dict = {}
        for tier_id in ("tier0", "tier1", "tier2", "tier3"):
            default = derived_defaults.get(tier_id, {})
            merged = dict(default)

            # Highest priority: primary bot's tier_assignments
            primary_assigned = primary_bot_tier_models(net, tier_id)
            if primary_assigned:
                merged = {**merged, "models": primary_assigned}
                source = f"primary bot tier_assignments ({primary_id})"
            elif available_providers:
                source = "derived_defaults"
            else:
                source = "DEFAULT_TIERS"

            primary_models = merged.get("models") or []
            fallbacks = merged.get("fallbacks") or []
            result[tier_id] = {
                "name": merged.get("name", tier_id),
                "primary": primary_models[0] if primary_models else None,
                "models": primary_models,
                "fallbacks": fallbacks,
                "policy": merged.get("policy"),
                "costClass": merged.get("costClass"),
                "source": source,
            }

        # Add the max (fable-class) rung when present in network.json rungs[].
        # Max is pull-only (spec-model-rungs-and-roles §max semantics) and
        # configured pod-wide in network.json::models.rungs, not per-bot.
        rungs = (net.get("models") or {}).get("rungs") or []
        roles = (net.get("models") or {}).get("roles") or {}
        fable_rung_id = (roles.get("max") or "fable-class") if isinstance(roles.get("max"), str) else "fable-class"
        fable_rung = next(
            (r for r in rungs if isinstance(r, dict) and r.get("id") == fable_rung_id),
            None,
        )
        if fable_rung:
            fable_models = fable_rung.get("models") or []
            result["max"] = {
                "name": "Max (Fable-class)",
                "primary": fable_models[0] if fable_models else None,
                "models": fable_models,
                "fallbacks": [],
                "policy": None,
                "costClass": fable_rung.get("costClass", "premium"),
                "source": "network_rungs",
            }

        # All-five-roles resolution with winning-layer provenance
        # (spec §Addendum 2.3). Resolves fast/standard/power/max through
        # the defaults ← pod ← bot merge and tags each with the layer
        # (default / pod / bot) that decided it. The engine view has no per-bot
        # layer, so the primary bot supplies the "bot" layer here — it is the
        # identity engine background calls run as.
        roles_view: dict = {}
        try:
            from primary_bot import resolve_roles_with_provenance  # type: ignore
            roles_view = resolve_roles_with_provenance(
                net, primary_id, credentialed_providers=credentialed_providers
            )
        except Exception as exc:
            _log.warning(
                "engine config: role resolution failed for primary %s, "
                "roles view falls back to empty: %s", primary_id, exc,
            )
            roles_view = {}

        return jsonify({
            "tiers": result,
            "roles": roles_view,
            "primary_bot": primary_id,
            "available_providers": sorted(available_providers),
            "engine_default_tier": forced_engine_tier,
        })

    # ``PUT /api/admin/bot-tiers`` retired 2026-05-25 along with the
    # network.json::models.tier_assignments storage location it wrote
    # to. No UI flow ever called this endpoint after the AI Optimization
    # rewrite — operators set tier models via
    # ``PUT /api/admin/config/<bot>/tiers`` (writes to
    # ~/.openclaw/evolve-tiers.json), and the Tier Resolution card
    # reads from that same file via bot_tier_models() in
    # primary_bot.py. Keeping a dead write path to the wrong location
    # is a maintainability footgun, so it's gone. See PR #1544.

    # ── Embedding-provider config ────────────────────────────────────────────
    # Embeddings are a separate provider stack from chat models (different model
    # families, different vendors). The two endpoints below mirror the tier API:
    # GET resolves the chain + lists what's available; PUT saves to network.json
    # AND writes agents.defaults.memorySearch into the bot's openclaw.json so
    # OpenClaw can fail over without evolve in the loop.

    @app.get("/api/admin/embedding-config/<bot_id>")
    def api_admin_embedding_config_get(bot_id: str) -> Response:
        from embeddings import (  # type: ignore
            EMBEDDING_PROVIDERS,
            DEFAULT_EMBEDDING_CHAIN,
            configured_embedding_providers,
            embedding_chain_warning,
            model_for_provider,
            providers_from_auth_profiles,
            resolve_embedding_chain,
        )

        # Real evolve installs keep credentials per-bot in auth-profiles.json,
        # not in network.json models.providers (which is typically empty).
        # Read the bot's actual auth-profiles to determine what's available.
        # The canonical _read_auth_profiles helper is a closure inside
        # create_app that we can't reach from this registry function, so
        # we replicate its happy-path here: ACL gives evolve direct read on
        # .openclaw/, and unreadable files just yield "no providers" — which
        # is the safer failure mode (nothing falsely marked as configured).
        cred_ids: set = set()
        try:
            paths = resolve_bot_paths(bot_id)
            ap_path = paths.get("auth_profiles") if isinstance(paths, dict) else None
            if ap_path:
                try:
                    raw_text = Path(ap_path).read_text()
                except (FileNotFoundError, PermissionError, OSError):
                    raw_text = ""
                    try:
                        proc = subprocess.run(
                            ["sudo", "/bin/cat", ap_path],
                            capture_output=True, text=True, timeout=5,
                        )
                        if proc.returncode == 0:
                            raw_text = proc.stdout
                    except Exception:
                        pass
                if raw_text.strip():
                    try:
                        cred_ids = providers_from_auth_profiles(json.loads(raw_text))
                    except Exception:
                        pass
        except Exception:
            pass

        net = load_network(network_path)
        embedding_block = net.get("models", {}).get("embedding", {}) or {}
        per_bot = embedding_block.get("per_bot", {}).get(bot_id, {}) or {}
        per_bot_chain = per_bot.get("chain")
        pod_default = embedding_block.get("default_chain") or list(DEFAULT_EMBEDDING_CHAIN)
        resolved = resolve_embedding_chain(net, bot_id=bot_id, credential_provider_ids=cred_ids)
        warning = embedding_chain_warning(net, bot_id=bot_id, credential_provider_ids=cred_ids)
        configured = set(configured_embedding_providers(net, cred_ids))

        # Provider rows for the picker. Each row carries the data the UI needs
        # to render (label, model, capabilities) plus an `is_configured` flag
        # that determines whether the row is selectable or grayed out.
        providers = []
        for pid, prov in EMBEDDING_PROVIDERS.items():
            providers.append({
                "id": pid,
                "label": prov.label,
                "default_model": prov.default_model,
                "model": model_for_provider(pid, net),
                "needs_api_key": prov.needs_api_key,
                "credential_keys": list(prov.credential_keys),
                "capabilities": sorted(prov.capabilities),
                "notes": prov.notes,
                "is_configured": pid in configured,
            })
        providers.sort(key=lambda p: (not p["is_configured"], p["id"]))

        return jsonify({
            "bot": bot_id,
            "resolved_chain": resolved,
            "per_bot_chain": per_bot_chain,
            "pod_default_chain": pod_default,
            "providers": providers,
            "warning": warning,
        })

    @app.put("/api/admin/embedding-config/<bot_id>")
    def api_admin_embedding_config_set(bot_id: str) -> Response:
        from embeddings import EMBEDDING_PROVIDERS  # type: ignore
        from runtime.agent_runtime import get_runtime

        body = request.get_json() or {}
        chain = body.get("chain")
        if not isinstance(chain, list) or not all(isinstance(p, str) for p in chain):
            return jsonify({"error": "chain must be a list of provider ids"}), 400
        # Validate every provider id is embedding-capable. Reject early so we
        # don't write a chain to network.json that the resolver will silently
        # filter out at runtime.
        unknown = [p for p in chain if p not in EMBEDDING_PROVIDERS]
        if unknown:
            return jsonify({"error": f"unknown embedding providers: {unknown}"}), 400

        # Save to network.json — empty chain clears the per-bot override.
        net = load_network(network_path)
        net.setdefault("models", {}).setdefault("embedding", {}).setdefault("per_bot", {})
        if chain:
            net["models"]["embedding"]["per_bot"][bot_id] = {"chain": chain}
        else:
            net["models"]["embedding"]["per_bot"].pop(bot_id, None)
        try:
            import json as _json
            network_path.write_text(_json.dumps(net, indent=2))
        except Exception as e:
            return jsonify({"error": f"Failed to save network.json: {e}"}), 500

        # Push into the bot's openclaw.json so OpenClaw fails over natively.
        # OC schema is {provider, fallback} — first two entries of the chain.
        primary = chain[0] if chain else ""
        fallback = chain[1] if len(chain) > 1 else None
        ok = get_runtime().memory_set(bot_id, primary, fallback)
        if not ok:
            return jsonify({"error": "memory_set failed — check server logs"}), 500

        _audit_log_entry("embedding.set", bot_id, {"chain": chain}, oc_keys={"agents"})
        return jsonify({"ok": True, "bot": bot_id, "chain": chain})

    # POST /api/bot/routing was retired (#3662 follow-up): it wrote
    # untranslated ``*Tier`` keys into network.json ``models.routing`` — the
    # POD-WIDE fallback both plugin load seams read (``tiersFile.routing ??
    # network.models?.routing``) — a shape the plugin runtime now refuses
    # wholesale (LegacyTierShapeError), so one curl could poison routing for
    # every bot without its own evolve-tiers.json block. It had no caller;
    # the routing card writes per-bot via PUT /api/admin/config/<bot>/routing,
    # which translates at the oc_model write boundary. The GET above only
    # reads and stays.

    @app.get("/api/bot/model-in-use")
    def api_bot_model_in_use():
        """
        Detect the actual model each bot is using, including source attribution.
        Checks (in order): openclaw.json agents.defaults.model → network.json tiers → fallback.
        Also checks recent tier-usage data to see if the model has been confirmed live.
        """
        import datetime as _dt
        bot_name = request.args.get("bot")
        user = _user_for_bot(bot_name)
        net = load_network(network_path)
        tiers = net.get("models", {}).get("tiers", {})

        result: dict = {}

        def _model_for_bot(bid: str) -> dict:
            bots_cfg = net.get("bots", {})
            usr = bots_cfg.get(bid, {}).get("user") or bid
            oc = _read_oc_json(usr) or {}
            agents = oc.get("agents", {})
            defaults = agents.get("defaults", {})
            raw = defaults.get("model", "")
            if isinstance(raw, dict):
                model = raw.get("primary") or next(iter(raw.values()), "") or ""
            else:
                model = raw or ""

            source = "openclaw.json" if model else None
            source_path = f"/Users/{usr}/.openclaw/openclaw.json" if model else None

            # Fallback: tier2 primary model from network.json
            if not model:
                t2 = (tiers.get("tier2", {}).get("models") or [""])[0].replace("anthropic/", "")
                if t2:
                    model = t2
                    source = "network.json tiers.tier2"
                    source_path = str(network_path)

            # Check if model has been confirmed via live tier-usage data in last 7 days
            shared = Path(net.get("sharedDir", "/Users/Shared/evolve"))
            confirmed_at: str | None = None
            cutoff = (_dt.date.today() - _dt.timedelta(days=7)).isoformat()
            tier_dir = shared / "cost" / "tier-usage" / bid
            if tier_dir.exists():
                for jf in sorted(tier_dir.glob("*.jsonl"), reverse=True):
                    if jf.stem < cutoff:
                        break
                    for line in jf.read_text().splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            if rec.get("model", "").replace("anthropic/", "") == model.replace("anthropic/", ""):
                                confirmed_at = rec.get("ts", jf.stem)
                                break
                        except Exception:
                            pass
                    if confirmed_at:
                        break

            # Fallback chain from routing config
            routing = net.get("models", {}).get("routing", {})
            maint_tier = routing.get("maintenanceTier", "tier3")
            maint_model = (tiers.get(maint_tier, {}).get("models") or [""])[0].replace("anthropic/", "")
            fallback_chain = [
                {"session": "productive", "tier": "tier2", "model": (tiers.get("tier2", {}).get("models") or [""])[0].replace("anthropic/", "")},
                {"session": "maintenance", "tier": maint_tier, "model": maint_model},
                {"session": "ambiguous", "tier": "tier2", "model": (tiers.get("tier2", {}).get("models") or [""])[0].replace("anthropic/", "")},
            ]

            return {
                "model": model or "unknown",
                "source": source or "unknown",
                "source_path": source_path,
                "confirmed_at": confirmed_at,
                "confirmation_method": "live_metrics" if confirmed_at else "config_only",
                "fallback_chain": fallback_chain,
            }

        if bot_name:
            result[bot_name] = _model_for_bot(bot_name)
        else:
            for bid in net.get("bots", {}):
                result[bid] = _model_for_bot(bid)

        return jsonify(result)

    @app.post("/api/cost/import")
    def api_cost_import():
        """
        Import API usage from CSV/JSON/text log to seed the cost baseline.
        Expects JSON body: {bot_id, format: "csv"|"json"|"text", data: "..."}
        Writes parsed records to shared/cost/{YYYY-MM-DD}.json.
        """
        import datetime as _dt
        body = request.get_json() or {}
        bot_id = body.get("bot_id", "")
        fmt = body.get("format", "json").lower()
        raw_data = body.get("data", "")

        if not raw_data:
            return jsonify({"error": "data field required"}), 400

        net = load_network(network_path)
        shared = Path(net.get("sharedDir", "/Users/Shared/evolve"))
        cost_dir = shared / "cost"
        cost_dir.mkdir(parents=True, exist_ok=True)

        parsed: list[dict] = []
        errors: list[str] = []

        try:
            if fmt == "json":
                obj = json.loads(raw_data)
                if isinstance(obj, list):
                    parsed = obj
                elif isinstance(obj, dict):
                    parsed = [obj]
            elif fmt == "csv":
                import csv, io
                reader = csv.DictReader(io.StringIO(raw_data))
                for row in reader:
                    try:
                        parsed.append({
                            "date": row.get("date", row.get("Date", "")),
                            "cost": float(row.get("cost", row.get("Cost", row.get("amount", 0)))),
                            "model": row.get("model", row.get("Model", "")),
                            "bot_id": bot_id or row.get("bot_id", ""),
                        })
                    except (ValueError, KeyError) as e:
                        errors.append(str(e))
            else:  # text: try to parse "YYYY-MM-DD $X.XX" lines
                for line in raw_data.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parts = line.split()
                        date_str = next((p for p in parts if len(p) == 10 and p[4] == "-"), None)
                        amount = next((float(p.lstrip("$")) for p in parts if p.lstrip("$").replace(".", "").isdigit()), None)
                        if date_str and amount is not None:
                            parsed.append({"date": date_str, "cost": amount, "bot_id": bot_id})
                    except Exception as e:
                        errors.append(str(e))
        except Exception as e:
            return jsonify({"error": f"Parse failed: {e}"}), 400

        if not parsed:
            return jsonify({"error": "No records parsed", "parse_errors": errors}), 400

        # Group by date and write/merge into cost files
        by_date: dict[str, dict] = {}
        for rec in parsed:
            d = rec.get("date", _dt.date.today().isoformat())[:10]
            if d not in by_date:
                by_date[d] = {}
            bid = rec.get("bot_id", bot_id or "imported")
            by_date[d][bid] = by_date[d].get(bid, 0) + float(rec.get("cost", 0))

        written = 0
        for d, bots_cost in by_date.items():
            cost_file = cost_dir / f"{d}.json"
            existing: dict = {}
            if cost_file.exists():
                try:
                    existing = json.loads(cost_file.read_text())
                except Exception:
                    pass
            for bid, amt in bots_cost.items():
                existing.setdefault(bid, {})["total_cost"] = round(
                    existing.get(bid, {}).get("total_cost", 0) + amt, 6
                )
            _write_json_sudo_fallback(cost_file, existing)
            written += 1

        return jsonify({
            "ok": True,
            "records_parsed": len(parsed),
            "dates_written": written,
            "parse_errors": errors,
        })

    # ── Display name (rename) ────────────────────────────────────────────────
    #
    # Wraps upstream `openclaw agents set-identity --name` (OC's own
    # display-name mechanism — see docs.openclaw.ai/cli/agents) and
    # caches the value in network.bots[bot_id].display_name so the
    # admin UI can render it without re-shelling on every refresh.
    #
    # This changes only the bot's display name. The bot_id key in
    # network.json, the OC agent id (typically "main"), the macOS user
    # account, and all file paths stay untouched — same shape as the
    # spec §9 "evo is the display name" guidance.

    _DISPLAY_NAME_MAX = 40

    def _validate_display_name(raw: Any) -> tuple[str | None, str | None]:
        if not isinstance(raw, str):
            return None, "name must be a string"
        cleaned = raw.strip()
        if not cleaned:
            return None, "name is required"
        if len(cleaned) > _DISPLAY_NAME_MAX:
            return None, f"name too long (max {_DISPLAY_NAME_MAX} chars)"
        # Control-character check — protects against accidental newline /
        # tab in pasted names that would render badly across surfaces.
        if any(ord(c) < 32 for c in cleaned):
            return None, "name contains control characters"
        return cleaned, None

    @app.post("/api/bots/<bot_id>/rename")
    def api_bot_rename(bot_id: str) -> Response:
        """Set the bot's display name (what it identifies as in chat
        and the admin UI). Shells `openclaw agents set-identity` so the
        change propagates to OC's IDENTITY.md, then caches in
        network.json.

        Body: ``{"name": str}`` — required.

        Returns:
            200 ``{"ok": true, "bot_id": str, "display_name": str}`` on success.
            400 on invalid name.
            404 when the bot id is not in network.json.
            502 when the upstream OC set-identity subprocess fails.
        """
        body = request.get_json(silent=True) or {}
        new_name, err = _validate_display_name(body.get("name"))
        if err:
            return jsonify({"error": err}), 400

        network = load_network(network_path)
        bots = network.get("bots") or {}
        if bot_id not in bots:
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404

        # OC agent id inside the bot's workspace. v1 hardcodes "main"
        # because every bot in the pod uses the OC default; bots with
        # a non-default agent id would need a discovery step
        # (`openclaw agents list --json` → pick isDefault), deferred.
        oc_agent_id = "main"

        # Import lazily so server-import doesn't depend on the analyzer
        # package being importable at module load (same pattern as
        # _audit_run_one in this file).
        from runtime.agent_runtime import get_runtime

        result = get_runtime().set_identity(
            bot_id, agent_id=oc_agent_id, name=new_name
        )
        if result is None:
            return jsonify({
                "error": (
                    "openclaw agents set-identity failed — "
                    "check gateway logs for this bot"
                ),
            }), 502

        # Cache the new name in network.json. Even if a subsequent
        # admin UI render happens before OC re-loads its identity, the
        # UI now shows the new name. If someone edits IDENTITY.md
        # directly the cache will drift — caller can re-run rename.
        #
        # Legacy network.json sometimes has ``"bots": {"team_bot_a": null}``
        # (member listed but no entry). ``setdefault`` returns the
        # existing None — repair to {} so save_network doesn't choke.
        bot_cfg = bots.get(bot_id)
        if not isinstance(bot_cfg, dict):
            bot_cfg = {}
            bots[bot_id] = bot_cfg
        bot_cfg["display_name"] = new_name

        # If save_network raises (e.g. ``sudo /bin/cp`` fails for the
        # atomic write), OC has already accepted the new name but our
        # cache didn't catch up. Surface the partial state to the
        # caller so the UI can prompt the user to retry. Without
        # this, the bot would identify as "Evo" in chat but the
        # admin UI would still show the old name until cache is
        # refreshed by an unrelated network.json write.
        try:
            save_network(network, network_path)
        except Exception as exc:
            return jsonify({
                "error": (
                    "OC accepted the new name but persisting to "
                    "network.json failed — re-run rename to sync. "
                    f"Cause: {exc}"
                ),
                "oc_applied": True,
                "cache_persisted": False,
                "bot_id": bot_id,
                "display_name": new_name,
            }), 500

        return jsonify({
            "ok": True,
            "bot_id": bot_id,
            "display_name": new_name,
        })

    @app.post("/api/bots/<bot_id>/heal-overrides")
    def api_bot_heal_overrides_set(bot_id: str) -> Response:
        """Set per-bot overrides for the heal daemon's thresholds.

        Body: ``{"heal": {<key>: <int>|null, ...}}``

        Allowlisted keys: failuresBeforeProposal, windowHours,
        slowThresholdMs, restartCooldownMin, checkTimeoutSec,
        ocHealthTimeoutSeconds. Mirrors the pod-wide Self-Healing card
        on Pod Config → Network but scoped to a single bot — the heal
        daemon prefers per-bot values over pod-wide defaults
        (see analyzer/heal.py:_resolve_heal_config).

        ``null`` clears an individual override (fall back to pod-wide).
        When every key is cleared, the whole ``heal`` block is removed
        from bots[bot_id] so a future audit doesn't show empty noise.
        """
        body = request.get_json(silent=True) or {}
        heal_in = body.get("heal", {}) if isinstance(body.get("heal"), dict) else {}
        _HEAL_KEYS = {
            "failuresBeforeProposal", "windowHours",
            "slowThresholdMs", "restartCooldownMin", "checkTimeoutSec",
            "ocHealthTimeoutSeconds",
        }

        network = load_network(network_path)
        bots = network.get("bots") or {}
        if bot_id not in bots:
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404
        bot_cfg = bots.get(bot_id)
        if not isinstance(bot_cfg, dict):
            bot_cfg = {}
            bots[bot_id] = bot_cfg

        existing = dict(bot_cfg.get("heal") or {})
        for k in _HEAL_KEYS:
            if k not in heal_in:
                continue
            v = heal_in[k]
            if v is None:
                existing.pop(k, None)
            else:
                try:
                    existing[k] = int(v)
                except (TypeError, ValueError):
                    existing.pop(k, None)
        if existing:
            bot_cfg["heal"] = existing
        else:
            bot_cfg.pop("heal", None)
        network["bots"] = bots
        save_network(network, network_path)
        return jsonify({"ok": True, "bot_id": bot_id, "heal": bot_cfg.get("heal") or {}})

    # POST /api/bots/<bot_id>/daily-cap-usd removed in Phase 4 of the
    # cost-cap normalization (2026-06). Daily hard cap now lives in
    # better-engine-config; use POST /api/arbiter/bot-setup/<bot_id> with
    # body {"daily_hard_usd": <float>|null} instead.

