"""Remediation handlers — one per ``Remediation.kind``.

Each handler takes ``(params, shared_dir)`` and returns a JSON-able dict
captured as ``Job.output``. Raise for failures; the dispatcher catches
and records the message in ``Job.error``.

The handler-registry is intentionally explicit: ``HANDLERS`` is a plain
dict, not a decorator-based registry. New action kinds add an entry; the
schema and UI surface them in their own PRs.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

# Canonical bot_id → macOS-username resolution (self-loads network config
# when called with just a bot_id; raises if the config can't be loaded).
from evolve_config import get_bot_user as _bot_user_for  # type: ignore


Handler = Callable[[dict[str, Any], Path], dict[str, Any]]


# ─────────────────────────────────────────────────────────────────────────────
# Handler 1: install_infra_jobs
# ─────────────────────────────────────────────────────────────────────────────


def handle_install_infra_jobs(params: dict[str, Any], shared_dir: Path) -> dict[str, Any]:
    """Run ``sudo evolve-admin install-infra-jobs`` and capture output.

    This is the canonical fix for the launchd-plist-missing alert
    (``ai.evolve.evolve.proposal_synthesizer`` and similar). Idempotent
    — the CLI skips already-installed plists.

    No params today; reserved for future ``--filter`` style narrowing.
    """
    r = subprocess.run(
        ["sudo", "evolve-admin", "install-infra-jobs"],
        capture_output=True, text=True, timeout=180,
    )
    out: dict[str, Any] = {
        "exit_code": r.returncode,
        "stdout": r.stdout,
        "stderr": r.stderr,
    }
    if r.returncode != 0:
        raise RuntimeError(
            f"install-infra-jobs exited {r.returncode}: {r.stderr.strip()[:500]}"
        )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Handler 2: reset_baseline
# ─────────────────────────────────────────────────────────────────────────────


def handle_reset_baseline(params: dict[str, Any], shared_dir: Path) -> dict[str, Any]:
    """Drop a bot's entry from a per-bot audit baseline (scripts, shell, cron-jobs).

    Params:
      - ``bot_id`` (required)
      - ``kind`` (required) — one of ``scripts`` / ``shell`` / ``cron-jobs``

    Delegates to ``audit.reset_baseline``. The next audit run reseeds the
    baseline from observed state, clearing the drift findings the alert
    was emitting.
    """
    bot_id = params.get("bot_id")
    kind = params.get("kind")
    if not isinstance(bot_id, str) or not bot_id:
        raise ValueError("reset_baseline requires params.bot_id (str)")
    if not isinstance(kind, str) or not kind:
        raise ValueError("reset_baseline requires params.kind (str)")

    # audit lives in the analyzer package; import lazily so the admin
    # process doesn't drag in audit's transitive deps unless this handler runs.
    from audit import reset_baseline  # type: ignore

    cleared = reset_baseline(bot_id, kind, shared_dir)
    if not cleared:
        # Not an error — the baseline may have been clean already, or the
        # kind may not be registered. Caller distinguishes via ``cleared``.
        return {"cleared": False, "bot_id": bot_id, "kind": kind,
                "note": "no entry to clear (already absent or unknown kind)"}
    return {"cleared": True, "bot_id": bot_id, "kind": kind}


# ─────────────────────────────────────────────────────────────────────────────
# Handler 3: flip_cron_session_target
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_bot_user(bot_id: str) -> str:
    """Look up the macOS username for a bot via evolve_config (network.json).

    Imported lazily for the same reason as audit above.
    """
    from evolve_config import get_bot_user, load_config  # type: ignore
    return get_bot_user(bot_id, load_config())


def handle_flip_cron_session_target(
    params: dict[str, Any], shared_dir: Path,
) -> dict[str, Any]:
    """Flip a named cron's ``sessionTarget`` from ``main`` to ``isolated``.

    Resolves the bot's cron/jobs.json (under the bot's macOS user home),
    reads via ``sudo /bin/cat`` (bot user owns the file), mutates the
    matching entry, writes back via /tmp staging + ``sudo /bin/cp``.

    Params:
      - ``bot_id`` (required)
      - ``cron_name`` (required)

    Returns ``{flipped: True, from: "main", to: "isolated"}`` on success.
    Raises if the cron file is unreadable, the named cron doesn't exist,
    or the current sessionTarget isn't ``main`` (silent no-op would
    mask the case where someone manually changed it).
    """
    bot_id = params.get("bot_id")
    cron_name = params.get("cron_name")
    if not isinstance(bot_id, str) or not bot_id:
        raise ValueError("flip_cron_session_target requires params.bot_id (str)")
    if not isinstance(cron_name, str) or not cron_name:
        raise ValueError("flip_cron_session_target requires params.cron_name (str)")

    bot_user = _resolve_bot_user(bot_id)
    cron_path = Path(f"/Users/{bot_user}/.openclaw/cron/jobs.json")

    r = subprocess.run(
        ["sudo", "/bin/cat", str(cron_path)],
        capture_output=True, text=True, timeout=5,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"cannot read {cron_path}: {r.stderr.strip() or 'sudo /bin/cat failed'}"
        )
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{cron_path} is not valid JSON: {e}") from e

    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        raise RuntimeError(f"{cron_path}: unexpected shape (expected jobs list)")

    target = next(
        (j for j in jobs if isinstance(j, dict) and j.get("name") == cron_name),
        None,
    )
    if target is None:
        raise RuntimeError(f"no cron named {cron_name!r} in {cron_path}")
    current = target.get("sessionTarget")
    if current == "isolated":
        return {
            "flipped": False, "bot_id": bot_id, "cron_name": cron_name,
            "note": "sessionTarget already 'isolated'",
        }
    if current != "main":
        raise RuntimeError(
            f"{cron_name!r}: sessionTarget is {current!r}, refusing to flip "
            f"(only 'main' → 'isolated' is supported)"
        )
    target["sessionTarget"] = "isolated"

    fd, tmp = tempfile.mkstemp(
        dir="/tmp",
        prefix=f"rem-{bot_user}-{cron_name}-",
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        cp = subprocess.run(
            ["sudo", "/bin/cp", tmp, str(cron_path)],
            capture_output=True, text=True,
        )
        if cp.returncode != 0:
            raise RuntimeError(f"sudo /bin/cp failed: {cp.stderr.strip()}")
        subprocess.run(["sudo", "/bin/chmod", "600", str(cron_path)], capture_output=True)
        subprocess.run(
            ["sudo", "/usr/sbin/chown", f"{bot_user}:staff", str(cron_path)],
            capture_output=True,
        )
    finally:
        Path(tmp).unlink(missing_ok=True)

    return {
        "flipped": True, "bot_id": bot_id, "cron_name": cron_name,
        "from": "main", "to": "isolated",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Handler 4: set_exec_allowlist
# ─────────────────────────────────────────────────────────────────────────────


def _load_bot_oc_config(bot_user: str) -> dict[str, Any]:
    """Read the bot's openclaw.json (owned by the bot user).

    Direct read first (the evolve user has ACL read on every bot's
    .openclaw dir per CLAUDE.md); fall back to `sudo /bin/cat` for hosts
    where the ACL hasn't been applied yet.
    """
    path = Path(f"/Users/{bot_user}/.openclaw/openclaw.json")
    try:
        return json.loads(path.read_text())
    except (PermissionError, OSError):
        pass
    r = subprocess.run(
        ["sudo", "/bin/cat", str(path)],
        capture_output=True, text=True, timeout=5,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"cannot read {path}: {r.stderr.strip() or 'sudo /bin/cat failed'}"
        )
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{path} is not valid JSON: {e}") from e


def _safe_write_bot_config(bot_id: str, cfg: dict[str, Any], reason: str) -> None:
    """Thin wrapper around deploy.safe_write_bot_config that raises rather
    than returning a (success, msg) tuple — simpler for handler error flow."""
    from evolve_admin.deploy import safe_write_bot_config  # type: ignore
    ok, err = safe_write_bot_config(bot_id, cfg, reason=reason)
    if not ok:
        raise RuntimeError(err)


def handle_set_exec_allowlist(params: dict[str, Any], shared_dir: Path) -> dict[str, Any]:
    """Write an allowlist into the bot's tools.exec.allowlist.

    Params:
      - ``bot_id`` (required)
      - ``commands`` (required) — list of command names (e.g. ``["ls", "cat", "git"]``).
        Empty list explicitly clears the allowlist; pass it deliberately
        rather than omitting the field, so we don't silently no-op.

    Resolves security_warden's ``multi_user_exec_full_unscoped`` finding
    when the bot has security="full" and the operator wants to scope down.
    OpenClaw treats a non-empty allowlist as "this is the full set of
    allowed commands"; commands outside the list prompt the operator via
    ``ask=on-miss``.
    """
    bot_id = params.get("bot_id")
    commands = params.get("commands")
    if not isinstance(bot_id, str) or not bot_id:
        raise ValueError("set_exec_allowlist requires params.bot_id (str)")
    if not isinstance(commands, list):
        raise ValueError("set_exec_allowlist requires params.commands (list[str])")
    # Reject non-string entries early — clearer error than a deferred OC
    # validation failure that mentions "schema mismatch at tools.exec.allowlist[3]".
    for i, c in enumerate(commands):
        if not isinstance(c, str):
            raise ValueError(
                f"set_exec_allowlist: commands[{i}] is not a string ({type(c).__name__})"
            )

    bot_user = _bot_user_for(bot_id)
    cfg = _load_bot_oc_config(bot_user)
    tools = cfg.setdefault("tools", {})
    if not isinstance(tools, dict):
        tools = {}
        cfg["tools"] = tools
    exec_cfg = tools.setdefault("exec", {})
    if not isinstance(exec_cfg, dict):
        exec_cfg = {}
        tools["exec"] = exec_cfg

    previous = list(exec_cfg.get("allowlist") or [])
    exec_cfg["allowlist"] = list(commands)
    _safe_write_bot_config(bot_id, cfg, reason=f"set_exec_allowlist via remediation ({len(commands)} commands)")

    return {
        "bot_id": bot_id,
        "previous_allowlist": previous,
        "new_allowlist": list(commands),
        "count": len(commands),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Handler 5: set_exec_security
# ─────────────────────────────────────────────────────────────────────────────


_ALLOWED_EXEC_LEVELS = frozenset({"full", "allowlist", "deny"})


def handle_set_exec_security(params: dict[str, Any], shared_dir: Path) -> dict[str, Any]:
    """Set tools.exec.security on a bot ({"full" | "allowlist" | "deny"}).

    Params:
      - ``bot_id`` (required)
      - ``level``  (required) — one of full / allowlist / deny

    Pair with set_exec_allowlist when downgrading to ``allowlist`` — an
    empty allowlist at that level means "no commands run." OpenClaw's
    validator catches the inconsistency before write, so a bad combo
    raises RuntimeError rather than corrupting the live config.
    """
    bot_id = params.get("bot_id")
    level = params.get("level")
    if not isinstance(bot_id, str) or not bot_id:
        raise ValueError("set_exec_security requires params.bot_id (str)")
    if level not in _ALLOWED_EXEC_LEVELS:
        raise ValueError(
            f"set_exec_security requires params.level in {sorted(_ALLOWED_EXEC_LEVELS)}; got {level!r}"
        )

    bot_user = _bot_user_for(bot_id)
    cfg = _load_bot_oc_config(bot_user)
    tools = cfg.setdefault("tools", {})
    if not isinstance(tools, dict):
        tools = {}
        cfg["tools"] = tools
    exec_cfg = tools.setdefault("exec", {})
    if not isinstance(exec_cfg, dict):
        exec_cfg = {}
        tools["exec"] = exec_cfg

    previous = exec_cfg.get("security")
    if previous == level:
        return {
            "bot_id": bot_id, "level": level,
            "note": "security already at this level (no write)",
        }
    exec_cfg["security"] = level
    _safe_write_bot_config(bot_id, cfg, reason=f"set_exec_security={level} via remediation")
    return {"bot_id": bot_id, "previous": previous, "new": level}


# ─────────────────────────────────────────────────────────────────────────────
# Handler 6: restore_autonomy_posture
# ─────────────────────────────────────────────────────────────────────────────


def handle_restore_autonomy_posture(
    params: dict[str, Any], shared_dir: Path,
) -> dict[str, Any]:
    """The autonomy_demoted alert's one-click restore (spec-autonomy-
    ladder §3.3): put a stepped-down integration back to "Acts within
    limits" with the rules block the demotion cleared.

    Restore is a PROMOTION — the Remediation.confirm text is the
    confirmation the spec requires, and HANDLER_FIX_RISK pins this
    handler "high" so no authority tier ever auto-fires it (the same
    permanent carve-out as every other auto-approve lane).

    Params (authored by autonomy.reflex when the alert fires):
      - ``bot_id`` (required)
      - ``integration_id`` (required)
      - ``rung`` (required — the rung to restore)
      - ``rules`` (the preserved prior rules block)
      - ``expected_current_rung`` — CAS guard; a posture the operator
        already changed by hand fails loudly instead of clobbering.
    """
    from autonomy import renderer as _arenderer
    from autonomy import store as _astore

    bot_id = str(params.get("bot_id") or "")
    integration_id = str(params.get("integration_id") or "")
    rung = str(params.get("rung") or "")
    if not bot_id or not integration_id or not rung:
        raise ValueError("bot_id, integration_id, and rung are required")
    if "expected_current_rung" not in params:
        # The CAS guard is mandatory, not best-effort: the only
        # legitimate author of these params (autonomy.reflex) always
        # writes it, and a promotion over HTTP without it could clobber
        # a level the operator already changed by hand.
        raise ValueError("expected_current_rung is required")
    posture = _astore.set_posture(
        shared_dir, bot_id, integration_id,
        rung=rung,
        rules=dict(params.get("rules") or {}),
        actor=_astore.ACTOR_OPERATOR_UI,
        note="restored after automatic safety step-down",
        expected_current_rung=params.get("expected_current_rung"),
    )
    render = _arenderer.render_bot(bot_id, shared_dir)
    out: dict[str, Any] = {
        "bot_id": bot_id,
        "integration_id": integration_id,
        "rung": posture.rung,
        "render_changed": render.changed,
        "render_written": render.written,
    }
    if render.write_error:
        out["render_error"] = render.write_error
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────

HANDLERS: dict[str, Handler] = {
    "install_infra_jobs": handle_install_infra_jobs,
    "reset_baseline": handle_reset_baseline,
    "flip_cron_session_target": handle_flip_cron_session_target,
    "set_exec_allowlist": handle_set_exec_allowlist,
    "set_exec_security": handle_set_exec_security,
    "restore_autonomy_posture": handle_restore_autonomy_posture,
}


# Fix-risk classification per handler — drives the tier-c authority gate
# in the Home page's auto-act loop. Spec:
# internal/spec-severity-framework-2026-05-18.md §3. The analyzer-side mirror
# at packages/analyzer/eligibility.REMEDIATION_FIX_RISK must stay in
# sync; both tables are intentionally explicit (no shared import) to
# avoid evolve_admin -> analyzer coupling at runtime.
#
# Risk levels:
#   low    — reversible, bounded blast radius, well-trodden path
#   medium — reversible but broader blast OR mildly destructive
#   high   — security-adjacent OR hard to undo; never auto-fires
HANDLER_FIX_RISK: dict[str, str] = {
    "install_infra_jobs": "medium",     # pod-wide LaunchDaemon reinstall, idempotent
    "reset_baseline": "low",            # cache clear; regenerates on next run
    "flip_cron_session_target": "low",  # single-cron config flip, reversible
    "set_exec_allowlist": "high",       # security policy; always confirm
    "set_exec_security": "high",        # exec policy mode; always confirm
    # Restoring a demoted autonomy posture is a PROMOTION — permanently
    # excluded from auto-fire (spec-autonomy-ladder §3.2/§3.3).
    "restore_autonomy_posture": "high",
}


def get_handler(kind: str) -> Handler:
    """Look up a handler by kind; raise KeyError with the available set."""
    if kind not in HANDLERS:
        raise KeyError(
            f"unknown remediation kind {kind!r}; available: {sorted(HANDLERS)}"
        )
    return HANDLERS[kind]
