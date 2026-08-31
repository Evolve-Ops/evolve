"""generators.manifest_quality.signal_proposals — Signal → Proposal factories.

Builds Investigation Proposals from compliance_scan Signals. One factory
per consumed signal type, returning a list of Proposals — one per item
in the rollup signal's ``details.items[]``. The scanner now emits a
single rollup Signal per (bot, issue_type), so a bot with 39 stale
manifests produces 1 Signal + 39 Proposals (rather than 39 Signals +
39 Proposals as in the per-item-signal era).

Investigation (claim=None) is the right shape — the *response* depends
on context the operator has (is the app still in active use? was the
test failure expected?) and that context isn't on disk.

Note: ``missing_required_field`` signals are intentionally NOT consumed
here. Per the 2026-05-25 triage decision, manifest completeness for an
existing app doesn't justify a proposal — the scanner already
auto-backfills the common case (``description`` from ``identity.purpose``)
and the remaining gaps (id, name, bot_id, status) are structural defects
that belong on audit/alerts, not in the proposal queue.
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


GENERATOR_ID = "manifest_quality"
DIMENSION = "app_quality"


# ── Dismiss signatures (Phase A.5 + Phase C-6) ──────────────────────────────
#
# Per-(kind, app) granularity: dismissing a stale finding for app X
# doesn't suppress the same finding for app Y, AND doesn't suppress a
# validation_error finding for app X. Three kinds × N apps = many
# distinct signatures, exactly what we want.
def dismiss_signature_for(kind: str, app_id: str) -> str:
    return f"{GENERATOR_ID}:{kind}:{app_id}"


def _signal_dict_get(signal: Any, key: str, default: Any = None) -> Any:
    """Read from a Signal dataclass or a plain dict — useful for tests."""
    if isinstance(signal, dict):
        return signal.get(key, default)
    return getattr(signal, key, default)


def _iter_signal_items(signal: Any) -> Iterator[dict[str, Any]]:
    """Yield per-item dicts from a rollup Signal, or one synthetic item
    from a legacy per-item Signal.

    Rollup shape (current): ``details.items = [{app_id, path, cron,
    message}, ...]``. Legacy shape (pre-rollup, still in store until
    sweep_resolve clears): top-level ``details.app_id`` /
    ``details.path`` / ``details.cron`` / ``details.message``.
    """
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
    """Return (bot_id, sig_id) — the per-Signal fields shared by every item."""
    bot_id = _signal_dict_get(signal, "bot_id") or "<unknown>"
    sig_id = _signal_dict_get(signal, "id") or ""
    return bot_id, sig_id


def make_stale_proposal(signal: Any) -> list[Proposal]:
    """`stale` Signal → list of Investigation Proposals — one per item."""
    bot_id, sig_id = _signal_basics(signal)
    out: list[Proposal] = []
    for item in _iter_signal_items(signal):
        app_id = item.get("app_id") or "<unknown>"
        message = item.get("message") or ""
        out.append(_build_stale_proposal(bot_id, sig_id, app_id, message))
    return out


def _build_stale_proposal(
    bot_id: str, sig_id: str, app_id: str, message: str
) -> Proposal:
    bot_name = bot_label(bot_id)
    problem = f"{bot_name}: {app_id} manifest is stale — {message}"
    headline = f"Review {bot_name}'s {app_id} manifest"

    # ── Phase C-6 operator-first content (Tier 2 — UI manual) ───────────────
    summary = (
        f"The manifest for `{app_id}` on {bot_name} hasn't been reviewed "
        f"in over 90 days ({message}). Manifest review keeps the "
        f"on-disk description honest about what the app does. If the "
        f"app is still in use, mark it reviewed; if not, archive it."
    )
    explanation = (
        f"Application manifests carry a `last_reviewed` timestamp so "
        f"the pod can spot apps that drifted out of regular review. "
        f"The cadence is conservative — 90 days is long enough that a "
        f"working app rarely trips this finding, but short enough "
        f"that abandoned apps eventually surface.\n\n"
        f"Diagnosis. {app_id}'s manifest was last reviewed beyond "
        f"the 90-day threshold ({message}). We don't know whether "
        f"the app is still in use or just hasn't needed changes.\n\n"
        f"What to do. (1) If the app is still in use, open it on the "
        f"Applications tab, verify the description + status fields, "
        f"and click the review affordance to update `last_reviewed`. "
        f"(2) If the app is no longer used, archive it (Applications "
        f"→ app detail → archive). Either action clears this finding "
        f"on the next compliance scan.\n\n"
        f"What could go wrong. Dismissing without acting leaves the "
        f"staleness flag set; the finding re-emerges next scan. Mark "
        f"reviewed or archive to actually clear it."
    )

    context = (
        f"{bot_name}'s `{app_id}` manifest hasn't been reviewed in "
        f"over the 90-day threshold ({message}). Manifest review keeps "
        f"the on-disk description honest about what the app does, what bots "
        f"use it, and whether it's still in active service.\n\n"
        f"**What to do:**\n"
        f"- If the app is still in use: open it on the Applications tab, "
        f"verify the description and status fields, then click the manifest's "
        f"review affordance to update `last_reviewed`.\n"
        f"- If the app is no longer used: archive it instead (Applications "
        f"tab → app detail → archive). The compliance scan stops nagging "
        f"once the manifest is archived.\n\n"
        f"Dismiss this proposal if you've already handled it elsewhere "
        f"or the staleness is deliberate."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"stale:{bot_id}:{app_id}"],
        provenance=Provenance(
            technique="manifest_quality.stale",
            signals={"app_id": app_id, "message": message},
            confidence=0.9,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=headline[:120],
        motivating_signals=[sig_id] if sig_id else [],
        # ── Phase C-6 operator-first content (Tier 2 — UI manual) ───────
        summary=summary,
        explanation=explanation,
        action_label="Open Applications tab",
        manual_path=f"Applications → {app_id}",
        dismiss_signature=dismiss_signature_for("stale", app_id),
        dismiss_scope="kind",
    )


def make_test_failing_proposal(signal: Any) -> list[Proposal]:
    """`test_failing` Signal → list of Investigation Proposals — one per item."""
    bot_id, sig_id = _signal_basics(signal)
    out: list[Proposal] = []
    for item in _iter_signal_items(signal):
        app_id = item.get("app_id") or "<unknown>"
        message = item.get("message") or ""
        out.append(_build_test_failing_proposal(bot_id, sig_id, app_id, message))
    return out


def _build_test_failing_proposal(
    bot_id: str, sig_id: str, app_id: str, message: str
) -> Proposal:
    bot_name = bot_label(bot_id)
    problem = f"{bot_name}: {app_id} tests are failing — {message}"
    headline = f"Tests are failing for {bot_name}'s {app_id}"

    # ── Phase C-6 operator-first content (Tier 5 — paste-to-bot) ────────────
    summary = (
        f"`{app_id}` on {bot_name} has a failing test ({message}). The "
        f"bot can read its own test history and propose a fix. Common "
        f"causes are a recent script change, an expired upstream "
        f"credential, or a stale test fixture — all things the bot is "
        f"positioned to investigate."
    )
    explanation = (
        f"Apps on this pod ship with tests so the engine can spot "
        f"regressions when scripts change. When a test starts "
        f"failing, the app's behavior may have already started "
        f"failing too — the test is the canary.\n\n"
        f"Diagnosis. {app_id}'s most recent test exit code is "
        f"non-zero ({message}). Usual causes: (1) recent change in "
        f"the app's scripts broke a precondition, (2) upstream "
        f"dependency or credential drift (token expired, API shape "
        f"changed), (3) the test itself bit-rot — fixture moved, "
        f"sample data stale.\n\n"
        f"What to do. Paste the instruction below to the bot. It "
        f"can read its own test history, surface the recent log "
        f"tails, identify which of the three causes fits, and "
        f"propose a fix. Once the test passes again, the next "
        f"compliance scan clears this proposal automatically.\n\n"
        f"What could go wrong. If the failure is from a deliberate "
        f"app change in flight (refactor underway, scaffold not "
        f"finished), the right call is to dismiss this finding "
        f"temporarily — the compliance scan will re-surface it "
        f"after the change lands and you can confirm tests pass."
    )

    manual_instruction = (
        f"One of your app `{app_id}`'s tests is failing ({message}). "
        f"Read the test history under Applications → {app_id} → "
        f"Test history (last few runs + exit code + log tail). "
        f"Identify whether the cause is (a) a recent script change "
        f"you made, (b) an upstream credential or API drift, or "
        f"(c) test bit-rot. Propose a concrete fix for whichever "
        f"applies and explain the trade-off if there's a choice "
        f"between fixing the app vs. fixing the test."
    )
    context = (
        f"{bot_name}'s `{app_id}` app has a non-zero last-test exit "
        f"code ({message}).\n\n"
        f"**Common causes:**\n"
        f"- A recent change in the app's scripts broke a precondition.\n"
        f"- An upstream dependency or credential drifted (token expired, API "
        f"shape changed).\n"
        f"- The test itself bit-rot — fixture file moved, sample data stale.\n\n"
        f"**Where to look:**\n"
        f"- Applications tab → `{app_id}` → Test history (last few runs "
        f"+ exit code + log tail).\n"
        f"- The bot's recent Sessions to see whether use of this app has "
        f"started failing too.\n\n"
        f"Once the cause is fixed and the test passes again, the next "
        f"compliance scan will clear this proposal automatically."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"test_failing:{bot_id}:{app_id}"],
        provenance=Provenance(
            technique="manifest_quality.test_failing",
            signals={"app_id": app_id, "message": message},
            confidence=0.95,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=headline[:120],
        motivating_signals=[sig_id] if sig_id else [],
        # ── Phase C-6 operator-first content (Tier 5 — paste-to-bot) ────
        summary=summary,
        explanation=explanation,
        manual_instruction=manual_instruction,
        dismiss_signature=dismiss_signature_for("test_failing", app_id),
        dismiss_scope="kind",
    )


def make_validation_error_proposal(signal: Any) -> list[Proposal]:
    """`validation_error` Signal → list of Investigation Proposals — one per item."""
    bot_id, sig_id = _signal_basics(signal)
    out: list[Proposal] = []
    for item in _iter_signal_items(signal):
        app_id = item.get("app_id") or "<unknown>"
        message = item.get("message") or ""
        out.append(_build_validation_error_proposal(bot_id, sig_id, app_id, message))
    return out


def _build_validation_error_proposal(
    bot_id: str, sig_id: str, app_id: str, message: str
) -> Proposal:
    bot_name = bot_label(bot_id)
    problem = (
        f"{bot_name}: {app_id} manifest failed schema validation — "
        f"{message}"
    )
    headline = f"Fix {bot_name}'s {app_id} manifest"

    # ── Phase C-6 operator-first content (Tier 2 — UI manual) ───────────────
    summary = (
        f"`{app_id}`'s manifest on {bot_name} failed schema validation "
        f"({message}). Usually a one-line edit on the Applications "
        f"tab — the validator names the offending field path. After "
        f"saving, the next compliance scan re-validates and clears "
        f"this finding."
    )
    explanation = (
        f"Manifests are JSON files validated against a schema at "
        f"every compliance scan. When validation fails, the engine "
        f"can't trust the manifest's claims about the app — what it "
        f"does, what bots use it, what permissions it needs. Fixing "
        f"the validation error restores that trust.\n\n"
        f"Diagnosis. {app_id}'s manifest didn't match the schema "
        f"({message}). Common shapes: a value type that doesn't "
        f"match (string where dict expected, etc.), an enum value "
        f"out of range, or a stale schema version not migrated.\n\n"
        f"What to do. Open `{app_id}` on the Applications tab. The "
        f"validator's error message above names the offending field "
        f"path so the fix is usually a one-line edit. Save, and the "
        f"next compliance scan re-validates.\n\n"
        f"What could go wrong. If the schema itself was recently "
        f"tightened and existing manifests genuinely need a "
        f"migration pass (rather than per-field fixes), surface that "
        f"as a separate concern — fixing one manifest at a time is "
        f"fine but expensive across N apps."
    )
    context = (
        f"{bot_name}'s `{app_id}` manifest failed JSON-schema "
        f"validation ({message}). Common shapes: a value type "
        f"that doesn't match the schema (string where dict expected, etc.), "
        f"an enum value out of range, or a stale schema version not migrated.\n\n"
        f"Open the manifest in the Applications tab. Validation errors "
        f"surface the offending field path so the fix is usually a one-line "
        f"edit. After saving, the next compliance scan re-validates and "
        f"clears this proposal."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"validation_error:{bot_id}:{app_id}"],
        provenance=Provenance(
            technique="manifest_quality.validation_error",
            signals={"app_id": app_id, "message": message},
            confidence=0.99,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="hygiene",
        admin_surface_summary=headline[:120],
        motivating_signals=[sig_id] if sig_id else [],
        # ── Phase C-6 operator-first content (Tier 2 — UI manual) ───────
        summary=summary,
        explanation=explanation,
        action_label="Open Applications tab",
        manual_path=f"Applications → {app_id}",
        dismiss_signature=dismiss_signature_for("validation_error", app_id),
        dismiss_scope="kind",
    )


SIGNAL_TYPE_TO_FACTORY: dict[str, Callable[[Any], list[Proposal]]] = {
    "stale": make_stale_proposal,
    "test_failing": make_test_failing_proposal,
    "validation_error": make_validation_error_proposal,
}
