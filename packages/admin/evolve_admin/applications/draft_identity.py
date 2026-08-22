"""draft_identity — the read-only census of design §3's draft-identity rule.

AL-1.6a (``docs/build-AL-1.6-draft-identity.md`` §2/§3; the rule itself is
``docs/design-app-spec-and-discovery-2026-08-15.md`` §3/§4). Design §3 says:

    Discovered (not yet defined) apps have no ``app_id``. They have a
    scanner-assigned ``draft_id``, which is explicitly unstable and never
    appears in attribution, access, or sharing.

Nothing on the pod could *measure* that before this module. ``id_migration``
(AL-1.4a) walks the same artifacts, but its classifier tests ``app_id``
FIRST — so a manifest carrying an ``app_id`` reports ``already`` whether it is
a vouched app or a discovered draft that should never have had one. The two
states design §3 distinguishes collapse into one row, and the row reads green.

WHY THIS IS READ-ONLY AND WHY IT NAMES A "GRANDFATHERED" CLASS. Measured on
both live pods on 2026-08-19 (recorded in the build brief §7): **84 bot
manifests fleet-wide, ZERO carrying a ``draft_id``, 74 of them ``discovered``
*with* an ``app_id``.** That is the state design §3 forbids, and it is
nobody's bug:
AL-1.4a's backfill deliberately stamped every pre-existing manifest with the
id its legacy chain already resolved to, and the mint gate in
``stamp_identity`` (``not data.get(APP_ID_FIELD)``) means a manifest that
already carries an ``app_id`` can never acquire a ``draft_id`` afterwards.

So this census must NOT report those 74 as either passes or violations:

* counting them as passes is the vacuity trap the brief §2 exists to stop —
  "drafts have no ``app_id``" is trivially true when there are no drafts;
* counting them as violations invites a repair, and both available repairs
  are ruled out by existing design (re-identifying 74 live apps, or
  bulk-promoting them past the one gate that exists).

They get their own class, ``grandfathered``, reported separately from
``conforming`` and from ``violations``, so no future run can quietly bank
them as evidence. **Which population AL-1.6's acceptance is measured on is an
OPERATOR decision (build brief §2.1) and is unsettled at the time of writing;
this module deliberately states no verdict.** It reports the populations and
lets the operator's decision pick the one that counts.

The two ``violation`` classes are narrow on purpose — they are the states
that cannot arise from the 1.4a backfill and must therefore come from a
genuine defect:

* ``contaminated``  — ``discovered`` holding BOTH a ``draft_id`` and an
  ``app_id``. A mint conferred identity on a draft, or a backfill reversed a
  mint's refusal. Design §3 forbids both directions.
* ``unpromoted``    — ``defined`` holding a ``draft_id`` and no ``app_id``.
  Promotion did not confer identity, and it is self-sealing: ``ensure_app_id``
  honours the ``draft_id`` and will never stamp over it, so this state does
  not repair itself on the next read.

``unstamped`` (neither id) is NOT a violation. It splits by *why*, because
those two whys need opposite responses and #3620's lesson is that a class
that lumps them reads as one number:

* ``conformable``      — the legacy id is a valid slug, so the backfill simply
  has not reached this artifact. All 7 such manifests on the macOS pod sit on a
  single bot absent from ``network.json::members`` — so this walk does not even
  reach them. That is the census-walk gap AL-1.5 §9.8 deposited, not a draft
  population, and it is sized rather than fixed here.
* ``needs_conferral``  — ``canonical_app_id`` REFUSED the legacy id because
  stamping it would rewrite it. That is the behaviour-neutrality rule working,
  not a failure; conferring an id here is an operator act.

ONE POPULATION THIS WALK DOES NOT REACH, NAMED SO THE NEXT READER DOES NOT
TRIP ON IT. ``spec_migration._iter_targets`` walks a **third** source this one
does not: v-next App Specs at ``{shared_dir}/apps/specs/*.json``
(``app_spec_store.iter_spec_paths``, AL-1.5b's writer). They are *absent* from
this census, not misread — they live at a path neither walker here reaches.
The omission is information-free today: ``app_spec_store._checked_app_id``
refuses to write one without a conforming ``app_id``, so every such file is
defined by construction and none can carry a draft; and both live pods hold
**zero** of them (measured 2026-08-19). But AL-1.5b just shipped the writer and
the population will grow, so: **whoever adds that walk must yield an explicit
``KIND_SPEC``-family walk kind.** Adding the path without one hands ``_shape_of``
a dict with no ``definition_status``, which reads as discovered-with-an-app_id
and lands the entire v-next Spec population in ``grandfathered`` overnight.

READ-ONLY: this module never writes anything, anywhere. It has no ``--apply``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .app_identity import (
    APP_ID_FIELD,
    canonical_app_id,
    draft_id_of,
    is_discovered,
    resolve_legacy_app_id,
)
from .id_migration import (
    KIND_INSTANCE,
    KIND_MANIFEST,
    KIND_SPEC,
    _iter_manifest_paths,
    _iter_spec_paths,
    _shape_of,
)

CENSUS_VERSION = 1

# ── Classes ─────────────────────────────────────────────────────────────────
CLASS_DRAFT = "draft"                  # discovered · draft_id · no app_id
CLASS_DEFINED = "defined"              # defined · app_id
CLASS_GRANDFATHERED = "grandfathered"  # discovered · app_id · no draft_id
CLASS_UNSTAMPED = "unstamped"          # neither id (see reason)
CLASS_CONTAMINATED = "contaminated"    # discovered · app_id · draft_id
CLASS_UNPROMOTED = "unpromoted"        # defined · draft_id · no app_id
CLASS_SPEC_IDENTITY = "spec_identity"  # a gallery Spec carrying an app_id

# The §3-conforming states. Reported together, but a run whose ONLY conforming
# rows are ``defined`` has proved nothing about the draft rule — the CLI says
# so rather than leaving the reader to notice.
#
# ``spec_identity`` is DELIBERATELY NOT HERE, and the reason is a finding this
# chip's own fixture produced (recorded in the build brief §7.3a). A gallery
# Spec is minted by ``native_write.mint_v7_arc_app`` for EVERY scanner
# detection — ``mint=True`` is threaded only into ``_extract_instance``, never
# into ``_extract_spec``, and ``_extract_spec`` stamps ``app_id =
# canonical_app_id(spec_id)`` unconditionally. So a pod holding exactly ONE
# scanner draft also holds that draft's Spec, in the gallery, carrying a
# durable ``p-`` id. Counting those as passes would report the draft rule
# GREEN on the very artifact that breaks it — the vacuity trap wearing the
# other hat. They are reported, named, and excluded from the pass column;
# whether the mint should publish a Spec for a draft at all is a design
# question (§4 puts ``published`` two states past ``discovered``), not one
# this census may answer.
CONFORMING_CLASSES = (CLASS_DRAFT, CLASS_DEFINED)

# States that cannot arise from AL-1.4a's backfill, so they indicate a defect.
VIOLATION_CLASSES = (CLASS_CONTAMINATED, CLASS_UNPROMOTED)

# Reasons for CLASS_UNSTAMPED.
REASON_CONFORMABLE = "conformable"          # backfill has not reached it
REASON_NEEDS_CONFERRAL = "needs_conferral"  # canonical_app_id refused the id
REASON_NO_ID = "no_id"                      # no identity field at all

# Print order. Public (no underscore) because the CLI imports it — a private
# name in ``__all__`` is a contradiction the review of #3724 flagged.
CLASS_ORDER = (
    CLASS_DRAFT,
    CLASS_DEFINED,
    CLASS_SPEC_IDENTITY,
    CLASS_GRANDFATHERED,
    CLASS_UNSTAMPED,
    CLASS_CONTAMINATED,
    CLASS_UNPROMOTED,
)


@dataclass
class Entry:
    path: str
    kind: str
    bot_id: str
    definition_status: str
    app_id: str
    draft_id: str
    legacy_id: str
    klass: str
    reason: str = ""
    # A promoted app that still carries its scanner draft id. Not a violation
    # on its own (the app_id is conferred and load-bearing), but design §3
    # says a draft id must never appear in attribution/access/sharing, so a
    # surviving one is residue worth naming.
    draft_residue: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "bot_id": self.bot_id,
            "definition_status": self.definition_status,
            "app_id": self.app_id,
            "draft_id": self.draft_id,
            # identity: see resolve_app_id — a census COLUMN recording what the
            # legacy chain resolves to, which is exactly what decides whether
            # an unstamped row is `conformable` or `needs_conferral`.
            "legacy_id": self.legacy_id,
            "class": self.klass,
            "reason": self.reason,
            "draft_residue": self.draft_residue,
        }


@dataclass
class CensusReport:
    entries: list[Entry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Bots walked, and bot homes carrying manifests that the walk did NOT
    # reach. The walk follows ``network.json::members`` like every other
    # census on this pod; AL-1.5 §9.8 sized the gap at 15 manifests / 3 bots
    # on the macOS pod. Reported rather than fixed — closing it moves every
    # baseline in the AL-1.5 record at once, and that is not this chip.
    bots_walked: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.klass] = out.get(e.klass, 0) + 1
        return out

    @property
    def unstamped_reasons(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries:
            if e.klass == CLASS_UNSTAMPED:
                out[e.reason] = out.get(e.reason, 0) + 1
        return out

    @property
    def violations(self) -> list[Entry]:
        return [e for e in self.entries if e.klass in VIOLATION_CLASSES]

    @property
    def drafts(self) -> list[Entry]:
        return [e for e in self.entries if e.klass == CLASS_DRAFT]

    @property
    def grandfathered(self) -> list[Entry]:
        return [e for e in self.entries if e.klass == CLASS_GRANDFATHERED]

    @property
    def spec_identity(self) -> list[Entry]:
        """Gallery Specs carrying an ``app_id`` — including, on any pod with a
        draft, that draft's OWN Spec. Neither a pass nor a violation."""
        return [e for e in self.entries if e.klass == CLASS_SPEC_IDENTITY]

    @property
    def draft_residue(self) -> list[Entry]:
        return [e for e in self.entries if e.draft_residue]

    @property
    def draft_rule_is_vacuous(self) -> bool:
        """True when NO artifact on the pod can exercise the draft rule.

        The load-bearing property of this whole census. "Discovered apps have
        no ``app_id``" is trivially true on a pod with zero drafts, and a
        green run that does not say so is the vacuity trap the build brief §2
        exists to stop. The CLI and the JSON both carry this flag so a
        downstream reader cannot bank an empty population as a pass.
        """
        return not self.drafts

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CENSUS_VERSION,
            "bots_walked": list(self.bots_walked),
            "population": len(self.entries),
            "counts": self.counts,
            "unstamped_reasons": self.unstamped_reasons,
            "draft_rule_is_vacuous": self.draft_rule_is_vacuous,
            "violations": len(self.violations),
            "spec_identity": len(self.spec_identity),
            "draft_residue": len(self.draft_residue),
            "entries": [e.to_dict() for e in self.entries],
            "errors": list(self.errors),
        }


