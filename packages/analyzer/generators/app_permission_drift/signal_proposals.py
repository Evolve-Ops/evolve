"""generators.app_permission_drift.signal_proposals — Signal → Proposal factory.

Per-Signal-kind factories that build Proposals. One Signal → one
Proposal — fan-out lives at the monitor.

Action kind mapping (spec docs/spec-app-permission-drift-2026-05-25.md §"Generator implementation outline"):

| Signal kind                 | Action                                                                 |
|-----------------------------|------------------------------------------------------------------------|
| declared_not_allowed        | UpdateExecApproval(operation="add", scope="agent", agent_id="main")    |
| allowed_not_declared        | UpdateExecApproval(operation="revoke", scope="agent", agent_id="main") |
| workspace_orphan_script     | Investigation                                                          |
| declared_missing_file       | Investigation                                                          |
| workspace_walk_truncated    | Investigation                                                          |

Primary bots are filtered out for the two UpdateExecApproval kinds —
exec-approvals mutations on a primary bot don't make sense (primary bots
are deny by carve-out today; the spec-evo-account-separation follow-on
makes evo a non-privileged user where exec-approvals operate normally).
The monitor still emits the Signal for visibility; this generator just
declines to author a mutation proposal.

Investigation kinds always emit regardless of role — operator visibility
is the point.
"""

from __future__ import annotations

from typing import Any

from schema.proposal import (
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    UpdateExecApproval,
    new_proposal_id,
)

from evolve_config import bot_label


GENERATOR_ID = "app_permission_drift"
DIMENSION = "safety"


# Phase A.5 dismiss signatures — per-(kind, pattern) so dismissing
# one stale entry doesn't suppress findings on other patterns.
def _ds_kind(kind: str, pattern: str = "") -> str:
    if pattern:
        return f"{GENERATOR_ID}:{kind}:{pattern}"
    return f"{GENERATOR_ID}:{kind}"

# Mirror of permissions.app_manifest_monitor's constants. Repeated here
# rather than imported to keep the generator self-contained (the analyzer
# package can be imported without admin-side deps available).
KIND_DECLARED_NOT_ALLOWED = "declared_not_allowed"
KIND_ALLOWED_NOT_DECLARED = "allowed_not_declared"
KIND_WORKSPACE_ORPHAN_SCRIPT = "workspace_orphan_script"
KIND_DECLARED_MISSING_FILE = "declared_missing_file"
KIND_WORKSPACE_WALK_TRUNCATED = "workspace_walk_truncated"


def _signal_attr(signal: Any, key: str, default: Any = None) -> Any:
    """Read from a Signal dataclass or a plain dict (tests use dicts)."""
    if isinstance(signal, dict):
        return signal.get(key, default)
    return getattr(signal, key, default)


def _iter_items(details: dict) -> list[dict]:
    """Yield per-pattern item dicts from a rollup Signal, or one synthetic
    item from a legacy per-pattern Signal.

    Rollup shape (current): ``details.items = [{pattern, app_id, app_name,
    former_declaring_app_id?}, ...]``. Legacy shape (pre-rollup, still
    in store until sweep_resolve clears): top-level ``details.pattern``
    / ``details.app_id`` / ``details.app_name``.
    """
    items = details.get("items")
    out: list[dict] = []
    if isinstance(items, list) and items:
        for it in items:
            if isinstance(it, dict):
                out.append(it)
        return out
    # Legacy: synthesize one item from top-level details.
    synthetic = {
        "pattern": details.get("pattern") or "",
        "app_id": details.get("app_id"),
        "app_name": details.get("app_name"),
    }
    if details.get("former_declaring_app_id") is not None:
        synthetic["former_declaring_app_id"] = details["former_declaring_app_id"]
    out.append(synthetic)
    return out


