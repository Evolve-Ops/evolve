#!/usr/bin/env python3
"""update_watcher.py — Phase E2 of the alert-subscriptions spec.

Polls upstream sources for new versions / commits and emits via the
alerts dispatcher when updates are available. The catalog already
declares three update event types (from Phase A1 #936):

  updates.openclaw_available  — new openclaw npm release published
  updates.evolve_repo         — new commits on origin/main
  updates.plugin_available    — per-bot plugin update (deferred to v2)

Pure-Python checks (urllib for npm registry, subprocess for git
ls-remote). Runs daily via launchd. Operator can mute or change
frequency from the Subscriptions UI.

State at ``{shared_dir}/update_watcher/state.json`` tracks the
last-notified version/sha per check so we only push once per new
release rather than every daily tick.

Usage:
    python3 update_watcher.py
    python3 update_watcher.py --network /Users/Shared/evolve/network.json
    python3 update_watcher.py --check openclaw   # one check only
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from platform_profile import get_profile

try:
    from evolve_config import load_config, get_shared_dir, resolve_network_path
except ImportError:
    def load_config(n=None):  # type: ignore[misc]
        return json.loads(Path("/Users/Shared/evolve/network.json").read_text()) \
            if Path("/Users/Shared/evolve/network.json").exists() else {}
    def get_shared_dir(c):  # type: ignore[misc]
        return Path(c.get("sharedDir", "/Users/Shared/evolve"))
    def resolve_network_path(o=None):  # type: ignore[misc]
        return Path("/Users/Shared/evolve/network.json")


# ── Constants ───────────────────────────────────────────────────────────────


NPM_REGISTRY = "https://registry.npmjs.org"

# Where openclaw lives once `npm install -g openclaw` has run on the pod host.
# We read package.json from the first path that exists. Platform-aware via
# platform_profile (macOS Homebrew/usr-local vs Linux /usr/lib/node_modules) —
# the macOS-only list before read OpenClaw's version as None on Linux.
def _resolve_openclaw_install_candidates() -> tuple[Path, ...]:
    return get_profile().openclaw_pkg_json_candidates


_OPENCLAW_INSTALL_CANDIDATES = _resolve_openclaw_install_candidates()

# Canonical deploy checkout — where origin/main is auto-pulled by
# repo_puller. Same path the puller writes wedge issues against.
# Platform-keyed: /Users/Shared/evolve-repo on macOS, /var/lib/evolve/repo
# on Linux (byte-identical on macOS).
_DEPLOY_REPO = Path(get_profile().deploy_checkout_default)


# ── State file ──────────────────────────────────────────────────────────────


def _state_path(shared_dir: Path) -> Path:
    return shared_dir / "update_watcher" / "state.json"


def _load_state(shared_dir: Path) -> dict[str, Any]:
    p = _state_path(shared_dir)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 1}


def _save_state(shared_dir: Path, state: dict[str, Any]) -> None:
    p = _state_path(shared_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    fd, tmp = tempfile.mkstemp(
        dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── npm registry helpers ────────────────────────────────────────────────────


def fetch_npm_latest_version(package: str) -> str | None:
    """Return the latest published version of an npm package, or None
    if the registry is unreachable / response is malformed."""
    url = f"{NPM_REGISTRY}/{package}/latest"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        return data.get("version")
    except (urllib.error.URLError, urllib.error.HTTPError,
            json.JSONDecodeError, OSError, ValueError):
        return None


def read_installed_openclaw_version() -> str | None:
    """Read openclaw's installed version from its npm package.json on
    disk. Returns None if not installed in any known location."""
    for p in _OPENCLAW_INSTALL_CANDIDATES:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                v = data.get("version")
                if v:
                    return str(v)
            except (json.JSONDecodeError, OSError):
                continue
    return None


# ── OC CLI `update status --json` (primary; npm poll is the fallback) ────────

# Known install locations for a runnable openclaw CLI — the per-platform exec
# PATH dirs (Homebrew/usr-local on macOS; /usr/bin etc. on Linux, where the
# NodeSource openclaw lands). Derived from platform_profile so it tracks the
# same bin dirs the launchd/systemd job PATH uses; shutil.which() in
# _find_openclaw_cli is the final fallback.
def _resolve_openclaw_cli_candidates() -> tuple[Path, ...]:
    return tuple(Path(d) / "openclaw" for d in get_profile().exec_path_dirs)


_OPENCLAW_CLI_CANDIDATES = _resolve_openclaw_cli_candidates()


def _find_openclaw_cli() -> str | None:
    """Best-effort path to a runnable ``openclaw`` binary, or None."""
    for p in _OPENCLAW_CLI_CANDIDATES:
        if p.exists():
            return str(p)
    import shutil
    return shutil.which("openclaw")


def fetch_oc_update_status(timeout: int = 20) -> dict[str, Any] | None:
    """Run ``openclaw update status --json`` and return the parsed structure.

    OC's own CLI is the canonical source of truth for "is there an update":
    it returns ``registry.latestVersion``, ``availability.{available,
    hasRegistryUpdate,hasGitUpdate,gitBehind}`` and ``channel.{value,source}``
    in one call — channel + git-behind awareness the raw npm poll can't give.
    Adopting it follows the repo's don't-reimplement-upstream discipline.

    Returns None on any failure (CLI absent, non-zero exit, unparseable
    output) so the caller falls back to the npm registry poll.
    """
    cli = _find_openclaw_cli()
    if not cli:
        return None
    # launchd/sudo contexts get a minimal PATH; the CLI's `env node` shebang
    # needs its own bin dir on PATH. cwd=/tmp: node calls uv_cwd() at startup.
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(cli) + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.run(
            [cli, "update", "status", "--json"],
            capture_output=True, text=True, timeout=timeout, cwd="/tmp", env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def resolve_latest_openclaw_version() -> tuple[str | None, dict[str, Any] | None]:
    """Resolve the latest available openclaw version, preferring OC's own
    ``update status --json`` and falling back to the npm registry poll.

    Returns ``(version, status)`` where ``status`` is the parsed
    ``update status`` payload when available (None on fallback). The version
    is None only when both sources are unreachable.
    """
    status = fetch_oc_update_status()
    if status:
        latest = (status.get("registry") or {}).get("latestVersion")
        if isinstance(latest, str) and latest:
            return latest, status
    return fetch_npm_latest_version("openclaw"), status


# ── Semver comparison ──────────────────────────────────────────────────────


def _version_tuple(v: str) -> tuple[int, ...]:
    """Lenient semver parse: '2026.4.29' → (2026, 4, 29). Non-numeric
    parts compare as 0. Used to compare two versions without importing
    a full semver library."""
    parts: list[int] = []
    for chunk in v.split("."):
        # Trim any pre-release suffix like '-rc.1'
        head = "".join(c for c in chunk.split("-")[0] if c.isdigit())
        parts.append(int(head) if head else 0)
    return tuple(parts)


def is_newer_version(latest: str, current: str) -> bool:
    """Return True iff latest semver > current semver."""
    return _version_tuple(latest) > _version_tuple(current)


# ── Git helpers ─────────────────────────────────────────────────────────────


def git_ls_remote_head(repo: Path, branch: str = "main") -> str | None:
    """Return the SHA of origin/<branch> via ``git ls-remote``, or None
    on any failure (network, no repo, no branch)."""
    if not repo.exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "origin", branch],
            cwd=str(repo), capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    line = proc.stdout.strip().split("\n", 1)[0]
    if not line:
        return None
    sha = line.split()[0]
    return sha if len(sha) >= 40 else None


def git_count_commits_between(repo: Path, base_sha: str, head_sha: str) -> int | None:
    """Count commits between ``base_sha`` and ``head_sha`` via
    ``git rev-list --count``. Returns None on failure."""
    if not repo.exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "rev-list", "--count", f"{base_sha}..{head_sha}"],
            cwd=str(repo), capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except (ValueError, AttributeError):
        return None


# ── Dispatcher hand-off ────────────────────────────────────────────────────


def _dispatch_update(
    *,
    shared_dir: Path,
    network: dict,
    dedup_key: str,
    catalog_event: str,
    payload: dict,
) -> bool:
    """Route an update notification through the alerts dispatcher.

    Phase F: passes the structured ``payload`` to the dispatcher and
    lets the catalog's ``body_template`` + ``action`` render the final
    message. No source-side template here — one place (the catalog)
    owns the format.

    Returns True iff DispatchResult.SENT. Caller treats anything else
    (suppressed, no_recipient, failed, deferred) as "not delivered" and
    keeps the prior last-notified state untouched so the next run can
    re-attempt.
    """
    try:
        from evolve_admin.alerts.dispatcher import (
            send as _dispatch_send, Severity, DispatchResult,
        )
    except Exception as exc:
        print(f"[update_watcher] dispatcher import failed; "
              f"alert dropped: {exc}", file=sys.stderr)
        return False

    try:
        outcome = _dispatch_send(
            shared_dir=shared_dir,
            network=network,
            source="update_watcher",
            severity=Severity.INFO,
            dedup_key=dedup_key,
            catalog_event=catalog_event,
            payload=payload,
        )
    except Exception as exc:
        print(f"[update_watcher] dispatcher.send raised; "
              f"alert dropped: {exc}", file=sys.stderr)
        return False

    return outcome.result == DispatchResult.SENT


# ── Checks ─────────────────────────────────────────────────────────────────


def check_openclaw_update(
    state: dict[str, Any],
    network: dict,
    shared_dir: Path,
) -> str | None:
    """Compare installed openclaw to latest published; emit if newer.

    Runs the safe-upgrade preflight to pick the right catalog event:
    ``updates.openclaw_available`` when it passes, ``updates.openclaw_blocked``
    when blockers are found. The persisted report under
    ``{shared_dir}/safe-upgrade/reports/`` doubles as the Overview tile's
    cached verdict — no extra round-trip needed when the operator opens
    the UI right after seeing the alert.

    Re-notifies when the safety verdict flips for the same version
    (e.g. operator resolved a blocker, version went from blocked → safe).

    Returns the new version string if a notification was emitted,
    otherwise None. Mutates ``state`` in place.
    """
    current = read_installed_openclaw_version()
    latest, status = resolve_latest_openclaw_version()

    if not current or not latest:
        # Can't compare — skip silently. Daily tick will retry.
        return None
    if not is_newer_version(latest, current):
        return None

    # Channel + git-behind context from OC's own CLI, when we had it. Logged
    # (not in the chat body — the catalog template owns that) so the daemon
    # log carries the richer picture for free.
    if status:
        avail = status.get("availability") or {}
        chan = status.get("channel") or {}
        print(f"[update_watcher] oc update status: channel={chan.get('value')!r} "
              f"src={chan.get('source')!r} gitBehind={avail.get('gitBehind')} "
              f"hasGitUpdate={avail.get('hasGitUpdate')}")

    preflight = _run_preflight_safe(latest, network=network, shared_dir=shared_dir)
    # Preflight failure (import error, gate exception, unreachable
    # registry from the gate path) is treated as "unknown" — we fall
    # back to the bare available alert so the operator still hears
    # about the version, just without a verdict.
    if preflight is None or preflight.get("ok"):
        catalog_event = "updates.openclaw_available"
        verdict = "safe"
        payload = {"new_version": latest, "current_version": current}
    else:
        catalog_event = "updates.openclaw_blocked"
        verdict = "blocked"
        blockers = [r["summary"] for r in preflight.get("requirements", [])
                    if r.get("blocking")]
        payload = {
            "new_version": latest,
            "current_version": current,
            "blocker_summary": _format_blockers(blockers),
        }

    # Already announced this version+verdict combo? Skip. The verdict
    # is part of the key so a blocked → safe transition (operator
    # fixed something) still re-fires.
    last_key = state.get("openclaw_last_notified_key")
    this_key = f"{latest}:{verdict}"
    if last_key == this_key:
        return None

    sent = _dispatch_update(
        shared_dir=shared_dir, network=network,
        dedup_key=f"update_watcher/openclaw/{latest}/{verdict}",
        catalog_event=catalog_event,
        payload=payload,
    )
    if sent:
        state["openclaw_last_notified_key"] = this_key
        state["openclaw_last_notified_version"] = latest  # legacy field
        return latest
    return None


def _run_preflight_safe(
    target_version: str,
    *,
    network: dict,
    shared_dir: Path,
) -> dict | None:
    """Run safe-upgrade preflight; return the report dict or None on failure.

    Persists the report so the admin UI's Overview tile + Maintenance →
    OC Version page see a fresh verdict alongside the alert.
    """
    try:
        from evolve_admin.safe_upgrade import run_preflight
    except Exception as exc:
        print(f"[update_watcher] safe-upgrade import failed; "
              f"falling back to bare alert: {exc}", file=sys.stderr)
        return None
    try:
        report = run_preflight(
            target_version, network=network, shared_dir=shared_dir, persist=True,
        )
    except Exception as exc:
        print(f"[update_watcher] safe-upgrade preflight raised; "
              f"falling back to bare alert: {exc}", file=sys.stderr)
        return None
    return report.to_json()


def _format_blockers(blockers: list[str]) -> str:
    """Render up to 3 blocker summaries as a bulleted list."""
    if not blockers:
        return "(no detail available)"
    head = blockers[:3]
    rest = len(blockers) - 3
    out = "Blockers:\n" + "\n".join(f"  • {b}" for b in head)
    if rest > 0:
        out += f"\n  …and {rest} more"
    return out


# ── OpenClaw surface drift-diff ──────────────────────────────────────────────
#
# Detecting *that* a new OC version exists (above) never told us *what changed*
# in it — which is how a breaking auth-store migration slipped through a
# routine bump. This check snapshots the candidate's surface (from the same
# tarball safe_upgrade fetches), diffs it against the installed OC, cross-
# references the changed surfaces against Evolve's declared OC dependencies
# (oc_deps, when it exists), writes the full diff as a dev-facing artifact, and
# emits an actionable Signal. Spec context: internal/openclaw-coverage-audit-
# 2026-06-04.md F1/F2.

SURFACE_DRIFT_PRODUCER = "oc_surface_drift"
SURFACE_DRIFT_SIGNAL_TYPE = "updates.openclaw_surface_drift"


def _surface_diffs_dir(shared_dir: Path) -> Path:
    return shared_dir / "update_watcher" / "oc-surface-diffs"


def _write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup of the temp file; the original error re-raises.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _import_surface_tools():
    """Lazy-import the admin-side surface library + tarball fetch. Returns
    ``(oc_surface, fetch_registry_metadata, download_candidate_tarball)`` or
    None when the admin package isn't importable (analyzer degrades)."""
    try:
        from evolve_admin import oc_surface  # pyright: ignore[reportMissingImports]
        from evolve_admin.safe_upgrade import (  # pyright: ignore[reportMissingImports]
            _fetch_registry_metadata, download_candidate_tarball,
        )
    except Exception as exc:
        print(f"[update_watcher] surface tools unavailable; "
              f"skipping drift-diff: {exc}", file=sys.stderr)
        return None
    return oc_surface, _fetch_registry_metadata, download_candidate_tarball


