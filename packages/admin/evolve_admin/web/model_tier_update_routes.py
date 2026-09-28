"""model_tier_update_routes — the two model-tier freshness write endpoints.

``POST /api/models/update-tier`` (single Apply) and
``POST /api/models/update-tier-bulk`` (Apply All), moved here whole from
``routes_admin_config.py`` (a frozen no-growth hot-hazard file) per the
helper-module convention, so the behavior-pin gate below had somewhere to
live. The write contract itself — stage only the changed tiers, verify the
model landed, record the swap — is unchanged and still comes from
``model_tier_apply``.

**The behavior-pin gate (2026-08-21 recurrence of the 2026-08-14 group-chat
silence incident).** ``evolve-admin models rollback`` reverted the incident
swap, but nothing recorded that the swapped-in model had been rejected *for
behavior* — so the next Model Freshness "Apply All" through the bulk endpoint
saw a merely-stale rung and re-applied ``anthropic/claude-sonnet-5``
(``admin_ui_bulk``, ledger ts 2026-08-21T22:28:53Z), and the deliberation
leaks resumed the next day. Rollbacks now pin the rejected (bot, tier, model)
in ``{shared_dir}/model_swap_pins.jsonl``; both endpoints here consult the
pins via ``model_tier_apply.behavior_pins`` and REFUSE an apply that would
reintroduce a pinned pair unless the request carries ``override_pin: true``
(single: top-level flag, 409 without it; bulk: top-level flag, pinned rows
fail per-row so the rest of the batch still applies). An override that lands
also records an ``unpin`` — the operator has consciously revoked the
rejection, and pin state must match what is actually applied.

Unknown pin state (the pin ledger exists but is unreadable) refuses the same
way rather than failing open — an unreadable pin file silently reading as
"no pins" is the exact non-sticky rollback this gate closes. The override
escape hatch still works, so a corrupt ledger degrades to explicit-confirm
applies, never to a bricked freshness surface.
"""

from __future__ import annotations

import sys
from pathlib import Path

from flask import Flask, jsonify, request, Response

from .routes_shared import tier_write_oc_keys as _tier_oc_keys


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


def _pin_refusal_error(pin: "dict | None", pin_err: "str | None") -> str:
    """The operator-facing refusal message for a pinned (or unknown-pin) apply."""
    if pin is not None:
        return (
            f"{pin.get('model')} was rolled back for behavior on "
            f"{pin.get('bot_id')}/{pin.get('tier')} ({pin.get('ts', 'unknown time')}: "
            f"{pin.get('reason', 'no reason recorded')}) and is pinned against "
            "re-apply. Re-applying it requires an explicit override."
        )
    return (
        f"{pin_err} — pin state is unknown, so this apply is refused rather "
        "than risking the re-introduction of a behavior-rejected model. Repair "
        "the pin ledger, or re-apply with an explicit override."
    )


