"""
cost_profiles.py — Cost containment profile management for Evolve pods.

Profiles are named bundles of OpenClaw cost settings (compaction, heartbeat,
context pruning, session reset) that an admin can apply to one or all bots in
one operation — no terminal access required.

Built-in profiles: conservative, balanced, unrestricted-debug
Custom profiles: stored in {sharedDir}/cost-profiles.json

The legacy name "performance" maps to "unrestricted-debug" via _PROFILE_ALIASES
so persisted profile names from before the 2026-06-07 rename keep resolving.
The rename happened because the old profile claimed "context continuity" as
the trade-off but actually delivered zero continuity at any heartbeat interval
≥ 5 minutes (Anthropic prompt cache TTL): it paid full input cost on every tick
with no cache benefit. See docs/principle-apps-minimize-bootstrap-cost.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evolve_config import bot_home as _bot_home

# ── Built-in profiles ─────────────────────────────────────────────────────────

BUILTIN_PROFILES: dict[str, dict] = {
    "conservative": {
        "name": "conservative",
        "builtin": True,
        "label": "Conservative",
        "description": "Maximum cost containment. Recommended for ambient/background bots.",
        "expected_savings": "80–90% vs unconfigured",
        "settings": {
            "heartbeat": {
                "isolatedSession": True,
                "lightContext": True,
                "model": "anthropic/claude-haiku-4-5",
            },
            "contextPruning": {"mode": "cache-ttl", "ttl": "1h", "keepLastAssistants": 3},
            "compaction": {
                "mode": "safeguard",
                "reserveTokensFloor": 30000,
                "memoryFlush": {"enabled": True, "softThresholdTokens": 6000},
            },
            "bootstrapTotalMaxChars": 60000,
            "session": {"reset": {"idleMinutes": 120}},
            # cache_retention lives in better-engine-config.json (not openclaw.json).
            # `long` is cheaper end-to-end because it eliminates TTL-invalidation
            # rewrites on multi-minute conversational gaps — Anthropic's 1-hour
            # cache window costs ~2x per write but saves several cache writes per
            # session. apply_profile_to_bot routes this field to BE config; the
            # openclaw.json writer skips it.
            "cache_retention": "long",
        },
    },
    "balanced": {
        "name": "balanced",
        "builtin": True,
        "label": "Balanced",
        "description": "Production-ready defaults. Good for interactive bots that need context continuity.",
        "expected_savings": "50–70% vs unconfigured",
        "settings": {
            "heartbeat": {
                "isolatedSession": True,
                "lightContext": True,
                "model": "anthropic/claude-haiku-4-5",
            },
            "contextPruning": {"mode": "cache-ttl", "ttl": "4h", "keepLastAssistants": 5},
            "compaction": {
                "mode": "safeguard",
                "reserveTokensFloor": 50000,
                "memoryFlush": {"enabled": True, "softThresholdTokens": 10000},
            },
            "bootstrapTotalMaxChars": 100000,
            "session": {"reset": {"idleMinutes": 240}},
            "cache_retention": "long",
        },
    },
    "unrestricted-debug": {
        "name": "unrestricted-debug",
        "builtin": True,
        "label": "Unrestricted (debug only)",
        "description": (
            "WARNING: heartbeat reuses the full session context on every tick. "
            "At heartbeat intervals ≥ 5 minutes (the typical case) Anthropic's "
            "prompt cache TTL expires between ticks, so this profile pays full "
            "input cost on every heartbeat with no cache benefit. Use only when "
            "actively debugging sub-5min cadence prompt-cache issues; revert "
            "to balanced when done."
        ),
        "expected_savings": "negative vs balanced at any interval ≥ 5 min",
        "settings": {
            "heartbeat": {"isolatedSession": False, "lightContext": False},
            "contextPruning": {"mode": None},
            "compaction": {"mode": "safeguard", "reserveTokensFloor": 10000},
            "bootstrapTotalMaxChars": None,
            "session": {"reset": {"idleMinutes": None}},
            # Unrestricted-debug leaves cache_retention unset — inherit OC's
            # compiled default ("short" / 5-min TTL). Distinct from
            # Conservative/Balanced which explicitly opt into "long".
            "cache_retention": None,
        },
    },
}

# Legacy profile names. Resolved by get_profile() so persisted cost-settings
# snapshots referencing the old name still load. The 2026-06-07 rename was
# motivated by the Atlas heartbeat-bloat incident: "Performance" misled an
# operator into selecting a profile whose real semantics were "negative
# savings, no continuity benefit." See docs/principle-apps-minimize-bootstrap-cost.md.
_PROFILE_ALIASES: dict[str, str] = {
    "performance": "unrestricted-debug",
}

# Fields the BE config owns rather than openclaw.json. apply_profile_to_bot
# and the openclaw-cost PATCH endpoint pull these out of the cost-settings
# dict and route them to better_engine_config setters; write_openclaw_cost_settings
# never sees them. (Keeping the list module-level so the routing logic and
# the schema documentation can't drift.)
BE_CONFIG_PROFILE_FIELDS = ("cache_retention",)

# Keys we read/write. Most live under agents.defaults in the openclaw schema;
# `session` lives at the ROOT (openclaw rejects agents.defaults.session as an
# unrecognized key, then doctor --fix rolls the file back from .bak — wiping
# every other change in the same save). _deep_merge_cost strips stale copies
# from the wrong location on every write to self-heal old configs.
COST_FIELDS_DEFAULTS = ["heartbeat", "contextPruning", "compaction", "bootstrapTotalMaxChars"]
COST_FIELDS_ROOT = ["session"]
COST_FIELDS = COST_FIELDS_DEFAULTS + COST_FIELDS_ROOT


# ── Cost settings snapshot ───────────────────────────────────────────────────
#
# Snapshots persist a bot's intentional cost configuration in the shared dir so
# it survives openclaw reinstalls, doctor --fix repairs, and bot user recreation.
# Written whenever settings are applied via the admin UI; restored by deploy.py
# during ensure_plugin_config if the bot's openclaw.json is missing cost fields.

def _snapshot_path(bot_id: str, shared_dir: Path) -> Path:
    return shared_dir / "cost-settings" / f"{bot_id}.json"


def save_cost_snapshot(bot_id: str, settings: dict, shared_dir: Path) -> None:
    """Persist the bot's cost settings to the shared dir.

    Called after every successful write so the config survives reinstalls.
    Silently skips on error — snapshot is best-effort, not load-bearing.
    """
    snap_dir = shared_dir / "cost-settings"
    snap_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "bot_id": bot_id,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "settings": dict(settings),  # preserve None — means field was explicitly disabled
    }
    fd, tmp = tempfile.mkstemp(dir="/tmp", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        import shutil
        shutil.copy2(tmp, str(_snapshot_path(bot_id, shared_dir)))
    except Exception:
        pass
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load_cost_snapshot(bot_id: str, shared_dir: Path) -> dict:
    """Load the last-saved cost settings for a bot.

    Returns an empty dict if no snapshot exists or it cannot be read.
    """
    try:
        data = json.loads(_snapshot_path(bot_id, shared_dir).read_text())
        return data.get("settings", {})
    except Exception:
        return {}


# ── Custom profile storage ────────────────────────────────────────────────────

def _profiles_path(shared_dir: Path) -> Path:
    return shared_dir / "cost-profiles.json"


def load_custom_profiles(shared_dir: Path) -> list[dict]:
    fp = _profiles_path(shared_dir)
    if not fp.exists():
        return []
    try:
        data = json.loads(fp.read_text())
        return data.get("custom_profiles", [])
    except (json.JSONDecodeError, OSError):
        return []


def save_custom_profiles(shared_dir: Path, profiles: list[dict]) -> None:
    fp = _profiles_path(shared_dir)
    fd, tmp = tempfile.mkstemp(dir="/tmp", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"custom_profiles": profiles}, f, indent=2)
        import shutil
        shutil.copy2(tmp, fp)
        os.chmod(fp, 0o644)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def list_profiles(shared_dir: Path) -> list[dict]:
    """Return all profiles — built-ins first, then custom."""
    custom = load_custom_profiles(shared_dir)
    return list(BUILTIN_PROFILES.values()) + custom


def get_profile(name: str, shared_dir: Path) -> dict | None:
    # Aliases first so legacy snapshots/UI calls resolve. _PROFILE_ALIASES is
    # the single point of legacy-name handling; do not pepper the rename
    # across the rest of the module.
    canonical = _PROFILE_ALIASES.get(name, name)
    if canonical in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[canonical]
    for p in load_custom_profiles(shared_dir):
        if p.get("name") == canonical:
            return p
    return None


def save_as_profile(name: str, settings: dict, shared_dir: Path) -> dict:
    """Save current settings as a named custom profile. Overwrites if exists."""
    profiles = [p for p in load_custom_profiles(shared_dir) if p.get("name") != name]
    profile = {
        "name": name,
        "builtin": False,
        "label": name,
        "description": f"Custom profile saved {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "settings": settings,
    }
    profiles.append(profile)
    save_custom_profiles(shared_dir, profiles)
    return profile


def delete_custom_profile(name: str, shared_dir: Path) -> bool:
    if name in BUILTIN_PROFILES:
        return False  # cannot delete built-ins
    profiles = load_custom_profiles(shared_dir)
    new_profiles = [p for p in profiles if p.get("name") != name]
    if len(new_profiles) == len(profiles):
        return False  # not found
    save_custom_profiles(shared_dir, new_profiles)
    return True


# ── Signal emissions ─────────────────────────────────────────────────────────


def _emit_signal_safe(*, shared_dir: Path, signature: str, sig_type: str,
                       severity: str, title: str, body: str,
                       details: dict | None = None) -> None:
    """Emit a Signal with best-effort import + best-effort call.

    The cost-profile module is imported in environments where the Signal
    store isn't always available (CLI, tests, forge-time). Failing the
    Signal emission must NOT fail the underlying write — the operator
    already got the config change they asked for.
    """
    try:
        from signals import store as signals_store  # type: ignore
    except ImportError:
        return
    try:
        signals_store.observe(
            shared_dir,
            signature=signature,
            producer="cost_profiles",
            type=sig_type,
            flavor="config_change",
            severity=severity,
            scope="bot",
            title=title,
            body=body,
            details=details or {},
        )
    except Exception:  # noqa: BLE001
        # Signal store is best-effort here; suppress so a Signal-store
        # outage doesn't break cost-profile writes.
        pass


def emit_unrestricted_profile_applied_signal(
    bot_id: str, shared_dir: Path,
) -> None:
    """Emit `cost_profile_unrestricted_applied` so the operator (and
    Evo) have a paper trail when the unrestricted-debug profile lands.

    Severity info — the profile is *legitimate* in narrow debug
    contexts. The signal exists for observability, not to block.
    """
    _emit_signal_safe(
        shared_dir=shared_dir,
        signature=f"cost_profiles:unrestricted_profile_applied:{bot_id}",
        sig_type="cost_profile_unrestricted_applied",
        severity="info",
        title=f"Unrestricted-debug profile applied to {bot_id}",
        body=(
            "The unrestricted-debug cost profile sets heartbeat.lightContext=false "
            "and isolatedSession=false. At typical heartbeat intervals this is a "
            "no-win combination — full input cost on every tick with no cache hit. "
            "Revert to the balanced profile when done debugging."
        ),
        details={"bot_id": bot_id, "profile": "unrestricted-debug"},
    )


def emit_cost_setting_forced_signal(
    bot_id: str, settings_excerpt: dict, shared_dir: Path,
) -> None:
    """Emit `cost_setting_forced` when an operator passes ?force=1 to
    skip the preflight gate.

    Severity info — the operator explicitly overrode the gate; we're
    recording the event, not raising an alarm.
    """
    hb = settings_excerpt.get("heartbeat") or {}
    _emit_signal_safe(
        shared_dir=shared_dir,
        signature=f"cost_profiles:cost_setting_forced:{bot_id}",
        sig_type="cost_setting_forced",
        severity="info",
        title=f"Cost-setting preflight overridden on {bot_id}",
        body=(
            "An operator force-applied a cost setting that would normally trip the "
            "preflight gate (lightContext=false + every>=1h). Check the bot's "
            "Cost detail page if heartbeat-bloat alerts start firing."
        ),
        details={"bot_id": bot_id, "heartbeat": hb},
    )


# ── Preflight gates ──────────────────────────────────────────────────────────


# Heartbeat intervals shorter than this round-trip Anthropic's prompt cache
# (5 min default, 1h max with extended cache). Above this, lightContext: false
# is pure waste — full session context written to cache every tick, cache
# always cold by the next tick, full input price paid for no benefit.
#
# 1h is the conservative ceiling: even with Anthropic's longest cache window
# (`cache_control: ttl: "1h"`), a 1h heartbeat interval just barely fits and
# typical conversational gaps push it past expiry. >1h is unambiguously
# wasteful with lightContext: false.
_PROMPT_CACHE_CEILING_SECONDS = 60 * 60  # 1 hour


def _parse_every_to_seconds(every: str | None) -> int | None:
    """Parse an OC heartbeat interval string ("5m", "1h", "30s") to seconds.

    Returns None when the value can't be parsed — caller treats unknown
    as "skip the gate" rather than misclassify a legitimate config.
    """
    if not isinstance(every, str) or not every:
        return None
    s = every.strip().lower()
    if not s:
        return None
    unit = s[-1]
    try:
        n = int(s[:-1])
    except (ValueError, TypeError):
        return None
    if unit == "s":
        return n
    if unit == "m":
        return n * 60
    if unit == "h":
        return n * 3600
    if unit == "d":
        return n * 86400
    return None


def preflight_heartbeat_combination(merged_settings: dict) -> str | None:
    """Return an error message if the merged config trips the heartbeat
    gate; return None when the config is acceptable.

    Gate: `heartbeat.lightContext = false` AND `heartbeat.every >= 1h`.
    Either condition alone passes — only the combination fails. See
    the principle doc for the rationale.

    `merged_settings` is the parsed openclaw.json AFTER the proposed
    write would be applied — the gate evaluates the *outcome*, not the
    delta. That way a partial PATCH that only touches `lightContext`
    correctly resolves `every` from the bot's current config.
    """
    defaults = merged_settings.get("agents", {}).get("defaults", {}) or {}
    hb = defaults.get("heartbeat") or {}
    if not isinstance(hb, dict):
        return None

    light_context = hb.get("lightContext")
    # Treat unset lightContext as the OC default of "true" — the gate fires
    # only on an explicit false. (Operators who haven't decided are not in
    # the wasteful state.)
    if light_context is not False:
        return None

    every_seconds = _parse_every_to_seconds(hb.get("every"))
    if every_seconds is None:
        # Unparseable interval: don't gate. The runtime will pick a default
        # and we can't tell at this layer whether it falls inside the
        # prompt-cache window.
        return None

    if every_seconds < _PROMPT_CACHE_CEILING_SECONDS:
        # Sub-1h heartbeat — Anthropic's extended-TTL cache can plausibly hit
        # tick-to-tick. Operator may legitimately be debugging cache shape.
        return None

    return (
        "heartbeat.lightContext=false combined with heartbeat.every>=1h has "
        "no valid use case — Anthropic's prompt cache TTL expires between "
        "ticks (5 min default / 1 h max with extended cache), so every "
        "heartbeat pays full input price for ~0% cache hit. "
        "Flip lightContext to true or shorten heartbeat.every below 5 min. "
        "Set ?force=1 to override (emits cost_setting_forced Signal)."
    )


# ── OpenClaw config read/write ────────────────────────────────────────────────

def read_openclaw_cost_settings(bot_id: str) -> dict | None:
    """Read the cost-relevant fields from a bot's openclaw.json.

    Tries direct read first; falls back to sudo -u {bot_id} cat if permission denied.
    Returns only the COST_FIELDS subset, with None for absent keys.
    """
    oc_path = _bot_home(bot_id) / ".openclaw" / "openclaw.json"
    data = _read_oc_json(oc_path, bot_id)
    if data is None:
        return None
    return _extract_cost_settings(data)


def write_openclaw_cost_settings(
    bot_id: str, new_settings: dict, *, force: bool = False,
) -> tuple[bool, str]:
    """Deep-merge new_settings into the bot's openclaw.json cost fields.

    Returns (success, error_message). error_message is empty on success.
    Uses /tmp staging + sudo /bin/cp as root (evolve sudoers grant).

    Runs the preflight gate against the merged (post-write) state before
    touching disk. The gate rejects one specific known-wasteful combo:
    `heartbeat.lightContext = false` AND `heartbeat.every >= 1h`. That
    combination has no valid use case (Anthropic prompt cache TTL is
    5 min default / 1h max, so heartbeats at 1h+ never get a cache hit
    and pay full input price every tick). `force=True` skips the gate;
    callers passing force=True should emit a `cost_setting_forced`
    Signal so the override leaves a paper trail.

    See docs/principle-apps-minimize-bootstrap-cost.md.
    """
    import tempfile as _tmp, os as _os
    oc_path = _bot_home(bot_id) / ".openclaw" / "openclaw.json"
    data = _read_oc_json(oc_path, bot_id)
    if data is None:
        return False, f"Could not read openclaw.json for {bot_id}"

    merged = _deep_merge_cost(data, new_settings)

    if not force:
        gate_err = preflight_heartbeat_combination(merged)
        if gate_err:
            return False, gate_err

    serialized = json.dumps(merged, indent=2)

    fd, tmp = _tmp.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-", suffix=".json")
    try:
        with _os.fdopen(fd, "w") as f:
            f.write(serialized)
        result = subprocess.run(
            ["sudo", "/bin/cp", tmp, str(oc_path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or f"cp returned {result.returncode}"
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        try:
            _os.unlink(tmp)
        except OSError:
            pass


def apply_profile_to_bot(profile_name: str, bot_id: str, shared_dir: Path) -> dict:
    """Apply a named profile to a single bot. Returns result dict.

    Profiles can contain a mix of openclaw.json fields (heartbeat,
    contextPruning, compaction, …) and BE-config-owned fields (currently
    just ``cache_retention``). This function routes each field to the
    right storage: openclaw.json via ``write_openclaw_cost_settings``,
    BE config via ``better_engine_config.set_per_bot_cache_retention``.
    """
    profile = get_profile(profile_name, shared_dir)
    if not profile:
        return {"bot_id": bot_id, "ok": False, "error": f"Profile '{profile_name}' not found"}

    # Split the profile's settings into openclaw.json vs BE config.
    full_settings = profile["settings"]
    oc_settings = {k: v for k, v in full_settings.items() if k not in BE_CONFIG_PROFILE_FIELDS}
    be_settings = {k: v for k, v in full_settings.items() if k in BE_CONFIG_PROFILE_FIELDS}

    # The unrestricted-debug profile (and its "performance" legacy alias) IS
    # the wasteful combo by design. The operator selected it from the profile
    # list with the WARNING label, so this counts as informed consent — pass
    # force=True past the preflight, and emit the unrestricted-applied Signal
    # so the override leaves a paper trail.
    canonical_name = profile.get("name") or profile_name
    is_unrestricted = canonical_name == "unrestricted-debug"

    before = read_openclaw_cost_settings(bot_id)
    ok, err = write_openclaw_cost_settings(
        bot_id, oc_settings, force=is_unrestricted,
    )
    after = read_openclaw_cost_settings(bot_id) if ok else None

    if ok and is_unrestricted:
        emit_unrestricted_profile_applied_signal(bot_id, shared_dir)

    # BE-config fields are best-effort: openclaw.json is the canonical write.
    # If the openclaw write succeeded, also route any BE-config fields so the
    # profile actually means what it says. A BE-config failure does NOT mark
    # the whole apply as failed — the user can re-apply or fix via the matrix.
    be_err = None
    if ok and be_settings:
        try:
            from better_engine_config import load as _be_load, save as _be_save  # type: ignore
            be = _be_load(shared_dir)
            if "cache_retention" in be_settings:
                be.set_per_bot_cache_retention(bot_id, be_settings["cache_retention"])
            _be_save(be, shared_dir)
        except Exception as exc:  # noqa: BLE001
            be_err = str(exc)

    if ok and after:
        save_cost_snapshot(bot_id, after, shared_dir)

    return {
        "bot_id": bot_id,
        "ok": ok,
        "error": err if not ok else (f"BE config write failed: {be_err}" if be_err else None),
        "profile": profile_name,
        "before": before,
        "after": after,
        "fields_changed": _count_changed(before or {}, oc_settings),
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _read_oc_json(oc_path: Path, bot_id: str) -> dict | None:
    try:
        return json.loads(oc_path.read_text())
    except PermissionError:
        pass
    except (json.JSONDecodeError, OSError):
        return None
    # Fallback: sudo /bin/cat as root (evolve sudoers grant)
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(oc_path)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def _extract_cost_settings(data: dict) -> dict:
    # Most cost fields live under agents.defaults; `session` lives at root.
    # For each field, prefer its canonical location, then fall back to the
    # other for configs written before the schema migration.
    defaults = data.get("agents", {}).get("defaults", {})
    out: dict = {}
    for field in COST_FIELDS_DEFAULTS:
        out[field] = defaults[field] if field in defaults else data.get(field)
    for field in COST_FIELDS_ROOT:
        out[field] = data[field] if field in data else defaults.get(field)
    return out


def _deep_merge_cost(base: dict, updates: dict) -> dict:
    """Merge cost settings into the canonical location for each field.

    Most fields live under agents.defaults; `session` lives at the root —
    openclaw's schema rejects agents.defaults.session, and doctor --fix
    responds by rolling back to .bak, wiping every change in the same save.
    Stale copies at the wrong location are stripped on every write so old
    configs self-heal.
    """
    import copy
    result = copy.deepcopy(base)

    result.setdefault("agents", {}).setdefault("defaults", {})
    defaults = result["agents"]["defaults"]

    def _apply(target: dict, field: str, val):
        if val is None:
            target.pop(field, None)
        elif isinstance(val, dict) and isinstance(target.get(field), dict):
            target[field] = _deep_merge_dict(target[field], val)
        else:
            target[field] = val

    for field in COST_FIELDS_DEFAULTS:
        result.pop(field, None)  # strip stale root-level copy
        if field in updates:
            _apply(defaults, field, updates[field])

    for field in COST_FIELDS_ROOT:
        defaults.pop(field, None)  # strip stale agents.defaults copy
        if field in updates:
            _apply(result, field, updates[field])

    return result


def _deep_merge_dict(base: dict, updates: dict) -> dict:
    import copy
    result = copy.deepcopy(base)
    for k, v in updates.items():
        if v is None:
            result.pop(k, None)
        elif isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge_dict(result[k], v)
        else:
            result[k] = v
    return result


def _count_changed(before: dict, updates: dict) -> int:
    count = 0
    for field in COST_FIELDS:
        if field in updates and updates.get(field) != before.get(field):
            count += 1
    return count
