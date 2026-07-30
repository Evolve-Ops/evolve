"""wizard_verify.py — Add-a-Bot verification gauntlet.

Spec: ``docs/spec-wizard-verification-gauntlet-2026-05-30.md``.

Three checks. All three must pass before the wizard declares success:

  1. Ownership audit  — walk /Users/<bot>/.openclaw/ and flag anything
                        not owned by the bot user (with carve-outs for
                        evolve-owned shared subtrees + the root-owned
                        plugin dist).
  2. Agent dry-run    — gateway liveness probe + openclaw config validate
                        + primary-model check + credential-shape check.
                        Stops short of issuing a real Anthropic call.
  3. Channel handshake — re-validate Telegram/Slack/Discord tokens
                        against the upstream API.

Channel pairing (the "operator DMs the bot, bot replies with a code,
operator pastes code back" flow) used to be Check 4 ("End-to-end
echo"). That check promised something the wizard couldn't deliver —
there was no UI to enter the pairing code — and operators hit a
spinner cul-de-sac. As of 2026-06-01 pairing is its own beat: a
dedicated modal launched from the Overview tile chip and the install
wizard's Done screen. See ``evolve_admin.pairing`` /
``routes_pairing`` / the Overview ``🔗 Pair messaging ID`` chip.

Same gauntlet drives the per-tile "Verify" button.

The module is plain Python — no LLM, no async, no external deps beyond
what the admin runtime already imports. Checks are independent and
parallel-safe; the orchestrator runs them sequentially today but can
fan out trivially when Check 2's gateway probe (slow path) starts to
dominate wall-clock.

The admin server runs as ``evolve``; this module assumes the evolve
ACL on ``/Users/<bot>/.openclaw/`` set by
``deploy.set_evolve_read_acl()``. Anything we read goes through that
ACL OR through one of the ``/bin/cat`` sudoers grants in
``setup_wizard._render_evolve_sudoers()``.
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from evolve_util import now_iso_micro as _now_iso

from .config import (
    get_bot_port,
    get_bot_user,
    load_network,
)


_log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

CHECK_OWNERSHIP = "ownership"
CHECK_AGENT_DRY_RUN = "agent_dry_run"
CHECK_CHANNELS = "channels"

CHECK_ORDER = [CHECK_OWNERSHIP, CHECK_AGENT_DRY_RUN, CHECK_CHANNELS]

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"
STATUS_PENDING = "pending"

# Path prefixes under ``/Users/<bot>/.openclaw/`` that are intentionally not
# bot-owned. Walk skips into these via prefix match (interpreted relative to
# the .openclaw/ root). See spec §1 "Carve-outs".
#
# When adding to this list, ALSO update the matching block in the spec doc so
# operators reading the spec see the same list the code enforces.
_OWNERSHIP_EXEMPT_RELATIVE_PREFIXES: tuple[str, ...] = (
    # Plugin code is root-owned by design — the bot must not be able to
    # rewrite the very code that runs with elevated trust.
    "plugins/evolve-plugin",
    # Admin (evolve) owns these subtrees; bot has ACL read/write but not
    # POSIX ownership.
    "workspace/evolve",
    "workspace/evolve-backup",
    "workspace/manifests",
    # Top-of-workspace files that the admin server writes by design. The
    # workspace/ root has an evolve write ACL (deploy.set_evolve_read_acl
    # §"Grant write access to workspace/ root"), and each of these files
    # is admin-authored:
    #   • INSTALLED_APPS.md  — forge install bookkeeping (per-bot)
    #   • POD_CONDUCT.md     — sysadmin conduct injection (session_surface)
    #   • AGENTS.md          — per-bot conduct (admin-rendered)
    #   • RUNTIME_NOTES.md   — pod-wide tactical-runtime channel
    # Without these carve-outs, every healthy bot post-2026-05-28 shows
    # workspace/INSTALLED_APPS.md owner=evolve as a finding (atlas hit this
    # in the gauntlet's first live pass on 2026-05-30). Bot-owned overrides
    # of these files are uncommon but legal — the gauntlet's contract is
    # "don't flag the file at all", not "flag unless evolve-owned", because
    # a root-owned INSTALLED_APPS.md is an admin-server bug, not a
    # bot-config bug, and is out of scope for the verify gauntlet.
    "workspace/INSTALLED_APPS.md",
    "workspace/POD_CONDUCT.md",
    "workspace/AGENTS.md",
    "workspace/RUNTIME_NOTES.md",
    # The workspace/ git repo is touched by many writers — bot agents
    # via local commits, the puller via cloud-sync, operators via ad-hoc
    # commands — so per-object ownership inside .git/ is intentionally
    # heterogeneous. Walking into git internals produces dozens of
    # findings on a healthy bot (atlas had 99 of them on 2026-05-30,
    # all in workspace/.git/objects/). The bot's runtime never reads
    # those object files directly; git does. Out of scope for the
    # gauntlet.
    "workspace/.git",
)

# Regex patterns matched against the BASENAME of any file/dir under
# .openclaw/. Anything matching is exempt regardless of its parent path —
# used for migration leftovers (operator-created backup copies) that turn
# up root-owned but are harmless. Kept narrow on purpose so we don't paper
# over real bugs.
_OWNERSHIP_EXEMPT_BASENAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    # openclaw.json.bak-20260402-220623, openclaw.json.bak.original, etc.
    # Operator-driven backup snapshots created via `sudo cp` before an
    # invasive edit. Root-owned by definition (the `sudo cp` preserves
    # root as the new file's owner). Harmless leftovers; the bot reads
    # only `openclaw.json` so backups don't affect runtime.
    re.compile(r"^openclaw\.json\.bak(?:[-.].*)?$"),
    # openclaw.json.pre-cleanup-2026-05-13, openclaw.json.pre-streaming-off
    # Same shape, different naming convention from older migration scripts.
    re.compile(r"^openclaw\.json\.pre[-.].*$"),
)

# Outbound channel-check timeout — generous but not so long that one slow
# upstream stalls the whole gauntlet.
_CHANNEL_HTTP_TIMEOUT = 8

# Gateway-liveness probe timeout. First call after gateway start can be
# slower than steady-state; 6s leaves room without surfacing every transient
# spike.
_GATEWAY_PROBE_TIMEOUT = 6

# How many times to retry the gateway probe before declaring failure, and
# how long to sleep between attempts. The post-provision Verify gauntlet
# tends to hit the gateway during its 5–10s warm-up window when the OC
# runtime is loading plugins but hasn't bound the /evolve/status route
# yet — single-shot probing turned that into a flaky "not responding"
# verdict that operators had to fix by manually clicking Retry or
# reloading the page. The retry loop is cheap on the success path (one
# probe, returns immediately) and bounded on the failure path
# (_GATEWAY_PROBE_RETRIES extra attempts × _GATEWAY_PROBE_RETRY_DELAY_S).
_GATEWAY_PROBE_RETRIES = 5
_GATEWAY_PROBE_RETRY_DELAY_S = 1.0

# openclaw config validate subprocess timeout. The validator itself is fast
# (~1-2s); the slow path is openclaw's startup.
_CONFIG_VALIDATE_TIMEOUT = 15

# How far back into gateway.err.log we surface when Check 2 fails. ~60
# lines is enough to catch the actual error without burying the operator.
_ERR_LOG_TAIL_LINES = 60


# ── Data classes ─────────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    name: str
    status: str  # one of STATUS_*
    summary: str  # one-line, shown on the row
    detail: str | None = None  # multi-line, shown on expand
    issues: list[dict] = field(default_factory=list)
    fix_available: bool = False
    fix_hint: str | None = None  # deep-link target or sentence of advice
    elapsed_ms: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GauntletResult:
    bot_id: str
    started_at: str
    finished_at: str | None
    checks: list[CheckResult]
    overall: str  # ok / warn / fail / pending

    def to_dict(self) -> dict:
        d = asdict(self)
        d["checks"] = [c.to_dict() if isinstance(c, CheckResult) else c for c in self.checks]
        return d


# ── Public entry points ──────────────────────────────────────────────────────


def run_gauntlet(
    bot_id: str,
    *,
    network: dict | None = None,
    network_path: Path | None = None,
) -> GauntletResult:
    """Run all three checks against ``bot_id`` and return a structured result.

    Never raises — any unexpected exception inside a check is captured
    and surfaced as a FAIL result so the overall gauntlet stays robust.
    """
    if network is None:
        network = load_network(network_path) if network_path else load_network()

    started_at = _now_iso()
    checks: list[CheckResult] = []

    # Check 1 — ownership audit
    checks.append(_safe_run_check(
        CHECK_OWNERSHIP,
        lambda: check_ownership(bot_id, network=network),
    ))

    # Check 2 — agent dry-run
    checks.append(_safe_run_check(
        CHECK_AGENT_DRY_RUN,
        lambda: check_agent_dry_run(bot_id, network=network),
    ))

    # Check 3 — channel handshake
    checks.append(_safe_run_check(
        CHECK_CHANNELS,
        lambda: check_channels(bot_id, network=network),
    ))

    overall = _overall_status(checks)
    return GauntletResult(
        bot_id=bot_id,
        started_at=started_at,
        finished_at=_now_iso(),
        checks=checks,
        overall=overall,
    )


# ── Check 1: ownership audit ─────────────────────────────────────────────────


def check_ownership(bot_id: str, *, network: dict) -> CheckResult:
    """Walk /Users/<bot>/.openclaw/ and flag anything not bot-owned.

    Excludes the prefixes in ``_OWNERSHIP_EXEMPT_RELATIVE_PREFIXES`` —
    those are intentionally evolve- or root-owned and finding them
    would be a false positive.
    """
    t0 = time.monotonic()
    bot_user = get_bot_user(bot_id, network)
    try:
        expected_uid = pwd.getpwnam(bot_user).pw_uid
    except KeyError:
        return CheckResult(
            name=CHECK_OWNERSHIP,
            status=STATUS_FAIL,
            summary=f"macOS user {bot_user!r} not found — bot not provisioned?",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    oc_root = Path(f"/Users/{bot_user}/.openclaw")
    if not oc_root.exists():
        return CheckResult(
            name=CHECK_OWNERSHIP,
            status=STATUS_FAIL,
            summary=f"{oc_root} does not exist — bot not deployed yet?",
            fix_hint="Run the provision pipeline first (wizard Screen 3).",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    issues: list[dict] = []
    # os.walk returns (dirpath, dirnames, filenames). We check the root
    # too — _walk_with_root yields the root dir as its own entry first.
    for path in _walk_with_root(oc_root):
        rel = _relative_to_oc_root(path, oc_root)
        if _is_exempt(rel):
            continue
        try:
            st = path.lstat()
        except (PermissionError, FileNotFoundError, OSError):
            # We rely on the evolve ACL for read; a path we can't stat
            # is almost certainly an ACL-not-applied case, which the
            # ACL re-check below will surface.
            continue
        if st.st_uid != expected_uid:
            issues.append({
                "path": str(path),
                "actual_uid": st.st_uid,
                "actual_owner": _uid_to_name(st.st_uid),
                "expected_owner": bot_user,
                "expected_uid": expected_uid,
            })

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    if not issues:
        return CheckResult(
            name=CHECK_OWNERSHIP,
            status=STATUS_OK,
            summary=f"All paths under {oc_root.name}/ owned by {bot_user}",
            elapsed_ms=elapsed_ms,
        )

    # Cap the user-visible issue list; full list stays in issues[]
    sample = issues[:5]
    sample_str = "\n".join(
        f"  • {i['path']} (owner: {i['actual_owner']})" for i in sample
    )
    extra = "" if len(issues) <= 5 else f"\n  … and {len(issues) - 5} more"
    return CheckResult(
        name=CHECK_OWNERSHIP,
        status=STATUS_FAIL,
        summary=f"{len(issues)} path(s) under .openclaw/ not owned by {bot_user}",
        detail=(
            f"These paths are owned by another user (often root from a sudo write):\n"
            f"{sample_str}{extra}\n\n"
            f"Fix chowns each path back to {bot_user}:staff. Re-runs the gauntlet on success."
        ),
        issues=issues,
        fix_available=True,
        elapsed_ms=elapsed_ms,
    )


def _walk_with_root(root: Path):
    """Yield root, then every entry under it. Mirrors os.walk + the root."""
    yield root
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in dirnames:
            yield base / name
        for name in filenames:
            yield base / name


def _relative_to_oc_root(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace(os.sep, "/")
    except ValueError:
        return ""


def _is_exempt(rel: str) -> bool:
    """True iff ``rel`` (a .openclaw/-relative POSIX path) is in a carve-out.

    Two exemption mechanisms:
      • prefix match against ``_OWNERSHIP_EXEMPT_RELATIVE_PREFIXES`` —
        for directory subtrees and named files at known locations.
      • basename regex match against ``_OWNERSHIP_EXEMPT_BASENAME_PATTERNS``
        — for files whose location is variable but whose name follows a
        well-known pattern (migration backups).
    """
    if not rel:
        return False
    for prefix in _OWNERSHIP_EXEMPT_RELATIVE_PREFIXES:
        if rel == prefix or rel.startswith(prefix + "/"):
            return True
    basename = rel.rsplit("/", 1)[-1]
    return any(p.match(basename) for p in _OWNERSHIP_EXEMPT_BASENAME_PATTERNS)


def _uid_to_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid={uid}"


# ── Check 1 (fix): ownership repair ──────────────────────────────────────────


@dataclass
class RepairResult:
    ok: bool
    path: str
    before: str | None  # owner name before
    after: str | None   # owner name after (None if chown failed)
    error: str | None = None


def repair_ownership(
    bot_id: str,
    path: str,
    *,
    network: dict,
) -> RepairResult:
    """Chown ``path`` recursively back to ``<bot_user>:staff``.

    ``path`` must be under ``/Users/<bot_user>/.openclaw/`` — anything
    else is rejected outright (defense against a hostile or malformed
    request slipping past the API layer). Sudoers grant is the broad
    ``/usr/sbin/chown -R * /Users/*/.openclaw`` line added in PR 1.
    """
    bot_user = get_bot_user(bot_id, network)
    oc_root = Path(f"/Users/{bot_user}/.openclaw")
    p = Path(path)
    try:
        p_resolved = p.resolve()
    except OSError:
        p_resolved = p
    try:
        p_resolved.relative_to(oc_root.resolve())
    except (ValueError, OSError):
        return RepairResult(
            ok=False, path=str(p), before=None, after=None,
            error=f"path {p} is not under {oc_root} — refusing to chown",
        )

    # Snapshot ownership before
    try:
        before = _uid_to_name(p.lstat().st_uid)
    except (FileNotFoundError, PermissionError, OSError):
        before = None

    # Recursive chown. Even for a file path -R is a no-op on the leaf,
    # so we don't need to branch.
    r = subprocess.run(
        ["sudo", "/usr/sbin/chown", "-R", f"{bot_user}:staff", str(p)],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0:
        return RepairResult(
            ok=False, path=str(p), before=before, after=None,
            error=(r.stderr or r.stdout or "chown failed").strip(),
        )

    # Re-snapshot
    try:
        after = _uid_to_name(p.lstat().st_uid)
    except (FileNotFoundError, PermissionError, OSError):
        after = None
    return RepairResult(ok=True, path=str(p), before=before, after=after)


# ── Check 2: agent dry-run ──────────────────────────────────────────────────


def check_agent_dry_run(bot_id: str, *, network: dict) -> CheckResult:
    """Four-step structural verification that the agent could run a turn.

    Order matters: gateway → config → model → credential. Failures
    short-circuit — once the gateway is down, the rest of the steps
    don't add information.
    """
    t0 = time.monotonic()
    bot_user = get_bot_user(bot_id, network)
    port = get_bot_port(bot_id, network)

    # ── Sub-step 1: gateway liveness + plugin loaded ────────────────────────
    if not port:
        return CheckResult(
            name=CHECK_AGENT_DRY_RUN,
            status=STATUS_FAIL,
            summary="No gateway port configured in network.json",
            fix_hint="Run the provision pipeline (wizard Screen 3).",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    loaded, gw_detail = _probe_gateway(port)
    if not loaded:
        tail = _read_gateway_err_tail(bot_user) or ""
        return CheckResult(
            name=CHECK_AGENT_DRY_RUN,
            status=STATUS_FAIL,
            summary=f"Gateway on port {port} not responding to /evolve/status",
            detail=(
                f"Probe result: {gw_detail}\n\n"
                f"Recent gateway.err.log:\n{tail}"
                if tail else f"Probe result: {gw_detail}"
            ),
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    # ── Sub-step 2: openclaw config validate ────────────────────────────────
    valid, validator_issues, validator_error = _run_config_validate(bot_id, network)
    if validator_error:
        return CheckResult(
            name=CHECK_AGENT_DRY_RUN,
            status=STATUS_WARN,
            summary=f"openclaw config validate couldn't run: {validator_error}",
            detail="Skipping remaining sub-steps; fix the validator before re-running.",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    if not valid:
        first = validator_issues[0] if validator_issues else {}
        return CheckResult(
            name=CHECK_AGENT_DRY_RUN,
            status=STATUS_FAIL,
            summary="openclaw.json rejected by the config validator",
            detail=_format_validator_issues(validator_issues),
            issues=validator_issues,
            fix_hint="Open the bot's Config page to inspect/repair.",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    # ── Sub-step 3: primary model configured ────────────────────────────────
    oc_cfg = _read_openclaw_json(bot_user)
    if oc_cfg is None:
        return CheckResult(
            name=CHECK_AGENT_DRY_RUN,
            status=STATUS_FAIL,
            summary=f"Could not read /Users/{bot_user}/.openclaw/openclaw.json",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    primary = (
        oc_cfg.get("agents", {})
              .get("defaults", {})
              .get("model", {})
              .get("primary")
    )
    if not primary:
        return CheckResult(
            name=CHECK_AGENT_DRY_RUN,
            status=STATUS_FAIL,
            summary="agents.defaults.model.primary not set — OC will fall back to a default model",
            detail=(
                "Without an explicit primary model the gateway falls back to whatever OC "
                "ships as default — historically that has meant an OpenAI model even when "
                "the operator only configured an Anthropic key. The atlas onboarding had "
                "this regression as #1701/#1703/#1707. Set agents.defaults.model.primary "
                "in openclaw.json (or use the wizard's Credentials screen)."
            ),
            issues=[{"field": "agents.defaults.model.primary", "value": None}],
            fix_hint="POST /api/wizard/seed-model-config can repair this in place.",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    # ── Sub-step 4: credential shape match ──────────────────────────────────
    auth = _read_auth_profiles(bot_user)
    # When the reader resolved nothing, distinguish a present-but-unreadable
    # store (schema/format drift — a loud advisory) from a genuinely-absent one
    # (un-configured bot). Only probed on the None path; cheap and read-only.
    store_present: bool | None = None
    if auth is None:
        from . import oc_store
        store_present = oc_store.auth_store_present(bot_user, user=bot_user)
    cred_issues = _scan_credential_shape(
        auth, primary, oc_cfg, store_present=store_present,
    )
    if cred_issues:
        # Distinguish "no auth file at all" (could be intentional skip
        # mid-wizard) from "auth file but malformed". Either way a fail.
        return CheckResult(
            name=CHECK_AGENT_DRY_RUN,
            status=STATUS_FAIL,
            summary=cred_issues[0]["summary"],
            detail=_format_credential_issues(cred_issues),
            issues=cred_issues,
            fix_hint="Open the bot's Credentials page to add or repair the key.",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    return CheckResult(
        name=CHECK_AGENT_DRY_RUN,
        status=STATUS_OK,
        summary=f"Gateway up, config valid, primary model {primary} configured, credentials present",
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )


def _probe_gateway_once(port: int) -> tuple[bool, str]:
    """Single probe attempt against /evolve/status.

    Returns (loaded, detail). Loaded=True iff /evolve/status returns
    JSON with one of bot_id/plugin_version/status keys (which only the
    evolve plugin emits — OC's SPA fallback returns 200 HTML).
    """
    url = f"http://127.0.0.1:{port}/evolve/status"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_GATEWAY_PROBE_TIMEOUT) as resp:
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        return False, f"probe failed: {e.reason}"
    except (TimeoutError, OSError, socket.timeout) as e:
        return False, f"probe failed: {e}"

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return False, "200 OK but body is not JSON (likely OC SPA fallback — plugin not loaded)"
    if not isinstance(data, dict):
        return False, "JSON body is not an object"
    if any(k in data for k in ("bot_id", "plugin_version", "status")):
        keys = [k for k in ("bot_id", "plugin_version", "status") if k in data]
        return True, f"plugin loaded (keys: {','.join(keys)})"
    return False, f"JSON missing evolve markers (keys: {','.join(data.keys())[:120]})"


def _probe_gateway(port: int) -> tuple[bool, str]:
    """Retrying gateway probe — calls _probe_gateway_once with backoff.

    The post-provision Verify gauntlet tends to hit the gateway during
    its 5–10s warm-up window when OC has bound the port but hasn't
    finished mounting the evolve plugin's /evolve/status route. A single-
    shot probe turns that transient state into a "not responding" verdict
    that requires the operator to reload the page. The retry loop is
    cheap on the success path (one call, returns immediately) and bounded
    on the failure path (_GATEWAY_PROBE_RETRIES extra attempts ×
    _GATEWAY_PROBE_RETRY_DELAY_S sleep).

    Returns (loaded, detail) — same contract as _probe_gateway_once. On
    success the detail comes from the first successful attempt; on
    failure it's the detail from the final attempt (the most recent
    error state).
    """
    last_detail = ""
    for attempt in range(1 + _GATEWAY_PROBE_RETRIES):
        loaded, detail = _probe_gateway_once(port)
        if loaded:
            return True, detail
        last_detail = detail
        # Don't sleep after the final attempt — we're about to return.
        if attempt < _GATEWAY_PROBE_RETRIES:
            time.sleep(_GATEWAY_PROBE_RETRY_DELAY_S)
    return False, last_detail


def _run_config_validate(
    bot_id: str, network: dict,
) -> tuple[bool, list[dict], str]:
    """Run ``openclaw config validate --json`` against the bot's openclaw.json.

    Delegates to ``openclaw_config_validator.validate_bot_openclaw_json`` so
    the gauntlet stays consistent with the puller's post-pull check.
    Returns (valid, issues, error).
    """
    try:
        from .openclaw_config_validator import validate_bot_openclaw_json
    except Exception as exc:
        return False, [], f"could not import validator: {exc}"
    try:
        return validate_bot_openclaw_json(
            bot_id, network, timeout=_CONFIG_VALIDATE_TIMEOUT,
        )
    except Exception as exc:
        return False, [], f"validator threw: {exc}"


def _read_openclaw_json(bot_user: str) -> dict | None:
    """Read /Users/<bot>/.openclaw/openclaw.json via direct read (ACL) or sudo cat."""
    path = Path(f"/Users/{bot_user}/.openclaw/openclaw.json")
    try:
        return json.loads(path.read_text())
    except PermissionError:
        # Fallback for bots not yet ACL'd
        r = subprocess.run(
            ["sudo", "-n", "/bin/cat", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            return None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _read_auth_profiles(bot_user: str) -> dict | None:
    """Read a bot's auth store as the ``evolve`` user; parsed dict or ``None``.

    Delegates to ``oc_store.read_auth_store`` — the single audited evolve-context
    reader that walks the OpenClaw source ladder (per-agent sqlite store → legacy
    ``auth-profiles.json`` → transitional ``.sqlite-import.<ms>.bak``) across all
    discovered agent dirs (main-first, not hardcoded). OpenClaw 2026.6.9 migrated
    per-agent auth out of the JSON file into ``openclaw-agent.sqlite``; reading
    only the now-deleted JSON returned ``None`` on every migrated pod, which the
    credential-shape check mis-reported as "auth-profiles missing". The sqlite
    ``store_json`` blob carries each profile's full fields (``provider``,
    ``type``, and any ``client_secret``/``value``), so the downstream
    provider/type shape check and the deprecated OAuth ``client_secret_ref``
    fallback both resolve from it unchanged.

    Returns the full parsed auth document ``{version, profiles: {...}}`` (the same
    shape the old JSON read returned). ``None`` means NO readable source existed
    at all (the loud-fail / schema-moved case) — distinct from a readable store
    that legitimately holds zero profiles (returns a ``{"profiles": {}}``-shaped
    dict). Callers MUST NOT treat ``None`` as "this bot has no credentials"; use
    ``oc_store.auth_store_present`` to tell schema-drift from a genuinely-absent
    store.
    """
    from . import oc_store
    raw = oc_store.read_auth_store(bot_user, user=bot_user)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


# Models whose model-id prefix indicates the provider. Used to pick the
# expected credential slot for the credential-shape check. Conservative —
# only the providers Evolve currently ships configurations for.
_MODEL_PROVIDER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("claude", "anthropic"),
    ("anthropic", "anthropic"),
    ("sonnet", "anthropic"),
    ("haiku", "anthropic"),
    ("opus", "anthropic"),
    ("gpt", "openai"),
    ("openai", "openai"),
    ("kimi", "moonshot"),
    ("moonshot", "moonshot"),
)


def _provider_for_model(model_id: str) -> str | None:
    if not model_id:
        return None
    m = model_id.lower()
    for prefix, provider in _MODEL_PROVIDER_PREFIXES:
        if m.startswith(prefix):
            return provider
    return None


# Recognised credential type strings per provider. The canonical profile
# shape is ``{name: "provider:slot", value: {provider, type, key|value, ...}}``
# where the *type* field — not the profile *name* — is what OC's resolver
# uses to pick a credential for a given primary model. Acceptable types
# for each provider are listed below; valid slot names are unconstrained
# (production bots use slots like ``default``, ``api``, ``api_key``, etc.).
#
# Pre-2026-06-01 this dict held expected profile NAMES (``anthropic:api_key``,
# ``anthropic:auth_token``) and `_scan_credential_shape` checked for those
# literal keys in the profiles object. Every production bot uses different
# slot names (production bots use ``anthropic:api``, the wizard-paste path
# writes to ``anthropic:default``), so the check has been a false-positive
# generator since it was written — it only happened to be benign because
# operators rarely run Screen 5 against a working bot. Surfaced 2026-06-01
# when the first successful onboarding hit the gauntlet and Check 2 wrongly
# flagged a working credential as missing.
_EXPECTED_CRED_TYPES: dict[str, tuple[str, ...]] = {
    "anthropic": ("api_key", "auth_token"),
    "openai":    ("api_key",),
    "brave":     ("api_key",),
}

# Scoped to LLM providers (anthropic, openai) only. The [#1752] bug is
# specifically about OC's `agent.profile` resolver dropping underscore-
# named keys; brave is an MCP plugin whose API key is read by
# provider+type lookup (see web/server.py find_brave_credential), so
# `brave_api_key` is intentionally tolerated until the next rotate-time
# rename and must NOT be surfaced as a legacy-shape issue. Adding brave
# back here fans out one alert per bot with no operator-actionable fix.
_LEGACY_KEY_RE = re.compile(r"^(anthropic|openai)_(api_key|auth_token)$")


def _has_credential_for_provider(
    profiles: dict, provider: str, accepted_types: tuple[str, ...],
) -> bool:
    """Return True if any profile satisfies the (provider, type) contract.

    Profile keys are unconstrained (``provider:default``, ``provider:api``,
    ``provider:api_key`` are all valid). What matters is the profile's
    ``provider`` and ``type`` fields, both of which OC's resolver reads.
    """
    for prof in profiles.values():
        if not isinstance(prof, dict):
            continue
        if prof.get("provider") != provider:
            continue
        if prof.get("type") in accepted_types:
            return True
    return False


def _scan_credential_shape(
    auth: dict | None, primary_model: str, oc_cfg: dict,
    *, store_present: bool | None = None,
) -> list[dict]:
    """Return a list of credential-shape issues; empty list = ok.

    Each issue is ``{path, summary, detail, key?}``.

    ``store_present`` disambiguates the ``auth is None`` case (no readable
    credential source) — it is ``oc_store.auth_store_present`` for the bot:
      * ``True``  — a store artifact exists on disk but Evolve couldn't read it.
        That is the loud-fail / schema-moved condition (OpenClaw moved the store
        format under us), NOT "credentials missing". Surface it with the #3248
        "Evolve can't read OpenClaw's credential store" vocabulary so the next
        storage migration fails loud instead of masquerading as a missing key.
      * ``False`` — no store artifact at all: a genuinely un-configured bot.
      * ``None``  — not probed (e.g. the direct unit-test call): keep the
        original conservative "missing or unreadable" wording.
    """
    issues: list[dict] = []
    if auth is None:
        if store_present:
            # Artifact present but unreadable → store format/schema drift, not a
            # missing credential. Mirror #3248's loud-fail vocabulary.
            issues.append({
                "path": "auth-profiles.json",
                "key": None,
                "summary": "Evolve can't read OpenClaw's credential store (format may have changed)",
                "detail": (
                    "A credential store exists for this bot but Evolve could not "
                    "read it through any known layout (the per-agent SQLite store, "
                    "the legacy auth-profiles.json, or a .sqlite-import bak). The "
                    "store format may have changed under us — credential detection "
                    "is degraded until the reader is updated. Routing is unaffected "
                    "(the gateway reads its own store)."
                ),
            })
            return issues
        if store_present is False:
            # Definitively no artifact on disk — a genuinely un-configured bot.
            issues.append({
                "path": "auth-profiles.json",
                "key": None,
                "summary": "No credential store found for this bot",
                "detail": (
                    "The bot has no OpenClaw credential store (no per-agent SQLite "
                    "store, legacy auth-profiles.json, or .sqlite-import bak under "
                    "~/.openclaw/agents/<agent>/agent/). Without it, the agent "
                    "cannot authenticate against any LLM provider."
                ),
            })
            return issues
        # store_present is None — presence not probed (e.g. a direct unit call).
        # Keep the conservative combined wording.
        issues.append({
            "path": "auth-profiles.json",
            "key": None,
            "summary": "Credential store missing or unreadable",
            "detail": (
                "Evolve found no readable credential store for this bot (checked "
                "the per-agent SQLite store, legacy auth-profiles.json, and "
                ".sqlite-import bak). Without it, the agent cannot authenticate "
                "against any LLM provider."
            ),
        })
        return issues

    profiles = auth.get("profiles", {})
    if not isinstance(profiles, dict):
        issues.append({
            "path": "auth-profiles.json::profiles",
            "key": None,
            "summary": "auth-profiles.json is malformed (profiles is not an object)",
        })
        return issues

    # Legacy key shape (the [#1752] bug). Surface every offender — it's
    # not unusual to have multiple legacy entries from the same writer.
    for key in profiles.keys():
        if _LEGACY_KEY_RE.match(str(key)):
            canonical = str(key).replace("_", ":", 1)
            issues.append({
                "path": f"auth-profiles.json::profiles.{key}",
                "key": str(key),
                "canonical": canonical,
                "summary": f"Legacy key shape {key!r} — OC expects {canonical!r}",
                "detail": (
                    "OC's profile resolver matches profile keys by the canonical "
                    "'provider:slot' shape. Underscore-only keys silently fall through "
                    "the matcher; the agent gets no credential and Anthropic API calls "
                    "fall back to OpenAI's default or fail outright. This was the atlas "
                    "[#1752] regression. Rewrite the key with a colon separator."
                ),
            })

    if issues:
        return issues

    # Primary-model credential presence. Only enforce providers we know
    # about. Match by (provider, type) on the profile values, NOT by
    # profile key name — see _EXPECTED_CRED_TYPES docstring for the
    # rationale.
    provider = _provider_for_model(primary_model)
    if provider and provider in _EXPECTED_CRED_TYPES:
        accepted_types = _EXPECTED_CRED_TYPES[provider]
        if not _has_credential_for_provider(profiles, provider, accepted_types):
            type_list = " or ".join(repr(t) for t in accepted_types)
            issues.append({
                "path": "auth-profiles.json::profiles",
                "key": None,
                "summary": (
                    f"No {provider} credential found in auth-profiles.json — "
                    f"no profile has provider={provider!r} with "
                    f"type={type_list} for primary model {primary_model!r}"
                ),
                "detail": (
                    f"The primary model is {primary_model!r}, which requires a "
                    f"{provider} credential. No profile in auth-profiles.json has "
                    f"both provider={provider!r} and type ∈ {accepted_types}. "
                    f"Add the credential through the Credentials page."
                ),
            })

    return issues


def _format_validator_issues(issues: list[dict]) -> str:
    if not issues:
        return ""
    lines = []
    for i, issue in enumerate(issues[:10]):
        path = issue.get("path") or "(root)"
        msg = issue.get("message") or "(no message)"
        lines.append(f"  {i+1}. {path}: {msg}")
    if len(issues) > 10:
        lines.append(f"  … and {len(issues) - 10} more")
    return "openclaw config validate reported:\n" + "\n".join(lines)


def _format_credential_issues(issues: list[dict]) -> str:
    if not issues:
        return ""
    lines = []
    for issue in issues:
        lines.append(f"• {issue.get('summary', '(no summary)')}")
        d = issue.get("detail")
        if d:
            for ln in d.splitlines():
                lines.append(f"    {ln}")
    return "\n".join(lines)


def _read_gateway_err_tail(bot_user: str, lines: int = _ERR_LOG_TAIL_LINES) -> str | None:
    """Read the last N lines of gateway.err.log via the sudo /bin/cat grant."""
    path = Path(f"/Users/{bot_user}/.openclaw/logs/gateway.err.log")
    r = subprocess.run(
        ["sudo", "-n", "/bin/cat", str(path)],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return None
    if not r.stdout:
        return None
    all_lines = r.stdout.splitlines()
    tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
    return "\n".join(tail)


# ── Check 3: channel handshake ───────────────────────────────────────────────


# Channel configurations the gauntlet knows how to re-check. Each entry
# describes how to find the token in network.json / auth-profiles.json
# and how to validate it against the upstream API.
#
# iMessage (added 2026-06-04 with the bundled-plugin rewire) and WhatsApp
# (added 2026-06-04 Phase 1.2) are special cases — their "handshake check"
# is the local OC channels-status probe (or, for WhatsApp, the live
# resolve_status which already runs the probe) rather than an HTTPS call
# to a remote vendor API. They're wired through the same executor below.
_CHANNEL_HANDLERS: dict[str, str] = {
    # bot_cfg-level dict; "telegram" / "slack" / "discord" sub-key.
    "telegram": "_check_telegram_token",
    "slack":    "_check_slack_token",
    "discord":  "_check_discord_token",
    "imessage": "_check_imessage_local",
    "whatsapp": "_check_whatsapp_local",
    "signal":   "_check_signal_local",
}


def check_channels(bot_id: str, *, network: dict) -> CheckResult:
    """Re-validate each configured channel's bot token against the upstream API.

    Returns OK iff every configured channel handshakes successfully.
    Channels with no token configured are skipped (with a note).
    Unsupported channels (whatsapp / signal / others) are reported as
    SKIP with an "unsupported" reason.

    Configuration source-of-truth resolution
    ----------------------------------------

    A channel is "configured" if EITHER:

      1. The bot's ``openclaw.json`` has a ``channels.<channel>`` entry.
         This is where the wizard's set-token endpoints actually write
         (via ``_<channel>.enable_channel_in_oc_config``) — the *primary*
         source of truth post-2026-06-01.

      2. The pod-level ``network.json::bots.<bot>.channels`` has the
         entry (legacy / kept for backwards compat with bots configured
         via the pre-wizard manual path).

      3. A token exists in ``auth-profiles.json`` under a profile key
         starting with the channel name (legacy — earlier wizard
         versions wrote here).

    Pre-2026-06-01 `check_channels` only looked at (2) + (3) and missed
    every wizard-installed channel (which writes to (1)). Surfaced when
    a wizard-installed Telegram channel showed up correctly in the
    wizard's All-Set summary but the verify gauntlet reported
    "No messaging channels configured — nothing to check" for the
    same bot.

    Token lookup follows the same priority: openclaw.json::channels.<ch>.botToken
    first, then auth-profiles.json.
    """
    t0 = time.monotonic()
    bot_user = get_bot_user(bot_id, network)
    bot_cfg = network.get("bots", {}).get(bot_id, {})
    network_channels_cfg = bot_cfg.get("channels", {}) if isinstance(bot_cfg, dict) else {}

    # Read the two source-of-truth files. _read_openclaw_json + _read_auth_profiles
    # both return None on missing/unreadable; we coerce to empty dict so
    # the membership checks below stay safe.
    oc_cfg = _read_openclaw_json(bot_user) or {}
    oc_channels_cfg = oc_cfg.get("channels", {}) if isinstance(oc_cfg, dict) else {}
    auth = _read_auth_profiles(bot_user) or {}
    profiles = auth.get("profiles", {}) if isinstance(auth, dict) else {}

    def _channel_configured(ch: str, token_finder) -> bool:
        if ch in oc_channels_cfg:
            return True
        if ch in network_channels_cfg:
            return True
        if token_finder(profiles):
            return True
        return False

    configured: list[str] = []
    if _channel_configured("telegram", _find_telegram_token):
        configured.append("telegram")
    if _channel_configured("slack", _find_slack_token):
        configured.append("slack")
    if _channel_configured("discord", _find_discord_token):
        configured.append("discord")
    # iMessage uses the openclaw.json channels.imessage block as its
    # source of truth. There is no token to find in auth-profiles.json
    # — the credential is a TCC grant on the evolve user (no token).
    if isinstance(oc_channels_cfg.get("imessage"), dict) \
            and oc_channels_cfg["imessage"].get("handle"):
        configured.append("imessage")
    # WhatsApp — configured when channels.whatsapp.accounts has any
    # entry with a populated authDir. The credential isn't a token; it's
    # the Baileys device-link files in authDir, and the resolve_status
    # call already runs the live probe.
    wa_block = oc_channels_cfg.get("whatsapp")
    if isinstance(wa_block, dict):
        wa_accounts = wa_block.get("accounts") or {}
        if any(
            isinstance(a, dict) and a.get("authDir")
            for a in wa_accounts.values()
        ):
            configured.append("whatsapp")
    # Signal — configured when channels.signal.accounts has any entry
    # with a populated configDir (the signal-cli linked-device state
    # dir). resolve_status runs the live probe.
    sg_block = oc_channels_cfg.get("signal")
    if isinstance(sg_block, dict):
        sg_accounts = sg_block.get("accounts") or {}
        if any(
            isinstance(a, dict) and a.get("configDir")
            for a in sg_accounts.values()
        ):
            configured.append("signal")

    if not configured:
        return CheckResult(
            name=CHECK_CHANNELS,
            status=STATUS_SKIP,
            summary="No messaging channels configured — nothing to check",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    def _resolve_token(ch: str, token_finder) -> str | None:
        # Priority: openclaw.json::channels.<ch>.botToken (where the
        # set-token endpoints actually write), then auth-profiles.json
        # (legacy fallback for earlier wizard versions).
        oc_entry = oc_channels_cfg.get(ch) if isinstance(oc_channels_cfg, dict) else None
        if isinstance(oc_entry, dict):
            for slot in ("botToken", "bot_token", "token"):
                v = oc_entry.get(slot)
                if isinstance(v, str) and v:
                    return v
        return token_finder(profiles)

    # Run all configured channel checks in parallel — each is a single
    # HTTPS call with a short timeout.
    issues: list[dict] = []
    detail_lines: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, len(configured))) as pool:
        futures = {}
        for ch in configured:
            if ch == "telegram":
                tok = _resolve_token("telegram", _find_telegram_token)
                futures[pool.submit(_check_telegram_token, tok)] = ch
            elif ch == "slack":
                tok = _resolve_token("slack", _find_slack_token)
                futures[pool.submit(_check_slack_token, tok)] = ch
            elif ch == "discord":
                tok = _resolve_token("discord", _find_discord_token)
                futures[pool.submit(_check_discord_token, tok)] = ch
            elif ch == "imessage":
                # Local CLI probe via the bundled-plugin status check —
                # no token, so we pass bot_id instead. ``resolve_status``
                # times out internally (default 12s) so we don't need to
                # set a futures-level timeout.
                futures[pool.submit(_check_imessage_local, bot_id)] = ch
            elif ch == "whatsapp":
                # Same pattern as iMessage — local probe via the install
                # module's resolve_status (internal 12s timeout).
                futures[pool.submit(_check_whatsapp_local, bot_id)] = ch
            elif ch == "signal":
                # Same pattern — local probe via signal_install.resolve_status.
                # LICENSING NOTE: see signal_install module docstring.
                futures[pool.submit(_check_signal_local, bot_id)] = ch
        for future, ch in futures.items():
            try:
                ok, summary, info = future.result(timeout=_CHANNEL_HTTP_TIMEOUT + 2)
            except Exception as exc:
                ok, summary, info = False, f"{ch}: probe crashed: {exc}", {}
            line_status = "✓" if ok else "✗"
            detail_lines.append(f"  {line_status} {ch}: {summary}")
            if not ok:
                issues.append({
                    "channel": ch,
                    "summary": summary,
                    "info": info,
                })

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    if not issues:
        return CheckResult(
            name=CHECK_CHANNELS,
            status=STATUS_OK,
            summary=f"{len(configured)} channel(s) reachable",
            detail="\n".join(detail_lines),
            elapsed_ms=elapsed_ms,
        )

    return CheckResult(
        name=CHECK_CHANNELS,
        status=STATUS_FAIL,
        summary=f"{len(issues)} of {len(configured)} channel(s) failed handshake",
        detail="\n".join(detail_lines),
        issues=issues,
        fix_hint="Rotate the failing token via the bot's Channels page.",
        elapsed_ms=elapsed_ms,
    )


def _find_telegram_token(profiles: dict) -> str | None:
    """Walk profiles for a Telegram bot token in any plausible key shape."""
    for key, entry in profiles.items():
        if not isinstance(key, str):
            continue
        if not key.startswith("telegram"):
            continue
        if not isinstance(entry, dict):
            continue
        for slot in ("bot_token", "token"):
            v = entry.get(slot)
            if isinstance(v, str) and v:
                return v
    return None


def _find_slack_token(profiles: dict) -> str | None:
    for key, entry in profiles.items():
        if not isinstance(key, str):
            continue
        if not key.startswith("slack"):
            continue
        if not isinstance(entry, dict):
            continue
        for slot in ("bot_token", "token", "botToken"):
            v = entry.get(slot)
            if isinstance(v, str) and v:
                return v
    return None


def _find_discord_token(profiles: dict) -> str | None:
    for key, entry in profiles.items():
        if not isinstance(key, str):
            continue
        if not key.startswith("discord"):
            continue
        if not isinstance(entry, dict):
            continue
        for slot in ("bot_token", "token"):
            v = entry.get(slot)
            if isinstance(v, str) and v:
                return v
    return None


def _check_telegram_token(token: str | None) -> tuple[bool, str, dict]:
    if not token:
        return False, "no Telegram token in auth-profiles.json", {}
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_CHANNEL_HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
            err = json.loads(err_body).get("description", str(e))
        except Exception:
            err = str(e)
        return False, f"Telegram: {err}", {"status": e.code}
    except Exception as e:
        return False, f"Telegram: probe failed ({e})", {}
    if not data.get("ok"):
        return False, f"Telegram: {data.get('description', 'unknown error')}", data
    username = (data.get("result") or {}).get("username")
    return True, f"Telegram: @{username}", {"username": username}


def _check_slack_token(token: str | None) -> tuple[bool, str, dict]:
    if not token:
        return False, "no Slack token in auth-profiles.json", {}
    url = "https://slack.com/api/auth.test"
    try:
        req = urllib.request.Request(
            url, method="POST",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=_CHANNEL_HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
    except Exception as e:
        return False, f"Slack: probe failed ({e})", {}
    if not data.get("ok"):
        return False, f"Slack: {data.get('error', 'unknown error')}", data
    return True, f"Slack: team {data.get('team', '?')} (user @{data.get('user', '?')})", data


def _check_discord_token(token: str | None) -> tuple[bool, str, dict]:
    if not token:
        return False, "no Discord token in auth-profiles.json", {}
    url = "https://discord.com/api/v10/users/@me"
    try:
        req = urllib.request.Request(
            url, method="GET",
            headers={"Authorization": f"Bot {token}"},
        )
        with urllib.request.urlopen(req, timeout=_CHANNEL_HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
    except urllib.error.HTTPError as e:
        return False, f"Discord: HTTP {e.code}", {"status": e.code}
    except Exception as e:
        return False, f"Discord: probe failed ({e})", {}
    name = data.get("username") or data.get("global_name") or "?"
    return True, f"Discord: bot @{name}", data


def _check_whatsapp_local(bot_id: str) -> tuple[bool, str, dict]:
    """Verify WhatsApp by running ``whatsapp_install.resolve_status``.

    Like iMessage, WhatsApp's handshake check is local (the bot's gateway
    talks Baileys to WhatsApp servers directly, not through an Evolve-side
    HTTP probe). The install resolver already runs the
    ``openclaw channels status --probe`` live check with a short timeout.
    """
    try:
        from .skills import whatsapp_install as _wa
    except Exception as exc:
        return False, f"WhatsApp: install module unavailable ({exc})", {}

    try:
        status = _wa.resolve_status(bot_id)
    except Exception as exc:
        return False, f"WhatsApp: status probe crashed ({exc})", {}

    info = {
        "state": status.status,
        "account_id": status.account_id,
        "paired_phone": status.paired_phone,
        "oc_probe_ok": status.oc_probe_ok,
    }
    if status.status == "active":
        suffix = f" ({status.paired_phone})" if status.paired_phone else ""
        return True, f"WhatsApp: connected{suffix}", info

    summaries = {
        "plugin_not_installed": "WhatsApp plugin not installed",
        "account_not_paired": "no account paired yet",
        "auth_dir_corrupt": "linked-device files missing or unreadable",
        "disabled": "WhatsApp is turned off for this bot",
        "oc_probe_failed": status.oc_probe_detail or "OpenClaw probe not responding",
        "unknown": status.error or "unknown error",
    }
    detail = summaries.get(status.status, status.status)
    return False, f"WhatsApp: {detail}", info


def _check_signal_local(bot_id: str) -> tuple[bool, str, dict]:
    """Verify Signal by running ``signal_install.resolve_status``.

    Same shape as ``_check_whatsapp_local`` — local probe via the install
    resolver, no Evolve-side HTTP call. The resolver runs OC's
    ``openclaw channels status --channel signal --probe`` against the
    bundled @openclaw/signal plugin.

    LICENSING NOTE: see ``evolve_admin.skills.signal_install`` module
    docstring. This check is harmless on its own (no signal-cli code is
    touched) but if the licensing review withdraws the skill, this hook
    + the ``_CHANNEL_HANDLERS`` entry above should be commented out too.
    """
    try:
        from .skills import signal_install as _sg
    except Exception as exc:
        return False, f"Signal: install module unavailable ({exc})", {}

    try:
        status = _sg.resolve_status(bot_id)
    except Exception as exc:
        return False, f"Signal: status probe crashed ({exc})", {}

    info = {
        "state": status.status,
        "paired_number": status.paired_number,
        "device_name": status.device_name,
        "oc_probe_ok": status.oc_probe_ok,
    }
    if status.status == "active":
        suffix = f" ({status.paired_number})" if status.paired_number else ""
        return True, f"Signal: connected{suffix}", info

    summaries = {
        "plugin_not_installed": "Signal plugin not installed",
        "number_not_captured": "phone number not entered yet",
        "account_not_paired": "no account paired yet",
        "config_dir_corrupt": "linked-device files missing or unreadable",
        "disabled": "Signal is turned off for this bot",
        "oc_probe_failed": status.oc_probe_detail or "OpenClaw probe not responding",
        "unknown": status.error or "unknown error",
    }
    detail = summaries.get(status.status, status.status)
    return False, f"Signal: {detail}", info


def _check_imessage_local(bot_id: str) -> tuple[bool, str, dict]:
    """Verify iMessage by running ``imessage_install.resolve_status``.

    Unlike the other channel checkers, iMessage has no remote API to
    hit — its "handshake" is the local OC channels-status probe plus
    the TCC grant state. We import lazily to avoid pulling the install
    module on platforms where iMessage is not relevant.

    Returns ``(ok, summary, info)``. ``ok=True`` only when the resolver
    returns the ``active`` state (TCC ok, handle set, OC plugin wired,
    AND live probe confirms connected — same load-bearing rule as the
    install-route status check).
    """
    try:
        from .skills import imessage_install as _ii
    except Exception as exc:
        return False, f"iMessage: install module unavailable ({exc})", {}

    try:
        status = _ii.resolve_status(bot_id)
    except Exception as exc:
        return False, f"iMessage: status probe crashed ({exc})", {}

    info = {
        "state": status.status,
        "handle": status.imessage_handle,
        "oc_channel_wired": status.oc_channel_wired,
        "oc_plugin_enabled": status.oc_plugin_enabled,
        "oc_probe_ok": status.oc_probe_ok,
    }
    if status.status == "active":
        return True, f"iMessage: connected as {status.imessage_handle}", info

    # Map states to short operator-readable summaries — same Plex-test
    # discipline as the access panel.
    summaries = {
        "no_tcc_fda": "Full Disk Access not granted to Evolve",
        "no_tcc_automation": "Automation grant missing for the Messages app",
        "messages_not_running": "Messages app isn't running",
        "not_signed_in": "Messages app isn't signed in to iMessage",
        "handle_not_configured": "iMessage address not set for this bot",
        "not_wired_to_oc": "needs one-click finish-setup",
        "oc_probe_failed": status.oc_probe_detail or "OpenClaw probe not responding",
        "unknown": status.error or "unknown error",
    }
    detail = summaries.get(status.status, status.status)
    return False, f"iMessage: {detail}", info


# ── Check 4 — REMOVED ────────────────────────────────────────────────────────
# The "End-to-end echo" check (arm_e2e_contract / poll_e2e_status /
# disarm_e2e_contract) was removed 2026-06-01. It promised something
# the wizard couldn't deliver: a UI for entering the OC pairing code
# the bot replies with on first DM. Operators hit a spinner cul-de-sac.
#
# Pairing is now its own beat — a dedicated modal launched from the
# Overview tile chip ("🔗 Pair messaging ID") and the install wizard's
# Done screen. See ``evolve_admin.pairing`` /
# ``evolve_admin.web.routes_pairing`` / the chip computation in
# ``packages/analyzer/pairing_chip.py``.
#
# If you're chasing a removed symbol referenced by an old caller:
#   CHECK_E2E_ECHO          → gone; CHECK_ORDER is now three checks
#   include_e2e/e2e_session → gone from run_gauntlet signature
#   arm_e2e_contract        → use the pairing wizard modal instead
#   disarm_e2e_contract     → no contract to disarm; nothing to call
#   poll_e2e_status         → no contract to poll; pairing wizard's
#                             ``commit`` endpoint is the new success signal


# ── Helpers ──────────────────────────────────────────────────────────────────


def _overall_status(checks: list[CheckResult]) -> str:
    """Reduce per-check statuses to an overall result.

    Rules:
      - any FAIL → fail
      - any WARN → warn
      - any PENDING (and no FAIL/WARN) → pending
      - otherwise → ok
    Skip rows do not affect the overall result.
    """
    states = [c.status for c in checks]
    if STATUS_FAIL in states:
        return STATUS_FAIL
    if STATUS_WARN in states:
        return STATUS_WARN
    if STATUS_PENDING in states:
        return STATUS_PENDING
    return STATUS_OK


def _safe_run_check(name: str, fn) -> CheckResult:
    """Wrap a check callable so it never bubbles an exception."""
    try:
        return fn()
    except Exception as exc:
        _log.exception("wizard_verify: check %r crashed", name)
        return CheckResult(
            name=name,
            status=STATUS_FAIL,
            summary=f"check crashed: {exc}",
            detail=f"This is a bug in the verification gauntlet itself, not the bot. "
                   f"Capture this message and the admin server log; file as an issue.",
        )
