"""
audit_poller.py — Admin-side ingester for bot audit outboxes.

Each bot's `audit_runner.py` writes outbox records to
`<home_root>/<bot>/.openclaw/workspace/evolve/audit_outbox/<record_id>.json`
(``<home_root>`` = ``/Users`` on macOS, ``/home`` on Linux — see the
platform-profile note on the path helpers below).
This module is the receiving end: it walks every bot's outbox, ingests
records into the pod-wide Signal store (Tier-2 findings → Signals; run
summaries → sweep_resolve calls), and archives processed files.

The poller is **idempotent**. observe() dedupes by signature; sweep_resolve
re-clears already-resolved signals harmlessly. Re-running the poller against
the same outbox is safe — the file move is the only side effect that takes
the work off the queue.

Called from the `audit-scheduler` tick once per hour (was `app-test-scheduler`
before the 2026-06-08 rename — see internal/decision-app-tests-2026-06-08.md). No new
daemon. The structural verifier in `audit_runner.py` runs on its own bot-
side schedule (every 6 hours per spec); the poller is just the bridge that
gets findings from the bot's outbox into the admin's Signal store.

See internal/spec-app-audit-2026-05-16.md §8 for the full output integration.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from platform_profile import get_profile
from .. import external_ids as _external_ids


logger = logging.getLogger(__name__)


# ── Paths ────────────────────────────────────────────────────────────────────
#
# ``bot_user`` is the already-resolved OS account name (poll_bot's caller
# resolves it via get_bot_user). The home ROOT, though, is platform-specific —
# ``/Users`` on macOS, ``/home`` on Linux — so it MUST come from the platform
# profile, never a literal. A hardcoded ``/Users/{bot}`` made every Linux-pod
# outbox path resolve to a directory that doesn't exist: _list_outbox_files saw
# ``outbox.exists() == False``, returned [], and the hourly audit-scheduler tick
# drained zero records on the VPS while the bot-side runner kept writing them —
# darwin/evo outboxes piled up at ~195/192 root records, _ingested never created.
# See the cross-OS path note in platform_profile.py.


def _bot_workspace_evolve_dir(bot_user: str) -> Path:
    return (
        Path(get_profile().user_home_root)
        / bot_user
        / ".openclaw"
        / "workspace"
        / "evolve"
    )


def _audit_outbox_dir(bot_user: str) -> Path:
    return _bot_workspace_evolve_dir(bot_user) / "audit_outbox"


def _audit_outbox_ingested(bot_user: str) -> Path:
    return _audit_outbox_dir(bot_user) / "_ingested"


def _audits_dir_for_bot(bot_user: str) -> Path:
    """Per-bot audits root (the trail.jsonl lives at
    ``<root>/<app_id>/trail.jsonl`` — same shape app_changelog uses).
    """
    return _bot_workspace_evolve_dir(bot_user) / "audits"


# ── Drain-liveness heartbeat ──────────────────────────────────────────────────
#
# Each tick drops one compact ``(ts, processed, backlog)`` sample under
# ``{shared_dir}/monitors/`` so monitor_coverage can fire ``audit_drain_silent``
# when the drain runs with Result=success but ingests ZERO records while the
# outbox roots stay non-empty — the silent-stall class a hardcoded outbox path
# (#3310) produced, which mtime-based producer-silence cannot see (the daemon's
# stdout advances on every empty-but-successful tick). This side is a *dumb
# recorder*: the rolling window lives here, but every silence threshold lives in
# monitor_coverage.detect_audit_drain_stall (the detector owns the policy).
AUDIT_DRAIN_HEARTBEAT_REL = "monitors/audit_drain_heartbeat.json"
_HEARTBEAT_HISTORY_MAX = 48  # ~2 days at the hourly drain cadence


# ── Result types ─────────────────────────────────────────────────────────────


@dataclass
class PollResult:
    """Per-bot poll outcome — what the scheduler logs after a tick."""
    bot_id: str
    bot_user: str
    files_processed: int = 0
    findings_ingested: int = 0
    summaries_processed: int = 0
    signals_swept: int = 0
    # Tier-3 counters
    tier3_findings_ingested: int = 0
    tier3_proposals_raised: int = 0
    tier3_conflict_notices: int = 0
    # Substrate-audit counters (Workstream B-skills).
    skill_findings_ingested: int = 0
    skill_proposals_raised: int = 0
    provider_findings_ingested: int = 0
    provider_proposals_raised: int = 0
    # Workstream C — investigation counters
    investigation_notifications: int = 0
    investigation_proposals_raised: int = 0
    # Repair session counters (spec-app-coherence-and-reconciliation §11.3).
    repair_applied: int = 0
    repair_failed: int = 0
    repair_proposals_raised: int = 0
    # Archive-vs-drop accounting (the re-emission cut). ``records_archived``
    # = drained records kept in _ingested/; ``records_dropped`` = drained
    # records DELETED because they add no forensic value (already durably
    # ingested into the signal/proposal store). Split by reason so operators
    # can see the reduction.
    records_archived: int = 0
    records_dropped: int = 0
    dropped_dedup_hit: int = 0     # finding re-emission (signal/proposal dedup)
    dropped_heartbeat: int = 0     # apps_audited:0 no-op run_summary
    errors: list[str] = field(default_factory=list)


@dataclass
class InfraPollResult:
    """Pod-wide infra-audit poll outcome.

    Distinct from PollResult because infra audits aren't bot-scoped —
    the runner is admin-side and the outbox lives under {shared_dir}/.
    """
    files_processed: int = 0
    findings_ingested: int = 0    # infra_finding → Proposal
    summaries_processed: int = 0  # infra_run_summary → counter
    signals_emitted: int = 0      # infra_run_failed → Signal
    signals_swept: int = 0        # cleared findings on next run
    records_archived: int = 0
    records_dropped: int = 0
    dropped_dedup_hit: int = 0
    dropped_heartbeat: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class TickResult:
    """Aggregate across all bots in a single tick."""
    bots: list[PollResult] = field(default_factory=list)
    infra: InfraPollResult = field(default_factory=InfraPollResult)

    @property
    def total_files(self) -> int:
        return sum(r.files_processed for r in self.bots) + self.infra.files_processed

    @property
    def total_archived(self) -> int:
        return (
            sum(r.records_archived for r in self.bots)
            + self.infra.records_archived
        )

    @property
    def total_dropped(self) -> int:
        return (
            sum(r.records_dropped for r in self.bots)
            + self.infra.records_dropped
        )

    @property
    def total_dropped_dedup_hit(self) -> int:
        return (
            sum(r.dropped_dedup_hit for r in self.bots)
            + self.infra.dropped_dedup_hit
        )

    @property
    def total_dropped_heartbeat(self) -> int:
        return (
            sum(r.dropped_heartbeat for r in self.bots)
            + self.infra.dropped_heartbeat
        )

    @property
    def total_findings(self) -> int:
        return sum(r.findings_ingested for r in self.bots)

    @property
    def total_swept(self) -> int:
        return sum(r.signals_swept for r in self.bots) + self.infra.signals_swept

    @property
    def total_tier3_findings(self) -> int:
        return sum(r.tier3_findings_ingested for r in self.bots)

    @property
    def total_tier3_proposals(self) -> int:
        return sum(r.tier3_proposals_raised for r in self.bots)

    @property
    def total_tier3_conflicts(self) -> int:
        return sum(r.tier3_conflict_notices for r in self.bots)

    @property
    def total_infra_findings(self) -> int:
        return self.infra.findings_ingested

    @property
    def total_skill_findings(self) -> int:
        return sum(r.skill_findings_ingested for r in self.bots)

    @property
    def total_skill_proposals(self) -> int:
        return sum(r.skill_proposals_raised for r in self.bots)

    @property
    def total_provider_findings(self) -> int:
        return sum(r.provider_findings_ingested for r in self.bots)

    @property
    def total_provider_proposals(self) -> int:
        return sum(r.provider_proposals_raised for r in self.bots)

    @property
    def total_investigation_notifications(self) -> int:
        return sum(r.investigation_notifications for r in self.bots)

    @property
    def total_investigation_proposals(self) -> int:
        return sum(r.investigation_proposals_raised for r in self.bots)


# ── Outbox reading (with sudo fallback) ──────────────────────────────────────


def _read_outbox_file(path: Path) -> dict | None:
    """Read an outbox JSON file. Falls back to sudo /bin/cat on PermissionError.

    The bot user owns audit_outbox/; evolve has ACL read via set_evolve_read_acl
    but freshly-deployed bots may not yet have the inherited ACL on newly-
    created files until a workspace ACL refresh runs. The sudo fallback covers
    that gap.
    """
    try:
        return json.loads(path.read_text())
    except PermissionError:
        pass
    except (OSError, json.JSONDecodeError):
        return None
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return None


def _list_outbox_files(bot_user: str) -> list[Path]:
    """Return processable outbox files (skips the _ingested archive dir).

    Sorted by mtime so we process records in roughly the order the bot wrote
    them — run summaries land after their findings.
    """
    outbox = _audit_outbox_dir(bot_user)
    if not outbox.exists():
        return []
    files: list[Path] = []
    try:
        for entry in outbox.iterdir():
            if entry.name.startswith(".") or entry.name == "_ingested":
                continue
            if entry.is_file() and entry.suffix == ".json":
                files.append(entry)
    except PermissionError:
        # Try sudo ls — but this should be rare given the ACL grant.
        try:
            r = subprocess.run(
                ["sudo", "/bin/ls", str(outbox)],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                for name in r.stdout.splitlines():
                    name = name.strip()
                    if name and not name.startswith(".") and name.endswith(".json"):
                        files.append(outbox / name)
        except subprocess.SubprocessError:
            pass
    try:
        files.sort(key=lambda p: p.stat().st_mtime)
    except OSError:
        pass
    return files


def _archive_file(path: Path, bot_user: str) -> bool:
    """Move a processed outbox file into _ingested/<YYYY-MM-DD>/.

    Failure is not fatal — a stale file in audit_outbox/ gets re-processed
    next tick (safe, since ingest is idempotent). On a healthy pod the
    ``workspace/evolve`` ACL gives ``evolve`` rw, so the direct move always
    succeeds. The only realistic way it fails is the Linux ACL-mask-reset bug
    locking ``evolve`` out of ``workspace/evolve``; that condition is already
    observable — the persistent backlog surfaces via #3317's
    ``audit_drain_silent`` Signal — so we log the failure (not swallow it) and
    leave the record for the next tick rather than reaching for a privileged
    fallback. There is no sudoers grant for ``/bin/mv`` on per-bot outbox
    records, and the evolve daemon has no tty, so a sudo fallback could never
    fire anyway.
    """
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    dest_dir = _audit_outbox_ingested(bot_user) / today
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest_dir / path.name))
        return True
    except OSError as exc:
        # PermissionError is an OSError subclass — both land here. Logged,
        # not swallowed: the record stays in the outbox and re-processes next
        # tick (ingest is idempotent).
        logger.warning("audit_poller: archive failed for %s: %s", path, exc)
        return False


def _delete_file(path: Path) -> bool:
    """Delete a processed outbox file that carries no forensic value.

    Mirror of :func:`_archive_file` but unlinks instead of moving. Used when
    the drained record is a pure re-emission the signal/proposal store already
    holds (a dedup-hit or a no-op heartbeat) — keeping an ``_ingested/`` copy
    would be redundant. Reversible/non-destructive in the only sense that
    matters: the durable artifact lives in the signal/proposal store, not here.

    On PermissionError, falls back to ``sudo /bin/rm`` (the bot user owns the
    file; the evolve service user has read ACL but not delete). Failure is not
    fatal — a record we couldn't delete simply gets re-processed next tick
    (ingest is idempotent) exactly as a stale archive would have.
    """
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        # Already gone (concurrent tick / prior partial run) — treat as done.
        return True
    except PermissionError:
        # The bot owns the file and the workspace ACL didn't cover delete;
        # try the sudo /bin/rm fallback below (handled, not swallowed).
        try:
            r = subprocess.run(
                ["sudo", "/bin/rm", "-f", str(path)],
                capture_output=True, timeout=5,
            )
            return r.returncode == 0
        except subprocess.SubprocessError:
            return False
    except OSError as exc:
        logger.warning("audit_poller: delete failed for %s: %s", path, exc)
        return False


# ── Ingest one record ────────────────────────────────────────────────────────


def _ingest_finding(record: dict, shared_dir: Path) -> tuple[bool, bool]:
    """Ingest a tier2_finding record → Signal store.

    Returns ``(ok, archive)``:
      - ``ok``      — True when the record was processed successfully (and so
                      may be taken off the outbox).
      - ``archive`` — True when the drained record carries forensic value and
                      should be moved to ``_ingested/``; False when it is a
                      pure dedup-hit on an already-firing Signal and the
                      caller should DELETE it instead (the signal store already
                      holds the deduped live copy — the archived copy is pure
                      redundancy, ~95% of tier2_finding volume).

    Idempotent: signals.store.observe() dedupes by signature, so re-ingesting
    a record just bumps observation_count on the existing Signal.

    **PR 5: Provenance-gated emission.** Spec §8.1 — Tier 2 findings
    on observational fields are recorded in the trail but NOT emitted
    as Signals. The gate looks up
    ``manifest.provenance.field_origins.<field>.source`` for the field
    the assertion targets; observational → return True (record ingested,
    moved to _ingested) without firing a Signal; authored → normal
    emission.
    """
    try:
        from signals import store as signals_store
    except Exception as exc:
        logger.warning("audit_poller: signals.store import failed: %s", exc)
        return False, True

    signature = record.get("signature")
    if not signature:
        return False, True

    severity = _severity_for_signal(record.get("severity", "info"))
    bot_id = record.get("bot_id") or ""
    app_id = record.get("app_id") or ""
    summary = record.get("summary") or "structural finding"
    assertion_id = record.get("assertion_id") or ""

    # ── Discoverability trail-only gate ─────────────────────────────────
    # The five ``app_discoverability_*`` assertions stay in the per-app
    # trail (already written by the bot-side runner) but never become
    # Signals. See _DISCOVERABILITY_TRAIL_ONLY_ASSERTIONS for the
    # rationale. Same return shape as the observational gate below.
    if assertion_id in _DISCOVERABILITY_TRAIL_ONLY_ASSERTIONS:
        logger.info(
            "audit_poller: discoverability finding (trail-only, no Signal) "
            "bot=%s app=%s assertion=%s",
            bot_id, app_id, assertion_id,
        )
        # Trail-only suppression — never reaches the signal store. Keep the
        # archived copy (low-volume, forensically distinct from the dedup-hit
        # cut we make below on observed findings).
        return True, True

    # ── PR 5: provenance gate ───────────────────────────────────────────
    # When the finding targets an observational field on the manifest,
    # skip Signal emission entirely. The record is still archived (the
    # caller moves it to _ingested/) and the trail still captures it —
    # operators see the finding in the audit trail, not in chat.
    if _is_observational_finding(record, bot_id, app_id):
        logger.info(
            "audit_poller: observational finding (no Signal) bot=%s app=%s assertion=%s",
            bot_id, app_id, assertion_id,
        )
        # Observational suppression — never reaches the signal store. Keep
        # the archived copy (same rationale as the trail-only gate above).
        return True, True

    # Resolve a human-readable display name. ``display_name`` is "" when the
    # manifest can't be loaded (e.g. app deleted between scan and ingest);
    # the Signal *title* substitutes "An app" rather than leaking the cryptic
    # ``i-XXXXXXXX`` app_id (the title-quality fix). ``display`` keeps the
    # name-or-id fallback for ``details.display_name`` (the coalesced-group
    # header the Alerts UI already reads).
    display_name = _app_display_name(bot_id, app_id)
    display = display_name or app_id

    # Coalesce key — the runner emits one per (bot, app) so the four
    # structural-verifier findings against the same manifest collapse
    # into a single expandable row in /alerts. Plumbed onto Signal as
    # ``incident_key``, which the Alerts UI already groups by.
    # See internal/spec-recommendations-rework-2026-06-02.md.
    coalesce_key = record.get("coalesce_key") or None

    try:
        _signal, outcome = signals_store.observe_with_outcome(
            shared_dir=shared_dir,
            signature=signature,
            producer="app_structural_verifier",
            type=record.get("assertion_id") or "structural_finding",
            flavor="maintenance",
            severity=severity,
            scope="bot",
            bot_id=bot_id,
            title=_structural_finding_title(display_name, assertion_id),
            body=summary,
            details={
                "app_id": app_id,
                "bot_id": bot_id,
                "audit_run_id": record.get("audit_run_id"),
                "record_id": record.get("record_id"),
                "evidence": record.get("evidence") or {},
                "runner_version": record.get("runner_version"),
                # display_name carried so the Alerts UI can render a
                # coalesced group header without re-loading the manifest.
                "display_name": display,
            },
            incident_key=coalesce_key,
        )
        # Archive only when the observation carried NEW information — a fresh
        # Signal (created), a re-opened one (reopened), or a severity change
        # (changed). An unchanged dedup-hit or a dismissed-bump adds no
        # forensic value beyond the live Signal the store already holds, so
        # the caller DELETEs the outbox copy instead of archiving it.
        archive = outcome in ("created", "reopened", "changed")
        return True, archive
    except Exception as exc:
        logger.warning(
            "audit_poller: observe() failed for %s/%s: %s",
            bot_id, signature, exc,
        )
        return False, True


_SEVERITY_MAP = {
    "critical": "alert",
    "major":    "warn",
    "minor":    "info",
    "info":     "info",
}


def _severity_for_signal(audit_severity: str) -> str:
    """Translate the audit-runner's severity to a Signal severity."""
    return _SEVERITY_MAP.get((audit_severity or "").lower(), "info")


