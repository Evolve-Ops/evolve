"""
provisioner.py — Provision a pod-template onto an EXISTING pod (Bite 2).

Bite 1 captured a live pod into a ``pod.yaml`` (``extractor.py``); this module
is the inverse: take a :class:`~evolve_admin.pod_templates.loader.PodTemplate`
and stand its bots + pod-wide settings up on an existing pod. It is the
"local multi-bot scaffold" half of spec-apps-meta-2026-06-13 §8 — remote /
new-pod bootstrap (multi-pod M1) and credential custody are explicitly out of
scope.

Plan/apply split (same shape as ``arbiter`` and ``bot_templates.cli_integration``)
────────────────────────────────────────────────────────────────────────────────
:func:`provision_pod_template` is the single entry point. ``dry_run=True`` (the
default) computes the full plan against an in-memory copy of ``network.json`` and
returns it **without writing anything** — no ``network.json`` write, no bot
creation, no skill install. ``dry_run=False`` performs the same computation and
then commits it with **one atomic ``network.json`` write** (temp-file + ``os.replace``
in the target's own directory). The planner *is* the applier — both branches run
identical merge logic, so the dry-run plan is a faithful preview of the apply.

Pinned collision / idempotency policy (do not deviate)
──────────────────────────────────────────────────────
* **Bots:** a ``bot_id`` already present in ``network.json::bots`` is **SKIPPED,
  never clobbered** — ``apply`` creates only MISSING bots. (A planned-bot block —
  ``{"purpose": ...}`` from the add-bot wizard — counts as present and is skipped
  too; graduating it is the wizard/deploy's job, not ours.) Running ``apply``
  twice therefore creates no duplicate bots.
* **Pod-wide seed:** the three seed blocks (``release_mode`` →
  ``pod.release.mode``, ``model_tiers`` → ``models``, ``integrations`` →
  ``podInvariantIntegrations``) FILL MISSING keys only by default — an existing,
  non-empty operator value is **never** overwritten. ``overwrite_pod_config=True``
  replaces a seed block wholesale. Either way the dry-run plan shows the exact
  diff.
* **Per-item, non-blocking:** one bot's planning failure never aborts the run and
  never half-writes ``network.json`` — every per-bot decision is made in memory
  first, and only then is the merged config committed in a single atomic write.

Materialization (the macOS account, skill install, file render) is **not** done
here — it is the existing ``sudo evolve-admin deploy <bot>`` path (spec §7). This
module registers the roster + pod config and produces, for each created bot, the
``bot_templates`` provision plan (via :func:`bot_templates.build_plan`) so the
operator can see exactly what ``deploy`` will install.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .errors import PodTemplateError
from .loader import PodBotSpec, PodSeed, PodTemplate

# Gateway-port range — mirrored from ``provisioning`` (the canonical source) so
# this module stays free of that package's heavy import chain. Kept in sync by
# the constants' rarity of change; a drift would only mean a created bot gets a
# port the next ``deploy`` re-picks, not a correctness bug.
_PORT_START = 19000
_PORT_END = 19100


# ── Result dataclasses ──────────────────────────────────────────────────────────


@dataclass
class PodConfigChange:
    """One pod-wide ``network.json`` mutation the provisioner made (or would make).

    Attributes:
        path:    Dotted location, e.g. ``"pod.release.mode"`` / ``"models"`` /
                 ``"podInvariantIntegrations"``.
        action:  ``"add"`` (filled a missing/empty key) or ``"replace"``
                 (``overwrite_pod_config`` replaced an existing value).
        old:     Prior value (``None`` when the key was absent).
        new:     Value written (or that would be written in dry-run).
    """

    path: str
    action: str
    old: Any
    new: Any


@dataclass
class BotProvisionItem:
    """The provisioner's decision + plan for one bot in the template roster.

    Attributes:
        bot_id:       The bot's id.
        action:       ``"create"`` / ``"skip-existing"`` / ``"skip-planned"`` /
                      ``"failed"``.
        bot_template: The referenced bot-template name (``None`` for an
                      inline / no-template bot).
        reason:       Human-readable explanation of the decision.
        port:         Gateway port assigned to a created bot (``None`` otherwise).
        plan:         The ``bot_templates`` :class:`BuildPlanResult` for a created
                      bot that references a bot-template — what ``deploy`` will
                      install. ``None`` for skipped / no-template / failed bots.
                      Typed ``Any`` to avoid importing the bot-template package at
                      module load (the planner is resolved lazily).
        warnings:     Per-bot non-blocking issues (e.g. a no-template bot's
                      ``manual: install apps [...]`` follow-up, or a planning
                      error that did not abort the run).
    """

    bot_id: str
    action: str
    bot_template: str | None = None
    reason: str = ""
    port: int | None = None
    plan: Any = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class PodProvisionResult:
    """The complete plan/outcome of provisioning a pod-template.

    In dry-run, this is a *preview* (``applied=False``, no write happened). In
    apply, it reflects the committed state (``applied=True`` iff a write occurred).
    """

    template_name: str
    dry_run: bool
    applied: bool
    network_path: str
    created: list[BotProvisionItem] = field(default_factory=list)
    skipped: list[BotProvisionItem] = field(default_factory=list)
    failed: list[BotProvisionItem] = field(default_factory=list)
    pod_config_changes: list[PodConfigChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff the pod-template itself is valid + safe to provision.

        Per-bot ``failed`` items are reported but do **not** flip this to False —
        the contract is per-item degradation, not all-or-nothing. ``ok`` gates on
        pod-level safety (validation, including the secret sweep) only.
        """
        return not self.validation_errors

    @property
    def has_changes(self) -> bool:
        """True iff this run created at least one bot or changed pod config."""
        return bool(self.created or self.pod_config_changes)

    def summary_lines(self) -> list[str]:
        """Render a human-readable summary (no leading whitespace; caller prints)."""
        out: list[str] = []
        mode = "DRY-RUN (no changes written)" if self.dry_run else (
            "APPLIED" if self.applied else "APPLY (no changes needed)"
        )
        out.append(f"Pod-template: {self.template_name}")
        out.append(f"Network:      {self.network_path}")
        out.append(f"Mode:         {mode}")

        if self.validation_errors:
            out.append("")
            out.append("VALIDATION ERRORS (provision refused):")
            out.extend(f"  - {e}" for e in self.validation_errors)
            return out
        if self.validation_warnings:
            out.append("")
            out.append("Validation warnings:")
            out.extend(f"  - {w}" for w in self.validation_warnings)

        out.append("")
        out.append(f"Bots to create ({len(self.created)}):")
        if not self.created:
            out.append("  (none)")
        for it in self.created:
            ref = it.bot_template or "(no bot-template — inline app list)"
            out.append(f"  + {it.bot_id} [{ref}] port={it.port}")
            for w in it.warnings:
                out.append(f"      ! {w}")

        out.append("")
        out.append(f"Bots skipped ({len(self.skipped)}):")
        if not self.skipped:
            out.append("  (none)")
        for it in self.skipped:
            out.append(f"  = {it.bot_id} — {it.reason}")

        if self.failed:
            out.append("")
            out.append(f"Bots failed ({len(self.failed)}):")
            for it in self.failed:
                out.append(f"  x {it.bot_id} — {it.reason}")

        out.append("")
        out.append(f"Pod-config changes ({len(self.pod_config_changes)}):")
        if not self.pod_config_changes:
            out.append("  (none — pod-wide seed already matches)")
        for ch in self.pod_config_changes:
            out.append(f"  {ch.action}: {ch.path} = {ch.new!r} (was {ch.old!r})")

        if self.warnings:
            out.append("")
            out.append("Warnings:")
            out.extend(f"  - {w}" for w in self.warnings)

        if self.created and self.dry_run:
            out.append("")
            out.append(
                "Next: re-run with --apply to register the bots + seed, then "
                "`sudo evolve-admin deploy <bot>` to materialize each one."
            )
        elif self.created and self.applied:
            out.append("")
            out.append(
                "Registered. Materialize each created bot with "
                "`sudo evolve-admin deploy <bot>`."
            )
        return out


