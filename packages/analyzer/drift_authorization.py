"""drift_authorization — the L2 "is this change authorized?" gate.

Spec: internal/spec-drift-alert-taxonomy-2026-06-26.md (L2, co-owned with
META:edr). Composes with R-3's event-vs-posture classification
(internal/design-security-alert-fatigue-2026-08-31.md): an UNEXPLAINED drift
is an ``event``; an explained one is not a finding at all.

The problem this closes
=======================

Every drift producer on the pod fires on ANY live-vs-baseline difference. So
Evolve's own deploys page the operator (sudoers refreshed, workspace scripts
installed, the gateway re-clamping an ACL mask), while the one detector built
to catch an unauthorized change had never fired in its life. A live audit of
the drift signals on one bot found 100% benign. That is the boy-who-cried-wolf
shape: the security surface is so noisy with housekeeping that a real intrusion
would be ignored.

The question is never "did it change" — config changes constantly — but "did it
change through an authorized path?". This module is that one question, asked
the same way by every drift producer.

Contract
========

:func:`explain` takes a :class:`DriftChange` and returns an
:class:`Explanation` when a known authorized event accounts for it, or
``None`` when nothing does. Callers then:

  explained   → at most an informational row carrying the explanation
                (or nothing at all — the producer's choice, per L1)
  unexplained → the genuine security finding, classified ``event``, which
                pages rather than waiting for a digest. The settle gate never
                withholds alert level, and the sudoers finding is in no flap
                family, so it pages on the first run. The identity-hash
                family keeps its R-2 flap dwell IN FRONT of this gate (spec
                build record, decision 3): the dwell asks "is this real, or a
                bot's own edit racing the next backup commit?", the gate then
                asks "is a real one authorized?" — so a sustained unexplained
                mismatch there dwells, then pages.

Properties this module holds itself to
======================================

* **Deterministic.** Pure file reads and comparisons. There is no LLM
  judgement anywhere in the gate — a model deciding "that looks like a
  deploy" is exactly the seam an attacker would talk their way through.
* **Read-only.** Nothing here mutates a security baseline, applies a repair,
  or touches a producer's remediation path. The single write is the
  explanation memo (below), which records what was already decided.
* **Narrow by kind.** Each drift kind consults only the sources that could
  physically explain it (:data:`_KIND_SOURCES`). A change to
  ``/etc/sudoers.d/evolve`` is never explained by the OpenClaw gateway
  re-hardening a bot's config directory, because the gateway cannot write
  that file.
* **Fails toward paging.** An unreadable source, an unavailable render, a
  missing record: all of these produce *no explanation*, and no explanation
  means the drift pages. A suppression gate that cannot evaluate itself must
  never suppress. A change that is unexplained but actually benign still
  pages — that is the point, not a bug to tune away.

The allow-set, in spec order
============================

``deploy``          A recent deploy of THE BOT the change belongs to: the
                    per-bot ``bot_versions[<bot>].deployed_at`` stamp in the
                    install record (``{shared_dir}/install.json``). The
                    pod-wide stamps — ``install.json::installed_at`` and the
                    release pointer's ``promoted_at`` — are deliberately not
                    credited: ``write_install_json`` rewrites ``installed_at``
                    on EVERY ``evolve-admin deploy <bot>``, so crediting it
                    would let a deploy of bot X account for bot Y's identity
                    files changing (and the memo below would then keep that
                    verdict for a year). Every kind this source can explain
                    is keyed to one bot, so a change with no bot has no
                    deploy explanation. For the sudoers file specifically
                    there is something better than a time window — see below.
``self_update``     Evolve changing its own configuration through the
                    proposal pipeline: the apply-results the applier writes
                    under ``{shared_dir}/proposals/apply-results/``.

Two of the spec's four members are NOT implemented, each because it cannot
answer any question this gate asks.

**The operator's own approvals.** The spec names ``config_intent`` records and
admin-surface writes as a source. Both are keyed to ``openclaw.json`` config
paths, and every drift this gate serves is about a FILE:

* ``audit-log.jsonl``'s ``oc_keys`` is contractually "top-level keys in
  openclaw.json this action mutated" (``routes_shared``, ``provisioning``);
* ``config_intent`` does not merely conventionally hold config paths —
  ``_validate_field_path`` RAISES ``ValueError`` on anything outside
  ``tools.`` / ``commands.`` / ``plugins.`` / ``agents.``.

So no writer emits a filename and none can be made to without widening
another module's contract. The source was registered for ``identity_file``
until 2026-09-02 and could never fire there; it was the residue left after
the same finding was fixed for the retired ``shell_config`` kind (review B1,
internal/dispatch/reviews/pr-3953.md). A source that cannot answer is worse
than an absent one: it reads like a way out for the operator and is not, and
it invites tests that assert a branch production cannot reach. It belongs
here the day a kind keyed on CONFIG PATHS is wired — the taxonomy's own
``perm_config_drift`` worked example — and not before.

**OpenClaw re-hardening its own directory.** ``pod_perms_drift_monitor``
already answers that question, and answers it better: it re-runs the access
grant and then RE-VERIFIES, so a bot whose access came back is dropped from
the drift set on evidence rather than on a category match. Reimplementing it
here as "an access-control finding on a path inside a settings folder" would
also have swallowed the case that monitor deliberately keeps — a bot still
locked out AFTER the heal, which is a real fault and not the gateway's
routine re-locking. Generalise the existing check; do not duplicate it.

Why the sudoers check is content-bound rather than time-bound
=============================================================

``/etc/sudoers.d/evolve`` is rendered from code by
``setup_wizard._render_evolve_sudoers()`` and installed verbatim by a root
run of ``refresh-sudoers``. So the authorized state of that file is not "it
changed near a deploy" but "its bytes are exactly what this Evolve version
renders". :func:`_explain_sudoers` compares hashes, in that order of trust:

  1. the live hash equals the hash of the current render — the file is the
     grants this version of Evolve installs, whenever it was written;
  2. failing that, the live hash equals the install marker
     (``{shared_dir}/state/sudoers-installed.sha256``) that a successful root
     install writes.

Tier 2 is the weaker of the two and is deliberately second: the marker lives
under ``{shared_dir}`` and is therefore writable by the evolve service user,
whereas the render is repo code. Tier 2 exists because the render can be
genuinely unavailable (it returns ``None`` when the openclaw command cannot be
located), and a hand-added grant matches neither tier.

The explanation memo
====================

Two of the sources are time-windowed, so an explanation would expire: a
proposal applied to ``AGENTS.md`` last week stops explaining the file once
its window closes, and the finding would page on day eight for a change
nobody made.
:func:`explain` therefore remembers each explanation it issues, keyed by the
exact content hash of the thing it explained, in
``{shared_dir}/security/drift-explained.json``. A later run that finds no live
source but recognises the *same* hash keeps the explanation.

This cannot be used to wait an attacker in: a change that was never explained
has no memo entry, and any new content is a new hash and therefore a fresh
question. Changes with no content hash (the permission checks) are never
memoised — there is nothing to key on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ── Drift kinds ───────────────────────────────────────────────────────────────

KIND_SUDOERS_BASELINE = "sudoers_baseline"
#: Deliberately ABSENT from :data:`_KIND_SOURCES` — see the note there. The
#: shell-startup check stays a posture finding in ``audit_shell_config``;
#: :func:`explain` answers ``None`` for it through the unregistered-kind path.
KIND_SHELL_CONFIG = "shell_config"
KIND_IDENTITY_FILE = "identity_file"
KIND_SCRIPT_INVENTORY = "script_inventory"
KIND_POD_PERMS = "pod_perms"
#: Bot-vs-admin version skew. Always explained — it is literally Evolve
#: shipping, and the taxonomy's L1 split re-homes it to housekeeping.
KIND_DEPLOY_VERSION = "deploy_version"
#: ``/etc/pam.d`` drift (``incursion.pam``). The one incursion kind with a
#: source that can genuinely answer: the host's own package-install record.
KIND_PAM_CONFIG = "pam_config"
#: An added ``~/.ssh/authorized_keys`` entry (``incursion.authorized_keys``).
#: Deliberately ABSENT from :data:`_KIND_SOURCES` — see the note there.
KIND_AUTHORIZED_KEYS = "authorized_keys"
#: A new or repointed scheduled job (``incursion.job_inventory``).
#: Deliberately ABSENT from :data:`_KIND_SOURCES` — see the note there.
KIND_JOB_INVENTORY = "job_inventory"
#: An interactive login from a (user, source) pair never seen before
#: (``incursion.logins``). Deliberately ABSENT from :data:`_KIND_SOURCES`.
KIND_LOGIN_SOURCE = "login_source"

# ── Authorized-event sources ──────────────────────────────────────────────────

SOURCE_DEPLOY = "deploy"
SOURCE_SELF_UPDATE = "self_update"
#: The host's OWN record that it installed OS/system packages — macOS's
#: ``/Library/Receipts/InstallHistory.plist``, Linux's ``/var/log/dpkg.log``.
#: The one source that reads outside ``{shared_dir}``, because the authorized
#: event it describes is one the OS performed and only the OS recorded. Still
#: deterministic and read-only: a timestamped line in a log the audit user can
#: already read, matched narrowly against the packages that own the changed
#: file (see :func:`_os_update_events`).
SOURCE_OS_UPDATE = "os_update"
#: Only for :data:`KIND_DEPLOY_VERSION`, which needs no lookup at all.
SOURCE_BY_DEFINITION = "by_definition"
#: Not a source — the memo of an explanation an earlier run issued.
SOURCE_REMEMBERED = "remembered"


# Which sources may explain which kind. A kind consults these and nothing
# else, so a source is only ever asked about the kinds of change it could
# physically have caused. (Within a source the match is still a window plus
# a declared key, not a content proof: an apply that touched the SAME
# identity file within its window explains that file's change, whatever the
# bytes — and the memo then keeps that verdict for the exact content hash.)
#
# Note what is ABSENT, in each case because that source could not physically
# have caused this kind of change:
#
#   * ``shell_config`` is not in the table AT ALL — deliberately, and it is
#     posture-only. No Evolve code path writes a bot's ``.zshrc`` (deploy only
#     grants evolve READ on it, for this very hash check), and no
#     admin-surface record can name the file either: ``oc_keys`` is
#     contractually the set of ``openclaw.json`` top-level keys an action
#     mutated, and a filename is not one. An ``operator_intent`` allow-set
#     here could therefore never fire in production — it would only have
#     made the finding LOUDER (event, page-now) while looking gated. So
#     ``audit_shell_config`` never calls :func:`explain`; the finding stays
#     ``posture`` and is cleared only by the operator's explicit "accept
#     current .zshrc as new baseline" action (``audit.reset_baseline``).
#   * ``script_inventory`` has no ``self_update``. The applier writes config,
#     not workspace scripts; a deploy is what installs and removes those. An
#     applied proposal accounting for a new script would be a coincidence of
#     timing, not a cause.
#   * ``sudoers_baseline`` has no ``operator_intent``. Its check is
#     content-bound (see _explain_sudoers), and both tiers there are strictly
#     stronger than any "the operator ran something" record could be.
#
# ``deploy`` is per-bot wherever it appears: ``identity_file``,
# ``script_inventory`` and ``pod_perms`` are each about ONE bot's files, and
# only that bot's own deploy stamp counts (see _deploy_events). A
# ``pod_perms`` change on a pod-wide directory carries no bot and therefore
# gets no deploy credit at all — a deploy re-asserts the pod-perms contract,
# it does not break it.
#
# The three incursion kinds with NO entry below (2026-09-02) are absent for
# the same reason ``shell_config`` is, and the absence is the design:
#
#   * ``authorized_keys`` — no Evolve code path writes any user's
#     ``~/.ssh/authorized_keys``. Deploy grants evolve a READ ACL on a bot's
#     ``.openclaw`` tree and nothing more; ``bot_credential_inventory`` treats
#     an authorized_keys file in a bot home as noteworthy precisely because
#     Evolve never puts one there. So no source could answer, and an added key
#     is unexplained by construction — which is the correct verdict, not a
#     gap. The operator's own new laptop key pages once and is then accepted
#     by reblessing the baseline.
#   * ``job_inventory`` — a new ``ai.evolve.*`` label IS explained, but by
#     LABEL OWNERSHIP, which the detector settles itself without a time
#     window (``incursion.job_inventory._OWNED_LABEL_PREFIXES``). What is left
#     for a gate to explain is a repointed program on a known label, and the
#     only source that could speak to that is ``deploy`` — which is per-bot
#     (see ``_deploy_events``) while every job here is pod-level, so it would
#     be a registered source that can never fire. An installer-written marker
#     of the plists it rendered, in the shape of ``_explain_sudoers``, is the
#     honest way to add this later.
#   * ``login_source`` — a login is not a change to a file, and nothing on the
#     pod records "the operator was expected to connect from here". The
#     baseline of known (user, source) pairs IS that record, and it lives with
#     the detector.
#
# ``pam_config`` is the one that HAS a source, and it is a real one: the
# host's own package-install record names the packages it upgraded and when
# (see :func:`_os_update_events`).
_KIND_SOURCES: dict[str, tuple[str, ...]] = {
    KIND_SUDOERS_BASELINE: (SOURCE_DEPLOY,),
    KIND_IDENTITY_FILE: (SOURCE_DEPLOY, SOURCE_SELF_UPDATE),
    KIND_SCRIPT_INVENTORY: (SOURCE_DEPLOY,),
    KIND_POD_PERMS: (SOURCE_DEPLOY,),
    KIND_DEPLOY_VERSION: (SOURCE_BY_DEFINITION,),
    KIND_PAM_CONFIG: (SOURCE_OS_UPDATE,),
}


# ── Windows ───────────────────────────────────────────────────────────────────

# How long after a deploy a file the deploy writes may still differ from its
# baseline. The audit and the deploy sweep both run every 15 minutes, so this
# is generous by roughly an order of magnitude — deliberately, because the
# cost of being tight here is a page for Evolve's own work, and the cost of
# being loose is bounded by the fact that a deploy is itself a recorded,
# operator-initiated event.
DEPLOY_WINDOW = timedelta(hours=6)

# The proposal applier's own result records. Matches the apply-results
# retention the drift check in heal.py already credits.
SELF_UPDATE_WINDOW = timedelta(hours=24)

# How long after an OS/system-package install a file that package owns may
# still differ from its baseline. Wider than DEPLOY_WINDOW because an OS
# update is not one atomic write: macOS finishes staged work across the
# reboot that follows, and an apt run can leave a `.pacnew`-style
# reconciliation for the next boot. A day is the smallest window that does
# not page for the tail of a routine patch cycle.
OS_UPDATE_WINDOW = timedelta(hours=24)

# Explanation memo entries older than this are pruned on the next write.
MEMO_RETENTION = timedelta(days=365)

_MEMO_RELPATH = "security/drift-explained.json"
_SUDOERS_MARKER_RELPATH = "state/sudoers-installed.sha256"
_AUDIT_LOG_FILENAME = "audit-log.jsonl"


# ── Records ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DriftChange:
    """One detected difference, described in the terms the gate needs.

    ``kind``          one of the ``KIND_*`` constants above.
    ``bot_id``        the bot the change belongs to, or ``None``/``"evolve"``
                      for a pod-level one.
    ``target``        the path or file the change is about. Used verbatim in
                      the memo key and by the OpenClaw re-harden predicate.
    ``content_hash``  the live hash, where the producer computes one. Its
                      presence is what makes an explanation memoisable.
    ``keys``          config field paths implicated, for matching against
                      operator intent records and apply results.

    Nothing else. A producer's own sub-classification and detail text stay
    with the producer — the gate asks the same question of every drift, and a
    field it does not read is a field a caller can be misled into filling.
    """

    kind: str
    bot_id: str | None = None
    target: str = ""
    content_hash: str = ""
    keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class Explanation:
    """Why a change is accounted for.

    ``source``   which member of the allow-set answered.
    ``evidence`` one operator-legible sentence, written to be read aloud. It
                 carries NO timestamp: a date inside the prose is data the
                 readability bar has to score as words, and it pushed every
                 line past grade 10 for nothing. The moment lives in ``at``,
                 and :meth:`line` puts the two back together for a surface
                 that wants one string.
    ``at``       ISO-8601 timestamp of the authorizing event, or ``""`` when
                 the source is not dated (a content match has no date).
    """

    source: str
    evidence: str
    at: str = ""

    def line(self) -> str:
        """Evidence plus the moment, for a surface that shows one string."""
        if not self.at:
            return self.evidence
        moment = _parse_iso(self.at)
        return f"{self.evidence} ({_human(moment) if moment else self.at})"

    def as_details(self) -> dict[str, str]:
        """The shape producers attach to a Signal's ``details``."""
        return {
            "authorized_by": self.source,
            "authorized_evidence": self.evidence,
            "authorized_at": self.at,
        }


