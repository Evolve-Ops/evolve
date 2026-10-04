"""infra_audit.py — Admin-side pod-infrastructure audit (Workstream B-infra).

The existing app-audit framework verifies per-app contracts against reality
on each bot. This module extends that pattern to pod-wide infrastructure
which isn't bot-owned and can't be audited by the bot-side runner:

  1. Managed daemons (admin-ui, mcp-bridge, verify, repo-puller, retention)
  2. Sudoers entries (/etc/sudoers.d/evolve and /etc/sudoers.d/evolve-admin)
  3. ACL state on shared + per-bot paths
  4. network.json schema validity
  5. Repo-puller freshness (last successful pull < 30 min)
  6. Signal-store retention (last run < 24 hours)

Architecture parallels app-audit:

  ┌──────────────────────────────────────────────┐
  │ infra_audit.run()  (admin-side, evolve user) │
  │   gather diagnostics → two-stage LLM         │
  │   → write outbox records to                  │
  │     {shared_dir}/infra_audit_outbox/         │
  │   → append trail lines to                    │
  │     {shared_dir}/infra_audits/<elem>/trail   │
  └──────────────────────────────────────────────┘
                       │
                       ▼ next scheduler tick
  ┌──────────────────────────────────────────────┐
  │ audit_poller.tick()                          │
  │   drains shared infra_audit_outbox/ in       │
  │   addition to each bot's audit_outbox/       │
  │   → Signal store / Proposal store            │
  └──────────────────────────────────────────────┘

Calibration discipline matches app-audit: `auto_fix` is demoted to
`propose` at the orchestration layer in v1 (calibration_mode=True
default). Findings always escalate to Proposals; the operator approves
manually. v1 has no auto-remediation path at all — spec §4.3 + §5.5 of
the audit-extensions spec.

The runner has no external daemon. It's invoked from the existing
admin scheduler tick on a daily cadence (gated by `infra_audit.cadence`
in the pod config). On-demand runs come from `evo audit infra` and a
"Run infra audit now" button in the Pod Health UI.

See internal/spec-audit-extensions-2026-05-17.md §4.3.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from evolve_util import atomic_write_json as _atomic_write_json
from evolve_util import now_iso as _iso_now

from ..runtime import LaunchdScheduler, get_launchd_scheduler


logger = logging.getLogger(__name__)


# Module version stamped into every outbox/trail record so a stale record on
# disk after an upgrade is identifiable.
INFRA_AUDIT_VERSION = "1.0.0"

# Canonical pod-infrastructure daemons. We don't audit every Evolve-installed
# plist (that's what `expected_plist_labels` is for) — only the ones whose
# absence breaks the pod's operational backbone. Per-bot plists are bot-owned
# and audited by app-audit's structural verifier.
#
# Spec §4.3 enumerates: admin-ui, mcp-bridge, verify, repo-puller, retention.
# We add signal-notifier + pod-health since they're the alert delivery path
# (locked layer per spec-alerts-signal-store-2026-05-07).
#
# All canonical infra daemons run system-scope (LaunchDaemons under
# /Library/LaunchDaemons/). The user-scope LaunchAgent special-case used to
# exist for mcp-bridge — that turned out to be the wrong substrate for a
# headless pod (user-scope gui/<uid> domains require an Aqua login session,
# which evolve pods don't have for the admin user) so mcp-bridge was
# converted to ai.evolve.evolve.mcp-bridge in PR #1XYZ. The canonical label
# is taken from mcp_service.LABEL so the two sources can't drift.

try:
    from evolve_admin.mcp_service import LABEL as _MCP_BRIDGE_LABEL
except Exception:  # pragma: no cover - admin not installed
    _MCP_BRIDGE_LABEL = "ai.evolve.evolve.mcp-bridge"

# Backward-compatible flat tuple of labels (kept so existing callers /
# tests that iterate CORE_INFRA_DAEMONS still work).
CORE_INFRA_DAEMONS: tuple[str, ...] = (
    "ai.evolve.evolve.admin-ui",
    "ai.evolve.evolve.repo-puller",
    "ai.evolve.evolve.verify",
    "ai.evolve.evolve.retention",
    "ai.evolve.evolve.signal-notifier",
    "ai.evolve.evolve.pod-health",
    _MCP_BRIDGE_LABEL,
)

# All canonical infra daemons are LaunchDaemons under /Library/LaunchDaemons/.
# The dict shape (kind per label) is preserved so the audit's per-label
# logic doesn't have to re-derive intent every release, and so a future
# legitimate user-scope service has a place to land — but adding a new
# "agent" entry MUST come with a written justification in
# `docs/policy/launchd-scope.md`.
CORE_INFRA_DAEMON_KINDS: dict[str, str] = {
    "ai.evolve.evolve.admin-ui":        "system",
    "ai.evolve.evolve.repo-puller":     "system",
    "ai.evolve.evolve.verify":          "system",
    "ai.evolve.evolve.retention":       "system",
    "ai.evolve.evolve.signal-notifier": "system",
    "ai.evolve.evolve.pod-health":      "system",
    _MCP_BRIDGE_LABEL:                  "system",
}


# Load-time guardrail: any kind="agent" entry above MUST be paired with a
# justification in `_scope_policy.JUSTIFIED_AGENTS`. The full enforcement
# lives in `tests/test_launchd_scope_policy.py` (CI-blocking); this log
# is the runtime defense-in-depth so a slipped-through agent surfaces in
# the admin-ui logs immediately rather than waiting for the next CI run.
def _check_agent_justifications() -> None:
    try:
        from ._scope_policy import is_justified  # noqa: WPS433 (local import)
    except Exception:
        # Policy module missing in some deploy snapshot — soft-fail rather
        # than crash the audit at import time. CI will catch the real shape.
        return
    for label, kind in CORE_INFRA_DAEMON_KINDS.items():
        if kind == "agent" and not is_justified(label):
            logger.warning(
                "infra_audit: unjustified LaunchAgent %s in CORE_INFRA_DAEMON_"
                "KINDS — add an entry to _scope_policy.JUSTIFIED_AGENTS and "
                "a section in docs/policy/launchd-scope.md. See "
                "https://github.com/evolve-ops/evolve/pull/1821 for the "
                "headless-pod failure mode this guardrail catches.",
                label,
            )


_check_agent_justifications()


# Sudoers files we audit. The contents are managed by setup_wizard's
# _render_evolve_sudoers / _write_sudoers, so we don't try to parse line
# semantics — we just check the file exists, parses via `visudo -c`, and
# contains a handful of grants we know are load-bearing.
SUDOERS_EVOLVE = Path("/etc/sudoers.d/evolve")
SUDOERS_EVOLVE_ADMIN = Path("/etc/sudoers.d/evolve-admin")

# Grants that must be present in /etc/sudoers.d/evolve. These are the lines
# without which the admin server cannot operate — losing one is a critical
# finding. (The full sudoers file has ~60 grants; we don't list them all
# because the canonical list is _render_evolve_sudoers in setup_wizard.py
# and pinning every line here creates a brittle drift target.)
#
# Platform-keyed: the service-manager + daemon-install verbs differ between
# launchd (macOS) and systemd (Linux). A single macOS-shaped list would
# false-positive `sudoers_required_grant_missing` on a Linux pod the moment
# the content check can actually read the file (the §23 cat grant added in
# this change makes that read succeed where it previously always returned
# None). Entries are SUBSTRINGS — the check is `grant in contents` — so each
# is a stable fragment of a real rendered line, sourced from the same
# profile command table _render_evolve_sudoers renders from.
def _required_evolve_sudoers_grants() -> tuple[str, ...]:
    profile = _get_profile()
    svc = profile.service_manager
    if profile.name == "macos":
        return (
            # Without launchctl list, the admin server can't see daemon status.
            f"{svc} list",
            # Without launchctl kickstart, no daemon restarts from the UI.
            f"{svc} kickstart -k system/ai.evolve.*",
            # Without cp /tmp/*.plist <daemon-dir>, install-infra-jobs can't
            # write new plists.
            f"{profile.cp} /tmp/*.plist {profile.daemon_dir}/",
            # Without cat, the read-fallback path for bot files breaks.
            profile.cat,
        )
    # systemd / Linux: no `list`/`kickstart` verbs; the SystemdScheduler argv
    # (mirrored by _render_evolve_sudoers' Linux branch) uses daemon-reload +
    # restart for both the ai.evolve.* and ai.openclaw.* unit families.
    return (
        f"{svc} daemon-reload",
        f"{svc} restart ai.evolve.*",
        f"{svc} restart ai.openclaw.*",
        profile.cat,
    )

# Path the repo-puller writes its status log to (used by `health.py`).
# Platform-keyed off the profile's shared-dir default (W10-F #8a) — a
# hardcoded /Users/Shared path made this check look at a file that never
# exists on a Linux pod (puller logs to /var/lib/evolve/logs/). platform_profile
# is a leaf module (no import cycle), so resolve it here at module load.
from platform_profile import get_profile as _get_profile  # noqa: E402
REPO_PULLER_LOG = Path(_get_profile().shared_dir_default) / "logs" / "repo-puller.log"
REPO_PULLER_STALE_AFTER_S = 30 * 60   # spec §4.3: "< 30 min"

# Path the signal-store retention job is expected to touch each day.
# The job runs from cli.py `retention` subcommand under the
# ai.evolve.evolve.retention LaunchDaemon and writes to today's log.
SIGNAL_RETENTION_STALE_AFTER_S = 24 * 60 * 60  # spec §4.3: "< 24 hours"

# Calibration / cadence defaults. Mirrors app-audit's defaults so behavior is
# predictable for operators familiar with that surface.
DEFAULT_INFRA_CADENCE = "daily"
VALID_INFRA_CADENCES = ("off", "daily", "weekly", "manual")


# ── Path helpers ────────────────────────────────────────────────────────────


def infra_audit_outbox_dir(shared_dir: Path) -> Path:
    """Outbox the admin runner writes to; poller drains it."""
    return Path(shared_dir) / "infra_audit_outbox"


def infra_audit_outbox_ingested(shared_dir: Path) -> Path:
    return infra_audit_outbox_dir(shared_dir) / "_ingested"


def infra_audits_root(shared_dir: Path) -> Path:
    """Root for per-element trails: {shared_dir}/infra_audits/<element>/."""
    return Path(shared_dir) / "infra_audits"


def element_trail_path(shared_dir: Path, element: str) -> Path:
    """Trail file for a specific element (e.g. 'sudoers', 'daemons')."""
    safe = "".join(c for c in element if c.isalnum() or c in "-_") or "unknown"
    return infra_audits_root(shared_dir) / safe / "trail.jsonl"


# ── Data shapes ─────────────────────────────────────────────────────────────


@dataclass
class Finding:
    """One observation about the pod's infrastructure state.

    Mirrors app-audit's Observation shape so the poller's record dispatch
    can treat them uniformly. ``element`` names the audit category (one
    of `daemons`, `sudoers`, `acls`, `network_json`, `repo_puller`,
    `signal_retention`); ``signature`` is the dedup key (so re-running
    against the same broken state doesn't pile Proposals).
    """
    element: str
    severity: str           # critical | major | minor | info
    category: str           # short slug, e.g. "daemon_not_loaded"
    description: str
    evidence: dict[str, Any] = field(default_factory=dict)
    suggested_fix: str = ""
    # Optional structured rationale tag set by the check function (not the
    # triage layer). Used to surface why a finding picked one category over
    # another — e.g. "aqua_session_missing; cannot_bootstrap_gui_domain" for
    # daemon_load_infeasible. Threaded through to the outbox record so the
    # downstream Proposal carries the same context.
    rationale: str = ""

    def signature(self) -> str:
        """Stable dedup key. Hashes element + category + a canonical key
        from evidence so the same finding doesn't duplicate Proposals."""
        ev_key = self.evidence.get("path") or self.evidence.get("label") \
            or self.evidence.get("key") or self.evidence.get("file") or ""
        h = sha256(f"{self.element}|{self.category}|{ev_key}".encode("utf-8"))
        return f"infra_audit:{self.element}:{self.category}:{h.hexdigest()[:12]}"


@dataclass
class AuditRunResult:
    """End-to-end output of one infra-audit run."""
    run_id: str
    started_at: str
    completed_at: str
    findings: list[Finding] = field(default_factory=list)
    outcomes: dict[str, int] = field(default_factory=lambda: {
        "dismiss": 0, "auto_fix": 0, "propose": 0,
    })
    elements_checked: list[str] = field(default_factory=list)
    elements_skipped: list[str] = field(default_factory=list)
    error: str = ""
    llm_used: bool = False
    tokens_used: int = 0

    def status(self) -> str:
        if self.error:
            return "failed"
        if self.findings:
            return "with_findings"
        return "clean"


# ── Diagnostics gatherer (pure Python, no LLM) ──────────────────────────────


def gather_diagnostics(
    *,
    network: Optional[dict] = None,
    shared_dir: Optional[Path] = None,
    elements: Optional[list[str]] = None,
    # Test injection points — defaults walk the live filesystem.
    launchctl_list_fn: Optional[Callable[[str], tuple[bool, str]]] = None,
    visudo_check_fn: Optional[Callable[[Path], tuple[str, str]]] = None,
    now: Optional[_dt.datetime] = None,
) -> list[Finding]:
    """Walk every infrastructure category and return raw findings.

    The result is purely a list of *observed* divergences from expected
    state. No LLM yet — this is the deterministic stage. The LLM in
    `run()` consumes this list to do triage (dismiss / propose) and add
    context, but the gathering itself is reproducible and testable
    without external dependencies.

    ``elements`` filters to a subset of categories (used by tests and
    the per-element retry path). ``None`` means run every check.

    Test seams:
      - ``launchctl_list_fn(label) -> (loaded, status_hint)`` — overrides
        the real `launchctl list <label>` shell-out. ``status_hint`` is
        either ``""`` (authoritative result) or ``"cannot_escalate"``
        (sudo couldn't escalate non-interactively; ``loaded`` should be
        ignored). Tests can simulate either condition without touching
        launchd.
      - ``visudo_check_fn(path) -> (status, detail)`` — overrides the
        real `visudo -c -f <path>` shell-out. ``status`` is one of
        ``"ok"``, ``"syntax_error"``, ``"cannot_escalate"`` so tests can
        exercise each finding path independently. ``detail`` is the
        underlying error text (used in the Finding description).
      - ``now`` — current time, for freshness checks. Tests inject a
        fixed datetime so they're not flaky around midnight.
    """
    if network is None:
        try:
            from ..config import load_network
            network = load_network()
        except Exception as exc:
            logger.warning("infra_audit: load_network failed: %s", exc)
            network = {}
    if shared_dir is None:
        try:
            from ..config import DEFAULT_SHARED_DIR
            shared_dir = DEFAULT_SHARED_DIR
        except Exception:
            shared_dir = Path(_get_profile().shared_dir_default)
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)

    requested = set(elements) if elements else set(_ALL_ELEMENTS)
    findings: list[Finding] = []

    def _guarded(element: str, fn: Callable[[], list[Finding]]) -> None:
        """Run one element check; a crash in one degrades to a single info
        finding instead of aborting the whole audit.

        run()'s docstring has long promised "one broken check doesn't abort
        the rest", but that was only true for the outer try in run() — a raise
        here propagated through gather_diagnostics and failed the entire run
        (e.g. the EACCES on /etc/sudoers.d that `_check_sudoers` now handles
        directly). This guard makes the promise real for every element and
        keeps any FUTURE check's unexpected raise from blacking out the audit.
        """
        if element not in requested:
            return
        try:
            findings.extend(fn())
        except Exception as exc:  # noqa: BLE001 — degrade, never abort the run
            logger.exception("infra_audit: %s check failed: %s", element, exc)
            findings.append(Finding(
                element=element,
                severity="info",
                category=f"{element}_check_unavailable",
                description=(
                    f"The {element} diagnostic could not run "
                    f"({type(exc).__name__}: {exc}). The rest of the infra "
                    "audit completed; this single check is reported as "
                    "unavailable rather than failing the whole run."
                ),
                evidence={"error": f"{type(exc).__name__}: {exc}"[:200]},
            ))

    _guarded("daemons", lambda: _check_daemons(
        network=network or {}, launchctl_list_fn=launchctl_list_fn,
    ))
    _guarded("sudoers", lambda: _check_sudoers(visudo_check_fn=visudo_check_fn))
    _guarded("acls", lambda: _check_acls(
        network=network or {}, shared_dir=shared_dir,
    ))
    _guarded("network_json", lambda: _check_network_json(network or {}))
    _guarded("repo_puller", lambda: _check_repo_puller(now=now))
    _guarded("signal_retention", lambda: _check_signal_retention(
        shared_dir=shared_dir, now=now,
    ))

    return findings


