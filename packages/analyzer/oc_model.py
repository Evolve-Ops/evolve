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
        "tier3": { "models": ["anthropic/claude-haiku-4-5", "openai/gpt-4o-mini"] }
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

import copy
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from evolve_util import assert_safe_sudo_dest
from evolve_util import atomic_write_json as _atomic_write_json

# Default path when the script is run AS the bot user (home = bot's home dir)
_DEFAULT_OC_JSON: Path = Path.home() / ".openclaw" / "openclaw.json"

# Evolve-owned tier config is stored in the bot user's own ~/.openclaw dir.
# oc_model.py always runs as the bot user (via "sudo -u {bot} python3 oc_model.py"),
# so Path.home() resolves to /Users/{bot} and write access is guaranteed.
_TIERS_FILENAME = "evolve-tiers.json"

# ── evolve-tiers.json drift declarations (spec-delta-digest-audit-noise D3) ───
#
# ``heal.detect_backup_drift_keys`` namespaces every evolve-tiers.json diff as
# ``tiers:<top-level key>`` and then filters those names against the keys
# writers self-declared (audit-log ``oc_keys`` / apply-results). Until this
# block landed, NO writer anywhere emitted a ``tiers:``-prefixed name, so the
# whole namespace was structurally unexplainable: an authorized Evolve write
# was permanently indistinguishable from a hand edit, on every bot that has an
# evolve-tiers.json. (Live on a pod 2026-08-25: two bots firing
# ``tiers:rungs`` + ``tiers:autoUpgrade`` forever.)
#
# The declaration is derived from what the write ACTUALLY changed on disk, not
# from the payload it was handed — ``_carry_pod_auto_upgrade`` and
# ``normalize_tiers_file_shape`` both leave keys the caller never named, and
# ``tiers:autoUpgrade`` (a carry, never in any request body) is precisely half
# of today's live finding. :func:`json_full_config_set` diffs the tiers doc
# across the write and reports the changed top-level keys as
# ``tiersKeysWritten``; call sites turn that into ``oc_keys`` via
# :func:`tiers_drift_declarations`.
TIERS_DRIFT_PREFIX = "tiers:"

# The result field carrying the changed keys. Present only when a write
# actually changed something in evolve-tiers.json, so the happy-path result
# shape is unchanged for existing consumers (same convention as
# ``routingKeysRefused``).
TIERS_KEYS_WRITTEN_FIELD = "tiersKeysWritten"

# The ``updates`` keys :func:`json_full_config_set` routes into
# evolve-tiers.json. Single source of truth for the save gate at the bottom of
# that function — everything else (``catalog``) lands in openclaw.json, and
# ``podAutoUpgrade`` is advisory context that is never persisted verbatim.
TIERS_UPDATE_KEYS = (
    "tiers", "routing", "fallbackMode", "tierCascade",
    "cascade", "userTierOverride", "rungs", "roles", "roleCaps",
    "autoUpgrade",
)


def changed_top_level_keys(before: "dict | None", after: "dict | None") -> "list[str]":
    """Top-level keys whose value differs between two JSON documents.

    Added/removed keys count as changed. Same comparison
    ``heal.detect_backup_drift_keys`` runs against the committed baseline, so a
    declaration built from this covers exactly what the drift check can emit.
    """
    b = before or {}
    a = after or {}
    return sorted(k for k in (set(b) | set(a)) if b.get(k) != a.get(k))


def tiers_drift_declarations(write_result: "Any") -> "set[str]":
    """``{"tiers:<key>", ...}`` for the evolve-tiers.json keys a write changed.

    Feed this into the ``oc_keys`` of the audit entry that records the write
    (union it with any genuine openclaw.json keys the same write touched —
    a tiers edit usually recomputes ``agents.defaults.model`` too).

    Returns an empty set for anything that isn't a write result carrying a
    ``tiersKeysWritten`` list — a write that changed nothing declares nothing,
    and an unrecognized result declares nothing rather than guessing. Both
    degrade toward OVER-alerting, which is the side heal deliberately errs on.
    """
    if not isinstance(write_result, dict):
        return set()
    written = write_result.get(TIERS_KEYS_WRITTEN_FIELD)
    if not isinstance(written, list):
        return set()
    return {f"{TIERS_DRIFT_PREFIX}{k}" for k in written if k}

# evolve-tiers.json is model-ROUTING config, not a secret — 0644 so the bot
# can read its own copy. Mirrors evolve_admin.secret_config_perms
# .BOT_OWNED_CONFIG_MODE (that module is not importable from here — see
# _restore_tiers_bot_ownership for why).
_TIERS_FILE_MODE = 0o644
_TIERS_FILE_MODE_ARG = oct(_TIERS_FILE_MODE)[2:]  # "644"

# Unix account names, as re-asserted before a name is interpolated into a sudo
# argv: no whitespace, no comma, no ``:`` — the characters that could change
# what ``chown <user>:staff`` means. Mirrors tier_prefs_acl._SAFE_UNIX_NAME_RE.
_SAFE_UNIX_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z")

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
    # Content-classifier maintenance downgrades for HUMAN conversations are
    # opt-in (routing-card rework): keyword misreads have routed real work
    # to the cheap model, so the default is off. Trigger-anchored routing
    # (cron/heartbeat/scaffolding) is unaffected by this knob.
    "classifierDowngrade": False,
}

# Keys the ``routing`` block accepts, with the value predicate each must
# satisfy. Single source of truth for BOTH the endpoint's 400 boundary
# (``PUT /api/admin/config/<bot>/routing``) and the writer's whitelist merge
# below — #3566 audit E-3, where the endpoint validated nothing and the writer
# did a bare wholesale assignment.
#
# Two naming generations coexist: the ``*Tier`` keys are the legacy/admin-UI
# view (the routing card still posts them), the ``*Role`` keys are what the
# plugin's ModelRouter reads post-rungs and what ``migrate-model-roles``
# writes. Both are ACCEPTED, but they are not independent — see
# ROUTING_SLOT_SIBLING below: each pair is ONE logical slot, because the
# plugin resolves ``maintenanceRole ?? maintenanceTier`` (Role wins), so
# keeping both would let a stale Role permanently shadow the operator's Tier.
def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _is_str_or_none(v: Any) -> bool:
    return v is None or isinstance(v, str)


def _is_tier_id_or_none(v: Any) -> bool:
    return v is None or (isinstance(v, str) and re.fullmatch(r"tier\d+", v) is not None)


# Roles a routing slot may name. Constrained at the trust boundary so every
# role the API accepts is one project_routing_to_tier_view can invert (``max``
# has no legacy tier and is handled by the suppression in get_routing). A
# free-form role would be accepted, stored, then read back as a two-generation
# view — a document the endpoint's own PUT refuses.
ROUTING_ROLE_NAMES = ("fast", "standard", "power", "max")


def _is_role_name_or_none(v: Any) -> bool:
    return v is None or v in ROUTING_ROLE_NAMES


def _is_unit_number(v: Any) -> bool:
    # bool is an int subclass — exclude it explicitly.
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= float(v) <= 1.0


ROUTING_KEY_VALIDATORS: "dict[str, tuple[Callable[[Any], bool], str]]" = {
    "enabled": (_is_bool, "a boolean"),
    "classifierDowngrade": (_is_bool, "a boolean"),
    "confidenceThreshold": (_is_unit_number, "a number between 0 and 1"),
    "maintenanceTier": (_is_tier_id_or_none, "a tierN id string or null"),
    "backgroundTier": (_is_tier_id_or_none, "a tierN id string or null"),
    "ambiguousTier": (_is_tier_id_or_none, "a tierN id string or null"),
    "maintenanceRole": (_is_role_name_or_none, "one of " + "/".join(ROUTING_ROLE_NAMES) + " or null"),
    "backgroundRole": (_is_role_name_or_none, "one of " + "/".join(ROUTING_ROLE_NAMES) + " or null"),
    "ambiguousRole": (_is_role_name_or_none, "one of " + "/".join(ROUTING_ROLE_NAMES) + " or null"),
}

ROUTING_ALLOWED_KEYS = frozenset(ROUTING_KEY_VALIDATORS)

# Each ``<slot>Tier`` / ``<slot>Role`` pair is ONE logical routing slot. The
# plugin's ModelRouter._normalizeRouting resolves
# ``toRole(r.maintenanceRole ?? r.maintenanceTier)`` — the Role key WINS — so a
# write that sets the Tier key must delete its Role sibling, or the operator's
# edit is accepted, echoed back, re-rendered by the card, and then permanently
# shadowed by the stale role on every bot that ``migrate-model-roles`` touched.
# (The pre-#3566-E-3 wholesale replace got this right only by accident: it
# deleted every key it didn't send.)
ROUTING_SLOT_PAIRS = (
    ("maintenanceRole", "maintenanceTier"),
    ("backgroundRole", "backgroundTier"),
    ("ambiguousRole", "ambiguousTier"),
)

ROUTING_SLOT_SIBLING = {
    "maintenanceTier": "maintenanceRole", "maintenanceRole": "maintenanceTier",
    "backgroundTier": "backgroundRole", "backgroundRole": "backgroundTier",
    "ambiguousTier": "ambiguousRole", "ambiguousRole": "ambiguousTier",
}


