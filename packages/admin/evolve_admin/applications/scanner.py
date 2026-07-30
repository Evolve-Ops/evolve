"""
Workspace scanner — auto-discovers applications from an existing bot workspace.

Pipeline (Python-orchestrated; LLM answers questions, never drives):

  Phase 1 — Inventory    : filesystem walk + crontab + memory files. Fast, no LLM. (~5s)
  Phase 2 — LLM discovery: rich-context application clustering. (tier3, ~35s)
  Phase 3 — Merge        : deduplicate LLM results. (~0.1s)
  Phase 4 — Manifests    : full 4-section manifest per new app. (tier3, parallel)

Reliability guarantees:
  - Python orchestrates every phase transition.
  - LLM failures fall back to stub manifests — pipeline always completes.
  - Each manifest is written atomically immediately after generation.
  - Partial results survive a crash: re-run skips already-saved manifests.
  - No hardcoded model names — always resolve_tier("tier3", config).
"""

from __future__ import annotations

import json
import logging
import plistlib
import re
import subprocess
import threading

from platform_profile import get_profile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Aliased to keep the historical local name. Call sites pass mode=0o644 to
# preserve the pre-consolidation permissions (the old local helper wrote via
# ``Path.write_text``, i.e. umask-derived 0o644): manifests/status files are
# written by the scanner but read by other user contexts (bot users, admin
# server).
from evolve_util import atomic_write_json as _atomic_write

from ..config import get_bot_user, load_network
from ..runtime import LaunchdScheduler, get_scheduler
from ..secret_config_perms import exists_or_unreachable
# Shared canonical workspace-relative join key (F-C1). ``path_keys`` has no
# internal deps, so this is cycle-free — unlike ``app_ownership_policy`` /
# ``recon_ledger`` which scanner can only reach via a deferred import.
from .path_keys import ws_rel_key

# Module logger. The scanner walks bot ``.openclaw/`` trees; on Linux/Py3.12 a
# 0700 ACL-mask clamp (the OC gateway re-hardens .openclaw on startup) makes a
# bare ``.exists()``/``.rglob()``/``.iterdir()``/``.stat()`` RAISE PermissionError
# rather than return — so the per-walk inventory sites guard with try/except and
# LOG the skip here (skip/empty under EACCES, never silently confabulate
# "absent"). Present-or-proceed gates route through ``exists_or_unreachable``.
_log = logging.getLogger(__name__)


class MissingApiKeyError(RuntimeError):
    """LLM phase requested but no LLM provider is credentialed.

    Raised by ``llm_discover_applications`` when ``use_llm=True`` and no
    provider key resolves via ``infra_llm`` (env vars + the primary
    bot's auth store). Catchers
    (``scan_workspace_pipeline``) translate this into a structured status
    file write so the admin UI can render a real diagnostic instead of
    showing the scan as a quick success with zero results.
    """


# ── Phase metadata (used by server status endpoint and UI) ────────────────────

SCAN_PHASES: list[dict] = [
    {"num": 1, "name": "inventory",        "desc": "Collecting workspace files, cron jobs, memory files, and scheduled tasks", "eta_s": 5},
    {"num": 2, "name": "llm_discovery",    "desc": "AI is analyzing workspace content to identify applications", "eta_s": 35},
    {"num": 3, "name": "merge",            "desc": "Merging and deduplicating results",                         "eta_s": 1},
    {"num": 4, "name": "manifests",        "desc": "Generating detailed manifests for discovered applications",  "eta_s": 20},
    # Post-manifest passes (added 2026-06-08 to fix "Phase 7 of 5"
    # display bug — these phases were writing status with a hardcoded
    # phase_total=5 that didn't match their actual phase numbers).
    {"num": 5, "name": "file_stamp",       "desc": "Registering component files with the manifests",         "eta_s": 5},
    {"num": 6, "name": "layer_classify",   "desc": "Re-classifying file layers",                              "eta_s": 3},
    {"num": 7, "name": "reconcile",        "desc": "Reconciling manifest claims vs reality",                  "eta_s": 5},
    {"num": 8, "name": "coherence_pass_a", "desc": "Coherence checks on each manifest",                       "eta_s": 3},
]
PHASE_TOTAL = len(SCAN_PHASES)

# Map historical-name phase ids that the scanner still uses in some
# write sites onto the canonical 1..PHASE_TOTAL numbering. Lets us
# minimize churn at the call sites while honoring the unified counter.
_LEGACY_PHASE_NUM_MAP = {
    5:     5,    # file_stamp
    "5":   5,
    "5.5": 6,    # layer_classify
    6:     7,    # reconcile
    "6":   7,
    7:     8,    # coherence_pass_a
    "7":   8,
}


# ── Inventory dataclass ───────────────────────────────────────────────────────

@dataclass
class WorkspaceInventory:
    """Rich snapshot of a bot's workspace — collected once, used across all phases."""
    workspace: Path
    bot_id: str
    user: str = ""  # OS username (from network.json); falls back to bot_id
    # Text content of key identity files
    soul_content: str = ""
    user_md_content: str = ""
    agents_content: str = ""
    memory_md_content: str = ""
    # v13: scheduled-action surfaces (the heartbeat-embedded behavior layer)
    heartbeat_content: str = ""
    pod_conduct_content: str = ""
    # Each entry: {file_path, heading, level, body, sha256, line_start, line_end}
    # — extracted from AGENTS.md / HEARTBEAT.md / POD_CONDUCT.md / SOUL.md.
    scheduled_action_candidates: list[dict] = field(default_factory=list)
    # Loaded launchd labels (from `launchctl list`) — fed into Tier-2 evidence
    launchctl_labels: list[str] = field(default_factory=list)
    # v16: enumerated LaunchAgent plist entries from ~/Library/LaunchAgents/.
    # Distinct from launchctl_labels (which only tells us what's loaded right now);
    # this carries the full plist contents so app attribution (manifest §6)
    # can match by Label namespace AND ProgramArguments path. Each entry:
    #   {label, plist_path, program_args[], start_interval, start_calendar_interval,
    #    run_at_load, raw}
    launchd_entries: list[dict] = field(default_factory=list)
    # v17: enumerated evolve-managed sections in HEARTBEAT.md / AGENTS.md
    # (in-session behavioral injection points). Replaces ``openclaw_hooks``
    # — OpenClaw has no ``hooks.heartbeat[]`` config surface (PR 9 spec).
    # Each entry: {file, anchor, pkg_id (from marker), body, command_hint}.
    # The pkg_id is parsed out of the ``<!-- evolve-managed: pkg=… -->``
    # marker for attribution; command_hint is whatever text in the body
    # looks like an executable invocation (regex match for the leading
    # backticked code snippet).
    heartbeat_md_sections: list[dict] = field(default_factory=list)
    # Kept for one schema version so in-flight consumers still see the
    # attribute (always empty in v17). Removed in v18.
    openclaw_hooks: list[dict] = field(default_factory=list)
    # Scheduled tasks — cron_entries as manifest-ready dicts, cron_jobs as rich inventory dicts
    cron_entries: list[dict] = field(default_factory=list)
    cron_jobs: list[dict] = field(default_factory=list)
    # cron_jobs entries:  {schedule, script_path, script_content, is_infrastructure}
    # cron_entries dicts: {schedule, script, label, file_id}  (manifest-ready v5 format)
    # Named workspace directories (non-infrastructure root dirs)
    named_dirs: list[dict] = field(default_factory=list)
    # Each named_dirs entry: {name, files, subdirs, content_preview}
    # JSON data stores — large structured JSON files (tasks, logs, registries, etc.)
    json_stores: list[dict] = field(default_factory=list)
    # Each json_stores entry: {path, size_kb, hint}
    # Memory files with recurring structure detection
    memory_files: list[dict] = field(default_factory=list)
    # Each memory_files entry: {path, preview, is_recurring, entry_count_estimate}
    # File lists (relative paths as strings)
    python_scripts: list[str] = field(default_factory=list)
    shell_scripts: list[str] = field(default_factory=list)
    # Markdown files with content previews
    markdown_files: list[dict] = field(default_factory=list)   # [{path, preview, size}]
    # Directory structure
    directories: list[str] = field(default_factory=list)
    # All other non-default files (for completeness)
    other_files: list[str] = field(default_factory=list)


# ── OC default files/dirs (excluded from non-preview inventory) ───────────────

OC_DEFAULT_FILES = {
    "AGENTS.md", "HEARTBEAT.md", "IDENTITY.md", "BOOTSTRAP.md",
    "COST_OPS.md", "EMAIL_POLICY.md", "EMAIL_WHITELIST.md",
    "MAGIC_COMMANDS.md", "SECURITY_PROTOCOLS.md", "TOOLS.md",
    "openclaw.json", "openclaw.plugin.json",
}
OC_DEFAULT_DIRS = {".git", "__pycache__", ".DS_Store", "node_modules", ".openclaw"}

# These files get full content even if normally excluded
OC_PREVIEW_FILES = {"SOUL.md", "USER.md", "MEMORY.md"}


# ── Infrastructure signal detection ───────────────────────────────────────────
# Scripts containing these strings are OC system crons — not user applications.

INFRA_SIGNALS = [
    "openclaw gateway", "evolve-admin", "git push", "git commit",
    "context prune", "heartbeat", "spend_alert", "cron_alert",
    "weekly_review", "gateway probe", "gateway restart", "launchctl",
    "evolve-repo", "backup", "git add", "openclaw update",
]

# Basenames (file stems, separator- and case-insensitive) of scripts that
# Evolve / OpenClaw install as pod infrastructure: gateway self-heal,
# usage/turn collectors, the repo puller, log trimmers, liveness/health
# probes, spend/cron alerts. These are platform-owned even when they live
# inside a bot's workspace (e.g. ``workspace/bin/gateway-selfheal.sh``) and
# must never be minted as — or counted as evidence for — a user application.
# Matched by EXACT normalized stem (plus a few unambiguous suffixes) so a
# real app whose name merely *contains* "monitor"/"report"/"status" never
# collides. See heal.py / deploy.py for the installers.
INFRA_SCRIPT_STEMS = frozenset({
    "gateway-selfheal", "gateway-self-heal", "selfheal",
    "liveness-ping", "log-trimmer",
    "turn-collector", "usage-collector", "repo-puller",
    "context-prune", "gateway-probe", "gateway-restart",
    "spend-alert", "cron-alert", "cron-exit-monitor",
})

# Unambiguous infra suffixes — a script whose normalized stem ends with one
# of these is platform machinery regardless of its prefix. ``-ping`` catches
# the gateway/watchdog liveness probes (e.g. ``<watchdog>_ping.sh``); the
# hyphen anchor means it matches only ``<x>-ping`` stems, never ``shipping``.
# NOTE: "-backup" is deliberately NOT here — "backup" is common in legit app
# filenames (photo_backup.py, db_backup.sh), and that over-drop would hit
# every bot; real per-bot config backups are a LaunchDaemon, never a workspace
# script, and the evolve-backup/ tree is already caught by PLATFORM_WRITTEN.
# Infra backups with custom names are still caught by the "backup" INFRA_SIGNAL
# (content), which _is_infrastructure_script checks.
INFRA_SCRIPT_SUFFIXES = ("-selfheal", "-self-heal", "-ping")


def _is_infra_script_path(path: str) -> bool:
    """True if a script's basename marks it as Evolve/OC infrastructure.

    Separator- and case-insensitive: ``sentry_ping.sh`` and
    ``gateway-selfheal.sh`` both match. Uses EXACT normalized stems plus a
    small set of unambiguous suffixes; it deliberately does NOT substring-
    match generic words like "monitor"/"report"/"status", so a real app
    named e.g. "System Monitor" (``system_monitor.py``) is never
    misclassified. Tolerates a leading ``<tag>:`` and a ``#anchor`` suffix.
    """
    s = re.sub(r"^[a-z_]+:\s*", "", str(path).strip()).split("#", 1)[0]
    stem = Path(s).stem.lower().replace("_", "-").strip("-")
    if not stem:
        return False
    if stem in INFRA_SCRIPT_STEMS:
        return True
    return any(stem.endswith(suf) for suf in INFRA_SCRIPT_SUFFIXES)

# Workspace root directories that are OC infrastructure, not user applications.
# NOTE: "ops" and "scripts" are intentionally NOT here — bots commonly use those
# names for custom application directories (e.g. Team_bot_a's ops/tasks/ task system).
OC_INFRA_DIRS = {
    "memory", "archive", "docs", ".git", "__pycache__",
    "node_modules", "slack_webhook_env", "evolve",
}

# Evolve's own platform-state trees that live INSIDE every bot's workspace but
# are written by the platform, never the bot — and never an app. ``evolve/``
# holds the Forge job inbox, audit outbox, observability layers, etc.;
# ``evolve-backup/`` holds config backups. Feeding their JSON to the discovery
# LLM makes it hallucinate phantom apps ("Evolve Forge", "Memory Continuity",
# "Audit Logging") and starves real apps of evidence. Extends the OC-infra
# exclusion first added in PR #2476. A path is excluded if any component
# matches — covering nested files like ``evolve/forge/inbox/j-*.json``.
EVOLVE_PLATFORM_TREES = {"evolve", "evolve-backup"}


def _is_infrastructure_script(content: str, path: str) -> bool:
    """Return True if a cron/script is OC/Evolve system infrastructure, not a
    user application.

    Matches on EITHER signal:
      - the basename (``INFRA_SCRIPT_STEMS`` / suffixes via
        :func:`_is_infra_script_path`) — works even when the script's content
        is unavailable. This is the fix for the #2705 gap: the discovery
        prompt's script list passes paths with empty content, so the
        content-only check always returned False and ``gateway-selfheal.sh`` /
        ``sentry_ping.sh`` reached the LLM and got minted as apps.
      - the content (``INFRA_SIGNALS``) — catches custom-named infra by what
        it does (gateway restarts, git push, backups, …).
    """
    path_lower = str(path).lower()
    if "evolve-repo" in path_lower or "evolve_admin" in path_lower:
        return True
    if _is_infra_script_path(path):
        return True
    content_lower = (content or "").lower()
    return any(sig in content_lower for sig in INFRA_SIGNALS)


# Two-tier platform-path classification. PR #2478 phase 1 introduced the
# broader "owned" tier after a pod survey (2026-06-09) found four shapes of
# OC/Evolve capability getting minted as bot apps by the LLM-driven scanner:
#
#   1. Platform telemetry outputs (memory/turns-*.jsonl, evolve/audit_outbox)
#   2. Platform-installed code (evolve/*.py — the Evolve runtime)
#   3. Pod-wide standing-instruction templates (HEARTBEAT.md, POD_CONDUCT.md)
#   4. Scanner state (manifests/.scan-status.json, manifests/_history)
#
# Tier A (WRITTEN) is the narrow set: files whose CONTENT is written by
# infrastructure at runtime. Stripping these from an app's evidence is
# always safe because a bot can't legitimately claim to "own" a file the
# platform writes underneath it. Used by L1 (LLM evidence filter), L2
# (stamper guard), and L3 (archival classifier).
#
# Tier B (OWNED) is the broader set: includes everything in Tier A plus
# static platform code, pod-wide templates, and scanner state. Some files
# in this tier (HEARTBEAT.md, AGENTS.md sections) are legitimately CITED
# by real apps as producer surfaces — that's why we DON'T use Tier B in
# L1/L2 (stripping HEARTBEAT.md from evidence would break Daily Logging).
# Tier B is used only by L3's footprint classifier, where the gate is
# "≥90% Tier-B AND no real producer surface" — the no-surface check is
# what prevents accidentally archiving a real app that happens to cite
# pod-wide templates.
#
# Patterns are glob-style with `**` supported (see _path_matches_any).
# To add a pattern: confirm the producer is platform code, document the
# producer in the inline comment beside the pattern, and (for Tier A)
# confirm a bot-app cannot legitimately CLAIM to own this path.

PLATFORM_WRITTEN_FILE_PATTERNS: tuple[str, ...] = (
    # Tier A — content written by pod-wide infrastructure at runtime.

    # /Users/Shared/openclaw-usage/turn-collector.py
    "memory/turns-*.jsonl",
    # packages/admin/evolve_admin/applications/audit_poller.py (admin daemon)
    "evolve/audit_outbox/**",
    "evolve/audit_inbox/**",
    # packages/analyzer/app_audit_runner.py (Tier-2 structural audit runner)
    "evolve/audits/**",
    "evolve/.audit_runner.lock",
    # Forge job queue — bot-driven manifest builds/deploys (build/candidate/
    # result job files: j-*.json). Platform-written, never an app.
    # packages/admin/evolve_admin/applications/forge_engine.py
    "evolve/forge/**",
    # Per-bot config/state backups mirrored by the backup daemon.
    # packages/admin/evolve_admin/applications/*backup* + ai.evolve.<bot>.backup
    "evolve-backup/**",
    # Defer queue (admin-side action arbitration)
    "evolve/defer-queue.jsonl",
    "evolve/defer-queue.jsonl.lock",
    "evolve/defer-archive.jsonl",
    # Manifest-reflex pipeline (admin-side manifest mutations)
    "evolve/manifest-reflex-queue.jsonl",
    "evolve/manifest-reflex-queue.jsonl.lock",
    "evolve/manifest-reflex-archive.jsonl",
    # Recommendation hints (per-bot, generated by analyzer)
    "evolve/rec-hints.json",
    # Observability layers — all written by the analyzer / admin
    "evolve/logs/**",
    "evolve/spans/**",
    "evolve/summaries/**",
    "evolve/recommendations/**",
    "evolve/cascade/**",
    "evolve/metrics/**",
    "evolve/turns/**",
    "evolve/investigations/**",
    "evolve/provider_audits/**",
    "evolve/skill_audits/**",
    # Scanner state — manifest store metadata + retired manifest archive
    "manifests/.scan-status.json",
    "manifests/_history/**",
)

# Tier B — everything in Tier A PLUS static platform code and pod-wide
# templates a bot hosts but doesn't author. Used only by L3 archival.
PLATFORM_OWNED_FILE_PATTERNS: tuple[str, ...] = PLATFORM_WRITTEN_FILE_PATTERNS + (
    # The Evolve runtime — packages/admin/evolve_admin/deploy.py installs
    # these scripts into every bot's workspace/evolve/ at deploy time.
    "evolve/*.py",
    # OC + Evolve LaunchDaemon plists installed by deploy.py
    "scripts/launchd/ai.openclaw.*.plist",
    "scripts/launchd/ai.evolve.*.plist",
    # Pod-wide standing-instruction surfaces — templates that the platform
    # injects per-bot. NOTE: AGENTS.md / SOUL.md / MEMORY.md / USER.md
    # carry per-bot content and are NOT included here; a real app
    # (Daily Logging) legitimately cites them.
    "HEARTBEAT.md",
    "POD_CONDUCT.md",
    "INSTALLED_APPS.md",
)


def _path_matches_any(rel_path: str, patterns: tuple[str, ...]) -> bool:
    """Glob-match ``rel_path`` against ``patterns`` with `**` recursion.

    fnmatch.fnmatch treats `**` as a single `*`, which fails for paths
    like ``evolve/audit_outbox/_ingested/2026-06-09/rec-X.json`` against
    ``evolve/audit_outbox/**`` (the embedded `/` is not matched). This
    helper handles `<prefix>/**` patterns via startswith and falls back
    to fnmatch for everything else.

    Also strips a leading ``<lowercase_tag>:`` prefix the LLM/stamper
    sometimes emits (``"file: memory/turns-*.jsonl"``) and any leading
    slash so absolute and workspace-relative paths match the same way.
    """
    import fnmatch as _fnmatch
    cleaned = re.sub(r'^[a-z_]+:\s*', '', str(rel_path).strip()).lstrip("/")
    if not cleaned:
        return False
    for pat in patterns:
        if "/**" in pat:
            prefix, _, suffix = pat.partition("/**")
            if suffix == "":
                if cleaned == prefix or cleaned.startswith(prefix + "/"):
                    return True
            else:
                # Rare "<prefix>/**/<suffix>" — fall back to fnmatch.
                if _fnmatch.fnmatch(cleaned, pat.replace("**", "*")):
                    return True
        else:
            if _fnmatch.fnmatch(cleaned, pat):
                return True
    return False


def _is_platform_written_path(rel_path: str) -> bool:
    """Tier A: ``rel_path`` is written by pod-wide infrastructure.
    Safe to strip from any app's evidence. See PLATFORM_WRITTEN_FILE_PATTERNS."""
    return _path_matches_any(rel_path, PLATFORM_WRITTEN_FILE_PATTERNS)


def _is_platform_owned_path(rel_path: str) -> bool:
    """Tier B: ``rel_path`` is platform-owned (Tier A plus static platform
    code, pod-wide templates, scanner state). Used only by L3 archival —
    NOT by L1/L2 stripping, since legitimate apps cite some Tier-B paths
    as producer surfaces (Daily Logging cites HEARTBEAT.md). See
    PLATFORM_OWNED_FILE_PATTERNS."""
    return _path_matches_any(rel_path, PLATFORM_OWNED_FILE_PATTERNS)


# ── App-evidence floor ───────────────────────────────────────────────────────
# One principled gate: a file counts as *application* evidence only if it is
# NOT a known non-application class. A discovered cluster whose evidence is
# entirely non-application (infra scripts, OC identity/system files, the
# manifest store, bare skill credentials) is the platform / OC system showing
# through the workspace — not a user app — and must never be minted (#2705).
#
# Keeping every non-app class in ONE function (not scattered across discovery,
# dedup, stamp, and archival) is the agent-legibility contract: one verifier,
# bounded, and a new class is a one-line addition. See docs/applications-vs-
# skills.md for the skill-vs-app doctrine the (e) rule encodes.

# OC identity / system files: per-bot identity + standing-instruction surfaces
# the OC runtime owns. A cluster pointing only at these is an OC *system
# function* (memory persistence, session startup, bot identity), not a user
# app. Matched on basename; any "#anchor" suffix is stripped first so
# "AGENTS.md#Memory" matches "AGENTS.md".
_OC_IDENTITY_SYSTEM_BASENAMES = frozenset(
    n.lower() for n in (
        OC_DEFAULT_FILES | OC_PREVIEW_FILES | {"POD_CONDUCT.md", "INSTALLED_APPS.md"}
    )
)

# Skill / integration credential + config artifacts. By the applications-vs-
# skills doctrine, a bare skill config is a *capability*, not an
# *application* — it becomes an app only when bot-authored orchestration sits
# on top. So these count as non-app evidence ONLY when the cluster carries no
# orchestration script (gated in _app_evidence_files). Matched on a config-
# shaped extension + an unambiguous credential token; kept tight so a generic
# app data file ("tokens.json", "settings.json") is NOT caught.
_SKILL_CONFIG_TOKENS = (
    "oauth", "client_secret", "client-secret",
    "service_account", "service-account",
    "credentials", "webhook", "apikey", "api_key", "api-key",
)
_SKILL_CONFIG_EXTS = frozenset(
    {".json", ".yaml", ".yml", ".env", ".cfg", ".ini", ".toml"}
)

# Bot-authored orchestration: an executable that is NOT Evolve/OC infra. Its
# presence is what turns a bare skill config into an application (Communication
# Hub keeps its Slack config because communication_hub.py rides on top).
_ORCHESTRATION_SCRIPT_EXTS = frozenset(
    {".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".ts", ".rb"}
)


def _clean_evidence_path(path: str) -> str:
    """Strip the LLM/stamper ``<tag>:`` prefix, any ``#anchor`` suffix, and
    surrounding slashes. Returns ``""`` for empty/whitespace input."""
    s = re.sub(r"^[a-z_]+:\s*", "", str(path).strip())
    s = s.split("#", 1)[0].strip().strip("/")
    return s


def _is_oc_identity_system_file(path: str) -> bool:
    """True if ``path`` is an OC identity / system / standing-instruction file
    (SOUL/USER/MEMORY/AGENTS/HEARTBEAT/…). Never application evidence."""
    cleaned = _clean_evidence_path(path)
    if not cleaned:
        return False
    return Path(cleaned).name.lower() in _OC_IDENTITY_SYSTEM_BASENAMES


def _is_manifest_store_path(path: str) -> bool:
    """True if ``path`` is under the per-bot ``manifests/`` app-manifest store.
    A cluster citing manifest files has 'discovered' the store itself, not an
    app (the 'Infrastructure Manifest Tracking' false positive). Matches
    ``manifests`` as ANY path segment so workspace-prefixed
    (``workspace/manifests/i-x.json``) and absolute paths are caught too —
    ``manifest.json`` (singular) and ``ops/manifests_helper.py`` are not."""
    cleaned = _clean_evidence_path(path)
    return "manifests" in [p for p in cleaned.split("/") if p]


def _is_bare_skill_config(path: str) -> bool:
    """True if ``path`` looks like a skill/integration credential or config
    file (OAuth json, webhook config, service-account key, .env)."""
    cleaned = _clean_evidence_path(path)
    if not cleaned:
        return False
    p = Path(cleaned)
    if p.suffix.lower() not in _SKILL_CONFIG_EXTS:
        return False
    name = p.name.lower()
    return any(tok in name for tok in _SKILL_CONFIG_TOKENS)


def _is_orchestration_script(path: str) -> bool:
    """True if ``path`` is a bot-authored orchestration script — an executable
    (.py/.sh/.js/…) that is NOT Evolve/OC infrastructure."""
    cleaned = _clean_evidence_path(path)
    if not cleaned:
        return False
    if Path(cleaned).suffix.lower() not in _ORCHESTRATION_SCRIPT_EXTS:
        return False
    return not _is_infra_script_path(cleaned)


# A file extension that starts with a letter (so a version fragment like
# "v1.5" → ".5" is NOT treated as a path). Used to pick the real file token
# out of a noisy citation.
_CITATION_EXT_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}$")


def _citation_paths(raw: str) -> list[str]:
    """Return the file-path token(s) inside one evidence/footprint citation.

    A CLEAN single-token citation (``"scripts/tasks.py"``,
    ``"AGENTS.md#Memory"``) returns just itself, cleaned — identical to the
    long-standing :func:`_clean_evidence_path` behavior, so every existing
    clean-path caller is unaffected.

    A NOISY multi-token citation is the case this exists for: the scanner/LLM
    sometimes wraps a real path in a cron schedule and prose, e.g.
    ``"gateway-selfheal.sh (cron job */15)"`` or
    ``"*/30 * * * * /Users/x/sentry_ping.sh (cron job)"``. A naive
    ``Path(s).name`` read of the whole string picks up the trailing prose
    (``"15)"``) and MISSES the script, so an infra-script citation was being
    silently miscounted as application evidence (the live ``Gateway
    Management`` false-positive). We instead split on whitespace and keep only
    tokens that carry a real file extension — the cron/prose tokens (``*/15``,
    ``(cron``) fall away and the script/config/markdown path survives for
    classification by the floor helpers.

    Assumes a path token has no internal spaces (true of the scanner's on-disk
    citations — infra is ``cron-exit-monitor.sh``, not ``cron-exit monitor.sh``);
    a space-containing path in a NOISY citation keeps only its last component.
    """
    cleaned = _clean_evidence_path(raw)
    if not cleaned:
        return []
    if not re.search(r"\s", cleaned):
        return [cleaned]
    out: list[str] = []
    for tok in re.split(r"\s+", cleaned):
        tok = tok.strip().strip("'\"()[]{},;")
        if not tok:
            continue
        if _CITATION_EXT_RE.search(Path(tok).name):
            c = _clean_evidence_path(tok)
            if c:
                out.append(c)
    return out


def _app_evidence_files(evidence_files: list[str]) -> list[str]:
    """Return only the files in ``evidence_files`` that count as *application*
    evidence — the app-evidence floor.

    Excludes every non-application class:
      (a) platform-written / platform-owned paths (Tier A + Tier B)
      (b) Evolve/OC infrastructure scripts (gateway-selfheal, sentry_ping, …)
      (c) OC identity / system files (SOUL/USER/MEMORY/AGENTS/HEARTBEAT/…)
      (d) the ``manifests/`` app-manifest store
      (e) bare skill/integration credentials/config — but ONLY when the
          cluster carries no bot-authored orchestration script (doctrine: a
          skill becomes an app when orchestration rides on top).

    Each file is classified independently EXCEPT the (e) skill-config rule,
    which is gated on a single cluster-wide ``has_orchestration`` computed up
    front — so the result is order-independent. Returns ``[]`` when nothing
    qualifies; callers treat an empty result as "no application here".
    """
    cleaned = [c for ev in (evidence_files or []) for c in _citation_paths(ev)]
    has_orchestration = any(_is_orchestration_script(s) for s in cleaned)
    kept: list[str] = []
    for s in cleaned:
        if _is_platform_written_path(s) or _is_platform_owned_path(s):
            continue
        if _is_infra_script_path(s):
            continue
        if _is_oc_identity_system_file(s):
            continue
        if _is_manifest_store_path(s):
            continue
        if not has_orchestration and _is_bare_skill_config(s):
            continue
        kept.append(s)
    return kept


def _is_2705_nonapp_class(path: str) -> bool:
    """True if ``path`` is one of the #2705 non-application classes: an
    Evolve/OC infra script, an OC identity/system file, the manifest store, or
    a bare skill config.

    Tier-A/B platform-written paths are deliberately EXCLUDED here: those are
    handled by L3 Rules 1 & 2, and excluding them keeps Rule 3 from disturbing
    the platform-output-monitor case (an operator manifest legitimately
    pointing at memory/turns-*.jsonl — see
    test_l3_skips_manifest_with_operator_content_even_if_files_are_platform)."""
    return (
        _is_infra_script_path(path)
        or _is_oc_identity_system_file(path)
        or _is_manifest_store_path(path)
        or _is_bare_skill_config(path)
    )


def _is_hard_nonapp_class(path: str) -> bool:
    """True if ``path`` is a non-app class that NO legitimate application ever
    cites: an Evolve/OC infra script or the manifest store.

    Narrower than :func:`_is_2705_nonapp_class` by TWO classes, and both
    omissions are load-bearing for L3 Rule 3's shield:

      - OC identity/system files (AGENTS.md / HEARTBEAT.md / MEMORY.md) are
        omitted because a legit hand-written recurring-behavior app (a "Morning
        Briefing" living in a HEARTBEAT.md section) legitimately cites them —
        those phantoms are told apart from real behavior apps by the OC-system
        NAME test instead (:func:`_name_is_oc_system_function`).
      - Bare skill credentials/configs (google-oauth.json, slack-webhook.json)
        are omitted because they are NOT unconditionally non-app: the floor
        (:func:`_app_evidence_files`) treats a skill config as evidence the
        moment a bot-authored orchestration script rides on top, and a legit
        behavior app may simply read a credential file. A skill config that is
        genuinely a bare capability (no orchestration) is already swept — such
        a manifest has no app evidence and, lacking any behavior, is not
        behavior-shaped, so the shield never engages.

    What remains — infra scripts and the manifest store — are the only classes
    NO real app ever owns. Citing one (in evidence OR via a scheduled-action
    target) is an unambiguous phantom tell that defeats EVERY shield, including
    a concrete producer surface (an infra LaunchAgent attributed back to the
    phantom would otherwise look like a real cron surface)."""
    return _is_infra_script_path(path) or _is_manifest_store_path(path)


# Manifest sources that indicate a human/operator (or gallery/RSI/forge)
# authored the manifest. L3 archival never touches these — only scanner-
# discovered manifests are eligible. A missing/empty source is treated as
# scanner output (legacy detections predate the source field).
_OPERATOR_AUTHORED_SOURCES = frozenset({
    "user_created", "user-defined", "file_imported", "imported",
    "gallery_installed", "rsi_proposed", "bot_created",
})


def _is_operator_authored(manifest: dict) -> bool:
    """True if the manifest was authored by an operator / gallery / RSI / bot
    instruction (not the auto-scanner). See _OPERATOR_AUTHORED_SOURCES."""
    return str(manifest.get("source") or "").strip().lower() in _OPERATOR_AUTHORED_SOURCES


def _is_defined(manifest: dict) -> bool:
    """True if an operator has vouched this manifest as the source of truth —
    ``definition_status == "defined"`` (Defined/Discovered lifecycle, Bite 1;
    docs/spec-apps-meta-2026-06-13.md §9).

    This is the EXISTENCE GUARANTEE key, PARALLEL to ``_is_operator_authored``
    but on the orthogonal source-of-truth axis: a defined manifest is NEVER
    L3-archived, even with zero files on disk. Distinct from
    ``_is_operator_authored`` (which keys on the immutable creation ``source``):
    a scanner-discovered app (source="discovered") that an operator PROMOTES
    gains existence protection it never had from source alone, while an existing
    operator-authored app stays shielded by source even though migration landed
    it at "discovered". The two shields are additive.

    Literal ``"defined"`` mirrors manifest.MANIFEST_DEFINITION_DEFINED — the
    scanner keeps these as module-level literals (cf. _OPERATOR_AUTHORED_SOURCES)
    rather than importing the constants."""
    return str(manifest.get("definition_status") or "").strip().lower() == "defined"


# OC system-function name tokens — an "app" whose name is built from these is
# describing how the OpenClaw runtime itself works (memory persistence, session
# startup, bot identity), never a user application. This denies the scheduled-
# action floor exemption to OC-system phantoms: identity/standing-instruction
# sections legitimately contain schedule words ("review MEMORY.md daily"), so a
# zero-evidence "Memory Persistence" / "Session Startup" / "Persistent Memory
# System" could otherwise sneak through the exemption via a single shared token.
_OC_SYSTEM_FUNCTION_TOKENS = frozenset({
    "memory", "persistence", "persistent", "session", "startup",
    "identity", "bootstrap", "heartbeat", "conduct", "soul",
})


def _name_is_oc_system_function(name: str) -> bool:
    """True when an app NAME is MAJORITY OC runtime-function tokens
    (memory / persistence / session / startup / identity / …) — i.e. it
    describes how the OpenClaw runtime itself works, never a user application.

    The majority (not ANY) test avoids over-dropping a legit zero-file
    behavior app that merely contains one such word: "Memory Lane Journal"
    {memory,lane,journal} is 1/3 → False (legit); "Memory Persistence"
    {memory,persistence} is 2/2 and "Persistent Memory System" is 2/3 → True
    (phantom). Single source of truth shared by the Phase-2 generation floor
    (:func:`_floor_behavior_exempt`) and the L3 reconcile archival decision,
    so reconcile retires exactly the OC-system phantoms the floor now blocks
    at mint."""
    name_tokens = {t for t in re.split(r"[\s_\-]+", (name or "").lower()) if len(t) > 3}
    if not name_tokens:
        return False
    return len(name_tokens & _OC_SYSTEM_FUNCTION_TOKENS) * 2 > len(name_tokens)


def _floor_behavior_exempt(det: "DetectedApplication", candidates: list[dict]) -> bool:
    """True if a zero-app-evidence cluster is a genuine recurring-behavior app
    (Protein Reminder, Morning Briefing) whose producer surface materializes in
    Phase 4 as a scheduled_action — and is NOT an OC system function.

    Stricter than a bare :func:`_candidate_relates_to_app` sweep: it FIRST
    rejects any name that is majority OC system-function tokens (via
    :func:`_name_is_oc_system_function`), then requires a relation to an
    extracted scheduled-action candidate. Without the denylist, the loose
    single-token candidate match let OC-system phantoms survive (the #2705
    adversarial finding)."""
    if _name_is_oc_system_function(det.name or ""):
        return False
    return any(_candidate_relates_to_app(c, det) for c in (candidates or []))


def _load_bound_spec(manifest: dict, shared_dir: Path | None) -> dict:
    """Load the raw Spec dict a v7-arc Instance is bound to.

    ``hydrate_v7_arc_instance`` overlays presentation + scheduled fields but
    NOT ``interface_contract`` / ``files`` / ``realized_files`` (its file list
    is built from the INSTANCE's realized_files). For L3 Rule 3 we need the
    Spec's real app surface + files so a legit CLI/script v7-arc app whose
    Instance is observational (empty realized_files) is never archived. Returns
    ``{}`` when not v7-arc, no shared_dir, or the Spec isn't on disk."""
    if manifest.get("manifest_shape") != "v7-arc" or shared_dir is None:
        return {}
    prov = manifest.get("provenance") or {}
    spec_id = prov.get("spec_id")
    spec_version = prov.get("spec_version")
    if not spec_id or not spec_version:
        return {}
    gallery = Path(shared_dir) / "gallery"
    candidates = [
        gallery / "local" / spec_id / f"{spec_version}.json",
        gallery / "builtin" / spec_id / f"{spec_version}.json",
    ]
    src_pod = prov.get("source_pod_id")
    if src_pod:
        candidates.insert(
            1, gallery / "imported" / src_pod / spec_id / f"{spec_version}.json"
        )
    for p in candidates:
        try:
            if p.is_file():
                d = json.loads(p.read_text())
                if isinstance(d, dict):
                    return d
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _detect_recurring_structure(content: str) -> bool:
    """Return True if content has dated entries or repeated structural patterns."""
    import re as _re
    date_pattern = _re.compile(r'##\s+\d{4}-\d{2}-\d{2}')
    table_rows = content.count("| ")
    header_count = content.count("## ")
    return bool(date_pattern.search(content)) or table_rows > 6 or header_count > 4


# ── v13: scheduled-action extraction ─────────────────────────────────────────

# Heuristic indicators that a markdown section describes a recurring behavior.
# We use these to pre-filter sections before sending them to the LLM, so the
# LLM only sees plausible candidates rather than every heading in the file.
_SCHEDULE_HINT_PATTERNS = [
    r"\b(?:every|each)\s+(?:morning|evening|night|day|hour|week|month)\b",
    r"\bdaily\b",
    r"\bweekly\b",
    r"\bnightly\b",
    r"\b(?:at|by)\s+\d{1,2}\s*(?:am|pm|:\d{2})\b",
    r"\bschedul(?:e|ed|es|ing)\b",
    r"\bremind(?:er|ers|s|ing)?\b",
    r"\bcron\b",
    r"\bperiodically\b",
    r"\bautomatically\s+(?:post|send|run|check)\b",
    r"\bonce\s+(?:a|per)\s+(?:day|week|month|hour)\b",
]
_SCHEDULE_HINT_RE = None  # lazily compiled

