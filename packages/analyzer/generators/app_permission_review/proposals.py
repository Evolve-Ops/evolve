"""generators.app_permission_review.proposals — ConsolidatedFinding → Investigation factory.

All B.2 proposals are Investigation-shaped in v1 (per the sub-spec /
scope agreement). The factory's job is to build per-finding operator-
readable context strings that frame the change correctly given the
consolidation outcome.

Operator framing matters here. A "remove this entry" finding lands
very differently when:

- No sibling references the resource → "this entry is dead; remove it"
- A sibling declares it too → "this entry is redundant; remove from app
  A but the bot retains the permission via app B"
- A sibling uses it without declaring → "move this from app A to app B"

Same underlying finding, three different proposals.
"""
from __future__ import annotations

from schema.proposal import (
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)

from generators.app_permission_review.consolidation import (
    OUTCOME_AS_IS,
    OUTCOME_MOVE_TO_SIBLING,
    OUTCOME_SIBLING_ALREADY_HAS,
    OUTCOME_SIBLING_DECLARES,
    ConsolidatedFinding,
)
from generators.app_permission_review.review import (
    ALL_MISSING_KINDS,
    ALL_OVERKILL_KINDS,
    ALL_UNUSED_KINDS,
)


GENERATOR_ID = "app_permission_review"
DIMENSION = "safety"


# ── Dismiss signatures (Phase A.5 + Phase C-3) ──────────────────────────────
#
# Per-finding granularity: the signature includes app + entry kind + value
# so dismissing "fs_read on path X for app Y" does NOT suppress "fs_read on
# path Z for app Y" or "fs_read on path X for app Q". The store layer
# handles per-bot scoping; the signature itself is bot-agnostic, so the
# same app/value finding on two different bots can be dismissed
# independently (each call to record_dismissal carries the bot_id).
def dismiss_signature_for_finding(
    *,
    kind: str,
    app_id: str,
    entry_kind: str,
    entry_value: str,
) -> str:
    return f"{GENERATOR_ID}:{kind}:{app_id}:{entry_kind}:{entry_value}"


def _format_sibling_list(sibling_apps: list[str]) -> str:
    """Format the sibling list for proposal context strings."""
    if not sibling_apps:
        return ""
    if len(sibling_apps) == 1:
        return repr(sibling_apps[0])
    if len(sibling_apps) == 2:
        return f"{sibling_apps[0]!r} and {sibling_apps[1]!r}"
    return ", ".join(repr(s) for s in sibling_apps[:-1]) + f", and {sibling_apps[-1]!r}"


def _format_context(cf: ConsolidatedFinding) -> str:
    """Build the Investigation context string given the consolidation outcome."""
    f = cf.finding
    base = (
        f"App {f.app_name!r} (id={f.app_id}) on {f.bot_id}: "
        f"{f.rationale}"
    )

    if cf.outcome == OUTCOME_AS_IS:
        return base

    if cf.outcome == OUTCOME_SIBLING_DECLARES:
        siblings = _format_sibling_list(cf.sibling_apps)
        return (
            base
            + f"\n\nNote: sibling app(s) {siblings} on this bot also declare "
            f"this entry in their `permissions:` block. The permission "
            f"remains in effect via the sibling declaration; this proposal "
            f"narrows {f.app_name!r}'s declared surface only — no change "
            f"to effective permissions."
        )

    if cf.outcome == OUTCOME_MOVE_TO_SIBLING:
        siblings = _format_sibling_list(cf.sibling_apps)
        return (
            base
            + f"\n\nMOVE PROPOSAL: sibling app(s) {siblings} use this resource "
            f"in their scripts but don't declare it. The right fix is to "
            f"move the declaration from {f.app_name!r} to the using app — "
            f"this makes the dependency explicit on the app that needs it.\n\n"
            f"Suggested edits:\n"
            f"  1. Remove {f.entry_value!r} from {f.app_name!r}'s "
            f"`permissions.{f.entry_kind}`.\n"
            f"  2. Add {f.entry_value!r} to the sibling app(s)' "
            f"`permissions.{f.entry_kind}`."
        )

    if cf.outcome == OUTCOME_SIBLING_ALREADY_HAS:
        siblings = _format_sibling_list(cf.sibling_apps)
        return (
            base
            + f"\n\nNote: sibling app(s) {siblings} already declare this "
            f"resource. This proposal makes the dependency explicit on "
            f"{f.app_name!r} as well, so the manifest reflects that "
            f"{f.app_name!r} relies on the resource directly."
        )

    return base


