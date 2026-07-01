"""provision_driver.py — async provision job behind the `evo add-bot` chain (M2).

Spec: docs/spec-add-bot-wizard-build-delta-2026-06-10.md §3.2
(AB_CREDENTIALS / AB_PROVISION / AB_SMOKE_WRAP) + §9 M2.

The add-bot chain runs inside the admin server process (the evo gateway
only ever talks HTTP to /api/evo/* — the account-separation posture M1
established). This module is the chain's handle on the *same* provision
substrate the shipped ``POST /api/wizard/provision`` endpoint drives:
``provisioning.provision_bot`` in a daemon thread, stage events relayed
through an in-memory job record the engine polls turn-by-turn.

Why an in-module registry instead of the web layer's ``_jobs`` dict:
the engine lives under ``evo/`` and importing ``web.server`` from here
would invert the layering (web → evo → web). The conversation is the
progress surface for this flow; the record holds everything the
verbatim renders need.

Credential safety: a pasted API key lives ONLY in this in-memory record
(``ProvisionJob._provider_api_key``) — never in the wizard state file on
disk. If the admin server restarts mid-run the record is gone; the
engine's AB_PROVISION handler treats that honestly (lost-track message,
re-collects the credential on retry).

Seams (tests substitute these instead of patching subprocess — see the
subprocess-patch-fakes lesson):

  * :func:`set_provision_runner` — replaces the blocking pipeline call.
  * :func:`set_smoke_runner` — replaces the post-provision smoke check.
  * :func:`set_quarantine_runner` — replaces the gateway switch-off.
  * :func:`set_borrow_candidates_loader` — replaces the "which bots have
    a key to borrow" lookup.

Each has a production default that lazily imports the real machinery.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ── Claude-assumed credential (locked design decision) ───────────────────────
#
# The add-bot wizard asks exactly one auth question — the Anthropic
# credential — per the design sync 2026-06-10 (delta spec header:
# "Claude-assumed", multi-runtime deferral spec §5). These constants are
# the chain's single expression of that decision; when multi-runtime
# bots land, the provider becomes a conversation answer and these go away.
ASSUMED_AUTH_CHOICE = "anthropic"
ASSUMED_PROFILE_PROVIDER = "anthropic"
# Anthropic API keys are "sk-ant-…". The paste validation gates on this
# prefix so a stray OpenAI key (or random prose) never reaches onboard.
KEY_PREFIX = "sk-ant-"


# ── Job record ────────────────────────────────────────────────────────────────


@dataclass
class ProvisionJob:
    """One provision run for one wizard session. Mutated by the runner
    thread under ``_lock``; the engine reads via :meth:`snapshot`."""

    bot_id: str            # the bot hosting the conversation (evo)
    user_key: str          # the admin's wizard session key
    account: str           # the NEW bot's id == macOS account
    display_name: str
    credential_source: str  # "paste" | "borrow:<bot>" — for honest copy
    status: str = "running"  # running | succeeded | failed
    # (stage, status, detail) in arrival order — same protocol as the
    # provision endpoint's job log.
    events: list[tuple[str, str, str]] = field(default_factory=list)
    error: Optional[str] = None
    failed_stage: Optional[str] = None
    rollback_log: list[str] = field(default_factory=list)
    # Smoke-check outcome (set after a successful provision):
    # {"status": "ok"|"fail", "summary": str}
    smoke: Optional[dict] = None
    # In-memory only — never serialized anywhere. Kept on the record so
    # a retry after a failed run can reuse the pasted key without
    # re-asking.
    _provider_api_key: Optional[str] = None
    _borrow_from: Optional[str] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        """Thread-safe copy of everything the renders need. The
        credential fields are deliberately absent."""
        with self._lock:
            return {
                "account": self.account,
                "display_name": self.display_name,
                "credential_source": self.credential_source,
                "status": self.status,
                "events": list(self.events),
                "error": self.error,
                "failed_stage": self.failed_stage,
                "rollback_log": list(self.rollback_log),
                "smoke": dict(self.smoke) if self.smoke else None,
            }


_JOBS: dict[tuple[str, str], ProvisionJob] = {}
_JOBS_LOCK = threading.Lock()


def get_job(bot_id: str, user_key: str) -> ProvisionJob | None:
    with _JOBS_LOCK:
        return _JOBS.get((bot_id, user_key))


def clear_job(bot_id: str, user_key: str) -> None:
    with _JOBS_LOCK:
        _JOBS.pop((bot_id, user_key), None)


# ── Seams ─────────────────────────────────────────────────────────────────────


# Runner: blocking call that executes the pipeline and mutates the job
# record (events / status / error / rollback_log / smoke). Production
# default below; tests substitute a synchronous fake.
ProvisionRunner = Callable[[ProvisionJob], None]
_provision_runner: ProvisionRunner | None = None


def set_provision_runner(fn: ProvisionRunner | None) -> None:
    global _provision_runner
    _provision_runner = fn


# Smoke: (account) -> {"status": "ok"|"fail", "summary": str}.
SmokeRunner = Callable[[str], dict]
_smoke_runner: SmokeRunner | None = None


def set_smoke_runner(fn: SmokeRunner | None) -> None:
    global _smoke_runner
    _smoke_runner = fn


# Quarantine: (account) -> (ok, message). Switches the new bot's gateway
# off so a bot that failed its smoke check never sits half-working.
QuarantineRunner = Callable[[str], tuple[bool, str]]
_quarantine_runner: QuarantineRunner | None = None


def set_quarantine_runner(fn: QuarantineRunner | None) -> None:
    global _quarantine_runner
    _quarantine_runner = fn


# Borrow candidates: (provider, network) -> [bot_id, ...].
BorrowCandidatesLoader = Callable[[str, dict], list[str]]
_borrow_candidates_loader: BorrowCandidatesLoader | None = None


def set_borrow_candidates_loader(fn: BorrowCandidatesLoader | None) -> None:
    global _borrow_candidates_loader
    _borrow_candidates_loader = fn


# Tests flip this so start()/retry() run their (fake) runner inline —
# no thread, no race between kickoff and the poll-turn assertions.
_force_sync = False


def set_force_sync(value: bool) -> None:
    global _force_sync
    _force_sync = value


# ── Public operations ─────────────────────────────────────────────────────────


def borrow_candidate_bots(network: dict) -> list[str]:
    """Bot ids that hold a credential the new bot could borrow.

    Default path reuses the shipped borrow-candidates lookup
    (``GET /api/wizard/borrow-candidates`` semantics, 05-28 spec §6).
    Best-effort: any failure returns an empty list and the wizard simply
    offers paste/skip.
    """
    loader = _borrow_candidates_loader or _default_borrow_candidates
    try:
        return list(loader(ASSUMED_PROFILE_PROVIDER, network))
    except Exception:
        return []


def _default_borrow_candidates(provider: str, network: dict) -> list[str]:
    from ...web.wizard_routes import borrow_candidates
    return [
        str(entry.get("bot_id"))
        for entry in borrow_candidates(provider, network)
        if entry.get("bot_id")
    ]


def start(
    *,
    bot_id: str,
    user_key: str,
    account: str,
    display_name: str,
    provider_api_key: str | None = None,
    borrow_from: str | None = None,
    run_async: bool = True,
) -> ProvisionJob:
    """Create the job record and kick the pipeline off in a daemon
    thread (or inline with ``run_async=False`` — tests, and the retry
    path's fake runners).

    Exactly one of ``provider_api_key`` / ``borrow_from`` is set; with
    neither, onboard runs keyless and fails loud, which the chain never
    requests (skip = don't provision)."""
    source = f"borrow:{borrow_from}" if borrow_from else "paste"
    job = ProvisionJob(
        bot_id=bot_id,
        user_key=user_key,
        account=account,
        display_name=display_name,
        credential_source=source,
    )
    job._provider_api_key = provider_api_key
    job._borrow_from = borrow_from
    with _JOBS_LOCK:
        _JOBS[(bot_id, user_key)] = job

    runner = _provision_runner or _default_provision_runner

    def _target() -> None:
        try:
            runner(job)
        except Exception as exc:  # the runner is supposed to catch; last resort
            with job._lock:
                job.status = "failed"
                job.error = f"unexpected: {exc}"

    if run_async and not _force_sync:
        t = threading.Thread(
            target=_target, daemon=True,
            name=f"add-bot-provision-{account}",
        )
        t.start()
    else:
        _target()
    return job


def retry(job: ProvisionJob, *, run_async: bool = True) -> ProvisionJob:
    """Re-run a failed job with the same inputs (the prior run rolled
    itself back, so a fresh run starts from a clean slate). Returns the
    new record, registered under the same session key."""
    return start(
        bot_id=job.bot_id,
        user_key=job.user_key,
        account=job.account,
        display_name=job.display_name,
        provider_api_key=job._provider_api_key,
        borrow_from=job._borrow_from,
        run_async=run_async,
    )


def run_smoke(account: str) -> dict:
    """Run the smoke check against a freshly provisioned bot.

    Returns ``{"status": "ok"|"fail", "summary": str}``. Never raises."""
    runner = _smoke_runner or _default_smoke_runner
    try:
        out = runner(account)
    except Exception as exc:
        return {"status": "fail", "summary": f"smoke check crashed: {exc}"}
    if not isinstance(out, dict) or out.get("status") not in ("ok", "fail"):
        return {"status": "fail", "summary": "smoke check returned no result"}
    return out


def quarantine(account: str) -> tuple[bool, str]:
    """Switch the new bot's gateway off (smoke failed; never leave a
    broken bot running). Returns ``(ok, message)``. Never raises."""
    runner = _quarantine_runner or _default_quarantine_runner
    try:
        return runner(account)
    except Exception as exc:
        return False, f"quarantine crashed: {exc}"


# ── Production defaults ───────────────────────────────────────────────────────


def _default_provision_runner(job: ProvisionJob) -> None:
    """Blocking pipeline: resolve a borrowed key if needed, run
    ``provision_bot`` with stage relay, then the smoke check on success.
    The same substrate ``POST /api/wizard/provision`` drives — kwargs
    mirror that endpoint's translation, with the chain's Claude-assumed
    auth choice."""
    from ...config import DEFAULT_NETWORK_CONFIG, load_network
    from ...provisioning import provision_bot

    key = job._provider_api_key
    if job._borrow_from and not key:
        # Same semantics as the provision endpoint's borrow path: a pure
        # read of the source bot's profile, resolved at run time. Unlike
        # the endpoint (which silently proceeds keyless and lets onboard
        # fail), the chain fails HERE with a reason the wizard can relay
        # honestly — nothing has been created yet at this point.
        try:
            from ...web.wizard_routes import read_provider_key_from_bot
            key = read_provider_key_from_bot(
                bot_id=job._borrow_from,
                provider=ASSUMED_PROFILE_PROVIDER,
                network=load_network(),
            )
        except Exception:
            key = None
        if not key:
            with job._lock:
                job.status = "failed"
                job.failed_stage = "borrow_credential"
                job.error = (
                    f"could not read a usable key from {job._borrow_from}"
                )
            return

    def on_stage(stage: str, status: str, detail: str = "") -> None:
        with job._lock:
            job.events.append((stage, status, detail))

    result = provision_bot(
        job.account,
        role="member",
        display_name=job.display_name,
        provider_api_key=key,
        auth_choice=ASSUMED_AUTH_CHOICE,
        network_path=DEFAULT_NETWORK_CONFIG,
        on_stage=on_stage,
    )

    if not result.success:
        with job._lock:
            job.status = "failed"
            job.error = result.error
            job.failed_stage = result.failed_stage
            job.rollback_log = list(result.rollback_log)
        return

    smoke = run_smoke(job.account)
    with job._lock:
        job.smoke = smoke
        job.status = "succeeded"


def _default_smoke_runner(account: str) -> dict:
    """Structural agent dry-run from the shipped verification gauntlet:
    gateway answering, config valid, model set, credential present —
    the same check the admin UI's Verify modal runs."""
    from ...config import load_network
    from ...wizard_verify import check_agent_dry_run

    network = load_network()
    check = check_agent_dry_run(account, network=network)
    return {
        "status": "ok" if check.status == "ok" else "fail",
        "summary": check.summary or "",
    }


def _default_quarantine_runner(account: str) -> tuple[bool, str]:
    """Boot the new bot's gateway out through the Scheduler seam so no
    real launchctl ever runs in tests that inject a fake scheduler."""
    from ...deploy import _scheduler_launchctl, per_bot_gateway_plist_label

    label = per_bot_gateway_plist_label(account)
    rc, out = _scheduler_launchctl("bootout", f"system/{label}")
    if rc == 0:
        return True, f"gateway {label} stopped"
    return False, out or f"bootout {label} exited {rc}"
