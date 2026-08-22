"""
Flask web server for the Evolve admin UI.
Binds to 127.0.0.1 only — never expose externally.

Routes:
  GET  /                      → Admin dashboard HTML
  GET  /api/status            → Network status (bots, live health, metrics)
  GET  /api/network           → Raw network config
  GET  /api/setup-status      → First-time-setup detection (has_primary, etc.)
  POST /api/deploy            → Deploy/update a bot
  POST /api/remove            → DEPRECATED — returns 410; use /api/lifecycle/{detach,retire,delete}
  POST /api/setup-shared      → Create shared directory
  POST /api/config            → Update thresholds/alerts/primary
  GET  /api/trust             → Per-module validation status (evidence-based)
  GET  /api/heartbeat/check   → Should-alert check for silence-first heartbeats
  GET  /api/accounts/status   → Auth profile routing status per bot
  GET  /api/host-health       → Host machine metrics (CPU, memory, disk, load, uptime)
  GET  /api/pod-health        → Full pod health scan (permissions, services, gateways)
  POST /api/pod-health/fix    → Apply fixable issues from health scan
"""

from __future__ import annotations

import copy
import errno
import fcntl
import json
import os
import re
import subprocess

from platform_profile import get_profile
import sys
import threading
import time as _time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from evolve_util import now_iso as _now_iso

from ..runtime import get_scheduler
from .. import external_ids as _external_ids
from .http_errors import error_response, log_request_error
from .sw_assets import render_sw_js

# Scan status tracking: bot_id → {status, count}
_scan_status: dict = {}
# Security audit cache: aliased to evolve_admin.audit_state._state so
# pod_state.audits.list_audits can read the same dict this module
# writes. See evolve_admin/audit_state.py for the rationale —
# essentially "single source of truth without a circular import."
# Writes through _audit_cache["data"][bot_id] = result land in the
# shared module automatically because this is an alias, not a copy.
from .. import audit_state as _audit_state, deploy_resilience as _dres
_audit_cache: dict = _audit_state._state
# Background audit job state: {running: bool, bots: list, done: int, total: int, started_at: float}
_audit_job: dict = {"running": False, "bots": [], "done": 0, "total": 0, "started_at": 0.0}
_audit_job_lock = threading.Lock()
# API key status cache: {bot_id: (timestamp, key_status_dict)} — refreshed every 5 min
_key_status_cache: dict = {}
# Server start time — used by /api/health to report uptime
_START_TIME: float = _time.time()

from flask import Flask, jsonify, redirect, request, send_from_directory, Response


def _import_analyzer(mod: str):
    """Import a module from the installed evolve-analyzer package."""
    import importlib
    return importlib.import_module(mod)

from ..config import load_network, save_network, DEFAULT_NETWORK_CONFIG, bot_home as _bot_home, get_bot_user, detect_system_timezone, resolve_pod_timezone
from . import admin_auth
from ..deploy import (
    add_bot, deploy_bot, deploy_shared_dir, DeployResult, read_install_json, write_install_json,
    EVOLVE_VERSION, get_bot_sync_status, find_orphaned_plists, remove_orphaned_plists, record_bot_deploy,
    build_plugin, fix_plugin_permissions, ensure_plugin_config, install_oc_plugin, ensure_workspace_git_init,
    ensure_pod_perms,
)
from ..status import network_status, setup_status
from ..telemetry import get_logger
from .routes_shared import _SECRET_KEY_NAMES, _REDACTED, _redact_secrets, _audit_log_entry  # re-exported: keep monkeypatch targets valid
from .probes import (
    DOTENV_PROVIDER_KEYS, MANIFEST_CATALOG, OPENCLAW_CHANNELS_FIELDS, Affordance, AuthProfilesTokenPairProbe,
    DotenvProbe, GhCliProbe, IntegrationTokenProbe, OpenclawChannelsTokenProbe, ProbeContext, ProbeHelpers,
    ProbeOutcome, SshKeyProbe, WizardAuthProfilesProbe, build_probes, envvar_for_provider_field,
    manifests_matching_provider,
)

# ── PWA manifest helpers ─────────────────────────────────────────────────────

# The wizard prompts for ``networkId`` with this placeholder; a user who
# hits Enter through setup ends up with it persisted as the literal pod
# name. We treat that case (plus an empty/whitespace value) as "no real
# name set" so the PWA manifest can render plain "Evolve" instead of the
# redundant "Evolve · my-pod" in the macOS Dock / Chrome standalone title.
_DEFAULT_POD_NAMES: frozenset[str] = frozenset({"my-pod", "evolve"})


def _is_default_or_empty(pod_name: str | None) -> bool:
    """Return True when the pod name is the wizard's default placeholder
    (case-insensitive, whitespace-trimmed) or empty/None.

    Used by ``/manifest.json`` to drop the per-pod suffix from the PWA
    name when the operator hasn't customised ``networkId``.
    """
    norm = (pod_name or "").strip().lower()
    return not norm or norm in _DEFAULT_POD_NAMES


# ── Background job registry ───────────────────────────────────────────────────
# Jobs are stored in-process. They survive server reloads but not restarts.
# TTL: 30 minutes after completion.

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_active_job_id: list[str] = []  # at most one element; list so threads can mutate it


def _new_job(job_type: str) -> str:
    import uuid
    job_id = f"{job_type}-{int(_time.time())}-{uuid.uuid4().hex[:6]}"
    with _jobs_lock:
        _jobs[job_id] = {
            "jobId": job_id,
            "type": job_type,
            "status": "running",
            "started_at": _time.strftime("%H:%M:%S"),
            "finished_at": None,
            "log": [],
            "progress": {"current": 0, "total": 0, "label": ""},
            "result": None,
            "error": None,
        }
        _active_job_id.clear()
        _active_job_id.append(job_id)
    return job_id


def _job_log(job_id: str, msg: str, level: str = "info") -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["log"].append({
                "t": _time.strftime("%H:%M:%S"),
                "level": level,
                "msg": msg,
            })


def _job_progress(job_id: str, current: int, total: int, label: str) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["progress"] = {"current": current, "total": total, "label": label}


def _job_finish(job_id: str, result: dict | None = None, error: str | None = None) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = "failed" if error else "complete"
            _jobs[job_id]["finished_at"] = _time.strftime("%H:%M:%S")
            _jobs[job_id]["result"] = result
            _jobs[job_id]["error"] = error
        if job_id in _active_job_id:
            _active_job_id.clear()
    # Evict stale jobs after 30 minutes (best-effort cleanup)
    _evict_stale_jobs()


def _evict_stale_jobs() -> None:
    cutoff = _time.time() - 1800  # 30 minutes
    with _jobs_lock:
        stale = [
            jid for jid, j in _jobs.items()
            if j["status"] in ("complete", "failed")
            and jid not in _active_job_id
            # Use started_at time string heuristic — good enough
            and (j.get("_ts", _time.time()) < cutoff)
        ]
        for jid in stale:
            del _jobs[jid]

_log = get_logger("web.server")


# ── Bot-workspace manifest helpers ────────────────────────────────────────────
# Manifests are canonical in the bot's own workspace:
#   <workspace>/manifests/{app_id}.json   (resolved via resolve_bot_paths)
# The admin server accesses them via `sudo -u <actual_user>` subprocess calls.

def _bot_manifests_dir(bot_id: str, user: str | None = None) -> Path:
    """Resolve manifests dir via resolve_bot_paths so non-standard home dirs work."""
    try:
        paths = resolve_bot_paths(bot_id, user=user)
        return Path(paths["workspace"]) / "manifests"
    except Exception:
        actual = user or bot_id
        try:
            import pwd as _pwd
            home = _pwd.getpwnam(actual).pw_dir
        except Exception:
            home = f"/Users/{actual}"
        return Path(home) / ".openclaw" / "workspace" / "manifests"


def _resolve_bot_user(bot_id: str, network_path: Path = DEFAULT_NETWORK_CONFIG) -> str:
    """Canonical bot_id→macOS-user lookup, self-loading network.json.

    Thin convenience over evolve_admin.config.get_bot_user for routes
    that only have a bot_id in hand; swallows load failures to bot_id
    (read-only display paths degrade, they don't 500)."""
    try:
        net = load_network(network_path)
        return get_bot_user(bot_id, net)
    except Exception:
        return bot_id


def _glob_manifests(dir_path: Path) -> list[str]:
    """Return manifest paths in ``dir_path`` — filters hidden + history files."""
    return [
        str(f) for f in dir_path.glob("*.json")
        if not f.name.startswith(".") and "_history" not in f.name
    ]


def _list_manifests_as_bot(bot_id: str, user: str | None = None) -> list[str]:
    """Return list of absolute path strings for *.json manifests for ``bot_id``.

    Reads from the canonical per-bot location at
    ``/Users/<bot>/.openclaw/workspace/manifests/``. The legacy shared-side
    fallback (``{shared_dir}/applications/<bot>/``) was removed in PR #1176
    when manifests became per-bot state — bot-side is now the only location.
    """
    u = user or _resolve_bot_user(bot_id)
    workspace_dir = _bot_manifests_dir(bot_id, user=u)

    # Direct glob — files are world-readable per the workspace ACL.
    try:
        wp = Path(str(workspace_dir))
        if wp.exists():
            return _glob_manifests(wp)
    except (PermissionError, OSError):
        pass

    # sudo ls fallback for the rare case where direct access fails — reuses the
    # granted `ls …/workspace/manifests` shape + _glob_manifests' filter in
    # Python; the previous `sudo find` argv had NO grant on either platform, so
    # this fallback silently returned [] (2026-07-29 VPS census: 17 denials/day).
    try:
        r = subprocess.run(["sudo", get_profile().ls, str(workspace_dir)],
                           capture_output=True, text=True, timeout=10,
                           cwd=get_profile().scratch_dir)
        if r.returncode == 0:
            return [str(Path(str(workspace_dir)) / n) for n in r.stdout.splitlines()
                    if n.endswith(".json") and not n.startswith(".") and "_history" not in n]
    except Exception:
        pass

    return []


def _read_manifest_as_bot(bot_id: str, app_id: str, user: str | None = None) -> dict | None:
    """Read a single manifest for ``bot_id`` / ``app_id``.

    Bot-side only since PR #1176 moved manifests to per-bot canonical
    storage.
    """
    u = user or _resolve_bot_user(bot_id)
    path = _bot_manifests_dir(bot_id, user=u) / f"{app_id}.json"

    try:
        if path.exists():
            return json.loads(path.read_text())
    except PermissionError:
        pass
    except Exception:
        return None

    try:
        result = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception:
        pass
    return None


def _find_manifest_by_id_field(bot_id: str, app_id: str, user: str | None = None) -> dict | None:
    """Resolve ``app_id`` by scanning manifests for a matching ``id`` /
    ``instance_id`` field. Fallback for the View endpoint when the
    filename stem != id, as happens on gallery-installed v7-arc-pre
    manifests where the file lands at ``<display-slug>.json`` while the
    internal ``id`` is ``app_<bot>_<slug>``."""
    u = user or _resolve_bot_user(bot_id)
    for mf_path in _list_manifests_as_bot(bot_id, user=u):
        try:
            try:
                data = json.loads(Path(mf_path).read_text())
            except PermissionError:
                r = subprocess.run(
                    ["sudo", "/bin/cat", mf_path],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode != 0:
                    continue
                data = json.loads(r.stdout)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("id") == app_id or data.get("instance_id") == app_id:  # identity: a permissive MATCHER, not a resolution — caller's id shape is unknown; resolve_app_id's one pick would 404 these manifests.
            return data
    return None


def _write_manifest_as_bot(bot_id: str, app_id: str, data: dict, user: str | None = None) -> bool:
    """Write a manifest to bot workspace.

    Prefers same-dir temp + rename: the plugin's Layer C trigger cache
    invalidates on the manifests *directory* mtime, and an in-place
    overwrite (copy2 / sudo cp) never bumps it — a status flip
    (pause/archive via _app_lifecycle) would sit unread by a running
    gateway until some unrelated dir-entry change. The rename also makes
    the write atomic. Mirrors ``unwire_event_triggers`` in
    applications/manifest.py.

    Falls back to /tmp staging + direct copy, then sudo /bin/cp as root,
    when evolve has no write ACL on the manifests dir yet (pre-scan
    bot). Those paths write in place — a running gateway serves stale
    triggers until the next dir-entry change or restart.
    """
    import tempfile as _tmpfile, os as _os
    u = user or _resolve_bot_user(bot_id)
    mdir = _bot_manifests_dir(bot_id, user=u)
    dest = mdir / f"{app_id}.json"

    # Ensure dir exists — direct mkdir works if ACL grants add_subdirectory on workspace/evolve/
    try:
        Path(str(mdir)).mkdir(parents=True, exist_ok=True)
    except PermissionError:
        subprocess.run(["sudo", "/bin/mkdir", "-p", str(mdir)],
                       capture_output=True, timeout=5)
    except Exception:
        pass

    content = json.dumps(data, indent=2)

    # Same-dir temp + rename. Dot-prefixed temp names are invisible to
    # both the plugin's scan and _glob_manifests, so a crashed write
    # can't surface as a manifest.
    tmp: str | None = None
    try:
        fd, tmp = _tmpfile.mkstemp(dir=str(mdir), prefix=f".{app_id}-", suffix=".tmp")
        with _os.fdopen(fd, "w") as f:
            f.write(content)
        _os.chmod(tmp, 0o644)
        _os.replace(tmp, dest)
        return True
    except OSError:
        # No write ACL on the manifests dir (PermissionError) or the dir
        # is missing despite the mkdir attempt (FileNotFoundError) —
        # both OSError; fall through to the staged-copy paths.
        if tmp is not None and _os.path.exists(tmp):
            try:
                _os.unlink(tmp)
            except OSError:
                # Partial ACL shape (add_file without delete): leave the
                # orphan — dot-temps are invisible to manifest scans — so
                # the staged-copy fallback below still gets to land the
                # write instead of this cleanup aborting it.
                _log.warning("manifest temp cleanup failed, orphaning %s", tmp)

    with _tmpfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, dir="/tmp") as f:
        f.write(content)
        tmp = f.name
    try:
        _os.chmod(tmp, 0o644)
        # Direct write — works once ACL is set on workspace/evolve/
        try:
            import shutil as _shutil
            _shutil.copy2(tmp, str(dest))
            return True
        except PermissionError:
            pass
        # Fallback: sudo /bin/cp as root
        result = subprocess.run(
            ["sudo", "/bin/cp", tmp, str(dest)],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    finally:
        try:
            _os.unlink(tmp)
        except OSError:
            pass


def _count_bot_manifests(bot_id: str, user: str | None = None) -> int:
    return len(_list_manifests_as_bot(bot_id, user=user))


# ── Operator docs renderer (PWA Phase 0 §4.1.d) ──────────────────────────
# Tiny markdown→HTML helper used by the /docs/<name> route. Supports the
# subset operator docs actually use: headings (# / ## / ###), paragraphs,
# bullet + numbered lists, fenced code blocks, inline `code`, **bold**,
# and [text](url) links. No tables, no nested lists — if a doc needs more,
# add it deliberately rather than reaching for a markdown dependency.
def _resolve_docs_dir() -> Path:
    """Pick the docs/ root the same way ``cli.py::_resolve_docs_root`` does.

    Priority: deploy checkout (``/Users/Shared/evolve-repo/docs``) → walk
    up from this file. Falls back to a path that won't exist so the
    /docs route returns a clean 404 rather than a 500.
    """
    deploy_docs = Path(get_profile().deploy_checkout_default) / "docs"
    if deploy_docs.is_dir():
        return deploy_docs
    p = Path(__file__).resolve()
    for _ in range(8):
        candidate = p.parent / "docs"
        if candidate.is_dir():
            return candidate
        p = p.parent
    return Path("/nonexistent-docs-root")


def _markdown_to_html(md: str) -> str:
    import html as _html
    import re as _re

    def _safe_link(match):
        # Only allow http(s)://, mailto:, and same-page fragment links.
        # Anything else (javascript:, file:, data:) drops the link and
        # renders as plain text — defense-in-depth even though the doc
        # source is author-controlled.
        text, url = match.group(1), match.group(2)
        ok = (
            url.startswith(("http://", "https://", "mailto:", "#"))
            or url.startswith("/")
        )
        if not ok:
            return text
        # Same-page (#anchor) links don't need target=_blank.
        target = "" if url.startswith("#") else ' target="_blank" rel="noopener"'
        return f'<a href="{url}"{target}>{text}</a>'

    def _inline(s: str) -> str:
        s = _html.escape(s)
        # Inline code first so its contents don't get bold/link-processed.
        s = _re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = _re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = _re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _safe_link, s)
        return s

    def _slugify(s: str) -> str:
        slug = _re.sub(r"[^\w\s-]", "", s.lower())
        slug = _re.sub(r"\s+", "-", slug.strip())
        return slug

    lines = md.split("\n")
    out: list[str] = []
    _num_prefix = r"^\d+\. "
    i = 0
    while i < len(lines):
        line = lines[i]
        # Fenced code block: ```...```
        if line.startswith("```"):
            i += 1
            block: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            out.append("<pre><code>" + _html.escape("\n".join(block)) + "</code></pre>")
            continue
        # Headings — each gets an id= matching its slug so #anchor links
        # inside the doc work.
        heading_match = _re.match(r"^(#{1,3}) (.+)$", line)
        if heading_match:
            level = len(heading_match.group(1))
            text = heading_match.group(2)
            out.append(f'<h{level} id="{_slugify(text)}">{_inline(text)}</h{level}>')
            i += 1
            continue
        # Bullet list
        if line.startswith("- "):
            items: list[str] = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(f"<li>{_inline(lines[i][2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        # Numbered list
        if _re.match(_num_prefix, line):
            items = []
            while i < len(lines) and _re.match(_num_prefix, lines[i]):
                stripped = _re.sub(_num_prefix, "", lines[i])
                items.append(f"<li>{_inline(stripped)}</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue
        # Horizontal rule
        if line.strip() == "---":
            out.append("<hr>")
            i += 1
            continue
        # Indented code block (≥4-space prefix). Collects contiguous
        # indented + blank lines, then dedents by the smallest leading
        # indent so a doubly-indented block (e.g. a code snippet nested
        # under a list item) doesn't keep a phantom 4-space indent.
        if line.startswith("    ") and line.strip():
            import textwrap as _textwrap
            block = []
            while i < len(lines) and (lines[i].startswith("    ") or not lines[i].strip()):
                block.append(lines[i])
                i += 1
            while block and not block[-1].strip():
                block.pop()
            dedented = _textwrap.dedent("\n".join(block))
            out.append("<pre><code>" + _html.escape(dedented) + "</code></pre>")
            continue
        # Blank line — paragraph break.
        if not line.strip():
            i += 1
            continue
        # Paragraph: greedily consume contiguous non-empty, non-special lines.
        para = [line]
        i += 1
        while (
            i < len(lines)
            and lines[i].strip()
            and not lines[i].startswith(("#", "- ", "```", "    "))
            and not _re.match(_num_prefix, lines[i])
            and lines[i].strip() != "---"
        ):
            para.append(lines[i])
            i += 1
        out.append("<p>" + _inline(" ".join(para)) + "</p>")
    return "\n".join(out)


def _doc_page_html(body_html: str) -> str:
    """Wrap rendered markdown in a self-contained HTML shell.

    Styled to feel like the admin UI (Inter font, dark default), but
    independent so a docs page renders correctly even if the SPA's
    stylesheet changes. CSS uses the same color palette tokens as
    index.html's :root block.
    """
    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Evolve · Docs</title>"
        "<style>"
        "html,body{margin:0;padding:0;background:#0B0D10;color:#E6EDF3;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Inter',sans-serif;"
        "font-size:15px;line-height:1.55;}"
        "main{max-width:760px;margin:0 auto;padding:32px 22px 80px;}"
        "h1{font-size:1.7rem;margin:0 0 10px;}"
        "h2{font-size:1.15rem;margin:28px 0 8px;color:#E6EDF3;}"
        "h3{font-size:1rem;margin:20px 0 6px;color:#8B949E;text-transform:uppercase;letter-spacing:0.04em;}"
        "p{margin:10px 0;}"
        "ul,ol{margin:10px 0 14px 22px;padding:0;}"
        "li{margin:4px 0;}"
        "hr{border:0;border-top:1px solid #1E2530;margin:24px 0;}"
        "code{background:#12161B;border:1px solid #1E2530;border-radius:4px;"
        "padding:1px 5px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:0.88em;}"
        "pre{background:#12161B;border:1px solid #1E2530;border-radius:6px;"
        "padding:12px 14px;overflow-x:auto;margin:10px 0;}"
        "pre code{background:none;border:0;padding:0;font-size:0.85em;}"
        "a{color:#4CC9F0;}"
        "a:hover{text-decoration:underline;}"
        "</style></head><body><main>"
        + body_html
        + "</main></body></html>"
    )


def create_app(network_path: Path = DEFAULT_NETWORK_CONFIG) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["NETWORK_PATH"] = network_path

    _STATIC = Path(__file__).parent

    # The legacy ``generators.profile_inferrer`` wire-up that lived here
    # was scaffolding for an inferrer that was never built. Profile
    # inference now runs per-bot at session_end via the OpenClaw hook in
    # ``packages/analyzer/generators/user_profile_inferrer/`` (spec
    # docs/spec-user-profile-2026-05-07.md §D3) — siloed in each bot's
    # process, not a centralized wire-up.

    # Register analytics + module routes
    from .routes_analytics import register_analytics_routes
    register_analytics_routes(app, network_path)
    _register_gateway_routes(app, network_path)
    from .routes_bot_config import register_bot_config_routes
    register_bot_config_routes(app, network_path)
    # Slack policy layer admin routes (Phase 2 UI)
    try:
        from .slack_routes import register_slack_policy_routes
        register_slack_policy_routes(app, network_path)
    except Exception as _slack_routes_err:
        log.warning("slack_routes: registration failed (%s); UI tab will be inert", _slack_routes_err)
    _register_oc_routes(app, network_path)
    _register_kaizen_routes(app, network_path)
    from .routes_trust import _register_trust_routes as _reg_trust
    _reg_trust(app, network_path)
    _register_accounts_routes(app, network_path)
    _register_admin_routes(app, network_path)
    # register_admin_routes decomposition siblings (4.1b Inc 1/2a/2b/2c/2d, memo §3):
    # config+models, token installs (notion/runway/linear), OAuth installs (slack/discord),
    # device-pairing installs (telegram/whatsapp/signal), and the remaining
    # skills-install cluster (generic dispatchers + obsidian/dropbox/google-workspace/
    # github-mcp/imessage) which completes the /api/skills/install/* dissolution.
    from .routes_admin_config import register_admin_config_routes
    register_admin_config_routes(app, network_path)
    from .model_discovery_adopt import register_model_discovery_routes
    register_model_discovery_routes(app, network_path)
    from .routes_skills_token import register_skills_token_routes
    register_skills_token_routes(app, network_path)
    from .routes_skills_oauth import register_skills_oauth_routes
    register_skills_oauth_routes(app, network_path)
    from .routes_skills_pairing import register_skills_pairing_routes
    register_skills_pairing_routes(app, network_path)
    from .routes_skills_workspace import register_skills_workspace_routes
    register_skills_workspace_routes(app, network_path)
    from .routes_maintenance import _register_maintenance_routes as _reg_maintenance
    _reg_maintenance(app, network_path)
    _register_service_routes(app)
    _register_recovery_routes(app, network_path)
    _register_breaker_routes(app, network_path)
    from .routes_host_health import _register_host_health_routes
    _register_host_health_routes(app, network_path)
    from .routes_pod_health import _register_pod_health_routes as _reg_pod_health
    _reg_pod_health(app, network_path)
    # _register_reliability_routes removed 2026-06-08 (dead app-test telemetry + Overview/Apps consumers; docs/decision-app-tests-2026-06-08.md).
    from .routes_mcp import _register_mcp_routes as _reg_mcp
    _reg_mcp(app, network_path)
    from .routes_mcp_admin import _register_mcp_admin_routes as _reg_mcp_admin
    _reg_mcp_admin(app, network_path)
    _register_permissions_admin_routes(app, network_path)
    from .routes_autonomy import _register_autonomy_routes as _reg_autonomy
    _reg_autonomy(app, network_path)
    _register_intent_routes(app, network_path)
    _register_plugins_admin_routes(app, network_path)
    _register_hooks_admin_routes(app, network_path)
    from .routes_content_scan import register_content_scan_routes
    register_content_scan_routes(app, network_path)
    from .routes_report import register_report_routes
    register_report_routes(app, network_path)
    from .routes_cost_measures import register_cost_measures_routes
    register_cost_measures_routes(app, network_path)
    _register_help_routes(app, network_path)
    from .routes_help_docs import register_help_docs_routes
    register_help_docs_routes(app, network_path)
    from .routes_better import register_better_routes
    register_better_routes(app, network_path)
    _register_arbiter_routes(app, network_path)
    from .routes_signals import register_signals_routes
    register_signals_routes(app, network_path)
    # Pod-first Apps surface reads (AL-1.8a). Read-only; the actions the page
    # offers still go to the existing /api/applications/* write routes.
    from .routes_apps import register_apps_routes
    register_apps_routes(app, network_path)
    _register_pod_rollup_routes(app, network_path)
    _register_candidates_routes(app, network_path)
    _register_skills_routes(app, network_path)

    # Alerts → Subscriptions tab routes (Phase A4)
    from .routes_alerts import register_alerts_subscription_routes
    register_alerts_subscription_routes(app, network_path)

    # Tier-cascade health surface (Phase 3 shadow-mode validation).
    # Backs the Overview-page cascade tile. Spec:
    # docs/cascade-validation-on-the-mini.md.
    from .routes_cascade import register_cascade_routes
    register_cascade_routes(app, network_path)

    # Pre-flight intent router health surface — per-bot agreement /
    # over-escalation / under-escalation rates + haiku cost projection.
    # Consumed by the Cost Optimization page's "Pre-flight router" card.
    # Spec: docs/spec-preflight-intent-router-2026-06-06.md.
    from .routes_preflight import register_preflight_routes
    register_preflight_routes(app, network_path)

    # Customizations tab (per-bot sandbox-overrides review).
    # Spec: docs/spec-openclaw-json-derived-artifact-2026-05-24.md §4.
    from .routes_customizations import register_customizations_routes
    register_customizations_routes(app, network_path)

    # PWA push notifications (Phase 1.2): VAPID public key + subscribe /
    # unsubscribe / list / test endpoints. Spec: docs/spec-pwa-2026-05-18.md §6.
    from .push_routes import register_push_routes
    register_push_routes(app, network_path)

    # Multi-pod M2 — hub switcher read route (GET /api/peers). Returns this
    # pod's name + version and the operator-maintained sibling registry
    # (links only, no tokens). Spec: docs/design-multi-pod-2026-06-11.md §3.1.
    from .peers_routes import register_peers_routes
    register_peers_routes(app, network_path)

    # PWA Phase 3 — embedded mini terminal. Adds /api/terminal/info (JSON)
    # and /api/terminal/ws (WebSocket → PTY → /bin/zsh -l). Spec:
    # docs/spec-pwa-2026-05-18.md §7. The WebSocket gates on
    # network.json::terminal.enabled (default true) inside the handler
    # rather than at registration time so a flip in network.json takes
    # effect immediately without an admin-server restart.
    from .terminal_routes import register_terminal_routes
    register_terminal_routes(app, network_path)

    # Gated-release operator action: POST /api/release/promote ("Complete
    # soak now"). Runs as the admin server's own user (evolve, which owns
    # the release artifacts) — no sudo, unlike the CLI hint. See
    # release_routes for the no-escalation and restart rationale.
    from .release_routes import register_release_routes
    register_release_routes(app, network_path)

    # Phase E.3.1 — admin-daemon endpoints called by evo's MCP tools
    # over the unix-socket binding. Routes here are guarded by
    # @require_trusted_peer (peer-uid check on the unix socket),
    # unreachable from the admin UI's TCP loopback. Spec:
    # docs/spec-evo-account-separation-2026-05-25.md §3.
    from .admin_bot_routes import register_admin_bot_routes
    register_admin_bot_routes(app, network_path)

    # Remediation execution API — POST .../execute spawns a persistent
    # background job, GET .../job/<id> polls status. Signal-attached
    # Remediation.kind drives which handler runs.
    from ..remediation.routes import register_routes as _register_remediation_routes
    from evolve_config import load_config, get_shared_dir
    _register_remediation_routes(
        app,
        lambda: get_shared_dir(load_config(network_path)),
    )

    # Identity claim API (Phase 5 of alerts hardening) — POST
    # claim-primary / claim-admin replaces the DM-based "evo claim
    # charles / darwin" flow with a UI form. Resolves
    # multi_user_no_pod_admins and multi_user_no_primary_recorded.
    from .routes_identity import register_routes as _register_identity_routes
    _register_identity_routes(app, network_path)

    # Per-bot paired-users routes (Users page).
    # Surfaces OpenClaw's per-bot pairing state — approved allowFrom and
    # pending pairing requests — so the admin can approve/reject/disconnect
    # users from the UI. Auto-approves matches against pod-admin claims.
    # Spec: docs/spec-per-bot-users-management-2026-05-29.md.
    from .routes_bot_users import register_routes as _register_bot_users_routes
    _register_bot_users_routes(app, network_path)

    # Person-link routes (M1-B4a) — the Users-page affordance for the
    # operator assertion "this platform id is the same person as this row".
    # Thin HTTP shell over roster_identity's D1 seam (link/unlink); the
    # collision refusal is surfaced as a named 409, never auto-forced.
    # Spec: docs/spec-users-meta-2026-06-15.md §M1 (B2b's "the B4 seam").
    from .routes_person_link import register_routes as _register_person_link_routes
    _register_person_link_routes(app, network_path)
    # Messaging-CHANNEL routes (M1-B4b) — WHERE a bot is reachable, not WHO.
    from .routes_bot_channels import register_routes as _register_bot_channels_routes
    _register_bot_channels_routes(app, network_path)

    # Per-bot directory routes (spec-user-directory-2026-06-22 §6, Phase 2):
    # operator emails + contact-attribute writes through the Phase-1 directory
    # store. STRICTLY directory-owned fields — these routes have no path to
    # membership/roles/admission (invariant #2); admitting a contact runs the
    # existing fail-closed flow above. Registered after bot_users because it
    # imports the page's requester-attribution + capability gate from it.
    from .routes_directory import register_routes as _register_directory_routes
    _register_directory_routes(app, network_path)

    # Per-bot messaging-ID pairing wizard backend (modal launched from
    # Overview tile chip, install-wizard Done screen, or deep link).
    # Primary input is the pairing code OC sent the operator; commit
    # routes the write by role (pod_admin / primary / other) and then
    # approves into the bot's allowFrom via the bot_users helpers above.
    from .routes_pairing import register_routes as _register_pairing_routes
    _register_pairing_routes(app, network_path)

    # Per-bot Setup checklist (Settings page card + Overview tile chip).
    # Tracks the operator's progress through recommended next steps after
    # a bot is provisioned (pairing, GitHub backup, search plugin, ...).
    # Data layer: evolve_admin.setup_checklist.
    from .routes_setup_checklist import register_routes as _register_setup_checklist_routes
    _register_setup_checklist_routes(app, network_path)

    # Gallery + Forge Jobs routes (separate module)
    from .gallery_routes import register_gallery_routes
    _shared_dir = Path(load_network(network_path).get("sharedDir", "/Users/Shared/evolve"))
    register_gallery_routes(app, network_path, _shared_dir)

    # Operator-initiated app-manifest mutation routes (each in its own module): make-reliable (agent_invokes→plugin_intercept) + definition promote/demote (§9 Bite 1) + smart-sync (scan+reflect).
    from .routes_applications_reliability import register_applications_reliability_routes
    register_applications_reliability_routes(app, network_path, _shared_dir)
    from .routes_app_definition import register_app_definition_routes
    register_app_definition_routes(app)
    from .routes_applications_sync import register_applications_sync_routes
    register_applications_sync_routes(app, network_path)

    # Add-a-Bot wizard backend (PR β; spec docs/spec-add-bot-wizard-2026-05-28.md)
    from .wizard_routes import register_wizard_routes
    register_wizard_routes(app, network_path)

    # Path-C Google integration wizard (PR ε; spec docs/spec-google-integration-paths-2026-05-30.md §7)
    from .wizard_google_routes import register_google_wizard_routes
    register_google_wizard_routes(app, network_path)

    # Path-A Personal-Gmail wizard backend (Phase A.2; spec docs/spec-google-path-a-2026-06-01.md)
    from .wizard_google_personal_routes import register_google_personal_wizard_routes
    register_google_personal_wizard_routes(app, network_path)

    # Standalone "Email alias" editor (writes only the `correspondence`
    # block; lets operators rename the From-header name without walking
    # the path-C wizard). Spec: docs/spec-correspondence-persona-2026-05-30.md
    # §3; sibling work in docs/spec-multi-user-alias-2026-06-01.md.
    from .routes_alias import register_routes as _register_alias_routes
    _register_alias_routes(app, network_path)

    # Within-pod application sharing (Session 4a / v7-arc §9.1)
    from .share_routes import register_share_routes
    register_share_routes(app, network_path, _shared_dir)

    # Hydrate the in-memory audit cache from disk so a restarted
    # admin server doesn't appear empty until the next scheduled
    # audit sweep. The disk mirror is also what the MCP child process
    # reads via pod_state.audit (cross-process visibility — see
    # audit_state.py for the rationale).
    try:
        _audit_state.hydrate(_shared_dir)
    except Exception as exc:  # noqa: BLE001
        # Hydration failures are non-fatal — the next audit run will
        # rebuild + persist.
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "audit_state.hydrate skipped (%s) — cache will rebuild on next sweep",
            exc,
        )

    # Create App wizard routes (spec sessions + spec builder)
    from .spec_routes import register_spec_routes
    register_spec_routes(app, _shared_dir)

    # Evo subcommand surface (/api/evo/dispatch, /api/evo/help)
    from .evo_routes import register_evo_routes
    register_evo_routes(app, network_path)

    # Forge install-phase endpoints (/api/forge/install/*) — interface
    # stubs in PR 1 of spec-forge-side-effects-2026-06-02.md; PR 4 wires
    # the actual launchd/crontab/openclaw.json install behavior.
    from .forge_install_routes import register_forge_install_routes
    register_forge_install_routes(app)

    # Gallery promotion endpoint (/api/gallery/promote/snapshot) — F-P.7.a.
    # Wraps snapshot_engine for the admin UI's Apps-tab "Promote to
    # gallery" button (F-P.7.b, follows). Spec:
    # docs/spec-files-pack-hybrid-2026-06-03.md §8.
    from .gallery_promote_routes import register_gallery_promote_routes
    register_gallery_promote_routes(app)

    # Scanned-export operator surface (/api/export/* + /export-review).
    # Spec: docs/spec-scanned-export-2026-06-02.md §3.5 (Stage 0e).
    # Lets the operator turn scanner-discovered manifests into gallery
    # packages without leaving the admin UI.
    from .export_routes import register_export_routes
    _REPO_ROOT_FOR_EXPORT = Path(__file__).resolve().parents[4]
    register_export_routes(
        app, network_path, _REPO_ROOT_FOR_EXPORT / "gallery",
        shared_dir=_shared_dir,
    )

    # Home conversational chat (/api/home/chat) — dispatcher fallback +
    # Haiku LLM call for free-form user prompts. Spec: §3 of the Home
    # design conversation. Uses the primary bot's Anthropic key.
    from .home_chat_routes import register_home_chat_routes
    register_home_chat_routes(app, network_path)

    # Apps-page repair chat (/api/applications/<bot>/<app>/repair-chat/*) —
    # conversational LLM-mediated semantic repair. Dispatches through the
    # bot's own OpenClaw agent so credentials + provider config stay
    # per-bot. Proposed mutations land in the chat log; nothing is
    # applied until the operator clicks Apply, which routes through
    # save_manifest_with_provenance(source=user_authored, via=repair_chat).
    from .repair_chat_routes import register_repair_chat_routes
    register_repair_chat_routes(app, network_path)

    # Chat-surface attachment uploads (/api/chat-uploads, /chat-uploads/...).
    # Backs the drag-and-drop + paste handlers on the evo drawer, the
    # Home chat page, and the Diagnostics card. Spec:
    # docs/spec-pwa-2026-05-18.md §5.4.
    from .chat_upload_routes import register_chat_upload_routes
    register_chat_upload_routes(app, network_path)

    # Primary-bot pod-state read tools (/api/primary/state/*)
    # Spec: docs/spec-primary-bot-interface-2026-05-14.md §5.
    from .primary_state_routes import register_primary_state_routes
    register_primary_state_routes(app, network_path)

    from .google_bot_routes import register_google_bot_routes
    from .directory_bot_routes import register_directory_bot_routes
    register_google_bot_routes(app, network_path)
    register_directory_bot_routes(app, network_path)

    # Bot-facing app capability index — Tier-2 expand_app(app_id) lookup.
    # Spec: docs/spec-app-invocation-just-works-2026-06-29.md §2.1 (recognition).
    from .applications_bot_routes import register_applications_bot_routes
    register_applications_bot_routes(app, network_path)

    # Multi-bot handover (V2.4-5) — onboarding-link generator + landing page
    from .handover_routes import register_handover_routes
    register_handover_routes(app, network_path)

    # ── JSON error handlers (prevents HTML 404/500 from breaking r.json() in browser) ──
    @app.errorhandler(404)
    def _not_found(e):
        return jsonify({"error": "not found", "path": request.path}), 404

    @app.errorhandler(405)
    def _method_not_allowed(e):
        return jsonify({"error": "method not allowed"}), 405

    @app.errorhandler(500)
    def _internal_error(e):
        orig = getattr(e, "original_exception", e)
        _log.error(
            "500 Internal Error on %s %s: %s",
            request.method, request.path, orig,
            exc_info=orig,
        )
        return jsonify({
            "error": str(orig),
            "type": type(orig).__name__,
        }), 500

    # ── Device-pairing auth gate (roadmap 2.1) ──────────────────────────────
    # Loopback binding doesn't defend against a compromised *local* process, so
    # once the operator runs ``evolve-admin pair`` (which creates the admin-auth
    # key) the server requires a paired device cookie. Before that — a fresh,
    # never-paired pod — auth is OFF, so nobody is locked out. Only browsers hit
    # this server over HTTP; the evo proxy + MCP bridge use direct paths.
    _auth_shared_dir = Path(
        load_network(network_path).get("sharedDir", "/Users/Shared/evolve")
    )
    # Brute-force throttle for the pairing endpoint — the short pairing code is
    # safe because guessing is rate-limited here, not because of its entropy.
    # One instance per app (in-process, single-operator action).
    _pair_throttle = admin_auth.PairThrottle()

    # ── One-time secret migration (roadmap 2.8) ─────────────────────────────
    # Move any plaintext network.json::github.pat into the keystore and scrub
    # the plaintext copy. Idempotent + best-effort; existing pods migrate on
    # the first daemon start after this ships. Readers are keystore-first
    # with a legacy fallback, so ordering with daemons is safe either way.
    try:
        from ..keystore import migrate_github_pat_from_network
        if migrate_github_pat_from_network(network_path):
            _log.info("github.pat migrated from network.json to the keystore")
    except Exception:
        _log.warning("github.pat keystore migration failed (will retry next start)",
                     exc_info=True)
    # Open even when enforcement is on: the pairing flow itself, static assets,
    # PWA shell files (no secrets there; the SPA is inert without API access),
    # and the read-only liveness probe (health.py / diagnose/probes.py hit
    # /api/health to check the daemon is up — a 401 there would make a healthy
    # daemon look down).
    _AUTH_EXEMPT_PATHS = frozenset({
        "/pair", "/api/pair",
        "/api/health",
        "/manifest.json", "/sw.js", "/favicon.ico", "/apple-touch-icon.png",
        "/favicon-16x16.png", "/favicon-32x32.png",
    })

    def _auth_exempt(path: str) -> bool:
        return path in _AUTH_EXEMPT_PATHS or path.startswith("/static/")

    @app.before_request
    def _enforce_device_auth():
        if not admin_auth.is_auth_enabled(_auth_shared_dir):
            return None  # operator recorded an explicit opt-out → open
        # A unix-socket request authenticated by kernel peer-uid is the
        # socket's own auth — exempt trusted peers (evo's tool runtime; the
        # 2.6 auth-on-by-default enabler) and the daemon's own uid (same-user
        # local tooling, e.g. the gallery-verify harness) from the
        # device-cookie gate. Any other uid falls through and 401s; see
        # peer_auth.device_gate_trusted_peer for the full rationale.
        if (request.environ.get("REMOTE_TRANSPORT") or "tcp") == "unix-socket":
            try:
                from . import peer_auth as _pa
                if _pa.device_gate_trusted_peer():
                    return None
                # Bot-facing /api/google/* routes self-authenticate by peer
                # uid (the route binds the call to the bot the uid maps to).
                # Exempt them from the device-cookie gate, scoped to that
                # path only — never widens access to any other surface.
                if _pa.peer_bot_route_exempt(network_path):
                    return None
            except Exception as _exc:
                # Fall through to the cookie gate on any resolution error —
                # fail-closed (a socket request that can't prove a trusted
                # peer-uid gets no exemption). Logged, not silently swallowed.
                _log.warning("auth: socket peer-uid resolution failed: %s", _exc)
        if _auth_exempt(request.path):
            return None
        if admin_auth.verify_device_token(
            _auth_shared_dir, request.cookies.get(admin_auth.DEVICE_COOKIE_NAME)
        ):
            return None
        # Unauthenticated: 401 for API/XHR, redirect to the pairing page for a
        # browser navigation.
        if request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
            return jsonify({"error": "device not paired", "pair_url": "/pair"}), 401
        return redirect("/pair")

    # ── CSRF / Origin / Host defense (roadmap 2.7) ──────────────────────────
    # Runs AFTER the auth gate, so a request reaching here on an auth-enabled
    # pod is already device-authenticated. Loopback + SameSite=Lax is not a
    # CSRF defense; this adds same-origin + double-submit-token enforcement
    # on mutating methods. Scoped to cookie-authenticated requests (nothing
    # to forge otherwise) and exempts the peer-authed unix socket.
    @app.before_request
    def _enforce_csrf():
        from . import csrf as _csrf
        # Cheap exits first — the vast majority of requests are safe-method
        # reads, which never need the device-token verify or the network read.
        if request.method.upper() in _csrf.SAFE_METHODS:
            return None
        if (request.environ.get("REMOTE_TRANSPORT") or "tcp") == "unix-socket":
            return None
        authed = (
            admin_auth.is_auth_enabled(_auth_shared_dir)
            and admin_auth.verify_device_token(
                _auth_shared_dir,
                request.cookies.get(admin_auth.DEVICE_COOKIE_NAME),
            )
        )
        if not authed:
            return None  # no session to forge
        try:
            net = load_network(network_path)
        except Exception:
            net = {}
        ok, reason = _csrf.check_request(
            method=request.method,
            path=request.path,
            transport="tcp",
            is_authenticated=True,
            request_host=request.host or "",
            header_get=request.headers.get,
            cookie_get=request.cookies.get,
            network=net,
        )
        if not ok:
            _log.warning("csrf: rejected %s %s — %s", request.method, request.path, reason)
            return jsonify({"error": "request blocked", "detail": reason}), 403
        return None

    # ── Request logging (slow requests + errors) ────────────────────────────
    @app.before_request
    def _before_request():
        request._evolve_start = _time.time()

    @app.after_request
    def _set_csrf_cookie(response):
        # Ensure the device carries a readable CSRF token cookie (roadmap
        # 2.7). Set once when absent; the browser echoes it back thereafter,
        # so it stays stable per device. JS-readable (the fetch wrapper
        # copies it into X-CSRF-Token); SameSite=Strict — a CSRF token never
        # needs to ride a top-level navigation.
        from .csrf import CSRF_COOKIE_NAME, new_csrf_token
        if not request.cookies.get(CSRF_COOKIE_NAME):
            response.set_cookie(
                CSRF_COOKIE_NAME, new_csrf_token(),
                max_age=31536000, httponly=False, samesite="Strict", path="/",
            )
        return response

    @app.after_request
    def _after_request(response):
        start = getattr(request, "_evolve_start", None)
        if start is not None:
            elapsed_ms = int((_time.time() - start) * 1000)
            level = "warning" if elapsed_ms > 3000 else "debug"
            getattr(_log, level)(
                "%s %s %d %dms",
                request.method, request.path, response.status_code, elapsed_ms,
            )
            if response.status_code >= 500:
                _log.error(
                    "Server error: %s %s → %d",
                    request.method, request.path, response.status_code,
                )
        return response

    # ── SPA <script src> injection (Phase 4a) ──────────────────────────────
    # The script-tag cluster in index.html is replaced with a single
    # placeholder marker. On every request, the marker is substituted
    # with `<script src>` tags computed from the SPA-asset allow-lists
    # (see _scan_spa_dir below). Adding a new page module is just `cp`
    # + restart; no edit to index.html needed, and no merge-conflict
    # surface between parallel page-extraction PRs.
    #
    # Load order discipline:
    #   - core/ first (router.js defines window.nav, which pages/forge.js
    #     captures at IIFE parse time)
    #   - then pages/
    #   - alphabetical within each tier for determinism (any order is
    #     safe within a tier — no top-level cross-file references)
    #
    # ``_SPA_SCRIPTS_HTML`` itself is populated AFTER the allow-lists
    # below; the helper here references it via closure so the deferred
    # lookup at request time picks up the populated value.
    _SPA_SCRIPTS_PLACEHOLDER = "<!--SPA_SCRIPTS-->"
    _SPA_SCRIPTS_HTML: str = ""  # populated by _build_spa_scripts_html below

    def _serve_spa_shell(extra_head_inject: str = "") -> Response:
        """Read index.html, expand the SPA_SCRIPTS placeholder, optionally
        inject ``extra_head_inject`` right before ``</head>`` (used by the
        investigation-landing route).

        Forces ``no-cache, must-revalidate`` on every page load. Without
        this, iOS Safari (and most browsers) apply heuristic caching to
        HTML responses with no explicit Cache-Control header — operators
        end up staring at a stale SPA shell for days after a deploy, with
        no way to force a refresh short of clearing site data. The SPA
        shell itself is still cached by the service worker for the
        offline-fallback story (sw.js: ``network-first`` with cache
        fallback), so this only changes what the browser's HTTP cache
        layer does.
        """
        html = (_STATIC / "index.html").read_text()
        html = html.replace(_SPA_SCRIPTS_PLACEHOLDER, _SPA_SCRIPTS_HTML, 1)
        if extra_head_inject:
            html = html.replace("</head>", extra_head_inject + "</head>", 1)
        resp = Response(html, mimetype="text/html")
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    # ── Serve admin UI ─────────────────────────────────────────────────────
    @app.get("/pair")
    def pair_page() -> Response:
        """Self-contained device-pairing page (exempt from the auth gate so an
        unpaired browser can reach it). The operator runs ``evolve-admin pair``
        to get a code, enters it here, and the device receives a signed cookie."""
        html = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair this device — Evolve</title>
<style>
 body{font-family:-apple-system,system-ui,sans-serif;background:#0b0d10;color:#e6e6e6;
   display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
 .card{background:#12161b;border:1px solid #1e2530;border-radius:10px;padding:28px;max-width:360px;width:100%}
 h1{font-size:1.1rem;margin:0 0 6px} p{color:#9aa4b2;font-size:.85rem;line-height:1.4;margin:0 0 16px}
 input{width:100%;box-sizing:border-box;padding:10px;font-size:1.1rem;letter-spacing:.15em;text-align:center;
   background:#0b0d10;border:1px solid #2a3340;border-radius:6px;color:#e6e6e6;margin-bottom:12px}
 button{width:100%;padding:10px;font-size:.95rem;background:#3b82f6;color:#fff;border:0;border-radius:6px;cursor:pointer}
 .err{color:#f87171;font-size:.82rem;min-height:1.2em;margin-top:10px;text-align:center}
 code{background:#0b0d10;padding:1px 5px;border-radius:4px}
</style></head><body><div class="card">
 <h1>Pair this device</h1>
 <p>Run <code>sudo evolve-admin pair</code> on the pod and enter the 6-digit code it shows.</p>
 <input id="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]*" maxlength="6" autofocus placeholder="6-digit code">
 <button onclick="go()">Pair</button>
 <div class="err" id="err"></div>
<script>
 async function go(){
  var c=document.getElementById('code').value.trim();
  var r=await fetch('/api/pair',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:c})});
  if(r.ok){location.href='/';return;}
  var e=document.getElementById('err');
  if(r.status===429){
   var j=await r.json().catch(function(){return {};});
   var s=j.retry_after||30;
   e.textContent='Too many attempts — wait '+s+'s and try again with a fresh code.';
  } else {
   e.textContent='That code did not work — generate a fresh one and try again.';
  }
 }
 document.getElementById('code').addEventListener('keydown',function(e){if(e.key==='Enter')go();});
</script></div></body></html>"""
        return Response(html, mimetype="text/html")

    @app.post("/api/pair")
    def pair_submit() -> Response:
        body = request.get_json(silent=True) or {}
        code = str(body.get("code") or request.form.get("code") or "")
        # Atomic check-verify-record under the throttle lock: while a cooldown is
        # armed the code is NOT verified at all, so a brute-forcing local process
        # gets zero guesses during it — even firing concurrent requests.
        outcome, wait = _pair_throttle.attempt(
            lambda: admin_auth.verify_pairing_code(_auth_shared_dir, code)
        )
        if outcome == "throttled":
            resp = jsonify({"error": "too many attempts", "retry_after": wait})
            resp.headers["Retry-After"] = str(wait)
            resp.status_code = 429
            return resp
        if outcome != "ok":
            return jsonify({"error": "invalid or expired code"}), 403
        token = admin_auth.issue_device_token(_auth_shared_dir)
        resp = jsonify({"ok": True})
        resp.set_cookie(
            admin_auth.DEVICE_COOKIE_NAME, token,
            max_age=31536000, httponly=True, samesite="Lax", path="/",
        )
        return resp

    @app.get("/")
    def index() -> Response:
        return _serve_spa_shell()

    # Trail-link deep-link target — the URL embedded in evo-fail
    # diagnosis replies for pod admins. Lands the operator on the admin
    # UI with a hash fragment the JS reads to open the trail viewer
    # for the named investigation. Spec: audit-extensions §5.3 + the
    # follow-up brief (Item 3).
    @app.get("/investigations/<investigation_id>")
    def investigation_landing(investigation_id: str) -> Response:
        # Set a small inline script that drops the id into a global the
        # dashboard checks on load; safer than relying on a fragment
        # that survives the SPA's nav handlers.
        from markupsafe import escape as _esc
        safe = _esc(investigation_id)
        marker = f"<script>window._pendingInvestigationId = \"{safe}\";</script>"
        return _serve_spa_shell(extra_head_inject=marker)

    @app.get("/favicon.ico")
    def favicon_ico() -> Response:
        return send_from_directory(_STATIC, "favicon.ico", mimetype="image/x-icon")

    @app.get("/apple-touch-icon.png")
    def apple_touch_icon() -> Response:
        return send_from_directory(_STATIC, "apple-touch-icon.png", mimetype="image/png")

    @app.get("/favicon-16x16.png")
    def favicon_16() -> Response:
        return send_from_directory(_STATIC, "favicon-16x16.png", mimetype="image/png")

    @app.get("/favicon-32x32.png")
    def favicon_32() -> Response:
        return send_from_directory(_STATIC, "favicon-32x32.png", mimetype="image/png")

    # ── PWA Phase 1.1.A: per-pod manifest, service worker, icon set ─────────
    # Spec: docs/spec-pwa-2026-05-18.md §3 + §5.1-5.3. The manifest is
    # rendered per-pod (the pod's name + theme), the service worker is
    # served from origin root so its default scope covers the whole SPA,
    # and the icons live under /static/icons/ for the manifest to reference.

    def _pod_name() -> str:
        # network.json's pod identifier is ``networkId`` (see wizard / config.py
        # default schema). Fall back to "Evolve" if the file is missing or the
        # value is empty.
        try:
            net = load_network(network_path)
        except Exception:
            return "Evolve"
        raw = net.get("networkId") or ""
        return str(raw).strip() or "Evolve"

    @app.get("/manifest.json")
    def manifest_json() -> Response:
        pod = _pod_name()
        # When ``networkId`` is the wizard's default placeholder ("my-pod") or
        # otherwise unset, drop the per-pod suffix so Chrome's standalone
        # window-title stitching (manifest name + document.title) doesn't show
        # "Evolve · my-pod Evolve" on a fresh install.
        if _is_default_or_empty(pod):
            name = "Evolve"
            short_name = "Evolve"
        else:
            name = f"Evolve · {pod}"
            short_name = pod
        body = {
            "name": name,
            "short_name": short_name,
            "description": "Your Evolve pod",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#0d1117",
            "theme_color": "#0d1117",
            "icons": [
                {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {"src": "/static/icons/icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            ],
        }
        return Response(
            json.dumps(body, indent=2),
            mimetype="application/manifest+json",
        )

    # Backwards-compat shim — older index.html builds referenced
    # /site.webmanifest. Redirect to the canonical /manifest.json so any
    # browser still holding a stale link gets the per-pod manifest.
    @app.get("/site.webmanifest")
    def site_webmanifest_alias() -> Response:
        return manifest_json()

    @app.get("/sw.js")
    def service_worker() -> Response:
        # Served from origin root so the default scope covers the whole SPA.
        # ``Cache-Control: no-cache`` stops intermediaries pinning a stale SW
        # past a deploy (browsers byte-compare the SW on every load and
        # install only when bytes change). The per-build cache version +
        # asset fingerprint that ``render_sw_js`` stamps are what make a
        # deploy flush the prior build's cache and fire ``updatefound`` —
        # the structural fix for the fleet-promote lockout (see
        # evolve_admin.web.sw_assets and sw.js's header).
        text = render_sw_js(_STATIC, (
            (_CORE_DIR, _ALLOWED_CORE),
            (_PAGES_DIR, _ALLOWED_PAGES),
            (_WIDGETS_DIR, _ALLOWED_WIDGETS),
            (_CSS_DIR, _ALLOWED_CSS),
        ))
        resp = Response(text, mimetype="application/javascript")
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    _ICONS_DIR = _STATIC / "static" / "icons"
    _ALLOWED_ICONS = {
        "icon-192.png",
        "icon-512.png",
        "icon-512-maskable.png",
        "apple-touch-icon-180.png",
    }

    @app.get("/static/icons/<filename>")
    def pwa_icon(filename: str) -> Response:
        # Explicit allow-list — keeps the route from being a generic static
        # server. Adding a new icon means adding it to ``_ALLOWED_ICONS``
        # *and* dropping the file in static/icons/; otherwise a typo'd
        # filename returns 404 instead of leaking the directory.
        if filename not in _ALLOWED_ICONS:
            return jsonify({"error": "icon not found", "name": filename}), 404
        return send_from_directory(_ICONS_DIR, filename, mimetype="image/png")

    # ── Extracted SPA assets (CSS + per-widget JS modules) ────────────────
    # The source-split moved 13K+ lines out of index.html into
    # ``static/{css,js/{core,pages,widgets}}/``. Each route below is
    # gated by a directory-scanned allow-list (see ``_scan_spa_dir``) —
    # adding a new page is just ``cp module.js static/js/pages/`` +
    # restart, no registry edit needed. The SW's network-first fetch
    # handler caches each module on first request.

    # Filename allow-list regex. Rejects anything outside the lowercase
    # kebab/snake-case convention (``[a-z][a-z0-9_-]*\.js``) — keeps the
    # "no directory leak" property the hand-maintained allow-lists were
    # giving us, while letting the filesystem be the source of truth
    # for what modules exist.
    _SPA_FILENAME_RE = re.compile(r"^[a-z][a-z0-9_-]*\.(?:js|css)$")

    def _scan_spa_dir(directory: Path, suffix: str) -> frozenset[str]:
        if not directory.is_dir():
            return frozenset()
        return frozenset(
            p.name for p in directory.glob(f"*.{suffix}")
            if _SPA_FILENAME_RE.match(p.name)
        )

    _CSS_DIR = _STATIC / "static" / "css"
    _ALLOWED_CSS = _scan_spa_dir(_CSS_DIR, "css")

    @app.get("/static/css/<filename>")
    def spa_css(filename: str) -> Response:
        if filename not in _ALLOWED_CSS:
            return jsonify({"error": "css not found", "name": filename}), 404
        return send_from_directory(_CSS_DIR, filename, mimetype="text/css")

    _WIDGETS_DIR = _STATIC / "static" / "js" / "widgets"
    _ALLOWED_WIDGETS = _scan_spa_dir(_WIDGETS_DIR, "js")

    @app.get("/static/js/widgets/<filename>")
    def spa_widget_js(filename: str) -> Response:
        if filename not in _ALLOWED_WIDGETS:
            return jsonify({"error": "widget not found", "name": filename}), 404
        return send_from_directory(
            _WIDGETS_DIR, filename, mimetype="application/javascript"
        )

    _CORE_DIR = _STATIC / "static" / "js" / "core"
    _ALLOWED_CORE = _scan_spa_dir(_CORE_DIR, "js")

    @app.get("/static/js/core/<filename>")
    def spa_core_js(filename: str) -> Response:
        if filename not in _ALLOWED_CORE:
            return jsonify({"error": "core module not found", "name": filename}), 404
        return send_from_directory(
            _CORE_DIR, filename, mimetype="application/javascript"
        )

    _PAGES_DIR = _STATIC / "static" / "js" / "pages"
    _ALLOWED_PAGES = _scan_spa_dir(_PAGES_DIR, "js")

    @app.get("/static/js/pages/<filename>")
    def spa_page_js(filename: str) -> Response:
        if filename not in _ALLOWED_PAGES:
            return jsonify({"error": "page module not found", "name": filename}), 404
        return send_from_directory(
            _PAGES_DIR, filename, mimetype="application/javascript"
        )

    # Populate the script-tag cluster string declared at the top of the
    # SPA-asset region (see _serve_spa_shell above). Done here, AFTER
    # _ALLOWED_CORE / _ALLOWED_PAGES are set, so the closure inside
    # _serve_spa_shell sees the populated value at request time. Order
    # within each tier is alphabetical for determinism; any order is
    # safe — no top-level cross-file references between SPA modules.
    _SPA_SCRIPTS_HTML = "\n".join(
        [f'<script src="/static/js/core/{name}"></script>'
         for name in sorted(_ALLOWED_CORE)]
        + [f'<script src="/static/js/pages/{name}"></script>'
           for name in sorted(_ALLOWED_PAGES)]
    )

    # ── Operator docs (PWA Phase 0 §4.1.d) ─────────────────────────────────
    # The HTTPS-nudge banner in index.html links to /docs/https-setup. This
    # route serves a small allow-list of markdown files from the repo's
    # docs/ directory, rendered to HTML via the tiny renderer below. We
    # intentionally do NOT expose the whole docs/ tree — the route exists
    # solely to land operators on the pages we link from the admin UI.
    _DOCS_ALLOWED = {"https-setup": "https-setup.md"}

    @app.get("/docs/<name>")
    def serve_doc(name: str) -> Response:
        filename = _DOCS_ALLOWED.get(name)
        if filename is None:
            return jsonify({"error": "doc not found", "name": name}), 404
        doc_path = _resolve_docs_dir() / filename
        if not doc_path.is_file():
            return jsonify({"error": "doc missing on disk", "path": str(doc_path)}), 404
        body_html = _markdown_to_html(doc_path.read_text(encoding="utf-8"))
        return Response(_doc_page_html(body_html), mimetype="text/html")

    # ── Version ────────────────────────────────────────────────────────────
    @app.get("/api/version")
    def api_version() -> Response:
        try:
            commit = subprocess.check_output(
                ["git", "describe", "--always", "--tags", "--dirty"],
                cwd=Path(__file__).parent,
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except Exception:
            commit = "unknown"
        build_date = date.today().isoformat()
        return jsonify({"version": f"v0.44-{commit}", "commit": commit, "date": build_date, "evolve_version": EVOLVE_VERSION})

    @app.get("/api/admin/version")
    def api_admin_version() -> Response:
        """Operator diagnostic — returns the live admin server's version,
        admin process start time, and an md5 of the installed plugin's
        canonical entry file. Used by ``evolve-admin upgrade``'s self-
        verification block and by operators answering "is this actually
        running the version I think it is?" without ssh.

        Surfaces three signals:
          * ``admin_version`` — EVOLVE_VERSION constant baked into the
            running Python module
          * ``admin_pid_started_at`` — when the running admin daemon
            was launched. Lets operators tell at a glance whether the
            kickstart from the last upgrade actually swapped the
            process.
          * ``plugin_install_md5`` — md5 of
            ``/Users/Shared/evolve-plugin/dist/observer/TurnObserver.js``,
            the file bots actually load. Compare to the same hash
            computed against the source dist to catch the
            sync-didn't-happen failure mode that motivated the
            self-verifying upgrade work.
        """
        import hashlib as _hashlib
        import os as _os
        import time as _time
        # admin pid_started_at: read from /proc-equivalent on macOS via ps.
        # Best-effort; the version response stays useful even when this lookup
        # fails (e.g. permission, ps unavailable).
        admin_started_iso: str | None = None
        try:
            proc = subprocess.run(
                ["/bin/ps", "-p", str(_os.getpid()), "-o", "lstart="],
                capture_output=True, text=True, timeout=3,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                admin_started_iso = proc.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            pass

        # plugin install md5 — same canonical file the verifier checks.
        plugin_md5: str | None = None
        plugin_path = Path("/Users/Shared/evolve-plugin/dist/observer/TurnObserver.js")
        try:
            with plugin_path.open("rb") as f:
                h = _hashlib.md5()
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
                plugin_md5 = h.hexdigest()
        except (FileNotFoundError, PermissionError, OSError):
            pass

        return jsonify({
            "admin_version": EVOLVE_VERSION,
            "admin_pid": _os.getpid(),
            "admin_pid_started_at": admin_started_iso,
            "plugin_install_md5": plugin_md5,
            "plugin_install_path": str(plugin_path),
            "queried_at": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })

    # ── Status ─────────────────────────────────────────────────────────────
    @app.get("/api/status")
    def api_status() -> Response:
        data = network_status(network_path)
        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        # Overlay cached security audit counts onto each bot's bot_data
        # before computing tile chips, so the chip rule can read them
        # without taking a separate dependency on the audit cache.
        # Counts come straight from /api/security/audit's cache; if the
        # cache is empty (audit never run since admin restart) the counts
        # are absent and the chip just stays quiet.
        try:
            _audit_data = (_audit_cache.get("data") or {}) if _audit_cache else {}
            for bot_id, bot_data in (data.get("bots") or {}).items():
                _ad = _audit_data.get(bot_id) or {}
                if "critical" in _ad:
                    bot_data["security_critical"] = int(_ad.get("critical") or 0)
                if "warned" in _ad:
                    bot_data["security_warn"] = int(_ad.get("warned") or 0)
        except Exception:
            pass
        # Overlay heal-written gateway probe (shared_dir/status/<bot>.json,
        # written every 5min by heal.py) so the tile chip rule can fire a
        # critical chip when gateway is unreachable — same source the
        # Maintenance tab uses. Without this, a bot with a recent metrics
        # file silently shows "Healthy" even when its gateway is down.
        # Freshness threshold matches /api/gateway/status (10 min).
        for bot_id, bot_data in (data.get("bots") or {}).items():
            try:
                _sf = json.loads((shared_dir / "status" / f"{bot_id}.json").read_text())
                _age = _time.time() - float(_sf.get("ts_epoch") or 0)
                if _age <= 600:
                    bot_data["gateway_running"] = bool(_sf.get("gateway_running"))
                    bot_data["gateway_reachable"] = bool(_sf.get("gateway_reachable"))
                    bot_data["gateway_status_fresh"] = True
            except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError, ValueError):
                pass
        # Per-bot + pod-wide circuit-breaker state overlay (Phase 4b).
        # Cheap — reads at most (N_bots * 2) + 2 small JSON files. Skips
        # expired trips (heal.py auto-clears them on its next cycle).
        # Each bot gets `active_breakers: [{type, trip_id, tripped_at,
        # expires_at, initiated_by, reason}]`; the response gets a
        # top-level `pod_breakers` list. Tile rendering uses these to
        # show the breaker pill without an extra round-trip.
        try:
            from breakers import store as _bstore
            _active = _bstore.list_active(shared_dir)
            _per_bot: dict[str, list[dict]] = {}
            _pod: list[dict] = []
            for r in _active:
                entry = {
                    "type": r.type,
                    "trip_id": r.trip_id,
                    "tripped_at": r.tripped_at,
                    "expires_at": r.expires_at,
                    "initiated_by": r.initiated_by,
                    "reason": r.reason,
                }
                if r.bot_id == "pod":
                    _pod.append(entry)
                else:
                    _per_bot.setdefault(r.bot_id, []).append(entry)
            for bot_id, bot_data in (data.get("bots") or {}).items():
                bot_data["active_breakers"] = _per_bot.get(bot_id, [])
            data["pod_breakers"] = _pod
        except Exception:
            # Never let breaker-store hiccups break the dashboard.
            for bot_id, bot_data in (data.get("bots") or {}).items():
                bot_data.setdefault("active_breakers", [])
            data.setdefault("pod_breakers", [])
        # Per-bot tile data: activity / cost / apps / health chips.
        try:
            from tile_metrics import compute_tile_data
            for bot_id, bot_data in (data.get("bots") or {}).items():
                try:
                    tile = compute_tile_data(
                        shared_dir=shared_dir,
                        bot_id=bot_id,
                        bot_data=bot_data,
                        network=network,
                    )
                    bot_data["tile"] = tile
                except Exception:
                    continue  # never let one bad bot break the whole response
        except Exception:
            pass

        # Per-bot Setup-checklist chip + Actions-menu flag.
        # tile_metrics lives in the analyzer package and can't import the
        # admin-side setup_checklist module (dependency direction). The
        # chip is post-attached here via the enrichment helper, which
        # appends a ``setup_progress`` chip when is_chip_visible and sets
        # the ``setup_in_actions_menu`` tile flag for the Overview action
        # ⋯ menu fallback when the chip is suppressed. Persist only when
        # at least one bot's state changed — save_network invokes sudo
        # chown on every write, so a per-request save on a quiet refresh
        # adds measurable cost.
        try:
            from .routes_setup_checklist import enrich_tiles_with_setup_chip
            if enrich_tiles_with_setup_chip(network, data.get("bots") or {}):
                try:
                    save_network(network, network_path)
                except Exception:
                    # Persistence is best-effort here — the chip will
                    # re-seed on the next request if this save loses.
                    pass
        except Exception:
            pass
        # Augment the primary admin bot with server uptime. Resolve the
        # primary id via the shared resolver, not the legacy literal "evolve"
        # — on a post-account-separation pod the primary is "evo", so the old
        # hardcode lost the uptime entirely (S2, spec-evo-account-separation).
        from primary_bot import primary_bot_id as _primary_bot_id  # type: ignore
        _primary_id = _primary_bot_id(network)
        if _primary_id and _primary_id in (data.get("bots") or {}):
            data["bots"][_primary_id]["admin_uptime_seconds"] = int(_time.time() - _START_TIME)
        # Augment with per-bot pending proposal counts. Observations/FYIs
        # (Investigation, VetoAnnotation, …) are NOT counted — the badge reflects
        # the *actionable* queue only (Effectiveness-Layer triage §11), so a pile
        # of "look into it" items doesn't inflate the number the operator sees.
        try:
            from arbiter.apply import is_informational_kind as _is_info_kind
            from arbiter import store as _arb_store  # Phase B sanctioned read
            _bot_proposal_counts: dict[str, int] = {}
            for _prop in _arb_store.iter_proposals(shared_dir, subdirs=("pending",)):
                _p = _prop.to_dict()
                _tb = _p.get("target_bot")
                if not _tb:
                    continue
                if _is_info_kind((_p.get("action") or {}).get("kind", "")):
                    continue
                _bot_proposal_counts[_tb] = _bot_proposal_counts.get(_tb, 0) + 1
            for bot_id, bot_data in (data.get("bots") or {}).items():
                bot_data["proposal_count"] = _bot_proposal_counts.get(bot_id, 0)
        except Exception:
            pass
        # Augment with per-bot API key status (parallel, cached 5 min — keys rarely change)
        try:
            from runtime.agent_runtime import get_runtime
            _oc_keys_get = get_runtime().keys_get
            _member_bots = [b for b, d in (data.get("bots") or {}).items()
                            if d.get("role") != "primary"]
            _now = _time.time()
            _stale = [b for b in _member_bots
                      if b not in _key_status_cache or _now - _key_status_cache[b][0] > 300]
            if _stale:
                with ThreadPoolExecutor(max_workers=4) as _ex:
                    _key_futs = {_ex.submit(_oc_keys_get, bid): bid for bid in _stale}
                    for _fut in as_completed(_key_futs, timeout=10):
                        _bid = _key_futs[_fut]
                        try:
                            _kr = _fut.result(timeout=8)
                            if _kr and "keys" in _kr:
                                _keys = _kr["keys"]
                                _has_any = any(
                                    v.get("api_key") or v.get("token")
                                    for v in _keys.values()
                                )
                                _entry = {
                                    "key_status": "ok" if _has_any else "missing",
                                    "key_providers": {
                                        p: {"api_key": v.get("api_key", False), "token": v.get("token", False)}
                                        for p, v in _keys.items()
                                    },
                                }
                            else:
                                _entry = {"key_status": "unknown"}
                        except Exception:
                            _entry = {"key_status": "unknown"}
                        _key_status_cache[_bid] = (_now, _entry)
            for _bid in _member_bots:
                if _bid in _key_status_cache:
                    data["bots"][_bid].update(_key_status_cache[_bid][1])
        except Exception:
            pass
        # Augment with per-bot Evolve version + tier/canary release state.
        # All the logic lives in release_manager.apply_release_status (keeps
        # this hot-hazard file from growing; canary-aware overlay tested there).
        try:
            install_info = read_install_json(shared_dir)
            bot_sync = get_bot_sync_status(network, install_info)
            from ..release_manager import apply_release_status as _apply_release
            _apply_release(data, network, shared_dir, bot_sync, EVOLVE_VERSION)
        except Exception:
            pass
        return jsonify(data)

    # ── Install status ─────────────────────────────────────────────────────
    @app.get("/api/install/status")
    def api_install_status() -> Response:
        """Return install.json data augmented with live per-bot sync state and
        orphaned plist detection."""
        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        install_info = read_install_json(shared_dir)
        bot_sync = get_bot_sync_status(network, install_info)

        orphaned: list[str] = []
        try:
            orphaned = [p.name for p in find_orphaned_plists(network)]
        except Exception:
            pass

        return jsonify({
            "evolve_version": EVOLVE_VERSION,
            "installed_at": (install_info or {}).get("installed_at"),
            "network_id": (install_info or {}).get("network_id"),
            "repo_path": (install_info or {}).get("repo_path"),
            "bots": (install_info or {}).get("bots", list(network.get("bots", {}).keys())),
            "bot_sync": bot_sync,
            "orphaned_plists": orphaned,
            "install_json_missing": install_info is None,
        })

    @app.get("/api/network")
    def api_network() -> Response:
        net = load_network(network_path)
        # Auto-detect from /etc/localtime so the Pod Config card can show
        # "Detected: <tz>" and the UI can render in the mini's local zone
        # without an explicit override. Stays out of network.json so the
        # file stays a clean record of operator-set values.
        net["timezone_detected"] = detect_system_timezone()
        net["timezone_effective"] = resolve_pod_timezone(net)
        # Never ship plaintext secrets (github.pat, channel botTokens, gateway
        # auth tokens, …) to the client. The UI reads presence/refs, not values.
        return jsonify(_redact_secrets(net))

    @app.get("/api/setup-status")
    def api_setup_status() -> Response:
        """First-time-setup detection.

        Drives the no-primary banner + install-evo wizard flow when
        setup_wizard.py was never run, ran partially, or left
        network.json with no usable primary. See spec docs/spec-add-bot-
        wizard-2026-05-28.md and PR for #1890 follow-up.
        """
        return jsonify(setup_status(network_path))

    @app.get("/api/admin/github-dev/status")
    def api_github_dev_status() -> Response:
        """Snapshot of pod-wide GitHub PAT (intake.github) state for the
        Phase E mini-wizard.

        The "third purpose" of GitHub credentials per
        ``project_github_credentials_three_purposes`` — distinct from
        per-bot backup (purpose 1) and per-bot MCP (purpose 2). When
        configured, Evolve can file dev issues against the operator's
        repo (intake adapter).

        Returns the normalized shape regardless of which storage form
        the operator wrote (v1 single-target vs v2 multi-target):

          {
            configured: bool,
            shape: "v1" | "v2" | null,
            targets: [{name, owner, repo, token_slot}],
            default_target: str | null,    # v2 only; null for v1
          }

        v1 entries surface as a single target with name="default" so the
        UI can render targets uniformly without branching on shape.
        """
        network = load_network(network_path)
        intake_gh = (network.get("intake") or {}).get("github") or {}
        targets: list[dict] = []
        shape: "str | None" = None
        default_target: "str | None" = None

        if isinstance(intake_gh, dict):
            # v2 first: has `targets` dict
            v2_targets = intake_gh.get("targets")
            if isinstance(v2_targets, dict) and v2_targets:
                shape = "v2"
                default_target = intake_gh.get("default") if isinstance(intake_gh.get("default"), str) else None
                for name, entry in v2_targets.items():
                    if not isinstance(entry, dict):
                        continue
                    targets.append({
                        "name": str(name),
                        "owner": entry.get("owner") or "",
                        "repo": entry.get("repo") or "",
                        "token_slot": entry.get("token_slot") or "",
                    })
            # v1 fallback: top-level owner+repo
            elif intake_gh.get("owner") and intake_gh.get("repo"):
                shape = "v1"
                targets.append({
                    "name": "default",
                    "owner": intake_gh.get("owner") or "",
                    "repo": intake_gh.get("repo") or "",
                    "token_slot": intake_gh.get("token_slot") or "github_intake",
                })

        return jsonify({
            "configured": bool(targets),
            "shape": shape,
            "targets": targets,
            "default_target": default_target,
        })

    @app.get("/api/admin/https-setup/status")
    def api_https_setup_status() -> Response:
        """Snapshot of HTTPS-on-LAN state for the Phase D mini-wizard.

        Composes existing helpers in ``https_setup.py`` — no new wiring
        for actually enabling HTTPS (that stays a CLI-driven host
        action). Returns:

          {
            current_scheme: "https" | "http",
            admin_url: str,
            tailscale_state: "ok" | "not_installed" | "not_signed_in" | "unknown",
            tailnet_host: str | null,   # e.g. "evolve-pod.tailfoo.ts.net"
            already_enabled: bool,      # current_scheme == "https"
            error: str | null,          # human-readable detail when state != "ok"
          }

        The admin daemon runs as the ``evolve`` user. ``tailscale status``
        is a read-only command that works for any user on the host on
        a normal Tailscale install, so this should succeed without
        elevated privileges — but if it doesn't (custom Tailscale policy,
        binary missing from PATH), ``tailscale_state`` lands on
        ``"unknown"`` and the modal degrades to "we can't tell; check on
        the host" mode.
        """
        from .. import https_setup
        network = load_network(network_path)
        admin_url = network.get("adminBaseUrl") or ""
        current_scheme = "https" if admin_url.startswith("https://") else "http"

        tailscale_state = "unknown"
        tailnet_host: "str | None" = None
        error: "str | None" = None
        try:
            status = https_setup._check_signed_in()
            tailscale_state = "ok"
            try:
                tailnet_host = https_setup._resolve_tailnet_hostname(status)
            except Exception:
                # Status came back but Self.DNSName empty — still "ok"
                # for sign-in purposes; tailnet_host stays None.
                pass
        except https_setup.TailscaleNotInstalled as exc:
            tailscale_state = "not_installed"
            error = str(exc)
        except https_setup.TailscaleNotSignedIn as exc:
            tailscale_state = "not_signed_in"
            error = str(exc)
        except Exception as exc:  # noqa: BLE001
            # CLI missing from PATH, permission denied, etc.
            error = str(exc)

        return jsonify({
            "current_scheme": current_scheme,
            "admin_url": admin_url,
            "tailscale_state": tailscale_state,
            "tailnet_host": tailnet_host,
            "already_enabled": current_scheme == "https",
            "error": error,
        })

    # ── Deploy ─────────────────────────────────────────────────────────────
    @app.post("/api/deploy")
    def api_deploy() -> Response:
        """Deploy or add+deploy a bot.

        Pod membership is explicit. The single registration path goes
        through `add_bot()`. If `botId` isn't yet in `network.bots`, this
        endpoint registers it first (this is the UI's Add Bot form).
        Otherwise it redeploys an existing bot. Either way, no other
        write path may add to the bot ledger — `deploy_bot()` itself
        refuses unknown ids.
        """
        body = request.json or {}
        bot_id = body.get("botId")
        role = body.get("role", "member")
        port = body.get("port")
        user = body.get("user")  # optional macOS user; defaults to bot_id
        dry_run = body.get("dryRun", False)
        multi_user = bool(body.get("multiUser", False))

        if not bot_id:
            return jsonify({"error": "botId required"}), 400

        results = []

        # Setup shared dir first
        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        sr = deploy_shared_dir(shared_dir, dry_run=dry_run)
        results.append(_result_dict("shared-dir", sr))

        # Register the bot if it isn't already a pod member. This is the
        # explicit Add Bot path: a human submitted the form, so the
        # registration is user-driven, not auto-discovered.
        if bot_id not in network.get("bots", {}) and not dry_run:
            if not port:
                return jsonify({
                    "error": f"port required to register new bot {bot_id!r}",
                }), 400
            try:
                add_bot(
                    bot_id,
                    role=role,
                    port=int(port),
                    user=user,
                    multi_user=multi_user,
                    network_path=network_path,
                )
            except ValueError as e:
                return jsonify({"error": str(e)}), 409

        # Deploy bot
        dr = deploy_bot(bot_id, role, port, network_path, dry_run=dry_run)
        results.append(_result_dict(bot_id, dr))

        # Persist multiUser flag for an existing bot (add_bot already set
        # it for newly-registered bots). deploy_bot preserves unknown
        # fields but doesn't accept this param, so patch directly.
        if not dry_run:
            _net = load_network(network_path)
            _bot_entry = _net.setdefault("bots", {}).get(bot_id)
            if _bot_entry is not None and _bot_entry.get("multiUser") != multi_user:
                _bot_entry["multiUser"] = multi_user
                save_network(_net, network_path)

        ok = all(r["success"] for r in results)
        return jsonify({"ok": ok, "results": results}), (200 if ok else 500)

    @app.patch("/api/bot/multi-user")
    def api_bot_multi_user() -> Response:
        """Toggle multiUser flag for a bot. PATCH {botId, multiUser: bool}"""
        body = request.get_json() or {}
        bot_id = body.get("botId")
        if not bot_id:
            return jsonify({"error": "botId required"}), 400
        if "multiUser" not in body:
            return jsonify({"error": "multiUser required"}), 400
        multi_user = bool(body["multiUser"])
        network = load_network(network_path)
        network.setdefault("bots", {}).setdefault(bot_id, {})["multiUser"] = multi_user
        save_network(network, network_path)
        return jsonify({"ok": True, "botId": bot_id, "multiUser": multi_user})

    # ── Bot purpose anchor (Effectiveness Layer, Phase B) ───────────────────────
    # What a bot is FOR — the Fit Reviewer (Layer 2) reads this to know what
    # "more effective" means for it. Stored at bots[<id>].purpose.
    # Spec: docs/spec-effectiveness-layer-2026-06-09.md §4.
    @app.get("/api/bot/<bot_id>/purpose")
    def api_get_bot_purpose(bot_id: str) -> Response:
        from bot_purpose import get_bot_purpose, ARCHETYPES
        network = load_network(network_path)
        return jsonify({
            "ok": True,
            "bot_id": bot_id,
            "purpose": get_bot_purpose(network, bot_id),
            "archetypes": list(ARCHETYPES),
        })

    @app.put("/api/bot/<bot_id>/purpose")
    def api_set_bot_purpose(bot_id: str) -> Response:
        """Declare a bot's purpose (operator-set). Body: {archetype, mission}.
        An empty/blank payload clears the purpose."""
        from bot_purpose import normalize_purpose
        body = request.get_json(silent=True) or {}
        network = load_network(network_path)
        if bot_id not in (network.get("bots") or {}):
            return jsonify({"error": f"unknown bot {bot_id!r}"}), 404
        purpose = normalize_purpose(
            {"archetype": body.get("archetype"), "mission": body.get("mission")},
            captured="declared",
        )
        if purpose is None:
            network["bots"][bot_id].pop("purpose", None)
        else:
            purpose["reviewed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            network["bots"][bot_id]["purpose"] = purpose
        save_network(network, network_path)
        return jsonify({"ok": True, "bot_id": bot_id, "purpose": purpose})

    @app.post("/api/remove")
    def api_remove() -> Response:
        """DEPRECATED — left as a 410 Gone for safety.

        The old ``deploy.remove_bot`` only removed a legacy ``measure``
        plist + network.json entry; it silently left 7+ live LaunchDaemons
        running (gateway, apply, test, cost-converter, audit-runner ×2,
        doctor-pass, backup) and never touched the macOS user or
        ``/Users/<bot>/``. Callers must move to the three explicit
        lifecycle endpoints below — there is no single "remove" semantics
        worth preserving.
        """
        return jsonify({
            "error": (
                "/api/remove is deprecated — use one of: "
                "/api/lifecycle/detach (keep bot, stop Evolve), "
                "/api/lifecycle/retire (archive + stop daemons, reversible), "
                "/api/lifecycle/delete (irreversible full removal)."
            ),
        }), 410

    # ── Lifecycle endpoints ────────────────────────────────────────────
    # Three first-class removal paths, mirroring the CLI commands:
    #   detach   → retire.remove_evolve_plugin   (keep bot, stop Evolve)
    #   retire   → retire.retire_bot              (archive + stop daemons)
    #   delete   → retire.delete_bot              (also nukes macOS user)
    # All accept ``botId`` (camelCase, the rest of the API's convention)
    # OR ``bot_id`` (snake_case, the shape the old broken frontend sent)
    # so we don't strand any in-flight callers on the rename.

    def _read_bot_id_param(body: dict) -> str | None:
        bot_id = body.get("botId") or body.get("bot_id")
        return bot_id.strip() if isinstance(bot_id, str) and bot_id.strip() else None

    def _retire_result_dict(bot_id: str, result: Any) -> dict:
        """Richer counterpart to ``_result_dict`` for RetireResult.

        Surfaces archive_path / summary_path / plists_stopped /
        plists_failed so the UI can render the inventory the operator
        needs (e.g. "X daemons stopped, archive lives at Y").
        """
        base = _result_dict(bot_id, result)
        base.update({
            "archive_path": str(result.archive_path) if getattr(result, "archive_path", None) else None,
            "summary_path": str(result.summary_path) if getattr(result, "summary_path", None) else None,
            "plists_stopped": list(getattr(result, "plists_stopped", []) or []),
            "plists_failed": list(getattr(result, "plists_failed", []) or []),
        })
        return base

    @app.post("/api/lifecycle/detach")
    def api_lifecycle_detach() -> Response:
        """Detach a bot from Evolve while leaving it running as an OC bot.

        Stops the per-bot evolve plists + strips the evolve plugin from
        the bot's openclaw.json. Gateway keeps running. Reversible: a
        re-deploy puts the plugin back.
        """
        body = request.json or {}
        bot_id = _read_bot_id_param(body)
        dry_run = bool(body.get("dryRun") or body.get("dry_run") or False)
        if not bot_id:
            return jsonify({"error": "botId required"}), 400
        from ..retire import remove_evolve_plugin
        result = remove_evolve_plugin(
            bot_id, network_path=network_path, dry_run=dry_run,
        )
        return jsonify(_retire_result_dict(bot_id, result)), (200 if result.success else 500)

    @app.post("/api/lifecycle/retire")
    def api_lifecycle_retire() -> Response:
        """Gracefully retire a bot — archive + stop daemons + remove from network.

        Archive directory is preserved under ``{shared_dir}/archived-bots/`` so the
        bot is reversible (its data, config, and manifest can be restored).
        macOS user account stays. Smaller-blast-radius alternative to delete.
        """
        body = request.json or {}
        bot_id = _read_bot_id_param(body)
        dry_run = bool(body.get("dryRun") or body.get("dry_run") or False)
        if not bot_id:
            return jsonify({"error": "botId required"}), 400
        from ..retire import retire_bot
        result = retire_bot(
            bot_id, network_path=network_path, dry_run=dry_run,
        )
        return jsonify(_retire_result_dict(bot_id, result)), (200 if result.success else 500)

    @app.post("/api/lifecycle/delete")
    def api_lifecycle_delete() -> Response:
        """Irreversibly delete a bot — retire + nuke macOS user + /Users/<bot>/.

        Requires ``confirmation: "DELETE"`` in the body as a server-side
        gate (in addition to the typed confirmation in the UI modal).
        Without it we 400 out before touching anything. Mirrors the
        CLI's typed-DELETE prompt.

        Safety: if the bot's macOS user does not match the bot_id (the
        piggyback case — bot runs under an existing user account), the
        user account is preserved
        even when ``confirmation`` is correct. See ``retire.delete_bot``.
        """
        body = request.json or {}
        bot_id = _read_bot_id_param(body)
        dry_run = bool(body.get("dryRun") or body.get("dry_run") or False)
        confirmation = body.get("confirmation")
        if not bot_id:
            return jsonify({"error": "botId required"}), 400
        if not dry_run and confirmation != "DELETE":
            return jsonify({
                "error": (
                    'confirmation field must equal "DELETE" (exact, '
                    "case-sensitive) for irreversible delete."
                ),
            }), 400
        from ..retire import delete_bot
        result = delete_bot(
            bot_id, network_path=network_path, dry_run=dry_run,
        )
        return jsonify(_retire_result_dict(bot_id, result)), (200 if result.success else 500)

    @app.post("/api/setup-shared")
    def api_setup_shared() -> Response:
        body = request.json or {}
        dry_run = body.get("dryRun", False)
        network = load_network(network_path)
        shared_dir = Path(body.get("sharedDir") or network.get("sharedDir", "/Users/Shared/evolve"))
        result = deploy_shared_dir(shared_dir, dry_run=dry_run)
        return jsonify(_result_dict("shared", result)), (200 if result.success else 500)

    # ── Upgrade ────────────────────────────────────────────────────────────
    @app.post("/api/upgrade")
    def api_upgrade() -> Response:
        """Start a pod-wide upgrade as a background job. Returns jobId immediately."""
        body = request.json or {}
        skip_plugin = body.get("skipPlugin", False)
        skip_deploy = body.get("skipDeploy", False)
        dry_run = body.get("dryRun", False)
        bot_id_filter = body.get("botId")  # None = all bots; str = single bot

        # Concurrency guard
        if _active_job_id:
            return jsonify({"error": "Another job is already running", "jobId": _active_job_id[0]}), 409

        network_snap = load_network(network_path)
        shared_dir = Path(network_snap.get("sharedDir", "/Users/Shared/evolve"))

        # Canary safety guard — refuse a direct upgrade that would race the
        # gated release pipeline (see release_manager.canary_upgrade_block).
        # dry-run is exempt: it mutates nothing. Fail-open on resolution error.
        if not dry_run:
            try:
                from ..release_manager import canary_upgrade_block as _canary_block
                _blk = _canary_block(network_snap)
                if _blk:
                    _resp = jsonify(_blk)
                    _resp.status_code = 409
                    return _resp
            except Exception as _exc:
                _log.warning("upgrade canary-guard failed (allowing): %s", _exc)

        # C1 deploy lock (deploy-resilience 2026-06-24): refuse a manual upgrade
        # racing the puller redeploy sweep (the starved-mini double-hammer).
        # NON-BLOCKING → synchronous, legible 409. Held across _run_upgrade,
        # released in its finally. dry-run mutates nothing, so it is exempt.
        _deploy_lock = None if dry_run else _dres.try_acquire_deploy_lock(shared_dir)
        if not dry_run and _deploy_lock is None:
            return jsonify({"error": "A deploy is already in progress (scheduled redeploy) — try again in a moment"}), 409

        job_id = _new_job("upgrade")

        def _run_upgrade():
            try:
                steps_total = 7 + (0 if skip_deploy else len(network_snap.get("members", [])))
                step = 0

                # ── 1. Orphan cleanup ──────────────────────────────────────
                step += 1
                _job_progress(job_id, step, steps_total, "Checking for orphaned jobs")
                _job_log(job_id, "Checking for orphaned launchd jobs...")
                try:
                    orphans = find_orphaned_plists(network_snap)
                    if orphans:
                        _job_log(job_id, f"Found {len(orphans)} orphaned plist(s) — removing", "warning")
                        removed, failures = remove_orphaned_plists(orphans, dry_run=dry_run)
                        for label in removed:
                            prefix = "[dry-run] would remove" if dry_run else "Removed orphan"
                            _job_log(job_id, f"  {prefix}: {label}")
                        for failure in failures:
                            _job_log(job_id, f"  Could not remove orphan: {failure}", "warning")
                    else:
                        _job_log(job_id, "No orphaned jobs found", "success")
                except Exception as e:
                    _job_log(job_id, f"Orphan check failed (non-fatal): {e}", "warning")

                # ── 2. git pull ────────────────────────────────────────────
                step += 1
                _job_progress(job_id, step, steps_total, "git pull")
                _job_log(job_id, "Running git pull...")
                if not dry_run:
                    from pathlib import Path as _Path
                    import subprocess as _sp
                    import pwd as _pwd
                    repo_root = _Path(__file__).resolve().parent
                    _git_found = False
                    for _ in range(10):
                        if repo_root.joinpath(".git").exists():
                            _git_found = True
                            break
                        repo_root = repo_root.parent
                    if not _git_found:
                        _job_log(job_id, "Could not locate .git directory — skipping git pull", "warning")
                    else:
                        git_dir = repo_root / ".git"
                        # Capture original owner before chowning so we can restore
                        # it afterward regardless of which user cloned the repo.
                        # On stat failure, fall back to the human running sudo.
                        # Never use a hardcoded admin name as a fallback —
                        # those leak machine-specific state into shared code.
                        try:
                            _orig_owner = _pwd.getpwuid(git_dir.stat().st_uid).pw_name
                        except Exception:
                            _orig_owner = os.environ.get("SUDO_USER", "") or None
                        if not _orig_owner:
                            _job_log(
                                job_id,
                                "Could not determine original .git owner — "
                                "skipping git pull to avoid leaving "
                                "evolve:staff ownership in place",
                                "warning",
                            )
                        else:
                            # Give evolve write access to .git so the pull can update
                            # FETCH_HEAD etc., then restore original owner so the human
                            # admin can still run `git pull` from the terminal.
                            _sp.run(
                                ["sudo", "/usr/sbin/chown", "-R", "evolve:staff", str(git_dir)],
                                capture_output=True, timeout=30,
                            )
                            try:
                                proc = _sp.run(
                                    ["git", "-c", f"safe.directory={repo_root}", "pull"],
                                    cwd=str(repo_root),
                                    capture_output=True, text=True, timeout=120,
                                )
                            finally:
                                # Always restore — timeout=30 guards against a hung chown
                                _sp.run(
                                    ["sudo", "/usr/sbin/chown", "-R",
                                     f"{_orig_owner}:staff", str(git_dir)],
                                    capture_output=True, timeout=30,
                                )
                            if proc.returncode == 0:
                                summary = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "up to date"
                                _job_log(job_id, f"git pull OK — {summary}", "success")
                            else:
                                _job_log(job_id, f"git pull warning: {proc.stderr.strip()[:200]}", "warning")
                else:
                    _job_log(job_id, "[dry-run] would run: git pull", "info")

                # ── 3. Rebuild plugin ──────────────────────────────────────
                if not skip_plugin:
                    step += 1
                    _job_progress(job_id, step, steps_total, "Rebuilding plugin")
                    _job_log(job_id, "Rebuilding TypeScript plugin...")
                    if not dry_run:
                        try:
                            build_plugin()
                            fix_plugin_permissions()
                            _job_log(job_id, "Plugin rebuilt OK", "success")
                        except Exception as e:
                            _job_log(job_id, f"Plugin build failed: {e}", "error")
                            _job_finish(job_id, error=f"Plugin build failed: {e}")
                            return
                    else:
                        _job_log(job_id, "[dry-run] would rebuild plugin", "info")
                else:
                    _job_log(job_id, "Skipping plugin rebuild (--skip-plugin)", "info")

                # ── 4–N. Per-bot deploy ────────────────────────────────────
                if not skip_deploy:
                    bots_cfg = network_snap.get("bots", {})
                    members = network_snap.get("members", [])
                    if bot_id_filter:
                        members = [b for b in members if b == bot_id_filter]

                    bots_ok: list[str] = []
                    bots_failed: list[str] = []
                    # Pre-deploy access heal (parity w/ install/wizard, which the web Upgrade path
                    # lacked): the OC gateway 0700-clamps each bot's .openclaw ACL mask → evolve
                    # EACCES'd on the FIRST ensure_plugin_config read ("Upgrade failed, 0 of N").
                    # Re-assert pod-wide here; in-loop re-clamp is covered by the EACCES-safe reads + safe_write_bot_config's mask reassert.
                    if not dry_run:
                        try:
                            ensure_pod_perms(network_path=network_path, check_only=False)
                        except Exception as _heal_e:
                            _job_log(job_id, f"pre-deploy perms heal warning: {_heal_e}", "warning")
                    for bot_id in members:
                        step += 1
                        _job_progress(job_id, step, steps_total, f"Deploying to {bot_id}")
                        _job_log(job_id, f"Deploying to {bot_id}...")
                        cfg = bots_cfg.get(bot_id, {})
                        t_role = cfg.get("role") or "member"
                        t_port = cfg.get("port")
                        try:
                            if not dry_run:
                                # Inject plugin config + install OC plugin
                                ensure_plugin_config(bot_id, network_snap)
                                install_oc_plugin(bot_id, port=t_port, network=network_snap)
                                dr = deploy_bot(bot_id, t_role, t_port, network_path, dry_run=False)
                                if dr.success:
                                    record_bot_deploy(bot_id, shared_dir)
                                    bots_ok.append(bot_id)
                                    _job_log(job_id, f"  {bot_id} OK", "success")
                                else:
                                    bots_failed.append(bot_id)
                                    for err in dr.errors:
                                        _job_log(job_id, f"  {bot_id} error: {err}", "error")
                            else:
                                _job_log(job_id, f"  [dry-run] would deploy to {bot_id}", "info")
                                bots_ok.append(bot_id)
                        except Exception as e:
                            bots_failed.append(bot_id)
                            _job_log(job_id, f"  {bot_id} failed: {e}", "error")
                else:
                    bots_ok, bots_failed = [], []

                # ── Final. Write install.json ──────────────────────────────
                step += 1
                _job_progress(job_id, step, steps_total, "Updating version record")
                if not dry_run:
                    try:
                        _net = load_network(network_path)
                        install_info = read_install_json(shared_dir) or {}
                        write_install_json(
                            shared_dir=shared_dir,
                            network_id=_net.get("networkId", install_info.get("network_id", "unknown")),
                            bots=list(_net.get("bots", {}).keys()),
                            repo_path=str(_net.get("repoPath", "")),
                        )
                        _job_log(job_id, f"install.json updated → v{EVOLVE_VERSION}", "success")
                    except Exception as e:
                        _job_log(job_id, f"Could not update install.json: {e}", "warning")

                if not bots_failed:
                    summary = f"Upgrade complete. {len(bots_ok)} bot(s) OK"
                    summary_level = "success"
                elif bots_ok:
                    summary = f"Upgrade partial — {len(bots_ok)} bot(s) OK, {len(bots_failed)} failed"
                    summary_level = "warning"
                else:
                    summary = f"Upgrade failed — 0 of {len(bots_failed)} bot(s) upgraded"
                    summary_level = "error"
                _job_log(job_id, summary, summary_level)

                # Mark the job complete BEFORE restarting the admin server.
                # `launchctl kickstart -k` SIGTERMs this Python process, so any
                # _job_finish call placed after the restart never runs and the
                # polling client (every 2s) sees only 404s on the new process,
                # which has an empty in-memory _jobs dict. Order: finish → log
                # restart-pending → sleep > poll interval → restart.
                step += 1
                _job_progress(job_id, step, steps_total, "Finalizing")
                _job_finish(job_id, result={
                    "bots_ok": bots_ok,
                    "bots_failed": bots_failed,
                    "dry_run": dry_run,
                    "restarted": not dry_run,
                })

                # ── Final: Restart admin server ────────────────────────────
                step += 1
                _job_progress(job_id, step, steps_total, "Restarting admin server")
                if not dry_run:
                    _job_log(job_id, "Restarting admin server to apply code changes…", "info")
                    # Wait long enough that the next poll lands a "complete"
                    # status — pollJob runs at 2s; 5s gives 1–2 cycles of margin.
                    _time.sleep(5)
                    from .. import service as _svc_mod
                    ok_r, msg_r = _svc_mod.restart()
                    _job_log(job_id, msg_r, "success" if ok_r else "warning")
                else:
                    _job_log(job_id, "[dry-run] would restart admin server", "info")

            except Exception as e:
                _job_log(job_id, f"Upgrade failed: {e}", "error")
                _job_finish(job_id, error=str(e))
            finally:
                _dres.release_deploy_lock(_deploy_lock)  # no-op on dry-run / restart SIGTERM

        t = threading.Thread(target=_run_upgrade, daemon=True)
        try:
            t.start()
        except Exception:
            _dres.release_deploy_lock(_deploy_lock)  # thread never ran → its finally won't; avoid a leaked lock
            raise
        return jsonify({"jobId": job_id, "status": "started"}), 202

    # ── Job status ─────────────────────────────────────────────────────────
    @app.get("/api/jobs/<job_id>")
    def api_job_status(job_id: str) -> Response:
        """Poll a background job started by /api/upgrade or /api/uninstall."""
        with _jobs_lock:
            job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "job not found", "jobId": job_id}), 404
        return jsonify(job)

    # ── Uninstall ──────────────────────────────────────────────────────────
    @app.post("/api/uninstall")
    def api_uninstall() -> Response:
        """Remove all Evolve launchd jobs and optionally shared data.

        Requires confirm="UNINSTALL" in the request body to prevent accidents.
        keepUi=true skips removing the admin-ui plist so the server survives.
        keepData=true preserves /Users/Shared/evolve/ data.
        """
        body = request.json or {}

        if body.get("confirm") != "UNINSTALL":
            return jsonify({"error": "confirm field must be the string 'UNINSTALL'"}), 400

        keep_data = body.get("keepData", True)
        keep_ui = body.get("keepUi", True)
        dry_run = body.get("dryRun", False)

        if _active_job_id:
            return jsonify({"error": "Another job is already running", "jobId": _active_job_id[0]}), 409

        job_id = _new_job("uninstall")
        network_snap = load_network(network_path)

        def _run_uninstall():
            import subprocess as _sp
            from pathlib import Path as _Path

            # 3-second delay so HTTP response reaches the client before we
            # potentially kill the server
            _time.sleep(3)

            try:
                shared_dir = _Path(network_snap.get("sharedDir", "/Users/Shared/evolve"))
                bots = network_snap.get("bots", {})

                # ── 1. Remove per-bot launchd jobs ─────────────────────────
                _job_log(job_id, "Removing per-bot launchd jobs...")
                from pathlib import Path as _P
                for pattern in ("ai.openclaw.evolve.*.plist", "ai.evolve.*.plist"):
                    for plist in sorted(_P("/Library/LaunchDaemons").glob(pattern)):
                        if keep_ui and "admin-ui" in plist.name:
                            _job_log(job_id, f"  Keeping (keepUi): {plist.name}")
                            continue
                        label = plist.stem
                        if not dry_run:
                            # Scheduler.remove() = bootout + delete the plist.
                            # Result deliberately ignored — best-effort, same
                            # as the historical unchecked subprocess calls.
                            get_scheduler().remove(label)
                        _job_log(job_id, f"  Removed: {label}", "success")

                # ── 2. Remove bot workspace evolve/ dirs ───────────────────
                _job_log(job_id, "Removing bot workspace directories...")
                from ..config import get_bot_workspace
                for bot_id in bots:
                    ws = get_bot_workspace(bot_id)
                    if ws:
                        evolve_dir = ws / "evolve"
                        if evolve_dir.exists() and not dry_run:
                            _sp.run(["sudo", "rm", "-rf", str(evolve_dir)], capture_output=True)
                        _job_log(job_id, f"  Removed workspace for {bot_id}", "success")

                # ── 3. Remove network.json ─────────────────────────────────
                _job_log(job_id, "Removing network.json...")
                if not dry_run and network_path.exists():
                    _sp.run(["sudo", "rm", "-f", str(network_path)], capture_output=True)
                _job_log(job_id, "  network.json removed", "success")

                # ── 4. Optionally remove shared data ───────────────────────
                if not keep_data:
                    _job_log(job_id, f"Removing shared data at {shared_dir}...", "warning")
                    if not dry_run:
                        _sp.run(["sudo", "rm", "-rf", str(shared_dir)], capture_output=True)
                    _job_log(job_id, "  Shared data removed", "success")
                else:
                    _job_log(job_id, f"Shared data preserved at {shared_dir}")

                _job_finish(job_id, result={
                    "keep_data": keep_data,
                    "keep_ui": keep_ui,
                    "dry_run": dry_run,
                })
                _job_log(job_id, "Uninstall complete", "success")
                if not keep_ui and not dry_run:
                    _job_log(job_id, "Server will shut down now", "warning")

            except Exception as e:
                _job_log(job_id, f"Uninstall failed: {e}", "error")
                _job_finish(job_id, error=str(e))

        t = threading.Thread(target=_run_uninstall, daemon=True)
        t.start()

        warning = None if keep_ui else "The admin UI server will shut down in approximately 5 seconds."
        return jsonify({"jobId": job_id, "status": "started", "warning": warning}), 202

    # ── Config ─────────────────────────────────────────────────────────────
    @app.post("/api/config")
    def api_config() -> Response:
        from .capability_gate import pod_admin_denial  # function-level: E402 this far down
        body = request.json or {}
        network = load_network(network_path)
        action = body.get("action")
        if action == "alerts":
            network["alerts"] = {**network.get("alerts", {}), **body.get("alerts", {})}
        elif action == "appTesting":
            network["app_testing"] = {
                **network.get("app_testing", {}),
                **body.get("appTesting", {}),
            }
        elif action == "backup":
            # Backup card on Pod Config → Network. Only defaultBackupAccount
            # left after the per-bot-daemons refactor (backupSshKey
            # retired — keys now live per-bot under each bot's ~/.ssh/).
            # Empty/None clears the setting.
            bk = body.get("backup", {}) if isinstance(body.get("backup"), dict) else {}
            if "defaultBackupAccount" in bk:
                v = bk["defaultBackupAccount"]
                if v is None or (isinstance(v, str) and not v.strip()):
                    network.pop("defaultBackupAccount", None)
                else:
                    network["defaultBackupAccount"] = v.strip() if isinstance(v, str) else v
            # Migration safety: if a legacy backupSshKey is still in the
            # body (old UI talking to new server), silently ignore it
            # instead of writing — per-bot keys are the new model.
            # Also drop any pre-existing backupSshKey from network.json
            # so the audit doesn't keep showing a dead reference.
            network.pop("backupSshKey", None)
        elif action == "heal":
            # Self-Healing card on Pod Config → Network. Allowlist the
            # known heal knobs so the daemon doesn't end up with stray
            # keys via a malformed request. Null/missing values drop the
            # key (so the daemon falls back to its hardcoded default).
            heal_in = body.get("heal", {}) if isinstance(body.get("heal"), dict) else {}
            _HEAL_KEYS = {
                "failuresBeforeProposal", "windowHours",
                "slowThresholdMs", "restartCooldownMin", "checkTimeoutSec",
            }
            existing = dict(network.get("heal") or {})
            for k in _HEAL_KEYS:
                if k not in heal_in:
                    continue
                v = heal_in[k]
                if v is None:
                    existing.pop(k, None)
                else:
                    try:
                        existing[k] = int(v)
                    except (TypeError, ValueError):
                        existing.pop(k, None)
            if existing:
                network["heal"] = existing
            else:
                network.pop("heal", None)
        elif action == "classifiers":
            # Classifiers card. One sub-dict (tier). Merge into existing
            # so future sub-keys aren't clobbered. The `judge` sub-dict was
            # removed 2026-08-21: nothing read network.classifiers.judge, so
            # the control saved a value no code path consumed.
            cls_in = body.get("classifiers", {}) if isinstance(body.get("classifiers"), dict) else {}
            existing = dict(network.get("classifiers") or {})
            tier_in = cls_in.get("tier") if isinstance(cls_in.get("tier"), dict) else None
            if tier_in:
                merged = dict(existing.get("tier") or {})
                for k in ("tier", "fallback"):
                    if k in tier_in and tier_in[k]:
                        merged[k] = tier_in[k]
                existing["tier"] = merged
            if existing:
                network["classifiers"] = existing
        # The "security" action was removed 2026-08-14 with the Settings
        # Security card: every field it wrote (mode / requireForge /
        # autoRejectRisk) configured review.py, retired by #3641. The one
        # live field of network.json::security, `botId`, is set by the setup
        # wizard, never by this route.
        elif action == "pod":
            # Pod Identity card. Allowlist three scalar fields; null / empty
            # clears them (config._apply_pod_defaults then backfills the
            # passphrase defaults — "charles" / "darwin"). Leaves pod.admins.*
            # alone (separate action below). Pod-admin gated — the passphrase
            # writes pivot to the same escalation; see web/capability_gate.py.
            if (denied := pod_admin_denial(network, "pod identity edit")):
                return denied
            pod_in = body.get("pod", {}) if isinstance(body.get("pod"), dict) else {}
            existing = dict(network.get("pod") or {})
            for k in ("admin_passphrase", "primary_passphrase", "ssh_target"):
                if k not in pod_in:
                    continue
                v = pod_in[k]
                if v is None or (isinstance(v, str) and not v.strip()):
                    existing.pop(k, None)
                else:
                    existing[k] = v.strip() if isinstance(v, str) else v
            network["pod"] = existing
        elif action == "timezone":
            # Pod Timezone card. Single IANA name (e.g. "America/Los_Angeles").
            # Validate with zoneinfo so a typo doesn't silently leave the UI
            # rendering wall-clock-from-nowhere. Empty/None clears the
            # override, at which point resolve_pod_timezone() auto-detects
            # from /etc/localtime.
            tz_in = body.get("timezone")
            if tz_in is None or (isinstance(tz_in, str) and not tz_in.strip()):
                network.pop("timezone", None)
            else:
                tz_str = tz_in.strip() if isinstance(tz_in, str) else str(tz_in)
                try:
                    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
                    ZoneInfo(tz_str)
                except ZoneInfoNotFoundError:
                    return jsonify({
                        "error": f"unknown IANA timezone {tz_str!r}",
                    }), 400
                except Exception as e:
                    return jsonify({
                        "error": f"invalid timezone {tz_str!r}: {e}",
                    }), 400
                network["timezone"] = tz_str
        elif action == "podAdmins":
            # Pod Admins list edit. Op-based (add | remove) so concurrent edits
            # to different channels don't clobber each other. Pod-admin gated —
            # same pod-wide promotion as claim-admin; see web/capability_gate.py.
            spec = body.get("podAdmins", {}) if isinstance(body.get("podAdmins"), dict) else {}
            op = spec.get("op")
            channel = (spec.get("channel") or "").strip()
            ext_id = (spec.get("external_id") or "").strip()
            if op not in ("add", "remove") or not channel or not ext_id:
                return jsonify({"error": "podAdmins requires op (add|remove), channel, external_id"}), 400
            if (denied := pod_admin_denial(network, "pod admin list edit")):
                return denied
            pod = dict(network.get("pod") or {})
            admins = dict(pod.get("admins") or {})
            # external_ids owns the list edit (tolerant read of a hand-edited
            # bare-string shape, list back out, emptied channel key dropped).
            if op == "add":
                _external_ids.add_external_id(admins, channel, ext_id)
            else:
                _external_ids.remove_external_id(admins, channel, ext_id)
                # Also drop the resolved_names cache entry for this pair so
                # a re-add later doesn't show a stale name.
                resolved = dict(admins.get("resolved_names") or {})
                resolved.pop(f"{channel}:{ext_id}", None)
                admins["resolved_names"] = resolved
            pod["admins"] = admins
            network["pod"] = pod
        else:
            # Reject unknown actions — the legacy "no-action key merges
            # top-level keys" fallback was a vector for the thresholds
            # dual-write-path conflict (PR #1526 audit findings). Every
            # network.json write should now route through an explicit
            # action so the policy is reviewable in one place.
            return jsonify({
                "error": f"unknown action {action!r}",
                "valid_actions": [
                    "alerts", "appTesting", "backup", "heal",
                    "classifiers", "security", "pod", "podAdmins",
                    "timezone",
                ],
            }), 400
        save_network(network, network_path)
        return jsonify({"ok": True})

    # ── Proposals ──────────────────────────────────────────────────────────
    # The legacy /api/proposals* endpoints lived here. They have been
    # removed in stages alongside the consolidation onto the v2 arbiter:
    # mutation routes (approve / reject / promote / defer) and the
    # /api/analyze/trigger button were removed in #511; the GET /api/proposals
    # read endpoint was the last remaining v1 surface and is removed in
    # this PR. The two consumers that used it (the Overview digest tile
    # and the Analytics funnel chart) now read from /api/arbiter/proposals
    # directly.

    # ── Applications ───────────────────────────────────────────────────────
    @app.post("/api/applications/scan")
    def api_applications_scan() -> Response:
        bot_id = request.args.get("bot") or (request.json or {}).get("bot")
        if not bot_id:
            return jsonify({"error": "bot required"}), 400
        # ?quick=1 skips LLM — fast evidence-only scan, no manifest generation
        quick = request.args.get("quick", "0") in ("1", "true")
        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        import tempfile as _tempfile
        # fcntl.flock so a crashed/restarted admin server doesn't leak the
        # lock — the kernel releases it when the process dies. Same shape as
        # app_audit_runner._acquire_lock. The lockfile itself may linger on
        # disk as an artifact; only the flock matters.
        lock_file = Path(_tempfile.gettempdir()) / f".evolve-scan-{bot_id}.lock"
        try:
            lock_fh = lock_file.open("a+")
        except OSError as exc:
            return jsonify({"error": f"cannot open scan lockfile: {exc}"}), 500
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            try:
                lock_fh.close()
            except Exception:
                pass
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                return jsonify({"status": "already_running"})
            raise
        # Stamp the file with PID + start time for `lsof`-free debugging
        # ("who holds the lock?"). Not used for liveness — flock is.
        try:
            lock_fh.seek(0)
            lock_fh.truncate()
            lock_fh.write(f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n")
            lock_fh.flush()
        except Exception:
            pass
        # Best-effort deletion of stale .scan-status.json so the status endpoint
        # doesn't show old phase info at the start of the new scan.
        # The status file is owned by the bot user; evolve can delete it only if
        # ACL write is set on the parent dir. If not, the scanner overwrites it quickly.
        _stale_user = _resolve_bot_user(bot_id)
        _stale_path = _bot_manifests_dir(bot_id, user=_stale_user) / ".scan-status.json"
        try:
            _stale_path.unlink(missing_ok=True)
        except Exception:
            pass  # Not critical — scanner will overwrite on start
        _scan_log: list[str] = []

        def _log(msg: str) -> None:
            import time as _t
            line = f"[{_t.strftime('%H:%M:%S')}] {msg}"
            _scan_log.append(line)
            _scan_status[bot_id]["log"] = "\n".join(_scan_log[-100:])

        # phase_total matches PHASE_TOTAL in scanner.py (8 as of 2026-06-08).
        # Hardcoded here to avoid importing scanner.py from the web layer.
        # If SCAN_PHASES grows, update this number to match.
        _scan_status[bot_id] = {"status": "running", "phase": 1, "phase_total": 8,
                                 "phase_desc": "Starting scan…", "eta_seconds": 70,
                                 "found": 0, "manifests_created": 0, "manifests_total": 0,
                                 "log": ""}

        def _count_manifests(bot_id: str) -> int:
            return _count_bot_manifests(bot_id)

        def _monitor() -> None:
            # NOTE: The plugin API path (/evolve/applications/scan on the bot) is
            # intentionally NOT used here.  That handler delegates back to THIS server
            # at /api/applications/scan, which hits the lock and returns
            # "already_running" — a deadlock where the scanner never actually runs.
            # The correct approach is to run the scanner directly as the bot user via
            # subprocess (requires the sudoers grant added in setup_wizard.py §7a).
            try:
                import os as _os
                returncode = 0
                scanner_py = Path(_os.path.dirname(__file__)).parent.parent.parent / \
                    "analyzer" / "application_scanner.py"
                scan_user = _resolve_bot_user(bot_id)
                if scanner_py.exists():
                    # Interpreter MUST be the venv python (has the packaged
                    # evolve_admin) — NOT /usr/bin/python3 (system 3.9, no
                    # packaged modules → ModuleNotFoundError, every scan dies).
                    # config.scanner_python() is the single source of truth shared
                    # with the §7a sudoers grant; sudo matches the command string
                    # literally, so the two must agree exactly.
                    # sudo strips the environment, so read the primary bot's LLM
                    # provider keys here and inject them via SETENV (requires the
                    # SETENV tag in setup_wizard.py §7a) — the subprocess runs as
                    # the scanned bot, which can't read the primary's auth store,
                    # and infra_llm resolution honors these env overrides.
                    from ..config import scanner_python as _scanner_python
                    from primary_bot import provider_key_env_assignments  # type: ignore
                    cmd = ["sudo", "-u", scan_user]
                    try:
                        cmd += provider_key_env_assignments()
                    except Exception:  # noqa: BLE001 — scan degrades to --no-llm
                        pass
                    cmd += [_scanner_python(), str(scanner_py),
                            "--bot", bot_id, "--user", scan_user,
                            "--shared-dir", str(shared_dir)]
                    if quick:
                        cmd.append("--no-llm")
                    _log_cmd = [
                        c.split("=", 1)[0] + "=***" if "_API_KEY=" in c else c
                        for c in cmd
                    ]
                    _log(f"Running scanner subprocess: {' '.join(_log_cmd)}")
                else:
                    _log(f"WARNING: scanner_py not found at {scanner_py}")
                    cmd = ["evolve-admin", "--network", str(network_path),
                           "application", "scan", bot_id, "--auto-approve"]
                    if quick:
                        cmd.append("--no-llm")
                    _log(f"Falling back to legacy CLI: {' '.join(cmd)}")

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                )
                # Use communicate() — proc.wait() with PIPE can deadlock on large output
                try:
                    stdout_b, stderr_b = proc.communicate(timeout=360)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout_b, stderr_b = proc.communicate()
                    _log("ERROR: subprocess timed out after 360s")

                returncode = proc.returncode
                stdout_str = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
                stderr_str = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
                if stdout_str:
                    for line in stdout_str.splitlines():
                        _log(f"stdout: {line}")
                if stderr_str:
                    for line in stderr_str.splitlines():
                        _log(f"stderr: {line}")
                _log(f"Process exited with code {returncode}")

                # After subprocess completes, pull the scanner's verbose log from the
                # status file (it contains per-phase detail not in stdout).
                _poll_user = _resolve_bot_user(bot_id)
                _poll_path = _bot_manifests_dir(bot_id, user=_poll_user) / ".scan-status.json"
                try:
                    _st = json.loads(_poll_path.read_text())
                except Exception:
                    _r2 = subprocess.run(
                        ["sudo", "/bin/cat", str(_poll_path)],
                        capture_output=True, text=True, timeout=5,
                    )
                    try:
                        _st = json.loads(_r2.stdout) if _r2.returncode == 0 else {}
                    except Exception:
                        _st = {}
                scanner_log = _st.get("log", "")
                if scanner_log:
                    _scan_status[bot_id]["log"] = (
                        "\n".join(_scan_log) + "\n---scanner---\n" + scanner_log
                    )

                # If the scanner wrote a terminal ``status: error`` (e.g.
                # MissingApiKeyError → status file gets ``error`` + ``error_kind``),
                # propagate it. Otherwise honour the subprocess exit code.
                scanner_status = _st.get("status") if isinstance(_st, dict) else None
                final_status: str
                if scanner_status == "error":
                    final_status = "error"
                elif returncode == 0:
                    final_status = "done"
                else:
                    final_status = "error"

                # v7-arc promotion: the scan subprocess runs as the bot user,
                # which cannot create a new Spec under the evolve-owned shared
                # gallery — so its native mint EACCESes and writes a legacy
                # manifest. Re-run the identical mint here, in the admin
                # process (evolve, which owns the gallery), to convert this
                # bot's fresh legacy manifests to native v7-arc (the shape
                # Reflect + the v7 readers need). Best-effort: failures leave
                # the valid legacy manifests untouched.
                if final_status == "done":
                    try:
                        from ..applications.native_write import (
                            convert_scanned_bot_to_v7_arc,
                        )
                        _promoted = convert_scanned_bot_to_v7_arc(
                            bot_id=bot_id,
                            shared_dir=shared_dir,
                            caps_dir=_bot_manifests_dir(bot_id, user=_poll_user),
                        )
                        if _promoted:
                            _log(f"v7-arc promotion: converted {len(_promoted)} legacy manifest(s) to native v7-arc")
                    except Exception as _exc:  # noqa: BLE001 — never break the scan
                        _log(f"v7-arc promotion skipped: {_exc}")

                count = _count_manifests(bot_id)
                _log(f"Final manifest count: {count}")
                # Build locally, then publish with a single atomic dict
                # assignment — never add keys to an already-published
                # _scan_status entry in place. A concurrent scan-status poll
                # splats {**entry}; an in-place key-add mid-iteration raises
                # "dict changed size during iteration" now that the admin
                # server runs threaded (cli.py app.run(threaded=True)).
                _final = {"status": final_status, "count": count, "log": "\n".join(_scan_log) + (("\n---scanner---\n" + scanner_log) if scanner_log else "")}
                if isinstance(_st, dict):
                    if _st.get("error"):
                        _final["error"] = _st["error"]
                    if _st.get("error_kind"):
                        _final["error_kind"] = _st["error_kind"]
                _scan_status[bot_id] = _final
                # The scan_needed chip rule now reads the scanner's own status
                # file at /Users/<bot_user>/.openclaw/workspace/manifests/.scan-status.json
                # directly (via macOS ACL read). Previously this block mirrored
                # that file into {shared_dir}/applications/{bot_id}/.scan-status.json
                # so tile_metrics could read it without crossing into bot homes,
                # but the mirror swallowed OSErrors silently and left the chip
                # stuck with no log trail. The mirror is gone; the scanner is
                # the single source of truth.
            except Exception as exc:
                log_request_error(exc)  # full traceback → admin log, never the client-visible scan log
                _log(f"EXCEPTION in _monitor: {exc}")
                _scan_status[bot_id] = {"status": "error", "count": 0, "error": str(exc), "log": "\n".join(_scan_log)}
            finally:
                # Close the file handle first — that releases the flock.
                # Unlink is best-effort; if it fails the next scan can still
                # acquire because flock is released.
                try:
                    lock_fh.close()
                except Exception:
                    pass
                try:
                    lock_file.unlink(missing_ok=True)
                except OSError:
                    pass

        threading.Thread(target=_monitor, daemon=True).start()
        return jsonify({"status": "started", "quick": quick})

    @app.get("/api/applications/scan/status")
    def api_applications_scan_status() -> Response:
        bot_id = request.args.get("bot")
        if not bot_id:
            return jsonify({"error": "bot required"}), 400

        mem_status = _scan_status.get(bot_id, {"status": "idle", "count": 0})

        # If scan is running, enrich with phase data written by the CLI subprocess.
        # The status file lives in the bot workspace — direct read (ACL) or sudo /bin/cat.
        if mem_status.get("status") == "running":
            _st_user = _resolve_bot_user(bot_id)
            _st_path = str(_bot_manifests_dir(bot_id, user=_st_user) / ".scan-status.json")
            _st_text = None
            try:
                _st_text = Path(_st_path).read_text()
            except Exception:
                _r2 = subprocess.run(["sudo", "/bin/cat", _st_path], capture_output=True, text=True, timeout=5)
                if _r2.returncode == 0:
                    _st_text = _r2.stdout
            if _st_text:
                try:
                    phase_data = json.loads(_st_text)
                    # Prefer the scanner's embedded log over the admin server's poll log
                    # (scanner log has Phase 5 detail that the polling path never captures)
                    if phase_data.get("log") and not mem_status.get("log"):
                        phase_data.setdefault("log", phase_data["log"])
                    # Cache the latest phase fields back into mem_status so the
                    # fallback path at line 2551 returns the LAST KNOWN GOOD
                    # progress when a subsequent poll hits the file mid-write
                    # (atomic write = tmp+rename, brief unreadability). Without
                    # this, every transient read failure regresses the UI from
                    # "phase 2 of 8" back to the stale seed "phase 1 of 8".
                    # Production observation 2026-06-08.
                    # Re-publish with the phase fields merged in as a fresh
                    # dict (atomic assign, no in-place key-add) — same
                    # threaded-safety invariant as the _monitor block above.
                    _cur = _scan_status.get(bot_id)
                    if isinstance(_cur, dict):
                        _scan_status[bot_id] = {**_cur, **{
                            k: phase_data[k] for k in (
                                "phase", "phase_total", "phase_desc",
                                "phase_name", "eta_seconds", "found",
                                "manifests_created", "manifests_total",
                                "current_app_name", "current_app_num",
                            ) if k in phase_data
                        }}
                    merged = {**mem_status, **phase_data, "status": "running"}
                    return jsonify(merged)
                except Exception:
                    pass

        # Always include log so UI can show what happened
        return jsonify(mem_status)

    @app.get("/api/applications/scan/log")
    def api_applications_scan_log() -> Response:
        """Return the raw scan log for debugging."""
        bot_id = request.args.get("bot")
        if not bot_id:
            return jsonify({"error": "bot required"}), 400
        status = _scan_status.get(bot_id, {})
        log = status.get("log", "(no log — scan not yet run or server restarted)")
        return Response(log, mimetype="text/plain")

    @app.post("/api/applications")
    def api_applications_create() -> Response:
        body = request.get_json() or {}
        bot_id = body.get("bot_id")
        cap_id = body.get("id")
        name = body.get("name")
        if not bot_id or not cap_id or not name:
            return jsonify({"error": "bot_id, id, and name required"}), 400
        network = load_network(network_path)
        bot_user = (network.get("bots") or {}).get(bot_id, {}).get("user") or bot_id
        expected_kw = body.get("expected_keywords", [])
        if isinstance(expected_kw, str):
            expected_kw = [k.strip() for k in expected_kw.split(",") if k.strip()]
        now_str = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
        description = body.get("description", "")
        priority = body.get("priority", "feature")
        test_trigger = body.get("test_trigger", "")
        from ..applications.manifest import MANIFEST_SCHEMA_VERSION
        # NOTE 2026-05-27: the `lifecycle` field (spec_drafted / built /
        # qa_status / rsi_status / build_history) used to be initialized
        # here. It got removed alongside the forge-job lifecycle UI block —
        # that progression is forge-job state and belongs on the Forge Jobs
        # tab, not on installed-app manifests. `source: "user_created"` is
        # the authoritative "how was this created" signal; nothing else
        # consumed the lifecycle block.
        manifest = {
            "id": cap_id,
            "bot_id": bot_id,
            "name": name,
            "description": description,
            "priority": priority,
            "satisfaction_score": None,
            "test_trigger": test_trigger,
            "expected_keywords": expected_kw,
            "created_at": now_str,
            "updated_at": now_str,
            "source": "user_created",
            "source_detail": f"ui:create:{now_str}",
            "schema_version": MANIFEST_SCHEMA_VERSION,
        }
        if not _write_manifest_as_bot(bot_id, cap_id, manifest, user=bot_user):
            return jsonify({"error": "Failed to write manifest to bot workspace"}), 500

        return jsonify({"ok": True, "id": cap_id})

    @app.get("/api/applications/<bot_id>/<app_id>")
    def api_application_manifest(bot_id: str, app_id: str) -> Response:
        """Return a single application manifest as JSON.

        For v7-arc Instance manifests, the bound Spec's presentation fields
        (name, description, tags, objective) are overlaid so the UI's existing
        rendering keeps working without v7-aware code in every consumer.

        Resolution: filename match first (fast path); if that fails, fall
        back to scanning manifests for ``id`` / ``instance_id`` equal to
        ``app_id``. Required because gallery-installed v7-arc-pre manifests
        (e.g. atlas-article-capture.json) carry an internal id
        (``app_atlas_article_capture``) that the list endpoint hands back
        to the UI, while the file lives at the display-slug filename.
        """
        manifest = _read_manifest_as_bot(bot_id, app_id)
        if manifest is None:
            manifest = _find_manifest_by_id_field(bot_id, app_id)
        if manifest is None:
            return jsonify({"error": "not found"}), 404
        if manifest.get("manifest_shape") == "v7-arc":
            from ..applications.manifest import hydrate_v7_arc_instance
            from ..config import load_network
            shared_dir = Path(load_network(network_path).get("sharedDir", "/Users/Shared/evolve"))
            manifest = hydrate_v7_arc_instance(manifest, shared_dir)
        return jsonify(manifest)

    def _shared() -> Path:
        """Live shared-dir read for the application routes below.

        Restores the create_app-scope helper the 4.1 server.py
        decomposition (#2549/#2557) carried away with the extracted route
        modules — ``_app_lifecycle`` and the dependents route still
        referenced the name, so pause/unpause/archive/restore and
        ``GET .../dependents`` were 500ing with NameError ever since
        (caught by test_app_lifecycle_guidance_unsplice.py)."""
        from evolve_config import CANONICAL_SHARED_DIR

        from ..config import load_network
        return Path(
            load_network(network_path).get("sharedDir", str(CANONICAL_SHARED_DIR))
        )

    def _resync_guidance_on_status_change(
        bot_id: str, old_status: str, new_status: str,
    ) -> None:
        """Regenerate INSTALLED_APPS.md + the AGENTS.md marker block when a
        manifest's status changed (base-spec §8.4 step 2 — pause/deprecate
        unsplice guidance; unpause/reactivate re-splice). Best-effort:
        regeneration filters to _VISIBLE_STATUSES, and a failure here never
        fails the write that triggered it."""
        if (old_status or "active") == (new_status or "active"):
            return
        try:
            from ..applications.app_registry import regenerate_installed_apps_md
            regenerate_installed_apps_md(bot_id, _shared())
        except Exception as exc:
            _log.warning(
                "guidance resync after status change failed for %s: %s",
                bot_id, exc,
            )

    @app.put("/api/applications/<bot_id>/<app_id>")
    def update_application(bot_id: str, app_id: str) -> Response:
        """Save edited manifest. Writes atomically to bot workspace via sudo."""
        body = request.get_json()
        if not body:
            return jsonify({"error": "JSON body required"}), 400
        existing = _read_manifest_as_bot(bot_id, app_id) or {}
        if not existing:
            return jsonify({"error": "not found"}), 404
        old_status = existing.get("status") or "active"
        # Merge: body takes precedence; preserve immutable fields
        existing.update(body)
        existing["id"] = app_id
        existing["bot_id"] = bot_id
        existing["updated_at"] = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
        if _write_manifest_as_bot(bot_id, app_id, existing):
            _resync_guidance_on_status_change(
                bot_id, old_status, existing.get("status") or "active",
            )
            return jsonify({"ok": True})
        return jsonify({"error": "write failed"}), 500

    @app.route("/api/applications/<bot_id>/<app_id>", methods=["PATCH"])
    def patch_application(bot_id: str, app_id: str) -> Response:
        """Deep-merge a partial update into an existing manifest (e.g. lifecycle fields)."""
        existing = _read_manifest_as_bot(bot_id, app_id)
        if not existing:
            return jsonify({"error": "not found"}), 404
        body = request.get_json()
        if not body:
            return jsonify({"error": "JSON body required"}), 400
        old_status = existing.get("status") or "active"
        # Deep-merge top-level dict fields; scalars overwrite
        for k, v in body.items():
            if isinstance(v, dict) and isinstance(existing.get(k), dict):
                existing[k] = {**existing[k], **v}
            else:
                existing[k] = v
        existing["id"] = app_id
        existing["bot_id"] = bot_id
        existing["updated_at"] = _now_iso()
        if _write_manifest_as_bot(bot_id, app_id, existing):
            # Status flips through this route (e.g. → "deprecated") must
            # unsplice/re-splice guidance like the lifecycle routes do.
            _resync_guidance_on_status_change(
                bot_id, old_status, existing.get("status") or "active",
            )
            return jsonify({"ok": True})
        return jsonify({"error": "write failed"}), 500

    @app.delete("/api/applications/<bot_id>/<app_id>")
    def delete_application(bot_id: str, app_id: str) -> Response:
        """
        Remove an application manifest with layer-aware file cleanup.

        Body (JSON, optional):
            delete_files : list[str]  — relative paths the operator confirmed
                                        deleting (from deletion_candidates or
                                        preserved_files).
            commit       : bool       — force execute even with no files to
                                        delete (e.g. uninstalling an app whose
                                        only artifact is the manifest itself).

        Two-pass design:
          - First call from the UI sends ``delete_files: []`` and no commit
            flag → returns the preview breakdown without touching disk.
          - Second call sends ``delete_files: [...]`` → executes the
            uninstall using the manifest as the cleanup checklist, deleting
            the manifest *last* so a mid-flight failure leaves it on disk
            as resumable state.

        Returns:
            preview      : {ok, pkg_id, preserved_files, cleaned_files,
                            deletion_candidates}
            execute      : preview shape + {actually_deleted, failed_deletes,
                            manual_delete_required, triggers_unwired}
        """
        from ..applications.manifest import (
            plan_manifest_deletion,
            unwire_event_triggers,
            execute_scheduled_teardown,
            apply_manifest_marker_cleanup,
            finalize_manifest_deletion, mark_manifest_dormant,
            unlink_confirmed_files,
        )
        from ..config import get_bot_workspace

        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))

        body = request.get_json(silent=True) or {}
        confirmed_deletes: list[str] = list(body.get("delete_files") or [])
        commit_requested: bool = bool(body.get("commit"))
        keep_manifest: bool = bool(body.get("keep_manifest"))

        # Resolve bot workspace for marker cleanup
        try:
            ws = Path(get_bot_workspace(bot_id))
        except Exception:
            ws = None

        # ── Phase 1: plan (non-mutating) ──────────────────────────────────────
        plan = plan_manifest_deletion(
            application_id=app_id,
            bot_id=bot_id,
            shared_dir=shared_dir,
            workspace_path=ws,
        )
        if not plan.get("ok"):
            return jsonify(plan), 404 if "not found" in plan.get("error", "") else 500

        # Empty delete_files + no explicit commit = preview only.
        if not confirmed_deletes and not commit_requested:
            return jsonify(plan)

        # ── Phase 2: execute, manifest-LAST ───────────────────────────────────
        # 2.0. Unregister plugin-interceptor triggers BEFORE any file leaves
        # disk (base-spec §8.4 step 3). The plugin compiles event_triggers[]
        # straight from the bot's manifests/ dir; without this, the window
        # between file unlink and manifest deletion — indefinite when a
        # failed delete leaves the manifest as the resumable checklist —
        # has live triggers invoking deleted scripts. Best-effort: the
        # finalize step unregisters too, so a failed unwire is surfaced in
        # the response but doesn't block the uninstall.
        triggers_unwired = unwire_event_triggers(app_id, bot_id, shared_dir)

        # 2.0b. Tear down Phase-4.5 artifacts (launchd/systemd units,
        # python-signal wrappers, heartbeat/AGENTS managed sections) while
        # the scripts they invoke are still on disk. A failed removal
        # aborts BEFORE any file unlink — manifest + files stay as the
        # resumable checklist (audit S4).
        teardown = execute_scheduled_teardown(app_id, bot_id, shared_dir, plan=plan)
        if not teardown.get("ok"):
            return jsonify({
                **plan,
                "triggers_unwired": triggers_unwired,
                "teardown_results": teardown.get("results", []),
                "error": ("scheduled-unit teardown failed; nothing was deleted "
                          "— fix and re-run uninstall to resume"),
            }), 500

        # 2a. Strip pkg_id from sidecar markers (best-effort). identity: a marker names the OWNING package (an attribution namespace), not this app's id.
        if ws is not None:
            apply_manifest_marker_cleanup(
                application_id=app_id,
                bot_id=bot_id,
                shared_dir=shared_dir,
                workspace_path=ws,
                plan=plan,
            )

        # 2b. Unlink operator-confirmed files. The manifest is still on
        # disk during this loop, so an aborted run leaves a resumable
        # checklist behind instead of orphan files + a missing manifest.
        actually_deleted: list[str] = []
        failed_deletes: list[str] = []
        manual_deletes: list[dict] = []
        if ws is not None:
            actually_deleted, failed_deletes, manual_deletes = unlink_confirmed_files(
                ws, bot_id, confirmed_deletes, plan,
            )

        # 2c. If any file delete failed RETRYABLY, refuse to delete the
        # manifest — the operator can re-run uninstall to clean up the rest.
        # Helper-refused paths (manual_deletes) are excluded from the guard:
        # no re-run can ever delete them, so blocking on them would wedge
        # the checklist forever; finalize records them in the archive.
        if failed_deletes:
            return jsonify({
                **plan,
                "actually_deleted": actually_deleted,
                "failed_deletes": failed_deletes,
                "manual_delete_required": manual_deletes,
                "triggers_unwired": triggers_unwired,
                "error": (
                    f"{len(failed_deletes)} file(s) could not be deleted; "
                    "manifest left in place so uninstall can be resumed"
                ),
            }), 500

        # 2d. Finalize: keep_manifest (keep-data scope) marks the manifest
        # dormant so its preserved data files retain provenance; otherwise
        # archive + delete the manifest JSON. Both rebuild the file index.
        finalized = (
            mark_manifest_dormant(app_id, bot_id, shared_dir) if keep_manifest
            else finalize_manifest_deletion(app_id, bot_id, shared_dir, manual_deletes)
        )
        if not finalized.get("ok"):
            return jsonify({
                **plan,
                "actually_deleted": actually_deleted,
                "failed_deletes": failed_deletes,
                "manual_delete_required": manual_deletes,
                "triggers_unwired": triggers_unwired,
                "error": finalized.get("error", "manifest finalize failed"),
            }), 500

        return jsonify({
            **plan,
            "actually_deleted": actually_deleted,
            "failed_deletes": failed_deletes,
            "manual_delete_required": manual_deletes,
            "triggers_unwired": triggers_unwired,
            "teardown_results": teardown.get("results", []),
        })

    # ── Application lifecycle actions ──────────────────────────────────────────
    #
    # Pause    → status=paused,  crons disabled;  all files intact
    # Unpause  → status=active,  crons re-enabled; all files intact
    # Archive  → status=hidden,  crons disabled;  all files intact; hidden from default view
    # Restore  → status=active,  crons re-enabled; back in default view
    #
    # These use the existing PATCH mechanism (_write_manifest_as_bot) so that all
    # the same sudo /bin/cp write guards apply.
    #
    # The status flip also takes the app's Layer C event_triggers dark
    # (base-spec §8.4 steps 3/4): the plugin skips inactive-status
    # manifests at trigger-compile time (_manifestStatusAllowsTriggers in
    # TurnObserver.ts), and _write_manifest_as_bot's same-dir temp+rename
    # bumps the manifests-dir mtime so a running gateway re-compiles
    # within its ~5s rescan window. No destructive unwire needed —
    # pause/archive stay reversible.
    #
    # Guidance unsplice (base-spec §8.4 step 2, closed in manifest-v7
    # Slice 2): after the status write, the bot's INSTALLED_APPS.md and
    # the AGENTS.md installed-apps marker block are regenerated.
    # regenerate_installed_apps_md filters to _VISIBLE_STATUSES, so
    # pause/archive/deprecate drop the app's guidance entries and
    # unpause/restore re-splice them — symmetric and reversible by
    # construction. Best-effort: a regeneration failure never fails the
    # lifecycle action (the next forge/scanner regenerate self-corrects).

    def _app_lifecycle(bot_id: str, app_id: str, new_status: str, cron_action: str) -> "Response":
        """Shared logic for pause/unpause/archive/restore."""
        from ..applications.manifest import load_manifest
        from ..applications.cron_manager import disable_app_crons, enable_app_crons

        shared = _shared()
        manifest = load_manifest(app_id, bot_id, shared)
        if manifest is None:
            return jsonify({"error": "manifest not found"}), 404

        # Cron management (best-effort — non-fatal if jobs.json missing)
        cron_result: dict = {"ok": True}
        if cron_action == "disable":
            cron_result = disable_app_crons(bot_id, manifest)
        elif cron_action == "enable":
            cron_result = enable_app_crons(bot_id, manifest)

        # audit S4: Phase-4.5 launchd/systemd units keep firing through pause/
        # archive (OC crons above are only one surface); helper is defensive.
        from ..applications.install_helpers import set_app_scheduled_units
        sched_result = set_app_scheduled_units(manifest, bot_id, enable=(cron_action == "enable"))

        # Status update via existing PATCH path
        existing = _read_manifest_as_bot(bot_id, app_id)
        if existing is None:
            return jsonify({"error": "manifest not found"}), 404
        existing["status"] = new_status
        existing["updated_at"] = _now_iso()
        if not _write_manifest_as_bot(bot_id, app_id, existing):
            return jsonify({"error": "write failed"}), 500

        # Unsplice / re-splice guidance to match the new status (§8.4
        # step 2 symmetry — see the comment block above).
        guidance_resynced = False
        try:
            from ..applications.app_registry import regenerate_installed_apps_md
            guidance_resynced = regenerate_installed_apps_md(bot_id, shared) is not None
        except Exception as exc:
            _log.warning(
                "guidance resync after %s failed for %s/%s: %s",
                new_status, bot_id, app_id, exc,
            )

        return jsonify({
            "ok": True,
            "status": new_status,
            "crons": cron_result,
            "scheduled_units": sched_result,
            "guidance_resynced": guidance_resynced,
        })

    @app.get("/api/applications/<bot_id>/<app_id>/adopt-preview")
    def api_application_adopt_preview(bot_id: str, app_id: str) -> Response:
        """Preview the diff between an Instance's pinned Spec version and
        a target (or the latest in gallery if ?target_version omitted).

        Returns the SpecDiff classification + a flag the UI uses to enable
        or disable the Adopt button. No state change.

        Query params:
            target_version: optional. Defaults to latest in gallery.

        Returns:
            200 {ok, from_version, to_version, safe_to_adopt, spec_diff}
            400 {error} — bad target_version, no Spec found
            404 {error} — instance missing
        """
        from dataclasses import asdict
        from ..applications.adopt import (
            adopt_with_specs, load_spec_version,
        )
        from ..applications.spec_drift import _latest_spec_version

        manifest = _read_manifest_as_bot(bot_id, app_id)
        if manifest is None:
            return jsonify({"error": "not found"}), 404
        if manifest.get("manifest_shape") != "v7-arc":
            return jsonify({"error": "adopt requires v7-arc Instance"}), 400

        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))

        provenance = manifest.get("provenance") or {}
        spec_id = provenance.get("spec_id")  # identity: see resolve_app_id — the Instance's BINDING to a Spec; builds gallery/<tier>/<spec_id>/<spec_version>.json and is legitimately null while the Instance still has an id.
        current_version = provenance.get("spec_version")
        if not spec_id or not current_version:
            return jsonify({"error": "instance missing provenance"}), 400

        target_version = request.args.get("target_version", "").strip()
        if not target_version:
            target_version = _latest_spec_version(spec_id, shared_dir)
            if not target_version:
                return jsonify({"error": f"no Spec in gallery for {spec_id}"}), 400

        current_spec = load_spec_version(shared_dir, spec_id, current_version)
        target_spec = load_spec_version(shared_dir, spec_id, target_version)
        if current_spec is None:
            return jsonify({
                "error": f"current Spec {spec_id}@{current_version} not in gallery"
            }), 400
        if target_spec is None:
            return jsonify({
                "error": f"target Spec {spec_id}@{target_version} not in gallery"
            }), 400

        try:
            plan = adopt_with_specs(
                manifest, current_spec, target_spec, target_version,
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        return jsonify({
            "ok": True,
            "from_version": plan.from_version,
            "to_version": plan.to_version,
            "safe_to_adopt": plan.safe_to_adopt,
            "spec_diff": asdict(plan.spec_diff),
        })

    @app.post("/api/applications/<bot_id>/<app_id>/adopt")
    def api_application_adopt(bot_id: str, app_id: str) -> Response:
        """Perform Adopt v1 — pointer-only rebind to a newer Spec version.

        Body (optional):
            target_version: str — Spec version to adopt. Default: latest.
            reason: str — recorded in spec_version_history. Default: manual_adopt.

        Refuses (400) when the Spec diff is structural — the operator must
        use the gallery install flow to re-build via Forge for those.

        Returns:
            200 {ok, from_version, to_version, spec_diff, instance}
            400 {error} — bad input or structural diff
            404 {error} — instance not found
            500 {error} — write failed
        """
        from dataclasses import asdict
        from ..applications.adopt import (
            adopt_with_specs, load_spec_version,
        )
        from ..applications.spec_drift import _latest_spec_version

        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be a JSON object or absent"}), 400

        manifest = _read_manifest_as_bot(bot_id, app_id)
        if manifest is None:
            return jsonify({"error": "not found"}), 404
        if manifest.get("manifest_shape") != "v7-arc":
            return jsonify({"error": "adopt requires v7-arc Instance"}), 400

        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))

        provenance = manifest.get("provenance") or {}
        spec_id = provenance.get("spec_id")  # identity: see resolve_app_id — the Instance's BINDING to a Spec; builds gallery/<tier>/<spec_id>/<spec_version>.json and is legitimately null while the Instance still has an id.
        current_version = provenance.get("spec_version")
        if not spec_id or not current_version:
            return jsonify({"error": "instance missing provenance"}), 400

        target_version = (body.get("target_version") or "").strip()
        if not target_version:
            target_version = _latest_spec_version(spec_id, shared_dir)
            if not target_version:
                return jsonify({"error": f"no Spec in gallery for {spec_id}"}), 400
        reason = (body.get("reason") or "manual_adopt").strip() or "manual_adopt"

        # No-op short-circuit: already at target.
        if target_version == current_version:
            return jsonify({
                "ok": True,
                "from_version": current_version,
                "to_version": target_version,
                "noop": True,
                "message": "Instance already at target version",
            })

        current_spec = load_spec_version(shared_dir, spec_id, current_version)
        target_spec = load_spec_version(shared_dir, spec_id, target_version)
        if current_spec is None or target_spec is None:
            return jsonify({"error": "Spec not found in gallery"}), 400

        try:
            plan = adopt_with_specs(
                manifest, current_spec, target_spec, target_version,
                reason=reason,
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        if not plan.safe_to_adopt:
            return jsonify({
                "error": (
                    "Adopt v1 only handles presentation-only changes. The "
                    "target Spec has structural changes "
                    f"({', '.join(plan.spec_diff.structural_fields_touched)}) "
                    "that need a Forge rebuild — use the gallery install flow "
                    "to re-install this app at the new version."
                ),
                "spec_diff": asdict(plan.spec_diff),
                "from_version": plan.from_version,
                "to_version": plan.to_version,
            }), 400

        if not _write_manifest_as_bot(bot_id, app_id, plan.new_instance):
            return jsonify({"error": "Instance write failed"}), 500

        return jsonify({
            "ok": True,
            "from_version": plan.from_version,
            "to_version": plan.to_version,
            "spec_diff": asdict(plan.spec_diff),
            "instance": plan.new_instance,
        })

    @app.patch("/api/applications/<bot_id>/<app_id>/spec-privacy")
    def api_application_spec_privacy_patch(bot_id: str, app_id: str) -> Response:
        """Flip the bound Spec's ``privacy.shareable_in_lessons`` flag.

        Looks up the Spec via the Instance's ``provenance.spec_id`` +
        ``spec_version``. Refuses if the Spec lives in ``gallery/builtin``
        or ``gallery/imported`` — those tiers are upstream-owned and the
        operator would need to fork before editing. Local-tier Specs
        (the migration's output, or anything operator-authored / shared
        within this pod) are mutable in place.

        Body:
            {"shareable_in_lessons": <bool>}

        Returns:
            200 {ok, shareable_in_lessons, spec_id, spec_version, spec_tier}
            400 {error} — bad input / non-v7-arc / non-local Spec tier
            404 {error} — instance not found / Spec not found
            500 {error} — write failure
        """
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        new_value = body.get("shareable_in_lessons")
        if not isinstance(new_value, bool):
            return jsonify({"error": "shareable_in_lessons must be boolean"}), 400

        manifest = _read_manifest_as_bot(bot_id, app_id)
        if manifest is None:
            return jsonify({"error": "not found"}), 404
        if manifest.get("manifest_shape") != "v7-arc":
            return jsonify({"error": "spec-privacy edit requires v7-arc Instance"}), 400

        provenance = manifest.get("provenance") or {}
        spec_id = provenance.get("spec_id")  # identity: see resolve_app_id — the Instance's BINDING to a Spec; builds gallery/<tier>/<spec_id>/<spec_version>.json and is legitimately null while the Instance still has an id.
        spec_version = provenance.get("spec_version")
        if not spec_id or not spec_version:
            return jsonify({"error": "instance missing provenance.spec_id or spec_version"}), 400

        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))

        # Only local-tier Specs are editable. builtin and imported are
        # upstream-owned — operator would need to fork (re-share locally)
        # before flipping any flag on them.
        local_path = shared_dir / "gallery" / "local" / spec_id / f"{spec_version}.json"
        if not local_path.is_file():
            # Check whether the Spec exists in another tier so the error
            # tells the operator what's wrong.
            builtin_path = shared_dir / "gallery" / "builtin" / spec_id / f"{spec_version}.json"
            if builtin_path.is_file():
                return jsonify({
                    "error": (
                        f"Spec {spec_id}@{spec_version} is in gallery/builtin "
                        "(upstream-owned). Re-share this app from this bot to "
                        "fork the Spec into gallery/local before editing privacy."
                    ),
                    "spec_tier": "builtin",
                }), 400
            return jsonify({
                "error": f"Spec {spec_id}@{spec_version} not in gallery/local",
            }), 404

        try:
            spec = json.loads(local_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return jsonify({"error": f"failed to read Spec: {exc}"}), 500

        privacy = spec.get("privacy")
        if not isinstance(privacy, dict):
            privacy = {}
        privacy["shareable_in_lessons"] = new_value
        spec["privacy"] = privacy

        # Atomic write. The shared_dir is evolve-owned so a plain Path
        # rewrite works — no /tmp + sudo needed.
        try:
            tmp = local_path.with_suffix(local_path.suffix + ".tmp")
            tmp.write_text(json.dumps(spec, indent=2, sort_keys=False))
            tmp.replace(local_path)
        except OSError as exc:
            return jsonify({"error": f"Spec write failed: {exc}"}), 500

        return jsonify({
            "ok": True,
            "shareable_in_lessons": new_value,
            "spec_id": spec_id,
            "spec_version": spec_version,
            "spec_tier": "local",
        })

    # POST /api/applications/<bot>/<app>/test removed 2026-06-08 — app-test
    # surface killed per docs/decision-app-tests-2026-06-08.md.

    @app.post("/api/applications/<bot_id>/<app_id>/audit")
    def request_application_audit(bot_id: str, app_id: str) -> "Response":
        """Queue a Tier-3 audit for this app on the bot and kick the runner.

        Body (optional JSON):
          - full_audit: bool — re-evaluate accepted findings
          - all_apps: bool — audit every eligible app on this bot (overrides app_id)

        Returns the request_id so the UI can poll the bot's outbox for
        completion. Doesn't wait for the audit to finish.
        """
        from ..applications.audit_dispatch import request_audit
        from ..config import get_bot_user

        body = request.get_json(silent=True) or {}
        full_audit = bool(body.get("full_audit", False))
        all_apps = bool(body.get("all_apps", False))

        net = load_network(network_path)
        bot_user = get_bot_user(bot_id, net)
        if not bot_user:
            return jsonify({"ok": False, "error": f"unknown bot: {bot_id}"}), 404

        apps_arg = None if all_apps else [app_id]
        result = request_audit(
            bot_id=bot_id, bot_user=bot_user,
            apps=apps_arg, full_audit=full_audit,
            requested_by="ui:operator",
            kick=True,
        )
        if not result.ok:
            return jsonify({"ok": False, "error": result.error}), 500
        return jsonify({
            "ok": True,
            "request_id": result.request_id,
            "kicked": result.kicked,
            "full_audit": full_audit,
            "apps": apps_arg or "all",
        })

    @app.post("/api/applications/<bot_id>/<app_id>/audit/accept")
    def accept_audit_finding(bot_id: str, app_id: str) -> "Response":
        """Mark an audit finding as accepted so future audits don't re-raise it.

        Body (required JSON):
          - signature: str — the finding's signature from the Proposal payload
          - rationale: str (optional) — why this is being accepted
        """
        from ..applications.audit_dispatch import mark_finding_accepted
        from ..config import get_bot_user

        body = request.get_json(silent=True) or {}
        signature = (body.get("signature") or "").strip()
        if not signature:
            return jsonify({"ok": False, "error": "signature required"}), 400
        rationale = body.get("rationale") or ""

        net = load_network(network_path)
        bot_user = get_bot_user(bot_id, net)
        if not bot_user:
            return jsonify({"ok": False, "error": f"unknown bot: {bot_id}"}), 404

        ok, err = mark_finding_accepted(
            bot_id=bot_id, bot_user=bot_user, app_id=app_id,
            signature=signature,
            accepted_by="ui:operator",
            rationale=rationale,
        )
        if not ok:
            return jsonify({"ok": False, "error": err}), 500
        return jsonify({"ok": True})

    @app.post("/api/applications/<bot_id>/<app_id>/audit/unaccept")
    def unaccept_audit_finding(bot_id: str, app_id: str) -> "Response":
        """Remove a signature from manifest.audit_accepted[]."""
        from ..applications.audit_dispatch import unaccept_finding
        from ..config import get_bot_user

        body = request.get_json(silent=True) or {}
        signature = (body.get("signature") or "").strip()
        if not signature:
            return jsonify({"ok": False, "error": "signature required"}), 400

        net = load_network(network_path)
        bot_user = get_bot_user(bot_id, net)
        if not bot_user:
            return jsonify({"ok": False, "error": f"unknown bot: {bot_id}"}), 404

        ok, err = unaccept_finding(
            bot_id=bot_id, bot_user=bot_user, app_id=app_id, signature=signature,
        )
        if not ok:
            return jsonify({"ok": False, "error": err}), 500
        return jsonify({"ok": True})

    @app.get("/api/applications/<bot_id>/<app_id>/audit/trail")
    def get_audit_trail(bot_id: str, app_id: str) -> "Response":
        """Return the last N entries from the bot's per-app trail.jsonl."""
        import subprocess
        from ..config import get_bot_user

        net = load_network(network_path)
        bot_user = get_bot_user(bot_id, net)
        if not bot_user:
            return jsonify({"entries": [], "error": f"unknown bot: {bot_id}"}), 404
        trail_path = Path(
            f"/Users/{bot_user}/.openclaw/workspace/evolve/audits/{app_id}/trail.jsonl"
        )
        limit = int(request.args.get("limit", 100))
        text: str | None = None
        try:
            text = trail_path.read_text()
        except PermissionError:
            try:
                r = subprocess.run(
                    ["sudo", "/bin/cat", str(trail_path)],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    text = r.stdout
            except subprocess.SubprocessError:
                pass
        except (OSError, FileNotFoundError):
            return jsonify({"entries": [], "note": "no audit trail yet"})

        if text is None:
            return jsonify({"entries": [], "note": "trail not readable"})

        entries: list[dict] = []
        for line in text.splitlines()[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return jsonify({"entries": entries})

    # ── Substrate audit endpoints (Workstream B-skills) ──────────────────
    #
    # Parallel to the app-audit endpoints above. Each (element_type, element)
    # gets the same surface area: queue audit, accept finding, read trail.

    @app.post("/api/<element_type>s/<bot_id>/<element_id>/audit")
    def request_substrate_audit_endpoint(
        element_type: str, bot_id: str, element_id: str,
    ) -> "Response":
        """Queue a substrate (skill or provider) audit and kick the runner.

        URL: ``/api/skills/<bot>/<skill>/audit`` or
             ``/api/providers/<bot>/<provider>/audit``.

        Body (optional JSON):
          - full_audit: bool — re-evaluate accepted findings
          - all_elements: bool — audit every element of this type on this bot

        Returns immediately with a request_id; the bot's audit_runner
        executes the audit asynchronously. UI polls the outbox or watches
        for the Proposal landing in pending/.
        """
        if element_type not in ("skill", "provider"):
            return jsonify({"ok": False, "error": "unknown element_type"}), 400

        from ..applications.audit_dispatch import request_substrate_audit
        from ..config import get_bot_user

        body = request.get_json(silent=True) or {}
        full_audit = bool(body.get("full_audit", False))
        all_elements = bool(body.get("all_elements", False))

        net = load_network(network_path)
        if bot_id not in ((net or {}).get("bots") or {}):
            return jsonify({"ok": False, "error": f"unknown bot: {bot_id}"}), 404
        bot_user = get_bot_user(bot_id, net)

        elements = None if all_elements else [element_id]
        result = request_substrate_audit(
            bot_id=bot_id, bot_user=bot_user,
            element_type=element_type,
            elements=elements,
            full_audit=full_audit,
            requested_by="ui:operator",
            kick=True,
        )
        if not result.ok:
            return jsonify({"ok": False, "error": result.error}), 500
        return jsonify({
            "ok": True,
            "request_id": result.request_id,
            "kicked": result.kicked,
            "full_audit": full_audit,
            "elements": elements or "all",
        })

    @app.post("/api/<element_type>s/<bot_id>/<element_id>/audit/accept")
    def accept_substrate_finding(
        element_type: str, bot_id: str, element_id: str,
    ) -> "Response":
        """Mark a substrate-audit finding accepted; future audits skip it."""
        if element_type not in ("skill", "provider"):
            return jsonify({"ok": False, "error": "unknown element_type"}), 400

        from ..applications.audit_dispatch import mark_substrate_finding_accepted
        from ..config import get_bot_user

        body = request.get_json(silent=True) or {}
        signature = (body.get("signature") or "").strip()
        if not signature:
            return jsonify({"ok": False, "error": "signature required"}), 400
        rationale = body.get("rationale") or ""

        net = load_network(network_path)
        if bot_id not in ((net or {}).get("bots") or {}):
            return jsonify({"ok": False, "error": f"unknown bot: {bot_id}"}), 404
        bot_user = get_bot_user(bot_id, net)

        ok, err = mark_substrate_finding_accepted(
            bot_id=bot_id, bot_user=bot_user,
            element_type=element_type, element_id=element_id,
            signature=signature,
            accepted_by="ui:operator",
            rationale=rationale,
        )
        if not ok:
            return jsonify({"ok": False, "error": err}), 500
        return jsonify({"ok": True})

    @app.get("/api/<element_type>s/<bot_id>/<element_id>/audit/trail")
    def get_substrate_audit_trail(
        element_type: str, bot_id: str, element_id: str,
    ) -> "Response":
        """Last N trail entries for a skill or provider audit.

        Reuses the same trail-viewer modal in the UI — the JS branches on
        element_type to construct the URL. Same response shape as the
        app-audit trail endpoint: ``{"entries": [...]}``.
        """
        if element_type not in ("skill", "provider"):
            return jsonify({"entries": [], "error": "unknown element_type"}), 400

        import subprocess
        from ..config import get_bot_user

        net = load_network(network_path)
        if bot_id not in ((net or {}).get("bots") or {}):
            return jsonify({"entries": [], "error": f"unknown bot: {bot_id}"}), 404
        bot_user = get_bot_user(bot_id, net)

        parent = "skill_audits" if element_type == "skill" else "provider_audits"
        trail_path = Path(
            f"/Users/{bot_user}/.openclaw/workspace/evolve/{parent}/"
            f"{element_id}/trail.jsonl"
        )
        limit = int(request.args.get("limit", 100))
        text: str | None = None
        try:
            text = trail_path.read_text()
        except PermissionError:
            try:
                r = subprocess.run(
                    ["sudo", "/bin/cat", str(trail_path)],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    text = r.stdout
            except subprocess.SubprocessError:
                pass
        except (OSError, FileNotFoundError):
            return jsonify({"entries": [], "note": "no audit trail yet"})

        if text is None:
            return jsonify({"entries": [], "note": "trail not readable"})

        entries: list[dict] = []
        for line in text.splitlines()[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return jsonify({"entries": entries, "element_type": element_type})

    # ── Substrate audit aggregated status (chip data) ────────────────────
    #
    # Powers the per-row audit pill on the Skills and OAuth provider
    # pages. Aggregates the most-recent ``audit_run`` entry from each
    # element's trail.jsonl into a compact map the UI can render
    # cheaply without N round-trips.

    @app.get("/api/<element_type>s/<bot_id>/audit-status")
    def get_substrate_audit_status(
        element_type: str, bot_id: str,
    ) -> "Response":
        """Aggregated audit status for every element of this type on a bot.

        Returns a map of element_id -> {status, last_verified, findings_count,
        raised_count}. ``status`` is one of:
          - "never"    - no audit_run trail entry yet
          - "healthy"  - last run was status=ok with zero findings raised
          - "findings" - last run produced one or more raised findings
          - "failed"   - last run errored (status=failed)
        """
        if element_type not in ("skill", "provider"):
            return jsonify({"ok": False, "error": "unknown element_type"}), 400

        import subprocess
        from ..config import get_bot_user

        net = load_network(network_path)
        if bot_id not in ((net or {}).get("bots") or {}):
            return jsonify({"ok": False, "error": f"unknown bot: {bot_id}"}), 404
        bot_user = get_bot_user(bot_id, net)

        parent = "skill_audits" if element_type == "skill" else "provider_audits"
        root = Path(
            f"/Users/{bot_user}/.openclaw/workspace/evolve/{parent}"
        )

        elements: dict[str, dict] = {}
        element_dirs: list[Path] = []
        try:
            if root.exists():
                element_dirs = [p for p in root.iterdir() if p.is_dir()]
        except (PermissionError, OSError):
            element_dirs = []

        # Fallback: sudo ls when direct read is blocked.
        if not element_dirs:
            try:
                r = subprocess.run(
                    ["sudo", "/bin/ls", str(root)],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    for line in r.stdout.splitlines():
                        line = line.strip()
                        if line:
                            element_dirs.append(root / line)
            except subprocess.SubprocessError:
                pass

        for elem_dir in element_dirs:
            elem_id = elem_dir.name
            trail_path = elem_dir / "trail.jsonl"
            text_t: str | None = None
            try:
                text_t = trail_path.read_text()
            except PermissionError:
                try:
                    r = subprocess.run(
                        ["sudo", "/bin/cat", str(trail_path)],
                        capture_output=True, text=True, timeout=5,
                    )
                    if r.returncode == 0:
                        text_t = r.stdout
                except subprocess.SubprocessError:
                    pass
            except (OSError, FileNotFoundError):
                pass
            if not text_t:
                elements[elem_id] = {
                    "status": "never",
                    "findings_count": 0,
                    "raised_count": 0,
                    "last_verified": None,
                }
                continue

            # Walk lines in reverse to find the latest audit_run summary.
            latest_run: dict | None = None
            for line in reversed(text_t.splitlines()):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("kind") == "audit_run":
                    latest_run = entry
                    break

            if latest_run is None:
                elements[elem_id] = {
                    "status": "never",
                    "findings_count": 0,
                    "raised_count": 0,
                    "last_verified": None,
                }
                continue

            outcomes = latest_run.get("outcomes") or {}
            raised = int(outcomes.get("propose", 0)) + int(
                outcomes.get("conflict_notice", 0)
            )
            findings_count = int(latest_run.get("findings_count") or 0)
            run_status = latest_run.get("status") or "ok"
            if run_status == "failed":
                status = "failed"
            elif raised > 0:
                status = "findings"
            else:
                status = "healthy"
            elements[elem_id] = {
                "status": status,
                "findings_count": findings_count,
                "raised_count": raised,
                "last_verified": latest_run.get("ts"),
                "error": latest_run.get("error"),
            }

        return jsonify({
            "ok": True,
            "elements": elements,
            "element_type": element_type,
        })

    # ── Substrate audit cadence (per-bot override) ───────────────────────
    #
    # Reads / writes network.json -> {skill,provider}_audit.bot_cadence[bot_id].
    # Used by the cadence dropdown on the Skills + OAuth provider pages.
    # Per-element cadence isn't supported in v1 (substrate elements don't
    # carry manifests yet - Option B of spec section 4.4); this is a per-bot
    # override that applies to every element of the chosen type on
    # that bot. Empty / "inherit" deletes the override.

    @app.get("/api/<element_type>s/<bot_id>/audit-cadence")
    def get_substrate_audit_cadence_endpoint(
        element_type: str, bot_id: str,
    ) -> "Response":
        if element_type not in ("skill", "provider"):
            return jsonify({"ok": False, "error": "unknown element_type"}), 400
        net = load_network(network_path)
        if bot_id not in ((net or {}).get("bots") or {}):
            return jsonify({"ok": False, "error": f"unknown bot: {bot_id}"}), 404
        cfg_key = "skill_audit" if element_type == "skill" else "provider_audit"
        cfg = (net.get(cfg_key) or {})
        pod_default = cfg.get("default_cadence", "weekly")
        per_bot = (cfg.get("bot_cadence") or {}).get(bot_id)
        return jsonify({
            "ok": True,
            "pod_default": pod_default,
            "bot_override": per_bot,
            "effective": per_bot if per_bot else pod_default,
        })

    @app.put("/api/<element_type>s/<bot_id>/audit-cadence")
    def set_substrate_audit_cadence_endpoint(
        element_type: str, bot_id: str,
    ) -> "Response":
        if element_type not in ("skill", "provider"):
            return jsonify({"ok": False, "error": "unknown element_type"}), 400
        body = request.get_json(silent=True) or {}
        cadence = (body.get("cadence") or "").strip()
        valid = ("", "inherit", "never", "quarterly", "monthly", "weekly", "daily")
        if cadence not in valid:
            return jsonify({
                "ok": False,
                "error": f"cadence must be one of {list(valid)}",
            }), 400

        net = load_network(network_path)
        if bot_id not in ((net or {}).get("bots") or {}):
            return jsonify({"ok": False, "error": f"unknown bot: {bot_id}"}), 404
        cfg_key = "skill_audit" if element_type == "skill" else "provider_audit"
        cfg = dict(net.get(cfg_key) or {})
        bot_cadence = dict(cfg.get("bot_cadence") or {})

        if cadence in ("", "inherit"):
            bot_cadence.pop(bot_id, None)
        else:
            bot_cadence[bot_id] = cadence
        cfg["bot_cadence"] = bot_cadence
        net[cfg_key] = cfg

        try:
            save_network(net, network_path)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        _audit_log_entry(
            f"{cfg_key}.cadence",
            "admin",
            {"bot_id": bot_id, "cadence": cadence or "(inherit)"},
        )
        return jsonify({"ok": True, "bot_override": bot_cadence.get(bot_id)})

    @app.post("/api/applications/<bot_id>/<app_id>/pause")
    def pause_application(bot_id: str, app_id: str) -> "Response":
        """Disable crons and mark app as paused.  Reversible with /unpause."""
        return _app_lifecycle(bot_id, app_id, "paused", "disable")

    @app.post("/api/applications/<bot_id>/<app_id>/unpause")
    def unpause_application(bot_id: str, app_id: str) -> "Response":
        """Re-enable crons and mark app as active."""
        return _app_lifecycle(bot_id, app_id, "active", "enable")

    @app.post("/api/applications/<bot_id>/<app_id>/archive")
    def archive_application(bot_id: str, app_id: str) -> "Response":
        """Disable crons and hide app from default Applications view.  Reversible."""
        return _app_lifecycle(bot_id, app_id, "hidden", "disable")

    @app.post("/api/applications/<bot_id>/<app_id>/restore")
    def restore_application(bot_id: str, app_id: str) -> "Response":
        """Restore a hidden or dormant app to active — re-enables crons."""
        return _app_lifecycle(bot_id, app_id, "active", "enable")

    # ── Coherence + reconciliation actions (spec §10–§11) ─────────────
    #
    # These power the Apps-page Coherence + Drift section. Each route is
    # a thin wrapper: read the manifest, hand to a pure helper in
    # ``applications.coherence_actions``, write the manifest back.

    def _read_or_404(bot_id: str, app_id: str):
        manifest = _read_manifest_as_bot(bot_id, app_id)
        if manifest is None:
            return None, (jsonify({"ok": False,
                                    "error": "manifest not found"}), 404)
        return manifest, None

    @app.post("/api/applications/<bot_id>/<app_id>/approve")
    def approve_application_changes(bot_id: str, app_id: str) -> "Response":
        """Clear reconciliation drift on this app.

        Spec §10.4 (Approve). The Apps-page modal posts here when the
        operator accepts the current observational state.
        """
        from ..applications.coherence_actions import approve_changes
        manifest, err = _read_or_404(bot_id, app_id)
        if err is not None:
            return err
        result = approve_changes(manifest, by="ui:operator")
        if not _write_manifest_as_bot(bot_id, app_id, manifest):
            return jsonify({"ok": False, "error": "write failed"}), 500
        return jsonify({"ok": True, **result})

    @app.post("/api/applications/<bot_id>/<app_id>/promote")
    def promote_application(bot_id: str, app_id: str) -> "Response":
        """Flip every observational provenance entry to ``bot_authored``.

        Spec §4.3.
        """
        from ..applications.coherence_actions import promote_to_authored
        manifest, err = _read_or_404(bot_id, app_id)
        if err is not None:
            return err
        result = promote_to_authored(manifest, by="ui:operator")
        if not _write_manifest_as_bot(bot_id, app_id, manifest):
            return jsonify({"ok": False, "error": "write failed"}), 500
        return jsonify({"ok": True, **result})

    @app.post("/api/applications/<bot_id>/<app_id>/flag")
    def flag_application(bot_id: str, app_id: str) -> "Response":
        """Record an operator-authored flag on this app's manifest.

        Body: ``{"description": "free text"}``. A follow-up wires the
        flag into an arbiter Proposal; today it surfaces in the modal
        + the bot's session-start Apps block.
        """
        from ..applications.coherence_actions import flag_for_operator
        body = request.get_json(silent=True) or {}
        description = (body.get("description") or "").strip()
        if not description:
            return jsonify({"ok": False,
                            "error": "description required"}), 400
        manifest, err = _read_or_404(bot_id, app_id)
        if err is not None:
            return err
        try:
            result = flag_for_operator(
                manifest, description=description, by="ui:operator",
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not _write_manifest_as_bot(bot_id, app_id, manifest):
            return jsonify({"ok": False, "error": "write failed"}), 500
        return jsonify({"ok": True, **result})

    @app.post("/api/applications/<bot_id>/<app_id>/coherence/mute")
    def mute_coherence_finding(bot_id: str, app_id: str) -> "Response":
        """Append a signature to manifest.coherence.coherence_accepted[]."""
        from ..applications.coherence_actions import mute_finding
        body = request.get_json(silent=True) or {}
        signature = (body.get("signature") or "").strip()
        rationale = body.get("rationale") or ""
        if not signature:
            return jsonify({"ok": False,
                            "error": "signature required"}), 400
        manifest, err = _read_or_404(bot_id, app_id)
        if err is not None:
            return err
        try:
            result = mute_finding(
                manifest, signature=signature, rationale=rationale,
                by="ui:operator",
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not _write_manifest_as_bot(bot_id, app_id, manifest):
            return jsonify({"ok": False, "error": "write failed"}), 500
        return jsonify({"ok": True, **result})

    @app.post("/api/applications/<bot_id>/<app_id>/coherence/unmute")
    def unmute_coherence_finding(bot_id: str, app_id: str) -> "Response":
        """Remove a previously-muted signature. Idempotent."""
        from ..applications.coherence_actions import unmute_finding
        body = request.get_json(silent=True) or {}
        signature = (body.get("signature") or "").strip()
        if not signature:
            return jsonify({"ok": False,
                            "error": "signature required"}), 400
        manifest, err = _read_or_404(bot_id, app_id)
        if err is not None:
            return err
        try:
            result = unmute_finding(manifest, signature=signature)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not _write_manifest_as_bot(bot_id, app_id, manifest):
            return jsonify({"ok": False, "error": "write failed"}), 500
        return jsonify({"ok": True, **result})

    @app.post("/api/applications/<bot_id>/<app_id>/coherence/snooze")
    def snooze_coherence_finding(bot_id: str, app_id: str) -> "Response":
        """Snooze a single finding until ``until_iso``.

        Body: ``{"signature": "...", "until_iso": "2026-06-13T00:00:00Z"}``.
        If the body omits ``until_iso`` the server defaults to 7 days
        from now (the "Snooze for 7 days" UI affordance).
        """
        from ..applications.coherence_actions import snooze_finding
        body = request.get_json(silent=True) or {}
        signature = (body.get("signature") or "").strip()
        until_iso = (body.get("until_iso") or "").strip()
        if not signature:
            return jsonify({"ok": False,
                            "error": "signature required"}), 400
        if not until_iso:
            until_iso = _time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                _time.gmtime(_time.time() + 7 * 86400),
            )
        manifest, err = _read_or_404(bot_id, app_id)
        if err is not None:
            return err
        try:
            result = snooze_finding(
                manifest, signature=signature, until_iso=until_iso,
                by="ui:operator",
            )
        except (ValueError, KeyError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not _write_manifest_as_bot(bot_id, app_id, manifest):
            return jsonify({"ok": False, "error": "write failed"}), 500
        return jsonify({"ok": True, **result})

    @app.get("/api/applications/<bot_id>/<app_id>/pre-deploy-verdict")
    def get_pre_deploy_verdict(bot_id: str, app_id: str) -> "Response":
        """Run Pass A live against the current manifest and return the
        ``GateVerdict``. Powers the Override Pre-deploy Gate modal —
        the override_key the operator types must match this verdict's
        key for the override to take effect.
        """
        from ..applications.pre_deploy_gate import manifest_editor_gate
        manifest, err = _read_or_404(bot_id, app_id)
        if err is not None:
            return err
        verdict = manifest_editor_gate(manifest)
        return jsonify({"ok": True, **verdict.to_dict()})

    @app.get("/api/applications/<bot_id>/<app_id>/dependents")
    def get_application_dependents(bot_id: str, app_id: str) -> "Response":
        """
        Return active apps on this bot that declare this app as an app_dependency.

        Used by the uninstall wizard to warn before removing an app that others rely on.
        """
        from ..applications.manifest import load_manifest, get_app_dependents

        shared = _shared()
        manifest = load_manifest(app_id, bot_id, shared)
        pkg_id = manifest.pkg_id if manifest else None  # identity: see resolve_app_id — the dependency graph is keyed on the gallery package (get_app_dependents matches app_dependencies[].pkg_id).
        if not pkg_id:
            return jsonify({
                "dependents": [],
                "note": "App has no pkg_id — dependency tracking not available",
            })
        deps = get_app_dependents(pkg_id, bot_id, shared)
        return jsonify({"dependents": deps, "pkg_id": pkg_id})

    @app.get("/api/bots/<bot_id>/file-index")
    def api_file_index(bot_id: str) -> Response:
        """
        Return the file index for a bot, optionally filtered by pkg_id.

        Query params:
            pkg_id : str  — if provided, only return entries where owned_by or
                            shared_with matches this pkg_id
            rebuild: 1    — rebuild the index before returning (expensive; use sparingly)

        Response:
            {bot_id, index: {file_id → record}, total, built_at}
        """
        from ..applications.file_index import (
            load_file_index, rebuild_file_index, files_on_bot
        )
        from ..applications.ids import now_iso

        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        pkg_id_filter = request.args.get("pkg_id", "").strip()  # identity: see resolve_app_id — a QUERY PARAM matched against the file index's owned_by/shared_with (package attribution namespaces), not a manifest read.
        do_rebuild = request.args.get("rebuild", "") == "1"

        try:
            if do_rebuild:
                bot_ids = list(network.get("bots", {}).keys())
                index = rebuild_file_index(shared_dir, bot_ids)
            else:
                index = load_file_index(shared_dir)

            # Filter to this bot first
            bot_entries = {
                fid: rec for fid, rec in index.items()
                if rec.get("bot_id") == bot_id
            }

            # Further filter by pkg_id if requested
            if pkg_id_filter:
                bot_entries = {
                    fid: rec for fid, rec in bot_entries.items()
                    if rec.get("owned_by") == pkg_id_filter
                    or pkg_id_filter in (rec.get("shared_with") or [])
                }

            from ..applications.ids import now_iso as _now_iso
            return jsonify({
                "bot_id": bot_id,
                "pkg_id": pkg_id_filter or None,
                "index": bot_entries,
                "total": len(bot_entries),
                "built_at": _now_iso(),
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.post("/api/bots/<bot_id>/reflect/apply-fix")
    def api_reflect_apply_fix(bot_id: str) -> Response:
        """Apply a single Reflect finding's proposed_action to disk.

        v1 handles two auto-fixable kinds:
          - ``stamp_marker``       — file in realized_files but no marker on disk
          - ``rewrite_marker_to_spec`` — file has v6 ``pkg=`` marker, rewrite as ``spec=``

        ``attach_to_instance_or_archive`` is intentionally NOT supported here —
        that's an operator decision (attach to which Instance? archive the file?)
        and shouldn't be a one-click action.

        Body:
            {
              "kind": "stamp_marker" | "rewrite_marker_to_spec",
              "file_path": "<absolute path inside bot workspace>",
              "spec_id": "p-...",
              "spec_version": "YYYY.MM.DD-major.minor",
              "file_id": "f-...@<version>"
            }

        Sanity-checked:
          - bot must be in network.json
          - file must exist on disk
          - file must be inside the bot's workspace (no path traversal)
          - spec_id must be a v7-arc pattern

        Returns:
            200 {ok, kind, file_path} — marker re-stamped
            400 {error} — bad input / unsupported kind / unsafe path
            403 {error, manual_cli} — direct write hit PermissionError;
                                      ``manual_cli`` is a shell command the
                                      operator can run via SSH to apply the fix
                                      with bot-user privileges
            404 {error} — bot/file not found
        """
        import re as _re
        from ..applications.provenance import render_marked_text
        from ..applications.app_ownership_policy import can_app_own

        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400

        kind = body.get("kind")
        if kind not in ("stamp_marker", "rewrite_marker_to_spec"):
            return jsonify({
                "error": (
                    f"unsupported fix kind {kind!r}; v1 supports "
                    "stamp_marker and rewrite_marker_to_spec only "
                    "(attach_to_instance_or_archive needs operator decision)"
                ),
            }), 400

        network = load_network(network_path)
        if bot_id not in (network.get("bots") or {}):
            return jsonify({"error": f"unknown bot_id {bot_id!r}"}), 404

        file_path = (body.get("file_path") or "").strip()
        if not file_path:
            return jsonify({"error": "file_path required"}), 400

        spec_id = (body.get("spec_id") or "").strip()  # identity: see resolve_app_id — a REQUEST-BODY field naming the package a marker attributes to, validated below; nothing on disk is identified.
        if not _re.match(r"^p-[a-f0-9]{8}$", spec_id):
            return jsonify({"error": f"spec_id {spec_id!r} not in canonical p-xxxxxxxx form"}), 400

        file_id = (body.get("file_id") or "").strip()
        if not file_id:
            return jsonify({"error": "file_id required"}), 400
        # Validate the file_id shape (``f-<id>[@<version>]``). Beyond rejecting a
        # malformed id, this is an injection guard: file_id is interpolated into
        # the ``manual_cli`` python3 -c hint below, so constraining it to an
        # injection-safe charset (no quotes / shell metacharacters) keeps a
        # crafted recon-ledger request from smuggling code into that copy-paste
        # string. The privileged-helper path passes it only as marker content,
        # never as a shell/argv token, so this is belt-and-suspenders there.
        if not _re.match(r"^f-[0-9A-Za-z_-]+(@[0-9A-Za-z._-]+)?$", file_id):
            return jsonify({
                "error": f"file_id {file_id!r} not in canonical f-<id>[@version] form",
            }), 400

        # Path-traversal guard: target file must live inside this bot's
        # workspace. Use resolved-paths comparison so symlinks can't escape.
        try:
            workspace = (resolve_bot_paths(bot_id).get("workspace") or
                         f"/Users/{bot_id}/.openclaw/workspace")
            ws_resolved = Path(workspace).resolve()
            target = Path(file_path).resolve()
            target.relative_to(ws_resolved)
        except ValueError:
            return jsonify({
                "error": f"file_path {file_path!r} is outside bot {bot_id!r}'s workspace",
            }), 400
        except Exception as exc:
            return jsonify({"error": f"path resolution failed: {exc}"}), 400

        if not target.is_file():
            return jsonify({"error": f"file not found: {file_path}"}), 404

        # ── Ownership-policy guard (shared predicate) ───────────────────────
        # The marker-WRITE side must consume the SAME can_app_own policy the
        # scrub action (routes_applications_sync) and the claims/marker sides
        # of the recon ledger already share. The Phase-5 stamp writer was gated
        # in #3301 and the realized_files claims side in #3341; this apply-fix
        # endpoint is the sibling write site that was missed — it trusted the
        # caller's file_path. Stamping (or rewriting) a `_evolve` text marker
        # onto a never-ownable path (a secret like member-hash-salt.bin, an
        # evolve/ telemetry rec-*.json, an OC-standard AGENTS.md) is exactly the
        # invalid-claim corruption can_app_own exists to prevent. By recon-ledger
        # construction the UI never offers stamp/rewrite for such a path (those
        # markers route to scrub_candidate / invalid_claim, never missing_marker
        # / stale_pkg), so this refusal is pure defense-in-depth against a
        # hand-crafted or stale request — a legitimate fix is unaffected.
        rel = str(target.relative_to(ws_resolved))
        if not can_app_own(rel, name=target.name):
            return jsonify({
                "error": (
                    f"{file_path} is not an application-ownable path (platform "
                    "telemetry, scanner/manifest state, a secret/runtime file, or "
                    "an OpenClaw-standard file). A provenance marker here would be "
                    "an invalid claim — scrub the stale marker instead of stamping."
                ),
                "denied_by": "ownership_policy",
            }), 400

        # ── Apply ──────────────────────────────────────────────────────────
        # Both kinds re-embed a `spec=` marker; rewrite uses merge=False to
        # force replacement of any pre-existing v6 marker. stamp uses default
        # merge=True (safe: union with any existing). Compute the marked content
        # once (evolve has ACL READ even on bot-owned files); a None here means
        # the file is unreadable / unparseable — surface that instead of writing
        # nothing and falsely reporting success.
        new_text = render_marked_text(
            target,
            pkg_ids=[spec_id],
            file_id=file_id,
            keyword="spec",
            merge=(kind == "stamp_marker"),
        )
        if new_text is None:
            return jsonify({
                "error": f"could not read/parse {file_path} to stamp a marker",
            }), 422

        try:
            from evolve_util import atomic_write_text
            atomic_write_text(target, new_text, mode=0o644)
        except PermissionError:
            # Bot-owned workspace files outside workspace/evolve/ — evolve has
            # ACL read but not write, so the in-place atomic write above fails.
            # PRIMARY path: apply the already-computed marked content via the
            # narrow, single-purpose marker_embed_helper under the §11h sudoers
            # grant — it re-validates the destination (bound to this bot's
            # workspace + can_app_own, symlink-/TOCTOU-proof) and writes as the
            # bot. No SSH for the operator.
            from ..applications.marker_embed_helper import embed_marker_privileged

            ok, detail = embed_marker_privileged(
                target, new_text=new_text, bot_id=bot_id,
            )
            if ok:
                return jsonify({
                    "ok": True,
                    "kind": kind,
                    "file_path": str(target),
                    "spec_id": spec_id,
                    "file_id": file_id,
                    "applied_via": "privileged_helper",
                })
            # FALLBACK: the grant isn't deployed on this pod yet (dormant until
            # `refresh-sudoers`), the helper is absent, or it still hit EACCES.
            # Surface the legacy CLI hint so older pods don't regress.
            # Venv python (evolve_admin is installed there; system python3
            # can't import it), and cd /tmp first — python3 puts the CWD on
            # sys.path and the bot user can't traverse the operator's home.
            manual_cli = (
                f"cd /tmp && sudo -u {bot_id} /Users/Shared/evolve-venv/bin/python3 -c \""
                f"from evolve_admin.applications.provenance import embed_marker; "
                f"from pathlib import Path; "
                f"embed_marker(Path('{file_path}'), pkg_ids=['{spec_id}'], "
                f"file_id='{file_id}', keyword='spec', "
                f"merge={kind == 'stamp_marker'})\""
            )
            return jsonify({
                "error": (
                    f"evolve user lacks write permission on {file_path} and the "
                    f"server-side privileged stamp was unavailable ({detail}). "
                    "Run the manual_cli command via SSH (or as the bot user), or "
                    "run `sudo evolve-admin refresh-sudoers` to activate the "
                    "server-side fix."
                ),
                "manual_cli": manual_cli,
                "denied_by": "filesystem_acl",
            }), 403
        except Exception as exc:
            return jsonify({"error": f"marker write failed: {exc}"}), 500

        return jsonify({
            "ok": True,
            "kind": kind,
            "file_path": str(target),
            "spec_id": spec_id,
            "file_id": file_id,
        })

    @app.post("/api/bots/<bot_id>/reflect/reconcile")
    def api_reflect_reconcile(bot_id: str) -> Response | tuple[Response, int]:
        """Run the auto-reconcile that converts Reflect orphan_file findings
        into Instance.realized_files[] entries.

        Resolution shares ONE authority with the classifier: Reflect (a thin reader of the recon
        ledger) already resolved each ``attach_candidate`` marker — lineage-aware — to its owning
        app's current spec_id, and this action attaches the file to that app's manifest (resolved
        to its on-disk Path through the SAME ``build_spec_index``, so a ``discovered`` app with no
        materialized Instance — ``instance_id is None`` — is attachable too). identity: see
        app_identity.resolve_app_id — these are MARKER-to-Spec bindings, not an app's own id.
        A candidate the modal shows is therefore always attachable (no "no
        Instance for spec_id"). >1 manifest sharing one current spec_id is
        surfaced as ``ambiguous`` for manual triage rather than attached
        arbitrarily. Attaching appends ``realized_files[]`` only — it never
        promotes a discovered app to defined.

        Body (all optional):
            {"apply": true | false,        # default false = dry-run
             "paths": ["scripts/x.sh", …]} # targeted subset: only attach these
                                           # candidates (per-row / batch). Omit
                                           # to attach ALL candidates ("Attach
                                           # all"). Paths may be absolute or
                                           # workspace-relative.

        The per-Instance batching means a list of paths is ONE read-mutate-write
        per affected Instance, so "Attach all (N)" is a single cheap call. This
        endpoint never re-runs discovery; the caller refreshes via the cheap
        GET reflect re-check.

        Returns:
            200 {ok, bot_id, applied, resolved: [...], ambiguous: [...],
                 unmatched: [...], skipped_already_listed: [...],
                 counts_by_spec: {...}, warnings: [...]}
            400 {error}   # malformed body / paths
            404 {error}   # unknown bot_id
            500 {error}   # reconcile threw
        """
        from dataclasses import asdict
        from ..applications.manifest_hygiene import reconcile_orphan_markers

        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        apply = bool(body.get("apply", False))

        # Optional targeted subset. None = attach all candidates. A list = only
        # these paths (per-row Attach or a batch of rows). Validate it's a list
        # of strings so a malformed client can't silently degrade to "attach all".
        paths = body.get("paths", None)
        if paths is not None:
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                return jsonify({"error": "paths must be a list of strings"}), 400

        network = load_network(network_path)
        if bot_id not in (network.get("bots") or {}):
            return jsonify({"error": f"unknown bot_id {bot_id!r}"}), 404
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))

        try:
            result = reconcile_orphan_markers(
                bot_id, apply=apply, shared_dir=shared_dir, paths=paths)
        except Exception as exc:
            return jsonify({"error": f"reconcile failed: {exc}"}), 500

        return jsonify({
            "ok": True,
            "bot_id": result.bot_id,
            "applied": result.applied,
            "resolved": [asdict(x) for x in result.resolved],
            "ambiguous": [asdict(x) for x in result.ambiguous],
            "unmatched": [asdict(x) for x in result.unmatched],
            "skipped_already_listed": result.skipped_already_listed,
            "counts_by_spec": result.counts_by_spec(),
            "warnings": result.warnings,
        })

    @app.post("/api/bots/<bot_id>/file-index/rebuild")
    def api_file_index_rebuild(bot_id: str) -> Response:
        """
        Trigger a full file-index rebuild across all bots and return the result
        filtered to the requested bot.

        This is the expensive variant — it re-reads every manifest in the network.
        Use sparingly (e.g. after a forge approval or batch import).

        Response:
            {bot_id, index: {file_id → record}, total, rebuilt: true}
        """
        from ..applications.file_index import rebuild_file_index

        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        bot_ids = list(network.get("bots", {}).keys())

        try:
            full_index = rebuild_file_index(shared_dir, bot_ids)
            bot_entries = {
                fid: rec for fid, rec in full_index.items()
                if rec.get("bot_id") == bot_id
            }
            return jsonify({
                "bot_id": bot_id,
                "index": bot_entries,
                "total": len(bot_entries),
                "rebuilt": True,
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/applications/<bot_id>/<app_id>/spec")
    def api_application_spec(bot_id: str, app_id: str) -> Response:
        """Return the spec.md file content for an application workspace."""
        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        app_dir = shared_dir / "applications" / bot_id / app_id
        # Try several common filenames
        for fname in ("spec.md", "spec.txt", "SPEC.md", "design.md"):
            spec_path = app_dir / fname
            if spec_path.exists():
                try:
                    return jsonify({"ok": True, "filename": fname, "content": spec_path.read_text()})
                except Exception as exc:
                    return jsonify({"error": str(exc)}), 500
        return jsonify({"error": "No spec file found in application workspace."}), 404

    @app.get("/api/applications/compliance")
    def api_applications_compliance() -> Response:
        """Return manifest compliance status for all bots (or a single bot via ?bot=)."""
        from ..applications.scanner import scan_compliance, scan_compliance_all
        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        bot_filter = request.args.get("bot")
        bot_ids = [bot_filter] if bot_filter else list(network.get("bots", {}).keys())
        try:
            result = scan_compliance_all(shared_dir, bot_ids)
            return jsonify({"ok": True, **result})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/api/applications/remediate")
    def api_applications_remediate() -> Response:
        """Re-run pod_conduct injection and manifest-spec distribution for all bots (or ?bot=)."""
        from ..deploy import inject_pod_conduct, _write_pod_conduct, DeployResult
        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        bot_filter = request.args.get("bot")
        bot_ids = [bot_filter] if bot_filter else list(network.get("bots", {}).keys())

        results: dict = {}
        # Re-distribute manifest-spec docs to shared dir
        r = DeployResult(bot_id="shared", success=True)
        try:
            _write_pod_conduct(shared_dir, r)
        except Exception as e:
            r.log(f"[error] _write_pod_conduct: {e}")

        for bot_id in bot_ids:
            bot_result: dict = {"ok": True, "logs": [], "errors": []}
            try:
                inject_pod_conduct(bot_id)
                bot_result["logs"].append("pod_conduct.md workspace injection: ok")
            except Exception as e:
                bot_result["ok"] = False
                bot_result["errors"].append(f"conduct injection: {e}")
            results[bot_id] = bot_result

            # Per-bot audit entry — heal.py's drift detector matches
            # entries by bot_id, so a single bot_id="admin" entry never
            # credits the per-bot drift check and every remediated bot
            # false-positives security.config_drift on the next sweep.
            # inject_pod_conduct → strip_agents_main can rewrite the
            # `agents` top-level key when a stray agents.main is present
            # (the rest of the work is workspace-side and doesn't touch
            # openclaw.json). Declaring `agents` over-credits in the
            # nominal no-op case, which is the safe direction.
            if bot_result["ok"]:
                _audit_log_entry(
                    "manifest.remediate",
                    bot_id,
                    {"logs": bot_result["logs"]},
                    oc_keys={"agents"},
                )

        _audit_log_entry("manifest.remediate.summary", "admin", {"bots": bot_ids})
        return jsonify({
            "ok": all(v["ok"] for v in results.values()),
            "bots": results,
            "shared_logs": r.steps,
        })

    # /api/applications/<bot>/<app>/run-tests, /api/applications/test-telemetry/<day>,
    # and /api/applications/<bot>/<app>/test-cases/<case>/run were removed
    # 2026-06-08 along with the app-test surface. See
    # docs/decision-app-tests-2026-06-08.md.

    # ── Reports ───────────────────────────────────────────────────────────────

    def _ra_shared() -> Path:
        return Path(load_network(network_path).get("sharedDir", "/Users/Shared/evolve"))

    # v2: thresholds live under network.json → pod_report.thresholds, not in
    # the legacy shared_dir/thresholds.json. The keys match
    # pod_report.DEFAULT_OVERRIDES (8 simple scalars; per-bot overrides aren't
    # supported in v2 because the baseline IS per-bot).

    # Allowlist of pod_report config keys recognized by pod_report.py v2.
    # Anything else gets dropped on PATCH so network.json doesn't accumulate
    # obsolete v1 keys (morning_enabled, evening_hour, sections, ...).
    _POD_REPORT_ALLOWED_KEYS = {
        "enabled", "report_hour", "frequency", "weekly_day",
        "notify_on", "thresholds",
    }

    @app.get("/api/reports-alerts/config")
    def api_ra_config_get() -> Response:
        net = load_network(network_path)
        cfg = net.get("pod_report", {})
        # Filter to known keys so the UI never sees v1 cruft.
        cleaned = {k: v for k, v in cfg.items() if k in _POD_REPORT_ALLOWED_KEYS}
        return jsonify({"pod_report": cleaned})

    @app.patch("/api/reports-alerts/config")
    def api_ra_config_patch() -> Response:
        body = request.get_json() or {}
        net = load_network(network_path)
        incoming = body.get("pod_report", {})
        # Drop unknown keys from incoming
        incoming = {k: v for k, v in incoming.items() if k in _POD_REPORT_ALLOWED_KEYS}
        existing = net.get("pod_report", {})
        # Drop unknown keys from existing — replaces v1 cruft on first save
        existing = {k: v for k, v in existing.items() if k in _POD_REPORT_ALLOWED_KEYS}
        net["pod_report"] = {**existing, **incoming}
        save_network(net, network_path)
        return jsonify({"ok": True})

    @app.get("/api/reports-alerts/thresholds")
    def api_ra_thresholds_get() -> Response:
        """Return pod_report v2 thresholds as a pod + per-bot matrix.

        Shape (compat with the legacy ``thresholds`` block + new matrix):
          {
            "thresholds": {  # legacy: pod-resolved value + hardcoded default
              "<key>": {"value": <pod-resolved>, "default": <hardcoded>}
            },
            "matrix": {
              "params": [{"id","label","help","unit","step","min","decimals","default"}],
              "pod":    {<key>: <effective pod default (network override OR hardcoded)>},
              "bots":   {<bot_id>: {<key>: {"value": <override|None>,
                                            "effective": <resolved>,
                                            "source": "override"|"pod"|"default"}}},
              "scope":  {<key>: "pod"|"bot"}  # pod-only knobs aren't editable per-bot
            }
          }
        """
        try:
            import pod_report  # type: ignore[import-not-found]
            defaults = dict(pod_report.DEFAULT_OVERRIDES)
        except Exception:
            defaults = {}
        net = load_network(network_path)
        pod_cfg = (net.get("pod_report") or {})
        pod_user = pod_cfg.get("thresholds") or {}
        per_bot_user = pod_cfg.get("thresholds_per_bot") or {}
        members = net.get("members", []) or []
        bot_ids: list[str] = [m if isinstance(m, str) else (m or {}).get("id", "") for m in members]
        bot_ids = [b for b in bot_ids if b]

        # Param metadata — labels + decimals + step shapes. Help text + scope
        # come straight from DEFAULT_OVERRIDES comments in pod_report.py.
        _META = {
            "pod_silent_session_floor": {
                "label": "Pod silent floor", "help": "Pod-wide sessions ≤ this fires Pod-silent alert",
                "step": 1, "min": 0, "decimals": 0, "unit": "", "scope": "pod",
            },
            "cost_anomaly_factor": {
                "label": "Cost spike factor", "help": "Ratio over 30d mean that fires a cost-spike anomaly",
                "step": 0.1, "min": 1.0, "decimals": 2, "unit": "×", "scope": "bot",
            },
            "cost_anomaly_factor_cold": {
                "label": "Cost spike factor (cold)", "help": "Stricter factor when baseline n<14",
                "step": 0.1, "min": 1.0, "decimals": 2, "unit": "×", "scope": "bot",
            },
            "cost_min_mean_usd": {
                "label": "Cost min mean", "help": "Suppress spike when 30d mean is below this",
                "step": 0.05, "min": 0.0, "decimals": 2, "unit": "$", "scope": "bot",
            },
            "sessions_anomaly_factor": {
                "label": "Session-drop factor", "help": "Ratio under 30d mean that fires a session-drop anomaly",
                "step": 0.05, "min": 0.0, "decimals": 2, "unit": "×", "scope": "bot",
            },
            "sessions_anomaly_factor_cold": {
                "label": "Session-drop factor (cold)", "help": "Stricter factor when baseline n<14",
                "step": 0.05, "min": 0.0, "decimals": 2, "unit": "×", "scope": "bot",
            },
            "sessions_min_mean": {
                "label": "Sessions min mean", "help": "Suppress drop when 30d mean is below this",
                "step": 0.5, "min": 0.0, "decimals": 1, "unit": "", "scope": "bot",
            },
        }
        params = []
        for key, hardcoded in defaults.items():
            m = _META.get(key, {"label": key, "help": "", "step": 0.1,
                                 "min": 0.0, "decimals": 2, "unit": "", "scope": "pod"})
            params.append({"id": key, "default": hardcoded, **m})

        # Pod-effective defaults: network override OR hardcoded fallback.
        pod_effective = {k: pod_user.get(k, defaults[k]) for k in defaults}

        # Legacy block — preserve the existing UI's read path.
        merged = {k: {"value": pod_user.get(k, defaults[k]), "default": defaults[k]}
                  for k in defaults}

        bots_rows: dict[str, dict] = {}
        for bot_id in bot_ids:
            bot_user = per_bot_user.get(bot_id) or {}
            row: dict[str, dict] = {}
            for key in defaults:
                override = bot_user.get(key)
                if override is not None:
                    eff = override
                    src = "override"
                elif key in pod_user:
                    eff = pod_user[key]
                    src = "pod"
                else:
                    eff = defaults[key]
                    src = "default"
                row[key] = {"value": override, "effective": eff, "source": src}
            bots_rows[bot_id] = row

        return jsonify({
            "thresholds": merged,
            "matrix": {
                "params": params,
                "pod": pod_effective,
                "bots": bots_rows,
                "scope": {k: _META.get(k, {}).get("scope", "pod") for k in defaults},
            },
        })

    @app.patch("/api/reports-alerts/thresholds")
    def api_ra_thresholds_patch() -> Response:
        """Write threshold overrides into ``network.json → pod_report``.

        Body shapes (one or both):
          {"thresholds": {<key>: <value>|null}}        # pod-wide override
          {"bot_id": "<bot>", "thresholds": {...}}     # per-bot override
        Null values reset that key (remove it from the override map).
        Allowlist enforced: only DEFAULT_OVERRIDES keys are accepted.
        """
        body = request.get_json() or {}
        incoming = body.get("thresholds", {}) or {}
        bot_id = (body.get("bot_id") or "").strip() or None
        try:
            import pod_report  # type: ignore[import-not-found]
            allowed = set(pod_report.DEFAULT_OVERRIDES.keys())
        except Exception:
            allowed = set()
        clean = {k: v for k, v in incoming.items() if k in allowed and v is not None}

        net = load_network(network_path)
        pod_cfg = net.get("pod_report", {})

        if bot_id:
            per_bot = pod_cfg.get("thresholds_per_bot") or {}
            existing = {k: v for k, v in (per_bot.get(bot_id) or {}).items() if k in allowed}
            for k, v in incoming.items():
                if v is None and k in existing:
                    del existing[k]
            existing.update(clean)
            if existing:
                per_bot[bot_id] = existing
            else:
                # Drop empty entries so the file stays tidy.
                per_bot.pop(bot_id, None)
            if per_bot:
                pod_cfg["thresholds_per_bot"] = per_bot
            else:
                pod_cfg.pop("thresholds_per_bot", None)
        else:
            existing = {k: v for k, v in (pod_cfg.get("thresholds") or {}).items() if k in allowed}
            for k, v in incoming.items():
                if v is None and k in existing:
                    del existing[k]
            existing.update(clean)
            if existing:
                pod_cfg["thresholds"] = existing
            else:
                pod_cfg.pop("thresholds", None)

        net["pod_report"] = pod_cfg
        save_network(net, network_path)
        return jsonify({"ok": True})

    @app.get("/api/reports-alerts/status")
    def api_ra_status() -> Response:
        """Return the same three-bucket structure pod_report.run_report() produces.

        v2: this endpoint is no longer a separate stack of section computations
        (which historically drifted out of sync with the Telegram message). It
        delegates to the same renderer the Telegram report uses, so the admin
        UI tile and the Telegram message agree by construction.
        """
        net = load_network(network_path)
        shared = Path(net.get("sharedDir", "/Users/Shared/evolve"))
        members = net.get("members", [])
        try:
            import pod_report  # type: ignore[import-not-found]
            overrides = pod_report._load_overrides(net)
            _, overall, structured = pod_report.run_report(
                shared, members, overrides, label="Live", config=net,
            )
            return jsonify({
                "buckets": structured["buckets"],
                "empty_summary": structured["empty_summary"],
                "ref_date": structured["ref_date"],
                "overall": overall,
                "last_non_green": structured.get("last_non_green"),
            })
        except Exception as e:
            return jsonify({
                "buckets": {"broken": [], "trending": [], "queue": []},
                "empty_summary": "",
                "ref_date": "",
                "overall": "unknown",
                "last_non_green": None,
                "error": str(e),
            })

    @app.get("/api/reports-alerts/history")
    def api_ra_history() -> Response:
        """Return recent report deliveries, newest first.

        Prefers JSON artifacts at shared_dir/reports/ (each carries the full
        report_text body). Falls back to the legacy one-line log so older
        deliveries still appear, with report_text=null for those rows.
        """
        shared = _ra_shared()
        entries: list[dict] = []

        # Primary source: per-run JSON artifacts written by pod_report.py.
        reports_dir = shared / "reports"
        if reports_dir.exists():
            try:
                files = sorted(
                    (f for f in reports_dir.glob("*.json")),
                    key=lambda f: f.name,
                    reverse=True,
                )[:50]
                for f in files:
                    try:
                        d = json.loads(f.read_text())
                        ts_raw = str(d.get("ts", ""))
                        entries.append({
                            "ts": ts_raw.replace("T", " ").split("+")[0],
                            "label": d.get("label", ""),
                            "status": d.get("status", "unknown"),
                            "sent": bool(d.get("sent", False)),
                            "report_text": d.get("report_text") or None,
                        })
                    except Exception:
                        continue
            except Exception:
                pass

        # Fallback: legacy log file. Skip rows already covered by an artifact
        # (matched by ts+label) so we don't double-count.
        log_path = shared / "logs" / "pod-report.log"
        if log_path.exists():
            seen = {(e["ts"], e["label"]) for e in entries}
            try:
                for line in log_path.read_text().splitlines()[-50:]:
                    # format: 2026-04-15T08:00:01+00:00 [Morning] status=green sent=True
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    ts_raw = parts[0]
                    label = parts[1].strip("[]") if len(parts) > 1 else ""
                    ts_norm = ts_raw.replace("T", " ").split("+")[0]
                    if (ts_norm, label) in seen:
                        continue
                    status_part = next((p for p in parts if p.startswith("status=")), "status=unknown")
                    sent_part = next((p for p in parts if p.startswith("sent=")), "sent=False")
                    entries.append({
                        "ts": ts_norm,
                        "label": label,
                        "status": status_part.split("=", 1)[1],
                        "sent": sent_part.split("=", 1)[1].lower() == "true",
                        "report_text": None,
                    })
            except Exception:
                pass

        # Newest first. Artifacts arrive sorted; merge log entries by ts.
        entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
        return jsonify({"entries": entries[:30]})

    @app.post("/api/reports-alerts/send-test")
    def api_ra_send_test() -> Response:
        """Trigger pod_report.py --force to generate and send a real report.

        Accepts optional body: {"real_send": true} (default true).
        Returns: {sent, preview, overall, error}
        """
        import tempfile as _tf
        body = request.get_json() or {}
        real_send = body.get("real_send", True)

        # Locate pod_report.py relative to this file
        _server_file = Path(__file__).resolve()
        # packages/admin/evolve_admin/web/server.py → repo root is 4 levels up
        _repo_root = _server_file.parent.parent.parent.parent.parent
        script = _repo_root / "packages" / "analyzer" / "pod_report.py"
        if not script.exists():
            return jsonify({"sent": False, "preview": "", "overall": "unknown",
                            "error": f"pod_report.py not found at {script}"}), 404

        net = load_network(network_path)
        net_path_str = str(network_path)

        # Write result to a temp file so we can capture preview + overall
        fd, out_path = _tf.mkstemp(suffix=".json", dir="/tmp", prefix="evolve-report-")
        os.close(fd)

        cmd = [
            sys.executable, str(script),
            "--network", net_path_str,
            "--output-json", out_path,
        ]
        if real_send:
            cmd.append("--force")
        else:
            cmd.append("--dry-run")

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            # Read structured output
            result_data: dict = {}
            try:
                result_data = json.loads(Path(out_path).read_text())
            except Exception:
                pass
            try:
                os.unlink(out_path)
            except Exception:
                pass

            preview = result_data.get("report_text", proc.stdout.strip() or proc.stderr.strip())
            overall = result_data.get("overall", "unknown")
            sent = real_send and proc.returncode == 0
            error = None if proc.returncode == 0 else (proc.stderr.strip() or f"exit code {proc.returncode}")
            return jsonify({"sent": sent, "preview": preview, "overall": overall, "error": error})
        except subprocess.TimeoutExpired:
            try:
                os.unlink(out_path)
            except Exception:
                pass
            return jsonify({"sent": False, "preview": "", "overall": "unknown",
                            "error": "timeout after 45s"})
        except Exception as e:
            return jsonify({"sent": False, "preview": "", "overall": "unknown",
                            "error": str(e)}), 500

    # ── Forge job recovery ─────────────────────────────────────────────────
    # Admin-ui restarts (launchd KeepAlive auto-respawn) orphan any in-flight
    # forge daemon threads. The job state on disk reads "running" but no
    # thread is advancing it. Re-spawn daemon threads for any orphaned jobs;
    # the dispatch primitives are idempotent (resume from existing outbox),
    # so phases that already completed advance instantly.
    try:
        from ..applications import forge_engine as _fe
        from evolve_config import load_config, get_shared_dir
        _resume_shared_dir = get_shared_dir(load_config(network_path))
        _resumed = _fe.recover_orphaned_jobs(_resume_shared_dir)
        if _resumed:
            _log.info("forge: resumed %d orphaned job(s) on startup", _resumed)
    except Exception as _resume_exc:
        _log.warning("forge: orphan recovery failed (non-fatal): %s", _resume_exc)

    # ── Cost-cap normalization migration (Phase 2) ─────────────────────────
    # One-shot, idempotent: moves legacy per-bot daily_cap_usd (network.json)
    # and sandbox-override sessionBudgetCapUsd + cacheRetention into the
    # canonical better-engine-config store, then strips the legacy keys.
    # No-op once data lives only in BE config.
    try:
        from ..migrations.cost_caps_normalize import run as _cost_caps_run
        from evolve_config import load_config, get_shared_dir
        _cc_shared_dir = get_shared_dir(load_config(network_path))
        _cc_result = _cost_caps_run(_cc_shared_dir, network_path)
        if _cc_result.total_changes or _cc_result.errors:
            _log.info("%s", _cc_result.summary_line())
            for _err in _cc_result.errors:
                _log.warning("cost_caps_normalize: %s", _err)
    except Exception as _cc_exc:
        _log.warning(
            "cost_caps_normalize: migration crashed (non-fatal): %s", _cc_exc
        )

    return app


# ── Helpers ───────────────────────────────────────────────────────────────────

def _result_dict(name: str, result: Any) -> dict:
    return {
        "name": name,
        "success": result.success,
        "steps": result.steps,
        "errors": result.errors,
    }


def _run_proposal_validation(
    proposal_id: str,
    proposal_path: Path,
    shared_dir: Path,
    network: dict,
) -> dict:
    """Validate a proposal immediately after approval and write the result.

    Returns a summary dict for the API response. Never raises — failures are
    captured and written as error results so apply.py can skip cleanly.
    """
    results_dir = shared_dir / "proposals" / "validation-results"
    results_dir.mkdir(parents=True, exist_ok=True)
    result_path = results_dir / f"{proposal_id}.json"

    try:
        from validate import validate_proposal, now_iso  # type: ignore[import]
        proposal = json.loads(proposal_path.read_text())
        vr = validate_proposal(proposal, network, shared_dir)
        vr.validated_at = now_iso()
        result_dict = vr.to_dict()
        _write_json_sudo_fallback(result_path, result_dict)
        return {
            "result": vr.result,
            "recommendation": vr.recommendation,
            "notes": vr.validation_notes,
        }
    except Exception as exc:
        # Write an error result so apply.py doesn't wait forever
        error_result = {
            "proposal_id": proposal_id,
            "validated_at": _now_iso(),
            "result": "error",
            "recommendation": "escalate",
            "validation_notes": f"Validation error: {exc}",
            "tests_run": [],
        }
        try:
            _write_json_sudo_fallback(result_path, error_result)
        except Exception:
            pass
        return {"result": "error", "recommendation": "escalate", "notes": str(exc)}


# _JUDGE_PRICE_PER_MTOK_USD / _estimate_judge_cost_usd /
# _summarize_test_telemetry were removed 2026-06-08 — app-test surface
# killed per docs/decision-app-tests-2026-06-08.md.


def resolve_bot_paths(bot_id: str, user: str | None = None) -> dict:
    """Resolve all important file paths for a bot by reading its own openclaw.json.

    bot_id  — the logical bot name (key in network.json)
    user    — the actual system username for sudo/pwd lookups.
              If omitted, falls back to bot_id (works when they match).

    Falls back to standard paths if config read fails. Never constructs paths
    from assumptions — always reads from the bot's actual config.

    Returns dict with keys:
      oc_config, workspace, agent_dir, auth_profiles, turns_dir,
      turns_dir_fallback, logs_dir, user
    """
    actual_user = user or bot_id
    try:
        import pwd as _pwd
        bot_home = _pwd.getpwnam(actual_user).pw_dir
    except KeyError:
        # Pre-account fallback via the platform home root (/Users on macOS,
        # /home on Linux), not a macOS literal.
        bot_home = f"{get_profile().user_home_root}/{actual_user}"

    oc_config_path = f"{bot_home}/.openclaw/openclaw.json"
    workspace = bot_home + "/.openclaw/workspace"
    agent_dir = bot_home + "/.openclaw/agents/main/agent"

    # Read openclaw.json to get configured paths (direct first, then sudo /bin/cat as root)
    oc_text: str | None = None
    try:
        oc_text = Path(oc_config_path).read_text()
    except PermissionError:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", oc_config_path],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                oc_text = r.stdout
        except Exception:
            pass
    except OSError:
        pass

    if oc_text:
        try:
            data = json.loads(oc_text)
            ws = data.get("agents", {}).get("defaults", {}).get("workspace")
            if ws:
                workspace = ws
            agents_list = data.get("agents", {}).get("list", [])
            main_agent = next((a for a in agents_list if a.get("id") == "main"), None)
            if main_agent and main_agent.get("agentDir"):
                agent_dir = main_agent["agentDir"]
        except Exception:
            pass

    # Build the list of candidate turns dirs in priority order.
    # We search multiple locations because deployments vary:
    #   - New-style: {shared_dir}/{bot}/turns/  (world-readable, preferred)
    #   - Workspace memory: {workspace}/memory/           (bot-owned, sudo needed)
    #   - Legacy workspace memory under home: {home}/.openclaw/workspace/memory/
    # Using a list lets callers try all of them without hardcoding assumptions.
    # The shared-dir candidate is platform-keyed via CANONICAL_SHARED_DIR
    # (/Users/Shared/evolve on macOS, /var/lib/evolve on Linux) — a macOS literal
    # here stranded every Linux pod's Usage page at "No turn data found".
    from evolve_config import CANONICAL_SHARED_DIR
    turns_dir_candidates: list[str] = []
    shared_turns = f"{CANONICAL_SHARED_DIR}/{bot_id}/turns"
    turns_dir_candidates.append(shared_turns)
    workspace_memory = workspace + "/memory"
    turns_dir_candidates.append(workspace_memory)
    # If workspace was read from config and differs from the home-derived default,
    # also add the home-derived path as an extra fallback.
    home_derived_memory = bot_home + "/.openclaw/workspace/memory"
    if home_derived_memory not in turns_dir_candidates:
        turns_dir_candidates.append(home_derived_memory)

    return {
        "oc_config": oc_config_path,
        "workspace": workspace,
        "agent_dir": agent_dir,
        "auth_profiles": agent_dir + "/auth-profiles.json",
        "turns_dir": shared_turns,               # primary (new-style shared dir)
        "turns_dir_fallback": workspace_memory,  # secondary (bot workspace)
        "turns_dir_candidates": turns_dir_candidates,  # all candidates for robust search
        "logs_dir": bot_home + "/.openclaw/logs",
        "user": actual_user,
    }


def _write_json_sudo_fallback(path: Path, data: dict) -> None:
    """/tmp-staging + sudo-cp JSON write for files the evolve user may not
    own (the bot-file write pattern — see CLAUDE.md). NOT an atomic write:
    the copy crosses filesystems, so readers can observe a torn file. For
    evolve-owned paths use ``evolve_util.atomic_write_json`` instead."""
    import tempfile, subprocess as _sp, shutil as _shutil
    tmp_fd, tmp_path = tempfile.mkstemp(dir="/tmp", prefix="evolve-proposal-", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copy2(tmp_path, str(path))
        except PermissionError:
            _sp.run(["sudo", "/bin/mkdir", "-p", str(path.parent)], capture_output=True)
            _sp.run(["sudo", "/bin/cp", tmp_path, str(path)], check=True, capture_output=True)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ───────────────────────────────────────────────────────────────
# Analytics routes shim — body lives in routes_analytics.py
# ───────────────────────────────────────────────────────────────


def _register_analytics_routes(app: Flask, network_path: Path) -> None:
    """Shim — body lives in routes_analytics.py.

    Kept here (not renamed) so the 5 analytics test files that do
    ``from evolve_admin.web.server import _register_analytics_routes``
    continue to work without modification.
    """
    from .routes_analytics import register_analytics_routes
    return register_analytics_routes(app, network_path)


def _register_gateway_routes(app: Flask, network_path: Path) -> None:
    """Register /api/gateway/status — reads from shared_dir/status/ (no bot home dir access)."""
    import time as _time
    from evolve_admin.ocadmin import _is_gateway_proc

    def _scan_gateway_proc(user: str) -> str | None:
        """Return the gateway PID (as str) for `user`, or None.

        Uses `ps auxww` + the canonical marker list in ocadmin so all openclaw
        process titles are matched (pgrep -f openclaw-gateway alone misses the
        node-entry-point form used by openclaw >= 2026.4.29).
        """
        try:
            res = subprocess.run(
                ["ps", "auxww"], capture_output=True, text=True, timeout=5,
            )
            for line in res.stdout.splitlines():
                if "grep" in line:
                    continue
                if _is_gateway_proc(line, user):
                    parts = line.split()
                    return parts[1] if len(parts) > 1 else None
        except Exception:
            pass
        return None

    def _probe_gateway_direct(bot_id: str, bot_cfg: dict) -> dict:
        """Check gateway health via direct HTTP probe, fallback to process check."""
        port = bot_cfg.get("port")
        user = bot_cfg.get("user", bot_id)
        if port:
            try:
                import urllib.request as _ureq
                req = _ureq.urlopen(
                    f"http://127.0.0.1:{port}/evolve/status", timeout=3
                )
                if req.status == 200:
                    return {
                        "bot_id": bot_id,
                        "gateway_running": True,
                        "gateway_reachable": True,
                        "stale": False,
                        "ts": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
                        "ts_epoch": _time.time(),
                        "gateway_pid": _scan_gateway_proc(user),
                        "source": "direct_probe",
                    }
            except Exception:
                pass
        # Fallback: check if process is running
        pid = _scan_gateway_proc(user)
        return {
            "bot_id": bot_id,
            "gateway_running": pid is not None,
            "gateway_reachable": False,
            "stale": False,
            "ts": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
            "ts_epoch": _time.time(),
            "gateway_pid": pid,
            "source": "process_check",
        }

    @app.get("/api/gateway/status")
    def api_gateway_status() -> Response:
        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        bots = network.get("bots", {})
        # Primary bot's gateway lives on the same Unix account as the admin
        # server. If we're responding, that gateway IS up (we ARE it).
        from primary_bot import primary_bot_id as _primary_bot_id  # type: ignore
        _primary_id = _primary_bot_id(network)
        result = {}
        for bot_id, bot_cfg in bots.items():
            if _primary_id and bot_id == _primary_id:
                result[bot_id] = {
                    "bot_id": bot_id,
                    "gateway_running": True,
                    "gateway_reachable": True,
                    "stale": False,
                    "ts": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
                    "ts_epoch": _time.time(),
                    "gateway_pid": None,
                    "source": "self",
                }
                continue
            status_file = shared_dir / "status" / f"{bot_id}.json"
            try:
                _sf_exists = status_file.exists()
            except (PermissionError, OSError):
                _sf_exists = False
            if _sf_exists:
                try:
                    data = json.loads(status_file.read_text())
                    age_s = _time.time() - data.get("ts_epoch", 0)
                    data["stale"] = age_s > 600
                    if not data["stale"]:
                        result[bot_id] = data
                        continue
                except Exception:
                    pass
            # Status file missing or stale — probe directly
            result[bot_id] = _probe_gateway_direct(bot_id, bot_cfg)
        return jsonify(result)


def _register_bot_config_routes(app: Flask, network_path: Path) -> None:
    """Shim — body lives in routes_bot_config.py."""
    from .routes_bot_config import register_bot_config_routes
    return register_bot_config_routes(app, network_path)


# Keys that must never be pushed pod-wide (too bot-specific or security-sensitive)
CONFIG_PUSH_BLOCKLIST = {
    "gateway.bind", "gateway.port", "gateway.auth",
    "channels", "plugins.entries", "auth",
    "agents.list",
}


def _flatten_config(obj: Any, prefix: str = "", depth: int = 0, max_depth: int = 6) -> dict:
    """Flatten nested dict to dot-notation keys. Skips list values."""
    result: dict = {}
    if not isinstance(obj, dict) or depth >= max_depth:
        return result
    for k, v in obj.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, list):
            continue  # skip arrays — too complex to diff simply
        elif isinstance(v, dict):
            result.update(_flatten_config(v, full_key, depth + 1, max_depth))
        else:
            result[full_key] = v
    return result


# OC routes shim — body lives in routes_oc.py
# ─────────────────────────────────────────────────
# ``_register_oc_routes`` is an alias to the real implementation so
# ``inspect.getsource(server._register_oc_routes)`` resolves to the route
# source in routes_oc.py — required by test_accept_drift_route_drops_privileges.py.
from .routes_oc import register_oc_routes as _register_oc_routes  # noqa: E402


def _register_kaizen_routes(app: Flask, network_path: Path) -> None:
    """Register /api/kaizen/latest — reads latest community intelligence report."""

    @app.get("/api/kaizen/latest")
    def api_kaizen_latest():
        """Returns the most recent community intelligence report."""
        network = load_network(network_path)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        kaizen_dir = shared_dir / "kaizen"
        if not kaizen_dir.exists():
            return jsonify({"available": False, "message": "No kaizen data yet"})
        files = sorted(kaizen_dir.glob("*.md"), reverse=True)
        if not files:
            return jsonify({"available": False, "message": "No kaizen reports found"})
        latest = files[0]
        try:
            stem = latest.stem
            report_date = date.fromisoformat(stem)
            age_days = (date.today() - report_date).days
            return jsonify({
                "available": True,
                "date": stem,
                "content": latest.read_text(),
                "age_days": age_days,
            })
        except (ValueError, OSError) as e:
            return jsonify({"available": False, "message": str(e)})


def _register_accounts_routes(app: Flask, network_path: Path) -> None:
    """Register /api/accounts/status — auth profile routing status per bot."""

    def _get_account_tiers_for_bot(bot_id: str, network: dict) -> dict:
        """Returns the accounts.tiers config from network.json."""
        return network.get("accounts", {}).get("tiers", {})

    @app.get("/api/accounts/status")
    def api_accounts_status() -> Response:
        """
        Returns auth profile routing status.
        Shows which account tiers are configured, which session types map to which
        profiles, and whether account routing is enabled.
        """
        network = load_network(network_path)
        accounts_cfg = network.get("accounts", {})
        routing_cfg = accounts_cfg.get("routing", {})
        tiers_cfg = accounts_cfg.get("tiers", {})
        bots_cfg = network.get("bots", {})

        enabled = routing_cfg.get("enabled", False)

        # Build per-session-type mapping for display
        session_type_map: dict[str, str] = {}
        for tier_name, tier_data in tiers_cfg.items():
            profiles = tier_data.get("profiles", [])
            for_types = tier_data.get("for_session_types", [])
            profile = profiles[0] if profiles else None
            for st in for_types:
                session_type_map[st] = profile or tier_name

        # Per-bot status: try oc_status for active profile info
        bot_statuses: dict[str, dict] = {}
        for bot_id in bots_cfg:
            bot_entry: dict = {
                "configured_tiers": _get_account_tiers_for_bot(bot_id, network),
            }
            try:
                from runtime.agent_runtime import get_runtime
                status = get_runtime().status(bot_id)
                if status:
                    bot_entry["profiles"] = status.get("auth_profiles", [])
                    bot_entry["active_profile"] = status.get("active_profile")
            except Exception:
                pass
            bot_statuses[bot_id] = bot_entry

        return jsonify({
            "routing_enabled": enabled,
            "tiers": tiers_cfg,
            "session_type_map": session_type_map,
            "bots": bot_statuses,
        })


# ── ocadmin integration helpers ───────────────────────────────────────────────

import json as _json
import subprocess as _subprocess
import os as _os
from datetime import datetime as _datetime, timezone as _timezone


def _call_ocadmin(args: list, stdin_data: str | None = None) -> dict:
    """DEPRECATED — no longer called. Kept for reference only.

    All model, key, gateway, and usage operations have been replaced with
    Evolve-native functions in oc_cli.py / oc_model.py / oc_keys.py.
    This function can be removed once confirmed no callers remain.
    """
    cmd = ["sudo", "python3", "/Users/Shared/openclaw-admin.py", "--json"] + args
    result = _subprocess.run(cmd, capture_output=True, text=True,
                             input=stdin_data, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ocadmin error: {result.stderr.strip()}")
    stdout = result.stdout.strip()
    # Try clean parse first; if it fails, strip any diagnostic preamble lines
    # and find the start of the JSON object/array.
    try:
        return _json.loads(stdout)
    except _json.JSONDecodeError:
        lines = stdout.splitlines()
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    return _json.loads("\n".join(lines[i:]))
                except _json.JSONDecodeError:
                    pass
        raise RuntimeError(f"ocadmin returned non-JSON output: {stdout[:300]}")


# ── Runtime mirror registry ───────────────────────────────────────────────────
# Maps each provider's auth-profiles field to its runtime location in
# openclaw.json. auth-profiles.json is the canonical credential store; this
# registry tells `_mirror_to_openclaw()` how to project a rotated value into
# the runtime config that gateways actually read at startup.
#
# Shape: provider → list[(target_filename, json_path, field_key)]
#   - target_filename: currently always "openclaw.json"
#   - json_path: deep-set path inside the JSON dict
#   - field_key: which auth-profiles field maps to this location
#               (must match a key in _PROVIDER_META.fields[].key for token_pair,
#                or the literal "api_key" for api_key providers)
#
# GitHub is intentionally absent — its dedicated rotate route handles its own
# mirroring + .git/config rewrite.
_RUNTIME_MIRROR_PATH: dict[str, list[tuple[str, list[str], str]]] = {
    "telegram": [("openclaw.json", ["channels", "telegram", "botToken"], "bot_token")],
    "slack":    [("openclaw.json", ["channels", "slack", "botToken"],    "bot_token")],
    "brave":    [("openclaw.json", ["plugins", "entries", "brave", "config", "webSearch", "apiKey"], "api_key")],
    # Discord intentionally not mirrored here — its credential lives entirely
    # in openclaw.json (channels.discord.token), so it has its own dedicated
    # rotate route at /api/admin/integration-token/<bot>/discord/rotate that
    # writes openclaw.json directly. The earlier botToken mapping was a bug —
    # openclaw's strict schema rejects that key for discord.
}


def _apply_credential_to_oc_dict(
    oc_dict: dict, provider: str, field_key: str, value: str,
) -> bool:
    """Deep-set the credential `value` into `oc_dict` per `_RUNTIME_MIRROR_PATH`.

    Mutates `oc_dict` in place. Returns True if the registry had a matching
    (provider, field_key) entry and the value was applied; False otherwise.
    Idempotent — calling twice with the same value leaves the dict identical.
    """
    entries = _RUNTIME_MIRROR_PATH.get(provider, [])
    applied = False
    for _target, json_path, registry_field in entries:
        if registry_field != field_key:
            continue
        cursor = oc_dict
        for segment in json_path[:-1]:
            nxt = cursor.get(segment)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[segment] = nxt
            cursor = nxt
        cursor[json_path[-1]] = value
        applied = True
    return applied


def _oc_keys_for_storage(
    storage: str, provider: str, *, mirrored: bool = False,
) -> set[str]:
    """Return the top-level openclaw.json keys mutated by a credential write.

    Companion to ``_audit_log_entry``'s ``oc_keys`` kwarg: each credential
    write path knows its ``storage`` + ``provider`` already, so the right
    set can be derived without each call site repeating the mapping.

      - ``"openclaw_channels"`` → ``{"channels"}`` (e.g. Telegram bot_token,
        Slack bot_token, the direct Discord rotate path).
      - ``"openclaw_plugins"``  → ``{"plugins"}`` (Brave api_key writes that
        land at ``plugins.entries.brave.config.webSearch.apiKey``).
      - ``"dotenv"``            → empty set (the value lives in
        ``~/.openclaw/workspace/.env``, not in openclaw.json).
      - ``"auth_profiles"``     → empty set when ``mirrored=False`` (only
        auth-profiles.json changed). When ``mirrored=True``, derived from
        the first path segment of every ``_RUNTIME_MIRROR_PATH[provider]``
        entry — typically ``{"channels"}`` for token_pair providers and
        ``{"plugins"}`` for brave's api_key.

    Callers that pass ``storage="auth_profiles"`` MUST gate ``mirrored`` on
    the actual mirror outcome (``_mirror_to_openclaw`` returns ``True`` even
    when the provider has no registry entry); otherwise heal would credit
    drift the writer didn't cause.
    """
    if storage == "openclaw_channels":
        return {"channels"}
    if storage == "openclaw_plugins":
        return {"plugins"}
    if storage == "auth_profiles" and mirrored:
        keys: set[str] = set()
        for _target, json_path, _field in _RUNTIME_MIRROR_PATH.get(provider, []):
            if json_path:
                keys.add(json_path[0])
        return keys
    return set()


# In-memory nonce store for the github onboarding discover endpoint. Maps
# nonce → (token, login, source_bot, expires_at_epoch). The browser only
# ever sees the nonce; the actual PAT stays here until redeemed by the
# verify or onboard endpoint (or expires). TTL is configurable via
# network.json → onboardingNonceTTLSeconds (default 600s = 10 min).
import threading as _threading_for_nonce
_DISCOVERED_PAT_NONCES: dict[str, tuple[str, str, str, float]] = {}
_DISCOVERED_PAT_LOCK = _threading_for_nonce.Lock()


def _store_discovered_pat_nonce(token: str, login: str, source_bot: str, ttl_seconds: int) -> str:
    """Store a discovered PAT under a fresh nonce; return the nonce."""
    import secrets, time
    nonce = secrets.token_urlsafe(24)
    expires_at = time.time() + max(int(ttl_seconds), 1)
    with _DISCOVERED_PAT_LOCK:
        _DISCOVERED_PAT_NONCES[nonce] = (token, login, source_bot, expires_at)
        # Opportunistic GC of expired entries (cheap; the dict stays small).
        now = time.time()
        for k in [k for k, v in _DISCOVERED_PAT_NONCES.items() if v[3] < now]:
            del _DISCOVERED_PAT_NONCES[k]
    return nonce


def _redeem_discovered_pat_nonce(nonce: str) -> tuple[str, str, str] | None:
    """Look up a nonce. Returns (token, login, source_bot) or None if expired/unknown."""
    import time
    with _DISCOVERED_PAT_LOCK:
        entry = _DISCOVERED_PAT_NONCES.get(nonce)
        if not entry:
            return None
        token, login, source_bot, expires_at = entry
        if expires_at < time.time():
            del _DISCOVERED_PAT_NONCES[nonce]
            return None
    return token, login, source_bot


def _resolve_credential(token_or_nonce: str | None) -> tuple[str | None, str | None, str | None]:
    """Resolve a `token_or_nonce` field into a real (token, login_hint, source_bot).

    If the input looks like a discovered-PAT nonce (no `ghp_`/`github_pat_` prefix
    and resolves in the nonce store), return the underlying token plus the
    login/source_bot it was discovered from. Otherwise treat the input as a
    raw PAT and return (token, None, None).

    Returns (None, None, None) for empty input or expired nonces.
    """
    if not token_or_nonce:
        return None, None, None
    s = token_or_nonce.strip()
    if not s:
        return None, None, None
    # Heuristic: github PATs always start with a known prefix; nonces don't.
    if s.startswith(("ghp_", "github_pat_", "ghs_")):
        return s, None, None
    redeemed = _redeem_discovered_pat_nonce(s)
    if redeemed is None:
        return None, None, None
    token, login, source_bot = redeemed
    return token, login, source_bot


def _ensure_brave_wired_in_dict(oc_dict: dict) -> dict:
    """Idempotently scaffold the brave plugin in an openclaw.json dict.

    Mutates `oc_dict` in place. Always ensures
    `plugins.entries.brave.config.webSearch` exists (so an api key has
    somewhere to land). Sets `tools.web.search.provider = "brave"` ONLY when
    the field is currently null/missing or already `"brave"` — any other
    value (e.g. `"tavily"`) is preserved per the v3 design decision: opt-out
    via `tools.web.search.provider` is a deliberate choice, not drift.

    Returns metadata about what changed:
      {
        "scaffolded": bool,            # plugins.entries.brave was created
        "provider_overridden": bool,   # provider was non-null and not "brave"
        "current_provider": str | None # provider value AFTER this call
      }
    """
    plugins = oc_dict.setdefault("plugins", {})
    entries = plugins.setdefault("entries", {})
    brave_entry = entries.setdefault("brave", {})
    brave_cfg = brave_entry.setdefault("config", {})
    brave_cfg.setdefault("webSearch", {})
    scaffolded = brave_entry.get("config") is brave_cfg and not brave_cfg["webSearch"]
    # The setdefault above creates the dict — we can't easily detect
    # "did we just create it?" reliably without snapshotting the prior state,
    # so report scaffolded=False; the test exercises the field shape directly.
    scaffolded = False  # opaque to callers; the visible result is the field shape

    tools = oc_dict.setdefault("tools", {})
    web = tools.setdefault("web", {})
    search = web.setdefault("search", {})
    current = search.get("provider")
    if current in (None, "", "brave"):
        search["provider"] = "brave"
        return {
            "scaffolded": scaffolded,
            "provider_overridden": False,
            "current_provider": "brave",
        }
    return {
        "scaffolded": scaffolded,
        "provider_overridden": True,
        "current_provider": current,
    }


# ── Google Workspace OAuth: scope registry, state store, token endpoints ─────
#
# The dashboard's wizard lets the operator pick services; each service maps to
# one or more Google API scopes. `default_on` controls which checkboxes are
# checked when the wizard opens; `restricted` flags scopes that require app
# verification on personal Google accounts (Workspace admins can whitelist
# them domain-wide).
#
# Trust-chain rule (added 2026-05-12 after A3 review found scope-vs-panel
# drift): only **read-only** services are `default_on: True`. Write-capable
# variants (`gmail` = send+readonly, `calendar` = full r/w, `drive*` = file
# write, `docs`, `sheets`, `slides`) must be explicit opt-in so the friendly
# "Add Gmail/Calendar" panel's "Won't send email / Won't modify calendar"
# promises remain truthful for the default install. The GOG skill install
# flow requests `gmail_readonly` + `calendar_readonly`; the wizard's full
# checkbox list lets advanced users opt into write capabilities, but only
# after un-collapsing the "Advanced / write access" section.
_GOOGLE_SCOPE_REGISTRY: dict[str, dict] = {
    # ── Read-only services (safe defaults; pre-checked in the wizard) ─────
    "gmail_readonly": {
        "label": "Gmail (read-only)",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        "default_on": True,
        "restricted": False,
        "advanced": False,
    },
    "calendar_readonly": {
        "label": "Calendar (read-only)",
        "scopes": ["https://www.googleapis.com/auth/calendar.readonly"],
        "default_on": True,
        "restricted": False,
        "advanced": False,
    },
    # Drive / Sheets / Docs readonly variants — added 2026-06-04 as part
    # of the Google Workspace skill suite (spec at
    # docs/spec-google-workspace-suite-2026-06-04.md §2.1). The Read skill
    # (google_workspace_read) requests these by default; they're not
    # ``default_on: True`` to preserve the legacy `gog` wizard's
    # checkbox-state contract — `gog` continues to pre-check only the
    # original two readonly services.
    "drive_readonly": {
        "label": "Drive (read-only)",
        "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
        "default_on": False,
        "restricted": False,
        "advanced": False,
    },
    "sheets_readonly": {
        "label": "Sheets (read-only)",
        "scopes": ["https://www.googleapis.com/auth/spreadsheets.readonly"],
        "default_on": False,
        "restricted": False,
        "advanced": False,
    },
    "docs_readonly": {
        "label": "Docs (read-only)",
        "scopes": ["https://www.googleapis.com/auth/documents.readonly"],
        "default_on": False,
        "restricted": False,
        "advanced": False,
    },
    # ── Write-capable variants (explicit opt-in; advanced section) ────────
    # These were `default_on: True` before A3 review caught that the
    # friendly panel promised the opposite. Keep them available for users
    # who genuinely need send/modify, but never pre-check them.
    "gmail": {
        "label": "Gmail (send + read)",
        "scopes": [
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.readonly",
        ],
        "default_on": False,
        "restricted": False,
        "advanced": True,
    },
    "calendar": {
        "label": "Calendar (read + write)",
        "scopes": ["https://www.googleapis.com/auth/calendar"],
        "default_on": False,
        "restricted": False,
        "advanced": True,
    },
    "drive": {
        "label": "Drive (per-file write)",
        "scopes": ["https://www.googleapis.com/auth/drive.file"],
        "default_on": False,
        "restricted": False,
        "advanced": True,
    },
    "docs": {
        "label": "Docs",
        "scopes": ["https://www.googleapis.com/auth/documents"],
        "default_on": False,
        "restricted": False,
        "advanced": True,
    },
    "sheets": {
        "label": "Sheets",
        "scopes": ["https://www.googleapis.com/auth/spreadsheets"],
        "default_on": False,
        "restricted": False,
        "advanced": True,
    },
    "slides": {
        "label": "Slides",
        "scopes": ["https://www.googleapis.com/auth/presentations"],
        "default_on": False,
        "restricted": False,
        "advanced": True,
    },
    # Advanced / restricted scopes — opt-in via wizard's "Advanced scopes"
    # collapsible. These hit Google's "restricted scopes" verification track
    # for personal accounts; Workspace admins can pre-approve them.
    "gmail_modify": {
        "label": "Gmail (full mailbox)",
        "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
        "default_on": False,
        "restricted": True,
        "advanced": True,
    },
    "drive_full": {
        "label": "Drive (all files)",
        "scopes": ["https://www.googleapis.com/auth/drive"],
        "default_on": False,
        "restricted": True,
        "advanced": True,
    },
}

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_OAUTH_BASE_SCOPES = ["openid", "email", "profile"]
GOOGLE_OAUTH_STATE_TTL_S = 600
GOOGLE_HTTP_TIMEOUT_S = 10

# Disk-backed state-token store for the OAuth flow. Maps state →
# JSON file at {shared_dir}/oauth_state/<state>.json with the
# pending request (bot_id, services, redirect_uri, expires_at) and,
# after callback, the result (status: pending|success|denied|error).
# State is short-lived (10 min). Persistence to disk (was: in-memory)
# survives admin-server restart — without it, a redeploy or daemon
# bounce mid-OAuth-flow stranded the user on the "Unknown or expired
# state" page (Bug 5 from the 2026-05-15 setup-google test session).
#
# Concurrency: admin server is single-instance, so each state token
# is written/updated/consumed by at most one request. Atomic writes
# via temp + os.replace handle any incidental races.
GOOGLE_OAUTH_STATE_DIRNAME = "oauth_state"


def _oauth_state_dir() -> Path:
    """Return ``{shared_dir}/oauth_state/`` — the canonical Evolve-owned
    location for OAuth state files. Loads sharedDir from network.json
    on each call; cheap, and avoids capturing at import-time when
    network.json may not exist yet (e.g., fresh-install path)."""
    try:
        net = load_network(DEFAULT_NETWORK_CONFIG)
        sd = Path(net.get("sharedDir") or "/Users/Shared/evolve")
    except Exception:
        sd = Path("/Users/Shared/evolve")
    return sd / GOOGLE_OAUTH_STATE_DIRNAME


def _oauth_state_path(state: str) -> Path:
    """Path to one state token's JSON file. ``state`` is a urlsafe
    base64 token (no path separators); using it as a filename is safe."""
    return _oauth_state_dir() / f"{state}.json"


def _oauth_state_write(state: str, payload: dict) -> bool:
    """Write a state entry atomically (temp + os.replace). chmod 600
    best-effort — directory ownership is the real gate. Returns True
    on success."""
    import os as _os
    path = _oauth_state_path(state)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        return False
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        try:
            _os.chmod(tmp, 0o600)
        except OSError:
            pass
        _os.replace(tmp, path)
        return True
    except (PermissionError, OSError):
        return False


def _oauth_state_read(state: str) -> dict | None:
    """Read a state entry from disk. Returns the dict or None on missing /
    malformed."""
    path = _oauth_state_path(state)
    try:
        text = path.read_text()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _oauth_state_delete(state: str) -> dict | None:
    """Read + remove a state entry. Returns the prior contents (for
    consume callers that want the data on their way out)."""
    import os as _os
    prior = _oauth_state_read(state)
    path = _oauth_state_path(state)
    try:
        _os.unlink(path)
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return prior


def _oauth_state_gc() -> None:
    """Best-effort cleanup of expired state files. Walks the state
    directory and unlinks any whose ``expires_at`` is in the past.
    Called opportunistically from create — the cost is one directory
    scan, bounded by the 10-min TTL × the OAuth start rate (low)."""
    import os as _os
    import time
    now = time.time()
    state_dir = _oauth_state_dir()
    try:
        entries = list(state_dir.iterdir())
    except (FileNotFoundError, PermissionError, OSError):
        return
    for entry in entries:
        if not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            obj = json.loads(entry.read_text())
        except (ValueError, OSError):
            # Corrupt or unreadable — leave it alone, the operator
            # can clean up by hand if needed.
            continue
        exp = (obj or {}).get("expires_at", 0)
        if isinstance(exp, (int, float)) and exp < now:
            try:
                _os.unlink(entry)
            except (FileNotFoundError, PermissionError, OSError):
                pass


def _google_state_create(bot_id: str, services: list[str], scopes: list[str], redirect_uri: str) -> str:
    """Create a fresh OAuth state token, persist to disk, return the
    urlsafe token. Survives admin restart — admins can be redeployed
    mid-flow without stranding the user's pending consent click."""
    import secrets, time
    state = secrets.token_urlsafe(24)
    expires_at = time.time() + GOOGLE_OAUTH_STATE_TTL_S
    payload = {
        "bot_id": bot_id,
        "services": list(services),
        "scopes": list(scopes),
        "redirect_uri": redirect_uri,
        "expires_at": expires_at,
        "result": {"status": "pending"},
    }
    _oauth_state_write(state, payload)
    # Opportunistic GC — keeps the state directory bounded.
    _oauth_state_gc()
    return state


def _google_state_get(state: str) -> dict | None:
    """Look up a state entry without consuming it (for poll + callback).
    Returns None for unknown or expired state."""
    import time
    entry = _oauth_state_read(state)
    if not entry:
        return None
    if entry.get("expires_at", 0) < time.time():
        # Expired — delete the file so the next call agrees.
        _oauth_state_delete(state)
        return None
    return entry


def _google_state_set_result(state: str, result: dict) -> bool:
    """Update the result of a state entry (called by callback). Returns
    True if the state was found and updated, False otherwise."""
    import time
    entry = _oauth_state_read(state)
    if not entry or entry.get("expires_at", 0) < time.time():
        return False
    entry["result"] = result
    return _oauth_state_write(state, entry)


def _google_state_consume(state: str) -> dict | None:
    """Pop a state entry (called by /poll once result is success/denied/
    error so the same state can't be redeemed twice)."""
    return _oauth_state_delete(state)


def _google_http_form_post(url: str, fields: dict) -> tuple[int, dict | None]:
    """POST application/x-www-form-urlencoded to a Google OAuth endpoint.
    Returns (status, parsed_json) — parsed_json is None on network error or
    non-JSON body.
    """
    import urllib.request, urllib.error, urllib.parse
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=GOOGLE_HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, _json.loads(raw) if raw else {}
            except _json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
            return e.code, _json.loads(raw) if raw else None
        except Exception:
            return e.code, None
    except Exception:
        return 0, None


def _google_token_exchange(code: str, client_id: str, client_secret: str, redirect_uri: str) -> tuple[int, dict | None]:
    """Exchange an authorization code for {access_token, refresh_token, ...}."""
    return _google_http_form_post(GOOGLE_TOKEN_URL, {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })


def _google_token_refresh(refresh_token: str, client_id: str, client_secret: str) -> tuple[int, dict | None]:
    """Use a refresh token to mint a fresh access token."""
    return _google_http_form_post(GOOGLE_TOKEN_URL, {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
    })


def _google_token_revoke(token: str) -> tuple[int, dict | None]:
    """Revoke a refresh or access token at Google's revoke endpoint."""
    return _google_http_form_post(GOOGLE_REVOKE_URL, {"token": token})


def _google_userinfo(access_token: str) -> tuple[int, dict | None]:
    """GET userinfo with a fresh access token. Used to surface the Google
    account email after callback so the dashboard row shows who's connected.
    """
    import urllib.request, urllib.error
    req = urllib.request.Request(GOOGLE_USERINFO_URL)
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=GOOGLE_HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, _json.loads(raw) if raw else None
            except _json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
            return e.code, _json.loads(raw) if raw else None
        except Exception:
            return e.code, None
    except Exception:
        return 0, None


def _google_oauth_profile_id(bot_id: str) -> str:
    """Canonical auth-profiles.json key for a bot's Google OAuth profile."""
    return f"google_workspace_{bot_id}"


# Legacy on-disk credential layout written by `oc gws --reauth` (the
# pre-wizard CLI flow). Bots that were Google-connected before the
# dashboard wizard shipped have these three files under
# /Users/<bot>/.config/gws/. We probe for them so the keys API can
# surface the row as active even when auth-profiles.json has no
# google_workspace_<bot> profile yet.
_LEGACY_GWS_CONFIG_DIR = ".config/gws"
# Files we strictly require to call a bot legacy-Google-connected.
# token_cache.json is only written on a successful OAuth completion, so
# its presence is the strongest "this integration actually worked" signal;
# client_secret.json is the OAuth client config the CLI installed at setup.
# credentials.enc / credentials.json (encrypted local state) are version-
# dependent — older `@googleworkspace/cli` releases used .enc, newer
# releases ship .json — and we never read them, so we don't gate on them.
_LEGACY_GWS_FILES = ("client_secret.json", "token_cache.json")


def _remediation_hint_for(reason: str) -> str | None:
    """Map a probe error reason to a known remediation hint (Q5).

    Returns None for reasons we don't have a confident hint for; the
    dashboard shows the bare reason in that case rather than fabricating
    advice that might point operators in the wrong direction.

    Currently: only sudoers-grant misconfigs get a hint, since that's
    the team_bot_a-and-team_bot_c failure mode this surface was built for and the
    fix is well-defined (refresh-sudoers).
    """
    if not reason:
        return None
    lower = reason.lower()
    if (
        "permission denied" in lower
        or "operation not permitted" in lower
        or "not in the sudoers file" in lower
        or "a password is required" in lower
    ):
        return (
            "Likely a missing sudoers grant for the evolve user. "
            "Run `sudo evolve-admin install-infra-jobs` to refresh "
            "/etc/sudoers.d/evolve."
        )
    return None


def _classify_sudo_failure(returncode: int, stderr: str) -> tuple[str, str | None]:
    """Classify a non-zero `sudo /bin/cat` / `sudo /bin/ls` outcome.

    Returns (kind, reason) where kind is one of:
      - "ok":         returncode 0 (caller should not call this)
      - "missing":    file/directory genuinely doesn't exist (treat as NO_EVIDENCE)
      - "permission": sudoers grant or filesystem permission rejected the read
      - "other":      any other non-zero — surface as a warning so the operator
                      can investigate

    The Q5 failure mode (team_bot_a/team_bot_c hidden by silent helpers) is specifically
    the "permission" case: the file exists, the sudoers grant or ACL doesn't
    cover this caller, and we collapsed it to NO_EVIDENCE. Stderr from
    `sudo /bin/cat` / `/bin/ls` is the only signal we have to distinguish
    "doesn't exist" from "couldn't read"; we match on the BSD/Darwin error
    strings the production mini emits.

    `reason` is a short human-readable string suitable for the dashboard
    warning chip — None when kind == "missing" or "ok".
    """
    if returncode == 0:
        return "ok", None
    err = (stderr or "").strip()
    err_l = err.lower()
    # Genuine "this file/dir does not exist" — every macOS `cat`/`ls` we've
    # seen in production emits one of these phrasings.
    if (
        "no such file or directory" in err_l
        or "not a directory" in err_l
        or "no such device" in err_l
    ):
        return "missing", None
    # Permission-class signals — sudoers misconfig, ACL gap, or filesystem
    # mode rejecting the read. The remediation hint differs by class so we
    # surface the raw reason and let the renderer suggest the fix.
    if (
        "permission denied" in err_l
        or "operation not permitted" in err_l
        or "not in the sudoers file" in err_l
        or "a password is required" in err_l
    ):
        return "permission", err or "permission denied"
    return "other", err or f"sudo failed (rc={returncode})"


def _detect_legacy_gws(bot_id: str, errors_out: list[str] | None = None) -> dict:
    """Probe the legacy oc-gws on-disk credential layout for a bot.

    The bot's home is mode 700 and the evolve user's ACL only covers
    .openclaw/, so we go through `sudo /bin/cat` for everything under
    .config/gws/. The sudoers grant in setup_wizard._render_evolve_sudoers
    section 3a authorizes /bin/cat on these three exact paths. We use
    cat's returncode as the existence test (returncode 0 ⇒ file exists
    AND is readable as root).

    Returns a dict with keys: present (bool), google_account (str|None),
    token_age_days (float|None), scopes (list[str]).

    `errors_out` (Q5): when provided, classified read failures append to
    this list as plain strings ("Permission denied: <path>", "sudo cat
    timed out: <path>"). Genuine "no such file" errors do NOT append —
    they are the standard NO_EVIDENCE path. Probes pass an accumulator so
    the dashboard can distinguish "no integration configured" from
    "couldn't read the legacy CLI files."
    """
    import time as _time
    user = _resolve_bot_user(bot_id)
    cfg_dir = f"/Users/{user}/{_LEGACY_GWS_CONFIG_DIR}"

    def _read_via_sudo(p: str) -> str | None:
        # errors='replace' is critical: credentials.enc is encrypted
        # binary, and the default 'strict' decoder raises UnicodeDecodeError
        # on the first non-UTF-8 byte — silently torpedoing the existence
        # check for that file. We don't parse credentials.enc, only check
        # it exists; replacement chars are harmless.
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", p],
                capture_output=True, text=True, errors="replace", timeout=10,
            )
        except subprocess.TimeoutExpired:
            if errors_out is not None:
                errors_out.append(f"sudo cat timed out reading {p}")
            return None
        except Exception as exc:
            if errors_out is not None:
                errors_out.append(f"sudo cat failed for {p}: {exc}")
            return None
        if r.returncode == 0:
            return r.stdout
        kind, reason = _classify_sudo_failure(r.returncode, r.stderr)
        if kind != "missing" and errors_out is not None:
            errors_out.append(f"{reason}: {p}")
        return None

    contents: dict[str, str | None] = {f: _read_via_sudo(f"{cfg_dir}/{f}") for f in _LEGACY_GWS_FILES}
    if not all(contents.values()):
        return {"present": False, "google_account": None, "token_age_days": None, "scopes": []}

    scopes: list[str] = []
    token_age_days: float | None = None
    tc_text = contents["token_cache.json"]
    if tc_text:
        try:
            tc = json.loads(tc_text)
        except Exception as exc:
            tc = None
            if errors_out is not None:
                errors_out.append(
                    f"Malformed JSON at {cfg_dir}/token_cache.json: {exc}"
                )
        if tc is not None:
            try:
                sc = tc.get("scope") or (tc.get("token") or {}).get("scope") or tc.get("scopes")
                if isinstance(sc, str):
                    scopes = [s for s in sc.split() if s]
                elif isinstance(sc, list):
                    scopes = [s for s in sc if isinstance(s, str)]
                # Approximate "token age" from the cached expiry: googleworkspace/cli
                # writes ISO-8601 expiry for each refresh, which is ~1h after the
                # last refresh. expired_for_days = max(0, now - expiry); that's a
                # lower bound on token age. Useful enough for the migration nudge.
                expiry_raw = tc.get("expiry") or (tc.get("token") or {}).get("expiry")
                if expiry_raw:
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        exp = _dt.fromisoformat(str(expiry_raw).replace("Z", "+00:00"))
                        delta = _dt.now(_tz.utc).timestamp() - exp.timestamp()
                        token_age_days = round(max(0.0, delta) / 86400, 1)
                    except Exception:
                        pass
            except Exception:
                pass

    account: str | None = None
    try:
        net = load_network(DEFAULT_NETWORK_CONFIG)
        bots = net.get("bots") or {}
        account = (bots.get(bot_id) or {}).get("gws_account") or None
    except Exception:
        pass

    return {
        "present": True,
        "google_account": account,
        "token_age_days": token_age_days,
        "scopes": scopes,
    }


# Dropbox is integrated via the macOS desktop sync app, not via OAuth/API.
# A connected bot has the Dropbox app signed in under its user account; the
# desktop client writes ~/.dropbox/info.json with the sync-folder path and
# subscription metadata. We probe that file as the existence signal — there
# is no auth-profiles.json profile or openclaw.json plugin entry for Dropbox.
_DROPBOX_INFO_FILE = ".dropbox/info.json"


def _detect_dropbox_desktop(bot_id: str, errors_out: list[str] | None = None) -> dict:
    """Probe the Dropbox desktop client's info.json for a bot.

    The bot's home is mode 700 and the evolve user's ACL only covers
    .openclaw/, so we go through `sudo /bin/cat`. The sudoers grant in
    setup_wizard._render_evolve_sudoers section 3b authorizes /bin/cat
    on this exact path.

    info.json is small UTF-8 JSON of the form
    {"personal": {"path": "...", "host": <int>, "is_team": false,
                  "subscription_type": "Pro"}}
    or with a "business" key instead of "personal" for team accounts.

    Returns a dict with keys: present (bool), sync_path (str|None),
    subscription_type (str|None), is_team (bool), account_kind
    ("personal"|"business"|None), host_id (int|None).

    `errors_out` (Q5): when provided, classified read failures append to
    this list. Genuine "no such file" errors are silent (NO_EVIDENCE);
    permission/timeout/JSON-decode errors append so the dashboard can
    surface a warning chip.
    """
    user = _resolve_bot_user(bot_id)
    info_path = f"/Users/{user}/{_DROPBOX_INFO_FILE}"
    _absent = {"present": False, "sync_path": None, "subscription_type": None,
               "is_team": False, "account_kind": None, "host_id": None}

    try:
        # errors='replace' mirrors the legacy-gws helper — info.json is
        # plain JSON in practice, but defensively decoding keeps a stray
        # binary byte from torpedoing the existence check.
        r = subprocess.run(
            ["sudo", "/bin/cat", info_path],
            capture_output=True, text=True, errors="replace", timeout=10,
        )
    except subprocess.TimeoutExpired:
        if errors_out is not None:
            errors_out.append(f"sudo cat timed out reading {info_path}")
        return dict(_absent)
    except Exception as exc:
        if errors_out is not None:
            errors_out.append(f"sudo cat failed for {info_path}: {exc}")
        return dict(_absent)

    if r.returncode != 0:
        kind, reason = _classify_sudo_failure(r.returncode, r.stderr)
        if kind != "missing" and errors_out is not None:
            errors_out.append(f"{reason}: {info_path}")
        return dict(_absent)
    if not r.stdout:
        return dict(_absent)

    try:
        info = json.loads(r.stdout)
    except Exception as exc:
        if errors_out is not None:
            errors_out.append(f"Malformed JSON at {info_path}: {exc}")
        return dict(_absent)

    # Pick whichever account block is present; "business" wins if both exist.
    block = info.get("business") or info.get("personal") or {}
    kind = "business" if "business" in info else ("personal" if "personal" in info else None)
    if not block:
        return {"present": False, "sync_path": None, "subscription_type": None,
                "is_team": False, "account_kind": None, "host_id": None}

    return {
        "present": True,
        "sync_path": block.get("path") or None,
        "subscription_type": block.get("subscription_type") or None,
        "is_team": bool(block.get("is_team")),
        "account_kind": kind,
        "host_id": block.get("host"),
    }


# ── Phase 2 integration discovery probes (workspace credentials, dotenv,
# system-level GitHub auth). Each helper does the I/O — sudo /bin/cat or
# /bin/ls under the bot's home — and returns plain data. The probes in
# web/probes/__init__.py interpret the result. Sudoers grants for these
# paths are in setup_wizard._render_evolve_sudoers section 3c-3f.

# File-shape recognizers used by _list_workspace_credentials. The classifier
# decides which auth model a credential file represents based on the keys
# it carries, not its filename — older `@googleworkspace/cli` releases
# used `credentials.enc`, newer ones ship `credentials.json` (see #715).
def _classify_workspace_credential_json(data: dict) -> tuple[str, str | None]:
    """Return (kind, account|None) for a parsed JSON credential file.

    Recognizers, in priority order:
      - service_account: top-level "type": "service_account" + "client_email"
      - oauth_client_secret: top-level "installed" or "web" key
      - oauth_token_cache: refresh_token / access_token / expiry shape
      - unknown: anything else (caller filters these out)
    """
    if not isinstance(data, dict):
        return "unknown", None
    if data.get("type") == "service_account" and data.get("client_email"):
        return "service_account", data.get("client_email") or None
    if isinstance(data.get("installed"), dict) or isinstance(data.get("web"), dict):
        return "oauth_client_secret", None
    has_refresh = bool(data.get("refresh_token") or data.get("token", {}).get("refresh_token"))
    has_access = bool(data.get("access_token") or data.get("token", {}).get("access_token"))
    has_expiry = bool(data.get("expiry") or data.get("token", {}).get("expiry"))
    if has_refresh or (has_access and has_expiry):
        # account email lives in different places depending on the writer;
        # try a few, return None if not found.
        account = (
            data.get("account")
            or data.get("email")
            or (data.get("id_token_claims") or {}).get("email")
            or (data.get("token") or {}).get("email")
        )
        return "oauth_token_cache", (account if isinstance(account, str) else None)
    return "unknown", None


def _list_workspace_credentials(
    bot_id: str,
    network_path: Path = DEFAULT_NETWORK_CONFIG,
    errors_out: list[str] | None = None,
) -> list[dict]:
    """Enumerate ~/.openclaw/workspace/credentials/*.json and classify each.

    Returns list of {path, kind, account|None} dicts; "unknown"-kind files
    are filtered out so the probe sees only credentials it recognizes.
    Sudoers grant: section 3c (`/bin/ls` + `/bin/cat`).

    `errors_out` (Q5): when provided, classified read failures append.
    Missing credentials directory is NOT an error (genuine NO_EVIDENCE);
    only permission denials, timeouts, and per-file read/JSON failures
    surface as warnings.
    """
    user = _resolve_bot_user(bot_id, network_path)
    cred_dir = f"/Users/{user}/.openclaw/workspace/credentials"
    try:
        ls = subprocess.run(
            ["sudo", "/bin/ls", cred_dir],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        if errors_out is not None:
            errors_out.append(f"sudo ls timed out reading {cred_dir}")
        return []
    except Exception as exc:
        if errors_out is not None:
            errors_out.append(f"sudo ls failed for {cred_dir}: {exc}")
        return []
    if ls.returncode != 0:
        kind, reason = _classify_sudo_failure(ls.returncode, ls.stderr)
        if kind != "missing" and errors_out is not None:
            errors_out.append(f"{reason}: {cred_dir}")
        return []
    files: list[dict] = []
    for name in (ls.stdout or "").split():
        name = name.strip()
        if not name.endswith(".json"):
            continue
        path = f"{cred_dir}/{name}"
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", path],
                capture_output=True, text=True, errors="replace", timeout=5,
            )
        except subprocess.TimeoutExpired:
            if errors_out is not None:
                errors_out.append(f"sudo cat timed out reading {path}")
            continue
        except Exception as exc:
            if errors_out is not None:
                errors_out.append(f"sudo cat failed for {path}: {exc}")
            continue
        if r.returncode != 0:
            kind, reason = _classify_sudo_failure(r.returncode, r.stderr)
            if kind != "missing" and errors_out is not None:
                errors_out.append(f"{reason}: {path}")
            continue
        if not r.stdout:
            continue
        try:
            data = _json.loads(r.stdout)
        except Exception as exc:
            if errors_out is not None:
                errors_out.append(f"Malformed JSON at {path}: {exc}")
            continue
        kind, account = _classify_workspace_credential_json(data)
        if kind == "unknown":
            continue
        files.append({"path": path, "kind": kind, "account": account})
    return files


def _detect_workspace_dotenv_keys(
    bot_id: str,
    env_var_names: tuple[str, ...],
    network_path: Path = DEFAULT_NETWORK_CONFIG,
    errors_out: list[str] | None = None,
) -> list[str]:
    """Extract the *names* of provided env vars present-with-value in
    ~/.openclaw/workspace/.env. Values are NEVER returned (privacy: the
    .env can hold unrelated secrets like database passwords).

    Sudoers grant: section 3d (`/bin/cat`).

    `errors_out` (Q5): classified read failures append. Missing .env is
    NO_EVIDENCE (no append); permission/timeout errors append.
    """
    if not env_var_names:
        return []
    user = _resolve_bot_user(bot_id, network_path)
    env_path = f"/Users/{user}/.openclaw/workspace/.env"
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", env_path],
            capture_output=True, text=True, errors="replace", timeout=5,
        )
    except subprocess.TimeoutExpired:
        if errors_out is not None:
            errors_out.append(f"sudo cat timed out reading {env_path}")
        return []
    except Exception as exc:
        if errors_out is not None:
            errors_out.append(f"sudo cat failed for {env_path}: {exc}")
        return []
    if r.returncode != 0:
        kind, reason = _classify_sudo_failure(r.returncode, r.stderr)
        if kind != "missing" and errors_out is not None:
            errors_out.append(f"{reason}: {env_path}")
        return []
    if not r.stdout:
        return []
    matched: list[str] = []
    wanted = set(env_var_names)
    for raw in r.stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        # Strip optional `export ` prefix; split on first `=` only.
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, _, value = line.partition("=")
        name = name.strip()
        if name not in wanted:
            continue
        # Discard surrounding quotes when checking for non-empty value.
        v = value.strip().strip('"').strip("'")
        if v and name not in matched:
            matched.append(name)
    return matched


def _rewrite_workspace_dotenv_value(
    existing_text: str, env_var_name: str, new_value: str,
) -> tuple[str | None, str | None]:
    """Pure rewrite — produce a new .env body where the line assigning
    `env_var_name` is replaced with the new value, preserving every other
    line (including comments, blank lines, and unrelated assignments).

    Returns (new_text, error_or_None). Constraints:
      - Idempotent: rewriting to the same value yields identical output.
      - Preserves a leading `export ` prefix on the assignment.
      - Preserves the original quoting style (single, double, none).
      - Never appends a missing line — if the var isn't present, returns
        an error so the caller can refuse to write (rotation here means
        rewriting an existing assignment, not invented one).
      - Refuses to match a commented-out line.

    The new_value is inserted verbatim inside the existing quotes; callers
    must ensure it is a credential-shaped string with no embedded newlines
    or quote characters that would invalidate the surrounding syntax.
    """
    if not env_var_name:
        return None, "env_var_name required"
    if "\n" in new_value or "\r" in new_value:
        return None, "value must not contain newline characters"

    out_lines: list[str] = []
    matched = False
    for raw in existing_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out_lines.append(raw)
            continue
        # Detect the leading whitespace + optional `export ` prefix.
        leading_ws_len = len(raw) - len(raw.lstrip())
        leading_ws = raw[:leading_ws_len]
        body = raw[leading_ws_len:]
        prefix = ""
        if body.startswith("export "):
            prefix = "export "
            body = body[len("export "):].lstrip()
        name, sep, value = body.partition("=")
        if not sep or name.strip() != env_var_name:
            out_lines.append(raw)
            continue
        # Preserve the original quote style if any.
        v = value
        # Trim trailing inline comment (after first unquoted #) — left in
        # place; callers should not rely on inline-comment preservation.
        quote = ""
        v_stripped = v.strip()
        if v_stripped.startswith('"') and v_stripped.endswith('"') and len(v_stripped) >= 2:
            quote = '"'
        elif v_stripped.startswith("'") and v_stripped.endswith("'") and len(v_stripped) >= 2:
            quote = "'"
        new_assignment = f"{name.strip()}={quote}{new_value}{quote}"
        out_lines.append(f"{leading_ws}{prefix}{new_assignment}")
        matched = True

    if not matched:
        return None, f"env var {env_var_name} not found in .env"

    # Preserve trailing newline if the original had one.
    new_text = "\n".join(out_lines)
    if existing_text.endswith("\n"):
        new_text += "\n"
    return new_text, None


def _write_workspace_dotenv_value(
    bot_id: str,
    env_var_name: str,
    new_value: str,
    network_path: Path = DEFAULT_NETWORK_CONFIG,
) -> tuple[bool, str | None]:
    """Rewrite a single env-var assignment in `~/.openclaw/workspace/.env`.

    Read-rewrite-write cycle:
      1. Read the existing .env via `sudo /bin/cat`.
      2. Pure-rewrite via `_rewrite_workspace_dotenv_value` (preserves
         every other line — comments, blank lines, unrelated secrets).
      3. Stage to /tmp and `sudo /bin/cp` to the destination (sudoers
         section 3d).

    Returns (ok, error_or_None). The caller's audit-log entry captures
    only the env-var name and storage tag — never the value.
    """
    import os as _os, tempfile as _tempfile
    user = _resolve_bot_user(bot_id, network_path)
    env_path = f"/Users/{user}/.openclaw/workspace/.env"
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", env_path],
            capture_output=True, text=True, errors="replace", timeout=5,
        )
    except Exception as exc:
        return False, f"failed to read .env: {exc}"
    if r.returncode != 0:
        return False, f"failed to read .env (rc={r.returncode}): {r.stderr.strip() or 'no stderr'}"
    existing = r.stdout or ""

    new_text, rewrite_err = _rewrite_workspace_dotenv_value(
        existing, env_var_name, new_value,
    )
    if rewrite_err or new_text is None:
        return False, rewrite_err or "rewrite failed"

    fd, tmp = _tempfile.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-dotenv-", suffix=".env")
    try:
        with _os.fdopen(fd, "w") as f:
            f.write(new_text)
        _os.chmod(tmp, 0o644)
        cp = subprocess.run(
            ["sudo", "/bin/cp", tmp, env_path],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode != 0:
            return False, f"sudo cp failed: {cp.stderr.strip() or 'no stderr'}"
    finally:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
    return True, None


def _list_workspace_manifest_files(
    bot_id: str,
    network_path: Path = DEFAULT_NETWORK_CONFIG,
    errors_out: list[str] | None = None,
) -> list[str]:
    """List basenames in ~/.openclaw/workspace/manifests/. Empty list when
    the directory is missing or unreadable.

    Manifests are evidence chips, not status drivers (per design doc Q2):
    they prove the bot's runtime expects the integration to work, but
    don't carry credentials. Sudoers grant: section 3e (`/bin/ls`).

    `errors_out` (Q5): classified read failures append.
    """
    user = _resolve_bot_user(bot_id, network_path)
    manifest_dir = f"/Users/{user}/.openclaw/workspace/manifests"
    try:
        r = subprocess.run(
            ["sudo", "/bin/ls", manifest_dir],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        if errors_out is not None:
            errors_out.append(f"sudo ls timed out reading {manifest_dir}")
        return []
    except Exception as exc:
        if errors_out is not None:
            errors_out.append(f"sudo ls failed for {manifest_dir}: {exc}")
        return []
    if r.returncode != 0:
        kind, reason = _classify_sudo_failure(r.returncode, r.stderr)
        if kind != "missing" and errors_out is not None:
            errors_out.append(f"{reason}: {manifest_dir}")
        return []
    return [n.strip() for n in (r.stdout or "").split() if n.strip()]


def _list_user_ssh_private_keys(
    bot_id: str,
    network_path: Path = DEFAULT_NETWORK_CONFIG,
    errors_out: list[str] | None = None,
) -> list[str]:
    """List private ssh keys under /Users/<bot>/.ssh/ — `id_*` filenames,
    excluding `.pub` and `known_hosts`. Returns full paths.

    The bot's ~/.ssh is mode 700, so we shell out via sudo /bin/ls. We
    don't read the keys themselves — only their existence is evidence.
    Sudoers grant: section 3f (`/bin/ls`).

    `errors_out` (Q5): classified read failures append.
    """
    user = _resolve_bot_user(bot_id, network_path)
    ssh_dir = f"/Users/{user}/.ssh"
    try:
        r = subprocess.run(
            ["sudo", "/bin/ls", ssh_dir],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        if errors_out is not None:
            errors_out.append(f"sudo ls timed out reading {ssh_dir}")
        return []
    except Exception as exc:
        if errors_out is not None:
            errors_out.append(f"sudo ls failed for {ssh_dir}: {exc}")
        return []
    if r.returncode != 0:
        kind, reason = _classify_sudo_failure(r.returncode, r.stderr)
        if kind != "missing" and errors_out is not None:
            errors_out.append(f"{reason}: {ssh_dir}")
        return []
    keys: list[str] = []
    for name in (r.stdout or "").split():
        name = name.strip()
        if not name.startswith("id_"):
            continue
        if name.endswith(".pub"):
            continue
        if name in ("known_hosts", "config", "authorized_keys"):
            continue
        keys.append(f"{ssh_dir}/{name}")
    return keys


def _read_gh_cli_hosts(
    bot_id: str,
    network_path: Path = DEFAULT_NETWORK_CONFIG,
    errors_out: list[str] | None = None,
) -> list[str] | None:
    """Parse ~/.config/gh/hosts.yml and return the host keys (e.g.
    ['github.com']). Returns None when the file is absent / unreadable.

    Tiny YAML — to avoid pulling in a YAML dep just for this, we do
    minimal line-based parsing matching the gh CLI's actual hosts.yml
    layout (top-level mapping of host → user-block). Sudoers grant:
    section 3f (`/bin/cat`).

    `errors_out` (Q5): classified read failures append.
    """
    user = _resolve_bot_user(bot_id, network_path)
    hosts_path = f"/Users/{user}/.config/gh/hosts.yml"
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", hosts_path],
            capture_output=True, text=True, errors="replace", timeout=5,
        )
    except subprocess.TimeoutExpired:
        if errors_out is not None:
            errors_out.append(f"sudo cat timed out reading {hosts_path}")
        return None
    except Exception as exc:
        if errors_out is not None:
            errors_out.append(f"sudo cat failed for {hosts_path}: {exc}")
        return None
    if r.returncode != 0:
        kind, reason = _classify_sudo_failure(r.returncode, r.stderr)
        if kind != "missing" and errors_out is not None:
            errors_out.append(f"{reason}: {hosts_path}")
        return None
    if not r.stdout:
        return None
    hosts: list[str] = []
    for raw in r.stdout.splitlines():
        if not raw or raw.startswith(("#", " ", "\t", "-")):
            continue
        if not raw.endswith(":"):
            continue
        candidate = raw[:-1].strip()
        # gh's hosts.yml uses `github.com:` / `enterprise.example.com:`
        # at the top level. Filter out anything that doesn't look like a
        # hostname.
        if "." in candidate or candidate.lower() == "github.com":
            hosts.append(candidate)
    return hosts or None


def _v2_probes_enabled(network: dict) -> bool:
    """Phase 2 integration-discovery probes — on by default; the network.json
    flag remains as a kill switch.

    Reads `integrations.discovery.v2` from network.json. The default is
    True (Phase 2 / 2.5 / 3 all shipped). Setting the flag to `false`
    explicitly disables the Phase 2 probes (WorkspaceCredentialsProbe,
    DotenvProbe, OpenclawChannelsTokenProbe, SshKeyProbe, GhCliProbe) so
    the dashboard renders the Phase 1.5 JSON shape — useful as an escape
    hatch if a future probe regresses an instance we don't have test
    coverage for. Omitting the key, or any non-False value, leaves the
    probes enabled.

    Documented in docs/configuration.md.
    """
    discovery = (network or {}).get("integrations", {}).get("discovery", {})
    if "v2" not in discovery:
        return True
    return bool(discovery.get("v2"))


# ── Module-level integration helpers (lifted out of _register_admin_routes
# closure so they can be unit-tested directly without spinning up a Flask
# test client). All accept `network_path` for parity with the Flask routes,
# defaulting to DEFAULT_NETWORK_CONFIG so production callers need not pass it. ─

def _mask_key(value: str) -> str:
    """Return first-8 + '...' + last-4 of a key value."""
    if not value or len(value) < 13:
        return value or "—"
    return value[:8] + "..." + value[-4:]


def _read_oc_json(bot_id: str, network_path: Path = DEFAULT_NETWORK_CONFIG) -> dict:
    """Read openclaw.json for bot_id using resolved path + sudo /bin/cat as root."""
    user = _resolve_bot_user(bot_id, network_path)
    paths = resolve_bot_paths(bot_id, user=user)
    oc_path = paths["oc_config"]
    try:
        return _json.loads(Path(oc_path).read_text())
    except Exception:
        pass
    try:
        proc = subprocess.run(
            ["sudo", "/bin/cat", oc_path],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return _json.loads(proc.stdout)
    except Exception:
        pass
    return {}


def _write_oc_json(bot_id: str, data: dict, network_path: Path = DEFAULT_NETWORK_CONFIG) -> bool:
    """Write openclaw.json for bot_id via /tmp staging + sudo /bin/cp as root."""
    import tempfile as _tmpmod, os as _os
    user = _resolve_bot_user(bot_id, network_path)
    paths = resolve_bot_paths(bot_id, user=user)
    oc_path = paths["oc_config"]
    content = _json.dumps(data, indent=2)
    fd, tmp = _tmpmod.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-", suffix=".json")
    try:
        with _os.fdopen(fd, "w") as f:
            f.write(content)
        r = subprocess.run(
            ["sudo", "/bin/cp", tmp, oc_path],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False
    finally:
        try:
            _os.unlink(tmp)
        except OSError:
            pass


def _discover_github_remote(
    bot_id: str,
    network_path: Path = DEFAULT_NETWORK_CONFIG,
    errors_out: list[str] | None = None,
) -> dict | None:
    """Discover the GitHub remote configuration for a bot's workspace.

    Returns one of:
      {"auth_type": "https_pat",        "token": <pat>, "repo_slug": "owner/repo"}
      {"auth_type": "https_credhelper", "repo_slug": "owner/repo"}
      {"auth_type": "ssh",              "ssh_key_path": <path or None>, "repo_slug": "owner/repo"}
      None — no github remote found in .git/config

    Three paths exist in the wild because backup auth diverges:
      - HTTPS-with-embedded-PAT: legacy/wizard-onboarded bots; rotate route
        edits this in-place
      - HTTPS-without-embedded-PAT: credentials live in a credential
        helper / keychain / env. Git itself authenticates; we just
        need to know the repo exists.
      - SSH (git@github.com:owner/repo) + deploy key at
        /Users/evolve/.ssh/evolve-backup-<bot>: the path documented by
        packages/analyzer/backup.py

    openclaw rejects 'integrations' as an unknown top-level key, so the
    canonical store is .git/config — not openclaw.json.
    """
    import re as _re
    user = _resolve_bot_user(bot_id, network_path)
    paths = resolve_bot_paths(bot_id, user=user)
    git_config_path = Path(paths["workspace"]) / ".git" / "config"
    _log.info("github_discover[%s]: checking %s", bot_id, git_config_path)
    cfg_text: str | None = None
    try:
        cfg_text = git_config_path.read_text()
    except FileNotFoundError:
        # Genuine "no .git/config" — NO_EVIDENCE, no warning. Common for
        # bots that haven't been backup-onboarded yet.
        return None
    except PermissionError:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(git_config_path)],
                capture_output=True, text=True, timeout=5,
            )
        except subprocess.TimeoutExpired:
            if errors_out is not None:
                errors_out.append(f"sudo cat timed out reading {git_config_path}")
            return None
        except Exception as _exc:
            if errors_out is not None:
                errors_out.append(f"sudo cat failed for {git_config_path}: {_exc}")
            return None
        if r.returncode == 0:
            cfg_text = r.stdout
        else:
            kind, reason = _classify_sudo_failure(r.returncode, r.stderr)
            _log.warning("github_discover[%s]: sudo cat failed: %s", bot_id, r.stderr.strip())
            if kind != "missing" and errors_out is not None:
                errors_out.append(f"{reason}: {git_config_path}")
    except Exception as _exc:
        if errors_out is not None:
            errors_out.append(f"read error for {git_config_path}: {_exc}")
        _log.warning("github_discover[%s]: read error: %s", bot_id, _exc)
    if not cfg_text:
        _log.warning("github_discover[%s]: could not read git config at %s", bot_id, git_config_path)
        return None

    # HTTPS — covers both embedded-PAT and plain forms in one match. The
    # leading `[user:]TOKEN@` group is optional; when present we treat it
    # as a PAT, when absent we treat the URL as credhelper-authenticated.
    m_https = _re.search(
        r"url\s*=\s*https://(?:(?:[^:@\s]*:)?([^@\s]+)@)?github\.com/([^\s]+?)(?:\.git)?\s*$",
        cfg_text, _re.MULTILINE,
    )
    if m_https:
        token = (m_https.group(1) or "").strip()
        repo_slug = m_https.group(2).strip()
        if repo_slug and token:
            return {"auth_type": "https_pat", "token": token, "repo_slug": repo_slug}
        if repo_slug:
            return {"auth_type": "https_credhelper", "repo_slug": repo_slug}

    # SSH form: git@github.com:owner/repo[.git]  (also ssh://git@github.com/owner/repo)
    m_ssh = _re.search(
        r"url\s*=\s*(?:ssh://)?git@github\.com[:/]([^\s]+?)(?:\.git)?\s*$",
        cfg_text, _re.MULTILINE,
    )
    if m_ssh:
        repo_slug = m_ssh.group(1).strip()
        if repo_slug:
            ssh_key = Path(f"/Users/evolve/.ssh/evolve-backup-{bot_id}")
            return {
                "auth_type": "ssh",
                "ssh_key_path": str(ssh_key) if ssh_key.exists() else None,
                "repo_slug": repo_slug,
            }

    _log.warning("github_discover[%s]: no github remote found in %s", bot_id, git_config_path)
    return None


# ── /api/admin/* routes (ocadmin integration) ─────────────────────────────────

def _register_admin_routes(app: Flask, network_path: Path) -> None:
    """Shim — body lives in routes_admin.py."""
    from .routes_admin import register_admin_routes
    return register_admin_routes(app, network_path)

def _register_service_routes(app: Flask) -> None:
    """Register health check, server service management, and tunnel wizard endpoints."""
    from .. import service as _svc
    from .. import tunnel as _tunnel

    @app.get("/api/health")
    def api_health() -> Response:
        """Health check — always 200. Used by frontend polling and external monitoring."""
        uptime = int(_time.time() - _START_TIME)
        try:
            from evolve_admin import __version__ as _ver
        except Exception:
            _ver = "unknown"
        return jsonify({"status": "ok", "uptime_seconds": uptime, "version": _ver})

    @app.get("/api/admin/service/status")
    def api_service_status() -> Response:
        """Return the launchd service status for the admin server."""
        return jsonify(_svc.status())

    @app.post("/api/admin/service/restart")
    def api_service_restart() -> Response:
        """Restart the admin service.

        If managed by launchd uses kickstart; otherwise falls back to
        os.execv so the server can restart itself without launchd.

        Returns 202 because the server is about to die and restart.
        The frontend should poll /api/health to detect recovery.
        """
        ok, msg = _svc.restart()
        return jsonify({"ok": ok, "message": msg}), 202

    @app.post("/api/admin/service/install")
    def api_service_install() -> Response:
        """Install the admin server as a launchd service."""
        body = request.get_json() or {}
        host = body.get("host", "127.0.0.1")
        port = int(body.get("port", 5050))
        try:
            ok, msg = _svc.install(host=host, port=port)
            if ok:
                return jsonify({"ok": True, "message": msg})
            return jsonify({"ok": False, "error": msg}), 400
        except Exception as e:
            return error_response(e, 400, ok=False)

    @app.post("/api/admin/service/uninstall")
    def api_service_uninstall() -> Response:
        """Uninstall the launchd service."""
        ok, msg = _svc.uninstall()
        return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)

    @app.get("/api/admin/service/logs")
    def api_service_logs() -> Response:
        """Return last N lines of the admin server log."""
        n = int(request.args.get("n", 100))
        lines = _svc.tail_logs(n)
        _sys_log = _svc._system_log_path()  # platform-derived (systemd on Linux)
        log_path = _svc.LOG_PATH if _svc.LOG_PATH.exists() else (
            _sys_log if _sys_log.exists() else _svc.LOG_PATH)
        return jsonify({"lines": lines, "log_path": str(log_path)})

    @app.post("/api/wizard/generate-artifacts")
    def api_wizard_generate_artifacts() -> Response:
        """Generate tunnel setup script content from the provided config."""
        body = request.get_json() or {}
        cfg: _tunnel.TunnelConfig = {
            "remote_host": body.get("remote_host", "mini"),
            "remote_user": body.get("remote_user", ""),
            "remote_port": int(body.get("remote_port", 5050)),
            "local_port": int(body.get("local_port", 5050)),
            "ssh_key": body.get("ssh_key", "~/.ssh/id_ed25519"),
        }
        setup = _tunnel.generate_setup_command(cfg)
        connect = _tunnel.generate_connect_command(cfg)
        browser = _tunnel.browser_shortcut_instructions(cfg["local_port"])
        import base64 as _b64
        return jsonify({
            "setup_command_b64": _b64.b64encode(setup.encode()).decode(),
            "connect_command_b64": _b64.b64encode(connect.encode()).decode(),
            "browser_instructions": browser,
        })

    @app.get("/api/wizard/download-setup")
    def api_wizard_download_setup() -> Response:
        """Download evolve-tunnel-setup.command as a file."""
        cfg: _tunnel.TunnelConfig = {
            "remote_host": request.args.get("remote_host", "mini"),
            "remote_user": request.args.get("remote_user", ""),
            "remote_port": int(request.args.get("remote_port", 5050)),
            "local_port": int(request.args.get("local_port", 5050)),
            "ssh_key": request.args.get("ssh_key", "~/.ssh/id_ed25519"),
        }
        content = _tunnel.generate_setup_command(cfg)
        return Response(
            content,
            mimetype="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="evolve-tunnel-setup.command"'},
        )

    @app.get("/api/wizard/download-connect")
    def api_wizard_download_connect() -> Response:
        """Download evolve-tunnel-connect.command as a file."""
        cfg: _tunnel.TunnelConfig = {
            "remote_host": request.args.get("remote_host", "mini"),
            "remote_user": request.args.get("remote_user", ""),
            "remote_port": int(request.args.get("remote_port", 5050)),
            "local_port": int(request.args.get("local_port", 5050)),
            "ssh_key": request.args.get("ssh_key", "~/.ssh/id_ed25519"),
        }
        content = _tunnel.generate_connect_command(cfg)
        return Response(
            content,
            mimetype="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="evolve-tunnel-connect.command"'},
        )


def _register_recovery_routes(app: Flask, network_path: Path) -> None:
    """Register panic-button + per-bot rollback endpoints (sprint pillar B2.e).

    Routes:
      GET  /api/recovery/status                  — pause-state + recent rollbacks
      POST /api/recovery/pause-all               — disable all gateways
      POST /api/recovery/resume-all              — re-enable all gateways
      GET  /api/recovery/rollback-points/<bot>   — list daily backup commits for a bot
      POST /api/recovery/rollback                — revert a bot's openclaw.json
      POST /api/recovery/reverse-rollback        — undo a previous rollback
      GET  /api/recovery/history                 — full rollback audit list

    All POSTs accept JSON bodies. ``dry_run: true`` returns the would-do
    result without touching anything mutable. ``initiated_by`` defaults
    to ``"web"`` so audit records distinguish UI clicks from CLI usage.
    """
    from .. import recovery as _recovery

    def _net() -> dict:
        return load_network(network_path)

    def _shared_dir() -> Path:
        return Path(load_network(network_path).get("sharedDir", "/Users/Shared/evolve"))

    @app.get("/api/recovery/status")
    def api_recovery_status() -> Response:
        try:
            return jsonify(_recovery.recovery_status(
                shared_dir=_shared_dir(), network=_net(),
            ))
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/recovery/pause-all")
    def api_recovery_pause_all() -> Response:
        body = request.get_json(silent=True) or {}
        reason = (body.get("reason") or "operator pause").strip() or "operator pause"
        initiated_by = (body.get("initiated_by") or "web").strip() or "web"
        dry_run = bool(body.get("dry_run", False))
        try:
            result = _recovery.pause_all(
                reason=reason, initiated_by=initiated_by,
                shared_dir=_shared_dir(), network=_net(),
                dry_run=dry_run,
            )
        except Exception as e:
            return error_response(e, ok=False)
        status = 200 if result.ok else 207  # 207 multi-status for partial success
        return jsonify(result.to_dict()), status

    @app.post("/api/recovery/resume-all")
    def api_recovery_resume_all() -> Response:
        body = request.get_json(silent=True) or {}
        initiated_by = (body.get("initiated_by") or "web").strip() or "web"
        dry_run = bool(body.get("dry_run", False))
        try:
            result = _recovery.resume_all(
                initiated_by=initiated_by,
                shared_dir=_shared_dir(), network=_net(),
                dry_run=dry_run,
            )
        except Exception as e:
            return error_response(e, ok=False)
        status = 200 if result.ok else 207
        return jsonify(result.to_dict()), status

    @app.get("/api/recovery/rollback-points/<bot_id>")
    def api_recovery_rollback_points(bot_id: str) -> Response:
        """List candidate rollback targets for a bot.

        Query params:
          - ``limit`` (default 14) — max rows returned
          - ``only_backup`` (default ``"true"``) — when ``"false"``, returns
            the full workspace git log (including intermediate commits like
            accept-drift baseline resets), not just the nightly backup
            commits. The Recovery UI uses ``true`` for the rollback
            dropdown (operators only want known-good daily snapshots) and
            ``false`` for the "Recent commits in this bot's workspace"
            history list (operators want full context).
        """
        try:
            limit = int(request.args.get("limit", "14"))
        except ValueError:
            limit = 14
        only_backup_arg = (request.args.get("only_backup", "true") or "").lower()
        only_backup = only_backup_arg not in ("0", "false", "no")
        net = _net()
        # Always look these up so the UI can render a specific empty-state
        # message when no rollback points exist (the difference between
        # "backup not configured", "workspace not yet a git repo", and
        # "configured but no backups yet" matters to the operator).
        bot_cfg = (net.get("bots") or {}).get(bot_id) or {}
        backup_url_configured = bool(bot_cfg.get("backupRepoUrl"))
        workspace = _recovery._bot_workspace(bot_id, net)
        workspace_is_git = _recovery._is_workspace_repo(workspace)
        try:
            points = _recovery.list_rollback_points(
                bot_id, network=net,
                limit=max(1, min(limit, 100)),
                only_backup=only_backup,
            )
        except Exception as e:
            return jsonify({
                "ok": False, "error": str(e), "bot_id": bot_id,
                "backup_url_configured": backup_url_configured,
                "workspace_is_git": workspace_is_git,
            }), 500
        return jsonify({
            "ok": True,
            "bot_id": bot_id,
            "points": [p.to_dict() for p in points],
            "backup_url_configured": backup_url_configured,
            "workspace_is_git": workspace_is_git,
            "only_backup": only_backup,
        })

    @app.post("/api/recovery/rollback")
    def api_recovery_rollback() -> Response:
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        target = (body.get("target") or "").strip()
        initiated_by = (body.get("initiated_by") or "web").strip() or "web"
        skip_restart = bool(body.get("skip_restart", False))
        dry_run = bool(body.get("dry_run", False))
        if not bot_id or not target:
            return jsonify({"ok": False, "error": "bot_id and target are required"}), 400
        try:
            result = _recovery.rollback_bot(
                bot_id=bot_id, target=target,
                network=_net(), shared_dir=_shared_dir(),
                initiated_by=initiated_by,
                skip_restart=skip_restart, dry_run=dry_run,
            )
        except Exception as e:
            return error_response(e, ok=False)
        status = 200 if result.ok else 422
        return jsonify(result.to_dict()), status

    @app.post("/api/recovery/reverse-rollback")
    def api_recovery_reverse_rollback() -> Response:
        body = request.get_json(silent=True) or {}
        rollback_id = (body.get("rollback_id") or "").strip()
        initiated_by = (body.get("initiated_by") or "web").strip() or "web"
        skip_restart = bool(body.get("skip_restart", False))
        dry_run = bool(body.get("dry_run", False))
        if not rollback_id:
            return jsonify({"ok": False, "error": "rollback_id is required"}), 400
        try:
            result = _recovery.reverse_rollback(
                rollback_id=rollback_id, network=_net(),
                shared_dir=_shared_dir(),
                initiated_by=initiated_by,
                skip_restart=skip_restart, dry_run=dry_run,
            )
        except Exception as e:
            return error_response(e, ok=False)
        status = 200 if result.ok else 422
        return jsonify(result.to_dict()), status

    @app.get("/api/recovery/history")
    def api_recovery_history() -> Response:
        bot_id = request.args.get("bot") or None
        try:
            limit = int(request.args.get("limit", "50"))
        except ValueError:
            limit = 50
        entries = _recovery.list_rollback_history(
            shared_dir=_shared_dir(), bot_id=bot_id, limit=max(1, min(limit, 500)),
        )
        return jsonify({"ok": True, "history": entries})

    # ── Pod-state commit endpoints (V2.4-2) ──────────────────────────────────

    @app.get("/api/recovery/recent-commits")
    def api_recovery_recent_commits() -> Response:
        """Return recent pod-state commits from the deploy checkout.

        Query params:
          days=7   — how many days back to look (default 7, max 90)
          limit=50 — max commits to return (default 50, max 200)

        Each commit includes a ``confirm_token`` — a single-use 30-second
        token the UI must send back to authorise a rollback to that sha.
        """
        try:
            days = max(1, min(int(request.args.get("days", "7")), 90))
        except ValueError:
            days = 7
        try:
            limit = max(1, min(int(request.args.get("limit", "50")), 200))
        except ValueError:
            limit = 50
        try:
            commits = _recovery.list_recent_pod_commits(
                days=days, limit=limit, shared_dir=_shared_dir(),
            )
        except Exception as e:
            return error_response(e, ok=False)
        return jsonify({
            "ok": True,
            "days": days,
            "commits": [c.to_dict() for c in commits],
        })

    @app.post("/api/recovery/rollback-pod")
    def api_recovery_rollback_pod() -> Response:
        """Revert the deploy checkout to a previous commit.

        Body: {commit_sha: str, confirm_token: str, dry_run?: bool}

        The confirm_token must be the one returned by GET /api/recovery/recent-commits
        for the requested sha — it is single-use and expires in 30 seconds.

        Safety: refuses if the deploy checkout has uncommitted changes.
        Audit log entry written on every attempt.
        After success, kickstarts the admin-ui daemon so the new code is live.
        """
        body = request.get_json(silent=True) or {}
        commit_sha = (body.get("commit_sha") or "").strip()
        confirm_token = (body.get("confirm_token") or "").strip()
        initiated_by = (body.get("initiated_by") or "web").strip() or "web"
        dry_run = bool(body.get("dry_run", False))
        if not commit_sha:
            return jsonify({"ok": False, "error": "commit_sha is required"}), 400
        if not confirm_token:
            return jsonify({"ok": False, "error": "confirm_token is required"}), 400
        try:
            result = _recovery.rollback_pod_state(
                commit_sha=commit_sha,
                confirm_token=confirm_token,
                shared_dir=_shared_dir(),
                initiated_by=initiated_by,
                dry_run=dry_run,
            )
        except Exception as e:
            return error_response(e, ok=False)
        status = 200 if result.ok else 422
        return jsonify(result.to_dict()), status

    @app.get("/api/recovery/pod-rollback-log")
    def api_recovery_pod_rollback_log() -> Response:
        """Return the pod-state rollback audit log, newest first."""
        try:
            limit = max(1, min(int(request.args.get("limit", "50")), 500))
        except ValueError:
            limit = 50
        entries = _recovery.list_pod_rollback_log(
            shared_dir=_shared_dir(), limit=limit,
        )
        return jsonify({"ok": True, "log": entries})


def _register_breaker_routes(app: Flask, network_path: Path) -> None:
    """Register /api/breakers routes for circuit-breaker UI surface (Phase 4b).

    Routes:
      GET  /api/breakers                — list active breakers + recent audit
      POST /api/breakers/trip           — trip a breaker (per-bot or pod-wide)
      POST /api/breakers/reset          — reset a breaker

    Spec: docs/spec-circuit-breakers-2026-05-21.md §3.1. Thin Flask layer
    over breakers.store + breakers_enforce — same primitives the CLI
    (#1399) and evo tools (#1406) use. All routes return JSON with an
    ``ok`` boolean; mutation routes mirror the {"trip": ..., "enforce":
    ...} shape from the CLI for consistency.
    """
    from .. import breakers_enforce as _enforce

    def _net() -> dict:
        return load_network(network_path)

    def _shared_dir() -> Path:
        n = load_network(network_path)
        # network.json uses "sharedDir" (camelCase) in some installs and
        # "shared_dir" (snake_case) in others. Try both, default fallback.
        return Path(
            n.get("sharedDir")
            or n.get("shared_dir")
            or "/Users/Shared/evolve"
        )

    def _import_store():
        """breakers.store ships in the installed evolve-analyzer package."""
        from breakers import store  # noqa: WPS433
        return store

    @app.get("/api/breakers")
    def api_breakers_list() -> Response:
        """List active (and optionally expired) breaker trips + recent audit log."""
        try:
            include_expired = request.args.get("include_expired") in ("1", "true", "yes")
            try:
                audit_days = int(request.args.get("audit_days", "7"))
            except ValueError:
                audit_days = 7
            audit_days = max(0, min(audit_days, 90))

            store = _import_store()
            sd = _shared_dir()
            records = (
                store.list_all(sd) if include_expired
                else store.list_active(sd)
            )
            trips = [
                {
                    "scope": r.bot_id,           # bot_id or "pod"
                    "type": r.type,
                    "trip_id": r.trip_id,
                    "tripped_at": r.tripped_at,
                    "expires_at": r.expires_at,
                    "initiated_by": r.initiated_by,
                    "reason": r.reason,
                    "expired": store.is_expired(r),
                }
                for r in records
            ]
            audit = (
                store.read_audit_log(sd, days=audit_days)
                if audit_days > 0 else []
            )
            return jsonify({
                "ok": True,
                "active_count": sum(1 for t in trips if not t["expired"]),
                "trips": trips,
                "audit": audit[:50],
            })
        except Exception as exc:  # noqa: BLE001
            return error_response(exc, ok=False)

    @app.post("/api/breakers/trip")
    def api_breakers_trip() -> Response:
        body = request.get_json(silent=True) or {}
        scope = (body.get("scope") or "").strip()
        breaker_type = (body.get("type") or "").strip()
        duration = (body.get("duration") or "24h").strip() or "24h"
        reason = (body.get("reason") or "operator trip").strip() or "operator trip"
        initiated_by = (body.get("initiated_by") or "web").strip() or "web"
        motivating_signals = body.get("motivating_signals") or []

        if not scope or not breaker_type:
            return jsonify({
                "ok": False,
                "error": "scope (bot_id or 'pod') and type ('cost'|'full') are required",
            }), 400

        try:
            store = _import_store()
            dur = store.parse_duration(duration)
            record = store.trip(
                shared_dir=_shared_dir(),
                scope=scope, breaker_type=breaker_type,
                duration=dur, initiated_by=initiated_by, reason=reason,
                motivating_signals=list(motivating_signals),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return error_response(exc, ok=False)

        try:
            enforce_result = _enforce.enforce_trip(
                scope=scope, breaker_type=breaker_type, network=_net(),
                reason=record.reason, expires_at_iso=record.expires_at,
            )
        except ValueError as exc:
            return jsonify({
                "ok": False,
                "error": f"state written, enforce failed: {exc}",
                "trip": record.to_json(),
            }), 207

        status = 200 if enforce_result.ok else 207
        return jsonify({
            "ok": enforce_result.ok,
            "trip": record.to_json(),
            "enforce": enforce_result.to_dict(),
        }), status

    @app.post("/api/breakers/reset")
    def api_breakers_reset() -> Response:
        body = request.get_json(silent=True) or {}
        scope = (body.get("scope") or "").strip()
        breaker_type = (body.get("type") or "").strip()
        reason = (body.get("reason") or "operator reset").strip() or "operator reset"
        initiated_by = (body.get("initiated_by") or "web").strip() or "web"

        if not scope or not breaker_type:
            return jsonify({
                "ok": False,
                "error": "scope and type are required",
            }), 400

        try:
            store = _import_store()
            prior = store.reset(
                shared_dir=_shared_dir(),
                scope=scope, breaker_type=breaker_type,
                initiated_by=initiated_by, reason=reason,
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return error_response(exc, ok=False)

        if prior is None:
            return jsonify({
                "ok": True,
                "was_tripped": False,
                "reset": None,
                "enforce": None,
            })

        try:
            enforce_result = _enforce.enforce_reset(
                scope=scope, breaker_type=breaker_type, network=_net(),
            )
        except ValueError as exc:
            return jsonify({
                "ok": False,
                "error": f"state cleared, enforce failed: {exc}",
                "was_tripped": True,
                "reset": prior.to_json(),
            }), 207

        status = 200 if enforce_result.ok else 207
        return jsonify({
            "ok": enforce_result.ok,
            "was_tripped": True,
            "reset": prior.to_json(),
            "enforce": enforce_result.to_dict(),
        }), status


# _register_host_health_routes extracted to routes_host_health.py to keep
# server.py under its no-growth cap; imported + called in create_app.

# _register_reliability_routes and the reliability.py module were removed
# 2026-06-08 along with the rest of the app-test surface. The endpoint
# depended on test_telemetry JSONL the scheduler never wrote; its frontend
# consumers (Overview tile + Apps subtab) were removed later as dead code.

def _operator_create_apply(
    *,
    action_kind: str,
    action_payload: dict,
    bot_id: str,
    summary: str,
    technique: str,
    dimension: str,
    risk,  # schema.proposal.RiskTag — typed loosely to avoid a top-level import
    shared_dir: Path,
) -> tuple[dict | None, str | None]:
    """Create an operator-originated proposal and immediately approve+apply it.

    UI access is the operator's authorization (memory:
    feedback_ui_authorization_presumed.md); routing operator-clicked config
    changes through Self-Improvement adds ceremony without value. This helper
    keeps the shared Proposal pipeline — write → security_warden gates inside
    appliers → snapshot + apply → verify — but auto-approves at write time so
    the change happens on click instead of after a separate approval step.

    Generator-originated proposals (status=pending, awaiting human review) are
    unchanged; only operator-originated ones (``generator_id="operator_ui"``)
    take this inline path.

    Returns ``(proposal_dict, None)`` on success — read ``proposal_dict["status"]``
    for the final outcome: ``"succeeded"`` if applied end-to-end,
    ``"failed_flagged"`` if security_warden / applier refused or raised. Returns
    ``(None, error_str)`` on creation-class failure (bad payload, import error)
    so the caller can respond 400.
    """
    try:
        from schema.proposal import Proposal, action_from_dict, new_proposal_id
        from schema.provenance import Provenance
        from arbiter.store import write_proposal, move_proposal
        from arbiter.state_machine import transition, IllegalTransitionError
        from arbiter.apply import apply as _arbiter_apply, is_deferred_completion_kind
    except ImportError as exc:
        return None, f"schema/arbiter import failed: {exc}"

    # Manual / external completion kinds (Investigation, WorkflowInstruction,
    # AddSignalCollection, BuildApp) need an operator or sweep to finish them
    # after apply. The five operator-UI surfaces don't produce these, but the
    # guard is defensive so a future caller can't accidentally fire-and-forget.
    if is_deferred_completion_kind(action_kind):
        return None, (
            f"action kind {action_kind!r} requires manual or external completion "
            "and is not supported on the operator UI inline path"
        )

    try:
        action = action_from_dict({"kind": action_kind, **action_payload})
    except (ValueError, TypeError) as exc:
        return None, f"action decode failed: {exc}"

    provenance = Provenance(
        technique=technique,
        signals={"action_kind": action_kind, "summary": summary},
        confidence=1.0,
    )

    proposal = Proposal(
        id=new_proposal_id(),
        bot_id=bot_id or "pod",
        generator_id="operator_ui",
        dimension=dimension,
        trigger_observations=[f"operator_ui:{action_kind}"],
        provenance=provenance,
        problem=summary,
        action=action,
        risk_tag=risk,
        admin_surface_summary=summary[:120],
        approval_audience="pod_operator",
        urgency="improvement",
        status="pending",
    )

    try:
        write_proposal(proposal, shared_dir)
    except OSError as exc:
        return None, f"proposal write failed: {exc}"

    try:
        transition(
            proposal, "approved_auto",
            actor="operator_ui",
            reason="operator UI click — UI access is the operator authorization",
        )
    except IllegalTransitionError as exc:
        # Shouldn't happen for a freshly-created pending proposal.
        return proposal.to_dict(), f"approval transition failed: {exc}"

    outcome = _arbiter_apply(proposal, actor="operator_ui", shared_dir=shared_dir)

    # If apply returned not-ok without auto-flagging (i.e. the applier refused
    # for a non-flag reason or raised), drive the proposal to failed_flagged
    # explicitly so it doesn't sit at approved_auto forever.
    if not outcome.ok and proposal.status in ("approved_auto", "approved_human"):
        try:
            transition(
                proposal, "failed_flagged",
                actor="operator_ui",
                reason=outcome.message or "applier returned not-ok",
            )
        except IllegalTransitionError:
            pass

    # Move the on-disk file from pending/ to whichever subdir the final
    # status maps to (archived/ for succeeded / failed_flagged, applied/
    # for any deferred-completion edge case).
    try:
        move_proposal(proposal, shared_dir, from_subdir="pending")
    except OSError as exc:
        return proposal.to_dict(), f"move_proposal warning: {exc}"

    return proposal.to_dict(), None


def _operator_proposal_response(proposal: dict | None, err: str | None):
    """Standard Flask response for operator-UI proposal endpoints.

    Creation-class failure (bad payload, import error) → 400.
    Apply outcome lives in ``proposal["status"]``; ``applied`` is a convenience
    boolean for the UI to render success vs. failure inline.
    """
    if proposal is None:
        return jsonify({"ok": False, "error": err}), 400
    history = proposal.get("history") or []
    last_reason = (history[-1].get("reason") if history else "") or ""
    body = {
        "ok": True,
        "proposal_id": proposal["id"],
        "status": proposal["status"],
        "applied": proposal["status"] == "succeeded",
        "message": last_reason,
    }
    if err:
        body["warning"] = err
    return jsonify(body)

def _register_permissions_admin_routes(app: Flask, network_path: Path) -> None:
    """Register /api/permissions/* endpoints — per-bot permission posture.

    Spec: docs/spec-permission-posture-2026-05-10.md.

    Phase A is read-only: inventory across the three OpenClaw permission
    surfaces (openclaw.json permission config, exec-approvals.json runtime
    store, cron/jobs.json scheduled invocations) plus the rule-based
    composite-posture classifier (tight/moderate/wide/open). Mutation
    appliers ship in Phase B.
    """
    def _all_bots() -> list[str]:
        return list(load_network(network_path).get("bots", {}).keys())

    def _shared_dir() -> Path:
        return Path(load_network(network_path).get("sharedDir", "/Users/Shared/evolve"))

    def _scan_all(bots: list[str]) -> dict[str, dict]:
        """Read live inventory for every bot, classify posture, return raw dicts.

        Live reads (not cached) — Phase A is small enough that we can re-read
        on demand without the audit-driven monitor. Cache writes come later.
        """
        from permissions import inventory as _inv, posture as _posture
        from permissions import denylist as _deny
        result: dict[str, dict] = {}
        for bid in bots:
            try:
                pi = _inv.read_inventory(bid)
                _posture.annotate(pi)
                d = pi.to_dict()
                d["denylist_matches"] = {
                    "approvals": [m.__dict__ for m in _deny.scan_approvals(d)],
                    "cron": [m.__dict__ for m in _deny.scan_cron(d)],
                }
                result[bid] = d
            except Exception as exc:  # surface as a per-bot error, don't 500
                result[bid] = {
                    "bot_id": bid,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        return result

    @app.get("/api/permissions/inventory")
    def api_permissions_inventory() -> Response:
        """Return live permission inventory + posture for every bot."""
        try:
            bots = _all_bots()
            return jsonify({"bots": _scan_all(bots)})
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"permissions module not importable: {exc}"}), 500

    @app.get("/api/permissions/bot/<bot_id>")
    def api_permissions_bot(bot_id: str) -> Response:
        """Return live permission inventory + posture for a single bot."""
        try:
            data = _scan_all([bot_id])
            return jsonify(data.get(bot_id, {"bot_id": bot_id, "error": "unknown bot"}))
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"permissions module not importable: {exc}"}), 500

    def _create_permission_proposal(action_kind: str, payload: dict, bot_id: str, summary: str):
        """Create + auto-apply an operator-originated permission proposal."""
        try:
            from schema.proposal import RiskTag
        except ImportError as exc:
            return None, f"schema import failed: {exc}"
        risk = RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["bot_config"],
        )
        return _operator_create_apply(
            action_kind=action_kind,
            action_payload=payload,
            bot_id=bot_id,
            summary=summary,
            technique="operator_ui_permission",
            dimension="operational_health",
            risk=risk,
            shared_dir=_shared_dir(),
        )

    @app.post("/api/permissions/config")
    def api_permissions_config_update() -> Response:
        """Create a pending UpdatePermissionConfig proposal.

        Body: {bot_id: str, fields: {dotpath: value, ...}, summary?: str}
        """
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        fields = data.get("fields") or {}
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        if not isinstance(fields, dict) or not fields:
            return jsonify({"ok": False, "error": "fields must be a non-empty object"}), 400

        summary = (data.get("summary") or "").strip() or (
            f"Update permission config on {bot_id}: {sorted(fields.keys())}"
        )
        proposal, err = _create_permission_proposal(
            "UpdatePermissionConfig",
            {"bot_id": bot_id, "fields": fields},
            bot_id=bot_id,
            summary=summary,
        )
        return _operator_proposal_response(proposal, err)

    @app.post("/api/permissions/approval")
    def api_permissions_approval_update() -> Response:
        """Create a pending UpdateExecApproval proposal.

        Body: {bot_id, operation: "add"|"revoke", pattern, agent_id?, scope?}
        """
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        operation = (data.get("operation") or "").strip()
        pattern = (data.get("pattern") or "").strip()
        agent_id = (data.get("agent_id") or "main").strip()
        scope = (data.get("scope") or "agent").strip()
        if not (bot_id and pattern and operation in ("add", "revoke")):
            return jsonify({"ok": False, "error": "bot_id, pattern, and operation in {add,revoke} required"}), 400
        summary = f"{operation.capitalize()} exec-approval {pattern!r} on {bot_id}"
        proposal, err = _create_permission_proposal(
            "UpdateExecApproval",
            {"bot_id": bot_id, "operation": operation, "pattern": pattern,
             "agent_id": agent_id, "scope": scope},
            bot_id=bot_id,
            summary=summary,
        )
        return _operator_proposal_response(proposal, err)

    @app.post("/api/permissions/cron/upsert")
    def api_permissions_cron_upsert() -> Response:
        """Create a pending UpsertCronJob proposal.

        Body: {bot_id, job: {...full job dict...}}
        """
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        job = data.get("job") or {}
        if not bot_id or not isinstance(job, dict) or not job.get("id"):
            return jsonify({"ok": False, "error": "bot_id and job.id required"}), 400
        summary = f"Upsert cron job {job.get('id')!r} on {bot_id}"
        proposal, err = _create_permission_proposal(
            "UpsertCronJob",
            {"bot_id": bot_id, "job": job},
            bot_id=bot_id,
            summary=summary,
        )
        return _operator_proposal_response(proposal, err)

    @app.post("/api/permissions/cron/remove")
    def api_permissions_cron_remove() -> Response:
        """Create a pending RemoveCronJob proposal."""
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        job_id = (data.get("job_id") or "").strip()
        if not (bot_id and job_id):
            return jsonify({"ok": False, "error": "bot_id and job_id required"}), 400
        summary = f"Remove cron job {job_id!r} from {bot_id}"
        proposal, err = _create_permission_proposal(
            "RemoveCronJob",
            {"bot_id": bot_id, "job_id": job_id},
            bot_id=bot_id,
            summary=summary,
        )
        return _operator_proposal_response(proposal, err)

    @app.get("/api/permissions/baseline")
    def api_permissions_baseline_get() -> Response:
        """Return the current permission baseline (with default seed if absent)."""
        try:
            from permissions import baseline as _bl
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"permissions.baseline not importable: {exc}"}), 500
        shared = _shared_dir()
        _bl.write_default_if_missing(shared)
        return jsonify(_bl.load(shared))

    @app.post("/api/permissions/baseline")
    def api_permissions_baseline_update() -> Response:
        """Create a pending UpdatePermissionBaseline proposal.

        Body: {operation, bot_id?, fields}
        """
        data = (request.get_json(silent=True) or {})
        operation = (data.get("operation") or "").strip()
        bot_id = (data.get("bot_id") or "").strip()
        fields = data.get("fields") or {}
        if operation not in ("set_pod_default", "set_bot_override", "set_denylist_patterns"):
            return jsonify({"ok": False, "error": "operation must be one of set_pod_default, set_bot_override, set_denylist_patterns"}), 400
        if not isinstance(fields, dict):
            return jsonify({"ok": False, "error": "fields must be an object"}), 400
        # bot_id is required only for set_bot_override
        if operation == "set_bot_override" and not bot_id:
            return jsonify({"ok": False, "error": "bot_id required for set_bot_override"}), 400
        proposal_bot = bot_id or "<pod>"
        summary = f"Update permission baseline ({operation})"
        proposal, err = _create_permission_proposal(
            "UpdatePermissionBaseline",
            {"operation": operation, "bot_id": bot_id, "fields": fields},
            bot_id=proposal_bot,
            summary=summary,
        )
        return _operator_proposal_response(proposal, err)

    @app.post("/api/permissions/scan")
    def api_permissions_scan() -> Response:
        """Run the permission monitor on demand and return its summary.

        Writes inventory caches, emits/sweeps Signals just like the
        audit-driven run. Returns counts the UI uses to flash a banner.
        """
        try:
            from permissions import monitor as _perm_monitor
        except ImportError:
            return jsonify({"ok": False, "error": "permissions.monitor not importable"}), 500
        try:
            result = _perm_monitor.run(_shared_dir(), _all_bots(), load_network(network_path))
            return jsonify({
                "ok": True,
                "bots_checked": result["bots_checked"],
                "findings_count": len(result["findings"]),
                "swept_resolved": result["swept_resolved"],
            })
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    @app.post("/api/permissions/bootstrap")
    def api_permissions_bootstrap() -> Response:
        """Re-snapshot the baseline from live pod state.

        Body: {overwrite?: bool}  — default false (no-op if file exists)

        Use after a deliberate posture change to re-anchor "expected"
        without leaving lingering drift signals.
        """
        try:
            from permissions import bootstrap as _bs
        except ImportError:
            return jsonify({"ok": False, "error": "permissions.bootstrap not importable"}), 500
        data = (request.get_json(silent=True) or {})
        overwrite = bool(data.get("overwrite", False))
        try:
            baseline = _bs.bootstrap(
                _shared_dir(), _all_bots(), load_network(network_path),
                overwrite=overwrite,
            )
            return jsonify({
                "ok": True,
                "overwrite": overwrite,
                "per_bot_overrides": list((baseline.get("per_bot_overrides") or {}).keys()),
            })
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


def _register_intent_routes(app: Flask, network_path: Path) -> None:
    """Register /api/intents/* — Phase 4 of docs/spec-config-intent-system-2026-05-21.md.

    Surface C ("Intentional Deviations") under Security. Four routes
    behind a single ``_register_*`` block so a future move to a
    different sub-package only has to swap one wiring call.

    Routes:
      GET  /api/intents                           — list every active intent across the pod
      GET  /api/intents/<bot_id>                  — list one bot's intents
      POST /api/intents/<bot_id>/<intent_id>/revoke      — move intent to intents_archive
      POST /api/intents/<bot_id>/<intent_id>/edit-reason — update intent.reason in place

    The two POST routes accept ``actor`` in the JSON body so the audit
    history captures who initiated the change. Default fallback is the
    documented pod-admin operator-UI label; tests override for
    deterministic assertions.

    The HTTP layer is intentionally thin — every operation has a
    matching ``evolve_admin.config_intent`` function and the route is
    just argument shuffling + JSON serialization. The fail-open
    behavior is delegated to those helpers (unreadable sidecar →
    empty list, missing intent → False/None).
    """
    def _shared_dir() -> Path:
        return Path(load_network(network_path).get("sharedDir", "/Users/Shared/evolve"))

    def _network_path() -> Path:
        return network_path

    @app.get("/api/intents")
    def api_intents_list_all() -> Response:
        """Pod-wide list. Returns ``{bot_id: [intent, ...]}``.

        Empty list values are preserved (sidecar exists, all intents
        revoked) so the UI can render a distinguishable "previously
        had intents" state.
        """
        try:
            from evolve_admin.config_intent import list_all_intents
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"config_intent not importable: {exc}"}), 500
        try:
            return jsonify({"bots": list_all_intents(shared_dir=_shared_dir())})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    @app.get("/api/intents/<bot_id>")
    def api_intents_list_bot(bot_id: str) -> Response:
        """Per-bot list."""
        try:
            from evolve_admin.config_intent import list_intents
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"config_intent not importable: {exc}"}), 500
        try:
            return jsonify({
                "bot_id": bot_id,
                "intents": list_intents(bot_id, shared_dir=_shared_dir()),
            })
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    @app.post("/api/intents/<bot_id>/<intent_id>/revoke")
    def api_intents_revoke(bot_id: str, intent_id: str) -> Response:
        """Move ``intent_id`` from ``intents[]`` to ``intents_archive[]``.

        Does NOT mutate the underlying config field — the spec calls
        out that revocation is a metadata operation. The next sweep
        sees the deviation as real drift and (per PR #1430)
        auth_drift_filler emits a revert proposal which the operator
        accepts or rejects normally.
        """
        try:
            from evolve_admin.config_intent import revoke_intent
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"config_intent not importable: {exc}"}), 500
        body = request.get_json(silent=True) or {}
        actor = str(body.get("actor") or "pod_admin (admin UI)").strip()
        if not actor:
            return jsonify({"ok": False, "error": "actor must be non-empty"}), 400
        try:
            cleared = revoke_intent(
                bot_id, intent_id,
                actor=actor,
                shared_dir=_shared_dir(),
                network_path=_network_path(),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
        if not cleared:
            return jsonify({"ok": False, "error": "intent not found"}), 404
        return jsonify({"ok": True, "bot_id": bot_id, "intent_id": intent_id})

    @app.post("/api/intents/<bot_id>/<intent_id>/edit-reason")
    def api_intents_edit_reason(bot_id: str, intent_id: str) -> Response:
        """Update an intent's ``reason`` field in place.

        Body: ``{new_reason: str, actor?: str}``.

        The recorded value and set_by stay intact so the audit chain
        still explains who originally set the field. Appends a
        ``reason_edited`` audit_history entry capturing the prior
        and new reason.
        """
        try:
            from evolve_admin.config_intent import edit_intent_reason
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"config_intent not importable: {exc}"}), 500
        body = request.get_json(silent=True) or {}
        new_reason = str(body.get("new_reason") or "").strip()
        actor = str(body.get("actor") or "pod_admin (admin UI)").strip()
        if not new_reason:
            return jsonify({"ok": False, "error": "new_reason must be non-empty"}), 400
        if not actor:
            return jsonify({"ok": False, "error": "actor must be non-empty"}), 400
        try:
            edited = edit_intent_reason(
                bot_id, intent_id,
                new_reason=new_reason, actor=actor,
                shared_dir=_shared_dir(),
                network_path=_network_path(),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
        if not edited:
            return jsonify({"ok": False, "error": "intent not found"}), 404
        return jsonify({"ok": True, "bot_id": bot_id, "intent_id": intent_id})

    @app.post("/api/intents/<bot_id>/<intent_id>/confirm-queued")
    def api_intents_confirm_queued(bot_id: str, intent_id: str) -> Response:
        """Operator-acknowledges a low-confidence inferred intent.

        Body: ``{new_reason?: str, actor?: str}``.

        Phase 4.1 — surfaces queued intents (those the inference layer
        marked low-confidence in Phase 3) and clears the queued flag
        when the operator confirms. The operator can either accept
        the inferred reason as-is (omit ``new_reason``) or replace it
        (provide a non-empty ``new_reason``). Either way, the queued
        flag clears and an audit_history ``confirmed_queued`` entry
        captures the acknowledgment.

        Mapped 1:1 onto ``config_intent.confirm_queued_intent`` — see
        that helper's docstring for the semantic details.
        """
        try:
            from evolve_admin.config_intent import confirm_queued_intent
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"config_intent not importable: {exc}"}), 500
        body = request.get_json(silent=True) or {}
        new_reason_raw = body.get("new_reason")
        new_reason = (
            str(new_reason_raw).strip()
            if isinstance(new_reason_raw, str) and new_reason_raw.strip()
            else None
        )
        actor = str(body.get("actor") or "pod_admin (admin UI)").strip()
        if not actor:
            return jsonify({"ok": False, "error": "actor must be non-empty"}), 400
        try:
            confirmed = confirm_queued_intent(
                bot_id, intent_id,
                new_reason=new_reason, actor=actor,
                shared_dir=_shared_dir(),
                network_path=_network_path(),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
        if not confirmed:
            return jsonify({"ok": False, "error": "intent not found"}), 404
        return jsonify({"ok": True, "bot_id": bot_id, "intent_id": intent_id})


def _register_plugins_admin_routes(app: Flask, network_path: Path) -> None:
    """Register /api/plugins-admin/* — per-bot OC plugin inventory + baseline.

    Phase A is read-only: inventory and baseline views plus an on-demand
    rescan trigger. Phase B will add UpdatePluginAllowDeny / UpdatePluginEntry
    / UpdatePluginBaseline proposal-creating endpoints.

    Spec: docs/spec-plugin-inventory-2026-05-10.md §7.
    """
    def _all_bots() -> list[str]:
        return list(load_network(network_path).get("bots", {}).keys())

    def _shared_dir() -> Path:
        return Path(load_network(network_path).get("sharedDir", "/Users/Shared/evolve"))

    @app.get("/api/plugins-admin/inventory")
    def api_plugins_admin_inventory() -> Response:
        """Return cached per-bot plugin inventories + the pod baseline.

        Each bot's entry includes the resolved baseline (every bot sees
        the same v2 policy) so the UI can render per-cell status without
        re-resolving client-side.
        """
        try:
            from plugins import inventory as _inv, baseline as _bl
        except ImportError:
            return jsonify({"ok": False, "error": "plugins module not importable"}), 500
        shared = _shared_dir()
        bots = _all_bots()
        bl = _bl.load(shared)
        result: dict = {
            "bots": {},
            "baseline": bl.to_dict(),
            "resolved": {},
        }
        for bid in bots:
            result["bots"][bid] = _inv.load_inventory(shared, bid)
            r = _bl.resolve_for(bl, bid)
            result["resolved"][bid] = {
                "required": sorted(r.required),
                "denied": sorted(r.denied),
                "expected_load_paths": list(r.expected_load_paths),
            }
        return jsonify(result)

    @app.post("/api/plugins-admin/scan")
    def api_plugins_admin_scan() -> Response:
        """Run the plugin monitor on demand."""
        try:
            from plugins import monitor as _plugin_monitor
        except ImportError:
            return jsonify({"ok": False, "error": "plugins module not importable"}), 500
        shared = _shared_dir()
        bots = _all_bots()
        try:
            result = _plugin_monitor.run(shared, bots, load_network(network_path))
            return jsonify({
                "ok": True,
                "bots_checked": result["bots_checked"],
                "findings_count": len(result["findings"]),
                "swept_resolved": result["swept_resolved"],
            })
        except Exception as e:
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500

    def _create_plugin_proposal(action_kind: str, action_payload: dict, bot_id: str, summary: str):
        """Create + auto-apply an operator-originated plugin proposal."""
        try:
            from schema.proposal import RiskTag
        except ImportError as exc:
            return None, f"schema import failed: {exc}"
        risk = RiskTag(
            blast_radius="bot" if bot_id else "pod",
            reversibility="auto",
            touches=["bot_config"] if bot_id else ["policy"],
        )
        return _operator_create_apply(
            action_kind=action_kind,
            action_payload=action_payload,
            bot_id=bot_id,
            summary=summary,
            technique="operator_ui_plugins",
            dimension="operational_health",
            risk=risk,
            shared_dir=_shared_dir(),
        )

    @app.post("/api/plugins-admin/propose-enable")
    def api_plugins_admin_propose_enable() -> Response:
        """Create a pending EnablePluginEntry proposal."""
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        plugin_name = (data.get("plugin_name") or "").strip()
        if not (bot_id and plugin_name):
            return jsonify({"ok": False, "error": "bot_id and plugin_name required"}), 400
        summary = f"Enable plugin {plugin_name!r} on {bot_id}"
        proposal, err = _create_plugin_proposal(
            "EnablePluginEntry",
            {"bot_id": bot_id, "plugin_name": plugin_name},
            bot_id=bot_id, summary=summary,
        )
        return _operator_proposal_response(proposal, err)

    @app.post("/api/plugins-admin/propose-disable")
    def api_plugins_admin_propose_disable() -> Response:
        """Create a pending DisablePluginEntry proposal."""
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        plugin_name = (data.get("plugin_name") or "").strip()
        if not (bot_id and plugin_name):
            return jsonify({"ok": False, "error": "bot_id and plugin_name required"}), 400
        summary = f"Disable plugin {plugin_name!r} on {bot_id}"
        proposal, err = _create_plugin_proposal(
            "DisablePluginEntry",
            {"bot_id": bot_id, "plugin_name": plugin_name},
            bot_id=bot_id, summary=summary,
        )
        return _operator_proposal_response(proposal, err)

    @app.post("/api/plugins-admin/propose-allow-deny")
    def api_plugins_admin_propose_allow_deny() -> Response:
        """Create a pending UpdatePluginAllowDeny proposal for one bot.

        Body: {bot_id, allow?: list[str], deny?: list[str]}
        Pass null/omit a field to leave that side untouched.
        """
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        allow = data.get("allow")
        deny = data.get("deny")
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        if allow is None and deny is None:
            return jsonify({"ok": False, "error": "must specify allow or deny"}), 400
        summary = f"Update plugins.allow / plugins.deny on {bot_id}"
        proposal, err = _create_plugin_proposal(
            "UpdatePluginAllowDeny",
            {"bot_id": bot_id, "allow": allow, "deny": deny},
            bot_id=bot_id, summary=summary,
        )
        return _operator_proposal_response(proposal, err)

    @app.post("/api/plugins-admin/propose-bulk-allow-deny")
    def api_plugins_admin_propose_bulk_allow_deny() -> Response:
        """Retired (2026-06-06).

        Original purpose was to auto-fill a per-bot plugins.allow list
        from the baseline's "expected enabled" set. The v2 rework retired
        the concept of a baseline-curated expected set
        (docs/spec-plugin-posture-rework-2026-06-06.md §2.3) so there's
        nothing to fill from. Operators who want to set an allow list
        can do so per-bot via /api/plugins-admin/propose-allow-deny.
        """
        return jsonify({
            "ok": False,
            "error": (
                "retired: plugin baseline v2 has no 'expected enabled' set "
                "to bulk-adopt from. Use propose-allow-deny per bot."
            ),
        }), 410

    @app.post("/api/plugins-admin/propose-config")
    def api_plugins_admin_propose_config() -> Response:
        """Create a pending UpdatePluginConfig proposal.

        Body: {bot_id, plugin_name, operation, fields}
        operation ∈ {"set_keys", "unset_keys", "replace_block"}.
        For set_keys, fields keys may use dot-notation ("webSearch.apiKey").
        """
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        plugin_name = (data.get("plugin_name") or "").strip()
        operation = (data.get("operation") or "").strip()
        fields = data.get("fields") or {}
        if not (bot_id and plugin_name):
            return jsonify({"ok": False, "error": "bot_id and plugin_name required"}), 400
        if operation not in ("set_keys", "unset_keys", "replace_block"):
            return jsonify({"ok": False, "error": "operation must be set_keys / unset_keys / replace_block"}), 400
        if not isinstance(fields, dict):
            return jsonify({"ok": False, "error": "fields must be an object"}), 400
        summary = f"Update plugin {plugin_name!r} config on {bot_id} ({operation})"
        proposal, err = _create_plugin_proposal(
            "UpdatePluginConfig",
            {"bot_id": bot_id, "plugin_name": plugin_name,
             "operation": operation, "fields": fields},
            bot_id=bot_id, summary=summary,
        )
        return _operator_proposal_response(proposal, err)

    @app.post("/api/plugins-admin/propose-load-paths")
    def api_plugins_admin_propose_load_paths() -> Response:
        """Create a pending UpdatePluginLoadPaths proposal.

        Body: {bot_id, operation, path}
        operation ∈ {"add_path", "remove_path"}.
        Adds are validated against a small trusted-directory whitelist
        (see arbiter/appliers/plugin.py:_LOAD_PATH_WHITELIST).
        """
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        operation = (data.get("operation") or "").strip()
        path = (data.get("path") or "").strip()
        if not (bot_id and path):
            return jsonify({"ok": False, "error": "bot_id and path required"}), 400
        if operation not in ("add_path", "remove_path"):
            return jsonify({"ok": False, "error": "operation must be add_path or remove_path"}), 400
        summary = f"{operation} {path!r} on {bot_id} plugins.load.paths"
        proposal, err = _create_plugin_proposal(
            "UpdatePluginLoadPaths",
            {"bot_id": bot_id, "operation": operation, "path": path},
            bot_id=bot_id, summary=summary,
        )
        return _operator_proposal_response(proposal, err)

    @app.post("/api/plugins-admin/propose-baseline-update")
    def api_plugins_admin_propose_baseline_update() -> Response:
        """Create a pending UpdatePluginBaseline proposal.

        Body: {operation, bot_id?, fields}
        operation ∈ {"set_pod_default", "set_bot_override"}.
        """
        data = (request.get_json(silent=True) or {})
        operation = (data.get("operation") or "").strip()
        if operation not in ("set_pod_default", "set_bot_override"):
            return jsonify({"ok": False, "error": "operation must be set_pod_default or set_bot_override"}), 400
        bot_id = (data.get("bot_id") or "").strip()
        fields = data.get("fields") or {}
        if not isinstance(fields, dict):
            return jsonify({"ok": False, "error": "fields must be an object"}), 400
        if operation == "set_bot_override" and not bot_id:
            return jsonify({"ok": False, "error": "set_bot_override requires bot_id"}), 400
        summary = (
            f"Update plugin baseline ({operation}"
            + (f", bot={bot_id}" if bot_id else "")
            + ")"
        )
        proposal, err = _create_plugin_proposal(
            "UpdatePluginBaseline",
            {"operation": operation, "bot_id": bot_id, "fields": fields},
            bot_id=bot_id, summary=summary,
        )
        return _operator_proposal_response(proposal, err)


def _register_hooks_admin_routes(app: Flask, network_path: Path) -> None:
    """Register /api/hooks-admin/* — per-bot hook config inventory + baseline.

    Phase A is read-only: inventory + baseline + on-demand rescan. Phase B
    will add UpdateHookBaseline / UpdateHookPolicy / EnableWebhookIngress
    proposal endpoints.

    Spec: docs/spec-hook-governance-2026-05-10.md §7.
    """
    def _all_bots() -> list[str]:
        return list(load_network(network_path).get("bots", {}).keys())

    def _shared_dir() -> Path:
        return Path(load_network(network_path).get("sharedDir", "/Users/Shared/evolve"))

    @app.get("/api/hooks-admin/inventory")
    def api_hooks_admin_inventory() -> Response:
        """Return cached per-bot hook inventories + pod baseline + resolved.

        Each bot's resolved baseline is shipped alongside the raw inventory so
        the UI can render per-cell status without re-resolving client-side.
        """
        try:
            from hooks import inventory as _inv, baseline as _bl
        except ImportError:
            return jsonify({"ok": False, "error": "hooks module not importable"}), 500
        shared = _shared_dir()
        bots = _all_bots()
        bl = _bl.load(shared)
        result: dict = {
            "bots": {},
            "baseline": bl.to_dict(),
            "resolved": {},
        }
        for bid in bots:
            result["bots"][bid] = _inv.load_inventory(shared, bid)
            r = _bl.resolve_for(bl, bid)
            result["resolved"][bid] = {
                "webhook_ingress_enabled": r.webhook_ingress_enabled,
                "expected_plugin_policies": {
                    name: {
                        "allow_conversation_access": p.allow_conversation_access,
                        "allow_prompt_injection": p.allow_prompt_injection,
                        "rationale": p.rationale,
                    }
                    for name, p in r.expected_plugin_policies.items()
                },
                "trusted_prompt_mutators": sorted(r.trusted_prompt_mutators),
            }
        return jsonify(result)

    @app.post("/api/hooks-admin/scan")
    def api_hooks_admin_scan() -> Response:
        """Run the hook monitor on demand."""
        try:
            from hooks import monitor as _hook_monitor
        except ImportError:
            return jsonify({"ok": False, "error": "hooks module not importable"}), 500
        shared = _shared_dir()
        bots = _all_bots()
        try:
            result = _hook_monitor.run(shared, bots, load_network(network_path))
            return jsonify({
                "ok": True,
                "bots_checked": result["bots_checked"],
                "findings_count": len(result["findings"]),
                "swept_resolved": result["swept_resolved"],
            })
        except Exception as e:
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500

    def _create_hook_proposal(action_kind: str, action_payload: dict, bot_id: str, summary: str):
        """Create + auto-apply an operator-originated hook proposal."""
        try:
            from schema.proposal import RiskTag
        except ImportError as exc:
            return None, f"schema import failed: {exc}"
        risk = RiskTag(
            blast_radius="bot" if bot_id else "pod",
            reversibility="auto",
            touches=["bot_config"] if bot_id else ["policy"],
        )
        return _operator_create_apply(
            action_kind=action_kind,
            action_payload=action_payload,
            bot_id=bot_id,
            summary=summary,
            technique="operator_ui_hooks",
            dimension="operational_health",
            risk=risk,
            shared_dir=_shared_dir(),
        )

    @app.post("/api/hooks-admin/propose-enable-ingress")
    def api_hooks_admin_propose_enable_ingress() -> Response:
        """Create a pending EnableWebhookIngress proposal."""
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        token = (data.get("token") or "").strip()
        allowed_agents = data.get("allowed_agent_ids") or []
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        if not token:
            return jsonify({"ok": False, "error": "token required (don't enable ingress without one)"}), 400
        if not isinstance(allowed_agents, list) or not allowed_agents:
            return jsonify({"ok": False, "error": "allowed_agent_ids must be a non-empty list"}), 400
        summary = f"Enable webhook ingress on {bot_id}"
        proposal, err = _create_hook_proposal(
            "EnableWebhookIngress",
            {
                "bot_id": bot_id,
                "token": token,
                "path": (data.get("path") or "/hooks"),
                "allowed_agent_ids": allowed_agents,
                "allowed_session_key_prefixes": data.get("allowed_session_key_prefixes") or [],
                "mappings": data.get("mappings") or [],
                "transforms_dir": (data.get("transforms_dir") or ""),
                "max_body_bytes": int(data.get("max_body_bytes") or 65536),
            },
            bot_id=bot_id, summary=summary,
        )
        return _operator_proposal_response(proposal, err)

    @app.post("/api/hooks-admin/propose-disable-ingress")
    def api_hooks_admin_propose_disable_ingress() -> Response:
        """Create a pending DisableWebhookIngress proposal."""
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        summary = f"Disable webhook ingress on {bot_id}"
        proposal, err = _create_hook_proposal(
            "DisableWebhookIngress", {"bot_id": bot_id},
            bot_id=bot_id, summary=summary,
        )
        return _operator_proposal_response(proposal, err)

    @app.post("/api/hooks-admin/propose-update-mapping")
    def api_hooks_admin_propose_update_mapping() -> Response:
        """Create a pending UpdateWebhookMapping proposal."""
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        operation = (data.get("operation") or "").strip()
        if not bot_id or operation not in ("add", "remove", "replace"):
            return jsonify({"ok": False, "error": "bot_id + operation∈{add,remove,replace} required"}), 400
        summary = f"UpdateWebhookMapping {operation} on {bot_id}"
        proposal, err = _create_hook_proposal(
            "UpdateWebhookMapping",
            {
                "bot_id": bot_id,
                "operation": operation,
                "mapping_id": (data.get("mapping_id") or ""),
                "mapping": data.get("mapping") or {},
            },
            bot_id=bot_id, summary=summary,
        )
        return _operator_proposal_response(proposal, err)

    @app.post("/api/hooks-admin/propose-plugin-policy")
    def api_hooks_admin_propose_plugin_policy() -> Response:
        """Create a pending UpdatePluginHookPolicy proposal."""
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        plugin_name = (data.get("plugin_name") or "").strip()
        if not (bot_id and plugin_name):
            return jsonify({"ok": False, "error": "bot_id + plugin_name required"}), 400
        allow_conv = data.get("allow_conversation_access")  # None = leave alone
        allow_inj = data.get("allow_prompt_injection")
        if allow_conv is None and allow_inj is None:
            return jsonify({"ok": False, "error": "must set at least one flag"}), 400
        summary = f"Update hook policy for {plugin_name!r} on {bot_id}"
        proposal, err = _create_hook_proposal(
            "UpdatePluginHookPolicy",
            {
                "bot_id": bot_id,
                "plugin_name": plugin_name,
                "allow_conversation_access": allow_conv,
                "allow_prompt_injection": allow_inj,
            },
            bot_id=bot_id, summary=summary,
        )
        return _operator_proposal_response(proposal, err)

    @app.post("/api/hooks-admin/propose-baseline-update")
    def api_hooks_admin_propose_baseline_update() -> Response:
        """Create a pending UpdateHookBaseline proposal."""
        data = (request.get_json(silent=True) or {})
        operation = (data.get("operation") or "").strip()
        if operation not in ("set_webhook_ingress", "set_plugin_policy", "set_trusted_mutators"):
            return jsonify({"ok": False, "error": "operation must be set_webhook_ingress, set_plugin_policy, or set_trusted_mutators"}), 400
        fields = data.get("fields") or {}
        if not isinstance(fields, dict):
            return jsonify({"ok": False, "error": "fields must be an object"}), 400
        summary = f"Update hook baseline ({operation})"
        proposal, err = _create_hook_proposal(
            "UpdateHookBaseline",
            {"operation": operation, "fields": fields},
            bot_id="", summary=summary,
        )
        return _operator_proposal_response(proposal, err)


def _register_cost_measures_routes(app: Flask, network_path: Path) -> None:
    """Shim — body lives in routes_cost_measures.py."""
    from .routes_cost_measures import register_cost_measures_routes
    return register_cost_measures_routes(app, network_path)
# ── Help ──────────────────────────────────────────────────────────────────────

_DOCS_DIR = Path(__file__).parents[4] / "docs"

# Core doc always included — gives the model grounding in what Evolve is.
_CORE_DOCS = ["help/overview.md"]

# Per-page help doc + optional supplementary technical docs.
_PAGE_DOCS: dict[str, list[str]] = {
    "overview":          ["help/overview-page.md"],
    # Post-v2.2 IA: page-id → doc file mapping (new names after renames/folds)
    "plugins":           ["help/plugins.md", "configuration.md"],
    "integrations-keys": ["help/plugins.md", "configuration.md"],          # redirect alias
    "usage":             ["help/usage.md", "model-roles.md"],
    "cost":              ["help/usage.md", "model-roles.md"],               # redirect alias
    "cost-measures":     ["help/cost-optimization.md", "model-roles.md"],  # redirect alias
    "cost-optimization": ["help/cost-optimization.md", "model-roles.md"],
    "ai-optimization":   ["help/ai-optimization.md", "model-roles.md", "spec-ai-optimization-v2-2026-04-10.md"],
    "maintenance":       ["help/maintenance.md", "operator-runbook.md", "spec-health-checker.md"],
    "security":          ["help/security.md", "spec-security-protocol.md", "spec-evolve-user.md"],
    "monitoring":        ["help/usage.md", "feedback-loop.md"],             # redirect alias → Sessions tab in Usage
    "continuity":        ["help/continuity.md", "continuity-engine.md"],
    "apps":              ["help/apps.md", "manifest-spec.md"],
    "capabilities":      ["help/apps.md", "manifest-spec.md"],             # redirect alias
    "gallery":           ["help/apps.md"],                                  # redirect alias → Apps Gallery tab
    "forge":             ["help/apps.md"],                                  # redirect alias → Apps Forge Jobs tab
    # Recommendations replaced the old Self-Improvement + Proposals pages.
    "recommendations":   ["help/recommendations.md", "feedback-loop.md"],
    "self-improvement":  ["help/recommendations.md", "feedback-loop.md"],  # redirect alias
    "settings":          ["help/settings.md"],
    "modules":           ["help/settings.md"],                             # redirect alias → Settings Modules tab
    "mcp":               ["help/maintenance.md", "spec-claude-integration-2026-04-11.md"],
    # Coaches page (was arbiter-generators); other arbiter sub-pages unchanged.
    "coaches":             ["help/coaches.md", "spec-rsi-architecture-2026-04-17.md"],
    "arbiter-generators":  ["help/coaches.md", "spec-rsi-architecture-2026-04-17.md"],  # redirect alias
    "arbiter-meta-health": ["help/meta-health.md", "spec-rsi-layer-6-completion-2026-04-18.md"],
    "arbiter-profile":     ["help/profile.md", "help/profile-inferrer.md", "spec-rsi-layer-4-adjacency-profile-2026-04-18.md"],
}


def _load_help_docs(page_context: str = "") -> str:
    """Load relevant docs for the given page context into a single string."""
    wanted: list[str] = list(_CORE_DOCS)
    if page_context in _PAGE_DOCS:
        for f in _PAGE_DOCS[page_context]:
            if f not in wanted:
                wanted.append(f)
    else:
        # General question: include all help/ docs (user-facing, concise) only
        seen = set(wanted)
        for files in _PAGE_DOCS.values():
            for f in files:
                if f not in seen and f.startswith("help/"):
                    wanted.append(f)
                    seen.add(f)

    parts = []
    for fname in wanted:
        path = _DOCS_DIR / fname
        if path.exists():
            parts.append(f"### {fname}\n\n{path.read_text()}")

    return "\n\n---\n\n".join(parts)


def _register_help_routes(app: Flask, network_path: Path) -> None:

    @app.post("/api/help")
    def api_help() -> Response:
        """Answer a help question using Evolve's documentation."""
        import urllib.request as _urllib_req
        import urllib.error as _urllib_err
        data = request.get_json(silent=True) or {}
        question = (data.get("question") or "").strip()
        page_context = (data.get("page_context") or "").strip().lower()

        if not question:
            return jsonify({"error": "question required"}), 400

        docs = _load_help_docs(page_context)
        if not docs:
            return jsonify({"error": "Documentation not found — check that the Evolve repo is intact"}), 500

        # Resolve tier3 model from network.json — help bot is a background call.
        net = load_network(network_path)
        try:
            from models import get_tier_models  # type: ignore
            tier3_models = get_tier_models("tier3", net)
        except Exception:
            tier3_models = ["anthropic/claude-haiku-4-5"]

        # Read API keys from the primary bot's auth-profiles.json.
        # Engine background calls (the help bot is one) authenticate against
        # the primary bot — change network.json → primary to redirect.
        try:
            from primary_bot import read_primary_bot_keys_by_provider  # type: ignore
            keys_by_provider = read_primary_bot_keys_by_provider(net)
        except Exception:
            keys_by_provider = {}

        # Pick the first tier3 model we have a key for.
        model_str: str | None = None
        provider: str | None = None
        api_key: str | None = None
        for candidate in tier3_models:
            p = candidate.split("/")[0] if "/" in candidate else "anthropic"
            if p in keys_by_provider:
                model_str = candidate.split("/", 1)[1] if "/" in candidate else candidate
                provider = p
                api_key = keys_by_provider[p]
                break

        if not api_key or not provider or not model_str:
            configured = list(keys_by_provider.keys()) or ["none"]
            return jsonify({"error": (
                f"No API key found for any tier3 model ({', '.join(tier3_models)}). "
                f"Keys configured: {', '.join(configured)}. Add a key in Integrations & Keys."
            )}), 500

        system_prompt = (
            "You are the Evolve help agent. Evolve is an operating model layer for OpenClaw AI bot pods. "
            "Answer the user's question using only the documentation provided. "
            "Be specific and direct. If the answer is not in the docs, say so clearly. "
            "Use markdown for structure when helpful (bullets, bold terms, code blocks). "
            "Keep answers concise but complete."
        )
        user_content = f"DOCUMENTATION:\n{docs}\n\nUSER QUESTION: {question}"

        try:
            if provider == "anthropic":
                payload = json.dumps({
                    "model": model_str,
                    "max_tokens": 1024,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_content}],
                }).encode()
                req = _urllib_req.Request(
                    "https://api.anthropic.com/v1/messages",
                    data=payload,
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    method="POST",
                )
                with _urllib_req.urlopen(req, timeout=45) as resp:
                    result = json.loads(resp.read())
                answer = result.get("content", [{}])[0].get("text", "").strip()

            elif provider == "openai":
                payload = json.dumps({
                    "model": model_str,
                    "max_tokens": 1024,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                }).encode()
                req = _urllib_req.Request(
                    "https://api.openai.com/v1/chat/completions",
                    data=payload,
                    headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
                    method="POST",
                )
                with _urllib_req.urlopen(req, timeout=45) as resp:
                    result = json.loads(resp.read())
                answer = result["choices"][0]["message"]["content"].strip()

            elif provider == "google":
                payload = json.dumps({
                    "contents": [{"parts": [{"text": f"{system_prompt}\n\n{user_content}"}]}],
                    "generationConfig": {"maxOutputTokens": 1024},
                }).encode()
                req = _urllib_req.Request(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model_str}:generateContent?key={api_key}",
                    data=payload,
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with _urllib_req.urlopen(req, timeout=45) as resp:
                    result = json.loads(resp.read())
                answer = result["candidates"][0]["content"]["parts"][0]["text"].strip()

            else:
                return jsonify({"error": f"Provider '{provider}' not supported for help bot"}), 500

            return jsonify({"answer": answer, "page_context": page_context, "model": f"{provider}/{model_str}"})

        except _urllib_err.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            return jsonify({"error": f"API error ({provider}) {e.code}: {body}"}), 500
        except TimeoutError:
            return jsonify({"error": "Request timed out (45s). Try a more specific question."}), 504
        except Exception as e:
            return jsonify({"error": f"LLM call failed: {str(e)[:200]}"}), 500


def _register_arbiter_routes(app: Flask, network_path: Path) -> None:
    """Shim — body lives in routes_arbiter.py."""
    from .routes_arbiter import register_arbiter_routes
    return register_arbiter_routes(app, network_path)


def _register_pod_rollup_routes(app: Flask, network_path: Path) -> None:
    """Register GET /api/pod/rollup — cross-bot aggregation endpoint.

    Returns the four pod-level rollup sections:
      - spend:     per-bot cost breakdown + pod total + projected month-end
      - attention: bots with firing signals or health chips
      - activity:  bots ranked by turns + sessions in the last 7 days
      - deadlines: scheduled applications (cron-based) across all bots

    The endpoint reads tile data already embedded in /api/status and
    augments with signals-store and manifest reads.  It is intentionally
    additive — the per-bot tile system is unchanged.
    """
    from ..pod_rollup import compute_pod_rollup
    from datetime import date as _date

    def _shared() -> Path:
        return Path(
            load_network(network_path).get("sharedDir", "/Users/Shared/evolve")
        )

    @app.get("/api/pod/rollup")
    def api_pod_rollup() -> Response:
        """Aggregate cross-bot pod metrics.

        Accepts optional query param ``today=YYYY-MM-DD`` for testing.

        Shape::

            {
                "spend":     {by_bot, pod_total_7d, pod_total_28d,
                               projected_month_end, bots_with_spend_7d, currency},
                "attention": {needs_attention, all_clear, total_firing_signals},
                "activity":  {ranked, pod_turns_7d, pod_sessions_7d, busiest_bot},
                "deadlines": {items, total},
            }
        """
        try:
            network = load_network(network_path)
            shared_dir = _shared()

            # Re-use the cached status data embedded in /api/status by
            # reading the per-bot tile data directly from tile_metrics.
            # We compute tiles here rather than requiring the caller to
            # fetch /api/status first, so /api/pod/rollup is self-contained.
            data = network_status(network_path)
            bots = data.get("bots") or {}

            # Overlay gateway probe freshness (same as api_status does)
            for bot_id, bot_data in bots.items():
                try:
                    _sf = json.loads(
                        (shared_dir / "status" / f"{bot_id}.json").read_text()
                    )
                    _age = _time.time() - float(_sf.get("ts_epoch") or 0)
                    if _age <= 600:
                        bot_data["gateway_running"] = bool(_sf.get("gateway_running"))
                        bot_data["gateway_reachable"] = bool(_sf.get("gateway_reachable"))
                        bot_data["gateway_status_fresh"] = True
                except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError, ValueError):
                    pass

            # Compute tile data for each bot so rollup has rich chip/cost/activity data.
            try:
                from tile_metrics import compute_tile_data as _compute_tile_data
                for bot_id, bot_data in bots.items():
                    try:
                        bot_data["tile"] = _compute_tile_data(
                            shared_dir=shared_dir,
                            bot_id=bot_id,
                            bot_data=bot_data,
                            network=network,
                        )
                    except Exception:
                        continue
            except Exception:
                pass

            today_str = request.args.get("today")
            try:
                today = _date.fromisoformat(today_str) if today_str else _date.today()
            except (TypeError, ValueError):
                today = _date.today()

            rollup = compute_pod_rollup(
                shared_dir=shared_dir,
                bots=bots,
                today=today,
            )
            return jsonify({"ok": True, **rollup})

        except Exception as e:
            return error_response(e)


def _register_candidates_routes(app: Flask, network_path: Path) -> None:
    """Register /api/candidates/* read endpoints for Phase 2's
    Tracked-candidates UI surface.

    Spec: docs/spec-proposal-synthesizer-2026-05-10.md §8.

    GET /api/candidates/watchlist     — candidates tracked but not yet
                                        Proposals (concreteness demotions,
                                        synthesizer-deferred entries)
    GET /api/candidates/synthesizing  — candidates awaiting the LLM
                                        synthesizer (substrate aggregates
                                        in Phase 2)
    GET /api/candidates/dropped       — gate-dropped candidates, last N
                                        days; the operator-tuning view

    Read-only in Phase 2. Mutation (manual escalate, dismiss) lands when
    the UI grows action affordances.
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    def _shared_dir() -> Path:
        return Path(
            load_network(network_path).get("sharedDir", "/Users/Shared/evolve")
        )

    def _candidate_view(cand) -> dict:
        d = cand.to_dict()
        # Slim the view down for list rendering — drop the full
        # provenance.signals payload (can be large) but keep the parts
        # the UI needs.
        prov = d.get("provenance") or {}
        d["provenance"] = {
            "technique": prov.get("technique"),
            "confidence": prov.get("confidence"),
        }
        return d

    @app.get("/api/candidates/watchlist")
    def api_candidates_watchlist() -> Response:
        try:
            store = _import_analyzer("proposal_synthesizer.store")
            shared_dir = _shared_dir()
            cands = list(store.iter_candidates(shared_dir, subdirs=("watchlist",)))
            try:
                limit = max(1, min(500, int(request.args.get("limit", 200))))
            except ValueError:
                limit = 200
            return jsonify(
                {
                    "candidates": [_candidate_view(c) for c in cands[:limit]],
                    "total": len(cands),
                }
            )
        except Exception as e:  # noqa: BLE001
            return error_response(e)

    @app.get("/api/candidates/synthesizing")
    def api_candidates_synthesizing() -> Response:
        try:
            store = _import_analyzer("proposal_synthesizer.store")
            shared_dir = _shared_dir()
            cands = list(
                store.iter_candidates(shared_dir, subdirs=("synthesizing",))
            )
            try:
                limit = max(1, min(500, int(request.args.get("limit", 200))))
            except ValueError:
                limit = 200
            return jsonify(
                {
                    "candidates": [_candidate_view(c) for c in cands[:limit]],
                    "total": len(cands),
                }
            )
        except Exception as e:  # noqa: BLE001
            return error_response(e)

    @app.get("/api/candidates/dropped")
    def api_candidates_dropped() -> Response:
        """List drop records across the last N days (default 7)."""
        try:
            shared_dir = _shared_dir()
            try:
                days = max(1, min(30, int(request.args.get("days", 7))))
            except ValueError:
                days = 7
            try:
                limit = max(1, min(2000, int(request.args.get("limit", 500))))
            except ValueError:
                limit = 500

            now = _dt.now(_tz.utc)
            records: list[dict] = []
            drop_root = shared_dir / "candidates" / "dropped"
            if drop_root.exists():
                from datetime import timedelta as _td

                for delta in range(days):
                    day = (now - _td(days=delta)).strftime("%Y-%m-%d")
                    log = drop_root / f"{day}.jsonl"
                    if not log.exists():
                        continue
                    try:
                        for line in log.read_text().splitlines():
                            if not line.strip():
                                continue
                            try:
                                records.append(_json.loads(line))
                            except _json.JSONDecodeError:
                                continue
                    except OSError:
                        continue
            # Newest first
            records.sort(key=lambda r: r.get("ts", ""), reverse=True)
            return jsonify(
                {
                    "drops": records[:limit],
                    "total": len(records),
                    "window_days": days,
                }
            )
        except Exception as e:  # noqa: BLE001
            return error_response(e)


# ── Skills inventory routes ────────────────────────────────────────────────────

def _register_skills_routes(app: Flask, network_path: Path) -> None:
    """Register /api/skills/* endpoints — per-bot and pod-level skills inventory.

    Spec 12: Skills inventory view (minimal MVP).

    GET /api/skills/pod       — Cross-bot skills matrix (must be before /<bot_id>)
    GET /api/skills/<bot_id>  — SkillInventory for one bot (plugins + MCP servers)
    """
    from ..skills import get_bot_skills, get_pod_skills

    def _net() -> dict:
        return load_network(network_path)

    @app.get("/api/skills/pod")
    def api_skills_pod() -> Response:
        """Return cross-bot skills matrix for the whole pod.

        Response shape:
        {
          "bots": {bot_id: SkillInventory},
          "matrix": {skill_id: {bot_id: status | null}},
          "skill_meta": {skill_id: {display, category, format_compliance}},
          "all_bot_ids": [str, ...],
          "capability_summaries": {
            "google": {
              bot_id: {
                "summary": "read" | "read + write" | "custom" | ...,
                "labels":  ["Read Gmail", "Send Gmail", ...],
              },
            },
          },
        }

        The ``capability_summaries`` field is the post-PR-#2231 IA layer.
        The catalog chip renders "✓ atlas (read)" by reading ``summary``;
        the tooltip falls back to enumerating ``labels`` when the summary
        is the opaque ``"custom"`` string (so the operator sees
        "Installed: Read Gmail, Send Gmail, Read Calendar" instead of
        the bare "Installed: custom"). Currently only the unified
        ``google`` skill populates summaries; if other skills grow per-
        bot capability metadata they can extend this section.
        """
        try:
            net = _net()
            bots = net.get("bots") or {}
            result = get_pod_skills(bots, network=net)

            # Enrich with capability summaries for the unified Google
            # skill. Per-bot parallel resolution (already the pattern
            # for get_pod_skills' internal bot reads) bounds wall time
            # to one bot's worth of status-resolve latency.
            from concurrent.futures import ThreadPoolExecutor
            from ..skills.google_install import resolve_capability_summary_entry

            def _resolve_one(bid: str) -> "tuple[str, dict | None]":
                # Resolves both config paths (OAuth profile + DwD); see the
                # helper's docstring. Path-C (service_account_dwd) bots have
                # no OAuth profile, so their entry is derived from the
                # network.json google_integration block passed here.
                return bid, resolve_capability_summary_entry(
                    bid, bot_cfg=bots.get(bid),
                )

            bot_ids = list(bots.keys())
            google_summaries: dict[str, dict] = {}
            if bot_ids:
                with ThreadPoolExecutor(max_workers=min(8, len(bot_ids))) as ex:
                    for bid, entry in ex.map(_resolve_one, bot_ids):
                        if entry is not None:
                            google_summaries[bid] = entry

            result["capability_summaries"] = {"google": google_summaries}
            return jsonify(result)
        except Exception as e:
            return error_response(e)

    @app.get("/api/skills/<bot_id>")
    def api_skills_bot(bot_id: str) -> Response:
        """Return the skills inventory for one bot.

        Response shape:
        {
          "bot_id": str,
          "skills": [
            {
              "id": str,
              "display": str,
              "category": str,
              "status": "configured" | "needs_oauth" | "missing_config",
              "format_compliance": "standard" | "proprietary",
              "enabled": bool,
              "install_source": str | null,
              "apps_using": [str, ...]
            },
            ...
          ],
          "read_error": str | null
        }
        """
        net = _net()
        bots = net.get("bots") or {}
        if bot_id not in bots:
            return jsonify({"error": f"Bot {bot_id!r} not found in network"}), 404
        user = bots[bot_id].get("user") or bot_id
        try:
            inv = get_bot_skills(bot_id, user, network=net)
            return jsonify(inv.to_dict())
        except Exception as e:
            log_request_error(e)
            return jsonify({
                "bot_id": bot_id,
                "skills": [],
                "read_error": str(e),
            }), 500