def _read_json_dict(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):  # ValueError: JSON + Unicode decode
        return None
    return data if isinstance(data, dict) else None


def classify(data: dict, kind: str = "") -> tuple[str, str, bool]:
    """``(class, reason, draft_residue)`` for one artifact dict.

    Pure: takes the raw dict, touches no disk. Split out from the walk so the
    tests and the §2.2 mint-path proof can drive it directly on a freshly
    minted Instance without staging a whole pod.

    ``kind`` matters for exactly one case, and getting it wrong swamps the
    report either way. A **gallery Spec** carries no ``definition_status``, so
    the v27 inert default would read all 153 of them on the macOS pod as
    "discovered with an app_id" and inflate the grandfathered population from
    72 to 225 — burying the number the build brief §2 is actually about.

    But the obvious correction — call them ``defined``, since design §4 puts
    ``published`` two states past ``discovered`` — is ALSO wrong, and wrong in
    the dangerous direction: it moves the same 153 rows into the pass column.
    Design §4 says a Spec means the app was defined; **the code does not**.
    ``mint_v7_arc_app`` extracts and writes a Spec for every scanner detection
    and threads ``mint=True`` only into ``_extract_instance``, so a draft's own
    Spec is published at discovery time carrying a durable ``app_id``. Specs
    therefore get their OWN class, ``spec_identity``, in neither the pass
    column nor the violation column — the same treatment ``grandfathered``
    gets, for the same reason.

    A Spec carrying a ``draft_id`` IS a violation: design §3 says a draft id
    never appears in sharing, and the gallery is the sharing layer.

    ``app_id`` is read as a raw non-empty string rather than through
    ``is_canonical_app_id``, matching ``id_migration._classify``'s
    ``STATUS_ALREADY``. Deliberate: the question here is "did something confer
    an identity field", not "does the resolver honour it". A non-conforming
    value is still a conferred id that ``ensure_app_id`` will refuse to
    overwrite, so treating it as absent would report a manifest as needing a
    stamp it can never receive.
    """
    app_id = data.get(APP_ID_FIELD)
    app_id = app_id.strip() if isinstance(app_id, str) else ""
    draft_id = draft_id_of(data)
    # ``is_discovered`` treats an ABSENT definition_status as discovered (the
    # v27 inert default). Reproduced here rather than re-tested, so the census
    # and the mint gate can never disagree about what a draft is.
    discovered = _reads_as_discovered(data) and kind != KIND_SPEC

    if discovered:
        if draft_id and app_id:
            return CLASS_CONTAMINATED, "", False
        if draft_id:
            return CLASS_DRAFT, "", False
        if app_id:
            return CLASS_GRANDFATHERED, "", False
        return CLASS_UNSTAMPED, _unstamped_reason(data), False

    if kind == KIND_SPEC:
        if draft_id:
            # A draft id in the gallery is the one thing design §3 names
            # outright: a draft "never appears in attribution, access, or
            # sharing".
            return CLASS_CONTAMINATED, "", True
        if app_id:
            return CLASS_SPEC_IDENTITY, "", False
        return CLASS_UNSTAMPED, _unstamped_reason(data), False

    # defined (or any other non-discovered status)
    if app_id:
        return CLASS_DEFINED, "", bool(draft_id)
    if draft_id:
        return CLASS_UNPROMOTED, "", True
    return CLASS_UNSTAMPED, _unstamped_reason(data), False


