"""Post-install and teardown-residue assertions.

Pure functions over a ``PodProbe`` — no I/O of their own — so a fixture-backed
fake drives every branch in unit tests. Each function appends ``Check``s to a
``StepResult``; the step (and app) fail iff any BLOCK check fails. Skipped
checks record absent coverage explicitly and never count as pass.

A recurring principle here (memory: distinguish tooling failure from findings):
the scheduler probe returns a ``status_error`` tri-state. When it is non-None
the probe was NOT authoritative — we record a WARN ("couldn't verify"), never
a BLOCK ("unit missing"). A missing unit is only asserted when the probe
authoritatively said so.
"""

from __future__ import annotations

from typing import Any

from .model import (
    INFO,
    WARN,
    StepResult,
    fail_check,
    ok_check,
    skip_check,
)

# recon bucket names that are healthy vs broken for an owned app.
_RECON_OK = "owned_ok"
_RECON_BROKEN = ("invalid_claim", "missing_marker", "missing_file")
_RECON_SOFT = ("attach_candidate", "scrub_candidate")

_VALID_APP_KINDS = {"application", "capability", "system"}


def _identity_set(pkg_id: str, manifest: dict) -> set[str]:
    """Every id token this install could have been *written down under*.

    identity: see ``applications.app_identity.resolve_app_id`` (AL-1.4).

    This is deliberately NOT a resolution and must not become one. The
    artifacts matched against it — recon-ledger rows, delivery-state keys —
    were written at different times by different writers, each of which
    recorded whichever member of the legacy chain it happened to hold. The
    resolver picks exactly ONE; a tolerant match needs all of them, so the
    superset stays.

    ``app_id`` is deliberately absent in AL-1.4b: 1.4a only ever stamps a
    value the legacy chain already resolved to, so for every manifest this
    harness sees (an installed gallery app, whose manifest ``pkg_id`` is the
    ``pkg_id`` argument by construction) ``app_id`` is already a member and
    adding it would be a no-op — while for anything else it would WIDEN the
    match set, which 1.4b is not allowed to do. **AL-1.4c must add it**: once
    writers stop mirroring the legacy fields, this set goes empty except for
    the ``pkg_id`` argument and both callers silently stop matching.
    """
    ids = {pkg_id}
    for k in ("spec_id", "instance_id", "id"):
        v = manifest.get(k)
        if v:
            ids.add(v)
    return ids


def scheduled_unit_target(action: dict) -> tuple[str, str]:
    """Classify a scheduled_action's materialized artifact.

    Returns ``(kind, target)`` where kind ∈:
      unit     → a launchd label / systemd unit to probe via the scheduler
      section  → an evolve-managed HEARTBEAT.md/AGENTS.md section
      crontab  → an OC/crontab line (no unit check in v1)
      none     → external/unknown mechanism, nothing to probe
    """
    mech = (action.get("mechanism") or "").lower()
    artifact = action.get("installed_artifact") or ""
    install = action.get("install") or {}

    if mech in ("oc_heartbeat_instruction", "oc_session_instruction"):
        return ("section", artifact)
    if mech == "crontab" or artifact.startswith("crontab:"):
        return ("crontab", artifact)
    if mech in ("launchd", "launchd_python_signal") or artifact.endswith(".plist"):
        label = install.get("plist_label") or _label_from_plist(artifact)
        return ("unit", label)
    if artifact.endswith(".service") or "systemd" in mech:
        return ("unit", _basename(artifact))
    return ("none", artifact)


def _label_from_plist(artifact: str) -> str:
    base = _basename(artifact)
    return base[:-6] if base.endswith(".plist") else base


def _basename(artifact: str) -> str:
    # installed_artifact may be a bare label, a path, or "FILE#anchor".
    a = artifact.split("#", 1)[0]
    return a.rsplit("/", 1)[-1]


# ── Post-install ──────────────────────────────────────────────────────────────

