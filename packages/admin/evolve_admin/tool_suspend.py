"""Tool-suspension flag — shared reader/writer.

The 2026-04-26 SSH-wedge third addendum surfaced the self-cancelling-tool
class: a tool whose stated purpose is the opposite of its actual effect.
heal.py (designed to keep gateways alive) had been killing them for an
unknown duration. The architectural response is meta-tests
(SKILL spec §5.5.5c) that catch this class — and a suspend flag the
broken tool reads BEFORE acting, so the harm stops while we investigate.

The flag is a single JSON file at `docs/verification/.tool-suspended.json`:

    {
      "heal": {
        "suspended_at": "2026-04-26T...",
        "suspended_until": null,
        "reason": "M1 meta-test failed: check_gateway returned False ...",
        "finding_id": "2026-04-26-NNN"
      },
      "apply": { ... }
    }

The file is git-tracked so suspensions are auditable in history. Tools
running on the mini see suspensions via the next git pull (~15 min worst
case via the apply daemon). For v1, this latency is acceptable; v2 may
add a runtime-state mirror at /Users/Shared/evolve/ for instant
propagation.

Usage in an operational tool:

    from evolve_admin.tool_suspend import is_suspended, get_suspension

    def main_loop():
        if is_suspended("heal"):
            sus = get_suspension("heal")
            log.warning(f"heal is suspended: {sus['reason']} (since {sus['suspended_at']}, "
                        f"finding {sus['finding_id']}). Skipping this cycle.")
            return
        # ... normal heal logic
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Default location relative to the repo. Tools that import this module pass
# `repo_root` explicitly when they're not running from the repo root.
DEFAULT_FLAG_PATH = "docs/verification/.tool-suspended.json"


def _flag_path(repo_root: Path | None = None) -> Path:
    """Resolve the suspended-tools flag file path."""
    if repo_root is None:
        # Walk up from this module's location to find the repo root
        here = Path(__file__).resolve()
        # packages/admin/evolve_admin/tool_suspend.py → repo root is 3 parents up
        repo_root = here.parents[3]
    return Path(repo_root) / DEFAULT_FLAG_PATH


def _load(repo_root: Path | None = None) -> dict[str, dict]:
    """Load the flag file. Returns empty dict if the file doesn't exist or
    can't be parsed (defensive — never raise to a tool that's just trying
    to check whether it should run)."""
    path = _flag_path(repo_root)
    if not path.exists():
        return {}
    try:
        with path.open("r") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError):
        # Defensive: a corrupted flag file should not block a tool from
        # running. Worst case we miss a suspension this cycle; the worker's
        # next tick rewrites the file.
        return {}


def _save(data: dict[str, dict], repo_root: Path | None = None) -> None:
    """Atomic write — tmp file in the same dir, then os.rename. Avoids the
    half-written-file race where a tool reads mid-update and gets garbage."""
    path = _flag_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tool-suspended-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.rename(tmp, path)
    except Exception:
        # Best-effort cleanup of the tmp file
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Public API ─────────────────────────────────────────────────────────────


def is_suspended(tool: str, repo_root: Path | None = None) -> bool:
    """Return True if the named tool is currently suspended.

    Cheap (~1 ms typical) — tools should call this on every cycle before
    acting. Defensive against missing file, corrupted JSON, or unparseable
    entries; returns False in any error case (a tool that fails-open is
    safer than one that fails-closed because of a corrupt flag file)."""
    state = _load(repo_root)
    entry = state.get(tool)
    if not entry:
        return False
    # An entry with `suspended_until` in the past is no longer active.
    # Worker normally clears entries via unsuspend(); this is belt-and-
    # suspenders for the case where the worker missed a re-verify.
    until = entry.get("suspended_until")
    if until:
        try:
            until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if until_dt < datetime.now(timezone.utc):
                return False
        except (ValueError, TypeError):
            pass
    return True


def get_suspension(tool: str, repo_root: Path | None = None) -> dict | None:
    """Return the full suspension record for the named tool, or None.

    Use this when a tool wants to log WHY it's suspended (e.g. include
    the finding_id in its output so an operator can find the related
    finding)."""
    state = _load(repo_root)
    entry = state.get(tool)
    if not entry:
        return None
    return dict(entry)   # shallow copy to prevent caller mutation


def suspend(
    tool: str,
    reason: str,
    finding_id: str,
    suspended_until: str | None = None,
    repo_root: Path | None = None,
) -> None:
    """Mark the named tool as suspended. Writes atomically.

    Called by the v2 worker's step 11 verdict dispatch when filing a
    `tool-meta-broken` finding. Idempotent: re-suspending an already-
    suspended tool overwrites the entry (newer reason/finding wins)."""
    state = _load(repo_root)
    state[tool] = {
        "suspended_at": datetime.now(timezone.utc).isoformat(),
        "suspended_until": suspended_until,
        "reason": reason,
        "finding_id": finding_id,
    }
    _save(state, repo_root)


def unsuspend(tool: str, repo_root: Path | None = None) -> bool:
    """Clear the suspension entry for the named tool.

    Returns True if an entry was removed, False if the tool wasn't
    suspended. Called by the worker's step 5a when re-verify confirms the
    meta-test now passes."""
    state = _load(repo_root)
    if tool not in state:
        return False
    del state[tool]
    _save(state, repo_root)
    return True


def list_suspensions(repo_root: Path | None = None) -> dict[str, dict]:
    """Return all current suspension records. Used by the diagnose tool
    + dashboard for visibility."""
    state = _load(repo_root)
    # Filter out expired entries (same logic as is_suspended)
    out: dict[str, dict] = {}
    now = datetime.now(timezone.utc)
    for tool, entry in state.items():
        until = entry.get("suspended_until")
        if until:
            try:
                until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
                if until_dt < now:
                    continue
            except (ValueError, TypeError):
                pass
        out[tool] = dict(entry)
    return out


# ── Convenience for tools that want a one-line guard ───────────────────────


def guard(tool: str, log_fn=None, repo_root: Path | None = None) -> bool:
    """Convenience guard for tool main loops.

    Returns True if the tool should SKIP this cycle (it's suspended).
    Returns False if the tool should proceed normally.

    **Never raises.** If anything in the suspend-check path fails
    unexpectedly (corrupt JSON shape we didn't anticipate, filesystem
    weirdness, _flag_path resolution edge case), guard() fails OPEN —
    returns False so the calling tool continues with normal operation.
    A fail-closed design (assume suspended on error) would silently
    halt critical tools like heal whenever the flag system itself is
    broken; that's a worse failure mode than the suspend mechanism
    being temporarily inactive.

    If log_fn is provided AND the tool is suspended, calls log_fn with a
    one-line description of why. log_fn is typically logging.warning or
    print.

    Usage:
        if guard("heal", log.warning):
            return
        # ... normal logic
    """
    try:
        if not is_suspended(tool, repo_root):
            return False
        if log_fn is not None:
            sus = get_suspension(tool, repo_root) or {}
            msg = (
                f"{tool} is SUSPENDED — skipping this cycle. "
                f"Reason: {sus.get('reason', 'unknown')}. "
                f"Suspended at: {sus.get('suspended_at', 'unknown')}. "
                f"Finding: {sus.get('finding_id', 'unknown')}."
            )
            try:
                log_fn(msg)
            except Exception:
                pass   # never let logging issues block the guard
        return True
    except Exception as e:
        # Fail-open. Best-effort log so the operator knows the suspend
        # mechanism is broken (separately from any real suspension).
        if log_fn is not None:
            try:
                log_fn(f"suspend-guard check failed (fail-open, continuing normal operation): "
                       f"{type(e).__name__}: {e}")
            except Exception:
                pass
        return False
