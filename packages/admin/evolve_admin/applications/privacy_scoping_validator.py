"""
privacy_scoping_validator — structural gate + projection for the manifest
``privacy{}`` and ``audience_scoping{}`` blocks (manifest-v7 Slice 2).

Spec: internal/spec-manifest-v7-slicing-2026-06-10.md §4; field shapes are the
base spec's §3 Atlas Gaps 3/4 with the §11.1 decision — **pinned structure,
open vocabulary**. The pinned structure here mirrors
docs/schemas/manifest-v7-spec.schema.json exactly (key sets, types, the
``operator`` enum, ``retention_days >= 1``); the *vocabulary* of role names,
surface ids, and bypass identifiers stays open in v1 and is owned by the
autonomy-ladder track (do not add a surface-id enum here — that would mint
a second id space).

Three checks, matching the slicing spec §4.1:

1. **Structural keys when a block is present.** An absent or empty block
   means "not yet declared" and always passes — no flag day for legacy
   manifests (§4.3). A *present* block must carry the pinned key set with
   the pinned types; ``audience_scoping`` additionally requires
   ``operator`` / ``approved_surfaces`` / ``role_capabilities`` (the JSON
   schema's required trio — ``operator_bypasses`` is optional there and
   stays optional here).

2. **Trigger-audience conformance.** ``event_triggers[].audience`` must
   name a key in ``audience_scoping.role_capabilities`` — this closes the
   unpinned-free-string gap Slice 1 left (slicing spec §6.2). Severity is
   asymmetric by design: when ``audience_scoping`` IS declared, a
   non-conforming audience is a ``build_blocker`` (the operator pinned a
   vocabulary; the trigger violates it). When the block is absent, audience
   values are merely flagged ``info`` (declaring the block is the opt-in;
   blocking here would be the flag day §4.3 forbids).

3. **Group-surface consent.** An app declaring ``event_triggers[]`` on a
   group surface (``match.channel`` in GROUP_CHANNELS — including ``any``,
   which matches group channels by definition) must carry
   ``privacy.consent_notice``: people in a group chat the operator doesn't
   control individually are having their messages pattern-matched, so the
   app must have something to tell them. Declaring a group-surface trigger
   is the opt-in that activates this gate.

Wired at the same two checkpoints as ``scheduled_actions_validator`` /
``bot_guidance_freelance_validator``:

  * ``gallery.preflight_check`` — embeds a ``build_blocker`` requirement
    row so the install dispatch refuses with a remediation message (and an
    ``info`` row surfacing the consent notice to the operator).
  * ``forge_engine.run_forge_job`` Step 1e + Phase 5e — pre-build gate and
    post-reconciliation re-validation.

Deliberately NOT here (slicing spec §4.2): gateway-level enforcement of
``role_capabilities`` beyond the trigger-audience check, and
``retention_days`` automation. Declared now, enforced later.

This module also hosts the canonical *inferred defaults*
(``default_privacy_block`` / ``default_audience_scoping_block``) shared by
the v7 migration inference, the backfill pass, and forge's fresh-install
seeding — one source of truth so pre- and post-v24 artifacts agree — and
``project_data_boundary``, the structured "what this app collects, who can
reach it" projection the security audit surface renders (U4.3's data
source; structured fields, not plain-language bullets — that pattern was
retired, see internal/roadmap-user-value-2026-06-10.md U4.3).
"""

from __future__ import annotations

from typing import Any, TypeGuard


# ── Pinned structure (mirrors manifest-v7-spec.schema.json) ──────────────────

PRIVACY_KEYS: frozenset[str] = frozenset({
    "user_data_collected",
    "opt_out_command",
    "consent_notice",
    "retention_days",
    "shareable_in_lessons",
})

AUDIENCE_SCOPING_KEYS: frozenset[str] = frozenset({
    "operator",
    "approved_surfaces",
    "role_capabilities",
    "operator_bypasses",
})

# The JSON schema's `required` trio. operator_bypasses is optional there.
AUDIENCE_SCOPING_REQUIRED: tuple[str, ...] = (
    "operator", "approved_surfaces", "role_capabilities",
)

OPERATOR_VALUES: tuple[str, ...] = ("operator_only", "named_users", "open")

# match.channel values that reach people beyond the operator's own DM.
# Subset of the channel enum in manifest-v7-spec.schema.json. "any" matches
# every channel — including group ones — so it counts as group-reaching.
GROUP_CHANNELS: frozenset[str] = frozenset({
    "telegram_group",
    "slack_channel",
    "discord_channel",
    "any",
})


