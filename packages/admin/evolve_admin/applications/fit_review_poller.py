"""fit_review_poller.py — Admin-side ingester for Fit Reviewer outboxes.

Spec: internal/spec-fit-reviewer-2026-06-12.md §3.1, §3.5, §3.6, §7 (Bite 4).

Bite 3 (the in-bot reflection runner) writes one capability *candidate* per
(run, bot) to::

    {shared_dir}/fit_review/outbox/<run_id>/<bot_id>.json

This module is the receiving end — the same shape as ``audit_poller`` (runner →
outbox → poller): it walks every pending run dir, reads each candidate, runs the
**deterministic gates** (``fit_review.gates`` — cite-or-don't, gallery-pkg-exists,
support-reconcile, dedup/cooldown), and on PASS mints a single ``InstallApp``
Proposal (``surface="improvement"``, ``altitude=2``) via ``arbiter.store``. On a
benign near-miss it emits a calmer informational Observation instead of letting
the near-miss silently vanish; hallucinations and cooldowns are dropped silently.

The poller reads the bot's **structured candidate**, never free-ranges over raw
content. The one place it touches a transcript is the cite-or-don't *verifier*:
a deterministic substring re-check that a cited quote actually exists at the
claimed session ("verify, don't trust", §3.6 Gate A) — not centralized
inference. It degrades gracefully (trusts the bot's session attestation) only
when the transcript store is entirely unreadable.

Idempotent: re-reading a candidate is safe (the arbiter coalesces by
``coalesce_key`` and the pre-write trigger scan skips an open duplicate); the
file move off the queue is the only state change.

Wired into the hourly ``audit-scheduler`` tick — no new daemon (spec §3.1).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ── Paths ────────────────────────────────────────────────────────────────────


def _outbox_root(shared_dir: Path) -> Path:
    return Path(shared_dir) / "fit_review" / "outbox"


def _ingested_root(shared_dir: Path) -> Path:
    return Path(shared_dir) / "fit_review" / "_ingested"


def _turns_dir(bot_user: str) -> Path:
    """Per-bot turn-transcript dir (``turns-<date>.jsonl`` live here).

    Same location ``turn_detail.py`` reads. The home is resolved via the blessed
    ``evolve_config.user_home`` helper (cross-OS; no hardcoded ``/Users`` literal)
    — ``bot_user`` is the already-resolved OS account. The ``evolve`` user has
    macOS ACL read on the bot's ``.openclaw/`` tree (CLAUDE.md), so a plain read
    works.
    """
    from evolve_config import user_home

    return user_home(bot_user) / ".openclaw" / "workspace" / "memory"


# ── Result types ─────────────────────────────────────────────────────────────


@dataclass
class FitReviewPollResult:
    """Per-tick outcome for the Fit Reviewer drain."""

    files_processed: int = 0
    proposals_raised: int = 0
    observations_raised: int = 0
    dropped: int = 0
    # Per-reason drop tally (reason_code → count) for legibility in logs.
    drops_by_reason: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def note_drop(self, reason_code: str) -> None:
        self.dropped += 1
        self.drops_by_reason[reason_code] = self.drops_by_reason.get(reason_code, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_processed": self.files_processed,
            "proposals_raised": self.proposals_raised,
            "observations_raised": self.observations_raised,
            "dropped": self.dropped,
            "drops_by_reason": dict(self.drops_by_reason),
            "errors": list(self.errors),
        }


# ── Outbox reading / archiving (sudo fallback like audit_poller) ─────────────


class _ReadOutcome(Enum):
    """Sentinel for a PERMANENT candidate-read failure (distinct from ``None``).

    ``_read_json`` returns ``MALFORMED`` when a file's content will *never* parse
    — invalid JSON, a non-object payload, or an oversized blob. The caller
    quarantines it instead of retrying: a permanent failure left in the outbox
    re-errors on every tick and wedges the shared audit-scheduler tick red (the
    #3174 cron_exit pattern). ``None`` stays reserved for a *transient* I/O
    failure (ACL not ready on a fresh deploy, read race) that is worth a retry.
    """

    MALFORMED = "malformed"


_MALFORMED = _ReadOutcome.MALFORMED

# A real reflection candidate is a few KB; anything past this ceiling is not a
# candidate at all (truncated dump, runaway transcript paste) and will never gate
# — treat it as malformed rather than reading it into memory.
_MAX_CANDIDATE_BYTES = 4_000_000  # ~4 MB

# How long a TRANSIENT-unreadable candidate may linger before it is quarantined
# anyway. Bounds the worst case: a permanently-stuck transient (a broken ACL, a
# sudo that always fails) keeps the tick red for at most this long — never
# "indefinitely". The invariant: no single outbox file wedges the shared tick.
_TRANSIENT_RETRY_MAX_AGE = _dt.timedelta(hours=24)


def _coerce_candidate(obj: Any) -> dict | _ReadOutcome:
    """A parsed candidate must be a JSON object; anything else is malformed."""
    return obj if isinstance(obj, dict) else _MALFORMED


def _read_json(path: Path) -> dict | _ReadOutcome | None:
    """Read + parse a candidate file, classifying failures permanent vs transient.

    Returns one of:
      * ``dict``       — the parsed candidate (success)
      * ``_MALFORMED`` — a PERMANENT failure (invalid JSON, non-object payload, or
                         oversized content); it will never parse, so the caller
                         quarantines it rather than re-reading it every tick
      * ``None``       — a TRANSIENT failure (PermissionError before the bot's ACL
                         is ready, another OSError) worth retrying on a later tick

    The pre-fix version collapsed both failure classes to ``None``; a single
    malformed outbox file then re-errored every tick and — because the
    audit-scheduler exits 1 on any error — wedged the shared scheduler tick
    permanently red (#3174).
    """
    try:
        if path.stat().st_size > _MAX_CANDIDATE_BYTES:
            logger.warning(
                "fit_review_poller: candidate %s exceeds %d bytes — malformed",
                path, _MAX_CANDIDATE_BYTES,
            )
            return _MALFORMED
    except OSError as exc:
        # Can't stat (vanished, ACL) — defer to the read below, which classifies
        # it as transient vs gone. Logged, not silently swallowed.
        logger.debug("fit_review_poller: size-stat failed for %s: %s", path, exc)
    try:
        return _coerce_candidate(json.loads(path.read_text()))
    except PermissionError:
        # Bot-owned file not yet readable via evolve's ACL on a fresh deploy —
        # fall through to the sudo /bin/cat fallback below (not a silent swallow).
        logger.debug("fit_review_poller: ACL read denied for %s; trying sudo", path)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Malformed / undecodable bytes will never parse — permanent.
        logger.warning("fit_review_poller: malformed candidate %s", path)
        return _MALFORMED
    except OSError:
        # Transient I/O (vanished mid-scan, read error) — retry next tick.
        return None
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None  # couldn't read even via sudo — transient, retry
        return _coerce_candidate(json.loads(r.stdout))
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("fit_review_poller: malformed candidate %s (via sudo)", path)
        return _MALFORMED
    except subprocess.SubprocessError:
        return None


def _list_candidate_files(shared_dir: Path) -> list[Path]:
    """Every ``outbox/<run_id>/<bot_id>.json`` not yet ingested, mtime-sorted."""
    root = _outbox_root(shared_dir)
    if not root.exists():
        return []
    files: list[Path] = []
    try:
        for run_dir in sorted(root.iterdir()):
            if not run_dir.is_dir() or run_dir.name.startswith((".", "_")):
                continue
            for entry in sorted(run_dir.iterdir()):
                if (
                    entry.is_file()
                    and entry.suffix == ".json"
                    and not entry.name.startswith((".", "_"))
                ):
                    files.append(entry)
    except OSError:
        return files
    try:
        files.sort(key=lambda p: p.stat().st_mtime)
    except OSError as exc:
        # A stat race (file archived mid-scan) just leaves files in directory
        # order — harmless for processing; don't mask it silently.
        logger.debug("fit_review_poller: mtime sort skipped: %s", exc)
    return files


def _archive_file(path: Path, shared_dir: Path, now: _dt.datetime) -> bool:
    """Move a processed candidate into ``_ingested/<YYYY-MM-DD>/<run_id>/``.

    Best-effort: a failed move just means the candidate is re-processed next
    tick (safe — the gates + arbiter coalesce are idempotent).
    """
    day = now.strftime("%Y-%m-%d")
    run_id = path.parent.name
    dest_dir = _ingested_root(shared_dir) / day / run_id
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest_dir / path.name))
        return True
    except OSError as exc:
        logger.warning("fit_review_poller: archive failed for %s: %s", path, exc)
        return False


def _file_age(path: Path, now: _dt.datetime) -> _dt.timedelta | None:
    """Wall-clock age since the candidate's last mtime (``None`` if unstat'able)."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return now - _dt.datetime.fromtimestamp(mtime, _dt.timezone.utc)


# ── The transcript quote verifier (cite-or-don't, Gate A) ────────────────────


def _read_session_text(bot_user: str, session_id: str) -> tuple[bool, str]:
    """Return ``(store_present, concatenated_text)`` for a session's turns.

    ``store_present`` is False only when the bot's turn store is entirely
    absent/unreadable — the signal to degrade to bot attestation rather than
    drop everything. When True, ``text`` is the (possibly empty) concatenation
    of every turn record whose ``session_id`` matches; empty text for a present
    store means the session_id appears in no turn → a fabricated/unknown session.
    """
    turns_dir = _turns_dir(bot_user)
    try:
        files = sorted(turns_dir.glob("turns-*.jsonl"))
    except OSError:
        files = []
    if not files:
        return False, ""

    store_present = False
    chunks: list[str] = []
    for f in files:
        try:
            raw = f.read_text()
        except (OSError, PermissionError):
            continue
        store_present = True
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("session_id") != session_id:
                continue
            for key in (
                "text", "content", "message", "prompt", "response",
                "user_text", "assistant_text", "summary",
            ):
                v = rec.get(key)
                if isinstance(v, str) and v:
                    chunks.append(v)
    return store_present, "\n".join(chunks)


def _normalize(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def make_transcript_quote_verifier(bot_user: str) -> Callable[[str, str, str], bool]:
    """Build the real (bot_id, session_id, quote) → bool verifier for one bot.

    Tri-state collapsed to bool:
      * store absent/unreadable  → True  (trust the bot's session attestation —
        spec §3.6 floor; the in-bot runner already enforced cite-or-don't)
      * session not in the store → False (fabricated/unknown session → drop)
      * quote present in session → True
      * quote absent from session→ False (fabricated quote → drop)

    Caches parsed session text per session_id within one verifier lifetime.
    """
    cache: dict[str, tuple[bool, str]] = {}

    def verify(_bot_id: str, session_id: str, quote: str) -> bool:
        if not session_id or not quote.strip():
            return False
        if session_id not in cache:
            cache[session_id] = _read_session_text(bot_user, session_id)
        store_present, text = cache[session_id]
        if not store_present:
            return True  # graceful degradation — trust attestation
        if not text:
            return False  # session_id unknown to the store
        return _normalize(quote) in _normalize(text)

    return verify


# ── Per-candidate ingest ─────────────────────────────────────────────────────


# Default report builder — injectable so tests can supply a crafted
# TargetingReport without standing up an observation store.
def _default_build_report(bot_id: str, *, network: dict, shared_dir: Path, now):
    from fit_review.targeting import build_targeting_report_for_bot

    return build_targeting_report_for_bot(
        bot_id, network=network, shared_dir=shared_dir, now=now
    )


def _installed_pkg_ids(shared_dir: Path, bot_id: str) -> set[str]:
    try:
        from fit_review.targeting import _installed_coverage

        _covered, installed = _installed_coverage(shared_dir, bot_id)
        return installed
    except Exception:
        return set()


def _operator_declined_check(shared_dir: Path, bot_id: str):
    def declined(cause_key: str) -> bool:
        try:
            from investigation.proposal_history import operator_already_declined

            return operator_already_declined(
                shared_dir, bot_id=bot_id, cause_key=cause_key
            )
        except Exception:
            return False

    return declined


def _trigger_already_open(shared_dir: Path, trigger: str) -> bool:
    """True if an open (pending/snoozed) proposal already carries ``trigger``."""
    try:
        from arbiter.store import iter_proposals

        for existing in iter_proposals(shared_dir, subdirs=("pending", "snoozed")):
            if trigger in (existing.trigger_observations or []):
                return True
    except Exception:
        return False
    return False


def _emit_install_app(proposal, shared_dir: Path) -> bool:
    """Promote draft → pending and write via arbiter.store. Idempotent.

    Returns False ONLY for the benign "an open card for this need already
    exists" case. A real import / transition / write failure is allowed to
    propagate so the tick's per-candidate handler records it loudly in
    ``result.errors`` — never silently counted as a duplicate.
    """
    from arbiter import store as arbiter_store
    from arbiter.state_machine import transition

    if _trigger_already_open(shared_dir, proposal.trigger_observations[0]):
        return False  # an open card for this need already exists
    transition(proposal, "pending", actor="fit_reviewer", reason="fit_review_poller ingest")
    arbiter_store.write_proposal(proposal, shared_dir)
    return True


def _emit_near_miss_observation(candidate, gate, shared_dir: Path, now) -> bool:
    """Emit a calmer informational Observation for a benign near-miss.

    An Investigation-kind proposal with ``surface="improvement"`` +
    ``altitude=0`` routes to the Recommendations **Observations** sub-block (not
    the actionable Pending inbox, not Alerts) — see
    ``proposal_routing.apply_legacy_investigation_routing``. So a "we looked and
    the need has thinned / is now covered" note stays visible without nagging.
    """
    try:
        from arbiter import store as arbiter_store
        from arbiter.state_machine import transition
        from schema.proposal import (
            Investigation,
            Proposal,
            RiskTag,
            new_proposal_id,
        )
        from schema.provenance import Provenance
        from fit_review.gates import _utc_iso
    except Exception as exc:
        logger.warning("fit_review_poller: near-miss import failed: %s", exc)
        return False

    bot_id = candidate.bot_id
    # identity: see resolve_app_id — GALLERY CATALOG KEY carried on the
    # fit-review candidate/gate (the reviewer's job is recommending gallery
    # packages), and half of the Proposal dedup trigger built on the next line.
    # Neither the gate nor the candidate is a manifest, so there is nothing for
    # the resolver to read.
    pkg_id = gate.pkg_id or "?"
    domain = gate.primary_domain or candidate.recommended_need or "capability"
    trigger = f"fit_review_nearmiss:{bot_id}:{pkg_id}"
    if _trigger_already_open(shared_dir, trigger):
        return False
    context = (
        f"## Fit Reviewer near-miss — {gate.app_name or pkg_id}\n\n"
        f"The Fit Reviewer looked at {bot_id}'s {domain} usage and considered "
        f"suggesting **{gate.app_name or pkg_id}**, but did not surface it:\n\n"
        f"> {gate.reason}\n\n"
        f"_No action needed — recorded so the near-miss isn't invisible._"
    )
    try:
        proposal = Proposal(
            id=new_proposal_id(),
            bot_id=bot_id,
            generator_id="fit_reviewer",
            dimension="capabilities",
            trigger_observations=[trigger],
            provenance=Provenance(
                technique="fit_reviewer.near_miss.v1",
                signals={
                    "pkg_id": pkg_id,
                    "reason_code": gate.reason_code,
                    "primary_domain": domain,
                },
                confidence=0.5,
            ),
            problem=f"{bot_id}: considered {gate.app_name or pkg_id}, did not suggest",
            action=Investigation(context=context),
            risk_tag=RiskTag(
                blast_radius="bot", reversibility="manual", touches=["app_install"]
            ),
            claim=None,
            approval_audience="pod_operator",
            urgency="improvement",
            admin_surface_summary=f"{bot_id}: {gate.app_name or pkg_id} near-miss"[:120],
            dismiss_signature=trigger,
            # informational: surface=improvement → Observations sub-block, not Pending.
            surface="improvement",
            altitude=0,
            created_at=_utc_iso(now),
        )
        transition(proposal, "pending", actor="fit_reviewer", reason="fit_review near-miss")
        arbiter_store.write_proposal(proposal, shared_dir)
        return True
    except Exception as exc:
        logger.warning("fit_review_poller: near-miss write failed: %s", exc)
        return False


def process_candidate(
    raw: dict,
    *,
    run_id: str,
    bot_id: str,
    bot_user: str,
    shared_dir: Path,
    network: dict,
    now: _dt.datetime,
    result: FitReviewPollResult,
    build_report: Callable | None = None,
    quote_verifier: Callable[[str, str, str], bool] | None = None,
) -> None:
    """Gate one candidate and emit/observe/drop. Mutates ``result``.

    ``build_report`` and ``quote_verifier`` are injectable for tests; the
    defaults wire the real targeting recompute and transcript re-read.
    """
    from fit_review.candidate import parse_candidate
    from fit_review.gates import build_install_app_proposal, evaluate_candidate

    candidate = parse_candidate(raw, run_id=run_id, bot_id=bot_id)

    # Clean no_candidate records are the common, correct case (the negative
    # control) — drop silently, no observation.
    if not candidate.is_suggestion:
        result.note_drop("not_a_suggestion")
        return

    build_report = build_report or _default_build_report
    verifier = quote_verifier or make_transcript_quote_verifier(bot_user)

    from fit_review.targeting import _load_gallery_catalog

    catalog = _load_gallery_catalog()
    report = build_report(bot_id, network=network, shared_dir=shared_dir, now=now)
    installed = _installed_pkg_ids(shared_dir, bot_id)

    gate = evaluate_candidate(
        candidate,
        catalog=catalog,
        targeting_report=report,
        installed_pkg_ids=installed,
        quote_verifier=verifier,
        operator_declined=_operator_declined_check(shared_dir, bot_id),
    )

    if gate.passed:
        proposal = build_install_app_proposal(candidate, gate, now=now)
        if _emit_install_app(proposal, shared_dir):
            result.proposals_raised += 1
        else:
            # Open duplicate already exists — not an error, not a new card.
            result.note_drop("duplicate_open")
        return

    # FAIL — drop, surfacing a calmer Observation only for benign near-misses.
    result.note_drop(gate.reason_code)
    if gate.observation_worthy:
        if _emit_near_miss_observation(candidate, gate, shared_dir, now):
            result.observations_raised += 1
    logger.info(
        "fit_review_poller: dropped %s/%s reason=%s",
        # identity: see resolve_app_id — gallery catalog key, as above.
        bot_id, candidate.pkg_id, gate.reason_code,
    )


# ── Tick ─────────────────────────────────────────────────────────────────────


def _resolve_bot_users(network: dict | None) -> dict[str, str]:
    bots = (network or {}).get("bots") or {}
    out: dict[str, str] = {}
    for bot_id, cfg in bots.items():
        if isinstance(cfg, dict):
            out[bot_id] = cfg.get("user") or bot_id
    return out


def tick(
    shared_dir: Path,
    network: dict | None = None,
    *,
    now: _dt.datetime | None = None,
    build_report: Callable | None = None,
    quote_verifier_factory: Callable[[str], Callable] | None = None,
) -> FitReviewPollResult:
    """One Fit Reviewer drain pass. Idempotent + best-effort.

    Iterates every ``outbox/<run_id>/<bot_id>.json``, gates it, emits/observes/
    drops, and archives the file. A crash on one candidate is captured into
    ``errors`` and never aborts the rest of the tick.
    """
    if network is None:
        try:
            from ..config import load_network

            network = load_network()
        except Exception:
            network = {}
    now = now or _dt.datetime.now(_dt.timezone.utc)
    bot_users = _resolve_bot_users(network)
    result = FitReviewPollResult()

    for path in _list_candidate_files(shared_dir):
        run_id = path.parent.name
        bot_id = path.stem
        bot_user = bot_users.get(bot_id, bot_id)
        raw = _read_json(path)
        result.files_processed += 1
        if raw is _MALFORMED:
            # PERMANENT: invalid JSON / non-object / oversized — it will never
            # parse, so quarantine it. Counted as a drop, NOT an error, so it
            # can't re-error every tick and wedge the shared audit-scheduler tick
            # red (#3174). A gate-crash already archives below; a malformed read
            # must too — that asymmetry was the bug.
            result.note_drop("malformed_quarantined")
            logger.warning(
                "fit_review_poller: quarantining malformed candidate %s", path.name
            )
            _archive_file(path, shared_dir, now)
            continue
        if raw is None:
            # TRANSIENT I/O (PermissionError before the ACL is ready, OSError):
            # leave for retry — UNLESS it has been stuck past the age cap, in
            # which case quarantine so even a permanently-stuck transient can't
            # keep the tick red indefinitely.
            age = _file_age(path, now)
            if age is not None and age > _TRANSIENT_RETRY_MAX_AGE:
                result.note_drop("unreadable_stuck_quarantined")
                logger.warning(
                    "fit_review_poller: quarantining candidate %s — unreadable for %s",
                    path.name, age,
                )
                _archive_file(path, shared_dir, now)
            else:
                result.errors.append(f"unreadable candidate {path.name}")
            continue
        try:
            verifier = (
                quote_verifier_factory(bot_user)
                if quote_verifier_factory is not None
                else None
            )
            process_candidate(
                raw,
                run_id=run_id,
                bot_id=bot_id,
                bot_user=bot_user,
                shared_dir=shared_dir,
                network=network,
                now=now,
                result=result,
                build_report=build_report,
                quote_verifier=verifier,
            )
        except Exception as exc:
            logger.exception(
                "fit_review_poller: process crashed for %s/%s: %s",
                run_id, bot_id, exc,
            )
            result.errors.append(f"{run_id}/{bot_id}: {exc}")
        # Always archive — a candidate that crashed the gates is still drained so
        # it doesn't re-crash every tick; the crash is captured in errors.
        _archive_file(path, shared_dir, now)

    return result
