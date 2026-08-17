"""
audit_dispatch.py — Admin-side helper for on-demand audit requests.

Used by:
  - CLI ``evolve-admin application audit ...``
  - API ``POST /api/applications/<bot>/<app>/audit``
  - evo wizard ``evo audit ...`` handler

Two responsibilities:
  1. Write an inbox request file into the bot's workspace
     (``/Users/<bot>/.openclaw/workspace/evolve/audit_inbox/<request_id>.json``)
     so the runner picks it up on its next tick or when kicked.
  2. Kick the bot's runner via ``sudo -H -u <bot> python3 audit_runner.py
     --pickup-inbox --request-id <id>`` so the audit runs immediately
     rather than waiting up to an hour for the cron tick. The dispatch
     does NOT wait for completion — callers poll the outbox for results.

The kick is idempotent: the runner's lockfile means a second invocation
within the same window exits cleanly without re-running.

See docs/spec-app-audit-2026-05-16.md §7.3.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from platform_profile import get_profile


logger = logging.getLogger(__name__)


# Resolution priority for the audit_runner.py path. The deploy-checkout
# location wins; fall back to /usr/local for unusual installs.
_RUNNER_PATH_CANDIDATES = (
    f"{get_profile().deploy_checkout_default}/packages/analyzer/app_audit_runner.py",
    "/usr/local/share/evolve/app_audit_runner.py",
)

# How long _kick_runner waits before declaring the spawn "stable enough to
# detach." Sudo permission rejections are immediate (~milliseconds); a
# successful audit either completes in <2s (no-op cadence pass) or stays
# alive doing real work. 2s catches the fast-fail cases without forcing
# real audits to block the HTTP request.
_KICK_FAST_FAIL_WAIT_SECONDS = 2.0


@dataclass
class DispatchResult:
    """What audit_dispatch returned to the caller."""
    ok: bool
    request_id: str
    bot_id: str
    bot_user: str
    apps: list[str] | None      # None means "all eligible"
    full_audit: bool
    inbox_path: str = ""
    kicked: bool = False
    error: str = ""


# ── Paths ────────────────────────────────────────────────────────────────────


def _acct_home(bot_user: str) -> Path:
    """The bot account's real home, pwd-first (W10-F #12, round-4).

    /Users/<u> on macOS, /home/<u> on Linux — never a hardcoded /Users
    literal. The inbox mkdir below left a root-owned
    /Users/<bot>/.openclaw/workspace/evolve/ tree on a fresh Linux pod
    (the gateway's real home is /home/<bot>). byte-identical on macOS."""
    from evolve_config import user_home
    return user_home(bot_user)


def _audit_inbox_dir(bot_user: str) -> Path:
    return _acct_home(bot_user) / ".openclaw/workspace/evolve/audit_inbox"


def _resolve_runner_path() -> str | None:
    for p in _RUNNER_PATH_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


# ── Public entry point ──────────────────────────────────────────────────────


def request_audit(
    bot_id: str,
    bot_user: str,
    *,
    apps: list[str] | None = None,
    full_audit: bool = False,
    requested_by: str = "operator",
    kick: bool = True,
) -> DispatchResult:
    """Queue an audit request on *bot_id* and (optionally) kick the runner.

    ``apps`` — list of app_ids to audit, or None for "all eligible apps on
    the bot." Note: the runner runs whatever's in the request regardless
    of cadence (manual requests bypass cadence gates).

    ``full_audit`` — when True, the runner sends an empty audit_accepted
    list to Stage 3a so previously-accepted findings get re-evaluated.

    ``requested_by`` — free-form attribution string ("cli", "ui:operator",
    "evo:ext:telegram:12345"). Captured in the inbox file for forensics.

    ``kick`` — when True (default), invoke the runner immediately via
    sudo so the audit runs without waiting for the next cron tick. When
    False, the request sits in the inbox until the runner's hourly Tier-3
    tick picks it up. Tests pass kick=False.

    Returns a DispatchResult. Always sets ``request_id``; sets ``ok=False``
    plus ``error`` when the inbox write fails. Kick failures do NOT clear
    ``ok`` — the inbox request still queued.
    """
    request_id = f"audit-req-{uuid.uuid4().hex[:10]}"
    inbox_dir = _audit_inbox_dir(bot_user)

    request = {
        "request_id": request_id,
        "kind": "tier3_audit",
        "apps": apps if apps is not None else "all",
        "full_audit": bool(full_audit),
        "requested_by": requested_by,
        "requested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    inbox_path = inbox_dir / f"{request_id}.json"
    write_ok, write_err = _write_inbox_file(inbox_path, bot_user, request)
    result = DispatchResult(
        ok=write_ok,
        request_id=request_id,
        bot_id=bot_id,
        bot_user=bot_user,
        apps=apps,
        full_audit=full_audit,
        inbox_path=str(inbox_path),
        error=write_err,
    )
    if not write_ok:
        return result

    if kick:
        kicked, kick_err = _kick_runner(bot_user, request_id)
        result.kicked = kicked
        if kick_err:
            # The runner's tier-3 cron daemon doesn't pass --pickup-inbox,
            # so a kick failure means the inbox file will sit unprocessed
            # indefinitely — NOT "runner picks up at next tick". We keep
            # ok=True because the inbox write itself succeeded, but we
            # propagate the kick error so the UI can warn the operator.
            logger.warning(
                "audit_dispatch: kick failed for %s/%s: %s",
                bot_user, request_id, kick_err,
            )
            result.error = f"kick failed: {kick_err}"

    return result


# ── Inbox write (with sudo fallback) ────────────────────────────────────────


def _write_inbox_file(
    inbox_path: Path, bot_user: str, request: dict,
) -> tuple[bool, str]:
    """Write inbox JSON, falling back to /tmp + sudo cp on PermissionError.

    The inbox dir is created by audit_runner at first run + by deploy_bot.
    Both should exist by the time the admin server is dispatching. When
    they don't, we mkdir as part of the sudo cp path.
    """
    content = json.dumps(request, indent=2)
    try:
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        inbox_path.write_text(content)
        return True, ""
    except PermissionError:
        pass
    except OSError as exc:
        return False, f"inbox direct write OSError: {exc}"

    # Fallback: /tmp staging + sudo cp.
    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix="evolve-audit-req-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        try:
            subprocess.run(
                ["sudo", "/bin/mkdir", "-p", str(inbox_path.parent)],
                capture_output=True, timeout=5,
            )
        except subprocess.SubprocessError:
            pass
        r = subprocess.run(
            ["sudo", "/bin/cp", tmp, str(inbox_path)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"sudo cp inbox failed: {r.stderr.strip()[:200]}"
        # Make the file readable by the bot.
        subprocess.run(
            ["sudo", "/bin/chmod", "644", str(inbox_path)],
            capture_output=True, timeout=5,
        )
        return True, ""
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ── Kick (sudo -H -u <bot> python3 runner --pickup-inbox) ──────────────────


def _current_process_user() -> str:
    """Return the username the admin process is running as.

    Wrapped so tests can monkeypatch it without touching ``getpass``
    globally. Used by ``_kick_runner`` to decide whether sudo is needed.
    """
    return getpass.getuser()


def _kick_runner(bot_user: str, request_id: str) -> tuple[bool, str]:
    """Invoke the runner so it picks up the inbox file now.

    Two-mode dispatch:

    * ``bot_user == current user`` (the evo bot on the admin's ``evolve``
      account): run the runner directly, no sudo. Sudo-to-self is
      pointless overhead and — in this pod's sudoers — not granted, so
      it produces a "user X is not allowed to execute ... as X" error.
      This case is the historical silent-failure mode for the evo bot.
    * ``bot_user != current user`` (every member bot): wrap in
      ``sudo -n -H -u <bot_user>``. The ``-n`` ensures we never block on
      a password prompt — if the sudoers grant is wrong, we fail fast
      rather than hanging the HTTP request.

    The spawn captures stderr to a tempfile (rather than ``DEVNULL``)
    and waits up to ``_KICK_FAST_FAIL_WAIT_SECONDS`` for the process to
    either exit or stabilize. Fast exits with non-zero return the
    stderr message so callers can surface it; sustained-running
    processes detach normally and report success. This replaces the
    historical fire-and-forget pattern that swallowed sudo and
    argparse errors into ``/dev/null``.
    """
    runner_path = _resolve_runner_path()
    if not runner_path:
        return False, "audit_runner.py not found at expected paths"

    current_user = _current_process_user()
    if bot_user == current_user:
        cmd = [
            "/opt/homebrew/bin/python3", runner_path,
            "--bot-id", bot_user,
            "--pickup-inbox",
            "--request-id", request_id,
        ]
    else:
        cmd = [
            "/usr/bin/sudo", "-n", "-H", "-u", bot_user,
            "/opt/homebrew/bin/python3", runner_path,
            "--bot-id", bot_user,
            "--pickup-inbox",
            "--request-id", request_id,
        ]

    # Capture stderr to a tempfile so we can inspect fast-fail errors
    # (sudo permission denied, runner argparse error) without using a
    # PIPE — PIPE buffers can fill and block a long-running detached
    # child, causing the runner to die mid-audit.
    stderr_fd, stderr_path = tempfile.mkstemp(
        prefix="evolve-kick-", suffix=".err",
    )
    try:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=stderr_fd,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                cwd="/tmp",
            )
        except OSError as exc:
            return False, f"Popen failed: {exc}"
        finally:
            # The child has its own dup of the fd; close ours.
            os.close(stderr_fd)
            stderr_fd = -1

        try:
            rc = proc.wait(timeout=_KICK_FAST_FAIL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            # Still running after the fast-fail window. Sudo would have
            # rejected immediately, so we know permission is OK. Let
            # the runner continue detached — return success.
            return True, ""

        # Exited within the window — read whatever stderr it produced.
        try:
            with open(stderr_path, "r") as f:
                stderr = f.read().strip()
        except OSError:
            stderr = ""

        if rc == 0:
            # Tiny audit completed in <2s (e.g. inbox already empty
            # or nothing due). Success.
            return True, ""

        msg = stderr[:300] if stderr else f"runner exited {rc}"
        return False, msg
    finally:
        if stderr_fd != -1:
            try:
                os.close(stderr_fd)
            except OSError:
                pass
        try:
            os.unlink(stderr_path)
        except OSError:
            # macOS: unlink works even if the child still holds the fd
            # (inode persists until last close). Only fails if path is
            # already gone.
            pass


# ── Mark-as-accepted helper (used by /audit/accept API + evo handler) ──────


def mark_finding_accepted(
    bot_id: str,
    bot_user: str,
    app_id: str,
    signature: str,
    *,
    accepted_by: str,
    rationale: str = "",
    shared_dir: Path | None = None,
) -> tuple[bool, str]:
    """Append a signature to ``manifest.audit_accepted[]`` on the bot.

    Idempotent: if the signature is already accepted, this is a no-op.
    Writes through the manifest module's existing _write_manifest_bytes
    helper (which falls back to /tmp + sudo cp).

    Returns (ok, error).
    """
    try:
        from .manifest import (
            applications_dir,
            _write_manifest_bytes,
        )
    except Exception as exc:
        return False, f"manifest import failed: {exc}"

    if shared_dir is None:
        try:
            from ..config import DEFAULT_SHARED_DIR
            shared_dir = DEFAULT_SHARED_DIR
        except Exception:
            shared_dir = Path("/Users/Shared/evolve")

    manifests_dir = applications_dir(shared_dir, bot_id)
    manifest_path = manifests_dir / f"{app_id}.json"
    if not manifest_path.exists():
        # Try sudo cat fallback.
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(manifest_path)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return False, f"manifest not found: {manifest_path}"
            data = json.loads(r.stdout)
        except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return False, f"manifest read failed: {exc}"
    else:
        try:
            data = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"manifest read failed: {exc}"

    accepted = data.get("audit_accepted") or []
    if any(isinstance(a, dict) and a.get("signature") == signature for a in accepted):
        return True, ""   # Already accepted; no-op.

    accepted.append({
        "signature": signature,
        "accepted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "accepted_by": accepted_by,
        "rationale": rationale,
    })
    data["audit_accepted"] = accepted

    try:
        _write_manifest_bytes(
            manifest_path,
            json.dumps(data, indent=2).encode("utf-8"),
        )
        return True, ""
    except Exception as exc:
        return False, f"manifest write failed: {exc}"



# ── Substrate dispatch (Workstream B-skills) ────────────────────────────────
#
# Spec: docs/spec-audit-extensions-2026-05-17.md §4. Parallel to request_audit
# but routes kind=skill_audit | provider_audit to the bot-side runner.


def request_substrate_audit(
    bot_id: str,
    bot_user: str,
    *,
    element_type: str,                  # "skill" | "provider"
    elements: list[str] | None = None,  # None == all
    full_audit: bool = False,
    requested_by: str = "operator",
    kick: bool = True,
) -> DispatchResult:
    """Queue a substrate (skill or provider) audit request on *bot_id*.

    Parallel to :func:`request_audit` but writes a different ``kind`` into
    the inbox file so the runner routes to ``run_skill_audits`` /
    ``run_provider_audits``. The runner shares the lockfile and the kick
    path with app audits.

    ``element_type`` must be "skill" or "provider". When ``elements`` is
    None, the runner audits every known element of that type that has an
    install / provider module on disk.
    """
    if element_type not in ("skill", "provider"):
        return DispatchResult(
            ok=False, request_id="", bot_id=bot_id, bot_user=bot_user,
            apps=None, full_audit=full_audit,
            error=f"unknown element_type: {element_type!r}",
        )

    request_id = f"audit-req-{uuid.uuid4().hex[:10]}"
    inbox_dir = _audit_inbox_dir(bot_user)

    kind = "skill_audit" if element_type == "skill" else "provider_audit"
    plural = "skills" if element_type == "skill" else "providers"
    request: dict = {
        "request_id": request_id,
        "kind": kind,
        plural: elements if elements is not None else "all",
        "full_audit": bool(full_audit),
        "requested_by": requested_by,
        "requested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    inbox_path = inbox_dir / f"{request_id}.json"
    write_ok, write_err = _write_inbox_file(inbox_path, bot_user, request)
    result = DispatchResult(
        ok=write_ok, request_id=request_id, bot_id=bot_id, bot_user=bot_user,
        apps=elements, full_audit=full_audit,
        inbox_path=str(inbox_path), error=write_err,
    )
    if not write_ok:
        return result

    if kick:
        kicked, kick_err = _kick_runner(bot_user, request_id)
        result.kicked = kicked
        if kick_err:
            # See request_audit — kick failure leaves the inbox stranded
            # because the tier-3 cron daemon doesn't drain it.
            logger.warning(
                "audit_dispatch: substrate kick failed for %s/%s: %s",
                bot_user, request_id, kick_err,
            )
            result.error = f"kick failed: {kick_err}"

    return result


def mark_substrate_finding_accepted(
    bot_id: str,
    bot_user: str,
    element_type: str,        # "skill" | "provider"
    element_id: str,
    signature: str,
    *,
    accepted_by: str,
    rationale: str = "",
) -> tuple[bool, str]:
    """Append a signature to the substrate accepted-list sidecar file.

    Substrate elements don't have manifests, so the accepted list lives at
    ``/Users/<bot>/.openclaw/workspace/evolve/<kind>_audits/<element>/accepted.json``.
    The runner reads this on each tick (same role manifest.audit_accepted
    plays for apps). Direct write — the bot has write ACL on workspace/evolve/.
    """
    if element_type not in ("skill", "provider"):
        return False, f"unknown element_type: {element_type!r}"

    parent = "skill_audits" if element_type == "skill" else "provider_audits"
    accepted_path = (
        _acct_home(bot_user)
        / f".openclaw/workspace/evolve/{parent}/{element_id}/accepted.json"
    )

    # Read existing (may not exist).
    if accepted_path.exists():
        try:
            data = json.loads(accepted_path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {"accepted": []}
    else:
        # sudo fallback for read on systems without ACL yet.
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(accepted_path)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
            else:
                data = {"accepted": []}
        except (subprocess.SubprocessError, json.JSONDecodeError):
            data = {"accepted": []}

    accepted = data.get("accepted") or []
    if any(isinstance(a, dict) and a.get("signature") == signature for a in accepted):
        return True, ""  # Idempotent no-op.

    accepted.append({
        "signature": signature,
        "accepted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "accepted_by": accepted_by,
        "rationale": rationale,
    })
    data["accepted"] = accepted

    content = json.dumps(data, indent=2)

    # Try direct write; fall back to /tmp + sudo cp.
    try:
        accepted_path.parent.mkdir(parents=True, exist_ok=True)
        accepted_path.write_text(content)
        return True, ""
    except PermissionError:
        pass
    except OSError as exc:
        return False, f"direct write OSError: {exc}"

    fd, tmp = tempfile.mkstemp(
        dir="/tmp", prefix="evolve-substrate-accept-", suffix=".json",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        try:
            subprocess.run(
                ["sudo", "/bin/mkdir", "-p", str(accepted_path.parent)],
                capture_output=True, timeout=5,
            )
        except subprocess.SubprocessError:
            pass
        r = subprocess.run(
            ["sudo", "/bin/cp", tmp, str(accepted_path)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"sudo cp failed: {r.stderr.strip()[:200]}"
        subprocess.run(
            ["sudo", "/bin/chmod", "644", str(accepted_path)],
            capture_output=True, timeout=5,
        )
        return True, ""
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass



# ── Investigation dispatch (Workstream C — evo fail) ───────────────────────
#
# Spec: docs/spec-audit-extensions-2026-05-17.md §5.2. The handler in
# evo/handlers/fail.py calls request_investigation(); the bot's audit
# runner picks the inbox file up and routes kind=="investigation" to its
# dedicated investigation runner module.


@dataclass
class InvestigationDispatchResult:
    """Return shape for :func:`request_investigation`."""
    ok: bool
    investigation_id: str
    bot_id: str
    bot_user: str
    user_description: str
    requesting_user: str
    inbox_path: str = ""
    kicked: bool = False
    error: str = ""


def request_investigation(
    bot_id: str,
    bot_user: str,
    *,
    user_description: str,
    requesting_user: str,
    kick: bool = True,
) -> InvestigationDispatchResult:
    """Queue an ``evo fail`` investigation on the bot and kick the runner.

    Spec: docs/spec-audit-extensions-2026-05-17.md §5.2.

    Writes an inbox file with shape::

        {
          "investigation_id": "inv-<8hex>",
          "kind":             "investigation",
          "user_description": str (verbatim),
          "requesting_user":  str (the user_key from the evo dispatcher),
          "requested_at":     ISO timestamp,
          "request_id":       same as investigation_id (legacy compat),
        }

    Kick failures are non-fatal — the runner picks the request up on its
    next inbox tick.
    """
    if not user_description or not user_description.strip():
        return InvestigationDispatchResult(
            ok=False, investigation_id="",
            bot_id=bot_id, bot_user=bot_user,
            user_description="", requesting_user=requesting_user,
            error="user_description is required",
        )

    investigation_id = f"inv-{uuid.uuid4().hex[:8]}"
    inbox_dir = _audit_inbox_dir(bot_user)

    request = {
        "investigation_id": investigation_id,
        "request_id": investigation_id,
        "kind": "investigation",
        "user_description": user_description.strip(),
        "requesting_user": requesting_user or "operator",
        "requested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    inbox_path = inbox_dir / f"investigation-{investigation_id}.json"
    write_ok, write_err = _write_inbox_file(inbox_path, bot_user, request)
    result = InvestigationDispatchResult(
        ok=write_ok,
        investigation_id=investigation_id,
        bot_id=bot_id, bot_user=bot_user,
        user_description=user_description.strip(),
        requesting_user=requesting_user or "operator",
        inbox_path=str(inbox_path),
        error=write_err,
    )
    if not write_ok:
        return result

    if kick:
        kicked, kick_err = _kick_runner(bot_user, investigation_id)
        result.kicked = kicked
        if kick_err:
            # See request_audit — kick failure leaves the inbox stranded
            # because the tier-3 cron daemon doesn't drain it.
            logger.warning(
                "audit_dispatch: investigation kick failed for %s/%s: %s",
                bot_user, investigation_id, kick_err,
            )
            result.error = f"kick failed: {kick_err}"

    return result


def _read_trail_text(trail_path: Path) -> str | None:
    """Read a bot-side trail file, falling back to sudo /bin/cat on PermissionError."""
    try:
        return trail_path.read_text()
    except (FileNotFoundError, OSError):
        pass
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(trail_path)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return r.stdout
    except subprocess.SubprocessError:
        pass
    return None


def _investigations_trail_path(bot_user: str) -> Path:
    """Bot-side investigation trail location. Module-level so tests can
    monkeypatch the resolver without intercepting Path() globally."""
    return (
        _acct_home(bot_user)
        / ".openclaw/workspace/evolve/investigations/trail.jsonl"
    )


def _investigations_outbox_dir(bot_user: str) -> Path:
    """Bot-side audit outbox (where investigation_unresolved records land)."""
    return _acct_home(bot_user) / ".openclaw/workspace/evolve/audit_outbox"


def latest_investigation_for_user(
    bot_user: str, requesting_user: str,
) -> dict | None:
    """Return the most-recent investigation trail entry for *requesting_user*.

    Used by ``evo fail flag`` (so we know which investigation to escalate),
    ``evo fail recent`` (re-investigates the latest), and ``evo fail
    status`` (reports state). Reads investigations/trail.jsonl directly.
    Returns None when no entries exist for this user.
    """
    trail_path = _investigations_trail_path(bot_user)
    text = _read_trail_text(trail_path)
    if not text:
        return None
    latest: dict | None = None
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("requesting_user") != requesting_user:
            continue
        latest = entry
    return latest


def count_investigations_in_window(
    bot_user: str, requesting_user: str, *, window_seconds: int,
) -> int:
    """Count investigations from *requesting_user* in the last *window_seconds*.

    Used to enforce the 3-per-user-per-hour soft cap (spec §8 Q3). Counts
    trail-recorded investigations regardless of status; trail entries
    persist after the in-flight inbox file is archived, which is the
    conservative behavior for rate limiting.
    """
    trail_path = _investigations_trail_path(bot_user)
    text = _read_trail_text(trail_path)
    if not text:
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - window_seconds
    count = 0
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("requesting_user") != requesting_user:
            continue
        ts = entry.get("ts") or ""
        try:
            ts_epoch = datetime.fromisoformat(
                ts.replace("Z", "+00:00")
            ).timestamp()
        except (ValueError, AttributeError):
            continue
        if ts_epoch >= cutoff:
            count += 1
    return count


def flag_investigation_unresolved(
    bot_id: str,
    bot_user: str,
    *,
    requesting_user: str,
) -> tuple[bool, str, str]:
    """Escalate the user's most-recent investigation to an operator Proposal.

    Spec: §5.5. The user types `evo fail flag` after a no-diagnosis or
    unsatisfying reply; this helper finds the relevant investigation
    trail entry and writes an ``investigation_unresolved`` record into
    the bot's audit_outbox. The admin poller (extended in audit_poller.py)
    turns it into a Proposal in the arbiter.

    Returns ``(ok, error, investigation_id)``.
    """
    inv = latest_investigation_for_user(bot_user, requesting_user)
    if inv is None:
        return False, "no recent investigation found for this user", ""

    investigation_id = inv.get("investigation_id") or ""
    outbox_dir = _investigations_outbox_dir(bot_user)
    record = {
        "record_id": f"inv-flag-{uuid.uuid4().hex[:8]}",
        "kind": "investigation_unresolved",
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "producer": "app_audit_investigation",
        "bot_id": bot_id,
        "investigation_id": investigation_id,
        "user_description": inv.get("user_description") or "",
        "requesting_user": requesting_user,
        "triage_candidates": inv.get("triage_candidates") or [],
        "chosen_candidate": inv.get("chosen_candidate"),
        "evidence": inv.get("evidence") or [],
        "previous_diagnosis": inv.get("diagnosis"),
        "previous_confidence": inv.get("confidence"),
        "related_signal_ids": inv.get("related_signal_ids") or [],
    }
    content = json.dumps(record, indent=2)

    # Direct write first; fall back to /tmp + sudo cp (matches
    # _write_inbox_file's pattern).
    try:
        outbox_dir.mkdir(parents=True, exist_ok=True)
        (outbox_dir / f"{record['record_id']}.json").write_text(content)
        return True, "", investigation_id
    except PermissionError:
        pass
    except OSError as exc:
        return False, f"direct write OSError: {exc}", investigation_id

    fd, tmp = tempfile.mkstemp(
        dir="/tmp", prefix="evolve-inv-flag-", suffix=".json",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        try:
            subprocess.run(
                ["sudo", "/bin/mkdir", "-p", str(outbox_dir)],
                capture_output=True, timeout=5,
            )
        except subprocess.SubprocessError:
            pass
        dest = outbox_dir / f"{record['record_id']}.json"
        r = subprocess.run(
            ["sudo", "/bin/cp", tmp, str(dest)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return (
                False,
                f"sudo cp outbox failed: {r.stderr.strip()[:200]}",
                investigation_id,
            )
        subprocess.run(
            ["sudo", "/bin/chmod", "644", str(dest)],
            capture_output=True, timeout=5,
        )
        return True, "", investigation_id
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def unaccept_finding(
    bot_id: str,
    bot_user: str,
    app_id: str,
    signature: str,
    *,
    shared_dir: Path | None = None,
) -> tuple[bool, str]:
    """Remove an accepted-signature entry from ``manifest.audit_accepted[]``.

    Used by the UI's "Un-accept" link. Returns (ok, error). Idempotent: if
    the signature isn't accepted, this is a no-op.
    """
    try:
        from .manifest import applications_dir, _write_manifest_bytes
    except Exception as exc:
        return False, f"manifest import failed: {exc}"

    if shared_dir is None:
        try:
            from ..config import DEFAULT_SHARED_DIR
            shared_dir = DEFAULT_SHARED_DIR
        except Exception:
            shared_dir = Path("/Users/Shared/evolve")

    manifest_path = applications_dir(shared_dir, bot_id) / f"{app_id}.json"
    try:
        data = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"manifest read failed: {exc}"

    accepted = data.get("audit_accepted") or []
    filtered = [a for a in accepted if not (isinstance(a, dict) and a.get("signature") == signature)]
    if len(filtered) == len(accepted):
        return True, ""  # Not present; no-op.
    data["audit_accepted"] = filtered

    try:
        _write_manifest_bytes(
            manifest_path,
            json.dumps(data, indent=2).encode("utf-8"),
        )
        return True, ""
    except Exception as exc:
        return False, f"manifest write failed: {exc}"
