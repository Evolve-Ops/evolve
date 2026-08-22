"""
health.py — Pod health checker.

Scans all OpenClaw instances, checks file locations, permissions, and process
state. Can be run as part of the setup wizard verification pass or on demand:

    evolve-admin health            # human-readable report
    evolve-admin health --fix      # apply fixes automatically (calls sudo as needed)
    evolve-admin health --json     # machine-readable output

Fixes are executed directly — no copy-paste required.  Each CheckResult carries
structured fix_args that apply_all_fixes() dispatches to the right subprocess.

Execution contexts:
  wizard     — runs as root; can apply every fix
  web server — runs as evolve user with narrow sudo grants; applies what it can
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import stat
import subprocess
import urllib.error

from platform_profile import get_profile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import DEFAULT_NETWORK_CONFIG, DEFAULT_SHARED_DIR, load_network, get_bot_port, get_bot_user, bot_home as _bot_home
from .deploy import LAUNCHD_DIR, EVOLVE_VERSION, VENV_PYTHON, VENV_EVOLVE_ADMIN, PLUGIN_SRC_DIR, PLUGIN_INSTALL_DIR, expected_plist_labels, is_label_feature_gated_off, per_bot_evolve_plist_labels, per_bot_gateway_plist_label

log = logging.getLogger(__name__)

# ── Better Engine integration ─────────────────────────────────────────────────

def _flag_urgent_refresh(shared_dir: Path, source: str, reason: str, **kwargs) -> None:
    """Write the Better Engine urgent-refresh flag file (§2.2)."""
    try:
        from .better_engine.model import now_iso
        flag = shared_dir / "better-engine" / ".refresh-urgent"
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(json.dumps({"source": source, "reason": reason,
                                    "ts": now_iso(), **kwargs}))
    except Exception:
        pass  # never let this fail the main operation


def _write_health_cache(report: "HealthReport", shared_dir: Path) -> None:
    """Write health check results to the Better Engine cache file (§2.3)."""
    import tempfile
    from datetime import datetime, timezone

    cache_dir = shared_dir / "better-engine" / "cache"
    cache_path = cache_dir / "health-latest.json"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        data = report.as_dict()
        data["generated_at"] = datetime.now(timezone.utc).isoformat()
        fd, tmp = tempfile.mkstemp(dir="/tmp", prefix="evolve-health-cache-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            import shutil as _shutil
            _shutil.copy2(tmp, str(cache_path))
            os.chmod(cache_path, 0o644)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception:
        pass  # cache write is best-effort

# ── Result types ──────────────────────────────────────────────────────────────

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


@dataclass
class CheckResult:
    status: str           # PASS | WARN | FAIL
    category: str         # logical grouping for display
    name: str             # short identifier
    detail: str           # human-readable explanation
    fix_cmd: str | None = None   # human-readable description of the fix (display only)
    fix_args: dict | None = None
    # When True the corresponding Signal is dismissed/snoozed in the store.
    # Dismissed checks are still present in report.checks (for auditability)
    # but excluded from report.failed/warned counts so the Pod Health surface
    # agrees with the Alerts page after an operator dismisses an alert there.
    dismissed: bool = False
    # Structured fix descriptor used by apply_all_fixes().  Actions:
    #   {"action": "chmod",              "path": str, "mode": str}
    #   {"action": "mkdir_chmod",        "path": str, "mode": str}
    #   {"action": "launchctl_kickstart","label": str}
    #   {"action": "launchctl_bootstrap","label": str}
    #   {"action": "setup_shared"}
    #   {"action": "deploy_bot",         "bot_id": str}   # heavy — redeploy full bot


@dataclass
class HealthReport:
    checks: list[CheckResult] = field(default_factory=list)
    network_id: str = "unknown"
    shared_dir: Path = DEFAULT_SHARED_DIR

    # ── Convenience counts ────────────────────────────────────────────────────

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == PASS)

    @property
    def warned(self) -> int:
        return sum(1 for c in self.checks if c.status == WARN and not c.dismissed)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c.status == FAIL and not c.dismissed)

    @property
    def ok(self) -> bool:
        """True when there are no FAILures (warnings are acceptable)."""
        return self.failed == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "network_id": self.network_id,
            "shared_dir": str(self.shared_dir),
            # Platform-aware section labels so the browser renders Linux
            # vocabulary on a Linux pod (instead of its own macOS map).
            "category_labels": category_labels(),
            "summary": {
                "pass": self.passed,
                "warn": self.warned,
                "fail": self.failed,
                "ok": self.ok,
            },
            "checks": [
                {
                    "status": c.status,
                    "category": c.category,
                    "name": c.name,
                    "detail": c.detail,
                    "fix_cmd": c.fix_cmd,
                    "fix_args": c.fix_args,
                    "dismissed": c.dismissed,
                }
                for c in self.checks
            ],
        }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _mode_str(mode: int) -> str:
    """Format a permission mode as octal string, e.g. '1777' or '755'."""
    return oct(mode)[2:]  # strip '0o' prefix


def category_labels() -> dict[str, str]:
    """Platform-aware section labels for health-check categories.

    Single source of truth for both ``print_report`` (Rich/CLI) and the
    ``/api/pod-health`` JSON the admin SPA renders — so the section heading
    can't drift between the two surfaces (the drift that left the browser
    showing "macOS User Accounts" on a Linux pod). macOS keeps its exact
    pre-W10-F wording (byte-identical); Linux gets neutral / systemd labels.
    """
    linux = get_profile().name == "linux"
    return {
        "network":    "Network Config",
        "runtime":    "Runtime Environment",
        "users":      "User Accounts" if linux else "macOS User Accounts",
        "oc_config":  "OpenClaw Instances",
        "plugin":     "Evolve Plugin",
        "shared_dir": "Shared Directory",
        "turns_dirs": "Bot Turns Directories",
        "launchd":    "systemd Services" if linux else "LaunchD Services",
        "security":   "File & Process Security",
        "gateways":   "Gateways",
    }


def _stat_mode(path: Path) -> int | None:
    """Return the permission bits for path, or None if stat fails."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _owner_name(path: Path) -> str | None:
    """Return the username that owns path, or None on error."""
    try:
        uid = path.stat().st_uid
        return pwd.getpwuid(uid).pw_name
    except (OSError, KeyError):
        return None


def _user_exists(username: str) -> bool:
    """Return True if a macOS user account exists (isolation-seam probe)."""
    from .runtime.isolation import get_isolation
    return get_isolation().user_exists(username)


def _launchd_loaded(label: str) -> bool | None:
    """
    Return True if the launchd service is loaded, False if not, None if we
    can't determine (e.g. insufficient privileges).
    """
    from .runtime import get_scheduler

    s = get_scheduler().status(label)
    if s.get("managed"):
        return True
    # status_error carries the tri-state (PR #2675): when it is non-None
    # ("cannot_escalate" / "error"), managed=False means "couldn't probe",
    # not "absent" — keep tooling failures distinguishable from findings.
    if s.get("status_error") in ("cannot_escalate", "error"):
        return None
    return False