def _admin_summary(cf: ConsolidatedFinding) -> str:
    f = cf.finding
    if cf.outcome == OUTCOME_MOVE_TO_SIBLING:
        verb = "Move"
    elif f.kind in ALL_UNUSED_KINDS:
        verb = "Remove"
    elif f.kind in ALL_MISSING_KINDS:
        verb = "Add"
    elif f.kind in ALL_OVERKILL_KINDS:
        verb = "Narrow"
    else:
        verb = "Review"
    return f"{verb} {f.entry_kind}: {f.entry_value} on {f.app_name}"


def _trigger_observation(cf: ConsolidatedFinding) -> str:
    f = cf.finding
    return (
        f"app_permission_review:{f.bot_id}:{f.app_id}:"
        f"{f.kind}:{f.entry_kind}:{f.entry_value}"
    )


# ── Phase C-3 operator-first content (2026-06-04 protocol) ──────────────────
#
# 9 finding kinds × 4 consolidation outcomes = 36 combinations. Rather
# than fan out 36 template strings, the content composes from two
# orthogonal pieces:
#
#   1. The KIND humanizer (what this finding is about — unused, missing,
#      overkill) tuned per entry_kind (exec / fs_read / fs_write /
#      egress / env).
#   2. The OUTCOME suffix (whether siblings change the recommendation).
#
# Both written for the Plex-test operator: no manifest jargon in the
# Summary, no slug leaks, explicit trade-offs.


def _entry_kind_label(entry_kind: str) -> str:
    """Plex-test rendering of the technical entry_kind. Used in Summary."""
    return {
        "exec": "shell command",
        "fs_read": "file read path",
        "fs_write": "file write path",
        "network_egress": "outbound host",
        "env": "environment variable",
    }.get(entry_kind, entry_kind)


def _kind_category(kind: str) -> str:
    """Return one of: unused | missing | overkill | other."""
    if kind in ALL_UNUSED_KINDS:
        return "unused"
    if kind in ALL_MISSING_KINDS:
        return "missing"
    if kind in ALL_OVERKILL_KINDS:
        return "overkill"
    return "other"