# ── Bot planner seam ─────────────────────────────────────────────────────────────


def _default_bot_planner(
    *,
    bot_template: str,
    bot_id: str,
    vars: dict[str, Any],
    templates_dir: Path | None,
) -> Any:
    """Default per-bot planner: delegate to ``bot_templates.build_plan``.

    Imported lazily so the pod-template package has no load-time dependency on
    the bot-template package (which pulls in the launchd/subprocess runtime).
    Tests inject a lightweight fake via ``provision_pod_template(plan_bot_fn=...)``
    so they never read a real ``openclaw.json`` or shell out to ``sudo``.
    """
    from ..bot_templates import build_plan

    return build_plan(
        template_name=bot_template,
        bot_id=bot_id,
        vars=vars,
        templates_dir=templates_dir,
    )


# ── Entry point ──────────────────────────────────────────────────────────────────


def provision_pod_template(
    pod_template: PodTemplate,
    *,
    dry_run: bool = True,
    overwrite_pod_config: bool = False,
    network_path: Path | None = None,
    templates_dir: Path | None = None,
    plan_bot_fn: Callable[..., Any] | None = None,
) -> PodProvisionResult:
    """Provision ``pod_template``'s bots + pod-wide seed onto an existing pod.

    Args:
        pod_template:         A loaded :class:`PodTemplate` (from the loader or
                              extractor).
        dry_run:              When True (default) compute the plan and return it
                              **without any side effect** — no ``network.json``
                              write, no bot creation, no skill install. When
                              False, commit the plan via a single atomic
                              ``network.json`` write.
        overwrite_pod_config: When False (default) the pod-wide seed only fills
                              missing/empty keys. When True it replaces each seed
                              block wholesale. Never touches existing operator
                              config in the default mode.
        network_path:         Path to the pod's ``network.json``. Defaults to the
                              live pod's canonical location
                              (``config.DEFAULT_NETWORK_CONFIG``).
        templates_dir:        Bot-template search root for resolving each
                              ``PodBotSpec.bot_template`` reference. Defaults to
                              the builtin gallery (``bot_templates.builtin_templates_dir``).
        plan_bot_fn:          Override the per-bot planner (mainly for tests).
                              Signature: ``(*, bot_template, bot_id, vars,
                              templates_dir) -> BuildPlanResult``.

    Returns:
        A :class:`PodProvisionResult`. ``ok`` is False only when the pod-template
        fails validation (structural or the secret-leak sweep); per-bot failures
        are reported per-item and do not flip ``ok``.

    Raises:
        PodTemplateError: only for an unreadable / malformed ``network.json``.
            A *missing* file is treated as a fresh, empty pod (not an error).
    """
    # Resolve the network path lazily so importing this module never requires the
    # admin config package (and so tests can pass a fixture path).
    if network_path is None:
        from ..config import DEFAULT_NETWORK_CONFIG

        network_path = DEFAULT_NETWORK_CONFIG
    network_path = Path(network_path)

    planner = plan_bot_fn or _default_bot_planner

    result = PodProvisionResult(
        template_name=pod_template.name,
        dry_run=dry_run,
        applied=False,
        network_path=str(network_path),
    )

    # ── Gate 1: the pod-template must be valid + secret-free ──────────────────
    # Defense in depth: the extractor + validator already guarantee this on
    # capture, but a hand-edited pod.yaml could carry a leaked token. Refuse to
    # provision (and especially to write network.json) from an invalid template.
    from .validator import validate_pod_template

    report = validate_pod_template(pod_template)
    result.validation_errors = list(report.errors)
    result.validation_warnings = list(report.warnings)
    if report.errors:
        return result  # ok == False; nothing computed, nothing written.

    # ── Read the live network.json (RAW — never via load_network) ─────────────
    # We read the raw JSON, NOT config.load_network, so we don't inject the
    # in-memory pod-field defaults (admin/primary passphrases) into the file we
    # may write back. We only ever add what the template seeds.
    network = _read_network_raw(network_path)
    existing_bots = network.get("bots")
    existing_bots = existing_bots if isinstance(existing_bots, dict) else {}

    # ``merged`` is the candidate next-state. Every per-bot + seed decision
    # mutates this in-memory copy; the single atomic write at the end commits it.
    merged = copy.deepcopy(network)
    merged_bots: dict[str, Any] = merged.setdefault("bots", {})
    if not isinstance(merged_bots, dict):  # malformed file — refuse to clobber
        raise PodTemplateError(
            f"{network_path}: 'bots' is not a mapping "
            f"({type(merged_bots).__name__}); refusing to provision over it"
        )

    # ── Per-bot decisions ─────────────────────────────────────────────────────
    for spec in pod_template.bots:
        existing = existing_bots.get(spec.bot_id)
        if spec.bot_id in existing_bots:
            # Present already → SKIP. Never clobber (the pinned policy). Use the
            # planned-bot predicate only to phrase the reason, not the decision.
            planned = _is_planned_bot(existing)
            result.skipped.append(
                BotProvisionItem(
                    bot_id=spec.bot_id,
                    action="skip-planned" if planned else "skip-existing",
                    bot_template=spec.bot_template,
                    reason=(
                        "already a planned bot in network.json — graduate it via "
                        "the add-bot wizard, not the pod-template provisioner"
                        if planned
                        else "already a member of this pod (left untouched)"
                    ),
                )
            )
            continue

        # Missing → create. Build the per-bot plan + register the entry, both
        # guarded so a single bot's failure is captured per-item, not raised.
        item = _create_bot(
            spec,
            merged=merged,
            merged_bots=merged_bots,
            planner=planner,
            templates_dir=templates_dir,
        )
        if item.action == "failed":
            result.failed.append(item)
        else:
            result.created.append(item)

    # ── Pod-wide seed merge ───────────────────────────────────────────────────
    _merge_seed(
        merged,
        pod_template.pod,
        overwrite=overwrite_pod_config,
        changes=result.pod_config_changes,
        warnings=result.warnings,
    )

    # ── Commit (apply only) ───────────────────────────────────────────────────
    if not dry_run and result.has_changes:
        _atomic_write_network(merged, network_path)
        result.applied = True

    return result