# ── Discoverability trail-only gate ─────────────────────────────────────────
#
# The structural verifier's five ``app_discoverability_*`` assertions flag
# manifest content gaps (no ``usage.model``, no ``how_to_use``/description/
# purpose, thin hint words, no ``example_triggers``, no CLI command). They
# are useful as audit-trail entries but generate enormous Signal-fanout
# (one per missing field × per app × per bot) for issues that are either
# scanner-fixable (``usage.model``, hint-word union — see scanner Pass D)
# or require authoring repair the operator can't action from a pager.
#
# Suppressing these at the Signal layer keeps the per-app trail at
# ``{workspace}/evolve/audits/{app_id}/trail.jsonl`` intact — the bot-side
# runner writes there before the outbox lands here, so the gate just
# declines to fan out to Signals. Once a per-app health chip ships on
# the Applications tile (paired with the chip work in the queue), that
# UI becomes the surface for the residual cases the scanner can't fix.
_DISCOVERABILITY_TRAIL_ONLY_ASSERTIONS = frozenset({
    "app_discoverability_no_invocation_model",
    "app_discoverability_no_how_to_use",
    "app_discoverability_thin_hint_words",
    "app_discoverability_no_example_triggers",
    "app_discoverability_no_cli",
})


# ── Human-readable structural-finding titles ────────────────────────────────
#
# The 2026-06-12 reports review found 100+ firing Signals whose title was just
# the signature echoed back — ``i-0bcaa46e: app_discoverability_no_cli`` — an
# ``<id>: <assertion_id>`` string no operator can parse. Each assertion_id
# maps to a short plain-English phrase that completes ``{App name}: {phrase}``
# (the same shape as the tier3 ``_problem_line`` headlines). Keep phrases
# under ~70 chars so ``{app}: {phrase}`` fits the title budget. Unknown
# assertion_ids fall back to a generic structural phrase — never the slug.
_ASSERTION_HEADLINE: dict[str, str] = {
    # Files
    "file_missing": "a file the app needs is missing",
    "file_sha_mismatch": "a file changed since it was registered",
    # Crons / scheduled scripts
    "cron_script_missing": "a scheduled script is missing",
    "cron_schedule_unparseable": "a schedule couldn't be parsed",
    "cron_not_in_crontab": "a scheduled job isn't installed",
    "cron_labels_loaded": "scheduled-job labels failed to load",
    "openclaw_cron_error": "a scheduled job is failing",
    "openclaw_cron_skipped": "a scheduled job is being skipped",
    "openclaw_cron_delivery_failure": "a scheduled job can't deliver its output",
    # Scheduled actions
    "scheduled_action_evidence_path": "a scheduled action's evidence path is wrong",
    "scheduled_action_anchor": "a scheduled action is missing its anchor",
    "scheduled_action_input_missing": "a scheduled action is missing required input",
    "scheduled_action_install_missing": "a scheduled action isn't installed",
    "scheduled_action_command_unresolvable": "a scheduled action's command can't be found",
    "scheduled_action_output_channel_invalid": "a scheduled action's output channel is invalid",
    "scheduled_action_orphan_install": "an installed job points at a removed scheduled action",
    "scheduled_action_section_drift": "the scheduled-actions list drifted from what's installed",
    # Heartbeat / delivery / packages / tests
    "heartbeat_anchors_present": "expected heartbeat anchors are missing",
    "delivery_contract_invalid": "the app's delivery contract is invalid",
    "delivery_contract_evidence_undeclared": "the app's delivery contract declares no evidence",
    "test_command_unresolvable": "the app's test command can't be run",
    "python_package_import_failed": "a required Python package won't import",
    # Structure / discoverability (the discoverability_* set is trail-only now,
    # mapped so any legacy firing Signal still renders a human title and in
    # case the gate is ever lifted)
    "app_no_producer_surface": "the app produces no user-visible output",
    "app_invocation_mode_not_subagent": "the app runs in the wrong invocation mode",
    "app_bot_guidance_oversized": "the app's bot guidance is too large",
    "app_cron_eligible_used_heartbeat": "the app uses a heartbeat where a schedule would fit",
    "app_discoverability_no_cli": "no CLI command to invoke it",
    "app_discoverability_no_example_triggers": "no example triggers showing how to use it",
    "app_discoverability_no_how_to_use": "no how-to-use guidance",
    "app_discoverability_no_invocation_model": "no invocation model declared",
    "app_discoverability_thin_hint_words": "too few hint words to be discoverable",
    "assertion_crashed": "an app structural check crashed while running",
}


def _structural_finding_title(display_name: str, assertion_id: str) -> str:
    """Human-readable Signal title for a structural-verifier finding.

    Renders ``{app}: {phrase}``. ``display_name`` is the resolved manifest
    display name, or "" when it couldn't be loaded — in which case the title
    says "An app" rather than leaking the cryptic ``i-XXXXXXXX`` app_id (the
    id stays recoverable via ``details.app_id``). The phrase comes from
    _ASSERTION_HEADLINE; unknown assertion_ids fall back to a generic
    structural phrase so a freshly-added assertion never echoes its slug as
    the entire title.
    """
    app = (display_name or "").strip() or "An app"
    phrase = _ASSERTION_HEADLINE.get(
        (assertion_id or "").strip(), "a structural check failed"
    )
    return f"{app}: {phrase}"


def _app_display_name(bot_id: str, app_id: str) -> str:
    """Resolve a human-readable display name for an app on a bot.

    Preference order: ``manifest.display_name`` → ``manifest.name`` →
    ``""`` (caller falls back to ``app_id``). The fallback matters
    because ``app_id`` is ``manifest.id``, which for v7-migrated /
    forge-minted apps is an ``i-XXXXXXXX`` 8-hex-char ID — unreadable
    in a Signal title.

    Defensive: any failure (no bot/app, missing manifest, load error)
    returns ``""`` so the caller falls back to the cryptic-but-stable
    ``app_id``. Best-effort only — never raise.
    """
    if not bot_id or not app_id:
        return ""
    try:
        from .manifest import load_manifest
    except Exception:
        return ""
    try:
        manifest = load_manifest(app_id, bot_id, Path("."))
    except Exception:
        return ""
    if manifest is None:
        return ""
    for attr in ("display_name", "name"):
        name = (getattr(manifest, attr, "") or "").strip()
        if name:
            return name
    return ""


# ── PR 5: provenance gate ───────────────────────────────────────────────────
# Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §8.1.
#
# The assertion→field map and the observational decision are the
# SINGLE SOURCE OF TRUTH in ``app_audit_structural`` (analyzer), imported
# by both this admin-side poller and the bot-side runner so they cannot
# drift. The runner now applies the same gate UPSTREAM (it skips the outbox
# write for observational findings, so they're never shipped); this poller
# keeps the gate as a backstop for records from older runners.


def _is_observational_finding(record: dict, bot_id: str, app_id: str) -> bool:
    """True iff the finding targets a field whose provenance is
    observational on the bot's manifest.

    Loads the manifest (admin-side) and delegates the actual decision to
    ``app_audit_structural.is_observational_finding`` — the shared predicate.
    Conservative: returns False (emit Signal) when the assertion_id is
    unknown, the manifest can't be loaded, there's no provenance block, or
    the field's source isn't an explicit ``"observational"``.
    """
    assertion_id = record.get("assertion_id") or ""
    if not assertion_id or not bot_id or not app_id:
        return False
    try:
        from .manifest import load_manifest
    except Exception:
        return False
    try:
        manifest = load_manifest(app_id, bot_id, Path("."))
    except Exception:
        return False
    if manifest is None:
        return False
    provenance = getattr(manifest, "provenance", None)
    try:
        from app_audit_structural import is_observational_finding
    except Exception:
        # Analyzer module unreachable — err on the side of emitting.
        return False
    return is_observational_finding(assertion_id, provenance)


