"""spec_migration — the v-next Spec readiness census (AL-1.5a).

internal/build-AL-1.5-spec-vnext.md §2. Walks every artifact on the pod that
carries an app's portable intent — per-bot manifests (legacy single-file and
v7-arc Instances) and gallery Specs across all tiers — runs each through
``app_spec.spec_from_manifest``, and reports what the v-next model makes of
it. The artifact set and the walk are deliberately the same shape as
``id_migration`` (AL-1.4a), because they are censusing the same population and
two different answers to "what is on this pod" would be a bug in one of them.

NOTHING ON DISK IS REWRITTEN — not by ``--dry-run``, not by ``--apply``. The
design's migration strategy is *migrate on read* (§10 risk table), so 1.5a
builds the read and proves it; ``--apply`` writes exactly one file, the census
table at ``{shared_dir}/apps/spec-migration.json``. That asymmetry with
``migrate-ids`` (whose ``--apply`` stamps ``app_id`` into artifacts) is
intentional: a stamped ``app_id`` was behaviour-neutral by construction, while
a v-next Spec written next to a v7-arc Spec would be a second source of truth
for the same app with no discriminator to tell them apart. 1.5b is where a
writer lands.

WHAT THE CENSUS IS FOR. Two questions the operator has to be able to answer
before 1.5b points a writer at this model:

  1. **Does every app on the pod derive?** ``clean`` / ``partial`` / ``draft``
     / ``blocked``, with the per-artifact problem list from
     ``AppSpec.validate()``. ``blocked`` is the real gate — an artifact with
     no conforming identity that is not a draft cannot be shared, and AL-3.1
     (publish then install elsewhere) is built on exactly that.

  2. **What does the frozen field list LOSE?** Design §5 says the per-field
     migration note "lives in the build chip, not here" — ``FIELD_DISPOSITION``
     below IS that note: all 104 manifest dataclass fields plus the v7-arc
     Spec and Instance keys, each assigned one of: carried into a §5 field,
     moved to the bot-local Instance, derived elsewhere, dropped by the
     design's own "what is gone" paragraph, or **no_home** — real content the
     frozen list has nowhere to put. The census reports a ``no_home`` field
     only when the artifact actually POPULATES it, so the output is the list
     of things that would really be lost on this pod, not a restatement of
     the schema.

     ``no_home`` is a finding, not an error. Design §10's scope-creep rule
     makes adding a twelfth field an operator decision, so this chip measures
     the gap and reports it in the PR body rather than closing it.

An artifact whose top-level key is not in ``FIELD_DISPOSITION`` at all is
reported as ``unclassified`` — a field nobody has decided about, which is the
one outcome that should never pass silently.

AL-1.5b ADDED THE SHAPE COLUMN. Now that ``app_spec_store`` writes v-next
Specs to ``{shared_dir}/apps/specs/``, "how much of this pod is v-next yet?"
is a question with a moving answer, and a census that reported a written Spec
identically to a migrated-on-read manifest could not answer it. Every row
carries ``shape`` — ``v-next`` for an artifact that already IS one,
``legacy`` for one the reader migrated — and ``CensusReport.shape_counts``
aggregates it. The shape is computed from the artifact's own discriminator
(``app_spec.is_vnext_artifact``), never from which directory the walk found it
in, so a Spec that has been copied or imported still reads as v-next and a
legacy manifest that lands in the v-next directory still reads as legacy.

``--apply`` STILL WRITES ONLY THE TABLE. 1.5b makes the *reflex* write a Spec
at the moment it defines an app; it does not make the census a pod-wide
migration. Rewriting 227 artifacts into a shape whose readers do not exist yet
is the opposite of migrate-on-read, and a test still byte-compares every
artifact before and after ``--apply``.
"""

# identity: see applications.app_identity.resolve_app_id. The legacy field
# names below are this module's SUBJECT MATTER — FIELD_DISPOSITION is the
# census table saying where each one lands in the v-next Spec, and it routes
# every id-shaped field to app_id "through resolve_app_id, never read
# directly". The one runtime read, ``data.get("pkg_id")``, resolves a
# FILES-PACK DIRECTORY on disk (gallery package key -> path), not an identity.

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from evolve_util import atomic_write_json, now_iso