def _import_signal_store():
    """Lazy-import signals.store + schema.signal. Returns ``(store, schema)``
    or ``(None, None)`` when unavailable (drift-diff still writes the artifact;
    just no Signal)."""
    try:
        import importlib
        store = importlib.import_module("signals.store")
        schema = importlib.import_module("schema.signal")
        return store, schema
    except Exception as exc:
        print(f"[update_watcher] signal store unavailable; "
              f"surface drift recorded to disk only: {exc}", file=sys.stderr)
        return None, None


def _emit_surface_drift_signal(
    *,
    shared_dir: Path,
    current: str,
    candidate: str,
    diff: dict[str, Any],
    cross_ref: dict[str, Any],
    summary: str,
    artifact_path: Path,
) -> bool:
    """Emit the surface-drift Signal. WARNING when Evolve-depended surfaces
    changed, INFO otherwise. Returns True iff a Signal was written."""
    store, schema = _import_signal_store()
    if store is None or schema is None:
        return False

    depended = cross_ref.get("depended_changed") or []
    severity = "warn" if depended else "info"
    # Match the model_discovery pattern: the producer default is info/activity;
    # a depended-surface change is a real fix-it task → maintenance flavor.
    flavor = "maintenance" if depended else "activity"

    title = f"OpenClaw {candidate}: surface drift vs installed {current}"
    body_lines = [summary]
    if depended:
        body_lines.append(
            f"Evolve depends on {len(depended)} changed surface(s): "
            + ", ".join(depended[:8])
        )
    body_lines.append(f"Full diff: {artifact_path}")
    body = "\n".join(body_lines)

    try:
        signature = schema.make_signature(
            SURFACE_DRIFT_PRODUCER, SURFACE_DRIFT_SIGNAL_TYPE, candidate,
        )
        store.observe(
            shared_dir,
            signature=signature,
            producer=SURFACE_DRIFT_PRODUCER,
            type=SURFACE_DRIFT_SIGNAL_TYPE,
            severity=severity,
            flavor=flavor,
            scope="pod",
            title=title,
            body=body,
            details={
                "current_version": current,
                "candidate_version": candidate,
                "summary": summary,
                "changed_surface_items": diff.get("changed_surface_items", []),
                "depended_changed": depended,
                "oc_deps_available": cross_ref.get("available", False),
                "artifact_path": str(artifact_path),
            },
        )
        # Archive any drift Signal left over for a *different* candidate (the
        # operator moved the target) — keep only this candidate's.
        store.sweep_resolve(
            shared_dir,
            producer=SURFACE_DRIFT_PRODUCER,
            kept_signatures={signature},
            reason="superseded by a newer surface-drift review",
        )
        return True
    except Exception as exc:
        print(f"[update_watcher] surface drift signal emit failed: {exc}",
              file=sys.stderr)
        return False


