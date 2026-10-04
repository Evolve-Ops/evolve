"""Brave API key resolution for deploy-time plugin gating.

Lives outside ``deploy.py`` because that file is a frozen hot file at its
no-growth cap (file-size ratchet, 4.1a) — the same reason #3219 moved
``brave_key_from_oc_config`` into ``web/credentials_oc``.

Why this module exists at all: ``plugins.entries.brave.enabled = true`` is a
CAPABILITY CLAIM. The Skills page, the plugin monitor, and the bot's own tool
listing all read it as "web search works". Writing it without an API key is
what produced the fleet-wide silent failure found 2026-07-31 — 6 of 9 mini
bots and VPS evo advertised a ``web_search`` tool that 401s at call time,
while every status surface reported health. ``resolve_pod_brave_key`` is the
single gate deploy consults before making that claim.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def read_oc_cfg_quiet(bot_id: str, network: dict) -> dict:
    """Best-effort read of a bot's openclaw.json as a dict; {} on any failure.

    Direct read first (the evolve user has an ACL on ``.openclaw/``), then
    ``sudo /bin/cat`` for bots whose home isn't searchable by the caller —
    the same two-step ``ensure_plugin_config`` uses inline. Callers that only
    need to *inspect* config (not rewrite it) use this instead of duplicating
    the read-or-explain ladder.
    """
    from .config import get_bot_user
    from .deploy import _PROFILE, _user_home

    try:
        # get_bot_user (network in hand), NOT bot_user_for: the latter is a
        # self-loading convenience that falls back to the bot_id on any
        # failure, which silently reads the WRONG home for any bot whose
        # macOS account differs from its bot id (network.json `user` field).
        # Such a bot would appear to have no key and get the pod key written
        # over the top of the one it already had.
        oc_json = _user_home(get_bot_user(bot_id, network)) / ".openclaw/openclaw.json"
    except Exception as exc:  # noqa: BLE001
        print(f"[evolve/deploy] {bot_id}: openclaw.json path resolution failed: {exc}")
        return {}
    content: str | None = None
    try:
        content = oc_json.read_text()
    except OSError:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(oc_json)],
                capture_output=True, text=True, cwd=_PROFILE.scratch_dir,
            )
            if r.returncode == 0:
                content = r.stdout
        except Exception:  # noqa: BLE001
            return {}
    if not content:
        return {}
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def resolve_pod_brave_key(
    bot_id: str, network: dict, cfg: dict | None = None,
) -> str | None:
    """Return the Brave API key this bot should use, or None if there isn't one.

    Resolution order (first hit wins):

      1. **This bot's existing key** in ``openclaw.json`` (canonical
         ``plugins.entries.brave.config.webSearch.apiKey``, or the legacy
         ``tools.web.search.apiKey``). Gap-fill must never clobber a key an
         operator set by hand or via the Credentials tab — ``keys sync`` is
         the deliberate fan-out path, not this one.
      2. **The pod keystore's shared-scope brave key**, when one is registered
         AND authorized for this bot. This is the pod-wide source of truth:
         an operator runs ``evolve-admin keys add brave --provider brave
         --scope shared`` + ``keys set brave`` once, and every subsequent
         deploy fans it out to new bots automatically. (Shared-scope keys are
         what the keystore was always documented to serve — its module
         docstring names "Brave API" as the canonical example.)

    Returns None when neither exists — the signal to callers that Brave must
    NOT be enabled.

    Never raises: a broken or absent keystore degrades to "no pod key", which
    is the safe direction (install-but-don't-enable, never enable-without-key).
    """
    if cfg is None:
        cfg = read_oc_cfg_quiet(bot_id, network)

    # 1. Key already on this bot.
    try:
        from .web.credentials_oc import brave_key_from_oc_config
        existing = brave_key_from_oc_config(cfg)
        if existing:
            return existing
    except Exception as exc:  # noqa: BLE001 — detection must never fail a deploy
        # Fall through to the keystore rather than dying. Logged, not
        # swallowed: if this fires we'd otherwise clobber a bot's own key
        # with the pod key on the next deploy.
        print(f"[evolve/deploy] {bot_id}: brave own-key detection failed: {exc}")

    # 2. Pod keystore shared-scope key.
    try:
        from .config import DEFAULT_SHARED_DIR
        from .keystore import KeystoreManager
        mgr = KeystoreManager(Path(network.get("sharedDir") or DEFAULT_SHARED_DIR))
        for entry in mgr.ks.list_keys():
            if (entry.get("provider") or "").lower() != "brave":
                continue
            if entry.get("scope") not in ("shared", "group"):
                continue
            authorized = entry.get("bots")  # None = all bots
            if authorized is not None and bot_id not in authorized:
                continue
            value = mgr.get_value(entry["name"])
            if value:
                return value
    except Exception as exc:  # noqa: BLE001 — keystore is optional infrastructure
        print(f"[evolve/deploy] {bot_id}: brave keystore lookup skipped: {exc}")

    return None
