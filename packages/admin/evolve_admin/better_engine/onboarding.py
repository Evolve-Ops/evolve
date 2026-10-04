"""
better_engine/onboarding.py — Onboarding task system (§9).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from .. import external_ids as _external_ids


# ── OnboardingTask dataclass ───────────────────────────────────────────────────

@dataclass
class OnboardingTask:
    id: str
    title: str
    detail: str
    context: str
    depends_on: list[str] = field(default_factory=list)
    action: str = ""
    action_args: dict = field(default_factory=dict)
    per_bot: bool = False
    tags: list[str] = field(default_factory=list)
    check: Callable[[dict], bool] = field(default=lambda state: False)

    def to_dict(self) -> dict:
        """Serialize the task metadata for the admin UI.

        Omits ``check`` (not JSON-serializable) and ``per_bot`` (a
        template-only flag — once an instantiation is on the wire it's
        always concrete). The UI uses ``action`` + ``action_args`` to
        wire the row's Go button.
        """
        return {
            "id": self.id,
            "title": self.title,
            "detail": self.detail,
            "context": self.context,
            "depends_on": list(self.depends_on),
            "action": self.action,
            "action_args": dict(self.action_args),
            "tags": list(self.tags),
        }


# ── State bundle ───────────────────────────────────────────────────────────────

def _list_manifests(shared_dir: Path, bot_id: str) -> list:
    """Return ApplicationManifest objects for a bot from the canonical shared-dir store."""
    try:
        from ..applications.manifest import list_manifests as _list
        return _list(shared_dir, bot_id)
    except Exception:
        return []


def build_onboarding_state(shared_dir: Path, network: dict) -> dict:
    """Assemble the cheap state bundle for onboarding completion checks (§9.2).

    Reads manifest directories to count apps per bot, checks for health cache
    file, reads network config for thresholds and alerts, counts reviewed
    proposals.  All filesystem operations are wrapped — missing dirs return
    graceful defaults.

    Pod-wide signals added 2026-06-02 (pod-setup checklist):
      - primary_installed: network.json::primary is set AND in bots
      - pod_admins_claimed: network.json::pod.admins has any external_ids or pod_users
      - https_enabled: network.json::adminBaseUrl uses https:// scheme
      - github_dev_pat_configured: network.json::intake.github has v1 owner/repo or v2 targets
      - pod_tiers_configured: primary bot's ~/.openclaw/evolve-tiers.json
        resolves a tier2 (workhorse) model binding (baseline the rest of the
        pod inherits from; the former tier0/judge requirement died with the
        judge-role collapse, 2026-08-31)
      - messaging_channel_installed: any bot has any messaging channel installed
        (telegram/slack/discord/whatsapp pairing.json present) — without one,
        no bot can be reached

    Removed 2026-06-04 (operator review):
      - pod_conduct_authored: POD_CONDUCT.md is repo-distributed and not
        editable from the admin UI — including it as a "setup task" was
        misleading.
      - first_gallery_app_installed: installing an app is "using the
        pod," not "setting up the pod."
      - reviewed_proposals_count → first_security_audit task: proposal
        flow is an artifact of the system running, not an operator setup
        step. Also kept showing pending forever on a real pod.
    """
    members = network.get("members", [])
    app_counts: dict[str, int] = {}
    reviewed_app_counts: dict[str, int] = {}

    for bot_id in members:
        manifests = _list_manifests(shared_dir, bot_id)
        app_counts[bot_id] = len(manifests)
        reviewed_app_counts[bot_id] = sum(
            1 for m in manifests
            if getattr(m, "last_reviewed", None) or getattr(m, "lastReviewed", None)
        )

    health_cache_path = shared_dir / "better-engine" / "cache" / "health-latest.json"

    # ── Pod-wide signals ────────────────────────────────────────────────
    # Primary installed: network claims a primary AND it's registered in
    # bots. We do NOT also verify the macOS account + openclaw.json here
    # (status.setup_status does that) — those failures are already
    # surfaced by the no-primary banner; the checklist's job is just to
    # nag at the "primary not declared" gap.
    primary_bot_id = network.get("primary")
    primary_installed = bool(
        primary_bot_id
        and primary_bot_id in (network.get("bots") or {})
    )

    # Pod admins: any external_id list non-empty OR pod_users non-empty.
    # See network.json::pod.admins shape in config._default_network.
    # Strict filter on inner contents: a per-channel list with only None
    # / empty-string entries (legacy migration leftovers) does NOT count
    # as claimed — the operator hasn't actually associated an id.
    pod_admins = (network.get("pod") or {}).get("admins") or {}
    # The reader already drops empty/blank ids and empty channels, so a
    # non-empty map IS "an id is actually associated" (M1-B2).
    pod_admins_claimed = bool(_external_ids.read_external_ids(pod_admins))
    if isinstance(pod_admins, dict):
        if not pod_admins_claimed:
            pod_users = pod_admins.get("pod_users") or []
            if isinstance(pod_users, list) and any(
                isinstance(u, str) and u.strip() for u in pod_users
            ):
                pod_admins_claimed = True

    # HTTPS enabled: adminBaseUrl uses https://. Empty/missing values mean
    # the wizard hasn't been run yet, which we treat as not enabled. The
    # PWA install-on-iOS flow needs https; without it the chip stays
    # pending so the operator notices.
    admin_url = network.get("adminBaseUrl") or ""
    https_enabled = isinstance(admin_url, str) and admin_url.startswith("https://")

    # Primary bot's tier config — pod-wide baseline that other bots
    # inherit from. Per CLAUDE.md/oc_model.py, tiers live in
    # ~/.openclaw/evolve-tiers.json (NOT openclaw.json — OC's schema
    # validator rejects `tiers` there). Considered "configured" when
    # tier2 (workhorse — what runs human turns) resolves at least one
    # model (the former tier0/judge half of this check died with the
    # judge-role collapse, 2026-08-31). Mirrors the per-bot
    # ai_optim_tiers detector but specifically on the primary bot — if
    # the primary is tiered, the operator has visited AI Optimization
    # at least once.
    pod_tiers_configured = False
    if primary_installed:
        try:
            import json as _json
            from ..config import bot_home as _bot_home
            tiers_path = _bot_home(primary_bot_id, network) / ".openclaw" / "evolve-tiers.json"
            if tiers_path.is_file():
                tiers_doc = _json.loads(tiers_path.read_text(encoding="utf-8"))
                if isinstance(tiers_doc, dict):
                    # Resolve across both shapes — a migrated rungs/roles file
                    # has no top-level ``tiers`` key, so the bare ``.get`` would
                    # report the pod un-onboarded forever after migration.
                    from primary_bot import (  # type: ignore
                        merge_model_catalog,
                        resolve_tier_chain,
                        tier_override_is_broken,
                    )

                    # Keyed-merge the pod-base catalog first (spec §Addendum
                    # A.4) — a pod-base-only rung satisfying standard would
                    # otherwise report the pod un-onboarded. Mirrors
                    # setup_checklist._detect_ai_optim_tiers. Only tier2
                    # (workhorse) is required — tier0/judge died with the
                    # judge-role collapse (nothing routes to it).
                    pod_models = (network or {}).get("models") if isinstance(network, dict) else None
                    merged_doc = merge_model_catalog(pod_models, tiers_doc)

                    have = True
                    for required in ("tier2",):
                        # Explicit-but-empty override = broken config, flag it
                        # even though the merge no-ops it back to defaults
                        # (#2561 onboarding semantics). Absent → defaulted → OK.
                        if tier_override_is_broken(tiers_doc, required):
                            have = False
                            break
                        models = resolve_tier_chain(merged_doc, required)
                        if not any(isinstance(m, str) and m.strip() for m in models):
                            have = False
                            break
                    pod_tiers_configured = have
        except (OSError, ValueError, ImportError):
            pod_tiers_configured = False

    # Pod-wide GitHub PAT for dev issue tracking ("third purpose" of
    # GitHub creds — see project_github_credentials_three_purposes).
    # Accepts both the v1 single-target shape (owner/repo at top level
    # of intake.github) and v2 multi-target shape (intake.github.targets).
    intake_gh = (network.get("intake") or {}).get("github") or {}
    github_dev_pat_configured = False
    if isinstance(intake_gh, dict):
        v1_ok = bool(intake_gh.get("owner") and intake_gh.get("repo"))
        v2_targets = intake_gh.get("targets")
        v2_ok = isinstance(v2_targets, dict) and bool(v2_targets)
        github_dev_pat_configured = v1_ok or v2_ok

    # Messaging channel installed on any bot — "is the pod reachable
    # by anyone?" The per-bot setup_checklist.pairing detector checks
    # for an approved user (allowFrom); this pod-level signal is one
    # step earlier — just whether the channel infrastructure exists.
    # Once installed, the per-bot checks nag about completing pairing.
    messaging_channel_installed = False
    try:
        import subprocess as _sp
        from ..config import bot_home as _bot_home
        channels = ("telegram", "slack", "discord", "whatsapp")
        for bot_id in members:
            try:
                creds_dir = _bot_home(bot_id, network) / ".openclaw" / "credentials"
            except Exception:
                continue
            for channel in channels:
                pairing = creds_dir / f"{channel}-pairing.json"
                # Direct read first; fall back to sudo test for dirs the
                # evolve user can't traverse (.openclaw/credentials is
                # mode 700, ACL grants on the subdir may be missing).
                try:
                    if pairing.is_file():
                        messaging_channel_installed = True
                        break
                except (PermissionError, OSError):
                    pass
                try:
                    r = _sp.run(
                        ["sudo", "-n", "/usr/bin/test", "-f", str(pairing)],
                        check=False, capture_output=True, timeout=3,
                    )
                    if r.returncode == 0:
                        messaging_channel_installed = True
                        break
                except (_sp.SubprocessError, OSError):
                    continue
            if messaging_channel_installed:
                break
    except Exception:
        messaging_channel_installed = False

    return {
        "members": members,
        "health_cache_exists": health_cache_path.exists(),
        "app_counts": app_counts,
        "reviewed_app_counts": reviewed_app_counts,
        "spend_threshold_set": bool(
            network.get("thresholds", {}).get("dailySpendAlertUsd")
        ),
        "pod_report_configured": bool(
            network.get("alerts", {}).get("chatId")
        ),
        # Pod-wide:
        "primary_installed": primary_installed,
        "pod_admins_claimed": pod_admins_claimed,
        "https_enabled": https_enabled,
        "github_dev_pat_configured": github_dev_pat_configured,
        "pod_tiers_configured": pod_tiers_configured,
        "messaging_channel_installed": messaging_channel_installed,
    }


# ── Task definitions (§9.3) ───────────────────────────────────────────────────

def _make_check_members(key: str = "members") -> Callable[[dict], bool]:
    return lambda state: bool(state.get(key))


def _make_check_health_cache() -> Callable[[dict], bool]:
    return lambda state: bool(state.get("health_cache_exists"))


def _make_check_app_counts(bot_id: str) -> Callable[[dict], bool]:
    def _check(state: dict) -> bool:
        counts = state.get("app_counts", {})
        return counts.get(bot_id, 0) > 0
    return _check


def _make_check_reviewed_apps(bot_id: str) -> Callable[[dict], bool]:
    def _check(state: dict) -> bool:
        counts = state.get("reviewed_app_counts", {})
        return counts.get(bot_id, 0) > 0
    return _check


def _make_check_spend_threshold() -> Callable[[dict], bool]:
    return lambda state: bool(state.get("spend_threshold_set"))


def _make_check_pod_reports() -> Callable[[dict], bool]:
    return lambda state: bool(state.get("pod_report_configured"))


# _make_check_proposals was removed 2026-06-04 along with the
# first_security_audit task — see _GETTING_STARTED_DEFAULTS comment.


# ── Pod-wide check makers (added 2026-06-02 for pod-setup checklist) ─────


def _make_check_primary_installed() -> Callable[[dict], bool]:
    return lambda state: bool(state.get("primary_installed"))


def _make_check_pod_admins() -> Callable[[dict], bool]:
    return lambda state: bool(state.get("pod_admins_claimed"))


def _make_check_https() -> Callable[[dict], bool]:
    return lambda state: bool(state.get("https_enabled"))


def _make_check_github_dev_pat() -> Callable[[dict], bool]:
    return lambda state: bool(state.get("github_dev_pat_configured"))


def _make_check_pod_tiers() -> Callable[[dict], bool]:
    return lambda state: bool(state.get("pod_tiers_configured"))


def _make_check_messaging_channel() -> Callable[[dict], bool]:
    return lambda state: bool(state.get("messaging_channel_installed"))


# Module-level task templates.  Per-bot tasks use {bot_id} as a placeholder
# in their id.  get_active_tasks() instantiates them per member.

ONBOARDING_TASKS: list[OnboardingTask] = [
    OnboardingTask(
        id="add_first_bot",
        title="Add a bot member to your pod",
        detail="Your pod has no member bots configured yet.",
        context=(
            "Member bots are the AI agents in your OpenClaw pod. Evolve manages "
            "and improves them. Add at least one bot to get started."
        ),
        depends_on=[],
        action="open_setup",
        action_args={},
        per_bot=False,
        tags=["intent:setup", "category:configuration"],
        check=_make_check_members("members"),
    ),
    OnboardingTask(
        id="run_health_check",
        title="Run your first health check",
        detail="Verify your pod infrastructure is correctly set up.",
        context=(
            "The health check confirms LaunchDaemon services are loaded, file "
            "permissions are correct, and every gateway is reachable. Takes ~5 "
            "seconds and surfaces setup issues with one-click fixes."
        ),
        depends_on=["add_first_bot"],
        action="run_health_check",
        action_args={},
        per_bot=False,
        tags=["intent:setup", "category:health"],
        check=_make_check_health_cache(),
    ),
    OnboardingTask(
        id="scan_applications_{bot_id}",
        title="Scan {bot_name} for applications",
        detail="{bot_name} has no registered applications yet.",
        context=(
            "The application scanner discovers what {bot_name} already knows how "
            "to do by examining workspace files, cron jobs, and memory. Takes "
            "30–60 seconds and generates formal capability manifests you can "
            "track, test, and improve over time."
        ),
        depends_on=["run_health_check"],
        action="run_scanner",
        action_args={"bot_id": "{bot_id}"},
        per_bot=True,
        tags=["intent:setup", "intent:improve", "category:applications"],
        check=lambda state: False,  # replaced per-instance with bot_id
    ),
    OnboardingTask(
        id="review_first_application_{bot_id}",
        title="Review an application for {bot_name}",
        detail="No applications have been reviewed for {bot_name} yet.",
        context=(
            "Reviewing a manifest confirms it accurately describes what the bot "
            "does. You'll rate satisfaction (1–10) and note any gaps. This is "
            "the foundation of RSI — bots improve based on review feedback."
        ),
        depends_on=["scan_applications_{bot_id}"],
        action="open_applications",
        action_args={"bot_id": "{bot_id}"},
        per_bot=True,
        tags=["intent:setup", "intent:improve", "category:applications"],
        check=lambda state: False,  # replaced per-instance with bot_id
    ),
    OnboardingTask(
        id="configure_spend_threshold",
        title="Set a daily spend threshold",
        detail="You won't be alerted if your API costs spike unexpectedly.",
        context=(
            "Evolve sends a message when any bot's daily API spend exceeds a "
            "threshold. $3–5/day is reasonable for a moderately active bot. "
            "You can change it at any time in Cost Measures."
        ),
        depends_on=["add_first_bot"],
        action="open_cost_config",
        action_args={},
        per_bot=False,
        tags=["intent:setup", "category:cost"],
        check=_make_check_spend_threshold(),
    ),
    OnboardingTask(
        id="configure_pod_reports",
        title="Set up pod health reports",
        detail="Evolve can send regular pod summaries to your Telegram.",
        context=(
            "Pod reports deliver a weekly digest of bot health, spending, session "
            "quality, and open issues. Configure a chat ID and report frequency "
            "in Reports & Alerts to activate them."
        ),
        depends_on=["add_first_bot"],
        action="open_reports_config",
        action_args={},
        per_bot=False,
        tags=["intent:setup", "category:reporting"],
        check=_make_check_pod_reports(),
    ),
    # ── Pod-setup checklist (2026-06-02) ─────────────────────────────────
    # Six pod-wide items the Getting Started page tracks as a checklist,
    # surfaced as a `Setup X/N` chip on the Overview page header. Each
    # uses the same OnboardingTask shape — no new dataclass needed; the
    # data layer and UI render path are shared with the existing tasks.
    # See docs/spec discussion 2026-06-02 + project_evolve_three_bucket_ia.

    OnboardingTask(
        id="primary_installed",
        title="Install your primary bot (evo)",
        detail="No primary bot is registered yet.",
        context=(
            "The primary bot is your pod's conversational partner — it handles "
            "system alerts, fields admin requests, and routes cross-bot work. "
            "Without one declared, most of the admin UI gates itself behind a "
            "no-primary banner. The install wizard sets up the macOS account, "
            "openclaw.json, and pod membership in one pass."
        ),
        depends_on=[],
        action="open_install_evo",
        action_args={},
        per_bot=False,
        tags=["intent:setup", "category:configuration", "scope:pod"],
        check=_make_check_primary_installed(),
    ),
    OnboardingTask(
        id="pod_admins_claimed",
        title="Claim pod admin privileges",
        detail="No pod admins are recorded yet.",
        context=(
            "Pod admins can approve proposals, reset breakers, and run "
            "admin-scoped commands via chat. Without any claimed, evo's "
            "admin-mode commands (claim charles / claim darwin from a chat "
            "channel) all reject. Claim yourself from each messaging "
            "channel you use so admin-mode works wherever you're chatting."
        ),
        depends_on=["primary_installed"],
        action="open_users",
        action_args={},
        per_bot=False,
        tags=["intent:setup", "category:identity", "scope:pod"],
        check=_make_check_pod_admins(),
    ),
    OnboardingTask(
        id="https_enabled",
        title="Enable HTTPS for the admin dashboard",
        detail="adminBaseUrl is currently http://. Mobile install-on-iOS won't work.",
        context=(
            "Tailscale Serve fronts the admin dashboard with HTTPS using your "
            "tailnet's cert, so the PWA installs cleanly on iOS Safari and "
            "passes secure-context APIs (clipboard, notifications, "
            "service-worker push). Wraps `evolve-admin enable-https`. "
            "Pre-req: Tailscale signed in on this host."
        ),
        depends_on=["primary_installed"],
        action="open_https_wizard",
        action_args={},
        per_bot=False,
        tags=["intent:setup", "category:network", "scope:pod"],
        check=_make_check_https(),
    ),
    OnboardingTask(
        id="pod_tiers_configured",
        title="Define your AI tiers (primary bot)",
        detail="The primary bot's tier config in AI Optimization isn't fully set up.",
        context=(
            "Tiers map the four roles (grunt, workhorse, power, max) to "
            "specific LLM models. The primary bot's tiers are the pod's "
            "baseline — they cascade as defaults for every other bot until "
            "that bot overrides them. tier2 (workhorse — what runs human "
            "turns) needs at least one model picked; the other tiers have "
            "sensible defaults. Visit AI Optimization, pick a bot, then save."
        ),
        depends_on=["primary_installed"],
        action="open_ai_optimization",
        action_args={},
        per_bot=False,
        tags=["intent:setup", "category:configuration", "scope:pod"],
        check=_make_check_pod_tiers(),
    ),
    OnboardingTask(
        id="messaging_channel_installed",
        title="Install a primary messaging channel",
        detail="No bot has a messaging channel installed yet — bots are unreachable.",
        context=(
            "Bots need at least one channel — Slack, Telegram, Discord, or "
            "WhatsApp — installed to be reachable. Without one, the bot "
            "can't be talked to and admin commands like `evo claim` "
            "(which need a chat-side identity) won't work. Install via "
            "Skills → pick channel → Install. The wizard walks through "
            "OAuth (Slack), bot-token entry (Telegram/Discord), or QR "
            "pairing (WhatsApp). Once any one channel is installed for "
            "any bot, this item flips to Done; the per-bot setup "
            "checklist then nags about completing pairing on each bot "
            "individually."
        ),
        depends_on=["primary_installed"],
        action="open_skills",
        action_args={},
        per_bot=False,
        tags=["intent:setup", "category:messaging", "scope:pod"],
        check=_make_check_messaging_channel(),
    ),
    OnboardingTask(
        id="github_dev_pat",
        title="Connect a pod-wide GitHub PAT for issue tracking",
        detail="No intake.github target is configured.",
        context=(
            "Evolve can file dev issues against your repo when it discovers "
            "bugs in itself or in apps it manages (the 'third purpose' of "
            "GitHub credentials, separate from per-bot backup and per-bot "
            "MCP). Configures `network.json::intake.github` + a keystore "
            "slot for the PAT. Wraps `evolve-admin intake configure`."
        ),
        depends_on=["primary_installed"],
        action="open_github_dev_wizard",
        action_args={},
        per_bot=False,
        tags=["intent:setup", "category:integration", "scope:pod"],
        check=_make_check_github_dev_pat(),
    ),
    # ── Dashboard install (desktop + mobile) ─────────────────────────────
    # These two items can't be auto-detected — there's no pod-side signal
    # for "operator has the dashboard installed as a PWA / saved to home
    # screen." check() always returns False; the row is cleared by the
    # operator clicking "Mark installed ✓" in the modal (POSTs to
    # /api/better/getting-started/<id>/done) or "Not for this pod" to
    # dismiss. The modal carries the per-browser / per-OS instructions
    # so the operator doesn't need to remember the steps.
    OnboardingTask(
        id="install_desktop_app",
        title="Install the dashboard as a desktop app",
        detail="Save the dashboard to your dock or taskbar so it opens in one click.",
        context=(
            "Modern browsers can install web pages as standalone desktop "
            "apps — they get their own dock/taskbar icon, open without "
            "browser chrome, and stay signed in. Chrome / Edge / Arc: "
            "click the install icon in the address bar (or ⋮ menu → "
            "'Install Evolve…'). Safari: File → Add to Dock…. Firefox: "
            "bookmark with the keyword 'evolve' to type-and-enter from "
            "the address bar. The Maintenance → Setup wizard walks "
            "through this on step 5; this checklist item just nags until "
            "you've done it once."
        ),
        depends_on=["primary_installed"],
        action="open_desktop_install_modal",
        action_args={},
        per_bot=False,
        tags=["intent:setup", "category:client", "scope:pod"],
        check=lambda state: False,
    ),
    OnboardingTask(
        id="install_mobile_app",
        title="Install the dashboard on your phone",
        detail="Add the dashboard to your iOS or Android home screen.",
        context=(
            "iOS Safari: tap the Share icon → Add to Home Screen → Add. "
            "Android Chrome: tap the ⋮ menu → Install app (or Add to "
            "Home screen). The installed shortcut opens the dashboard "
            "full-screen, no browser bars — same approval and inspection "
            "flows as on desktop, just from your couch. Requires HTTPS "
            "(the previous checklist item) — iOS won't install an HTTP "
            "page as a full-screen app."
        ),
        depends_on=["primary_installed", "https_enabled"],
        action="open_mobile_install_modal",
        action_args={},
        per_bot=False,
        tags=["intent:setup", "category:client", "scope:pod"],
        check=lambda state: False,
    ),
]


def _instantiate_per_bot(template: OnboardingTask, bot_id: str) -> OnboardingTask:
    """Instantiate a per_bot task template for a specific bot_id."""
    bot_name = bot_id.capitalize()

    def _subst(s: str) -> str:
        return s.replace("{bot_id}", bot_id).replace("{bot_name}", bot_name)

    # Resolve depends_on: per_bot dependency templates also get bot_id substituted
    resolved_depends = [_subst(d) for d in template.depends_on]

    # Build per-bot check function
    if "scan_applications" in template.id:
        check_fn = _make_check_app_counts(bot_id)
    elif "review_first_application" in template.id:
        check_fn = _make_check_reviewed_apps(bot_id)
    else:
        check_fn = template.check

    resolved_args = {k: _subst(str(v)) for k, v in template.action_args.items()}

    return OnboardingTask(
        id=_subst(template.id),
        title=_subst(template.title),
        detail=_subst(template.detail),
        context=_subst(template.context),
        depends_on=resolved_depends,
        action=template.action,
        action_args=resolved_args,
        per_bot=False,  # instantiated tasks are no longer templates
        tags=list(template.tags),
        check=check_fn,
    )


# ── Task lifecycle helpers ─────────────────────────────────────────────────────

def _is_complete_or_skipped(task_id: str, getting_started: dict) -> bool:
    """Return True if a task is completed or skipped in the getting-started file."""
    task_state = getting_started.get("tasks", {}).get(task_id, {})
    return bool(task_state.get("completed") or task_state.get("how") == "skipped")


def get_active_tasks(
    state: dict,
    getting_started: dict,
    members: list[str],
) -> list[OnboardingTask]:
    """Return tasks ready to work on: dependencies complete, not yet done/skipped.

    Per-bot task templates are instantiated for each member.  Returns tasks in
    dependency order (parents before children).
    """
    if getting_started.get("dismissed"):
        return []

    # Build full task list, expanding per_bot templates
    all_tasks: list[OnboardingTask] = []
    for template in ONBOARDING_TASKS:
        if template.per_bot:
            for bot_id in members:
                all_tasks.append(_instantiate_per_bot(template, bot_id))
        else:
            all_tasks.append(template)

    completed_ids = {
        task_id
        for task_id, task_state in getting_started.get("tasks", {}).items()
        if task_state.get("completed") or task_state.get("how") == "skipped"
    }

    active: list[OnboardingTask] = []
    for task in all_tasks:
        # Skip if already complete or skipped
        if task.id in completed_ids:
            continue
        # Skip if any dependency is not yet complete
        if any(dep not in completed_ids for dep in task.depends_on):
            continue
        active.append(task)

    return active


def mark_task_complete(
    task_id: str,
    how: str,
    getting_started: dict,
) -> dict:
    """Update getting_started dict in memory (does not save to disk).

    how: "auto" | "manual" | "skipped"
    """
    from .model import now_iso
    tasks = getting_started.setdefault("tasks", {})
    tasks[task_id] = {
        "completed": how != "skipped",
        "completed_at": now_iso() if how != "skipped" else None,
        "how": how,
    }
    return getting_started


def auto_complete_tasks(
    state: dict,
    getting_started: dict,
) -> tuple[dict, list[str]]:
    """Run check() on all incomplete tasks; mark newly-complete ones with how="auto".

    Returns (updated_getting_started, list_of_newly_completed_task_ids).
    Does nothing if the guide has been dismissed.
    """
    if getting_started.get("dismissed"):
        return getting_started, []

    members = state.get("members", [])
    newly_completed: list[str] = []

    # Build full task list
    all_tasks: list[OnboardingTask] = []
    for template in ONBOARDING_TASKS:
        if template.per_bot:
            for bot_id in members:
                all_tasks.append(_instantiate_per_bot(template, bot_id))
        else:
            all_tasks.append(template)

    for task in all_tasks:
        task_state = getting_started.get("tasks", {}).get(task.id, {})
        if task_state.get("completed") or task_state.get("how") == "skipped":
            continue
        try:
            if task.check(state):
                getting_started = mark_task_complete(task.id, "auto", getting_started)
                newly_completed.append(task.id)
        except Exception:
            pass  # don't let a bad check break the refresh

    return getting_started, newly_completed