# Surface files whose recurring-behavior content gets extracted as candidates.
# These are the bot's standing-routine layer — the failure surface the
# protein-reminder regression hit (heartbeat clobber → silent stop).
_SCHEDULED_ACTION_SURFACES = (
    "AGENTS.md",
    "HEARTBEAT.md",
    "POD_CONDUCT.md",
    "SOUL.md",
)


def _schedule_hint_re():
    """Lazy-compile the combined schedule-hint regex."""
    global _SCHEDULE_HINT_RE
    if _SCHEDULE_HINT_RE is None:
        import re as _re
        _SCHEDULE_HINT_RE = _re.compile(
            "|".join(f"(?:{p})" for p in _SCHEDULE_HINT_PATTERNS),
            _re.IGNORECASE,
        )
    return _SCHEDULE_HINT_RE


def _split_markdown_sections(text: str) -> list[dict]:
    """Split a markdown document into sections by heading.

    Each section runs from a heading line to the next same-or-higher-level
    heading. Returns a list of dicts:
      {heading, level, body, line_start, line_end, sha256}

    Body excludes the heading line itself, so the sha is stable across
    formatting variants of the heading. Content above the first heading
    (e.g. a top-of-file preamble) gets a synthetic section with heading=""
    and level=0.
    """
    import hashlib as _hashlib
    import re as _re

    heading_re = _re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    lines = text.splitlines()
    sections: list[dict] = []

    # Preamble (before any heading)
    starts: list[tuple[int, int, str]] = []  # (line_index, level, heading_text)
    for i, line in enumerate(lines):
        m = heading_re.match(line)
        if m:
            starts.append((i, len(m.group(1)), m.group(2).strip()))

    if not starts:
        # No headings — treat the whole file as one anonymous section
        body = text
        sections.append({
            "heading": "",
            "level": 0,
            "body": body,
            "line_start": 0,
            "line_end": len(lines),
            "sha256": _hashlib.sha256(body.encode("utf-8")).hexdigest(),
        })
        return sections

    # Preamble (lines before the first heading)
    if starts[0][0] > 0:
        body = "\n".join(lines[: starts[0][0]])
        sections.append({
            "heading": "",
            "level": 0,
            "body": body,
            "line_start": 0,
            "line_end": starts[0][0],
            "sha256": _hashlib.sha256(body.encode("utf-8")).hexdigest(),
        })

    for idx, (line_idx, level, heading_text) in enumerate(starts):
        # End is the next same-or-higher-level heading (or EOF)
        end = len(lines)
        for j in range(idx + 1, len(starts)):
            if starts[j][1] <= level:
                end = starts[j][0]
                break
        body = "\n".join(lines[line_idx + 1: end])
        sections.append({
            "heading": heading_text,
            "level": level,
            "body": body,
            "line_start": line_idx,
            "line_end": end,
            "sha256": _hashlib.sha256(body.encode("utf-8")).hexdigest(),
        })
    return sections


def _extract_scheduled_action_candidates(
    workspace: Path,
    bot_id: str,
) -> list[dict]:
    """Read the bot's standing-instruction surfaces, return schedule-hinted sections.

    Each candidate carries the source file, heading text (anchor), level,
    full body text, content sha (for drift detection), and the heuristic
    that matched (so the LLM gets a hint about what to extract).

    Returns an empty list if none of the surfaces exist. Pure-Python; no
    LLM dispatch. Best-effort — read failures are silently swallowed.
    """
    hint_re = _schedule_hint_re()
    candidates: list[dict] = []
    for fname in _SCHEDULED_ACTION_SURFACES:
        path = workspace / fname
        # exists_or_unreachable: an EACCES clamp on the workspace's .openclaw
        # parent makes a bare .exists() RAISE (Py3.12) — treat unreachable as
        # present and fall through to the guarded read below (which returns ""
        # and skips this surface), rather than letting it crash the extraction.
        if not exists_or_unreachable(path):
            continue
        try:
            text = _safe_read_as_bot(path, max_chars=20000)
        except Exception:
            continue
        if not text:
            continue
        sections = _split_markdown_sections(text)
        for sec in sections:
            # Combine heading + body for hint detection; a heading like
            # "Daily routines" by itself counts as a schedule signal.
            search_text = f"{sec['heading']}\n{sec['body']}"
            if not hint_re.search(search_text):
                continue
            candidates.append({
                "file_path": fname,
                "heading": sec["heading"],
                "level": sec["level"],
                "body": sec["body"][:1500],   # cap for prompt budget
                "section_sha256": sec["sha256"],
                "line_start": sec["line_start"],
                "line_end": sec["line_end"],
            })
    return candidates


def _snapshot_launchctl_labels(bot_id: str) -> list[str]:
    """Snapshot loaded launchd labels for the bot user.

    Returns an empty list on any failure. Mirrors the runner-side helper
    in app_audit_runner.py; we snapshot here too so the scanner can carry
    the label set into the LLM context (useful when a behavior references
    a specific launchd job).

    Goes through the process-wide Scheduler seam's ``list()`` verb, so a Linux
    pod resolves the injected SystemdScheduler and returns the REAL system-unit
    label set instead of an empty list. Per design §3 the bot gateways are
    systemd system units (``User=<bot>``), all visible to the daemon — no
    run-as-bot indirection is needed there, and ``list()`` answers truthfully.

    On macOS the probe must run AS THE BOT USER (it enumerates the bot's own
    launchd domain), which the adapter's sudo prefix can't express — so we
    guarded-derive an unsudo'd launchd adapter and inject a runner that wraps
    the adapter-built argv in ``sudo -u <bot_user>`` with the historical cwd
    (/Users/Shared — a directory the bot user can read; see the sudo-cwd gotcha
    in CLAUDE.md) and the 5s probe timeout. The run-as-bot runner is load-bearing
    on launchd, so this is byte-identical to the pre-seam
    ``LaunchdScheduler(use_sudo=False, runner=_as_bot_runner)`` argv. Same
    guarded-derive shape as mcp_service._scheduler_nosudo; the list feeds LLM
    scan context only — read-only, never destructive, ``[]`` on any failure.
    """
    sched = get_scheduler()
    if not isinstance(sched, LaunchdScheduler):
        # Non-launchd pod (systemd): the bot's gateway is a system unit the
        # daemon already sees, so the portable list() verb is truthful and the
        # run-as-bot indirection is unnecessary.
        try:
            return sched.list()
        except Exception:
            return []

    try:
        bot_user = get_bot_user(bot_id, load_network())
    except Exception:
        return []

    def _as_bot_runner(argv: list[str]) -> tuple[int, str, str]:
        try:
            r = subprocess.run(
                ["sudo", "-u", bot_user, *argv],
                capture_output=True, text=True, timeout=5, cwd=get_profile().scratch_dir,
            )
            return r.returncode, r.stdout, r.stderr
        except Exception as e:
            return 1, "", str(e)

    return LaunchdScheduler(use_sudo=False, runner=_as_bot_runner).list()


def _enumerate_launch_agents(bot_id: str) -> list[dict]:
    """Enumerate plist files under ``~/Library/LaunchAgents/`` for the bot user.

    Returns a list of dicts — one per ``.plist`` file. Each dict carries
    enough to support spec-forge-side-effects §6.1 attribution:

      label: str               — plist Label, used for ``com.{bot}.{app}.*`` match
      plist_path: str          — on-disk path so the verifier can find it later
      program_args: list[str]  — used for script-path attribution (step 2)
      start_interval: int|None — seconds between fires (StartInterval)
      start_calendar_interval: dict|None  — StartCalendarInterval (hour/minute/etc.)
      run_at_load: bool        — whether the agent fires on load
      raw: dict                — full parsed plist for any extra fields the
                                 attributor / verifier needs later

    Returns ``[]`` on any failure (missing dir, permission denied, malformed
    plist) — same posture as ``_snapshot_launchctl_labels``. The downstream
    LLM clustering still has plenty of signal from filesystem walks.
    """
    try:
        bot_user = get_bot_user(bot_id, load_network())
    except Exception:
        return []

    la_dir = Path(f"/Users/{bot_user}/Library/LaunchAgents")
    # exists_or_unreachable: a bare .exists() RAISES under an EACCES clamp
    # (Py3.12); treat unreachable as present and fall through to the guarded
    # glob below, which returns [] when the dir can't actually be listed.
    if not exists_or_unreachable(la_dir):
        return []

    entries: list[dict] = []
    try:
        plist_paths = sorted(la_dir.glob("*.plist"))
    except (OSError, PermissionError):
        # Can't traverse the dir, and there is no sudo fallback: this runs as
        # the bot user (the application_scanner.py scan subprocess) or as root
        # (the evolve-admin CLI), both of which list the bot's own
        # LaunchAgents directly. The evolve daemon never runs this in-process
        # and has no `sudo -u <bot>` grant, so the former `sudo -u <bot> ls`
        # fallback only ran — and failed silently — in a context that never
        # reaches here (CLAUDE.md §"File Access Pattern").
        return []

    for plist_path in plist_paths:
        raw_bytes = _read_plist_bytes(plist_path)
        if not raw_bytes:
            continue
        try:
            parsed = plistlib.loads(raw_bytes)
        except (plistlib.InvalidFileException, ValueError, Exception):
            continue
        if not isinstance(parsed, dict):
            continue
        entries.append({
            "label": parsed.get("Label", "") or "",
            "plist_path": str(plist_path),
            "program_args": list(parsed.get("ProgramArguments") or []),
            "start_interval": parsed.get("StartInterval"),
            "start_calendar_interval": parsed.get("StartCalendarInterval"),
            "run_at_load": bool(parsed.get("RunAtLoad", False)),
            "raw": parsed,
        })
    return entries


def _read_plist_bytes(plist_path: Path) -> bytes:
    """Read a plist file directly, returning ``b""`` on any read error.

    Runs as the bot user (the ``application_scanner.py`` scan subprocess) or
    as root (the ``evolve-admin`` CLI) — both read the bot's own
    ``~/Library/LaunchAgents`` plists directly. There is deliberately no
    ``sudo -u <bot>`` fallback: the ``evolve`` daemon never runs this
    in-process and has no such grant, and there is no root grant for
    ``Library/LaunchAgents`` either, so the old fallback only ever failed
    silently (CLAUDE.md §"File Access Pattern"). ``b""`` is what the caller
    already treats as "skip this plist".
    """
    try:
        return plist_path.read_bytes()
    except (OSError, PermissionError):
        return b""


def _read_openclaw_hooks(bot_id: str) -> list[dict]:
    """DEPRECATED v17 — see _read_heartbeat_md_sections.

    OpenClaw has no top-level ``hooks`` field; the prior v16 design was
    structurally wrong (PR 9 spec). Kept for one version so in-flight
    callers don't ImportError; always returns ``[]``.
    """
    del bot_id
    return []


# v17 helpers for the scanner attribution swap. The marker shape matches
# install_helpers._MANAGED_MARKER_RE; pkg_id is the load-bearing piece
# (attribution) — the rest of the marker is optional.
_MANAGED_MARKER_PKG_RE = re.compile(
    r"<!--\s*evolve-managed(?::\s*(?P<kv>[^>]*))?\s*-->",
    re.IGNORECASE,
)
_PKG_KV_RE = re.compile(r"\bpkg\s*=\s*(?P<pkg>p-[a-zA-Z0-9_-]+)")
_BACKTICK_CMD_RE = re.compile(r"`([^`]+)`")


def _read_heartbeat_md_sections(bot_id: str) -> list[dict]:
    """Enumerate evolve-managed sections in the bot's HEARTBEAT.md / AGENTS.md.

    Replaces ``_read_openclaw_hooks``. For each evolve-managed section
    (a ``##``+ heading whose body contains an ``<!-- evolve-managed -->``
    marker), returns a dict:

        {
          file: "HEARTBEAT.md" | "AGENTS.md",
          anchor: "Task Manager — Heartbeat Check",  # heading text, no #s
          pkg_id: "p-9bfa1c84" | "",   # parsed from marker, "" if absent
          body: "<full section body, trimmed>",
          command_hint: "python3 scripts/tasks.py check" | "",
        }

    ``pkg_id`` is the attribution key: an app whose manifest carries the
    same ``pkg_id`` claims this section. When absent (e.g. operator-
    authored managed sections without forge attribution), the LLM
    clustering pass falls back to fuzzy matching by command.

    Returns ``[]`` on any failure — the filesystem walk still has plenty
    of signal for the LLM phase.
    """
    try:
        bot_user = get_bot_user(bot_id, load_network())
    except Exception:
        return []

    workspace = Path(f"/Users/{bot_user}/.openclaw/workspace")
    # exists_or_unreachable: an EACCES clamp on .openclaw makes a bare .exists()
    # RAISE (Py3.12) — treat unreachable as present and fall through to the
    # guarded per-file reads below (which skip on EACCES), never crash.
    if not exists_or_unreachable(workspace):
        return []

    sections: list[dict] = []
    for filename in ("HEARTBEAT.md", "AGENTS.md"):
        target = workspace / filename
        if not exists_or_unreachable(target):
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        sections.extend(_extract_managed_sections(text, filename))
    return sections


def _extract_managed_sections(text: str, filename: str) -> list[dict]:
    """Pure function: parse markdown text for evolve-managed sections.

    Split out from ``_read_heartbeat_md_sections`` for testability.
    """
    heading_re = re.compile(
        r"^(?P<level>#{2,4})\s+(?P<anchor>.+?)\s*$", re.MULTILINE,
    )
    matches = list(heading_re.finditer(text))
    out: list[dict] = []
    for i, m in enumerate(matches):
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start: body_end].strip()
        marker_m = _MANAGED_MARKER_PKG_RE.search(body)
        if not marker_m:
            continue
        # Extract pkg_id from the marker if present.
        pkg_id = ""
        kv = marker_m.group("kv") or ""
        pkg_kv = _PKG_KV_RE.search(kv)
        if pkg_kv:
            pkg_id = pkg_kv.group("pkg")
        # First backticked snippet in the body is usually the command.
        cmd_m = _BACKTICK_CMD_RE.search(body)
        command_hint = cmd_m.group(1).strip() if cmd_m else ""
        out.append({
            "file": filename,
            "anchor": m.group("anchor").strip(),
            "pkg_id": pkg_id,
            "body": body,
            "command_hint": command_hint,
        })
    return out


def _read_openclaw_hooks_legacy_impl(bot_id: str) -> list[dict]:
    """Pre-v17 implementation, kept temporarily for tests that pin the
    deprecated openclaw.json reading. Not exported; remove in v18."""
    try:
        bot_user = get_bot_user(bot_id, load_network())
    except Exception:
        return []

    oc_json = Path(f"/Users/{bot_user}/.openclaw/openclaw.json")
    raw_bytes = b""
    try:
        raw_bytes = oc_json.read_bytes()
    except (OSError, PermissionError):
        # ACL read failed (pre-deploy bot, or ACL not yet set). Fall back to
        # the root `sudo /bin/cat` grant — NOT `sudo -u <bot>` (evolve has no
        # such grant). openclaw.json is granted as root via
        # `evolve ALL=(root) NOPASSWD: /bin/cat /Users/*/.openclaw/openclaw.json`
        # (CLAUDE.md §"File Access Pattern").
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(oc_json)],
                capture_output=True, timeout=5, cwd=get_profile().scratch_dir,
            )
            if r.returncode == 0:
                raw_bytes = r.stdout
        except Exception:
            return []
    if not raw_bytes:
        return []

    try:
        config = json.loads(raw_bytes)
    except (json.JSONDecodeError, ValueError):
        return []

    hooks_block = (config or {}).get("hooks")
    if not isinstance(hooks_block, dict):
        return []

    flat: list[dict] = []
    for event_name, entries in hooks_block.items():
        # OC uses non-list values for some hook config (e.g. the bare
        # `allowConversationAccess: true` flag). Only walk list-shaped
        # event entries — bare flags aren't hooks.
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            flat.append({
                "event": event_name,
                # OC entries use either `command` or `cmd` depending on
                # version; tolerate both so the attribution works across
                # upgrades.
                "command": entry.get("command", entry.get("cmd", "")) or "",
                "label": entry.get("label", "") or "",
                "original_entry": entry,
            })
    return flat


# ── LLM target resolution (provider-agnostic via infra_llm, #3466) ────────────

def _resolve_llm(tier: str = "tier3") -> tuple[str, str]:
    """Resolve ``(model, api_key)`` for a scan LLM call via the
    provider-agnostic ``infra_llm`` resolver (pod tier config first, then
    the primary bot's credentialed providers; per-provider env vars —
    e.g. the SETENV injection from the admin server's scan subprocess
    dispatch — override, so this works even where the bot user can't
    read the primary bot's auth store).

    Returns ``("", "")`` when no LLM provider is credentialed — callers
    degrade to a structural scan rather than hard-failing.
    """
    try:
        from infra_llm import resolve_infra_llm  # type: ignore
        target = resolve_infra_llm(tier)
    except Exception:
        return "", ""
    if target is None:
        return "", ""
    return target.model, target.api_key


def _call_llm(model: str, prompt: str, api_key: str, timeout: int = 60) -> str:
    """
    Make one LLM call via ``infra_llm.complete``. Returns the text
    response, or "" on failure.

    ``model`` is the provider-qualified id from :func:`_resolve_llm`
    (the provider rides in the prefix, so ``(model, api_key)`` string
    plumbing — including the ``purpose_classifier`` ``llm_fn`` seam —
    round-trips the full target). A bare model id or empty key yields
    "" (no provider is ever presumed).
    """
    if not api_key or "/" not in model:
        return ""
    from infra_llm import InfraLLMTarget, complete  # type: ignore

    target = InfraLLMTarget(
        provider=model.split("/", 1)[0], model=model, api_key=api_key
    )
    try:
        return complete(target, prompt=prompt, max_tokens=4096, timeout=timeout)
    except Exception as e:
        print(f"[scanner] LLM call failed: {e}", flush=True)
        return ""


# ── Purpose/fit classifier seam (app-scan bite D1) ────────────────────────────
# The classifier judges a manifest goal-application vs capability/skill. It
# routes through a STRONGER tier than Phase-2 discovery: tier3 (haiku) already
# FAILS this exact judgment — it proposed "Google Services Integration" (a bare
# OAuth capability) as an application. tier2 is the workhorse/standard tier,
# resolved through the model-tier seam so no provider/model literal lands in
# logic (invariant: no-provider-literals-in-logic). The LLM call goes through
# the injectable ``_call_llm`` seam so tests run fully offline.
_CLASSIFIER_TIER = "tier2"


def _stamp_app_kind(
    manifest: dict,
    *,
    model: str,
    api_key: str,
    bound_spec: dict | None = None,
    extra_files: list[str] | None = None,
    log=None,
) -> bool:
    """Classify a manifest goal-application vs capability and stamp the
    ``app_kind`` + ``classification`` block on it in place.

    Returns True when a block was stamped (the manifest was judged), False
    when it was left at the inert "application" default — no signal to judge,
    no api key, or a hard LLM/parse failure, all of which are retried on the
    next scan. Never raises: classification must never break a scan.
    """
    try:
        from .purpose_classifier import classify_app_kind
        block = classify_app_kind(
            manifest,
            llm_fn=_call_llm,
            model=model,
            api_key=api_key,
            bound_spec=bound_spec,
            extra_files=extra_files,
            model_tier=_CLASSIFIER_TIER,
        )
    except Exception as exc:  # noqa: BLE001 — never block a scan on classification
        if log is not None:
            log(f"[scanner] purpose/fit classify error: {exc}")
        return False
    if block is None:
        return False
    manifest["classification"] = block
    manifest["app_kind"] = block["kind"]
    return True


def _safe_read_as_bot(path: Path, max_chars: int = 3000) -> str:
    """Read a workspace file directly, returning ``""`` on any read error.

    Runs as the bot user (the ``application_scanner.py`` scan subprocess) or
    as root (the ``evolve-admin`` CLI); both read the bot's own workspace
    directly, and the ``evolve`` daemon has ACL read on ``.openclaw/`` (where
    the workspace lives). There is deliberately no ``sudo -u <bot>`` fallback —
    ``evolve`` has no such grant, so it only ever failed silently
    (CLAUDE.md §"File Access Pattern").
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except OSError:
        return ""


# ── Phase 1: Inventory collection ─────────────────────────────────────────────

def collect_inventory(workspace: Path, bot_id: str) -> WorkspaceInventory:
    """
    Phase 1 — collect a rich snapshot of the bot's workspace.
    Reads crontab (via sudo), SOUL.md, all scripts, and markdown files.
    Never raises — permission failures are silently swallowed.
    """
    inv = WorkspaceInventory(workspace=workspace, bot_id=bot_id)
    MAX_PREVIEW = 400
    MAX_FILE_SIZE = 256 * 1024

    def _safe_read(path: Path, max_chars: int = MAX_PREVIEW) -> str:
        # Direct read only — this runs as the bot user (the scan subprocess)
        # or root (the CLI), and the evolve daemon has ACL read on .openclaw/
        # where the workspace lives. No `sudo -u <bot>` fallback: evolve has
        # no such grant, so it only ever failed silently (CLAUDE.md §"File
        # Access Pattern").
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
        except OSError:
            return ""

    def _is_text(path: Path) -> bool:
        try:
            with path.open("rb") as f:
                f.read(512).decode("utf-8")
            return True
        except (UnicodeDecodeError, OSError):
            return False

    # SOUL.md and USER.md — read fully for context
    inv.soul_content  = _safe_read(workspace / "SOUL.md",  max_chars=800)
    inv.user_md_content = _safe_read(workspace / "USER.md", max_chars=600)

    # AGENTS.md and MEMORY.md — read for additional context
    inv.agents_content = _safe_read(workspace / "AGENTS.md", max_chars=1000)
    inv.memory_md_content = _safe_read(workspace / "MEMORY.md", max_chars=600)

    # Cron jobs with script content — use rich collection
    inv.cron_jobs = _collect_crons(bot_id, workspace)
    # cron_entries: manifest-ready dicts (v5 format); file_id assigned later by migrate_manifest
    inv.cron_entries = [
        {"schedule": j["schedule"], "script": j["script_path"], "label": "", "file_id": ""}
        for j in inv.cron_jobs
    ]

    # Walk workspace
    try:
        for entry in sorted(workspace.rglob("*")):
            try:
                rel = entry.relative_to(workspace)
            except ValueError:
                continue

            parts = rel.parts
            if any(p.startswith(".") or p in OC_DEFAULT_DIRS for p in parts):
                continue

            if entry.is_dir():
                inv.directories.append(str(rel) + "/")
                continue

            if not entry.is_file():
                continue

            # Skip oversized files
            try:
                size = entry.stat().st_size
                if size > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue

            name = entry.name
            suffix = entry.suffix.lower()

            # Skip default OC files (except preview files)
            if name in OC_DEFAULT_FILES and name not in OC_PREVIEW_FILES:
                continue

            if suffix == ".py":
                inv.python_scripts.append(str(rel))
            elif suffix == ".sh":
                inv.shell_scripts.append(str(rel))
            elif suffix == ".md":
                # Skip SOUL.md and USER.md — already read above
                if name not in ("SOUL.md", "USER.md"):
                    preview = _safe_read(entry, max_chars=MAX_PREVIEW) if _is_text(entry) else "[binary]"
                    inv.markdown_files.append({"path": str(rel), "preview": preview, "size": size})
            else:
                inv.other_files.append(str(rel))

    except (PermissionError, OSError) as e:
        # The rglob walk itself is the EACCES point under a 0700 clamp: skip the
        # rest of the walk (whatever was inventoried before the raise is kept on
        # ``inv``) and LOG it, rather than silently confabulating an empty
        # workspace. (W10-G follow-up sweep.)
        _log.warning("scanner: workspace walk of %s unreachable (EACCES clamp?): %s", workspace, e)

    # Named workspace directories and recurring memory files — new signal types
    inv.named_dirs = _collect_named_dirs(workspace, bot_id)
    inv.memory_files = _collect_memory_files(workspace, bot_id)
    inv.json_stores = _collect_json_stores(workspace, bot_id)

    # v13: capture the full heartbeat / pod-conduct content for the LLM,
    # and pre-extract schedule-hinted sections so the LLM has a focused
    # list of candidate behaviors to promote into scheduled_actions[].
    inv.heartbeat_content = _safe_read(workspace / "HEARTBEAT.md", max_chars=12000)
    inv.pod_conduct_content = _safe_read(workspace / "POD_CONDUCT.md", max_chars=8000)
    try:
        inv.scheduled_action_candidates = _extract_scheduled_action_candidates(
            workspace, bot_id,
        )
    except Exception as e:
        print(f"[scanner] WARNING: scheduled-action extraction failed: {e}", flush=True)
        inv.scheduled_action_candidates = []
    try:
        inv.launchctl_labels = _snapshot_launchctl_labels(bot_id)
    except Exception:
        inv.launchctl_labels = []

    # v16: enumerate plist contents under ~/Library/LaunchAgents/ and the
    # `hooks` block of openclaw.json so the per-app attribution step
    # (generate_manifest_for_app below) can populate scheduled_actions[]
    # with the v16 mechanism + install_artifact fields. Both fall back to
    # [] on any failure — downstream code defaults to "scanner couldn't
    # see this install surface" rather than erroring.
    try:
        inv.launchd_entries = _enumerate_launch_agents(bot_id)
    except Exception as e:
        print(f"[scanner] WARNING: LaunchAgents enumeration failed: {e}", flush=True)
        inv.launchd_entries = []
    # v17: enumerate evolve-managed sections in workspace HEARTBEAT.md
    # / AGENTS.md. Replaces the prior openclaw.json hooks read (the
    # surface didn't exist in OC's schema — see PR 9 spec).
    try:
        inv.heartbeat_md_sections = _read_heartbeat_md_sections(bot_id)
    except Exception as e:
        print(f"[scanner] WARNING: heartbeat sections read failed: {e}", flush=True)
        inv.heartbeat_md_sections = []
    # Deprecated; always empty in v17. Removed in v18.
    inv.openclaw_hooks = []

    return inv


def _collect_crons(bot_id: str, workspace: Path) -> list[dict]:
    """Read crontab entries with script content for each job."""
    try:
        result = subprocess.run(
            ["sudo", "-u", get_bot_user(bot_id, load_network()), "crontab", "-l"],
            capture_output=True, text=True, timeout=5, cwd=get_profile().scratch_dir,
        )
        if result.returncode != 0:
            print(f"[scanner] crontab -l failed (rc={result.returncode}): {result.stderr[:200]}", flush=True)
            return []
        crons = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            # Handle @keyword crons (@reboot, @hourly, @daily, etc.)
            if line.startswith("@"):
                if len(parts) < 2:
                    continue
                schedule = parts[0]
                cmd_parts = parts[1:]
            elif len(parts) < 6:
                continue
            else:
                schedule = " ".join(parts[:5])
                cmd_parts = parts[5:]
            script_path = next(
                (p for p in cmd_parts if p.endswith((".py", ".sh"))), None
            )
            if not script_path:
                continue
            content = _safe_read_as_bot(Path(script_path), max_chars=3000)
            is_infra = _is_infrastructure_script(content, script_path)
            crons.append({
                "schedule": schedule,
                "script_path": script_path,
                "script_content": content,
                "is_infrastructure": is_infra,
            })
        return crons
    except Exception:
        return []


def _collect_named_dirs(workspace: Path, bot_id: str) -> list[dict]:
    """
    List non-infrastructure root directories with content samples.
    Recurses one level into subdirectories so that dirs like ops/tools/ are visible.
    """
    dirs = []
    try:
        for d in sorted(workspace.iterdir()):
            if not d.is_dir() or d.name in OC_INFRA_DIRS or d.name.startswith("."):
                continue
            try:
                direct_files = [f.name for f in sorted(d.iterdir()) if f.is_file()][:5]
                # Recurse one level into subdirs to expose nested app files
                subdirs: list[dict] = []
                for sd in sorted(d.iterdir()):
                    if sd.is_dir() and not sd.name.startswith(".") and sd.name not in OC_INFRA_DIRS:
                        try:
                            sd_files = [f.name for f in sorted(sd.iterdir()) if f.is_file()][:6]
                            subdirs.append({"name": sd.name, "files": sd_files})
                        except OSError:
                            pass
                # Content preview from any markdown in the tree
                md_files = sorted(d.rglob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
                preview = _safe_read_as_bot(md_files[0], max_chars=200) if md_files else ""
                dirs.append({
                    "name": d.name,
                    "files": direct_files,
                    "subdirs": subdirs[:6],
                    "content_preview": preview,
                })
            except OSError:
                continue
    except OSError as e:
        # workspace.iterdir() unreachable under a 0700 ACL-mask clamp (Py3.12
        # RAISES): return whatever was collected so far and LOG, rather than
        # confabulating "no named dirs".
        _log.warning("scanner: named-dir walk of %s unreachable (EACCES clamp?): %s", workspace, e)
    return dirs


def _collect_json_stores(workspace: Path, bot_id: str) -> list[dict]:
    """
    Find JSON files that look like active data stores — large arrays, task lists,
    registries, etc. Ignores tiny config files and OC default files.
    """
    MIN_SIZE = 2 * 1024  # 2 KB
    stores: list[dict] = []
    try:
        for f in sorted(workspace.rglob("*.json")):
            try:
                parts = f.relative_to(workspace).parts
            except ValueError:
                continue
            # Skip hidden dirs, OC infrastructure dirs, and Evolve's own
            # platform-state trees (evolve/, evolve-backup/). Without the
            # last clause the platform's Forge inbox / audit outbox / backup
            # JSON floods the discovery inventory and the LLM invents phantom
            # apps from it (see EVOLVE_PLATFORM_TREES).
            if any(
                p.startswith(".")
                or p in OC_DEFAULT_DIRS
                or p in EVOLVE_PLATFORM_TREES
                for p in parts
            ):
                continue
            # Skip tiny files (configs, not data stores)
            try:
                size = f.stat().st_size
                if size < MIN_SIZE:
                    continue
            except OSError:
                continue
            # Peek at the content to characterise it
            raw = _safe_read_as_bot(f, max_chars=500)
            if not raw:
                continue
            hint = ""
            try:
                data = json.loads(raw + ("]" if raw.strip().startswith("[") and not raw.strip().endswith("]") else ""))
                if isinstance(data, list):
                    hint = f"array, ~{len(data)} items"
                elif isinstance(data, dict):
                    # Surface meaningful top-level keys
                    keys = [k for k in list(data.keys())[:6] if not k.startswith("_")]
                    if "schema_version" in data or "tasks" in data:
                        hint = f"dict keys: {', '.join(keys)}"
                    else:
                        hint = f"dict ({len(data)} keys)"
            except Exception:
                hint = "json"
            stores.append({
                "path": str(f.relative_to(workspace)),
                "size_kb": round(size / 1024, 1),
                "hint": hint,
            })
            if len(stores) >= 20:
                break
    except OSError:
        pass
    return stores


def _collect_memory_files(workspace: Path, bot_id: str) -> list[dict]:
    """Scan memory directories for files with recurring dated/structured entries."""
    results = []
    search_dirs = [
        workspace / "memory",
        workspace / "memory" / "health",
        workspace / "memory" / "private",
        workspace / "home",
    ]
    for search_dir in search_dirs:
        # exists_or_unreachable: a bare .exists() RAISES under an EACCES clamp
        # (Py3.12) and would crash collect_inventory (this helper's call site is
        # unguarded). Treat unreachable as present → the guarded glob below
        # returns [] for this dir; an unreadable dir is logged there.
        if not exists_or_unreachable(search_dir):
            continue
        try:
            for f in sorted(search_dir.glob("*.md")):
                try:
                    if f.stat().st_size < 200:
                        continue
                except OSError:
                    continue
                content = _safe_read_as_bot(f, max_chars=600)
                if not content:
                    continue
                is_recurring = _detect_recurring_structure(content)
                entry_count = content.count("## ") + content.count("| ") // 3
                results.append({
                    "path": str(f.relative_to(workspace)),
                    "preview": content[:400],
                    "is_recurring": is_recurring,
                    "entry_count_estimate": entry_count,
                })
        except OSError as e:
            # search_dir.glob() unreachable under a 0700 ACL-mask clamp (Py3.12
            # RAISES): skip this dir and LOG, rather than confabulating "no
            # memory files".
            _log.warning("scanner: memory-file walk of %s unreachable (EACCES clamp?): %s", search_dir, e)
            continue
    return results


# ── Phase 2: LLM discovery ────────────────────────────────────────────────────

def llm_discover_applications(
    inventory: WorkspaceInventory,
    model: str,
    openclaw_cmd: str = "openclaw",  # kept for compat, no longer used
    cwd: str | None = None,           # kept for compat, no longer used
    api_key: str = "",
) -> list[DetectedApplication]:
    """
    Phase 2 — ask the fast-tier LLM to identify and cluster applications from
    the rich workspace inventory. Returns DetectedApplication list
    (source='llm-inferred'). Falls back to empty list on any failure.

    ``api_key`` pairs with the provider-qualified ``model`` (both from
    :func:`_resolve_llm`); when empty, resolution runs here.
    """
    # Build prompt sections from four signal types
    app_crons = [c for c in inventory.cron_jobs if not c.get("is_infrastructure")]
    cron_section = ""
    if app_crons:
        lines = []
        for c in app_crons:
            purpose_hint = c["script_content"][:200].replace("\n", " ")
            lines.append(
                f"  Schedule: {c['schedule']}\n"
                f"  Script: {c['script_path']}\n"
                f"  Content preview: {purpose_hint}"
            )
        cron_section = (
            "CRON JOBS (user-configured automation — each is likely part of an application):\n"
            + "\n---\n".join(lines)
        )

    dir_section = ""
    if inventory.named_dirs:
        lines = []
        for d in inventory.named_dirs:
            files_str = ", ".join(d["files"][:5])
            subdir_parts = []
            for sd in d.get("subdirs", []):
                sf = ", ".join(sd["files"][:4])
                subdir_parts.append(f"{sd['name']}/({sf})" if sf else sd["name"] + "/")
            subdirs_str = ("  Subdirs: " + "; ".join(subdir_parts)) if subdir_parts else ""
            lines.append(
                f"  Directory: {d['name']}/\n"
                + (f"  Files: {files_str}\n" if files_str else "")
                + (subdirs_str + "\n" if subdirs_str else "")
                + f"  Preview: {d.get('content_preview', '')[:100]}"
            )
        dir_section = (
            "NAMED WORKSPACE DIRECTORIES (each likely represents an application domain):\n"
            + "\n---\n".join(lines)
        )

    recurring = [m for m in inventory.memory_files if m.get("is_recurring")]
    mem_section = ""
    if recurring:
        lines = [
            f"  {m['path']} (~{m['entry_count_estimate']} entries)\n"
            f"  Preview: {m['preview'][:100]}"
            for m in recurring
        ]
        mem_section = (
            "RECURRING MEMORY FILES (active logs/trackers — each likely part of an application):\n"
            + "\n".join(lines)
        )

    # Script file list — filter infra, raise cap, add content preview for suggestive names
    scripts_section = ""
    _app_name_hints = {"task", "manager", "system", "tracker", "processor", "intake",
                       "status", "report", "log", "scheduler", "monitor", "assistant"}
    all_scripts = inventory.python_scripts[:40] + inventory.shell_scripts[:10]
    if all_scripts:
        # Filter infra by BOTH basename and content. Passing "" here was the
        # #2705 gap: the content check always failed AND the path was never
        # inspected, so gateway-selfheal.sh / sentry_ping.sh reached the LLM
        # and got minted as apps. Read the script so content signals fire too
        # (catches custom-named infra), with the basename check as the floor.
        filtered = []
        for s in all_scripts:
            content = _safe_read_as_bot(inventory.workspace / s, max_chars=3000)
            if _is_infrastructure_script(content, s):
                continue
            filtered.append(s)
        if filtered:
            lines = []
            for s in filtered[:40]:
                stem = Path(s).stem.lower()
                if any(h in stem for h in _app_name_hints):
                    # Include a brief content preview for scripts with suggestive names
                    full_path = inventory.workspace / s
                    preview = _safe_read_as_bot(full_path, max_chars=120)
                    first_line = next((ln.strip() for ln in preview.splitlines() if ln.strip() and not ln.startswith("#")), "")
                    lines.append(f"  {s}" + (f"  # {first_line[:80]}" if first_line else ""))
                else:
                    lines.append(f"  {s}")
            scripts_section = "SCRIPTS (may indicate applications):\n" + "\n".join(lines)

    # JSON data stores
    json_section = ""
    if inventory.json_stores:
        lines = [
            f"  {s['path']} ({s['size_kb']} KB, {s['hint']})"
            for s in inventory.json_stores
        ]
        json_section = (
            "JSON DATA STORES (structured data files — each likely belongs to an application):\n"
            + "\n".join(lines)
        )

    soul_section = (
        f"BOT PURPOSE (SOUL.md):\n{inventory.soul_content[:600]}"
        if inventory.soul_content else ""
    )
    agents_section = (
        f"LIFE AREAS (AGENTS.md):\n{inventory.agents_content[:800]}"
        if inventory.agents_content else ""
    )

    # v13: scheduled-action candidates extracted from heartbeats / standing
    # instructions. Each candidate is a markdown section whose body matched
    # a recurring-behavior pattern (e.g. "every evening at 6 PM"). The LLM
    # should treat each as a possible new app or a behavior to attach to an
    # existing app's scheduled_actions[]. See spec §3.1.
    sched_section = ""
    if inventory.scheduled_action_candidates:
        lines = []
        for c in inventory.scheduled_action_candidates[:8]:
            heading = c.get("heading") or "(top of file)"
            body_excerpt = (c.get("body") or "")[:400].replace("\n", " ")
            lines.append(
                f"  File: {c.get('file_path')}\n"
                f"  Section heading (anchor): {heading}\n"
                f"  Body excerpt: {body_excerpt}"
            )
        sched_section = (
            "SCHEDULED-ACTION CANDIDATES (heartbeat / standing-instruction sections\n"
            "that describe recurring behaviors — each is a likely scheduled_action\n"
            "claim that should attach to the most-relevant app, or create a new app\n"
            "if no existing one fits):\n"
            + "\n---\n".join(lines)
        )

    sections = [cron_section, dir_section, mem_section, json_section,
                scripts_section, soul_section, agents_section, sched_section]
    context = "\n\n".join(s for s in sections if s)

    prompt = f"""You are analyzing an OpenClaw AI assistant's workspace to discover what