# ── Bot creation ─────────────────────────────────────────────────────────────────


def _create_bot(
    spec: PodBotSpec,
    *,
    merged: dict[str, Any],
    merged_bots: dict[str, Any],
    planner: Callable[..., Any],
    templates_dir: Path | None,
) -> BotProvisionItem:
    """Register a missing bot in ``merged`` (in memory) + build its plan.

    Per-item non-blocking: any failure is captured on the returned item (or as a
    ``"failed"`` item) — it never raises. Registration mirrors
    ``deploy.add_bot``'s network.json entry shape (role + port + multiUser) so a
    subsequent ``deploy`` materializes it identically; account creation + skill
    install are NOT done here.
    """
    item = BotProvisionItem(
        bot_id=spec.bot_id,
        action="create",
        bot_template=spec.bot_template,
    )

    # Assign a free port across the *merged* roster (so two bots created in one
    # run don't collide — each registration below makes its port visible to the
    # next call).
    try:
        port = _next_free_port(merged_bots)
    except PodTemplateError as e:
        return BotProvisionItem(
            bot_id=spec.bot_id,
            action="failed",
            bot_template=spec.bot_template,
            reason=str(e),
        )
    item.port = port

    role = (spec.role or "member").strip() or "member"
    entry: dict[str, Any] = {"role": role, "port": port, "multiUser": False}
    merged_bots[spec.bot_id] = entry

    # Roster membership mirror (add_bot keeps both ``bots`` and ``members``).
    members = merged.setdefault("members", [])
    if isinstance(members, list) and spec.bot_id not in members:
        members.append(spec.bot_id)

    # Primary pointer: fill only if the pod has none (never clobber an existing
    # primary — that would silently re-home pod alerts/proposals).
    if role == "primary":
        cur_primary = merged.get("primary")
        if not (isinstance(cur_primary, str) and cur_primary.strip()):
            merged["primary"] = spec.bot_id
        elif cur_primary != spec.bot_id:
            item.warnings.append(
                f"template marks {spec.bot_id!r} as primary but pod primary is "
                f"already {cur_primary!r} — left unchanged"
            )

    # No-template bot: nothing to plan. Best-effort app install would need a
    # list-install seam that does not exist in this bite, so emit the pinned
    # ``manual:`` follow-up rather than silently dropping the apps.
    if not spec.bot_template:
        if spec.installed_apps:
            item.warnings.append(
                "manual: install apps "
                f"{list(spec.installed_apps)} for {spec.bot_id} "
                "(no bot-template to drive an automated install)"
            )
        else:
            item.warnings.append(
                "no bot-template and no recorded apps — registered as a bare "
                "roster member; configure it via `deploy` / the wizard"
            )
        return item

    # Templated bot: loop the bot_templates provisioner to build its plan. A
    # planning failure is recorded as a warning — the registration still stands
    # (the operator fixes the template and re-runs deploy), honouring per-item
    # degradation.
    try:
        plan = planner(
            bot_template=spec.bot_template,
            bot_id=spec.bot_id,
            vars=dict(spec.overrides),
            templates_dir=templates_dir,
        )
    except Exception as e:  # noqa: BLE001 — never let one bot abort the run
        item.warnings.append(
            f"could not build bot-template plan for {spec.bot_template!r}: {e}"
        )
        return item

    item.plan = plan
    # Surface the plan's own validation/blocking issues as bot warnings so they
    # are visible without abort.
    for attr in ("validation_errors", "error"):
        val = getattr(plan, attr, None)
        if not val:
            continue
        for msg in (val if isinstance(val, (list, tuple)) else [val]):
            item.warnings.append(f"bot-template plan: {msg}")
    return item