@dataclass
class _Event:
    """An authorized event found on disk, before it becomes an Explanation."""

    at: datetime
    evidence: str


# ── Time helpers ──────────────────────────────────────────────────────────────


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(raw: Any) -> datetime | None:
    """Parse an ISO-8601 stamp, tolerating both ``Z`` and ``+00:00``.

    Naive stamps are read as UTC — every writer in this pod stamps UTC, and
    treating a naive one as local time would shift it by hours in exactly the
    direction that silently widens a window.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _within(event_at: datetime | None, now: datetime, window: timedelta) -> bool:
    """Whether ``event_at`` sits inside ``window`` looking back from ``now``.

    A stamp in the FUTURE does not qualify. Clock skew or a hand-edited
    record must not be able to hold a window open indefinitely.
    """
    if event_at is None:
        return False
    if event_at > now:
        return False
    return (now - event_at) <= window


def _human(at: datetime) -> str:
    return at.strftime("%Y-%m-%d %H:%M UTC")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


# ── Source: deploy / version bump ─────────────────────────────────────────────


def _deploy_events(shared_dir: Path, bot_id: str | None, now: datetime) -> list[_Event]:
    """The recent deploy of ``bot_id`` itself, if there was one.

    Reads exactly one record: ``install.json::bot_versions[<bot>].deployed_at``,
    the stamp that bot's own redeploy writes — which is what actually rewrites
    its workspace files and re-grants its permissions.

    The pod-wide stamps in the same file (``installed_at``) and in the release
    pointer (``release.json::stable.promoted_at``) are NOT read. Both move on
    every per-bot deploy — ``write_install_json`` rewrites ``installed_at`` to
    now on each ``evolve-admin deploy <bot>`` — so crediting them would open a
    six-hour window in which deploying bot X explains bot Y's drift, and the
    memo would then keep that verdict by content hash for a year. No kind
    that consults this source is pod-level, so a change with no ``bot_id``
    has no deploy explanation.
    """
    if not bot_id:
        return []
    install = _read_json(Path(shared_dir) / "install.json")
    if not isinstance(install, dict):
        return []
    versions = install.get("bot_versions")
    if not isinstance(versions, dict):
        return []
    stamp = versions.get(bot_id)
    if not isinstance(stamp, dict):
        return []
    at = _parse_iso(stamp.get("deployed_at"))
    if not _within(at, now, DEPLOY_WINDOW):
        return []
    assert at is not None  # _within rejects None
    version = str(stamp.get("version") or "?")
    return [_Event(at, f"{bot_id} was set up again by a deploy of {version}")]


# ── Source: Evolve self-update (the proposal pipeline) ────────────────────────


def applied_config_keys(record: Any) -> set[str]:
    """Top-level ``openclaw.json`` keys an apply-result declared touching.

    The two shapes the (retired, 2026-08-18) apply daemon wrote, and the one
    extraction for both — ``heal._get_recently_applied_config_keys`` reads
    the same directory through this helper:

      * ``{"status": "applied", "proposed_change": {"agents.defaultModel": …}}``
        declares the top-level key of every dotted path in the change;
      * ``{"success": true, "action_taken": "set_agents.defaultModel"}``
        declares the top-level key of the one path the action set.

    Anything else declares nothing. A declaration here is always an
    ``openclaw.json`` key — never an instruction file's name.
    """
    if not isinstance(record, dict):
        return set()
    keys: set[str] = set()
    if record.get("status") == "applied":
        change = record.get("proposed_change")
        if isinstance(change, dict):
            keys.update(str(k).split(".")[0] for k in change if k)
    if record.get("success"):
        action = str(record.get("action_taken") or "")
        if action.startswith("set_"):
            top = action[4:].split(".")[0]
            if top:
                keys.add(top)
    return keys


def _applied_file_name(record: Any) -> str:
    """The file name an arbiter applier recorded writing, or ``""``.

    ``arbiter.apply`` persists the applier's ``ApplyResult.details`` under
    ``provenance.signals._apply_details``; SoulEdit — and AgentsAppend, which
    delegates to it — record the ``path`` they wrote there. That path's
    basename (``AGENTS.md`` / ``SOUL.md``) is the one thing an applied
    proposal can truthfully declare about an instruction file.
    """
    if not isinstance(record, dict):
        return ""
    provenance = record.get("provenance")
    signals = provenance.get("signals") if isinstance(provenance, dict) else None
    details = signals.get("_apply_details") if isinstance(signals, dict) else None
    path = details.get("path") if isinstance(details, dict) else None
    return Path(str(path)).name if isinstance(path, str) and path.strip() else ""


def _applied_transition_at(record: dict[str, Any]) -> datetime | None:
    """When the proposal's history says it reached ``applied`` (latest)."""
    history = record.get("history")
    if not isinstance(history, list):
        return None
    stamps = [
        _parse_iso(entry.get("at"))
        for entry in history
        if isinstance(entry, dict) and entry.get("to_status") == "applied"
    ]
    stamps = [at for at in stamps if at is not None]
    return max(stamps) if stamps else None