def validate_routing_update(routing: Any) -> "str | None":
    """Return an operator-facing error, or ``None`` when ``routing`` is a
    well-formed (possibly partial) routing update.

    Callers MUST run this before persisting. Before #3566 audit E-3 the
    endpoint persisted the body unvalidated, so a non-dict (str / list / int /
    bool) landed in evolve-tiers.json and then wedged :func:`get_routing`
    forever — self-concealing, because the throw happens in the read-back
    AFTER the save succeeded.
    """
    if not isinstance(routing, dict):
        return (
            f"routing must be a JSON object, got {type(routing).__name__} "
            "— refusing the write"
        )
    unknown = sorted(k for k in routing if k not in ROUTING_ALLOWED_KEYS)
    if unknown:
        return (
            "unknown routing key(s): " + ", ".join(unknown)
            + " (accepted: " + ", ".join(sorted(ROUTING_ALLOWED_KEYS)) + ")"
        )
    for key, value in routing.items():
        predicate, expected = ROUTING_KEY_VALIDATORS[key]
        if not predicate(value):
            return f"routing.{key} must be {expected}, got {value!r}"
    # Both halves of one slot in a single body is ambiguous — the plugin would
    # honor the Role and drop the Tier on the floor. Refuse rather than pick.
    for key in sorted(routing):
        sibling = ROUTING_SLOT_SIBLING.get(key)
        if sibling and sibling in routing and key.endswith("Tier"):
            return (
                f"routing names both {key} and {sibling} — they are the same "
                "slot; send one"
            )
    return None


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

    Stage 1 goes through ``evolve_util.atomic_write_json`` (#3566 audit D-1).
    It used to stage at the DETERMINISTIC name ``<dest>.tmp`` via
    ``Path.write_text`` — plain ``open(..., "w")``, i.e. no ``O_EXCL`` and no
    ``O_NOFOLLOW``, so a symlink pre-planted at that exact name was FOLLOWED
    and the caller's JSON landed on the link's target; ``os.replace`` then
    promoted the result into the live tiers file. There was no race to win:
    the name is derived from the destination, so the plant can sit there
    indefinitely. ``atomic_write_json`` uses ``tempfile.mkstemp`` in the same
    directory — random name, ``O_CREAT|O_EXCL|O_NOFOLLOW`` — which removes
    the predictable target entirely, keeps the same-dir ``os.replace``
    atomicity, and (verified in #3574) still raises ``PermissionError`` on
    POSIX when the directory is not writable, so the stage-2 fallback below
    is reached exactly as before. ``os.replace`` does not follow a symlink at
    the DESTINATION either — it replaces the link itself — so the promoted
    file is always a real file at ``path``.
    """
    path = _tiers_path(bot)
    content = json.dumps(tiers_data, indent=2)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # mode=0o644: the tiers file is model-ROUTING config, not a secret, and
        # the bot must be able to read its own copy. mkstemp defaults to 0600,
        # so the mode has to be pinned rather than inherited (see the sibling
        # hazard in feedback_tempfile_rename_carries_0600_onto_dest).
        _atomic_write_json(path, tiers_data, mode=_TIERS_FILE_MODE)
        return
    except PermissionError as _e:
        # atomic_write_json removes its own temp on any failure, so there is
        # nothing to clean up here. Fall through to the /tmp + sudo path.
        print(
            f"[oc_model] direct tiers write denied ({_e}); using sudo fallback",
            file=sys.stderr,
        )
    import tempfile
    fd, tmp_path = tempfile.mkstemp(dir="/tmp", prefix="evolve-tiers-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        # D-2 gate — see evolve_util.assert_safe_sudo_dest. `cp` FOLLOWS a
        # symlink at the destination, and this cp runs as root, so an unchecked
        # dest is a root-write primitive. Refuse rather than write through a
        # link. Reproduced end-to-end against THIS function on origin/main:
        # with ~/.openclaw/evolve-tiers.json replaced by a symlink to a victim
        # file, the victim's CONTENT was overwritten through the link. The mode
        # half of the escalation — the victim relabelled 0600 → 0644 by the
        # ownership repair below — did NOT fire in that run, only because B-1
        # kept that repair dead outside /Users. That is the ordering trap in one
        # observation: fixing B-1 alone would have completed the primitive.
        assert_safe_sudo_dest(path)
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


def _tiers_bot_user_from_path(path: Path) -> "str | None":
    """Derive the owning bot account from a ``<home_root>/<user>/.openclaw/…`` path.

    ``<home_root>`` comes from the platform profile — ``/Users`` on macOS,
    ``/home`` on Linux. This is path PARSING, not the path CONSTRUCTION that
    ``platform_profile.user_home_root``'s docstring warns against: the path was
    produced by ``Path.home()`` on this same host, and the account name is read
    back out of it rather than interpolated into a new one.

    Returns ``None`` for anything else (test tmpdirs, unexpected home layouts)
    so the caller no-ops instead of chowning an unrelated file.
    """
    parts = Path(path).parts
    prof = _get_platform_profile()
    if prof is None:
        return None
    home_root = Path(prof.user_home_root).parts
    idx = len(home_root)
    # EXACT shape ``<home_root>/<user>/.openclaw/<file>``. Anything looser lets
    # a shorter path (``<home_root>/.openclaw/<file>``) hand back ".openclaw"
    # as the account name, or a deeper one point the repair at a nested file.
    if len(parts) != idx + 3:
        return None
    if parts[:idx] != home_root or parts[idx + 1] != ".openclaw":
        return None
    bot_user = parts[idx]
    # The name is interpolated into a sudo argv (``<user>:staff``); re-assert a
    # strict account shape at the sink, as tier_prefs_acl does (#3565 audit).
    if not _SAFE_UNIX_NAME_RE.fullmatch(bot_user):
        return None
    return bot_user


def _get_platform_profile():
    """The running host's ``PlatformProfile``, or ``None`` if unavailable.

    Imported lazily: ``oc_model.py`` is executed by the SYSTEM python
    (``/usr/bin/python3`` — see ``oc_cli._sudo_python``), so it must degrade
    rather than die if a sibling module is missing from a partial deploy. The
    repair below is best-effort anyway.
    """
    try:
        from platform_profile import get_profile  # local sibling, stdlib-only
        return get_profile()
    except Exception as e:  # pragma: no cover - defensive
        print(f"[oc_model] platform profile unavailable: {e}", file=sys.stderr)
        return None


def _restore_tiers_bot_ownership(path: Path) -> None:
    """``sudo chown <bot>:staff`` + ``sudo chmod 644`` a tiers file landed by
    ``sudo /bin/cp`` so the bot can read its own config.

    The bot user is derived from the path
    (``<home_root>/<user>/.openclaw/...``), so this is correct regardless of
    which user the process runs as. Best-effort: the cp already succeeded; a
    chown failure only matters when the dest was freshly created (then it's
    root-owned), and on macOS the deploy-time self-heal (``ensure_pod_perms``
    → ``check_bot_tiers_ownership``) converges it.

    Do NOT lean on that backstop on Linux. ``check_bot_tiers_ownership``'s
    repair goes through ``secret_config_perms.chown_chmod_bot_config`` →
    ``_bot_user_from_path``, which still carries the ``/Users``-only bug this
    function just shed (measured: ``/home/<bot>/...`` → ``None`` → the repair
    returns False without issuing anything). Fixing it is a one-site change in
    ``evolve_admin``, deliberately left out of this PR to keep the blast radius
    on the analyzer side — tracked separately. Until it lands, this repair is
    the ONLY thing that converges a fresh root-owned tiers file on the VPS.

    #3566 audit B-1 — two independent bugs made this DEAD on Linux, and the
    previous docstring asserted the opposite ("Linux homes never reach the
    sudo branch"). That premise was false, and both bugs are fixed here:

      * the home-root guard was hardcoded to ``/Users``, so every
        ``/home/<bot>/...`` path early-returned before issuing any sudo call
        (measured: 2 sudo calls for a ``/Users`` path, 0 for ``/home``). The
        root now comes from ``platform_profile.user_home_root``.
      * the chown BINARY was hardcoded to the macOS ``/usr/sbin/chown``, but
        ``platform_profile.LINUX.chown`` is ``/usr/bin/chown`` and only THAT
        is rendered into the Linux evolve sudoers grant
        (``_render_evolve_sudoers``: ``{chown} * {home}/*/.openclaw/
        evolve-tiers.json``). A hardcoded macOS path is absent from the Linux
        NOPASSWD allowlist, so sudo would fall through to a password prompt
        and the TTY-less daemon fails with "sudo: a terminal is required".
        Both binaries now route through the profile — the same fix, and the
        same hazard note, that ``secret_config_perms.chown_chmod_bot_config``
        already carries. That helper is NOT reused directly: it lives in
        ``evolve_admin`` (venv-only, imports ``pwd`` + the admin ``runtime``/
        ``telemetry`` packages), and this module is executed by the world-
        executable SYSTEM python as the bot user, where ``evolve_admin`` is
        not importable. ``platform_profile`` is a stdlib-only sibling in
        ``packages/analyzer``, so the profile routing is done locally instead.

    Symlink gate (D-2): ``chown``/``chmod`` without ``-h`` follow a symlink
    argument, and both run as root here — so the destination is re-checked
    through ``evolve_util.assert_safe_sudo_dest`` before either fires. Re-checked
    deliberately, not redundantly: ``_save_tiers_file`` already gated the same
    path before its ``cp``, but a ``cp`` that went through a link left the LINK
    in place, so a second check is what stops the repair from relabelling a
    victim that was planted inside the window. This is load bearing: B-1's two
    bugs were, until now, the ONLY thing keeping this escalation-grade step from
    running on the Linux pod. Fixing B-1 without D-2 would arm it.
    """
    bot_user = _tiers_bot_user_from_path(path)
    if not bot_user:
        return
    try:
        assert_safe_sudo_dest(path)
    except PermissionError as e:
        print(f"[oc_model] ownership repair skipped: {e}", file=sys.stderr)
        return
    prof = _get_platform_profile()
    if prof is None:
        # No profile → no way to know which chown binary this host's sudoers
        # grant was rendered with. Falling back to a LITERAL here is what B-1
        # was: a hardcoded /usr/sbin/chown does not exist on Linux, and the
        # subprocess is unchecked, so it would fail silently and leave the
        # file root-owned. Skip loudly instead.
        print(
            f"[oc_model] ownership repair skipped for {path}: no platform profile",
            file=sys.stderr,
        )
        return
    subprocess.run(
        ["sudo", prof.chown, f"{bot_user}:staff", str(path)],
        capture_output=True, text=True,
    )
    subprocess.run(
        ["sudo", prof.chmod, _TIERS_FILE_MODE_ARG, str(path)],
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
    # tier0 (the legacy judge slot) no longer maps to a role — the judge role
    # was collapsed into the cross-vendor derivation (design-judge-role-
    # collapse-2026-08-21 §5.4). A tier0 write is now an unmapped tierN
    # (dropped/refused like tier7); a tier0 already on disk stays parseable
    # via the read-side fold in primary_bot._normalize_legacy_layer.
}
_ROLE_TO_TIER: dict[str, str] = {v: k for k, v in _TIER_TO_ROLE.items()}
_TIER_TO_RUNG: dict[str, str] = {
    "tier3": "haiku-class",
    "tier2": "sonnet-class",
    "tier1": "opus-class",
    # ``max`` has no legacy tierN slot (it is the new frontier rung). The
    # per-bot Tier Definitions panel edits it under the role key ``max``;
    # the write path folds it into the ``fable-class`` rung. (spec §Addendum3.C.6)
    "max": "fable-class",
}
_TIER_COST_CLASS: dict[str, str] = {
    "tier3": "low",
    "tier2": "medium",
    "tier1": "high",
    # tier0 kept here (COST data only): generate_fallback_list still ranks a
    # legacy on-disk cascade that names tier0, even though nothing writes it.
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

    Roles are uniformly ``role → rung-slug`` (the structured ``{rung,
    provider}`` judge shape died with the judge role). Returns None when the
    role is absent (caller may create it).
    """
    roles = tiers_file.get("roles")
    if isinstance(roles, dict):
        entry = roles.get(role)
        if isinstance(entry, str) and entry:
            return entry
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
    following the spec's slug conventions when absent.
    """
    rung_id = _rung_for_role(tiers_file, role)
    if rung_id is None:
        rung_id = _TIER_TO_RUNG[tier_key]
        roles = tiers_file.setdefault("roles", {})
        if not isinstance(roles, dict):
            roles = {}
            tiers_file["roles"] = roles
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

    ``tier0`` (the legacy judge slot — no role since the judge-role collapse,
    design-judge-role-collapse-2026-08-21 §6) is handled AFTER the main fold,
    mirroring ``migrate_model_roles``: its models MERGE into the standard
    rung's chain as fallback entries (append-dedup — never replacing the
    chain), or land in an orphan ``judge-class`` rung when the file defines
    no standard rung of its own. The post-loop ordering keeps the result
    independent of the payload's key order (a tier2 SET always precedes the
    tier0 append).
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

    # tier0 merge (judge-role collapse) — after the ladder fold, see docstring.
    tier0 = updates_tiers.get("tier0")
    tier0_models = tier0.get("models") if isinstance(tier0, dict) else None
    clean0 = [m for m in (tier0_models or []) if isinstance(m, str) and m]
    if clean0:
        std_slug = _rung_for_role(tiers_file, "standard") or _TIER_TO_RUNG["tier2"]
        rungs = tiers_file.get("rungs")
        rungs = rungs if isinstance(rungs, list) else []
        target = next(
            (r for r in rungs
             if isinstance(r, dict) and r.get("id") in (std_slug, "judge-class")),
            None,
        )
        if target is None:
            # No standard rung in THIS file: keep the models as an orphan rung
            # (parseable, never routed) rather than minting a standard chain
            # from judge picks — that would silently change the workhorse.
            tiers_file.setdefault("rungs", rungs)
            rungs.append({"id": "judge-class", "models": clean0, "costClass": "medium"})
            if tiers_file.get("rungs") is not rungs:
                tiers_file["rungs"] = rungs
        else:
            merged = [m for m in (target.get("models") or []) if isinstance(m, str)]
            for m in clean0:
                if m not in merged:
                    merged.append(m)
            target["models"] = merged


# Sentinel prefix on the structured error a rung-collision raise emits, so the
# admin endpoint can tell a "two roles fight over one rung" conflict (operator
# can fix it — return 409) apart from a genuine write failure (return 500). The
# human-readable cause follows the prefix and is shown to the operator verbatim.
RUNG_COLLISION_PREFIX = "RUNG_COLLISION: "


def detect_rung_collisions(tiers_file: dict, updates_tiers: dict) -> list[dict]:
    """Find incoming legacy tiers that fold to the SAME rung with DIFFERENT models.

    Under the new (rungs/roles) shape two legacy tier keys can map to one rung
    if an operator points them there (the guard is a general safety net for
    any hand-authored roles map that points two roles at one rung —
    historically standard+judge → sonnet-class; the judge role is gone, but
    the collision class is not judge-specific).
    :func:`apply_tiers_update_new_shape` folds each legacy tier into its rung
    last-writer-wins, so two incoming tiers that share a rung
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
    if not isinstance(updates_tiers, dict):
        return []
    if not _file_is_new_shape(tiers_file):
        # No rungs — but a ``roles`` map alone is enough to make two incoming
        # tiers fold to one rung, because _ensure_role_and_rung resolves through
        # the roles map first and CREATES the rung it names. That state is
        # reachable when a wholesale ``roles`` replacement lands ahead of the
        # fold in the same payload (#3566 follow-up). Without this, the write
        # takes the create path, materializes one rung, and reports success
        # while one of the two edits is gone — the exact false-success this
        # guard exists to stop.
        roles = tiers_file.get("roles")
        if not (isinstance(roles, dict) and roles):
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
    if not _file_is_new_shape(tiers_file) or "tiers" not in tiers_file:
        return tiers_file
    if isinstance(tiers_file["tiers"], dict):
        apply_tiers_update_new_shape(tiers_file, tiers_file["tiers"])
    # A non-dict ``tiers`` (null, a string, a list) carries no allocations to
    # fold, but it is still the deprecated key sitting beside ``rungs`` — the
    # mixed shape this function exists to eliminate. Drop it either way, or it
    # survives every write AND `migrate-model-roles` (which leaves changed=False
    # for a non-dict tiers), leaving the file permanently mixed.
    tiers_file.pop("tiers", None)
    return tiers_file


#: Order the create path folds incoming legacy tiers in, so the rungs[] array
#: it emits does not depend on the payload's key order. Mirrors the migrator:
#: the ladder in cost order, then ``max``. Array position IS the cost rank, so
#: it is not a property to leave dependent on dict iteration order. (tier0 was
#: dropped with the judge role — an incoming tier0 is now unmapped and refused
#: like any unknown tierN.)
_CREATE_TIER_ORDER: tuple[str, ...] = ("tier3", "tier2", "tier1", "max")


def legacy_tiers_for_create(tiers: dict) -> "tuple[dict, dict]":
    """Normalize a legacy ``tiers`` payload for the create path.

    Returns ``(ordered_tiers, role_caps)``. Brings the payload up to what
    ``migrate_model_roles.migrate_evolve_tiers`` does with the same input, so a
    file created here is what `migrate-model-roles --apply` would have written:

    - canonical key order (see :data:`_CREATE_TIER_ORDER`);
    - a per-tier ``fallbacks`` list folded into the rung cluster after the
      primaries, dedup-preserving — the rungs shape has no separate fallback
      slot, so dropping it would silently discard models the operator sent;
    - ``tier1.maxPerDayPerBot`` lifted to ``roleCaps.power``, its new home;
    - tiers with no usable models skipped entirely. On a CREATE there is
      nothing to clear, so an empty list is not an edit — and materializing an
      empty rung for it would flip the bot to Custom over a no-op.

    Unknown keys are dropped by the fold downstream; they are passed through
    here so the caller can still tell an all-unknown payload from an empty one.
    """
    if not isinstance(tiers, dict):
        # A non-dict payload (null, a string, a list) carries no allocations.
        # The tiers PUT does zero body validation, so this reaches the writer;
        # the fold path has always tolerated it (apply_tiers_update_new_shape
        # returns early), and the create path must too rather than raising
        # AttributeError/TypeError out of a function contracted to return a dict.
        return {}, {}
    ordered: dict = {}
    for key in (*_CREATE_TIER_ORDER,
                *(k for k in tiers if k not in _CREATE_TIER_ORDER)):
        cfg = tiers.get(key)
        if not isinstance(cfg, dict):
            continue
        models = cfg.get("models")
        if not isinstance(models, list):
            continue
        cluster = [m for m in models if isinstance(m, str) and m]
        for m in (cfg.get("fallbacks") or []):
            if isinstance(m, str) and m and m not in cluster:
                cluster.append(m)
        if not cluster:
            continue
        ordered[key] = {"models": cluster}

    role_caps: dict = {}
    cap = (tiers.get("tier1") or {}).get("maxPerDayPerBot") if isinstance(
        tiers.get("tier1"), dict) else None
    if isinstance(cap, int) and not isinstance(cap, bool):
        role_caps["power"] = {"maxPerDayPerBot": cap}
    return ordered, role_caps


#: Advisory update key carrying the POD's ``models.autoUpgrade`` block into a
#: write. It is never persisted verbatim — see :func:`_carry_pod_auto_upgrade`.
#: Injected by ``oc_cli.oc_full_config_set_with_error``, which is the layer that
#: can read network.json (this script runs as the bot user and deliberately
#: does not), exactly like the ``role`` positional it already threads through.
POD_AUTO_UPGRADE_KEY = "podAutoUpgrade"


def _carry_pod_auto_upgrade(
    tiers_file: dict, updates: dict, *, caller_set: bool = False,
) -> None:
    """Seed the pod's auto-upgrade block onto a bot that just became Custom.

    Lifecycle rule 1 (spec-model-auto-upgrade-2026-07-30 §Scope), mirrored from
    the "Customize this bot" route in ``routes_admin_config.py`` and from
    ``provisioning.seed_model_config_if_empty``: a bot whose file carries
    ``rungs`` is Custom (``primary_bot.bot_has_custom_tiers``), and
    ``model_auto_upgrade.bot_policy`` does NOT inherit the pod's ``enabled``
    for a Custom bot — a Custom bot that never set the key resolves to the CODE
    default (false). So the moment a write leaves ``rungs`` on a file that had
    none, the bot would silently stop riding the latest model version. Carrying
    the pod's current block forward keeps the auto-upgrade posture exactly where
    it was.

    The caller decides WHEN: ``json_full_config_set`` calls this on the on-disk
    not-Custom → Custom transition, whatever payload shape produced it — a bare
    ``tiers`` write on a bot with no file (create), a ``tiers`` write folding
    onto a same-payload wholesale ``rungs`` write, or a wholesale NON-EMPTY
    ``rungs`` replacement with no ``tiers`` key at all (the easy-setup wizard's
    bot scope, #3566 audit E-1). An EMPTY ``rungs`` is the reset and leaves the
    bot not-Custom, so it never reaches here — lifecycle rule 2 stands.

    No pod block, or a file that already has its own, → leave it alone (the bot
    resolves to the code default, same as the pod does).

    What lands is a frozen literal SEED of the pod's whole block, per spec
    lifecycle rule 1 ("inherits the pod's current auto-upgrade value as a
    literal seed") and identical to what ``routes_admin_config``'s Customize
    route and ``provisioning.seed_model_config_if_empty`` already copy. Note
    that is a snapshot, NOT live layering: ``bot_policy`` withholds only
    ``enabled`` from a Custom bot, so seeding the subordinate knobs pins this
    bot's cadence at the pod's value as of the flip. That is the spec'd
    behavior; it is called out here because the wording is easy to misread.

    ``caller_set`` says the ``autoUpgrade`` block currently on ``tiers_file``
    was produced by THIS write's explicit ``autoUpgrade`` key (which the caller
    applies before calling this). Those knobs win, but they merge ON TOP of the
    pod block rather than replacing it — a bot that writes only
    ``{"enabled": false}`` still gets the pod's cadence, the same shape
    ``model_auto_upgrade.bot_policy`` would resolve to. Without
    the flag a caller-supplied block is indistinguishable from the bot's own,
    and an ``autoUpgrade: {}`` (the reset's clear) would leave a bot that this
    same write turned Custom with auto-upgrade resolving to the code default.
    """
    pod_block = updates.get(POD_AUTO_UPGRADE_KEY)
    if not isinstance(pod_block, dict) or not pod_block:
        return
    existing = tiers_file.get("autoUpgrade")
    if not isinstance(existing, dict) or not existing:
        tiers_file["autoUpgrade"] = dict(pod_block)
        return
    if not caller_set:
        return          # the bot's own block — never touch it
    tiers_file["autoUpgrade"] = {**pod_block, **existing}


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


# Transport (``api`` / ``baseUrl``) for provider blocks Evolve registers.
#
# WHY THIS EXISTS. OC never derives ``api`` from the provider id:
# ``resolveProviderRequestPolicyConfig`` returns ``api: params.api``
# unchanged, and every resolution site ends in ``?? "openai-responses"``.
# A provider block that declares only ``models: [...]`` therefore
# dispatches EVERY model under it to the OpenAI Responses transport at
# api.openai.com. OC says so itself when it loads such a block:
#
#     Provider xai, model grok-4-1-fast: no "api" specified.
#     Set at provider or model level.
#
# Observed on the reference pod 2026-07-31: a Google ``AIza…`` key and an
# ``xai-…`` key were both being POSTed to https://api.openai.com/v1/responses
# and 401ing. The 401 masquerades as expired credentials — it is not; the
# credential is fine and pointed at the wrong vendor. Both providers' rungs
# were silently dead fleet-wide, which is how a fallback chain walked past
# two cheap rungs and terminated on Opus (see generate_fallback_list).
#
# WHY THIS TABLE IS DELIBERATELY NOT EXHAUSTIVE. It lists only providers
# whose brokenness AND fix were verified live against the installed OC.
# Two bundled providers are excluded ON PURPOSE:
#
#   anthropic — already resolves ``anthropic-messages`` →
#               api.anthropic.com/v1/messages (verified 200). Nothing to fix;
#               writing a value here could only introduce a regression.
#   openai    — routes through the codex app-server, NOT the HTTP transport
#               (verified: a successful run emits no model-fetch line at all).
#               Pinning api/baseUrl would likely break that routing. The
#               generic ``openai-responses`` default is correct for it anyway.
#
# So absence from this table means "leave OC's own resolution alone", which
# is the fail-safe direction. Add an entry only after verifying the provider
# is actually mis-dispatched and that the entry fixes it on a live pod.
#
# ``baseUrl`` is omitted where OC's bundled static catalog already supplies
# a correct one (google), and set where it does not (xai — without it the
# request goes to api.openai.com even with the right ``api``). Values are
# OC's own: google from buildGoogleStaticCatalogProvider, xai from
# extensions/xai/onboard.ts (XAI_BASE_URL + applyProviderConfig's api arg).
#
# provider-literal-allow-begin: provider transport catalog DATA (three-homes
# rule, spec-model-rungs-and-roles-2026-06-09 §Addendum 3.B home #1)
_OC_PROVIDER_TRANSPORT: dict[str, dict[str, str]] = {
    "google": {"api": "google-generative-ai"},
    "xai": {"api": "openai-responses", "baseUrl": "https://api.x.ai/v1"},
}
# provider-literal-allow-end


# Per-model output cap (``maxTokens``) stamped onto provider-registry
# entries Evolve mints for models it KNOWS.
#
# WHY THIS EXISTS. OC's cap resolution, verified live against the
# installed OC 2026.7.1-2 dist (``resolvePreferredTokenLimit`` in
# ``dist/models-config-*.js``), is **explicit-wins**: an explicit
# positive ``maxTokens`` on the registry entry wins unconditionally; the
# bundled implicit catalog is consulted only when the explicit value is
# absent/invalid; and when NEITHER exists the config default 8192
# (``dist/config-*.js``) applies. (The schema prose at
# ``docs/schemas/oc-config-schema.txt`` "merge" description says
# "higher value between explicit and implicit" — that prose is stale
# relative to the shipped implementation; trust the dist.) Observed
# 2026-08-31: the PoC personal-assistant bot's turn died with
# ``incomplete turn detected ... provider=anthropic/claude-opus-5
# stopReason=length`` — ``claude-opus-5`` has no entry in the installed
# OC's bundled catalog, so the Evolve-minted ``{id, name}`` entry fell
# all the way to 8192. (That bot was hand-patched the same day; this
# catalog supersedes the patch fleet-wide via the repair pass below.)
#
# BECAUSE explicit wins, the stamping rule is asymmetric:
#
#   * KNOWN family → stamp the catalog value. Values sit at or below
#     each family's true cap (the Claude 5 / 4.6+ families support 128K
#     output; haiku-4-5's cap is 64K, and 64000 is also exactly what
#     OC's own modern-Anthropic registration defaults to in
#     ``register.runtime-*.js``, so stamping it is behavior-neutral
#     where the implicit entry exists and the incident fix where it
#     doesn't). Never raise a value above a cap the provider actually
#     enforces — OC sends it as the request's max_tokens, and an
#     over-cap request is a hard API error, worse than truncation.
#     (OC force-raises some families — fable-5/sonnet-5/opus-4-8 — to
#     128000 at resolution time regardless; our value is a floor-keeper
#     there, not a bound.)
#   * UNKNOWN id → stamp NOTHING. An explicit "conservative" value
#     would CLAMP any model OC's implicit catalog knows (e.g. gemini
#     models carry implicit 65536 — explicit 8192 would win over it),
#     re-creating the truncation class this table exists to fix.
#     Absence lets the implicit cap flow; truly-uncataloged unknowns
#     fall to the same 8192 default they get today.
#
# Keys are model-id FAMILY PREFIXES: an id matches on equality or on
# ``<key>-`` (so dated snapshots like ``claude-haiku-4-5-20251001`` land
# on their family). Known limitation: re-prefixed ids
# (``openrouter/anthropic/claude-opus-5`` partitions to model_id
# ``anthropic/claude-opus-5``; bedrock's ``us.anthropic.…``) match no
# family and are left unstamped — the incident class persists on such
# routes until they're taught here.
#
# provider-literal-allow-begin: model output-cap catalog DATA (same
# three-homes rule as the transport table above)
_MODEL_MAX_TOKENS: dict[str, int] = {
    "claude-fable-5": 64000,
    "claude-opus-5": 64000,
    "claude-opus-4-8": 64000,
    "claude-opus-4-7": 64000,
    "claude-opus-4-6": 64000,
    "claude-sonnet-5": 64000,
    "claude-sonnet-4-6": 64000,
    "claude-haiku-4-5": 64000,
}
# provider-literal-allow-end


def _lookup_model_max_tokens(model_id: str) -> int | None:
    """Return the output cap for ``model_id`` (family-prefix match).

    Longest family first, so a more specific family always beats a
    shorter one that happens to prefix it. Returns None for ids the
    catalog doesn't know — the caller must then leave ``maxTokens``
    ABSENT so OC's implicit catalog (or its config default) resolves
    the cap; see the explicit-wins note on ``_MODEL_MAX_TOKENS``.
    """
    for family in sorted(_MODEL_MAX_TOKENS, key=len, reverse=True):
        if model_id == family or model_id.startswith(family + "-"):
            return _MODEL_MAX_TOKENS[family]
    return None


def _valid_max_tokens(value: object) -> bool:
    """True if ``value`` is a schema-valid, operator-respectable cap.

    The OC schema wants ``number, exclusiveMinimum 0``. bool is excluded
    explicitly (``True`` is an ``int`` in Python but ``maxTokens: true``
    is schema-invalid and meaningless as a cap).
    """
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
    )


def _ensure_provider_transport(provider: str, prov_block: dict) -> bool:
    """Backfill ``api``/``baseUrl`` on a provider block. Returns True if changed.

    Additive only: an operator (or a future OC onboarding flow) who set a
    value keeps it. That matters for self-hosted and region-pinned
    deployments, where a hardcoded ``baseUrl`` would be actively wrong —
    we are correcting an ABSENT field, never overriding a chosen one.
    """
    transport = _OC_PROVIDER_TRANSPORT.get(provider)
    if not transport:
        return False
    changed = False
    for field, value in transport.items():
        existing = prov_block.get(field)
        if isinstance(existing, str) and existing.strip():
            continue  # operator/OC already chose — never override
        prov_block[field] = value
        changed = True
    return changed


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
    are filled in by OC at runtime — we don't have to carry them. The
    one field we DO carry — for models in the output-cap catalog only —
    is ``maxTokens``: a model OC's bundled catalog doesn't know lands on
    the 8192 config default without it and truncates long turns, while
    ids WE don't know are deliberately left unstamped so OC's implicit
    cap can win (see ``_MODEL_MAX_TOKENS`` for the 2026-08-31 incident
    and the explicit-wins resolution order).

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
    (non-string / empty string), and — for KNOWN-family ids —
    ``maxTokens`` from the output-cap catalog when missing or
    non-positive/non-numeric (operator-set positive values are
    preserved; a schema-invalid value on an unknown-family id is removed
    rather than guessed). Without this, an entry left behind by a
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
            # Backfill the output cap for KNOWN families: absent (the
            # pre-catalog {id, name} shape every deployed bot carries) or
            # schema-invalid gets the catalog value; an operator-set
            # positive number — the 2026-08-31 hand-patch included — is
            # preserved. This is what heals the fleet on its next deploy.
            # Unknown-family ids are never given a value (explicit-wins:
            # a stamp would clamp implicitly-cataloged models); a
            # schema-invalid value on one is REMOVED instead, restoring
            # the absence that lets OC's implicit catalog resolve it.
            existing_cap = entry.get("maxTokens")
            if not _valid_max_tokens(existing_cap):
                known_cap = _lookup_model_max_tokens(entry_id)
                if known_cap is not None:
                    entry["maxTokens"] = known_cap
                elif "maxTokens" in entry:
                    del entry["maxTokens"]

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
        # Minimum viable shape per OC schema is {id, name}; for models in
        # the output-cap catalog we add ``maxTokens``, because a model
        # absent from OC's bundled implicit catalog otherwise pins to the
        # 8192 config default — while UNKNOWN ids are left unstamped so
        # the implicit cap can flow (explicit-wins; see
        # _MODEL_MAX_TOKENS). name=id is the safest default — OC's merge
        # mode fills in display details from the bundled catalog.
        # Operators who want a prettier display name or a different cap
        # can edit the entry directly; this function preserves their edit.
        #
        # NOTE: merge mode covers model-level DISPLAY metadata only. The
        # transport (``api``/``baseUrl``) is provider-level and is NOT
        # inherited from OC's bundled catalog — see the transport pass
        # below, which is what keeps this registration from silently
        # dispatching the provider to api.openai.com.
        entry: dict = {"id": model_id, "name": model_id}
        known_cap = _lookup_model_max_tokens(model_id)
        if known_cap is not None:
            entry["maxTokens"] = known_cap
        models_list.append(entry)

    # Transport pass — every provider block we registered above (and any
    # pre-existing one) needs an explicit ``api``, because declaring a
    # provider block suppresses nothing but inherits nothing either: OC
    # falls through to ``"openai-responses"`` and ships the provider's key
    # to api.openai.com. Runs over every block, not just newly-created
    # ones, so bots registered by an earlier Evolve version self-heal on
    # their next deploy rather than needing a migration.
    for provider, prov_block in providers.items():
        if isinstance(prov_block, dict):
            _ensure_provider_transport(provider, prov_block)


def get_tiers(bot: str) -> dict:
    """Return tier definitions for the current bot user from ~/.openclaw/evolve-tiers.json.

    Speaks both shapes: a new rungs/roles file is projected back to the legacy
    ``{tierN: {models}}`` view via :func:`synthesize_legacy_tiers` so every
    legacy-shaped consumer (the UI render path, ``generate_fallback_list``)
    sees the migrated allocations instead of the empty dict the bare
    ``.get("tiers")`` returned for migrated files (the "erased" UI bug).
    """
    return synthesize_legacy_tiers(_load_tiers_file(bot))


# Bots already warned about a poisoned routing block (see get_routing).
_WARNED_POISONED_ROUTING: "set[str]" = set()


def project_routing_to_tier_view(stored: dict) -> dict:
    """Return ``stored`` with each ``<x>Role`` projected onto ``<x>Tier``.

    Every PYTHON consumer of the routing block (the admin UI's routing card,
    heal, models, the cascade audit runner, efficiency_hawk, the arbiter's
    tier_adjustment applier) speaks the legacy ``<x>Tier`` names. The PLUGIN
    reads the file itself and resolves ``maintenanceRole ?? maintenanceTier``
    — Role wins — and ``migrate-model-roles`` rewrote ``*Tier`` → ``*Role`` on
    every bot it touched.

    Without this projection, a migrated bot renders the ``DEFAULT_ROUTING``
    tier (``tier3``) in the card while actually routing to whatever role is on
    disk — the card shows a value the operator never chose, and saving it
    writes that fiction back. Projecting makes the tier view TRUE, which is
    what makes the writer's slot eviction a faithful round-trip instead of a
    clobber, and it means a read-modify-write client posts a single-generation
    body the endpoint accepts.

    The ``*Role`` key is dropped from the projected view: one generation out,
    one generation in. A role with no legacy tier equivalent (``max``, or a
    hand-edited value outside the canonical set) cannot be projected — it is
    left as-is so the real value stays visible, and :func:`get_routing` then
    SUPPRESSES the defaulted ``*Tier`` for that slot. Emitting both halves
    would hand clients a view their own PUT refuses (``validate_routing_update``
    treats both-halves as ambiguous) and would suppress the writer's slot
    eviction, silently no-opping a read-modify-write caller.
    """
    view = dict(stored)
    for role_key, tier_key in ROUTING_SLOT_PAIRS:
        if role_key not in view:
            continue
        role = view[role_key]
        if role is None:
            view.pop(role_key)
            view[tier_key] = None
            continue
        tier = _ROLE_TO_TIER.get(role) if isinstance(role, str) else None
        if tier is None:
            continue  # unprojectable (e.g. "max") — leave the role visible
        view.pop(role_key)
        view[tier_key] = tier
    return view


def get_routing(bot: str) -> dict:
    """Return routing config for bot, falling back to defaults.

    The returned view is TIER-shaped: a stored ``<x>Role`` is projected onto
    ``<x>Tier`` by :func:`project_routing_to_tier_view` so the admin card and
    every other python consumer see what the bot actually routes to rather
    than the code default. See that function for why this matters.

    Defensive against an ALREADY-POISONED file: pods that took a bad write
    under the pre-#3566-E-3 endpoint have a non-dict ``routing`` value on
    disk, and the bare ``result.update(stored)`` raised ValueError/TypeError
    on every read from then on — including the read-back at the end of
    ``json_full_config_set``, so even the repairing write returned 500.
    A non-dict block is now ignored (the code defaults apply, which is the
    same thing an absent block gets) and the next valid write replaces it
    with a clean dict. Note the safe direction: ignoring the poison yields
    ``enabled=True`` rather than a wedged pod — the operator's kill-switch
    intent, if they ever set one, is not recoverable from a poisoned value,
    so the routing card must be re-saved after a heal.
    """
    stored = _load_tiers_file(bot).get("routing", {})
    if not isinstance(stored, dict):
        # Warn once per bot per process: get_routing is on the config-GET and
        # deploy paths, so an unconditional print would be per-request noise
        # until the block is repaired.
        if bot not in _WARNED_POISONED_ROUTING:
            _WARNED_POISONED_ROUTING.add(bot)
            print(
                f"[oc_model] ignoring non-dict routing block for {bot} "
                f"({type(stored).__name__}) — re-save the routing card to repair",
                file=sys.stderr,
            )
        stored = {}
    view = project_routing_to_tier_view(stored)
    result = dict(DEFAULT_ROUTING)
    result.update(view)
    # A slot whose role could not be projected keeps its ``*Role`` key — so
    # drop the DEFAULT ``*Tier`` that would otherwise sit beside it. The view
    # must never name both halves of one slot: that is the shape the endpoint
    # rejects as ambiguous and the shape that suppresses the writer's slot
    # eviction (a read-modify-write caller would then silently no-op).
    for role_key, tier_key in ROUTING_SLOT_PAIRS:
        if role_key in view:
            result.pop(tier_key, None)
    return result


def get_fallback_config(bot: str) -> dict:
    """Return fallbackMode and tierCascade for bot."""
    tf = _load_tiers_file(bot)
    return {
        "fallbackMode": tf.get("fallbackMode", "static"),
        "tierCascade": tf.get("tierCascade", DEFAULT_TIER_CASCADE[:]),
    }


# -- Fallback list generation -------------------------------------------------

def _failover_affinity_key(model_id: object) -> str | None:
    """Affinity-grouping key for the provider-affinity partition below.

    ``anthropic/claude-…`` → ``anthropic``. For a multi-segment ref
    (``openrouter/anthropic/claude-…``) the key is the SECOND segment:
    OC parses the first segment as the provider, but for a re-hosted ref
    that segment is a transport, and the behavioral dialect the partition
    protects (tool-schema strictness, sentinel conventions) is the hosted
    vendor's — grouping by transport would hoist a proxied cross-family
    model over the operator's same-rung peers (Pass-2 review finding on
    #3910). Structural rule, no provider table (three-homes rule).

    ``None`` for a bare un-prefixed id, an empty-prefix id (``/x``), or a
    non-string entry (hand-edited legacy tiers can carry those; they must
    not crash a deploy pass) — affinity is unknowable, so they group with
    no vendor.
    """
    if not isinstance(model_id, str):
        return None
    segs = [s for s in model_id.split("/") if s]
    if len(segs) < 2:
        return None
    return (segs[1] if len(segs) >= 3 else segs[0]).lower()


def generate_fallback_list(tiers: dict, cascade: list[str]) -> list[str]:
    """Compute the flat fallback list from tier definitions and cascade order.

    Concatenates each tier's model list in cascade order, deduplicating while
    preserving first-occurrence order. Tier 0 (the legacy judge slot) is
    excluded from the cascade -- read-compat for an on-disk ``tierCascade``
    that still names it (the judge role is gone; nothing writes tier0).

    **Fallback degrades; it never escalates.** Tiers costlier than the one
    that supplied ``primary`` (result[0]) are dropped from the tail. A
    fallback fires because the previous candidate *failed*, and OC's
    failure taxonomy is dominated by causes a pricier model does not fix:
    ``stopReason=length`` (output ceiling) and provider auth 401s both
    surface as ``candidate_failed`` and walk the chain. Escalating there
    buys no additional chance of success -- only a bigger bill, charged at
    the exact moment the context is largest because every prior rung
    re-primed the cache from cold.

    That is not hypothetical: on 2026-07-31 a personal-bot on the
    reference pod walked sonnet-4-6 -> gemini-3.1-pro (401) -> haiku-4-5
    -> gemini-3-flash (401) -> **opus-4-8**, and the terminal Opus turn
    alone cost $11.32 of a $14.06 session (2.57M cache-read + 144k
    cache-write). The same turn on the Sonnet primary would have been
    ~$2.26. Both triggering failures were `stopReason=length` from a long
    report -- a ceiling Opus shares.

    Cost class comes from the tier id via ``_TIER_COST_CLASS`` /
    ``_RUNG_COST_RANK``, so no costClass plumbing through
    ``synthesize_legacy_tiers`` (which emits only ``{"models": [...]}``)
    is needed. A tier contributing no *new* models neither sets nor
    constrains the ceiling.

    This filter lives in the shared helper on purpose: deploy-time
    propagation, the AI-Optimization save path, ``json_full_config``, and
    audit's tier-drift detector all derive through here. Filtering in any
    one of them alone would make audit flag every bot as permanently
    drifted against the others.

    Operator intent still wins -- a power bot whose explicit
    ``tierCascade`` leads with tier1 gets the full chain, because the
    ceiling is set by the *primary's* tier, not by a hardcoded rung.

    **Fallback degrades within the primary's provider before it crosses
    providers.** After the ceiling filter, the tail is stable-partitioned:
    models sharing the primary's provider prefix come first (in cascade
    order, i.e. walking DOWN the primary provider's own ladder), then
    everything else in its original order. A failover hop to a same-rung
    peer from another provider is a *behavior* change, not just a model
    swap: on 2026-08-31 a single sonnet API error walked the PoC
    personal-assistant bot onto its cross-provider peer (grok-4)
    mid-conversation, where it re-issued the same gmail_send call ~16
    times while claiming schema compliance in prose (contained below the
    LLM by #3907; design:
    internal/design-failover-provider-affinity-2026-08-31.md). The
    within-provider rung below keeps the conventions the session was
    built on — tool-schema dialect, sentinel/silence contracts — at the
    accepted cost axis (capability), and a provider-WIDE outage still
    reaches the cross-provider tail one cheap hop later. The partition
    is stable both sides, so an operator's within-tier reorder survives
    as relative order; a bare un-prefixed primary skips the partition
    (affinity unknowable). Re-hosted refs group by the hosted VENDOR, not
    the transport (``openrouter/anthropic/claude-x`` ↔
    ``anthropic/claude-haiku`` are affine — see
    :func:`_failover_affinity_key`). ``primary`` itself NEVER moves
    (PR #1765 lesson).

    Example:
        tiers = {
            "tier2": {"models": ["a/sonnet", "o/gpt-4o"]},
            "tier3": {"models": ["a/haiku", "o/gpt-4o-mini"]},
            "tier1": {"models": ["a/opus"]},
        }
        cascade = ["tier2", "tier3", "tier1"]
        -> ["a/sonnet", "a/haiku", "o/gpt-4o", "o/gpt-4o-mini"]
           (tier1 dropped: "high" outranks the "medium" primary tier2;
            a/haiku hoisted ahead of the cross-provider peers)

        cascade = ["tier1", "tier2", "tier3"]     # power-first operator choice
        -> ["a/opus", "a/sonnet", "a/haiku", "o/gpt-4o", "o/gpt-4o-mini"]
           (nothing outranks a "high" primary -- full chain preserved)
    """
    seen: set[str] = set()
    result: list[str] = []
    primary_rank: int | None = None
    for tier_id in cascade:
        if tier_id == "tier0":
            continue  # legacy judge slot excluded from cascade (read-compat)
        tier = tiers.get(tier_id, {})
        if not isinstance(tier, dict):
            continue
        fresh = [m for m in (tier.get("models") or []) if m and m not in seen]
        if not fresh:
            # Empty or fully-duplicate tier contributes nothing, so it must
            # not become the rank ceiling -- otherwise an empty leading tier
            # would pin the ceiling below the model that actually lands as
            # primary.
            continue
        rank = _RUNG_COST_RANK.get(_TIER_COST_CLASS.get(tier_id, "medium"), 1)
        if primary_rank is None:
            primary_rank = rank  # first contributing tier supplies `primary`
        elif rank > primary_rank:
            continue  # costlier than primary — never escalate on failure
        for m in fresh:
            seen.add(m)
            result.append(m)
    # Provider-affinity partition (see docstring). Applied last so it can
    # never resurrect a model the ceiling filter dropped.
    if len(result) > 2:
        primary_key = _failover_affinity_key(result[0])
        if primary_key is not None:
            tail = result[1:]
            affine = [m for m in tail if _failover_affinity_key(m) == primary_key]
            if affine and len(affine) < len(tail):
                others = [m for m in tail if _failover_affinity_key(m) != primary_key]
                result = [result[0], *affine, *others]
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
        tiers         -- dict: stored in ~/.openclaw/evolve-tiers.json.
                         A legacy-shaped ``{tierN: {models}}`` PER-TIER edit.
                         Sent together with ``rungs``/``roles``, it is applied
                         ON TOP of that wholesale replacement — the replace runs
                         first, then this folds into the resulting rung set
                         (#3566 follow-up; it used to be discarded).
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
        autoUpgrade   -- dict: stored in ~/.openclaw/evolve-tiers.json
                         (per-bot automatic version-upgrade policy,
                          spec-model-auto-upgrade-2026-07-30 §Config shape.
                          Partial updates merge into the existing block;
                          an EMPTY dict clears it — the reset-to-pod path.)
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
        podAutoUpgrade -- dict: NOT a config field. Advisory pod context
                         (``network.json::models.autoUpgrade``) injected by
                          ``oc_cli``; consumed only when this write flips the
                          bot from Use-pod-defaults to Custom, i.e. leaves
                          ``rungs`` on a file that had none — whether via a
                          ``tiers`` edit or a wholesale non-empty ``rungs``
                          replacement (#3566 audit E-1). An EMPTY ``rungs``
                          (the reset) never consumes it.
                          See :data:`POD_AUTO_UPGRADE_KEY`.

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

    Returns the full post-write config (:func:`json_full_config`), plus
    ``routingKeysRefused`` — the sorted routing keys this write DROPPED
    (unknown key, a value the whitelist rejects, or a ``*Tier`` key naming a
    tierN with no role mapping — whether sent by the caller or found on disk
    by the legacy-tier self-heal), or ``["routing"]`` when the whole block
    was a non-dict. The key is ABSENT when nothing was
    refused, so the happy-path shape is unchanged. Callers doing a
    read-modify-write of the routing block must look at it (or verify the
    post-write state themselves): a refusal is not a failure, the write
    still returns a success-shaped result, and a caller that only checks for
    ``None`` will report success for a write that landed nothing.
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
    # Pre-write snapshot for the drift declaration (D3). Deep-copied because
    # every branch below mutates ``tiers_file`` in place, and the declaration
    # must describe what LANDED — including keys no caller named (the
    # ``_carry_pod_auto_upgrade`` seed, a legacy ``tiers`` key normalize folds
    # away). See TIERS_DRIFT_PREFIX.
    _tiers_before = copy.deepcopy(tiers_file)
    _tiers_keys_written: list[str] = []
    tiers_changed = False

    # Was the bot Custom BEFORE this write? ``primary_bot.bot_has_custom_tiers``
    # decides purely on ``rungs`` presence, so this is the on-disk Custom flag —
    # captured before anything below mutates the file. The auto-upgrade carry at
    # the end of the tiers block keys off the not-Custom → Custom transition
    # (lifecycle rule 1), which is now reachable from more than the create
    # branch: a same-payload wholesale ``rungs`` write establishes the rung set
    # first, so the tiers edit that follows folds instead of creating.
    _had_rungs_on_entry = _file_is_new_shape(tiers_file)

    # Wholesale rungs/roles writes — the per-bot Use-defaults/Custom toggle
    # (spec-model-rungs-and-roles §Addendum 5). Unlike the ``tiers`` key below
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
    #
    # ORDERING (#3566 follow-up): this block runs BEFORE the ``tiers`` block, so
    # that a payload carrying BOTH lands the per-tier edit ON TOP of the
    # replacement set rather than having the replacement discard it. It used to
    # run after, and the wholesale replace silently dropped the tier edit —
    # latent (no in-repo caller sends both keys) but a real edit-loss in the one
    # write path every tier change goes through. See the ``tiers`` block below
    # for the stale-legacy-key consequence of the swap.
    #
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

    # Did a replacement actually LAND? Key presence is not enough: an empty (or
    # junk) value clears rather than replaces, and the stale-legacy drop below
    # keys off a real replacement having happened.
    _wholesale_landed = False
    # Narrower sibling of ``_wholesale_landed``: did a NON-EMPTY ``rungs``
    # replacement land? That — not a ``tiers`` key — is what can flip the bot to
    # Custom, so it is the auto-upgrade carry's trigger (#3566 audit E-1). A
    # roles-only write can't flip anything, and an EMPTY rungs list is the reset
    # (lifecycle rule 2), so neither sets this. Narrower than strictly
    # necessary at the carry gate — the other two conjuncts there already
    # exclude both cases — and kept as defence in depth, so no test can
    # demonstrate it apart from ``_wholesale_landed``.
    _wholesale_rungs_landed = False
    if "rungs" in updates:
        new_rungs = updates["rungs"]
        if isinstance(new_rungs, list) and new_rungs:
            # Deep-copy: the tiers fold below appends rungs and rewrites their
            # models[] in place, and aliasing the caller's list would mutate the
            # ``updates`` dict it was handed. Harmless in production (oc_cli
            # serializes to JSON across the subprocess boundary) but a landmine
            # for any in-process caller — and only reachable at all since the
            # reorder put the fold AFTER this write.
            tiers_file["rungs"] = copy.deepcopy(new_rungs)
            _wholesale_landed = True
            _wholesale_rungs_landed = True
        else:
            tiers_file.pop("rungs", None)
        tiers_changed = True
    if "roles" in updates:
        new_roles = updates["roles"]
        if isinstance(new_roles, dict) and new_roles:
            tiers_file["roles"] = copy.deepcopy(new_roles)
            _wholesale_landed = True
        else:
            tiers_file.pop("roles", None)
        tiers_changed = True
    if "roleCaps" in updates:
        new_caps = updates["roleCaps"]
        if isinstance(new_caps, dict) and new_caps:
            tiers_file["roleCaps"] = new_caps
        else:
            tiers_file.pop("roleCaps", None)

    # Rule-1 eligibility for the auto-upgrade carry at the end of the tiers
    # block: was the bot NOT Custom at some point during this write? Either it
    # was not Custom on disk, or a wholesale reset in this same payload just
    # cleared its rungs (and the tiers edit is about to re-create them).
    _non_custom_at_some_point = (
        not _had_rungs_on_entry or not _file_is_new_shape(tiers_file)
    )

    if "tiers" in updates:
        if _wholesale_landed and _file_is_new_shape(tiers_file):
            # A wholesale replace just landed in this same payload. Any legacy
            # ``tiers`` key still on the file describes the PRE-replacement rung
            # set, so it must not survive: normalize_tiers_file_shape() folds a
            # legacy sibling into the rungs at save time, which would put those
            # stale allocations back on top of the operator's per-tier edit
            # below. Dropping it here matches what the preserve branch has
            # always done with a legacy file (replace the key outright, never
            # merge into it) — the update is the authoritative legacy-shaped
            # intent. Gated on a replacement having actually LANDED, not on the
            # key being present: an empty/junk ``rungs`` or ``roles`` replaces
            # nothing, so a mixed file's own allocations must still fold.
            tiers_file.pop("tiers", None)

        # Guard against the silent rung-collision clobber FIRST, for whichever
        # branch below runs: if the update names two legacy tiers that fold to
        # one rung with different models (a hand-authored roles map can point
        # two roles at one rung), the fold is
        # last-writer-wins and one edit vanishes while the write still reports
        # success. Refuse the write with a structured, operator-fixable error
        # instead (model-tiers false-success, 2026-06-27). The sentinel prefix
        # lets the admin endpoint map it to a 409 rather than a generic 500.
        #
        # Hoisted above the three-way split (#3566 follow-up) because a
        # wholesale ``roles`` replacement can now land ahead of the fold and
        # steer a CREATE through a colliding map. detect_rung_collisions returns
        # [] for a file with neither rungs nor roles, so the legacy-preserve and
        # ordinary create paths are unaffected.
        _collisions = detect_rung_collisions(tiers_file, updates["tiers"])
        if _collisions:
            raise ValueError(
                RUNG_COLLISION_PREFIX + format_rung_collision_error(_collisions)
            )

        if _file_is_new_shape(tiers_file):
            # New-shape file: fold the legacy-shaped update into the rung
            # clusters the mapped roles point at (creating role/rung if the
            # operator is configuring a tier the file hasn't adopted yet).
            # Writing a sibling ``tiers`` key would be ignored by the gateway
            # loader — the freshness-advisory "apply" cosmetic-write bug.
            apply_tiers_update_new_shape(tiers_file, updates["tiers"])
        elif isinstance(tiers_file.get("tiers"), dict) and tiers_file["tiers"]:
            # PRESERVE. Legacy-only file that ALREADY EXISTS: keep writing the
            # legacy shape — we don't half-migrate on a partial config-set
            # write. `evolve-admin migrate-model-roles` is the whole-file
            # conversion, and it is the operator's call to run it.
            #
            # NOTE (#3566): this comment used to claim "the deploy-time
            # migrate-model-roles pass converts it to the new shape". There is
            # no such pass. `evolve-admin migrate-model-roles` is a MANUAL
            # command with no caller in deploy or anywhere else, so a bot that
            # is legacy-shaped here stays legacy-shaped indefinitely — every
            # subsequent write just re-affirms it. That false premise is why
            # two bots sat on the deprecated shape for eight weeks after the
            # June fleet migration, still logging the ModelRouter DEPRECATION
            # line. Shape-preservation here is correct; the gap is that
            # nothing ever runs the migrator.
            tiers_file["tiers"] = updates["tiers"]
        else:
            # CREATE. No tier definitions on disk at all (absent file, or a
            # file carrying only siblings like ``cascade``/``routing``). There
            # is nothing to preserve, so a file must never be BORN on the
            # deprecated shape — the branch above used to fall through to here
            # and mint one from scratch, which is how an operator editing a
            # single tier on a never-seeded bot created a legacy config in
            # 2026 (#3566 follow-up; the seed producer was fixed in #3567).
            #
            # ``legacy_tiers_for_create`` + ``apply_tiers_update_new_shape``
            # against the empty dict IS the tier→rung conversion: it creates
            # each role and its canonically-named rung at the cost-ordered
            # position with ``costClass``.
            # Its output equals ``migrate_model_roles.migrate_evolve_tiers`` of
            # the same payload — i.e. exactly what `migrate-model-roles
            # --apply` would have produced — for every tier-key ordering, and
            # including the per-tier ``fallbacks`` and ``maxPerDayPerBot``
            # forms that only the migrator used to handle. That equality is
            # pinned by a test rather than asserted here, and it is what makes
            # this conversion behaviour-preserving instead of a silent re-tune.
            # (It is also strictly more complete on this call surface: the
            # migrator drops the ``max`` role key, which the per-bot Tier
            # Definitions panel can send.)
            _create_tiers, _create_caps = legacy_tiers_for_create(updates["tiers"])
            apply_tiers_update_new_shape(tiers_file, _create_tiers)
            if _file_is_new_shape(tiers_file):
                if _create_caps and "roleCaps" not in updates:
                    tiers_file["roleCaps"] = _create_caps
            elif isinstance(updates["tiers"], dict) and updates["tiers"]:
                # A non-empty payload that folded to nothing means every key in
                # it was uninterpretable (unknown tier id, non-list models). We
                # deliberately do NOT fall back to writing the legacy key: that
                # would remint the exact shape this branch exists to stop
                # minting, for a payload that carries no usable allocation
                # anyway. The routes' post-write "did it land?" guards turn the
                # no-op into an operator-visible error; this line makes it
                # visible in the daemon log too.
                print(
                    f"[oc_model] tiers update for {bot} named no known tier "
                    f"({sorted(updates['tiers'])}) — nothing written",
                    file=sys.stderr,
                )
        tiers_changed = True

    # Routing keys this write refused (unknown key / bad value). Surfaced in
    # the return value so a caller can tell "written" from "silently dropped".
    _routing_refused: list[str] = []

    if "routing" in updates:
        # PARTIAL MERGE with an explicit key whitelist — same shape as the
        # cascade / userTierOverride blocks below (#3566 audit E-3). The old
        # wholesale ``tiers_file["routing"] = updates["routing"]`` had two
        # failure modes:
        #   1. A partial write silently cleared ``routing.enabled=false`` — the
        #      OC-2026.7 kill-switch. Every consumer defaults absent-to-enabled
        #      (DEFAULT_ROUTING above; ``routing?.enabled !== false`` in the
        #      plugin's ModelRouter), so dropping the key RE-ENABLES routing.
        #   2. A non-dict value was persisted and then wedged get_routing().
        # The endpoint rejects a malformed body with a 400 via
        # validate_routing_update(); this is the storage-layer safety net for
        # non-endpoint callers (e.g. the arbiter's tier_adjustment applier).
        #
        # Merging is per-SLOT, not per-key: writing ``maintenanceTier`` evicts
        # ``maintenanceRole`` (and vice versa), because the plugin resolves
        # ``maintenanceRole ?? maintenanceTier``. Keeping both would let a
        # stale role from ``migrate-model-roles`` permanently shadow the
        # operator's routing-card edit — a silent no-op Save with no UI repair
        # path. Everything NOT part of the incoming slots still merges.
        #
        # Whatever this block refuses is reported back to the caller in the
        # return value (``routingKeysRefused``) as well as on stderr. The
        # stderr line alone is not a signal an in-process caller can act on —
        # it is how the arbiter's revert path came to report ok=True on a
        # write the whitelist had emptied.
        existing_routing = tiers_file.get("routing")
        if "routing" in tiers_file and not isinstance(existing_routing, dict):
            # Heal a file poisoned by the old wholesale write: drop the
            # non-dict so get_routing() stops raising, then merge onto {}.
            existing_routing = {}
            tiers_file["routing"] = existing_routing
        incoming_routing = updates["routing"]
        if isinstance(incoming_routing, dict):
            if not isinstance(existing_routing, dict):
                existing_routing = {}
            _accepted: dict = {}
            _dropped: list[str] = []
            for _k, _v in incoming_routing.items():
                _spec = ROUTING_KEY_VALIDATORS.get(_k)
                if _spec is not None and _spec[0](_v):
                    _accepted[_k] = _v
                else:
                    _dropped.append(_k)
            # A body carrying BOTH halves of a slot: the endpoint refuses it
            # (validate_routing_update), so it can only reach here from an
            # in-process read-modify-write caller — the arbiter's
            # tier_adjustment applier, which reads the projected view and sets
            # ``<x>Tier`` on it. When the view carried an unprojectable
            # ``<x>Role`` (``max``), the applier's body then names both halves,
            # and without this the eviction below is suppressed, the stale role
            # keeps winning ``Role ?? Tier`` in the plugin, and the applier
            # reports ok=True on a write that changed nothing. The half the
            # caller ADDED is the intent; the echoed Role is stale context — so
            # the Tier wins and the Role is evicted.
            for _role_key, _tier_key in ROUTING_SLOT_PAIRS:
                if _tier_key in _accepted and _role_key in _accepted:
                    _accepted.pop(_role_key)
            # Legacy-tier translation at the write boundary (#3662 review
            # blocker): the plugin runtime now REFUSES a routing block that
            # carries any ``*Tier`` key (LegacyTierShapeError — presence, not
            # value, is what throws), so persisting one would take the bot's
            # router down on the next plugin load. Callers may still SEND the
            # tier shape (an older cached SPA tab, the tier_adjustment
            # applier's tier-shaped projected view) — accept it, but write the
            # ROLE slot. ``_is_tier_id_or_none`` admits any tierN but only
            # tier0-3 map; an unmapped tierN (tier7) is a REFUSAL — reported
            # like any other dropped key, never a silent null-role write.
            for _role_key, _tier_key in ROUTING_SLOT_PAIRS:
                if _tier_key in _accepted:
                    _tv = _accepted.pop(_tier_key)
                    if _tv is not None and _tv not in _TIER_TO_ROLE:
                        _dropped.append(_tier_key)
                    else:
                        _accepted[_role_key] = (
                            None if _tv is None else _TIER_TO_ROLE[_tv]
                        )
            for _k, _v in _accepted.items():
                _sibling = ROUTING_SLOT_SIBLING.get(_k)
                if _sibling and _sibling not in _accepted:
                    existing_routing.pop(_sibling, None)
                existing_routing[_k] = _v
            # Self-heal: a block persisted before the boundary translation may
            # still carry a ``*Tier`` key on disk, and the runtime refuses the
            # WHOLE block on sight of one — so translate it out on any routing
            # write, whichever slot that write was about. An unmapped tierN is
            # still stripped (presence is what the runtime refuses) but its
            # value is unrepresentable — leave the role slot alone and report
            # the loss instead of minting an explicit null role from garbage.
            for _role_key, _tier_key in ROUTING_SLOT_PAIRS:
                if _tier_key in existing_routing:
                    _tv = existing_routing.pop(_tier_key)
                    if _tv is not None and _tv not in _TIER_TO_ROLE:
                        _dropped.append(_tier_key)
                    elif _role_key not in existing_routing:
                        existing_routing[_role_key] = (
                            None if _tv is None else _TIER_TO_ROLE[_tv]
                        )
            if _dropped:
                # Same "we wrote less than you asked for" honesty the tiers
                # fold path prints — a silent drop is how a caller ends up
                # believing a write landed. ``set``: the same slot can be
                # refused twice (bad incoming value AND unmapped on-disk
                # residue).
                _routing_refused = sorted(set(_dropped))
                print(
                    f"[oc_model] routing keys not written for {bot} "
                    f"(unknown key or bad value): {', '.join(_routing_refused)}",
                    file=sys.stderr,
                )
            # Don't mint an empty ``routing: {}`` on a file that never had one
            # just because a caller sent an empty (or fully rejected) update.
            if _accepted or "routing" in tiers_file:
                tiers_file["routing"] = existing_routing
        else:
            # Non-dict update — nothing is written (the endpoint 400s this via
            # validate_routing_update; this is the storage-layer safety net).
            # Say so rather than returning a success-shaped result for a write
            # that did nothing at all.
            print(
                f"[oc_model] routing block not written for {bot} — expected a "
                f"JSON object, got {type(incoming_routing).__name__}",
                file=sys.stderr,
            )
            _routing_refused = ["routing"]

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

    if "autoUpgrade" in updates:
        # Per-bot auto-upgrade policy (spec-model-auto-upgrade-2026-07-30
        # §Config shape) — a Custom bot's own toggle, sibling of cascade in
        # evolve-tiers.json. Partial merge with a key whitelist (same pattern
        # as userTierOverride); an empty dict CLEARS the block — the "Reset to
        # pod defaults" path, lifecycle rule 2: the bot goes back to following
        # the pod in full.
        _ALLOWED_AUTO_UPGRADE_KEYS = {
            "enabled", "applyDay", "requireCostNonRegression",
            "requireGA", "minVisibleDays",
        }
        incoming_au = updates["autoUpgrade"]
        if isinstance(incoming_au, dict) and not incoming_au:
            tiers_file.pop("autoUpgrade", None)
        elif isinstance(incoming_au, dict):
            existing_au = tiers_file.get("autoUpgrade") or {}
            if not isinstance(existing_au, dict):
                existing_au = {}
            for k, v in incoming_au.items():
                if k in _ALLOWED_AUTO_UPGRADE_KEYS:
                    existing_au[k] = v
            tiers_file["autoUpgrade"] = existing_au

    if (
        ("tiers" in updates or _wholesale_rungs_landed)
        and _non_custom_at_some_point
        and _file_is_new_shape(tiers_file)
    ):
        # This write flipped the bot to Custom (``bot_has_custom_tiers`` == rungs
        # present), and ``model_auto_upgrade.bot_policy`` does NOT inherit the
        # pod's ``enabled`` for a Custom bot — so carry the pod's current block
        # forward or the bot silently stops riding the latest model version
        # (lifecycle rule 1; see _carry_pod_auto_upgrade).
        #
        # The trigger is "this write left ``rungs`` on a file that had none",
        # NOT "the payload carried a ``tiers`` key" (#3566 audit E-1). Gating on
        # ``tiers`` missed the easy-setup wizard's bot scope, which writes
        # ``{"rungs", "roles", "roleCaps"}`` wholesale with no ``tiers`` key —
        # a fourth carry site that flipped bots to Custom with auto-upgrade
        # resolving to the code default (false) while the pod said true.
        # ``_wholesale_rungs_landed`` is deliberately the NON-EMPTY-rungs flag:
        # a reset payload (``rungs: []``) clears the key instead, leaving the
        # bot not-Custom, and re-seeding a pod block there would undo lifecycle
        # rule 2. (``_file_is_new_shape`` below is a second, independent guard
        # on that same case.)
        #
        # ``_non_custom_at_some_point`` is now load-bearing in a way it was not
        # before: a wholesale rungs write on an ALREADY-Custom bot (the
        # migrate-model-roles residue cohort — Custom, no ``autoUpgrade`` key,
        # resolving to the code default) reaches this branch for the first
        # time, and that conjunct is the only thing stopping the pod's
        # ``enabled: true`` from being seeded onto it. Pinned by
        # test_migration_residue_bot_is_not_silently_switched_on.
        #
        # Placed AFTER the autoUpgrade merge, not inside the create branch where
        # #3566 first put it, because of two things the reorder changed:
        #   - the flip now also happens in the FOLD branch, when a same-payload
        #     wholesale ``rungs`` write established the rung set first;
        #   - a reset payload (``rungs: []`` + ``autoUpgrade: {}``) carrying a
        #     tiers edit un-Customs the bot and then the edit re-Customs it. Run
        #     before the merge, the carry's seed was wiped by that empty clear
        #     and the bot ended Custom with auto-upgrade resolving to the CODE
        #     default (false) while the pod said true — the precise regression
        #     this carry exists to prevent.
        # ``caller_set`` keeps #3566's contract that an explicit ``autoUpgrade``
        # merges ON TOP of the pod block rather than replacing it.
        _carry_pod_auto_upgrade(
            tiers_file, updates, caller_set="autoUpgrade" in updates,
        )

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
    if any(k in updates for k in TIERS_UPDATE_KEYS):
        normalize_tiers_file_shape(tiers_file)
        # Diff AFTER normalize, BEFORE save: this is the exact set of top-level
        # keys the on-disk file gains/loses/changes, i.e. the exact set
        # ``heal.detect_backup_drift_keys`` would report as ``tiers:<key>``
        # drift once the write lands.
        _tiers_keys_written = changed_top_level_keys(_tiers_before, tiers_file)
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

    result = json_full_config(bot, path)
    if _routing_refused:
        # Only present when something was refused, so the happy-path shape is
        # unchanged for every existing consumer of this dict.
        result["routingKeysRefused"] = _routing_refused
    if _tiers_keys_written:
        # Same convention: absent when this write changed nothing in
        # evolve-tiers.json, so an idempotent re-write declares nothing and the
        # happy-path shape is unchanged. Call sites feed it through
        # ``tiers_drift_declarations`` into their audit entry's ``oc_keys``.
        result[TIERS_KEYS_WRITTEN_FIELD] = _tiers_keys_written
    return result


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