def make_proposals(signal: Any) -> list[Proposal]:
    """Dispatch by ``details.kind``. Returns one Proposal per item in
    ``details.items[]`` (or one Proposal for a legacy per-item signal)."""
    details = _signal_attr(signal, "details") or {}
    if not isinstance(details, dict):
        return []
    kind = details.get("kind") or ""
    bot_id = _signal_attr(signal, "bot_id") or ""
    sig_id = _signal_attr(signal, "id") or ""
    current_mode = details.get("current_mode") or ""
    role = (details.get("role") or "member").lower().strip()
    rationale = details.get("rationale") or ""

    if not bot_id or not kind:
        return []

    out: list[Proposal] = []
    for item in _iter_items(details):
        pattern = item.get("pattern") or ""
        if not pattern:
            # walk_truncated has no per-item pattern in the rollup
            # shape — only emit it if the kind is truncated.
            if kind != KIND_WORKSPACE_WALK_TRUNCATED:
                continue
        app_id = item.get("app_id") or None
        app_name = item.get("app_name") or app_id or "(unattributed)"

        if kind == KIND_DECLARED_NOT_ALLOWED:
            out.extend(_declared_not_allowed_proposal(
                bot_id=bot_id, pattern=pattern, sig_id=sig_id,
                current_mode=current_mode, role=role,
                app_id=app_id, app_name=app_name, rationale=rationale,
            ))
        elif kind == KIND_ALLOWED_NOT_DECLARED:
            out.extend(_allowed_not_declared_proposal(
                bot_id=bot_id, pattern=pattern, sig_id=sig_id,
                role=role, rationale=rationale,
            ))
        elif kind == KIND_WORKSPACE_ORPHAN_SCRIPT:
            out.extend(_orphan_script_investigation(
                bot_id=bot_id, pattern=pattern, sig_id=sig_id,
                current_mode=current_mode, rationale=rationale,
            ))
        elif kind == KIND_DECLARED_MISSING_FILE:
            out.extend(_missing_file_investigation(
                bot_id=bot_id, pattern=pattern, sig_id=sig_id,
                app_id=app_id, app_name=app_name, rationale=rationale,
            ))
        elif kind == KIND_WORKSPACE_WALK_TRUNCATED:
            # Singular finding; only one item ever lands here.
            out.extend(_walk_truncated_investigation(
                bot_id=bot_id, sig_id=sig_id, rationale=rationale,
            ))
            break  # truncation is per-bot, not per-item
    return out


# ── declared_not_allowed → UpdateExecApproval(add) ───────────────────────────


def _declared_not_allowed_proposal(
    *,
    bot_id: str,
    pattern: str,
    sig_id: str,
    current_mode: str,
    role: str,
    app_id: str | None,
    app_name: str,
    rationale: str,
) -> list[Proposal]:
    # Phase E.4 (2026-05-25) — the primary-bot guard that used to short-
    # circuit this generator for evo (or any role=primary bot) has been
    # removed. Phase E.2.b cut evo over to the unprivileged ``evo`` macOS
    # user, so exec-approvals proposals against primary bots are now
    # treated uniformly: the proposal pipeline + applier pair work for
    # evo exactly like for any member bot.
    _ = role  # kept on the signature for compatibility with the caller.

    bot_name = bot_label(bot_id)
    if current_mode == "allowlist":
        urgency = "critical"
        problem = (
            f"{bot_name}: app {app_name!r} declares the exec entry "
            f"{pattern!r}, but no matching pattern exists in the bot's "
            f"exec-approvals.json. The bot is in allowlist mode — this "
            f"script cannot run until the entry is added. Applying this "
            f"proposal adds the entry."
        )
        admin_summary = f"Add {pattern} to {bot_name} allowlist"
    else:
        # full mode (deny is suppressed at the monitor level)
        urgency = "improvement"
        problem = (
            f"{bot_name}: app {app_name!r} declares the exec entry "
            f"{pattern!r}, but it's not present in the bot's "
            f"exec-approvals.json. The bot is in full mode, so this "
            f"script currently runs unconditionally — but a future switch "
            f"to allowlist mode (or Phase C's opt-in toggle) would break "
            f"this script unless the entry is added. Applying this "
            f"proposal seeds the entry preemptively; safe in any mode."
        )
        admin_summary = f"Seed {pattern} for future {bot_name} allowlist"

    proposal_id = new_proposal_id()
    return [Proposal(
        id=proposal_id,
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"app_permission_drift:{bot_id}:declared_not_allowed:{pattern}"],
        provenance=Provenance(
            technique="app_permission_drift.declared_not_allowed",
            signals={
                "bot_id": bot_id,
                "pattern": pattern,
                "app_id": app_id,
                "app_name": app_name,
                "current_mode": current_mode,
                "rationale": rationale,
            },
            confidence=0.92,
        ),
        problem=problem,
        action=UpdateExecApproval(
            bot_id=bot_id,
            operation="add",
            pattern=pattern,
            agent_id="main",
            scope="agent",
        ),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["auth_config"],
        ),
        claim=None,
        approval_audience="pod_operator",
        urgency=urgency,
        admin_surface_summary=admin_summary[:120],
        motivating_signals=[sig_id] if sig_id else [],
        dismiss_signature=_ds_kind("declared_not_allowed", pattern),
        dismiss_scope="kind",
    )]


