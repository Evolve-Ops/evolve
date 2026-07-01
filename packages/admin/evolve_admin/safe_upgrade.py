"""
safe_upgrade.py — read-only preflight that answers "would upgrading
openclaw right now break Evolve?".

Independent gates probe the live pod state and the candidate
package metadata from the npm registry. Nothing here mutates state;
the only side effect is writing the structured report to
/Users/Shared/evolve/safe-upgrade/reports/<id>.json.

Spec: docs/spec-safe-upgrade-2026-05-02.md.

Importable by both the CLI (`evolve-admin menu safe-upgrade`) and the
admin server's HTTP routes — no click/rich/flask deps here.
"""

from __future__ import annotations

import io
import json
import os
import plistlib
import re
import sqlite3
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from evolve_util import atomic_write_json as _atomic_write_json

from .config import DEFAULT_SHARED_DIR, load_network, user_home


# ── On-disk layout ────────────────────────────────────────────────────────────

REPORTS_SUBDIR = "safe-upgrade/reports"
RETAIN_REPORTS = 20

OPENCLAW_PACKAGE_JSON = Path("/opt/homebrew/lib/node_modules/openclaw/package.json")
OPENCLAW_REGISTRY_BASE = "https://registry.npmjs.org/openclaw"

# postinstall artifact dropped by openclaw under each user
USER_AGENT_BASENAME = "ai.openclaw.gateway.plist"

# Tarball size below this is "stub-shaped" (real openclaw is multi-MB)
MIN_REASONABLE_TARBALL_BYTES = 100 * 1024


def reports_dir(shared_dir: Path | None = None) -> Path:
    """Return /Users/Shared/evolve/safe-upgrade/reports (configurable for tests)."""
    base = shared_dir or DEFAULT_SHARED_DIR
    return base / REPORTS_SUBDIR


# ── Report dataclasses ───────────────────────────────────────────────────────

@dataclass
class GateResult:
    ok: bool
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Requirement:
    id: str
    summary: str
    remediation: str
    blocking: bool
    source_gate: str


@dataclass
class Report:
    report_id: str
    checked_at: str
    duration_ms: int
    candidate: dict[str, Any]
    current: dict[str, Any]
    ok: bool
    summary: str
    gates: dict[str, GateResult]
    requirements: list[Requirement]

    def to_json(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "checked_at": self.checked_at,
            "duration_ms": self.duration_ms,
            "candidate": self.candidate,
            "current": self.current,
            "ok": self.ok,
            "summary": self.summary,
            "gates": {k: asdict(v) for k, v in self.gates.items()},
            "requirements": [asdict(r) for r in self.requirements],
        }


# ── Concurrency: single in-flight check ──────────────────────────────────────

_inflight_lock = threading.Lock()
_inflight_id: str | None = None


def inflight_report_id() -> str | None:
    """Return the report_id of the currently-running check, if any."""
    with _inflight_lock:
        return _inflight_id


# ── Helpers ──────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_report_id() -> str:
    dt = _utcnow().strftime("%Y%m%dT%H%M%SZ")
    return f"{dt}-{uuid.uuid4().hex[:8]}"


def _update_latest_symlink(reports_root: Path, report_id: str) -> None:
    """Point reports_root/latest.json at <report_id>.json."""
    link = reports_root / "latest.json"
    target_name = f"{report_id}.json"
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
    except OSError:
        pass
    try:
        os.symlink(target_name, link)
    except OSError:
        # Fall back to a regular copy if symlinks aren't permitted
        try:
            link.write_text((reports_root / target_name).read_text())
        except OSError:
            pass


def _retention_sweep(reports_root: Path, keep: int = RETAIN_REPORTS) -> None:
    try:
        files = sorted(
            (p for p in reports_root.glob("*.json") if p.name != "latest.json"),
            reverse=True,
        )
    except OSError:
        return
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


# ── Registry fetch ────────────────────────────────────────────────────────────

def _fetch_registry_metadata(target_spec: str, timeout: int = 8) -> dict[str, Any]:
    """Fetch openclaw package metadata for <target_spec> from the npm registry.

    target_spec may be 'latest', a dist-tag, or a concrete version. The registry
    endpoint /openclaw/<spec> resolves dist-tags server-side.
    """
    url = f"{OPENCLAW_REGISTRY_BASE}/{urllib.parse.quote(target_spec)}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "evolve-admin-safe-upgrade/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body)


# ── Local version helpers ────────────────────────────────────────────────────

def _installed_version() -> str | None:
    try:
        return json.loads(OPENCLAW_PACKAGE_JSON.read_text()).get("version")
    except (OSError, ValueError):
        return None


def _node_version() -> str | None:
    try:
        r = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return r.stdout.strip().lstrip("v")
    except (OSError, subprocess.SubprocessError):
        pass
    return None


# ── Semver-range checker (minimal — handles npm engines.node shapes) ─────────

_VERSION_TOKEN_RE = re.compile(r"\d+(?:\.\d+){0,2}")


def _parse_version(v: str) -> tuple[int, int, int] | None:
    """Parse 'X', 'X.Y', or 'X.Y.Z' into (X,Y,Z). Returns None on failure."""
    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", (v or "").strip().lstrip("v"))
    if not m:
        return None
    return (
        int(m.group(1)),
        int(m.group(2) or 0),
        int(m.group(3) or 0),
    )


_RANGE_PART_RE = re.compile(r"\s*(>=|<=|>|<|=|\^|~)?\s*(\d+(?:\.\d+){0,2})\s*")


def _satisfies_range(current: str, range_spec: str) -> bool | None:
    """Check whether `current` satisfies an npm-style range expression.

    Supports: >=N, >N, <=N, <N, =N, N (exact), ^N, ~N, and conjunctions
    like ">=18 <22". Disjunctions ("||") are evaluated.

    Returns True/False on a parseable range, or None if the range is in
    a shape we don't recognize (caller should treat that as a soft fail).
    """
    cur = _parse_version(current)
    if cur is None or not range_spec or not range_spec.strip():
        return None

    for clause in range_spec.split("||"):
        ok = _check_clause(cur, clause.strip())
        if ok:
            return True
        if ok is None:
            return None  # unknown shape — bail
    return False


def _check_clause(cur: tuple[int, int, int], clause: str) -> bool | None:
    parts = _RANGE_PART_RE.findall(clause)
    if not parts:
        return None
    for op, ver in parts:
        v = _parse_version(ver)
        if v is None:
            return None
        op = op or "="
        if op == "^":
            # ^X.Y.Z → >=X.Y.Z and <(X+1).0.0  (assuming X > 0)
            if cur < v or cur >= (v[0] + 1, 0, 0):
                return False
        elif op == "~":
            # ~X.Y.Z → >=X.Y.Z and <X.(Y+1).0
            if cur < v or cur >= (v[0], v[1] + 1, 0):
                return False
        elif op == ">=":
            if cur < v:
                return False
        elif op == ">":
            if cur <= v:
                return False
        elif op == "<=":
            if cur > v:
                return False
        elif op == "<":
            if cur >= v:
                return False
        else:  # "=" or bare version
            if cur != v:
                return False
    return True


# ── User-launchagent scan (read-only) ─────────────────────────────────────────

def scan_user_launchagents(network: dict[str, Any]) -> dict[str, Any]:
    """Scan each bot user's ~/Library/LaunchAgents/ for orphan postinstall
    artifacts that would compete with the system gateway daemons.

    Returns:
        {
          "scanned_users": [<user>...],
          "found_agents":  [{"bot_id": ..., "user": ..., "path": ...}, ...],
        }

    Read-only counterpart of ocadmin._remove_conflicting_user_agents — no
    bootout, no rename, no chmod.
    """
    from .ocadmin import _gateway_runtime_user as _bot_user, _bot_service, _bot_ids  # local import: avoid cycle

    found: list[dict[str, str]] = []
    scanned: list[str] = []
    for bot_id in _bot_ids(network):
        user = _bot_user(bot_id, network)
        scanned.append(user)
        agent_path = Path(f"/Users/{user}/Library/LaunchAgents/{USER_AGENT_BASENAME}")
        daemon_path = Path(f"/Library/LaunchDaemons/{_bot_service(bot_id)}.plist")

        # Only flag when a system daemon exists for this bot — otherwise the
        # user-level agent may be intentional.
        if not daemon_path.exists():
            continue

        if agent_path.exists():
            found.append({"bot_id": bot_id, "user": user, "path": str(agent_path)})

    return {"scanned_users": scanned, "found_agents": found}


# ── Gate implementations ─────────────────────────────────────────────────────

def gate_node_version(metadata: dict[str, Any]) -> tuple[GateResult, list[Requirement]]:
    current = _node_version()
    required = (
        (metadata.get("engines") or {}).get("node") if isinstance(metadata, dict) else None
    )

    if not current:
        return (
            GateResult(ok=False, details={"current": None, "required": required, "error": "could not run `node --version`"}),
            [Requirement(
                id="node-version-unknown",
                summary="Could not determine running Node.js version (`node --version` failed).",
                remediation="Verify Node is installed and on PATH for the evolve user, then re-run the check.",
                blocking=True, source_gate="node_version",
            )],
        )

    if not required:
        return (
            GateResult(ok=False, details={"current": current, "required": None}),
            [Requirement(
                id="node-version-unpinned",
                summary=f"Candidate openclaw does not declare engines.node — Node compatibility is unknown.",
                remediation="Refuse to upgrade to a build that does not pin engines.node. Pin to a known-good version.",
                blocking=True, source_gate="node_version",
            )],
        )

    satisfied = _satisfies_range(current, required)
    if satisfied is True:
        return GateResult(ok=True, details={"current": current, "required": required}), []
    if satisfied is False:
        return (
            GateResult(ok=False, details={"current": current, "required": required}),
            [Requirement(
                id="node-version-mismatch",
                summary=f"Node {current} does not satisfy candidate's engines.node \"{required}\".",
                remediation=f"Upgrade Node to satisfy `{required}`. Current: {current}. Run: `brew upgrade node` and re-run the check.",
                blocking=True, source_gate="node_version",
            )],
        )
    # Unknown range shape — be defensive
    return (
        GateResult(ok=False, details={"current": current, "required": required, "error": "unrecognized range shape"}),
        [Requirement(
            id="node-version-unparseable-range",
            summary=f"Could not parse engines.node range \"{required}\".",
            remediation="Inspect the candidate package.json manually before upgrading.",
            blocking=True, source_gate="node_version",
        )],
    )