def _http_ok(url: str, timeout: int = 3) -> bool:
    """Return True if the URL responds with a 2xx or 4xx HTTP status."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False


# ── Individual check groups ───────────────────────────────────────────────────

def _check_network_config(
    report: HealthReport,
    network_path: Path,
) -> dict[str, Any]:
    """Load and validate network.json; return the parsed config (or {})."""
    cat = "network"
    if not network_path.exists():
        report.checks.append(CheckResult(
            FAIL, cat, "network.json",
            f"Not found: {network_path}",
            fix_cmd=f"sudo evolve-admin setup --fresh",
        ))
        return {}

    try:
        network = load_network(network_path)
    except Exception as e:
        report.checks.append(CheckResult(
            FAIL, cat, "network.json",
            f"Parse error: {e}",
            fix_cmd=f"# Inspect and repair: {network_path}",
        ))
        return {}

    report.checks.append(CheckResult(
        PASS, cat, "network.json",
        f"Loaded — networkId={network.get('networkId', '?')}, "
        f"members={network.get('members', [])}",
    ))
    return network


def _check_users(report: HealthReport, members: list[str], network: dict | None = None) -> None:
    """Verify that the evolve system user and each bot user account exist."""
    cat = "users"
    network = network or {}
    # Account vocabulary tracks the host. macOS keeps "macOS user 'X'"
    # byte-identical (pre-W10-F wording); Linux drops the macOS noun so an
    # Ubuntu pod's health detail doesn't read "macOS user 'evo'".
    user_noun = "user account" if get_profile().name == "linux" else "macOS user"

    for special in ("evolve",):
        if _user_exists(special):
            report.checks.append(CheckResult(PASS, cat, f"user:{special}", f"{user_noun} '{special}' exists"))
        else:
            report.checks.append(CheckResult(
                FAIL, cat, f"user:{special}",
                f"{user_noun} '{special}' not found",
                fix_cmd="sudo evolve-admin setup evolve-user",
            ))

    for bot_id in members:
        actual_user = get_bot_user(bot_id, network)
        if _user_exists(actual_user):
            detail = (
                f"{user_noun} '{actual_user}' exists (bot '{bot_id}')"
                if actual_user != bot_id
                else f"{user_noun} '{bot_id}' exists"
            )
            report.checks.append(CheckResult(PASS, cat, f"user:{bot_id}", detail))
        else:
            detail = (
                f"{user_noun} '{actual_user}' not found (bot '{bot_id}' maps to account '{actual_user}')"
                if actual_user != bot_id
                else f"{user_noun} '{bot_id}' not found"
            )
            report.checks.append(CheckResult(
                FAIL, cat, f"user:{bot_id}",
                detail,
                fix_cmd=f"sudo evolve-admin setup --fresh   # or add {actual_user} manually",
            ))


def _oc_host_account() -> str:
    """The account hosting the pod's primary OpenClaw instance — platform-keyed.

    macOS: the ``evolve`` infrastructure user IS the gateway account, so it
    carries an openclaw.json + gateway port the member bots don't. Linux: the
    dedicated primary runs on ``evo`` and ``evolve`` is a service-only account
    with NO openclaw.json (only cron/logs/workspace). Hardcoding ``evolve``
    made a Linux health scan FAIL "Cannot read /home/evolve/.openclaw/
    openclaw.json" and WARN "No port configured for evolve" (round-3 W10-F
    #D/#E). Mirrors setup_wizard's ``primary_account`` so health checks the
    account that actually has the instance."""
    return "evo" if get_profile().name == "linux" else "evolve"


def _check_oc_instances(
    report: HealthReport,
    members: list[str],
    phase: str = "post",
    network: dict | None = None,
) -> None:
    """Check ~/.openclaw/openclaw.json for each bot user."""
    cat = "oc_config"
    for bot_id in members:
        oc_dir = _bot_home(bot_id) / ".openclaw"
        oc_json = oc_dir / "openclaw.json"

        if not oc_dir.exists():
            report.checks.append(CheckResult(
                FAIL, cat, f"{bot_id}:oc_dir",
                f"OC directory missing: {oc_dir}",
                fix_cmd=f"sudo evolve-admin deploy --bot {bot_id}",
            ))
            continue

        # Try reading as current user; fall back to sudo /usr/bin/cat (as root,
        # matching the /etc/sudoers.d/evolve grant written by _write_evolve_sudoers).
        content: str | None = None
        try:
            content = oc_json.read_text()
        except (PermissionError, OSError):
            result = subprocess.run(
                ["sudo", "-n", "/bin/cat", str(oc_json)],
                capture_output=True, text=True, cwd=get_profile().scratch_dir,
            )
            if result.returncode == 0:
                content = result.stdout

        if content is None:
            report.checks.append(CheckResult(
                FAIL, cat, f"{bot_id}:openclaw.json",
                f"Cannot read {oc_json} (missing or permission denied)",
                fix_cmd=f"sudo evolve-admin deploy --bot {bot_id}",
            ))
            continue

        try:
            cfg = json.loads(content)
        except json.JSONDecodeError as e:
            report.checks.append(CheckResult(
                FAIL, cat, f"{bot_id}:openclaw.json",
                f"Invalid JSON in {oc_json}: {e}",
                fix_cmd=f"sudo evolve-admin deploy --bot {bot_id}",
            ))
            continue

        report.checks.append(CheckResult(
            PASS, cat, f"{bot_id}:openclaw.json",
            f"Valid — model={cfg.get('agents', {}).get('main', {}).get('defaults', {}).get('model', '?')}",
        ))

        # Check plugin dist/ is installed and root-owned (post-deploy only)
        if phase == "post":
            port = get_bot_port(bot_id, network) if network is not None else None
            _check_plugin_install(report, bot_id, oc_dir, port=port)


def _check_plugin_install(
    report: HealthReport,
    bot_id: str,
    oc_dir: Path,
    port: int | None = None,
) -> None:
    """Verify the Evolve plugin is installed, root-owned, and actually loaded.

    'openclaw plugins install -l' loads directly from the source tree rather
    than copying to ~/.openclaw/plugins/, so we accept either location:
      1. Per-bot copy/symlink: ~/.openclaw/plugins/evolve-plugin/dist/
      2. Shared source build:  <repo>/packages/plugin/dist/  (local install)

    When ``port`` is provided, also probe http://127.0.0.1:{port}/evolve/status
    to verify the gateway has actually loaded the plugin. On-disk presence is
    necessary but not sufficient: a missing runtime dependency, a node-version
    mismatch, or an OC ownership-check rejection will all leave the file
    artifacts present while the plugin fails to register at startup. Without
    this probe the regression mode is silent — the substrate-health watchdog
    eventually fires plugin-missing, but only after up to one cadence cycle.
    Doctor runs this check in seconds.
    """
    cat = "plugin"
    plugin_dist = oc_dir / "plugins" / "evolve-plugin" / "dist"
    install_dist = PLUGIN_INSTALL_DIR / "dist"
    src_dist = PLUGIN_SRC_DIR / "dist"

    if not plugin_dist.exists():
        if install_dist.exists():
            # Standard local install (-l /Users/Shared/evolve-plugin) — root-owned.
            plugin_dist = install_dist
        elif src_dist.exists():
            # Fallback: plugin loads from shared source tree (admin-owned).
            plugin_dist = src_dist
        else:
            report.checks.append(CheckResult(
                WARN, cat, f"{bot_id}:plugin_dist",
                f"Plugin dist/ not found at {plugin_dist} or {src_dist} — "
                f"run: sudo evolve-admin deploy --bot {bot_id}",
                fix_cmd=f"sudo evolve-admin deploy --bot {bot_id}",
            ))
            return

    owner = _owner_name(plugin_dist)
    if owner != "root":
        report.checks.append(CheckResult(
            WARN, cat, f"{bot_id}:plugin_owner",
            f"Plugin dist/ owned by '{owner}' (expected root) — security requirement",
            fix_cmd=f"sudo chown -R root:wheel {plugin_dist}",
        ))
    else:
        report.checks.append(CheckResult(
            PASS, cat, f"{bot_id}:plugin",
            f"Plugin installed and root-owned at {plugin_dist}",
        ))

    # Verify dist/ is not world-writable — a world-writable plugin dir would allow
    # bot users to overwrite plugin code that runs with elevated trust
    mode = _stat_mode(plugin_dist)
    if mode is not None and (mode & 0o002):
        report.checks.append(CheckResult(
            WARN, cat, f"{bot_id}:plugin_mode",
            f"Plugin dist/ mode={_mode_str(mode)} is world-writable — "
            f"bot users could overwrite plugin code",
            fix_cmd=f"sudo chmod 755 {plugin_dist}",
            fix_args={"action": "chmod", "path": str(plugin_dist), "mode": "755"},
        ))

    # Runtime liveness: the gateway must actually have loaded the plugin.
    # On-disk presence + correct ownership is necessary but not sufficient.
    # Tier=off bots intentionally short-circuit register() before the HTTP
    # routes attach, so /evolve/status returns the OC SPA fallback (200 HTML).
    # Skip the liveness probe in that case — the plugin is "loaded" in the
    # sense the user asked for: present on disk, opted out at runtime.
    if port is not None:
        bot_tier = _read_bot_tier(oc_dir)
        if bot_tier == "off":
            report.checks.append(CheckResult(
                PASS, cat, f"{bot_id}:plugin_loaded",
                f"Plugin tier=off — runtime hooks intentionally disabled (port {port} probe skipped)",
            ))
        else:
            loaded, detail = _probe_evolve_plugin_loaded(port)
            if loaded:
                report.checks.append(CheckResult(
                    PASS, cat, f"{bot_id}:plugin_loaded",
                    f"Plugin responding on port {port} ({detail})",
                ))
            else:
                report.checks.append(CheckResult(
                    WARN, cat, f"{bot_id}:plugin_loaded",
                    f"Plugin not loaded on gateway (port {port}): {detail} — "
                    f"check {oc_dir}/logs/gateway.err.log for load errors",
                    fix_cmd=f"sudo evolve-admin deploy --bot {bot_id}",
                ))


def _read_bot_tier(oc_dir: Path) -> str:
    """Read the per-bot Evolve integration tier from openclaw.json. Returns
    "full" on any error or missing field — same default the plugin applies."""
    try:
        cfg = json.loads((oc_dir / "openclaw.json").read_text())
        tier = cfg.get("plugins", {}).get("entries", {}).get("evolve", {}).get("config", {}).get("tier")
        if tier in ("off", "monitor", "manage", "full"):
            return tier
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return "full"


def _probe_evolve_plugin_loaded(port: int) -> tuple[bool, str]:
    """Probe a bot gateway and decide whether the evolve plugin is loaded.

    Mirrors the parser in analyzer/metrics/resolvers/plugin.py — keep them in
    sync. Returns (loaded, detail). ``detail`` is short and suitable for
    inclusion in a CheckResult message.

    The gateway returns its OpenClaw control-UI HTML (HTTP 200) for any
    unmatched path, so a pure 200 check would false-positive. The evolve
    plugin's status route serves JSON containing one of bot_id / plugin_version
    / status — those keys are sufficient evidence the plugin handler ran.
    """
    url = f"http://127.0.0.1:{port}/evolve/status"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"probe failed: {e}"

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return False, "response not JSON (likely SPA fallback — plugin not registered)"
    if not isinstance(data, dict):
        return False, f"response not a JSON object (got {type(data).__name__})"
    plugin_keys = ("bot_id", "plugin_version", "status")
    matched = [k for k in plugin_keys if k in data]
    if not matched:
        return False, f"response lacks any of {plugin_keys!r}"
    version = data.get("plugin_version") or "unknown"
    return True, f"version {version}"


def _check_shared_dir(report: HealthReport, shared_dir: Path) -> None:
    """Verify shared directory structure, ownership, and permissions."""
    cat = "shared_dir"

    # Root of shared dir: must exist with mode 1777
    if not shared_dir.exists():
        report.checks.append(CheckResult(
            FAIL, cat, "shared_dir:root",
            f"Shared directory not found: {shared_dir}",
            fix_cmd="sudo evolve-admin setup-shared",
            fix_args={"action": "setup_shared"},
        ))
        return

    mode = _stat_mode(shared_dir)
    owner = _owner_name(shared_dir)
    if mode != 0o1777:
        report.checks.append(CheckResult(
            FAIL, cat, "shared_dir:mode",
            f"{shared_dir} mode={_mode_str(mode or 0)} (expected 1777)",
            fix_cmd=f"sudo chmod 1777 {shared_dir}",
            fix_args={"action": "chmod", "path": str(shared_dir), "mode": "1777"},
        ))
    else:
        report.checks.append(CheckResult(
            PASS, cat, "shared_dir:mode",
            f"{shared_dir} mode=1777 owner={owner}",
        ))

    if owner not in ("evolve", "root"):
        report.checks.append(CheckResult(
            WARN, cat, "shared_dir:owner",
            f"{shared_dir} owned by '{owner}' (expected evolve or root)",
            fix_cmd=f"sudo chown evolve:wheel {shared_dir}",
            fix_args={"action": "setup_shared"},
        ))

    # Standard subdirs: must exist and be readable
    for subdir in ("metrics", "proposals/pending", "proposals/approved",
                   "scoreboard", "logs", "kaizen", "applications"):
        p = shared_dir / subdir
        if not p.exists():
            report.checks.append(CheckResult(
                WARN, cat, f"subdir:{subdir}",
                f"Expected subdir missing: {p}",
                fix_cmd="sudo evolve-admin setup-shared",
                fix_args={"action": "setup_shared"},
            ))
        else:
            report.checks.append(CheckResult(PASS, cat, f"subdir:{subdir}", f"{p} exists"))


def _check_bot_turns_dirs(report: HealthReport, members: list[str], shared_dir: Path) -> None:
    """Verify per-bot turns directories exist with mode 1777 (sticky world-writable)."""
    cat = "turns_dirs"
    for bot_id in members:
        turns = shared_dir / bot_id / "turns"
        if not turns.exists():
            report.checks.append(CheckResult(
                WARN, cat, f"{bot_id}:turns",
                f"Turns dir missing: {turns}",
                fix_cmd=f"sudo mkdir -p {turns} && sudo chmod 1777 {turns}",
                fix_args={"action": "mkdir_chmod", "path": str(turns), "mode": "1777"},
            ))
            continue
        mode = _stat_mode(turns)
        if mode != 0o1777:
            report.checks.append(CheckResult(
                FAIL, cat, f"{bot_id}:turns",
                f"{turns} mode={_mode_str(mode or 0)} (expected 1777)",
                fix_cmd=f"sudo chmod 1777 {turns}",
                fix_args={"action": "chmod", "path": str(turns), "mode": "1777"},
            ))
        else:
            report.checks.append(CheckResult(PASS, cat, f"{bot_id}:turns", f"{turns} mode=1777"))


_TEMPLATE_PLACEHOLDERS = ("BOT_USER", "WORKSPACE_PATH")
_EXPECTED_PLIST_BINARIES = frozenset({
    "/usr/bin/python3",
    "/usr/local/bin/python3",
    VENV_PYTHON,
    VENV_EVOLVE_ADMIN,            # admin-ui LaunchDaemon
    "/opt/homebrew/bin/openclaw", # evolve-gateway (Apple Silicon homebrew)
    "/usr/local/bin/openclaw",    # evolve-gateway (Intel homebrew)
    "/opt/homebrew/bin/node",          # evolve-gateway via node (Apple Silicon, brew-linked)
    "/usr/local/bin/node",             # evolve-gateway via node (Intel, brew-linked)
    "/opt/homebrew/opt/node/bin/node", # evolve-gateway via node (Apple Silicon, keg/realpath)
    "/bin/bash",                  # jittered daemons: bash -c "sleep ...; exec <real binary>"
})


def _validate_plist_content(plist_path: Path) -> tuple[bool, str]:
    """
    Verify an installed plist has no unsubstituted template placeholders
    and a recognised binary in ProgramArguments[0].
    Returns (ok, error_message).
    """
    import plistlib
    try:
        text = plist_path.read_text()
    except OSError as e:
        return False, f"Unreadable: {e}"

    for placeholder in _TEMPLATE_PLACEHOLDERS:
        if placeholder in text:
            return False, f"Unsubstituted placeholder '{placeholder}' — plist was not fully rendered"

    try:
        data = plistlib.loads(text.encode())
    except Exception as e:
        return False, f"XML parse error: {e}"

    args = data.get("ProgramArguments", [])
    if not args:
        return False, "ProgramArguments missing or empty"

    binary = args[0]
    if binary not in _EXPECTED_PLIST_BINARIES:
        # Also accept any node binary under Homebrew — the Cellar path changes
        # with every brew upgrade so we can't enumerate it in the static set.
        import os as _os
        _name = _os.path.basename(binary)
        _is_homebrew_node = (
            _name == "node" and (
                binary.startswith("/opt/homebrew/")
                or binary.startswith("/usr/local/")
            )
        )
        if not _is_homebrew_node:
            return False, f"Unexpected binary in ProgramArguments: '{binary}'"

    return True, ""


def _check_file_security(
    report: HealthReport,
    members: list[str],
    network_path: Path,
) -> None:
    """
    Security-focused permission and ownership checks.

    Verifies that config files, bot home directories, and the shared venv are
    not writable by unexpected users.  A world-writable network.json or
    openclaw.json would let any local user hijack bot behaviour without going
    through the normal deploy path.
    """
    cat = "security"

    # 1. network.json — not group/world-writable; owned by root or evolve
    if network_path.exists():
        mode = _stat_mode(network_path)
        owner = _owner_name(network_path)
        if mode is not None and (mode & 0o022):
            report.checks.append(CheckResult(
                FAIL, cat, "network.json:perms",
                f"network.json mode={_mode_str(mode)} is group/world-writable — "
                f"a compromised process could redirect bots",
                fix_cmd=f"sudo chmod 644 {network_path}",
                fix_args={"action": "chmod", "path": str(network_path), "mode": "644"},
            ))
        elif owner not in ("root", "evolve"):
            report.checks.append(CheckResult(
                WARN, cat, "network.json:owner",
                f"network.json owned by '{owner}' (expected root or evolve)",
                fix_cmd=f"sudo chown evolve:wheel {network_path}",
            ))
        else:
            report.checks.append(CheckResult(
                PASS, cat, "network.json:perms",
                f"network.json mode={_mode_str(mode or 0)} owner={owner}",
            ))

    # 2. Per-bot openclaw.json — not world-writable; owned by bot user or root
    network = load_network()
    from .config import get_bot_user
    for bot_id in members:
        bot_user = get_bot_user(bot_id, network)
        oc_json = _bot_home(bot_id, network) / ".openclaw" / "openclaw.json"
        if not oc_json.exists():
            continue  # absence already flagged in _check_oc_instances
        mode = _stat_mode(oc_json)
        owner = _owner_name(oc_json)
        if mode is not None and (mode & 0o002):
            report.checks.append(CheckResult(
                FAIL, cat, f"{bot_id}:openclaw.json:perms",
                f"openclaw.json mode={_mode_str(mode)} is world-writable — "
                f"any local user could inject bot configuration",
                fix_cmd=f"sudo chmod 640 {oc_json}",
                fix_args={"action": "chmod", "path": str(oc_json), "mode": "640"},
            ))
        elif owner not in (bot_user, "root"):
            report.checks.append(CheckResult(
                WARN, cat, f"{bot_id}:openclaw.json:owner",
                f"openclaw.json owned by '{owner}' (expected {bot_user} or root)",
            ))
        else:
            report.checks.append(CheckResult(
                PASS, cat, f"{bot_id}:openclaw.json:perms",
                f"openclaw.json mode={_mode_str(mode or 0)} owner={owner}",
            ))

    # 3. Bot home directories — not world-writable
    for bot_id in members:
        home = _bot_home(bot_id, network)
        if not home.exists():
            continue  # absence already flagged in _check_users
        mode = _stat_mode(home)
        if mode is not None and (mode & 0o002):
            report.checks.append(CheckResult(
                FAIL, cat, f"{bot_id}:home_dir:perms",
                f"{home} mode={_mode_str(mode)} is world-writable — "
                f"allows any local user to plant files in the bot's home",
                fix_cmd=f"sudo chmod 755 {home}",
                fix_args={"action": "chmod", "path": str(home), "mode": "755"},
            ))
        else:
            report.checks.append(CheckResult(
                PASS, cat, f"{bot_id}:home_dir:perms",
                f"{home} mode={_mode_str(mode or 0)}",
            ))

    # 4. Venv root directory — owned by root or evolve.
    # We check the venv root dir (not the python3 binary) because the python3
    # binary inside the venv is usually a symlink that chains to the Homebrew
    # Python binary, which may be owned by the admin user.  stat() follows the
    # chain all the way to the real binary, so checking it would flag a false
    # positive every run.  The root directory is never a symlink and accurately
    # reflects who controls the venv.
    venv_python = Path(VENV_PYTHON)
    venv_root = venv_python.parent.parent  # /Users/Shared/evolve-venv
    if venv_python.exists():
        owner = _owner_name(venv_root)
        mode = _stat_mode(venv_python)
        if mode is not None and (mode & 0o002):
            report.checks.append(CheckResult(
                FAIL, cat, "venv:python:perms",
                f"Venv Python mode={_mode_str(mode)} is world-writable — "
                f"a bot user could replace the interpreter",
                fix_cmd=f"sudo chmod 755 {venv_python}",
                fix_args={"action": "chmod", "path": str(venv_python), "mode": "755"},
            ))
        elif owner not in ("root", "evolve"):
            report.checks.append(CheckResult(
                WARN, cat, "venv:python:owner",
                f"Venv owned by '{owner}' (expected root or evolve)",
                fix_cmd=f"sudo chown -R root:wheel {venv_root}",
                fix_args={"action": "chown", "path": str(venv_root),
                          "owner": "root:wheel", "recursive": True},
            ))
        else:
            report.checks.append(CheckResult(
                PASS, cat, "venv:python",
                f"Venv owned by '{owner}' mode={_mode_str(mode or 0)}",
            ))


# Per-bot launchd jobs that deploy_bot installs (one per network member).
# Anything outside this set is a pod-wide infra job installed by
# install_evolve_infra_jobs — for those, `evolve-admin deploy --all` is a
# no-op and the right remediation is `evolve-admin install-infra-jobs`.
# ``apply.`` (retired 2026-08-18) and ``test.`` (retired 2026-06-08) are
# listed but currently UNREACHABLE, and that is the honest statement — an
# earlier version of this comment justified them by a live remediation path
# that does not exist.
#
# This tuple is NOT an expected-daemon set (that is
# ``deploy.per_bot_evolve_plist_labels``, which both labels were dropped
# from). Its only consumer is ``_plist_fix_cmd``, whose only two call sites
# sit inside the ``for label in sorted(expected)`` loop below, where
# ``expected`` is ``deploy.expected_plist_labels`` — per-bot labels, the
# iMessage poller, template installs and manifest-scheduled app daemons.
# None of those is expected to yield an ``ai.openclaw.evolve.apply.*`` or
# ``…test.*`` label, so in practice the two prefixes go unmatched. Note
# this is not a hard guarantee: ``template_installed_labels`` surfaces
# manifest-authored labels, which are arbitrary strings. A stale unit
# that survives on disk is handled by the orphan-sweeper, not by this map.
#
# They are kept anyway because the cost is two strings and the failure mode
# of removing them is asymmetric: if a future caller ever hands
# ``_plist_fix_cmd`` a label from outside ``expected`` (an orphan report, a
# drift Signal), a retired per-bot label must still route to
# ``deploy --bot <bot_id>``, which is the path that runs
# ``_bootout_retired_per_bot_jobs``. Falling through would suggest
# ``install-infra-jobs``, which does nothing for a per-bot unit. Retire them
# only together with that fallback.
_PER_BOT_PLIST_PREFIXES = (
    "ai.openclaw.evolve.apply.",
    "ai.openclaw.evolve.test.",
    "ai.openclaw.evolve.cost-converter.",
)


def _launchd_label_bot_id(label: str, members) -> str | None:
    """Best-effort attribution of a launchd plist label to a bot.

    Used only for Signal scoping + remediation hints — never for control
    flow. Returns the owning bot id for a known per-bot daemon, else None
    for pod-wide infra (and for any label we can't confidently place — an
    honest pod scope beats a bogus bot row, e.g. the 2026-06-11 "launchd"
    bot rows on every ``pod_health_launchd`` Signal).

    Recognises:
      - per-bot gateway        ``ai.openclaw.<bot>-gateway``
      - per-bot Evolve daemons ``per_bot_evolve_plist_labels(<bot>)``
        (apply / cost-converter / audit-runner[-t3] / doctor-pass / backup)
      - per-bot app daemons    ``ai.evolve.<bot>.<app>`` (PR #2164 path)
    Pod-wide infra (``ai.evolve.evolve.*`` / ``ai.openclaw.evolve.*``)
    returns None. The primary bot's own per-bot daemons — including its
    gateway (``ai.openclaw.evo-gateway`` on an evo-primary pod, the legacy
    ``ai.openclaw.evolve-gateway`` on an evolve-primary pod) — resolve to
    its id here via the ``per_bot_gateway_plist_label`` match below; the
    caller folds primary → pod scope, keeping that policy in one place.
    """
    members = list(members or [])
    # Canonical per-bot Evolve daemons (single source of truth in deploy.py).
    # per_bot_* build label strings from the bot id and never raise.
    for m in members:
        if label == per_bot_gateway_plist_label(m) or label in per_bot_evolve_plist_labels(m):
            return m
    # Per-bot app daemons install at ai.evolve.<bot>.<app>; attribute when
    # the bot segment is a known non-primary member.
    if label.startswith("ai.evolve."):
        parts = label.split(".")
        if len(parts) >= 4 and parts[2] in set(members) and parts[2] != "evolve":
            return parts[2]
    return None


def _plist_fix_cmd(label: str, members=None) -> str:
    """Right remediation command for a failing launchd plist, by label.

    - Per-bot jobs → `evolve-admin deploy --bot <bot_id>` (deploy_bot path)
    - Pod-wide infra → `evolve-admin install-infra-jobs` (rewrites the plist
      and re-bootstraps all infra daemons including admin-ui, which clears
      false-positive content failures from a stale validator).
    """
    for prefix in _PER_BOT_PLIST_PREFIXES:
        if label.startswith(prefix):
            bot_id = label[len(prefix):]
            return f"sudo evolve-admin deploy --bot {bot_id}"
    # Other per-bot daemons (gateway, audit-runner, doctor-pass, backup, and
    # per-bot app daemons at ai.evolve.<bot>.<app>) also redeploy via the bot
    # path; only genuine pod-wide infra wants install-infra-jobs.
    bot_id = _launchd_label_bot_id(label, members or [])
    if bot_id:
        return f"sudo evolve-admin deploy --bot {bot_id}"
    return "sudo evolve-admin install-infra-jobs"


def _check_launchd(report: HealthReport, network: dict) -> None:
    """Check that expected launchd plists are installed and services are loaded."""
    cat = "launchd"
    # realized_only=True: alert only for plists that were ACTUALLY realized on
    # disk (stamped ``installed_artifact``), not for spec-hydrated-but-never-
    # materialized scheduled actions. The orphan-sweeper still uses the broad
    # default set — see the ``realized_only`` skip in expected_plist_labels'
    # scheduled-actions fallback for why the two consumers diverge.
    expected = expected_plist_labels(network, realized_only=True)
    members = network.get("members", []) if isinstance(network, dict) else []
    install_json = Path(
        network.get("sharedDir", str(DEFAULT_SHARED_DIR))
    ) / "install.json"

    missing_plists: list[str] = []
    unloaded: list[str] = []
    # Labels intentionally absent because their feature is off (e.g.
    # upstream-issues-watcher on a standard-profile install). These stay in
    # expected_plist_labels() so the orphan-sweeper doesn't delete them when
    # the feature flips off mid-uptime; the check skips the FAIL here to
    # avoid misreporting intentional absence as a broken install.
    gated_off_skipped: list[str] = []

    # Platform-keyed "installed on disk" probe. macOS stats the LaunchDaemon
    # plist (cheap, byte-identical to pre-W10-F); Linux routes through the
    # Scheduler seam's status() — its `installed` is the .service unit file on
    # disk under /etc/systemd/system. Before W10-F this stat'd
    # /Library/LaunchDaemons/<label>.plist unconditionally, so on Linux EVERY
    # label was "missing" → 73 false "Plist not found" FAILs + a bogus
    # "0/73 plists present — Evolve not deployed" though the fleet was running
    # as systemd units.
    _prof = get_profile()
    _is_linux = _prof.name == "linux"
    if _is_linux:
        from .runtime import get_scheduler
        _sched = get_scheduler()

    for label in sorted(expected):
        if _is_linux:
            installed = bool(_sched.status(label).get("installed"))
            plist = None
        else:
            plist = LAUNCHD_DIR / f"{label}.plist"
            installed = plist.exists()
        if not installed:
            if is_label_feature_gated_off(label, install_json):
                gated_off_skipped.append(label)
                continue
            missing_plists.append(label)
            continue

        # Validate plist content: no unrendered placeholders, known binary.
        # launchd-plist-specific — systemd validates its own unit syntax at
        # load (a malformed unit surfaces as managed=False / unloaded below),
        # so there is no Linux analog to walk here.
        if plist is not None:
            content_ok, content_err = _validate_plist_content(plist)
            if not content_ok:
                report.checks.append(CheckResult(
                    FAIL, cat, f"launchd:{label}:content",
                    f"Plist content invalid — {content_err}",
                    fix_cmd=_plist_fix_cmd(label, members),
                ))

        loaded = _launchd_loaded(label)
        if loaded is False:
            unloaded.append(label)
        elif loaded is None:
            # Can't determine — note but don't fail
            report.checks.append(CheckResult(
                WARN, cat, f"launchd:{label}",
                f"Plist exists but service status unknown (run with sudo to check)",
            ))
        # loaded=True → no individual PASS to keep output concise

    if missing_plists:
        # Cross-reference: the repo-puller daemon auto-installs new infra
        # plists on every pull that touches deploy.py (PR #920). When it
        # silently unloads, "Plist not found" warnings cascade for every
        # subsequent new-daemon PR until an operator runs install-infra-jobs
        # by hand. Hint at the root cause so the operator restarts the
        # puller first instead of repeatedly chasing individual plists.
        if _is_linux:
            puller_hint = (
                " — likely root cause: repo-puller daemon is unloaded (the "
                "auto-installer for new units is not running). Restart it "
                "first: sudo /usr/bin/systemctl restart "
                "ai.evolve.evolve.repo-puller.service"
                if _puller_log_is_stale()
                else ""
            )
        else:
            puller_hint = (
                " — likely root cause: repo-puller daemon is unloaded (the "
                "auto-installer for new plists is not running). Restart it "
                "first: sudo /bin/launchctl bootstrap system "
                f"{LAUNCHD_DIR}/ai.evolve.evolve.repo-puller.plist"
                if _puller_log_is_stale()
                else ""
            )
        for label in missing_plists:
            if _is_linux:
                not_found = (
                    f"systemd unit not found: {_prof.daemon_dir}/{label}.service"
                    f"{puller_hint}"
                )
            else:
                not_found = f"Plist not found: {LAUNCHD_DIR}/{label}.plist{puller_hint}"
            report.checks.append(CheckResult(
                FAIL, cat, f"launchd:{label}",
                not_found,
                fix_cmd=_plist_fix_cmd(label, members),
            ))
    if unloaded:
        # Resolve the primary's gateway port via primary_bot_id — it lives on
        # the primary's bots[] entry now; the legacy top-level `evolve` block
        # is honoured as a fallback for pre-S1 pods (evo-account-separation S1).
        try:
            from primary_bot import primary_bot_gateway_port  # type: ignore
            evolve_port = primary_bot_gateway_port(network)
        except Exception:
            evolve_port = network.get("evolve", {}).get("gateway_port")

        def _bootstrap_fix(label: str) -> str:
            # Platform-keyed remediation: systemctl on Linux, launchctl on macOS.
            if _is_linux:
                return f"sudo /usr/bin/systemctl restart {label}.service"
            return f"sudo launchctl bootstrap system {LAUNCHD_DIR}/{label}.plist"

        manager = "systemd" if _is_linux else "launchd"
        # Resolve the primary gateway label from the primary id (evo-gateway on
        # an evo-primary pod, evolve-gateway on a legacy evolve-primary pod) —
        # a hardcoded literal would skip the softening on every evo-primary pod.
        from primary_bot import primary_bot_id as _pbid  # type: ignore
        # Fail-closed: when the primary is unresolvable, do NOT fall back to the
        # literal "evolve" — that label would only match a phantom
        # ai.openclaw.evolve-gateway (EVO-GATEWAY-RESIDUE-RERENDER), softening
        # exactly the unit we want to flag. ``None`` here yields a sentinel that
        # matches no real unit, so every unloaded gateway is reported normally.
        # Legacy pods with a real "evolve" bot still resolve via primary_bot_id's
        # own "evolve" fallback, so their softening is unchanged.
        _primary_id = _pbid(network)
        primary_gw_label = (
            per_bot_gateway_plist_label(_primary_id) if _primary_id else None
        )
        for label in unloaded:
            # For the primary bot's gateway, check if it's actually responding
            # before marking as FAIL — it may be running from a prior session
            # even if the service manager failed to register.  Running-but-
            # unmanaged is a WARN (won't auto-restart on reboot) not a hard FAIL.
            if label == primary_gw_label and evolve_port:
                if _http_ok(f"http://127.0.0.1:{evolve_port}/health"):
                    report.checks.append(CheckResult(
                        WARN, cat, f"launchd:{label}",
                        f"Gateway responding on :{evolve_port} but not registered in {manager} "
                        f"— will not auto-restart on reboot",
                        fix_cmd=_bootstrap_fix(label),
                        fix_args={"action": "launchctl_bootstrap", "label": label},
                    ))
                    continue
            report.checks.append(CheckResult(
                FAIL, cat, f"launchd:{label}",
                f"Service not loaded in {manager}",
                fix_cmd=_bootstrap_fix(label),
                fix_args={"action": "launchctl_bootstrap", "label": label},
            ))

    # Feature-gated-off labels aren't applicable to this install, so they
    # shouldn't inflate the denominator in the "X/Y plists present" summary.
    applicable = len(expected) - len(gated_off_skipped)
    installed_count = applicable - len(missing_plists)
    gated_suffix = (
        f" ({len(gated_off_skipped)} feature-gated, off)"
        if gated_off_skipped else ""
    )
    if not missing_plists and not unloaded:
        report.checks.append(CheckResult(
            PASS, cat, "launchd:all",
            f"All {applicable} expected plists installed and loaded{gated_suffix}",
        ))
    else:
        report.checks.append(CheckResult(
            PASS, cat, "launchd:installed",
            f"{installed_count}/{applicable} plists present{gated_suffix}",
        ) if installed_count > 0 else CheckResult(
            FAIL, cat, "launchd:installed",
            f"0/{applicable} plists present — Evolve not deployed",
            fix_cmd="sudo evolve-admin deploy --all",
            fix_args={"action": "deploy_all"},
        ))


def _puller_kickstart_fix_cmd() -> str:
    """Platform-keyed restart hint for the repo-puller daemon (W10-F #8a):
    systemctl on Linux, launchctl kickstart on macOS."""
    if get_profile().name == "linux":
        return "sudo /usr/bin/systemctl restart ai.evolve.evolve.repo-puller.service"
    return "sudo launchctl kickstart -k system/ai.evolve.evolve.repo-puller"


def _puller_log_is_stale(
    log_path: "Path | None" = None,
    stale_after_seconds: int = 90 * 60,
) -> bool:
    """Quick puller-freshness probe used to enrich launchd FAIL messages
    with the likely root cause. Returns True iff the puller log exists
    and is older than ``stale_after_seconds``. Anything else (missing
    file, OSError) returns False — we only want to add the hint when
    we're confident the puller is the cause, not when we can't tell.

    Default path resolves from the platform-keyed ``DEFAULT_SHARED_DIR``
    (W10-F #8a): a literal ``/Users/Shared/evolve/...`` default never
    matched the Linux puller log at ``/var/lib/evolve/logs/`` → the hint
    silently never fired on a Linux pod."""
    if log_path is None:
        log_path = DEFAULT_SHARED_DIR / "logs" / "repo-puller.log"
    import time as _time
    try:
        return (_time.time() - log_path.stat().st_mtime) > stale_after_seconds
    except OSError:
        return False


def _check_repo_puller_freshness(
    report: HealthReport,
    log_path: "Path | None" = None,
    stale_after_seconds: int = 90 * 60,
) -> None:
    """Surface repo-puller wedge state in the standard health output.

    The puller files its own dedup'd `puller-stuck` incident record on
    every failed tick (see ``repo_puller.file_or_update_puller_stuck_issue``;
    records land in ``{shared_dir}/repo-puller/incidents/``), but those
    records aren't visible during a routine ``evolve-admin health`` run.
    Between
    2026-04-29 (puller installed) and 2026-05-04 (manual rescue), the
    daemon failed silently with ``Permission denied (publickey)`` for
    days because no operator-visible signal fired.

    Three states surface here, derived from the daemon's stdout log:
      - log missing → WARN (puller never ran or wrong path)
      - file mtime older than ``stale_after_seconds`` → WARN (daemon
        unloaded; launchd should be writing every 15min)
      - tail contains ``[repo-puller] ERROR:`` and no recent success
        marker → FAIL (auth wedged or non-fast-forward)

    Healthy state emits a quiet PASS, matching the launchd:all
    convention.
    """
    cat = "repo_puller"
    # W10-F #8a: resolve from the platform-keyed shared dir when the caller
    # didn't pass an explicit path (the launchd FAIL-message callers don't).
    if log_path is None:
        log_path = DEFAULT_SHARED_DIR / "logs" / "repo-puller.log"
    if not log_path.exists():
        report.checks.append(CheckResult(
            WARN, cat, "repo-puller:log",
            f"Log file missing: {log_path} — daemon may not have run yet",
            fix_cmd=_puller_kickstart_fix_cmd(),
            fix_args={"action": "launchctl_kickstart",
                      "label": "ai.evolve.evolve.repo-puller"},
        ))
        return

    import time as _time
    try:
        mtime = log_path.stat().st_mtime
    except OSError as e:
        report.checks.append(CheckResult(
            WARN, cat, "repo-puller:log",
            f"Could not stat {log_path}: {e}",
        ))
        return

    age = _time.time() - mtime
    if age > stale_after_seconds:
        report.checks.append(CheckResult(
            WARN, cat, "repo-puller:stale",
            f"Log not written in {int(age // 60)}min "
            f"(daemon ticks every 15min) — likely unloaded",
            fix_cmd=_puller_kickstart_fix_cmd(),
            fix_args={"action": "launchctl_kickstart",
                      "label": "ai.evolve.evolve.repo-puller"},
        ))
        return

    # Read the tail (4 KB is plenty — each tick writes at most a few
    # hundred bytes, so 4 KB covers ~16 ticks = 4 hours of history).
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError as e:
        report.checks.append(CheckResult(
            WARN, cat, "repo-puller:log",
            f"Could not read {log_path}: {e}",
        ))
        return

    lines = [l for l in tail.splitlines() if l.strip()]
    if not lines:
        report.checks.append(CheckResult(
            WARN, cat, "repo-puller:empty",
            f"Log {log_path} is empty — daemon hasn't written anything",
        ))
        return

    last_error: str | None = None
    has_recent_success = False
    for line in lines[-10:]:
        if "[repo-puller] ERROR:" in line:
            last_error = line.strip()
        if "[repo-puller] stashes=" in line or "[repo-puller] advanced" in line:
            has_recent_success = True

    if last_error and not has_recent_success:
        try:
            from .config import load_network, resolve_pod_context, resolve_repo_url, DEFAULT_NETWORK_CONFIG
            _net = load_network(DEFAULT_NETWORK_CONFIG)
            _pod = resolve_pod_context(_net)
            _ssh_prefix = _pod.get("ssh_prefix", "")
            _repo_url = resolve_repo_url(_net) or "your repo"
        except Exception:
            _ssh_prefix, _repo_url = "", "your repo"
        report.checks.append(CheckResult(
            FAIL, cat, "repo-puller:wedged",
            f"No successful pull in recent log; last error: {last_error}. "
            f"The deploy box's code is drifting from origin/main — every "
            f"PR merge needs manual `{_ssh_prefix}git pull` until resolved.",
            fix_cmd=(
                "sudo evolve-admin repo-pull --setup-key  "
                f"# then add the printed key to {_repo_url}/settings/keys"
            ),
        ))
        return

    report.checks.append(CheckResult(
        PASS, cat, "repo-puller:freshness",
        f"recent successful pull (log mtime {int(age // 60)}min ago)",
    ))


def _check_repo_ownership(
    report: HealthReport,
    repo_root: Path = Path(get_profile().deploy_checkout_default),
) -> None:
    """Surface evolve-repo plugin-candidate dirs not owned by evolve.

    OC 2026.4.29's plugin scanner rejects any plugin candidate dir whose
    owner uid != evolve(or root) and silently drops the plugin's tools.
    This is exactly how the 2026-05-05 defer-tool drop happened: a fresh
    clone landed under the human admin user (uid 501), the repo-puller
    (running as evolve) updated file CONTENT but not directory ownership,
    so packages/plugin stayed admin-owned and the next gateway restart
    quietly registered zero evolve tools.

    A WARN here lets the operator see the drift before a gateway restart
    triggers it. Fix: chown the deploy checkout to ``evolve:staff`` (the
    WARN's ``fix_cmd`` renders the exact platform-correct command —
    ``/usr/sbin/chown`` on macOS, ``/usr/bin/chown`` on Linux — and the
    deploy-checkout path for the running pod; also wired into
    deploy_shared_dir for fresh setups). Pattern matches
    ``_check_repo_puller_freshness`` — quiet PASS in the healthy case,
    single WARN otherwise.
    """
    cat = "repo_ownership"
    if not repo_root.exists():
        # No deploy checkout on this host — nothing to check (e.g. CI/dev).
        return

    pkgs_dir = repo_root / "packages"
    if not pkgs_dir.is_dir():
        report.checks.append(CheckResult(
            WARN, cat, "evolve-repo:packages-missing",
            f"{pkgs_dir} not found — repo layout unexpected",
        ))
        return

    bad: list[tuple[str, str]] = []
    try:
        candidates = sorted(p for p in pkgs_dir.iterdir() if p.is_dir())
    except OSError as e:
        report.checks.append(CheckResult(
            WARN, cat, "evolve-repo:packages-unreadable",
            f"Could not list {pkgs_dir}: {e}",
        ))
        return

    for sub in candidates:
        owner = _owner_name(sub)
        if owner is None:
            continue
        if owner not in ("evolve", "root"):
            bad.append((sub.name, owner))

    if bad:
        joined = ", ".join(f"{name} (uid={owner})" for name, owner in bad[:5])
        more = f" +{len(bad) - 5} more" if len(bad) > 5 else ""
        report.checks.append(CheckResult(
            WARN, cat, "evolve-repo:ownership",
            f"{len(bad)} plugin-candidate dir(s) under {pkgs_dir} not owned by "
            f"evolve|root: {joined}{more}. Gateway plugin scanner will "
            f"silently reject these on next restart.",
            fix_cmd=f"sudo {get_profile().chown} -R evolve:staff {repo_root}",
            fix_args={"action": "chown", "path": str(repo_root),
                      "owner": "evolve:staff", "recursive": True},
        ))
        return

    report.checks.append(CheckResult(
        PASS, cat, "evolve-repo:ownership",
        f"all {len(candidates)} dirs under {pkgs_dir} owned by evolve|root",
    ))


def _check_gateways(
    report: HealthReport,
    members: list[str],
    network: dict,
    *,
    retry_secs: int = 0,
) -> None:
    """HTTP-probe each gateway; check admin server.

    retry_secs: if >0, retry each failing gateway probe for up to this many
    seconds before reporting FAIL.  Used by the mid-wizard check to give
    freshly-restarted gateways time to come up.
    """
    import time

    cat = "gateways"
    for bot_id in members:
        port = get_bot_port(bot_id, network)
        if not port:
            report.checks.append(CheckResult(
                WARN, cat, f"{bot_id}:gateway",
                f"No port configured for {bot_id} in network.json",
                fix_cmd=f"sudo evolve-admin deploy {bot_id}",
                fix_args={"action": "deploy_bot", "bot_id": bot_id},
            ))
            continue
        url = f"http://127.0.0.1:{port}/evolve/status"
        ok = _http_ok(url)
        if not ok and retry_secs > 0:
            deadline = time.monotonic() + retry_secs
            while not ok and time.monotonic() < deadline:
                time.sleep(1)
                ok = _http_ok(url)
        if ok:
            report.checks.append(CheckResult(PASS, cat, f"{bot_id}:gateway", f"Responding on port {port}"))
        else:
            label = f"ai.openclaw.{bot_id}-gateway"
            report.checks.append(CheckResult(
                FAIL, cat, f"{bot_id}:gateway",
                f"No response on port {port} ({url})",
                fix_cmd=f"sudo launchctl kickstart -k system/{label}",
                fix_args={"action": "launchctl_kickstart", "label": label},
            ))
            # Flag urgent refresh — gateway has transitioned to FAIL
            _flag_urgent_refresh(
                report.shared_dir, source="health_check",
                reason="gateway_fail", bot_id=bot_id,
            )

    # Admin UI
    if _http_ok("http://127.0.0.1:5050/api/health", timeout=2):
        report.checks.append(CheckResult(PASS, cat, "admin_ui", "Admin UI responding on port 5050"))
    else:
        admin_label = "ai.evolve.evolve.admin-ui"
        report.checks.append(CheckResult(
            WARN, cat, "admin_ui",
            "Admin UI not responding on port 5050",
            fix_cmd=f"sudo launchctl bootstrap system {LAUNCHD_DIR}/{admin_label}.plist",
            fix_args={"action": "launchctl_bootstrap", "label": admin_label},
        ))


def _check_gateway_supervision(
    report: HealthReport, network: dict, shared_dir: Path,
) -> None:
    """Label-agnostic gateway crash-loop + port-collision net.

    The liveness probe in ``_check_gateways`` only sees gateways listed in
    network.json by port — it is blind to a crash-looping *orphan* unit and to
    two units fighting over one port. This walks every ``ai.openclaw.*-gateway``
    unit the service manager actually knows about (via the Scheduler seam, no
    launchd/systemd branch here) and emits its own Signal types so the failure
    mode that once burned ~2266 silent restarts can never go unalerted again.

    Findings become FAIL CheckResults under two new categories →
    ``pod_health_gateway_crashloop`` / ``pod_health_gateway_port_collision``
    Signal types. They are NOT category ``"gateways"``, so the flap-suppression
    state machine (which keys on that category) leaves them untouched; and they
    come from the live unit list, not the realized/expected-plist set, so the
    #2973 realized-only narrowing cannot suppress them. ``sweep_resolve`` on
    those types (see ``run_pod_health_signals_only``) auto-archives a finding
    the moment the gateway recovers.
    """
    try:
        import gateway_supervision
        from .runtime import get_scheduler
    except Exception as exc:  # pragma: no cover — import wiring only
        log.debug("gateway-supervision import failed: %s", exc)
        return
    try:
        findings = gateway_supervision.evaluate(
            get_scheduler(),
            shared_dir,
            config=gateway_supervision.config_from_network(network),
        )
    except Exception as exc:
        log.debug("gateway-supervision evaluate failed: %s", exc)
        return
    for f in findings:
        category = (
            "gateway_crashloop" if f.kind == gateway_supervision.CRASHLOOP
            else "gateway_port_collision"
        )
        report.checks.append(CheckResult(FAIL, category, f.scope_key, f.detail))


# ── Runtime environment ───────────────────────────────────────────────────────

# Packages that must be installed in the venv for evolve to function correctly.
# Add entries here as new features introduce new runtime dependencies.
_VENV_REQUIRED_PACKAGES: list[tuple[str, str]] = [
    # (import_name, pip_install_name)
    ("flask",             "flask"),             # web server — core dependency
    ("psutil",            "psutil"),            # host-health metrics (Overview → Host strip)
    # PWA Phase 3 (#1372): embedded mini terminal. Without these the
    # Terminal page's WebSocket upgrade falls through to Werkzeug's
    # default 400 handler, presenting as "disconnected (code 1006)" in
    # the UI with no other clue what went wrong. The deploy mechanism
    # is git-pull-only — new pyproject.toml deps don't auto-install —
    # so we surface the gap as a health check instead.
    ("flask_sock",        "flask-sock"),        # WS bridge for /api/terminal/ws
    ("simple_websocket",  "simple-websocket"),  # underlying WS transport
]

# System binaries the admin server or analyzer depend on.  These are checked by
# PATH lookup; the setup wizard validates Node/npm separately at install time so
# we focus on what must be present at runtime.
_REQUIRED_BINARIES: list[tuple[str, str]] = [
    # (display_name, binary_name)
    ("git",       "git"),
    ("openclaw",  "openclaw"),
]

# Derived from the platform profile via VENV_PYTHON (…/bin/python3 → …/bin/pip)
# so the fix hint prints the right path on Linux too, not a hardcoded /Users/….
_VENV_PIP = Path(VENV_PYTHON).parent / "pip"


def _check_runtime_environment(report: HealthReport, shared_dir: Path) -> None:
    """Check that the Python venv and required packages are healthy."""
    import shutil
    cat = "runtime"

    # ── 1. Venv Python exists ────────────────────────────────────────────────
    venv_python = Path(VENV_PYTHON)
    # VENV_PYTHON may be a generic "python3" symlink — resolve to real path
    venv_python_real = Path(str(venv_python).replace("python3", "python3") )
    if not venv_python_real.exists():
        # Try glob for any versioned python3.x in the same bin dir
        candidates = list(venv_python_real.parent.glob("python3*"))
        venv_python_real = candidates[0] if candidates else venv_python_real

    if not venv_python_real.exists():
        report.checks.append(CheckResult(
            FAIL, cat, "venv:python",
            f"Venv Python not found at {VENV_PYTHON} — venv may need to be recreated",
            fix_cmd="sudo python3 -m venv /Users/Shared/evolve-venv && "
                    "sudo /Users/Shared/evolve-venv/bin/pip install -e "
                    "/Users/Shared/evolve-repo/packages/admin",
        ))
        return  # remaining checks depend on Python existing
    report.checks.append(CheckResult(
        PASS, cat, "venv:python",
        f"Venv Python: {venv_python_real}",
    ))

    # ── 1b. Interpreter exec-able by the service accounts ────────────────────
    # The venv python is a symlink chain to a base interpreter. If any hop
    # resolves under a directory without o+x (the classic: uv's managed
    # CPython in /root/.local/share/uv, because the bootstrap `uv sync` ran
    # via sudo), every non-root daemon dies at exec with status=203/EXEC
    # before a line of Python runs — the wizard passes, the pod is dead.
    from .installer import _world_exec_ok
    if not _world_exec_ok(venv_python_real):
        real = Path(os.path.realpath(venv_python_real))
        venv_root = venv_python.parent.parent
        checkout = get_profile().deploy_checkout_default
        report.checks.append(CheckResult(
            FAIL, cat, "venv:python:exec",
            f"Venv interpreter resolves to {real}, which service accounts "
            f"cannot execute (a path component lacks o+x) — non-root units "
            f"fail with status=203/EXEC. Rebuild the venv against a "
            f"world-readable interpreter (e.g. /usr/bin/python3).",
            fix_cmd=f"sudo /usr/bin/python3 -m venv --clear {venv_root} && "
                    f"sudo {venv_root}/bin/pip install --config-settings "
                    f"editable_mode=compat -e {checkout}/packages/analyzer "
                    f"-e {checkout}/packages/admin",
        ))
    else:
        report.checks.append(CheckResult(
            PASS, cat, "venv:python:exec",
            "Venv interpreter chain is executable by service accounts",
        ))

    # ── 2. Required packages ─────────────────────────────────────────────────
    for import_name, pip_name in _VENV_REQUIRED_PACKAGES:
        check_name = f"venv:pkg:{pip_name}"
        r = subprocess.run(
            [str(venv_python_real), "-c", f"import {import_name}"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            report.checks.append(CheckResult(
                FAIL, cat, check_name,
                f"Package '{pip_name}' not installed in venv",
                fix_cmd=f"sudo {_VENV_PIP} install {pip_name}",
                fix_args={"action": "pip_install", "package": pip_name},
            ))
        else:
            # Get version for display
            ver_r = subprocess.run(
                [str(venv_python_real), "-c",
                 f"import importlib.metadata; print(importlib.metadata.version('{pip_name}'))"],
                capture_output=True, text=True,
            )
            ver = ver_r.stdout.strip() or "?"
            report.checks.append(CheckResult(PASS, cat, check_name,
                                             f"{pip_name} {ver} installed"))

    # ── 3. System binaries ───────────────────────────────────────────────────
    search_path = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
    for display, binary in _REQUIRED_BINARIES:
        check_name = f"runtime:bin:{binary}"
        found = (shutil.which(binary) or
                 shutil.which(binary, path=search_path))
        if found:
            report.checks.append(CheckResult(PASS, cat, check_name,
                                             f"{display}: {found}"))
        else:
            report.checks.append(CheckResult(
                WARN, cat, check_name,
                f"'{binary}' not found on PATH — some features may fail",
                fix_cmd=f"Ensure '{binary}' is installed and on the evolve user's PATH",
            ))

    # ── 4. docs/system runtime files ────────────────────────────────────────
    conduct_path = shared_dir / "POD_CONDUCT.md"
    if conduct_path.exists():
        report.checks.append(CheckResult(PASS, cat, "runtime:pod_conduct",
                                         f"POD_CONDUCT.md present at {conduct_path}"))
    else:
        report.checks.append(CheckResult(
            FAIL, cat, "runtime:pod_conduct",
            f"POD_CONDUCT.md missing from {shared_dir} — bots will use fallback conduct rules",
            fix_cmd="evolve-admin setup  # re-runs _write_pod_conduct",
            fix_args={"action": "write_pod_conduct", "shared_dir": str(shared_dir)},
        ))


# ── Public API ────────────────────────────────────────────────────────────────

def _check_bot_auth_provisioning(
    report: HealthReport, members: list[str], network: dict | None = None
) -> None:
    """Verify each bot's OpenClaw auth store is provisioned (the #3136 class).

    A bot whose ``auth-profiles.json`` carries a stored provider key but whose
    per-agent sqlite store never received it boots credential-less — every model
    dispatch fails ``Missing API key``. This mirrors that into a per-bot
    ``bot_auth`` CheckResult, which ``_emit_health_signals`` turns into a
    ``pod_health_bot_auth`` Signal (scope=bot), so the daily/refresh scan catches
    a mute bot the way it catches gateway faults.

    The verdict comes from :func:`oc_auth_provision.audit_bot_auth`, which gates
    on "is this a key-auth bot?" so a gateway-auth bot is never flagged, and
    returns ``"unknown"`` on a transient CLI failure (mapped to WARN, not FAIL,
    so the Signal does not flap on a one-off read error). Best-effort: a per-bot
    exception is logged and skipped, never failing the whole scan.
    """
    cat = "bot_auth"
    try:
        from .oc_auth_provision import audit_bot_auth
    except Exception as exc:  # pragma: no cover - defensive import guard
        log.debug("bot-auth check skipped (import failed): %s", exc)
        return
    for bot_id in members:
        try:
            bot_user = get_bot_user(bot_id, network or {})
        except Exception:
            bot_user = bot_id
        try:
            verdict, detail = audit_bot_auth(bot_id, bot_user, _bot_home(bot_id, network))
        except Exception as exc:  # pragma: no cover - audit_bot_auth never raises
            log.debug("bot-auth check raised for %s: %s", bot_id, exc)
            continue
        name = f"{bot_id}:auth"
        if verdict == "missing":
            report.checks.append(CheckResult(
                FAIL, cat, name,
                f"OpenClaw auth store unprovisioned — {detail}",
                fix_cmd=f"sudo evolve-admin deploy --bot {bot_id}",
                fix_args={"action": "deploy_bot", "bot_id": bot_id},
            ))
        elif verdict == "unknown":
            report.checks.append(CheckResult(
                WARN, cat, name,
                f"Could not verify auth provisioning — {detail}",
            ))
        else:  # "ok"
            report.checks.append(CheckResult(PASS, cat, name, detail))


def run_health_check(
    network_path: Path = DEFAULT_NETWORK_CONFIG,
    members_override: list[str] | None = None,
    phase: str = "post",
) -> HealthReport:
    """
    Run health checks and return a HealthReport.

    Parameters
    ----------
    network_path:
        Path to network.json.  Defaults to /Users/Shared/evolve/network.json.
    members_override:
        If provided, use this bot list instead of reading from network.json.
        Useful for wizard verification before network.json is fully written.
    phase:
        ``"pre"``  — pre-install checks only (1–5): network config, user accounts,
                     OC instances, shared directory, turns directories.  Skips
                     launchd, file-security, and gateway checks that cannot pass
                     before the first deploy has run.
        ``"mid"``  — mid-wizard check (after bot deploy, before infra plists).
                     Runs gateway liveness only (with a short retry window for
                     freshly-restarted gateways).  Skips launchd and file-security
                     checks because infra plists are not installed until Step 13.
        ``"post"`` — all checks (1–8), including launchd plists, file-security,
                     and gateway liveness.  Default; used for the final wizard
                     scan, ``evolve-admin health``, and ongoing monitoring.
    """
    report = HealthReport()

    # 1. Network config
    network = _check_network_config(report, network_path)
    members: list[str] = members_override or network.get("members", [])
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))
    report.network_id = network.get("networkId", "unknown")
    report.shared_dir = shared_dir

    if not members:
        report.checks.append(CheckResult(
            WARN, "network", "members",
            "No bot members found in network.json — run 'evolve-admin setup' first",
        ))
        # Still check shared dir even with no members
        _check_shared_dir(report, shared_dir)
        return report

    # The pod's primary OC instance runs on the gateway account, which hosts an
    # openclaw.json + gateway port the member bots don't. That account is
    # platform-keyed: on macOS it's the `evolve` infrastructure user; on Linux
    # the gateway/primary is `evo` and `evolve` is a service-only account with
    # NO openclaw.json (it has only cron/logs/workspace). Hardcoding "evolve"
    # made a Linux scan FAIL "Cannot read /home/evolve/.openclaw/openclaw.json"
    # and WARN "No port configured for evolve" (round-3 #D/#E). Mirror
    # setup_wizard's primary_account so we check the account that actually has
    # the instance. _check_users handles the account via its own special loop;
    # we only extend the list for checks that were previously missing it.
    host_account = _oc_host_account()
    oc_members = members + (
        [host_account] if host_account not in members else []
    )

    # 2. Runtime environment — venv, required packages, system binaries, system docs
    _check_runtime_environment(report, shared_dir)

    # 3. macOS user accounts
    _check_users(report, members, network=network)

    # 3. OpenClaw instance configs (includes plugin check) — includes evolve
    _check_oc_instances(report, oc_members, phase=phase, network=network)

    # 4. Shared directory structure
    _check_shared_dir(report, shared_dir)

    # 5. Per-bot turns directories — includes evolve
    _check_bot_turns_dirs(report, oc_members, shared_dir)

    if phase == "pre":
        # Pre-install: stop here — launchd/security/gateway checks are not
        # meaningful before the first deploy has run.
        return report

    if phase == "mid":
        # Mid-wizard (after bot deploy, before Step 13 installs infra plists):
        # only probe gateways.  Skip launchd and file-security — infra plists
        # haven't been installed yet so _check_launchd would produce false FAILs.
        # Retry up to 45 s (matching deploy._wait_for_gateway_port's bind
        # timeout): /evolve/status only answers once the OC plugin has finished
        # loading — ~10-25 s after the port binds, longer on a cold VPS. A 5 s
        # window false-failed a perfectly healthy install and showed the operator
        # a scary "No response on port … Continue anyway?" prompt while the
        # gateway was simply still starting. The retry loop returns the instant
        # the gateway responds, so this adds no delay to a healthy run; the final
        # health scan stays authoritative for all gateways.
        _check_gateways(report, oc_members, network, retry_secs=45)
        return report

    # 6. Launchd plists + service state
    if network:
        _check_launchd(report, network)

    # 6b. repo-puller freshness (catches multi-day silent auth wedges)
    _check_repo_puller_freshness(report)

    # 6c. repo ownership (catches plugin-candidate dirs the OC scanner will reject)
    _check_repo_ownership(report)

    # 7. Security: file permissions, ownership, and plist integrity — includes evolve
    _check_file_security(report, oc_members, network_path)

    # 8. Gateway + admin UI liveness — includes evolve
    _check_gateways(report, oc_members, network)

    # 8b. Per-bot auth provisioning — catches a credential-less bot (#3136 class)
    # the way gateways catch liveness faults. Full-path only (NOT the 1-min fast
    # path): auth state is stable and the check spawns one `openclaw models list`
    # per key-auth bot.
    _check_bot_auth_provisioning(report, members, network)

    # 9. Persist manifest so other scripts can resolve file paths without re-scanning
    if network:  # skip if network.json failed to load — manifest would be garbage
        _write_manifest(report, oc_members, network, shared_dir)

    # 10. Write Better Engine cache file (best-effort side-effect)
    _write_health_cache(report, shared_dir)

    # 11. Mirror failing checks into the Signal store. Phase 5 of the
    # alerts/signal-store consolidation
    # (docs/spec-alerts-signal-store-2026-05-07.md). Best-effort.
    try:
        _primary_for_signals: str | None = None
        try:
            from primary_bot import primary_bot_id as _pbid  # type: ignore
            _primary_for_signals = _pbid(network or {})
        except Exception:
            pass
        _emit_health_signals(
            report, shared_dir,
            members=oc_members,
            primary_bot_id=_primary_for_signals,
        )
    except Exception:
        pass

    # 12. Cross-reference dismissed/snoozed Signals so Pod Health and Alerts
    # agree: a dismissed alert on the Alerts page suppresses the matching check
    # here. Best-effort — if the signal store is unavailable the report still
    # returns (without dismissal decorations).
    try:
        _apply_signal_dismissals(report, shared_dir)
    except Exception as exc:
        log.debug("pod-health dismissal cross-reference skipped: %s", exc)

    return report


def run_pod_health_signals_only(
    network_path: Path = DEFAULT_NETWORK_CONFIG,
    *,
    pre_emit_filter=None,
) -> HealthReport:
    """Liveness-only pod_health run for high-cadence (1-min) cron.

    Runs the gateway probe and the repo-puller freshness check; emits
    Signals for FAIL/WARN states and sweep-resolves any previously-firing
    pod_health_gateways / pod_health_repo_puller Signal that came back
    PASS this tick. Other pod_health categories (launchd state,
    file_security, repo_ownership, runtime_environment) are deliberately
    not in this fast path — they live in the full ``run_health_check``
    that fires from the admin UI Refresh button, the setup wizard, and
    ``evolve-admin doctor``.

    The repo-puller is in the fast path because it is itself the
    auto-installer for new launchd plists (see _paths_touch_infra_install
    in repo_puller.py): if it silently unloads, every new-daemon PR after
    that point leaves its plist un-installed until an operator notices
    the downstream "Plist not found" warnings. One stat() per minute is
    a cheap price for a transition-detectable Signal.

    Phase 0a of the alert-notifier spec
    (docs/spec-alert-notifier-2026-05-09.md) — gives the alert notifier
    a transition signal to fire on without needing a comprehensive scan
    every minute.
    """
    report = HealthReport()
    network = _check_network_config(report, network_path)
    members: list[str] = network.get("members", [])
    shared_dir = Path(network.get("sharedDir", str(DEFAULT_SHARED_DIR)))
    report.network_id = network.get("networkId", "unknown")
    report.shared_dir = shared_dir

    if not members:
        return report

    # Resolve the primary bot id (with legacy "evolve" fallback) for the
    # gateway probe sweep + signal scoping.
    _primary_for_health: str | None = None
    try:
        from primary_bot import primary_bot_id as _pbid  # type: ignore
        _primary_for_health = _pbid(network or {})
    except Exception:
        pass
    # Add the primary to the gateway-probe sweep when it resolves and isn't
    # already a member. When no primary resolves (degenerate network), don't
    # inject the literal "evolve" — that would probe a gateway that doesn't
    # exist on a pod whose primary is "evo".
    oc_members = list(members)
    if _primary_for_health and _primary_for_health not in oc_members:
        oc_members.append(_primary_for_health)
    _check_gateways(report, oc_members, network)
    _check_gateway_supervision(report, network, shared_dir)
    _check_repo_puller_freshness(
        report, log_path=shared_dir / "logs" / "repo-puller.log"
    )

    # Optional pre-emit hook — used by pod_health_runner to apply the
    # gateway state machine so single-tick FAILs don't churn Signal
    # state for flaps. Other callers (admin UI Refresh, doctor) pass
    # no filter; their checks emit Signals immediately as before.
    if pre_emit_filter is not None:
        try:
            pre_emit_filter(report, network)
        except Exception as e:
            print(f"[pod_health] pre_emit_filter raised, continuing: {e}",
                  flush=True)

    try:
        _emit_health_signals(
            report,
            shared_dir,
            members=oc_members,
            primary_bot_id=_primary_for_health,
            sweep_types={
                "pod_health_gateways",
                "pod_health_repo_puller",
                "pod_health_gateway_crashloop",
                "pod_health_gateway_port_collision",
            },
        )
    except Exception:
        pass

    return report


def _emit_health_signals(
    report: HealthReport,
    shared_dir: Path,
    *,
    members: list[str] | None = None,
    sweep_types: set[str] | None = None,
    primary_bot_id: str | None = None,
) -> None:
    """Mirror FAIL / WARN checks into the Signal store + sweep_resolve.

    One Signal per (category, name) pair — sweep at end clears any
    pod_health Signal whose check came back PASS this run.

    FAIL → severity=alert, flavor=maintenance.
    WARN → severity=warn, flavor=activity (it's "watch this" not
    "fix this now").

    ``sweep_types``: when set, the trailing sweep_resolve only touches
    Signals whose ``type`` is in this set. Used by partial-coverage
    runners (e.g. the 1-min liveness-only pod_health cron) so they don't
    accidentally auto-resolve categories they didn't actually re-check.
    Default ``None`` sweeps every pod_health Signal — correct for full
    ``run_health_check`` which examines every category.
    """
    import importlib
    try:
        signals_store = importlib.import_module("signals.store")
        make_signature = importlib.import_module("schema.signal").make_signature
    except Exception:
        return

    kept: set[str] = set()
    for check in report.checks:
        if check.status == PASS:
            continue
        sig_type = f"pod_health_{check.category}"
        # name is structured like "user:admin_bot" / "subdir:proposals" /
        # "admin_bot:turns" — keep it as the scope_key so each check has
        # its own Signal slot.
        signature = make_signature("pod_health", sig_type, check.name)
        kept.add(signature)
        flavor = "maintenance" if check.status == FAIL else "activity"
        severity = "alert" if check.status == FAIL else "warn"

        # Bot scoping: parse "<bot_id>:<rest>" or "user:<bot_id>" patterns
        bot_id: str | None = None
        if check.category == "launchd":
            # The launchd check names every row "launchd:<plist_label>"
            # (optionally a trailing ":content"); here the head is the
            # CATEGORY word, not a bot. The generic head-as-bot parse below
            # therefore stamped a bogus "launchd" bot on every launchd
            # Signal (2026-06-11). Attribute to the real owning bot from the
            # plist label instead; pod-wide infra and unattributable labels
            # stay pod-scoped.
            if ":" in check.name:
                label = check.name.split(":", 1)[1].split(":", 1)[0]
                bot_id = _launchd_label_bot_id(label, members)
        elif ":" in check.name:
            head, rest = check.name.split(":", 1)
            if head == "user":
                bot_id = rest
            else:
                bot_id = head
        # The primary bot's "bot" scope check categorises as pod-scope —
        # admin/daemon-fleet findings cover the whole pod, not the bot row.
        # Compare against the RESOLVED primary id, never the literal "evolve"
        # (which would fail to fold an "evo"-primary pod's findings into pod
        # scope). When primary is unresolved, every real bot id keeps "bot"
        # scope, which is the safe default on a degenerate network.
        scope = "bot" if bot_id and bot_id != primary_bot_id else "pod"
        if scope == "pod":
            bot_id = None

        try:
            signals_store.observe(
                shared_dir,
                signature=signature,
                producer="pod_health",
                type=sig_type,
                flavor=flavor,
                severity=severity,
                scope=scope,
                bot_id=bot_id,
                title=f"{check.category}: {check.name}",
                body=check.detail,
                details={
                    "category": check.category,
                    "name": check.name,
                    "status": check.status,
                    "fix_cmd": check.fix_cmd,
                    "fix_args": check.fix_args,
                },
            )
        except Exception:
            continue

    try:
        signals_store.sweep_resolve(
            shared_dir,
            producer="pod_health",
            kept_signatures=kept,
            reason="auto-resolve: check returned PASS",
            types=sweep_types,
        )
    except Exception:
        pass


def _dismissed_pod_health_signatures(shared_dir: Path) -> frozenset[str]:
    """Return signatures of pod_health Signals that are currently dismissed or snoozed."""
    import importlib
    try:
        signals_store = importlib.import_module("signals.store")
    except Exception:
        return frozenset()
    result: set[str] = set()
    try:
        for sig in signals_store.iter_signals(shared_dir, subdirs=("archived",)):
            if sig.producer == "pod_health" and sig.state == "dismissed":
                result.add(sig.signature)
        for sig in signals_store.iter_signals(shared_dir, subdirs=("snoozed",)):
            if sig.producer == "pod_health":
                result.add(sig.signature)
    except Exception as exc:
        log.debug("pod-health dismissed-signature scan failed: %s", exc)
    return frozenset(result)


def _apply_signal_dismissals(report: HealthReport, shared_dir: Path) -> None:
    """Mark checks whose pod_health Signal is dismissed or snoozed.

    Called at the end of run_health_check() so report.failed/warned counts
    exclude operator-acknowledged conditions and the Pod Health surface stays
    consistent with the Alerts page. The checks remain in report.checks with
    dismissed=True for auditability.
    """
    import importlib
    try:
        make_signature = importlib.import_module("schema.signal").make_signature
    except Exception:
        return
    suppressed = _dismissed_pod_health_signatures(shared_dir)
    if not suppressed:
        return
    for check in report.checks:
        if check.status == PASS or check.dismissed:
            continue
        signature = make_signature("pod_health", f"pod_health_{check.category}", check.name)
        if signature in suppressed:
            check.dismissed = True


def filter_dismissed_checks(data: dict[str, Any], shared_dir: Path) -> dict[str, Any]:
    """Apply live Signal-store dismissal state to a cached pod-health JSON dict.

    Marks individual checks with dismissed=true and adjusts summary fail/warn
    counts so /api/pod-health/cached stays consistent with the Alerts page even
    when a Signal is dismissed between health scans. Mutates and returns the dict.
    """
    import importlib
    try:
        make_signature = importlib.import_module("schema.signal").make_signature
    except Exception:
        return data
    suppressed = _dismissed_pod_health_signatures(shared_dir)
    if not suppressed:
        return data
    dismissed_fail = 0
    dismissed_warn = 0
    for check in data.get("checks", []):
        if check.get("status") == PASS or check.get("dismissed"):
            continue
        signature = make_signature(
            "pod_health",
            f"pod_health_{check.get('category', '')}",
            check.get("name", ""),
        )
        if signature in suppressed:
            check["dismissed"] = True
            if check.get("status") == FAIL:
                dismissed_fail += 1
            elif check.get("status") == WARN:
                dismissed_warn += 1
    if dismissed_fail or dismissed_warn:
        summary = data.get("summary", {})
        summary["fail"] = max(0, summary.get("fail", 0) - dismissed_fail)
        summary["warn"] = max(0, summary.get("warn", 0) - dismissed_warn)
        summary["ok"] = summary.get("fail", 0) == 0
    return data


def _write_manifest(
    report: HealthReport,
    members: list[str],
    network: dict,
    shared_dir: Path,
) -> None:
    """
    Write pod-manifest.json to the shared directory after a scan.

    The manifest is a stable, machine-readable record of where every important
    file lives in this install.  Other scripts (apply.py, defer_runner.py, etc.)
    read it to resolve paths instead of re-walking the filesystem each time.

    Format: see docs/spec-health-checker.md §Manifest schema.
    """
    from datetime import datetime, timezone

    bots_data: dict[str, Any] = {}
    for bot_id in members:
        oc_dir = _bot_home(bot_id) / ".openclaw"
        _plugin_dist_candidate = oc_dir / "plugins" / "evolve-plugin" / "dist"
        plugin_dist = _plugin_dist_candidate if _plugin_dist_candidate.exists() else (
            PLUGIN_INSTALL_DIR / "dist" if (PLUGIN_INSTALL_DIR / "dist").exists() else (
            PLUGIN_SRC_DIR / "dist" if (PLUGIN_SRC_DIR / "dist").exists() else _plugin_dist_candidate))
        turns = shared_dir / bot_id / "turns"

        port = get_bot_port(bot_id, network)
        bots_data[bot_id] = {
            "oc_dir": str(oc_dir),
            "oc_json": str(oc_dir / "openclaw.json"),
            "plugin_dist": str(plugin_dist) if plugin_dist.exists() else None,
            "plugin_owner": _owner_name(plugin_dist) if plugin_dist.exists() else None,
            "turns_dir": str(turns),
            "turns_mode": _mode_str(_stat_mode(turns) or 0) if turns.exists() else None,
            "gateway_port": port,
            "gateway_live": _http_ok(f"http://127.0.0.1:{port}/evolve/status") if port else False,
            "launchd_gateway": f"ai.openclaw.{bot_id}-gateway",
        }

    expected_labels = set()
    try:
        from .deploy import expected_plist_labels
        expected_labels = expected_plist_labels(network)
    except Exception:
        pass

    manifest: dict[str, Any] = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "network_id": report.network_id,
        "shared_dir": str(shared_dir),
        "summary": {
            "pass": report.passed,
            "warn": report.warned,
            "fail": report.failed,
            "ok": report.ok,
        },
        "bots": bots_data,
        "launchd": {
            "expected_count": len(expected_labels),
            "expected_labels": sorted(expected_labels),
        },
    }

    manifest_path = shared_dir / "pod-manifest.json"
    try:
        import tempfile
        fd, tmp = tempfile.mkstemp(dir="/tmp", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(manifest, f, indent=2)
            import shutil as _shutil
            _shutil.copy2(tmp, str(manifest_path))
            os.chmod(manifest_path, 0o644)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except PermissionError:
        # Shared dir owned by another user — try sudo cp
        import tempfile
        fd, tmp = tempfile.mkstemp(dir="/tmp", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(manifest, f, indent=2)
            subprocess.run(
                ["sudo", "/bin/cp", tmp, str(manifest_path)],
                capture_output=True, check=False,
            )
            subprocess.run(
                ["sudo", "/bin/chmod", "644", str(manifest_path)],
                capture_output=True, check=False,
            )
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception:
        pass  # manifest write is best-effort; never let it crash the scan


def read_manifest(shared_dir: Path = DEFAULT_SHARED_DIR) -> dict[str, Any] | None:
    """
    Read the most recent pod-manifest.json written by run_health_check().

    Returns None if the manifest doesn't exist or is unreadable.

    Usage from analyzer scripts::

        from evolve_admin.health import read_manifest
        m = read_manifest()
        if m:
            oc_json = m["bots"][bot_id]["oc_json"]
    """
    path = shared_dir / "pod-manifest.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


@dataclass
class FixResult:
    name: str
    ok: bool
    message: str


def _execute_fix(fix_args: dict, dry_run: bool = False) -> tuple[bool, str]:
    """
    Execute a single fix described by fix_args.  Returns (success, message).

    All subprocess calls go through sudo so this works correctly whether the
    caller is root (wizard) or the evolve user (web server with sudoers grants).
    deploy_all / deploy_bot actions call the deploy library directly.
    """
    action = fix_args.get("action")

    if action == "chmod":
        path, mode = fix_args["path"], fix_args["mode"]
        cmd = ["sudo", "/bin/chmod", mode, path]
        if dry_run:
            return True, f"[dry-run] would chmod {mode} {path}"
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode == 0, (r.stderr.strip() or f"chmod {mode} {path}")

    elif action == "chown":
        path, owner = fix_args["path"], fix_args["owner"]
        recursive = fix_args.get("recursive", False)
        # chown BINARY platform-keyed (W7): /usr/sbin/chown (macOS) vs
        # /usr/bin/chown (Linux); the macOS literal never matches the Linux
        # NOPASSWD grant. `owner` is supplied in fix_args by the producing check.
        cmd = ["sudo", get_profile().chown] + (["-R"] if recursive else []) + [owner, path]
        if dry_run:
            return True, f"[dry-run] would chown {owner} {path}"
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode == 0, (r.stderr.strip() or f"chown {owner} {path}")

    elif action == "mkdir_chmod":
        path, mode = fix_args["path"], fix_args["mode"]
        if dry_run:
            return True, f"[dry-run] would mkdir -p {path} && chmod {mode} {path}"
        r1 = subprocess.run(["sudo", "/bin/mkdir", "-p", path], capture_output=True, text=True)
        if r1.returncode != 0:
            return False, f"mkdir failed: {r1.stderr.strip()}"
        r2 = subprocess.run(["sudo", "/bin/chmod", mode, path], capture_output=True, text=True)
        return r2.returncode == 0, (r2.stderr.strip() or f"mkdir+chmod {mode} {path}")

    elif action == "launchctl_kickstart":
        label = fix_args["label"]
        if dry_run:
            return True, f"[dry-run] would kickstart {label}"
        from .runtime import get_scheduler
        ok, out = get_scheduler().restart(label)
        return ok, (out or f"kickstart {label}")

    elif action == "launchctl_bootstrap":
        label = fix_args["label"]
        plist = LAUNCHD_DIR / f"{label}.plist"
        if dry_run:
            return True, f"[dry-run] would bootstrap {label}"
        if not plist.exists():
            return False, f"Plist not found: {plist} — run 'sudo evolve-admin deploy --all' first"
        # raw (not Scheduler.install()): re-registers the deploy-owned plist
        # already on disk; install() owns the plist render+write and skips
        # byte-identical content — wrong for re-loading an unloaded job.
        from .runtime import get_launchd_scheduler
        rc, _out, err = get_launchd_scheduler().raw("bootstrap", "system", str(plist))
        return rc == 0, (err.strip() or f"bootstrap {label}")

    elif action == "setup_shared":
        if dry_run:
            return True, "[dry-run] would run deploy_shared_dir()"
        try:
            from .deploy import deploy_shared_dir
            result = deploy_shared_dir(DEFAULT_SHARED_DIR)
            return result.success, "; ".join(result.errors) if result.errors else "setup_shared OK"
        except Exception as e:
            return False, str(e)

    elif action == "pip_install":
        package = fix_args["package"]
        pip = str(_VENV_PIP)
        if not Path(pip).exists():
            return False, f"Venv pip not found at {pip} — venv may need to be recreated"
        cmd = ["sudo", pip, "install", package]
        if dry_run:
            return True, f"[dry-run] would run: sudo {pip} install {package}"
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return True, f"Installed {package} into venv"
        err = r.stderr.strip() or r.stdout.strip() or f"pip install {package} failed"
        return False, f"{err}\nRun manually: sudo {pip} install {package}"

    elif action == "write_pod_conduct":
        shared = Path(fix_args.get("shared_dir", str(DEFAULT_SHARED_DIR)))
        if dry_run:
            return True, f"[dry-run] would write POD_CONDUCT.md to {shared}"
        try:
            from .deploy import _read_pod_conduct_content
            content = _read_pod_conduct_content() + "\n"
            dest = shared / "POD_CONDUCT.md"
            try:
                dest.write_text(content)
            except PermissionError:
                import tempfile as _tf
                fd, tmp = _tf.mkstemp(suffix=".md")
                with os.fdopen(fd, "w") as f:
                    f.write(content)
                subprocess.run(["sudo", "/bin/cp", tmp, str(dest)], check=True, capture_output=True)
                subprocess.run(["sudo", "/bin/chmod", "644", str(dest)], capture_output=True)
                os.unlink(tmp)
            return True, f"POD_CONDUCT.md written to {dest}"
        except Exception as e:
            return False, str(e)

    elif action in ("deploy_bot", "deploy_all"):
        # Heavy operation — don't run silently.  Caller should route through
        # the deploy flow (web server: POST /api/deploy; wizard: own deploy step).
        return False, f"deploy action requires explicit approval — use 'evolve-admin deploy'"

    return False, f"Unknown fix action: {action!r}"


def apply_all_fixes(report: HealthReport, dry_run: bool = False) -> list[FixResult]:
    """
    Apply all actionable fixes in the report.  Skips passing checks and checks
    without fix_args.  Returns one FixResult per attempted fix.

    Safe to call in both execution contexts:
      - wizard (running as root via sudo evolve-admin setup --fresh)
      - web server (running as evolve user; sudoers grants cover chmod/mkdir/
        launchctl on the relevant paths — see spec-sudoers.md §7)
    """
    results: list[FixResult] = []
    for check in report.checks:
        if check.status == PASS or not check.fix_args:
            continue
        ok, msg = _execute_fix(check.fix_args, dry_run=dry_run)
        if ok and not dry_run:
            check.status = PASS
            check.detail += " [fixed]"
        results.append(FixResult(name=check.name, ok=ok, message=msg))
    return results


# ── Rich display ──────────────────────────────────────────────────────────────

def print_report(report: HealthReport, verbose: bool = False) -> None:
    """Render the health report to stdout using Rich."""
    try:
        from rich.console import Console
        from rich.table import Table
        from rich.panel import Panel
        console = Console()
    except ImportError:
        # Fallback if rich isn't available (shouldn't happen in this project)
        for c in report.checks:
            print(f"[{c.status}] {c.category}/{c.name}: {c.detail}")
        return

    # Summary panel
    status_color = "green" if report.ok else "red"
    summary = (
        f"[green]{report.passed} passed[/]  "
        f"[yellow]{report.warned} warned[/]  "
        f"[red]{report.failed} failed[/]  "
        f"  pod=[bold]{report.network_id}[/]  "
        f"evolve=v{EVOLVE_VERSION}"
    )
    console.print(Panel(summary, title="[bold]Pod Health Check[/]", border_style=status_color))

    # Group checks by category
    categories: dict[str, list[CheckResult]] = {}
    for c in report.checks:
        categories.setdefault(c.category, []).append(c)

    # W10-F: section headings track the host's service manager / account
    # vocabulary (macOS byte-identical, Linux neutral/systemd). Shared with
    # the /api/pod-health JSON so the CLI and the browser can't drift.
    CATEGORY_LABELS = category_labels()

    STATUS_ICON = {PASS: "[green]✓[/]", WARN: "[yellow]⚠[/]", FAIL: "[red]✗[/]"}

    for cat_key, checks in categories.items():
        label = CATEGORY_LABELS.get(cat_key, cat_key.replace("_", " ").title())
        # Skip categories with all-PASS when not verbose
        if not verbose and all(c.status == PASS for c in checks):
            pass_count = len(checks)
            console.print(f"  {STATUS_ICON[PASS]} [dim]{label}: {pass_count} check(s) passed[/]")
            continue

        console.print(f"\n  [bold]{label}[/]")
        for c in checks:
            if c.status == PASS and not verbose:
                continue
            icon = STATUS_ICON[c.status]
            fixable = " [dim cyan](fixable)[/]" if c.fix_args and c.status != PASS else ""
            console.print(f"    {icon} [dim]{c.name}[/] — {c.detail}{fixable}")
            if c.fix_cmd and c.status != PASS and verbose:
                console.print(f"       [dim]fix:[/] [dim cyan]{c.fix_cmd}[/]")

    unfixed = [c for c in report.checks if c.status != PASS and c.fix_args]
    unresolvable = [c for c in report.checks if c.status != PASS and not c.fix_args]
    if unfixed and not verbose:
        console.print(
            f"\n  [dim]Run [bold]evolve-admin health --fix[/] to apply "
            f"{len(unfixed)} fixable issue(s).[/]"
        )
    if unresolvable:
        console.print(
            f"\n  [yellow]{len(unresolvable)} issue(s) require manual action "
            f"(e.g. full redeploy).[/]"
        )
