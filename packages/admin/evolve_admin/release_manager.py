"""release_manager — gated release pipeline for the deploy checkout (7.2).

Spec: docs/spec-state-store-and-deploy-resilience-2026-06-10.md Part 2.

Replaces the repo-puller's direct ``git pull --ff-only origin main`` with
a promoted-release model when ``pod.release.mode == "canary"``:

    origin/main ─fetch─▶ CANDIDATE ─Gate 1─▶ soaking ─Gate 2─▶ STABLE ─▶ fleet

- The fleet checkout (``/Users/Shared/evolve-repo``) only ever sits at
  the promoted ``stable`` pointer — never raw origin/main. It stays on
  branch ``main`` (existing tooling asserts the branch name), moved by
  ``git reset --hard <sha>`` at promote/rollback.
- Candidates are evaluated in per-candidate staging worktrees under
  ``/Users/Shared/evolve-staging/<short-sha>/`` — immutable for the
  candidate's lifetime, pruned when the candidate resolves. A newer
  origin/main sha *replaces* an in-flight candidate (fresh worktree,
  clocks reset), so a burst of merges batches into one release.
- Gate 1 (static): compileall + import-smoke with staging PYTHONPATH
  (PYTHONPATH wins over the venv's editable install — verified), a
  staging *venv* when the candidate touches pyproject.toml (a
  dep-adding PR must not deadlock the pipeline), and a plugin build
  check when the candidate touches packages/plugin/.
- Gate 2 (canary): the configured canary bot is deployed running
  staging code (pod side effects suppressed) and soaks for
  ``soak_minutes``; new firing Signals scoped to the canary fail the
  soak. No canary configured → degraded mode (Gate 1 + timer).
- Promote: ancestry-checked ``reset --hard``, then the existing
  post-pull hook suite runs in a **fresh subprocess** from the new
  checkout (``evolve-admin repo-pull --hooks-from … --hooks-to …``) so
  EVOLVE_VERSION stamps the post-promote version. The canary is
  redeployed from the fleet checkout before its worktree is pruned
  (its plists bake staging paths).
- Rollback (``evolve-admin release rollback``): one command — cancels
  any in-flight soak, restores the canary, resets the fleet to the
  target, runs the hook suite, pins, and skip-lists the fled sha.

State machine is persisted in ``{shared_dir}/release.json`` — the tick
is a stateless 15-minute LaunchDaemon, the file carries the pipeline
across ticks. The file is the authoritative pointer: a fleet HEAD that
disagrees with it gets repaired back (out-of-band git surgery cannot
silently fork the fleet; use ``release rollback``/``pin`` instead).

Corrupt release.json freezes promotion and fires a Signal — never
silently reinitialized (a pin set by a rollback must not evaporate).
``evolve-admin release init`` is the explicit recovery path.

Everything that touches the host (git, subprocesses, deploys, signals)
is injectable for tests — see ``ReleaseDeps``.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Shared with the deploy installer so the staging-venv build and the
# canonical-venv build resolve uv the same way (W7c — see _ensure_staging_venv).
from evolve_admin.installer import _find_uv
from platform_profile import get_profile

# Deploy checkout, platform-keyed: /Users/Shared/evolve-repo on macOS,
# /var/lib/evolve/repo on Linux (byte-identical on macOS).
DEFAULT_REPO = Path(get_profile().deploy_checkout_default)
DEFAULT_SHARED_DIR = Path("/Users/Shared/evolve")
DEFAULT_STAGING_ROOT = Path("/Users/Shared/evolve-staging")
DEFAULT_STAGING_VENV = Path("/Users/Shared/evolve-venv-staging")
DEFAULT_VENV_PYTHON = "/Users/Shared/evolve-venv/bin/python3"
DEFAULT_EVOLVE_ADMIN = "/Users/Shared/evolve-venv/bin/evolve-admin"
DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"

# Full-tier RESIDUAL ceiling (minutes) — D7. The release timer is no
# longer the primary promote gate; active validation (D5 gateway liveness
# + D2 changed-surface probes + D4 baseline-diff health) is. This is the
# short residual *dwell* the irreversible / privileged `full` minority must
# still sit through after active validation passes — the only concession to
# slow / accumulating failures that can't be cheaply forced. Dropped 60→15
# under D7 (it was the universal passive window before). NOT the universal
# default: an active-validated `skip`/`short` promotes at active-pass time
# (residual 0); see tier_residual_minutes. Configurable via
# `pod.release.soak_minutes`.
DEFAULT_SOAK_MINUTES = 15

# Local tags moved at promote/rollback — the git-native face of the
# release pointer ("what is the fleet running" without release.json).
STABLE_TAG = "evolve-stable"
PREVIOUS_TAG = "evolve-previous"

# Mode resolution: env beats network.json beats the code default.
# "direct" is the code default and STAYS the default — instant deploy +
# Gate-1 static scan + one-command rollback is the right ergonomic for a
# single or small, supervised fleet. "canary" is the opt-in posture for
# large/unsupervised fleets (Managed on the footprint dial). The earlier
# plan to flip the default to canary is cancelled; see
# docs/spec-delta-updates-vocabulary-and-direct-default-2026-06-22.md.
RELEASE_MODE_ENV = "EVOLVE_RELEASE_MODE"
DEFAULT_RELEASE_MODE = "direct"

# Modules imported by Gate 1's import-smoke — the daemon entry points
# whose import-time crash class (missing dep, syntax-adjacent, module-
# scope undefined name) is what historically broke fleets. Keep cheap:
# this list runs on every candidate.
IMPORT_SMOKE_MODULES: tuple[str, ...] = (
    "evolve_admin.repo_puller",
    "evolve_admin.deploy",
    "evolve_admin.release_manager",
    "evolve_admin.web.server",
    "signals.store",
    "arbiter.store",
    "generator_runner",
)

# Signal identities (producer is shared; signatures distinguish class).
SIGNAL_PRODUCER = "release_manager"


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(dt: _dt.datetime | None = None) -> str:
    return (dt or _utc_now()).isoformat(timespec="seconds")


def _parse_iso(s: str | None) -> _dt.datetime | None:
    if not s:
        return None
    try:
        return _dt.datetime.fromisoformat(s)
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Release state (release.json)
# ─────────────────────────────────────────────────────────────────────────────


class ReleaseStateCorrupt(RuntimeError):
    """release.json exists but cannot be parsed — promotion freezes."""


def release_state_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / "release.json"


@dataclass
class ReleaseState:
    """In-memory mirror of release.json. ``stable``/``previous`` are
    ``{"sha","version","promoted_at"}`` dicts; ``candidate`` is None or
    ``{"sha","first_seen_at","soak_started_at","state","failure"}``
    where state ∈ checking | soaking | failed. Once Gate 1 classifies it
    the candidate also carries ``soak_tier`` ∈ skip | short | full (absent
    on candidates predating the tier policy → full), an optional operator
    ``soak_override_minutes`` (set by ``release soak``), and the D7
    ``soak_active_validated`` bool stamped at soak entry (True only when the
    active probe returned OK on a real canary — gates the residual-0
    fast-track). All ride through ``from_dict``/``to_dict`` inside the
    candidate dict — no separate schema field."""
    stable: dict[str, Any]
    previous: dict[str, Any] | None = None
    candidate: dict[str, Any] | None = None
    skip: list[str] = field(default_factory=list)
    pin: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable": self.stable,
            "previous": self.previous,
            "candidate": self.candidate,
            "skip": list(self.skip),
            "pin": self.pin,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ReleaseState":
        if not isinstance(raw, dict) or not isinstance(raw.get("stable"), dict) \
                or not raw["stable"].get("sha"):
            raise ReleaseStateCorrupt("release.json missing stable.sha")
        return cls(
            stable=dict(raw["stable"]),
            previous=dict(raw["previous"]) if raw.get("previous") else None,
            candidate=dict(raw["candidate"]) if raw.get("candidate") else None,
            skip=[s for s in (raw.get("skip") or []) if isinstance(s, str)],
            pin=dict(raw["pin"]) if raw.get("pin") else None,
        )


def refold_operator_fields(state: ReleaseState, shared_dir: Path) -> None:
    """Fold operator actions written to disk mid-tick back into the
    tick's in-memory snapshot before saving it.

    The tick loads state once, then spends minutes in gates; a
    `release pin` / failed-candidate skip landed by the CLI in that
    window must not be clobbered by the tick's save. Pin: disk wins.
    Skip: additive union (a `release retry` issued mid-tick can be
    re-skipped by the stale snapshot — rare, self-healing via a second
    retry, and preferable to a merge protocol). Best-effort: unreadable
    disk state leaves the snapshot as-is.
    """
    try:
        disk = load_release_state(shared_dir)
    except ReleaseStateCorrupt:
        return
    if disk is None:
        return
    state.pin = disk.pin
    for sha in disk.skip:
        if sha not in state.skip:
            state.skip.append(sha)


def load_release_state(shared_dir: Path) -> ReleaseState | None:
    """None = file genuinely missing (first run). Raises
    :class:`ReleaseStateCorrupt` on unparseable content — callers must
    freeze promotion, not reinitialize."""
    path = release_state_path(shared_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ReleaseStateCorrupt(f"release.json unreadable: {e}") from e
    return ReleaseState.from_dict(raw)


def save_release_state(state: ReleaseState, shared_dir: Path) -> None:
    path = release_state_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".release.json.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh, indent=2)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Pod release config
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ReleaseConfig:
    mode: str = DEFAULT_RELEASE_MODE          # "direct" | "canary"
    canary_bot: str = ""                       # "" → degraded mode
    soak_minutes: int = DEFAULT_SOAK_MINUTES


def resolve_release_config(network: dict | None) -> ReleaseConfig:
    """``pod.release.*`` from network.json, with the env var trumping
    mode (30-second disable, same pattern as EVOLVE_PULLER_AUTO_RESTART)."""
    rel = {}
    if isinstance(network, dict):
        pod = network.get("pod") or {}
        if isinstance(pod, dict):
            rel = pod.get("release") or {}
            if not isinstance(rel, dict):
                rel = {}
    mode = str(rel.get("mode") or DEFAULT_RELEASE_MODE).strip().lower()
    env_mode = os.environ.get(RELEASE_MODE_ENV, "").strip().lower()
    if env_mode in ("direct", "canary"):
        mode = env_mode
    if mode not in ("direct", "canary"):
        mode = DEFAULT_RELEASE_MODE
    canary = str(rel.get("canary_bot") or "").strip()
    try:
        soak = int(rel.get("soak_minutes", DEFAULT_SOAK_MINUTES))
    except (TypeError, ValueError):
        soak = DEFAULT_SOAK_MINUTES
    return ReleaseConfig(mode=mode, canary_bot=canary, soak_minutes=max(0, soak))


# ─────────────────────────────────────────────────────────────────────────────
# Admin-UI views (pure — no IO; assembled from a loaded config + state so
# the Overview sync banner can read release/canary state without a git
# subprocess on every /api/status poll). See docs/spec-state-store-and-
# deploy-resilience-2026-06-10.md + the canary-aware banner change.
# ─────────────────────────────────────────────────────────────────────────────


def release_ui_view(
    cfg: "ReleaseConfig", state: "ReleaseState | None", *, corrupt: bool = False,
) -> dict | None:
    """Compact release/canary state for the Overview update banner.

    Returns ``None`` in direct mode — the banner then keeps its legacy
    per-bot version-equality behavior untouched. Under canary it returns::

        {
          "mode": "canary",
          "canary_bot": "<bot_id>" | None,     # exempt from "behind" count
          "stable_version": "2026.0611.2730",  # the promoted fleet pointer
          "stable_commit_date": "2026-06-11" | None,  # ancestry-true recency
          "stable_short_sha": "<12-char>" | None,
          "stable_commits_ahead": 5 | None,    # stable is N ahead of previous
          "previous_version": "2026.0613.2885" | None,  # what stable replaced
          "stable": {                           # D-8 nested mirror (see below)
            "promoted_at": "<iso>" | None,      # last pointer-move timestamp
          },
          "candidate": {                        # None when nothing in flight
            "state": "checking" | "soaking" | "failed",
            "soak_started_at": "<iso>" | None,  # banner derives the ETA
            "sha": "<12-char>",
            "commit_date": "2026-06-14" | None,  # candidate's commit date
            "commits_ahead": 3 | None,          # candidate is N ahead of stable
            # The full candidate state-dict (not surfaced in this view) also
            # carries soak_baseline_signatures: [..] — the canary's
            # stable-code active-signal signatures captured at soak entry,
            # which the soak health check diffs against (D4) so re-firing
            # standing app-debt no longer fails the gate.
          } | None,
          "pin": {"sha","pinned_at","reason"} | None,  # auto-promote freeze
          "corrupt": False,                     # release.json unparseable
          "soak_minutes": 60,
        }

    Pure: no git, no disk. The recency fields (commit_date / commits_ahead)
    are read straight from release.json where ``_stamp_recency`` wrote them at
    pointer-move / candidate-detect time — so the per-poll status path never
    shells out to git. They are ``None`` on pre-D-2 state (no backfill); the
    banner then leads with the date-led version label, which already carries
    recency in its ``YYYY.MMDD`` prefix. The PR-number tail is never compared.

    D-8 state→representation deltas (docs/spec-deploy-meta-2026-06-14.md, and
    the field-name oracle docs/fixtures/deploy-updates-cell-states.json — the
    shape the Overview ``_classifyUpdates`` reads). Read-only surfacing; none
    of this alters promotion/soak/pin/rollback behavior:

    * ``stable.promoted_at`` — the last pointer-move timestamp, surfaced under
      a nested ``stable`` mirror (alongside the flat ``stable_*`` fields, which
      stay) because ``_classifyUpdates`` reads ``rel.stable.promoted_at``. This
      powers the D-3 fold: transient post-promote lag (within ~30 min) reads
      ``lag-redeploying`` (quiet) instead of ``lag-stuck`` (loud). Absent →
      lag fails loud (treated as stuck), the safe default.
    * ``pin`` — the ``state.pin`` dict (``None`` when auto-promotion is not
      frozen). ``_classifyUpdates`` derives ``pinned = !!pin && !corrupt`` and
      reads ``pin.reason``; the canonical predicate is ``state.pin is not
      None``. Surfaced as the object (not a bare bool) to match the contract.
    * ``corrupt`` — passed IN by the caller (detection stays at the I/O
      boundary so this stays pure). ``True`` when release.json failed to parse;
      the cell then shows ``pipeline-halted``. This is *surfacing only* — a
      corrupt release.json already freezes promotion upstream, unchanged here.
    """
    if cfg.mode != "canary":
        return None
    stable_version = None
    stable_commit_date = None
    stable_short_sha = None
    stable_promoted_at = None
    previous_version = None
    if state is not None and isinstance(state.stable, dict):
        stable_version = state.stable.get("version") or None
        stable_commit_date = state.stable.get("commit_date") or None
        stable_short_sha = (state.stable.get("sha") or "")[:12] or None
        stable_promoted_at = state.stable.get("promoted_at") or None
    if state is not None and isinstance(state.previous, dict):
        previous_version = state.previous.get("version") or None
    pin_view = dict(state.pin) if state is not None and state.pin else None
    cand = None
    if state is not None and state.candidate:
        cand = {
            "state": state.candidate.get("state"),
            "soak_started_at": state.candidate.get("soak_started_at"),
            "sha": (state.candidate.get("sha") or "")[:12],
            # Ancestry-true recency, stamped at candidate-detect (no per-poll
            # git): "candidate is N commits ahead of the fleet" + when it was
            # committed. Absent on pre-D-2 candidates → banner shows the date
            # label only.
            "commit_date": state.candidate.get("commit_date") or None,
            "commits_ahead": state.candidate.get("commits_ahead"),
        }
    return {
        "mode": cfg.mode,
        "canary_bot": cfg.canary_bot or None,
        "stable_version": stable_version,
        "stable_commit_date": stable_commit_date,
        "stable_short_sha": stable_short_sha,
        # "stable is N commits ahead of previous" — the forward-vs-backward
        # signal the PR-number tail can't give (D-2). Absent until the next
        # promote on pre-D-2 release.json.
        "stable_commits_ahead": (state.stable.get("commits_ahead")
                                 if state is not None
                                 and isinstance(state.stable, dict) else None),
        "previous_version": previous_version,
        # D-8 nested mirror: _classifyUpdates reads rel.stable.promoted_at to
        # tell transient-redeploy lag (quiet) from stuck lag (loud).
        "stable": {"promoted_at": stable_promoted_at},
        "candidate": cand,
        # D-8: pin object (None when not frozen) + corrupt flag (passed in by
        # the I/O boundary). Read-only surfacing — no behavior change.
        "pin": pin_view,
        "corrupt": bool(corrupt),
        "soak_minutes": cfg.soak_minutes,
    }


def canary_sync_view(bot_sync: dict, stable_version: str) -> dict:
    """Per-bot synced flag under canary, keyed against the **stable
    pointer** rather than the latest available code.

    ``bot_sync`` is the mapping from ``deploy.get_bot_sync_status`` —
    ``{bot_id: {"deployed_version": str | None, ...}}``. A bot is synced
    iff its deployed version equals ``stable_version``; a never-deployed
    bot (``deployed_version`` is None) is not synced. This is the core of
    the canary fix: a bot sitting on the gated stable pointer is correctly
    deployed, not "one version behind" the soaking candidate.
    """
    out: dict[str, bool] = {}
    for bot_id, s in (bot_sync or {}).items():
        dep = (s or {}).get("deployed_version")
        out[bot_id] = bool(dep) and dep == stable_version
    return out


def apply_release_status(
    data: dict, network: dict, shared_dir: Any, bot_sync: dict,
    current_version: str,
) -> None:
    """Augment the ``/api/status`` payload with Evolve version + release state.

    Mutates ``data`` in place: ``evolve_current_version``,
    ``any_bots_out_of_sync``, per-bot ``evolve_version`` / ``evolve_synced``,
    the tier-aware ``latest_release`` banner metadata (spec-release-tiers-
    2026-05-16), and — under canary — the ``release`` overlay that recomputes
    "synced" against the gated **stable pointer** (canary bot exempt, ahead by
    design). Direct mode leaves the legacy behavior intact (no ``release`` key,
    no per-bot recompute, no disk read).

    Extracted whole from web/server.py so that hot-hazard file does not grow;
    unit-tested directly here.
    """
    data["evolve_current_version"] = current_version
    data["any_bots_out_of_sync"] = any(
        not s.get("synced") for s in bot_sync.values()
    )
    bots = data.get("bots") or {}
    for bot_id, sync in bot_sync.items():
        if bot_id in bots:
            bots[bot_id]["evolve_version"] = sync.get("deployed_version")
            bots[bot_id]["evolve_synced"] = sync.get("synced")
            # Identity-based direction (synced/behind/ahead/unknown/never) so the
            # Overview can route an "ahead" bot out of the "behind" count and a
            # plain "behind" bot to the calm auto-updating affordance. Direct
            # mode only — the canary overlay below recomputes evolve_synced vs
            # the stable pointer and does not use this field.
            bots[bot_id]["evolve_relation"] = sync.get("relation")
    # Tier-aware release metadata for the banner. Primary bots are excluded —
    # their version reflects local code, not deploy state.
    try:
        from .release_notes import (
            min_deployed_version as _min_deployed,
            resolve_latest_release as _resolve_release,
        )
        member_sync = {
            bid: s for bid, s in bot_sync.items()
            if bots.get(bid, {}).get("role") != "primary"
        }
        resolved = _resolve_release(
            min_deployed_version=_min_deployed(member_sync),
            current_version=current_version,
        )
        data["latest_release"] = resolved.to_dict() if resolved else None
    except Exception:
        data["latest_release"] = None
    # Canary overlay (deploy-resilience spec). Only canary pods read
    # release.json — direct pods skip the per-poll disk hit entirely.
    cfg = resolve_release_config(network)
    if cfg.mode != "canary":
        return
    corrupt = False
    try:
        state = load_release_state(Path(shared_dir))
    except ReleaseStateCorrupt:
        # corrupt → no overlay state, but surface the corrupt flag so the
        # Overview cell renders pipeline-halted instead of reading the
        # post-promote lag as a loud over-alert. Detection stays here at the
        # I/O boundary; release_ui_view stays pure. Promotion is already frozen
        # upstream by the same parse failure — this changes nothing there.
        state = None
        corrupt = True
    view = release_ui_view(cfg, state, corrupt=corrupt)
    if not view:
        return
    data["release"] = view
    stable_v = view.get("stable_version")
    if not stable_v:
        return
    synced = canary_sync_view(bot_sync, stable_v)
    for bid, ok in synced.items():
        if bid in bots:
            bots[bid]["evolve_synced"] = ok
    # "out of sync" now means "behind the stable pointer"; the canary bot is
    # exempt — it runs ahead by design.
    canary = view.get("canary_bot")
    data["any_bots_out_of_sync"] = any(
        (not ok) for bid, ok in synced.items() if bid != canary
    )


def canary_upgrade_block(network: dict) -> dict | None:
    """Error payload ``/api/upgrade`` must return (409) under canary, else
    ``None``.

    A direct upgrade ``git pull``s origin tip onto the deploy checkout and
    redeploys the fleet — under canary that races the gated pipeline, which
    holds the checkout on the stable pointer while a candidate soaks. The
    operator promotes via ``sudo evolve-admin release promote`` instead. This
    is the server-side invariant behind "[Update] never races the pipeline".
    """
    if resolve_release_config(network).mode != "canary":
        return None
    # Message renders in the admin web UI (the upgrade modal surfaces this
    # error verbatim), so it must not hand the operator a `sudo …` CLI hint —
    # the in-app Terminal runs as the passwordless evolve service user and
    # can't answer the prompt (feedback_no_sudo_cli_hints_in_web_ui). Point at
    # the in-app "Complete soak now" button instead. Keep the "release promote"
    # phrase: it's the load-bearing token both the unit and route tests pin.
    return {
        "error": (
            "Pod is in canary release mode — direct upgrade is disabled here "
            "to avoid racing the gated release pipeline. Lagging bots redeploy "
            "automatically; to push a soaking candidate to the fleet early, use "
            "“Complete soak now” on the Overview page, which runs the release "
            "promote in-process (no sudo)."
        ),
        "release_mode": "canary",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Git helpers (always run as the invoking user — the daemon runs as
# evolve; CLI paths re-exec via sudo -u evolve, see cli.py)
# ─────────────────────────────────────────────────────────────────────────────


def _maybe_as_evolve(cmd: list[str], *, pythonpath: str | None = None) -> list[str]:
    """Re-exec as the evolve service user when invoked as root.

    The deploy checkout, staging worktrees, and release artifacts are
    evolve-owned; running git/deploys as root from a `sudo evolve-admin
    release …` invocation would litter them with root-owned files that
    wedge the evolve-user puller daemon later (the same reason cli.py's
    sudoers-refresh path re-execs its git as evolve). The daemon path
    (already evolve) passes through unchanged.

    ``pythonpath`` rides an explicit ``env`` prefix because sudo's
    env_reset strips inherited PYTHONPATH.
    """
    if os.geteuid() != 0:
        return cmd
    prefix = ["sudo", "-u", "evolve"]
    if pythonpath:
        prefix += ["env", f"PYTHONPATH={pythonpath}"]
    return prefix + cmd


def _git(repo: Path, args: list[str], *, staging_root: Path = DEFAULT_STAGING_ROOT,
         timeout: int = 120) -> tuple[int, str, str]:
    """Run git as evolve with safe.directory covering the fleet repo AND
    the staging worktrees (worktree git ops resolve through the main
    repo's .git, but git checks dubious-ownership per working tree)."""
    cmd = _maybe_as_evolve([
        "git",
        "-c", f"safe.directory={repo}",
        "-c", f"safe.directory={staging_root}/*",
        "-C", str(repo),
    ] + args)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def version_for_sha(repo: Path, sha: str, git_fn=None) -> str:
    """EVOLVE_VERSION (YYYY.MMDD.PR) for an arbitrary sha — mirrors
    deploy._compute_version, which only knows how to stamp HEAD of the
    checkout it was imported from."""
    git = git_fn or _git
    rc, out, _ = git(repo, [
        "log", "-1", "--format=%cd %s", "--date=format:%Y.%m%d", sha,
    ])
    if rc != 0 or not out:
        return ""
    date_part = out[:9]
    m = re.search(r"\(#(\d+)\)\s*$", out)
    return f"{date_part}.{m.group(1) if m else '0'}"


def commit_date_for_sha(repo: Path, sha: str, git_fn=None) -> str:
    """ISO commit date (YYYY-MM-DD) for a sha — the ancestry-true recency
    anchor a human reads ("committed Jun 14"), distinct from the version
    string's PR-number tail. Empty string when git can't resolve it."""
    git = git_fn or _git
    rc, out, _ = git(repo, [
        "log", "-1", "--format=%cd", "--date=format:%Y-%m-%d", sha,
    ])
    return out.strip() if rc == 0 and out else ""


def ancestry_counts(repo: Path, base: str, head: str, git_fn=None) -> dict:
    """How `head` relates to `base` by COMMIT ANCESTRY, never by PR-number
    arithmetic. Returns ``{"ahead": int, "behind": int}`` where ``ahead`` is
    commits in ``head`` not in ``base`` and ``behind`` is commits in ``base``
    not in ``head`` (a fast-forward of head over base → ahead>0, behind==0).
    Values are ``None`` when git can't resolve the pair (missing/unrelated).

    This is the only correct "is head newer or older than base" signal: the
    version string's trailing component is a PR number assigned at PR creation,
    so a later-merged PR can sit at the tip with a *lower* number. Subtracting
    those tails reverses the truth (the 2026-06-14 promote incident)."""
    git = git_fn or _git
    none = {"ahead": None, "behind": None}
    if not base or not head:
        return dict(none)
    # `A...B` with --left-right --count → "<left>\t<right>" = (in base not head,
    # in head not base).
    rc, out, _ = git(repo, ["rev-list", "--left-right", "--count",
                            f"{base}...{head}"])
    if rc != 0 or not out:
        return dict(none)
    parts = out.split()
    if len(parts) != 2:
        return dict(none)
    try:
        return {"behind": int(parts[0]), "ahead": int(parts[1])}
    except ValueError:
        return dict(none)


def _stamp_recency(repo: Path, entry: dict, base_sha: str, git_fn) -> dict:
    """Annotate a pointer dict (stable / candidate) with ancestry-true recency
    so the read surfaces (banner, CLI, status JSON) never have to fall back to
    PR-number arithmetic:

      ``commit_date``   — ISO date the human anchors recency on
      ``commits_ahead`` — commits this pointer is AHEAD of ``base_sha``
                          (stable over previous; candidate over stable)

    Best-effort: a field is simply absent when git can't resolve it, and the
    surfaces degrade to the date-led version label. Mutates and returns
    ``entry``. Stored in release.json so the per-poll status path stays
    git-free (computed once, at pointer-move / candidate-detect time)."""
    sha = entry.get("sha") or ""
    cd = commit_date_for_sha(repo, sha, git_fn)
    if cd:
        entry["commit_date"] = cd
    if base_sha:
        ahead = ancestry_counts(repo, base_sha, sha, git_fn).get("ahead")
        if ahead is not None:
            entry["commits_ahead"] = ahead
    return entry


def _record_pod_version(version: str, shared_dir: Path,
                        result: "ReleaseTickResult") -> None:
    """Refresh install.json's top-level pod ``version`` to the just-promoted/
    moved stable so no human-facing surface shows a pod version older than the
    fleet (D-2 staleness: install.json read 2026.0611.2759 while every bot ran
    2026.0614.2884). The top-level field is otherwise only rewritten by a full
    install/upgrade, so a *promote* would leave it stale.

    ``bot_versions`` and every other field are preserved. Written with the same
    evolve-owned atomic temp+rename as release.json — the pointer-move paths run
    as evolve, which owns shared_dir, so no sudo is needed. Best-effort: a
    cosmetic stamp must never abort a pointer move, so any failure is logged
    loudly onto the tick result rather than swallowed (and never raises).
    No-op on a falsy version, a missing install.json (the first install owns
    the initial stamp), or an already-current value."""
    if not version:
        return
    try:
        from . import deploy as _deploy
        data = _deploy.read_install_json(Path(shared_dir))
        if data is None or data.get("version") == version:
            return
        data["version"] = version
        data["version_updated_at"] = _iso()
        path = Path(shared_dir) / "install.json"
        fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                                   prefix=".install.json.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        finally:
            # No-op after a successful rename (tmp is gone); cleans the leftover
            # on any write failure. missing_ok avoids a swallow-only except.
            Path(tmp).unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001 — stamp is cosmetic; pointer move wins
        result.log(f"WARN pod-version stamp to {version} failed "
                   f"({type(e).__name__}: {e}) — pointer move stands")


# ─────────────────────────────────────────────────────────────────────────────
# Tick result
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ReleaseTickResult:
    success: bool = True
    error: str = ""
    steps: list[str] = field(default_factory=list)
    promoted_to: str = ""          # sha when this tick promoted
    candidate_sha: str = ""
    candidate_state: str = ""      # checking|soaking|failed|"" (none)
    soak_tier: str = ""            # skip|short|full|"" (not yet classified)
    soak_probe: str = ""           # ok|regression|error|"" (no active probe)
    fleet_sha: str = ""
    pinned: bool = False
    degraded_no_canary: bool = False

    def log(self, msg: str) -> None:
        self.steps.append(msg)


# ─────────────────────────────────────────────────────────────────────────────
# Injection surface
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ReleaseDeps:
    """Host-touching operations, injectable for tests.

    Defaults are the production implementations. Tests replace any of
    these with fakes; the tick logic itself stays un-mocked.
    """
    git_fn: Callable = _git
    # gate1_fn(staging_dir, changed_paths, deps) -> (ok, detail)
    gate1_fn: Callable | None = None
    # canary_deploy_fn(bot_id, code_dir, network_path) -> (ok, detail)
    #   code_dir = staging worktree (soak) or fleet repo (restore)
    canary_deploy_fn: Callable | None = None
    # soak_health_fn(shared_dir, canary_bot, since_iso, baseline_signatures)
    #   -> (healthy, detail). baseline_signatures = the canary's stable-code
    #   active-signal signatures captured at soak entry (D4); the check
    #   fails only on firing signatures NOT in that baseline. None/[] ⇒
    #   fail-safe fallback to the legacy since_iso predicate.
    soak_health_fn: Callable | None = None
    # soak_probe_fn(shared_dir, canary_bot, staging, network_path, plan)
    #   -> (status, detail), status ∈ "ok" | "regression" | "error".
    # The ACTIVE soak probe (D2/B2). Runs once at soak entry, exercising
    # the candidate on the canary. Injectable so tests substitute a fake.
    soak_probe_fn: Callable | None = None
    # hooks_fn(repo, head_before, head_after) -> (ok, detail) — the
    # post-advance hook suite, run from the NEW checkout's code.
    hooks_fn: Callable | None = None
    # observe_fn(**signal_kwargs) / resolve_fn(signature) — signal store
    observe_fn: Callable | None = None
    resolve_fn: Callable | None = None
    staging_root: Path = DEFAULT_STAGING_ROOT
    staging_venv: Path = DEFAULT_STAGING_VENV
    quarantine_root: Path = Path("/Users/Shared/evolve-quarantine")
    venv_python: str = DEFAULT_VENV_PYTHON
    evolve_admin_bin: str = DEFAULT_EVOLVE_ADMIN
    network_path: Path | None = None
    now_fn: Callable[[], _dt.datetime] = _utc_now


# ─────────────────────────────────────────────────────────────────────────────
# Signals
# ─────────────────────────────────────────────────────────────────────────────


def _observe_signal(deps: ReleaseDeps, shared_dir: Path, *, type_: str,
                    severity: str, title: str, body: str = "",
                    details: dict | None = None) -> None:
    """Best-effort Signal emission — release decisions must not depend
    on the signal store being healthy."""
    try:
        if deps.observe_fn is not None:
            deps.observe_fn(
                shared_dir=shared_dir, type=type_, severity=severity,
                title=title, body=body, details=details or {},
            )
            return
        from . import repo_puller as _rp
        store, schema = _rp._signals_module()
        if store is None:
            return
        store.observe(
            shared_dir,
            signature=schema.make_signature(SIGNAL_PRODUCER, type_, "pod"),
            producer=SIGNAL_PRODUCER,
            type=type_,
            flavor="maintenance",
            severity=severity,
            scope="pod",
            title=title,
            body=body,
            details=details or {},
        )
    except Exception:
        pass


def _resolve_signal(deps: ReleaseDeps, shared_dir: Path, *, type_: str) -> None:
    try:
        if deps.resolve_fn is not None:
            deps.resolve_fn(shared_dir=shared_dir, type=type_)
            return
        from . import repo_puller as _rp
        store, schema = _rp._signals_module()
        if store is None:
            return
        sig = store.find_active_by_signature(
            shared_dir, schema.make_signature(SIGNAL_PRODUCER, type_, "pod"),
        )
        if sig is not None:
            store.apply_transition(
                sig, "resolved", shared_dir,
                actor=SIGNAL_PRODUCER, reason="condition cleared",
            )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Staging worktrees
# ─────────────────────────────────────────────────────────────────────────────


def staging_dir_for(sha: str, staging_root: Path) -> Path:
    return Path(staging_root) / sha[:12]


def _ensure_staging_worktree(repo: Path, sha: str, deps: ReleaseDeps,
                             result: ReleaseTickResult) -> Path | None:
    """Create (or reuse) the immutable per-candidate worktree."""
    git = deps.git_fn
    dest = staging_dir_for(sha, deps.staging_root)
    if dest.exists():
        rc, head, _ = git(dest, ["rev-parse", "HEAD"])
        if rc == 0 and head == sha:
            return dest
        # Wedged / wrong sha — remove and recreate.
        _prune_staging_worktree(repo, dest, deps, result)
    if os.geteuid() == 0:
        # Root-invoked tick (operator ran `sudo evolve-admin repo-pull`):
        # a root-owned staging root would brick every subsequent daemon
        # tick's `worktree add` (evolve can't write into it).
        subprocess.run(_maybe_as_evolve(
            ["/bin/mkdir", "-p", str(deps.staging_root)]),
            capture_output=True, timeout=15)
    else:
        Path(deps.staging_root).mkdir(parents=True, exist_ok=True)
    rc, _, err = git(repo, ["worktree", "add", "--detach", str(dest), sha])
    if rc != 0:
        result.log(f"FAIL worktree add {dest}: {err.splitlines()[0] if err else 'unknown'}")
        return None
    # -x: stale ignored debris (node_modules, dist, egg-info) must not
    # leak into gate checks. Fresh worktrees are clean; this guards the
    # reuse-after-crash path.
    git(dest, ["clean", "-fdx"])
    result.log(f"staging worktree ready: {dest}")
    return dest


def _prune_staging_worktree(repo: Path, dest: Path, deps: ReleaseDeps,
                            result: ReleaseTickResult) -> None:
    git = deps.git_fn
    rc, _, err = git(repo, ["worktree", "remove", "--force", str(dest)])
    if rc != 0 and dest.exists():
        # Fallback: rm -rf + prune the registration.
        try:
            import shutil
            shutil.rmtree(dest, ignore_errors=True)
        except Exception:
            pass
    git(repo, ["worktree", "prune"])
    if rc == 0 or not dest.exists():
        result.log(f"pruned staging worktree {dest}")


def _prune_all_other_staging(repo: Path, keep_sha: str | None,
                             deps: ReleaseDeps, result: ReleaseTickResult) -> None:
    root = Path(deps.staging_root)
    if not root.is_dir():
        return
    keep = staging_dir_for(keep_sha, root).name if keep_sha else None
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if keep and child.name == keep:
            continue
        _prune_staging_worktree(repo, child, deps, result)


# ─────────────────────────────────────────────────────────────────────────────
# Gate 1 — static checks in staging
# ─────────────────────────────────────────────────────────────────────────────


def _changed_paths(repo: Path, base_sha: str, target_sha: str, git_fn) -> list[str]:
    rc, out, _ = git_fn(repo, ["diff", "--name-only", f"{base_sha}..{target_sha}"])
    if rc != 0:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


# ─────────────────────────────────────────────────────────────────────────────
# Soak risk-tier policy (D1)
#
# Spec: docs/spec-delta-soak-risk-tier-and-active-canary-2026-06-12.md §D1.
#
# Classify a candidate's diff into a soak tier by blast-radius ×
# reversibility. This is a small, auditable path→tier table — it mirrors
# the CI path-filter pattern and is NEVER a model judgment.
#
#   skip  — Gate 1 + promote immediately, no soak window (rollback is the
#           net). Only files that no pod daemon imports or executes:
#           docs, *.md, tests, web/static assets. A false-skip here is
#           structurally harmless — these paths cannot crash a gateway,
#           send a message, or mutate state.
#   short — residual 0 under D7: an active-validated short candidate (the
#           D5 gateway probe + any D2 changed-surface probe passed + D4
#           health clean) promotes at active-pass time, no dwell. The B3
#           15-min passive window survives ONLY as the degraded fallback —
#           when there's no canary to actively validate (never silently
#           fast-track a candidate nothing exercised). Ordinary, reversible
#           runtime code (generators, analyzers, routes).
#   full  — a short residual dwell (DEFAULT_SOAK_MINUTES) AFTER active
#           validation passes. Irreversible / privileged paths where a
#           bad release reaching the fleet does damage rollback can't
#           undo: messages already sent, state already written, an
#           auth/sudoers/perms hole already exposed, a migration already
#           run, the deploy/release pipeline itself, plugin (TS) builds.
#
# Fail-safe default = full: any path that is not confidently inert (skip)
# and not confidently ordinary-runtime (short) falls to full, and a
# candidate's tier is the MOST CAUTIOUS tier across all its paths (mixed
# risk → full). An empty diff (we can't tell what changed) → full.
# ─────────────────────────────────────────────────────────────────────────────

SOAK_TIER_SKIP = "skip"
SOAK_TIER_SHORT = "short"
SOAK_TIER_FULL = "full"

# Most-cautious-first ranking used to combine per-path tiers (max wins).
_SOAK_TIER_RANK = {SOAK_TIER_SKIP: 0, SOAK_TIER_SHORT: 1, SOAK_TIER_FULL: 2}

# DEGRADED-mode `short` fallback window (minutes). Under D7 an active-
# validated `short` candidate promotes at active-pass time (residual 0) —
# this window is NOT used in the common path. It is the fallback ONLY when
# there is no canary to actively validate (cfg.canary_bot == ""): without
# active evidence we won't silently fast-track, so `short` falls back to
# the 15-min telemetry-tick floor (the passive quiet-window observes the
# canary's Signals at the 15-min tick, so a shorter window can't see a full
# tick). Always clamped to ≤ the configured full window in
# tier_residual_minutes.
SHORT_SOAK_MINUTES = 15

# File extensions that are inert assets — they ship in the repo but are
# never imported/executed by a pod daemon, so a change to one cannot
# crash the fleet. (.js/.css under web/static are already covered by the
# prefix rule below; this catches stray assets like a favicon in web/.)
_INERT_ASSET_EXTS = frozenset({
    "css", "scss", "svg", "png", "jpg", "jpeg", "gif", "ico",
    "webp", "woff", "woff2", "ttf", "eot", "map",
})

# Path substrings marking an irreversible / privileged file → full.
# Matched against the full repo-relative path; checked BEFORE the short
# allowlist so a dangerous file in an otherwise-ordinary package cannot
# be downgraded. Deliberately broad: over-matching only adds caution (the
# safe direction); a miss would silently soak-skip a privileged change.
_FULL_PATH_TOKENS = (
    # fleet-moving machinery — release / deploy / puller / provisioning
    "release_manager", "repo_puller", "setup_wizard", "/deploy.py",
    "/provision",
    # secrets / auth / sudoers / perms — a hole here is exposed the moment
    # the fleet runs it
    "keystore", "/secret", "credential", "sudoer", "/perms.py",
    "permission", "_auth", "/oauth", "/vault", "/csrf.py",
    # durable state writes — stores + state machines
    "/store.py", "state_machine", "state_store",
    # already-sent side effects — delivery / message-send
    "deliver", "message_send", "send_surface",
    # data-shape migrations (down-migration is a Phase-7 non-goal)
    "migrat",
)

# Exact paths and prefixes that are platform/security-critical → full.
# These mirror the CI "platform-critical" path filter (.github/workflows/
# ci.yml) plus the release pipeline, so the two policies agree on what is
# high-risk.
_FULL_PATH_PREFIXES = (
    "packages/plugin/",                      # plugin TS — built, not per-bot canaried
    "packages/admin/evolve_admin/runtime/",  # platform runtime seam
    "packages/analyzer/runtime/",
)
_FULL_PATH_EXACT = frozenset({
    "packages/analyzer/platform_profile.py",
    "packages/analyzer/evolve_config.py",
    "packages/admin/evolve_admin/web/run.py",
})

# Docs that are NOT inert: files under docs/system/ are read at daemon
# import time and their marker-block contents are injected into EVERY
# bot's system prompt (packages/analyzer/session_surface.py loads
# POD_CONDUCT.md, RUNTIME_NOTES.md, COHERENCE_VOCAB.md this way). A change
# to one alters fleet-wide bot behavior — and a malformed edit (e.g. the
# <!-- …:begin/end --> markers broken) silently drops the conduct rules to
# a fallback string for every session. That is exactly the privileged,
# irreversible-on-arrival class the `full` tier exists for, so these .md
# files must NOT be treated as skip-tier docs. Checked BEFORE the skip
# rules in _soak_tier_for_path. (New runtime-injected docs → add the
# prefix here; the directory is the contract.)
_RUNTIME_INJECTED_DOC_PREFIXES = (
    "docs/system/",
)

# Repo package roots that ship "inert-looking" Markdown VERBATIM to a
# daemon or to bots at runtime/deploy. A `.md` here is NOT a doc — it
# reaches fleet behavior with no compile/import gate, the same hazard
# class the docs/system carve-out (#2819) exists for, just outside docs/.
# Scoped to `.md` (checked in _soak_tier_for_path before the bare-.md
# skip): ordinary `.py` under these roots stays `short`, and `.yaml`
# (e.g. evolve_bot/glossary.yaml) already falls to the fail-safe `full`.
#   proposal_synthesizer/charter.md → the synthesizer LLM's charter,
#       re-read every synthesis run on the LIVE fleet
#       (proposal_synthesizer/synthesizer.py::_load_charter);
#   evolve_bot/ → the RSI actor's SOUL.md / AGENTS.md / skills/*.md,
#       shipped verbatim to the evolve bot (deploy.py::install_evolve_bot_docs);
#   templates/bot_workspace/ → SOUL/AGENTS/MEMORY/README seeded into
#       every bot's workspace at setup (deploy.py);
#   evolve_apps/<id>/procedure.md → installed as an app's runbook
#       (deploy.py::_install_app_procedure).
_RUNTIME_INJECTED_MD_PREFIXES = (
    "packages/analyzer/proposal_synthesizer/",
    "packages/analyzer/evolve_bot/",
    "packages/analyzer/evolve_apps/",
    "packages/admin/evolve_admin/templates/",
)

# Runtime packages whose ordinary .py is reversible → short (once the
# privileged subset above has been peeled off).
_SHORT_PY_PREFIXES = (
    "packages/admin/",
    "packages/analyzer/",
)


def _soak_tier_for_path(rel: str) -> str:
    """Classify ONE changed path into a soak tier. Pure + table-driven."""
    p = rel.strip().replace("\\", "/").lstrip("./")
    if not p:
        return SOAK_TIER_FULL
    segs = p.split("/")
    base = segs[-1]

    # 0) Runtime-injected docs are NOT inert → full. A change to a
    #    docs/system/ file is read by a daemon at import and reaches every
    #    bot's system prompt, so the .md/docs skip rules below MUST NOT
    #    claim it. Checked first, ahead of the inert allowlist.
    if any(p.startswith(pre) for pre in _RUNTIME_INJECTED_DOC_PREFIXES):
        return SOAK_TIER_FULL

    # 0b) A `.md` shipped verbatim to a daemon/bot at runtime/deploy (a
    #     synthesizer charter, a bot SOUL/skill, a workspace template, an
    #     app procedure) is NOT an inert doc — same hazard as docs/system,
    #     just inside the runtime packages. Checked before the bare-.md
    #     skip so these can't be downgraded. Scoped to `.md`: .py here is
    #     correctly `short`, .yaml is correctly fail-safe `full`.
    if base.endswith(".md") and any(
            p.startswith(pre) for pre in _RUNTIME_INJECTED_MD_PREFIXES):
        return SOAK_TIER_FULL

    # 1) Inert → skip. Not imported/executed by any daemon.
    if base.endswith(".md"):
        return SOAK_TIER_SKIP
    if segs[0] == "docs":
        return SOAK_TIER_SKIP
    if ("tests" in segs or "test" in segs
            or base.startswith("test_") or base.endswith("_test.py")
            or base == "conftest.py"):
        return SOAK_TIER_SKIP
    if p.startswith("packages/admin/evolve_admin/web/static/"):
        return SOAK_TIER_SKIP
    if "." in base and base.rsplit(".", 1)[-1].lower() in _INERT_ASSET_EXTS:
        return SOAK_TIER_SKIP

    # 2) Irreversible / privileged → full. Checked before the short
    #    allowlist so a dangerous file can't be downgraded.
    if any(tok in p for tok in _FULL_PATH_TOKENS):
        return SOAK_TIER_FULL
    if p in _FULL_PATH_EXACT or any(p.startswith(pre) for pre in _FULL_PATH_PREFIXES):
        return SOAK_TIER_FULL

    # 3) Ordinary, reversible runtime → short. Plain Python in the runtime
    #    packages (generators, analyzers, admin-server routes, …); the
    #    privileged subset was already peeled off above.
    if base.endswith(".py") and any(p.startswith(pre) for pre in _SHORT_PY_PREFIXES):
        return SOAK_TIER_SHORT

    # 4) Fail-safe: unmatched / ambiguous (configs, plists, shell, json,
    #    anything outside the runtime packages) → full.
    return SOAK_TIER_FULL


def _max_tier(a: str, b: str) -> str:
    """The more cautious of two tiers (full > short > skip)."""
    return a if _SOAK_TIER_RANK.get(a, 2) >= _SOAK_TIER_RANK.get(b, 2) else b


def classify_soak_tier(changed_paths: list[str]) -> str:
    """Candidate soak tier = the most cautious tier across its paths.

    Empty diff (we can't tell what changed) or any mixed/ambiguous path
    → full. This is the fail-safe heart of D1: when unsure, soak."""
    if not changed_paths:
        return SOAK_TIER_FULL
    tier = SOAK_TIER_SKIP
    for p in changed_paths:
        tier = _max_tier(tier, _soak_tier_for_path(p))
        if tier == SOAK_TIER_FULL:
            break  # cannot get more cautious
    return tier


def tier_residual_minutes(tier: str, configured_minutes: int, *,
                          active_validated: bool) -> int:
    """D7 residual dwell (minutes) a candidate must sit through AFTER active
    validation, derived from its tier.

    The release timer is no longer the primary promote gate — active
    validation (D5 gateway liveness + the D2 changed-surface probe(s) + the
    D4 baseline-diff health) is. What remains is a per-tier *residual
    ceiling* for the slow-failure classes that can't be cheaply forced:

      - ``skip``  → 0 always (promotes after Gate 1, never enters soak).
      - ``short`` → 0 WHEN ``active_validated`` — a canary exercised the
                    candidate and it passed, so promote at active-pass time.
                    With NO active validation (``active_validated=False``,
                    i.e. degraded / no canary) it falls back to the legacy
                    B3 passive window (``min(SHORT_SOAK_MINUTES, full)``):
                    never silently fast-track a candidate nothing validated.
      - ``full``  → the configured window (``DEFAULT_SOAK_MINUTES`` by
                    default) regardless of ``active_validated`` — the dwell
                    IS the point for the irreversible-consequence minority,
                    the one concession to accumulating / slow failures.

    Always clamped so ``short`` can NEVER exceed ``full`` (an operator who
    dials ``soak_minutes`` below the floor: degraded ``short`` tracks
    ``full`` rather than out-soaking the privileged tier)."""
    full = max(0, int(configured_minutes))
    if tier == SOAK_TIER_SKIP:
        return 0
    if tier == SOAK_TIER_SHORT:
        return 0 if active_validated else min(SHORT_SOAK_MINUTES, full)
    return full


def _candidate_soak_minutes(candidate: dict | None, cfg: "ReleaseConfig") -> int:
    """Effective residual dwell (D7) for the in-flight candidate.

    Reads the tier, the active-validation verdict, and an optional operator
    minutes override off the candidate. The ``soak_active_validated`` flag is
    stamped at soak entry (True only when the active probe genuinely returned
    OK on a real canary); a tooling-fault (degraded) probe leaves it False so
    the candidate falls back to the legacy passive window rather than being
    fast-tracked on validation that never actually ran. A candidate that
    predates the tier policy has no ``soak_tier`` and no flag → treated as
    full / not-validated → the configured window (backward compatible)."""
    c = candidate or {}
    override = c.get("soak_override_minutes")
    if isinstance(override, int) and override >= 0:
        return override
    tier = c.get("soak_tier") or SOAK_TIER_FULL
    return tier_residual_minutes(
        tier, cfg.soak_minutes,
        active_validated=bool(c.get("soak_active_validated")))


# ─────────────────────────────────────────────────────────────────────────────
# Active canary probe selection (D2)
#
# Spec: docs/spec-delta-soak-risk-tier-and-active-canary-2026-06-12.md §D2.
#
# B1 made the soak window risk-tiered (passive). B2 makes the soak ACTIVE:
# during the window, exercise the candidate on the canary instead of only
# watching it. The probe is selected from the SAME diff classification B1
# already computes — one classification pass over the (already-in-hand)
# diff yields both the tier (classify_soak_tier) and the probe plan
# (classify_soak_probe). No second git call, no duplicated path tables.
#
#   gateway   → (D5) a runtime liveness round-trip against the canary's
#               loopback /evolve/status — ALWAYS in the plan, not derived
#               from the diff. Gateway health is universal evidence: a
#               candidate can import clean (Gate 1) yet crash the gateway
#               on boot, which no diff-selected probe would catch.
#   generator → run the changed generator(s) once against the canary;
#   route     → exercise the changed admin-server route module(s);
#   send      → a canary send round-trip (the post-upgrade send precedent
#               from PR #2704 — safe_upgrade.probe_send_surface + a
#               dispatcher round-trip to the OPERATOR alert channel, never
#               real users; §2.4 canary side-effect suppression stays, and
#               this one deliberate operator send is the active exercise).
#
# Beyond the always-on gateway probe, a path with no recognizable probe
# target yields no *additional* probe — the passive quiet-window stays the
# backstop for those (and for the fail-safe-full opaque changes). The probe
# step never CHANGES a candidate's tier (B1 owns that); it only adds active
# checks on top.
# ─────────────────────────────────────────────────────────────────────────────

SOAK_PROBE_GATEWAY = "gateway"     # D5 — always-on runtime liveness check
SOAK_PROBE_GENERATOR = "generator"
SOAK_PROBE_ROUTE = "route"
SOAK_PROBE_SEND = "send"

# Tri-state probe verdict — mirrors the send-surface tri-state (#2704):
# a probe that RUNS and finds breakage is a regression (fail closed); a
# probe that could not run is a tooling fault (fail OPEN + loud), never a
# candidate failure. The two must never be conflated.
SOAK_PROBE_OK = "ok"
SOAK_PROBE_REGRESSION = "regression"
SOAK_PROBE_ERROR = "error"

# Generators ship one dir per id at this prefix (charter.yaml + .py).
_GENERATOR_PREFIX = "packages/analyzer/generators/"
# Admin-server route modules live here as routes_*.py / *_routes.py.
_ROUTE_PREFIX = "packages/admin/evolve_admin/web/"
# Delivery / message-send paths — the SAME token subset B1 routes to full
# (_FULL_PATH_TOKENS delivery group); kept as its own tuple so the probe
# selector and the tier table stay independently auditable.
_SEND_PATH_TOKENS = ("deliver", "message_send", "send_surface")


def _soak_probe_for_path(rel: str) -> dict | None:
    """Select an active probe for ONE changed path, or None if the path
    has no exercisable surface. Pure + table-driven (no I/O)."""
    p = rel.strip().replace("\\", "/").lstrip("./")
    if not p:
        return None

    # generator: packages/analyzer/generators/<id>/<file> → run <id>.
    # Package-level helpers (__init__.py, _intent_memory.py, …) sit
    # directly under the prefix with no <id> dir → no per-generator probe.
    if p.startswith(_GENERATOR_PREFIX):
        rest = p[len(_GENERATOR_PREFIX):]
        seg = rest.split("/", 1)[0]
        if "/" in rest and seg and not seg.startswith("_"):
            return {"kind": SOAK_PROBE_GENERATOR, "target": seg}
        return None

    # delivery / message-send → canary send round-trip (no per-file target).
    if any(tok in p for tok in _SEND_PATH_TOKENS):
        return {"kind": SOAK_PROBE_SEND, "target": None}

    # admin-server route module → exercise that module.
    base = p.rsplit("/", 1)[-1]
    if (p.startswith(_ROUTE_PREFIX) and base.endswith(".py")
            and (base.startswith("routes_") or base.endswith("_routes.py"))):
        return {"kind": SOAK_PROBE_ROUTE, "target": base[:-len(".py")]}

    return None


def classify_soak_probe(changed_paths: list[str]) -> list[dict]:
    """Probe plan for a candidate = the always-on gateway liveness probe
    (D5), then the de-duplicated set of active probes the diff selects.

    The gateway probe is ALWAYS first and never derived from the diff —
    gateway health is universal runtime evidence (a candidate can import
    clean yet crash the gateway on boot). The diff-selected probes add
    surface-specific checks on top; a diff with no exercisable surface
    still gets the gateway probe, and leans on the passive quiet-window
    for everything else. A candidate touching several kinds runs each
    (e.g. a generator + a route change exercises both)."""
    plan: list[dict] = [{"kind": SOAK_PROBE_GATEWAY, "target": None}]
    seen: set[tuple[str, str | None]] = {(SOAK_PROBE_GATEWAY, None)}
    for p in changed_paths:
        spec = _soak_probe_for_path(p)
        if spec is None:
            continue
        key = (spec["kind"], spec["target"])
        if key in seen:
            continue
        seen.add(key)
        plan.append(spec)
    return plan


def _staging_pythonpath(staging: Path) -> str:
    return os.pathsep.join([
        str(staging / "packages" / "admin"),
        str(staging / "packages" / "analyzer"),
    ])


def _ensure_staging_venv(staging: Path, deps: ReleaseDeps) -> tuple[bool, str]:
    """Build/refresh the staging venv when a candidate changes
    pyproject.toml. NEVER ``pip install -e`` the staging tree into the
    *live* venv — that repoints the fleet's editable install at staging.

    The venv is built with ``uv venv --seed`` when uv is available — uv
    creates virtualenvs WITHOUT stdlib ``venv``/``ensurepip``, which stock
    Ubuntu ships in the un-installed-by-default ``python3-venv`` apt pkg
    (``python3 -m venv`` dies there with "ensurepip is not available" — the
    same W7c blocker fixed in ``installer.ensure_evolve_venv``; a Linux
    canary pod refreshing this staging venv would hit it otherwise).
    ``--seed`` populates pip so the downstream ``pip install -e`` runs
    through the venv's own pip unchanged; ``--clear`` makes the build
    idempotent (we only reach the builder when the staging python is
    ABSENT, so any dir at ``venv`` is partial/stray state from a crashed
    earlier build). Falls back to stdlib ``venv`` when uv is absent (a host
    with ``python3-venv`` but no uv). macOS is byte-identical — staging
    venvs already build there; the resolver just finds Homebrew's uv (or
    falls back to stdlib if neither). ``--python`` pins the live deploy
    interpreter (the interpreter contract; see [[analyzer-packaged-compat-editable]])."""
    venv = Path(deps.staging_venv)
    py = venv / "bin" / "python3"
    try:
        if not py.exists():
            uv = _find_uv()
            if uv is not None:
                create_cmd = [uv, "venv", "--clear", "--seed",
                              "--python", deps.venv_python, str(venv)]
            else:
                create_cmd = [deps.venv_python, "-m", "venv", str(venv)]
            r = subprocess.run(
                _maybe_as_evolve(create_cmd),
                capture_output=True, text=True, timeout=300,
                cwd=str(staging),
            )
            if r.returncode != 0:
                return False, f"venv create failed: {(r.stderr or r.stdout)[:200]}"
        r = subprocess.run(
            _maybe_as_evolve([str(venv / "bin" / "pip"), "install", "-q", "-e",
                              str(staging / "packages" / "admin")]),
            capture_output=True, text=True, timeout=600,
            cwd=str(staging),
        )
        if r.returncode != 0:
            return False, f"staging pip install failed: {(r.stderr or r.stdout)[:300]}"
        return True, "staging venv refreshed"
    except subprocess.TimeoutExpired:
        return False, "staging venv build timed out"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def gate1_static_checks(staging: Path, changed_paths: list[str],
                        deps: ReleaseDeps) -> tuple[bool, str]:
    """compileall + import-smoke (+ staging venv on dep changes,
    + plugin build check on plugin changes). Returns (ok, detail)."""
    notes: list[str] = []

    python = deps.venv_python
    if any(p == "packages/admin/pyproject.toml" for p in changed_paths):
        ok, info = _ensure_staging_venv(staging, deps)
        notes.append(info)
        if not ok:
            return False, "; ".join(notes)
        python = str(Path(deps.staging_venv) / "bin" / "python3")

    # Syntax class — cheap, whole tree. All gate subprocesses run as
    # evolve (root-invoked ticks would otherwise litter root-owned
    # __pycache__/node_modules inside worktrees the evolve daemon then
    # can't prune) and with cwd inside staging (sudo -u evolve dies on
    # an evolve-unreadable caller cwd).
    try:
        r = subprocess.run(
            _maybe_as_evolve(
                [python, "-m", "compileall", "-q", str(staging / "packages")]),
            capture_output=True, text=True, timeout=300, cwd=str(staging),
        )
    except Exception as e:
        return False, f"compileall could not run: {type(e).__name__}: {e}"
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip()[-400:]
        return False, f"compileall failed: {tail}"
    notes.append("compileall ok")

    # Import-time crash class — the daemons' pre-restart gate.
    env = dict(os.environ)
    env["PYTHONPATH"] = _staging_pythonpath(staging)
    code = (
        "import importlib, sys\n"
        "for m in sys.argv[1:]:\n"
        "    importlib.import_module(m)\n"
    )
    try:
        r = subprocess.run(
            _maybe_as_evolve([python, "-c", code, *IMPORT_SMOKE_MODULES],
                             pythonpath=env["PYTHONPATH"]),
            capture_output=True, text=True, timeout=300,
            env=env, cwd=str(staging),
        )
    except Exception as e:
        return False, f"import-smoke could not run: {type(e).__name__}: {e}"
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip()[-400:]
        return False, f"import-smoke failed: {tail}"
    notes.append(f"import-smoke ok ({len(IMPORT_SMOKE_MODULES)} modules)")

    # Plugin build-validation (build only — staging never restages the
    # shared dist; that happens at promote).
    if any(p.startswith("packages/plugin/") for p in changed_paths):
        ok, info = _gate1_plugin_build_check(staging)
        notes.append(info)
        if not ok:
            return False, "; ".join(notes)

    return True, "; ".join(notes)


def _gate1_plugin_build_check(staging: Path) -> tuple[bool, str]:
    plugin_dir = staging / "packages" / "plugin"
    if not plugin_dir.is_dir():
        return True, "plugin dir absent; skipped"
    try:
        r = subprocess.run(
            _maybe_as_evolve(
                ["npm", "install", "--no-audit", "--no-fund", "--silent"]),
            capture_output=True, text=True, timeout=600, cwd=str(plugin_dir),
        )
        if r.returncode != 0:
            return False, f"plugin npm install failed: {(r.stderr or r.stdout)[-300:]}"
        # Type-check via the LOCAL compiler, bound deterministically. `npm
        # exec --no` resolves packages/plugin/node_modules/.bin/tsc (present
        # from the npm install above) and `--no` forbids any registry fetch —
        # so a cold-cache / minimal canary host can never resolve a squatted
        # standalone `tsc` instead (the lower-risk sibling of the bug PR #2993
        # fixed in deploy.build_plugin, where `npx --yes tsc` fetched the
        # deprecated tsc@2.0.4). `--noEmit` keeps this a pure type-check, not a
        # build (so we cannot reuse `npm run build`, which emits dist/).
        r = subprocess.run(
            _maybe_as_evolve(["npm", "exec", "--no", "--", "tsc", "--noEmit"]),
            capture_output=True, text=True, timeout=600, cwd=str(plugin_dir),
        )
        if r.returncode != 0:
            return False, f"plugin tsc failed: {(r.stdout or r.stderr)[-400:]}"
        return True, "plugin build check ok"
    except subprocess.TimeoutExpired:
        return False, "plugin build check timed out"
    except FileNotFoundError as e:
        # npm/npx missing on a minimal install — treat as gate failure:
        # we cannot vouch for the candidate's plugin.
        return False, f"plugin toolchain unavailable: {e}"
    except Exception as e:
        return False, f"plugin build check error: {type(e).__name__}: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Gate 2 — canary deploy + soak
# ─────────────────────────────────────────────────────────────────────────────


def _default_canary_deploy(bot_id: str, code_dir: Path, network_path: Path,
                           deps: ReleaseDeps) -> tuple[bool, str]:
    """Deploy ``bot_id`` running ``code_dir``'s code, pod side effects
    suppressed. PYTHONPATH precedes the venv's editable install, so the
    subprocess resolves evolve_admin from ``code_dir`` (verified
    empirically during design review)."""
    pythonpath = _staging_pythonpath(code_dir)
    env = dict(os.environ)
    env["PYTHONPATH"] = pythonpath
    cmd = _maybe_as_evolve([
        deps.venv_python, "-m", "evolve_admin.canary_deploy",
        "--bot", bot_id,
        "--network", str(network_path),
    ], pythonpath=pythonpath)
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=900,
            env=env, cwd=str(code_dir),
        )
    except subprocess.TimeoutExpired:
        return False, "canary deploy timed out (900s)"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if r.returncode != 0:
        return False, f"rc={r.returncode}: {(r.stderr or r.stdout)[-400:]}"
    return True, "canary deployed"