# ── allowed_not_declared → UpdateExecApproval(revoke) ────────────────────────


def _allowed_not_declared_proposal(
    *,
    bot_id: str,
    pattern: str,
    sig_id: str,
    role: str,
    rationale: str,
) -> list[Proposal]:
    # Phase E.4 (2026-05-25) — primary-bot guard removed for symmetry
    # with ``_declared_not_allowed_proposal``. Post-cutover, evo runs
    # as the unprivileged ``evo`` user and exec-approvals mutations
    # against it are no longer a special case.
    _ = role

    bot_name = bot_label(bot_id)
    problem = (
        f"{bot_name}: exec-approvals.json carries the entry {pattern!r}, "
        f"but no installed app manifest declares it. Likely a stale "
        f"app-derived entry — the declaring app was retired but the "
        f"allowlist entry survived. Applying this proposal revokes the "
        f"entry. If this entry is operator-set (sysadmin command, not "
        f"app-related), reject this proposal — Phase C will add explicit "
        f"provenance tagging that prevents this false positive."
    )
    proposal_id = new_proposal_id()
    return [Proposal(
        id=proposal_id,
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"app_permission_drift:{bot_id}:allowed_not_declared:{pattern}"],
        provenance=Provenance(
            technique="app_permission_drift.allowed_not_declared",
            signals={
                "bot_id": bot_id,
                "pattern": pattern,
                "rationale": rationale,
            },
            confidence=0.7,  # heuristic-based; lower than declared-not-allowed
        ),
        problem=problem,
        action=UpdateExecApproval(
            bot_id=bot_id,
            operation="revoke",
            pattern=pattern,
            agent_id="main",
            scope="agent",
        ),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["auth_config"],
        ),
        claim=None,
        approval_audience="pod_operator",
        urgency="hygiene",
        admin_surface_summary=f"Revoke stale {pattern} on {bot_name}"[:120],
        motivating_signals=[sig_id] if sig_id else [],
        dismiss_signature=_ds_kind("allowed_not_declared", pattern),
        dismiss_scope="kind",
    )]


# ── workspace_orphan_script → Investigation ──────────────────────────────────