def _next_free_port(bots: dict[str, Any]) -> int:
    """Lowest free gateway port not used by any bot in ``bots``.

    Mirrors ``provisioning._next_free_port`` but operates on the merged roster
    dict directly (so in-run registrations are visible). Raises
    :class:`PodTemplateError` on range exhaustion (caught per-item by the caller).

    Harder than the canonical guard: it counts a port written as a numeric
    *string* (``"19000"``) or whole float as used too. ``deploy.add_bot`` always
    writes ``int`` ports, but a hand-edited / migrated entry could carry a string
    — and an int-only guard would then hand the new bot a *colliding* port (two
    bots, same port), which is worse than the documented "deploy re-picks" drift.
    """
    used: set[int] = set()
    for b in bots.values():
        if not isinstance(b, dict):
            continue
        coerced = _coerce_port(b.get("port"))
        if coerced is not None:
            used.add(coerced)
    port = _PORT_START
    while port in used and port <= _PORT_END:
        port += 1
    if port > _PORT_END:
        raise PodTemplateError(
            f"no free gateway port in [{_PORT_START}, {_PORT_END}]"
        )
    return port


def _coerce_port(p: Any) -> int | None:
    """Return ``p`` as an ``int`` port, or ``None`` if it is not port-shaped.

    Accepts an int, a whole float, or an all-digit string. Rejects ``bool``
    (``True``/``False`` are not ports even though they are ``int`` subclasses).
    """
    if isinstance(p, bool):
        return None
    if isinstance(p, int):
        return p
    if isinstance(p, float) and p.is_integer():
        return int(p)
    if isinstance(p, str) and p.strip().isdigit():
        return int(p.strip())
    return None