def _capture_canary_signal_baseline(shared_dir: Path, canary_bot: str,
                                    deps: ReleaseDeps) -> list[str]:
    """Snapshot the canary's standing-debt baseline as a sorted list of
    Signal ``signature`` values, taken at the ``checking→soaking`` edge —
    *before* the candidate is deployed to the canary, so the canary still
    runs STABLE code and its active signals are exactly the ambient debt.

    Captures BOTH firing and snoozed signals (no ``state=`` filter): a
    snoozed ambient signal that un-snoozes mid-soak must not later be
    mistaken for a candidate-caused regression by the baseline diff.

    ``signature`` is the producer-computed, deterministic dedup identity
    (see schema.signal.make_signature); it survives the resolve→reopen
    cycle that re-stamps a fresh ``to_state="firing"`` transition every
    scan, which is precisely why the diff is immune to the re-fire that
    jammed the old timestamp-keyed predicate.

    Defensive by contract: any tooling fault returns ``[]`` so the caller
    persists an empty baseline and the soak-health check's fail-safe
    fallback (the legacy since_iso predicate) takes over — a release must
    never fail, nor silently pass, on our own tooling fault."""
    try:
        from . import repo_puller as _rp
        store, _schema = _rp._signals_module()
        if store is None:
            return []
        sigs = {
            sig.signature
            for sig in store.iter_active(shared_dir, bot_id=canary_bot)
            if getattr(sig, "signature", None)
        }
        return sorted(sigs)
    except Exception:
        return []