def _assertion_id_from_signature(signature: str) -> str:
    """Extract the assertion_id from an app_structural_verifier signature.

    Signature shape (``app_audit_structural.Finding.signature``):
      ``app_structural_verifier:{assertion_id}:{bot_id}:{app_id}:{ev_key}``
    Bot-scoped variants omit ``app_id`` but the assertion_id is always field 1
    (assertion_ids are snake_case and carry no ``:``). Returns ``""`` for any
    string that isn't a structural-verifier signature.
    """
    parts = (signature or "").split(":")
    if len(parts) >= 2 and parts[0] == "app_structural_verifier":
        return parts[1]
    return ""


def _run_summary_is_noop(record: dict) -> bool:
    """True when a run_summary is a no-op heartbeat with no forensic value.

    A no-op summary audited zero apps AND surfaced no findings/outcomes — it
    is the periodic "I woke up and there was nothing to do" tick. Those make
    up the bulk of the archived run-summary churn (~731 ``apps_audited:0``
    records in the live review). Summaries that actually audited apps, found
    something, or carried outcomes are kept (they record real work).

    Conservative by construction: any non-zero signal of activity
    (``apps_audited``, ``total_findings``, a non-empty ``kept_signatures``,
    or any non-zero ``outcomes`` count) flips this to False → KEEP.
    """
    def _as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    if _as_int(record.get("apps_audited")) > 0:
        return False
    if _as_int(record.get("total_findings")) > 0:
        return False
    if _as_int(record.get("apps_with_findings")) > 0:
        return False
    if record.get("kept_signatures"):
        return False
    outcomes = record.get("outcomes")
    if isinstance(outcomes, dict) and any(_as_int(v) > 0 for v in outcomes.values()):
        return False
    return True


def _ingest_run_summary(record: dict, shared_dir: Path) -> int:
    """Process a tier2_run_summary → sweep_resolve.

    Auto-resolves any active ``app_structural_verifier`` Signal for this bot
    whose signature isn't in this run's kept_signatures. Returns the count
    of signals resolved.

    The ``bot_ids={bot_id}`` filter on sweep_resolve is load-bearing:
    ``producer="app_structural_verifier"`` is shared pod-wide, but
    ``kept_signatures`` carries only this bot's signatures. Without the
    filter, every bot's run-summary processing would mass-resolve every
    *other* bot's still-firing structural findings.

    Trail-only (discoverability) signatures are dropped from the keep-set
    before the sweep. The bot-side runner still lists every finding's
    signature in ``kept_signatures``, including the ``app_discoverability_*``
    findings that ``_ingest_finding`` suppresses (they never become Signals —
    see ``_DISCOVERABILITY_TRAIL_ONLY_ASSERTIONS``). Leaving them in the keep
    set permanently protects any *pre-gate* discoverability Signal — one
    emitted before the trail-only gate shipped — from ever being swept: it can
    never be re-observed (the poller now suppresses it) nor resolved (its
    signature is "kept"), so it strands in ``firing/`` forever. Stripping them
    lets sweep_resolve archive those stranded legacy Signals (56 of them from a
    single 06-08 run were still firing two runs later in the 2026-06-12
    review). Genuine, still-firing discoverability conditions don't reappear as
    Signals — they remain trail-only by design.
    """
    try:
        from signals import store as signals_store
    except Exception:
        return 0
    bot_id = record.get("bot_id") or ""
    if not bot_id:
        return 0
    kept = {
        s for s in (record.get("kept_signatures") or [])
        if _assertion_id_from_signature(s)
        not in _DISCOVERABILITY_TRAIL_ONLY_ASSERTIONS
    }
    try:
        resolved = signals_store.sweep_resolve(
            shared_dir=shared_dir,
            producer="app_structural_verifier",
            kept_signatures=kept,
            bot_ids={bot_id},
            reason="audit_poller: bot reports cleared structural finding",
        )
        return len(resolved) if isinstance(resolved, list) else 0
    except Exception as exc:
        logger.warning(
            "audit_poller: sweep_resolve failed for bot %s: %s", bot_id, exc,
        )
        return 0


# ── Tier-3 ingestion ─────────────────────────────────────────────────────────


def _ingest_tier3_finding(
    record: dict,
    shared_dir: Path,
    *,
    superseded_runs: set[tuple[str, str, str]] | None = None,
) -> tuple[bool, bool]:
    """Turn a tier3_finding record into an Investigation Proposal.

    Returns ``(ok, archive)`` — ``archive`` is False when an open Proposal
    with this finding's trigger already existed (a pure re-emission the
    proposal store already holds), so the caller DELETEs the outbox copy
    instead of archiving it. This is the tier3 analogue of the tier2
    dedup-hit cut.

    Stage 3b decided the operator should see this. Investigation is the
    right action kind — apply doesn't change state, it just acknowledges.
    The audit's context lives in the Proposal's `action.context` markdown.

    Idempotent: dedup'd by record signature; an existing Proposal with the
    same trigger_observation is left untouched.

    Before writing the new Proposal, archives any pending app_audit_tier3
    Proposals for the same (bot, app) that came from a *prior* audit run —
    see :func:`_supersede_prior_tier3_proposals` for the rationale. The
    ``superseded_runs`` memo (one entry per (bot, app, run) triple) keeps
    the per-tick scan from running once per finding when a single run
    emits multiple findings for the same app.
    """
    bot_id = record.get("bot_id") or ""
    app_id = record.get("app_id") or ""
    new_run_id = record.get("audit_run_id") or ""
    if superseded_runs is not None and bot_id and app_id and new_run_id:
        key = (bot_id, app_id, new_run_id)
        if key not in superseded_runs:
            _supersede_prior_tier3_proposals(
                shared_dir=shared_dir,
                bot_id=bot_id,
                app_id=app_id,
                new_audit_run_id=new_run_id,
            )
            superseded_runs.add(key)

    dedup_out: dict = {}
    ok = _emit_audit_proposal(
        shared_dir=shared_dir,
        record=record,
        generator_id="app_audit_tier3",
        dimension="reliability",
        context_builder=_render_finding_context,
        compute_dispatch=_dispatch_for_tier3_finding,
        compute_coalesce=_coalesce_for_tier3_finding,
        dedup_out=dedup_out,
    )
    # Archive only when a fresh Proposal was written. A dedup-hit (open
    # Proposal already exists) adds no forensic value — delete the outbox
    # copy. On a write failure (ok False) we keep/archive so nothing is
    # lost; dedup_out["dedup"] defaults to absent → archive=True.
    archive = bool(dedup_out.get("dedup")) is False
    return ok, archive


def _coalesce_for_tier3_finding(
    record: dict,
) -> tuple[Optional[str], Optional[str]]:
    """Group all tier3 findings for one ``(bot, app)`` under one parent.

    A single audit run typically surfaces 1-5 findings per app
    (broken_path / behavior_mismatch / missing_functionality / …) and
    each lands as its own outbox record. Folding them into one parent
    Proposal collapses the queue without losing per-finding evidence
    (each lands in ``sub_findings`` on the parent).

    **Grain: ``(bot, app)``, deliberately NOT ``(bot, app, audit_run)``.**
    The earlier per-run grain made *coalescing* the within-run collapse and
    leaned entirely on :func:`_supersede_prior_tier3_proposals` to fold the
    *cross-run* duplicates — so a missed supersede (daemon downtime, an
    operator-engaged prior card, or simply code that predated supersede)
    let every run pile a fresh card on top of the last. That pile-up is
    exactly the 118-cards / 30-(bot,app)-pairs symptom the 2026-06-12 pod
    review found. Keying on ``(bot, app)`` makes coalescing itself the
    cross-run backstop: ``arbiter.store`` folds the next run's findings into
    the existing pending parent (deduped by ``trigger_observations[0]``) even
    if supersede never fires. This matches the sibling structural-finding
    grain (``app_structural:{bot}:{app}``) and ``model_discovery``'s
    time-free ``model_discovery:{provider}`` grain (the recommendation-
    legibility precedent, design-recommendation-legibility-2026-06-12.md).
    Supersede still runs as the *pruning* step — it archives the prior
    run's operator-untouched parent so stale sub_findings don't accumulate
    forever (coalescing only appends, never removes); the ``audit_run_id``
    it keys on lives in ``provenance.signals``, not the coalesce_key.

    Returns ``(coalesce_key, human_title)``; both ``None`` if the record
    is missing the identifiers needed to group safely. ``human_title`` is
    COUNT-AGNOSTIC — the UI's sub-findings badge supplies the live count, so
    the title stays correct as findings fold in or get resolved out.
    """
    bot_id = (record.get("bot_id") or "").strip()
    app_id = (record.get("app_id") or "").strip()
    if not (bot_id and app_id):
        return None, None
    coalesce_key = f"app_audit_tier3:{bot_id}:{app_id}"
    human_title = f"{app_id}: audit findings"
    return coalesce_key, human_title


# ── Tier-3 supersede (Finding 1 Phase 3, 2026-06-09) ─────────────────────────
#
# Tier 3 caps at 5 Proposals per (bot, app, run) but does NOT supersede prior
# runs. Without this archive step, next week's audit of a given (bot, app)
# adds 5 fresh Proposals on top of the 5 already in pending/ from the prior
# run, even when the operator hasn't engaged. The accumulation
# is the second half of Finding 1 in
# docs/proposal-root-cause-audit-2026-06-09.md (the first half is the 7-day
# staleness archive, tracked separately).
#
# Scope: only ``app_audit_tier3``. Other generators don't have the per-run
# accumulation pattern — sweep_resolve / signal dedup already handles them.
# Don't generalize until a second generator exhibits the same shape.


def _supersede_prior_tier3_proposals(
    *,
    shared_dir: Path,
    bot_id: str,
    app_id: str,
    new_audit_run_id: str,
) -> int:
    """Archive pending app_audit_tier3 Proposals for this (bot, app) that
    were emitted by a prior audit run. Returns the count archived.

    Skipped:
      - Proposals whose ``provenance.signals.audit_run_id`` matches the
        incoming run (they're not "prior").
      - Proposals the operator has engaged with — ``len(history) > 1``,
        i.e. anything beyond the initial draft→pending promotion the
        poller itself wrote. Snoozes, dismissal-starts, etc. stay put.
      - Snoozed Proposals (we only iterate ``pending/``).
    """
    if not bot_id or not app_id or not new_audit_run_id:
        return 0
    try:
        from arbiter import store as arbiter_store
        from arbiter.state_machine import IllegalTransitionError, transition
    except Exception as exc:
        logger.warning(
            "audit_poller: tier3 supersede arbiter import failed: %s", exc,
        )
        return 0

    try:
        candidates = list(
            arbiter_store.iter_proposals(shared_dir, subdirs=("pending",))
        )
    except Exception as exc:
        logger.warning(
            "audit_poller: tier3 supersede iter failed for %s/%s: %s",
            bot_id, app_id, exc,
        )
        return 0

    superseded = 0
    for prop in candidates:
        if getattr(prop, "generator_id", "") != "app_audit_tier3":
            continue
        if getattr(prop, "bot_id", "") != bot_id:
            continue
        prov = getattr(prop, "provenance", None)
        signals = (
            (getattr(prov, "signals", None) or {}) if prov is not None else {}
        )
        if signals.get("app_id") != app_id:
            continue
        prior_run_id = signals.get("audit_run_id")
        if not prior_run_id or prior_run_id == new_audit_run_id:
            continue

        history = getattr(prop, "history", None) or []
        if len(history) > 1:
            # Operator engaged (snoozed, started dismissal, etc.) — leave alone.
            continue

        try:
            transition(
                prop, "resolved_externally",
                actor="app_audit_tier3",
                reason=f"superseded by {new_audit_run_id}",
            )
        except IllegalTransitionError:
            continue
        try:
            arbiter_store.move_proposal(
                prop, shared_dir, from_subdir="pending",
            )
            superseded += 1
        except OSError as exc:
            logger.warning(
                "audit_poller: tier3 supersede move failed for %s: %s",
                getattr(prop, "id", "?"), exc,
            )
    return superseded


# Audit categories whose fix lives inside one bot's workspace — the bot
# has the context, credentials, and scope. drift (TAG_ALIASES, persona-style)
# stays operator-only; the spec calls it out as needing human judgment.
# Anything not in this set defaults to operator-only (None).
_BOT_DISPATCHABLE_CATEGORIES = frozenset({
    "broken_path",
    "missing_functionality",
    "behavior_mismatch",
    "dead_code",
    "manifest_drift",
})