def _is_planned_bot(cfg: Any) -> bool:
    """True for a planned-bot block (``{"purpose": ...}``) — mirrors
    ``config.is_planned_bot_block`` without importing the heavy config module."""
    return isinstance(cfg, dict) and set(cfg.keys()) <= {"purpose"} and "purpose" in cfg


# ── Pod-wide seed merge ──────────────────────────────────────────────────────────


def _merge_seed(
    network: dict[str, Any],
    seed: PodSeed,
    *,
    overwrite: bool,
    changes: list[PodConfigChange],
    warnings: list[str],
) -> None:
    """Merge the pod-wide seed into ``network`` (in place), recording each change.

    Fill-missing by default; ``overwrite`` replaces a seed block wholesale. The
    invariant: in fill-missing mode a non-empty existing operator value is never
    modified.
    """
    # 1. release_mode → pod.release.mode (scalar)
    if seed.release_mode is not None:
        _merge_release_mode(network, seed.release_mode, overwrite, changes, warnings)

    # 2. model_tiers → models (dict; deep fill-missing)
    if seed.model_tiers:
        _merge_dict_block(
            network, "models", seed.model_tiers, overwrite, changes
        )

    # 3. integrations → podInvariantIntegrations (pod-scope names; list) +
    #    bot-scope names surfaced as manual warnings (no credential custody).
    pod_names = [
        name
        for name, meta in seed.integrations.items()
        if isinstance(meta, dict) and meta.get("scope") == "pod"
    ]
    bot_scope = {
        name: meta
        for name, meta in seed.integrations.items()
        if isinstance(meta, dict) and meta.get("scope") == "bot"
    }
    if pod_names:
        _merge_list_block(
            network, "podInvariantIntegrations", pod_names, overwrite, changes
        )
    for name, meta in bot_scope.items():
        bots = meta.get("bots") if isinstance(meta, dict) else None
        who = f" (used by {bots})" if bots else ""
        warnings.append(
            f"manual: connect integration {name!r}{who} — credentials are not "
            f"captured by pod-templates; configure it per-bot after deploy"
        )

    # 4. Forward-compat keys the loader preserved but this version does not know
    #    how to provision. Surface them rather than silently dropping them on the
    #    floor (no-silent-caps): a newer pod.yaml seeding an unknown pod-wide key
    #    should not look like it applied cleanly.
    if seed.extra:
        warnings.append(
            "pod-template seed carries forward-compat keys this version does not "
            f"provision: {sorted(seed.extra)} — apply them manually or upgrade"
        )


