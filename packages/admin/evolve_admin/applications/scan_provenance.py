"""scan_provenance — what the last app scan on a bot actually did.

ALPHA-2 (internal/audit-alpha-journey-2026-08.md §4.1, findings B2a / U2 / U5).
Three findings of one shape: *the surface asserts something the pod did not do,
because scan provenance never reaches the reader.*

  - The scanner writes ``llm_degraded: true`` into ``.scan-status.json`` when no
    provider key resolves and it falls back to a structural pass. Before this
    module nothing read that field, so ``POST /api/applications/sync/pod``
    reported "ran full scan; discovered 0 app(s)" on a pod where the part of the
    scan that recognises apps never ran at all.
  - Discovered's only empty state said "everything the scanner has found has
    been vouched for or set aside" — false, and discouragingly so, on a pod the
    scanner has never visited.

The primitive is this module: read the scan's own status file, classify it into
ONE of four states, and let every empty state / banner / summary branch on that
instead of guessing.

THE FOUR STATES (docs/principle-tri-state-status.md — ``null`` is never ``0``,
"we could not tell" is never "we looked and there was nothing"):

    never_scanned  no status file: no scan has ever written one for this bot
    ok             a scan ran to completion with its model phase behind it
    degraded       a scan ran but skipped part of itself, or never finished
    unreadable     the status file exists (or may) and we could not read it

``unreadable`` is deliberately NOT folded into ``never_scanned``. A pod whose
ACLs are wrong would otherwise render as a pod nobody has scanned, and the
operator would be told to run a scan that is already running fine.

``ok`` REQUIRES A TERMINAL RECORD, and that is load-bearing rather than
pedantic. ``scanner._write_status`` stamps the file at every phase and only the
final write carries ``status: "done"``; there is no early return between, so a
scan killed at Phase 2 leaves a phase-2 record on disk indefinitely (the admin
server deletes a stale one only when a NEW scan starts). Classifying that as
``ok`` would put "everything the scanner found has been vouched for" under a
scan that never finished — the exact assertion this module exists to prevent.
An independent review caught it; the first cut of this module had the bug.

WHERE THE FILE LIVES. ``manifest.applications_dir`` — the same resolver
``scan_workspace_pipeline`` uses when ``output_dir`` is None, which is the
smart-sync path and therefore every write this module's readers care about.
Stated precisely because the guarantee is not universal: the older
``POST /api/applications/scan`` route passes an explicit ``output_dir``
(``analyzer/application_scanner.py`` builds ``user_home(os_user)/.openclaw/
workspace/manifests``), and that differs from ``applications_dir`` on a bot whose
``openclaw.json`` sets a custom workspace. Identical on a stock pod; a bot with a
relocated workspace scanned through the old route would read ``never_scanned``
here. ``provenance_for`` guards the worst version of that (see below).
The scanner's module docstring also promises a mirror under
``{shared_dir}/applications/{bot_id}/``; that mirror was removed from the admin
server's scan monitor ("the scanner is the single source of truth") and the
ALPHA-1 audit observed the directory empty, so this module does not read it.

READS ONLY, and via the documented pattern: direct read first, ``sudo /bin/cat``
after. Never ``sudo -u <bot>`` — the ``evolve`` user has no such grant
(CLAUDE.md, "Reads — use direct reads, not ``sudo -u <bot>``").
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from platform_profile import get_profile

log = logging.getLogger(__name__)

#: The scan's own status file, written by ``scanner._write_status``.
SCAN_STATUS_FILENAME = ".scan-status.json"

# ── The four states ───────────────────────────────────────────────────────────

STATE_NEVER_SCANNED = "never_scanned"
STATE_OK = "ok"
STATE_DEGRADED = "degraded"
STATE_UNREADABLE = "unreadable"

#: Every state this module can emit. Consumers that switch on ``state`` should
#: cover all four; a fifth would be a contract change, not a new branch.
SCAN_STATES = (STATE_NEVER_SCANNED, STATE_OK, STATE_DEGRADED, STATE_UNREADABLE)

# ── Read outcomes (the tri-state the filesystem gives us) ─────────────────────

READ_OK = "ok"
READ_MISSING = "missing"
READ_DENIED = "denied"

# ── Why a scan was degraded, in the operator's words ──────────────────────────
#
# docs/principle-alerts-explain-and-remediate.md: every alert-shaped element
# explains itself AND offers a next step. docs/principle-plex-test.md: the
# operator never sees a field name — ``llm_degraded`` and
# ``no_llm_provider_key`` are keys on the wire and nothing more.

#: The scanner's own key for "no provider key resolved, structural pass only".
REASON_NO_LLM_KEY = "no_llm_provider_key"

#: The scanner's key for "the caller asked for --no-llm" — a completed run
#: that nonetheless skipped the pass that recognises apps.
REASON_STRUCTURAL_BY_REQUEST = "structural_by_request"

#: Ours, not the scanner's: the record never reached ``status: "done"``.
REASON_UNFINISHED = "scan_did_not_finish"

#: Ours: the path we resolved is not a workspace at all, so a missing status
#: file there proves nothing about whether this bot has been scanned.
REASON_NO_WORKSPACE = "workspace_not_found"

#: What a COMPLETED scan's status file says. ``scanner._write_status`` puts this
#: in the ``extra`` of the final write only; every phase write before it carries
#: no ``status`` key at all.
STATUS_DONE = "done"

#: reason key → (what happened, what to do about it). Both are whole sentences
#: because they are rendered as prose, not as a label.
DEGRADED_REASONS: dict[str, tuple[str, str]] = {
    REASON_NO_LLM_KEY: (
        "Discovery ran without a model — Evolve found no working provider key, "
        "so only the pass that lists files and folders ran, and on its own that "
        "pass does not recognise apps.",
        "Add a provider key under Plugins → Credentials, then run Sync again.",
    ),
    # The caller asked for a structural pass. Not a failure — but the run
    # still did not do the part that recognises apps, and the surface must not
    # read an empty result as "there is nothing here".
    REASON_STRUCTURAL_BY_REQUEST: (
        "This was a quick scan — it listed files and folders but did not run "
        "the part that recognises apps, so it does not find new ones.",
        "Choose Sync all bots for a full scan when you want Evolve to look "
        "properly.",
    ),
    # A record that never reached ``status: "done"``. Deliberately ONE sentence
    # for two situations — a scan still running, and a scan that died partway —
    # because the file cannot tell them apart, and picking one would be exactly
    # the confident wrong answer this module exists to remove.
    REASON_UNFINISHED: (
        "Evolve has no record of the last scan on this bot finishing — it may "
        "still be running, or it may have stopped partway, so what Discovered "
        "shows is not the whole picture yet.",
        "If no scan is running, run Sync again. If it keeps stopping, check "
        "the admin log for the scan.",
    ),
}

#: Fallback for a degrade reason the scanner grew after this table was written.
#: Honest about the gap rather than silently rendering the raw key.
GENERIC_DEGRADED = (
    "The last scan skipped part of itself, so what Discovered shows may be "
    "incomplete.",
    "Run Sync again. If it keeps skipping, check the admin log for the scan.",
)


def explain_reason(reason: str | None) -> tuple[str, str]:
    """``(what happened, what to do)`` for a degrade reason key.

    An unknown key gets the generic copy rather than the key itself — a raw
    ``no_llm_provider_key`` on screen fails the Plex test, and inventing
    specific advice for a reason we do not recognise fails the remediation one.
    """
    return DEGRADED_REASONS.get(reason or "", GENERIC_DEGRADED)


# ── The per-bot block ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScanProvenance:
    """What the last scan on one bot did, and how sure we are of that.

    ``last_scan_at`` is ``None`` — never a placeholder date — whenever the
    status carries no timestamp, including in every ``never_scanned`` and
    ``unreadable`` case.
    """

    bot_id: str
    state: str
    last_scan_at: str | None = None
    reason: str | None = None
    note: str | None = None
    remedy: str | None = None

    @property
    def degraded(self) -> bool:
        return self.state == STATE_DEGRADED

    @property
    def scanned(self) -> bool:
        """True when a scan is known to have run (well or badly).

        ``unreadable`` is False: we do not know that one ran, and this property
        backs the "has anything ever looked here?" question.
        """
        return self.state in (STATE_OK, STATE_DEGRADED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "state": self.state,
            "last_scan_at": self.last_scan_at,
            "reason": self.reason,
            "note": self.note,
            "remedy": self.remedy,
        }


def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def classify(bot_id: str, status: Mapping[str, Any] | None, read_state: str) -> ScanProvenance:
    """Turn one read of ``.scan-status.json`` into a :class:`ScanProvenance`.

    Pure — no disk, no clock — so the four states are unit-testable without a
    pod. ``read_state`` is one of ``READ_OK`` / ``READ_MISSING`` / ``READ_DENIED``.

    A record that is still ``running`` classifies as ``degraded``, not ``ok``
    and emphatically not ``never_scanned``: something has looked here, but we
    have no evidence it finished, and on disk an in-flight scan is
    indistinguishable from one that died at the same phase. The copy for
    ``scan_did_not_finish`` covers both rather than picking one.
    """
    if read_state == READ_MISSING:
        return ScanProvenance(bot_id=bot_id, state=STATE_NEVER_SCANNED)
    if read_state != READ_OK or not isinstance(status, Mapping):
        return unreadable(bot_id)

    last = _s(status.get("updated_at")) or None

    # TERMINALITY FIRST. A record without ``status: "done"`` is a scan we have
    # no evidence finished — a phase-N write left behind by a killed process
    # looks exactly like this, and it never expires (the admin server clears a
    # stale one only when a NEW scan starts). It outranks the degrade flag
    # below: a run that stopped may also have set ``llm_degraded`` on its way
    # down, and "we do not know that it finished" is the more actionable half.
    if _s(status.get("status")) != STATUS_DONE:
        note, remedy = explain_reason(REASON_UNFINISHED)
        return ScanProvenance(
            bot_id=bot_id, state=STATE_DEGRADED, last_scan_at=last,
            reason=REASON_UNFINISHED, note=note, remedy=remedy,
        )

    if status.get("llm_degraded"):
        reason = _s(status.get("llm_degraded_reason")) or REASON_NO_LLM_KEY
        note, remedy = explain_reason(reason)
        return ScanProvenance(
            bot_id=bot_id, state=STATE_DEGRADED, last_scan_at=last,
            reason=reason, note=note, remedy=remedy,
        )

    return ScanProvenance(bot_id=bot_id, state=STATE_OK, last_scan_at=last)


def unreadable(bot_id: str, *, reason: str | None = None) -> ScanProvenance:
    """The ``unreadable`` block, with copy chosen by WHY we could not tell.

    Two causes need different remedies, and telling an operator to fix the
    wrong one is a dead end (docs/principle-alerts-explain-and-remediate.md):
    a permission problem is repaired by re-deploying the bot, while a workspace
    we could not locate at all is not.
    """
    if reason == REASON_NO_WORKSPACE:
        return ScanProvenance(
            bot_id=bot_id, state=STATE_UNREADABLE, reason=reason,
            note=(
                "Evolve could not find this bot's workspace, so it cannot say "
                "whether the bot has ever been scanned."
            ),
            remedy=(
                "Check that the bot is deployed and that its OpenClaw config "
                "still points at a workspace that exists."
            ),
        )
    return ScanProvenance(
        bot_id=bot_id, state=STATE_UNREADABLE, reason=reason,
        note=(
            "Evolve could not read this bot's scan record, so it cannot say "
            "whether a scan has run."
        ),
        remedy=(
            "Re-deploy the bot to repair its file permissions, then run "
            "Sync again."
        ),
    )


# ── Reading the file ──────────────────────────────────────────────────────────


def scan_status_path(bot_id: str, shared_dir: Path) -> Path:
    """Where the scanner wrote this bot's status file.

    Resolved through ``manifest.applications_dir`` — the same call
    ``scan_workspace_pipeline`` uses to place it — so a pod with a non-standard
    home cannot have the file written in one place and looked for in another.
    """
    from .manifest import applications_dir

    return applications_dir(shared_dir, bot_id) / SCAN_STATUS_FILENAME


def read_scan_status(path: Path) -> tuple[dict[str, Any] | None, str]:
    """``(status, read_state)`` for one status file.

    ``FileNotFoundError`` is the one signal that genuinely means *missing*: a
    directory component we cannot traverse raises ``PermissionError`` instead
    (which is why ``exists()`` is not used here — it answers False for both,
    and that is exactly the conflation this module exists to avoid).

    A ``sudo /bin/cat`` that exits non-zero is reported as ``READ_DENIED``, not
    as missing: without the grant we cannot tell "no such file" from "not
    allowed", and guessing the friendlier one would resurrect the lie.
    """
    try:
        parsed = json.loads(path.read_text())
    except FileNotFoundError:
        return None, READ_MISSING
    except PermissionError:
        # Expected on a bot not yet deployed through set_evolve_read_acl: fall
        # through to the privileged read rather than reporting "never scanned".
        log.debug("scan_provenance: direct read denied for %s; trying sudo", path)
    except (OSError, ValueError) as exc:
        log.info("scan_provenance: %s unreadable: %s", path, exc)
        return None, READ_DENIED
    else:
        return (parsed, READ_OK) if isinstance(parsed, dict) else (None, READ_DENIED)

    try:
        result = subprocess.run(
            # ``-n``: never prompt. Without it a pod whose grant is missing
            # burns the whole timeout per bot, and this runs once per bot
            # inside a GET. Matches ``config.get_bot_workspace``'s call shape.
            ["sudo", "-n", get_profile().cat, str(path)],
            capture_output=True, text=True, timeout=5,
            cwd=get_profile().scratch_dir,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("scan_provenance: privileged read of %s failed: %s", path, exc)
        return None, READ_DENIED
    if result.returncode != 0:
        return None, READ_DENIED
    try:
        parsed = json.loads(result.stdout)
    except ValueError:
        return None, READ_DENIED
    return (parsed, READ_OK) if isinstance(parsed, dict) else (None, READ_DENIED)


def provenance_for(bot_id: str, shared_dir: Path) -> ScanProvenance:
    """The scan-provenance block for one bot, read from disk.

    A MISSING status file only means "never scanned" if we were looking in the
    right place. ``manifest.applications_dir``'s last-resort fallback keys on
    the bot_id rather than the OS account and hardcodes a macOS home, so a bot
    whose account name differs — or any bot on a Linux pod that reaches that
    fallback — would otherwise be reported as never scanned when it has been.
    So a missing file under a workspace that DOES NOT EXIST is reported as
    "could not tell", not as "nobody has looked".

    ``exists_or_unreachable`` is the right test rather than ``is_dir()``: a
    workspace we cannot traverse is treated as present, so only a positively
    absent one downgrades the answer. Erring the other way would turn every
    EACCES-clamped Linux pod into "could not tell" for every bot.
    """
    try:
        path = scan_status_path(bot_id, shared_dir)
    except Exception as exc:  # noqa: BLE001 — path resolution crosses config/sudo
        log.info("scan_provenance: no status path for %s: %s", bot_id, exc)
        return unreadable(bot_id)
    status, read_state = read_scan_status(path)
    if read_state == READ_MISSING and not _workspace_plausible(path):
        return unreadable(bot_id, reason=REASON_NO_WORKSPACE)
    return classify(bot_id, status, read_state)


def _workspace_plausible(status_path: Path) -> bool:
    """Does ``<workspace>/`` (the status file's grandparent) actually exist?"""
    from ..secret_config_perms import exists_or_unreachable

    try:
        return exists_or_unreachable(status_path.parent.parent)
    except Exception:  # noqa: BLE001 — a probe failure must not assert absence
        return True


# ── The pod-wide rollup ───────────────────────────────────────────────────────


def summarize(provenances: Iterable[ScanProvenance]) -> dict[str, Any]:
    """Roll per-bot provenance up to the sentence a banner can render.

    ``degraded_reasons`` groups by reason so a pod where five bots share one
    missing key produces one line, not five — that grouping is what the banner
    renders.

    THIS IS THE POD-WIDE ROLLUP, AND THAT IS ITS LIMIT. The Discovered tab
    filters by bot, so its empty-state and footer branches derive their own
    per-state counts from the filterable ``scans[]`` list instead — a pod-wide
    summary would answer "has any scan here completed?" about the wrong set the
    moment an operator picks one bot. What the summary is for is the banner
    (``degraded_reasons``, grouped) and any consumer that does not filter.

    ``any_scanned`` is the weak "has anything ever run here, well or badly": it
    is False when every bot is ``unreadable``, so such a consumer can say "we
    could not check" rather than "nobody has looked". ``counts[STATE_OK]`` is
    the stricter question — "did a scan actually complete" — and is the one to
    branch on before asserting anything is settled.
    """
    rows = list(provenances)
    counts = {state: 0 for state in SCAN_STATES}
    grouped: dict[str, dict[str, Any]] = {}
    last_seen: list[str] = []

    for p in rows:
        counts[p.state] = counts.get(p.state, 0) + 1
        if p.last_scan_at:
            last_seen.append(p.last_scan_at)
        if p.state != STATE_DEGRADED:
            continue
        key = p.reason or "unknown"
        slot = grouped.setdefault(
            key, {"reason": key, "note": p.note, "remedy": p.remedy, "bots": []},
        )
        slot["bots"].append(p.bot_id)

    return {
        "total": len(rows),
        "counts": counts,
        "any_scanned": any(p.scanned for p in rows),
        "degraded_bots": sorted(p.bot_id for p in rows if p.degraded),
        "degraded_reasons": [grouped[k] for k in sorted(grouped)],
        "last_scan_at": max(last_seen) if last_seen else None,
    }
