"""arbiter.appliers.agent_defaults — mutate ``agents.defaults.*`` knobs
on a member bot's openclaw.json via the sandbox overrides file.

Spec:
  - internal/spec-openclaw-json-derived-artifact-2026-05-24.md §7 (L2 applier
    pattern: overrides → materialize → safe_write → kickstart).
  - internal/spec-cache-retention-tunable-2026-05-30 (PR A — surfaced the
    Anthropic prompt-cache TTL knob to operators).

Why a separate applier (not UpdatePermissionConfigApplier):

  The permissions applier is restricted to ``tools.*`` and ``commands.*``
  by design — those are the permission-posture surface, with composite
  guards (``ask=off`` refused, ``security=full + ask=off`` refused) that
  don't apply to model-config knobs. Mixing them would either bloat the
  guard logic with knob-specific exemptions or accidentally apply
  permission-posture guards to cache-TTL flips.

  Same /tmp + sudo + chmod + kickstart pattern, different field set,
  different validator. Each L2 applier owns one surface.

Today the applier only accepts the cacheRetention wildcard dotpath. The
allowed-dotpath whitelist + per-dotpath value validator live on the
``UpdateAgentDefaults`` action type in schema/proposal.py — widening
the whitelist is a separate PR (with its own materializer slice if the
new field needs different fan-out semantics).

Motivating incident: team-bot-a session 3d5cde22 (2026-05-29) — a Slack
DM cost $7.67 because the 5-minute prompt-cache TTL invalidated on
every human-paced turn gap. PR A let the operator flip the knob; this
applier lets PR F's ``cache_ttl_tuner`` generator flip it autonomously
when telemetry says the bot is on a Slack-DM cadence.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from evolve_config import CANONICAL_SHARED_DIR

from arbiter.appliers.base import ApplyResult, RevertResult, register_applier
from evolve_config import bot_home as _bot_home
from runtime.scheduler import get_scheduler
from schema.proposal import (
    UpdateAgentDefaults,
    _UPDATE_AGENT_DEFAULTS_ALLOWED_DOTPATHS,
    _UPDATE_AGENT_DEFAULTS_VALUE_VALIDATORS,
)


# Sentinel marking "this key was absent from openclaw.json at snapshot
# time" — distinct from "the key was present with a None/null value".
# Snapshots are JSON-serialized through the proposal store, so we keep
# the marker as a string literal that the revert path checks by-equality.
# Plain ``None`` can't carry the distinction because operators may
# deliberately set sessionBudgetCapUsd to null (which itself means "no
# cap" and is a meaningful on-disk value).
_ABSENT = "__absent__"


# ─────────────────────────────────────────────────────────────────────────────
# Dotpath → sandbox schema path
# ─────────────────────────────────────────────────────────────────────────────
#
# The sandbox schema is the source of truth for "which logical key
# governs this openclaw.json dotpath." For wildcard target_paths (like
# ``agents.defaults.models.*.params.cacheRetention``) we look up the
# schema entry whose ``target_path`` matches and read its ``path`` —
# that's what gets written into the overrides file.
#
# Today there's one entry; lookup is a dict for symmetry with the
# permissions applier's ``_permission_posture_field_map`` and so the
# function naturally extends as more agents.* knobs come online.


def _agent_defaults_field_map() -> dict[str, str]:
    """Return ``{openclaw_dotpath: schema_path}`` for every agents-defaults
    field declared in the sandbox schema with a wildcard target_path.

    Wildcard target_paths (containing ``*``) are the fan-out signal —
    the materializer applies a single override value to every matching
    catalog entry. Non-wildcard ``agents.*`` knobs (e.g. a future
    per-model alias) would route through a different applier (or a
    wider whitelist on this one once the materializer learns the
    appropriate fan-out rule).
    """
    try:
        from evolve_admin.config_sandbox import SCHEMA, Store
    except ImportError:
        return {}
    result: dict[str, str] = {}
    for entry in SCHEMA:
        if entry.store != Store.OPENCLAW:
            continue
        if not entry.per_bot:
            continue
        tp = entry.target_path
        if not tp.startswith("openclaw.json::"):
            continue
        dotpath = tp[len("openclaw.json::"):]
        # Only ``agents.*`` knobs — permission-posture fields live on
        # the permissions applier.
        if not dotpath.startswith("agents."):
            continue
        result[dotpath] = entry.path
    return result


def _dotpath_get(obj: dict, dotpath: str) -> Any:
    """Return the value at ``dotpath`` in ``obj``, or None if any segment
    is absent. Wildcards are NOT expanded — for a wildcard dotpath this
    just returns None (the materializer handles fan-out)."""
    cur: Any = obj
    for seg in dotpath.split("."):
        if seg == "*":
            # No-op for wildcards — the materializer expands these
            # post-hoc via catalog enumeration; per-field "prior value"
            # snapshots use a different shape (see capture_snapshot).
            return None
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
        if cur is None:
            return None
    return cur


def _shared_dir() -> Path:
    """Resolve shared_dir from env or default. Mirrors permissions._shared_dir
    — kept inline here so the agent-defaults applier doesn't reach into
    a sibling applier's privates."""
    import os
    candidates = [os.environ.get("EVOLVE_SHARED_DIR"), str(CANONICAL_SHARED_DIR)]
    for c in candidates:
        if c and Path(c).exists():
            return Path(c)
    return CANONICAL_SHARED_DIR


