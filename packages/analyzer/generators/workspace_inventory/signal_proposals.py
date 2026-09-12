"""generators.workspace_inventory.signal_proposals — Signal → Proposal factories.

One factory per consumed signal type. Both emit Investigation Proposals
— the operator decides whether to register a manifest, delete the
orphan, or suppress the finding.

Each factory returns a list of Proposals — one per item in the rollup
Signal's ``details.items[]`` — so a bot with 12 unregistered scripts
produces 1 Signal + 12 Proposals (rather than 12 Signals + 12 Proposals
as in the per-item-signal era).
"""

from __future__ import annotations

from typing import Any, Callable, Iterator

from schema.proposal import (
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)

from evolve_config import bot_label


GENERATOR_ID = "workspace_inventory"
DIMENSION = "app_quality"


# ── Dismiss signatures (Phase A.5 + Phase C-8) ──────────────────────────────
def dismiss_signature_for_script(path: str) -> str:
    return f"workspace_inventory:unregistered_script:{path}"


def dismiss_signature_for_cron(cron: str) -> str:
    # Truncate to keep the signature bounded; full text is in details.
    return f"workspace_inventory:unregistered_cron:{cron[:80]}"


def _signal_dict_get(signal: Any, key: str, default: Any = None) -> Any:
    if isinstance(signal, dict):
        return signal.get(key, default)
    return getattr(signal, key, default)


def _iter_signal_items(signal: Any) -> Iterator[dict[str, Any]]:
    """Yield per-item dicts from a rollup Signal, or one synthetic item
    from a legacy per-item Signal (see manifest_quality for shape doc)."""
    details = _signal_dict_get(signal, "details") or {}
    items = details.get("items") if isinstance(details, dict) else None
    if isinstance(items, list) and items:
        for item in items:
            if isinstance(item, dict):
                yield item
        return
    synthetic: dict[str, Any] = {}
    if isinstance(details, dict):
        for k in ("app_id", "path", "cron", "message"):
            v = details.get(k)
            if v is not None:
                synthetic[k] = v
    yield synthetic


def _signal_basics(signal: Any) -> tuple[str, str]:
    bot_id = _signal_dict_get(signal, "bot_id") or "<unknown>"
    sig_id = _signal_dict_get(signal, "id") or ""
    return bot_id, sig_id


def make_unregistered_script_proposal(signal: Any) -> list[Proposal]:
    """`unregistered_script` Signal → list of Investigation Proposals."""
    bot_id, sig_id = _signal_basics(signal)
    out: list[Proposal] = []
    for item in _iter_signal_items(signal):
        path = item.get("path") or "<unknown>"
        message = item.get("message") or ""
        out.append(_build_unregistered_script_proposal(bot_id, sig_id, path, message))
    return out


def _build_unregistered_script_proposal(
    bot_id: str, sig_id: str, path: str, message: str
) -> Proposal:
    bot_name = bot_label(bot_id)
    problem = f"{bot_name}: unregistered script in workspace — {path}"
    headline = f"Decide what to do with {bot_name}'s {path}"
    summary = (
        f"{bot_name}'s workspace has `{path}` but no manifest claims "
        f"it. Either the script belongs to an app (register it), is "
        f"dead code (remove it), or is intentional infra that "
        f"doesn't need a manifest (suppress)."
    )
    explanation = (
        f"Every script in a bot's workspace should be accounted for "
        f"by some app manifest. The compliance scan answers the "
        f"\"what does this bot actually do?\" question by walking "
        f"the manifest registry; unregistered scripts are gaps the "
        f"scan can't see through.\n\n"
        f"Diagnosis. {path} exists in {bot_name}'s workspace but no "
        f"manifest's `file_paths` includes it. The script either "
        f"belongs to an app and needs to be linked, or it's leftover "
        f"and should go.\n\n"
        f"Three resolutions. (1) Register: add the path to the "
        f"owning app's manifest, or run the Applications scanner "
        f"to auto-discover and create a manifest. (2) Remove: "
        f"delete the file if it's dead code. (3) Suppress: set "
        f"`compliance_suppressed: true` on a neighboring manifest "
        f"if this is intentional infra.\n\n"
        f"What could go wrong. Removing a script the bot relies on "
        f"breaks the bot's behavior. If you're unsure, register "
        f"first — that's reversible; deletion isn't."
    )
    context = (
        f"{bot_name}'s workspace contains `{path}` but no manifest claims it. "
        f"Unregistered scripts are a compliance blind spot: the system can't "
        f"answer 'what apps does {bot_name} actually run?' if scripts exist "
        f"outside the manifest registry.\n\n"
        f"**Three resolutions:**\n"
        f"1. **Register it** — the script belongs to an existing app: add "
        f"the path to that app's manifest `file_paths`. Or run the workspace "
        f"scanner (Applications tab → Run scanner) to auto-discover and "
        f"create a manifest.\n"
        f"2. **Remove it** — the script is dead code: delete from the "
        f"workspace.\n"
        f"3. **Suppress it** — the script is intentional infra not meant to "
        f"be a registered app: set `compliance_suppressed: true` on an "
        f"adjacent manifest that owns the same area.\n\n"
        f"Source: {message}"
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"unregistered_script:{bot_id}:{path}"],
        provenance=Provenance(
            technique="workspace_inventory.unregistered_script",
            signals={"path": path, "message": message},
            confidence=0.9,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="hygiene",
        admin_surface_summary=headline[:120],
        motivating_signals=[sig_id] if sig_id else [],
        # ── Phase C-8 operator-first content (Tier 2 — UI manual) ───────
        summary=summary,
        explanation=explanation,
        action_label="Open Applications tab",
        manual_path=f"Applications → {bot_name}",
        dismiss_signature=dismiss_signature_for_script(path),
        dismiss_scope="kind",
    )