APPLICATIONS it has been built to serve.

An application is a coherent area of recurring functionality — e.g. "Health Tracking",
"Task Manager", "Home Repairs Log", "Email Assistant", "Project Tracker".
Applications typically involve: one or more of — a script, a JSON data file, a memory/log file,
a cron job, or a named directory. A cron job is NOT required.

{context}

Rules:
- Group related signals (directory + scripts + JSON store) into single applications
- Each application must have at least one clear evidence signal
- A JSON data store IS sufficient evidence on its own (e.g. tasks.json → Task Manager)
- A directory with app-specific scripts IS sufficient evidence on its own
- A heartbeat / standing-instruction section describing a recurring behavior
  (a SCHEDULED-ACTION CANDIDATE) IS sufficient evidence on its own — create
  a new application for it if no existing app fits, naming it after the
  behavior (e.g. "Protein Reminder", "Morning Briefing")
- Name each application clearly (2-4 words)
- Do not invent applications without evidence
- The following are NEVER applications — skip them entirely. (A deterministic
  post-filter also drops them, but don't surface them in the first place.)
  * Evolve/OpenClaw platform infrastructure — gateway/watchdog self-heal,
    health/liveness probes, backups, the repo puller, usage/turn collectors,
    log trimmers, git push/commit crons (e.g. gateway-selfheal.sh, a
    <watchdog>_ping.sh liveness probe). A capability that would appear on
    EVERY bot is platform infra, not an app.
  * OC system functions — how the agent itself works: session startup,
    persistent memory / memory persistence, bot identity. Their only
    "evidence" is OC identity files (AGENTS.md, MEMORY.md, SOUL.md, USER.md,
    HEARTBEAT.md) — that is the runtime, not an application.
  * The manifest store itself — files under manifests/ (i-*.json, app-*.json)
    are Evolve's own application records, not an application.
  * Bare skills / integrations with no orchestration — a credential or config
    file alone (a Google OAuth json, a webhook config) is a SKILL, not an app.
    It becomes an application only when a bot-authored script orchestrates it.
- The "Personal Assistant" base (SOUL.md + USER.md) is always present — skip unless clearly distinct
- Return confidence 0.7-1.0 based on evidence strength

Evidence file rules — IMPORTANT:
- Prefer listing INDIVIDUAL files over directory hints. A directory hint
  causes the file-stamper to claim every file inside, which sweeps up
  scratch and template files that don't belong to the app.
- Only use "directory: <path>" when the directory is exclusively owned by
  this one application (e.g. ops/tools/ for a Task Manager whose every
  file is task-system code). When in doubt, list files individually.
- Do NOT list project-wide boilerplate (README.md, LICENSE, .gitignore,
  *-template.md, *.example) as evidence.

Return JSON array only, no other text:
[{{"id": "...", "name": "...", "description": "...", "confidence": 0.9,
  "evidence_files": ["ops/tools/unified_task_system.py", "ops/tasks/unified_tasks/tasks.json", "ops/tools/task_runner.py"]}}]"""

    if not api_key:
        r_model, api_key = _resolve_llm("tier3")
        if not model:
            model = r_model
    print(f"[scanner] API key found: {'yes' if api_key else 'NO'}", flush=True)
    print(f"[scanner] LLM call: model={model} prompt_chars={len(context)}", flush=True)
    if not api_key:
        # Surface this loudly to the caller. Previously this returned [] and the
        # pipeline raced to "done, 0 found", which UI users perceive as the scan
        # "exiting early without finding anything". The caller (scan_workspace_pipeline)
        # catches MissingApiKeyError and writes a status with status=error so the
        # admin UI can surface a real diagnostic instead of a clean "0 results".
        raise MissingApiKeyError(
            f"No LLM provider credentialed for the pod (scan of bot "
            f"{inventory.bot_id!r}). Checked the provider API-key env vars and "
            f"the primary bot's OpenClaw auth store. Fix: add an api_key-typed "
            f"provider profile on the primary bot, or re-run the scan with --no-llm."
        )

    try:
        text = _call_llm(model, prompt, api_key, timeout=60)
        print(f"[scanner] LLM raw output ({len(text)} chars): {text[:300]}", flush=True)
        if not text:
            return []

        start = text.find("[")
        end = text.rfind("]") + 1
        if start == -1 or end <= start:
            print(f"[scanner] No JSON array found in LLM output", flush=True)
            return []

        apps = json.loads(text[start:end])
        if not isinstance(apps, list):
            print(f"[scanner] LLM output parsed but not a list: {type(apps)}", flush=True)
            return []

        detected: list[DetectedApplication] = []
        for app in apps:
            if not isinstance(app, dict):
                continue
            app_id = str(app.get("id", "")).strip().lower().replace(" ", "-")
            name = str(app.get("name", "")).strip()
            if not app_id or not name:
                continue
            raw_evidence = [str(e) for e in app.get("evidence_files", [])]
            # L1 platform-files defense (stored-evidence strip): drop any
            # evidence path written by pod-wide OC infrastructure (turn-
            # collector etc.) so it never lands on the manifest. This is the
            # NARROW Tier-A strip — it deliberately keeps behavior-doc surfaces
            # (HEARTBEAT.md/AGENTS.md) a real app legitimately cites. See
            # PLATFORM_WRITTEN_FILE_PATTERNS.
            evidence_files: list[str] = []
            dropped_platform: list[str] = []
            for ev in raw_evidence:
                if _is_platform_written_path(ev):
                    dropped_platform.append(ev)
                else:
                    evidence_files.append(ev)
            if dropped_platform:
                print(
                    f"[scanner] LLM evidence filter: dropped {len(dropped_platform)} "
                    f"platform-written path(s) from app {name!r}: "
                    f"{dropped_platform[:3]}",
                    flush=True,
                )
            description = str(app.get("description", "")).strip()
            confidence = float(app.get("confidence", 0.7))
            summary = (
                f"AI-inferred from: {', '.join(evidence_files[:3])}"
                if evidence_files else "AI-inferred from workspace structure"
            )
            det = DetectedApplication(
                id=app_id,
                name=name,
                description=description,
                confidence=confidence,
                evidence_files=evidence_files,
                evidence_summary=summary,
                suggested_goals=[],
                suggested_tests=[],
                suggested_privacy=[],
                source="llm-inferred",
            )
            # ── App-evidence floor (the #2705 hard wall) ────────────────────
            # The cluster is an application only if it cites at least one file
            # that counts as APPLICATION evidence. Infra scripts, OC
            # identity/system files, the manifest store, and (orchestration-
            # less) skill configs are NOT app evidence — see
            # _app_evidence_files. This subsumes the old "all evidence was
            # platform-written" drop AND closes the #2705 gaps (infra scripts,
            # identity files, manifests/, OAuth-only skills, zero-evidence
            # clusters that the LLM emitted with empty evidence).
            if not _app_evidence_files(raw_evidence):
                # Exemption: a recurring-behavior app whose producer surface is
                # a scheduled_action materialized in Phase 4 (e.g. "Protein
                # Reminder" from a HEARTBEAT.md section), not a file. These
                # legitimately carry no file evidence. _floor_behavior_exempt
                # rejects OC system functions ("Session Startup", "Memory
                # Persistence") by name BEFORE the candidate match — identity
                # sections legitimately contain schedule words, so a name-token
                # denylist (not "are they schedule-hinted") is the sound gate.
                sched_match = _floor_behavior_exempt(
                    det, inventory.scheduled_action_candidates or []
                )
                if not sched_match:
                    print(
                        f"[scanner] app-evidence floor: dropping app {name!r} — "
                        f"no application evidence (all {len(raw_evidence)} "
                        f"cited file(s) are infra/system/skill/manifest-store, "
                        f"or none were cited)",
                        flush=True,
                    )
                    continue
                print(
                    f"[scanner] app-evidence floor: keeping app {name!r} with no "
                    f"file evidence — matches a scheduled-action candidate "
                    f"(behavior surface materializes in Phase 4)",
                    flush=True,
                )
            detected.append(det)
        return detected

    except Exception as e:
        import traceback as _tb3
        print(f"[scanner] LLM discovery exception: {e}", flush=True)
        print(_tb3.format_exc(), flush=True)
        return []


# ── v13: scheduled-action assignment ─────────────────────────────────────────


def _slugify_action_id(text: str, fallback: str = "scheduled-action") -> str:
    """Lowercase + hyphenate; restrict to slug-safe characters."""
    import re as _re
    s = _re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or fallback


def _candidate_relates_to_app(candidate: dict, app: "DetectedApplication") -> bool:
    """Heuristic: does a heartbeat section relate to this app?

    Match if the app's name tokens appear in the candidate's heading or
    body, OR if any of the app's evidence file paths show up in the body.
    This is the deterministic fallback used when the LLM call fails or
    when the scanner is invoked with use_llm=False.
    """
    text = (candidate.get("heading", "") + " " + candidate.get("body", "")).lower()
    name = (app.name or "").lower()
    if name and name in text:
        return True
    # Token overlap (>1 token) — handles "protein tally" matching "Protein Reminder"
    import re as _re
    name_tokens = {t for t in _re.split(r"[\s_\-]+", name) if len(t) > 3}
    text_tokens = set(_re.findall(r"[a-z]{4,}", text))
    if name_tokens and len(name_tokens & text_tokens) >= 1:
        return True
    # Evidence file path match (basename or stripped path)
    for ev in app.evidence_files or []:
        s = str(ev).lower()
        if ":" in s:
            s = s.split(":", 1)[1].strip()
        s = s.strip("/")
        if s and s in text:
            return True
        base = Path(s).name
        if base and base in text:
            return True
    return False


def _build_scheduled_action_entry(candidate: dict, app_name: str) -> dict:
    """Build a manifest scheduled_action dict from an extraction candidate.

    Trigger kind is `heartbeat` when the source file is a heartbeat surface,
    otherwise `cron` is used (matching the spec's enum). Schedule is left
    blank in the deterministic path — the LLM will fill it when present;
    Tier 2's anchor check is the load-bearing claim either way.
    """
    file_path = candidate.get("file_path", "")
    is_heartbeat = file_path.upper() in {"HEARTBEAT.MD", "AGENTS.MD", "POD_CONDUCT.MD", "SOUL.MD"}
    locator = candidate.get("heading") or _first_unique_phrase(candidate.get("body", ""))
    action_id = _slugify_action_id(
        f"{app_name}-{candidate.get('heading') or 'action'}",
        fallback="scheduled-action",
    )[:60]
    return {
        "id": action_id,
        "trigger": {
            "kind": "heartbeat" if is_heartbeat else "cron",
            "schedule": _infer_schedule_string(candidate.get("body", "")),
            "evidence_path": file_path,
            "evidence_locator": locator,
            "section_sha256": candidate.get("section_sha256", ""),
        },
        "inputs": [],
        "outputs": [],
        "summary": _summarize_candidate(candidate),
    }


# ── v16: scanner attribution for LaunchAgents + openclaw.json hooks ───────────
#
# spec-forge-side-effects-2026-06-02.md §6. The two helpers below let
# generate_manifest_for_app populate scheduled_actions[] with v16-shape
# install metadata (mechanism, install, installed_artifact) drawn from
# actual install sites on disk. Pre-PR-4 installs (team-bot-a, team-bot-c) get
# retroactively attributed; post-PR-4 forge installs round-trip into the
# same manifest shape.


def _slug_variants(s: str) -> set[str]:
    """Return slug variants for fuzzy matching: lowercased, no separators."""
    s = (s or "").lower()
    return {
        s,
        s.replace("-", ""),
        s.replace("_", ""),
        s.replace("-", "_"),
        s.replace("_", "-"),
        s.replace("-", "").replace("_", ""),
    }


def _attribute_launchd_to_app(entry: dict, app: "DetectedApplication", bot_user: str) -> bool:
    """Decide whether a LaunchAgent plist belongs to ``app`` (spec §6.1).

    Three-step attribution, deterministic only — LLM fallback for unattributed
    entries happens in the Phase 2 clustering loop.

      1. **Label namespace match**: ``com.{bot}.{app-slug}.*`` Label prefix.
         Forge installs use this convention; backfills do not, but live
         installs increasingly will after PR 4.

      2. **ProgramArguments path match**: any arg references a file under
         the bot's workspace whose path appears in ``app.evidence_files``.
         This is how hand-installed crons get attributed retroactively.

    Returns ``True`` iff the entry should be claimed by this app.
    """
    label = (entry.get("label") or "").lower()
    program_args = entry.get("program_args") or []

    # Step 1: Label namespace match. We tolerate ``{bot_id}`` and ``{bot_user}``
    # because forge picks one and ops may have used the other for hand-installs.
    app_slugs = _slug_variants(app.id)
    for bot_token in _slug_variants(bot_user) | _slug_variants(app.id):
        # We don't have bot_id here — but bot_user is the closest stable token,
        # and the spec convention is `com.{bot}.{app}.*`. Tolerant match below.
        del bot_token  # bookkeeping; see comment above
    for slug in app_slugs:
        if not slug:
            continue
        # Match `com.X.{slug}.` OR `.{slug}.` anywhere — covers both
        # `com.bot.task-manager.check` and rarer `org.example.task-manager`.
        if f".{slug}." in label or label.endswith(f".{slug}"):
            return True

    # Step 2: ProgramArguments path match. Walk every arg; if it's a
    # path that contains one of the app's evidence files (or their
    # basenames), attribute to this app.
    for arg in program_args:
        if not isinstance(arg, str):
            continue
        arg_lower = arg.lower()
        for ev in (app.evidence_files or []):
            ev_lower = (ev or "").lower().strip("/")
            if not ev_lower:
                continue
            if ev_lower in arg_lower:
                return True
            base = Path(ev_lower).name
            if base and base in arg_lower:
                return True
    return False


def _attribute_hook_to_app(entry: dict, app: "DetectedApplication") -> bool:
    """DEPRECATED v17 — see _attribute_instruction_to_app.

    Pre-v17 attribution path. Kept for one schema version so existing
    callers continue to import cleanly. Always returns False.
    """
    del entry, app
    return False


def _attribute_instruction_to_app(
    section: dict, app: "DetectedApplication", *, app_pkg_id: str = "",
) -> bool:
    """Decide whether a HEARTBEAT.md/AGENTS.md managed section belongs to ``app``.

    Two-step attribution per spec-heartbeat-instruction §6:

      1. **Marker pkg_id match**: ``section["pkg_id"]`` equals
         ``app_pkg_id`` → the marker was forge-installed and explicitly
         attributed to this app. Authoritative.

      2. **command-path match**: ``section["command_hint"]`` references
         one of ``app.evidence_files`` (or its basename). Used for
         operator-authored managed sections without a pkg attribution.

    Returns ``True`` iff the section should be claimed by this app.
    """
    if not isinstance(section, dict):
        return False
    pkg_in_marker = (section.get("pkg_id") or "").strip()
    if pkg_in_marker and app_pkg_id and pkg_in_marker == app_pkg_id:
        return True
    command = (section.get("command_hint") or "").lower()
    if not command:
        return False
    for ev in (app.evidence_files or []):
        ev_lower = (ev or "").lower().strip("/")
        if not ev_lower:
            continue
        if ev_lower in command:
            return True
        base = Path(ev_lower).name
        if base and base in command:
            return True
    return False


def _collect_cron_evidence_labels(
    launchd_entries: list[dict],
    app: "DetectedApplication",
    bot_user: str,
) -> list[str]:
    """v23: collect the LaunchAgent Labels attributable to ``app``.

    Walks ``launchd_entries`` (an inventory snapshot of plist files
    under ``~/Library/LaunchAgents/``), runs each through
    ``_attribute_launchd_to_app``, and returns the de-duplicated list of
    Labels. The result populates ``manifest['cron_evidence']['labels']``
    so the bot-side ``check_cron_labels_loaded`` audit has something
    concrete to verify against ``launchctl list``.

    Order-preserving and stable: the output mirrors the input order of
    matched entries, with duplicates dropped. Empty / missing labels are
    skipped (the inventory occasionally surfaces those for malformed
    plists). Empty input → empty output.

    Factored out from ``generate_manifest_for_app``'s inline loop so the
    population is unit-testable without an LLM dispatch.
    """
    labels: list[str] = []
    for entry in launchd_entries or []:
        if not isinstance(entry, dict):
            continue
        if not _attribute_launchd_to_app(entry, app, bot_user):
            continue
        label = (entry.get("label") or "").strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def _build_scheduled_action_from_launchd(
    entry: dict, app: "DetectedApplication", bot_user: str,
) -> dict:
    """Build a v16-shape scheduled_actions[] entry from a LaunchAgent plist.

    ``mechanism`` is always ``"launchd"``. The ``install`` block carries
    the plist Label so PR 4's forge install phase can find and replace
    this entry idempotently. ``installed_artifact`` points back to the
    plist file so the verifier (PR 3) can sha-check it.
    """
    from .manifest import MECHANISM_LAUNCHD

    label = entry.get("label") or ""
    program_args = entry.get("program_args") or []
    command = " ".join(str(a) for a in program_args) if program_args else ""

    # Build a short, stable id. Prefer the launchd label tail
    # (`com.bot.task-manager.check` → `task-manager-check`); fall back to
    # the first program arg that isn't a known interpreter, so a plist
    # invoking ``["python3", "tasks.py"]`` yields ``…-tasks`` not ``…-python3``.
    _INTERPRETERS = {"python", "python3", "bash", "sh", "zsh", "node", "ruby", "perl"}
    if label:
        tail = label.rsplit(".", 1)[-1] if "." in label else label
        action_id = _slugify_action_id(f"{app.name}-{tail}", fallback="scheduled-action")[:60]
    else:
        script_arg = ""
        for arg in program_args:
            if not isinstance(arg, str):
                continue
            base = Path(arg).name
            if base and base not in _INTERPRETERS:
                script_arg = base
                break
        base = Path(script_arg).stem if script_arg else "action"
        action_id = _slugify_action_id(f"{app.name}-{base}", fallback="scheduled-action")[:60]

    schedule = ""
    interval = entry.get("start_interval")
    cal = entry.get("start_calendar_interval")
    if isinstance(interval, int) and interval > 0:
        schedule = f"every {interval} seconds"
    elif isinstance(cal, dict):
        # StartCalendarInterval keys: Hour, Minute, Day, Weekday, Month
        parts = []
        for k in ("Weekday", "Day", "Month", "Hour", "Minute"):
            if k in cal:
                parts.append(f"{k}={cal[k]}")
        if parts:
            schedule = " ".join(parts)

    return {
        "id": action_id,
        "mechanism": MECHANISM_LAUNCHD,
        "trigger": {
            "kind": "launchd",
            "schedule": schedule,
        },
        "install": {
            "command": command,
            "plist_label": label,
        },
        "installed_artifact": entry.get("plist_path") or (
            f"/Users/{bot_user}/Library/LaunchAgents/{label}.plist" if label else ""
        ),
        "installed_by": "scanner:backfill",
        "inputs": [],
        "outputs": [],
        "summary": f"LaunchAgent {label}".strip() if label else command,
    }


def _build_scheduled_action_from_hook(*args, **kwargs) -> dict:
    """DEPRECATED v17 — use _build_scheduled_action_from_instruction."""
    del args, kwargs
    return {}


def _build_scheduled_action_from_instruction(
    section: dict, app: "DetectedApplication",
) -> dict:
    """Build a v17-shape scheduled_actions[] entry from a managed section.

    ``mechanism`` is ``oc_heartbeat_instruction`` when the source file
    is HEARTBEAT.md; ``oc_session_instruction`` for AGENTS.md (the v17
    convention). ``installed_artifact`` is ``{file}#{anchor}``, which
    the verifier (PR 11) resolves via _extract_section.
    """
    from .manifest import (
        MECHANISM_OC_HEARTBEAT_INSTRUCTION,
        MECHANISM_OC_SESSION_INSTRUCTION,
    )

    file = section.get("file") or "HEARTBEAT.md"
    anchor = section.get("anchor") or ""
    body = section.get("body") or ""
    command = section.get("command_hint") or ""

    mechanism = (
        MECHANISM_OC_HEARTBEAT_INSTRUCTION
        if file == "HEARTBEAT.md"
        else MECHANISM_OC_SESSION_INSTRUCTION
    )
    trigger_kind = "heartbeat" if file == "HEARTBEAT.md" else "session_start"

    action_id = _slugify_action_id(
        f"{app.name}-{anchor}", fallback="scheduled-action",
    )[:60]

    return {
        "id": action_id,
        "mechanism": mechanism,
        "trigger": {
            "kind": trigger_kind,
            "schedule": "every_heartbeat" if trigger_kind == "heartbeat" else "session_start",
        },
        "install": {
            "file": file,
            "section_anchor": f"## {anchor}",   # canonical heading form
            "body": body,
            "command": command,
        },
        "installed_artifact": f"{file}#{anchor}",
        "installed_by": "scanner:backfill",
        "inputs": [],
        "outputs": [{"kind": "session_message", "channel": "primary"}],
        "summary": (
            f"Managed section {anchor!r} in {file}: "
            f"{command[:80]}" if command else
            f"Managed section {anchor!r} in {file}"
        ),
    }


def _first_unique_phrase(body: str) -> str:
    """Return a 5-10-word phrase from the body suitable as an anchor.

    Used when a behavior isn't under a markdown heading. Picks the first
    non-empty line, trimmed to a 5-10 word slice.
    """
    for line in (body or "").splitlines():
        line = line.strip().lstrip("-*").strip()
        if not line:
            continue
        words = line.split()
        if len(words) >= 5:
            return " ".join(words[:10])
        if words:
            return " ".join(words)
    return ""


def _infer_schedule_string(body: str) -> str:
    """Best-effort extraction of a human schedule phrase from body text.

    Pulls common forms like "6 PM daily", "every morning", "at 7:00 AM",
    etc. Returns empty string when no obvious schedule is present — the
    audit doesn't depend on this, it's just a hint for humans reading
    the manifest.
    """
    import re as _re
    patterns = [
        r"\b(?:every|each)\s+(?:morning|evening|night|day|hour|week|month)\b[^\.\n]*",
        r"\b(?:at|by)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?(?:\s+daily)?\b",
        r"\b\d{1,2}\s*(?:am|pm)\s+daily\b",
        r"\bdaily\b",
        r"\bweekly\b",
        r"\bnightly\b",
        r"\bonce\s+(?:a|per)\s+(?:day|week|month|hour)\b",
    ]
    for pat in patterns:
        m = _re.search(pat, body or "", _re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return ""


def _summarize_candidate(candidate: dict) -> str:
    """One-line plain-language summary from the candidate body.

    First non-empty content line, capped at 160 chars. The LLM will
    overwrite this in the LLM-enriched path; this is the deterministic
    fallback.
    """
    body = candidate.get("body") or ""
    for line in body.splitlines():
        line = line.strip().lstrip("-*").strip()
        if line and not line.startswith("#"):
            return line[:160]
    heading = candidate.get("heading") or ""
    return heading[:160]


# ── Phase 4: Manifest generation ──────────────────────────────────────────────

def generate_manifest_for_app(
    app: DetectedApplication,
    inventory: WorkspaceInventory,
    model: str,
    openclaw_cmd: str = "openclaw",
    *,
    api_key: str = "",
    repair: bool = True,
    repair_log: list[str] | None = None,
) -> dict:
    """
    Generate a full 4-section manifest for a discovered application.
    Uses the fast-tier LLM with rich context from the inventory
    (``api_key`` pairs with the provider-qualified ``model``; when empty,
    resolution runs here). Falls back to a stub manifest if the LLM
    fails or times out.

    When ``repair=True`` (default) the Phase 4.5 mechanical-repair pass
    runs on the assembled dict before return — closes common gaps the
    verifier would otherwise flag (missing CLI on user-routed apps,
    thin hint_words, missing test_exemption_reason). See
    ``scanner_repair.repair_manifest`` for the per-repair rules.
    Pass-through ``repair_log`` collects the human-readable applied
    /skipped lines so the caller can flush them into the scan log.
    """
    ws = inventory.workspace
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Collect content previews of the app's evidence files
    file_previews: list[str] = []
    for ev in app.evidence_files[:8]:
        ev_path = ws / ev.rstrip("/")
        # is_file()/is_dir() themselves RAISE PermissionError under a 0700
        # ACL-mask clamp (Py3.12) — guard the type-probe so one unreachable
        # evidence file is skipped+logged, not a crash of the whole manifest gen.
        try:
            is_file = ev_path.is_file()
            is_dir = ev_path.is_dir() if not is_file else False
        except OSError as e:
            _log.warning("scanner: evidence file %s unreachable (EACCES clamp?): %s", ev_path, e)
            continue
        if is_file:
            try:
                content = ev_path.read_text(encoding="utf-8", errors="replace")[:400]
                file_previews.append(f"--- {ev} ---\n{content}")
            except OSError:
                pass
        elif is_dir:
            try:
                items = sorted(ev_path.iterdir())[:6]
                file_previews.append(f"--- {ev}/ (directory) ---\n" +
                                     "\n".join(f"  {i.name}" for i in items))
            except OSError:
                pass

    # Include non-infrastructure cron jobs that reference this app's evidence files
    app_crons = [j for j in inventory.cron_jobs if not j.get("is_infrastructure")]
    related_crons = [
        j for j in app_crons
        if any(ev.rstrip("/") in j["script_path"] for ev in app.evidence_files)
    ] or app_crons[:3]

    soul_excerpt = inventory.soul_content[:500] if inventory.soul_content else "(not available)"
    previews_text = "\n\n".join(file_previews) if file_previews else "(no file content available)"
    crons_text = "\n".join(
        f"  {j['schedule']} {j['script_path']}" for j in related_crons
    ) if related_crons else "  (none)"

    prompt = f"""Generate a structured manifest for the following application discovered
in an OpenClaw AI assistant's workspace. Be specific — use actual details from
the files, not generic placeholder text.

APPLICATION:
  Name: {app.name}
  Description: {app.description or '(none provided)'}
  Confidence: {app.confidence:.0%}
  Evidence files: {', '.join(app.evidence_files[:6]) or '(none listed)'}

BOT SOUL (context for this bot's purpose):
{soul_excerpt}

FILE CONTENT PREVIEWS:
{previews_text}

RELATED SCHEDULED TASKS:
{crons_text}

Return a JSON object only (no other text):
{{
  "description": "One-paragraph description of what this app does and who uses it.",
  "build_spec": "Markdown build specification that would rebuild this app from scratch. Describe: file layout (paths + purpose); data file formats (schema, headers, entry conventions); CLI / entry-point signatures with exact flag names and behaviors; key invariants the existing code preserves (e.g. append-only writes, canonical section headers); test steps that exercise the main paths. Aim for the level of detail a different bot would need to reproduce this app — pretend the files are about to be deleted and this spec is the only record of how to rebuild them.",
  "identity": {{
    "purpose": "This application exists to ...",
    "scope_includes": ["specific capability 1", "specific capability 2"],
    "scope_excludes": ["what it does NOT do"],
    "user": "who uses this (person or role)"
  }},
  "success_criteria": {{
    "observable_outcomes": ["concrete, testable outcome 1", "concrete outcome 2"],
    "failure_signals": ["clear sign that something is broken"],
    "quality_bar": {{
      "minimum": "minimum acceptable behavior description",
      "excellent": "what excellent performance looks like"
    }}
  }},
  "constraints": {{
    "privacy": ["data privacy rule based on what this app handles"],
    "safety": ["safety constraint if applicable"],
    "dependencies": ["file, script, or service this depends on"],
    "boundaries": ["what this application should never do"]
  }},
  "example_triggers": ["example user message that uses this app", "another example"],
  "test_cases": [
    {{"trigger": "a test message", "expected": "what the bot should do or say"}},
    {{"trigger": "another test", "expected": "expected behavior"}}
  ]
}}"""

    enriched: dict = {}
    if not api_key:
        r_model, api_key = _resolve_llm("tier3")
        if not model:
            model = r_model
    if api_key:
        try:
            text = _call_llm(model, prompt, api_key, timeout=90)
            if text:
                start = text.find("{")
                end = text.rfind("}") + 1
                if start != -1 and end > start:
                    parsed = json.loads(text[start:end])
                    if isinstance(parsed, dict):
                        enriched = parsed
        except Exception:
            pass

    # Build the full manifest dict, using enriched content or falling back to
    # stub values derived from the DetectedApplication data.
    identity = enriched.get("identity") or {
        "purpose": f"This application exists to help {inventory.bot_id} with {app.description or app.name}.",
        "scope_includes": app.suggested_goals[:4] if app.suggested_goals else [],
        "scope_excludes": [],
        "user": inventory.bot_id,
    }
    success_criteria = enriched.get("success_criteria") or {
        "observable_outcomes": [],
        "failure_signals": app.suggested_tests[:3] if app.suggested_tests else [],
        "quality_bar": {"minimum": "", "excellent": ""},
    }
    constraints = enriched.get("constraints") or {
        "privacy": app.suggested_privacy if app.suggested_privacy else [],
        "safety": [],
        "dependencies": app.evidence_files[:6],
        "boundaries": [],
    }

    # ── Derive capability_tags and session_keywords from the app name ────────
    # These feed the app-session correlator's Tier 1 (capability name matching)
    # and Tier 3 (outcome keyword matching) attribution strategies.
    # We produce word-token splits of the display name so short synonyms also
    # match (e.g. "Health Tracking" → tags ["health", "tracking", "health tracking"]).
    import re as _re
    _name_norm = app.name.strip()
    _tokens = [t for t in _re.split(r"[\s_\-]+", _name_norm.lower()) if len(t) > 2]
    capability_tags: list[str] = list(dict.fromkeys(
        [_name_norm] + _tokens  # full name first, then individual tokens
    ))
    session_keywords: list[str] = list(dict.fromkeys(
        [_name_norm.lower()] + _tokens
    ))

    description = (
        enriched.get("description")
        or app.description
        or _derive_description_from_purpose(
            identity.get("purpose", ""), app.name
        )
    )
    # Build spec: the LLM-generated reproduction recipe (Phase E of the audit).
    # Required by forge_engine to rebuild this app via the bot-driven dispatch.
    # Without it, scan→forge round-trip is broken: forge_engine refuses or
    # produces a minimal stub. Even an imperfect generated spec is far better
    # than no spec — operators can edit it before re-forging if needed.
    build_spec = enriched.get("build_spec", "")

    # ── v13: scheduled-action contracts ──────────────────────────────────────
    # Attach scheduled actions surfaced by the heartbeat / standing-instruction
    # extraction pass. Each candidate that "relates" to this app (by name token,
    # body mention, or evidence-file overlap) becomes a scheduled_actions entry.
    # Tier-2's anchor check (`scheduled_action_anchor`) is the load-bearing
    # claim — when the heartbeat section gets clobbered, the anchor stops
    # resolving and emits a `critical` finding. That's the protein-reminder
    # catch from spec §1.1.
    scheduled_actions: list[dict] = []
    heartbeat_anchors: list[str] = []
    heartbeat_path = ""
    for cand in inventory.scheduled_action_candidates or []:
        if not _candidate_relates_to_app(cand, app):
            continue
        entry = _build_scheduled_action_entry(cand, app.name)
        scheduled_actions.append(entry)
        # Aggregate anchors per heartbeat file so heartbeat_evidence carries
        # the full set without duplication
        if cand.get("file_path", "").upper() == "HEARTBEAT.MD":
            anchor = entry["trigger"].get("evidence_locator", "")
            if anchor and anchor not in heartbeat_anchors:
                heartbeat_anchors.append(anchor)
                heartbeat_path = cand.get("file_path", "")
    heartbeat_evidence: dict = {}
    if heartbeat_anchors and heartbeat_path:
        heartbeat_evidence = {
            "file_path": heartbeat_path,
            "section_anchors": heartbeat_anchors,
        }

    # ── v16: install-artifact attribution (spec-forge-side-effects §6) ───────
    # Walk the enumerated LaunchAgents and openclaw.json hooks; for each that
    # attributes to this app by Label namespace or script-path match, build a
    # v16-shape scheduled_actions[] entry with mechanism + install_artifact
    # populated. Closes the scan-miss gap that flagged team-bot-a's and
    # team-bot-c's hand-installed task-check crons as "no scheduled_actions"
    # in the pre-v16 scanner output.
    try:
        bot_user = get_bot_user(inventory.bot_id, load_network())
    except Exception:
        bot_user = inventory.bot_id
    for launchd_entry in inventory.launchd_entries or []:
        if not _attribute_launchd_to_app(launchd_entry, app, bot_user):
            continue
        scheduled_actions.append(
            _build_scheduled_action_from_launchd(launchd_entry, app, bot_user)
        )
    # v23: cron_evidence is the v23 manifest field that captures the
    # launchd Labels attributable to this app. Pre-v23 the scanner wrote
    # cron_evidence: {} unconditionally, so check_cron_labels_loaded had
    # nothing to verify — a dead loop. The same attribution match driving
    # scheduled_actions[] above populates this for free. Single source of
    # truth via _collect_cron_evidence_labels (unit-tested directly).
    cron_evidence_labels = _collect_cron_evidence_labels(
        inventory.launchd_entries or [], app, bot_user,
    )
    # v17: attribute evolve-managed sections in HEARTBEAT.md / AGENTS.md
    # to this app. Two-step match (pkg_id from marker, then command path
    # fallback) — see _attribute_instruction_to_app.
    app_pkg_id = getattr(app, "pkg_id", "") or ""
    for section in inventory.heartbeat_md_sections or []:
        if not _attribute_instruction_to_app(section, app, app_pkg_id=app_pkg_id):
            continue
        scheduled_actions.append(
            _build_scheduled_action_from_instruction(section, app)
        )

    # ── Infer usage.model from detected features ─────────────────────────────
    # Without this, the bot-side discoverability verifier defaults to
    # user-routed (its _USER_ROUTED_MODELS set includes the empty
    # string) and fires app_discoverability_no_cli + _no_example_triggers
    # findings on every scheduled app. Confirmed pod-wide 2026-06-07 against
    # production manifests: 114 firing alerts of this exact shape, every
    # one on a heartbeat- or cron-driven app whose scanner output had
    # ``usage.model`` unset.
    inferred_model = _infer_usage_model(
        scheduled_actions=scheduled_actions,
        heartbeat_evidence=heartbeat_evidence,
        # ``crons`` still isn't populated by this builder; OC-native
        # crons[] flow through a different write path. ``cron_evidence``
        # IS populated as of v23 from launchd matches above. Both are
        # redundant with scheduled_actions in practice (each matched
        # launchd entry already produced a scheduled_actions[] row), but
        # passing through keeps the inferrer single-source-of-truth in
        # case a future scanner path populates one without the other.
        crons=None,
        cron_evidence=(
            {"labels": cron_evidence_labels} if cron_evidence_labels else None
        ),
        # Library-detection inputs — the inferrer scans these prose
        # fields for "App must call X()" / "available on demand" /
        # "token lifecycle" style indicators. Production calibration
        # 2026-06-08: Biometric Integration is a token-management
        # library whose description was being misclassified as
        # user-routed.
        description=description,
        example_triggers=enriched.get("example_triggers", []),
        success_criteria=success_criteria,
        identity=identity,
    )
    # Merge the inferred model into any LLM-enriched usage block. LLM wins
    # when it picked a concrete model (operator may have crafted the
    # enrichment prompt to disambiguate); scanner fills in only when the
    # LLM didn't.
    enriched_usage = enriched.get("usage")
    if isinstance(enriched_usage, dict):
        usage_block = dict(enriched_usage)
        if not (usage_block.get("model") or "").strip():
            usage_block["model"] = inferred_model
    else:
        usage_block = {"model": inferred_model}

    manifest_dict = {
        "id": app.id,
        "name": app.name,
        "bot_id": inventory.bot_id,
        "description": description,
        "source": app.source,
        "confidence": app.confidence,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "schema_version": 3,
        "manifest_type": "evolve_application",
        "evidence_files": app.evidence_files,
        # Session attribution hints (used by app_session_correlator)
        "capability_tags": capability_tags,
        "session_keywords": session_keywords,
        # 4-section RSI fields
        "identity": identity,
        "success_criteria": success_criteria,
        "constraints": constraints,
        "usage": usage_block,
        "example_triggers": enriched.get("example_triggers", []),
        "test_cases": enriched.get("test_cases", []),
        "satisfaction": {"score": None, "notes": None, "rated_at": None},
        "improvement_history": [],
        # Forge contract: build_spec must be present for run_forge_job's
        # bot-driven dispatch to have anything to send. Closes the scan→forge
        # round-trip gap from the manifest framework audit (Phase E).
        "build_spec": build_spec,
        # v13: scheduled-action contracts (heartbeat / cron / standing
        # instructions). Empty list when no recurring behavior is described
        # in the bot's standing-instruction surfaces; spec §3.1.
        "scheduled_actions": scheduled_actions,
        "heartbeat_evidence": heartbeat_evidence,
        # v23: populate cron_evidence from launchd labels matched above.
        # Empty dict when no labels were attributed — preserves the
        # historical shape so consumers reading {} or {labels: []} both
        # see "no labels" consistently.
        "cron_evidence": (
            {"labels": cron_evidence_labels} if cron_evidence_labels else {}
        ),
    }

    # ── Phase 4.5: mechanical repair pass ────────────────────────────────────
    # Close common verifier-visible gaps (missing CLI on user-routed apps,
    # thin hint_words, missing test_exemption_reason) so the manifest
    # never lands in the broken state the verifier would flag. Best-
    # effort: a repair exception is captured in the RepairResult, not
    # raised, so a single bad repair can't block the manifest write.
    if repair:
        try:
            from .scanner_repair import repair_manifest as _do_repair
            manifest_dict, repair_result = _do_repair(
                manifest_dict, workspace=ws, bot_id=inventory.bot_id,
            )
            if repair_log is not None:
                tag = f"[repair {app.id}]"
                for line in repair_result.applied:
                    repair_log.append(f"{tag} APPLIED {line}")
                for line in repair_result.skipped:
                    repair_log.append(f"{tag} SKIP    {line}")
                for line in repair_result.errors:
                    repair_log.append(f"{tag} ERROR   {line}")
        except Exception as exc:  # noqa: BLE001 — never block on a repair bug
            if repair_log is not None:
                repair_log.append(
                    f"[repair {app.id}] ERROR   pass aborted: "
                    f"{type(exc).__name__}: {exc}"
                )

    return manifest_dict


# ── usage.model inference ───────────────────────────────────────────────────
#
# The bot-side Tier-2 discoverability verifier in
# packages/analyzer/app_audit_structural.py treats apps with
# usage.model in {"user-initiated", "ambient", ""} as user-routed and
# requires hint_words + example_triggers + interface_contract.cli on
# each. Apps with model in {"scheduled", "event-driven"} skip those
# checks — they run on their own trigger, the bot relays output rather
# than recognizing user intent.
#
# When the scanner produces a manifest with model unset, every
# scheduled app trips no_cli + no_example_triggers findings (114
# alerts pod-wide as of 2026-06-07). This helper closes that gap by
# inferring the model from what the scanner already detected.

_HEARTBEAT_TRIGGER_KINDS  = frozenset({"heartbeat"})
_CRON_TRIGGER_KINDS       = frozenset({"cron", "launchd", "systemd"})
_EVENT_TRIGGER_KINDS      = frozenset({"webhook", "event", "signal"})


# Phrases that indicate a library / on-demand app — called by other
# apps rather than user-routed. The "App must call X()" / "available
# on demand via X()" patterns are the strongest signal; the explicit
# "library" keyword also catches it.
#
# Production calibration 2026-06-08: Biometric Integration's example
# triggers contain "App must call get_token() first, auto-refresh if
# needed" — a textbook library-app pattern that the verifier
# misclassified as user-routed.
_LIBRARY_INDICATOR_PHRASES = (
    "app must call",
    "available on demand via",
    "called by other apps",
    "called by another app",
    "called on behalf of",
    "on behalf of the calling app",
    "library for",
    "token lifecycle",
    "is a library",
    "this library",
    "callable function",
    "on-demand access",
    "invoked by other apps",
)


def _looks_like_library_app(
    *,
    description: str = "",
    example_triggers: list | None = None,
    success_criteria: dict | None = None,
    identity: dict | None = None,
) -> bool:
    """Heuristic: does this manifest look like a library / on-demand
    app rather than a user-routed one?

    Walks description + example_triggers + success_criteria for the
    library-indicator phrases. Conservative — needs an unambiguous
    phrase, not just keyword overlap. Returns False on tie.
    """
    blobs: list[str] = []
    if description:
        blobs.append(description)
    if isinstance(example_triggers, list):
        for t in example_triggers:
            if isinstance(t, str):
                blobs.append(t)
    if isinstance(success_criteria, dict):
        for v in success_criteria.values():
            if isinstance(v, list):
                blobs.extend(str(x) for x in v if isinstance(x, str))
    if isinstance(identity, dict):
        scope = identity.get("scope_includes")
        if isinstance(scope, list):
            blobs.extend(str(x) for x in scope if isinstance(x, str))
    haystack = " \n ".join(blobs).lower()
    return any(p in haystack for p in _LIBRARY_INDICATOR_PHRASES)


def _infer_usage_model(
    *,
    scheduled_actions: list[dict] | None = None,
    heartbeat_evidence: dict | None = None,
    crons: list[dict] | None = None,
    cron_evidence: dict | None = None,
    description: str = "",
    example_triggers: list | None = None,
    success_criteria: dict | None = None,
    identity: dict | None = None,
) -> str:
    """Pick usage.model based on the scanner's discovered features.

    Returns one of:
        "scheduled"      — heartbeat- or cron-driven app
        "event-driven"   — webhook / signal-triggered
        "library"        — called by other apps; example_triggers and
                           description indicate on-demand invocation
        "user-initiated" — fallback when nothing else fits (preserves
                           the verifier's default behavior)

    Conservatism order: scheduled > event-driven > library > user-initiated.
    Heartbeat or cron evidence alone is enough to claim "scheduled"
    — even if no individual scheduled_actions entry survived
    attribution. Library detection runs LAST because the indicators
    aren't deterministic (prose patterns); only fires when scheduled/
    event paths didn't.

    Production calibration:
      - 2026-06-07: ``security-cve-scan`` on evo uses ``crons[]`` (OC-
        native cron format) without ``scheduled_actions[]``. Earlier
        inferrers missed it and returned ``user-initiated``.
      - 2026-06-08: Biometric Integration is a Whoop OAuth token-
        management library. example_triggers say "App must call
        get_token() first, auto-refresh if needed". Without the
        library detection below it returned user-initiated, the
        verifier treated it as user-routed, and C-A1 fired critical
        on the "daily" mention in the description (which referred to
        a CALLER, not this app's own schedule).
    """
    if heartbeat_evidence:
        return "scheduled"
    if cron_evidence:
        return "scheduled"
    # OC-native crons[]: any non-empty list means scheduled.
    if crons:
        for entry in crons:
            if isinstance(entry, dict) and (entry.get("schedule") or
                                              entry.get("name") or
                                              entry.get("task")):
                return "scheduled"
    trigger_kinds = set()
    for action in scheduled_actions or []:
        if not isinstance(action, dict):
            continue
        trigger = action.get("trigger") or {}
        if isinstance(trigger, dict):
            kind = (trigger.get("kind") or "").strip().lower()
            if kind:
                trigger_kinds.add(kind)
    if trigger_kinds & _HEARTBEAT_TRIGGER_KINDS:
        return "scheduled"
    if trigger_kinds & _CRON_TRIGGER_KINDS:
        return "scheduled"
    if trigger_kinds & _EVENT_TRIGGER_KINDS:
        return "event-driven"
    # Library detection runs after the deterministic paths to keep
    # scheduled/event apps from being misclassified by ambiguous prose.
    if _looks_like_library_app(
        description=description,
        example_triggers=example_triggers,
        success_criteria=success_criteria,
        identity=identity,
    ):
        return "library"
    return "user-initiated"


# ── Status file helpers ───────────────────────────────────────────────────────

def _write_status(
    status_path: Path,
    phase_num: int,
    found: int = 0,
    created: int = 0,
    total: int = 0,
    extra: dict | None = None,
    scan_log: list[str] | None = None,
) -> None:
    """Write phase progress to a JSON status file for the server to read.

    scan_log, if provided, is written as a ``log`` field (last 80 lines) so
    the admin server can surface it via /api/applications/scan/log even when
    the scanner runs inside the bot's plugin process and stdout is not captured.
    """
    phase_info = next((p for p in SCAN_PHASES if p["num"] == phase_num), SCAN_PHASES[-1])
    # Remaining time estimate: sum of remaining phases' ETA
    remaining_phases = [p for p in SCAN_PHASES if p["num"] >= phase_num]
    # For phase 5, scale ETA by number of apps
    eta_s = sum(
        p["eta_s"] * max(total, 1) if p["name"] == "manifests" else p["eta_s"]
        for p in remaining_phases
    )
    data = {
        "phase": phase_num,
        "phase_total": PHASE_TOTAL,
        "phase_name": phase_info["name"],
        "phase_desc": phase_info["desc"],
        "eta_seconds": max(1, eta_s),
        "found": found,
        "manifests_created": created,
        "manifests_total": total,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **(extra or {}),
    }
    if scan_log is not None:
        data["log"] = "\n".join(scan_log[-80:])
    tmp = status_path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data))
        tmp.replace(status_path)
    except OSError as e:
        print(f"[scanner] WARNING: could not write status file {status_path}: {e}", flush=True)


# ── DetectedApplication (kept for backward compat) ─────────────────────────────

@dataclass
class DetectedApplication:
    """Raw detection output — seeds a draft manifest."""
    id: str
    name: str
    confidence: float
    evidence_files: list[str]
    evidence_summary: str
    suggested_goals: list[str]
    suggested_tests: list[str]
    suggested_privacy: list[str]
    description: str = ""
    raw_content: dict[str, str] = field(default_factory=dict)
    source: str = "detected"


# ── Main public APIs ──────────────────────────────────────────────────────────

def scan_workspace(
    workspace: Path,
    oc_json_path: Path | None = None,
    min_confidence: float = 0.5,
    use_llm: bool = True,
    bot_id: str = "unknown",
) -> list[DetectedApplication]:
    """
    Backward-compat scan: returns DetectedApplication list.
    Runs LLM discovery (if use_llm).
    Does NOT generate manifests — use scan_workspace_pipeline for that.
    """
    inventory = collect_inventory(workspace, bot_id)
    detected: list[DetectedApplication] = []

    if use_llm:
        model, api_key = _resolve_llm("tier3")
        llm_results = llm_discover_applications(inventory, model, api_key=api_key)
        detected.extend(llm_results)

    # Deduplicate: highest confidence wins on id collision
    seen: dict[str, DetectedApplication] = {}
    for d in detected:
        if d.id not in seen or d.confidence > seen[d.id].confidence:
            seen[d.id] = d

    return sorted(
        (d for d in seen.values() if d.confidence >= min_confidence),
        key=lambda x: x.confidence,
        reverse=True,
    )


def scan_workspace_pipeline(
    workspace: Path,
    bot_id: str,
    shared_dir: Path,
    config: dict,
    use_llm: bool = True,
    min_confidence: float = 0.5,
    openclaw_cmd: str = "openclaw",
    output_dir: Path | None = None,
    user: str | None = None,
    repair: bool = True,
) -> list[dict]:
    """
    Full 4-phase pipeline. Discovers applications AND generates manifests.

    Manifests are written to `output_dir` when provided (canonical: bot's own
    workspace at ~/.openclaw/workspace/manifests/), otherwise falls back to
    shared_dir/applications/{bot_id}/ for backward compatibility.

    Status file is written alongside manifests (in caps_dir).  When the scanner
    runs as the bot user, the admin server (evolve user) additionally stamps
    shared_dir/applications/{bot_id}/.scan-status.json after the scan finishes,
    so tile_metrics.py can read a canonical location regardless of who ran the scan.

    Returns list of manifest dicts for all discovered apps (new + existing).
    """
    # Canonical manifest location is per-bot at
    # /Users/<bot>/.openclaw/workspace/manifests/. ``output_dir`` is kept as
    # an override for tests; ``shared_dir`` is no longer used here as a
    # write target (manifests are bot-state, not pod-state).
    if output_dir is not None:
        caps_dir = output_dir
    else:
        from .manifest import applications_dir as _apps_dir
        caps_dir = _apps_dir(shared_dir, bot_id)
    try:
        caps_dir.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        pass

    # Grant `evolve` write ACL on the manifests dir + _history subdir. The
    # scanner runs as the bot user (owner of caps_dir) so chmod +a succeeds
    # here; the admin server (`evolve`) needs this ACL for forge approval
    # updates, scheduled test results, and post-apply verification writes.
    # Idempotent — re-running is a no-op when the ACE is already present.
    if caps_dir.exists():
        history_dir = caps_dir / "_history"
        try:
            history_dir.mkdir(exist_ok=True)
        except (PermissionError, OSError):
            pass
        write_acl = (
            "user:evolve allow list,add_file,search,delete_child,"
            "readattr,writeattr,readextattr,writeextattr,readsecurity,"
            "file_inherit,directory_inherit"
        )
        for target in (caps_dir, history_dir):
            if not target.exists():
                continue
            try:
                subprocess.run(
                    ["/bin/chmod", "+a", write_acl, str(target)],
                    check=False, capture_output=True, text=True, timeout=5,
                )
            except Exception:
                pass

    # Status file lives alongside manifests so the scanner (bot user) can write it.
    # The admin server reads it back via `sudo -u {bot_user} cat`.
    status_path = caps_dir / ".scan-status.json"
    # Discovery (fast tier) + the purpose/fit classifier (bite D1 — a
    # stronger tier than discovery, see _stamp_app_kind) both resolve
    # through infra_llm below, once, and are shared by the mint gate +
    # reconcile reclassification pass.
    model = ""
    api_key = ""
    classifier_model = ""
    classifier_api_key = ""
    # Set when an LLM scan was requested but no provider key resolves — we
    # DEGRADE to a structural (--no-llm) scan rather than aborting with
    # error_kind="missing_api_key". Recorded in the terminal status so the
    # operator sees "ran structurally, no LLM key" instead of a red error.
    llm_degraded_reason: str | None = None

    # Scan log — accumulated and written into the status file so the admin UI
    # can surface it even when the scanner runs inside the bot's plugin process.
    _scan_log: list[str] = []

    def _slog(msg: str) -> None:
        print(msg, flush=True)
        _scan_log.append(msg)

    # ── Phase 1: Inventory ────────────────────────────────────────────────────
    os_user = user or bot_id
    _slog(f"[scanner] Phase 1: Inventory — workspace={workspace} user={os_user}")
    _write_status(status_path, 1, scan_log=_scan_log)
    inventory = collect_inventory(workspace, bot_id)
    inventory.user = os_user
    if use_llm:
        # Resolve the LLM targets ONCE here (shared by LLM discovery, the
        # mint gate, and the reclassification pass) via the provider-
        # agnostic infra_llm resolver. If no provider key resolves —
        # e.g. a pod whose primary bot authenticates via a gateway token
        # and never had a raw key — DEGRADE to a structural (--no-llm)
        # scan instead of aborting at Phase 2 with MissingApiKeyError.
        # The structural scan still inventories the workspace and stamps
        # stub manifests; the operator gets a clear "degraded — no LLM
        # provider key" note rather than a red terminal error.
        try:
            model, api_key = _resolve_llm("tier3")
            classifier_model, classifier_api_key = _resolve_llm(_CLASSIFIER_TIER)
        except Exception:  # noqa: BLE001 — never block a scan on key resolution
            classifier_api_key = ""
        if not classifier_api_key:
            use_llm = False
            model = ""
            classifier_model = ""
            llm_degraded_reason = "no_llm_provider_key"
            _slog(
                "[scanner] No LLM provider credentialed — degrading to a "
                "structural (--no-llm) scan (checked the provider API-key "
                "env vars and the primary bot's OpenClaw auth store)."
            )
    print(
        f"[scanner] Inventory complete: cron_jobs={len(inventory.cron_jobs)}, "
        f"named_dirs={len(inventory.named_dirs)}, memory_files={len(inventory.memory_files)}, "
        f"python_scripts={len(inventory.python_scripts)}, shell_scripts={len(inventory.shell_scripts)}",
        flush=True,
    )
    app_crons = [c for c in inventory.cron_jobs if not c.get("is_infrastructure")]
    infra_crons = [c for c in inventory.cron_jobs if c.get("is_infrastructure")]
    print(
        f"[scanner]   crons: {len(app_crons)} app-crons, {len(infra_crons)} infra (filtered)",
        flush=True,
    )
    for c in app_crons:
        print(f"[scanner]     cron: {c['script_path']}", flush=True)
    for d in inventory.named_dirs:
        print(f"[scanner]     dir: {d['name']}/  files={d['files'][:3]}", flush=True)

    # ── Phase 2: LLM discovery ────────────────────────────────────────────────
    llm_hits: list[DetectedApplication] = []
    if use_llm:
        _slog(f"[scanner] Phase 2: LLM discovery — model={model}, cmd={openclaw_cmd}")
        _write_status(status_path, 2, scan_log=_scan_log)
        try:
            llm_hits = llm_discover_applications(inventory, model, openclaw_cmd,
                                                   cwd=str(inventory.workspace),
                                                   api_key=api_key)
            _slog(f"[scanner] LLM returned {len(llm_hits)} candidate(s)")
            for h in llm_hits:
                _slog(f"[scanner]   - {h.id}: {h.name} (conf={h.confidence:.0%})")
        except MissingApiKeyError as e:
            # DEGRADE, don't hard-error. The up-front key probe (Phase 1) already
            # flips use_llm off when no key resolves, so this is a defensive
            # backstop for a key that vanished mid-scan. Fall through to a
            # structural scan with empty llm_hits instead of writing a terminal
            # error_kind="missing_api_key" status (which the admin UI rendered as
            # a red failure even though a structural scan was possible).
            _slog(f"[scanner] LLM discovery has no key — degrading to structural: {e}")
            use_llm = False
            llm_degraded_reason = "no_llm_provider_key"
            llm_hits = []
        except Exception as e:
            import traceback as _tb
            _slog(f"[scanner] LLM discovery FAILED: {e}")
            _slog(_tb.format_exc())
    else:
        _slog("[scanner] Phase 2: LLM skipped (--no-llm)")
        _write_status(status_path, 2, scan_log=_scan_log)

    # ── Phase 3: Merge ────────────────────────────────────────────────────────
    _slog(f"[scanner] Phase 3: Merge — {len(llm_hits)} hits, min_confidence={min_confidence}")
    _write_status(status_path, 3, found=len(llm_hits), scan_log=_scan_log)
    seen: dict[str, DetectedApplication] = {}
    for d in llm_hits:
        if d.id not in seen or d.confidence > seen[d.id].confidence:
            seen[d.id] = d
    merged = sorted(
        (d for d in seen.values() if d.confidence >= min_confidence),
        key=lambda x: x.confidence,
        reverse=True,
    )
    print(f"[scanner] After merge: {len(merged)} unique app(s) above confidence threshold", flush=True)

    # Skip apps that already have manifests (don't overwrite good data),
    # but keep the DetectedApplications around — we use them below to
    # backfill empty identity/success fields on existing manifests without
    # needing a second LLM discovery pass.
    #
    # Match by a richer key than filename stem: detected ids drift from
    # the existing stems ("app-" prefix gain/loss, slug case/hyphenation,
    # v7-arc instance ids like ``i-34cfcab1`` that the LLM cannot
    # reproduce). See _match_detected_to_existing for the precedence and
    # 2026-06-08 production incident (18 apps → 13 misclassified as new).
    existing_index = _build_existing_manifest_index(caps_dir, shared_dir)
    claimed_paths: set[Path] = set()
    new_apps: list[DetectedApplication] = []
    # Map existing manifest *path* → DetectedApplication that claimed it.
    # Keyed by Path (not stem) because the detected id and stem may
    # differ — Pass A backfill below reads ``existing_app_hits[mf]``.
    existing_app_hits: dict[Path, DetectedApplication] = {}
    for d in merged:
        match = _match_detected_to_existing(d, existing_index, claimed_paths)
        if match is None:
            new_apps.append(d)
        else:
            existing_app_hits[match["path"]] = d
            claimed_paths.add(match["path"])
    total_found = len(merged)
    print(
        f"[scanner] Manifests dir: {caps_dir}  existing={len(existing_index)}  "
        f"matched={len(existing_app_hits)}  new={len(new_apps)}",
        flush=True,
    )

    # ── Phase 4: Manifest generation ─────────────────────────────────────────
    _slog(f"[scanner] Phase 4: Generating {len(new_apps)} manifest(s)")
    _write_status(status_path, 4, found=total_found, created=0, total=len(new_apps),
                  scan_log=_scan_log)
    created = 0

    _app_num = 0  # tracks which app we're currently generating (approximate for concurrent workers)

    # Idempotent re-discovery (apps-idempotent-rediscovery): a freshly-generated
    # manifest whose files already belong to an installed Instance must REUSE
    # that Instance's spec_id, not mint a fresh ``p-`` that orphans the prior
    # generation. ``_reuse_claimed`` (lock-guarded — workers run concurrently)
    # ensures two re-discovered apps never bind the same Instance.
    _reuse_lock = threading.Lock()
    _reuse_claimed: set[Path] = set()

    def _gen_and_save(app: DetectedApplication) -> str:
        nonlocal created, _app_num
        _app_num += 1
        cur_num = _app_num
        _slog(f"[scanner]   Generating manifest {cur_num}/{len(new_apps)}: {app.name} ({app.id})")
        # Write status before LLM call so the UI shows which app is in progress
        _write_status(status_path, 4, found=total_found, created=created, total=len(new_apps),
                      scan_log=_scan_log,
                      extra={"current_app_name": app.name,
                             "current_app_num": cur_num})
        try:
            manifest = generate_manifest_for_app(
                app, inventory, model, openclaw_cmd,
                api_key=api_key, repair=repair, repair_log=_scan_log,
            ) if use_llm else _stub_manifest(app, bot_id)
            # 2026-05-29 — when a new manifest is being created and the
            # bot has a ``backup_default_tier`` configured in
            # network.json, stamp the tier template onto the manifest
            # before it's persisted. No-op if the bot has no default or
            # the manifest already declares classification fields. See
            # data_classification.stamp_per_bot_default.
            try:
                from data_classification import stamp_per_bot_default  # type: ignore
                manifest = stamp_per_bot_default(
                    manifest, bot_id=bot_id, network=load_network(),
                )
            except Exception as exc:  # noqa: BLE001 — never block scan on the stamp
                _slog(f"[scanner]   stamp default tier failed for {app.id}: {exc}")
            # ── Mint gate (bite D1): classify goal-application vs capability
            # and stamp app_kind BEFORE the manifest first lands on disk, so a
            # new skill is tagged from birth in BOTH write paths (the v7-arc
            # mint carries app_kind/classification onto the Instance via
            # _NATIVE_INSTANCE_PASSTHROUGH; the legacy write keeps them on the
            # dict). evidence_files are the real surface here — Phase 5 hasn't
            # stamped files[] yet. Non-destructive: a capability is real owned
            # code, so it is labeled, never archived. Conservative on failure:
            # left unstamped → inert "application" default → retried next scan.
            if use_llm and classifier_api_key:
                if _stamp_app_kind(
                    manifest,
                    model=classifier_model,
                    api_key=classifier_api_key,
                    extra_files=list(app.evidence_files or []),
                    log=_slog,
                ):
                    _slog(
                        f"[scanner]   purpose/fit: {app.name} → "
                        f"{manifest.get('app_kind')} "
                        f"(conf={manifest.get('classification', {}).get('confidence')})"
                    )
            # ── Slice 3a: native v7-arc mint (manifest-v7 native-write
            # cutover, docs/spec-manifest-v7-slicing-2026-06-10.md §5.1).
            # The freshly generated dict is split into Spec (gallery/local)
            # + v7-arc Instance (caps_dir/<id>.json) before it ever lands
            # on disk in legacy form. Falls back to the legacy single-file
            # write on any failure — a legacy manifest is valid by
            # construction and the next migrate_v7 run converts it.
            minted = False
            reuse_state = False
            try:
                from .native_write import mint_scanner_detection

                # ── Idempotent re-discovery (apps-idempotent-rediscovery) ────
                # Before minting a fresh spec_id, check whether this manifest's
                # files already belong to an installed Instance the identity
                # matcher missed. On a confident match, redirect the manifest
                # onto that Instance (its stem + spec_id) so the mint UPDATES it
                # in place and REUSES the live spec_id — instead of minting a
                # new ``p-`` that strands the prior generation as orphans. The
                # claim is taken under a lock so concurrent workers can't bind
                # two detected apps to the same Instance.
                with _reuse_lock:
                    reuse = _rediscovery_match(
                        manifest, existing_index,
                        claimed=claimed_paths | _reuse_claimed,
                    )
                    if reuse is not None:
                        _reuse_claimed.add(reuse["path"])
                if reuse is not None:
                    reuse_sid = reuse["data"]["provenance"]["spec_id"]
                    reuse_stem = reuse["path"].stem
                    # pkg_id seeds _resolve_spec_id → it returns the existing
                    # spec_id (no fresh mint); id steers the Instance write onto
                    # the existing file (instance_id == filename stem invariant).
                    manifest["pkg_id"] = reuse_sid
                    manifest["id"] = reuse_stem
                    reuse_state = True
                    _slog(
                        f"[scanner]   Re-discovery: {app.id} overlaps installed "
                        f"instance {reuse['path'].name} (spec {reuse_sid}); "
                        f"reusing spec_id, updating in place (no new id minted)"
                    )

                mint = mint_scanner_detection(
                    manifest,
                    shared_dir=shared_dir,
                    bot_id=bot_id,
                    caps_dir=caps_dir,
                    installed_by="scanner_rediscovery" if reuse_state else "scanner",
                    preserve_instance_state_from_disk=reuse_state,
                )
                if mint.succeeded:
                    minted = True
                    _slog(
                        f"[scanner]   Minted v7-arc: spec {mint.spec_id} "
                        f"(instance {mint.instance_id})"
                    )
                    for w in mint.warnings:
                        _slog(f"[scanner]   v7-arc mint: {w}")
                else:
                    _slog(
                        f"[scanner]   v7-arc mint failed for {app.id} "
                        f"({'; '.join(mint.errors)}); writing legacy shape"
                    )
            except Exception as _exc:  # noqa: BLE001 — fall back to legacy write
                _slog(
                    f"[scanner]   v7-arc mint failed for {app.id} "
                    f"({_exc}); writing legacy shape"
                )
            if not minted:
                # v20: stamp observational provenance on every field of a
                # fresh-discovery LEGACY manifest. Spec: docs/spec-app-
                # coherence-and-reconciliation-2026-06-05.md PR 2 + §4.3.
                # v7-arc Instances don't carry field_origins — identity
                # lives in provenance.spec_id — so the stamp only applies
                # on the legacy fallback path.
                #
                # The manifest dict is being written here without going through
                # save_manifest (the scanner skips the dataclass round-trip to
                # avoid losing scanner-only fields). We stamp directly on the
                # dict using stamp_field_origins; the migration loop will then
                # leave the stamp intact on first read.
                try:
                    from .manifest import (
                        stamp_field_origins,
                        PROVENANCE_OBSERVATIONAL,
                    )
                    stamp_field_origins(
                        manifest,
                        source=PROVENANCE_OBSERVATIONAL,
                        fields=None,    # stamp every top-level field
                        by="scanner",
                        via="scan_phase_4",
                    )
                except Exception as _exc:  # noqa: BLE001 — never block scan on the stamp
                    _slog(f"[scanner]   stamp provenance failed for {app.id}: {_exc}")
                # On a re-discovery reuse whose v7-arc mint fell through, honor
                # the redirect: write to the matched Instance's stem (carrying
                # the reused pkg_id), not a parallel ``app.id`` file — so the
                # legacy fallback updates the same app rather than orphaning it.
                _legacy_stem = manifest.get("id") if reuse_state else app.id
                _atomic_write(caps_dir / f"{_legacy_stem}.json", manifest, mode=0o644)
            created += 1
            _slog(f"[scanner]   Done: {app.name} ({created}/{len(new_apps)})")
            _write_status(status_path, 4, found=total_found, created=created, total=len(new_apps),
                          scan_log=_scan_log)
        except Exception as e:
            import traceback as _tb2
            _slog(f"[scanner]   FAILED manifest for {app.id}: {e}")
            _slog(_tb2.format_exc())
        return app.id

    if new_apps:
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {ex.submit(_gen_and_save, app): app for app in new_apps}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    print(f"[scanner] Future exception: {e}", flush=True)

    # ── Deduplication: merge manifests that are clearly the same application ──
    # shared_dir is required for v7-arc Instance hydration (name + files
    # come from the bound Spec under {shared_dir}/gallery/).
    merged_count = _dedup_manifests(caps_dir, shared_dir=shared_dir)
    if merged_count:
        print(f"[scanner] Dedup: merged {merged_count} duplicate manifest(s)", flush=True)

    # ── L3 platform-files defense: archive no-content stub manifests whose ──
    # entire file footprint is OC-infrastructure output (e.g. legacy v13-
    # migrated "Session Turn Logs" pointing at memory/turns-*.jsonl —
    # written by /Users/Shared/openclaw-usage/turn-collector.py, not by
    # any bot app). Runs after dedup so we don't archive a manifest that
    # was about to be merged; runs before Phase 5 so the stamp pass
    # doesn't waste work on a manifest we're about to remove. Conservative
    # guards (see _archive_platform_file_only_stubs docstring) keep this
    # from touching real apps that happen to attach platform files.
    platform_stub_archived = _archive_platform_file_only_stubs(
        caps_dir, log_collector=_scan_log, shared_dir=shared_dir,
    )
    if platform_stub_archived:
        _slog(
            f"[scanner] L3 platform-stub sweep: archived "
            f"{len(platform_stub_archived)} manifest(s)"
        )

    # ── Phase 5: Stamp discovered files with provenance markers ──────────────
    # Walk every manifest that was generated (or touched by dedup) in this scan,
    # plus any existing manifest that still has an empty files list (catches
    # manifests generated before Phase 5 existed or before the path-resolution fix).
    # Best-effort: failures per manifest are logged and skipped.
    new_app_ids = {d.id for d in new_apps}
    stamp_targets = []
    for mf in sorted(caps_dir.glob("*.json")):
        if mf.name.startswith(".") or "_history" in str(mf):
            continue
        if mf.stem in new_app_ids:
            stamp_targets.append(mf)  # newly generated — always stamp
        else:
            # Existing manifest — stamp only if files list is still empty
            try:
                existing_data = json.loads(mf.read_text())
                if not existing_data.get("files"):
                    stamp_targets.append(mf)
            except Exception:
                pass
    if stamp_targets:
        _slog(f"[scanner] Phase 5: Stamping files for {len(stamp_targets)} manifest(s)")
        # We call _write_status with phase_num=4 (last defined phase) so it resolves
        # the phase_info correctly, then override phase/phase_total in `extra` to
        # surface a logical Phase 5 in the UI without adding it to SCAN_PHASES.
        _write_status(status_path, 4, found=total_found, created=created, total=len(new_apps),
                      scan_log=_scan_log,
                      extra={"phase": 5, "phase_total": PHASE_TOTAL,
                             "phase_name": "file_stamp",
                             "phase_desc": f"Registering component files for {len(stamp_targets)} application(s)",
                             "eta_seconds": max(5, len(stamp_targets) * 3)})
        for mf in stamp_targets:
            try:
                manifest_data = json.loads(mf.read_text())
                _stamp_discovered_files(manifest_data, inventory.workspace, mf,
                                        log_collector=_scan_log)
                # Flush log to status file after each manifest so progress is live
                _write_status(status_path, 4, found=total_found, created=created,
                               total=len(new_apps), scan_log=_scan_log,
                               extra={"phase": 5, "phase_total": PHASE_TOTAL,
                                      "phase_name": "file_stamp",
                                      "phase_desc": f"Registering component files for {len(stamp_targets)} application(s)"})
            except Exception as e:
                import traceback as _tb5
                _slog(f"[scanner] Phase 5: stamp FAILED for {mf.name}: {e}")
                _slog(_tb5.format_exc())

    # ── Phase 5.5: Layer classification ───────────────────────────────────────
    # Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §3.1 +
    # §3.5 + §7.1. PR 3.
    #
    # Re-classify every files[*] entry against the v20 layer enum (code /
    # config / contract / behavior_doc / reference / content / data / log
    # / state). Runs on every manifest in caps_dir — not just newly-
    # stamped ones — because validation against the production pod
    # surfaced legacy mis-classifications across existing manifests
    # (personal-bot's HEARTBEAT.md stamped 'state' instead of 'behavior_doc';
    # content directories' .md files stamped 'state' instead of 'content').
    # The classifier is pure-Python and idempotent — running on
    # already-classified manifests is a no-op.
    #
    # The new layer values are written back via _atomic_write so they
    # persist for PR 4's reconciliation pass. _legacy_layer is captured
    # on first transition (preserving PR 1's migration capture of
    # "script" → "code" transitions).
    #
    # Best-effort: a classifier failure on one manifest doesn't block
    # the rest.
    classified_count = 0
    classified_changes = 0
    try:
        from .layer_classifier import apply_to_manifest as _classify_manifest
    except Exception as _exc:  # noqa: BLE001
        _slog(f"[scanner] Phase 5.5: classifier import failed: {_exc}")
        _classify_manifest = None
    if _classify_manifest is not None:
        classify_targets = [mf for mf in sorted(caps_dir.glob("*.json"))
                            if not mf.name.startswith(".") and "_history" not in str(mf)]
        if classify_targets:
            _slog(f"[scanner] Phase 5.5: Classifying file layers for "
                  f"{len(classify_targets)} manifest(s)")
            _write_status(status_path, 4, found=total_found, created=created,
                          total=len(new_apps), scan_log=_scan_log,
                          extra={"phase": 6, "phase_total": PHASE_TOTAL,
                                 "phase_name": "layer_classify",
                                 "phase_desc": (
                                     f"Re-classifying file layers for "
                                     f"{len(classify_targets)} application(s)"
                                 ),
                                 "eta_seconds": max(2, len(classify_targets))})
            for mf in classify_targets:
                try:
                    manifest_data = json.loads(mf.read_text())
                    changes = _classify_manifest(manifest_data)
                    if changes:
                        # Only persist when something actually changed —
                        # avoids no-op rewrites of every manifest on every
                        # scan tick.
                        _atomic_write(mf, manifest_data, mode=0o644)
                        classified_changes += len(changes)
                        _slog(f"[scanner]   {mf.stem}: {len(changes)} layer "
                              f"transition(s)")
                        for path, prior, new in changes[:5]:
                            _slog(f"[scanner]     {path}: {prior!r} → {new!r}")
                        if len(changes) > 5:
                            _slog(f"[scanner]     ... and {len(changes) - 5} more")
                    classified_count += 1
                except Exception as e:
                    import traceback as _tb55
                    _slog(f"[scanner] Phase 5.5: classify FAILED for {mf.name}: {e}")
                    _slog(_tb55.format_exc())
            _slog(f"[scanner] Phase 5.5: Classified {classified_count} "
                  f"manifest(s), {classified_changes} layer transition(s) total")

    # ── Phase 5.6: Stale owned_by cleanup ────────────────────────────────────
    # Production validation 2026-06-08: 78 file entries pod-wide carried
    # ``owned_by`` tags pointing at 9 distinct dead pkg_ids — symptom
    # was a personal-bot's Biometric Integration app showing 9 of its
    # 16 files as "orphaned"/"external" in the admin UI because the
    # dashboard classifier compares ``files[*].owned_by`` against the
    # manifest's own ``pkg_id`` and any mismatch lights up. The dead pkg_ids
    # weren't anywhere — likely the manifest's own previous identity
    # from before a forge rebuild — so the only repair is to clear
    # the dangling tag and let the file_index default it back to the
    # manifest's own pkg_id.
    #
    # Conservative: only clears when the target pkg_id is definitely
    # NOT alive on the pod. Legitimate cross-app sharing (live
    # ``owned_by`` on a different live manifest) is left untouched.
    # Live set is gathered from the current bot's manifests; the
    # diagnosis showed zero cross-bot legitimate refs so this is
    # sufficient. ``extra_live_pkg_ids`` is a hook for future
    # cross-bot widening.
    try:
        from .scanner_repair import repair_stale_owned_by_in_dir as _stale_owned_by
    except Exception as _exc:  # noqa: BLE001
        _slog(f"[scanner] Phase 5.6: stale owned_by import failed: {_exc}")
        _stale_owned_by = None
    if _stale_owned_by is not None:
        try:
            stale_result, stale_diag = _stale_owned_by(caps_dir, write=True)
        except Exception as _exc:  # noqa: BLE001
            _slog(
                f"[scanner] Phase 5.6: stale owned_by pass aborted "
                f"(non-fatal): {type(_exc).__name__}: {_exc}"
            )
        else:
            # Phase 1 diagnostic: log the pre-cleanup snapshot — counts
            # per dead pkg_id, with example file paths — even when the
            # cleanup is a no-op. This is what surfaces the underlying
            # accumulation pattern to the operator.
            if stale_diag:
                total = sum(stale_diag.values())
                _slog(
                    f"[scanner] Phase 5.6: found {total} file(s) with "
                    f"owned_by pointing at {len(stale_diag)} dead "
                    f"pkg_id(s) on this bot"
                )
                for dead_pkg, n in sorted(
                    stale_diag.items(), key=lambda kv: -kv[1],
                ):
                    _slog(
                        f"[scanner]   dead pkg_id {dead_pkg}: "
                        f"{n} file(s) tagged"
                    )
            else:
                _slog(
                    "[scanner] Phase 5.6: no stale owned_by tags on this bot"
                )
            # Phase 2 cleanup log: applied/skipped/errors. Skipped lines
            # are noisy (1 per manifest) so summarise rather than dump
            # them all.
            for line in stale_result.applied:
                _slog(f"[scanner]   APPLIED {line}")
            for line in stale_result.errors:
                _slog(f"[scanner]   ERROR   {line}")
            skipped_n = len(stale_result.skipped)
            if skipped_n and not stale_result.applied:
                _slog(
                    f"[scanner] Phase 5.6: {skipped_n} manifest(s) "
                    f"had no stale tags (skipped)"
                )

    # ── Phase 6: Reconciliation pass (provenance-aware) ───────────────────────
    # Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §7.4.
    # PR 4 — the first PR where the spec's central doctrine becomes
    # operationally visible: silent for observational fields, chip for
    # authored fields.
    #
    # For every manifest in caps_dir, walk the delta between claims and
    # workspace state. Observational fields drop missing entries
    # silently; authored fields stage in reconciliation.<list>[]. The
    # downstream UI (PR 7+) reads the staged lists and renders chips.
    #
    # Skips if no manifests are present. Best-effort: per-manifest
    # failures don't block the rest.
    try:
        from .reconciliation import apply_reconciliation as _apply_reconciliation
    except Exception as _exc:  # noqa: BLE001
        _slog(f"[scanner] Phase 6: reconciliation import failed: {_exc}")
        _apply_reconciliation = None
    # Bite 3 (spec §9.3): drift-significance classifier. Narrates MAJOR drift
    # into manifest.drift_log for ``defined`` apps. Best-effort, independent of
    # the reconcile import so a classifier import failure never aborts reconcile.
    # llm_fn=None here: the reconcile deltas are add/remove (+ action-anchor
    # modify, a deterministic-major behavioral surface), so the LLM escalation
    # path — the ambiguous *script-content* modify — is not reachable from this
    # feed and stays a deterministic-only pass (cheap-floor discipline). The
    # injectable seam is exercised by the unit tests.
    try:
        from .drift_classifier import narrate_drift as _narrate_drift
    except Exception as _exc:  # noqa: BLE001
        _slog(f"[scanner] Phase 6: drift_classifier import failed: {_exc}")
        _narrate_drift = None
    if _apply_reconciliation is not None:
        recon_targets = [mf for mf in sorted(caps_dir.glob("*.json"))
                         if not mf.name.startswith(".")
                         and "_history" not in str(mf)]
        if recon_targets:
            _slog(f"[scanner] Phase 6: Reconciling {len(recon_targets)} "
                  f"manifest(s) against workspace state")
            _write_status(status_path, 4, found=total_found, created=created,
                          total=len(new_apps), scan_log=_scan_log,
                          extra={"phase": 7, "phase_total": PHASE_TOTAL,
                                 "phase_name": "reconcile",
                                 "phase_desc": (
                                     f"Reconciling manifest claims vs "
                                     f"workspace for {len(recon_targets)} "
                                     f"application(s)"
                                 ),
                                 "eta_seconds": max(2, len(recon_targets))})
            recon_silent = 0
            recon_staged = 0
            recon_drift_logged = 0
            for mf in recon_targets:
                try:
                    manifest_data = json.loads(mf.read_text())
                    summary = _apply_reconciliation(
                        manifest_data, inventory.workspace,
                        bot_id=bot_id,
                    )
                    # Bite 3: narrate MAJOR drift into drift_log for ``defined``
                    # apps. A ``discovered`` app is a churnable draft — the
                    # reconcile above already refreshed its content, no
                    # narrative needed (narrate_drift no-ops for it). MINOR
                    # drift (data/doc) is absorbed silently, never logged.
                    narrated = 0
                    if _narrate_drift is not None:
                        try:
                            narrated = _narrate_drift(
                                manifest_data,
                                summary.drift_events,
                                definition_status=manifest_data.get(
                                    "definition_status", ""),
                                llm_fn=None,
                            )
                        except Exception as _de:  # noqa: BLE001 — never block scan
                            import traceback as _tbd
                            _slog(f"[scanner] Phase 6: drift narrate FAILED "
                                  f"for {mf.name}: {_de}")
                            _slog(_tbd.format_exc())
                    # Persist when ANY change occurred — silent updates
                    # mutate files[]/crons[]; staged entries land in
                    # reconciliation.*; drift narration appends to drift_log.
                    # All need to be written back.
                    if summary.silent_total or summary.staged_total or narrated:
                        _atomic_write(mf, manifest_data, mode=0o644)
                        recon_silent += summary.silent_total
                        recon_staged += summary.staged_total
                        recon_drift_logged += narrated
                        _slog(
                            f"[scanner]   {mf.stem}: "
                            f"silent={summary.silent_total} "
                            f"staged={summary.staged_total} "
                            f"drift_logged={narrated}"
                        )
                except Exception as e:
                    import traceback as _tb6
                    _slog(f"[scanner] Phase 6: reconcile FAILED for "
                          f"{mf.name}: {e}")
                    _slog(_tb6.format_exc())
            _slog(f"[scanner] Phase 6: Reconciled "
                  f"{len(recon_targets)} manifest(s); "
                  f"{recon_silent} silent update(s), "
                  f"{recon_staged} staged entries, "
                  f"{recon_drift_logged} drift entries logged")

    # ── Phase 6.5: Purpose/fit reclassification (bite D1; Slice 2) ────────────
    # The reconcile-side leg of the classifier (parallels the floor's
    # archive-at-reconcile). The mint gate (Phase 4) tags newly-minted
    # manifests; this pass re-judges manifests ALREADY on disk that are absent a
    # classification block OR carry one from an OLDER classifier vocabulary —
    # e.g. a personal-bot's "Google Services Integration" (a bare OAuth
    # capability), or a pod's "Operations Automation" / heartbeat monitors that
    # #2899 stamped {kind:application} before the Slice-2 "system" verdict
    # existed. This is the path that fixes the live state.
    #
    # Idempotency key = a classification block PRESENT AND at the current
    # ``classifier_version`` (purpose_classifier.needs_reclassification): a
    # manifest the mint gate already judged this scan is skipped (no double LLM
    # call), and a current sticky judgment from a prior scan is not re-spent. An
    # absent block (transient failure) or a stale-version block (vocabulary
    # upgrade, e.g. v1/#2899 → v2) is re-judged here / next scan. NON-DESTRUCTIVE:
    # a capability/system is real owned code — it is labeled, never archived (the
    # Apps page filters on the label in the follow-on UI bite).
    if use_llm and classifier_api_key:
        from .purpose_classifier import needs_reclassification
        classify_targets = [
            mf for mf in sorted(caps_dir.glob("*.json"))
            if not mf.name.startswith(".") and "_history" not in str(mf)
        ]
        judged = 0
        capability_count = 0
        system_count = 0
        for mf in classify_targets:
            try:
                manifest_data = json.loads(mf.read_text())
            except Exception as e:  # noqa: BLE001
                _slog(f"[scanner] Phase 6.5: read FAILED for {mf.name}: {e}")
                continue
            # Already judged at the current classifier_version (mint gate this
            # scan, or a current prior scan) → skip. Absent / stale-version →
            # (re)judge so a vocabulary upgrade reaches manifests on disk.
            if not needs_reclassification(manifest_data):
                continue
            bound_spec = _load_bound_spec(manifest_data, shared_dir)
            try:
                stamped = _stamp_app_kind(
                    manifest_data,
                    model=classifier_model,
                    api_key=classifier_api_key,
                    bound_spec=bound_spec,
                    log=_slog,
                )
            except Exception as e:  # noqa: BLE001 — never block the scan
                import traceback as _tb65
                _slog(f"[scanner] Phase 6.5: classify FAILED for {mf.name}: {e}")
                _slog(_tb65.format_exc())
                continue
            if not stamped:
                continue
            judged += 1
            kind = manifest_data.get("app_kind")
            if kind == "capability":
                capability_count += 1
            elif kind == "system":
                system_count += 1
            try:
                _atomic_write(mf, manifest_data, mode=0o644)
            except Exception as e:  # noqa: BLE001
                _slog(f"[scanner] Phase 6.5: write FAILED for {mf.name}: {e}")
                continue
            _slog(
                f"[scanner]   {mf.stem}: app_kind={kind} "
                f"(conf={manifest_data.get('classification', {}).get('confidence')})"
            )
        if judged:
            _slog(
                f"[scanner] Phase 6.5: classified {judged} manifest(s); "
                f"{capability_count} labeled capability, {system_count} system"
            )

    # ── Phase 7: Coherence Pass A (manifest-internal graph walk) ──────────────
    # Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §6.1.
    # Pure-Python; no filesystem, no subprocess, no LLM. ~10ms per app.
    # Catches claims-vs-mechanisms incoherences (the protein-reminder-
    # style failure: description claims "daily briefing" but no
    # scheduled_action / cron / heartbeat instruction declared).
    #
    # Provenance-INDEPENDENT (spec §4.5): even observational manifests
    # get checked. If the scanner-discovered description claims
    # something the implementation can't support, the operator should
    # see it.
    try:
        from .coherence_pass_a import apply_pass_a as _apply_pass_a
        from .pass_runner import hydrate_if_needed as _hydrate_if_needed
    except Exception as _exc:  # noqa: BLE001
        _slog(f"[scanner] Phase 7: coherence_pass_a import failed: {_exc}")
        _apply_pass_a = None
        _hydrate_if_needed = None
    if _apply_pass_a is not None:
        coh_targets = [mf for mf in sorted(caps_dir.glob("*.json"))
                       if not mf.name.startswith(".")
                       and "_history" not in str(mf)]
        if coh_targets:
            _slog(f"[scanner] Phase 7: Coherence Pass A on "
                  f"{len(coh_targets)} manifest(s)")
            _write_status(status_path, 4, found=total_found, created=created,
                          total=len(new_apps), scan_log=_scan_log,
                          extra={"phase": 8, "phase_total": PHASE_TOTAL,
                                 "phase_name": "coherence_pass_a",
                                 "phase_desc": (
                                     f"Pure-Python coherence checks on "
                                     f"{len(coh_targets)} application(s)"
                                 ),
                                 "eta_seconds": max(2, len(coh_targets) // 5)})
            findings_total = 0
            incoherent_count = 0
            for mf in coh_targets:
                try:
                    raw = json.loads(mf.read_text())
                    # v7-arc Instances carry empty legacy files[]/
                    # scheduled_actions[]; their objective + description live
                    # on the bound Spec. Hydrate so Pass A evaluates the real
                    # claims/surfaces, not the stub. Legacy manifests pass
                    # through unchanged. (coherence_pass_a's helpers also read
                    # realized_files/configured_schedules directly, so this is
                    # belt-and-suspenders — single source of truth per spec.)
                    eval_manifest = raw
                    if _hydrate_if_needed is not None:
                        eval_manifest = _hydrate_if_needed(raw, shared_dir)
                    summary = _apply_pass_a(eval_manifest)
                    # Persist only the coherence block back onto the on-disk
                    # Instance — the hydrated Spec overlay (description,
                    # files[], …) must not be written into the Instance file.
                    raw["coherence"] = eval_manifest.get("coherence")
                    _atomic_write(mf, raw, mode=0o644)
                    if summary["findings_count"]:
                        findings_total += summary["findings_count"]
                        if summary["status"] == "incoherent":
                            incoherent_count += 1
                            _slog(
                                f"[scanner]   {mf.stem}: "
                                f"{summary['findings_count']} finding(s); "
                                f"status=incoherent"
                            )
                except Exception as e:
                    import traceback as _tb7
                    _slog(f"[scanner] Phase 7: coherence FAILED for "
                          f"{mf.name}: {e}")
                    _slog(_tb7.format_exc())
            _slog(f"[scanner] Phase 7: {findings_total} coherence finding(s) "
                  f"across {len(coh_targets)} manifest(s); "
                  f"{incoherent_count} incoherent")

    # ── Patch existing manifests with empty required fields ───────────────────
    # Two passes, both conservative ("only-if-empty" — never overwrite hand-edits):
    #
    #   Pass A — identity / success_criteria / constraints backfill via LLM.
    #     Triggered when the scanner re-detected an app this scan AND the existing
    #     manifest is missing any of: identity.purpose, identity.scope_includes,
    #     identity.user, success_criteria.observable_outcomes. Calls
    #     generate_manifest_for_app to get a fresh draft, then merges only into
    #     fields that are currently empty/null/[]/{}.  Stub-mode (use_llm=False)
    #     skips this pass since there's no LLM data to draw from.
    #
    #   Pass B — derive description from identity.purpose when description is empty.
    #     Cheap, pure-Python derivation.  Runs after Pass A so newly-backfilled
    #     purpose feeds into the description.
    #
    # The original implementation only had Pass B, which left scanner-discovered
    # apps stuck with empty Purpose/Scope/User indefinitely (the UI rendered them
    # as bare dashes on the detail panel). Pass A fixes that.
    patch_count = 0
    identity_backfill_count = 0

    def _is_empty(v) -> bool:
        return v is None or v == "" or (isinstance(v, (list, dict)) and not v)

    for mf in sorted(caps_dir.glob("*.json")):
        if mf.name.startswith(".") or "_history" in str(mf):
            continue
        if mf.stem in new_app_ids:
            continue  # just generated — skip
        try:
            data = json.loads(mf.read_text())
            patched = False

            # ── Pass A: identity / success_criteria / constraints +
            #           producer-surface backfill ───────────────────────────
            # existing_app_hits is keyed by Path (not stem) so the detected
            # id can differ from the filename — e.g. detected id
            # ``biometric-integration`` matched against existing stem
            # ``app-biometric-integration``.
            #
            # Two backfill targets:
            #   1. Identity/Success/Constraints — closes the empty
            #      Purpose/Scope/User gap on first re-detection of a
            #      sparsely-migrated app.
            #   2. Producer-surface evidence (scheduled_actions,
            #      heartbeat_evidence, cron_evidence, crons,
            #      event_triggers, interface_contract) — the migration
            #      from v13 → v7-arc Instance dropped these (they live on
            #      the v6 manifest dict that the scanner now writes, and
            #      neither Spec nor Instance carried them through
            #      migration). Without this backfill every re-detected
            #      v7-arc Instance fires app_no_producer_surface every
            #      audit pass. The scanner already has fresh observational
            #      evidence in `fresh` — just merge it in.
            if use_llm and mf in existing_app_hits:
                identity = data.get("identity") or {}
                success = data.get("success_criteria") or {}
                needs_identity_backfill = (
                    _is_empty(identity.get("purpose"))
                    or _is_empty(identity.get("scope_includes"))
                    or _is_empty(identity.get("user"))
                    or _is_empty(success.get("observable_outcomes"))
                )
                # Mirror the audit's definition of "has a surface" rather
                # than "all six fields empty". The audit (v23.1, tightened
                # 2026-06-09 the same day as PR #2494) rejects vacuous
                # scheduled_actions[] entries — mechanism=unknown with
                # install.file/plist_label/command all None. The original
                # all-empty trigger missed those manifests entirely: the
                # field was non-empty so backfill skipped, then the audit
                # fired anyway. ~120 of the 138 alerts in the 2026-06-09
                # storm sat in this gap.
                needs_producer_backfill = not _has_real_producer_surface(data)
                # Discoverability backfill — fired INDEPENDENTLY of the
                # producer-surface trigger above. PR #2519 made a script
                # realized_file count as a producer surface, so for a
                # script-bearing v7-arc Instance _has_real_producer_surface
                # returns True and needs_producer_backfill is False. With
                # identity already populated, that skipped this whole block —
                # and the interface_contract.cli synthesis + example_triggers
                # merge that close app_discoverability_no_cli /
                # _no_example_triggers live INSIDE it. The script that closes
                # app_no_producer_surface thus suppressed the very backfill
                # written to fix these same Instances (2026-06-09 residual:
                # 33 no_cli + 27 no_example_triggers still firing). Fire on
                # the discoverability fields directly: a script is present and
                # either field is still empty. The cli/example_triggers
                # only-if-empty guards inside the block keep operator edits
                # sacred regardless of which trigger entered it.
                ic_now = data.get("interface_contract") or {}
                cli_empty = _is_empty(
                    ic_now.get("cli") if isinstance(ic_now, dict) else None
                )
                triggers_empty = _is_empty(data.get("example_triggers"))
                needs_discoverability_backfill = _has_script_realized_file(data) and (
                    cli_empty or triggers_empty
                )
                if (
                    needs_identity_backfill
                    or needs_producer_backfill
                    or needs_discoverability_backfill
                ):
                    try:
                        app = existing_app_hits[mf]
                        _slog(f"[scanner]   Backfill: regenerating for {mf.stem}")
                        fresh = generate_manifest_for_app(
                            app, inventory, model, openclaw_cmd,
                            api_key=api_key, repair=repair, repair_log=_scan_log,
                        )
                        # Merge only into empty fields — never clobber existing data.
                        # We intentionally do NOT backfill non-dict scalar fields
                        # (description, build_spec, etc.) from `fresh` here; Pass B
                        # handles description, and the rest are not part of the
                        # user-visible "Identity / Scope / Success Criteria" gap
                        # this backfill targets.
                        fields_filled = 0
                        for fld in ("identity", "success_criteria", "constraints"):
                            existing_block = dict(data.get(fld) or {})
                            fresh_block = fresh.get(fld) or {}
                            for k, v in fresh_block.items():
                                if _is_empty(existing_block.get(k)) and not _is_empty(v):
                                    existing_block[k] = v
                                    fields_filled += 1
                            data[fld] = existing_block
                        # Producer-surface fields are atomic units (a
                        # heartbeat_evidence dict isn't half a heartbeat;
                        # scheduled_actions[] isn't half an action). Replace
                        # whole field only when the existing one is empty —
                        # operator edits never get clobbered.
                        #
                        # scheduled_actions is a special case: pre-v23.1 the
                        # scanner minted stub entries with mechanism=unknown
                        # and entirely-None install blocks (audit-vacuous,
                        # never operator-authored — the operator would never
                        # produce a mechanism=unknown stub). We treat those
                        # as replaceable so the audit and backfill agree.
                        #
                        # example_triggers rides the same only-if-empty merge
                        # (it's a discoverability field, not a producer
                        # surface, but the merge semantics are identical):
                        # the fresh LLM draft carries example_triggers[] and
                        # this closes app_discoverability_no_example_triggers
                        # on script-bearing Instances. Operator-authored
                        # triggers (non-empty) are preserved by the same
                        # _is_empty(existing_val) guard.
                        for fld in (
                            "scheduled_actions", "heartbeat_evidence",
                            "cron_evidence", "crons", "event_triggers",
                            "interface_contract", "example_triggers",
                        ):
                            fresh_val = fresh.get(fld)
                            if _is_empty(fresh_val):
                                continue
                            existing_val = data.get(fld)
                            replaceable = _is_empty(existing_val) or (
                                fld == "scheduled_actions"
                                and _scheduled_actions_all_vacuous(existing_val)
                            )
                            if replaceable:
                                data[fld] = fresh_val
                                fields_filled += 1
                        # Final fallback: if interface_contract.cli is
                        # STILL empty but realized_files[] contains a
                        # script, synthesize CLI entries from the
                        # scripts. v7-arc Instance migration kept
                        # realized_files but dropped interface_contract;
                        # without this, every re-detected
                        # script-realized Instance fires
                        # app_no_producer_surface (~10–15 of the
                        # 66 firing alerts on the 2026-06-09 pod
                        # survey — vineyard-ops, document-generator,
                        # etc.). The `_is_empty` guard on
                        # ``interface_contract.cli`` ensures operator
                        # edits never get clobbered.
                        ic = data.get("interface_contract") or {}
                        if _is_empty(ic.get("cli") if isinstance(ic, dict) else None):
                            inferred_cli = _synthesize_cli_from_scripts(data)
                            if inferred_cli:
                                if not isinstance(ic, dict):
                                    ic = {}
                                ic["cli"] = inferred_cli
                                data["interface_contract"] = ic
                                fields_filled += 1
                        if fields_filled:
                            identity_backfill_count += 1
                            patched = True
                            _slog(f"[scanner]   Backfilled {fields_filled} field(s) on {mf.stem}")
                    except Exception as e:
                        # LLM failure or merge failure — leave manifest alone, log it.
                        # Pass B may still patch description if purpose was already set.
                        import traceback as _tb_bf
                        _slog(f"[scanner]   Backfill FAILED for {mf.stem}: {e}")
                        _slog(_tb_bf.format_exc())

            # ── Pass B: derive description from identity.purpose ────────────
            if _is_empty(data.get("description")):
                purpose = (data.get("identity") or {}).get("purpose", "")
                derived = _derive_description_from_purpose(purpose, data.get("name", ""))
                if derived:
                    data["description"] = derived
                    patched = True

            # ── Pass C: seed permissions: block from inferred entries ───────
            # Only fires when the block is entirely absent. Once written,
            # the permissions: block is operator-owned and never clobbered
            # (the spec deliberately keeps `permissions` OUT of
            # AUTO_MANAGED_FIELDS — see docs/spec-app-derived-permissions-2026-05-24.md).
            # This replaces the standalone app_permission_bootstrapper
            # generator (retired 2026-05-25); scan-time backfill closes
            # the gap directly instead of queueing one Investigation
            # proposal per app.
            if not data.get("permissions"):
                inferred_block = _infer_permissions_block(data)
                if inferred_block:
                    data["permissions"] = inferred_block
                    patched = True

            # ── Pass D: discoverability backfill (usage.model + hint_words) ─
            # Closes the gaps muted by audit_poller's discoverability
            # trail-only gate. Conservative ("only-if-empty") — never
            # clobbers operator-set values. See _apply_discoverability_backfill
            # above for the exact rules.
            #
            # Pure-Python, no LLM. The structural verifier remains the
            # source of truth for what "discoverable" means; this pass is
            # best-effort repair so the next audit run has nothing to
            # report on these fields for the trivially-fixable cases.
            if _apply_discoverability_backfill(data):
                patched = True

            if patched:
                data["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                _atomic_write(mf, data, mode=0o644)
                patch_count += 1
        except Exception:
            pass
    if patch_count:
        if identity_backfill_count:
            _slog(f"[scanner] Patched {patch_count} existing manifest(s) — "
                  f"{identity_backfill_count} got LLM-backed identity/success backfill")
        else:
            _slog(f"[scanner] Patched {patch_count} existing manifest(s)")

    # Return all manifests (new + existing)
    result: list[dict] = []
    for mf in caps_dir.glob("*.json"):
        if mf.name.startswith(".") or "_history" in str(mf):
            continue
        try:
            result.append(json.loads(mf.read_text()))
        except Exception:
            pass
    _slog(f"[scanner] Done — returning {len(result)} manifest(s) total")

    # Phase 4.5 repair (pod-wide leg): ensure every active manifest has
    # a section in the bot's INSTALLED_APPS.md so the LLM can route to
    # it at session start. Idempotent — re-running with the same set of
    # manifests produces no diff. Best-effort: a failure here doesn't
    # change the scan outcome.
    if repair:
        try:
            from .manifest import list_manifests as _list_m
            from .scanner_repair import repair_installed_apps_md
            current = _list_m(shared_dir, bot_id)
            apps_repair = repair_installed_apps_md(
                current, inventory.workspace, bot_id=bot_id,
            )
            for line in apps_repair.applied:
                _slog(f"[scanner] INSTALLED_APPS.md APPLIED {line}")
            for line in apps_repair.skipped:
                _slog(f"[scanner] INSTALLED_APPS.md SKIP    {line}")
            for line in apps_repair.errors:
                _slog(f"[scanner] INSTALLED_APPS.md ERROR   {line}")
        except Exception as e:
            _slog(
                f"[scanner] INSTALLED_APPS.md repair skipped (non-fatal): {e}"
            )

        # Delivery-contract stamp (pod-wide leg): an app adopted via scanner
        # extraction never went through forge approval, so its user-facing
        # scheduled_actions[] can sit with outputs:[] + delivery_contract:null
        # — invisible to the proactive-delivery monitor (the atlas-digest /
        # ledger U1 class; see _stamp_scheduled_delivery_contracts). Re-assert
        # the Spec's delivery intent here, the same stamp forge applies on
        # approval. Spec-gated + live-channel-gated, so internal actions are
        # left untouched (no false app_delivery_missed). Idempotent.
        try:
            from .forge_engine import stamp_bot_user_facing_deliveries
            stamped_map = stamp_bot_user_facing_deliveries(bot_id, shared_dir)
            for app_id, action_ids in stamped_map.items():
                _slog(
                    f"[scanner] delivery-contract STAMPED {app_id}: "
                    f"{', '.join(action_ids)} (now monitored)"
                )
        except Exception as e:
            _slog(
                f"[scanner] delivery-contract stamp skipped (non-fatal): {e}"
            )
    else:
        # Even with --no-repair, keep the legacy regenerate path so the
        # bot's LLM still sees newly-scanned apps. This matches the
        # pre-repair behaviour exactly.
        try:
            from . import app_registry as _ar
            out_path = _ar.regenerate_installed_apps_md(bot_id, shared_dir)
            if out_path is not None:
                _slog(f"[scanner] INSTALLED_APPS.md regenerated at {out_path}")
        except Exception as e:
            _slog(
                f"[scanner] INSTALLED_APPS.md regenerate skipped "
                f"(non-fatal): {e}"
            )

    # Trigger app_permission_review immediately so operator-initiated scans
    # surface manifest-hygiene findings in the proposals queue without
    # waiting for the next scheduled tick (B.2 of
    # docs/spec-app-derived-permissions-2026-05-24.md, sub-spec
    # docs/spec-app-permission-review-2026-05-26.md).
    #
    # Reuses the standard ingest pipeline (dedup, fingerprint, charter
    # invariants, rejection cooldown) via generator_runner.run_one_generator.
    # Best-effort — a generator hiccup must not break the scan return.
    try:
        # Lazy import — when it fails, the standalone weekly cadence
        # picks up the findings instead.
        from generator_runner import run_one_generator
        # Resolve the network config the runner expects (mirror of what
        # better_engine_refresh passes). For the scan path, we only need
        # the bots map + the bot's role. NOTE: ``load_network`` is already
        # imported at module level — re-importing it here as a function-local
        # made it local to the whole enclosing scope, which left the nested
        # ``_gen_and_save`` closure unable to read it ("cannot access free
        # variable 'load_network'"), silently failing per-bot tier stamping.
        net = load_network()
        run_one_generator(
            shared_dir, net, "app_permission_review", bot_id=bot_id,
        )
        _slog(f"[scanner] app_permission_review triggered for {bot_id}")
    except Exception as exc:
        _slog(
            f"[scanner] app_permission_review trigger skipped "
            f"(non-fatal): {type(exc).__name__}: {exc}"
        )

    # Write a terminal "done" status so the admin server polling loop exits promptly
    # instead of running for its full 6-minute timeout.
    done_extra = {"status": "done", "manifests_total_returned": len(result)}
    if llm_degraded_reason:
        # The scan completed structurally because no LLM provider key resolved.
        # A clear, non-error signal — distinct from error_kind="missing_api_key".
        done_extra["llm_degraded"] = True
        done_extra["llm_degraded_reason"] = llm_degraded_reason
    _write_status(status_path, 4, found=total_found, created=created, total=len(new_apps),
                  scan_log=_scan_log, extra=done_extra)

    return result


# ── Internal helpers ──────────────────────────────────────────────────────────

def _normalize_name(name: str) -> str:
    """Lowercase, strip punctuation/whitespace for fuzzy name comparison."""
    import re as _re
    return _re.sub(r"[^a-z0-9]", "", name.lower())


def _canon_name(s: str) -> str:
    """Canonicalize an app id, filename stem, or display name for dedup.

    Lowercase; collapse runs of whitespace/underscore/hyphen into single
    '-'; strip the conventional ``app-`` prefix. Designed so detected ids
    like ``biometric-integration`` and existing filename stems like
    ``app-biometric-integration`` (and display names like ``Biometric
    Integration``) all canonicalize to the same key. Stricter than
    :func:`_normalize_name`, which strips ALL separators and is used for
    fuzzy similarity scoring inside :func:`_dedup_manifests`.
    """
    import re as _re
    s = (s or "").strip().lower()
    s = _re.sub(r"[\s_\-]+", "-", s)
    # Strip the conventional ``app-`` prefix BEFORE the final dash trim so
    # the degenerate input ``"app-"`` collapses to an empty string (rather
    # than to the misleading bare ``"app"``).
    if s.startswith("app-"):
        s = s[4:]
    return s.strip("-")


def _build_existing_manifest_index(
    caps_dir: Path, shared_dir: Path | None = None
) -> list[dict]:
    """Load every manifest under ``caps_dir`` into a dedup-comparison index.

    Each entry: ``{path, id, stem, id_canon, stem_canon, name_canon,
    evidence_set, created_at}``. Used by both the in-scan dedup step
    (skip apps that already have a manifest) and the ``--dedup-existing``
    cleanup mode (find duplicate pairs left over from previous broken
    scans that compared stems only).

    **v7-arc hydration.** A thin v7-arc Instance carries ``id``/``name`` of
    ``null`` at top level — the human-facing name lives in the bound gallery
    Spec and only materializes after ``hydrate_v7_arc_instance``. Reading the
    raw JSON therefore yields ``id_canon=""``/``name_canon=""``, leaving only
    ``stem_canon`` (the instance-id filename, which resembles no detected app)
    for the matcher. When a freshly detected app's id/name drifts from that
    stem, ``_match_detected_to_existing`` finds nothing and Phase 4 mints a
    DUPLICATE manifest for an app that already exists (atlas Task Manager,
    2026-06-16). To prevent that, hydrate each Instance via
    ``_comparable_view`` (the same path ``_dedup_manifests`` already uses)
    before computing the canonical keys, so the Spec-derived ``name`` and the
    Instance's ``realized_files`` participate in matching. ``shared_dir`` is
    required for hydration; when ``None`` (older callers / tests) the raw
    pre-fix view is used and v7-arc Instances match on stem/evidence only.

    The on-disk ``data`` dict stays RAW (un-hydrated) so the cleanup mode's
    winner-selection + ``_fill_missing_fields`` + atomic rewrite never leak
    Spec-overlaid fields back into the Instance file.

    Skips dotfiles and ``_history/`` archives. Malformed JSON is silently
    omitted — those entries never participated in match scoring anyway.
    """
    index: list[dict] = []
    for p in sorted(caps_dir.glob("*.json")):
        if p.name.startswith(".") or "_history" in str(p):
            continue
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        # Hydrated view for comparison only; falls back to ``data`` for
        # legacy manifests, no-shared_dir callers, and hydration failures.
        view = _comparable_view(data, shared_dir)
        name = (view.get("name") or view.get("display_name") or "").strip()
        index.append({
            "path": p,
            "id": (view.get("id") or "").strip(),
            "stem": p.stem,
            "name": name,
            "id_canon": _canon_name(view.get("id") or ""),
            "stem_canon": _canon_name(p.stem),
            "name_canon": _canon_name(name),
            "evidence_set": frozenset(_ev_path_keys(view) or _ev_path_keys(data)),
            "created_at": data.get("created_at", ""),
            "data": data,
        })
    return index


def _match_detected_to_existing(
    detected: "DetectedApplication",
    existing_index: list[dict],
    claimed: set[Path],
) -> dict | None:
    """Find the existing manifest that best matches a freshly detected app.

    Precedence (first hit wins; subsequent passes are only consulted when
    no stronger match is found):

      a) exact id match (``detected.id == existing.id``)
      b) exact stem match (``detected.id == existing.stem``) — the
         pre-fix behaviour, preserved for the small fraction of names
         that did match
      c) canonical-key overlap — ``_canon_name`` of the detected id or
         name matches the canonicalized id, stem, or name of an existing
         manifest. This is the fix for the ``app-`` prefix drift +
         case/hyphen drift that caused 13/18 apps on the reference
         pod to be misclassified as new on every scan.
      d) evidence-file overlap >= 50% — catches LLM re-clustering of the
         same files under a different name, including v7-arc Instance
         filenames (``i-34cfcab1.json``) that bear no resemblance to
         the LLM's generated id.

    ``claimed`` is updated by the caller after each match so two
    detected apps cannot both claim the same existing manifest.
    """
    det_id = (detected.id or "").strip()
    det_id_canon = _canon_name(det_id)
    det_name_canon = _canon_name(detected.name or "")
    det_ev = frozenset(
        _ev_path_keys({"evidence_files": list(detected.evidence_files or [])})
    )

    # Passes (a)/(b)/(c): identity-based matches.
    for entry in existing_index:
        if entry["path"] in claimed:
            continue
        # (a) exact id, (b) exact stem
        if det_id and (det_id == entry["id"] or det_id == entry["stem"]):
            return entry
        # (c) canonical-key overlap
        canons_det = {c for c in (det_id_canon, det_name_canon) if c}
        canons_existing = {
            c for c in (entry["id_canon"], entry["stem_canon"], entry["name_canon"])
            if c
        }
        if canons_det & canons_existing:
            return entry

    # Pass (d): evidence-overlap match — pick the strongest if multiple
    # exist (rare but possible after prior scans accumulated duplicates).
    #
    # This pass intentionally matches across DIFFERENT names (the LLM re-clusters
    # the same files under a drifted name, or a v7-arc instance filename bears no
    # resemblance to the detected id). But file overlap alone must not bind a
    # freshly-detected, established app to an existing manifest that is a
    # CLEARLY DIFFERENT app — otherwise a re-scan re-absorbs the split we are
    # trying to recover (Atlas: a re-detected "Daily Digest" would fold straight
    # back into "Article Capture" on shared substrate). _are_distinct_apps is
    # conservative: it only vetoes when both names are non-empty + dissimilar
    # AND both sides are characterized (have an objective/purpose/description),
    # so thin re-mints and un-hydrated v7-arc instances still match here.
    det_raw = {"description": detected.description or ""}
    best: dict | None = None
    best_overlap = 0.0
    for entry in existing_index:
        if entry["path"] in claimed:
            continue
        ev_existing = entry["evidence_set"]
        if not det_ev or not ev_existing:
            continue
        if _are_distinct_apps(
            detected.name or "", det_raw, entry.get("name") or "", entry["data"],
        ):
            continue
        overlap = len(det_ev & ev_existing) / min(len(det_ev), len(ev_existing))
        if overlap >= 0.5 and overlap > best_overlap:
            best = entry
            best_overlap = overlap
    return best


# A freshly-generated manifest must share at least this fraction of its file
# footprint with an existing Instance before re-discovery REUSES that
# Instance's spec_id. Matches the file-overlap floor used by
# ``_match_detected_to_existing`` pass (d) and ``_dedup_manifests`` cond 1 —
# deliberately conservative: a missed reuse is a harmless transient duplicate
# the next scan/dedup collapses, but a WRONG reuse silently fuses two distinct
# apps onto one spec_id, which is unrecoverable. Keep this in step with those.
_REDISCOVERY_OVERLAP_MIN = 0.5


def _rediscovery_match(
    manifest: dict,
    existing_index: list[dict],
    *,
    claimed: set[Path],
    min_overlap: float = _REDISCOVERY_OVERLAP_MIN,
) -> dict | None:
    """Return the installed v7-arc Instance this manifest RE-DISCOVERS, or None.

    The orphan engine this guards: when the identity matcher
    (``_match_detected_to_existing``) misses an already-installed app — its
    LLM-generated id/name drifted, or its evidence set shifted under the
    pass-(d) floor — the app lands in ``new_apps`` and the mint path stamps it
    with a BRAND-NEW ``p-`` spec_id. The prior spec_id is then stranded and
    every workspace file still carrying it in its ``_evolve`` marker becomes an
    orphan (spec §4.B "idempotent re-discovery"). This is a second, spec-id-
    focused safety net run at the mint site over the freshly-GENERATED manifest
    (whose ``realized_files``/``files`` footprint is richer than the raw
    ``DetectedApplication.evidence_files`` pass (d) compared), so it can
    recognize an installed app the identity matcher let through.

    A match is returned only when ALL hold (conservative by construction —
    item 4 of the chip: a false reuse is worse than a missed reuse):

      - the existing entry is a v7-arc Instance carrying a non-empty
        ``provenance.spec_id`` (there is a live spec_id to reuse),
      - its file is not already ``claimed`` by another detected app this scan
        (so two apps never bind the same Instance),
      - file-key overlap (evidence + realized + files, via ``_ev_path_keys``)
        is ``>= min_overlap`` of the smaller set, and
      - ``_are_distinct_apps`` does NOT veto — i.e. they are not two
        established, clearly-different apps that merely share files (the Atlas
        shared-substrate failure class). Mirrors pass (d)'s veto exactly:
        hydrated ``name`` + raw ``data`` for identity.

    The strongest overlap wins when several qualify. ``None`` means "mint a
    fresh id as before" (a genuinely new app, or no confident match).
    """
    man_keys = _ev_path_keys(manifest)
    if not man_keys:
        return None
    man_name = (manifest.get("name") or manifest.get("display_name") or "").strip()

    best: dict | None = None
    best_overlap = 0.0
    for entry in existing_index:
        if entry["path"] in claimed:
            continue
        data = entry.get("data") or {}
        prov = data.get("provenance")
        spec_id = prov.get("spec_id") if isinstance(prov, dict) else None
        if not (isinstance(spec_id, str) and spec_id):
            continue  # no live spec_id to reuse — not a re-discovery target
        ev_existing = entry["evidence_set"]
        if not ev_existing:
            continue
        overlap = len(man_keys & ev_existing) / min(len(man_keys), len(ev_existing))
        if overlap < min_overlap or overlap <= best_overlap:
            continue
        if _are_distinct_apps(
            man_name, manifest, entry.get("name") or "", data,
        ):
            continue  # established + clearly different apps that share files
        best = entry
        best_overlap = overlap
    return best


def _name_similarity(a: str, b: str) -> float:
    """Return 0-1 similarity between two application names.

    Empty or missing names never match — otherwise two v7-arc Instances
    whose Spec hydration failed would compare as identical (both ""),
    triggering bogus merges.
    """
    from difflib import SequenceMatcher
    na, nb = _normalize_name(a), _normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    # Check if one is a substring of the other (e.g. "Email Assistant" vs "Email Management")
    if na in nb or nb in na:
        return 0.85
    return SequenceMatcher(None, na, nb).ratio()


# A pair of manifests whose DISTINCTIVE names (bot-prefix stripped) are at least
# this similar are treated as plausibly the same app — below it, and with both
# sides established, they are "distinct apps" and a file-/name-overlap merge or
# evidence match is vetoed. The threshold value matches cond 3's name floor, but
# the veto applies it to _distinctive_name_similarity (prefix-stripped, symmetric)
# — strictly MORE conservative than cond 3's raw _name_similarity, so it only
# ever blocks a merge cond 3 would otherwise have allowed, never the reverse.
_DISTINCT_NAME_SIM_MAX = 0.55


def _identity_text(raw: dict) -> str:
    """First non-empty identity string for a manifest: objective, then
    identity.purpose, then description. Empty string when the manifest carries
    none of them (a thin / uncharacterized stub).
    """
    obj = raw.get("objective")
    if isinstance(obj, str) and obj.strip():
        return obj.strip()
    purpose = (raw.get("identity") or {}).get("purpose")
    if isinstance(purpose, str) and purpose.strip():
        return purpose.strip()
    desc = raw.get("description")
    if isinstance(desc, str) and desc.strip():
        return desc.strip()
    return ""


def _distinctive_name_similarity(name_a: str, name_b: str) -> float:
    """Symmetric 0-1 name similarity AFTER stripping the shared leading tokens
    (the bot-name prefix). Apps on one bot routinely share a prefix — "Atlas
    Daily Digest" / "Atlas Article Capture" — which inflates a raw similarity
    score and hides that the *distinctive* parts ("Daily Digest" vs "Article
    Capture") are unrelated. We compare only the distinctive remainder.

    ``_name_similarity`` (SequenceMatcher) is also order-asymmetric, so we take
    the MAX of both directions — the most generous reading — which makes
    "these are different apps" the harder verdict (conservative for the veto).

    Prefix/superset case (one name's distinctive remainder is empty, e.g.
    "Atlas Daily Digest" vs "Atlas Daily Digest Job"): score by how much of the
    LONGER name the shared prefix covers — ``shared_prefix_tokens / max_tokens``.
    "Daily Digest" vs "Daily Digest Job" → 3/4 = 0.75 (same-ish, won't veto);
    "Research" vs "Research Budget Guard" → 2/4 = 0.50 (the extra distinctive
    tokens make it plausibly a DIFFERENT app, so the veto can fire). Hardcoding
    1.0 here would let two genuinely-different prefix-shaped apps re-merge — the
    exact failure class this guards against.
    """
    import re as _re

    def _toks(n: str) -> list[str]:
        return [t for t in _re.split(r"[^a-z0-9]+", (n or "").lower()) if t]

    ta, tb = _toks(name_a), _toks(name_b)
    if not ta or not tb:
        return 0.0
    i = 0
    while i < len(ta) and i < len(tb) and ta[i] == tb[i]:
        i += 1
    ra, rb = ta[i:], tb[i:]
    if not ra or not rb:
        # One name is a token-prefix of the other. Reward a long shared prefix
        # (likely the same app + a qualifier) and discount when the longer name
        # piles on extra distinctive tokens (likely a different app).
        return i / max(len(ta), len(tb))
    rem_a, rem_b = " ".join(ra), " ".join(rb)
    return max(_name_similarity(rem_a, rem_b), _name_similarity(rem_b, rem_a))


def _are_distinct_apps(name_a: str, raw_a: dict, name_b: str, raw_b: dict) -> bool:
    """True when two manifests are established, clearly-DIFFERENT applications —
    used to veto a merge/match that is justified by file overlap ALONE.

    Two real apps routinely share files: a library directory, a common data
    store, a shared archive. Atlas ships four apps (daily-digest, article-capture,
    on-demand-research, weekly-recap) that share ``scripts/atlas_lib/`` +
    ``archive/index.json`` + ``atlas/*`` *by design* (see
    docs/atlas-app-manifests/README.md). The dedup file-overlap heuristic read
    that shared substrate as "same app" and collapsed all four into one
    Frankenstein manifest (incident-atlas-app-conflation-2026-06-22).

    Conservative by construction (keep-bias toward dedup — a missed merge is a
    harmless transient duplicate; a wrong merge destroys an app's identity):
    returns True ONLY when BOTH sides have a non-empty name, the names'
    DISTINCTIVE parts are clearly dissimilar (``< _DISTINCT_NAME_SIM_MAX`` after
    shared-prefix stripping), AND BOTH carry a non-empty objective/purpose/
    description (i.e. both are characterized apps, not thin re-mint stubs or
    un-hydrated v7-arc instances). Anything thinner falls through to False and
    is allowed to merge.
    """
    na = (name_a or "").strip()
    nb = (name_b or "").strip()
    if not na or not nb:
        return False
    if _distinctive_name_similarity(na, nb) >= _DISTINCT_NAME_SIM_MAX:
        return False
    return bool(_identity_text(raw_a)) and bool(_identity_text(raw_b))


def _merge_two_manifests(winner: dict, loser: dict) -> dict:
    """
    Merge loser into winner. Winner keeps its id/name. Combined fields:
      - evidence_files / realized_files / files: union (by path key)
      - confidence: max
      - description/identity: keep whichever is longer/richer
      - created_at: earliest
      - updated_at: now

    Both legacy and v7-arc shapes are handled. For v7-arc Instances the
    Spec-derived fields (name, description, identity) are not present on
    the raw Instance and are not written back — those live in the Spec
    and the dedup pass must not pollute the Instance with them.
    """
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    merged = dict(winner)
    is_v7_arc = winner.get("manifest_shape") == "v7-arc"

    # Identity-stability (Bite 2; §9.2 guarantee 2): the survivor inherits a
    # `defined` vouch if EITHER input carried one. A merge must never silently
    # DEMOTE an operator-vouched app. This matters on the cond-0 same_spec path
    # in _dedup_manifests, where a defined Instance can legitimately be the loser
    # (two Instances of one Spec → one app); on every other dedup path the loser
    # is never defined (the _dedup_manifests veto forbids it), so this is a no-op
    # there. ``merged = dict(winner)`` already carries the winner's id + name
    # unchanged — the anchored handle is structurally preserved for any winner.
    if _is_defined(winner) or _is_defined(loser):
        merged["definition_status"] = "defined"  # cf. manifest.MANIFEST_DEFINITION_DEFINED

    # Union evidence_files (legacy + sometimes preserved on v7-arc)
    ev_w = set(winner.get("evidence_files", []) or [])
    ev_l = set(loser.get("evidence_files", []) or [])
    if ev_w or ev_l:
        merged["evidence_files"] = sorted(ev_w | ev_l)

    # Union realized_files (v7-arc) by path
    rf_w = list(winner.get("realized_files", []) or [])
    rf_l = list(loser.get("realized_files", []) or [])
    if rf_w or rf_l:
        by_path: dict[str, dict] = {}
        for rf in rf_w + rf_l:
            if isinstance(rf, dict) and rf.get("path"):
                by_path.setdefault(rf["path"], rf)
        merged["realized_files"] = list(by_path.values())

    # Union files list (legacy v5+ list-of-dicts) by path. Loser-only
    # entries carry loser.pkg_id in ``owned_by``; rewrite those to
    # winner.pkg_id since the loser manifest is about to be unlinked
    # (dedup caller deletes loser at line ~3222). Without this
    # rewrite, every dedup pass leaked stale ``owned_by`` tags into
    # winner — the smoking gun for the 78 stale entries surfaced
    # pod-wide on 2026-06-08 (a personal-bot's Biometric Integration
    # and several team-bot apps).
    f_w = list(winner.get("files", []) or [])
    f_l = list(loser.get("files", []) or [])
    if f_w or f_l:
        winner_pkg = (winner.get("pkg_id") or "").strip()
        loser_pkg = (loser.get("pkg_id") or "").strip()
        by_path = {}
        for f in f_w + f_l:
            if isinstance(f, dict) and f.get("path"):
                by_path.setdefault(f["path"], f)
            elif isinstance(f, str):
                by_path.setdefault(f, {"path": f})
        if winner_pkg and loser_pkg and loser_pkg != winner_pkg:
            for entry in by_path.values():
                if not isinstance(entry, dict):
                    continue
                if (entry.get("owned_by") or "").strip() == loser_pkg:
                    entry["owned_by"] = winner_pkg
        merged["files"] = list(by_path.values())

    # Higher confidence
    conf_w = winner.get("confidence") or 0
    conf_l = loser.get("confidence") or 0
    if conf_w or conf_l:
        merged["confidence"] = max(conf_w, conf_l) or conf_w

    # Description/identity merge — only meaningful for legacy manifests;
    # v7-arc Instances don't carry these fields (they live on the Spec).
    #
    # FILL-ONLY, never "longest wins". The survivor's description /
    # identity.purpose is part of its STABLE identity: adopt the loser's
    # only when the survivor's field is empty. The old rule ("take whichever
    # string is longer") made a stable app-id re-state a different purpose on
    # every scan — whichever co-merged sibling happened to have the wordiest
    # text won — which is how Atlas's article-capture app ended up describing
    # itself as the daily-news-digest job (incident-atlas-app-conflation-2026-06-22).
    # `objective` was never length-merged, which is why each manifest kept its
    # real objective while its description/purpose flipped — the diagnostic
    # footprint of this bug. Keep identity authoritative on the winner.
    if not is_v7_arc:
        desc_w = winner.get("description", "") or ""
        desc_l = loser.get("description", "") or ""
        # Anchored-identity protection (Bite 2; §9.2 guarantee 2): the canonical
        # description is half of a defined app's anchored identity (name +
        # this line). When the winner is DEFINED and its description is
        # operator-authored (field_origins marks it on promote), the scanner
        # must NOT write it — not even fill-empty: a vouched app may carry an
        # intentionally terse or blank identity line, and inventing one from a
        # merged sibling is exactly the re-identification §9.2 forbids. A
        # non-empty winner description is already preserved by the fill-only
        # rule below; this guard additionally covers the authored-but-empty
        # case. Discovered apps keep the existing fill-only behavior.
        from .reconciliation import is_field_authored as _is_field_authored
        desc_locked = _is_defined(winner) and _is_field_authored(winner, "description")
        if desc_l and not desc_w and not desc_locked:
            merged["description"] = desc_l

        purpose_w = (winner.get("identity") or {}).get("purpose", "")
        purpose_l = (loser.get("identity") or {}).get("purpose", "")
        if purpose_l and not purpose_w:
            merged["identity"] = dict(winner.get("identity") or {})
            merged["identity"]["purpose"] = purpose_l

    # Earliest created_at
    ca_w = winner.get("created_at", now)
    ca_l = loser.get("created_at", now)
    merged["created_at"] = min(ca_w, ca_l)
    merged["updated_at"] = now
    merged["merged_from"] = merged.get("merged_from", []) + [
        loser.get("id") or loser.get("instance_id") or "?"
    ]

    return merged


def _ev_path_keys(manifest: dict) -> set[str]:
    """
    Collect comparable path keys from every field a manifest may use to
    declare its file set: evidence_files (legacy + v7-arc passthrough),
    realized_files (v7-arc), and files (v5+ legacy).

    Handles plain paths ("foo/bar.py") and tagged strings ("directory: foo/").
    """
    import re as _re
    keys: set[str] = set()

    def _add(raw: str) -> None:
        s = _re.sub(r"^[a-z_]+:\s*", "", str(raw).strip())
        s = s.strip("/").lower()
        if s:
            keys.add(s)

    for e in manifest.get("evidence_files", []) or []:
        _add(e)
    for rf in manifest.get("realized_files", []) or []:
        if isinstance(rf, dict) and rf.get("path"):
            _add(rf["path"])
    for f in manifest.get("files", []) or []:
        if isinstance(f, dict) and f.get("path"):
            _add(f["path"])
        elif isinstance(f, str):
            _add(f)
    return keys


def _comparable_view(raw: dict, shared_dir: Path | None) -> dict:
    """
    Return a dict suitable for name + file comparison in dedup.

    For v7-arc Instances, hydrate via hydrate_v7_arc_instance() so the
    Spec-derived name + a files list (built from realized_files) become
    available. For legacy manifests, return the raw dict unchanged.

    If hydration fails (Spec missing on disk, etc.) the raw Instance is
    returned — it still carries realized_files which _ev_path_keys can use,
    so file-overlap dedup still has a chance even if name comparison won't.
    """
    if raw.get("manifest_shape") != "v7-arc":
        return raw
    if shared_dir is None:
        return raw
    try:
        from .manifest import hydrate_v7_arc_instance
        return hydrate_v7_arc_instance(raw, shared_dir)
    except Exception:
        return raw


def _dedup_manifests(caps_dir: Path, shared_dir: Path | None = None) -> int:
    """
    Load all manifests in caps_dir, identify duplicates, merge them, and
    delete the losers. Returns number of merges performed.

    Merge conditions (any one is sufficient):
      0. v7-arc Instances with the same provenance.spec_id — guaranteed dupe
      1. path-key overlap >= 50%        (same underlying files → same app)
      2. name similarity >= 0.85        (nearly identical names)
      3. name similarity >= 0.55 AND path-key overlap >= 20%

    Path keys come from evidence_files, realized_files, and files combined,
    so v7-arc Instances (which use realized_files, not evidence_files)
    participate correctly.

    Hydration: v7-arc Instances are hydrated from their bound Spec before
    name comparison so the Spec's name field is visible to the heuristic.
    Without this, v7-arc Instances were skipped entirely because the raw
    Instance file carries no top-level `name`.
    """
    # Each entry: (path, raw_dict, comparable_view)
    manifests: list[tuple[Path, dict, dict]] = []
    for mf in sorted(caps_dir.glob("*.json")):
        if mf.name.startswith(".") or "_history" in str(mf):
            continue
        try:
            data = json.loads(mf.read_text())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        view = _comparable_view(data, shared_dir)
        # Accept v7-arc Instances even if hydration failed and yielded no
        # name — file-overlap can still catch them.
        has_name = bool(view.get("name"))
        has_paths = bool(_ev_path_keys(view) or _ev_path_keys(data))
        if not has_name and not has_paths:
            continue
        manifests.append((mf, data, view))

    if len(manifests) < 2:
        return 0

    # ── Identity-stability ordering (Bite 2; §9.2 guarantee 2) ───────────────
    # Stable-partition so DEFINED (operator-vouched) manifests sort first. The
    # pairwise loop below always treats the lower-indexed manifest as the merge
    # WINNER (it keeps its id/name and absorbs the other), so defined-first makes
    # a defined app the winner in every defined×discovered pair — a surviving
    # merge then only ABSORBS a discovered duplicate INTO the vouched app, never
    # deletes or re-identifies it. ``list.sort`` is stable, so discovered↔
    # discovered relative order — and thus their existing winner selection — is
    # unchanged (no regression to the Atlas-conflation dedup fix). The both-
    # defined and (defense-in-depth) loser-defined cases are vetoed in the loop.
    manifests.sort(key=lambda t: 0 if _is_defined(t[1]) else 1)

    merged_count = 0
    deleted_paths: set[Path] = set()

    for i, (path_a, raw_a, view_a) in enumerate(manifests):
        if path_a in deleted_paths:
            continue
        for path_b, raw_b, view_b in manifests[i + 1:]:
            if path_b in deleted_paths:
                continue

            # Condition 0: v7-arc Instances bound to the same Spec are the
            # same app by definition — no heuristic needed.
            spec_a = (raw_a.get("provenance") or {}).get("spec_id")
            spec_b = (raw_b.get("provenance") or {}).get("spec_id")
            same_spec = bool(spec_a) and spec_a == spec_b

            sim = _name_similarity(view_a.get("name", ""), view_b.get("name", ""))
            ev_a = _ev_path_keys(view_a) or _ev_path_keys(raw_a)
            ev_b = _ev_path_keys(view_b) or _ev_path_keys(raw_b)

            if ev_a and ev_b:
                ev_overlap = len(ev_a & ev_b) / min(len(ev_a), len(ev_b))
            else:
                ev_overlap = 0.0

            # App-evidence backstop (defense-in-depth for #2705). When BOTH
            # manifests carry zero APPLICATION evidence — infra/system/skill
            # shells the floor would have dropped at mint — the ev_overlap test
            # above is 0 by construction even when they are the same non-app
            # (the 5 gateway-selfheal dupes share only the infra script, which
            # is not app evidence). Collapse them when their raw path sets
            # overlap at all, so the interim count doesn't balloon; the floor +
            # L3 archival remove the survivor. Restricted to the both-empty
            # case so two legitimate apps that merely share one file are never
            # over-merged.
            both_no_app_evidence = (
                not _app_evidence_files(sorted(ev_a))
                and not _app_evidence_files(sorted(ev_b))
            )

            # Two established, distinctly-named apps must not collapse just
            # because their files / prefixed names overlap — that is what
            # conflated Atlas's four apps (they share a library + data dir AND
            # an "Atlas " name prefix that inflates the raw SequenceMatcher
            # score into cond 2/3 range). The veto gates every file/name-overlap
            # trigger (cond 1/2/3); cond 0 (same Spec) stays authoritative and
            # cond 4 (no-app-evidence shells) carry no objective so it is a
            # no-op there. _are_distinct_apps compares the DISTINCTIVE name parts
            # (prefix stripped) so it is not fooled by the shared "Atlas " head.
            distinct = _are_distinct_apps(
                view_a.get("name") or raw_a.get("name") or "", raw_a,
                view_b.get("name") or raw_b.get("name") or "", raw_b,
            )

            should_merge = False
            reason = ""
            if same_spec:
                should_merge = True
                reason = f"same_spec_id={spec_a}"
            elif ev_overlap >= 0.50 and not distinct:
                should_merge = True
                reason = f"ev_overlap={ev_overlap:.2f}"
            elif sim >= 0.85 and not distinct:
                should_merge = True
                reason = f"sim={sim:.2f}"
            elif sim >= 0.55 and ev_overlap >= 0.20 and not distinct:
                should_merge = True
                reason = f"sim={sim:.2f} ev_overlap={ev_overlap:.2f}"
            elif both_no_app_evidence and (ev_a & ev_b):
                should_merge = True
                reason = f"both_no_app_evidence shared={sorted(ev_a & ev_b)[:2]}"
            elif distinct and (ev_overlap >= 0.50 or sim >= 0.55):
                # Logged, not merged: helps the operator see when shared
                # substrate / a shared name prefix (not a real duplicate) drove
                # a near-merge.
                print(
                    f"[scanner] Dedup: NOT merging distinct apps "
                    f"'{view_b.get('name') or path_b.stem}' / "
                    f"'{view_a.get('name') or path_a.stem}' despite "
                    f"ev_overlap={ev_overlap:.2f} sim={sim:.2f} "
                    f"(different names + objectives)",
                    flush=True,
                )

            # ── Identity-stability veto (Bite 2; §9.2 guarantee 2) ───────────
            # A `defined` app is the operator's vouched source of truth: the
            # scanner observes and proposes, it never merges a defined app away,
            # renames it, or re-identifies it (the Atlas-conflation failure mode
            # this whole lifecycle shields). The defined-first ordering above
            # makes a defined app the WINNER (raw_a) in any defined×discovered
            # pair, so a surviving merge here only ABSORBS a discovered duplicate
            # INTO the defined app — which keeps its id + name (winner) and its
            # anchored description (fill-only + authored-guard in
            # _merge_two_manifests), and stays `defined` (sticky there). Two
            # cases still need a veto — cond 0 (same_spec) is EXCLUDED because
            # two Instances of one Spec are the SAME app, so collapsing them is
            # identity-preserving (the survivor stays vouched via the sticky
            # rule), not a cross-app merge:
            #   • both defined  → never collapse two DISTINCT vouched apps.
            #   • loser defined → would DELETE a vouched app. Unreachable after
            #     the defined-first ordering (a defined loser implies a defined
            #     winner → caught by the both-defined arm), but kept as the HARD
            #     INVARIANT that makes the merge/delete path auditable: the
            #     `path_b.unlink()` below can never destroy a defined manifest.
            defined_a = _is_defined(raw_a)
            defined_b = _is_defined(raw_b)
            if should_merge and not same_spec and (defined_a or defined_b):
                if defined_a and defined_b:
                    print(
                        f"[scanner] Dedup: NOT merging two DEFINED apps "
                        f"'{view_b.get('name') or path_b.stem}' / "
                        f"'{view_a.get('name') or path_a.stem}' "
                        f"(both operator-vouched; keeping both)",
                        flush=True,
                    )
                    should_merge = False
                elif defined_b:
                    print(
                        f"[scanner] Dedup: NOT merging — loser "
                        f"'{view_b.get('name') or path_b.stem}' is DEFINED "
                        f"(operator-vouched; never merged away)",
                        flush=True,
                    )
                    should_merge = False

            if should_merge:
                name_a = view_a.get("name") or raw_a.get("name") or path_a.stem
                name_b = view_b.get("name") or raw_b.get("name") or path_b.stem
                print(
                    f"[scanner] Dedup: merging '{name_b}' ({path_b.stem})"
                    f" → '{name_a}' ({path_a.stem})  {reason}",
                    flush=True,
                )
                raw_a = _merge_two_manifests(raw_a, raw_b)
                _atomic_write(path_a, raw_a, mode=0o644)
                # Refresh the comparable view so subsequent comparisons see
                # the merged file set (matters when more than two dupes
                # collapse into one winner).
                view_a = _comparable_view(raw_a, shared_dir)
                deleted_paths.add(path_b)
                try:
                    path_b.unlink()
                except OSError as e:
                    print(f"[scanner] Dedup: could not delete {path_b}: {e}", flush=True)
                merged_count += 1

    return merged_count


def _fill_missing_fields(older: dict, newer: dict) -> tuple[dict, int]:
    """Return a copy of ``older`` populated from ``newer`` only where the
    older manifest's top-level field is missing/empty. Lists and dicts
    are merged shallowly when present in both (older entries win).
    Returns ``(merged_dict, filled_count)``.
    """
    def _is_empty(v) -> bool:
        return v is None or v == "" or (isinstance(v, (list, dict)) and not v)

    merged = dict(older)
    filled = 0
    # Anchored-identity protection (Bite 2; §9.2 guarantee 2): when the survivor
    # (``older``) is a DEFINED app, never fill its operator-authored anchored
    # identity (name + canonical description) from the loser — not even an empty
    # field. Mirror of the guard in _merge_two_manifests, here for the
    # --dedup-existing cleanup path. No-op for discovered survivors.
    _locked_fields: set[str] = set()
    if _is_defined(older):
        from .reconciliation import is_field_authored as _is_field_authored
        from .coherence_actions import ANCHORED_IDENTITY_FIELDS as _ANCHOR_FIELDS
        _locked_fields = {f for f in _ANCHOR_FIELDS if _is_field_authored(older, f)}
    for key, newer_val in newer.items():
        if key in _locked_fields:
            continue
        older_val = merged.get(key)
        if _is_empty(older_val) and not _is_empty(newer_val):
            merged[key] = newer_val
            filled += 1
        elif isinstance(older_val, dict) and isinstance(newer_val, dict):
            for k, v in newer_val.items():
                if _is_empty(older_val.get(k)) and not _is_empty(v):
                    older_val[k] = v
                    filled += 1
    return merged, filled


def _archive_to_history(path: Path, reason: str) -> Path | None:
    """Move a manifest file into ``_history/`` with a reason-stamped name.

    Returns the archive path on success, None on failure. The archive
    name encodes the original stem, the reason, and the move timestamp
    so the operator can trace which scan deduped which manifest.
    Best-effort: file-permission failures fall back to copy-then-unlink.
    """
    history_dir = path.parent / "_history"
    try:
        history_dir.mkdir(exist_ok=True)
    except (PermissionError, OSError):
        pass
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = history_dir / f"{path.stem}_{reason}_{ts}.json"
    try:
        path.rename(archive_path)
        return archive_path
    except OSError:
        # Cross-device or permission issue — try copy + unlink.
        try:
            archive_path.write_bytes(path.read_bytes())
            path.unlink()
            return archive_path
        except OSError as e:
            print(f"[scanner] Dedup-existing: could not archive {path}: {e}",
                  flush=True)
            return None


def _manifest_file_footprint(data: dict) -> list[str]:
    """Return the union of paths a manifest claims to own.

    Reads both ``realized_files`` (v7 + v13 carry-over) and ``files`` (v5
    stamped entries). Either may be empty. Paths are returned as relative
    strings; non-string / malformed entries are silently skipped.
    """
    paths: list[str] = []
    for entry in (data.get("realized_files") or []):
        if isinstance(entry, dict):
            p = entry.get("path")
            if isinstance(p, str) and p:
                paths.append(p)
    for entry in (data.get("files") or []):
        if isinstance(entry, dict):
            p = entry.get("path")
            if isinstance(p, str) and p:
                paths.append(p)
    return paths


# Minimum file count for L3 platform-dominant archival. Below this, the
# rule is "100% platform-written" (the strict pre-Phase-1 test); at or
# above this, the rule relaxes to ≥90% platform-OWNED + no producer
# surface. Two-tier so a small platform-output stub still gets caught
# without a 10-file mass while a large manifest with a few stray
# bot-files isn't protected by mixing one real file in.
_L3_PLATFORM_DOMINANT_MIN_FILES = 10
_L3_PLATFORM_DOMINANT_PCT = 0.90


def _scheduled_actions_all_vacuous(value) -> bool:
    """True when every entry in a scheduled_actions[] list lacks an
    install.file / install.plist_label / install.command.

    Matches the v23.1 audit's "vacuous entry" definition (see
    app_audit_structural.producer_surface_kinds). Used by Pass A to
    decide whether scheduled_actions can be safely replaced — pre-v23.1
    scanner stubs (mechanism=unknown, all install fields None) are
    never operator-authored, so replacing them with fresh observational
    evidence never clobbers user data. Returns False for empty lists
    (handled by the standard ``_is_empty`` check) and for non-lists.
    """
    if not isinstance(value, list) or not value:
        return False
    for entry in value:
        if not isinstance(entry, dict):
            return False
        install = entry.get("install") or {}
        if isinstance(install, dict) and (
            install.get("file")
            or install.get("plist_label")
            or install.get("command")
        ):
            return False
    return True


def _has_script_realized_file(manifest: dict) -> bool:
    """True if ``realized_files[]`` carries at least one script entry.

    A script (.py / .sh / .bash) in realized_files[] is an operator-
    invocable surface — the operator runs ``python scripts/foo.py`` or
    similar. Counts toward producer-surface presence even when
    interface_contract.cli is empty (e.g. v7-arc Instance migration
    that dropped interface_contract). Mirrored in
    app_audit_structural._has_script_realized_file — kept in two
    places to avoid the cross-package import.
    """
    for rf in manifest.get("realized_files") or []:
        if not isinstance(rf, dict):
            continue
        path = (rf.get("path") or "").lower()
        if path.endswith((".py", ".sh", ".bash")):
            return True
    return False


def _synthesize_cli_from_scripts(manifest: dict) -> list[dict]:
    """Return synthesized ``interface_contract.cli`` entries derived from
    ``realized_files[]`` script entries.

    One entry per script: ``{command, name, source}``.

      - ``command`` matches the canonical
        ``interface_contract.cli[]`` schema (also what
        ``_manifest_cli_commands`` and the gallery renderer key off).
        ``python <path>`` for ``.py``; raw path for ``.sh`` / ``.bash``.
      - ``name`` is the logical name if present, else the path stem.
      - ``source: "scanner-inferred"`` flags the provenance so future
        passes / operator-facing UI can distinguish inferred entries
        from operator-asserted ones.

    Returns ``[]`` when realized_files carries no scripts. Caller is
    responsible for the only-if-empty guard on the existing CLI list —
    operator edits never get clobbered.
    """
    inferred: list[dict] = []
    for rf in manifest.get("realized_files") or []:
        if not isinstance(rf, dict):
            continue
        rpath = rf.get("path") or ""
        lower = rpath.lower()
        if not lower.endswith((".py", ".sh", ".bash")):
            continue
        command = f"python {rpath}" if lower.endswith(".py") else rpath
        inferred.append({
            "command": command,
            "name": rf.get("logical_name") or Path(rpath).stem,
            "source": "scanner-inferred",
        })
    return inferred


def _has_real_producer_surface(manifest: dict) -> bool:
    """Returns True iff the manifest declares at least one concrete
    invocation surface, as defined by the Tier-2 audit.

    Delegates to
    ``packages/analyzer/app_audit_structural.producer_surface_kinds``
    so the audit check, Pass A backfill trigger, and L3 archival gate
    share one definition. Without this, the three drifted: PR #2494's
    Pass A used ``all(field empty)`` while the audit (tightened v23.1
    the same day) rejected vacuous ``scheduled_actions[]`` entries, so
    backfill skipped the very manifests the audit then flagged.

    The analyzer's ``producer_surface_kinds`` covers vacuous-stub
    rejection (v23.1) AND script-realized-files-as-CLI (v23.2 — PR
    #2519); delegation picks up both behaviors automatically.
    """
    from app_audit_structural import producer_surface_kinds  # type: ignore
    return bool(producer_surface_kinds(manifest))


# Producer-surface kinds the scanner extracts from generic standing-instruction
# sections (AGENTS.md / HEARTBEAT.md identity headings). They are "SOFT": the
# scanner attaches them to an OC-system phantom (Session Startup, Persistent
# Memory System — whose heartbeat_evidence merely anchors AGENTS.md's "Memory"
# / "Session Startup" headings) exactly as readily as to a legit behavior app.
# So a soft surface, on its own, must NOT shield a no-app-evidence manifest
# from L3 archival — it shields only once the NAME / hard-class checks confirm
# a legit recurring-behavior app. Everything else producer_surface_kinds
# reports (a CLI, cron/launchd labels, event triggers, a realized script, or a
# NON-vacuous scheduled-action install) is CONCRETE — the app is genuinely
# wired into the runtime — and always shields.
_SOFT_SURFACE_KINDS = frozenset({"heartbeat_evidence"})


def _concrete_producer_surface_kinds(manifest: dict) -> set[str]:
    """The :func:`_has_real_producer_surface` kinds minus the soft, noise-
    extractable ones (:data:`_SOFT_SURFACE_KINDS`). A non-empty result means
    the manifest declares a surface that is actually installed/invocable, not
    just an anchor into a standing-instruction file. Used by L3 Rule 3 so a
    bare ``heartbeat_evidence`` anchor on an OC-system phantom no longer
    shields it. ``_has_real_producer_surface`` itself is unchanged — the
    Tier-2 audit and Pass A still treat heartbeat_evidence as a real surface
    for legit behavior apps."""
    from app_audit_structural import producer_surface_kinds  # type: ignore
    return set(producer_surface_kinds(manifest)) - _SOFT_SURFACE_KINDS


def _has_soft_producer_surface(manifest: dict) -> bool:
    """True if the manifest declares only a SOFT producer surface
    (:data:`_SOFT_SURFACE_KINDS` — a heartbeat_evidence anchor). Used by L3
    Rule 3 to recognize a behavior-shaped manifest without letting that soft
    anchor, on its own, shield an OC-system phantom from archival."""
    from app_audit_structural import producer_surface_kinds  # type: ignore
    return bool(_SOFT_SURFACE_KINDS & set(producer_surface_kinds(manifest)))


# The two PATH-BACKED producer-surface kinds the L3 archival gate discounts
# below when every backing path is the Evolve platform's own runtime:
#   - ``realized_files.script`` (v23.2) — a realized ``.py``/``.sh``/``.bash``.
#     Extensions mirror app_audit_structural._has_script_realized_file so "the
#     kind fires" and "a bot-authored script backs it" are decided over the
#     same file set.
#   - ``interface_contract.cli`` — a declared/inferred CLI command. The command
#     string's target path is pulled via :func:`_citation_paths` (so "python
#     evolve/task_extractor.py" → "evolve/task_extractor.py"), and the kind is
#     decided over app_audit_structural._manifest_cli_commands — the SAME
#     command set that makes producer_surface_kinds emit the kind.
# Every OTHER surface kind (crons, event_triggers, cron/heartbeat anchors, a
# non-vacuous scheduled-action install) is left untouched — Slice 1 covers only
# the path-backed script + CLI shields the live 'Evolve AI Pipeline' phantom
# carries.
_REALIZED_SCRIPT_SURFACE_KIND = "realized_files.script"
_REALIZED_SCRIPT_EXTS = (".py", ".sh", ".bash")
_CLI_SURFACE_KIND = "interface_contract.cli"


def _has_nonplatform_script_realized_file(manifest: dict) -> bool:
    """True if the manifest's file footprint carries at least one SCRIPT
    (``.py``/``.sh``/``.bash``) whose path is NOT a platform/infra file.

    The inferred ``realized_files.script`` producer surface fires on ANY
    realized script — including the Evolve platform's OWN runtime
    (``evolve/*.py``) when it is realized into a scanner-minted manifest
    (the live 'Evolve AI Pipeline' phantom: 12 ``evolve/*.py`` +
    ``evolve-backup/*``, every file platform-owned). A bot can never
    legitimately own ``evolve/*.py``, so for the L3 archival gate that
    inferred surface only counts when a bot-authored — non-platform,
    non-infra — script backs it. Mirrors the platform exclusion
    :func:`_app_evidence_files` already applies to evidence; the Tier-2
    audit + Pass A are unaffected (they read the surface directly)."""
    for p in _manifest_file_footprint(manifest):
        cleaned = _clean_evidence_path(p)
        if not cleaned or Path(cleaned).suffix.lower() not in _REALIZED_SCRIPT_EXTS:
            continue
        if (
            _is_platform_written_path(p)
            or _is_platform_owned_path(p)
            or _is_infra_script_path(p)
        ):
            continue
        return True
    return False


def _cli_command_is_platform_only(command: str) -> bool:
    """True if a CLI ``command`` string targets ONLY the Evolve platform's own
    runtime / infra.

    The command is run through :func:`_citation_paths` to pull its target
    script path(s) out of the surrounding invocation prose ("python
    evolve/task_extractor.py" → "evolve/task_extractor.py"). A command that
    resolves to AT LEAST ONE path, EVERY one of which is platform/infra, is
    platform-only. KEEP-BIASED: a command that resolves to NO path (a bare verb
    like "pipeline run", or an opaque entrypoint) is NOT platform-only — we
    cannot prove it targets the platform, so the surface is preserved."""
    paths = _citation_paths(command)
    if not paths:
        return False
    return all(
        _is_platform_written_path(p)
        or _is_platform_owned_path(p)
        or _is_infra_script_path(p)
        for p in paths
    )


def _has_nonplatform_cli_surface(manifest: dict) -> bool:
    """True if the manifest declares at least one CLI command that is NOT purely
    platform/infra-targeted — see :func:`_cli_command_is_platform_only`.

    The ``interface_contract.cli`` producer surface fires on ANY non-empty
    ``cli[].command``. The live 'Evolve AI Pipeline' phantom carries 11
    ``scanner-inferred`` entries whose command is EVERY ``python evolve/<x>.py``
    — a hallucinated CLI over the platform's OWN runtime (task_extractor /
    task_queue / task_runner / analyze / validate / apply / review / scoreboard
    / heal / outcome / evolve_config). A bot can never legitimately own
    ``evolve/*.py``, so for the L3 archival gate that surface only counts when a
    bot-authored (non-platform) command backs it. Reads the SAME command set as
    :func:`app_audit_structural._manifest_has_cli` (via ``_manifest_cli_commands``)
    that makes the kind fire, so "the kind fires" and "a real command backs it"
    are decided over one list. The Tier-2 audit + Pass A are unaffected — they
    read ``interface_contract.cli`` directly."""
    from app_audit_structural import _manifest_cli_commands  # type: ignore
    commands = _manifest_cli_commands(manifest)
    if not commands:
        return False
    return any(not _cli_command_is_platform_only(c) for c in commands)


def _l3_discount_platform_script(manifest: dict, kinds: set[str]) -> set[str]:
    """Drop a PATH-BACKED producer-surface kind from ``kinds`` when every path
    that backs it is the Evolve platform's own runtime / infra:
      - ``realized_files.script`` — no bot-authored (non-platform, non-infra)
        script backs it (:func:`_has_nonplatform_script_realized_file`);
      - ``interface_contract.cli`` — no bot-authored command backs it
        (:func:`_has_nonplatform_cli_surface`).

    The L3-archival-gate-only refinement of the producer-surface set, scoped
    exactly like :data:`_SOFT_SURFACE_KINDS`: a manifest whose ONLY surfaces are
    the platform's own realized runtime (the live 'Evolve AI Pipeline' phantom —
    12 ``evolve/*.py``-shaped realized files PLUS an 11-entry inferred CLI, all
    ``python evolve/<x>.py``) no longer shields itself from archival. A single
    bot-authored script OR CLI command keeps the surface (the MIXED 'Task
    Management System': real ``ops/tools/*.py`` beside platform
    ``evolve/task_*.py``). Every OTHER kind — cron/launchd, events, a non-vacuous
    scheduled-action install, a soft heartbeat anchor — is left untouched."""
    discounted = set(kinds)
    if (
        _REALIZED_SCRIPT_SURFACE_KIND in discounted
        and not _has_nonplatform_script_realized_file(manifest)
    ):
        discounted.discard(_REALIZED_SCRIPT_SURFACE_KIND)
    if _CLI_SURFACE_KIND in discounted and not _has_nonplatform_cli_surface(manifest):
        discounted.discard(_CLI_SURFACE_KIND)
    return discounted


def _l3_has_real_producer_surface(manifest: dict) -> bool:
    """:func:`_has_real_producer_surface` for the L3 archival gate (Rule 2):
    every surface kind EXCEPT a platform-only ``realized_files.script`` or
    ``interface_contract.cli`` one. Heartbeat / cron / events / non-vacuous
    install all still shield exactly as before; a bot-authored script or CLI
    command still shields — only a surface backed SOLELY by the platform's own
    runtime is removed."""
    from app_audit_structural import producer_surface_kinds  # type: ignore
    return bool(
        _l3_discount_platform_script(manifest, set(producer_surface_kinds(manifest)))
    )


def _l3_concrete_producer_surface_kinds(manifest: dict) -> set[str]:
    """:func:`_concrete_producer_surface_kinds` (soft heartbeat anchors already
    dropped) for the L3 gate, with the platform-script + platform-CLI discount
    applied. Used by Rule 3's concrete-surface shield so the Evolve runtime
    realized into an Instance — whether as realized scripts or as an inferred
    ``python evolve/*.py`` CLI — no longer reads as a real producer surface."""
    return _l3_discount_platform_script(
        manifest, _concrete_producer_surface_kinds(manifest)
    )


def _scheduled_action_referenced_paths(action: dict) -> list[str]:
    """Return every (raw) file path a single scheduled_action references — the
    files it installs, is anchored to, reads, or writes.

    Folded into L3 Rule 3's "cites a hard non-app class" test so an action
    whose target is an infra script (``install.command='bash
    gateway-selfheal.sh'`` / ``installed_artifact`` pointing at a self-heal
    plist) or the manifest store is recognized even when the manifest's
    ``evidence_files`` don't list it. Returned strings are RAW — the caller
    runs them through :func:`_citation_paths` so a command string like
    ``"bash bin/gateway-selfheal.sh"`` yields the script path. Pulls from
    ``install.file``, ``install.command``, ``installed_artifact``,
    ``trigger.evidence_path`` (the v13 extraction source file), and any
    ``inputs[]`` / ``outputs[]`` entries carrying a ``path`` / ``file``."""
    if not isinstance(action, dict):
        return []
    out: list[str] = []
    install = action.get("install")
    if isinstance(install, dict):
        for key in ("file", "command"):
            v = install.get(key)
            if isinstance(v, str) and v:
                out.append(v)
    art = action.get("installed_artifact")
    if isinstance(art, str) and art:
        out.append(art)
    trig = action.get("trigger")
    if isinstance(trig, dict):
        ep = trig.get("evidence_path")
        if isinstance(ep, str) and ep:
            out.append(ep)
    for key in ("inputs", "outputs"):
        for io in action.get(key) or []:
            if isinstance(io, str) and io:
                out.append(io)
            elif isinstance(io, dict):
                p = io.get("path") or io.get("file")
                if isinstance(p, str) and p:
                    out.append(p)
    return out


# ── Class-2: an infra-runbook doc does not redeem an all-infra cluster ────────
# Markdown / text doc extensions. A doc is GENUINE application evidence for a
# normal app — Daily Operations Briefing's ``operations/status-reports/*.md``
# are its OUTPUT, Property Management's ``property/*.md`` are its data. So a doc
# counts toward the app-evidence floor in the general case (and still does in
# :func:`_app_evidence_files`). The exception this set exists for: a doc must
# NOT, on its own, redeem a cluster whose every CODE/script citation is HARD
# infrastructure. An ``operations/maintenance/<x>-setup-instructions.md`` sitting
# beside ``sentry_ping.sh`` + ``gateway-selfheal.sh`` is documentation OF the
# infra, not a user application (the live 'Infrastructure Health Monitoring'
# false-positive — it survived #2894 only because that lone .md made the floor
# report app evidence, so Rule 3 never fired).
_DOC_EVIDENCE_EXTS = frozenset({".md", ".markdown", ".rst", ".txt"})


def _is_doc_evidence(path: str) -> bool:
    """True if ``path`` is a documentation file (``.md`` / ``.rst`` / ``.txt``)."""
    cleaned = _clean_evidence_path(path)
    if not cleaned:
        return False
    return Path(cleaned).suffix.lower() in _DOC_EVIDENCE_EXTS


def _doc_only_over_hard_infra(ev_paths: list[str], app_ev: list[str]) -> bool:
    """True when a cluster's surviving app-evidence is ALL docs AND every
    non-doc evidence file is a HARD non-app class (infra script / manifest
    store). In that case the docs are a runbook FOR the infra, not a user
    application, so they do not redeem the cluster — the floor's doc evidence
    is discounted and Rule 3 treats the cluster as no-app-evidence.

    Keyed on ``bool(non_doc) and all-hard-infra``: there must be at least one
    hard-infra CODE file and NO non-infra code/producer file. This is precisely
    why it can NOT over-drop the legit doc-producing apps:

      - Daily Operations Briefing / Property Management cite docs with NO
        hard-infra script in evidence → ``non_doc`` is empty (or holds a real
        producer) → returns False (the docs redeem normally).
      - A cluster with a real ``.py`` producer beside an infra script → that
        ``.py`` is non-doc and not hard-infra → ``all(...)`` is False → returns
        False (kept; it has a genuine app surface).
    """
    if not app_ev or not all(_is_doc_evidence(p) for p in app_ev):
        return False
    non_doc = [p for p in ev_paths if not _is_doc_evidence(p)]
    return bool(non_doc) and all(_is_hard_nonapp_class(p) for p in non_doc)


# ── Class-1: incoherent zero-realization infra shell discriminators ───────────
# Rule 3 needs a CITED non-app file to classify, so it deliberately skips a
# manifest minted with zero evidence + zero footprint (the empty-in-progress
# contract). But an empty shell that ASSERTS active recurring behavior in prose
# while realizing NOTHING is not in-progress — it is incoherent (it claims to
# run on a schedule with nothing backing it). Rule 4 splits the two; these
# helpers supply its CLAIM half and its over-drop guard.

# The CLAIM vocabulary: schedule hints (reused from _SCHEDULE_HINT_PATTERNS)
# plus the recurrence/automation words the scanner's LLM writes for monitoring
# /self-healing infra ("periodic", "automated ... system", "continuous
# operation", "self-healing", "runs on a schedule"). Deliberately does NOT
# include a bare "monitor" — that word alone is not a recurrence claim, so a
# placeholder objective ("<App> objective text", "Watchdog Monitoring System")
# stays below the bar and the empty-in-progress contract holds.
_RECURRING_CLAIM_PATTERNS = _SCHEDULE_HINT_PATTERNS + [
    r"\bperiodic(?:al(?:ly)?)?\b",
    r"\bautomat(?:e|ed|es|ion|ic|ically)\b",
    r"\bcontinuous(?:ly)?\b",
    r"\bongoing\b",
    r"\brecurring\b",
    r"\bself[\s\-]?heal(?:ing|s|ed)?\b",
    r"\bruns?\s+(?:on\s+)?(?:a\s+)?(?:set\s+|fixed\s+|regular\s+)?schedul",
]
_RECURRING_CLAIM_RE = re.compile("|".join(_RECURRING_CLAIM_PATTERNS), re.I)

# The over-drop guard: infra/platform PURPOSE vocabulary. A phantom whose NAME
# or objective describes maintaining the pod's OWN runtime (the OpenClaw
# runtime, liveness probing, self-healing, the repo puller) rather than serving
# the operator's domain. HIGH-PRECISION terms ONLY — product-specific
# (`openclaw`) or infra-jargon (`liveness`, `self-heal`, `repo-pull`) that a
# legit operator app never uses. Bare English words with common non-infra
# meanings (`gateway` — payment/API gateway; `heartbeat` — a heart-rate app;
# `uptime` — a website-uptime app) are deliberately EXCLUDED: a freshly-stubbed
# 'Heartbeat Tracker' fitness app or 'Payment Gateway Reconciler' that asserts a
# schedule must not be archived by Rule 4 (adversarial-review over-drop). The
# live targets are still caught — 'Gateway Self-Healing' via self-heal +
# openclaw, 'Liveness Monitoring System' via liveness. Generic only, never a
# deployment-specific bot NAME (trips the scrub guard, isn't portable).
_INFRA_PURPOSE_PATTERNS = [
    r"\bopenclaw\b",
    r"\bliveness\b",
    r"\bself[\s\-]?heal(?:ing|s|ed)?\b",
    r"\bself[\s\-]?recover(?:y|ing|s)?\b",
    r"\bwatchdog\b",
    r"\brepo[\s\-]?pull",
]
_INFRA_PURPOSE_RE = re.compile("|".join(_INFRA_PURPOSE_PATTERNS), re.I)


def _manifest_prose(manifest: dict) -> str:
    """Concatenate a manifest's human prose — name + description + purpose +
    objective — for the Class-1 claim / infra text classifiers. ``objective``
    may be a plain string (legacy) or a v7-arc ``{primary, sub_objectives}``
    dict; both shapes are flattened."""
    parts: list[str] = []
    for key in ("name", "description", "purpose", "summary"):
        v = manifest.get(key)
        if isinstance(v, str):
            parts.append(v)
    obj = manifest.get("objective")
    if isinstance(obj, str):
        parts.append(obj)
    elif isinstance(obj, dict):
        p = obj.get("primary")
        if isinstance(p, str):
            parts.append(p)
        for so in obj.get("sub_objectives") or []:
            if isinstance(so, str):
                parts.append(so)
    return " ".join(parts)


def _asserts_recurring_behavior(text: str) -> bool:
    """True if prose ASSERTS active recurring / automated behavior (a schedule,
    "periodic", "automated ... system", "continuous operation", "self-healing",
    "runs on a schedule"). This is the CLAIM half of Rule 4's claim-without-
    realization coherence test. Placeholder prose with no recurrence/automation
    verb returns False, preserving the empty-in-progress contract."""
    return bool(_RECURRING_CLAIM_RE.search(text or ""))


def _describes_pod_infra(text: str) -> bool:
    """True if NAME / objective prose describes pod or platform infrastructure
    (the OpenClaw gateway, liveness / self-heal machinery, the repo puller) —
    not an operator-domain application. Generic vocabulary only, no bot names.
    Rule 4's over-drop guard: a freshly-stubbed operator app that merely asserts
    a future schedule (no infra signal) is never archived."""
    return bool(_INFRA_PURPOSE_RE.search(text or ""))


def _archive_platform_file_only_stubs(
    caps_dir: Path,
    log_collector: list[str] | None = None,
    shared_dir: Path | None = None,
) -> list[str]:
    """L3 platform-files defense — archive scanner-minted manifests whose file
    footprint is platform/infrastructure noise and which declare no real
    producer surface.

    Background: pre-#2476 scanner passes minted "Session Turn Logs",
    "Audit Logging" and similar manifests by handing platform-output
    paths (memory/turns-*.jsonl, evolve/audit_outbox/*) to the LLM as
    workspace evidence. Migration carried these forward. Phase 1
    (pod survey 2026-06-09) extended the rule to also catch large
    manifests pointing at the Evolve runtime (evolve/*.py), pod-wide
    templates (HEARTBEAT.md, POD_CONDUCT.md), and scanner state
    (manifests/_history/). #2705 (this PR) added the app-evidence floor:
    manifests whose evidence is infra scripts, OC identity/system files,
    the manifest store, or bare skill configs. All shapes share one thing:
    the manifest declares NO real producer surface.

    Four archival rules, joined by OR:

      RULE 1 — strict platform-only (small or large)
        - footprint non-empty
        - EVERY path is platform-WRITTEN (Tier A)
        - manifest carries no operator semantics (empty objective +
          identity + evidence_files)

      RULE 2 — platform-dominant + no surface (Phase 1)
        - footprint size >= _L3_PLATFORM_DOMINANT_MIN_FILES
        - >= _L3_PLATFORM_DOMINANT_PCT of paths are platform-OWNED (Tier B)
        - no real producer surface (see _has_real_producer_surface)

      RULE 3 — no app-evidence (#2705, the floor applied on reconcile)
        - the manifest cites at least one #2705 non-app class (infra
          script / OC identity-system file / manifest store / bare skill
          config) — this is what distinguishes it from RULE 1/2's
          platform-OUTPUT case, so an operator's turn-collector monitor is
          left alone
        - ZERO application evidence across evidence_files + footprint
          (per _app_evidence_files). Bite C refinement: a doc/.md is normally
          app evidence, but does NOT redeem a cluster whose every CODE/script
          citation is HARD infra (_doc_only_over_hard_infra) — the live
          'Infrastructure Health Monitoring' case (sentry_ping.sh +
          gateway-selfheal.sh + one setup-instructions .md).
        - it is not SHIELDED (see below). A no-app-evidence manifest is kept
          only when it does NOT cite a HARD non-app class (_is_hard_nonapp_class
          — infra script / manifest store, which no real app ever owns; this
          defeats every shield) AND it EITHER has a concrete producer surface
          (a CLI, cron/launchd labels, event triggers, a realized script, or a
          non-vacuous scheduled-action install) OR presents as a legit
          recurring-behavior app (behavior-shaped — scheduled_actions or a soft
          heartbeat_evidence anchor — with a name that is not an OC runtime-
          function, _name_is_oc_system_function). OC identity files
          (AGENTS.md/HEARTBEAT.md sections) are NOT hard, so a hand-written
          "Morning Briefing" behavior app is kept while a "Memory Persistence"
          / "System Health Monitoring" phantom — whose name is OC-system or
          whose evidence cites an infra script — is swept.
        - scanner-authored only (never operator/gallery/RSI/bot — see
          _is_operator_authored)

      RULE 4 — incoherent zero-realization infra shell (Bite C)
        Rule 3 needs a cited non-app file to classify, so it cannot fire on a
        manifest with zero evidence + zero footprint. Rule 4 covers exactly
        that gap WITHOUT breaking the empty-in-progress contract, archiving
        only when ALL of:
        - NO realization: no concrete producer surface AND no application
          evidence (a realized script or real app file makes this False);
        - it CLAIMS recurring behavior — prose asserts a schedule / automation
          (_asserts_recurring_behavior: "periodic", "automated ... system",
          "self-healing", "runs on a schedule") OR it is behavior-shaped
          (scheduled_actions / a soft heartbeat anchor). A genuinely empty
          manifest that makes NO behavioral claim stays below this bar and is
          preserved as in-progress;
        - it carries an INFRA signal — infra-by-purpose name/objective
          (_describes_pod_infra: gateway / liveness / self-heal / openclaw), an
          OC-system-function name, or a hard-infra citation. This is the
          over-drop guard: a freshly-stubbed OPERATOR-domain app that merely
          asserts a future schedule has no infra signal and is kept;
        - scanner-authored.
        The live 'Liveness Monitoring System' / 'Gateway Self-Healing' phantoms
        (empty evidence + empty realized_files, description asserts "periodic
        pings" / "automated gateway ... recovery") are this exact shape.

    Rule 1 preserves PR #2476 behavior for the original Session Turn
    Logs class. Rule 2 catches the survey's platform-dominant classes.
    Rule 3 retires the #2705 phantoms already on disk that CITE at least one
    non-app file (infra-script monitoring, Session Startup, Memory
    Persistence, Google OAuth, Infrastructure Manifest Tracking) — INCLUDING
    the ones that carry vacuous scheduled_actions[] stubs, which the pre-fix
    raw-presence guard wrongly shielded (the 4th-round production false-positive).
    A phantom minted with ZERO evidence and zero footprint has nothing for
    Rule 3 to classify; Rule 4 now retires the subset of those that ASSERT
    recurring behavior with an infra signal (incoherent shells), while a
    zero-evidence manifest that makes NO behavioral claim is still preserved as
    in-progress (test_l3_skips_manifest_with_empty_realized_files). The
    Phase-2 floor prevents any from recurring. The no-realization / infra-signal
    / scanner-authored gates keep every rule from touching a real app.
    ``shared_dir`` loads each v7-arc Instance's bound Spec (via
    _load_bound_spec) so its real surface + files protect legit v7-arc apps
    from Rules 3 and 4.

    Returns a list of one-line ``"<stem>: <reason>"`` strings.
    """
    archived: list[str] = []

    def _slog(msg: str) -> None:
        print(msg, flush=True)
        if log_collector is not None:
            log_collector.append(msg)

    try:
        candidates = sorted(caps_dir.glob("*.json"))
    except OSError:
        return archived

    for mf in candidates:
        if mf.name.startswith(".") or "_history" in str(mf):
            continue
        try:
            data = json.loads(mf.read_text())
        except (OSError, json.JSONDecodeError):
            continue

        # EXISTENCE GUARANTEE — L3 archival shield (Defined/Discovered
        # lifecycle, Bite 1; docs/spec-apps-meta-2026-06-13.md §9.2 guarantee 1).
        # A manifest an operator has promoted to definition_status="defined" is
        # the vouched source of truth and is NEVER archived by THIS L3
        # classifier — even with zero files on disk. Short-circuit at the top of
        # the loop so the shield is unconditional across ALL four archival rules
        # below (a defined app with a platform-only footprint or a zero-evidence
        # shell stays put). Parallel to the source-based _is_operator_authored
        # shield used inside Rules 3/4 (see _is_defined); the two are additive.
        # NOTE: like _is_operator_authored, this does NOT guard the same-pass
        # _dedup_manifests merge/delete path — that is the identity-stability
        # guarantee (§9.2 guarantee 2), built by the scanner-watchdog Bite 2
        # (§9.4 / §9.6 bite 2). Bite 1 ships the existence shield only.
        if _is_defined(data):
            _slog(
                f"[scanner] defined keep {mf.stem} "
                f"({data.get('name','?')!r}) — definition_status=defined "
                f"(operator-vouched; existence-shielded from L3 archival)"
            )
            continue

        footprint = _manifest_file_footprint(data)
        n = len(footprint)
        platform_written = sum(1 for p in footprint if _is_platform_written_path(p))
        platform_owned = sum(1 for p in footprint if _is_platform_owned_path(p))

        objective = data.get("objective") or ""
        identity = data.get("identity") or {}
        evidence_files = data.get("evidence_files") or []

        # Rule 1 — strict platform-written + no-content shell. (needs footprint)
        rule1_strict = bool(footprint) and (platform_written == n)
        rule1_no_content = not (objective or identity or evidence_files)
        rule1_match = rule1_strict and rule1_no_content
        rule1_reason = "platform_files_only_stub" if rule1_match else ""

        # Rule 2 — platform-dominant + no producer surface. (needs footprint)
        # The surface check is L3-gate-scoped: a manifest whose ONLY surface is
        # the Evolve platform's own realized runtime (evolve/*.py, all
        # platform-owned — the 'Evolve AI Pipeline' phantom) reads as
        # NO surface here, so the ≥90%-platform-owned rule retires it. A
        # bot-authored script, CLI, cron, or non-vacuous install still shields.
        rule2_size = n >= _L3_PLATFORM_DOMINANT_MIN_FILES
        rule2_pct = bool(n) and (platform_owned / n) >= _L3_PLATFORM_DOMINANT_PCT
        rule2_no_surface = not _l3_has_real_producer_surface(data)
        rule2_match = rule2_size and rule2_pct and rule2_no_surface
        rule2_reason = "platform_dominant_no_surface" if rule2_match else ""

        # Rule 3 — no app-evidence (#2705). For v7-arc Instances the bound Spec
        # carries the real app surface + files (hydrate_v7_arc_instance does
        # NOT overlay interface_contract/files), so load it explicitly: a legit
        # CLI/script v7-arc app whose Instance is observational must never be
        # archived. Combine every place a manifest declares files: raw +
        # hydrated + Spec evidence_files, plus all three footprints.
        view3 = _comparable_view(data, shared_dir)
        spec3 = _load_bound_spec(data, shared_dir)
        # Expand each citation into clean path token(s) via _citation_paths so a
        # noisy infra citation ("gateway-selfheal.sh (cron job */15)") is
        # classified by its real script path, not the trailing cron prose.
        ev3 = list(
            dict.fromkeys(
                t
                for cit in (
                    list(evidence_files)
                    + list(view3.get("evidence_files") or [])
                    + list(spec3.get("evidence_files") or [])
                    + _manifest_file_footprint(view3)
                    + _manifest_file_footprint(spec3)
                    + footprint
                )
                for t in _citation_paths(cit)
            )
        )
        rule3_has_2705 = any(_is_2705_nonapp_class(p) for p in ev3)
        # Prose across the instance, hydrated view, and bound Spec — fuels the
        # infra-by-purpose / recurring-behavior text classifiers (Class 1 + 2).
        prose4 = " ".join(_manifest_prose(m) for m in (data, view3, spec3))
        rule3_name_oc = _name_is_oc_system_function(data.get("name") or "")
        # Infra-by-purpose: the name/objective describes the pod's OWN runtime
        # (an OC-system-function name, or _describes_pod_infra prose) — never an
        # operator-domain app.
        rule3_infra_purpose = rule3_name_oc or _describes_pod_infra(prose4)
        # Class-2 (Bite C): a doc/.md is normally app evidence, but it does NOT
        # redeem a cluster whose every CODE/script citation is HARD infra AND
        # whose name/objective is infra-by-purpose. The live 'Infrastructure
        # Health Monitoring' phantom cites sentry_ping.sh + gateway-selfheal.sh
        # (both hard) plus a lone setup-instructions .md, and its objective is
        # "self-healing ... OpenClaw gateway ... liveness pinging". Pre-fix the
        # .md made _app_evidence_files non-empty so Rule 3 never fired. The
        # infra-purpose gate keeps a legit doc-PRODUCING app that merely cites
        # ONE infra/manifest path (a "Weekly Ops Report" with a real surface, an
        # "App Catalog" reading the manifest store) from losing its doc
        # redemption — only an infra-named cluster is discounted.
        app_ev3 = _app_evidence_files(ev3)
        rule3_no_app_ev = bool(ev3) and (
            not app_ev3
            or (rule3_infra_purpose and _doc_only_over_hard_infra(ev3, app_ev3))
        )
        # Shield model (replaces the old raw-presence `rule3_no_sched` +
        # any-surface gate, which together shielded EVERY phantom that carried
        # a scheduled_actions[] list or a heartbeat_evidence anchor — including
        # the vacuous mechanism="unknown" stubs and generic AGENTS.md identity
        # anchors the scanner attaches to infra/OC-system clusters, the live
        # production false-positives). A no-app-evidence manifest is KEPT only when
        # it does NOT cite a HARD non-app class (infra script / manifest store
        # — classes no real app ever owns; this defeats EVERY shield, since an
        # infra LaunchAgent attributed back to the phantom would otherwise look
        # like a real cron surface) AND EITHER:
        #   (a) it has a CONCRETE producer surface — a CLI, cron/launchd
        #       labels, event triggers, a realized script, or a non-vacuous
        #       scheduled-action install — i.e. it is genuinely wired into the
        #       runtime; OR
        #   (b) it presents as a legit recurring-behavior app: it is
        #       behavior-shaped (has scheduled_actions or a soft
        #       heartbeat_evidence anchor) AND its name is NOT an OC runtime-
        #       function (memory/session/startup/…).
        # OC identity files (AGENTS.md/HEARTBEAT.md sections) are deliberately
        # NOT "hard": a hand-written "Morning Briefing" behavior app lives
        # there too, and is told apart from a "Memory Persistence" /
        # "Session Startup" phantom by the OC-system NAME test. A real Spec's
        # surface/evidence is already folded in via spec3.
        sched_actions = (
            list(view3.get("scheduled_actions") or [])
            + list(data.get("scheduled_actions") or [])
            + list(spec3.get("scheduled_actions") or [])
        )
        sched_paths = [
            t
            for a in sched_actions
            for raw in _scheduled_action_referenced_paths(a)
            for t in _citation_paths(raw)
        ]
        # L3-gate-scoped concrete surface: discounts a platform-only inferred
        # realized-script surface (see _l3_discount_platform_script) so the
        # Evolve runtime realized into an Instance doesn't masquerade as a real
        # CLI. The bound Spec (spec3) keeps a legit v7-arc app's real surface.
        rule3_concrete_surface = bool(
            _l3_concrete_producer_surface_kinds(view3)
            or _l3_concrete_producer_surface_kinds(data)
            or _l3_concrete_producer_surface_kinds(spec3)
        )
        rule3_soft_surface = (
            _has_soft_producer_surface(view3)
            or _has_soft_producer_surface(data)
            or _has_soft_producer_surface(spec3)
        )
        rule3_behavior_shaped = bool(sched_actions) or rule3_soft_surface
        # rule3_name_oc / prose4 computed above (feed Class 1 + 2).
        rule3_cites_hard = any(_is_hard_nonapp_class(p) for p in (ev3 + sched_paths))
        rule3_shielded = not rule3_cites_hard and (
            rule3_concrete_surface
            or (rule3_behavior_shaped and not rule3_name_oc)
        )
        rule3_scanner = not _is_operator_authored(data)
        rule3_match = (
            rule3_has_2705 and rule3_no_app_ev
            and not rule3_shielded and rule3_scanner
        )
        rule3_reason = "no_app_evidence" if rule3_match else ""

        # Rule 4 — incoherent zero-realization infra shell (Bite C). Rule 3
        # needs a cited non-app file to classify, so it deliberately skips a
        # manifest minted with zero evidence + zero footprint (the empty-in-
        # progress contract). But an empty shell that ASSERTS active recurring
        # behavior while realizing NOTHING is not in-progress — it is incoherent
        # (it claims to run on a schedule with nothing backing it). Archive only
        # when ALL four hold, so a legit freshly-stubbed app is never caught:
        #   - NO realization: no concrete producer surface AND no application
        #     evidence (a realized script or real app file makes this False);
        #   - it CLAIMS recurring behavior — prose asserts a schedule/automation
        #     ("periodic", "automated ... system", "self-healing") OR it is
        #     behavior-shaped (scheduled_actions / a soft heartbeat anchor).
        #     Placeholder prose with no recurrence verb stays below the bar, so
        #     a no-claim empty manifest is preserved as in-progress;
        #   - it carries an INFRA signal — infra-by-purpose name/objective
        #     (gateway / liveness / self-heal / openclaw), an OC-system-function
        #     name, or a hard-infra citation. This is the over-drop guard: a
        #     freshly-stubbed OPERATOR-domain app that merely asserts a future
        #     schedule is kept (it has no infra signal);
        #   - scanner-authored.
        # The live 'Liveness Monitoring System' / 'Gateway Self-Healing'
        # phantoms (empty evidence + empty realized_files, description asserts
        # "periodic pings" / "automated gateway ... recovery") are this shape.
        # prose4 / rule3_infra_purpose computed above (Class 1 + 2 share them).
        rule4_no_realization = not rule3_concrete_surface and not app_ev3
        rule4_claims = _asserts_recurring_behavior(prose4) or rule3_behavior_shaped
        rule4_infra = rule3_infra_purpose or rule3_cites_hard
        rule4_match = (
            rule4_no_realization and rule4_claims and rule4_infra and rule3_scanner
        )
        rule4_reason = "incoherent_infra_shell" if rule4_match else ""

        if not (rule1_match or rule2_match or rule3_match or rule4_match):
            # Log-only when something nearly matched but a guard kept it.
            # Helps operators spot near-misses without acting on them.
            if rule2_size and rule2_pct:
                _slog(
                    f"[scanner] platform-dominant keep {mf.stem} "
                    f"({data.get('name','?')!r}) — "
                    f"{platform_owned}/{n} platform-owned but "
                    f"has_real_surface={not rule2_no_surface}"
                )
            elif rule1_strict and not rule1_no_content:
                _slog(
                    f"[scanner] platform-stub keep {mf.stem} "
                    f"({data.get('name','?')!r}) — footprint is platform-only but "
                    f"manifest has operator content "
                    f"(objective={bool(objective)}, identity={bool(identity)}, "
                    f"evidence={len(evidence_files)})"
                )
            elif rule3_has_2705 and not rule3_match:
                _slog(
                    f"[scanner] no-app-evidence keep {mf.stem} "
                    f"({data.get('name','?')!r}) — cites a non-app class but "
                    f"kept (app_evidence={not rule3_no_app_ev}, "
                    f"shielded={rule3_shielded} "
                    f"[concrete_surface={rule3_concrete_surface} "
                    f"behavior_shaped={rule3_behavior_shaped} "
                    f"name_oc={rule3_name_oc} cites_hard={rule3_cites_hard}], "
                    f"operator_authored={not rule3_scanner})"
                )
            elif rule4_claims and rule4_infra and not rule4_match:
                # An incoherent-shell near-miss: claims recurring behavior with
                # an infra signal but something real (a surface / app evidence /
                # operator authorship) kept it.
                _slog(
                    f"[scanner] infra-shell keep {mf.stem} "
                    f"({data.get('name','?')!r}) — claims recurring behavior + "
                    f"infra signal but kept (no_realization={rule4_no_realization} "
                    f"[concrete_surface={rule3_concrete_surface} "
                    f"app_evidence={bool(app_ev3)}], "
                    f"operator_authored={not rule3_scanner})"
                )
            continue

        reason = rule1_reason or rule2_reason or rule3_reason or rule4_reason
        archive_path = _archive_to_history(mf, reason=reason)
        if archive_path is not None:
            example = footprint[0] if footprint else (ev3[0] if ev3 else "?")
            if rule1_match:
                entry = (
                    f"{mf.stem} ({data.get('name','?')!r}) — "
                    f"{n} platform-written file(s) [rule1] e.g. {example}"
                )
            elif rule2_match:
                entry = (
                    f"{mf.stem} ({data.get('name','?')!r}) — "
                    f"{platform_owned}/{n} platform-owned, no producer surface "
                    f"[rule2] e.g. {example}"
                )
            elif rule3_match:
                entry = (
                    f"{mf.stem} ({data.get('name','?')!r}) — "
                    f"no application evidence, no producer surface "
                    f"[rule3:no_app_evidence] e.g. {example}"
                )
            else:
                entry = (
                    f"{mf.stem} ({data.get('name','?')!r}) — "
                    f"claims recurring behavior, realizes nothing, infra signal "
                    f"[rule4:incoherent_infra_shell]"
                )
            archived.append(entry)
            _slog(f"[scanner] platform-stub ARCHIVED {entry}")
        else:
            _slog(f"[scanner] platform-stub archive FAILED {mf}")

    return archived


def dedup_existing_manifests(caps_dir: Path, shared_dir: Path | None = None) -> dict:
    """One-time cleanup mode: walk ``caps_dir`` looking for PAIRS of
    manifests that the new richer match logic identifies as duplicates,
    merge each pair, and move the loser to ``_history/``.

    ``shared_dir`` is threaded into ``_build_existing_manifest_index`` so
    thin v7-arc Instances hydrate before comparison (see that function);
    without it, a pair of duplicate Instances whose only common signal is
    the Spec-derived name would be missed by this cleanup pass too.

    For each duplicate pair (matched via canonical id/name/stem overlap
    or evidence-file overlap >= 50%):

      - The manifest with the older ``created_at`` is the WINNER
        (operator-edited content lives there). Tiebreaker on equal
        timestamps: the manifest with more populated top-level fields
        wins; final tiebreak is lexicographic on stem (deterministic).
      - Top-level fields from the loser are filled into the winner only
        when the winner's field is missing/empty. Existing winner state
        is never clobbered.
      - The loser is moved to ``{caps_dir}/_history/`` with a name
        encoding the original stem, the reason (``deduped``), and a UTC
        timestamp. Operator can review and restore.

    Returns ``{"merged": int, "winners": [stem,...], "archived": [stem,...]}``.

    Safe to run on a clean dir — no duplicate pairs means no writes.
    Not invoked from the normal scan path; called by the
    ``application scan --dedup-existing`` CLI flag.
    """
    index = _build_existing_manifest_index(caps_dir, shared_dir)
    if len(index) < 2:
        return {"merged": 0, "winners": [], "archived": []}

    def _populated_count(d: dict) -> int:
        return sum(
            1 for v in d.values()
            if v not in (None, "", [], {})
        )

    archived_paths: set[Path] = set()
    merged_count = 0
    winners: list[str] = []
    archived_stems: list[str] = []

    # Pairwise: O(n^2) over typically <30 manifests — fine.
    for i in range(len(index)):
        entry_a = index[i]
        if entry_a["path"] in archived_paths:
            continue
        for j in range(i + 1, len(index)):
            entry_b = index[j]
            if entry_b["path"] in archived_paths:
                continue

            # Match using the same rules as scan-time dedup.
            canons_a = {c for c in (entry_a["id_canon"], entry_a["stem_canon"],
                                    entry_a["name_canon"]) if c}
            canons_b = {c for c in (entry_b["id_canon"], entry_b["stem_canon"],
                                    entry_b["name_canon"]) if c}
            ev_a, ev_b = entry_a["evidence_set"], entry_b["evidence_set"]
            ev_overlap = 0.0
            if ev_a and ev_b:
                ev_overlap = len(ev_a & ev_b) / min(len(ev_a), len(ev_b))

            should_merge = False
            reason = ""
            if canons_a & canons_b:
                # Canonical id/name/stem equality = the SAME app under a drifted
                # filename — an identity match, authoritative (mirrors scan-time
                # cond 0). Not gated by the distinct-apps veto.
                should_merge = True
                reason = f"canon={sorted(canons_a & canons_b)[0]}"
            elif ev_overlap >= 0.5 and not _are_distinct_apps(
                entry_a.get("name") or "", entry_a["data"],
                entry_b.get("name") or "", entry_b["data"],
            ):
                # File-overlap ALONE — gate it with the #3095 distinct-apps veto
                # exactly as scan-time _dedup_manifests does (this cleanup path
                # historically lacked the veto, so two established, distinctly-
                # named apps sharing a substrate — Atlas's atlas_lib/ — could
                # collapse here). The veto also stops the Bite-2 defined-
                # precedence below from deterministically archiving a DISTINCT
                # discovered app that merely shares a defined app's substrate.
                should_merge = True
                reason = f"ev_overlap={ev_overlap:.2f}"

            if not should_merge:
                continue

            # ── Identity-stability (Bite 2; §9.2 guarantee 2) ────────────────
            # A `defined` app is operator-vouched and must never be archived as
            # a dedup loser. Definition status OUTRANKS the created_at winner
            # rule: a defined manifest always wins over a discovered one, so only
            # the discovered duplicate is archived. Two matched defined
            # manifests are BOTH vouched — never archive either (keep both). The
            # loser passed to _archive_to_history below is therefore provably
            # never a defined app.
            def_a = _is_defined(entry_a["data"])
            def_b = _is_defined(entry_b["data"])
            if def_a and def_b:
                print(
                    f"[scanner] Dedup-existing: NOT merging two DEFINED apps "
                    f"'{entry_a['stem']}' / '{entry_b['stem']}' "
                    f"(both operator-vouched; keeping both)",
                    flush=True,
                )
                continue

            # Pick winner: a defined manifest outranks a discovered one;
            # otherwise older created_at wins, tiebreak on populated fields,
            # then deterministic stem.
            if def_a != def_b:
                winner, loser = (entry_a, entry_b) if def_a else (entry_b, entry_a)
            else:
                ca_a, ca_b = entry_a["created_at"], entry_b["created_at"]
                if ca_a and ca_b and ca_a != ca_b:
                    winner, loser = (entry_a, entry_b) if ca_a < ca_b else (entry_b, entry_a)
                else:
                    pop_a = _populated_count(entry_a["data"])
                    pop_b = _populated_count(entry_b["data"])
                    if pop_a != pop_b:
                        winner, loser = (entry_a, entry_b) if pop_a > pop_b else (entry_b, entry_a)
                    else:
                        winner, loser = (entry_a, entry_b) if entry_a["stem"] < entry_b["stem"] else (entry_b, entry_a)

            merged_data, filled = _fill_missing_fields(winner["data"], loser["data"])
            # Track the merge in the manifest for operator audit.
            merged_data["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            merged_data["merged_from"] = (
                merged_data.get("merged_from") or []
            ) + [loser["stem"]]

            print(
                f"[scanner] Dedup-existing: '{loser['stem']}' → '{winner['stem']}' "
                f"({reason}; filled {filled} field(s))",
                flush=True,
            )
            _atomic_write(winner["path"], merged_data, mode=0o644)
            arch = _archive_to_history(loser["path"], reason="deduped")
            if arch is not None:
                archived_paths.add(loser["path"])
                archived_stems.append(loser["stem"])
                merged_count += 1
                winners.append(winner["stem"])
                # Refresh winner entry's data so subsequent comparisons in
                # this pass see the merged state.
                winner["data"] = merged_data
                winner["evidence_set"] = frozenset(_ev_path_keys(merged_data))
                # Mark entry_a as updated when it's the winner (so the next
                # j-loop iteration uses the merged state).
                if winner is entry_a:
                    entry_a = winner

    return {
        "merged": merged_count,
        "winners": winners,
        "archived": archived_stems,
    }


def _stamp_discovered_files(
    manifest_dict: dict,
    workspace: Path,
    manifest_path: Path,
    log_collector: list[str] | None = None,
) -> None:
    """
    Phase 5 — assign provenance IDs and embed markers into every file claimed by
    a freshly discovered manifest.

    This is intentionally best-effort: individual file failures are logged and
    skipped rather than aborting the whole stamp.  The manifest is re-saved only
    when at least one field was updated (pkg_id assigned or files list built).

    What it does:
      1. Assigns a pkg_id to the manifest if it lacks one.
      2. Resolves evidence_files entries to actual on-disk paths.
      3. Assigns a file_id to each found file, infers a layer from its extension,
         and builds a v5-format files list entry.
      4. Calls embed_marker() on each file (merge=True so existing ownership is
         preserved if the file is already claimed by another app).
      5. Re-saves the manifest with the updated pkg_id and files list.
      6. Sets source to 'discovered' and source_detail to the scan timestamp.
    """
    from .ids import new_pkg_id, new_file_id, calver_today
    from .provenance import embed_marker
    from .manifest import MANIFEST_SOURCE_DISCOVERED, MANIFEST_DEFINITION_DISCOVERED
    # Deferred import to break the module cycle: app_ownership_policy imports
    # scanner's canonical never-ownable artifacts at module load, so scanner
    # cannot import it at top level. can_app_own is the AUTHORITATIVE gate for
    # "may an application own this path?" — it subsumes _STAMP_SKIP_DIRS and the
    # platform-written / OC-identity checks, and is what keeps Phase 5 from
    # minting file_ids + embedding _evolve markers into the evolve/ telemetry
    # tree (audit_outbox rec-*.json) or OC-standard files (AGENTS.md, …).
    from .app_ownership_policy import can_app_own
    import re as _re

    # Helper: print + optionally append to log_collector
    def _slog(msg: str) -> None:
        print(msg, flush=True)
        if log_collector is not None:
            log_collector.append(msg)

    changed = False
    now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    app_id   = manifest_dict.get("id", "?")
    app_name = manifest_dict.get("name", "?")

    _slog(f"[stamp] {app_id} ({app_name})")

    # v7-arc Instances carry identity via provenance.spec_id; the legacy
    # top-level pkg_id is a v6-era field that has no meaning here. Earlier
    # versions of this pass unconditionally minted a fresh pkg_id onto
    # every manifest without one, polluting v7-arc Instances with a
    # ghost id that didn't resolve to any Spec and broke downstream
    # coherence checks (the "p-ab0a2ed8 with no matching Spec" pattern
    # observed pod-wide 2026-06-09). For v7-arc shapes, skip the entire
    # stamp pass — file_id/marker registration on Instances goes through
    # the realized_files writer, not this legacy path.
    if manifest_dict.get("manifest_shape") == "v7-arc":
        _slog(f"[stamp]   skipped: v7-arc Instance (pkg_id/files live on Spec/realized_files)")
        return

    # 1. Assign pkg_id
    pkg_id = manifest_dict.get("pkg_id", "")
    if not pkg_id:
        pkg_id = new_pkg_id()
        manifest_dict["pkg_id"] = pkg_id
        changed = True
        _slog(f"[stamp]   pkg_id assigned: {pkg_id}")
    else:
        _slog(f"[stamp]   pkg_id: {pkg_id}")

    # 2. Set source/source_detail
    if manifest_dict.get("source") in ("", "detected", "llm-inferred", None):
        manifest_dict["source"] = MANIFEST_SOURCE_DISCOVERED
        changed = True
    if not manifest_dict.get("source_detail"):
        manifest_dict["source_detail"] = f"scan:{now_str}"
        changed = True

    # v27 born-status: a scanner discovery is born "discovered" (churnable
    # draft; §9). setdefault, NOT assignment — this stamp pass re-runs over
    # existing manifests, and an operator may have PROMOTED one to "defined";
    # overwriting here would silently un-promote it every scan. Absent only
    # (= a genuinely fresh discovery) gets the default.
    if not manifest_dict.get("definition_status"):
        manifest_dict["definition_status"] = MANIFEST_DEFINITION_DISCOVERED
        changed = True

    # 3. Resolve evidence_files → real paths
    bot_id = manifest_dict.get("bot_id", "")
    evidence_files: list[str] = manifest_dict.get("evidence_files", []) or []
    _slog(f"[stamp]   evidence_files ({len(evidence_files)}): {evidence_files[:8]}")
    # exists_or_unreachable: a bare .exists() in this debug line would itself
    # RAISE under a 0700 ACL-mask clamp (Py3.12) and crash the stamp pass.
    _slog(f"[stamp]   workspace: {workspace}  exists={exists_or_unreachable(workspace)}")

    # Extension → layer inference
    _ext_layer: dict[str, str] = {
        ".py":   "script",
        ".sh":   "script",
        ".bash": "script",
        ".zsh":  "script",
    }
    _data_exts = {".json", ".jsonl"}
    _ref_exts  = {".md", ".markdown", ".txt", ".rst"}

    def _infer_layer(path: Path) -> str:
        sfx = path.suffix.lower()
        if sfx in _ext_layer:
            return _ext_layer[sfx]
        if sfx in _data_exts:
            return "data"
        if sfx in _ref_exts:
            return "state"   # md files in workspace are usually living state
        return "reference"

    # Reserved workspace subdirs whose contents are scanner/Evolve state
    # rather than app implementation files. When evidence_files lists one
    # of these as a directory hint, the dir-expansion below skips it. If
    # an app legitimately *did* write its own data inside one of these
    # paths (rare), it would be listed as an explicit file evidence, not
    # a directory hint — that still gets registered.
    #
    # Without this guard, an app whose evidence includes "directory:
    # manifests/" (as the LLM clustering sometimes emits for self-aware
    # apps) ends up claiming every other manifest JSON as one of its
    # own files. Symptom seen on security_bot post-2026-05-15 scan:
    # heartbeat_monitoring claimed 8 unrelated manifest files.
    _STAMP_SKIP_DIRS = frozenset({"manifests", ".openclaw", ".git"})

    # Filenames that are project boilerplate or scratch templates rather
    # than per-app data. When the LLM emits "directory: home/" as evidence,
    # expansion walks every file in home/ — without this filter, scratch
    # files like gitignore-template.md ended up claimed by Home Repairs Log
    # on admin_bot (2026-05-27 observation).
    _STAMP_SKIP_FILE_NAMES = frozenset({
        ".gitignore", ".gitattributes", ".gitkeep", ".keep",
        "readme.md", "readme.txt", "readme",
        "license", "license.md", "license.txt",
        "changelog.md", "contributing.md", "code_of_conduct.md",
    })
    _STAMP_SKIP_FILE_PATTERNS = (
        "-template.md", "-template.txt", "-template.json",
        "template-", ".template.",
        ".example", ".sample",
    )

    def _looks_like_template(name: str) -> bool:
        lower = name.lower()
        if lower in _STAMP_SKIP_FILE_NAMES:
            return True
        return any(pat in lower for pat in _STAMP_SKIP_FILE_PATTERNS)

    # Collect candidate paths from evidence_files
    candidate_paths: list[Path] = []
    for ev in evidence_files:
        # Strip leading type tag, e.g. "directory: ops/" → "ops/"
        # Use rstrip only — leading slash on absolute paths must be preserved
        clean = _re.sub(r'^[a-z_]+:\s*', '', str(ev).strip()).rstrip("/")
        if not clean:
            continue
        # AUTHORITATIVE never-ownable gate — runs FIRST, before the
        # platform-written / template heuristics below. can_app_own subsumes
        # _STAMP_SKIP_DIRS and _is_platform_written_path: it rejects the whole
        # evolve/ telemetry tree (audit_outbox rec-*.json), the manifests store,
        # .openclaw/.git, and OC-standard files (AGENTS.md, …). This is what
        # stops Phase 5 from minting file_ids + embedding _evolve markers into
        # platform telemetry when LLM evidence points at it — whether the
        # evidence is a direct file or a "directory:" hint over a reserved tree.
        if not can_app_own(clean, name=Path(clean).name):
            _slog(f"[stamp]   ev '{ev}' ✗ skipped (never-ownable per ownership policy)")
            continue
        # L2 platform-files defense (now largely subsumed by can_app_own; kept
        # as a cheap, explicit secondary guard): never register a file matching a
        # known pod-wide infrastructure writer pattern. See
        # PLATFORM_WRITTEN_FILE_PATTERNS.
        if _is_platform_written_path(clean):
            _slog(f"[stamp]   ev '{ev}' ✗ skipped (platform-written infra file)")
            continue
        # Absolute paths are used directly; relative paths are resolved from workspace
        p = Path(clean) if Path(clean).is_absolute() else workspace / clean
        # is_file()/is_dir() RAISE PermissionError under a 0700 ACL-mask clamp
        # (Py3.12) — guard the type-probe so one unreachable evidence path is
        # skipped+logged, not a crash of the whole stamp pass.
        try:
            p_is_file = p.is_file()
            p_is_dir = p.is_dir() if not p_is_file else False
        except OSError as e:
            _slog(f"[stamp]   ev '{ev}' → {p} ✗ unreachable (EACCES clamp?): {e}")
            continue
        if p_is_file:
            # Never-ownable already filtered at the top of the loop (can_app_own
            # on the cleaned evidence string).
            candidate_paths.append(p)
            _slog(f"[stamp]   ev '{ev}' → {p} ✓ file")
        elif p_is_dir:
            # Guard against scanner-state directories — see _STAMP_SKIP_DIRS above.
            if p.name in _STAMP_SKIP_DIRS:
                _slog(f"[stamp]   ev '{ev}' → {p} ✗ skipped (reserved scanner dir)")
                continue
            # Register files directly inside the directory (one level, non-recursive).
            # Template/boilerplate files are excluded so a "directory: home/" hint
            # doesn't drag e.g. gitignore-template.md into an app's owned files.
            # Platform-written files are also excluded — same intent as the
            # _is_platform_written_path guard above, applied here for the
            # directory-expansion case.
            try:
                children: list[Path] = []
                skipped: list[str] = []
                platform_skipped: list[str] = []
                policy_skipped: list[str] = []
                for c in sorted(p.iterdir()):
                    if not c.is_file() or c.name.startswith("."):
                        continue
                    if _looks_like_template(c.name):
                        skipped.append(c.name)
                        continue
                    try:
                        rel = c.relative_to(workspace)
                    except ValueError:
                        rel = Path(c.name)
                    # Never-ownable gate first — a "directory: evolve/audit_outbox/…"
                    # hint must NOT drag its rec-*.json children into the app's
                    # owned files (the ~1,000-false-orphan bug).
                    if not can_app_own(str(rel), name=c.name):
                        policy_skipped.append(c.name)
                        continue
                    if _is_platform_written_path(str(rel)):
                        platform_skipped.append(c.name)
                        continue
                    children.append(c)
                candidate_paths.extend(children)
                notes: list[str] = []
                if skipped:
                    notes.append(f"skipped templates: {', '.join(skipped)}")
                if platform_skipped:
                    notes.append(f"skipped platform files: {', '.join(platform_skipped[:3])}")
                if policy_skipped:
                    notes.append(
                        f"skipped never-ownable: {', '.join(policy_skipped[:3])}"
                        + (f" (+{len(policy_skipped) - 3} more)" if len(policy_skipped) > 3 else "")
                    )
                note = f" ({'; '.join(notes)})" if notes else ""
                _slog(f"[stamp]   ev '{ev}' → {p} ✓ dir ({len(children)} files){note}")
            except OSError as e:
                _slog(f"[stamp]   ev '{ev}' → {p} ✗ dir read error: {e}")
        else:
            _slog(f"[stamp]   ev '{ev}' → {p} ✗ not found")

    # Also include cron scripts associated with this manifest
    cron_entries: list = manifest_dict.get("crons") or []
    for cron in cron_entries:
        script = cron.get("script", "") if isinstance(cron, dict) else ""
        if script:
            cp = workspace / script if not Path(script).is_absolute() else Path(script)
            # is_file() RAISES under a 0700 ACL-mask clamp (Py3.12) — guard so an
            # unreachable cron script is skipped+logged, not a crash.
            try:
                cp_is_file = cp.is_file()
            except OSError as e:
                _slog(f"[stamp]   cron script: {cp} ✗ unreachable (EACCES clamp?): {e}")
                cp_is_file = False
            if cp_is_file and cp not in candidate_paths:
                candidate_paths.append(cp)
                _slog(f"[stamp]   cron script: {cp} ✓")

    # Authoritative never-ownable gate (belt-and-suspenders): drop ANY remaining
    # candidate the ownership policy rejects, regardless of how it entered the
    # list — direct-file evidence, dir-expansion child, or a cron script (which
    # bypasses the per-branch evidence checks above). This is the single point
    # that guarantees a never-ownable path is NEITHER registered into the
    # manifest files list NOR physically stamped with a marker.
    if candidate_paths:
        kept: list[Path] = []
        for cp in candidate_paths:
            try:
                cp_rel = str(cp.relative_to(workspace))
            except ValueError:
                cp_rel = str(cp)
            if not can_app_own(cp_rel, name=Path(cp_rel).name):
                _slog(f"[stamp]   {cp_rel} ✗ skipped (never-ownable per ownership policy)")
                continue
            kept.append(cp)
        candidate_paths = kept

    _slog(f"[stamp]   candidate_paths: {len(candidate_paths)} file(s)")

    if not candidate_paths:
        _slog(f"[stamp]   → no candidates, skipping file registration")
        if changed:
            _atomic_write(manifest_path, manifest_dict, mode=0o644)
        return

    # 4. Build v5 files list, embedding markers
    existing_files: list[dict] = []
    for entry in (manifest_dict.get("files") or []):
        if isinstance(entry, dict) and entry.get("file_id"):
            existing_files.append(entry)
    # Canonicalize both join sides to the workspace-relative key (F-C1): an
    # existing entry stored as an absolute path (extend_application) must still
    # match the workspace-relative ``rel`` of a re-discovered candidate, else a
    # fresh file_id is minted and the file is stamped a SECOND time.
    existing_paths = {ws_rel_key(e["path"], workspace) for e in existing_files}
    _slog(f"[stamp]   existing registered files: {len(existing_files)}")

    pkg_version = manifest_dict.get("pkg_version", "")
    file_date = calver_today()

    new_entries: list[dict] = list(existing_files)

    for fpath in candidate_paths:
        try:
            rel = str(fpath.relative_to(workspace))
        except ValueError:
            rel = str(fpath)

        if ws_rel_key(rel, workspace) in existing_paths:
            # File already registered — just ensure its marker has our pkg_id
            try:
                embed_marker(fpath, pkg_ids=[pkg_id], file_id="", merge=True)
            except Exception as e:
                print(f"[scanner] stamp: embed_marker skipped for {rel}: {e}", flush=True)
            continue

        # Assign a new file_id
        file_id = new_file_id()
        layer   = _infer_layer(fpath)
        fver    = f"{file_date}.1"

        entry: dict = {
            "file_id":             file_id,
            "path":                rel,
            "layer":               layer,
            "purpose":             "",        # RSI loop fills this in on first improvement
            "owned_by":            pkg_id,
            "shared_with":         [],
            "file_version":        fver,
            "created_at":          now_str,
            "modified_at":         now_str,
        }
        if pkg_version:
            entry["created_in_run"] = pkg_version

        # Always register the file in the manifest — marker embedding is best-effort.
        # The evolve service user may not have write access to bot workspace files,
        # so we decouple manifest registration from physical marker embedding.
        new_entries.append(entry)
        existing_paths.add(rel)
        _slog(f"[stamp]   + {rel}  layer={layer}  file_id={file_id}")

        try:
            embed_marker(
                fpath,
                pkg_ids=[pkg_id],
                file_id=file_id,
                pkg_versions={pkg_id: pkg_version} if pkg_version else None,
                file_version=fver,
                merge=True,
            )
        except Exception as e:
            _slog(f"[stamp]   embed_marker failed (non-fatal) for {rel}: {e}")

    new_registered = len(new_entries) - len(existing_files)
    if new_entries != existing_files:
        manifest_dict["files"] = new_entries
        changed = True

    if changed:
        manifest_dict["updated_at"] = now_str
        _atomic_write(manifest_path, manifest_dict, mode=0o644)
        _slog(
            f"[stamp]   manifest saved: {new_registered} new file(s), "
            f"{len(new_entries)} total  pkg_id={pkg_id}"
        )
    else:
        _slog(f"[stamp]   no changes")


def _stub_manifest(app: DetectedApplication, bot_id: str) -> dict:
    """Minimal manifest when LLM is disabled. Uses DetectedApplication fields."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    purpose = f"This application exists to help {bot_id} with {app.description or app.name}."
    description = app.description or _derive_description_from_purpose(purpose, app.name)
    return {
        "id": app.id,
        "name": app.name,
        "bot_id": bot_id,
        "description": description,
        "source": app.source,
        "confidence": app.confidence,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "schema_version": 3,
        "manifest_type": "evolve_application",
        "evidence_files": app.evidence_files,
        "identity": {
            "purpose": f"This application exists to help {bot_id} with {app.description or app.name}.",
            "scope_includes": app.suggested_goals,
            "scope_excludes": [],
            "user": bot_id,
        },
        "success_criteria": {
            "observable_outcomes": [],
            "failure_signals": app.suggested_tests,
            "quality_bar": {"minimum": "", "excellent": ""},
        },
        "constraints": {
            "privacy": app.suggested_privacy,
            "safety": [],
            "dependencies": app.evidence_files[:6],
            "boundaries": [],
        },
        "example_triggers": [],
        "test_cases": [],
        "satisfaction": {"score": None, "notes": None, "rated_at": None},
        "improvement_history": [],
    }


def _derive_description_from_purpose(purpose: str, name: str = "") -> str:
    """Derive a manifest description from identity.purpose when description is missing."""
    import re as _re
    if purpose:
        cleaned = _re.sub(r"^This application exists to\s+", "", purpose, flags=_re.IGNORECASE)
        cleaned = cleaned.strip()
        if cleaned:
            cleaned = cleaned[0].upper() + cleaned[1:]
            if not cleaned.endswith("."):
                cleaned += "."
            return cleaned
    return name


# ── Discoverability backfill (Pass D) ────────────────────────────────────────
#
# Mirror app_audit_structural.{_DISCOVERABILITY_MIN_HINT_WORDS,
# _USER_ROUTED_MODELS, _manifest_usage_block, _manifest_hint_words}. Keeping
# the constants here (vs. importing) avoids a scanner→analyzer dependency
# that doesn't currently exist and that the scanner runs as the bot user
# where the analyzer path is not always present.
#
# When the audit's `_DISCOVERABILITY_MIN_HINT_WORDS` is retuned, mirror it
# here. The structural verifier remains the source of truth — Pass D is
# best-effort repair, not the gate.
_DISCOVERABILITY_HINT_WORDS_MIN = 3
_RENDERER_HINT_WORDS_CAP = 12


def _manifest_usage_block_for_backfill(manifest: dict) -> dict:
    """Top-level usage block, tolerating identity-nested placement."""
    top = manifest.get("usage")
    if isinstance(top, dict):
        return top
    identity = manifest.get("identity")
    if isinstance(identity, dict):
        nested = identity.get("usage")
        if isinstance(nested, dict):
            return nested
    return {}


def _infer_usage_model_from_manifest(manifest: dict) -> str:
    """Pick a ``usage.model`` value from a manifest dict.

    Thin wrapper that pulls the relevant fields out of the dict and
    delegates to the keyword-args ``_infer_usage_model`` so the
    classification logic stays in one place. Used by Phase 4.5
    mechanical repair to backfill model on existing manifests.

    Returns ``""`` when the manifest is unusable; callers skip the write.
    """
    if not isinstance(manifest, dict):
        return ""
    return _infer_usage_model(
        scheduled_actions=manifest.get("scheduled_actions"),
        heartbeat_evidence=manifest.get("heartbeat_evidence"),
        crons=manifest.get("crons"),
        cron_evidence=manifest.get("cron_evidence"),
        description=manifest.get("description") or "",
        example_triggers=manifest.get("example_triggers"),
        success_criteria=manifest.get("success_criteria"),
        identity=manifest.get("identity"),
    )


def _hint_words_union(manifest: dict) -> list[str]:
    """Union of explicit hint_words + capability_tags + session_keywords,
    in that priority order, deduped while preserving first occurrence.
    Mirrors app_audit_structural._manifest_hint_words on the raw dict.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(words):
        if not isinstance(words, list):
            return
        for w in words:
            if isinstance(w, str):
                s = w.strip()
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)

    usage = _manifest_usage_block_for_backfill(manifest)
    tr = usage.get("trigger_recognition") if isinstance(usage, dict) else None
    if isinstance(tr, dict):
        _add(tr.get("hint_words"))
    _add(manifest.get("capability_tags"))
    _add(manifest.get("session_keywords"))
    return out


def _apply_discoverability_backfill(data: dict) -> bool:
    """Pass D — fill empty ``usage.model`` and explicit ``hint_words``.

    Conservative ("only-if-empty"). Returns True iff anything was written.
    No-ops when the model can't be inferred or the hint union is too thin.

    Specifically:
      * ``usage.model``: written when missing/blank and ``_infer_usage_model``
        returns a non-empty value (always true for valid dict input — the
        fallback is ``"user-initiated"``).
      * ``usage.trigger_recognition.hint_words``: materialized from
        ``_hint_words_union(data)`` when explicit hints are empty AND the
        union meets ``_DISCOVERABILITY_HINT_WORDS_MIN``. Capped at the
        renderer's 12-word ceiling.
    """
    if not isinstance(data, dict):
        return False
    changed = False

    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}

    # usage.model — write when missing/blank
    model = usage.get("model")
    if not isinstance(model, str) or not model.strip():
        inferred = _infer_usage_model_from_manifest(data)
        if inferred:
            usage["model"] = inferred
            changed = True

    # usage.trigger_recognition.hint_words — materialize union when explicit is empty
    tr = usage.get("trigger_recognition")
    if not isinstance(tr, dict):
        tr = {}
    explicit = tr.get("hint_words")
    explicit_clean = []
    if isinstance(explicit, list):
        explicit_clean = [w for w in explicit if isinstance(w, str) and w.strip()]
    if not explicit_clean:
        union = _hint_words_union(data)
        if len(union) >= _DISCOVERABILITY_HINT_WORDS_MIN:
            tr["hint_words"] = union[:_RENDERER_HINT_WORDS_CAP]
            usage["trigger_recognition"] = tr
            changed = True

    if changed:
        data["usage"] = usage
    return changed


