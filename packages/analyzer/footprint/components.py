"""footprint.components — the auto-generated output declaration contract.

Spec: docs/spec-footprint-2026-06-18.md (§ "Auto-generated output declaration
contract"). Audit that motivated it: docs/footprint-disk-output-audit-2026-06-28.md.

Why this exists
---------------
Evolve writes a lot of files to two **auto-generated surfaces**:

* ``{shared_dir}/**``                       — the pod-wide arbiter / signals /
                                              watchdog / incidents / alerts store
* ``~/.openclaw/workspace/evolve/**``       — per-bot workspace telemetry
                                              (audit outbox, manifests, …)

The 2026-06-28 disk-output audit found **537 MB** of audit records under
``audit_outbox/_ingested`` that *nothing reads* and *nothing prunes* — a pure
write-only sediment leak. The root cause was structural: nothing required the
producer to declare **who reads the output** or **who prunes it**. A new monitor
can reintroduce exactly that leak tomorrow.

This module closes the gap **by construction**, mirroring the
``signals.protection_registry`` pattern: every component that writes to an
auto-generated surface must declare, *per output*, an
:class:`OutputDeclaration` carrying **both** a :class:`Retention` policy (who
prunes it + the window) **and** a :class:`Consumer` (who reads it + the read
site — or an explicit ``consumer: none`` *with a justification*). A declared
``consumer: none`` with **no retention** is forbidden — that is precisely the
``_ingested`` failure mode.

``tools/footprint-output-lint`` statically finds every file-write site targeting
those two surfaces and FAILS if the writing file is not claimed by a declaration
here — so a new producer cannot ship without a reviewable answer to "who reads
this, and who deletes it?".

Relationship to the F-3 posture dial
------------------------------------
:data:`FOOTPRINT_COMPONENTS` is the **seed** of the larger component registry the
F-3 posture-dial engine will build (spec § "Net F-3 build shape"). F-3 EXTENDS
:class:`FootprintComponent` with the dependency-graph fields
(``kind`` / ``classification`` / ``requires`` / ``required_by`` /
``footprint_dims`` / ``safety_floor`` / ``gate`` / ``unverified``) — declared
here as optional-with-defaults so F-3 can fill them in **without a schema
migration**. The output-declaration shape (``outputs=``) is this PR's durable
contribution; F-3 re-homes these producers under whatever final component ids it
settles on and adds the dependency wiring. Do NOT build a parallel registry —
extend this one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# ── The two auto-generated surfaces the contract governs ─────────────────────
SURFACE_SHARED = "shared_dir"            # {shared_dir}/**
SURFACE_WORKSPACE_EVOLVE = "workspace_evolve"  # ~/.openclaw/workspace/evolve/**

VALID_SURFACES = frozenset({SURFACE_SHARED, SURFACE_WORKSPACE_EVOLVE})

# Writer-file suffixes for an EXTERNAL producer — a write site the lint cannot
# govern because it is not Python (the TypeScript gateway / plugin). The contract
# still accepts such a declaration *for inventory + retention completeness*: the
# output's retention is performed admin-side (evolve owns the files via ACL) even
# though the write happens in the gateway. The file-coverage lint only matches
# Python targets, so an external writer is informational — but it MUST still
# carry the same retention + consumer discipline as a Python output.
_EXTERNAL_WRITER_SUFFIXES = (".ts", ".tsx", ".js", ".mjs")

# Retention windows that mean "this output is never bounded by time". A
# declaration may legitimately use one (active firing signals are kept until they
# go terminal; a per-bot profile is overwritten in place, not accumulated) — but
# it MUST then carry a ``justification`` explaining why unbounded is safe. An
# unjustified unbounded window is the audit's failure mode and the lint blocks it.
UNBOUNDED_WINDOWS = frozenset({"unbounded", "none", "indefinite", ""})


@dataclass(frozen=True)
class Retention:
    """How an output is pruned back to its steady state.

    ``pruner``        — *who* prunes it: a ``"relpath.py:func"`` (the cron/sweep
                        that deletes old records), an ``"overwrite-in-place"``
                        sentinel (the file is rewritten, not accumulated), or a
                        short human description (``"operator / manual"``).
    ``window``        — the retention horizon (``"90d"`` / ``"30d"`` / ``"1y"`` /
                        ``"session"`` / ``"overwrite"`` / ``"unbounded"``).
    ``justification`` — REQUIRED iff ``window`` is unbounded; explains why an
                        unbounded output is nonetheless safe (bounded cardinality,
                        overwritten in place, …). The escape hatch is "explain
                        it", never "leave it undeclared".
    """

    pruner: str
    window: str
    justification: str = ""

    @property
    def is_unbounded(self) -> bool:
        return self.window.strip().lower() in UNBOUNDED_WINDOWS


@dataclass(frozen=True)
class Consumer:
    """Who reads an output (or an explicit, justified "nobody").

    A *named* consumer carries ``reader`` (the reading module / subsystem) and
    ``read_site`` (a ``"relpath.py:func"`` or ``"relpath.py:LNN"`` pointer to the
    actual read). ``none=True`` declares that the output has **no programmatic
    reader** (operator forensics only, debug trail, …) and then REQUIRES a
    ``justification`` *and* a finite :class:`Retention` window on the owning
    output — a no-consumer output that also never prunes is exactly the
    ``_ingested`` sediment the contract exists to prevent, and is forbidden.
    """

    reader: str = ""
    read_site: str = ""
    none: bool = False
    justification: str = ""


def named(reader: str, read_site: str) -> Consumer:
    """A named consumer (the common case)."""
    return Consumer(reader=reader, read_site=read_site)


def no_consumer(justification: str) -> Consumer:
    """An explicit, justified ``consumer: none`` (forensics-only / debug trail).

    The owning :class:`OutputDeclaration` MUST still carry a finite retention
    window — enforced by :func:`output_errors`.
    """
    return Consumer(none=True, justification=justification)


@dataclass(frozen=True)
class OutputDeclaration:
    """One auto-generated output a component writes.

    ``path_glob`` — the output's path, relative to its ``surface`` root
                    (e.g. ``"signals/archived/<id>.json"``,
                    ``"audit_outbox/_ingested/<date>/<id>.json"``).
    ``surface``   — :data:`SURFACE_SHARED` or :data:`SURFACE_WORKSPACE_EVOLVE`.
    ``writer``    — the write site as ``"relpath.py:func"``. The lint's
                    file-coverage check claims the ``relpath.py`` portion; a
                    surface-write in an unclaimed file fails the lint.
    ``cadence``   — when / how often a write happens (human text).
    ``volume_files`` / ``volume_bytes`` — expected **steady-state** volume after
                    retention (human text; the budget monitor — F-5-F4b — turns
                    these into a runtime ceiling).
    ``retention`` — :class:`Retention` (required).
    ``consumer``  — :class:`Consumer` (required; named or justified-none).
    ``note``      — optional free text.
    """

    path_glob: str
    surface: str
    writer: str
    cadence: str
    volume_files: str
    volume_bytes: str
    retention: Retention
    consumer: Consumer
    note: str = ""

    @property
    def writer_file(self) -> str:
        """The repo-relative source file portion of ``writer`` (drops ``:func``)."""
        return self.writer.rsplit(":", 1)[0] if ":" in self.writer else self.writer


@dataclass(frozen=True)
class FootprintComponent:
    """A footprint component + the auto-generated outputs it writes.

    The ``outputs`` field is the #3322 output-declaration contract. The F-3
    posture-graph fields below were declared optional-with-defaults by #3322 so
    F-3 could fill them in without a schema migration; F-3-1a now populates them
    on a superset of graph nodes (see the "F-3 posture-dial dependency graph"
    section). A component is a **graph node** iff ``kind`` is set; the #3322
    output-only entries keep ``kind=""`` and are governed solely by the output
    contract, not by :func:`graph_consistency_errors`.
    """

    id: str
    outputs: tuple[OutputDeclaration, ...] = ()
    summary: str = ""

    # ── F-3 posture-graph fields (populated for graph nodes; "" / () for the
    #    #3322 output-only entries) ──────────────────────────────────────────
    kind: str = ""              # VALID_KINDS: module | tier_capability | infra |
                                # daemon | monitor | applier | runtime
    classification: str = ""    # VALID_CLASSIFICATIONS: infra-floor | cascade |
                                # safe-leaf  (only cascade + safe-leaf are
                                # dial-scoped, FD-7)
    requires: tuple[str, ...] = ()        # single source of truth for edges
    required_by: tuple[str, ...] = ()     # DERIVED (inverted from requires)
    footprint_dims: tuple[str, ...] = ()  # VALID_DIMS subset (FD four-dim model)
    safety_floor: bool = False  # FD-3: kept ON even when dialed down
    gate: str = ""              # where the component is actually read/gated
    unverified: bool = False    # graph §6 soft edge — honored as real (fail-safe)
    note: str = ""


def _c(id: str, *, summary: str, outputs: tuple[OutputDeclaration, ...]) -> tuple[
    str, FootprintComponent
]:
    comp = FootprintComponent(id=id, summary=summary, outputs=tuple(outputs))
    return comp.id, comp


# Common pruner sentinels.
_RETENTION_CRON = "packages/analyzer/signals/retention.py:prune_retention"
_OVERWRITE = "overwrite-in-place"


# ─────────────────────────────────────────────────────────────────────────────
# The registry — backfilled from the 2026-06-28 disk-output audit + the F-1
# catalog. Each entry's outputs were verified against the live write sites; the
# retention pruners + windows come from signals.retention's documented rules and
# the per-producer code. `tools/footprint-output-lint` keeps this in lock-step
# with the code (a surface-write in an unclaimed file fails the lint).
# ─────────────────────────────────────────────────────────────────────────────
FOOTPRINT_COMPONENTS: dict[str, FootprintComponent] = dict(
    [
        # ── Signals store ────────────────────────────────────────────────────
        _c(
            "signals_store",
            summary="The unified observation/alert store. Every monitor writes "
            "Signals here; the admin UI Alerts page + notifier read them.",
            outputs=(
                OutputDeclaration(
                    path_glob="signals/{firing,snoozed,archived}/<id>.json",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/signals/store.py:write_signal",
                    cadence="Once per Signal state change (find-or-create + "
                    "transitions); subdir tracks state.",
                    volume_files="~hundreds active; archived bounded by the 90-day prune",
                    volume_bytes="single-digit MB steady-state",
                    retention=Retention(
                        pruner=_RETENTION_CRON,
                        window="firing/snoozed: until terminal; archived: 90d",
                        justification="Active states (firing/snoozed) are kept until "
                        "they transition to a terminal state — bounded by the number "
                        "of live conditions, not by time; archived/ is time-pruned.",
                    ),
                    consumer=named(
                        "admin Alerts page + signal_notifier + sweep daemons",
                        "packages/admin/evolve_admin/web/routes_alerts.py + "
                        "packages/admin/evolve_admin/alerts/signal_notifier.py",
                    ),
                ),
                OutputDeclaration(
                    path_glob="signals/log/<YYYY-MM-DD>.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/signals/store.py:_append_state_change_log",
                    cadence="One append per Signal state change.",
                    volume_files="1 file/day, 1-year rolling window",
                    volume_bytes="low-MB/year",
                    retention=Retention(pruner=_RETENTION_CRON, window="1y"),
                    consumer=named(
                        "Alerts History tab + external liveness probe (mtime)",
                        "scripts/evolve_liveness_external.py + signals.store readers",
                    ),
                ),
                OutputDeclaration(
                    path_glob="signals/feedback.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/signals/store.py:write_feedback",
                    cadence="One append per rejected-proposal feedback event.",
                    volume_files="1 file, append-only",
                    volume_bytes="KB-scale",
                    retention=Retention(
                        pruner="operator / signal-tuning ingest",
                        window="unbounded",
                        justification="Signal-tuning training input; low-cardinality "
                        "(only rejected proposals append) and KB-scale. Read by the "
                        "signal-tuning path, not time-pruned by design.",
                    ),
                    consumer=named(
                        "signal-tuning / calibration input",
                        "packages/analyzer/signals/store.py:iter_feedback",
                    ),
                ),
            ),
        ),
        # ── Proposals store (arbiter) ────────────────────────────────────────
        _c(
            "proposals_store",
            summary="The RSI arbiter proposal store (pending → applied → archived).",
            outputs=(
                OutputDeclaration(
                    path_glob="proposals/{pending,snoozed,applied,archived}/<id>.json",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/arbiter/store.py:write_proposal",
                    cadence="Once per proposal create / status change.",
                    volume_files="~thousands; archived bounded by the 90-day prune",
                    volume_bytes="single-digit MB steady-state",
                    retention=Retention(
                        pruner=_RETENTION_CRON,
                        window="pending/applied: until terminal; archived: 90d",
                        justification="Non-terminal proposals are bounded by the live "
                        "RSI backlog, not time; archived/ is time-pruned.",
                    ),
                    consumer=named(
                        "admin Proposals UI + applier/verify daemons",
                        "packages/analyzer/arbiter/store.py:iter_proposals",
                    ),
                ),
            ),
        ),
        # ── Watchdog events ──────────────────────────────────────────────────
        _c(
            "watchdog",
            summary="RSI-meta WatchdogEvent JSONL (proposal-volume / rejection-rate "
            "/ calibration drift), mirrored into Signals.",
            outputs=(
                OutputDeclaration(
                    path_glob="watchdog/<YYYY-MM-DD>.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/generators/evolve_watchdog/events.py:write_events",
                    cadence="Appended per generator-runner watchdog tick.",
                    volume_files="1 file/day, 1-year window",
                    volume_bytes="low-MB/year",
                    retention=Retention(pruner=_RETENTION_CRON, window="1y"),
                    consumer=named(
                        "evolve_watchdog→Signal mirror + Alerts History backfill",
                        "packages/analyzer/generators/evolve_watchdog/events.py:read_events",
                    ),
                ),
            ),
        ),
        # ── Observation tuples ───────────────────────────────────────────────
        _c(
            "observations",
            summary="Per-bot (noun×verb×mood×engagement) ObservationTuple JSONL "
            "feeding the RSI profile/analysis layer.",
            outputs=(
                OutputDeclaration(
                    path_glob="observations/<bot_id>/<YYYY-MM-DD>.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/observations/tuples.py:write_tuples",
                    cadence="Appended when content hooks extract tuples (per session).",
                    volume_files="1 file/bot/day",
                    volume_bytes="low-MB/bot/year",
                    retention=Retention(
                        pruner="packages/analyzer/observations/retention.py:prune",
                        window="bounded by profile-window (rolling)",
                        justification="Dedup by source_hash bounds per-day size; the "
                        "profile builder consumes a rolling window. (Retention sweep "
                        "tracked under observations; if absent the budget monitor "
                        "F-5-F4b backstops.)",
                    ),
                    consumer=named(
                        "profile builder + analysis generators",
                        "packages/analyzer/observations/tuples.py:read_tuples",
                    ),
                ),
            ),
        ),
        # ── Profiles ─────────────────────────────────────────────────────────
        _c(
            "profiles",
            summary="Per-bot behavioral profile (YAML frontmatter weights + Markdown).",
            outputs=(
                OutputDeclaration(
                    path_glob="profiles/<bot_id>.md",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/profile/storage.py:save_profile",
                    cadence="Rewritten when the profile is recomputed.",
                    volume_files="1 file/bot (overwritten in place)",
                    volume_bytes="KB-scale/bot",
                    retention=Retention(
                        pruner=_OVERWRITE,
                        window="overwrite",
                        justification="Exactly one file per bot, rewritten in place — "
                        "bounded cardinality, never accumulates.",
                    ),
                    consumer=named(
                        "profile loader (generators, weighting)",
                        "packages/analyzer/profile/storage.py:load_profile",
                    ),
                ),
            ),
        ),
        # ── Calibration snapshots ────────────────────────────────────────────
        _c(
            "calibration",
            summary="Signal / generator / user calibration state.",
            outputs=(
                OutputDeclaration(
                    path_glob="calibration/<name>.json",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/calibration.py:CalibrationLoader._write_local",
                    cadence="Rewritten when calibration recomputes.",
                    volume_files="bounded set of named files (overwritten)",
                    volume_bytes="KB-scale",
                    retention=Retention(
                        pruner=_OVERWRITE,
                        window="overwrite",
                        justification="A fixed, small set of named calibration files, "
                        "each rewritten in place — bounded, never accumulates.",
                    ),
                    consumer=named(
                        "calibration loader (signal/generator weighting)",
                        "packages/analyzer/calibration.py:CalibrationLoader._load_local",
                    ),
                ),
            ),
        ),
        # ── App-audit pipeline (the 537 MB leak the audit found) ─────────────
        _c(
            "audit_pipeline",
            summary="The bot→admin app-audit telemetry pipeline: bots write findings "
            "to their workspace audit_outbox; the admin poller ingests them into "
            "Signals/Proposals and archives the processed records.",
            outputs=(
                # NB: the bot-side ``audit_outbox/<id>.json`` SOURCE records are
                # written by the gateway/skill on the BOT (not in packages/), so the
                # lint cannot govern them; they are drained by the admin poller. The
                # packages-side surfaces below are what the contract governs.
                OutputDeclaration(
                    path_glob="audit_outbox/_ingested/<YYYY-MM-DD>/<record_id>.json",
                    surface=SURFACE_WORKSPACE_EVOLVE,
                    writer="packages/admin/evolve_admin/applications/audit_poller.py:_archive_file",
                    cadence="Every processed outbox record is moved here after ingest.",
                    volume_files="post-F-5-A: deleted on ingest (was 134k+ files / "
                    "537 MB — the audit's dominant leak)",
                    volume_bytes="post-F-5-A: ~0 (was 537 MB)",
                    retention=Retention(
                        pruner="packages/admin/evolve_admin/applications/audit_poller.py:_archive_file (F-5-A: delete-on-ingest)",
                        window="14d (debug-retain path only; default delete)",
                        justification="Ingest is idempotent + signature-deduped, so a "
                        "processed record has no production reader (the F-5-A source-cut "
                        "deletes on ingest; an opt-in debug-retain path keeps 14 days). "
                        "Declared as the canonical example of the contract this lint "
                        "enforces — a tombstone archive MUST prune.",
                    ),
                    consumer=no_consumer(
                        "Forensic tombstone only — ingest is idempotent and "
                        "signature-deduped, so re-processing a record is a no-op. Zero "
                        "production readers (only tests read _ingested). The F-5-A "
                        "source-cut deletes on ingest; the 14-day debug-retain path is "
                        "opt-in for local debugging."
                    ),
                ),
                OutputDeclaration(
                    path_glob="audit_inbox/<request_id>.json",
                    surface=SURFACE_WORKSPACE_EVOLVE,
                    writer="packages/admin/evolve_admin/applications/audit_dispatch.py:_write_inbox_file",
                    cadence="Admin writes a re-audit request the bot picks up.",
                    volume_files="transient — drained by the bot",
                    volume_bytes="transient",
                    retention=Retention(
                        pruner="bot-side audit runner (consumes the request)",
                        window="drained-on-pickup",
                        justification="Request queue the bot drains; not accumulated.",
                    ),
                    consumer=named(
                        "bot-side audit runner",
                        "packages/admin/evolve_admin/applications/audit_dispatch.py:_kick_runner",
                    ),
                ),
                OutputDeclaration(
                    path_glob="infra_audit_outbox/<record_id>.json",
                    surface=SURFACE_SHARED,
                    writer="packages/admin/evolve_admin/applications/infra_audit.py:_write_outbox_record",
                    cadence="Per infra-audit finding.",
                    volume_files="transient — drained by the poller",
                    volume_bytes="transient",
                    retention=Retention(
                        pruner="packages/admin/evolve_admin/applications/audit_poller.py:run_infra_audit_poll",
                        window="drained-on-ingest",
                        justification="Pod-level sibling of audit_outbox; the poller "
                        "drains it into Signals/Proposals each tick.",
                    ),
                    consumer=named(
                        "admin audit poller (→ Signals/Proposals)",
                        "packages/admin/evolve_admin/applications/audit_poller.py:run_infra_audit_poll",
                    ),
                ),
                OutputDeclaration(
                    path_glob="infra_audit_inbox/<request_id>.json",
                    surface=SURFACE_SHARED,
                    writer="packages/admin/evolve_admin/applications/infra_audit.py:request_infra_audit",
                    cadence="Admin queues an infra re-audit request.",
                    volume_files="transient — drained by the infra runner",
                    volume_bytes="transient",
                    retention=Retention(
                        pruner="infra audit runner (consumes the request)",
                        window="drained-on-pickup",
                        justification="Request queue drained by the runner; not "
                        "accumulated.",
                    ),
                    consumer=named(
                        "infra audit runner",
                        "packages/admin/evolve_admin/applications/infra_audit.py:request_infra_audit",
                    ),
                ),
                OutputDeclaration(
                    path_glob="audits/<app_id>/trail.jsonl",
                    surface=SURFACE_WORKSPACE_EVOLVE,
                    writer="packages/analyzer/app_audit_runner.py:_append_trail",
                    cadence="Tier-2 (~6h): one audit_run summary line per "
                    "non-dormant app + one tier2_finding line per NEW-or-CHANGED "
                    "finding (source-cut 2026-06-28; was per-finding every run).",
                    volume_files="1 trail.jsonl/app, soft-capped to 500 lines",
                    volume_bytes="bounded by the soft-cap (was 1994 lines / one "
                    "signature re-emitted 1065× pre-cut)",
                    retention=Retention(
                        pruner="packages/analyzer/app_audit_runner.py:_cap_trail",
                        window="soft-cap (>1000 lines → keep most-recent 500)",
                        justification="Rolling local audit log; every reader is "
                        "bounded-tail (UI trail modal, evo tray, Tier-3 LLM, "
                        "compare-to-N-days) so the most-recent 500 lines preserve "
                        "every consumer. Mirrors the investigations/ trail cap.",
                    ),
                    consumer=named(
                        "admin UI trail modal + evo tray + Tier-3 LLM context",
                        "packages/admin/evolve_admin/web/server.py (trail modal) + "
                        "packages/analyzer/app_audit_tier3.py",
                    ),
                ),
                OutputDeclaration(
                    path_glob="<infra-audit element>/trail.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/admin/evolve_admin/applications/infra_audit.py:_append_trail",
                    cadence="Append per infra-audit element outcome + one audit_run "
                    "roll-up per touched element each run.",
                    volume_files="1 trail.jsonl/element, soft-capped to 500 lines",
                    volume_bytes="bounded by the soft-cap",
                    retention=Retention(
                        pruner="packages/admin/evolve_admin/applications/infra_audit.py:_cap_trail",
                        window="soft-cap (>1000 lines → keep most-recent 500)",
                        justification="Per-element infra-audit trail, bounded by the "
                        "fixed set of infra elements AND soft-capped per element; "
                        "readers are bounded-tail.",
                    ),
                    consumer=named(
                        "evo infra-audit handler + admin UI",
                        "packages/admin/evolve_admin/evo/handlers/infra_audit.py",
                    ),
                ),
            ),
        ),
        # ── Incidents (heal + repo-puller) ───────────────────────────────────
        _c(
            "incidents",
            summary="Heal daily incident records + repo-puller pull-failure incidents.",
            outputs=(
                OutputDeclaration(
                    path_glob="incidents/<YYYY-MM-DD>/<bot_id>-<ts>.json",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/heal.py:_record_incident",
                    cadence="One file per heal-detected incident (gateway down, "
                    "restart-failed, config-drift).",
                    volume_files="day-dirs, 30-day window",
                    volume_bytes="low-MB",
                    retention=Retention(pruner=_RETENTION_CRON, window="30d"),
                    consumer=named(
                        "gateway_diagnostician + heal recurrence logic (7-day window)",
                        "packages/analyzer/heal.py + gateway_diagnostician",
                    ),
                ),
                OutputDeclaration(
                    path_glob="repo-puller/incidents/<id>.json",
                    surface=SURFACE_SHARED,
                    writer="packages/admin/evolve_admin/repo_puller.py:_write_new_puller_stuck_issue",
                    cadence="One file per repo-pull failure (ff-only wedge, untracked "
                    "conflict).",
                    volume_files="bounded; pull failures are rare",
                    volume_bytes="KB-scale",
                    retention=Retention(
                        pruner="packages/admin/evolve_admin/repo_puller.py (rolling id cap)",
                        window="rolling per-day id sequence",
                        justification="Pull failures are rare events; the id allocator "
                        "rolls per day and old incidents are pruned with the puller's "
                        "own housekeeping.",
                    ),
                    consumer=named(
                        "admin health page (repo-puller status)",
                        "packages/admin/evolve_admin/health.py",
                    ),
                ),
            ),
        ),
        # ── Alert dispatcher logs ────────────────────────────────────────────
        _c(
            "alerts_log",
            summary="Date-partitioned dispatcher delivery logs + digest pending queues.",
            outputs=(
                OutputDeclaration(
                    path_glob="alerts/{dispatcher,dispatcher-suppressed,delivery-failures}/<YYYY-MM-DD>.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/admin/evolve_admin/alerts/dispatcher.py:_append_log",
                    cadence="One append per dispatch decision.",
                    volume_files="3 streams × 1 file/day, 30-day window",
                    volume_bytes="bounded by 30d (was 14 MB/day unrotated — 2026-06-01 fix)",
                    retention=Retention(pruner=_RETENTION_CRON, window="30d"),
                    consumer=named(
                        "Reports → Subscriptions → Messages (recent ~100 records)",
                        "packages/admin/evolve_admin/web/routes_alerts.py",
                    ),
                ),
                OutputDeclaration(
                    path_glob="alerts/digest-pending/<frequency>.jsonl + <frequency>.flushed-<iso>",
                    surface=SURFACE_SHARED,
                    writer="packages/admin/evolve_admin/alerts/digest_dispatcher.py:flush",
                    cadence="Appended as items defer to digest; flushed on the digest "
                    "cadence.",
                    volume_files="small pending queue + dated flush markers",
                    volume_bytes="KB-scale",
                    retention=Retention(pruner=_RETENTION_CRON, window="30d"),
                    consumer=named(
                        "digest dispatcher (bundles + sends the digest)",
                        "packages/admin/evolve_admin/alerts/digest_dispatcher.py",
                    ),
                ),
            ),
        ),
        # ── App manifests ────────────────────────────────────────────────────
        _c(
            "manifests",
            summary="Per-app manifests written into the bot workspace.",
            outputs=(
                OutputDeclaration(
                    path_glob="workspace/manifests/<app_id>.json",
                    surface=SURFACE_WORKSPACE_EVOLVE,
                    writer="packages/admin/evolve_admin/applications/manifest.py:_write_manifest_bytes",
                    cadence="Rewritten when an app's manifest is materialized.",
                    volume_files="1 file/app (overwritten in place)",
                    volume_bytes="KB-scale/app",
                    retention=Retention(
                        pruner=_OVERWRITE,
                        window="overwrite (removed with the app)",
                        justification="One file per app, rewritten in place; bounded by "
                        "the app count and removed on app deletion.",
                    ),
                    consumer=named(
                        "gallery / scanner / app lifecycle + the bot itself",
                        "packages/admin/evolve_admin/applications/manifest.py:read_manifest",
                    ),
                ),
            ),
        ),
        # ── Admin engine queues (workspace) ──────────────────────────────────
        _c(
            "admin_tasks",
            summary="The better-engine pending-admin-task queue + recommendation hints.",
            outputs=(
                OutputDeclaration(
                    path_glob="workspace/evolve/pending-admin-tasks.json",
                    surface=SURFACE_WORKSPACE_EVOLVE,
                    writer="packages/admin/evolve_admin/better_engine/pending_tasks.py:save_pending_tasks",
                    cadence="Rewritten when the pending-task set changes.",
                    volume_files="1 file (overwritten in place)",
                    volume_bytes="KB-scale",
                    retention=Retention(
                        pruner=_OVERWRITE,
                        window="overwrite",
                        justification="Single rewritten queue file; bounded by the live "
                        "pending-task set.",
                    ),
                    consumer=named(
                        "better-engine admin-task runner",
                        "packages/admin/evolve_admin/better_engine/pending_tasks.py:load_pending_tasks",
                    ),
                ),
                OutputDeclaration(
                    path_glob="workspace/evolve/rec-hints.json",
                    surface=SURFACE_WORKSPACE_EVOLVE,
                    writer="packages/admin/evolve_admin/better_engine/hints.py:write_hints_for_bot",
                    cadence="Rewritten when recommendation hints recompute.",
                    volume_files="1 file (overwritten in place)",
                    volume_bytes="KB-scale",
                    retention=Retention(
                        pruner=_OVERWRITE,
                        window="overwrite",
                        justification="Single rewritten hints file the bot reads; "
                        "bounded.",
                    ),
                    consumer=named(
                        "bot-side recommendation surface",
                        "packages/admin/evolve_admin/better_engine/hints.py:read_hints",
                    ),
                ),
            ),
        ),
        # ── Anthropic admin-API ingest log ───────────────────────────────────
        _c(
            "anthropic_ingest",
            summary="Admin-API cost/usage ingest audit log.",
            outputs=(
                OutputDeclaration(
                    path_glob="anthropic_api/audit_logs/<date>.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/admin/evolve_admin/anthropic_admin_ingest.py:write_audit_log_snapshot",
                    cadence="One append per admin-API ingest run.",
                    volume_files="1 file/date",
                    volume_bytes="low-MB/year",
                    retention=Retention(
                        pruner="packages/admin/evolve_admin/anthropic_admin_ingest.py (date-window)",
                        window="bounded date window",
                        justification="Ingest-reconciliation audit trail; date-keyed "
                        "and bounded by the ingest cadence.",
                    ),
                    consumer=named(
                        "cost reconciliation (ledger ≠ Anthropic-total check)",
                        "packages/admin/evolve_admin/anthropic_admin_ingest.py",
                    ),
                ),
            ),
        ),
        # ── Recovery pause-log ───────────────────────────────────────────────
        _c(
            "recovery",
            summary="The deploy/apply recovery pause-log.",
            outputs=(
                OutputDeclaration(
                    path_glob="recovery/pause-log.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/admin/evolve_admin/recovery.py:_append_jsonl",
                    cadence="One append per pause/resume recovery event.",
                    volume_files="1 file, append-only",
                    volume_bytes="KB-scale",
                    retention=Retention(
                        pruner="operator / recovery housekeeping",
                        window="unbounded",
                        justification="Rare recovery events (pause/resume); KB-scale "
                        "and operator-forensic. Low cardinality by construction.",
                    ),
                    consumer=named(
                        "recovery state machine + operator forensics",
                        "packages/admin/evolve_admin/recovery.py:read_pause_state",
                    ),
                ),
            ),
        ),
        # ── Admin/provisioning audit log ─────────────────────────────────────
        _c(
            "admin_audit_log",
            summary="The admin/wizard config-change audit log (every config write).",
            outputs=(
                OutputDeclaration(
                    path_glob="audit-log.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/admin/evolve_admin/provisioning.py:_record_audit",
                    cadence="One append per admin/wizard config-change action.",
                    volume_files="1 file, append-only",
                    volume_bytes="bounded by the log-cap cron (size-capped)",
                    retention=Retention(
                        pruner="packages/analyzer/log_cap.py:cap_logs",
                        window="size-capped (daily log-cap cron)",
                    ),
                    consumer=named(
                        "heal config-drift credit + admin UI audit view",
                        "packages/analyzer/heal.py:_credit_authorized_changes "
                        "(reads audit-log.jsonl) + web/routes_shared.py",
                    ),
                ),
            ),
        ),
        # ── Roster / directory audit logs ────────────────────────────────────
        _c(
            "roster_audit_log",
            summary="Per-day audit logs for roster-overlay + user-directory mutations.",
            outputs=(
                OutputDeclaration(
                    path_glob="rosters/log/<YYYY-MM-DD>.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/admin/evolve_admin/roster_overlay.py:_audit_log_append",
                    cadence="One append per roster-overlay mutation.",
                    volume_files="1 file/day",
                    volume_bytes="KB-scale/day",
                    retention=Retention(
                        pruner="operator / roster housekeeping",
                        window="unbounded",
                        justification="Low-cardinality mutation audit (roster edits "
                        "are infrequent operator actions); KB-scale/day. Forensic "
                        "trail; not time-pruned today. Budget monitor (F-5-F4b) "
                        "backstops if it grows.",
                    ),
                    consumer=named(
                        "operator forensics / roster change history",
                        "packages/admin/evolve_admin/roster_overlay.py",
                    ),
                ),
                OutputDeclaration(
                    path_glob="directory/log/<YYYY-MM-DD>.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/admin/evolve_admin/user_directory/storage.py:_audit_log_append",
                    cadence="One append per user-directory mutation.",
                    volume_files="1 file/day",
                    volume_bytes="KB-scale/day",
                    retention=Retention(
                        pruner="operator / directory housekeeping",
                        window="unbounded",
                        justification="Low-cardinality mutation audit (directory edits "
                        "are infrequent); KB-scale/day. Forensic trail; budget monitor "
                        "(F-5-F4b) backstops.",
                    ),
                    consumer=named(
                        "operator forensics / directory change history",
                        "packages/admin/evolve_admin/user_directory/storage.py",
                    ),
                ),
            ),
        ),
        # ── Audit WARN log ───────────────────────────────────────────────────
        _c(
            "audit_warn_log",
            summary="audit.py WARN-finding JSONL surfaced in the weekly pod report.",
            outputs=(
                OutputDeclaration(
                    path_glob="logs/audit-warns.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/audit.py:_send_warn_log",
                    cadence="One append per WARN-tier audit finding.",
                    volume_files="1 file, append-only",
                    volume_bytes="size-capped",
                    retention=Retention(
                        pruner="packages/analyzer/log_cap.py:cap_logs",
                        window="size-capped (daily log-cap cron)",
                    ),
                    consumer=named(
                        "pod report (renders the audit-warn summary)",
                        "packages/analyzer/pod_report.py",
                    ),
                ),
            ),
        ),
        # ── Outcome feedback (the calibration dataset) ───────────────────────
        _c(
            "outcome_feedback",
            summary="The 'did this proposal help?' outcome dataset that calibration "
            "trains on.",
            outputs=(
                OutputDeclaration(
                    path_glob="feedback/outcomes.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/outcome.py:write_outcome_result",
                    cadence="One append per resolved outcome (👍/👎/expired).",
                    volume_files="1 file, append-only",
                    volume_bytes="low-MB (one record per applied proposal)",
                    retention=Retention(
                        pruner="operator / calibration retention",
                        window="unbounded",
                        justification="The calibration training dataset — value grows "
                        "with history and cardinality is bounded by applied-proposal "
                        "count (low). Deliberately retained as training input.",
                    ),
                    consumer=named(
                        "calibration / outcome expiry reader",
                        "packages/analyzer/outcome.py:expire_pending_outcomes",
                    ),
                ),
                OutputDeclaration(
                    path_glob="feedback/pending-outcomes.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/outcome.py:write_pending_outcome",
                    cadence="Appended on apply; rewritten (compacted) by the daily "
                    "outcome job.",
                    volume_files="1 file; compacted in place",
                    volume_bytes="KB-scale (only in-flight outcomes)",
                    retention=Retention(
                        pruner="packages/analyzer/outcome.py:rewrite_pending_outcomes",
                        window="compacted (in-flight only)",
                        justification="Holds only outcomes awaiting their 7-day "
                        "check-in; the daily job rewrites it dropping resolved ones, so "
                        "it is bounded by the in-flight set.",
                    ),
                    consumer=named(
                        "daily outcome check-in job",
                        "packages/analyzer/outcome.py:rewrite_pending_outcomes",
                    ),
                ),
            ),
        ),
        # ── RSI recommendation profile (per bot) ─────────────────────────────
        _c(
            "recommendation_profile",
            summary="Per-bot RSI recommendation profile.json.",
            outputs=(
                OutputDeclaration(
                    path_glob="<bot_id>/recommendations/profile.json",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/profile_builder.py:write_profile",
                    cadence="Rewritten when the recommendation profile recomputes.",
                    volume_files="1 file/bot (overwritten in place)",
                    volume_bytes="KB-scale/bot",
                    retention=Retention(
                        pruner=_OVERWRITE,
                        window="overwrite",
                        justification="One profile file per bot, rewritten in place; "
                        "bounded cardinality, never accumulates.",
                    ),
                    consumer=named(
                        "recommendation surface / RSI pipeline",
                        "packages/analyzer/profile_builder.py:load_profile",
                    ),
                ),
            ),
        ),
        # ── RSI recommendation event log (per bot) ───────────────────────────
        _c(
            "recommendations_log",
            summary="Per-bot append-only audit history of recommendation events "
            "(created/updated/refreshed/expired/status_change).",
            outputs=(
                OutputDeclaration(
                    path_glob="<bot_id>/recommendations/log.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/recommendations.py:_append_log",
                    cadence="One append per recommendation lifecycle event "
                    "(diff-gated: a stable rec does not re-emit 'updated').",
                    volume_files="1 file/bot; 90-day rolling line window",
                    volume_bytes="KB-scale/bot after the line-age cap",
                    retention=Retention(
                        pruner="packages/analyzer/recommendations.py:_prune_log",
                        window="90d",
                    ),
                    consumer=no_consumer(
                        "Audit/forensic history only — production reads the active "
                        "set from current.json, never the event log. Bounded by the "
                        "90-day line-age cap (matches DEFAULT_EXPIRY_DAYS)."
                    ),
                ),
            ),
        ),
        # ── Per-user profile (workspace) ─────────────────────────────────────
        _c(
            "user_profiles",
            summary="Per-user profile markdown in the bot workspace.",
            outputs=(
                OutputDeclaration(
                    path_glob="<user_key> profile.md",
                    surface=SURFACE_WORKSPACE_EVOLVE,
                    writer="packages/analyzer/user_profile/storage.py:save_profile",
                    cadence="Rewritten when a user profile updates.",
                    volume_files="1 file/user (overwritten in place)",
                    volume_bytes="KB-scale/user",
                    retention=Retention(
                        pruner=_OVERWRITE,
                        window="overwrite",
                        justification="One file per user, rewritten in place; bounded "
                        "by the user count.",
                    ),
                    consumer=named(
                        "user-profile loader (per-user context)",
                        "packages/analyzer/user_profile/storage.py:load_profile",
                    ),
                ),
            ),
        ),
        # ── Turn annotations (TS gateway → admin-side prune) ─────────────────
        _c(
            "turn_annotations",
            summary="Per-turn annotation JSONL the TS gateway's TurnObserver mints; "
            "the cost/metrics layer reads a trailing window of it.",
            outputs=(
                OutputDeclaration(
                    path_glob="annotations/<bot_id>/<YYYY-MM-DD>.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/plugin/src/observer/TurnObserver.ts:TurnObserver",
                    cadence="One append per agent turn (external TS producer).",
                    volume_files="1 file/bot/day; 90-day window after the prune",
                    volume_bytes="low-MB/bot (19 MB pod-wide pre-prune, 2026-06-28)",
                    retention=Retention(
                        pruner=_RETENTION_CRON,
                        window="90d",
                        justification="",
                    ),
                    consumer=named(
                        "cost/metrics readers (trailing window, ≤7d lookback)",
                        "packages/analyzer/measure.py:load_annotations + "
                        "packages/analyzer/cost_ledger.py",
                    ),
                    note="External (TypeScript) writer — the lint cannot govern the "
                    "write site; the 90-day prune is admin-side (evolve owns the "
                    "files via ACL) and rides the daily retention cron.",
                ),
            ),
        ),
        # ── Proposal-synthesizer decision log ────────────────────────────────
        _c(
            "synthesis_log",
            summary="The proposal-synthesizer's per-run decision log.",
            outputs=(
                OutputDeclaration(
                    path_glob="candidates/synthesis_log/<YYYY-MM-DD>.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/proposal_synthesizer/synthesizer.py:_append_synthesis_log",
                    cadence="One append per synthesizer run.",
                    volume_files="1 file/day; 30-day window after the prune",
                    volume_bytes="low-MB/year (bounded by the 30-day prune)",
                    retention=Retention(pruner=_RETENTION_CRON, window="30d"),
                    consumer=no_consumer(
                        "Per-run synthesizer decision/debug log — operator forensics "
                        "only. No production reader: only tests reference "
                        "store.py:synthesis_log_path. Bounded by the 30-day prune."
                    ),
                ),
            ),
        ),
        # ── Gate-dropped candidates log ──────────────────────────────────────
        _c(
            "candidates_dropped",
            summary="The proposal gate's 'what almost surfaced' drop log — one "
            "record per dropped candidate.",
            outputs=(
                OutputDeclaration(
                    path_glob="candidates/dropped/<YYYY-MM-DD>.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/proposal_synthesizer/store.py:record_drop",
                    cadence="One append per gate-dropped candidate.",
                    volume_files="1 file/day; 30-day window after the prune",
                    volume_bytes="low-MB/year (bounded by the 30-day prune)",
                    retention=Retention(pruner=_RETENTION_CRON, window="30d"),
                    consumer=named(
                        "Reports → gate-dropped candidates view (last N, days≤30)",
                        "packages/admin/evolve_admin/web/server.py:api_candidates_dropped",
                    ),
                    note="The reader hard-clamps its lookback to days≤30, so records "
                    "older than 30 days are unreadable — the prune matches that "
                    "ceiling instead of accumulating sediment below it.",
                ),
            ),
        ),
        # ── Cascade labeled outcomes ─────────────────────────────────────────
        _c(
            "cascade_labels",
            summary="Tier-cascade labeled outcomes (per session/day) — training "
            "input for the (not-yet-built) cascade tuner.",
            outputs=(
                OutputDeclaration(
                    path_glob="cascade/labels/<YYYY-MM-DD>.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/cascade/labeler.py:write_labels",
                    cadence="Rewritten each labeler run; dedup-on-write keyed by "
                    "session_id (one row per session/day).",
                    volume_files="1 file/day; 90-day window after the prune",
                    volume_bytes="low-MB (dedup-on-write keeps each day small)",
                    retention=Retention(pruner=_RETENTION_CRON, window="90d"),
                    consumer=named(
                        "cascade status (today/yesterday line counts) + future tuner",
                        "packages/admin/evolve_admin/web/routes_cascade.py:_count_labels",
                    ),
                ),
            ),
        ),
        # ── App-posture per-week log copies (per bot) ────────────────────────
        _c(
            "app_posture_log",
            summary="Per-week copies of each bot's app-posture document — an audit "
            "trail beside the canonical app_posture/<bot>.md.",
            outputs=(
                OutputDeclaration(
                    path_glob="app_posture/<bot_id>/log/<YYYY-MM-DD>.md",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/app_posture_review.py:write_posture",
                    cadence="One log copy per posture-review run (weekly).",
                    volume_files="newest-12 kept per bot",
                    volume_bytes="KB-scale/bot (≤12 docs)",
                    retention=Retention(
                        pruner="packages/analyzer/app_posture_review.py:_prune_posture_log",
                        window="keep-newest-12",
                    ),
                    consumer=no_consumer(
                        "No programmatic reader — session_surface reads only the "
                        "canonical app_posture/<bot>.md (overwritten in place), not "
                        "the per-week LOG copy. Kept newest-12 for operator/dev "
                        "forensics."
                    ),
                ),
            ),
        ),
        # ── Fit-review decision trail (per bot) ──────────────────────────────
        _c(
            "fit_review_trail",
            summary="Per-bot fit-review decision trail — one record per run "
            "(including the non-emitting ones), for observability.",
            outputs=(
                OutputDeclaration(
                    path_glob="evolve/fit_review/trail.jsonl",
                    surface=SURFACE_WORKSPACE_EVOLVE,
                    writer="packages/analyzer/fit_review/runner.py:_append_trail",
                    cadence="One append per fit-review run.",
                    volume_files="1 file/bot; soft line-cap 1000→500",
                    volume_bytes="KB-scale/bot after the cap",
                    retention=Retention(
                        pruner="packages/analyzer/fit_review/runner.py:_cap_trail",
                        window="line-cap 1000→500",
                    ),
                    consumer=no_consumer(
                        "Pure per-run observability — no reader. The Bite-4 poller "
                        "drains the shared-dir outbox/, never the bot-local trail. "
                        "Bounded by the soft line-cap (mirrors _prune_investigations)."
                    ),
                ),
            ),
        ),
        # ── Weekly RSI review documents ──────────────────────────────────────
        _c(
            "weekly_reviews",
            summary="The weekly RSI process-health report, one Markdown doc per week.",
            outputs=(
                OutputDeclaration(
                    path_glob="reviews/<YYYY-MM-DD>.md",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/weekly_review.py:write_report",
                    cadence="One doc per weekly review run.",
                    volume_files="newest-12 kept (~a quarter of history)",
                    volume_bytes="KB-scale (≤12 docs)",
                    retention=Retention(
                        pruner="packages/analyzer/weekly_review.py:_prune_old_reviews",
                        window="keep-newest-12",
                    ),
                    consumer=no_consumer(
                        "No programmatic reader — the report is delivered to operators "
                        "via the dispatcher (send_report), not read back from disk. The "
                        "/api/reviews/latest route that once served the .md was removed "
                        "(dead SPA loader). Kept newest-12 for operator file-browsing."
                    ),
                ),
            ),
        ),
        # ── Circuit-breaker runner decision log ──────────────────────────────
        _c(
            "breakers_runner_log",
            summary="The activity-shape detector's per-cycle 'what would have "
            "tripped' decision log + its change-detection sidecar.",
            outputs=(
                OutputDeclaration(
                    path_glob="breakers/runner-log/<YYYY-MM-DD>.jsonl",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/breakers/runner.py:_append_runner_log",
                    cadence="Per (bot, cycle) the breakers-runner LaunchDaemon "
                    "evaluates (~every 10 min) — but post-F-5-A only when the "
                    "decision is ACTIONABLE (a trip is recommended) or CHANGED "
                    "vs the bot's prior cycle; the steady-state observe-only "
                    "no-op is suppressed at the source.",
                    volume_files="1 file/day, 14-day window",
                    volume_bytes="post-F-5-A: low-KB/day (was ~1 MB/day / 15 MB "
                    "unpruned — the no-op re-emission was the bulk)",
                    retention=Retention(
                        pruner=_RETENTION_CRON,
                        window="14d",
                    ),
                    consumer=no_consumer(
                        "Calibration-soak forensics only. No production reader: "
                        "the breakers backtest/calibration path reads the turns "
                        "corpus (breakers.backtest.read_turns), not this log, and "
                        "heal.py reaps only breakers/<scope>/full.json. The F-5-A "
                        "source-cut suppresses the no-op bulk; the F-5-B 14-day "
                        "retention floor bounds what remains."
                    ),
                ),
                OutputDeclaration(
                    path_glob="breakers/runner-log/.last-decision.json",
                    surface=SURFACE_SHARED,
                    writer="packages/analyzer/breakers/runner.py:_write_last_decisions",
                    cadence="Rewritten once per cycle with each bot's current "
                    "decision signature.",
                    volume_files="1 dotfile (overwritten in place)",
                    volume_bytes="KB-scale (one short signature per bot)",
                    retention=Retention(
                        pruner=_OVERWRITE,
                        window="overwrite",
                        justification="A single sidecar rewritten in place each "
                        "cycle — bounded by the bot count, never accumulates. "
                        "Powers the F-5-A change-detection that suppresses the "
                        "runner-log no-op re-emission.",
                    ),
                    consumer=named(
                        "the runner's own next-cycle change detection",
                        "packages/analyzer/breakers/runner.py:_read_last_decisions",
                    ),
                ),
            ),
        ),
    ]
)


# ═════════════════════════════════════════════════════════════════════════════
# F-3 posture-dial dependency graph
# ─────────────────────────────────────────────────────────────────────────────
# Spec: docs/spec-footprint-2026-06-18.md § "FD-5 engine design" (part 1, the
# component graph). Authoritative edge list + classification: the VERIFIED-from-
# code graph in docs/footprint-dependency-graph-2026-06-18.md (the F-2.5 audit) —
# its §1 component table, §3 hazard table (H1-H4), §4 infra floor, §5 cascade
# groups (CG-1..CG-5), and §6 soft-edge list. Module catalog: evolve_config.py
# DEFAULT_MODULES. Tier ladder: packages/plugin/src/config.ts TIERS.
#
# The graph is a SUPERSET of DEFAULT_MODULES: it also carries the tier-ladder
# capabilities (observer / modelRouting / injectKeywords / injectPodConduct /
# preflight — real dial targets that are NOT module keys) and the infra-floor
# items (kept in the registry for the guard even though FD-7 excludes them from
# the dial). Each node is one FootprintComponent with kind/classification/
# requires set; required_by is DERIVED below.
#
# DATA ONLY — this bite (F-3-1a) builds the registry + a load-validator. Posture
# storage/resolution (1b), the cascade/apply engine (2), CLI/UI (3) come later.
#
# ── Fail-safe semantics (spec FD-5; chip F-3-1a) ─────────────────────────────
# Any edge the F-2.5 doc could NOT verify from code (§6) is marked on its node
# with unverified=True. Fail-safe = fail-toward-NOT-disabling: an unverified
# `requires` edge is **honored as a real edge** — it is never skipped in
# target-existence / reciprocity / cycle checks, and it still produces a
# required_by link, so the (future) cascade engine treats the dependent as real
# and never silently drops it. unverified is advisory provenance for the engine,
# NEVER a license to ignore the edge. See honored_requires().
# ═════════════════════════════════════════════════════════════════════════════

# kind — what a node is (drives how the engine reads/writes its gate).
KIND_MODULE = "module"                  # a DEFAULT_MODULES key
KIND_TIER_CAPABILITY = "tier_capability"  # an openclaw.json::tier capability flag
KIND_INFRA = "infra"                    # control-plane / apply-mechanism floor
KIND_DAEMON = "daemon"                  # a standalone launchd/systemd job
KIND_MONITOR = "monitor"                # a Signal-producing monitor
KIND_APPLIER = "applier"               # a config/behavior mutation enforcer
KIND_RUNTIME = "runtime"                # gateway hot-path artifact (not tier-gated)
VALID_KINDS = frozenset({
    KIND_MODULE, KIND_TIER_CAPABILITY, KIND_INFRA, KIND_DAEMON,
    KIND_MONITOR, KIND_APPLIER, KIND_RUNTIME,
})

# classification — exactly one per node (graph doc §"Classification").
CLASS_INFRA_FLOOR = "infra-floor"  # control plane / apply mechanism — OUT of the dial (FD-7)
CLASS_CASCADE = "cascade"          # has dependents — disable cascades-with-consent
CLASS_SAFE_LEAF = "safe-leaf"      # nothing depends on it — flip alone
VALID_CLASSIFICATIONS = frozenset({
    CLASS_INFRA_FLOOR, CLASS_CASCADE, CLASS_SAFE_LEAF,
})
# FD-7: the v1 dial domain is cascade + safe-leaf ONLY; the infra floor is excluded.
DIAL_SCOPED_CLASSIFICATIONS = frozenset({CLASS_CASCADE, CLASS_SAFE_LEAF})

# footprint_dims — the four-dimension invasiveness model (catalog § "four
# footprint dimensions"): Mutation / Runtime / Cost / Privilege.
DIM_MUTATION = "mutation"
DIM_RUNTIME = "runtime"
DIM_COST = "cost"
DIM_PRIVILEGE = "privilege"
VALID_DIMS = frozenset({DIM_MUTATION, DIM_RUNTIME, DIM_COST, DIM_PRIVILEGE})


def _node(
    id: str,
    *,
    kind: str,
    classification: str,
    summary: str,
    footprint_dims: tuple[str, ...],
    requires: tuple[str, ...] = (),
    safety_floor: bool = False,
    gate: str = "",
    unverified: bool = False,
    note: str = "",
) -> FootprintComponent:
    """Build one posture-graph node. ``required_by`` is derived, never authored."""
    return FootprintComponent(
        id=id,
        kind=kind,
        classification=classification,
        summary=summary,
        footprint_dims=footprint_dims,
        requires=requires,
        safety_floor=safety_floor,
        gate=gate,
        unverified=unverified,
        note=note,
    )


# ─────────────────────────────────────────────────────────────────────────────
# The graph nodes. Each row maps to a graph-doc §1 row; `requires` is transcribed
# from that row's requires[] column (the single source of truth — required_by is
# derived). Citations live in `gate`. Soft edges (§6) carry unverified=True.
# ─────────────────────────────────────────────────────────────────────────────
_GRAPH_NODES: tuple[FootprintComponent, ...] = (
    # ── §1a Infra floor (kind=infra, classification=infra-floor; OUT of dial) ──
    _node(
        "sudoers",
        kind=KIND_INFRA, classification=CLASS_INFRA_FLOOR,
        summary="The /etc/sudoers.d/evolve grant set — the apply mechanism "
        "(cp/chown/chmod/kickstart) depends on it. Lockout hazard H1.",
        footprint_dims=(DIM_PRIVILEGE,),
        gate="packages/admin/evolve_admin/setup_wizard.py:_render_evolve_sudoers",
    ),
    _node(
        "openclaw_acls",
        kind=KIND_INFRA, classification=CLASS_INFRA_FLOOR,
        summary="macOS .openclaw/ read ACLs (set_evolve_read_acl). Config reads "
        "on the apply critical path fall back only to `sudo cat`. Lockout H1.",
        footprint_dims=(DIM_PRIVILEGE,),
        gate="packages/admin/evolve_admin/deploy.py:set_evolve_read_acl",
    ),
    _node(
        "deploy_checkout",
        kind=KIND_INFRA, classification=CLASS_INFRA_FLOOR,
        summary="The managed deploy checkout (platform_profile.deploy_checkout_"
        "default) every daemon loads from; the repo-puller's target.",
        footprint_dims=(DIM_PRIVILEGE,),
        gate="platform_profile.deploy_checkout_default; loaded by all daemons",
    ),
    _node(
        "admin_daemon",
        kind=KIND_INFRA, classification=CLASS_INFRA_FLOOR,
        summary="The ai.evolve.evolve.admin-ui control plane + the UI re-enable "
        "path (the module enable/disable endpoints). Off ⇒ CLI-only recovery (H2).",
        footprint_dims=(DIM_PRIVILEGE, DIM_RUNTIME),
        requires=("sudoers", "openclaw_acls"),
        gate="LaunchDaemon ai.evolve.evolve.admin-ui; "
        "packages/admin/evolve_admin/web/routes_analytics.py "
        "(/api/modules/<m>/enable|disable)",
    ),
    _node(
        "gateway_plugin_install",
        kind=KIND_INFRA, classification=CLASS_INFRA_FLOOR,
        summary="The `evolve` plugin entry in openclaw.json — THE runtime root. "
        "No plugin ⇒ vanilla OC (no hooks/tools/ledgers). Maximal half-on (H3).",
        footprint_dims=(DIM_MUTATION, DIM_RUNTIME, DIM_PRIVILEGE),
        requires=("sudoers", "openclaw_acls", "admin_daemon"),
        gate="packages/admin/evolve_admin/deploy.py:2446 "
        "(plugin_entry.setdefault('enabled', True), unconditional)",
        note="Posture writes `tier`, never plugin presence — the precondition "
        "for the whole dial (graph §4).",
    ),
    _node(
        "allow_conversation_access",
        kind=KIND_INFRA, classification=CLASS_INFRA_FLOOR,
        summary="openclaw.json plugin hooks `allowConversationAccess` — the "
        "upstream-OC precondition for the whole content/observe layer. No toggle "
        "today; deploy-forced true.",
        footprint_dims=(DIM_MUTATION, DIM_RUNTIME),
        requires=("gateway_plugin_install",),
        gate="packages/admin/evolve_admin/deploy.py:2521+ (deploy-forced true)",
        unverified=True,
        note="Graph §6.1 — enforcement is upstream OpenClaw (≥2026.4.29); our "
        "plugin never reads the flag (grep=0). Edge direction firm; the precise "
        "per-hook degradation when false is inferred, not traced into OC. The "
        "requires edge is honored as real (fail-safe).",
    ),
    _node(
        "repo_puller_security",
        kind=KIND_INFRA, classification=CLASS_INFRA_FLOOR,
        summary="The repo-puller's security-update delivery role (15-min "
        "`git pull --ff-only` + restage/kickstart). No delivery path ⇒ pod can't "
        "be patched.",
        footprint_dims=(DIM_PRIVILEGE, DIM_MUTATION, DIM_RUNTIME),
        requires=("sudoers", "deploy_checkout"),
        gate="packages/analyzer/repo_puller.py (15-min ff-only pull)",
        unverified=True,
        note="Graph §6.5 — the infra-floor split (security-update path = floor; "
        "convenience auto-pull = cascade-ish) is a spec FD-5 #2 POLICY call, not a "
        "code branch that distinguishes security from feature pulls. The cadence "
        "could become a dial knob later; the existence of a delivery path is floor.",
    ),

    # ── §1b Runtime / tier ladder (the real runtime dial) ──────────────────────
    _node(
        "tier_ladder",
        kind=KIND_TIER_CAPABILITY, classification=CLASS_CASCADE,
        summary="The master runtime gate (off/monitor/manage/full, default full). "
        "`off` ⇒ all capabilities inert. Posture WRITES this; operators never set "
        "it by hand.",
        footprint_dims=(DIM_RUNTIME, DIM_MUTATION, DIM_COST),
        requires=("gateway_plugin_install",),
        gate="packages/plugin/src/config.ts:40 (TIERS), :114 (?? 'full' default)",
    ),
    _node(
        "observer",
        kind=KIND_TIER_CAPABILITY, classification=CLASS_CASCADE,
        summary="The content hooks (llm_output / agent_end / session_end) — the "
        "PRODUCER of the whole observe→analyze spine (annotations, summaries, cost "
        "data). Gated by the tier capability, NOT the dead `observer` module key.",
        footprint_dims=(DIM_RUNTIME, DIM_COST),
        requires=("tier_ladder", "allow_conversation_access", "gateway_plugin_install"),
        gate="packages/plugin/src/observer/TurnObserver.ts:1402/1479/1499 "
        "(observer capability at tier ≥ monitor)",
        note="CG-1 head (graph §5). The `observer` module key gates nothing "
        "(catalog accuracy flag #1) — the real gate is this tier capability.",
    ),
    _node(
        "model_routing",
        kind=KIND_TIER_CAPABILITY, classification=CLASS_CASCADE,
        summary="before_model_resolve tier/cost rewrite — hosts the runaway-rate "
        "cap + spend-cap safety nets (FD-8 coupling). Gated at tier ≥ manage.",
        footprint_dims=(DIM_MUTATION, DIM_RUNTIME, DIM_COST),
        requires=("tier_ladder",),
        gate="packages/plugin/src/observer/TurnObserver.ts:1688 (gate, manage+) "
        "+ routing.enabled kill-switch",
        note="CG-3 (graph §5 / H4): cutting routing also drops runaway_rate_cap + "
        "spend_cap_net unless FD-8 breaker-only mode is written. Co-own model-tiers/edr.",
    ),
    _node(
        "inject_pod_conduct",
        kind=KIND_TIER_CAPABILITY, classification=CLASS_SAFE_LEAF,
        summary="session_start persona injection. Pure system-prompt mutation; no "
        "reader downstream. Gated at tier ≥ manage.",
        footprint_dims=(DIM_MUTATION, DIM_RUNTIME, DIM_COST),
        requires=("tier_ladder",),
        gate="packages/plugin/src/observer/TurnObserver.ts:1374 (manage+)",
    ),
    _node(
        "inject_keywords",
        kind=KIND_TIER_CAPABILITY, classification=CLASS_SAFE_LEAF,
        summary="before_prompt_build per-turn system-prompt injection. Top of the "
        "ladder; nothing downstream. The smallest, safest cut.",
        footprint_dims=(DIM_MUTATION, DIM_RUNTIME, DIM_COST),
        requires=("tier_ladder",),
        gate="packages/plugin/src/observer/TurnObserver.ts:1527 (full)",
    ),
    _node(
        "preflight",
        kind=KIND_TIER_CAPABILITY, classification=CLASS_SAFE_LEAF,
        summary="Per-turn haiku intent router. Own kill-switch; Phase-1 returns "
        "ABSTAIN so nothing downstream depends on it.",
        footprint_dims=(DIM_RUNTIME, DIM_COST, DIM_MUTATION),
        requires=("gateway_plugin_install",),
        gate="packages/plugin/src/observer/TurnObserver.ts:906 "
        "(_isPreflightEnabled, cascade.preflight.enabled default true)",
        note="Gated by its OWN flag, not the tier ladder — so it requires only the "
        "plugin, not tier_ladder.",
    ),
    _node(
        "outward_action_ledger",
        kind=KIND_RUNTIME, classification=CLASS_CASCADE,
        summary="Per-MCP-call JSONL written in agent_end — the data source the "
        "autonomy caps read. Deliberately un-gateable (a kill-switch would silently "
        "disable an operator-set limit).",
        footprint_dims=(DIM_RUNTIME,),
        requires=("observer",),
        gate="packages/plugin/src/observer/OutwardActionLedger.ts:154 (write); "
        "instantiated unconditionally TurnObserver.ts:1121",
        note="CG-4 head (graph §5). Graph §1b requires = 'tier ≥ monitor (written "
        "in agent_end), gateway plugin' — the writes happen in the observer's "
        "agent_end hook, so the single faithful edge is →observer (which itself "
        "requires tier_ladder + gateway_plugin_install). This preserves the CG-1/"
        "CG-4 cascade: dialing observer off pulls the ledger and autonomy_caps.",
    ),
    _node(
        "autonomy_caps",
        kind=KIND_RUNTIME, classification=CLASS_CASCADE,
        summary="Rung-3 record_application limits — read the outward-action ledger "
        "to count actions. The contract decides caps-exist-or-not as a unit, never "
        "a half-disable (H7).",
        footprint_dims=(DIM_MUTATION,),
        requires=("outward_action_ledger",),
        gate="rung-3 record_application caps read the outward ledger",
        unverified=True,
        note="Graph §6.2 — the WRITE side of the ledger is cited; the claim that "
        "rung-3 caps READ it rests on the in-code docstring, not a traced reader. "
        "The requires→outward_action_ledger edge is honored as real (fail-safe): if "
        "the ledger is dialed off, the cascade pulls autonomy_caps with it (CG-4).",
    ),
    _node(
        "registered_tools",
        kind=KIND_TIER_CAPABILITY, classification=CLASS_CASCADE,
        summary="Tier-gated tools (defer / session.set_tier / record_application). "
        "record_application is the autonomy-cap write tool, tied to tier ≥ manage.",
        footprint_dims=(DIM_COST, DIM_MUTATION),
        requires=("tier_ladder", "gateway_plugin_install"),
        gate="packages/plugin/src/index.ts:111 (tier-gated registration)",
        note="CG-5 (graph §5): dropping below `manage` removes record_application "
        "alongside the ledger consumer — reason CG-4 + CG-5 together.",
    ),
    _node(
        "roster_tools",
        kind=KIND_RUNTIME, classification=CLASS_SAFE_LEAF,
        summary="set_role / block / unblock / newcomer_mode — registered "
        "unconditionally on every bot. Tool-surface token cost only; per-call auth "
        "at the daemon. Not a dial target.",
        footprint_dims=(DIM_COST,),
        requires=("gateway_plugin_install",),
        gate="packages/plugin/src/index.ts:188 (unconditional)",
    ),

    # ── §1c Analysis / RSI data chain (Python analyzer) ────────────────────────
    _node(
        "rsi",
        kind=KIND_MODULE, classification=CLASS_CASCADE,
        summary="The RSI master switch — short-circuits analysis + apply + "
        "outcomes together (CG-2). Gates ONLY those three (NOT tier-3 audit / "
        "scanner / tuples — catalog accuracy flag #2).",
        footprint_dims=(DIM_COST,),
        gate="packages/analyzer/analyze.py:1071 / apply.py:114 / outcome.py:316 "
        "(is_rsi_enabled)",
        note="CG-2 head (graph §5) — already a clean cascade in code.",
    ),
    _node(
        "metrics",
        kind=KIND_MODULE, classification=CLASS_CASCADE,
        summary="Daily metric aggregation (measure.py) — reads annotations from the "
        "observer. No annotations ⇒ empty metrics (H5).",
        footprint_dims=(DIM_PRIVILEGE,),
        requires=("observer",),
        gate="packages/analyzer/measure.py:475 (metrics module)",
    ),
    _node(
        "tuples",
        kind=KIND_DAEMON, classification=CLASS_CASCADE,
        summary="Observation-tuple extraction (extract_tuples.py, daily haiku) — "
        "reads session summaries, feeds generators/profile inference. NO module gate "
        "(GAP).",
        footprint_dims=(DIM_COST, DIM_PRIVILEGE),
        requires=("observer",),
        gate="packages/analyzer/extract_tuples.py:207 (reads summaries); no "
        "is_module_enabled/is_rsi_enabled gate (catalog: tuples GAP)",
        unverified=True,
        note="Graph §6.3 — producer→observations proven; the specific generators "
        "that READ tuples were not individually enumerated (consumer set partial). "
        "Modeled consumers are unwired here; required_by stays empty pending §6.3.",
    ),
    _node(
        "analysis",
        kind=KIND_MODULE, classification=CLASS_CASCADE,
        summary="The 11 behavior detectors (analyze.py) — writes proposals/pending. "
        "Metric detectors need metrics; annotation detectors need annotations; each "
        "degrades independently (H5).",
        footprint_dims=(DIM_COST, DIM_PRIVILEGE),
        requires=("metrics", "observer", "rsi"),
        gate="packages/analyzer/analyze.py:1071 (is_rsi_enabled) + :1075 (analysis)",
    ),
    _node(
        "apply",
        kind=KIND_MODULE, classification=CLASS_CASCADE,
        summary="The applier daemon — realizes approved proposals into bot config. "
        "The L2 write path couples it to the sudoers/ACL infra floor.",
        footprint_dims=(DIM_MUTATION, DIM_PRIVILEGE),
        requires=("analysis", "rsi", "sudoers", "openclaw_acls"),
        gate="packages/analyzer/apply.py:114 (is_rsi_enabled) + :117 (apply)",
    ),
    _node(
        "outcomes",
        kind=KIND_MODULE, classification=CLASS_CASCADE,
        summary="Outcome tallying (outcome.py) — terminal of the RSI chain. Nothing "
        "applied ⇒ empty no-op (H9).",
        footprint_dims=(DIM_PRIVILEGE,),
        requires=("apply", "rsi"),
        gate="packages/analyzer/outcome.py:316 (is_rsi_enabled) + :322 (outcomes)",
    ),
    _node(
        "behavior_detectors",
        kind=KIND_MODULE, classification=CLASS_SAFE_LEAF,
        summary="The 11 individual behavior detectors — flippable one at a time "
        "under the `analysis` cascade parent; nothing depends on a single detector.",
        footprint_dims=(DIM_COST, DIM_PRIVILEGE),
        requires=("analysis",),
        gate="per-detector toggles under DEFAULT_MODULES['analysis']['detectors']",
        note="Edge basis = graph §5 ('safe leaves UNDER the analysis cascade "
        "parent') + the detectors dict nesting under the analysis module, NOT the "
        "§1e 'data source' column (that names which detectors no-op, informational). "
        "If analysis is off, no detector runs — so the structural parent is analysis.",
    ),
    _node(
        "arbiter_appliers",
        kind=KIND_APPLIER, classification=CLASS_CASCADE,
        summary="The per-applier arbiter mutations (ConfigPatch / permissions / cron "
        "/ tier / MCP / soul) — gated by apply + rsi; auto vs approved_human per "
        "is_autonomous_eligible.",
        footprint_dims=(DIM_MUTATION, DIM_RUNTIME, DIM_COST, DIM_PRIVILEGE),
        requires=("apply", "rsi"),
        gate="packages/analyzer/arbiter/appliers/ (gated by apply + is_rsi_enabled)",
        note="Sourced from the consolidated catalog F-1d §D 'Arbiter appliers' "
        "(module key: apply + rsi), per the F-3-1a chip's 'appliers from the "
        "catalog' superset — not a graph-doc §1 row. Gives KIND_APPLIER a home "
        "alongside l1_cost_breaker.",
    ),

    # ── §1d Cost / safety breakers (FD-3 safety floor — kept ON when dialed down) ─
    _node(
        "cost",
        kind=KIND_MODULE, classification=CLASS_CASCADE, safety_floor=True,
        summary="spend_alert.py burst/cap detection AND the L1-breaker auto-trip "
        "feed. Disabling it is a SAFETY regression, not just a feature cut (H4).",
        footprint_dims=(DIM_PRIVILEGE,),
        requires=("observer",),
        gate="packages/analyzer/spend_alert.py:1553 (cost module)",
        note="CG-3 (graph §5). FD-3: stays ON even in Passive, framed as safety.",
    ),
    _node(
        "l1_cost_breaker",
        kind=KIND_APPLIER, classification=CLASS_CASCADE, safety_floor=True,
        summary="The L1 daily-cap breaker — strips heartbeat / narrows exec on trip. "
        "$0 to run; mutates bot behavior when it fires.",
        footprint_dims=(DIM_MUTATION, DIM_RUNTIME, DIM_PRIVILEGE),
        requires=("cost",),
        gate="packages/analyzer/spend_alert.py:991 "
        "(writes breakers/<bot>/cost.json, runs enforce)",
        note="FD-3 safety floor (graph §1d).",
    ),
    _node(
        "runaway_rate_cap",
        kind=KIND_RUNTIME, classification=CLASS_CASCADE, safety_floor=True,
        summary="ModelRouter forced-downgrade runaway-spend protection. Lives INSIDE "
        "the modelRouting hook — cutting routing drops it (H4).",
        footprint_dims=(DIM_MUTATION, DIM_RUNTIME),
        requires=("model_routing",),
        gate="packages/plugin/src/observer/ModelRouter.ts:193 (on by default)",
        note="FD-3 / FD-8 — kept via breaker-only routing mode when routing is cut.",
    ),
    _node(
        "spend_cap_net",
        kind=KIND_RUNTIME, classification=CLASS_CASCADE, safety_floor=True,
        summary="isSpendCapActive → force `fast`/pause. Same modelRouting-hook "
        "coupling as the runaway cap (H4).",
        footprint_dims=(DIM_MUTATION, DIM_RUNTIME),
        requires=("model_routing",),
        gate="packages/plugin/src/observer/ModelRouter.ts:2726 (isSpendCapActive)",
        note="FD-3 / FD-8 — kept via breaker-only routing mode when routing is cut.",
    ),
    _node(
        "cost_watchdog",
        kind=KIND_DAEMON, classification=CLASS_SAFE_LEAF,
        summary="$0 cost-antipattern detector — runs unconditionally, NOT gated by "
        "the `cost` module (catalog accuracy flag #4). Independent of `cost`.",
        footprint_dims=(DIM_PRIVILEGE,),
        gate="runs unconditionally; no is_module_enabled('cost') consumer",
    ),

    # ── §1e Healing / monitors / costed leaves ─────────────────────────────────
    _node(
        "healing",
        kind=KIND_MODULE, classification=CLASS_SAFE_LEAF,
        summary="heal.py gateway self-heal — watches live HTTP health + ps, NOT the "
        "observe/analysis layer. The REFUTED spine edge; the natural floor self-heal "
        "(it restores the plugin).",
        footprint_dims=(DIM_PRIVILEGE, DIM_RUNTIME),
        gate="packages/analyzer/heal.py:439 (healing module)",
        note="Graph §1e — independent of observations/metrics/annotations "
        "(grep-confirmed absent); requires nothing in the dial.",
    ),
    _node(
        "pod_monitors",
        kind=KIND_MONITOR, classification=CLASS_CASCADE,
        summary="The ~30 pure-Python pod-wide monitors (pod_report, audit, "
        "host_health, watchdog…) that write Signals. Observability, not control plane.",
        footprint_dims=(DIM_PRIVILEGE,),
        gate="unconditional per-daemon; each writes signals.store.observe()",
    ),
    _node(
        "signal_subscriber",
        kind=KIND_DAEMON, classification=CLASS_CASCADE,
        summary="1 Hz firing-dir watch → event-driven generator dispatch. A LATENCY "
        "reduction; the daily generator sweep backstops it.",
        footprint_dims=(DIM_PRIVILEGE, DIM_RUNTIME, DIM_COST),
        requires=("pod_monitors",),
        gate="ai.evolve.evolve.signal-subscriber LaunchDaemon (watches signals/firing/)",
        unverified=True,
        note="Graph §6.4 — classified cascade/observability from the spec's 'daily "
        "sweep is the safety net,' not a per-generator dependency trace. If a "
        "load-bearing generator subscribes AND lacks a daily fallback, the subscriber "
        "is more load-bearing than classified. Edge →pod_monitors models the §1e "
        "Notes ('monitors write Signals; subscriber dispatches generators') — the "
        "doc's terse requires column bundles both as one row with requires=—; this "
        "bite splits them so KIND_MONITOR has a home and the producer→consumer "
        "relationship is explicit.",
    ),
    _node(
        "expansion",
        kind=KIND_MODULE, classification=CLASS_SAFE_LEAF,
        summary="Monthly app-expansion haiku (~5 calls/mo). Isolated.",
        footprint_dims=(DIM_COST, DIM_PRIVILEGE),
        gate="packages/analyzer/expansion.py:729 (expansion module)",
    ),
    _node(
        "continuity_engine",
        kind=KIND_MODULE, classification=CLASS_SAFE_LEAF,
        summary="Per-bot time-deferred promises. Per-bot, default on; nothing "
        "pod-wide depends on it.",
        footprint_dims=(DIM_COST, DIM_PRIVILEGE),
        gate="packages/analyzer/defer_runner.py:210 (continuity_engine module)",
    ),
    _node(
        "community_intel",
        kind=KIND_MODULE, classification=CLASS_SAFE_LEAF,
        summary="Weekly external Kaizen scan. Default OFF already.",
        footprint_dims=(DIM_COST, DIM_PRIVILEGE),
        gate="packages/analyzer/community_intel.py:373 (community_intel module)",
    ),
    _node(
        "slack_signals",
        kind=KIND_MODULE, classification=CLASS_SAFE_LEAF,
        summary="Slack reaction signals. Default OFF already (ingestion token-gated).",
        footprint_dims=(DIM_PRIVILEGE,),
        gate="packages/analyzer/slack_signals.py:503 (slack_signals module)",
    ),
    _node(
        "tier3_app_audit",
        kind=KIND_DAEMON, classification=CLASS_SAFE_LEAF,
        summary="Tier-3 semantic app audit (2 `openclaw agent` LLM dispatches/due "
        "app) — the heaviest token spender. Cadence-gated, NOT under the `rsi` "
        "master. Owner: apps.",
        footprint_dims=(DIM_COST, DIM_RUNTIME),
        gate="packages/analyzer/app_audit_runner.py (cadence-gated; no is_rsi_enabled)",
    ),
    _node(
        "app_scanner",
        kind=KIND_DAEMON, classification=CLASS_SAFE_LEAF,
        summary="App discovery + purpose classifier (Haiku/Sonnet). On-demand only. "
        "Owner: apps.",
        footprint_dims=(DIM_COST,),
        gate="packages/analyzer/scanner.py (on-demand; no is_rsi_enabled)",
    ),
    _node(
        "user_profile_inferrer",
        kind=KIND_DAEMON, classification=CLASS_SAFE_LEAF,
        summary="Per-session user-profile inference (Haiku, bot's own creds, "
        "DNT-gated). Nothing downstream consumes it as a hard dep.",
        footprint_dims=(DIM_COST, DIM_PRIVILEGE),
        gate="per-session Haiku; DNT-gated (own creds)",
    ),
    _node(
        "model_discovery",
        kind=KIND_DAEMON, classification=CLASS_SAFE_LEAF,
        summary="Model-fit classifier (Haiku, only on a new model). Fail-open; "
        "isolated.",
        footprint_dims=(DIM_COST,),
        gate="per-discovery; fail-open",
    ),
    _node(
        "security_warden",
        kind=KIND_DAEMON, classification=CLASS_SAFE_LEAF, safety_floor=True,
        summary="Haiku injection-verifier. Off ⇒ regex-only injection detection "
        "(not zero). Security floor (H6) — co-own edr.",
        footprint_dims=(DIM_COST,),
        gate="regex-gated Haiku verifier; fails open to regex-only",
        note="FD-3 security floor (graph §1e / H6) — kept ON in Passive, framed as "
        "safety. No edge: nothing requires it and it requires nothing in the dial.",
    ),
)

# Register the graph nodes alongside the #3322 output-only entries (one shared
# registry — spec: "do NOT build a parallel registry"). Id collisions are a bug.
for _n in _GRAPH_NODES:
    if _n.id in FOOTPRINT_COMPONENTS:
        raise ValueError(
            f"F-3 graph node id {_n.id!r} collides with an existing output "
            f"component — pick a distinct id"
        )
    FOOTPRINT_COMPONENTS[_n.id] = _n


def _derive_required_by() -> None:
    """Populate every node's ``required_by`` by inverting ``requires``.

    ``requires`` is the single authored source of truth (spec FD-5:
    "required_by[] (derived)"); this makes the two reciprocal **by construction**,
    so the graph cannot drift. Unverified edges are inverted exactly like verified
    ones — an unverified ``requires`` still produces a ``required_by`` link, which
    is the fail-safe (the dependent is never silently dropped).
    """
    inverse: dict[str, list[str]] = {cid: [] for cid in FOOTPRINT_COMPONENTS}
    for cid, comp in FOOTPRINT_COMPONENTS.items():
        for dep in comp.requires:
            if dep in inverse:           # missing targets are flagged by the validator
                inverse[dep].append(cid)
    for cid, deps in inverse.items():
        rb = tuple(sorted(deps))
        comp = FOOTPRINT_COMPONENTS[cid]
        if comp.required_by != rb:
            FOOTPRINT_COMPONENTS[cid] = replace(comp, required_by=rb)


_derive_required_by()


# ── Posture-graph query API ──────────────────────────────────────────────────
def graph_nodes() -> dict[str, FootprintComponent]:
    """The F-3 posture-graph nodes (``kind`` set) — excludes #3322 output-only entries."""
    return {cid: c for cid, c in FOOTPRINT_COMPONENTS.items() if c.kind}