def gate_stub_install(metadata: dict[str, Any], installed_version: str | None) -> tuple[GateResult, list[Requirement]]:
    version = (metadata or {}).get("version") if isinstance(metadata, dict) else None
    bin_field = (metadata or {}).get("bin") if isinstance(metadata, dict) else None
    dist = (metadata or {}).get("dist") or {}
    size = dist.get("unpackedSize") or dist.get("fileCount")  # unpackedSize when present

    bin_present = bool(bin_field) and (
        bool(bin_field) if isinstance(bin_field, str) else len(bin_field) > 0
    )
    parsed_v = _parse_version(version) if version else None
    version_low = bool(parsed_v and parsed_v[0] == 0)
    size_kb = round(size / 1024) if isinstance(size, (int, float)) and size else None
    size_too_small = bool(
        isinstance(size, (int, float)) and size and size < MIN_REASONABLE_TARBALL_BYTES
    )

    details = {
        "resolved_version": version,
        "size_kb": size_kb,
        "bin_present": bin_present,
        "version_low": version_low,
    }

    failures: list[str] = []
    if version_low:
        failures.append(f"version {version} looks suspiciously low (≤ 0.x)")
    if not bin_present:
        failures.append("bin field is missing or empty")
    if size_too_small:
        failures.append(f"tarball is suspiciously small ({size_kb} KB; expect multi-MB)")

    if not failures:
        return GateResult(ok=True, details=details), []

    last_good = installed_version or "unknown"
    return (
        GateResult(ok=False, details=details),
        [Requirement(
            id="stub-install-pin-target",
            summary="Target package looks like a name-squat or stub: " + "; ".join(failures),
            remediation=f"Refuse the 'latest' tag. Pin the upgrade to a specific known-good version. Last known-good shipped on this pod: {last_good}.",
            blocking=True, source_gate="stub_install",
        )],
    )


def gate_user_launchagents(network: dict[str, Any]) -> tuple[GateResult, list[Requirement]]:
    scan = scan_user_launchagents(network)
    found = scan["found_agents"]
    if not found:
        return GateResult(ok=True, details=scan), []
    bot_list = sorted({f["bot_id"] for f in found})
    return (
        GateResult(ok=False, details=scan),
        [Requirement(
            id="user-launchagents-cleanup",
            summary=f"Bot users {bot_list} have orphan ai.openclaw.gateway LaunchAgents in ~/Library/LaunchAgents/.",
            remediation="These will compete with the system gateway daemons after upgrade. Run: `sudo evolve-admin menu upgrade` (which removes them) or remove them manually before re-running this check.",
            blocking=True, source_gate="user_launchagents",
        )],
    )


def download_candidate_tarball(
    metadata: dict[str, Any], timeout: int = 30,
) -> tuple[bytes | None, str | None]:
    """Download the candidate npm tarball named in ``metadata.dist.tarball``.

    Returns ``(bytes, None)`` on success or ``(None, error_message)`` on any
    failure. Factored out of :func:`_fetch_candidate_plugin_ids` so other
    consumers (the OC surface-snapshot drift-diff in ``update_watcher``) can
    build a full surface snapshot from the same bytes without re-implementing
    the fetch.
    """
    if not isinstance(metadata, dict):
        return None, "no candidate metadata"
    tarball_url = (metadata.get("dist") or {}).get("tarball")
    if not tarball_url:
        return None, "candidate metadata missing dist.tarball URL"
    try:
        req = urllib.request.Request(
            tarball_url,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": "evolve-admin-safe-upgrade/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), None
    except urllib.error.URLError as e:
        return None, f"tarball fetch failed: {getattr(e, 'reason', e)}"
    except Exception as e:
        return None, f"tarball fetch failed: {e}"


def _fetch_candidate_plugin_ids(
    metadata: dict[str, Any], timeout: int = 30,
) -> tuple[set[str] | None, str | None]:
    """Download the candidate tarball and return the set of stock plugin ids it ships.

    Plugin ids are the names of top-level directories under
    `package/dist/extensions/` inside the npm tarball — the same layout
    `openclaw plugins list` reports as `stock:` source.

    Returns (plugin_ids, None) on success or (None, error_message) on any
    failure. Caller treats fetch failure as a blocker — we don't want to
    silently pass the gate just because the registry blipped.
    """
    data, err = download_candidate_tarball(metadata, timeout=timeout)
    if err is not None:
        return None, err
    assert data is not None  # err is None ⇒ data is present

    plugin_ids: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for name in tf.getnames():
                # npm tarballs prefix every entry with 'package/'. We want
                # top-level dirs under package/dist/extensions/.
                parts = name.split("/")
                if (len(parts) >= 4
                        and parts[0] == "package"
                        and parts[1] == "dist"
                        and parts[2] == "extensions"
                        and parts[3]):
                    plugin_ids.add(parts[3])
    except tarfile.TarError as e:
        return None, f"could not read tarball: {e}"

    if not plugin_ids:
        return None, "tarball did not contain dist/extensions/ — layout changed?"
    return plugin_ids, None


def _read_bot_openclaw_json(bot_id: str, network: dict[str, Any]) -> dict[str, Any] | None:
    """Read a bot's openclaw.json. Returns parsed dict, or None if the file
    can't be read/parsed. The evolve user has an ACL read grant on bot
    `.openclaw/` dirs; sudo /bin/cat is the documented fallback for bots
    not yet onboarded through the new path (see CLAUDE.md)."""
    from .ocadmin import _gateway_runtime_user as _bot_user  # local import: avoid cycle
    user = _bot_user(bot_id, network)
    path = user_home(user) / ".openclaw/openclaw.json"  # platform-keyed home (Linux /home, macOS /Users)
    text: str | None = None
    try:
        text = path.read_text()
    except PermissionError:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)], capture_output=True, text=True,
        )
        if r.returncode == 0:
            text = r.stdout
    except (OSError, FileNotFoundError):
        return None
    if text is None:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _enabled_plugin_refs(cfg: dict[str, Any]) -> list[str]:
    """Return the plugin ids the bot actually relies on.

    Three signals in openclaw.json can imply "this bot needs plugin X":

    1. `plugins.entries.<id>.enabled = true` — explicit opt-in. Older
       bots were set up this way (every needed plugin listed in entries).

    2. `channels.<id>.enabled = true` — channel config implies the
       channel plugin. Newer bots may rely on this alone, with no
       plugins.entries record. Caught the 2026-05-15 false-negative
       where team_bot_c had `channels.slack.enabled = true` but no
       `plugins.entries.slack` and the gate missed slack on team_bot_c.

    3. `tools.web.search.provider = "<id>"` — the web-search provider's
       plugin is required (brave / duckduckgo / exa / perplexity /
       searxng / tavily are all plugin ids).

    Explicit `plugins.entries.<id>.enabled = false` overrides — operator
    opt-out beats any inferred-from-config-presence signal.
    """
    if not isinstance(cfg, dict):
        return []
    plugins = cfg.get("plugins") or {}
    entries = plugins.get("entries") if isinstance(plugins, dict) else None

    enabled: set[str] = set()
    disabled: set[str] = set()
    if isinstance(entries, dict):
        for pid, body in entries.items():
            if not isinstance(pid, str) or not pid:
                continue
            if isinstance(body, dict):
                if body.get("enabled") is True:
                    enabled.add(pid)
                elif body.get("enabled") is False:
                    disabled.add(pid)

    # Channel config — `channels.<id>.enabled = true` implies plugin <id>.
    channels = cfg.get("channels")
    if isinstance(channels, dict):
        for cid, body in channels.items():
            if (isinstance(cid, str) and cid
                    and isinstance(body, dict) and body.get("enabled") is True):
                enabled.add(cid)

    # Web-search provider — `tools.web.search.provider` names a plugin id.
    tools = cfg.get("tools")
    if isinstance(tools, dict):
        web = tools.get("web")
        if isinstance(web, dict):
            search = web.get("search")
            if isinstance(search, dict):
                provider = search.get("provider")
                if isinstance(provider, str) and provider:
                    enabled.add(provider)

    return sorted(enabled - disabled)


# Entry-file names the openclaw runtime tries when loading a plugin from
# its installPath. Mirrors the runtime's own error message:
#   "expected ./dist/index.js, ./dist/index.mjs, ./dist/index.cjs,
#    index.js, index.mjs, index.cjs."
# A plugin whose installPath contains NONE of these is "phantom" — the
# install record exists but the runtime will refuse to load it. The
# 2026.5.1-beta.1 versions of brave/discord-plugin are this shape: they
# ship TypeScript source (index.ts) and an empty dist/ stamp directory,
# so the install command accepts them but the runtime rejects them.
_LOADABLE_PLUGIN_ENTRY_PATHS = (
    "dist/index.js",
    "dist/index.mjs",
    "dist/index.cjs",
    "index.js",
    "index.mjs",
    "index.cjs",
)


