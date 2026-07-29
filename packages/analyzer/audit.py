#!/usr/bin/env python3
"""
audit.py — Evolve Security Audit (Security Protocol v2, Layer 3)

Runs every 15 minutes as the `evolve` user. Performs four categories of checks:

  1. Identity audit  — SHA256 SOUL.md/AGENTS.md/HEARTBEAT.md vs git backup baseline;
                       EMAIL_WHITELIST.md / EMAIL_POLICY.md mode-0444 enforcement
  2. Config audit    — gateway.bind, port, exec allowlist, unexpected plugins, sudoers
  3. Machine audit   — firewall, FileVault, admin-user gateway, SSH config, macOS updates,
                       listening ports, user accounts, OC binary mtime
  4. Proposal audit  — volume spike detection, consecutive rollback detection

Alert tiers:
  🔴 CRITICAL → immediate alert via the standard alerts channel (network.json alerts section)
  🟡 WARN     → logged; surfaced in weekly review (no immediate alert)
  ✅ OK       → single log line, silent

Run as: evolve user
Schedule: every 15 minutes (StartInterval 900, installed by deploy.py)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evolve_config import (
    load_config, get_shared_dir, get_members, get_alerts,
    resolve_network_path, CANONICAL_SHARED_DIR,
    bot_home as _bot_home, get_bot_user,
)
from heal import _is_gateway_proc_line
from platform_profile import get_profile

logger = logging.getLogger(__name__)

AUDIT_VERSION = "1.0.0"


# ── Policy acceptances ────────────────────────────────────────────────────────
#
# Some legitimate audit checks fire on conditions the operator has
# deliberately accepted — e.g. FileVault intentionally off on a
# physically-controlled single-tenant dev mini, or pending macOS
# updates being deferred during a release freeze. Without an explicit
# acceptance mechanism, those checks fire forever and crowd the
# Alerts page with noise the operator has already triaged.
#
# The ``policy_acceptances`` block on ``network.json`` records those
# operator decisions. Format:
#
#   "policy_acceptances": {
#     "machine.filevault_off": {
#       "reason": "Single-tenant dev mini in locked room; theft surface is low",
#       "accepted_at": "2026-06-04",
#       "accepted_by": "pod-admin"
#     }
#   }
#
# Producer code calls ``policy_acceptance(check_id, config)``; if the
# check is accepted, the helper returns the entry (for logging) and
# the producer drops the finding to ``ok`` level (or skips emission).
# A future audit-trail surface (Settings → Accepted findings) reads
# the same block.
#
# Accepted check IDs (today):
#   machine.filevault_off          — Evolve-side FileVault check
#   machine.macos_updates_pending  — Evolve-side macOS-updates check
#
# When you add a new acceptable check, add it here AND wire the
# producer code to call policy_acceptance() before emitting.

def policy_acceptance(check_id: str, config: dict | None) -> dict | None:
    """Return the operator's acceptance entry for ``check_id`` if any.

    Reads ``network.json::policy_acceptances``. Returns the per-check
    entry dict (with ``reason`` / ``accepted_at`` / ``accepted_by``
    fields) when the operator has declared the finding accepted;
    returns None otherwise.

    The block is optional — most installs won't have it. Tolerant of
    missing/malformed structure: anything other than a dict at any
    level treats the entry as absent.
    """
    if not isinstance(config, dict):
        return None
    acceptances = config.get("policy_acceptances")
    if not isinstance(acceptances, dict):
        return None
    entry = acceptances.get(check_id)
    if not isinstance(entry, dict):
        return None
    return entry


# ── Finding dataclass ─────────────────────────────────────────────────────────

@dataclass
class Finding:
    # "skipped" marks an audit check the runner could not perform (capability
    # gap — e.g. sudo grant or ACL missing). Skipped findings are kept in the
    # returned list for the report but are NOT mirrored to signals by
    # _emit_signals_from_findings (which iterates criticals + warns), so the
    # alerts page no longer carries audit-infrastructure noise.
    level: str          # "critical" | "warn" | "ok" | "skipped"
    category: str       # "identity" | "config" | "machine" | "proposal"
    bot_id: str | None  # None for machine-level findings
    message: str
    detail: str = ""
    # Optional explanation surfaced on the Alerts row.
    #   what_it_means — 1-3 sentence plain-English description of why the
    #     finding is firing and what it actually implies. Goes above "How
    #     to fix" so the operator can decide whether to act.
    #   fix_steps — numbered, copy-pasteable remediation steps. One step
    #     per line; the UI renders newlines as <li> when the string starts
    #     with "1." (or splits on "\n" otherwise).
    # Both default to empty so existing call sites keep working.
    what_it_means: str = ""
    fix_steps: str = ""


# ── Logging ───────────────────────────────────────────────────────────────────

_LOG_FILE = Path("/Users/Shared/evolve/logs/audit.log")


def _log(msg: str) -> None:
    print(msg, flush=True)
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(_LOG_FILE, "a") as f:
            f.write(f"{ts} {msg}\n")
    except OSError:
        pass


# ── Hashing ───────────────────────────────────────────────────────────────────

def sha256_str(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sha256_file(path: Path) -> str | None:
    """Hash a file we own directly. Returns None if unreadable."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def sha256_sudo(path: str | Path) -> str | None:
    """Hash a file we can read via sudo /bin/cat. Returns None if unreadable."""
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        return hashlib.sha256(r.stdout).hexdigest()
    except (subprocess.TimeoutExpired, OSError):
        return None


def sha256_git_workspace(bot_id: str, fname: str) -> str | None:
    """Hash a file from the bot's workspace HEAD commit. Returns None if not found."""
    workspace = _bot_home(bot_id) / ".openclaw" / "workspace"
    try:
        r = subprocess.run(
            ["sudo", "git", "-C", str(workspace), "show", f"HEAD:{fname}"],
            capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        return hashlib.sha256(r.stdout).hexdigest()
    except (subprocess.TimeoutExpired, OSError):
        return None


# ── Security alert channel ────────────────────────────────────────────────────


def _html_escape(value) -> str:
    """HTML-escape an interpolated value for Telegram parse_mode=HTML.

    Lazy import from the admin package — audit.py may execute when the
    admin package is partially-installed, so the import is best-effort
    with a stdlib fallback. Same pattern as heal.py and report.py.
    """
    try:
        from evolve_admin.alerts.catalog import html_escape
        return html_escape(value)
    except Exception:
        import html as _html
        return _html.escape("" if value is None else str(value), quote=False)


def _send_telegram_direct(token: str, chat_id: str, msg: str) -> None:
    """Send a Telegram message directly via Bot API (no OC gateway required)."""
    import urllib.request
    import urllib.parse
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": msg}).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=10)
    except Exception as e:
        _log(f"[audit] CRITICAL alert delivery failed via direct Telegram: {e}")


def _send_security_alert(msg: str, shared_dir: Path, config: dict) -> None:
    """Send a CRITICAL audit alert.

    Routes through ``alerts.dispatcher.send`` so the message appears in
    Reports → Subscriptions → Messages alongside every other automated
    alert, gets the standard origin footer, and honors operator subscription
    preferences. Falls back to a direct Telegram POST (using the dedicated
    ``shared_dir/keystore/security-alert-{token,chat-id}`` pair when
    present, otherwise the main bot's token) if the dispatcher itself is
    broken — defense-in-depth for "the admin package is wedged" scenarios,
    not a subscription bypass.

    Earlier behavior preferred the direct path whenever the dedicated
    keystore files existed, on the theory that security alerts should
    survive a broken admin package. That was correct for the failure mode
    it solved but had the unintended effect of making every CRITICAL alert
    invisible in the Messages log — operators couldn't see what had been
    sent or correlate it with subscription settings. Routing through the
    dispatcher first, falling back to direct only on dispatcher failure,
    preserves the resilience and adds the visibility.
    """
    sent_via_dispatcher = _send_via_dispatcher(msg, shared_dir, config)
    if sent_via_dispatcher:
        return

    sec_token_path = shared_dir / "keystore" / "security-alert-token"
    sec_chat_id_path = shared_dir / "keystore" / "security-alert-chat-id"
    if sec_token_path.exists() and sec_chat_id_path.exists():
        try:
            token = sec_token_path.read_text().strip()
            chat_id = sec_chat_id_path.read_text().strip()
            if token and chat_id:
                _send_telegram_direct(token, chat_id, msg)
        except OSError:
            _log("[audit] CRITICAL alert dropped — dispatcher failed and "
                 "security-alert keystore unreadable")


def _send_via_dispatcher(msg: str, shared_dir: Path, config: dict) -> bool:
    """Route an audit batch through the alert dispatcher.

    Returns ``True`` iff the dispatcher accepted the alert (any non-FAILED
    outcome — SENT, SUPPRESSED_DISABLED, SUPPRESSED_COOLDOWN, DEFERRED all
    count as "the dispatcher handled it"). Returns ``False`` when the
    dispatcher itself is broken (import failure, exception during send)
    so the caller can fall back to the direct Telegram path.

    dedup_key is a stable hash of the message content: identical
    findings → same key → cooldown applies; any change to the finding
    set → fresh key → push goes through.

    Phase 5b will replace this batched-hash dedup with per-finding
    Signal-store transitions (one Signal per check, dispatcher-fired on
    transitions only). Until then, batch-hash is operator-coherent
    enough — adding a new finding is the operator-relevant event, and
    that re-fires immediately.
    """
    try:
        from evolve_admin.alerts.dispatcher import (
            send as _dispatch_send, Severity, DispatchResult,
        )
    except Exception as exc:
        _log(f"[audit] dispatcher import failed; falling back to direct: {exc}")
        return False

    content_hash = hashlib.sha256(msg.encode("utf-8")).hexdigest()[:16]
    try:
        outcome = _dispatch_send(
            shared_dir=shared_dir,
            network=config,
            source="audit",
            message=msg,
            severity=Severity.CRITICAL,
            dedup_key=f"audit/batch/{content_hash}",
            catalog_event="security.audit_finding",
        )
        # A FAILED outcome means the dispatcher ran but its underlying send
        # didn't reach the channel — the direct-Telegram fallback in
        # _send_security_alert is the right next step.
        return outcome.result != DispatchResult.FAILED
    except Exception as exc:
        _log(f"[audit] dispatcher.send raised; falling back to direct: {exc}")
        return False


def _send_warn_log(msg: str, shared_dir: Path) -> None:
    """Log a WARN finding to the audit warn log for weekly review surfacing."""
    warn_log = shared_dir / "logs" / "audit-warns.jsonl"
    try:
        warn_log.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with open(warn_log, "a") as f:
            f.write(json.dumps({"ts": ts, "msg": msg}) + "\n")
    except OSError:
        pass


# ── Current-findings snapshot ─────────────────────────────────────────────────

CURRENT_FINDINGS_SCHEMA = 1


def _write_findings_snapshot(
    criticals: list[Finding],
    warns: list[Finding],
    shared_dir: Path,
) -> None:
    """Write the current set of open findings to {shared_dir}/audit/current-findings.json.

    This snapshot represents *current* state — overwritten each successful run.
    The pod report reads this file (not the audit-warns event log) to render the
    Security section, so a missing or stale snapshot signals "audit not running."
    Atomic via temp + os.replace, matching the {shared_dir}-owned write pattern
    in CLAUDE.md.
    """
    snapshot_dir = shared_dir / "audit"
    snapshot_path = snapshot_dir / "current-findings.json"
    payload = {
        "schema_version": CURRENT_FINDINGS_SCHEMA,
        "audit_completed_at": datetime.now(timezone.utc).isoformat(),
        "audit_succeeded": True,
        "critical": [
            {"category": f.category, "bot_id": f.bot_id,
             "message": f.message, "detail": f.detail}
            for f in criticals
        ],
        "warn": [
            {"category": f.category, "bot_id": f.bot_id,
             "message": f.message, "detail": f.detail}
            for f in warns
        ],
    }
    try:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        tmp = snapshot_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, snapshot_path)
    except OSError as e:
        _log(f"[audit] WARN: failed to write current-findings snapshot: {e}")


# ── 1. Identity audit ─────────────────────────────────────────────────────────

IDENTITY_FILES = ["SOUL.md", "AGENTS.md", "HEARTBEAT.md"]

# Additional files monitored for the evolve bot only — procedure docs for manifest apps.
# Changes to these flow through the RSI proposal pipeline, not direct edits.
EVOLVE_PROCEDURE_FILES = [
    "procedures/security-cve-scan.md",
]

# Policy files that must be locked read-only (mode 0444 / no write bit).
# A bot that can write to its own EMAIL_WHITELIST.md can append any
# recipient and effectively bypass operator-approved sending limits;
# EMAIL_POLICY.md has the same shape for outbound-content rules.
LOCKED_POLICY_FILES = ["EMAIL_WHITELIST.md", "EMAIL_POLICY.md"]


def audit_identity(
    bot_id: str, shared_dir: Path, primary_bot_id: str | None = None,
) -> list[Finding]:
    """Check identity files vs last git backup commit in the bot's workspace repo.

    When ``bot_id == primary_bot_id`` the audit also covers ``EVOLVE_PROCEDURE_FILES``
    — pod-wide security-sensitive procedure docs maintained on the primary
    bot's workspace.
    """
    findings: list[Finding] = []
    workspace = _bot_home(bot_id) / ".openclaw" / "workspace"

    # Check whether the workspace repo has any commits (backup may not have run yet).
    # No commits = git backup not configured yet; treat as intentional, not a warning.
    r = subprocess.run(
        ["sudo", "git", "-C", str(workspace), "rev-parse", "HEAD"],
        capture_output=True, timeout=5,
    )
    if r.returncode != 0:
        findings.append(Finding(
            level="ok", category="identity", bot_id=bot_id,
            message=f"{bot_id}: git backup not configured — identity baseline skipped",
        ))
        return findings

    for fname in IDENTITY_FILES:
        live_path = workspace / fname

        live_hash = sha256_sudo(live_path)
        git_hash = sha256_git_workspace(bot_id, fname)

        if live_hash is None:
            findings.append(Finding(
                level="warn", category="identity", bot_id=bot_id,
                message=f"{bot_id}: {fname} unreadable",
            ))
            continue

        if git_hash is None:
            # Not yet committed in this repo — not a problem, just not baselined
            findings.append(Finding(
                level="ok", category="identity", bot_id=bot_id,
                message=f"{bot_id}: {fname} not yet in backup baseline",
            ))
            continue

        if live_hash != git_hash:
            findings.append(Finding(
                level="critical", category="identity", bot_id=bot_id,
                message=f"🔴 CRITICAL: {bot_id} {fname} hash mismatch vs git backup",
                detail=f"live={live_hash[:12]} backup={git_hash[:12]}",
            ))
        else:
            findings.append(Finding(
                level="ok", category="identity", bot_id=bot_id,
                message=f"{bot_id}: {fname} OK",
            ))

    # For the primary bot, also check procedure docs for manifest apps.
    # These are security-sensitive: changes should flow through the proposal pipeline.
    # When the caller didn't pass a primary id (None), no bot matches and the
    # primary-only procedure checks are simply skipped — we do NOT substitute the
    # literal "evolve", which would mis-fire on a pod whose primary is "evo".
    if primary_bot_id and bot_id == primary_bot_id:
        for fname in EVOLVE_PROCEDURE_FILES:
            live_path = workspace / fname
            live_hash = sha256_sudo(live_path)
            git_hash = sha256_git_workspace(bot_id, fname)

            if live_hash is None:
                # Procedure file not yet deployed — not an error
                findings.append(Finding(
                    level="ok", category="identity", bot_id=bot_id,
                    message=f"{bot_id}: {fname} not yet deployed",
                ))
                continue

            if git_hash is None:
                findings.append(Finding(
                    level="ok", category="identity", bot_id=bot_id,
                    message=f"{bot_id}: {fname} not yet in backup baseline",
                ))
                continue

            if live_hash != git_hash:
                findings.append(Finding(
                    level="warn", category="identity", bot_id=bot_id,
                    message=f"{bot_id}: {fname} changed outside proposal pipeline",
                    detail=f"live={live_hash[:12]} backup={git_hash[:12]}",
                ))
            else:
                findings.append(Finding(
                    level="ok", category="identity", bot_id=bot_id,
                    message=f"{bot_id}: {fname} OK",
                ))

    return findings


def audit_policy_file_permissions(bot_id: str) -> list[Finding]:
    """Enforce that policy files are mode 0444 (no write bit set anywhere).

    Policy files (EMAIL_WHITELIST.md, EMAIL_POLICY.md) define what the
    bot is allowed to do. If they're writable, the bot can quietly
    rewrite its own policy to grant itself permissions the operator
    never approved — e.g. add an external recipient to EMAIL_WHITELIST
    and start sending exfil emails. They must be 0444 so even the
    bot's own user account cannot modify them.
    """
    findings: list[Finding] = []
    workspace = _bot_home(bot_id) / ".openclaw" / "workspace"
    for fname in LOCKED_POLICY_FILES:
        path = workspace / fname
        try:
            st = path.stat()
        except FileNotFoundError:
            continue  # file not present → no policy to enforce, no finding
        except (PermissionError, OSError):
            findings.append(Finding(
                level="skipped", category="identity", bot_id=bot_id,
                message=f"{bot_id}: {fname} permission check skipped (stat denied)",
            ))
            continue
        mode = st.st_mode & 0o777
        if mode & 0o222:  # any write bit set → policy is mutable by SOMEONE
            findings.append(Finding(
                level="critical", category="identity", bot_id=bot_id,
                message=f"🔴 CRITICAL: {bot_id} {fname} is writable (mode 0{mode:03o})",
                detail=f"path={path}",
                what_it_means=(
                    "Policy files must be locked read-only (mode 0444) so "
                    "the bot cannot quietly rewrite its own policy to grant "
                    "itself permissions the operator never approved. A "
                    f"writable {fname} would let the bot, or anyone who "
                    "can prompt-inject it, edit the policy on disk and "
                    "then act under the rewritten rules."
                ),
                fix_steps=(
                    f"On the mini, run:\n"
                    f"  ssh pod_admin_user@mini sudo chmod 0444 {path}\n"
                    "Then re-run the audit (or wait one cycle) to clear this finding."
                ),
            ))
        else:
            findings.append(Finding(
                level="ok", category="identity", bot_id=bot_id,
                message=f"{bot_id}: {fname} permission OK (0{mode:03o})",
            ))
    return findings


# ── 2. Config audit ───────────────────────────────────────────────────────────

