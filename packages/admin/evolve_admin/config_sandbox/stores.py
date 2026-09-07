"""stores — physical-file resolution for the sandbox's logical store names.

Bridges the schema's logical address (``Store.NETWORK``, ``target_path``) to
the actual files on disk. The sandbox's read API uses these helpers; it does
not write through them (writes still go through each store's existing
applier / endpoint, by design — see spec §7.3).

Reads from bot-side files (openclaw.json, evolve-tiers.json, SOUL.md,
AGENTS.md) follow the CLAUDE.md pattern: try ``Path.read_text`` first
(succeeds via the macOS ACL set by ``deploy.set_evolve_read_acl``), fall
back to ``sudo /bin/cat`` for bots not yet deployed through that path.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import Store, TunableKey


_DEFAULT_SHARED_DIR = Path("/Users/Shared/evolve")
_DEFAULT_NETWORK_JSON = _DEFAULT_SHARED_DIR / "network.json"


# Sentinel meaning "key path not present in the file" — distinct from a key
# whose value is literally None. Callers use ``Found.found`` to disambiguate.
_MISSING = object()


@dataclass(frozen=True)
class Found:
    """Result of a store read.

    Attributes:
        found: True if the key was present in the backing file.
        value: The current value (None if not present).
        source_path: Filesystem path that was read (None if no file existed).
    """

    found: bool
    value: Any
    source_path: Path | None = None


def _read_text_with_fallback(path: Path) -> str | None:
    """Read a file's text. Try direct read first, fall back to sudo /bin/cat."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except PermissionError:
        # Fallback: sudo /bin/cat (sudoers grant for evolve user).
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout if r.returncode == 0 else None