def _self_update_events(
    shared_dir: Path, bot_id: str | None, keys: Iterable[str], now: datetime,
) -> list[_Event]:
    """Changes Evolve made to its own configuration through the applier.

    A result counts only if it DECLARED touching one of ``keys`` — an applier
    that changed the model routing does not account for a rewritten
    instruction file. A record that declares nothing explains nothing, and
    so does one with no applied stamp (there is no file-mtime fallback: the
    directory's mode is not a timestamp anyone signed). With no ``keys`` to
    match, nothing qualifies.

    Two records, both Evolve's own:

      * ``proposals/apply-results/`` — the retired apply daemon's records,
        read through :func:`applied_config_keys`. These declare
        ``openclaw.json`` keys only, so they can answer a config-key
        question and never an identity-file one.
      * ``proposals/applied/`` and ``proposals/archived/`` — arbiter
        proposals whose applier recorded the file it wrote
        (:func:`_applied_file_name`), stamped by the history entry that
        moved them to ``applied``. This is what accounts for a SoulEdit or
        AgentsAppend the operator approved.
    """
    wanted = {k for k in keys if k}
    if not wanted:
        return []
    events: list[_Event] = []
    proposals_dir = Path(shared_dir) / "proposals"

    results_dir = proposals_dir / "apply-results"
    if results_dir.is_dir():
        pattern = f"{bot_id}-*.json" if bot_id else "*.json"
        try:
            candidates = sorted(results_dir.glob(pattern))
        except OSError:
            candidates = []
        for path in candidates:
            record = _read_json(path)
            if not isinstance(record, dict):
                continue
            at = _parse_iso(record.get("applied_at"))
            if not _within(at, now, SELF_UPDATE_WINDOW):
                continue
            assert at is not None
            if not (wanted & applied_config_keys(record)):
                continue
            title = str(record.get("title") or record.get("proposal_id") or path.stem)
            events.append(_Event(at, f"Evolve applied a change you approved: {title}"))

    for subdir in ("applied", "archived"):
        store_dir = proposals_dir / subdir
        if not store_dir.is_dir():
            continue
        try:
            candidates = sorted(store_dir.glob("*.json"))
        except OSError:
            continue
        for path in candidates:
            record = _read_json(path)
            if not isinstance(record, dict):
                continue
            if bot_id and str(record.get("bot_id") or "") != bot_id:
                continue
            if _applied_file_name(record) not in wanted:
                continue
            at = _applied_transition_at(record)
            if not _within(at, now, SELF_UPDATE_WINDOW):
                continue
            assert at is not None
            title = str(record.get("title") or record.get("id") or path.stem)
            events.append(_Event(at, f"Evolve applied a change you approved: {title}"))

    events.sort(key=lambda e: e.at, reverse=True)
    return events