def _infer_permissions_block(manifest_dict: dict) -> dict:
    """Build a ``permissions:`` block from the reconciler's inferred entries.

    Pure-Python: reads files/realized_files + crons[] off the manifest dict
    and emits ``exec`` + ``crons`` lists. Phase A keeps fs_read / fs_write
    / network_egress / env operator-only — those are never auto-inferred.
    Returns ``{}`` when there's nothing to seed (caller should skip the
    write in that case).

    Lazily imports ``_entries_for_app`` to avoid a circular import at
    module load (reconciler → applications → scanner).
    """
    from ..app_permissions.reconciler import _entries_for_app

    try:
        entries = _entries_for_app(manifest_dict)
    except Exception:
        return {}

    exec_patterns: list[str] = []
    cron_lines: list[str] = []
    seen_exec: set[str] = set()
    seen_cron: set[str] = set()
    for entry in entries:
        kind = getattr(entry, "kind", None)
        pattern = getattr(entry, "pattern", "") or ""
        if not pattern:
            continue
        if kind == "exec":
            if pattern in seen_exec:
                continue
            seen_exec.add(pattern)
            exec_patterns.append(pattern)
        elif kind == "cron":
            if pattern in seen_cron:
                continue
            seen_cron.add(pattern)
            cron_lines.append(pattern)

    block: dict = {}
    if exec_patterns:
        block["exec"] = sorted(exec_patterns)
    if cron_lines:
        block["crons"] = sorted(cron_lines)
    return block