def _read_current(bot_id: str, home_override: Path | None = None) -> dict | None:
    """Read the bot's openclaw.json via the writer's sudo-cat fallback."""
    from permissions.writer import read_openclaw_json
    home = home_override or _bot_home(bot_id)
    return read_openclaw_json(home / ".openclaw" / "openclaw.json")


def _materialize_and_write(
    target_bot: str,
    shared_dir: Path,
    *,
    home_override: Path | None = None,
    bot_user: str | None = None,
    kickstart: bool = True,
    reason: str,
) -> tuple[bool, str]:
    """Read current openclaw.json, run the policy-slice materializer
    (which fans out the cacheRetention override across every Anthropic
    model in the catalog), write back atomically, kickstart the gateway.

    Mirrors permissions._materialize_and_write — separate copy here keeps
    each applier file self-contained (the function is short and
    importing across applier modules would create a cycle once
    appliers start consuming each other's privates).

    Test path (``home_override`` set): read+write directly against the
    synthetic ``home_override/.openclaw/openclaw.json`` tree, no
    validation shellout, no kickstart.

    Production path: ``safe_write_bot_config`` validates against the OC
    schema before writing (via /tmp + sudo + chmod 644), then we run
    ``launchctl kickstart -k`` so OC reloads with the new params.
    """
    if home_override is not None:
        oc_path = home_override / ".openclaw" / "openclaw.json"
        from permissions.writer import read_openclaw_json
        current = read_openclaw_json(oc_path)
    else:
        from permissions.writer import read_openclaw_json
        home = _bot_home(target_bot)
        oc_path = home / ".openclaw" / "openclaw.json"
        current = read_openclaw_json(oc_path)
    if current is None:
        return False, f"openclaw.json unreadable at {oc_path}"

    new_cfg = deepcopy(current)
    try:
        from evolve_admin.openclaw_materializer import materialize_openclaw_policy_slice
    except ImportError as e:
        return False, f"openclaw_materializer unavailable: {e}"
    materialize_openclaw_policy_slice(new_cfg, target_bot, shared_dir)

    if home_override is not None:
        oc_path.parent.mkdir(parents=True, exist_ok=True)
        oc_path.write_text(json.dumps(new_cfg, indent=2))
        return True, f"Wrote {oc_path} directly (test mode)."

    try:
        from evolve_admin.deploy import safe_write_bot_config
    except ImportError as e:
        return False, f"safe_write_bot_config unavailable: {e}"
    ok, err = safe_write_bot_config(
        target_bot, new_cfg, reason=reason, bot_user=bot_user,
    )
    if not ok:
        return False, err

    if kickstart:
        # Scheduler-seam restart (kickstart -k). Result deliberately ignored:
        # restart fails if the service isn't loaded — that's not a write
        # failure, just nothing to kick.
        get_scheduler().restart(f"ai.openclaw.{target_bot}-gateway")

    return True, f"Materialized + wrote openclaw.json for {target_bot}."


