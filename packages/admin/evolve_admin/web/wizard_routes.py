"""wizard_routes.py — Add-a-Bot wizard backend endpoints.

PR β of the wizard work. Spec:
`docs/spec-add-bot-wizard-2026-05-28.md` §6 + §7.

This module registers the HTTP endpoints the wizard frontend (PR γ)
calls. It's a thin layer over PR α's `provisioning.provision_bot()`
plus a small amount of credential plumbing.

Endpoints:
  POST /api/wizard/preview
      Synchronous. Validates the form inputs (UID/port conflicts,
      bot_id format, role) and returns the resolved values + a
      suggested display name. No side effects.

  POST /api/wizard/provision
      Async. Runs `provision_bot()` in a background thread; returns
      `{jobId}` immediately. The frontend polls `GET /api/jobs/<id>`
      (the existing job-status endpoint) for stage-by-stage progress.

  POST /api/credentials/borrow
      Synchronous. Copies selected provider entries from one bot's
      auth-profiles.json into another's. Per-bot independent after
      copy — no rotation cascading. Audit-stamps each profile with
      `borrowed_from` + `borrowed_at`.

  GET  /api/wizard/borrow-candidates?provider=<id>
      List bots that have a configured profile for the given provider,
      so the wizard's borrow dropdown knows what to show.

All endpoints assume the admin server's auth boundary is the surface
(per memory note: admin-UI authorization is presumed). No per-role
gates here.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

from flask import Flask, Response, jsonify, request

from ..config import get_bot_user as _get_bot_user
from ..config import load_network


_log = logging.getLogger(__name__)


# ── Bot-id heuristic + LLM name-suggestion ────────────────────────────────────


# Pattern: 2-32 chars, lowercase letters / digits / underscore. Matches
# what add_bot() accepts and what the rest of the codebase assumes
# everywhere (no hyphens — those clash with launchd label namespacing).
_BOT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

# Words we strip from a description before slugging — they don't help
# the operator pick a name.
_FILLER_WORDS = {
    "a", "an", "the", "for", "to", "of", "on", "in", "and", "or",
    "bot", "agent", "assistant", "ai", "my", "our", "with", "that",
    "this", "is", "will", "would", "should", "can",
}


def suggest_bot_id_heuristic(description: str) -> str:
    """Slug-based suggestion: take the first meaningful noun-ish word.

    This is the fallback when no LLM key is available, and the
    starting point even when LLM is. Operator can always override in
    the UI.

    Examples:
      "Research bot for OpenClaw enthusiasts"  → "research"
      "A telegram bot that watches my GitHub"  → "github"
      "Atlas — community research"             → "atlas"
    """
    if not description or not description.strip():
        return "newbot"
    # Lowercase, strip punctuation
    cleaned = re.sub(r"[^a-z0-9_\s]", " ", description.lower())
    words = [w for w in cleaned.split() if w and w not in _FILLER_WORDS]
    if not words:
        return "newbot"
    # Take the first meaningful word, truncated to 16 chars
    candidate = words[0][:16]
    # If it doesn't match the pattern (e.g. starts with digit), prepend
    if not candidate or not candidate[0].isalpha():
        candidate = "bot_" + candidate
    if not _BOT_ID_RE.match(candidate):
        return "newbot"
    return candidate


def suggest_bot_id_via_llm(description: str, target) -> str:
    """LLM-assisted name from the description.

    Fast-role (tier-3) call — ~$0.001 per invocation per the Q1
    resolution in the wizard spec. ``target`` is a resolved
    ``infra_llm.InfraLLMTarget`` (#3466: any credentialed provider).
    Falls back to the heuristic on any failure.

    Returns a candidate bot_id matching `_BOT_ID_RE`. Never raises —
    every failure path returns the heuristic.
    """
    if target is None or not description.strip():
        return suggest_bot_id_heuristic(description)
    try:
        from infra_llm import complete  # type: ignore

        text = complete(
            target,
            prompt=description.strip(),
            system=(
                "You suggest short identifiers for bots. The user gives a "
                "one-line description of what the bot does; you return "
                "a single lowercase identifier, 2-16 chars, [a-z][a-z0-9_]*, "
                "no punctuation, no quotes, no explanation. Examples: "
                "'research bot for openclaw' -> 'openclaw_news'; "
                "'home assistant watcher' -> 'ha_watch'; "
                "'travel planning helper' -> 'travel'."
            ),
            max_tokens=32,
            timeout=10,
        )
        # Sanitize — LLMs sometimes add quotes or trailing punctuation
        text = text.strip().strip("'\"`.,").lower()
        # Replace hyphens with underscores (some models prefer hyphens)
        text = text.replace("-", "_")
        if _BOT_ID_RE.match(text):
            return text
    except Exception as exc:
        _log.info("wizard: LLM name suggestion failed (%s); using heuristic", exc)
    return suggest_bot_id_heuristic(description)


# ── Credentials borrow ────────────────────────────────────────────────────────


def _auth_profiles_path(user: str) -> Path:
    return Path(f"/Users/{user}/.openclaw/agents/main/agent/auth-profiles.json")


def _read_auth_profiles_safe(user: str) -> dict | None:
    """Read a bot's auth-profiles.json.

    The evolve user has ACL read on .openclaw/ after deploy. Falls
    back to `sudo /bin/cat` (sudoers grant for the evolve user) if a
    bot pre-dates that grant.

    Returns None on absent or unparseable file.
    """
    path = _auth_profiles_path(user)
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except PermissionError:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except Exception:
            return None
        return None
    except json.JSONDecodeError:
        return None
    except Exception:
        return None


def _write_auth_profiles_as_bot(user: str, data: dict) -> bool:
    """Write auth-profiles.json under the bot's account.

    Pattern matches the rest of admin: /tmp staging + sudo /bin/cp.
    Returns True on success.
    """
    dest = _auth_profiles_path(user)
    try:
        # Ensure target dir exists (may need mkdir as bot user)
        parent = dest.parent
        if not parent.exists():
            subprocess.run(
                ["sudo", "/bin/mkdir", "-p", str(parent)],
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["sudo", "/usr/sbin/chown", f"{user}:staff", str(parent)],
                capture_output=True, timeout=10,
            )
        fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-{user}-auth-", suffix=".json")
        import os as _os
        with _os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        # Copy + own + chmod 600 (auth file)
        r1 = subprocess.run(
            ["sudo", "/bin/cp", tmp, str(dest)],
            capture_output=True, text=True, timeout=10,
        )
        r2 = subprocess.run(
            ["sudo", "/usr/sbin/chown", f"{user}:staff", str(dest)],
            capture_output=True, text=True, timeout=10,
        )
        r3 = subprocess.run(
            ["sudo", "/bin/chmod", "600", str(dest)],
            capture_output=True, text=True, timeout=10,
        )
        _os.unlink(tmp)
        return r1.returncode == 0 and r2.returncode == 0 and r3.returncode == 0
    except Exception as exc:
        _log.warning("wizard: write auth-profiles for %s failed: %s", user, exc)
        return False


def borrow_credentials(
    *,
    from_bot: str,
    to_bot: str,
    providers: list[str],
    network: dict,
) -> dict:
    """Copy named provider entries from one bot's auth-profiles into another.

    Returns a dict suitable for jsonify:
      {"ok": bool, "copied": [provider names], "skipped": [...], "error": str?}

    Each copied profile gets `borrowed_from` + `borrowed_at` audit
    fields so we can tell later that it was a wizard-time copy and not
    an original entry.

    Per-bot independent after copy: rotation in `from_bot` does not
    propagate. Documented limitation in spec §6.3.
    """
    from_user = _get_bot_user(from_bot, network)
    to_user = _get_bot_user(to_bot, network)

    source = _read_auth_profiles_safe(from_user)
    if source is None:
        return {"ok": False, "error": f"could not read auth-profiles for {from_bot}"}

    src_profiles = source.get("profiles", {}) if isinstance(source, dict) else {}
    if not isinstance(src_profiles, dict):
        return {"ok": False, "error": f"{from_bot}'s auth-profiles is malformed"}

    dest = _read_auth_profiles_safe(to_user) or {}
    if not isinstance(dest, dict):
        dest = {}
    dest_profiles = dest.setdefault("profiles", {})

    copied: list[str] = []
    skipped: list[dict] = []
    now = datetime.datetime.utcnow().isoformat() + "Z"

    for provider in providers:
        # Match any profile whose key starts with "<provider>:" — that's
        # the canonical prefix (anthropic:api_key, brave:api, etc.)
        matching = [k for k in src_profiles.keys() if k.startswith(f"{provider}:")]
        if not matching:
            skipped.append({"provider": provider, "reason": "no profile in source bot"})
            continue
        for pkey in matching:
            entry = src_profiles[pkey]
            if not isinstance(entry, dict):
                skipped.append({"provider": pkey, "reason": "source profile malformed"})
                continue
            # Shallow copy + audit stamp
            new_entry = dict(entry)
            new_entry["borrowed_from"] = from_bot
            new_entry["borrowed_at"] = now
            dest_profiles[pkey] = new_entry
            copied.append(pkey)

    if not copied:
        return {"ok": False, "copied": [], "skipped": skipped,
                "error": "no profiles matched the requested providers"}

    if not _write_auth_profiles_as_bot(to_user, dest):
        return {"ok": False, "copied": [], "skipped": skipped,
                "error": f"write to {to_bot}'s auth-profiles failed"}

    return {"ok": True, "copied": copied, "skipped": skipped}


# Wizard auth_choice values → the profile-key prefix used in
# auth-profiles.json. The profile-key format is "<provider>:<kind>"
# (e.g. "anthropic:api_key", "openai:api_key", "brave:api"); this
# table maps the wizard's provider identifier to the prefix so we
# can find the right profile when borrowing from another bot.
#
# Built from the openclaw onboard --help auth-choice enum so it
# stays parallel with provisioning.AUTH_CHOICE_TO_KEY_FLAG. New
# wizard providers should be added here AND in that dict.
AUTH_CHOICE_TO_PROFILE_PROVIDER: dict[str, str] = {
    "anthropic":          "anthropic",
    "anthropic-api-key":  "anthropic",
    "openai-api-key":     "openai",
    "gemini-api-key":     "gemini",
    "xai-api-key":        "xai",
    "deepseek-api-key":   "deepseek",
    "groq-api-key":       "groq",
    "mistral-api-key":    "mistral",
    "openrouter-api-key": "openrouter",
    "together-api-key":   "together",
    "fireworks-api-key":  "fireworks",
    "moonshot-api-key":   "moonshot",
    "nvidia-api-key":     "nvidia",
}


def read_provider_key_from_bot(
    *, bot_id: str, provider: str, network: dict,
) -> Optional[str]:
    """Read a single LLM API key for ``provider`` from ``bot_id``'s
    auth-profiles.json. Returns the raw key string, or None if the
    bot has no profile for that provider.

    Used by the wizard's Screen-2 "Borrow from <bot>" path during
    provision. Unlike ``borrow_credentials`` (which copies +
    write-back-with-audit-stamp for post-creation borrowing), this
    is a pure read: the borrowed key is passed to openclaw onboard
    via ``--<provider>-api-key`` and the target bot ends up with
    its own copy via openclaw's normal config flow. No audit
    stamp because no write happens here.
    """
    user = _get_bot_user(bot_id, network)
    profiles_doc = _read_auth_profiles_safe(user)
    if not isinstance(profiles_doc, dict):
        return None
    profs = profiles_doc.get("profiles", {})
    if not isinstance(profs, dict):
        return None
    for pkey, entry in profs.items():
        if not pkey.startswith(f"{provider}:"):
            continue
        if isinstance(entry, dict) and entry.get("key"):
            return entry["key"]
    return None


def borrow_candidates(provider: str, network: dict) -> list[dict]:
    """Return bots that have a configured profile for `provider`.

    Used by the wizard's "Borrow from <bot>" dropdown to show options.
    """
    results: list[dict] = []
    for bot_id in network.get("members", []):
        user = _get_bot_user(bot_id, network)
        profiles_doc = _read_auth_profiles_safe(user)
        if not profiles_doc:
            continue
        profs = profiles_doc.get("profiles", {}) if isinstance(profiles_doc, dict) else {}
        if not isinstance(profs, dict):
            continue
        matching = [k for k in profs.keys() if k.startswith(f"{provider}:")]
        if matching:
            results.append({"bot_id": bot_id, "profile_keys": matching})
    return results


# ── Flask registration ────────────────────────────────────────────────────────


def register_wizard_routes(app: Flask, network_path: Path) -> None:
    """Register /api/wizard/* + /api/credentials/borrow on the Flask app."""

    @app.post("/api/wizard/preview")
    def api_wizard_preview() -> Response:
        """Validate wizard inputs + return resolved UID/port + suggested name.

        Body (all optional except `description`):
          {
            "description": "research bot for openclaw enthusiasts",
            "bot_id": "atlas",          # if operator typed one already
            "user": null,                # default: same as bot_id
            "port": null,                # default: next free in 19000-19100
            "uid": null,                 # default: next free >= 502
            "role": "member",
            "allow_existing_user": false
          }

        Returns:
          {
            "ok": bool,
            "suggested_bot_id": "openclaw_news",   # heuristic + LLM
            "resolved": {bot_id, user, uid, port, role},
            "validation": {"ok": bool, "issues": [...]}
          }
        """
        from ..provisioning import (
            _next_free_port, _next_free_uid, _user_exists,
            DEFAULT_UID_START,
        )

        body = request.get_json(silent=True) or {}
        description = (body.get("description") or "").strip()
        explicit_bot_id = (body.get("bot_id") or "").strip().lower()
        explicit_user = (body.get("user") or "").strip()
        explicit_port = body.get("port")
        explicit_uid = body.get("uid")
        role = (body.get("role") or "member").strip()
        allow_existing_user = bool(body.get("allow_existing_user", False))

        # Name suggestion — even if operator typed one, we return a
        # suggestion so the form can offer it as a placeholder.
        suggestion = _suggest_bot_id_with_optional_llm(description)

        network = load_network(network_path)

        # Resolved values
        resolved_bot_id = explicit_bot_id or suggestion
        resolved_user = explicit_user or resolved_bot_id

        # Validation pass — non-destructive, collects all issues
        issues: list[dict] = []

        if not _BOT_ID_RE.match(resolved_bot_id):
            issues.append({
                "field": "bot_id",
                "message": (
                    f"{resolved_bot_id!r} not a valid bot_id "
                    f"(must be lowercase letter+[a-z0-9_], 2-32 chars)"
                ),
            })

        if role not in ("primary", "member"):
            issues.append({"field": "role", "message": f"role must be 'primary' or 'member', got {role!r}"})

        if resolved_bot_id in network.get("bots", {}):
            issues.append({
                "field": "bot_id",
                "message": f"{resolved_bot_id!r} is already registered",
            })

        # User existence check
        try:
            user_exists = _user_exists(resolved_user)
        except Exception:
            user_exists = False
        if user_exists and resolved_user != resolved_bot_id and not allow_existing_user:
            issues.append({
                "field": "user",
                "message": (
                    f"macOS user {resolved_user!r} already exists; "
                    f"check 'shared account' if this is intentional"
                ),
            })

        # UID resolution
        try:
            resolved_uid = int(explicit_uid) if explicit_uid is not None else _next_free_uid(start=DEFAULT_UID_START)
        except (TypeError, ValueError):
            issues.append({"field": "uid", "message": f"uid {explicit_uid!r} must be an integer"})
            resolved_uid = -1

        # Port resolution
        if explicit_port is not None:
            try:
                resolved_port = int(explicit_port)
                if resolved_port < 1024 or resolved_port > 65535:
                    issues.append({"field": "port", "message": "port outside [1024-65535]"})
                else:
                    for b_id, b in network.get("bots", {}).items():
                        if b.get("port") == resolved_port:
                            issues.append({
                                "field": "port",
                                "message": f"port {resolved_port} already used by {b_id!r}",
                            })
                            break
            except (TypeError, ValueError):
                issues.append({"field": "port", "message": f"port {explicit_port!r} must be an integer"})
                resolved_port = -1
        else:
            try:
                resolved_port = _next_free_port(network)
            except Exception as exc:
                issues.append({"field": "port", "message": str(exc)})
                resolved_port = -1

        return jsonify({
            "ok": len(issues) == 0,
            "suggested_bot_id": suggestion,
            "resolved": {
                "bot_id": resolved_bot_id,
                "user": resolved_user,
                "uid": resolved_uid,
                "port": resolved_port,
                "role": role,
            },
            "validation": {
                "ok": len(issues) == 0,
                "issues": issues,
            },
        })

    @app.post("/api/wizard/provision")
    def api_wizard_provision() -> Response:
        """Kick off the provision pipeline in a background thread.

        Returns 202 + `{jobId}`. The frontend polls
        `GET /api/jobs/<jobId>` for stage-by-stage progress, where
        each stage log entry has the shape:
          {t: "HH:MM:SS", level: "info|success|warning|error",
           msg: "stage_name: status[: detail]"}
        """
        from .server import _new_job, _job_log, _job_finish, _jobs_lock, _jobs
        from ..provisioning import (
            provision_bot, STATUS_OK, STATUS_SKIP, STATUS_FAIL, STATUS_START,
        )

        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip().lower()
        if not bot_id:
            return jsonify({"error": "bot_id required"}), 400

        # Translate JSON body to provision_bot kwargs.
        #
        # LLM provider auth — Evolve is provider-agnostic by principle:
        # the wizard MUST send an explicit ``auth_choice`` (one of the
        # values in ``provisioning.AUTH_CHOICE_TO_KEY_FLAG``) and EITHER
        # ``provider_api_key`` (the operator pasted a key) OR
        # ``borrow_provider_from_bot`` (the operator picked a sibling bot
        # to borrow from). ``anthropic_api_key`` is accepted as a legacy
        # field for one deprecation cycle.
        auth_choice = body.get("auth_choice")
        provider_api_key = body.get("provider_api_key")
        borrow_from_bot = body.get("borrow_provider_from_bot")
        legacy_anthropic_key = body.get("anthropic_api_key")

        if legacy_anthropic_key and not provider_api_key:
            provider_api_key = legacy_anthropic_key
            if not auth_choice:
                auth_choice = "anthropic"

        # Borrow path: resolve to a raw key by reading the source bot's
        # auth-profiles.json. We translate the wizard's auth_choice to
        # the profile-key prefix via AUTH_CHOICE_TO_PROFILE_PROVIDER.
        # If the borrow source has no key for the requested provider,
        # the borrow silently no-ops and provision proceeds without a
        # key — openclaw onboard will surface the actionable error.
        if borrow_from_bot and not provider_api_key and auth_choice:
            profile_provider = AUTH_CHOICE_TO_PROFILE_PROVIDER.get(auth_choice)
            if profile_provider:
                _network = load_network(network_path)
                borrowed = read_provider_key_from_bot(
                    bot_id=borrow_from_bot,
                    provider=profile_provider,
                    network=_network,
                )
                if borrowed:
                    provider_api_key = borrowed

        kwargs: dict[str, Any] = {
            "bot_id": bot_id,
            "user": (body.get("user") or None),
            "uid": body.get("uid"),
            "port": body.get("port"),
            "role": body.get("role") or "member",
            "display_name": body.get("display_name"),
            "provider_api_key": provider_api_key,
            "auth_choice": auth_choice,
            # Custom-provider extras (Other / OpenAI-compatible endpoint
            # in the Screen 2 chooser). Only meaningful when
            # ``auth_choice == "custom-api-key"``; provisioning silently
            # ignores them for the other auth choices.
            "custom_base_url": body.get("custom_base_url"),
            "custom_compatibility": body.get("custom_compatibility"),
            "custom_provider_id": body.get("custom_provider_id"),
            "custom_model_id": body.get("custom_model_id"),
            "gateway_bind": body.get("gateway_bind") or "loopback",
            "allow_existing_user": bool(body.get("allow_existing_user", False)),
            "multi_user": bool(body.get("multi_user", False)),
            "backup_repo_url": body.get("backup_repo_url") or "",
            "network_path": network_path,
        }

        job_id = _new_job("wizard-provision")

        def _run() -> None:
            # Map provision_bot's on_stage callback to the job-log
            # protocol. Each stage emits start → terminal. Frontend
            # parses the messages to render the progress bar.
            level_map = {
                STATUS_OK: "success",
                STATUS_SKIP: "info",
                STATUS_FAIL: "error",
                STATUS_START: "info",
            }

            def on_stage(stage: str, status: str, detail: str = "") -> None:
                msg = f"{stage}: {status}"
                if detail:
                    msg = f"{msg}: {detail}"
                _job_log(job_id, msg, level_map.get(status, "info"))
                # Stash the latest stage state in result for the
                # frontend to read between polls.
                #
                # NOTE: ``dict.setdefault("result", {})`` is the WRONG idiom
                # here. _new_job() initialises ``job["result"] = None`` (line
                # 139 of server.py), so the key is present with a None value.
                # ``setdefault`` only acts when the key is *missing* — when
                # the key exists with a None value, it returns the existing
                # None unchanged. The next assignment then crashed with
                # ``'NoneType' object does not support item assignment``,
                # killing the wizard at the very first ``on_stage`` callback
                # (validate_inputs:start). Fix: explicitly normalise to a
                # dict before writing.
                with _jobs_lock:
                    j = _jobs.get(job_id)
                    if j is not None:
                        if not isinstance(j.get("result"), dict):
                            j["result"] = {}
                        j["result"]["last_stage"] = stage
                        j["result"]["last_status"] = status

            try:
                _job_log(job_id, f"Starting provision for {bot_id}…", "info")
                result = provision_bot(on_stage=on_stage, **kwargs)
                _job_finish(job_id, result={
                    "ok": result.success,
                    "bot_id": result.bot_id,
                    "user": result.user,
                    "uid": result.uid,
                    "port": result.port,
                    "role": result.role,
                    "created_user": result.created_user,
                    "stage_log": [
                        {"stage": s, "status": st, "detail": d}
                        for (s, st, d) in result.stage_log
                    ],
                    "rollback_log": result.rollback_log,
                    "failed_stage": result.failed_stage,
                }, error=result.error if not result.success else None)
            except Exception as exc:
                _log.exception("wizard provision: thread failed")
                _job_log(job_id, f"Provision failed: {exc}", "error")
                _job_finish(job_id, error=str(exc))

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return jsonify({"jobId": job_id, "status": "started"}), 202

    @app.post("/api/credentials/borrow")
    def api_credentials_borrow() -> Response:
        """Copy named provider entries from from_bot's auth-profiles to to_bot's.

        Body:
          {
            "from_bot": "evo",
            "to_bot": "atlas",
            "providers": ["brave", "anthropic"]
          }

        Returns 200 + `{ok, copied, skipped}` on success; 400 on
        validation error; 500 on write failure.
        """
        body = request.get_json(silent=True) or {}
        from_bot = (body.get("from_bot") or "").strip()
        to_bot = (body.get("to_bot") or "").strip()
        providers = body.get("providers") or []

        if not from_bot or not to_bot:
            return jsonify({"error": "from_bot and to_bot required"}), 400
        if from_bot == to_bot:
            return jsonify({"error": "from_bot and to_bot must differ"}), 400
        if not isinstance(providers, list) or not all(isinstance(p, str) for p in providers):
            return jsonify({"error": "providers must be a list of strings"}), 400
        if not providers:
            return jsonify({"error": "providers list cannot be empty"}), 400

        network = load_network(network_path)
        members = network.get("members", [])
        if from_bot not in members:
            return jsonify({"error": f"from_bot {from_bot!r} not a pod member"}), 400
        if to_bot not in members:
            return jsonify({"error": f"to_bot {to_bot!r} not a pod member"}), 400

        result = borrow_credentials(
            from_bot=from_bot,
            to_bot=to_bot,
            providers=providers,
            network=network,
        )
        status = 200 if result.get("ok") else 500
        return jsonify(result), status

    @app.post("/api/wizard/seed-model-config")
    def api_wizard_seed_model_config() -> Response:
        """Retrofit endpoint — bootstrap the model catalog for a bot that
        doesn't have one. Same operation as `evolve-admin seed-model-config`
        but callable from the admin UI so operators can fix atlas-shaped
        bots without dropping to the CLI.

        Body: {"bot_id": "atlas", "provider": "anthropic"}  (provider optional)
        Returns: {ok, seeded, primary, catalog_count, provider, reason}
        """
        from ..provisioning import seed_model_config_if_empty
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        preferred = body.get("provider") or None
        if not bot_id:
            return jsonify({"error": "bot_id required"}), 400
        network = load_network(network_path)
        if bot_id not in network.get("members", []):
            return jsonify({"error": f"bot_id {bot_id!r} not a pod member"}), 400
        result = seed_model_config_if_empty(
            bot_id, preferred_provider=preferred, network_path=network_path,
        )
        # Record the write in the audit log so the drift detector credits
        # it as operator-authorized (vs the false-positive "Unexplained
        # config drift" alert). Only log on actual seeds — skip-paths
        # ("already configured", "no LLM provider") aren't writes.
        if result.get("seeded"):
            try:
                from .server import _audit_log_entry as _audit
                _audit("wizard.seed_model_config", bot_id, {
                    "provider": result.get("provider"),
                    "catalog_count": result.get("catalog_count"),
                    "primary": result.get("primary"),
                }, oc_keys={"agents"})
            except Exception:
                pass  # Audit log is best-effort; never fail the request on it
        status = 200 if result.get("ok") else 500
        return jsonify(result), status

    @app.get("/api/wizard/borrow-candidates")
    def api_wizard_borrow_candidates() -> Response:
        """List bots configured for a given provider.

        Query: ?provider=<id>
        Returns: {"provider": "...", "bots": [{"bot_id": "...", "profile_keys": [...]}]}
        """
        provider = (request.args.get("provider") or "").strip()
        if not provider:
            return jsonify({"error": "provider query param required"}), 400
        network = load_network(network_path)
        bots = borrow_candidates(provider, network)
        return jsonify({"provider": provider, "bots": bots})

    @app.get("/api/wizard/cost-projection")
    def api_wizard_cost_projection() -> Response:
        """Projected cost envelope for a brand-new bot (cost-to-first-value).

        Powers the wizard's cost panel shown at the creation-confirmation step.
        Every figure is read from the resolver that actually *enforces* it, so
        the panel can never drift from enforcement and the UI carries no dollar
        literals:

          * the cap fields (provisioning ceiling + graduated/pod daily hard
            caps + window) come from ``better_engine_config`` — the same source
            the daily-cap + provisioning-ceiling enforcers read;
          * ``daily_alert_usd`` comes from
            ``spend_alert.daily_spend_alert_threshold_usd`` — the same value the
            spend alerter fires on (a network.json threshold, NOT a BE-config
            warn cap).

        Returns: ``{provisioning_ceiling_usd, daily_hard_cap_usd,
        daily_hard_cap_after_window_usd, daily_alert_usd, window_days}``.

        Fail-open: a read failure degrades to the compiled product defaults
        (via ``BetterEngineConfig.default()`` — pure, no I/O) so the panel still
        renders rather than blocking the wizard.
        """
        network = load_network(network_path)
        from evolve_config import get_shared_dir  # type: ignore

        shared_dir = get_shared_dir(network)

        projection: dict = {}
        try:
            from better_engine_config import (  # type: ignore
                BetterEngineConfig,
                load as _load_be,
            )

            try:
                be = _load_be(shared_dir)
            except Exception as exc:  # config unreadable — compiled defaults
                _log.warning(
                    "cost-projection: BE config read failed (%s); "
                    "using compiled defaults", exc,
                )
                be = BetterEngineConfig.default()
            projection = be.new_bot_cost_projection()
        except Exception as exc:  # better_engine_config unimportable
            _log.warning("cost-projection: better_engine_config unavailable (%s)", exc)

        # Daily-spend alert — the value the spend alerter fires on (network.json
        # alerts/thresholds), via its own accessor so it can't drift from what
        # actually alerts. Distinct source from the cap fields above.
        try:
            import spend_alert  # type: ignore

            projection["daily_alert_usd"] = spend_alert.daily_spend_alert_threshold_usd(
                network
            )
        except Exception as exc:
            _log.warning("cost-projection: alert threshold unavailable (%s)", exc)
            projection.setdefault("daily_alert_usd", None)

        return jsonify(projection)

    # ── Verification gauntlet ────────────────────────────────────────────────
    #
    # Spec: docs/spec-wizard-verification-gauntlet-2026-05-30.md.
    # Same endpoints drive the wizard's Screen 5 verification rows AND
    # the per-tile Verify modal. As of 2026-06-01 the gauntlet is three
    # checks (ownership / agent-dry-run / channel-handshake); the old
    # Check 4 "end-to-end echo" was retired in favor of the dedicated
    # pairing wizard modal.

    @app.post("/api/wizard/verify/start")
    def api_wizard_verify_start() -> Response:
        """Kick off the gauntlet in a background thread.

        Body: {bot_id}. Returns 202 + {jobId, status: "started"}.
        """
        from .server import _new_job, _job_log, _job_finish
        from ..wizard_verify import run_gauntlet

        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip().lower()
        if not bot_id:
            return jsonify({"error": "bot_id required"}), 400

        network = load_network(network_path)
        if bot_id not in network.get("bots", {}):
            return jsonify({"error": f"bot_id {bot_id!r} not in network.json"}), 400

        job_id = _new_job("wizard-verify")

        def _run() -> None:
            try:
                _job_log(job_id, f"Starting verify for {bot_id}…", "info")
                result = run_gauntlet(
                    bot_id,
                    network=network,
                    network_path=network_path,
                )
                payload = result.to_dict()
                # Mirror each check's outcome to the job log so the frontend
                # can render a live timeline.
                for c in result.checks:
                    lvl = (
                        "success" if c.status == "ok"
                        else "warning" if c.status == "warn"
                        else "info" if c.status == "skip" or c.status == "pending"
                        else "error"
                    )
                    _job_log(job_id, f"{c.name}: {c.status} — {c.summary}", lvl)
                _job_finish(job_id, result=payload)
            except Exception as exc:
                _log.exception("wizard verify: thread failed")
                _job_log(job_id, f"Gauntlet crashed: {exc}", "error")
                _job_finish(job_id, error=str(exc))

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return jsonify({"jobId": job_id, "status": "started"}), 202

    @app.get("/api/wizard/verify/<job_id>")
    def api_wizard_verify_status(job_id: str) -> Response:
        """Return the GauntletResult once available, plus the running log.

        Frontend polls every ~1.5s. While running, returns
        {status: "running", log: [...]}; once finished, returns
        {status: "complete", result: GauntletResult, log: [...]}.
        """
        from .server import _jobs, _jobs_lock
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is None:
                return jsonify({"error": f"job {job_id!r} not found"}), 404
            return jsonify({
                "jobId": job_id,
                "status": job["status"],
                "log": list(job["log"]),
                "result": job.get("result"),
                "error": job.get("error"),
            })

    @app.post("/api/wizard/verify/<bot_id>/fix-ownership")
    def api_wizard_verify_fix_ownership(bot_id: str) -> Response:
        """Chown a single path back to <bot_user>:staff.

        Body: {path}. Returns {ok, before, after, error?}.
        Path MUST be under /Users/<bot_user>/.openclaw/ — repair_ownership
        rejects anything else.
        """
        from ..wizard_verify import repair_ownership

        body = request.get_json(silent=True) or {}
        path = (body.get("path") or "").strip()
        if not path:
            return jsonify({"error": "path required"}), 400
        network = load_network(network_path)
        if bot_id not in network.get("bots", {}):
            return jsonify({"error": f"bot_id {bot_id!r} not in network.json"}), 400
        result = repair_ownership(bot_id, path, network=network)
        status = 200 if result.ok else 500
        return jsonify({
            "ok": result.ok,
            "path": result.path,
            "before": result.before,
            "after": result.after,
            "error": result.error,
        }), status

    # /arm-e2e, /e2e-status, /disarm-e2e were removed 2026-06-01 along
    # with the gauntlet's Check 4. Pairing now happens through the
    # dedicated wizard modal — see routes_pairing.py.


# ── Internal helper: name suggestion with optional LLM ────────────────────────


def _suggest_bot_id_with_optional_llm(description: str) -> str:
    """Try LLM-assisted suggestion via the pod's fast-role infra_llm
    target, else heuristic.

    Resolution (primary bot's credentialed providers) happens lazily on
    each preview, which is fine; previews are infrequent operator
    actions, not hot-path.

    Always returns a valid bot_id (never raises).
    """
    if not description.strip():
        return suggest_bot_id_heuristic(description)
    target = None
    try:
        from infra_llm import resolve_infra_llm  # type: ignore

        target = resolve_infra_llm("fast")
    except Exception:
        target = None
    if target is not None:
        return suggest_bot_id_via_llm(description, target)
    return suggest_bot_id_heuristic(description)