def audit_config(bot_id: str, config: dict, shared_dir: Path) -> list[Finding]:
    """Check openclaw.json for expected values and unexpected changes."""
    findings: list[Finding] = []
    bots_cfg = config.get("bots", {})
    expected_port = bots_cfg.get(bot_id, {}).get("port")

    # Post-evo-account-separation exemption (spec-evo-account-separation-2026-05-25,
    # EVOLVE-ACCT-OCJSON): the separated evo primary does not host a stat-able
    # bot-shaped openclaw.json the way an ordinary member bot does — the
    # privileged `evolve` service account carries none, and the `evo` account's
    # config may live in the migrated OC agent SQLite store rather than at the
    # JSON path this check stats. `sudo /bin/cat` then returns rc!=0 and we'd
    # fire a spurious "cannot read openclaw.json" warn every audit. Skip ONLY
    # the separated evo primary; ordinary bots (and the pre-separation primary,
    # whose openclaw.json IS present) still get the full config audit.
    from primary_bot import (  # type: ignore
        primary_bot_id as _primary_bot_id,
        primary_is_separated_evo as _primary_is_separated_evo,
    )
    if bot_id == _primary_bot_id(config) and _primary_is_separated_evo(config):
        findings.append(Finding(
            level="ok", category="config", bot_id=bot_id,
            message=(
                f"{bot_id}: openclaw.json config audit skipped — separated evo "
                f"primary (no bot-shaped openclaw.json by design)"
            ),
        ))
        return findings

    # Read live openclaw.json via sudo
    oc_path = _bot_home(bot_id, config) / ".openclaw" / "openclaw.json"
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(oc_path)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            findings.append(Finding(
                level="warn", category="config", bot_id=bot_id,
                message=f"{bot_id}: cannot read openclaw.json",
            ))
            return findings
        oc = json.loads(r.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        findings.append(Finding(
            level="warn", category="config", bot_id=bot_id,
            message=f"{bot_id}: cannot parse openclaw.json",
        ))
        return findings

    # Gateway bind — must be 127.0.0.1
    bind = oc.get("gateway", {}).get("bind", "127.0.0.1")
    if bind and bind not in ("127.0.0.1", "localhost", "loopback", ""):
        findings.append(Finding(
            level="critical", category="config", bot_id=bot_id,
            message=f"🔴 CRITICAL: {bot_id} gateway.bind is {bind!r} — should be 127.0.0.1",
        ))
    else:
        findings.append(Finding(
            level="ok", category="config", bot_id=bot_id,
            message=f"{bot_id}: gateway.bind OK ({bind!r})",
        ))

    # Port match
    actual_port = oc.get("gateway", {}).get("port")
    if expected_port and actual_port and actual_port != expected_port:
        findings.append(Finding(
            level="warn", category="config", bot_id=bot_id,
            message=f"{bot_id}: port mismatch — network.json={expected_port} openclaw.json={actual_port}",
        ))
    else:
        findings.append(Finding(
            level="ok", category="config", bot_id=bot_id,
            message=f"{bot_id}: port OK",
        ))

    # Exec allowlist — any entry is notable; compare to backup baseline
    exec_cfg = oc.get("exec", {})
    if exec_cfg.get("enabled") and exec_cfg.get("allowList"):
        allow = exec_cfg["allowList"]
        findings.append(Finding(
            level="warn", category="config", bot_id=bot_id,
            message=f"{bot_id}: exec allowlist enabled with {len(allow)} entries",
            detail=", ".join(str(x) for x in allow[:5]),
        ))

    # New plugins vs backup baseline
    _check_new_plugins(bot_id, oc, shared_dir, findings)

    # Provider-models registry drift — agents.defaults.models key without
    # a matching entry under models.providers[prov].models[]. OC's failover
    # runtime rejects these at request time with FailoverError, which on a
    # cold cache can burn real tokens before the chain is declared
    # exhausted (2026-06-03 personal-bot incident — $36 in two background
    # turns). The ensure_plugin_config reconciler closes the gap on the
    # next deploy, but the audit visibility catches it sooner.
    _check_provider_models_registry(bot_id, oc, findings)

    # Tier→openclaw drift — primary/fallbacks in openclaw.json don't match
    # what evolve-tiers.json would produce. Same pattern as the registry
    # check above: the deploy-time reconciler closes the gap, and this
    # audit makes the gap visible before the next deploy.
    _check_tier_to_openclaw_drift(bot_id, oc, config, findings)

    return findings


def _check_provider_models_registry(bot_id: str, oc: dict, findings: list[Finding]) -> None:
    """Warn if any ``agents.defaults.models`` slug lacks a registry entry.

    The OC runtime contract (per its own error message):

      ``FailoverError: Unknown model: <prov>/<mid>. Found
      agents.defaults.models["<prov>/<mid>"], but no matching
      models.providers["<prov>"].models[] entry.``

    OC tolerates a config that omits ``models.providers`` entirely —
    it falls back to its implicit registry. But once the section
    EXISTS, every ``agents.defaults.models`` slug must be covered or
    the runtime fails (and on background tasks the failure is
    invisible to the operator until the cost ledger surfaces it).

    The fix is mechanical and shipped: ``_reconcile_provider_models_registry``
    in deploy.py fills the gap on every ensure_plugin_config pass. This
    audit is the visibility surface so the operator sees the gap
    BEFORE the next deploy closes it (or if the deploy is delayed).
    """
    agents_defaults = (oc.get("agents") or {}).get("defaults", {})
    agents_models = agents_defaults.get("models")
    if not isinstance(agents_models, dict) or not agents_models:
        return  # nothing to validate

    providers_block = (oc.get("models") or {}).get("providers")
    if not isinstance(providers_block, dict) or not providers_block:
        # OC tolerates absent registry — the typical member-bot shape on
        # the reference pod has no ``models.providers`` section at all
        # and works fine via OC's implicit registry. Don't fire on this
        # case.
        return

    # Build {provider: {registered_ids}} for fast membership checks.
    registered: dict[str, set[str]] = {}
    for prov_name, prov_body in providers_block.items():
        if not isinstance(prov_body, dict):
            continue
        models_list = prov_body.get("models") or []
        if not isinstance(models_list, list):
            continue
        ids: set[str] = set()
        for entry in models_list:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                ids.add(entry["id"])
        registered[prov_name] = ids

    missing: list[str] = []
    for slug in agents_models:
        if not isinstance(slug, str) or "/" not in slug:
            continue  # malformed slug — separate problem
        prov, _, mid = slug.partition("/")
        if not prov or not mid:
            continue
        if mid not in registered.get(prov, set()):
            missing.append(slug)

    if not missing:
        return

    preview = ", ".join(missing[:5])
    if len(missing) > 5:
        preview += f", … (+{len(missing) - 5} more)"
    findings.append(Finding(
        level="warn",
        category="config",
        bot_id=bot_id,
        message=(
            f"{bot_id}: {len(missing)} agents.defaults.models slug(s) missing "
            f"from models.providers registry"
        ),
        detail=preview,
        what_it_means=(
            "Each agents.defaults.models entry must have a matching "
            "models.providers[provider].models[] entry whose id is the "
            "model portion of the slug (everything after the slash). "
            "When a slug is missing, the OC failover runtime rejects "
            "any request that picks that model — the bot's whole "
            "fallback chain can exhaust on cold cache and bill real "
            "tokens before any visible error surfaces."
        ),
        fix_steps=(
            f"1. sudo evolve-admin deploy {bot_id} "
            f"(the ensure_plugin_config reconciler fills the gap)\n"
            f"2. sudo /bin/launchctl kickstart -k "
            f"system/ai.evolve.openclaw.{bot_id}.gateway "
            f"(picks up the new registry)\n"
            f"3. Verify with: sudo /bin/cat /Users/{bot_id}/.openclaw/openclaw.json "
            f"| python3 -c \"import json,sys; "
            f"c=json.load(sys.stdin); "
            f"print(json.dumps(c.get('models',{{}}).get('providers',{{}}), indent=2))\""
        ),
    ))


def _check_tier_to_openclaw_drift(
    bot_id: str, oc: dict, config: dict, findings: list[Finding],
) -> None:
    """Warn if openclaw.json's primary/fallbacks disagree with evolve-tiers.json.

    The contract: ``agents.defaults.model.primary`` and ``fallbacks`` in
    ``openclaw.json`` are DERIVED from the per-bot
    ``~/.openclaw/evolve-tiers.json`` (set on the AI Optimization page).
    The materialization path lives in ``deploy.py::ensure_plugin_config``
    (every deploy) and ``oc_model.py::json_full_config_set`` (every
    tier-save through the UI).

    Bots whose primary was seeded outside that materialization path
    — e.g. an ad-hoc ``openclaw config set agents.defaults.model.primary
    <model>`` on the host, or a wizard write that ran before the tier
    derivation logic landed — can carry a primary that no longer
    matches the tier cascade and/or an empty ``fallbacks`` list even
    though tier2 has multiple models.

    The next deploy auto-fixes this (tier→openclaw propagation in
    ensure_plugin_config). This audit surfaces the drift between
    deploys so the operator sees it and can run a deploy on demand.

    Skipped silently when:
      * evolve-tiers.json is missing (fresh bot, never visited AI
        Optimization — there's no tier source-of-truth to compare to)
      * the tiers block is empty (same reason)
      * the file is unreadable (sudo failure — separate problem)
    """
    bot_user = get_bot_user(bot_id, config)
    tiers_path = _bot_home(bot_id, config) / ".openclaw" / "evolve-tiers.json"

    # Read tiers via sudo /bin/cat for the same reason as openclaw.json above:
    # parent dir may not be searchable by the audit user even with an ACL.
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(tiers_path)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            # File is missing OR sudo grant is wrong. "No such file" is the
            # benign case (bot never visited AI Optimization); other errors
            # are sudoers-config problems that surface under audit_evolve_sudoers.
            return
        tiers_file = json.loads(r.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return

    if not isinstance(tiers_file, dict):
        return

    # Compute the expected (primary, fallbacks) using oc_model's helpers.
    # Both functions are pure and side-effect-free; safe to call from audit.
    # synthesize_legacy_tiers projects a new rungs/roles file back to the
    # legacy tier view so drift detection keeps working post-migration —
    # without it, ``tiers_file.get("tiers")`` is empty on every migrated bot
    # and tier-drift detection silently goes dark.
    try:
        from oc_model import (
            default_tier_cascade_for_role,
            generate_fallback_list,
            synthesize_legacy_tiers,
        )
    except ImportError:
        return  # can't compute expected — skip silently

    tiers = synthesize_legacy_tiers(tiers_file)
    if not isinstance(tiers, dict) or not tiers:
        return  # nothing to compare to

    bots_cfg = config.get("bots", {})
    role = bots_cfg.get(bot_id, {}).get("role") or "member"
    cascade = tiers_file.get("tierCascade")
    if not isinstance(cascade, list) or not cascade:
        cascade = default_tier_cascade_for_role(role)
    flat = generate_fallback_list(tiers, cascade)
    if not flat:
        return  # cascade resolves to nothing — separate problem (empty tiers)

    expected_primary = flat[0]
    expected_fallbacks = flat[1:]

    model_cfg = (oc.get("agents") or {}).get("defaults", {}).get("model", {})
    actual_primary = model_cfg.get("primary") or ""
    actual_fallbacks = model_cfg.get("fallbacks") or []
    if not isinstance(actual_fallbacks, list):
        actual_fallbacks = []

    primary_drift = actual_primary != expected_primary
    fallbacks_drift = list(actual_fallbacks) != expected_fallbacks

    if not (primary_drift or fallbacks_drift):
        return  # in sync — silent OK

    # Build a focused detail string showing the disagreement.
    detail_parts: list[str] = []
    if primary_drift:
        detail_parts.append(
            f"primary: openclaw.json={actual_primary or '∅'} "
            f"vs tiers={expected_primary}"
        )
    if fallbacks_drift:
        detail_parts.append(
            f"fallbacks: openclaw.json=[{', '.join(actual_fallbacks) or '∅'}] "
            f"vs tiers=[{', '.join(expected_fallbacks) or '∅'}]"
        )

    findings.append(Finding(
        level="warn",
        category="config",
        bot_id=bot_id,
        message=(
            f"{bot_id}: agents.defaults.model drifted from evolve-tiers.json"
        ),
        detail="; ".join(detail_parts),
        what_it_means=(
            "The bot's primary model and fallbacks are derived from its "
            "tier definitions (AI Optimization page → Tier Definitions, "
            "stored in ~/.openclaw/evolve-tiers.json). The values "
            "currently in openclaw.json don't match what the tier "
            "definitions would produce — usually because the primary "
            "was seeded by an older codepath that ran before the tier "
            "derivation logic, or because an ad-hoc `openclaw config "
            "set` was run on the host. The bot still works, but turns "
            "won't use the model the AI Optimization page advertises "
            "and the configured fallbacks won't kick in on rate-limit "
            "or error."
        ),
        fix_steps=(
            f"1. sudo evolve-admin deploy {bot_id} "
            f"(ensure_plugin_config recomputes primary+fallbacks from "
            f"evolve-tiers.json on every deploy)\n"
            f"2. Verify with: sudo /bin/cat /Users/{bot_user}/.openclaw/openclaw.json "
            f"| python3 -c \"import json,sys; "
            f"m=json.load(sys.stdin)['agents']['defaults']['model']; "
            f"print('primary:', m.get('primary')); "
            f"print('fallbacks:', m.get('fallbacks'))\""
        ),
    ))


def _check_new_plugins(bot_id: str, oc: dict, shared_dir: Path, findings: list[Finding]) -> None:
    """Warn if openclaw.json has plugin entries not in any recent apply-result."""
    plugins_raw = oc.get("plugins") or {}
    if isinstance(plugins_raw, dict):
        entries = plugins_raw.get("entries") or {}
    elif isinstance(plugins_raw, list):
        return  # list form has no plugin IDs to check
    else:
        return

    if isinstance(entries, dict):
        plugin_ids = set(entries.keys())
    else:
        return

    # Get plugin IDs mentioned in apply-results
    applied_plugins: set[str] = set()
    results_dir = shared_dir / "proposals" / "apply-results"
    if results_dir.exists():
        for f in results_dir.glob("*.json"):
            try:
                r = json.loads(f.read_text())
                if r.get("bot_id") != bot_id or not r.get("success"):
                    continue
                change = r.get("proposed_change", {})
                # Plugin changes typically touch plugins.entries.<id>
                for k in change.keys():
                    if k.startswith("plugins.entries."):
                        applied_plugins.add(k.split(".")[2])
            except (OSError, json.JSONDecodeError):
                pass

    new_plugins = plugin_ids - applied_plugins
    # Only meaningful once RSI has applied at least one plugin change for this
    # bot — before that we have no baseline and every plugin looks "new".
    if applied_plugins and new_plugins:
        findings.append(Finding(
            level="warn", category="config", bot_id=bot_id,
            message=f"{bot_id}: {len(new_plugins)} plugin(s) not in apply-results: {', '.join(sorted(new_plugins)[:5])}",
        ))


def audit_evolve_sudoers(shared_dir: Path, config: dict) -> list[Finding]:
    """Hash-check /etc/sudoers.d/evolve against stored baseline AND lint
    contents for dangerous grant patterns.

    The hash check catches drift (any edit alerts). The lint catches the
    failure mode where someone intentionally writes a dangerous grant
    and the baseline gets re-blessed — the hash matches, but the content
    is now privilege-equivalent to root. Patterns like
    ``evolve ALL=(ALL) NOPASSWD: ALL`` would silently grant unrestricted
    root if they ever land; the linter ensures they fire critical
    regardless of baseline state.

    Lint runs against the live sudoers content, not the baseline — so
    it ALSO catches a dangerous grant that was always present (e.g.
    pasted during initial install before the baseline was written).
    """
    findings: list[Finding] = []
    sudoers_path = Path("/etc/sudoers.d/evolve")
    baseline_path = shared_dir / "security" / "sudoers-evolve.sha256"

    current_hash = sha256_sudo(sudoers_path)
    if current_hash is None:
        findings.append(Finding(
            level="skipped", category="config", bot_id="evolve",
            message="evolve: sudoers read denied — audit user lacks sudo /bin/cat grant for /etc/sudoers.d/evolve",
            detail="check /etc/sudoers.d/evolve and the audit user's sudoers entry",
        ))
        return findings

    # Always lint content, even on first-run baseline creation. A
    # dangerous grant existing on day 0 should fire critical immediately.
    findings.extend(_lint_sudoers_content(sudoers_path))

    if not baseline_path.exists():
        # First run — write baseline
        try:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(current_hash)
            findings.append(Finding(
                level="ok", category="config", bot_id="evolve",
                message="evolve: sudoers baseline created",
            ))
        except OSError:
            findings.append(Finding(
                level="warn", category="config", bot_id="evolve",
                message="evolve: cannot write sudoers baseline",
            ))
        return findings

    stored_hash = baseline_path.read_text().strip()
    if current_hash != stored_hash:
        findings.append(Finding(
            level="critical", category="config", bot_id="evolve",
            message="🔴 CRITICAL: /etc/sudoers.d/evolve changed since baseline",
            detail=f"stored={stored_hash[:12]} current={current_hash[:12]}",
        ))
    else:
        findings.append(Finding(
            level="ok", category="config", bot_id="evolve",
            message="evolve: sudoers OK",
        ))

    return findings


# Dangerous-grant patterns. Each entry: (regex, label, what_it_means).
# Order matters — the most-specific patterns first so a single offending
# line gets the most-precise classification.
_SUDOERS_DANGEROUS_PATTERNS: list[tuple[str, str, str]] = [
    (
        r"^\s*evolve\s+ALL\s*=\s*\(\s*ALL\s*\)\s*NOPASSWD\s*:\s*ALL\s*$",
        "evolve ALL=(ALL) NOPASSWD: ALL",
        "Grants the evolve service user passwordless root for any "
        "command. This is privilege-equivalent to running every Evolve "
        "process as root and defeats the entire narrow-grants design of "
        "the sudoers file.",
    ),
    (
        r"^\s*evolve\s+ALL\s*=\s*NOPASSWD\s*:\s*ALL\s*$",
        "evolve ALL=NOPASSWD: ALL",
        "Same as the (ALL) form — grants unrestricted passwordless "
        "root to the evolve service user.",
    ),
    (
        r"^\s*evolve\s+ALL\s*=\s*\(\s*root\s*\)\s*NOPASSWD\s*:\s*ALL\s*$",
        "evolve ALL=(root) NOPASSWD: ALL",
        "Grants unrestricted passwordless root via the (root) runas "
        "spec. Equivalent in effect to NOPASSWD: ALL.",
    ),
    (
        r"^\s*evolve\s+ALL\s*=\s*\([^)]*\)\s*NOPASSWD\s*:\s*\*\s*$",
        "evolve ... NOPASSWD: *",
        "A bare wildcard as the command spec matches any executable "
        "path. Functionally equivalent to NOPASSWD: ALL but harder to "
        "grep for — call it out explicitly.",
    ),
]


def _lint_sudoers_content(sudoers_path: Path) -> list[Finding]:
    """Scan the live sudoers file for dangerous grant patterns."""
    findings: list[Finding] = []
    text = _read_sudoers_text(sudoers_path)
    if text is None:
        # Read failure isn't a lint issue — the hash check already
        # surfaces "can't read sudoers" via the skipped path.
        return findings

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        # Skip comments and blank lines so the regexes don't match
        # commented-out examples in docs blocks.
        stripped = raw_line.split("#", 1)[0].strip()
        if not stripped:
            continue
        for pattern, label, what_it_means in _SUDOERS_DANGEROUS_PATTERNS:
            if re.match(pattern, stripped):
                findings.append(Finding(
                    level="critical", category="config", bot_id="evolve",
                    message=(
                        f"🔴 CRITICAL: /etc/sudoers.d/evolve contains "
                        f"dangerous grant: {label} (line {line_no})"
                    ),
                    detail=raw_line[:200],
                    what_it_means=what_it_means,
                    fix_steps=(
                        "1. The evolve sudoers file is rendered by "
                        "`setup_wizard._render_evolve_sudoers()` and "
                        "installed by `_write_evolve_sudoers()`. Check "
                        "whether the dangerous grant was added in code "
                        "or pasted directly into /etc/sudoers.d/evolve\n"
                        "2. If it's in code, revert the change — every "
                        "legitimate grant should be command-specific\n"
                        "3. If pasted directly, re-render and reinstall:\n"
                        "   ssh pod_admin_user@mini sudo evolve-admin refresh-sudoers\n"
                        "4. After reinstalling, the next audit cycle will "
                        "clear this finding"
                    ),
                ))
                break   # match the most-specific pattern only
    return findings


def _read_sudoers_text(sudoers_path: Path) -> str | None:
    """Read sudoers via sudo /bin/cat (the file is mode 0440 root:wheel
    so direct read fails for the evolve user). Returns None on failure."""
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(sudoers_path)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        return r.stdout
    except (subprocess.TimeoutExpired, OSError):
        return None


# ── 3. Machine audit ──────────────────────────────────────────────────────────

# Platform applicability for the machine-level checks. Each entry is
# ``(name, run, applies_to)``:
#   * ``name``        — short check id, used only for the skip-debug line and
#                       as the legible key a follow-up coverage lint reads.
#   * ``run``         — a ``(shared_dir, config) -> list[Finding]`` adapter.
#                       The per-check call signatures diverge (some take
#                       config, some shared_dir, some neither); normalising
#                       them here keeps audit_machine's loop uniform. Each
#                       adapter resolves the ``_check_*`` name from module
#                       globals at CALL time (not import), so the unit suite's
#                       ``monkeypatch.setattr(audit, "_check_*", ...)`` still
#                       takes effect.
#   * ``applies_to``  — the set of platform-profile names
#                       (``platform_profile.get_profile().name``) the check is
#                       valid on. ``audit_machine`` runs a check only when the
#                       pod's running profile is in this set.
#
# The monitor runs ON the host it audits, so ``get_profile()`` (autodetected
# from ``sys.platform``) is the pod's REAL OS — there is intentionally no
# stored per-pod platform field. A macOS-only check is SILENTLY skipped on
# Linux (emitting no Signal is correct: it has nothing valid to say about a
# host whose macOS binaries/paths don't exist, and running it just FileNotFound-
# falls-through into a false CRITICAL). This table is the single declaration of
# each check's platform applicability.
_MACOS_ONLY = frozenset({"macos"})
_LINUX_ONLY = frozenset({"linux"})
_ALL_PLATFORMS = frozenset({"macos", "linux"})

_MACHINE_CHECKS: tuple[tuple[str, Any, frozenset[str]], ...] = (
    ("firewall",           lambda shared_dir, config: _check_firewall(),                        _MACOS_ONLY),
    ("filevault",          lambda shared_dir, config: _check_filevault(config),                 _MACOS_ONLY),
    ("admin_user_gateway", lambda shared_dir, config: _check_admin_user_gateway(config),        _ALL_PLATFORMS),
    ("ssh_config",         lambda shared_dir, config: _check_ssh_config(),                      _ALL_PLATFORMS),
    ("macos_updates",      lambda shared_dir, config: _check_macos_updates(shared_dir, config),  _MACOS_ONLY),
    ("user_accounts",      lambda shared_dir, config: _check_user_accounts(shared_dir, config),  _MACOS_ONLY),
    ("listening_ports",    lambda shared_dir, config: _check_listening_ports(shared_dir),        _ALL_PLATFORMS),
    ("oc_binary_mtime",    lambda shared_dir, config: _check_oc_binary_mtime(shared_dir),        _MACOS_ONLY),
    # Linux equivalents of the macOS firewall/FileVault/softwareupdate checks.
    # Appended after the macOS set so the macOS run order stays byte-identical
    # (these are skipped on macOS). See the Linux machine-checks section below.
    ("linux_firewall",        lambda shared_dir, config: _check_linux_firewall(config),         _LINUX_ONLY),
    ("linux_disk_encryption", lambda shared_dir, config: _check_linux_disk_encryption(config),  _LINUX_ONLY),
    ("linux_os_updates",      lambda shared_dir, config: _check_linux_os_updates(config),        _LINUX_ONLY),
)


def audit_machine(shared_dir: Path, config: dict) -> list[Finding]:
    """Machine-level security checks, gated by platform applicability.

    Each check declares which platform profiles it applies to in
    ``_MACHINE_CHECKS``; a check whose ``applies_to`` excludes the running
    pod's profile (``get_profile().name``, autodetected from ``sys.platform``
    — the monitor runs on the host it audits) is silently skipped. On macOS
    every check applies, so the behaviour is identical to the prior fixed
    call list (same order, same args). On Linux the three platform-neutral
    checks run — admin-user-gateway, ssh-config, listening-ports — plus the
    three Linux machine checks (firewall, disk-encryption, OS-updates); the
    macOS-only checks (FileVault, macOS updates, user accounts, OC binary
    mtime, the macOS firewall) emit nothing rather than false-firing on
    absent macOS binaries.
    """
    profile = get_profile().name
    findings: list[Finding] = []
    for name, run, applies_to in _MACHINE_CHECKS:
        if profile not in applies_to:
            logger.debug("audit_machine: skipping %s check on %s", name, profile)
            continue
        findings.extend(run(shared_dir, config))
    return findings


def _check_firewall() -> list[Finding]:
    # macOS has two independent host firewalls: Application Firewall (ALF, the
    # default Mac users actually configure via System Settings → Network →
    # Firewall) and pfctl (the lower-level packet filter, rarely enabled on
    # client Macs and not even available as a CLI on recent macOS releases).
    # Prefer ALF; fall back to pfctl only if ALF is off or unavailable.
    try:
        r = subprocess.run(
            ["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and "enabled" in r.stdout.lower():
            return [Finding(level="ok", category="machine", bot_id=None,
                            message="machine: firewall OK (Application Firewall enabled)")]
    except (subprocess.TimeoutExpired, OSError):
        pass

    try:
        r = subprocess.run(
            ["sudo", "pfctl", "-s", "rules"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return [Finding(level="ok", category="machine", bot_id=None,
                            message="machine: firewall OK (pfctl rules loaded)")]
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        pass

    return [Finding(
        level="critical", category="machine", bot_id=None,
        message="🔴 CRITICAL: macOS host firewall is off (neither Application Firewall nor pfctl is active)",
    )]


def _check_filevault(config: dict | None = None) -> list[Finding]:
    # FileVault is macOS full-disk encryption. Without it, anyone with
    # physical access to the mini (or its disk, if the unit is stolen)
    # can read every bot's transcripts, API keys, and workspace files
    # by booting from external media. `fdesetup status` is read-only and
    # works for any local user on default macOS; no sudo grant required.
    #
    # The operator can declare FileVault-off as accepted via
    # network.json::policy_acceptances["machine.filevault_off"] — that
    # demotes the critical finding to an ok-level "operator-accepted"
    # line. See ``policy_acceptance()`` for the block format.
    try:
        r = subprocess.run(
            ["/usr/bin/fdesetup", "status"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return [Finding(
            level="skipped", category="machine", bot_id=None,
            message="machine: FileVault status check skipped (fdesetup unavailable)",
        )]

    if r.returncode != 0:
        return [Finding(
            level="skipped", category="machine", bot_id=None,
            message="machine: FileVault status read failed",
            detail=(r.stderr or r.stdout or "").strip()[:200],
        )]

    out = (r.stdout or "").strip()
    out_lower = out.lower()
    if "filevault is on" in out_lower:
        return [Finding(level="ok", category="machine", bot_id=None,
                        message="machine: FileVault OK (enabled)")]
    if "filevault is off" in out_lower:
        accepted = policy_acceptance("machine.filevault_off", config)
        if accepted:
            reason = (accepted.get("reason") or "").strip() or "no reason recorded"
            return [Finding(
                level="ok", category="machine", bot_id=None,
                message=(
                    f"machine: FileVault is off — operator-accepted "
                    f"({reason})"
                ),
                detail=out[:200],
            )]
        return [Finding(
            level="critical", category="machine", bot_id=None,
            message="🔴 CRITICAL: FileVault is off — disk is not encrypted",
            detail=out[:200],
            what_it_means=(
                "FileVault is macOS full-disk encryption. While it is off, "
                "anyone with physical access to this Mac (or to its disk, "
                "if the unit is stolen or sent for repair) can read every "
                "bot's transcripts, API keys, OAuth tokens, and workspace "
                "files by booting from external media."
            ),
            fix_steps=(
                "1. Open System Settings → Privacy & Security → FileVault\n"
                "2. Click \"Turn On…\" and follow the prompts\n"
                "3. Store the recovery key somewhere safe and offline (1Password, "
                "a hardware safe, or a sealed envelope — NOT on this Mac)\n"
                "4. Initial encryption runs in the background and can take "
                "several hours; the Mac remains usable throughout\n"
                "\n"
                "If FileVault is intentionally off (single-tenant dev mini, "
                "physically locked room, etc.), declare it in "
                "network.json:\n"
                "   \"policy_acceptances\": {\n"
                "     \"machine.filevault_off\": {\n"
                "       \"reason\": \"why\",\n"
                "       \"accepted_at\": \"2026-06-04\",\n"
                "       \"accepted_by\": \"<your handle>\"\n"
                "     }\n"
                "   }"
            ),
        )]
    # In-progress or unrecognized output ("Decryption in progress…",
    # "Encryption in progress…", etc.). Treat as warn so it shows up
    # without paging — encryption-in-progress is benign, decryption-
    # in-progress is bad-but-not-immediate.
    return [Finding(
        level="warn", category="machine", bot_id=None,
        message="machine: FileVault status indeterminate",
        detail=out[:200],
    )]


# 12 hours. `softwareupdate --list` hits Apple's catalog and takes
# 5-30s; with audit running every 15 min, an uncached check would
# query Apple ~96 times/day per Mac for slowly-changing state. 12h
# means at most two real queries per day, and a pending security
# patch is still surfaced within half a day of becoming available.
_MACOS_UPDATES_CACHE_TTL_SEC = 12 * 60 * 60


def _check_macos_updates(
    shared_dir: Path, config: dict | None = None,
) -> list[Finding]:
    """Flag pending macOS updates (especially security/recommended).

    Security_bot's daily security checklist explicitly checked for available
    OS updates. Evolve had no equivalent — a pending Security Update
    advertised by Apple could sit unnoticed for weeks.

    Severity policy:
      - "Security Update" in any pending title       → critical
      - any pending update with "Recommended: YES"   → warn
      - other pending updates (optional / drivers)   → ok (noted)
      - no pending updates                           → ok

    The operator can defer recommended/pending non-security updates via
    network.json::policy_acceptances["machine.macos_updates_pending"];
    Security Updates always fire regardless (the acceptance mechanism is
    deliberately scoped to non-critical findings — paging the operator
    on a published CVE patch is the whole point of the check).

    Caching: results are stored at
    ``{shared_dir}/security/macos-updates-cache.json`` with a 12h TTL
    so the 15-minute audit cycle doesn't re-query Apple every run.
    """
    cache_path = shared_dir / "security" / "macos-updates-cache.json"

    raw_output, queried_at, used_cache = _read_macos_updates_cache(cache_path)
    if raw_output is None:
        raw_output, queried_at = _query_macos_updates()
        if raw_output is None:
            return [Finding(
                level="skipped", category="machine", bot_id=None,
                message="machine: macOS update check skipped (softwareupdate unavailable)",
            )]
        _write_macos_updates_cache(cache_path, raw_output, queried_at)
        used_cache = False

    pending = _parse_softwareupdate_list(raw_output)

    if not pending:
        return [Finding(level="ok", category="machine", bot_id=None,
                        message="machine: macOS updates OK (none pending)")]

    age_note = " (cached)" if used_cache else ""
    labels = [p["label"] for p in pending]
    detail = f"pending: {', '.join(labels)}{age_note}"

    security = [p for p in pending if "security update" in p["title"].lower()]
    recommended = [p for p in pending if p["recommended"] and p not in security]

    if security:
        return [Finding(
            level="critical", category="machine", bot_id=None,
            message=f"🔴 CRITICAL: pending macOS Security Update — {security[0]['title']}",
            detail=detail,
            what_it_means=(
                "Apple has published a Security Update for this Mac that is not "
                "yet installed. Security Updates patch publicly-disclosed "
                "vulnerabilities; the window between publication and patch is "
                "the period of highest exploit risk."
            ),
            fix_steps=(
                "1. Open System Settings → General → Software Update\n"
                "2. Install all pending updates (most apply without a full "
                "restart; OS updates will reboot the Mac)\n"
                "3. After reboot, re-run the audit (or wait one cycle) to "
                "clear this finding"
            ),
        )]

    if recommended:
        accepted = policy_acceptance("machine.macos_updates_pending", config)
        if accepted:
            reason = (accepted.get("reason") or "").strip() or "no reason recorded"
            return [Finding(
                level="ok", category="machine", bot_id=None,
                message=(
                    f"machine: {len(recommended)} recommended macOS update(s) "
                    f"pending — operator-accepted ({reason})"
                ),
                detail=detail,
            )]
        return [Finding(
            level="warn", category="machine", bot_id=None,
            message=f"machine: {len(recommended)} recommended macOS update(s) pending",
            detail=detail,
            what_it_means=(
                "Apple has marked one or more pending updates as Recommended. "
                "These usually include security fixes bundled into larger OS "
                "releases. Not as time-critical as a standalone Security "
                "Update, but worth installing within the next few days."
            ),
            fix_steps=(
                "1. Open System Settings → General → Software Update\n"
                "2. Review pending updates and install when convenient\n"
                "3. Plan for a brief outage if any update requires a restart\n"
                "\n"
                "If you're intentionally deferring (release freeze, etc.), "
                "declare the deferral in network.json:\n"
                "   \"policy_acceptances\": {\n"
                "     \"machine.macos_updates_pending\": {\n"
                "       \"reason\": \"deferring until release-freeze ends\",\n"
                "       \"accepted_at\": \"2026-06-04\",\n"
                "       \"accepted_by\": \"<your handle>\"\n"
                "     }\n"
                "   }\n"
                "Security Updates still fire regardless of this acceptance."
            ),
        )]

    return [Finding(
        level="ok", category="machine", bot_id=None,
        message=f"machine: macOS updates OK ({len(pending)} optional pending)",
        detail=detail,
    )]


def _query_macos_updates() -> tuple[str | None, str | None]:
    """Run softwareupdate --list. Returns (stdout+stderr, iso_timestamp) or (None, None)."""
    try:
        r = subprocess.run(
            ["/usr/sbin/softwareupdate", "--list"],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return None, None
    # softwareupdate writes the actual listing to stderr on some macOS
    # versions and stdout on others. Concatenate for stable parsing.
    output = (r.stdout or "") + "\n" + (r.stderr or "")
    return output, datetime.now(timezone.utc).isoformat()


def _read_macos_updates_cache(cache_path: Path) -> tuple[str | None, str | None, bool]:
    """Returns (raw_output, queried_at, is_fresh). is_fresh False ⇒ caller should re-query."""
    try:
        data = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None, False
    queried_at = data.get("queried_at")
    raw_output = data.get("raw_output")
    if not queried_at or raw_output is None:
        return None, None, False
    try:
        ts = datetime.fromisoformat(queried_at)
    except ValueError:
        return None, None, False
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age > _MACOS_UPDATES_CACHE_TTL_SEC:
        return None, None, False
    return raw_output, queried_at, True


def _write_macos_updates_cache(cache_path: Path, raw_output: str, queried_at: str) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"queried_at": queried_at, "raw_output": raw_output}))
        os.replace(tmp, cache_path)
    except OSError:
        pass


_SOFTWAREUPDATE_LABEL_RE = re.compile(r"^\*\s*Label:\s*(.+?)\s*$")
_SOFTWAREUPDATE_TITLE_RE = re.compile(r"\bTitle:\s*(.+?),\s*Version:")
_SOFTWAREUPDATE_RECOMMENDED_RE = re.compile(r"\bRecommended:\s*YES\b", re.IGNORECASE)


def _parse_softwareupdate_list(output: str) -> list[dict]:
    """Parse softwareupdate --list output into [{label, title, recommended}].

    Output format on recent macOS is two lines per update:
        * Label: macOS Tahoe 26.5-25F71
            Title: macOS Tahoe 26.5, Version: 26.5, Size: ..., Recommended: YES, Action: restart,

    Older macOS versions used a slightly different format ("* macOS..."
    without "Label:"); we only target the modern format, which has been
    stable since macOS 12.
    """
    updates: list[dict] = []
    lines = output.splitlines()
    i = 0
    while i < len(lines):
        m = _SOFTWAREUPDATE_LABEL_RE.match(lines[i])
        if not m:
            i += 1
            continue
        label = m.group(1)
        # Detail line is typically the next line; tolerate one blank line.
        detail_line = ""
        for j in (i + 1, i + 2):
            if j < len(lines) and "Title:" in lines[j]:
                detail_line = lines[j]
                break
        title_m = _SOFTWAREUPDATE_TITLE_RE.search(detail_line)
        title = title_m.group(1).strip() if title_m else label
        recommended = bool(_SOFTWAREUPDATE_RECOMMENDED_RE.search(detail_line))
        updates.append({"label": label, "title": title, "recommended": recommended})
        i += 1
    return updates


# ── Linux machine checks (firewall / disk-encryption / OS-updates) ────────────
#
# These are the Linux equivalents of the macOS-only firewall / FileVault /
# softwareupdate checks above. They run only on a Linux profile (applies_to in
# _MACHINE_CHECKS); on macOS they emit nothing. All probes are deliberately
# NON-ROOT (DMI sysfs reads, `systemctl is-active`, ufw.conf, lsblk, apt-check
# reading the already-fetched apt lists) — the audit runs as the unprivileged
# `evolve` user on the Linux pod and has no sudo grant for ufw/nft/iptables
# rule dumps, which all require CAP_NET_ADMIN.
#
# Severity bar (the hard part — see docs/threat-model.md):
#   * The pod's security model rests on OS user isolation; the admin server
#     binds 127.0.0.1 and is reached over an SSH tunnel, and the dedicated-VPS
#     topology (§2) is provider-managed at the network + disk layer. A host
#     firewall and host disk-encryption are therefore DEFENSE-IN-DEPTH on a
#     managed VPS, not the load-bearing control — so "off" on a known
#     managed/virtualized host is WARN (verify the provider layer), reserving
#     CRITICAL for an apparently bare-metal host where the host control IS the
#     control (parity with the macOS firewall/FileVault CRITICALs).
#   * The DigitalOcean trap: the live VPS runs `ufw` INACTIVE behind a
#     DigitalOcean Cloud Firewall. _detect_managed_host() reads DMI to spot
#     that case and down-ranks the firewall finding to WARN so the audit does
#     not emit a CRITICAL phantom on a host that is in fact firewalled at the
#     provider edge.

# DMI sysfs nodes carry the hypervisor/cloud vendor strings (world-readable,
# no network). Read in priority order; the first node that matches a known
# marker wins.
_DMI_ID_PATHS = (
    "/sys/class/dmi/id/sys_vendor",
    "/sys/class/dmi/id/product_name",
    "/sys/class/dmi/id/board_vendor",
    "/sys/class/dmi/id/chassis_vendor",
    "/sys/class/dmi/id/bios_vendor",
)

# (substring, display label). A match means "this host is provider-managed or
# virtualized, so a network firewall and disk management likely live a layer
# below this OS" — which down-ranks a missing host firewall / host disk
# encryption from CRITICAL to WARN. Ordered most-specific first; generic
# virtualization markers (QEMU/KVM/OpenStack) are last and yield a generic
# label rather than a provider name.
_MANAGED_HOST_MARKERS: tuple[tuple[str, str], ...] = (
    ("digitalocean", "a DigitalOcean Cloud Firewall"),
    ("amazon ec2", "AWS security groups"),
    ("amazon", "AWS security groups"),
    ("google", "Google Cloud firewall rules"),
    ("microsoft corporation", "Azure network security groups"),
    ("hetzner", "the Hetzner Cloud Firewall"),
    ("vultr", "the Vultr Firewall"),
    ("linode", "the Akamai/Linode Cloud Firewall"),
    ("akamai", "the Akamai/Linode Cloud Firewall"),
    ("alibaba", "Alibaba Cloud security groups"),
    ("oraclecloud", "Oracle Cloud security lists"),
    ("oracle", "Oracle Cloud security lists"),
    ("scaleway", "the Scaleway security group"),
    ("ovh", "the OVH firewall"),
    # Generic virtualization — a hypervisor host (and its network/disk layer)
    # sits below this guest. Softer signal, generic label.
    ("openstack", "the hypervisor/network firewall"),
    ("qemu", "the hypervisor/network firewall"),
    ("kvm", "the hypervisor/network firewall"),
    ("bochs", "the hypervisor/network firewall"),
    ("vmware", "the hypervisor/network firewall"),
    ("virtualbox", "the hypervisor/network firewall"),
    ("xen", "the hypervisor/network firewall"),
)


def _detect_managed_host() -> str | None:
    """Return a human label for the provider/hypervisor network layer if this
    host looks provider-managed or virtualized, else None (apparently
    bare-metal). Used to down-rank missing host firewall / disk-encryption from
    CRITICAL to WARN — on a managed VPS those controls live below the OS.

    Reads only DMI sysfs (local, no network). Returns None if DMI is
    unreadable, which conservatively keeps the bare-metal CRITICAL bar.
    """
    for path in _DMI_ID_PATHS:
        try:
            text = Path(path).read_text(errors="ignore").strip().lower()
        except OSError:
            continue
        if not text:
            continue
        for marker, label in _MANAGED_HOST_MARKERS:
            if marker in text:
                return label
    return None


def _systemctl_is_active(unit: str) -> str | None:
    """`systemctl is-active <unit>` → 'active'|'inactive'|'failed'|... or None
    if systemctl is unavailable / errored. Non-root readable."""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return None
    try:
        r = subprocess.run(
            [systemctl, "is-active", unit],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    # is-active exits non-zero for inactive/failed but still prints the state
    # word on stdout; an unknown unit prints "inactive"/"unknown".
    out = (r.stdout or "").strip()
    return out or None


# ufw's enable flag lives here (mode 0644 root:root, world-readable). Module
# constant so tests can repoint it at a fixture.
_UFW_CONF_PATH = "/etc/ufw/ufw.conf"


def _linux_firewall_state() -> tuple[str, str]:
    """Determine the host firewall state from non-root signals.

    Returns ``(state, detail)`` where state is one of:
      * ``"active"``   — a host firewall is enabled (ufw / firewalld / nftables
                         service running, or ufw.conf ENABLED=yes).
      * ``"inactive"`` — we have a POSITIVE off-signal (ufw ENABLED=no, or a
                         firewall service present-but-inactive).
      * ``"unknown"``  — nothing conclusive: no recognizable tooling, OR only
                         the iptables binary (whose rules we cannot read
                         without CAP_NET_ADMIN). Skip rather than guess — a
                         false CRITICAL on a host that is in fact firewalled
                         via raw iptables is worse than silence.

    We cannot dump rules without CAP_NET_ADMIN, so we read the service state
    (`systemctl is-active`) and ufw's own config flag instead — both readable
    by the unprivileged audit user. ``"inactive"`` requires a positive
    off-signal; the mere presence of a binary is not enough.
    """
    inactive_evidence: list[str] = []

    # ufw — the Ubuntu default. Two non-root signals: the service state and the
    # ENABLED flag in /etc/ufw/ufw.conf (mode 0644 root:root, world-readable).
    if shutil.which("ufw") or Path(_UFW_CONF_PATH).exists():
        if _systemctl_is_active("ufw") == "active":
            return "active", "ufw service active"
        try:
            conf = Path(_UFW_CONF_PATH).read_text(errors="ignore")
            m = re.search(r"^\s*ENABLED\s*=\s*(\w+)", conf, re.IGNORECASE | re.MULTILINE)
            if m and m.group(1).lower() == "yes":
                return "active", "ufw ENABLED=yes (/etc/ufw/ufw.conf)"
            if m:
                inactive_evidence.append(f"ufw ENABLED={m.group(1).lower()}")
        except OSError as e:
            logger.debug("audit: ufw.conf read failed: %s", e)

    # firewalld — the RHEL/Fedora default, also packaged on Debian.
    fwd = _systemctl_is_active("firewalld")
    if fwd == "active":
        return "active", "firewalld service active"
    if fwd in ("inactive", "failed"):
        inactive_evidence.append(f"firewalld {fwd}")

    # nftables.service loads /etc/nftables.conf at boot.
    nft = _systemctl_is_active("nftables")
    if nft == "active":
        return "active", "nftables service active"
    if nft in ("inactive", "failed"):
        inactive_evidence.append(f"nftables {nft}")

    if inactive_evidence:
        return "inactive", "; ".join(inactive_evidence)

    # No positive signal either way — including the iptables-only case, whose
    # rules we cannot read unprivileged. Stay silent.
    return "unknown", "no conclusive host-firewall state (rules need root to read)"


def _check_linux_firewall(config: dict | None = None) -> list[Finding]:
    """Flag a Linux host with no active host firewall.

    Severity:
      * active                                   → ok
      * inactive + operator-accepted             → ok
      * inactive + managed/virtualized host      → warn (provider firewall
                                                    likely fronts the host;
                                                    verify it is restrictive)
      * inactive + apparently bare-metal         → critical (parity with the
                                                    macOS firewall CRITICAL)
      * no firewall tooling found                → skipped

    The operator can accept a deliberately-off host firewall via
    network.json::policy_acceptances["machine.firewall_off"].
    """
    state, detail = _linux_firewall_state()
    if state == "active":
        return [Finding(level="ok", category="machine", bot_id=None,
                        message="machine: firewall OK (host firewall active)",
                        detail=detail)]
    if state == "unknown":
        return [Finding(
            level="skipped", category="machine", bot_id=None,
            message="machine: Linux firewall check skipped (no firewall tooling found)",
            detail=detail,
        )]

    accepted = policy_acceptance("machine.firewall_off", config)
    if accepted:
        reason = (accepted.get("reason") or "").strip() or "no reason recorded"
        return [Finding(
            level="ok", category="machine", bot_id=None,
            message=f"machine: host firewall is off — operator-accepted ({reason})",
            detail=detail,
        )]

    managed = _detect_managed_host()
    if managed:
        return [Finding(
            level="warn", category="machine", bot_id=None,
            # Stable core message (no host-specific text) so the mirrored
            # Signal's signature is stable across audit runs.
            message="machine: host firewall inactive (provider firewall likely fronts this host)",
            detail=f"{detail}; provider layer: {managed}",
            what_it_means=(
                f"No host firewall (ufw/firewalld/nftables) is active on this "
                f"host. It appears to be a provider-managed/virtualized host, "
                f"so inbound traffic is most likely filtered by {managed} a "
                f"layer below the OS — which is why this is a warning, not a "
                f"critical. The risk is that the provider firewall is open or "
                f"misconfigured, in which case any service that binds beyond "
                f"loopback (see the listening-ports finding) is exposed."
            ),
            fix_steps=(
                f"1. Verify {managed} only allows the ports you intend "
                "(typically just SSH); tighten it if it is open\n"
                "2. Optionally enable a host firewall as defense-in-depth:\n"
                "     sudo ufw default deny incoming\n"
                "     sudo ufw allow OpenSSH\n"
                "     sudo ufw enable\n"
                "3. If the provider firewall is your deliberate single control, "
                "accept this in network.json:\n"
                "   \"policy_acceptances\": {\n"
                "     \"machine.firewall_off\": {\n"
                "       \"reason\": \"fronted by <provider> cloud firewall\",\n"
                "       \"accepted_at\": \"2026-06-26\",\n"
                "       \"accepted_by\": \"<your handle>\"\n"
                "     }\n"
                "   }"
            ),
        )]

    return [Finding(
        level="critical", category="machine", bot_id=None,
        message="🔴 CRITICAL: Linux host firewall is off (no ufw/firewalld/nftables active)",
        detail=detail,
        what_it_means=(
            "No host firewall is active and this host does not appear to be "
            "behind a provider/hypervisor network firewall. Any service that "
            "binds beyond loopback — a bot gateway, a debug server, SSH — is "
            "reachable from the network. The pod's security model assumes the "
            "admin server is only reachable over an SSH tunnel; an open host "
            "with no firewall breaks that assumption."
        ),
        fix_steps=(
            "1. Enable a host firewall that defaults to deny-inbound:\n"
            "     sudo ufw default deny incoming\n"
            "     sudo ufw default allow outgoing\n"
            "     sudo ufw allow OpenSSH\n"
            "     sudo ufw enable\n"
            "2. Confirm SSH still works from a second session BEFORE closing "
            "this one\n"
            "3. If a firewall is intentionally not used (e.g. an isolated lab "
            "network), declare it in network.json:\n"
            "   \"policy_acceptances\": {\n"
            "     \"machine.firewall_off\": {\n"
            "       \"reason\": \"why\",\n"
            "       \"accepted_at\": \"2026-06-26\",\n"
            "       \"accepted_by\": \"<your handle>\"\n"
            "     }\n"
            "   }"
        ),
    )]


def _linux_luks_present() -> bool | None:
    """Whether any block device is LUKS/dm-crypt encrypted, via `lsblk`.

    Returns True/False, or None if lsblk is unavailable (skip). lsblk is
    readable by an unprivileged user and reports FSTYPE ``crypto_LUKS`` on the
    backing device and TYPE ``crypt`` on the opened mapper — either is proof of
    at-rest encryption.
    """
    lsblk = shutil.which("lsblk")
    if not lsblk:
        return None
    try:
        r = subprocess.run(
            [lsblk, "--noheadings", "--output", "TYPE,FSTYPE"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    text = (r.stdout or "").lower()
    if "crypto_luks" in text or re.search(r"(^|\s)crypt(\s|$)", text, re.MULTILINE):
        return True
    return False


def _check_linux_disk_encryption(config: dict | None = None) -> list[Finding]:
    """Flag a Linux host whose root disk is not encrypted (no LUKS).

    Severity (see docs/threat-model.md §6.2 — secrets-at-rest is the accepted
    floor, single-tenant assumption carries the root-capable-local residual):
      * LUKS present                          → ok
      * absent + operator-accepted            → ok
      * absent + managed/virtualized host     → warn (the disk is
                                                provider-managed; physical
                                                access is the provider's —
                                                NOT critical on a VPS)
      * absent + apparently bare-metal        → critical (parity with the
                                                macOS FileVault CRITICAL — a
                                                physically-accessible host whose
                                                stolen disk reveals every
                                                secret)

    Accept a deliberately-unencrypted disk via
    network.json::policy_acceptances["machine.disk_encryption_off"].
    """
    present = _linux_luks_present()
    if present is None:
        return [Finding(
            level="skipped", category="machine", bot_id=None,
            message="machine: disk-encryption check skipped (lsblk unavailable)",
        )]
    if present:
        return [Finding(level="ok", category="machine", bot_id=None,
                        message="machine: disk encryption OK (LUKS volume present)")]

    accepted = policy_acceptance("machine.disk_encryption_off", config)
    if accepted:
        reason = (accepted.get("reason") or "").strip() or "no reason recorded"
        return [Finding(
            level="ok", category="machine", bot_id=None,
            message=f"machine: disk is not encrypted — operator-accepted ({reason})",
        )]

    managed = _detect_managed_host()
    if managed:
        return [Finding(
            level="warn", category="machine", bot_id=None,
            message="machine: disk not encrypted (provider-managed VPS disk)",
            what_it_means=(
                "No LUKS-encrypted volume was found. This host appears to be a "
                "provider-managed/virtualized VPS, where the physical disk is "
                "the provider's responsibility and you cannot boot from "
                "external media to read it — so this is a warning, not a "
                "critical. Per the threat model (§6.2), secrets-at-rest on disk "
                "is the accepted floor, carried by the single-tenant "
                "assumption. The residual is that a provider/hypervisor "
                "operator, or a leaked disk snapshot, could read unencrypted "
                "bot secrets."
            ),
            fix_steps=(
                "1. Prefer provider-side encryption-at-rest if offered (many "
                "VPS providers encrypt volumes by default — confirm in the "
                "provider console)\n"
                "2. For new hosts, provision with full-disk encryption (LUKS) "
                "from the installer; retrofitting LUKS on a live VPS root is "
                "disruptive\n"
                "3. If you accept the provider's at-rest posture, declare it in "
                "network.json:\n"
                "   \"policy_acceptances\": {\n"
                "     \"machine.disk_encryption_off\": {\n"
                "       \"reason\": \"provider-managed at-rest encryption\",\n"
                "       \"accepted_at\": \"2026-06-26\",\n"
                "       \"accepted_by\": \"<your handle>\"\n"
                "     }\n"
                "   }"
            ),
        )]

    return [Finding(
        level="critical", category="machine", bot_id=None,
        message="🔴 CRITICAL: disk is not encrypted (no LUKS volume)",
        what_it_means=(
            "No LUKS-encrypted volume was found and this host appears to be "
            "physically accessible (not a managed VPS). Anyone with physical "
            "access to the machine or its disk — theft, repair, decommission — "
            "can read every bot's transcripts, API keys, OAuth tokens, and "
            "workspace files by mounting the disk elsewhere."
        ),
        fix_steps=(
            "1. The robust fix is full-disk encryption (LUKS), which generally "
            "requires a reinstall — provision the host with LUKS from the "
            "installer and restore from backup\n"
            "2. If the host is in a physically-secured location and you accept "
            "the risk, declare it in network.json:\n"
            "   \"policy_acceptances\": {\n"
            "     \"machine.disk_encryption_off\": {\n"
            "       \"reason\": \"physically-secured host\",\n"
            "       \"accepted_at\": \"2026-06-26\",\n"
            "       \"accepted_by\": \"<your handle>\"\n"
            "     }\n"
            "   }"
        ),
    )]


def _linux_unattended_upgrades_enabled() -> bool:
    """True if unattended-upgrades is configured to auto-apply. Non-root.

    Reads /etc/apt/apt.conf.d/*auto-upgrades for
    ``APT::Periodic::Unattended-Upgrade "1"`` (the canonical enable flag), and
    falls back to the unit's enable state.
    """
    for name in ("20auto-upgrades", "10periodic"):
        try:
            conf = Path(f"/etc/apt/apt.conf.d/{name}").read_text(errors="ignore")
        except OSError:
            continue
        if re.search(
            r'APT::Periodic::Unattended-Upgrade\s+"1"', conf
        ):
            return True
    systemctl = shutil.which("systemctl")
    if systemctl:
        try:
            r = subprocess.run(
                [systemctl, "is-enabled", "unattended-upgrades"],
                capture_output=True, text=True, timeout=5,
            )
            if (r.stdout or "").strip() == "enabled":
                return True
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug("audit: systemctl is-enabled unattended-upgrades failed: %s", e)
    return False


def _linux_pending_updates() -> tuple[int | None, int | None, str]:
    """Pending package updates as ``(security_count, regular_count, source)``.

    Reads already-fetched apt metadata only — never runs ``apt-get update``
    (that needs root + network). Prefers update-notifier's ``apt-check`` (which
    prints ``regular;security`` to stderr), falling back to parsing
    ``apt list --upgradable`` for ``-security`` suites. Returns
    ``(None, None, source)`` when the host has no apt (non-Debian distro) — the
    caller then skips.
    """
    apt_check = "/usr/lib/update-notifier/apt-check"
    if Path(apt_check).exists():
        try:
            r = subprocess.run(
                [apt_check],
                capture_output=True, text=True, timeout=30,
            )
            # apt-check writes "<total>;<security>" to stderr (and
            # --human-readable prose to stdout); the machine form is on stderr.
            # The first number is TOTAL upgradable, the second is the security
            # subset — so non-security = total - security.
            raw = (r.stderr or r.stdout or "").strip()
            m = re.match(r"^\s*(\d+)\s*;\s*(\d+)\s*$", raw)
            if m:
                total = int(m.group(1))
                security = int(m.group(2))
                return security, max(total - security, 0), "apt-check"
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug("audit: apt-check failed: %s", e)

    apt = shutil.which("apt")
    if apt:
        try:
            r = subprocess.run(
                [apt, "list", "--upgradable"],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "LC_ALL": "C"},
            )
            if r.returncode == 0:
                return _parse_apt_upgradable(r.stdout or "")
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug("audit: apt list --upgradable failed: %s", e)

    return None, None, "no apt"


def _parse_apt_upgradable(output: str) -> tuple[int, int, str]:
    """Parse `apt list --upgradable` into (security, regular, source).

    Each upgradable line looks like:
        pkg/jammy-security 1.2-3 amd64 [upgradable from: 1.2-2]
    A ``-security`` (or ``-securi`` truncated) suite in the origin marks a
    security update. Lines without that suite are counted as regular.
    """
    security = 0
    regular = 0
    for line in output.splitlines():
        line = line.strip()
        if not line or "/" not in line or "[upgradable" not in line:
            continue
        # The suite list is the token right after "pkg/".
        suite = line.split("/", 1)[1].split(None, 1)[0].lower()
        if "-security" in suite or suite.endswith("-security"):
            security += 1
        else:
            regular += 1
    return security, regular, "apt list"


def _check_linux_os_updates(config: dict | None = None) -> list[Finding]:
    """Flag pending Linux package updates, security ones loudest.

    Severity (parity with the macOS Security-Update CRITICAL):
      * pending security updates, unattended-upgrades OFF  → critical
      * pending security updates, unattended-upgrades ON   → warn (they will be
                                                             auto-applied; not a
                                                             standing exposure)
      * pending regular updates                            → warn
      * pending regular updates + operator-accepted        → ok
      * none pending                                       → ok
      * no apt (non-Debian distro)                         → skipped

    Operator can defer NON-security pending updates via
    network.json::policy_acceptances["machine.os_updates_pending"]; security
    updates always fire regardless (paging on a published-CVE patch is the
    point of the check). Like the macOS check this never runs ``apt-get
    update`` — it reads the already-fetched apt metadata, so it is a cheap
    local read with no network and no caching needed.
    """
    security, regular, source = _linux_pending_updates()
    if security is None and regular is None:
        return [Finding(
            level="skipped", category="machine", bot_id=None,
            message="machine: Linux update check skipped (apt not available)",
        )]
    security = security or 0
    regular = regular or 0

    if security == 0 and regular == 0:
        return [Finding(level="ok", category="machine", bot_id=None,
                        message="machine: Linux package updates OK (none pending)")]

    detail = f"pending: {security} security, {regular} other ({source})"

    if security > 0:
        if _linux_unattended_upgrades_enabled():
            return [Finding(
                level="warn", category="machine", bot_id=None,
                message="machine: pending Linux security update(s) — unattended-upgrades will auto-apply",
                detail=detail,
                what_it_means=(
                    "Security updates are pending, but unattended-upgrades is "
                    "enabled on this host and applies security patches "
                    "automatically (typically within a day). Surfaced as a "
                    "warning so you can confirm the auto-apply is healthy; if "
                    "it keeps lagging, the timer may be stuck."
                ),
                fix_steps=(
                    "1. Confirm the auto-upgrade timer is running:\n"
                    "     systemctl status apt-daily-upgrade.timer\n"
                    "2. To apply now rather than wait:\n"
                    "     sudo unattended-upgrade -v\n"
                    "3. Review the log if updates are not landing:\n"
                    "     less /var/log/unattended-upgrades/unattended-upgrades.log"
                ),
            )]
        return [Finding(
            level="critical", category="machine", bot_id=None,
            message="🔴 CRITICAL: pending Linux security update(s)",
            detail=detail,
            what_it_means=(
                "One or more security updates are pending and this host is NOT "
                "configured to apply them automatically (unattended-upgrades "
                "is off). Security updates patch publicly-disclosed "
                "vulnerabilities; the window between publication and patch is "
                "the period of highest exploit risk."
            ),
            fix_steps=(
                "1. Apply pending updates now:\n"
                "     sudo apt-get update && sudo apt-get upgrade\n"
                "2. Reboot if a kernel or libc update was installed\n"
                "3. Enable automatic security updates so this does not recur:\n"
                "     sudo apt-get install unattended-upgrades\n"
                "     sudo dpkg-reconfigure -plow unattended-upgrades"
            ),
        )]

    # regular updates only
    accepted = policy_acceptance("machine.os_updates_pending", config)
    if accepted:
        reason = (accepted.get("reason") or "").strip() or "no reason recorded"
        return [Finding(
            level="ok", category="machine", bot_id=None,
            message=(
                f"machine: {regular} non-security Linux update(s) pending — "
                f"operator-accepted ({reason})"
            ),
            detail=detail,
        )]
    return [Finding(
        level="warn", category="machine", bot_id=None,
        message="machine: pending Linux package update(s)",
        detail=detail,
        what_it_means=(
            "Non-security package updates are available. Not as time-critical "
            "as a security patch, but worth installing within the next few "
            "days to limit drift."
        ),
        fix_steps=(
            "1. Apply when convenient:\n"
            "     sudo apt-get update && sudo apt-get upgrade\n"
            "2. If you are intentionally deferring (release freeze, etc.), "
            "declare it in network.json:\n"
            "   \"policy_acceptances\": {\n"
            "     \"machine.os_updates_pending\": {\n"
            "       \"reason\": \"deferring until release-freeze ends\",\n"
            "       \"accepted_at\": \"2026-06-26\",\n"
            "       \"accepted_by\": \"<your handle>\"\n"
            "     }\n"
            "   }\n"
            "Security updates still fire regardless of this acceptance."
        ),
    )]


def _check_admin_user_gateway(config: dict) -> list[Finding]:
    """Fire CRITICAL if the pod admin user is running an openclaw gateway.

    Security_bot's highest-stakes invariant. The admin user (``pod_admin_user`` on
    Pod_admin's pod) has sudoers privileges to manage every bot's
    installation. An openclaw gateway running under that account would
    let an LLM execute commands as the admin — effectively granting the
    bot sudo over its own and every sibling bot's `.openclaw` tree, the
    `evolve` user, the launchd configuration, and the deploy checkout.
    This is a never-do invariant: gateways belong under member-bot
    users or the dedicated ``evo`` account, never the admin login.

    The admin user is resolved from ``network.json::admin_user``; if
    unset (e.g. pre-setup or a deploy-mode pod), the check skips.
    """
    admin_user = (config or {}).get("admin_user")
    if not admin_user:
        return [Finding(
            level="skipped", category="machine", bot_id=None,
            message="machine: admin-user gateway check skipped (admin_user not set in network.json)",
        )]

    try:
        r = subprocess.run(
            ["ps", "auxww"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return [Finding(
            level="skipped", category="machine", bot_id=None,
            message="machine: admin-user gateway check skipped (ps unavailable)",
        )]
    if r.returncode != 0:
        return [Finding(
            level="skipped", category="machine", bot_id=None,
            message="machine: admin-user gateway check skipped (ps failed)",
        )]

    offending: list[str] = []
    for line in r.stdout.splitlines():
        if _is_gateway_proc_line(line, admin_user):
            # Trim the line for the detail field — the full ps line
            # contains the launch command which is useful but long.
            offending.append(line[:300])

    if not offending:
        return [Finding(level="ok", category="machine", bot_id=None,
                        message=f"machine: no openclaw gateway running as {admin_user}")]

    return [Finding(
        level="critical", category="machine", bot_id=None,
        message=f"🔴 CRITICAL: openclaw gateway running as admin user {admin_user!r}",
        detail="\n".join(offending),
        what_it_means=(
            f"The admin user {admin_user!r} has sudo privileges to manage "
            "every bot, the evolve service, launchd, and the deploy "
            "checkout. An openclaw gateway running under that account "
            "would let an LLM (or anyone who can prompt-inject one) "
            "execute commands at that privilege level — effectively "
            "granting the bot root over the entire pod. Bots must run "
            "under member-bot users or the dedicated evo account, never "
            "the admin login."
        ),
        fix_steps=(
            "1. Stop the offending gateway IMMEDIATELY:\n"
            f"   sudo launchctl print user/$(id -u {admin_user}) | grep openclaw\n"
            "2. Disable the launchd plist that started it (look under "
            f"~{admin_user}/Library/LaunchAgents/ and /Library/LaunchAgents/)\n"
            "3. Audit recent activity in the admin user's shell history "
            "and the gateway's exec log for anything that ran with sudo\n"
            "4. Rotate any credentials the admin user has access to "
            "(SSH keys, API tokens, 1Password vault)\n"
            "5. Re-deploy the bot to its intended member-bot user via "
            "`sudo evolve-admin deploy <bot_id>`"
        ),
    )]


def _check_ssh_config() -> list[Finding]:
    findings: list[Finding] = []
    try:
        r = subprocess.run(
            ["sudo", "sshd", "-T"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return [Finding(
                level="skipped", category="machine", bot_id=None,
                message="machine: sshd config read denied — sshd -T failed",
                detail="audit user likely lacks sudo grant for sshd -T",
            )]
        cfg_text = r.stdout.lower()
        if "passwordauthentication yes" in cfg_text:
            findings.append(Finding(
                level="critical", category="machine", bot_id=None,
                message="🔴 CRITICAL: SSH PasswordAuthentication is enabled",
            ))
        else:
            findings.append(Finding(level="ok", category="machine", bot_id=None,
                                    message="machine: SSH PasswordAuthentication OK"))
        if "permitrootlogin yes" in cfg_text or "permitrootlogin without-password" in cfg_text:
            findings.append(Finding(
                level="critical", category="machine", bot_id=None,
                message="🔴 CRITICAL: SSH PermitRootLogin is enabled",
            ))
        else:
            findings.append(Finding(level="ok", category="machine", bot_id=None,
                                    message="machine: SSH PermitRootLogin OK"))
    except (subprocess.TimeoutExpired, OSError):
        findings.append(Finding(
            level="warn", category="machine", bot_id=None,
            message="machine: cannot read sshd config",
        ))
    return findings


def _known_bot_users(config: dict) -> set[str]:
    """Return the set of macOS usernames that back known bots in network.json.

    Each member's UNIX user is `bots[member].user` (or member id if unmapped)
    when one bot lives on a personal/shared account. Auto-allowlisting these
    prevents the user-account audit from firing 🔴 CRITICAL every cycle when
    a new bot is added but the baseline file hasn't been refreshed.
    """
    members = config.get("members") or []
    bots = config.get("bots") or {}
    users: set[str] = set()
    for m in members:
        info = bots.get(m) if isinstance(bots, dict) else None
        u = (info or {}).get("user") if isinstance(info, dict) else None
        users.add(u or m)
    return users


def _check_user_accounts(shared_dir: Path, config: dict | None = None) -> list[Finding]:
    """Compare current macOS users against stored baseline. New accounts → CRITICAL.

    Bot users from `config['bots'][...]['user']` (resolved via
    _known_bot_users) are treated as baseline-equivalent: adding/removing a
    bot in network.json no longer triggers a CRITICAL alert. The baseline
    file remains the source of truth for non-bot accounts (admin user,
    forge, etc.).
    """
    findings: list[Finding] = []
    baseline_path = shared_dir / "security" / "user-accounts.baseline"

    try:
        r = subprocess.run(
            ["dscl", ".", "-list", "/Users"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return [Finding(level="warn", category="machine", bot_id=None,
                            message="machine: cannot list user accounts (dscl failed)")]
        # Filter system accounts (start with _ or are well-known system users)
        _SYSTEM_USERS = {"daemon", "nobody", "root", "Guest"}
        current_users = sorted(
            u for u in r.stdout.strip().splitlines()
            if u and not u.startswith("_") and u not in _SYSTEM_USERS
        )
        current_set = set(current_users)
    except (subprocess.TimeoutExpired, OSError):
        return [Finding(level="warn", category="machine", bot_id=None,
                        message="machine: cannot list user accounts")]

    if not baseline_path.exists():
        try:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text("\n".join(current_users))
            findings.append(Finding(level="ok", category="machine", bot_id=None,
                                    message=f"machine: user account baseline created ({len(current_users)} users)"))
        except OSError:
            findings.append(Finding(level="warn", category="machine", bot_id=None,
                                    message="machine: cannot write user account baseline"))
        return findings

    baseline_users = set(baseline_path.read_text().strip().splitlines())
    # Auto-allowlist any user that backs a bot listed in network.json. The
    # baseline file becomes a fallback for users that don't appear in the
    # bot list (admin user, forge, etc.); bots get treated as known by
    # membership. Removing a bot from network.json doesn't trigger the
    # "removed" warning either — the user account still exists, just isn't
    # in active service anymore.
    known_bot_users = _known_bot_users(config or {})
    allowed_users = baseline_users | known_bot_users
    new_users = current_set - allowed_users
    removed_users = baseline_users - current_set - known_bot_users

    if new_users:
        findings.append(Finding(
            level="critical", category="machine", bot_id=None,
            message=f"🔴 CRITICAL: New user account(s) detected: {', '.join(sorted(new_users))}",
        ))
    if removed_users:
        findings.append(Finding(
            level="warn", category="machine", bot_id=None,
            message=f"machine: User account(s) removed: {', '.join(sorted(removed_users))}",
        ))
    if not new_users and not removed_users:
        findings.append(Finding(level="ok", category="machine", bot_id=None,
                                message=f"machine: user accounts OK ({len(current_set)} users)"))

    return findings


def _check_listening_ports(shared_dir: Path) -> list[Finding]:
    """Compare listening ports against stored baseline. Unexpected new ports → WARN."""
    findings: list[Finding] = []
    baseline_path = shared_dir / "security" / "listening-ports.baseline"

    try:
        r = subprocess.run(
            ["sudo", "lsof", "-iTCP", "-sTCP:LISTEN", "-n", "-P"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return [Finding(level="skipped", category="machine", bot_id=None,
                            message="machine: listening-ports check denied — sudo lsof failed",
                            detail="audit user likely lacks sudo grant for lsof")]
        # Extract unique port numbers from lsof output
        ports: set[str] = set()
        for line in r.stdout.splitlines()[1:]:  # skip header
            parts = line.split()
            if len(parts) >= 9:
                addr = parts[8]  # e.g. *:22 or 127.0.0.1:5050
                port = addr.rsplit(":", 1)[-1]
                if port.isdigit():
                    ports.add(port)
        current_ports = sorted(ports, key=int)
    except (subprocess.TimeoutExpired, OSError):
        return [Finding(level="skipped", category="machine", bot_id=None,
                        message="machine: listening-ports check skipped (lsof unavailable)")]

    if not baseline_path.exists():
        try:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text("\n".join(current_ports))
            findings.append(Finding(level="ok", category="machine", bot_id=None,
                                    message=f"machine: port baseline created ({len(current_ports)} ports)"))
        except OSError:
            findings.append(Finding(level="warn", category="machine", bot_id=None,
                                    message="machine: cannot write port baseline"))
        return findings

    baseline_ports = set(baseline_path.read_text().strip().splitlines())
    new_ports = set(current_ports) - baseline_ports

    if new_ports:
        findings.append(Finding(
            level="warn", category="machine", bot_id=None,
            message=f"machine: new listening port(s) since baseline: {', '.join(sorted(new_ports, key=int))}",
        ))
    else:
        findings.append(Finding(level="ok", category="machine", bot_id=None,
                                message=f"machine: listening ports OK ({len(current_ports)} ports)"))

    return findings


def _read_openclaw_version(oc_path: Path) -> str | None:
    """Return the openclaw binary's reported version string, or None."""
    try:
        r = subprocess.run(
            [str(oc_path), "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        # Output is typically "openclaw 2026.4.29" or similar — first line, trimmed.
        first_line = (r.stdout or r.stderr).splitlines()[0].strip() if (r.stdout or r.stderr) else ""
        return first_line or None
    except (subprocess.TimeoutExpired, OSError):
        return None


_OC_BINARY_CANDIDATES: list[Path] = [
    Path("/opt/homebrew/bin/openclaw"),
    Path("/usr/local/bin/openclaw"),
]


def _check_oc_binary_mtime(shared_dir: Path) -> list[Finding]:
    """Flag if the openclaw binary mtime has changed unexpectedly.

    The baseline stores both mtime and version. When mtime changes we
    re-read the version: if the version also changed, it's a clean upgrade
    (auto-refresh the baseline, emit OK). If mtime changed without a
    version bump, that's a suspicious replacement — emit warn.
    """
    baseline_path = shared_dir / "security" / "oc-binary-mtime.baseline"
    oc_path = next((p for p in _OC_BINARY_CANDIDATES if p.exists()), None)

    if oc_path is None:
        return [Finding(level="warn", category="machine", bot_id=None,
                        message="machine: openclaw binary not found at expected paths")]

    try:
        mtime = str(int(oc_path.stat().st_mtime))
    except OSError:
        return [Finding(level="warn", category="machine", bot_id=None,
                        message="machine: cannot stat openclaw binary")]

    current_version = _read_openclaw_version(oc_path)

    def _write_baseline(mt: str, ver: str | None) -> bool:
        try:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(json.dumps({"mtime": mt, "version": ver}))
            return True
        except OSError:
            return False

    if not baseline_path.exists():
        if _write_baseline(mtime, current_version):
            return [Finding(level="ok", category="machine", bot_id=None,
                            message=f"machine: OC binary baseline created ({oc_path}, version={current_version or 'unknown'})")]
        return [Finding(level="warn", category="machine", bot_id=None,
                        message="machine: cannot write OC binary baseline")]

    # Backward-compat: baseline was previously a bare mtime string. Parse
    # either shape — JSON dict if available, raw mtime otherwise.
    raw = baseline_path.read_text().strip()
    stored_mtime: str = raw
    stored_version: str | None = None
    try:
        baseline = json.loads(raw)
        if isinstance(baseline, dict):
            stored_mtime = str(baseline.get("mtime") or "").strip()
            stored_version = baseline.get("version")
    except json.JSONDecodeError:
        pass

    if mtime == stored_mtime:
        return [Finding(level="ok", category="machine", bot_id=None,
                        message=f"machine: OC binary mtime OK")]

    # Mtime changed — check if version moved with it.
    if current_version and stored_version and current_version != stored_version:
        # Clean upgrade. Refresh the baseline and emit OK.
        _write_baseline(mtime, current_version)
        return [Finding(level="ok", category="machine", bot_id=None,
                        message=f"machine: openclaw upgraded ({stored_version} → {current_version}); baseline refreshed")]
    if current_version and not stored_version:
        # Migrating from the old bare-mtime baseline shape. We can't tell
        # whether this is a real change; record the current version and
        # treat as an upgrade for this one transition.
        _write_baseline(mtime, current_version)
        return [Finding(level="ok", category="machine", bot_id=None,
                        message=f"machine: openclaw baseline migrated to versioned format (version={current_version})")]

    # mtime changed without a version delta — that's the suspicious case.
    return [Finding(
        level="warn", category="machine", bot_id=None,
        message="machine: openclaw binary mtime changed without version delta",
        detail=f"was={stored_mtime} now={mtime} version={current_version or 'unknown'} path={oc_path}",
    )]


# ── 4. (retired) Cost audit ───────────────────────────────────────────────────
#
# audit_cost() was removed in Phase E1 of docs/spec-alert-subscriptions-
# 2026-05-10.md. The audit emitter framed spend overages as "🔴 CRITICAL:
# Security Audit Findings", which mislabeled a usage notice as a security
# issue. The canonical operator-facing path for daily spend thresholds is
# spend_alert.py (catalog_event cost.daily_threshold), which already gates
# via the Subscriptions UI. Removing audit_cost stops the audit emitter
# from including spend findings in its "Security Audit" batch entirely;
# the Cost category in the Subscriptions UI is the single place to
# manage that notification.


# ── 5. Proposal volume audit ──────────────────────────────────────────────────

def audit_proposals(shared_dir: Path, config: dict) -> list[Finding]:
    """Check for proposal volume spikes and consecutive rollbacks."""
    findings: list[Finding] = []
    proposals_dir = shared_dir / "proposals"

    # Volume spike: count proposals whose `created_at` ISO date is today (UTC,
    # matching how Proposal.created_at is written). Filename and mtime are
    # both unreliable: filenames are UUIDs (no date prefix), and mtime moves
    # whenever state-history is appended, so a days-old proposal looks "new"
    # the moment it's transitioned. created_at is the authoritative birth time.
    today_iso = datetime.now(timezone.utc).date().isoformat()
    limit = config.get("thresholds", {}).get("maxProposalsPerDay", 100)
    for stage in ("pending", "approved"):
        stage_dir = proposals_dir / stage
        if not stage_dir.exists():
            continue
        today_count = 0
        for f in stage_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            created = data.get("created_at")
            if isinstance(created, str) and created[:10] == today_iso:
                today_count += 1
        if today_count > limit:
            findings.append(Finding(
                level="warn", category="proposal", bot_id=None,
                message=f"proposals/{stage}: {today_count} proposals today exceeds limit {limit}",
            ))

    # Consecutive rollback detection
    results_dir = proposals_dir / "apply-results"
    if results_dir.exists():
        rollback_counts: dict[str, int] = {}
        for f in sorted(results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
            try:
                r = json.loads(f.read_text())
                bot = r.get("bot_id", "?")
                if r.get("rollback_triggered"):
                    rollback_counts[bot] = rollback_counts.get(bot, 0) + 1
            except (OSError, json.JSONDecodeError):
                pass
        for bot_id, count in rollback_counts.items():
            if count >= 3:
                findings.append(Finding(
                    level="warn", category="proposal", bot_id=bot_id,
                    message=f"{bot_id}: {count} consecutive rollbacks in recent apply-results",
                ))

    if not findings:
        findings.append(Finding(level="ok", category="proposal", bot_id=None,
                                message="proposals: volume and rollback checks OK"))
    return findings


# ── 6. Shell config audit ─────────────────────────────────────────────────────

def _hash_bot_zshrc(bot_id: str) -> str | None:
    """Return a hex hash of the bot's .zshrc, the literal "absent" if the file
    doesn't exist, or None if the file exists but evolve can't read it.

    Tries a direct read first (most bots' .zshrc is 0644). Falls back to
    `sudo /bin/cat` for bots whose .zshrc is locked down — that fallback only
    works when /etc/sudoers.d/evolve grants /bin/cat for the path; the audit
    treats a missing grant as "unreadable" and surfaces a WARN.
    """
    zshrc = _bot_home(bot_id) / ".zshrc"
    try:
        return hashlib.sha256(zshrc.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "absent"
    except PermissionError:
        pass
    except OSError:
        return None
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(zshrc)],
            capture_output=True, timeout=5,
        )
        if r.returncode == 0:
            return hashlib.sha256(r.stdout).hexdigest()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def audit_shell_config(bot_ids: list[str], shared_dir: Path) -> list[Finding]:
    """Hash .zshrc for each bot user; baseline on first run; alert on change."""
    findings: list[Finding] = []
    baseline_path = shared_dir / "security" / "baselines" / "shell-hashes.json"

    try:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    # Load existing baseline
    baseline: dict[str, str] = {}
    if baseline_path.exists():
        try:
            baseline = json.loads(baseline_path.read_text())
        except (json.JSONDecodeError, OSError):
            baseline = {}

    updated_baseline = dict(baseline)

    for bot_id in bot_ids:
        # Three possible states: hex hash, "absent", or None (unreadable).
        # Distinguishing "unreadable" from "absent" matters — unreadable is a
        # WARN that audit can't verify the file; absent matches a clean baseline.
        state = _hash_bot_zshrc(bot_id)
        stored = baseline.get(bot_id)

        if stored is None:
            # No baseline for this bot yet
            if state is None:
                findings.append(Finding(
                    level="skipped", category="identity", bot_id=bot_id,
                    message=f"{bot_id}: .zshrc read denied — cannot establish baseline",
                    detail="audit user lacks ACL/sudo read for the bot's .zshrc",
                ))
                # Don't write a placeholder; next run gets another chance
            elif state == "absent":
                updated_baseline[bot_id] = "absent"
                findings.append(Finding(
                    level="ok", category="identity", bot_id=bot_id,
                    message=f"{bot_id}: .zshrc absent — baseline recorded as absent",
                ))
            else:
                updated_baseline[bot_id] = state
                findings.append(Finding(
                    level="ok", category="identity", bot_id=bot_id,
                    message=f"{bot_id}: .zshrc baseline created",
                ))
        elif state is None:
            findings.append(Finding(
                level="warn", category="identity", bot_id=bot_id,
                message=f"{bot_id}: .zshrc unreadable",
            ))
        elif state == stored:
            # Covers both "same hash" and "still absent" — they share the idiom.
            findings.append(Finding(
                level="ok", category="identity", bot_id=bot_id,
                message=f"{bot_id}: .zshrc OK",
            ))
        elif stored == "absent":
            # File appeared since baseline
            updated_baseline[bot_id] = state
            findings.append(Finding(
                level="warn", category="identity", bot_id=bot_id,
                message=f"{bot_id}: .zshrc appeared — new baseline established",
                detail=f"hash={state[:12]}",
            ))
        elif state == "absent":
            # File disappeared since baseline
            findings.append(Finding(
                level="warn", category="identity", bot_id=bot_id,
                message=f"{bot_id}: .zshrc deleted (baseline says present)",
                detail=f"baseline={stored[:12]}",
            ))
        else:
            findings.append(Finding(
                level="critical", category="identity", bot_id=bot_id,
                message=f"🔴 CRITICAL: {bot_id} .zshrc hash changed since baseline",
                detail=f"baseline={stored[:12]} current={state[:12]}",
            ))

    # Persist updated baseline
    try:
        baseline_path.write_text(json.dumps(updated_baseline, indent=2))
    except OSError:
        pass

    return findings


# ── 7. Script inventory audit ─────────────────────────────────────────────────

# Map "kind" → baseline file (relative to {shared_dir}/security/baselines/).
# Each baseline is a JSON dict keyed by bot_id; reset_baseline drops the
# bot's entry so the next audit run rebuilds it from observed state.
_PER_BOT_BASELINES: dict[str, str] = {
    "scripts": "scripts.json",
    "shell": "shell-hashes.json",
    "cron-jobs": "cron-jobs.json",
}


def reset_baseline(bot_id: str, kind: str, shared_dir: Path) -> bool:
    """Drop a bot's entry from a per-bot audit baseline.

    Used by deploy hooks (when evolve installs/removes workspace files,
    so the baseline drift the change creates isn't a real signal) and by
    operator-driven "accept current state as new baseline" affordances.

    Returns True if the bot had an entry and it was removed; False if no
    entry existed, the kind is unknown, or the write failed.
    """
    rel = _PER_BOT_BASELINES.get(kind)
    if rel is None:
        return False
    path = shared_dir / "security" / "baselines" / rel
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict) or bot_id not in data:
        return False
    del data[bot_id]
    try:
        path.write_text(json.dumps(data, indent=2))
        return True
    except OSError:
        return False


def _find_is_permission_denied(stderr: str | None) -> bool:
    """True when a non-zero ``find`` is purely an EACCES on the search root.

    ``find`` prints ``find: '<path>': Permission denied`` (and exits non-zero)
    when it cannot traverse a directory — exactly the shape evolve hits when
    the OC gateway's 0700-harden has clamped ``.openclaw``'s ACL mask. We treat
    that as a transient, self-healing condition (see audit_script_inventory)
    rather than a real audit failure. Any OTHER stderr (a genuinely broken
    find, a missing binary) is NOT suppressed."""
    if not stderr:
        return False
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    if not lines:
        return False
    # Every emitted line must be a permission-denied line — one unexplained
    # error and we fall through to the loud finding.
    return all("permission denied" in ln.lower() for ln in lines)


# How many CONSECUTIVE permission-denied `find` cycles to ride quietly before
# escalating to a warn. The periodic reassert (pod_perms_drift_monitor) heals a
# transient .openclaw mask clamp within ≤1 cycle, so a brief skip is expected
# noise. But a clamp that PERSISTS past this window is not self-healing — e.g.
# a bot 0700-ing one of its OWN workspace subdirs to hide scripts from this
# tamper tripwire — and MUST page. (Without this, the graceful degrade would
# silently suppress the inventory drift signal forever.)
_SCRIPT_INVENTORY_EACCES_GRACE = 3
_SCRIPT_INVENTORY_EACCES_REL = ("security", "baselines", "scripts-eacces.json")


def _eacces_counter_path(shared_dir: Path) -> Path:
    return shared_dir.joinpath(*_SCRIPT_INVENTORY_EACCES_REL)


def _load_eacces_counts(shared_dir: Path) -> dict[str, int]:
    path = _eacces_counter_path(shared_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_eacces_counts(shared_dir: Path, counts: dict[str, int]) -> None:
    try:
        _eacces_counter_path(shared_dir).write_text(json.dumps(counts, indent=2))
    except OSError as exc:
        logger.debug("script-inventory eacces counter write failed: %s", exc)


def _bump_eacces_skip(shared_dir: Path, bot_id: str) -> int:
    """Increment + persist the consecutive-skip counter; return the new value."""
    counts = _load_eacces_counts(shared_dir)
    counts[bot_id] = int(counts.get(bot_id, 0)) + 1
    _write_eacces_counts(shared_dir, counts)
    return counts[bot_id]


def _clear_eacces_skip(shared_dir: Path, bot_id: str) -> None:
    """Reset the counter once a find succeeds — the clamp self-healed."""
    counts = _load_eacces_counts(shared_dir)
    if counts.pop(bot_id, None) is not None:
        _write_eacces_counts(shared_dir, counts)


def _script_inventory_eacces_finding(
    bot_id: str, shared_dir: Path, stderr_detail: str
) -> Finding:
    """Build the degrade/escalate Finding for a permission-denied find.

    Quiet (``ok``) while within the self-heal grace window; ``warn`` once a
    clamp persists past it (a genuine, non-transient lockout that the periodic
    reassert is NOT clearing — page the operator)."""
    n = _bump_eacces_skip(shared_dir, bot_id)
    if n < _SCRIPT_INVENTORY_EACCES_GRACE:
        return Finding(
            level="ok", category="identity", bot_id=bot_id,
            message=(
                f"{bot_id}: script inventory skipped this cycle "
                f"(evolve traverse transiently clamped, {n}/"
                f"{_SCRIPT_INVENTORY_EACCES_GRACE}; self-heals via periodic "
                "ACL reassert)"
            ),
            detail=stderr_detail,
        )
    return Finding(
        level="warn", category="identity", bot_id=bot_id,
        message=(
            f"{bot_id}: script inventory blocked by EACCES for {n} consecutive "
            "cycles — evolve cannot traverse the workspace and the periodic "
            "ACL reassert is not clearing it"
        ),
        detail=(
            (stderr_detail + " | " if stderr_detail else "")
            + "a persistently 0700 path (possibly a bot hiding scripts from "
            "the inventory) — run `sudo evolve-admin ensure-pod-perms` and "
            "inspect the workspace permissions"
        )[:300],
    )


def audit_script_inventory(bot_id: str, shared_dir: Path) -> list[Finding]:
    """Baseline list of .sh/.py files in bot workspace; warn on new or missing files."""
    findings: list[Finding] = []
    baseline_path = shared_dir / "security" / "baselines" / "scripts.json"

    try:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    # Load existing baseline for all bots
    all_baselines: dict[str, list[str]] = {}
    if baseline_path.exists():
        try:
            all_baselines = json.loads(baseline_path.read_text())
        except (json.JSONDecodeError, OSError):
            all_baselines = {}

    oc_dir = _bot_home(bot_id) / ".openclaw"
    workspace = str(oc_dir / "workspace")
    # Couple a mask-reassert to THIS Evolve-side reader before the find. On
    # Linux the OC gateway re-hardens .openclaw to 0700 on its runtime ops,
    # clamping the POSIX-ACL mask so evolve loses traverse — and this audit is
    # one of the hourly readers that trips over it. `reassert_mask` re-widens
    # the mask (`setfacl -m m::rwX`) using evolve's own grant; it is guarded to
    # no-op on un-ACL'd paths and on macOS, so it is cheap and always safe.
    try:
        from runtime.perms import get_perms
        get_perms().reassert_mask(oc_dir)
    except Exception as exc:  # noqa: BLE001 — best-effort; the find/degrade below is authoritative
        logger.debug("audit_script_inventory: pre-find reassert_mask "
                     "best-effort failed for %s: %s", bot_id, exc)

    # Direct (non-sudo) find: deploy.set_evolve_read_acl() grants the evolve
    # user ACL read access to every bot's .openclaw/ tree, so `find` works
    # without sudo. The previous `sudo find` invocation triggered a password
    # prompt because no /usr/bin/find grant exists in /etc/sudoers.d/evolve,
    # and the audit ran without a TTY.
    try:
        r = subprocess.run(
            ["find", workspace, "-name", "*.sh", "-o", "-name", "*.py"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            if _find_is_permission_denied(r.stderr):
                # The gateway re-clamped .openclaw's ACL mask after our reassert
                # above (or between deploys), so evolve can't traverse the tree.
                # Quiet within the self-heal grace window (the periodic reassert
                # clears a transient clamp in ≤1 cycle); escalates to warn once
                # it persists past the window — see the helper.
                findings.append(_script_inventory_eacces_finding(
                    bot_id, shared_dir,
                    r.stderr.strip()[:200] if r.stderr else "",
                ))
                return findings
            findings.append(Finding(
                level="warn", category="identity", bot_id=bot_id,
                message=f"{bot_id}: script inventory find failed",
                detail=r.stderr.strip()[:200] if r.stderr else "",
            ))
            return findings
        current_files = sorted(
            line.strip() for line in r.stdout.splitlines() if line.strip()
        )
        # find succeeded → the clamp (if any) cleared; reset the skip counter.
        _clear_eacces_skip(shared_dir, bot_id)
    except (subprocess.TimeoutExpired, OSError) as e:
        if isinstance(e, PermissionError):
            # Same clamp class as the stderr branch above, surfaced as EACCES
            # on the subprocess spawn — degrade/escalate via the same counter.
            findings.append(_script_inventory_eacces_finding(
                bot_id, shared_dir, str(e)[:200]))
            return findings
        findings.append(Finding(
            level="warn", category="identity", bot_id=bot_id,
            message=f"{bot_id}: script inventory find error: {e}",
        ))
        return findings

    if bot_id not in all_baselines:
        # First run for this bot
        all_baselines[bot_id] = current_files
        try:
            baseline_path.write_text(json.dumps(all_baselines, indent=2))
        except OSError:
            pass
        findings.append(Finding(
            level="ok", category="identity", bot_id=bot_id,
            message=f"{bot_id}: script inventory baseline created ({len(current_files)} files)",
        ))
        return findings

    baseline_files = set(all_baselines[bot_id])
    current_set = set(current_files)
    new_files = sorted(current_set - baseline_files)
    missing_files = sorted(baseline_files - current_set)

    if new_files or missing_files:
        # Coalesce into ONE drift finding per bot. The previous shape emitted
        # N findings (one per file) which made a single redeploy look like
        # five separate problems on the alerts page.
        parts = []
        if new_files:
            parts.append(f"+{len(new_files)} new")
        if missing_files:
            parts.append(f"-{len(missing_files)} missing")
        message = f"{bot_id}: script inventory drift ({', '.join(parts)})"
        detail_lines = []
        if new_files:
            detail_lines.append("new: " + ", ".join(new_files))
        if missing_files:
            detail_lines.append("missing: " + ", ".join(missing_files))
        findings.append(Finding(
            level="warn", category="identity", bot_id=bot_id,
            message=message,
            detail=" | ".join(detail_lines),
        ))
    else:
        findings.append(Finding(
            level="ok", category="identity", bot_id=bot_id,
            message=f"{bot_id}: script inventory OK ({len(current_files)} files)",
        ))

    return findings


# ── 7b. Workspace secrets audit ───────────────────────────────────────────────

# Known credential prefixes — patterns that should never appear in workspace files.
# Paths explicitly allowed to contain credentials:
#   auth-profiles.json  (canonical key storage)
#   openclaw.json       (integration tokens — integrations.github.token, etc.)
import re as _re

# Credential patterns that should never appear in workspace text files.
# evolve has ACL read on .openclaw/ — no sudo needed, no sudoers grant required.
# Files explicitly allowed to contain credentials:
#   auth-profiles.json  (canonical key storage)
#   openclaw.json       (integration tokens — integrations.github.token, etc.)
_SECRET_PATTERNS: list[tuple["_re.Pattern[str]", str]] = [
    (_re.compile(r"ghp_[A-Za-z0-9]{36}"),                       "GitHub PAT (classic)"),
    (_re.compile(r"github_pat_[A-Za-z0-9_]{82}"),                "GitHub PAT (fine-grained)"),
    (_re.compile(r"sk-ant-[A-Za-z0-9\-_]{80,}"),                 "Anthropic API key"),
    (_re.compile(r"sk-proj-[A-Za-z0-9\-_]{40,}"),                "OpenAI project key"),
    (_re.compile(r"xai-[A-Za-z0-9]{40,}"),                       "xAI API key"),
    (_re.compile(r"\d{8,12}:[A-Za-z0-9_\-]{35}"),                "Telegram bot token"),
    # Slack tokens: bot (xoxb-), app (xapp-), and the legacy/alt forms
    # (xoxa/xoxc/xoxe/xoxp/xoxr/xoxs). Minimum-length anchors avoid catching
    # placeholders like `xoxb-token` in templates.
    (_re.compile(r"xoxb-\d{10,}-\d{10,}-[A-Za-z0-9]{20,}"),      "Slack bot token"),
    (_re.compile(r"xapp-\d-[A-Z0-9]{10,}-\d{10,}-[a-f0-9]{40,}"), "Slack app token"),
    (_re.compile(r"xox[acepr]-[A-Za-z0-9\-]{30,}"),               "Slack token (legacy form)"),
]

_ALLOWED_SECRET_BASENAMES = {"auth-profiles.json", "openclaw.json"}

# Extensions worth scanning; skip binaries and lock files
_SCAN_EXTENSIONS = {".md", ".txt", ".env", ".sh", ".py", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini"}


def audit_workspace_secrets(bot_id: str, shared_dir: Path) -> list[Finding]:
    """Scan bot workspace text files for hardcoded credentials.

    Uses ACL-based direct reads — no sudo needed (evolve has ACL read on .openclaw/).
    """
    findings: list[Finding] = []
    workspace = _bot_home(bot_id) / ".openclaw" / "workspace"
    try:
        if not workspace.is_dir():
            return findings
    except (PermissionError, OSError):
        return findings

    for fpath in workspace.rglob("*"):
        try:
            if not fpath.is_file():
                continue
            # `.env` files have an empty Path.suffix (the leading dot makes the
            # whole name the stem), so checking only suffix misses them. They
            # are the canonical hiding spot for plaintext tokens — explicitly
            # include them, plus `.env.local`, `.env.production`, etc.
            is_env_file = fpath.name == ".env" or fpath.name.startswith(".env.")
            if not is_env_file and fpath.suffix not in _SCAN_EXTENSIONS:
                continue
            if fpath.name in _ALLOWED_SECRET_BASENAMES:
                continue
            if ".git" in fpath.parts or "node_modules" in fpath.parts:
                continue
            # Skip archival and memory directories — old tokens in archived
            # scripts and historical memory files are not live credentials.
            if "archive" in fpath.parts or "memory" in fpath.parts:
                continue
            content = fpath.read_text(errors="replace")
        except (PermissionError, OSError):
            continue
        except Exception:
            continue

        for pattern, label in _SECRET_PATTERNS:
            if pattern.search(content):
                rel = str(fpath.relative_to(workspace))
                findings.append(Finding(
                    level="critical", category="identity", bot_id=bot_id,
                    message=f"{bot_id}: {label} found in workspace file: {rel}",
                    detail="Move credential to auth-profiles.json or openclaw.json → integrations",
                ))
                break  # one finding per file is enough

    if not findings:
        findings.append(Finding(
            level="ok", category="identity", bot_id=bot_id,
            message=f"{bot_id}: workspace secrets scan clean",
        ))
    return findings


# ── 8. Cron health audit ──────────────────────────────────────────────────────

def audit_cron_health(bot_id: str, shared_dir: Path) -> list[Finding]:
    """Check cron jobs.json for error counts, silent-exec patterns, and new job names."""
    findings: list[Finding] = []
    jobs_path = _bot_home(bot_id) / ".openclaw" / "cron" / "jobs.json"

    # Try ACL read first, fall back to sudo /bin/cat
    raw: str | None = None
    try:
        raw = jobs_path.read_text()
    except (PermissionError, OSError):
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(jobs_path)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                raw = r.stdout
        except (subprocess.TimeoutExpired, OSError):
            pass

    if raw is None:
        findings.append(Finding(
            level="skipped", category="config", bot_id=bot_id,
            message=f"{bot_id}: cron/jobs.json read denied",
            detail="audit user lacks ACL/sudo read for the bot's cron/jobs.json",
        ))
        return findings

    try:
        jobs_data = json.loads(raw)
    except json.JSONDecodeError:
        findings.append(Finding(
            level="warn", category="config", bot_id=bot_id,
            message=f"{bot_id}: cannot parse cron/jobs.json",
        ))
        return findings

    # jobs_data may be a list of job objects or a dict with a "jobs" key
    if isinstance(jobs_data, dict):
        jobs: list[dict] = jobs_data.get("jobs", [])
    elif isinstance(jobs_data, list):
        jobs = jobs_data
    else:
        findings.append(Finding(
            level="warn", category="config", bot_id=bot_id,
            message=f"{bot_id}: cron/jobs.json has unexpected format",
        ))
        return findings

    # Load cron baseline
    baseline_path = shared_dir / "security" / "baselines" / "cron-jobs.json"
    try:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    all_baselines: dict[str, list[str]] = {}
    if baseline_path.exists():
        try:
            all_baselines = json.loads(baseline_path.read_text())
        except (json.JSONDecodeError, OSError):
            all_baselines = {}

    current_job_names = sorted(j.get("name", "") for j in jobs if isinstance(j, dict))
    baseline_job_names: list[str] | None = all_baselines.get(bot_id)

    if baseline_job_names is None:
        # First run — establish baseline
        all_baselines[bot_id] = current_job_names
        try:
            baseline_path.write_text(json.dumps(all_baselines, indent=2))
        except OSError:
            pass
        findings.append(Finding(
            level="ok", category="config", bot_id=bot_id,
            message=f"{bot_id}: cron job baseline created ({len(current_job_names)} jobs)",
        ))
    else:
        baseline_set = set(baseline_job_names)
        current_set = set(current_job_names)
        for name in sorted(current_set - baseline_set):
            findings.append(Finding(
                level="warn", category="config", bot_id=bot_id,
                message=f"{bot_id}: new cron job not in baseline: {name!r}",
                what_it_means=(
                    f"The cron-jobs audit caches the set of expected job names "
                    f"per bot. A job named {name!r} now exists on {bot_id} that "
                    f"wasn't there when the baseline was seeded. That's almost "
                    f"always either (a) drift from a recent intentional install "
                    f"— in which case the baseline just needs reblessing — or "
                    f"(b) an unexpected job that warrants investigation."
                ),
                fix_steps=(
                    f"1. Inspect the new job:\n"
                    f"   sudo /bin/cat ~{get_bot_user(bot_id)}/.openclaw/cron/jobs.json "
                    f"| jq '.jobs[] | select(.name==\"{name}\")'\n"
                    f"2. If the job is expected, rebless the baseline. Edit:\n"
                    f"   {shared_dir}/security/baselines/cron-jobs.json\n"
                    f"   Either delete the {bot_id!r} key entirely (next audit "
                    f"reseeds it from observed state) or append {name!r} to "
                    f"the existing list.\n"
                    f"3. If the job is NOT expected, remove it from "
                    f"~{get_bot_user(bot_id)}/.openclaw/cron/jobs.json and "
                    f"investigate how it appeared — check recent git history "
                    f"and recent admin activity."
                ),
            ))

    # Per-job checks
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = job.get("name", "<unnamed>")
        consecutive_errors = job.get("consecutiveErrors", 0)
        if isinstance(consecutive_errors, (int, float)) and consecutive_errors > 2:
            is_critical_job = (
                isinstance(name, str)
                and (name.startswith("security") or name.startswith("healthcheck"))
            )
            level = "critical" if is_critical_job else "warn"
            msg_prefix = "🔴 CRITICAL" if is_critical_job else "WARN"
            findings.append(Finding(
                level=level, category="config", bot_id=bot_id,
                message=(
                    f"{msg_prefix}: {bot_id} cron job {name!r} has "
                    f"{consecutive_errors} consecutive errors"
                ),
            ))

        # Silent-exec pattern: sessionTarget == "main" and payload.kind == "exec"
        session_target = job.get("sessionTarget", "")
        payload = job.get("payload", {})
        if (
            isinstance(session_target, str) and session_target == "main"
            and isinstance(payload, dict) and payload.get("kind") == "exec"
        ):
            findings.append(Finding(
                level="warn", category="config", bot_id=bot_id,
                message=f"{bot_id}: cron job {name!r} uses sessionTarget=main with exec payload (silent-skip pattern)",
            ))

    if not findings:
        findings.append(Finding(
            level="ok", category="config", bot_id=bot_id,
            message=f"{bot_id}: cron health OK ({len(jobs)} jobs)",
        ))

    return findings


# ── 9. Process audit ──────────────────────────────────────────────────────────

# Process names that have no legitimate long-running use under a bot user.
# Pretty much every entry maps to one of: install-time tooling that should
# never be running steady-state, an interactive session backgrounder
# (screen, tmux, nohup) that's a classic backdoor primitive, a network
# swiss army knife (nc/ncat/socat) used for reverse shells and pivoting,
# or alternate-platform package managers a Mac bot has no reason to call.
#
# Snapshot-based ps detection: only long-running processes survive long
# enough to be caught. That's intentional — the things on this list are
# the ones that *should* exit quickly; if one is still around when audit
# walks ps, that's the signal.
_SUSPICIOUS_PROCS = {
    # Install/package tooling — fine briefly, suspicious if long-running
    "npm", "brew", "pip", "pip3", "gem", "cargo", "installer",
    "dpkg", "apt", "apt-get", "yum", "rpm",
    # Network swiss army knives — no bot has a reason to run these
    "nc", "ncat", "socat",
    # Listening / privilege-escalation daemons under a bot user
    "sshd", "sudo", "su",
    # Interactive-session backgrounders (classic backdoor pattern)
    "screen", "tmux", "nohup",
    # Alternate HTTP clients — bots use curl if anything
    "wget",
}


def audit_process(bot_ids: list[str], admin_user: str, shared_dir: Path) -> list[Finding]:
    """Check ps aux for suspicious long-running processes under bot users.

    Time-gated to at most once every 6 hours since this is a full ps
    scan and the suspicious-process threat model is "long-running" by
    construction — a tool that exits quickly won't be caught regardless
    of cadence, and one that's been around 6 hours is just as suspicious
    as one around for 6 minutes.

    The admin-user-gateway check that used to live here was extracted
    to ``_check_admin_user_gateway`` (called from ``audit_machine``)
    so it runs every 15 minutes with operator-facing playbook context.
    """
    findings: list[Finding] = []
    timestamp_path = shared_dir / "security" / "last-process-audit"
    # admin_user parameter kept for callsite compatibility; the gateway
    # check that used it moved to _check_admin_user_gateway.
    del admin_user

    # Time-gate: skip if audited within the last 6 hours
    six_hours = 6 * 3600
    now = datetime.now(timezone.utc).timestamp()
    if timestamp_path.exists():
        try:
            last_run = float(timestamp_path.read_text().strip())
            if now - last_run < six_hours:
                return []
        except (OSError, ValueError):
            pass

    try:
        r = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            findings.append(Finding(
                level="warn", category="machine", bot_id=None,
                message="machine: process audit failed (ps aux error)",
            ))
            return findings
        lines = r.stdout.splitlines()[1:]  # skip header
    except (subprocess.TimeoutExpired, OSError):
        findings.append(Finding(
            level="warn", category="machine", bot_id=None,
            message="machine: process audit failed (ps aux unavailable)",
        ))
        return findings

    bot_set = set(bot_ids)
    for line in lines:
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        proc_user = parts[0]
        cmd_full = parts[10]
        first_token = cmd_full.split(None, 1)[0] if cmd_full.strip() else ""
        cmd_name = first_token.rsplit("/", 1)[-1] if first_token else ""

        # Suspicious tools running under an AI bot user.
        if proc_user in bot_set and cmd_name in _SUSPICIOUS_PROCS:
            findings.append(Finding(
                level="warn", category="machine", bot_id=proc_user,
                message=f"{proc_user}: suspicious process running: {cmd_name}",
                detail=cmd_full[:200],
                what_it_means=(
                    f"The bot user {proc_user!r} has a long-running "
                    f"{cmd_name!r} process. Tools on this list (network "
                    "swiss-army knives like nc/socat, install tooling, "
                    "interactive backgrounders like screen/tmux) have no "
                    "steady-state use for an automated bot. A long-running "
                    "one is often the first visible artifact of a "
                    "post-exploitation foothold."
                ),
                fix_steps=(
                    f"1. Identify the process and what started it:\n"
                    f"   ssh pod_admin_user@mini 'ps -fp <PID>'\n"
                    "2. If it's expected (e.g. a manually-staged install you "
                    "forgot about), stop it and re-run the audit to clear.\n"
                    "3. If it's unexpected, capture the process tree and "
                    "shell history BEFORE killing it:\n"
                    f"   ssh pod_admin_user@mini 'ps -ef | grep {proc_user}'\n"
                    f"   ssh pod_admin_user@mini 'sudo lsof -p <PID>'\n"
                    "4. Audit the bot's recent gateway exec log + workspace "
                    "for what kicked it off; rotate any credentials it could "
                    "have accessed."
                ),
            ))

    if not findings:
        findings.append(Finding(
            level="ok", category="machine", bot_id=None,
            message="machine: process audit OK",
        ))

    # Update timestamp
    try:
        timestamp_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp_path.write_text(str(now))
    except OSError:
        pass

    return findings


# ── 10. OC security audit ─────────────────────────────────────────────────────

# Plain-English explanations for OpenClaw security checks. Keyed by the
# `checkId` field on each finding in `openclaw security audit --json` output.
# `what_it_means` describes the underlying condition; `fix_steps` is a
# copy-pasteable numbered remediation. Falls back to OpenClaw's own
# `remediation` text when a check isn't in this table.
_OC_CHECK_EXPLANATIONS: dict[str, tuple[str, str]] = {
    "gateway.loopback_no_auth": (
        "The bot's OpenClaw control gateway is bound to loopback (127.0.0.1) "
        "but has no `gateway.auth` token configured. Loopback-only is safe in "
        "isolation — only processes on this Mac can reach it — but if anything "
        "ever forwards that port off the host (reverse proxy, SSH tunnel, mDNS "
        "publisher), requests will arrive with no authentication at all.",
        "1. Decide whether the Control UI should ever be reachable off this Mac.\n"
        "2. Either way, set an auth token as defense in depth. Generate one:\n"
        "   openssl rand -hex 32\n"
        "3. Edit ~/<bot-user>/.openclaw/openclaw.json and add the token under "
        "`gateway.auth.token` (string). Keep the file at mode 0600.\n"
        "4. Apply: sudo evolve-admin deploy <bot>\n"
        "5. If you DO want network access, also set `gateway.trustedProxies` "
        "(see the trusted_proxies_missing check)."
    ),
    "gateway.trusted_proxies_missing": (
        "A reverse proxy is in front of the gateway, but `gateway.trustedProxies` "
        "is empty. Without trusted-proxy entries the gateway honors every "
        "incoming `X-Forwarded-For` header — clients can spoof their source IP, "
        "and rate-limit + audit logs record the wrong client.\n\n"
        "Evolve note: bind=loopback bots (the default) cannot be reached off-host, "
        "so spoofable XFF headers are not a real risk. This finding is suppressed "
        "for loopback bots via _OC_POLICY_OVERRIDES_SUPPRESS — see the table for "
        "the policy rationale. The remediation below applies if you've intentionally "
        "exposed a bot off-loopback.",
        "1. Find the IP(s) of whatever proxy fronts this bot's gateway. For a "
        "local reverse proxy that's usually 127.0.0.1; for a LAN proxy it's the "
        "proxy machine's address.\n"
        "2. Edit ~/<bot-user>/.openclaw/openclaw.json. Set "
        "`gateway.trustedProxies` to a JSON array of those IPs.\n"
        "3. Apply: sudo evolve-admin deploy <bot>"
    ),
    "gateway.probe_failed": (
        "OpenClaw's own deep health probe sent a request to the bot's control "
        "gateway and didn't get a healthy response. The control plane may be "
        "down, slow, mis-configured, or stuck behind a crashed plugin.",
        "1. Confirm the gateway launchd job is loaded. On the mini:\n"
        "   launchctl print gui/$UID/ai.evolve.<bot>.gateway 2>&1 | head -20\n"
        "2. Tail the gateway log:\n"
        "   sudo tail -n 100 ~<bot-user>/.openclaw/logs/gateway.log\n"
        "3. Run OC's own self-check as the bot user. The `env HOME=…` is "
        "load-bearing — without it OC reads the caller's openclaw.json:\n"
        "   cd /tmp && sudo -u <bot-user> env HOME=/Users/<bot-user> openclaw doctor\n"
        "4. If everything looks healthy but the probe still fails, redeploy "
        "to re-roll the gateway:\n"
        "   sudo evolve-admin deploy <bot>"
    ),
    "plugins.installs_unpinned_npm_specs": (
        "OpenClaw's plugin install record stores the spec string the operator "
        "passed to `openclaw plugins install` — and from 2026.5.18 onward, "
        "the security audit flags any record whose spec isn't pinned to an "
        "exact version (`@scope/pkg@X.Y.Z`). A bare `@openclaw/brave-plugin` "
        "resolves to whatever 'latest' is at install time, which makes the "
        "install non-reproducible. The `resolvedSpec` / `integrity` / "
        "`shasum` fields in the install record do capture the actual version "
        "that landed, but the audit only inspects `spec`. Evolve historically "
        "installed @openclaw/* plugins without a version suffix; the auto-pin "
        "in oc_neutralize.install_externalized_plugin now passes "
        "`<pkg>@<oc_version>` so a fresh deploy clears this finding.",
        "1. Redeploy the affected bot — the install path now auto-pins to "
        "the running OC version:\n"
        "   sudo evolve-admin deploy <bot>\n"
        "2. Verify the spec got rewritten (look for `@X.Y.Z` suffixes):\n"
        "   sudo cat /Users/<bot-user>/.openclaw/plugins/installs.json | "
        "python3 -c 'import json,sys; "
        "d=json.load(sys.stdin)[\"installRecords\"]; "
        "print({k:v[\"spec\"] for k,v in d.items()})'\n"
        "3. The next OC audit cycle (≤23h, time-gated by audit_oc_security) "
        "drops the finding. To force an immediate re-audit, on the mini:\n"
        "   sudo rm /Users/Shared/evolve/security/last-oc-audit-<bot>"
    ),
}


def _explain_oc_finding(check_id: str, oc_remediation: str) -> tuple[str, str]:
    """Return (what_it_means, fix_steps) for an OC finding.

    Falls back to OpenClaw's own short `remediation` text when we don't
    have a hand-written explanation for the check_id yet.
    """
    if check_id in _OC_CHECK_EXPLANATIONS:
        return _OC_CHECK_EXPLANATIONS[check_id]
    return "", oc_remediation or ""


# Test-file-name pattern matching upstream OC's TEST_FILE_NAME_PATTERN
# (src/security/skill-scanner.ts): `*.test.ext`, `*.spec.ext`, `*.mock.ext`.
# Used by _is_oc_code_safety_test_only_noise.
_OC_TEST_FILE_RE = re.compile(r"\.(?:mock|spec|test)\.[a-z]+$", re.IGNORECASE)


# OC native audit findings we suppress because the underlying behavior
# is documented Evolve policy, not a misconfiguration.
#
# Each entry: (check_id, reason — printed once at debug log on suppression).
#
# Adding to this list is a policy statement: we've decided the OC
# advisory misclassifies *our* setup as a finding. The corresponding
# spec/diagnosis link is required so future-us knows why the rule
# was suppressed and when to revisit.
_OC_POLICY_OVERRIDES_SUPPRESS: dict[str, str] = {
    # Phase A (2026-05-25): "full" is the documented member-bot default.
    # OC's advisory recommends "allowlist with ask prompts" as a security
    # best practice; Evolve's policy is that member bots run as their
    # own macOS user account with no privileged reach, so "full" inside
    # the bot's own user account is the right default. Operators who want
    # tighter posture can opt in to "allowlist" — see
    # docs/spec-app-derived-permissions-2026-05-24.md §"Why full as default".
    # The audit signal was firing nightly across every bot since Phase A
    # shipped; this filter retires it.
    "tools.exec.security_full_configured": (
        "Evolve policy: 'full' is the member-bot default — see "
        "docs/spec-app-derived-permissions-2026-05-24.md"
    ),
    # 2026-05-28: Every Evolve bot uses bind=loopback with trustedProxies=[]
    # (deploy.py:1438 gap-fills this on every deploy). The upstream OC check
    # (2026.5.18+) fires on bind=loopback + trustedProxies=[] regardless of
    # whether a proxy is actually in use — its logic is "loopback bot with
    # no trusted proxies might be exposed off-host", but Evolve bots are
    # never exposed off-host. The check's own remediation text used to say
    # `[]` would silence it (audit.py's _OC_CHECK_EXPLANATIONS captured that
    # claim); live behavior on the 2026-05-28 deploy contradicts it — the
    # signal fired 8/8 with `[]` already set. Suppress here; if Evolve ever
    # gains non-loopback bind support, this override should become
    # conditional on bind != "loopback".
    "gateway.trusted_proxies_missing": (
        "Evolve policy: loopback-only is the bot-gateway invariant. "
        "deploy.py:1438 gap-fills trustedProxies=[]; upstream OC fires "
        "the check anyway on empty list. No off-host attack surface to "
        "protect — see "
        "docs/diagnosis-trusted-proxies-loopback-suppress-2026-05-28.md"
    ),
    # 2026-06-04 (Alerts quality-control pass): OC's generic
    # "multi-user setup heuristic" prose fires on every pod with multiple
    # bot accounts regardless of whether the setup is intentional. Evolve
    # has its own specific multi-user posture checks under
    # security_warden.posture (multi_user_no_pod_admins,
    # multi_user_exec_full_unscoped, multi_user_no_primary_recorded) that
    # point at concrete config rather than firing on the architecture
    # itself. server.py already demotes this to info at render-time —
    # this suppression moves the demote to signal-emission so it doesn't
    # crowd the Alerts page in the first place.
    "security.trust_model.multi_user_heuristic": (
        "Evolve policy: multi-user pods are an intentional, supported "
        "deployment shape. The generic OC heuristic adds no signal beyond "
        "what security_warden.posture's specific checks already provide. "
        "See docs/diagnosis-oc-noisy-advisories-2026-06-04.md."
    ),
    # 2026-06-04: Same rationale as tools.exec.security_full_configured —
    # Evolve member bots run as their own macOS user account with no
    # privileged reach, so a "permissive plugin tools" posture is the
    # intended state for the typical member bot. Operators who want
    # tighter scoping can opt in via the plugin allowlist; the OC advisory
    # treating the default as a finding is noise.
    "plugins.tools_reachable_permissive_policy": (
        "Evolve policy: permissive plugin-tool reach inside a bot's own "
        "macOS user account is the documented default. Same rationale as "
        "tools.exec.security_full_configured. See "
        "docs/diagnosis-oc-noisy-advisories-2026-06-04.md."
    ),
    # NOTE: the old "fs.config.perms_world_readable" suppression was REMOVED
    # by the 2026-06-12 security fix. It had blessed world-readable bot configs
    # on the false premise that /Users/<bot>/ is mode 0700 — it is actually 0755
    # (deploy.py forces it for shell access), so a 0644 openclaw.json IS
    # world-readable. openclaw.json + auth-profiles.json are now enforced to
    # 0600 everywhere (secret_config_perms), so OC's native world-readable
    # finding is signal, not noise — let it through.
    #
    # 2026-06-04: OC's models.weak_tier fires when ANY model in the
    # config (including agents.defaults.model.fallbacks) is below
    # the "recommended tier." But fallbacks are intentionally cheaper —
    # that's the whole point of having them. On the 2026-06-03 pod, this
    # signal fired on 9 of 9 bots because every bot lists gpt-4o,
    # haiku-4-5, gpt-4o-mini as fallbacks; the primary on 8 of those is
    # already a top-tier model. The check has no way to distinguish
    # "weak primary" (real concern) from "intentionally cheap fallback"
    # (the point), so it's noise as currently designed. If a future OC
    # version adds models.primary_weak_tier as a distinct check, that
    # one should NOT be suppressed — handle separately. server.py's
    # render-time demote (live audit-state) is now redundant with this
    # suppression; can be cleaned up.
    "models.weak_tier": (
        "Evolve policy: OC's check fires on any below-recommended model "
        "in the config, including intentional cost-optimized fallbacks. "
        "Use a primary-specific check when one ships upstream — the "
        "current check has no signal-to-noise. See "
        "docs/diagnosis-oc-noisy-advisories-2026-06-04.md."
    ),
}


def _is_oc_policy_override(check_id: str) -> bool:
    """True if this OC finding is a policy override that should be
    suppressed at audit time. See ``_OC_POLICY_OVERRIDES_SUPPRESS``.

    Silent suppression — adding to the table is the audit trail.
    """
    return check_id in _OC_POLICY_OVERRIDES_SUPPRESS


def _is_oc_code_safety_test_only_noise(check_id: str, oc_detail: str) -> bool:
    """Suppress plugins.code_safety findings whose hits are all in *.test.* files.

    Upstream bug: openclaw#82469 — `plugins.code_safety` scans bundled plugin
    test files (`*.test.ts`, `*.spec.ts`, …) and the `env-harvesting` rule
    matches `process.env` against any `fetch(`/`post(`/`http.request(` in the
    whole file, including matches inside `it("...")` description strings. The
    rule fires CRITICAL on stock OC channel plugins (e.g. discord's
    `provider.proxy.test.ts` + `thread-bindings.lifecycle.test.ts`). Test
    files are never the runtime entry point, so this is noise — drop it.

    When the upstream fix ships (passing `excludeTestFiles: true`), findings
    will stop coming and this suppression no-ops naturally.

    Only suppresses when EVERY file referenced in the detail looks like a
    test/spec/mock file. Any non-test path → emit normally.
    """
    if check_id != "plugins.code_safety":
        return False
    # OC's formatCodeSafetyDetails formats each match as:
    #   "  - [rule-id] message (path/to/file.ext:line)"
    paths: list[str] = []
    for line in oc_detail.splitlines():
        line = line.strip()
        if not line.startswith("- ["):
            continue
        # Pull the parenthesized "(path:line)" tail.
        open_paren = line.rfind("(")
        close_paren = line.rfind(")")
        if open_paren == -1 or close_paren <= open_paren:
            continue
        path_with_line = line[open_paren + 1 : close_paren]
        paths.append(path_with_line.rsplit(":", 1)[0])
    if not paths:
        return False
    return all(_OC_TEST_FILE_RE.search(p) for p in paths)


# OpenClaw fs.* checks that stat ``st_mode`` and flag the group/other read OR
# WRITE bits. On Linux these can be a pure ACL-MASK artifact of Evolve's own
# evolve-read ACL (``user:evolve:r-x`` forces an ACL ``mask``, and the stat
# group triad then displays the mask) rather than a real group/other grant —
# see ``LinuxPerms.acl_masked_owner_only``. Only checks whose finding can be a
# mask artifact belong here.
#
# WRITE bit, specifically: the every-~5-min self-heal ``reassert_mask`` widens
# the mask to ``m::rwX`` (``runtime/perms.py`` — "safe-generous: the mask only
# caps named ACEs, whose grants are the intent"). That is correct for access
# control — the real ``group::``/``other::`` stay ``---`` and the only named
# reader, ``user:evolve``, is itself only ``r-x`` — but ``st_mode``'s group
# triad then shows ``rwx`` (a dir reads 0770, a non-exec secret 0660), so OC's
# st_mode-based audit fires "State dir is group-writable" / "Config file is
# writable by others" as a false positive of the same shape as the readable
# one. The discriminator is identical: ``acl_masked_owner_only`` proves the
# real ``group::`` AND ``other::`` grant nothing (``eff & 0o077 == 0`` covers
# the write bit too — ``effective_mode`` substitutes the real ``group::`` bits
# incl. ``w``), so a genuine ``group::w`` or ``other::w`` still fires. The mask
# is the right call — ``workspace/evolve`` needs evolve write — so the fix is
# making the audit ACL-aware for the write bit, not narrowing the mask.
#
# Deliberately EXCLUDES the WORLD-class siblings
# (``fs.config.perms_world_readable``, ``fs.state_dir.perms_world_writable``):
# the POSIX ACL mask caps only the GROUP class (named users/groups + owning
# group), never the OTHER class, so an ``other::`` read or write bit is always
# real — a world-readable/world-writable finding can never be a mask false
# positive. Re-adding either would be a no-op at best (the seam fires on a real
# ``other::`` bit regardless) and an echo of the blanket suppression the
# 2026-06-12 security fix correctly removed (see the NOTE in
# _OC_POLICY_OVERRIDES_SUPPRESS). The combined ``fs.{config,auth_profiles}.
# perms_writable`` ids DO fire on world-OR-group write, but the seam keeps a
# real ``other::w`` firing, so they are safe to list.
_OC_MASK_PRONE_PERMS_CHECKS: frozenset[str] = frozenset({
    # readable family (group-class read bit reflected by the mask)
    "fs.config.perms_group_readable",
    "fs.config_include.perms_group_readable",
    "fs.state_dir.perms_readable",
    "fs.auth_profiles.perms_readable",
    "fs.sessions_store.perms_readable",
    "fs.log_file.perms_readable",
    # writable family (group-class write bit from the m::rwX reassert).
    # ``fs.state_dir.perms_world_writable`` is intentionally absent — see above.
    "fs.state_dir.perms_group_writable",
    "fs.config.perms_writable",
    "fs.config_include.perms_writable",
    "fs.auth_profiles.perms_writable",
})
# NOTE: this set is kept aligned with OpenClaw's emitted ``fs.*.perms_*`` check
# vocabulary (enumerated 2026-06-24 from the installed package on the Linux
# pod). The GROUP-class / mask-reflected ids above are listed; the WORLD-class
# ids OC also emits — ``fs.config.perms_world_readable``,
# ``fs.config_include.perms_world_readable``, ``fs.state_dir.perms_world_writable``
# — and the carved-out ``fs.credentials_dir.perms_{readable,writable}`` are
# deliberately ABSENT: a real ``other::`` bit (mask never caps OTHER) and the
# creds dir (no evolve ACE — #3213) are genuine, so they must always fire.


def _oc_finding_path(oc_detail: str) -> "Path | None":
    """Pull the leading absolute path out of an OC ``fs.*`` finding detail.

    OpenClaw formats these as ``<abs-path> mode=<NNN>; <note>`` (e.g.
    ``/home/evo/.openclaw/openclaw.json mode=650; config can contain
    tokens…``). The ``" mode="`` separator is the stable delimiter — split on
    it so a path containing spaces is still captured whole. Returns None when
    the detail doesn't begin with an absolute path.
    """
    text = (oc_detail or "").strip()
    if not text:
        return None
    head = text.split(" mode=", 1)[0].strip() if " mode=" in text else text.split()[0]
    if not head.startswith("/"):
        return None
    return Path(head)


def _is_acl_masked_perms_false_positive(check_id: str, oc_detail: str) -> bool:
    """True if this OC group/other read-OR-write finding is a Linux ACL-mask
    artifact of Evolve's evolve-read ACL, proven via ``getfacl``, and so
    should be suppressed.

    Linux-scoped *by construction*, with no ``sys.platform`` branch: the work
    is delegated to the platform-keyed Perms seam, whose macOS backend returns
    False (macOS ACLs have no mask — out of scope) and whose Linux backend runs
    the getfacl check (``LinuxPerms.acl_masked_owner_only``). Suppress ONLY when
    the real ``group::`` AND ``other::`` entries grant nothing — across read AND
    write bits; a genuine 0644 (real ``other::r``), a real ``group::r-x``, or a
    real ``group::w``/``other::w`` still fires there.

    Fail-closed: any error → False (emit the finding). Scoped to a bot's
    ``.openclaw`` tree so the suppression never reaches beyond what Evolve's
    read ACL touches.
    """
    if check_id not in _OC_MASK_PRONE_PERMS_CHECKS:
        return False
    path = _oc_finding_path(oc_detail)
    # Scope to a bot's .openclaw tree, and refuse traversal (``..``) so a
    # crafted detail can't point the getfacl probe outside it. ``parts``
    # without resolution keeps this a pure string check (no filesystem race).
    if path is None or ".openclaw" not in path.parts or ".." in path.parts:
        return False
    try:
        from runtime.perms import get_perms
        return get_perms().acl_masked_owner_only(path)
    except Exception:
        return False


# ── Live-UI OC finding normalization (single source for the operator-facing
#    forwarders) ───────────────────────────────────────────────────────────────
#
# OpenClaw's native audit findings reach operators through THREE forwarders,
# each historically carrying its own hand-copied "drop/demote this OC advisory"
# logic:
#   1. audit_oc_security (this module) — daemon → Signal store
#   2. routes_oc._audit_run_one        — the Security page
#   3. evo/handlers/oc_audit._normalize_findings — the evo "what can my bot do?"
#      tray
# That duplication is exactly the "keep the two lists in sync" hazard that
# produced the mask-FP drift bug (#3259 / memory:
# feedback_three_oc_audit_forwarders_must_share_suppression). ``normalize_oc_
# finding`` below is the single home for the *live-UI* normalization that
# forwarders (2) and (3) share; both call it instead of re-deriving the rules.
#
# Forwarder (1), the daemon, is deliberately NOT routed through here: it emits
# ``Finding`` objects to the Signal store (which has no "info" tier to demote
# to) and DROPS the broader ``_OC_POLICY_OVERRIDES_SUPPRESS`` set at emission
# rather than demoting prose to info. The one rule it shares with the live
# forwarders — the member-bot "full" exec drop — lives in the policy table it
# already consults; ``_OC_LIVE_DROP_CHECK_IDS`` is asserted to be a subset of
# that table (test_oc_audit_forwarder_parity) so the two can't silently diverge.
#
# Vocabulary: the live UI uses "warning" where OC emits "warn"; the helper
# normalizes that and returns the UI vocabulary.

# Check ids the live forwarders DROP outright (vs demote to info). This is the
# subset of OC policy contradictions the live UI historically *removed* — the
# rest of ``_OC_POLICY_OVERRIDES_SUPPRESS`` the live UI demotes by prose, so
# routing the live forwarders through the full table would change behavior.
# Keep this to exactly the ids the live UI dropped before single-sourcing.
_OC_LIVE_DROP_CHECK_IDS: frozenset[str] = frozenset({
    # "full" is the documented member-bot exec default — see
    # docs/spec-app-derived-permissions-2026-05-24.md §"Why full as default".
    "tools.exec.security_full_configured",
})

# Prose substrings (matched against title + remediation, lowercased) that mark
# an OC advisory as one of the generic, non-actionable findings the live
# forwarders demote to info. Single-sourced so the Security page and tray can't
# drift in what counts as "multi-user prose" / "proxy advisory" / etc.
_OC_MULTI_USER_PROSE: tuple[str, ...] = (
    "multi-user", "multi user", "multiple users", "shared",
)
_OC_PROXY_PROSE: tuple[str, ...] = (
    "reverse proxy", "trusted proxies", "proxy headers",
)
_OC_BELOW_RECOMMENDED_PROSE = "below recommended"

# openclaw's symbolic + literal loopback bind values; a gateway on any of these
# has no off-host attack surface, so the proxy-header advisory is noise.
_OC_LOOPBACK_BINDS: tuple[str, ...] = ("127.0.0.1", "::1", "localhost", "loopback")


@dataclass(frozen=True)
class OCFindingDecision:
    """Normalized live-UI decision for one raw OC finding.

    drop          — remove the finding entirely (mask FP or policy override).
    severity      — UI-vocabulary severity. For a KEEP it is the final
                    (possibly demoted) severity; for a DROP it is the
                    normalized-raw severity, so the caller can decrement the
                    right score bucket.
    raw_severity  — the normalized-raw severity (OC "warn" → "warning"),
                    independent of any demotion.
    demoted       — True iff a demotion rule lowered the severity to "info".
    reason        — short tag for logging (set on drops only).
    """

    drop: bool
    severity: str
    raw_severity: str
    demoted: bool
    reason: str | None = None


def normalize_oc_finding(
    finding: dict,
    *,
    gateway_bind: str | None = None,
    routing_enabled: bool = False,
    primary_model: str = "",
) -> OCFindingDecision:
    """Single source of the operator-facing OC-finding normalization shared by
    the Security page (routes_oc._audit_run_one) and the evo tray
    (oc_audit._normalize_findings).

    Returns the keep/drop + (possibly demoted) severity decision for one raw OC
    finding. The caller is responsible for any score-counter bookkeeping (the
    two forwarders track score differently — one decrements live buckets, the
    other recomputes from kept findings), so this helper stays pure.

    Context (``gateway_bind`` / ``routing_enabled`` / ``primary_model``) is
    OPTIONAL: the tray handler has no gateway/routing config to pass, so it
    calls with none. The two context-dependent demotions (proxy-header on a
    loopback gateway; model below-recommended when routing is on or the primary
    is a recommended tier) then never fire, which exactly reproduces the tray's
    historical surface-local behavior (it kept those findings as-is). The
    Security page passes its config context so those two demotions apply there.

    Order matters and mirrors the pre-single-source forwarders: drops first
    (mask FP, then policy override), then the prose demotions in
    multi-user → proxy → below-recommended order. Once a finding is demoted to
    info the later ``severity == "warning"`` guards stop matching, so at most
    one demotion applies.
    """
    check_id = finding.get("checkId", "") or ""
    raw_sev = finding.get("severity", "info")
    norm_sev = "warning" if raw_sev == "warn" else raw_sev
    detail = finding.get("detail", "") or ""

    # 1. Linux ACL-mask false positives (getfacl-proven) — drop. No-op on macOS
    #    (the Perms seam's mask check returns False there). A genuine exposure
    #    (real other::r world-readable, real group::r-x, the creds-dir #3213
    #    critical) is excluded from the mask-prone set and is NOT dropped here.
    if _is_acl_masked_perms_false_positive(check_id, detail):
        return OCFindingDecision(
            drop=True, severity=norm_sev, raw_severity=norm_sev,
            demoted=False, reason="acl_mask_fp",
        )

    # 2. Policy-override drops (currently the member-bot "full" exec default).
    if check_id in _OC_LIVE_DROP_CHECK_IDS:
        return OCFindingDecision(
            drop=True, severity=norm_sev, raw_severity=norm_sev,
            demoted=False, reason="policy_override",
        )

    msg_lower = (
        (finding.get("title", "") or "")
        + " "
        + (finding.get("remediation", "") or "")
    ).lower()
    sev = norm_sev

    # 3. Generic multi-user prose → info (advisory; the actionable multi-user
    #    posture checks live in security_warden.posture).
    if sev in ("warning", "critical") and any(
        k in msg_lower for k in _OC_MULTI_USER_PROSE
    ):
        sev = "info"

    # 4. Proxy-header advisory on a loopback gateway → info. Needs gateway_bind
    #    context; the tray passes none, so this is a no-op there.
    gateway_is_loopback = (gateway_bind or "") in _OC_LOOPBACK_BINDS
    if gateway_is_loopback and sev == "warning" and any(
        k in msg_lower for k in _OC_PROXY_PROSE
    ):
        sev = "info"

    # 5. Model "below recommended" advisory when routing is on or the primary is
    #    a recommended-tier model → info (fallbacks are intentionally cheaper).
    #    Needs routing/primary context; the tray passes none, so a no-op there.
    primary_is_recommended_tier = (
        bool(primary_model) and "haiku" not in primary_model.lower()
    )
    if ((routing_enabled or primary_is_recommended_tier) and sev == "warning"
            and _OC_BELOW_RECOMMENDED_PROSE in msg_lower):
        sev = "info"

    return OCFindingDecision(
        drop=False, severity=sev, raw_severity=norm_sev,
        demoted=(sev == "info" and norm_sev in ("warning", "critical")),
        reason=None,
    )


def audit_oc_security(bot_id: str, shared_dir: Path) -> list[Finding]:
    """Run openclaw security audit --deep --json per bot; check openclaw.json permissions.

    Time-gated: runs at most once every 23 hours per bot.
    """
    findings: list[Finding] = []
    timestamp_path = shared_dir / "security" / f"last-oc-audit-{bot_id}"

    # Time-gate: skip if audited within the last 23 hours
    twenty_three_hours = 23 * 3600
    now = datetime.now(timezone.utc).timestamp()
    if timestamp_path.exists():
        try:
            last_run = float(timestamp_path.read_text().strip())
            if now - last_run < twenty_three_hours:
                return []
        except (OSError, ValueError):
            pass

    # Run openclaw security audit via the runtime seam so background children
    # (openclaw-security) are killed when the JSON result is received.
    # The seam uses sudo -H which sets HOME to the bot user's home dir,
    # equivalent to the old `env HOME=...` wrapper.
    bot_user = get_bot_user(bot_id)
    from runtime.agent_runtime import get_runtime
    err_out: list[str] = []
    audit_result = get_runtime().security_audit(bot_id, deep=True, _err_out=err_out)
    if audit_result is not None:
        oc_findings = audit_result if isinstance(audit_result, list) else audit_result.get("findings", [])
        for item in oc_findings:
            if not isinstance(item, dict):
                continue
            sev = str(item.get("severity", "")).lower()
            check_id = item.get("checkId", "")
            title = (item.get("title") or item.get("description")
                     or item.get("message") or check_id or "unknown issue")
            oc_detail = item.get("detail", "")
            remediation = item.get("remediation", "")
            if _is_oc_code_safety_test_only_noise(check_id, oc_detail):
                # Upstream false positive — openclaw#82469. Drop silently;
                # when OC stops scanning test files this will stop firing.
                continue
            if _is_oc_policy_override(check_id):
                # OC advisory contradicts a documented Evolve policy —
                # see _OC_POLICY_OVERRIDES_SUPPRESS for the spec link.
                continue
            if _is_acl_masked_perms_false_positive(check_id, oc_detail):
                # Linux only, getfacl-proven: the "group/other readable-or-
                # writable" bits are the ACL MASK that Evolve's evolve-read ACL
                # (widened to m::rwX by reassert_mask) forces onto st_mode — the
                # real group::/other:: grant nothing. A genuine group/other
                # grant (incl. a real group::r-x, other::r, group::w, or the
                # world-writable check on a real other::w) is NOT suppressed
                # here. See _is_acl_masked_perms_false_positive.
                logger.debug(
                    "audit_oc_security: suppressing ACL-mask false positive "
                    "for %s (%s): %s", bot_id, check_id, oc_detail,
                )
                continue
            what_means, fix_steps = _explain_oc_finding(check_id, remediation)
            # Sub in this bot's real names so copy-pasteable commands
            # are actually runnable.
            fix_steps = (
                fix_steps
                .replace("<bot-user>", bot_user)
                .replace("<bot>", bot_id)
            )
            if sev == "critical":
                msg = f"🔴 CRITICAL: {bot_id} ({check_id}): {title}"
                if remediation:
                    msg += f" — Fix: {remediation}"
                findings.append(Finding(
                    level="critical", category="config", bot_id=bot_id,
                    message=msg,
                    detail=oc_detail,
                    what_it_means=what_means,
                    fix_steps=fix_steps,
                ))
            elif sev in ("warn", "warning"):
                msg = f"{bot_id} ({check_id}): {title}"
                findings.append(Finding(
                    level="warn", category="config", bot_id=bot_id,
                    message=msg,
                    detail=oc_detail or remediation,
                    what_it_means=what_means,
                    fix_steps=fix_steps,
                ))
    else:
        err_text = err_out[0] if err_out else ""
        findings.append(Finding(
            level="warn", category="config", bot_id=bot_id,
            message=f"{bot_id}: OC security audit command failed",
            detail=err_text[:200],
            what_it_means=(
                "The audit runner couldn't complete `openclaw security "
                "audit --deep --json` for this bot. None of the OpenClaw "
                "security checks (gateway auth, trusted proxies, probe "
                "health, etc.) ran — so the bot's OC security state is "
                "unknown, not necessarily bad."
            ),
            fix_steps=(
                "1. Read the stderr in this alert's `detail` field — it's "
                "the actual error message from OC.\n"
                f"2. Try the command by hand on the mini:\n"
                f"   cd /tmp && sudo -u {bot_user} env HOME=/Users/{bot_user} openclaw security audit --deep --json\n"
                f"3. If openclaw itself is the problem, run:\n"
                f"   cd /tmp && sudo -u {bot_user} env HOME=/Users/{bot_user} openclaw doctor\n"
                f"4. If the failure is a sudo grant, check /etc/sudoers.d/evolve "
                f"allows the audit user to run openclaw as {bot_user}."
            ),
        ))

    # Check and enforce openclaw.json permissions.
    #
    # openclaw.json holds the gateway token + every messaging-channel bot token,
    # so it MUST be 0600. /Users/<bot>/ (macOS) and /home/<bot>/ (Linux) are
    # mode 0755 (deploy.py forces it so for shell access), NOT 0700 — so a 0644
    # inner file IS world-readable on a multi-user box. A genuine non-0600 mode
    # (real group/other grant) is a finding and gets corrected to 0600.
    # Grant: §4 of _render_evolve_sudoers (chmod 600).
    # Security fix 2026-06-12 — previously this blessed 0644 and re-set 0644.
    #
    # Read the mode with os.stat (portable: the old `stat -f "%Mp%Lp"` is BSD/
    # macOS-only — on Linux `-f` means --file-system and the format string
    # becomes a bogus operand, so the call ALWAYS failed → a permanent false
    # "cannot stat openclaw.json" on every Linux audit run, the bug this fixes).
    # The evolve service user reads via its named user:evolve ACE, so a direct
    # os.stat suffices (no sudo).
    #
    # Mask-aware on Linux (acl_masked_owner_only, False on macOS): the evolve-
    # read ACL adds user:evolve:r-x, which forces mask::r-x; st_mode's group
    # triad then DISPLAYS the mask, so a genuinely owner-only file reads 0640/
    # 0650 while the real group::/other:: deny. `chmod 600` on such a file would
    # be ACTIVELY HARMFUL on Linux: chmod recalculates the mask to ---, clamping
    # user:evolve to #effective:--- and locking the admin user out of the bot's
    # config pod-wide (feedback_linux_chmod_recalculates_acl_mask_locks_out_evolve).
    # So treat a getfacl-proven mask artifact as already-private and DON'T touch
    # it; only correct a REAL exposure, and re-assert the mask after (no-op on
    # macOS) so the correction itself can't lock evolve out.
    from runtime.perms import get_perms
    oc_path = _bot_home(bot_id) / ".openclaw" / "openclaw.json"
    try:
        mode = oc_path.stat().st_mode & 0o7777
    except (FileNotFoundError, PermissionError):
        findings.append(Finding(
            level="warn", category="config", bot_id=bot_id,
            message=f"{bot_id}: cannot stat openclaw.json for permission check",
        ))
    except OSError:
        findings.append(Finding(
            level="warn", category="config", bot_id=bot_id,
            message=f"{bot_id}: cannot check openclaw.json permissions",
        ))
    else:
        perms = f"{mode:04o}"
        if perms == "0600":
            findings.append(Finding(
                level="ok", category="config", bot_id=bot_id,
                message=f"{bot_id}: openclaw.json permissions OK ({perms})",
            ))
        elif get_perms().acl_masked_owner_only(oc_path):
            findings.append(Finding(
                level="ok", category="config", bot_id=bot_id,
                message=f"{bot_id}: openclaw.json permissions OK "
                        f"({perms}; group bits are an evolve-read ACL-mask "
                        f"artifact, real group/other deny)",
            ))
        else:
            chmod_r = subprocess.run(
                ["sudo", "/bin/chmod", "600", str(oc_path)],
                capture_output=True, timeout=5,
            )
            ok = chmod_r.returncode == 0
            if ok:
                # chmod 600 zeroes the POSIX ACL mask, which also clamps the
                # evolve-read ACE — restore m::rX so the admin read path survives
                # the correction. No-op on macOS (no POSIX mask).
                get_perms().reassert_mask(oc_path)
            findings.append(Finding(
                level="warn", category="config", bot_id=bot_id,
                message=f"{bot_id}: openclaw.json permissions corrected to 0600",
                detail=f"was {perms} (token-bearing; must be 0600), "
                       f"chmod 600 {'succeeded' if ok else 'failed'}",
            ))

    # Update timestamp
    try:
        timestamp_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp_path.write_text(str(now))
    except OSError:
        pass

    if not findings:
        findings.append(Finding(
            level="ok", category="config", bot_id=bot_id,
            message=f"{bot_id}: OC security audit OK",
        ))

    return findings


# ── Alert dispatch ────────────────────────────────────────────────────────────

_CRITICAL_DEDUP_FILE = "audit-critical-dedup.json"
_CRITICAL_RESEND_HOURS = 168  # re-alert even if unchanged after this many hours (7 days)


def _critical_fingerprint(criticals: list[Finding]) -> str:
    """Stable hash of the current critical finding messages."""
    key = "|".join(sorted(f.message for f in criticals))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _should_send_critical(criticals: list[Finding], shared_dir: Path) -> bool:
    """Return True only if findings changed or the last alert is older than resend threshold."""
    fp = _critical_fingerprint(criticals)
    dedup_path = shared_dir / "alerts" / _CRITICAL_DEDUP_FILE
    try:
        data = json.loads(dedup_path.read_text())
        if data.get("fingerprint") != fp:
            return True
        last = datetime.fromisoformat(data["alerted_at"])
        hours_elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        return hours_elapsed >= _CRITICAL_RESEND_HOURS
    except (FileNotFoundError, KeyError, ValueError):
        return True


def _record_critical_sent(criticals: list[Finding], shared_dir: Path) -> None:
    """Write dedup record after sending a critical alert."""
    try:
        alerts_dir = shared_dir / "alerts"
        alerts_dir.mkdir(parents=True, exist_ok=True)
        dedup_path = alerts_dir / _CRITICAL_DEDUP_FILE
        tmp = dedup_path.with_suffix(".json.tmp")
        payload = json.dumps({
            "fingerprint": _critical_fingerprint(criticals),
            "count": len(criticals),
            "alerted_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2)
        tmp.write_text(payload)
        os.replace(tmp, dedup_path)
    except Exception as exc:
        _log(f"[audit] WARNING: could not write dedup record ({exc}); alerts will repeat until this is fixed")


def _audit_signal_title(finding: Finding) -> str:
    """Produce a title that distinguishes findings on the same (category, bot).

    Most audit messages start with the bot_id in one of two shapes —
    ``"security_bot: ..."`` (identity / OK / generic) or ``"security_bot (oc.check_id): ..."``
    (OC security findings). Both bake the bot context into the
    ``message`` so CLI/log readers have it inline, but the Alerts page
    already shows scope as a separate badge and signal_notifier
    prepends the bot independently — so the prefix in the title is
    redundant noise. Strip whichever form is present, then truncate.
    """
    msg = finding.message.strip()
    if finding.bot_id:
        lower = msg.lower()
        bot = finding.bot_id.lower()
        if lower.startswith(bot):
            tail = msg[len(bot):]
            # Strip a single colon or whitespace, plus an optional
            # "(check_id):" parenthetical that OC-security findings use.
            while tail and tail[0] in " :\t":
                tail = tail[1:]
            msg = tail.strip() or msg
    if len(msg) > 80:
        msg = msg[:77].rstrip() + "…"
    return msg or f"{finding.category.capitalize()} {finding.level}"


def _audit_signature(finding: Finding) -> str:
    """Stable signature for a finding's mirrored Signal.

    Phase 3 of docs/spec-alerts-signal-store-2026-05-07.md. Findings
    have no stable "type" field, so we hash the (category, bot_id,
    message) triple to get a key that's stable while the finding
    persists across audit runs and unique across categories.
    """
    from schema.signal import make_signature
    key_input = f"{finding.category}:{finding.bot_id or 'pod'}:{finding.message}"
    short = hashlib.sha256(key_input.encode()).hexdigest()[:16]
    scope_key = f"{finding.bot_id or 'pod'}:{short}"
    return make_signature("audit", finding.category, scope_key)


def _remediation_for_finding(finding: Finding):
    """Map an audit Finding to its server-side remediation, if one exists.

    Phase 4 PR-2: the alerts UI renders a "Run fix" button when a signal
    carries a Remediation. Today only the script-inventory drift coalesced
    finding has a clean mechanical remediation (drop the bot's baseline
    entry; next audit reseeds). Other audit findings either need operator
    judgment (sudoers hash changes are security events worth reading
    before clearing) or have no automation today.

    The mapping is intentionally narrow — it grows by adding cases as
    handlers land in the remediation registry.
    """
    # Lazy-import to avoid circular deps when signals/Remediation pull from
    # schema, and to keep audit importable in contexts without the schema
    # package (some legacy CLI entrypoints).
    try:
        from schema.signal import Remediation
    except ImportError:
        return None

    # Script-inventory drift — the coalesced finding emitted by
    # audit_script_inventory after Phase 3. Match on category + message
    # prefix since Finding has no kind/slug field today.
    if (
        finding.category == "identity"
        and finding.bot_id
        and "script inventory drift" in finding.message
    ):
        return Remediation(
            kind="reset_baseline",
            params={"bot_id": finding.bot_id, "kind": "scripts"},
            label="Reset baseline",
            confirm=(
                f"Drops {finding.bot_id}'s entry from the script-inventory "
                "baseline. The next audit run will reseed it from observed "
                "state, clearing the drift findings. Use this when the "
                "drift is a known/intentional change (e.g. a redeploy) "
                "rather than something suspicious."
            ),
        )

    # Cron-baseline drift — one finding per new cron name. Deploy
    # auto-reblesses Evolve's own additions; this remediation covers the
    # bot-self-installed case (e.g. security_bot's usage-alert-dispatch) where
    # an operator confirms the new job is intentional.
    if (
        finding.category == "config"
        and finding.bot_id
        and "new cron job not in baseline" in finding.message
    ):
        return Remediation(
            kind="reset_baseline",
            params={"bot_id": finding.bot_id, "kind": "cron-jobs"},
            label="Accept current cron jobs",
            confirm=(
                f"Drops {finding.bot_id}'s entry from the cron-jobs "
                "baseline. The next audit run will reseed it from the "
                "bot's current cron/jobs.json, blessing the new job(s). "
                "Use this when the bot legitimately installed a cron "
                "itself; investigate first if the addition is unexpected."
            ),
        )

    # Shell-baseline drift (`.zshrc` hash changed / `.zshrc` deleted).
    #
    # Posture flip 2026-06-06: the original implementation deliberately
    # withheld a remediation here on the rationale "a .zshrc change is a
    # security event — operator should read before resetting." In
    # practice that meant a legitimate operator edit (e.g. evolve's
    # one-line ``source openclaw completion`` line landed during initial
    # account setup) fires the audit forever with no targeted reset
    # path. The only remediation paths were ``evolve-admin audit
    # --reset-baselines`` (wider blast radius — resets every baseline)
    # or hand-editing shell-hashes.json.
    #
    # The flip: provide the per-bot reset remediation, but put the
    # security guardrail in the confirm string so operators have to
    # acknowledge "I read the diff and the change is legitimate" before
    # clicking. The label is intentionally not "Reset" — it's "Accept …
    # after verifying" so the action surface signals what the operator is
    # asserting. ``severity`` stays critical so the first-fire still
    # surfaces loudly; the remediation just unblocks resolution after
    # the operator has investigated.
    if (
        finding.category == "identity"
        and finding.bot_id
        and (
            ".zshrc hash changed since baseline" in finding.message
            or ".zshrc deleted (baseline says present)" in finding.message
        )
    ):
        return Remediation(
            kind="reset_baseline",
            params={"bot_id": finding.bot_id, "kind": "shell"},
            label=f"Accept current .zshrc as new baseline for {finding.bot_id}",
            confirm=(
                f"Reads the current /Users/{finding.bot_id}/.zshrc and "
                f"records its hash as the new baseline, clearing this "
                f"audit_identity signal. The previous baseline hash is "
                f"discarded.\n\n"
                f"SECURITY: this is a one-click acceptance of whatever "
                f"is currently on disk. Click cancel and investigate "
                f"first if any of the following is true:\n"
                f"  - You did not edit /Users/{finding.bot_id}/.zshrc "
                f"yourself.\n"
                f"  - You haven't read the current contents and "
                f"confirmed nothing malicious was inserted (e.g. an "
                f"alias for `ssh`, `sudo`, or `openclaw` that pipes "
                f"input to an attacker, or a PATH entry that shadows "
                f"system binaries).\n"
                f"  - The bot user account ({finding.bot_id}) has been "
                f"compromised within the last day.\n\n"
                f"To inspect first:\n"
                f"  ssh pod-admin-user@mini sudo /bin/cat "
                f"/Users/{finding.bot_id}/.zshrc"
            ),
        )

    return None


_AUDIT_VECTOR_BY_CATEGORY: dict[str, str] = {
    # identity = user accounts / SSH config drift → security exposure
    "identity": "security",
    # config = openclaw config / plugin / auth file changes → security
    "config": "security",
    # machine = firewall, listening ports, sudoers, OC binary mtime → security
    "machine": "security",
    # proposal = proposal queue hygiene (stuck, orphaned) → quality
    "proposal": "quality",
}


# Category × level → magnitude defaults. Most audit findings are advisory
# (mag 1) — script inventory drift, mtime changes, .zshrc unreadable.
# Criticals (unauthorized user accounts, sudoers tamper, firewall off)
# step up to 3-4. A producer-side override is possible later by stashing
# a magnitude on Finding.detail or extending the dataclass; for the
# Phase 2 retrofit, these defaults are the calibration.
_AUDIT_MAGNITUDE_BY_CATEGORY_LEVEL: dict[tuple[str, str], int] = {
    ("identity", "warn"): 1,
    ("identity", "critical"): 3,
    ("config", "warn"): 1,
    ("config", "critical"): 3,
    # machine spans more — firewall off / sudoers tamper are bigger than
    # an mtime change. The runner promotes specific findings via
    # what_it_means; we keep one number here and let composition handle
    # urgency (active=True for ongoing risks).
    ("machine", "warn"): 1,
    ("machine", "critical"): 4,
    ("proposal", "warn"): 1,
    ("proposal", "critical"): 2,
}


def _audit_severity_tag(finding: Finding) -> tuple[str, int]:
    """Return (vector, magnitude) for an audit finding.

    Defaults come from category + level. Spec:
    docs/spec-severity-framework-2026-05-18.md §2.
    """
    vector = _AUDIT_VECTOR_BY_CATEGORY.get(finding.category, "operations")
    magnitude = _AUDIT_MAGNITUDE_BY_CATEGORY_LEVEL.get(
        (finding.category, finding.level),
        2 if finding.level == "warn" else 3,
    )
    return vector, magnitude


def _is_bringup_transient_finding(finding: Finding) -> bool:
    """True for findings that are transient artifacts of fresh-pod bring-up.

    Scoped narrowly to the one self-healing condition the deploy cycles
    through: the ``evolve`` user lacking ACL/sudo read on a bot's
    ``.openclaw`` file (surfaced as ``*unreadable*`` warns). Critical-level
    findings are never transient — they fire regardless of settle state.
    See docs/spec-pod-bringup-settle-2026-06-23.md.
    """
    if finding.level == "critical":
        return False
    message = (finding.message or "").lower()
    detail = (finding.detail or "").lower()
    if "unreadable" in message:
        return True
    # Anticipatory: no current warn/critical finding carries an "ACL … read"
    # detail (the two that do are level="skipped", which _emit_signals_from_
    # findings never iterates). Kept so that if such a finding is ever promoted
    # to warn, it's gated without a code change here.
    if "acl" in detail and "read" in detail:
        return True
    return False


# Benign perm/mode/acl finding family that oscillates fire↔clear as the OC
# gateway re-hardens a bot's file modes / clamps the POSIX ACL mask on its own
# runtime ops and Evolve's deploy/perms self-heal restores them seconds-to-
# minutes later (the Linux-VPS flap — e.g. ``auth-profiles.json mode=640``
# re-clamping ~8×/day on the evo-vps pod). Matched case-insensitively as
# substrings of ``"{message} {detail}"``. CRITICAL findings (genuine
# world-readable credential exposure) are NEVER in this family — see
# ``_is_flap_prone_perm_finding``.
_FLAP_PRONE_PERM_FRAGMENTS: tuple[str, ...] = (
    "permission",       # "openclaw.json permissions corrected to 0600"
    "perms_",           # OC check ids: fs.config.perms_world_readable, …
    " perms",
    "acl",              # ACL mask / evolve-read ACL artifacts
    "group-readable",   # specific so a plain "unreadable" (the settle-gate
    "group readable",   # bring-up family) never matches here
    "world-readable",
    "world readable",
    "group-writable",
    "group writable",
    "world-writable",
    "world writable",
    "mode 0",           # "… (mode 0640)"
    "mode=0",
    "file mode",
    "filemode",
    "secret_mode",
)

# Stable flap_gate ledger ``type`` for audit's perm/mode/acl dwell entries. The
# pending dir is shared across producers, so this lets the per-run clear-sweep
# (``note_cleared_absent``) reset only audit's OWN dwell counters — pod_perms_
# drift / acl_drift keep theirs. is_transient_prone is overridden
# (``transient=True``) so this value is diagnostic + filter only, never the page
# decision.
_AUDIT_PERM_FLAP_TYPE = "audit_perm_flap"


def _is_flap_prone_perm_finding(finding: Finding) -> bool:
    """Whether a finding is in the benign perm/mode/acl family subject to flap
    hysteresis (must dwell N≥2 consecutive runs before paging).

    The Linux-VPS phenomenon: the OC gateway re-hardens a bot's ``.openclaw``
    file modes / clamps the POSIX ACL mask; Evolve auto-restores them shortly
    after. The SAME benign "mode 0640" / "group-readable" finding fires then
    clears every cycle, paging the operator for noise.

    CRITICAL is the must-page floor (docs/spec-drift-alert-taxonomy-2026-06-26.md,
    co-owned with edr): a genuine world-readable credential exposure surfaces at
    CRITICAL severity and MUST page on cycle 1 — it is never flap-gated. Only the
    benign group-readable / mask-artifact / mode-correction family (warn level)
    dwells. ``ok``/``skipped`` findings are never emitted, so the explicit
    ``== "warn"`` guard is what keeps CRITICAL on the immediate path.

    Bring-up transients (the ``.openclaw``/``.zshrc`` *unreadable* class) are the
    settle gate's domain, not the flap gate's — they are excluded here so the two
    mechanisms stay cleanly layered (settle withholds during bring-up; once
    settled an unreadable finding emits immediately, as the settle tests pin).
    """
    if finding.level != "warn":
        return False
    if _is_bringup_transient_finding(finding):
        return False
    haystack = f"{finding.message or ''} {finding.detail or ''}".lower()
    return any(frag in haystack for frag in _FLAP_PRONE_PERM_FRAGMENTS)


def _emit_signals_from_findings(
    criticals: list[Finding],
    warns: list[Finding],
    shared_dir: Path,
    *,
    now: datetime | None = None,
) -> tuple[set[str], dict[str, str]]:
    """Mirror each finding to a Signal; sweep-resolve the rest.

    Returns ``(kept_signatures, critical_signal_ids)`` where
    ``critical_signal_ids`` maps each critical finding's signature to
    the resulting Signal id — used by the Telegram delivery path to
    audit the dispatch on each Signal.

    ``now`` is injectable so the flap-hysteresis dwell ledger is deterministic
    under test; live callers leave it ``None`` (= wall clock).
    """
    try:
        from signals import store as signals_store
    except ImportError:
        return set(), {}

    try:
        from signals import settle_gate
    except ImportError:
        settle_gate = None  # type: ignore[assignment]

    try:
        from signals import flap_gate
    except ImportError:
        flap_gate = None  # type: ignore[assignment]

    kept_signatures: set[str] = set()
    critical_ids: dict[str, str] = {}
    # Signatures of benign perm/mode/acl findings PRESENT this run (whether they
    # paged or are still dwelling). Used by the post-loop clear-sweep to reset
    # the dwell counter for any tracked perm-flap signature now absent.
    present_perm_flap_sigs: set[str] = set()

    for finding in criticals + warns:
        signature = _audit_signature(finding)
        severity = "alert" if finding.level == "critical" else "warn"
        # Fresh-pod bring-up settle gate: withhold transient "evolve can't
        # read .openclaw" findings (the deploy self-heals them seconds later)
        # until the pod settles. alert-level findings are never withheld; see
        # docs/spec-pod-bringup-settle-2026-06-23.md. Withheld findings are
        # NOT added to kept_signatures, so the sweep below leaves any prior
        # firing signal alone and the finding emits normally next run.
        if settle_gate is not None and settle_gate.should_withhold(
            shared_dir,
            severity=severity,
            transient=_is_bringup_transient_finding(finding),
        ):
            continue
        # Flap hysteresis for the benign perm/mode/acl family (the Linux-VPS
        # re-clamp flap). The finding is real this cycle, but if it oscillates
        # fire↔clear it must persist across N≥2 CONSECUTIVE runs before paging,
        # exactly as the pod_perms_drift monitor and the sysadmin watchdog ACL
        # detector already gate. CRITICAL credential-exposure findings are
        # excluded by _is_flap_prone_perm_finding (the must-page-now floor of
        # docs/spec-drift-alert-taxonomy-2026-06-26.md) and fall straight through
        # to observe(). A dwelling finding is withheld and NOT added to
        # kept_signatures, so the sweep below leaves any prior firing Signal
        # alone and the finding emits normally once it dwells. See
        # docs/spec-transient-signal-suppression-2026-06-23.md.
        if flap_gate is not None and _is_flap_prone_perm_finding(finding):
            present_perm_flap_sigs.add(signature)
            verdict = flap_gate.note_observed(
                shared_dir,
                signature=signature,
                transient=True,
                type=_AUDIT_PERM_FLAP_TYPE,
                now=now,
            )
            if not verdict.page:
                continue
        kept_signatures.add(signature)
        scope = "bot" if finding.bot_id else "pod"
        # The chips on the row already show producer + bot + age, so the
        # title should answer "what's wrong" — i.e. the message itself.
        # Generic titles like "Config warn on security_bot" caused four distinct
        # findings on the same (category, bot) to look like duplicates.
        title = _audit_signal_title(finding)
        # Phase 2 severity-framework retrofit — explicit (vector, magnitude)
        # alongside the legacy severity field. The Signal store's resolver
        # reads these from details first, falling back to the legacy field
        # for non-retrofitted producers. Spec: §2.1, §2.2, §2.4.
        vector, magnitude = _audit_severity_tag(finding)
        try:
            details_payload: dict[str, Any] = {
                "category": finding.category,
                "level": finding.level,
                "message": finding.message,
                "detail": finding.detail,
                "vector": vector,
                "magnitude": magnitude,
            }
            # Only include the explanation fields when populated so older
            # findings without them don't render empty sections in the UI.
            if finding.what_it_means:
                details_payload["what_it_means"] = finding.what_it_means
            if finding.fix_steps:
                details_payload["fix_steps"] = finding.fix_steps
            # Body picks the most useful non-title content. The producer's
            # ``message`` is just the title in another form (and often
            # identical after bot-prefix stripping), so prefer
            # ``what_it_means`` (operator-facing explanation) → ``detail``
            # (technical context) → ``message`` (legacy fallback). Without
            # this, signal_notifier and the Alerts page both render the
            # title twice — once as headline, once as body.
            body = (
                (finding.what_it_means or "").strip()
                or (finding.detail or "").strip()
                or finding.message
            )
            sig = signals_store.observe(
                shared_dir,
                signature=signature,
                producer="audit",
                type=f"audit_{finding.category}",
                flavor="maintenance",
                severity=severity,
                scope=scope,
                bot_id=finding.bot_id,
                title=title,
                body=body,
                details=details_payload,
                remediation=_remediation_for_finding(finding),
            )
        except Exception:
            continue
        if finding.level == "critical":
            critical_ids[signature] = sig.id

    # Sweep-resolve audit signals whose conditions are no longer present
    try:
        signals_store.sweep_resolve(
            shared_dir,
            producer="audit",
            kept_signatures=kept_signatures,
            reason="auto-resolve: finding cleared on next audit run",
        )
    except Exception:
        pass

    # Reset the flap-dwell counter for any benign perm/mode/acl signature that
    # was tracked on a prior run but is absent this run — the consecutive-cycle
    # reset that makes a single-cycle flap never accumulate toward the dwell
    # threshold. Scoped to audit's own ledger entries (_AUDIT_PERM_FLAP_TYPE) so
    # it never touches pod_perms_drift / acl_drift counters in the shared dir.
    if flap_gate is not None:
        try:
            flap_gate.note_cleared_absent(
                shared_dir,
                present_perm_flap_sigs,
                type_filter=_AUDIT_PERM_FLAP_TYPE,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort dwell reset
            _log(f"[audit] flap-dwell clear-sweep failed: {exc}")

    return kept_signatures, critical_ids


def _record_telegram_delivery(
    critical_ids: dict[str, str],
    shared_dir: Path,
    *,
    suppressed_reason: str | None = None,
) -> None:
    """Append a Delivery audit entry to each critical Signal."""
    try:
        from signals import store as signals_store
    except ImportError:
        return
    for _signature, signal_id in critical_ids.items():
        located = signals_store.find_signal(shared_dir, signal_id)
        if located is None:
            continue
        sig, _path, _subdir = located
        try:
            signals_store.record_delivery(
                sig, shared_dir, channel="telegram",
                suppressed_reason=suppressed_reason,
            )
        except Exception:
            continue


def dispatch_findings(findings: list[Finding], shared_dir: Path, config: dict, dry_run: bool) -> None:
    """Log all findings; send CRITICAL alerts immediately, accumulate WARNs."""
    criticals: list[Finding] = []
    warns: list[Finding] = []

    for f in findings:
        if f.level == "critical":
            _log(f"[audit] CRITICAL: {f.message}" + (f" — {f.detail}" if f.detail else ""))
            criticals.append(f)
        elif f.level == "warn":
            _log(f"[audit] WARN: {f.message}" + (f" — {f.detail}" if f.detail else ""))
            warns.append(f)
        elif f.level == "skipped":
            # Capability gap (sudoers/ACL/binary missing) — recorded in the
            # log so the operator can find them; not mirrored to signals.
            _log(f"[audit] SKIPPED: {f.message}" + (f" — {f.detail}" if f.detail else ""))
        else:
            _log(f"[audit] OK: {f.message}")

    if dry_run:
        if criticals:
            _log(f"[audit] [dry-run] Would send {len(criticals)} CRITICAL alert(s)")
        return

    # Snapshot first — the report's view of "currently open findings" must not
    # depend on whether alert delivery or dedup bookkeeping succeeded below.
    _write_findings_snapshot(criticals, warns, shared_dir)

    # Phase 3 of the alerts/signal-store consolidation
    # (docs/spec-alerts-signal-store-2026-05-07.md): mirror every
    # finding into the Signal store. Sweep-resolves audit signals
    # whose conditions are no longer present, so a fixed CRITICAL
    # auto-clears on the next run.
    _kept, critical_ids = _emit_signals_from_findings(
        criticals, warns, shared_dir
    )

    # Send one combined CRITICAL alert per run, but deduplicate:
    # only alert when findings change or 7 days have passed since last alert.
    if criticals:
        if _should_send_critical(criticals, shared_dir):
            # Dispatcher sends with parse_mode=HTML — use <b> instead of
            # Markdown *…*. c.message interpolated escaped via
            # _html_escape since findings can contain operator-facing
            # text with arbitrary punctuation.
            lines = ["🔴 <b>Evolve Security Audit — CRITICAL Findings</b>", ""]
            for c in criticals:
                lines.append(f"• {_html_escape(c.message)}")
            _send_security_alert("\n".join(lines), shared_dir, config)
            _record_critical_sent(criticals, shared_dir)
            _record_telegram_delivery(critical_ids, shared_dir)
        else:
            _log(f"[audit] Suppressed duplicate CRITICAL alert ({len(criticals)} finding(s) unchanged)")
            _record_telegram_delivery(
                critical_ids, shared_dir,
                suppressed_reason="dedup: findings unchanged within 7d",
            )

    # Accumulate WARNs into the warn log for weekly review
    for w in warns:
        _send_warn_log(w.message + (f" — {w.detail}" if w.detail else ""), shared_dir)

    _log(f"[audit] Run complete: {len(criticals)} critical, {len(warns)} warn, "
         f"{len([f for f in findings if f.level == 'ok'])} ok")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve security audit")
    parser.add_argument("--network", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="Run checks but don't send alerts or write baselines")
    parser.add_argument("--bot", default=None, help="Audit only this bot (identity + config)")
    parser.add_argument("--category", default=None,
                        choices=["identity", "config", "machine", "proposal", "process", "mcp", "plugins", "hooks", "content_scan", "permissions", "app_permissions"],
                        help="Run only this category of checks")
    parser.add_argument("--reset-baselines", action="store_true",
                        help="Recreate all stored baselines from current state")
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    members = get_members(config)
    primary = config.get("primary")
    all_bots = ([primary] if primary and primary not in members else []) + members
    all_bots = [b for b in all_bots if b]

    if args.bot:
        all_bots = [args.bot]

    if args.reset_baselines:
        _reset_baselines(shared_dir)
        return

    _log(f"[audit] Starting — v{AUDIT_VERSION}, {len(all_bots)} bot(s)")

    all_findings: list[Finding] = []

    run_all = args.category is None

    # Identity audits (per bot) + shell config (all bots at once)
    if run_all or args.category == "identity":
        all_findings.extend(audit_shell_config(all_bots, shared_dir))
        for bot_id in all_bots:
            all_findings.extend(audit_identity(bot_id, shared_dir, primary_bot_id=primary))
            all_findings.extend(audit_script_inventory(bot_id, shared_dir))
            all_findings.extend(audit_workspace_secrets(bot_id, shared_dir))
            all_findings.extend(audit_policy_file_permissions(bot_id))

    if run_all or args.category == "config":
        for bot_id in all_bots:
            all_findings.extend(audit_config(bot_id, config, shared_dir))
            all_findings.extend(audit_cron_health(bot_id, shared_dir))
            all_findings.extend(audit_oc_security(bot_id, shared_dir))
        all_findings.extend(audit_evolve_sudoers(shared_dir, config))

    # Machine audit (pod-wide)
    if run_all or args.category in ("machine", "process"):
        all_findings.extend(audit_machine(shared_dir, config))
        admin_user = config.get("adminUser", "pod_admin")
        all_findings.extend(audit_process(all_bots, admin_user, shared_dir))

    # Cost audit retired in Phase E1 — spend_alert.py is the canonical
    # operator-facing path for cost.daily_threshold. See the deletion
    # site above (formerly audit_cost) for the design rationale.

    # Proposal volume audit
    if run_all or args.category == "proposal":
        all_findings.extend(audit_proposals(shared_dir, config))

    dispatch_findings(all_findings, shared_dir, config, dry_run=args.dry_run)

    # MCP monitor (Phase A of spec-mcp-administration-2026-05-10.md).
    # Owns its own producer name ("mcp_monitor") and emits Signals
    # directly with typed names (mcp_unknown_server, etc.) — it doesn't
    # route through dispatch_findings because that's tied to the
    # category/level shape of Finding which the spec's signals don't
    # match. Runs last so identity/config drift findings on the same
    # bots are visible before MCP-specific drift.
    if run_all or args.category == "mcp":
        try:
            from mcp_admin import monitor as _mcp_monitor
            result = _mcp_monitor.run(
                shared_dir, all_bots, config,
                emit_signals=not args.dry_run,
            )
            adv_refreshed = sum(1 for a in result.get('advisories_refreshed') or [] if a.get('refreshed'))
            _log(
                f"[audit] MCP monitor: {result['bots_checked']} bot(s), "
                f"{result.get('probes_run', 0)} probe(s), "
                f"{adv_refreshed} advisory-pkg(s) refreshed, "
                f"{len(result['findings'])} finding(s), "
                f"{result['swept_resolved']} signal(s) auto-resolved"
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"[audit] MCP monitor failed: {exc}")

    # Plugin monitor (Phase A of spec-plugin-inventory-2026-05-10.md).
    # Same shape as the MCP monitor — emits typed Signals directly with
    # producer "plugin_monitor". Runs after MCP so the two surfaces'
    # signals show up adjacent in the alerts feed when both fire on
    # the same bot.
    if run_all or args.category == "plugins":
        try:
            from plugins import monitor as _plugin_monitor
            result = _plugin_monitor.run(
                shared_dir, all_bots, config,
                emit_signals=not args.dry_run,
            )
            _log(
                f"[audit] Plugin monitor: {result['bots_checked']} bot(s), "
                f"{len(result['findings'])} finding(s), "
                f"{result['swept_resolved']} signal(s) auto-resolved"
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"[audit] Plugin monitor failed: {exc}")

    # Hook monitor (Phase A of spec-hook-governance-2026-05-10.md).
    # Reads both webhook ingress + per-plugin hook policies; emits typed
    # signals via producer "hook_monitor". Runs after plugin monitor so
    # plugin/hook signals show up adjacent in the alerts feed when both
    # fire on the same bot.
    if run_all or args.category == "hooks":
        try:
            from hooks import monitor as _hook_monitor
            result = _hook_monitor.run(
                shared_dir, all_bots, config,
                emit_signals=not args.dry_run,
            )
            _log(
                f"[audit] Hook monitor: {result['bots_checked']} bot(s), "
                f"{len(result['findings'])} finding(s), "
                f"{result['swept_resolved']} signal(s) auto-resolved"
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"[audit] Hook monitor failed: {exc}")

    # Content scanner (Phase A of spec-prompt-injection-scanner-2026-05-10.md).
    # Walks each bot's instruction files (AGENTS.md, SOUL.md, …) + the pod-wide
    # POD_CONDUCT.md, applies the operator-curated pattern catalog, and emits
    # content_scan_* signals. Producer "content_scan". Per-file hash cache means
    # most cycles do near-zero work — only modified files run through the matcher.
    if run_all or args.category == "content_scan":
        try:
            from content_scan import scanner as _content_scanner
            result = _content_scanner.run(
                shared_dir, all_bots, config,
                emit_signals=not args.dry_run,
            )
            _log(
                f"[audit] Content scan: {result['bots_checked']} bot(s), "
                f"{result.get('bots_skipped', 0)} undeployed skipped, "
                f"{result['files_scanned']} file(s), "
                f"{len(result['findings'])} finding(s), "
                f"{result['swept_resolved']} signal(s) auto-resolved"
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"[audit] Content scan failed: {exc}")

    # Permission monitor (Phase C of spec-permission-posture-2026-05-10.md).
    # Reads the three permission surfaces (openclaw.json permission config,
    # exec-approvals.json runtime store, cron/jobs.json scheduled invocations),
    # compares against the operator-curated baseline, runs denylist scans,
    # and emits typed Signals via producer "permission_monitor". Runs after
    # content_scan so all four security monitors land adjacent in alerts.
    if run_all or args.category == "permissions":
        try:
            from permissions import monitor as _perm_monitor
            result = _perm_monitor.run(
                shared_dir, all_bots, config,
                emit_signals=not args.dry_run,
            )
            _log(
                f"[audit] Permission monitor: {result['bots_checked']} bot(s), "
                f"{len(result['findings'])} finding(s), "
                f"{result['swept_resolved']} signal(s) auto-resolved"
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"[audit] Permission monitor failed: {exc}")

    # App-manifest permission monitor (Phase B.1 of
    # spec-app-derived-permissions-2026-05-24.md, implementation sub-spec
    # at spec-app-permission-drift-2026-05-25.md). Compares each bot's
    # manifest declarations against the live exec-approvals.json + workspace
    # state and emits app_permission_drift Signals (one per finding kind:
    # declared-not-allowed, allowed-not-declared, workspace-orphan-script,
    # declared-missing-file). Producer "app_manifest_monitor".
    #
    # Runs after permission_monitor so a deploy-time drift fix from
    # auth_drift_filler doesn't get re-flagged here in the same pass.
    if run_all or args.category in ("permissions", "app_permissions"):
        try:
            from permissions import app_manifest_monitor as _app_monitor
            # network config is the dict we pass everywhere else;
            # config is the runner-wide config dict (may have nothing
            # useful for this monitor) — pass the network shape via
            # whatever is the canonical key. Existing monitors take the
            # combined config dict; we match the pattern.
            result = _app_monitor.run(
                shared_dir, all_bots, config or {},
                emit_signals=not args.dry_run,
            )
            _log(
                f"[audit] App-manifest monitor: {result['bots_checked']} bot(s), "
                f"{len(result['findings'])} finding(s), "
                f"{result['swept_resolved']} signal(s) auto-resolved"
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"[audit] App-manifest monitor failed: {exc}")


def _reset_baselines(shared_dir: Path) -> None:
    """Delete stored baselines so they're recreated on next run."""
    baseline_dir = shared_dir / "security"
    baselines = [
        baseline_dir / "sudoers-evolve.sha256",
        baseline_dir / "user-accounts.baseline",
        baseline_dir / "listening-ports.baseline",
        baseline_dir / "oc-binary-mtime.baseline",
        baseline_dir / "baselines" / "shell-hashes.json",
        baseline_dir / "baselines" / "scripts.json",
        baseline_dir / "baselines" / "cron-jobs.json",
        baseline_dir / "last-process-audit",
    ]
    for b in baselines:
        if b.exists():
            b.unlink()
            _log(f"[audit] Removed baseline: {b.name}")

    # Remove per-bot OC audit timestamps
    for ts_file in baseline_dir.glob("last-oc-audit-*"):
        ts_file.unlink()
        _log(f"[audit] Removed baseline: {ts_file.name}")

    _log("[audit] Baselines reset — they will be recreated on next run")


if __name__ == "__main__":
    main()