def _excerpt(path: Path, max_chars: int = 300) -> str:
    try:
        return path.read_text()[:max_chars]
    except OSError:
        return ""


# ── Compliance scan ───────────────────────────────────────────────────────────

# Signal emission producer name. Kept as a module-level constant so the
# generator (generators/manifest_quality, workspace_inventory,
# workspace_security) can filter active signals by this producer.
_COMPLIANCE_PRODUCER = "compliance_scan"


def _compliance_signal_severity(issue: dict) -> str:
    """Map issue severity to Signal severity.

    Scan severities are ``error`` / ``warning`` / ``info``; Signal
    severities are ``alert`` / ``warn`` / ``info``. ``error`` → alert,
    ``warning`` → warn, ``info`` → info. The info tier was added in the
    2026-06-04 quality-control pass so hygiene findings (missing
    description, etc.) don't crowd the Alerts page.
    """
    sev = (issue.get("severity") or "").lower()
    if sev == "error":
        return "alert"
    if sev == "warning":
        return "warn"
    return "info"


def _compliance_signal_item(issue: dict) -> dict:
    """Distill a raw issue into a per-item dict for the rollup signal.

    Only includes fields actually carried by this issue_type so the
    items list stays tight (no ``"path": None`` noise on app-scoped
    issues).
    """
    item: dict = {"message": issue.get("message") or ""}
    for k in ("app_id", "path", "cron", "principle_violation"):
        v = issue.get(k)
        if v is not None:
            item[k] = v
    return item