def _phase_c_content_for(cf: ConsolidatedFinding) -> dict:
    """Return Phase C-3 content fields keyed by finding category +
    consolidation outcome.

    Returns dict with summary / explanation / action_label /
    manual_path / manual_instruction. Investigation-only proposal, so
    action_label is None — the Tier-5 manual_instruction is the
    operator's action lever.
    """
    f = cf.finding
    label = _entry_kind_label(f.entry_kind)
    category = _kind_category(f.kind)

    # Outcome suffix appended to the Summary so the operator sees the
    # consolidation context (a sibling already declares it, etc) up
    # front. Empty for AS_IS.
    if cf.outcome == OUTCOME_SIBLING_DECLARES:
        sibs = _format_sibling_list(cf.sibling_apps)
        outcome_note = (
            f" Effective permissions stay the same — sibling app(s) "
            f"{sibs} already declare it."
        )
    elif cf.outcome == OUTCOME_MOVE_TO_SIBLING:
        sibs = _format_sibling_list(cf.sibling_apps)
        outcome_note = (
            f" Sibling app(s) {sibs} use this resource without declaring "
            f"it — moving the declaration there makes the dependency "
            f"explicit on the app that needs it."
        )
    elif cf.outcome == OUTCOME_SIBLING_ALREADY_HAS:
        sibs = _format_sibling_list(cf.sibling_apps)
        outcome_note = (
            f" Sibling app(s) {sibs} already declare this resource; "
            f"this proposal makes the dependency explicit on "
            f"{f.app_name!r} as well."
        )
    else:
        outcome_note = ""

    # Summaries diverge by category — the operator question is different.
    if category == "unused":
        summary = (
            f"App {f.app_name!r} declares a {label} ({f.entry_value!r}) "
            f"that nothing in its scripts actually uses. Removing the "
            f"declaration narrows what the app is allowed to do without "
            f"changing what it does.{outcome_note}"
        )
        explanation = (
            f"Application manifests on this pod declare the resources "
            f"each app needs — shell commands it can run, files it can "
            f"read or write, hosts it can talk to, environment "
            f"variables it can read. The engine grants only the "
            f"declared resources at runtime, so a tighter manifest "
            f"means a tighter blast radius if anything goes wrong.\n\n"
            f"What we found. {f.app_name!r} declares "
            f"{f.entry_value!r} as a {label}, but a scan of the app's "
            f"scripts shows nothing referencing it. Either the entry "
            f"is left over from an earlier version of the app, or the "
            f"scan missed something dynamic.\n\n"
            f"What to do. Paste the instruction below to "
            f"{f.app_name!r} (or the bot running it) and have it "
            f"confirm — the bot can grep its own scripts for the value "
            f"and tell you whether the declaration is genuinely "
            f"unused. If confirmed unused, removing the entry is a "
            f"one-line manifest edit on the Manifests page.\n\n"
            f"What could go wrong. Dynamic references (assembled "
            f"strings, environment-driven paths) can dodge the scan. "
            f"If the app uses the resource at runtime in a way the "
            f"scan can't see, removing the declaration breaks it. "
            f"The bot's own audit catches those — when in doubt, "
            f"check before editing."
        )
        instruction = (
            f"Audit your manifest entry "
            f"`permissions.{f.entry_kind}: {f.entry_value!r}` on app "
            f"{f.app_name!r}. Grep your scripts for any reference to "
            f"{f.entry_value!r} — including string concatenation, "
            f"f-strings, env-derived values. Report whether the entry "
            f"is genuinely unused. If yes, propose removing it. If no, "
            f"explain what's using it so the scan rule can be tuned."
        )

    elif category == "missing":
        summary = (
            f"App {f.app_name!r}'s scripts use a {label} "
            f"({f.entry_value!r}) but its manifest doesn't declare it. "
            f"At runtime the engine will refuse the access — adding "
            f"the declaration makes the dependency explicit and lets "
            f"the app work as intended.{outcome_note}"
        )
        explanation = (
            f"Manifests on this pod gate runtime access — an app can "
            f"only use resources its manifest declares. When a script "
            f"references something the manifest doesn't list, the "
            f"engine blocks the call at runtime and the app fails "
            f"hard.\n\n"
            f"What we found. {f.app_name!r}'s scripts reference "
            f"{f.entry_value!r} ({label}), but the manifest's "
            f"`permissions.{f.entry_kind}` block doesn't include it.\n\n"
            f"What to do. Paste the instruction below to the bot. The "
            f"bot can confirm the reference is intentional (not "
            f"copy-paste leftover) and propose the manifest edit. Once "
            f"the entry is added on the Manifests page, the next "
            f"runtime access succeeds.\n\n"
            f"What could go wrong. If the reference is leftover code "
            f"from an earlier integration, adding the manifest entry "
            f"widens the app's permissions for no real reason. Have "
            f"the bot check whether the reference is reachable before "
            f"approving — dead code shouldn't drive a manifest grant."
        )
        instruction = (
            f"Your manifest is missing a `permissions.{f.entry_kind}` "
            f"declaration for {f.entry_value!r} ({label}), but your "
            f"scripts reference it. Confirm whether this reference is "
            f"live code (not leftover from a removed integration). "
            f"If live, propose the manifest edit to add "
            f"{f.entry_value!r}. If dead, propose removing the "
            f"reference from your scripts instead."
        )

    else:  # overkill (wildcard too broad)
        summary = (
            f"App {f.app_name!r}'s {label} declaration "
            f"({f.entry_value!r}) is broader than its scripts actually "
            f"need. Narrowing the wildcard to the specific patterns "
            f"the app uses tightens the permission without changing "
            f"functionality.{outcome_note}"
        )
        explanation = (
            f"Wildcards in manifest entries are convenient but blunt — "
            f"an `exec: *` line lets the app run any shell command, "
            f"not just the few it actually uses. Tightening the "
            f"wildcard to the specific values the app needs shrinks "
            f"the blast radius if something goes wrong.\n\n"
            f"What we found. {f.app_name!r}'s manifest declares "
            f"{f.entry_value!r} as a {label} wildcard, but the scripts "
            f"only reference a small concrete set.\n\n"
            f"What to do. Paste the instruction below to the bot. It "
            f"can list the specific values it actually uses and "
            f"propose a tighter manifest. Once approved on the "
            f"Manifests page, the wildcard becomes an enumerated list.\n\n"
            f"What could go wrong. Static analysis sees the scripts "
            f"as written; runtime can introduce values the scan "
            f"missed — operator-passed parameters, env-driven paths, "
            f"dynamic hosts. Have the bot list what it actually uses, "
            f"not just what the scan found. When in doubt, leave the "
            f"wildcard."
        )
        instruction = (
            f"Your `permissions.{f.entry_kind}: {f.entry_value!r}` "
            f"declaration is wildcard-shaped but your scripts reference "
            f"only a few concrete values. List the specific {label}s "
            f"you actually use at runtime — include any dynamic values "
            f"the static scan would miss (env-driven, "
            f"operator-supplied). Propose a tighter manifest with the "
            f"explicit list. Default to leaving the wildcard if the "
            f"runtime set is genuinely open."
        )

    manual_path = f"Manifests → {f.bot_id} → {f.app_name}"
    return {
        "summary": summary,
        "explanation": explanation,
        "action_label": None,  # Investigation default button
        "manual_path": manual_path,
        "manual_instruction": instruction,
    }