def _is_phantom_install(install_path: str | None) -> bool:
    """Return True iff ``install_path`` exists as a directory but contains
    no loadable plugin entry. Returns False on uncertainty (path missing,
    permission denied) so we don't false-positive on real opaque cases.

    Tries ``Path.exists`` directly first — works for paths covered by the
    evolve ACL grant (``set_evolve_read_acl``). For root-owned install
    dirs that the evolve user can't traverse, falls back to
    ``sudo /bin/ls -1`` per the CLAUDE.md read pattern.
    """
    if not install_path:
        return False
    root = Path(install_path)
    # Direct probe — fastest path, works for ACL-readable dirs.
    try:
        if root.is_dir():
            for entry in _LOADABLE_PLUGIN_ENTRY_PATHS:
                if (root / entry).is_file():
                    return False
            # Directory exists, no loadable entry found → phantom.
            return True
        # Path doesn't exist or isn't a directory — uncertain, not phantom.
        return False
    except PermissionError:
        pass

    # Fallback: ls -1 with sudo. Build a single-pass listing so we don't
    # spam subprocess calls for every candidate entry.
    r = subprocess.run(
        ["sudo", "/bin/ls", "-1", str(root)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False  # opaque — don't flag
    top_level = set(r.stdout.splitlines())
    if not top_level:
        return True
    # Top-level entries that count as loadable
    for entry in _LOADABLE_PLUGIN_ENTRY_PATHS:
        if "/" not in entry and entry in top_level:
            return False
    # Need to peek inside dist/ if present
    if "dist" in top_level:
        r2 = subprocess.run(
            ["sudo", "/bin/ls", "-1", str(root / "dist")],
            capture_output=True, text=True,
        )
        if r2.returncode == 0:
            dist_files = set(r2.stdout.splitlines())
            for entry in _LOADABLE_PLUGIN_ENTRY_PATHS:
                if entry.startswith("dist/") and entry.removeprefix("dist/") in dist_files:
                    return False
    return True


# OpenClaw's plugin install registry has moved on disk over time. As of
# the 2026.6.x migration it lives in a SQLite table (state/openclaw.sqlite
# → installed_plugin_index), and the old flat JSON file is left behind
# renamed to `installs.json.migrated` — which then goes STALE (a plugin
# installed after the migration only updates the SQLite row). Read the
# live source first; fall back to the legacy locations for un-migrated
# pods. Listed newest-format-first.
def _installs_sqlite_path(user: str) -> Path:
    # user_home() resolves the OS account's home via pwd (macOS /Users, Linux
    # /home) — never the macOS-only ``/Users/<user>`` literal that left these
    # reads pointing at a non-existent path on the Ubuntu/VPS pods (W10 Linux
    # port; platform-path-lint baseline ratcheted down with this change).
    return user_home(user) / ".openclaw/state/openclaw.sqlite"


def _installs_json_path(user: str) -> Path:
    return user_home(user) / ".openclaw/plugins/installs.json"


def _installs_json_migrated_path(user: str) -> Path:
    return user_home(user) / ".openclaw/plugins/installs.json.migrated"


def _read_installs_from_sqlite(path: Path) -> dict[str, Any] | None:
    """Read the newest `installed_plugin_index` row from OpenClaw's state
    DB and normalize it to the legacy installs.json shape
    (``{"installRecords": {...}, ...}``) so existing callers are unchanged.

    Opened read-only + immutable: ``immutable=1`` reads the main DB file
    directly without touching the -wal/-shm or taking locks, which is what
    we want for a read-only observer that has ACL read on the file but not
    write on the directory. Tradeoff: records committed to the WAL but not
    yet checkpointed are invisible — acceptable for a deliberate preflight
    (install ops are rare and the fail-safe in the gate covers the gap).
    Returns None if the DB can't be opened or has no usable row."""
    uri = f"file:{path}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        cur = conn.execute(
            "SELECT install_records_json, host_contract_version, "
            "compat_registry_version "
            "FROM installed_plugin_index "
            "ORDER BY generated_at_ms DESC LIMIT 1"
        )
        row = cur.fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row:
        return None
    try:
        records = json.loads(row[0]) if isinstance(row[0], str) else None
    except (json.JSONDecodeError, ValueError):
        records = None
    if not isinstance(records, dict):
        return None
    return {
        "installRecords": records,
        "hostContractVersion": row[1],
        "compatRegistryVersion": row[2],
        "_source": "state/openclaw.sqlite",
    }


def _read_installs_json_file(path: Path) -> dict[str, Any] | None:
    """Read a flat-JSON install registry. Same read-with-sudo-fallback
    pattern as other helpers in this module."""
    text: str | None = None
    try:
        text = path.read_text()
    except PermissionError:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)], capture_output=True, text=True,
        )
        if r.returncode == 0:
            text = r.stdout
    except (OSError, FileNotFoundError):
        return None
    if text is None:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _read_installs_json(user: str) -> dict[str, Any] | None:
    """Read a bot's OpenClaw plugin install records, newest on-disk format
    first: the SQLite state DB (OC 2026.6.x+), then the legacy flat
    `installs.json`, then its `.migrated` backup. Records are normalized to
    ``{"installRecords": {...}, ...}`` regardless of source.

    A non-None return means we POSITIVELY read a registry. None means we
    could not — the file is missing or unparseable. Callers must not
    conflate None with "nothing installed"; pair with
    ``_install_registry_exists()`` to tell the two apart (this is the
    distinction that turned an OpenClaw registry-format migration into a
    false 'plugin missing' blocker — see the gate's fail-safe)."""
    sqlite_data = _read_installs_from_sqlite(_installs_sqlite_path(user))
    if sqlite_data is not None:
        return sqlite_data
    legacy = _read_installs_json_file(_installs_json_path(user))
    if legacy is not None:
        return legacy
    return _read_installs_json_file(_installs_json_migrated_path(user))


def _install_registry_exists(user: str) -> bool:
    """True if ANY recognized plugin-install registry file is present on
    disk for this bot — even one we failed to parse. This distinguishes
    "OpenClaw changed its registry format/location" (a file is present but
    we couldn't read it) from "this bot never installed a plugin" (no file
    at all). On a stat error we can't tell, so we fail safe and report
    present (the gate then warns rather than blocks)."""
    for p in (_installs_sqlite_path(user),
              _installs_json_path(user),
              _installs_json_migrated_path(user)):
        try:
            if p.exists():
                return True
        except OSError:
            return True
    return False


def _installed_plugin_ids(bot_id: str, network: dict[str, Any]) -> set[str]:
    """Return plugin ids the bot has *loadably* installed via
    ``openclaw plugins install``.

    Filters out phantom installs — records that exist in the install
    registry but whose ``installPath`` lacks a loadable entry. The
    @openclaw/brave-plugin@2026.5.1-beta.1 case is this shape: install
    succeeds on the older runtime, record gets written, but the package
    is TS-source-only and the newer runtime refuses to load it. Treating
    those as "installed" would mask a real config_references blocker.

    Use ``_phantom_install_ids()`` to retrieve just the phantoms (for
    surfacing in gate details and cleaning up during the dance).
    """
    from .ocadmin import _gateway_runtime_user as _bot_user  # local import: avoid cycle
    user = _bot_user(bot_id, network)
    data = _read_installs_json(user)
    records = data.get("installRecords") if isinstance(data, dict) else None
    if not isinstance(records, dict):
        return set()
    loadable: set[str] = set()
    for pid, rec in records.items():
        if not isinstance(pid, str) or not pid:
            continue
        ipath = rec.get("installPath") if isinstance(rec, dict) else None
        if not _is_phantom_install(ipath):
            loadable.add(pid)
    return loadable


def _phantom_install_ids(bot_id: str, network: dict[str, Any]) -> dict[str, str]:
    """Return ``{plugin_id: installPath}`` for installs whose path lacks a
    loadable entry. The dance phase that installs externalized plugins
    can use this to know what to clean up first (the phantom record
    blocks a fresh install of the same id)."""
    from .ocadmin import _gateway_runtime_user as _bot_user  # local import: avoid cycle
    user = _bot_user(bot_id, network)
    data = _read_installs_json(user)
    records = data.get("installRecords") if isinstance(data, dict) else None
    if not isinstance(records, dict):
        return {}
    phantoms: dict[str, str] = {}
    for pid, rec in records.items():
        if not isinstance(pid, str) or not pid:
            continue
        ipath = rec.get("installPath") if isinstance(rec, dict) else None
        if isinstance(ipath, str) and _is_phantom_install(ipath):
            phantoms[pid] = ipath
    return phantoms


def _install_state_determinable(bot_id: str, network: dict[str, Any]) -> bool:
    """False iff a plugin-install registry exists for this bot but couldn't
    be read in any recognized format — i.e. install state is UNKNOWN.

    Returns True when the registry was read OK, or when no registry file
    exists at all (a bot that never installed a plugin — genuinely empty,
    so a reference to a dropped plugin is a real blocker). Returns False
    only for the in-between case: a registry IS present but opaque to us,
    which is the signature of OpenClaw moving its on-disk layout out from
    under the reader. The config_references gate downgrades 'missing'
    plugins to a non-blocking warning in that case so an unrecognized
    migration fails safe instead of blocking the fleet on a phantom
    finding (the exact failure that produced a false NOT-SAFE on the
    2026.6.1 → 2026.6.5 check)."""
    from .ocadmin import _gateway_runtime_user as _bot_user  # local import: avoid cycle
    user = _bot_user(bot_id, network)
    if _read_installs_json(user) is not None:
        return True
    return not _install_registry_exists(user)


# Plugin ids whose plugin lives in a separately-installed npm package
# (the "npm; ClawHub" rows in openclaw's docs/plugins/reference.md). When
# the gate sees one of these missing from the candidate's stock plugins,
# the remediation knows the right npm package to install rather than
# suggesting migrate/disable.
#
# Source: openclaw@2026.5.12 docs/plugins/reference.md "Install route:
# npm; ClawHub". Update when openclaw externalizes more bundled plugins
# (or move to dynamic parsing of the docs file shipped in the candidate
# tarball — out of scope for now).
_KNOWN_EXTERNALIZED_PLUGINS: dict[str, str] = {
    "acpx": "@openclaw/acpx",
    "anthropic-vertex": "@openclaw/anthropic-vertex-provider",
    "brave": "@openclaw/brave-plugin",
    "codex": "@openclaw/codex",
    "diagnostics-otel": "@openclaw/diagnostics-otel",
    "diagnostics-prometheus": "@openclaw/diagnostics-prometheus",
    "diffs": "@openclaw/diffs",
    "discord": "@openclaw/discord",
    "feishu": "@openclaw/feishu",
    "google-meet": "@openclaw/google-meet",
    "googlechat": "@openclaw/googlechat",
    "line": "@openclaw/line",
    "lobster": "@openclaw/lobster",
    "memory-lancedb": "@openclaw/memory-lancedb",
    "msteams": "@openclaw/msteams",
    "nextcloud-talk": "@openclaw/nextcloud-talk",
    "nostr": "@openclaw/nostr",
    "openshell": "@openclaw/openshell-sandbox",
    "openshell-sandbox": "@openclaw/openshell-sandbox",  # legacy alias
    "qqbot": "@openclaw/qqbot",
    "slack": "@openclaw/slack",
    "synology-chat": "@openclaw/synology-chat",
    "tlon": "@openclaw/tlon",
    "twitch": "@openclaw/twitch",
    "voice-call": "@openclaw/voice-call",
    "zalo": "@openclaw/zalo",
    "zalouser": "@openclaw/zalouser",
}