def _dispatch_for_tier3_finding(
    record: dict, proposal_id: str,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve ``(dispatch_target, dispatch_message)`` for a tier3 finding.

    Spec: internal/spec-take-this-on-evo-dispatch-2026-06-04.md §"audit_poller
    mapping". Categories whose fix lives in one bot dispatch to that bot;
    `drift` (and anything unknown) stays operator-only.
    """
    category = (record.get("category") or "").strip().lower()
    bot_id = (record.get("bot_id") or "").strip()
    if not bot_id or category not in _BOT_DISPATCHABLE_CATEGORIES:
        return None, None
    app_id = record.get("app_id") or "unknown"
    description = (record.get("description") or "").strip() or "(no description)"
    message = (
        f"Investigate the {category} finding in app `{app_id}`:\n\n"
        f"{description}\n\n"
        f"Either fix the implementation to match the manifest claim "
        f"or update the manifest if the feature isn't intended. "
        f"Reference: proposal id {proposal_id}."
    )
    return bot_id, message


def _ingest_tier3_conflict_notice(record: dict, shared_dir: Path) -> bool:
    """Turn a conflict_notice record into an Investigation Proposal.

    Auto_fix tried to touch a file used by another app. The runner refused;
    operator decides whether to coordinate, drop the dependency, or accept.
    See spec §5.6.
    """
    return _emit_audit_proposal(
        shared_dir=shared_dir,
        record=record,
        generator_id="app_audit_tier3_conflict",
        dimension="reliability",
        context_builder=_render_conflict_context,
    )


def _emit_audit_proposal(
    *,
    shared_dir: Path,
    record: dict,
    generator_id: str,
    dimension: str,
    context_builder,
    compute_dispatch: Optional[
        Callable[[dict, str], tuple[Optional[str], Optional[str]]]
    ] = None,
    compute_coalesce: Optional[
        Callable[[dict], tuple[Optional[str], Optional[str]]]
    ] = None,
    dedup_out: Optional[dict] = None,
) -> bool:
    """Write a Proposal via arbiter.store.write_proposal. Idempotent.

    Best-effort: failures land in PollResult.errors but don't abort the
    drain — losing one proposal is preferable to losing all of them.

    ``dedup_out``, when provided, has its ``"dedup"`` key set to True when an
    open Proposal with this trigger already existed (so no new Proposal was
    written — a pure re-emission) and False when a fresh Proposal was written.
    The tier3_finding ingest path reads this to decide archive-vs-delete on
    the drained outbox record; callers that don't care leave it None.

    ``compute_dispatch``, when provided, is called with ``(record, proposal_id)``
    and returns ``(dispatch_target, dispatch_message)``. Tier-3 audit findings
    pass a resolver derived from the spec mapping table; conflict notices and
    infra findings pass nothing, leaving both fields ``None``. See
    internal/spec-take-this-on-evo-dispatch-2026-06-04.md §"audit_poller mapping".

    ``compute_coalesce``, when provided, returns ``(coalesce_key,
    human_title)`` so multiple findings sharing a root cause (e.g. all
    tier3 findings from one audit run on one app) fold into one parent
    Proposal via :func:`arbiter.store.write_proposal`'s coalescing path.
    """
    try:
        from arbiter import store as arbiter_store
        from arbiter.state_machine import transition
        from schema.proposal import (
            Investigation,
            Proposal,
            Provenance,
            RiskTag,
            new_proposal_id,
        )
    except Exception as exc:
        # The arbiter module lives in packages/analyzer/; if it isn't
        # importable from the admin server's PYTHONPATH, log and bail.
        logger.warning("audit_poller: arbiter import failed: %s", exc)
        return False

    signature = record.get("signature") or ""
    bot_id = record.get("bot_id") or ""
    app_id = record.get("app_id") or ""
    if not signature or not bot_id:
        return False

    trigger_obs = f"app_audit:{generator_id}:{signature}"

    # Idempotency: skip when an open Proposal with this trigger already exists.
    # We scan pending+snoozed for any proposal whose trigger_observations list
    # contains our signature-derived id. (The arbiter's fingerprint-based
    # `find_open_duplicate` operates on a fully-constructed Proposal, which
    # is more rigid than we need; a trigger-id match is the natural dedup
    # key for audit findings.)
    try:
        for existing in arbiter_store.iter_proposals(
            shared_dir, subdirs=("pending", "snoozed"),
        ):
            if trigger_obs in (existing.trigger_observations or []):
                if dedup_out is not None:
                    dedup_out["dedup"] = True
                return True
    except Exception:
        # If iteration fails, fall through and write a new proposal — duplicate
        # is preferable to losing the finding.
        pass

    if dedup_out is not None:
        dedup_out["dedup"] = False

    context = context_builder(record)
    problem = _problem_line(record)
    summary = _summary_line(record)
    proposal_id = new_proposal_id()

    if compute_dispatch is not None:
        dispatch_target, dispatch_message = compute_dispatch(record, proposal_id)
    else:
        dispatch_target, dispatch_message = None, None

    if compute_coalesce is not None:
        coalesce_key, human_title = compute_coalesce(record)
    else:
        coalesce_key, human_title = None, None

    try:
        proposal = Proposal(
            id=proposal_id,
            bot_id=bot_id,
            generator_id=generator_id,
            dimension=dimension,
            trigger_observations=[trigger_obs],
            provenance=Provenance(
                technique=f"{generator_id}.v1",
                signals={
                    "audit_run_id": record.get("audit_run_id"),
                    "record_id": record.get("record_id"),
                    "obs_id": record.get("obs_id"),
                    "category": record.get("category"),
                    "severity": record.get("severity"),
                    "app_id": app_id,
                },
                confidence=_confidence_from_severity(record.get("severity")),
            ),
            problem=problem,
            action=Investigation(context=context),
            risk_tag=RiskTag(
                blast_radius="bot",
                reversibility="manual",
                touches=["app_manifest"],
            ),
            # An audit finding is an Investigation: apply only acknowledges,
            # there is no metric to verify after the fact. A Claim is a
            # falsifiable verify-after-apply predicate, so leaving it None is
            # correct — never fabricate one to fill the field.
            claim=None,
            approval_audience="pod_admin",
            urgency=_urgency_from_severity(record.get("severity")),
            # ``summary`` switches the proposal-detail renderer to the
            # five-section operator-first layout (Title / Summary / Proposed
            # Action / Explanation / Details). It projects detail that already
            # exists on the finding into a terse line up front; the full
            # description + evidence stay in action.context for the drill-down.
            summary=summary,
            admin_surface_summary=problem[:120],
            # Stable per-FINDING dismiss signature, keyed on the finding
            # identity (``app_audit_tier3:{category}:{bot}:{app}:{digest}``),
            # NOT the (bot, app) coalesce grain — dismissing one finding must
            # never suppress all of an app's audit advisories. Setting it
            # explicitly (rather than leaning on the trigger-derived default)
            # follows the schema's guidance so a future change to the
            # trigger-observation shape doesn't invalidate prior dismissals.
            dismiss_signature=signature,
            dispatch_target=dispatch_target,
            dispatch_message=dispatch_message,
            coalesce_key=coalesce_key,
            human_title=human_title,
        )
        # Promote draft → pending so the admin UI's Self-Improvement queue
        # surfaces the finding. Without this, the proposal stays at the
        # schema-default status of "draft" and the UI's status filter
        # (p.status === 'pending') hides it. Audit findings are
        # operator-actionable end-of-pipeline; there's no separate ingest
        # step downstream that would otherwise promote them.
        transition(
            proposal, "pending",
            actor=generator_id, reason="audit_poller ingest",
        )
        arbiter_store.write_proposal(proposal, shared_dir)
        return True
    except Exception as exc:
        logger.warning(
            "audit_poller: proposal write failed for %s/%s: %s",
            bot_id, signature, exc,
        )
        return False


# Category → plain-English headline. Slice 2 of the post-revert
# recommendations review (2026-06-04): the prior "{app_id}: <full LLM
# description>" titles were a wall of jargon ("...states 'Commitments
# captured in memory/contacts/{person}.md and surfaced as follow-ups
# within 24..."). Replace with a short category-driven line; the LLM
# description still lives in the proposal body for operators who want
# the details. Categories mirror VALID_CATEGORIES in
# packages/analyzer/app_audit_tier3.py — if a category is added there,
# add a phrase here too (the fallback handles unknowns gracefully).
_CATEGORY_HEADLINE = {
    "broken_path": "a path or reference doesn't resolve",
    "code_smell": "code pattern looks risky",
    "behavior_mismatch": "code doesn't match what the manifest claims",
    "dead_code": "unused code path",
    "manifest_drift": "manifest is out of date with reality",
    "missing_functionality": (
        "manifest claims a feature that isn't fully wired up"
    ),
    # Substrate ingestion + run-summary records use these slugs too.
    "structural_finding": "structural audit finding",
    "skill_finding": "skill audit finding",
    "provider_finding": "provider audit finding",
}


def _problem_line(record: dict) -> str:
    """Build the operator-visible title for an audit finding.

    Format: ``{app_id}: {category_phrase}``. The LLM's verbose
    description goes in the proposal body via ``_audit_context_builder``
    (further down in this module), so operators who want details can
    expand the proposal; operators scanning the queue see a
    consistent, parseable line.

    Spec: internal/spec-take-this-on-evo-dispatch-2026-06-04.md §"audit_poller
    mapping" + the post-revert recommendations review feedback
    "titles and text are really technical and hard to understand."
    """
    app_id = record.get("app_id") or "unknown"
    category = str(record.get("category") or "").strip().lower()
    phrase = _CATEGORY_HEADLINE.get(category) or (
        # Unknown category — fall back to the category slug itself
        # (better than nothing; surfaces missing entries in
        # _CATEGORY_HEADLINE for follow-up).
        category or "structural finding"
    )
    return f"{app_id}: {phrase}"


# Summary length budget. The operator-first card layout (triggered when
# ``Proposal.summary`` is set) shows the summary as a 1-2 line plain-English
# gloss under the title; the full finding detail (description, evidence,
# triage rationale) stays in ``action.context`` for the drill-down. ~240 chars
# keeps it to roughly the ~45-word digestible-card budget from
# internal/design-recommendation-legibility-2026-06-12.md without clipping a
# normal one-sentence finding.
_SUMMARY_MAX_CHARS = 240


def _summary_line(record: dict) -> str:
    """Terse, plain-English one-liner for the operator-first card layout.

    Projects detail that ALREADY exists on the finding record into
    ``Proposal.summary`` — presentation-synthesis, not new content. The
    category headline (``_problem_line``) is the card *title*; this summary
    is the gloss beneath it, so it surfaces the finding's own words rather
    than restating the category.

    Preference order:
      1. An explicit ``summary`` the producer already wrote (conflict
         notices carry one).
      2. The finding ``description``, clamped to the first sentence(s)
         within the char budget.
      3. The category headline (``_problem_line``) when there's no prose —
         degrades to the title rather than rendering an empty summary.
    """
    explicit = (record.get("summary") or "").strip()
    if explicit:
        return _clamp_summary(explicit)
    description = (record.get("description") or "").strip()
    if description:
        return _clamp_summary(description)
    return _problem_line(record)


def _clamp_summary(text: str) -> str:
    """Trim ``text`` to the first sentence(s) within ``_SUMMARY_MAX_CHARS``.

    Collapses whitespace, then keeps whole sentences until adding the next
    would exceed the budget; if even the first sentence is over budget, hard-
    truncates on a word boundary with an ellipsis. Never returns mid-word.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= _SUMMARY_MAX_CHARS:
        return collapsed
    out = ""
    for sentence in re.split(r"(?<=[.!?])\s+", collapsed):
        candidate = f"{out} {sentence}".strip() if out else sentence
        if len(candidate) > _SUMMARY_MAX_CHARS:
            break
        out = candidate
    if out:
        return out
    # First sentence alone exceeds the budget — hard truncate on a word
    # boundary so the card never shows a clipped mid-word fragment.
    clipped = collapsed[:_SUMMARY_MAX_CHARS].rsplit(" ", 1)[0]
    return clipped.rstrip(",.;:") + "…"


# ── Substrate ingestion (skill + provider) ──────────────────────────────────
#
# Substrate findings come from the bot-side skill / provider audits
# (Workstream B-skills). Each finding becomes a Proposal with source
# attribution carrying ``skill_audit:<skill>`` or
# ``provider_audit:<provider>``. Same idempotency + signature-dedup logic
# as Tier-3 ingestion; just a different generator_id + element naming.


def _ingest_skill_finding(record: dict, shared_dir: Path) -> bool:
    """Turn a skill_finding outbox record into a Proposal in the arbiter.

    Idempotent: signature-prefixed trigger_observation entry; existing
    pending+snoozed Proposals carrying the same trigger are left alone.
    """
    skill_id = record.get("skill_id") or "unknown"
    return _emit_substrate_proposal(
        shared_dir=shared_dir,
        record=record,
        element_type="skill",
        element_id=skill_id,
        generator_id=f"skill_audit:{skill_id}",
        dimension="reliability",
    )


def _ingest_provider_finding(record: dict, shared_dir: Path) -> bool:
    """Turn a provider_finding outbox record into a Proposal."""
    provider_id = record.get("provider_id") or "unknown"
    return _emit_substrate_proposal(
        shared_dir=shared_dir,
        record=record,
        element_type="provider",
        element_id=provider_id,
        generator_id=f"provider_audit:{provider_id}",
        dimension="reliability",
    )


def _emit_substrate_proposal(
    *,
    shared_dir: Path,
    record: dict,
    element_type: str,           # "skill" | "provider"
    element_id: str,             # skill_id or provider_id
    generator_id: str,
    dimension: str,
) -> bool:
    """Write a substrate-audit Proposal via arbiter.store.write_proposal.

    Reuses the existing Investigation action shape — the audit's context
    lives in `action.context` as Markdown. The trigger_observation field
    carries a stable signature so re-ingesting the same record is a no-op.
    """
    try:
        from arbiter import store as arbiter_store
        from arbiter.state_machine import transition
        from schema.proposal import (
            Investigation,
            Proposal,
            Provenance,
            RiskTag,
            new_proposal_id,
        )
    except Exception as exc:
        logger.warning("audit_poller: arbiter import failed: %s", exc)
        return False

    signature = record.get("signature") or ""
    bot_id = record.get("bot_id") or ""
    if not signature or not bot_id:
        return False

    trigger_obs = f"{element_type}_audit:{generator_id}:{signature}"

    # Idempotency check — same shape as _emit_audit_proposal.
    try:
        for existing in arbiter_store.iter_proposals(
            shared_dir, subdirs=("pending", "snoozed"),
        ):
            if trigger_obs in (existing.trigger_observations or []):
                return True
    except Exception:
        pass

    description = (record.get("description") or "").strip()
    severity = record.get("severity") or "info"
    rationale = (record.get("rationale") or "").strip()
    evidence = record.get("evidence") or []

    problem = (
        f"{element_type.title()} audit ({record.get('category') or 'audit'}) "
        f"on `{element_id}`: {description[:150]}"
    )

    context_lines = [
        f"## {element_type.title()} audit finding — `{element_id}` ({severity})",
        "",
        description or "(no description)",
        "",
    ]
    if evidence:
        context_lines.append("**Evidence:**")
        for ev in evidence:
            context_lines.append(f"- `{ev}`")
        context_lines.append("")
    if rationale:
        context_lines.append(f"**Triage rationale:** {rationale}")
        context_lines.append("")
    context_lines.append(
        f"_Audit run: `{record.get('audit_run_id', 'unknown')}` · "
        f"record: `{record.get('record_id', 'unknown')}`. "
        f"Mark as accepted via `evo audit {element_type} accept "
        f"{element_id} <signature>` to stop this from re-raising._"
    )
    context = "\n".join(context_lines)

    try:
        proposal = Proposal(
            id=new_proposal_id(),
            bot_id=bot_id,
            generator_id=generator_id,
            dimension=dimension,
            trigger_observations=[trigger_obs],
            provenance=Provenance(
                technique=f"{element_type}_audit.v1",
                signals={
                    "audit_run_id": record.get("audit_run_id"),
                    "record_id": record.get("record_id"),
                    "obs_id": record.get("obs_id"),
                    "category": record.get("category"),
                    "severity": severity,
                    "element_type": element_type,
                    "element_id": element_id,
                },
                confidence=_confidence_from_severity(severity),
            ),
            problem=problem,
            action=Investigation(context=context),
            risk_tag=RiskTag(
                blast_radius="bot",
                reversibility="manual",
                touches=[f"{element_type}_config"],
            ),
            claim=None,
            approval_audience="pod_admin",
            urgency=_urgency_from_severity(severity),
            admin_surface_summary=problem[:120],
        )
        transition(
            proposal, "pending",
            actor=generator_id, reason="audit_poller ingest",
        )
        arbiter_store.write_proposal(proposal, shared_dir)
        return True
    except Exception as exc:
        logger.warning(
            "audit_poller: %s proposal write failed for %s/%s: %s",
            element_type, bot_id, signature, exc,
        )
        return False


def _render_finding_context(record: dict) -> str:
    """Multi-paragraph context for an audit finding Proposal."""
    app_id = record.get("app_id") or "unknown"
    severity = record.get("severity") or "info"
    description = (record.get("description") or "").strip()
    rationale = (record.get("rationale") or "").strip()
    evidence = record.get("evidence") or []

    lines = [
        f"## Audit finding — app `{app_id}` ({severity})",
        "",
        description,
        "",
    ]
    if evidence:
        lines.append("**Evidence:**")
        for ev in evidence:
            lines.append(f"- `{ev}`")
        lines.append("")
    if rationale:
        lines.append(f"**Triage rationale:** {rationale}")
        lines.append("")
    lines.append(
        f"_Audit run: `{record.get('audit_run_id', 'unknown')}` · "
        f"record: `{record.get('record_id', 'unknown')}`. "
        f"Mark as accepted via `evo audit accept <id>` or the manifest's "
        f"Accepted findings panel to stop this from re-raising._"
    )
    return "\n".join(lines)


def _render_conflict_context(record: dict) -> str:
    """Multi-paragraph context for an audit-conflict-notice Proposal."""
    app_id = record.get("app_id") or "unknown"
    file_path = record.get("file_path") or "?"
    affected = record.get("affected_apps") or []

    lines = [
        f"## Cross-app conflict on `{file_path}`",
        "",
        f"App `{app_id}`'s audit wanted to auto-fix this file, but the file "
        f"is also referenced by other apps. The auto-fix was deferred to "
        f"avoid breaking them.",
        "",
        "**Affected apps:**",
    ]
    for a in affected:
        role = a.get("role") or "?"
        name = a.get("display_name") or a.get("app_id") or "?"
        lines.append(f"- `{a.get('app_id', '?')}` — {name} ({role})")
    lines.append("")
    lines.append(
        "**Resolution options:**\n"
        "1. Update the manifests of every affected app together (recommended "
        "for coordinated apps).\n"
        "2. Remove this file from the dependent manifests if they no longer "
        "need it.\n"
        "3. Mark this finding as accepted to acknowledge the divergence "
        "without acting on it."
    )
    return "\n".join(lines)


# ── Investigation ingestion (Workstream C — evo fail) ──────────────────────
#
# Two record kinds:
#   investigation_diagnosis  — runner produced a diagnosis (or "no diagnosis").
#       Routes to a notification on the requesting user's queue. NEVER
#       becomes a Proposal — per direct-reply design, the diagnosis lives
#       in the message thread, not the dashboard.
#   investigation_unresolved — user escalated via `evo fail flag` after
#       a no-diagnosis or unsatisfying reply. Becomes a Proposal in the
#       arbiter so an operator can investigate manually.
#
# Spec: internal/spec-audit-extensions-2026-05-17.md §5.6 deliverable 5.


def _ingest_investigation_diagnosis(record: dict, shared_dir: Path) -> bool:
    """Emit a notification to the requesting user's queue. No Proposal.

    The notification carries the diagnosis text in ``detail`` so the
    session_surface hook injects it at the user's next interaction (or
    the plugin direct-sends it when the surface supports it). The full
    structured record is already preserved on the bot side in the
    investigation trail; we only need to deliver the conversational
    reply here.
    """
    try:
        from evolve_admin.evo import notifications as evo_notifications
    except Exception as exc:
        logger.warning(
            "audit_poller: evo.notifications import failed for "
            "investigation_diagnosis: %s",
            exc,
        )
        return False

    # Lazy-import the renderer co-located with the runner module. Same
    # rationale as render_notification_detail living in analyzer/ — keeps
    # the notification template close to the record producer so they
    # stay in sync.
    try:
        from app_audit_investigation import render_notification_detail
    except Exception:
        # Fallback: render here from the record fields. Less polished but
        # we never want to drop a user's reply.
        render_notification_detail = _fallback_render_notification

    requesting_user = (record.get("requesting_user") or "").strip()
    bot_id = (record.get("bot_id") or "").strip()
    if not requesting_user or not bot_id:
        return False

    # Per-user admin lookup + trail URL build. The renderer only emits
    # the trail link when both conditions hold (admin AND non-None URL).
    # Spec: internal/spec-audit-extensions-2026-05-17.md §5.3 + the audit-
    # extensions follow-up brief (Item 3).
    is_pod_admin = False
    trail_link: str | None = None
    try:
        from ..config import load_network
        network = load_network()
        is_pod_admin = _requesting_user_is_pod_admin(requesting_user, network)
        if is_pod_admin:
            investigation_id = (record.get("investigation_id") or "").strip()
            if investigation_id:
                trail_link = _build_investigation_trail_url(
                    network, investigation_id,
                )
    except Exception as exc:
        # Don't block the notification on a URL-build failure — the
        # body without the link is still useful.
        logger.warning(
            "audit_poller: admin/url resolution failed for %s: %s",
            requesting_user, exc,
        )

    detail = render_notification_detail(
        record, is_pod_admin=is_pod_admin, trail_link=trail_link,
    )
    summary = (record.get("user_description") or "")[:100]

    try:
        evo_notifications.append_event(
            shared_dir,
            requesting_user,
            kind="investigation_diagnosis",
            bot_id=bot_id,
            summary=summary or None,
            detail=detail,
            extra={
                "investigation_id": record.get("investigation_id"),
                "confidence": record.get("confidence"),
                "status": record.get("status"),
            },
        )
        return True
    except Exception as exc:
        logger.warning(
            "audit_poller: notification append failed for %s: %s",
            requesting_user, exc,
        )
        return False


# ── Admin lookup + trail URL builder (Item 3 follow-up) ────────────────
#
# The investigation_diagnosis renderer only includes the trail link when
# the requesting user is a pod admin AND a URL is provided. These helpers
# resolve both:
#   - _requesting_user_is_pod_admin parses the audit_poller record's
#     requesting_user key (one of "pod:<user>", "ext:<channel>:<id>",
#     or "anon:<bot>") against network.json -> pod.admins.
#   - _build_investigation_trail_url uses the same host-resolution
#     order as evolve-admin handover (PR #1199): pod.public_host →
#     tunnel.remote_host → localhost:5050.


def _requesting_user_is_pod_admin(
    requesting_user: str, network: dict,
) -> bool:
    """True when the (channel, external_id) or pod_user in requesting_user
    appears under network.pod.admins."""
    rk = (requesting_user or "").strip()
    if not rk or rk.startswith("anon:"):
        return False
    pod = network.get("pod") or {}
    if not isinstance(pod, dict):
        return False
    admins = pod.get("admins") or {}
    if not isinstance(admins, dict):
        return False

    if rk.startswith("pod:"):
        pod_user = rk.split(":", 1)[1]
        pod_users = admins.get("pod_users") or []
        return isinstance(pod_users, list) and str(pod_user) in [str(x) for x in pod_users]

    if rk.startswith("ext:"):
        # ext:<channel>:<id>
        rest = rk.split(":", 2)
        if len(rest) < 3:
            return False
        _, channel, ext_id = rest
        # Tolerant of both external_ids shapes (M1-B2).
        return _external_ids.has_id(admins, channel, ext_id)

    return False


def _pod_host_for_dashboard(network: dict) -> tuple[str, str]:
    """Return ``(scheme, host)`` for the investigation-trail link.

    Delegates to :func:`evolve_admin.config.resolve_pod_host` so the
    audit poller stays out of the handover module's import graph
    (handover's storage path needs Flask context). Same precedence chain
    as :func:`evolve_admin.handover.pod_host`: ``pod.public_host`` →
    ``tunnel.remote_host`` → ``resolve_admin_base_url`` →
    ``("http", "localhost:5050")``. Bare-host overrides inherit the
    scheme from ``adminBaseUrl``; full-URL overrides keep their own.
    """
    from evolve_admin.config import resolve_pod_host
    return resolve_pod_host(network)


def _build_investigation_trail_url(
    network: dict, investigation_id: str,
) -> str:
    """Deep-link URL the operator can tap to open the investigation trail
    in the admin dashboard. The path is /investigations/<id>; a small
    redirect route in web/server.py rewrites it to the index with a
    hash fragment the JS picks up to open the trail viewer.

    Scheme is preserved from ``adminBaseUrl`` — see
    ``internal/spec-pwa-phase0-https-2026-05-18.md`` §3.3.
    """
    scheme, host = _pod_host_for_dashboard(network)
    return f"{scheme}://{host}/investigations/{investigation_id}"


def _fallback_render_notification(
    record: dict, *, is_pod_admin: bool = False, trail_link: str | None = None,
) -> str:
    """Bare-bones notification body when the runner renderer isn't importable."""
    diagnosis = (record.get("diagnosis") or "").strip()
    fix = (record.get("suggested_fix") or "").strip()
    if diagnosis:
        body = f"I checked. {diagnosis}"
        if fix:
            body += f"\n\nSuggested fix: {fix}"
        return body
    return (
        "I checked but couldn't pinpoint a single cause. "
        "If you want me to flag this for the operator, reply `evo fail flag`."
    )


def _ingest_investigation_unresolved(record: dict, shared_dir: Path) -> bool:
    """Turn an investigation_unresolved record into a Proposal in the arbiter.

    Spec §5.5: ``evo fail flag`` after a no-diagnosis. The Proposal
    carries the full investigation context — the user's complaint, the
    triage candidates the runner considered, any evidence, the prior
    diagnosis. Operator handles via the existing Proposals queue.
    """
    try:
        from arbiter import store as arbiter_store
        from arbiter.state_machine import transition
        from schema.proposal import (
            Investigation,
            Proposal,
            Provenance,
            RiskTag,
            new_proposal_id,
        )
    except Exception as exc:
        logger.warning(
            "audit_poller: arbiter import failed for investigation_unresolved: %s",
            exc,
        )
        return False

    bot_id = record.get("bot_id") or ""
    investigation_id = record.get("investigation_id") or ""
    if not bot_id or not investigation_id:
        return False

    trigger_obs = f"investigation_unresolved:{investigation_id}"

    # Idempotency: don't re-raise if the operator already has this one open.
    try:
        for existing in arbiter_store.iter_proposals(
            shared_dir, subdirs=("pending", "snoozed"),
        ):
            if trigger_obs in (existing.trigger_observations or []):
                return True
    except Exception:
        pass

    description = (record.get("user_description") or "").strip() or "(no description)"
    problem = f"User flagged unresolved failure on `{bot_id}`: {description[:120]}"

    chosen = record.get("chosen_candidate") or {}
    triage = record.get("triage_candidates") or []
    previous_diagnosis = (record.get("previous_diagnosis") or "").strip()
    previous_confidence = (record.get("previous_confidence") or "").strip()
    evidence = record.get("evidence") or []
    signal_ids = record.get("related_signal_ids") or []

    lines = [
        f"## Unresolved failure — bot `{bot_id}` · investigation `{investigation_id}`",
        "",
        "**User reported:**",
        f"> {description}",
        "",
    ]
    if previous_diagnosis:
        lines += [
            "**Previous bot diagnosis "
            f"(confidence: {previous_confidence or 'unknown'}):**",
            previous_diagnosis,
            "",
        ]
    else:
        lines += [
            "**Bot couldn't pinpoint a single cause.** The user escalated.",
            "",
        ]
    if chosen:
        lines += [
            f"**Top candidate the bot investigated:** "
            f"`{chosen.get('element_type')}` / `{chosen.get('element_id')}` "
            f"(confidence: {chosen.get('confidence', '?')}).",
            "",
        ]
    if triage:
        lines.append("**All triage candidates considered:**")
        for c in triage:
            lines.append(
                f"- `{c.get('element_type', '?')}/"
                f"{c.get('element_id', '?')}` "
                f"({c.get('confidence', '?')})"
            )
        lines.append("")
    if evidence:
        lines.append("**Evidence the bot cited:**")
        for ev in evidence:
            lines.append(f"- `{ev}`")
        lines.append("")
    if signal_ids:
        lines += [
            "**Related signal IDs:**",
            ", ".join(f"`{s}`" for s in signal_ids),
            "",
        ]
    lines.append(
        "_Operator follow-up: investigate the most-likely candidate manually, "
        "fix the root cause, and reply to the user when resolved._"
    )
    context = "\n".join(lines)

    try:
        proposal = Proposal(
            id=new_proposal_id(),
            bot_id=bot_id,
            generator_id="app_audit_investigation_unresolved",
            dimension="reliability",
            trigger_observations=[trigger_obs],
            provenance=Provenance(
                technique="app_audit_investigation.v1",
                signals={
                    "investigation_id": investigation_id,
                    "record_id": record.get("record_id"),
                    "requesting_user": record.get("requesting_user"),
                    "previous_confidence": previous_confidence,
                },
                confidence=0.65,
            ),
            problem=problem,
            action=Investigation(context=context),
            risk_tag=RiskTag(
                blast_radius="bot",
                reversibility="manual",
                touches=["investigation"],
            ),
            claim=None,
            approval_audience="pod_admin",
            urgency="operational_urgent",
            admin_surface_summary=problem[:120],
        )
        transition(
            proposal, "pending",
            actor="app_audit_investigation_unresolved",
            reason="audit_poller ingest",
        )
        arbiter_store.write_proposal(proposal, shared_dir)
        return True
    except Exception as exc:
        logger.warning(
            "audit_poller: investigation_unresolved proposal write failed "
            "for %s/%s: %s",
            bot_id, investigation_id, exc,
        )
        return False


_URGENCY_BY_SEVERITY = {
    "critical": "operational_urgent",
    "major":    "operational_urgent",
    "minor":    "improvement",
    "info":     "improvement",
}


def _urgency_from_severity(severity: str | None) -> str:
    return _URGENCY_BY_SEVERITY.get((severity or "").lower(), "improvement")


def _confidence_from_severity(severity: str | None) -> float:
    return {
        "critical": 0.85,
        "major":    0.7,
        "minor":    0.55,
        "info":     0.4,
    }.get((severity or "").lower(), 0.5)


# ── Repair-session ingestion (spec-app-coherence-and-reconciliation §11.3) ─


def _ingest_repair_applied(
    record: dict, shared_dir: Path, *, bot_user: str,
) -> tuple[bool, int]:
    """Write a ``repair_applied`` changelog entry + emit any Proposals.

    The bot's repair runner finished a session and produced an outbox
    record with ``applied_transformations[]`` (what landed on disk) and
    ``proposals[]`` (LLM picks that the executor refused, or design-level
    fixes outside the allowlist). We:

      1. Append a ``KIND_REPAIR_APPLIED`` entry to the per-app changelog
         carrying both lists so the operator can audit the session.
      2. For each Proposal in the record, emit an arbiter Proposal under
         the ``app_repair`` generator id so the operator sees the
         non-mechanical follow-ups inline with the rest of the queue.

    Returns ``(ok, proposals_raised)``. ``ok=False`` only when the
    changelog append itself fails — Proposal emission is best-effort.
    """
    from .app_changelog import build_repair_applied_entry, append_to_trail

    bot_id = (record.get("bot_id") or "").strip()
    app_id = (record.get("app_id") or "").strip()
    request_id = (record.get("request_id") or "").strip()
    if not bot_user or not app_id or not request_id:
        logger.warning(
            "audit_poller: repair_applied missing bot/app/request_id: %s",
            record.get("record_id"),
        )
        return False, 0

    entry = build_repair_applied_entry(
        request_id=request_id,
        transformations=record.get("applied_transformations") or [],
        proposals=record.get("proposals") or [],
    )
    audits_dir = _audits_dir_for_bot(bot_user)
    if not append_to_trail(audits_dir, app_id, entry):
        logger.warning(
            "audit_poller: repair_applied trail write failed for %s/%s",
            bot_id, request_id,
        )
        return False, 0

    proposals_raised = 0
    for proposal in record.get("proposals") or []:
        if not isinstance(proposal, dict):
            continue
        if _emit_repair_proposal(record, proposal, shared_dir):
            proposals_raised += 1

    return True, proposals_raised


def _ingest_repair_failed(
    record: dict, shared_dir: Path, *, bot_user: str,
) -> bool:
    """Write a ``repair_failed`` changelog entry. No Proposals.

    The session failed end-to-end (LLM dispatch error, parse failure,
    rate-limit refusal, runner crash). The operator sees the failure
    in the changelog with the error string the runner emitted.
    """
    from .app_changelog import build_repair_failed_entry, append_to_trail

    bot_id = (record.get("bot_id") or "").strip()
    app_id = (record.get("app_id") or "").strip()
    request_id = (record.get("request_id") or "").strip()
    error = (record.get("error") or "unknown error").strip()
    if not bot_user or not app_id or not request_id:
        logger.warning(
            "audit_poller: repair_failed missing bot/app/request_id: %s",
            record.get("record_id"),
        )
        return False

    entry = build_repair_failed_entry(request_id=request_id, error=error)
    audits_dir = _audits_dir_for_bot(bot_user)
    if not append_to_trail(audits_dir, app_id, entry):
        logger.warning(
            "audit_poller: repair_failed trail write failed for %s/%s",
            bot_id, request_id,
        )
        return False
    return True


def _emit_repair_proposal(
    record: dict, proposal: dict, shared_dir: Path,
) -> bool:
    """Emit one Proposal from a repair session's leftover Proposal list.

    Wraps ``_emit_audit_proposal`` so the repair Proposal gets the
    same arbiter treatment as a Tier-3 Proposal — dedup by signature,
    motivating_signals link, urgency from finding severity.
    """
    finding_id = (proposal.get("finding_id") or "").strip()
    pkind = (proposal.get("kind") or "proposal").strip()
    bot_id = (record.get("bot_id") or "").strip()
    app_id = (record.get("app_id") or "").strip()
    request_id = (record.get("request_id") or "").strip()
    rationale = (proposal.get("rationale") or "").strip()

    # Synthesize a tier3-like record so the existing emitter can render
    # the Proposal. The ``signature`` keeps Proposals deduped per
    # (bot, app, finding) across re-runs.
    signature = f"repair:{bot_id}:{app_id}:{finding_id}:{pkind}"
    synthetic = {
        "record_id":       record.get("record_id"),
        "bot_id":          bot_id,
        "app_id":          app_id,
        "signature":       signature,
        "obs_id":          finding_id,
        "category":        "manifest_drift",
        "severity":        "minor",
        "description":     rationale or f"Repair session emitted Proposal ({pkind})",
        "evidence":        [f"repair_request:{request_id}"],
        "outcome":         "propose",
        "rationale":       rationale,
        "transformation_summary": (
            f"Repair session emitted a Proposal of kind {pkind!r} "
            f"because the LLM picked a fix outside the auto-apply allowlist"
        ),
    }
    return _emit_audit_proposal(
        shared_dir=shared_dir,
        record=synthetic,
        generator_id="app_repair",
        dimension="reliability",
        context_builder=_render_finding_context,
    )


# ── Infra-audit outbox paths + listing ──────────────────────────────────────


def _infra_outbox_dir(shared_dir: Path) -> Path:
    return Path(shared_dir) / "infra_audit_outbox"


def _infra_outbox_ingested(shared_dir: Path) -> Path:
    return _infra_outbox_dir(shared_dir) / "_ingested"


def _list_infra_outbox_files(shared_dir: Path) -> list[Path]:
    """Return processable infra-outbox files (skips _ingested archive)."""
    outbox = _infra_outbox_dir(shared_dir)
    if not outbox.exists():
        return []
    files: list[Path] = []
    try:
        for entry in outbox.iterdir():
            if entry.name.startswith(".") or entry.name == "_ingested":
                continue
            if entry.is_file() and entry.suffix == ".json":
                files.append(entry)
    except OSError:
        return []
    try:
        files.sort(key=lambda p: p.stat().st_mtime)
    except OSError:
        pass
    return files


def _archive_infra_file(path: Path, shared_dir: Path) -> bool:
    """Move processed infra-outbox file under _ingested/<YYYY-MM-DD>/.

    shared_dir is evolve-owned so direct moves work (no sudo).
    """
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    dest_dir = _infra_outbox_ingested(shared_dir) / today
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest_dir / path.name))
        return True
    except OSError as exc:
        logger.warning("audit_poller: infra archive failed for %s: %s", path, exc)
        return False