# ── The explanation memo ──────────────────────────────────────────────────────


def _memo_key(change: DriftChange) -> str | None:
    """Stable key for a memoisable change, or ``None`` when it is not.

    Keyed on the CONTENT, so remembering an explanation can never carry over
    to different content. A change with no content hash is not memoisable.
    """
    if not change.content_hash:
        return None
    raw = f"{change.kind}:{change.bot_id or 'pod'}:{change.target}:{change.content_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _memo_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / _MEMO_RELPATH


def _load_memo(shared_dir: Path) -> dict[str, Any]:
    data = _read_json(_memo_path(shared_dir))
    if isinstance(data, dict) and isinstance(data.get("entries"), dict):
        return data
    return {"version": 1, "entries": {}}


def _remember(
    shared_dir: Path, change: DriftChange, explanation: Explanation, now: datetime,
) -> None:
    """Record that ``explanation`` accounted for this exact content.

    Best-effort: a memo that cannot be written costs a future page, never a
    missed one, so a write failure is logged and swallowed.
    """
    key = _memo_key(change)
    if key is None:
        return
    data = _load_memo(shared_dir)
    entries: dict[str, Any] = data["entries"]
    before = len(entries)
    if key not in entries:
        entries[key] = {
            "kind": change.kind,
            "bot_id": change.bot_id,
            "target": change.target,
            "content_hash": change.content_hash,
            "source": explanation.source,
            "evidence": explanation.evidence,
            "at": explanation.at,
            "first_explained_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    cutoff = now - MEMO_RETENTION
    kept = {
        k: v for k, v in entries.items()
        if (_parse_iso(v.get("first_explained_at")) or now) >= cutoff
    }
    if len(kept) == before:
        # Nothing new and nothing to prune. The producers that call this run
        # every 15 minutes against a standing explained condition, so skipping
        # the no-op rewrite is the difference between one write and ~100/day.
        return
    data["entries"] = kept
    path = _memo_path(shared_dir)
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp name: audit and pod_perms_drift_monitor both reach this,
        # on their own schedules, and a shared fixed name would let one
        # rename the other's half-written file into place.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".drift-explained.", suffix=".tmp",
        )
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        tmp.replace(path)
    except OSError as exc:
        logger.debug("drift_authorization: memo write failed: %s", exc)
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError as cleanup_exc:
                logger.debug(
                    "drift_authorization: could not remove the staged memo "
                    "%s: %s", tmp, cleanup_exc,
                )