def check_openclaw_surface_drift(
    state: dict[str, Any],
    network: dict,
    shared_dir: Path,
) -> str | None:
    """Snapshot the candidate OC's surface, diff it against the installed OC,
    and emit a Signal when surfaces drift.

    Returns the candidate version string when a Signal was emitted, else None.
    Mutates ``state`` in place (records the last candidate we diffed so we
    don't re-download the tarball every daily tick for a pending upgrade).
    """
    tools = _import_surface_tools()
    if tools is None:
        return None
    ocs, fetch_registry_metadata, download_candidate_tarball = tools

    current = read_installed_openclaw_version()
    installed_root = ocs.installed_oc_root()
    if not current or installed_root is None:
        return None

    candidate, _status = resolve_latest_openclaw_version()
    if not candidate:
        return None

    if not is_newer_version(candidate, current):
        # Up to date — archive any lingering drift review (the upgrade that
        # review warned about has happened or is moot).
        store, _schema = _import_signal_store()
        if store is not None:
            try:
                store.sweep_resolve(
                    shared_dir, producer=SURFACE_DRIFT_PRODUCER,
                    kept_signatures=set(),
                    reason="installed OC caught up; drift review moot",
                )
            except Exception as exc:
                print(f"[update_watcher] surface drift sweep_resolve failed: {exc}",
                      file=sys.stderr)
        return None

    # Already reviewed this exact candidate? Don't re-download daily.
    if state.get("surface_last_diffed_candidate") == candidate:
        return None

    try:
        metadata = fetch_registry_metadata(candidate)
    except Exception as exc:
        print(f"[update_watcher] surface drift: registry fetch failed: {exc}",
              file=sys.stderr)
        return None

    data, err = download_candidate_tarball(metadata)
    if err or not data:
        print(f"[update_watcher] surface drift: tarball fetch failed: {err}",
              file=sys.stderr)
        return None

    installed_snap = ocs.build_snapshot(
        ocs.DirSource(installed_root), version=current, cli_path=_find_openclaw_cli(),
    )
    cand_source = ocs.TarballSource(data)
    try:
        candidate_snap = ocs.build_snapshot(cand_source, version=candidate)
    finally:
        cand_source.close()

    diff = ocs.diff_snapshots(installed_snap, candidate_snap)
    cross_ref = ocs.cross_reference_dependencies(diff)
    summary = ocs.summarize_diff(diff, cross_ref)

    if not diff.get("any_change"):
        # Reviewed, no drift — record the candidate so the tarball isn't
        # re-fetched every tick while this upgrade stays pending. (Nothing
        # to emit, so there's no notification to lose by committing here.)
        state["surface_last_diffed_candidate"] = candidate
        print(f"[update_watcher] surface drift: {candidate} vs {current} — "
              f"no surface changes")
        return None

    # Dev-facing artifact: the full diff + the cross-reference + both
    # snapshots, for whoever has to decide whether Evolve needs updating.
    # Sanitize the (upstream-derived) version before it becomes a filename so
    # an odd version string can't escape the diffs directory.
    safe_version = "".join(
        c if (c.isalnum() or c in "._-") else "_" for c in candidate
    ) or "unknown"
    artifact_path = _surface_diffs_dir(shared_dir) / f"{safe_version}.json"
    try:
        _write_json_atomic(artifact_path, {
            "diff": diff,
            "dependency_cross_reference": cross_ref,
            "summary": summary,
            "installed_snapshot": installed_snap,
            "candidate_snapshot": candidate_snap,
        })
    except Exception as exc:
        print(f"[update_watcher] surface drift: could not write artifact: {exc}",
              file=sys.stderr)

    emitted = _emit_surface_drift_signal(
        shared_dir=shared_dir, current=current, candidate=candidate,
        diff=diff, cross_ref=cross_ref, summary=summary,
        artifact_path=artifact_path,
    )
    if emitted:
        # Commit the dedup only after a successful emit — a transient signal-
        # store failure must leave the candidate un-recorded so the next tick
        # retries rather than silently swallowing the drift review.
        state["surface_last_diffed_candidate"] = candidate
        return candidate
    return None