def _default_soak_health(shared_dir: Path, canary_bot: str,
                         since_iso: str,
                         baseline_signatures: list[str] | None = None,
                         ) -> tuple[bool, str]:
    """Healthy iff the canary emits no firing Signal whose ``signature``
    is NEW relative to the stable-code baseline captured at soak entry.

    Diffing by **signature** (not by transition timestamp) is the D4 fix:
    standing app-quality debt on the canary's own apps re-fires every scan
    (a resolve→reopen, or a fresh observe outside the reopen window,
    re-stamps a ``to_state="firing"`` transition inside every soak window).
    The old predicate keyed on that timestamp and so attributed pre-existing
    debt to every candidate — jamming the whole fleet. The signature is the
    stable dedup identity and survives the reopen cycle, so a signature
    absent from the baseline is genuinely candidate-caused, from ANY
    producer (category-agnostic — a candidate that truly breaks an app is
    still caught). Gateway crash loops surface here transitively (heal +
    watchdog emit Signals on them).

    Fail-safe fallback: when ``baseline_signatures`` is None/empty (a
    pre-D4 candidate already mid-soak across the upgrade, or a baseline
    capture that faulted), fall back to the legacy ``since_iso`` predicate
    so the gate NEVER silently passes everything. New candidates always
    capture a baseline, so the diff path is the norm.
    """
    try:
        from . import repo_puller as _rp
        store, _schema = _rp._signals_module()
        if store is None:
            return True, "signal store unavailable; soak check skipped"
        baseline = set(baseline_signatures or [])
        since = _parse_iso(since_iso)
        offenders: list[str] = []
        for sig in store.iter_active(shared_dir, bot_id=canary_bot, state="firing"):
            if baseline:
                # D4 baseline-diff: new signature ⇒ candidate-caused.
                if getattr(sig, "signature", None) not in baseline:
                    offenders.append(f"{sig.producer}:{sig.type}")
            else:
                # Fail-safe: no baseline ⇒ legacy since_iso predicate.
                fired_at = None
                for tr in reversed(sig.state_history or []):
                    if getattr(tr, "to_state", None) == "firing":
                        fired_at = _parse_iso(getattr(tr, "at", None))
                        break
                if since is None or (fired_at is not None and fired_at >= since):
                    offenders.append(f"{sig.producer}:{sig.type}")
        if offenders:
            return False, f"new firing signals on canary: {', '.join(offenders[:5])}"
        return True, "no new canary signals"
    except Exception as e:
        return True, f"soak check errored ({type(e).__name__}: {e}); not failing soak on tooling"


