"""Admin-UI routes for the Slack policy layer.

Surface the doctor's status + the policy file via HTTP so the admin UI
can render a "Slack" card on the Bot Config page. Phase 2 spec §7.1.

Endpoints (all per-bot, ``?bot=<id>`` query param to match the
established pattern in ``server.py``):

- ``GET  /api/bot/slack-policy``       — read state (doctor + policy)
- ``POST /api/bot/slack-policy/init``  — bootstrap policy from openclaw.json
- ``POST /api/bot/slack-policy/apply`` — render policy → openclaw.json

The Slack-doctor work happens via the same ``doctor.run_doctor`` the
CLI uses — we never duplicate check logic in the web layer.

Auth: per ``feedback_ui_authorization_presumed``, admin-UI routes
have no per-route auth gates. The shared dir + bot home ACLs handle
file permissions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request


def register_slack_policy_routes(app: "Flask", network_path: "Path") -> None:
    """Register the ``/api/bot/slack-policy*`` endpoints.

    Caller passes the network config path so we can resolve bot homes
    + the shared dir without leaking module-level state.
    """
    def _resolve_bot(bot_name: str | None) -> "tuple[str | None, Path | None, Path | None]":
        """Resolve ``(bot_id, bot_home, shared_dir)`` for a request.

        Returns ``(None, None, None)`` if the bot isn't a known
        member. Callers map that to 404. The web layer never invents
        bot IDs — pod membership is explicit (per
        ``feedback_explicit_pod_membership``).

        Imports ``bot_home`` + ``load_network`` lazily on each call —
        capturing them at register time bound the closure to whatever
        was in ``evolve_admin.config`` at registration, which broke
        tests that ``monkeypatch.setattr`` the module attribute *after*
        first import. Per-call lookup picks up the patched function.
        """
        from ..config import bot_home as _bot_home, load_network as _load_network
        if not bot_name:
            return None, None, None
        cfg = _load_network(network_path)
        bots = cfg.get("bots") or {}
        if bot_name not in bots:
            return None, None, None
        home = _bot_home(bot_name, cfg)
        shared = Path(cfg.get("sharedDir") or "/Users/Shared/evolve")
        return bot_name, home, shared

    @app.get("/api/bot/slack-policy")
    def api_get_slack_policy() -> "Response":
        bot_name = request.args.get("bot")
        bot_id, home, shared = _resolve_bot(bot_name)
        if not bot_id or not home or not shared:
            return jsonify({"error": "unknown bot", "bot": bot_name}), 404

        from ..integrations.slack.doctor import run_doctor
        from ..integrations.slack.policy import (
            load_policy, policy_path, to_dict_redacted,
        )
        from ..integrations.slack.writer import is_render_up_to_date

        try:
            result = run_doctor(bot_id, bot_home=home, shared_dir=shared)
        except Exception as exc:
            return jsonify({
                "bot_id": bot_id,
                "error": f"doctor crashed: {exc}",
            }), 500

        try:
            policy = load_policy(shared, bot_id)
            policy_load_error = None
        except ValueError as exc:
            policy = None
            policy_load_error = str(exc)

        policy_exists = policy_path(shared, bot_id).exists()
        policy_payload = to_dict_redacted(policy) if policy is not None else None

        # Drift state — does openclaw.json match what we'd render now?
        drift_state = "unknown"
        if policy is not None:
            try:
                target = home / ".openclaw" / "openclaw.json"
                existing = json.loads(target.read_text())
                drift_state = "in_sync" if is_render_up_to_date(
                    existing=existing, policy=policy,
                ) else "drifted"
            except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
                drift_state = "unknown"

        # Build a name index from joined channels for the listening list
        name_by_id = {
            c.get("id"): c.get("name") for c in result.bot_member_channels
            if isinstance(c.get("id"), str)
        }
        joined_set = set(name_by_id.keys())
        listening_rows: list[dict[str, Any]] = []
        for entry in result.listening_channel_entries:
            cid = entry["channel_id"]
            listening_rows.append({
                "channel_id": cid,
                "display_name": name_by_id.get(cid) or entry.get("display_name") or "",
                "require_mention": bool(entry.get("require_mention")),
                "visible_replies": entry.get("visible_replies"),
                "is_joined": cid in joined_set,
            })
        joined_not_listening = sorted(
            joined_set - set(result.listening_channel_ids)
        )
        joined_not_listening_rows = [
            {"channel_id": cid, "display_name": name_by_id.get(cid, "")}
            for cid in joined_not_listening
        ]

        # auth_test carries identity; surface workspace name for the header.
        auth = result.auth_test or {}
        return jsonify({
            "bot_id": bot_id,
            "policy_exists": policy_exists,
            "policy_load_error": policy_load_error,
            "policy": policy_payload,
            "drift_state": drift_state,
            "doctor": {
                "slack_enabled": result.slack_enabled,
                "transport_mode": result.transport_mode,
                "group_policy": result.group_policy,
                "dm_policy": result.dm_policy,
                "has_signing_secret": result.has_signing_secret,
                "has_app_token": result.has_app_token,
                "streaming_mode": result.streaming_mode,
                "streaming_native_transport": result.streaming_native_transport,
                "bot_token_source": result.bot_token_source,
                "workspace_name": auth.get("team") or "",
                "workspace_team_id": auth.get("team_id") or "",
                "listening_channels": listening_rows,
                "joined_not_listening": joined_not_listening_rows,
                "allow_from_user_ids": list(result.allow_from_user_ids),
                "allow_from_count": len(result.allow_from_user_ids),
                "other_provider_keys": list(result.other_provider_keys),
                "feature_bundles": [
                    {
                        "key": b.bundle.key,
                        "name": b.bundle.name,
                        "rationale": b.bundle.rationale,
                        "scopes": list(b.bundle.scopes),
                        "enabled": b.enabled,
                        "missing": list(b.missing),
                    }
                    for b in result.feature_bundles
                ],
                "elevated_scopes_granted": list(result.elevated_scopes_granted),
                "oauth_scopes": list(result.oauth_scopes),
                "oauth_scopes_count": len(result.oauth_scopes),
                "findings": [
                    {
                        "code": f.code, "severity": f.severity,
                        "title": f.title, "detail": f.detail,
                        "channel_id": f.channel_id, "user_id": f.user_id,
                    }
                    for f in result.findings
                ],
                "has_fail": result.has_fail(),
            },
        })

    @app.post("/api/bot/slack-policy/init")
    def api_init_slack_policy() -> "Response":
        bot_name = request.args.get("bot")
        bot_id, home, shared = _resolve_bot(bot_name)
        if not bot_id or not home or not shared:
            return jsonify({"error": "unknown bot", "bot": bot_name}), 404

        from ..integrations.slack.doctor import run_doctor
        from ..integrations.slack.policy import policy_path, save_policy
        from ..integrations.slack.writer import synthesize_policy_from_openclaw

        body = request.get_json(silent=True) or {}
        force = bool(body.get("force", False))

        existing = policy_path(shared, bot_id)
        if existing.exists() and not force:
            return jsonify({
                "error": "policy_already_exists",
                "detail": f"slack-policy.json already exists at {existing}. "
                          f"Pass force=true to overwrite.",
            }), 409

        # Doctor gate: refuse to synthesize from a FAIL-state openclaw.json
        # unless explicitly forced (matches CLI behavior).
        try:
            pre_result = run_doctor(bot_id, bot_home=home)
        except Exception as exc:
            return jsonify({"error": f"doctor crashed: {exc}"}), 500
        fails = pre_result.by_severity("fail")
        if fails and not force:
            return jsonify({
                "error": "refused_by_doctor",
                "detail": (
                    "openclaw.json has FAIL findings; encoding them into "
                    "the policy would re-render the bug. Fix the issues "
                    "first, or pass force=true to encode the broken state."
                ),
                "fails": [
                    {"code": f.code, "title": f.title, "detail": f.detail}
                    for f in fails
                ],
            }), 422

        try:
            policy, err = synthesize_policy_from_openclaw(
                bot_id=bot_id, bot_home=home,
            )
        except Exception as exc:
            return jsonify({"error": f"synthesize_crashed: {exc}"}), 500
        if err or policy is None:
            return jsonify({"error": err or "synthesize_returned_none"}), 422

        try:
            saved_path = save_policy(shared, policy)
        except ValueError as exc:
            return jsonify({"error": "policy_invalid", "detail": str(exc)}), 422
        except OSError as exc:
            return jsonify({"error": "policy_write_failed", "detail": str(exc)}), 500

        return jsonify({"ok": True, "path": str(saved_path), "bot_id": bot_id})

    @app.post("/api/bot/slack-policy/save")
    def api_save_slack_policy() -> "Response":
        """Persist edits to slack-policy.json (no openclaw.json render yet).

        Body shape mirrors the on-disk policy file (per
        ``policy._to_dict``). The doctor's full apply gate runs on
        ``/apply``, not here — saving lets the operator iterate on the
        policy in the UI without each save triggering a write to the
        live bot config.
        """
        bot_name = request.args.get("bot")
        bot_id, home, shared = _resolve_bot(bot_name)
        if not bot_id or not home or not shared:
            return jsonify({"error": "unknown bot", "bot": bot_name}), 404

        from ..integrations.slack.policy import (
            _from_dict, save_policy,
        )
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "body_required",
                            "detail": "JSON body matching slack-policy.json shape required"}), 400
        # Force the saved policy's bot_id to match the URL — never trust
        # the client to set it. Otherwise a typo in the body could
        # cross-write another bot's policy.
        body["bot_id"] = bot_id
        try:
            policy = _from_dict(body, bot_id_default=bot_id)
        except ValueError as exc:
            return jsonify({"error": "policy_invalid", "detail": str(exc)}), 422
        try:
            saved = save_policy(shared, policy)
        except ValueError as exc:
            return jsonify({"error": "policy_invalid", "detail": str(exc)}), 422
        except OSError as exc:
            return jsonify({"error": "policy_write_failed", "detail": str(exc)}), 500
        return jsonify({"ok": True, "path": str(saved), "bot_id": bot_id})

    @app.post("/api/bot/slack-policy/apply")
    def api_apply_slack_policy() -> "Response":
        bot_name = request.args.get("bot")
        bot_id, home, shared = _resolve_bot(bot_name)
        if not bot_id or not home or not shared:
            return jsonify({"error": "unknown bot", "bot": bot_name}), 404

        from ..integrations.slack.doctor import run_doctor
        from ..integrations.slack.policy import load_policy
        from ..integrations.slack.writer import render_to_openclaw_json

        body = request.get_json(silent=True) or {}
        force = bool(body.get("force", False))

        try:
            policy = load_policy(shared, bot_id)
        except ValueError as exc:
            return jsonify({"error": "policy_malformed", "detail": str(exc)}), 422
        if policy is None:
            return jsonify({
                "error": "no_policy",
                "detail": "Initialize the policy first via /api/bot/slack-policy/init.",
            }), 404

        # Doctor gate, matching the CLI.
        try:
            result = run_doctor(bot_id, bot_home=home, shared_dir=shared)
        except Exception as exc:
            return jsonify({"error": f"doctor crashed: {exc}"}), 500
        fails = result.by_severity("fail")
        if fails and not force:
            return jsonify({
                "error": "refused_by_doctor",
                "fails": [
                    {"code": f.code, "title": f.title, "detail": f.detail}
                    for f in fails
                ],
            }), 422

        try:
            write = render_to_openclaw_json(
                bot_id=bot_id, bot_home=home, policy=policy,
            )
        except Exception as exc:
            return jsonify({"error": f"render_crashed: {exc}"}), 500

        if not write.ok:
            return jsonify({
                "error": "render_failed",
                "write_error": write.write_error,
                "pre_validate_errors": list(write.pre_validate_errors),
            }), 500
        return jsonify({
            "ok": True,
            "written": write.written,
            "added_channel_ids": list(write.added_channel_ids),
            "updated_channel_ids": list(write.updated_channel_ids),
            "removed_channel_ids": list(write.removed_channel_ids),
        })


__all__ = ["register_slack_policy_routes"]