def post_install_checks(
    probe: Any, pkg_id: str, bot_id: str, manifest: dict, step: StepResult
) -> None:
    """Assert an installed app is born-healthy. ``manifest`` is non-None here
    (the caller fails the step earlier if the manifest is missing).
    """
    # definition_status == defined (born-defined, #3228)
    ds = manifest.get("definition_status")
    step.add(
        ok_check("definition_status", f"defined ({ds})")
        if ds == "defined"
        else fail_check("definition_status", f"expected 'defined', got {ds!r}")
    )

    # v7-arc shape
    shape = manifest.get("manifest_shape")
    step.add(
        ok_check("manifest_shape", "v7-arc")
        if shape == "v7-arc"
        else fail_check("manifest_shape", f"expected 'v7-arc', got {shape!r}")
    )

    # classification sane (empty at install is by design — deferred to scanner)
    _check_classification(manifest, step)

    # status visible (approved/active)
    st = manifest.get("status")
    step.add(
        ok_check("status", str(st), severity=INFO)
        if st in ("approved", "active")
        else fail_check("status", f"unexpected status {st!r}", severity=WARN)
    )

    # recon ledger buckets the app's files owned & healthy
    _check_recon(probe, pkg_id, bot_id, manifest, step)

    # scheduled units materialized + present
    _check_scheduled_units(probe, pkg_id, bot_id, manifest, step)

    # integrity coverage lists the app (macOS now; Linux dead until S7/#3392)
    _check_coverage(probe, pkg_id, bot_id, manifest, step)

    # capability index Tier-1 menu includes it
    _check_capability_index(probe, pkg_id, bot_id, manifest, step)

    # delivery monitor picks up scheduled apps
    _check_delivery(probe, pkg_id, bot_id, manifest, step)

    # Coverage floor: the two checks that actually prove the files landed and
    # are owned are recon_ledger + integrity_coverage. If BOTH only skipped
    # (fresh install not yet reconciled + Linux coverage dead until S7), a PASS
    # would rest on shape-only checks. Surface that loudly so a thin PASS is
    # never mistaken for a fully-verified one.
    _check_coverage_floor(step)


def _check_coverage_floor(step: StepResult) -> None:
    ownership = {"recon_ledger", "integrity_coverage"}
    ran = {c.name for c in step.checks if c.name in ownership and not c.skipped}
    if not ran:
        step.add(fail_check(
            "coverage_floor",
            "thin PASS — both file-ownership checks (recon + integrity coverage) "
            "were skipped; ownership not verified (re-run after recon, or on macOS)",
            severity=WARN,
        ))
    else:
        step.add(ok_check(
            "coverage_floor", f"ownership verified via {sorted(ran)}", severity=INFO,
        ))


def _check_classification(manifest: dict, step: StepResult) -> None:
    cls = manifest.get("classification")
    if not isinstance(cls, dict):
        step.add(fail_check("classification", f"not a dict: {type(cls).__name__}"))
        return
    if not cls:
        step.add(ok_check("classification", "empty (deferred to scanner — expected)", severity=INFO))
        return
    kind = cls.get("app_kind")
    step.add(
        ok_check("classification", f"app_kind={kind}")
        if kind in _VALID_APP_KINDS
        else fail_check("classification", f"unknown app_kind {kind!r}", severity=WARN)
    )