def _record_applier_intents(
    *, bot_id: str, fields: dict, proposal_id: str | None,
    shared_dir: Path,
) -> None:
    """Record one config-intent per dotpath after a successful applier write.

    Spec internal/spec-config-intent-system-2026-05-21.md §3 — every code
    path that mutates a tracked config field calls ``set_intent`` so
    later generators (PR F's ``cache_ttl_tuner``, etc.) know this is a
    deliberate change rather than drift to revert. Memory note
    ``feedback_generators_consider_intent``.

    For UpdateAgentDefaults the field_path that gets recorded is the
    schema's logical path (e.g. ``agents.defaults.models.*.params.cacheRetention``)
    — that's what the generator queries when deciding whether to skip
    a re-propose. We deliberately do NOT record one intent per fanned-out
    model (5 Anthropic models on team-bot-a's catalog would generate 5
    intent rows for one logical decision); the logical key is the
    cache-policy decision the generator made.

    Best-effort: any failure here is logged but does not undo the
    successful config write. The intent record is hygiene metadata;
    the openclaw.json change is the source of truth.
    """
    try:
        from evolve_admin.config_intent import set_intent
    except ImportError:
        return
    set_by = f"applier:{proposal_id}" if proposal_id else "applier"
    reason = (
        f"Applied via proposal {proposal_id}"
        if proposal_id
        else "Applied via UpdateAgentDefaultsApplier"
    )
    for field_path, value in (fields or {}).items():
        try:
            set_intent(
                bot_id, field_path, value,
                reason=reason,
                set_by=set_by,
                set_by_detail=(
                    "Phase 2 placeholder reason; Phase 3 inference layer "
                    "will replace this with a model-derived explanation."
                ),
                actor=set_by,
                shared_dir=shared_dir,
            )
        except Exception:  # noqa: BLE001 — hygiene metadata, never blocking
            continue


# ─────────────────────────────────────────────────────────────────────────────
# UpdateAgentDefaultsApplier
# ─────────────────────────────────────────────────────────────────────────────