def _emit_compliance_signals(
    shared_dir: Path, bot_id: str, issues: list[dict]
) -> set[str]:
    """Roll up issues by issue_type and emit ONE Signal per (bot, issue_type).

    Reduces Alerts-page noise: a bot with 41 ``missing_required_field``
    findings produces 1 Signal carrying ``details.items=[{app_id, message},
    ...]``, not 41 separate Signals. Consumer generators
    (manifest_quality, workspace_inventory, workspace_security) iterate
    ``details.items`` to emit per-item Proposals.

    Returns kept_signatures for sweep_resolve — one entry per emitted
    signal. Best-effort: a Signal-write failure for one issue_type is
    logged and the rest of the run continues.
    """
    try:
        from schema.signal import make_signature
        from signals import store as signals_store
    except ImportError:
        return set()

    by_type: dict[str, list[dict]] = {}
    for issue in issues:
        by_type.setdefault(issue.get("issue_type") or "unknown", []).append(issue)

    kept: set[str] = set()
    for issue_type, group in by_type.items():
        signature = make_signature(_COMPLIANCE_PRODUCER, issue_type, bot_id)
        kept.add(signature)

        items = [_compliance_signal_item(i) for i in group]
        # Aggregate severity: worst-case dominates. ``error`` → alert,
        # ``warning`` → warn, all-``info`` (or unrecognized) → info. Pre-
        # 2026-06-04 this collapsed to alert-vs-warn only, which forced
        # hygiene items (missing description, etc.) to fire at warn — the
        # quality-control pass added the info tier so those rollups can
        # land off the Alerts page entirely.
        sev_set = {(i.get("severity") or "").lower() for i in group}
        if "error" in sev_set:
            severity = "alert"
        elif "warning" in sev_set:
            severity = "warn"
        else:
            severity = "info"

        n = len(items)
        if n == 1:
            title = group[0].get("message") or f"{bot_id}: {issue_type}"
            body = group[0].get("message") or ""
        else:
            title = f"{bot_id}: {n} {issue_type} issues"
            preview_lines = [
                f"- {i.get('message') or ''}" for i in group[:5]
            ]
            if n > 5:
                preview_lines.append(f"…and {n - 5} more")
            body = "\n".join(preview_lines)

        details = {
            "bot_id": bot_id,
            "issue_type": issue_type,
            "items": items,
            "item_count": n,
        }
        try:
            signals_store.observe(
                shared_dir,
                signature=signature,
                producer=_COMPLIANCE_PRODUCER,
                type=issue_type,
                flavor="maintenance",
                severity=severity,
                scope="bot",
                bot_id=bot_id,
                title=title[:160],
                body=body[:600],
                details=details,
            )
        except Exception as exc:  # noqa: BLE001 — never break the scan on signal IO
            print(
                f"[scan_compliance] signal observe failed for {bot_id} {issue_type}: {exc}",
                flush=True,
            )
    return kept