def _check_recon(probe: Any, pkg_id: str, bot_id: str, manifest: dict, step: StepResult) -> None:
    ids = _identity_set(pkg_id, manifest)
    try:
        buckets = probe.recon_buckets(bot_id)
    except Exception as exc:
        step.add(fail_check("recon_ledger", f"recon build failed: {exc}", severity=WARN))
        return

    matched: list[tuple[str, dict]] = []
    for bucket, rows in buckets.items():
        for row in rows:
            # identity: see resolve_app_id — the recon-ledger row schema names
            # its owning-app column ``spec_id`` (audit-app-framework §2.4).
            # That is a field of the LEDGER, not of a manifest, so the
            # resolver does not apply; ``ids`` above carries the tolerance.
            if row.get("spec_id") in ids:
                matched.append((bucket, row))

    if not matched:
        step.add(skip_check(
            "recon_ledger",
            "app files not yet in recon ledger (recon runs post-install; watch live)",
        ))
        return

    broken = [(b, r) for b, r in matched if b in _RECON_BROKEN]
    soft = [(b, r) for b, r in matched if b in _RECON_SOFT]
    owned = [(b, r) for b, r in matched if b == _RECON_OK]

    if broken:
        detail = "; ".join(
            f"{r.get('path')} → {b} ({r.get('reason')})" for b, r in broken[:6]
        )
        step.add(fail_check("recon_ledger", f"{len(broken)} file(s) not owned/healthy: {detail}"))
    elif soft:
        detail = "; ".join(f"{r.get('path')} → {b}" for b, r in soft[:6])
        step.add(fail_check(
            "recon_ledger", f"{len(soft)} file(s) attach/scrub-candidate: {detail}", severity=WARN,
        ))
    else:
        step.add(ok_check("recon_ledger", f"{len(owned)} file(s) owned_ok"))


def _check_scheduled_units(
    probe: Any, pkg_id: str, bot_id: str, manifest: dict, step: StepResult
) -> None:
    actions = manifest.get("scheduled_actions") or []
    if not actions:
        step.add(ok_check("scheduled_units", "no scheduled actions declared", severity=INFO))
        return

    for action in actions:
        aid = action.get("id", "?")
        artifact = action.get("installed_artifact")
        if not artifact:
            step.add(fail_check(
                f"scheduled_unit:{aid}",
                f"installed_artifact missing (materialize likely failed): {action.get('mechanism')}",
            ))
            continue

        kind, target = scheduled_unit_target(action)
        if kind == "unit":
            status = probe.scheduler_status(target)
            serr = status.get("status_error")
            if serr:
                step.add(fail_check(
                    f"scheduled_unit:{aid}",
                    f"scheduler probe not authoritative for {target}: {serr}",
                    severity=WARN,
                ))
            elif status.get("installed"):
                step.add(ok_check(f"scheduled_unit:{aid}", f"unit {target} present"))
            else:
                step.add(fail_check(
                    f"scheduled_unit:{aid}",
                    f"unit {target} NOT present (artifact stamped but unit absent)",
                ))
        elif kind == "section":
            # identity: see resolve_app_id — a managed heartbeat/AGENTS
            # section record stamps the installing package's id in its own
            # ``pkg_id`` field. Foreign record schema, not a manifest.
            present = any(
                s.get("pkg_id") == pkg_id for s in probe.heartbeat_sections(bot_id)
            )
            step.add(
                ok_check(f"scheduled_unit:{aid}", f"managed section present ({artifact})")
                if present
                else fail_check(
                    f"scheduled_unit:{aid}", f"managed section absent ({artifact})",
                )
            )
        elif kind == "crontab":
            step.add(skip_check(
                f"scheduled_unit:{aid}", f"crontab line ({artifact}) — no probe in v1",
            ))
        else:
            step.add(skip_check(
                f"scheduled_unit:{aid}",
                f"mechanism {action.get('mechanism')!r} has no unit to probe",
            ))


def _check_coverage(probe: Any, pkg_id: str, bot_id: str, manifest: dict, step: StepResult) -> None:
    if probe.platform == "linux":
        step.add(skip_check(
            "integrity_coverage",
            "coverage dead on Linux until S7/#3392 (plugin workspaceRoot hardcoded)",
        ))
        return
    ids = _identity_set(pkg_id, manifest)
    covered = probe.covered_ids(bot_id)
    if ids & covered:
        step.add(ok_check("integrity_coverage", f"covered ({sorted(ids & covered)})"))
    else:
        # Eventually-consistent: populated at next gateway tool-result. Not a
        # hard failure on a fresh install.
        step.add(fail_check(
            "integrity_coverage",
            "app not yet in coverage set (populated at next gateway activity)",
            severity=WARN,
        ))