def _default_soak_probe(shared_dir: Path, canary_bot: str, staging: Path,
                        network_path: Path, plan: list[dict],
                        deps: ReleaseDeps) -> tuple[str, str]:
    """Run the active soak probe against the candidate (D2/B2).

    Mirrors ``_default_canary_deploy``: a subprocess whose PYTHONPATH is
    pinned to the candidate's STAGING worktree, so every probe exercises
    the candidate's code rather than the live fleet's. The probe plan is
    handed over as JSON; ``evolve_admin.soak_probe`` does the per-kind
    exercise (reusing the #2704 send precedent for the send kind).

    Returns ``(status, detail)``, status ∈ ``ok`` | ``regression`` |
    ``error``. The subprocess exit code carries the verdict —
    0 = ok, 2 = regression (candidate broken → fail closed), 3 = tooling
    error (could not run → fail OPEN). Any launch failure / timeout /
    unexpected exit is treated as a tooling error: a release is never
    failed on our own fault."""
    if not plan:
        return SOAK_PROBE_OK, "no active probe selected for this diff"
    pythonpath = _staging_pythonpath(staging)
    env = dict(os.environ)
    env["PYTHONPATH"] = pythonpath
    cmd = _maybe_as_evolve([
        deps.venv_python, "-m", "evolve_admin.soak_probe",
        "--bot", canary_bot,
        "--network", str(network_path),
        "--shared-dir", str(shared_dir),
        "--plan", json.dumps(plan),
    ], pythonpath=pythonpath)
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=900,
            env=env, cwd=str(staging),
        )
    except subprocess.TimeoutExpired:
        return SOAK_PROBE_ERROR, "active probe timed out (900s)"
    except Exception as e:
        return SOAK_PROBE_ERROR, f"active probe could not launch: {type(e).__name__}: {e}"
    detail = (r.stdout or "").strip()[-600:] or (r.stderr or "").strip()[-600:]
    if r.returncode == 0:
        return SOAK_PROBE_OK, detail or "active probe ok"
    if r.returncode == 2:
        return SOAK_PROBE_REGRESSION, detail or "active probe found a regression"
    if r.returncode == 3:
        return SOAK_PROBE_ERROR, detail or "active probe could not run (tooling)"
    # Any other exit (crash in the probe harness itself) — fail OPEN, loud.
    return SOAK_PROBE_ERROR, f"active probe exited {r.returncode}: {detail}"


