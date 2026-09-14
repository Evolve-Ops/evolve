"""arbiter.appliers.permissions — mutate the three permission surfaces.

Spec: internal/spec-permission-posture-2026-05-10.md §3.4 + §5.4.

Five appliers, one file:

  - ``UpdatePermissionConfig``  — openclaw.json field mutation (B1)
  - ``UpdateExecApproval``      — add/revoke an approved-command pattern
  - ``UpsertCronJob``           — add or modify a cron job
  - ``RemoveCronJob``           — remove a cron job by id
  - ``UpdatePermissionBaseline`` — pod baseline mutation (denylist,
    pod_default, per-bot overrides)

Each applier runs inline auto-reject guards before any file write.
See each class's docstring for the specific rejections; spec §5.4 is
authoritative.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from arbiter.appliers.base import ApplyResult, RevertResult, register_applier
from evolve_config import bot_home as _bot_home
from runtime.scheduler import get_scheduler
from schema.proposal import (
    UpdatePermissionConfig,
    UpdateExecApproval,
    UpsertCronJob,
    RemoveCronJob,
    UpdatePermissionBaseline,
)

# Permission-posture field set is now derived from the sandbox schema
# (config_sandbox/schema.py) — every OPENCLAW schema entry whose
# target_path is ``openclaw.json::<tools|commands>.<dotpath>`` is one
# of the fields this applier can write. The lookup runs lazily so a
# schema-only change (e.g. adding a new ``commands.*`` knob) doesn't
# require touching this module.
#
# Why schema-driven: Phase 3d cuts this applier over to the overrides
# file as the durable layer. ``config_sandbox.write_override`` already
# validates "is this a known schema path, is it per_bot, does the value
# type match" — duplicating the field list here would only let it drift
# out of sync. See internal/spec-openclaw-json-derived-artifact-2026-05-24.md
# §7 + memory note ``project_l1_l2_applier_architecture``.


def _permission_posture_field_map() -> dict[str, str]:
    """Return ``{openclaw_dotpath: schema_path}`` for every permission-posture
    field declared in the sandbox schema.

    Excludes the evolve-plugin block (those are owned by the
    materialize_evolve_plugin_config pass, not this applier) and any
    pod-wide (per_bot=False) entries. Cached at import time wouldn't be
    safe under test-driven schema mutation; the walk is cheap (~few
    dozen entries) so we just rebuild each call.
    """
    try:
        from evolve_admin.config_sandbox import SCHEMA, Store
    except ImportError:
        return {}
    _PLUGIN_PREFIX = "openclaw.json::plugins.entries.evolve."
    result: dict[str, str] = {}
    for entry in SCHEMA:
        if entry.store != Store.OPENCLAW:
            continue
        if not entry.per_bot:
            continue
        tp = entry.target_path
        if not tp.startswith("openclaw.json::"):
            continue
        if tp.startswith(_PLUGIN_PREFIX):
            continue
        dotpath = tp[len("openclaw.json::"):]
        # Permission-posture is specifically tools.* and commands.*.
        # Other openclaw schema entries (agents.defaults.model.primary)
        # are not touched by this applier — those go through other
        # appliers/CLI paths.
        head = dotpath.split(".", 1)[0]
        if head not in ("tools", "commands"):
            continue
        result[dotpath] = entry.path
    return result


def _dotpath_get(obj: dict, dotpath: str) -> Any:
    cur: Any = obj
    for seg in dotpath.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
        if cur is None:
            return None
    return cur


def _dotpath_set(obj: dict, dotpath: str, value: Any) -> None:
    parts = dotpath.split(".")
    cur = obj
    for seg in parts[:-1]:
        if seg not in cur or not isinstance(cur[seg], dict):
            cur[seg] = {}
        cur = cur[seg]
    cur[parts[-1]] = value


def _dotpath_unset(obj: dict, dotpath: str) -> None:
    parts = dotpath.split(".")
    cur = obj
    for seg in parts[:-1]:
        if not isinstance(cur, dict) or seg not in cur:
            return
        cur = cur[seg]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


def _post_apply_value(current: dict, fields: dict, dotpath: str) -> Any:
    """The value `dotpath` will hold after `fields` are applied to `current`."""
    if dotpath in fields:
        return fields[dotpath]
    return _dotpath_get(current, dotpath)


def _check_guards(current: dict, fields: dict) -> tuple[bool, str]:
    """Return (ok, error_message) — reject dangerous combinations.

    Evaluates the **post-apply** state: the union of current openclaw.json
    fields and the proposed updates. This catches the "split the dangerous
    combo across two proposals" pattern.
    """
    # Hard guard: ask=off is never allowed via this applier
    if fields.get("tools.exec.ask") == "off":
        return False, (
            "Refused: tools.exec.ask='off' removes the approval gate entirely. "
            "If you really want this posture, edit the pod baseline first."
        )

    # Composite combo: full + ask=off (read post-apply state)
    # Sandbox is no longer factored — see permissions/baseline.py rationale.
    # In practice no bot ever set sandbox.enabled=True (the path didn't exist),
    # so this guard previously fired whenever sec==full and ask==off, same as now.
    sec  = _post_apply_value(current, fields, "tools.exec.security")
    ask  = _post_apply_value(current, fields, "tools.exec.ask")
    if sec == "full" and ask == "off":
        return False, (
            "Refused: post-apply state would be security='full' + ask='off' "
            "(the 'open' posture). Run an explicit UpdatePermissionBaseline first."
        )

    return True, ""


def _read_current(bot_id: str, home_override: Path | None = None) -> dict | None:
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
    """Read current openclaw.json, apply policy-slice overrides, write back.

    Bridges the L2 applier (which writes overrides) to the materializer
    (which produces the openclaw.json). The deploy pipeline does the same
    via ``ensure_plugin_config`` — this is the on-demand variant that runs
    immediately after an RSI override write so the new posture takes
    effect without waiting for the next deploy.

    Production path: read the bot's openclaw.json via the writer's
    sudo-cat fallback, run ``materialize_openclaw_policy_slice``, write
    back through ``safe_write_bot_config`` (which validates against the
    OC schema), then kickstart the gateway.

    Test path (``home_override`` set): read+write directly against the
    synthetic ``home_override/.openclaw/openclaw.json`` tree, no
    validation shellout, no kickstart.
    """
    # Read current
    if home_override is not None:
        oc_path = home_override / ".openclaw" / "openclaw.json"
        from permissions.writer import read_openclaw_json
        current = read_openclaw_json(oc_path)
    else:
        # Production: resolve bot home via _bot_home (handles team_bot_b/personal_bot_user
        # case where bot_id ≠ account name; see memory note
        # ``feedback_bot_id_not_account_name``). bot_user kwarg, if supplied,
        # is forwarded to safe_write_bot_config later.
        from permissions.writer import read_openclaw_json
        home = _bot_home(target_bot)
        oc_path = home / ".openclaw" / "openclaw.json"
        current = read_openclaw_json(oc_path)
    if current is None:
        return False, f"openclaw.json unreadable at {oc_path}"

    # Materialize: apply per-bot overrides from sandbox/overrides/<bot>.json
    new_cfg = deepcopy(current)
    try:
        from evolve_admin.openclaw_materializer import materialize_openclaw_policy_slice
    except ImportError as e:
        return False, f"openclaw_materializer unavailable: {e}"
    materialize_openclaw_policy_slice(new_cfg, target_bot, shared_dir)

    # Write
    if home_override is not None:
        oc_path.parent.mkdir(parents=True, exist_ok=True)
        oc_path.write_text(json.dumps(new_cfg, indent=2))
        return True, f"Wrote {oc_path} directly (test mode)."

    # Production: validate-then-write via safe_write_bot_config, then kickstart.
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
        # restart can fail if the service is not loaded — not a write failure.
        get_scheduler().restart(f"ai.openclaw.{target_bot}-gateway")

    return True, f"Materialized + wrote openclaw.json for {target_bot}."


def _record_applier_intents(
    *, bot_id: str, fields: dict, proposal_id: str | None,
    shared_dir: Path,
) -> None:
    """Record one config-intent per field after a successful applier write.

    Spec internal/spec-config-intent-system-2026-05-21.md §3 — every code path
    that mutates a permission-config field calls ``set_intent``. For the
    applier path, ``set_by`` encodes the proposal id (``applier:<pid>``);
    Phase 2 ships a placeholder reason that references the proposal — Phase
    3's inference layer will replace this with model-derived reasons later.

    Best-effort: any failure here is logged but does not undo the
    successful config write. The intent record is hygiene metadata; the
    underlying openclaw.json change is the source of truth.
    """
    try:
        from evolve_admin.config_intent import set_intent
    except ImportError:
        return
    set_by = f"applier:{proposal_id}" if proposal_id else "applier"
    reason = (
        f"Applied via proposal {proposal_id}"
        if proposal_id
        else "Applied via UpdatePermissionConfigApplier"
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


class UpdatePermissionConfigApplier:
    """Mutate a bot's permission posture via the sandbox overrides file.

    Spec: internal/spec-openclaw-json-derived-artifact-2026-05-24.md §7
    (Phase 3d), supersedes the L1 direct-write design pre-#1430.

    What happens on ``apply``:

      1. Validate each field path against the sandbox schema (replaces
         the prior ``_ALLOWED_FIELDS`` whitelist — schema is the source of
         truth now).
      2. Run the post-apply guards (``ask=off`` refused; composite
         ``security=full + ask=off`` refused) against (current + proposed).
      3. ``config_sandbox.write_override(... set_by="rsi:<proposal_id>")``
         for each field — the durable layer.
      4. Materialize: apply the overrides to a fresh copy of openclaw.json
         and write through ``safe_write_bot_config`` (validates against the
         OC schema before writing).
      5. Kickstart the bot's gateway so the new posture takes effect.

    What happens on ``revert``:

      For each field touched, either restore the prior override entry
      (operator/migration value the RSI write replaced) or delete the
      override (if no prior override existed and the field was originally
      unset). Then materialize + write so openclaw.json matches.

    Test hooks:

      ``home_override`` bypasses ``/Users/<bot>`` and goes direct.
      ``shared_override`` points the overrides file at a test path.
    """

    # Optional test hooks — set to Paths to bypass production resolution.
    home_override: Path | None = None
    shared_override: Path | None = None

    def _resolve_shared(self) -> Path:
        return self.shared_override or _shared_dir()

    def capture_snapshot(self, action: UpdatePermissionConfig, bot_id: str) -> dict:
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
            # Overrides file unreadable — capture empty. Revert will then
            # be a best-effort "remove the RSI-written override" pass.
            prior_overrides_dict = {}

        # Per-field prior context: openclaw.json value (for diagnostics)
        # AND the prior override entry (if any) keyed by schema path.
        field_map = _permission_posture_field_map()
        prior_values: dict[str, Any] = {}
        prior_overrides_for_fields: dict[str, dict] = {}
        for dotpath in (action.fields or {}).keys():
            prior_values[dotpath] = _dotpath_get(current, dotpath)
            schema_path = field_map.get(dotpath)
            if schema_path and schema_path in prior_overrides_dict:
                prior_overrides_for_fields[schema_path] = prior_overrides_dict[schema_path]

        return {
            "action_kind": "UpdatePermissionConfig",
            "bot_id": target_bot,
            "fields_changed": list((action.fields or {}).keys()),
            "prior_values": prior_values,
            # schema_path → prior OverrideEntry dict (only fields that
            # had a recorded override before this apply); used by revert
            # to distinguish "restore prior override" from "delete our
            # RSI entry."
            "prior_overrides": prior_overrides_for_fields,
        }

    def apply(
        self,
        action: UpdatePermissionConfig,
        bot_id: str,
        *,
        proposal_id: str | None = None,
    ) -> ApplyResult:
        target_bot = action.bot_id or bot_id
        fields = action.fields or {}

        if not fields:
            return ApplyResult(
                ok=False, details={"error": "no_fields"},
                message="UpdatePermissionConfig.fields is empty; nothing to apply.",
            )

        # 1) Validate against schema. Replaces _ALLOWED_FIELDS — the
        # schema is now the source of truth for which paths are tunable.
        field_map = _permission_posture_field_map()
        unknown = sorted(set(fields.keys()) - set(field_map.keys()))
        if unknown:
            return ApplyResult(
                ok=False, details={"unknown_fields": unknown},
                message=f"Refused: field(s) outside permission-config set: {unknown}",
            )

        # 2) Guards on post-apply state. Read current openclaw.json so the
        # composite guard (security=full + ask=off) considers the union
        # of stored state and proposed mutation.
        current = _read_current(target_bot, self.home_override)
        if current is None:
            return ApplyResult(
                ok=False, details={"error": "openclaw_unreadable"},
                message=f"Cannot read openclaw.json for {target_bot}.",
            )
        ok, err = _check_guards(current, fields)
        if not ok:
            return ApplyResult(
                ok=False, details={"guard_violation": True, "guard": err}, message=err,
            )

        # 3) Write overrides — durable layer. set_by encodes the proposal
        # id when present so the audit log links back to the originating
        # RSI proposal (Customizations page surfaces this).
        shared = self._resolve_shared()
        set_by = f"rsi:{proposal_id}" if proposal_id else "rsi"
        try:
            from evolve_admin.config_sandbox import write_override
        except ImportError as e:
            return ApplyResult(
                ok=False, details={"error": "config_sandbox_unavailable", "exc": str(e)},
                message=f"config_sandbox not importable: {e}",
            )
        # Partial-write recovery: if write_override raises on the Nth field,
        # the first N-1 already landed in the overrides file. arbiter.apply
        # doesn't call revert on result.ok=False (only on later verification
        # failure), so those partial writes would leak into the next deploy
        # via the policy-slice pass. Roll them back explicitly here.
        # Schema validation catches type/path errors upfront via _validate_write
        # in write_override; realistic raises here are filesystem / lock /
        # state errors, which are rare but worth catching.
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
                # Clean up the partial writes from this loop.
                if _delete_override is not None:
                    for prior_schema_path in written:
                        try:
                            _delete_override(shared, target_bot, prior_schema_path)
                        except Exception:
                            # Best-effort cleanup; further failures here are
                            # logged via the materializer's own paths.
                            pass
                return ApplyResult(
                    ok=False, details={"error": "override_write_failed",
                                       "field": dotpath, "exc": str(e),
                                       "rolled_back": list(written)},
                    message=f"Failed to write override for {dotpath}: {e}",
                )
            written.append(schema_path)

        # 4+5) Materialize + write + kickstart so the new posture takes
        # effect immediately. The deploy pipeline (ensure_plugin_config →
        # materialize_openclaw_policy_slice) reproduces the same result on
        # the next deploy; this is the on-demand variant.
        write_ok, msg = _materialize_and_write(
            target_bot, shared,
            home_override=self.home_override,
            kickstart=self.home_override is None,
            reason=f"UpdatePermissionConfig({proposal_id or 'no-proposal'})",
        )
        if not write_ok:
            return ApplyResult(ok=False, details={"write_error": msg}, message=msg)

        # record_intent_on_set reflex (spec internal/spec-config-intent-system-2026-05-21.md §3):
        # after a successful write, annotate each touched field with an
        # intent record so auth_drift_filler and friends know this is a
        # deliberate, motivated change — not drift to revert. Best-effort:
        # a recording failure must not undo the successful config write
        # (the underlying file is the source of truth; the intent record
        # is a hygiene annotation).
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
            message=f"Updated permission config on {target_bot}: {sorted(fields.keys())}",
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        target_bot = snapshot.get("bot_id") or bot_id
        prior_values = snapshot.get("prior_values") or {}
        prior_overrides = snapshot.get("prior_overrides") or {}
        if not prior_values:
            return RevertResult(
                ok=False, details={"reason": "no_prior_values"},
                message="No prior values captured; cannot revert.",
            )

        field_map = _permission_posture_field_map()
        shared = self._resolve_shared()
        try:
            from evolve_admin.config_sandbox import (
                delete_override,
                write_override,
            )
        except ImportError as e:
            return RevertResult(
                ok=False, details={"error": "config_sandbox_unavailable", "exc": str(e)},
                message=f"config_sandbox not importable: {e}",
            )

        # Two parallel tracks for revert:
        #
        #   1. Restore the OVERRIDES FILE to its pre-apply state so the
        #      next deploy's materialize_openclaw_policy_slice pass produces
        #      the right openclaw.json. For each field, either restore the
        #      prior override entry (if one existed before apply) or delete
        #      the RSI-written entry (if apply created it from scratch).
        #
        #   2. Restore the openclaw.json IMMEDIATELY by writing the captured
        #      prior_value verbatim (or unsetting the field if it didn't
        #      exist pre-apply). The materializer doesn't synthesize a
        #      "no override → unset the field" rule on its own; without
        #      this direct restoration, fields like ``tools.fs.workspaceOnly``
        #      that were absent pre-apply would linger after revert.
        #
        # Both tracks must run — track 1 alone leaves the on-disk file
        # stale until the next deploy; track 2 alone leaves the overrides
        # file out of sync with on-disk state and the next materialize
        # would re-introduce the RSI value.
        restored: list[str] = []
        deleted: list[str] = []
        for dotpath in prior_values.keys():
            schema_path = field_map.get(dotpath)
            if schema_path is None:
                # Schema rename / removal between apply and revert — fall
                # back to nothing; openclaw.json materialize won't write
                # the missing field.
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
                    # Best-effort: log + continue. We don't fail revert if
                    # one of the restorations didn't validate (e.g. the
                    # prior value's type drifted) — that lands on the
                    # operator's review queue via persistence_failed Signal.
                    continue
                restored.append(schema_path)
            else:
                if delete_override(shared, target_bot, schema_path):
                    deleted.append(schema_path)

        # Direct on-disk restore: read current openclaw.json, set/unset
        # each field to its captured prior value, write back.
        if self.home_override is not None:
            oc_path = self.home_override / ".openclaw" / "openclaw.json"
        else:
            oc_path = _bot_home(target_bot) / ".openclaw" / "openclaw.json"
        from permissions.writer import read_openclaw_json
        current = read_openclaw_json(oc_path)
        if current is None:
            return RevertResult(
                ok=False, details={"error": "openclaw_unreadable"},
                message=f"openclaw.json unreadable at {oc_path}; revert can't write.",
            )
        new_cfg = deepcopy(current)
        for dotpath, prior_val in prior_values.items():
            if prior_val is None:
                _dotpath_unset(new_cfg, dotpath)
            else:
                _dotpath_set(new_cfg, dotpath, prior_val)

        if self.home_override is not None:
            oc_path.parent.mkdir(parents=True, exist_ok=True)
            oc_path.write_text(json.dumps(new_cfg, indent=2))
        else:
            try:
                from evolve_admin.deploy import safe_write_bot_config
            except ImportError as e:
                return RevertResult(
                    ok=False, details={"error": "deploy_unavailable", "exc": str(e)},
                    message=f"safe_write_bot_config unavailable: {e}",
                )
            ok, err = safe_write_bot_config(
                target_bot, new_cfg, reason="UpdatePermissionConfig.revert",
            )
            if not ok:
                return RevertResult(ok=False, details={"write_error": err}, message=err)
            # Scheduler-seam restart; failure (service not loaded) is not a
            # revert failure — same contract as the apply path above.
            get_scheduler().restart(f"ai.openclaw.{target_bot}-gateway")

        return RevertResult(
            ok=True,
            details={"bot_id": target_bot,
                     "restored_overrides": restored,
                     "deleted_overrides": deleted,
                     "restored_fields": sorted(prior_values.keys())},
            message=(
                f"Reverted permission config on {target_bot}: "
                f"{len(restored)} overrides restored, {len(deleted)} deleted."
            ),
        )


register_applier("UpdatePermissionConfig", UpdatePermissionConfigApplier())


# ─────────────────────────────────────────────────────────────────────────────
# UpdateExecApproval — add/revoke approved-command patterns
# ─────────────────────────────────────────────────────────────────────────────

def _shared_dir() -> Path:
    """Resolve shared_dir from env or default."""
    import os
    candidates = [os.environ.get("EVOLVE_SHARED_DIR"), "/Users/Shared/evolve"]
    for c in candidates:
        if c and Path(c).exists():
            return Path(c)
    return Path("/Users/Shared/evolve")


def _denylist(surface: str, shared_override: Path | None = None) -> list[str]:
    """Load operator-curated denylist patterns from the baseline."""
    from permissions import baseline as _bl
    shared = shared_override or _shared_dir()
    return _bl.denylist_for(_bl.load(shared), surface)


def _approval_matches_denylist(pattern: str, denylist_patterns: list[str]) -> str | None:
    """Return the first matching denylist regex, or None."""
    import re
    from permissions.inventory import _canonicalize_pattern
    candidates = [pattern, _canonicalize_pattern(pattern)]
    for rule in denylist_patterns:
        try:
            rx = re.compile(rule)
        except re.error:
            continue
        if any(rx.search(c) for c in candidates):
            return rule
    return None


class UpdateExecApprovalApplier:
    """Add or revoke an exec-approval pattern.

    Refuses ``add`` if the pattern matches the approval denylist.
    """

    home_override: Path | None = None
    shared_override: Path | None = None

    def _read_or_init(self, action: UpdateExecApproval) -> dict:
        from permissions.writer import read_exec_approvals
        obj = read_exec_approvals(action.bot_id, home_override=self.home_override)
        return obj if isinstance(obj, dict) else {"defaults": {}, "agents": {}}

    def capture_snapshot(self, action: UpdateExecApproval, bot_id: str) -> dict:
        prior = self._read_or_init(action)
        return {
            "action_kind": "UpdateExecApproval",
            "bot_id": action.bot_id,
            "operation": action.operation,
            "scope": action.scope,
            "agent_id": action.agent_id,
            "pattern": action.pattern,
            "prior_file": deepcopy(prior),
        }

    def apply(self, action: UpdateExecApproval, bot_id: str) -> ApplyResult:
        from permissions.writer import write_exec_approvals
        from permissions.inventory import _canonicalize_pattern

        target = action.bot_id or bot_id
        pat = (action.pattern or "").strip()
        if not pat:
            return ApplyResult(ok=False, details={"error": "empty_pattern"},
                               message="pattern is required")
        if action.operation not in ("add", "revoke"):
            return ApplyResult(ok=False, details={"error": "bad_operation"},
                               message=f"unknown operation: {action.operation!r}")

        obj = self._read_or_init(action)

        if action.operation == "add":
            # Auto-reject: deny patterns
            deny = _denylist("approvals", shared_override=self.shared_override)
            hit = _approval_matches_denylist(pat, deny)
            if hit:
                return ApplyResult(
                    ok=False, details={"denylist_rule": hit, "pattern": pat},
                    message=f"Refused: pattern matches approval denylist rule {hit!r}.",
                )

            if action.scope == "defaults":
                defaults = obj.setdefault("defaults", {})
                if isinstance(defaults, list):
                    if pat not in defaults:
                        defaults.append(pat)
                else:
                    defaults[pat] = {}
            else:
                agents = obj.setdefault("agents", {})
                agent_entry = agents.setdefault(action.agent_id, {})
                approvals = agent_entry.setdefault("approvals", {})
                if isinstance(approvals, list):
                    if pat not in approvals:
                        approvals.append(pat)
                else:
                    approvals[pat] = {}

        else:  # revoke
            target_canonical = _canonicalize_pattern(pat)

            def _matches(raw: str) -> bool:
                if raw == pat:
                    return True
                return _canonicalize_pattern(raw) == target_canonical

            removed: list[str] = []
            if action.scope == "defaults":
                defaults = obj.get("defaults") or {}
                if isinstance(defaults, dict):
                    obj["defaults"] = {k: v for k, v in defaults.items() if not _matches(k) or removed.append(k)}
                elif isinstance(defaults, list):
                    obj["defaults"] = [x for x in defaults if not _matches(x) or removed.append(x)]
            else:
                agents = obj.get("agents") or {}
                agent_entry = agents.get(action.agent_id) or {}
                approvals = agent_entry.get("approvals")
                if isinstance(approvals, dict):
                    agent_entry["approvals"] = {k: v for k, v in approvals.items() if not _matches(k) or removed.append(k)}
                elif isinstance(approvals, list):
                    agent_entry["approvals"] = [x for x in approvals if not _matches(x) or removed.append(x)]
                if "agents" not in obj:
                    obj["agents"] = {}
                obj["agents"][action.agent_id] = agent_entry

            if not removed:
                return ApplyResult(
                    ok=True, details={"no_op": True, "pattern": pat},
                    message=f"Pattern {pat!r} not present; no change.",
                )

        ok, msg = write_exec_approvals(
            target, obj,
            home_override=self.home_override,
            kickstart_gateway=self.home_override is None,
        )
        if not ok:
            return ApplyResult(ok=False, details={"write_error": msg}, message=msg)

        return ApplyResult(
            ok=True,
            details={"operation": action.operation, "pattern": pat,
                     "scope": action.scope, "agent_id": action.agent_id},
            message=f"{action.operation.capitalize()}d exec-approval {pat!r} on {target}.",
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        from permissions.writer import write_exec_approvals
        target = snapshot.get("bot_id") or bot_id
        prior = snapshot.get("prior_file")
        if not isinstance(prior, dict):
            return RevertResult(ok=False, message="No prior file captured; cannot revert.")
        ok, msg = write_exec_approvals(
            target, prior,
            home_override=self.home_override,
            kickstart_gateway=self.home_override is None,
        )
        if not ok:
            return RevertResult(ok=False, details={"write_error": msg}, message=msg)
        return RevertResult(
            ok=True, details={"bot_id": target},
            message=f"Reverted exec-approvals.json on {target} to prior state.",
        )


register_applier("UpdateExecApproval", UpdateExecApprovalApplier())


# ─────────────────────────────────────────────────────────────────────────────
# UpsertCronJob — add or modify a cron job
# ─────────────────────────────────────────────────────────────────────────────

def _job_has_required_caps(job: dict) -> tuple[bool, bool]:
    """Return (has_turn_cap, has_budget_cap) for an agentTurn job."""
    def _gt0(*chains):
        for chain in chains:
            cur: Any = job
            for seg in chain:
                if not isinstance(cur, dict):
                    cur = None
                    break
                cur = cur.get(seg)
            if isinstance(cur, (int, float)) and cur > 0:
                return True
        return False

    turn = _gt0(("payload", "maxTurns"), ("payload", "turnCap"), ("maxTurns",), ("turnCap",))
    budget = _gt0(("payload", "maxBudgetUsd"), ("payload", "budgetUsd"),
                  ("maxBudgetUsd",), ("budgetUsd",))
    return turn, budget


def _job_payload_text(job: dict) -> str:
    p = job.get("payload") or {}
    if not isinstance(p, dict):
        return ""
    return str(p.get("message") or p.get("command") or p.get("event") or "")


def _cron_payload_matches_denylist(job: dict, denylist_patterns: list[str]) -> str | None:
    import re
    text = _job_payload_text(job)
    for rule in denylist_patterns:
        try:
            rx = re.compile(rule)
        except re.error:
            continue
        if rx.search(text):
            return rule
    return None


def _bot_owned_cron_read_or_init(
    bot_id: str, *, home_override: Path | None = None,
) -> dict:
    from permissions.writer import read_cron_jobs
    obj = read_cron_jobs(bot_id, home_override=home_override)
    return obj if isinstance(obj, dict) else {"jobs": []}


class UpsertCronJobApplier:
    """Add or modify a cron job in .openclaw/cron/jobs.json.

    Refuses uncapped agentTurn payloads and denylist matches.
    """

    home_override: Path | None = None
    shared_override: Path | None = None

    def capture_snapshot(self, action: UpsertCronJob, bot_id: str) -> dict:
        prior = _bot_owned_cron_read_or_init(action.bot_id, home_override=self.home_override)
        return {
            "action_kind": "UpsertCronJob",
            "bot_id": action.bot_id,
            "job_id": (action.job or {}).get("id"),
            "prior_file": deepcopy(prior),
        }

    def apply(self, action: UpsertCronJob, bot_id: str) -> ApplyResult:
        from permissions.writer import write_cron_jobs
        target = action.bot_id or bot_id
        job = action.job or {}
        if not isinstance(job, dict) or not job.get("id"):
            return ApplyResult(
                ok=False, details={"error": "missing_job_id"},
                message="UpsertCronJob.job must be a dict with non-empty id",
            )

        # Auto-reject: uncapped agentTurn
        payload = job.get("payload") or {}
        payload_kind = payload.get("kind") if isinstance(payload, dict) else None
        if payload_kind == "agentTurn":
            has_turn, has_budget = _job_has_required_caps(job)
            if not (has_turn and has_budget):
                return ApplyResult(
                    ok=False, details={"missing_caps": True, "has_turn": has_turn, "has_budget": has_budget},
                    message="Refused: agentTurn cron job must include both maxTurns and maxBudgetUsd.",
                )

        # Auto-reject: cron denylist
        deny = _denylist("cron", shared_override=self.shared_override)
        hit = _cron_payload_matches_denylist(job, deny)
        if hit:
            return ApplyResult(
                ok=False, details={"denylist_rule": hit},
                message=f"Refused: cron payload matches denylist rule {hit!r}.",
            )

        obj = _bot_owned_cron_read_or_init(target, home_override=self.home_override)
        jobs = obj.setdefault("jobs", [])
        if not isinstance(jobs, list):
            return ApplyResult(
                ok=False, details={"error": "jobs_not_list"},
                message="cron/jobs.json 'jobs' field is not a list",
            )

        # Replace existing-by-id, else append
        replaced = False
        for i, existing in enumerate(jobs):
            if isinstance(existing, dict) and existing.get("id") == job["id"]:
                jobs[i] = job
                replaced = True
                break
        if not replaced:
            jobs.append(job)

        ok, msg = write_cron_jobs(
            target, obj,
            home_override=self.home_override,
            kickstart_gateway=self.home_override is None,
        )
        if not ok:
            return ApplyResult(ok=False, details={"write_error": msg}, message=msg)
        return ApplyResult(
            ok=True,
            details={"job_id": job["id"], "replaced": replaced},
            message=f"{'Updated' if replaced else 'Added'} cron job {job['id']!r} on {target}.",
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        from permissions.writer import write_cron_jobs
        target = snapshot.get("bot_id") or bot_id
        prior = snapshot.get("prior_file")
        if not isinstance(prior, dict):
            return RevertResult(ok=False, message="No prior file captured; cannot revert.")
        ok, msg = write_cron_jobs(
            target, prior,
            home_override=self.home_override,
            kickstart_gateway=self.home_override is None,
        )
        if not ok:
            return RevertResult(ok=False, details={"write_error": msg}, message=msg)
        return RevertResult(
            ok=True, details={"bot_id": target},
            message=f"Reverted cron/jobs.json on {target}.",
        )


register_applier("UpsertCronJob", UpsertCronJobApplier())


# ─────────────────────────────────────────────────────────────────────────────
# RemoveCronJob — remove a cron job by id
# ─────────────────────────────────────────────────────────────────────────────

class RemoveCronJobApplier:
    home_override: Path | None = None

    def capture_snapshot(self, action: RemoveCronJob, bot_id: str) -> dict:
        prior = _bot_owned_cron_read_or_init(action.bot_id, home_override=self.home_override)
        return {
            "action_kind": "RemoveCronJob",
            "bot_id": action.bot_id,
            "job_id": action.job_id,
            "prior_file": deepcopy(prior),
        }

    def apply(self, action: RemoveCronJob, bot_id: str) -> ApplyResult:
        from permissions.writer import write_cron_jobs
        target = action.bot_id or bot_id
        if not action.job_id:
            return ApplyResult(ok=False, details={"error": "missing_job_id"},
                               message="RemoveCronJob.job_id is required")
        obj = _bot_owned_cron_read_or_init(target, home_override=self.home_override)
        jobs = obj.get("jobs") or []
        if not isinstance(jobs, list):
            return ApplyResult(ok=False, details={"error": "jobs_not_list"},
                               message="cron/jobs.json 'jobs' field is not a list")
        before = len(jobs)
        obj["jobs"] = [j for j in jobs if not (isinstance(j, dict) and j.get("id") == action.job_id)]
        if len(obj["jobs"]) == before:
            return ApplyResult(
                ok=True, details={"no_op": True, "job_id": action.job_id},
                message=f"Job {action.job_id!r} not present; no change.",
            )
        ok, msg = write_cron_jobs(
            target, obj,
            home_override=self.home_override,
            kickstart_gateway=self.home_override is None,
        )
        if not ok:
            return ApplyResult(ok=False, details={"write_error": msg}, message=msg)
        return ApplyResult(
            ok=True, details={"job_id": action.job_id},
            message=f"Removed cron job {action.job_id!r} from {target}.",
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        from permissions.writer import write_cron_jobs
        target = snapshot.get("bot_id") or bot_id
        prior = snapshot.get("prior_file")
        if not isinstance(prior, dict):
            return RevertResult(ok=False, message="No prior file captured; cannot revert.")
        ok, msg = write_cron_jobs(
            target, prior,
            home_override=self.home_override,
            kickstart_gateway=self.home_override is None,
        )
        if not ok:
            return RevertResult(ok=False, details={"write_error": msg}, message=msg)
        return RevertResult(
            ok=True, details={"bot_id": target},
            message=f"Reverted cron/jobs.json on {target} (restored removed job).",
        )


register_applier("RemoveCronJob", RemoveCronJobApplier())


# ─────────────────────────────────────────────────────────────────────────────
# UpdatePermissionBaseline — pod baseline mutation
# ─────────────────────────────────────────────────────────────────────────────

class UpdatePermissionBaselineApplier:
    """Mutate {shared_dir}/policy/permission-baseline.json.

    Refuses denylist removals (only additions allowed in v1, spec §5.4).
    """

    shared_override: Path | None = None

    def _shared(self) -> Path:
        return self.shared_override or _shared_dir()

    def capture_snapshot(self, action: UpdatePermissionBaseline, bot_id: str) -> dict:
        from permissions import baseline as _bl
        prior = _bl.load(self._shared())
        return {
            "action_kind": "UpdatePermissionBaseline",
            "operation": action.operation,
            "bot_id": action.bot_id,
            "fields": deepcopy(action.fields),
            "prior_baseline": deepcopy(prior),
        }

    def apply(self, action: UpdatePermissionBaseline, bot_id: str) -> ApplyResult:
        from permissions import baseline as _bl

        op = action.operation
        if op not in ("set_pod_default", "set_bot_override", "set_denylist_patterns"):
            return ApplyResult(ok=False, details={"error": "bad_operation"},
                               message=f"unknown operation: {op!r}")

        baseline = _bl.load(self._shared())

        if op == "set_pod_default":
            pc = baseline.setdefault("pod_default", {}).setdefault("permission_config", {})
            for k, v in (action.fields or {}).items():
                pc[k] = v

        elif op == "set_bot_override":
            target_bot = (action.bot_id or "").strip()
            if not target_bot:
                return ApplyResult(ok=False, details={"error": "missing_bot_id"},
                                   message="set_bot_override requires bot_id")
            overrides = baseline.setdefault("per_bot_overrides", {})
            if not action.fields:
                overrides.pop(target_bot, None)
            else:
                overrides[target_bot] = {"permission_config": dict(action.fields)}

        else:  # set_denylist_patterns
            surface = action.fields.get("surface")
            patterns = action.fields.get("patterns")
            if surface not in ("approvals", "cron"):
                return ApplyResult(ok=False, details={"error": "bad_surface"},
                                   message="surface must be 'approvals' or 'cron'")
            if not isinstance(patterns, list):
                return ApplyResult(ok=False, details={"error": "bad_patterns"},
                                   message="patterns must be a list")
            current = _bl.denylist_for(baseline, surface)
            removed = [p for p in current if p not in patterns]
            if removed:
                return ApplyResult(
                    ok=False, details={"removed_rules": removed},
                    message=f"Refused: removing denylist rule(s) {removed!r}. v1 allows additions only.",
                )
            pod_default = baseline.setdefault("pod_default", {})
            key = "exec_approvals" if surface == "approvals" else "scheduled_invocations"
            pod_default.setdefault(key, {})["denylist_patterns"] = [str(p) for p in patterns]

        _bl.write(baseline, self._shared())
        return ApplyResult(
            ok=True,
            details={"operation": op},
            message=f"Updated permission baseline ({op}).",
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        from permissions import baseline as _bl
        prior = snapshot.get("prior_baseline")
        if not isinstance(prior, dict):
            return RevertResult(ok=False, message="No prior baseline captured.")
        _bl.write(prior, self._shared())
        return RevertResult(
            ok=True, details={"operation": snapshot.get("operation")},
            message="Reverted permission baseline.",
        )


register_applier("UpdatePermissionBaseline", UpdatePermissionBaselineApplier())