def check_evolve_repo_update(
    state: dict[str, Any],
    network: dict,
    shared_dir: Path,
    *,
    repo: Path = _DEPLOY_REPO,
) -> str | None:
    """Detect new commits on origin/main of the deploy checkout; emit
    via dispatcher. Returns the new HEAD SHA if a notification was
    emitted, otherwise None. Mutates ``state`` in place.

    Default behavior depends on the operator's subscription preference
    for updates.evolve_repo (default off — opt-in via UI).
    """
    head = git_ls_remote_head(repo, "main")
    if not head:
        return None
    last = state.get("evolve_repo_last_notified_sha")
    if last == head:
        return None

    if last:
        n = git_count_commits_between(repo, last, head)
        if n is None:
            summary = "new updates available"
        elif n == 0:
            # Edge case: ls-remote returned a head we've already passed.
            # Don't re-notify.
            state["evolve_repo_last_notified_sha"] = head
            return None
        else:
            summary = f"{n} new update{'s' if n != 1 else ''} since the last check"
    else:
        summary = "new updates available"

    # Phase F: pass structured payload — catalog body_template renders.
    sent = _dispatch_update(
        shared_dir=shared_dir, network=network,
        dedup_key=f"update_watcher/evolve_repo/{head[:16]}",
        catalog_event="updates.evolve_repo",
        payload={"commits_summary": summary},
    )
    if sent:
        state["evolve_repo_last_notified_sha"] = head
        return head
    return None