from .app_spec import AppSpec, is_vnext_artifact
from .app_spec_store import iter_spec_paths as _iter_vnext_spec_paths
from .app_spec_store import spec_from_artifact_with_notes
from .id_migration import (
    KIND_INSTANCE,
    KIND_MANIFEST,
    KIND_SPEC,
    _iter_manifest_paths,
    _iter_spec_paths,
    _read_json_dict,
    _shape_of,
)

CENSUS_TABLE_VERSION = 3

# The fourth artifact kind, alongside id_migration's manifest / instance /
# spec: a v-next App Spec written by ``app_spec_store`` (AL-1.5b).
KIND_APP_SPEC = "app_spec"

# What the reader had to do to get an AppSpec out of the artifact. This is the
# "is the pod v-next yet?" column, and it is the one number 1.5c's rollout is
# measured against.
SHAPE_VNEXT = "v-next"      # already a v-next Spec; round-tripped, not derived
SHAPE_LEGACY = "legacy"     # migrated on read from a v28/v30 or v7-arc artifact

STATUS_CLEAN = "clean"          # derives with no validate() problems
STATUS_PARTIAL = "partial"      # derives, but incompletely
STATUS_DRAFT = "draft"          # no app_id because it is a draft (design §3)
STATUS_BLOCKED = "blocked"      # no identity and not a draft — cannot be shared

# Statuses that mean the artifact cannot become a portable spec as it stands.
BLOCKING_STATUSES = (STATUS_BLOCKED,)

# ── The per-field migration note design §5 defers to this chip ───────────────
#
# Every top-level key any of the three artifact shapes can carry, mapped to
# what v-next does with it. Five dispositions:
#
#   "spec"     — feeds one of the eleven §5 fields.
#   "instance" — bot-local: design §5's Instance list, or per-bot operational
#                state that was never portable (audit stamps, install job,
#                reconciliation, workspace sync).
#   "derived"  — recomputed from other state; design §5 names coherence-pass
#                state and the Tier-2 detail blocks explicitly.
#   "dropped"  — named in design §5's "What is gone from the portable model"
#                paragraph: per-app test cases, satisfaction as a spec field,
#                the core/feature/optional priority, interface_contract prose.
#   "no_home"  — REAL CONTENT WITH NO §5 FIELD. Not a decision, a measurement.
#   "envelope" — metadata about the artifact, not about the app: which shape
#                the file is written in, which schema version it parses as.
#                Never portable, never lost — the writer restamps it.

DISPOSITION_SPEC = "spec"
DISPOSITION_INSTANCE = "instance"
DISPOSITION_DERIVED = "derived"
DISPOSITION_DROPPED = "dropped"
DISPOSITION_NO_HOME = "no_home"
# AL-1.5b: a sixth, for keys that describe the ARTIFACT rather than the app —
# which shape the file is written in, which dataclass version it parses as.
# 1.5a filed ``schema_version`` and ``manifest_shape`` under "instance" for
# want of anywhere better; they are not bot-local state and calling them that
# was the map saying something untrue about itself. Writing v-next forces the
# question, because the discriminator ``spec_shape`` is exactly this kind of
# key and it sits on a *Spec* — the one artifact that is by definition not an
# instance. Purely a labelling change: neither bucket is reported (only
# ``no_home`` is), so the census's output is identical either way.
DISPOSITION_ENVELOPE = "envelope"