def _ingest_infra_finding(record: dict, shared_dir: Path) -> bool:
    """Turn an infra_finding record into a Proposal in the arbiter store.

    Infrastructure findings are pod-scoped (bot_id is the empty string in
    the Proposal — no bot owns "/etc/sudoers.d/evolve"). The poller maps
    element + category through to the same dimension as app-audit findings
    so the arbiter's existing routing applies.

    Annotates the record with lineage state — past dismissals for the same
    fingerprint — so the context builder can surface "this proposal has
    been dismissed N times" headers rather than re-stating an unworkable
    suggested_fix. See `_lineage_for_signature` for the policy.
    """
    normalized = _normalize_infra_record(record)
    try:
        normalized["_lineage"] = _lineage_for_signature(
            shared_dir=shared_dir,
            signature=normalized.get("signature") or "",
            window_days=30,
        )
    except Exception as exc:  # noqa: BLE001
        # Lineage is advisory; a failure here must NOT block the proposal.
        logger.warning("audit_poller: lineage probe failed: %s", exc)
        normalized["_lineage"] = {"dismissal_count": 0, "rejection_count": 0}
    return _emit_audit_proposal(
        shared_dir=shared_dir,
        record=normalized,
        generator_id="infra_audit",
        dimension="reliability",
        context_builder=_render_infra_finding_context,
    )