def is_dial_scoped(comp: FootprintComponent) -> bool:
    """FD-7: only cascade + safe-leaf are dial targets; the infra floor is OUT."""
    return comp.classification in DIAL_SCOPED_CLASSIFICATIONS


def dial_components() -> dict[str, FootprintComponent]:
    """The v1 dial domain — the cascade + safe-leaf graph nodes (FD-7)."""
    return {cid: c for cid, c in graph_nodes().items() if is_dial_scoped(c)}


def honored_requires(comp: FootprintComponent) -> tuple[str, ...]:
    """The requires edges the engine must honor — ALL of them.

    Fail-safe (spec FD-5): an ``unverified`` (graph §6 soft) edge is honored as
    real, never skipped. This helper exists so a future cascade engine has one
    obvious call that *cannot* accidentally drop a soft edge.
    """
    return comp.requires


def _requires_cycle(nodes: dict[str, FootprintComponent]) -> list[str]:
    """Return a cycle path in the ``requires`` DAG, or [] if acyclic (DFS)."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {cid: WHITE for cid in nodes}
    path: list[str] = []
    found: list[str] = []

    def visit(cid: str) -> bool:
        color[cid] = GREY
        path.append(cid)
        for dep in nodes[cid].requires:
            if dep not in nodes:
                continue  # missing-target — reported separately, not a cycle
            if color[dep] == GREY:
                found.extend(path[path.index(dep):] + [dep])
                return True
            if color[dep] == WHITE and visit(dep):
                return True
        path.pop()
        color[cid] = BLACK
        return False

    for cid in nodes:
        if color[cid] == WHITE and visit(cid):
            break
    return found


def graph_consistency_errors() -> list[str]:
    """Structural integrity of the posture graph (the load-validator).

    Asserts: valid kind/classification/dims; every ``requires`` target exists and
    is itself a graph node; ``requires``/``required_by`` are reciprocal; no cycles
    in the cascade DAG; the FD-7 partition holds (the dial domain is exactly the
    non-floor classifications — the control plane can never become a dial target);
    safety_floor items are dial-scoped (FD-3, never infra-floor); cascade nodes
    carry ≥1 edge; every unverified node documents its soft edge. Unverified edges
    are validated **identically to verified ones** (fail-safe — never skipped).
    """
    errors: list[str] = []
    nodes = graph_nodes()

    # FD-7 partition invariant (guards the constants, not a per-node tautology):
    # the dial domain is EXACTLY the non-floor classifications. If someone ever
    # adds infra-floor to DIAL_SCOPED_CLASSIFICATIONS the engine would offer to
    # dial off a control-plane floor item — the lockout this whole split prevents.
    if CLASS_INFRA_FLOOR in DIAL_SCOPED_CLASSIFICATIONS:
        errors.append("FD-7 violated: infra-floor is dial-scoped — the control "
                      "plane must never be a dial target")
    if DIAL_SCOPED_CLASSIFICATIONS | {CLASS_INFRA_FLOOR} != VALID_CLASSIFICATIONS:
        errors.append("FD-7 partition broken: dial-scoped ∪ {infra-floor} must "
                      "equal VALID_CLASSIFICATIONS")

    for cid, comp in nodes.items():
        if comp.kind not in VALID_KINDS:
            errors.append(f"{cid}: unknown kind {comp.kind!r} "
                          f"(expected one of {sorted(VALID_KINDS)})")
        if comp.classification not in VALID_CLASSIFICATIONS:
            errors.append(f"{cid}: unknown classification {comp.classification!r} "
                          f"(expected one of {sorted(VALID_CLASSIFICATIONS)})")
        if not comp.footprint_dims:
            errors.append(f"{cid}: a graph node must declare ≥1 footprint dimension")
        for d in comp.footprint_dims:
            if d not in VALID_DIMS:
                errors.append(f"{cid}: unknown footprint dim {d!r} "
                              f"(expected a subset of {sorted(VALID_DIMS)})")

        # requires: exists, is a graph node, no self-loop, no duplicates.
        seen: set[str] = set()
        for dep in comp.requires:
            if dep == cid:
                errors.append(f"{cid}: requires itself")
            if dep in seen:
                errors.append(f"{cid}: duplicate requires {dep!r}")
            seen.add(dep)
            if dep not in FOOTPRINT_COMPONENTS:
                errors.append(f"{cid}: requires {dep!r} which is not a registered "
                              f"component")
            elif dep not in nodes:
                errors.append(f"{cid}: requires {dep!r} which is an output-only "
                              f"component, not a graph node")

        # Reciprocity (both directions). Derived required_by makes this hold by
        # construction; the check guards against a future hand-edited override.
        for dep in comp.requires:
            if dep in nodes and cid not in nodes[dep].required_by:
                errors.append(f"{cid}: requires {dep!r} but {dep!r}.required_by is "
                              f"missing {cid!r} (reciprocity)")
        for rb in comp.required_by:
            if rb not in nodes:
                errors.append(f"{cid}: required_by {rb!r} is not a graph node")
            elif cid not in nodes[rb].requires:
                errors.append(f"{cid}: required_by {rb!r} but {rb!r}.requires is "
                              f"missing {cid!r} (reciprocity)")

        # Classification ↔ safety invariant (FD-3): a safety-floor item is
        # dialable-in-principle-but-kept-ON, so it is NEVER infra-floor (graph §4
        # note). Mis-marking one infra-floor would wrongly exclude it from the dial.
        if comp.safety_floor and comp.classification == CLASS_INFRA_FLOOR:
            errors.append(f"{cid}: safety_floor item must be dial-scoped "
                          f"(cascade/safe-leaf), never infra-floor (FD-3)")
        if (comp.classification == CLASS_CASCADE
                and not comp.requires and not comp.required_by):
            errors.append(f"{cid}: cascade node has no edges — a cascade implies a "
                          f"dependency relationship (mis-classified safe-leaf?)")

        # Unverified soft edges must document their provenance (graph §6).
        if comp.unverified and not comp.note:
            errors.append(f"{cid}: unverified node must carry a note citing the "
                          f"soft edge (graph §6)")

    cycle = _requires_cycle(nodes)
    if cycle:
        errors.append("cycle in the requires DAG: " + " → ".join(cycle))

    return errors


# ── Query / validation API (used by tools/footprint-output-lint + tests) ─────
def iter_outputs() -> list[tuple[str, OutputDeclaration]]:
    """``(component_id, OutputDeclaration)`` for every declared output."""
    out: list[tuple[str, OutputDeclaration]] = []
    for cid, comp in FOOTPRINT_COMPONENTS.items():
        for od in comp.outputs:
            out.append((cid, od))
    return out


def claimed_writer_files() -> set[str]:
    """Repo-relative source files that own at least one declared output.

    The lint's file-coverage check: any file with a surface write-site that is
    NOT in this set hosts an undeclared auto-generated output.
    """
    return {od.writer_file for _cid, od in iter_outputs()}


def output_errors(od: OutputDeclaration, *, where: str) -> list[str]:
    """Policy violations for one :class:`OutputDeclaration`.

    The contract, enforced mechanically:
      * a valid ``surface``;
      * a ``writer`` of the form ``relpath.py:func``;
      * a retention ``pruner`` and ``window`` (always present, non-empty);
      * an unbounded retention window REQUIRES a ``justification``;
      * a named consumer REQUIRES ``reader`` + ``read_site``;
      * a ``consumer: none`` REQUIRES a ``justification`` **and** a finite
        retention window (no-consumer + no-retention is the forbidden
        ``_ingested`` sediment shape).
    """
    errors: list[str] = []
    tag = f"{where} [{od.path_glob}]"

    if od.surface not in VALID_SURFACES:
        errors.append(f"{tag}: unknown surface {od.surface!r} "
                      f"(expected one of {sorted(VALID_SURFACES)})")
    if not od.writer.strip():
        errors.append(f"{tag}: writer is required (relpath.py:func)")
    elif not (od.writer_file.endswith(".py")
              or od.writer_file.endswith(_EXTERNAL_WRITER_SUFFIXES)):
        errors.append(f"{tag}: writer {od.writer!r} must point at a .py file "
                      f"(form 'relpath.py:func') — or, for an external producer "
                      f"the lint cannot govern, a {_EXTERNAL_WRITER_SUFFIXES} file")

    r = od.retention
    if not r.pruner.strip() or not r.window.strip():
        errors.append(f"{tag}: retention requires both a pruner and a window")
    if r.is_unbounded and not r.justification.strip():
        errors.append(
            f"{tag}: retention window {r.window!r} is unbounded — it REQUIRES a "
            f"justification (why does this output never need time-pruning?)"
        )

    c = od.consumer
    if c.none:
        if not c.justification.strip():
            errors.append(
                f"{tag}: consumer: none REQUIRES a justification (who, if anyone, "
                f"ever reads this — and why is no programmatic reader OK?)"
            )
        if r.is_unbounded:
            errors.append(
                f"{tag}: consumer: none with an unbounded retention window is "
                f"FORBIDDEN — a no-reader output that also never prunes is exactly "
                f"the write-only sediment this contract exists to prevent. Give it a "
                f"finite retention window."
            )
    else:
        if not c.reader.strip() or not c.read_site.strip():
            errors.append(
                f"{tag}: a named consumer requires both a reader and a read_site "
                f"(or declare consumer: none with a justification)"
            )
    return errors


def consistency_errors() -> list[str]:
    """Registry-internal policy violations (the lint blocks on any of these)."""
    errors: list[str] = []
    seen_keys: set[tuple[str, str]] = set()
    for cid, comp in FOOTPRINT_COMPONENTS.items():
        if cid != comp.id:
            errors.append(f"{cid}: key does not match component.id ({comp.id!r})")
        # The output contract requires ≥1 output of an OUTPUT-only entry. A
        # pure F-3 graph node (kind set, no auto-gen output) is exempt — its
        # integrity is checked by graph_consistency_errors() instead.
        if not comp.outputs and not comp.kind:
            errors.append(f"{cid}: a footprint component must declare ≥1 output")
        for od in comp.outputs:
            errors.extend(output_errors(od, where=cid))
            key = (od.surface, od.path_glob)
            if key in seen_keys:
                errors.append(
                    f"{cid}: duplicate output declaration for "
                    f"{od.surface}:{od.path_glob}"
                )
            seen_keys.add(key)
    return errors