FIELD_DISPOSITION: dict[str, str] = {
    # → app_id (through resolve_app_id, never read directly)
    "app_id": DISPOSITION_SPEC, "id": DISPOSITION_SPEC,
    "pkg_id": DISPOSITION_SPEC, "spec_id": DISPOSITION_SPEC,
    "instance_id": DISPOSITION_SPEC,
    # → spec_version
    "spec_version": DISPOSITION_SPEC, "version": DISPOSITION_SPEC,
    "pkg_version": DISPOSITION_SPEC, "gallery_version": DISPOSITION_SPEC,
    # → name / purpose
    "name": DISPOSITION_SPEC, "display_name": DISPOSITION_SPEC,
    "purpose": DISPOSITION_SPEC, "objective": DISPOSITION_SPEC,
    "description": DISPOSITION_SPEC, "identity": DISPOSITION_SPEC,
    # → kind / runs / invocation_mode / bot_guidance. The invocation cluster
    # became §5 fields by operator decision 2026-08-18, on this census's
    # evidence and because all three have live readers: the plugin's
    # TurnObserver gates Layer-C interception on ``invocation_mode``, and
    # ``bot_guidance_freelance_validator`` gates install on ``bot_guidance``.
    "bot_guidance": DISPOSITION_SPEC, "invocation_mode": DISPOSITION_SPEC,
    "crons": DISPOSITION_SPEC, "scheduled_actions": DISPOSITION_SPEC,
    "schedules": DISPOSITION_SPEC, "heartbeat_evidence": DISPOSITION_SPEC,
    "cron_evidence": DISPOSITION_SPEC, "usage": DISPOSITION_SPEC,
    "example_triggers": DISPOSITION_SPEC,
    # → requires
    "requirements": DISPOSITION_SPEC, "dependencies": DISPOSITION_SPEC,
    "provided_capabilities": DISPOSITION_SPEC,
    # → audience / privacy (the gateable pair). ``privacy`` became a §5 field
    # by operator decision 2026-08-18, on this census's own evidence: it was
    # populated on 206 of 227 macOS-pod artifacts and all 5 Linux-pod ones,
    # and privacy.shareable_in_lessons is the live Lessons-sharing gate.
    "audience_scoping": DISPOSITION_SPEC, "approval_audience": DISPOSITION_SPEC,
    "privacy": DISPOSITION_SPEC,
    # → permissions (gateable): app_permissions.reconciler reads the declared
    # exec/fs/network/env surface and app_manifest_monitor raises
    # ``app_permission_drift`` against exec-approvals.json off it.
    "permissions": DISPOSITION_SPEC,
    # → provenance. AL-1.5b correction: ``provenance`` itself was filed
    # "instance" in 1.5a, and it is not — ``_derive_provenance`` reads
    # ``provenance.source.pod_id``/``.bot_id``/``.created_at`` and
    # ``derive_spec_version`` reads ``provenance.spec_version``, so on a legacy
    # artifact it FEEDS the §5 field, and on a v-next Spec it IS the §5 field.
    # Design §5's Instance list does not name it. Output-neutral (neither
    # bucket is reported), but the note is the deliverable design §5 delegated
    # to this arc, so it has to be true.
    "provenance": DISPOSITION_SPEC,
    "source": DISPOSITION_SPEC, "created_at": DISPOSITION_SPEC,
    "definition_status": DISPOSITION_SPEC,
    # → package
    "files": DISPOSITION_SPEC, "realized_files": DISPOSITION_SPEC,
    "blueprint": DISPOSITION_SPEC, "files_pack": DISPOSITION_SPEC,

    # Bot-local. design §5 Instance: app_id, bot_id, spec_version pinned,
    # config, realized_files[], installed_schedules[], access_overrides,
    # status, change_log[]. Everything else here is per-bot operational state
    # a portable spec never carried meaning for.
    "bot_id": DISPOSITION_INSTANCE, "status": DISPOSITION_INSTANCE,
    "owner": DISPOSITION_INSTANCE, "change_log": DISPOSITION_INSTANCE,
    "configured_schedules": DISPOSITION_INSTANCE,
    "learned_config": DISPOSITION_INSTANCE,
    "spec_version_history": DISPOSITION_INSTANCE,
    "dependency_check_at_install": DISPOSITION_INSTANCE,
    "usage_metadata": DISPOSITION_INSTANCE, "evidence": DISPOSITION_INSTANCE,
    "evidence_files": DISPOSITION_INSTANCE,
    "source_detail": DISPOSITION_INSTANCE, "confidence": DISPOSITION_INSTANCE,
    "install_job": DISPOSITION_INSTANCE, "workspace_sync": DISPOSITION_INSTANCE,
    "workspace_files_source": DISPOSITION_INSTANCE,
    "reconciliation": DISPOSITION_INSTANCE, "drift_log": DISPOSITION_INSTANCE,
    "improvement_history": DISPOSITION_INSTANCE,
    "updated_at": DISPOSITION_INSTANCE, "approved_at": DISPOSITION_INSTANCE,
    "last_reviewed": DISPOSITION_INSTANCE,
    "last_reviewed_at": DISPOSITION_INSTANCE,
    "last_reflect_at": DISPOSITION_INSTANCE,
    "audit_trail_path": DISPOSITION_INSTANCE,
    "audit_cadence": DISPOSITION_INSTANCE,
    "audit_eligible": DISPOSITION_INSTANCE,
    "audit_accepted": DISPOSITION_INSTANCE,
    "app_files_privacy": DISPOSITION_INSTANCE,
    "data_paths": DISPOSITION_INSTANCE,
    "default_for_unclassified": DISPOSITION_INSTANCE,
    "volatile_paths": DISPOSITION_INSTANCE,
    "compliance_suppressed": DISPOSITION_INSTANCE,
    "compliance_suppressed_reason": DISPOSITION_INSTANCE,
    "draft_id": DISPOSITION_INSTANCE,
    "seeded_from_pkg_version": DISPOSITION_INSTANCE,
    "seeded_from_pkg_sha256": DISPOSITION_INSTANCE,

    # ── The v-next App Spec's own top-level keys (AL-1.5b) ──────────────
    # A written Spec is censused like any other artifact, so its keys need
    # dispositions too or the census's own output would read ``unclassified``
    # against the shape this arc exists to produce. Ten of design §5's fifteen
    # fields already appear above under the legacy carrier they migrate from
    # (``app_id``, ``spec_version``, ``name``, ``purpose``, ``invocation_mode``,
    # ``bot_guidance``, ``privacy``, ``permissions``, plus ``provenance``);
    # these six are the names that exist only once an artifact IS v-next.
    # None of them collides with a manifest dataclass field or a v7 schema
    # property — checked, not assumed.
    "kind": DISPOSITION_SPEC, "runs": DISPOSITION_SPEC,
    "requires": DISPOSITION_SPEC, "exclusive_tools": DISPOSITION_SPEC,
    "audience": DISPOSITION_SPEC, "package": DISPOSITION_SPEC,

    # The shape discriminator. Envelope, NOT a sixteenth §5 field — see
    # app_spec's module docstring for why that boundary is where it is.
    "schema_version": DISPOSITION_ENVELOPE,
    "manifest_shape": DISPOSITION_ENVELOPE,
    "spec_shape": DISPOSITION_ENVELOPE,
    "spec_shape_version": DISPOSITION_ENVELOPE,

    # Recomputed, never stored portably (design §5: coherence-pass state is
    # "derived, lives with the audit runner"; Tier-2 detail blocks are
    # "rendered from runs/requires, not stored").
    "coherence": DISPOSITION_DERIVED, "classification": DISPOSITION_DERIVED,
    "app_kind": DISPOSITION_DERIVED, "capability_tags": DISPOSITION_DERIVED,
    "session_keywords": DISPOSITION_DERIVED,
    "last_audit": DISPOSITION_DERIVED,
    "last_structural_verify": DISPOSITION_DERIVED,
    "last_verification": DISPOSITION_DERIVED,

    # Named in design §5's "what is gone" paragraph.
    "test_cases": DISPOSITION_DROPPED, "tests": DISPOSITION_DROPPED,
    "test_command": DISPOSITION_DROPPED, "test_cadence": DISPOSITION_DROPPED,
    "test_exemption_reason": DISPOSITION_DROPPED,
    "last_tested": DISPOSITION_DROPPED, "last_test_result": DISPOSITION_DROPPED,
    "last_test_run": DISPOSITION_DROPPED, "last_test_output": DISPOSITION_DROPPED,
    "last_test_exit_code": DISPOSITION_DROPPED,
    "satisfaction": DISPOSITION_DROPPED,
    "satisfaction_score": DISPOSITION_DROPPED,
    "satisfaction_notes": DISPOSITION_DROPPED,
    "priority": DISPOSITION_DROPPED,
    "interface_contract": DISPOSITION_DROPPED,

    # No §5 field. Each is real, authored or scanner-derived content the
    # eleven fields cannot hold. Reported per artifact ONLY when populated.
    "event_triggers": DISPOSITION_NO_HOME,
    "app_dependencies": DISPOSITION_NO_HOME,
    "inputs": DISPOSITION_NO_HOME, "outputs": DISPOSITION_NO_HOME,
    "exported_hooks": DISPOSITION_NO_HOME,
    "build_spec": DISPOSITION_NO_HOME,
    "goals": DISPOSITION_NO_HOME, "known_issues": DISPOSITION_NO_HOME,
    "open_questions": DISPOSITION_NO_HOME,
    "constraints": DISPOSITION_NO_HOME,
    "success_criteria": DISPOSITION_NO_HOME,
    "success_metrics": DISPOSITION_NO_HOME,
    "privacy_constraints": DISPOSITION_NO_HOME,
    "rsigrade_signals": DISPOSITION_NO_HOME, "docs": DISPOSITION_NO_HOME,
    "tags": DISPOSITION_NO_HOME, "maintainers": DISPOSITION_NO_HOME,
    "author": DISPOSITION_NO_HOME, "app_version": DISPOSITION_NO_HOME,
    "desired_improvements": DISPOSITION_NO_HOME,
    "scope_excludes": DISPOSITION_NO_HOME,
    # Found by the live-pod census (2026-08-18) as ``unclassified`` — written
    # on disk by writers outside the declared dataclass/schema, which is
    # exactly the hole that bucket exists to surface.
    #   merged_from [draft-name…]      — 8 instances. Scanner merge history:
    #     bot-local provenance of a draft merge, never portable.
    "merged_from": DISPOSITION_INSTANCE,
    #   _evolve {"pkg", "file"}        — 4 instances. Embedded marker block
    #     tying the artifact to a package + file marker. Bot-local.
    "_evolve": DISPOSITION_INSTANCE,
}


