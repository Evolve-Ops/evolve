"""
evolve_config.py — Shared configuration resolver for all Evolve analyzer scripts.

Resolution order for network.json:
  1. --network CLI flag (explicit override)
  2. /Users/Shared/evolve/network.json (canonical shared location)
  3. {script_dir}/network.json (local copy, legacy fallback)
  4. Empty defaults

This means scripts no longer need a local copy of network.json — they
always read from the shared directory. The local copy in each bot's
workspace/evolve/ is now deprecated and will be ignored if the shared
copy exists.

All Evolve analyzer scripts should import this module:
  from evolve_config import load_config, get_shared_dir
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import pwd
import secrets
from pathlib import Path
from typing import Any

from platform_profile import get_profile

logger = logging.getLogger(__name__)

# Canonical shared directory — this is the single source of truth.
# The default is platform-keyed (/Users/Shared/evolve on macOS,
# /var/lib/evolve on Linux — design doc 2026-06-10 §6); network.json's
# `sharedDir` overrides it via get_shared_dir().
CANONICAL_SHARED_DIR = Path(get_profile().shared_dir_default)
CANONICAL_NETWORK_JSON = CANONICAL_SHARED_DIR / "network.json"

# Current schema version
EVOLVE_VERSION = "0.1.0"

_SHARED_NETWORK = CANONICAL_NETWORK_JSON


def resolve_network_path(override: "Path | None" = None) -> Path:
    """
    Resolve the canonical network.json path.
    Priority: override arg > EVOLVE_NETWORK env > /Users/Shared/evolve/network.json > script-adjacent fallback
    """
    if override is not None:
        return override
    env = os.environ.get("EVOLVE_NETWORK")
    if env:
        return Path(env)
    if _SHARED_NETWORK.exists():
        return _SHARED_NETWORK
    # Fallback: look for network.json adjacent to this file (dev mode)
    local = Path(__file__).parent / "network.json"
    return local


def _migrate_network(network: dict) -> "tuple[dict, bool]":
    """
    Apply schema migrations in order. Returns (migrated_dict, was_changed).
    Add new migrations here as the schema evolves.
    """
    changed = False
    ver = network.get("evolveVersion", "0.0.0")

    # 0.0.0 -> 0.1.0: add evolveVersion field, add modules key if missing
    if ver == "0.0.0":
        network["evolveVersion"] = "0.1.0"
        if "modules" not in network:
            network["modules"] = {}
        changed = True
        ver = "0.1.0"

    # Future migrations go here:
    # if ver == "0.1.0":
    #     ... migrate to 0.2.0 ...
    #     network["evolveVersion"] = "0.2.0"
    #     changed = True

    return network, changed


def load_config(network_arg: "str | None" = None) -> "dict[str, Any]":
    """
    Load the Evolve network config from the best available source.
    Returns a config dict (never raises — returns empty dict on failure).
    Auto-migrates the schema if needed and writes back atomically.
    """
    candidates = []

    # 1. Explicit --network flag
    if network_arg:
        candidates.append(Path(network_arg))

    # 2. Canonical shared location
    candidates.append(CANONICAL_NETWORK_JSON)

    # 3. Local copy in same directory as calling script (legacy)
    caller_dir = Path(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(caller_dir / "network.json")

    for path in candidates:
        if path.exists():
            try:
                data = json.loads(path.read_text())
                data, changed = _migrate_network(data)
                if changed:
                    tmp = path.with_suffix(".json.tmp")
                    try:
                        tmp.write_text(json.dumps(data, indent=2))
                        tmp.rename(path)
                    except OSError:
                        pass  # best-effort migration
                return data
            except (json.JSONDecodeError, OSError):
                continue

    return {}


def get_shared_dir(config: dict[str, Any]) -> Path:
    """Return the shared directory from config, or the canonical default."""
    return Path(config.get("sharedDir", str(CANONICAL_SHARED_DIR)))


def get_bot_user(bot_id: str, config: "dict[str, Any] | None" = None) -> str:
    """Return the macOS username for a bot.

    bot_id (logical name in network.json) may differ from the macOS account
    name (when one bot lives on a personal/shared account). Falls back to bot_id
    when no override is configured.
    """
    if config is None:
        config = load_config()
    return (config.get("bots") or {}).get(bot_id, {}).get("user") or bot_id


def user_home(user: str) -> Path:
    """Resolve an OS ACCOUNT name's home directory via pwd.

    Companion to bot_home() for call sites that already hold the resolved
    account name (``bot_home(bot_id) == user_home(get_bot_user(bot_id))``).
    Never pass a bot_id here — bot_id is a logical name that may differ
    from the OS account; use bot_home() for logical ids.

    Falls back to profile-keyed construction ({user_home_root}/{user},
    i.e. /Users/{user} on macOS) when the account does not exist yet —
    e.g. paths computed before account creation, or tests on a dev box.
    """
    try:
        return Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        return Path(get_profile().user_home_root) / user


def bot_home(bot_id: str, config: "dict[str, Any] | None" = None) -> Path:
    """Resolve a bot's home directory via pwd, falling back to {user_home_root}/{user}."""
    return user_home(get_bot_user(bot_id, config))