# ── Tracked upstream issues ─────────────────────────────────────────────────

# Default seed of upstream GitHub issues to poll. Operators can override by
# writing {shared_dir}/update_watcher/tracked_upstream_issues.json with their
# own list (same shape: `{"issues": [{repo, number, hint}, ...]}`); when that
# file exists we use it verbatim, when it doesn't we fall back here.
#
# Pin issues whose closure unblocks a workflow on this pod — we don't want
# update_watcher to become a generic GitHub-issue inbox. One issue per real
# blocker is the right granularity.
_DEFAULT_TRACKED_ISSUES: list[dict[str, Any]] = [
    {
        "repo": "openclaw/openclaw",
        "number": 82301,
        "hint": (
            "stock→externalized plugin migration is currently unblockable on "
            "5.12 — when this closes, the 4.29→5.12 upgrade may no longer "
            "require the manual config-neutralize-install-restore dance. "
            "Re-check the safe-upgrade preflight."
        ),
    },
]


def _tracked_issues_config_path(shared_dir: Path) -> Path:
    return shared_dir / "update_watcher" / "tracked_upstream_issues.json"


def _load_tracked_issues(shared_dir: Path) -> list[dict[str, Any]]:
    """Read the operator-overridable tracked-issues list, or fall back to
    the embedded default seed."""
    p = _tracked_issues_config_path(shared_dir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return list(_DEFAULT_TRACKED_ISSUES)
    issues = data.get("issues") if isinstance(data, dict) else None
    return list(issues) if isinstance(issues, list) else list(_DEFAULT_TRACKED_ISSUES)


def _fetch_github_issue(repo: str, number: int, timeout: int = 10) -> dict[str, Any] | None:
    """Fetch a single GitHub issue's state via the public REST API.

    Public API: no auth required for read-only access to public repos,
    rate-limited to 60 req/hr per IP. Daily polls of a handful of issues
    stay well under that ceiling.

    Returns the parsed issue dict (with `state`, `title`, `closed_at`,
    `html_url`, etc.) or None on any network/parse error — caller treats
    a None as "skip this tick, retry tomorrow".
    """
    url = f"https://api.github.com/repos/{repo}/issues/{number}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "evolve-update-watcher/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        return json.loads(body)
    except (urllib.error.URLError, urllib.error.HTTPError,
            json.JSONDecodeError, OSError, ValueError):
        return None


# Window in which a just-closed issue, seen for the first time, still
# warrants a notification rather than being silently recorded. Tuned so
# weekend-delayed polls don't drop a Monday-morning close, but long-
# stale closures (operator added a years-old issue to their tracker)
# don't spam.
_FRESH_CLOSURE_WINDOW_SECONDS = 24 * 60 * 60


def _closed_within_fresh_window(issue: dict[str, Any]) -> bool:
    """Return True iff ``issue['closed_at']`` is parseable and within the
    last ``_FRESH_CLOSURE_WINDOW_SECONDS``. Conservative on uncertainty:
    missing/malformed timestamps → False (don't notify), so a
    misbehaving GitHub response can't spuriously trigger an alert.
    """
    raw = issue.get("closed_at")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        # GitHub returns ISO8601 with trailing 'Z'. fromisoformat handles
        # 'Z' since Python 3.11; on 3.10 we normalize first for safety.
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - ts).total_seconds() <= _FRESH_CLOSURE_WINDOW_SECONDS