def scan_compliance(bot_id: str, shared_dir: Path) -> dict[str, Any]:
    """Check manifest compliance for a single bot.

    Returns a dict with:
      - bot_id: str
      - registered: list of manifest ids that are compliant
      - issues: list of issue dicts, each with keys:
          app_id, issue_type, severity, message
        issue_type values:
          'unregistered_script' | 'unregistered_cron' | 'stale' |
          'test_failing' | 'validation_error' | 'missing_required_field'
      - summary: {total_manifests, compliant, issues_count}
    """
    from datetime import date as _date
    from .manifest import list_manifests, applications_dir

    issues: list[dict] = []
    registered_ids: list[str] = []

    # ── 1. Load registered manifests ──────────────────────────────────────────
    manifests = list_manifests(shared_dir, bot_id)
    manifest_map = {m.id: m for m in manifests}

    # Build sets of all files and crons claimed by manifests
    claimed_files: set[str] = set()
    claimed_crons: set[str] = set()
    for m in manifests:
        claimed_files.update(m.file_paths())   # v5-safe: handles str or dict entries
        claimed_crons.update(m.cron_lines())   # v5-safe: handles str or dict crons

    # ── 2. Validate each registered manifest ──────────────────────────────────
    STALE_DAYS = 90
    today_str = _date.today().isoformat()
    today = _date.today()

    for m in manifests:
        app_issues: list[str] = []

        # Required fields. Severity tiers calibrated for ALERT-PAGE noise:
        # - identity fields (id, name, bot_id) and status are LOAD-BEARING —
        #   a missing one breaks downstream tooling (manifest router,
        #   workspace inventory, app-permission audit). Real "warning".
        # - description is HYGIENE — operators see it on the manifest tile
        #   and on app-permission proposals, but every downstream tool
        #   degrades gracefully. A missing description is a "nice to fix"
        #   chip on the Apps tab, not an alert-page entry. The 2026-06-03
        #   review caught this firing as a red "alert"-tier Signal across
        #   long-tail discovered apps that hadn't been auto-described yet.
        #
        # The "info" severity below maps to a warn-tier signal at most
        # in the compliance-signal rollup (see _emit_compliance_signals),
        # which doesn't escalate to alert. Net effect: missing description
        # no longer crowds the Alerts page.
        _HYGIENE_FIELDS = {"description"}
        for req in ("id", "name", "bot_id", "description", "status"):
            if not getattr(m, req, None):
                issues.append({
                    "app_id": m.id,
                    "issue_type": "missing_required_field",
                    "severity": "info" if req in _HYGIENE_FIELDS else "error",
                    "message": f"Required field '{req}' is missing or empty",
                })
                app_issues.append(req)

        # Stale check
        if m.last_reviewed:
            try:
                reviewed = _date.fromisoformat(m.last_reviewed)
                age_days = (today - reviewed).days
                if age_days > STALE_DAYS:
                    issues.append({
                        "app_id": m.id,
                        "issue_type": "stale",
                        "severity": "warning",
                        "message": f"Last reviewed {age_days} days ago (>{STALE_DAYS} day threshold)",
                    })
                    app_issues.append("stale")
            except ValueError:
                pass  # malformed date — not worth a separate issue

        # Test failure check
        if m.last_test_exit_code is not None and m.last_test_exit_code != 0:
            issues.append({
                "app_id": m.id,
                "issue_type": "test_failing",
                "severity": "error",
                "message": (
                    f"Last test run exited with code {m.last_test_exit_code}"
                    + (f" on {m.last_test_run}" if m.last_test_run else "")
                ),
            })
            app_issues.append("test_failing")

        if not app_issues:
            registered_ids.append(m.id)

    # ── 3. Check for unregistered assets in the workspace ─────────────────────
    # _get_workspace already returns None under an EACCES clamp; exists_or_unreachable
    # guards the second .exists() (a bare call RAISES on Py3.12 under a 0700 clamp).
    workspace = _get_workspace(bot_id)
    if workspace and exists_or_unreachable(workspace):
        # Unregistered scripts
        script_exts = {".py", ".sh", ".rb", ".js", ".ts"}
        infra_keywords = {"evolve", "openclaw", "heal", "measure", "analyze", "report"}

        # F-C1: canonicalize the claimed / suppressed file sets to the
        # workspace-relative key so a manifest that stored an ABSOLUTE path
        # (extend_application) still matches the workspace-relative disk path —
        # else a genuinely-registered script mis-fires `unregistered_script`
        # (the #3303-class "two sides, different canonical form → join miss").
        claimed_keys = {ws_rel_key(p, workspace) for p in claimed_files}
        suppressed_keys = {
            ws_rel_key(p, workspace)
            for m in manifests if m.compliance_suppressed
            for p in m.file_paths()
        }

        # The rglob walk itself is the EACCES point: a clamped subdir makes the
        # generator RAISE mid-iteration (Py3.12). Wrap so we keep whatever was
        # found before the clamp and LOG it, rather than crashing scan_compliance.
        try:
            for script_path in workspace.rglob("*"):
                if not script_path.is_file():
                    continue
                if script_path.suffix not in script_exts:
                    continue
                # Skip hidden dirs and evolve infrastructure paths
                parts = script_path.parts
                if any(p.startswith(".") for p in parts):
                    continue
                if any(kw in str(script_path).lower() for kw in infra_keywords):
                    continue

                rel = str(script_path.relative_to(workspace))
                rel_key = ws_rel_key(rel, workspace)
                if rel_key not in claimed_keys:
                    # Check if any manifest's compliance_suppressed covers this
                    suppressed = rel_key in suppressed_keys
                    if not suppressed:
                        issues.append({
                            "app_id": None,
                            "issue_type": "unregistered_script",
                            "severity": "warning",
                            "message": f"Script has no registered manifest: {rel}",
                            "path": rel,
                        })
        except (PermissionError, OSError) as e:
            _log.warning("scan_compliance: script walk of %s unreachable (EACCES clamp?): %s", workspace, e)

        # Unregistered crons
        try:
            r = subprocess.run(
                ["sudo", "-u", get_bot_user(bot_id, load_network()), "crontab", "-l"],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # Skip infrastructure crons
                    if any(kw in line.lower() for kw in infra_keywords):
                        continue
                    # Match: exact line, OR claimed entry is substring of live line
                    # (handles "python3 /path/script.py" vs "/path/script.py"), OR
                    # the script path token alone appears in the live line
                    # (handles different interpreter paths, e.g. /usr/bin/python3 vs python3)
                    _line_scripts = {p for p in line.split() if p.endswith((".py", ".sh"))}
                    if line not in claimed_crons \
                            and not any(cl and cl in line for cl in claimed_crons) \
                            and not any(
                                cl_script in _line_scripts
                                for cl in claimed_crons
                                for cl_script in [cl.split()[-1]] if cl_script.endswith((".py", ".sh"))
                            ):
                        issues.append({
                            "app_id": None,
                            "issue_type": "unregistered_cron",
                            "severity": "warning",
                            "message": f"Cron entry has no registered manifest: {line[:120]}",
                            "cron": line,
                        })
        except Exception:
            pass  # crontab not accessible — skip

        # ── 4. Workspace secrets scan ─────────────────────────────────────────
        # Uses direct ACL-based reads — no sudo needed (evolve has ACL on .openclaw/).
        import re as _re
        _SEC_PATS = [
            (_re.compile(r"ghp_[A-Za-z0-9]{36}"),          "GitHub PAT (classic)"),
            (_re.compile(r"github_pat_[A-Za-z0-9_]{82}"),   "GitHub PAT (fine-grained)"),
            (_re.compile(r"sk-ant-[A-Za-z0-9\-_]{80,}"),    "Anthropic API key"),
            (_re.compile(r"sk-proj-[A-Za-z0-9\-_]{40,}"),   "OpenAI project key"),
            (_re.compile(r"xai-[A-Za-z0-9]{40,}"),          "xAI API key"),
            (_re.compile(r"\d{8,12}:[A-Za-z0-9_\-]{35}"),   "Telegram bot token"),
        ]
        _ALLOWED_BASENAMES = {"auth-profiles.json", "openclaw.json"}
        _SCAN_EXTS = {".md", ".txt", ".env", ".sh", ".py", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini"}
        # Build a {workspace-relative path → owning app_id} index from installed
        # manifests so misplaced_secret findings at a declared api_key_source
        # path get tagged with `principle_violation: apps_inherit_bot_llm`. This
        # is the structural-issue overlay on top of the generic secrets scan:
        # the key is a stray secret AND the app violates docs/principle-apps-inherit-bot-llm.md.
        _api_key_source_index: dict[str, str] = {}
        for _m in manifests:
            try:
                _raw = getattr(_m, "raw", None) or {}
                _rllm = _raw.get("recursive_llm") or {}
                _aks = _rllm.get("api_key_source") if isinstance(_rllm, dict) else None
                if isinstance(_aks, str) and _aks:
                    # F-C1: key by the canonical workspace-relative form so a
                    # declared api_key_source recorded as an absolute path still
                    # matches the workspace-relative disk path of the secret —
                    # else owner attribution (and the apps_inherit_bot_llm
                    # principle overlay) is silently dropped.
                    _api_key_source_index[ws_rel_key(_aks, workspace)] = _m.id
            except Exception:
                continue

        # The per-file body below guards reads, but the rglob ITERATION itself
        # RAISES under a 0700 ACL-mask clamp (Py3.12) — and that raise is outside
        # the body's try. Materialize the walk up-front under a guard so a clamp
        # is logged + skipped, never crashing scan_compliance.
        try:
            _secrets_walk = list(workspace.rglob("*"))
        except (PermissionError, OSError) as e:
            _log.warning("scan_compliance: secrets walk of %s unreachable (EACCES clamp?): %s", workspace, e)
            _secrets_walk = []
        for _fp in _secrets_walk:
            try:
                if not _fp.is_file():
                    continue
                if _fp.suffix not in _SCAN_EXTS or _fp.name in _ALLOWED_BASENAMES:
                    continue
                if ".git" in _fp.parts or "node_modules" in _fp.parts:
                    continue
                _content = _fp.read_text(errors="replace")
            except (PermissionError, OSError):
                continue
            except Exception:
                continue
            for _pat, _label in _SEC_PATS:
                if _pat.search(_content):
                    _rel = str(_fp.relative_to(workspace))
                    _owning_app = _api_key_source_index.get(ws_rel_key(_rel, workspace))
                    _issue: dict = {
                        "app_id": _owning_app,
                        "issue_type": "misplaced_secret",
                        "severity": "error",
                        "message": f"{_label} found in workspace file: {_rel}",
                        "path": _rel,
                    }
                    if _owning_app:
                        # The credential isn't stray — the app's manifest
                        # declares it. That makes this a principle violation:
                        # the app credentials itself instead of routing LLM
                        # calls through the bot's gateway. The remediation
                        # is rearchitect, not delete the key.
                        _issue["principle_violation"] = "apps_inherit_bot_llm"
                        _issue["message"] = (
                            f"{_label} found at {_rel} — declared as "
                            f"recursive_llm.api_key_source by app {_owning_app}. "
                            f"App needs rearchitect per "
                            f"docs/spec-apps-inherit-bot-llm-2026-06-06.md "
                            f"(route LLM calls through the bot's gateway instead "
                            f"of carrying a per-app credential)."
                        )
                    issues.append(_issue)
                    break

    compliant = len(registered_ids)
    result = {
        "bot_id": bot_id,
        "registered": registered_ids,
        "issues": issues,
        "summary": {
            "total_manifests": len(manifests),
            "compliant": compliant,
            "issues_count": len(issues),
        },
    }

    # Emit one Signal per issue. The canonical Signals → Proposals
    # pipeline (generators/manifest_quality, workspace_inventory,
    # workspace_security) consumes these on the next refresh tick.
    # The legacy ``{shared_dir}/better-engine/cache/compliance-{bot}.json``
    # cache file is no longer written — ComplianceAdapter was retired
    # alongside this migration; nothing else consumed the file.
    kept = _emit_compliance_signals(shared_dir, bot_id, issues)
    result["_signal_signatures"] = sorted(kept)

    return result


def scan_compliance_all(shared_dir: Path, bot_ids: list[str]) -> dict[str, Any]:
    """Run compliance scan across all bots. Returns per-bot results and a fleet summary."""
    results = {}
    total_issues = 0
    all_kept: set[str] = set()
    for bot_id in bot_ids:
        r = scan_compliance(bot_id, shared_dir)
        results[bot_id] = r
        total_issues += r["summary"]["issues_count"]
        all_kept.update(r.get("_signal_signatures") or [])

    # Auto-resolve Signals for compliance issues that didn't re-fire on
    # this pass. ``sweep_resolve`` only touches signals owned by the
    # ``compliance_scan`` producer, so other producers' signals are
    # untouched. The kept set is the union across every bot we just
    # scanned — anything in the firing state from this producer that
    # is NOT in the set has cleared.
    try:
        from signals import store as _signals_store
        _signals_store.sweep_resolve(
            shared_dir,
            producer=_COMPLIANCE_PRODUCER,
            kept_signatures=all_kept,
            reason="auto-resolve: compliance issue cleared on next scan",
        )
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup
        print(f"[scan_compliance_all] sweep_resolve failed: {exc}", flush=True)

    return {
        "bots": results,
        "fleet_summary": {
            "total_bots": len(bot_ids),
            "total_issues": total_issues,
            "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }


def _get_workspace(bot_id: str) -> Path | None:
    """Resolve a bot's workspace path from its openclaw.json."""
    try:
        from evolve_admin.config import get_bot_workspace
        return get_bot_workspace(bot_id)
    except Exception:
        # Fallback to conventional path
        from evolve_admin.config import bot_home as _bh
        ws = _bh(bot_id) / ".openclaw" / "workspace"
        try:
            return ws if ws.exists() else None
        except OSError:
            # EACCES: the OC gateway 0700-clamped .openclaw's ACL mask so evolve
            # can't traverse it (Py3.12 RAISES here; 3.11 returned False). Skip the
            # scan this pass rather than crash — the mask self-heals on the next
            # deploy / ensure_pod_perms tick and the scanner re-runs. (Chokepoint
            # guard; the per-walk inventory sites are tracked as a follow-up sweep.)
            return None


# Legacy: kept for any callers that import inventory_workspace directly
def inventory_workspace(workspace: Path) -> dict[str, Any]:
    """Legacy wrapper — use collect_inventory() for new code."""
    inv = collect_inventory(workspace, "unknown")
    files = (
        [{"path": s, "size": 0, "preview": ""} for s in inv.python_scripts + inv.shell_scripts]
        + inv.markdown_files
    )
    top_dirs = sorted({d.split("/")[0] for d in inv.directories if d})
    return {
        "directories": inv.directories[:50],
        "key_files": files[:40],
        "structure_summary": ", ".join(top_dirs[:10]) or "(empty workspace)",
    }