def _load_path_plugin_ids(cfg: dict[str, Any]) -> set[str]:
    """Return plugin ids supplied by `plugins.load.paths` (external sources).

    Each entry in `plugins.load.paths` is a filesystem path to a
    single-plugin package whose `openclaw.plugin.json` declares its `id`.
    Evolve's own plugin lives at `/Users/Shared/evolve-plugin/` and
    declares id "evolve"; without reading this, the gate would
    false-positive-flag "evolve" as missing on every upgrade.

    Returns the set of ids it could resolve. Paths that don't exist or
    don't declare an id are skipped silently — the gate's job is to
    answer "is this plugin available somewhere", and we want to be
    permissive about external sources rather than block on a missing
    sidecar directory.
    """
    if not isinstance(cfg, dict):
        return set()
    load = (cfg.get("plugins") or {}).get("load")
    paths: list[str] = []
    if isinstance(load, dict):
        raw_paths = load.get("paths")
        if isinstance(raw_paths, list):
            paths = [p for p in raw_paths if isinstance(p, str)]
    elif isinstance(load, list):
        paths = [p for p in load if isinstance(p, str)]

    ids: set[str] = set()
    for p in paths:
        manifest = Path(p) / "openclaw.plugin.json"
        try:
            data = json.loads(manifest.read_text())
        except (OSError, FileNotFoundError, json.JSONDecodeError, ValueError):
            continue
        pid = data.get("id") if isinstance(data, dict) else None
        if isinstance(pid, str) and pid:
            ids.add(pid)
    return ids


def gate_config_references(
    metadata: dict[str, Any], network: dict[str, Any],
) -> tuple[GateResult, list[Requirement]]:
    """Check that every plugin a bot has enabled still ships in the candidate.

    Catches the 2026-05-15 failure mode: openclaw@2026.5.12 dropped the
    `brave` stock plugin, but every bot's openclaw.json still had
    `plugins.entries.brave.enabled = true`, so post-upgrade gateways
    refused to start ("web_search provider is not available: brave").
    """
    from .ocadmin import _bot_ids  # local import: avoid cycle

    plugin_ids, fetch_err = _fetch_candidate_plugin_ids(metadata)
    if plugin_ids is None:
        details = {"error": fetch_err, "candidate_plugins": [], "bots": []}
        return (
            GateResult(ok=False, details=details),
            [Requirement(
                id="config-references-tarball-fetch",
                summary=f"Could not inspect candidate plugin list: {fetch_err}",
                remediation=(
                    "Resolve the tarball fetch error (network reachability, "
                    "registry availability) and re-run the preflight."
                ),
                blocking=True, source_gate="config_references",
            )],
        )

    bots_detail: list[dict[str, Any]] = []
    missing_by_bot: dict[str, list[str]] = {}
    unknown_by_bot: dict[str, list[str]] = {}
    unreadable: list[str] = []
    for bot_id in _bot_ids(network):
        cfg = _read_bot_openclaw_json(bot_id, network)
        if cfg is None:
            bots_detail.append({"bot_id": bot_id, "error": "could not read openclaw.json"})
            unreadable.append(bot_id)
            continue
        refs = _enabled_plugin_refs(cfg)
        # A plugin is "available" to a bot via any of three sources:
        #   1. Stock — bundled in the openclaw tarball at dist/extensions/<id>/.
        #   2. Load path — supplied by a directory in plugins.load.paths
        #      (Evolve's pattern: /Users/Shared/evolve-plugin → id=evolve).
        #   3. Installed — pulled in via `openclaw plugins install <pkg>` and
        #      recorded in the bot's install registry (state/openclaw.sqlite as
        #      of OC 2026.6.x; legacy plugins/installs.json before that). This
        #      is the externalization story: brave/codex/slack/discord live
        #      here, not in stock.
        load_ids = _load_path_plugin_ids(cfg)
        installed_ids = _installed_plugin_ids(bot_id, network)
        phantoms = _phantom_install_ids(bot_id, network)
        # Could we actually read this bot's install registry? If a registry
        # file is present but unreadable (e.g. OpenClaw moved its on-disk
        # layout out from under us), install state is UNKNOWN — we must not
        # claim a plugin is missing just because we couldn't see the record.
        determinable = _install_state_determinable(bot_id, network)
        available = plugin_ids | load_ids | installed_ids
        missing = sorted(r for r in refs if r not in available)
        bots_detail.append({
            "bot_id": bot_id,
            "enabled_plugins": refs,
            "load_path_plugins": sorted(load_ids),
            "installed_plugins": sorted(installed_ids),
            "phantom_installs": sorted(phantoms.keys()),
            "install_state_determinable": determinable,
            "missing_in_candidate": missing,
        })
        if missing:
            if determinable:
                missing_by_bot[bot_id] = missing
            else:
                # Registry present but unreadable → warn, don't block.
                unknown_by_bot[bot_id] = missing

    details = {
        "candidate_plugins": sorted(plugin_ids),
        "bots": bots_detail,
        "missing_by_bot": missing_by_bot,
        "unknown_by_bot": unknown_by_bot,
        "unreadable_bots": unreadable,
    }

    requirements: list[Requirement] = []
    if missing_by_bot:
        all_missing = sorted({m for ms in missing_by_bot.values() for m in ms})
        affected_bots = sorted(missing_by_bot.keys())

        # Split missing plugins by whether openclaw has externalized them (in
        # which case `openclaw plugins install <pkg>` is the right fix and
        # the plugin's functionality is fully preserved) or actually removed
        # them (in which case the user has to migrate/pin/disable).
        externalized = {p: _KNOWN_EXTERNALIZED_PLUGINS[p]
                        for p in all_missing if p in _KNOWN_EXTERNALIZED_PLUGINS}
        truly_missing = [p for p in all_missing if p not in _KNOWN_EXTERNALIZED_PLUGINS]

        # Per-bot install commands for the externalized case. The fix is to
        # install the npm package on each affected bot BEFORE the upgrade,
        # while the bot is still on its current OpenClaw version (where its
        # openclaw.json is valid). Once a runtime that has dropped the plugin
        # is live, the bot's config references a plugin the runtime can't
        # load, and config validation can refuse to run `openclaw plugins
        # install` against an invalid config — the 2026-05-15 catch-22. Order
        # of operations (install-then-upgrade) sidesteps it; if you're already
        # stuck on the new runtime, pin back to a bundling version (option b)
        # or run the neutralize/install dance.
        install_block = ""
        if externalized:
            # Resolve each bot's actual macOS user — `team_bot_b` (bot id) maps to
            # `personal_bot_user` (macOS user) per network.json. `-H` resets HOME so
            # npm writes its cache to the target user's home, not the
            # invoking user's (otherwise: EACCES on /Users/<caller>/.npm).
            from .ocadmin import _gateway_runtime_user as _bot_user  # local import: avoid cycle
            install_cmds_per_bot: list[str] = []
            for b in affected_bots:
                bot_missing_ext = [
                    p for p in missing_by_bot[b] if p in externalized
                ]
                if not bot_missing_ext:
                    continue
                user = _bot_user(b, network)
                cmds = " && \\\n                 ".join(
                    f"sudo -u {user} -H openclaw plugins install {externalized[p]}"
                    for p in bot_missing_ext
                )
                install_cmds_per_bot.append(f"             cd /tmp && {cmds}")
            install_block = (
                "         (a) These plugins were externalized — install the npm "
                "package on each affected bot, then upgrade. Packages: "
                + ", ".join(sorted({externalized[p] for p in externalized}))
                + ".\n             Run these while the bots are still on their "
                "CURRENT OpenClaw version (config still valid):\n"
                + "\n".join(install_cmds_per_bot)
                + "\n             If a bot is already on the new runtime and the "
                "install refuses against an invalid config, pin back to a "
                "bundling version (b) first, or run the neutralize/install dance.\n"
            )

        # Fallback options for genuinely-removed plugins (or if the operator
        # decides to stop using a plugin rather than install the npm package).
        fallback_options = (
            "         (b) Pin the upgrade to a version that still bundles them — "
            "`sudo evolve-admin menu safe-upgrade --target=<version>` to verify, then\n"
            "             `sudo /opt/homebrew/bin/npm install -g --prefix=/opt/homebrew "
            "openclaw@<version>`.\n"
            "         (c) Migrate to a plugin the candidate DOES ship. For web-search "
            "providers, 2026.5.12 ships duckduckgo / tavily / searxng / perplexity — "
            "switch `tools.web.search.provider` accordingly.\n"
            "         (d) Only if the functionality is truly optional: disable per bot "
            f"via `sudo evolve-admin menu plugins <bot>`."
        )

        ext_list = sorted(externalized)
        ext_note = (
            f" ({ext_list} were externalized — installable as npm packages)"
            if externalized else ""
        )
        summary = (
            f"Bot(s) {affected_bots} have enabled plugins not available in the candidate "
            f"(checked stock + plugins.load.paths + install registry): {all_missing}{ext_note}."
        )

        requirements.append(Requirement(
            id="config-references-missing-plugins",
            summary=summary,
            remediation=(
                "These bots will fail to start after upgrade.\n"
                + (install_block if externalized else "")
                + (f"         For genuinely-removed plugins ({truly_missing}):\n"
                   if truly_missing and externalized else "")
                + fallback_options
            ),
            blocking=True, source_gate="config_references",
        ))
    if unknown_by_bot:
        all_unknown = sorted({m for ms in unknown_by_bot.values() for m in ms})
        unknown_bots = sorted(unknown_by_bot.keys())
        requirements.append(Requirement(
            id="config-references-install-state-unknown",
            summary=(
                f"Could not read the plugin install registry for bot(s) {unknown_bots}; "
                f"cannot confirm whether {all_unknown} survive the upgrade."
            ),
            remediation=(
                "A plugin-install registry is present for these bots but wasn't "
                "readable in a recognized format — OpenClaw may have changed its "
                "on-disk layout again (the reader knows state/openclaw.sqlite, "
                "plugins/installs.json, and the .migrated backup). This is NOT a "
                "confirmed blocker; these plugins may well be installed. Canary-"
                "upgrade ONE low-traffic bot, confirm its gateway log lists every "
                "expected plugin (`http server listening (N plugins: …)`), then roll "
                "the rest — and file an Evolve issue to teach the reader the new "
                "registry layout."
            ),
            blocking=False, source_gate="config_references",
        ))
    if unreadable:
        requirements.append(Requirement(
            id="config-references-unreadable",
            summary=f"Could not read openclaw.json for bot(s): {unreadable}.",
            remediation=(
                "Verify the file exists and the evolve user has read ACL on each bot's "
                "`.openclaw/` directory. Re-deploy affected bots if needed "
                "(`sudo evolve-admin deploy <bot>`)."
            ),
            blocking=True, source_gate="config_references",
        ))

    return GateResult(ok=not requirements, details=details), requirements