def census_table_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / "apps" / "spec-migration.json"


def _is_populated(value: Any) -> bool:
    """Truthy-but-honest: 0 and False are populated, [] / {} / "" are not."""
    if value is None:
        return False
    if isinstance(value, (list, dict, str)):
        return len(value) > 0
    return True


def _no_home_fields(data: dict) -> list[str]:
    """``no_home`` fields this artifact actually carries data in."""
    return sorted(
        key for key, value in data.items()
        if FIELD_DISPOSITION.get(key) == DISPOSITION_NO_HOME and _is_populated(value)
    )


def _unclassified_fields(data: dict) -> list[str]:
    """Top-level keys no disposition covers — the one silent-loss risk."""
    return sorted(key for key in data if key not in FIELD_DISPOSITION)


@dataclass
class Entry:
    path: str
    kind: str
    bot_id: str
    app_id: str
    spec_version: int
    status: str
    problems: list[str] = field(default_factory=list)
    no_home: list[str] = field(default_factory=list)
    unclassified: list[str] = field(default_factory=list)
    legacy_spec_version: str = ""
    # SHAPE_VNEXT | SHAPE_LEGACY — what the reader had to do to get an AppSpec
    # out of this artifact. Defaults to legacy because every artifact on the
    # pod was legacy until 1.5b's writer landed.
    shape: str = SHAPE_LEGACY
    # AL-1.5c — why a declared file still has no digest. Each entry is
    # "<kind>: <path>" from app_spec_store.PackageFileNote. EMPTY is the
    # good state and means every declared file hashed. The brief's rule:
    # a file that cannot be hashed is reported, never silently written as
    # sha256="" — an empty sha reads the same as "nobody has looked yet",
    # and that ambiguity is how the deterministic-install gap survived
    # from design §6 all the way to AL-1.5c.
    sha_gaps: list[str] = field(default_factory=list)
    # The derived ``package.files`` for this artifact, kept so the report can
    # count declared-vs-hashed without re-deriving. Not serialized into the
    # table: the digests belong on the Spec, not in a census row.
    package_files: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "shape": self.shape,
            "bot_id": self.bot_id,
            "app_id": self.app_id,
            "spec_version": self.spec_version,
            "legacy_spec_version": self.legacy_spec_version,
            "status": self.status,
            "problems": list(self.problems),
            "no_home": list(self.no_home),
            "unclassified": list(self.unclassified),
            "sha_gaps": list(self.sha_gaps),
        }


