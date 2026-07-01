"""
oc_model.py — Evolve-native bot model config reader/writer.

Ported from docs/reference/openclaw-admin.py into the Evolve codebase so
model configuration is managed by Evolve, not an external admin utility.

Designed to run AS the bot user (via ``sudo -u {user} python3 oc_model.py``)
so it can read/write the bot's openclaw.json without extra permissions beyond
the standard NOPASSWD rule for ``sudo -u {user}``.

Usage as standalone script:
    sudo -u team_bot_a python3 oc_model.py models team_bot_a
    sudo -u team_bot_a python3 oc_model.py models set team_bot_a "anthropic/claude-sonnet-4-6 anthropic/claude-haiku-4-5"
    sudo -u team_bot_a python3 oc_model.py config team_bot_a
    sudo -u team_bot_a python3 oc_model.py config set team_bot_a '{"tiers":{"tier2":{"models":["anthropic/claude-sonnet-4-6"]}}}'

Schema notes
------------
OpenClaw's agents.defaults.model schema only accepts two fields:
  - primary   (str)   -- the active model
  - fallbacks (list)  -- ordered fallback chain

The "tiers" field MUST NOT be written into agents.defaults.model -- OC's
schema validator rejects it and breaks the bot.

Storage layout:
  openclaw.json  agents.defaults.model  -> only "primary" and "fallbacks"
  openclaw.json  agents.defaults.models -> OC model catalog dict
  ~/.openclaw/evolve-tiers.json          -> Evolve tier definitions (this file, written as bot user)

tiers.json schema:
    {
      "tiers": {
        "tier1": { "models": ["anthropic/claude-opus-4-6"] },
        "tier2": { "models": ["anthropic/claude-sonnet-4-6", "openai/gpt-4o"] },
        "tier3": { "models": ["anthropic/claude-haiku-4-5", "openai/gpt-4o-mini"] },
        "tier0": { "models": ["openai/gpt-4o"] }
      },
      "routing": {
        "enabled": true,
        "maintenanceTier": "tier3",
        "backgroundTier": "tier3",
        "ambiguousTier": null,
        "confidenceThreshold": 0.65
      },
      "fallbackMode": "static",
      "tierCascade": ["tier2", "tier3", "tier1"]
    }
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Default path when the script is run AS the bot user (home = bot's home dir)
_DEFAULT_OC_JSON: Path = Path.home() / ".openclaw" / "openclaw.json"

# Evolve-owned tier config is stored in the bot user's own ~/.openclaw dir.
# oc_model.py always runs as the bot user (via "sudo -u {bot} python3 oc_model.py"),
# so Path.home() resolves to /Users/{bot} and write access is guaranteed.
_TIERS_FILENAME = "evolve-tiers.json"

# Cascade order used when evolve-tiers.json has no explicit tierCascade.
# The first entry decides the bot's `primary` model after the writer
# recomputes the flat fallback list.
#
# All bots use the workhorse-first default. Trigger-aware tier routing
# (PR #1737 / #1764) sends background work (heartbeat / cron / subagent /
# summarizer / classifier / task_extractor / fallback) to tier3 via the
# pre-classification anchor + `routing.backgroundTier` / `maintenanceTier`,
# regardless of what `primary` is set to. So `primary = tier2` (Sonnet)
# is correct for ALL bots — it's the default for *user-facing turns*,
# which IS the right destination for human chat on every bot type.
#
# Historical note (PR #1765 + this revert):
#   PR #1765 made the default cascade role-aware — member bots got a
#   floor-first cascade ([tier3, tier2, tier1]) so primary derived to
#   the tier3 floor. The reasoning was "member bots' dominant work is
#   background → save cost by defaulting them to Haiku." That was
#   wrong on two counts:
#     1. Background work was ALREADY routing to tier3 via the trigger
#        anchor, independent of primary. The role-aware default
#        achieved no additional cost reduction on background turns.
#     2. It silently degraded human-facing chat on member bots:
#        Slack/Telegram/Discord users got tier3 (Haiku) replies with
#        no in-channel way to escalate (chip surface is admin-only).
#   This revert restores the workhorse-first default for all bots.
#   The per-bot default-tier picker (auto/fast/standard/power) is the
#   correct path for operator/user-driven defaults — see follow-up.
DEFAULT_TIER_CASCADE = ["tier2", "tier3", "tier1"]


def default_tier_cascade_for_role(role: str | None) -> list[str]:
    """Return the default tierCascade for a bot of the given role.

    Currently returns the workhorse-first default ``["tier2", "tier3",
    "tier1"]`` for ALL roles. The signature accepts ``role`` so the
    plumbing through oc_cli + json_full_config_set stays in place for
    the planned per-bot default-tier picker (auto/fast/standard/power),
    which will replace this function's role-dispatch with a config-
    driven lookup.

    Don't reintroduce role-based dispatch here without addressing the
    Slack/Telegram-can't-escalate problem first — see the comment block
    on DEFAULT_TIER_CASCADE for the PR #1765 history.
    """
    # role is currently ignored — see docstring. Kept on signature
    # so callers (oc_cli, deploy, provisioning) compile unchanged.
    del role
    return DEFAULT_TIER_CASCADE[:]


DEFAULT_ROUTING = {
    "enabled": True,
    "maintenanceTier": "tier3",
    "backgroundTier": "tier3",
    "ambiguousTier": None,
    "confidenceThreshold": 0.65,
}

# Fields that Evolve manages but that OC's schema does NOT accept under
# agents.defaults.model. Strip these before writing openclaw.json.
_OC_FORBIDDEN_MODEL_FIELDS = {"tiers", "routing", "fallbackMode", "tierCascade"}


# -- JSON helpers --------------------------------------------------------------

def _load_json(path: Path) -> dict:
    """Load a JSON file, stripping trailing commas for lenient parsing.

    Returns {} if the file does not exist (new bots with no openclaw.json yet).
    """
    try:
        content = path.read_text()
    except FileNotFoundError:
        return {}
    content = re.sub(r',(\s*[}\]])', r'\1', content)
    return json.loads(content)


def _preserve_write(data: dict, path: Path) -> None:
    """Write JSON atomically, preserving existing ownership and permissions.

    Safety protocol — MUST be maintained for any openclaw.json write path:
    1. Serialize to a temp file in the same directory (never touch live file yet)
    2. Validate the temp file via ``openclaw config validate --json``
       — an invalid key crashes the gateway into a crash-loop on next restart
    3. If invalid: raise ValueError, clean up temp, live file untouched
    4. Backup the existing live file to <name>.bak before replacing
    5. Atomic rename (os.replace) into place

    Raises:
        ValueError: if openclaw schema validation rejects the new config.
    """
    import tempfile
    try:
        st = os.stat(path)
        existing_uid: int | None = st.st_uid
        existing_gid: int | None = st.st_gid
        existing_mode: int | None = st.st_mode
    except OSError:
        existing_uid = existing_gid = existing_mode = None

    # Write to a temp file in the same directory then rename (atomic)
    dir_ = path.parent
    dir_.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=dir_, suffix=".json.tmp", delete=False
    ) as f:
        json.dump(data, f, indent=2)
        tmp = Path(f.name)

    try:
        # Validate against OC schema before touching the live file.
        # An unknown key in openclaw.json causes a gateway crash-loop.
        # polling-bypass: one-shot config validation (pre-write check, not polling)
        # cwd: openclaw is a Node binary; Node calls uv_cwd() at startup, which
        # EACCESes if the inherited CWD is untraversable (e.g. ssh lands in the
        # admin's home). dir_ is traversable — we just wrote the temp file there.
        val = subprocess.run(
            ["openclaw", "config", "validate", "--json"],
            env={**os.environ, "OPENCLAW_CONFIG_PATH": str(tmp)},
            capture_output=True, text=True, timeout=15, cwd=str(dir_),
        )
        try:
            vresult = json.loads(val.stdout)
            if not vresult.get("valid", False):
                issues = vresult.get("issues", [])
                issue_str = "; ".join(
                    f"{i.get('path')}: {i.get('message')}" for i in issues
                )
                raise ValueError(f"openclaw.json validation failed: {issue_str}")
        except json.JSONDecodeError:
            combined = val.stdout + val.stderr
            if "invalid" in combined.lower() or val.returncode != 0:
                raise ValueError(
                    f"openclaw.json validation failed (returncode={val.returncode}): {combined[:200]}"
                )

        # Backup the live file before replacing it — enables manual recovery
        # if a delayed-effect bug slips through validation.
        if path.exists():
            shutil.copy2(path, path.with_suffix(".json.bak"))

        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    if existing_uid is not None:
        try:
            os.chown(path, existing_uid, existing_gid)
            os.chmod(path, existing_mode)
        except OSError as e:
            print(
                f"[oc_model] warning: could not restore permissions on {path}: {e}",
                file=sys.stderr,
            )


# -- Evolve tiers file helpers ------------------------------------------------

def _tiers_path(bot: str) -> Path:
    """Return path to Evolve's tiers file for the current (bot) user.

    This script always runs as the bot user via ``sudo -u {bot} python3 oc_model.py``,
    so Path.home() resolves to /Users/{bot} — a directory the bot user owns and
    can always write to without chmod gymnastics.
    """
    return Path.home() / ".openclaw" / _TIERS_FILENAME


def _load_tiers_file(bot: str) -> dict:
    """Load ~/.openclaw/evolve-tiers.json for the current bot user."""
    return _load_json(_tiers_path(bot))


def _save_tiers_file(bot: str, tiers_data: dict) -> None:
    """Write tiers_data to ~/.openclaw/evolve-tiers.json for the bot.

    Ownership-robust write (CLAUDE.md File Access Pattern). The naive
    ``path.open("w")`` truncates the live file IN PLACE, which needs write
    permission on the *file itself* — so it raised ``[Errno 13] Permission
    denied`` whenever the live evolve-tiers.json was owned by a different
    uid than the running process (e.g. root-owned after a migration left it
    root:wheel, with oc_model.py running as the bot user; or bot-owned with
    oc_model.py running as the evolve admin user). Both cases are real.

    Two-stage strategy, identical in spirit to
    ``migrate_model_roles._write_bot_owned_json``:

      1. Atomic temp-in-dir + ``os.replace``. ``os.replace`` only needs
         write on the *parent directory* (which the bot owns), so this
         succeeds when running as the bot user regardless of who owns the
         live file — no more dependence on ownership luck.
      2. On ``PermissionError`` (running as evolve, which cannot write the
         bot's ``.openclaw/`` directly) fall back to ``/tmp`` staging +
         ``sudo /bin/cp`` — the only sanctioned way for the evolve service
         user to land a bot-owned file. Never ``sudo -u <bot>`` (no grant).
    """
    path = _tiers_path(bot)
    content = json.dumps(tiers_data, indent=2)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content)
        os.replace(tmp, path)
        return
    except PermissionError:
        # Clean up the in-dir temp if we managed to create it before the
        # replace failed, then fall through to the sudo path.
        try:
            tmp.unlink()  # type: ignore[possibly-undefined]
        except OSError as _e:
            print(f"[oc_model] temp cleanup skipped: {_e}", file=sys.stderr)
    import tempfile
    fd, tmp_path = tempfile.mkstemp(dir="/tmp", prefix="evolve-tiers-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        r = subprocess.run(
            ["sudo", "/bin/cp", tmp_path, str(path)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise PermissionError(
                f"sudo /bin/cp failed for {path}: {r.stderr.strip()}"
            )
        # A bare `cp` (no -p) to a *fresh* dest lands it root:wheel 0600 —
        # unreadable by the bot user that runs this module to read its OWN
        # tier config, so every subsequent read 500s with [Errno 13] on
        # evolve-tiers.json. Chown it back to the bot + chmod 644 so the bot
        # keeps access. Idempotent when the dest already existed bot-owned.
        # Sudoers grants: setup_wizard §4b. The deploy-time self-heal
        # (ensure_pod_perms → check_bot_tiers_ownership) converges any miss.
        _restore_tiers_bot_ownership(path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError as _e:
            print(f"[oc_model] /tmp cleanup skipped: {_e}", file=sys.stderr)


def _restore_tiers_bot_ownership(path: Path) -> None:
    """``sudo chown <bot>:staff`` + ``sudo /bin/chmod 644`` a tiers file landed
    by ``sudo /bin/cp`` so the bot can read its own config.

    The bot user is derived from the path (``/Users/<user>/.openclaw/...``), so
    this is correct regardless of which user the process runs as. Best-effort:
    the cp already succeeded; a chown failure only matters when the dest was
    freshly created (then it's root-owned) and the deploy-time self-heal
    (``ensure_pod_perms`` → ``check_bot_tiers_ownership``) converges it. No-op
    for non-``/Users`` paths (tests / Linux homes never reach the sudo branch).
    """
    parts = path.parts
    if len(parts) < 3 or parts[1] != "Users":
        return
    bot_user = parts[2]
    subprocess.run(
        ["sudo", "/usr/sbin/chown", f"{bot_user}:staff", str(path)],
        capture_output=True, text=True,
    )
    subprocess.run(
        ["sudo", "/bin/chmod", "644", str(path)],
        capture_output=True, text=True,
    )


# ── Rungs/roles shape bridge (spec-model-rungs-and-roles-2026-06-09) ──────────
#
# The 2026-06-09 migration moved evolve-tiers.json from the legacy
# ``{tiers: {tierN: {models}}}`` shape to ``{rungs: [...], roles: {...}}``.
# The gateway loader (ModelRouter.ts) ignores a legacy ``tiers`` key whenever
# ``rungs`` is present. The read/write helpers below let this module's
# legacy-shaped internals (get_tiers, generate_fallback_list, the cascade
# derivation, the config-set writer) keep working against the new shape:
#
#   - reads  synthesize a legacy ``tiers`` dict FROM the new shape, so the
#     UI/API render the migrated allocations instead of an empty dict; and
#   - writes fold any operator-supplied legacy ``tiers`` content INTO the new
#     shape's rung clusters and DROP the stale ``tiers`` key, so a freshness-
#     advisory "apply" actually reaches routing (the loader reads rungs).
#
# Canonical mapping mirrors TIER_TO_ROLE / TIER_TO_RUNG in
# migrate_model_roles.py and primary_bot.py — keep all three in sync.

_TIER_TO_ROLE: dict[str, str] = {
    "tier3": "fast",
    "tier2": "standard",
    "tier1": "power",
    "tier0": "judge",
}
_ROLE_TO_TIER: dict[str, str] = {v: k for k, v in _TIER_TO_ROLE.items()}
_TIER_TO_RUNG: dict[str, str] = {
    "tier3": "haiku-class",
    "tier2": "sonnet-class",
    "tier1": "opus-class",
    "tier0": "sonnet-class",
    # ``max`` has no legacy tierN slot (it is the new frontier rung). The
    # per-bot Tier Definitions panel edits it under the role key ``max``;
    # the write path folds it into the ``fable-class`` rung. (spec §Addendum3.C.6)
    "max": "fable-class",
}
_TIER_COST_CLASS: dict[str, str] = {
    "tier3": "low",
    "tier2": "medium",
    "tier1": "high",
    "tier0": "medium",
    "max": "premium",
}
# Cost rank (cheapest first) for placing a freshly-created rung at the right
# array position.
_RUNG_COST_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "premium": 3}


def _file_is_new_shape(tiers_file: dict) -> bool:
    """True when the file carries a non-empty ``rungs`` array (new shape)."""
    rungs = tiers_file.get("rungs")
    return isinstance(rungs, list) and bool(rungs)


def _rung_for_role(tiers_file: dict, role: str) -> str | None:
    """Resolve a role ID to its rung slug via the file's ``roles`` map.

    ``judge`` may be a ``{rung, provider}`` object; both forms resolve to the
    rung id. Returns None when the role is absent (caller may create it).
    """
    roles = tiers_file.get("roles")
    if isinstance(roles, dict):
        entry = roles.get(role)
        if isinstance(entry, str) and entry:
            return entry
        if isinstance(entry, dict):
            rung = entry.get("rung")
            if isinstance(rung, str) and rung:
                return rung
    return None


def _rung_models_list(tiers_file: dict, rung_id: str) -> list[str]:
    """Return the models[] of ``rung_id``, or [] when the rung is absent."""
    for rung in (tiers_file.get("rungs") or []):
        if isinstance(rung, dict) and rung.get("id") == rung_id:
            models = rung.get("models")
            if isinstance(models, list):
                return [m for m in models if isinstance(m, str) and m]
    return []


def synthesize_legacy_tiers(tiers_file: dict) -> dict:
    """Build a legacy ``{tierN: {models}}`` dict from a new/mixed-shape file.

    For each tier key, resolve its role → rung → models and emit
    ``{tierN: {"models": [...]}}``. Used by the read path so legacy-shaped
    consumers (get_tiers, generate_fallback_list) render the migrated
    allocations instead of an empty dict. A legacy-only file (no rungs) is
    returned via its own ``tiers`` dict unchanged.
    """
    if not isinstance(tiers_file, dict):
        return {}
    if not _file_is_new_shape(tiers_file):
        legacy = tiers_file.get("tiers")
        return legacy if isinstance(legacy, dict) else {}
    out: dict[str, dict] = {}
    for tier_key, role in _TIER_TO_ROLE.items():
        rung_id = _rung_for_role(tiers_file, role) or _TIER_TO_RUNG.get(tier_key)
        if not rung_id:
            continue
        models = _rung_models_list(tiers_file, rung_id)
        if models:
            out[tier_key] = {"models": models}
    # ``max`` has no legacy tierN key, so the loop above skips it. Surface its
    # models under the role key ``max`` so the per-bot panel can render/edit a
    # Max row (spec §Addendum3.C.6). Only when the bot's own file defines the
    # role — a code-default Max rung is shown by the read-only resolution view,
    # not duplicated into the editable per-bot doc.
    max_rung = _rung_for_role(tiers_file, "max")
    if max_rung:
        max_models = _rung_models_list(tiers_file, max_rung)
        if max_models:
            out["max"] = {"models": max_models}
    return out


def _ensure_role_and_rung(tiers_file: dict, role: str, tier_key: str) -> str:
    """Ensure ``role`` maps to a rung and that rung exists; return its slug.

    Creates the role entry (and the rung row, at its cost-ordered position)
    following the spec's slug conventions when absent. ``judge`` is created as
    the structured ``{rung, provider:"not-standard"}`` form.
    """
    rung_id = _rung_for_role(tiers_file, role)
    if rung_id is None:
        rung_id = _TIER_TO_RUNG[tier_key]
        roles = tiers_file.setdefault("roles", {})
        if not isinstance(roles, dict):
            roles = {}
            tiers_file["roles"] = roles
        if role == "judge":
            roles[role] = {"rung": rung_id, "provider": "not-standard"}
        else:
            roles[role] = rung_id
    rungs = tiers_file.setdefault("rungs", [])
    if not isinstance(rungs, list):
        rungs = []
        tiers_file["rungs"] = rungs
    if not any(isinstance(r, dict) and r.get("id") == rung_id for r in rungs):
        cost = _TIER_COST_CLASS.get(tier_key, "medium")
        new_rung = {"id": rung_id, "models": [], "costClass": cost}
        # Insert at the cost-ordered position (cheapest first).
        rank = _RUNG_COST_RANK.get(cost, 1)
        idx = len(rungs)
        for i, r in enumerate(rungs):
            if not isinstance(r, dict):
                continue
            if _RUNG_COST_RANK.get(r.get("costClass"), 1) > rank:
                idx = i
                break
        rungs.insert(idx, new_rung)
    return rung_id


def _set_rung_models(tiers_file: dict, rung_id: str, models: list[str]) -> None:
    """Replace the models[] of ``rung_id`` in a new-shape file."""
    for rung in tiers_file.get("rungs", []):
        if isinstance(rung, dict) and rung.get("id") == rung_id:
            rung["models"] = list(models)
            return


def apply_tiers_update_new_shape(tiers_file: dict, updates_tiers: dict) -> None:
    """Fold a legacy ``{tierN: {models}}`` update into the new shape in place.

    For each tier in ``updates_tiers``, resolve its role → rung (creating the
    role/rung if absent) and set that rung's models[] to the update's models.
    Leaves rungs not named by the update untouched.
    """
    if not isinstance(updates_tiers, dict):
        return
    for tier_key, cfg in updates_tiers.items():
        # Accept both legacy ``tierN`` keys and direct role keys. ``max`` is
        # the only role without a legacy tierN slot — it arrives as the role
        # key ``max`` from the per-bot panel and folds into ``fable-class``
        # (spec §Addendum3.C.6). _ensure_role_and_rung keys its rung/cost
        # lookup off the same string, which _TIER_TO_RUNG/_TIER_COST_CLASS
        # now carry an entry for.
        role = _TIER_TO_ROLE.get(tier_key) or (tier_key if tier_key == "max" else None)
        if role is None or not isinstance(cfg, dict):
            continue
        models = cfg.get("models")
        if not isinstance(models, list):
            continue
        clean = [m for m in models if isinstance(m, str) and m]
        rung_id = _ensure_role_and_rung(tiers_file, role, tier_key)
        _set_rung_models(tiers_file, rung_id, clean)


# Sentinel prefix on the structured error a rung-collision raise emits, so the
# admin endpoint can tell a "two roles fight over one rung" conflict (operator
# can fix it — return 409) apart from a genuine write failure (return 500). The
# human-readable cause follows the prefix and is shown to the operator verbatim.
RUNG_COLLISION_PREFIX = "RUNG_COLLISION: "


def detect_rung_collisions(tiers_file: dict, updates_tiers: dict) -> list[dict]:
    """Find incoming legacy tiers that fold to the SAME rung with DIFFERENT models.

    Under the new (rungs/roles) shape several legacy tier keys can map to one
    rung — e.g. ``standard``(tier2) and ``judge``(tier0) both default to
    ``sonnet-class``. :func:`apply_tiers_update_new_shape` folds each legacy
    tier into its rung last-writer-wins, so two incoming tiers that share a rung
    but carry different model lists silently lose one edit: the write reports
    success while the operator's change never lands (model-tiers false-success,
    2026-06-27). Callers use this to REJECT such a write instead of persisting a
    coin-flip — the per-bot Tier Definitions editor's "block conflicting saves"
    contract.

    Returns one entry per colliding rung, each
    ``{"rung": rung_id, "roles": {role: [models], ...}}``. Empty list when
    there is no collision — including a legacy-only file (no rungs to share),
    a single incoming tier, or sibling tiers whose model SETS already match
    (the common case: the editor loads both rows from the same rung, and a
    catalog re-save resends identical synthesized lists — neither is a real
    conflict).
    """
    if not isinstance(updates_tiers, dict) or not _file_is_new_shape(tiers_file):
        return []
    # rung_id -> {role: cleaned-models}. Resolve each incoming tier to the rung
    # the fold WOULD target, mirroring _ensure_role_and_rung's lookup order
    # (the bot's own roles map first, then the code-default tier→rung).
    by_rung: dict[str, dict[str, list[str]]] = {}
    for tier_key, cfg in updates_tiers.items():
        role = _TIER_TO_ROLE.get(tier_key) or (tier_key if tier_key == "max" else None)
        if role is None or not isinstance(cfg, dict):
            continue
        models = cfg.get("models")
        if not isinstance(models, list):
            continue
        clean = [m for m in models if isinstance(m, str) and m]
        rung_id = _rung_for_role(tiers_file, role) or _TIER_TO_RUNG.get(tier_key)
        if not rung_id:
            continue
        by_rung.setdefault(rung_id, {})[role] = clean
    collisions: list[dict] = []
    for rung_id, roles in by_rung.items():
        if len(roles) < 2:
            continue
        # A collision only when the model SETS differ (order/dupes are not a
        # conflict — the fold would pick one ordering, but the intent matches).
        signatures = {frozenset(models) for models in roles.values()}
        if len(signatures) > 1:
            collisions.append({"rung": rung_id, "roles": roles})
    return collisions


def format_rung_collision_error(collisions: list[dict]) -> str:
    """Render :func:`detect_rung_collisions` output as an operator-facing line.

    Names the shared rung and the roles that disagree, with each role's intended
    models, plus the two ways out (make them match, or split a role onto its own
    rung). The string is shown verbatim in the admin UI, so it stays plain.
    """
    parts: list[str] = []
    for c in collisions:
        roles = c["roles"]
        role_bits = ", ".join(
            f"{role}={models or '(empty)'}" for role, models in sorted(roles.items())
        )
        parts.append(
            f"{' and '.join(sorted(roles))} both use the {c['rung']} rung, so they "
            f"must list the same models — but you set {role_bits}"
        )
    return (
        "; ".join(parts)
        + ". Make the shared roles match, or move one onto its own rung."
    )


def normalize_tiers_file_shape(tiers_file: dict) -> dict:
    """Fold a mixed-shape file into the new shape and drop the stale ``tiers``.

    When BOTH ``rungs`` and ``tiers`` keys are present (the pollution a
    freshness-advisory "apply" wrote against the old code), fold the legacy
    ``tiers`` content into the rung clusters and remove the ``tiers`` key, so
    the gateway loader — which ignores ``tiers`` when ``rungs`` is present —
    sees the operator's intended change. Idempotent: a clean new-shape file
    (rungs only) or a legacy-only file (tiers only) is returned unchanged.
    """
    if not isinstance(tiers_file, dict):
        return tiers_file
    if _file_is_new_shape(tiers_file) and isinstance(tiers_file.get("tiers"), dict):
        apply_tiers_update_new_shape(tiers_file, tiers_file["tiers"])
        tiers_file.pop("tiers", None)
    return tiers_file


# -- Config accessors ---------------------------------------------------------

def get_model_config(data: dict) -> dict:
    """Return agents.defaults.model dict (mutable reference)."""
    return data.get("agents", {}).get("defaults", {}).get("model", {})


def set_model_config(data: dict, mc: dict) -> None:
    """Write mc into agents.defaults.model in-place.

    Strips any Evolve-owned fields that OC's schema rejects before writing,
    so it is always safe to call this even if mc came from get_tiers/get_routing.
    """
    safe_mc = {k: v for k, v in mc.items() if k not in _OC_FORBIDDEN_MODEL_FIELDS}
    data.setdefault("agents", {}).setdefault("defaults", {})["model"] = safe_mc


def get_memory_search_config(data: dict) -> dict:
    """Return agents.defaults.memorySearch dict (mutable reference)."""
    return data.get("agents", {}).get("defaults", {}).get("memorySearch", {})


def set_memory_search_config(data: dict, ms: dict) -> None:
    """Write memorySearch into agents.defaults in-place.

    Empty dict deletes the field — used to revert to OpenClaw's auto-detection.
    """
    if ms:
        data.setdefault("agents", {}).setdefault("defaults", {})["memorySearch"] = ms
    else:
        defaults = data.setdefault("agents", {}).setdefault("defaults", {})
        defaults.pop("memorySearch", None)


def get_catalog(data: dict) -> list[str]:
    """Return ordered model IDs from agents.defaults.models catalog dict."""
    models_dict = data.get("agents", {}).get("defaults", {}).get("models", {})
    if isinstance(models_dict, dict):
        return list(models_dict.keys())
    return []


def set_catalog(data: dict, models: list) -> None:
    """Write catalog as agents.defaults.models dict (preserves existing per-model metadata).

    Accepts EITHER shape — the canonical form is list[str] of model IDs,
    but callers historically pass list[dict] (each {"id": "...", "provider": "..."})
    because that's the richer representation they carry internally. We
    normalize both into the OC storage form ``{<model_id>: {<meta>}}``.

    Before this tolerance was added, passing list[dict] tripped
    ``TypeError: unhashable type: 'dict'`` deep inside oc_model.py,
    which surfaced to operators as the opaque "write failed — check
    server logs" / "oc_full_config_set returned None" message. Two
    real callers (provisioning.seed_model_config_if_empty + the
    /api/admin/config/<bot>/tiers reconcile endpoint) tripped this
    in production. Defending here at the boundary makes any future
    caller immune.

    Non-string, non-dict entries and entries without an extractable
    ID are silently dropped — they cannot be represented in OC's
    storage shape regardless.

    Side effect: also keeps ``models.providers[<provider>].models[]`` in
    sync via :func:`sync_provider_models_from_catalog`. OpenClaw v2026.6.1+
    requires every model referenced in ``agents.defaults.models`` to ALSO
    appear in the provider-registry under ``models.providers``; the
    bundled static catalog is no longer consulted by the registry-gate.
    Centralizing both writes here means every callsite (provisioning
    seed, AI Optimization reconcile, evolve-admin reconcile-catalog CLI,
    deploy gap-fill) stays consistent without per-caller awareness.
    """
    existing = data.get("agents", {}).get("defaults", {}).get("models", {})
    new_catalog: dict = {}
    for m in models:
        if isinstance(m, str):
            model_id = m
        elif isinstance(m, dict):
            model_id = m.get("id") or m.get("model")
        else:
            continue
        if not model_id or model_id in new_catalog:
            continue
        new_catalog[model_id] = existing.get(model_id, {})
    data.setdefault("agents", {}).setdefault("defaults", {})["models"] = new_catalog

    # Keep models.providers in sync — see docstring above.
    sync_provider_models_from_catalog(data)


# Providers that OpenClaw ships with a bundled catalog. For these, a
# minimal ``{"id": ..., "name": ...}`` registration is sufficient — OC's
# default ``models.mode = "merge"`` fills in ``api``/``cost``/``contextWindow``
# from the bundled catalog at runtime. For ANY other provider (e.g.
# ``runway``, ``replicate``, or a self-hosted endpoint) OC's schema
# requires a full custom-provider declaration with ``baseUrl``, ``api``,
# etc — which we cannot synthesize from the catalog key alone. Such
# providers are skipped here and surface their own errors if the operator
# hasn't configured them manually.
#
# Enumerated from OC v2026.6.1 dist files; update when OC adds new
# bundled providers (the safe-upgrade ``gate_bot_config_schema`` should
# surface this drift when it lands).
_OC_BUNDLED_PROVIDERS: frozenset[str] = frozenset({
    "anthropic", "openai", "google", "google-gemini-cli", "google-vertex",
    "xai", "openrouter", "nvidia", "huggingface", "together", "ollama",
    "amazon-bedrock", "github-copilot", "vercel-ai-gateway",
    "openai-codex", "azure-openai", "moonshot", "deepseek", "fireworks",
    "groq", "mistral",
})


def sync_provider_models_from_catalog(data: dict) -> None:
    """Ensure ``models.providers[<provider>].models[]`` has an entry for every
    model referenced in ``agents.defaults.models`` whose provider OC bundles.

    Runtime context (OpenClaw v2026.6.1+)
    -------------------------------------
    OC's runtime registry-gate (``buildMissingProviderModelRegistrationHint``)
    requires every model resolved via ``agents.defaults.models`` to ALSO be
    registered under ``models.providers[<provider>].models[]``. The bundled
    static catalog (``claude-model-refs-*.js`` etc) is no longer consulted
    by this gate, so a bot with a catalog-keyed reference like
    ``agents.defaults.models["anthropic/claude-haiku-4-5"]`` but an empty
    ``models.providers`` block hits ``FailoverError: Unknown model: ...``
    on every routing decision that targets that model. See OC issues #88517
    and the openclaw schema fragment in
    ``docs/schemas/oc-config-schema.txt`` (search for ``"providers"``).

    Shape requirement
    -----------------
    The OC schema for ``models.providers[<provider>].models[]`` items
    requires ``{"id": <str minLength 1>, "name": <str minLength 1>}`` — a
    bare ``{"id": ...}`` is rejected at startup with
    ``models.providers.<P>.models.<N>.name: Invalid input`` (the OC
    runtime hint text only mentions ``id``; the schema requires more).
    We synthesize ``name`` from ``id`` when no richer name exists. OC's
    default ``models.mode = "merge"`` overlays our minimal entries on
    top of OC's bundled catalog, so ``api``/``cost``/``contextWindow``
    are filled in by OC at runtime — we don't have to carry them.

    Custom providers
    ----------------
    For providers OC does NOT bundle (see ``_OC_BUNDLED_PROVIDERS``), OC's
    schema requires the operator to declare ``baseUrl`` and the full
    provider transport config — values we have no way to synthesize from a
    catalog key. Those providers are skipped here. Any pre-existing
    operator-written registration under such a provider is preserved
    untouched.

    Idempotent
    ----------
    Only adds missing entries. Preserves existing entries (including any
    richer metadata an operator set manually). Never removes anything.
    Malformed catalog keys (no ``/`` separator, empty provider, etc) are
    silently skipped — they cannot be represented in the provider-registry
    shape regardless and surface as their own errors elsewhere.

    Repair pass
    -----------
    Before the add-missing pass, walks every pre-existing
    ``models.providers[<provider>].models[]`` entry across ALL providers
    and backfills ``name`` from ``id`` when missing or schema-invalid
    (non-string / empty string). Without this, an entry left behind by a
    deleted reconciler — or stripped by an OC normalizer in a past
    version — survives untouched (the add-missing pass uses
    ``entry.get("id") == model_id`` as its dedupe key, so a malformed
    ``{"id": ...}`` reads as "already registered" and the schema-fatal
    state persists across deploys). The repair is the single source of
    healing for the
    ``models.providers.<P>.models.<N>.name: Invalid input`` startup
    failure that crash-loops gateways.
    """
    catalog = (
        data.get("agents", {}).get("defaults", {}).get("models") or {}
    )
    if not isinstance(catalog, dict) or not catalog:
        return

    data.setdefault("models", {}).setdefault("providers", {})
    providers = data["models"]["providers"]
    if not isinstance(providers, dict):
        return  # operator wrote a non-dict here; refuse to clobber

    # Repair pass — heal pre-existing entries before adding new ones.
    # See "Repair pass" in the docstring above.
    for prov_block in providers.values():
        if not isinstance(prov_block, dict):
            continue
        existing_models = prov_block.get("models")
        if not isinstance(existing_models, list):
            continue
        for entry in existing_models:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id")
            if not isinstance(entry_id, str) or not entry_id:
                continue  # no id to mirror — leave the entry for OC to reject
            existing_name = entry.get("name")
            if not isinstance(existing_name, str) or not existing_name:
                entry["name"] = entry_id

    for model_key in catalog:
        if not isinstance(model_key, str) or "/" not in model_key:
            continue
        provider, _, model_id = model_key.partition("/")
        if not provider or not model_id:
            continue
        if provider not in _OC_BUNDLED_PROVIDERS:
            # Custom provider — operator must configure baseUrl / api /
            # auth themselves. Don't synthesize a half-registration that
            # will fail OC's schema validator.
            continue
        prov_block = providers.setdefault(provider, {})
        if not isinstance(prov_block, dict):
            continue  # operator-set scalar under this provider; leave alone
        models_list = prov_block.setdefault("models", [])
        if not isinstance(models_list, list):
            continue
        already_registered = any(
            isinstance(entry, dict) and entry.get("id") == model_id
            for entry in models_list
        )
        if already_registered:
            continue
        # Minimum viable shape per OC schema: {id, name}. name=id is the
        # safest default — OC's merge mode fills in display details from
        # the bundled catalog. Operators who want a prettier display name
        # can edit the entry directly; this function preserves their edit.
        models_list.append({"id": model_id, "name": model_id})


def get_tiers(bot: str) -> dict:
    """Return tier definitions for the current bot user from ~/.openclaw/evolve-tiers.json.

    Speaks both shapes: a new rungs/roles file is projected back to the legacy
    ``{tierN: {models}}`` view via :func:`synthesize_legacy_tiers` so every
    legacy-shaped consumer (the UI render path, ``generate_fallback_list``)
    sees the migrated allocations instead of the empty dict the bare
    ``.get("tiers")`` returned for migrated files (the "erased" UI bug).
    """
    return synthesize_legacy_tiers(_load_tiers_file(bot))


def get_routing(bot: str) -> dict:
    """Return routing config for bot, falling back to defaults."""
    stored = _load_tiers_file(bot).get("routing", {})
    result = dict(DEFAULT_ROUTING)
    result.update(stored)
    return result


def get_fallback_config(bot: str) -> dict:
    """Return fallbackMode and tierCascade for bot."""
    tf = _load_tiers_file(bot)
    return {
        "fallbackMode": tf.get("fallbackMode", "static"),
        "tierCascade": tf.get("tierCascade", DEFAULT_TIER_CASCADE[:]),
    }


# -- Fallback list generation -------------------------------------------------

def generate_fallback_list(tiers: dict, cascade: list[str]) -> list[str]:
    """Compute the flat fallback list from tier definitions and cascade order.

    Concatenates each tier's model list in cascade order, deduplicating while
    preserving first-occurrence order. Tier 0 (Judge) is excluded from the
    cascade -- it is only used explicitly.

    Example:
        tiers = {
            "tier2": {"models": ["sonnet", "gpt-4o"]},
            "tier3": {"models": ["haiku", "gpt-4o-mini"]},
            "tier1": {"models": ["opus"]},
        }
        cascade = ["tier2", "tier3", "tier1"]
        -> ["sonnet", "gpt-4o", "haiku", "gpt-4o-mini", "opus"]
    """
    seen: set[str] = set()
    result: list[str] = []
    for tier_id in cascade:
        if tier_id == "tier0":
            continue  # Judge tier excluded from cascade
        tier = tiers.get(tier_id, {})
        for m in tier.get("models", []):
            if m and m not in seen:
                seen.add(m)
                result.append(m)
    return result


def compute_primary_from_tiers_file(
    tiers_path: Path,
    role: str | None = None,
) -> tuple[str, list[str]] | None:
    """Derive (primary, fallbacks) for openclaw.json from a tiers file.

    Reads ``evolve-tiers.json`` at ``tiers_path`` directly. The caller is
    responsible for ensuring read access — evolve has ACL read on every
    bot's ``.openclaw/`` via ``set_evolve_read_acl`` so this works from
    the admin user context without sudo.

    Returns ``(primary, fallbacks)`` when the file exists and yields a
    non-empty flat fallback list; returns ``None`` when the file is
    missing, malformed, or every tier in the cascade is empty.

    This is the deploy-time materialization path: deploy.py calls this
    on every ``ensure_plugin_config`` pass so the bot's
    ``agents.defaults.model`` stays in sync with the tier definitions
    even when the operator never touches the AI Optimization page after
    a one-time historical seed. The on-UI-save path
    (``json_full_config_set`` above, lines 748-758) covers the live-edit
    case; this covers the historical-drift case.
    """
    try:
        if not tiers_path.is_file():
            return None
        tiers_file = _load_json(tiers_path)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(tiers_file, dict):
        return None
    # Project the new rungs/roles shape back to the legacy tier view so the
    # cascade-driven flat-fallback derivation works against migrated files.
    tiers = synthesize_legacy_tiers(tiers_file)
    if not isinstance(tiers, dict) or not tiers:
        return None
    cascade = tiers_file.get("tierCascade")
    if not isinstance(cascade, list) or not cascade:
        cascade = default_tier_cascade_for_role(role)
    flat = generate_fallback_list(tiers, cascade)
    if not flat:
        return None
    return flat[0], flat[1:]


# -- High-level JSON API ------------------------------------------------------

def json_models(bot: str, oc_json_path: Path | None = None) -> dict:
    """Read primary+fallbacks+catalog. Synthesizes catalog if absent.

    Returns {bot, primary, catalog, fallback_order}.
    """
    path = oc_json_path or _DEFAULT_OC_JSON
    data = _load_json(path)
    mc = get_model_config(data)
    primary = mc.get("primary", "")
    fallbacks = mc.get("fallbacks", [])
    catalog = get_catalog(data)

    # Synthesize catalog from active models when the catalog dict isn't populated
    if not catalog:
        seen: set[str] = set()
        synth: list[str] = []
        for m in ([primary] if primary else []) + fallbacks:
            if m and m not in seen:
                seen.add(m)
                synth.append(m)
        catalog = synth

    return {
        "bot": bot,
        "primary": primary,
        "catalog": catalog,
        "fallback_order": fallbacks,
    }


def json_full_config(bot: str, oc_json_path: Path | None = None) -> dict:
    """Read the complete Evolve model config for a bot.

    Returns:
        {
          bot, primary, fallbacks,
          catalog,           # list of model IDs from agents.defaults.models
          tiers,             # from ~/.openclaw/evolve-tiers.json
          routing,           # from evolve-tiers.json (with defaults merged)
          fallbackMode,      # "static" | "reactive" | "dynamic"
          tierCascade,       # ["tier2", "tier3", "tier1"]
          generatedFallbacks # computed flat list from tiers + cascade
        }
    """
    path = oc_json_path or _DEFAULT_OC_JSON
    data = _load_json(path)
    mc = get_model_config(data)
    primary = mc.get("primary", "")
    fallbacks = mc.get("fallbacks", [])
    catalog = get_catalog(data)
    tiers = get_tiers(bot)
    routing = get_routing(bot)
    fb_cfg = get_fallback_config(bot)

    # Synthesize catalog if absent
    if not catalog:
        seen: set[str] = set()
        synth: list[str] = []
        for m in ([primary] if primary else []) + fallbacks:
            if m and m not in seen:
                seen.add(m)
                synth.append(m)
        catalog = synth

    generated = generate_fallback_list(tiers, fb_cfg["tierCascade"])

    # cascade key — Phase-3 cutover flag. Returns the on-disk value when
    # present, otherwise the safe default (disabled). Lets the UI/API
    # surface the current state alongside everything else without a
    # separate read path.
    cascade = _load_tiers_file(bot).get("cascade") or {}
    if not isinstance(cascade, dict):
        cascade = {}
    cascade.setdefault("enabled", False)

    # userTierOverride — per-bot operator-controlled defaults
    # (audit #69 Phase A + existing PR #1780 fields). Surfaced here
    # so the UI's AI Optimization page can render the picker, daily
    # cap, allow-bot-initiated toggle, and enabled flag from one
    # config endpoint. Shape:
    #   {"enabled": bool, "dailyCap": int,
    #    "allowBotInitiated": bool, "defaultTier": str}
    user_tier_override = _load_tiers_file(bot).get("userTierOverride") or {}
    if not isinstance(user_tier_override, dict):
        user_tier_override = {}

    return {
        "bot": bot,
        "primary": primary,
        "fallbacks": fallbacks,
        "catalog": catalog,
        "tiers": tiers,
        "routing": routing,
        "fallbackMode": fb_cfg["fallbackMode"],
        "tierCascade": fb_cfg["tierCascade"],
        "generatedFallbacks": generated,
        "cascade": cascade,
        "userTierOverride": user_tier_override,
    }


def json_full_config_set(
    bot: str,
    updates: dict,
    oc_json_path: Path | None = None,
    role: str | None = None,
) -> dict:
    """Atomically write one or more config sections for a bot.

    ``updates`` may contain any combination of:
        catalog       -- list[str]: replaces agents.defaults.models
        tiers         -- dict: stored in ~/.openclaw/evolve-tiers.json
        rungs         -- list: WHOLESALE replace of the bot's rung clusters
                         (per-bot Use-defaults/Custom toggle, spec §Addendum 5).
                         An empty list clears the key so the bot inherits the
                         pod/code default (the "Reset to pod defaults" path).
        roles         -- dict: WHOLESALE replace of the role→rung map; empty
                         dict clears it. Paired with ``rungs`` for the toggle.
        roleCaps      -- dict: WHOLESALE replace of per-role daily caps; empty
                         clears it. Carried alongside rungs/roles on Customize.
        routing       -- dict: stored in ~/.openclaw/evolve-tiers.json
        fallbackMode  -- str: stored in ~/.openclaw/evolve-tiers.json
        tierCascade   -- list[str]: stored in ~/.openclaw/evolve-tiers.json
        cascade       -- dict: stored in ~/.openclaw/evolve-tiers.json
                         (Phase-3 cutover flag — see CascadeController docstring.
                          Shape: ``{"enabled": bool}``. The plugin's ModelRouter
                          consults this at hook time to decide whether the
                          CascadeController's per-session verdict drives
                          routing or stays shadow-only.)
        userTierOverride -- dict: stored in ~/.openclaw/evolve-tiers.json
                         (per-bot operator-controlled defaults — audit #69
                          Phase A + PR #1780. Shape:
                          ``{"enabled": bool, "dailyCap": int,
                            "allowBotInitiated": bool, "defaultTier": str}``
                          Partial updates merge into the existing block,
                          same shape as cascade. The plugin's ModelRouter
                          reads ``defaultTier`` to seed user-turn /
                          ambiguous sessions before the bot-default
                          fallback kicks in.)

    ``role`` -- "primary" or "member" (or None for legacy behavior).
    Controls which default tierCascade is used to derive the flat
    fallback list (primary == workhorse-first; member == floor-first).
    When evolve-tiers.json has an explicit ``tierCascade`` field, that
    wins over the role-derived default (operator control preserved).
    Pre-role-aware callers that pass role=None get the legacy
    primary-cascade default — byte-for-byte the same as before.

    IMPORTANT: tiers (and related Evolve fields) are stored in the Evolve-owned
    evolve-tiers.json, NOT in openclaw.json. OC's schema does not allow them under
    agents.defaults.model.

    When tiers or tierCascade are updated, the flat fallback list
    (agents.defaults.model.primary + fallbacks) is automatically
    recomputed from the new tier cascade so OC always has a fresh
    native fallback chain.
    """
    path = oc_json_path or _DEFAULT_OC_JSON
    data = _load_json(path)
    mc = get_model_config(data)

    # Track whether openclaw.json itself needs rewriting. Some update
    # keys (catalog, tiers, tierCascade) feed into openclaw.json's
    # ``agents.defaults.models`` / ``model.primary`` / ``model.fallbacks``.
    # Others (routing, fallbackMode, cascade) live ONLY in evolve-
    # tiers.json and never touch openclaw.json. The previous version
    # rewrote openclaw.json unconditionally — wasting an OC schema-
    # validate shell-out for evolve-tiers-only flips and making
    # cascade-only writes fail in test environments where the
    # openclaw CLI isn't installed.
    oc_json_changed = False

    if "catalog" in updates:
        set_catalog(data, updates["catalog"])
        oc_json_changed = True

    # Load existing tiers file to merge into
    tiers_file = _load_tiers_file(bot)
    tiers_changed = False

    if "tiers" in updates:
        if _file_is_new_shape(tiers_file):
            # New-shape file: fold the legacy-shaped update into the rung
            # clusters the mapped roles point at (creating role/rung if the
            # operator is configuring a tier the file hasn't adopted yet).
            # Writing a sibling ``tiers`` key would be ignored by the gateway
            # loader — the freshness-advisory "apply" cosmetic-write bug.
            #
            # Guard against the silent rung-collision clobber FIRST: if the
            # update names two legacy tiers that fold to one rung with
            # different models (e.g. standard+judge → sonnet-class), the fold
            # below is last-writer-wins and one edit vanishes while the write
            # still reports success. Refuse the write with a structured,
            # operator-fixable error instead (model-tiers false-success,
            # 2026-06-27). The sentinel prefix lets the admin endpoint map it
            # to a 409 rather than a generic 500.
            _collisions = detect_rung_collisions(tiers_file, updates["tiers"])
            if _collisions:
                raise ValueError(
                    RUNG_COLLISION_PREFIX + format_rung_collision_error(_collisions)
                )
            apply_tiers_update_new_shape(tiers_file, updates["tiers"])
        else:
            # Legacy-only file: keep writing the legacy shape. The deploy-time
            # migrate-model-roles pass converts it to the new shape; we don't
            # half-migrate on a partial config-set write.
            tiers_file["tiers"] = updates["tiers"]
        tiers_changed = True

    # Wholesale rungs/roles writes — the per-bot Use-defaults/Custom toggle
    # (spec-model-rungs-and-roles §Addendum 5). Unlike the ``tiers`` key above
    # (which FOLDS a legacy-shaped per-tier edit into existing rungs), these
    # keys REPLACE the bot's whole rungs/roles cluster set wholesale:
    #   - "Customize this bot" writes the full merged rungs+roles (seeded from
    #     the bot's current resolved view), flipping the bot to Custom.
    #   - "Reset to pod defaults" writes empties (rungs:[] / roles:{}), which we
    #     drop below so the file carries NO rungs/roles and the merge inherits
    #     the pod/code default again.
    # An empty list/dict clears the key entirely (the all-or-nothing reset);
    # a non-empty value is written as-is. normalize_tiers_file_shape() runs on
    # save, so a stale legacy ``tiers`` sibling is folded/dropped automatically.
    # Canonicalize rung ids + backfill costClass on a wholesale rungs+roles
    # write (Customize this bot) so the bot's own catalog OVERLAYS the code/pod
    # default by matching ids instead of accumulating synthetic ``*-default``
    # rungs (spec Addendum 8 §D — same enforcement as the pod-default editor and
    # easy-setup write). Only when BOTH keys arrive non-empty (a full Custom
    # tier set); a bare reset (empty list/dict) or a partial write is left as-is.
    _incoming_rungs = updates.get("rungs")
    _incoming_roles = updates.get("roles")
    if (
        isinstance(_incoming_rungs, list) and _incoming_rungs
        and isinstance(_incoming_roles, dict) and _incoming_roles
    ):
        try:
            from primary_bot import canonicalize_catalog_rung_ids  # type: ignore
            _canon = canonicalize_catalog_rung_ids(
                {"rungs": _incoming_rungs, "roles": _incoming_roles}
            )
            updates = dict(updates)
            updates["rungs"] = _canon["rungs"]
            updates["roles"] = _canon["roles"]
        except Exception as _e:
            # Fail open — a canonicalizer import/shape error must not block a
            # legitimate bot tier write; the rank fix tolerates junk rungs. Log
            # so a silent skip is still visible in the admin-ui error log.
            print(f"[oc_model] rung canonicalize skipped: {_e}", file=sys.stderr)

    if "rungs" in updates:
        new_rungs = updates["rungs"]
        if isinstance(new_rungs, list) and new_rungs:
            tiers_file["rungs"] = new_rungs
        else:
            tiers_file.pop("rungs", None)
        tiers_changed = True
    if "roles" in updates:
        new_roles = updates["roles"]
        if isinstance(new_roles, dict) and new_roles:
            tiers_file["roles"] = new_roles
        else:
            tiers_file.pop("roles", None)
        tiers_changed = True
    if "roleCaps" in updates:
        new_caps = updates["roleCaps"]
        if isinstance(new_caps, dict) and new_caps:
            tiers_file["roleCaps"] = new_caps
        else:
            tiers_file.pop("roleCaps", None)

    if "routing" in updates:
        tiers_file["routing"] = updates["routing"]

    if "fallbackMode" in updates:
        tiers_file["fallbackMode"] = updates["fallbackMode"]

    if "tierCascade" in updates:
        tiers_file["tierCascade"] = updates["tierCascade"]
        tiers_changed = True

    if "cascade" in updates:
        # The cascade key is a sibling of tiers/routing in evolve-tiers.json.
        # Shape: {"enabled": bool}. The plugin's ModelRouter / CascadeController
        # reads cascade.enabled to decide whether to honor per-session verdicts
        # (Phase 3) or keep them shadow-only (Phase 2 default). Doesn't affect
        # the fallback list — purely a routing-precedence flag.
        existing_cascade = tiers_file.get("cascade") or {}
        if isinstance(existing_cascade, dict) and isinstance(updates["cascade"], dict):
            existing_cascade.update(updates["cascade"])
            tiers_file["cascade"] = existing_cascade
        else:
            tiers_file["cascade"] = updates["cascade"]

    if "userTierOverride" in updates:
        # Per-bot operator defaults — partial merge (so a defaultTier-only
        # write doesn't clobber dailyCap, and an enabled-only write doesn't
        # clobber defaultTier). Same pattern as cascade above. Whitelist of
        # accepted keys keeps drive-by writers from poisoning the block;
        # unknown keys are silently dropped rather than rejecting the
        # whole write (matches the conservative "less aggressive" stance
        # the plugin-side fall-through already takes).
        _ALLOWED_OVERRIDE_KEYS = {
            "enabled", "dailyCap", "allowBotInitiated", "defaultTier",
        }
        existing_override = tiers_file.get("userTierOverride") or {}
        if not isinstance(existing_override, dict):
            existing_override = {}
        incoming = updates["userTierOverride"]
        if isinstance(incoming, dict):
            for k, v in incoming.items():
                if k in _ALLOWED_OVERRIDE_KEYS:
                    existing_override[k] = v
            tiers_file["userTierOverride"] = existing_override
        else:
            # Non-dict (shouldn't happen — server validates) → reject by
            # leaving existing block untouched. Logged via the structured
            # error in the wrapper.
            pass

    # Persist Evolve-owned config to evolve-tiers.json (separate from openclaw.json).
    # NORMALIZE on every write: if a stale legacy ``tiers`` key sits alongside
    # ``rungs`` (pollution from an old-code freshness "apply"), fold it into the
    # new shape and drop it so the gateway loader sees the operator's intent.
    if any(k in updates for k in (
        "tiers", "routing", "fallbackMode", "tierCascade",
        "cascade", "userTierOverride", "rungs", "roles", "roleCaps",
    )):
        normalize_tiers_file_shape(tiers_file)
        _save_tiers_file(bot, tiers_file)

    # Recompute flat fallback list whenever tiers or cascade change.
    # This is the path that propagates a tier-config change into
    # openclaw.json's model.primary and model.fallbacks fields.
    #
    # Cascade-order precedence:
    #   1. Explicit tierCascade in evolve-tiers.json (operator's wish wins)
    #   2. Role-aware default (member → floor-first; primary → workhorse-first)
    #   3. Legacy DEFAULT_TIER_CASCADE (workhorse-first) when no role provided
    #
    # The role-aware branch is the L2 audit fix: every member bot was
    # running Sonnet primary (~5x cost vs Haiku floor) because step 3
    # was the only path and it always picked tier2 first regardless of
    # what the bot's tier3 was configured to.
    if tiers_changed:
        # Derive the flat fallback from the legacy tier view of whatever shape
        # the file now is (new shape was folded above, then normalized).
        tiers = synthesize_legacy_tiers(tiers_file)
        cascade = tiers_file.get(
            "tierCascade",
            default_tier_cascade_for_role(role),
        )
        flat = generate_fallback_list(tiers, cascade)
        if flat:
            mc["primary"] = flat[0]
            mc["fallbacks"] = flat[1:]
            oc_json_changed = True

    # Only touch openclaw.json (and trigger OC schema validation) when
    # an update actually changed something there. evolve-tiers-only
    # updates (routing, fallbackMode, cascade) skip this entire block.
    if oc_json_changed:
        # set_model_config strips OC-forbidden fields before writing
        set_model_config(data, mc)
        _preserve_write(data, path)

    return json_full_config(bot, path)


def json_memory(bot: str, oc_json_path: Path | None = None) -> dict:
    """Return current agents.defaults.memorySearch block."""
    path = oc_json_path or _DEFAULT_OC_JSON
    data = _load_json(path)
    ms = get_memory_search_config(data)
    return {
        "bot": bot,
        "provider": ms.get("provider"),
        "fallback": ms.get("fallback"),
        "ok": True,
    }


def json_memory_set(
    bot: str,
    provider: str,
    fallback: str | None = None,
    oc_json_path: Path | None = None,
) -> dict:
    """Set agents.defaults.memorySearch.{provider, fallback}.

    Empty/missing provider clears the block, reverting to OpenClaw auto-detect.
    """
    path = oc_json_path or _DEFAULT_OC_JSON
    data = _load_json(path)

    if not provider:
        set_memory_search_config(data, {})
        _preserve_write(data, path)
        return {"bot": bot, "provider": None, "fallback": None, "ok": True}

    ms: dict = {"provider": provider}
    if fallback:
        ms["fallback"] = fallback
    set_memory_search_config(data, ms)
    _preserve_write(data, path)
    return {
        "bot": bot,
        "provider": provider,
        "fallback": fallback,
        "ok": True,
    }


def json_models_set(
    bot: str,
    model_list_str: str,
    oc_json_path: Path | None = None,
) -> dict:
    """Set primary + fallbacks from a space-separated model list (legacy API).

    First token -> primary; remaining tokens -> fallbacks in order.
    """
    models = model_list_str.strip().split()
    if not models:
        return {"error": "no models provided", "bot": bot, "ok": False}

    path = oc_json_path or _DEFAULT_OC_JSON
    data = _load_json(path)
    mc = get_model_config(data)
    mc["primary"] = models[0]
    mc["fallbacks"] = models[1:]
    set_model_config(data, mc)
    _preserve_write(data, path)

    return {
        "bot": bot,
        "primary": mc["primary"],
        "fallbacks": mc["fallbacks"],
        "fallback_order": mc["fallbacks"],
        "ok": True,
    }


# -- Standalone CLI entrypoint ------------------------------------------------

if __name__ == "__main__":
    """
    Called as:
        sudo -u team_bot_a python3 oc_model.py models team_bot_a
        sudo -u team_bot_a python3 oc_model.py models set team_bot_a "primary fb1 fb2"
        sudo -u team_bot_a python3 oc_model.py config team_bot_a
        sudo -u team_bot_a python3 oc_model.py config set team_bot_a '{"tiers":{...}}'

    Output is always JSON on stdout; errors exit non-zero.
    """
    _args = sys.argv[1:]
    if len(_args) < 2 or _args[0] not in ("models", "config", "memory"):
        print(json.dumps({
            "error": "usage: oc_model.py {models|config|memory} {bot} [set ...]",
        }))
        sys.exit(1)

    _cmd = _args[0]
    _subcmd = _args[1]

    try:
        if _cmd == "models":
            if _subcmd == "set":
                if len(_args) < 4:
                    print(json.dumps({"error": "usage: oc_model.py models set {bot} {model_list}"}))
                    sys.exit(1)
                _result = json_models_set(_args[2], _args[3])
            else:
                _result = json_models(_subcmd)

        elif _cmd == "config":
            if _subcmd == "set":
                if len(_args) < 4:
                    print(json.dumps({
                        "error": "usage: oc_model.py config set {bot} {json} [role]",
                    }))
                    sys.exit(1)
                _updates = json.loads(_args[3])
                # Optional 5th positional: role ("primary" or "member").
                # Used to derive the default tierCascade — see
                # default_tier_cascade_for_role docstring for the L2
                # cost-bleed context.
                _role = _args[4] if len(_args) >= 5 else None
                _result = json_full_config_set(_args[2], _updates, role=_role)
            else:
                _result = json_full_config(_subcmd)

        elif _cmd == "memory":
            if _subcmd == "set":
                # usage: oc_model.py memory set {bot} {provider} [fallback]
                if len(_args) < 4:
                    print(json.dumps({"error": "usage: oc_model.py memory set {bot} {provider} [fallback]"}))
                    sys.exit(1)
                _bot_arg = _args[2]
                _provider_arg = _args[3] if len(_args) >= 4 else ""
                _fallback_arg = _args[4] if len(_args) >= 5 else None
                _result = json_memory_set(_bot_arg, _provider_arg, _fallback_arg)
            else:
                # usage: oc_model.py memory {bot}
                _result = json_memory(_subcmd)

    except Exception as _exc:
        print(json.dumps({"error": str(_exc), "ok": False}))
        sys.exit(1)

    print(json.dumps(_result))