def register_model_tier_update_routes(
    app: Flask, network_path: Path, *, reject_unknown_bot,
) -> None:
    """Attach the two tier-write endpoints. ``reject_unknown_bot`` is the
    roster guard closure from ``routes_admin_config`` (#3566 audit C-2),
    passed in so both files keep exactly one implementation of it."""
    # Late-bound server-module handle (routes_admin_config memo §1.3): reach
    # patchable helpers via ``_module._NAME`` at call time so test
    # monkeypatches on ``server._NAME`` are respected.
    _module = sys.modules["evolve_admin.web.server"]

    @app.post("/api/models/update-tier")
    def api_models_update_tier() -> "Response | tuple[Response, int]":
        """One-click update of a bot's tier to the recommended model.

        Body: {bot_id, tier, provider, model, override_pin?}.
        Replaces any same-provider entry in the tier with the new model
        (or appends if no same-provider entry exists), preserving other
        providers' models in that tier. A model behavior-pinned by a
        rollback is refused with 409 unless ``override_pin`` is true.
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
        # when network.json was unreadable. (It also rejects non-string
        # bot_ids, so the str() narrowing below is for the type checker, not
        # a behavior change.)
        rej = reject_unknown_bot(bot_id)
        if rej is not None:
            return rej
        bot_id, tier, new_model = str(bot_id), str(tier), str(new_model)

        from runtime.agent_runtime import get_runtime
        _rt = get_runtime()
        _cfg_get = _rt.full_config_get
        _cfg_set = _rt.full_config_set_with_error

        # Validate against the resolver the freshness check uses — see
        # _check_recommendation_match for the rationale (must match check).
        err = _check_recommendation_match(provider, tier, new_model)
        if err is not None:
            return jsonify({"error": err}), 400

        from .model_tier_apply import (
            behavior_pins, model_landed, pin_lookup, record_swap, record_unpin,
            stage_tier_model, tier_models,
        )
        pins, pin_err = behavior_pins(network_path)
        pin = pin_lookup(pins, bot_id, tier, new_model)
        override = bool(body.get("override_pin"))
        if (pin is not None or pin_err) and not override:
            return jsonify({
                "error": _pin_refusal_error(pin, pin_err),
                "behavior_pin": pin,
                "override_required": True,
            }), 409

        cfg = _cfg_get(bot_id)
        if not cfg:
            return jsonify({"error": f"could not read config for bot {bot_id!r}"}), 404

        # Send ONLY this tier (a sibling sharing a rung would clobber it) and
        # verify it landed before reporting success — see model_tier_apply.
        prev_models = tier_models(cfg, tier)  # BEFORE the write — the undo target
        tier_entry = stage_tier_model((cfg.get("tiers") or {}).get(tier), provider, new_model)

        # OC's catalog (agents.defaults.models) must also carry the model (else
        # OC silently falls back); keep the prior model as a safe fallback.
        existing_catalog = list(cfg.get("catalog") or [])
        catalog_changed = new_model not in existing_catalog
        if catalog_changed:
            existing_catalog.append(new_model)

        updates: dict = {"tiers": {tier: tier_entry}}
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

        record_swap(network_path, bot_id, tier, provider, prev_models, landed,
                    source="admin_ui_single")
        unpinned = False
        if override and pin is not None:
            unpinned = record_unpin(network_path, bot_id, tier, new_model,
                                    source="admin_ui_single_override")
        _module._audit_log_entry("models.tier.update", bot_id, {
            "tier": tier, "provider": provider, "new_model": new_model,
            "catalog_added": catalog_changed, "previous_models": prev_models,
            **({"override_pin": True, "unpinned": unpinned}
               if override and (pin is not None or pin_err) else {}),
        }, oc_keys=_tier_oc_keys(result, {"agents"}))
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
    def api_models_update_tier_bulk() -> "Response | tuple[Response, int]":
        """Apply a batch of freshness advisories in one pass.

        Body: {updates: [{bot_id, tier, provider, model}, ...], override_pin?}

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
        rest of the bot's changes still apply. A behavior-pinned
        (bot, tier, model) fails per-row the same way unless the batch
        carries ``override_pin: true``.
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
            rej = reject_unknown_bot(bot_id)
            if rej is not None:
                rej_body, rej_status = rej
                return jsonify({
                    "error": f"updates[{i}]: {rej_body.get_json()['error']}",
                }), rej_status
            bot_id, tier, new_model = str(bot_id), str(tier), str(new_model)
            err = _check_recommendation_match(provider, tier, new_model)
            if err is not None:
                return jsonify({"error": f"updates[{i}]: {err}"}), 400
            normalized.append({
                "bot_id": bot_id, "tier": tier,
                "provider": provider, "model": new_model,
            })

        # Behavior-pin gate — read pin state ONCE for the batch. A pinned
        # row is a policy refusal, not a malformed entry, so it fails
        # per-row (the rest of the batch still applies) rather than
        # rejecting the whole request the way validation errors do.
        from .model_tier_apply import (
            behavior_pins, model_landed, pin_lookup, record_swap, record_unpin,
            stage_tier_model, tier_models,
        )
        override = bool(body.get("override_pin"))
        pins, pin_err = behavior_pins(network_path)

        results: list[dict] = []
        allowed: list[dict] = []
        for u in normalized:
            pin = None if override else pin_lookup(pins, u["bot_id"], u["tier"], u["model"])
            if not override and (pin is not None or pin_err):
                results.append({
                    "bot_id": u["bot_id"], "tier": u["tier"],
                    "provider": u["provider"], "success": False,
                    "pinned": True,
                    "error": _pin_refusal_error(pin, pin_err),
                })
                continue
            allowed.append(u)

        # Group by bot_id, preserving each bot's first-seen order so the
        # response is deterministic for tests.
        by_bot: dict[str, list[dict]] = {}
        bot_order: list[str] = []
        for u in allowed:
            bot_id = u["bot_id"]
            if bot_id not in by_bot:
                by_bot[bot_id] = []
                bot_order.append(bot_id)
            by_bot[bot_id].append(u)

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
            overrode_pins: list[str] = []
            for u in bot_updates:
                tier = u["tier"]
                if model_landed(write_result, tier, u["model"]):
                    record_swap(network_path, bot_id, tier, u["provider"], prev_by_tier.get(tier),
                                tier_models(write_result, tier), source="admin_ui_bulk")
                    if override and pin_lookup(pins, bot_id, tier, u["model"]) is not None:
                        record_unpin(network_path, bot_id, tier, u["model"],
                                     source="admin_ui_bulk_override")
                        overrode_pins.append(f"{tier}:{u['model']}")
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
                        **({"override_pin": True, "unpinned": overrode_pins}
                           if overrode_pins else {}),
                    },
                    oc_keys=_tier_oc_keys(write_result, {"agents"}),
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