def _lineage_for_signature(
    *, shared_dir: Path, signature: str, window_days: int,
) -> dict:
    """Count past terminal proposals for this fingerprint within a window.

    Returns ``{"dismissal_count": int, "rejection_count": int, "last_dismissed_iso": str}``.

    The "dismissed" status fires when the operator clicked Dismiss in the UI;
    "rejected" fires when a peer review (or arbiter veto) blocked the
    proposal. Both signal "this fix was not adopted" — but for different
    reasons, so we count them separately.

    A high dismissal_count means the same finding keeps coming back without
    underlying remediation. The most common cause is that the suggested_fix
    is wrong — either it's a bootstrap into a non-existent domain (mcp-bridge
    LaunchAgent on a headless pod), a config patch that the operator
    deliberately drifted from for a documented reason, or a recommendation
    that the operator knows but can't action right now. Re-proposing the same
    fix is operator-spam; surfacing the lineage prompts an investigation
    instead.

    Window: 30 days by default. Older history is ignored — operator priorities
    shift, the underlying config may have changed, and ancient dismissals
    don't speak to current state.
    """
    out = {"dismissal_count": 0, "rejection_count": 0, "last_dismissed_iso": ""}
    if not signature:
        return out
    try:
        from arbiter import store as arbiter_store
    except Exception:
        return out

    import datetime as _dt
    cutoff = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=window_days)
    )

    # Use the infra trigger_obs shape consistently with _emit_audit_proposal:
    # `infra_audit:<generator_id>:<signature>`. Stored proposals' trigger
    # observations include this string; we match on substring containment
    # to be forgiving of future prefix changes.
    needle = f"infra_audit:{signature}"

    last_dismissed: Optional[str] = None
    try:
        for prop in arbiter_store.iter_proposals(
            shared_dir, subdirs=("archived",),
        ):
            triggers = getattr(prop, "trigger_observations", None) or []
            if not any(needle in (t or "") for t in triggers):
                continue
            # Filter to terminal-not-adopted statuses
            status = (getattr(prop, "status", "") or "").lower()
            if status not in ("dismissed", "rejected"):
                continue
            # Timestamp resolution: prefer the most recent matching transition
            # in history[] (StatusTransition.at), fall back to created_at.
            # Proposals have no `updated_at` field — the append-only history
            # is the canonical record of when each status change happened.
            ts_raw = ""
            history = getattr(prop, "history", None) or []
            for h in reversed(history):
                to_status = (getattr(h, "to_status", "") or "").lower()
                if to_status == status:
                    ts_raw = getattr(h, "at", "") or ""
                    break
            if not ts_raw:
                ts_raw = getattr(prop, "created_at", "") or ""
            try:
                ts = _dt.datetime.fromisoformat((ts_raw or "").replace("Z", "+00:00"))
            except (TypeError, ValueError):
                ts = None
            if ts and ts < cutoff:
                continue
            if status == "dismissed":
                out["dismissal_count"] += 1
                if not last_dismissed or (ts_raw and ts_raw > last_dismissed):
                    last_dismissed = ts_raw
            elif status == "rejected":
                out["rejection_count"] += 1
    except Exception:
        # Iteration failure → return what we have; advisory only.
        return out
    if last_dismissed:
        out["last_dismissed_iso"] = last_dismissed
    return out


