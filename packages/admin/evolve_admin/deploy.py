"""
Bot deployment and launchd job management.
Requires admin (sudo) privileges for cross-user operations.
"""

from __future__ import annotations

import grp
import json
import os
import re
import secrets
import pwd
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .app_cron_map import merge_app_cron_map
from .config import get_bot_workspace, get_bot_port, get_bot_user, load_network, save_network, is_reserved_account
# Re-exported under their historical private names so module-level patch
# targets (tests) and the wizard import keep working; defs live in
# bot_cost_defaults to keep this frozen file from growing.
from .bot_cost_defaults import set_bot_created_at as _be_set_bot_created_at
from .bot_cost_defaults import set_per_bot_daily_hard as _be_set_per_bot_daily_hard

# bot_id (OpenClaw instance name) → macOS account name. Canonical impl:
# (bot_id, config=None) — passes a provided network dict through, self-loads
# network.json when omitted. Filesystem paths (/Users/<user>/…) and
# `sudo -u <user>` need the macOS user; plugin config (botId), network.json
# keys, and log labels stay keyed by the instance name.
from evolve_config import CANONICAL_NETWORK_JSON as _CANONICAL_NETWORK_JSON
from evolve_config import CANONICAL_SHARED_DIR as _CANONICAL_SHARED_DIR
from evolve_config import get_bot_user as _bot_user_for
from evolve_config import user_home as _user_home
from platform_profile import get_profile as _get_profile
from .plugin_signature import INSTALL_TREE_FILES, stamp_install_tree, verify_plugin_signature
from .runtime import (
    JobSpec,
    get_perms,
    get_scheduler,
    is_timer_activated_oneshot,
    render_launchd_plist,
)
from .runtime.perms import POD_READ_ACL_PERMS as _POD_READ_ACL_PERMS
from . import secret_config_perms as _secret_perms, evo_gateway_client as _evo_gw
from . import bot_version_sync as _vsync
from .brave_key import resolve_pod_brave_key
from . import bot_doc_seeding as _bot_docs, evo_socket_acl as _evo_sock_acl, deploy_resilience as _dres, tier_prefs_acl as _tier_prefs_acl, shared_bot_dir_perms as _bot_dir_perms
from .sudo_dest import redirect_refusal, sudo_dest_refusal  # D-2 gates: file- and dir-shaped bot-owned dests
from .telemetry import get_logger
from .analyzer_monitor_jobs import ANALYZER_MONITOR_PLIST_LABELS  # labels live next to their installers

_log = get_logger("deploy")


# review.py retired 2026-08-14 (deny mandate folded into arbiter/security_screen.py); validate.py dropped — it runs in-process on the admin host (web/server.py), never on bots.
# apply.py removed 2026-08-18 — the legacy per-bot apply watcher was
# structurally unreachable (it polled proposals/approved/, a dir no arbiter
# status maps to).
# NB this list has no readers: nothing copies analyzer scripts to bots (deploy
# step 3 is "using repo path directly (no copy needed)") and the plists point
# at ANALYZER_DIR inside the deploy checkout. Editing it removes nothing from
# any pod. What removes apply.py from a pod is the git deletion arriving via
# the repo-puller; see _bootout_retired_per_bot_jobs for the teardown ordering
# that actually holds.
ANALYZER_SCRIPTS = ["measure.py", "analyze.py",
                    "heal.py", "cost.py",
                    # test_runner.py removed 2026-06-08 — app-test surface killed
                    "evolve_config.py",
                    # Phase 6.2 blessed home for atomic-write/now-iso primitives;
                    # several scripts above import it, so it must ship alongside.
                    "evolve_util.py",
                    "outcome.py",
                    # Continuity Engine v2 — defer tool + pod-wide runner.
                    # v1 (task_queue.py / task_extractor.py / task_runner.py /
                    # inline_executor.py / recurrence.py) was removed — the bot
                    # now schedules its own follow-ups via the defer plugin
                    # tool, fired by defer_runner.
                    "defer_queue.py", "defer_runner.py",
                    "slack_signals.py", "expansion.py",
                    "spend_alert.py", "cron_alert.py",
                    "weekly_review.py", "community_intel.py",
                    # App Gallery & Forge engine — bot-side runner (forge_runner.py)
                    # imports evolve_admin from the installed package (with a
                    # direct-path fallback)
                    "forge_runner.py",
                    # Recommendations engine — Phase 1: profile + gallery
                    "recommendations.py", "profile_builder.py", "gallery_recommender.py",
                    # Phase 6: app usage correlation + aggregation
                    "app_session_correlator.py", "usage_logger.py", "usage_by_app.py"]
ANALYZER_DATA_FILES: list[str] = []  # static files copied alongside scripts (security_rules.json retired 2026-08-14 with review.py — the mandate is code now, not shipped data)
LAUNCHD_DIR = Path("/Library/LaunchDaemons")

# ── Plugin-config defaults registry ──────────────────────────────────────────
# Single source of truth for the non-identity, non-role-conditional fields that
# ``ensure_plugin_config`` writes into ``plugins.entries.evolve.config``.
#
# Identity-only fields (botId, role, networkId, sharedDir) are NOT here — they
# come from per-call inputs (network rename, role change, etc.). dashboardEnabled
# is also NOT here because it is role-conditional (primary → True, member → False).
#
# Adding a new plugin-config default is a one-line change here. ensure_plugin_config
# always gap-fills missing/None values from this registry on every deploy, so the
# rewrite-vs-gap-fill split that caused the PR #312 incident is gone.
_PLUGIN_CONFIG_DEFAULTS: dict[str, Any] = {
    # Plugin behavior. classifierModel: STATIC last-resort only — deploys pass a credential-derived registry via openclaw_materializer.plugin_defaults_for_bot
    "classifierModel": "anthropic/claude-haiku-4-5",
    "tierClassification": "session",

    # Per-bot Evolve integration tier — one of "off" / "monitor" / "manage" / "full".
    # Default "full" preserves pre-tier behavior. To opt a bot out of intrusive
    # Evolve features (pod conduct injection, defer tool, recommendation injection),
    # change this field in the bot's openclaw.json directly. The gap-fill below
    # never overwrites a manually-set value, so editing the JSON is sticky.
    # Capability semantics live in packages/plugin/src/config.ts (TIERS table).
    "tier": "full",

    # Budget Hawk v2 cost hygiene (the fields PR #312 had to gap-fill)
    "summarizerMinTurns": 2,
    "classifierKeywordConfidenceFloor": 0.80,
    "costLedgerEnabled": True,
    # Layer-2 tool-call enforcement ARMING flag. False (default) = OBSERVE-ONLY:
    # the below-LLM before_tool_call gate records every would-block but always
    # allows the call; True (or nested `layer2.enforce: true`) arms real blocking.
    # Merging is non-arming. Spec: docs/spec-user-roster-and-roles-2026-06-07.md §8.
    "layer2Enforce": False,
    # Context-observability Phase 0 prefix-hash ledger: dark by default; set True
    # per-bot to arm (sticky). Spec: docs/spec-context-observability-2026-07-30.md.
    "prefixHashLedgerEnabled": False,
}

# Path to this package's root (packages/admin/) → repo root
_REPO_ROOT = Path(__file__).parent.parent.parent.parent
PLUGIN_SRC_DIR = _REPO_ROOT / "packages" / "plugin"


def _allowed_plugin_config_keys() -> set[str]:
    """Keys accepted by ``plugins.entries.evolve.config``, per the manifest the
    GATEWAY actually loads — the DEPLOYED/staged plugin at PLUGIN_INSTALL_DIR,
    NOT the repo source. The two can skew: the staged plugin lags the admin
    code when the repo-puller needs 2-3 passes to fast-forward (VPS), so a key
    the SOURCE already declares (``repoRoot``, #3115) can still be rejected by
    the running gateway's strict ``additionalProperties: false`` schema —
    making OC reject the WHOLE config on every reload. Keying the allowed set
    on the DEPLOYED manifest means the strip pass removes exactly the keys the
    gateway would reject, no sooner and no later (the #1525 ``reportingEnabled``
    self-heal still works: it strips once the deployed manifest drops the key).

    Falls back to the SOURCE manifest only when the staged copy is absent or
    unreadable (fresh bring-up before the first restage — source == staged
    there). Empty set when neither is readable — callers treat that as "skip
    the strip pass" so a broken manifest never blocks deploys.
    """
    from .openclaw_materializer import deployed_plugin_config_keys
    return deployed_plugin_config_keys(PLUGIN_INSTALL_DIR, PLUGIN_SRC_DIR)


# Version stamping + identity-based sync detection live in bot_version_sync.py
# (deploy.py is a size-capped hot-hazard file). EVOLVE_VERSION is a display
# string (NOT monotonic); the synced decision uses commit identity. See that
# module's docstring and the 2026-06-25 incident.
EVOLVE_VERSION = _vsync.compute_version(_REPO_ROOT)
EVOLVE_COMMIT_SHA, EVOLVE_COMMIT_COUNT = _vsync.compute_commit_identity(_REPO_ROOT, _log)


def deploy_stamp(deployed_at: str | None = None) -> dict[str, Any]:
    """The install.json per-bot version record for the CURRENTLY-running code —
    the single source every stamp site uses (thin bind of
    :func:`bot_version_sync.build_deploy_stamp` to the running identity)."""
    return _vsync.build_deploy_stamp(
        EVOLVE_VERSION, EVOLVE_COMMIT_SHA, EVOLVE_COMMIT_COUNT, deployed_at)


# Canonical deploy-layer paths, platform-keyed via platform_profile (W7; mirrors evolve_config's module-level `Path(get_profile().shared_dir_default)`). Resolved at import from sys.platform — byte-identical /Users/Shared on macOS, /var/lib siblings on a Linux pod (design-linux-port §6). PLUGIN_INSTALL_DIR is the root-owned plugin install (openclaw loads from here; admins git-pull PLUGIN_SRC_DIR).
_PROFILE = _get_profile()
# chown routing (W7-followup, design-linux-port §6): every chown in this module uses `_PROFILE.chown` (the binary: /usr/sbin/chown macOS, /usr/bin/chown Linux) and routes the admin group through `_PROFILE.admin_group` (wheel macOS / root Linux — `wheel` is NOT gid 0 on Ubuntu and absent by default, so a literal `:wheel` chown HARD-fails there). A bot account's PRIMARY group (`:staff`) is deliberately LEFT literal — `staff` exists on Ubuntu (gid 50) so `<bot>:staff` succeeds; per-OS primary-group resolution is a follow-up (would risk macOS byte-identity, and platform_profile keeps the per-account group out of the profile by design).
PLUGIN_INSTALL_DIR = Path(_PROFILE.plugin_install_dir)
ANALYZER_DIR = _REPO_ROOT / "packages" / "analyzer"
VENV_PYTHON = _PROFILE.venv_python
VENV_EVOLVE_ADMIN = _PROFILE.venv_evolve_admin


# ── openclaw CLI path discovery ───────────────────────────────────────────────
#
# Every `sudo -u <bot> openclaw ...` invocation matches the sudoers grant
# `evolve ALL=(ALL) NOPASSWD: <oc_path>` — that grant is installed by
# setup_wizard._write_evolve_sudoers() using the absolute path discovered
# at setup time. We must use the SAME absolute path at runtime for sudo's
# command match to succeed.
#
# Historically deploy.py wrapped every call in `env HOME=... PATH=... openclaw`
# which broke sudo matching: the grant is for `openclaw`, not `/usr/bin/env`.
# Result was "sudo: a password is required" on every deploy from the evolve
# service user. Fix: use the absolute openclaw path directly, let `sudo -H`
# set HOME to the bot's home, and let the `Defaults:evolve secure_path`
# sudoers directive supply PATH.

_OPENCLAW_BIN: str | None = None


def _openclaw_bin() -> str:
    """Absolute path to the openclaw CLI. Cached after first lookup.

    Delegates to the single shared resolver
    ``platform_profile.find_openclaw_cli()`` so the runtime path matches what
    setup_wizard baked into the sudoers file (both go through the same
    candidate list, which covers the node_modules .mjs entrypoint that Apple
    Silicon Homebrew uses). Falls back to the bare 'openclaw' (PATH-relative)
    if nothing is found — that will fail sudo matching but at least produces a
    useful error.
    """
    global _OPENCLAW_BIN
    if _OPENCLAW_BIN is None:
        from platform_profile import find_openclaw_cli
        _OPENCLAW_BIN = find_openclaw_cli() or "openclaw"
    return _OPENCLAW_BIN


def _secure_stage(content: str, *, suffix: str = ".json", mode: int = 0o600) -> Path:
    """Stage ``content`` into an unpredictable, exclusively-created /tmp file
    for a subsequent ``sudo /bin/cp`` into a root- or bot-owned destination.

    Replaces the old ``/tmp/evolve-<bot>-<purpose>.json`` predictable-name
    pattern, which is a local TOCTOU/symlink privilege-escalation surface on a
    multi-user box: ``/tmp`` is world-writable and ``cp`` follows symlinks, so an
    attacker could pre-create the path (or swap it between our write and the
    root ``cp``) and have root copy attacker content into a bot's openclaw.json.

    ``mkstemp`` defeats both halves of that race — it opens with ``O_EXCL`` (an
    attacker cannot pre-create the path) and uses a random suffix (unguessable).
    Default mode 0600; pass 0644 when a *bot-user* subprocess must read the
    staged file (e.g. ``openclaw config validate`` via OPENCLAW_CONFIG_PATH).
    Caller is responsible for unlinking the returned path.
    """
    fd, name = tempfile.mkstemp(dir="/tmp", prefix="evolve-stage-", suffix=suffix)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(name, mode)
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
    return Path(name)


# First-party manifest apps bundled with Evolve.
# Each entry is an app_id matching a directory under packages/analyzer/evolve_apps/.
FIRST_PARTY_EVOLVE_APPS = [
    "security-cve-scan",
]


# ── Version tracking ──────────────────────────────────────────────────────────

def read_install_json(shared_dir: Path = _CANONICAL_SHARED_DIR) -> dict | None:
    """Read install.json from the shared directory. Returns None if not present or unreadable."""
    path = shared_dir / "install.json"
    try:  # read_text covers both missing (ENOENT) and unreadable (EACCES under a clamp) without a bare .exists() that would RAISE on Py3.12
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def write_install_json(
    shared_dir: Path,
    network_id: str,
    bots: list[str],
    repo_path: str | None = None,
    bot_versions: dict[str, dict] | None = None,
) -> None:
    """Write install.json to the shared directory after a successful install/upgrade.

    Captures version, timestamp, network_id, bots list, repo path, and per-bot
    version records so subsequent runs can distinguish fresh / repair / upgrade /
    downgrade and detect bots that are out of sync with the current codebase.

    ``bot_versions`` is merged over any existing per-bot records so a partial
    redeploy (one bot at a time) doesn't wipe records for bots not touched in
    this run.
    """
    from datetime import datetime, timezone
    # Preserve existing bot_versions for bots not touched in this run
    existing = read_install_json(shared_dir) or {}
    merged_bot_versions: dict[str, dict] = {**existing.get("bot_versions", {})}
    if bot_versions:
        merged_bot_versions.update(bot_versions)

    data = {
        "version": EVOLVE_VERSION,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "network_id": network_id,
        "bots": bots,
        "repo_path": repo_path or str(_REPO_ROOT),
        "bot_versions": merged_bot_versions,
    }
    path = shared_dir / "install.json"
    try:
        path.write_text(json.dumps(data, indent=2))
        subprocess.run(["chmod", "644", str(path)], capture_output=True, check=False)
    except PermissionError:  # bootstrap left install.json root-owned; stage+cp (grant: setup_wizard §10a) then chown evolve so the fast write_text path wins next time (parity w/ macOS evolve-owned {shared})
        tmp_path = _secure_stage(json.dumps(data, indent=2))
        subprocess.run(["sudo", "/bin/cp", str(tmp_path), str(path)], check=True, capture_output=True)
        subprocess.run(["sudo", "/bin/chmod", "644", str(path)], capture_output=True)
        subprocess.run(["sudo", _PROFILE.chown, f"{EVOLVE_SERVICE_USER}:{_PROFILE.admin_group}", str(path)], capture_output=True)
        tmp_path.unlink(missing_ok=True)


def record_bot_deploy(bot_id: str, shared_dir: Path = _CANONICAL_SHARED_DIR) -> None:
    """Stamp bot_id as deployed at the current EVOLVE_VERSION in install.json.

    Safe to call after a partial deploy (touches only the bot_versions entry for
    this bot; all other install.json fields are left unchanged).
    """
    path = shared_dir / "install.json"
    existing = read_install_json(shared_dir) or {}
    bot_versions: dict[str, dict] = {**existing.get("bot_versions", {})}
    bot_versions[bot_id] = deploy_stamp()
    existing["bot_versions"] = bot_versions
    payload = json.dumps(existing, indent=2)
    try:
        path.write_text(payload)
        subprocess.run(["chmod", "644", str(path)], capture_output=True, check=False)
    except PermissionError:  # see write_install_json: stage+cp (grant §10a) then chown evolve so the daemon's fast path wins next deploy
        tmp_path = _secure_stage(payload)
        subprocess.run(["sudo", "/bin/cp", str(tmp_path), str(path)], check=True, capture_output=True)
        subprocess.run(["sudo", "/bin/chmod", "644", str(path)], capture_output=True)
        subprocess.run(["sudo", _PROFILE.chown, f"{EVOLVE_SERVICE_USER}:{_PROFILE.admin_group}", str(path)], capture_output=True)
        tmp_path.unlink(missing_ok=True)


# ── Plist lifecycle management ─────────────────────────────────────────────────


def per_bot_evolve_plist_labels(bot_id: str) -> list[str]:
    """The Evolve-owned per-bot launchd labels installed for every member bot.

    SOURCE OF TRUTH for the per-bot daemon set. ``expected_plist_labels`` (used
    by deploy/upgrade) and ``retire.stop_bot_services`` (used by retire-bot /
    remove-evolve) both consume this list so they cannot drift.

    NOTE: ``measure`` is **not** in this list — it's been pod-wide since the
    migration to ``ai.openclaw.evolve.measure`` (run as the evolve user for
    every bot). Legacy per-bot measure plists may still exist on disk on
    long-lived pods; retire-bot handles those defensively by scanning
    ``/Library/LaunchDaemons/`` for any ``ai.openclaw.evolve.*.<bot>.plist``
    file at runtime, not from this list.
    """
    return [
        # ai.openclaw.evolve.apply.{bot_id} removed 2026-08-18 — the legacy
        # per-bot apply watcher (packages/analyzer/apply.py) scanned
        # proposals/approved/, a directory no arbiter status maps to
        # (arbiter/store.py::_STATUS_TO_SUBDIR), so it had applied nothing in
        # its entire logged history. See
        # docs/design-proposal-signing-key-2026-08-18.md.
        # ai.openclaw.evolve.test.{bot_id} removed 2026-06-08 — app-test
        # surface killed per docs/decision-app-tests-2026-06-08.md.
        # Both are swept off existing pods by _bootout_retired_per_bot_jobs
        # on the next deploy.
        # cost_event converter — replaces the broken plugin llm_output emit
        # path (silent since OC 2026.4.29 stopped firing the hook on the
        # embedded runner). See packages/analyzer/cost_event_converter.py.
        f"ai.openclaw.evolve.cost-converter.{bot_id}",
        # App-audit runners — Tier 2 (6h structural) + Tier 3 (hourly cadence).
        # Installed by _install_launchd_audit_runner[_tier3] during deploy_bot.
        f"ai.openclaw.evolve.audit-runner.{bot_id}",
        f"ai.openclaw.evolve.audit-runner-t3.{bot_id}",
        # Nightly `openclaw doctor --fix` runner (PR #1748). Installed by
        # _install_launchd_doctor_pass during deploy_bot. Runs as the bot
        # user at 03:17 + jitter. Missing from this list pre-2026-05-30
        # meant the orphan-sweeper flagged every doctor-pass plist as
        # "from a previous Evolve version" on every upgrade and deleted
        # them; the next deploy reinstalled them; loop. Operators saw
        # 8 spurious orphan warnings on every Versions-page upgrade banner.
        f"ai.openclaw.evolve.doctor-pass.{bot_id}",
        # Nightly git-backup daemon — runs as the bot user at 02:00 daily,
        # one independent daemon per bot. Installed by
        # ``_install_launchd_backup`` during ``deploy_bot``. Naming uses the
        # ``ai.evolve.<bot>.*`` prefix (not ``ai.openclaw.evolve.*.<bot>``)
        # so the legacy-glob in ``retire._glob_legacy_per_bot_plists`` does
        # not catch it — this entry is the only thing keeping the
        # orphan-sweeper from deleting all per-bot backup plists on upgrade.
        f"ai.evolve.{bot_id}.backup",
    ]


def per_bot_gateway_plist_label(bot_id: str) -> str:
    """The OpenClaw gateway launchd label for a bot (the user-facing 'bot is
    running' daemon). Distinct from the Evolve infra daemons. For the PRIMARY
    bot resolve via ``primary_bot_id(network) or "evolve"`` — EVO-LINUX-PHANTOM-GATEWAY.
    """
    return f"ai.openclaw.{bot_id}-gateway"


def expected_plist_labels(network: dict, *, realized_only: bool = False) -> set[str]:
    """Return the canonical set of launchd labels that the current Evolve version
    should have installed in /Library/LaunchDaemons/ for the given network.

    Used by upgrade and repair to detect orphaned plists left behind by previous
    versions or removed features.

    Also consults the per-bot template-installs manifest at
    ``{sharedDir}/template-installs/<bot_id>.json`` so plists installed
    by ``bot_templates.apply_embedded_app`` (V1.1-1+) are NOT flagged as
    orphans on the next deploy. Without this consultation the
    orphan-sweeper would delete every template-installed LaunchDaemon
    on its next pass — the install-side mirror of the C1.d retire-bot
    drift bug.
    """
    members = network.get("members", [])
    labels: set[str] = set()

    # Per-bot jobs (every network member) — sourced from
    # ``per_bot_evolve_plist_labels`` so retire-bot stays in sync.
    for bot_id in members:
        for label in per_bot_evolve_plist_labels(bot_id):
            labels.add(label)

    # iMessage poller — per-bot, only when imessage.json is configured.
    # We check for the config file to avoid adding the label to bots that
    # never had iMessage installed (the orphan-sweeper would then delete
    # a non-existent plist, which is harmless but noisy).
    for bot_id in members:
        imessage_label = _imessage_poller_plist_label(bot_id)
        imessage_plist = LAUNCHD_DIR / f"{imessage_label}.plist"
        if imessage_plist.exists():
            labels.add(imessage_label)

    # Template-installed per-bot plists — read from the manifest written
    # by ``bot_templates.cli_integration.apply_embedded_app``. Limited to
    # launchdaemon-destination entries (launchagent plists live in the
    # bot user's home, not /Library/LaunchDaemons/, so the orphan-sweeper
    # never sees them).
    shared_dir = network.get("sharedDir", str(_CANONICAL_SHARED_DIR))
    try:
        from .template_installs import template_installed_labels
        for bot_id in members:
            for label in template_installed_labels(
                shared_dir, bot_id, destination="launchdaemon",
            ):
                labels.add(label)
    except Exception as exc:
        # Manifest unreadable: behave as if no template-installed plists
        # are recorded. The orphan-sweeper will still spare anything in
        # the static label set; the only risk is removing a template-
        # installed plist whose manifest is corrupt. Log loudly so the
        # operator can intervene.
        _log.warning(
            "expected_plist_labels: could not read template-installs "
            "manifests under %s: %s", shared_dir, exc,
        )

    # Scheduled-actions plists per bot. PR #2164's install_launchd_command_action
    # (and PR #2168's apply-actions code path that re-runs it) installs
    # LaunchDaemons at ``ai.evolve.<bot>.<app>`` labels under
    # /Library/LaunchDaemons/. Each install stamps the action's
    # ``installed_artifact`` field with the full plist path, which is what
    # we walk here to recover the label. Pre-2026-06-05 those labels were
    # NOT in the expected set, so the orphan-sweeper deleted them on
    # every deploy — application-installed crons silently vanished after
    # the first ``evolve-admin deploy --all`` post-install, and the
    # related ea-pack timers went with them. The fix has to live here
    # (not in the installer) because the sweeper is what enforces the
    # invariant.
    try:
        from .applications.manifest import list_manifests as _list_manifests
    except ImportError:
        _list_manifests = None  # type: ignore[assignment]

    if _list_manifests is not None:
        from pathlib import Path as _P
        shared_dir_path = _P(shared_dir)
        for bot_id in members:
            try:
                manifests = _list_manifests(shared_dir_path, bot_id)
            except Exception as exc:  # noqa: BLE001
                # Per-bot best-effort — one bot's unreadable manifests
                # dir mustn't blank the whole expected set (that would
                # turn every legitimate plist on the pod into an orphan
                # candidate).
                _log.warning(
                    "expected_plist_labels: list_manifests failed for "
                    "bot %s: %s", bot_id, exc,
                )
                continue
            for manifest in manifests:
                for action in (manifest.scheduled_actions or []):
                    if not isinstance(action, dict):
                        continue
                    # Prefer installed_artifact (stamped by Phase 4.5 with
                    # the actual on-disk path) over plist_label (which
                    # may still carry the ${bot_id} placeholder unresolved).
                    artifact = action.get("installed_artifact") or ""
                    if isinstance(artifact, str) and artifact.endswith(".plist"):
                        # A stamped artifact is authoritative about WHERE the
                        # plist landed, and expected_plist_labels governs
                        # system LaunchDaemons only (LAUNCHD_DIR ==
                        # /Library/LaunchDaemons). A scheduled action can
                        # instead install a per-user LaunchAgent into the bot's
                        # home (/Users/<bot>/Library/LaunchAgents/<label>.plist
                        # or ~/Library/LaunchAgents/...) — e.g. a bot-created
                        # gateway-selfheal, or a legacy per-bot spend-alert
                        # left over from before those daemons went pod-wide.
                        # Such a label MUST NOT enter the daemon set:
                        # pod_health._check_launchd would look for it under
                        # /Library/LaunchDaemons/, never find it, and fire a
                        # perpetual false ``pod_health_launchd`` "Plist not
                        # found" Signal (observed 2026-06-11 as two false
                        # alert-severity Signals). The template-installs branch
                        # above already filters to destination="launchdaemon"
                        # for exactly this reason; mirror it here. Compare on
                        # the directory name as well as the path so the
                        # monkeypatched-LAUNCHD_DIR tests (tmp/LaunchDaemons)
                        # still classify a /Library/LaunchDaemons artifact as a
                        # daemon.
                        parent = _P(artifact).parent
                        is_launch_daemon = (
                            parent == LAUNCHD_DIR
                            or parent.name == LAUNCHD_DIR.name
                        )
                        if is_launch_daemon:
                            label_from_artifact = _P(artifact).name[:-len(".plist")]
                            if label_from_artifact:
                                labels.add(label_from_artifact)
                        # The stamp is the source of truth either way — a
                        # non-daemon (LaunchAgent) artifact contributes nothing
                        # and must not fall through to the plist_label guess.
                        continue
                    # Fallback: derive from plist_label with ${bot_id}
                    # substitution. Mirrors install_launchd_command_action's
                    # _substitute_install_vars (we don't import it here to
                    # keep this function side-effect-free — the substitution
                    # for sweeper purposes is just bot_id, never workspace).
                    install_cfg = action.get("install") or {}
                    # Skip the unstamped plist_label guess when (a) realized_only
                    # (pod_health): no stamp == never materialized, so the guess
                    # would invent a label for a daemon never on disk → false
                    # "Plist not found" (the orphan-sweeper instead keeps the guess
                    # to spare just-installed pre-stamp crons); or (b) the action
                    # explicitly targets a LaunchAgent (~/Library/LaunchAgents/).
                    destination = str(install_cfg.get("destination") or "").strip().lower()
                    if realized_only or destination == "launchagent":
                        continue
                    label = (install_cfg.get("plist_label") or "").strip()
                    if label:
                        label = label.replace("${bot_id}", bot_id)
                        if label:
                            labels.add(label)

    # Infra jobs (evolve user) + primary bot's OC gateway (resolved, not phantom).
    labels.add(per_bot_gateway_plist_label(_resolve_evolve_app_target(network)[0]))
    labels.update({
        "ai.openclaw.evolve.analyze.evolve",
        # ai.openclaw.evolve.report.evolve retired 2026-06-05 — see
        # below; the per-bot daily digest content was merged into
        # ai.evolve.evolve.pod-report-daily. Removed from the expected
        # set so the orphan-sweeper removes the leftover plist from
        # existing pods on the next deploy.
        "ai.openclaw.evolve.outcome.evolve",
        "ai.openclaw.evolve.defer-runner",   # Continuity Engine v2 — pod-wide, no per-bot suffix
        "ai.openclaw.evolve.manifest-reflex-runner",   # Manifest Reflex — pod-wide, sweeps every bot's reflex queue
        "ai.openclaw.evolve.app-posture-review",       # App Posture — weekly per-bot inventory snapshot
        "ai.openclaw.evolve.slack-signals.evolve",
        "ai.openclaw.evolve.expansion.evolve",
        "ai.evolve.evolve.spend-alert",
        "ai.evolve.evolve.cron-alert",
        "ai.evolve.evolve.pod-report-daily",   # v2 single self-gating hourly plist
                                               # (replaced morning/evening split — see
                                               # _install_launchd_pod_report_daily)
        "ai.evolve.evolve.weekly-review",
        "ai.evolve.evolve.weekly-bot-trends",   # Sunday per-bot 7d-horizon trend digest; installed by _install_launchd_weekly_bot_trends("evolve"). Sibling to weekly-review (RSI) and report (daily per-bot).
        # `ai.evolve.<bot>.backup` is installed per-bot during deploy_bot
        # and is enumerated by `per_bot_evolve_plist_labels` above (one
        # daemon per bot, runs as the bot user). The legacy single
        # `ai.evolve.evolve.backup` (iterated all bots as evolve) and
        # `ai.evolve.security_bot-backup` (vestigial bash-script pointer) were
        # both retired 2026-05-25 — see `_install_launchd_backup`.
        "ai.evolve.evolve.heal",
        "ai.evolve.evolve.pod-health",   # 1-min gateway-liveness Signal emitter (alert-notifier Phase 0a)
        "ai.evolve.evolve.signal-notifier",   # 1-min Signal-transition notifier (alert-notifier Phase 4)
        "ai.evolve.evolve.audit",
        "ai.evolve.evolve.update-watcher",   # Phase E2: daily upstream-update checks
        "ai.evolve.evolve.anthropic-admin-ingest",   # daily: snapshot Anthropic cost-report + audit logs, emit cost_diverges_from_anthropic signal
        "ai.evolve.evolve.retention",        # daily: signals/watchdog/proposals cleanup
        "ai.evolve.evolve.log-cap",          # daily 03:35: cap flat-file logs (audit.log, better_engine.log, audit-warns.jsonl) by size
        "ai.evolve.evolve.oc-log-rotate",    # daily 04:30: truncate launchd-captured gateway.log/.err.log (bot-owned, needs sudo) when >10MB
        "ai.evolve.evolve.openclaw-overrides-expiry",  # daily 04:00: enforce expires_at on per-bot openclaw overrides
        "ai.evolve.evolve.proposal-auto-resolve",   # daily 03:45: archive proposals whose motivating_signals[] are all cleared
        "ai.evolve.evolve.breakers-audit",   # every 5 min: write audit_summary + audit_recommendation back to active breaker trips
        "ai.evolve.evolve.breakers-runner",  # every 10 min: evaluate the activity-shape detector, log decisions to {shared}/breakers/runner-log/, and act on trips unless network.json::breakers.auto_trip_enabled is false (default true — ARMED since the §5.2 arming PR; `evolve-admin breaker disarm` = observe-only). Installed by _install_launchd_breakers_runner. Spec: docs/spec-circuit-breakers-2026-05-21.md §5.1 / §8 Phase 5.
        "ai.evolve.evolve.proposal_synthesizer",  # every 6h: LLM synthesis over candidates/synthesizing/
        "ai.evolve.evolve.cost_watchdog",
        "ai.evolve.evolve.session_economics",  # hourly cache-health + bot-engagement Signal emitter
        "ai.evolve.evolve.embedding_monitor",
        "ai.evolve.evolve.verify",
        "ai.evolve.evolve.admin-ui",
        "ai.evolve.evolve.mcp-bridge",   # 2026-05-30: converted from per-user LaunchAgent (~/Library/LaunchAgents/com.evolve.mcp-bridge.plist) → system-scope LaunchDaemon. User-scope structurally can't load on headless pods (no Aqua session for admin user). See mcp_service.py header.
        "ai.evolve.evolve.repo-puller",   # added 2026-04-29 — auto-pulls origin/main every 15min
        "ai.evolve.evolve.audit-scheduler",       # hourly tick: infra audit + per-bot audit-outbox drain. Renamed 2026-06-08 from app-test-scheduler when the app-test surface was killed.
        "ai.evolve.evolve.pairing-sweep",        # 30s sweep: auto-approves pod-admin / primary-owner / auto_admit-channel pending pairings; never auto-approves blocked. Installed by pairing.auto_approver.install_launchd. Spec: docs/spec-user-roster-and-roles-2026-06-07.md.
        "ai.evolve.evolve.upstream-issues-watcher",  # GitHub upstream-issues watcher; feature-gated by install_profile (upstream_issues_watcher). Plist absent when feature off — listing the label here is harmless and keeps orphan-sweeper from deleting it after feature flips off mid-uptime.
        "ai.evolve.evolve.inbound-issues-watcher",   # GitHub inbound-issues watcher; same feature-gated pattern as upstream-issues-watcher (inbound_issues_watcher feature flag).
        "ai.evolve.evolve.digest-flush",     # hourly tick; alert-digest dispatcher self-gates to digest_hour_local. Installed by digest_dispatcher.install_launchd. Omitted before 2026-05-26 → flagged orphan.
        "ai.evolve.evolve.security-cve-scan-finalize",  # daily 09:10 PT; CVE-scan finalizer. Installed by _install_launchd_cve_scan_finalize("evolve"). Omitted before 2026-05-26 → flagged orphan.
        "ai.evolve.evolve.usage-logger", "ai.evolve.evolve.usage-by-app",   # the two daily app-usage sweeps, both installed by _install_launchd_usage_jobs("evolve"): 03:30 manifest-mtime footprint (usage-stats.json; the FALLBACK signal since AL-1.3) + 03:35 per-app annotation rollup (usage-by-app.json; the PRIMARY one). usage-logger was omitted before 2026-05-26 → flagged orphan.
        # Evolve-level jobs (run for all bots, installed by migrate-jobs / setup)
        "ai.openclaw.evolve.measure",        # daily metrics for all bots (replaces per-bot)
        "ai.openclaw.evolve.better",         # Better Engine 15-min refresh + WatchPaths
        "ai.openclaw.evolve.deploy_drift_monitor",   # hourly: emit Signal when bots are behind admin code
        "ai.openclaw.evolve.bot_recovery_monitor",   # hourly: emit Signal per heal recovered_alerts
        "ai.openclaw.evolve.stuck_proposal_monitor", # hourly: emit Signal when approved proposals sit >7d
        "ai.openclaw.evolve.backup_signal",          # hourly: emit Signal when a bot's nightly backup has bounced 3+ times
        "ai.openclaw.evolve.local_backup_signal",    # hourly: emit Signal on Time Machine gaps (Phase 2 of backup architecture)
        "ai.openclaw.evolve.backup_audit_signal",    # hourly: defense-in-depth audit of pushed backup tree against classification (Phase 4a)
        "ai.openclaw.evolve.local_backup_excluder",  # hourly: sync ephemeral classification → tmutil exclusions (Phase 4c, opt-in via network.json::backup.tm_exclusion_sync)
        "ai.openclaw.evolve.alerts_loop_monitor",    # hourly: Signal producer for dispatcher-log loop patterns. Installed by _install_launchd_alerts_loop_monitor. Omitted before 2026-05-26 → flagged orphan.
        "ai.openclaw.evolve.tuples",                 # daily 01:30: L3 tuple extraction into observations/. Installed by _install_launchd_tuples. Omitted before 2026-05-26 → flagged orphan.
        "ai.openclaw.evolve.monitor_coverage",       # daily SELF_AUDIT: emit Signal when any Evolve monitor's stdout log goes silent past expected cadence
        "ai.openclaw.evolve.install_integrity_monitor",  # daily: wizard-verify gauntlet (ownership/agent/channels) → Signals when install drift detected. Spec: docs/spec-wizard-verification-gauntlet-2026-05-30.md.
        "ai.openclaw.evolve.cascade_pressure_watchdog",  # 60s: tier-cascade pressure-flag emitter. Reads cascade telemetry spans + in-process tier1 counters, writes {shared}/cascade/pressure_flags.json. CascadeController reads the flags at decision time to throttle escalation. Installed by _install_launchd_cascade_pressure_watchdog. Spec: docs/spec-tier-cascade-2026-05-26.md § pressure watchdog.
        "ai.openclaw.evolve.cascade_audit_runner",       # hourly: cascade telemetry → Signals + labels. Bridges anomaly_detector + labeler + plugin-recorded runaway/dangerous-combo flags into the pod's standard alerting layer (Signals via signals.store.observe) and the Phase 4 calibration layer (labels at {shared}/cascade/labels/<day>.jsonl). Installed by _install_launchd_cascade_audit_runner. Spec: docs/spec-tier-cascade-2026-05-26.md § audit layer.
        "ai.openclaw.evolve.pod_perms_drift_monitor",    # hourly: runs ensure_pod_perms(check_only=True), emits Signal on drift. Catches the class where a per-bot daemon (running as bot user) is the first writer to a shared dir, so the dir gets bot-owned ownership; with sticky 1777, cross-user renames then fail (e.g. dismissing a proposal owned by a different bot daemon). ensure_pod_perms's contract is correct; this daemon just closes the gap between deploys. Installed by _install_launchd_pod_perms_drift_monitor.
        "ai.openclaw.evolve.gmail_integration_health",   # 30 min: per-bot Google API probe → Signals per (bot, failure_category); auto-resolves on next clean probe. Installed by _install_launchd_gmail_integration_health. Spec: docs/spec-google-integration-paths-2026-05-30.md §8 (PR δ).
        "ai.openclaw.evolve.oc_substrate_monitor",       # hourly: freshness Signal producer for OC auto-updater + usage-collector state files (live outside the ai.{evolve,openclaw}.evolve.* namespace monitor_coverage walks). Installed by _install_launchd_oc_substrate_monitor.
        "ai.openclaw.evolve.home_artifacts_monitor",     # hourly: per-bot workspace large/exec-file + macOS Quarantine-DB Signal producer. Replaces the retired pod-admin-side openclaw-watchdog checks. Installed by _install_launchd_home_artifacts_monitor.
        "ai.openclaw.evolve.code_quality_monitor",       # daily: repo-process KPIs (revert rate, fix-heavy scopes, same-day fix-on-feat). Signals catch dev-workflow drift before the next revert. Installed by _install_launchd_code_quality_monitor.
        "ai.evolve.evolve.reconcile-audit",              # daily 04:30: walks every installed app's scheduled_actions[] vs the current gallery and emits Signals per drifted (bot, app) pair (type=scheduled_actions_drift). Idempotent — sweep_resolves when the operator runs `evolve-admin reconcile-actions --apply` (or otherwise clears drift). Installed by _install_launchd_reconcile_audit. Closes the proactive-detection gap that the 2026-06-04 Atlas Daily Digest incident exposed.
        "ai.evolve.evolve.digest-source-audit",          # daily 04:35: walks every bot's workspace/digest/source_health-*.json files, tracks per-(bot, source) consecutive failures, emits Signals per source dark for ≥3 runs (type=digest_source_broken). Auto-resolves when source recovers. Installed by _install_launchd_digest_source_audit. Surfaces broken RSS/Brave/GitHub sources before the operator notices the digest is missing content.
        "ai.evolve.evolve.agent-bypass-audit",           # daily 04:40: walks recent session transcripts for bots with at-risk apps installed (manifests whose bot_guidance routes a chat trigger through a bot-local script). Two sibling producers over the same walk (run_agent_bypass_audit.py wrapper): agent_bypass_audit counts trigger messages that did NOT invoke the declared script (type=agent_bypass); app_script_failure_audit counts declared scripts the agent invoked but that FAILED — the "(agent) failed" exec chip (type=app_script_failure). One Signal per (bot, app); both auto-resolve when the bot returns to compliance. Installed by _install_launchd_agent_bypass_audit. Phase 1 of docs/spec-agent-freelance-bypass-2026-06-05.md.
        "ai.evolve.evolve.signal-subscriber",            # long-running (KeepAlive): watches {shared}/signals/firing/ and dispatches generators with subscribes_to: [<signal_type>, ...] on Signal arrival. Daily generator_runner sweep is the safety net. Installed by _install_launchd_signal_subscriber. Spec: docs/spec-signal-subscriber-2026-05-31.md.
        "ai.evolve.evolve.delivery-monitor",             # every 5 min: per-window delivery outcomes for scheduled user-facing apps (manifest scheduled_actions[] + optional delivery_contract{}). Tri-state Signals (app_delivery_missed / app_delivery_unmeasurable) + {shared}/delivery_monitor/ledger/<date>.jsonl. Heal (§8): one kickstart/bootstrap attempt per missed window, canary-gated to Morning Briefing v2 (HEAL_CANARY_APP_IDS) during the soak. Installed by _install_launchd_delivery_monitor. Spec: docs/spec-proactive-delivery-monitor-2026-06-10.md.
        # Analyzer-monitor family — each label lives next to its installer in
        # analyzer_monitor_jobs, so adding a monitor is one edit, not two.
        *ANALYZER_MONITOR_PLIST_LABELS,
        # NOTE: ai.openclaw.evolve.heal removed — was a redundant install of the
        # canonical ai.evolve.evolve.heal daemon (which is also in this set above).
        # Both running simultaneously caused two heal probes per 5-min cycle on
        # different offsets → close-together probes triggered openclaw 1006
        # connection-cleanup race on evolve specifically → recurring kill cycle.
        # Verified: stopping the redundant daemon eliminated all evolve down events
        # over a controlled 30-min window (was +2/30min, became +0/30min).
        # Removing it from expected_plist_labels causes find_orphaned_plists +
        # remove_orphaned_plists to clean up the leftover plist on next deploy.
    })

    return labels


# Map of plist label → feature-toggle name (the ``install_profile.is_feature_enabled``
# key). These plists are intentionally absent on installs where the feature is
# off, but the labels remain in :func:`expected_plist_labels` so the
# orphan-sweeper doesn't delete them when the feature flips off mid-uptime.
# Health checks consult this map to distinguish "intentionally absent" from
# "broken install" — otherwise a stock standard-profile install reports a
# spurious FAIL on every Pod Health scan (the
# ``upstream-issues-watcher`` / ``inbound-issues-watcher`` Plist-not-found
# error operators kept seeing despite install-infra-jobs being a no-op).
FEATURE_GATED_PLIST_LABELS: dict[str, str] = {
    "ai.evolve.evolve.upstream-issues-watcher": "upstream_issues_watcher",
    "ai.evolve.evolve.inbound-issues-watcher": "inbound_issues_watcher",
}


def is_label_feature_gated_off(
    label: str, install_json: Path | None = None,
) -> bool:
    """Return True iff ``label`` is a feature-gated plist whose feature is off.

    A "feature-gated plist" is one whose install is conditional on
    :func:`install_profile.is_feature_enabled` (see
    :func:`_maybe_install_launchd_upstream_issues_watcher` and its inbound
    twin). Such plists are intentionally absent from
    ``/Library/LaunchDaemons/`` when the feature is off; the health check
    uses this helper to avoid flagging that intentional absence as a missing
    plist.

    Returns False for unknown labels (no gating applies) and for
    gated-feature-on cases (the plist SHOULD be present; absence is a real
    failure). When the ``install_profile`` import fails we treat the gate
    as off — mirrors ``_maybe_install_launchd_upstream_issues_watcher``'s
    import-failure branch, so we don't FAIL on what we can't classify.
    """
    feature = FEATURE_GATED_PLIST_LABELS.get(label)
    if feature is None:
        return False
    try:
        from install_profile import is_feature_enabled  # type: ignore
    except Exception:
        return True
    path = install_json or (_CANONICAL_SHARED_DIR / "install.json")
    return not is_feature_enabled(feature, path)


# The orphan-sweeper (``find_orphaned_plists`` / ``remove_orphaned_plists``)
# lives in ``orphan_sweep.py`` — extracted there so the Linux systemd path could
# be added without growing this (size-capped) module. Re-exported here so the
# historical import sites (cli.py upgrade, web/server.py maintenance, tests) keep
# working unchanged. ``orphan_sweep`` imports this module lazily to avoid a cycle.
from .orphan_sweep import (  # noqa: E402,F401
    find_orphaned_plists,
    remove_orphaned_plists,
)


def classify_sync(
    deployed_version: str | None,
    deployed_sha: str | None,
    deployed_count: int | None,
    current_sha: str,
    current_count: int | None,
) -> tuple[bool, str]:
    """Decide ``(synced, relation)`` for a deployed stamp vs the running code by
    commit identity (thin bind of :func:`bot_version_sync.classify_sync` to
    EVOLVE_VERSION for the legacy no-sha fallback). ``relation`` ∈ never /
    synced / behind / ahead / unknown — ordered by commit_count, never by PR#,
    so a bot genuinely behind can never be told "current is a LOWER number"."""
    return _vsync.classify_sync(
        deployed_version, deployed_sha, deployed_count,
        current_sha, current_count, EVOLVE_VERSION)


def get_bot_sync_status(
    network: dict, install_info: dict | None
) -> dict[str, dict]:
    """Per-bot sync state (deployed vs current), decided by commit identity —
    see :func:`bot_version_sync.build_sync_status`. ``synced`` / ``relation``
    derive from sha/commit_count, so they cannot invert when a later-merged PR
    carries a lower number; a bot with no entry is never-deployed."""
    return _vsync.build_sync_status(
        network.get("members", []),
        (install_info or {}).get("bot_versions", {}),
        EVOLVE_VERSION, EVOLVE_COMMIT_SHA, EVOLVE_COMMIT_COUNT)


# ── Full-deploy helpers ────────────────────────────────────────────────────────

def run_cmd(
    args: list[str],
    cwd: str = _PROFILE.scratch_dir,  # /Users/Shared (macOS) / /tmp (Linux) — both 1777
    env_extra: dict[str, str] | None = None,
    timeout: int = 300,
) -> str:
    """Run a command; raise RuntimeError on non-zero exit. Returns stdout."""
    env = os.environ.copy()
    env["MallocStackLogging"] = "0"  # suppress macOS malloc-logging noise on Python spawn
    if env_extra:
        env.update(env_extra)
    cmd_str = " ".join(str(a) for a in args)
    _log.debug("run_cmd: %s", cmd_str)
    proc = subprocess.run(
        args, cwd=cwd, env=env,
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        _log.error("Command failed (rc=%d): %s\n%s", proc.returncode, cmd_str, proc.stderr.strip())
        raise RuntimeError(
            f"Command failed: {cmd_str}\n{proc.stderr.strip()}"
        )
    return proc.stdout


def ensure_analyzer_installed() -> str | None:
    """Ensure evolve-analyzer is pip-installed (editable) in the shared venv.

    The analyzer historically reached the daemons via ``PYTHONPATH`` in
    their launchd plists and reached admin code via per-call-site
    ``sys.path`` inserts. Phase 6.1 packaged it properly; admin code now
    does plain ``import audit`` / ``from signals import ...``, which
    requires the package to be installed in the venv.

    Compat-mode editable install (``--config-settings editable_mode=compat``)
    writes a plain .pth entry pointing at the repo checkout — REQUIRED, not
    cosmetic: the default PEP 660 editable install freezes the module list
    at install time, so a module added later by ``git pull`` (the normal
    deploy mechanism) would not import until reinstall. ``--no-deps``
    because the venv already carries the runtime deps via evolve-admin and
    deploy boxes shouldn't need network access for this step.

    Idempotent and fast when already installed (one metadata probe).
    Requires root (the venv is root:wheel) — callers are the deploy
    pipeline and install-infra-jobs, both already root-gated.

    Returns an error string on failure, None on success/already-installed.
    """
    probe = subprocess.run(
        [VENV_PYTHON, "-c",
         "import importlib.metadata as m; m.distribution('evolve-analyzer')"],
        capture_output=True, text=True, timeout=30,
    )
    if probe.returncode == 0:
        return None

    cmd = [
        VENV_PYTHON, "-m", "pip", "install", "--quiet", "--no-deps",
        "--config-settings", "editable_mode=compat", "-e", str(ANALYZER_DIR),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 and "--config-settings" in (r.stderr or ""):
        # Ancient pip without --config-settings: plain -e on old
        # pip/setuptools writes easy-install.pth — same compat semantics.
        r = subprocess.run(
            [VENV_PYTHON, "-m", "pip", "install", "--quiet", "--no-deps",
             "-e", str(ANALYZER_DIR)],
            capture_output=True, text=True, timeout=300,
        )
    if r.returncode != 0:
        return (r.stderr or r.stdout or "pip install failed").strip()[-500:]
    return None


def reinstall_evolve_admin() -> None:
    """Step 1: Ensure the venv exists and its package set is complete.

    On a fresh Linux pod the canonical venv must be BUILT first (W7, §8.2
    venv contract); on macOS / already-set-up pods that is a no-op.
    evolve-admin stays an editable install (-e) so repo changes reflect
    without reinstalling. evolve-analyzer needs a one-time compat-editable
    install (Phase 6.1) — ensure it here so existing pods self-migrate on
    their next deploy run."""
    from .installer import ensure_evolve_venv
    ensure_evolve_venv()
    err = ensure_analyzer_installed()
    if err:
        raise RuntimeError(f"evolve-analyzer install failed: {err}")


def _node_install_hint() -> str:
    """Platform Node.js install hint — brew (macOS) / NodeSource apt (Linux; setup_24.x, matching the wizard)."""
    if _PROFILE.name == "macos":
        return "  brew install node    (or: brew upgrade node)"
    return "  curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash - && sudo apt-get install -y nodejs"


def _check_node_version(path_env: str) -> None:
    """Raise a clear error if Node.js is missing or too old for the TypeScript build."""
    result = subprocess.run(
        ["env", f"PATH={path_env}", "node", "--version"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"Node.js not found. Install Node.js 20 LTS or later:\n{_node_install_hint()}")
    version_str = result.stdout.strip().lstrip("v")  # e.g. "12.16.1"
    try:
        node_major = int(version_str.split(".")[0])
    except ValueError:
        return  # can't parse — let tsc fail naturally
    if node_major < 14:
        raise RuntimeError(
            f"Node.js {version_str} is too old — TypeScript 5.x requires Node 14 or later "
            f"(Node 20 LTS recommended).\nUpgrade with:\n{_node_install_hint()}"
        )


def build_plugin() -> None:
    """Step 2: Compile TypeScript plugin.

    Build sequence:
    1. Run npm install + tsc as current user (admin user who ran sudo)
    2. Chown dist/ to root:wheel (OC security scanner requires this)
    """
    path_env = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")
    dist_dir = PLUGIN_SRC_DIR / "dist"

    # If PLUGIN_SRC_DIR is not writable (e.g. evolve service user on a machine
    # where the repo was cloned by a different user), temporarily take ownership
    # so npm install can write package-lock.json and node_modules/.
    import pwd as _pwd
    _plugin_orig_owner: str | None = None
    if not os.access(str(PLUGIN_SRC_DIR), os.W_OK):
        try:
            _plugin_orig_owner = _pwd.getpwuid(PLUGIN_SRC_DIR.stat().st_uid).pw_name
        except Exception:
            pass
        subprocess.run(
            ["sudo", _PROFILE.chown, "-R", "evolve:staff", str(PLUGIN_SRC_DIR)],
            capture_output=True, timeout=30,
        )

    try:
        # Step 0: verify Node.js is present and new enough to run tsc
        _check_node_version(path_env)

        # Step 0b: ensure dist/ is writable.
        #
        # Earlier versions of the install step (or the OC security scan in the
        # finally-block below) can leave dist/ files owned by root. When the
        # next build runs as the admin user, tsc emits
        #   error TS5033: Could not write file ... EACCES: permission denied
        # for every artifact and the deploy appears as "tsc failed" with no
        # useful error. Blow it away up front — tsc will regenerate everything
        # owned by the current user. Cheap: the compile itself is <1s on a
        # warm npm install, so removing dist/ isn't a meaningful cost.
        if dist_dir.exists():
            if not os.access(str(dist_dir), os.W_OK):
                # Root-owned from a prior install cycle. The evolve sudoers
                # grant covers `chown -R * /Users/Shared/evolve-repo/packages/plugin`
                # but NOT `rm -rf` on the same path, so take ownership first and
                # then remove with plain shutil. Raise on chown failure instead
                # of swallowing it — silent failure here was the 2026-05-05
                # mode where tsc emitted EACCES for every output and the real
                # cause (sudoers gap) was invisible.
                #
                # Capture the original PLUGIN_SRC_DIR owner if the outer block
                # didn't already, so the finally-block can restore it. Without
                # this, a writable parent + root-owned dist/ leaves the whole
                # tree evolve-owned after the build.
                if _plugin_orig_owner is None:
                    try:
                        _plugin_orig_owner = _pwd.getpwuid(
                            PLUGIN_SRC_DIR.stat().st_uid
                        ).pw_name
                    except Exception:
                        pass
                chown = subprocess.run(
                    ["sudo", _PROFILE.chown, "-R", "evolve:staff",
                     str(PLUGIN_SRC_DIR)],
                    capture_output=True, text=True, timeout=30,
                )
                if chown.returncode != 0:
                    detail = (chown.stderr or chown.stdout or "").strip()
                    raise RuntimeError(
                        f"could not take ownership of {PLUGIN_SRC_DIR} to "
                        f"clear stale dist/: {detail or chown.returncode}"
                    )
            shutil.rmtree(str(dist_dir), ignore_errors=True)

        # Step 1a: deps when node_modules is absent. `npm ci` (NOT `npm install`)
        # installs from package-lock.json without rewriting it — install's in-place
        # lockfile churn (+1 line) dirties the Linux nested checkout → ff-only wedge.
        node_modules = PLUGIN_SRC_DIR / "node_modules"
        if not node_modules.exists():
            result = subprocess.run(
                ["env", f"PATH={path_env}", "npm", "ci", "--quiet",
                 "--workspaces=false"],
                cwd=str(PLUGIN_SRC_DIR), capture_output=True, text=True
            )
            if result.returncode != 0:
                # npm also sends most real output to stdout; include both.
                detail = (result.stdout or "").strip()
                if result.stderr:
                    detail = detail + "\n" + result.stderr.strip() if detail else result.stderr.strip()
                if not detail:
                    detail = f"(no output captured; exit code {result.returncode})"
                raise RuntimeError(f"npm ci failed:\n{detail}")

        # Step 1b: compile TypeScript via the plugin's LOCAL compiler. Use `npm run build`
        # (package.json "build": "tsc"; npm run binds packages/plugin/node_modules/.bin/tsc), NOT `npx --yes tsc`:
        # `--yes` forces install-from-registry, and on a fresh Linux pod (Ubuntu 24.04 / npm 11) npx ignored the local binary and fetched the squatted DEPRECATED `tsc@2.0.4` ("This is not the tsc command you are looking for"), exiting non-zero and wedging `setup --fresh` at Step 14 (the mini's warm npx cache masked it; DO pathfinder 2026-06-17). Do NOT revert to npx. release_manager.py's canary gate runs a SEPARATE type-check, hardened to `npm exec --no -- tsc --noEmit` (registry fetch forbidden) for the same reason.
        result = subprocess.run(
            ["env", f"PATH={path_env}", "npm", "run", "build"],
            cwd=str(PLUGIN_SRC_DIR), capture_output=True, text=True
        )
        if result.returncode != 0:
            # `npm run build` runs tsc (diagnostics on stdout) plus npm's own stderr tail — include both; a silent "tsc failed:" with nothing after was the 2026-04-21 failure mode this combine fixes.
            detail = (result.stdout or "").strip()
            if result.stderr:
                detail = detail + "\n" + result.stderr.strip() if detail else result.stderr.strip()
            if not detail:
                detail = f"(no output captured; exit code {result.returncode})"
            raise RuntimeError(f"tsc failed:\n{detail}")
    finally:
        # Step 2: sync build artifacts to PLUGIN_INSTALL_DIR (root-owned, separate from git).
        # openclaw's security check requires the plugin root to be owned by the bot user
        # or root. By installing to a path outside the git working tree, we avoid the
        # ownership conflict where git needs admin-owned files but openclaw needs root.
        #
        # node_modules/ MUST be staged alongside dist/ — without it the gateway fails
        # to load the plugin entirely the moment any source file imports a non-bundled
        # npm package. PR #734's DeferTool added `import { Type } from "@sinclair/typebox"`
        # and a stale install (no node_modules in evolve-plugin/) silently broke the
        # plugin on every gateway: substrate_health watchdog fires plugin-missing on
        # every bot, Integrations & Keys page shows "Evolve Plugin: Error". Until then
        # bots happened to load from /Users/Shared/evolve-repo/packages/plugin (which
        # has node_modules/ from the build's own npm install) — but OC 2026.4.29's
        # ownership check started rejecting that path. node_modules/ here costs ~31MB
        # of disk per deploy (measured on the mini 2026-08-11; the "~100MB" this
        # comment used to claim was never re-measured); cheap insurance against a
        # class of regressions where a new dependency lands without its install path
        # also landing. It is covered by the install-tree digest — plugin_signature's
        # treeDigest, spec §4.1 — at ~60ms per hash, so adding to it is not free but
        # is far below noise on a deploy.
        node_modules = PLUGIN_SRC_DIR / "node_modules"
        if dist_dir.exists():
            subprocess.run(["sudo", "/bin/mkdir", "-p", str(PLUGIN_INSTALL_DIR)],
                           capture_output=True)
            subprocess.run(["sudo", "/bin/rm", "-rf", str(PLUGIN_INSTALL_DIR / "dist")],
                           capture_output=True)
            subprocess.run(["sudo", "/bin/cp", "-R", str(dist_dir),
                            str(PLUGIN_INSTALL_DIR / "dist")],
                           capture_output=True)
            if node_modules.exists():
                subprocess.run(["sudo", "/bin/rm", "-rf",
                                str(PLUGIN_INSTALL_DIR / "node_modules")],
                               capture_output=True)
                subprocess.run(["sudo", "/bin/cp", "-R", str(node_modules),
                                str(PLUGIN_INSTALL_DIR / "node_modules")],
                               capture_output=True)
            # INSTALL_TREE_FILES is the digest's own list of covered top-level
            # files — driving the copy from it keeps "what we stage" and "what
            # we hash" from drifting apart. The manifest is copied too but is
            # deliberately outside the digest: it carries the stamp.
            for fname in (*INSTALL_TREE_FILES, "openclaw.plugin.json"):
                src = PLUGIN_SRC_DIR / fname
                if src.exists():
                    subprocess.run(["sudo", "/bin/cp", str(src),
                                    str(PLUGIN_INSTALL_DIR / fname)],
                                   capture_output=True)

            subprocess.run(["sudo", _PROFILE.chown, "-R", f"root:{_PROFILE.admin_group}",
                            str(PLUGIN_INSTALL_DIR)],
                           capture_output=True)
            subprocess.run(["sudo", "/bin/chmod", "-R", "755",
                            str(PLUGIN_INSTALL_DIR)],
                           capture_output=True)

            # Stamp the deployed manifest with the canonical digests of the
            # staged tree (dist/ + node_modules/ + the JSON files + the
            # manifest's own content). The committed manifest stays clean —
            # only the deployed copy at PLUGIN_INSTALL_DIR is stamped. This is
            # the anchor for verify_plugin_signature() at install time; without
            # it, the install would trust the path /Users/Shared/evolve-plugin/
            # rather than its content. Logic lives in plugin_signature.py;
            # see docs/spec-plugin-install-trust-2026-06-06.md §4.
            #
            # MUST run AFTER the chown/chmod above. `cp -R` preserves source
            # modes, so a dependency shipping a 0700 dir would be unreadable to
            # the `evolve` user build_plugin runs as; rglob swallows the
            # PermissionError, so the stamp would omit that subtree while
            # verification (always post-chmod) includes it — divergent digests
            # on every bot, reproduced by each rebuild. Latent today (0 of 1565
            # files under the mini's node_modules is non-o+r); see spec §4.1.
            stamp_install_tree(PLUGIN_INSTALL_DIR, repo_root=_REPO_ROOT)

            # Step 2b: verify the install dist matches the tsc output before
            # git checkout wipes dist_dir back to HEAD. The verify must run
            # here — after the sync but before checkout — because after checkout
            # dist_dir contains the git-committed JS, not the freshly compiled
            # JS, and the two can diverge (tsc non-determinism, minor version
            # differences). Checking src vs install here confirms the cp -R
            # succeeded; checking after checkout would be comparing HEAD vs
            # compiled output and would false-positive on any non-determinism.
            from .deploy_verify import verify_plugin_install_matches_source
            _verify_result = verify_plugin_install_matches_source(
                src_dist_dir=dist_dir,
                install_dist_dir=PLUGIN_INSTALL_DIR / "dist",
            )
            if not _verify_result.ok:
                raise RuntimeError(
                    f"Plugin install verification failed after build: "
                    f"{_verify_result.summary}\n{_verify_result.detail}"
                )

        # Step 3: restore the git working tree's dist/ so it stays clean.
        # tsc rewrites the compiled .js files in place, which makes the repo
        # dirty and causes `git pull` to fail.  We already copied the artifacts
        # to PLUGIN_INSTALL_DIR above, so the working-tree copy can safely be
        # reset back to HEAD.  This keeps `git pull` working without stash.
        subprocess.run(
            ["git", "checkout", "--", str(dist_dir)],
            cwd=str(_REPO_ROOT),
            capture_output=True,
        )

        # Step 3b: restore dist/ ownership, mode bits, and strip ACLs.
        # When `sudo evolve-admin deploy` runs as root, both tsc and the
        # `git checkout` above produce dist/ files owned by root:wheel
        # at mode 644 inside 755 dirs. Chowning back to the repo owner
        # is necessary but NOT sufficient — the next `git pull` from
        # any other staff-group user (e.g. the human admin user, in the
        # same `staff` group as evolve) still hits
        # "unable to unlink old '...': Permission denied" because mode
        # 755 dirs have no group write. The chmod g+rwX below closes
        # that gap; pairs with PR #767's repo-wide normalization for
        # the rest of the working tree.
        if dist_dir.exists():
            try:
                repo_st = _REPO_ROOT.stat()
                repo_owner = pwd.getpwuid(repo_st.st_uid).pw_name
                repo_group = grp.getgrgid(repo_st.st_gid).gr_name
            except Exception:
                repo_owner = repo_group = None
            if repo_owner and repo_group:
                subprocess.run(
                    ["sudo", _PROFILE.chown, "-R",
                     f"{repo_owner}:{repo_group}", str(dist_dir)],
                    capture_output=True, timeout=30,
                )
                # Strip ACLs via the perms seam (macOS `chmod -R -N`, byte-identical; Linux `setfacl -R -b/-k`). Best-effort.
                get_perms().clear_acl(dist_dir, recursive=True)
                # Make group-writable so any staff member can later
                # `git pull` past these files. `g+rwX` (capital X) only
                # adds execute on dirs / already-exec files — won't make
                # plain artifacts spuriously executable.
                subprocess.run(
                    ["sudo", "/bin/chmod", "-R", "g+rwX", str(dist_dir)],
                    capture_output=True, timeout=30,
                )

        # Step 4: restore plugin dir ownership if we temporarily claimed it.
        if _plugin_orig_owner:
            subprocess.run(
                ["sudo", _PROFILE.chown, "-R", f"{_plugin_orig_owner}:staff",
                 str(PLUGIN_SRC_DIR)],
                capture_output=True, timeout=30,
            )


def fix_plugin_permissions() -> None:
    """Step 3: Ensure PLUGIN_INSTALL_DIR is root-owned (root:<admin_group>) 755.

    build_plugin() already does this as part of the sync step; this function
    exists as a safety re-run for callers that invoke it standalone.
    The git working tree (PLUGIN_SRC_DIR) is intentionally left admin-owned
    so that git pull works without sudo.
    """
    if PLUGIN_INSTALL_DIR.exists():
        subprocess.run(
            ["sudo", _PROFILE.chown, "-R", f"root:{_PROFILE.admin_group}", str(PLUGIN_INSTALL_DIR)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["sudo", "/bin/chmod", "-R", "755", str(PLUGIN_INSTALL_DIR)],
            check=True, capture_output=True,
        )
        # On Linux a recursive chmod sets the ACL mask from the group bits
        # on any ACL'd file in the tree, silently capping named ACEs —
        # re-widen it. No-op on macOS and on paths without extended ACLs.
        get_perms().reassert_mask(PLUGIN_INSTALL_DIR, recursive=True)


# The evolve-writable workspace-subdir contract (workspace/evolve/,
# workspace/manifests/, workspace/evolve-backup/): full create/edit/delete
# with inheritance onto new children. One constant, three grant sites —
# the strings were identical copies before the W4a perms-seam reroute.
EVOLVE_WS_WRITE_ACL_PERMS = (
    "list,search,add_file,add_subdirectory,"
    "readattr,writeattr,readextattr,writeextattr,"
    "readsecurity,delete,write,file_inherit,directory_inherit"
)

# workspace/ root: files created directly in it (AGENTS.md etc.) inherit
# the write grant, but no directory_inherit — the cascade must not reach
# into subdirectories (they have their own contracts).
WORKSPACE_ROOT_WRITE_ACL_PERMS = (
    "list,search,add_file,"
    "readattr,writeattr,readextattr,writeextattr,"
    "readsecurity,delete,write,file_inherit"
)

# Per-file backfill shape for pre-existing files directly in workspace/.
WORKSPACE_FILE_WRITE_ACL_PERMS = (
    "read,write,readattr,writeattr,readextattr,writeextattr,"
    "readsecurity,delete,append"
)

# Single-leaf-file read grant (.zshrc tamper-detection hashing) — no
# inherit flags because it's not a directory.
FILE_READ_ACL_PERMS = "read,readattr,readextattr,readsecurity"


def _ensure_evolve_write_dir(rel: str, bot_user: str, perms, *,
                             recursive_chown: bool = False) -> None:
    """mkdir a bot ``.openclaw`` subdir (if absent) + grant the evolve write ACL.

    Shared by the workspace/evolve + workspace/manifests contracts. Creating the
    dir BEFORE granting is load-bearing on Linux (W10-G #1): the recursive read
    grant plants an inheritable rX default ACL on ``.openclaw``, so a subdir created
    later inherits read-ONLY and evolve daemons hit EACCES on write. ``recursive_chown``
    matches workspace/evolve's chown -R grant.
    """
    d = str(_user_home(bot_user) / ".openclaw" / rel)
    # exists_or_unreachable: .exists() RAISES under a 0700 clamp (Py3.12); unreachable
    # reads as present → skip the mkdir, still attempt the grant (root setfacl re-heals).
    if not _secret_perms.exists_or_unreachable(Path(d)) and not redirect_refusal(d):  # D-2
        subprocess.run(["sudo", "/bin/mkdir", "-p", d],
                       check=False, capture_output=True, timeout=10)
        chown = (["sudo", _PROFILE.chown] + (["-R"] if recursive_chown else [])
                 + [f"{bot_user}:staff", d])
        subprocess.run(chown, check=False, capture_output=True, timeout=10)
    if _secret_perms.exists_or_unreachable(Path(d)):
        # share_group_other_read: workspace channel — bot reads evolve-written files it doesn't own.
        perms.grant_write_recursive(Path(d), EVOLVE_SERVICE_USER, EVOLVE_WS_WRITE_ACL_PERMS, share_group_other_read=True)


def _apply_openclaw_read_contract(bot_user: str, user: str, perms) -> bool:
    """Single source of truth for the bot-private ``.openclaw`` read contract, shared
    by ``set_evolve_read_acl`` (full deploy) and ``_add_acl`` (the drift repair) —
    closing the #3198 gap where ``_add_acl`` re-ran only the read grant UNCLAMPED and
    re-armed world-readable minting. THREE coupled, order-dependent pieces (each
    annotated inline): the group/other clamp (#3190 keystone), the credentials/ +
    profiles carve-out, then the workspace re-widen (which MUST follow the clamp).
    Idempotent + order-independent across per-user repairs."""
    oc_dir = Path(_user_home(bot_user) / ".openclaw")

    # (1) bot-private clamp (group::/other:: → ---); keystone for #3190. macOS no-op.
    ok = perms.grant_read_recursive(oc_dir, user, restrict_group_other=True)

    # (2) credentials/ + profiles/*.md carve-out — best-effort (.exists()/.glob()
    # RAISE under a 0700 clamp on Py3.12 if the mask wasn't healed; skip+log).
    try:
        creds_dir = oc_dir / "credentials"
        if creds_dir.exists() and not redirect_refusal(creds_dir):  # D-2: .exists() FOLLOWS
            perms.clear_acl(creds_dir)
            subprocess.run(["sudo", "/bin/chmod", "700", str(creds_dir)],
                           check=False, capture_output=True, timeout=10)
        profiles_dir = oc_dir / "profiles"
        if profiles_dir.exists():
            for md_file in profiles_dir.glob("*.md"):
                perms.clear_acl(md_file)
        _secret_perms.strip_bot_private_acl(oc_dir)  # #3452 file-shaped carve-out
    except OSError as e:
        _log.warning("_apply_openclaw_read_contract: creds/profiles carve-out skipped for %s (EACCES clamp?): %s", bot_user, e)

    # (3) workspace shared-channel re-widen — MUST run after the clamp. Create the
    # dir before granting (W10-G #1) so a later root/bot-created dir can't inherit a
    # read-only ACL and block evolve writes on Linux.
    _ensure_evolve_write_dir("workspace/evolve", bot_user, perms, recursive_chown=True)
    _ensure_evolve_write_dir("workspace/manifests", bot_user, perms)
    evolve_backup = oc_dir / "workspace/evolve-backup"
    if _secret_perms.exists_or_unreachable(evolve_backup):
        perms.grant_write_recursive(evolve_backup, EVOLVE_SERVICE_USER, EVOLVE_WS_WRITE_ACL_PERMS, share_group_other_read=True)

    return ok


def set_evolve_read_acl(bot_id: str) -> None:
    """Grant evolve ACL read access to a bot's .openclaw/ (current + future files,
    no per-path sudoers) + read/write on workspace/evolve/. All ops go through the
    Perms seam (runtime.perms, W4a): macOS = chmod +a/-N byte-for-byte, Linux =
    setfacl access + default ACLs. Idempotent (the round-8 self-heal: a re-run
    recomputes every clamped child mask + re-plants the default ACL). The clamp +
    carve-out + workspace re-widen live in ``_apply_openclaw_read_contract`` — the
    SAME helper the drift repair (``_add_acl``) routes through, so the self-heal can
    never re-widen what the deploy clamped (#3198). See Perms seam + CLAUDE.md.
    """
    bot_user = _bot_user_for(bot_id)
    bot_home = _user_home(bot_user)
    oc_dir = str(bot_home / ".openclaw")
    if not _secret_perms.exists_or_unreachable(Path(oc_dir)):  # EACCES (clamped) → proceed; the grants below recompute the mask
        return

    perms = get_perms()

    # W10-F #1: traverse (--x) on the bot HOME so evolve can reach .openclaw.
    # macOS no-op (/Users/<acct> 0755); on Linux `useradd -m` makes /home/<bot>
    # 0750 — without this the rX ACL below is unreachable (14 daemons died EACCES).
    perms.grant_traverse(bot_home, EVOLVE_SERVICE_USER)

    # The bot-private .openclaw read contract (clamp + carve-out + ws re-widen).
    _apply_openclaw_read_contract(bot_user, EVOLVE_SERVICE_USER, perms)

    # Grant read on .claude/projects/ for the Auto-Memory inventory (Tier 2.4):
    # listing per-project auto-memory dirs needs read at every level. Idempotent.
    claude_projects_dir = str(_user_home(bot_user) / ".claude/projects")
    if _secret_perms.exists_or_unreachable(Path(claude_projects_dir)):
        perms.grant_read_recursive(Path(claude_projects_dir), EVOLVE_SERVICE_USER)

    # Grant evolve read on the bot's ``.zshrc`` (``audit.audit_shell_config`` hashes
    # it for tamper detection; without the ACL the bot trips ``.zshrc unreadable``
    # forever). evolve-only, single leaf file → no inherit flags.
    zshrc_path = str(_user_home(bot_user) / ".zshrc")
    if _secret_perms.exists_or_unreachable(Path(zshrc_path)):
        perms.grant(Path(zshrc_path), EVOLVE_SERVICE_USER, FILE_READ_ACL_PERMS)

    # Grant write on workspace/ root (files directly in workspace/, e.g. AGENTS.md);
    # file_inherit (not directory_inherit) so it does not cascade into subdirs.
    workspace_root = str(_user_home(bot_user) / ".openclaw/workspace")
    if _secret_perms.exists_or_unreachable(Path(workspace_root)):
        perms.grant(Path(workspace_root), EVOLVE_SERVICE_USER, WORKSPACE_ROOT_WRITE_ACL_PERMS)
        try:  # retro-grant existing workspace/ files (AGENTS.md); iterdir RAISES under a clamp
            for f in Path(workspace_root).iterdir():
                if f.is_file():
                    perms.grant(f, EVOLVE_SERVICE_USER, WORKSPACE_FILE_WRITE_ACL_PERMS)
        except OSError as e:
            _log.warning("set_evolve_read_acl: workspace/ retro-grant skipped for %s: %s", bot_user, e)
    # Grants are best-effort (check=False); verify the contract + log loud on a
    # clamped mask (W10-G r7/r8).
    _secret_perms.verify_evolve_access(bot_user, perms)
    _dres.plant_never_index_marker(Path(oc_dir), via_sudo=True)  # Part A: macOS Spotlight skip (bot-owned → sudo)


def ensure_workspace_git_init(bot_id: str, network: dict | None = None) -> tuple[bool, str]:
    """Ensure ``/Users/<bot_user>/.openclaw/workspace/`` is a git repo.

    Idempotent. Returns ``(ok, status)`` where status is one of:
      - ``"already-initialized"`` — `.git` directory already present
      - ``"no-workspace"`` — workspace dir does not exist (bot not deployed yet)
      - ``"initialized"`` — `.git` newly created
      - ``"failed:<reason>"`` — error during init

    Works from both root context (CLI deploy) and the admin server (evolve user)
    via /tmp staging — git init runs in a temp dir as the current user, then
    `.git` is transplanted into the bot's workspace via sudo cp + sudo chown
    (both already in evolve's sudoers grants).
    """
    bot_user = _bot_user_for(bot_id, network)
    workspace = _user_home(bot_user) / ".openclaw/workspace"
    if not workspace.exists():
        return (False, "no-workspace")
    dest = workspace / ".git"
    if dest.exists():
        return (True, "already-initialized")
    try:
        with tempfile.TemporaryDirectory(prefix=f"evolve-git-init-{bot_id}-", dir="/tmp") as tmp:
            staging = Path(tmp) / "ws"
            staging.mkdir()
            r = subprocess.run(
                ["/usr/bin/git", "init", "-b", "main", str(staging)],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                return (False, f"failed:git-init:{r.stderr.strip()[:200]}")
            if why := redirect_refusal(dest):  # D-2: dir-shaped dest (walk covers workspace/)
                return (False, f"failed:unsafe-dest:{why}")
            r = subprocess.run(
                ["sudo", "/bin/cp", "-R", str(staging / ".git"), str(dest)],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                return (False, f"failed:cp:{r.stderr.strip()[:200]}")
            if why := redirect_refusal(dest):  # re-assert: cp leaves a LINK for the chown
                return (False, f"failed:unsafe-dest:{why}")
            r = subprocess.run(
                ["sudo", _PROFILE.chown, "-R", f"{bot_user}:staff", str(dest)],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                return (False, f"failed:chown:{r.stderr.strip()[:200]}")
    except Exception as e:
        return (False, f"failed:exception:{str(e)[:200]}")

    return (True, "initialized")


def _detect_provider_model(
    bot_id: str,
    bot_user: str | None = None,
    role: str = "member",
    cfg: dict | None = None,
) -> str | None:
    """Resolve the floor primary model for (bot, role).

    Resolution order — **tier config first, hardcoded RECOMMENDED last**:

      1. **Bot's own tier config** (``cfg.tiers.tier3.models[0]`` for
         member, ``cfg.tiers.tier2.models[0]`` for primary, falling
         through to the next tier if empty). This is the authoritative
         source — when the operator (or the AI Optimization UI) has
         defined tiers, the seed primary MUST come from those tiers,
         not from a hardcoded RECOMMENDED entry in admin code. Reasons:

           - Updates to the tier-model preference (new Haiku release,
             switch to a different provider's flagship, etc.) are made
             in one place (the bot's tier config), not in two parallel
             surfaces.
           - Tier definitions reflect the operator's actual preferences
             (which models they trust, which providers they have keys
             for), not admin code's guess at "the default for this
             provider."
           - The cost-leak architecture (primary as floor) is preserved
             without needing admin code to track which model is the
             current Anthropic floor.

      2. **RECOMMENDED registry** — only when the bot has no tier
         config (true new bot, no AI Optimization yet). This is the
         seed for first-time setup; once the operator visits AI
         Optimization or the floor advisor proposes a tier config, the
         tier config takes over and admin code's hardcoded fallback
         stops mattering.

    ``role`` drives which tier is the floor:
      - member (default): tier3 → tier2 → tier1
      - primary: tier2 → tier1

    Returns ``None`` when neither a tier config nor a detectable
    provider is available, so deploy leaves the field empty rather
    than fabricating a model string.

    Updated 2026-05-28 after operator pushback on the original "floor
    by hardcoded model name" design: defaults must be tier-resolved,
    not model-hardcoded, so the source of truth lives in one place.
    """
    # Step 1 — try the bot's existing tier config first.
    if isinstance(cfg, dict):
        from_tier = _resolve_primary_from_tier_config(cfg, role)
        if from_tier:
            return from_tier

    # Step 2 — fall back to RECOMMENDED only when no tier config exists.
    bot_user = bot_user or _bot_user_for(bot_id)
    auth_path = _user_home(bot_user) / ".openclaw/agents/main/agent/auth-profiles.json"
    try:
        try:
            text = auth_path.read_text()
        except PermissionError:
            r = subprocess.run(["sudo", "/bin/cat", str(auth_path)], capture_output=True, text=True)
            text = r.stdout if r.returncode == 0 else ""
        if not text.strip():
            return None
        auth = json.loads(text)
        profiles = auth.get("profiles", {})
        providers = {p.get("provider") for p in profiles.values() if p.get("provider")}
        # Provider preference order matches _pick_provider in provisioning.py
        # (anthropic > openai > google > xai) so deploy and seed agree on
        # which provider gets seeded.
        for prov in ("anthropic", "openai", "google", "xai"):
            if prov in providers:
                return _floor_model_for_role_from_registry(prov, role)
    except Exception:
        pass
    return None


# ── Tier-resolution helpers ────────────────────────────────────────────────

# Tier walk order — workhorse-first for ALL bots. Background work
# routes to tier3 via the trigger anchor (PR #1737 / #1764) + the
# bot's `routing.backgroundTier` config, independent of what `primary`
# is set to. So `primary = tier2` (workhorse) is correct for all
# bots — it's the default for *user-facing turns*, which is the
# right destination for human chat on every bot type.
#
# Historical note (PRs #1735 / #1736 / #1765, reverted 2026-05-29):
#   Earlier these walks differentiated by role — member bots walked
#   ["tier3", "tier2", "tier1"] (floor-first) so primary derived to
#   tier3. That was wrong on two counts:
#     1. Background work was already routing to tier3 via the trigger
#        anchor — flipping `primary` to tier3 achieved no additional
#        cost reduction on background turns.
#     2. Human chat on member bots silently got tier3 (Haiku) replies
#        with no in-channel escalation path. Slack/Telegram/Discord
#        users can't reach the chip surface (admin-UI-only).
#   The per-bot default-tier picker (auto/fast/standard/power) is the
#   correct path for operator/user-driven defaults — coming as a
#   follow-up. Until then, the workhorse-first walk applies universally.
_DEFAULT_TIER_WALK: tuple[str, ...] = ("tier2", "tier3", "tier1")


def _resolve_primary_from_tier_config(cfg: dict, role: str) -> str | None:
    """Walk the bot's openclaw.json tier config and return the first
    model in the workhorse-first walk.

    Looks under both possible tier paths because the tier config can
    live at either ``tiers`` (top-level, post-2026-05-25 layout) or
    ``agents.defaults.model.tiers`` (legacy). Returns None if neither
    has a usable tier.

    ``role`` is currently ignored — kept on signature so callers
    compile unchanged and the forthcoming default-tier picker can
    drop in a role/preference-aware walk here cleanly.
    """
    del role  # see docstring + module-level comment block
    candidates = [
        cfg.get("tiers"),
        (cfg.get("agents") or {})
            .get("defaults", {})
            .get("model", {})
            .get("tiers"),
    ]
    for tiers_obj in candidates:
        if not isinstance(tiers_obj, dict):
            continue
        for tier_id in _DEFAULT_TIER_WALK:
            tier_entry = tiers_obj.get(tier_id) or {}
            models = tier_entry.get("models") or []
            if models:
                first = str(models[0]).strip()
                if first:
                    return first
    return None


def _floor_model_for_role_from_registry(provider: str, role: str) -> str | None:
    """RECOMMENDED-registry fallback used only when the bot has no tier
    config (true new bot). Returns the workhorse-tier model (tier2)
    for all roles — see module-level comment block for the rationale
    on why role-aware dispatch was reverted.

    Once the bot has tier config — either operator-set or seeded by
    Seed Defaults — _resolve_primary_from_tier_config wins and this
    function is not consulted. So this is the safety net for first-
    time deploy of a bot that hasn't had Seed Defaults run yet.

    ``role`` is currently ignored — kept on signature for caller
    compat / future per-bot tier-picker drop-in.
    """
    del role  # see docstring + module-level comment block
    try:
        from model_registry import RECOMMENDED  # type: ignore
    except Exception:
        # Last-resort fallback when model_registry is unreachable.
        # Returning None lets the caller decide (typically falls through
        # to OC's own default-model handling). No hardcoded model
        # names here — see follow-up "no-hardcoded-models" cleanup.
        return None
    rec = RECOMMENDED.get(provider, {})
    for tier_id in _DEFAULT_TIER_WALK:
        entry = rec.get(tier_id)
        if entry and entry.get("model"):
            return entry["model"]
    return None


def _read_exec_approvals(bot_user: str) -> dict | None:
    """Read exec-approvals.json for bot_user.

    Returns {} when the file is absent (normal for a fresh bot with no
    approved commands yet), the parsed dict when present, or None on a
    read/parse error (caller treats None as "can't determine" and falls
    back to deny — the safe side).

    Uses subprocess (sudo /bin/cat) rather than Path.read_text so the
    read path is independent of the openclaw.json direct-read path and
    is consistently interceptable in tests via the existing fake_run
    pattern.  The evolve user's ACL grant covers exec-approvals.json,
    so this works without special sudoers additions.
    """
    path = _user_home(bot_user) / ".openclaw/exec-approvals.json"
    r = subprocess.run(
        ["sudo", "/bin/cat", str(path)],
        capture_output=True, text=True, cwd=_PROFILE.scratch_dir,
    )
    if r.returncode != 0:
        if "No such file" in (r.stderr or ""):
            return {}
        return None
    try:
        data = json.loads(r.stdout)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _infer_exec_policy(
    bot_cfg: dict,
    exec_approvals: dict | None,
    *,
    bot_id: str = "",
    bot_role: str = "",
) -> str:
    """Return the exec security mode that deploy should enforce for this bot.

    Priority:
    1. Explicit ``execPolicy`` in the bot's network.json entry
       (``"deny"`` | ``"allowlist"`` | ``"full"``). Honored unconditionally
       so the operator retains an escape hatch for genuine power-user cases.
    2. exec-approvals.json has at least one allowlist entry → ``"allowlist"``.
    3. exec-approvals.json ``defaults`` carries an actual
       ``allowlist``/``approvals``/``allow`` array → ``"allowlist"``.
    4. Default → ``"full"``. See "Why ``full`` as default" below.

    Phase E.4 (2026-05-25) removed the previous primary-bot carve-out that
    short-circuited evo (or any role=primary bot) to ``"deny"``. The
    carve-out was a defense-in-depth measure for the ``/approve``
    exfiltration leak documented in
    docs/diagnosis-evo-exec-approval-leak-2026-05-21.md. Phase E.2.b's
    account separation made that defense unnecessary: evo now runs as
    the unprivileged ``evo`` macOS user with no sudo, no cross-bot ACL,
    and no admin-daemon reach. A successful /approve exfil at worst
    runs shell as the ``evo`` user — same blast radius as a prompt
    injection on team_bot_a. See docs/spec-evo-account-separation-2026-05-25.md
    §"What we gain" for the full argument. ``bot_id`` and ``bot_role``
    parameters are kept for backward compatibility (other callers pass
    them) and to preserve the option to special-case in the future
    without a signature change.

    Why ``full`` as default for member bots (pivoted 2026-05-25):
    A member bot runs as its own macOS user account in its own workspace.
    The right default is "the bot can do anything in its own shell, like a
    human can in their own account" — not "treat the bot like a hostile
    process." See docs/spec-app-derived-permissions-2026-05-24.md §Principle.

    The prior ``"deny"`` default produced the Slack failure on 2026-05-24:
    INSTALLED_APPS.md announced capabilities the exec policy denied, team_bot_a's
    LLM dispatched a declared script, OC denied, team_bot_a composed an optimistic
    reply, the operator saw a contradiction (Slack post:
    ``run python3 ops/tools/unified_task_system.py (agent) failed``). The
    permissions reconciler (``evolve_admin.app_permissions.reconciler``) now
    computes a manifest-derived allowlist *preview* in ``full`` mode for
    future opt-in to ``allowlist`` mode; this function's job here is just
    to pick the right runtime enforcement mode.

    Note on Priority 3: the prior "any non-empty ``defaults`` → allowlist"
    branch false-positived on socket-protocol metadata. Evo's
    exec-approvals.json carries ``defaults: {"security": "full"}`` —
    that's the unix-socket auth posture for the exec-approvals daemon, NOT
    a per-agent approval list. Only an actual allowlist/approvals/allow
    array under ``defaults`` is real approvals content. See the diagnosis
    doc for the leak chain this narrowing closes.
    """
    # bot_id / bot_role kept on the signature so callers don't have to
    # update at carve-out removal time; they're unused in the current
    # logic but documented as future special-case hooks. silence ``unused``
    # statically.
    _ = (bot_id, bot_role)

    explicit = (bot_cfg.get("execPolicy") or "").lower().strip()
    if explicit in ("deny", "allowlist", "full"):
        return explicit

    if exec_approvals is None:
        return "full"

    agents = exec_approvals.get("agents") or {}
    if isinstance(agents, dict):
        for agent_block in agents.values():
            if not isinstance(agent_block, dict):
                continue
            al = (
                agent_block.get("allowlist")
                or agent_block.get("approvals")
                or agent_block.get("allow")
            )
            if isinstance(al, (list, dict)) and len(al) > 0:
                return "allowlist"

    # Only an actual ``allowlist``/``approvals``/``allow`` array in defaults
    # counts as evidence the bot needs allowlist mode. The socket-meta
    # ``security`` key describes the unix-socket auth posture for the
    # exec-approvals daemon, NOT a per-agent approval list — ignore it.
    defaults = exec_approvals.get("defaults")
    if isinstance(defaults, dict):
        for key in ("allowlist", "approvals", "allow"):
            v = defaults.get(key)
            if isinstance(v, (list, dict)) and len(v) > 0:
                return "allowlist"

    return "full"


# Mirror of the two permission-config fields _infer_exec_policy may
# diverge from baseline on. Kept in sync with
# ``analyzer/permissions/baseline.py::DEFAULT_BASELINE.pod_default.permission_config``
# so the deploy-time intent recorder can decide "is this value worth
# recording an intent for?" without importing the analyzer package
# eagerly. The full baseline is consulted lazily inside the recorder
# when present (it's authoritative); the constants below are the
# Phase 1 / 2026-05-25-pivot defaults the deploy code knows about
# independently — used as a fallback when the analyzer package isn't
# importable from this context (e.g. CLI not run from the deploy box).
_DEPLOY_BASELINE_EXEC_SECURITY = "full"
_DEPLOY_BASELINE_EXEC_ASK = "on-miss"


def _explain_exec_policy_inference(
    bot_cfg: dict,
    exec_approvals: dict | None,
    chosen: str,
) -> tuple[str, str]:
    """Return ``(set_by_detail, reason)`` explaining how
    ``_infer_exec_policy`` arrived at ``chosen`` for this bot.

    Mirrors the priority order in ``_infer_exec_policy``:
      1. Explicit ``execPolicy`` in network.json → "network.json:execPolicy".
      2. exec-approvals.json has agent-block or defaults-block allowlist
         entries → "exec_approvals:allowlist_entries".
      3. Default "full" for member bots (post-2026-05-25 pivot) →
         "member_bot_default".

    The recorded intent's ``set_by_detail`` field carries the branch
    label so future audits / generators can see WHY this bot's exec
    posture is what it is without re-deriving from raw inputs.
    """
    explicit = (bot_cfg.get("execPolicy") or "").lower().strip()
    if explicit in ("deny", "allowlist", "full"):
        return (
            "network.json:execPolicy",
            (
                f"deploy: exec security set to {chosen!r} because the "
                f"bot's network.json carries execPolicy={explicit!r} — "
                f"explicit operator override per _infer_exec_policy "
                f"priority 1"
            ),
        )

    if exec_approvals is not None:
        agents = exec_approvals.get("agents") or {}
        if isinstance(agents, dict):
            for agent_block in agents.values():
                if not isinstance(agent_block, dict):
                    continue
                al = (
                    agent_block.get("allowlist")
                    or agent_block.get("approvals")
                    or agent_block.get("allow")
                )
                if isinstance(al, (list, dict)) and len(al) > 0:
                    return (
                        "exec_approvals:agent_allowlist",
                        (
                            f"deploy: exec security set to {chosen!r} "
                            f"because exec-approvals.json has at least "
                            f"one populated agent allowlist — "
                            f"_infer_exec_policy priority 2"
                        ),
                    )
        defaults = exec_approvals.get("defaults")
        if isinstance(defaults, dict):
            for key in ("allowlist", "approvals", "allow"):
                v = defaults.get(key)
                if isinstance(v, (list, dict)) and len(v) > 0:
                    return (
                        "exec_approvals:defaults_allowlist",
                        (
                            f"deploy: exec security set to {chosen!r} "
                            f"because exec-approvals.json defaults block "
                            f"carries a populated {key!r} array — "
                            f"_infer_exec_policy priority 3"
                        ),
                    )

    return (
        "member_bot_default",
        (
            f"deploy: exec security set to {chosen!r} as the post-"
            f"2026-05-25 member-bot default (see "
            f"docs/spec-app-derived-permissions-2026-05-24.md). "
            f"No explicit override in network.json, no allowlist in "
            f"exec-approvals.json"
        ),
    )


def _record_exec_policy_intent(
    *,
    bot_id: str,
    bot_cfg: dict,
    exec_approvals: dict | None,
    security: str,
    network: dict,
) -> None:
    """Record a config_intent for the exec posture if it diverges from
    the deploy baseline.

    Called from ``ensure_plugin_config`` after the exec.security write.
    The matching ``tools.exec.ask`` field is recorded together when its
    post-write value also diverges. Idempotent on re-deploy with the
    same value (``set_intent`` last-write-wins; calling with identical
    args just appends a redundant ``updated`` audit entry — guarded by
    the value-comparison below).

    No-op when:
      - The chosen value equals the baseline (no intent needed; the
        absence-of-intent IS the "matches baseline" signal).
      - An existing intent already records the same value (skip the
        no-op write to keep audit_history tidy).

    Fail-open at the import layer so unit tests / CI environments that
    don't have ``evolve_admin.config_intent`` reachable don't break the
    deploy.
    """
    try:
        from evolve_admin.config_intent import get_intent, set_intent
    except ImportError:
        return

    # Resolve shared_dir + network_path. The network dict came from
    # ``load_network`` which doesn't carry its source path, so we fall
    # back to the conventional location. The intent helpers tolerate a
    # None network_path (skip the inline mirror update).
    shared_dir = Path(network.get("sharedDir") or _CANONICAL_SHARED_DIR)

    detail, base_reason = _explain_exec_policy_inference(
        bot_cfg, exec_approvals, security,
    )

    # exec.security
    if security != _DEPLOY_BASELINE_EXEC_SECURITY:
        existing = get_intent(
            bot_id, "tools.exec.security", shared_dir=shared_dir,
        )
        if existing is None or existing.get("value") != security:
            set_intent(
                bot_id, "tools.exec.security", security,
                reason=base_reason,
                set_by="deploy:exec_policy_inference",
                set_by_detail=detail,
                actor="deploy:exec_policy_inference",
                shared_dir=shared_dir,
            )

    # exec.ask — only diverges from baseline when security="deny" (ask
    # is removed entirely); otherwise it lands on "on-miss" which IS
    # the baseline.
    ask_value: Any = "<removed>" if security == "deny" else "on-miss"
    if security == "deny":
        # ask was deleted; record the "no ask" deviation explicitly so
        # the monitor can recognize this as deliberate rather than
        # missing-config. The recorded ``value`` is None — the same
        # representation _diff_one_bot sees for an absent field.
        existing_ask = get_intent(
            bot_id, "tools.exec.ask", shared_dir=shared_dir,
        )
        if existing_ask is None or existing_ask.get("value") is not None:
            set_intent(
                bot_id, "tools.exec.ask", None,
                reason=(
                    f"deploy: tools.exec.ask removed because exec "
                    f"security is {security!r} — ask only applies to "
                    f"allowlist / full modes"
                ),
                set_by="deploy:exec_policy_inference",
                set_by_detail=detail,
                actor="deploy:exec_policy_inference",
                shared_dir=shared_dir,
            )
    elif _DEPLOY_BASELINE_EXEC_ASK != "on-miss":
        # Defensive: if the deploy baseline for ask ever shifts away
        # from "on-miss", record the deviation. Today this branch is
        # dead but the symmetry is intentional — future baseline pivots
        # produce intent records without code churn here.
        pass

    # Keep ask_value bound for static-analyzer warnings; the value
    # itself is only meaningful inside the conditional above.
    del ask_value


def _apply_permission_baseline_gap_fills(cfg: dict) -> bool:
    """Fill in permission-config fields the baseline expects, in-place.

    Returns True iff any field was added.

    Every key written here MUST exist in OC's config schema — writing an
    unrecognized root key causes ``openclaw config validate`` to reject the
    file, and ``safe_write_bot_config`` then aborts the deploy. The
    pre-2026-05-19 version of this gap-fill wrote a ``sandbox.enabled``
    root-level boolean that doesn't exist in OC's schema (OC's real sandbox
    config is at ``agents.defaults.sandbox.mode``); that field has been
    removed (#1271). See ``permissions.posture`` for the architectural note.

    The dotted paths this gap-fill writes must stay in lockstep with
    ``permissions.inventory.PERMISSION_CONFIG_FIELDS`` and the
    ``DEFAULT_BASELINE.pod_default.permission_config`` block, modulo fields
    intentionally left absent (e.g. ``tools.exec.host``, which is bot-specific).

    Extracted from ``ensure_plugin_config`` so the invariant "this function
    only writes keys at OC-valid root paths" can be tested in isolation,
    without standing up a real bot home and shelling out to ``openclaw config
    validate``. See ``test_permission_baseline_uses_only_valid_oc_root_keys``.
    """
    changed = False
    tools_section = cfg.setdefault("tools", {})
    if tools_section.setdefault("web", {}).setdefault("search", {}).get("enabled") is None:
        tools_section["web"]["search"]["enabled"] = True
        changed = True
    if tools_section.setdefault("web", {}).setdefault("fetch", {}).get("enabled") is None:
        tools_section["web"]["fetch"]["enabled"] = True
        changed = True
    commands_section = cfg.setdefault("commands", {})
    if commands_section.get("native") is None:
        commands_section["native"] = "auto"
        changed = True
    if commands_section.get("nativeSkills") is None:
        commands_section["nativeSkills"] = "auto"
        changed = True
    return changed


# ── Cost-settings gap-fill ───────────────────────────────────────────────────
#
# Restores cost settings that doctor --fix or a reinstall may have wiped.
# Priority: 1) saved snapshot (operator intent), 2) balanced defaults.
# Only fills gaps — never overwrites settings already present in openclaw.json.
#
# Heartbeat is special: it gets *subfield* gap-fill. Block-level was missing
# `every` defaults on bots whose heartbeat block existed but was incomplete,
# leaving them on OC's 30-min default — see incident 2026-06-04. The other
# fields stay block-level (whole-dict replace) — different semantics.
#
# heartbeat.model is intentionally NOT set here. The Evolve plugin's
# ModelRouter (TurnObserver.resolveModelRouting → ModelRouter.resolveModelOverride)
# pre-classifies heartbeat sessions as `background` via the
# `before_model_resolve` hook and routes to tier3.models[0] from the bot's
# evolve-tiers.json — *before* OC ever consults this field. Pinning a literal
# here would be misleading: it would never be honored on the heartbeat path
# while ModelRouter is active. If you genuinely want to override tier3
# routing for heartbeats, the right knob is the bot's tier3.models[0] in
# evolve-tiers.json, not this field.
_BALANCED_HEARTBEAT_DEFAULTS: dict[str, Any] = {
    "isolatedSession": True,
    "lightContext": True,
    "every": "2h",
}
_BALANCED_COST_DEFAULTS: dict[str, Any] = {
    "heartbeat": _BALANCED_HEARTBEAT_DEFAULTS,
    "contextPruning": {"mode": "cache-ttl", "ttl": "5m", "keepLastAssistants": 5},
    "compaction": {
        "mode": "safeguard",
        "reserveTokensFloor": 50000,
        "memoryFlush": {"enabled": True, "softThresholdTokens": 10000},
    },
    "bootstrapTotalMaxChars": 100_000,
    # bootstrapMaxChars is OC's per-file injection cap. Default is 12000;
    # evo's AGENTS.md is intentionally larger (~28KB) because it carries the
    # pod glossary (chips, signal producers, proposal generators) +
    # cite-the-tool / anti-fabrication rules + page context guidance.
    # Without bumping the cap, OC truncates AGENTS.md to 12KB and the model
    # loses ~half its taught semantics. See spec §3.3 "Bootstrap budget".
    # Harmless on member bots (their AGENTS.md is small; this is just a ceiling).
    "bootstrapMaxChars": 40_000,
}


def gap_fill_cost_settings(cfg: dict, snapshot: dict | None) -> bool:
    """Apply cost-defaults gap-fill to ``cfg`` in-place.

    Returns True iff anything was added. Extracted from ``ensure_plugin_config``
    so the heartbeat subfield logic can be unit-tested without exercising the
    full file-I/O path.

    ``cfg`` is the parsed openclaw.json; ``snapshot`` is the operator's saved
    cost-settings (``{shared_dir}/cost-settings/{bot}.json::settings``) or
    None when no snapshot exists.
    """
    snapshot = snapshot or {}
    cfg.setdefault("agents", {}).setdefault("defaults", {})
    agent_defaults = cfg["agents"]["defaults"]
    changed = False
    for cost_field in _BALANCED_COST_DEFAULTS:
        if cost_field == "heartbeat":
            # Subfield gap-fill: every existing field is preserved (operator
            # intent wins). Missing subfields filled from snapshot first, then
            # _BALANCED_HEARTBEAT_DEFAULTS. This is what catches bots whose
            # heartbeat block exists but is missing `every` — they would
            # otherwise stay on OC's 30-min default and accumulate billable
            # heartbeats far above their intended cadence.
            hb_block = agent_defaults.get("heartbeat")
            if not isinstance(hb_block, dict):
                snap_hb = snapshot.get("heartbeat")
                if isinstance(snap_hb, dict):
                    agent_defaults["heartbeat"] = dict(snap_hb)
                else:
                    agent_defaults["heartbeat"] = dict(_BALANCED_HEARTBEAT_DEFAULTS)
                changed = True
                hb_block = agent_defaults["heartbeat"]
            snap_hb = (
                snapshot.get("heartbeat")
                if isinstance(snapshot.get("heartbeat"), dict) else {}
            )
            for subkey, subdefault in _BALANCED_HEARTBEAT_DEFAULTS.items():
                if subkey not in hb_block:
                    if subkey in snap_hb and snap_hb[subkey] is not None:
                        hb_block[subkey] = snap_hb[subkey]
                    else:
                        hb_block[subkey] = subdefault
                    changed = True
        elif cost_field not in agent_defaults:
            if cost_field in snapshot:
                # Snapshot has the field — None means it was intentionally disabled
                # (e.g. performance profile). Respect that; don't inject a default.
                if snapshot[cost_field] is not None:
                    agent_defaults[cost_field] = snapshot[cost_field]
                    changed = True
            else:
                # No snapshot entry → fresh install or pre-snapshot install.
                # Inject the balanced default so the bot starts cost-configured.
                agent_defaults[cost_field] = _BALANCED_COST_DEFAULTS[cost_field]
                changed = True
    return changed


# Detail-string parsing for OC's plugins.installs_unpinned_npm_specs
# finding. The detail format OC v2026.5.18+ emits is:
#
#   Unpinned plugin index install records:
#   - codex (@openclaw/codex)
#   - other-id (@scope/other-pkg)
#
# Anchored on "- " line prefix with the parenthesized npm package after
# the plugin id. Tolerates extra whitespace and ignores any non-matching
# lines so a future detail-format change doesn't crash parsing.
_UNPINNED_LINE_RE = re.compile(
    r"^\s*-\s*([A-Za-z0-9_.-]+)\s*\(([^)]+)\)\s*$"
)


def _parse_unpinned_finding_detail(detail: str) -> list[tuple[str, str]]:
    """From OC's unpinned-finding detail text, return ``[(plugin_id, npm_pkg)]``.

    Empty list when the detail doesn't carry the expected ``- id (pkg)``
    lines — including the empty-detail and "no findings" cases. The
    caller short-circuits when this returns empty.
    """
    if not isinstance(detail, str) or not detail:
        return []
    out: list[tuple[str, str]] = []
    for line in detail.splitlines():
        m = _UNPINNED_LINE_RE.match(line)
        if not m:
            continue
        plugin_id = m.group(1).strip()
        npm_pkg = m.group(2).strip()
        if plugin_id and npm_pkg:
            out.append((plugin_id, npm_pkg))
    return out


def _live_plugin_versions(bot_user: str) -> dict[str, str]:
    """Return ``{plugin_id: version}`` from ``openclaw plugins list --json``.

    Drops any plugin whose ``version`` field is null / missing — for those
    we don't have a useful pin target. Caller treats the absence as "skip".
    """
    proc = subprocess.run(
        ["sudo", "-u", bot_user, "-H", "openclaw", "plugins", "list", "--json"],
        capture_output=True, text=True, timeout=30, cwd="/tmp",
    )
    if proc.returncode != 0:
        return {}
    try:
        payload = json.loads(proc.stdout or "")
    except (json.JSONDecodeError, ValueError):
        return {}
    plugins = payload.get("plugins") if isinstance(payload, dict) else None
    if not isinstance(plugins, list):
        return {}
    out: dict[str, str] = {}
    for p in plugins:
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        ver = p.get("version")
        if isinstance(pid, str) and isinstance(ver, str) and pid and ver:
            out[pid] = ver
    return out


def _repin_unpinned_via_audit(
    bot_id: str, bot_user: str,
) -> tuple[list[str], list[str]]:
    """Drive plugin re-pinning from ``openclaw security audit`` output.

    Returns ``(ok_plugin_ids, failure_messages)``. Both lists are
    operator-readable — the caller logs them per bot during deploy.

    Implementation:
      1. Run ``openclaw security audit --deep --json``. If it fails or
         carries no ``plugins.installs_unpinned_npm_specs`` finding, the
         function exits with empty lists.
      2. Parse the finding's detail string for ``(plugin_id, npm_pkg)``
         pairs. Stop early if there are none.
      3. Run ``openclaw plugins list --json`` to learn each plugin's
         current runtime version. Plugins without a discoverable version
         are skipped (added to ``failure_messages``) — we won't guess a
         pin.
      4. For each pair, call
         ``oc_neutralize.install_externalized_plugin(bot_user, npm_pkg,
         force=True, version=version)`` which runs ``openclaw plugins
         install <npm_pkg>@<version> --force`` and writes a fresh
         install record that satisfies the audit.

    Defensive about subprocess failures — never raises. The caller treats
    a raised exception as "skip the sweep this deploy" but in practice
    the function returns the failures via ``failure_messages``.
    """
    proc = subprocess.run(
        ["sudo", "-u", bot_user, "-H", "openclaw", "security", "audit",
         "--deep", "--json"],
        capture_output=True, text=True, timeout=60, cwd="/tmp",
    )
    if proc.returncode != 0:
        return [], []
    try:
        payload = json.loads(proc.stdout or "")
    except (json.JSONDecodeError, ValueError):
        return [], []
    if isinstance(payload, list):
        findings = payload
    elif isinstance(payload, dict):
        findings = payload.get("findings") or []
    else:
        return [], []

    pairs: list[tuple[str, str]] = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        if f.get("checkId") != "plugins.installs_unpinned_npm_specs":
            continue
        pairs.extend(_parse_unpinned_finding_detail(f.get("detail") or ""))
    if not pairs:
        return [], []

    versions = _live_plugin_versions(bot_user)

    ok_ids: list[str] = []
    failures: list[str] = []
    try:
        from .oc_neutralize import install_externalized_plugin
    except ImportError as exc:
        return [], [f"oc_neutralize import failed: {exc}"]

    for plugin_id, npm_pkg in pairs:
        version = versions.get(plugin_id)
        if not version:
            failures.append(
                f"{plugin_id} ({npm_pkg}): no live version from "
                f"`openclaw plugins list`; skipping re-pin"
            )
            continue
        try:
            # allow_unlisted: `npm_pkg` is parsed out of an audit finding's
            # detail string, so it comes from OC's install records rather than
            # a repo constant, and `version` is the plugin's LIVE version from
            # `openclaw plugins list` — i.e. this is a re-pin of an
            # already-installed plugin at the version it already runs, which
            # fetches no new code. The Layer 1 provenance gate warns rather
            # than refusing (design §4, "warn-not-refuse on re-pin"); refusing
            # would regress this working sweep.
            ok, err = install_externalized_plugin(
                bot_user, npm_pkg, force=True, version=version,
                allow_unlisted=True,
            )
        except Exception as exc:  # noqa: BLE001 — keep the sweep going
            failures.append(f"{plugin_id} ({npm_pkg}@{version}): raised: {exc}")
            continue
        if ok:
            ok_ids.append(plugin_id)
        else:
            failures.append(f"{plugin_id} ({npm_pkg}@{version}): {err}")
    return ok_ids, failures


def ensure_plugin_config(bot_id: str, network: dict) -> None:
    """Write plugin config and cost defaults to bot's openclaw.json as needed.

    - Injects the evolve plugin entry if absent or incomplete.
    - Injects balanced cost defaults for any absent cost settings, so fresh
      installs and post-doctor--fix repairs start configured rather than
      unconfigured (which scores F on the cost efficiency rating).

    Reads openclaw.json directly (evolve has ACL read access after set_evolve_read_acl),
    with sudo /bin/cat as root fallback.
    """
    bot_user = _bot_user_for(bot_id, network)
    oc_json = _user_home(bot_user) / ".openclaw/openclaw.json"

    # Consolidated existence + read via direct-first, sudo-fallback.
    #
    # Path.exists() + Path.read_text() as the admin user both fail on bot homes that
    # are drwx------ (the default) even though an ACL grants the evolve user
    # read access, because the admin user is neither the owner nor the evolve user.
    # The file does exist — the admin just can't stat it. The 2026-04-21 deploy
    # surfaced this as "/Users/<bot>/.openclaw/openclaw.json not found" for 5
    # of 7 bots. Fix: don't distinguish "missing" from "unsearchable" until
    # we've tried the sudo path that already exists as a read fallback.
    content: str | None = None
    direct_err: str | None = None
    try:
        content = oc_json.read_text()
    except FileNotFoundError:
        # Could be truly missing OR (more likely for bot homes) parent dir
        # isn't searchable by this user. Fall through to sudo path.
        direct_err = "FileNotFoundError"
    except PermissionError:
        direct_err = "PermissionError"
    except OSError as e:
        direct_err = f"{type(e).__name__}: {e}"

    if content is None:
        result = subprocess.run(
            ["sudo", "/bin/cat", str(oc_json)],
            capture_output=True, text=True,
            cwd=_PROFILE.scratch_dir
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            # /bin/cat prints "No such file or directory" only when the
            # file genuinely doesn't exist. Other failures (perm denied on
            # the sudoers side, etc.) indicate admin-install issues, not
            # missing files — be explicit about which.
            if "No such file" in stderr:
                raise RuntimeError(f"{oc_json} not found")
            raise RuntimeError(
                f"Cannot read {oc_json}: "
                f"direct={direct_err or 'ok'} sudo_cat={stderr or 'empty'}"
            )
        content = result.stdout

    try:
        cfg = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{oc_json} is not valid JSON: {exc}")

    bots_cfg = network.get("bots", {})
    bot_cfg = bots_cfg.get(bot_id, {})
    role = bot_cfg.get("role") or "member"
    network_id = network.get("networkId", "my-network")
    shared_dir = network.get("sharedDir", str(_CANONICAL_SHARED_DIR))

    changed = False

    # Repair agents.main → agents.defaults migration.
    # Old wizard wrote agents.main.defaults.model; OC schema only accepts agents.defaults.
    # Must run BEFORE the setdefault("defaults", {}) below so the model isn't lost.
    if isinstance(cfg.get("agents", {}).get("main"), dict):
        main_defaults = cfg["agents"]["main"].get("defaults", {})
        top_defaults = cfg["agents"].setdefault("defaults", {})
        for key, val in main_defaults.items():
            if key not in top_defaults:  # don't clobber existing top-level settings
                top_defaults[key] = val
        cfg["agents"].pop("main")
        if not cfg["agents"]:
            cfg.pop("agents")
        changed = True

    # Normalize/restore agents.defaults.model.
    # - Bare string → {primary, fallbacks} object (old wizard format)
    # - Missing → detect provider from auth-profiles and set a default
    # This runs after agents.main migration and after every openclaw plugins install.
    top_defaults = cfg.get("agents", {}).get("defaults", {})
    if isinstance(top_defaults.get("model"), str):
        top_defaults["model"] = {"primary": top_defaults["model"], "fallbacks": []}
        changed = True
    elif not top_defaults.get("model"):
        # Pass cfg so _detect_provider_model can resolve from the bot's
        # OWN tier config first (the tier-resolved primary), falling
        # back to RECOMMENDED only when no tier config exists. This
        # honors the "defaults by tiers, not by hardcoded models" rule:
        # admin code never picks the model name — the bot's tier config
        # does, and admin code just picks WHICH tier (floor for member,
        # workhorse for primary).
        detected = _detect_provider_model(
            bot_id, bot_user=bot_user, role=role, cfg=cfg,
        )
        if detected:
            cfg.setdefault("agents", {}).setdefault("defaults", {})["model"] = {
                "primary": detected, "fallbacks": [],
            }
            changed = True
            try:
                _log.info(
                    "deploy: seeded agents.defaults.model.primary for "
                    "%s (role=%s) → %s", bot_id, role, detected,
                )
            except Exception:
                pass

    # Ensure thinkingDefault is off.
    # claude-sonnet-4-6 defaults to adaptive thinking, which injects thinking blocks into
    # session history. Context pruning then modifies those blocks, and Anthropic's API
    # rejects any request that contains modified thinking blocks.
    agent_def = cfg.setdefault("agents", {}).setdefault("defaults", {})
    if agent_def.get("thinkingDefault") != "off":
        agent_def["thinkingDefault"] = "off"
        changed = True

    # Repair legacy configs where plugins.entries was written as [] instead of {}.
    if isinstance(cfg.get("plugins", {}).get("entries"), list):
        cfg.setdefault("plugins", {})["entries"] = {}
        changed = True

    # Normalize plugins.load.paths to the single root-owned install dir.
    #
    # Older bot configs (team_bot_a, admin_bot, security_bot as of 2026-05) listed both
    #   /Users/Shared/evolve-repo/packages/plugin   ← admin-owned
    #   /Users/Shared/evolve-plugin                  ← root-owned
    # OC 2026.4.29 added an ownership check that requires plugin paths to be
    # owned by the bot user or root — the repo path now logs
    #   blocked plugin candidate: suspicious ownership
    # at every gateway start. Even when the install-dir entry is correct, the
    # blocked first-path attempt produces noise and historically masked load
    # failures (the operator sees "evolve loaded" from the second path and
    # assumes the first was redundant rather than broken). Strip anything
    # that isn't the install dir; treat .load.paths as a flat list of strings.
    plugins_section = cfg.setdefault("plugins", {})
    load_section = plugins_section.get("load")
    expected_path = str(PLUGIN_INSTALL_DIR)
    # Only normalize when load.paths already exists — `openclaw plugins install`
    # writes it on first install. Don't seed it here on a fresh bot; that's
    # the install command's job. We're scoped to one job: removing entries
    # that aren't PLUGIN_INSTALL_DIR. We deliberately don't preserve other
    # paths an operator might have added — there's exactly one supported
    # layout for this plugin (root-owned install at PLUGIN_INSTALL_DIR), and
    # any other path is either a relic of a prior layout or a copy that
    # OC's ownership check will reject.
    if isinstance(load_section, dict):
        existing_paths = load_section.get("paths")
        if isinstance(existing_paths, list) and existing_paths != [expected_path]:
            load_section["paths"] = [expected_path]
            changed = True

    # Repair missing gateway.mode — without it the gateway refuses to start.
    gw = cfg.setdefault("gateway", {})
    if not gw.get("mode"):
        gw["mode"] = "local"
        changed = True
    if not gw.get("bind"):
        gw["bind"] = "loopback"
        changed = True
    # Repair missing gateway.auth — unauthenticated loopback gateways fail the
    # OC security audit (gateway.loopback_no_auth). Generate a fresh token if
    # auth is absent or the token field is empty. The gateway reads this from
    # openclaw.json on startup, so no plist change is needed — just a redeploy.
    gw_auth = gw.get("auth") or {}
    if not isinstance(gw_auth, dict) or not gw_auth.get("token"):
        gw["auth"] = {"mode": "token", "token": secrets.token_hex(32)}
        changed = True

    # Default gateway.trustedProxies=[] for loopback-only bots. OC's security
    # audit emits gateway.trusted_proxies_missing whenever bind=loopback and
    # the key is missing/null — cosmetic (no proxy = no spoofable XFF) but
    # noisy on the Alerts page. Gap-fill only; an operator-set list of proxy
    # IPs must survive untouched.
    if gw.get("bind") == "loopback" and gw.get("trustedProxies") is None:
        gw["trustedProxies"] = []
        changed = True

    # ── App-derived permissions reconciler ────────────────────────────
    # Spec: docs/spec-app-derived-permissions-2026-05-24.md §2.
    # Computes the manifest-derived "would-be" allowlist with provenance
    # and writes it to /Users/<bot>/.openclaw/exec-approvals.preview.json.
    # Phase A is tracking-only — never modifies the live exec-approvals.json.
    # Runs BEFORE _infer_exec_policy so the preview is always fresh when an
    # operator inspects the bot's posture after a deploy.
    try:
        from evolve_admin.app_permissions.reconciler import reconcile_bot_permissions
        reconcile_bot_permissions(bot_id, network=network)
    except Exception as exc:
        # Reconciler failure is informational, not a deploy-blocker. The
        # bot's runtime config (openclaw.json) is set below regardless;
        # losing a preview file is a visibility-gap, not a breakage.
        print(
            f"[deploy] permissions.reconciler failed for {bot_id}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

    # Set tools.exec.security based on the bot's actual exec needs.
    #
    # Policy (in priority order):
    #   1. Explicit execPolicy in network.json bot config.
    #   2. exec-approvals.json has allowlist entries → "allowlist".
    #   3. Default → "full".
    #
    # Phase E.4 (2026-05-25) removed the primary-bot carve-out that
    # previously routed evo to "deny". Post-Phase-E.2.b cutover (evo
    # runs as the unprivileged ``evo`` macOS user), the carve-out is
    # unnecessary — see docs/spec-evo-account-separation-2026-05-25.md.
    #
    # Pivoted 2026-05-25 from a "deny" default to a "full" default for
    # member bots — see docs/spec-app-derived-permissions-2026-05-24.md.
    # A member bot is a trusted agent running in its own user account;
    # the "deny" default produced a team_bot_a Slack failure on 2026-05-24 where
    # INSTALLED_APPS.md declared capabilities the policy denied at runtime.
    # The permissions reconciler now computes a manifest-derived allowlist
    # *preview* under "full" mode so operators can opt into tighter
    # ("allowlist") posture from a known-correct seed without breaking the
    # bot in the meantime.
    ea = _read_exec_approvals(bot_user)
    target_exec_security = _infer_exec_policy(
        bot_cfg, ea, bot_id=bot_id, bot_role=role,
    )
    tools_exec = cfg.setdefault("tools", {}).setdefault("exec", {})
    if tools_exec.get("security") != target_exec_security:
        tools_exec["security"] = target_exec_security
        changed = True
    # "ask" is only meaningful for allowlist/full modes where exec is possible.
    # For deny mode, remove any stale ask key.
    if target_exec_security == "deny":
        if "ask" in tools_exec:
            del tools_exec["ask"]
            changed = True
    else:
        if tools_exec.get("ask") != "on-miss":
            tools_exec["ask"] = "on-miss"
            changed = True

    # Phase 1 of docs/spec-config-intent-system-2026-05-21.md §3:
    # record_intent_on_set for any non-baseline exec posture this deploy
    # wrote. Without this, operator-deliberate deviations (a bot with
    # an explicit network.json execPolicy override, a bot whose
    # exec-approvals.json has been operator-curated to a non-empty
    # allowlist, etc.) only get an intent if someone runs
    # `evolve-admin intent set` by hand — which is how the first
    # cohort of bots were backfilled in May 2026. deploy.py's exec
    # policy inference itself silently produced the deviation. After
    # this lands, every fresh deploy whose exec posture diverges from
    # the post-2026-05-25 baseline ("full" + "on-miss") records the
    # intent in the same sweep that wrote the config field. The next
    # permission_monitor pass then suppresses the drift signal per
    # PR #2295. Fail-open: any exception during intent recording is
    # swallowed — never block a deploy on metadata hygiene.
    try:
        _record_exec_policy_intent(
            bot_id=bot_id, bot_cfg=bot_cfg, exec_approvals=ea,
            security=target_exec_security,
            network=network,
        )
    except Exception as _intent_exc:  # noqa: BLE001
        print(
            f"[deploy] config_intent record skipped for {bot_id}: "
            f"{type(_intent_exc).__name__}: {_intent_exc}",
            flush=True,
        )

    # ── Permission baseline gap-fills ───────────────────────────────────────
    # Fields the permission_monitor baseline (DEFAULT_BASELINE in
    # packages/analyzer/permissions/baseline.py) expects every bot to carry.
    # Setup wizard wrote a minimal openclaw.json that omits these — without
    # gap-fill, every fresh bot drifts on its first monitor pass with
    # perm_config_drift signals. The values match the modal pod posture;
    # per-bot overrides go in the baseline's per_bot_overrides, not here.
    # Gap-fill only (setdefault) so deliberate operator-set values survive.
    if _apply_permission_baseline_gap_fills(cfg):
        changed = True
    # Google bots: re-expose plugin Google tools past a curated tool profile (e.g. "coding") via tools.alsoAllow — see google_tools_policy.
    from .google_tools_policy import ensure_google_tools_allowlisted
    if ensure_google_tools_allowlisted(cfg, bot_id, network):
        changed = True
    # NB: sandbox is intentionally NOT injected — top-level `sandbox` is not an OC-valid root key (use agents.defaults.sandbox.mode); see packages/analyzer/permissions/baseline.py for the full rationale.

    # Repair channels.telegram written with wrong field names by old wizard.
    tg = cfg.get("channels", {}).get("telegram")
    if isinstance(tg, dict):
        if "token" in tg:
            tg["botToken"] = tg.pop("token")
            changed = True
        if "chatId" in tg:
            del tg["chatId"]
            changed = True
        for key, val in [("enabled", True), ("dmPolicy", "pairing"),
                         ("groupPolicy", "allowlist"), ("streaming", {"mode": "off"})]:
            if key not in tg:
                tg[key] = val
                changed = True

    # ── Plugin entry ────────────────────────────────────────────────────────────
    # Phase 3 cutover (docs/spec-openclaw-json-derived-artifact-2026-05-24.md):
    # the ``plugins.entries.evolve.config.*`` block is now *materialized* from
    # declared inputs (identity + defaults + per-bot overrides) instead of
    # being gap-filled. Materialization gives us three properties gap-fill
    # could not:
    #
    #   1. **Stale-key prune.** Keys removed from the plugin's strict
    #      configSchema are dropped — the PR #1525 cleanup.
    #   2. **Drift auto-promotion.** Ad-hoc edits land as
    #      ``needs_review=True`` overrides; redeploys never silently lose
    #      them.
    #   3. **Override consumption.** Operators (and L2 RSI proposals) write
    #      to ``{shared_dir}/sandbox/overrides/<bot>.json``, not the live
    #      openclaw.json. The deploy is the materializer.
    #
    # Other openclaw.json sections (gateway.*, session.*, channels.*, rest
    # of agents.defaults.*) still flow through the repair passes above —
    # widening the materialized slice is a follow-on PR. ``subagent`` and
    # ``hooks`` siblings of ``config`` stay below.

    # 1. Ensure the structural skeleton exists.
    plugin_entry = (
        cfg.setdefault("plugins", {})
           .setdefault("entries", {})
           .setdefault("evolve", {})
    )
    plugin_entry.setdefault("enabled", True)
    plugin_entry.setdefault("config", {})
    plugin_entry.setdefault("subagent", {})

    # 2. Materialize the config block from inputs. The materializer
    # handles identity, defaults, override consumption, drift
    # auto-promotion, and stale-key prune in one pass.
    #
    # Exception handling: we DON'T catch a broad ``Exception`` here. The
    # materializer's internal logic catches expected I/O / schema errors
    # (OverrideStateError, OverrideValidationError, generic OSError on
    # write) and surfaces them via the ``persistence_failed`` field. Any
    # exception that escapes is genuinely unexpected (programming error,
    # corrupt schema, network/network.json issue) — better to surface
    # those than silently fall back to gap-fill, since the gap-fill path
    # does NOT include the stale-key prune that fixes the #1525 class.
    # A degraded deploy that doesn't strip stale keys is worse than a
    # loud failure that surfaces the bug.
    from .openclaw_materializer import materialize_evolve_plugin_config, plugin_defaults_for_bot
    from .config import DEFAULT_SHARED_DIR as _DEFAULT_SHARED_DIR
    materialize_result = materialize_evolve_plugin_config(
        bot_id,
        network,
        current_block=dict(plugin_entry["config"]),   # defensive copy
        defaults_registry=plugin_defaults_for_bot(bot_user, _PLUGIN_CONFIG_DEFAULTS),
        shared_dir=Path(network.get("sharedDir") or _DEFAULT_SHARED_DIR),
    )

    # Replace the config dict in place.
    if plugin_entry["config"] != materialize_result.new_block:
        plugin_entry["config"] = materialize_result.new_block
        changed = True
    if materialize_result.pruned_stale:
        print(
            f"[evolve/deploy] {bot_id}: pruned stale evolve-config keys: "
            f"{', '.join(materialize_result.pruned_stale)}"
        )
    if materialize_result.auto_promoted:
        print(
            f"[evolve/deploy] {bot_id}: auto-promoted ad-hoc edits to "
            f"overrides (needs_review): "
            f"{', '.join(materialize_result.auto_promoted)}"
        )
    if materialize_result.kept_overrides:
        print(
            f"[evolve/deploy] {bot_id}: applied overrides: "
            f"{', '.join(materialize_result.kept_overrides)}"
        )
    for clobber in materialize_result.override_clobbered:
        print(f"[evolve/deploy] {bot_id}: WARN {clobber.message}")
    if materialize_result.persistence_failed:
        # One or more drift auto-promotes failed to persist to the
        # overrides file. The in-memory value is honored in the
        # materialized block (so this deploy looks correct), but the
        # next deploy will re-detect the same drift. Operator should
        # investigate the overrides directory ACL / filesystem state.
        # Loud warning suffices; this isn't user-facing breakage.
        print(
            f"[evolve/deploy] {bot_id}: WARN auto-promote persistence "
            f"failed for one or more keys — see materializer logs. "
            f"Next deploy will re-attempt; inspect "
            f"{network.get('sharedDir')}/sandbox/overrides/{bot_id}.json"
        )

    # 3. Lets the evolve plugin pin a model in subagent.run() calls.
    # ``subagent`` is a SIBLING of ``config`` — the materializer leaves it
    # alone. OC >= 2026.7 narrowed the grant to FALLBACK-scoped runs only
    # (2026-07-31 incident); request-scoped pins are rejected and the plugin
    # adapts at runtime (observer/subagentRun.ts). Keep writing it anyway.
    if plugin_entry["subagent"].get("allowModelOverride") is not True:
        plugin_entry["subagent"]["allowModelOverride"] = True
        changed = True

    # Required by openclaw ≥ 2026.4.29: non-bundled plugins must declare
    # allowConversationAccess=true to receive llm_output, agent_end, and
    # before_agent_run hook events that carry conversation content.
    # Also a sibling of config — not materialized.
    plugin_hooks = plugin_entry.setdefault("hooks", {})
    if plugin_hooks.get("allowConversationAccess") is not True:
        plugin_hooks["allowConversationAccess"] = True
        changed = True

    # 5. dashboardEnabled is role-conditional (primary → True, member → False).
    # The materializer doesn't compute this (defaults_registry has no
    # entry for it); set explicitly so a role change is picked up.
    plugin_cfg = plugin_entry["config"]
    target_dashboard = role == "primary"
    if plugin_cfg.get("dashboardEnabled") != target_dashboard:
        plugin_cfg["dashboardEnabled"] = target_dashboard
        changed = True

    # 6. Strip keys that left the plugin manifest's configSchema. The schema
    # uses additionalProperties: false, so any lingering field rejected at
    # plugins install time with "must NOT have additional properties". A
    # field removed from the schema is never gap-filled by the loop above —
    # so without an active prune it lingers on existing bots until something
    # strips it by hand. PR #1525 dropping `reportingEnabled` without a
    # migration broke deploys on six bots before this prune was added; this
    # pass closes the trapdoor for every future schema removal.
    #
    # Skipped silently when the manifest is unreadable (allowed == empty set);
    # a broken manifest is a worse failure mode than a stale key surviving.
    allowed = _allowed_plugin_config_keys()
    if allowed:
        for stale_key in [k for k in plugin_cfg if k not in allowed]:
            del plugin_cfg[stale_key]
            print(
                f"[evolve/deploy] {bot_id}: pruned stale plugin-config key "
                f"'{stale_key}' (no longer in configSchema)"
            )
            changed = True

    # 6b. Schema-skew tripwire (legibility). When the deployed manifest lags
    # the source schema the omit/strip above is SILENT — raise a pod Signal so
    # the operator sees it. Guarded: a signals hiccup must never block a deploy.
    try:
        from .plugin_schema_skew import reconcile_plugin_schema_skew
        reconcile_plugin_schema_skew(
            Path(network.get("sharedDir") or _DEFAULT_SHARED_DIR),
            install_dir=PLUGIN_INSTALL_DIR, source_dir=PLUGIN_SRC_DIR,
        )
    except Exception as _skew_exc:   # noqa: BLE001 — never fail a deploy on this
        print(f"[evolve/deploy] {bot_id}: plugin-schema-skew check skipped: {_skew_exc}")

    # 7. Brave plugin entry — enabled ONLY when a key backs it (see brave_key).
    # Brave stopped being required on 2026-06-24 (#3219); the baseline this
    # cited is now empty (bootstrap.py required_plugins=[]). Only write the
    # entry when an install record exists, else deploys oscillate.
    try:
        from .safe_upgrade import _installed_plugin_ids as _brave_installed_check
        brave_key = resolve_pod_brave_key(bot_id, network, cfg)
        installed_ids = _brave_installed_check(bot_id, network)
        if brave_key and "brave" not in installed_ids:
            try:
                from .oc_neutralize import install_externalized_plugin
                ok_brave, err_brave = install_externalized_plugin(
                    bot_user, "@openclaw/brave-plugin", force=True,
                )
                if ok_brave:
                    installed_ids = _brave_installed_check(bot_id, network)
                else:
                    print(f"[evolve/deploy] {bot_id}: brave gap-fill install failed: {err_brave}")
            except Exception as e:
                print(f"[evolve/deploy] {bot_id}: brave gap-fill install raised: {e}")
        if brave_key and "brave" in installed_ids:
            brave_entry = (
                cfg.setdefault("plugins", {})
                   .setdefault("entries", {})
                   .setdefault("brave", {})
            )
            web_search = brave_entry.setdefault("config", {}).setdefault("webSearch", {})
            # Gap-fill only — resolve returns the bot's OWN key when it has
            # one, so this never clobbers a hand-set per-bot key.
            if web_search.get("apiKey") != brave_key:
                web_search["apiKey"] = brave_key
                changed = True
            if brave_entry.get("enabled") is not True:
                brave_entry["enabled"] = True
                changed = True
        elif not brave_key and "brave" in installed_ids:
            # Installed but unusable — say so rather than silently enabling.
            print(
                f"[evolve/deploy] {bot_id}: brave installed but no API key "
                f"resolvable — leaving it unconfigured (register a pod key: "
                f"evolve-admin keys add brave --provider brave --scope shared)"
            )
    except ImportError:
        pass  # safe_upgrade unavailable — gap-fill skipped, monitor will alert

    # 7b. Reconcile unpinned npm install records.
    #
    # OC 2026.5.18+ added `plugins.installs_unpinned_npm_specs`, which fires
    # on any install record whose stored `spec` field isn't of the form
    # `<pkg>@X.Y.Z`. Fresh installs via `install_externalized_plugin` get
    # auto-pinned to `<pkg>@<oc_version>` (see _resolve_install_spec in
    # oc_neutralize.py). But bots whose @openclaw/* plugins were installed
    # BEFORE the auto-pin landed keep their bare-spec records — the install
    # path only fires when a plugin is missing, not when it's already there.
    # On the 2026-05-28 deploy --all this fired on 7/8 bots; atlas was clean
    # because its brave install was created after the auto-pin path shipped.
    #
    # Reconcile here by re-installing each unpinned @openclaw/* npm record
    # with version pinned to its ALREADY-RESOLVED version (the `resolvedVersion`
    # field in installs.json). Pinning to resolvedVersion (not the current OC
    # runtime version) makes the fix cosmetic — never silently upgrades a
    # plugin. Idempotent: once spec is pinned, subsequent deploys skip.
    #
    # Sticky-pin verified 2026-05-28 (live on the pod, post-#1705): team_bot_a's
    # specs stayed pinned across a re-deploy with no `re-pinned` line printed,
    # and neither `doctor --fix` nor `plugins install -l <localpath>` (the
    # only two OC writes between reconciler invocations within a single
    # deploy) touches the npm install records' `spec` field. The
    # `installs_unpinned_npm_specs` audit warnings that persisted across
    # multiple audit cycles earlier that day reflected `deploy --all` not
    # having been run since the reconciler shipped — not the reconciler
    # failing to stick. Don't re-investigate sticky-pin behavior without
    # first checking whether a deploy has actually run since the audit window.
    try:
        from .oc_neutralize import install_externalized_plugin as _reinst
        from .safe_upgrade import read_installs_for_write_decision as _read_inst  # None on a degraded (non-WAL) read — never re-pin from a stale snapshot
        inst_data = _read_inst(bot_user)
        records = inst_data.get("installRecords") if isinstance(inst_data, dict) else None
        # Collect outcomes so we can emit one summary line per bot instead of
        # one per plugin. The previous per-plugin output flooded `deploy --all`
        # with 18+ lines on a typical pod (≈3 @openclaw/* plugins × 6 affected
        # bots). Failures are still surfaced individually because each one
        # is an actual problem the operator may need to investigate — but the
        # happy path is rolled up into a single line per bot.
        repinned_oks: list[str] = []
        repin_failures: list[str] = []
        if isinstance(records, dict):
            for plugin_id, rec in records.items():
                if not isinstance(rec, dict):
                    continue
                if rec.get("source") != "npm":
                    continue  # path-source plugins (e.g. 'evolve') have no spec to pin
                spec = rec.get("spec")
                if not isinstance(spec, str) or not spec:
                    continue
                # Already pinned? Scoped pkgs have @ at index 0; a second @
                # past index 0 means a version tag is present. Bare pkgs are
                # rare in our install path but the same logic applies.
                last_at = spec.rfind("@")
                if (spec.startswith("@") and last_at > 0) or (not spec.startswith("@") and last_at >= 0):
                    continue
                resolved_name = rec.get("resolvedName") or spec
                resolved_version = rec.get("resolvedVersion")
                if not isinstance(resolved_version, str) or not resolved_version:
                    # No resolved version to pin to. Skip — re-running install
                    # without a version would auto-pin to OC version (which
                    # might silently upgrade), and that's not the job here.
                    continue
                try:
                    # allow_unlisted: `resolved_name` comes from OC's OWN
                    # install records, not from a repo constant — this plugin
                    # is already present and we are re-pinning its spec to the
                    # version it already resolved to, so no new code is
                    # fetched. The Layer 1 provenance gate warns instead of
                    # refusing here; refusing would break a working path
                    # (design §4, "warn-not-refuse on re-pin").
                    ok, err = _reinst(
                        bot_user, resolved_name,
                        version=resolved_version, force=True,
                        allow_unlisted=True,
                    )
                    if ok:
                        repinned_oks.append(plugin_id)
                    else:
                        repin_failures.append(
                            f"{plugin_id} ({resolved_name}@{resolved_version}): {err}"
                        )
                except Exception as exc:
                    repin_failures.append(f"{plugin_id} raised: {exc}")
        if repinned_oks:
            print(
                f"[evolve/deploy] {bot_id}: re-pinned "
                f"{len(repinned_oks)} plugin spec(s) "
                f"({', '.join(sorted(repinned_oks))})"
            )
        for fail_msg in repin_failures:
            print(f"[evolve/deploy] {bot_id}: re-pin failed: {fail_msg}")
    except ImportError:
        pass  # helpers unavailable — reconciliation skipped, audit will re-flag

    # 7c. Audit-driven re-pin for post-migration bots.
    #
    # OC v2026.5.28 introduced a migration that moves the legacy
    # ``~/.openclaw/plugins/installs.json`` to ``installs.json.migrated``
    # and serves install records from a new in-memory registry. The
    # file-based reconciler above can't see anything once that migration
    # has run (its read of installs.json returns None) — and even when
    # we fall back to reading installs.json.migrated, the records there
    # look pinned to operators (``spec=@openclaw/codex@2026.5.18``) while
    # OC's live audit sees the in-memory state as unpinned. The 2026-06-06
    # test pod hit this on 5 bots that had codex installed pre-migration:
    # the audit kept firing ``plugins.installs_unpinned_npm_specs`` even
    # though the file-based view said codex was already pinned.
    #
    # The fix: drive the re-pin from OC's own audit output. Run
    # ``openclaw security audit --deep --json``, parse the unpinned
    # finding's detail string for plugin ids, cross-reference with
    # ``openclaw plugins list --json`` to learn each one's live version,
    # then re-install with ``--force`` to write a fresh install record
    # matching the live runtime.
    #
    # Best-effort post-migration re-pin (only when installs.json is absent).
    # The .exists() gate is INSIDE the try: evolve can lose ACL traverse on
    # the bot's 0700 .openclaw mid-deploy (gateway re-chmod resets the ACL mask
    # to ---), and Path.exists() RAISES EACCES on Py>=3.12 (<=3.11 was False).
    try:
        if not (_user_home(bot_user) / ".openclaw/plugins/installs.json").exists():
            ok_ids, fail_msgs = _repin_unpinned_via_audit(bot_id, bot_user)
            if ok_ids:
                print(
                    f"[evolve/deploy] {bot_id}: audit-driven re-pin of "
                    f"{len(ok_ids)} plugin(s) ({', '.join(sorted(ok_ids))})"
                )
            for fail_msg in fail_msgs:
                print(
                    f"[evolve/deploy] {bot_id}: audit-driven re-pin failed: "
                    f"{fail_msg}"
                )
    except Exception as exc:  # noqa: BLE001 — best-effort hygiene; also swallows .exists() EACCES under a clamped .openclaw (Py>=3.12)
        print(
            f"[evolve/deploy] {bot_id}: audit-driven re-pin skipped: "
            f"{type(exc).__name__}: {exc}"
        )

    # ── Cost settings ────────────────────────────────────────────────────────────
    # See `gap_fill_cost_settings` (module-level) for the gap-fill contract
    # and heartbeat-subfield semantics.
    snapshot: dict[str, Any] = {}
    try:
        snap_file = Path(shared_dir) / "cost-settings" / f"{bot_id}.json"
        if snap_file.exists():
            snapshot = json.loads(snap_file.read_text()).get("settings", {})
    except Exception:
        pass
    if gap_fill_cost_settings(cfg, snapshot):
        changed = True

    # ── Session scoping ─────────────────────────────────────────────────────
    # session.dmScope = "per-channel-peer" makes OC create one session per
    # (channel, peer) — so a Telegram DM session's sessionKey is shaped
    # "agent:<id>:telegram:direct:<chatId>" with the chat ID at the trailing
    # numeric segment. The Evolve plugin's evo-keyword path direct-sends the
    # rec via the Telegram Bot API by extracting that chatId; without
    # per-channel-peer, sessionKey collapses to "agent:<id>:main" and direct
    # send has nothing to dial. Diagnosed 2026-05-09: personal_bot was missing this
    # field and "evo" silently fell back to LLM-echo (which is unreliable
    # per PR #666). Gap-fill brings any bot that lacks dmScope into the
    # working shape. Only fills missing — never overwrites a deliberate
    # value (e.g. a future bot that wants "shared" or some other scope).
    session_cfg = cfg.setdefault("session", {})
    if "dmScope" not in session_cfg:
        session_cfg["dmScope"] = "per-channel-peer"
        changed = True
    if "reset" not in session_cfg:
        session_cfg["reset"] = {"idleMinutes": 120}
        changed = True

    # ── OC logger output path ───────────────────────────────────────────────
    # Without ``logging.file`` set, OC falls through to console-only output
    # and launchd's StandardOut/Err in the gateway plist capture every log
    # line into ``gateway.log`` / ``gateway.err.log`` unbounded — 4 bots
    # crossed 100M each on the test pod as of 2026-06. Gap-fill points OC
    # at its own bounded path (25MB × 5 archives = 125MB ceiling per bot);
    # the launchd-stdout remainder is then small enough that the
    # ai.evolve.evolve.oc-log-rotate daily cron's 10MB threshold catches
    # any drift. Uses bot_user (already resolved via _bot_user_for above)
    # so the path is correct even when bot_id ≠ macOS account name.
    logging_cfg = cfg.setdefault("logging", {})
    expected_log_file = str(_user_home(bot_user) / ".openclaw/logs/openclaw.log")
    if logging_cfg.get("file") != expected_log_file:
        logging_cfg["file"] = expected_log_file
        changed = True
    if "maxFileBytes" not in logging_cfg:
        logging_cfg["maxFileBytes"] = 26214400  # 25 MiB
        changed = True

    # ── Provider-registry sync (re-introduced 2026-06-03 evening) ───────────
    #
    # PRIOR HISTORY — read before touching this block.
    #
    # A morning attempt (PR #2019, 01:34 PT) introduced
    # ``_reconcile_provider_models_registry`` to derive
    # ``models.providers[<provider>].models[]`` from ``agents.defaults.models``
    # keys. It produced a doubled-prefix bug on OpenClaw v2026.5.28:
    #
    #   * defaults keys are ``"<provider>/<model-id>"`` (prefixed)
    #   * the reconciler stored ``id="<model-id>"`` (unprefixed) — correct
    #     per OC's documented schema
    #   * BUT OC v2026.5.28's resolver double-prefixed at lookup time,
    #     producing ``"<provider>/<provider>/<model-id>"`` → model_not_found
    #
    # The double-prefix was an OC bug, NOT an Evolve bug; the unprefixed
    # ``id`` shape is what OC's schema actually requires. PR #2025 reverted
    # the writer and added regression guards in
    # ``tests/test_deploy_provider_models_registry.py``, working with the
    # information available at the time (OC was broken; writer looked
    # wrong because it produced bad behavior).
    #
    # WHY THIS IS BACK — see openclaw upstream:
    #   * https://github.com/openclaw/openclaw/issues/88560 (filed by us)
    #   * https://github.com/openclaw/openclaw/pull/88587 (merged 2026-05-31)
    #   * OpenClaw v2026.6.1 released 2026-06-03 19:35 UTC contains the fix
    #
    # On v2026.6.1, OC's normalizer correctly handles unprefixed ``id``
    # entries and the registry-gate is strictly required — bots whose
    # ``models.providers`` is empty now hit
    # ``FailoverError: Unknown model: <provider>/<model-id>`` (single
    # prefix, no longer the doubled form) on every routing decision that
    # references a catalog model. Validated 2026-06-03 17:38 PT: two
    # consecutive haiku heartbeat routes resolved cleanly with the fix
    # applied; zero schema or FailoverError events across all six
    # affected bots since.
    #
    # The new helper is structurally distinct from the deleted reconciler:
    #   * Lives in ``oc_model.py`` (the canonical write surface), not
    #     duplicated in deploy.py
    #   * Called transitively from ``oc_model.set_catalog`` so every
    #     catalog write maintains both layers in sync
    #   * Skips providers OC does NOT bundle (e.g. ``runway``) since
    #     those require ``baseUrl`` we can't synthesize — caught
    #     empirically when the pre-write OC validator rejected a
    #     ``runway`` overlay
    #   * Pre-write validation now lives in ``safe_write_bot_config``
    #     AND ``_kickstart_gateway_and_wait`` — any schema-invalid
    #     write is rejected before disk
    #
    # The original regression guards in
    # ``tests/test_deploy_provider_models_registry.py`` are updated to
    # match: they still forbid the OLD function name and the
    # ``partition("/")``-near-providers shape, but allow the new helper
    # via name + the OC-version-acknowledgment comment block above.
    try:
        from oc_model import sync_provider_models_from_catalog  # type: ignore
        before_models = json.dumps(cfg.get("models", {}), sort_keys=True)
        sync_provider_models_from_catalog(cfg)
        after_models = json.dumps(cfg.get("models", {}), sort_keys=True)
        if before_models != after_models:
            changed = True
    except ImportError:
        # Analyzer package not importable from this deploy context — log and
        # skip. The reconcile path through ``oc_model.set_catalog`` (which
        # calls the helper transitively) will still backfill on the next
        # tier write.
        print(f"[evolve/deploy] {bot_id}: provider-registry sync skipped (oc_model unavailable)")

    # ── Tier→openclaw propagation pass ─────────────────────────────────────
    # evolve-tiers.json is the source of truth for primary/fallbacks. The
    # ``json_full_config_set`` codepath in oc_model.py rewrites
    # openclaw.json's ``agents.defaults.model`` ONLY when the operator
    # changes tiers via AI Optimization. Bots whose primary was seeded
    # by an older codepath (pre-tier-derivation, or via a one-off
    # ``openclaw config set``) can drift: primary present, fallbacks
    # empty, neither matching the tier cascade — observed across multiple
    # bots on the reference pod as of 2026-06-04 (primary set to
    # ``anthropic/claude-sonnet-4-6`` but ``fallbacks`` empty even
    # though tier2 lists both sonnet and gemini-2.5-pro).
    #
    # This pass closes the gap by recomputing (primary, fallbacks) from
    # the tier file on every deploy. Idempotent — no-op when the
    # existing config already matches what the tiers would produce.
    # Silently skipped on bots whose evolve-tiers.json is missing or
    # has no tier definitions yet (fresh installs before the operator
    # has visited the AI Optimization page).
    try:
        from oc_model import compute_primary_from_tiers_file  # type: ignore
        tiers_path = _user_home(bot_user) / ".openclaw/evolve-tiers.json"
        derived = compute_primary_from_tiers_file(tiers_path, role=role)
        if derived is not None:
            derived_primary, derived_fallbacks = derived
            agent_defaults = cfg.setdefault("agents", {}).setdefault("defaults", {})
            model_cfg = agent_defaults.setdefault("model", {})
            current_primary = model_cfg.get("primary")
            current_fallbacks = model_cfg.get("fallbacks") or []
            if (
                current_primary != derived_primary
                or list(current_fallbacks) != derived_fallbacks
            ):
                model_cfg["primary"] = derived_primary
                model_cfg["fallbacks"] = derived_fallbacks
                changed = True
                print(
                    f"[evolve/deploy] {bot_id}: tier→openclaw propagation: "
                    f"primary={derived_primary}, "
                    f"fallbacks=[{', '.join(derived_fallbacks)}] "
                    f"(was primary={current_primary!r}, "
                    f"fallbacks={list(current_fallbacks)!r})"
                )
    except ImportError:
        pass

    # ── Policy-slice override pass (Phase 3d) ───────────────────────────────
    # Apply per-bot Policy overrides from
    # ``{shared_dir}/sandbox/overrides/<bot_id>.json`` to permission-posture
    # fields (tools.*, commands.*). This runs LAST among the repair passes
    # so the override supersedes anything earlier inference (_infer_exec_policy,
    # _apply_permission_baseline_gap_fills) wrote — the layering rule from
    # docs/spec-openclaw-json-derived-artifact-2026-05-24.md §7:
    #
    #   schema_default < pod_default < inference < per_bot_override
    #
    # The slice is restricted to OPENCLAW schema entries OUTSIDE the
    # evolve plugin block (those are owned by ``materialize_evolve_plugin_config``
    # earlier in this function). Idempotent on bots with no overrides.
    from .openclaw_materializer import materialize_openclaw_policy_slice
    policy_slice_before = json.dumps(cfg, sort_keys=True)
    slice_result = materialize_openclaw_policy_slice(
        cfg, bot_id, Path(network.get("sharedDir") or _DEFAULT_SHARED_DIR),
    )
    if json.dumps(cfg, sort_keys=True) != policy_slice_before:
        changed = True
    if slice_result.applied_dotpaths:
        print(
            f"[evolve/deploy] {bot_id}: applied policy-slice overrides: "
            f"{', '.join(slice_result.applied_dotpaths)}"
        )

    # ── Autonomy posture render (U4.1) ──────────────────────────────────────
    # Merge the per-integration autonomy deny slice from
    # {shared_dir}/bots/<bot>/autonomy.json into the config (spec
    # docs/spec-autonomy-ladder-2026-06-10.md §2.3 — renderer runs on
    # every posture write AND every deploy, like the Slack policy
    # writer, so posture enforcement survives redeploys by
    # construction). Runs after the policy-slice pass: the posture
    # render owns only its mcp__<server>__ prefixed tools.deny entries
    # and preserves everything else. Lazy import + broad catch — a
    # posture-render failure must not block a deploy; the coherence
    # check surfaces the gap as autonomy_posture_drift instead.
    try:
        from autonomy.renderer import merge_autonomy_into_config
        if merge_autonomy_into_config(
            cfg, bot_id, Path(network.get("sharedDir") or _DEFAULT_SHARED_DIR),
        ):
            changed = True
            print(f"[evolve/deploy] {bot_id}: applied autonomy posture deny slice")
    except Exception as e:  # noqa: BLE001 — deploy must not fail on posture render
        print(f"[evolve/deploy] {bot_id}: [warn] autonomy posture render failed: {e}")

    if not changed:
        return

    # Use safe_write_bot_config to validate before writing
    ok, err = safe_write_bot_config(bot_id, cfg, reason="ensure_plugin_config", bot_user=bot_user)
    if not ok:
        raise RuntimeError(f"ensure_plugin_config failed for {bot_id}: {err}")


def _clear_stale_plugin_install(bot_id: str, bot_user: str) -> None:
    """Force a clean plugin reinstall when the installed schema differs from source.

    ``openclaw plugins install`` pre-validates the existing openclaw.json against
    the INSTALLED plugin's schema, not the source. When the source schema has
    gained fields since the bot's last successful install — and
    ensure_plugin_config has just written those fields — validation fails with
    "must NOT have additional properties" and the install aborts. The plugin's
    ``version`` string in package.json is not bumped per-schema-change, so a
    version match is not enough; we compare the manifest file content directly.

    Removing the stale install (both the extensions/evolve dir and the
    plugins.installs.evolve registry entry in openclaw.json) lets plugins install
    proceed against the fresh source schema.

    No-op when the installed manifest matches source. Idempotent. Requires root.
    """
    src_manifest = PLUGIN_INSTALL_DIR / "openclaw.plugin.json"
    inst_dir = _user_home(bot_user) / ".openclaw/extensions/evolve"
    inst_manifest = inst_dir / "openclaw.plugin.json"

    try:
        src_text = src_manifest.read_text()
    except OSError:
        return  # no source manifest — nothing to compare against

    inst_exists = _secret_perms.exists_or_unreachable(inst_dir)  # EACCES → assume present; the oc_json read below early-returns under a clamp before any rm
    if inst_exists:
        try:
            inst_text = inst_manifest.read_text()
        except OSError:
            inst_text = ""
        if inst_text == src_text:
            return  # installed schema matches source — nothing stale

    # Either install dir is missing, or its manifest differs from source.
    oc_json_path = _user_home(bot_user) / ".openclaw/openclaw.json"
    try:
        cfg = json.loads(oc_json_path.read_text())
    except (OSError, json.JSONDecodeError):
        return

    installs = cfg.get("plugins", {}).get("installs", {})
    has_registry = "evolve" in installs
    if not inst_exists and not has_registry:
        return  # already clean

    if inst_exists:
        subprocess.run(
            ["sudo", "/bin/rm", "-rf", str(inst_dir)],
            capture_output=True, timeout=10,
        )

    if has_registry:
        if why := sudo_dest_refusal(oc_json_path):  # D-2: bot-owned dest of a root cp + chown
            print(f"[evolve/deploy] {bot_id}: refusing plugin-install cleanup write — {why}")
            return
        installs.pop("evolve", None)
        tmp = _secure_stage(json.dumps(cfg, indent=2))
        try:
            subprocess.run(
                ["sudo", "/bin/cp", str(tmp), str(oc_json_path)],
                check=True, capture_output=True, timeout=5,
            )
            if why := sudo_dest_refusal(oc_json_path):  # re-assert between cp and chown
                print(f"[evolve/deploy] {bot_id}: refusing plugin-install cleanup chown — {why}")
                return
            subprocess.run(
                ["sudo", _PROFILE.chown, f"{bot_user}:staff", str(oc_json_path)],
                capture_output=True, timeout=5,
            )
            _secret_perms.chmod_secret_config(oc_json_path)  # 0600: token-bearing
        finally:
            tmp.unlink(missing_ok=True)

    print(f"[evolve/deploy] {bot_id}: cleared stale evolve plugin install before reinstall")


# Upstream OC stderr substring emitted when the CLI binary was upgraded ahead
# of the still-running per-bot gateway daemon. The CLI refuses to read its own
# config in that state, so neither --version nor `gateway status --deep` parse
# — i.e. exactly the condition this preflight exists to catch.
_OC_CONFIG_DRIFT_STDERR = "config changed since last load"

# Internal sentinel injected into the gateway-version-read stderr when the
# `openclaw gateway status --deep` subprocess itself times out (i.e. the
# gateway process is alive enough to hold the port but not responding to
# the status RPC — the "wedged" failure mode). Distinguishes wedged from
# absent/unparseable so the preflight can kickstart instead of silently
# skipping and letting doctor --fix walk into the same wedged gateway.
# Not a real OC stderr string; chosen so substring checks can't collide
# with anything the OC binary would print.
_OC_GATEWAY_WEDGED_MARKER = "__evolve_gateway_wedged__"


def _read_oc_cli_version(bot_user: str, bot_home: str) -> tuple[str | None, str]:
    """Return ``(canonical YYYY.M.PATCH | None, stderr)`` for the OC CLI.

    Stderr is returned alongside the parsed version so the preflight can
    escalate on the specific ``config changed since last load`` failure mode
    instead of treating every read-failure as a silent skip.
    """
    from .upstream_version import canonical_version

    try:
        r = subprocess.run(
            ["sudo", "-H", "-u", bot_user, _openclaw_bin(), "--version"],
            capture_output=True, text=True, timeout=10, cwd=bot_home,
        )
    except Exception:
        return None, ""
    stderr = r.stderr or ""
    if r.returncode != 0 or not r.stdout:
        return None, stderr
    first_line = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    return canonical_version(first_line), stderr


def _read_oc_gateway_version(bot_user: str, bot_home: str) -> tuple[str | None, str]:
    """Return ``(canonical YYYY.M.PATCH | None, stderr)`` for the running gateway.

    Parses ``openclaw gateway status --deep`` for a ``Gateway version: X.Y.Z``
    line. Returns ``(None, stderr)`` when the gateway isn't running, the output
    shape changed, or the version can't be parsed — callers treat that as
    "skip the check" unless stderr matches the known config-drift signature.

    When the status subprocess itself times out, we surface that distinctly
    via ``_OC_GATEWAY_WEDGED_MARKER`` so the preflight can kickstart instead
    of silently skipping — the wedged case is what was leaving doctor --fix
    to hang for 60s on its own retry (team_bot_a/team_bot_c/admin_bot/security_bot/team_bot_b/evolve
    2026-05-29 deploy).
    """
    from .upstream_version import canonical_version

    try:
        r = subprocess.run(
            ["sudo", "-H", "-u", bot_user, _openclaw_bin(), "gateway", "status", "--deep"],
            capture_output=True, text=True, timeout=10, cwd=bot_home,
        )
    except subprocess.TimeoutExpired:
        return None, _OC_GATEWAY_WEDGED_MARKER
    except Exception:
        return None, ""
    stderr = r.stderr or ""
    # Non-zero typically means "gateway not running" — skip the check rather
    # than block deploy on a missing daemon.
    for line in (r.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("gateway version:"):
            raw = stripped.split(":", 1)[1].strip()
            return canonical_version(raw), stderr
    return None, stderr


def _kickstart_gateway_and_wait(
    bot_id: str,
    bot_user: str,
    bot_home: str,
    *,
    expected_cli_ver: str | None,
    timeout_seconds: int = 30,
    skip_preflight_validate: bool = False,
) -> tuple[bool, str]:
    """Kickstart a bot's gateway and poll until it reports the new OC version.

    Returns (success, message). Success means: the gateway came back up AND
    its reported version matches the CLI version. Failure: either launchctl
    couldn't restart it OR the gateway didn't catch up within the timeout.

    The poll interval is intentionally short (1s) — gateway boot is typically
    5–10s on the mini, and we'd rather notice the mismatch quickly than wait.

    Preflight validation
    --------------------
    Before issuing the kickstart, the bot's openclaw.json is run through
    OC's own schema validator (``openclaw config validate --json``). If
    it fails, we refuse to kickstart — a kickstart against an invalid
    config produces a crash-loop at ~5s intervals (the failure mode that
    bricked six bots simultaneously when a hand-rolled backfill wrote
    schema-invalid entries to ``models.providers``). The validator runs
    in <2s on a typical config; the cost is negligible vs the recovery
    cost of a crash-loop.

    Pass ``skip_preflight_validate=True`` only when the caller has
    already validated (e.g. via ``safe_write_bot_config`` which runs
    its own validation pre-write), or in the rare recovery scenario
    where the config is known-bad but the operator wants to kickstart
    anyway to surface the OC error message.
    """
    import time
    label = per_bot_gateway_plist_label(bot_id)

    if not skip_preflight_validate:
        try:
            from .openclaw_config_validator import validate_bot_openclaw_json
            from .config import load_network
            network = load_network()
            valid, issues, val_err = validate_bot_openclaw_json(bot_id, network)
            if val_err:
                # Validator couldn't run — log and proceed (don't block on
                # the validator itself being broken). Matches the
                # validate_all_bots policy in openclaw_config_validator.py.
                print(
                    f"[evolve/deploy] {bot_id}: pre-kickstart validation "
                    f"skipped — validator error: {val_err}"
                )
            elif not valid:
                issue_str = "; ".join(
                    f"{it.get('path')}: {it.get('message')}" for it in issues[:5]
                )
                more = f" (+{len(issues)-5} more)" if len(issues) > 5 else ""
                return False, (
                    f"pre-kickstart schema validation FAILED for {bot_id}; "
                    f"refusing to kickstart (crash-loop would result). "
                    f"Issues: {issue_str}{more}. Inspect and fix "
                    f"{_user_home(bot_user) / '.openclaw/openclaw.json'}, or restore "
                    f"openclaw.json.bak, then retry."
                )
        except Exception as exc:
            # Preflight is best-effort — never break the kickstart path
            # because of an unexpected validator failure. Log and proceed.
            print(f"[evolve/deploy] {bot_id}: pre-kickstart validation raised: {exc}")

    # Fire the kickstart. restart() issues `launchctl kickstart -k`, which
    # kills the running process first if needed, and hands back the CLI
    # output so callers get a clean error message for the deploy log.
    restarted, restart_out = get_scheduler().restart(label)
    if not restarted:
        return False, (
            f"launchctl kickstart -k system/{label} failed: {restart_out}"
        )

    # Poll for the gateway to come back. We can't tell EXACTLY when it's
    # ready, but `openclaw gateway status` will return a parseable version
    # once it's responsive again. Be patient — gateway boot can take ~10s.
    deadline = time.time() + timeout_seconds
    last_gw_ver: str | None = None
    while time.time() < deadline:
        time.sleep(1)
        gw_ver, gw_err = _read_oc_gateway_version(bot_user, bot_home)
        if _OC_CONFIG_DRIFT_STDERR in gw_err:
            # Gateway is reporting drift — it hasn't fully reloaded yet.
            continue
        if gw_ver is None:
            # Still booting (status command returned empty / non-parsable).
            continue
        last_gw_ver = gw_ver
        # If we know the CLI version, gate on match. Otherwise any
        # parseable response means the gateway is back up.
        if expected_cli_ver is None or gw_ver == expected_cli_ver:
            return True, f"gateway back up at version {gw_ver}"

    if last_gw_ver and expected_cli_ver and last_gw_ver != expected_cli_ver:
        return False, (
            f"gateway came back at version {last_gw_ver} but CLI is "
            f"{expected_cli_ver} — kickstart didn't pick up the new binary "
            f"(brew/auto-updater state odd?)"
        )
    return False, f"gateway did not respond within {timeout_seconds}s after kickstart"


def _preflight_oc_version_match(bot_id: str, bot_user: str, bot_home: str) -> None:
    """Ensure the OC CLI and running gateway versions match — auto-kickstart
    on mismatch.

    A partial brew/auto-updater cycle can leave the CLI ahead of the daemon
    gateway; subsequent ``openclaw plugins install`` then fails with an opaque
    "config changed since last load" error. Earlier this function raised a
    "restart the gateway and re-run deploy" RuntimeError. The user just told
    the operator "redeploy this bot"; making them re-run after a manual
    kickstart is workflow friction — the deploy itself is destructive enough
    that auto-kickstarting the gateway is an obvious next step.

    New behavior:
      1. Detect mismatch (versions differ, OR either subprocess emitted
         ``config changed since last load``).
      2. Auto-kickstart the gateway and poll for it to come back at the
         new version (up to 30s).
      3. If it comes back matching → continue the deploy silently.
      4. If kickstart fails / gateway doesn't catch up → raise a clear
         error pointing the operator at the underlying state.

    Silent skip when either version is unreadable for benign reasons —
    gateway not running, CLI broken, output-shape change.
    """
    cli_ver, cli_err = _read_oc_cli_version(bot_user, bot_home)
    gw_ver, gw_err = _read_oc_gateway_version(bot_user, bot_home)
    label = per_bot_gateway_plist_label(bot_id)

    # ── Detect mismatch ────────────────────────────────────────────────────
    # Three paths land here:
    #   (a) `config changed since last load` on stderr from either CLI or
    #       gateway-status — that's OC explicitly telling us the daemon's
    #       loaded config diverges from disk.
    #   (b) Parseable versions on both sides that differ — typical of an
    #       OC binary upgrade ahead of a gateway restart.
    #   (c) The gateway-status subprocess itself timed out — gateway is
    #       wedged (process alive, holding the port, but not answering
    #       the status RPC). Without kickstarting here, doctor --fix and
    #       plugins install both walk into the same wedged gateway and
    #       hang for their own timeouts — observed on 6/8 bots in the
    #       2026-05-29 deploy --all (team_bot_a/team_bot_c/admin_bot/security_bot/team_bot_b/evolve).
    config_drift = (
        _OC_CONFIG_DRIFT_STDERR in cli_err
        or _OC_CONFIG_DRIFT_STDERR in gw_err
    )
    version_drift = (
        cli_ver is not None and gw_ver is not None and cli_ver != gw_ver
    )
    gateway_wedged = _OC_GATEWAY_WEDGED_MARKER in gw_err

    if not (config_drift or version_drift or gateway_wedged):
        # Either healthy or unreadable for benign reasons — leave the
        # original "log and skip" path intact.
        if cli_ver is None and not config_drift:
            print(f"[evolve/deploy] {bot_id}: could not read OC CLI version; skipping version-match check")
        elif gw_ver is None and not config_drift:
            print(f"[evolve/deploy] {bot_id}: could not read OC gateway version; skipping version-match check")
        return

    # ── Auto-kickstart and re-verify ───────────────────────────────────────
    if config_drift:
        cause = "config changed since last load"
    elif gateway_wedged:
        cause = "gateway status RPC timed out — daemon wedged"
    else:
        cause = f"CLI {cli_ver} ≠ gateway {gw_ver}"
    print(
        f"[evolve/deploy] {bot_id}: OC version drift detected ({cause}); "
        f"auto-kickstarting gateway and retrying..."
    )
    ok, msg = _kickstart_gateway_and_wait(
        bot_id, bot_user, bot_home,
        expected_cli_ver=cli_ver,
    )
    if not ok:
        # The kickstart itself failed, or the gateway didn't catch up.
        # Surface the underlying problem rather than letting the deploy
        # proceed into the cryptic OC error chain.
        raise RuntimeError(
            f"OC version drift on bot {bot_id} ({cause}); auto-kickstart "
            f"failed: {msg}. Manual fix: "
            f"sudo launchctl kickstart -k system/{label}"
        )
    print(f"[evolve/deploy] {bot_id}: {msg}")


def install_oc_plugin(bot_id: str, port: int | None = None, network: dict | None = None) -> None:
    """Step 4b: Install the OC plugin into the bot's OpenClaw instance.

    Tries with a 60s timeout. If the install command times out (which can happen
    with Team_bot_a), waits 5s then checks the /evolve/status endpoint. If the plugin
    is already live the timeout was harmless and we return normally.

    Pass ``network`` so the evolve plugin config is guaranteed to be present
    immediately before ``plugins install`` runs — even if doctor --fix stripped it.
    """
    import time
    import urllib.request

    bot_user = _bot_user_for(bot_id, network)
    bot_home = str(_user_home(bot_user))

    # Fail fast on CLI/gateway version drift — `plugins install` and `doctor --fix`
    # both fail opaquely when the CLI is ahead of the running gateway daemon.
    _preflight_oc_version_match(bot_id, bot_user, bot_home)

    # If the installed plugin's schema differs from source, force a clean reinstall.
    # Without this, plugins install pre-validates against the stale installed schema
    # and rejects fields ensure_plugin_config has just written for the new schema.
    _clear_stale_plugin_install(bot_id, bot_user)

    # Doctor --fix is no longer invoked here.
    #
    # The history: doctor was added before plugins install to clear stale OC
    # config so the install schema-validation wouldn't reject our writes.
    # In practice the only deploy-critical part of that work — clearing the
    # stale evolve plugin install when the manifest schema changed — is
    # handled by _clear_stale_plugin_install above. The rest of doctor's
    # work (model-ref migrations, cron-payload upgrades, orphan reports,
    # security warnings) is maintenance, not a deploy precondition.
    #
    # The reason for the removal: on the 2026-05-29 / 30 `deploy --all`,
    # doctor --fix hit its 60s and later 120s timeouts on 6 of 8 bots. The
    # hang only manifested inside deploy.py's subprocess wrapper — I could
    # never reproduce it manually under the same invocation, even as the
    # same evolve user with the same cwd and capture_output flags (manual
    # runs consistently completed in 12-15s). Rather than keep chasing
    # the discrepancy and burning deploy time, doctor --fix now runs as a
    # nightly per-bot launchd job (_install_launchd_doctor_pass below).
    # Operators who need an immediate doctor pass can run
    # `sudo evolve-admin doctor-pass --bot <id>` (or --all).
    #
    # Schema-validate the result of _clear_stale_plugin_install before
    # proceeding into `plugins install` — this is what doctor's
    # post-`config validate --json` step used to gate on. Best-effort:
    # the install will produce its own validation errors if this misses
    # something.
    try:
        val = subprocess.run(
            ["sudo", "-H", "-u", bot_user,
             _openclaw_bin(), "config", "validate", "--json"],
            capture_output=True, text=True, timeout=15,
            cwd=bot_home,
        )
        try:
            vresult = json.loads(val.stdout)
            if not vresult.get("valid", True):
                issues = vresult.get("issues", [])
                for issue in issues:
                    print(f"[evolve/deploy] {bot_id}: pre-install config issue: "
                          f"{issue.get('path')}: {issue.get('message')}")
        except json.JSONDecodeError:
            pass  # validation output not parseable — proceed
    except Exception:
        pass  # validation is best-effort

    # Content gate: refuse to install at all unless the deployed
    # /Users/Shared/evolve-plugin/openclaw.plugin.json carries stamped
    # x-evolve-trust digests matching a fresh canonical hash of the install
    # tree. OC installs this plugin from a local path and vouches for the
    # path, not its content — so this is the only content check anywhere on
    # the install path. Closes the gap PR #2293's unconditional bypass left
    # open. See docs/spec-plugin-install-trust-2026-06-06.md §4.
    #
    # Deliberately NOT coupled to any OC flag or scanner state. The old
    # "verify, then pass --dangerously-force-unsafe-install" framing was never
    # real enforcement — withholding a flag OC ignores enforces nothing. Flag
    # dropped 2026-08; this gate never depended on it (spec §4).
    ok, msg = verify_plugin_signature(PLUGIN_INSTALL_DIR)
    if not ok:
        raise RuntimeError(
            f"refusing to install evolve plugin into {bot_id}: "
            f"signature verification failed — {msg}. "
            f"Recovery: re-run `sudo evolve-admin upgrade` to rebuild the "
            f"plugin and re-stamp the manifest. If the rebuild still doesn't "
            f"clear it, investigate what wrote to {PLUGIN_INSTALL_DIR} since "
            f"the last build — the message above names which part of the tree."
        )
    if msg:
        # Verification passed with something to say — today, the pre-treeDigest
        # stamp during the staged rollout (plugin_signature.REQUIRE_TREE_DIGEST).
        # Caveat recorded rather than fixed: a bare print misses the web Upgrade
        # job's _job_log and the add-bot wizard — the two paths that install
        # without rebuilding. Durable answer is spec §6.3's monitor finding.
        print(f"[evolve/deploy] {bot_id}: plugin signature warning — {msg}")

    cmd = [
        "sudo", "-H", "-u", bot_user,
        _openclaw_bin(), "plugins", "install",
        "-l", str(PLUGIN_INSTALL_DIR),
    ]
    try:
        run_cmd(cmd, cwd=bot_home, timeout=60)
    except subprocess.TimeoutExpired:
        pass  # may have timed out but still worked
    except Exception:
        raise

    # Plugin install rewrites openclaw.json — re-inject our config (model, evolve plugin)
    # so nothing that plugins install touched survives as a regression.
    if network is not None:
        try:
            ensure_plugin_config(bot_id, network)
        except Exception as e:
            print(f"[evolve/deploy] {bot_id}: could not re-inject config after plugins install: {e}")

    # Poll until the plugin endpoint responds or we give up (30s window, 3s between attempts).
    if port:
        url = f"http://localhost:{port}/evolve/status"
        for _ in range(10):
            time.sleep(3)
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    data = json.loads(r.read())
                    if data.get("bot_id") == bot_id:
                        return  # plugin is live
            except Exception:
                pass
    return  # best-effort — caller verifies if needed


def safe_write_bot_config(
    bot_id: str,
    new_config: dict,
    reason: str = "evolve config update",
    bot_user: str | None = None,
) -> tuple[bool, str]:
    """
    CRITICAL SAFETY FUNCTION: Safely write a new openclaw.json for a bot.

    This function exists because writing an invalid key to openclaw.json will
    crash the bot's gateway in a crash-loop. Always use this instead of
    direct sudo cp when modifying bot configs.

    Safety steps:
    1. Write new config to /tmp staging file
    2. Validate against OC schema using OPENCLAW_CONFIG_PATH env override
    3. If invalid: abort entirely — do NOT touch the live config
    4. If valid: backup live config to openclaw.json.bak, then copy staged file

    The .bak file provides a manual recovery path if a delayed-effect bug
    slips through validation (e.g. a key that validates but breaks at runtime).
    Recovery: restore openclaw.json.bak over openclaw.json, then restart the gateway.

    Returns (success, error_message).
    """
    bot_user = bot_user or _bot_user_for(bot_id)
    config_path = _user_home(bot_user) / ".openclaw/openclaw.json"
    # mode 0644 so the bot-user `openclaw config validate` subprocess can read
    # the staged config via OPENCLAW_CONFIG_PATH. Random name + O_EXCL still
    # kills the predictable-/tmp symlink race that a fixed name is open to.
    tmp_path = _secure_stage(json.dumps(new_config, indent=2), mode=0o644)

    try:
        # Validate against OC schema (does not touch live config).
        # OPENCLAW_CONFIG_PATH points openclaw at the staged temp config
        # instead of the live one. sudo --preserve-env preserves just
        # that one variable, matching the SETENV sudoers grant for
        # evolve → <bot> on the openclaw binary. `-H` sets HOME to
        # bot's home. cwd must be bot's home — bot user can't cd to
        # /Users/Shared.
        val = subprocess.run(
            ["sudo", "--preserve-env=OPENCLAW_CONFIG_PATH", "-H", "-u", bot_user,
             _openclaw_bin(), "config", "validate", "--json"],
            capture_output=True, text=True, timeout=15,
            cwd=str(_user_home(bot_user)),
            env={**os.environ, "OPENCLAW_CONFIG_PATH": str(tmp_path)},
        )
        try:
            result = json.loads(val.stdout)
            if not result.get("valid", False):
                issues = result.get("issues", [])
                issue_str = "; ".join(f"{i.get('path')}: {i.get('message')}" for i in issues)
                return False, f"Config validation FAILED ({reason}): {issue_str}"
        except json.JSONDecodeError:
            # If can't parse validation output, check for error indicators
            combined = val.stdout + val.stderr
            if "invalid" in combined.lower() or val.returncode != 0:
                return False, f"Config validation failed ({reason}): {combined[:200]}"

        # Validation passed — backup existing config then write new one.
        # The `openclaw config validate` above ran as the bot user → re-clamped
        # .openclaw's ACL mask to ---, so a BARE config_path.exists() RAISES
        # PermissionError on modern Python (the live VPS 0-of-N upgrade blocker).
        # The backup + write below run as root, so classify unreachable as present.
        bak_path = config_path.with_suffix(".json.bak")
        # HOISTED: the D-2 gate below lstats AS EVOLVE and that same clamp hides config_path on Linux — un-hoisted it always fails closed.
        get_perms().reassert_mask(config_path.parent)  # re-widen evolve's traverse (Linux; macOS no-op). Harden→reassert coupling for the gateway's own re-clamp.
        if why := sudo_dest_refusal(config_path, bak_path):  # D-2: bak_path is a write sink too, and config_path is the backup's SOURCE (a link there = root READ)
            return False, f"safe_write_bot_config refused ({reason}): {why}"
        if _secret_perms.exists_or_unreachable(config_path):
            subprocess.run(["sudo", "/bin/cp", str(config_path), str(bak_path)], check=False, capture_output=True)
        subprocess.run(["sudo", "/bin/cp", str(tmp_path), str(config_path)], check=True, capture_output=True)
        if why := sudo_dest_refusal(config_path):  # re-assert: a plant landed inside the cp window leaves the LINK for the chown to follow
            return False, f"safe_write_bot_config refused ({reason}): {why}"
        subprocess.run(["sudo", _PROFILE.chown, f"{bot_user}:staff", str(config_path)], check=True, capture_output=True)
        get_perms().reassert_mask(config_path.parent)  # the `openclaw config validate` above ran as the bot user → 0700-clamped .openclaw's ACL mask; re-widen evolve's traverse (Linux; macOS no-op). Harden→reassert coupling for the gateway's own re-clamp. MUST precede chmod_secret_config: that helper's D-2 symlink gate lstats the dest AS EVOLVE and fails closed when it can't, so under the clamp this validate just re-created the 0600 enforcement would be skipped (#3566 audit D-2 follow-up).
        _secret_perms.chmod_secret_config(config_path)  # 0600: token-bearing; bare cp lands 0644
        return True, ""

    except Exception as e:
        return False, f"safe_write_bot_config error: {e}"
    finally:
        tmp_path.unlink(missing_ok=True)


def strip_agents_main(bot_id: str) -> bool:
    """Remove the invalid ``agents.main`` key from a bot's openclaw.json.

    OC's schema does not recognise ``agents.main`` — only ``agents.defaults``.
    Any config that contains it will fail ``openclaw config get`` with
    "Unrecognized key: main", which breaks cron listing and model reads.

    Returns True if the config was repaired (or was already clean), False on error.
    """
    bot_user = _bot_user_for(bot_id)
    oc_json = _user_home(bot_user) / ".openclaw/openclaw.json"
    r = subprocess.run(["sudo", "/bin/cat", str(oc_json)], capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        return False
    try:
        config = json.loads(r.stdout)
    except json.JSONDecodeError:
        return False

    if "main" not in config.get("agents", {}):
        return True  # already clean

    print(f"[deploy] Removing invalid agents.main from {bot_id} openclaw.json")
    config["agents"].pop("main")
    if not config["agents"]:
        config.pop("agents")

    if why := sudo_dest_refusal(oc_json):  # D-2: bot-owned dest of a root cp + chown
        print(f"[deploy] Refusing agents.main repair write for {bot_id}: {why}")
        return False
    tmp = _secure_stage(json.dumps(config, indent=2))
    try:
        r2 = subprocess.run(["sudo", "/bin/cp", str(tmp), str(oc_json)], capture_output=True, text=True)
        if why := sudo_dest_refusal(oc_json):  # re-assert BEFORE the chmod, not just the chown — chmod follows a link too
            print(f"[deploy] Refusing agents.main repair chmod/chown for {bot_id}: {why}")
            return False
        _secret_perms.chmod_secret_config(oc_json)  # 0600: token-bearing
        subprocess.run(["sudo", _PROFILE.chown, f"{bot_user}:staff", str(oc_json)], capture_output=True)
        if r2.returncode != 0:
            print(f"[deploy] Failed to write repaired config for {bot_id}: {r2.stderr}")
            return False
    finally:
        tmp.unlink(missing_ok=True)

    print(f"[deploy] agents.main removed from {bot_id} — restarting gateway")
    # Fire-and-forget, matching the legacy rc-ignored kickstart here.
    get_scheduler().restart(f"ai.openclaw.{bot_id}-gateway")
    return True


def inject_pod_conduct(bot_id: str, bot_user: str | None = None) -> None:
    """Ensure POD_CONDUCT.md is available to a bot each session.

    The file is copied to the bot's workspace so bots can read the full rules
    on demand (referenced from the session_start system-prompt injection).

    NOTE: AGENTS.md reference injection is disabled. Pod conduct is now surfaced
    via session_surface.py → session_start systemAppend on every session, which
    guarantees the rules are actually in context rather than being a pointer the
    bot may or may not follow. See packages/analyzer/session_surface.py.

    This function is idempotent.
    """
    bot_user = bot_user or _bot_user_for(bot_id)
    conduct_src = _CANONICAL_SHARED_DIR / "POD_CONDUCT.md"
    workspace = _user_home(bot_user) / ".openclaw/workspace"
    conduct_dst = workspace / "POD_CONDUCT.md"

    if not conduct_src.exists():
        print(f"[warn] POD_CONDUCT.md not found at {conduct_src} — skipping workspace copy")
        return

    # Repair: remove agents.main if present from a previous broken write
    strip_agents_main(bot_id)

    # Copy POD_CONDUCT.md to bot workspace so bots can read full rules if needed
    if why := sudo_dest_refusal(conduct_dst):  # D-2: bot owns workspace/ — dest of a root cp + chown
        print(f"[warn] {bot_id}: refusing POD_CONDUCT.md workspace copy — {why}")
        return
    subprocess.run(["sudo", "/bin/cp", str(conduct_src), str(conduct_dst)], check=True, capture_output=True)
    if why := sudo_dest_refusal(conduct_dst):  # re-assert between cp and chown
        print(f"[warn] {bot_id}: refusing POD_CONDUCT.md chown — {why}")
        return
    subprocess.run(["sudo", _PROFILE.chown, f"{bot_user}:staff", str(conduct_dst)], check=True, capture_output=True)

    # ── AGENTS.md reference injection — disabled ──────────────────────────────
    # Previously we added "See POD_CONDUCT.md" to AGENTS.md, but this was only
    # a pointer; the bot had no guarantee of reading the content. Replaced by
    # session_surface.py injecting a condensed summary into every system prompt.
    #
    # agents_md = workspace / "AGENTS.md"
    # marker = "POD_CONDUCT.md"
    # reference_line = "## Pod Conduct\nSee POD_CONDUCT.md — pod-wide behavioral rules."
    # try:
    #     r = subprocess.run(["sudo", "/bin/cat", str(agents_md)], capture_output=True, text=True, timeout=5)
    #     if r.returncode == 0 and marker in r.stdout:
    #         return
    #     existing = r.stdout if r.returncode == 0 else ""
    # except Exception:
    #     existing = ""
    # new_content = existing.rstrip() + f"\n\n{reference_line}\n"
    # tmp = Path(f"/tmp/evolve-{bot_id}-agents.md")
    # try:
    #     tmp.write_text(new_content)
    #     subprocess.run(["sudo", "cp", str(tmp), str(agents_md)], check=True, capture_output=True)
    #     subprocess.run(["sudo", _PROFILE.chown, f"{bot_id}:staff", str(agents_md)], check=True, capture_output=True)
    # finally:
    #     tmp.unlink(missing_ok=True)
    # ─────────────────────────────────────────────────────────────────────────


_HANDOFF_MARKER = "handoffs/"
_HANDOFF_INSTRUCTION = (
    "## Session Start Checklist\n"
    "- Check `handoffs/` directory for any pending handoffs from Claude Desktop sessions\n"
    "- Process each handoff in creation order\n"
    "- After processing a handoff, move it to `handoffs/completed/`"
)

_SESSION_SURFACE_MARKER = "session_surface.py"

# Delimited-block markers — wrapping injected sections in HTML comments lets us
# strip + rewrite on every deploy, so changes to the instruction text actually
# reach the bot's AGENTS.md (predicate-drift fix — see PR #312, PR #317).
EVOLVE_HANDOFF_BEGIN = "<!-- evolve-handoff:begin -->"
EVOLVE_HANDOFF_END = "<!-- evolve-handoff:end -->"
EVOLVE_SURFACE_BEGIN = "<!-- evolve-surface:begin -->"
EVOLVE_SURFACE_END = "<!-- evolve-surface:end -->"


def _strip_delimited_block(text: str, begin: str, end: str) -> str:
    """Remove every BEGIN..END delimited block (and adjacent blank lines) from text."""
    import re
    pattern = re.compile(
        r"\n*" + re.escape(begin) + r".*?" + re.escape(end) + r"\n*",
        re.DOTALL,
    )
    return pattern.sub("\n", text)


def _strip_legacy_section(text: str, heading: str) -> str:
    """Strip a section identified by '## <heading>' through end-of-file or next '## '.

    Used to clear legacy un-delimited injections (pre-delimiter format) so they
    don't accumulate alongside the new delimited block.
    """
    out: list[str] = []
    in_section = False
    for line in text.split("\n"):
        if line.startswith("## "):
            in_section = (line.strip() == f"## {heading}")
            if not in_section:
                out.append(line)
        elif not in_section:
            out.append(line)
    return "\n".join(out)


def _build_handoff_agents_md(existing: str) -> str:
    """Pure helper: return AGENTS.md content with a fresh handoff block injected.

    Strips any prior delimited block + legacy "Session Start Checklist" section,
    then appends the current block. Idempotent: f(f(x)) == f(x).
    """
    stripped = _strip_delimited_block(existing, EVOLVE_HANDOFF_BEGIN, EVOLVE_HANDOFF_END)
    stripped = _strip_legacy_section(stripped, "Session Start Checklist").rstrip()
    block = f"{EVOLVE_HANDOFF_BEGIN}\n{_HANDOFF_INSTRUCTION}\n{EVOLVE_HANDOFF_END}"
    return (stripped + "\n\n" + block + "\n") if stripped else (block + "\n")


def _build_surface_agents_md(existing: str) -> str:
    """Pure helper: return AGENTS.md content with any session-surface block removed.

    The block previously injected here ("Pending Approval Tasks", with the line
    "no pending tasks — proceed normally") primed the LLM to regurgitate that
    exact phrase whenever 'evo' came in via the Evolve plugin's out-of-band
    Telegram dispatch — the user got two messages: the rec, then a hallucinated
    "No pending tasks. Something not working as expected with Evolve?" reply.

    The block is also redundant: pending tasks are surfaced via session_surface.py
    stdout into the session_start systemAppend (see TurnObserver.handleSessionStart),
    so the bot does not need an AGENTS.md directive to check for them.

    Strips both the delimited block and any legacy un-delimited section so existing
    bots get cleaned up on re-deploy. Idempotent: f(f(x)) == f(x).
    """
    stripped = _strip_delimited_block(existing, EVOLVE_SURFACE_BEGIN, EVOLVE_SURFACE_END)
    stripped = _strip_legacy_section(stripped, "Pending Approval Tasks").rstrip()
    return stripped + "\n" if stripped else ""


def has_handoff_check(bot_id: str, bot_user: str | None = None) -> bool:
    """Return True if the bot's AGENTS.md contains the handoff check (delimited or legacy)."""
    bot_user = bot_user or _bot_user_for(bot_id)
    agents_md = _user_home(bot_user) / ".openclaw/workspace/AGENTS.md"
    try:
        # Direct read — evolve has ACL read access to .openclaw/ via set_evolve_read_acl.
        # (sudo /bin/cat has no sudoers grant for this path.)
        text = agents_md.read_text()
    except Exception:
        return False
    return EVOLVE_HANDOFF_BEGIN in text or _HANDOFF_MARKER in text


def inject_handoff_check(bot_id: str, bot_user: str | None = None) -> None:
    """Ensure the bot's AGENTS.md includes the handoff check at session start.

    Required for the MCP Bridge's create_handoff tool to be reliable — bots must
    know to check handoffs/ at session start and process pending items.

    Refreshes the delimited block on every call so instruction-text changes
    propagate. Idempotent: a no-op when content already matches.
    """
    bot_user = bot_user or _bot_user_for(bot_id)
    workspace = _user_home(bot_user) / ".openclaw/workspace"
    agents_md = workspace / "AGENTS.md"

    # Read existing content — direct read works via ACL; no sudo grant for cat here.
    # ABORT on any read failure except FileNotFoundError (the only case where
    # treating content as empty is safe). A PermissionError or OSError means the
    # file exists but is unreadable — overwriting it with the minimal template
    # would silently destroy rich bot-specific content. The next deploy cycle
    # will retry; meanwhile the file is preserved.
    try:
        existing = agents_md.read_text()
    except FileNotFoundError:
        existing = ""   # legitimately new file — safe to build from scratch
    except (PermissionError, OSError) as e:
        print(f"[deploy] inject_handoff_check: failed to read {agents_md} ({e}); "
              f"skipping injection for {bot_id} this cycle — file preserved")
        return

    new_content = _build_handoff_agents_md(existing)
    if new_content == existing:
        return  # already up to date

    # Try direct write first — works when set_evolve_read_acl has granted write
    # ACL on workspace/.  Fall back to /tmp staging + sudo /bin/cp when not yet
    # granted (e.g. bot not yet re-deployed after ACL extension).
    try:
        agents_md.write_text(new_content)
        print(f"Handoff check refreshed in {bot_id} AGENTS.md (direct write)")
        return
    except PermissionError:
        pass

    # Fallback: /tmp staging + sudo /bin/cp
    # Sudoers grant: evolve ALL=(root) NOPASSWD: /bin/cp /tmp/evolve-*.md /Users/*/.openclaw/workspace/AGENTS.md
    # (_secure_stage's evolve-stage-*.md name stays inside that glob)
    if why := sudo_dest_refusal(agents_md):  # D-2: bot owns workspace/ — dest of a root cp + chown
        print(f"[warn] {bot_id}: refusing handoff AGENTS.md write — {why}")
        return
    tmp = _secure_stage(new_content, suffix=".md")
    try:
        subprocess.run(
            ["sudo", "/bin/cp", str(tmp), str(agents_md)],
            check=True, capture_output=True,
        )
        if why := sudo_dest_refusal(agents_md):  # re-assert between cp and chown
            print(f"[warn] {bot_id}: refusing handoff AGENTS.md chown — {why}")
            return
        subprocess.run(
            ["sudo", _PROFILE.chown, f"{bot_user}:staff", str(agents_md)],
            check=True, capture_output=True,
        )
    finally:
        tmp.unlink(missing_ok=True)

    print(f"Handoff check refreshed in {bot_id} AGENTS.md (sudo cp)")


def has_session_surface_check(bot_id: str, bot_user: str | None = None) -> bool:
    """Return True if the bot's AGENTS.md contains the session surface check (delimited or legacy)."""
    bot_user = bot_user or _bot_user_for(bot_id)
    agents_md = _user_home(bot_user) / ".openclaw/workspace/AGENTS.md"
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(agents_md)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return False
        return EVOLVE_SURFACE_BEGIN in r.stdout or _SESSION_SURFACE_MARKER in r.stdout
    except Exception:
        return False


def inject_session_surface_check(
    bot_id: str,
    shared_dir: str = "/Users/Shared/evolve",
    bot_user: str | None = None,
) -> None:
    """Scrub any "Pending Approval Tasks" block from the bot's AGENTS.md.

    Earlier versions injected an instruction here telling the bot to invoke
    session_surface.py at session start. The closing line ("no pending tasks —
    proceed normally") primed the LLM to hallucinate that exact phrase back to
    users when the Evolve plugin handled 'evo' out of band, producing a
    confusing duplicate Telegram message after the rec.

    Pending tasks are now surfaced exclusively via the plugin: TurnObserver's
    session_start hook runs session_surface.py and injects its stdout as
    systemAppend, so the bot sees pending tasks without an AGENTS.md directive.

    This function strips the block on every deploy. The signature is preserved
    (shared_dir kept for call-site stability) — a future cleanup can rename it.
    """
    del shared_dir  # no longer used; kept for API compatibility
    bot_user = bot_user or _bot_user_for(bot_id)
    workspace = _user_home(bot_user) / ".openclaw/workspace"
    agents_md = workspace / "AGENTS.md"

    # Read existing content via sudo /bin/cat (this function always uses the sudo
    # path since it runs in the deploy context where direct ACL read may not yet
    # be set up).  Distinguish "file does not exist" (safe to treat as empty —
    # nothing to scrub) from a genuine read failure (ABORT to preserve content).
    try:
        existing_direct = agents_md.read_text()
        existing = existing_direct
    except FileNotFoundError:
        print(f"[deploy] inject_session_surface_check: {agents_md} already absent — nothing to scrub")
        existing = ""   # file doesn't exist — nothing to scrub, no-op ahead
    except (PermissionError, OSError):
        # Direct read failed — try sudo /bin/cat fallback
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(agents_md)],
                capture_output=True, text=True, timeout=5,
            )
        except Exception as e:
            print(f"[deploy] inject_session_surface_check: sudo cat raised {e} for "
                  f"{agents_md}; skipping scrub for {bot_id} — file preserved")
            return
        if r.returncode != 0:
            # Non-zero means the file exists but is unreadable (or the sudo grant
            # is missing). Either way, overwriting with empty content would destroy
            # data. Abort and let the next deploy cycle retry.
            print(f"[deploy] inject_session_surface_check: failed to read {agents_md} "
                  f"(sudo cat rc={r.returncode}); skipping scrub for {bot_id} — file preserved")
            return
        existing = r.stdout

    new_content = _build_surface_agents_md(existing)
    if new_content == existing:
        return  # already clean

    if why := sudo_dest_refusal(agents_md):  # D-2: bot owns workspace/ — dest of a root cp + chown
        print(f"[warn] {bot_id}: refusing session-surface AGENTS.md scrub — {why}")
        return
    tmp = _secure_stage(new_content, suffix=".md")
    try:
        subprocess.run(["sudo", "/bin/cp", str(tmp), str(agents_md)], check=True, capture_output=True)
        if why := sudo_dest_refusal(agents_md):  # re-assert between cp and chown
            print(f"[warn] {bot_id}: refusing session-surface AGENTS.md chown — {why}")
            return
        subprocess.run(["sudo", _PROFILE.chown, f"{bot_user}:staff", str(agents_md)], check=True, capture_output=True)
    finally:
        tmp.unlink(missing_ok=True)

    print(f"Session surface block scrubbed from {bot_id} AGENTS.md")


def repair_security_bot_config(
    bot_id: str | None = None,
    network_path: Path | None = None,
) -> dict:
    """Remove any partial/invalid evolve plugin entry from the security bot's openclaw.json.

    The security/audit bot is a read-only auditor — it should NOT have an evolve
    plugin entry. This function:
      1. Resolves which bot to repair (explicit bot_id, else network.security.botId).
      2. Reads /Users/<user>/.openclaw/openclaw.json (via sudo cat).
      3. Removes plugins.entries.evolve if present.
      4. Writes the cleaned config back (via sudo cp with atomic temp file).
      5. Fixes file ownership (<user>:staff) and permissions (644).
      6. Verifies /Users/<user> home dir is accessible (755).

    Returns a dict with keys:
      - changed: bool — True if the evolve entry was removed
      - warnings: list[str] — non-fatal issues found
      - error: str | None — fatal error message, None on success
      - bot_id: str | None — the bot that was repaired (None if resolution failed)
    """
    from .config import DEFAULT_NETWORK_CONFIG

    result: dict = {"changed": False, "warnings": [], "error": None, "bot_id": None}

    np = network_path or DEFAULT_NETWORK_CONFIG
    try:
        network = load_network(np)
    except Exception as e:
        result["error"] = f"Could not load network config at {np}: {e}"
        return result

    if bot_id is None:
        bot_id = (network.get("security") or {}).get("botId")
    if not bot_id:
        result["error"] = (
            "No security bot configured (network.security.botId is unset). "
            "Specify a bot explicitly: `evolve-admin repair-security_bot --bot <bot_id>`"
        )
        return result

    result["bot_id"] = bot_id
    user = get_bot_user(bot_id, network)
    home = _user_home(user)
    oc_json = home / ".openclaw" / "openclaw.json"

    # ── 1. Verify home exists ───────────────────────────────────────────────────
    if not _secret_perms.exists_or_unreachable(home):  # EACCES (clamped) ≠ absent → don't false-report "not created"
        result["error"] = (
            f"{home} does not exist — bot {bot_id!r} (user {user!r}) not yet created"
        )
        return result

    # ── 2. Check home dir permissions (must be 755 for shell access) ────────────
    try:
        home_stat = home.stat()
        home_mode = stat.S_IMODE(home_stat.st_mode)
        if home_mode != 0o755:
            subprocess.run(
                ["sudo", "/bin/chmod", "755", str(home)],
                check=True, capture_output=True,
            )
            result["warnings"].append(f"{home} mode was {oct(home_mode)}, corrected to 755")
        try:
            pw = pwd.getpwuid(home_stat.st_uid)
            gr = grp.getgrgid(home_stat.st_gid)
            if pw.pw_name != user or gr.gr_name != "staff":
                subprocess.run(
                    ["sudo", _PROFILE.chown, f"{user}:staff", str(home)],
                    check=True, capture_output=True,
                )
                result["warnings"].append(
                    f"{home} was owned by {pw.pw_name}:{gr.gr_name}, corrected to {user}:staff"
                )
        except (KeyError, Exception):
            pass  # uid/gid lookup failed — not fatal
    except OSError as e:
        result["warnings"].append(f"Could not stat {home}: {e}")

    # ── 3. Read openclaw.json ────────────────────────────────────────────────────
    if not _secret_perms.exists_or_unreachable(oc_json):  # EACCES → fall through to the sudo /bin/cat read below, never false "not found"
        result["warnings"].append(f"{oc_json} not found — nothing to clean")
        return result

    try:
        read_proc = subprocess.run(
            ["sudo", "/bin/cat", str(oc_json)],
            capture_output=True, text=True, timeout=10,
        )
        if read_proc.returncode != 0:
            result["error"] = f"Cannot read {oc_json}: {read_proc.stderr.strip()}"
            return result
        cfg = json.loads(read_proc.stdout)
    except json.JSONDecodeError as e:
        result["error"] = f"{oc_json} is not valid JSON: {e}"
        return result
    except Exception as e:
        result["error"] = f"Failed to read {oc_json}: {e}"
        return result

    # ── 4. Remove plugins.entries.evolve if present ──────────────────────────────
    plugins_entries = cfg.get("plugins", {}).get("entries", {})
    if "evolve" in plugins_entries:
        del plugins_entries["evolve"]
        result["changed"] = True

    # ── 5. Write cleaned config back atomically ──────────────────────────────────
    if why := sudo_dest_refusal(oc_json):  # D-2: bot-owned dest of a root cp + chown
        result["error"] = f"Refusing to write repaired {oc_json}: {why}"
        return result
    try:
        tmp_path = _secure_stage(json.dumps(cfg, indent=2))
        subprocess.run(["sudo", "/bin/cp", str(tmp_path), str(oc_json)], check=True, capture_output=True)
        if why := sudo_dest_refusal(oc_json):  # re-assert between cp and chown
            tmp_path.unlink(missing_ok=True)
            result["error"] = f"Refusing to chown repaired {oc_json}: {why}"
            return result
        subprocess.run(["sudo", _PROFILE.chown, f"{user}:staff", str(oc_json)], check=True, capture_output=True)
        _secret_perms.chmod_secret_config(oc_json)  # 0600: token-bearing
        tmp_path.unlink(missing_ok=True)
    except Exception as e:
        result["error"] = f"Failed to write repaired {oc_json}: {e}"
        return result

    return result


# Inheritable evolve-read grant for bot-owned shared-dir subdirs
# (annotations/, turns/, metrics/, spans/, cascade/): the bot's processes
# write files at umask 077, and the admin-side readers (measure.py,
# cost_rollup, pressure_watchdog, …) need read without per-file sudo.
# Perm-verb portion only — the Perms seam renders the platform ACE.
BOT_SHARED_SUBDIR_READ_ACL_PERMS = (
    "read,readattr,list,search,file_inherit,directory_inherit"
)


def fix_shared_dir_permissions(bot_id: str, shared_dir: Path) -> None:
    """Ensure per-bot shared subdirs exist with correct ownership and permissions.

    Called on every deploy_bot() — must be fully idempotent.

    Invariants maintained after this call:
      {sharedDir}/                      — sticky world-writable (1777)
      {sharedDir}/annotations/          — sticky world-writable (1777)
      {sharedDir}/annotations/{bot_id}/ — owned by bot user, writable by bot
      {sharedDir}/{bot_id}/turns/       — owned by bot user, writable by bot, evolve read ACL

    Resolves ``bot_id`` to its macOS account name via ``_bot_user_for`` so
    chown targets the actual unix user — instances where bot_id ≠ unix user
    (e.g. team_bot_b → personal_bot_user) need this or chown silently fails and the dir
    keeps its previous owner. See PR #679 follow-up: team_bot_b's annotations dir
    was stuck at a stale UID, causing the cost converter to PermissionError
    on every run.
    """
    bot_user = _bot_user_for(bot_id)
    # ── Assert root has sticky bit ────────────────────────────────────────────
    # The sticky bit prevents one bot from deleting another bot's subdirectories.
    # It is set by deploy_shared_dir() but can be lost if someone manually ran
    # chmod 777 as a quick fix.  Re-assert here on every deploy.
    try:
        import stat as _stat
        cur_mode = shared_dir.stat().st_mode
        if not (cur_mode & _stat.S_ISVTX):
            shared_dir.chmod(0o1777)
    except PermissionError:
        subprocess.run(
            ["sudo", "/bin/chmod", "1777", str(shared_dir)],
            capture_output=True, check=False,
        )
    except Exception:
        pass  # non-fatal: shared dir may not exist yet, deploy_shared_dir handles it

    # Only the genuinely per-bot subdir is chowned here. proposals/,
    # scoreboard/, and feedback/ are POD-WIDE multi-writer dirs owned by
    # evolve and managed by deploy_shared_dir() + ensure_pod_perms(); they
    # used to be in this loop and got recursively chowned to the
    # most-recently-deployed bot user on every deploy_bot() — turning
    # `sudo evolve-admin deploy --all` (and repo_puller's lagging-bot
    # sweep) into a generator of pod_perms_drift Signals. See the
    # 2026-06-07 incident: the drift cycled 4× in 24h on the canary pod
    # because EVOLVE_VERSION bumps trigger redeploys via repo_puller,
    # which calls deploy_bot for each lagging bot, which used to land
    # here with proposals/ in the loop. The same comment that excluded
    # alerts/ for multi-writer reasons applies word-for-word to the three
    # removed subdirs; the fix is simply to align this loop with that
    # comment.
    for subdir in [f"applications/{bot_id}"]:
        p = shared_dir / subdir
        if p.exists():
            subprocess.run(
                ["sudo", _PROFILE.chown, "-R", f"{bot_user}:{_PROFILE.admin_group}", str(p)],
                capture_output=True, check=False,
            )
    # Note: shared_dir/alerts/, /proposals/, /scoreboard/, /feedback/, and
    # /signals/ are intentionally NOT chowned here. They are multi-writer
    # directories — audit.py writes the CRITICAL dedup file as `evolve`,
    # spend_alert.py and cron_alert.py write per-bot flag files as the
    # bot user, generators write proposals as `evolve`, and bot appliers
    # write apply-results as their own user. Chowning to the
    # most-recently-deployed bot broke audit dedup writes with
    # PermissionError on _record_critical_sent, and (proposals/) caused
    # cross-user atomic renames to EACCES once dir-owner drifted off
    # evolve. Mode 1777 (sticky world-write) for alerts/proposals and
    # mode 0o755 for the evolve-owned set are enforced by
    # ensure_pod_perms — that makes ownership irrelevant for write
    # access on the sticky dirs and keeps the evolve-owned dirs single-
    # writer. pod_perms_drift_monitor catches re-drift between deploys.

    def _create_bot_subdir(subdir: Path, mode: int, acl_perms: str | None = None) -> None:
        """Create a per-bot subdir, set mode, chown to bot, optionally grant
        the evolve service user an ACL (``acl_perms`` = perm-verb portion of
        the ACE; applied through the Perms seam)."""
        try:
            subdir.mkdir(parents=True, exist_ok=True)
            subdir.chmod(mode)
        except PermissionError:
            subprocess.run(
                ["sudo", "/bin/mkdir", "-p", str(subdir)],
                capture_output=True, check=False,
            )
            subprocess.run(
                ["sudo", "/bin/chmod", oct(mode)[2:], str(subdir)],
                capture_output=True, check=False,
            )
        subprocess.run(
            ["sudo", _PROFILE.chown, f"{bot_user}:{_PROFILE.admin_group}", str(subdir)],
            capture_output=True, check=False,
        )
        if acl_perms:
            get_perms().grant(subdir, EVOLVE_SERVICE_USER, acl_perms)

    # ── Pre-create {sharedDir}/annotations/ parent at 1777 ───────────────────
    # This dir is created by deploy_shared_dir() but may be absent if only
    # deploy_bot() was run (e.g., adding a new bot to an existing pod).
    ann_parent = shared_dir / "annotations"
    try:
        ann_parent.mkdir(parents=True, exist_ok=True)
        ann_parent.chmod(0o1777)
    except PermissionError:
        subprocess.run(["sudo", "/bin/mkdir", "-p", str(ann_parent)],
                       capture_output=True, check=False)
        subprocess.run(["sudo", "/bin/chmod", "1777", str(ann_parent)],
                       capture_output=True, check=False)

    # ── Pre-create {sharedDir}/annotations/{bot_id}/ owned by the bot ────────
    # TurnObserver.ts creates this lazily, but pre-creating it here ensures the
    # bot can write annotations from the very first session.  Without this,
    # a permissions race can occur: the annotation dir ends up owned by the
    # evolve user (from a previous run) and the bot user can't write to it.
    # Inheritable evolve-read ACL because TurnObserver.writeAnnotation appends
    # with the bot process's umask (077 → mode 600 on 2026-05-23+), which
    # measure.py / cost_rollup / observations.access otherwise can't read.
    _create_bot_subdir(
        ann_parent / bot_id, 0o755,
        acl_perms=BOT_SHARED_SUBDIR_READ_ACL_PERMS,
    )

    # ── Pre-create {sharedDir}/{bot_id}/turns/ owned by the bot ──────────────
    # The parent {sharedDir}/{bot_id}/ may be evolve-owned (created by cron jobs
    # writing tiers.json, metrics, etc.) — chowning it is fragile.  We only
    # own the turns/ leaf; the bot can write there regardless of parent owner
    # as long as the parent is world-executable (755+).
    _create_bot_subdir(
        shared_dir / bot_id / "turns",
        0o1777,
        # Grant evolve inheritable read ACL so the admin service can read turn
        # files written by the bot user (which default to mode 600).
        acl_perms=BOT_SHARED_SUBDIR_READ_ACL_PERMS,
    )

    # ── Pre-create {sharedDir}/{bot_id}/recommendations/ ─────────────────────
    # Sticky world-writable (1777) so both the evolve user (running daily cron
    # analysis scripts: usage_logger.py, profile_builder.py, gallery_recommender.py)
    # AND the bot user can write here without permission errors.
    # current.json and usage-stats.json are written by evolve-user analysis jobs.
    _create_bot_subdir(shared_dir / bot_id / "recommendations", 0o1777)

    # ── Pre-create {sharedDir}/metrics/{bot_id}/ — two-writer dir ────────────
    # Both the evolve user (running ``cost_rollup.refresh_all`` from
    # ``better_engine_refresh`` every 15 min, writing ``cost-<date>.json``)
    # AND the bot user (via ``RecentTranscriptCapture.ts`` in the plugin,
    # writing ``recent-transcripts.json``) need to write here. Without a
    # pre-creation step the dir was created lazily by whichever process
    # arrived first; if the plugin won the race the dir landed
    # bot-owned and ``cost_rollup`` running as evolve got PermissionError
    # on every subsequent write — that's how personal_bot's dir broke on
    # 2026-05-15, which silently killed the rollup pass for every bot
    # iterated after personal_bot for 10 days (cost_rollup.refresh_all wasn't
    # per-bot fault-tolerant until 2026-05-18). Same 1777 contract as
    # ``recommendations/`` above: sticky bit prevents cross-user deletes.
    # Inheritable evolve-read ACL because recent-transcripts.json is written
    # by the bot's gateway plugin at the gateway umask (077 → mode 600 on
    # 2026-05-23+), and app_posture_reflect / generator_runner /
    # pod_state.turns read it as evolve.
    _create_bot_subdir(
        shared_dir / "metrics" / bot_id, 0o1777,
        acl_perms=BOT_SHARED_SUBDIR_READ_ACL_PERMS,
    )

    # ── Pre-create {sharedDir}/{bot_id}/spans/ — cascade telemetry ───────────
    # Written by the plugin's CascadeTelemetry (one .jsonl file per day:
    # ``spans-YYYY-MM-DD.jsonl``). Read by audit_runner, pressure_watchdog,
    # and the routes_cascade health endpoint, all via the cross-location
    # merge helper observability.session_rollup.iter_turn_spans.
    #
    # Without pre-creation, the plugin's mkdirSync fails with EACCES (parent
    # is evolve-owned drwxr-xr-x, bot user lacks write). The plugin warns
    # once-per-process — visible in the bot's gateway.log but invisible
    # operationally — and silently emits zero spans. That dropped the
    # entire cascade pipeline (Phase 2 + Phase 3) on the mini before this
    # pre-creation landed (2026-05-28 investigation of admin_bot's empty
    # spans dir confirmed the failure mode).
    #
    # Mode 755 because there's exactly one writer (the bot's plugin process).
    # Inheritable evolve-read ACL so audit_runner / pressure_watchdog /
    # routes_cascade can read the bot's spans without needing per-file
    # sudo grants.
    _create_bot_subdir(
        shared_dir / bot_id / "spans", 0o755,
        acl_perms=BOT_SHARED_SUBDIR_READ_ACL_PERMS,
    )

    # ── Pre-create {sharedDir}/{bot_id}/cascade/ — tier1 in-process counter ──
    # The plugin's ModelRouter writes ``tier1_active.json`` here on every
    # tier1 grant — the telemetry-coupled-failure defense for the pressure
    # watchdog (when spans go dark, the watchdog still has these in-process
    # counts as a floor via max(spans, in_process)).
    #
    # Same EACCES failure as spans/ above; same fix.  Mode 755, single
    # writer is the plugin process.  Inheritable evolve-read ACL so the
    # pressure_watchdog daemon (running as evolve) can read the file.
    _create_bot_subdir(
        shared_dir / bot_id / "cascade", 0o755,
        acl_perms=BOT_SHARED_SUBDIR_READ_ACL_PERMS,
    )

    # ── Bot-user write ACE on {sharedDir}/{bot_id}/ itself ───────────────────
    # The plugin's setStandingUserTierDefault (running as the BOT user)
    # temp+renames user-tier-prefs.json in this evolve-owned dir; owner-direct
    # ACE (no sudo, no sudoers grant) opens exactly this bot's user on exactly
    # its own dir. Body + rationale in tier_prefs_acl (deploy.py no-growth cap).
    if not _tier_prefs_acl.ensure_bot_tier_prefs_acl(shared_dir, bot_id, bot_user):
        _log.warning(
            "tier-prefs write ACL grant failed for %s — plugin falls back to "
            "the 'use evo tier-default' message until repaired", bot_id)


# ── Pod-side perm enforcement ────────────────────────────────────────────────
#
# ensure_pod_perms() codifies the four perm layers we hand-applied during the
# 2026-04-25 apply.py-zombie incident. Drift in any layer silently breaks the
# apply daemons: launchd reports EX_CONFIG (78) without surfacing the cause,
# the daemon exits, accumulates zombies, and eventually hangs the host.
#
# All four layers were "obvious in hindsight" but had drifted from deploy.py
# because they were never enforced here in the first place. This pass closes
# that gap.
#
# Idempotency contract:
#   - Re-running on a correct pod is a no-op (no stat or perm changes).
#   - Adding an ACL entry that already exists does NOT add a duplicate ACE.
#   - Recursive chmod is gated on a pre-check; we walk only when we must.
#   - We never restart launchd services here. Wrong layer.
#
# Pair with tools/etr-pod-doctor (when written): both should agree on what
# "correct" looks like — pod-doctor probes, this enforces.

# Service user that runs the admin LaunchDaemon. Architectural — every
# bot's .openclaw/ must be readable by this user so the admin can read
# openclaw.json, auth-profiles.json, and the workspace from a central
# process. Not a personal name and not pod-membership state, so it lives
# here as a constant.
EVOLVE_SERVICE_USER = "evolve"

# Canonical string lives in runtime.perms (the Perms seam owns the read
# contract); this alias preserves the long-standing public name.
POD_ACL_PERMS = _POD_READ_ACL_PERMS


def pod_acl_users(network: dict | None) -> tuple[str, ...]:
    """Derive the canonical ACL allow-set for `.openclaw/` directories.

    Built from system + network state — never hardcoded names. The set is:
      - the admin user running this command (`SUDO_USER`, if any) — CLI /
        debugging access on the operator's machine
      - the evolve service user — admin LaunchDaemon reads openclaw.json,
        auth-profiles.json from a central process
      - the security/audit bot, if one is configured in
        `network.security.botId` — cross-bot read access for review

    Order is stable; duplicates and empty entries are dropped.
    """
    network = network or {}
    out: list[str] = []
    sudo_user = os.environ.get("SUDO_USER", "").strip()
    if sudo_user:
        out.append(sudo_user)
    out.append(EVOLVE_SERVICE_USER)
    sec_bot = (network.get("security") or {}).get("botId")
    if isinstance(sec_bot, str) and sec_bot.strip():
        out.append(sec_bot.strip())
    # De-duplicate while preserving order.
    seen: set[str] = set()
    return tuple(u for u in out if not (u in seen or seen.add(u)))

# The Python venv chain. A bot can only `exec` /Users/Shared/evolve-venv/bin/python3
# if every directory along the symlink chain is g+x and o+x. The chain currently
# resolves into /opt/homebrew/Cellar/python@3.14/.../bin/python3.14. Homebrew
# upgrades sometimes leave the new Cellar tree mode 750, which silently breaks
# every bot's apply.py / measure.py launchd job.
POD_CELLAR_ROOT = Path("/opt/homebrew/Cellar/python@3.14")

# Standard subdirs under {sharedDir}/proposals/. Each is world-writable so the
# evolve admin server (writing apply-results) and bot users (writing proposals)
# can both create files. Cross-user state transitions (e.g. evo's MCP tools
# atomic-renaming an evolve-owned file from pending/ → archived/) require an
# explicit no-sticky-bit invariant — see ``POD_PROPOSALS_MODE``.
POD_PROPOSALS_SUBDIRS: tuple[str, ...] = (
    "pending", "applied", "snoozed", "archived", "apply-results",
)

# Mode 0o0777 — world-readable/writable/executable, NO sticky bit.
#
# Sticky here is wrong (2026-06-08): proposals are written by one user
# (the originating generator/daemon, typically `evolve`) and transitioned
# by another (evo's MCP tools rename-overwrite the same path during
# dismiss/snooze/archive; the still_motivated archive sweep moves a
# pending/<id>.json to archived/ via atomic write-at-src + rename). On a
# sticky-bit dir, only the file owner / dir owner / root can delete or
# overwrite a child — and that check fires regardless of the
# inherited ``user:evo allow delete`` ACL applied by
# ``_ensure_evo_write_acl``, because the kernel evaluates sticky at the
# directory level before the file's delete bit. Symptom is the
# still_motivated generator's "archive write failed [Errno 13] Permission
# denied: …/.<id>.json.<rand>.tmp → …/<id>.json" log spam — the tmp
# (owned by evo) succeeded mkstemp, but ``os.replace(tmp, dest)`` hit
# EACCES on the implicit unlink of the evolve-owned dest.
#
# Dropping sticky lets any writer with world-write transition any
# proposal — which is what we want, since the set of legitimate
# transition actors (admin daemon, evo MCP, verify daemon, sweep) all
# legitimately rewrite each others' files. The original 2026-05-12
# fix to deploy_shared_dir already established 0o777 for these dirs;
# this constant was the contract drift that quietly re-imposed sticky
# on every ``ensure_pod_perms`` pass. Pair-with: ``_check_proposals_dir``.
POD_PROPOSALS_MODE = 0o0777

# {sharedDir}/alerts/ is also multi-writer, but with a different actor set:
#   - audit.py writes audit-critical-dedup.json as the `evolve` user.
#   - spend_alert.py / cron_alert.py write `spend-{bot}-...flag` and
#     `cron-{job}-...flag` as the bot user that owns the cron job.
# Each writer only ever touches its own files (no cross-user overwrite),
# so sticky 1777 is the right contract — bots can't delete another bot's
# alert flag, the admin daemon (dir owner) can still clean up.
POD_ALERTS_MODE = 0o1777

# Single-writer evolve-only shared subdirs. The 2026-06-06 audit (chip
# spawned off the config_intents fix in PR #2299) generalized the class:
# every dir under {shared_dir} that ONLY the evolve service user writes
# is at risk of the "first writer wins ownership" bug — whichever process
# mkdir's it first claims uid ownership, and if a non-evolve user wins
# (operator hand-placing files over SSH, a per-bot daemon racing during
# install), every subsequent evolve write hits EACCES on tempfile.mkstemp()
# inside the atomic-write helpers. The fix is the same shape for every entry:
#   - dir-mode check with create=True (covers fresh pod)
#   - dir-owner check with a recursive chown apply (covers wrong-owner
#     files already inside — safe because single-writer = no other claimant)
#
# Spec invariant: spec-config-intent-system-2026-05-21.md §2.6 — "All paths
# under {shared_dir} are owned by the evolve user." This list codifies it
# for ensure_pod_perms; pod_perms_drift_monitor catches re-occurrences hourly.
#
# Mode 0o755 is enough — only evolve writes, world-read keeps the door
# open for an operator `cat` over SSH. The load-bearing invariant is
# dir owner == evolve.
#
# Excluded from this list (handled separately):
#   - proposals/, alerts/   → multi-writer (mode 1777), checked by
#                             _check_proposals_dir + _check_alerts_dir.
#   - keystore/             → covered by _check_evo_write_acl whose apply
#                             recursively chowns to evolve:wheel as a side
#                             effect of the ACL fix.
#   - per-bot subdirs ({shared_dir}/<bot>/turns/, etc.) → bot-owned by
#                             design, not in this single-writer class.
POD_EVOLVE_OWNED_DIR_MODE = 0o755

EVOLVE_OWNED_SHARED_SUBDIRS: tuple[str, ...] = (
    # Config intent sidecars — the 2026-06-06 incident that surfaced
    # this whole class (see spec-config-intent-system-2026-05-21.md, PR #2299).
    "config_intents",
    # Unified observation store. evo bot also reads/writes via MCP, but
    # write happens through admin-daemon socket → still single-writer
    # from the file-perm standpoint. The evo write ACL on signals/ is
    # orthogonal — _check_evo_write_acl covers cross-user rename perms;
    # this check covers ownership of the dir itself.
    "signals",
    # ObservationTuple JSONL extracted from per-bot conversations
    # (analyzer/extract_tuples.py + cli tool_gaps.jsonl writer).
    "observations",
    # Per-bot profile.md frontmatter+body files
    # (admin/profile.storage + admin/migrate_user_profile).
    "profiles",
    # Per-generator GeneratorRecord JSON (analyzer/generator_runner +
    # admin/repo_puller + analyzer/signal_subscriber_runner).
    "generators",
    # WatchdogEvent JSONL (analyzer/app_audit_investigation + signals.backfill).
    "watchdog",
    # Calibration snapshots (analyzer/calibration).
    "calibration",
    # Per-bot cost-cap settings (analyzer/cost_profiles + deploy.snap_file).
    "cost-settings",
    # Tier-cascade pressure flags + audit labels (web/routes_cascade).
    "cascade",
    # Handover ledger entries (admin/recovery).
    "recovery",
    # Handover token + preference stores (admin/handover).
    "handover-tokens", "handover-prefs",
    "apps",  # apps layer: id/spec-migration tables + v-next Specs (app_spec_store)
)

# Back-compat alias — POD_CONFIG_INTENTS_MODE landed in PR #2299, this
# audit generalized the symbol in the same series. Keep the name
# available for any out-of-tree caller that imported it during the
# brief window between the two PRs.
POD_CONFIG_INTENTS_MODE = POD_EVOLVE_OWNED_DIR_MODE

# ── Evo write-ACL contract (post-evo-account-separation) ─────────────────────
# After Phase E.2.b of docs/spec-evo-account-separation-2026-05-25.md, the evo
# bot runs as an unprivileged `evo` macOS user instead of sharing the `evolve`
# service account. Its tools still need to rename/delete files in two shared
# stores that are owned by evolve:
#   - {sharedDir}/proposals/ — arbiter.store.move_proposal on user actions
#     (dismiss/approve/snooze) via action.proposal.* MCP tools.
#   - {sharedDir}/signals/  — signals.store.apply_transition on snooze /
#     resolve / dismiss via action.signal.* MCP tools.
# Two perm contracts compose to make cross-user state transitions work:
#   1. Dir owned by evolve:wheel + inherited ACL granting evo the
#      write/delete/append perms it needs. Covers the case where the
#      *dir* owner drifted to evo (2026-05-25 post-cutover bug:
#      evolve hit EACCES on the implicit unlink because the dir was
#      evo-owned).
#   2. NO sticky bit on the dir (POD_PROPOSALS_MODE = 0o0777, not 1777).
#      Sticky overrides ACL for the parent-dir-level delete check during
#      ``os.replace(tmp, dest)``: when evo's tmp file replaces an
#      evolve-owned dest, the kernel blocks the implicit unlink because
#      the sticky bit restricts deletes to file/dir owners regardless
#      of the file's own delete ACL. Symptom is the still_motivated
#      "archive write failed [Errno 13]" log spam from 2026-06-08.
#      ``proposals/`` covers this via POD_PROPOSALS_MODE; ``signals/``
#      is non-world-writable (mode 0o755) so the question doesn't
#      arise — writes there go through the dir-owner (evolve) or the
#      evo ACL grant, neither of which needs the sticky-bypass.
EVO_GATEWAY_USER = "evo"

# Granted to evo with file_inherit + directory_inherit so new files/subdirs
# pick up the same grant. Mirrors the evolve-side workspace/evolve/ write ACL
# (set_evolve_read_acl), inverted. ``execute`` = traverse; see KEYSTORE_DIR_MODES.
EVO_WRITE_ACL_PERMS = (
    "read,write,execute,delete,append,"
    "readattr,writeattr,readextattr,writeextattr,readsecurity,"
    "file_inherit,directory_inherit"
)

# Subdirs the evo write contract applies to. Keep narrow — only the dirs
# evo's MCP tools mutate. Other shared subdirs (profiles/, observations/,
# alerts/, …) stay evolve-only; evo reads them via either world-readable
# perms or admin-daemon HTTP endpoints.
#
# ``keystore``: evo doesn't write keystore entries directly, but it must
# *read* the shared machine key at ``{shared_dir}/keystore/.machine-key``
# to XOR-decrypt entries written by the admin daemon (e.g. github_intake
# for ``evo intake promote``). Without an ACL grant, mode 0640 on the
# key file leaves evo blocked; the file-vault read silently returns None
# and surfaces as ``no token in keystore slot`` even when the UI sees
# the token. The write bit on the ACL is unused but matches the existing
# perm shape; restricting to read-only would require a second constant
# without buying meaningful safety (evo's threat model already includes
# arbitrary writes to proposals/signals via the gateway socket).
#
# ``config_intents``: evolve daemon writes here too — the L2 appliers
# (UpdatePermissionConfigApplier, UpdateAgentDefaultsApplier),
# deploy.py's exec-policy inference recorder (PR #2304), and Phase 3's
# inference layer (PR #2317) all call ``config_intent.set_intent``
# which atomic-rename-writes into this dir. Pre-2026-06-06 the
# directory wasn't in this contract and shipped owned by the wizard
# admin user; daemon-side writes failed EACCES on the tempfile.mkstemp
# call and the existing intent records had to be manually placed via
# direct admin write. Adding it here means ensure_pod_perms applies
# the same chown evolve:wheel + inherited user:evo write ACL the
# other dirs get.
EVO_WRITE_SHARED_SUBDIRS: tuple[str, ...] = (
    "proposals", "signals", "keystore", "config_intents",
)

# ── Store lock files (7.1 Phase A) ───────────────────────────────────────────
# Cross-process flock files for the Signal + Proposal stores
# (store_lock.py in packages/analyzer; spec-state-store-and-deploy-
# resilience-2026-06-10.md §1.4). Pre-creation here is LOAD-BEARING:
# bot users take the signals lock transitively (write_proposal →
# signal backrefs) but cannot create files in the evolve-owned 0755
# signals/ dir — they can only flock a lock file that already exists
# with read access. Mode 0666 so every writer class (evolve, evo, bot
# users) can open it; flock works on a read-only fd, so 0666 is
# generous-but-harmless rather than strictly required for openability.
#
# NEVER repair these files by replace/rename — chmod/chown in place
# only. A renamed lock file splits mutual exclusion across two inodes
# silently.
STORE_LOCK_FILE_NAME = ".store.lock"
STORE_LOCK_MODE = 0o666
STORE_LOCK_SUBDIRS: tuple[str, ...] = ("proposals", "signals")


@dataclass
class _PermCheck:
    """One drift check + the action that would fix it.

    `ok=True` means the host already matches the canonical contract.
    `apply` is a zero-arg callable; calling it in apply mode runs the fix.
    None means the check is informational-only (we have no fix for it).
    """
    category: str          # e.g. "ACL", "lock-file", "proposals-dir"
    target: str            # the path or subject
    ok: bool
    detail: str = ""       # human-readable reason / observed value
    fix_description: str = ""  # what the fix would do, for --check-only output
    apply: Any = None      # zero-arg callable: () -> bool, returns True on success


@dataclass
class PodPermsResult:
    """Outcome of an ensure_pod_perms() pass.

    `checks` is the full list (passes + drift). `applied` lists fix descriptions
    for changes that were actually run. `errors` collects fix-failures. The CLI
    formats this into a per-section report.
    """
    bot_ids: list[str] = field(default_factory=list)
    checks: list[_PermCheck] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def drift(self) -> list[_PermCheck]:
        return [c for c in self.checks if not c.ok]


def _acl_user_present(path: Path, user: str, required_perms: str = POD_ACL_PERMS) -> bool:
    """True if `path` grants `user` at least `required_perms` — EFFECTIVELY.

    Thin wrapper over the Perms seam (W4a): macOS parses `ls -lde` ACE
    presence (mode bits and ACLs are orthogonal there, so presence is
    effective), Linux parses `getfacl` *effective* perms so a
    chmod-clobbered ACL mask reads as drift. The check is "at least"
    rather than exact-match — operators may have hand-added more
    permissive entries, and those already cover the contract.

    `required_perms` defaults to POD_ACL_PERMS (the .openclaw/ read
    contract). Callers checking different contracts — e.g. the evo write
    ACL on {sharedDir}/proposals/ — pass their own perm set.
    """
    return get_perms().acl_user_effective(path, user, required_perms)


def _add_acl(bot_user: str, user: str) -> bool:
    """(Re)establish the canonical `.openclaw` read contract for `user` — the
    ensure_pod_perms drift repair for a missing/clamped ACE (every deploy + the
    pod-wide ``ensure-pod-perms`` pass; the hourly drift monitor is check_only, so
    its apply lands on the next pass). Routes through
    ``_apply_openclaw_read_contract`` so the repair carries the bot-private CLAMP —
    without it, this repair re-widened ``other::``→``r-x`` and re-armed world-readable
    minting (the #3198 sibling-call-site gap). Idempotent.
    """
    return _apply_openclaw_read_contract(bot_user, user, get_perms())


def _check_bot_acl(bot_user: str, allow_users: tuple[str, ...]) -> list[_PermCheck]:
    """Check the .openclaw/ ACL for one bot. One _PermCheck per allow-set user.

    `allow_users` is the canonical ACL list derived from system + network
    state via `pod_acl_users(network)` — never a hardcoded literal.
    """
    oc_dir = _user_home(bot_user) / ".openclaw"
    checks: list[_PermCheck] = []
    if not _secret_perms.exists_or_unreachable(oc_dir):  # EACCES (clamped) → proceed; the per-user ACL checks below detect+repair the clamp
        # Bot not yet bootstrapped; nothing to enforce. Single informational check.
        checks.append(_PermCheck(
            category="ACL",
            target=str(oc_dir),
            ok=True,
            detail="(bot not yet bootstrapped — skipping)",
        ))
        return checks
    for user in allow_users:
        present = _acl_user_present(oc_dir, user)
        checks.append(_PermCheck(
            category="ACL",
            target=str(oc_dir),
            ok=present,
            detail=f"user:{user} {'present' if present else 'missing'}",
            fix_description=f"re-assert recursive read contract (clamp + carve-out + re-widen) for {user} on {oc_dir}",
            apply=(None if present else (lambda u=user, bu=bot_user: _add_acl(bu, u))),
        ))
    return checks


def _check_store_lock_file(shared_dir: Path, store_subdir: str) -> _PermCheck:
    """Check {sharedDir}/{store_subdir}/.store.lock exists, owned by evolve, mode 0666.

    See STORE_LOCK_FILE_NAME — the pre-creation is load-bearing for bot
    users (they can flock an existing file but can't create one in the
    evolve-owned signals/ dir). Repair is strictly in-place
    (touch/chown/chmod); never replace or rename a lock file.
    """
    lock = shared_dir / store_subdir / STORE_LOCK_FILE_NAME
    if not lock.exists():
        return _PermCheck(
            category="store-lock",
            target=str(lock),
            ok=False,
            detail="missing",
            fix_description=(
                f"touch {lock} && chown evolve {lock} && chmod 666 {lock}"
            ),
            apply=lambda: _ensure_store_lock_file(lock),
        )
    try:
        st = lock.stat()
        cur_owner = pwd.getpwuid(st.st_uid).pw_name
    except (KeyError, FileNotFoundError, PermissionError) as e:
        return _PermCheck(
            category="store-lock", target=str(lock), ok=False,
            detail=f"stat failed: {e}",
            fix_description=f"repair {lock} in place (chown evolve, chmod 666)",
            apply=lambda: _ensure_store_lock_file(lock),
        )
    mode = st.st_mode & 0o777
    if mode != STORE_LOCK_MODE:
        return _PermCheck(
            category="store-lock", target=str(lock), ok=False,
            detail=f"owner={cur_owner!r} mode={oct(mode)}; expected 0o666",
            fix_description=f"chown evolve {lock} && chmod 666 {lock}",
            apply=lambda: _ensure_store_lock_file(lock),
        )
    # Owner is informational only: flock needs read access, which 0666
    # grants regardless of owner. A lazily-created lock owned by root
    # (CLI) or evo (gateway) is fully functional — flagging it as drift
    # would page hourly for a non-problem (the next root deploy chowns
    # it to evolve anyway via _ensure_store_lock_file).
    return _PermCheck(
        category="store-lock", target=str(lock), ok=True,
        detail=f"owner={cur_owner}, mode={oct(mode)}",
    )


def _ensure_store_lock_file(lock: Path) -> bool:
    """Create/repair a store lock file in place. Idempotent.

    In-place only: an existing file is chown/chmod'ed, never recreated —
    replacing the inode would split flock mutual exclusion between
    holders of the old fd and openers of the new path.
    """
    try:
        if not lock.exists():
            try:
                lock.touch(mode=STORE_LOCK_MODE, exist_ok=True)
            except PermissionError:
                proc = subprocess.run(
                    ["sudo", "/usr/bin/touch", str(lock)],
                    capture_output=True, text=True, timeout=5,
                )
                if proc.returncode != 0:
                    return False
        proc = subprocess.run(
            ["sudo", _PROFILE.chown, "evolve", str(lock)],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            return False
        proc = subprocess.run(
            ["sudo", "/bin/chmod", oct(STORE_LOCK_MODE)[2:], str(lock)],
            capture_output=True, text=True, timeout=5,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _check_proposals_dir(shared_dir: Path) -> list[_PermCheck]:
    """Check {sharedDir}/proposals/ root + standard subdirs.

    Two invariants per dir:
      - mode 0o777 (world-writable, NO sticky bit; see ``POD_PROPOSALS_MODE``
        for the rationale — sticky on these dirs blocks evo from
        atomic-renaming an evolve-owned proposal during snooze/dismiss/
        archive, even with the inherited evo write ACL).
      - owner evolve:wheel (the admin daemon's user)

    Owner enforcement is its own check, separate from the ACL check in
    ``_check_evo_write_acl``: the ACL fix only chowns as a side effect of
    its apply branch, so once the ACL is in place an owner drift goes
    unnoticed indefinitely. That's how personal_bot:wheel ownership persisted
    on the mini through every deploy until the 2026-05-29 cleanup —
    personal_bot-owned proposal/ dirs locked evolve out of moves into peer
    subdirs (file owner isn't evolve; dir owner isn't evolve; no ACL grant
    for evolve), and writes still half-worked enough that no one noticed.
    """
    checks: list[_PermCheck] = []
    root = shared_dir / "proposals"
    checks.append(_check_dir_mode(root, POD_PROPOSALS_MODE, create=True))
    checks.append(_check_dir_owner(root, "evolve"))
    for sub in POD_PROPOSALS_SUBDIRS:
        sub_path = root / sub
        checks.append(_check_dir_mode(sub_path, POD_PROPOSALS_MODE, create=True))
        checks.append(_check_dir_owner(sub_path, "evolve"))
    return checks


def _check_alerts_dir(shared_dir: Path) -> list[_PermCheck]:
    """Check {sharedDir}/alerts/ is mode 1777, owned by evolve:wheel.

    audit.py (running as `evolve`), spend_alert.py and cron_alert.py
    (running as bot users) all write here. Mode 1777 makes ownership
    irrelevant for write access; the sticky bit prevents one writer
    from deleting another writer's files.

    Owner is enforced separately for the same reason as proposals/: if
    a bot's writer wins the mkdir race on a fresh install, the dir
    silently becomes bot-owned, and `evolve` then can't `rmdir` or
    repair it (writes still work, so no daemon notices). Pair the mode
    check with an explicit owner check so drift surfaces.
    """
    p = shared_dir / "alerts"
    return [
        _check_dir_mode(p, POD_ALERTS_MODE, create=True),
        _check_dir_owner(p, "evolve"),
    ]


def _ensure_evolve_owned_dir_perms(path: Path) -> bool:
    """Idempotently make `path` an evolve-owned dir at POD_EVOLVE_OWNED_DIR_MODE, recursively.

    Unlike `_set_dir_owner` (used by the generic owner check), this
    recurses into the dir so existing files written by a wrong-owner
    process — the symptom from the 2026-06-06 config_intents bug, where
    sidecars hand-placed as the pod-admin-user blocked the daemon from
    rewriting them — are normalized in the same apply pass. Every entry
    in EVOLVE_OWNED_SHARED_SUBDIRS is single-writer (evolve only); no
    other daemon has a legitimate write claim on any file in those
    trees, so `chown -R` is safe.
    """
    if not path.exists():
        # Mode check's create branch handles initial mkdir; this path
        # is only entered as the owner check's apply, so the dir should
        # exist by the time we get here. Safety guard for the unusual
        # ordering case where owner apply runs before mode apply.
        proc = subprocess.run(
            ["sudo", "/bin/mkdir", "-p", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            return False
    proc = subprocess.run(
        ["sudo", _PROFILE.chown, "-R", f"evolve:{_PROFILE.admin_group}", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return False
    proc = subprocess.run(
        ["sudo", "/bin/chmod", oct(POD_EVOLVE_OWNED_DIR_MODE)[2:], str(path)],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        return False
    # On Linux the chmod above just set the ACL mask from its group bits,
    # capping any named ACE (e.g. the evo write grant on signals/) — re-widen.
    # No-op on macOS and on paths without an extended ACL.
    return get_perms().reassert_mask(path)


# Back-compat alias — kept for the brief overlap window between the
# config_intents PR (which introduced this name) and the audit PR.
_ensure_config_intents_perms = _ensure_evolve_owned_dir_perms


def _check_evolve_owned_dir(shared_dir: Path, subdir: str) -> list[_PermCheck]:
    """Check {sharedDir}/<subdir>/ is owned by evolve:wheel, mode POD_EVOLVE_OWNED_DIR_MODE.

    Generic owner-drift detector for every entry in EVOLVE_OWNED_SHARED_SUBDIRS.
    The 2026-06-06 incident on config_intents/ surfaced the class: hand-placing
    the first files as a non-evolve user left the dir wrong-owned, and every
    subsequent atomic write from the daemon hit EACCES on tempfile.mkstemp
    inside the parent dir. The mode check's create branch covers the "fresh
    pod" case; the owner check (with a recursive chown apply) covers the
    "wrong-owner files already inside" case.
    """
    p = shared_dir / subdir
    mode_check = _check_dir_mode(p, POD_EVOLVE_OWNED_DIR_MODE, create=True)
    owner_check = _check_dir_owner(p, "evolve")
    # Override the generic non-recursive chown with our recursive variant
    # so existing files get normalized in the same pass. Generic
    # _check_dir_owner skips its apply when the dir is missing — we only
    # patch the apply when the dir exists and ownership has actually
    # drifted, matching the generic helper's behavior elsewhere.
    if not owner_check.ok and owner_check.apply is not None:
        owner_check.apply = lambda: _ensure_evolve_owned_dir_perms(p)
    return [mode_check, owner_check]


# Back-compat alias — kept for the brief overlap window between the
# config_intents PR (which introduced this name) and the audit PR.
def _check_config_intents_dir(shared_dir: Path) -> list[_PermCheck]:
    return _check_evolve_owned_dir(shared_dir, "config_intents")


def _evo_user_exists() -> bool:
    """True if the `evo` macOS account is provisioned (Phase E.2.a done)."""
    try:
        import pwd as _pwd
        _pwd.getpwnam(EVO_GATEWAY_USER)
        return True
    except KeyError:
        return False


def _ensure_evo_write_acl(path: Path) -> bool:
    """Idempotently grant the evo user write+inherit ACL on `path`.

    Two-step apply via the Perms seam: grant on the dir itself
    (inheritance covers new children) plus a recursive backfill onto
    existing files/dirs that pre-date the ACL. On macOS that is the
    historical `chmod +a` / `-R +a` pair with the prefixed ACE shape
    (`exit 1` + "exists" = already present = success); on Linux it is the
    setfacl access + default-ACL pair.

    Also normalizes ownership of `path` (and its existing tree) to
    evolve:wheel so the dir owner is the admin daemon's user. Without
    this, a stale evo-owned tree (the symptom from the post-cutover
    bug on 2026-05-25) leaves the admin daemon unable to rename
    state files even with the ACL in place.
    """
    # 1. chown -R back to evolve:<admin_group> (wheel macOS / root Linux) —
    #    recovers stale ownership. Idempotent (no-op when already correct).
    subprocess.run(
        ["sudo", _PROFILE.chown, "-R", f"evolve:{_PROFILE.admin_group}", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    # 2.+3. inheritable grant on the dir + recursive backfill (best-effort).
    return get_perms().grant_write_recursive(
        path, EVO_GATEWAY_USER, EVO_WRITE_ACL_PERMS, prefixed=True)


def _check_evo_write_acl(shared_dir: Path, subdir: str) -> _PermCheck:
    """Check {sharedDir}/<subdir>/ grants the evo user write ACL.

    Skipped (informational pass) when the `evo` macOS user doesn't exist —
    a fresh pod that hasn't run Phase E.2.a, or an operator who chose
    not to separate accounts, has nothing to enforce here.
    """
    target = shared_dir / subdir
    category = "ACL"
    if not _evo_user_exists():
        return _PermCheck(
            category=category, target=str(target), ok=True,
            detail=f"(evo macOS user does not exist — skipping {subdir} ACL)",
        )
    if not target.exists():
        # The dir-mode check on the proposals/ tree creates it; for stores
        # that don't have a parallel mode check (signals/), absence is just
        # "nothing to enforce yet" — the first writer creates the dir and
        # the next ensure_pod_perms pass picks it up.
        return _PermCheck(
            category=category, target=str(target), ok=True,
            detail=f"(dir not yet created — skipping {subdir} ACL)",
        )
    present = _acl_user_present(target, EVO_GATEWAY_USER, EVO_WRITE_ACL_PERMS)
    if present:
        return _PermCheck(
            category=category, target=str(target), ok=True,
            detail=f"user:{EVO_GATEWAY_USER} write ACL present",
        )
    return _PermCheck(
        category=category, target=str(target), ok=False,
        detail=f"user:{EVO_GATEWAY_USER} write ACL missing",
        fix_description=(
            f'chown -R evolve:wheel {target} && '
            f'chmod +a "user:{EVO_GATEWAY_USER} allow {EVO_WRITE_ACL_PERMS}" '
            f'{target}'
        ),
        apply=lambda: _ensure_evo_write_acl(target),
    )


def _check_dir_mode(path: Path, expected_mode: int, create: bool = False) -> _PermCheck:
    """Check `path` exists and has mode `expected_mode`. Optionally allow creation."""
    if not path.exists():
        if not create:
            return _PermCheck(
                category="dir-mode", target=str(path), ok=False,
                detail="missing",
                fix_description=f"(no fix — caller did not authorize creation)",
            )
        return _PermCheck(
            category="dir-mode", target=str(path), ok=False,
            detail="missing",
            fix_description=f"mkdir -p {path} && chmod {oct(expected_mode)[2:]} {path}",
            apply=lambda: _create_dir_with_mode(path, expected_mode),
        )
    try:
        # ACL-mask-aware on Linux (the stat group triad displays the mask
        # on ACL'd paths — see runtime.perms); plain stat on macOS.
        cur_mode = get_perms().effective_mode(path)
    except PermissionError:
        return _PermCheck(
            category="dir-mode", target=str(path), ok=False,
            detail=f"cannot stat (PermissionError)",
        )
    if cur_mode == expected_mode:
        return _PermCheck(
            category="dir-mode", target=str(path), ok=True,
            detail=f"mode {oct(cur_mode)}",
        )
    return _PermCheck(
        category="dir-mode", target=str(path), ok=False,
        detail=f"mode {oct(cur_mode)} != expected {oct(expected_mode)}",
        fix_description=f"chmod {oct(expected_mode)[2:]} {path}",
        apply=lambda: _set_dir_mode(path, expected_mode),
    )


def _create_dir_with_mode(path: Path, mode: int) -> bool:
    """mkdir -p + chmod. Uses sudo as fallback when current user can't write parent."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
        return True
    except PermissionError:
        proc = subprocess.run(
            ["sudo", "/bin/mkdir", "-p", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            return False
        proc = subprocess.run(
            ["sudo", "/bin/chmod", oct(mode)[2:], str(path)],
            capture_output=True, text=True, timeout=5,
        )
        return proc.returncode == 0


def _set_dir_mode(path: Path, mode: int) -> bool:
    try:
        os.chmod(path, mode)
        return True
    except PermissionError:
        proc = subprocess.run(
            ["sudo", "/bin/chmod", oct(mode)[2:], str(path)],
            capture_output=True, text=True, timeout=5,
        )
        return proc.returncode == 0


def _check_dir_owner(path: Path, expected_user: str) -> _PermCheck:
    """Check `path` is owned by `expected_user`. Apply via sudo chown.

    Non-recursive on purpose: file ownership inside multi-writer dirs is
    legitimately mixed — proposals/pending/ holds files written by the
    admin daemon (evolve) and by evo's MCP tools (evo), and a recursive
    chown would overwrite that.  For alerts/turns/metrics/annotations
    (sticky 1777), the dir-owner grant is what lets the admin daemon
    clean up bot-owned children; for proposals/ (now 0o777 non-sticky)
    world-write covers cross-owner deletes directly.  Either way the
    invariant is "dir owner is evolve"; children take care of themselves.
    """
    import pwd as _pwd
    if not path.exists():
        # The mode check runs first and creates the dir if authorized; if
        # this owner check sees missing, the create either failed or wasn't
        # authorized — either way nothing to enforce yet.
        return _PermCheck(
            category="dir-owner", target=str(path), ok=True,
            detail="(dir not yet created — skipping owner check)",
        )
    try:
        cur_user = _pwd.getpwuid(path.stat().st_uid).pw_name
    except (PermissionError, KeyError):
        return _PermCheck(
            category="dir-owner", target=str(path), ok=False,
            detail="cannot resolve owner",
        )
    if cur_user == expected_user:
        return _PermCheck(
            category="dir-owner", target=str(path), ok=True,
            detail=f"owner={cur_user}",
        )
    return _PermCheck(
        category="dir-owner", target=str(path), ok=False,
        detail=f"owner={cur_user} != expected {expected_user}",
        fix_description=f"chown {expected_user}:{_PROFILE.admin_group} {path}",
        apply=lambda: _set_dir_owner(path, expected_user),
    )


def _set_dir_owner(path: Path, user: str) -> bool:
    proc = subprocess.run(
        ["sudo", _PROFILE.chown, f"{user}:{_PROFILE.admin_group}", str(path)],
        capture_output=True, text=True, timeout=10,
    )
    return proc.returncode == 0


def _check_cellar_perms() -> _PermCheck:
    """Spot-check that /opt/homebrew/Cellar/python@3.14 is bot-readable.

    Walking the full Cellar tree on every deploy would be wasteful (it has
    thousands of files). Instead we sample: the Cellar root and the resolved
    python3 binary. If both are g+rX and o+rX, the chain is in good shape.
    A drift in a leaf file would still page someone via the apply daemon's
    EX_CONFIG, but the common drift mode is "homebrew upgrade reset the
    top-level dir to 750", which the spot check catches.
    """
    # Homebrew Cellar is macOS-only — skip explicitly on Linux (no /opt/homebrew; never reach _apply_cellar_perms chmod).
    if _PROFILE.name != "macos":
        return _PermCheck(category="cellar", target=str(POD_CELLAR_ROOT), ok=True,
                          detail=f"(non-macOS pod [{_PROFILE.name}] — no Homebrew Cellar, skipping)")
    if not POD_CELLAR_ROOT.exists():
        return _PermCheck(
            category="cellar", target=str(POD_CELLAR_ROOT), ok=True,
            detail="(no python@3.14 Cellar — different host layout, skipping)",
        )

    # Sample 1: the Cellar root itself
    samples: list[Path] = [POD_CELLAR_ROOT]
    # Sample 2: resolved venv python3 binary (the common failure path)
    venv_py = Path(VENV_PYTHON)
    if venv_py.is_symlink() or venv_py.exists():
        try:
            resolved = venv_py.resolve()
            if resolved.exists() and str(resolved).startswith(str(POD_CELLAR_ROOT)):
                samples.append(resolved)
                if resolved.parent.exists():
                    samples.append(resolved.parent)
        except Exception:
            pass

    bad: list[Path] = []
    for p in samples:
        try:
            st = p.stat()
            mode = st.st_mode & 0o777
            # Need group + other read. For dirs, also need execute.
            need = 0o044
            if p.is_dir():
                need = 0o055
            if (mode & need) != need:
                bad.append(p)
        except (PermissionError, FileNotFoundError):
            bad.append(p)

    if not bad:
        return _PermCheck(
            category="cellar", target=str(POD_CELLAR_ROOT), ok=True,
            detail=f"{len(samples)} samples g+rX o+rX",
        )
    return _PermCheck(
        category="cellar", target=str(POD_CELLAR_ROOT), ok=False,
        detail=f"missing g+rX or o+rX on: {', '.join(str(p) for p in bad)}",
        fix_description=f"chmod -R go+rX {POD_CELLAR_ROOT}",
        apply=lambda: _apply_cellar_perms(),
    )


def _apply_cellar_perms() -> bool:
    """Run `chmod -R go+rX` on the Cellar root."""
    proc = subprocess.run(
        ["sudo", "/bin/chmod", "-R", "go+rX", str(POD_CELLAR_ROOT)],  # sudo-grant: known-gap: no chmod grant for the Homebrew Cellar root — _check_cellar_perms apply path fails in daemon (evolve) context; tracked
        capture_output=True, text=True, timeout=120,
    )
    return proc.returncode == 0


# _check_apply_plist / _read_plist_username / _regen_apply_plist removed
# 2026-08-18 with the legacy per-bot apply daemon. Leaving the check in
# place after the installer was deleted would have made ensure_pod_perms
# report a missing plist on every bot forever, with a fix that could never
# succeed. See docs/design-proposal-signing-key-2026-08-18.md.


def _check_cli_device_scopes(bot_id: str, bot_user: str) -> _PermCheck:
    """Check the bot's own CLI device carries the full operator scope set.

    The OC 2026.6 upgrade narrowed every bot's CLI device to
    ``["operator.read"]`` in ``~/.openclaw/devices/paired.json``, killing
    ``openclaw message send`` and every defer/deliver fire pod-wide
    (spec-gallery-delivery-convention-2026-06-11.md §6 step 0). The check/
    repair/day-1-seed logic lives in ``oc_cli_device``; this wrapper maps
    it onto the ensure_pod_perms _PermCheck shape so every deploy applies
    it and the hourly pod_perms_drift_monitor turns a future re-narrowing
    (the next OC upgrade) into a Signal between deploys.

    The apply kickstarts the bot's gateway — but only when the file was
    actually narrowed or unseeded, never on a no-op pass (same contract
    as every other check here).
    """
    from . import oc_cli_device as _ocd

    # Same resolver the check itself probes — keeps the reported target
    # honest (and tests that rebind the resolver see consistent paths).
    target = str(_ocd._oc_dir_resolver(bot_user) / _ocd.PAIRED_REL)
    try:
        chk = _ocd.check_cli_device_scopes(bot_user)
    except Exception as e:  # never let a probe failure break the perms pass
        return _PermCheck(
            category="cli-device", target=target, ok=False,
            detail=f"check raised: {e}",
        )
    if chk.ok:
        return _PermCheck(
            category="cli-device", target=target, ok=True, detail=chk.detail,
        )
    if not chk.fixable:
        # No fix offered (malformed paired.json, unrecognized identity
        # format): leave fix_description empty — the drift Signal embeds
        # it verbatim, and advertising a fix that ensure will refuse
        # sends operators in circles.
        return _PermCheck(
            category="cli-device", target=target, ok=False, detail=chk.detail,
        )
    parts: list[str] = []
    if chk.needs_repair:
        parts.append(
            f"widen CLI device scopes to {', '.join(_ocd.CLI_DEVICE_SCOPES)}"
        )
    if chk.needs_seed:
        parts.append("seed approved CLI device entry for the current identity")
    return _PermCheck(
        category="cli-device", target=target, ok=False,
        detail=chk.detail,
        fix_description=" + ".join(parts) + " + kickstart gateway",
        apply=lambda: _ocd.ensure_cli_device_scopes(bot_id, bot_user).ok,
    )


def ensure_pod_perms(
    bot_id: str | None = None,
    network_path: Path | None = None,
    check_only: bool = False,
) -> PodPermsResult:
    """Idempotently enforce the canonical pod-side perm contract.

    When `bot_id` is provided, runs only that bot's per-bot checks plus the
    pod-wide checks they depend on (proposals dir, Cellar perms). When None,
    runs the full pod-wide pass over every bot in `network.json`.

    Returns a PodPermsResult listing every check (passes + drifts) and, when
    `check_only=False`, every fix that was applied.

    The five contract layers (see incident note 2026-04-25 + the
    2026-05-25 evo-account-separation post-cutover dismiss bug):
      1. .openclaw/ ACL contains entries for the admin user (SUDO_USER),
         the evolve service user, and the security/audit bot configured
         in network.security.botId — see `pod_acl_users(network)`
      2. /opt/homebrew/Cellar/python@3.14 is g+rX o+rX
      3. {sharedDir}/proposals/ + standard subdirs are mode 1777
      4. {sharedDir}/{proposals,signals}/ grant the evo user a write+
         inherit ACL (only if the `evo` macOS user exists, i.e. post
         Phase E.2.a of spec-evo-account-separation-2026-05-25.md)
      5. each bot's own CLI device in ~/.openclaw/devices/paired.json
         carries the full operator scope set (read/write/pairing) — the
         OC 2026.6 upgrade narrowed these pod-wide and broke `openclaw
         message send` + defer fires; day-1 bots get a pre-seeded
         approved entry (see oc_cli_device module docstring)
      7. token-bearing bot config files are mode 0600 (secret_config_perms).
      8. {sharedDir}/{bot}/ grants a dir-level write ACE to BOTH its writers — the bot user (tier_prefs_acl) and the evolve user (shared_bot_dir_perms); the dir has no owner guarantee, so whoever lost the mkdir race needs the ACE to write its own file there.

    Idempotency: every fix is gated on a pre-check; a re-run on a correct
    pod produces zero `applied` entries and never invokes a recursive chmod
    or restarts a daemon.
    """
    from .config import DEFAULT_NETWORK_CONFIG, load_network, get_bot_user

    np = network_path or DEFAULT_NETWORK_CONFIG
    network = load_network(np)
    shared_dir = Path(network.get("sharedDir") or _CANONICAL_SHARED_DIR)
    _dres.plant_never_index_marker(shared_dir, via_sudo=False, enabled=not check_only)  # Part A: pod Spotlight marker

    # Track (bot_id, bot_user) pairs — they differ when a bot's OS account name
    # was renamed after initial deploy (e.g. team_bot_b→personal_bot_user). bot_id is the logical
    # name used by --bot-id / plist labels / lock filenames; bot_user is the macOS
    # account that owns files and runs the daemon.
    if bot_id is not None:
        bot_pairs = [(bot_id, get_bot_user(bot_id, network))]
    else:
        members = network.get("members") or list((network.get("bots") or {}).keys())
        bot_pairs = [(m, get_bot_user(m, network)) for m in members]

    result = PodPermsResult(bot_ids=[u for _, u in bot_pairs])

    # ── Pod-wide checks ──────────────────────────────────────────────────────
    result.checks.extend(_check_proposals_dir(shared_dir))
    result.checks.extend(_check_alerts_dir(shared_dir) + _secret_perms.check_shared_secret_modes(shared_dir) + _secret_perms.check_shared_directory_modes(shared_dir) + _secret_perms.check_keystore_secret_modes(shared_dir) + _secret_perms.check_keystore_dir_modes(shared_dir) + [_evo_gw.check_evolve_gateway_client(network)])  # + evolve-owned Google secret-key 0600 self-heal + directory/ contact-PII 0600 self-heal + keystore machine-key 0640 / admin-auth-key 0600 self-heal (the a+rX re-exposer, 2026-08-18) + keystore/ + keystore/vault/ 0750 dir-mode self-heal, guarded on the evo traverse ACE + admin→evo gateway client-token drift self-heal (EVO-SEP-GW-CRED) (folded: deploy.py at no-growth cap; rationale in the helper docstrings)
    # Single-writer evolve-owned shared subdirs (see EVOLVE_OWNED_SHARED_SUBDIRS
    # docstring): every entry gets a [dir-mode, dir-owner] pair, so the
    # 2026-06-06 first-writer-wins bug class can't recur silently on any
    # of these dirs. pod_perms_drift_monitor (hourly) catches re-occurrences
    # between deploys.
    for subdir in EVOLVE_OWNED_SHARED_SUBDIRS:
        result.checks.extend(_check_evolve_owned_dir(shared_dir, subdir))
    # Post-evo-separation: evo write ACL on the shared subdirs it mutates (no-op
    # pre-sep). The admin-daemon socket gets a shared-bot-group connect ACE (Linux).
    for subdir in EVO_WRITE_SHARED_SUBDIRS:
        result.checks.append(_check_evo_write_acl(shared_dir, subdir))
    result.checks.append(_evo_sock_acl._check_bot_socket_acl(shared_dir))
    # Store lock files (7.1 Phase A): pre-creation is load-bearing for
    # bot-user writers — see STORE_LOCK_FILE_NAME docstring.
    for subdir in STORE_LOCK_SUBDIRS:
        result.checks.append(_check_store_lock_file(shared_dir, subdir))
    result.checks.append(_check_cellar_perms())

    # ── Per-bot checks ───────────────────────────────────────────────────────
    acl_allow = pod_acl_users(network)
    for bid, bot_user in bot_pairs:
        result.checks.extend(_check_bot_acl(bot_user, acl_allow))
        # The evolve read/write contract as a self-healing drift check (the
        # architectural end of the round-6/7/8 mask-clamp whack-a-mole — see
        # secret_config_perms.check_evolve_access). Linux-only signal, macOS no-op.
        result.checks.append(_secret_perms.check_evolve_access(bid, bot_user))
        result.checks.append(_check_cli_device_scopes(bid, bot_user))
        result.checks.extend(_secret_perms.check_bot_secret_modes(bot_user) + _secret_perms.check_bot_tiers_ownership(bot_user))
        # Bot-user write ACE on {sharedDir}/{bid}/ for user-tier-prefs.json
        # temp+rename (PR #3562 follow-up) — self-heal backstop to the
        # at-deploy grant in fix_shared_dir_permissions.
        result.checks.append(_tier_prefs_acl.check_bot_tier_prefs_acl(shared_dir, bid, bot_user))
        result.checks.append(_bot_dir_perms.check_evolve_bot_dir_acl(shared_dir, bid))  # evolve-direction twin

    # App-cron PATH self-heal: re-install launchd app-cron plists missing a PATH
    # (openclaw exit-127 silent-non-delivery, 2026-06-22). Wired here so a stale
    # plist heals on the affected bot's next deploy instead of only via the manual
    # `application repair-app-crons` CLI. Reuses the tested installer; best-effort
    # + non-fatal (a heal failure must never abort a deploy). Body lives in
    # install_helpers (deploy.py is at its no-growth cap).
    from .applications.install_helpers import heal_app_cron_paths_into
    heal_app_cron_paths_into(result, _PermCheck, [bid for bid, _ in bot_pairs],
                             shared_dir, network, check_only, _log)

    # ── Apply phase ──────────────────────────────────────────────────────────
    if not check_only:
        for c in result.checks:
            if c.ok or c.apply is None:
                continue
            try:
                ok = bool(c.apply())
            except Exception as e:
                result.errors.append(f"{c.category}/{c.target}: fix failed: {e}")
                continue
            if ok:
                # Re-run the check would be the gold standard, but we trust the
                # fix for now — the next ensure_pod_perms() pass verifies.
                c.ok = True
                result.applied.append(f"{c.category}/{c.target}: {c.fix_description}")
            else:
                result.errors.append(
                    f"{c.category}/{c.target}: fix did not return success"
                )

    return result


def _scheduler_launchctl(*args: str) -> tuple[int, str]:
    """Issue a raw launchctl verb through the Scheduler seam (4.3C S2).

    Call sites that map onto the Scheduler protocol use the verbs directly
    (``install`` / ``remove`` / ``restart``). The leftover bootout/bootstrap-
    of-an-existing-plist rituals (pre-rendered plist content, call-site-
    specific error protocols) go through the adapter's raw plumbing instead,
    so the argv still flows through the injectable runner and tests never
    spawn a real launchctl. Returns ``(rc, combined stripped output)``;
    no-op ``(0, "")`` when a non-launchd scheduler is injected.
    """
    fn = getattr(get_scheduler(), "_launchctl", None)
    if fn is None:  # pure-protocol fake or future non-launchd adapter
        return 0, ""
    return fn(*args)


def _wait_for_launchd_unload(label: str, timeout_seconds: float = 5.0) -> None:
    """Wait for a system launchd service to fully unload after ``bootout``.

    ``launchctl bootout system/<label>`` returns before tear-down completes;
    an immediate follow-up ``bootstrap`` of the same label then fails with
    "Service is being unloaded" / "Service already loaded". Poll until
    ``launchctl print system/<label>`` reports the service is gone (empty
    stdout — Apple's CLI returns 0 in both states, so the returncode is not
    a usable signal; stderr is also unusable, "Could not find service" lands
    there). Bounded so a stuck service can't wedge a deploy.
    """
    raw = getattr(get_scheduler(), "_launchctl_raw", None)
    if raw is None:  # non-launchd scheduler: nothing to settle
        return
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        _rc, out, _err = raw("print", f"system/{label}")
        if not out.strip():
            return
        time.sleep(0.1)


def _wait_for_gateway_port(
    bot_id: str,
    port: int,
    *,
    timeout_seconds: float = 20.0,
    host: str = "127.0.0.1",
) -> bool:
    """Poll ``127.0.0.1:<port>`` until the gateway is accepting connections.

    ``launchctl bootstrap`` returns the moment launchd has accepted the
    job spec; the Node gateway process then forks, initialises, and only
    binds its listening socket some seconds later. Callers that probe
    the port immediately (smoke audit, version preflight) race the
    bootstrap and fire spurious ``gateway.probe_failed`` signals.

    Returns True once a TCP connect succeeds, or False on timeout.
    Emits a one-line info log at the halfway mark so we have observability
    if boot time ever drifts upward.
    """
    import socket as _sock

    t0 = time.monotonic()
    deadline = t0 + timeout_seconds
    half = timeout_seconds / 2.0
    half_logged = False
    while time.monotonic() < deadline:
        try:
            with _sock.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            pass
        if not half_logged and (time.monotonic() - t0) >= half:
            _log.info(
                "install_bot_gateway_plist(%s): gateway port %d not yet open after %.0fs — still waiting",
                bot_id, port, half,
            )
            half_logged = True
        time.sleep(0.5)
    return False


def _install_job_ensuring_restart(spec: JobSpec) -> tuple[bool, str]:
    """``Scheduler.install()`` + the legacy always-bounce guarantee, for the
    job shapes that actually need it.

    The pre-seam install ritual unconditionally ran bootout+bootstrap, so
    every call restarted the daemon — call sites like the gateway deploy
    rely on that (a config change needs a restarted gateway to take
    effect). ``install()`` skips the bounce when the on-disk unit is
    byte-identical, so on the skip path we issue an explicit ``restart()``
    to preserve the restart-happens guarantee. If the restart fails (e.g.
    plist on disk but the service was booted out, which kickstart can't
    recover), fall back to a full remove+install cycle — the effective
    equivalent of what the legacy always-bounce path did.

    **That bounce is for long-running daemons only.** ``restart()`` is
    ``kickstart -k`` / ``systemctl restart <label>.service`` — on a
    timer/calendar-activated oneshot that does not "restart" anything, it
    RUNS THE JOB NOW (see ``SystemdScheduler.restart``'s own docstring).
    Units are byte-identical on nearly every deploy (the repo content
    changed, not the unit files), and the repo-puller deploys every 15 min,
    so the skip path was force-running every timer job on the pod several
    times a day: ~30 ``systemctl restart …service`` calls in one measured
    2026-08-19 VPS deploy. That is wrong twice over —

    - **cost**: ``model_liveness_monitor`` dispatches a REAL ``openclaw
      agent`` turn per routed model (~$0.89/run). It ran 6× on 2026-08-19
      (1 scheduled + 5 deploy-triggered), tripping darwin's $5.00/day
      breaker;
    - **cadence**: sweep-style monitors calling
      ``signals.store.sweep_resolve`` are designed around a known cadence
      and a settled pod, and were instead firing mid-deploy while gateways
      may be mid-restart.

    A timer oneshot needs no bounce: the unit is byte-identical, and the
    next scheduled firing execs the new script from disk anyway. So the
    skip path consults :func:`is_timer_activated_oneshot` (a JobSpec-level
    predicate, so launchd and systemd agree) and bounces only daemons.
    ``run_at_load=False`` was NEVER protection against this — it shapes
    unit CONTENT, and the restart bypasses unit content entirely.

    Known, accepted loss: on macOS the failed-``kickstart`` fallback used to
    re-register a timer job whose plist was on disk but booted out. Timer
    jobs no longer reach that fallback. The condition was already
    unrecoverable on Linux (``systemctl restart`` of a oneshot service
    succeeds without re-arming its timer, so the fallback never fired), and
    ``monitor_coverage`` already fires ``monitor_silent`` for a monitor whose
    log has gone stale — so detection is kept and both platforms now behave
    identically, which is the point.

    Regression guard: #3362. A ``deploy <bot>`` bounces the gateway ONLY as
    this side effect — on a plugin-only change the plist is byte-identical,
    so the skip-path ``restart()`` IS the bounce. The gateway spec carries
    ``keep_alive=True``, so it is not a timer oneshot and keeps every bit of
    the behaviour above. See ``deploy_steps``'s module docstring.
    """
    scheduler = get_scheduler()
    res = scheduler.install(spec)
    if not res.ok or not res.skipped:
        return res.ok, res.message
    if is_timer_activated_oneshot(spec):
        # Byte-identical timer unit: nothing to bounce. Restarting here would
        # RUN the job, not restart it.
        return True, f"{res.message}; no bounce (timer-activated oneshot)"
    r_ok, _r_msg = scheduler.restart(spec.label)
    if r_ok:
        return True, f"{res.message}; restarted"
    scheduler.remove(spec.label)
    retry = scheduler.install(spec)
    return retry.ok, retry.message


def _install_spec_via_seam(spec: JobSpec, result: DeployResult) -> None:
    """Install a custom-shaped daemon JobSpec through the Scheduler seam.

    A systemd unit on Linux, a byte-identical launchd plist on macOS. For the
    KeepAlive / WatchPaths daemons whose specs can't be expressed via
    ``_job_spec_for`` (admin-ui, mcp-bridge, signal-subscriber, better-engine).

    Unlike :func:`_install_job_ensuring_restart` this does NOT force a restart
    on the byte-identical skip path — it preserves the old ``_write_plist``
    no-bounce contract so the puller's deploy hook doesn't bounce these
    long-running daemons on every PR that merely touches deploy.py.
    """
    res = get_scheduler().install(spec)
    if res.ok:
        result.log(res.message if res.skipped else f"Installed launchd: {spec.label}")
    else:
        result.error(f"Cannot install {spec.label}: {res.message}")


def _ensure_gateway_mode_seeded(bot_id: str, bot_user: str) -> None:
    """W10-F #2: gap-fill ``gateway.mode`` (+ ``bind``) before the gateway
    starts, so it binds first-try instead of crash-looping the bind-wait on
    "existing config is missing gateway.mode". Only the startup-blocking fields
    are touched; the full ``ensure_plugin_config`` reconcile still runs later
    and is idempotent. No-op (no write, no validate) when gateway.mode is
    already present — steady state and macOS are untouched. Best-effort: any
    read/write hiccup is logged + swallowed (the bind-wait is the backstop)."""
    oc_json = _user_home(bot_user) / ".openclaw/openclaw.json"
    content: "str | None" = None
    try:
        content = oc_json.read_text()
    except (FileNotFoundError, PermissionError, OSError):
        r = subprocess.run(
            ["sudo", "/bin/cat", str(oc_json)],
            capture_output=True, text=True, cwd=_PROFILE.scratch_dir,
        )
        content = r.stdout if r.returncode == 0 else None
    if not content:
        return  # not-yet-written config — the bind-wait will surface a real failure
    try:
        cfg = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return
    gw = cfg.get("gateway")
    if isinstance(gw, dict) and gw.get("mode"):
        return  # already seeded — the common path (and all of macOS)
    if not isinstance(gw, dict):
        gw = {}
        cfg["gateway"] = gw
    gw["mode"] = "local"
    gw.setdefault("bind", "loopback")
    ok, err = safe_write_bot_config(
        bot_id, cfg, reason="W10-F: seed gateway.mode pre-start", bot_user=bot_user,
    )
    if not ok:
        _log.warning(
            "install_bot_gateway_plist(%s): pre-start gateway.mode seed failed "
            "(continuing; bind-wait is the backstop): %s", bot_id, err)


def _is_provisionable_bot(bot_id: str, network: "dict[str, Any] | None" = None) -> bool:
    """True iff ``bot_id`` is an authoritative pod member (so it may get a
    gateway unit + openclaw.json), False for any on-disk residue / phantom.

    The single roster authority is ``network["members"]`` (the same source
    every per-bot deploy loop uses), unioned with the resolved primary id —
    the primary is always a bot and always provisionable even on a network
    whose ``members`` array predates the explicit-primary field. Legacy pods
    that have no ``members`` array fall back to ``bots.keys()``.

    This is the fail-closed gate that stops EVO-GATEWAY-RESIDUE-RERENDER: on a
    post-evo-account-separation pod (primary "evo", members [darwin, evo], NO
    "evolve" bot) a phantom ``bot_id="evolve"`` reaching the gateway-install
    funnel — from a stale on-disk unit, a literal roster injection, or any
    discovery sourced from ``/home/*/.openclaw`` residue rather than the roster
    — is refused here, so the deploy can never (re)render
    ``ai.openclaw.evolve-gateway`` or seed ``/home/evolve/.openclaw/openclaw.json``
    for it. The ``evolve`` service account legitimately runs the admin daemon
    and carries no bot-shaped config (spec-evo-account-separation-2026-05-25).
    """
    if network is None:
        try:
            network = load_network(_CANONICAL_NETWORK_JSON)
        except Exception:
            # No readable network → cannot prove membership → fail-closed only
            # for a bot we can't otherwise vouch for. Return False so the caller
            # refuses rather than silently materializing a phantom.
            return False
    members = network.get("members")
    roster: set[str] = set()
    if isinstance(members, list) and members:
        roster = {m for m in members if isinstance(m, str) and m}
    else:
        roster = set((network.get("bots") or {}).keys())
    # Union the resolved primary — always a bot, always provisionable.
    try:
        from primary_bot import primary_bot_id as _pbid  # type: ignore
        primary = _pbid(network)
        if isinstance(primary, str) and primary:
            roster.add(primary)
    except Exception:
        pass
    return bot_id in roster


def install_bot_gateway_plist(
    bot_id: str, port: int, user: str | None = None,
) -> tuple[bool, str]:
    """Write and bootstrap the system LaunchDaemon plist for a bot's OpenClaw gateway.

    `user` is the resolved Unix username for the bot; may differ from `bot_id`
    (e.g. when one bot lives on a personal/shared account, bot_id != user). Defaults to bot_id for back-compat;
    that path is only correct when bot_id == os user, and breaks the deploy
    when they differ — the gateway plist gets `UserName=<bot_id>` and writes
    logs to /Users/<bot_id>/.openclaw/logs/, even though the actual macOS
    account lives at /Users/<user>/.

    This is the same approach used by the setup wizard for the evolve user.
    Required for headless bot users who have no GUI session — user-level
    LaunchAgents (~/Library/LaunchAgents/) only run when the user is logged in.

    Returns ``(success, detail)``:
      * ``(True, "...")``  — plist installed and bootstrapped on `port`.
      * ``(False, "...")`` — first failure point with its subprocess stderr.

    The detail string is what the wizard's UI surfaces in the live log.
    Without it, the wizard could only emit a canned "manual recovery: run
    launchctl bootstrap…" hint, which hid the *real* error (missing
    sudoers grant, port already in use, bad plist, etc.) and forced every
    failure into the same debug-this-by-hand path.
    """
    plist_user = user or bot_id
    label = f"ai.openclaw.{bot_id}-gateway"
    log_dir = _user_home(plist_user) / ".openclaw/logs"

    # Platform-keyed node/openclaw resolution: macOS keeps the Homebrew search
    # order UNCHANGED; Linux appends NodeSource/apt locations. Before this a
    # Linux pod found neither and fell back to /opt/homebrew/... → the rendered
    # systemd ExecStart=/opt/homebrew/bin/node then 203/EXEC crash-looped.
    node_search = "/opt/homebrew/bin:/usr/local/bin:/opt/homebrew/opt/node/bin"
    if _PROFILE.name == "linux":
        node_search += ":/usr/bin:/bin"  # NodeSource/apt node lives in /usr/bin
    node_bin = shutil.which("node", path=node_search) or (
        "/usr/bin/node" if _PROFILE.name == "linux" else "/opt/homebrew/bin/node"
    )
    oc_candidates = [
        "/opt/homebrew/lib/node_modules/openclaw/dist/index.js",
        "/opt/homebrew/lib/node_modules/openclaw/dist/entry.js",
        "/opt/homebrew/lib/node_modules/openclaw/openclaw.mjs",
        "/usr/local/lib/node_modules/openclaw/dist/index.js",
        "/usr/local/lib/node_modules/openclaw/dist/entry.js",
        "/usr/local/lib/node_modules/openclaw/openclaw.mjs",
        # Linux NodeSource global prefix — the box hotfix's missing candidate.
        "/usr/lib/node_modules/openclaw/dist/index.js",
        "/usr/lib/node_modules/openclaw/dist/entry.js",
        "/usr/lib/node_modules/openclaw/openclaw.mjs",
    ]
    # macOS keeps the [0] (Homebrew) default; Linux defaults to the NodeSource
    # prefix so a not-yet-installed openclaw never bakes a /opt/homebrew path
    # into a Linux unit. macOS candidates lead, so a resolved mac path is unchanged.
    oc_default = (
        "/usr/lib/node_modules/openclaw/dist/index.js"
        if _PROFILE.name == "linux" else oc_candidates[0]
    )
    oc_index = next((p for p in oc_candidates if Path(p).exists()), oc_default)

    spec = JobSpec(
        label=label,
        comment=(
            f"OpenClaw Gateway for {bot_id} "
            f"(runs as {plist_user}, headless, survives GUI logout)"
        ),
        run_at_load=True,
        keep_alive=True,
        throttle_interval=5,
        user=plist_user,
        group_name="staff",
        umask=63,
        program_args=[node_bin, oc_index, "gateway", "--port", str(port)],
        stdout_path=str(_user_home(plist_user) / ".openclaw/logs/gateway.log"),
        stderr_path=str(_user_home(plist_user) / ".openclaw/logs/gateway.err.log"),
        env={
            "HOME": str(_user_home(plist_user)),
            "TMPDIR": "/tmp",
            # macOS PATH is UNCHANGED; Linux drops the /opt/homebrew prefix
            # (absent there) so no Homebrew path leaks into the systemd unit.
            "PATH": (
                "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
                if _PROFILE.name == "macos" else "/usr/local/bin:/usr/bin:/bin"
            ),
            # W10-F #4: platform-keyed — Ubuntu has no /etc/ssl/cert.pem.
            "NODE_EXTRA_CA_CERTS": _PROFILE.ca_bundle,
            "OPENCLAW_GATEWAY_PORT": str(port),
            "OPENCLAW_LAUNCHD_LABEL": label,
            "OPENCLAW_SERVICE_MARKER": "openclaw",
            "OPENCLAW_SERVICE_KIND": "gateway",
        },
    )

    def _run(cmd: list[str], step: str) -> tuple[bool, str]:
        """Run a subprocess and return (ok, error-detail-if-not-ok).

        Captures stderr — without it, sudoers misses show up as the
        opaque "Command returned non-zero exit status 1" with no clue
        which grant is missing.
        """
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            stderr = (r.stderr or "").strip() or (r.stdout or "").strip()
            err = f"{step}: `{' '.join(cmd)}` exit {r.returncode}: {stderr}"
            _log.warning("install_bot_gateway_plist(%s): %s", bot_id, err)
            return False, err
        return True, ""

    try:
        # Ensure log dir exists and is owned by the bot's macOS user.
        # These two are non-fatal on the happy path (the dir often
        # exists already from openclaw_onboard), but we still surface
        # their stderr if they DO fail — that's usually a permissions
        # issue worth showing the operator.
        ok, err = _run(
            ["sudo", "/bin/mkdir", "-p", str(log_dir)],
            "create log dir",
        )
        if not ok:
            return False, err
        ok, err = _run(
            ["sudo", _PROFILE.chown, f"{plist_user}:staff", str(log_dir)],
            "chown log dir",
        )
        if not ok:
            return False, err

        # W10-F #2: seed gateway.mode before start so the gateway binds first-try
        # (missing-mode crash-loops the bind-wait). No-op when seeded; see docstring.
        _ensure_gateway_mode_seeded(bot_id, plist_user)

        # OC 2026.6+ auth: per-agent sqlite store; helper now verify-driven (#3136).
        from .oc_auth_provision import ensure_agent_auth_store_imported
        _ok, _msg = ensure_agent_auth_store_imported(bot_id, plist_user)
        if not _ok:
            _log.warning("auth store NOT provisioned for %s: %s", bot_id, _msg)

        # The Scheduler seam owns the plist write + registration ritual
        # (/tmp staging, sudo cp + chown root:wheel + chmod 644, bootout,
        # unload-settle wait, bootstrap). The restart-ensuring wrapper
        # preserves the legacy guarantee that every call bounces the
        # gateway — config changes need a restarted gateway to take effect.
        ok, err = _install_job_ensuring_restart(spec)
        if not ok:
            err = f"install {label}: {err}"
            _log.warning("install_bot_gateway_plist(%s): %s", bot_id, err)
            return False, err

        # bootstrap returns when the scheduler accepts the spec, but the gateway
        # needs ~5–10s (cold first-boot VPS ~25s) to fork + bind; wait so the
        # smoke audit doesn't race the bind and fire gateway.probe_failed. The
        # 45s ceiling is a no-op for a warm box and only adds patience to a fail.
        bind_timeout_s = 45.0
        if not _wait_for_gateway_port(bot_id, port, timeout_seconds=bind_timeout_s):
            err = (
                f"port-bind: gateway did not bind port {port} within "
                f"{bind_timeout_s:.0f}s after bootstrap (process started but "
                "never opened the listener — check the bot's gateway.err.log)"
            )
            _log.warning("install_bot_gateway_plist(%s): %s", bot_id, err)
            return False, err
        msg = f"installed and bootstrapped on port {port}"
        _log.info("install_bot_gateway_plist(%s): %s", bot_id, msg)
        return True, msg
    except Exception as e:
        err = f"unexpected {type(e).__name__}: {e}"
        _log.warning("install_bot_gateway_plist(%s): %s", bot_id, err)
        return False, err


def _restart_gateway_linux(bot_id: str, bot_user: str) -> None:
    """Linux gateway (re)start: restart the systemd unit, or install it if absent.

    On macOS ``restart_gateway`` keys off
    ``/Library/LaunchDaemons/<label>.plist`` and falls back to
    ``openclaw gateway restart`` when no system daemon exists. On Linux that
    plist path NEVER exists, so the legacy code silently dropped to that
    user-daemon fallback — which on a fresh pod does not create the persistent
    systemd unit, binds nothing on the port, and yet returned success (the
    wizard then printed a phantom "✓ Gateway restarted"). The gateway install
    was deferred at bot-creation ("venv not built yet → install during deploy"),
    so without this nothing ever installs the unit (W10-E).

    Drive the systemd seam instead: restart when the unit is installed; install
    it (idempotent — ``install_bot_gateway_plist`` bootstraps the unit and waits
    for the port to bind) when it is not. Either failure raises LOUD.
    """
    label = f"ai.openclaw.{bot_id}-gateway"
    sched = get_scheduler()
    network = load_network(_CANONICAL_NETWORK_JSON)
    # Fail-closed roster gate + stale-unit reap (EVO-GATEWAY-RESIDUE-RERENDER).
    # A non-member must never be (re)started OR (re)installed here. If a stale
    # unit for it is on disk (e.g. an ai.openclaw.evolve-gateway left by an
    # older build or a literal-roster injection), REAP it instead of restarting
    # — restarting would just re-arm the crash-loop on the primary's port. This
    # mirrors the #3167 orphan-sweeper but guarantees it on EVERY restart path,
    # in the same run, so the sweep can never lose a race to a re-provision.
    if not _is_provisionable_bot(bot_id, network):
        if sched.status(label).get("installed"):
            _log.warning(
                "_restart_gateway_linux(%s): %s is on disk but %s is not a pod "
                "member — reaping the stale unit instead of restarting "
                "(EVO-GATEWAY-RESIDUE-RERENDER)",
                bot_id, label, bot_id,
            )
            try:
                ok, out = sched.remove(label)
                if not ok:
                    _log.warning(
                        "_restart_gateway_linux(%s): reap of stale unit %s "
                        "reported failure: %s", bot_id, label, out,
                    )
            except Exception as exc:
                _log.warning(
                    "_restart_gateway_linux(%s): could not reap stale unit %s: %s",
                    bot_id, label, exc,
                )
        else:
            _log.info(
                "_restart_gateway_linux(%s): not a pod member and no unit on "
                "disk — nothing to do", bot_id,
            )
        return
    if sched.status(label).get("installed"):
        ok, out = sched.restart(label)
        if not ok:
            raise RuntimeError(f"systemctl restart {label} failed: {out}")
        return
    # Unit absent — install it. Resolve the gateway port from network config
    # (falls back to the bot's openclaw.json inside get_bot_port).
    port = get_bot_port(bot_id, network)
    if not port:
        raise RuntimeError(
            f"cannot install gateway for {bot_id}: no port in "
            f"{_CANONICAL_NETWORK_JSON} and no openclaw.json port to fall back on"
        )
    ok, detail = install_bot_gateway_plist(bot_id, port, user=bot_user)
    if not ok:
        raise RuntimeError(f"gateway install for {bot_id} failed: {detail}")


def restart_gateway(bot_id: str, bot_user: str | None = None) -> None:
    """Step 7: Restart the bot's OpenClaw gateway (system daemon or user daemon).

    Before kickstarting, kill ALL processes on the gateway port.
    launchctl kickstart -k only kills the launchd-tracked PID; orphaned processes
    from prior manual starts or crash/restart races survive and hold the port,
    causing the new gateway to fail with EADDRINUSE.

    Also boots out any generic ai.openclaw.gateway user LaunchAgent that npm's
    post-install may have dropped.  If it's still loaded when the system daemon
    starts, both compete for the same port and the system daemon crash-loops.
    """
    bot_user = bot_user or _bot_user_for(bot_id)
    # Linux has no /Library/LaunchDaemons — route to the systemd seam, which
    # installs the unit if it is missing rather than no-op'ing into a phantom
    # success. macOS keeps the byte-identical body below.
    if _PROFILE.name == "linux":
        _restart_gateway_linux(bot_id, bot_user)
        return
    system_plist = Path(f"/Library/LaunchDaemons/ai.openclaw.{bot_id}-gateway.plist")

    # Boot out any conflicting user-level generic gateway agent first.
    # npm post-install drops ai.openclaw.gateway.plist into ~/Library/LaunchAgents/
    # on openclaw install/upgrade.  Renaming it to .DISABLED does NOT unload it —
    # we must explicitly bootout before kickstarting the system daemon.
    if system_plist.exists():
        try:
            uid_raw = subprocess.run(
                ["id", "-u", bot_user], capture_output=True, text=True
            ).stdout.strip()
            uid = int(uid_raw)
            # gui-domain bootout — the seam's default instance targets the
            # system domain, so this goes through the raw plumbing with an
            # explicit gui/<uid> service target. rc ignored (agent may not
            # be loaded), same as the legacy call.
            _scheduler_launchctl("bootout", f"gui/{uid}/ai.openclaw.gateway")
            # Rename plist to .DISABLED so it doesn't reload on next login.
            # with_suffix(".plist.DISABLED") replaces the last extension, producing
            # ai.openclaw.gateway.plist.DISABLED — .DISABLED is appended, not substituted.
            agent_plist = _user_home(bot_user) / "Library/LaunchAgents/ai.openclaw.gateway.plist"
            if agent_plist.exists():
                r = subprocess.run(
                    ["sudo", "/bin/mv", str(agent_plist),
                     str(agent_plist.with_suffix(".plist.DISABLED"))],
                    capture_output=True,
                )
                if r.returncode != 0:
                    _log.warning("restart_gateway(%s): could not rename user agent plist: %s",
                                 bot_id, r.stderr)
        except (ValueError, Exception) as exc:
            _log.warning("restart_gateway(%s): could not resolve uid or bootout user agent: %s",
                         bot_id, exc)

    # Kill every process currently listening on the bot's gateway port.
    try:
        import plistlib
        plist_path = system_plist if system_plist.exists() else None
        port: int | None = None
        if plist_path:
            with open(plist_path, "rb") as f:
                plist_data = plistlib.load(f)
            port_str = plist_data.get("EnvironmentVariables", {}).get("OPENCLAW_GATEWAY_PORT")
            if port_str:
                port = int(port_str)
        if port:
            r = subprocess.run(
                ["sudo", _PROFILE.lsof, "-ti", f":{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True,
            )
            for pid in r.stdout.strip().split():
                if pid.strip():
                    subprocess.run(["sudo", "/bin/kill", "-9", pid.strip()],
                                   capture_output=True)
    except Exception:
        pass  # best-effort; kickstart may still succeed

    if system_plist.exists():
        ok, out = get_scheduler().restart(f"ai.openclaw.{bot_id}-gateway")
        if not ok:
            # The legacy check=True raised on failure; callers
            # (restart_all_gateways, deploy step 8) catch and report.
            raise RuntimeError(
                f"launchctl kickstart -k system/ai.openclaw.{bot_id}-gateway "
                f"failed: {out}"
            )
    else:
        run_cmd(
            ["sudo", "-H", "-u", bot_user,
             _openclaw_bin(), "gateway", "restart"],
            cwd=_PROFILE.scratch_dir,
        )


def restart_all_gateways(bot_ids: list[str]) -> dict[str, str]:
    """Restart every listed bot's gateway.

    Calls restart_gateway() for each bot in sequence, which kills any orphaned
    process on the port then does ``launchctl kickstart -k``.

    Returns a dict of bot_id → "ok" | error message.
    """
    results: dict[str, str] = {}
    for bot_id in bot_ids:
        try:
            restart_gateway(bot_id)
            results[bot_id] = "ok"
        except Exception as exc:
            results[bot_id] = str(exc)
    return results


def install_staged_plists() -> None:
    """Install plists staged in /Users/Shared/evolve/plists/ into /Library/LaunchDaemons.

    Hash-compares staged vs live: if identical, no-op; if different (or missing),
    bootout + replace + bootstrap so content drift in staged plists actually
    reaches the running launchd job (predicate-drift fix — see PR #312, PR #317).
    """
    import filecmp
    plists_dir = Path("/Users/Shared/evolve/plists")
    if not plists_dir.exists():
        return
    for plist in plists_dir.glob("*.plist"):
        dst = Path("/Library/LaunchDaemons") / plist.name
        if dst.exists() and filecmp.cmp(plist, dst, shallow=False):
            continue  # content identical — no-op
        label = plist.stem
        # Staged plists are pre-rendered files (no JobSpec at this call
        # site), so the registration ritual uses the seam's raw verbs.
        # Bootout any existing instance before swapping the file
        # (best-effort — ok if it fails, i.e. not loaded yet).
        _scheduler_launchctl("bootout", f"system/{label}")
        _wait_for_launchd_unload(label)
        subprocess.run(["sudo", "/bin/cp", str(plist), str(dst)], check=True)
        subprocess.run(["sudo", _PROFILE.chown, f"root:{_PROFILE.admin_group}", str(dst)], check=True)
        subprocess.run(["sudo", "/bin/chmod", "644", str(dst)], check=True)
        rc, out = _scheduler_launchctl("bootstrap", "system", str(dst))
        if rc != 0:
            # Legacy check=True raised on bootstrap failure; keep that
            # contract so a wedged install still aborts loudly.
            raise RuntimeError(
                f"launchctl bootstrap system {dst} failed (rc={rc}): {out}"
            )


def verify_plugin_live(bot_id: str, port: int | None) -> str | None:
    """Step 8: Return a status string if the plugin responds, else None. Also checks admin UI."""
    if not port:
        return None
    import time
    import urllib.request
    time.sleep(3)
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/evolve/status", timeout=5
        ) as resp:
            data = json.loads(resp.read())
            if data.get("bot_id") == bot_id:
                # Check admin UI (non-fatal)
                try:
                    with urllib.request.urlopen("http://localhost:5050/api/status", timeout=5) as r:
                        if r.status == 200:
                            pass  # admin UI URL shown in final summary
                except Exception:
                    pass
                return f"{bot_id} plugin live at :{port}"
    except Exception:
        pass
    return None


def _imessage_poller_plist_label(bot_id: str) -> str:
    """Return the launchd label for a bot's iMessage poller daemon."""
    return f"ai.evolve.evolve.imessage-poller.{bot_id}"


def _imessage_poller_plist_spec(
    label: str, bot_id: str, port: int, analyzer_dir: Path, python3: str,
) -> JobSpec:
    """Build the iMessage poller LaunchDaemon JobSpec (pure — no disk access)."""
    return JobSpec(
        label=label,
        comment=f"Evolve iMessage poller for {bot_id} (reads chat.db, forwards to gateway)",
        program_args=[
            python3, "-m", "imessage_plugin.poller",
            "--bot-id", bot_id, "--port", str(port),
        ],
        user="evolve",
        group_name="staff",
        keep_alive=True,
        run_at_load=True,
        throttle_interval=30,
        working_dir=str(analyzer_dir),
        stdout_path=str(_CANONICAL_SHARED_DIR / f"logs/imessage-poller-{bot_id}.log"),
        stderr_path=str(_CANONICAL_SHARED_DIR / f"logs/imessage-poller-{bot_id}.err.log"),
        env={
            "HOME": str(_user_home("evolve")),
            "TMPDIR": "/tmp",
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
    )


def _imessage_poller_plist_content(
    label: str, bot_id: str, port: int, analyzer_dir: Path, python3: str,
) -> str:
    """Render the iMessage poller LaunchDaemon plist (pure — no disk access)."""
    return render_launchd_plist(
        _imessage_poller_plist_spec(label, bot_id, port, analyzer_dir, python3)
    )


def install_imessage_poller(
    bot_id: str,
    port: int,
    *,
    bot_user: str | None = None,
    analyzer_dir: Path | None = None,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Install (or update) the iMessage poller LaunchDaemon for a bot.

    .. deprecated:: 2026-06-04 (bundled-plugin rewire)

        Dead code. The bundled @openclaw/imessage plugin owns the runtime
        adapter now; this LaunchDaemon's HTTP target (``/api/chat``) was
        never a real endpoint on OC's gateway, so the daemon was inert
        even when "installed". Callers should not call this function;
        ``deploy_bot`` step 10 now calls ``uninstall_imessage_poller``
        instead to tear down any orphan daemons from pre-rewire deploys.
        Function kept on disk for one PR cycle so the cleanup diff stays
        reviewable; deletion is queued as a follow-on.

    Called by deploy_bot when the bot has iMessage configured as a skill.
    Only installs if ~/.openclaw/skills/imessage.json exists and is non-empty.

    The poller runs as the evolve user (it only needs to read chat.db via FDA
    and HTTP-POST to the gateway — no GUI session required). We use a
    LaunchDaemon (system-level, always-on) rather than a LaunchAgent (GUI
    session) because the evolve user on the mini has no persistent GUI session.

    Args:
        bot_id: Logical bot id.
        port: Gateway port for this bot.
        bot_user: Optional macOS account name override.
        analyzer_dir: Path to the analyzer package (for the poller script).
        dry_run: If True, log the plan but don't install.

    Returns:
        (success, message)
    """
    import json as _json

    # Check if iMessage is configured for this bot
    from .config import bot_home as _bot_home
    from .skills.imessage_install import IMESSAGE_CONFIG_PATH
    cfg_path = _bot_home(bot_id) / IMESSAGE_CONFIG_PATH

    # Try to read config; fall back to sudo /bin/cat
    config: dict = {}
    try:
        if cfg_path.exists():
            config = _json.loads(cfg_path.read_text(encoding="utf-8"))
    except PermissionError:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(cfg_path)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                config = _json.loads(r.stdout)
        except Exception:
            pass
    except (OSError, _json.JSONDecodeError):
        pass

    if not config:
        # iMessage not configured — no poller needed
        return True, f"iMessage not configured for {bot_id}; poller not installed"

    label = _imessage_poller_plist_label(bot_id)
    plist_path = LAUNCHD_DIR / f"{label}.plist"

    # Locate the poller script
    if analyzer_dir is None:
        analyzer_dir = Path(__file__).parent.parent.parent / "analyzer"

    # Use the python3 that's running this code (matches the evolve user's env)
    import sys as _sys
    python3 = _sys.executable or "/opt/homebrew/bin/python3"

    spec = _imessage_poller_plist_spec(
        label=label, bot_id=bot_id, port=port,
        analyzer_dir=analyzer_dir, python3=python3,
    )

    if dry_run:
        return True, f"[dry-run] Would install {plist_path}"

    try:
        # Seam install owns staging + cp/chown/chmod + bootout/bootstrap.
        # (Deprecated dead code — the byte-identical install skip is fine
        # here; nothing depends on this daemon bouncing.)
        res = get_scheduler().install(spec)
        if not res.ok:
            _log.warning("install_imessage_poller(%s): %s", bot_id, res.message)
            return False, res.message
        return True, f"iMessage poller installed: {label}"
    except Exception as exc:
        _log.error("install_imessage_poller(%s): failed: %s", bot_id, exc)
        return False, f"install failed: {exc}"


def uninstall_imessage_poller(bot_id: str) -> tuple[bool, str]:
    """Remove the iMessage poller LaunchDaemon for a bot.

    Idempotent — safe to call even if the poller was never installed.
    """
    label = _imessage_poller_plist_label(bot_id)
    plist_path = LAUNCHD_DIR / f"{label}.plist"

    if not plist_path.exists():
        # Early return BEFORE remove(): deploy_bot step 10 only logs the
        # teardown when the message says "removed", so the common
        # nothing-installed case must keep this distinct wording.
        return True, f"iMessage poller not installed for {bot_id}"

    try:
        ok, msg = get_scheduler().remove(label)
        if not ok:
            return False, f"uninstall failed: {msg}"
        return True, f"iMessage poller removed: {label}"
    except Exception as exc:
        return False, f"uninstall failed: {exc}"


@dataclass
class DeployResult:
    bot_id: str
    success: bool
    steps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def log(self, msg: str) -> None:
        self.steps.append(msg)

    def error(self, msg: str) -> None:
        self.errors.append(msg)
        self.success = False


def deploy_bot(
    bot_id: str,
    role: str,
    port: int | None,
    network_path: Path,
    dry_run: bool = False,
    backup_repo_url: str = "",
    pod_side_effects: bool = True,
) -> DeployResult:
    """
    Deploy or update Evolve on a single bot.
    Steps:
      1. Locate bot workspace
      2. Create evolve/ directory in workspace
      3. Scripts sourced from repo path directly (no copy)
      4. Fix ownership (chown bot_id)
      5. Inject POD_CONDUCT.md + handoff check into AGENTS.md; scrub legacy session-surface block
      6. Sweep retired per-bot jobs (_bootout_retired_per_bot_jobs)
      7. Update network config to include this bot

    Step 6 used to INSTALL the per-bot apply.py watcher. That daemon was
    retired 2026-08-18 (docs/design-proposal-signing-key-2026-08-18.md) —
    it polled proposals/approved/, a directory no arbiter status maps to,
    and had applied nothing in its entire logged history. Proposals are
    applied by ``arbiter.apply`` on the admin side; the run-as-the-bot
    boundary that apply.py provided is no longer part of the live path.

    ``pod_side_effects=False`` (release-canary deploys; see
    docs/spec-state-store-and-deploy-resilience-2026-06-10.md §2.4):
    candidate code deploying the canary bot must not mutate POD-WIDE
    state before it has been promoted. Suppresses the ensure_pod_perms
    apply pass (check-only instead) and the network.json update — both
    pod-scoped writes. Per-bot work (the canary's own files, plists,
    config) proceeds normally.
    """
    _log.info("deploy_bot start: bot=%s role=%s port=%s dry_run=%s", bot_id, role, port, dry_run)
    result = DeployResult(bot_id=bot_id, success=True)

    # Resolve actual macOS username — may differ from bot_id (e.g. when one bot lives on a personal/shared account, bot_id != user).
    # Primary source: network.json "user" field.
    # Fallback: read UserName from the bot's launchd gateway plist (openclaw writes this).
    network_pre = load_network(network_path)
    bot_user = get_bot_user(bot_id, network_pre)
    if bot_user == bot_id:
        # Check launchd plist for the real UserName in case network.json isn't updated yet
        _plist = LAUNCHD_DIR / f"ai.openclaw.{bot_id}-gateway.plist"
        if _plist.exists():
            try:
                _r = subprocess.run(
                    ["sudo", "/usr/libexec/PlistBuddy", "-c", "Print :UserName", str(_plist)],
                    capture_output=True, text=True, timeout=5,
                )
                _plist_user = _r.stdout.strip()
                if _plist_user and _plist_user != bot_id:
                    bot_user = _plist_user
                    result.log(f"Detected bot_user={bot_user} from gateway plist (differs from bot_id)")
            except Exception:
                pass

    # Refuse if bot_id isn't already registered in the pod ledger.
    # The single explicit-add path is add_bot() — called by the wizard,
    # `evolve-admin add-bot`, and the UI's Add Bot flow. Refusing here
    # blocks the historical backdoors (discover_bots auto-write,
    # `evolve-admin deploy <newbot> --port X` auto-create, etc.) that
    # let phantom bots accumulate.
    if bot_id not in network_pre.get("bots", {}):
        msg = (
            f"{bot_id!r} is not a registered pod member. "
            f"Add it explicitly via `evolve-admin add-bot {bot_id}` "
            f"or the UI's Add Bot flow before deploying."
        )
        _log.error("deploy_bot: refusing unknown bot_id %r", bot_id)
        result.error(msg)
        return result

    # ── 1. Locate workspace ────────────────────────────────────────────────
    ws = get_bot_workspace(bot_id, user=bot_user)
    if ws is None:
        _log.error("deploy_bot: cannot locate workspace for %s", bot_id)
        result.error(f"Cannot locate workspace for {bot_id} — is it an OpenClaw instance?")
        return result
    evolve_dir = ws / "evolve"
    result.log(f"Workspace: {ws}")
    result.log(f"Evolve dir: {evolve_dir}")

    if not dry_run:
        # ── 2. Create evolve/ dir ──────────────────────────────────────────
        try:
            evolve_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            # Need sudo — use subprocess
            _sudo_mkdir(evolve_dir, result)
            if not result.success:
                return result

        # ── 3. Script copying — SKIPPED ───────────────────────────────────
        # Plists point directly to /Users/Shared/evolve-repo/packages/analyzer/
        # No need to copy scripts to workspace. The repo IS the canonical source.
        result.log("Scripts: using repo path directly (no copy needed)")

        # ── 4. Fix ownership ───────────────────────────────────────────────
        _sudo_chown(evolve_dir, bot_user, result)

        # ── 4b. Set ACL so evolve user can read all .openclaw/ files ──────
        try:
            set_evolve_read_acl(bot_user)
            result.log(f"ACL: evolve read access set on {bot_user}/.openclaw/")
        except Exception as e:
            result.log(f"[warn] set_evolve_read_acl failed for {bot_id}: {e}")

        # ── 4b.1 Ensure workspace is a git repo (for nightly backup) ──────
        # Backup is opt-in per bot (via bots.<id>.backupRepoUrl in network.json),
        # but the local repo should always exist so a bot is one URL-save away
        # from working backups. Idempotent — no-op if `.git` already present.
        try:
            ok, status = ensure_workspace_git_init(bot_id)
            if status == "initialized":
                result.log(f"workspace: git init -b main on {bot_user}/.openclaw/workspace/")
            elif not ok and status != "no-workspace":
                result.log(f"[warn] ensure_workspace_git_init failed for {bot_id}: {status}")
        except Exception as e:
            result.log(f"[warn] ensure_workspace_git_init failed for {bot_id}: {e}")

        # ── 4c. Enforce pod-side perm contract for this bot ───────────────
        # Codifies the four perm layers we hand-fixed during the 2026-04-25
        # apply.py-zombie incident: .openclaw ACL allow-set, apply lock file,
        # Cellar python@3.14 readability, and proposals/ subdirs mode 1777.
        # All checks are pre-flighted; a clean pod produces zero changes.
        try:
            # Canary deploys run check-only: candidate code may not apply
            # pod-wide perm fixes before promotion (spec §2.4).
            pp = ensure_pod_perms(
                bot_id=bot_id, network_path=network_path,
                check_only=not pod_side_effects,
            )
            if pp.applied:
                for fix in pp.applied:
                    result.log(f"perms: applied {fix}")
            if pp.errors:
                for err in pp.errors:
                    result.log(f"[warn] perms: {err}")
        except Exception as e:
            result.log(f"[warn] ensure_pod_perms failed for {bot_id}: {e}")

        # ── 4c. Ensure shared dir tree + per-bot subdirs exist ────────────────
        #        deploy_shared_dir() is idempotent — all mkdir/chmod calls use
        #        exist_ok=True and are no-ops on an already-correct tree.
        #        Running it here on every deploy_bot() guarantees the full tree
        #        (metrics/, annotations/, proposals/, etc.) is present even when
        #        deploy_bot() is called without a prior deploy_shared_dir() run
        #        (e.g., adding a new bot to an existing pod).
        #        fix_shared_dir_permissions() then handles the per-bot subdirs:
        #        turns/, annotations/{bot_id}/, and the root sticky bit.
        if pod_side_effects:
            try:
                _net2 = load_network(network_path)
                _shared2 = Path(_net2.get("sharedDir", "/Users/Shared/evolve"))
                deploy_shared_dir(_shared2)  # idempotent — creates/fixes full tree
                fix_shared_dir_permissions(bot_id, _shared2)
                result.log(f"shared dir: {_shared2} tree verified; {bot_id}/turns/ and annotations/{bot_id}/ ready")
            except Exception as e:
                result.log(f"[warn] shared dir setup failed for {bot_id}: {e}")
        else:
            # Canary deploy: deploy_shared_dir creates/chmods the entire
            # pod-wide shared tree — candidate code may not reshape pod
            # perms before promotion (spec §2.4; e.g. a candidate carrying
            # the 7.1 Phase-D store-perm changes would apply them pod-wide
            # from inside a soak).
            result.log("shared dir setup skipped (pod_side_effects=False)")

        # ── 4d. Plant starter docs (SOUL/AGENTS/MEMORY/README) ────────────
        # Setup-wizard previously left workspaces with only OpenClaw's own
        # stub SOUL.md (~300 bytes — fires content_scan_structural_anomaly)
        # and no MEMORY.md / README.md (fires content_scan_file_disappeared).
        # Plant the shipped starter templates so a freshly-provisioned bot
        # is content-scan clean on first monitor pass. Idempotent: skips
        # operator-edited destinations.
        #
        # Until #1273 the primary bot was excluded here on the assumption
        # that install_evolve_bot_docs() (called separately from
        # install_evolve_infra_jobs) covered it — but that path only fires
        # during ``evolve-admin install-infra-jobs`` and only ever planted
        # SOUL/AGENTS, never MEMORY/README. install_bot_docs is role-aware:
        # primary gets evo's hand-written SOUL/AGENTS plus the generic
        # MEMORY/README; member bots get all four from the generic template.
        try:
            docs_result = install_bot_docs(
                bot_id, bot_user, role=role, dry_run=False,
            )
            for line in docs_result.steps:
                result.log(line)
            for err in docs_result.errors:
                result.log(f"[warn] {err}")
        except Exception as e:
            result.log(f"[warn] bot docs install failed for {bot_id}: {e}")

        # ── 4e. Ensure brave plugin is installed — only when a key backs it ──
        # Used to install unconditionally ("out-of-the-box web search works
        # without operator setup") — but it does NOT work without an API key,
        # so what shipped was a tool that 401s at call time. Gate on the same
        # resolver §7 uses so install and enable can't disagree.
        # ``_full_deploy`` ran ``ensure_plugin_config`` BEFORE this install, so
        # its gap-fill saw "not installed yet". Re-run it for the new record.
        try:
            from .safe_upgrade import _installed_plugin_ids
            _net_for_brave = load_network(network_path)
            _brave_key = resolve_pod_brave_key(bot_id, _net_for_brave)
            if _brave_key and "brave" not in _installed_plugin_ids(bot_id, _net_for_brave):
                from .oc_neutralize import install_externalized_plugin
                ok_brave, err_brave = install_externalized_plugin(
                    bot_user, "@openclaw/brave-plugin", force=False,
                )
                if ok_brave:
                    result.log(f"brave plugin installed on {bot_id}")
                    try:
                        ensure_plugin_config(bot_id, _net_for_brave)
                        result.log(f"plugin config refreshed for {bot_id} (brave entry)")
                    except Exception as e:
                        result.log(f"[warn] plugin config refresh for {bot_id}: {e}")
                else:
                    result.log(f"[warn] brave plugin install for {bot_id}: {err_brave}")
            elif not _brave_key:
                result.log(f"brave skipped for {bot_id} — no pod keystore key")
        except Exception as e:
            result.log(f"[warn] brave plugin install for {bot_id}: {e}")

        # ── 4f. Register evo tool registry as an OC MCP server (primary only)
        # Wires mcp.servers.evo_tools to point at the stdio bridge
        # (python3 -m evolve_admin.evo.tools). Idempotent gap-fill — skips
        # member bots; only the primary bot (evo) owns the direct-access
        # tool surface per spec §1.3. Failure is non-fatal: the bot still
        # runs, only the tool surface is dark until the next deploy.
        try:
            from .evo.tools.deploy_integration import ensure_evo_tools_mcp_server
            _net_for_evo_tools = load_network(network_path)
            _changed, _status = ensure_evo_tools_mcp_server(
                bot_id, _net_for_evo_tools, network_path=network_path,
            )
            if _changed:
                result.log(f"evo tool registry: mcp.servers.evo_tools {_status}")
            elif _status.startswith("error:"):
                result.log(f"[warn] evo tool registry: {_status}")
            else:
                # unchanged / skipped:not-primary — logged at debug only.
                _log.debug(
                    "evo tool registry for %s: %s", bot_id, _status,
                )
        except Exception as e:
            result.log(f"[warn] evo tool registry registration for {bot_id}: {e}")

        # ── 4g. Register Evolve-shipped skills dir with OC's loader (primary only)
        # Adds agents.defaults.skills.load.extraDirs[] = <repo>/packages/analyzer/
        # evolve_bot/skills/ so OC's six-source loader picks up Evolve-shipped
        # skills at source-1 precedence (lowest — workspace customization wins).
        # Spec §14.2. Idempotent gap-fill; member bots skipped.
        try:
            from .evo.tools.deploy_skills import ensure_evo_skills_extra_dir
            _changed, _status = ensure_evo_skills_extra_dir(
                bot_id, _net_for_evo_tools,
            )
            if _changed:
                result.log(f"evo skills loader: extraDirs {_status}")
            elif _status.startswith("error:"):
                result.log(f"[warn] evo skills loader: {_status}")
            else:
                _log.debug(
                    "evo skills loader for %s: %s", bot_id, _status,
                )
        except Exception as e:
            result.log(f"[warn] evo skills loader registration for {bot_id}: {e}")

        # ── 4h. Skill retirement detector — surface obviated local skills ──
        # Spec §14.2: when upstream Evolve ships a tool that obviates a
        # pod-local skill, log a deploy notice so the operator can decide
        # whether to retire the skill or keep it. NEVER auto-deletes.
        # Idempotent: re-runs every deploy and logs whatever's currently
        # obviated. Primary-only (member bots don't carry the tool surface
        # or local skills). Failure is non-fatal — same severity as the
        # other §4* hooks: a missing retirement notice doesn't block the
        # deploy.
        try:
            from .evo.skill_retirement import (
                detect_retirement_candidates,
                format_deploy_notice,
                resolve_skill_sources_for_bot,
            )
            from .evo.tools import all_tools
            from .evo.tools.deploy_integration import _primary_bot_id

            # Only run on the primary bot — the loader hookup itself is
            # primary-only, and member bots don't house pod-local skills.
            # Use the explicit primary resolver (honors role=primary AND
            # the legacy "evolve" name) rather than a bare ``or "evolve"``
            # fallback, which would misfire on a pod that has a bot named
            # "evolve" but a different primary.
            _primary = _primary_bot_id(_net_for_evo_tools)
            if _primary is not None and bot_id == _primary:
                _tool_names = [t.name for t in all_tools()]
                _extra_dirs, _workspace_dir = resolve_skill_sources_for_bot(
                    bot_id, bot_user=bot_user,
                )
                _candidates = detect_retirement_candidates(
                    extra_dirs=_extra_dirs,
                    workspace_dir=_workspace_dir,
                    tool_names=_tool_names,
                )
                for _c in _candidates:
                    result.log(f"[notice] {format_deploy_notice(_c)}")
        except Exception as e:
            result.log(f"[warn] skill retirement detector for {bot_id}: {e}")

        # ── 5. Inject pod_conduct.md via contextFiles ──────────────────────
        try:
            inject_pod_conduct(bot_id, bot_user=bot_user)
            result.log(f"pod_conduct.md: contextFiles injection verified for {bot_id}")
        except Exception as e:
            result.log(f"[warn] pod_conduct injection failed for {bot_id}: {e}")

        # ── 5b. Inject MCP Bridge handoff check into AGENTS.md ─────────────
        try:
            inject_handoff_check(bot_id, bot_user=bot_user)
            result.log(f"handoff check: AGENTS.md updated for {bot_id}")
        except Exception as e:
            result.log(f"[warn] handoff check injection failed for {bot_id}: {e}")

        # ── 5c. Scrub legacy session-surface block from AGENTS.md ─────────
        # The block primed the LLM to hallucinate "no pending tasks" replies
        # when the Evolve plugin handled 'evo' out of band. Pending tasks are
        # now surfaced exclusively via session_surface.py → session_start
        # systemAppend (see TurnObserver.handleSessionStart).
        try:
            _net = load_network(network_path)
            _shared = _net.get("sharedDir", "/Users/Shared/evolve")
            inject_session_surface_check(bot_id, _shared, bot_user=bot_user)
            result.log(f"session surface: AGENTS.md scrub verified for {bot_id}")
        except Exception as e:
            result.log(f"[warn] session surface scrub failed for {bot_id}: {e}")

        # ── 6. Sweep retired per-bot jobs ─────────────────────────────────
        # NOTE: per-bot measure jobs (ai.openclaw.evolve.measure.<bot>) are
        # NOT installed here. Measure is pod-wide via
        # ai.openclaw.evolve.measure (installed by install-infra-jobs);
        # see ``per_bot_evolve_plist_labels`` and ``expected_plist_labels``
        # for the canonical per-bot vs pod-wide split. The previous
        # ``_install_launchd_measure(bot_id, …)`` was a leftover from the
        # pre-migration era — the call was retired (every redeploy was
        # re-creating per-bot plists immediately after the orphan-sweeper
        # deleted them) and the function itself was removed 2026-05-26
        # when the orphan-sweep meta-test caught the dead code.
        # ai.openclaw.evolve.test.<bot> install removed 2026-06-08 — the
        # weekly per-bot test runner was defunct (read from a shared dir
        # that no longer held manifests) and the app-test surface is
        # killed.
        # ai.openclaw.evolve.apply.<bot> install removed 2026-08-18 — the
        # legacy apply watcher polled a directory no arbiter status maps
        # to (docs/design-proposal-signing-key-2026-08-18.md). The script
        # deletion and this teardown are NOT one atomic event — see the
        # ordering note in _bootout_retired_per_bot_jobs.
        _bootout_retired_per_bot_jobs(bot_id, result)

        # ── 6b'. Install per-bot cost-event converter (every 15 min) ──────
        # Replaces the broken plugin llm_output → cost_event emission path.
        # See packages/analyzer/cost_event_converter.py for the why.
        _install_launchd_cost_converter(bot_id, result, user=bot_user)

        # ── 6c. Install per-bot app-audit runners (Tier 2 + Tier 3) ───────
        # Tier 2 runs every 6 hours doing pure-Python structural checks.
        # Tier 3 runs hourly, dispatching the bot's local LLM to do
        # semantic audits for apps that are cadence-due. See
        # docs/spec-app-audit-2026-05-16.md §7.
        _install_launchd_audit_runner(bot_id, evolve_dir, result, user=bot_user)
        _install_launchd_audit_runner_tier3(bot_id, evolve_dir, result, user=bot_user)

        # ── 6c''. Install per-bot nightly backup daemon ───────────────────
        # Runs as bot_user (not evolve) so it has full access to its own
        # workspace — no cross-user ACL needed. No-ops at run time if
        # backupRepoUrl isn't set (backup.py skips uneligible bots).
        # Installed idempotently here so setting a URL later in the UI
        # doesn't require a re-deploy to get the daemon.
        _install_launchd_backup(bot_id, evolve_dir, result, user=bot_user)

        # ── 6c'''. Install per-bot nightly `openclaw doctor --fix` job ────
        # Doctor used to run synchronously inside `install_oc_plugin` but
        # started timing out during deploy on 6/8 bots (2026-05-29/30 runs).
        # The hang only manifested inside deploy's subprocess wrapper —
        # manual runs as the same evolve user completed in 12-15s. Rather
        # than keep chasing the discrepancy, doctor moved to a nightly
        # cadence here. Operators who need an immediate pass can run
        # `sudo evolve-admin doctor-pass --bot <id>`.
        _install_launchd_doctor_pass(bot_id, result, user=bot_user)

        # ── 6c'. Sync the audit pod_config.json into the bot's workspace ──
        # The Tier-3 runner reads cadence + calibration + ceilings from
        # this file. It's a tiny slice of network.json, synced on every
        # network save (admin server hook) and also planted here at deploy
        # time so a freshly-deployed bot's first audit has the config.
        try:
            from .applications.audit_pod_config import write_pod_config
            network_for_sync = load_network(network_path)
            ok = write_pod_config(network_for_sync, bot_id, bot_user)
            if ok:
                result.log(f"Wrote audit pod_config for {bot_user}")
            else:
                result.log(f"[warn] audit pod_config write failed for {bot_user}")
        except Exception as exc:
            result.log(f"[warn] audit pod_config sync error: {exc}")

        # ── 7. Update mutable fields on the existing network entry ────────
        # bot_id is guaranteed to already be in network.bots (precondition
        # checked at top of function). Roster mutation is add_bot's job;
        # deploy_bot only refreshes deploy-time fields.
        if pod_side_effects:
            network = load_network(network_path)
            bots: dict[str, Any] = network.setdefault("bots", {})
            # Heal drift between bots and members (idempotent).
            members: list[str] = network.setdefault("members", [])
            if bot_id not in members:
                members.append(bot_id)
            bot_entry = dict(bots.get(bot_id, {}))  # don't mutate in place
            bot_entry["role"] = role
            bot_entry["port"] = port or bot_entry.get("port") or get_bot_port(bot_id, network)
            if backup_repo_url:
                bot_entry["backupRepoUrl"] = backup_repo_url
            if bot_user != bot_id:
                bot_entry["user"] = bot_user
            bots[bot_id] = bot_entry
            if role == "primary":
                network["primary"] = bot_id
            save_network(network, network_path)
            result.log(f"Updated network config: role={role}")
        else:
            # Canary deploy: network.json is pod-wide state — candidate
            # code may not rewrite it before promotion (spec §2.4).
            result.log("network config update skipped (pod_side_effects=False)")

        # ── 8. Remove conflicting user-level gateway LaunchAgents ─────────
        # npm's post-install drops ai.openclaw.gateway.plist into the bot's
        # ~/Library/LaunchAgents/ on every openclaw upgrade.  That user agent
        # runs alongside the system daemon and holds the port, causing the
        # system daemon to crash-loop with EADDRINUSE.  Clean it up here so
        # a deploy always leaves a single gateway process.
        #
        # IMPORTANT: after booting out the user agent the system daemon may
        # not be running (it lost the port race on the previous launch).
        # Restart it so the gateway is guaranteed to be up after deploy.
        _user_agent_removed = False
        try:
            from .ocadmin import _remove_conflicting_user_agents
            network_snap = load_network(network_path)
            _remove_conflicting_user_agents(network_snap)
            _user_agent_removed = True
        except Exception as e:
            result.log(f"[warn] conflicting user-agent cleanup failed for {bot_id}: {e}")

        system_plist = Path(f"/Library/LaunchDaemons/ai.openclaw.{bot_id}-gateway.plist")
        if _user_agent_removed and system_plist.exists():
            try:
                restart_gateway(bot_id)
                result.log(f"Gateway restarted after user-agent cleanup for {bot_id}")
            except Exception as e:
                result.log(f"[warn] gateway restart after cleanup failed for {bot_id}: {e}")

        # ── 9. Ensure SSH backup deploy key (shared model) ────────────────
        # The pod-wide shared keypair at
        # /Users/evolve/.ssh/evolve-backup-shared{,.pub} is the canonical
        # source. This step copies it into bot_user's home so the per-bot
        # backup daemon (running as bot_user) can find it via the
        # backup.ssh_key_path lookup (~/.ssh/evolve-backup-<bot>).
        #
        # Without this step the daemon hits "Host key verification failed"
        # on every nightly push and the failure stays silent until
        # backup_signal's 3-strikes threshold fires the backup_failing
        # alert — days later.
        #
        # If the canonical source doesn't exist yet (pre-migration pod or
        # fresh install), this returns NO_SOURCE and we log a hint. The
        # operator runs the admin-UI Distribute Key flow once per pod to
        # generate it; that flow also handles GitHub deploy-key
        # registration (invariant clause 4). See
        # docs/spec-backup-key-distribution-unification-2026-06-08.md.
        try:
            from . import backup_keys
            sync_result = backup_keys.ensure_bot_in_sync(
                bot_id, bot_user, log=result.log,
            )
            if sync_result.status == backup_keys.BotSyncStatus.NO_SOURCE:
                result.log(
                    f"[hint] No pod-wide backup key at {backup_keys.SHARED_SOURCE_PRIV}; "
                    f"run Distribute Key in the admin UI to generate and register one. "
                    f"Cloud backup for {bot_id} stays paused until then."
                )
            elif sync_result.status == backup_keys.BotSyncStatus.FAILED:
                result.log(
                    f"[warn] SSH backup key sync failed for {bot_id}: {sync_result.error}"
                )
        except Exception as e:
            result.log(f"[warn] SSH backup key ensure failed for {bot_id}: {e}")

        # ── 10. iMessage poller — DEPRECATED 2026-06-04 ────────────────────
        # The bundled-plugin rewire moved iMessage onto OC's bundled
        # @openclaw/imessage plugin. The home-rolled poller's HTTP target
        # (/api/chat) never existed on OC's gateway in any version —
        # confirmed live against OC v2026.6.1 — so the LaunchDaemon was
        # always inert even when "installed". Deploy now actively tears
        # down any existing poller LaunchDaemon so the deprecated state
        # converges on retired-and-gone rather than retired-and-orphaned.
        #
        # ``install_imessage_poller`` + ``uninstall_imessage_poller`` will
        # be deleted in a follow-on cleanup PR alongside
        # ``packages/analyzer/imessage_plugin/poller.py``. This block keeps
        # the cleanup direction explicit so an operator running deploy
        # before that cleanup PR ships still gets the right end state.
        try:
            ok_un, msg_un = uninstall_imessage_poller(bot_id)
            if ok_un and "removed" in (msg_un or "").lower():
                # Only log when something was actually torn down — avoid
                # spamming the log on the common no-LaunchDaemon-existed
                # case.
                result.log(f"[deprecated] iMessage poller LaunchDaemon: {msg_un}")
        except Exception as e:
            result.log(f"[warn] iMessage poller teardown skipped: {e}")

    else:
        result.log("[dry-run] Would create " + str(evolve_dir))
        result.log("[dry-run] Would fix ownership")
        result.log("[dry-run] Would update network config")

    # Refresh the heal drift baseline so deploy-driven openclaw.json
    # changes don't immediately trigger a `security.config_drift` alert
    # on the next heal cron. Without this, every `evolve-admin deploy`
    # leaves a window where the live config differs from the committed
    # evolve-backup/openclaw.json; heal detects "unexplained drift",
    # operators see a misleading alert telling them to deploy (which
    # they just did — and which used to make it worse). Local-only
    # commit, no remote push. Best-effort: a baseline-refresh failure
    # never rolls back the deploy itself.
    #
    # Must run AS the bot user (via sudo -H -u <bot_user>), not in-process.
    # `sudo evolve-admin deploy` runs as root, and commit_baseline_local
    # writes .git/config (via `git config user.name`), creates
    # evolve-backup/ if missing, and copies openclaw.json into it.
    # Running those writes as root leaves files root-owned mode 0600/0755 —
    # the next nightly backup daemon (running as the bot user) then can't
    # read .git/config or write into evolve-backup/, wedging backups
    # silently until a human spots it. Symptom on 2026-05-30: 8/8 bots
    # showed BACKED UP=failed/skipped in the admin UI; root cause was
    # the in-process call here. See incident notes in this PR.
    if not dry_run and result.success:
        _baseline_refresh_as_bot_user(bot_id, bot_user, network_path, result)

    # ── L2 audit follow-up: heal primary from role-aware tier config ─────
    #
    # Member bots seeded before the role-aware writer fix were running
    # Sonnet primary while their tier3 was Haiku (~5x cost). Re-running
    # the writer with current tiers triggers the role-aware primary
    # derivation. The writer is idempotent: if evolve-tiers.json has an
    # explicit tierCascade, the operator's choice wins; if not, member
    # bots get tier3 primary, primary bots get tier2 primary.
    #
    # Safe to run on every deploy — if primary already matches, no diff.
    # Wrapped in try so a heal failure never blocks deploy.
    try:
        _heal_primary_from_tier_config(bot_id, role, network_path, result)
    except Exception as exc:  # noqa: BLE001
        result.log(f"[warn] primary-heal: skipped on {exc}")

    _log.info(
        "deploy_bot done: bot=%s success=%s errors=%d",
        bot_id, result.success, len(result.errors),
    )
    if result.errors:
        for err in result.errors:
            _log.error("deploy_bot error [%s]: %s", bot_id, err)
    return result


def _reclaim_baseline_ownership(workspace: Path, bot_user: str) -> list[str]:
    """Chown any root-owned baseline artefacts under ``workspace`` back to ``bot_user``.

    Recovery for pods whose prior deploys ran ``commit_baseline_local``
    in-process as root (pre-2026-05-30 behavior). Those runs left
    ``.git/`` (notably ``.git/config`` mode 0600) and ``evolve-backup/``
    owned by ``root:staff``, locking the bot's backup daemon out of
    its own files. Without this sweep the new sudo-drop path can't
    write into the still-root-owned ``evolve-backup/`` either — the
    fix wouldn't self-heal.

    We chown the ``.git/`` tree (which the baseline writer touches via
    ``git config`` + ``git commit``) and the ``evolve-backup/`` tree.
    Only run as root — chown of a root-owned file by anyone else is
    EPERM and would noise up logs. Returns a list of human-readable
    paths that were reclaimed, for logging by the caller. Errors are
    swallowed (deploy is best-effort here).
    """
    if os.geteuid() != 0:
        return []
    reclaimed: list[str] = []
    for sub in (".git", "evolve-backup"):
        target = workspace / sub
        if not target.exists():
            continue
        try:
            st = target.stat()
        except OSError:
            continue
        # Skip the chown sweep if the top-level dir is already owned by
        # someone other than root — covers the common already-healed
        # case and the legitimate-mixed-ownership case where a human
        # operator already chmod/chown-ed things.
        if st.st_uid != 0:
            continue
        r = subprocess.run(
            [_PROFILE.chown, "-R", f"{bot_user}:staff", str(target)],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            reclaimed.append(str(target))
    return reclaimed


def _baseline_refresh_as_bot_user(
    bot_id: str,
    bot_user: str,
    network_path: Path,
    result: "DeployResult",
) -> None:
    """Run backup.commit_baseline_local for ``bot_id`` AS the bot user.

    Two steps:
      1. Reclaim ownership of any root-owned ``.git/`` or ``evolve-backup/``
         left behind by older deploys (see ``_reclaim_baseline_ownership``).
      2. Invoke ``python3 backup.py --commit-baseline-local <bot_id>``
         via ``sudo -H -u <bot_user>``. The CLI flag lives in
         ``packages/analyzer/backup.py``; it loads network.json,
         resolves shared_dir, and calls ``commit_baseline_local``.

    Best-effort: a refresh failure logs a warning but never rolls back
    the deploy itself. Matches the original in-process call's failure
    semantics.
    """
    try:
        net = load_network(network_path)
    except Exception as exc:
        result.log(f"[warn] baseline-refresh: load_network failed: {exc}")
        return

    # Step 1: reclaim ownership of any artefacts the pre-fix deploys
    # corrupted. Cheap and idempotent — no-op once the dirs are bot-owned.
    try:
        workspace = Path(get_bot_workspace(bot_id, net))
        if workspace.exists():
            reclaimed = _reclaim_baseline_ownership(workspace, bot_user)
            for p in reclaimed:
                result.log(f"baseline-refresh: reclaimed ownership of {p} → {bot_user}:staff")
    except Exception as exc:
        result.log(f"[warn] baseline-refresh: ownership-reclaim raised: {exc}")

    # Step 2: invoke the CLI as the bot user.
    backup_py = ANALYZER_DIR / "backup.py"
    if not backup_py.exists():
        result.log(f"[warn] baseline-refresh skipped: {backup_py} not found")
        return
    cmd = [
        "sudo", "-H", "-u", bot_user,
        "/usr/bin/python3", str(backup_py),
        "--commit-baseline-local", bot_id,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        result.log("[warn] baseline-refresh: timed out after 30s")
        return
    except Exception as exc:
        result.log(f"[warn] baseline-refresh: subprocess raised: {exc}")
        return
    msg = (r.stdout.strip() or r.stderr.strip() or "").splitlines()
    tail = msg[-1] if msg else f"rc={r.returncode}"
    if r.returncode == 0:
        result.log(f"baseline-refresh: {tail}")
    else:
        result.log(f"[warn] baseline-refresh (rc={r.returncode}): {tail}")


def _heal_primary_from_tier_config(
    bot_id: str,
    role: str,
    network_path: Path,
    result: "DeployResult",
) -> None:
    """Re-derive openclaw.json's `primary` from the bot's tier config.

    Reads current tiers via oc_full_config_get; if there are any tier
    entries (i.e. the bot's tier config exists), calls
    oc_full_config_set_with_error with the same tiers — which triggers
    oc_model.py's role-aware primary recompute. The writer takes the
    bot's role from network.json and uses the role-aware default
    tierCascade only when evolve-tiers.json has no explicit cascade.

    This is the heal pass for the 2026-05-29 L2 audit finding: existing
    member bots that were seeded with the legacy workhorse-first cascade
    default get their primary corrected to the tier3 floor on next
    deploy. No-op for bots whose primary is already correct.
    """
    try:
        from runtime.agent_runtime import get_runtime  # type: ignore
        _runtime = get_runtime()
        _get = _runtime.full_config_get
        _set_err = _runtime.full_config_set_with_error
    except Exception as exc:  # noqa: BLE001
        result.log(f"[warn] primary-heal: runtime seam unavailable: {exc}")
        return

    current = _get(bot_id, network_path=str(network_path))
    if not current:
        result.log("[warn] primary-heal: could not read current config; skipping")
        return
    tiers = current.get("tiers") or {}
    if not tiers:
        # No tier config yet — nothing to derive from. seed_model_config_if_empty
        # is the right call for that path (provisioning stage, not deploy).
        result.log("primary-heal: no tier config yet (skipped)")
        return

    primary_before = current.get("primary")
    write_result, write_err = _set_err(
        bot_id, {"tiers": tiers}, network_path=str(network_path),
    )
    if write_result is None:
        # A failed config-set means openclaw.json was NOT updated — mark the
        # deploy result failed (success=False, surfaced in the CLI step summary)
        # instead of burying it in the step log. The deploy still continues:
        # like sibling non-fatal steps, a heal failure shouldn't block the
        # gateway restart. Caught live 2026-06-10: canary deploy printed
        # "oc_full_config_set bot=<bot> exit=1" yet the workspace step
        # reported plain "done".
        result.error(f"primary-heal: config write failed: {write_err}")
        return
    primary_after = write_result.get("primary")
    if primary_before != primary_after:
        result.log(
            f"primary-heal: {primary_before} → {primary_after} "
            f"(role={role}, role-aware cascade)"
        )
    else:
        result.log(f"primary-heal: unchanged ({primary_after}; role={role})")


# ── Post-deploy smoke audit ───────────────────────────────────────────────────
#
# Nothing in the deploy flow asserts "a freshly-deployed bot should produce no
# CRITICAL audit findings." The 23h-gated background audit can take a day to
# notice regressions — long enough for a phantom-finding bug (see
# evolve-ops/evolve#1088) to run unchecked. This helper bypasses the time gate
# and reruns audit_oc_security so deploy can fail loudly the moment criticals
# appear.

@dataclass
class SmokeAuditResult:
    """Outcome of run_smoke_audit. ``error`` is non-None when the audit itself
    failed to run — that's its own kind of news, distinct from clean."""
    critical_findings: list[Any] = field(default_factory=list)
    warn_findings: list[Any] = field(default_factory=list)
    error: str | None = None

    @property
    def is_clean(self) -> bool:
        return self.error is None and not self.critical_findings


def run_smoke_audit(bot_id: str, shared_dir: Path) -> SmokeAuditResult:
    """Run an immediate post-deploy OC security audit on ``bot_id``.

    Deletes ``{shared_dir}/security/last-oc-audit-{bot_id}`` first to bypass
    the 23h time-gate in ``audit_oc_security``. Returns a structured result
    so the caller can render criticals/warns separately and decide on exit
    behaviour.
    """
    ts = shared_dir / "security" / f"last-oc-audit-{bot_id}"
    try:
        ts.unlink(missing_ok=True)
    except OSError as e:
        return SmokeAuditResult(error=f"could not reset audit time-gate ({ts}): {e}")

    try:
        from audit import audit_oc_security  # type: ignore[import-not-found]
    except ImportError as e:
        return SmokeAuditResult(error=f"audit module not importable: {e}")

    try:
        findings = audit_oc_security(bot_id, shared_dir)
    except subprocess.CalledProcessError as e:
        return SmokeAuditResult(
            error=f"audit_oc_security raised CalledProcessError: {e}"
        )
    except Exception as e:
        return SmokeAuditResult(
            error=f"audit_oc_security raised {type(e).__name__}: {e}"
        )

    crits = [f for f in findings if getattr(f, "level", None) == "critical"]
    warns = [f for f in findings if getattr(f, "level", None) == "warn"]
    return SmokeAuditResult(critical_findings=crits, warn_findings=warns)


# Activation-window daily $ cap for a freshly-created bot. This is now a
# *graduated product default* resolved in code (better_engine_config:
# ``NEW_BOT_DAILY_HARD_USD`` for the first ``NEW_BOT_GRADUATION_DAYS`` days,
# then the pod default), NOT a value materialized at creation — a static value
# never graduates. add_bot only stamps ``created_at`` so the resolver can
# age-grade it. This mirror is kept for the CLI help text. Background: a new
# bot with no cap ate $36 in two 2026-06-03 turns and $30.26 over the
# ``ledger`` bot's first two days (2026-06-12).
DEFAULT_NEW_BOT_DAILY_CAP_USD: float = 10.0


def add_bot(
    bot_id: str,
    *,
    role: str = "member",
    port: int,
    user: str | None = None,
    multi_user: bool = False,
    backup_repo_url: str = "",
    daily_cap_usd: float | None = None,
    network_path: Path,
) -> None:
    """Register a new bot in the pod ledger.

    This is the single entry point that creates an entry in
    network.json's `bots` dict and `members` array. After calling,
    invoke `deploy_bot()` to install launchd jobs and config — that
    function refuses unknown bot_ids by design.

    Use this from the wizard (interactive add), the
    `evolve-admin add-bot` CLI command, and the UI's Add Bot flow —
    any explicit user-driven path. Discovery scans (find_oc_candidates)
    must NOT call this; they're advisory only.

    ``daily_cap_usd`` defaults to ``None`` — the daily hard cap comes from the
    *graduated new-bot default* (resolved in code), so nothing static is
    materialized that would fail to graduate. A positive ``daily_cap_usd`` sets
    an explicit per-bot override (wins over the graduated default and the pod
    default; settable later via Cost & caps UI / ``action.cost.set_bot_cap``).
    Either way add_bot stamps ``bots.<bot>.created_at`` (better-engine-config,
    the canonical store since Phase-4) so the resolver can age-grade the cap.

    Raises:
      ValueError: if bot_id is already registered. To redeploy use
        deploy_bot; to disconnect Evolve and re-add use
        `evolve-admin detach-bot` (heavier graceful paths:
        `retire-bot` / `delete-bot`).
    """
    from .config import RESERVED_BOT_IDS, is_planned_bot_block
    network = load_network(network_path)
    # EVO-SEP-S5: `evolve`/`evo` are reserved service/assistant accounts, not member bots — refuse to register one (alias-aware via resolved user + explicit --user). This is the registration-side complement to the removal guards in retire.py (PR #3083). See docs/spec-evo-account-separation-2026-05-25.md.
    if is_reserved_account(bot_id, network) or (user and user in RESERVED_BOT_IDS):
        raise ValueError(f"refusing to register {bot_id!r}: `evolve`/`evo` are reserved Evolve service/assistant accounts (the `evolve` user runs the admin server + pod-infra daemons; `evo` is the separated assistant account), not member bots. (EVO-SEP-S5)")
    bots: dict[str, Any] = network.setdefault("bots", {})
    existing = bots.get(bot_id)
    if bot_id in bots and not is_planned_bot_block(existing):
        raise ValueError(
            f"{bot_id!r} is already registered. "
            f"Use deploy_bot to redeploy, or `evolve-admin detach-bot` "
            f"(or retire-bot / delete-bot) to disconnect from Evolve first."
        )
    entry: dict[str, Any] = {"role": role, "port": port, "multiUser": multi_user}
    # Graduating a *planned* bot (purpose-only block from the add-bot
    # wizard): the declared purpose carries over into the full entry.
    if isinstance(existing, dict) and existing.get("purpose"):
        entry["purpose"] = existing["purpose"]
    if user and user != bot_id:
        entry["user"] = user
    if backup_repo_url:
        entry["backupRepoUrl"] = backup_repo_url
    bots[bot_id] = entry
    members: list[str] = network.setdefault("members", [])
    if bot_id not in members:
        members.append(bot_id)
    if role == "primary":
        network["primary"] = bot_id
    save_network(network, network_path)

    shared_dir = Path(network.get("sharedDir") or _CANONICAL_SHARED_DIR)

    # Stamp creation time so the resolver can age-grade the graduated new-bot cap (without it a new bot looks "mature" and skips its backstop — the 2026-06-12 ledger gap). Best-effort: a failure just falls to the pod default (safe), so it's logged, not raised.
    try:
        from datetime import datetime, timezone
        _be_set_bot_created_at(shared_dir, bot_id, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "add_bot(%s): failed to stamp created_at (%s); graduated cap "
            "will not apply (falls to pod default)", bot_id, exc,
        )

    # An explicit ``daily_cap_usd`` writes a per-bot override (wins over the
    # graduated default + pod default). Mirrors action_cost: only positive
    # numbers count; None / ≤ 0 / non-numeric writes nothing.
    persisted_cap: float | None = None
    if daily_cap_usd is not None:
        try:
            cap = float(daily_cap_usd)
        except (TypeError, ValueError):
            cap = 0.0
        if cap > 0:
            try:
                _be_set_per_bot_daily_hard(shared_dir, bot_id, cap)
                persisted_cap = cap
            except Exception as exc:  # noqa: BLE001
                # Non-fatal: the bot is registered in network.json; the cap
                # can be set later via the Cost & caps UI. Log loudly so the
                # operator sees it on subsequent boot.
                _log.warning(
                    "add_bot(%s): failed to write per_bot_daily_hard_usd to "
                    "better-engine-config (%s); cap left at pod default",
                    bot_id, exc,
                )

    _log.info(
        "add_bot: registered bot=%s role=%s port=%s user=%s multi_user=%s "
        "daily_hard_usd=%s",
        bot_id, role, port, user or bot_id, multi_user, persisted_cap,
    )


POD_CONDUCT_SOURCE = _REPO_ROOT / "docs" / "system" / "POD_CONDUCT.md"


def _read_pod_conduct_content() -> str:
    """Read POD_CONDUCT.md from docs/system/ — the canonical source of truth."""
    if not POD_CONDUCT_SOURCE.exists():
        raise RuntimeError(f"POD_CONDUCT.md not found at {POD_CONDUCT_SOURCE} — do not inline it, fix the path")
    return POD_CONDUCT_SOURCE.read_text().strip()


def _write_pod_conduct(shared_dir: Path, result: "DeployResult") -> None:
    """Write POD_CONDUCT.md to shared dir during setup.

    Hash-compares against the canonical source so repo-side updates propagate
    on every deploy (predicate-drift fix — see PR #312, PR #317).

    Also distributes manifest-spec.md and manifest-schema.json from the evolve
    repo's docs/ to shared_dir/docs/ so bots can reference them at runtime.
    """
    conduct_path = shared_dir / "POD_CONDUCT.md"
    content = _read_pod_conduct_content() + "\n"

    existing: str | None = None
    if conduct_path.exists():
        try:
            existing = conduct_path.read_text()
        except Exception:
            existing = None  # fall through to rewrite

    if existing == content:
        result.log("POD_CONDUCT.md up to date — no-op")
    else:
        try:
            conduct_path.write_text(content)
            result.log(f"POD_CONDUCT.md written to {conduct_path}")
        except PermissionError:
            import tempfile
            _fd, tmp = tempfile.mkstemp(dir="/tmp", suffix=".md")
            with os.fdopen(_fd, "w") as f:
                f.write(content)
            _run_sudo(["cp", tmp, str(conduct_path)], result)
            _run_sudo(["chmod", "644", str(conduct_path)], result)
            result.log(f"POD_CONDUCT.md written via sudo to {conduct_path}")

    # Distribute manifest-spec.md and manifest-schema.json to shared docs/.
    # Source from the actual deploy checkout (_REPO_ROOT) rather than a
    # hardcoded /Users/Shared/evolve-repo — byte-identical in a macOS deploy,
    # and resolves to /var/lib/evolve/repo on Linux (W10-E: the hardcode made
    # both docs "not found, skipping" on the VPS).
    repo_docs = _REPO_ROOT / "docs"
    shared_docs = shared_dir / "docs"
    try:
        shared_docs.mkdir(exist_ok=True)
    except PermissionError:
        _run_sudo(["mkdir", "-p", str(shared_docs)], result)

    for doc_name in ("manifest-spec.md", "manifest-schema.json"):
        src = repo_docs / doc_name
        dst = shared_docs / doc_name
        if src.exists():
            try:
                import shutil
                shutil.copy2(src, dst)
                result.log(f"{doc_name} distributed to {dst}")
            except PermissionError:
                _run_sudo(["cp", str(src), str(dst)], result)
                _run_sudo(["chmod", "644", str(dst)], result)
                result.log(f"{doc_name} distributed via sudo to {dst}")
        else:
            result.log(f"[warn] {doc_name} not found at {src} — skipping")


RUNTIME_NOTES_SOURCE = _REPO_ROOT / "docs" / "system" / "RUNTIME_NOTES.md"


def _seed_pod_runtime_notes(shared_dir: Path, result: "DeployResult") -> None:
    """Seed shared_dir/RUNTIME_NOTES.md if absent — create-if-absent, never clobber.

    The pod-scope content scanner lists RUNTIME_NOTES.md in catalog scope
    ``scanned_pod_files`` and expects it at ``shared_dir/<name>`` — but unlike
    POD_CONDUCT.md (seeded here + copied into bot workspaces), RUNTIME_NOTES.md
    is injected into sessions straight from the repo docs/system/ source and was
    never seeded, so the scanner fired a GENUINE content_scan_file_disappeared
    for ``__pod__: RUNTIME_NOTES.md`` every sweep (live on evo-vps since
    2026-06-23). Seed the canonical doc once so fresh AND existing pods self-heal
    on deploy; deliberately NOT hash-propagated like _write_pod_conduct — an
    operator copy is left untouched, and a stale copy is harmless since the live
    injection reads the repo source. shared_dir is evolve-owned → plain write.
    """
    dest = shared_dir / "RUNTIME_NOTES.md"
    if dest.exists():
        result.log("RUNTIME_NOTES.md already present — no-op")
        return
    if not RUNTIME_NOTES_SOURCE.exists():
        result.log(f"[warn] RUNTIME_NOTES.md source missing at {RUNTIME_NOTES_SOURCE} — skip")
        return
    try:
        dest.write_text(RUNTIME_NOTES_SOURCE.read_text().strip() + "\n")
        result.log(f"RUNTIME_NOTES.md seeded to {dest}")
    except PermissionError:
        # Only trips on a not-yet-ACL'd fresh tree; next deploy writes it cleanly.
        result.log(f"[warn] could not seed RUNTIME_NOTES.md to {dest} — retry next deploy")


# Proposal lifecycle subdirs are EXPLICITLY non-sticky.
#
# The arbiter store writes proposals here from multiple producers — bot
# appliers (running as the bot user) write to ``applied/``, the admin UI
# daemon (running as ``evolve``) moves proposals between subdirs on user
# actions like dismiss/approve, and the verify daemon transitions
# ``applied/ → archived/``. Sticky-bit world-writable (1777) lets every
# writer create files but ONLY the file's owner can unlink — that's
# what trapped the dismiss path on 2026-05-12 when ``applied/`` was
# 1777 with bot-owned files; ``evolve``'s os.replace(src, dest) hit
# EACCES on the implicit unlink. 0o777 with no sticky bit lets any
# system writer transition a proposal regardless of which daemon wrote
# the source file.
PROPOSAL_LIFECYCLE_SUBDIRS = (
    "proposals/pending",
    "proposals/snoozed",
    "proposals/applied",
    "proposals/archived",
)


def deploy_shared_dir(shared_dir: Path, dry_run: bool = False) -> DeployResult:
    """Create the shared directory with sticky-bit world-writable permissions."""
    result = DeployResult(bot_id="shared", success=True)
    if not dry_run:
        try:
            shared_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            _run_sudo(["mkdir", "-p", str(shared_dir)], result)
        for subdir in ["metrics",
                        *PROPOSAL_LIFECYCLE_SUBDIRS,
                        "proposals/approved",
                        "proposals/validation-results", "proposals/deployed",
                        "scoreboard", "feedback", "annotations",
                        "alerts", "applications", "plists", "logs", "kaizen",
                        # Single-writer evolve-owned subdirs: pre-create so
                        # fresh pods land at the right ownership from the
                        # start (no race window for a non-evolve writer to
                        # win the mkdir). ensure_pod_perms enforces
                        # owner=evolve + mode 0o755 every deploy, but
                        # creating the dir here closes the gap between
                        # "first writer wins" and "first ensure_pod_perms".
                        # See spec-config-intent-system-2026-05-21.md §2.6 +
                        # POD_EVOLVE_OWNED_DIR_MODE.
                        *EVOLVE_OWNED_SHARED_SUBDIRS]:
            p = shared_dir / subdir
            try:
                p.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                _run_sudo(["mkdir", "-p", str(p)], result)
        # Make multi-writer dirs sticky world-writable so every bot user can
        # create their own files without needing to own the directory.
        # metrics/annotations: each bot writes {bot_id}-{date}.json files.
        # Sticky bit (1777) prevents one bot from deleting another's files.
        for multi_writer_dir in ["metrics", "annotations"]:
            p = shared_dir / multi_writer_dir
            try:
                os.chmod(p, 0o1777)
            except PermissionError:
                _run_sudo(["chmod", "1777", str(p)], result)
        # Make proposals/ subdirs, feedback/, and tests/ world-writable so the
        # evolve admin server can write/move/delete proposal and test-run files.
        # NOTE: 0o777 *without* sticky bit — see the PROPOSAL_LIFECYCLE_SUBDIRS
        # docstring above. Re-applied here unconditionally so existing installs
        # whose ``applied/``, ``snoozed/``, or ``archived/`` dirs got created
        # on-demand by a bot writer (often inheriting 1777 from the parent or
        # picking up the bot's umask) are normalized on next deploy.
        for shared_writer_dir in [
            *PROPOSAL_LIFECYCLE_SUBDIRS,
            "proposals/approved", "proposals/rejected",
            "proposals/deployed", "proposals/validation-results",
            "feedback", "tests",
        ]:
            p = shared_dir / shared_writer_dir
            try:
                p.mkdir(parents=True, exist_ok=True)
                os.chmod(p, 0o777)
            except PermissionError:
                _run_sudo(["mkdir", "-p", str(p)], result)
                _run_sudo(["chmod", "777", str(p)], result)
        # Chown the proposal lifecycle subdirs to evolve:<admin_group> (wheel
        # macOS / root Linux) so the dir owner is the service user.
        # Files inside may still be bot-owned (legitimate: bot appliers write
        # them), but with 0o777 + no sticky, evolve can still delete them.
        for canonical_subdir in PROPOSAL_LIFECYCLE_SUBDIRS:
            p = shared_dir / canonical_subdir
            subprocess.run(
                ["sudo", _PROFILE.chown, f"evolve:{_PROFILE.admin_group}", str(p)],
                capture_output=True, check=False,
            )
        # Post-evo-account-separation invariant: proposals/ and signals/ trees
        # owned by evolve:wheel + inherited evo write ACL. _ensure_evo_write_acl
        # is a no-op for ownership and ACL when they're already in the canonical
        # state, so re-running on every deploy is cheap. Only takes effect when
        # the `evo` macOS account exists; skipped for fresh / pre-Phase-E.2.a
        # pods. See spec-evo-account-separation-2026-05-25.md and the 2026-05-25
        # post-cutover dismiss bug.
        if _evo_user_exists():
            for evo_subdir in EVO_WRITE_SHARED_SUBDIRS:
                p = shared_dir / evo_subdir
                if p.exists():
                    _ensure_evo_write_acl(p)
        # chmod 1777 (sticky world-writable) so all bot users can create their own
        # {bot_id}/turns/ subdirectory without needing root access each time.
        try:
            os.chmod(shared_dir, 0o1777)
        except PermissionError:
            _run_sudo(["chmod", "1777", str(shared_dir)], result)
        # World-read/exec non-secret subdirs so bots read each other's output (secrets/
        # re-protected right after; multi-writer dirs re-1777'd below). Prunes a nested
        # Linux deploy checkout so the widen can't flip its git exec bits → ff-only wedge.
        _secret_perms.widen_shared_dir_world_read(shared_dir, chmod=_PROFILE.chmod,
            sudo_fallback=lambda a: _run_sudo(a, result, check=False))
        _secret_perms.tighten_shared_protected_trees(shared_dir)  # undo a+rX's g/o-read on every protected tree: secrets/ (Google SA/DwD keys + OAuth tokens) + directory/ (per-bot contact emails) + keystore/ (file-vault machine key + admin-auth HMAC key)
        # Re-assert 1777 on multi-writer dirs after the a+rX pass
        for multi_writer_dir in ["metrics", "annotations"]:
            p = shared_dir / multi_writer_dir
            try:
                os.chmod(p, 0o1777)
            except PermissionError:
                _run_sudo(["chmod", "1777", str(p)], result)
        # Per-bot dir contract (shared_bot_dir_perms): turns/ stays sticky
        # 1777, and the PARENT {bot}/ gets the evolve write ACE so evolve-user
        # jobs can write usage-by-app.json there whichever user won the mkdir
        # race (atlas EACCES, 2026-08-17).
        _bot_dir_perms.reassert_per_bot_dir_perms(
            shared_dir, lambda p: _run_sudo(["chmod", "1777", str(p)], result))
        # Ensure the shared dir root itself is 1777
        try:
            os.chmod(shared_dir, 0o1777)
        except PermissionError:
            _run_sudo(["chmod", "1777", str(shared_dir)], result)
        # Fix network.json ownership — it ends up owned by the deploying user
        # (often a bot or the admin user) but must be writable by the evolve service.
        # Run unconditionally (check=False): if the file doesn't exist yet on
        # first deploy, chown is a no-op; it will be called again after the file
        # is written in deploy_bot() step 7.
        subprocess.run(
            ["sudo", _PROFILE.chown, f"evolve:{_PROFILE.admin_group}", str(shared_dir / "network.json")],
            capture_output=True, check=False,
        )
        # Normalize evolve-repo ownership to evolve:staff. Fresh clones land
        # owned by whoever ran `git clone` (often the admin user), and
        # subsequent `git pull` ticks (run as evolve) update file CONTENT but
        # not directory ownership — so any new directory under the repo
        # inherits the original cloner's uid. OC 2026.4.29's plugin scanner
        # rejects plugin dirs with uid != evolve|root and silently drops the
        # plugin's tools. Idempotent; safe to run on every setup pass.
        evolve_repo_root = Path(_get_profile().deploy_checkout_default)
        if evolve_repo_root.exists():
            r = subprocess.run(
                ["sudo", _PROFILE.chown, "-R", "evolve:staff", str(evolve_repo_root)],
                check=False, capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                result.log(f"Normalized {evolve_repo_root} ownership to evolve:staff")
            else:
                result.log(
                    f"[warn] chown -R evolve:staff {evolve_repo_root} failed "
                    f"(rc={r.returncode}): {(r.stderr or r.stdout).strip()}"
                )
            # Mode normalization: chown alone leaves dirs at 755, which
            # locks staff-group writers (the human admin) out of writing
            # objects the daemon (evolve) just created. `chmod -R g+rwX`
            # gets us 775 dirs + 664 files (X = "execute only if dir or
            # already-exec") in one pass. Pairs with the puller's `umask
            # 002` and `core.sharedRepository=group` config — together
            # they guarantee both users coexist forever rather than
            # drifting apart whenever the daemon writes new objects.
            r = subprocess.run(
                ["sudo", "/bin/chmod", "-R", "g+rwX", str(evolve_repo_root)],
                check=False, capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                result.log(
                    f"Normalized {evolve_repo_root} mode (775 dirs / 664 files)"
                )
            else:
                result.log(
                    f"[warn] chmod -R g+rwX {evolve_repo_root} failed "
                    f"(rc={r.returncode}): {(r.stderr or r.stdout).strip()}"
                )
        # Grant evolve read ACL on evolve-repo/packages/ (admin imports analyzer modules
        # like cost_ledger) — same read contract as set_evolve_read_acl(), via the perms seam.
        evolve_repo_pkgs = evolve_repo_root / "packages"
        if evolve_repo_pkgs.exists():
            get_perms().grant_read_recursive(evolve_repo_pkgs, EVOLVE_SERVICE_USER)
            result.log(f"Set evolve read ACL on {evolve_repo_pkgs}")
        # Write POD_CONDUCT.md — the shared behavioral contract for all bots
        _write_pod_conduct(shared_dir, result)
        # Seed RUNTIME_NOTES.md (create-if-absent) so the pod-scope content
        # scanner's scanned_pod_files target exists — without it the scanner
        # fires a genuine content_scan_file_disappeared for __pod__:
        # RUNTIME_NOTES.md every sweep (was live on evo-vps).
        _seed_pod_runtime_notes(shared_dir, result)
        # Build the help-doc index used by the primary bot's grounded Q&A
        # surface (`/api/evo/help/search`). Idempotent; rebuilds on every
        # shared-dir setup. Best-effort — a missing or partially-broken
        # docs/ shouldn't block a deploy.
        try:
            from . import help_index as _help_index_pkg
            # _REPO_ROOT = the deploy checkout this code loaded from
            # (/Users/Shared/evolve-repo on macOS, /var/lib/evolve/repo on
            # Linux). Byte-identical in a macOS deploy; the walk-up below stays
            # as the local-dev fallback. (W10-E /Users leak.)
            docs_root = _REPO_ROOT / "docs"
            if not docs_root.exists():
                # Fall back to the repo this code was loaded from, for
                # local-dev runs where the deploy checkout doesn't exist.
                here = Path(__file__).resolve()
                for _ in range(8):
                    candidate = here.parent / "docs"
                    if candidate.is_dir():
                        docs_root = candidate
                        break
                    here = here.parent
            if docs_root.exists():
                idx = _help_index_pkg.build.build_index(docs_root)
                _help_index_pkg.build.write_index(idx, shared_dir)
                result.log(
                    f"Built help index: {len(idx.docs)} docs from {docs_root}"
                )
            else:
                result.log(
                    "[warn] No docs/ dir found for help-index build — "
                    "primary bot Q&A will respond 'no index built'"
                )
        except Exception as e:  # noqa: BLE001
            result.log(f"[warn] help-index build failed: {e}")
        # Pod-wide perm enforcement — same contract that runs in deploy_bot.
        # Safe to call here even when no bots have been registered yet; the
        # per-bot loop is empty in that case and the call is effectively just
        # the pod-wide proposals + Cellar checks.
        try:
            net_path = shared_dir / "network.json"
            pp = ensure_pod_perms(bot_id=None, network_path=net_path, check_only=False)
            if pp.applied:
                for fix in pp.applied:
                    result.log(f"perms: applied {fix}")
            if pp.errors:
                for err in pp.errors:
                    result.log(f"[warn] perms: {err}")
        except Exception as e:
            result.log(f"[warn] ensure_pod_perms (pod-wide) failed: {e}")
        result.log(f"Shared dir ready: {shared_dir}")
    else:
        result.log(f"[dry-run] Would create {shared_dir} with mode 1777")
    return result


_MEMBER_BOT_DOCS_TEMPLATE_DIR = (
    Path(__file__).parent / "templates" / "bot_workspace"
)
# Docs Evolve ships rich templates for. SSOT lives in bot_doc_seeding, which
# also derives the create-only stub set from content_scan's required files so
# the seeded set and the scanned set can't drift (the cause of the ledger
# MEMORY.md/README.md red-flags). SOUL.md/AGENTS.md are structurally checked.
_MEMBER_BOT_DOC_FILES: tuple[str, ...] = _bot_docs.EVOLVE_SEEDED_DOCS

# Primary bots have a hand-written identity (evo, the conversational interface
# for the Evolve pod admin) that doesn't fit the generic member-bot template.
# SOUL.md + AGENTS.md ship verbatim from packages/analyzer/evolve_bot/; MEMORY.md
# and README.md fall back to the generic member templates with placeholder
# substitution so the primary still satisfies the same content_scan checks
# (#1260 baseline parity).
_PRIMARY_BOT_VERBATIM_DOCS_DIR = (
    Path(__file__).parent.parent.parent / "analyzer" / "evolve_bot"
)
_PRIMARY_BOT_VERBATIM_DOC_FILES: tuple[str, ...] = ("SOUL.md", "AGENTS.md")
_PRIMARY_BOT_TEMPLATED_DOC_FILES: tuple[str, ...] = ("MEMORY.md", "README.md")


def _doc_plan_for_role(role: str) -> list[tuple[Path, str, bool]]:
    """Return the (source_path, dest_basename, substitute_placeholders) plan
    for installing baseline docs for a bot of the given ``role``.

    Primary bots get the hand-written evo identity for SOUL.md + AGENTS.md
    (no placeholder substitution — the content is bot-specific, not templated)
    plus the generic templates for MEMORY.md + README.md (with substitution).
    Member bots get the four generic templates with substitution.
    """
    if role == "primary":
        plan: list[tuple[Path, str, bool]] = [
            (_PRIMARY_BOT_VERBATIM_DOCS_DIR / fname, fname, False)
            for fname in _PRIMARY_BOT_VERBATIM_DOC_FILES
        ]
        plan.extend(
            (_MEMBER_BOT_DOCS_TEMPLATE_DIR / fname, fname, True)
            for fname in _PRIMARY_BOT_TEMPLATED_DOC_FILES
        )
        return plan
    return [
        (_MEMBER_BOT_DOCS_TEMPLATE_DIR / fname, fname, True)
        for fname in _MEMBER_BOT_DOC_FILES
    ]


def install_bot_docs(
    bot_id: str,
    bot_user: str,
    role: str = "member",
    *,
    dry_run: bool = False,
) -> "DeployResult":
    """
    Install starter SOUL.md / AGENTS.md / MEMORY.md / README.md into a bot's
    workspace so it satisfies content_scan, then gap-fill a stub for any other
    content_scan-required doc a creation path left missing (see bot_doc_seeding).

    Idempotent: skips any destination that already holds an operator-edited file
    (differs from the template *and* ≥ the 1500-byte structural floor), so it is
    safe to call on every redeploy. Files are written via /tmp staging + sudo cp
    (the workspace is ``bot_user``-owned); ``{bot_id}``/``{role}`` placeholders
    are substituted in templated sources, verbatim sources copied byte-for-byte.
    """
    result = DeployResult(bot_id=bot_id, success=True)
    workspace_dir = _user_home(bot_user) / ".openclaw/workspace"
    plan = _doc_plan_for_role(role)

    if dry_run:
        names = ", ".join(fname for _, fname, _ in plan)
        result.log(f"[dry-run] Would install {names} into {workspace_dir}")
        return result

    for src, fname, substitute in plan:
        dst = workspace_dir / fname
        if not src.exists():
            result.log(f"[warn] Template not found, skipping: {src}")
            continue

        rendered = (
            _render_member_bot_doc(src, bot_id, role) if substitute
            else src.read_text()
        )

        # Preserve a genuine operator hand-edit (substantive + differs from rendered).
        # Primary-bot verbatim SOUL/AGENTS are exempt — see should_skip_operator_edited.
        existing_text: str | None = None
        try:
            existing = subprocess.run(
                ["sudo", "/bin/cat", str(dst)],
                capture_output=True, text=True, timeout=10,
            )
            if existing.returncode == 0:
                existing_text = existing.stdout
        except Exception:
            existing_text = None
        if _bot_docs.should_skip_operator_edited(
            existing_text, rendered, role=role, fname=fname
        ):
            result.log(f"Skipped (operator-edited): {dst}")
            continue

        _bot_docs.write_doc(
            _run_sudo, workspace_dir=workspace_dir, fname=fname, content=rendered,
            bot_user=bot_user, bot_id=bot_id, result=result,
        )

    # Primary's on-demand reference library (B6): PAGE_CONTEXT / PLAYBOOKS /
    # COMMANDS / GLOSSARY under workspace evolve/reference/. The generated evo
    # glossary lands in GLOSSARY.md there — no longer appended to AGENTS.md —
    # so the ingested AGENTS.md stays under OC's bootstrapMaxChars cap
    # (the 2026-08-01 silent-truncation incident; spec-evolve-overhead-budget §B6).
    if role == "primary":
        _bot_docs.install_primary_reference_docs(
            _run_sudo, workspace_dir=workspace_dir,
            bot_user=bot_user, bot_id=bot_id, result=result,
        )

    # Guarantee every content_scan-required file exists — close the gap that
    # red-flagged ledger (install_bot_docs is the SOLE seeder of MEMORY.md/
    # README.md; onboard never writes them, and a skipped/failed deploy leaves
    # them absent undetected). Stub-fill any onboard-owned doc a path missed,
    # then surface any required doc STILL missing as a hard error.
    def _present(fn: str) -> bool:
        pr = _run_sudo(["cat", str(workspace_dir / fn)], result, check=False)
        return bool(pr and pr.returncode == 0)

    for fn, stub in _bot_docs.plan_gap_fill(bot_id, _present):
        _bot_docs.write_doc(
            _run_sudo, workspace_dir=workspace_dir, fname=fn, content=stub,
            bot_user=bot_user, bot_id=bot_id, result=result,
            label="Gap-filled required doc",
        )
    still_missing = _bot_docs.missing_required(_present)
    if still_missing:
        result.error(
            f"content_scan-required docs missing after seeding {bot_id}: "
            f"{', '.join(still_missing)}"
        )

    return result


def install_member_bot_docs(
    bot_id: str,
    bot_user: str,
    role: str = "member",
    *,
    dry_run: bool = False,
) -> "DeployResult":
    """Backward-compatible wrapper around :func:`install_bot_docs`.

    Pre-existing callers (and external scripts) that imported this name
    keep working; new code should call :func:`install_bot_docs` directly.
    """
    return install_bot_docs(bot_id, bot_user, role=role, dry_run=dry_run)


def _render_member_bot_doc(template_path: Path, bot_id: str, role: str) -> str:
    """Substitute {bot_id} and {role} placeholders in a member-bot template.

    Uses str.replace rather than str.format so the template can contain
    other curly-brace text (e.g. ``{other_bot}`` in handoff guidance) without
    raising KeyError. Placeholders are intentionally narrow.
    """
    text = template_path.read_text()
    return text.replace("{bot_id}", bot_id).replace("{role}", role)


def install_evolve_bot_docs(dry_run: bool = False) -> "DeployResult":
    """
    Deploy SOUL.md, AGENTS.md, MEMORY.md, and README.md to the Evolve bot's
    workspace, plus create the procedures/ subdirectory.

    Source: packages/analyzer/evolve_bot/{SOUL,AGENTS}.md (verbatim — hand-
    written evo identity) and packages/admin/evolve_admin/templates/bot_workspace/
    {MEMORY,README}.md (with {bot_id} + {role} substitution).
    Dest:   the primary bot's RESOLVED home via :func:`_resolve_evolve_app_target`
            (#3063 EVOLVE-ACCT-OCJSON) — /home/evo on Linux, byte-identical
            /Users/evolve on macOS. The old ``_bot_user_for("evolve")`` passed the
            literal id → fresh Linux fell back to /home/evolve where the evo
            gateway never reads. The LOGICAL bot-id arg stays "evolve" (namespace
            label + ``{bot_id}`` rendered into MEMORY/README, NOT a lookup key —
            the account resolves separately), so macOS rendered bytes are unchanged.

    Idempotent — see :func:`install_bot_docs` for the operator-edit guard.
    """
    _primary_id, _evolve_acct, _evolve_oc = _resolve_evolve_app_target()
    result = install_bot_docs("evolve", _evolve_acct, role="primary", dry_run=dry_run)
    procedures_dir = _evolve_oc / "workspace/procedures"

    if dry_run:
        result.log(f"[dry-run] Would create {procedures_dir}")
        return result

    _run_sudo(["mkdir", "-p", str(procedures_dir)], result)
    if result.success:
        _run_sudo(["chown", f"{_evolve_acct}:staff", str(procedures_dir)], result)
        result.log(f"Ensured procedures dir: {procedures_dir}")

    return result


def _evolve_managed_cron_names(shared_dir: Path | None = None) -> list[str]:
    """Cron job names Evolve installs on the evolve bot via FIRST_PARTY_EVOLVE_APPS.

    Union across each app manifest's ``crons[].name``. The audit baseline
    rebless writes this as ``baseline["evolve"]`` after each deploy so
    Evolve's own additions stop firing "new cron job not in baseline"
    warns on the next audit run.
    """
    app_root = Path(__file__).parent.parent.parent / "analyzer" / "evolve_apps"
    names: set[str] = set()
    for app_id in FIRST_PARTY_EVOLVE_APPS:
        manifest_path = app_root / app_id / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for entry in manifest.get("crons", []):
            if isinstance(entry, dict):
                name = entry.get("name")
                if isinstance(name, str) and name:
                    names.add(name)
    return sorted(names)


def _rebless_cron_baseline(
    bot_id: str,
    known_job_names: list[str],
    shared_dir: Path,
    result: DeployResult,
) -> None:
    """Set ``baseline[bot_id]`` in cron-jobs.json to Evolve's known cron set.

    Atomic temp + os.replace; preserves other bots' entries. The baseline
    file lives under shared_dir which is evolve-owned, so deploy (running
    as evolve via launchd, or as root via ``sudo evolve-admin``) can write
    it directly without /tmp staging or sudo.

    Intentionally writes Evolve's *known* set rather than the current
    on-disk jobs.json — otherwise a bot's self-installed cron (e.g.
    security_bot's ``usage-alert-dispatch``) would be silently blessed on
    every deploy, defeating the drift alert.
    """
    baseline_path = shared_dir / "security" / "baselines" / "cron-jobs.json"
    try:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        result.log(f"[warn] cron baseline rebless: cannot mkdir {baseline_path.parent}: {e}")
        return

    data: dict[str, list[str]] = {}
    if baseline_path.exists():
        try:
            loaded = json.loads(baseline_path.read_text())
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError) as e:
            result.log(f"[warn] cron baseline rebless: existing file unreadable ({e}); starting fresh")
            data = {}

    new_value = sorted(known_job_names)
    if data.get(bot_id) == new_value:
        return

    data[bot_id] = new_value
    tmp = baseline_path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, baseline_path)
        result.log(
            f"Reblessed cron baseline for {bot_id}: {len(new_value)} job name(s)"
        )
    except OSError as e:
        result.log(f"[warn] cron baseline rebless write failed: {e}")
        try:
            tmp.unlink()
        except OSError:
            pass


# Manifest-cron → OpenClaw wire-schema translation lives in cron_wire — see
# that module's docstring for the silent-quarantine failure it prevents.
from .cron_wire import (  # noqa: E402
    _cron_entry_needs_shape_heal,
    _is_no_delivery,
    _normalize_cron_delivery,
    _normalize_cron_wire,
)


def _merge_cron_entries(jobs_path: Path, new_entries: list[dict], result: DeployResult, owner: str = "evolve") -> None:
    """Merge cron entries from a manifest into jobs.json without duplicating by name."""
    # Read existing jobs.json
    existing_text: str | None = None
    try:
        existing_text = jobs_path.read_text()
    except (PermissionError, FileNotFoundError):
        proc = subprocess.run(["sudo", "/bin/cat", str(jobs_path)], capture_output=True, text=True)
        if proc.returncode == 0:
            existing_text = proc.stdout
        else:
            existing_text = None

    if existing_text:
        try:
            jobs_data = json.loads(existing_text)
        except json.JSONDecodeError:
            jobs_data = {"jobs": []}
    else:
        jobs_data = {"jobs": []}

    existing_jobs = jobs_data.setdefault("jobs", [])
    existing_by_name = {j.get("name"): j for j in existing_jobs}
    added = 0
    reconciled = 0
    for entry in new_entries:
        name = entry.get("name")
        if name not in existing_by_name:
            try:
                wire = _normalize_cron_wire(entry)
            except ValueError as e:
                # Never write the raw legacy shape: OpenClaw ≥2026.7 quarantines
                # it silently and the job never runs. Fail loudly instead.
                result.error(f"cron entry {name!r}: {e} — refusing to install it raw")
                continue
            existing_jobs.append(wire)
            existing_by_name[name] = wire
            added += 1
            continue

        existing = existing_by_name[name]
        if _cron_entry_needs_shape_heal(existing):
            # A previously-installed legacy-shape entry would be quarantined by
            # the ≥2026.7 import. Translate the EXISTING entry's own values in
            # place — operator task text/schedule preserved, only the shape
            # migrates. Heal failure is non-fatal: as-is keeps the status quo
            # and the jobs-quarantine audit finding is the backstop.
            try:
                healed = _normalize_cron_wire(existing)
            except ValueError as e:
                result.log(f"[warn] cron entry {name!r} has legacy shape but cannot be translated: {e}")
            else:
                existing.clear()
                existing.update(healed)
                reconciled += 1
        if _is_no_delivery(_normalize_cron_delivery(entry)) and not _is_no_delivery(existing):
            # Heal a previously-installed file-only cron still carrying the
            # announce default — it errors "Delivering to Telegram requires
            # target <chatId>" on a chatId-less primary account (#3151 Facet B).
            # ONLY the delivery field; never the operator's schedule/payload.
            existing["delivery"] = {"mode": "none"}
            reconciled += 1

    if added == 0 and reconciled == 0:
        if result.success:
            result.log(f"Cron entries already present in {jobs_path} — nothing to merge")
        return

    payload = json.dumps(jobs_data, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir="/tmp", prefix="evolve-cron-jobs-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        why = sudo_dest_refusal(jobs_path)  # D-2: .openclaw/cron/ is bot-owned, and the merge above read this file back through the link, so the bot picks the bytes root writes
        if not why:
            _run_sudo(["cp", tmp_path, str(jobs_path)], result)
            why = sudo_dest_refusal(jobs_path)  # re-assert before the chown
        if why:
            result.error(f"Refusing cron jobs.json write: {why}")
        elif result.success:
            _run_sudo(["chown", f"{owner}:staff", str(jobs_path)], result)
            summary = f"Merged {added} cron entry/entries into {jobs_path}"
            if reconciled:
                summary += f" ({reconciled} existing entry field(s) healed in place)"
            result.log(summary)
            # OpenClaw ≥2026.7 imports jobs.json once, at gateway start.
            result.log("Note: on OpenClaw ≥2026.7 the gateway imports cron/jobs.json at startup — restart the bot's gateway to activate merged entries")
    except Exception as e:
        result.error(f"Failed to write cron jobs.json: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _resolve_evolve_app_target(
    network: "dict[str, Any] | None" = None,
) -> "tuple[str, str, Path]":
    """Resolve the (logical bot_id, OS account, .openclaw dir) the first-party
    Evolve apps install onto: the pod's PRIMARY bot — NOT the literal ``evolve``
    service account (post evo-account-separation that account runs only the admin
    daemon and carries NO openclaw.json; spec-evo-account-separation-2026-05-25).

    The replaced ``_bot_user_for("evolve")`` fell back to the literal "evolve"
    on every fresh pod (no ``bots.evolve`` entry — the primary is "evo"), so on
    Linux the artifacts + required_tools patch landed under /home/evolve where the
    evo gateway (HOME=/home/evo) never reads (EVOLVE-ACCT-OCJSON). Resolution now
    mirrors status.py #3052 / setup_status #3047:
    ``primary_bot_id → get_bot_user → user_home``.

    macOS byte-identity: fresh macOS has primary "evo" on account "evolve"
    (pre-cutover) → /Users/evolve; legacy pods → bot "evolve" → /Users/evolve —
    unchanged from the old ``_PROFILE.name == "macos"`` hardcode either way.
    """
    if network is None:
        network = load_network()
    from primary_bot import primary_bot_id as _primary_bot_id  # type: ignore
    # NOTE: the degenerate-network ``or "evolve"`` fallback is INTENTIONAL and
    # tested (test_evolve_app_required_tools_target — macOS byte-identity proof
    # #3052: every macOS shape including the empty/degenerate network must
    # resolve to /Users/evolve, never crash). The EVO-GATEWAY-RESIDUE-RERENDER
    # fix lives at the gateway-provision funnel (_is_provisionable_bot in
    # install_bot_gateway_plist / _restart_gateway_linux) + the restart-gateways
    # roster, NOT here — first-party APP install onto the primary's .openclaw is
    # a different surface from rendering a phantom gateway UNIT, and it keeps the
    # byte-identical legacy behaviour. Do not fail-close this one.
    bot_id = _primary_bot_id(network) or "evolve"
    account = _bot_user_for(bot_id, network)
    return bot_id, account, _user_home(account) / ".openclaw"


def install_evolve_app(app_id: str, shared_dir: Path, dry_run: bool = False) -> "DeployResult":
    """
    Install a first-party manifest app on the pod's primary (Evolve) bot.

    Installs three artifacts:
    1. manifest.json → shared_dir/applications/evolve/{app_id}.json
    2. procedure.md  → <primary-bot home>/.openclaw/workspace/procedures/{app_id}.md
    3. cron entries  → merged into <primary-bot home>/.openclaw/cron/jobs.json

    Idempotent: safe to call on re-deploy.
    Does NOT require the Evolve gateway to be running.
    """
    result = DeployResult(bot_id="evolve", success=True)
    # Primary-bot account + .openclaw home for the procedure/cron/openclaw
    # artifacts below — see _resolve_evolve_app_target (Linux→/home/evo where the
    # gateway reads; macOS byte-identical at /Users/evolve).
    _primary_id, _evolve_acct, _evolve_oc = _resolve_evolve_app_target()
    app_src_dir = Path(__file__).parent.parent.parent / "analyzer" / "evolve_apps" / app_id
    manifest_src = app_src_dir / "manifest.json"
    procedure_src = app_src_dir / "procedure.md"

    if not manifest_src.exists():
        result.error(f"App manifest not found: {manifest_src}")
        return result

    try:
        manifest = json.loads(manifest_src.read_text().replace("{shared_dir}", str(shared_dir)))
    except (json.JSONDecodeError, OSError) as e:
        result.error(f"Failed to read manifest for {app_id}: {e}")
        return result

    if dry_run:
        result.log(f"[dry-run] Would install app '{app_id}' from {app_src_dir} onto primary bot '{_primary_id}' ({_evolve_acct})")
        result.log(f"[dry-run]   manifest.json → {shared_dir}/applications/evolve/{app_id}.json")
        if procedure_src.exists():
            result.log(f"[dry-run]   procedure.md → {_evolve_oc}/workspace/procedures/{app_id}.md")
        if manifest.get("crons"):
            result.log(f"[dry-run]   {len(manifest['crons'])} cron entry/entries → {_evolve_oc}/cron/jobs.json")
        return result

    # 1. Copy manifest.json → shared_dir/applications/evolve/{app_id}.json
    app_manifest_dir = shared_dir / "applications" / "evolve"
    _run_sudo(["mkdir", "-p", str(app_manifest_dir)], result)
    if not result.success:
        return result

    manifest_dst = app_manifest_dir / f"{app_id}.json"
    fd, tmp_path = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-app-{app_id}-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(manifest_src.read_text().replace("{shared_dir}", str(shared_dir)))
        _run_sudo(["cp", tmp_path, str(manifest_dst)], result)
        if result.success:
            result.log(f"Installed manifest: {manifest_dst}")
    except Exception as e:
        result.error(f"Failed to copy manifest for {app_id}: {e}")
        return result
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if not result.success:
        return result

    # 2. Copy procedure.md → <evolve home>/.openclaw/workspace/procedures/{app_id}.md
    if procedure_src.exists():
        procedures_dir = _evolve_oc / "workspace/procedures"
        _run_sudo(["mkdir", "-p", str(procedures_dir)], result)
        procedure_dst = procedures_dir / f"{app_id}.md"
        fd, tmp_path = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-proc-{app_id}-", suffix=".md")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(procedure_src.read_text().replace("{shared_dir}", str(shared_dir)))
            why = sudo_dest_refusal(procedure_dst)  # D-2: procedures/ is bot-owned
            if not why:
                _run_sudo(["cp", tmp_path, str(procedure_dst)], result)
                why = sudo_dest_refusal(procedure_dst)  # re-assert before the chown
            if why:
                result.error(f"Refusing procedure install for {app_id}: {why}")
            elif result.success:
                _run_sudo(["chown", f"{_evolve_acct}:staff", str(procedure_dst)], result)
                result.log(f"Installed procedure: {procedure_dst}")
        except Exception as e:
            result.error(f"Failed to copy procedure for {app_id}: {e}")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if not result.success:
        return result

    # 3. Merge cron entries into <evolve home>/.openclaw/cron/jobs.json
    crons = manifest.get("crons", [])
    if crons:
        cron_dir = _evolve_oc / "cron"
        _run_sudo(["mkdir", "-p", str(cron_dir)], result)
        # chown the dir to the owning account: the GATEWAY (running as the bot)
        # writes its own store files here — OpenClaw ≥2026.7 stages its import
        # migration temp file in this dir and dies ("Failed writing migrated
        # cron store" in gateway.log) when a fresh sudo-mkdir left it root-owned.
        _run_sudo(["chown", f"{_evolve_acct}:staff", str(cron_dir)], result)
        jobs_path = cron_dir / "jobs.json"
        _merge_cron_entries(jobs_path, crons, result, owner=_evolve_acct)
        # AL-1.2: record cron name → app_id so the plugin can grade the firing turn "scheduled" (logic in app_cron_map.py — deploy.py is size-capped).
        merge_app_cron_map(shared_dir, _primary_id, {c["name"]: app_id for c in crons if isinstance(c.get("name"), str)}, result)

    # 4. Patch openclaw.json if required_tools specified
    required_tools = manifest.get("_install", {}).get("required_tools", {})
    if required_tools:
        oc_json_path = _evolve_oc / "openclaw.json"
        oc_text: str | None = None
        try:
            oc_text = oc_json_path.read_text()
        except (PermissionError, FileNotFoundError):
            proc = subprocess.run(
                ["sudo", "/bin/cat", str(oc_json_path)],
                capture_output=True, text=True,
            )
            if proc.returncode == 0:
                oc_text = proc.stdout

        if oc_text:
            try:
                oc_cfg = json.loads(oc_text)
                changed = False
                for dotted_key, value in required_tools.items():
                    # Walk/create nested dict from dotted key
                    parts = dotted_key.split(".")
                    node = oc_cfg
                    for part in parts[:-1]:
                        node = node.setdefault(part, {})
                    if node.get(parts[-1]) != value:
                        node[parts[-1]] = value
                        changed = True
                if changed:
                    payload = json.dumps(oc_cfg, indent=2)
                    fd, tmp_path = tempfile.mkstemp(
                        dir="/tmp", prefix=f"evolve-oc-{app_id}-", suffix=".json"
                    )
                    try:
                        with os.fdopen(fd, "w") as f:
                            f.write(payload)
                        why = sudo_dest_refusal(oc_json_path)  # D-2: primary bot owns .openclaw
                        if not why:
                            _run_sudo(["cp", tmp_path, str(oc_json_path)], result)
                            why = sudo_dest_refusal(oc_json_path)  # re-assert before the chmod, which follows a link too
                        if why:
                            result.error(f"Refusing required_tools patch for {app_id}: {why}")
                        elif result.success:
                            # openclaw.json carries tokens → MUST be 0600. The dest always
                            # pre-exists here (we only patch what we just read), so `cp`
                            # (no -p) PRESERVES its owner — no chown needed.
                            from .secret_config_perms import chmod_secret_config
                            chmod_secret_config(oc_json_path)
                            result.log(f"Patched openclaw.json with required_tools for {app_id} (primary bot '{_primary_id}', {_evolve_acct})")
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                else:
                    result.log(f"openclaw.json already has required_tools for {app_id}")
            except (json.JSONDecodeError, Exception) as e:
                result.log(f"[warn] Could not patch openclaw.json for {app_id}: {e}")
        else:
            result.log(f"[warn] primary bot '{_primary_id}' openclaw.json absent at {oc_json_path}; cannot enable required_tools for {app_id}. The primary bot's OC config is provisioned during setup (setup_wizard `_provision_evo_oc`) and should exist by deploy time — investigate if this persists (a genuinely missing primary OC config, not a benign deferral).")

    return result


def install_evolve_infra_jobs(
    evolve_dir: Path,
    dry_run: bool = False,
    shared_dir: Path | None = None,
) -> DeployResult:
    """Install all infra launchd jobs on the evolve user.

    All jobs run as the 'evolve' macOS user (not a bot user).
    Called automatically during fresh setup (setup_wizard.py Step 13) and
    available as 'evolve-admin install-infra-jobs' for manual re-runs.
    """
    result = DeployResult(bot_id="evolve", success=True)
    if not dry_run:
        # Phase 6.1: the daemons import analyzer modules as an installed
        # package (the per-call-site sys.path inserts are gone). Make sure
        # the venv has it before (re)installing anything that loads it.
        try:
            _analyzer_err = ensure_analyzer_installed()
            if _analyzer_err:
                result.log(f"[warn] evolve-analyzer install: {_analyzer_err}; continuing")
            else:
                result.log("evolve-analyzer present in venv (editable)")
        except Exception as exc:  # noqa: BLE001
            result.log(f"[warn] evolve-analyzer install raised: {exc}; continuing")
        # Normalize the shared directory layout + permissions BEFORE
        # installing the daemons that read/write under it. This is the
        # only path through which the dir-mode fixes (PR #1019) get
        # applied to an existing install — without this, the
        # ``proposals/applied/`` etc. sticky-bit fix only takes effect
        # on a full ``deploy <bot>`` run. Idempotent.
        _shared = shared_dir or Path("/Users/Shared/evolve")
        try:
            shared_result = deploy_shared_dir(_shared)
            result.steps.extend(shared_result.steps)
            if not shared_result.success:
                # Surface but don't abort — the daemons may still install
                # and an operator can re-run after fixing the underlying
                # permission issue.
                result.log("[warn] deploy_shared_dir reported failures; continuing")
        except Exception as exc:  # noqa: BLE001
            result.log(f"[warn] deploy_shared_dir raised: {exc}; continuing")
        _install_launchd_analyze("evolve", evolve_dir, result)
        _install_launchd_outcome("evolve", evolve_dir, result)
        _install_launchd_defer_runner("evolve", evolve_dir, result)
        _install_launchd_manifest_reflex_runner("evolve", evolve_dir, result)
        _install_launchd_app_posture_review("evolve", evolve_dir, result)
        _install_launchd_slack_signals("evolve", evolve_dir, result)
        _install_launchd_expansion("evolve", evolve_dir, result)
        _install_launchd_spend_alert("evolve", evolve_dir, result)
        _install_launchd_cron_alert("evolve", evolve_dir, result)
        _install_launchd_weekly_review("evolve", result)
        _install_launchd_weekly_bot_trends("evolve", result)
        _install_launchd_usage_jobs("evolve", result)
        # Per-bot backup daemons (`ai.evolve.<bot>.backup`) are installed
        # by deploy_bot() during each bot's deploy. The pre-2026-05-25
        # central evolve daemon (`ai.evolve.evolve.backup` that iterated
        # all bots) is gone; evo gets its own per-bot daemon like
        # everyone else. The vestigial `ai.evolve.security_bot-backup` plist
        # (pointed at a bin/backup.sh that doesn't exist) is unused.
        _install_launchd_pod_report_daily("evolve", result)
        _install_launchd_heal("evolve", result)
        _install_launchd_pod_health("evolve", result)
        _install_launchd_signal_notifier("evolve", result)
        _install_launchd_better_engine("evolve", result)
        _install_launchd_tuples("evolve", result)
        _install_launchd_measure(result)
        _install_launchd_audit("evolve", result)
        _install_launchd_update_watcher("evolve", result)
        # Gated dev-tier features — only installed when the operator opts in
        # via install.json::feature_profile or explicit per-feature flag.
        # See docs/spec-upstream-issue-watcher-2026-05-22.md.
        _maybe_install_launchd_upstream_issues_watcher(result, shared_dir=_shared)
        # Issue-Inbox Phase 4: triage queue depends on a captured stream of
        # inbound issues. Same dev-tier posture as the upstream watcher above.
        _maybe_install_launchd_inbound_issues_watcher(result, shared_dir=_shared)
        _install_launchd_retention("evolve", result)
        _install_launchd_log_cap("evolve", result)
        _install_launchd_oc_log_rotate("evolve", result)
        _install_launchd_openclaw_overrides_expiry("evolve", result)
        _install_launchd_proposal_auto_resolve("evolve", result)
        _install_launchd_breakers_audit("evolve", result)
        # Circuit-breakers detector runner — every 10 min. Evaluates the
        # activity-shape detector, logs decisions to {shared}/breakers/
        # runner-log/, and acts on trips unless breakers.auto_trip_enabled
        # is false (default true — ARMED since the §5.2 arming PR;
        # `evolve-admin breaker disarm` returns it to observe-only).
        # Spec: docs/spec-circuit-breakers-2026-05-21.md §5.1 / §8 Phase 5.
        _install_launchd_breakers_runner("evolve", result)
        _install_launchd_autonomy_limits("evolve", result)
        _install_launchd_anthropic_admin_ingest("evolve", result)
        _install_launchd_proposal_synthesizer("evolve", result)
        _install_launchd_cost_watchdog("evolve", result)
        _install_launchd_session_economics("evolve", result)
        _install_launchd_embedding_monitor("evolve", result)
        _install_launchd_alerts_loop_monitor(result)
        from .analyzer_monitor_jobs import install_analyzer_monitor_jobs
        install_analyzer_monitor_jobs(result)
        _install_launchd_deploy_drift_monitor(result)
        _install_launchd_bot_recovery_monitor(result)
        _install_launchd_stuck_proposal_monitor(result)
        _install_launchd_backup_signal(result)
        _install_launchd_local_backup_signal(result)
        _install_launchd_backup_audit_signal(result)
        _install_launchd_local_backup_excluder(result)
        _install_launchd_monitor_coverage(result)
        # Daily install-integrity monitor: runs the wizard verify gauntlet's
        # non-interactive checks (ownership, agent dry-run, channel handshake)
        # per bot and emits Signals when install drift is detected. Spec:
        # docs/spec-wizard-verification-gauntlet-2026-05-30.md.
        _install_launchd_install_integrity_monitor(result)
        # Hourly OC substrate freshness monitor: emits Signals when the
        # ai.openclaw.updater LaunchAgent state or the ai.openclaw.usage-
        # collector LaunchAgent stop advancing. Both daemons sit outside
        # the ai.{evolve,openclaw}.evolve.* namespace that monitor_coverage
        # watches, so without this their silence was only caught by the
        # decommissioned pod-admin-user-side openclaw-watchdog.py script.
        _install_launchd_oc_substrate_monitor(result)
        # Daily scheduled_actions[] drift audit: walks every installed
        # app's actions vs the current gallery and emits Signals per
        # drifted (bot, app) pair. The 2026-06-04 Atlas Daily Digest
        # incident motivated this — had it existed, the silence would
        # have surfaced as a Signal on the Alerts page the next morning
        # instead of waiting six days for the operator to notice.
        _install_launchd_reconcile_audit(result)
        # Daily digest-source health audit: walks every bot's digest/
        # source_health-*.json files (written by atlas_digest.py at
        # cron-end) and emits a Signal per source that has been dark
        # for ≥3 consecutive runs. Catches RSS retirements, URL
        # refactors, and persistent upstream errors before they
        # silently shrink the digest's coverage.
        _install_launchd_digest_source_audit(result)
        # Daily agent-bypass + app-script-failure audit: walks recent session
        # transcripts on bots with at-risk apps installed (today: atlas-on-
        # demand-research and atlas-article-capture). Two sibling producers
        # (run_agent_bypass_audit.py wrapper): one emits a Signal per (bot, app)
        # where a triggering message arrived but the declared script was NOT
        # invoked (agent_bypass); the other where the script WAS invoked but
        # FAILED — the "(agent) failed" chip (app_script_failure). Surfaces the
        # agent-freelance bypass class gap. Pure Python, no LLM, idempotent.
        _install_launchd_agent_bypass_audit(result)
        # Hourly home-artifacts monitor: per-bot Signal producer for new
        # large/executable files in .openclaw/workspace/ and recent
        # macOS Quarantine downloads. Replaces the watchdog's
        # check_large_files_and_executables + check_quarantine_log.
        _install_launchd_home_artifacts_monitor(result)
        # 30-min gmail integration health monitor: per-bot probe of Google
        # API health for every bot with a google_integration block. Catches
        # SA key rotation, DwD authorization pulls, Workspace user
        # suspension, scope-list drift. Spec: docs/spec-google-integration-
        # paths-2026-05-30.md §8 (PR δ).
        _install_launchd_gmail_integration_health(result)
        # 5-min proactive-delivery monitor: per-window delivery outcomes for
        # scheduled user-facing apps (did the 7:00 briefing reach the user by
        # 7:30?). Tri-state honest — did_not_run / ran_undelivered /
        # unmeasurable — plus the delivery ledger U0's value metrics read.
        # Detection only; heal path is PR 2 of the spec.
        # Spec: docs/spec-proactive-delivery-monitor-2026-06-10.md.
        _install_launchd_delivery_monitor(result)
        # Tier-cascade pressure watchdog: 60s heartbeat that writes
        # pressure_flags.json from cascade spans + in-process tier1
        # counters. CascadeController reads the flags to throttle
        # escalation when the pod is under pressure. Safe to install
        # day-one — the watchdog is read-only against telemetry and
        # the file it writes is auto-bootstrap: when no flags fire,
        # the file is just a heartbeat timestamp. See spec § pressure
        # watchdog at docs/spec-tier-cascade-2026-05-26.md.
        _install_launchd_cascade_pressure_watchdog(result)
        # Tier-cascade audit runner: hourly bridge from cascade
        # telemetry into the pod's standard alerting + calibration
        # layers. Three Signal types (anomaly, dangerous-combo,
        # runaway-rate) plus per-day persistence of labeled outcomes
        # for the Phase 4 tuner. Safe to install day-one — on an empty
        # pod the runner is a no-op; signals only fire when real
        # cascade telemetry accumulates. See spec § audit layer.
        _install_launchd_cascade_audit_runner(result)
        # Pod-perms drift monitor: hourly check_only pass over the same
        # contract ensure_pod_perms() enforces. Catches the class where a
        # per-bot daemon (running as a bot user) is the first writer to
        # a shared dir, so the dir ends up bot-owned. With sticky 1777,
        # only the dir owner can rename foreign files — so the admin
        # server's cross-user operations (e.g. dismissing a proposal
        # owned by a different bot daemon) fail with EACCES. The
        # ensure_pod_perms code path already enforces the right
        # contract; this daemon closes the gap between deploys by
        # emitting a Signal when drift accumulates.
        _install_launchd_pod_perms_drift_monitor(result)
        # Code-quality monitor: daily Signal producer for repo-process KPIs
        # (revert rate, fix-heavy scopes, same-day fix-on-feat). Catches
        # dev-workflow drift before the next revert lands. Reads the deploy
        # checkout's git history; emits pod-scope Signals. Cheap (~5s).
        _install_launchd_code_quality_monitor(result)
        # Signal-subscriber: long-running daemon (KeepAlive) that watches
        # {shared_dir}/signals/firing/ and dispatches any generator whose
        # charter declares ``subscribes_to: [<signal_type>, ...]`` as soon
        # as a matching Signal lands. Closes the latency gap between Signal
        # arrival and generator response. The daily generator_runner sweep
        # stays as the safety net for downtime and unsubscribed generators.
        # Spec: docs/spec-signal-subscriber-2026-05-31.md.
        _install_launchd_signal_subscriber(result)
        _install_launchd_verify("evolve", result)
        _install_launchd_admin_ui("evolve", result)
        # MCP bridge — system-scope LaunchDaemon as of PR fix/mcp-bridge-system-launchd.
        # Reads network.json::mcp_bridge for port; default 5051 listening on
        # 0.0.0.0 so Tailscale peers (admin's laptop running Claude Desktop)
        # can connect. Skip if mcp_bridge is explicitly disabled in network.json.
        try:
            from .config import load_network, DEFAULT_NETWORK_CONFIG
            _mcp_cfg = (load_network(DEFAULT_NETWORK_CONFIG).get("mcp_bridge") or {})
            if _mcp_cfg.get("enabled", True):
                _install_launchd_mcp_bridge("evolve", result)
            else:
                result.log("mcp-bridge: disabled in network.json, skipping plist install")
        except Exception as _exc:  # noqa: BLE001
            # Network.json may not exist on a fresh install; install with defaults.
            _install_launchd_mcp_bridge("evolve", result)
        # Auto-pull the deployed repo every 15min so daemons see new code
        # without a manual `ssh mini 'git pull'` after every PR merge.
        # See packages/admin/evolve_admin/repo_puller.py for design notes.
        from . import repo_puller as _rp
        _rp.install_launchd(result_logger=result.log)

        # Audit scheduler — hourly. Fires pod-wide infra audit + drains
        # per-bot audit outboxes into the Signal store. Renamed from
        # app-test-scheduler on 2026-06-08 when the app-test surface was
        # killed; install_launchd also boots-out the legacy label.
        # See docs/decision-app-tests-2026-06-08.md and
        # packages/admin/evolve_admin/applications/audit_scheduler.py.
        try:
            from .applications import audit_scheduler as _sched
            ok = _sched.install_launchd()
            result.log(
                "audit-scheduler: installed (hourly tick)"
                if ok else
                "audit-scheduler: install attempted but bootstrap reported failure"
            )
        except Exception as _e:
            result.log(f"[warn] audit-scheduler install skipped: {_e}")

        # Pairing auto-approver — 30s sweep. Honors pod-admin claims
        # (existing), primary-owner claims, per-channel auto_admit
        # mode, plus the overlay block index. Inline GET-time sweep in
        # routes_bot_users now delegates to the same module so both
        # paths share the auto-approval logic. Spec:
        # docs/spec-user-roster-and-roles-2026-06-07.md §11.
        try:
            from .pairing import auto_approver as _pair_sweep
            ok = _pair_sweep.install_launchd()
            result.log(
                "pairing-sweep: installed (30s sweep — auto-approves "
                "pod admins, primary owners, and auto_admit channels; "
                "always skips blocked identities)"
                if ok else
                "pairing-sweep: install attempted but bootstrap "
                "reported failure"
            )
        except Exception as _e:
            result.log(f"[warn] pairing-sweep install skipped: {_e}")

        # Digest flusher — hourly, self-gates to digest_hour_local.
        # Phase G of docs/spec-alert-subscriptions-2026-05-10.md. Each
        # hourly tick is a cheap no-op outside the operator's configured
        # digest hour; exactly one tick per day (or per week) drains the
        # queue. Safe to install dark — queue is empty until an operator
        # picks a digest frequency for some catalog event.
        try:
            from .alerts import digest_dispatcher as _digest
            ok = _digest.install_launchd()
            result.log(
                "digest-flush: installed (hourly tick, self-gates to "
                "digest_hour_local)"
                if ok else
                "digest-flush: install attempted but bootstrap reported failure"
            )
        except Exception as _e:
            result.log(f"[warn] digest-flush install skipped: {_e}")

        # Security-CVE-scan finalizer — daily at 09:10 America/Los_Angeles,
        # ten minutes after the LLM discovery cron. Reads the candidate
        # JSON the LLM produced, applies installed-version + baseline +
        # idempotency filters, renders the operator-facing message per
        # docs/operator-message-style.md, and dispatches via the security
        # Telegram channel. Safe to install dark — finalizer no-ops if
        # there is no candidate JSON for today.
        _install_launchd_cve_scan_finalize("evolve", result)
    else:
        result.log("[dry-run] Would install evolve infra launchd jobs (as evolve user)")

    # Deploy bot identity docs and first-party apps
    _effective_shared_dir = shared_dir or Path("/Users/Shared/evolve")
    docs_result = install_evolve_bot_docs(dry_run=dry_run)
    result.steps.extend(docs_result.steps)
    result.errors.extend(docs_result.errors)
    if not docs_result.success:
        result.success = False

    for app_id in FIRST_PARTY_EVOLVE_APPS:
        app_result = install_evolve_app(app_id, shared_dir=_effective_shared_dir, dry_run=dry_run)
        result.steps.extend(app_result.steps)
        result.errors.extend(app_result.errors)
        if not app_result.success:
            result.success = False

    # Sync audit baseline so Evolve's own cron additions/removals stop firing
    # "new cron job not in baseline" warns. Crons land on the PRIMARY bot, and
    # audit_cron_health reads baseline[<logical bot_id>] — so key the baseline by
    # the primary's id ("evo" on fresh pods; "evolve" on legacy/macOS). Keying a
    # bare "evolve" while crons live under "evo" would fire spurious Linux drift.
    if not dry_run:
        _primary_id_for_baseline, _, _ = _resolve_evolve_app_target()
        _rebless_cron_baseline(
            _primary_id_for_baseline,
            _evolve_managed_cron_names(_effective_shared_dir),
            _effective_shared_dir,
            result,
        )

    # Restart the Evolve gateway so the newly deployed SOUL.md, AGENTS.md, and
    # app procedure docs are loaded into the next session.  Best-effort — a
    # failure here doesn't fail the overall install. Label = resolved primary.
    if not dry_run:
        try:
            _gw = per_bot_gateway_plist_label(_resolve_evolve_app_target()[0])
            restarted, _restart_out = get_scheduler().restart(_gw)
            if restarted:
                result.log("Evolve gateway restarted — identity docs and apps are live")
            else:
                result.log(
                    "[warn] Could not restart evolve gateway automatically — "
                    "it may not have been bootstrapped yet (normal on first install). "
                    "Identity docs will load on next gateway start."
                )
        except Exception as _e:
            result.log(f"[warn] Gateway restart skipped: {_e}")

    return result


# Keep backward-compat alias so existing callers don't break immediately
install_primary_launchd = install_evolve_infra_jobs


# ── Opik companion install (v1.5-1) ───────────────────────────────────────────


OPIK_INSTALL_DIR = Path("/Users/evolve/.evolve/opik")
OPIK_LAUNCHD_LABEL = "ai.evolve.opik"
OPIK_DEPLOYMENT_REPO = "https://github.com/comet-ml/opik.git"


def _opik_plist_spec(label: str, docker_bin: str, compose_dir: Path) -> JobSpec:
    """Build the Opik docker-compose LaunchDaemon JobSpec (pure — no disk access)."""
    return JobSpec(
        label=label,
        program_args=[docker_bin, "compose", "up"],
        working_dir=str(compose_dir),
        user="evolve",
        keep_alive=True,
        run_at_load=True,
        stdout_path="/Users/evolve/.openclaw/logs/opik.log",
        stderr_path="/Users/Shared/evolve/logs/opik.err.log",
    )


def _opik_plist_content(label: str, docker_bin: str, compose_dir: Path) -> str:
    """Render the Opik docker-compose LaunchDaemon plist (pure — no disk access)."""
    return render_launchd_plist(_opik_plist_spec(label, docker_bin, compose_dir))


def install_opik_companion(
    evolve_dir: Path,
    dry_run: bool = False,
) -> DeployResult:
    """Install the self-hosted Opik observability stack as a launchd companion.

    Opik (Apache-2.0; https://github.com/comet-ml/opik) ships an
    ``opik.sh`` start script that runs the full server stack via
    docker-compose. We:

      1. Verify Docker is available (preflight; bail with a clear error
         if not).
      2. Clone the Opik repo into ``/Users/evolve/.evolve/opik/`` if
         not already present. Future runs ``git pull --ff-only`` to
         track upstream (matches the repo-puller pattern used for
         evolve itself).
      3. Install a launchd plist that runs ``opik.sh`` at boot under
         the evolve user. The script keeps the docker-compose stack
         running; launchd KeepAlive restarts if it exits.
      4. Write an evolve-side config marker so ``get_client()`` knows
         to default to the Opik backend.

    Returns a ``DeployResult`` so the caller can surface step-by-step
    progress. Best-effort throughout — a failed install leaves Evolve
    using the JSONL backend (the documented fallback per
    project_external_dependency_vetting).

    Notes:
      * Docker itself is NOT installed by this helper — the operator
        needs to have Docker Desktop or Colima already set up. We
        check ``docker info`` as the readiness signal.
      * The Opik server listens on 127.0.0.1:5173 by default
        (frontend) plus its REST API on 127.0.0.1:5174. Both are
        bound to loopback only — no external exposure.
    """
    result = DeployResult(bot_id="opik", success=True)

    if dry_run:
        result.log("[dry-run] Would clone Opik to /Users/evolve/.evolve/opik")
        result.log("[dry-run] Would install launchd job ai.evolve.opik")
        result.log("[dry-run] Would mark observability.backend=opik in network.json")
        return result

    # 1. Preflight — Docker present?
    try:
        r = subprocess.run(
            ["/usr/bin/which", "docker"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0 or not r.stdout.strip():
            result.error(
                "Docker not found on PATH. Install Docker Desktop or Colima, "
                "then re-run: sudo evolve-admin install-infra-jobs --with-opik"
            )
            return result
        docker_bin = r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        result.error(f"Could not verify Docker presence: {e}")
        return result

    try:
        info = subprocess.run(
            [docker_bin, "info"],
            capture_output=True, timeout=10,
        )
        if info.returncode != 0:
            result.error(
                "Docker is installed but not responding (start Docker "
                "Desktop / Colima first)."
            )
            return result
    except (subprocess.TimeoutExpired, OSError) as e:
        result.error(f"Docker info check failed: {e}")
        return result

    result.log(f"Docker verified at {docker_bin}")

    # 2. Clone or update the Opik repo as the evolve user.
    OPIK_INSTALL_DIR.parent.mkdir(parents=True, exist_ok=True)
    if OPIK_INSTALL_DIR.exists() and (OPIK_INSTALL_DIR / ".git").exists():
        result.log("Opik checkout exists; pulling latest")
        pull = subprocess.run(  # sudo-grant: root-only — install_opik runs under `sudo evolve-admin` (operator root), dropping TO evolve
            ["sudo", "-u", "evolve", "git", "-C", str(OPIK_INSTALL_DIR),
             "pull", "--ff-only"],
            capture_output=True, text=True, timeout=120,
        )
        if pull.returncode != 0:
            result.log(f"[warn] Opik pull non-zero: {pull.stderr.strip()[:200]}")
    else:
        result.log(f"Cloning Opik into {OPIK_INSTALL_DIR}")
        # Ensure parent dir exists and is owned by evolve.
        _run_sudo(["mkdir", "-p", str(OPIK_INSTALL_DIR.parent)], result)
        _run_sudo(["chown", "evolve:staff", str(OPIK_INSTALL_DIR.parent)], result)
        clone = subprocess.run(  # sudo-grant: root-only — same operator-root CLI context as the pull above
            ["sudo", "-u", "evolve", "git", "clone",
             "--depth", "1",
             OPIK_DEPLOYMENT_REPO, str(OPIK_INSTALL_DIR)],
            capture_output=True, text=True, timeout=180,
        )
        if clone.returncode != 0:
            result.error(
                f"Opik git clone failed: {clone.stderr.strip()[:300]}"
            )
            return result

    # 3. Install the launchd plist. Use docker-compose directly rather
    #    than the opik.sh wrapper — launchd needs a long-running
    #    foreground process, and docker-compose up (no -d) gives us
    #    that. KeepAlive bounces the stack on crash.
    compose_dir = OPIK_INSTALL_DIR / "deployment" / "docker-compose"
    if not compose_dir.exists():
        # Older Opik layouts use a different path; bail gracefully so
        # the operator can inspect.
        result.error(
            f"Opik docker-compose path not found at {compose_dir}. "
            f"Inspect {OPIK_INSTALL_DIR} and run the install manually."
        )
        return result

    plist_label = OPIK_LAUNCHD_LABEL
    plist_dst = LAUNCHD_DIR / f"{plist_label}.plist"
    # Seam install owns staging + cp/chown/chmod + bootout/bootstrap. The
    # byte-identical skip is safe here: the daemon's job is keeping the
    # docker-compose stack up; nothing depends on a bounce when the plist
    # hasn't changed.
    install_res = get_scheduler().install(
        _opik_plist_spec(plist_label, docker_bin, compose_dir)
    )
    if install_res.ok:
        if install_res.skipped:
            result.log(f"Up-to-date launchd: {plist_label} (skipped reinstall)")
        else:
            result.log(f"Installed launchd: {plist_label}")
    elif "bootstrap failed" in install_res.message:
        result.error(
            f"Plist written but bootstrap failed ({install_res.message}). "
            f"Run 'sudo launchctl bootstrap system {plist_dst}' manually after fixing the cause."
        )
    else:
        result.error(f"Cannot install {plist_label}: {install_res.message}")

    # 4. Mark the operator's network config so ``get_client()`` picks
    #    the Opik backend. Done as a separate step so operators on a
    #    Windows/Linux dev box without launchd can still flip the
    #    backend manually via network.json.
    result.log(
        "Opik companion installed. To activate the Opik observability backend "
        "in Evolve, edit network.json and set:\n"
        '  "observability": {"backend": "opik", '
        '"opik": {"host": "http://localhost:5173", "project_name": "evolve"}}'
    )
    return result


# ── Internal helpers ──────────────────────────────────────────────────────────

def _sudo_mkdir(path: Path, result: DeployResult) -> None:
    _run_sudo(["mkdir", "-p", str(path)], result)


def _sudo_copy(src: Path, dst: Path, result: DeployResult) -> None:
    _run_sudo(["cp", str(src), str(dst)], result)


def _sudo_chown(path: Path, owner: str, result: DeployResult) -> None:
    # Try direct chown first (works when we already own the path).
    # Fall back to sudo for cross-user ownership changes.
    try:
        uid = pwd.getpwnam(owner).pw_uid
        gid = grp.getgrnam("staff").gr_gid
        for root, dirs, files in os.walk(str(path)):
            os.lchown(root, uid, gid)
            for f in files:
                os.lchown(os.path.join(root, f), uid, gid)
        result.log(f"chown -R {owner} {path}")
        return
    except (PermissionError, KeyError):
        pass
    _run_sudo(["chown", "-R", owner, str(path)], result)
    if result.success:
        result.log(f"chown -R {owner} {path}")


_SUDO_CMD_PATHS: dict[str, str] = {
    "cp": "/bin/cp",
    "chmod": "/bin/chmod",
    "chown": _PROFILE.chown,  # /usr/sbin/chown (macOS) vs /usr/bin/chown (Linux)
    "mkdir": "/bin/mkdir",
    "mv": "/bin/mv",
    "rm": "/bin/rm",
    "ln": "/bin/ln",
    "cat": "/bin/cat",
    # "launchctl" intentionally absent — launchctl goes through the
    # Scheduler seam (runtime.scheduler), never through _run_sudo (4.3C S2).
}


def _run_sudo(cmd: list[str], result: DeployResult, check: bool = True) -> subprocess.CompletedProcess | None:
    resolved = [_SUDO_CMD_PATHS.get(cmd[0], cmd[0])] + cmd[1:] if cmd else cmd
    try:
        proc = subprocess.run(
            ["sudo"] + resolved,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check and proc.returncode != 0:
            result.error(f"sudo {' '.join(cmd)} failed: {proc.stderr.strip()}")
        return proc
    except subprocess.TimeoutExpired:
        result.error(f"Timeout running: sudo {' '.join(cmd)}")
        return None
    except Exception as e:
        result.error(f"Error running sudo: {e}")
        return None


def _job_spec_for(label: str, user: str, script_path: Path, schedule: dict,
                  extra_args: list[str] | None = None, run_at_load: bool = False,
                  jitter_seconds: int = 0) -> JobSpec:
    """Build the JobSpec for a per-bot analyzer daemon.

    Single source of the schedule→job mapping so the launchd-XML render
    (:func:`_plist_content`) and the seam-routed installer
    (:func:`_install_launchd` with ``via_seam=True``) emit the SAME job —
    a byte-identical launchd plist on macOS, a systemd unit on Linux.

    jitter_seconds > 0 wraps the program in `bash -c "sleep $((RANDOM % N)); exec ..."`
    so per-bot daemons that share an interval don't all fire in lockstep. macOS
    launchd does not jitter StartInterval, and same-plist installs across N bots
    fire synchronously — this avoids the resulting process-spawn storm that has
    wedged the mini's userland (sshd hangs at banner exchange).
    """
    start_interval: int | None = None
    start_calendar: dict | list[dict] | None = None
    if "interval" in schedule:
        start_interval = int(schedule["interval"])
    elif "Weekday" in schedule:
        start_calendar = {
            "Weekday": int(schedule["Weekday"]),
            "Hour": int(schedule["Hour"]),
            "Minute": int(schedule["Minute"]),
        }
    elif isinstance(schedule.get("times"), list):
        start_calendar = [
            {"Hour": int(t["Hour"]), "Minute": int(t["Minute"])}
            for t in schedule["times"]
        ]
    elif "Minute" in schedule and "Hour" not in schedule and "Weekday" not in schedule:
        # Hourly: run every hour at the given minute (no Hour key = every hour)
        start_calendar = [{"Minute": int(schedule["Minute"])}]
    else:
        start_calendar = {
            "Hour": int(schedule["Hour"]),
            "Minute": int(schedule["Minute"]),
        }

    log_dir = str(_user_home(user) / ".openclaw/logs")
    job_name = label.replace("ai.openclaw.evolve.", "").replace("ai.evolve.", "").replace(f".{user}", "")
    spec = JobSpec(
        label=label,
        program_args=[str(VENV_PYTHON), str(script_path), *(extra_args or [])],
        user=user,
        # RunAtLoad always lands on disk explicitly (renderer guarantee) so
        # intent is unambiguous during incident response — see
        # pod-health-invariants.H7.
        run_at_load=run_at_load,
        start_interval=start_interval,
        start_calendar=start_calendar,
        stdout_path=f"{log_dir}/evolve-{job_name}.log",
        stderr_path=f"{log_dir}/evolve-{job_name}.err.log",
        env={
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "PYTHONPATH": str(ANALYZER_DIR),
            "EVOLVE_NETWORK": str(_CANONICAL_NETWORK_JSON),
        },
        jitter_seconds=jitter_seconds or 0,
    )
    return spec


def _plist_content(label: str, user: str, script_path: Path, schedule: dict,
                   extra_args: list[str] | None = None, run_at_load: bool = False,
                   jitter_seconds: int = 0) -> str:
    """Render the per-bot daemon JobSpec to launchd plist XML (macOS path)."""
    return render_launchd_plist(
        _job_spec_for(label, user, script_path, schedule, extra_args,
                      run_at_load, jitter_seconds)
    )


def _install_launchd(label: str, user: str, script_path: Path, schedule: dict,
                     result: DeployResult, extra_args: list[str] | None = None,
                     run_at_load: bool = False, jitter_seconds: int = 0,
                     via_seam: bool = True) -> None:
    # Preflight: verify the Python interpreter and the script both exist before
    # writing the plist. A missing venv or script causes silent launchd job failures.
    # Log as warnings (not errors) so a missing venv on first-run doesn't poison
    # result.success and block subsequent launchd installs in the same deploy_bot call.
    if not Path(VENV_PYTHON).exists():
        result.log(
            f"[warn] Skipping {label}: Python interpreter not found at {VENV_PYTHON}. "
            f"Run 'evolve-admin setup venv' to create the virtual environment, "
            f"then re-run: sudo evolve-admin deploy {user}"
        )
        return
    if not script_path.exists():
        result.log(
            f"[warn] Skipping {label}: script not found at {script_path}. "
            f"Ensure the repo is checked out at the expected location."
        )
        return

    if via_seam:
        # DEFAULT path: materialize through the Scheduler seam — a systemd unit
        # on Linux, a BYTE-IDENTICAL launchd plist on macOS (same _job_spec_for).
        # _install_job_ensuring_restart keeps the legacy cp-to-LAUNCHD_DIR
        # path's bounce guarantee for DAEMON-shaped specs; a timer/calendar
        # oneshot (every monitor installed through here) is installed without
        # a bounce, because a "restart" of one of those is a forced RUN.
        # That legacy path is retained
        # only as an explicit `via_seam=False` escape hatch (no caller passes it
        # today); it silently no-ops on a box with no /Library/LaunchDaemons,
        # which is why it cannot be the Linux default.
        spec = _job_spec_for(label, user, script_path, schedule, extra_args,
                             run_at_load, jitter_seconds)
        ok, msg = _install_job_ensuring_restart(spec)
        if ok:
            result.log(f"Installed launchd: {label}")
        else:
            result.log(
                f"[warn] {label} install via scheduler failed: {msg} "
                "— will activate on next reboot"
            )
        return

    plist_name = f"{label}.plist"
    plist_dst = LAUNCHD_DIR / plist_name
    content = _plist_content(label, user, script_path, schedule, extra_args, run_at_load, jitter_seconds)

    # Write to /tmp (sudoers requires /tmp/*.plist), then sudo-move into place.
    # Use the cp's own returncode to gate the bootstrap sequence — do NOT check
    # result.success here, as a pre-existing failure from an earlier deploy step
    # would otherwise prevent the plist from being registered with launchd.
    fd, tmp_path = tempfile.mkstemp(dir="/tmp", suffix=".plist")
    with os.fdopen(fd, "w") as f:
        f.write(content)

    cp_proc = _run_sudo(["cp", tmp_path, str(plist_dst)], result, check=False)
    os.unlink(tmp_path)
    if cp_proc is None or cp_proc.returncode != 0:
        result.error(f"Cannot install {label}: plist write to {plist_dst} failed")
        return
    _run_sudo(["chown", f"root:{_PROFILE.admin_group}", str(plist_dst)], result)
    _run_sudo(["chmod", "644", str(plist_dst)], result)

    # Unload if already loaded (ignore error), then load. Always re-registers
    # — intentionally no byte-identical skip on this legacy escape-hatch path,
    # matching the pre-seam per-bot deploy behavior (raw seam verbs keep those
    # semantics exact).
    _scheduler_launchctl("bootout", f"system/{label}")
    _wait_for_launchd_unload(label)
    boot_rc, _boot_out = _scheduler_launchctl("bootstrap", "system", str(plist_dst))
    if boot_rc == 0:
        result.log(f"Installed launchd: {label}")
    else:
        result.log(f"[warn] Plist written for {label} but launchctl bootstrap failed — will activate on next reboot")


def _uninstall_launchd(label: str, result: DeployResult) -> None:
    """Bootout + remove a launchd plist. Idempotent.

    Used by feature-toggle paths to turn a watcher OFF without leaving the
    daemon running. Logs but doesn't fail when the plist isn't present or
    the bootout reports "not loaded" — those are the expected steady-states
    on a feature that's already off.
    """
    plist_path = LAUNCHD_DIR / f"{label}.plist"
    # Bootout first (whether or not the plist is on disk — the unit may be
    # loaded from a previous version), ignore failures.
    _scheduler_launchctl("bootout", f"system/{label}")
    _wait_for_launchd_unload(label)
    # Then remove the plist file if present.
    if plist_path.exists():
        rm_proc = _run_sudo(["rm", str(plist_path)], result, check=False)
        if rm_proc is not None and rm_proc.returncode == 0:
            result.log(f"Uninstalled launchd: {label}")
        else:
            result.log(f"[warn] launchd {label} bootout ran but plist remove failed")
    else:
        result.log(f"launchd {label}: not installed (nothing to remove)")


def _bootout_retired_per_bot_jobs(bot_id: str, result: DeployResult) -> None:
    """Boot out the per-bot jobs Evolve no longer installs. Idempotent.

    Retired labels swept here:

    * ``ai.openclaw.evolve.test.<bot>`` — killed 2026-06-08 with the app-test
      surface (docs/decision-app-tests-2026-06-08.md).
    * ``ai.openclaw.evolve.apply.<bot>`` — killed 2026-08-18. The legacy
      per-bot apply watcher (``packages/analyzer/apply.py``) polled
      ``{shared_dir}/proposals/approved/``, a directory no arbiter status
      maps to (``arbiter/store.py::_STATUS_TO_SUBDIR``), so it was
      structurally unable to see a modern proposal and had applied nothing
      in its entire logged history. See
      docs/design-proposal-signing-key-2026-08-18.md.

    **Ordering — and why this is no longer the only trigger.** A retirement
    reaches a pod in two separate events: ``git rm`` of the retired script
    arrives when the ``repo-puller`` daemon pulls (at that instant the units
    still exist and name a file that is gone), and the units come off on the
    next ``deploy_bot``.

    This function used to be the ONLY thing that ran the second event, and an
    earlier draft of this docstring claimed the puller's lagging-bot redeploy
    sweep (``repo_puller._run_lagging_bot_redeploy_sweep``) would trigger it
    within "one to two puller ticks (~15-30 min) … benign and self-clearing".
    **That guarantee was false and was disproven in production on 2026-08-18**
    (#3705, both pods). The lagging sweep is skipped on every HEAD-ADVANCING
    tick by ``_loaded_deploy_code_is_current`` — correctly, since the tick
    process imported the pre-pull ``deploy`` module and its stamps would name
    a superseded commit — so it converges only on a NO-OP tick. When merges
    land inside every 15-minute window there is no no-op tick. #3705 arrived
    in a run of six consecutive advancing ticks; the sweep never ran, and 9
    launchd plists plus 2 systemd units kept firing against a deleted file
    until an operator ran ``deploy --all`` by hand.

    So teardown now has a second trigger that shares none of that machinery:
    ``retired_jobs.sweep_pod``, which the puller runs pod-wide on EVERY tick,
    advance or no-op. See that module's docstring for why its exposure bound
    (one tick) actually holds. This per-bot sweep stays because it calls
    ``remove()`` unconditionally rather than gating on the artifact being on
    disk, so it also clears a unit still registered after its file was removed
    by hand.

    The stale-code guard remains load-bearing for a second reason: it is what
    stops the PRE-pull deploy module, which still contains the installer for
    the job being retired, from re-installing the units it is about to lose.

    **Platform:** goes through the scheduler seam (``get_scheduler().remove``)
    rather than a hardcoded ``/Library/LaunchDaemons`` ``rm``, so the VPS's
    systemd units are removed too. Its predecessor
    (``_bootout_legacy_test_plists``) early-returned on Linux — correct for
    the app-test daemon, which never existed there, but it would have
    stranded the apply units, which do exist there. The seam's ``remove()``
    is idempotent on both OSes: removing a label that was never installed
    succeeds.
    """
    from .retired_jobs import retired_labels_for

    sched = get_scheduler()
    for label in retired_labels_for(bot_id):
        try:
            # ``installed`` is plist/unit-file existence on both adapters.
            # Probed BEFORE remove() so the log line reflects an actual
            # teardown — remove() reports success for the already-gone case
            # too, and sniffing its message string would couple this to
            # per-adapter wording ("no plist on disk" vs "no unit files on
            # disk"). The steady state on every pod after the first sweep is
            # "nothing installed"; a deploy must not claim a removal then.
            installed = bool(sched.status(label).get("installed"))
            ok, msg = sched.remove(label)
        except Exception as exc:   # never let a teardown abort a deploy
            result.log(f"[warn] could not remove retired job {label}: {exc}")
            continue
        if not ok:
            result.log(f"[warn] could not remove retired job {label}: {msg}")
        elif installed:
            result.log(f"removed retired per-bot job {label}")


def _install_launchd_audit_runner(
    bot_id: str, evolve_dir: Path, result: DeployResult,
    *, user: str | None = None,
) -> None:
    """Install the per-bot app-audit runner (Tier 2 every 6 hours).

    Runs as the bot's own user. The runner reads manifests from the bot's
    workspace, runs structural assertions, writes findings to
    ``workspace/evolve/audit_outbox/``. An admin-side poller in
    ``evolve_admin.applications.audit_poller`` drains the outbox into the
    pod-wide Signal store on the existing scheduler tick.

    Runs every 6 hours (StartInterval=21600). RunAtLoad fires once on
    install so the operator gets immediate Tier-2 coverage rather than
    waiting up to 6h for the first run. Jitter spreads simultaneous bot
    firings so we don't get N bots all auditing at the same wall-clock
    minute.

    See ``packages/analyzer/app_audit_runner.py`` for the runner.
    See ``docs/spec-app-audit-2026-05-16.md`` §7 for the trigger model.
    """
    plist_user = user or bot_id
    _install_launchd(
        label=f"ai.openclaw.evolve.audit-runner.{bot_id}",
        user=plist_user,
        script_path=ANALYZER_DIR / "app_audit_runner.py",
        schedule={"interval": 21600},   # 6 hours
        result=result,
        extra_args=[
            "--bot-id", bot_id,
            "--shared-dir", str(_CANONICAL_SHARED_DIR),
            "--tier", "2",
        ],
        run_at_load=True,
        # Spread per-bot firings across the 6-hour window so N bots don't
        # all run their audits at the same wall-clock moment after a pod-
        # wide deploy. 3600s of jitter is plenty given the work is cheap.
        jitter_seconds=3600,
    )


def _install_launchd_doctor_pass(
    bot_id: str, result: DeployResult,
    *, user: str | None = None,
) -> None:
    """Install the per-bot nightly `openclaw doctor --fix` job.

    Runs as the bot's own user. Previously doctor was invoked inline by
    ``install_oc_plugin``; that path hit timeouts on most of the pod
    during the 2026-05-29/30 ``deploy --all`` runs (60s, then 120s,
    both hit). The hang only manifested in deploy.py's subprocess
    wrapper — manual replays under the same user/cwd/flags consistently
    completed in 12-15s. Rather than keep chasing the discrepancy,
    doctor moved to this nightly cadence. The one deploy-critical
    piece of doctor's work (clearing a stale plugin install when the
    manifest schema changed) is still handled by
    ``_clear_stale_plugin_install`` in the inline path.

    Cadence: once a day at 03:17 + per-bot jitter (up to 30 min). The
    odd minute avoids colliding with the hour/half-hour cron grid
    everything else lives on. ``RunAtLoad`` is False — fresh deploys
    shouldn't kick off a 1-5 min doctor pass synchronously; the next
    overnight tick will pick it up. Operators who need it sooner run
    ``sudo evolve-admin doctor-pass --bot <id>`` (see CLI).
    """
    plist_user = user or bot_id
    _install_launchd(
        label=f"ai.openclaw.evolve.doctor-pass.{bot_id}",
        user=plist_user,
        script_path=ANALYZER_DIR / "doctor_pass_runner.py",
        # Daily 03:17 + jitter. _plist_content honors {"Hour": H, "Minute": M}.
        schedule={"Hour": 3, "Minute": 17},
        result=result,
        extra_args=["--bot-id", bot_id],
        run_at_load=False,
        jitter_seconds=1800,
    )


def _install_launchd_audit_runner_tier3(
    bot_id: str, evolve_dir: Path, result: DeployResult,
    *, user: str | None = None,
) -> None:
    """Install the per-bot Tier-3 audit scan (hourly cadence check).

    Distinct from the Tier-2 daemon: this one wakes hourly, checks
    cadence + due-state, and dispatches LLM-driven audits for apps
    that are due. Most ticks are no-ops (cadence not due yet).

    Hourly wake-up is the minimum granularity needed to honor the
    ``daily`` cadence + handle on-demand inbox pickups within an hour
    of the request. Lower-frequency cadences (weekly / monthly) also
    work fine — the runner does the actual gating.
    """
    plist_user = user or bot_id
    _install_launchd(
        label=f"ai.openclaw.evolve.audit-runner-t3.{bot_id}",
        user=plist_user,
        script_path=ANALYZER_DIR / "app_audit_runner.py",
        schedule={"interval": 3600},   # hourly
        result=result,
        extra_args=[
            "--bot-id", bot_id,
            "--shared-dir", str(_CANONICAL_SHARED_DIR),
            "--tier", "3",
        ],
        run_at_load=False,   # Don't fire on install — wait for first hour tick
        # Spread firings across the hour so a pod-wide deploy doesn't
        # blast every bot at the same wall-clock minute.
        jitter_seconds=1800,
    )


def _install_launchd_cost_converter(
    bot_id: str, result: DeployResult, *, user: str | None = None,
) -> None:
    """Install the per-bot cost_event converter (every 15 minutes).

    Reads the bot's own ``~/.openclaw/workspace/memory/turns-<date>.jsonl``
    and writes cost_event records to the shared annotations dir. Runs as
    the bot user so it can read its own workspace without ACLs and write
    output owned by the bot user (matching the historical plugin write).

    See ``packages/analyzer/cost_event_converter.py`` for the why; the
    short version is OC 2026.4.29 stopped firing the ``llm_output`` hook
    on embedded turns, so cost_event records have to come from a
    converter over OC's on-disk turn record instead of from a hook.
    """
    plist_user = user or bot_id
    _install_launchd(
        label=f"ai.openclaw.evolve.cost-converter.{bot_id}",
        user=plist_user,
        script_path=ANALYZER_DIR / "cost_event_converter.py",
        schedule={"interval": 900},   # every 15 minutes
        result=result,
        extra_args=["--bot-id", bot_id, "--shared-dir", str(_CANONICAL_SHARED_DIR)],
        run_at_load=True,
        # Spread firings across 15 min so all bots don't hit shared dir
        # simultaneously when the plists are bootstrapped at deploy time.
        jitter_seconds=900,
    )


def _install_launchd_analyze(bot_id: str, evolve_dir: Path, result: DeployResult) -> None:
    _install_launchd(
        label=f"ai.openclaw.evolve.analyze.{bot_id}",
        user=bot_id,
        script_path=ANALYZER_DIR / "analyze.py",
        schedule={"Weekday": 0, "Hour": 2, "Minute": 0},
        result=result,
    )


def _install_launchd_outcome(bot_id: str, evolve_dir: Path, result: DeployResult) -> None:
    _install_launchd(
        label=f"ai.openclaw.evolve.outcome.{bot_id}",
        user=bot_id,
        script_path=ANALYZER_DIR / "outcome.py",
        schedule={"Hour": 9, "Minute": 0},  # daily 09:00
        result=result,
    )


def _install_launchd_tuples(_bot_id: str, result: DeployResult) -> None:
    """Install pod-wide L3 tuple-extraction job (daily, evolve user).

    The script iterates every bot in network.json, reads recent
    session_summary records, and extracts (noun × verb × mood ×
    engagement) tuples to ``{shared_dir}/observations/{bot}/``. Runs as
    evolve so it can read the Anthropic API key from auth-profiles.json
    and write into the shared dir without per-bot ACL fiddling.

    Daily 01:30 — between cost-converter (every 15 min) and analyze
    (Sunday 02:00). Generators in better_engine_refresh consume these
    tuples via observations.access.window().clusters() / .tuples().
    """
    _install_launchd(
        label="ai.openclaw.evolve.tuples",
        user="evolve",
        script_path=ANALYZER_DIR / "extract_tuples.py",
        schedule={"Hour": 1, "Minute": 30},
        result=result,
    )


def _chown_legacy_evolve_log(log_basename: str, result: DeployResult) -> None:
    """Chown a stale launchd log under ``/Users/evolve/.openclaw/logs/``
    to ``evolve:staff`` if it's currently root-owned.

    Migration path: until this fix, three pod-wide daemons
    (defer-runner, manifest-reflex-runner, app-posture-review) shipped
    via raw-template plists that omitted the ``UserName`` key, so
    launchd booted them as root. Their existing log files at the path
    the now-evolve-owned daemon writes to are root-owned mode 644 —
    the evolve user can't append. Without this fixup, launchd silently
    drops every stdout write from the new daemon on every pod that
    ran a pre-fix install.

    Idempotent: a no-op when the file is missing or already owned by
    evolve. Errors are non-fatal; the daemon still installs.
    """
    log_path = _user_home("evolve") / ".openclaw/logs" / log_basename
    try:
        st = log_path.stat()
    except (FileNotFoundError, PermissionError):
        return
    try:
        evolve_uid = pwd.getpwnam("evolve").pw_uid
    except KeyError:
        return
    if st.st_uid == evolve_uid:
        return
    _run_sudo(["chown", "evolve:staff", str(log_path)], result, check=False)


def _install_launchd_defer_runner(_bot_id: str, _evolve_dir: Path, result: DeployResult) -> None:
    """Install Continuity Engine v2 defer runner: every 2 minutes (StartInterval=120).

    Pod-wide job — there is no per-bot variant. The runner walks every
    bot's defer-queue.jsonl on each cycle. Runs as the ``evolve`` user
    so it can read each bot's ``.openclaw/`` queue via the evolve
    read-ACL (set by ``set_evolve_read_acl``) and write the post-fire
    archive into the shared dir without per-bot ACL fiddling.

    Faster cadence than task-runner because a 20-minute defer should
    feel ±2 min, not ±15. No jitter wrapper at this cadence: jitter
    would double effective latency without buying anything (no other
    2-min daemons to desync from).
    """
    _chown_legacy_evolve_log("evolve-defer-runner.log", result)
    _install_launchd(
        label="ai.openclaw.evolve.defer-runner",
        user="evolve",
        script_path=ANALYZER_DIR / "defer_runner.py",
        schedule={"interval": 120},
        result=result,
    )


def _install_launchd_manifest_reflex_runner(_bot_id: str, _evolve_dir: Path, result: DeployResult) -> None:
    """Install Manifest Reflex runner: every 60 seconds (StartInterval=60).

    Pod-wide job — there's no per-bot variant. The runner walks every
    bot's manifest-reflex-queue.jsonl on each cycle and lands rows as
    ApplicationManifests. Runs as the ``evolve`` user (read ACL on each
    bot's ``.openclaw/``; write ACL on ``workspace/evolve/``).

    Cadence is faster than defer (2 min) because the bot's reply often
    hands the user back something tied to a manifest (e.g. "I built
    protein-tracker — see the Applications tab"); a 60s lag keeps that
    surface honest.
    """
    _chown_legacy_evolve_log("evolve-manifest-reflex-runner.log", result)
    _install_launchd(
        label="ai.openclaw.evolve.manifest-reflex-runner",
        user="evolve",
        script_path=ANALYZER_DIR / "manifest_reflex_runner.py",
        schedule={"interval": 60},
        result=result,
    )


def _install_launchd_app_posture_review(_bot_id: str, _evolve_dir: Path, result: DeployResult) -> None:
    """Install App Posture review: weekly Sunday 04:30.

    Pod-wide job — walks every bot in network.json, gathers per-bot
    manifests/signals/orphans for the prior 7 days, and writes a
    structured markdown snapshot to {shared_dir}/app_posture/<bot>.md
    that session_surface.py injects into the bot's systemAppend. Runs
    as the ``evolve`` user so the per-bot reads + shared-dir writes
    go through the existing ACL grants.

    Sunday 04:30 deliberately runs AFTER ai.openclaw.evolve.expansion
    (Sunday 04:00) — expansion may file proposals that affect manifest
    state, so posture should observe the post-expansion state.
    """
    _chown_legacy_evolve_log("evolve-app-posture-review.log", result)
    _install_launchd(
        label="ai.openclaw.evolve.app-posture-review",
        user="evolve",
        script_path=ANALYZER_DIR / "app_posture_review.py",
        schedule={"Weekday": 0, "Hour": 4, "Minute": 30},
        result=result,
    )


def _install_launchd_slack_signals(bot_id: str, evolve_dir: Path, result: DeployResult) -> None:
    """Install Slack signal ingestion: daily at 03:00.

    slack_signals.py takes a REQUIRED ``--bot`` — pass the RESOLVED primary bot
    (never the literal "evolve": on an evo-primary pod a "--bot evolve" unit dies
    every fire). If no primary resolves, skip rather than install a dead unit.
    """
    from primary_bot import primary_bot_id
    try:
        _primary = primary_bot_id(json.loads(Path(_CANONICAL_NETWORK_JSON).read_text()))
    except Exception:
        _primary = None
    if not _primary:
        result.log("slack-signals: no primary bot resolved; skipping install")
        return
    _install_launchd(
        label=f"ai.openclaw.evolve.slack-signals.{bot_id}",
        user=bot_id,
        script_path=ANALYZER_DIR / "slack_signals.py",
        schedule={"Hour": 3, "Minute": 0},
        result=result,
        extra_args=["--bot", _primary, "--network", str(_CANONICAL_NETWORK_JSON)],
    )


def _install_launchd_expansion(bot_id: str, evolve_dir: Path, result: DeployResult) -> None:
    """Install proactive expansion engine: first Sunday of month at 04:00."""
    # launchd has no 'first Sunday of month'; fire every Sunday (Weekday=0,
    # Hour=4) and let expansion.py self-gate on day-of-month (run if day <= 7).
    _install_launchd(
        label=f"ai.openclaw.evolve.expansion.{bot_id}",
        user=bot_id,
        script_path=ANALYZER_DIR / "expansion.py",
        schedule={"Weekday": 0, "Hour": 4, "Minute": 0},
        result=result,
        extra_args=["--bot", bot_id],  # required: expansion.py argparse marks --bot
    )


def _install_launchd_spend_alert(bot_id: str, evolve_dir: Path, result: DeployResult) -> None:
    """Install intraday spend + burst check.

    Runs every 5 minutes (StartInterval=300). The burst detector reads
    live turn JSONL and fires within one polling cycle once a bot
    crosses the burst threshold in the rolling window — far better than
    the previous hourly cadence, which (combined with the wrong file
    path bug) missed the 2026-05-20 spike for 24 consecutive ticks.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.spend-alert",
        user=bot_id,
        script_path=ANALYZER_DIR / "spend_alert.py",
        schedule={"interval": 300},  # StartInterval — every 5 minutes
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        # Modest jitter so multiple bots don't all fire at the same
        # 5-min boundary and pile onto the dispatcher.
        jitter_seconds=60,
    )


def _install_launchd_cron_alert(bot_id: str, evolve_dir: Path, result: DeployResult) -> None:
    """Install hourly cron alert check (every hour at :15)."""
    _install_launchd(
        label=f"ai.evolve.{bot_id}.cron-alert",
        user=bot_id,
        script_path=ANALYZER_DIR / "cron_alert.py",
        schedule={"Minute": 15},
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
    )


def _install_launchd_weekly_review(bot_id: str, result: DeployResult) -> None:
    """Install the weekly review job into /Library/LaunchDaemons and bootstrap it.

    Sunday 03:00 — runs after analyze.py (02:00).
    No RunAtLoad — first firing is the following Sunday.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.weekly-review",
        user=bot_id,
        script_path=ANALYZER_DIR / "weekly_review.py",
        schedule={"Weekday": 0, "Hour": 3, "Minute": 0},
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
    )


def _install_launchd_weekly_bot_trends(bot_id: str, result: DeployResult) -> None:
    """Install the weekly per-bot 7-day trend digest into /Library/LaunchDaemons.

    Sunday 03:30 — after weekly_review (03:00) which itself runs after
    analyze.py (02:00). Sibling cadence to weekly_review; reads the same
    tile_metrics chip set the daily report reads but filters to
    ``horizon: "7d"`` chips that the daily intentionally skips.

    No RunAtLoad — first firing is the following Sunday.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.weekly-bot-trends",
        user=bot_id,
        script_path=ANALYZER_DIR / "weekly_bot_trends.py",
        schedule={"Weekday": 0, "Hour": 3, "Minute": 30},
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
    )


def _install_launchd_usage_jobs(bot_id: str, result: DeployResult) -> None:
    """Install the two daily app-usage sweeps; each covers every bot per run.
    03:30 usage-logger — manifest-mtime footprint (usage-stats.json):
    structural inference, the FALLBACK signal since AL-1.3. 03:35
    usage-by-app — per-app rollup over turn annotations, split by
    attribution grade (usage-by-app.json): the PRIMARY signal.
    """
    for label, script, minute in (
        ("usage-logger", "usage_logger.py", 30), ("usage-by-app", "usage_by_app.py", 35),
    ):
        _install_launchd(
            label=f"ai.evolve.{bot_id}.{label}", user=bot_id, result=result,
            script_path=ANALYZER_DIR / script, schedule={"Hour": 3, "Minute": minute},
            extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        )


def _install_launchd_backup(
    bot_id: str,
    evolve_dir: Path,
    result: DeployResult,
    *,
    user: str | None = None,
) -> None:
    """Install nightly git backup daemon for one bot (02:00 daily).

    Runs as the bot's macOS user (defaults to bot_id; pass ``user=``
    for the bot_id ≠ account case, e.g. team_bot_b runs on personal_bot_user). The
    daemon invokes ``backup.py --bot <bot_id>`` so it only processes
    its own bot — no cross-bot iteration, no ACL gymnastics.

    Each bot's daemon is independent: team_bot_a's backup failing doesn't
    block personal_bot's. Replaces the pre-2026-05-25 central-daemon-as-evolve
    model where one process iterated every bot and needed write ACL
    on every workspace.
    """
    daemon_user = user or bot_id
    _install_launchd(
        label=f"ai.evolve.{bot_id}.backup",
        user=daemon_user,
        script_path=ANALYZER_DIR / "backup.py",
        schedule={"Hour": 2, "Minute": 0},
        result=result,
        extra_args=[
            "--bot", bot_id,
            "--network", str(_CANONICAL_NETWORK_JSON),
        ],
    )


def _install_launchd_pod_report_daily(bot_id: str, result: DeployResult) -> None:
    """Install pod operational report — fires every hour at :00, self-gates.

    The plist fires every hour and pod_report.should_run() checks whether
    `report_hour` from network.json matches the current hour. This means the
    user can change the report time in the UI and it takes effect at the
    next hour boundary without any plist regeneration.

    Cleans up the obsolete `pod-report-morning` and `pod-report-evening`
    plists from the v1 split-schedule design — those labels are no longer
    in use as of the v2 single-time schedule.
    """
    # Cleanup: bootout + remove the v1 split-schedule plists if they exist.
    # Idempotent — safe on hosts that never had them.
    for legacy in ("pod-report-morning", "pod-report-evening"):
        legacy_label = f"ai.evolve.{bot_id}.{legacy}"
        legacy_plist = LAUNCHD_DIR / f"{legacy_label}.plist"
        _scheduler_launchctl("bootout", f"system/{legacy_label}")  # rc ignored
        if legacy_plist.exists():
            _run_sudo(["rm", str(legacy_plist)], result, check=False)

    # Cleanup: remove the legacy shared_dir/thresholds.json file. v2 stores
    # thresholds under network.json → pod_report.thresholds; the old file is
    # never read or written. Idempotent.
    legacy_thresholds = _CANONICAL_SHARED_DIR / "thresholds.json"
    if legacy_thresholds.exists():
        _run_sudo(["rm", str(legacy_thresholds)], result, check=False)
    legacy_per_bot = _CANONICAL_SHARED_DIR / "thresholds"
    if legacy_per_bot.is_dir():
        _run_sudo(["rm", "-rf", str(legacy_per_bot)], result, check=False)

    _install_launchd(
        label=f"ai.evolve.{bot_id}.pod-report-daily",
        user=bot_id,
        script_path=ANALYZER_DIR / "pod_report.py",
        # Minute=0 with no Hour key fires at :00 every hour. The script
        # self-gates against report_hour from network.json.
        schedule={"Minute": 0},
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
    )


def _install_launchd_heal(bot_id: str, result: DeployResult) -> None:
    """Install gateway self-healing monitor (every 5 minutes, RunAtLoad, evolve user).

    heal.py checks all configured gateways, auto-restarts failed ones, and sends
    a Telegram alert only when a restart fails or repeated failures occur.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.heal",
        user=bot_id,
        script_path=ANALYZER_DIR / "heal.py",
        schedule={"interval": 300},   # StartInterval — every 5 minutes
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=60,   # desync from other 5-min daemons (verify) — H8
    )


def _install_launchd_pod_health(bot_id: str, result: DeployResult) -> None:
    """Install pod_health Signal-emitter (every 60s, evolve user).

    Runs ``_check_gateways`` only and writes/sweeps
    ``pod_health:pod_health_gateways:<bot>:gateway`` Signals so the alert
    notifier and Alerts UI see gateway transitions promptly. Other
    pod_health categories (launchd, file_security, repo_ownership) are
    NOT in this fast path — they run only on UI Refresh, wizard, or
    ``evolve-admin doctor``.

    Phase 0a of the alert-notifier spec
    (docs/spec-alert-notifier-2026-05-09.md). Gives the not-yet-built
    alert notifier a transition signal to fire on without forcing a
    comprehensive health scan every minute.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.pod-health",
        user=bot_id,
        script_path=ANALYZER_DIR / "pod_health_runner.py",
        schedule={"interval": 60},   # StartInterval — every minute
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=10,   # small desync to avoid lockstep with other 60s daemons
    )


def _install_launchd_signal_notifier(bot_id: str, result: DeployResult) -> None:
    """Install Signal-store transition notifier (every 60s, evolve user).

    Watches the Signal store for new ``firing`` Signals (with debounce)
    and ``firing → resolved`` transitions for previously-announced
    Signals, and pushes them to the operator's chat channel via the
    alert dispatcher. Default-off in v1 — opt in via the admin UI's
    ``alerts.signal_notifier.enabled`` toggle.

    Phase 4 of the alert-notifier spec
    (docs/spec-alert-notifier-2026-05-09.md). The daemon itself is
    cheap and idempotent — when the toggle is off, dispatcher.send
    short-circuits to ``SUPPRESSED_DISABLED`` and no chat messages
    fire, so leaving the daemon installed in the off state costs
    almost nothing.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.signal-notifier",
        user=bot_id,
        script_path=ANALYZER_DIR / "signal_notifier_runner.py",
        schedule={"interval": 60},   # StartInterval — every minute
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=20,   # offset from pod-health (10s jitter) so they
                             # don't run in lockstep — pod-health writes
                             # the firing Signal that signal-notifier reads
    )


def _better_engine_jobspec(
    label: str, daemon_user: str, daemon_home: str, script_path: Path,
) -> JobSpec:
    """Build the Better Engine refresh daemon JobSpec (pure — no disk access).

    Small jitter desyncs the 15-min StartInterval firing from other
    daemons that share the boundary — see pod-health-invariants.H8.
    Kept small (5s) so WatchPaths-triggered urgent runs stay near-immediate.
    """
    return JobSpec(
        label=label,
        program_args=[str(VENV_PYTHON), str(script_path)],
        user=daemon_user,
        jitter_seconds=5,
        env={
            "HOME": daemon_home,
            "EVOLVE_SHARED": str(_CANONICAL_SHARED_DIR),
        },
        start_interval=900,
        watch_paths=[str(_CANONICAL_SHARED_DIR / "better-engine/.refresh-urgent")],
        run_at_load=True,
        stdout_path=str(_CANONICAL_SHARED_DIR / "logs/better_engine.log"),
        stderr_path=str(_CANONICAL_SHARED_DIR / "logs/better_engine.err"),
    )


def _install_launchd_better_engine(bot_id: str, result: DeployResult) -> None:
    """Install Better Engine 15-minute refresh job with WatchPaths urgent trigger.

    Pod-wide infrastructure — runs as the evolve service user regardless of
    which bot's deploy triggered the install. Fires every 15 min, immediately
    on load (RunAtLoad=true), and whenever the urgent flag file appears
    (WatchPaths), which is written by health.py and spend_alert.py on critical
    events needing an immediate refresh (review.py was a third until #3641).

    Uses WatchPaths so it can't be expressed via the generic _plist_content
    helper — written as inline XML instead.
    """
    # Pinned to "evolve" — this daemon writes to metrics/, signals/, and
    # generators/ across every bot, and those dirs are owned by evolve:wheel.
    # Pre-2026-05-30 the bots all ran as the evolve user too, so resolving
    # UserName via _bot_user_for(bot_id) accidentally produced "evolve"; once
    # the primary bot got its own "evo" macOS user (evo-account-separation),
    # deploying evo rewrote this plist with UserName=evo and the daemon began
    # exiting 78 (EX_CONFIG) on every 15-min trigger — taking cost rollups,
    # the generator-runner cadence, and the compliance scan down with it.
    # Regression test: test_better_engine_plist_pinned_to_evolve_user.
    _daemon_user = "evolve"
    _daemon_home = str(_user_home("evolve"))
    label = "ai.openclaw.evolve.better"
    script_path = ANALYZER_DIR / "better_engine_refresh.py"

    if not Path(VENV_PYTHON).exists():
        result.error(
            f"Cannot install {label}: Python interpreter not found at {VENV_PYTHON}."
        )
        return
    if not script_path.exists():
        result.error(
            f"Cannot install {label}: script not found at {script_path}."
        )
        return

    # Ensure log dir and better-engine dir exist and are owned by the evolve user.
    # launchd fails with error 5 (I/O error) if StandardOutPath directory is missing.
    # Use -R on better-engine so any existing files inside it (e.g. created when
    # the server was run manually as another user) are also fixed.
    for _dir, _recursive in [
        (_CANONICAL_SHARED_DIR / "logs", False),
        (_CANONICAL_SHARED_DIR / "better-engine", True),
    ]:
        try:
            _run_sudo(["/bin/mkdir", "-p", str(_dir)], result, check=False)
            chown_cmd = [_PROFILE.chown]
            if _recursive:
                chown_cmd.append("-R")
            chown_cmd += [_daemon_user, str(_dir)]
            _run_sudo(chown_cmd, result, check=False)
            _run_sudo(["/bin/chmod", "755", str(_dir)], result, check=False)
        except Exception:
            pass

    _install_spec_via_seam(
        _better_engine_jobspec(label, _daemon_user, _daemon_home, script_path),
        result,
    )


def _install_launchd_audit(bot_id: str, result: DeployResult) -> None:
    """Install security audit job (every 15 minutes, evolve user)."""
    _install_launchd(
        label=f"ai.evolve.{bot_id}.audit",
        user=bot_id,
        script_path=ANALYZER_DIR / "audit.py",
        schedule={"interval": 900},   # StartInterval — every 15 minutes
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=120,   # desync from other 15-min daemons — H8
    )


def _maybe_install_launchd_upstream_issues_watcher(
    result: DeployResult, shared_dir: Path | None = None,
) -> None:
    """Conditionally install the upstream_issues_watcher launchd job.

    Gated by ``install_profile.is_feature_enabled("upstream_issues_watcher")``.
    On a standard household install this is a noop with a clear log line, so
    re-running ``install-infra-jobs`` on a stock install never deploys this
    monitor. When the feature is flipped off later, the operator should
    remove the plist via ``launchctl unload`` + delete — uninstall flow is
    handled by ``evolve-admin features set ... off`` (see cli.py).

    See docs/spec-upstream-issue-watcher-2026-05-22.md.
    """
    try:
        from install_profile import is_feature_enabled  # type: ignore
    except Exception as exc:  # noqa: BLE001
        result.log(f"[warn] upstream_issues_watcher: install_profile import failed; "
                   f"feature treated as off: {exc}")
        return

    install_json = (shared_dir or Path("/Users/Shared/evolve")) / "install.json"
    if not is_feature_enabled("upstream_issues_watcher", install_json):
        result.log("upstream-issues-watcher: feature off — skipped "
                   "(enable via 'evolve-admin features set upstream_issues_watcher on')")
        return

    _install_launchd(
        label="ai.evolve.evolve.upstream-issues-watcher",
        user="evolve",
        script_path=ANALYZER_DIR / "upstream_issues_watcher.py",
        # 15 min interval — faster than update_watcher's daily so maintainer
        # replies surface while still relevant. Per-feature config can override
        # at runtime via install.json::features.upstream_issues_watcher.poll_interval_minutes
        # but the launchd cadence is the cheap pre-filter.
        schedule={"interval": 15 * 60},
        result=result,
        extra_args=[],
        # First fire on install so the operator gets immediate validation that
        # auth works — better than waiting 15 min wondering whether it's broken.
        run_at_load=True,
        # Jitter so the script isn't lockstep with other 15-min intervals
        # (repo_puller in particular).
        jitter_seconds=120,
    )

    # Operator-facing post-install nudge: gh auth needs to be set up under
    # the evolve user before the script can talk to GitHub. We don't auto-
    # provision the token (silent-breakage risk — see Plex-test design
    # constraint); instead, print clear instructions exactly once.
    result.log(
        "upstream-issues-watcher: installed. One-time setup required:\n"
        "    sudo -u evolve gh auth login --hostname github.com --git-protocol https --web\n"
        "  Then add repos to /Users/Shared/evolve/install.json under\n"
        "    features.upstream_issues_watcher.repos = [{repo, author}, ...]"
    )


def _maybe_install_launchd_inbound_issues_watcher(
    result: DeployResult, shared_dir: Path | None = None,
) -> None:
    """Conditionally install the inbound_issues_watcher launchd job.

    Gated by ``install_profile.is_feature_enabled("inbound_issues_watcher")``.
    On standard installs this is a noop with a clear log line. The watcher
    polls the configured intake-target repos (under
    ``network.json::intake.github.targets``) for new issues filed by THIRD
    PARTIES, then writes each one as an inbound :class:`Intake` with an
    LLM triage verdict so it surfaces in the admin UI's Triage queue.

    Cadence: same 15-minute pre-filter as upstream_issues_watcher. The
    GitHub-side query window is bounded by per-repo last-polled timestamps
    persisted at ``{shared_dir}/inbound_issues_watcher/state.json``.

    See docs/spec-issue-inbox-2026-05-21.md (Phase 4).
    """
    try:
        from install_profile import is_feature_enabled  # type: ignore
    except Exception as exc:  # noqa: BLE001
        result.log(f"[warn] inbound_issues_watcher: install_profile import failed; "
                   f"feature treated as off: {exc}")
        return

    install_json = (shared_dir or Path("/Users/Shared/evolve")) / "install.json"
    if not is_feature_enabled("inbound_issues_watcher", install_json):
        result.log("inbound-issues-watcher: feature off — skipped "
                   "(enable via the Inbox tab toggle, or "
                   "'evolve-admin features set inbound_issues_watcher on')")
        return

    _install_launchd(
        label="ai.evolve.evolve.inbound-issues-watcher",
        user="evolve",
        script_path=ANALYZER_DIR / "inbound_issues_watcher.py",
        # 15-min interval matches the upstream watcher cadence — fresh
        # triage items should appear within one poll cycle. Per-feature
        # config in install.json::features.inbound_issues_watcher overrides.
        schedule={"interval": 15 * 60},
        result=result,
        extra_args=[],
        # Run-on-load so the operator gets immediate validation that auth
        # works AND the Triage queue populates without a 15-min wait.
        run_at_load=True,
        # Jitter so it isn't lockstep with upstream_issues_watcher /
        # repo_puller / other 15-min jobs.
        jitter_seconds=60,
    )

    # One-time setup nudge. Same shape as upstream watcher: gh auth + repos.
    # The repo list lives at network.json::intake.github.targets (managed
    # via the Tracked Repos UI from Phase 3) — no install.json mirror needed.
    result.log(
        "inbound-issues-watcher: installed. One-time setup required:\n"
        "    sudo -u evolve gh auth login --hostname github.com --git-protocol https --web\n"
        "  Then add the repos you maintain via the Inbox tab's 'Tracked repos'\n"
        "  card. Watcher only polls repos where you have maintainer/triage perms."
    )


INBOUND_ISSUES_WATCHER_LABEL = "ai.evolve.evolve.inbound-issues-watcher"


def install_inbound_issues_watcher_now(
    result: DeployResult, shared_dir: Path | None = None,
) -> None:
    """Force-install the inbound watcher launchd job, skipping the feature gate.

    Used by the feature-toggle path: the operator just flipped the gate on
    via the UI, so we install immediately (no need to wait for the next
    ``install-infra-jobs`` run). The feature-gate check is the caller's
    job — they've already written the install.json override.

    Same plist/schedule/jitter as the gated install path; the only difference
    is we don't re-check the gate.
    """
    _install_launchd(
        label=INBOUND_ISSUES_WATCHER_LABEL,
        user="evolve",
        script_path=ANALYZER_DIR / "inbound_issues_watcher.py",
        schedule={"interval": 15 * 60},
        result=result,
        extra_args=[],
        run_at_load=True,
        jitter_seconds=60,
    )


def uninstall_inbound_issues_watcher_now(result: DeployResult) -> None:
    """Bootout + remove the inbound watcher plist.

    Idempotent: safe to call when the watcher isn't installed.
    """
    _uninstall_launchd(INBOUND_ISSUES_WATCHER_LABEL, result)


def _install_launchd_update_watcher(bot_id: str, result: DeployResult) -> None:
    """Install update_watcher daily check (Phase E2).

    Polls npm for OpenClaw releases + origin/main for new commits on
    the deploy checkout; emits via the alerts dispatcher when updates
    are available. Operator-tunable via:
      - alerts.update_watcher.enabled / .cooldown_seconds (Config page)
      - updates.openclaw_available / updates.evolve_repo subscriptions
        (Alerts page → Subscriptions tab)

    Fires daily at 09:30 local — once a day is plenty for release-pace
    upstream sources, and afternoon-of-release notifications are fine.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.update-watcher",
        user=bot_id,
        script_path=ANALYZER_DIR / "update_watcher.py",
        schedule={"Hour": 9, "Minute": 30},   # daily 09:30 local
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=False,    # first fire is the next 09:30, not on install
        jitter_seconds=60,    # spread within a minute to avoid lockstep
    )


def _install_launchd_proposal_synthesizer(
    bot_id: str, result: DeployResult
) -> None:
    """Install proposal_synthesizer LLM run (every 6h, evolve user).

    Spec: docs/spec-proposal-synthesizer-2026-05-10.md §5, §7.

    Reads ``{shared_dir}/candidates/synthesizing/`` and runs one
    tool-using synthesis pass on Sonnet per fire. The substantiveness
    gate (handled by generator_runner) feeds this queue with substrate
    aggregates that need LLM judgment. Per-run hard cap is $10 in the
    Budget tracker; with the gate keeping the synthesizing/ queue
    small, expected per-day cost is single-digit dollars.

    No --quiet: the wrapper prints a one-line summary
    ("synthesizer: read=… proposals=… …") every 6h tick so the daemon's
    stdout log advances even on no-op runs. Silent daemons hide whether
    the pipeline is alive (cf. app-test-scheduler's logging blackout
    pre-2026-06-06).
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.proposal_synthesizer",
        user=bot_id,
        script_path=ANALYZER_DIR / "run_proposal_synthesizer.py",
        # Every 6 hours per spec §7 default. interval-based; no calendar
        # alignment matters since the queue is the gating factor (not
        # wall-clock time).
        schedule={"interval": 6 * 3600},
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR)],
        run_at_load=False,
        # Desync from other long-interval daemons that might also fire
        # at startup or share a wall-clock boundary.
        jitter_seconds=600,
    )


def _install_launchd_retention(bot_id: str, result: DeployResult) -> None:
    """Install daily data-retention pruner (03:30, evolve user).

    Prunes data categories that have no other cleanup path:
      - signals/archived/   → 90-day retention (spec §7)
      - signals/log/        → 365-day rolling retention (spec §7)
      - watchdog/           → 365-day retention (CLAUDE.md §Signal store)
      - proposals/archived/ → 90-day retention (mirrors signals archived)
      - alerts/{dispatcher,dispatcher-suppressed,delivery-failures}/
                            → 30-day retention (dispatcher rotation)
      - incidents/          → 30-day retention (heal.py day-dirs; longest
                              active reader is gateway_diagnostician at 7d)
      - audit_outbox/_ingested/ (per-bot) + infra_audit_outbox/_ingested/
                            → 30-day retention (drained audit-record
                              archives; signals.audit_outbox_retention,
                              called from prune_retention())

    03:30 is off-peak, after the 02:00 backup job and before the 09:30
    update-watcher. run_at_load=False: first fire is the next 03:30.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.retention",
        user=bot_id,
        script_path=ANALYZER_DIR / "run_retention.py",
        schedule={"Hour": 3, "Minute": 30},
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR)],
        run_at_load=False,
    )


def _install_launchd_log_cap(bot_id: str, result: DeployResult) -> None:
    """Install daily flat-file log-size cap (03:35, evolve user).

    Caps the flat-file logs that have no in-process rotation:
      - logs/audit.log           — audit.py's _log() append target
      - logs/better_engine.log   — better_engine_refresh.py's _log() AND
                                    the launchd StandardOutPath for the
                                    same daemon (copy-then-truncate keeps
                                    the open launchd fd valid)
      - logs/audit-warns.jsonl   — audit.py's warn-finding JSONL

    Default policy is 10 MB × 3 backups (see log_cap.DEFAULT_*); the
    built-in DEFAULT_TARGETS list is used when no positional paths are
    passed, so the launchd plist only needs to invoke the script.

    03:35 slots between retention (03:30) and proposal-auto-resolve
    (03:45). No ordering dependency on either — the cap doesn't read
    signals or proposals — but distinct minutes avoid simultaneous
    launchd wakeups on the same node. run_at_load=False: first fire is
    the next 03:35.

    NOTE: log_cap.py's targets are evolve-owned files under
    /Users/Shared/evolve/logs/. Per-bot gateway.log/.err.log files are
    bot-owned and need sudo to rotate — that's handled by the separate
    ai.evolve.evolve.oc-log-rotate daemon installed below.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.log-cap",
        user=bot_id,
        script_path=ANALYZER_DIR / "run_log_cap.py",
        schedule={"Hour": 3, "Minute": 35},
        result=result,
        extra_args=[],
        run_at_load=False,
    )


def _install_launchd_oc_log_rotate(bot_id: str, result: DeployResult) -> None:
    """Install daily OC gateway-log rotator (04:30, evolve user).

    Truncates ``/Users/<bot>/.openclaw/logs/gateway.log`` and
    ``gateway.err.log`` for any bot whose file exceeds 10MB. These two
    files are populated by the per-bot gateway plist's ``StandardOut/
    ErrorPath`` capture; launchd has no rotation for those, and OC's
    own logger (writing to ``logging.file`` per setup_wizard +
    ensure_plugin_config) does NOT touch them. Most of what lands in
    them is startup banner / transient stderr that the operator
    rarely needs to inspect — truncation, not archival rotation, is
    fine.

    Separate from ai.evolve.evolve.log-cap because gateway.log/.err.log
    are bot-owned (mode 600); evolve has ACL read but not write, so
    rotation needs ``sudo /usr/bin/truncate`` (narrow sudoers grant
    pinned to the two filenames). log_cap's in-process ``os.truncate``
    can't reach them.

    04:30 fits after retention (03:30), log-cap (03:35),
    proposal-auto-resolve (03:45), overrides-expiry (04:00); precedes
    anthropic-admin-ingest (04:15 — note overlap, but the workloads
    don't interact).
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.oc-log-rotate",
        user=bot_id,
        script_path=ANALYZER_DIR / "run_oc_log_rotate.py",
        schedule={"Hour": 4, "Minute": 30},
        result=result,
        run_at_load=False,
    )


def _install_launchd_openclaw_overrides_expiry(
    bot_id: str, result: DeployResult,
) -> None:
    """Install daily expires_at enforcer (04:00, evolve user).

    Walks every bot's sandbox/overrides/<bot>.json, deletes lapsed
    overrides + emits Signals (expired + pre_expiry within 7d). Phase 5
    of docs/spec-openclaw-json-derived-artifact-2026-05-24.md.

    04:00 follows retention (03:30) + proposal-auto-resolve (03:45)
    and precedes the anthropic-admin-ingest at 04:15 — comfortably
    inside the daily-maintenance window.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.openclaw-overrides-expiry",
        user=bot_id,
        script_path=ANALYZER_DIR / "run_openclaw_overrides_expiry.py",
        schedule={"Hour": 4, "Minute": 0},
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR)],
        run_at_load=False,
    )


def _install_launchd_breakers_audit(bot_id: str, result: DeployResult) -> None:
    """Install breakers audit-of-cause generator (every 5 min, evolve user).

    Scans active breaker trips and writes ``audit_summary`` +
    ``audit_recommendation`` back to each trip's JSON record. The
    dashboard modal's "📋 Diagnosis" section renders these fields when
    present, giving the operator a "what happened + what to do about
    it" view alongside the raw trip record.

    Pure Python — no LLM call. Pattern-matches the heartbeat-on-wrong-
    model / runaway-session / cache-write-no-reuse incidents from
    docs/incident-cost-audit-2026-05-21.md, with a manual-review
    fallback when nothing matches cleanly.

    Idempotent: trips whose audit fields are already populated are
    skipped on subsequent runs. The 5-minute cadence balances
    "operator sees diagnosis quickly after a trip" against "we don't
    re-read four hours of turn JSONL every minute".
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.breakers-audit",
        user=bot_id,
        script_path=ANALYZER_DIR / "run_breakers_audit.py",
        schedule={"interval": 300},   # StartInterval — every 5 minutes
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR), "--once"],
        run_at_load=True,
        jitter_seconds=30,   # desync from other 5-min daemons
    )


def _install_launchd_breakers_runner(bot_id: str, result: DeployResult) -> None:
    """Install the circuit-breakers detector runner (every 10 min, evolve user).

    Drives ``breakers.runner`` over the pod's recent turn data. The runner
    evaluates the activity-shape detector, appends decisions to
    ``{shared_dir}/breakers/runner-log/``, and acts on trips unless
    ``network.json::breakers.auto_trip_enabled`` is false (default ``true``
    — ARMED since the §5.2 arming PR closed the calibration soak;
    ``evolve-admin breaker disarm`` returns it to observe-only).

    10-minute cadence mirrors the runner's own daemon-mode default
    (``--interval-seconds 600``) and spec §8 Phase 5's "detector runs every
    N minutes." Periodic ``--once`` (matching the sibling breakers-audit
    daemon) rather than a KeepAlive ``--daemon`` so a crashed cycle simply
    waits for the next tick instead of relaunch-storming.

    The runner creates ``{shared_dir}/breakers/runner-log/`` itself on first
    write (parents=True); the dir lives under the evolve-owned shared dir, so
    no extra permission setup is needed. Pure Python, no LLM, no sudo.

    Spec: docs/spec-circuit-breakers-2026-05-21.md §5.1 / §8 Phase 5.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.breakers-runner",
        user=bot_id,
        script_path=ANALYZER_DIR / "run_breakers_runner.py",
        schedule={"interval": 600},   # StartInterval — every 10 minutes
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR), "--once"],
        run_at_load=True,
        jitter_seconds=60,   # desync from breakers-audit (:30) + other daemons
    )


def _install_launchd_autonomy_limits(bot_id: str, result: DeployResult) -> None:
    """Install the autonomy limits + demotion-reflex pass (every 5 min,
    evolve user).

    Spec docs/spec-autonomy-ladder-2026-06-10.md §1.3 + §3.3 (Phase B).
    Evaluates rung-3 daily caps from the bot-side outward-action ledger,
    pauses capped integrations for the rest of the UTC day (re-render +
    gateway kickstart), and runs the auto-demotion reflex. The
    permission monitor repeats the same evaluation on the audit cadence
    as the slow backstop — this daemon exists because §3.3's rationale
    is "during an active incident, minutes matter," the same cadence
    reasoning as the 5-minute breakers audit above.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.autonomy-limits",
        user=bot_id,
        script_path=ANALYZER_DIR / "run_autonomy_limits.py",
        schedule={"interval": 300},   # StartInterval — every 5 minutes
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR), "--once"],
        run_at_load=True,
        jitter_seconds=45,   # desync from breakers-audit's :30 offset
    )


def _install_launchd_proposal_auto_resolve(
    bot_id: str, result: DeployResult
) -> None:
    """Install daily proposal auto-resolve sweep (03:45, evolve user).

    Archives proposals whose ``motivating_signals[]`` are all
    resolved/dismissed (or have aged out of the signal store) — the
    underlying condition has cleared and the operator shouldn't have
    to manually dismiss the stale proposal. Pure Python sweep over
    ``proposals/pending/`` + ``proposals/snoozed/`` against the Signal
    store.

    03:45 follows retention (03:30) so any signals that retention
    archived in this run are visible to the sweep, and precedes the
    anthropic-admin-ingest at 04:15.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.proposal-auto-resolve",
        user=bot_id,
        script_path=ANALYZER_DIR / "run_proposal_auto_resolve.py",
        schedule={"Hour": 3, "Minute": 45},
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR)],
        run_at_load=False,
    )


def _install_launchd_anthropic_admin_ingest(
    bot_id: str, result: DeployResult
) -> None:
    """Install daily Anthropic Admin API ingest (04:15, evolve user).

    Fetches the prior UTC day's cost report + audit-log page from
    Anthropic's Admin API and snapshots them under
    ``{shared_dir}/anthropic_api/``. Compares Anthropic's reported total
    against the local cost ledger and emits
    ``cost_diverges_from_anthropic`` when they disagree by >10%.

    04:15 is after retention (03:30) and the 02:00 backup, comfortably
    inside the window where yesterday's UTC day is finalized for any
    reasonable host clock skew.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.anthropic-admin-ingest",
        user=bot_id,
        script_path=ANALYZER_DIR / "run_anthropic_admin_ingest.py",
        schedule={"Hour": 4, "Minute": 15},
        result=result,
        extra_args=[
            "--shared-dir",
            str(_CANONICAL_SHARED_DIR),
            "--network",
            str(_CANONICAL_NETWORK_JSON),
        ],
        run_at_load=False,
    )


def _install_launchd_cost_watchdog(bot_id: str, result: DeployResult) -> None:
    """Install cost_watchdog Signal-emitter (hourly, evolve user).

    Reads cost_event JSONL + each bot's openclaw cron records + workspace
    file sizes; emits Signals for daily-spend, automation-dominance,
    cron-wakes-agent (config smell), cron-overactive, context-bloat. Pure
    Python, no LLM. efficiency_hawk consumes these signals and produces
    Proposals on its daily run.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.cost_watchdog",
        user=bot_id,
        script_path=ANALYZER_DIR / "cost_watchdog.py",
        schedule={"interval": 3600},   # StartInterval — hourly
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=180,   # desync from other hourly daemons — H8
    )


def _install_launchd_session_economics(bot_id: str, result: DeployResult) -> None:
    """Install session_economics Signal-emitter (hourly, evolve user).

    Reads cost_event JSONL; emits Signals for cache-health and
    engagement gaps that cost_watchdog doesn't already cover:
    cache_invalidation_elevated (TTL-too-short fingerprint),
    cache_hit_rate_low (caching not engaging — prompt churn or
    wrong TTL), bot_unused (zero user_turn events in N days). Pure
    Python, no LLM. Distinct from cost_watchdog: that producer owns
    spend/automation/cron/MD-bloat; this one owns cache economics
    and engagement.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.session_economics",
        user=bot_id,
        script_path=ANALYZER_DIR / "session_economics.py",
        schedule={"interval": 3600},   # StartInterval — hourly
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=300,   # desync from other hourly daemons — H8
    )


def _install_launchd_cve_scan_finalize(bot_id: str, result: DeployResult) -> None:
    """Install security-cve-scan finalizer (daily 09:10 PT, evolve user).

    Reads the LLM-produced candidate JSON at
    ``/Users/Shared/evolve/security/candidates-{date}.json``, applies
    installed-version + baseline-mute + idempotency filters, renders
    the operator-facing message per ``docs/operator-message-style.md``,
    and dispatches via the security Telegram channel. Idempotent —
    a duplicate same-day invocation no-ops.

    Fires at 09:10 to give the 09:00 LLM discovery cron headroom to
    finish. If the LLM cron is still running / fails, the finalizer
    sees no candidate JSON and writes an "All clear" log entry.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.security-cve-scan-finalize",
        user=bot_id,
        script_path=(
            ANALYZER_DIR / "evolve_apps" / "security-cve-scan" / "finalize.py"
        ),
        schedule={"Hour": 9, "Minute": 10},
        result=result,
        run_at_load=False,
        jitter_seconds=30,
    )


def _install_launchd_embedding_monitor(bot_id: str, result: DeployResult) -> None:
    """Install embedding_monitor Signal-emitter (hourly, evolve user).

    Tails each bot's gateway.err.log, classifies embedding-API failures,
    emits provider_failing / rate_limit_storm Signals so a quota-exhausted
    or revoked credential gets surfaced instead of silently shadowing the
    fallback chain. Pure Python, no LLM. Mirrors cost_watchdog cadence.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.embedding_monitor",
        user=bot_id,
        script_path=ANALYZER_DIR / "embedding_monitor.py",
        schedule={"interval": 3600},   # StartInterval — hourly
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=240,   # desync from other hourly daemons — H8
    )


def _install_launchd_measure(result: DeployResult) -> None:
    """Install the pod-wide daily metrics job (daily 01:00, evolve user).

    ``ai.openclaw.evolve.measure`` iterates every member in network.json,
    writes one per-bot metrics file per date, then runs the value-baseline
    pod rollup. Pure Python — no macOS host APIs — so it belongs on every
    pod regardless of OS. It is in ``expected_plist_labels`` but was only
    ever installed by the legacy macOS-only ``migrate-jobs`` static-plist
    copy, never by the fresh-install path here — so a Linux pod's health
    scan flagged "systemd unit not found: …measure.service" (round-3 #C).
    Routing it through the seam installs a launchd plist on macOS and a
    systemd unit on Linux, byte-identically on macOS.

    No ``--bot-id`` → full-fleet run (the value-baseline post-step only
    runs on full-fleet runs). Cadence mirrors the retired static plist
    (01:00, before the 01:30 tuples extraction).
    """
    _install_launchd(
        label="ai.openclaw.evolve.measure",
        user="evolve",
        script_path=ANALYZER_DIR / "measure.py",
        schedule={"Hour": 1, "Minute": 0},
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR)],
        run_at_load=False,
        jitter_seconds=120,
    )


def _install_launchd_alerts_loop_monitor(result: DeployResult) -> None:
    """Install pod-wide alerts_loop_monitor Signal-emitter (hourly, evolve user).

    Reads {shared_dir}/alerts/dispatcher.jsonl (successful sends) and
    {shared_dir}/alerts/delivery-failures.jsonl (failed sends — split
    out for the PWA-polish file split) and emits Signals for two
    operator-invisible-by-default loop patterns: many FAILED sends from
    one source (alerts silently swallowed), and same-content SENT
    repeats across hours (a heal-style nag where what's needed is a
    structural fix). Pure Python, no LLM.

    Pod-wide — the dispatcher log is shared, not per-bot. Installed
    once under the evolve user. See alerts_loop_monitor.py for the
    detector logic + configurable thresholds.
    """
    _install_launchd(
        label="ai.openclaw.evolve.alerts_loop_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "alerts_loop_monitor.py",
        schedule={"interval": 3600},   # StartInterval — hourly
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=420,   # desync from cost_watchdog (180) +
                              # session_economics (300) + embedding_monitor (240) — H8
    )


def _install_launchd_deploy_drift_monitor(result: DeployResult) -> None:
    """Install pod-wide deploy_drift_monitor Signal-emitter (hourly, evolve user).

    Reads install.json + network.json and emits one pod-wide Signal when
    member bots have deployed an older Evolve commit than the admin
    server. Distinct from sysadmin_watchdog/version_behind (which
    measures release lag in days). Pure Python, no LLM.
    """
    _install_launchd(
        label="ai.openclaw.evolve.deploy_drift_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "deploy_drift_monitor.py",
        schedule={"interval": 3600},   # StartInterval — hourly
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=480,   # desync from alerts_loop_monitor (420) — H8
    )


def _install_launchd_bot_recovery_monitor(result: DeployResult) -> None:
    """Install pod-wide bot_recovery_monitor (hourly, evolve user).

    Walks each bot's heal-written status file at
    ``{shared_dir}/status/{bot_id}.json`` and emits an info-tier Signal
    per active recovered_alerts entry. Auto-resolves when the entry
    decays out of heal's 24h sticky window or the underlying error
    recurs. Pure Python, no LLM.
    """
    _install_launchd(
        label="ai.openclaw.evolve.bot_recovery_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "bot_recovery_monitor.py",
        schedule={"interval": 3600},   # StartInterval — hourly
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=540,   # desync from deploy_drift_monitor (480) — H8
    )


def _install_launchd_stuck_proposal_monitor(result: DeployResult) -> None:
    """Install pod-wide stuck_proposal_monitor (hourly, evolve user).

    Scans ``{shared_dir}/proposals/approved/`` for proposals whose mtime is
    older than 7 days and fires a single pod-wide Signal listing them all.
    Added after the 2026-05-20 forensic case where ``heal-team_bot_a-1776294604``
    sat in approved/ for a full month because a quarantine unlink silently
    failed and apply.py's idempotency check pinned it. Pure Python, no LLM.
    """
    _install_launchd(
        label="ai.openclaw.evolve.stuck_proposal_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "stuck_proposal_monitor.py",
        schedule={"interval": 3600},   # StartInterval — hourly
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=600,   # desync from bot_recovery_monitor (540) — H8
    )


def _install_launchd_monitor_coverage(result: DeployResult) -> None:
    """Install pod-wide monitor_coverage daemon (daily, evolve user).

    The SELF_AUDIT — walks every Evolve launchd plist, computes each
    daemon's expected cadence from StartInterval (or StartCalendarInterval),
    and emits a Signal when any daemon's stdout log is older than the
    expected interval × 3 (bounded [5 min, 7 d]). Catches the failure
    mode where a monitor goes silent and nobody notices — the case that
    motivated this whole effort was finding verify.log silent for a full
    month on the mini.

    Daily cadence — silence within 24h is the worst-case detection
    window. Pure Python, no LLM. Spec context: Security_bot retirement
    coverage sweep, design idea #1.
    """
    _install_launchd(
        label="ai.openclaw.evolve.monitor_coverage",
        user="evolve",
        script_path=ANALYZER_DIR / "monitor_coverage.py",
        schedule={"interval": 86400},   # StartInterval — daily
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=720,   # desync from backup_signal (660) — H8
    )


def _install_launchd_gmail_integration_health(result: DeployResult) -> None:
    """Install pod-wide gmail_integration_health monitor (30 min, evolve user).

    Per-bot probe of Google API health for every bot whose network.json
    carries a ``google_integration`` block. Categorises 401 (DwD
    unauthorized), 403 (scope), 404 (subject), 5xx (transient) and
    fires a signature-deduped Signal — one per (bot, failure_category) —
    with the same operator-facing remediation copy the wizard shows at
    config time. Auto-resolves on the next clean probe.

    30-minute cadence: the failure modes (key rotation, DwD authorization
    pull, user suspension) are operator-visible-only-eventually without
    this monitor; 30m is the spec's cap (§8.1) on how stale a Signal
    can be before it stops being useful.

    Spec: docs/spec-google-integration-paths-2026-05-30.md §8 (PR δ).
    """
    _install_launchd(
        label="ai.openclaw.evolve.gmail_integration_health",
        user="evolve",
        script_path=ANALYZER_DIR / "monitor_gmail_integration_health.py",
        schedule={"interval": 1800},   # StartInterval — 30 min
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=840,   # desync from install_integrity_monitor (780) — H8
    )


def _install_launchd_delivery_monitor(result: DeployResult) -> None:
    """Install the pod-wide proactive-delivery monitor (5 min, evolve user).

    Walks every installed manifest's scheduled_actions[], computes due
    delivery windows, classifies each elapsed window tri-state (delivered
    on time / did_not_run / ran_undelivered / unmeasurable), appends per-
    window rows to {shared_dir}/delivery_monitor/ledger/, and emits
    app_delivery_missed / app_delivery_unmeasurable Signals. Detection
    only in this build — the §8 heal path (kickstart/bootstrap + sudoers
    grants) is PR 2 of the spec rollout.

    5-minute cadence matches heal.py: a 7:00 briefing with the default
    30-minute grace gets its miss classified within minutes of 7:30.

    Spec: docs/spec-proactive-delivery-monitor-2026-06-10.md §4 (U2.1).
    """
    _install_launchd(
        label="ai.evolve.evolve.delivery-monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "delivery_monitor.py",
        schedule={"interval": 300},    # StartInterval — 5 min
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=120,   # desync from heal (60) + breakers-audit — H8
    )


def _install_launchd_install_integrity_monitor(result: DeployResult) -> None:
    """Install pod-wide install_integrity_monitor daemon (daily, evolve user).

    Runs the wizard verification gauntlet's non-interactive checks
    (Check 1 ownership, Check 2 agent dry-run, Check 3 channel
    handshake) against every bot in the pod once a day and emits a
    Signal per non-OK finding. Sweep-resolves stale findings on the
    next pass when the operator fixes the underlying condition.

    Daily cadence — the gauntlet's checks are correctness checks, not
    liveness. Liveness is already covered hourly by heal/pod-health.
    File ownership, credential shape, and `model.primary` drift slowly;
    daily is enough to surface within-day without burning cycles.

    Spec: docs/spec-wizard-verification-gauntlet-2026-05-30.md (the
    monitor was deferred there as an out-of-scope item; landed as a
    follow-up because the gap was real).
    """
    _install_launchd(
        label="ai.openclaw.evolve.install_integrity_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "install_integrity_monitor.py",
        schedule={"interval": 86400},   # StartInterval — daily
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=780,   # desync from monitor_coverage (720) — H8
    )


def _install_launchd_digest_source_audit(result: DeployResult) -> None:
    """Install pod-wide digest_source_audit daemon (daily 04:35, evolve user).

    Reads every bot's ``workspace/digest/source_health-{date}.json`` files,
    tracks per-(bot, source) consecutive-failure runs, and emits one Signal
    (``digest_source_broken``) per source that has been dark for
    ``CONSECUTIVE_FAILURE_THRESHOLD`` runs (default 3). Auto-resolves via
    sweep_resolve when a source comes back on the next successful run.

    The 2026-06-05 atlas Brave/Telegram lookup fixes (commit forthcoming)
    surfaced this need: atlas_digest.py was logging
    ``[fetchers] HTTP Error 404`` to stderr for retired RSS feeds, but
    those lines were just noise — nothing turned a persistently-broken
    source into an actionable Signal. With this daemon installed,
    a source moving (Anthropic-style retirement, blog.google URL
    refactor) surfaces on the Alerts page within ~3 days instead of
    waiting for the operator to notice an empty digest bucket.

    04:35 UTC is 5 min after reconcile-audit at 04:30 to stay desync'd.
    Pure Python, no LLM, reads bot workspaces via the existing evolve
    ACL read grant (no sudo needed). Idempotent.
    """
    _install_launchd(
        label="ai.evolve.evolve.digest-source-audit",
        user="evolve",
        script_path=ANALYZER_DIR / "run_digest_source_audit.py",
        schedule={"Hour": 4, "Minute": 35},
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR), "--once"],
        run_at_load=False,
    )


def _install_launchd_reconcile_audit(result: DeployResult) -> None:
    """Install pod-wide reconcile_audit daemon (daily 04:30, evolve user).

    Walks every installed app's ``scheduled_actions[]`` against the
    current gallery and emits one Signal (``scheduled_actions_drift``)
    per drifted (bot, app) pair. Signature embeds bot + app so the
    Alerts UI shows per-pair drift without aggregating.

    The 2026-06-04 Atlas Daily Digest incident was caught reactively
    after six days of silence. With this daemon installed, the same
    incident would surface on the Alerts page the morning after a
    gallery migration lands — paged by the daemon, not by the operator
    noticing that messages never arrived. The fix command is in the
    Signal body so the operator doesn't have to look it up::

      sudo evolve-admin reconcile-actions --apply

    04:30 UTC sits between retention (03:30) / proposal_auto_resolve
    (03:45) and anthropic_admin_ingest (04:15) so it doesn't pile on
    the early-morning daemon bunch. Pure Python, no LLM. Idempotent
    via ``signals.store.observe`` (same signature → existing Signal
    updated, not duplicated) and ``signals.store.sweep_resolve``
    (drift cleared on disk → corresponding Signal archived on next run).
    """
    _install_launchd(
        label="ai.evolve.evolve.reconcile-audit",
        user="evolve",
        script_path=ANALYZER_DIR / "run_reconcile_audit.py",
        schedule={"Hour": 4, "Minute": 30},
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR), "--once"],
        run_at_load=False,
    )


def _install_launchd_agent_bypass_audit(result: DeployResult) -> None:
    """Install pod-wide agent_bypass_audit daemon (daily 04:40, evolve user).

    Walks recent session transcripts on bots that have at-risk apps
    installed (apps whose manifest ``bot_guidance`` routes a chat trigger
    through a bot-local script — currently atlas-on-demand-research and
    atlas-article-capture). For each (bot, app), counts triggering
    messages that did NOT cause the declared script to be invoked, and
    emits one Signal per (bot, app) with bypass candidates over the
    24h window.

    Why this exists: the 2026-06-05 atlas live test exposed that when the
    declared script fails (OC exec preflight, missing file, etc.), the
    agent silently falls back to its general tools and answers freelance —
    bypassing scope gates, source grounding, opt-out honor, rate limits,
    and privacy logging. PR #2192 added a per-app instruction "do NOT
    freelance" and the JSON-request-file workaround that motivated it;
    POD_CONDUCT.md rule 11 (added 2026-06-05) generalises the instruction
    pod-wide. Both are *policy*, not enforcement. This daemon makes the
    residual bypass surface *visible* on the Alerts page until Phase 2
    (subagent narrowing, install-time validator) closes the gap
    structurally. Spec at docs/spec-agent-freelance-bypass-2026-06-05.md.

    04:40 UTC: 5 min after digest-source-audit (04:35) and 10 min after
    reconcile-audit (04:30) to stay desync'd from the early-morning
    daemon bunch.

    Pure Python, no LLM, no external I/O. Reads session transcripts via
    the existing evolve ACL read grant on ``/Users/<bot>/.openclaw/``.
    Idempotent via ``signals.store.observe`` (same signature → existing
    Signal updated) and ``signals.store.sweep_resolve`` (bypass cleared
    → Signal archived).
    """
    _install_launchd(
        label="ai.evolve.evolve.agent-bypass-audit",
        user="evolve",
        script_path=ANALYZER_DIR / "run_agent_bypass_audit.py",
        schedule={"Hour": 4, "Minute": 40},
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR), "--once"],
        run_at_load=False,
    )


def _install_launchd_oc_substrate_monitor(result: DeployResult) -> None:
    """Install pod-wide oc_substrate_monitor daemon (hourly, evolve user).

    Signal producer for OpenClaw substrate-daemon freshness — the
    auto-updater LaunchAgent state file and the usage-collector
    LaunchAgent's daily rollup file. Both live outside the
    ``ai.evolve.evolve.*`` / ``ai.openclaw.evolve.*`` naming that
    monitor_coverage walks, so a silence in either historically went
    unnoticed by the in-pod stack and was only caught by the pod-
    admin-side ``openclaw-watchdog.py`` script.

    Hourly cadence — both upstream daemons advertise 30-60 min cadences,
    so hourly detection bounds silent staleness at ~2.5h worst-case.

    Pure Python, no LLM. Replaces ``check_updater_freshness`` and
    ``check_collector_freshness`` from the retired watchdog.
    """
    _install_launchd(
        label="ai.openclaw.evolve.oc_substrate_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "oc_substrate_monitor.py",
        schedule={"interval": 3600},   # StartInterval — hourly
        result=result,
        extra_args=[],
        run_at_load=True,
        jitter_seconds=540,   # desync from cost_watchdog (180) and
                              # gmail_integration_health (840)
    )


def _install_launchd_home_artifacts_monitor(result: DeployResult) -> None:
    """Install pod-wide home_artifacts_monitor daemon (hourly, evolve user).

    Per-bot Signal producer for workspace large/exec file appearances
    and recent macOS LaunchServices Quarantine downloads. Replaces the
    pod-admin-side openclaw-watchdog's
    ``check_large_files_and_executables`` and ``check_quarantine_log``
    checks.

    Scope reduction vs the watchdog: the workspace scan is bounded to
    ``.openclaw/`` (the evolve user already has ACL read there;
    expanding to ``/Users/<bot>/`` recursively would require ``sudo
    /usr/bin/find /Users/*`` which carries a real privilege-escalation
    risk via ``find -exec``). The user-account compromise case
    (something dropped into ``~/Downloads``) is still caught by the
    Quarantine DB check, which reads the LaunchServices SQLite via a
    narrow ``sudo /bin/cp`` to /tmp.

    Hourly cadence — both checks tolerate a few cycles of latency.

    Pure Python, no LLM. Requires the sudoers grant in section 3g of
    ``_render_evolve_sudoers`` (Quarantine DB copy). When that grant is
    missing the monitor fires a per-bot ``quarantine_check_failed``
    Signal rather than silently reporting nothing.
    """
    _install_launchd(
        label="ai.openclaw.evolve.home_artifacts_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "home_artifacts_monitor.py",
        schedule={"interval": 3600},   # StartInterval — hourly
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=900,   # desync from oc_substrate_monitor (540)
    )


def _install_launchd_cascade_pressure_watchdog(result: DeployResult) -> None:
    """Install pod-wide cascade pressure watchdog daemon (60s, evolve user).

    Reads recent cascade telemetry spans + per-bot in-process tier1
    counters (``{shared}/{bot_id}/cascade/tier1_active.json``) and
    writes the pod-wide pressure-flag bundle to
    ``{shared}/cascade/pressure_flags.json`` on every poll. The
    CascadeController reads those flags at decision time to throttle
    escalation when the pod is under pressure (too many concurrent
    tier1 sessions, escalation storms, spend bursts).

    Cadence is 60s per the spec § pressure watchdog: pressure_flags.json
    carries a ``watchdog_heartbeat`` timestamp updated every poll;
    CascadeController treats a stale heartbeat (> 180s — three missed
    polls) as "watchdog dead" and shifts to conservative defaults.

    monitor_coverage's SILENCE_FLOOR_SEC is 5min, so a 60s daemon won't
    page on a single missed poll — silence detection waits 5 minutes,
    which matches the watchdog-of-the-watchdog escalation in the spec.

    Spec: docs/spec-tier-cascade-2026-05-26.md § pressure watchdog.
    """
    _install_launchd(
        label="ai.openclaw.evolve.cascade_pressure_watchdog",
        user="evolve",
        script_path=ANALYZER_DIR / "cascade" / "pressure_watchdog.py",
        schedule={"interval": 60},   # StartInterval — 60s heartbeat per spec
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR)],
        run_at_load=True,
        # No jitter — at 60s the desync window would exceed the interval.
        # The daemon's pure-Python read+write of ~few-MB files is cheap;
        # alignment with other launchd ticks is not an issue at this scale.
        jitter_seconds=0,
    )


def _install_launchd_cascade_audit_runner(result: DeployResult) -> None:
    """Install pod-wide cascade audit runner daemon (hourly, evolve user).

    Bridges cascade telemetry spans into the pod's standard alerting +
    calibration layers:

      - Signals (via signals.store.observe) — three types under
        producer ``cascade_audit``:
          * cascade_anomaly_* (per-bot, per-metric, auto-resolve on
            next-run clear)
          * dangerous_combo (per-session, manual triage)
          * runaway_rate_tripped (per-session, manual triage)

      - Labels (via labeler.write_labels) — per-day .jsonl files at
        {shared_dir}/cascade/labels/<YYYY-MM-DD>.jsonl, consumed by
        the Phase 4 calibration tuner.

    Hourly cadence — anomalies aren't minute-scale events, and label
    files roll daily so an hourly tick is plenty of granularity for
    the Phase 4 pipeline. Auto-bootstrap: safe to install day-one,
    no-ops on empty cascade telemetry until real data accumulates
    (the plugin's TurnObserver started writing spans in Phase 1).

    Spec: docs/spec-tier-cascade-2026-05-26.md § audit layer.
    """
    _install_launchd(
        label="ai.openclaw.evolve.cascade_audit_runner",
        user="evolve",
        script_path=ANALYZER_DIR / "cascade" / "audit_runner.py",
        schedule={"interval": 3600},   # StartInterval — hourly
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        # Desync from other hourly daemons. The chain so far:
        # alerts_loop_monitor=420, deploy_drift_monitor=480,
        # bot_recovery_monitor=540, stuck_proposal_monitor=600,
        # backup_signal=660, monitor_coverage=720 — pick 780, next slot up.
        jitter_seconds=780,
    )


def _install_launchd_pod_perms_drift_monitor(result: DeployResult) -> None:
    """Install pod-wide perm-drift monitor (hourly, evolve user, check_only).

    Closes the gap between deploys where ``ensure_pod_perms`` would
    otherwise let dir-ownership drift sit broken. The actual problem
    catch is described in detail on ``pod_perms_drift_monitor.py``'s
    module docstring; in short: per-bot daemons (running as bot
    users) can be the first writer to a shared dir, creating it
    with bot-user ownership. In a sticky 1777 dir, only the dir
    owner can rename foreign files — so cross-user admin operations
    (dismissing a proposal owned by a different bot daemon, applying
    a proposal that was drafted by yet another, etc.) fail with
    EACCES. ``ensure_pod_perms``'s contract is correct but only
    re-asserted at deploy time.

    This daemon runs ``ensure_pod_perms(check_only=True)`` hourly,
    emits a ``pod_perms_drift`` Signal when drift is detected. The
    operator runs ``sudo evolve-admin ensure-pod-perms`` to apply
    the fix (existing CLI; existing sudoers grant) — the next tick
    of this monitor then sees clean state and auto-resolves the
    Signal.

    Runs as evolve (not root) because the check pass needs no sudo
    — it only ``stat``s directories. Drift in dir mode / dir owner /
    ACL is all readable by any user with traverse access on the
    shared dir.

    Hourly cadence — drift accumulates on the order of days (between
    proposal events triggering new directory creation), so hourly is
    well within the response budget without burning CPU.
    """
    _install_launchd(
        label="ai.openclaw.evolve.pod_perms_drift_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "pod_perms_drift_monitor.py",
        schedule={"interval": 3600},   # StartInterval — hourly
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        # Next desync slot after code_quality_monitor=840.
        jitter_seconds=900,
    )


def _install_launchd_code_quality_monitor(result: DeployResult) -> None:
    """Install pod-wide code_quality_monitor daemon (daily, evolve user).

    Walks the deploy checkout's git log over the last 30 days and emits
    Signals for three repo-process KPIs:

      - revert_rate_high — share of merged PRs that were reverts climbed
        above 1.5% (warn) / 3% (alert). A revert is an event a pre-PR
        review should have caught.
      - fix_heavy_scope — per scope, fix:feat ratio ≥ 2x (and ≥ 8
        commits in the scope). Surfaces verification-weak surfaces
        where bugs slip past review.
      - sameday_fix_on_feat — share of feats whose author shipped a
        same-author related fix within 24h. The in-session bug pattern
        that the Stop-time verifier and pre-PR gate are designed to
        catch.

    Daily cadence — the metrics are 30-day rolling, so sub-daily ticks
    add no signal. ~5s wall-clock for ~1500 commits. Pure Python +
    git subprocess; no LLM, no network.
    """
    _install_launchd(
        label="ai.openclaw.evolve.code_quality_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "code_quality_monitor.py",
        schedule={"interval": 86400},   # StartInterval — daily
        result=result,
        extra_args=[
            "--network", str(_CANONICAL_NETWORK_JSON),
            # Deploy-checkout path, platform-keyed (W10-D): /Users/Shared/evolve-repo
            # on macOS (byte-identical), /var/lib/evolve/repo on a Linux pod — a bare
            # literal baked /Users into this daemon's systemd unit body.
            "--repo", str(_get_profile().deploy_checkout_default),
        ],
        run_at_load=True,
        jitter_seconds=840,   # next slot after install_integrity_monitor=780
    )


def _signal_subscriber_jobspec(label: str, user: str, script_path: Path) -> JobSpec:
    """Build the signal-subscriber daemon JobSpec (pure — no disk access).

    Log naming matches the other evolve-user pod-wide infra (the convention
    generated by _plist_content's job_name derivation) — mirrored explicitly
    here because this is a KeepAlive daemon (no StartInterval) and doesn't
    go through _plist_content.
    """
    log_dir = str(_user_home(user) / ".openclaw/logs")
    job_name = "signal-subscriber"
    return JobSpec(
        label=label,
        program_args=[
            str(VENV_PYTHON), str(script_path),
            "--shared-dir", str(_CANONICAL_SHARED_DIR),
            "--network", str(_CANONICAL_NETWORK_JSON),
        ],
        user=user,
        run_at_load=True,
        keep_alive=True,
        throttle_interval=10,
        stdout_path=f"{log_dir}/evolve-{job_name}.log",
        stderr_path=f"{log_dir}/evolve-{job_name}.err.log",
        env={
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "PYTHONPATH": str(ANALYZER_DIR),
            "EVOLVE_NETWORK": str(_CANONICAL_NETWORK_JSON),
            "EVOLVE_SHARED": str(_CANONICAL_SHARED_DIR),
        },
    )


def _install_launchd_signal_subscriber(result: DeployResult) -> None:
    """Install the pod-wide signal-subscriber daemon (long-running, evolve user).

    Watches ``/Users/Shared/evolve/signals/firing/`` and dispatches any
    generator whose charter declared ``subscribes_to: [<signal_type>, ...]``
    the moment a matching Signal lands. Closes the latency gap between
    Signal arrival and generator response — without this, an acute Signal
    landing two hours after the daily generator sweep had to wait ~22h for
    the next sweep to act on it.

    Long-running daemon (KeepAlive, not periodic) — distinct from the
    other infra daemons in this file, which are all StartInterval-driven.
    Polls firing/ at 1s; meets the 5s spec ceiling with margin and avoids
    pulling the ``watchdog`` library into the venv (no fsnotify needed at
    1s for a directory that holds tens of small JSON files).

    The daily generator_runner sweep stays — it's the safety net for the
    subscriber being down, the ledger being lost, or a generator that
    doesn't subscribe. Arbiter dedup ensures a sweep-side re-emission of
    the same Proposal merges into the subscriber-emitted one.

    Spec: docs/spec-signal-subscriber-2026-05-31.md.
    """
    label = "ai.evolve.evolve.signal-subscriber"
    user = "evolve"
    script_path = ANALYZER_DIR / "signal_subscriber_runner.py"

    if not Path(VENV_PYTHON).exists():
        result.log(
            f"[warn] Skipping {label}: Python interpreter not found at {VENV_PYTHON}. "
            f"Run 'evolve-admin setup venv', then re-run install-infra-jobs."
        )
        return
    if not script_path.exists():
        result.log(
            f"[warn] Skipping {label}: script not found at {script_path}."
        )
        return

    _install_spec_via_seam(
        _signal_subscriber_jobspec(label, user, script_path), result
    )


def _install_launchd_backup_signal(result: DeployResult) -> None:
    """Install pod-wide backup_signal monitor (hourly, evolve user).

    Reads each bot's per-attempt backup run-state file at
    ``{shared_dir}/backup/{bot_id}.json`` (written by backup.py after
    every attempt) and emits a ``backup_failing`` Signal once a bot has
    missed 3+ consecutive nightly runs. Auto-resolves via sweep_resolve
    when the bot recovers. Closes the May 2026 silent-failure gap where
    evo's backup bounced for weeks with no UI surface.
    """
    _install_launchd(
        label="ai.openclaw.evolve.backup_signal",
        user="evolve",
        script_path=ANALYZER_DIR / "backup_signal.py",
        schedule={"interval": 3600},   # StartInterval — hourly
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=660,   # desync from stuck_proposal_monitor (600) — H8
    )


def _install_launchd_local_backup_signal(result: DeployResult) -> None:
    """Install pod-wide local_backup_signal monitor (hourly, evolve user).

    Phase 2 of the backup architecture rework (spec-backup-and-data-
    classification-2026-05-28.md). Wraps ``tmutil`` to fire Signals when
    Time Machine is missing, stale, or excluding pod paths. Pure Python;
    cheap (3-4 local subprocess calls per pass). Auto-resolves via
    sweep_resolve when conditions clear.
    """
    _install_launchd(
        label="ai.openclaw.evolve.local_backup_signal",
        user="evolve",
        script_path=ANALYZER_DIR / "local_backup_signal.py",
        schedule={"interval": 3600},   # StartInterval — hourly
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=840,   # 0..840s random sleep — statistically biases against same-tick collision with backup_signal (660) and monitor_coverage (720). NOT a fixed offset.
    )


def _install_launchd_backup_audit_signal(result: DeployResult) -> None:
    """Install pod-wide backup_audit_signal monitor (hourly, evolve user).

    Phase 4a of the backup architecture rework (spec-backup-and-data-
    classification-2026-05-28.md §"Post-push audit"). Reads each bot's
    backup repo HEAD tree and verifies every path classifies as
    ``cloud``. Fires alert Signals when a non-cloud path is found —
    caught after-the-fact since the path has already been pushed.
    Defense-in-depth for bugs in the Phase 3 classification filter or
    operator reclassification after files were pushed.
    """
    _install_launchd(
        label="ai.openclaw.evolve.backup_audit_signal",
        user="evolve",
        script_path=ANALYZER_DIR / "backup_audit_signal.py",
        schedule={"interval": 3600},   # StartInterval — hourly
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=960,   # 0..960s random sleep — statistical desync from sibling backup monitors. NOT a fixed offset; the "900 collision" note in earlier comments described the deterministic-offset semantics this code doesn't actually have.
    )


def _install_launchd_local_backup_excluder(result: DeployResult) -> None:
    """Install pod-wide local_backup_excluder reconciler (hourly, evolve user).

    Phase 4c of the backup architecture rework — syncs ephemeral-classified
    paths to Time Machine exclusions via ``tmutil addexclusion``. Opt-in
    via ``network.json::backup.tm_exclusion_sync``; no-ops when the flag
    is unset so the daemon is safe to install pod-wide on day one. Hourly
    cadence keeps newly-classified paths from waiting too long.
    """
    _install_launchd(
        label="ai.openclaw.evolve.local_backup_excluder",
        user="evolve",
        script_path=ANALYZER_DIR / "local_backup_excluder.py",
        schedule={"interval": 3600},   # StartInterval — hourly
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=True,
        jitter_seconds=1020,   # 0..1020s random sleep — statistical desync from sibling backup monitors. NOT a fixed offset.
    )


def _install_launchd_verify(bot_id: str, result: DeployResult) -> None:
    """Install arbiter verify daemon (every 5 minutes, evolve user).

    Runs one verify cycle per invocation — checks proposals in applied/,
    evaluates metric claims, and routes to succeeded/failed_*.
    """
    _install_launchd(
        label=f"ai.evolve.{bot_id}.verify",
        user=bot_id,
        script_path=ANALYZER_DIR / "verify" / "daemon.py",
        schedule={"interval": 300},   # StartInterval — every 5 minutes
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR)],
        run_at_load=True,
        jitter_seconds=60,   # desync from other 5-min daemons (heal) — H8
    )


def _admin_ui_jobspec(label: str) -> JobSpec:
    """Build the admin web UI daemon JobSpec (pure — no disk access)."""
    return JobSpec(
        label=label,
        program_args=[
            VENV_EVOLVE_ADMIN, "serve",
            "--host", "127.0.0.1", "--port", "5050",
        ],
        user="evolve",
        keep_alive=True,
        run_at_load=True,
        # launchd's 256 soft NOFILE default caused the 2026-07-28 EMFILE storm.
        soft_file_limit=4096,
        stdout_path=str(_user_home("evolve") / ".openclaw/logs/evolve-admin-ui.log"),
        stderr_path=str(_CANONICAL_SHARED_DIR / "logs/evolve-admin-ui.err.log"),
        env={
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "EVOLVE_NETWORK": str(_CANONICAL_NETWORK_JSON),
        },
    )


def _install_launchd_admin_ui(user: str, result: DeployResult) -> None:
    """Install the admin web UI as a persistent KeepAlive launchd job (evolve user)."""
    label = f"ai.evolve.{user}.admin-ui"
    _install_spec_via_seam(_admin_ui_jobspec(label), result)


def _mcp_bridge_jobspec(label: str, user: str, port: int, host: str) -> JobSpec:
    """Build the MCP bridge daemon JobSpec (pure — no disk access)."""
    return JobSpec(
        label=label,
        program_args=[
            str(VENV_PYTHON), "-m", "evolve_admin.mcp_bridge",
            "--network", str(_CANONICAL_NETWORK_JSON),
            "--port", str(port), "--host", host,
        ],
        user=user,
        keep_alive=True,
        run_at_load=True,
        stdout_path=str(_user_home("evolve") / ".evolve/logs/mcp-bridge.log"),
        stderr_path=str(_user_home("evolve") / ".evolve/logs/mcp-bridge.log"),
        env={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    )


def _install_launchd_mcp_bridge(user: str, result: DeployResult) -> None:
    """Install the MCP bridge as a system-scope LaunchDaemon (evolve user).

    Lives alongside the other ai.evolve.evolve.* infra daemons. The bridge
    listens on 0.0.0.0:5051 (Tailscale-reachable) and exposes pod context to
    Claude Desktop / Claude Code over MCP. Pre-2026-05-30 the bridge ran as a
    LaunchAgent under ~/Library/LaunchAgents/com.evolve.mcp-bridge.plist;
    that scope structurally cannot load on headless pods (no Aqua session
    for the admin user), so it was converted to system-scope.

    Sweeps legacy LaunchAgent registrations and stale plist files before
    writing the system plist, so an in-place upgrade leaves no orphans.

    Reads port from network.json::mcp_bridge.port; falls back to 5051.
    """
    # One-time migration: clean up any legacy LaunchAgent before the new
    # system daemon takes over. Idempotent — re-runs are no-ops after the
    # legacy plists are gone.
    try:
        from . import mcp_service as _mcp_svc
        legacy_notes = _mcp_svc._bootout_legacy_agents()
        for note in legacy_notes:
            result.log(f"mcp-bridge legacy cleanup: {note}")
    except Exception as exc:  # noqa: BLE001
        result.log(f"[warn] mcp-bridge legacy cleanup raised: {exc}; continuing")

    label = f"ai.evolve.{user}.mcp-bridge"

    port = 5051
    host = "0.0.0.0"
    try:
        from .config import load_network, DEFAULT_NETWORK_CONFIG
        net = load_network(DEFAULT_NETWORK_CONFIG)
        mcp_cfg = net.get("mcp_bridge", {}) or {}
        port = int(mcp_cfg.get("port", port))
    except Exception:
        pass

    _install_spec_via_seam(_mcp_bridge_jobspec(label, user, port, host), result)


