"""Auto-pull the deployed evolve-repo on every install.

Background:
    The deploy checkout (`/Users/Shared/evolve-repo` on macOS,
    `/var/lib/evolve/repo` on Linux — platform-keyed via
    `platform_profile.deploy_checkout_default`, see DEFAULT_REPO below) is
    the source of truth that every deployed daemon (heal, apply, verify,
    admin-ui, etc.) loads code from. Every PR merge to origin/main needs to
    be pulled before those daemons see the new code. On a single-box /
    tarball-staged pod the checkout has no `.git`; the puller detects this
    and no-ops cleanly (see PullResult.skipped_not_git) rather than wedging.

    Pre-2026-04-29: this was manual. Operator (or a session like
    today's) had to `ssh mini 'git pull'` after every merge. Drift
    accumulated between sessions. Today's V3 PermissionError finding
    was the third occurrence of "mini is N commits behind, downstream
    catalog item fails" pattern.

    This module is the durable fix: a small, testable pull function
    wired to a LaunchDaemon (`ai.evolve.evolve.repo-puller`) that
    fires every 15min as the `evolve` user. Lives in code so every
    Evolve install gets it, not just one mini.

Design rules:
    - Use `--ff-only` so we never silently overwrite a local commit
      (mini SHOULD never have local commits beyond worker auto-commits
      that came through origin, but `--ff-only` protects against
      operator mistakes).
    - Set `safe.directory` so git doesn't refuse to operate on a repo
      owned by a different user than the invoker (the evolve user
      may not own the repo on every install).
    - Log only on state change (HEAD advanced or error). A pull every
      15min that does nothing fills logs with noise; quiet on no-op
      keeps them readable.
    - Never raise to the caller; subprocess errors get returned in
      the result struct so the LaunchDaemon plist sees clean exit
      codes (errors → exit 1, success → exit 0).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os as _os
import re as _re
import shlex
import subprocess
import sys as _sys
import time as _time
from dataclasses import dataclass, field
from pathlib import Path

from .runtime import (
    JobSpec,
    get_scheduler,
    render_launchd_plist,
)
from platform_profile import get_profile as _get_profile, is_within as _is_within

# Platform-keyed at import: macOS resolves the /Users/Shared layout
# byte-identically; a Linux host resolves /var/lib/evolve, so the puller's
# repo/shared defaults carry no /Users leak (W10-E — the live VPS install hit
# "repo missing" because these were hardcoded to /Users/Shared/evolve-repo).
# get_profile() reads the real sys.platform in production daemons and the
# conftest-pinned MACOS profile under tests, preserving macOS test behavior.
_PROFILE = _get_profile()

DEFAULT_REPO = Path(_PROFILE.deploy_checkout_default)
DEFAULT_SHARED_DIR = Path(_PROFILE.shared_dir_default)
DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"
DEFAULT_QUARANTINE = Path(str(DEFAULT_SHARED_DIR) + "-quarantine")
DEFAULT_LAUNCHD_DIR = Path("/Library/LaunchDaemons")
# Where the puller files its own machine-generated incident records
# (puller-stuck markdown). Deliberately OUTSIDE the deploy checkout: the
# puller used to write these to `<repo>/issues/open/`, and the untracked
# files could wedge the very `git pull --ff-only` they were documenting
# (see docs/incidents/README.md). NOT `{shared_dir}/incidents/` either —
# that dir belongs to the heal daemon (date-named subdirs of per-bot
# JSON), and heal's restart-cooldown check assumes the lexicographically
# last two entries are day dirs. Records worth keeping get curated into
# the source repo's `docs/incidents/` via PR.
DEFAULT_INCIDENTS_DIR = DEFAULT_SHARED_DIR / "repo-puller" / "incidents"

# Stagger between successive bot-gateway kickstarts after a plugin rebuild.
# Each gateway is a fresh node process that opens connections to its
# messaging integration on boot; restarting 6+ simultaneously briefly
# thunders the pod's network/disk. 2s is enough to spread the load, small
# enough that a 6-bot pod adds ~10s to the puller tick (well under the
# 15-min cadence).
DEFAULT_GATEWAY_KICKSTART_STAGGER_SECONDS = 2.0

# Phrases git emits when untracked working-tree files would be clobbered
# by a fast-forward merge. Two known variants across git versions:
#   "The following untracked working tree files would be overwritten by merge:"
#   "would be overwritten by checkout"
# Match the load-bearing substring rather than the full sentence.
_UNTRACKED_CONFLICT_MARKER = "untracked working tree files would be overwritten"


@dataclass
class PullResult:
    """Outcome of a single pull attempt.

    `head_before` and `head_after` are full SHAs. If they differ, the
    pull advanced the working tree; the diff between them is what
    landed. `commits_advanced` is the count for quick log lines.

    `error` is set ONLY on failure (non-zero exit from any git
    invocation). `success` is True iff the pull completed (whether
    or not HEAD moved).

    `quarantined`/`deleted_identical` capture the side-effect of the
    untracked-file conflict recovery path (see `_handle_untracked_conflict`):
    files git refused to overwrite that we either deleted (because they
    were byte-identical to the version on origin) or moved to the
    quarantine dir for forensics (because they diverged).

    `plugin_rebuilt` indicates whether the puller rebuilt + restaged the
    OpenClaw plugin after detecting `packages/plugin/` paths in the pulled
    diff. `plugin_rebuild_error` captures any failure from that step;
    plugin failures don't fail the overall pull (HEAD has already advanced),
    but the operator needs to see them.

    `restarted_daemons` lists the LaunchDaemon labels the puller
    successfully kickstarted after the pull (because the pulled diff
    touched code those daemons load). `restart_errors` maps a label to
    the failure detail when kickstart returned non-zero — restart
    failures never fail the pull (HEAD has already advanced); the
    operator sees them in the puller log and can re-run manually.
    `restart_warnings` collects soft-skip messages (e.g. "repo_puller.py
    changed; skipping self-restart"). `restart_skipped_disabled` is True
    when the kill-switch env var was set and we'd have otherwise
    restarted at least one daemon.

    `bot_gateways_restarted` / `bot_gateway_restart_errors` mirror the
    above but for the OpenClaw bot gateways (``ai.openclaw.<bot>-gateway``)
    that load the plugin at process start. Populated only when a plugin
    rebuild actually succeeded this tick — without it, the staged dist
    hasn't changed and a restart is pointless. Failures here never fail
    the pull. `bot_gateway_discovery_error` is set when the launchd-dir
    scan itself failed (rare; permissions or missing dir).
    """
    success: bool
    head_before: str = ""
    head_after: str = ""
    commits_advanced: int = 0
    log_summary: str = ""
    error: str = ""
    steps: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    deleted_identical: list[str] = field(default_factory=list)
    quarantine_dir: str = ""
    plugin_rebuilt: bool = False
    plugin_rebuild_error: str = ""
    restarted_daemons: list[str] = field(default_factory=list)
    restart_errors: dict[str, str] = field(default_factory=dict)
    restart_warnings: list[str] = field(default_factory=list)
    restart_skipped_disabled: bool = False
    bot_gateways_restarted: list[str] = field(default_factory=list)
    bot_gateway_restart_errors: dict[str, str] = field(default_factory=dict)
    bot_gateway_discovery_error: str = ""
    # Auto-install of new infra plists (cost_watchdog, embedding_monitor, etc.)
    # — populated when the pulled diff touches deploy.py.
    infra_jobs_installed: list[str] = field(default_factory=list)
    infra_jobs_install_error: str = ""
    # Charter fingerprint bump — populated when the pulled diff touches a
    # generator charter.yaml.  Count is generators updated; error is non-empty
    # only when the bump step itself threw; bump failures never fail the pull.
    charter_fingerprints_bumped: int = 0
    charter_fingerprint_bump_error: str = ""
    # pip install -e packages/admin — populated when the pulled diff touches
    # packages/admin/pyproject.toml. New dependencies in pyproject don't auto-
    # install with editable mode; without this hook the new module imports
    # at runtime fail with ModuleNotFoundError. Failures here don't fail the
    # overall pull (HEAD has already advanced).
    pip_install_attempted: bool = False
    pip_install_ok: bool = False
    pip_install_info: str = ""
    # /etc/sudoers.d/evolve DRIFT detection. The puller can't install sudoers
    # (Option B, #2759 — evolve must not rewrite its own sudoers), so it only
    # detects whether the rendered template is installed and fires/resolves the
    # sudoers_refresh_failed Signal. ``sudoers_refresh_attempted`` = the drift
    # check ran; ``sudoers_refresh_ok`` = in sync (no dormant grants). Never
    # fails the pull. Operator applies grants with `sudo evolve-admin
    # refresh-sudoers` as root.
    sudoers_refresh_attempted: bool = False
    sudoers_refresh_ok: bool = False
    sudoers_refresh_info: str = ""
    # Post-pull openclaw.json validation — populated by the
    # `openclaw_config_validator` hook. The hook runs every pull (not gated
    # on the pulled diff), since the trigger condition is "schema changed
    # vs. existing on-disk config" and the schema can have moved without
    # the puller seeing the source file change locally (e.g. plugin was
    # rebuilt last pull). Per-bot Signal emission is handled inside the
    # hook; this field is for the operator-facing log line.
    openclaw_invalid_bots: list[str] = field(default_factory=list)
    # Lagging-bot redeploy sweep — populated whenever HEAD advanced. The
    # version string EVOLVE_VERSION embeds the head commit's PR number,
    # so every successful pull leaves bots stamped at the prior version
    # in install.json's bot_versions{}. Without an auto-redeploy of those
    # lagging bots, deploy_drift_monitor fires for every merged PR (the
    # 8-bot drift signal in the 2026-06-03 Alerts review). Redeploy is
    # best-effort: failures here don't fail the pull.
    lagging_bots_redeployed: list[str] = field(default_factory=list)
    lagging_bot_deploy_errors: dict[str, str] = field(default_factory=dict)
    # Builtin-Spec re-seed sweep — populated every tick. Repo gallery-package
    # edits don't reach a deployed pod's bound builtin Specs on their own (a
    # gallery install binds the pre-existing builtin and never re-reads the
    # repo package), so a bumped pkg_version is re-seeded into the builtin tier
    # here. `gallery_specs_reseeded` lists the spec_ids regenerated this tick;
    # `gallery_reseed_error` is the sweep-level failure detail (per-package
    # errors never fail the pull). See migrate_v7.reseed_builtin_specs +
    # docs/decision-add-bot-m4-u1-proof-2026-06-11.md (#2792).
    gallery_specs_reseeded: list[str] = field(default_factory=list)
    gallery_reseed_error: str = ""
    # Single-box / tarball-staged pod: the deploy checkout exists but is NOT a
    # git working tree (no `.git`), so there is nothing to `git pull`. Set when
    # the puller deliberately no-ops on such a checkout — `success` stays True
    # (a clean idle, not a wedge) and the daemon exits 0. Updates on these pods
    # land via re-staging, not the puller. See docs/runbook-vps-pod-provision.md.
    skipped_not_git: bool = False


def _git(repo: Path, args: list[str]) -> tuple[int, str, str]:
    """Run a git command in `repo` with safe.directory set.

    safe.directory bypasses git's CVE-2022-24765 protection that
    refuses operations on repos owned by a different user. The
    LaunchDaemon runs as `evolve` but the repo is sometimes owned
    by the operator who initially cloned it. This is safe because
    we explicitly target a single repo path; we're not trusting
    arbitrary user-owned repos.
    """
    cmd = ["git", "-c", f"safe.directory={repo}", "-C", str(repo)] + args
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def pin_filemode_off_if_nested(
    repo: Path, shared_dir: Path, *, sudo_evolve: bool = False
) -> tuple[bool, str]:
    """Defensively pin ``core.fileMode=false`` on a deploy checkout that is
    nested under *shared_dir* (the Linux layout ``/var/lib/evolve/repo``).

    Belt-and-suspenders for the 2026-06-23 freeze: a recursive perms pass over
    shared_dir that descends into the checkout flips tracked files
    100644→100755; with ``core.fileMode=true`` git reports them modified and
    ``git pull --ff-only`` refuses, freezing the pod on stale code. With
    ``core.fileMode=false`` git ignores exec-bit churn, so no perms pass can
    wedge the pull — independent of whether the pruning fix (1) holds.

    Scoped to the nested layout via a structural containment check — NOT a
    ``sys.platform`` branch — so the macOS deploy checkout (a SIBLING of
    shared_dir) is left exactly as it is (byte-identical). Idempotent: a no-op
    once the config is already ``false``.

    *sudo_evolve* writes the config as the ``evolve`` user (the setup path,
    where the caller may be the operator); the per-tick puller runs as evolve
    already and writes directly. Returns ``(ok, message)``."""
    if not _is_within(repo, shared_dir):
        return True, "skip core.fileMode: deploy checkout not nested under shared_dir (sibling layout)"
    if not (repo / ".git").exists():
        return True, "skip core.fileMode: not a git working tree (tarball-staged)"
    rc, current, _ = _git(repo, ["config", "core.fileMode"])
    if rc == 0 and current.strip() == "false":
        return True, "core.fileMode already false"
    if sudo_evolve:
        r = subprocess.run(  # sudo-grant: root-only — sudo_evolve branch runs in operator-root context, dropping TO evolve
            ["sudo", "-u", "evolve", "git", "-c", f"safe.directory={repo}",
             "-C", str(repo), "config", "core.fileMode", "false"],
            capture_output=True, text=True, timeout=15,
        )
        rc2, err = r.returncode, (r.stderr or r.stdout)
    else:
        rc2, _, err = _git(repo, ["config", "core.fileMode", "false"])
    if rc2 != 0:
        return False, f"git config core.fileMode false failed (rc={rc2}): {err.strip()[:200]}"
    return True, "set core.fileMode=false"


def _parse_untracked_conflict_files(err: str) -> list[str]:
    """Pull the offending file paths out of git's untracked-conflict error.

    Git's stderr on this failure shape looks like (note the tab indent on
    each path — git always uses tabs here):

        error: The following untracked working tree files would be
        overwritten by merge:
            packages/analyzer/extract_tuples.py
            packages/analyzer/observations/llm_extractor.py
        Please move or remove them before you merge.
        Aborting

    Take every tab-indented line between the marker line and the next
    non-tab line. Empty list means we couldn't parse — caller falls back
    to the normal failure path rather than guess.
    """
    files: list[str] = []
    in_block = False
    for line in err.splitlines():
        if _UNTRACKED_CONFLICT_MARKER in line:
            in_block = True
            continue
        if not in_block:
            continue
        if line.startswith("\t"):
            files.append(line[1:].strip())
            continue
        # First non-tab line ends the block ("Please move or remove..." /
        # "Aborting" / blank).
        if files:
            break
    return files


def _handle_untracked_conflict(
    repo: Path,
    remote: str,
    branch: str,
    err: str,
    result: PullResult,
    quarantine_root: Path,
    now: "_dt.datetime | None" = None,
) -> bool:
    """If the pull failed because of untracked working-tree files, sweep
    them aside and retry. Returns True iff the retry succeeded — caller
    should treat that as the new authoritative outcome.

    Per-file decision:
      - If the file is byte-identical to ``<remote>/<branch>:<path>``, just
        delete it. No information lost; pull was going to write that exact
        content anyway.
      - Otherwise, move it to ``<quarantine_root>/<utc-iso>/<relpath>``.
        The diverged content is preserved for the operator to inspect
        without blocking the next pull cycle.

    Mutates ``result`` so the caller's log line / issue body include what
    happened. Does NOT raise — any IO failure during cleanup leaves the
    result as-failed and lets the standard wedge-issue path take over."""
    if _UNTRACKED_CONFLICT_MARKER not in err:
        return False

    files = _parse_untracked_conflict_files(err)
    if not files:
        return False

    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    quarantine_dir = quarantine_root / stamp

    for rel in files:
        src = repo / rel
        if not src.exists():
            # Race: file is already gone (concurrent operator cleanup).
            # Treat as deleted-identical so we still retry the pull.
            result.deleted_identical.append(rel)
            continue
        # Compare against the version git is about to install. If
        # `git show` fails (file not on origin yet, e.g. branch-local
        # rename), fall through to the quarantine path.
        rc, remote_blob, _ = _git(
            repo, ["show", f"{remote}/{branch}:{rel}"]
        )
        try:
            local_text = src.read_text()
        except (OSError, UnicodeDecodeError):
            local_text = None
        if rc == 0 and local_text is not None and local_text == remote_blob:
            try:
                src.unlink()
                result.deleted_identical.append(rel)
                continue
            except OSError:
                pass   # fall through to quarantine
        try:
            dest = quarantine_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dest)
            result.quarantined.append(rel)
        except OSError as e:
            result.steps.append(
                f"FAIL: could not quarantine {rel}: {e}"
            )
            return False

    if result.quarantined:
        result.quarantine_dir = str(quarantine_dir)

    # Retry the pull now that the offending paths are out of the way.
    rc, _, retry_err = _git(repo, ["pull", "--ff-only", remote, branch])
    if rc != 0:
        result.steps.append(
            f"retry after sweep also failed: {retry_err.splitlines()[0] if retry_err else 'unknown'}"
        )
        return False

    summary_bits: list[str] = []
    if result.deleted_identical:
        summary_bits.append(
            f"deleted {len(result.deleted_identical)} identical-to-origin file(s)"
        )
    if result.quarantined:
        summary_bits.append(
            f"quarantined {len(result.quarantined)} divergent file(s) → {quarantine_dir}"
        )
    result.steps.append(
        "recovered untracked-conflict: " + "; ".join(summary_bits)
    )
    return True


def _pulled_paths(repo: Path, head_before: str, head_after: str) -> list[str]:
    """Return the list of paths changed between `head_before..head_after`.

    Empty list on git failure — the puller's downstream decisions
    (plugin rebuild, daemon restart) treat "couldn't determine the
    diff" as "nothing actionable" rather than crashing. The HEAD has
    already advanced; the operator can re-run manually if needed.
    """
    rc, out, _err = _git(repo, [
        "diff", "--name-only", f"{head_before}..{head_after}",
    ])
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _paths_touch_plugin(paths: list[str]) -> bool:
    """True iff any path in `paths` lives under `packages/plugin/`."""
    return any(p.startswith("packages/plugin/") for p in paths)


# Files whose changes mean install_evolve_infra_jobs should re-run after the
# pull: deploy.py owns the launchd plist installers + expected_plist_labels;
# applications/audit_scheduler.py owns the audit-scheduler plist installer
# that install_evolve_infra_jobs also calls. (Renamed from scheduler.py
# 2026-06-08 when the app-test surface was killed.)
_INFRA_INSTALL_PATHS: tuple[str, ...] = (
    "packages/admin/evolve_admin/deploy.py",
    "packages/admin/evolve_admin/applications/audit_scheduler.py",
)


def _paths_touch_infra_install(paths: list[str]) -> bool:
    """True iff any path in `paths` is one of the launchd-install source files."""
    return any(p in _INFRA_INSTALL_PATHS for p in paths)


def _paths_touch_charters(paths: list[str]) -> bool:
    """True iff any path in `paths` is a generator charter YAML."""
    return any(
        p.startswith("packages/analyzer/generators/") and p.endswith(("charter.yaml", "charter.yml"))
        for p in paths
    )


# Files whose changes mean the editable-installed evolve-admin venv must
# pip-install fresh: adding a new dependency in pyproject.toml lands the
# code change via git pull but the new module isn't in the venv's
# site-packages until pip runs. Before 2026-05-31 we hit this concretely
# with google-auth / google-api-python-client (PR #1862) — the code
# imported them fine in dev but the mini's MCP bridge crashed on first
# call. Editable-install metadata (the entry_points + console_scripts in
# pyproject.toml) needs a reinstall on those changes too, though that's
# rarer in practice.
_PYPROJECT_PATHS: tuple[str, ...] = (
    "packages/admin/pyproject.toml",
)


def _paths_touch_pyproject(paths: list[str]) -> bool:
    """True iff any path in `paths` is a pyproject.toml the venv tracks."""
    return any(p in _PYPROJECT_PATHS for p in paths)


# Files whose changes mean /etc/sudoers.d/evolve needs to be re-installed.
# _render_evolve_sudoers + _write_evolve_sudoers both live in setup_wizard.py;
# every PR that adds a new narrow sudo grant lands here, and a pulled diff
# touching this file means the on-disk sudoers may be missing grants the
# new code paths assume. Without an auto-refresh the operator has to
# remember `sudo evolve-admin refresh-sudoers` after every such merge.
# Pattern-match on the basename so a future refactor that moves the
# sudoers helpers into setup_wizard_sudoers.py (or similar) keeps tripping.
_SETUP_WIZARD_PATH_PREFIX = "packages/admin/evolve_admin/setup_wizard"


def _paths_touch_setup_wizard(paths: list[str]) -> bool:
    """True iff any path in `paths` is setup_wizard.py or a setup_wizard_*.py sibling.

    The trigger is intentionally broad: today the sudoers helpers live in
    setup_wizard.py, but renaming or splitting that module shouldn't
    silently disable the auto-refresh.
    """
    return any(
        p.startswith(_SETUP_WIZARD_PATH_PREFIX) and p.endswith(".py")
        for p in paths
    )


def _compute_charter_fingerprint(content: str) -> str:
    normalized = "\n".join(line.rstrip() for line in content.splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _bump_charter_fingerprints(repo: Path, shared_dir: Path) -> tuple[int, str]:
    """Re-sync stored GeneratorRecord fingerprints to match deployed charters.

    Iterates every charter.yaml under packages/analyzer/generators/, recomputes
    its fingerprint, and updates the matching record in {shared_dir}/generators/
    when they disagree.  Records with no on-disk file yet are skipped (the
    registry creates them on first load).  Returns (count_changed, error_string).
    """
    generators_dir = repo / "packages" / "analyzer" / "generators"
    records_dir = shared_dir / "generators"
    if not generators_dir.is_dir() or not records_dir.is_dir():
        return 0, ""

    changed = 0
    try:
        for entry in sorted(generators_dir.iterdir()):
            if not entry.is_dir():
                continue
            charter_path = entry / "charter.yaml"
            if not charter_path.exists():
                charter_path = entry / "charter.yml"
            if not charter_path.exists():
                continue

            gen_id = entry.name
            record_path = records_dir / f"{gen_id}.json"
            if not record_path.exists():
                continue

            new_fp = _compute_charter_fingerprint(
                charter_path.read_text(encoding="utf-8")
            )
            record = json.loads(record_path.read_text(encoding="utf-8"))
            if record.get("charter_fingerprint") == new_fp:
                continue

            record["charter_fingerprint"] = new_fp
            record_path.write_text(json.dumps(record, indent=2))
            changed += 1
    except Exception as e:
        return changed, f"{type(e).__name__}: {e}"

    return changed, ""


def _find_lagging_bots(shared_dir: Path, current_version: str) -> list[tuple[str, str]]:
    """Return [(bot_id, deployed_version_or_empty), ...] for bots whose
    install.json stamp differs from current_version.

    Read-only; uses the same install.json that deploy_drift_monitor reads,
    so any divergence here is exactly what fires the drift signal.
    """
    install_path = shared_dir / "install.json"
    if not install_path.exists():
        return []
    try:
        info = json.loads(install_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    bot_versions = info.get("bot_versions", {}) or {}
    lagging: list[tuple[str, str]] = []
    for bot_id, entry in bot_versions.items():
        deployed = (entry or {}).get("version") or ""
        if deployed != current_version:
            lagging.append((bot_id, deployed))
    return lagging


def _redeploy_lagging_bots(
    repo: Path,
    shared_dir: Path,
    *,
    deploy_fn=None,
    record_fn=None,
) -> tuple[list[str], dict[str, str]]:
    """Redeploy every bot whose install.json bot_versions stamp lags the
    admin server's EVOLVE_VERSION. Idempotent: no-op when nothing lags.

    Returns (succeeded, errors) where ``errors`` maps bot_id → error string.
    Best-effort: any single bot's failure is captured and the sweep
    continues; the function never raises.

    deploy_fn / record_fn are injection points for tests.
    """
    from evolve_admin import deploy as _deploy  # local import to avoid cycle at module load

    deploy_fn = deploy_fn or _deploy.deploy_bot
    record_fn = record_fn or _deploy.record_bot_deploy
    current_version = _deploy.EVOLVE_VERSION

    succeeded: list[str] = []
    errors: dict[str, str] = {}

    lagging = _find_lagging_bots(shared_dir, current_version)

    # Release-canary exemption (spec-state-store-and-deploy-resilience
    # §2.4): while a candidate is soaking, the canary bot is *supposed*
    # to run the candidate version — its stamp differs from
    # EVOLVE_VERSION by design. Without this carve-out the sweep
    # redeploys the canary back to stable within one tick of the canary
    # deploy and the soak then passes on fabricated evidence.
    if lagging:
        try:
            from . import release_manager as _relmgr
            _np = repo / "config" / "network.json"
            if not _np.exists():
                _np = shared_dir / "network.json"
            try:
                _network_probe = _deploy.load_network(_np)
            except Exception:
                _network_probe = None
            _canary = _relmgr.canary_bot_during_soak(shared_dir, _network_probe)
            if _canary:
                lagging = [(b, v) for b, v in lagging if b != _canary]
        except Exception:
            pass

    if not lagging:
        return succeeded, errors

    network_path = repo / "config" / "network.json"
    # Fall back to the install-side network.json if the in-repo one isn't
    # present (older installs).
    if not network_path.exists():
        alt = shared_dir / "network.json"
        if alt.exists():
            network_path = alt
        else:
            errors["__no_network_json__"] = f"network.json not found at {network_path}"
            return succeeded, errors

    try:
        network = _deploy.load_network(network_path)
    except Exception as e:
        errors["__load_network__"] = f"{type(e).__name__}: {e}"
        return succeeded, errors

    bots_cfg = (network or {}).get("bots", {}) or {}

    for bot_id, _ in lagging:
        cfg = bots_cfg.get(bot_id, {})
        # Only redeploy bots the pod ledger still knows about. A stale
        # install.json entry for a removed bot would otherwise raise inside
        # deploy_bot (which refuses unknown bot_ids).
        if bot_id not in bots_cfg:
            errors[bot_id] = "bot not registered in network.json (stale install.json entry)"
            continue
        try:
            result = deploy_fn(
                bot_id,
                role=cfg.get("role") or "member",
                port=cfg.get("port"),
                network_path=network_path,
                dry_run=False,
                backup_repo_url=cfg.get("backupRepoUrl", ""),
            )
        except Exception as e:
            errors[bot_id] = f"{type(e).__name__}: {e}"
            continue
        if not getattr(result, "success", False):
            errors[bot_id] = getattr(result, "error_msg", "") or "deploy_bot reported failure"
            continue
        try:
            record_fn(bot_id, shared_dir)
        except Exception as e:
            # Stamp failure is non-fatal — the bot is deployed, just the
            # ledger entry is stale. Surface it so the operator can chase.
            errors[bot_id] = f"deployed ok but install.json stamp failed: {type(e).__name__}: {e}"
            continue
        succeeded.append(bot_id)

    return succeeded, errors


# ── Daemon auto-restart on pull ──────────────────────────────────────────
#
# Background: daemons load Python source from `/Users/Shared/evolve-repo`
# at process start. A `git pull` updates the source on disk but the
# already-running daemon keeps executing the previously-imported bytecode.
# Without an explicit restart, fixes shipped via PR sit dormant until the
# next deploy or a manual `launchctl kickstart`. PR #867 made this concrete:
# the fix was on disk for 14min before the user reported the bug "still
# exists" because admin-ui was still running pre-pull code.
#
# The mapping below is intentionally narrow. We restart daemons whose
# loaded code is known to come from a given path; we do NOT restart on
# every pull (that would interrupt long-running operations and make pulls
# disruptive). Index-html / JS changes get the same restart as server.py
# changes for one reason: the operator can't easily tell HTML changes
# apart from server changes, and a restart on either is a millisecond.

PULLER_SELF_PATH = "packages/admin/evolve_admin/repo_puller.py"

# Path-prefix → tuple-of-daemon-labels. A pulled path matches the FIRST
# prefix it starts with; daemons mentioned by multiple matching prefixes
# get deduped via the set in `daemons_for_paths`. Order is documentation
# only — changes with no prefix overlap (the case today) make order
# irrelevant.
_PATH_DAEMON_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Admin server (Flask + CLI + plist installers + web/index.html). The
    # Flask process serves index.html on demand so HTML doesn't *need* a
    # restart, but the operator can't differentiate from server.py edits
    # so a single rule keeps the mental model simple.
    #
    # The MCP bridge (``ai.evolve.evolve.mcp-bridge``) is a separate process
    # that loads ``evolve_admin.mcp_bridge.tools.TOOL_HANDLERS`` once at
    # startup. Without restart, a PR that adds a new MCP tool (PR #1862 added
    # gmail_send / drive_write_file / calendar_create_event) leaves the
    # bridge serving the pre-pull registry — bots can't see the new tools.
    # Restarting both daemons together is a millisecond.
    ("packages/admin/", (
        "ai.evolve.evolve.admin-ui",
        "ai.evolve.evolve.mcp-bridge",
    )),
    # Analyzer code is imported via `_import_analyzer` by the long-running
    # heal/audit/verify daemons; without a restart, those keep using the
    # pre-pull module objects.
    ("packages/analyzer/", (
        "ai.evolve.evolve.heal",
        "ai.evolve.evolve.audit",
        "ai.evolve.evolve.verify",
    )),
)


AUTO_RESTART_ENV = "EVOLVE_PULLER_AUTO_RESTART"


def _auto_restart_enabled() -> bool:
    """Default ON. Set ``EVOLVE_PULLER_AUTO_RESTART=0`` (or false/no/off)
    in the puller plist's environment to disable in 30 seconds if the
    auto-restart misbehaves in production. The puller itself keeps running;
    we just stop kickstarting downstream daemons."""
    val = _os.environ.get(AUTO_RESTART_ENV, "1").strip().lower()
    return val not in ("0", "false", "no", "off", "")


def daemons_for_paths(paths: list[str]) -> tuple[set[str], list[str]]:
    """Map changed file paths → (daemon_labels_to_restart, soft_warnings).

    `warnings` is non-empty when we deliberately decline to restart a
    daemon we'd otherwise touch. The only such case today is the puller
    itself: a daemon kickstarting its own LaunchDaemon mid-tick is racy
    (launchd may kill the process before the kickstart syscall returns;
    the next cron tick will load the new code anyway).
    """
    daemons: set[str] = set()
    warnings: list[str] = []
    for p in paths:
        if p == PULLER_SELF_PATH:
            warnings.append(
                f"{PULLER_SELF_PATH} changed; skipping self-restart "
                "(manual `sudo /bin/launchctl kickstart -k "
                "system/ai.evolve.evolve.repo-puller` required, "
                "or wait for the next 15-min cron tick)"
            )
            continue
        for prefix, labels in _PATH_DAEMON_RULES:
            if p.startswith(prefix):
                daemons.update(labels)
    return daemons, warnings


# Where the editable-installed evolve-admin venv lives. Pull-side
# pip-install runs as the evolve user against this venv. Platform-keyed
# (W10-F #8b): /Users/Shared/evolve-venv on macOS, /var/lib/evolve-venv on
# Linux — a hardcoded macOS path made `_pip_install_admin` fail
# "No such file" on every Linux pull that touched the admin pyproject.
VENV_PIP = f"{_PROFILE.venv_dir}/bin/pip"
ADMIN_PACKAGE_DIR = "packages/admin"


def _pip_install_admin(repo: Path) -> tuple[bool, str]:
    """Run ``pip install -e packages/admin`` in the evolve venv.

    Picks up new pyproject.toml dependencies (and entry-point changes)
    after a pull that touched the admin package's pyproject.toml.

    Returns (ok, info). Same best-effort contract as ``_kickstart_daemon``:
    failures never raise; the pull itself has already advanced HEAD and
    we don't want a pip glitch to fail the whole tick. The operator sees
    the failure in the puller log and can re-run pip manually.

    Runs as root via plain ``sudo`` (not ``-u evolve``) because the venv
    at ``/Users/Shared/evolve-venv`` is root-owned — every other package
    in its site-packages dir was installed as root, and ``sudo -u evolve
    pip install`` would fail with EACCES on the site-packages directory.
    The setup-wizard sudoers grant ``evolve ALL=(root) NOPASSWD:
    /Users/Shared/evolve-venv/bin/pip install -e
    /Users/Shared/evolve-repo/packages/admin`` makes this work from the
    puller daemon (which runs as evolve). If the grant is missing
    (operator hasn't run ``sudo evolve-admin refresh-sudoers`` since
    the grant landed), pip fails with "evolve is not in the sudoers
    file" — that error surfaces in pip_install_info so the operator
    sees the fix path."""
    try:
        r = subprocess.run(
            ["sudo", VENV_PIP, "install", "-e", str(repo / ADMIN_PACKAGE_DIR)],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False, "timeout (300s)"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if r.returncode == 0:
        # pip is verbose; the most operator-useful signal is the install
        # summary line (e.g. "Successfully installed X-1.0 Y-2.0") which
        # is on stdout.
        tail = (r.stdout or "").strip().splitlines()
        summary = next(
            (line for line in reversed(tail) if line.startswith("Successfully")),
            "ok",
        )
        return True, summary[:200]
    detail = (r.stderr or r.stdout or "").strip()
    return False, f"rc={r.returncode}: {detail[:200] or '(no output)'}"


SUDOERS_MARKER_RELPATH = "state/sudoers-installed.sha256"


def _sudoers_drift_check(shared_dir: Path) -> "tuple[bool | None, str]":
    """Detect whether the installed sudoers lags the rendered template.

    The puller runs as ``evolve``, which CANNOT install ``/etc/sudoers.d/evolve``
    (Option B, #2759 — a self-grant would be a direct privilege-escalation path,
    and the deploy checkout this code lives in is evolve-writable, so a root
    auto-installer running repo-rendered code would let a compromised evolve
    escalate). It also can't read the root-owned file. So drift is judged by
    hashing the current render and comparing to the marker the *trusted root
    install* writes (``setup_wizard._record_installed_sudoers_marker``).

    Returns ``(in_sync, info)``:
      * ``(True,  ...)``  rendered grants are installed — nothing to do.
      * ``(False, ...)``  DRIFT — merged grants are dormant; operator must run
                          ``sudo evolve-admin refresh-sudoers`` as root.
      * ``(None,  ...)``  can't determine (render unavailable) — caller leaves
                          any existing Signal untouched rather than cry-wolf.

    Lazy import keeps setup_wizard's transitive deps off the puller hot path.
    """
    try:
        from . import setup_wizard as _sw
        content = _sw._render_evolve_sudoers()
    except Exception as e:  # noqa: BLE001
        return None, f"render unavailable: {type(e).__name__}: {e}"
    if content is None:
        return None, "render unavailable (openclaw CLI not discoverable)"
    import hashlib
    want = hashlib.sha256(content.encode("utf-8")).hexdigest()
    marker = Path(shared_dir) / SUDOERS_MARKER_RELPATH
    try:
        have = marker.read_text().strip()
    except OSError:
        have = ""
    if have == want:
        return True, "in sync"
    return False, (
        "installed /etc/sudoers.d/evolve lags the rendered template — grants "
        "from merged PRs are dormant. Run `sudo evolve-admin refresh-sudoers` "
        "as root (the evolve service user is intentionally barred from "
        "installing its own sudoers — Option B, #2759)."
    )


def _sudoers_drift_signal_firing(shared_dir: Path) -> bool:
    """Cheap check: is the ``sudoers_refresh_failed`` Signal currently firing?

    Lets the puller re-evaluate (and RESOLVE) the Signal on a pull that did
    NOT touch setup_wizard.py — the fix for the cry-wolf where a manually-
    refreshed sudoers left the Signal stuck firing because the old hook only
    ever auto-resolved on its own (impossible-as-evolve) install success.

    Asks signals.store: the spec's ``signature_index.json`` was never
    implemented (signature lookup is a directory scan inside signals.store —
    see the historical note in signals/retention.py), so the original
    index-file read here was dead code that always returned False. Live
    consequence on the VPS pod, 2026-07-29: an operator ran refresh-sudoers,
    but the Signal stayed firing for every subsequent no-change pull because
    this gate never let the drift check re-run. Signals-module-unavailable
    degrades to False, matching observe/resolve (which would be no-ops too).
    """
    store, schema = _signals_module()
    if store is None or schema is None:
        return False
    try:
        signature = schema.make_signature(
            "repo_puller_sudoers", "sudoers_refresh_failed", "evolve",
        )
    except Exception:  # noqa: BLE001 — mirror observe_sudoers_refresh_failed_signal
        signature = "repo_puller_sudoers:sudoers_refresh_failed:evolve"
    try:
        return store.find_active_by_signature(Path(shared_dir), signature) is not None
    except Exception:  # noqa: BLE001 — unreadable store → treat as not firing
        return False


# Per-restart timeout bound for post-pull daemon kickstarts. Threaded as a
# per-call override into get_scheduler().restart(..., timeout=...) so the
# platform-portable seam (LaunchdScheduler on macOS, SystemdScheduler on
# Linux via the platform gate's set_scheduler() injection) is honored —
# constructing a module-global LaunchdScheduler() would bypass that injection
# and invoke launchctl on a Linux pod. The 15s bound is load-bearing
# (get_scheduler()'s default is 30s; the puller restarts up to ~7 daemons
# sequentially and must stay bounded). Tests inject a fake via
# runtime.set_scheduler(LaunchdScheduler(runner=<fake>)); never let a test
# reach a real launchctl — kickstart -k is live-traffic destructive.
_KICKSTART_TIMEOUT_S = 15.0


def _kickstart_daemon(label: str) -> tuple[bool, str]:
    """Restart a single daemon via the Scheduler seam (kickstart -k).

    Returns (ok, info). Failures never raise: a held process or a
    sudoers gap shouldn't crash the puller — log loudly and move on, the
    pull itself has already succeeded. The whole point of auto-restart
    is best-effort. (The seam's runner reports timeouts and OSErrors as
    failures with the exception text as output, so the never-raise
    contract holds.)"""
    ok, out = get_scheduler().restart(label, timeout=_KICKSTART_TIMEOUT_S)
    if ok:
        return True, "ok"
    return False, (out.strip()[:200] or "(no output)")


def _discover_bot_gateways(
    launchd_dir: Path = DEFAULT_LAUNCHD_DIR,
) -> list[str]:
    """Find installed OpenClaw gateway launchd labels via plist scan.

    Returns a sorted list of ``ai.openclaw.<bot>-gateway`` labels by
    scanning ``/Library/LaunchDaemons/`` for matching plist files. The
    glob is bot-id-agnostic, so it discovers the PRIMARY bot's gateway
    too — ``ai.openclaw.evo-gateway`` on an evo-primary pod (or the legacy
    ``ai.openclaw.evolve-gateway`` on an evolve-primary pod), both of which
    also load the evolve plugin at startup, so the same restart rule
    applies. No literal ``evolve`` is required here — discovery resolves
    the primary by shape, not name.

    Reading the launchd dir doesn't require sudo (it's world-readable on
    macOS), so the daemon (evolve user) can discover gateways without a
    new sudoers grant for ``launchctl list``. Returns ``[]`` on missing
    dir rather than raising; the caller treats "nothing to restart" as
    success.
    """
    if not launchd_dir.is_dir():
        return []
    labels: list[str] = []
    for p in launchd_dir.glob("ai.openclaw.*-gateway.plist"):
        labels.append(p.stem)
    return sorted(labels)


def _short_bot_name(gateway_label: str) -> str:
    """``ai.openclaw.team_bot_a-gateway`` → ``team_bot_a`` for the operator log line."""
    return gateway_label.removeprefix("ai.openclaw.").removesuffix("-gateway")


def _restart_bot_gateways(
    labels: list[str],
    kickstart_fn: "callable",
    stagger_seconds: float = DEFAULT_GATEWAY_KICKSTART_STAGGER_SECONDS,
    sleep_fn: "callable | None" = None,
) -> tuple[list[str], dict[str, str]]:
    """Kickstart a list of gateway labels with a small delay between each.

    Returns (restarted, errors). ``restarted`` is the labels that
    kickstart_fn returned ok=True for, in invocation order; ``errors``
    maps label → failure info for the rest.

    Staggering trade-off: each gateway is a fresh node process that
    opens connections to its messaging integration on boot. Six bots
    respawning at once briefly thunders the network/disk; spacing them
    by a couple of seconds avoids that without meaningfully extending
    the puller tick.

    Note on graceful shutdown: ``launchctl kickstart -k`` (the existing
    ``_kickstart_daemon`` invocation) sends SIGTERM first and gives the
    process the plist's ExitTimeOut window (default ~20s) before
    SIGKILL. OC gateways don't currently install signal handlers for
    session draining, so in-flight session state can still be lost on a
    busy gateway. Acceptable here because plugin rebuilds are infrequent
    and the alternative (gateways running pre-cascade code indefinitely)
    is worse.
    """
    if sleep_fn is None:
        sleep_fn = _time.sleep
    restarted: list[str] = []
    errors: dict[str, str] = {}
    for i, label in enumerate(labels):
        if i > 0 and stagger_seconds > 0:
            sleep_fn(stagger_seconds)
        try:
            ok, info = kickstart_fn(label)
        except Exception as e:
            ok, info = False, f"{type(e).__name__}: {e}"
        if ok:
            restarted.append(label)
        else:
            errors[label] = info
    return restarted, errors


def _rebuild_plugin() -> tuple[bool, str]:
    """Rebuild + restage the OpenClaw plugin from the pulled source.

    The mini's openclaw gateways load from `/Users/Shared/evolve-plugin/`,
    NOT from the repo working tree. Without a restage step, a fresh pull
    that updates `packages/plugin/` has zero effect on running gateways
    until someone runs `sudo evolve-admin deploy` manually.

    This is a real gap: discovered 2026-05-06 when the dist on the mini
    was missing `RecentTranscriptCapture.js` despite the source being
    pulled — gateway load failed silently and the defer tool wasn't
    registered. Operator had to ssh in and rebuild manually.

    Returns (ok, info). On error, info is the failure message; the
    caller logs but does NOT fail the pull (HEAD has already advanced).

    Imports lazily so the puller's hot path stays light when no plugin
    work is in flight.
    """
    try:
        from . import deploy as _deploy
        _deploy.build_plugin()
        return True, "rebuilt + staged"
    except Exception as e:
        return False, f"build_plugin failed: {e}"


def pull(
    repo: Path = DEFAULT_REPO,
    remote: str = DEFAULT_REMOTE,
    branch: str = DEFAULT_BRANCH,
    *,
    shared_dir: Path = DEFAULT_SHARED_DIR,
    quarantine_root: Path = DEFAULT_QUARANTINE,
    now: "_dt.datetime | None" = None,
    rebuild_plugin_fn: "callable | None" = None,
    kickstart_fn: "callable | None" = None,
    pip_install_fn: "callable | None" = None,
    sudoers_refresh_fn: "callable | None" = None,
    gateway_discovery_fn: "callable | None" = None,
    gateway_stagger_seconds: float = DEFAULT_GATEWAY_KICKSTART_STAGGER_SECONDS,
    gateway_sleep_fn: "callable | None" = None,
) -> PullResult:
    """Pull `remote/branch` into `repo` with --ff-only.

    Returns a PullResult capturing what happened. Caller (CLI shim)
    decides what to print and which exit code to return.
    """
    result = PullResult(success=False)

    if not repo.exists():
        result.error = f"repo path does not exist: {repo}"
        result.steps.append(f"FAIL: {result.error}")
        return result

    rc, head_before, err = _git(repo, ["rev-parse", "HEAD"])
    if rc != 0:
        # Single-box / tarball-staged pod: the deploy checkout is real source
        # but NOT a git working tree (no `.git`), so `git pull` can't run.
        # That is a legitimate single-VPS shape, not a wedge — no-op cleanly
        # (success=True, exit 0) so health stays green and the daemon doesn't
        # flap on a perpetual "rev-parse HEAD failed". Updates land via
        # re-staging. Gated on the rev-parse failure (not a pre-check) so the
        # mocked-git unit tests, whose tmp repos carry no `.git`, are
        # unaffected. Uses `.exists()` (not `.is_dir()`) so a PRESENT-but-broken
        # `.git` — a dir with a dangling HEAD, or a worktree `.git` FILE whose
        # gitdir pointer is stale — still surfaces as a hard error rather than
        # being silently misclassified as a no-git no-op. Only a fully-absent
        # `.git` (the tarball-staged shape) no-ops. See
        # docs/runbook-vps-pod-provision.md.
        if not (repo / ".git").exists():
            result.success = True
            result.skipped_not_git = True
            result.steps.append(
                f"SKIP: {repo} is not a git working tree (tarball-staged "
                f"single-box pod) — puller idle; updates land via re-staging"
            )
            return result
        result.error = f"rev-parse HEAD failed: {err}"
        result.steps.append(f"FAIL: {result.error}")
        return result
    result.head_before = head_before
    result.steps.append(f"HEAD before: {head_before[:8]}")

    # Defensive (2026-06-23 freeze): pin core.fileMode=false on a nested Linux
    # deploy checkout BEFORE the ff-only pull, so exec-bit churn from a
    # recursive shared-dir perms pass (which flipped 100644→100755 on 3096
    # files) can never wedge this pull. No-op on the macOS sibling layout and
    # once already false. Runs as evolve (the daemon owns the repo).
    fm_ok, fm_msg = pin_filemode_off_if_nested(repo, shared_dir, sudo_evolve=False)
    if not fm_ok:
        result.steps.append(f"WARN core.fileMode: {fm_msg}")

    rc, _, err = _git(repo, ["pull", "--ff-only", remote, branch])
    if rc != 0:
        # Untracked-file conflict has its own recovery path: sweep the
        # offenders aside (delete-if-identical, else quarantine) and
        # retry once. This is the most common wedge shape — silently
        # resolving it keeps the deploy in sync without operator action.
        if _handle_untracked_conflict(
            repo, remote, branch, err, result, quarantine_root, now=now,
        ):
            err = ""   # retry succeeded; clear so we fall through
            rc = 0
        else:
            result.error = f"pull --ff-only failed: {err}"
            result.steps.append(f"FAIL: {result.error}")
            # Common case: non-fast-forward (someone committed locally on
            # mini). Surface this clearly so the operator notices instead
            # of the failure being one line of git noise. Match all the
            # phrases git uses across versions: "non-fast-forward" (older),
            # "Not possible to fast-forward" (current), "would clobber"
            # (worktree conflict), "diverged" (less common variant).
            err_l = err.lower()
            if any(s in err_l for s in (
                "non-fast-forward", "not possible to fast-forward",
                "would clobber", "diverged",
            )):
                result.steps.append(
                    "HINT: mini's repo has a local commit that origin lacks. "
                    "This should not happen — mini is read-only from the operator's "
                    "POV. Investigate before resolving (do not force-pull)."
                )
            return result

    rc, head_after, err = _git(repo, ["rev-parse", "HEAD"])
    if rc != 0:
        result.error = f"rev-parse HEAD (after pull) failed: {err}"
        result.steps.append(f"FAIL: {result.error}")
        return result
    result.head_after = head_after
    result.success = True

    if head_before == head_after:
        result.steps.append(f"already up to date at {head_before[:8]}")
        # Even on no-op pulls we run the post-pull openclaw.json validation.
        # The trigger condition is "schema vs on-disk drift", and drift can
        # appear without HEAD moving — e.g. someone hand-edits an
        # openclaw.json on the mini between ticks. Without this, drift
        # would go unnoticed until the next code change advanced HEAD.
        _run_openclaw_validation_hook(result, shared_dir)
        # Lagging-bot redeploy also runs on no-op pulls: prior ticks may
        # have advanced EVOLVE_VERSION and restarted admin daemons without
        # successfully redeploying every bot. The check is cheap (single
        # install.json read) and a no-op when nothing lags.
        _run_lagging_bot_redeploy_sweep(result, repo, shared_dir)
        _run_gallery_reseed_hook(result, repo, shared_dir)
        return result

    rc, log, err = _git(repo, ["log", "--oneline", f"{head_before}..{head_after}"])
    if rc == 0:
        result.commits_advanced = len([l for l in log.splitlines() if l.strip()])
        result.log_summary = log
    result.steps.append(
        f"advanced {head_before[:8]}..{head_after[:8]} "
        f"({result.commits_advanced} commits)"
    )

    # One diff call feeds the downstream hook decisions: plugin rebuild,
    # infra reinstall, charter bumps, sudoers refresh, pip install, and
    # daemon restarts. Empty list means we couldn't determine the diff
    # (best-effort; HEAD has already advanced). The hook suite itself is
    # shared with the release manager's promote/rollback path
    # (run_hooks_only), which executes it from a fresh subprocess so
    # EVOLVE_VERSION reflects the post-move checkout.
    paths = _pulled_paths(repo, head_before, head_after)
    _run_post_advance_hooks(
        repo, shared_dir, result, paths,
        rebuild_plugin_fn=rebuild_plugin_fn,
        kickstart_fn=kickstart_fn,
        pip_install_fn=pip_install_fn,
        sudoers_refresh_fn=sudoers_refresh_fn,
        gateway_discovery_fn=gateway_discovery_fn,
        gateway_stagger_seconds=gateway_stagger_seconds,
        gateway_sleep_fn=gateway_sleep_fn,
    )

    # Post-pull openclaw.json validation. Catches the schema-tightened-
    # existing-config-stale class (PR #1525 was the canonical case).
    # Runs AFTER daemon restart so we're validating the actual end state
    # the gateways will load on their next reload.
    _run_openclaw_validation_hook(result, shared_dir)
    _run_lagging_bot_redeploy_sweep(result, repo, shared_dir)
    _run_gallery_reseed_hook(result, repo, shared_dir)
    return result


def _run_post_advance_hooks(
    repo: Path,
    shared_dir: Path,
    result: PullResult,
    paths: list[str],
    *,
    rebuild_plugin_fn: "callable | None" = None,
    kickstart_fn: "callable | None" = None,
    pip_install_fn: "callable | None" = None,
    sudoers_refresh_fn: "callable | None" = None,
    gateway_discovery_fn: "callable | None" = None,
    gateway_stagger_seconds: float = DEFAULT_GATEWAY_KICKSTART_STAGGER_SECONDS,
    gateway_sleep_fn: "callable | None" = None,
) -> None:
    """The post-advance hook suite — everything that must happen after
    the deploy checkout's code moved (plugin rebuild + gateway restarts,
    infra-jobs reinstall, charter fingerprint bumps, sudoers refresh,
    pip install, path-mapped daemon kickstarts).

    Extracted from pull() so the release manager's promote/rollback path
    (run_hooks_only) can run the exact same suite for an arbitrary
    head_before..head_after move. Mutates ``result``; never raises.
    """

    # If the pulled diff touched packages/plugin/, rebuild + restage so
    # the running gateways pick up the new TS code on their next reload.
    # Without this, plugin updates land in the working tree but openclaw
    # keeps loading the stale staged copy from /Users/Shared/evolve-plugin/.
    # Discovered the hard way on 2026-05-06: a pull brought in new
    # DeferTool source, but the gateway loaded yesterday's dist and the
    # defer tool failed to register. Operator had to ssh + rebuild manually.
    #
    # Failures here don't fail the overall pull — HEAD has already advanced
    # and reverting that is more disruptive than logging a broken stage.
    # The operator sees the error in the puller log on the next cycle.
    if _paths_touch_plugin(paths):
        result.steps.append("plugin paths changed → rebuild + restage")
        rebuild_fn = rebuild_plugin_fn or _rebuild_plugin
        ok, info = rebuild_fn()
        if ok:
            result.plugin_rebuilt = True
            result.steps.append(f"plugin: {info}")
        else:
            result.plugin_rebuild_error = info
            result.steps.append(f"FAIL plugin rebuild: {info}")

        # The rebuild step (build_plugin → tsc → git checkout → chown/chmod
        # cycle) updates file mtimes without changing content. That leaves
        # git's stat-cache stale: a concurrent `git pull` from any other
        # user (the human admin running `git pull` minutes after the
        # puller's tick) sees "Your local changes to the following files
        # would be overwritten" even though the files are byte-identical
        # to HEAD. Refreshing the index re-stats every entry and clears
        # the false-positive flag. Exit code 1 is fine here — that's
        # `update-index` saying "real differences exist," which we treat
        # as informational; we never want to fail the pull on this.
        _git(repo, ["update-index", "--refresh"])
        result.steps.append("refreshed stat-cache after rebuild")

        # Restart bot OC gateways so they pick up the freshly-staged
        # plugin dist. The repo-puller already restarts the ai.evolve.*
        # admin daemons via the path→daemon rules below, but those rules
        # don't cover ai.openclaw.<bot>-gateway daemons — and those are
        # the long-running node processes that load the evolve plugin
        # once at startup. Without this hook, every plugin restage left
        # the bot gateways executing pre-cascade code with no operator
        # signal (witnessed PR #1639, cascade Phase 1 telemetry).
        #
        # Gated on plugin_rebuilt=True (not merely "plugin paths in the
        # diff") so a failed rebuild doesn't yank gateways for no benefit
        # — they'd come up loading the same stale dist.
        if result.plugin_rebuilt:
            if not _auto_restart_enabled():
                result.steps.append(
                    f"bot-gateway restart skipped: "
                    f"{AUTO_RESTART_ENV}=0"
                )
            else:
                discover = gateway_discovery_fn or _discover_bot_gateways
                try:
                    gateways = discover()
                except Exception as e:
                    gateways = []
                    result.bot_gateway_discovery_error = f"{type(e).__name__}: {e}"
                    result.steps.append(
                        f"FAIL bot-gateway discovery: "
                        f"{result.bot_gateway_discovery_error}"
                    )
                if gateways:
                    kick = kickstart_fn or _kickstart_daemon
                    result.steps.append(
                        f"restarting {len(gateways)} bot gateway(s) "
                        f"after plugin rebuild "
                        f"(stagger {gateway_stagger_seconds}s): "
                        f"{', '.join(_short_bot_name(l) for l in gateways)}"
                    )
                    restarted, errors = _restart_bot_gateways(
                        gateways,
                        kickstart_fn=kick,
                        stagger_seconds=gateway_stagger_seconds,
                        sleep_fn=gateway_sleep_fn,
                    )
                    result.bot_gateways_restarted = restarted
                    result.bot_gateway_restart_errors = errors
                    for label in restarted:
                        result.steps.append(f"restart {label}: ok")
                    for label, info in sorted(errors.items()):
                        result.steps.append(f"FAIL restart {label}: {info}")

    # If the pulled diff added/changed launchd-install code in deploy.py,
    # re-run install_evolve_infra_jobs so new plists land + existing ones
    # pick up content changes. Without this hook, plists added in a PR
    # (cost_watchdog from #906, embedding_monitor from #917, etc.) sit
    # un-installed on the mini until someone manually runs
    # `sudo evolve-admin install-infra-jobs` — discovered the hard way
    # 2026-05-10 when ~6 PRs worth of new infra plists were missing.
    # The Scheduler seam's install() skips the bootout/bootstrap (and the
    # systemd daemon-reload/restart) when content matches the unit already on
    # disk, so this call is cheap on the typical case where no plist content
    # actually changed.
    if _paths_touch_infra_install(paths):
        result.steps.append("infra-install paths changed → install_evolve_infra_jobs")
        try:
            from . import deploy as _deploy
            # Platform-keyed evolve home (/Users on macOS, /home on Linux). (W10-G #5.)
            ij_result = _deploy.install_evolve_infra_jobs(
                Path(_PROFILE.user_home_root) / "evolve")
            installed = [
                line for line in ij_result.steps
                if line.startswith("Installed launchd:")
            ]
            up_to_date = sum(
                1 for line in ij_result.steps
                if line.startswith("Up-to-date launchd:")
            )
            result.infra_jobs_installed = [
                line.removeprefix("Installed launchd: ").strip()
                for line in installed
            ]
            result.steps.append(
                f"infra-install: {len(installed)} new/changed, {up_to_date} unchanged"
            )
            if not ij_result.success:
                result.infra_jobs_install_error = "install_evolve_infra_jobs reported failure"
                result.steps.append(f"FAIL infra-install: {result.infra_jobs_install_error}")
        except Exception as e:
            result.infra_jobs_install_error = f"{type(e).__name__}: {e}"
            result.steps.append(f"FAIL infra-install: {result.infra_jobs_install_error}")

    # When the pulled diff touches a generator charter, bump the stored
    # fingerprints so the registry can load those generators on the next
    # cycle.  Without this, every charter-modifying PR leaves the registry
    # with a mismatch until an operator manually runs bump_charter_fingerprints.py
    # — the recurring pattern that caused three simultaneous load errors
    # (efficiency_hawk, security_warden, sysadmin_watchdog) in May 2026.
    if _paths_touch_charters(paths):
        result.steps.append("charter paths changed → bump fingerprints")
        try:
            n, err = _bump_charter_fingerprints(repo, shared_dir)
            if err:
                result.charter_fingerprint_bump_error = err
                result.steps.append(f"FAIL charter-bump: {err}")
            else:
                result.charter_fingerprints_bumped = n
                result.steps.append(f"charter-bump: {n} updated")
        except Exception as e:
            result.charter_fingerprint_bump_error = f"{type(e).__name__}: {e}"
            result.steps.append(f"FAIL charter-bump: {result.charter_fingerprint_bump_error}")

    # /etc/sudoers.d/evolve is installed ONLY by an operator running
    # `sudo evolve-admin refresh-sudoers` as root. The evolve service user —
    # which this puller runs as — is DELIBERATELY barred from installing its
    # own sudoers (Option B, #2759): a self-grant is a direct privilege-
    # escalation path, and the deploy checkout this code lives in is evolve-
    # writable, so a root auto-installer running repo-rendered code would let a
    # compromised evolve escalate to root. So the puller does NOT apply grants.
    #
    # Instead it DETECTS DRIFT — the rendered template vs a hash marker the
    # trusted root install writes — and fires/RESOLVES the sudoers_refresh_failed
    # Signal so a dormant-grant backlog can't sit silently. This replaces the old
    # attempt-based hook, which (a) always "failed" as evolve (a misleading FAIL
    # line every pull) and (b) only auto-resolved on its own impossible success,
    # so once fired the Signal never cleared after a manual fix and became ignored
    # noise — exactly how /etc/sudoers.d/evolve sat stale Jun 12–16 with merged
    # grants dormant. Gated on a setup_wizard.py change OR a still-firing Signal
    # so a manual fix is noticed (and the Signal resolved) on the next tick.
    #
    # MUST run before the pip-install + daemon-restart blocks below: a PR can
    # introduce a grant that pip-install or a restarted daemon depends on, and
    # surfacing the drift first gives the operator the heads-up before the
    # downstream "evolve is not in the sudoers file" failure.
    if _paths_touch_setup_wizard(paths) or _sudoers_drift_signal_firing(shared_dir):
        result.sudoers_refresh_attempted = True
        drift_fn = sudoers_refresh_fn or _sudoers_drift_check
        try:
            in_sync, info = drift_fn(shared_dir)
        except Exception as e:
            in_sync, info = None, f"{type(e).__name__}: {e}"
        result.sudoers_refresh_ok = in_sync is True
        result.sudoers_refresh_info = info
        if in_sync is True:
            result.steps.append("sudoers: in sync")
            try:
                resolve_sudoers_refresh_signal(shared_dir)
            except Exception:
                pass
        elif in_sync is False:
            result.steps.append(f"sudoers DRIFT (grants dormant): {info}")
            try:
                observe_sudoers_refresh_failed_signal(
                    error=info, head=result.head_after, shared_dir=shared_dir,
                )
            except Exception:
                pass
        else:
            # Render unavailable — can't judge; leave any existing Signal as-is.
            result.steps.append(f"sudoers drift check skipped: {info}")

    # If the pulled diff touched packages/admin/pyproject.toml, run
    # pip install -e packages/admin so new dependencies land in the venv
    # before downstream daemons restart against them. Editable install
    # picks up code changes automatically (the venv reads from the repo
    # working tree directly) but new pip-installed packages must be
    # explicitly installed — concretely surfaced 2026-05-31 when PR
    # #1862 added google-auth + google-api-python-client and the bridge
    # crashed on first call until pip was run manually on the mini.
    #
    # MUST run before the daemon-restart block below. Otherwise daemons
    # restart, fail to import the new modules, and the new tools are
    # dormant on a process that's also now in a crash loop.
    if _paths_touch_pyproject(paths):
        result.pip_install_attempted = True
        result.steps.append("pyproject.toml changed → pip install -e packages/admin")
        pip_fn = pip_install_fn or _pip_install_admin
        try:
            ok, info = pip_fn(repo)
        except Exception as e:
            ok, info = False, f"{type(e).__name__}: {e}"
        result.pip_install_ok = ok
        result.pip_install_info = info
        if ok:
            result.steps.append(f"pip install: {info}")
        else:
            result.steps.append(f"FAIL pip install: {info}")

    # Restart any LaunchDaemons whose loaded code came from a path in the
    # pulled diff. Without this, fixes shipped via PR sit dormant in the
    # daemon process until the next deploy or a manual kickstart — exactly
    # the gap that surfaced in PR #867 (admin-ui ran pre-pull code for
    # 14min after the fix landed). Best-effort: kickstart failures never
    # fail the overall pull; the operator sees them in the puller log.
    daemons, restart_warnings = daemons_for_paths(paths)
    for w in restart_warnings:
        result.restart_warnings.append(w)
        result.steps.append(f"restart skip: {w}")
    if daemons:
        if not _auto_restart_enabled():
            result.restart_skipped_disabled = True
            result.steps.append(
                f"auto-restart disabled ({AUTO_RESTART_ENV}=0); would have "
                f"restarted: {', '.join(sorted(daemons))}"
            )
        else:
            kick = kickstart_fn or _kickstart_daemon
            result.steps.append(
                f"restarting {len(daemons)} daemon(s): "
                f"{', '.join(sorted(daemons))}"
            )
            for label in sorted(daemons):
                try:
                    ok, info = kick(label)
                except Exception as e:
                    ok, info = False, f"{type(e).__name__}: {e}"
                if ok:
                    result.restarted_daemons.append(label)
                    result.steps.append(f"restart {label}: ok")
                else:
                    result.restart_errors[label] = info
                    result.steps.append(f"FAIL restart {label}: {info}")



def run_hooks_only(
    repo: Path,
    head_before: str,
    head_after: str,
    *,
    shared_dir: Path = DEFAULT_SHARED_DIR,
    rebuild_plugin_fn: "callable | None" = None,
    kickstart_fn: "callable | None" = None,
    pip_install_fn: "callable | None" = None,
    sudoers_refresh_fn: "callable | None" = None,
    gateway_discovery_fn: "callable | None" = None,
) -> PullResult:
    """Run the post-advance hook suite for an externally-performed code
    move (release-manager promote/rollback), plus the per-tick
    maintenance hooks.

    The release manager invokes this via ``evolve-admin repo-pull
    --hooks-from A --hooks-to B`` in a FRESH subprocess after moving the
    fleet checkout, so deploy.EVOLVE_VERSION (computed at import time)
    stamps the post-move version — running it in the long-lived tick
    process would stamp the pre-move version (spec §2.4 review #14).

    Works for backward moves too (rollback): ``git diff A..B`` names the
    changed paths regardless of ancestry direction.
    """
    result = PullResult(
        success=True, head_before=head_before, head_after=head_after,
    )
    paths = _pulled_paths(repo, head_before, head_after)
    result.steps.append(
        f"hooks-only: {head_before[:8]}..{head_after[:8]} "
        f"({len(paths)} changed paths)"
    )
    _run_post_advance_hooks(
        repo, shared_dir, result, paths,
        rebuild_plugin_fn=rebuild_plugin_fn,
        kickstart_fn=kickstart_fn,
        pip_install_fn=pip_install_fn,
        sudoers_refresh_fn=sudoers_refresh_fn,
        gateway_discovery_fn=gateway_discovery_fn,
    )
    _run_openclaw_validation_hook(result, shared_dir)
    _run_lagging_bot_redeploy_sweep(result, repo, shared_dir)
    _run_gallery_reseed_hook(result, repo, shared_dir)
    return result


def run_tick_maintenance(
    repo: Path,
    shared_dir: Path = DEFAULT_SHARED_DIR,
) -> PullResult:
    """The per-tick healing hooks (openclaw config validation +
    lagging-bot redeploy + builtin-Spec re-seed) detached from any code
    move. Canary-mode ticks call this so the every-15-minutes pod
    maintenance the legacy pull() provided keeps running even though the
    fleet checkout only moves at promote."""
    result = PullResult(success=True)
    _run_openclaw_validation_hook(result, shared_dir)
    _run_lagging_bot_redeploy_sweep(result, repo, shared_dir)
    _run_gallery_reseed_hook(result, repo, shared_dir)
    return result


def _run_openclaw_validation_hook(result: PullResult, shared_dir: Path) -> None:
    """Run the post-pull openclaw.json validator and decorate ``result``.

    Best-effort: never raises. Validator failure or absent network/signals
    infra is captured as a step on ``result`` so the operator sees it in
    the puller log, but the pull itself remains successful.

    Called from both the head-moved and head-unchanged code paths in
    :func:`pull` — drift between schema and on-disk config can appear
    even when HEAD doesn't advance (manual edits, plugin rebuilt last
    tick, etc.), so we want to validate every tick.
    """
    try:
        from . import openclaw_config_validator as _ocv
        val_results = _ocv.validate_all_bots(shared_dir=shared_dir)
        invalid = sorted(
            b for b, r in val_results.items()
            if isinstance(r, dict) and not r.get("valid") and not r.get("error")
        )
        errored = sorted(
            b for b, r in val_results.items()
            if isinstance(r, dict) and r.get("error")
        )
        if invalid:
            result.openclaw_invalid_bots = invalid
            result.steps.append(
                f"openclaw config validation: {len(invalid)} bot(s) failing: "
                f"{', '.join(invalid)}"
            )
        elif val_results:
            result.steps.append("openclaw config validation: all bots pass")
        if errored:
            result.steps.append(
                f"openclaw config validation skipped for "
                f"{len(errored)} bot(s) (validator couldn't run): "
                f"{', '.join(errored)}"
            )
    except Exception as e:
        result.steps.append(
            f"openclaw config validation hook failed: {type(e).__name__}: {e}"
        )


def _run_gallery_reseed_hook(
    result: PullResult, repo: Path, shared_dir: Path
) -> None:
    """Re-seed builtin Specs from repo gallery packages that moved ahead.

    Repo-side gallery edits (e.g. #2695's delivery-endpoint migration) don't
    reach a deployed pod's bound builtin Specs: a gallery install binds the
    pre-existing ``gallery/builtin/<spec_id>/<version>.json`` and never
    re-reads the repo package. Without this sweep a freshly-forged bot keeps
    inheriting the stale Spec until someone manually re-runs the migration —
    the root cause of the 2026-06-12 U1 morning-briefing delivery bug (#2792).

    Reads the deploy checkout's ``<repo>/gallery`` (so canary-pinned and
    HEAD-following checkouts both re-seed from whatever gallery they actually
    run) and writes the evolve-owned builtin tier under ``{shared_dir}``. Runs
    every tick — like the openclaw-config validation hook — because a stale
    builtin is a standing condition, not one gated on this pull's diff.

    Best-effort: never raises. Idempotent — re-seeds only the builtins whose
    repo package version/content actually drifted, writes nothing in the
    steady state.
    """
    try:
        from .applications import migrate_v7 as _mv7
        rs = _mv7.reseed_builtin_specs(
            shared_dir, gallery_root=repo / "gallery",
        )
    except Exception as e:  # noqa: BLE001 — never break the pull on a re-seed
        result.gallery_reseed_error = f"{type(e).__name__}: {e}"
        result.steps.append(f"FAIL builtin re-seed: {result.gallery_reseed_error}")
        return
    if rs.reseeded:
        result.gallery_specs_reseeded = rs.reseeded
        result.steps.append(
            f"re-seeded {len(rs.reseeded)} builtin Spec(s): "
            f"{', '.join(sorted(rs.reseeded))}"
        )
    if rs.errors:
        # Per-package failures: surface but don't fail the pull.
        result.gallery_reseed_error = "; ".join(rs.errors)
        for e in rs.errors:
            result.steps.append(f"FAIL builtin re-seed: {e}")


def _run_lagging_bot_redeploy_sweep(
    result: PullResult, repo: Path, shared_dir: Path
) -> None:
    """Redeploy any bots whose install.json stamp lags EVOLVE_VERSION.

    Closes the gap behind the deploy_drift_monitor signal: every merged
    PR bumps EVOLVE_VERSION (the version string embeds the PR number), so
    bot_versions stamps go stale on every pull unless someone manually
    runs `evolve-admin deploy --all`. Without this sweep, the drift
    signal fires for hours after each merge.

    Best-effort: catches everything, decorates ``result`` with succeeded /
    errored bots, never raises. Called from both the head-moved and
    head-unchanged paths in :func:`pull` (no-op tick still checks for
    lag carried over from earlier ticks).

    Pod-wide deploy lock (C1, deploy-resilience 2026-06-24): the sweep's
    recursive perm-passes must not run concurrently with a manual web upgrade
    (the 2026-06-24 starved-mini double-hammer). Take the NON-BLOCKING deploy
    lock around the sweep only — NOT the git pull / status reads that precede
    it in :func:`pull`. If a manual upgrade holds the lock, skip the sweep this
    tick and retry next tick (direct mode pulls every 15 min, so the lag the
    sweep closes is reopened only briefly).
    """
    from evolve_admin import deploy_resilience as _dres  # local import to avoid cycle at module load

    with _dres.deploy_lock(shared_dir) as _lk:
        if _lk is None:
            # result.steps is the puller's operator-facing log surface
            # (format_for_log renders it); a clear skip line keeps the deferral
            # legible rather than silently no-op'ing the sweep this tick.
            result.steps.append(
                "skipped redeploy sweep: pod deploy lock held (manual upgrade in "
                "progress) — will retry next tick"
            )
            return
        try:
            succeeded, errors = _redeploy_lagging_bots(repo, shared_dir)
        except Exception as e:  # noqa: BLE001 — never break the pull on sweep IO
            result.lagging_bot_deploy_errors = {"__sweep__": f"{type(e).__name__}: {e}"}
            result.steps.append(
                f"FAIL redeploy sweep: {result.lagging_bot_deploy_errors['__sweep__']}"
            )
            return
        if succeeded:
            result.lagging_bots_redeployed = succeeded
            result.steps.append(
                f"redeployed {len(succeeded)} lagging bot(s): "
                f"{', '.join(sorted(succeeded))}"
            )
        if errors:
            result.lagging_bot_deploy_errors = errors
            for bot_id, msg in errors.items():
                result.steps.append(f"FAIL redeploy {bot_id}: {msg}")


# ── LaunchDaemon install ──────────────────────────────────────────────────


REPO_PULLER_LABEL = "ai.evolve.evolve.repo-puller"
REPO_PULLER_PLIST = f"/Library/LaunchDaemons/{REPO_PULLER_LABEL}.plist"
REPO_PULLER_INTERVAL_SECONDS = 900   # 15min — matches v2 worker cadence

# Marker set by `evolve-admin repo-pull` (the daemon's CLI entrypoint)
# before invoking tick(). When install_launchd() sees this, it skips the
# bootout/bootstrap of its own service — running it from inside the
# puller process would SIGTERM us mid-call and leave the daemon silently
# unloaded. Witnessed 2026-05-10: PR #953 added the update-watcher
# daemon (a deploy.py change), the puller's auto-install hook fired,
# install_evolve_infra_jobs reached _rp.install_launchd, bootout self,
# puller died. The whole daemon was offline for ~12h before the
# downstream "Plist not found" warnings surfaced.
PULLER_PROCESS_ENV = "EVOLVE_PULLER_PID"


def render_plist(
    evolve_admin_path: str | None = None,
    interval_seconds: int = REPO_PULLER_INTERVAL_SECONDS,
    log_dir: str | None = None,
) -> str:
    """Render the LaunchDaemon plist content as a string.

    Pure function — does not touch disk. Caller writes via the standard
    /tmp staging + sudo /bin/cp + chown + bootstrap sequence.

    Notes on choices:
    - `UserName=evolve` — the puller needs git write access to
      /Users/Shared/evolve-repo. Repo ownership should be evolve:staff
      after a normal install; if it isn't, this daemon will surface
      the perms problem cleanly via the err log rather than failing
      silently.
    - `umask 002` — files and dirs created by git operations (fetch
      unpacks objects into `.git/objects/<xx>/`, gc rewrites packs)
      must stay group-writable. Default macOS launchd umask is 022,
      which gives mode 755 dirs that lock the human admin user out
      of the same `staff` group. With 002, new dirs land at 775 and
      both the daemon and the operator can write. Pairs with the
      `core.sharedRepository=group` config set up by `install_launchd`.
    - `RunAtLoad=true` — pull immediately on install + on system boot.
      Catches the case where mini boots back from sleep with a stale
      checkout.
    - `StartInterval=900` — 15min, same cadence as the v2 worker.
      Drift between merge-to-main and worker-sees-it stays under
      ~15 min in steady state.
    - `RANDOM % 60` jitter — desyncs from other 15-min daemons that
      share the boundary. See pod-health-invariants.H8. Small (60s)
      so first pull on install/boot stays prompt.
    - No `KeepAlive` — the pull is a short-lived task, not a persistent
      service. launchd respawns it on the interval.
    """
    return render_launchd_plist(_puller_job_spec(
        evolve_admin_path, interval_seconds, log_dir,
    ))


def _puller_job_spec(
    evolve_admin_path: str | None = None,
    interval_seconds: int = REPO_PULLER_INTERVAL_SECONDS,
    log_dir: str | None = None,
) -> JobSpec:
    """The repo-puller LaunchDaemon spec — see :func:`render_plist` for
    the rationale behind each choice.

    Paths default to the active platform profile (W10-D): macOS renders
    /Users/Shared byte-identically; a Linux pod renders /var/lib, so the
    systemd unit carries no /Users leak."""
    from platform_profile import get_profile
    _prof = get_profile()
    if evolve_admin_path is None:
        evolve_admin_path = _prof.venv_evolve_admin
    if log_dir is None:
        log_dir = f"{_prof.shared_dir_default}/logs"
    pull_cmd = " ".join(
        shlex.quote(a) for a in [evolve_admin_path, "repo-pull", "--quiet"]
    )
    return JobSpec(
        label=REPO_PULLER_LABEL,
        # umask must be set BEFORE the sleep + exec so it's in effect when
        # the pull subprocess runs — hence the explicit bash wrapper here
        # rather than JobSpec.jitter_seconds (which has no umask prelude).
        program_args=[
            "/bin/bash", "-c",
            f"umask 002; sleep $((RANDOM % 60)); exec {pull_cmd}",
        ],
        user="evolve",
        start_interval=interval_seconds,
        run_at_load=True,
        stdout_path=f"{log_dir}/repo-puller.log",
        stderr_path=f"{log_dir}/repo-puller.err.log",
        env={
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            # Kill-switch for daemon auto-restart on pull. Default 1 (ON).
            # Edit the installed plist to 0 + `sudo launchctl bootout &&
            # bootstrap` to disable in 30 seconds if auto-restart misbehaves
            # in production. The puller itself keeps running; only the
            # post-pull kickstart of dependent daemons (admin-ui,
            # heal/audit/verify) is skipped.
            AUTO_RESTART_ENV: "1",
        },
    )


# ── Deploy key bootstrap ──────────────────────────────────────────────────


# Platform-keyed: /Users/evolve/.ssh on macOS, /home/evolve/.ssh on Linux.
# A hardcoded /Users/ wrote the deploy key to the wrong (non-existent on
# Linux) path and told the operator to register a key that wasn't there. (W10-G #5.)
EVOLVE_SSH_DIR = Path(_PROFILE.user_home_root) / "evolve" / ".ssh"
DEPLOY_KEY_PATH = EVOLVE_SSH_DIR / "evolve-repo"
SSH_CONFIG_PATH = EVOLVE_SSH_DIR / "config"
SSH_CONFIG_MARKER = "evolve-repo"   # used to detect existing config entry

DEPLOY_KEY_SSH_CONFIG = """
# Deploy key for evolve-repo (used by ai.evolve.evolve.repo-puller daemon)
# Auto-added by evolve-admin install-infra-jobs. The key file is generated
# locally; the public key must be added as a read-only deploy key on the
# GitHub repo for the puller to actually pull.
Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/evolve-repo
    IdentitiesOnly yes
"""


@dataclass
class DeployKeyResult:
    """Outcome of a deploy-key bootstrap attempt."""
    success: bool
    public_key: str = ""           # Full content of the .pub file (one line)
    key_path: str = ""              # Filesystem path to the private key
    config_updated: bool = False    # True if we modified ~/.ssh/config this run
    key_generated: bool = False     # True if we generated a new key this run
    auth_test_ok: bool = False      # True if `ssh -T git@github.com` succeeded
    auth_test_msg: str = ""         # Output of the auth test (success or error)
    steps: list[str] = field(default_factory=list)
    error: str = ""


def ensure_deploy_key(
    key_path: Path = DEPLOY_KEY_PATH,
    ssh_config: Path = SSH_CONFIG_PATH,
    test_auth: bool = True,
    pod_label: str | None = None,
) -> DeployKeyResult:
    """Ensure evolve user has an SSH deploy key for the evolve-repo remote.

    Idempotent. Steps (each skipped if already done):
    1. Generate ed25519 keypair at `key_path` if it doesn't exist.
    2. chown / chmod the key files to evolve:staff with 600/644.
    3. Append SSH config entry to `ssh_config` if not already present.
    4. (Optional) Test auth via `ssh -T git@github.com`. Reports whether
       the operator has finished the GitHub-side step (adding the public
       key as a read-only deploy key).

    The public key is always returned in the result, regardless of whether
    we generated it this run or it was pre-existing — the caller (CLI shim)
    prints it with operator instructions so the operator can copy it on
    install or re-print on demand.

    Must be called as root (writes to /Users/evolve/.ssh/) — caller is
    responsible for ensuring that.
    """
    result = DeployKeyResult(success=False, key_path=str(key_path))

    # Step 1: ensure SSH dir exists with correct perms
    if not EVOLVE_SSH_DIR.exists():
        # Should be impossible on a normal install (evolve user setup creates
        # ~/.ssh) but guard anyway.
        r = subprocess.run(["/bin/mkdir", "-p", str(EVOLVE_SSH_DIR)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            result.error = f"mkdir {EVOLVE_SSH_DIR} failed: {r.stderr.strip()}"
            return result
        subprocess.run([_PROFILE.chown, "evolve:staff", str(EVOLVE_SSH_DIR)],
                       capture_output=True)
        subprocess.run(["/bin/chmod", "700", str(EVOLVE_SSH_DIR)], capture_output=True)
        result.steps.append(f"Created {EVOLVE_SSH_DIR}")

    # Step 2: generate key if absent
    pub_path = key_path.with_suffix(".pub")
    if not key_path.exists():
        # Key comment shows up in GitHub's deploy-key UI — use the pod's
        # actual ssh_target_label (e.g. "pod_admin_user@pod_admins-mac-mini.tail00233d.ts.net")
        # so the operator can identify which pod the key came from. Falls
        # back to a generic label when network config isn't available.
        try:
            from .config import load_network, resolve_pod_context, DEFAULT_NETWORK_CONFIG
            label = pod_label or resolve_pod_context(load_network(DEFAULT_NETWORK_CONFIG)).get("ssh_target_label", "evolve-pod")
        except Exception:
            label = pod_label or "evolve-pod"
        r = subprocess.run([
            "/usr/bin/ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path),
            "-C", f"evolve-repo deploy key ({label})",
        ], capture_output=True, text=True)
        if r.returncode != 0:
            result.error = f"ssh-keygen failed: {r.stderr.strip()}"
            result.steps.append(f"FAIL: {result.error}")
            return result
        subprocess.run([_PROFILE.chown, "evolve:staff", str(key_path), str(pub_path)],
                       capture_output=True)
        subprocess.run(["/bin/chmod", "600", str(key_path)], capture_output=True)
        subprocess.run(["/bin/chmod", "644", str(pub_path)], capture_output=True)
        result.key_generated = True
        result.steps.append(f"Generated {key_path}")
    else:
        result.steps.append(f"Key already present at {key_path}")

    # Step 3: read public key (always — caller may want to re-print it)
    if pub_path.exists():
        result.public_key = pub_path.read_text().strip()
    else:
        result.error = f"public key {pub_path} not found after generation"
        return result

    # Step 4: ensure SSH config has the github.com entry
    existing = ""
    if ssh_config.exists():
        try:
            existing = ssh_config.read_text()
        except OSError:
            pass
    if SSH_CONFIG_MARKER not in existing:
        new_config = (existing.rstrip() + "\n" + DEPLOY_KEY_SSH_CONFIG
                      if existing else DEPLOY_KEY_SSH_CONFIG.lstrip())
        try:
            ssh_config.write_text(new_config)
        except OSError as e:
            result.error = f"writing {ssh_config} failed: {e}"
            return result
        subprocess.run([_PROFILE.chown, "evolve:staff", str(ssh_config)],
                       capture_output=True)
        subprocess.run(["/bin/chmod", "600", str(ssh_config)], capture_output=True)
        result.config_updated = True
        result.steps.append(f"Updated {ssh_config}")
    else:
        result.steps.append(f"{ssh_config} already references evolve-repo")

    # Step 5: optional auth test (sudo -u evolve ssh -T git@github.com)
    # GitHub's expected response on success: "Hi <user>! You've successfully
    # authenticated, but GitHub does not provide shell access." Exit code is 1
    # (because shell access is denied) — we check for the success substring,
    # not the exit code. On auth failure: "Permission denied (publickey)."
    if test_auth:
        r = subprocess.run([
            "sudo", "-u", "evolve",
            "/usr/bin/ssh",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes",
            "-T", "git@github.com",
        ], capture_output=True, text=True, timeout=30)
        out = (r.stdout + r.stderr).strip()
        if "successfully authenticated" in out:
            result.auth_test_ok = True
            result.auth_test_msg = out.split("\n")[0]
            result.steps.append("Auth test: ✓ deploy key accepted by GitHub")
        else:
            result.auth_test_ok = False
            result.auth_test_msg = out.split("\n")[0] if out else f"exit {r.returncode}"
            # Expected on a fresh install: the key was just generated and isn't
            # registered on GitHub yet. Frame it as a pending one-time step, not
            # a ✗ failure that reads as "something is broken" — the actual
            # registration walkthrough (and the fact it's only needed for
            # git-based auto-update/backup) is in format_deploy_key_instructions.
            result.steps.append(
                f"Auth test: deploy key not yet registered on GitHub "
                f"({result.auth_test_msg}) — see the optional one-time setup step below"
            )

    result.success = True
    return result


def format_deploy_key_instructions(result: DeployKeyResult, repo_url: str = "") -> str:
    """Render operator instructions for adding the public key to GitHub.

    `repo_url` is the GitHub repo URL (auto-derived from network config or
    `git remote get-url origin` when not passed — see
    :func:`evolve_admin.config.resolve_repo_url`). If empty after fallback,
    the message just says "your repo's deploy keys".
    """
    if not repo_url:
        try:
            from .config import load_network, resolve_repo_url, DEFAULT_NETWORK_CONFIG
            repo_url = resolve_repo_url(load_network(DEFAULT_NETWORK_CONFIG))
        except Exception:
            repo_url = ""
    # Derive pod label so the operator sees a meaningful GitHub deploy-key title.
    try:
        from .config import load_network, resolve_pod_context, DEFAULT_NETWORK_CONFIG
        pod_label = resolve_pod_context(load_network(DEFAULT_NETWORK_CONFIG)).get("ssh_target_label", "evolve-pod")
    except Exception:
        pod_label = "evolve-pod"
    keys_url = f"{repo_url}/settings/keys/new" if repo_url else "your repo's Settings → Deploy keys"
    if result.auth_test_ok:
        return (
            "[deploy-key] ✓ Auth verified — deploy key already registered on GitHub. "
            "No further action needed."
        )
    return (
        "[deploy-key] Optional one-time step — only needed if you want "
        "automatic code updates or git-based workspace backup. To enable them, "
        "add the public key below as a READ-ONLY deploy key on the repo:\n"
        f"  1. Open: {keys_url}\n"
        f"  2. Title: 'evolve repo-puller ({pod_label})'\n"
        "  3. Key: paste the line below exactly\n"
        "  4. Leave 'Allow write access' UNCHECKED\n"
        "  5. Click 'Add key'\n"
        "\n"
        f"  {result.public_key}\n"
        "\n"
        "After adding: re-run `evolve-admin repo-pull` to confirm. The "
        "ai.evolve.evolve.repo-puller daemon will pick up automatically on "
        "its next 15-min tick."
    )


def ensure_shared_repo_config(
    repo: Path = DEFAULT_REPO,
) -> tuple[bool, str]:
    """Set ``core.sharedRepository = group`` on the deploy checkout.

    Two users (the ``evolve`` daemon and a human admin in the ``staff``
    group) both write to ``/Users/Shared/evolve-repo``. Without this
    config, git creates objects with the inheriting umask — 022 for a
    LaunchDaemon, which yields mode-755 dirs that lock anyone in
    ``staff`` (but not ``evolve``) out of the next ``git pull``.

    With ``core.sharedRepository=group``, every git operation creates
    files 664 and dirs 775+setgid, with the directory's group on new
    children. Combined with the puller's ``umask 002`` and a one-time
    chmod normalization in ``deploy_shared_dir``, both users coexist
    indefinitely.

    Idempotent. Runs ``git config`` as the evolve user (the canonical
    owner of the repo) via sudo. Returns ``(success, message)``.
    """
    if not repo.exists():
        return False, f"repo missing: {repo}"
    if not (repo / ".git").exists():
        # Tarball-staged single-box pod: the deploy checkout is real source
        # but not a git working tree, so `git config` errors "not in a git
        # directory". That is the legitimate single-VPS shape (mirrors
        # pull()'s skipped_not_git no-op), not a failure — return success so
        # the warning stops and health stays green. (W10-G #5.)
        return True, "skip core.sharedRepository: not a git working tree (tarball-staged)"
    # Belt-and-suspenders for the 2026-06-23 freeze: pin core.fileMode=false on
    # a nested Linux deploy checkout at setup too (pull() re-asserts each tick).
    # Done BEFORE the sharedRepository early-return below so it always runs.
    # No-op on the macOS sibling layout. Written as evolve (operator may run setup).
    pin_filemode_off_if_nested(
        repo, Path(_get_profile().shared_dir_default), sudo_evolve=True,
    )
    rc, current, _ = _git(repo, ["config", "core.sharedRepository"])
    if rc == 0 and current == "group":
        return True, "core.sharedRepository already 'group'"
    # Use sudo -u evolve so the resulting .git/config is owned by the
    # daemon user. If we wrote as root or the operator, the daemon's
    # next read could be blocked depending on perms.
    r = subprocess.run(  # sudo-grant: root-only — installer/operator-root context, dropping TO evolve
        ["sudo", "-u", "evolve", "git",
         "-c", f"safe.directory={repo}", "-C", str(repo),
         "config", "core.sharedRepository", "group"],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0:
        return False, (
            f"git config failed (rc={r.returncode}): "
            f"{(r.stderr or r.stdout).strip()[:200]}"
        )
    return True, "set core.sharedRepository=group"


def install_launchd(result_logger=None, repo_url: str | None = None) -> bool:
    """Install ai.evolve.evolve.repo-puller LaunchDaemon.

    Also bootstraps the deploy key for the evolve user if it's not
    already present, and prints operator instructions for adding the
    public key to GitHub. Without a registered deploy key the daemon
    installs fine but every pull will fail; surfacing the instructions
    at install time means the operator catches this in one pass instead
    of investigating later when findings start firing about stale code.

    `result_logger` is an optional callable like `result.log` /
    `result.error` from deploy.py's DeployResult — passed through so
    install steps land in the unified deploy log. If None, prints to
    stdout.

    `repo_url` is the GitHub web URL for the repo (used to render a
    deep link in the operator instructions). When None, auto-resolves
    via ``config.resolve_repo_url`` — reads ``pod.repo_url`` from
    network.json or falls back to ``git remote get-url origin`` on the
    deploy checkout. No hardcoded org/repo default.

    Returns True on success. Idempotent: re-installing replaces the
    existing plist + re-bootstraps; deploy key is preserved if present.
    """
    import os
    import tempfile

    if repo_url is None:
        try:
            from .config import load_network, resolve_repo_url, DEFAULT_NETWORK_CONFIG
            repo_url = resolve_repo_url(load_network(DEFAULT_NETWORK_CONFIG))
        except Exception:
            repo_url = ""

    def log(msg: str) -> None:
        if result_logger is not None:
            result_logger(msg)
        else:
            print(msg)

    plist_content = render_plist()

    # Decide which of three paths to take, then act. The middle branch
    # (plist changed, running inside the puller) exists because a
    # self-bootout SIGTERMs this process before the follow-up bootstrap
    # can register the new plist — the daemon ends up unloaded and
    # silently stays that way until an operator notices. Witnessed
    # 2026-05-10 with PR #953 (see PULLER_PROCESS_ENV comment).
    try:
        existing = Path(REPO_PULLER_PLIST).read_text()
    except OSError:
        existing = None
    plist_changed = existing != plist_content
    running_inside_puller = bool(os.environ.get(PULLER_PROCESS_ENV))

    def _write_plist_to_disk() -> bool:
        """Write to /tmp (sudoers requires /tmp/*.plist), then sudo /bin/cp.
        Returns True on success."""
        fd, tmp_path = tempfile.mkstemp(dir="/tmp", suffix=".plist")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(plist_content)
            cp = subprocess.run(
                ["sudo", "/bin/cp", tmp_path, REPO_PULLER_PLIST],
                capture_output=True, text=True,
            )
            if cp.returncode != 0:
                log(f"[repo-puller install] cp failed: {cp.stderr.strip()}")
                return False
            subprocess.run(["sudo", "/usr/sbin/chown", "root:wheel", REPO_PULLER_PLIST],
                           capture_output=True)
            subprocess.run(["sudo", "/bin/chmod", "644", REPO_PULLER_PLIST],
                           capture_output=True)
            return True
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if not plist_changed:
        # Idempotency win: under the auto-install hook (PR #920),
        # install_evolve_infra_jobs re-runs on every deploy.py-touching
        # pull even though the puller's own plist content rarely changes.
        # Skipping the bootout/bootstrap entirely here is what closes
        # the self-SIGTERM hole for the overwhelmingly common path.
        log(f"[repo-puller install] plist already up to date: {REPO_PULLER_LABEL}")
    elif running_inside_puller and _PROFILE.name == "macos":
        # macOS-only: a launchctl bootout SIGTERMs this very puller before the
        # follow-up bootstrap can register the new plist, so write the plist to
        # disk but skip the self-bootstrap (operator bounces). This hole is
        # specific to launchd's unload-then-load ritual — systemd's seam
        # install does daemon-reload + (re)start without booting the running
        # unit out, so on Linux running_inside_puller falls through to the
        # seam path below (W10-F #8: the macOS REPO_PULLER_PLIST / chown
        # root:wheel write must never run on a systemd pod).
        if not _write_plist_to_disk():
            return False
        log(f"[repo-puller install] plist updated but self-bootstrap "
            f"skipped (running inside puller PID="
            f"{os.environ.get(PULLER_PROCESS_ENV)}). Bounce manually "
            f"to pick up the new content: sudo /bin/launchctl "
            f"kickstart -k system/{REPO_PULLER_LABEL}")
    else:
        # Normal path: the seam's install() runs the full legacy ritual —
        # /tmp staging + sudo /bin/cp + chown root:wheel + chmod 644, then
        # bootout (rc ignored — daemon may not be loaded yet on first
        # install) → unload-settle wait → bootstrap.
        res = get_scheduler().install(_puller_job_spec())
        if not res.ok:
            log(f"[repo-puller install] {res.message} "
                f"(if the plist was written it will activate on next boot)")
            return False
        log(f"[repo-puller install] installed + bootstrapped: {REPO_PULLER_LABEL} "
            f"(every {REPO_PULLER_INTERVAL_SECONDS}s)")

    # Set the shared-repo config so the daemon's writes don't lock out
    # other staff-group users on subsequent manual operations. Failure
    # here is a warning, not a hard stop — the daemon still functions,
    # just with potential umask drift on multi-user installs.
    ok, msg = ensure_shared_repo_config()
    if ok:
        log(f"[repo-puller install] {msg}")
    else:
        log(f"[repo-puller install] WARN: shared-repo config: {msg}")

    # Bootstrap deploy key + print operator instructions. Done after the
    # daemon install so the operator sees one cohesive output: "daemon
    # installed; here's the next step." If we did it before, a daemon-install
    # failure would leave the operator with key instructions for a daemon
    # that isn't running.
    try:
        dk = ensure_deploy_key()
    except Exception as e:
        log(f"[deploy-key] WARN: bootstrap raised: {type(e).__name__}: {e}. "
            f"You can re-run with `sudo evolve-admin repo-pull --setup-key`.")
        return True   # daemon install succeeded; key is fixable later

    if not dk.success:
        log(f"[deploy-key] WARN: bootstrap failed: {dk.error}. "
            f"Re-run with `sudo evolve-admin repo-pull --setup-key`.")
        return True

    for step in dk.steps:
        log(f"[deploy-key] {step}")
    log(format_deploy_key_instructions(dk, repo_url=repo_url))
    return True


def format_for_log(result: PullResult, quiet: bool = False) -> str:
    """Render a result as a single line (or multi-line on advance/error).

    `quiet` mode suppresses no-op pulls (already up to date) so the
    LaunchDaemon's log doesn't grow with one line every 15min.
    Errors and actual advances always print.
    """
    if not result.success:
        return f"[repo-puller] ERROR: {result.error}"
    lines: list[str]
    if result.head_before == result.head_after:
        if quiet and not (result.deleted_identical or result.quarantined):
            return ""
        lines = [f"[repo-puller] up to date at {result.head_before[:8]}"]
    else:
        lines = [
            f"[repo-puller] advanced {result.head_before[:8]}..{result.head_after[:8]} "
            f"({result.commits_advanced} commits)"
        ]
        if result.log_summary:
            lines.append(result.log_summary)
    if result.deleted_identical:
        lines.append(
            f"[repo-puller] swept {len(result.deleted_identical)} untracked "
            f"file(s) identical to origin (deleted): "
            f"{', '.join(result.deleted_identical)}"
        )
    if result.quarantined:
        lines.append(
            f"[repo-puller] quarantined {len(result.quarantined)} divergent "
            f"untracked file(s) → {result.quarantine_dir}: "
            f"{', '.join(result.quarantined)}"
        )
    if result.plugin_rebuilt:
        lines.append("[repo-puller] plugin rebuilt + restaged after pull")
    if result.plugin_rebuild_error:
        lines.append(
            f"[repo-puller] WARN plugin rebuild failed: "
            f"{result.plugin_rebuild_error}"
        )
    if result.restarted_daemons:
        lines.append(
            "[repo-puller] restarted daemons: "
            + ", ".join(result.restarted_daemons)
        )
    for label, info in sorted(result.restart_errors.items()):
        lines.append(
            f"[repo-puller] WARN restart {label} failed: {info}"
        )
    for w in result.restart_warnings:
        lines.append(f"[repo-puller] {w}")
    if result.restart_skipped_disabled:
        lines.append(
            f"[repo-puller] auto-restart disabled "
            f"({AUTO_RESTART_ENV}=0); skipped daemon kickstart"
        )
    if result.sudoers_refresh_attempted:
        if result.sudoers_refresh_ok:
            lines.append("[repo-puller] /etc/sudoers.d/evolve in sync")
        else:
            # info already carries the recovery hint ("run … refresh-sudoers
            # as root") for the drift case, or the reason for the can't-check
            # case — surface it verbatim.
            lines.append(f"[repo-puller] WARN sudoers: {result.sudoers_refresh_info}")
    if result.openclaw_invalid_bots:
        lines.append(
            f"[repo-puller] WARN openclaw config invalid for "
            f"{len(result.openclaw_invalid_bots)} bot(s): "
            + ", ".join(result.openclaw_invalid_bots)
            + " (Signal filed; redeploy to fix)"
        )
    if result.lagging_bots_redeployed:
        lines.append(
            f"[repo-puller] redeployed {len(result.lagging_bots_redeployed)} "
            f"lagging bot(s): " + ", ".join(sorted(result.lagging_bots_redeployed))
        )
    for bot_id, info in sorted(result.lagging_bot_deploy_errors.items()):
        lines.append(
            f"[repo-puller] WARN redeploy {bot_id} failed: {info}"
        )
    if result.bot_gateways_restarted:
        lines.append(
            "[repo-puller] kicked bot gateways: "
            + ", ".join(
                _short_bot_name(l) for l in result.bot_gateways_restarted
            )
        )
    for label, info in sorted(result.bot_gateway_restart_errors.items()):
        lines.append(
            f"[repo-puller] WARN bot-gateway restart "
            f"{_short_bot_name(label)} failed: {info}"
        )
    if result.bot_gateway_discovery_error:
        lines.append(
            f"[repo-puller] WARN bot-gateway discovery failed: "
            f"{result.bot_gateway_discovery_error}"
        )
    return "\n".join(lines)


# ── Invocation guard: `sudo evolve-admin repo-pull` runs as ROOT ──────────
#
# The GitHub deploy key belongs to the `evolve` service user (the
# LaunchDaemon's own account, key under evolve's ~/.ssh/). Root has no
# key, so an operator who follows the CLI help and types
# `sudo evolve-admin repo-pull` gets
#
#     ERROR: pull --ff-only failed: git@github.com: Permission denied (publickey)
#
# — an INVOCATION error, not a wedge. Before 2026-07-31 that failure walked
# the whole wedge path anyway: it filed a puller-stuck incident record and
# paged the alerts channel. Two of the four records on the mini
# (2026-07-01-001, 2026-07-31-001) are exactly this false positive, filed
# while the daemon — running as evolve — was pulling perfectly.
#
# Two layers close it:
#   1. `enforce_evolve_invocation()` — the CLI re-execs the whole command
#      as evolve (same move as `release_manager._maybe_as_evolve`), so the
#      documented `sudo` form just works; when that is impossible it
#      refuses with the exact command to run instead.
#   2. `root_invocation_error()` — `tick()` consults it BEFORE filing, so a
#      root-euid auth failure can never mint an incident or a page even if
#      layer 1 is bypassed (a direct `tick()` call, no evolve account, a
#      third-party wrapper).
#
# Note what is deliberately NOT suppressed: the same auth failure from a
# NON-root euid. That is the daemon's own account failing to authenticate
# (deploy key revoked or removed) — a genuine wedge that must still file
# and page.

EVOLVE_SERVICE_USER = "evolve"

# Set on the re-exec'd child. If we land as root a second time (sudo that
# didn't change user, a container with no evolve account), refuse rather
# than fork forever.
REEXEC_GUARD_ENV = "EVOLVE_REPO_PULL_AS_EVOLVE"

# Substrings git/ssh emit when the INVOKING USER has no usable credential
# for the remote. Matched case-insensitively against the whole error blob.
_AUTH_FAILURE_MARKERS = (
    "permission denied (publickey)",
    "could not read from remote repository",
    "authentication failed",
    "terminal prompts disabled",
)

# ssh never got far enough to authenticate. "Could not read from remote
# repository" trails BOTH shapes, so these win: a pod that can't reach
# github.com has a real problem worth filing, whoever ran the pull.
# (The 2026-07-10-001 record on the mini is exactly this — "ssh: connect
# to host github.com port 22" — and must keep filing.)
_CONNECTIVITY_FAILURE_MARKERS = (
    "connect to host",
    "could not resolve host",
    "network is unreachable",
    "connection timed out",
    "operation timed out",
    "connection refused",
)


def _effective_uid() -> int:
    """Indirection over ``os.geteuid()`` so tests can act like root."""
    return _os.geteuid()


def is_remote_auth_failure(error: str) -> bool:
    """True when `error` is git failing to AUTHENTICATE to the remote.

    Distinct from every other pull failure (non-fast-forward, untracked
    conflict, dirty tree), which are working-tree states the operator has
    to unstick. An auth failure says nothing about the working tree.

    A failure to REACH the remote is not an auth failure even though git
    prints the same trailing line — see _CONNECTIVITY_FAILURE_MARKERS.
    """
    low = (error or "").lower()
    if any(marker in low for marker in _CONNECTIVITY_FAILURE_MARKERS):
        return False
    return any(marker in low for marker in _AUTH_FAILURE_MARKERS)


def evolve_reexec_command(argv: "list[str] | None" = None) -> list[str]:
    """argv that re-runs this exact invocation as the evolve user."""
    argv = list(argv if argv is not None else _sys.argv)
    return [
        "sudo", "-H", "-u", EVOLVE_SERVICE_USER,
        # sudo's env_reset drops inherited env, so the recursion guard
        # rides an explicit `env` prefix (same trick as
        # release_manager._maybe_as_evolve does for PYTHONPATH).
        "env", f"{REEXEC_GUARD_ENV}=1",
        _resolve_admin_bin(argv[0]), *argv[1:],
    ]


def _resolve_admin_bin(argv0: str) -> str:
    """Absolute path to the evolve-admin console script.

    ``sudo`` resolves the command through the OPERATOR's PATH and we hand
    the child a different cwd, so the re-exec has to name an absolute
    path. In preference order:

    1. ``argv[0]`` itself when it names a real file — re-running exactly
       what the operator typed is the only answer that can't silently
       swap in a DIFFERENT installation.
    2. The console script beside the running interpreter (the venv that
       is executing us — what the daemon's plist points at). Note: no
       ``.resolve()``. A venv's ``bin/python3`` is a symlink to the
       system/homebrew interpreter, so resolving it walks out of the venv
       entirely and this lookup always misses.
    3. ``which`` on the bare name, as a last resort — this one CAN pick a
       different install than the operator invoked, so it is the fallback,
       not the first move.
    """
    direct = Path(argv0)
    if direct.name and direct.exists():
        return str(direct.resolve())
    sibling = Path(_sys.executable).parent / direct.name
    if sibling.exists():
        return str(sibling)
    import shutil
    found = shutil.which(direct.name)
    return found or str(direct)


def _evolve_user_exists() -> bool:
    try:
        import pwd
        pwd.getpwnam(EVOLVE_SERVICE_USER)
        return True
    except Exception:
        return False


def format_root_invocation_message(
    *, error: str = "", reason: str = "",
) -> str:
    """Operator-facing explanation + the command that actually works.

    Used both by the CLI refusal (layer 1, when the drop to evolve can't
    happen) and by `tick()`'s log line (layer 2). Deliberately says "no
    incident was filed" — the whole point is that the operator does not
    go hunting for a wedge that never existed.
    """
    bin_path = _resolve_admin_bin(_sys.argv[0] if _sys.argv else "evolve-admin")
    lines = [
        "ERROR: `evolve-admin repo-pull` cannot run as root.",
        "",
        f"  The GitHub deploy key belongs to the `{EVOLVE_SERVICE_USER}` service "
        f"user (the account the",
        "  ai.evolve.evolve.repo-puller LaunchDaemon runs as). Root has no key, so "
        "git",
        "  authenticates with nothing and fails with "
        "\"Permission denied (publickey)\".",
        "",
        "  This is an invocation error, not a wedged puller — no incident was "
        "filed and",
        "  no alert was sent. Run it as the service account instead:",
        "",
        f"      cd /tmp && sudo -H -u {EVOLVE_SERVICE_USER} {bin_path} repo-pull",
        "",
        "  (cd first: sudo keeps the operator's cwd, which the evolve user "
        "usually cannot",
        "  traverse.) The daemon already does this every 15 minutes — a manual "
        "run is only",
        "  needed to pull immediately.",
    ]
    if reason:
        lines += ["", f"  Could not drop privileges automatically: {reason}"]
    if error:
        first = error.splitlines()[0] if error.splitlines() else error
        lines += ["", f"  git said: {first}"]
    return "\n".join(lines)


def root_invocation_error(error: str) -> str:
    """Non-empty operator message when `error` is explained ENTIRELY by
    running as root; empty string otherwise.

    The gate `tick()` consults before filing an incident: root euid AND an
    authentication failure. A root-euid pull that fails to fast-forward is
    still a real wedge and files normally.
    """
    if _effective_uid() != 0 or not is_remote_auth_failure(error):
        return ""
    return format_root_invocation_message(error=error)


def enforce_evolve_invocation(
    *, allow_root: bool = False, runner=None, exit_fn=None,
) -> None:
    """Re-exec as the evolve user when invoked as root, else refuse.

    Called at the top of the `repo-pull` CLI. Non-root invocations (the
    daemon, `sudo -u evolve …`, a developer) return immediately.

    `allow_root=True` is for the sub-modes that genuinely need root —
    `--setup-key` writes into evolve's ~/.ssh and must NOT be dropped.

    Never returns when it acts: it exits with the child's status, or with
    2 (usage error) when the drop is impossible. Exiting 2 rather than 1
    keeps this distinguishable from a real pull failure in scripts.
    """
    if allow_root or _effective_uid() != 0:
        return
    exit_fn = exit_fn or _sys.exit
    runner = runner or subprocess.run

    if _os.environ.get(REEXEC_GUARD_ENV):
        print(format_root_invocation_message(
            reason="already re-exec'd once and still running as root"),
            file=_sys.stderr)
        return exit_fn(2)
    if not _evolve_user_exists():
        print(format_root_invocation_message(
            reason=f"no `{EVOLVE_SERVICE_USER}` user on this host"),
            file=_sys.stderr)
        return exit_fn(2)

    cmd = evolve_reexec_command()
    print(f"[repo-pull] running as root; re-exec as {EVOLVE_SERVICE_USER}: "
          f"{' '.join(shlex.quote(c) for c in cmd)}", file=_sys.stderr)
    try:
        # cwd=/tmp: sudo keeps the operator's cwd (typically the admin
        # user's home), which evolve cannot traverse — python dies in
        # sys.path[0] resolution before main() runs. Output is NOT
        # captured: the operator watches the pull live.
        proc = runner(cmd, cwd="/tmp")  # sudo-grant: root-only — operator `sudo evolve-admin`, dropping TO evolve
    except Exception as exc:
        print(format_root_invocation_message(
            reason=f"{type(exc).__name__}: {exc}"), file=_sys.stderr)
        return exit_fn(2)
    return exit_fn(proc.returncode)


# ── Wedge detection: file a deduped incident record when pull fails ───────
#
# The puller's plain log is the only signal a wedge exists. Between
# 2026-05-02 and 2026-05-03 the daemon failed 34 ticks in a row before
# anyone noticed (historical records: docs/incidents/ in the source
# repo). Filing one record per failure tick would bury the queue with
# 96/day, so dedup windows mirror the v3 worker's rule (see
# tools/skills/evolve-verify-v3/SKILL.md step 8): one open
# `puller-stuck` record per hour, with a `## Recurrences` line appended
# on subsequent ticks within that window.
#
# Records land in DEFAULT_INCIDENTS_DIR under {shared_dir} — never in
# the deploy checkout's working tree, where the untracked record file is
# itself a latent wedge: the moment origin commits a file at the same
# path, `git pull --ff-only` refuses with "untracked working tree files
# would be overwritten by merge". See docs/incidents/README.md.

PULLER_STUCK_KIND = "puller-stuck"
PULLER_STUCK_DEDUP_WINDOW = _dt.timedelta(hours=1)
PULLER_STUCK_TITLE = "repo-puller wedged: pull --ff-only failed"


def _iso_utc(now: _dt.datetime) -> str:
    """Format a datetime as the ISO-8601 UTC string used in issue
    frontmatter (e.g. ``2026-05-03T07:00:58Z``). Caller is responsible
    for passing a timezone-aware UTC datetime."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    return now.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_utc(s: str) -> _dt.datetime | None:
    """Parse an ISO-8601 timestamp like ``2026-05-03T07:00:58Z`` into a
    UTC-aware datetime. Returns None on any malformed input — callers
    treat None as "no timestamp" and fall through to filing a new issue
    rather than crashing the daemon on a hand-edited frontmatter line."""
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.astimezone(_dt.timezone.utc)
    except (ValueError, AttributeError):
        return None


_FRONTMATTER_RE = _re.compile(r"^---\s*\n(.*?)\n---\s*\n", _re.S)


def _read_frontmatter(text: str) -> dict[str, str]:
    """Pull simple `key: value` pairs out of a markdown file's YAML
    frontmatter. Doesn't try to be a real YAML parser — only used to
    read the small flat dict of fields we write below."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip().strip('"')
    return out


def count_stashes(repo: Path) -> int:
    """Return the number of entries in `git stash list` for `repo`.

    Returns 0 if git fails for any reason — this is a soft warning
    signal, not a correctness gate, so any error is treated as "no
    stashes detected" rather than crashing the puller."""
    rc, out, _ = _git(repo, ["stash", "list"])
    if rc != 0:
        return 0
    return sum(1 for line in out.splitlines() if line.strip())


def _next_issue_id(incidents_dir: Path, now: _dt.datetime) -> str:
    """Mint a new `<YYYY-MM-DD-NNN>` issue id by scanning sibling files
    in `incidents_dir` for the highest NNN used today.

    Idempotent: running twice on the same day, with no new issues filed
    in between, returns successive ids."""
    today = _iso_utc(now)[:10]   # "YYYY-MM-DD"
    pattern = _re.compile(rf"^{_re.escape(today)}-(\d+)")
    highest = 0
    if incidents_dir.exists():
        for p in incidents_dir.iterdir():
            m = pattern.match(p.name)
            if m:
                n = int(m.group(1))
                if n > highest:
                    highest = n
    return f"{today}-{highest + 1:03d}"


def _find_recent_puller_stuck_issue(
    incidents_dir: Path, now: _dt.datetime,
    window: _dt.timedelta = PULLER_STUCK_DEDUP_WINDOW,
) -> Path | None:
    """Return the path to an existing `puller-stuck` issue whose
    `last_seen` is within `window` of `now`, or None.

    Walks `incidents_dir/*.md`, parses just enough of the frontmatter to
    match `kind:` and `last_seen:`. Skips any file with malformed or
    missing fields rather than raising — bad files shouldn't take down
    the puller."""
    if not incidents_dir.exists():
        return None
    cutoff = now - window
    for p in sorted(incidents_dir.iterdir()):
        if not p.name.endswith(".md"):
            continue
        try:
            text = p.read_text()
        except OSError:
            continue
        fm = _read_frontmatter(text)
        if fm.get("kind") != PULLER_STUCK_KIND:
            continue
        last_seen = _parse_iso_utc(fm.get("last_seen", ""))
        if last_seen is None:
            continue
        if last_seen >= cutoff:
            return p
    return None


def _bump_existing_puller_stuck_issue(
    path: Path, now: _dt.datetime, error: str,
) -> None:
    """Update `last_seen` and append a `## Recurrences` row to an
    existing puller-stuck issue. Idempotent within a tick: if the
    exact same now-stamp is already the bottom Recurrences row, no
    second row is added (the LaunchDaemon should not double-fire, but
    guard anyway)."""
    text = path.read_text()
    now_iso = _iso_utc(now)

    # Update last_seen in frontmatter.
    text = _re.sub(
        r"(?m)^last_seen:\s*.*$",
        f"last_seen: {now_iso}",
        text,
        count=1,
    )

    rec_line = f"- {now_iso} — {error.splitlines()[0] if error else 'pull failed'}"
    if rec_line.strip() in text:
        path.write_text(text)
        return
    if "## Recurrences" in text:
        text = text.rstrip() + f"\n{rec_line}\n"
    else:
        text = text.rstrip() + f"\n\n## Recurrences\n\n{rec_line}\n"
    path.write_text(text)


def _render_unstick_recipe(
    *,
    ssh_recipe: str,
    upstream_touched: dict[str, list[str]] | None,
    error: str = "",
) -> str:
    """Render the "## Hypothesis" + recipe block for the incident-md body.

    An authentication failure gets its own shape (the working tree is
    irrelevant — git never reached the remote). Reaching this branch at
    all means the DAEMON's account could not authenticate: a root-euid
    auth failure is refused up front by `root_invocation_error()` and
    never files a record.

    Otherwise, two shapes, picked by whether upstream has commits
    touching any of the blocking paths:

    - **Upstream untouched** (no ``upstream_touched`` data) — the local
      diff is unique to this checkout. Preserve it as a stash, pull, then
      either commit it on a ``wip-deploy-*`` branch or pop it.

    - **Upstream touched** — the local diff likely matches an already-
      merged upstream commit (the duplicate-PR case from 2026-06-06:
      someone hand-applied a change that has since landed on origin/main
      as a PR). ``git stash`` + ``git stash pop`` would conflict on pop
      because the change is on both sides. Confirm with
      ``git diff origin/main -- <path>``, then discard local via
      ``git checkout -- <path>`` and ``git pull --ff-only``.

    Listing the upstream commits inline lets the operator spot the
    duplicate-PR case at a glance without rerunning ``git log``.
    """
    if is_remote_auth_failure(error):
        return f"""## Hypothesis

git could not AUTHENTICATE to the remote — it never got far enough to
touch the working tree, so none of the stash/discard recipes apply.

This record was filed by a non-root invocation, i.e. the `{EVOLVE_SERVICE_USER}`
service account the daemon runs as. (A root invocation — `sudo
evolve-admin repo-pull` — has no deploy key by design and is refused
before anything is filed.) So the deploy key itself stopped working:
revoked or removed on the GitHub side, or the key file's
ownership/permissions changed on the pod.

Recipe:

```bash
{ssh_recipe}cd /tmp
# 1. Confirm the service account can no longer authenticate:
sudo -H -u {EVOLVE_SERVICE_USER} ssh -T git@github.com
# 2. Re-print the public key + the GitHub deploy-key instructions:
sudo evolve-admin repo-pull --setup-key
# 3. Re-add the printed key as a deploy key on the repo, then:
sudo -H -u {EVOLVE_SERVICE_USER} evolve-admin repo-pull
```"""

    if not upstream_touched:
        return f"""## Hypothesis

Most common cause: the deployed working tree has uncommitted local
changes (someone debugged on the deploy box and didn't commit), and
`git pull --ff-only` refuses the merge to avoid clobbering them.
Recipe to unstick:

```bash
{ssh_recipe}cd /Users/Shared/evolve-repo
git status                            # confirm what's dirty
git diff > /tmp/evolve-pod-wip.patch  # preserve the diff before stashing
git stash push -m "wip: <what was being debugged> $(date -u +%FT%TZ)"
sudo -u evolve /Users/Shared/evolve-venv/bin/evolve-admin repo-pull
```

If the diff is real WIP, follow up by committing it on a `wip-deploy-*`
branch and pushing — don't leave it in the stash list."""

    commit_lines: list[str] = []
    for path, commits in upstream_touched.items():
        commit_lines.append(f"- `{path}`")
        for c in commits:
            commit_lines.append(f"  - {c}")
    commits_block = "\n".join(commit_lines)

    git_lines: list[str] = []
    for path in upstream_touched:
        git_lines.append(
            f"git diff origin/main -- {path}    # confirm local == upstream"
        )
        git_lines.append(
            f"git checkout -- {path}            # discard local (only after the diff confirms)"
        )
    git_block = "\n".join(git_lines)

    return f"""## Hypothesis

Upstream has commits touching the blocking path(s). The local diff is
likely a duplicate of an already-merged upstream commit (someone hand-
applied a change that has since landed on origin/main as a PR). In that
case `git stash` + `git pull` + `git stash pop` would hit a merge
conflict on pop — the change is on both sides. Confirm the local diff
matches the upstream commit, then discard local.

Upstream commits touching blocking paths:

{commits_block}

Recipe to unstick:

```bash
{ssh_recipe}cd /Users/Shared/evolve-repo
git status                            # confirm what's dirty
{git_block}
git pull --ff-only
sudo -u evolve /Users/Shared/evolve-venv/bin/evolve-admin repo-pull
```

If the diffs do NOT match upstream, the local changes are real WIP —
do NOT discard. Instead, preserve the diff (`git diff > /tmp/wip.patch`)
and stash it before pulling."""


def _write_new_puller_stuck_issue(
    incidents_dir: Path, now: _dt.datetime, error: str,
    *, upstream_touched: dict[str, list[str]] | None = None,
) -> Path:
    """Write a new puller-stuck issue and return its path. Caller has
    already confirmed no recent dup exists.

    ``upstream_touched`` is the ``upstream_commits_touching_blocking_paths``
    field from the wedge Signal payload (built by
    :func:`_build_wedge_signal_details`). When present, the recipe switches
    from the stash shape to the checkout/discard shape — see
    :func:`_render_unstick_recipe` for the rationale.
    """
    incidents_dir.mkdir(parents=True, exist_ok=True)
    now_iso = _iso_utc(now)
    issue_id = _next_issue_id(incidents_dir, now)
    fname = f"{issue_id}-repo-puller-wedged.md"
    # Resolve pod-specific SSH info for the operator recipe so the
    # rendered instructions don't tell readers to `ssh mini` (a host
    # alias from the original developer's ~/.ssh/config). ssh_prefix
    # is e.g. "ssh pod_admin_user@<host> " (with trailing space) or "" when
    # the operator is at the deploy box. ssh_target_label is the
    # bare label without the "ssh " prefix, useful for the YAML
    # frontmatter "instance" field.
    try:
        from .config import load_network, resolve_pod_context, DEFAULT_NETWORK_CONFIG
        _pod_ctx = resolve_pod_context(load_network(DEFAULT_NETWORK_CONFIG))
        ssh_prefix = _pod_ctx.get("ssh_prefix", "")
        ssh_target_label = _pod_ctx.get("ssh_target_label", "evolve-pod")
    except Exception:
        ssh_prefix = ""
        ssh_target_label = "evolve-pod"
    # When ssh_prefix is empty (operator at the box) the recipe shows
    # the local commands directly; when set, it shows the ssh hop first.
    ssh_recipe = (
        f"{ssh_prefix.rstrip()}\n" if ssh_prefix else ""
    )
    hypothesis_section = _render_unstick_recipe(
        ssh_recipe=ssh_recipe, upstream_touched=upstream_touched,
        error=error,
    )
    body = f"""---
id: {issue_id}
kind: {PULLER_STUCK_KIND}
title: "{PULLER_STUCK_TITLE}"
catalog_ref: null
instance: {ssh_target_label}
first_seen: {now_iso}
last_seen: {now_iso}
state: inbox
---

## Symptom

The repo-puller LaunchDaemon (`ai.evolve.evolve.repo-puller`) tried to
fast-forward `/Users/Shared/evolve-repo` to origin/main and failed.
Until this issue is resolved every PR merged to origin/main will fail
to reach the deployed daemons; downstream catalogs will run against
stale code.

## Reproduction

```bash
sudo -u evolve /Users/Shared/evolve-venv/bin/evolve-admin repo-pull
```

## Evidence

- First-seen: {now_iso}
- Error from `git pull --ff-only`:

```
{error}
```

{hypothesis_section}

## Recurrences

- {now_iso} — {error.splitlines()[0] if error else 'pull failed'}
"""
    path = incidents_dir / fname
    path.write_text(body)
    return path


def file_or_update_puller_stuck_issue(
    error: str, now: _dt.datetime,
    *, incidents_dir: Path | None = None,
    window: _dt.timedelta = PULLER_STUCK_DEDUP_WINDOW,
    upstream_touched: dict[str, list[str]] | None = None,
) -> tuple[Path, bool]:
    """File a new puller-stuck incident record, or append a recurrence to
    an existing one within the dedup `window`. Returns (path, was_new).

    `incidents_dir` defaults to ``DEFAULT_INCIDENTS_DIR``
    (`{shared_dir}/repo-puller/incidents/`) — never inside the deploy
    checkout, where an untracked record file can itself wedge the next
    pull. `error` is the raw git error string we want surfaced in the
    record body.

    ``upstream_touched`` is the ``upstream_commits_touching_blocking_paths``
    field from the wedge Signal payload; when provided it switches the
    recipe shape from stash-and-pop to discard-and-pull. See
    :func:`_render_unstick_recipe`. Only consulted on new issues — the
    recurrence path bumps last_seen without rewriting the body.
    """
    incidents = incidents_dir if incidents_dir is not None else DEFAULT_INCIDENTS_DIR
    existing = _find_recent_puller_stuck_issue(incidents, now, window=window)
    if existing is not None:
        _bump_existing_puller_stuck_issue(existing, now, error)
        return existing, False
    return _write_new_puller_stuck_issue(
        incidents, now, error, upstream_touched=upstream_touched,
    ), True


# ── tick(): pull + side effects (issue-on-fail, stash warning) ────────────


@dataclass
class TickResult:
    """Outcome of a `tick()` — wraps a `PullResult` with the side-effect
    state the LaunchDaemon needs to surface."""
    pull: PullResult
    stash_count: int = 0
    stash_warning: bool = False
    issue_path: Path | None = None
    issue_was_new: bool = False
    # Populated when incident-record filing itself failed (e.g. the
    # incidents dir is unwritable). The tick still exits cleanly with
    # the pull failure surfaced — this field keeps the filing failure
    # from being silently swallowed alongside it.
    issue_error: str = ""
    notified: bool = False
    notify_error: str = ""
    # Set when the pull failed for a reason that is ENTIRELY an invocation
    # error (root euid, which has no deploy key) rather than a wedge. When
    # non-empty, tick() filed no incident, sent no page, and emitted no
    # wedge Signal — the string is the operator-facing explanation.
    invocation_error: str = ""
    # Recovery-notification side-channel: only populated on a tick that
    # actually transitioned a firing wedge Signal to resolved (not every
    # successful pull).
    recovery_notified: bool = False
    recovery_notify_error: str = ""


def _send_wedge_notification(
    issue_path: Path, error: str, repo: Path,
) -> tuple[bool, str]:
    """Push a wedge notification through the alerts dispatcher.

    Reuses the alerts config from network.json — same channel as the
    other operator-facing pushers. Returns (sent, error_msg).
    ``sent=False`` with an empty error_msg means a non-error suppression
    (operator muted, no chat_id configured, etc.).

    Only called from ``tick()`` when ``was_new=True``. The wedge issue's
    own dedup window prevents repeat-spam; we never notify on recurrences.

    Phase C of docs/spec-alert-subscriptions-2026-05-10.md: routes
    through alerts.dispatcher.send so operator preferences for
    system.repo_puller_wedged take effect.
    """
    try:
        from evolve_config import (   # local import: optional dependency
            load_config, get_shared_dir, resolve_network_path,
        )
    except ImportError as e:
        return False, f"evolve_config import failed: {e}"

    try:
        cfg = load_config(resolve_network_path())
    except Exception as e:
        return False, f"load_config failed: {e}"

    try:
        from evolve_admin.alerts.dispatcher import (
            send as _dispatch_send, Severity, DispatchResult,
        )
    except Exception as exc:
        return False, f"dispatcher import failed: {exc}"

    try:
        outcome = _dispatch_send(
            shared_dir=Path(get_shared_dir(cfg)),
            network=cfg,
            source="repo_puller",
            payload={
                "repo_name": repo.name,
                "issue_filename": issue_path.name,
                # Full path — the records live under {shared_dir} now,
                # so the filename alone is no longer locatable from the
                # repo the operator is already looking at.
                "incident_path": str(issue_path),
                "error_summary": error.splitlines()[0] if error else "(no detail)",
            },
            severity=Severity.ERROR,
            dedup_key=f"repo_puller/wedge/{issue_path.name}",
            catalog_event="system.repo_puller_wedged",
        )
    except Exception as exc:
        return False, f"dispatcher.send raised: {exc}"

    if outcome.result == DispatchResult.SENT:
        return True, ""
    # Suppressed (operator muted) or no recipient: not an error condition;
    # the wedge issue file on disk is the canonical record either way.
    return False, outcome.error or ""


def _send_recovery_notification(
    repo: Path, pr: "PullResult",
) -> tuple[bool, str]:
    """Push a "recovery" notification through the alerts dispatcher.

    The closing-bracket message to ``_send_wedge_notification``: fires
    on the tick where ``resolve_wedge_signal`` actually cleared a firing
    Signal, never on routine successful ticks. Without this, an operator
    who got the red "puller wedged" alert has no positive confirmation
    that the deploy is current again — they have to ssh and check.

    Same shape as the wedge notifier (network.json → dispatcher.send),
    different catalog event (``system.repo_puller_recovered``) so
    operators can subscribe/mute independently.
    """
    try:
        from evolve_config import (   # local import: optional dependency
            load_config, get_shared_dir, resolve_network_path,
        )
    except ImportError as e:
        return False, f"evolve_config import failed: {e}"

    try:
        cfg = load_config(resolve_network_path())
    except Exception as e:
        return False, f"load_config failed: {e}"

    try:
        from evolve_admin.alerts.dispatcher import (
            send as _dispatch_send, Severity, DispatchResult,
        )
    except Exception as exc:
        return False, f"dispatcher import failed: {exc}"

    # Build the "what landed" summary so the template doesn't render an
    # empty trailing line on no-op recoveries. Show the new HEAD short
    # SHA when we have it — that's the question the operator wants
    # answered ("what code is the deploy on now?").
    if pr.commits_advanced and pr.commits_advanced > 0:
        if pr.head_after:
            advanced = (
                f"Advanced {pr.commits_advanced} commit(s) "
                f"(now on {pr.head_after[:8]})."
            )
        else:
            advanced = f"Advanced {pr.commits_advanced} commit(s)."
    else:
        advanced = "Already up to date — the wedge cleared on its own."

    try:
        outcome = _dispatch_send(
            shared_dir=Path(get_shared_dir(cfg)),
            network=cfg,
            source="repo_puller",
            payload={
                "repo_name": repo.name,
                "advanced_summary": advanced,
            },
            severity=Severity.INFO,
            # One recovery message per resolve transition. Tag the
            # dedup_key with head_after so a subsequent wedge-then-recover
            # cycle gets its own message (instead of the dispatcher's
            # identical-content floor suppressing it).
            dedup_key=f"repo_puller/recovered/{pr.head_after or 'nohead'}",
            catalog_event="system.repo_puller_recovered",
        )
    except Exception as exc:
        return False, f"dispatcher.send raised: {exc}"

    if outcome.result == DispatchResult.SENT:
        return True, ""
    return False, outcome.error or ""


# ── Signal-store integration ──────────────────────────────────────────────
#
# The dispatcher path above is the operator's chat wake-up. The Signal we
# emit here is the parallel structured record — Evo, the Alerts UI, and
# any future consumer can read it via `signals.store` / `action.signal.*`
# and get the evidence directly instead of guessing from priors.
#
# The 2026-06-06 incident: operator asked Evo "help me unwedge repo-puller",
# Evo confabulated "SSH key missing" + recommended a `git stash` / `git pull`
# / `git stash pop` sequence that would have produced a merge conflict
# (the local diff was identical to an already-merged upstream PR). The
# fields below are the cheap evidence that would have falsified Evo's
# diagnosis at zero operator effort — see
# memory/project_evo_confabulation_failure_mode.md.
#
# Direct-dispatch producer rule (see alerts/signal_notifier._DEFAULT_PRODUCERS
# header comment): because this producer ALREADY calls dispatcher.send for
# the chat path, it must NOT be added to that allowlist or the operator
# would get double-messaged. The Signal is read-only state for queryable
# consumers; chat continues to flow through the existing dispatcher call.


# Marker phrases git uses for the two merge-blocking shapes. Match
# case-insensitively because git's wording is stable but capitalization
# isn't (historical variants exist across versions).
_BLOCKING_PATHS_MARKERS = (
    "your local changes to the following files would be overwritten",
    "untracked working tree files would be overwritten",
)


def _parse_blocking_paths(error: str) -> list[str]:
    """Pull tab-indented file paths out of a merge-blocking git error.

    Both common variants — "Your local changes to the following files
    would be overwritten by merge:\\n\\tfoo\\n\\tbar\\n" and "The following
    untracked working tree files would be overwritten by merge:\\n\\tfoo\\n"
    — list one path per tab-indented line between the marker and the
    next non-tab line (typically "Please ..." or "Aborting").

    Returns ``[]`` if no recognised marker is present so the caller can
    distinguish "wedge but not a blocking-paths shape" (e.g. a network
    failure) from "blocking-paths shape but no paths parsed."
    """
    if not error:
        return []
    el = error.lower()
    if not any(m in el for m in _BLOCKING_PATHS_MARKERS):
        return []
    paths: list[str] = []
    in_block = False
    for line in error.splitlines():
        if any(m in line.lower() for m in _BLOCKING_PATHS_MARKERS):
            in_block = True
            continue
        if not in_block:
            continue
        if not line.startswith("\t"):
            break
        path = line.strip()
        if path:
            paths.append(path)
    return paths


def _build_wedge_signal_details(
    repo: Path,
    error: str,
    *,
    head_before: str = "",
    branch: str = DEFAULT_BRANCH,
    issue_path: Path | None = None,
) -> dict:
    """Build the diagnostic payload for the wedged-repo Signal.

    Each sub-step is wrapped in try/except — Signal emission must never
    crash the daemon. Fields that fail to populate are omitted (not set
    to empty) so consumers can distinguish absent from empty.

    The fields, with the failure-mode each one disambiguates:

    - ``last_stderr_tail`` — the literal pull-failure error text. Names
      the blocking file(s) directly in the "your local changes" shape.
      The single most load-bearing field; alone it would have prevented
      Evo's "SSH key missing" 2026-06-06 confabulation.

    - ``fetch_succeeded`` / ``upstream_commits_ahead`` — proves the
      underlying fetch worked. True here rules out SSH / auth / DNS as
      causes; the operator can stop looking there.

    - ``git_status_porcelain`` — working-tree shape (modified/untracked).

    - ``blocking_paths`` — the paths git refused to overwrite.

    - ``upstream_commits_touching_blocking_paths`` — for each blocking
      path, the upstream commits that have touched it. Surfaces the
      "local diff is a duplicate of a merged upstream commit" case:
      when present, the right fix is ``git checkout -- <path>`` (discard
      local), NOT ``git stash`` / ``git stash pop`` (which would conflict
      because the change is on both sides). This is the exact case Evo
      missed in the 2026-06-06 incident.
    """
    details: dict = {
        "repo_path": str(repo),
        "branch": branch,
        "last_stderr_tail": (error or "")[-4000:],
    }
    if head_before:
        details["head_before"] = head_before
    if issue_path is not None:
        try:
            details["incident_md"] = issue_path.name
            details["incident_path"] = str(issue_path)
        except Exception:
            pass

    try:
        rc, out, _ = _git(repo, ["status", "--porcelain"])
        if rc == 0:
            details["git_status_porcelain"] = out[-4000:]
    except Exception:
        pass

    try:
        rc, out, _ = _git(
            repo, ["rev-list", "--count", f"HEAD..origin/{branch}"],
        )
        if rc == 0:
            details["fetch_succeeded"] = True
            try:
                details["upstream_commits_ahead"] = int(out.strip())
            except ValueError:
                pass
        else:
            details["fetch_succeeded"] = False
    except Exception:
        pass

    blocking = _parse_blocking_paths(error)
    if blocking:
        details["blocking_paths"] = blocking
        upstream: dict[str, list[str]] = {}
        # Cap at 10 paths — real wedges name <5; the cap is just a guard
        # against a pathological error message blowing out the payload.
        for p in blocking[:10]:
            try:
                rc, out, _ = _git(
                    repo,
                    ["log", "--oneline", "-n", "5",
                     f"HEAD..origin/{branch}", "--", p],
                )
                if rc == 0 and out.strip():
                    upstream[p] = [
                        line for line in out.splitlines() if line.strip()
                    ]
            except Exception:
                continue
        if upstream:
            details["upstream_commits_touching_blocking_paths"] = upstream
    return details


def _signals_module():
    """Lazy import of signals.store + schema.signal (from the installed
    evolve-analyzer package) so an import failure doesn't crash the
    daemon at import time.

    Returns ``(store_module, schema_module)`` or ``(None, None)`` on any
    failure — callers must check before use.
    """
    try:
        import importlib
        return (
            importlib.import_module("signals.store"),
            importlib.import_module("schema.signal"),
        )
    except Exception:
        return None, None


def observe_wedge_signal(
    repo: Path,
    *,
    error: str,
    head_before: str,
    branch: str,
    issue_path: Path | None,
    shared_dir: Path | None = None,
    details: dict | None = None,
) -> None:
    """Emit (or bump) a Signal capturing the wedge + diagnostic payload.

    Best-effort: any failure (signals module unavailable, shared_dir
    unwritable, etc.) is swallowed. The chat notification + incident
    markdown remain the primary operator-visible paths; the Signal is
    parallel structured state for Evo / Alerts UI / generators.

    ``details`` lets the caller hand in a pre-computed payload (typically
    ``_build_wedge_signal_details(...)``) so the incident-md writer and
    the Signal emitter share one round of git probes. When ``None``, the
    payload is built here from ``repo`` + ``error``.

    Public (no leading underscore) so the test suite can call it
    directly without monkeypatching internals.
    """
    if shared_dir is None:
        shared_dir = DEFAULT_SHARED_DIR
    store, schema = _signals_module()
    if store is None or schema is None:
        return
    if details is None:
        try:
            details = _build_wedge_signal_details(
                repo, error,
                head_before=head_before, branch=branch, issue_path=issue_path,
            )
        except Exception:
            details = {
                "repo_path": str(repo),
                "last_stderr_tail": (error or "")[-4000:],
            }
    try:
        signature = schema.make_signature(
            "repo_puller", "repo_puller_wedged", repo.name,
        )
    except Exception:
        signature = f"repo_puller:repo_puller_wedged:{repo.name}"
    try:
        store.observe(
            shared_dir,
            signature=signature,
            producer="repo_puller",
            type="repo_puller_wedged",
            flavor="maintenance",
            scope="pod",
            title=f"repo-puller wedged: pull --ff-only failed ({repo.name})",
            body=(error or "")[:1000],
            details=details,
        )
    except Exception:
        pass


def observe_sudoers_refresh_failed_signal(
    *,
    error: str,
    head: str,
    shared_dir: Path | None = None,
) -> None:
    """Emit (or bump) a Signal when the installed sudoers lags the render.

    Fired by the puller's drift check when grants merged in recent PRs are not
    yet installed (the evolve user can't install them — Option B, #2759 — so an
    operator runs ``refresh-sudoers`` as root). ``error`` carries the drift
    detail. Auto-resolved by ``resolve_sudoers_refresh_signal`` on the next pull
    once the install matches the render.

    Closes the gap that f294255e surfaced: PR #1909 wrapped
    ``setup_wizard._write_evolve_sudoers`` for the puller but called it
    with a kwarg the helper didn't accept, so every auto-refresh crashed
    with TypeError. The wrapper caught the exception into
    ``result.sudoers_refresh_info`` and emitted ``[repo-puller] WARN
    sudoers refresh failed: ...``, but the operator only sees the puller
    log file if they tail it. The class is "monitor wrote correctly,
    operator's chat stayed silent" — same shape as the 2026-06-03 outage
    that motivated the pod_report/permission_monitor allowlist additions.

    Producer name is ``repo_puller_sudoers`` (not ``repo_puller``) so the
    wedge sweep — which archives every active Signal under producer
    ``repo_puller`` on a clean pull — doesn't clobber a still-firing
    refresh failure. The two conditions are independent: a refresh can
    fail on a pull that itself succeeded.

    Best-effort: any failure (signals module unavailable, shared_dir
    unwritable) is swallowed. The WARN line in the puller log remains
    the primary operator-visible path; the Signal is parallel structured
    state for chat dispatch via signal_notifier and the Alerts UI.
    """
    if shared_dir is None:
        shared_dir = DEFAULT_SHARED_DIR
    store, schema = _signals_module()
    if store is None or schema is None:
        return
    try:
        signature = schema.make_signature(
            "repo_puller_sudoers", "sudoers_refresh_failed", "evolve",
        )
    except Exception:
        signature = "repo_puller_sudoers:sudoers_refresh_failed:evolve"
    details: dict = {
        "error": (error or "")[:4000],
        "recovery_command": "sudo evolve-admin refresh-sudoers",
    }
    if head:
        details["head_after"] = head
    try:
        store.observe(
            shared_dir,
            signature=signature,
            producer="repo_puller_sudoers",
            type="sudoers_refresh_failed",
            flavor="maintenance",
            scope="pod",
            # Voice: Tier A lead (docs/voice-guide.md) — what happened + the one
            # action, THEN the technical detail. The old title led with
            # "sudoers grants dormant", which is meaningless to the operator.
            title="One command needed to finish an Evolve update",
            body=(
                "Evolve pulled an update that needs new admin permissions, but "
                "for safety it can't grant those to itself — an operator has to "
                "approve them by running one command on the pod (as root):\n\n"
                "sudo evolve-admin refresh-sudoers\n\n"
                "Until then, the newest changes stay dormant. This alert clears "
                "itself once the command has run.\n\n"
                f"Technical detail: {(error or '')[:400]}"
            ),
            details=details,
        )
    except Exception:
        pass


def resolve_sudoers_refresh_signal(
    shared_dir: Path | None = None,
) -> list:
    """Auto-resolve a firing sudoers-refresh-failed Signal once refresh succeeds.

    Sweep-style: empty kept_signatures scoped to producer
    ``repo_puller_sudoers`` archives any active Signal. Single-producer
    so the sweep is exactly right.

    Returns the list of Signals that were resolved.
    """
    if shared_dir is None:
        shared_dir = DEFAULT_SHARED_DIR
    store, _ = _signals_module()
    if store is None:
        return []
    try:
        return store.sweep_resolve(
            shared_dir,
            producer="repo_puller_sudoers",
            kept_signatures=set(),
            reason="auto-resolve: sudoers auto-refresh succeeded",
        )
    except Exception:
        return []


def resolve_wedge_signal(
    repo: Path,
    *,
    shared_dir: Path | None = None,
) -> list:
    """Auto-resolve a firing wedge Signal once a pull succeeds.

    Sweep-style: empty kept_signatures scoped to producer ``repo_puller``
    archives any active wedge Signal. Multiple deploy checkouts on the
    same host would share this producer; today the pod has one, so the
    sweep is exactly right. If we ever support multiple, switch to
    passing the still-firing signatures.

    Returns the list of Signals that were resolved. Empty list means
    nothing was firing; non-empty means a recovery just happened and
    the caller should send a recovery notification (the "closing
    bracket" to the original wedge alert).
    """
    if shared_dir is None:
        shared_dir = DEFAULT_SHARED_DIR
    store, _ = _signals_module()
    if store is None:
        return []
    try:
        return store.sweep_resolve(
            shared_dir,
            producer="repo_puller",
            kept_signatures=set(),
            reason="auto-resolve: pull --ff-only succeeded",
        )
    except Exception:
        return []


def tick(
    repo: Path = DEFAULT_REPO,
    remote: str = DEFAULT_REMOTE,
    branch: str = DEFAULT_BRANCH,
    *,
    now: _dt.datetime | None = None,
    stash_warn_threshold: int = 3,
    quarantine_root: Path = DEFAULT_QUARANTINE,
    incidents_dir: Path | None = None,
    notifier: "callable | None" = None,
    recovery_notifier: "callable | None" = None,
    kickstart_fn: "callable | None" = None,
) -> TickResult:
    """Run one puller tick: `pull()` + dedup-file-issue-on-fail +
    stash-count warning on success.

    The LaunchDaemon calls this via the CLI shim; tests can call it
    directly with `now=` set for deterministic timestamps. Never
    raises — every error path lands in the result so the daemon's
    exit code stays clean (success → 0, pull failure → 1).

    `incidents_dir` overrides where the puller-stuck incident record is
    filed on failure; defaults to `DEFAULT_INCIDENTS_DIR` under
    `{shared_dir}` (never the deploy checkout — see
    `file_or_update_puller_stuck_issue`). Tests pass a tmp dir.

    `notifier` is the function used to send a wedge alert on a NEW issue
    (not recurrences — those are noise after the first page). Defaults
    to `_send_wedge_notification`; tests inject a stub.

    `recovery_notifier` is the closing-bracket dispatcher — called only
    on the tick where a previously-firing wedge Signal actually clears
    (never on routine successful ticks). Defaults to
    `_send_recovery_notification`; tests inject a stub.
    """
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    if notifier is None:
        notifier = _send_wedge_notification
    if recovery_notifier is None:
        recovery_notifier = _send_recovery_notification

    pr = pull(repo=repo, remote=remote, branch=branch,
              quarantine_root=quarantine_root, now=now,
              kickstart_fn=kickstart_fn)
    out = TickResult(pull=pr)

    if not pr.success:
        # Invocation error, not a wedge: running as root, which has no
        # deploy key, so git could never have authenticated. Filing here
        # is what produced the 2026-07-01-001 / 2026-07-31-001 false
        # positives (and paged the alerts channel for both). Return
        # before the incident writer, the notifier, and the Signal.
        # An auth failure from the DAEMON's euid still falls through.
        invocation = root_invocation_error(pr.error)
        if invocation:
            out.invocation_error = invocation
            return out
        # Build the diagnostic payload ONCE so the incident-md writer and
        # the Signal emitter share one round of git probes (status, rev-
        # list, per-blocking-path log). Both consumers read the same
        # upstream_commits_touching_blocking_paths field — the field that
        # distinguishes "stash + pull" from "checkout + pull".
        try:
            wedge_details = _build_wedge_signal_details(
                repo, pr.error, head_before=pr.head_before, branch=branch,
            )
        except Exception:
            wedge_details = {}
        upstream_touched = wedge_details.get(
            "upstream_commits_touching_blocking_paths"
        )
        try:
            path, was_new = file_or_update_puller_stuck_issue(
                error=pr.error, now=now, incidents_dir=incidents_dir,
                upstream_touched=upstream_touched,
            )
            out.issue_path = path
            out.issue_was_new = was_new
            if was_new:
                # Page on first sighting only — recurrences keep the
                # issue file fresh but don't re-alert. The existing log
                # line ("appended recurrence to ...") is the durable
                # signal for ongoing wedges.
                try:
                    sent, nerr = notifier(path, pr.error, repo)
                    out.notified = sent
                    out.notify_error = nerr
                except Exception as e:
                    # Notifier failure must never crash the daemon —
                    # the issue is still on disk and surfaces via health.
                    out.notify_error = f"{type(e).__name__}: {e}"
        except Exception as e:
            # Never let an issue-filing problem stop the daemon from
            # exiting cleanly with the original failure surfaced — but
            # record it: a swallowed filing failure also suppresses the
            # wedge notification (the notifier needs the record path),
            # so without this the only trace would be the missing file.
            out.issue_error = f"{type(e).__name__}: {e}"
        # Backfill issue_path into the shared payload so observe_wedge_signal
        # records it in details.incident_md without recomputing anything.
        if out.issue_path is not None:
            try:
                wedge_details.setdefault("incident_md", out.issue_path.name)
                wedge_details.setdefault("incident_path", str(out.issue_path))
            except Exception:
                pass
        elif out.issue_error:
            # Filing failed — put the tooling failure on the Signal too,
            # so Evo / Alerts UI see "record could not be written" rather
            # than a wedge that mysteriously lacks its incident_md.
            wedge_details.setdefault("incident_filing_error", out.issue_error)
        # Parallel structured record so Evo / Alerts UI / generators
        # have evidence to read instead of guessing. Best-effort; failures
        # are swallowed inside observe_wedge_signal.
        observe_wedge_signal(
            repo=repo,
            error=pr.error,
            head_before=pr.head_before,
            branch=branch,
            issue_path=out.issue_path,
            details=wedge_details if wedge_details else None,
        )
        return out

    # Pull succeeded — clear any firing wedge Signal AND send a recovery
    # notification iff something actually transitioned. The "only on
    # transition" gate is what stops the operator from getting "puller
    # is fine!" pings every 15 minutes on a healthy pod — only the tick
    # where the wedge cleared is interesting.
    resolved = resolve_wedge_signal(repo=repo)
    if resolved:
        try:
            sent, nerr = recovery_notifier(repo, pr)
            out.recovery_notified = sent
            out.recovery_notify_error = nerr
        except Exception as e:
            # Recovery notification failure must never crash the daemon —
            # the Signal transition has already landed (Alerts UI reflects
            # the clear); the chat message is best-effort.
            out.recovery_notify_error = f"{type(e).__name__}: {e}"

    out.stash_count = count_stashes(repo)
    if out.stash_count > stash_warn_threshold:
        out.stash_warning = True
    return out


def format_tick_for_log(result: TickResult, quiet: bool = False) -> str:
    """Render a TickResult for the LaunchDaemon log.

    Wraps `format_for_log(result.pull)` and appends the stash-count line
    (always, on success — operators want to see the trend) plus the
    issue-filing line (on failure, distinguishing new vs recurrence)."""
    base = format_for_log(result.pull, quiet=quiet)
    extras: list[str] = []
    if result.pull.success:
        extras.append(f"[repo-puller] stashes={result.stash_count}")
        if result.stash_warning:
            extras.append(
                f"[repo-puller] WARNING: {result.stash_count} stashes accumulated; "
                "investigate before they grow further."
            )
        # Recovery line is only populated on the tick that actually
        # cleared a wedge — leaves the routine-success path unchanged.
        if result.recovery_notified:
            extras.append("[repo-puller] notified alerts channel: recovery")
        elif result.recovery_notify_error:
            extras.append(
                f"[repo-puller] recovery notify FAILED: {result.recovery_notify_error}"
            )
    elif result.invocation_error:
        # Not a wedge — nothing was filed and nothing was paged. Print the
        # command that actually works instead of a bare git error.
        extras.append(result.invocation_error)
    elif result.issue_path is not None:
        verb = "filed" if result.issue_was_new else "appended recurrence to"
        # Full path, not just the name — the records moved out of the
        # deploy checkout, so the location is no longer guessable from
        # the repo the operator is already looking at.
        extras.append(f"[repo-puller] {verb} {result.issue_path}")
        if result.issue_was_new:
            if result.notified:
                extras.append("[repo-puller] notified alerts channel")
            elif result.notify_error:
                extras.append(
                    f"[repo-puller] notify FAILED: {result.notify_error}"
                )
    elif result.issue_error:
        # Filing itself failed — and with it the wedge notification,
        # which only fires on a freshly-filed record. Say so explicitly;
        # a missing "filed ..." line is too easy to read past.
        extras.append(
            f"[repo-puller] incident filing FAILED: {result.issue_error}"
        )
    if not base and not extras:
        return ""
    parts = [p for p in [base, *extras] if p]
    return "\n".join(parts)