def gate_plist_paths(network: dict[str, Any]) -> tuple[GateResult, list[Requirement]]:
    """Check that each bot's gateway service definition is present and valid.

    The gateway is defined differently per platform, so the check routes
    through the platform-profile seam (``get_profile()``) rather than
    assuming launchd everywhere:

    - **macOS** — gateways are launchd plists in ``/Library/LaunchDaemons``.
      We parse ``ProgramArguments`` and verify every referenced file path
      exists; stale paths are the failure mode that bricked the fleet on
      2026-04-24 (:func:`_gate_gateway_plists`).
    - **Linux** — gateways are systemd units; there are no launchd plists,
      so the macOS plist-path check is a category error there — it produced a
      false "Gateway plists … reference paths that don't exist on disk"
      blocker on every Linux pod. We instead verify the unit exists and is
      loaded via the Scheduler seam (:func:`_gate_gateway_units`).

    The ``source_gate``/``GATE_ORDER`` key stays ``plist_paths`` on both
    platforms so the report shape is unchanged.
    """
    from platform_profile import get_profile

    if get_profile().name == "linux":
        return _gate_gateway_units(network)
    return _gate_gateway_plists(network)


def _gate_gateway_plists(network: dict[str, Any]) -> tuple[GateResult, list[Requirement]]:
    """macOS gateway-existence check: each launchd plist exists and the file
    paths it references in ``ProgramArguments`` exist on disk."""
    from platform_profile import get_profile

    from .ocadmin import _bot_ids, _bot_service  # local import: avoid cycle

    daemon_dir = Path(get_profile().daemon_dir)
    inspected: list[dict[str, Any]] = []
    stale: list[str] = []

    for bot_id in _bot_ids(network):
        label = _bot_service(bot_id)
        plist_path = daemon_dir / f"{label}.plist"
        entry = {"bot_id": bot_id, "label": label, "plist": str(plist_path),
                 "exists": plist_path.exists(), "missing_paths": []}
        if not plist_path.exists():
            entry["error"] = "plist does not exist"
            stale.append(label)
            inspected.append(entry)
            continue
        try:
            data = plistlib.loads(plist_path.read_bytes())
        except (OSError, plistlib.InvalidFileException) as e:
            entry["error"] = f"could not read plist: {e}"
            stale.append(label)
            inspected.append(entry)
            continue
        args = data.get("ProgramArguments") or []
        # ProgramArguments[0] is typically /opt/homebrew/bin/node
        # ProgramArguments[1] is typically the openclaw entry script
        for arg in args[:2]:
            if isinstance(arg, str) and arg.startswith("/") and not Path(arg).exists():
                entry["missing_paths"].append(arg)
        if entry["missing_paths"]:
            stale.append(label)
        inspected.append(entry)

    if not stale:
        return GateResult(ok=True, details={"inspected": inspected, "stale_plists": []}), []

    bot_csv = ",".join(label.removeprefix("ai.openclaw.").removesuffix("-gateway") for label in stale)
    return (
        GateResult(ok=False, details={"inspected": inspected, "stale_plists": stale}),
        [Requirement(
            id="regenerate-gateway-plists",
            summary=f"Gateway plists {stale} reference paths that don't exist on disk.",
            remediation=f"Regenerate the plists before upgrading: `sudo evolve-admin deploy --bot={bot_csv}` (or remove and re-deploy each affected bot).",
            blocking=True, source_gate="plist_paths",
        )],
    )


def _gate_gateway_units(network: dict[str, Any]) -> tuple[GateResult, list[Requirement]]:
    """Linux gateway-existence check via the Scheduler seam.

    For each bot's gateway label, ask ``get_scheduler().status(label)`` —
    the systemd adapter answers ``installed`` (the ``.service`` unit file is
    on disk; a sudo-free ``Path.exists()`` so it is authoritative even when
    the ``systemctl show`` probe can't escalate) and ``managed`` (systemd has
    the unit loaded). Buckets:

    - **unit file absent** (``installed`` False) → determinate, authoritative
      finding (``Path.exists`` needs no sudo) → blocking; redeploy regenerates
      it. This is the systemd analogue of the macOS "plist does not exist".
    - **unit file present but conclusively NOT loaded** (``managed`` False
      with no ``status_error``) → determinate finding → blocking; redeploy
      runs ``daemon-reload``.
    - **unit file present, load state UNCONFIRMED** (``status_error`` — e.g.
      the admin daemon's ``sudo systemctl show`` lacked a non-interactive
      grant) → PASS. The unit file is on disk (the gateway-existence
      guarantee this gate provides), and a missing diagnostic capability
      must not gate the upgrade (#3188 principle). The unconfirmed state is
      recorded in ``details`` for legibility but raises no per-run
      requirement — the daemon hits this on every probe, so a requirement
      here would be permanent noise. The port-owner gate is the separate
      "is it actually listening" check.
    - **unit loaded** → pass.
    """
    from .ocadmin import _bot_ids, _bot_service  # local import: avoid cycle
    from .runtime import get_scheduler  # shim → runtime.scheduler singleton

    sched = get_scheduler()
    inspected: list[dict[str, Any]] = []
    missing: list[str] = []              # determinate: absent or not-loaded
    load_unconfirmed: list[str] = []     # file present, load state unprobeable

    for bot_id in _bot_ids(network):
        label = _bot_service(bot_id)
        st = sched.status(label)
        entry = {
            "bot_id": bot_id, "label": label,
            "installed": bool(st.get("installed")),
            "managed": bool(st.get("managed")),
            "status_error": st.get("status_error"),
        }
        if not st.get("installed"):
            # Path.exists() is sudo-free and authoritative — a genuinely
            # absent unit file, regardless of any status_error on the probe.
            entry["error"] = "systemd unit file not found on disk"
            missing.append(label)
        elif st.get("status_error"):
            # File present; couldn't confirm it is loaded — pass, but record
            # the degraded probe in details (no noisy per-run requirement).
            entry["note"] = "unit file present; load state unconfirmed (probe could not escalate)"
            load_unconfirmed.append(label)
        elif not st.get("managed"):
            # File present but systemd doesn't know it (needs daemon-reload).
            entry["error"] = "systemd unit on disk but not loaded"
            missing.append(label)
        inspected.append(entry)

    if not missing:
        return (
            GateResult(ok=True, details={"inspected": inspected, "missing_units": [],
                                         "load_unconfirmed_units": load_unconfirmed}),
            [],
        )

    bot_csv = ",".join(
        label.removeprefix("ai.openclaw.").removesuffix("-gateway") for label in missing
    )
    return (
        GateResult(
            ok=False,
            details={"inspected": inspected, "missing_units": missing,
                     "load_unconfirmed_units": load_unconfirmed},
        ),
        [Requirement(
            id="regenerate-gateway-units",
            summary=f"Gateway systemd units {missing} are missing or not loaded.",
            remediation=(
                f"Regenerate and reload the units before upgrading: "
                f"`sudo evolve-admin deploy --bot={bot_csv}` (writes the unit "
                f"set and runs daemon-reload). Confirm with "
                f"`systemctl status {missing[0]}.service`."
            ),
            blocking=True, source_gate="plist_paths",
        )],
    )