def _normalize_infra_record(record: dict) -> dict:
    """Adapt an infra_finding record's shape to the one _emit_audit_proposal
    expects. The Proposal helper was written for app findings (bot_id +
    app_id); for infra we synthesize app_id from `element` and leave
    bot_id blank — the arbiter accepts an empty bot scope for pod-wide
    proposals via `RiskTag(blast_radius="pod")`.
    """
    out = dict(record)
    out.setdefault("bot_id", "pod")
    out.setdefault("app_id", record.get("element") or "infra")
    out["category"] = record.get("category") or "infra_finding"
    return out


def _render_infra_finding_context(record: dict) -> str:
    """Markdown context body for an infra Proposal.

    When the record carries `_lineage` (dismissal/rejection counts populated
    by `_lineage_for_signature`), the body leads with a "repeatedly dismissed"
    header inviting investigation rather than re-stating the suggested_fix as
    the next step. The fix stays in the body for operators who do want to
    re-apply it, but the framing pivots to "why does this keep coming back?"
    when the dismissal count crosses a threshold.

    Threshold: ≥2 dismissals in the 30-day window. One dismissal might be
    "not now"; two or more is a pattern.
    """
    element = record.get("element") or record.get("app_id") or "infra"
    severity = record.get("severity") or "info"
    description = (record.get("description") or "").strip()
    evidence = record.get("evidence") or {}
    suggested_fix = (record.get("suggested_fix") or "").strip()
    rationale = (record.get("rationale") or "").strip()
    lineage = record.get("_lineage") or {}
    dismissal_count = int(lineage.get("dismissal_count") or 0)
    rejection_count = int(lineage.get("rejection_count") or 0)
    last_dismissed = lineage.get("last_dismissed_iso") or ""

    lines: list[str] = []

    if dismissal_count >= 2:
        # Lead with the lineage signal — the operator's "this keeps coming
        # back" feeling is the load-bearing context, not the description.
        lines.extend([
            f"## ⚠ Repeatedly dismissed — `{element}` ({severity})",
            "",
            (
                f"This finding has been **dismissed {dismissal_count} times** "
                f"in the past 30 days"
                + (f" (most recently {last_dismissed[:10]})" if last_dismissed else "")
                + ". Re-proposing the same fix is likely operator-spam — "
                "investigate why the dismissals happened before applying:"
            ),
            "",
            "- Was the suggested fix wrong for this pod (e.g. a bootstrap "
              "into a domain that doesn't exist, like the mcp-bridge "
              "LaunchAgent → headless-pod case)?",
            "- Was the deviation deliberate (a documented exception that "
              "the generator doesn't know about)?",
            "- Has the underlying check itself drifted out of calibration?",
            "",
            "If the answer is one of these, the right action is to **fix "
            "the generator / record an intent annotation / amend the "
            "infra_audit category**, not to re-apply the same suggested "
            "fix. See "
            "[feedback_generators_consider_intent.md](memory:feedback_generators_consider_intent) "
            "for the broader pattern.",
            "",
            "### Current finding",
            "",
        ])
    else:
        lines.append(f"## Infrastructure audit finding — `{element}` ({severity})")
        lines.append("")
    lines.append(description)
    lines.append("")
    if evidence:
        lines.append("**Evidence:**")
        for k, v in evidence.items():
            if isinstance(v, (list, dict)):
                lines.append(f"- `{k}`: `{json.dumps(v)[:200]}`")
            else:
                lines.append(f"- `{k}`: `{v}`")
        lines.append("")
    if suggested_fix:
        lines.append(f"**Suggested fix:**\n\n    {suggested_fix}\n")
    if rationale:
        lines.append(f"**Triage rationale:** {rationale}")
        lines.append("")
    if dismissal_count == 1:
        # One dismissal in window — note it without pivoting the framing.
        lines.append(
            f"_Note: this fingerprint was dismissed once in the past 30 days_"
            + (f" ({last_dismissed[:10]})." if last_dismissed else ".")
        )
        lines.append("")
    if rejection_count > 0:
        lines.append(
            f"_Note: peer review has rejected this fingerprint "
            f"{rejection_count} time(s) in the past 30 days._"
        )
        lines.append("")
    lines.append(
        f"_Audit run: `{record.get('audit_run_id', 'unknown')}` · "
        f"record: `{record.get('record_id', 'unknown')}`. "
        f"This audit is read-only — no automatic changes were made._"
    )
    return "\n".join(lines)


def _ingest_infra_run_summary(record: dict, shared_dir: Path) -> int:
    """Sweep cleared infra findings from the Signal store.

    Infra audits don't generally emit Signals (their findings go to
    Proposals directly), but if an `infra_run_failed` Signal was active
    and the next run completed cleanly, sweep_resolve will clear it.
    Returns the count of resolved signals.
    """
    try:
        from signals import store as signals_store
    except Exception:
        return 0
    try:
        # Use the run-summary's kept_signatures as a no-op kept set for
        # infra_audit_runner-emitted Signals. Anything firing under
        # producer=infra_audit that isn't in this list will be archived.
        kept = set(record.get("kept_signatures") or [])
        resolved = signals_store.sweep_resolve(
            shared_dir=shared_dir,
            producer="infra_audit",
            kept_signatures=kept,
            reason="audit_poller: infra audit completed; sweeping cleared signals",
        )
        if isinstance(resolved, list):
            return len(resolved)
        return 0
    except Exception as exc:
        logger.warning("audit_poller: infra sweep_resolve failed: %s", exc)
        return 0


def _ingest_infra_run_failed(record: dict, shared_dir: Path) -> bool:
    """Raise a Signal when the infra audit itself broke.

    Distinct from a finding — this is "the watcher is broken." Operators
    must see it because no Proposals will land until the audit is
    fixed.
    """
    try:
        from signals import store as signals_store
    except Exception as exc:
        logger.warning("audit_poller: signals import failed: %s", exc)
        return False
    error = record.get("error") or "infra audit run failed"
    try:
        signals_store.observe(
            shared_dir=shared_dir,
            signature=f"infra_audit_run_failed:{record.get('audit_run_id', 'unknown')}",
            producer="infra_audit",
            type="infra_audit_run_failed",
            flavor="maintenance",
            severity="alert",
            scope="pod",
            bot_id="",
            title="Infrastructure audit run failed",
            body=str(error)[:500],
            details={
                "audit_run_id": record.get("audit_run_id"),
                "record_id": record.get("record_id"),
                "requested_by": record.get("requested_by"),
            },
        )
        return True
    except Exception as exc:
        logger.warning("audit_poller: infra run_failed observe failed: %s", exc)
        return False


# ── Pod-wide infra outbox drain ─────────────────────────────────────────────


def poll_infra(shared_dir: Path) -> InfraPollResult:
    """Drain {shared_dir}/infra_audit_outbox into Signal + Proposal stores.

    Mirrors poll_bot's record-kind dispatch but for the pod-wide infra
    audit outbox. Run kinds:

      - infra_finding       → Proposal (idempotent via signature)
      - infra_run_summary   → sweep_resolve cleared signals; counter
      - infra_run_failed    → Signal in firing/

    Returns an InfraPollResult with counters + errors.
    """
    result = InfraPollResult()
    files = _list_infra_outbox_files(shared_dir)
    for path in files:
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            result.errors.append(f"unreadable: {path.name}")
            continue

        kind = record.get("kind")
        ok = False
        archive = True
        drop_reason = ""
        if kind == "infra_finding":
            # Low-volume, forensically valuable — always KEEP/archive.
            ok = _ingest_infra_finding(record, shared_dir)
            if ok:
                result.findings_ingested += 1
        elif kind == "infra_run_summary":
            swept = _ingest_infra_run_summary(record, shared_dir)
            result.signals_swept += swept
            result.summaries_processed += 1
            ok = True
            # Drop no-op heartbeat summaries (nothing audited, no findings).
            if _run_summary_is_noop(record):
                archive = False
                drop_reason = "heartbeat"
        elif kind == "infra_run_failed":
            # Low-volume, forensically valuable — always KEEP/archive.
            ok = _ingest_infra_run_failed(record, shared_dir)
            if ok:
                result.signals_emitted += 1
        else:
            result.errors.append(f"unhandled infra kind: {kind}")
            continue

        if ok:
            if archive:
                if _archive_infra_file(path, shared_dir):
                    result.files_processed += 1
                    result.records_archived += 1
                else:
                    result.errors.append(f"infra archive failed: {path.name}")
            else:
                if _delete_file(path):
                    result.files_processed += 1
                    result.records_dropped += 1
                    if drop_reason == "heartbeat":
                        result.dropped_heartbeat += 1
                else:
                    result.errors.append(f"infra delete failed: {path.name}")

    return result


# ── Per-bot poll ─────────────────────────────────────────────────────────────