# ─────────────────────────────────────────────────────────────────────────────
# Promote / rollback plumbing
# ─────────────────────────────────────────────────────────────────────────────


def _default_hooks(repo: Path, head_before: str, head_after: str,
                   deps: ReleaseDeps) -> tuple[bool, str]:
    """Run the existing post-pull hook suite from the NEW checkout in a
    fresh subprocess, so deploy.EVOLVE_VERSION (computed at import time
    from git log of the checkout the code was imported from) stamps the
    post-move version."""
    cmd = _maybe_as_evolve([
        deps.evolve_admin_bin, "repo-pull",
        "--repo", str(repo),
        "--hooks-from", head_before,
        "--hooks-to", head_after,
        "--quiet",
    ])
    try:
        # cwd=repo: under `sudo evolve-admin release rollback` over SSH the
        # caller's cwd is the admin user's home, which the evolve user can't
        # traverse — the re-exec'd interpreter dies before line 1 (the
        # documented sudo-evolve-cwd gotcha). start_new_session: the hook
        # suite kickstarts admin-ui, and when the CALLER is admin-ui (web
        # rollback) that kill would otherwise take this child down with it
        # mid-suite — detached, the hooks run to completion even if the
        # parent dies (the release pointer was already persisted before the
        # hooks run; see _promote/release_rollback ordering).
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1800,
            cwd=str(repo), start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return False, "hook suite timed out (1800s)"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    detail = (r.stdout or "").strip()[-800:]
    if r.returncode != 0:
        return False, f"rc={r.returncode}: {detail or (r.stderr or '')[-400:]}"
    return True, detail or "hooks ok"


def _sweep_untracked_collisions(repo: Path, sha: str, deps: ReleaseDeps,
                                result: ReleaseTickResult) -> None:
    """Quarantine untracked files that collide with tracked paths at
    ``sha`` before a reset --hard silently destroys them.

    ``git reset --hard`` overwrites colliding untracked files with no
    error (verified) — unlike the legacy pull, which refuses and lets
    _handle_untracked_conflict quarantine. Same per-file policy here:
    byte-identical to the target blob → delete; divergent → move to the
    quarantine dir for forensics. Best-effort: a sweep failure must not
    block the move (the file would simply be overwritten, which is
    today's pull-path-recovery behavior, not a regression).
    """
    git = deps.git_fn
    rc, out, _ = git(repo, ["status", "--porcelain"])
    if rc != 0 or not out:
        return
    untracked = [ln[3:].strip() for ln in out.splitlines() if ln.startswith("??")]
    if not untracked:
        return
    stamp = deps.now_fn().strftime("%Y%m%dT%H%M%SZ")
    quarantine = Path(deps.quarantine_root) / stamp
    for rel in untracked:
        rc, blob, _ = git(repo, ["show", f"{sha}:{rel}"])
        if rc != 0:
            continue  # not tracked at target — reset leaves it alone
        try:
            local = (repo / rel).read_text()
        except (OSError, UnicodeDecodeError):
            local = None
        try:
            if local is not None and local == blob:
                (repo / rel).unlink()
                result.log(f"deleted identical-to-target untracked file {rel}")
            else:
                dest = quarantine / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                (repo / rel).rename(dest)
                result.log(f"quarantined divergent untracked file {rel} → {dest}")
        except OSError as e:
            result.log(f"WARN could not sweep untracked {rel}: {e}")


def _move_fleet(repo: Path, sha: str, deps: ReleaseDeps,
                result: ReleaseTickResult) -> bool:
    """reset --hard the fleet checkout (branch main) to sha."""
    git = deps.git_fn
    rc, _, err = git(repo, ["rev-parse", "--verify", f"{sha}^{{commit}}"])
    if rc != 0:
        result.log(f"FAIL fleet move: unknown sha {sha[:12]}: {err}")
        return False
    _sweep_untracked_collisions(repo, sha, deps, result)
    rc, _, err = git(repo, ["reset", "--hard", sha])
    if rc != 0:
        result.log(f"FAIL fleet reset --hard {sha[:12]}: {err.splitlines()[0] if err else ''}")
        return False
    result.log(f"fleet checkout → {sha[:12]}")
    return True


def _move_tags(repo: Path, stable_sha: str, previous_sha: str | None,
               deps: ReleaseDeps) -> None:
    git = deps.git_fn
    git(repo, ["tag", "-f", STABLE_TAG, stable_sha])
    if previous_sha:
        git(repo, ["tag", "-f", PREVIOUS_TAG, previous_sha])


def _is_ancestor(repo: Path, ancestor: str, descendant: str, git_fn) -> bool:
    rc, _, _ = git_fn(repo, ["merge-base", "--is-ancestor", ancestor, descendant])
    return rc == 0