def _recall(shared_dir: Path, change: DriftChange) -> Explanation | None:
    """An explanation an earlier run issued for this exact content."""
    key = _memo_key(change)
    if key is None:
        return None
    entry = _load_memo(shared_dir)["entries"].get(key)
    if not isinstance(entry, dict):
        return None
    return Explanation(
        source=SOURCE_REMEMBERED,
        evidence=(
            "this was already accounted for: "
            f"{entry.get('evidence') or 'a change Evolve or you made'}"
        ),
        at=str(entry.get("at") or ""),
    )


# ── Per-kind explanation ──────────────────────────────────────────────────────


def _sudoers_render_hash() -> str | None:
    """Hash of the sudoers file this Evolve version would install.

    ``None`` when the render is unavailable — which the caller must treat as
    "cannot verify", never as "verified clean".
    """
    try:
        from evolve_admin import setup_wizard  # pyright: ignore[reportMissingImports]  # noqa: PLC0415 — heavy, lazy by design
        content = setup_wizard._render_evolve_sudoers()
    except Exception as exc:  # noqa: BLE001 — any import/render failure is "unavailable"
        logger.debug("drift_authorization: sudoers render unavailable: %s", exc)
        return None
    if not content:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _sudoers_marker(shared_dir: Path) -> tuple[str, datetime | None] | None:
    path = Path(shared_dir) / _SUDOERS_MARKER_RELPATH
    try:
        return path.read_text(encoding="utf-8").strip(), _mtime(path)
    except OSError:
        return None