def _merge_release_mode(
    network: dict[str, Any],
    mode: str,
    overwrite: bool,
    changes: list[PodConfigChange],
    warnings: list[str],
) -> None:
    pod = network.get("pod")
    if pod is None:
        pod = {}
        network["pod"] = pod
    elif not isinstance(pod, dict):
        warnings.append(
            "network.json 'pod' is not a mapping — skipping release_mode seed"
        )
        return
    rel = pod.get("release")
    if rel is None:
        rel = {}
        pod["release"] = rel
    elif not isinstance(rel, dict):
        warnings.append(
            "network.json 'pod.release' is not a mapping — skipping release_mode "
            "seed"
        )
        return
    cur = rel.get("mode")
    if overwrite:
        if cur != mode:
            rel["mode"] = mode
            changes.append(
                PodConfigChange("pod.release.mode", "replace", cur, mode)
            )
    elif not (isinstance(cur, str) and cur.strip()):
        rel["mode"] = mode
        changes.append(PodConfigChange("pod.release.mode", "add", cur, mode))


def _merge_dict_block(
    network: dict[str, Any],
    key: str,
    source: dict[str, Any],
    overwrite: bool,
    changes: list[PodConfigChange],
) -> None:
    cur = network.get(key)
    if overwrite:
        if cur != source:
            network[key] = copy.deepcopy(source)
            changes.append(PodConfigChange(key, "replace", cur, network[key]))
        return
    if _is_empty(cur):
        network[key] = copy.deepcopy(source)
        changes.append(PodConfigChange(key, "add", cur, network[key]))
    elif isinstance(cur, dict):
        _deep_fill(cur, source, key, changes)
    # else: present non-dict scalar/list — keep operator value, no change.