_HUMAN_TITLE_BY_CATEGORY = {
    "unused": "unused {label} declarations",
    "missing": "undeclared {label}s referenced by scripts",
    "overkill": "overly broad {label} declarations",
}


def _coalesce_key_for(cf: ConsolidatedFinding) -> str:
    """Stable per-root-cause key.

    All findings of one category (unused / missing / overkill) on one
    (bot, app, entry_kind) share a parent. The 9-host
    ``app-evolve-framework`` example collapses to one parent for the
    "missing network_egress" cluster instead of 9 separate cards.
    """
    f = cf.finding
    return (
        f"{GENERATOR_ID}:{f.bot_id}:{f.app_id}:"
        f"{f.entry_kind}:{_kind_category(f.kind)}"
    )


def _human_title_for(cf: ConsolidatedFinding) -> str:
    f = cf.finding
    label = _entry_kind_label(f.entry_kind)
    template = _HUMAN_TITLE_BY_CATEGORY.get(_kind_category(f.kind))
    if not template:
        return f"{f.app_name}: {f.entry_kind} review"
    return f"{f.app_name}: " + template.format(label=label)


def build_proposal(cf: ConsolidatedFinding) -> Proposal:
    """Produce one Investigation Proposal from a consolidated finding."""
    f = cf.finding
    context = _format_context(cf)
    content = _phase_c_content_for(cf)

    proposal_id = new_proposal_id()
    return Proposal(
        id=proposal_id,
        bot_id=f.bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[_trigger_observation(cf)],
        provenance=Provenance(
            technique=f"app_permission_review.{f.kind}",
            signals={
                "bot_id": f.bot_id,
                "app_id": f.app_id,
                "app_name": f.app_name,
                "entry_kind": f.entry_kind,
                "entry_value": f.entry_value,
                "severity": f.severity,
                "outcome": cf.outcome,
                "sibling_apps": list(cf.sibling_apps),
                "rationale": f.rationale,
                "meta": dict(f.meta),
            },
            confidence=_confidence_for(f.kind),
        ),
        problem=context,
        action=Investigation(context=context),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",  # Investigation has no state change
            touches=["manifest"],
        ),
        claim=None,
        approval_audience="pod_operator",
        urgency="hygiene" if f.severity != "warn" else "improvement",
        admin_surface_summary=_admin_summary(cf)[:120],
        motivating_signals=[],  # B.2 doesn't consume Signals
        # ── Phase C-3 operator-first content (Tier 5 — paste-to-bot) ────
        summary=content["summary"],
        explanation=content["explanation"],
        action_label=content["action_label"],
        manual_path=content["manual_path"],
        manual_instruction=content["manual_instruction"],
        dismiss_signature=dismiss_signature_for_finding(
            kind=f.kind,
            app_id=f.app_id,
            entry_kind=f.entry_kind,
            entry_value=f.entry_value,
        ),
        dismiss_scope="kind",
        coalesce_key=_coalesce_key_for(cf),
        human_title=_human_title_for(cf),
    )


def _confidence_for(kind: str) -> float:
    """Map finding kind → provenance confidence.

    Necessity / missing checks are higher-confidence (grep is decisive).
    Overkill checks are lower (operator may know about runtime hosts
    the static analysis missed).
    """
    if kind in ALL_UNUSED_KINDS or kind in ALL_MISSING_KINDS:
        return 0.85
    if kind in ALL_OVERKILL_KINDS:
        return 0.6
    return 0.75