@dataclass
class CensusReport:
    entries: list[Entry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    table_path: str = ""
    dry_run: bool = True

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.status] = out.get(e.status, 0) + 1
        return out

    @property
    def blocking(self) -> list[Entry]:
        return [e for e in self.entries if e.status in BLOCKING_STATUSES]

    @property
    def shape_counts(self) -> dict[str, int]:
        """``{shape: artifacts}`` — the "is this pod v-next yet?" number.

        Ordered v-next first so the answer to the question leads, and both
        keys are always present (a zero is an answer; a missing key reads as
        "not measured").
        """
        out = {SHAPE_VNEXT: 0, SHAPE_LEGACY: 0}
        for e in self.entries:
            out[e.shape] = out.get(e.shape, 0) + 1
        return out

    @property
    def sha_counts(self) -> dict[str, int]:
        """AL-1.5c — is this pod's deterministic-install gap actually closing?

        ``files_declared`` / ``files_hashed`` are the headline pair: before
        this chip the second number was ~0 pod-wide, because the only carrier
        for ``package.files[].sha256`` was a gallery files-pack and one
        artifact of 232 had one. ``artifacts_with_gaps`` counts artifacts
        where at least one declared file could not be hashed, and
        ``gap_kinds`` says why (missing / unreadable / directory /
        no_workspace) so a permissions regression never hides inside the same
        number as ordinary manifest-vs-workspace drift.
        """
        declared = hashed = with_gaps = 0
        kinds: dict[str, int] = {}
        for e in self.entries:
            for f in e.package_files:
                declared += 1
                if f.get("sha256"):
                    hashed += 1
            if e.sha_gaps:
                with_gaps += 1
            for gap in e.sha_gaps:
                kind = gap.split(":", 1)[0]
                kinds[kind] = kinds.get(kind, 0) + 1
        return {
            "files_declared": declared,
            "files_hashed": hashed,
            "artifacts_with_gaps": with_gaps,
            **{f"gap_{k}": v for k, v in sorted(kinds.items())},
        }

    @property
    def no_home_counts(self) -> dict[str, int]:
        """``{field: artifacts carrying it}`` — the §5 gap, measured."""
        out: dict[str, int] = {}
        for e in self.entries:
            for name in e.no_home:
                out[name] = out.get(name, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

    @property
    def unclassified_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries:
            for name in e.unclassified:
                out[name] = out.get(name, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _legacy_spec_version(data: dict) -> str:
    """The version string ``derive_spec_version`` packed into an int, kept so
    the census never loses the original (the tail clamp is lossy above 9.9)."""
    for key in ("spec_version", "pkg_version", "gallery_version"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    prov = data.get("provenance")
    if isinstance(prov, dict) and isinstance(prov.get("spec_version"), str):
        return prov["spec_version"].strip()
    return ""


def _classify(path: Path, data: dict, kind: str, bot_id: str,
              spec: AppSpec, notes: list[Any] | None = None) -> Entry:
    problems = spec.validate()
    draft_id = data.get("draft_id")
    is_draft = isinstance(draft_id, str) and bool(draft_id.strip())

    if not spec.app_id:
        # A draft correctly has no identity (design §3) — that is the scanner
        # holding the line, not a migration failure. Anything else with no
        # identity cannot be published or granted against, so it blocks.
        status = STATUS_DRAFT if is_draft else STATUS_BLOCKED
    elif problems:
        status = STATUS_PARTIAL
    else:
        status = STATUS_CLEAN

    return Entry(
        path=str(path), kind=kind, bot_id=bot_id, app_id=spec.app_id,
        spec_version=spec.spec_version, status=status, problems=problems,
        no_home=_no_home_fields(data), unclassified=_unclassified_fields(data),
        legacy_spec_version=_legacy_spec_version(data),
        # From the artifact's own discriminator, not from the directory the
        # walk found it in — see the module docstring.
        shape=SHAPE_VNEXT if is_vnext_artifact(data) else SHAPE_LEGACY,
        sha_gaps=[f"{n.kind}: {n.path}" for n in (notes or [])],
        package_files=list(spec.package.get("files") or []),
    )


def _iter_targets(shared: Path, bot_ids: list[str]
                  ) -> Iterator[tuple[Path, str, str]]:
    """``(path, bot_id, kind)`` for every artifact on the pod.

    ``kind`` is carried out of the walk rather than re-inferred from the data,
    because the walk is the only thing that knows WHERE a file came from —
    ``_shape_of`` cannot tell a gallery Spec from a bot manifest, and after
    AL-1.5b it cannot tell either from a v-next App Spec.
    """
    for bot in bot_ids:
        for p in _iter_manifest_paths(shared, bot):
            yield p, bot, ""
    for p in _iter_spec_paths(shared):
        yield p, "", KIND_SPEC
    # AL-1.5b: v-next Specs written by app_spec_store. Pod-wide like the
    # gallery, so no bot_id.
    for p in _iter_vnext_spec_paths(shared):
        yield p, "", KIND_APP_SPEC


def build_report(
    shared_dir: Path, bot_ids: list[str], *, apply: bool = False,
) -> CensusReport:
    """Derive a v-next ``AppSpec`` for every artifact on the pod and report.

    ``apply=True`` writes the census table and nothing else — no artifact on
    disk is modified either way (see the module docstring).
    """
    shared = Path(shared_dir)
    report = CensusReport(dry_run=not apply)

    for path, bot_id, walk_kind in _iter_targets(shared, bot_ids):
        data = _read_json_dict(path)
        if data is None:
            report.errors.append(f"unreadable or non-object JSON: {path}")
            continue
        kind = walk_kind or _shape_of(data)
        try:
            # ONE derivation, shared with the writer (app_spec_store) — the
            # census must not answer "what does this artifact become" any
            # differently from the thing that writes the answer down.
            spec, notes = spec_from_artifact_with_notes(data)
        except Exception as exc:  # noqa: BLE001 — one bad artifact must not
            # end the census; the point of the run is the whole population.
            report.errors.append(f"derive failed for {path}: {exc}")
            continue
        report.entries.append(_classify(path, data, kind, bot_id, spec, notes))

    table_path = census_table_path(shared)
    report.table_path = str(table_path)
    if apply:
        try:
            table_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(table_path, _table(report), mode=0o644)
        except OSError as exc:
            report.errors.append(f"table write failed for {table_path}: {exc}")
    return report


def _table(report: CensusReport) -> dict[str, Any]:
    return {
        "version": CENSUS_TABLE_VERSION,
        "generated_at": now_iso(),
        "generated_by": "evolve-admin application migrate-specs",
        "note": (
            "AL-1.5a readiness census. NOTHING ON DISK WAS REWRITTEN — v-next "
            "is migrate-on-read, so this run derived an AppSpec for every "
            "artifact and reported the result. 'blocked' rows have no "
            "conforming identity and are not drafts: they cannot be published "
            "or granted against, and they gate AL-3.1. 'no_home' names real "
            "content the frozen design-§5 field list has nowhere to put — a "
            "measurement for the operator, not a defect. 'unclassified' names "
            "a top-level key no disposition covers, which is the one outcome "
            "that should never pass silently."
        ),
        "counts": report.counts,
        "shape_counts": report.shape_counts,
        "sha_counts": report.sha_counts,
        "no_home_fields": report.no_home_counts,
        "unclassified_fields": report.unclassified_counts,
        "entries": [e.to_dict() for e in report.entries],
    }


# Re-exported so callers (and tests) get the artifact-kind vocabulary from one
# place rather than importing half of it from id_migration.
__all__ = [
    "BLOCKING_STATUSES",
    "CENSUS_TABLE_VERSION",
    "CensusReport",
    "DISPOSITION_DERIVED",
    "DISPOSITION_DROPPED",
    "DISPOSITION_ENVELOPE",
    "DISPOSITION_INSTANCE",
    "DISPOSITION_NO_HOME",
    "DISPOSITION_SPEC",
    "Entry",
    "FIELD_DISPOSITION",
    "KIND_APP_SPEC",
    "KIND_INSTANCE",
    "KIND_MANIFEST",
    "KIND_SPEC",
    "SHAPE_LEGACY",
    "SHAPE_VNEXT",
    "STATUS_BLOCKED",
    "STATUS_CLEAN",
    "STATUS_DRAFT",
    "STATUS_PARTIAL",
    "build_report",
    "census_table_path",
]