def poll_bot(bot_id: str, bot_user: str, shared_dir: Path) -> PollResult:
    """Drain one bot's audit outbox into the shared Signal + Proposal stores.

    Record-kind dispatch:
      - tier2_finding       → Signal via signals.store.observe
      - tier2_run_summary   → sweep_resolve Signals not in kept_signatures
      - tier3_finding       → Proposal (or no-op for ``outcome=auto_fix``)
      - conflict_notice     → audit_conflict_notice Proposal
      - tier3_run_summary   → counted but not currently surfaced
      - run_failed          → trail-only; the per-app last_audit stamp
                              captures it on the bot side
      - anything else       → left in outbox; logged as unhandled

    Returns a PollResult with per-bot counters and any errors encountered.
    """
    result = PollResult(bot_id=bot_id, bot_user=bot_user)

    # Per-tick memo for tier3 supersede: (bot_id, app_id, audit_run_id)
    # triples we've already swept. The first finding of run R2 for app X
    # archives R1's pending proposals; subsequent R2 findings for X reuse
    # the memo and skip the (now-empty) scan.
    superseded_runs: set[tuple[str, str, str]] = set()

    files = _list_outbox_files(bot_user)
    for path in files:
        record = _read_outbox_file(path)
        if record is None:
            result.errors.append(f"unreadable: {path.name}")
            continue

        kind = record.get("kind")
        ok = False
        # archive=True → move the drained record to _ingested/ (forensic
        # value). archive=False → DELETE it (already durably ingested; the
        # copy is pure redundancy). drop_reason feeds the per-reason counters.
        archive = True
        drop_reason = ""
        if kind == "tier2_finding":
            ok, archive = _ingest_finding(record, shared_dir)
            if ok:
                result.findings_ingested += 1
                if not archive:
                    drop_reason = "dedup_hit"
        elif kind == "tier2_run_summary":
            swept = _ingest_run_summary(record, shared_dir)
            result.signals_swept += swept
            result.summaries_processed += 1
            ok = True
            # Drop the no-op heartbeat summaries (apps_audited:0, no
            # findings/outcomes). The sweep_resolve above still ran, so any
            # cleared-condition resolves happened regardless of archive/drop.
            if _run_summary_is_noop(record):
                archive = False
                drop_reason = "heartbeat"
        elif kind == "tier3_finding":
            # auto_fix outcomes are recorded in the trail and never raise a
            # Proposal — they're either fixed (calibration off + applied) or
            # demoted to propose first (calibration on). When we see one
            # here, the outcome should be propose or auto_fix; auto_fix lands
            # in the trail only, so we no-op + archive.
            outcome = record.get("outcome", "propose")
            if outcome == "propose":
                ok, archive = _ingest_tier3_finding(
                    record, shared_dir, superseded_runs=superseded_runs,
                )
                if ok:
                    result.tier3_findings_ingested += 1
                    result.tier3_proposals_raised += 1
                    if not archive:
                        drop_reason = "dedup_hit"
            elif outcome == "auto_fix":
                # Trail-only; no shared-side artifact needed.
                ok = True
            else:
                # Unknown outcome — treat as propose for safety.
                ok, archive = _ingest_tier3_finding(
                    record, shared_dir, superseded_runs=superseded_runs,
                )
                if ok:
                    result.tier3_findings_ingested += 1
                    result.tier3_proposals_raised += 1
                    if not archive:
                        drop_reason = "dedup_hit"
        elif kind == "conflict_notice":
            ok = _ingest_tier3_conflict_notice(record, shared_dir)
            if ok:
                result.tier3_conflict_notices += 1
        elif kind == "tier3_run_summary":
            # No pod-wide artifact — counter-only. Drop no-op heartbeats
            # (apps_audited:0, all outcomes zero); keep summaries with work.
            result.summaries_processed += 1
            ok = True
            if _run_summary_is_noop(record):
                archive = False
                drop_reason = "heartbeat"
        elif kind == "skill_finding":
            # Workstream B-skills. Same outcome semantics as tier3_finding —
            # propose → Proposal, auto_fix → trail-only.
            outcome = record.get("outcome", "propose")
            if outcome == "propose":
                ok = _ingest_skill_finding(record, shared_dir)
                if ok:
                    result.skill_findings_ingested += 1
                    result.skill_proposals_raised += 1
            elif outcome == "auto_fix":
                # Trail-only; no shared-side artifact.
                ok = True
            else:
                ok = _ingest_skill_finding(record, shared_dir)
                if ok:
                    result.skill_findings_ingested += 1
                    result.skill_proposals_raised += 1
        elif kind == "provider_finding":
            outcome = record.get("outcome", "propose")
            if outcome == "propose":
                ok = _ingest_provider_finding(record, shared_dir)
                if ok:
                    result.provider_findings_ingested += 1
                    result.provider_proposals_raised += 1
            elif outcome == "auto_fix":
                ok = True
            else:
                ok = _ingest_provider_finding(record, shared_dir)
                if ok:
                    result.provider_findings_ingested += 1
                    result.provider_proposals_raised += 1
        elif kind in ("skill_run_summary", "provider_run_summary"):
            # Counter-only; substrate audits don't sweep-resolve in v1
            # (Signals come from the apps that use them, not from the
            # audit itself).
            result.summaries_processed += 1
            ok = True
            if _run_summary_is_noop(record):
                archive = False
                drop_reason = "heartbeat"
        elif kind == "investigation_diagnosis":
            # Workstream C — evo fail. ALWAYS emits a notification, never
            # a Proposal (direct-reply design). Both diagnosed and
            # no-diagnosis outcomes flow through the same record kind;
            # the renderer chooses the template based on confidence +
            # diagnosis presence.
            ok = _ingest_investigation_diagnosis(record, shared_dir)
            if ok:
                result.investigation_notifications += 1
        elif kind == "investigation_unresolved":
            # Workstream C — `evo fail flag` escalation. Creates a Proposal
            # for the operator with the prior investigation context.
            ok = _ingest_investigation_unresolved(record, shared_dir)
            if ok:
                result.investigation_proposals_raised += 1
        elif kind == "run_failed":
            # Trail-only. Bot stamps manifest.last_audit.status=failed itself.
            ok = True
        elif kind == "repair_applied":
            # spec-app-coherence-and-reconciliation-2026-06-05.md §11.3.
            # The bot ran a repair session and at least one transformation
            # may have applied. Write a changelog entry + raise any
            # Proposals the LLM emitted for non-mechanical sub-fixes.
            ok, proposals_raised = _ingest_repair_applied(
                record, shared_dir, bot_user=bot_user,
            )
            if ok:
                result.repair_applied += 1
                result.repair_proposals_raised += proposals_raised
        elif kind == "repair_failed":
            # Repair session failed end-to-end (dispatch error, parse
            # failure, rate limit). One changelog entry; no Proposals.
            ok = _ingest_repair_failed(record, shared_dir, bot_user=bot_user)
            if ok:
                result.repair_failed += 1
        else:
            # Unknown kinds — leave in outbox; don't archive blindly.
            result.errors.append(f"unhandled kind: {kind}")
            continue

        if ok:
            # Terminal step: archive records with forensic value, DELETE pure
            # re-emissions/heartbeats. Either way the root outbox is drained
            # (files_processed counts the take-off-the-queue). The signal/
            # proposal store already holds the durable copy of dropped records.
            if archive:
                if _archive_file(path, bot_user):
                    result.files_processed += 1
                    result.records_archived += 1
                else:
                    result.errors.append(f"archive failed: {path.name}")
            else:
                if _delete_file(path):
                    result.files_processed += 1
                    result.records_dropped += 1
                    if drop_reason == "dedup_hit":
                        result.dropped_dedup_hit += 1
                    elif drop_reason == "heartbeat":
                        result.dropped_heartbeat += 1
                else:
                    result.errors.append(f"delete failed: {path.name}")

    return result


# ── Drain-liveness heartbeat (recorder) ──────────────────────────────────────


def _count_pending_backlog(shared_dir: Path, bot_users: dict[str, str]) -> int:
    """Outbox records still un-ingested across every bot + the infra outbox.

    Counted via the SAME path resolution the drain uses, so after a healthy
    tick (everything archived to ``_ingested/``) this is ~0. A non-zero count
    that persists while ``files_processed`` stays 0 is the silent-stall
    fingerprint monitor_coverage keys on. Per-source failures are swallowed —
    a backlog probe must never break the heartbeat.
    """
    total = 0
    for bot_user in bot_users.values():
        try:
            total += len(_list_outbox_files(bot_user))
        except Exception as exc:  # noqa: BLE001
            # Best-effort probe — a single bad source must not break the
            # heartbeat, but log so the swallow is never silent.
            logger.debug(
                "audit-drain backlog probe failed for %s: %s", bot_user, exc
            )
            continue
    try:
        total += len(_list_infra_outbox_files(shared_dir))
    except Exception as exc:  # noqa: BLE001
        logger.debug("audit-drain infra backlog probe failed: %s", exc)
    return total


def _record_drain_heartbeat(
    shared_dir: Path,
    processed: int,
    backlog: int,
    *,
    now: float | None = None,
) -> None:
    """Append one ``(ts, processed, backlog)`` sample to the drain heartbeat.

    Best-effort and bounded: a heartbeat write must never break a drain tick,
    and the file is a rolling window of the last ``_HEARTBEAT_HISTORY_MAX``
    samples. monitor_coverage reads it and owns the silence thresholds.
    """
    ts = int(now if now is not None else time.time())
    path = Path(shared_dir) / AUDIT_DRAIN_HEARTBEAT_REL
    try:
        recent: list[dict] = []
        if path.exists():
            try:
                data = json.loads(path.read_text())
                if isinstance(data, dict) and isinstance(data.get("recent"), list):
                    recent = [
                        e for e in data["recent"]
                        if isinstance(e, dict) and "ts" in e
                    ]
            except (OSError, json.JSONDecodeError):
                recent = []
        recent.append(
            {"ts": ts, "processed": int(processed), "backlog": int(backlog)}
        )
        recent = recent[-_HEARTBEAT_HISTORY_MAX:]
        payload = json.dumps({"schema": 1, "recent": recent})
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(payload)
        tmp.replace(path)  # atomic
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit_poller: drain heartbeat write failed: %s", exc)


# ── Tick (called from audit-scheduler) ───────────────────────────────────────


def tick(
    shared_dir: Path,
    network: dict | None = None,
    bot_users: dict[str, str] | None = None,
) -> TickResult:
    """One poller tick — iterate every bot and drain its audit outbox.

    bot_users is the {bot_id → macOS user} mapping the caller already
    resolved (typically from network.json + get_bot_user). When omitted,
    the function loads network and resolves itself.

    Errors per-bot are captured into PollResult.errors and do not abort the
    tick — one broken bot must not stop other bots' findings from landing.
    """
    if bot_users is None:
        bot_users = _resolve_bot_users(network)

    aggregate = TickResult()
    for bot_id, bot_user in bot_users.items():
        try:
            aggregate.bots.append(poll_bot(bot_id, bot_user, shared_dir))
        except Exception as exc:
            logger.exception(
                "audit_poller: poll_bot crashed for %s: %s", bot_id, exc,
            )
            pr = PollResult(bot_id=bot_id, bot_user=bot_user)
            pr.errors.append(f"poll crashed: {exc}")
            aggregate.bots.append(pr)

    # Pod-wide infra audit outbox — admin-side runner writes records here.
    # One drain per tick, in addition to the per-bot drains above. Errors
    # captured but don't abort the tick.
    try:
        aggregate.infra = poll_infra(shared_dir)
    except Exception as exc:
        logger.exception("audit_poller: poll_infra crashed: %s", exc)
        aggregate.infra.errors.append(f"poll_infra crashed: {exc}")

    # Drain-liveness heartbeat — record (processed, backlog) so monitor_coverage
    # can fire `audit_drain_silent` when the drain ingests nothing while the
    # outbox roots stay non-empty (the silent-stall class #3310 hit). The
    # backlog is read AFTER the drain, so a healthy tick reports ~0. Best-effort.
    try:
        backlog = _count_pending_backlog(shared_dir, bot_users)
        _record_drain_heartbeat(shared_dir, aggregate.total_files, backlog)
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit_poller: drain heartbeat skipped: %s", exc)

    return aggregate


def _resolve_bot_users(network: dict | None) -> dict[str, str]:
    """Return {bot_id → macOS user} from network.json."""
    if network is None:
        try:
            from ..config import load_network
            network = load_network()
        except Exception:
            return {}
    bots = (network or {}).get("bots") or {}
    out: dict[str, str] = {}
    for bot_id, cfg in bots.items():
        if not isinstance(cfg, dict):
            continue
        out[bot_id] = cfg.get("user") or bot_id
    return out
