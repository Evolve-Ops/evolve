"""pod_apps — the pod-first Apps surface: one row per ``app_id``, across bots.

AL-1.8a (internal/build-AL-1.8a-apps-shell.md §2; IA decision in
internal/design-apps-surface-2026-08-16.md §3/§4). The Apps page was bot-first
and instance-first: to see what "Morning Brief" does across the pod you
switched bot tabs, and every row was one manifest. This module is the read
model the new surface renders — the **app** is the row, the **bots** are a
column, and a bot that does not have the app simply is not in that column.

WHAT IT IS. A pure-ish assembler over readers that already exist:

  * ``app_identity.resolve_app_id`` — the grouping key. Two manifests on two
    bots that resolve to the same id are ONE app with two installs. Resolved
    against the RAW manifest, before hydration, because hydration rewrites
    ``id``/``pkg_id`` on a v7-arc Instance (see ``app_identity`` docstring)
    and the rewritten answer is a different string.
  * ``app_spec.spec_from_manifest`` — AL-1.5a's migrate-on-read. Every
    artifact shape on the pod (v28/v30 legacy, v7-arc Spec, v7-arc Instance,
    already-v-next) derives an ``AppSpec``, so ``name`` / ``purpose`` /
    ``kind`` / ``audience`` / ``spec_version`` / ``provenance`` come from ONE
    reader instead of five per-shape branches in a route.
  * ``app_spec_store.load_spec`` — when a v-next Spec exists on disk it is the
    app's portable intent and wins over anything derived from an Instance.
  * ``usage_by_app.load_usage_by_app`` — AL-1.3's per-app rollup, joined on
    the same raw ``app_id``.

WHAT IT IS NOT. It computes no verdict. There is deliberately no
Active/Quiet/Inactive collapse (removed 2026-06-13), no delivery tri-state
(AL-2.1) and no drift column (B2). Where it has no data it emits ``None``
and the surface renders "can't measure" — ``None`` is not zero, and a
placeholder is not a finding.

AL-1.8b turned two of those placeholders into displays rather than into
computations: ``readiness`` shows AL-1.6b's scorer
(``app_readiness.readiness_payload``, pure) and ``offer`` shows AL-1.7's
promotion Proposal (``app_offer_state``, read-only). Neither number is
minted here, and both stay nullable — a scorer that cannot be imported is
"not yet scored" and a proposal store that cannot be read is ``unknown``,
never "nobody asked".

THE HONESTY CONTRACT, spelled out because it is the whole point of the
surface:

  * ``cost_7d`` / ``turns_7d`` are ``None`` when the rollup has no entry for
    this (app, bot). Not 0.0. A bot whose rollup file has not been written
    yet carries ``usage_measured: False`` and every metric ``None``.
  * The pod-level ``cost_7d`` sums only the bots that ARE measured, and
    ``usage_measured_bots`` / ``bots_total`` say how many of each — so a
    small number next to "2 of 5 bots measured" reads as partial coverage
    rather than as a low total.
  * ``last_run`` is a tri-state envelope ``{state, ts}`` where state is
    ``seen`` (AL-1.3 recorded a last-seen timestamp) or ``cant_measure``.
    The third state ("didn't run") needs the delivery ledger, which is
    AL-2.1's column — this module never guesses it.
  * ``inferred`` usage rides in its own key and is never folded into the
    deterministic total (design-app-attribution §3).

READER INJECTION. ``read_manifests`` is a callable ``bot_id -> [(stem, raw
dict)]`` rather than a hard-wired glob, for two reasons: the live route hands
in the privileged reader that already carries the ``sudo /bin/cat`` fallback
for a not-yet-ACLed bot (server.``_list_manifests_as_bot``), and a test hands
in a dict — so the assembly logic is testable without a pod. Same for
``load_usage`` and ``load_spec``.

NO WRITES. Every entry point here reads. AL-1.8a adds no write route (brief
§2); the actions the surface offers are the existing promote / demote /
share / archive endpoints, unchanged.

ONE APP ON N BOTS — THE PRESENTATION CLAIM (ALPHA-3a, audit B3, operator
decision D-I). ``resolve_app_id`` is a faithful grouping key and that is
exactly the problem: two bots that independently discover the same app mint
two ids, so the pod-first page rendered "Morning Brief · on 1 bot" twice.
``app_grouping`` supplies a reversible CLAIM that two ids describe one app
(normalized name + evidence overlap + disjoint bots), applied HERE, at
presentation, and nowhere else. Stored identity is untouched — every install
keeps its own ``app_id``, the usage join still runs on that id, and
``grouped=False`` withdraws the claim entirely — every id renders as its own
row again. The GROUPING is what is reversible, not every byte of the payload:
audit P4 landed here too, and its subtraction of the ``evolve`` service
account from "not on" lists applies regardless of ``grouped``. Birth-time
content-derived identity was explicitly deferred by D-I; promotion-time
cross-bot ADOPT is ALPHA-3b.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .app_identity import resolve_app_id
from .app_spec import AppSpec, spec_from_manifest

if TYPE_CHECKING:  # import-cost only; the runtime import is inside build_discovered
    from . import scan_provenance

log = logging.getLogger(__name__)

__all__ = [
    "DEFINED",
    "DISCOVERED",
    "SERVICE_ACCOUNT_BOTS",
    "ManifestReader",
    "build_pod_apps",
    "defined_app_facts",
    "build_app_detail",
    "build_discovered",
    "build_discovered_detail",
    "build_activity",
    "evidence_detail",
    "evidence_kinds",
    "definition_status_of",
]

# The two ``definition_status`` values, repeated here rather than imported
# from ``manifest`` so this module stays cheap to import from a route.
# ``manifest.py`` pulls in the whole schema layer; pinned by a test.
DEFINED = "defined"
DISCOVERED = "discovered"

#: ``bot_id -> [(manifest_stem, raw_manifest_dict), ...]``
ManifestReader = Callable[[str], "list[tuple[str, dict]]"]

#: ``bot_id -> ScanProvenance`` — what that bot's last app scan actually did.
#: Quoted so this module keeps its cheap-to-import property (the annotation is
#: never evaluated; ``scan_provenance`` is imported inside the one function
#: that needs it).
ScanProvenanceReader = Callable[[str], "scan_provenance.ScanProvenance"]

#: How many activity entries any one source may contribute. The feed is a
#: bounded read, not a log viewer — an unbounded union over every bot's
#: manifests would grow with the pod.
ACTIVITY_PER_SOURCE_CAP = 100

#: The pod's own service accounts. They CAN hold a manifest — which is why
#: the route's bot list appends ``evolve`` — but they are never somewhere an
#: operator installs an app, so listing them among "the bots that don't have
#: this app" beside an *Install to…* offers a target that does not exist
#: (audit finding P4). Excluded from ``bots_without`` only: an app that
#: genuinely lives on the service account still shows up in its own row's
#: bots column, because hiding a real install would be the opposite failure.
SERVICE_ACCOUNT_BOTS = frozenset({"evolve"})


# ── Small helpers ────────────────────────────────────────────────────────────

def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def definition_status_of(raw: dict) -> str:
    """``defined`` or ``discovered`` for one raw manifest.

    Absent reads as ``discovered`` — the inert default and the value the v27
    fleet migration landed every pre-existing manifest at. Mirrors the same
    normalization the Apps tile has done since Bite 4.
    """
    value = _s(raw.get("definition_status")).lower()
    return DEFINED if value == DEFINED else DISCOVERED


def evidence_kinds(raw: dict) -> list[str]:
    """Which kinds of evidence this manifest actually carries.

    Derived from what EXISTS on the manifest, never from a stored label:

      ``files``         — registered/evidence files
      ``cron``          — cron entries, cron evidence, or scheduled actions
      ``memory``        — heartbeat evidence (the bot's standing-instruction /
                          memory surfaces, where a fileless app lives)
      ``conversation``  — a recurring ask with no structure behind it

    ``conversation`` was NOT derivable in 1.8a — the carrier had no producer.
    AL-1.6c built one (``scanner._stamp_conversation_evidence`` writes
    ``conversation_only`` / ``conversation_evidence``), so leaving it out now
    would report a draft that exists BECAUSE someone keeps asking for it as
    having no evidence at all. An empty list still means "nothing recorded".

    Since ALPHA-4 there is a SECOND producer —
    ``scanner._merge_conversation_evidence_into_manifest`` merges the block onto
    a manifest that already has files, deliberately WITHOUT setting
    ``conversation_only``. So a manifest can now legitimately report BOTH
    ``files`` and ``conversation``, and the standalone ``if`` below is what lets
    it: collapsing the two would hide half of why such a draft is
    offer-eligible.

    That ``if`` is NOT here because of ALPHA-4, and an earlier draft of this
    docstring said it was (independent review of PR #3809). It landed in #3755
    on 2026-08-22, three days before ALPHA-4 existed, and ``cron`` / ``memory``
    are standalone ``if``s for the same ordinary reason — the kinds are
    independent. The function's only ``elif`` is a fallback between two carriers
    of the SAME kind. ALPHA-4 is what made the both-kinds case reachable in
    fact; it is not what shaped the code.
    """
    kinds: list[str] = []
    if raw.get("evidence_files") or raw.get("files") or raw.get("realized_files"):
        kinds.append("files")
    elif isinstance(raw.get("evidence"), dict) and raw["evidence"].get("files"):
        kinds.append("files")
    if raw.get("crons") or raw.get("cron_evidence") or raw.get("scheduled_actions"):
        kinds.append("cron")
    if raw.get("heartbeat_evidence"):
        kinds.append("memory")
    if raw.get("conversation_only") or raw.get("conversation_evidence"):
        kinds.append("conversation")
    return kinds


def _hydrated(raw: dict, shared_dir: Path) -> dict:
    """Display-shaped manifest.

    A v7-arc Instance carries no name/description — those live on the bound
    Spec — so the display fields come from the hydrated copy. The IDENTITY
    does not: callers resolve ``app_id`` from the raw dict BEFORE calling
    this, because ``hydrate_v7_arc_instance`` rewrites ``id`` and ``pkg_id``
    (``app_identity`` docstring; the same ordering the Apps tile uses).
    """
    if raw.get("manifest_shape") != "v7-arc":
        return raw
    try:
        from .manifest import hydrate_v7_arc_instance
        return hydrate_v7_arc_instance(raw, shared_dir)
    except Exception:
        # Hydration is a nicety; a failure costs a name, not a row.
        return raw


def _config_summary(raw: dict) -> str:
    """One operator-readable line describing how this bot runs the app.

    Counts of the things that make an install differ between bots —
    schedules, files, exclusive tools. Plain language, no field names
    (design §7: ``definition_status`` is never shown, and neither is
    ``scheduled_actions``).
    """
    parts: list[str] = []
    schedules = len(raw.get("scheduled_actions") or []) + len(raw.get("crons") or [])
    if schedules:
        parts.append(f"{schedules} schedule{'s' if schedules != 1 else ''}")
    files = len(raw.get("files") or raw.get("realized_files") or [])
    if files:
        parts.append(f"{files} file{'s' if files != 1 else ''}")
    if not parts:
        return "no schedules or files recorded"
    return " · ".join(parts)


def _grade_breakdown(window_entry: dict) -> dict[str, Any]:
    """Per-grade turns/cost, carried through exactly as the rollup wrote them.

    ``total`` is scheduled + explicit; ``inferred`` stays its own key. This
    function never sums the two — the grades are shown, not collapsed
    (brief §6).
    """
    out: dict[str, Any] = {}
    for grade in ("scheduled", "explicit", "inferred"):
        entry = _d(window_entry.get(grade))
        if entry:
            out[grade] = {
                "turns": entry.get("turns"),
                "cost_estimated": entry.get("cost_estimated"),
            }
    return out


# ── Per-bot fact row ─────────────────────────────────────────────────────────

def _bot_facts(
    bot_id: str,
    stem: str,
    raw: dict,
    display: dict,
    spec: AppSpec,
    usage: dict | None,
    usage_measured: bool,
) -> dict[str, Any]:
    """The bots × facts row for one (app, bot) pair — design §4's table.

    ``usage`` is this app's entry in the bot's rollup, or ``None`` when the
    rollup has no row for it. ``usage_measured`` says whether the bot has a
    rollup AT ALL, which is the difference between "this app served no turns"
    and "we have not measured this bot yet" — two states the surface must
    keep apart.
    """
    window = _d(usage)
    total = _d(window.get("total"))
    last_seen = _s(window.get("last_seen_ts")) or None
    return {
        "bot_id": bot_id,
        "manifest_stem": stem,
        "spec_version": spec.spec_version,
        "status": _s(display.get("status")) or "active",
        "app_kind": _s(display.get("app_kind")) or "application",
        "config_summary": _config_summary(raw),
        # AL-1.3 usage. None (never 0) when unmeasured — see the module
        # docstring's honesty contract.
        "usage_measured": bool(usage_measured),
        "turns_7d": total.get("turns") if usage else None,
        "cost_7d": total.get("cost_estimated") if usage else None,
        "grade_breakdown": _grade_breakdown(window) if usage else {},
        # Tri-state, honest: ``seen`` when AL-1.3 recorded a last-seen
        # timestamp, ``cant_measure`` otherwise. "didn't run" is AL-2.1's
        # answer and is never inferred here.
        "last_run": (
            {"state": "seen", "ts": last_seen}
            if last_seen
            else {"state": "cant_measure", "ts": None}
        ),
    }


# ── Pod-level assembly ───────────────────────────────────────────────────────

def _collect(
    bots: Iterable[str],
    *,
    read_manifests: ManifestReader,
    shared_dir: Path,
) -> "dict[str, list[tuple[str, str, dict, dict, AppSpec]]]":
    """Group every readable manifest by ``app_id``.

    Returns ``{app_id: [(bot_id, stem, raw, display, spec), ...]}``. A
    manifest that resolves to no id at all is dropped — it has no identity to
    group by, and inventing one here would mint an app that exists nowhere
    else on the pod.
    """
    grouped: "dict[str, list[tuple[str, str, dict, dict, AppSpec]]]" = {}
    for bot_id in bots:
        try:
            entries = read_manifests(bot_id)
        except Exception as exc:
            log.warning("pod_apps: manifest read failed for %s: %s", bot_id, exc)
            continue
        for stem, raw in entries:
            if not isinstance(raw, dict):
                continue
            app_id = resolve_app_id(raw)
            if not app_id:
                continue
            display = _hydrated(raw, shared_dir)
            try:
                spec = spec_from_manifest(display)
            except Exception as exc:
                log.warning("pod_apps: spec derive failed for %s/%s: %s",
                            bot_id, stem, exc)
                continue
            # The raw answer is the identity (hydration rewrote id/pkg_id on
            # a v7-arc Instance), and it is also the key AL-1.3's rollup is
            # written under — so grouping and the usage join agree by
            # construction.
            spec.app_id = app_id
            grouped.setdefault(app_id, []).append((bot_id, stem, raw, display, spec))
    return grouped


def _app_row(
    app_id: str,
    installs: "list[tuple[str, str, dict, dict, AppSpec]]",
    *,
    usage_by_bot: dict[str, dict],
    measured_bots: set[str],
    vnext: AppSpec | None,
) -> dict[str, Any]:
    """One pod-level Apps row from every install of one app."""
    # Portable intent: the v-next Spec when one is on disk, else the install
    # with the highest spec_version — the closest thing to "what this app is"
    # that a pod without written Specs has.
    lead = vnext or max((s for _, _, _, _, s in installs), key=lambda s: s.spec_version)

    bots: list[dict[str, Any]] = []
    for bot_id, stem, raw, display, spec in sorted(installs, key=lambda i: i[0]):
        bots.append(_bot_facts(
            bot_id, stem, raw, display, spec,
            # The join runs on the INSTALL's own id, not on the row's. They
            # are the same string for an ungrouped row; for a grouped one
            # (ALPHA-3a) they are not, and joining on the row's id would
            # find no rollup entry for every member but the lead — turning a
            # measured cost into "measured, no turns", which is a lie the
            # honesty contract exists to prevent.
            #
            # Resolved from RAW here, not from ``spec``. ``_collect`` does
            # assign the raw answer onto ``spec.app_id``, so the two agree
            # today — but that is action at a distance, and the id a v7-arc
            # Instance's own ``AppSpec`` would otherwise carry is the
            # HYDRATED one (hydration rewrites ``id``/``pkg_id``), which the
            # rollup is never keyed by. Reading ``raw`` makes the join key
            # correct at the call site instead of thirty lines away. Pinned
            # by the v7-arc tests in test_apps_pod_grouping.py.
            usage=_d(usage_by_bot.get(bot_id)).get(resolve_app_id(raw) or app_id),
            usage_measured=bot_id in measured_bots,
        ))

    measured = [b for b in bots if b["cost_7d"] is not None]
    seen = [b["last_run"]["ts"] for b in bots if b["last_run"]["state"] == "seen"]
    statuses = {b["status"] for b in bots}

    return {
        "app_id": app_id,
        "name": lead.name or app_id,
        "purpose": lead.purpose,
        "kind": lead.kind,
        "audience": lead.audience,
        "spec_version": lead.spec_version,
        "spec_source": "vnext" if vnext else "derived",
        "provenance": dict(lead.provenance),
        "defined_since": _s(dict(lead.provenance).get("at")) or None,
        "app_kind": bots[0]["app_kind"] if bots else "application",
        # One status for the row: "active" unless every install agrees on
        # something else. A mixed pod says "mixed" rather than picking a
        # winner the operator cannot see.
        "status": statuses.pop() if len(statuses) == 1 else "mixed",
        "bots": bots,
        "bots_total": len(bots),
        "usage_measured_bots": len(measured),
        # Sums cover the measured bots only; None when nothing is measured.
        "cost_7d": (round(sum(b["cost_7d"] or 0.0 for b in measured), 4)
                    if measured else None),
        "turns_7d": (sum(b["turns_7d"] or 0 for b in measured)
                     if measured else None),
        "last_run": ({"state": "seen", "ts": max(seen)}
                     if seen else {"state": "cant_measure", "ts": None}),
    }


# ── The one-app-on-N-bots claim (ALPHA-3a / D-I) ─────────────────────────────

def _grouping_facts(
    by_id: "dict[str, list[tuple[str, str, dict, dict, AppSpec]]]",
) -> "dict[str, dict]":
    """The four inputs ``app_grouping.cluster_app_ids`` needs, per app id.

    Name comes from the derived Spec (the same one the row's title comes
    from), evidence from the union of every install's recorded files, bots
    from the installs themselves, spec_version for the lead tie-break.
    """
    from .app_grouping import evidence_signature

    facts: "dict[str, dict]" = {}
    for app_id, installs in by_id.items():
        paths: list[str] = []
        names: list[str] = []
        versions: list[int] = []
        for _bot, _stem, raw, display, spec in installs:
            paths.extend(_evidence_file_paths(raw))
            names.append(spec.name or _s(display.get("name")))
            versions.append(int(spec.spec_version or 0))
        facts[app_id] = {
            "name": next((n for n in names if n), ""),
            "evidence": evidence_signature(paths),
            "bots": {bot for bot, _, _, _, _ in installs},
            "spec_version": max(versions) if versions else 0,
        }
    return facts


def defined_app_facts(
    bots: Iterable[str],
    *,
    read_manifests: ManifestReader,
    shared_dir: Path,
) -> "dict[str, dict]":
    """The grouping facts for the pod's **defined** apps, per ``app_id``.

    ALPHA-3b (D-I half 2). Promotion asks "does this pod already have this
    app, defined, on another bot?" and this is what it asks against. It is
    the SAME assembly the page groups with — ``_collect`` then
    ``_grouping_facts`` — filtered to the defined half, so the id promotion
    adopts is the id the Apps page already leads that app's row with.

    DEFINED ONLY, and that is a rule, not a filter for convenience: a draft
    adopting from another draft would spread an identity that nobody has
    vouched for yet, which is the opposite of D-I's "identity converges at
    the moment a human vouches". An app counts as defined when at least one
    of its installs is — the same test ``build_pod_apps`` uses for the row.

    Reads: one pass over every listed bot's manifests through the injected
    reader. The caller decides when that is worth paying for; the promotion
    sweep pays it only once it is actually about to mint an offer.
    """
    by_id = _collect(bots, read_manifests=read_manifests, shared_dir=shared_dir)
    defined = {
        app_id: installs
        for app_id, installs in by_id.items()
        if any(definition_status_of(raw) == DEFINED
               for _bot, _stem, raw, _display, _spec in installs)
    }
    return _grouping_facts(defined)


def _grouped_views(
    by_id: "dict[str, list[tuple[str, str, dict, dict, AppSpec]]]",
    *,
    grouped: bool,
) -> "list[tuple[str, list[str], str | None, list]]":
    """``[(lead_app_id, member_app_ids, basis, merged_installs), ...]``.

    With ``grouped=False`` every app id is its own view and ``basis`` is
    ``None`` — the pre-ALPHA-3a behaviour, reached by the same code path so
    there is no second assembly to drift.
    """
    if not grouped:
        return [(app_id, [app_id], None, list(installs))
                for app_id, installs in sorted(by_id.items())]

    from .app_grouping import cluster_app_ids

    out: "list[tuple[str, list[str], str | None, list]]" = []
    for group in cluster_app_ids(_grouping_facts(by_id)):
        merged: list = []
        for member in group.members:
            merged.extend(by_id.get(member) or [])
        out.append((group.lead, list(group.members),
                    group.basis if group.grouped else None, merged))
    return out


def _resolve_vnext(
    member_ids: "Iterable[str]",
    shared_dir: Path,
    load_spec: "Callable[[Path, str], AppSpec | None] | None",
) -> "AppSpec | None":
    """The written Spec for a row, tried lead-first across its member ids.

    A grouped row stands for several ids and at most one of them is likely
    to have a Spec on disk (they were minted independently). First hit in
    member order wins; ``None`` when none of them has one, which is what
    makes the row say "read from the bots" rather than claim a spec.
    """
    if load_spec is None:
        return None
    for app_id in member_ids:
        try:
            found = load_spec(shared_dir, app_id)
        except Exception:
            found = None
        if found is not None:
            return found
    return None


def _stamp_group(row: dict[str, Any], members: "list[str]",
                 basis: "str | None") -> dict[str, Any]:
    """Attach the claim's provenance to a row.

    ``grouped`` is the only field the surface branches on. ``group_basis``
    is carried for auditability and is NEVER rendered — the page says "these
    look like the same app", in words (design §7's Plex test).
    """
    row["grouped"] = len(members) > 1
    row["grouped_app_ids"] = list(members)
    row["group_basis"] = basis if len(members) > 1 else None
    return row


def build_pod_apps(
    bots: Iterable[str],
    *,
    read_manifests: ManifestReader,
    shared_dir: Path,
    load_usage: Callable[[Path, str], dict] | None = None,
    load_spec: Callable[[Path, str], "AppSpec | None"] | None = None,
    grouped: bool = True,
) -> dict[str, Any]:
    """The pod's **defined** apps, one row per app (brief §2).

    Discovered drafts are excluded here and listed by ``build_discovered`` —
    they are the other lifecycle state, not a filter on this one. An app
    counts as defined when at least one of its installs is defined; the
    per-bot rows still carry every install, because "defined on team-bot-a,
    discovered on team-bot-c" is a real and interesting state and hiding the
    second half would misreport where the app lives.

    ``grouped`` applies ALPHA-3a's claim (``app_grouping``): app ids that
    look like the same app on different bots render as ONE row carrying every
    install. It is on by default because two rows each saying "on 1 bot" for
    one app is the audit's B3 blocker, and it is a parameter because the
    claim must be withdrawable — ``grouped=False`` returns the ungrouped
    payload, which is what the surface's "Show separately" asks for. A
    grouped row additionally carries ``members``: the same rows it stands
    for, assembled independently, so a client can split without a re-fetch.
    """
    bot_list = list(bots)
    by_id = _collect(bot_list, read_manifests=read_manifests, shared_dir=shared_dir)

    usage_by_bot, measured_bots = _load_usage_maps(bot_list, shared_dir, load_usage)

    def _row(app_id: str, installs: list, member_ids: "list[str]") -> dict[str, Any]:
        return _app_row(
            app_id, installs,
            usage_by_bot=usage_by_bot,
            measured_bots=measured_bots,
            vnext=_resolve_vnext(member_ids, shared_dir, load_spec),
        )

    rows: list[dict[str, Any]] = []
    for lead, members, basis, installs in _grouped_views(by_id, grouped=grouped):
        if not any(definition_status_of(raw) == DEFINED for _, _, raw, _, _ in installs):
            continue
        row = _stamp_group(_row(lead, installs, members), members, basis)
        if row["grouped"]:
            # The split view, precomputed. Each member is exactly the row it
            # would have been on its own — same assembly, same helper — so
            # "Show separately" is a render, not a different answer.
            row["members"] = [
                _stamp_group(_row(m, by_id.get(m) or [], [m]), [m], None)
                for m in members
            ]
        rows.append(row)

    rows.sort(key=lambda r: (r["name"] or "").lower())
    return {
        "apps": rows,
        "bots": bot_list,
        "grouped": bool(grouped),
        "usage_measured_bots": sorted(measured_bots),
        # Bots with no rollup on disk. The surface labels their cells "not
        # measured yet" rather than showing a zero.
        "usage_unmeasured_bots": sorted(set(bot_list) - measured_bots),
    }


def _load_usage_maps(
    bots: "list[str]",
    shared_dir: Path,
    load_usage: Callable[[Path, str], dict] | None,
) -> "tuple[dict[str, dict], set[str]]":
    """``({bot: {app_id: d7 entry}}, {bots with a rollup on disk})``.

    A bot with a rollup file but no row for a given app is measured-with-no-
    turns; a bot with no rollup file at all is not measured. Both render
    differently, so both are tracked.
    """
    usage_by_bot: dict[str, dict] = {}
    measured: set[str] = set()
    if load_usage is None:
        return usage_by_bot, measured
    for bot_id in bots:
        try:
            payload = load_usage(shared_dir, bot_id)
        except Exception:
            payload = {}
        if not payload:
            continue
        measured.add(bot_id)
        entries: dict[str, Any] = {}
        for app_id, entry in _d(payload.get("apps")).items():
            window = dict(_d(entry).get("d7") or {})
            window["last_seen_ts"] = _d(entry).get("last_seen_ts")
            entries[app_id] = window
        usage_by_bot[bot_id] = entries
    return usage_by_bot, measured


def build_app_detail(
    app_id: str,
    bots: Iterable[str],
    *,
    read_manifests: ManifestReader,
    shared_dir: Path,
    load_usage: Callable[[Path, str], dict] | None = None,
    load_spec: Callable[[Path, str], "AppSpec | None"] | None = None,
    signals: "list[dict] | None" = None,
    workspace_for: "Callable[[str], Path | None] | None" = None,
    pack_context_for: "Callable[[str, dict], dict | None] | None" = None,
    grouped: bool = True,
) -> dict[str, Any] | None:
    """One app's detail payload — the row plus per-bot config and signals.

    Returns ``None`` when no manifest on the pod resolves to ``app_id`` (the
    route answers 404). Unlike ``build_pod_apps`` this does NOT require the
    app to be defined: an operator following a link to a draft should land on
    its detail rather than on a dead end.

    ``workspace_for`` / ``pack_context_for`` are AL-1.8b's Files panel
    (design §4a, D-U6). Both are injected for the same reason
    ``read_manifests`` is: resolving a bot's workspace needs the privileged
    reader the route already owns, and a test hands in a dict. Omit
    ``workspace_for`` and every per-bot file cell reads ``can't measure``,
    which is the true statement for a caller that cannot reach the bots.

    ``grouped`` applies the SAME claim the list does (ALPHA-3a). It must:
    B3's actual evidence was the detail page saying *"Not on team-bot-a"*
    beside a bot that visibly had the app, so a list that grouped and a
    detail that did not would move the blocker one click deeper rather than
    fix it. The row keeps the ``app_id`` that was ASKED for — every member id
    remains a working link — and ``grouped_app_ids`` says which others it now
    speaks for.
    """
    bot_list = list(bots)
    by_id = _collect(bot_list, read_manifests=read_manifests, shared_dir=shared_dir)
    if app_id not in by_id:
        return None
    # The requested id keys this payload, not the group's lead — see the
    # docstring — so the view's first element is deliberately unused.
    _group_lead, members, basis, installs = next(
        (view for view in _grouped_views(by_id, grouped=grouped)
         if app_id in view[1]),
        (app_id, [app_id], None, by_id[app_id]),
    )

    usage_by_bot, measured_bots = _load_usage_maps(bot_list, shared_dir, load_usage)
    vnext = _resolve_vnext(members, shared_dir, load_spec)

    row = _app_row(
        app_id, installs,
        usage_by_bot=usage_by_bot, measured_bots=measured_bots, vnext=vnext,
    )
    _stamp_group(row, members, basis)
    row["definition_states"] = {
        bot_id: definition_status_of(raw) for bot_id, _, raw, _, _ in installs
    }
    # Bots that do NOT have this app — the "Install to…" column's domain.
    # Disabled in 1.8a (deterministic install is AL-1.5b), but the surface
    # still has to be able to say where the app is absent. The pod's own
    # service account is not a place an operator installs an app, so it is
    # never in this list (audit P4) — see SERVICE_ACCOUNT_BOTS.
    row["bots_without"] = sorted(
        set(bot_list)
        - {b["bot_id"] for b in row["bots"]}
        - SERVICE_ACCOUNT_BOTS
    )
    row["signals"] = list(signals or [])

    # ── AL-1.8b: the two panels the 1.8a shell lost (design §4a) ────────────
    # Files belong to the APP and bots are realization columns; ``requires``
    # is the app's declared surface. Both are read models over data that
    # already exists — no new field, no write.
    lead = vnext or max((s for _, _, _, _, s in installs), key=lambda s: s.spec_version)
    app_pack = _app_pack_dir(shared_dir, app_id)
    try:
        from .app_files_view import build_package_view, build_uses_view
        row["package"] = build_package_view(
            installs,
            vnext=vnext,
            workspace_for=workspace_for or (lambda _bot: None),
            pack_context_for=pack_context_for,
            app_pack_dir=app_pack,
        )
        uses = build_uses_view(lead)
    except Exception as exc:  # noqa: BLE001
        # A failure here costs the two panels, not the whole detail view —
        # and it says so rather than rendering an empty file list, which
        # would read as "this app has no files".
        log.warning("pod_apps: file/uses view failed for %s: %s", app_id, exc)
        row["package"] = None
        uses = {"requires": None, "exclusive_tools": [], "declared": 0}
    row["requires"] = uses["requires"]
    row["exclusive_tools"] = uses["exclusive_tools"]
    row["requires_declared"] = uses["declared"]

    # ── AL-3.2: can this app be put on a bot that does not have it? ─────────
    # A READ, like everything else here: it loads the app's pack and verifies
    # it, and answers with one of three states so the surface can enable the
    # button, offer it with a snapshot first, or turn it off with a reason.
    # A failure costs the affordance, not the page — and it says so rather
    # than defaulting to "installable", which would move the failure from
    # before the operator's click to after it.
    try:
        from .app_install import installability
        row["install"] = installability(app_id, installs, shared_dir)
    except Exception as exc:  # noqa: BLE001
        log.warning("pod_apps: installability unavailable for %s: %s", app_id, exc)
        row["install"] = {
            "state": "unavailable", "pack": False, "pack_dir": "",
            "pack_files": 0, "sources": [],
            "reason": "this pod could not work out whether the app can be "
                      "installed elsewhere",
        }
    return row


def _app_pack_dir(shared_dir: Path, app_id: str) -> "Path | None":
    """The app's own AL-3.1 files-pack directory, or None.

    A path derivation plus one existence check — ``app_snapshot.pack_dir``
    keyed on ``app_id``, exactly as the Spec is keyed, which is why no field on
    the artifact has to carry the location. Returned as a DIRECTORY rather than
    a boolean so ``app_files_view`` reads the pack itself and never re-derives
    where it lives.
    """
    try:
        from .app_snapshot import pack_dir
        candidate = pack_dir(Path(shared_dir), app_id)
    except Exception:  # noqa: BLE001 — a non-conferrable id is simply no pack
        return None
    return candidate if (candidate / "manifest.json").is_file() else None


def _readiness_of(raw: dict) -> "dict[str, Any] | None":
    """AL-1.6b's score for one draft, or None when the scorer is unreachable.

    DISPLAY ONLY (brief §3). ``app_readiness.readiness_payload`` is the one
    scorer on the pod and it is pure — no clock, no disk — so calling it here
    is showing AL-1.6's answer, not computing a second one. A pod whose
    analyzer package is absent gets ``None`` and the surface says "not yet
    scored", which stays true rather than becoming a fabricated zero.
    """
    try:
        from .app_readiness import readiness_payload
        return readiness_payload(raw)
    except Exception as exc:  # noqa: BLE001
        log.info("pod_apps: readiness unavailable (%s)", exc)
        return None


def build_discovered(
    bots: Iterable[str],
    *,
    read_manifests: ManifestReader,
    shared_dir: Path,
    read_provenance: ScanProvenanceReader | None = None,
    scan_bots: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Pod-wide drafts — manifests still at ``discovered`` (brief §2).

    Each row carries the name guess, the bot, the evidence kinds that
    actually exist, AL-1.6's readiness score and AL-1.7's offer state.

    AND WHAT THE SCAN DID (ALPHA-2, audit findings B2a / U5). ``scans`` is one
    :class:`~.scan_provenance.ScanProvenance` block per bot and ``scan_summary``
    is their rollup, so an empty draft list is no longer ambiguous: the surface
    can tell "nobody has scanned this pod yet" from "everything found here has
    been dealt with" from "the scan ran without a model, so of course it found
    nothing". Before this the payload carried no last-scan fact at all and the
    one empty state asserted the second of those three unconditionally.

    ``read_provenance`` is a seam so the assembly stays testable without a pod,
    and it is REQUIRED in the sense that omitting it does not fabricate a
    state — it produces ``scans: None`` / ``scan_summary: None``, which the
    surface renders as "no scan record was consulted" and never as "ok".

    ``scan_bots`` is the set discovery ACTUALLY covers (``network.bots`` — the
    list ``POST /api/applications/sync/pod`` iterates), which is not ``bots``:
    the latter appends the ``evolve`` service account because it *can* hold a
    manifest. Provenance is read for ``scan_bots`` only, because every sentence
    built from it ends in "run Sync" — and naming a bot Sync will never visit is
    a remediation that cannot work (docs/principle-alerts-explain-and-remediate.md).
    Defaults to ``bots`` so a caller with one list is not forced to invent two.

    Both of those were explicit ``None`` placeholders in 1.8a because neither
    chip had landed. They have (AL-1.6b's scorer, AL-1.7's promotion
    Proposal), so AL-1.8b displays them — design §5's row for each chip, and
    §6's "the offer-state column if AL-1.7 has not already landed it". They
    stay nullable: ``readiness: None`` still means "nothing scored this" and
    an ``offer.state`` of ``unknown`` still means "the pod could not check",
    which is not the same as "nobody has asked".

    A draft has no ``app_id`` by design (identity is conferred at promotion),
    so rows are keyed by ``(bot_id, manifest_stem)`` — the pair the existing
    promote/archive endpoints already take.
    """
    from .app_offer_state import build_offer_index, offer_state_for
    from .scan_provenance import summarize

    bot_list = list(bots)
    offers, offers_readable = build_offer_index(shared_dir)
    rows: list[dict[str, Any]] = []
    for bot_id in bot_list:
        try:
            entries = read_manifests(bot_id)
        except Exception as exc:
            log.warning("pod_apps: manifest read failed for %s: %s", bot_id, exc)
            continue
        for stem, raw in entries:
            if not isinstance(raw, dict):
                continue
            if definition_status_of(raw) != DISCOVERED:
                continue
            display = _hydrated(raw, shared_dir)
            try:
                spec = spec_from_manifest(display)
            except Exception:
                continue
            rows.append({
                "bot_id": bot_id,
                "manifest_stem": stem,
                "draft_id": _s(raw.get("draft_id")) or None,
                "name": spec.name or _s(display.get("name")) or stem,
                "purpose": spec.purpose,
                "evidence": evidence_kinds(raw),
                "app_kind": _s(display.get("app_kind")) or "application",
                "created_at": _s(display.get("created_at")) or None,
                "readiness": _readiness_of(raw),
                "offer": offer_state_for(
                    raw, bot_id=bot_id, manifest_stem=stem,
                    offers=offers, offers_readable=offers_readable,
                ),
            })
    rows.sort(key=lambda r: (r["bot_id"], (r["name"] or "").lower()))
    # Every bot on the pod, not just the ones with a draft: the Discovered
    # bot filter is the same control the Apps subtab has, and a picker whose
    # options changed with the data would make "no drafts on team-bot-c"
    # unaskable — the option would simply not be there.
    # Provenance for EVERY bot, including the ones with no draft: "has anything
    # ever scanned team-bot-c?" is precisely the question a list keyed by drafts
    # cannot answer, and it is the question U5's empty state got wrong.
    scans: list[dict[str, Any]] | None = None
    scan_summary: dict[str, Any] | None = None
    if read_provenance is not None:
        scan_list = bot_list if scan_bots is None else list(scan_bots)
        provs = []
        for bot_id in scan_list:
            try:
                provs.append(read_provenance(bot_id))
            except Exception as exc:  # noqa: BLE001
                # A reader that raised told us nothing — record that, rather
                # than dropping the bot and shrinking the denominator.
                log.warning("pod_apps: scan provenance failed for %s: %s", bot_id, exc)
                from .scan_provenance import READ_DENIED, classify
                provs.append(classify(bot_id, None, READ_DENIED))
        scans = [p.to_dict() for p in provs]
        scan_summary = summarize(provs)

    return {"drafts": rows, "count": len(rows), "bots": bot_list,
            "offers_readable": bool(offers_readable),
            "scans": scans, "scan_summary": scan_summary}


# ── The Discovered drawer (design §4a, D-U7) ─────────────────────────────────

#: How many evidence rows of one kind the drawer carries. The drawer is a
#: legible summary, not a file browser; the count travels with the list so
#: the surface says "showing 20 of 63" instead of quietly stopping at 20.
EVIDENCE_ROW_CAP = 20


def _evidence_file_paths(raw: dict) -> "list[str]":
    """Every file path this draft's evidence names, deduped, in order.

    The implementation moved to ``app_grouping.evidence_paths`` in ALPHA-3b,
    where it sits next to the claim it feeds: promotion now asks the same
    "is this the same app" question against the same file sets, and two
    extractors would have been two answers. This name stays because it is
    what this module's readers call.
    """
    from .app_grouping import evidence_paths

    return evidence_paths(raw)


def _evidence_schedules(raw: dict) -> "list[dict[str, str]]":
    """Schedule evidence as ``[{when, what, where}]``, plain-language keys.

    Unions the three carriers a draft can have — ``crons[]`` (raw crontab
    lines on v4, dicts from v5), ``scheduled_actions[]`` (the v16 shape, with
    the trigger block naming the file the claim came from), and
    ``cron_evidence.labels`` (launchd labels attributable to the app). An
    entry with no schedule string still appears: "something runs this, we
    just do not have the timing" is evidence, and dropping it would under-
    report why the draft exists.
    """
    out: list[dict[str, str]] = []
    for cron in (raw.get("crons") or []):
        if isinstance(cron, str):
            out.append({"when": cron.strip(), "what": "", "where": ""})
        elif isinstance(cron, dict):
            out.append({
                "when": _s(cron.get("schedule")),
                "what": _s(cron.get("script")) or _s(cron.get("label")),
                "where": "",
            })
    for action in (raw.get("scheduled_actions") or []):
        if not isinstance(action, dict):
            continue
        trigger = _d(action.get("trigger"))
        out.append({
            "when": _s(trigger.get("schedule")),
            "what": _s(action.get("summary")) or _s(action.get("id")),
            "where": _s(trigger.get("evidence_path")),
        })
    for label in (_d(raw.get("cron_evidence")).get("labels") or []):
        text = _s(label)
        if text:
            out.append({"when": "", "what": text, "where": ""})
    return out


def _evidence_memory(raw: dict) -> "dict[str, Any]":
    """Standing-instruction evidence: which file, and which sections of it."""
    heartbeat = _d(raw.get("heartbeat_evidence"))
    anchors = [_s(a) for a in (heartbeat.get("section_anchors") or []) if _s(a)]
    path = _s(heartbeat.get("file_path"))
    if not path and not anchors:
        return {}
    return {"path": path, "sections": anchors}


def _evidence_conversation(raw: dict) -> "dict[str, Any]":
    """The recurrence arithmetic AL-1.6c stamps, carried through verbatim.

    Only the fields the drawer shows, and only when the producer wrote them
    — an absent ``days_seen`` stays absent rather than becoming a 0 that
    reads as "asked on none of the days".
    """
    evidence = _d(raw.get("conversation_evidence"))
    if not evidence:
        return {}
    out: dict[str, Any] = {}
    for key in ("label", "days_seen", "window_days", "occurrences",
                "first_day", "last_day", "center_hour", "primary_requester"):
        if evidence.get(key) not in (None, ""):
            out[key] = evidence[key]
    requesters = [_s(r) for r in (evidence.get("requesters") or []) if _s(r)]
    if requesters:
        out["requesters"] = requesters
    return out


def evidence_detail(raw: dict) -> dict[str, Any]:
    """The concrete evidence behind one draft, made legible (design §4a).

    Every list is capped at ``EVIDENCE_ROW_CAP`` and reports its true total
    beside the cap, because a silently sliced list reads as "that is all
    there is" — the same rule ``build_activity``'s ``truncated`` follows.
    """
    files = _evidence_file_paths(raw)
    schedules = _evidence_schedules(raw)
    return {
        "kinds": evidence_kinds(raw),
        "conversation_only": bool(raw.get("conversation_only")),
        "files": files[:EVIDENCE_ROW_CAP],
        "files_total": len(files),
        "schedules": schedules[:EVIDENCE_ROW_CAP],
        "schedules_total": len(schedules),
        "memory": _evidence_memory(raw),
        "conversation": _evidence_conversation(raw),
    }


def build_discovered_detail(
    ref: str,
    bots: Iterable[str],
    *,
    read_manifests: ManifestReader,
    shared_dir: Path,
    bot_id: str = "",
) -> "dict[str, Any] | list[dict[str, str]] | None":
    """One draft, in full — the drawer payload (design §4a / D-U7).

    ``ref`` is the draft's ``draft_id`` when it has one and its manifest
    stem otherwise. **Both, because the pod has both:** design §3 gives a
    draft a ``draft_id``, and the live macOS pod measured ``with_draft_id=0``
    across 74 manifests on 2026-08-21 — a route that accepted only the
    designed key would open nothing on the pod it was built for.

    Returns the payload, or ``None`` when nothing matches, or — when a stem
    is ambiguous across bots and no ``bot_id`` was given — the list of
    candidates, so the caller can answer "which one?" instead of picking a
    bot for the operator.
    """
    from .app_offer_state import build_offer_index, offer_state_for

    matches: list[tuple[str, str, dict]] = []
    for bot in bots:
        if bot_id and bot != bot_id:
            continue
        try:
            entries = read_manifests(bot)
        except Exception as exc:
            log.warning("pod_apps: manifest read failed for %s: %s", bot, exc)
            continue
        for stem, raw in entries:
            if not isinstance(raw, dict):
                continue
            if definition_status_of(raw) != DISCOVERED:
                continue
            if ref in (_s(raw.get("draft_id")), stem):
                matches.append((bot, stem, raw))
    if not matches:
        return None
    if len(matches) > 1:
        return [{"bot_id": b, "manifest_stem": st} for b, st, _ in matches]

    bot, stem, raw = matches[0]
    display = _hydrated(raw, shared_dir)
    try:
        spec = spec_from_manifest(display)
    except Exception:
        spec = None
    offers, offers_readable = build_offer_index(shared_dir)
    description = (_s(display.get("description"))
                   or _s(display.get("conversational_summary"))
                   or _s(display.get("objective")))
    return {
        "bot_id": bot,
        "manifest_stem": stem,
        "draft_id": _s(raw.get("draft_id")) or None,
        "name": (spec.name if spec else "") or _s(display.get("name")) or stem,
        "purpose": (spec.purpose if spec else "") or None,
        "description": description or None,
        "app_kind": _s(display.get("app_kind")) or "application",
        "created_at": _s(display.get("created_at")) or None,
        "evidence": evidence_detail(raw),
        "readiness": _readiness_of(raw),
        "offer": offer_state_for(
            raw, bot_id=bot, manifest_stem=stem,
            offers=offers, offers_readable=offers_readable,
        ),
    }


# ── Activity ─────────────────────────────────────────────────────────────────

def _activity_from_jobs(jobs: Iterable[Any]) -> list[dict[str, Any]]:
    """Forge jobs → activity entries.

    A forge job is how an app is authored, installed from the gallery, or
    improved — so it is the same feed, discriminated by ``job_type`` rather
    than by which page it was started from. ``completed_with_errors`` is
    carried through as its own outcome: a job that shipped files but failed
    to materialize a schedule is not a clean success and must not read as one.
    """
    out: list[dict[str, Any]] = []
    for job in list(jobs)[:ACTIVITY_PER_SOURCE_CAP]:
        data = job if isinstance(job, dict) else getattr(job, "__dict__", {})
        status = _s(data.get("status"))
        if status == "complete" and data.get("completed_with_errors"):
            outcome = "completed with problems"
        else:
            outcome = status or "unknown"
        kind = {
            "install": "install",
            "improvement": "improvement",
            "update": "update",
            "hotfix": "hotfix",
        }.get(_s(data.get("job_type")), "authoring")
        out.append({
            "kind": kind,
            "ts": _s(data.get("last_updated")) or _s(data.get("created_at")),
            "app_id": _s(data.get("app_id")),
            "bot_id": _s(data.get("bot_id")),
            "outcome": outcome,
            "detail": _s(data.get("test_output_summary"))[:200],
            "job_id": _s(data.get("job_id")),
        })
    return out


def _activity_from_manifests(
    bots: Iterable[str],
    *,
    read_manifests: ManifestReader,
) -> list[dict[str, Any]]:
    """Promotions and demotions, read off the manifests that recorded them.

    ``promote_to_defined`` stamps ``provenance.last_promoted_at`` /
    ``last_promoted_by`` and ``demote_to_discovered`` its mirror, so the LAST
    such act per install is recoverable without a second log. Only the last
    one: this is a state stamp, not a history, and presenting it as a full
    trail would overstate what is on disk.
    """
    out: list[dict[str, Any]] = []
    for bot_id in bots:
        try:
            entries = read_manifests(bot_id)
        except Exception:
            continue
        for stem, raw in entries:
            if not isinstance(raw, dict):
                continue
            prov = _d(raw.get("provenance"))
            app_id = resolve_app_id(raw) or stem
            for field_name, kind in (
                ("last_promoted_at", "promoted"),
                ("last_demoted_at", "demoted"),
            ):
                ts = _s(prov.get(field_name))
                if not ts:
                    continue
                by = _s(prov.get(field_name.replace("_at", "_by")))
                out.append({
                    "kind": kind,
                    "ts": ts,
                    "app_id": app_id,
                    "bot_id": bot_id,
                    "outcome": kind,
                    "detail": f"by {by}" if by else "",
                    "manifest_stem": stem,
                })
    return out[:ACTIVITY_PER_SOURCE_CAP]


def _activity_from_gallery_local(shared_dir: Path) -> list[dict[str, Any]]:
    """Publishes — one entry per Spec version written into the local tier.

    ``share_routes`` writes ``gallery/local/<spec_id>/<version>.json`` on
    every publish, so the file IS the record. The timestamp comes from the
    Spec's own ``source.shared_at`` when it carries one, and falls back to
    the file mtime — a fallback the entry names, because a file mtime is
    when the bytes landed, not necessarily when the operator acted.
    """
    out: list[dict[str, Any]] = []
    local = Path(shared_dir) / "gallery" / "local"
    if not local.is_dir():
        return out
    try:
        spec_dirs = sorted(p for p in local.iterdir() if p.is_dir())
    except OSError:
        return out
    for spec_dir in spec_dirs:
        try:
            versions = sorted(spec_dir.glob("*.json"))
        except OSError:
            continue
        for path in versions:
            ts, exact = "", True
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
            source = _d(_d(data).get("source"))
            ts = _s(source.get("shared_at"))
            if not ts:
                exact = False
                try:
                    from datetime import datetime, timezone
                    ts = datetime.fromtimestamp(
                        path.stat().st_mtime, tz=timezone.utc
                    ).isoformat()
                except OSError:
                    continue
            out.append({
                "kind": "published",
                "ts": ts,
                "app_id": resolve_app_id(data) or spec_dir.name,
                "bot_id": _s(source.get("bot_id")),
                "outcome": "published",
                "detail": f"version {path.stem}"
                          + ("" if exact else " (time from file, not recorded)"),
            })
    return out[:ACTIVITY_PER_SOURCE_CAP]


def build_activity(
    bots: Iterable[str],
    *,
    read_manifests: ManifestReader,
    shared_dir: Path,
    jobs: Iterable[Any] = (),
    limit: int = 100,
) -> dict[str, Any]:
    """One feed: authoring/install jobs, promotions, publishes — newest first.

    Bounded on both sides: each source contributes at most
    ``ACTIVITY_PER_SOURCE_CAP`` and the union is sliced to ``limit``. The
    payload reports ``truncated`` so the surface can SAY it is showing a
    window — a silently sliced feed reads as "that's everything that
    happened", which it is not.
    """
    entries: list[dict[str, Any]] = []
    entries += _activity_from_jobs(jobs)
    entries += _activity_from_manifests(bots, read_manifests=read_manifests)
    entries += _activity_from_gallery_local(shared_dir)
    entries = [e for e in entries if e.get("ts")]
    entries.sort(key=lambda e: e["ts"], reverse=True)
    total = len(entries)
    return {
        "entries": entries[:limit],
        "count": min(total, limit),
        "total": total,
        "truncated": total > limit,
    }