def check_tracked_upstream_issues(
    state: dict[str, Any],
    network: dict,
    shared_dir: Path,
) -> list[str]:
    """Poll each configured issue; emit a Signal when one transitions
    open → closed since the last tick.

    Tracking state lives at state['tracked_upstream_issues'] as
    `{<repo>#<number>: <last_seen_state>}`. First time we see an issue
    we record its state but don't notify (avoids a spurious "closed"
    on initial seed of an already-closed issue).

    Returns the list of `<repo>#<number>` keys that we notified on this
    tick (for the caller to log). Mutates ``state`` in place.
    """
    tracked = _load_tracked_issues(shared_dir)
    if not tracked:
        return []

    bucket = state.setdefault("tracked_upstream_issues", {})
    notified: list[str] = []

    for entry in tracked:
        repo = entry.get("repo")
        number = entry.get("number")
        if not isinstance(repo, str) or not isinstance(number, int):
            continue
        key = f"{repo}#{number}"
        hint = entry.get("hint", "")

        issue = _fetch_github_issue(repo, number)
        if issue is None:
            # Network or API error — leave prior state untouched so the
            # next tick can retry. Don't notify on a fetch failure.
            continue

        new_state = issue.get("state")  # "open" or "closed"
        if not isinstance(new_state, str):
            continue

        prior = bucket.get(key)
        bucket[key] = new_state  # always refresh — captures initial seed too

        if prior is None:
            # First observation of this issue. Default behavior: silently
            # record state, don't notify retroactively (would spam stale
            # closures whenever an operator adds a long-resolved issue to
            # their tracked list).
            #
            # Exception: if the issue is "closed" AND closed within the
            # FRESH_CLOSURE_WINDOW (24h), notify anyway. This catches the
            # case where update_watcher's first poll runs AFTER the issue
            # closes — e.g. issue filed Mon, tracker deployed Mon evening,
            # issue closes Tue morning, first poll Tue afternoon → without
            # this branch, the close-notification is silently lost.
            if new_state == "closed" and _closed_within_fresh_window(issue):
                pass  # fall through to the notify block below
            else:
                continue
        elif prior == new_state:
            continue
        if (prior == "open" or prior is None) and new_state == "closed":
            payload = {
                "repo": repo,
                "number": number,
                "title": issue.get("title", "(no title)"),
                "hint": hint,
                "closed_at": issue.get("closed_at", "(unknown)"),
                "url": issue.get("html_url", f"https://github.com/{repo}/issues/{number}"),
            }
            sent = _dispatch_update(
                shared_dir=shared_dir, network=network,
                dedup_key=f"update_watcher/upstream_issue/{repo}/{number}/closed",
                catalog_event="updates.upstream_issue_resolved",
                payload=payload,
            )
            if sent:
                notified.append(key)
        # Other transitions (closed → open = reopened) we silently track
        # for next time but don't notify on. Closure is the interesting
        # signal for operator action.

    return notified