def _merge_list_block(
    network: dict[str, Any],
    key: str,
    source: list[Any],
    overwrite: bool,
    changes: list[PodConfigChange],
) -> None:
    cur = network.get(key)
    if overwrite:
        new = list(source)
        if cur != new:
            network[key] = new
            changes.append(PodConfigChange(key, "replace", cur, new))
        return
    if _is_empty(cur):
        network[key] = list(source)
        changes.append(PodConfigChange(key, "add", cur, network[key]))
    # else: present non-empty list — fill-missing leaves it untouched (a list is
    # an atomic value here; we never silently mutate an operator's integration
    # roster).


def _deep_fill(
    target: dict[str, Any],
    source: dict[str, Any],
    base_path: str,
    changes: list[PodConfigChange],
) -> None:
    """Recursively add keys from ``source`` missing/empty in ``target``.

    Never replaces a non-empty existing value: a present scalar/list is kept, a
    present dict is recursed into. Each added subtree records one change at the
    key where it was added.
    """
    for k, v in source.items():
        child = f"{base_path}.{k}"
        if k not in target or _is_empty(target.get(k)):
            old = target.get(k)
            target[k] = copy.deepcopy(v)
            changes.append(PodConfigChange(child, "add", old, target[k]))
        elif isinstance(target[k], dict) and isinstance(v, dict):
            _deep_fill(target[k], v, child, changes)
        # else: present scalar/list — keep operator value, no change.


def _is_empty(v: Any) -> bool:
    """True for absent-equivalent values: ``None``, ``""``, ``{}``, ``[]``.

    ``0`` / ``False`` are real values, not empty — they are not matched.
    """
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (dict, list, tuple, set)):
        return len(v) == 0
    return False


# ── network.json IO ──────────────────────────────────────────────────────────────


def _read_network_raw(network_path: Path) -> dict[str, Any]:
    """Read ``network.json`` as a raw dict. A missing file → empty pod ({}).

    Raises :class:`PodTemplateError` for an unreadable / malformed file or a
    non-object top level (refuse to provision over a corrupt config).
    """
    if not network_path.exists():
        return {}
    try:
        text = network_path.read_text(encoding="utf-8")
    except OSError as e:
        raise PodTemplateError(f"cannot read {network_path}: {e}") from e
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        raise PodTemplateError(f"{network_path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise PodTemplateError(
            f"{network_path} top-level must be an object, got "
            f"{type(data).__name__}"
        )
    return data


def _atomic_write_network(data: dict[str, Any], network_path: Path) -> None:
    """Write ``data`` to ``network_path`` atomically (temp-file + ``os.replace``).

    The temp file is created in the target's *own directory* so ``os.replace`` is
    a same-filesystem atomic rename — a crash mid-write leaves the original
    intact. ``network.json`` lives under ``{shared_dir}`` and is owned by the
    ``evolve`` user (CLAUDE.md "Arbiter on-disk layout"), so a direct
    temp+rename needs no ``/tmp`` staging or sudo. (Materialization that may run
    on a root-owned file is the ``deploy`` path's concern, not this scaffold's.)
    """
    network_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(network_path.parent), prefix=".network-prov-", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, network_path)
    except Exception:
        # Best-effort cleanup of the staged temp on any failure (a successful
        # os.replace already consumed it). Leaving a stray .network-prov-*.json
        # is harmless; the original network.json is untouched either way.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