def _read_json(path: Path) -> dict | None:
    text = _read_text_with_fallback(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _walk(data: Any, dotted: str) -> Any:
    """Walk a dotted key path through a nested dict. Returns _MISSING if absent.

    A segment of ``*`` is a wildcard: at that point we collect the
    sub-walk result from every child key whose name starts with
    ``anthropic/`` (matching the schema convention for fan-out keys
    like ``agents.defaults.models.*.params.cacheRetention``). The walk
    returns the representative value if every contributing child agrees;
    if they disagree, the first non-MISSING value wins so the
    customizations UI surfaces a divergence rather than masking it.
    Returns ``_MISSING`` when no contributing child has the suffix path.
    """
    parts = dotted.split(".")
    return _walk_parts(data, parts)


def _walk_parts(cur: Any, parts: list[str]) -> Any:
    """Recursive variant of ``_walk`` so the wildcard branch can re-enter
    the walk at each candidate child."""
    for i, k in enumerate(parts):
        if k == "*":
            if not isinstance(cur, dict):
                return _MISSING
            suffix = parts[i + 1:]
            values: list[Any] = []
            for child_key, child_val in cur.items():
                # Wildcard fan-out matches the same provider prefix the
                # materializer fans out across — currently Anthropic only.
                # Other catalog entries don't carry the per-Anthropic
                # parameters that motivated wildcards (e.g.
                # ``params.cacheRetention``) and including them would
                # surface noise as "divergence" in the customizations UI.
                if not (isinstance(child_key, str) and child_key.startswith("anthropic/")):
                    continue
                sub = _walk_parts(child_val, suffix) if suffix else child_val
                if sub is _MISSING:
                    continue
                values.append(sub)
            if not values:
                return _MISSING
            # Return representative value: the first child's value when
            # all agree, otherwise the first non-MISSING value so the
            # customizations diff still surfaces "this bot's catalog has
            # at least one non-default value here".
            return values[0]
        if not isinstance(cur, dict) or k not in cur:
            return _MISSING
        cur = cur[k]
    return cur


# ─────────────────────────────────────────────────────────────────────────────
# File-path resolution
# ─────────────────────────────────────────────────────────────────────────────


def _bot_oc_dir(bot_id: str, network: "dict[str, Any] | None" = None) -> Path:
    """Path to a bot's ~/.openclaw/ directory.

    Resolves via get_bot_user(bot_id, network) so that bots whose macOS
    account name differs from their logical bot_id (e.g. team_bot_b/personal_bot_user) are
    handled correctly. Falls back to bot_id == account_name when no network
    is available.
    """
    try:
        from evolve_admin.config import get_bot_user, load_network
        net = network or load_network()
        user = get_bot_user(bot_id, net)
    except Exception:
        user = bot_id
    return Path(f"/Users/{user}/.openclaw")


def resolve_file_path(
    entry: TunableKey,
    *,
    shared_dir: Path,
    bot_id: str | None = None,
    gen_id: str | None = None,
    network_json: Path | None = None,
    network: "dict[str, Any] | None" = None,
) -> Path | None:
    """Map a schema entry to its physical file path.

    Returns None when the entry needs a parameter (bot_id / gen_id) the
    caller didn't supply.

    ``network`` — parsed network.json dict; used to resolve the macOS account
    name for bots where bot_id differs from the account name (e.g. team_bot_b/personal_bot_user).
    When None, network.json is loaded on demand via load_network().
    """
    file_part = entry.target_path.split("::", 1)[0]

    if entry.store == Store.NETWORK:
        return network_json or (shared_dir / "network.json")

    if entry.store == Store.BETTER_ENGINE:
        return shared_dir / "better-engine-config.json"

    if entry.store == Store.BOT_GUIDE:
        if bot_id is None:
            return None
        return shared_dir / "bot_guides" / f"{bot_id}.md"

    # Bot-side stores (per_bot=True). Need bot_id.
    if entry.store in (Store.OPENCLAW, Store.EVOLVE_TIERS, Store.SOUL,
                        Store.AGENTS, Store.MANIFEST):
        if bot_id is None:
            return None
        oc = _bot_oc_dir(bot_id, network)
        if entry.store == Store.OPENCLAW:
            return oc / "openclaw.json"
        if entry.store == Store.EVOLVE_TIERS:
            return oc / "evolve-tiers.json"
        if entry.store == Store.SOUL:
            return oc / "SOUL.md"
        if entry.store == Store.AGENTS:
            return oc / "AGENTS.md"
        if entry.store == Store.MANIFEST:
            # Manifests are a directory; this resolver handles whole-doc only.
            return None

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Store read
# ─────────────────────────────────────────────────────────────────────────────


def read_value(
    entry: TunableKey,
    *,
    shared_dir: Path,
    bot_id: str | None = None,
    gen_id: str | None = None,
    network_json: Path | None = None,
    network: "dict[str, Any] | None" = None,
) -> Found:
    """Read the current value for a schema entry from its native store.

    For doc-typed entries (bot_guide, SOUL, AGENTS), the value is the raw
    file contents as a string. For key-level entries, the value is whatever
    the dotted-key walk yields.
    """
    path = resolve_file_path(
        entry,
        shared_dir=shared_dir,
        bot_id=bot_id,
        gen_id=gen_id,
        network_json=network_json,
        network=network,
    )
    if path is None:
        return Found(found=False, value=None, source_path=None)

    if entry.type_hint == "doc":
        text = _read_text_with_fallback(path)
        if text is None:
            return Found(found=False, value=None, source_path=path)
        return Found(found=True, value=text, source_path=path)

    data = _read_json(path)
    if data is None:
        return Found(found=False, value=None, source_path=path)

    _, _, dotted = entry.target_path.partition("::")
    walked = _walk(data, dotted)
    if walked is _MISSING:
        return Found(found=False, value=None, source_path=path)
    return Found(found=True, value=walked, source_path=path)


def default_shared_dir() -> Path:
    return _DEFAULT_SHARED_DIR


def default_network_json() -> Path:
    return _DEFAULT_NETWORK_JSON
