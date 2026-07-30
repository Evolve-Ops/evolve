"""Materializer for the ``plugins.entries.evolve.config.*`` block in
each bot's ``openclaw.json``.

Phase 3 of ``docs/spec-openclaw-json-derived-artifact-2026-05-24.md``.

Why this exists
---------------
PR #1525 (2026-05-24) removed ``reportingEnabled`` from the evolve
plugin's strict ``additionalProperties: false`` schema. Six bots' on-disk
``openclaw.json`` still carried the key from earlier deploys; the next
gateway reload rejected the config and crash-looped. The first
operator-visible symptom was "evo went silent in Chat" — by which point
six bots were broken.

``deploy.ensure_plugin_config``'s gap-fill semantics could not have
prevented this: it ``setdefault``s missing keys from a registry, but it
never strips a key that's been removed from the schema. Every schema
tightening leaves a silent landmine until the next per-bot redeploy.

This module replaces the gap-fill step for the
``plugins.entries.evolve.config.*`` slice with a *materialization* step
that:

1. Reads the live plugin manifest (``openclaw.plugin.json``) to learn
   what the schema actually permits *right now*.
2. Reads any per-bot overrides from
   ``{shared_dir}/sandbox/overrides/<bot_id>.json``.
3. Computes the would-be plugin config from (overrides + registry
   defaults + identity from network.json).
4. Detects drift — any current key whose value differs from the
   would-be value and isn't already an override — and auto-promotes it
   to a ``needs_review=True`` override so redeploys never silently
   destroy operator edits.
5. Returns the regenerated block plus the list of changes (auto-promoted
   keys, pruned-stale keys) so the caller can emit Signals and the
   operator can review on the Customizations page.

Scope: the evolve plugin config block ONLY. Other openclaw.json
sections (gateway.*, session.*, channels.*, the rest of agents.defaults)
continue to be managed by ``ensure_plugin_config``'s existing repair
passes. A follow-on PR widens the materialized slice once the
narrow change has soaked.

The function is pure (no I/O of its own beyond schema/overrides reads);
callers wire it into ``deploy.ensure_plugin_config`` and pass the result
through ``safe_write_bot_config`` for atomic write + OC validation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from platform_profile import get_profile

from .config_sandbox import (
    OverrideEntry,
    OverrideStateError,
    OverrideValidationError,
    Store,
    by_path,
    read_bot_overrides,
    write_override,
)
from .config_sandbox.schema import SCHEMA


_log = logging.getLogger(__name__)


# Plugin manifest lives under the staged plugin dir on the mini (set up
# by the repo puller + plugin build). The configSchema in there is the
# *runtime* truth — what OC will validate against on next gateway
# reload. Read live so we pick up schema changes the same tick the
# plugin rebuilds.
# Platform-keyed: macOS /Users/Shared/evolve-plugin, Linux /var/lib/evolve-plugin
# (get_profile().plugin_install_dir — same seam evolve_config uses for shared_dir).
# The bare /Users/... literal logged "manifest … not found" every materialize pass
# on the Ubuntu/VPS pods because the path simply doesn't exist there. (Linux port.)
DEFAULT_PLUGIN_MANIFEST = Path(get_profile().plugin_install_dir) / "openclaw.plugin.json"


# Identity fields — always pulled from network.json / deploy context.
# These are NOT in the sandbox schema (they're per-deploy, not
# operator-tunable). The materializer rewrites them verbatim every
# deploy so a network rename, role change, or sharedDir move is picked
# up immediately.
#
# CORE identity fields predate the plugin's configSchema; OC has always
# accepted them and they are structurally required to identify the bot,
# so they are written UNCONDITIONALLY every deploy.
_CORE_IDENTITY_FIELDS = ("botId", "role", "networkId", "sharedDir")
#
# SCHEMA-ADDITIVE identity fields were added to the plugin's strict
# configSchema AFTER it first shipped (``repoRoot`` by #3115). The
# DEPLOYED manifest the gateway loads can LAG the admin code that writes
# them: the VPS repo-puller needs 2-3 passes to fast-forward, so the
# admin starts writing a key while the staged plugin still predates it.
# Writing such a key when the deployed schema rejects it makes OC reject
# the ENTIRE config on EVERY reload (configSchema additionalProperties:
# false) — silent and total: the bot stops picking up ANY config change
# until its gateway restarts. So these fields are GATED on the deployed
# manifest actually declaring the key (see load_plugin_config_schema,
# which unions only the CORE set, and the Pass-3a write gate below).
# When ``repoRoot`` is omitted during a lag window the plugin falls back
# to dirname(sharedDir)/evolve-repo (TurnObserver.resolveAnalyzerDir, the
# pre-#3115 behavior); reloads keep working and it self-corrects on the
# next deploy once the staged plugin catches up — far better than
# breaking every reload.
#
# ``repoRoot`` is the deploy checkout (the read-only repo daemons load
# from). The plugin uses it to locate the packages/analyzer scripts it
# spawns (session_surface.py, task_extractor.py). It is derived from the
# active platform profile — /Users/Shared/evolve-repo on macOS,
# /var/lib/evolve/repo on Linux — NOT from dirname(sharedDir), because on
# Linux the checkout is a CHILD of sharedDir, not a sibling (the live
# evolve-vsp-pod "can't open /var/lib/evolve-repo/.../session_surface.py"
# regression). See _resolve_identity.
_SCHEMA_ADDITIVE_IDENTITY_FIELDS = ("repoRoot",)
#
# Full identity set — for callers that only need "is this an identity
# field at all" (Pass-1 drift skip here; openclaw_migration).
_IDENTITY_FIELDS = _CORE_IDENTITY_FIELDS + _SCHEMA_ADDITIVE_IDENTITY_FIELDS


@dataclass
class MaterializeResult:
    """What the materializer produced + what changed.

    Attributes:
        new_block: the regenerated ``plugins.entries.evolve.config`` dict.
        auto_promoted: keys whose live values diverged from the would-be
            value with no recorded override; we auto-recorded them and
            flagged ``needs_review = True``. Operator should review on
            the Customizations page.
        pruned_stale: keys present in the previous block but no longer
            allowed by the plugin's configSchema; dropped from the new
            block. This is the #1525 cleanup.
        kept_overrides: keys whose value comes from the overrides file
            (not from defaults). Surfaced for logging.
        persistence_failed: True if any auto-promote write_override
            raised a non-validation error (filesystem failure, malformed
            existing overrides file, etc.). The in-memory value is still
            used in ``new_block`` for the current deploy, but the
            override file did NOT capture it — the next deploy will
            re-detect the drift. Surface as a Signal at the caller; the
            materializer doesn't have the signals package as a
            dependency.
        unchanged: True iff new_block is byte-equal to current_block —
            caller can skip writing.
    """

    new_block: dict[str, Any]
    auto_promoted: list[str] = field(default_factory=list)
    pruned_stale: list[str] = field(default_factory=list)
    kept_overrides: list[str] = field(default_factory=list)
    persistence_failed: bool = False
    unchanged: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Plugin manifest reader
# ─────────────────────────────────────────────────────────────────────────────


def _read_manifest_config_keys(manifest_path: Path) -> set[str] | None:
    """Raw ``configSchema.properties`` key set from a plugin manifest.

    Returns ``None`` when the manifest is absent / unreadable / malformed
    (the caller decides whether to fall back to another manifest), and an
    empty set only when the schema genuinely declares zero properties. The
    ``None`` vs ``set()`` distinction is what lets ``deployed_plugin_config_keys``
    fall back to source on a missing staged copy without conflating it with
    a (pathological) zero-property schema.

    Uses builtin ``open()`` (not ``Path.read_text`` / ``Path.exists``) so test
    fixtures that patch those on ``Path`` for the bot's ``openclaw.json`` don't
    intercept this manifest read — the same bypass the original
    ``_allowed_plugin_config_keys`` relied on (the #1525 strip path).
    """
    try:
        with open(manifest_path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    props = (data.get("configSchema") or {}).get("properties")
    if not isinstance(props, dict):
        return None
    return set(props.keys())


def deployed_plugin_config_keys(install_dir: Path, source_dir: Path) -> set[str]:
    """Keys ``plugins.entries.evolve.config`` may carry per the manifest the
    GATEWAY actually loads — the DEPLOYED/staged plugin at
    ``{install_dir}/openclaw.plugin.json``, NOT the repo source.

    The two skew: the staged plugin lags the admin code when the repo-puller
    needs 2-3 passes to fast-forward (VPS), so a key the SOURCE already
    declares (``repoRoot``, #3115) can still be rejected by the running
    gateway's strict ``additionalProperties: false`` schema — making OC
    reject the WHOLE config on every reload. Keying the allowed set on the
    DEPLOYED manifest means the admin never writes (or leaves) a key the
    gateway rejects.

    Falls back to the SOURCE manifest only when the staged copy is absent or
    unreadable (fresh bring-up before the first restage — there source ==
    staged because they build together). Empty set when neither is readable,
    so callers treat that as "skip the stale-key strip" (a broken manifest
    must never block deploys). The identity fields are already in the
    manifest's own ``properties`` — no union is needed here.
    """
    for base in (install_dir, source_dir):
        keys = _read_manifest_config_keys(base / "openclaw.plugin.json")
        if keys is not None:
            return keys
    return set()


def load_plugin_config_schema(
    manifest_path: Path = DEFAULT_PLUGIN_MANIFEST,
) -> tuple[set[str], bool]:
    """Read the live (DEPLOYED) plugin manifest and return the set of keys
    allowed under ``plugins.entries.evolve.config``.

    Returns ``(allowed_keys, strict)`` where ``strict`` is True if the
    schema declares ``additionalProperties: false`` (the #1525-class
    failure mode). When ``strict`` is False, unknown keys are tolerated
    by OC — we still surface them to the operator as a Signal but don't
    prune them aggressively.

    ``allowed_keys`` unions the manifest's own ``configSchema.properties``
    with ONLY ``_CORE_IDENTITY_FIELDS`` — never the schema-additive identity
    fields (``repoRoot``). That is the keystone of the schema-skew fix: a
    schema-additive key is "allowed" iff the DEPLOYED manifest itself
    declares it, so when the staged plugin lags, ``repoRoot`` is absent from
    ``allowed_keys`` and is therefore neither written (Pass 3a) nor preserved
    (Pass 3d) — see ``_SCHEMA_ADDITIVE_IDENTITY_FIELDS``.

    Failures (missing file, malformed JSON) return ``(set(), False)`` —
    safer than guessing; the caller's ``unchanged`` path will still
    materialize the configured defaults, but no stale-key prune runs.
    A WARN is logged so the operator sees the degradation.
    """
    if not manifest_path.exists():
        _log.warning(
            "materializer: plugin manifest %s not found; skipping stale-key prune",
            manifest_path,
        )
        return set(), False
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log.warning(
            "materializer: plugin manifest %s unreadable (%s); skipping stale-key prune",
            manifest_path, e,
        )
        return set(), False
    schema_block = (data.get("configSchema") or {})
    props = schema_block.get("properties")
    # Guard a malformed manifest (``properties`` a list/null/scalar): this is
    # the un-try/excepted materializer call path, so an AttributeError here
    # would crash the bot's deploy. Treat a non-dict as "no declared keys."
    if not isinstance(props, dict):
        props = {}
    strict = schema_block.get("additionalProperties") is False
    return set(props.keys()) | set(_CORE_IDENTITY_FIELDS), strict


# ─────────────────────────────────────────────────────────────────────────────
# Override → schema-path key map
# ─────────────────────────────────────────────────────────────────────────────


# Map from a top-level plugin-config field name (the key as it appears
# in ``plugins.entries.evolve.config.<field>``) to the sandbox schema's
# logical path. The materializer uses this to look up overrides per
# field. Anchored to the schema so adding a new tunable in
# ``config_sandbox/schema.py`` is the only change needed.
def _build_field_to_schema_path() -> dict[str, str]:
    """Walk the openclaw schema entries and build a field → schema_path map.

    Only entries whose ``target_path`` is under
    ``openclaw.json::plugins.entries.evolve.config.<field>`` are
    relevant — those are the plugin config block. Other openclaw schema
    entries (``hooks.allowConversationAccess``, ``agents.defaults.*``)
    live outside this block and aren't materialized by Phase 3.
    """
    prefix = "openclaw.json::plugins.entries.evolve.config."
    result: dict[str, str] = {}
    # Lazy import: schema may not be loaded at module-import time in
    # some lightweight test contexts.
    from .config_sandbox import SCHEMA
    for entry in SCHEMA:
        if not entry.target_path.startswith(prefix):
            continue
        field_name = entry.target_path[len(prefix):]
        # Skip nested keys (no top-level ones in evolve config today)
        if "." in field_name:
            continue
        result[field_name] = entry.path
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Materialize
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_identity(bot_id: str, network: dict[str, Any]) -> dict[str, Any]:
    bot_cfg = (network.get("bots") or {}).get(bot_id, {}) or {}
    # repoRoot: explicit network.json override wins (operator escape hatch,
    # symmetric with sharedDir); otherwise the active platform profile's
    # deploy_checkout_default. get_profile() honors set_profile() so both
    # OS profiles are testable, and the value is byte-identical to today's
    # macOS path (/Users/Shared/evolve-repo) while being correct on Linux
    # (/var/lib/evolve/repo, NOT the sibling /var/lib/evolve-repo).
    from platform_profile import get_profile
    repo_root = network.get("repoRoot") or get_profile().deploy_checkout_default
    return {
        "botId": bot_id,
        "role": bot_cfg.get("role") or "member",
        "networkId": network.get("networkId") or "",
        "sharedDir": network.get("sharedDir") or "/Users/Shared/evolve",
        "repoRoot": repo_root,
    }


def _utc_iso(now: datetime | None = None) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    return now.isoformat(timespec="seconds").replace("+00:00", "Z")


def plugin_defaults_for_bot(
    bot_user: str,
    defaults_registry: dict[str, Any],
) -> dict[str, Any]:
    """Per-bot effective defaults registry: derive ``classifierModel``
    from the bot's credentialed providers.

    The static registry (``deploy._PLUGIN_CONFIG_DEFAULTS``) pins
    ``classifierModel`` to an Anthropic model — correct only when the bot
    actually holds an Anthropic credential. On a pod credentialed with a
    different known provider (e.g. OpenAI-only), materializing the
    Anthropic classifier seeds a dead model id
    (docs/principle-llm-provider-agnostic.md). Derive the classifier from
    the bot's own auth-profiles via ``models.derive_seed_models`` — the
    same helper the fresh-setup wizard seed uses, so wizard-seeded values
    and deploy-materialized defaults agree (no spurious drift
    auto-promotion on a healthy pod).

    Falls back to the static registry unchanged when derivation yields
    nothing (no readable credentials, or only providers without a
    ``_PROVIDER_PICKS`` entry) — the same last-resort posture as the
    plugin's own ``config.ts`` default.
    """
    try:
        from models import derive_seed_models  # type: ignore  # analyzer top-level module
        from .provisioning import _read_auth_profile_providers
        providers = {str(p).lower() for p in _read_auth_profile_providers(bot_user)}
        _, classifier = derive_seed_models(providers)
    except Exception:
        return defaults_registry
    if not classifier or classifier == defaults_registry.get("classifierModel"):
        return defaults_registry
    out = dict(defaults_registry)
    out["classifierModel"] = classifier
    return out


def materialize_evolve_plugin_config(
    bot_id: str,
    network: dict[str, Any],
    current_block: dict[str, Any],
    *,
    defaults_registry: dict[str, Any],
    shared_dir: Path,
    deploy_id: str | None = None,
    plugin_manifest: Path = DEFAULT_PLUGIN_MANIFEST,
    now: datetime | None = None,
) -> MaterializeResult:
    """Compute the regenerated ``plugins.entries.evolve.config`` block.

    Inputs (in precedence order, lowest → highest):

    1. **Identity** from ``network.json`` — botId, role, networkId,
       sharedDir. Always rewritten every deploy so a network rename or
       role change takes effect immediately.
    2. **Schema defaults** from ``defaults_registry``
       (``deploy._PLUGIN_CONFIG_DEFAULTS`` in production). Static
       per-version values for every operator-tunable Policy key.
    3. **Per-bot overrides** from
       ``{shared_dir}/sandbox/overrides/<bot_id>.json``. Operator's
       (or RSI's) explicit divergences from defaults. Schema-validated
       on write (Phase 2), so any value here is by construction OC-valid.

    Drift detection: every key in the current block whose value differs
    from the would-be value (and isn't already an override) is
    auto-promoted to a ``needs_review=True`` override. The would-be
    value is then re-derived with the new override in place, so the
    materialized output matches what was on disk. Redeploys never
    silently destroy edits; they queue them for async review.

    Stale-key prune: any key in the current block that's no longer
    declared in the live plugin manifest's strict ``configSchema``
    (``additionalProperties: false``) is dropped from the new block.
    This is the #1525 cleanup. When the schema isn't strict (or the
    manifest can't be read), the prune is skipped — caller's pre-write
    ``openclaw config validate`` is the ultimate gate.

    The function does NOT write to disk. Caller composes the new block
    into the full openclaw.json and writes via ``safe_write_bot_config``.
    Auto-promote writes to the overrides file DO happen here (since
    they need to land before the materialized block is composed); they
    use ``config_sandbox.write_override`` and follow its atomic /
    locking semantics.
    """
    identity = _resolve_identity(bot_id, network)
    allowed_keys, strict = load_plugin_config_schema(plugin_manifest)
    field_to_schema_path = _build_field_to_schema_path()
    deploy_id = deploy_id or _utc_iso(now)

    # ── Pass 1: load overrides ───────────────────────────────────────────
    bot_overrides = read_bot_overrides(shared_dir, bot_id)

    def _override_for_field(field_name: str) -> OverrideEntry | None:
        schema_path = field_to_schema_path.get(field_name)
        if schema_path is None:
            return None
        return bot_overrides.get(schema_path)

    # ── Pass 2: detect drift in current block and auto-promote ───────────
    # Drift = current value diverges from would-be default AND no override
    # already records the operator's intent. Two conditions both required.
    #
    # We only auto-promote when the field is in ``defaults_registry`` —
    # that's how we know what the "default" actually is to drift FROM.
    # Schema-known fields without a registry entry (e.g.
    # ``dashboardEnabled``, which deploy computes from role per-bot)
    # are preserved verbatim from ``current_block`` in Pass 3 without
    # any drift detection — they have no static default to compare to.
    #
    # We track auto_promoted_values explicitly in addition to writing the
    # override file. Pass 3 then consumes the in-memory value directly,
    # so a transient overrides-file roundtrip failure (or a hostile test
    # mock that returns the wrong file contents) can't mask the operator's
    # tuned value as "default" in the materialized output. The file
    # write is still authoritative for persistence; the in-memory copy is
    # for the same-deploy use within this function.
    auto_promoted: list[str] = []
    auto_promoted_values: dict[str, Any] = {}
    persistence_failed = False
    # OverrideStateError (malformed overrides file) cascades — every
    # subsequent write_override would hit the same file and raise the
    # same error. Break the loop after the first to avoid a flood of
    # duplicate warnings; the caller's Signal makes the file-corruption
    # visible once.
    overrides_file_unreadable = False
    for field_name, schema_path in field_to_schema_path.items():
        if field_name in _IDENTITY_FIELDS:
            continue
        if field_name not in defaults_registry:
            # No static default for this schema-known field → drift is
            # not a meaningful concept; preserve current value as-is.
            continue
        if field_name not in current_block:
            # Genuinely absent on disk; let Pass 3 inject the default.
            continue
        current_val = current_block[field_name]   # may be None if explicitly nulled
        existing_override = _override_for_field(field_name)
        if existing_override is not None:
            continue   # already represented; not "drift"
        default_val = defaults_registry.get(field_name)
        if current_val == default_val:
            continue   # matches default; no override needed
        if overrides_file_unreadable:
            # The overrides file is malformed; any further write_override
            # would re-raise OverrideStateError. Still record the value
            # in-memory so the materialized block honors the operator's
            # edit, but skip the file write.
            auto_promoted_values[field_name] = current_val
            persistence_failed = True
            continue
        # Drift detected. Auto-promote to override with needs_review.
        try:
            write_override(
                shared_dir,
                bot_id,
                schema_path,
                current_val,
                set_by=f"auto:drift_{deploy_id}",
                note="Auto-recorded from ad-hoc edit; review.",
                needs_review=True,
                now=now,
            )
            auto_promoted.append(field_name)
            auto_promoted_values[field_name] = current_val
        except OverrideValidationError as e:
            # Schema-invalid drift (wrong type, malformed enum, etc.)
            # — refuse to promote. The materializer will revert to the
            # default on the next pass; caller's openclaw config validate
            # may then surface the original value as an issue. Log so the
            # operator sees the schema mismatch.
            _log.warning(
                "materializer: %s/%s drift NOT auto-promoted (schema-invalid): %s",
                bot_id, field_name, e.reason,
            )
        except OverrideStateError as e:
            # The overrides file is malformed; subsequent writes would
            # fail the same way. Flag for the caller to Signal once,
            # keep the in-memory value so this deploy doesn't regress,
            # and skip further write attempts in this pass.
            _log.warning(
                "materializer: %s overrides file is unreadable, "
                "skipping further auto-promote: %s",
                bot_id, e,
            )
            auto_promoted_values[field_name] = current_val
            persistence_failed = True
            overrides_file_unreadable = True
        except Exception as e:
            # Filesystem error (permission denied on shared_dir, disk
            # full, etc.). Treat as "couldn't persist override" — fall
            # back to keeping the current value in the materialized
            # output via auto_promoted_values, but mark the persistence
            # as failed so the caller can decide how loud to be.
            _log.warning(
                "materializer: %s/%s override write failed (%s: %s); "
                "value preserved in this deploy but NOT persisted",
                bot_id, field_name, type(e).__name__, e,
            )
            auto_promoted_values[field_name] = current_val
            persistence_failed = True

    # Reload overrides — we may have just added entries. If the reload
    # fails (e.g. test mock returns wrong file contents), the
    # auto_promoted_values fallback in Pass 3 covers us.
    if auto_promoted:
        try:
            bot_overrides = read_bot_overrides(shared_dir, bot_id)
        except Exception as e:
            _log.warning(
                "materializer: overrides reload after auto-promote failed (%s: %s); "
                "using in-memory fallback for newly-promoted values",
                type(e).__name__, e,
            )

    # ── Pass 3: compose new block from inputs ────────────────────────────
    new_block: dict[str, Any] = {}
    kept_overrides: list[str] = []

    # 3a. Identity. CORE fields are always written. SCHEMA-ADDITIVE identity
    # fields (``repoRoot``) are GATED on the DEPLOYED manifest actually
    # declaring them: when the staged plugin LAGS the admin code, writing a
    # key its strict configSchema rejects makes OC reject the WHOLE config on
    # every reload — silent + total. ``allowed_keys`` unions only the CORE
    # set, so a schema-additive field is in ``allowed_keys`` iff the deployed
    # manifest's own properties carry it. When non-strict (OC tolerates extra
    # keys) or the manifest is unreadable (allowed_keys empty), the gate is a
    # no-op and we write the field — preserving pre-skew behavior. See
    # ``_SCHEMA_ADDITIVE_IDENTITY_FIELDS``.
    for _id_field, _id_val in identity.items():
        if (
            _id_field in _SCHEMA_ADDITIVE_IDENTITY_FIELDS
            and strict
            and allowed_keys
            and _id_field not in allowed_keys
        ):
            continue   # deployed plugin lags this key — omit to keep reloads valid
        new_block[_id_field] = _id_val

    # 3b. Every defaults_registry key → override value (file or in-memory
    # auto-promote fallback) or default.
    for field_name, default_val in defaults_registry.items():
        ov = _override_for_field(field_name)
        if ov is not None:
            new_block[field_name] = ov.value
            kept_overrides.append(field_name)
        elif field_name in auto_promoted_values:
            # File-reload didn't surface the override we just wrote (or
            # the write failed and we kept the in-memory copy). Either
            # way, honor it here so the operator's tuned value isn't
            # masked as "default" in the materialized output.
            new_block[field_name] = auto_promoted_values[field_name]
            kept_overrides.append(field_name)
        else:
            new_block[field_name] = default_val

    # 3c. Schema-known keys NOT in defaults_registry — preserve from
    # current block or apply override. Two cases:
    #   - Override exists → use it.
    #   - Override doesn't exist → preserve current value verbatim if
    #     present (e.g. ``dashboardEnabled`` set by deploy.py post-
    #     materialize; ``enableLLMSummarization`` set by some other
    #     code path). Without this preservation, the materializer
    #     would drop these fields, regressing their semantics.
    for field_name, schema_path in field_to_schema_path.items():
        if field_name in new_block:
            continue
        ov = _override_for_field(field_name)
        if ov is not None:
            new_block[field_name] = ov.value
            kept_overrides.append(field_name)
        elif field_name in current_block:
            new_block[field_name] = current_block[field_name]

    # 3d. Honor existing values for keys NEITHER in defaults_registry
    # NOR in our local schema. Three scenarios:
    #   - Plugin shipped a newer field the admin doesn't know about, but
    #     the manifest's allowed_keys list does → keep verbatim.
    #   - Plugin is non-strict (additionalProperties: true) → OC won't
    #     reject unknown keys, so we don't prune them either.
    #   - Strict plugin + unknown key + not in allowed_keys → this is
    #     the #1525 case; drop (enumerated in Pass 4 below).
    for field_name, value in current_block.items():
        if field_name in new_block:
            continue
        if field_name in field_to_schema_path:
            continue   # already handled in 3c
        if strict and allowed_keys and field_name not in allowed_keys:
            continue   # stale under strict schema — drop
        new_block[field_name] = value

    # ── Pass 4: enumerate pruned-stale keys ──────────────────────────────
    # Only meaningful when the schema is strict. When the manifest is
    # missing/non-strict, pruned_stale is empty by design (caller's
    # openclaw config validate is the ultimate gate; speculative pruning
    # would be unsafe).
    pruned_stale: list[str] = []
    if strict and allowed_keys:
        for field_name in current_block:
            if field_name not in new_block:
                pruned_stale.append(field_name)

    # ── Pass 5: unchanged optimization ───────────────────────────────────
    unchanged = (current_block == new_block) and not auto_promoted and not pruned_stale

    return MaterializeResult(
        new_block=new_block,
        auto_promoted=sorted(auto_promoted),
        pruned_stale=sorted(pruned_stale),
        kept_overrides=sorted(kept_overrides),
        persistence_failed=persistence_failed,
        unchanged=unchanged,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3d — policy-slice materializer (permission-posture + other top-level
# openclaw.json Policy keys outside the evolve-plugin block)
# ─────────────────────────────────────────────────────────────────────────────


# Prefix used to identify schema entries the EVOLVE-PLUGIN-block materializer
# already handles. Anything starting with this prefix is owned by
# ``materialize_evolve_plugin_config`` and must NOT be touched here, or the
# two passes would write the same field twice (with the policy-slice pass
# winning, since it runs second — defeating the per-block drift detection).
_EVOLVE_PLUGIN_PREFIX = "openclaw.json::plugins.entries.evolve.config."
# The plugin-block materializer also owns the ``hooks.allowConversationAccess``
# sibling under the evolve plugin entry — handle it analogously.
_EVOLVE_PLUGIN_HOOKS_PREFIX = "openclaw.json::plugins.entries.evolve.hooks."


@dataclass
class PolicySliceResult:
    """What the policy-slice materializer changed.

    Attributes:
        applied_overrides: schema paths whose override was written into
            cfg. The dotpath form of each is also enumerated in
            ``applied_dotpaths`` for caller-side logging.
        applied_dotpaths: the openclaw.json dotpath form
            (e.g. ``tools.exec.security``) corresponding to each entry
            in ``applied_overrides``.
        skipped_unparseable_overrides: True iff a malformed overrides file
            was encountered. The slice falls back to no-overrides for
            that bot; callers may want to emit a Signal.
    """

    applied_overrides: list[str] = field(default_factory=list)
    applied_dotpaths: list[str] = field(default_factory=list)
    skipped_unparseable_overrides: bool = False


# Wildcard segment token used in a schema's ``target_path`` to mean
# "fan out across every matching child of this parent dict." Used today
# for per-Anthropic-model knobs like ``params.cacheRetention`` whose
# single operator-tuned value applies uniformly to every Anthropic
# model in the bot's catalog. See ``_apply_models_cache_retention``.
_DOTPATH_WILDCARD = "*"


def _policy_slice_entries() -> list[tuple[str, str]]:
    """Return ``(schema_path, openclaw_dotpath)`` for every OPENCLAW
    schema entry the policy-slice materializer owns.

    Excludes anything the evolve-plugin-block materializer already
    handles (``plugins.entries.evolve.config.*`` and ``...hooks.*``).
    Excludes per_bot=False entries — pod-wide keys aren't in scope for
    per-bot override consumption.
    Excludes wildcard target_paths — those are fanned out by a dedicated
    handler (see ``_apply_models_cache_retention``) because the simple
    "1 schema_path → 1 dotpath" mapping doesn't fit a per-model knob
    that has to land on N catalog entries.
    """
    result: list[tuple[str, str]] = []
    for entry in SCHEMA:
        if entry.store != Store.OPENCLAW:
            continue
        if not entry.per_bot:
            continue
        tp = entry.target_path
        if not tp.startswith("openclaw.json::"):
            continue
        if tp.startswith(_EVOLVE_PLUGIN_PREFIX):
            continue
        if tp.startswith(_EVOLVE_PLUGIN_HOOKS_PREFIX):
            continue
        dotpath = tp[len("openclaw.json::"):]
        # Wildcard schema entries (e.g. ``agents.defaults.models.*.params.cacheRetention``)
        # are handled by a dedicated fan-out step rather than the generic
        # dotpath writer, because the wildcard segment needs catalog enumeration.
        if _DOTPATH_WILDCARD in dotpath.split("."):
            continue
        result.append((entry.path, dotpath))
    return result


def _dotpath_set(obj: dict, dotpath: str, value: Any) -> None:
    """Set ``obj[a][b][c] = value`` for dotpath "a.b.c", creating dicts."""
    parts = dotpath.split(".")
    cur = obj
    for seg in parts[:-1]:
        if seg not in cur or not isinstance(cur[seg], dict):
            cur[seg] = {}
        cur = cur[seg]
    cur[parts[-1]] = value


# Schema path for the cacheRetention knob. Kept as a module-level
# constant so the fan-out handler and any future test that wants to set
# this override don't have to repeat the string.
_CACHE_RETENTION_SCHEMA_PATH = "openclaw.agents.defaults.models.cacheRetention"
# Schema path for the per-session cost cap. Kept as a constant for the
# BE-config consultation step below.
_SESSION_BUDGET_CAP_SCHEMA_PATH = "openclaw.agents.defaults.sessionBudgetCapUsd"
# Provider prefix that gates fan-out. ``cacheRetention`` is an
# Anthropic-only model parameter (it controls Anthropic's prompt-cache
# TTL); other providers don't accept the field and writing it there
# would either be silently ignored or trip the provider client. The
# match is on the catalog key's ``provider/model`` prefix.
_ANTHROPIC_MODEL_PREFIX = "anthropic/"


def _is_anthropic_model_id(model_id: str) -> bool:
    """Return True for catalog entries that name an Anthropic model.

    The catalog key shape is ``provider/model`` (e.g.
    ``anthropic/claude-sonnet-4-6``). Match by prefix so a new Anthropic
    family member added to the catalog later picks up the override
    automatically.
    """
    return isinstance(model_id, str) and model_id.startswith(_ANTHROPIC_MODEL_PREFIX)


def _apply_models_cache_retention(
    cfg: dict[str, Any],
    value: str,
) -> list[str]:
    """Fan ``value`` out to ``agents.defaults.models.<id>.params.cacheRetention``
    for every Anthropic model in the bot's catalog. Returns the list of
    model IDs we wrote to.

    Pure function on ``cfg``: idempotent, and tolerant of a missing /
    empty / non-dict catalog (returns []). Each affected model's
    ``params`` dict is created if absent, never wholesale-replaced —
    other per-model parameters (alias, etc.) stay intact.

    Non-Anthropic models in the catalog are deliberately skipped — the
    setting has no meaning for other providers and writing it could
    trip strict provider clients on future OC versions.
    """
    # Defensive walk — a malformed cfg (caller passed a non-dict at some
    # rung) returns [] rather than crashing the deploy.
    agents = cfg.get("agents")
    if not isinstance(agents, dict):
        return []
    defaults = agents.get("defaults")
    if not isinstance(defaults, dict):
        return []
    models = defaults.get("models")
    if not isinstance(models, dict):
        return []
    affected: list[str] = []
    for model_id, model_cfg in list(models.items()):
        if not _is_anthropic_model_id(model_id):
            continue
        if not isinstance(model_cfg, dict):
            # Operator-edited catalog entry shape we don't recognize.
            # Replace with a fresh dict carrying just the cache knob —
            # caller's openclaw config validate will catch any wider
            # malformation. Loud-fail later beats silent-skip here.
            model_cfg = {}
            models[model_id] = model_cfg
        params = model_cfg.get("params")
        if not isinstance(params, dict):
            params = {}
            model_cfg["params"] = params
        params["cacheRetention"] = value
        affected.append(model_id)
    return affected


def materialize_openclaw_policy_slice(
    cfg: dict[str, Any],
    bot_id: str,
    shared_dir: Path,
) -> PolicySliceResult:
    """Apply per-bot Policy overrides to ``cfg`` in place.

    Walks every OPENCLAW schema entry whose ``target_path`` is NOT under
    ``plugins.entries.evolve.config.*`` (those are handled by
    ``materialize_evolve_plugin_config``). For each, if an override is
    recorded for ``bot_id`` in
    ``{shared_dir}/sandbox/overrides/<bot_id>.json``, writes the value
    into ``cfg`` at the schema's dotpath.

    The override takes precedence over whatever inference / gap-fill
    passes have already written to ``cfg``. Layering precedence (lowest
    → highest):

        schema_default  <  pod_default  <  inference  <  per_bot_override

    This is the rule Phase 3d encodes: the auth_drift_filler PR #1316
    incident showed that context-free inference can produce
    contextually-wrong values once deliberate deviations exist. Once an
    operator (or L2 RSI proposal) records a deliberate value here, the
    materializer makes it stick across deploys.

    No drift auto-promotion happens for these fields — their "default"
    is inferred per-bot at deploy time (e.g. ``tools.exec.security``
    depends on ``exec-approvals.json`` contents), so the simple
    "value ≠ default → drift" heuristic doesn't apply. Drift detection
    on these fields is owned by the ``auth_drift_filler`` /
    ``cron_caps_filler`` generators, which inspect the active baseline
    rather than a static default.

    Returns a ``PolicySliceResult`` listing each schema path whose
    override was applied (for caller-side logging).

    Idempotent: re-running with no override changes produces the same
    ``cfg``. Caller is responsible for the actual disk write
    (``safe_write_bot_config`` or equivalent) — this function only
    mutates the in-memory ``cfg``.
    """
    result = PolicySliceResult()

    try:
        bot_overrides = read_bot_overrides(shared_dir, bot_id)
    except Exception as e:
        # read_bot_overrides is defensive — a malformed file becomes
        # empty — but catch anything wider (filesystem perm issue, etc.)
        # so the caller can surface a Signal without crashing the deploy.
        _log.warning(
            "policy_slice: reading overrides for %s failed (%s: %s); "
            "skipping override application this pass",
            bot_id, type(e).__name__, e,
        )
        result.skipped_unparseable_overrides = True
        return result

    # ── BE-config: sole source for the two cost knobs ───────────────────────
    # Phase 1 of the cost-cap normalization (PR #2189) made
    # better-engine-config the canonical operator-facing source for the
    # per-session cap + Anthropic prompt-cache TTL. Phase 2 (#2193) drained
    # any existing sandbox values into BE config and stripped them clean.
    # Phase 4b (this PR) removes the sandbox-override fallback for those
    # two keys; the TunableKey schema entries are deleted in tandem. Any
    # legacy sandbox file that still carries one of these keys is now
    # silently ignored — operators must edit via the Cost & caps card.
    be_session_cap: float | None = None
    be_cache_retention: str | None = None
    try:
        from better_engine_config import load as _load_be_config  # type: ignore

        _be = _load_be_config(shared_dir)
        be_session_cap = _be.per_bot_session_cost_cap_usd(bot_id)
        be_cache_retention = _be.per_bot_cache_retention(bot_id)
    except Exception as e:
        _log.warning(
            "policy_slice: BE config read for %s failed (%s: %s); "
            "cost knobs (per-session cap, cache TTL) will fall through to "
            "shipped defaults this pass — the sandbox-override fallback "
            "was removed in Phase 4b",
            bot_id, type(e).__name__, e,
        )

    for schema_path, dotpath in _policy_slice_entries():
        override = bot_overrides.get(schema_path)
        if override is None:
            continue
        _dotpath_set(cfg, dotpath, override.value)
        result.applied_overrides.append(schema_path)
        result.applied_dotpaths.append(dotpath)

    # ── Per-session cost cap (BE-config-only, post-Phase-4b) ───────────────
    # The TunableKey schema entry was deleted in Phase 4b — there's no
    # sandbox path left, so this block writes the openclaw.json field
    # directly from BE config or skips when unset.
    if be_session_cap is not None:
        _dotpath_set(
            cfg, "agents.defaults.sessionBudgetCapUsd", be_session_cap,
        )
        result.applied_overrides.append(_SESSION_BUDGET_CAP_SCHEMA_PATH)
        result.applied_dotpaths.append("agents.defaults.sessionBudgetCapUsd")

    # ── Wildcard fan-out: per-Anthropic-model cacheRetention ────────────────
    # Single-value-fans-to-N catalog entries. BE config is the sole source
    # (Phase 4b removed the sandbox fallback + schema entry). See
    # ``_apply_models_cache_retention`` for the per-model write logic.
    if be_cache_retention is not None:
        affected = _apply_models_cache_retention(cfg, be_cache_retention)
        if affected:
            result.applied_overrides.append(_CACHE_RETENTION_SCHEMA_PATH)
            for mid in affected:
                result.applied_dotpaths.append(
                    f"agents.defaults.models.{mid}.params.cacheRetention"
                )

    return result