# ── Canonical inferred defaults ──────────────────────────────────────────────
#
# Shared by migrate_v7._infer_privacy / _infer_audience_scoping, the
# migrate_v7_backfill re-stamp pass, and forge_engine's fresh-install
# seeding. Conservative direction on every axis: collect-nothing declared,
# lessons sharing off, operator-only audience.


def default_privacy_block() -> dict:
    """The inferred privacy{} default for an app that declared nothing."""
    return {
        "user_data_collected": [],
        "retention_days": 365,
        "shareable_in_lessons": False,
    }


def default_audience_scoping_block() -> dict:
    """The inferred audience_scoping{} default — operator-only boundary."""
    return {
        "operator": "operator_only",
        "approved_surfaces": [],
        "role_capabilities": {
            "operator_only": ["read", "write", "configure"],
        },
        "operator_bypasses": [],
    }


def seed_privacy_scoping_defaults(
    manifest: Any,
    *,
    privacy_overrides: dict | None = None,
    audience_scoping_override: dict | None = None,
) -> bool:
    """Fill missing privacy{} / audience_scoping{} with the canonical defaults.

    The shared seeding step every NEW-app mint site calls (manifest-v7
    Slice 3a: create-app drafts acquire the blocks at mint time, the
    same defaults the v7 migration / forge gallery seeding write, so all
    paths agree). Accepts a manifest dict or an ApplicationManifest;
    fill-if-missing only — declared blocks are never overwritten.
    Returns True when anything was seeded.

    Overrides (add-bot wizard, delta spec §7 + the v24 update): when a
    mint site carries the operator's consent answers, they overlay the
    defaults being filled — ``privacy_overrides`` keys (filtered to
    ``PRIVACY_KEYS``) merge onto the default privacy block, and a
    non-empty ``audience_scoping_override`` replaces the default
    scoping block wholesale. A declared block still wins outright;
    overrides only shape what fills the gap.
    """
    def _privacy_block() -> dict:
        block = default_privacy_block()
        if isinstance(privacy_overrides, dict):
            block.update({
                k: v for k, v in privacy_overrides.items()
                if k in PRIVACY_KEYS
            })
        return block

    def _scoping_block() -> dict:
        if isinstance(audience_scoping_override, dict) and audience_scoping_override:
            return dict(audience_scoping_override)
        return default_audience_scoping_block()

    changed = False
    if isinstance(manifest, dict):
        if not manifest.get("privacy"):
            manifest["privacy"] = _privacy_block()
            changed = True
        if not manifest.get("audience_scoping"):
            manifest["audience_scoping"] = _scoping_block()
            changed = True
    else:
        if not getattr(manifest, "privacy", None):
            manifest.privacy = _privacy_block()
            changed = True
        if not getattr(manifest, "audience_scoping", None):
            manifest.audience_scoping = _scoping_block()
            changed = True
    return changed


# ── Field extraction ─────────────────────────────────────────────────────────


def _get(pkg_or_manifest: Any, key: str) -> Any:
    if isinstance(pkg_or_manifest, dict):
        return pkg_or_manifest.get(key)
    return getattr(pkg_or_manifest, key, None)


def _is_str_list(value: Any) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


# ── Structural checks ────────────────────────────────────────────────────────


def _validate_privacy_block(privacy: Any) -> list[str]:
    """Pinned-structure errors for a *present* privacy block."""
    if not isinstance(privacy, dict):
        return [f"privacy must be an object, got {type(privacy).__name__}"]

    errors: list[str] = []
    unknown = sorted(set(privacy) - PRIVACY_KEYS)
    if unknown:
        errors.append(
            f"privacy has unknown key(s): {', '.join(unknown)}. The structure "
            f"is pinned (base-spec §11.1); allowed keys: "
            f"{', '.join(sorted(PRIVACY_KEYS))}."
        )
    if "user_data_collected" in privacy and not _is_str_list(privacy["user_data_collected"]):
        errors.append("privacy.user_data_collected must be a list of strings.")
    for str_key in ("opt_out_command", "consent_notice"):
        if str_key in privacy and not isinstance(privacy[str_key], str):
            errors.append(f"privacy.{str_key} must be a string.")
    if "retention_days" in privacy:
        rd = privacy["retention_days"]
        if not isinstance(rd, int) or isinstance(rd, bool) or rd < 1:
            errors.append("privacy.retention_days must be an integer >= 1.")
    if "shareable_in_lessons" in privacy and not isinstance(
        privacy["shareable_in_lessons"], bool
    ):
        errors.append("privacy.shareable_in_lessons must be a boolean.")
    return errors