def _restore_canary(repo: Path, cfg: ReleaseConfig, deps: ReleaseDeps,
                    network_path: Path, result: ReleaseTickResult,
                    *, shared_dir: Path | None = None) -> None:
    """Redeploy the canary from the fleet checkout (fleet paths/code).

    The install.json version stamp is written HERE, by the running
    (stable) code, not by canary_deploy — install.json is a pod-wide
    file and candidate code may not rewrite pod-wide state (spec §2.4).
    """
    if not cfg.canary_bot:
        return
    deploy = deps.canary_deploy_fn or (
        lambda bot, code_dir, np: _default_canary_deploy(bot, code_dir, np, deps)
    )
    ok, info = deploy(cfg.canary_bot, repo, network_path)
    result.log(f"canary restore ({cfg.canary_bot}): {'ok' if ok else 'FAIL'} — {info}")
    if ok and shared_dir is not None:
        try:
            from . import deploy as _deploy
            _deploy.record_bot_deploy(cfg.canary_bot, Path(shared_dir))
        except Exception as e:  # noqa: BLE001 — stamp is best-effort
            result.log(f"WARN canary restore stamp failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# The tick
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_release_env(
    repo: Path, shared_dir: Path, deps: ReleaseDeps,
    cfg: ReleaseConfig | None,
) -> tuple[ReleaseConfig, Path]:
    """Resolve ``(cfg, network_path)`` the way the tick does at its top.

    A passed ``cfg`` is honored; otherwise it is read from the pod's
    network.json (falling back to canary mode). ``network_path`` prefers the
    injected ``deps.network_path``, then ``{shared_dir}/network.json``, then the
    in-repo ``config/network.json`` — the canary-restore path in ``_promote``
    reads it, so an operator promote must land on the same file the tick would.
    Shared by ``release_tick`` and ``release_promote`` so the two never drift.
    """
    if cfg is None:
        try:
            from .config import load_network, DEFAULT_NETWORK_CONFIG
            cfg = resolve_release_config(
                load_network(deps.network_path or DEFAULT_NETWORK_CONFIG))
        except Exception:
            cfg = ReleaseConfig(mode="canary")
    network_path = deps.network_path or Path(shared_dir) / "network.json"
    if not network_path.exists():
        alt = repo / "config" / "network.json"
        if alt.exists():
            network_path = alt
    return cfg, network_path


def release_tick(
    repo: Path = DEFAULT_REPO,
    shared_dir: Path = DEFAULT_SHARED_DIR,
    *,
    remote: str = DEFAULT_REMOTE,
    branch: str = DEFAULT_BRANCH,
    cfg: ReleaseConfig | None = None,
    deps: ReleaseDeps | None = None,
) -> ReleaseTickResult:
    """One canary-mode tick. Stateless — release.json carries the
    pipeline across ticks. Never raises."""
    deps = deps or ReleaseDeps()
    result = ReleaseTickResult()
    git = deps.git_fn

    cfg, network_path = _resolve_release_env(repo, shared_dir, deps, cfg)

    # ── 0. State ─────────────────────────────────────────────────────────
    try:
        state = load_release_state(shared_dir)
    except ReleaseStateCorrupt as e:
        result.success = False
        result.error = f"release.json corrupt: {e}"
        result.log(f"FAIL {result.error}")
        result.log("promotion FROZEN — run `evolve-admin release init` after investigating")
        _observe_signal(
            deps, shared_dir, type_="release_state_corrupt", severity="alert",
            title="release.json is corrupt — promotion frozen",
            body=str(e),
        )
        return result
    _resolve_signal(deps, shared_dir, type_="release_state_corrupt")

    rc, fleet_head, err = git(repo, ["rev-parse", "HEAD"])
    if rc != 0:
        result.success = False
        result.error = f"rev-parse HEAD failed: {err}"
        result.log(f"FAIL {result.error}")
        _observe_signal(
            deps, shared_dir, type_="release_tick_failed", severity="alert",
            title="release tick: fleet checkout unreadable", body=result.error,
        )
        return result
    result.fleet_sha = fleet_head

    if state is None:
        state = ReleaseState(stable={
            "sha": fleet_head,
            "version": version_for_sha(repo, fleet_head, git),
            "promoted_at": _iso(deps.now_fn()),
        })
        save_release_state(state, shared_dir)
        _move_tags(repo, fleet_head, None, deps)
        result.log(f"initialized release.json: stable = current fleet HEAD {fleet_head[:12]}")

    # ── 1. Pointer repair — release.json is authoritative ───────────────
    if fleet_head != state.stable["sha"]:
        result.log(
            f"fleet HEAD {fleet_head[:12]} != stable {state.stable['sha'][:12]} "
            f"(out-of-band git surgery?) — repairing to pointer"
        )
        _observe_signal(
            deps, shared_dir, type_="release_pointer_repaired", severity="warn",
            title="Fleet checkout drifted from the release pointer — repaired",
            body=f"HEAD was {fleet_head[:12]}, pointer is {state.stable['sha'][:12]}. "
                 f"Use `evolve-admin release rollback/pin` for intentional moves.",
        )
        if not _move_fleet(repo, state.stable["sha"], deps, result):
            result.success = False
            result.error = "pointer repair failed"
            return result
        result.fleet_sha = state.stable["sha"]

    # ── 2. Fetch ─────────────────────────────────────────────────────────
    rc, _, err = git(repo, ["fetch", remote, branch])
    if rc != 0:
        result.success = False
        result.error = f"fetch failed: {err.splitlines()[0] if err else 'unknown'}"
        result.log(f"FAIL {result.error}")
        _observe_signal(
            deps, shared_dir, type_="release_tick_failed", severity="alert",
            title="release tick: git fetch failed", body=result.error,
        )
        return result
    _resolve_signal(deps, shared_dir, type_="release_tick_failed")
    rc, origin_sha, _ = git(repo, ["rev-parse", "FETCH_HEAD"])
    if rc != 0 or not origin_sha:
        result.success = False
        result.error = "rev-parse FETCH_HEAD failed"
        result.log(f"FAIL {result.error}")
        return result

    result.pinned = state.pin is not None
    if not cfg.canary_bot:
        result.degraded_no_canary = True

    # ── 3. Candidate selection ───────────────────────────────────────────
    if origin_sha == state.stable["sha"]:
        if state.candidate is not None:
            # origin retreated to stable (rare) — drop the candidate.
            result.log("origin/main back at stable; dropping in-flight candidate")
            _on_candidate_resolved(repo, state, cfg, deps, network_path, result,
                                   restore_canary=state.candidate.get("state") == "soaking")
            refold_operator_fields(state, shared_dir)
            save_release_state(state, shared_dir)
        result.log(f"up to date at stable {state.stable['sha'][:12]}")
        return result

    if origin_sha in state.skip:
        result.log(f"origin/main {origin_sha[:12]} is skip-listed (previously failed); waiting for a new sha")
        return result

    if state.candidate is None or state.candidate.get("sha") != origin_sha:
        replacing = state.candidate.get("sha", "")[:12] if state.candidate else ""
        was_soaking = bool(state.candidate) and state.candidate.get("state") == "soaking"
        state.candidate = {
            "sha": origin_sha,
            "first_seen_at": _iso(deps.now_fn()),
            "soak_started_at": "",
            "state": "checking",
            "failure": "",
        }
        # Recency vs the current stable — "candidate is N commits ahead of the
        # fleet" reads forward; the PR-number tail would mislead (D-2).
        _stamp_recency(repo, state.candidate, state.stable.get("sha", ""), git)
        if replacing:
            result.log(f"candidate {replacing} superseded by {origin_sha[:12]} (clocks reset)")
        else:
            result.log(f"new candidate {origin_sha[:12]}")
        if was_soaking:
            # Restore the canary BEFORE pruning the old worktree its
            # plists still point into — pruning first guarantees a dead
            # window if the restore deploy fails.
            _restore_canary(repo, cfg, deps, network_path, result,
                            shared_dir=shared_dir)
        _prune_all_other_staging(repo, origin_sha, deps, result)
        refold_operator_fields(state, shared_dir)
        save_release_state(state, shared_dir)

    result.candidate_sha = state.candidate["sha"]
    result.candidate_state = state.candidate["state"]

    # ── 4. Pipeline ──────────────────────────────────────────────────────
    if state.candidate["state"] == "failed":
        result.log(
            f"candidate {state.candidate['sha'][:12]} previously failed: "
            f"{state.candidate.get('failure', '')[:200]}"
        )
        return result

    staging = _ensure_staging_worktree(repo, state.candidate["sha"], deps, result)
    if staging is None:
        result.success = False
        result.error = "staging worktree unavailable"
        return result

    if state.candidate["state"] == "checking":
        changed = _changed_paths(repo, state.stable["sha"], state.candidate["sha"], git)
        gate1 = deps.gate1_fn or (lambda st, ch, dp: gate1_static_checks(st, ch, dp))
        ok, detail = gate1(staging, changed, deps)
        result.log(f"gate1: {'pass' if ok else 'FAIL'} — {detail[:400]}")
        if not ok:
            _fail_candidate(repo, state, cfg, deps, network_path, result,
                            reason=f"gate1: {detail}", shared_dir=shared_dir,
                            canary_was_deployed=False)
            refold_operator_fields(state, shared_dir)
            save_release_state(state, shared_dir)
            return result

        # Risk-tier the soak from the diff (D1). The diff (`changed`) is
        # already in hand from Gate 1 — no fresh git call. An operator
        # pre-bump (`release soak`) is honored as a floor: the policy can
        # only raise the tier from there, never lower the operator's
        # chosen caution.
        policy_tier = classify_soak_tier(changed)
        prior = state.candidate.get("soak_tier")
        tier = _max_tier(policy_tier, prior) if prior else policy_tier
        state.candidate["soak_tier"] = tier
        result.soak_tier = tier

        if tier == SOAK_TIER_SKIP:
            # Gate 1 passed and nothing in the diff can crash a gateway,
            # send a message, or mutate state — promote immediately, no
            # soak window. No canary soak deploy: the post-promote restore
            # brings the canary onto the promoted fleet code like every
            # other bot, and rollback stays the net. Every promote
            # invariant (ancestry, mid-tick pin re-check, pointer-before-
            # hooks, canary restore, prune) is the shared `_promote` path.
            result.log(f"soak tier: skip ({len(changed)} inert/reversible "
                       f"path(s)) — promoting after Gate 1, no soak window")
            refold_operator_fields(state, shared_dir)
            save_release_state(state, shared_dir)
            return _promote(repo, shared_dir, state, cfg, deps, network_path, result)

        if cfg.canary_bot:
            # D4: capture the canary's standing-debt baseline NOW — the
            # canary still runs STABLE code, so its active (firing+snoozed)
            # signal signatures are the ambient app-quality debt. The soak
            # health check below diffs the canary's firing set under the
            # candidate against this baseline and fails only on NEW
            # signatures, so re-firing standing debt no longer jams the
            # fleet. Defensive: a capture fault stores [] and the health
            # check's fail-safe (legacy since_iso predicate) takes over.
            #
            # A1: capture is idempotent + PERSISTED before the deploy. The
            # soaking-flip's save (below) is the only other persistence point,
            # but it lands AFTER the canary deploy AND the ≤900s active probe.
            # If the daemon dies anywhere in that window, on-disk state is
            # still `checking` with the canary now on CANDIDATE code — and the
            # next tick re-enters here. Re-capturing then would baseline-IN any
            # candidate-introduced firing signal (a passive-only regression the
            # active probe can miss), silently passing the soak to the whole
            # fleet. The `not in` key-presence guard (NOT a truthiness check —
            # a fault-captured [] is "present") keeps the baseline stable-code-
            # derived across that crash-restart; an [] survivor takes the
            # health check's fail-safe since_iso path, never a blanket pass.
            if "soak_baseline_signatures" not in state.candidate:
                state.candidate["soak_baseline_signatures"] = (
                    _capture_canary_signal_baseline(shared_dir, cfg.canary_bot, deps))
                save_release_state(state, shared_dir)
            deploy = deps.canary_deploy_fn or (
                lambda bot, code_dir, np: _default_canary_deploy(bot, code_dir, np, deps))
            ok, detail = deploy(cfg.canary_bot, staging, network_path)
            result.log(f"canary deploy ({cfg.canary_bot} ← staging): "
                       f"{'ok' if ok else 'FAIL'} — {detail[:300]}")
            if not ok:
                _fail_candidate(repo, state, cfg, deps, network_path, result,
                                reason=f"canary deploy: {detail}",
                                shared_dir=shared_dir, canary_was_deployed=True)
                refold_operator_fields(state, shared_dir)
                save_release_state(state, shared_dir)
                return result
        else:
            result.log("degraded mode: no canary bot configured — soak is a pure timer")

        # Active canary probe (D2/B2 + D5). Exercise the candidate on the
        # canary once, here at soak entry (the canary is now on staging
        # code). The plan is the always-on D5 gateway liveness probe plus
        # the diff-selected D2 probes (same diff classification as the tier
        # — no second git call). A regression fails the soak in THIS tick
        # (minutes, not the full window); a tooling fault fails OPEN with a
        # loud degraded Signal (never a candidate failure). The passive
        # quiet-window (below) stays the backstop. The gateway probe makes
        # the plan non-empty even for a diff with no exercisable surface, so
        # every soaking canary now gets a runtime liveness check.
        plan = classify_soak_probe(changed)
        state.candidate["soak_probe_plan"] = plan
        if plan and cfg.canary_bot:
            probe = deps.soak_probe_fn or (
                lambda sd, bot, st, np, pl: _default_soak_probe(
                    sd, bot, st, np, pl, deps))
            status, pdetail = probe(shared_dir, cfg.canary_bot, staging,
                                    network_path, plan)
            kinds = ",".join(sorted({s["kind"] for s in plan}))
            result.soak_probe = status
            result.log(f"active probe ({kinds}): {status} — {pdetail[:300]}")
            if status == SOAK_PROBE_REGRESSION:
                _fail_candidate(repo, state, cfg, deps, network_path, result,
                                reason=f"active soak probe ({kinds}): {pdetail}",
                                shared_dir=shared_dir, canary_was_deployed=True)
                refold_operator_fields(state, shared_dir)
                save_release_state(state, shared_dir)
                return result
            if status == SOAK_PROBE_ERROR:
                # Tooling fault — fail OPEN (don't fail a release on our own
                # fault) but surface the degraded probe, mirroring the
                # passive health-check's degraded path below.
                _observe_signal(
                    deps, shared_dir, type_="release_soak_probe_degraded",
                    severity="warn",
                    title="Active canary soak probe could not run (failing open)",
                    body=pdetail,
                )
            # D7: record whether active validation genuinely PASSED. Only an
            # OK verdict counts — a tooling-fault ERROR failed OPEN above but
            # did NOT validate the candidate, so it must NOT earn the
            # residual-0 fast-track (it falls back to the legacy passive
            # window via tier_residual_minutes). REGRESSION already returned.
            state.candidate["soak_active_validated"] = (status == SOAK_PROBE_OK)
        else:
            # No probe ran (degraded / no canary) → not actively validated.
            state.candidate["soak_active_validated"] = False

        state.candidate["state"] = "soaking"
        state.candidate["soak_started_at"] = _iso(deps.now_fn())
        result.candidate_state = "soaking"
        refold_operator_fields(state, shared_dir)
        save_release_state(state, shared_dir)
        eff_minutes = _candidate_soak_minutes(state.candidate, cfg)
        result.log(f"active validation done — tier {tier}, "
                   f"residual ceiling {eff_minutes} min")
        # D7: the promote predicate — active_validation_passed AND
        # tier_residual_elapsed AND soak_health_clean — lives in the soaking
        # branch below. active_validation_passed is already enforced (a
        # REGRESSION fails the candidate above; only an OK/degraded candidate
        # reaches here). Fall through and evaluate the rest THIS tick instead
        # of returning: a residual-0 tier (skip/short, active-validated)
        # promotes NOW, at active-pass time — not on the next 15-min puller
        # tick. A full-tier residual (or a degraded short) logs "soaking" and
        # waits the dwell, exactly as before. One predicate, one code path.

    # state == "soaking"
    result.soak_tier = state.candidate.get("soak_tier") or ""
    eff_soak_minutes = _candidate_soak_minutes(state.candidate, cfg)
    soak_started = _parse_iso(state.candidate.get("soak_started_at"))
    if soak_started is None:
        # Missing/unparseable clock would otherwise soak forever with a
        # bare "soaking" log line. Repair: the soak starts now.
        state.candidate["soak_started_at"] = _iso(deps.now_fn())
        soak_started = deps.now_fn()
        refold_operator_fields(state, shared_dir)
        save_release_state(state, shared_dir)
        result.log("soak clock missing — restarted it")
    if cfg.canary_bot:
        health = deps.soak_health_fn or _default_soak_health
        healthy, detail = health(shared_dir, cfg.canary_bot,
                                 state.candidate.get("soak_started_at", ""),
                                 state.candidate.get("soak_baseline_signatures"))
        result.log(f"soak health: {'ok' if healthy else 'FAIL'} — {detail[:300]}")
        if healthy and "errored" in detail:
            # The check failed open (tooling problem, not canary health).
            # Keep promoting — Gate 1 covers the import class — but make
            # the degraded verification visible instead of silent.
            _observe_signal(
                deps, shared_dir, type_="release_soak_check_degraded",
                severity="warn",
                title="Canary soak health-check is erroring (failing open)",
                body=detail,
            )
        if not healthy:
            _fail_candidate(repo, state, cfg, deps, network_path, result,
                            reason=f"soak: {detail}", shared_dir=shared_dir,
                            canary_was_deployed=True)
            refold_operator_fields(state, shared_dir)
            save_release_state(state, shared_dir)
            return result

    elapsed_ok = (
        soak_started is not None
        and deps.now_fn() >= soak_started + _dt.timedelta(minutes=eff_soak_minutes)
    )
    if not elapsed_ok:
        remaining = ""
        if soak_started is not None:
            left = soak_started + _dt.timedelta(minutes=eff_soak_minutes) - deps.now_fn()
            remaining = f" (~{max(0, int(left.total_seconds() // 60))} min left)"
        result.log(f"soaking{remaining}")
        return result

    if state.pin is not None:
        result.log(
            f"soak complete but release is PINNED to {state.pin.get('sha', '')[:12]} "
            f"— holding (use `evolve-admin release unpin` to resume)"
        )
        return result

    return _promote(repo, shared_dir, state, cfg, deps, network_path, result)


def _fail_candidate(repo: Path, state: ReleaseState, cfg: ReleaseConfig,
                    deps: ReleaseDeps, network_path: Path,
                    result: ReleaseTickResult, *, reason: str,
                    shared_dir: Path, canary_was_deployed: bool) -> None:
    sha = state.candidate["sha"]
    state.candidate["state"] = "failed"
    state.candidate["failure"] = reason[:1000]
    if sha not in state.skip:
        state.skip.append(sha)
    result.candidate_state = "failed"
    result.log(f"candidate {sha[:12]} FAILED: {reason[:300]}")
    _observe_signal(
        deps, shared_dir, type_="release_canary_failed", severity="alert",
        title=f"Release candidate {sha[:12]} failed — fleet stays on stable",
        body=f"{reason}\n\nFleet remains at {state.stable['sha'][:12]}. "
             f"Fix forward on main, or `evolve-admin release retry {sha[:12]}` "
             f"after investigating.",
        details={"candidate_sha": sha, "stable_sha": state.stable["sha"]},
    )
    if canary_was_deployed:
        _restore_canary(repo, cfg, deps, network_path, result)
    _prune_staging_worktree(repo, staging_dir_for(sha, deps.staging_root), deps, result)


def _on_candidate_resolved(repo: Path, state: ReleaseState, cfg: ReleaseConfig,
                           deps: ReleaseDeps, network_path: Path,
                           result: ReleaseTickResult, *,
                           restore_canary: bool) -> None:
    sha = state.candidate["sha"] if state.candidate else None
    if sha:
        _prune_staging_worktree(repo, staging_dir_for(sha, deps.staging_root), deps, result)
    state.candidate = None
    if restore_canary:
        _restore_canary(repo, cfg, deps, network_path, result)


def _promote(repo: Path, shared_dir: Path, state: ReleaseState,
             cfg: ReleaseConfig, deps: ReleaseDeps, network_path: Path,
             result: ReleaseTickResult) -> ReleaseTickResult:
    sha = state.candidate["sha"]
    old_stable = state.stable["sha"]

    if not _is_ancestor(repo, old_stable, sha, deps.git_fn):
        _fail_candidate(repo, state, cfg, deps, network_path, result,
                        reason=f"candidate {sha[:12]} does not descend from "
                               f"stable {old_stable[:12]} (history rewrite?) — "
                               f"automatic promotion refused",
                        shared_dir=shared_dir, canary_was_deployed=bool(cfg.canary_bot))
        refold_operator_fields(state, shared_dir)
        save_release_state(state, shared_dir)
        return result

    # Operator-action re-check straight from disk: a `release pin` (or
    # rollback) issued while this tick spent minutes in gates must win
    # over the tick's in-memory snapshot.
    try:
        on_disk = load_release_state(shared_dir)
        if on_disk is not None and on_disk.pin is not None:
            state.pin = on_disk.pin
            result.log("promotion held: release was pinned mid-soak")
            save_release_state(state, shared_dir)
            return result
    except ReleaseStateCorrupt:
        result.success = False
        result.error = "release.json corrupted mid-tick; promotion aborted"
        result.log(f"FAIL {result.error}")
        return result

    if not _move_fleet(repo, sha, deps, result):
        result.success = False
        result.error = "fleet move failed at promote"
        _observe_signal(
            deps, shared_dir, type_="release_tick_failed", severity="alert",
            title="Promote failed moving the fleet checkout", body=result.error,
        )
        return result

    # Persist the pointer BEFORE the hook suite runs. The hooks restart
    # daemons; if anything kills this process mid-suite (the web-rollback
    # caller IS admin-ui, which the hooks kickstart), an unsaved pointer
    # would make the next tick's pointer-repair yank the fleet straight
    # back — silently undoing the move. State first, side effects second.
    state.previous = dict(state.stable)
    state.stable = {
        "sha": sha,
        "version": version_for_sha(repo, sha, deps.git_fn),
        "promoted_at": _iso(deps.now_fn()),
    }
    # Recency vs what we just replaced — "stable is N commits ahead of previous"
    # is the ancestry-true signal; the version-string tails (PR numbers) can run
    # backward across a forward promote (D-2 / the 2026-06-14 incident).
    _stamp_recency(repo, state.stable, state.previous.get("sha", ""), deps.git_fn)
    # Keep the pod-wide install.json version honest with the freshly promoted
    # stable so no human-facing surface shows a pod version older than the fleet.
    _record_pod_version(state.stable.get("version", ""), shared_dir, result)
    # Fold mid-tick operator skips in BEFORE pruning, so the prune sees
    # the full set and we don't resurrect already-pruned ancestors.
    refold_operator_fields(state, shared_dir)
    state.pin = None  # pin was re-checked from disk above; it is None
    # Skip-list hygiene: anything now an ancestor of stable is moot.
    state.skip = [
        s for s in state.skip
        if not _is_ancestor(repo, s, sha, deps.git_fn)
    ]
    state.candidate = None
    save_release_state(state, shared_dir)
    _move_tags(repo, sha, state.previous["sha"], deps)

    hooks = deps.hooks_fn or (lambda r, b, a: _default_hooks(r, b, a, deps))
    try:
        ok, detail = hooks(repo, old_stable, sha)
    except Exception as e:  # noqa: BLE001 — pointer is saved; never crash the tick
        ok, detail = False, f"{type(e).__name__}: {e}"
    result.log(f"promote hooks: {'ok' if ok else 'FAIL'} — {detail[:400]}")
    if not ok:
        # Code has moved; hooks partially ran. This is the same posture
        # the legacy puller takes (HEAD advanced; hook failures are
        # logged loudly, not rolled back) — but surface it as a Signal.
        _observe_signal(
            deps, shared_dir, type_="release_promote_hooks_failed", severity="alert",
            title=f"Promoted to {sha[:12]} but post-move hooks failed",
            body=detail,
        )

    # The canary ran staging code via plists that bake staging paths;
    # bring it home BEFORE pruning the worktree those plists point at.
    # (If this process dies before the restore, the post-promote
    # lagging-bot sweep redeploys the canary from the fleet — the soak
    # exemption lifted when candidate cleared above.)
    if cfg.canary_bot:
        _restore_canary(repo, cfg, deps, network_path, result,
                        shared_dir=shared_dir)
    _prune_staging_worktree(repo, staging_dir_for(sha, deps.staging_root),
                            deps, result)

    result.promoted_to = sha
    result.fleet_sha = sha
    result.candidate_sha = ""
    result.candidate_state = ""
    result.log(f"PROMOTED {old_stable[:12]} → {sha[:12]} "
               f"(version {state.stable['version']})")
    _observe_signal(
        deps, shared_dir, type_="release_promoted", severity="info",
        title=f"Release {state.stable['version']} promoted to the fleet",
        body=f"{old_stable[:12]} → {sha[:12]}",
        details={"sha": sha, "version": state.stable["version"]},
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Operator commands (CLI surface lives in cli.py; logic lives here)
# ─────────────────────────────────────────────────────────────────────────────


def release_promote(
    repo: Path = DEFAULT_REPO,
    shared_dir: Path = DEFAULT_SHARED_DIR,
    *,
    cfg: ReleaseConfig | None = None,
    deps: ReleaseDeps | None = None,
) -> ReleaseTickResult:
    """Operator override: skip the remaining soak and promote the CURRENT
    candidate now. Gate 1 must already have passed (state == "soaking").

    Shared by the CLI (``evolve-admin release promote``) and the admin-UI
    "Make live now" button. Runs entirely as the *invoking* user — no
    privilege escalation. The admin server and the repo-puller daemon both
    run as ``evolve``, which owns ``release.json`` and the fleet checkout,
    so this needs no sudo.

    It promotes the on-disk soaking candidate by calling the shared
    ``_promote`` path **directly** — it does NOT fetch origin and does NOT
    run a ``release_tick``. That sharing keeps every promote invariant
    (ancestry check, mid-soak pin re-check from disk, pointer-before-hooks
    ordering, canary restore, staging-worktree prune) identical to the
    auto-promote path without re-implementing any of it here.

    Why not a re-fetching tick (the old mechanism): the tick's candidate-
    selection step re-reads ``origin/main`` and, whenever origin has advanced
    past the soaking sha (the common case on an active repo), **supersedes**
    the operator's candidate with the newer tip and restarts Gate 1 + soak —
    so promote moved nothing and the operator saw "no effect". Promoting the
    current candidate directly is deterministic; newer origin commits are not
    lost — the next normal tick sees origin tip ≠ the new stable and rides it
    through Gate 1 + soak as the next candidate. Never raises; failures are
    reported on the returned result.
    """
    result = ReleaseTickResult()
    deps = deps or ReleaseDeps()
    try:
        state = load_release_state(shared_dir)
    except ReleaseStateCorrupt as e:
        result.success = False
        result.error = (f"release.json corrupt ({e}) — promotion frozen; "
                        f"run `evolve-admin release init` after investigating")
        return result
    if state is None or state.candidate is None:
        result.success = False
        result.error = "no candidate in flight — nothing to promote"
        return result
    cand_state = state.candidate.get("state")
    sha12 = (state.candidate.get("sha") or "")[:12]
    if cand_state == "failed":
        result.success = False
        result.error = (f"candidate {sha12} failed its gates; run "
                        f"`release retry` after investigating, or fix forward")
        return result
    if cand_state != "soaking":
        result.success = False
        result.error = ("candidate has not passed Gate 1 yet — wait for the next "
                        "tick (or run `evolve-admin repo-pull`) first")
        return result
    # Promote the candidate the operator is looking at, straight through the
    # shared path — no origin fetch, no candidate-selection step (which would
    # supersede it whenever origin advanced). `_promote` enforces ancestry,
    # re-checks a mid-soak pin from disk, saves the pointer BEFORE the hooks,
    # restores the canary, and prunes the staging worktree.
    cfg, network_path = _resolve_release_env(repo, shared_dir, deps, cfg)
    return _promote(repo, shared_dir, state, cfg, deps, network_path, result)


def _commits_between(repo: Path, base: str, tip: str, git_fn) -> list[str]:
    """``base..tip`` as one-line ``<short-sha> <subject>`` strings (newest
    first), or ``[]`` if the range can't be computed. Used to show the
    operator exactly what un-soaked commits a force-move promotes."""
    rc, out, _ = git_fn(repo, ["log", "--oneline", "--no-decorate",
                               f"{base}..{tip}"])
    if rc != 0 or not out:
        return []
    return [ln for ln in out.splitlines() if ln.strip()]


def _force_move_fleet(
    repo: Path, shared_dir: Path, state: ReleaseState, target: str, *,
    cfg: ReleaseConfig, deps: ReleaseDeps, network_path: Path,
    result: ReleaseTickResult, skip_fled: bool, pin_reason: str,
    done_log_fn: Callable[[str], str], signal_type: str,
    signal_title_fn: Callable[[str], str], signal_body: str,
    signal_details: dict,
) -> ReleaseTickResult:
    """Shared body of :func:`release_rollback` and :func:`release_bootstrap`
    — a deliberate operator force-move of the fleet to an already-resolved
    ``target`` sha.

    Both verbs bypass the soak gate, so neither enforces ancestry; what
    differs is direction (back vs forward), whether the fled sha is
    skip-listed, and the audit Signal emitted. Everything else — cancelling
    an in-flight candidate, the **pointer-before-hooks** ordering, the
    auto-pin (``pin.sha == stable.sha`` invariant), and the canary restore
    — is identical and lives here exactly once, so a fix to one of those
    hard-won invariants can never reach only one of the two paths.

    The caller has already loaded ``state``, resolved ``target``, and
    rejected ``target == stable``. ``done_log_fn``/``signal_title_fn``
    receive the post-move version string. Never raises.
    """
    git = deps.git_fn
    fled = state.stable["sha"]

    # Cancel any in-flight candidate (prune its worktree). restore_canary is
    # False here: the canary is redeployed below onto the post-move fleet
    # code, so an interim restore onto stable would be wasted work.
    if state.candidate is not None:
        result.log(f"cancelling in-flight candidate {state.candidate['sha'][:12]}")
        _on_candidate_resolved(repo, state, cfg, deps, network_path, result,
                               restore_canary=False)

    if not _move_fleet(repo, target, deps, result):
        result.success = False
        result.error = "fleet move failed"
        return result

    # Persist the pointer BEFORE the hook suite. The hooks kickstart
    # admin-ui — which is the very process running this function when the
    # move came from the web UI. With the pointer unsaved, that kill would
    # leave the fleet moved but release.json still naming the fled sha, and
    # the next tick's pointer-repair would silently undo the operator's move
    # — the exact failure 7.2 exists to kill. State first, side effects
    # second.
    version = version_for_sha(repo, target, git)
    state.previous = dict(state.stable)
    state.stable = {
        "sha": target,
        "version": version,
        "promoted_at": _iso(deps.now_fn()),
    }
    # Ancestry-true recency vs what we moved off (rollback/bootstrap can move
    # EITHER direction, so this may legitimately report behind — the count is
    # the truth, the PR-number tail is not). Keep install.json's pod version in
    # step with the moved-to stable (D-2).
    _stamp_recency(repo, state.stable, state.previous.get("sha", ""), git)
    _record_pod_version(version, shared_dir, result)
    if skip_fled and fled not in state.skip:
        state.skip.append(fled)
    state.pin = {"sha": target, "pinned_at": _iso(deps.now_fn()), "reason": pin_reason}
    save_release_state(state, shared_dir)
    _move_tags(repo, target, fled, deps)

    hooks = deps.hooks_fn or (lambda r, b, a: _default_hooks(r, b, a, deps))
    try:
        ok, detail = hooks(repo, fled, target)
    except Exception as e:  # noqa: BLE001 — pointer is saved; finish what we can
        ok, detail = False, f"{type(e).__name__}: {e}"
    result.log(f"post-move hooks: {'ok' if ok else 'FAIL'} — {detail[:400]}")
    if not ok:
        _observe_signal(
            deps, shared_dir, type_="release_promote_hooks_failed",
            severity="alert",
            title=f"Moved to {target[:12]} but post-move hooks failed",
            body=f"{detail}\n\nDaemons may still be running pre-move "
                 f"code — run `sudo evolve-admin repo-pull --hooks-from "
                 f"{fled[:12]} --hooks-to {target[:12]}` to retry.",
        )

    # Canary back onto fleet code (which is now the move target).
    _restore_canary(repo, cfg, deps, network_path, result, shared_dir=shared_dir)

    result.fleet_sha = target
    result.pinned = True
    result.log(done_log_fn(version))
    _observe_signal(
        deps, shared_dir, type_=signal_type, severity="alert",
        title=signal_title_fn(version), body=signal_body, details=signal_details,
    )
    return result


def release_rollback(
    repo: Path = DEFAULT_REPO,
    shared_dir: Path = DEFAULT_SHARED_DIR,
    *,
    to_ref: str = "",
    dry_run: bool = False,
    cfg: ReleaseConfig | None = None,
    deps: ReleaseDeps | None = None,
) -> ReleaseTickResult:
    """One-command rollback: fleet → previous stable (or ``to_ref``).

    Cancels any in-flight candidate (worktree pruned, canary restored
    to the rollback target), runs the full hook suite, pins to the
    target, and skip-lists the fled sha so ``unpin`` alone can't
    re-promote it.
    """
    deps = deps or ReleaseDeps()
    result = ReleaseTickResult()
    git = deps.git_fn

    if cfg is None:
        try:
            from .config import load_network, DEFAULT_NETWORK_CONFIG
            cfg = resolve_release_config(
                load_network(deps.network_path or DEFAULT_NETWORK_CONFIG))
        except Exception:
            cfg = ReleaseConfig(mode="canary")
    network_path = deps.network_path or Path(shared_dir) / "network.json"

    try:
        state = load_release_state(shared_dir)
    except ReleaseStateCorrupt as e:
        result.success = False
        result.error = (f"release.json corrupt ({e}); run `evolve-admin release init` "
                        f"then rollback with --to <sha>")
        return result
    if state is None:
        result.success = False
        result.error = "no release.json — nothing to roll back (run a canary-mode tick first)"
        return result

    if to_ref:
        rc, target, err = git(repo, ["rev-parse", "--verify", f"{to_ref}^{{commit}}"])
        if rc != 0:
            result.success = False
            result.error = f"cannot resolve --to {to_ref!r}: {err}"
            return result
    elif state.previous and state.previous.get("sha"):
        target = state.previous["sha"]
    else:
        result.success = False
        result.error = "no previous release recorded — pass --to <sha|tag>"
        return result

    fled = state.stable["sha"]
    if target == fled:
        result.success = False
        result.error = f"target {target[:12]} is already the current stable"
        return result

    result.log(f"rollback: {fled[:12]} → {target[:12]}")
    if dry_run:
        result.log("dry-run: no changes made")
        return result

    return _force_move_fleet(
        repo, shared_dir, state, target,
        cfg=cfg, deps=deps, network_path=network_path, result=result,
        # Rollback flees a bad release: skip-list the fled sha so `unpin`
        # alone can't re-promote it on the next tick.
        skip_fled=True,
        pin_reason=f"rollback from {fled[:12]}",
        done_log_fn=lambda v: (
            f"ROLLED BACK to {target[:12]} (version {v}); pinned — "
            f"`evolve-admin release unpin` to resume auto-promotion"),
        signal_type="release_rolled_back",
        signal_title_fn=lambda v: f"Fleet rolled back to {v}",
        signal_body=(f"{fled[:12]} → {target[:12]}; {fled[:12]} skip-listed; "
                     f"release pinned."),
        signal_details={"from_sha": fled, "to_sha": target},
    )


def release_bootstrap(
    repo: Path = DEFAULT_REPO,
    shared_dir: Path = DEFAULT_SHARED_DIR,
    *,
    to_ref: str = "",
    dry_run: bool = False,
    cfg: ReleaseConfig | None = None,
    deps: ReleaseDeps | None = None,
) -> ReleaseTickResult:
    """Force-move the fleet FORWARD to ``to_ref``, bypassing the soak gate.

    This is the sanctioned way to deploy a fix to the release pipeline
    *itself*. The pipeline has a bootstrap deadlock: the soak gate is
    evaluated by the release daemon **from the deploy checkout** (= current
    stable), not from the candidate, so a fix to the gate can never promote
    *through* the broken gate — it would be soaked by the very code it
    repairs. ``bootstrap`` is the one verb that force-promotes past the
    gate while still moving the fleet correctly (pointer-before-hooks,
    auto-pin, canary restore — the same machinery as ``rollback``).

    Differences from ``rollback`` (which is a force-move *backward*):
    - it moves forward, so the fled sha is an ancestor of the target and is
      **not** skip-listed (a skip would block re-promoting it later for no
      reason);
    - it emits a distinct ``release_bootstrapped`` Signal, so the Alerts
      page reads "bootstrapped" — not "rolled back" — when the operator
      deliberately moved forward.

    Like ``rollback`` it **auto-pins** (``pin.sha == target == new stable``)
    so auto-promotion stays frozen until the operator has verified the
    bootstrap took (gate fixed, daemons healthy) and runs ``release unpin``
    — at which point normal soaking resumes for anything past the target.

    Ancestry is not enforced (it is a force-move), but a target that does
    not descend from stable is flagged loudly in the steps. ``dry_run``
    reports the un-soaked ``stable..to_ref`` commit range and changes
    nothing. Never raises; failures are reported on the result.
    """
    deps = deps or ReleaseDeps()
    result = ReleaseTickResult()
    git = deps.git_fn

    if cfg is None:
        try:
            from .config import load_network, DEFAULT_NETWORK_CONFIG
            cfg = resolve_release_config(
                load_network(deps.network_path or DEFAULT_NETWORK_CONFIG))
        except Exception:
            cfg = ReleaseConfig(mode="canary")
    network_path = deps.network_path or Path(shared_dir) / "network.json"

    if not to_ref:
        result.success = False
        result.error = "bootstrap requires a target ref (the sha/tag/version to move to)"
        return result

    try:
        state = load_release_state(shared_dir)
    except ReleaseStateCorrupt as e:
        result.success = False
        result.error = (f"release.json corrupt ({e}); run `evolve-admin release init` "
                        f"then bootstrap again")
        return result
    if state is None:
        result.success = False
        result.error = ("no release.json — nothing to bootstrap (run a canary-mode "
                        "tick first to initialize the pointer)")
        return result

    rc, target, err = git(repo, ["rev-parse", "--verify", f"{to_ref}^{{commit}}"])
    if rc != 0:
        result.success = False
        result.error = f"cannot resolve {to_ref!r}: {err}"
        return result

    fled = state.stable["sha"]
    if target == fled:
        result.success = False
        result.error = (f"{target[:12]} is already the current stable — nothing to "
                        f"bootstrap. To freeze auto-promotion here use `release pin`.")
        return result

    # Loud banner: name exactly what un-soaked commits this promotes.
    commits = _commits_between(repo, fled, target, git)
    forward = _is_ancestor(repo, fled, target, git)
    if not commits:
        # `stable..target` is empty ⇒ target is at or BEHIND stable (the
        # ==stable case was already refused above, so this is strictly
        # behind). bootstrap is a forward verb; a backward move is a
        # rollback — redirect rather than mislabel it as a bootstrap.
        result.success = False
        result.error = (f"{target[:12]} is not ahead of stable {fled[:12]} — "
                        f"nothing to bootstrap forward. To move BACK to it use "
                        f"`sudo evolve-admin release rollback --to {to_ref}`.")
        return result
    result.log(f"bootstrap: {fled[:12]} → {target[:12]} "
               f"({len(commits)} commit(s), UN-SOAKED — bypassing the soak gate)")
    if not forward:
        result.log(f"WARNING: {target[:12]} does not descend from stable "
                   f"{fled[:12]} (not a fast-forward — divergent history). "
                   f"Listing commits reachable from the target but not stable.")
    for ln in commits[:40]:
        result.log(f"  • {ln}")
    if len(commits) > 40:
        result.log(f"  … and {len(commits) - 40} more")
    if dry_run:
        result.log("dry-run: no changes made")
        return result

    n = len(commits)
    plural = "s" if n != 1 else ""
    return _force_move_fleet(
        repo, shared_dir, state, target,
        cfg=cfg, deps=deps, network_path=network_path, result=result,
        # Forward move: the fled sha is an ancestor of the target. Skip-
        # listing it would needlessly block ever re-promoting it; nothing
        # past the target is touched, so after `unpin` the next tick soaks
        # any newer origin/main commit normally.
        skip_fled=False,
        pin_reason=f"bootstrap past soak gate (was {fled[:12]})",
        done_log_fn=lambda v: (
            f"BOOTSTRAPPED to {target[:12]} (version {v}); pinned — verify the "
            f"fleet, then `evolve-admin release unpin` to resume auto-promotion"),
        signal_type="release_bootstrapped",
        signal_title_fn=lambda v: f"Fleet bootstrapped to {v} (soak gate bypassed)",
        signal_body=(f"{fled[:12]} → {target[:12]}: {n} commit{plural} force-promoted "
                     f"to the fleet WITHOUT soak coverage (release-pipeline bootstrap). "
                     f"Release pinned — `evolve-admin release unpin` to resume "
                     f"auto-promotion."),
        signal_details={"from_sha": fled, "to_sha": target, "unsoaked_commits": n},
    )


def release_pin(shared_dir: Path, *, ref: str = "", repo: Path = DEFAULT_REPO,
                deps: ReleaseDeps | None = None,
                reason: str = "operator pin") -> tuple[bool, str]:
    """Freeze auto-promotion at the CURRENT stable. Returns ``(ok, message)``.

    ``pin`` is a freeze, never a move: it holds the fleet exactly where it
    is so a bad auto-promotion can't land while an operator investigates.
    The invariant ``state.pin["sha"] == state.stable["sha"]`` holds at
    every write site — here, :func:`release_rollback`, and
    :func:`release_bootstrap` — so a pin never names a sha the fleet isn't
    actually on. That is precisely what made the old ``pin <some-ref>`` a
    confusing dead-data no-op: it recorded a ``pin.sha`` that disagreed
    with stable and moved *nothing* (the pointer-repair and deploy
    checkout both follow ``state.stable``, never ``state.pin``).

    A ``ref`` argument is therefore only an *assertion* that stable is
    where the operator thinks it is. If ``ref`` resolves to anything other
    than the current stable the pin is REFUSED (``ok=False``) and the
    operator is redirected to the verb that actually moves the fleet:
    ``release bootstrap`` (forward) or ``release rollback --to`` (back).
    """
    deps = deps or ReleaseDeps()
    state = load_release_state(shared_dir)
    if state is None:
        return False, "no release.json — nothing to pin"
    sha = state.stable["sha"]
    if ref:
        rc, resolved, err = deps.git_fn(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
        if rc != 0:
            return False, f"cannot resolve {ref!r}: {err}"
        if resolved != sha:
            return False, (
                f"`release pin` only FREEZES auto-promotion at the current stable "
                f"({sha[:12]}) — it does NOT move the fleet, and {ref!r} resolves to "
                f"{resolved[:12]}, which is not stable.\n"
                f"  • To MOVE the fleet forward to {resolved[:12]} (e.g. to deploy a "
                f"release-pipeline fix past a broken gate):\n"
                f"      sudo evolve-admin release bootstrap {ref}\n"
                f"  • To move BACK to a prior release:\n"
                f"      sudo evolve-admin release rollback --to {ref}\n"
                f"  • To just freeze at the current stable, drop the ref:\n"
                f"      sudo evolve-admin release pin"
            )
    state.pin = {"sha": sha, "pinned_at": _iso(deps.now_fn()), "reason": reason}
    save_release_state(state, shared_dir)
    return True, f"pinned to {sha[:12]} — auto-promotion held until `release unpin`"


def release_unpin(shared_dir: Path) -> str:
    state = load_release_state(shared_dir)
    if state is None:
        return "no release.json — nothing to unpin"
    if state.pin is None:
        return "not pinned"
    state.pin = None
    save_release_state(state, shared_dir)
    return "unpinned — auto-promotion resumes next tick"


def release_retry(shared_dir: Path, sha_prefix: str) -> str:
    state = load_release_state(shared_dir)
    if state is None:
        return "no release.json"
    matches = [s for s in state.skip if s.startswith(sha_prefix)]
    if not matches:
        return f"no skip-listed sha matches {sha_prefix!r}"
    for s in matches:
        state.skip.remove(s)
    if state.candidate and state.candidate.get("sha") in matches:
        state.candidate = None
    save_release_state(state, shared_dir)
    return f"removed {len(matches)} sha(s) from the skip list — re-evaluated next tick"


def release_set_soak(
    shared_dir: Path,
    selector: str,
    *,
    sha_prefix: str = "",
    repo: Path = DEFAULT_REPO,
    cfg: ReleaseConfig | None = None,
    deps: ReleaseDeps | None = None,
) -> str:
    """Operator override: bump the in-flight candidate's soak UP (D1).

    ``selector`` is a tier (``skip`` | ``short`` | ``full``) or an integer
    number of minutes. ``sha_prefix`` pins the action to a specific
    candidate (default: the current one). The override only ever ADDS
    caution — it refuses any change that would shorten the effective soak
    window, pointing the operator at ``release promote`` to go faster
    instead. ``skip`` is never accepted here (same reasoning). Applies on
    the next tick. Never raises; returns an operator-facing message."""
    deps = deps or ReleaseDeps()
    if cfg is None:
        try:
            from .config import load_network, DEFAULT_NETWORK_CONFIG
            cfg = resolve_release_config(load_network(DEFAULT_NETWORK_CONFIG))
        except Exception:
            cfg = ReleaseConfig()
    try:
        state = load_release_state(shared_dir)
    except ReleaseStateCorrupt as e:
        return f"release.json corrupt ({e}) — run `evolve-admin release init`"
    if state is None or state.candidate is None:
        return "no candidate in flight — nothing to adjust"
    cand = state.candidate
    sha12 = (cand.get("sha") or "")[:12]
    if sha_prefix and not (cand.get("sha") or "").startswith(sha_prefix):
        return f"candidate {sha12} does not match {sha_prefix!r}"
    if cand.get("state") == "failed":
        return (f"candidate {sha12} already failed its gates — "
                f"`release retry` after investigating, or fix forward")

    cur_eff = _candidate_soak_minutes(cand, cfg)
    sel = (selector or "").strip().lower()
    if sel in _SOAK_TIER_RANK:
        if sel == SOAK_TIER_SKIP:
            return ("`release soak skip` is not allowed — the override only adds "
                    "caution. Use `release promote` to skip the remaining soak.")
        new_tier = sel
        new_override: int | None = None
        # Compute the new tier's residual against THIS candidate's active-
        # validation verdict (same source cur_eff uses), so the shorten-guard
        # below compares like with like — a tier bump that lowers the residual
        # is still refused, a bump that raises it is allowed.
        new_eff = tier_residual_minutes(
            new_tier, cfg.soak_minutes,
            active_validated=bool(cand.get("soak_active_validated")))
        label = f"tier {new_tier}"
    else:
        try:
            mins = int(sel)
        except ValueError:
            return (f"unrecognized soak setting {selector!r} — use "
                    f"skip|short|full or a number of minutes")
        if mins < 0:
            return "minutes must be ≥ 0"
        new_tier = cand.get("soak_tier") or SOAK_TIER_FULL
        new_override = mins
        new_eff = mins
        label = f"{mins} min"

    if new_eff < cur_eff:
        return (f"that would shorten the soak ({cur_eff} → {new_eff} min). The "
                f"override only adds caution — use `release promote` to go faster.")

    cand["soak_tier"] = new_tier
    if new_override is None:
        cand.pop("soak_override_minutes", None)
    else:
        cand["soak_override_minutes"] = new_override
    save_release_state(state, shared_dir)
    return (f"candidate {sha12} soak set to {label} "
            f"({cur_eff} → {new_eff} min) — applies on the next tick")


def release_init(repo: Path, shared_dir: Path,
                 deps: ReleaseDeps | None = None) -> str:
    """Explicit (re)initialization — the recovery path for corrupt
    state. Never called automatically."""
    deps = deps or ReleaseDeps()
    rc, head, err = deps.git_fn(repo, ["rev-parse", "HEAD"])
    if rc != 0:
        return f"cannot read fleet HEAD: {err}"
    state = ReleaseState(stable={
        "sha": head,
        "version": version_for_sha(repo, head, deps.git_fn),
        "promoted_at": _iso(deps.now_fn()),
    })
    save_release_state(state, shared_dir)
    _move_tags(repo, head, None, deps)
    return f"release.json initialized: stable = {head[:12]}"


def _status_recency(repo: Path, state: "ReleaseState", git_fn) -> dict[str, Any]:
    """Ancestry-true recency for the `release status` surface, computed live
    (git is free on this on-demand path). Returns::

        {
          "stable_commit_date": "2026-06-14" | None,
          "stable_vs_previous": {"ahead": 5, "behind": 0} | None,
          "stable_vs_origin": {"ahead": 4, "behind": 0} | None,
          "candidate_commit_date": "..." | None,
          "candidate_vs_stable": {"ahead": 3, "behind": 0} | None,
        }

    ``ahead`` > 0 with ``behind`` == 0 means a clean fast-forward (newer); a
    nonzero ``behind`` (possible after a rollback) means the pointer moved
    back. The CLI renders these as words ("ahead"/"behind") so the operator
    never has to subtract PR numbers. All fields degrade to None if git can't
    resolve the pair.

    ``stable_vs_origin`` is the origin tip's relationship to the promoted
    stable: its ``ahead`` is the count of commits on ``origin/main`` that the
    fleet does NOT yet have — i.e. how many commits the gated checkout sits
    *behind* tip (the D-5 legibility number). The caller refreshes the local
    ``origin/main`` ref first (``release_status(fetch_origin=True)``) so this is
    the true tip, not the last 15-min puller fetch."""
    stable_sha = (state.stable or {}).get("sha") or ""
    prev_sha = (state.previous or {}).get("sha") or ""
    cand_sha = (state.candidate or {}).get("sha") or ""
    rec: dict[str, Any] = {
        "stable_commit_date": commit_date_for_sha(repo, stable_sha, git_fn) or None,
        "stable_vs_previous": None,
        "stable_vs_origin": None,
        "candidate_commit_date": None,
        "candidate_vs_stable": None,
    }
    if prev_sha:
        ac = ancestry_counts(repo, prev_sha, stable_sha, git_fn)
        if ac.get("ahead") is not None:
            rec["stable_vs_previous"] = ac
    if stable_sha:
        # origin tip vs stable: ahead = commits on origin/main the fleet lacks
        # = "fleet is N commits behind tip". Reuses ancestry_counts (the same
        # left-right rev-list the other recency fields use) — never PR-number
        # arithmetic, never a bespoke diff parser.
        ac = ancestry_counts(repo, stable_sha,
                             f"{DEFAULT_REMOTE}/{DEFAULT_BRANCH}", git_fn)
        if ac.get("ahead") is not None:
            rec["stable_vs_origin"] = ac
    if cand_sha:
        rec["candidate_commit_date"] = (
            commit_date_for_sha(repo, cand_sha, git_fn) or None)
        ac = ancestry_counts(repo, stable_sha, cand_sha, git_fn)
        if ac.get("ahead") is not None:
            rec["candidate_vs_stable"] = ac
    return rec


def release_status(repo: Path, shared_dir: Path,
                   cfg: ReleaseConfig | None = None,
                   deps: ReleaseDeps | None = None,
                   *, fetch_origin: bool = False) -> dict[str, Any]:
    """Status dict for `evolve-admin release status` (+ JSON consumers).

    ``fetch_origin`` (off by default — the CLI path keeps its cheap behavior)
    best-effort refreshes the local ``origin/main`` ref before computing
    recency, so ``behind_origin`` reflects the true tip rather than the last
    15-min puller fetch. The admin-UI ``GET /api/release/status`` passes it: the
    admin server runs as the ``evolve`` user that owns the fleet checkout, so
    the fetch needs no sudo. A fetch failure (offline, slow) is swallowed — the
    count just falls back to the last-known ref.

    ``behind_origin`` is the headline D-5 legibility number: how many commits
    ``origin/main`` is ahead of the promoted stable pointer (``None`` when git
    can't resolve the pair). It makes the canary invariant — the fleet tracks
    the *pointer*, not tip — visible at a glance."""
    deps = deps or ReleaseDeps()
    if cfg is None:
        try:
            from .config import load_network, DEFAULT_NETWORK_CONFIG
            cfg = resolve_release_config(load_network(DEFAULT_NETWORK_CONFIG))
        except Exception:
            cfg = ReleaseConfig()
    out: dict[str, Any] = {
        "mode": cfg.mode,
        "canary_bot": cfg.canary_bot or None,
        "soak_minutes": cfg.soak_minutes,
        "degraded_no_canary": not cfg.canary_bot,
    }
    if fetch_origin:
        # Refresh origin/main so "behind tip" is exact. Best-effort: a network
        # failure (timeout / OSError) leaves the last-known ref in place and the
        # count degrades to the last fetch rather than erroring the status read.
        with contextlib.suppress(Exception):
            deps.git_fn(repo, ["fetch", DEFAULT_REMOTE, DEFAULT_BRANCH], timeout=30)
    rc, head, _ = deps.git_fn(repo, ["rev-parse", "HEAD"])
    out["fleet_sha"] = head if rc == 0 else None
    try:
        state = load_release_state(shared_dir)
    except ReleaseStateCorrupt as e:
        out["state"] = "CORRUPT"
        out["error"] = str(e)
        return out
    if state is None:
        out["state"] = "uninitialized (first canary-mode tick will set stable = fleet HEAD)"
        return out
    out["stable"] = state.stable
    out["previous"] = state.previous
    out["candidate"] = state.candidate
    out["pin"] = state.pin
    out["skip"] = state.skip
    # Ancestry-true recency, computed LIVE here (the CLI is on-demand, not
    # polled, so git is free and authoritative — it backfills pre-D-2 state
    # that lacks the stored fields). This is what lets `release status` say
    # "stable is 5 commits ahead of previous" instead of leaving the operator
    # to (mis)read v2884 < v2885 as a backward move. ``relation`` is keyed by
    # the same word the CLI prints; values degrade to None on git failure.
    out["recency"] = _status_recency(repo, state, deps.git_fn)
    # Headline legibility number (D-5): commits the fleet is behind origin/main
    # tip = origin's `ahead` over stable. None when git can't resolve the pair.
    origin_rel = out["recency"].get("stable_vs_origin") or {}
    out["behind_origin"] = origin_rel.get("ahead")
    if state.candidate and state.candidate.get("state") == "soaking":
        eff = _candidate_soak_minutes(state.candidate, cfg)
        out["soak_tier"] = state.candidate.get("soak_tier") or SOAK_TIER_FULL
        out["soak_minutes_effective"] = eff
        started = _parse_iso(state.candidate.get("soak_started_at"))
        if started is not None:
            left = started + _dt.timedelta(minutes=eff) - deps.now_fn()
            out["soak_minutes_remaining"] = max(0, int(left.total_seconds() // 60))
    return out


def canary_bot_during_soak(shared_dir: Path, network: dict | None) -> str | None:
    """The bot to exempt from lagging-bot redeploy / deploy-drift while a
    soak is active, else None. Shared by repo_puller and
    deploy_drift_monitor so both carve-outs agree."""
    try:
        cfg = resolve_release_config(network)
        if not cfg.canary_bot:
            return None
        state = load_release_state(Path(shared_dir))
        if state is None or state.candidate is None:
            return None
        if state.candidate.get("state") == "soaking":
            return cfg.canary_bot
        return None
    except Exception:
        return None