def gate_port_owners(network: dict[str, Any]) -> tuple[GateResult, list[Requirement]]:
    """For each bot's expected gateway port, confirm a listener exists and
    belongs to the expected user. Three failure modes are reported as
    separate Requirements so the remediation matches the actual cause:

    - no listener  → gateway daemon stopped (fix: restart-gateways)
    - wrong owner  → stray daemon claimed the port (fix: investigate)
    - probe error  → no port configured / lsof failed / sudo grant missing

    lsof is invoked via `sudo -n` because the admin server runs as the
    `evolve` user and macOS lsof from a non-root user cannot see TCP
    sockets owned by other users — every bot gateway runs under its own
    user, so an unprivileged probe would falsely report "no listener" for
    every bot. The sudoers grant lives in /etc/sudoers.d/evolve.

    The lsof binary path comes from ``get_profile().lsof`` (``/usr/sbin/lsof``
    on macOS, ``/usr/bin/lsof`` on Linux) so the invoked argv and the
    rendered sudoers grant — which derives the same path from the same
    profile table — can never drift. Hardcoding the macOS path made the
    Linux probe call ``sudo -n /usr/sbin/lsof`` while the grant covered
    ``/usr/bin/lsof``: no match, so ``sudo -n`` failed with "a password is
    required" on every Linux pod and the probe error read as a hard blocker.

    A probe that cannot DETERMINE the listener (lsof missing, sudo grant
    absent, no port configured) is reported as an inconclusive,
    NON-blocking note — a missing diagnostic capability must not gate the
    upgrade (same principle as the infra-audit non-fatal sudoers read).
    Only a determinate finding (no listener / wrong owner) blocks.
    """
    from platform_profile import get_profile

    from .config import get_bot_port
    from .ocadmin import _bot_ids, _gateway_runtime_user as _bot_user

    lsof_bin = get_profile().lsof

    ports: list[dict[str, Any]] = []
    no_listener: list[str] = []
    wrong_owner: list[str] = []
    probe_error: list[str] = []

    for bot_id in _bot_ids(network):
        port = get_bot_port(bot_id, network)
        expected_user = _bot_user(bot_id, network)
        info: dict[str, Any] = {
            "bot_id": bot_id, "port": port, "expected_user": expected_user,
            "listener_user": None, "listener_pid": None, "ok": False, "error": None,
        }
        if not port:
            info["error"] = "no port configured for bot"
            ports.append(info)
            probe_error.append(f"{bot_id}: no port in network config")
            continue

        try:
            r = subprocess.run(
                ["sudo", "-n", lsof_bin, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpcun"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as e:
            info["error"] = f"lsof failed: {e}"
            ports.append(info)
            probe_error.append(f"{bot_id}: lsof failed ({e})")
            continue

        # sudo -n failure (missing grant, password required) — distinct from
        # "no listener". sudo prefixes its own errors with "sudo:" on stderr.
        if r.stderr.startswith("sudo:"):
            err = r.stderr.strip().splitlines()[0]
            info["error"] = f"sudo lsof failed: {err}"
            ports.append(info)
            probe_error.append(f"{bot_id}: sudo lsof failed ({err})")
            continue

        if r.returncode != 0 or not r.stdout.strip():
            info["error"] = "no listener on port"
            ports.append(info)
            no_listener.append(f"{bot_id} (port {port})")
            continue

        # Parse lsof -F output: lines start with field codes (p=pid, c=command, u=user-uid, n=name)
        actual_user = None
        actual_pid = None
        for line in r.stdout.splitlines():
            if line.startswith("p"):
                actual_pid = line[1:]
            elif line.startswith("u"):
                # u<uid>; resolve to username
                try:
                    import pwd
                    actual_user = pwd.getpwuid(int(line[1:])).pw_name
                except (KeyError, ValueError):
                    actual_user = line[1:]
        info["listener_user"] = actual_user
        info["listener_pid"] = actual_pid
        if actual_user == expected_user:
            info["ok"] = True
        else:
            info["error"] = f"listener user {actual_user!r} != expected {expected_user!r}"
            wrong_owner.append(f"port {port} ({bot_id}): owned by {actual_user!r}, expected {expected_user!r}")
        ports.append(info)

    # Only determinate findings (gateway down / stray owner) are failures;
    # an undeterminable probe is inconclusive and does not flip the gate.
    determinate = bool(no_listener or wrong_owner)
    if not (determinate or probe_error):
        return GateResult(ok=True, details={"ports": ports}), []

    requirements: list[Requirement] = []
    if no_listener:
        bot_csv = ",".join(s.split(" ", 1)[0] for s in no_listener)
        requirements.append(Requirement(
            id="port-owners-no-listener",
            summary=f"Gateway not running for {', '.join(no_listener)}.",
            remediation=(
                f"Start the gateways before upgrading: `sudo evolve-admin restart-gateways {bot_csv}`. "
                f"If that fails the LaunchDaemon may not be bootstrapped — redeploy with "
                f"`sudo evolve-admin deploy --bot={bot_csv}`."
            ),
            blocking=True, source_gate="port_owners",
        ))
    if wrong_owner:
        requirements.append(Requirement(
            id="port-owners-mismatch",
            summary=f"Gateway ports owned by unexpected user: {'; '.join(wrong_owner)}.",
            remediation=(
                "A stray daemon or user-level agent has claimed the port. "
                "Investigate with `sudo evolve-admin menu processes` and stop the rogue process before upgrading."
            ),
            blocking=True, source_gate="port_owners",
        ))
    if probe_error:
        requirements.append(Requirement(
            id="port-owners-probe-error",
            summary=f"Could not determine gateway port listener (probe unavailable): {'; '.join(probe_error)}.",
            remediation=(
                "This is a degraded diagnostic, NOT a blocker — the gateways may "
                "well be healthy; the preflight just couldn't confirm the port "
                "listener. If the error mentions `sudo:` (missing sudoers grant), "
                "run `sudo evolve-admin refresh-sudoers` to install the latest "
                "grants, then re-run the preflight for a conclusive answer."
            ),
            blocking=False, source_gate="port_owners",
        ))

    # ok reflects determinate health: an inconclusive probe alone keeps the
    # gate green (degraded note), so a missing capability can't gate the upgrade.
    return GateResult(ok=not determinate, details={"ports": ports}), requirements


# ── Bot-config schema gate ──────────────────────────────────────────────────


def gate_bot_config_schema(
    network: dict[str, Any],
) -> tuple[GateResult, list[Requirement]]:
    """Validate every bot's openclaw.json against the *currently installed*
    OpenClaw's schema.

    Why this gate exists
    --------------------
    The six prior gates (node version, tarball integrity, plugin
    references, LaunchAgents, plist paths, ports) catch infrastructure
    drift. They do NOT catch the case where a bot's openclaw.json
    contains a field shape that the current OC schema rejects — the
    failure mode that crash-looped six gateways in the
    ``models.providers[<provider>].models[].name: Invalid input``
    incident: an in-flight backfill wrote schema-invalid entries that
    OC's startup validator rejected, and every gateway then crashed-
    looped at ~5s intervals.

    This gate runs OC's own validator (``openclaw config validate
    --json``) against every bot's config and flags anything that fails.
    Treat the failure as ADVISORY for the *upgrade* decision — the
    config was already invalid before any upgrade, so blocking the
    upgrade doesn't fix it. But for the *operator visibility* surface,
    knowing which bots are running invalid configs is exactly the
    signal they need to act on (revert, redeploy, repair) before
    something else trips on it.

    Caller semantics
    ----------------
    Returns ``Requirement`` records with ``blocking=False`` — they are
    informational warnings rather than hard blockers. The
    ``GateResult`` itself reports ``ok=False`` only if at least one
    bot's config is invalid AND its validator ran cleanly (i.e. we
    have a high-confidence "invalid" rather than a "couldn't check").
    Validator-can't-run conditions (missing binary, missing file,
    timeout) are reported on the per-bot detail but do not flip
    ``ok=False`` — separating "we know it's broken" from "we don't
    know" matches the policy in
    ``openclaw_config_validator.validate_all_bots``.

    Best-effort
    -----------
    Never raises into the caller. A surprise import failure or
    catastrophic subprocess error degrades gracefully to a "couldn't
    check" outcome with the error recorded in ``details``.
    """
    from .ocadmin import _bot_ids  # local import to avoid cycle
    try:
        from .openclaw_config_validator import validate_bot_openclaw_json
    except Exception as exc:
        return (
            GateResult(
                ok=True,  # ok=True under "couldn't check" semantics — no false alarm
                details={
                    "error": f"could not import validator: {exc}",
                    "checked": 0, "invalid": [], "errored": [],
                },
            ),
            [],
        )

    invalid: list[dict[str, Any]] = []
    errored: list[dict[str, Any]] = []
    checked = 0
    for bot_id in _bot_ids(network):
        try:
            valid, issues, val_err = validate_bot_openclaw_json(bot_id, network)
        except Exception as exc:
            errored.append({"bot_id": bot_id, "error": f"validator raised: {exc}"})
            continue
        checked += 1
        if val_err:
            errored.append({"bot_id": bot_id, "error": val_err})
            continue
        if not valid:
            invalid.append({
                "bot_id": bot_id,
                "issues": issues[:20],  # cap for report size; full list available in logs
                "issue_count": len(issues),
            })

    details: dict[str, Any] = {
        "checked": checked,
        "invalid": invalid,
        "errored": errored,
    }

    requirements: list[Requirement] = []
    for bot in invalid:
        bot_id = bot["bot_id"]
        # Format the first few issues for the operator-visible summary.
        issues_preview = "; ".join(
            f"{it.get('path')}: {it.get('message')}"
            for it in bot["issues"][:3]
        )
        more = (
            f" (+{bot['issue_count'] - 3} more)"
            if bot["issue_count"] > 3 else ""
        )
        requirements.append(Requirement(
            id=f"bot-config-schema-{bot_id}",
            summary=(
                f"Bot {bot_id} has an invalid openclaw.json under the current "
                f"OC schema — gateway will crash-loop on next reload. "
                f"Issues: {issues_preview}{more}"
            ),
            remediation=(
                f"Inspect /Users/<bot>/.openclaw/openclaw.json for {bot_id}; "
                f"either fix in place, restore openclaw.json.bak, or run "
                f"`sudo evolve-admin deploy {bot_id}` to rewrite from defaults. "
                f"Then re-run the safe-upgrade check."
            ),
            blocking=False,  # advisory — does not block the upgrade itself
            source_gate="bot_config_schema",
        ))

    # ok=False ONLY when we have at least one high-confidence invalid bot.
    # "Couldn't check" outcomes (validator errors) leave ok=True so we
    # don't false-alarm the operator on infrastructure issues that are
    # unrelated to upgrade safety.
    ok = len(invalid) == 0
    return GateResult(ok=ok, details=details), requirements


# ── Gate: send_surface ───────────────────────────────────────────────────────
#
# The message-send surface every gallery app delivers through
# (docs/spec-gallery-delivery-convention-2026-06-11.md §3):
#
#   openclaw message send --channel=<ch> --target=<t> --message=<text> --json
#
# OpenClaw 2026.6.1 removed the previous delivery surface (the gateway's
# POST /api/message) with no release-note entry, and every scheduled
# user-facing send pod-wide died silently for 8 days (the 2026-06-11 P0,
# docs/decision-add-bot-m4-u1-proof-2026-06-11.md §"The 07:00 window").
# Upstream documents `message send` (docs.openclaw.ai/cli/message) but
# states no stability contract, so the surface must be re-proven around
# every upgrade: this gate establishes the baseline on the INSTALLED
# version, and ocadmin re-runs the same probe after the install so a
# baseline-ok → post-fail flip is loud, attributable evidence that the
# upgrade broke delivery.

SEND_SURFACE_OK = "ok"
SEND_SURFACE_FAILED = "failed"
SEND_SURFACE_UNVERIFIED = "unverified"

# The flags every embedded gallery send helper passes — the contract the
# probe asserts on. If upstream renames or drops one, scheduled sends
# break even though the subcommand still exists.
SEND_SURFACE_REQUIRED_FLAGS = ("--channel", "--target", "--message", "--json")

_SEND_SURFACE_PROBE_TIMEOUT = 30

# Single-sourced from platform_profile (which()-first + the six fixed
# candidates for launchd/sudo contexts whose minimal PATH misses Homebrew).
# The list itself lives in exactly one place — do NOT re-copy it. The
# historical module-level name ``OPENCLAW_CLI_CANDIDATES`` is preserved as a
# lazy re-export (module __getattr__ below) so any back-compat reference keeps
# working without forcing a top-level platform_profile import (this module
# imports it lazily, inside functions, by design).


def _find_openclaw_cli() -> str | None:
    from platform_profile import find_openclaw_cli
    return find_openclaw_cli()


def __getattr__(name: str):
    # Lazy re-export of the single-sourced candidate tuple. PEP 562.
    if name == "OPENCLAW_CLI_CANDIDATES":
        from platform_profile import OPENCLAW_CLI_CANDIDATES
        return OPENCLAW_CLI_CANDIDATES
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _run_send_surface_cmd(
    argv: list[str], env: dict[str, str],
) -> tuple[int, str, str]:
    """The probe's only subprocess call site (injectable seam for tests).

    cwd=/tmp: node calls uv_cwd() at startup, and the caller may be root
    (sudo evolve-admin) or the evolve user inside a directory the CLI
    can't traverse — /tmp is readable by everyone.
    """
    proc = subprocess.run(
        argv, capture_output=True, text=True,
        timeout=_SEND_SURFACE_PROBE_TIMEOUT, cwd="/tmp", env=env,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def probe_send_surface(
    *,
    runner: Callable[[list[str], dict[str, str]], tuple[int, str, str]] | None = None,
    cli_path: str | None = None,
) -> dict[str, Any]:
    """Probe the `openclaw message send` surface of the installed CLI.

    Tri-state — ``status`` is ``ok`` | ``failed`` | ``unverified``. A
    probe that cannot run reports ``unverified``, never ``ok`` (the
    delivery-monitor tri-state rule). ``failed`` means the surface the
    gallery delivery convention depends on is positively broken: the CLI
    is missing, the subcommand is gone, or a required flag disappeared
    from its advertised contract.

    This is the tightest honest proxy that needs no bot identity and
    sends nothing: ``openclaw message send --help`` must exit 0 and
    still advertise every flag the embedded gallery helpers pass. The
    end-to-end variant (a real send) runs once per upgrade from
    ocadmin's post-install verification, not here — preflight runs from
    the admin server as the evolve user, which cannot act as a bot.
    """
    run = runner or _run_send_surface_cmd
    cli = cli_path or _find_openclaw_cli()
    result: dict[str, Any] = {
        "status": SEND_SURFACE_UNVERIFIED,
        "cli_path": cli,
        "required_flags": list(SEND_SURFACE_REQUIRED_FLAGS),
        "missing_flags": [],
        "reason": None,
        "rc": None,
        "output_excerpt": None,
    }
    if cli is None:
        result["status"] = SEND_SURFACE_FAILED
        result["reason"] = "cli_not_found"
        return result
    # launchd/sudo contexts get a minimal PATH; the CLI's
    # `#!/usr/bin/env node` shebang needs the CLI's own bin dir (where
    # node also lives) prepended — same fix as the gallery helpers.
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(cli) + os.pathsep + env.get("PATH", "")
    try:
        rc, out, err = run([cli, "message", "send", "--help"], env)
    except subprocess.TimeoutExpired:
        result["reason"] = "probe_timeout"
        return result
    except OSError as e:
        result["reason"] = f"probe_error: {e}"
        return result
    result["rc"] = rc
    combined = f"{out}\n{err}"
    result["output_excerpt"] = combined.strip()[:400]
    if rc != 0:
        result["status"] = SEND_SURFACE_FAILED
        result["reason"] = "help_exited_nonzero"
        return result
    missing = [f for f in SEND_SURFACE_REQUIRED_FLAGS if f not in combined]
    if missing:
        result["status"] = SEND_SURFACE_FAILED
        result["reason"] = "contract_flags_missing"
        result["missing_flags"] = missing
        return result
    result["status"] = SEND_SURFACE_OK
    return result


def gate_send_surface() -> tuple[GateResult, list[Requirement]]:
    """Gate 8: the delivery surface scheduled user-facing apps send
    through must be verifiably working on the INSTALLED OpenClaw before
    an upgrade is accepted.

    Upgrading from a broken-or-unknown baseline destroys the only
    attribution evidence: after the install nobody can tell "the upgrade
    broke delivery" from "it was already broken". Tri-state mapping:

      ok          → baseline established; the post-upgrade re-probe can
                    attribute any flip to the upgrade.
      failed      → blocking requirement (--force exists for the case
                    where the upgrade itself is the fix).
      unverified  → ok=False + non-blocking advisory — "couldn't verify"
                    is reported as exactly that, never as a pass.
    """
    probe = probe_send_surface()
    if probe["status"] == SEND_SURFACE_OK:
        return GateResult(ok=True, details=probe), []
    if probe["status"] == SEND_SURFACE_FAILED:
        detail = probe["reason"]
        if probe["missing_flags"]:
            detail = f"flags gone from `message send`: {', '.join(probe['missing_flags'])}"
        elif detail == "cli_not_found":
            detail = "the openclaw CLI is not on this machine's expected paths"
        elif detail == "help_exited_nonzero":
            detail = f"`openclaw message send --help` exited {probe['rc']}"
        req = Requirement(
            id="send-surface-broken",
            summary=(
                "The message-send surface scheduled apps deliver through is "
                f"broken on the installed OpenClaw ({detail})."
            ),
            remediation=(
                "Fix delivery on the current version first — upgrading now "
                "destroys the evidence of what broke it. Probe by hand: "
                "`openclaw message send --help`. If this upgrade is itself "
                "the fix, pass --force."
            ),
            blocking=True,
            source_gate="send_surface",
        )
        return GateResult(ok=False, details=probe), [req]
    req = Requirement(
        id="send-surface-unverified",
        summary=(
            "Couldn't verify the message-send surface on the installed "
            f"OpenClaw ({probe['reason']}) — it is NOT confirmed working."
        ),
        remediation=(
            "Re-run the check; if it stays unverified, probe by hand: "
            "`openclaw message send --help` (as any user, from /tmp)."
        ),
        blocking=False,
        source_gate="send_surface",
    )
    return GateResult(ok=False, details=probe), [req]


# ── Gate: oc_data_dependencies ───────────────────────────────────────────────
#
# The seven gates above encode named failure shapes scoped to plugins +
# gateway infra. None of them exercise the data files Evolve reads OUT OF
# BAND from OpenClaw — so when OpenClaw migrated auth-profiles.json into a
# per-agent SQLite store on 2026-06-22, this preflight returned SAFE while the
# app scanner was already failing pod-wide with error_kind=missing_api_key.
#
# This gate runs the REAL Evolve→OpenClaw readers (the manifest in
# evolve_admin.oc_deps) against the live pod and fails the instant one stops
# resolving. The auth_store entry is the dependency that broke; it would have
# fired the moment the auth migration landed.


def gate_oc_data_dependencies(
    network: dict[str, Any],
) -> tuple[GateResult, list[Requirement]]:
    """Gate 9: exercise every real Evolve→OpenClaw out-of-band reader.

    Iterates ``oc_deps.OC_DEPENDENCIES``; per-bot dependencies run once per
    bot, pod-scope ones run once. Verdict routing:

      ok / not_applicable → PASS (no finding)
      missing / empty     → blocking requirement when the dependency is
                            ``load_bearing`` (the upgrade is refused),
                            non-blocking WARN otherwise
      indeterminate       → non-blocking WARN (a tooling fault is reported as
                            exactly that, never as a hard block)

    The ``GateResult`` reports ``ok=False`` only when at least one BLOCKING
    requirement was raised — WARNs (indeterminate / non-load-bearing) leave
    ``ok=True`` so a permission blip or a gracefully-degrading dependency
    never false-alarms the operator. Never raises into the caller: a probe
    that blows up degrades to an indeterminate WARN for that (dep, bot).
    """
    from .ocadmin import _bot_ids  # local import to avoid cycle
    from . import oc_deps

    bot_ids = _bot_ids(network)
    results: list[dict[str, Any]] = []
    requirements: list[Requirement] = []

    for dep in oc_deps.OC_DEPENDENCIES:
        targets: list[str | None] = (
            list(bot_ids) if dep.scope == oc_deps.SCOPE_PER_BOT else [None]
        )
        for bot_id in targets:
            try:
                pr = dep.probe(bot_id, network=network)
            except Exception as exc:  # noqa: BLE001 — a probe must never wedge the gate
                pr = oc_deps.ProbeResult(
                    oc_deps.PROBE_INDETERMINATE, f"probe raised: {exc}"
                )
            results.append({
                "key": dep.key,
                "bot_id": bot_id,
                "scope": dep.scope,
                "status": pr.status,
                "detail": pr.detail,
            })
            if pr.status in (oc_deps.PROBE_MISSING, oc_deps.PROBE_EMPTY):
                where = f" on {bot_id}" if bot_id else ""
                blocking = dep.load_bearing
                requirements.append(Requirement(
                    id=f"oc-dep-{dep.key}{('-' + bot_id) if bot_id else ''}",
                    summary=(
                        f"Evolve's {dep.key} dependency is broken{where}: "
                        f"{pr.detail}. {dep.why}"
                    ),
                    remediation=(
                        "An OpenClaw artifact Evolve reads out-of-band has drifted. "
                        f"Inspect: {dep.artifact}. If this upgrade is itself the fix, "
                        "pass --force; otherwise repair the reader before upgrading."
                    ),
                    blocking=blocking,
                    source_gate="oc_data_dependencies",
                ))
            elif pr.status == oc_deps.PROBE_INDETERMINATE:
                where = f" on {bot_id}" if bot_id else ""
                requirements.append(Requirement(
                    id=f"oc-dep-{dep.key}-unverified{('-' + bot_id) if bot_id else ''}",
                    summary=(
                        f"Couldn't verify Evolve's {dep.key} dependency{where}: "
                        f"{pr.detail} — it is NOT confirmed working."
                    ),
                    remediation=(
                        f"Re-run the check. If it stays unverified, inspect by hand: "
                        f"{dep.artifact}."
                    ),
                    blocking=False,
                    source_gate="oc_data_dependencies",
                ))

    details: dict[str, Any] = {
        "checked_bots": list(bot_ids),
        "dependencies": [d.key for d in oc_deps.OC_DEPENDENCIES],
        "results": results,
    }
    ok = not any(r.blocking for r in requirements)
    return GateResult(ok=ok, details=details), requirements


# ── Orchestrator ─────────────────────────────────────────────────────────────

GATE_ORDER = [
    "node_version",
    "stub_install",
    "config_references",
    "user_launchagents",
    "plist_paths",
    "port_owners",
    "send_surface",
    "bot_config_schema",
    "oc_data_dependencies",
]


def run_preflight(
    target_spec: str = "latest",
    *,
    network: dict[str, Any] | None = None,
    shared_dir: Path | None = None,
    persist: bool = True,
) -> Report:
    """Run the safe-upgrade gates against the candidate target and (optionally) persist
    the report to /Users/Shared/evolve/safe-upgrade/reports/.

    target_spec: 'latest' or a concrete version like '2026.4.15'.
    network:     pre-loaded network config (else loaded from default path).
    shared_dir:  override for tests; defaults to /Users/Shared/evolve.
    persist:     write report JSON + update latest.json + retention sweep.
    """
    started = time.monotonic()
    if network is None:
        try:
            network = load_network()
        except Exception:
            network = {"members": [], "bots": {}}

    # Resolve candidate metadata once — most gates need it
    metadata: dict[str, Any] = {}
    fetch_error: str | None = None
    try:
        metadata = _fetch_registry_metadata(target_spec)
    except urllib.error.URLError as e:
        fetch_error = f"npm registry unreachable: {e.reason}"
    except (urllib.error.HTTPError, json.JSONDecodeError) as e:
        fetch_error = f"could not parse registry response: {e}"
    except Exception as e:
        fetch_error = f"unexpected registry error: {e}"

    installed = _installed_version()

    candidate = {
        "target_spec": target_spec,
        "resolved_version": metadata.get("version") if isinstance(metadata, dict) else None,
        "registry_url": f"{OPENCLAW_REGISTRY_BASE}/{target_spec}",
        "fetch_error": fetch_error,
    }
    current = {
        "installed_version": installed,
        "node_version": _node_version(),
    }

    gates: dict[str, GateResult] = {}
    requirements: list[Requirement] = []

    if fetch_error:
        # Registry-dependent gates fail closed; others can still run
        gates["node_version"] = GateResult(
            ok=False,
            details={"current": current["node_version"], "required": None, "error": fetch_error},
        )
        requirements.append(Requirement(
            id="registry-unreachable",
            summary=f"Could not reach npm registry: {fetch_error}",
            remediation="Check network connectivity and re-run the check.",
            blocking=True, source_gate="node_version",
        ))
        gates["stub_install"] = GateResult(
            ok=False, details={"error": fetch_error, "resolved_version": None},
        )
        gates["config_references"] = GateResult(
            ok=False, details={"error": fetch_error, "candidate_plugins": [], "bots": []},
        )
    else:
        nv_result, nv_reqs = gate_node_version(metadata)
        gates["node_version"] = nv_result
        requirements.extend(nv_reqs)

        si_result, si_reqs = gate_stub_install(metadata, installed)
        gates["stub_install"] = si_result
        requirements.extend(si_reqs)

        cr_result, cr_reqs = gate_config_references(metadata, network)
        gates["config_references"] = cr_result
        requirements.extend(cr_reqs)

    ula_result, ula_reqs = gate_user_launchagents(network)
    gates["user_launchagents"] = ula_result
    requirements.extend(ula_reqs)

    pp_result, pp_reqs = gate_plist_paths(network)
    gates["plist_paths"] = pp_result
    requirements.extend(pp_reqs)

    po_result, po_reqs = gate_port_owners(network)
    gates["port_owners"] = po_result
    requirements.extend(po_reqs)

    # The send-surface gate probes the INSTALLED CLI (registry-independent)
    # — the upgrade must start from a proven delivery baseline so the
    # post-install re-probe in ocadmin can attribute a flip to the upgrade.
    ss_result, ss_reqs = gate_send_surface()
    gates["send_surface"] = ss_result
    requirements.extend(ss_reqs)

    # The bot-config-schema gate runs OC's validator against every bot
    # openclaw.json under the currently installed OC. Its requirements are
    # advisory (blocking=False) — invalid configs were already broken
    # before any upgrade, so they don't block the upgrade itself, but the
    # operator needs to see them since they're guaranteed crash-loop
    # candidates on the next gateway reload.
    bcs_result, bcs_reqs = gate_bot_config_schema(network)
    gates["bot_config_schema"] = bcs_result
    requirements.extend(bcs_reqs)

    # The OC-data-dependency gate runs the REAL Evolve→OpenClaw out-of-band
    # readers (oc_deps manifest) against the live pod. Registry-independent —
    # it probes installed data files, not the candidate. This is the gate that
    # would have fired the instant the 2026-06-22 auth-store migration landed.
    ocd_result, ocd_reqs = gate_oc_data_dependencies(network)
    gates["oc_data_dependencies"] = ocd_result
    requirements.extend(ocd_reqs)

    # Re-order gates dict to canonical order
    gates = {k: gates[k] for k in GATE_ORDER if k in gates}

    blockers = [r for r in requirements if r.blocking]
    overall_ok = len(blockers) == 0
    if overall_ok:
        summary = "All gates passed — safe to upgrade."
    elif len(blockers) == 1:
        summary = "1 blocker must be resolved first"
    else:
        summary = f"{len(blockers)} blockers must be resolved first"

    duration_ms = int((time.monotonic() - started) * 1000)
    report = Report(
        report_id=_new_report_id(),
        checked_at=_iso(_utcnow()),
        duration_ms=duration_ms,
        candidate=candidate,
        current=current,
        ok=overall_ok,
        summary=summary,
        gates=gates,
        requirements=requirements,
    )

    if persist:
        root = reports_dir(shared_dir)
        root.mkdir(parents=True, exist_ok=True)
        # mode=0o644: reports may be written under sudo (root) but are read
        # by the admin server running as the evolve user.
        _atomic_write_json(root / f"{report.report_id}.json", report.to_json(), mode=0o644)
        _update_latest_symlink(root, report.report_id)
        _retention_sweep(root)

    return report


# ── Background-thread runner (for the HTTP POST endpoint) ────────────────────

def start_background_check(
    target_spec: str = "latest",
    *,
    network: dict[str, Any] | None = None,
    shared_dir: Path | None = None,
) -> tuple[str, str]:
    """Kick off a preflight on a background thread.

    Returns (report_id, status). If a check is already running, returns the
    in-flight report_id with status='running' and does not start a new one.
    """
    global _inflight_id
    with _inflight_lock:
        if _inflight_id is not None:
            return _inflight_id, "running"
        rid = _new_report_id()
        _inflight_id = rid

    def _worker(report_id: str) -> None:
        global _inflight_id
        try:
            # We pre-allocated the report_id so callers can poll for it.
            started = time.monotonic()
            report = run_preflight(
                target_spec, network=network, shared_dir=shared_dir, persist=False,
            )
            # Replace the auto-generated id with our pre-allocated one
            report.report_id = report_id
            report.duration_ms = int((time.monotonic() - started) * 1000)
            root = reports_dir(shared_dir)
            root.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(root / f"{report_id}.json", report.to_json(), mode=0o644)
            _update_latest_symlink(root, report_id)
            _retention_sweep(root)
        finally:
            with _inflight_lock:
                _inflight_id = None

    t = threading.Thread(target=_worker, args=(rid,), daemon=True)
    t.start()
    return rid, "running"


# ── Report read helpers (used by HTTP) ───────────────────────────────────────

def load_report(report_id: str, shared_dir: Path | None = None) -> dict[str, Any] | None:
    path = reports_dir(shared_dir) / f"{report_id}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def load_latest_report(shared_dir: Path | None = None) -> dict[str, Any] | None:
    root = reports_dir(shared_dir)
    link = root / "latest.json"
    if link.exists():
        try:
            return json.loads(link.read_text())
        except (OSError, ValueError):
            pass
    # Fall back to most-recent-by-name
    try:
        files = sorted(
            (p for p in root.glob("*.json") if p.name != "latest.json"),
            reverse=True,
        )
    except OSError:
        return None
    if not files:
        return None
    try:
        return json.loads(files[0].read_text())
    except (OSError, ValueError):
        return None