_ALL_ELEMENTS: tuple[str, ...] = (
    "daemons", "sudoers", "acls",
    "network_json", "repo_puller", "signal_retention",
)


# ── Element checks ──────────────────────────────────────────────────────────


# Sudo-escalation classification (stderr substrings → "tooling-side failure,
# the underlying check never ran") moved to the Scheduler seam
# (runtime.scheduler) so the adapters can classify their own probe failures;
# re-imported under the historical name because oc_log_rotate and the
# infra-audit tests reference it here.
from ..runtime import is_sudo_escalation_error as _is_sudo_escalation_error  # noqa: E402


# Scheduler-seam probe handle (S2 migration). The audit's launchctl probes
# need `sudo -n` (fail fast in a TTY-less daemon context, so a missing
# NOPASSWD grant is classifiable from stderr instead of a hang) and the
# historical 5s per-probe timeout — hence a dedicated adapter instance
# rather than get_scheduler()'s default. raw() is used (not status()/list())
# because the tooling-failure tri-state needs stderr verbatim, and two of
# the probes are domain-level `print`s with no verb equivalent.
#
# Portability (bite 3): all three probes are launchd-only — the system-domain
# `list` (the rc-113 tri-state) plus the `print gui/<uid>/<label>` and
# `print user/<uid>` domain probes (per-user LaunchAgent / Aqua-session
# checks) have NO systemd analogue (per-user units were rejected in the Linux
# design). The whole ``daemons`` audit element therefore platform-gates OUT on
# a Linux pod (``_check_daemons`` early-returns []), so these probes are never
# reached off macOS. On macOS the handle is derived from get_launchd_scheduler
# (the FAIL-FAST launchd-typed accessor — raises loudly on a non-launchd
# adapter, impossible behind the element gate). A systemd-aware daemon-presence
# audit is the GA follow-up. Tests monkeypatch ``_probe_scheduler`` with a
# fake-runner LaunchdScheduler — set, that instance is reused verbatim.
_probe_scheduler: Optional[LaunchdScheduler] = None