def bot_label(bot_id: str, *, network_path: "Path | None" = None) -> str:
    """Return the bot's display_name from network.json, or bot_id if unset.

    Mirrors the JS botLabel() helper. Use anywhere operator-facing text
    embeds a bot identifier. Falls back to bot_id when no display_name
    is set so non-renamed bots render unchanged.
    """
    if not bot_id:
        return ""
    config = load_config(str(network_path) if network_path else None)
    cfg = (config.get("bots") or {}).get(bot_id) or {}
    name = cfg.get("display_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return bot_id


def get_members(config: dict[str, Any]) -> list[str]:
    """Return the list of member bot IDs."""
    return config.get("members", [])


def get_primary(config: dict[str, Any]) -> str | None:
    """Return the primary bot ID."""
    return config.get("primary")


def get_alerts(config: dict[str, Any]) -> dict[str, str]:
    """Return alert config {channel, chatId}."""
    return config.get("alerts", {})


# ── Module system ─────────────────────────────────────────────────────────────

# Default module configuration. Applied when 'modules' key is absent or
# a module entry is missing. Scripts read from this merged config.
DEFAULT_MODULES: dict[str, Any] = {
    # Master switch for the RSI feedback loop. When disabled, analysis/apply/
    # outcome daemons short-circuit so the pod stops spending model tokens
    # on improvement work — metrics stays on so dashboards keep populating
    # and healing keeps running.
    #
    # NB: there is no "observer" module key. The runtime observer is gated by
    # the plugin `tier` ladder (observer capability at monitor+), not by
    # network.json::modules — a key here would be dead (no is_module_enabled
    # consumer). The "Observer" stage shown in the RSI pipeline diagram is the
    # conceptual first stage, not a toggleable module.
    "rsi": {"enabled": True},
    "metrics": {"enabled": True, "retentionDays": 90},
    "healing": {
        "enabled": True,
        "checkIntervalMin": 5,
        "failuresBeforeAlert": 3,
        "slowThresholdMs": 3000,
        "restartCooldownMin": 10,
    },
    "analysis": {
        "enabled": True,
        "days": 7,
        "detectors": {
            "high_maintenance_ratio":  {"enabled": True,  "threshold": 0.30},
            "unexpected_billing_mode":  {"enabled": True},
            "declining_resolution":    {"enabled": True,  "threshold": 0.70},
            "zero_activity":           {"enabled": True,  "daysMissing": 3},
            "low_satisfaction":        {"enabled": True,  "minScore": 3},
            "promise_breach":          {"enabled": True,  "threshold": 0.40},
            "efficiency_problems":     {"enabled": True,  "threshold": 0.25},
            "capability_abandonment":  {"enabled": True},
            "promise_resolution":      {"enabled": True},
            "slack_quality_drop":      {"enabled": False},
            "detector_staleness":      {"enabled": True},
        },
    },
    "apply":            {"enabled": True},
    "continuity_engine": {
        "enabled": True,
        "idleThresholdMin": 15,
        "maxAgentTasksPerRun": 3,
        "budgetFloor": 0.10,
    },
    "expansion":        {"enabled": True,  "minSessionsForTheme": 3},
    "slack_signals":    {"enabled": False},
    "outcomes":         {"enabled": True},
    "cost":             {"enabled": True},
    "community_intel":  {
        "enabled": False,  # disabled by default — enable after validating first run
        "description": "Weekly external Kaizen scan — OC community intelligence",
        "schedule": "weekly_friday",
        "tier": "intelligence",
    },
}


def get_modules(config: dict[str, Any]) -> dict[str, Any]:
    """Return merged module config (defaults + any network.json overrides)."""
    overrides = config.get("modules", {})
    result: dict[str, Any] = {}
    for name, defaults in DEFAULT_MODULES.items():
        override = overrides.get(name, {})
        merged = {**defaults, **override}
        # Deep-merge detectors sub-dict if present
        if "detectors" in defaults and "detectors" in override:
            merged["detectors"] = {
                **defaults["detectors"],
                **{k: {**defaults["detectors"].get(k, {}), **v}
                   for k, v in override["detectors"].items()},
            }
        result[name] = merged
    # Include any custom modules not in defaults
    for name, cfg in overrides.items():
        if name not in result:
            result[name] = cfg
    return result


def is_module_enabled(config: dict[str, Any], module_name: str) -> bool:
    """Return True if the named module is enabled (default: True for all except
    continuity_engine and slack_signals which default to False)."""
    modules = get_modules(config)
    return bool(modules.get(module_name, {}).get("enabled", True))


def is_rsi_enabled(config: dict[str, Any]) -> bool:
    """Return True if the RSI feedback loop master switch is on. When False,
    the analysis/apply/outcome daemons must short-circuit so the pod stops
    spending model tokens on improvement work."""
    return is_module_enabled(config, "rsi")


def get_module_config(config: dict[str, Any], module_name: str) -> dict[str, Any]:
    """Return the full config dict for one module, with defaults merged in."""
    return get_modules(config).get(module_name, {})


def get_detector_enabled(config: dict[str, Any], detector_name: str) -> bool:
    """Return True if a specific analysis detector is enabled."""
    analysis = get_module_config(config, "analysis")
    detectors = analysis.get("detectors", {})
    detector = detectors.get(detector_name, {})
    return bool(detector.get("enabled", True))


def get_detector_threshold(
    config: dict[str, Any], detector_name: str, key: str, default: Any
) -> Any:
    """Return a threshold value for a specific detector, falling back to default."""
    analysis = get_module_config(config, "analysis")
    detectors = analysis.get("detectors", {})
    detector = detectors.get(detector_name, {})
    return detector.get(key, default)


def set_module_enabled(
    network_path: Path, module_name: str, enabled: bool
) -> None:
    """Persist an enable/disable change to network.json."""
    _patch_network_json(
        network_path,
        ["modules", module_name, "enabled"],
        enabled,
    )


def is_bot_module_enabled(
    config: dict[str, Any],
    bot_id: str,
    module_name: str,
    default: bool = True,
) -> bool:
    """Return True if a module is enabled for a specific bot.

    Per-bot opts live at ``config["bots"][bot_id][module_name]["enabled"]``.
    When the per-bot entry is absent the pod-wide default applies — pass
    ``default=True`` for modules that are default-on (e.g. continuity_engine).
    """
    bots = config.get("bots") or {}
    cfg = bots.get(bot_id) or {}
    module_cfg = cfg.get(module_name)
    if not isinstance(module_cfg, dict):
        return default
    return bool(module_cfg.get("enabled", default))


def set_bot_module_enabled(
    network_path: Path,
    bot_id: str,
    module_name: str,
    enabled: bool,
) -> None:
    """Persist a per-bot module enable/disable change to network.json."""
    _patch_network_json(
        network_path,
        ["bots", bot_id, module_name, "enabled"],
        enabled,
    )


def set_module_setting(
    network_path: Path, module_name: str, key: str, value: Any
) -> None:
    """Persist a tuning setting for a module to network.json."""
    _patch_network_json(
        network_path,
        ["modules", module_name, key],
        value,
    )


def set_detector_enabled(
    network_path: Path, detector_name: str, enabled: bool
) -> None:
    """Persist a detector enable/disable to network.json."""
    _patch_network_json(
        network_path,
        ["modules", "analysis", "detectors", detector_name, "enabled"],
        enabled,
    )


# ── HMAC signing (Phase 3a — proposal pipeline integrity) ─────────────────────

SIGNING_KEY_PATH = CANONICAL_SHARED_DIR / "keystore" / "evolve-signing.key"

# Marker recording that proposal signing has been enabled on this pod. Lives in
# the shared-dir ROOT, deliberately *outside* the keystore/ dir: if it lived
# next to the key, an accidental keystore loss (ACL hiccup, botched restore,
# stray rm) would clear both at once and silently re-open the fail-open hole.
# Once set, signature verification fails CLOSED when the key is missing rather
# than accepting unsigned proposals. The marker self-heals (see
# ``_load_signing_key``) so already-deployed pods that have a working key adopt
# enforcement with no migration step; a genuinely fresh pre-setup pod (key never
# generated) has no marker and still bootstraps.
SIGNING_ENFORCED_MARKER = CANONICAL_SHARED_DIR / ".proposal-signing-enabled"

# Proposal fields that are included in the HMAC payload (canonical, stable set).
# These are the fields written at proposal creation time. review_stamp and
# forge_sig are added later and verified separately.
_SIGNED_FIELDS = ("id", "type", "target_bot", "pattern_key", "proposed_change", "generated")


def _signing_enforced() -> bool:
    """Return True once proposal signing has been enabled on this pod.

    Independent of the key file's own presence — that is the whole point. When
    this is True but the key is absent, verification fails closed.
    """
    try:
        return SIGNING_ENFORCED_MARKER.exists()
    except OSError:
        return False


def _mark_signing_enforced() -> None:
    """Best-effort: record that signing is enabled. Idempotent; never raises."""
    try:
        if SIGNING_ENFORCED_MARKER.exists():
            return
        SIGNING_ENFORCED_MARKER.parent.mkdir(parents=True, exist_ok=True)
        tmp = SIGNING_ENFORCED_MARKER.with_suffix(".tmp")
        tmp.write_text("1\n")
        tmp.chmod(0o644)  # readable by every bot user, like the rest of shared/
        tmp.rename(SIGNING_ENFORCED_MARKER)
    except OSError:
        pass


def _load_signing_key() -> bytes | None:
    """Load the HMAC signing key from the keystore. Returns None if not present."""
    try:
        raw = SIGNING_KEY_PATH.read_text().strip()
        key = bytes.fromhex(raw)
    except (OSError, ValueError):
        return None
    # A usable key means signing is live on this pod — latch enforcement on so a
    # later key loss fails closed instead of silently reverting to fail-open.
    _mark_signing_enforced()
    return key


def generate_signing_key() -> None:
    """Generate a new HMAC signing key and write it to the keystore (0600)."""
    key_dir = SIGNING_KEY_PATH.parent
    key_dir.mkdir(parents=True, exist_ok=True)
    key_hex = secrets.token_hex(32)  # 32 random bytes
    tmp = SIGNING_KEY_PATH.with_suffix(".key.tmp")
    tmp.write_text(key_hex)
    tmp.chmod(0o600)
    tmp.rename(SIGNING_KEY_PATH)
    SIGNING_KEY_PATH.chmod(0o600)
    _mark_signing_enforced()


def sign_proposal(proposal: dict) -> str:
    """Compute HMAC-SHA256 over the canonical proposal fields. Returns hex digest.

    Signs the stable set of fields written at creation time. Call this
    after building the full_proposal dict, before writing to disk.
    Returns empty string if the signing key is not available (key not yet
    generated — new pod setup or key missing). Callers should log a warning
    when empty is returned but must not block proposal creation.
    """
    key = _load_signing_key()
    if key is None:
        return ""
    payload_parts = []
    for field in _SIGNED_FIELDS:
        value = proposal.get(field, "")
        # Stable serialization: strings as-is, everything else as compact JSON
        if isinstance(value, str):
            payload_parts.append(f"{field}:{value}")
        else:
            payload_parts.append(f"{field}:{json.dumps(value, sort_keys=True, separators=(',', ':'))}")
    payload = "\n".join(payload_parts).encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_proposal_sig(proposal: dict) -> bool:
    """Verify the evolve_sig field in a proposal. Returns True if valid.

    Fails CLOSED when signing has been enabled on this pod but the key is
    missing/unreadable (a key loss must not silently accept unsigned
    proposals). Only a genuinely fresh, never-keyed pod bootstraps open.
    """
    key = _load_signing_key()
    if key is None:
        if _signing_enforced():
            logger.error(
                "Proposal signing enforced but signing key missing/unreadable "
                "(%s); refusing proposal %s — failing closed.",
                SIGNING_KEY_PATH,
                proposal.get("id", "<unknown>"),
            )
            return False
        return True  # never-keyed pod — bootstrap open
    stored_sig = proposal.get("evolve_sig", "")
    if not stored_sig:
        return False
    expected = sign_proposal(proposal)
    return hmac.compare_digest(stored_sig, expected)


def sign_review_stamp(stamp: dict) -> str:
    """Compute HMAC over a review stamp {proposal_id, reviewed_at, result}."""
    key = _load_signing_key()
    if key is None:
        return ""
    payload = (
        f"proposal_id:{stamp.get('proposal_id', '')}\n"
        f"reviewed_at:{stamp.get('reviewed_at', '')}\n"
        f"result:{stamp.get('result', '')}"
    ).encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_review_stamp(proposal: dict) -> bool:
    """Verify review_stamp.sig in an approved proposal.

    Fails closed when signing is enforced but the key is missing (see
    ``verify_proposal_sig``).
    """
    key = _load_signing_key()
    if key is None:
        if _signing_enforced():
            logger.error(
                "Proposal signing enforced but signing key missing/unreadable "
                "(%s); refusing review stamp for %s — failing closed.",
                SIGNING_KEY_PATH,
                proposal.get("id", "<unknown>"),
            )
            return False
        return True
    stamp = proposal.get("review_stamp")
    if not stamp or not isinstance(stamp, dict):
        return False
    stored_sig = stamp.get("sig", "")
    if not stored_sig:
        return False
    expected = sign_review_stamp(stamp)
    return hmac.compare_digest(stored_sig, expected)


def sign_forge_result(result: dict) -> str:
    """Compute HMAC over a forge result {proposal_id, recommendation, validated_at}."""
    key = _load_signing_key()
    if key is None:
        return ""
    payload = (
        f"proposal_id:{result.get('proposal_id', '')}\n"
        f"recommendation:{result.get('recommendation', '')}\n"
        f"validated_at:{result.get('validated_at', '')}"
    ).encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_forge_result_sig(result: dict) -> bool:
    """Verify forge_sig in a forge result dict.

    Fails closed when signing is enforced but the key is missing (see
    ``verify_proposal_sig``).
    """
    key = _load_signing_key()
    if key is None:
        if _signing_enforced():
            logger.error(
                "Proposal signing enforced but signing key missing/unreadable "
                "(%s); refusing forge result for %s — failing closed.",
                SIGNING_KEY_PATH,
                result.get("proposal_id", "<unknown>"),
            )
            return False
        return True
    stored_sig = result.get("forge_sig", "")
    if not stored_sig:
        return False
    expected = sign_forge_result(result)
    return hmac.compare_digest(stored_sig, expected)


def _patch_network_json(network_path: Path, key_path: list[str], value: Any) -> None:
    """Atomically update a nested key in network.json."""
    data = json.loads(network_path.read_text()) if network_path.exists() else {}
    node = data
    for k in key_path[:-1]:
        node = node.setdefault(k, {})
    node[key_path[-1]] = value
    payload = json.dumps(data, indent=2)
    tmp = network_path.with_suffix(".json.tmp")
    try:
        tmp.write_text(payload)
        tmp.rename(network_path)
    except PermissionError:
        # Admin server lacks write access — fall back to sudo tee
        import subprocess, tempfile, os
        fd, tmp_path = tempfile.mkstemp(suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            subprocess.run(
                ["sudo", "cp", tmp_path, str(network_path)],
                check=True, timeout=10,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