def _validate_audience_scoping_block(scoping: Any) -> list[str]:
    """Pinned-structure errors for a *present* audience_scoping block."""
    if not isinstance(scoping, dict):
        return [
            f"audience_scoping must be an object, got {type(scoping).__name__}"
        ]

    errors: list[str] = []
    unknown = sorted(set(scoping) - AUDIENCE_SCOPING_KEYS)
    if unknown:
        errors.append(
            f"audience_scoping has unknown key(s): {', '.join(unknown)}. The "
            f"structure is pinned (base-spec §11.1); allowed keys: "
            f"{', '.join(sorted(AUDIENCE_SCOPING_KEYS))}."
        )
    missing = [k for k in AUDIENCE_SCOPING_REQUIRED if k not in scoping]
    if missing:
        errors.append(
            "audience_scoping is missing required key(s): "
            + ", ".join(missing)
            + ". A declared trust boundary must say who (operator), where "
            "(approved_surfaces), and what (role_capabilities)."
        )
    operator = scoping.get("operator")
    if "operator" in scoping and operator not in OPERATOR_VALUES:
        errors.append(
            f"audience_scoping.operator {operator!r} is not one of "
            f"{', '.join(repr(v) for v in OPERATOR_VALUES)}."
        )
    if "approved_surfaces" in scoping and not _is_str_list(scoping["approved_surfaces"]):
        errors.append(
            "audience_scoping.approved_surfaces must be a list of strings "
            "(surface ids; vocabulary is open in v1)."
        )
    rc = scoping.get("role_capabilities")
    if "role_capabilities" in scoping:
        if not isinstance(rc, dict) or not all(
            isinstance(role, str) and _is_str_list(caps)
            for role, caps in rc.items()
        ):
            errors.append(
                "audience_scoping.role_capabilities must be an object mapping "
                "role name → list of capability strings."
            )
    if "operator_bypasses" in scoping and not _is_str_list(scoping["operator_bypasses"]):
        errors.append("audience_scoping.operator_bypasses must be a list of strings.")
    return errors


# ── Validator entry point ────────────────────────────────────────────────────


def validate_privacy_scoping(pkg_or_manifest: Any) -> dict:
    """Validate the privacy/audience posture of a gallery package or manifest.

    Accepts either a gallery package dict or an ApplicationManifest-like
    object (same convention as ``validate_bot_guidance``). Tolerant of
    missing fields — absence means "not yet declared" and passes.

    Returns a dict shaped for direct embedding in
    ``gallery.preflight_check``'s ``requirements`` list::

        {
          "ok":                bool,
          "privacy_declared":  bool,
          "audience_declared": bool,
          "trigger_count":     int,    # event_triggers[] length
          "group_trigger_count": int,  # triggers on a GROUP_CHANNELS surface
          "errors":            list[str],
          "severity":          "build_blocker" | "info",
          "message":           str,
        }
    """
    privacy = _get(pkg_or_manifest, "privacy")
    scoping = _get(pkg_or_manifest, "audience_scoping")
    event_triggers = _get(pkg_or_manifest, "event_triggers") or []
    if not isinstance(event_triggers, list):
        event_triggers = []

    # Empty dict == absent == "not yet declared". The migration/forge
    # seeding never write empty blocks, so {} only occurs as the inert
    # dataclass default on pre-v24 manifests.
    privacy_declared = bool(privacy)
    audience_declared = bool(scoping)

    errors: list[str] = []
    notes: list[str] = []

    if privacy_declared:
        errors.extend(_validate_privacy_block(privacy))
    if audience_declared:
        errors.extend(_validate_audience_scoping_block(scoping))

    # ── trigger-audience conformance (closes the Slice-1 free-string gap) ──
    role_keys: set[str] = set()
    if audience_declared and isinstance(scoping, dict):
        rc = scoping.get("role_capabilities")
        if isinstance(rc, dict):
            role_keys = {k for k in rc if isinstance(k, str)}

    group_trigger_count = 0
    for idx, trigger in enumerate(event_triggers):
        if not isinstance(trigger, dict):
            continue  # shape errors are the freelance validator's job
        trigger_id = trigger.get("id") or f"#{idx}"

        audience = trigger.get("audience")
        if isinstance(audience, str) and audience:
            if audience_declared:
                if audience not in role_keys:
                    known = ", ".join(sorted(role_keys)) or "(none)"
                    errors.append(
                        f"event_triggers[{trigger_id}].audience {audience!r} "
                        f"does not name a key in "
                        f"audience_scoping.role_capabilities. Declared roles: "
                        f"{known}. Add the role to role_capabilities or fix "
                        f"the trigger's audience."
                    )
            else:
                notes.append(
                    f"event_triggers[{trigger_id}].audience {audience!r} is "
                    f"an unpinned free string — declare audience_scoping with "
                    f"a matching role_capabilities key to pin it."
                )

        match = trigger.get("match")
        channel = match.get("channel") if isinstance(match, dict) else None
        if isinstance(channel, str) and channel in GROUP_CHANNELS:
            group_trigger_count += 1

    # ── group-surface consent gate ──────────────────────────────────────────
    consent = ""
    if isinstance(privacy, dict):
        raw_consent = privacy.get("consent_notice")
        if isinstance(raw_consent, str):
            consent = raw_consent.strip()
    if group_trigger_count and not consent:
        errors.append(
            f"{group_trigger_count} event_trigger(s) listen on a group "
            f"surface (match.channel in {', '.join(sorted(GROUP_CHANNELS))}) "
            f"but privacy.consent_notice is missing or empty. Apps that "
            f"pattern-match group messages must declare what they tell the "
            f"people in that group."
        )

    if errors:
        return {
            "ok": False,
            "privacy_declared": privacy_declared,
            "audience_declared": audience_declared,
            "trigger_count": len(event_triggers),
            "group_trigger_count": group_trigger_count,
            "errors": errors,
            "severity": "build_blocker",
            "message": (
                f"privacy/audience_scoping gate refused: {len(errors)} "
                f"issue(s). " + " ".join(errors[:3])
                + (" …" if len(errors) > 3 else "")
            ),
        }

    if notes:
        message = notes[0] if len(notes) == 1 else (
            f"{len(notes)} trigger audience value(s) are unpinned free "
            f"strings; declare audience_scoping to pin the vocabulary."
        )
    elif privacy_declared or audience_declared:
        message = "privacy/audience_scoping posture is declared and well-formed."
    else:
        message = (
            "privacy and audience_scoping not yet declared — allowed "
            "(no flag day), but the trust boundary is unchecked until "
            "declared."
        )

    return {
        "ok": True,
        "privacy_declared": privacy_declared,
        "audience_declared": audience_declared,
        "trigger_count": len(event_triggers),
        "group_trigger_count": group_trigger_count,
        "errors": [],
        "severity": "info",
        "message": message,
    }