def _check_capability_index(
    probe: Any, pkg_id: str, bot_id: str, manifest: dict, step: StepResult
) -> None:
    md = probe.installed_apps_md(bot_id)
    if not md:
        step.add(fail_check("capability_index", "INSTALLED_APPS.md empty or unreadable", severity=WARN))
        return
    name = manifest.get("name") or manifest.get("display_name") or ""
    # identity: NOT resolve_app_id — INSTALLED_APPS.md
    # is generated text and renders the manifest's ``id`` (the filename stem);
    # for a gallery install ``resolve_app_id`` returns the ``pkg_id``
    # (``p-a3f91c8b``), which the index never renders, so substituting it would
    # fail this check for every gallery app. See AL-1.4 §6 delta 12.
    app_id = manifest.get("id") or ""
    if (name and name in md) or (app_id and app_id in md):
        step.add(ok_check("capability_index", f"listed ({name or app_id})"))
    else:
        step.add(fail_check(
            "capability_index", f"app '{name or app_id}' not found in INSTALLED_APPS.md",
        ))


def _check_delivery(probe: Any, pkg_id: str, bot_id: str, manifest: dict, step: StepResult) -> None:
    actions = manifest.get("scheduled_actions") or []
    has_schedule = bool(actions)
    if not has_schedule:
        step.add(ok_check("delivery_monitor", "no schedule — monitor N/A", severity=INFO))
        return
    state = probe.delivery_state()
    entry = _find_delivery_entry(state, pkg_id, manifest, bot_id)
    if entry is None:
        step.add(fail_check(
            "delivery_monitor",
            "scheduled app not yet in delivery monitor (5-min daemon; watch live)",
            severity=WARN,
        ))
        return
    dstate = entry.get("state")
    if dstate == "unmeasurable":
        step.add(fail_check(
            "delivery_monitor",
            "delivery state 'unmeasurable' despite a declared schedule",
            severity=WARN,
        ))
    else:
        step.add(ok_check("delivery_monitor", f"state={dstate}"))


def _find_delivery_entry(state: dict, pkg_id: str, manifest: dict, bot_id: str) -> dict | None:
    """The delivery state.json is keyed by an internal id; match tolerantly by
    any identity token appearing in a key or an entry's app_id field.
    """
    ids = _identity_set(pkg_id, manifest)
    entries = state.get("entries") if isinstance(state.get("entries"), dict) else state
    if not isinstance(entries, dict):
        return None
    for key, val in entries.items():
        if not isinstance(val, dict):
            continue
        # identity: see resolve_app_id — ``val`` is a delivery-state entry, not
        # a manifest; ``app_id``/``pkg_id`` are that store's own columns and
        # either may hold the token. ``ids`` carries the tolerance.
        if key in ids or val.get("app_id") in ids or val.get("pkg_id") in ids:
            return val
    return None


# ── Teardown residue ──────────────────────────────────────────────────────────