def _explain_sudoers(change: DriftChange, shared_dir: Path) -> Explanation | None:
    """The content-bound sudoers check. See the module docstring.

    No ``now`` parameter, unlike its sibling below: both tiers compare
    content, so there is no window for a clock to be measured against.
    """
    live = change.content_hash
    if not live:
        return None

    rendered = _sudoers_render_hash()
    if rendered is not None and rendered == live:
        return Explanation(
            source=SOURCE_DEPLOY,
            evidence="the file matches the one this version of Evolve sets up",
        )

    marker = _sudoers_marker(shared_dir)
    if marker is not None and marker[0] == live:
        return Explanation(
            source=SOURCE_DEPLOY,
            evidence="the file is the one Evolve's own installer set up here last",
            at=marker[1].strftime("%Y-%m-%dT%H:%M:%SZ") if marker[1] else "",
        )

    # No third tier. An operator "I ran refresh-sudoers" record would be
    # strictly weaker than both hash comparisons above — a successful run
    # writes the marker tier 2 already reads — and the wizard logs that action
    # to a different file (logs/admin-actions.jsonl, keyed ``ts``/``bot``)
    # than the one this module reads, so a third tier here would have been a
    # branch that could never fire.
    return None


# ── Source: the host's own OS / system-package install record ────────────────
#
# The only source that reads outside ``{shared_dir}``, and the only one whose
# record Evolve does not write. That is what makes it usable: an attacker who
# wants to launder a ``/etc/pam.d`` edit through this source has to forge a
# root-owned system log, which is a strictly larger ask than the edit itself.
#
# The two platforms answer with different precision, and the evidence text
# says which one answered so the operator is never misled:
#
#   Linux  — ``/var/log/dpkg.log`` names the PACKAGE and the moment. Matched
#            narrowly: only a package whose name carries the changed kind's
#            hint (``pam`` for ``/etc/pam.d``) counts, so a routine ``apt
#            upgrade`` of something unrelated explains nothing.
#   macOS  — ``/Library/Receipts/InstallHistory.plist`` records OS updates as
#            a whole; Apple ships no per-file package identifier for
#            ``/etc/pam.d``. So the macOS answer is necessarily coarser: "the
#            OS was updated at T". It is still a real, dated, host-written
#            record of an authorized event, which is the bar this allow-set
#            sets — but it is the reason the window is a day and not a week.
#            It is also why the receipt has to be a SOFTWARE update: see
#            ``_MACOS_OS_UPDATE_PACKAGE_PREFIXES``.
#
# Both paths are module-level so a test can point them at a fixture; neither
# is ever written.

_MACOS_INSTALL_HISTORY = Path("/Library/Receipts/InstallHistory.plist")
_DPKG_LOGS: tuple[Path, ...] = (
    Path("/var/log/dpkg.log"),
    Path("/var/log/dpkg.log.1"),
)

#: Which package-name fragments may explain which kind. A kind with no entry
#: gets no OS-update explanation at all, even on a host that updated an hour
#: ago — the source has to be able to say "the package that owns THIS file",
#: not merely "something was installed".
_OS_UPDATE_PACKAGE_HINTS: dict[str, tuple[str, ...]] = {
    KIND_PAM_CONFIG: ("pam",),
}

#: dpkg actions that rewrite a package's conffiles. ``remove``/``purge`` are
#: absent: a removal that deletes a PAM policy is exactly the change the
#: detector must still page for.
_DPKG_INSTALL_ACTIONS = frozenset({"install", "upgrade", "configure"})

#: The macOS receipt identifiers that count as "the OS updated its own
#: software", prefix → why that prefix qualifies.
#:
#: The predicate used to be ``any identifier starting com.apple.`` — which
#: review #3967 showed is not a narrow test at all. macOS writes a receipt for
#: every background DATA refresh too: XProtect signatures, MRT definitions,
#: Gatekeeper config. Those land near-daily on a healthy Mac and rewrite no
#: file in ``/etc``, so "any com.apple.* receipt in the last 24h" meant a
#: ``/etc/pam.d`` edit was explained away on most days of the year — the
#: opposite of the ``_KIND_SOURCES`` doctrine that a source must be able to
#: answer "the package that owns THIS file", not merely "something happened".
#:
#: This is an allow-list on purpose: a receipt identifier nobody listed here
#: explains nothing, so a new Apple data-refresh channel is silently narrow
#: rather than silently wide. Adding a prefix is a deliberate widening and
#: needs its reason written next to it.
_MACOS_OS_UPDATE_PACKAGE_PREFIXES: dict[str, str] = {
    "com.apple.pkg.update.os": (
        "the macOS system software update payload — the receipt Software "
        "Update writes when it replaces OS files, which is the only macOS "
        "event that plausibly rewrites /etc/pam.d"
    ),
    "com.apple.pkg.update.security": (
        "a security update / Rapid Security Response payload: software, "
        "replacing system binaries and configuration, not a definitions file"
    ),
    "com.apple.pkg.MobileSoftwareUpdate": (
        "the software-update installer bundle itself; present in the receipts "
        "of an OS update applied through the MSU mechanism"
    ),
    "com.apple.MobileSoftwareUpdate": (
        "the same MSU mechanism under its non-`pkg` identifier — some macOS "
        "releases record it without the `pkg` segment"
    ),
}