class UpdateAgentDefaultsApplier:
    """Mutate ``agents.defaults.*`` knobs via the sandbox overrides file.

    What happens on ``apply``:

      1. Validate each requested dotpath is in the action type's narrow
         whitelist (currently just cacheRetention).
      2. Validate each value against the per-dotpath validator
         (cacheRetention accepts "short" | "long").
      3. Look up the sandbox schema path for the dotpath and
         ``config_sandbox.write_override(...)`` with
         ``set_by="rsi:<proposal_id>"``.
      4. Materialize: apply the policy-slice overrides to a fresh copy
         of openclaw.json (which fans the cacheRetention out across
         every Anthropic model in the catalog) and write through
         ``safe_write_bot_config``.
      5. Kickstart the bot's gateway so OC reloads with the new params.
      6. Record config-intent annotations so later generators know
         this is a deliberate change.

    Idempotent: if the schema's overrides file already carries the
    requested value, the write_override is still issued (last-write-wins
    bumps ``set_at`` and the audit-history entry) but the materialized
    openclaw.json comes out identical.

    What happens on ``revert``:

      For each touched dotpath, either restore the prior override entry
      (operator/migration value the RSI write replaced) or delete the
      override (if no prior override existed). Then materialize + write
      so openclaw.json matches the new overrides state.

    Test hooks (mirrors UpdatePermissionConfigApplier):

      ``home_override`` bypasses ``_bot_home`` and points at a synthetic
      bot tree.
      ``shared_override`` points the overrides file + materializer input
      at a per-test tmp dir so tests don't leak writes to
      ``/Users/Shared/evolve``.
    """

    home_override: Path | None = None
    shared_override: Path | None = None

    def _resolve_shared(self) -> Path:
        return self.shared_override or _shared_dir()

    def _validate_action(
        self, action: UpdateAgentDefaults,
    ) -> tuple[bool, str, dict]:
        """Validate fields against the dotpath whitelist + value
        validators. Returns ``(ok, error_message, details)``."""
        fields = action.fields or {}
        if not fields:
            return False, "UpdateAgentDefaults.fields is empty; nothing to apply.", {
                "error": "no_fields",
            }
        unknown = sorted(
            set(fields.keys()) - _UPDATE_AGENT_DEFAULTS_ALLOWED_DOTPATHS
        )
        if unknown:
            return (
                False,
                f"Refused: dotpath(s) outside UpdateAgentDefaults whitelist: {unknown}",
                {"unknown_fields": unknown,
                 "allowed_fields": sorted(_UPDATE_AGENT_DEFAULTS_ALLOWED_DOTPATHS)},
            )
        for dotpath, value in fields.items():
            validator = _UPDATE_AGENT_DEFAULTS_VALUE_VALIDATORS.get(dotpath)
            if validator is None:
                # Dotpath is whitelisted but no validator declared. Refuse
                # rather than silently accept arbitrary values — every
                # whitelist entry must carry an explicit validator.
                return (
                    False,
                    f"Refused: no value validator declared for {dotpath!r}.",
                    {"error": "missing_validator", "field": dotpath},
                )
            if isinstance(validator, tuple):
                if value not in validator:
                    return (
                        False,
                        (
                            f"Refused: value {value!r} for {dotpath!r} not in "
                            f"allowed set {list(validator)}."
                        ),
                        {"error": "invalid_value", "field": dotpath,
                         "value": value, "allowed": list(validator)},
                    )
            elif isinstance(validator, type):
                if not isinstance(value, validator):
                    return (
                        False,
                        (
                            f"Refused: value for {dotpath!r} must be "
                            f"{validator.__name__}, got {type(value).__name__}."
                        ),
                        {"error": "invalid_type", "field": dotpath,
                         "expected": validator.__name__,
                         "got": type(value).__name__},
                    )
            elif callable(validator):
                # Predicate validator: ``(value) -> bool``. Used when a
                # simple type-or-tuple check isn't enough (e.g.
                # sessionBudgetCapUsd requires "positive float OR None"
                # and must reject bool, which is-a int).
                try:
                    ok_pred = bool(validator(value))
                except Exception as e:  # noqa: BLE001 — validator bug surfaces here
                    return (
                        False,
                        f"Refused: validator for {dotpath!r} raised {type(e).__name__}: {e}",
                        {"error": "validator_raised", "field": dotpath,
                         "exc": str(e)},
                    )
                if not ok_pred:
                    return (
                        False,
                        f"Refused: value {value!r} for {dotpath!r} failed validator.",
                        {"error": "invalid_value", "field": dotpath,
                         "value": value},
                    )
        return True, "", {}

    def capture_snapshot(
        self, action: UpdateAgentDefaults, bot_id: str,
    ) -> dict:
        target_bot = action.bot_id or bot_id
        current = _read_current(target_bot, self.home_override) or {}
        shared = self._resolve_shared()
        try:
            from evolve_admin.config_sandbox import read_bot_overrides
            prior_overrides_obj = read_bot_overrides(shared, target_bot)
            prior_overrides_dict = {
                k: {
                    "value": e.value,
                    "set_by": e.set_by,
                    "set_at": e.set_at,
                    "note": e.note,
                    "expires_at": e.expires_at,
                    "needs_review": e.needs_review,
                }
                for k, e in prior_overrides_obj.overrides.items()
            }
        except Exception:
            prior_overrides_dict = {}

        field_map = _agent_defaults_field_map()
        # For wildcard dotpaths the per-field "prior openclaw.json value"
        # is a per-model dict — we capture the catalog snapshot under a
        # dedicated key so revert can restore exactly what was there.
        catalog_snapshot: dict[str, Any] = {}
        try:
            models = (
                (current.get("agents") or {})
                .get("defaults", {})
                .get("models", {})
            )
            if isinstance(models, dict):
                for mid, mcfg in models.items():
                    if isinstance(mcfg, dict):
                        params = mcfg.get("params")
                        if isinstance(params, dict) and "cacheRetention" in params:
                            catalog_snapshot[mid] = params.get("cacheRetention")
        except Exception:
            catalog_snapshot = {}

        prior_overrides_for_fields: dict[str, dict] = {}
        for dotpath in (action.fields or {}).keys():
            schema_path = field_map.get(dotpath)
            if schema_path and schema_path in prior_overrides_dict:
                prior_overrides_for_fields[schema_path] = prior_overrides_dict[schema_path]

        # PR C — sessionBudgetCapUsd is a scalar dotpath, so we also
        # snapshot its prior on-disk value (may be absent). This lets
        # revert restore the exact pre-apply state when the override is
        # deleted (the materializer only WRITES when an override exists;
        # it doesn't DELETE keys, so without this snapshot a revert would
        # leave the apply()-written value stranded in openclaw.json).
        prior_scalar_snapshot: dict[str, Any] = {}
        try:
            defaults = (current.get("agents") or {}).get("defaults") or {}
            if isinstance(defaults, dict):
                if "sessionBudgetCapUsd" in defaults:
                    prior_scalar_snapshot["agents.defaults.sessionBudgetCapUsd"] = (
                        defaults["sessionBudgetCapUsd"]
                    )
                # Sentinel value to record absence — distinguishes "was
                # null/None on disk" from "the key wasn't present".
                else:
                    prior_scalar_snapshot["agents.defaults.sessionBudgetCapUsd"] = _ABSENT
        except Exception:
            prior_scalar_snapshot = {}

        return {
            "action_kind": "UpdateAgentDefaults",
            "bot_id": target_bot,
            "fields_changed": list((action.fields or {}).keys()),
            "prior_overrides": prior_overrides_for_fields,
            "prior_catalog_cache_retention": catalog_snapshot,
            "prior_scalar_values": prior_scalar_snapshot,
        }

    def apply(
        self,
        action: UpdateAgentDefaults,
        bot_id: str,
        *,
        proposal_id: str | None = None,
    ) -> ApplyResult:
        target_bot = action.bot_id or bot_id
        fields = action.fields or {}

        # 1+2) Whitelist + per-dotpath value validation. These checks are
        # cheap and avoid touching disk on a malformed action.
        ok, err, details = self._validate_action(action)
        if not ok:
            return ApplyResult(ok=False, details=details, message=err)

        # 3) Confirm openclaw.json is readable up front so we can give a
        # clear error if the bot isn't deployed (or the ACL is wrong) —
        # the materializer would error later with a less actionable
        # message.
        current = _read_current(target_bot, self.home_override)
        if current is None:
            return ApplyResult(
                ok=False, details={"error": "openclaw_unreadable"},
                message=f"Cannot read openclaw.json for {target_bot}.",
            )

        # 4) Map dotpath → sandbox schema path so write_override can
        # validate against the schema's type_hint + per_bot flag.
        field_map = _agent_defaults_field_map()
        unmapped = sorted(set(fields.keys()) - set(field_map.keys()))
        if unmapped:
            # Whitelisted in the action type but not in the sandbox
            # schema — schema/proposal.py and config_sandbox/schema.py
            # drifted. Surface clearly rather than silently no-op.
            return ApplyResult(
                ok=False,
                details={"error": "schema_path_unmapped",
                         "unmapped": unmapped},
                message=(
                    f"Internal: dotpath(s) {unmapped} have no sandbox "
                    "schema entry. Action-type whitelist and sandbox "
                    "schema must agree."
                ),
            )

        # 5) Write overrides — durable layer. Partial-write recovery
        # mirrors the permissions applier: if write_override raises on
        # the Nth field, roll back the first N-1 so a half-applied
        # state doesn't leak into the next deploy.
        shared = self._resolve_shared()
        set_by = f"rsi:{proposal_id}" if proposal_id else "rsi"
        try:
            from evolve_admin.config_sandbox import write_override
        except ImportError as e:
            return ApplyResult(
                ok=False, details={"error": "config_sandbox_unavailable",
                                   "exc": str(e)},
                message=f"config_sandbox not importable: {e}",
            )
        try:
            from evolve_admin.config_sandbox import delete_override as _delete_override
        except ImportError:
            _delete_override = None  # type: ignore[assignment]
        written: list[str] = []
        for dotpath, value in fields.items():
            schema_path = field_map[dotpath]
            try:
                write_override(
                    shared, target_bot, schema_path, value,
                    set_by=set_by,
                )
            except Exception as e:  # noqa: BLE001 — surface write errors
                if _delete_override is not None:
                    for prior_schema_path in written:
                        try:
                            _delete_override(shared, target_bot, prior_schema_path)
                        except Exception:
                            pass
                return ApplyResult(
                    ok=False,
                    details={"error": "override_write_failed",
                             "field": dotpath, "exc": str(e),
                             "rolled_back": list(written)},
                    message=f"Failed to write override for {dotpath}: {e}",
                )
            written.append(schema_path)

        # 6+7) Materialize + write + kickstart. Same flow the deploy
        # pipeline runs via ensure_plugin_config — on-demand variant so
        # the new params take effect immediately.
        write_ok, msg = _materialize_and_write(
            target_bot, shared,
            home_override=self.home_override,
            kickstart=self.home_override is None,
            reason=f"UpdateAgentDefaults({proposal_id or 'no-proposal'})",
        )
        if not write_ok:
            return ApplyResult(ok=False, details={"write_error": msg}, message=msg)

        # 8) Record config-intent annotations. Best-effort — a recording
        # failure must not undo the successful config write.
        _record_applier_intents(
            bot_id=target_bot,
            fields=fields,
            proposal_id=proposal_id,
            shared_dir=shared,
        )

        return ApplyResult(
            ok=True,
            details={
                "bot_id": target_bot,
                "fields": dict(fields),
                "override_paths": written,
                "set_by": set_by,
            },
            message=(
                f"Updated agent defaults on {target_bot}: "
                f"{sorted(fields.keys())}"
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        target_bot = snapshot.get("bot_id") or bot_id
        fields_changed = list(snapshot.get("fields_changed") or [])
        prior_overrides = snapshot.get("prior_overrides") or {}
        if not fields_changed:
            return RevertResult(
                ok=False, details={"reason": "no_fields_changed"},
                message="Snapshot recorded no fields_changed; cannot revert.",
            )

        field_map = _agent_defaults_field_map()
        shared = self._resolve_shared()
        try:
            from evolve_admin.config_sandbox import (
                delete_override,
                write_override,
            )
        except ImportError as e:
            return RevertResult(
                ok=False, details={"error": "config_sandbox_unavailable",
                                   "exc": str(e)},
                message=f"config_sandbox not importable: {e}",
            )

        # Restore the overrides file — for each schema path, either
        # restore the captured prior entry or delete (if the apply
        # created it from scratch). Same shape as the permissions
        # applier's revert.
        restored: list[str] = []
        deleted: list[str] = []
        for dotpath in fields_changed:
            schema_path = field_map.get(dotpath)
            if schema_path is None:
                continue
            prior = prior_overrides.get(schema_path)
            if prior is not None:
                try:
                    write_override(
                        shared, target_bot, schema_path, prior["value"],
                        set_by=prior.get("set_by") or "operator",
                        note=prior.get("note"),
                        expires_at=prior.get("expires_at"),
                        needs_review=bool(prior.get("needs_review", False)),
                    )
                except Exception:
                    continue
                restored.append(schema_path)
            else:
                if delete_override(shared, target_bot, schema_path):
                    deleted.append(schema_path)

        # Direct on-disk restore: the materializer's policy-slice pass
        # only WRITES catalog params when an override is set. If apply()
        # created an override that revert() just deleted, the materializer
        # alone won't undo the catalog edit — the now-stale "long" values
        # would linger on every Anthropic model. We need to write the
        # captured pre-apply values back onto each catalog entry first,
        # then let the materializer re-apply any restored operator override
        # on top.
        prior_catalog = snapshot.get("prior_catalog_cache_retention") or {}
        oc_path = (
            (self.home_override or _bot_home(target_bot))
            / ".openclaw" / "openclaw.json"
        )
        from permissions.writer import read_openclaw_json
        current = read_openclaw_json(oc_path)
        if current is None:
            return RevertResult(
                ok=False, details={"error": "openclaw_unreadable"},
                message=f"openclaw.json unreadable at {oc_path}; revert can't write.",
            )
        new_cfg = deepcopy(current)
        models = (
            (new_cfg.get("agents") or {})
            .get("defaults", {})
            .get("models", {})
        )
        if isinstance(models, dict):
            for mid, mcfg in models.items():
                if not isinstance(mcfg, dict):
                    continue
                params = mcfg.get("params")
                prior_value = prior_catalog.get(mid)
                if prior_value is None:
                    # Pre-apply this model had no cacheRetention key.
                    # Remove the one apply() (or the prior materialize)
                    # added.
                    if isinstance(params, dict) and "cacheRetention" in params:
                        del params["cacheRetention"]
                else:
                    if not isinstance(params, dict):
                        params = {}
                        mcfg["params"] = params
                    params["cacheRetention"] = prior_value

        # PR C — restore scalar fields (agents.defaults.sessionBudgetCapUsd).
        # Same reasoning as the catalog restore above: the materializer
        # only WRITES when an override exists; if revert deletes the
        # override the apply()-written scalar lingers. Restore the pre-apply
        # state by either replacing with the snapshot value or removing
        # the key entirely (when it was absent before).
        prior_scalars = snapshot.get("prior_scalar_values") or {}
        if prior_scalars:
            defaults_node = new_cfg.setdefault("agents", {}).setdefault("defaults", {})
            for scalar_dotpath, prior_val in prior_scalars.items():
                # Currently the only scalar covered is sessionBudgetCapUsd;
                # gate on the dotpath shape so future scalars added to
                # ``prior_scalar_values`` are routed by their own segment.
                if scalar_dotpath == "agents.defaults.sessionBudgetCapUsd":
                    if prior_val == _ABSENT:
                        defaults_node.pop("sessionBudgetCapUsd", None)
                    else:
                        defaults_node["sessionBudgetCapUsd"] = prior_val

        # Write the catalog-restored state. In test mode we write
        # directly; in production we route through safe_write_bot_config
        # + kickstart so OC reloads with the reverted params.
        if self.home_override is not None:
            oc_path.parent.mkdir(parents=True, exist_ok=True)
            oc_path.write_text(json.dumps(new_cfg, indent=2))
        else:
            try:
                from evolve_admin.deploy import safe_write_bot_config
            except ImportError as e:
                return RevertResult(
                    ok=False, details={"error": "deploy_unavailable",
                                       "exc": str(e)},
                    message=f"safe_write_bot_config unavailable: {e}",
                )
            ok, err = safe_write_bot_config(
                target_bot, new_cfg, reason="UpdateAgentDefaults.revert",
            )
            if not ok:
                return RevertResult(
                    ok=False, details={"write_error": err}, message=err,
                )
            # Scheduler-seam restart; failure (service not loaded) is not a
            # revert failure — same contract as the apply path.
            get_scheduler().restart(f"ai.openclaw.{target_bot}-gateway")

        # If a prior operator override was restored in the overrides
        # file, run the materializer one more time so the restored
        # override wins over the catalog-snapshot value we just wrote.
        # (Capture order: snapshot reads catalog BEFORE the apply, so
        # the catalog values reflect whatever the prior override
        # produced — re-materializing is mostly a defensive belt-and-
        # suspenders pass.)
        if restored:
            write_ok, msg = _materialize_and_write(
                target_bot, shared,
                home_override=self.home_override,
                kickstart=self.home_override is None,
                reason="UpdateAgentDefaults.revert.rematerialize",
            )
            if not write_ok:
                return RevertResult(
                    ok=False, details={"write_error": msg}, message=msg,
                )

        return RevertResult(
            ok=True,
            details={"bot_id": target_bot,
                     "restored_overrides": restored,
                     "deleted_overrides": deleted,
                     "fields_reverted": fields_changed},
            message=(
                f"Reverted agent defaults on {target_bot}: "
                f"{len(restored)} overrides restored, "
                f"{len(deleted)} deleted."
            ),
        )


register_applier("UpdateAgentDefaults", UpdateAgentDefaultsApplier())