def teardown_residue_checks(
    probe: Any, pkg_id: str, bot_id: str, manifest_before: dict, step: StepResult
) -> None:
    """After a full uninstall, assert the pod is residue-free (S4). Uses the
    manifest captured *before* teardown to know which units/labels to hunt.
    """
    # If we never captured a manifest (install produced none), we cannot
    # enumerate the app's former scheduled units — unit-residue checking is
    # limited. Say so rather than reporting a false "clean".
    if not manifest_before.get("scheduled_actions") and not manifest_before.get("id"):
        step.add(fail_check(
            "residue_scope",
            "no pre-teardown manifest captured — unit/section residue not fully enumerable",
            severity=WARN,
        ))

    # 1. Manifest no longer active (archived → app_manifest returns None).
    still = probe.app_manifest(pkg_id, bot_id)
    step.add(
        ok_check("manifest_archived", "manifest no longer active")
        if still is None
        else fail_check("manifest_archived", f"manifest still active (status={still.get('status')})")
    )

    # 2. No scheduled units for the app's labels still present.
    actions = manifest_before.get("scheduled_actions") or []
    unit_targets = []
    for action in actions:
        kind, target = scheduled_unit_target(action)
        if kind == "unit" and target:
            unit_targets.append((action.get("id", "?"), target))
    if not unit_targets:
        step.add(ok_check("units_removed", "no scheduled units to remove", severity=INFO))
    for aid, target in unit_targets:
        status = probe.scheduler_status(target)
        serr = status.get("status_error")
        if serr:
            step.add(fail_check(
                f"unit_removed:{aid}",
                f"scheduler probe not authoritative for {target}: {serr}",
                severity=WARN,
            ))
        elif status.get("installed"):
            step.add(fail_check(
                f"unit_removed:{aid}", f"unit {target} STILL firing after full uninstall (residue)",
            ))
        else:
            step.add(ok_check(f"unit_removed:{aid}", f"unit {target} gone"))

    # 3. Capability index no longer lists it. Matching is textual (the md has
    # no stable per-app ids), so a matched token is only residue when NO other
    # still-installed app accounts for it: the index is regenerated from
    # visible manifests, and a distinct app with the same display name keeps
    # rendering that name forever (live false positive: the 2026-07-11 sweep's
    # failed Journal install vs the pre-existing active Journal app).
    md = probe.installed_apps_md(bot_id)
    name = manifest_before.get("name") or manifest_before.get("display_name") or ""
    # identity: NOT resolve_app_id — same textual-match constraint as
    # _check_capability_index above: the index renders ``id``, never ``pkg_id``.
    app_id = manifest_before.get("id") or ""
    matched = [t for t in dict.fromkeys((name, app_id)) if t and md and t in md]
    unattributed = list(matched)
    if matched:
        try:
            # identity: see resolve_app_id — self-exclusion is keyed on
            # ``pkg_id`` because that is what ``probe.app_manifest`` matched on
            # to find this install in the first place (pkg_id is stable across
            # installs; the filename slug is not). Left as-is by AL-1.4b: a
            # manifest with NO pkg_id never matches and is therefore always
            # treated as "another app" — see the PR body's finding F2.
            others = [m for m in (probe.visible_manifests(bot_id) or [])
                      if m.get("pkg_id") != pkg_id]
        except Exception:
            others = []  # can't attribute → keep flagging (conservative)
        # Attribute against the other apps' IDENTITY fields only (not their
        # whole manifest JSON — a description merely *mentioning* the name
        # must not exempt residue). Case-insensitive: the index renders both
        # "Journal" (name) and "journal" (hint words) from one identity.
        idents = " ".join(
            str(m.get(k) or "") for m in others
            for k in ("name", "display_name", "id")
        ).lower()
        unattributed = [t for t in matched if t.lower() not in idents]
    if not matched:
        step.add(ok_check("index_cleared", "no longer in capability index"))
    elif unattributed:
        step.add(fail_check(
            "index_cleared", f"'{unattributed[0]}' still in INSTALLED_APPS.md",
        ))
    else:
        step.add(ok_check(
            "index_cleared",
            f"'{matched[0]}' in the index belongs to a different installed app "
            "(same name, different pkg) — this install's entry is gone",
        ))

    # 4. No orphan managed heartbeat/AGENTS sections.
    # identity: see resolve_app_id — managed-section record schema, as above.
    orphan = [s for s in probe.heartbeat_sections(bot_id) if s.get("pkg_id") == pkg_id]
    step.add(
        ok_check("heartbeat_clean", "no orphan managed sections")
        if not orphan
        else fail_check(
            "heartbeat_clean",
            f"{len(orphan)} orphan managed section(s): "
            + ", ".join(f"{s.get('file')}#{s.get('anchor')}" for s in orphan[:5]),
        )
    )