#: Receipt identifiers that are DATA refreshes, listed only so the test suite
#: can assert by name that they explain nothing. They are excluded by
#: construction (the allow-list above does not cover them); this set exists so
#: that a future edit which widens the allow-list has to break a test that
#: names the exact receipts the 2026-09-02 review found doing the laundering.
_MACOS_DATA_ONLY_RECEIPT_IDS: tuple[str, ...] = (
    "com.apple.pkg.XProtectPlistConfigData",
    "com.apple.pkg.XProtectPayloads",
    "com.apple.pkg.MRTConfigData",
    "com.apple.pkg.GatekeeperConfigData",
)


def _macos_os_update_reason(identifiers: list) -> "tuple[str, str] | None":
    """``(identifier, why it qualifies)`` for the first OS-software receipt.

    ``None`` when every identifier on the receipt is something else — a data
    refresh, a third-party ``.pkg``, an Apple app update.
    """
    for identifier in identifiers:
        text = str(identifier)
        for prefix, reason in _MACOS_OS_UPDATE_PACKAGE_PREFIXES.items():
            if text.startswith(prefix):
                return text, reason
    return None


def _dpkg_update_events(change: DriftChange, now: datetime) -> list[_Event]:
    """Recent installs/upgrades of a package that owns ``change``'s kind."""
    hints = _OS_UPDATE_PACKAGE_HINTS.get(change.kind)
    if not hints:
        return []
    events: list[_Event] = []
    for log in _DPKG_LOGS:
        try:
            text = log.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[2] not in _DPKG_INSTALL_ACTIONS:
                continue
            package = parts[3].split(":")[0]
            if not any(hint in package.lower() for hint in hints):
                continue
            try:
                # dpkg stamps local wall-clock with no offset; astimezone()
                # attaches the host's zone so the comparison against a UTC
                # ``now`` is a real one and not an hours-off guess.
                at = datetime.strptime(
                    f"{parts[0]} {parts[1]}", "%Y-%m-%d %H:%M:%S",
                ).astimezone()
            except ValueError:
                continue
            if not _within(at, now, OS_UPDATE_WINDOW):
                continue
            events.append(_Event(
                at,
                f"the host updated {package}, which owns this file",
            ))
    events.sort(key=lambda e: e.at, reverse=True)
    return events


def _macos_update_events(change: DriftChange, now: datetime) -> list[_Event]:
    """A recent OS update, from the receipts database macOS keeps itself."""
    if change.kind not in _OS_UPDATE_PACKAGE_HINTS:
        return []
    import plistlib

    try:
        raw = _MACOS_INSTALL_HISTORY.read_bytes()
    except OSError:
        return []
    try:
        entries = plistlib.loads(raw)
    except Exception:  # noqa: BLE001 — a malformed receipts file explains nothing
        logger.debug("drift_authorization: unreadable install history")
        return []
    if not isinstance(entries, list):
        return []
    events: list[_Event] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ids = entry.get("packageIdentifiers")
        ids = ids if isinstance(ids, list) else []
        qualifying = _macos_os_update_reason(ids)
        if qualifying is None:
            # A third-party .pkg, an Apple app update, or — the case that
            # motivated the allow-list — a background data refresh
            # (XProtect / MRT / Gatekeeper). None of them touch /etc/pam.d.
            continue
        at = entry.get("date")
        if isinstance(at, datetime):
            # plistlib returns naive datetimes that are UTC by the format's
            # own definition; say so rather than letting the comparison
            # silently adopt the host's zone.
            at = at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at
        else:
            at = _parse_iso(at)
        if not _within(at, now, OS_UPDATE_WINDOW):
            continue
        assert at is not None  # _within rejects None
        name = str(entry.get("displayName") or "an OS update")
        identifier, _reason = qualifying
        events.append(_Event(
            at,
            f"the host installed an OS software update called {name} "
            f"({identifier})",
        ))
    events.sort(key=lambda e: e.at, reverse=True)
    return events


def _os_update_events(change: DriftChange, now: datetime) -> list[_Event]:
    """The platform's own install record, if it accounts for this change."""
    from platform_profile import get_profile

    if get_profile().name == "macos":
        return _macos_update_events(change, now)
    return _dpkg_update_events(change, now)