def _reads_as_discovered(data: dict) -> bool:
    """``is_discovered``, plus the out-of-enum case it does not cover.

    ``is_discovered`` returns True for absent/blank (the v27 inert default) and
    for the literal ``"discovered"`` — so a status that is present but NOT in
    the closed enum (a typo, a hand-edit, a future value) returns False and
    would land in the ``defined`` arm, i.e. report as a conforming pass. That
    is the safe-LOOKING direction, which is the one this census must never
    take. ``manifest.migrate_manifest`` already normalizes such a value to
    ``discovered`` on read; this mirrors that decision so an artifact that has
    not been migrated yet is classified the same way as one that has.
    """
    status = data.get("definition_status") if isinstance(data, dict) else None
    if isinstance(status, str) and status.strip():
        from .manifest import VALID_MANIFEST_DEFINITION_STATUSES
        if status.strip() not in VALID_MANIFEST_DEFINITION_STATUSES:
            return True  # out-of-enum reads as discovered, as migrate does
    return is_discovered(data)


def _unstamped_reason(data: dict) -> str:
    legacy = resolve_legacy_app_id(data)
    if not legacy:
        return REASON_NO_ID
    # ``context=""`` on purpose: canonical_app_id logs an INFO line when it
    # refuses, and a census that walks the whole pod would emit one per
    # non-conforming artifact on every run. The refusal is the return value;
    # the operator sees it as a row, not as log noise.
    return (
        REASON_CONFORMABLE if canonical_app_id(legacy) else REASON_NEEDS_CONFERRAL
    )