def _launchctl_probe(*args: str) -> tuple[int, str, str]:
    """Run a launchctl probe via the Scheduler seam; ``(rc, stdout, stderr)``.

    argv shape is unchanged from the pre-seam code:
    ``sudo -n /bin/launchctl <args…>`` with a 5s timeout; exceptions
    surface as ``(1, "", str(e))`` (the seam runner's contract).

    A test-injected ``_probe_scheduler`` is reused as-is. Otherwise the handle
    is derived from the FAIL-FAST launchd-typed seam accessor (raises on a
    non-launchd adapter — the ``daemons`` element gates this whole path to
    macOS, so that never fires in production). When the accessor returns a
    test fake (custom runner), reuse its runner so the injected fake still
    intercepts; otherwise build the historical ``sudo -n`` 5s adapter.
    """
    global _probe_scheduler
    if _probe_scheduler is None:
        base = get_launchd_scheduler()
        if base._custom_runner:
            _probe_scheduler = LaunchdScheduler(
                sudo_non_interactive=True, runner=base._runner, timeout=5.0,
            )
        else:
            _probe_scheduler = LaunchdScheduler(
                sudo_non_interactive=True, timeout=5.0,
            )
    return _probe_scheduler.raw(*args)


def _launchctl_loaded(label: str) -> tuple[bool, str]:
    """Probe whether `launchctl list <label>` reports the daemon as loaded.

    Returns ``(loaded, status_hint)``:
      - ``(True, "")`` — daemon is loaded.
      - ``(False, "")`` — daemon is not loaded (authoritative).
      - ``(False, "cannot_escalate")`` — sudo couldn't escalate
        non-interactively; we don't know whether the daemon is loaded.
        The caller must NOT treat this as ``daemon_not_loaded``.

    The `-n` flag on sudo guarantees fast failure rather than blocking
    on a password prompt in a daemon context (the evolve user runs
    under launchd, never sees a TTY).

    For LaunchAgents we additionally `launchctl print gui/<uid>/<label>`
    against the admin user's UID — `sudo launchctl list <label>` only
    sees the calling launchd domain, which is root for the evolve
    service user and would miss user-scope agents.
    """
    cannot_escalate = False
    rc, _out, err = _launchctl_probe("list", label)
    if rc == 0:
        return True, ""
    if _is_sudo_escalation_error(err.strip()):
        cannot_escalate = True

    # Fall through for LaunchAgents: probe the admin user's gui domain.
    kind = CORE_INFRA_DAEMON_KINDS.get(label)
    if kind != "agent":
        return False, ("cannot_escalate" if cannot_escalate else "")

    sudo_user = os.environ.get("SUDO_USER")
    if not sudo_user:
        return False, ("cannot_escalate" if cannot_escalate else "")
    try:
        rid = subprocess.run(
            ["/usr/bin/id", "-u", sudo_user],
            capture_output=True, text=True, timeout=5,
        )
        if rid.returncode != 0:
            return False, ("cannot_escalate" if cannot_escalate else "")
        uid = rid.stdout.strip()
        prc, _pout, perr = _launchctl_probe("print", f"gui/{uid}/{label}")
        if prc == 0:
            return True, ""
        if _is_sudo_escalation_error(perr.strip()):
            cannot_escalate = True
        return False, ("cannot_escalate" if cannot_escalate else "")
    except (subprocess.SubprocessError, OSError):
        return False, ("cannot_escalate" if cannot_escalate else "")


def _agent_plist_path(label: str) -> Path:
    """LaunchAgent plist path — pod-admin's home, not the evolve user's.

    Read SUDO_USER (the original admin who invoked the runner) so the
    same lookup works whether the audit runs from the admin shell or
    a launchd-spawned subprocess that inherited that env. Falls back
    to HOME for non-sudo contexts (e.g. tests).
    """
    import os
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.getuid() == 0:
        home = Path(f"/Users/{sudo_user}")
    else:
        home = Path(os.environ.get("HOME") or "~").expanduser()
    return home / "Library" / "LaunchAgents" / f"{label}.plist"


def _probe_aqua_session(uid: str) -> tuple[bool, str]:
    """Does user/<uid> have an active Aqua (graphical) login session?

    A LaunchAgent in the `gui/<uid>` domain can only be bootstrapped if that
    user is logged in graphically. SSH-only login produces a `Background`
    session whose gui/<uid> domain doesn't exist; attempting to bootstrap
    against it returns error 125 "Domain does not support specified action".

    Returns ``(has_aqua, hint)``:
      - ``(True,  "")``               — gui/<uid> domain is live.
      - ``(False, "background_only")``  — user/<uid> is Background-only.
      - ``(False, "no_session")``       — user/<uid> has no session at all
                                          (user not logged in).
      - ``(False, "cannot_probe")``     — sudo couldn't escalate / probe
                                          failed; feasibility unknown.

    The probe reads `launchctl print user/<uid>` and looks for a line of the
    form ``gui asid = <number>`` — present iff the user has an Aqua session.
    """
    if not uid:
        return False, "cannot_probe"
    rc, out, err = _launchctl_probe("print", f"user/{uid}")
    if rc != 0:
        stderr = err.lower()
        if _is_sudo_escalation_error(stderr):
            return False, "cannot_probe"
        # "Could not print domain" / "No such process" → no session at all.
        if "could not print" in stderr or "no such" in stderr or "domain" in stderr:
            return False, "no_session"
        return False, "cannot_probe"
    # Look for the `gui asid` line. Background-only sessions have `session =
    # Background` but no `gui asid` — that's the discriminator.
    has_gui_asid = any(
        ln.strip().startswith("gui asid")
        for ln in out.splitlines()
    )
    if has_gui_asid:
        return True, ""
    return False, "background_only"