def make_unregistered_cron_proposal(signal: Any) -> list[Proposal]:
    """`unregistered_cron` Signal → list of Investigation Proposals."""
    bot_id, sig_id = _signal_basics(signal)
    out: list[Proposal] = []
    for item in _iter_signal_items(signal):
        cron = item.get("cron") or "<unknown>"
        message = item.get("message") or ""
        out.append(_build_unregistered_cron_proposal(bot_id, sig_id, cron, message))
    return out


def _build_unregistered_cron_proposal(
    bot_id: str, sig_id: str, cron: str, message: str
) -> Proposal:
    bot_name = bot_label(bot_id)
    # Truncate the cron entry for display — full text lives in the
    # signal details and the rendered context block.
    cron_short = cron if len(cron) <= 80 else cron[:77] + "..."

    problem = f"{bot_name}: unregistered cron entry — {cron_short}"
    headline = f"Decide what to do with an unregistered cron on {bot_name}"
    summary = (
        f"{bot_name}'s crontab has an entry that no manifest declares. "
        f"Either it belongs to an app (register it), or it's leftover "
        f"after an app was removed (delete it). Leftover crons quietly "
        f"keep waking the agent and burning money."
    )
    explanation = (
        f"Crons that aren't owned by an app are invisible to the "
        f"compliance + cost systems. They keep firing on schedule, "
        f"the agent keeps responding, and nobody can answer \"why "
        f"is this bot waking up at 3am?\".\n\n"
        f"Diagnosis. The crontab includes `{cron_short}` but no "
        f"manifest's `crons` block references it.\n\n"
        f"What to do. Two paths. (1) If the entry is part of an "
        f"app you know about, register it under that app's manifest. "
        f"(2) If it's stale (an app was retired but the cron wasn't), "
        f"remove the entry from the crontab.\n\n"
        f"What could go wrong. Removing a cron that's actually "
        f"load-bearing breaks whatever it was doing. Check what the "
        f"shell payload does before deleting; if you're unsure, "
        f"register it under a placeholder app first to keep it "
        f"visible while you investigate."
    )
    context = (
        f"{bot_name}'s live crontab includes an entry that no manifest "
        f"declares:\n\n"
        f"    {cron}\n\n"
        f"Unregistered crons run on the bot's behalf without the app-quality "
        f"system being aware. If this entry is part of a known app, register "
        f"it on that app's manifest `crons` block so future scans recognize "
        f"it. If it's stale, remove it from the bot's crontab — leftover "
        f"crons after an app retirement are a common cost / drift source.\n\n"
        f"Source: {message}"
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"unregistered_cron:{bot_id}"],
        provenance=Provenance(
            technique="workspace_inventory.unregistered_cron",
            signals={"cron": cron[:200], "message": message},
            confidence=0.9,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="hygiene",
        admin_surface_summary=headline[:120],
        motivating_signals=[sig_id] if sig_id else [],
        # ── Phase C-8 operator-first content (Tier 2 — UI manual) ───────
        summary=summary,
        explanation=explanation,
        action_label="Open Applications tab",
        manual_path=f"Applications → {bot_name}",
        dismiss_signature=dismiss_signature_for_cron(cron),
        dismiss_scope="kind",
    )


SIGNAL_TYPE_TO_FACTORY: dict[str, Callable[[Any], list[Proposal]]] = {
    "unregistered_script": make_unregistered_script_proposal,
    "unregistered_cron": make_unregistered_cron_proposal,
}