def _definition_status_of(data: dict) -> str:
    status = data.get("definition_status")
    if isinstance(status, str) and status.strip():
        return status.strip()
    return "(absent)"


def _iter_targets(shared: Path, bot_ids: list[str]
                  ) -> Iterator[tuple[Path, str, str]]:
    """``(path, bot_id, kind)`` for every identity-bearing artifact.

    Same population as ``id_migration.build_report`` — deliberately reusing
    its two path walkers rather than writing a second one (#3498-class rule:
    two walkers drift and then the two censuses disagree about the pod).
    """
    for bot in bot_ids:
        for p in _iter_manifest_paths(shared, bot):
            yield p, bot, ""
    for p in _iter_spec_paths(shared):
        yield p, "", KIND_SPEC


def build_report(shared_dir: Path, bot_ids: list[str]) -> CensusReport:
    """Census design §3's draft-identity rule across the pod. READ-ONLY."""
    shared = Path(shared_dir)
    report = CensusReport(bots_walked=list(bot_ids))

    for path, bot_id, walk_kind in _iter_targets(shared, bot_ids):
        data = _read_json_dict(path)
        if data is None:
            report.errors.append(f"unreadable or non-object JSON: {path}")
            continue
        kind = walk_kind or _shape_of(data)
        klass, reason, residue = classify(data, kind)
        report.entries.append(Entry(
            path=str(path),
            kind=kind,
            bot_id=bot_id,
            definition_status=_definition_status_of(data),
            app_id=(data.get(APP_ID_FIELD) or "").strip()
            if isinstance(data.get(APP_ID_FIELD), str) else "",
            draft_id=draft_id_of(data),
            legacy_id=resolve_legacy_app_id(data),
            klass=klass,
            reason=reason,
            draft_residue=residue,
        ))
    return report


__all__ = [
    "CENSUS_VERSION",
    "CLASS_CONTAMINATED",
    "CLASS_DEFINED",
    "CLASS_DRAFT",
    "CLASS_GRANDFATHERED",
    "CLASS_SPEC_IDENTITY",
    "CLASS_UNPROMOTED",
    "CLASS_UNSTAMPED",
    "CONFORMING_CLASSES",
    "VIOLATION_CLASSES",
    "REASON_CONFORMABLE",
    "REASON_NEEDS_CONFERRAL",
    "REASON_NO_ID",
    "KIND_INSTANCE",
    "KIND_MANIFEST",
    "KIND_SPEC",
    "CLASS_ORDER",
    "CensusReport",
    "Entry",
    "build_report",
    "classify",
]