def _resolve_admin_uid() -> Optional[str]:
    """The UID a kind="agent" daemon would be bootstrapped against.

    Mirrors `_launchctl_loaded`'s logic: prefers SUDO_USER (when invoked via
    sudo) over the calling process's uid (when run from a LaunchDaemon).
    Returns None if neither yields a usable answer.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            r = subprocess.run(
                ["/usr/bin/id", "-u", sudo_user],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return r.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            pass
    # Fallback to the calling user. In a LaunchDaemon context this is the
    # evolve user (uid 507 on the canonical pod), which also lacks an Aqua
    # session — feasibility check will correctly return False either way.
    try:
        return str(os.getuid())
    except Exception:
        return None


def _check_daemons(
    *,
    network: dict,
    launchctl_list_fn: Optional[Callable[[str], tuple[bool, str]]] = None,
) -> list[Finding]:
    """For each canonical infra daemon, verify the plist + load state.

    Handles both LaunchDaemons (kind="system", root scope, plists in
    /Library/LaunchDaemons/) and LaunchAgents (kind="agent", user scope,
    plists in ~/Library/LaunchAgents/). The mcp-bridge runs in the
    agent scope because Claude Desktop / Claude Code consume it via the
    user's MCP config.

    If `launchctl list` can't escalate non-interactively (missing sudoers
    grant), the check emits ONE tooling-side meta-finding rather than a
    per-daemon `daemon_not_loaded` cascade — the audit's `sudo -n`
    failure tells us nothing about whether the daemons are actually
    loaded.

    Linux (bite 3): this element is launchd-specific end to end — the
    `/Library/LaunchDaemons` plist glob, `plutil -lint`, the rc-113 `list`
    probe, and the `gui/<uid>` / `user/<uid>` domain feasibility probes all
    have no systemd analogue (per-user units were rejected in the Linux
    design). On a non-macOS pod the element platform-gates OUT and returns no
    findings — better a quiet "not-applicable" than a false-clean pass or a
    launchctl-not-found crash. A systemd-aware daemon-presence audit (unit
    files + `systemctl is-active`) is the GA follow-up. A test-injected
    ``launchctl_list_fn`` does NOT bypass the gate — pin ``set_profile(MACOS)``
    to exercise the macOS findings path.
    """
    from platform_profile import get_profile

    if get_profile().name != "macos":
        return []

    findings: list[Finding] = []
    system_plist_dir = Path("/Library/LaunchDaemons")
    is_loaded = launchctl_list_fn or _launchctl_loaded
    cannot_escalate_seen = False

    for label in CORE_INFRA_DAEMONS:
        kind = CORE_INFRA_DAEMON_KINDS.get(label, "system")
        if kind == "agent":
            plist = _agent_plist_path(label)
            scope_label = "LaunchAgent"
            install_fix = "evolve-admin mcp-bridge install"
            bootstrap_fix = f"/bin/launchctl bootstrap gui/$UID {plist}"
        else:
            plist = system_plist_dir / f"{label}.plist"
            scope_label = "LaunchDaemon"
            install_fix = "sudo evolve-admin install-infra-jobs"
            bootstrap_fix = f"sudo /bin/launchctl bootstrap system {plist}"

        if not plist.exists():
            findings.append(Finding(
                element="daemons",
                severity="critical",
                category="daemon_plist_missing",
                description=(
                    f"{scope_label} plist {plist} does not exist. "
                    "The daemon cannot run and will not auto-restart at boot."
                ),
                evidence={"label": label, "path": str(plist), "kind": kind},
                suggested_fix=install_fix,
            ))
            continue

        # plutil -lint: cheap, doesn't load the file.
        try:
            r = subprocess.run(
                ["/usr/bin/plutil", "-lint", str(plist)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                findings.append(Finding(
                    element="daemons",
                    severity="critical",
                    category="daemon_plist_invalid",
                    description=(
                        f"{scope_label} plist {plist} failed plutil -lint: "
                        f"{(r.stdout or r.stderr).strip()[:200]}."
                    ),
                    evidence={"label": label, "path": str(plist), "kind": kind},
                    suggested_fix=install_fix,
                ))
                continue
        except (subprocess.SubprocessError, OSError):
            # plutil unavailable on this host — skip the lint, still
            # check load state. (Linting is belt-and-suspenders.)
            pass

        loaded, status_hint = is_loaded(label)
        if status_hint == "cannot_escalate":
            # Tooling-side failure: emit ONE meta-finding per audit run
            # rather than a per-daemon false positive. The load state is
            # unknown until the grant is wired up.
            if not cannot_escalate_seen:
                findings.append(Finding(
                    element="daemons",
                    severity="major",
                    category="daemons_audit_cannot_escalate",
                    description=(
                        "The infra audit could not run "
                        "`sudo -n /bin/launchctl list` non-interactively. "
                        "The evolve user most likely lacks a NOPASSWD grant "
                        "for /bin/launchctl. This is a tooling gap — the "
                        "audit cannot determine whether infra daemons are "
                        "loaded until the grant is restored. Daemons "
                        "themselves may be fine; verify manually with "
                        "`sudo launchctl list`."
                    ),
                    evidence={
                        "missing_grant": "/bin/launchctl list",
                        "label": label,
                    },
                    suggested_fix="sudo evolve-admin refresh-sudoers",
                ))
                cannot_escalate_seen = True
            continue
        if not loaded:
            # Before emitting `daemon_not_loaded` with a bootstrap fix, ask:
            # *can* the suggested bootstrap actually succeed on this pod?
            # For LaunchAgents on a headless pod the answer is no — the
            # admin user has no Aqua session, so `launchctl bootstrap
            # gui/<uid> …` returns error 125. Emitting the bootstrap fix
            # anyway gets the proposal dismissed and re-proposed forever.
            #
            # The feasibility probe runs only for kind="agent" (system
            # bootstraps work from any context with the existing sudoers
            # grants). If the gui domain isn't viable, switch to a
            # `daemon_load_infeasible` finding with a constructive fix —
            # either convert the service to system-scope (the right answer
            # for headless pods) or relocate the plist to a user with an
            # Aqua session.
            if kind == "agent":
                target_uid = _resolve_admin_uid()
                has_aqua, hint = _probe_aqua_session(target_uid or "")
                if not has_aqua and hint in ("background_only", "no_session"):
                    findings.append(Finding(
                        element="daemons",
                        severity="critical",
                        category="daemon_load_infeasible",
                        description=(
                            f"{scope_label} {label} is installed but cannot "
                            f"be loaded on this pod. The bootstrap target "
                            f"`gui/{target_uid}` does not exist — the user "
                            f"(uid {target_uid}) has no Aqua login session "
                            f"({hint.replace('_', ' ')}). On a headless pod "
                            f"there is no graphical session for an admin "
                            f"user, so user-scope LaunchAgents structurally "
                            f"cannot run. Convert the service to a "
                            f"system-scope LaunchDaemon (run as the evolve "
                            f"user under /Library/LaunchDaemons/) or "
                            f"relocate the plist to a user with an active "
                            f"Aqua session."
                        ),
                        evidence={
                            "label": label,
                            "path": str(plist),
                            "kind": kind,
                            "target_uid": target_uid,
                            "aqua_probe": hint,
                        },
                        # The fix here is structural (convert to LaunchDaemon),
                        # not operational (bootstrap into a domain that
                        # doesn't exist). The bootstrap command is preserved
                        # in evidence for operators who *do* have an Aqua
                        # session and just need the one-liner.
                        suggested_fix=(
                            f"Convert {label} to a system-scope LaunchDaemon "
                            f"(see mcp_service.py for the reference pattern). "
                            f"If the service is genuinely per-user, ensure "
                            f"the target user has an Aqua login session "
                            f"before running: {bootstrap_fix}"
                        ),
                        rationale="aqua_session_missing; cannot_bootstrap_gui_domain",
                    ))
                    continue
                # If hint == "cannot_probe" we couldn't tell — fall through
                # to the normal `daemon_not_loaded` finding rather than
                # silently dropping the diagnostic.
            findings.append(Finding(
                element="daemons",
                severity="critical",
                category="daemon_not_loaded",
                description=(
                    f"{scope_label} {label} is installed but not loaded. "
                    "It will not run until bootstrapped."
                ),
                evidence={"label": label, "path": str(plist), "kind": kind},
                suggested_fix=bootstrap_fix,
            ))

    return findings


def _check_sudoers(
    *,
    visudo_check_fn: Optional[Callable[[Path], tuple[str, str]]] = None,
) -> list[Finding]:
    """Check the two evolve sudoers files for existence + visudo validity +
    presence of load-bearing grants.

    The visudo check distinguishes three outcomes (see `_visudo_check`):

      - ``"ok"`` — file parses; proceed to content check.
      - ``"syntax_error"`` — visudo found a real parse problem → critical
        ``sudoers_invalid_syntax`` finding.
      - ``"cannot_escalate"`` — `sudo -n` couldn't escalate (almost
        always: missing NOPASSWD grant for visudo). The check itself
        never ran, so we MUST NOT pretend it found a syntax error. Emit
        one tooling-side meta-finding (`sudoers_audit_cannot_escalate`,
        major severity) and skip subsequent privileged checks. The 2026-05-25
        false-positive (operators saw two critical "fails visudo -c" findings
        whose stderr was actually sudo's "a terminal is required to read the
        password" — both files validated fine under interactive sudo) was
        exactly this conflation; do not reintroduce it.
    """
    findings: list[Finding] = []
    visudo = visudo_check_fn or _visudo_check
    cannot_escalate_seen = False

    for path, required_grants in (
        (SUDOERS_EVOLVE, _required_evolve_sudoers_grants()),
        (SUDOERS_EVOLVE_ADMIN, ()),  # admin sudoers grants vary per-pod
    ):
        exists = _sudoers_path_exists(path)
        if exists is False:
            findings.append(Finding(
                element="sudoers",
                severity="critical",
                category="sudoers_file_missing",
                description=(
                    f"Sudoers file {path} does not exist. The evolve "
                    "service user cannot run privileged commands; admin "
                    "operations will fail."
                ),
                evidence={"path": str(path)},
                suggested_fix=(
                    "sudo evolve-admin refresh-sudoers"
                    if path == SUDOERS_EVOLVE
                    else "Re-run the setup wizard to re-install evolve-admin sudoers."
                ),
            ))
            continue

        # exists is True, or None ("undetermined"). On Linux /etc/sudoers.d is
        # mode 0750 root and the evolve user can't traverse it, so Path.exists()
        # raises EACCES under Py>=3.12 (<=3.11 silently returned False — a false
        # "missing" critical). We MUST NOT crash the whole audit on that (it
        # previously propagated out of gather_diagnostics → run() set
        # result.error = "diagnostics failed: [Errno 13] ..." and the operator
        # got NO infra audit at all). The visudo + cat checks below run as root
        # via `sudo -n`, so they don't depend on the evolve user's own traverse
        # permission — proceed and let them be the oracle. (A genuinely-missing
        # file on Linux also lands here as undetermined, but visudo -c then
        # fails to open it and emits a critical finding below, so it is not
        # silently swallowed.)
        undetermined = exists is None

        status, detail = visudo(path)
        if status == "cannot_escalate":
            # Tooling-side failure: emit ONE meta-finding per audit run.
            # Don't `continue` — the content check below uses /bin/cat
            # (separately granted) and still runs, so the operator gets
            # both the tooling diagnosis AND any substantive grant-drift
            # findings in the same audit.
            if not cannot_escalate_seen:
                findings.append(Finding(
                    element="sudoers",
                    severity="major",
                    category="sudoers_audit_cannot_escalate",
                    description=(
                        "The infra audit could not run "
                        "`sudo -n /usr/sbin/visudo -c -f` non-interactively: "
                        f"{detail[:200]}. The evolve user most likely lacks "
                        "a NOPASSWD grant for /usr/sbin/visudo. This is a "
                        "tooling gap, not a sudoers syntax problem — the "
                        "file may be perfectly valid. Refresh sudoers to "
                        "add the grant, then re-run the audit."
                    ),
                    evidence={
                        "path": str(path),
                        "stderr": detail[:200],
                        "missing_grant": (
                            "/usr/sbin/visudo -c -f /etc/sudoers.d/*"
                        ),
                    },
                    suggested_fix="sudo evolve-admin refresh-sudoers",
                ))
                cannot_escalate_seen = True
        elif status == "syntax_error":
            findings.append(Finding(
                element="sudoers",
                severity="critical",
                category="sudoers_invalid_syntax",
                description=(
                    f"Sudoers file {path} fails visudo -c: {detail[:200]}. "
                    "Until fixed, all sudo grants in the file are inactive."
                ),
                evidence={"path": str(path), "error": detail[:200]},
                suggested_fix=(
                    "sudo evolve-admin refresh-sudoers"
                    if path == SUDOERS_EVOLVE
                    else "Edit the file via `sudo visudo -f` and fix the syntax error."
                ),
            ))
            continue

        # Required-grant content check. Read with sudo since /etc/sudoers.d/
        # is mode 440 root:wheel.
        contents = _read_sudoers_contents(path)
        if contents is None:
            if undetermined:
                # We couldn't stat the file locally (EACCES traversing
                # /etc/sudoers.d) AND the privileged read returned nothing.
                # The file's grant content is genuinely UNDETERMINED — not
                # known-bad. Record it as such (info, not minor/critical) so
                # the audit COMPLETES with an honest "couldn't verify" rather
                # than crashing or false-alarming. The §23 cat grant exists to
                # make the read succeed; if it's still failing the operator
                # should refresh sudoers.
                findings.append(Finding(
                    element="sudoers",
                    severity="info",
                    category="sudoers_content_undetermined",
                    description=(
                        f"The infra audit could not determine the grant "
                        f"contents of {path}: the evolve user cannot traverse "
                        "/etc/sudoers.d (mode 0750 root on Linux) and the "
                        "privileged read returned nothing. Required-grant "
                        "verification was skipped for this file — this is a "
                        "tooling limitation, not a sudoers problem."
                    ),
                    evidence={"path": str(path)},
                    suggested_fix="sudo evolve-admin refresh-sudoers",
                ))
                continue
            findings.append(Finding(
                element="sudoers",
                severity="minor",
                category="sudoers_unreadable",
                description=(
                    f"Sudoers file {path} exists and parses, but the audit "
                    "couldn't read its contents to verify required grants."
                ),
                evidence={"path": str(path)},
            ))
            continue

        missing = [g for g in required_grants if g not in contents]
        if missing:
            findings.append(Finding(
                element="sudoers",
                severity="critical",
                category="sudoers_required_grant_missing",
                description=(
                    f"Sudoers file {path} is missing required grants: "
                    f"{missing[:3]}. Without these, the admin server cannot "
                    "perform its core operations."
                ),
                evidence={"path": str(path), "missing": missing},
                suggested_fix="sudo evolve-admin refresh-sudoers",
            ))

    return findings


def _visudo_check(path: Path) -> tuple[str, str]:
    """Run `sudo -n /usr/sbin/visudo -c -f <path>` non-interactively.

    Returns ``(status, detail)`` where status is one of:

      - ``"ok"``: visudo parsed the file with no errors.
      - ``"syntax_error"``: visudo reported a real syntax problem.
      - ``"cannot_escalate"``: sudo could not get root non-interactively.
        Almost always means the evolve user has no NOPASSWD grant for
        visudo. The visudo check itself never ran, so callers MUST NOT
        treat this as a sudoers-syntax finding.

    The `-n` flag is load-bearing: without it sudo blocks waiting for a
    TTY password prompt (the admin daemon never has one), and its
    "a terminal is required to read the password" stderr gets misread
    as a visudo parse error. See feedback_diagnosis_must_survive_live_inspection.
    """
    try:
        r = subprocess.run(
            ["sudo", "-n", "/usr/sbin/visudo", "-c", "-f", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return "ok", ""
        detail = (r.stderr or r.stdout).strip()
        if _is_sudo_escalation_error(detail):
            return "cannot_escalate", detail
        return "syntax_error", detail
    except (subprocess.SubprocessError, OSError) as exc:
        return "cannot_escalate", f"visudo invocation failed: {exc}"


def _sudoers_path_exists(path: Path) -> Optional[bool]:
    """Return True/False, or None when the answer is UNDETERMINED.

    ``Path.exists()`` stats the file as the *current* (evolve) user. On Linux
    ``/etc/sudoers.d`` is mode 0750 root, so the evolve user can't traverse it
    and the stat raises ``PermissionError`` (EACCES) under Python >= 3.12
    — versions <= 3.11 swallowed EACCES and returned False, which is itself a
    silent false "missing". Either way the bare call is unsafe: an unhandled
    EACCES propagates out of ``gather_diagnostics`` and aborts the ENTIRE infra
    audit (run() reports ``diagnostics failed: [Errno 13] Permission denied:
    '/etc/sudoers.d/evolve'`` and the operator gets no audit). Treat EACCES as
    "undetermined" (None) so callers neither crash nor false-alarm — the
    privileged ``sudo -n`` visudo/cat checks can still establish the truth.
    """
    try:
        return path.exists()
    except PermissionError:
        return None
    except OSError:
        return None


def _read_sudoers_contents(path: Path) -> Optional[str]:
    """Read /etc/sudoers.d/<f> via `sudo -n <cat>`. Returns None on failure.

    `-n` is load-bearing: without it sudo blocks on a password prompt in
    daemon context. A failure here (including cannot-escalate) maps to
    a minor `sudoers_unreadable` (or info `sudoers_content_undetermined`)
    finding upstream — the visudo check handles the tooling-side escalation
    diagnosis at higher severity.

    The `cat` path comes from the platform profile (the SAME source
    _render_evolve_sudoers renders the §23 grant from), so the invoked argv
    and the granted argv can't drift: a grant for ``/bin/cat`` and a call to
    ``/usr/bin/cat`` (or vice-versa) would silently fail to match and sudo
    would demand a password. Single source = guaranteed match.
    """
    try:
        r = subprocess.run(
            ["sudo", "-n", _get_profile().cat, str(path)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        return r.stdout
    except (subprocess.SubprocessError, OSError):
        return None


def _withhold_bot_openclaw_unreadable(shared_dir: Path) -> bool:
    """Settle-gate check for the transient ``bot_openclaw_unreadable`` finding.

    Treated as non-critical/transient: withheld until the pod settles. The
    settle_gate lives in the analyzer ``signals`` package (importable here, as
    audit_poller already imports ``signals.store``). Fails OPEN (does not
    withhold) if the gate can't be imported."""
    try:
        from signals.settle_gate import should_withhold
    except ImportError:
        return False
    return should_withhold(shared_dir, severity="warn", transient=True)


def _check_acls(
    *,
    network: dict,
    shared_dir: Path,
) -> list[Finding]:
    """Spot-check the ACL state of paths the evolve user must read/write.

    We don't try to parse the full ACL output — that's surface area for
    a string-matching war. Instead, we verify the evolve user can
    *actually* read/write a representative file under each path. If
    the read fails with PermissionError, the ACL is missing or broken.
    """
    findings: list[Finding] = []

    # shared_dir itself must be readable + writable by evolve. We're
    # running AS evolve (admin daemon), so a write probe is conclusive.
    if not shared_dir.exists():
        findings.append(Finding(
            element="acls",
            severity="critical",
            category="shared_dir_missing",
            description=(
                f"Shared directory {shared_dir} does not exist. Most pod "
                "state (signals, proposals, manifests) cannot be persisted."
            ),
            evidence={"path": str(shared_dir)},
            suggested_fix="sudo evolve-admin install-infra-jobs",
        ))
    else:
        if not os.access(str(shared_dir), os.W_OK):
            findings.append(Finding(
                element="acls",
                severity="critical",
                category="shared_dir_not_writable",
                description=(
                    f"Shared directory {shared_dir} is not writable by the "
                    "evolve service user. Pod-wide writes (Signals, Proposals, "
                    "infra audit outbox) will fail."
                ),
                evidence={"path": str(shared_dir)},
                suggested_fix=(
                    f"sudo /usr/sbin/chown -R evolve:staff {shared_dir}"
                ),
            ))

    # Per-bot .openclaw read access. evolve gets ACL read via
    # set_evolve_read_acl in deploy.py. A bot whose ACL hasn't been
    # applied is a deploy-time miss.
    bots = (network or {}).get("bots") or {}
    for bot_id, cfg in bots.items():
        if not isinstance(cfg, dict):
            continue
        bot_user = cfg.get("user") or bot_id
        oc_dir = Path(f"/Users/{bot_user}/.openclaw")
        if not oc_dir.exists():
            # Bot account may not have been set up yet — this is for the
            # deploy story, not the audit. Skip rather than spam.
            continue
        if not os.access(str(oc_dir), os.R_OK | os.X_OK):
            # Fresh-pod bring-up settle gate: the deploy's read-ACL pass lands
            # within minutes; on a not-yet-settled pod this is self-healing
            # bring-up state, not a real miss. Withhold (non-critical/transient)
            # until settled. See internal/spec-pod-bringup-settle-2026-06-23.md.
            if _withhold_bot_openclaw_unreadable(shared_dir):
                continue
            findings.append(Finding(
                element="acls",
                severity="major",
                category="bot_openclaw_unreadable",
                description=(
                    f"evolve cannot read {oc_dir} — ACL grant is missing. "
                    "Per-bot audit reads, manifest sync, and openclaw.json "
                    "lookups will fall back to sudo cat (slower, may fail)."
                ),
                evidence={"path": str(oc_dir), "bot_id": bot_id},
                suggested_fix=f"sudo evolve-admin deploy --bot {bot_id}",
            ))

    return findings


def _check_network_json(network: dict) -> list[Finding]:
    """Validate the shape of network.json at the schema level.

    The on-disk file is loaded by load_network() before we get it as a
    dict, so JSON-parse errors aren't visible here — they'd fail load
    upstream and the runner won't fire at all. What we CAN check:

      - Required top-level keys are present.
      - bots dict has the per-bot fields we depend on (user, port).
      - integration references on a bot exist (rough orphan check).
    """
    findings: list[Finding] = []

    for required_key in ("bots", "sharedDir", "pod"):
        if required_key not in network:
            findings.append(Finding(
                element="network_json",
                severity="critical",
                category="network_json_missing_key",
                description=(
                    f"network.json is missing required top-level key "
                    f"`{required_key}`. The admin server may behave "
                    "unpredictably; downstream code assumes this key."
                ),
                evidence={"key": required_key},
                suggested_fix=(
                    "Edit /Users/Shared/evolve/network.json (with sudo) to "
                    "add the missing key, or restore from a recent backup."
                ),
            ))

    bots = network.get("bots") or {}
    if not isinstance(bots, dict):
        findings.append(Finding(
            element="network_json",
            severity="critical",
            category="network_json_bots_not_dict",
            description=(
                "network.json `bots` field is not a dict. The pod's bot "
                "registry is unreadable; nothing will deploy."
            ),
            evidence={"actual_type": type(bots).__name__},
        ))
        return findings   # nothing else we can sensibly check.

    for bot_id, cfg in bots.items():
        if not isinstance(cfg, dict):
            findings.append(Finding(
                element="network_json",
                severity="major",
                category="network_json_bot_entry_not_dict",
                description=(
                    f"network.json bot `{bot_id}` is not a dict. Skipping "
                    "per-bot field checks for this entry."
                ),
                evidence={"bot_id": bot_id},
            ))
            continue
        # Strictly speaking `user` is optional (defaults to bot_id), but a
        # bot entry with no port and no user is almost certainly a typo.
        if not cfg.get("user") and not cfg.get("port"):
            findings.append(Finding(
                element="network_json",
                severity="minor",
                category="network_json_bot_minimal_config",
                description=(
                    f"network.json bot `{bot_id}` has neither `user` nor "
                    "`port` set. Confirm the entry is intentional (not a "
                    "typo / orphan from a removed bot)."
                ),
                evidence={"bot_id": bot_id, "key": bot_id},
            ))

    return findings


def _check_repo_puller(*, now: _dt.datetime) -> list[Finding]:
    """Repo-puller status: log exists, was updated in the last 30 min,
    no quarantine files >24h old."""
    findings: list[Finding] = []

    if not REPO_PULLER_LOG.exists():
        findings.append(Finding(
            element="repo_puller",
            severity="major",
            category="repo_puller_log_missing",
            description=(
                f"Repo-puller log {REPO_PULLER_LOG} does not exist. "
                "Either the puller daemon never ran or its log path "
                "changed. The deploy checkout may drift from origin/main."
            ),
            evidence={"path": str(REPO_PULLER_LOG)},
            suggested_fix=(
                "sudo /bin/launchctl kickstart -k "
                "system/ai.evolve.evolve.repo-puller"
            ),
        ))
        return findings

    try:
        mtime = REPO_PULLER_LOG.stat().st_mtime
    except OSError as exc:
        findings.append(Finding(
            element="repo_puller",
            severity="minor",
            category="repo_puller_log_unreadable",
            description=(
                f"Could not stat repo-puller log {REPO_PULLER_LOG}: {exc}."
            ),
            evidence={"path": str(REPO_PULLER_LOG)},
        ))
        return findings

    age_s = now.timestamp() - mtime
    if age_s > REPO_PULLER_STALE_AFTER_S:
        findings.append(Finding(
            element="repo_puller",
            severity="major",
            category="repo_puller_stale",
            description=(
                f"Repo-puller log hasn't been touched in "
                f"{int(age_s / 60)}min (expected: every 15min). The "
                "daemon may be unloaded or wedged."
            ),
            evidence={
                "path": str(REPO_PULLER_LOG),
                "age_seconds": int(age_s),
                "stale_after_seconds": REPO_PULLER_STALE_AFTER_S,
            },
            suggested_fix=(
                "sudo /bin/launchctl kickstart -k "
                "system/ai.evolve.evolve.repo-puller"
            ),
        ))

    # Quarantine directory: repo-puller moves identical-to-origin
    # files here when a pull would otherwise wedge. Anything older
    # than 24h is operator-action-required.
    quarantine = Path("/Users/Shared/evolve/quarantine")
    if quarantine.exists():
        cutoff = now.timestamp() - 24 * 3600
        stale = []
        try:
            for entry in quarantine.rglob("*"):
                if entry.is_file():
                    try:
                        if entry.stat().st_mtime < cutoff:
                            stale.append(str(entry))
                    except OSError:
                        continue
        except OSError:
            stale = []
        if stale:
            findings.append(Finding(
                element="repo_puller",
                severity="minor",
                category="repo_puller_quarantine_stale",
                description=(
                    f"Repo-puller quarantine contains {len(stale)} file(s) "
                    "older than 24h. These are typically leftover from a "
                    "merge conflict and should be reviewed."
                ),
                evidence={
                    "path": str(quarantine),
                    "count": len(stale),
                    "examples": stale[:3],
                },
                suggested_fix=(
                    f"Review and remove old files under {quarantine}."
                ),
            ))

    return findings


def _check_signal_retention(
    *,
    shared_dir: Path,
    now: _dt.datetime,
) -> list[Finding]:
    """Signal-store retention: today's log was written within 24h."""
    findings: list[Finding] = []
    log_dir = Path(shared_dir) / "signals" / "log"
    if not log_dir.exists():
        # The signal store is created on first observe(); a brand-new
        # pod won't have it yet. Surface as minor so the operator
        # notices but isn't alarmed.
        findings.append(Finding(
            element="signal_retention",
            severity="minor",
            category="signal_log_dir_missing",
            description=(
                f"Signal log directory {log_dir} does not exist. Either "
                "the signal store hasn't been written to yet (fresh pod) "
                "or retention has wiped everything."
            ),
            evidence={"path": str(log_dir)},
        ))
        return findings

    # Find the most-recent log file. If its mtime is older than 24h,
    # retention hasn't run.
    try:
        candidates = [p for p in log_dir.iterdir() if p.suffix == ".jsonl"]
    except OSError as exc:
        findings.append(Finding(
            element="signal_retention",
            severity="minor",
            category="signal_log_unreadable",
            description=f"Could not list {log_dir}: {exc}.",
            evidence={"path": str(log_dir)},
        ))
        return findings

    if not candidates:
        findings.append(Finding(
            element="signal_retention",
            severity="minor",
            category="signal_log_empty",
            description=(
                f"Signal log directory {log_dir} has no jsonl files. "
                "Retention may have wiped them; sweeps haven't fired."
            ),
            evidence={"path": str(log_dir)},
        ))
        return findings

    try:
        latest_mtime = max(p.stat().st_mtime for p in candidates)
    except OSError:
        return findings   # transient stat failure; not worth a finding.

    age_s = now.timestamp() - latest_mtime
    if age_s > SIGNAL_RETENTION_STALE_AFTER_S:
        findings.append(Finding(
            element="signal_retention",
            severity="major",
            category="signal_retention_stale",
            description=(
                f"No signal-log writes in {int(age_s / 3600)}h. The "
                "retention daemon ai.evolve.evolve.retention has likely "
                "stopped running."
            ),
            evidence={
                "path": str(log_dir),
                "age_seconds": int(age_s),
                "stale_after_seconds": SIGNAL_RETENTION_STALE_AFTER_S,
            },
            suggested_fix=(
                "sudo /bin/launchctl kickstart -k "
                "system/ai.evolve.evolve.retention"
            ),
        ))

    return findings


# ── LLM dispatch (two-stage) ────────────────────────────────────────────────


# Discovery prompt — given the diagnostics dump, the model returns the same
# set of findings it observes (effectively a sanity-check pass). This is a
# placeholder for the spec's "Discovery + Triage" pattern when no LLM is
# available; the runner's `llm_dispatch` argument can override.
_DISCOVERY_SYSTEM = """You are auditing the infrastructure of an Evolve pod.

Below is a list of observed divergences from the pod's expected state.
For each, decide whether the observation is genuinely worth surfacing to
the operator or whether it's likely benign (e.g. a brand-new pod where a
file isn't created yet).

Output a JSON array, one object per kept observation. Drop observations
you believe are noise. The fields are:

  {
    "element": "<element name>",
    "category": "<category slug>",
    "severity": "critical|major|minor|info",
    "description": "<short operator-facing explanation>",
    "rationale": "<why you kept this>",
    "evidence_keys": ["<key1>", "<key2>"]
  }
"""

_TRIAGE_SYSTEM = """You are triaging infrastructure audit observations for
an Evolve pod operator. For each observation in the input, output a
decision:

  - "dismiss": false positive or benign drift; do not surface.
  - "propose": create an operator Proposal so the operator can fix or
    acknowledge it.
  - "auto_fix": the system could safely fix this. (NOTE: in calibration
    mode — which is the default in v1 — auto_fix is demoted to propose
    at the orchestration layer. You can still pick auto_fix; the runner
    will treat it as propose.)

Output a JSON array of decisions, one per input observation, in the same
order. Each object has:

  {
    "element": "<from input>",
    "category": "<from input>",
    "outcome": "dismiss|propose|auto_fix",
    "rationale": "<one sentence>"
  }
"""


@dataclass
class TriageDecision:
    element: str
    category: str
    outcome: str       # dismiss | propose | auto_fix
    rationale: str = ""


def _heuristic_triage(findings: list[Finding]) -> list[TriageDecision]:
    """Fallback triage when no LLM is available.

    Spec §4.3 requires the two-stage LLM pattern, but the runner must
    still produce useful output if openclaw isn't available or LLM
    dispatch fails. The heuristic surfaces every finding above `minor`
    as a Proposal and dismisses pure info / minor entries that don't
    have a suggested_fix. This is conservative — we'd rather over-
    surface and dedup via signature than miss a real critical.
    """
    out: list[TriageDecision] = []
    for f in findings:
        sev = (f.severity or "").lower()
        if sev in ("critical", "major"):
            outcome = "propose"
            rationale = f"severity={sev}; auto-surface per heuristic triage"
        elif sev == "minor" and f.suggested_fix:
            outcome = "propose"
            rationale = "minor with fix path; operator should see it"
        else:
            outcome = "dismiss"
            rationale = "minor/info without actionable fix"
        out.append(TriageDecision(
            element=f.element, category=f.category,
            outcome=outcome, rationale=rationale,
        ))
    return out


# ── Outbox + trail writes ───────────────────────────────────────────────────


# Soft-cap the per-element trail.jsonl the same way the app-audit runner caps
# its per-app trail (footprint cut, 2026-06-28; internal/footprint-antipattern-
# cross-app-audit-2026-06-28.md). Infra audit re-appends a line per finding +
# one ``audit_run`` roll-up per element EVERY run with no prune; without a cap
# the trail grows unbounded. The per-run roll-up means _append_trail runs at
# least once per touched element each run, so the cap is reached even for an
# element whose findings have gone quiet. Every reader is bounded-tail (the UI
# trail tail, the evo handler) so dropping the head is safe.
_TRAIL_CAP_LINES = 1000
_TRAIL_CAP_KEEP = 500


def _cap_trail(path: Path) -> None:
    """Soft-cap a trail.jsonl to its most-recent lines. Best-effort; never raises."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    if len(lines) > _TRAIL_CAP_LINES:
        try:
            path.write_text("\n".join(lines[-_TRAIL_CAP_KEEP:]) + "\n")
        except OSError as exc:
            # Best-effort cap: a failed rewrite just leaves the trail oversized
            # until the next append retries — never fail the audit run.
            logger.warning("infra_audit: trail cap rewrite failed for %s: %s",
                           path, exc)


def _append_trail(path: Path, entry: dict) -> bool:
    """Append one JSON line to the per-element trail file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        _cap_trail(path)
        return True
    except OSError as exc:
        logger.warning("infra_audit: trail append failed for %s: %s", path, exc)
        return False


def _write_outbox_record(shared_dir: Path, record: dict) -> Path | None:
    record_id = record.get("record_id") or f"infra-rec-{uuid.uuid4().hex[:10]}"
    record["record_id"] = record_id
    out = infra_audit_outbox_dir(shared_dir) / f"{record_id}.json"
    # shared_dir is evolve-owned, so a direct atomic write works. A failed
    # write is non-fatal: log + return None (callers treat the record as
    # not-written), matching the pre-consolidation bool-returning helper.
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(out, record)
    except OSError as exc:
        logger.warning("infra_audit: atomic write failed for %s: %s", out, exc)
        return None
    return out


# ── Top-level runner ────────────────────────────────────────────────────────


def run(
    *,
    network: Optional[dict] = None,
    shared_dir: Optional[Path] = None,
    elements: Optional[list[str]] = None,
    requested_by: str = "scheduler",
    calibration_mode: bool = True,
    llm_dispatch: Optional[Callable[[list[Finding]], list[TriageDecision]]] = None,
    diagnostics_fn: Optional[Callable[..., list[Finding]]] = None,
) -> AuditRunResult:
    """End-to-end infra audit run.

    1. Gather diagnostics (pure Python).
    2. Triage (LLM if `llm_dispatch` supplied, else heuristic).
    3. Apply calibration: auto_fix → propose when calibration_mode=True.
    4. Write per-finding outbox records + per-element trail lines.
    5. Write a run-summary outbox record (idempotent key for the poller).

    The runner is admin-side (evolve user). Shared-dir writes are
    direct (no /tmp staging + sudo dance — shared_dir is evolve-owned).

    Returns AuditRunResult capturing what happened. Exceptions during
    individual element checks are caught and recorded; one broken
    check doesn't abort the rest.
    """
    if shared_dir is None:
        try:
            from ..config import DEFAULT_SHARED_DIR
            shared_dir = DEFAULT_SHARED_DIR
        except Exception:
            shared_dir = Path(_get_profile().shared_dir_default)
    if network is None:
        try:
            from ..config import load_network
            network = load_network()
        except Exception:
            network = {}

    run_id = f"infra-run-{uuid.uuid4().hex[:10]}"
    started_at = _iso_now()
    result = AuditRunResult(
        run_id=run_id,
        started_at=started_at,
        completed_at=started_at,
    )

    # ── 1. Diagnostics ──────────────────────────────────────────────────────
    gather = diagnostics_fn or gather_diagnostics
    try:
        findings = gather(
            network=network, shared_dir=shared_dir, elements=elements,
        )
    except Exception as exc:
        logger.exception("infra_audit: gather_diagnostics failed: %s", exc)
        result.error = f"diagnostics failed: {exc}"
        result.completed_at = _iso_now()
        # Surface the run failure so the operator sees the audit broke.
        _write_outbox_record(shared_dir, {
            "record_id": f"infra-fail-{uuid.uuid4().hex[:10]}",
            "audit_run_id": run_id,
            "kind": "infra_run_failed",
            "ts": result.completed_at,
            "runner_version": INFRA_AUDIT_VERSION,
            "producer": "infra_audit",
            "error": result.error,
            "requested_by": requested_by,
        })
        return result

    result.findings = findings
    result.elements_checked = list(elements or _ALL_ELEMENTS)

    # ── 2. Triage ───────────────────────────────────────────────────────────
    if findings:
        triage = llm_dispatch or _heuristic_triage
        try:
            decisions = triage(findings)
            if llm_dispatch is not None:
                result.llm_used = True
        except Exception as exc:
            logger.warning(
                "infra_audit: LLM triage failed (%s); falling back to "
                "heuristic", exc,
            )
            decisions = _heuristic_triage(findings)

        # Align decisions to findings by element+category. Heuristic
        # already returns them in order; LLMs may not.
        dec_by_key = {(d.element, d.category): d for d in decisions}

        # ── 3. Calibration + outbox + trail ────────────────────────────────
        for f in findings:
            dec = dec_by_key.get((f.element, f.category))
            if dec is None:
                # Triage missed this one — safe default = propose.
                dec = TriageDecision(
                    element=f.element, category=f.category,
                    outcome="propose",
                    rationale="missing triage decision; defaulted to propose",
                )
            outcome = dec.outcome
            if outcome == "auto_fix" and calibration_mode:
                outcome = "propose"
                dec.rationale += " [calibration_mode=true → demoted to propose]"
            result.outcomes[outcome] = result.outcomes.get(outcome, 0) + 1

            trail_entry = {
                "ts": _iso_now(),
                "kind": f"infra_{outcome}",
                "audit_run_id": run_id,
                "element": f.element,
                "category": f.category,
                "severity": f.severity,
                "signature": f.signature(),
                "rationale": dec.rationale,
                "description": f.description,
            }
            _append_trail(element_trail_path(shared_dir, f.element), trail_entry)

            if outcome == "propose":
                merged_rationale = dec.rationale
                if f.rationale:
                    # Check-level rationale (why this category) prepended;
                    # triage rationale (why surface) appended in brackets.
                    merged_rationale = f"{f.rationale} [triage: {dec.rationale}]"
                _write_outbox_record(shared_dir, {
                    "record_id": f"infra-rec-{uuid.uuid4().hex[:10]}",
                    "audit_run_id": run_id,
                    "kind": "infra_finding",
                    "ts": _iso_now(),
                    "runner_version": INFRA_AUDIT_VERSION,
                    "producer": "infra_audit",
                    "element": f.element,
                    "category": f.category,
                    "severity": f.severity,
                    "signature": f.signature(),
                    "outcome": outcome,
                    "description": f.description,
                    "evidence": f.evidence,
                    "suggested_fix": f.suggested_fix,
                    "rationale": merged_rationale,
                    "requested_by": requested_by,
                })
            elif outcome == "auto_fix":
                # Calibration is OFF — emit a finding anyway so the poller
                # can route it (v1 keeps auto_fix as a Proposal because we
                # have no in-place remediation; spec §4.6).
                _write_outbox_record(shared_dir, {
                    "record_id": f"infra-rec-{uuid.uuid4().hex[:10]}",
                    "audit_run_id": run_id,
                    "kind": "infra_finding",
                    "ts": _iso_now(),
                    "runner_version": INFRA_AUDIT_VERSION,
                    "producer": "infra_audit",
                    "element": f.element,
                    "category": f.category,
                    "severity": f.severity,
                    "signature": f.signature(),
                    "outcome": "propose",   # forced to propose in v1
                    "description": f.description,
                    "evidence": f.evidence,
                    "suggested_fix": f.suggested_fix,
                    "rationale": (
                        dec.rationale
                        + " [v1: auto_fix not implemented; forced to propose]"
                    ),
                    "requested_by": requested_by,
                })

    # ── 4. Run summary ──────────────────────────────────────────────────────
    result.completed_at = _iso_now()
    _write_outbox_record(shared_dir, {
        "record_id": f"infra-sum-{uuid.uuid4().hex[:10]}",
        "audit_run_id": run_id,
        "kind": "infra_run_summary",
        "ts": result.completed_at,
        "runner_version": INFRA_AUDIT_VERSION,
        "producer": "infra_audit",
        "started_at": result.started_at,
        "completed_at": result.completed_at,
        "elements_checked": result.elements_checked,
        "elements_skipped": result.elements_skipped,
        "findings_count": len(result.findings),
        "outcomes": result.outcomes,
        "kept_signatures": [f.signature() for f in result.findings],
        "llm_used": result.llm_used,
        "tokens_used": result.tokens_used,
        "requested_by": requested_by,
    })

    # Also append a roll-up trail entry under each touched element so the
    # operator can read a coherent history per element.
    for elem in set(f.element for f in result.findings) | set(result.elements_checked):
        per_elem_findings = [f for f in result.findings if f.element == elem]
        _append_trail(element_trail_path(shared_dir, elem), {
            "ts": result.completed_at,
            "kind": "audit_run",
            "audit_run_id": run_id,
            "element": elem,
            "status": result.status(),
            "findings_count": len(per_elem_findings),
        })

    return result


# ── On-demand dispatch helper (used by evo handler + UI) ─────────────────────


def request_infra_audit(
    *,
    requested_by: str = "operator",
    elements: Optional[list[str]] = None,
    shared_dir: Optional[Path] = None,
    network: Optional[dict] = None,
    background: bool = True,
) -> dict[str, Any]:
    """Queue an on-demand infra audit run.

    For v1 we run synchronously when `background=False` (used by tests
    and the cron path) and fire-and-forget via a thread when
    `background=True` (used by `evo audit infra` and the UI button).
    A small inbox file is also written under
    `{shared_dir}/infra_audit_inbox/` for forensics.

    Returns a dict with `request_id`, `ok`, `started`, `error` so the
    caller can render an immediate confirmation.
    """
    if shared_dir is None:
        try:
            from ..config import DEFAULT_SHARED_DIR
            shared_dir = DEFAULT_SHARED_DIR
        except Exception:
            shared_dir = Path(_get_profile().shared_dir_default)
    if network is None:
        try:
            from ..config import load_network
            network = load_network()
        except Exception:
            network = {}

    request_id = f"infra-req-{uuid.uuid4().hex[:10]}"
    inbox_dir = Path(shared_dir) / "infra_audit_inbox"
    try:
        inbox_dir.mkdir(parents=True, exist_ok=True)
        (inbox_dir / f"{request_id}.json").write_text(json.dumps({
            "request_id": request_id,
            "kind": "infra_audit",
            "elements": elements or list(_ALL_ELEMENTS),
            "requested_by": requested_by,
            "requested_at": _iso_now(),
        }, indent=2))
    except OSError as exc:
        return {
            "ok": False, "request_id": request_id,
            "error": f"inbox write failed: {exc}",
        }

    if not background:
        try:
            res = run(
                network=network, shared_dir=shared_dir,
                elements=elements, requested_by=requested_by,
            )
            return {
                "ok": True, "request_id": request_id,
                "started": True, "synchronous": True,
                "run_id": res.run_id,
                "findings": len(res.findings),
                "outcomes": res.outcomes,
            }
        except Exception as exc:
            return {
                "ok": False, "request_id": request_id,
                "error": f"run failed: {exc}",
            }

    # Background path. Use a daemon thread so the caller returns immediately;
    # the poller will pick up the outbox record next tick.
    import threading
    def _go():
        try:
            run(
                network=network, shared_dir=shared_dir,
                elements=elements, requested_by=requested_by,
            )
        except Exception as exc:
            logger.exception("infra_audit: background run failed: %s", exc)
    t = threading.Thread(target=_go, name=f"infra-audit-{request_id}",
                         daemon=True)
    t.start()
    return {
        "ok": True, "request_id": request_id,
        "started": True, "synchronous": False,
    }


# ── Read helpers for UI ─────────────────────────────────────────────────────


def latest_run_summary(shared_dir: Optional[Path] = None) -> Optional[dict]:
    """Return the most-recent infra_run_summary record (live or archived).

    Used by the Pod Health UI to render "last audit: 2026-05-17 · 2
    findings". Walks the outbox + _ingested dirs and returns the
    highest-ts record.
    """
    if shared_dir is None:
        try:
            from ..config import DEFAULT_SHARED_DIR
            shared_dir = DEFAULT_SHARED_DIR
        except Exception:
            shared_dir = Path(_get_profile().shared_dir_default)
    candidates: list[Path] = []
    out = infra_audit_outbox_dir(shared_dir)
    if out.exists():
        candidates.extend(p for p in out.iterdir() if p.is_file() and p.suffix == ".json")
    ingested = infra_audit_outbox_ingested(shared_dir)
    if ingested.exists():
        candidates.extend(ingested.rglob("*.json"))
    latest: Optional[dict] = None
    latest_ts = ""
    for p in candidates:
        try:
            r = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if r.get("kind") != "infra_run_summary":
            continue
        if (r.get("completed_at") or r.get("ts") or "") > latest_ts:
            latest = r
            latest_ts = r.get("completed_at") or r.get("ts") or ""
    return latest


def read_element_trail(
    element: str, shared_dir: Optional[Path] = None,
    *, limit: int = 20,
) -> list[dict]:
    """Tail an element's trail.jsonl file."""
    if shared_dir is None:
        try:
            from ..config import DEFAULT_SHARED_DIR
            shared_dir = DEFAULT_SHARED_DIR
        except Exception:
            shared_dir = Path(_get_profile().shared_dir_default)
    path = element_trail_path(shared_dir, element)
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