# ── Data-boundary projection (audit+score surface) ───────────────────────────


def project_data_boundary(manifest: dict) -> dict:
    """Structured "what this app collects, who can reach it" projection.

    Input is a manifest/Spec dict (either shape — both carry the same two
    blocks as of v24). Output feeds the security audit surface and is
    U4.3's data source. Structured fields only — rendering stays in the
    UI; no prose synthesis here.
    """
    privacy = manifest.get("privacy")
    scoping = manifest.get("audience_scoping")
    privacy = privacy if isinstance(privacy, dict) else {}
    scoping = scoping if isinstance(scoping, dict) else {}

    triggers = manifest.get("event_triggers")
    triggers = triggers if isinstance(triggers, list) else []
    group_triggers = 0
    for t in triggers:
        match = t.get("match") if isinstance(t, dict) else None
        channel = match.get("channel") if isinstance(match, dict) else None
        if isinstance(channel, str) and channel in GROUP_CHANNELS:
            group_triggers += 1

    collected = privacy.get("user_data_collected")
    consent = privacy.get("consent_notice")
    opt_out = privacy.get("opt_out_command")
    retention = privacy.get("retention_days")
    operator = scoping.get("operator")
    surfaces = scoping.get("approved_surfaces")
    bypasses = scoping.get("operator_bypasses")
    rc = scoping.get("role_capabilities")
    return {
        "app_id": manifest.get("id") or "",
        "name": (
            manifest.get("display_name") or manifest.get("name")
            or manifest.get("id") or ""
        ),
        "status": manifest.get("status") or "active",
        "privacy_declared": bool(privacy),
        "audience_declared": bool(scoping),
        "collects": list(collected) if _is_str_list(collected) else [],
        "consent_notice": consent if isinstance(consent, str) else "",
        "opt_out_command": opt_out if isinstance(opt_out, str) else "",
        "retention_days": (
            retention
            if isinstance(retention, int) and not isinstance(retention, bool)
            else None
        ),
        "shareable_in_lessons": bool(privacy.get("shareable_in_lessons", False)),
        "operator": operator if isinstance(operator, str) else "",
        "approved_surfaces": list(surfaces) if _is_str_list(surfaces) else [],
        "roles": sorted(rc) if isinstance(rc, dict) else [],
        "operator_bypasses": list(bypasses) if _is_str_list(bypasses) else [],
        "trigger_count": len(triggers),
        "group_trigger_count": group_triggers,
    }