def _orphan_script_investigation(
    *,
    bot_id: str,
    pattern: str,
    sig_id: str,
    current_mode: str,
    rationale: str,
) -> list[Proposal]:
    if current_mode == "allowlist":
        running_note = (
            "The bot is in allowlist mode — this script is not in the "
            "allowlist either, so it can't run today even if invoked. "
            "Decide whether to declare it (add to a manifest, which would "
            "queue an allowlist-add proposal) or retire it (delete from "
            "the workspace)."
        )
    else:
        running_note = (
            "The bot is in full mode — this script can still run, but it's "
            "outside the manifest's purview from a visibility standpoint. "
            "Bind it to an app's files/realized_files (preferred) or retire "
            "the file."
        )

    bot_name = bot_label(bot_id)
    context = (
        f"{bot_name}: workspace script {pattern!r} is not declared by any "
        f"installed app manifest. {running_note}"
    )

    proposal_id = new_proposal_id()
    return [Proposal(
        id=proposal_id,
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"app_permission_drift:{bot_id}:workspace_orphan_script:{pattern}"],
        provenance=Provenance(
            technique="app_permission_drift.workspace_orphan_script",
            signals={
                "bot_id": bot_id,
                "pattern": pattern,
                "current_mode": current_mode,
                "rationale": rationale,
            },
            confidence=0.85,
        ),
        problem=context,
        action=Investigation(context=context),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",  # Investigation has no state change
            touches=["manifest"],  # logical target, not direct write
        ),
        claim=None,
        approval_audience="pod_operator",
        urgency="hygiene",
        admin_surface_summary=f"Orphan script {pattern} on {bot_name}"[:120],
        motivating_signals=[sig_id] if sig_id else [],
        dismiss_signature=_ds_kind("workspace_orphan_script", pattern),
        dismiss_scope="kind",
    )]


# ── declared_missing_file → Investigation ────────────────────────────────────


def _missing_file_investigation(
    *,
    bot_id: str,
    pattern: str,
    sig_id: str,
    app_id: str | None,
    app_name: str,
    rationale: str,
) -> list[Proposal]:
    bot_name = bot_label(bot_id)
    context = (
        f"{bot_name}: app {app_name!r} declares the file {pattern!r} but it "
        f"doesn't exist in the bot's workspace. Stale declaration. "
        f"Propose removing it from the manifest (B.2's app_permission_review "
        f"will produce a typed manifest-narrowing proposal once it lands)."
    )
    proposal_id = new_proposal_id()
    return [Proposal(
        id=proposal_id,
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"app_permission_drift:{bot_id}:declared_missing_file:{pattern}"],
        provenance=Provenance(
            technique="app_permission_drift.declared_missing_file",
            signals={
                "bot_id": bot_id,
                "pattern": pattern,
                "app_id": app_id,
                "app_name": app_name,
                "rationale": rationale,
            },
            confidence=0.9,
        ),
        problem=context,
        action=Investigation(context=context),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["manifest"],
        ),
        claim=None,
        approval_audience="pod_operator",
        urgency="hygiene",
        admin_surface_summary=f"Stale manifest declaration {pattern} on {bot_name}"[:120],
        motivating_signals=[sig_id] if sig_id else [],
        dismiss_signature=_ds_kind("declared_missing_file", pattern),
        dismiss_scope="kind",
    )]


# ── workspace_walk_truncated → Investigation ─────────────────────────────────


def _walk_truncated_investigation(
    *,
    bot_id: str,
    sig_id: str,
    rationale: str,
) -> list[Proposal]:
    bot_name = bot_label(bot_id)
    context = (
        f"{bot_name}: the orphan-script walk on this bot's workspace hit "
        f"its bounds and didn't enumerate every file. Some orphan scripts "
        f"may have gone undetected. {rationale} Consider tightening the "
        f"workspace skip-dirs in app_manifest_monitor or accepting the "
        f"bound as deliberate."
    )
    proposal_id = new_proposal_id()
    return [Proposal(
        id=proposal_id,
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"app_permission_drift:{bot_id}:workspace_walk_truncated"],
        provenance=Provenance(
            technique="app_permission_drift.workspace_walk_truncated",
            signals={"bot_id": bot_id, "rationale": rationale},
            confidence=0.6,
        ),
        problem=context,
        action=Investigation(context=context),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=[],
        ),
        claim=None,
        approval_audience="pod_operator",
        urgency="hygiene",
        admin_surface_summary=f"Workspace walk truncated on {bot_name}"[:120],
        dismiss_signature=_ds_kind("workspace_walk_truncated"),
        dismiss_scope="kind",
        motivating_signals=[sig_id] if sig_id else [],
    )]