def _explain_from_sources(
    change: DriftChange, shared_dir: Path, now: datetime, sources: tuple[str, ...],
) -> Explanation | None:
    """Walk ``sources`` in order and return the first that answers."""
    for source in sources:
        if source == SOURCE_DEPLOY:
            events = _deploy_events(shared_dir, change.bot_id, now)
        elif source == SOURCE_SELF_UPDATE:
            events = _self_update_events(shared_dir, change.bot_id, change.keys, now)
        elif source == SOURCE_OS_UPDATE:
            events = _os_update_events(change, now)
        else:
            events = []
        if events:
            at = events[0].at
            return Explanation(
                source=source,
                evidence=events[0].evidence,
                at=at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
    return None


# ── The gate ──────────────────────────────────────────────────────────────────


def explain(
    change: DriftChange,
    shared_dir: Path,
    *,
    now: datetime | None = None,
    memo: bool = True,
) -> Explanation | None:
    """Is this change accounted for by a known authorized event?

    Returns the :class:`Explanation` when one is, ``None`` when nothing
    explains it. ``None`` is the security answer: the caller must surface it
    as an event-classified finding that pages.

    ``memo=False`` disables both reading and writing the explanation memo,
    for callers that want only the live sources (and for tests that assert
    what the live sources alone say).
    """
    now = now or _utc_now()
    shared_dir = Path(shared_dir)

    if change.kind == KIND_DEPLOY_VERSION:
        return Explanation(
            source=SOURCE_BY_DEFINITION,
            evidence=(
                "this is Evolve updating itself — the bots catch up next round"
            ),
        )

    sources = _KIND_SOURCES.get(change.kind)
    if sources is None:
        # An unregistered kind has no allow-set, so nothing can explain it.
        # Fail toward paging rather than inventing a default allow-set.
        logger.debug("drift_authorization: no allow-set for kind %r", change.kind)
        return None

    if change.kind == KIND_SUDOERS_BASELINE:
        found = _explain_sudoers(change, shared_dir)
    else:
        found = _explain_from_sources(change, shared_dir, now, sources)

    if found is not None:
        if memo:
            _remember(shared_dir, change, found, now)
        return found
    if memo:
        return _recall(shared_dir, change)
    return None


def partition(
    changes: Iterable[DriftChange],
    shared_dir: Path,
    *,
    now: datetime | None = None,
) -> tuple[list[tuple[DriftChange, Explanation]], list[DriftChange]]:
    """Split ``changes`` into ``(explained, unexplained)``.

    For producers that detect a SET of drifted targets in one pass. The
    unexplained list is the one that becomes a security finding; a run whose
    unexplained list is empty should emit nothing at all.
    """
    now = now or _utc_now()
    explained: list[tuple[DriftChange, Explanation]] = []
    unexplained: list[DriftChange] = []
    for change in changes:
        found = explain(change, shared_dir, now=now)
        if found is None:
            unexplained.append(change)
        else:
            explained.append((change, found))
    return explained, unexplained


# ── Operator report ───────────────────────────────────────────────────────────
#
# The read-only answer to "what drift did Evolve see this week, and what
# explained it?". Everything the gate decides shows up in the audit log —
# explained drift as an OK line carrying its evidence, unexplained drift as a
# CRITICAL — so the report is a read of that one file. No pod state is
# touched, nothing is written, and it needs no privilege beyond reading the
# log the audit already writes.
#
#   python3 packages/analyzer/drift_authorization.py --shared-dir <dir> --days 7

#: Message fragments that mark a line as one of the drift checks the report
#: lists. Matched case-insensitively against the audit log line. The shell
#: startup check is posture-only and never gate-adjudicated (see
#: _KIND_SOURCES), so it always lists as UNEXPLAINED — it is here so the
#: weekly answer to "did anything change that nobody accounts for?" is not
#: silent about the one file nothing authorized ever writes.
_REPORT_FRAGMENTS: tuple[tuple[str, str], ...] = (
    ("sudoers changed since baseline", "sudoers"),
    ("/etc/sudoers.d/evolve changed", "sudoers"),
    (".zshrc hash changed since baseline", "shell startup"),
    ("differs from the backup", "instructions"),
    ("hash mismatch vs git backup", "instructions"),
    ("script inventory changed", "workspace scripts"),
    ("script inventory drift", "workspace scripts"),
)

#: How an explained line separates the finding from its evidence. The
#: producers all use the same em-dash join, which is what makes the
#: explanation a column rather than a paragraph.
_EVIDENCE_SEPARATOR = " — "


@dataclass(frozen=True)
class ReportRow:
    when: str
    check: str
    explained: bool
    finding: str
    explanation: str


def report_rows(shared_dir: Path, *, days: int = 7,
                now: datetime | None = None) -> list[ReportRow]:
    """Drift findings from the last ``days`` of the audit log, newest last.

    Read-only. An unreadable or absent log yields no rows — the report says
    nothing rather than guessing.
    """
    now = now or _utc_now()
    cutoff = now - timedelta(days=days)
    path = Path(shared_dir) / "logs" / "audit.log"
    rows: list[ReportRow] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    for raw in text.splitlines():
        parts = raw.split(" ", 1)
        if len(parts) != 2:
            continue
        stamp, rest = parts
        at = _parse_iso(stamp)
        if at is None or at < cutoff:
            continue
        lowered = rest.lower()
        check = next((c for frag, c in _REPORT_FRAGMENTS if frag in lowered), "")
        if not check:
            continue
        # "[audit] OK: …" is the explained branch; "[audit] CRITICAL: …" and
        # "[audit] WARN: …" are the unexplained ones.
        explained = rest.startswith("[audit] OK:")
        body = rest.split(":", 1)[1].strip() if ":" in rest else rest
        finding, _, explanation = body.partition(_EVIDENCE_SEPARATOR)
        rows.append(ReportRow(
            when=stamp,
            check=check,
            explained=explained,
            finding=finding.strip(),
            explanation=explanation.strip() if explained else "",
        ))
    return rows


def render_report(rows: list[ReportRow]) -> str:
    """The rows as a plain table, unexplained ones first."""
    if not rows:
        return "No drift findings in the window."
    ordered = sorted(rows, key=lambda r: (r.explained, r.when))
    head = f"{'WHEN':21} {'VERDICT':12} {'CHECK':18} FINDING / EXPLANATION"
    out = [head, "-" * len(head)]
    for row in ordered:
        verdict = "explained" if row.explained else "UNEXPLAINED"
        tail = row.explanation if row.explained else row.finding
        out.append(f"{row.when:21} {verdict:12} {row.check:18} {tail}")
    n_open = sum(1 for r in rows if not r.explained)
    out.append("")
    out.append(
        f"{len(rows)} drift finding(s); {n_open} with nothing on record to "
        "explain them."
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "List recent drift findings and what authorized them. "
            "Read-only: reads the audit log and writes nothing."
        ),
    )
    parser.add_argument("--shared-dir", required=True,
                        help="the pod's shared directory")
    parser.add_argument("--days", type=int, default=7,
                        help="how far back to look (default: 7)")
    args = parser.parse_args(argv)
    print(render_report(report_rows(Path(args.shared_dir), days=args.days)))
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI entry
    raise SystemExit(main())