# ── Main ───────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network", default=None,
        help="Path to network.json (defaults to evolve_config.resolve_network_path).",
    )
    parser.add_argument(
        "--check", default="all",
        choices=("all", "openclaw", "surface", "evolve_repo", "upstream_issues"),
        help="Run only one check (default: all).",
    )
    args = parser.parse_args(argv)

    config = load_config(args.network)
    shared_dir = Path(get_shared_dir(config))
    state = _load_state(shared_dir)

    if args.check in ("all", "openclaw"):
        try:
            sent = check_openclaw_update(state, config, shared_dir)
            if sent:
                print(f"[update_watcher] openclaw notification sent ({sent})")
            else:
                current = read_installed_openclaw_version() or "?"
                print(f"[update_watcher] openclaw up to date ({current})")
        except Exception as exc:
            print(f"[update_watcher] openclaw check failed: {exc}",
                  file=sys.stderr)

    if args.check in ("all", "surface"):
        try:
            drifted = check_openclaw_surface_drift(state, config, shared_dir)
            if drifted:
                print(f"[update_watcher] openclaw surface drift signal sent ({drifted})")
        except Exception as exc:
            print(f"[update_watcher] surface drift check failed: {exc}",
                  file=sys.stderr)

    if args.check in ("all", "evolve_repo"):
        try:
            sent = check_evolve_repo_update(state, config, shared_dir)
            if sent:
                print(f"[update_watcher] evolve_repo notification sent ({sent[:8]})")
        except Exception as exc:
            print(f"[update_watcher] evolve_repo check failed: {exc}",
                  file=sys.stderr)

    if args.check in ("all", "upstream_issues"):
        try:
            notified = check_tracked_upstream_issues(state, config, shared_dir)
            if notified:
                print(f"[update_watcher] upstream issue(s) closed: {', '.join(notified)}")
            else:
                tracked = _load_tracked_issues(shared_dir)
                print(f"[update_watcher] tracking {len(tracked)} upstream issue(s); none changed")
        except Exception as exc:
            print(f"[update_watcher] upstream_issues check failed: {exc}",
                  file=sys.stderr)

    _save_state(shared_dir, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
