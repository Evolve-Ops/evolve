"""Subcommand registry for the ``evo`` keyword surface.

Each subcommand declares its name, help text, role-availability, and handler.
``evo help`` and ``/api/evo/help`` render directly from this registry, so
adding a future subcommand only requires adding one entry here plus a handler
module.

The registry is intentionally small in v1 — wizard, guide, apps, profile, and
default are present but stub to a "coming soon" message. They graduate to real
handlers in subsequent PRs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Role = Literal["admin", "primary", "secondary"]

# Visibility tags used by ``EvoSubcommand.available_to``. Note that the
# dispatcher's gate (in dispatch.py) lets ``"admin"`` bypass these sets
# entirely — an admin can run anything visible to any role. So
# ``PRIMARY`` here means "primary AND admin can run this"; ``BOTH``
# (kept for backward-compat) means "everyone, including secondaries".
ADMIN: frozenset[Role] = frozenset({"admin"})
PRIMARY: frozenset[Role] = frozenset({"primary"})
SECONDARY: frozenset[Role] = frozenset({"secondary"})
BOTH: frozenset[Role] = frozenset({"primary", "secondary"})
ALL: frozenset[Role] = frozenset({"admin", "primary", "secondary"})


@dataclass(frozen=True)
class EvoSubcommand:
    """A single ``evo <name>`` command.

    ``handler`` is an import path of the form ``module:function``. The
    function is loaded lazily by the dispatcher and called as
    ``handler(role=..., bot_id=..., args=..., network=...)``.
    """

    name: str
    short_help: str
    long_help: str
    available_to: frozenset[Role]
    handler: str
    # Aliases route to the same handler. ``evo`` itself is handled
    # client-side (plugin) for the legacy bare-keyword path; not in this list.
    aliases: tuple[str, ...] = ()


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────


_REGISTRY: tuple[EvoSubcommand, ...] = (
    EvoSubcommand(
        name="help",
        short_help="show available evo commands",
        long_help=(
            "Lists every evo command you can run, filtered by your role.\n"
            "Run `evo help <name>` for details on a single command."
        ),
        available_to=BOTH,
        handler="evolve_admin.evo.handlers.help:render",
    ),
    EvoSubcommand(
        name="better",
        short_help="get a recommendation from the Better Engine",
        long_help=(
            "Surfaces the single most valuable next thing for this bot, "
            "across operations, security, cost, improvements, and whimsy. "
            "Reply A/accept, S/snooze, or N/next to act on it."
        ),
        available_to=PRIMARY,
        # NOTE: legacy direct-Telegram path; the plugin handles this client-side
        # and does not actually call this handler in v1.  Listed here so it
        # appears in `evo help` output. The handler points at a stub that
        # explains what to do, in case anything ever falls through to it.
        handler="evolve_admin.evo.handlers.stub:render_better_stub",
    ),
    EvoSubcommand(
        name="wizard",
        short_help="run (or re-run) the Evolve setup wizard for this bot",
        long_help=(
            "Walks you through a short conversation about who you are and what "
            "you want this bot to do. Builds your user profile and surfaces the "
            "platform's main capabilities. Resumable, and re-runnable any time "
            "you want to redo setup."
        ),
        available_to=BOTH,
        handler="evolve_admin.evo.handlers.stub:render_wizard_stub",
    ),
    EvoSubcommand(
        name="add-bot",
        short_help="plan and build a new bot for the pod in conversation (admin only)",
        long_help=(
            "Starts a conversation that plans a new bot — what job it "
            "should do, who it's for, its starter apps, the morning "
            "briefing default, the ground rules (what people are told, "
            "how the bot sounds), and what to call it — and then builds "
            "it: you confirm the plan, sort out its key (copy another "
            "bot's, paste a fresh one, or skip for later), and the bot "
            "is created, switched on, and checked, with progress "
            "reported as it goes. Its starter apps build right after.\n\n"
            "Admin-only: adding bots is a pod-level action. Distinct from "
            "`evo wizard`, which sets *you* up on an existing bot.\n\n"
            "Resumable — if you step away mid-conversation, `evo add-bot` "
            "picks up where you left off."
        ),
        available_to=ADMIN,
        # Dispatcher special-cases this (needs shared_dir + user_key for
        # the wizard session); stub keeps the registry's import well-formed.
        handler="evolve_admin.evo.handlers.stub:render_default_stub",
    ),
    EvoSubcommand(
        name="guide",
        short_help="author or edit the team guide for this bot",
        long_help=(
            "If your bot has secondary users (a team channel, a household, etc), "
            "the guide is a short note from you about how the bot is meant to be "
            "used. It's shown to secondaries during their wizard, and the bot "
            "itself reads it at every session start. Not yet implemented in v1."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.stub:render_guide_stub",
    ),
    EvoSubcommand(
        name="apps",
        short_help="list apps installed on this bot",
        long_help=(
            "Shows the applications registered on this bot. Each app is a "
            "discovered or hand-authored capability bundle — workflows, files, "
            "and tools that work together. Type `evo gallery` to see what you "
            "can install."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.apps:render",
    ),
    EvoSubcommand(
        name="app-audit",
        short_help="run / inspect app audits on this bot",
        long_help=(
            "Usage: `evo app-audit [<app> [full] | all [full] | accept "
            "<app> <sig> | history <app>]`\n\n"
            "Bare `evo app-audit` shows audit status for every app on this "
            "bot (`✓ clean`, `⚠ N findings`, `❌ failed`, `– never audited`).\n\n"
            "`evo app-audit <app>` queues an audit for one app and kicks "
            "the runner immediately. Append `full` to re-evaluate "
            "previously-accepted findings (`evo app-audit journal full`).\n\n"
            "`evo app-audit all` queues audits for every eligible app on "
            "this bot. `evo app-audit all full` does the same with "
            "--ignore-accepted.\n\n"
            "`evo app-audit accept <app> <sig>` marks a finding as accepted "
            "from a Proposal so future audits don't re-raise it.\n\n"
            "`evo app-audit history <app>` pastes the last few audit-trail "
            "entries for one app.\n\n"
            "Distinct from `evo audit` (security audit findings) — this "
            "one verifies that each installed app still does what its "
            "manifest claims."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.app_audit:render",
    ),
    EvoSubcommand(
        name="app-changes",
        short_help="inspect or resolve manifest drift on this bot's apps",
        long_help=(
            "Usage: `evo app-changes [<app> [approve | promote | flag "
            "<description>]]`\n\n"
            "Bare `evo app-changes` lists every app on this bot with the "
            "drift summary since the last review — file additions, "
            "removals, authored-field changes, quiet failures.\n\n"
            "`evo app-changes <app>` walks the detail for one app, "
            "grouping by quiet failures > authored drift > file changes.\n\n"
            "`evo app-changes <app> approve` accepts all current "
            "observational changes — the manifest now reflects the new "
            "reality but the fields stay observational (still tracked, "
            "but no chip).\n\n"
            "`evo app-changes <app> promote` marks the current state as "
            "authored. Future drift on these fields will surface again.\n\n"
            "`evo app-changes <app> flag <description>` lands a Proposal "
            "in the operator's queue for review.\n\n"
            "Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §10.4."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.app_changes:render",
    ),
    EvoSubcommand(
        name="app-coherence",
        short_help="run a coherence capability check on one app",
        long_help=(
            "Usage: `evo app-coherence <app>`\n\n"
            "Runs Pass A (manifest-internal graph walk, pure Python) + "
            "Pass C3 (LLM capability check — 'given only this manifest, "
            "could a competent developer build something that "
            "accomplishes the stated goal?') against one app and "
            "replies with the verdict.\n\n"
            "Pass C3 is rate-limited to 1 run per app per day. If a "
            "check already ran today, the reply uses the cached "
            "result.\n\n"
            "Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §6.5 + §10.4."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.app_changes:render_coherence",
    ),
    EvoSubcommand(
        name="file-proposal",
        short_help="file a structured app-repair proposal for operator review",
        long_help=(
            "Usage: `evo file-proposal --on-behalf-of <bot_id> --app "
            "<app_id> --action <kind> --content <json>`\n\n"
            "Bot-side scaffolding for in-situ app repair (spec §10.9). "
            "When a non-evo bot's primary user agrees to a fix during a "
            "Telegram/Slack/Discord conversation, the bot routes the "
            "proposal through the cross-bot `evo` keyword. The handler "
            "writes a Proposal to the arbiter store with "
            "`audience: pod_operator`; the operator approves before any "
            "manifest mutation happens.\n\n"
            "Action values match the admin-UI repair-chat surface "
            "(propose_field_edit, propose_file_edit, "
            "propose_test_exemption, mark_resolved, done). The --content "
            "payload is the JSON body from the proposal block the bot "
            "emitted in its session — same shape as Channel A's "
            "`<<<repair_proposal action=\"…\">>>` blocks.\n\n"
            "Available to every bot's gateway (any role can FILE — the "
            "operator approves)."
        ),
        # ALL (not PRIMARY) — any bot may route a proposal it agreed to
        # with its user. The Proposal's audience is locked to
        # pod_operator inside the handler so secondaries can't smuggle
        # bot_primary_user-audience proposals through this channel.
        available_to=ALL,
        handler="evolve_admin.evo.handlers.file_proposal:render",
    ),
    EvoSubcommand(
        name="app-scan",
        short_help="kick an immediate Tier 1 scan on a bot's workspace",
        long_help=(
            "Usage: `evo app-scan [<bot>]`\n\n"
            "Bypasses the change-detector and runs a Tier 1 scan "
            "immediately. Useful when you just landed new files and "
            "want them reflected on the Apps page without waiting for "
            "the next sweep.\n\n"
            "When `<bot>` is omitted, scans the bot the command runs "
            "on. Pod admins can scan any bot by name.\n\n"
            "Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §7.6.6."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.app_changes:render_scan",
    ),
    EvoSubcommand(
        name="fail",
        short_help="report a failure; the bot investigates and replies",
        long_help=(
            "Usage: `evo fail <description>` — describe what stopped "
            "working and the bot kicks off an investigation. You'll get "
            "an immediate `Looking into it` reply; the diagnosis arrives "
            "as a follow-up message when the investigation completes "
            "(typically under two minutes).\n\n"
            "Other forms:\n"
            "  • `evo fail recent` — re-investigates the most-recent "
            "failure you reported, useful if the first reply wasn't "
            "actionable.\n"
            "  • `evo fail status` — reports the state of any in-flight "
            "or recent investigation.\n"
            "  • `evo fail flag` — escalates to the operator after a "
            "no-diagnosis or unsatisfying reply. Creates a Proposal in "
            "the operator's queue with the full investigation context.\n\n"
            "Auth: this command is for the bot's primary user and pod "
            "admins. Other users on team bots get a `not for you` reply. "
            "Rate limit: 3 investigations per user per hour."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.fail:render",
    ),
    EvoSubcommand(
        name="cost",
        short_help="spend in the last 1d / 7d / 30d",
        long_help=(
            "Shows what this bot has spent on LLM calls over the last day, "
            "week, and month. When run from the sysadmin `evolve` bot, "
            "reports pod-wide totals with a per-bot breakdown."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.cost:render",
    ),
    EvoSubcommand(
        name="usage",
        short_help="activity in the last 1d / 7d / 30d",
        long_help=(
            "Calls, tokens, and cache-hit ratio for the last day, week, and "
            "month. When run from the sysadmin `evolve` bot, reports pod-wide "
            "with per-bot call counts."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.usage:render",
    ),
    EvoSubcommand(
        name="alerts",
        short_help="currently firing alerts for this bot",
        long_help=(
            "Lists firing signals — alerts the monitors have raised that "
            "haven't been resolved or snoozed. Pod-scope signals show up on "
            "every bot since they're relevant pod-wide. Sysadmin `evolve` bot "
            "sees everything grouped by producer."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.alerts:render",
    ),
    EvoSubcommand(
        name="health",
        short_help="operational health snapshot",
        long_help=(
            "Narrower than `evo alerts`: focuses on substrate health — "
            "gateway processes, daemons, daily backups, host load — by "
            "filtering signals to the `pod_health` and `host_health` "
            "producers."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.health:render",
    ),
    EvoSubcommand(
        name="integrations",
        short_help="what this bot is wired into",
        long_help=(
            "Shows messaging channels (Telegram, Slack, Discord, …), "
            "integration plugins (GitHub, Google, Brave, …), and MCP servers "
            "configured on this bot. Sysadmin `evolve` bot shows a compact "
            "matrix across the pod. For credential health, check `evo alerts`."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.integrations:render",
    ),
    EvoSubcommand(
        name="gallery",
        short_help="apps you can install from the gallery",
        long_help=(
            "Lists apps in the gallery catalog you don't have installed yet. "
            "Type `evo apps` to see what's already installed, or "
            "`evo install <name>` to queue an install."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.gallery:render",
    ),
    EvoSubcommand(
        name="fun",
        short_help="a whimsy item — riddle, dad joke, quote, fun fact",
        long_help=(
            "Pulls one random item from the whimsy pool (riddles, dad "
            "jokes, fun facts, historical trivia, quotes, brain "
            "teasers, word-of-the-day). Same content the BetterEngine "
            "surfaces in the dashboard's whimsy slot — `evo fun` just "
            "shows one in chat without waiting for an engine cycle."
        ),
        available_to=BOTH,
        handler="evolve_admin.evo.handlers.fun:render",
    ),
    EvoSubcommand(
        name="setup-google",
        short_help="one-time Google OAuth client setup for this bot",
        long_help=(
            "Usage: `evo setup-google` (run from each bot you want "
            "Google access on)\n\n"
            "Walks you through Google Cloud Console setup for *this bot*, "
            "collects the resulting client ID + secret + your admin "
            "server's externally-reachable base URL, and persists them so "
            "per-bot Workspace authorization (`evo connect google`) can "
            "fire on this bot.\n\n"
            "Per-bot under PR #1123: each bot has its own OAuth "
            "Application — typically tied to a different Google Cloud "
            "project / org / billing path (e.g. one bot for a household "
            "Google account, another for a company Workspace org). Run "
            "this once per bot. Two bots can opt-in to share a single "
            "OAuth app by copying one bot's `googleOAuthClient` block "
            "into the other's in `network.json`.\n\n"
            "One-and-done per bot. You don't need to re-run unless Google "
            "forces you to rotate the OAuth client. `client_id` lands in "
            "`network.json → bots[<this-bot>].googleOAuthClient`; "
            "`client_secret` lands in this bot's `auth-profiles.json` "
            "(chmod 600, root-owned) — never in plaintext logs, never in "
            "`network.json`, never on a different bot."
        ),
        available_to=PRIMARY,
        # Dispatcher special-cases this; stub keeps registry happy.
        handler="evolve_admin.evo.handlers.stub:render_default_stub",
    ),
    EvoSubcommand(
        name="connect",
        short_help="authorize this bot for an integration (e.g. Google)",
        long_help=(
            "Usage: `evo connect google [services...]`\n\n"
            "Surfaces a Google OAuth URL the operator clicks to grant this "
            "bot access to Gmail / Calendar / Drive / Docs / Sheets / "
            "Slides. Once consent completes, tokens land in this bot's "
            "`auth-profiles.json` and gallery apps (Morning Briefing, "
            "etc.) start working automatically.\n\n"
            "Defaults to the full Workspace scope set. Add explicit "
            "service ids to narrow scope, e.g. `evo connect google "
            "gmail_readonly calendar_readonly`. Or `evo connect google "
            "readonly` for the read-only defaults.\n\n"
            "Requires `evo setup-google` to have been run on *this bot* "
            "first — that wires this bot's own OAuth client (per-bot "
            "under PR #1123), which this command issues consent against. "
            "If you have legacy `oc gws` tokens, this is also the path "
            "to migrate them to the wizard-managed flow."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.connect:render",
    ),
    EvoSubcommand(
        name="app",
        short_help="create a new app via conversational drafting",
        long_help=(
            "Usage: `evo app create`\n\n"
            "Starts a short back-and-forth: you describe what the app "
            "should do, I draft a full spec, you reply `approve` to "
            "queue install, `iterate <feedback>` to revise, or "
            "`cancel` to drop it. Each iteration is one LLM call (~10s, "
            "~$0.05). Targets the bot you're talking to.\n\n"
            "For installing existing apps from the gallery, use `evo "
            "gallery` + `evo install <n>`."
        ),
        available_to=PRIMARY,
        # Dispatcher special-cases `evo app create`; this stub is here
        # so the registry's handler-import resolves at module load.
        handler="evolve_admin.evo.handlers.stub:render_default_stub",
    ),
    EvoSubcommand(
        name="install",
        short_help="install a gallery app on this bot",
        long_help=(
            "Usage: `evo install <n>` or `evo install <name>`\n\n"
            "Type `evo gallery` first — each app is numbered. `evo "
            "install 1` installs the first one. The name form (`evo "
            "install daily-health-tracker`) also works.\n\n"
            "Queues a forge job to build and install. Works for apps "
            "that don't need OAuth setup; OAuth-required apps still "
            "need the admin UI's Apps page in v1.\n\n"
            "Installs target the bot you're talking to. The sysadmin "
            "`evolve` bot can't install on others — run `evo install` "
            "from each bot directly."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.install:render",
    ),
    EvoSubcommand(
        name="mute",
        short_help="snooze a firing alert (default 24h)",
        long_help=(
            "Usage: `evo mute <alert-id> [duration]`\n\n"
            "Snoozes a firing signal so it doesn't show up in `evo alerts` "
            "until the duration elapses. Default 24h. Examples: `evo mute "
            "abc12345 2d`, `evo mute abc12345 1w`. Use the short id "
            "surfaced after each `evo alerts` entry."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.mute:render_mute",
    ),
    EvoSubcommand(
        name="unmute",
        short_help="re-fire a snoozed alert",
        long_help=(
            "Usage: `evo unmute <alert-id>`\n\n"
            "Transitions a snoozed signal back to firing so it shows up "
            "in `evo alerts` again. Useful when the condition still "
            "matters but the snooze was set too long."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.mute:render_unmute",
    ),
    EvoSubcommand(
        name="audit",
        short_help="latest security audit findings",
        long_help=(
            "Shows findings from the security audit (security_warden) — "
            "identity, config, machine, and proposal categories. Filtered "
            "view of `evo alerts` for one producer. The audit runs every "
            "15 min on cron; no need to trigger it manually.\n\n"
            "Sub-forms:\n"
            "  `evo audit infra`            run a pod-wide infrastructure "
            "audit (daemons, sudoers, ACLs, network.json schema, repo-puller "
            "freshness, signal-store retention).\n"
            "  `evo audit infra <element>`  same but limited to one element "
            "(daemons | sudoers | acls | network_json | repo_puller | "
            "signal_retention).\n"
            "  `evo audit infra status`     latest infra-audit summary.\n"
            "  `evo audit infra history`    recent infra-audit trail entries."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.audit:render",
    ),
    EvoSubcommand(
        name="skills",
        short_help="plugins and MCP servers wired into this bot",
        long_help=(
            "Shows OpenClaw plugins and MCP servers installed on this bot, "
            "grouped by category. Sysadmin `evolve` bot shows per-bot "
            "counts across the pod."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.skills:render",
    ),
    EvoSubcommand(
        name="profile",
        short_help="view your user profile, toggle do-not-track",
        long_help=(
            "Forms:\n"
            "  evo profile           — show what this bot has recorded about you\n"
            "  evo profile dnt on    — stop the bot from learning, wipe existing notes\n"
            "  evo profile dnt off   — resume learning\n\n"
            "Profile contents are private to this bot — they aren't surfaced "
            "in the admin UI."
        ),
        available_to=BOTH,
        # Special-cased in dispatch.py (needs channel + sender_external_id).
        # The stub stays in place so the registry's handler-import is well-formed.
        handler="evolve_admin.evo.handlers.stub:render_profile_stub",
    ),
    EvoSubcommand(
        name="continuity",
        short_help="learn about the Continuity Engine",
        long_help=(
            "The Continuity Engine is what lets the bot pick work up when "
            "you're not around. It captures deferred commitments from "
            "conversations (\"I'll do that tonight\", \"remind me Tuesday\", "
            "\"keep working on this overnight\"), queues them as structured "
            "tasks, and runs them at the right time — autonomously for "
            "low-risk inline actions, on your approval for full bot sessions."
        ),
        available_to=BOTH,
        handler="evolve_admin.evo.handlers.continuity:render",
    ),
    EvoSubcommand(
        name="default",
        short_help="set what bare 'evo' does for you on this bot",
        long_help=(
            "Usage: `evo default <subcommand>`. Currently bare `evo` prints "
            "a short orientation pointing at `evo wizard` (if you haven't "
            "completed setup) or `evo better` (otherwise). Overriding that "
            "default is not yet implemented in v1."
        ),
        available_to=BOTH,
        handler="evolve_admin.evo.handlers.stub:render_default_stub",
    ),
    EvoSubcommand(
        name="claim",
        short_help="claim admin or primary status for this bot",
        long_help=(
            "Usage: `evo claim <passphrase> [<passphrase>]`. Tells the system "
            "you are the bot's primary user, an admin for this pod, or both. "
            "The pod admin sets the passphrases (one for admin, one for "
            "primary) and shares them with the people who should have those "
            "roles. Passphrases are matched case-insensitively in any order, "
            "so `evo claim charles darwin` and `evo claim darwin charles` "
            "both record admin + primary in one go. Refuses to overwrite an "
            "existing primary on this channel without `--force` (run "
            "`evo show-identity <bot>` to check current state). Every claim "
            "is recorded to the identity audit log."
        ),
        available_to=ALL,
        # Special-cased in dispatch like `wizard` — listed here for help.
        handler="evolve_admin.evo.handlers.stub:render_claim_stub",
    ),
    EvoSubcommand(
        name="summary",
        short_help="pod-wide summary: alerts, activity, suggestions (admin only)",
        long_help=(
            "Shows what's happening across all your bots right now: any active "
            "alerts, this week's activity and spend, pending improvement "
            "suggestions, and which apps have been running.\n\n"
            "Admin-only: this is a pod-wide view that aggregates every bot's "
            "data, including bots that aren't yours. Non-admin primaries see "
            "their own bot's data through `evo cost`, `evo usage`, `evo alerts`, "
            "etc., which run per-bot when called from a member bot.\n\n"
            "Also reachable as: `evo status`, `evo week`, or the natural-language "
            "phrase `evo, what's on this week?`."
        ),
        available_to=ADMIN,
        aliases=("status", "week"),
        handler="evolve_admin.evo.handlers.pod_summary:render",
    ),
    EvoSubcommand(
        name="security",
        short_help="audit score + active findings for this bot",
        long_help=(
            "Runs the security audit for this bot and shows the result in plain "
            "language — score out of 100, and any active findings with "
            "what they mean and what to do about them.\n\n"
            "Also reachable via: `evo, what can my bot do?`\n\n"
            "The audit checks configuration, channel setup, command permissions, "
            "and authentication settings using OpenClaw's own scanner. Every "
            "finding here is a measured result, not an architectural claim.\n\n"
            "For the full audit history, open the Security page on the admin "
            "dashboard. To see what skills are installed, use the Skills page."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.oc_audit:render",
    ),
    EvoSubcommand(
        name="improve",
        short_help="describe what to make better — I'll classify and draft an issue",
        long_help=(
            "Usage: `evo improve <description>` (or just `/improve`).\n\n"
            "The conversational front door. Tell me what isn't working, what's "
            "confusing, or what you wish Evolve did differently — in your own "
            "words. I'll classify it (local-env / Evolve code / upstream / "
            "mixed), try to fix it in this chat if it's a setup issue, and "
            "otherwise draft a GitHub issue you can review and post.\n\n"
            "I never auto-post — you always approve before anything goes "
            "upstream. The drafted intake stays in your queue (`evo intake "
            "list`) until you promote it.\n\n"
            "Once captured, refine the draft with `evo revise <id> "
            "<instruction>` before posting."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.improve:render_improve",
    ),
    EvoSubcommand(
        name="revise",
        short_help="iterate on a captured-but-unfiled intake's draft",
        long_help=(
            "Usage: `evo revise <intake_id> <instruction>`.\n\n"
            "After `evo improve` captures a draft, refine it inline before "
            "posting. Examples:\n\n"
            "- `evo revise intake-20260523-a3f9 make it more concise`\n"
            "- `evo revise intake-20260523-a3f9 add a stack trace section`\n"
            "- `evo revise intake-20260523-a3f9 reframe as a feature request`\n\n"
            "Each revision overwrites the intake body; the prior version is "
            "kept in `revision_history` for audit (and a future `evo revise "
            "--undo`). Filed intakes are rejected — reply on GitHub instead."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.improve:render_revise",
    ),
    EvoSubcommand(
        name="bug",
        short_help="file a bug report",
        long_help=(
            "Usage: `evo bug <description>` or `evo bug --post <description>`.\n\n"
            "Captures a bug report locally with the current pod context "
            "(active bot, commit, firing-signal count). Use `evo intake list` "
            "to review, `evo intake promote <id>` to file it on GitHub.\n\n"
            "Add `--post` to capture and file in one step (the transcript is "
            "stripped before posting; redact any other PII yourself).\n\n"
            "Prefer `evo improve` for the conversational flow that "
            "classifies + drafts; `evo bug` is the direct-capture path "
            "for when you already know exactly what you want filed."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.intake:render_bug",
    ),
    EvoSubcommand(
        name="feature",
        short_help="file a feature request",
        long_help=(
            "Usage: `evo feature <description>` or `evo feature --post <description>`.\n\n"
            "Like `evo bug` but tags the intake as `feature`. Same review + "
            "promote flow."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.intake:render_feature",
    ),
    EvoSubcommand(
        name="intake",
        short_help="review captured bug reports / feature requests",
        long_help=(
            "Forms:\n"
            "  evo intake              — show recent open intakes\n"
            "  evo intake list         — same, with optional `bug`/`feature` filter\n"
            "  evo intake promote <id> — file the intake on GitHub\n\n"
            "Promotion requires `evolve-admin intake configure` to have set "
            "the target repo and stored a GitHub PAT in the keystore."
        ),
        available_to=PRIMARY,
        handler="evolve_admin.evo.handlers.intake:render",
    ),
    EvoSubcommand(
        name="tier",
        short_help="set the model tier for this conversation",
        long_help=(
            "Usage: `evo tier {auto|fast|standard|power|max}`\n\n"
            "Sets the model role for the rest of this conversation.\n"
            "  • Fast — quickest, cheapest (Haiku-class)\n"
            "  • Standard — balanced default (Sonnet-class)\n"
            "  • Power — high-complexity, expensive (Opus-class)\n"
            "  • Max — frontier model, ~2× Power cost; pull-only (Fable-class)\n"
            "  • Auto — clear any prior choice; classifier picks\n\n"
            "Clears on the next gateway restart. Use `evo tier-default` "
            "for an ongoing default.\n\n"
            "Per-bot opt-out: if the operator has turned off tier-control "
            "on this bot (`userTierOverride.enabled = false`), this "
            "command returns a notice instead of applying the change."
        ),
        available_to=BOTH,
        handler="evolve_admin.evo.handlers.tier:render_tier",
    ),
    EvoSubcommand(
        name="tier-default",
        short_help="set your ongoing default tier for this bot",
        long_help=(
            "Usage: `evo tier-default {auto|fast|standard|power|max}`\n\n"
            "Sets YOUR ongoing default role on this bot — persists across "
            "sessions and restarts per-user. The session classifier still "
            "overrides this for background work. Use `evo tier X` for a "
            "one-conversation override.\n\n"
            "Writes to {sharedDir}/{botId}/user-tier-prefs.json keyed by "
            "your user identity. `max` (Fable-class) is accepted as a "
            "per-user default; the operator's bot-wide default cannot be max."
        ),
        available_to=BOTH,
        handler="evolve_admin.evo.handlers.tier:render_tier_default",
    ),
)


def all_subcommands() -> tuple[EvoSubcommand, ...]:
    """Return the full registry."""
    return _REGISTRY


def role_can_run(role: Role, cmd: EvoSubcommand) -> bool:
    """True when ``role`` is allowed to invoke ``cmd``.

    Single source of truth for the admin-is-superset-of-primary rule.
    The dispatcher's pre-handler gate, ``subcommands_for_role``, and the
    ``evo help <name>`` long-form renderer all consult this so the three
    surfaces can't drift out of sync (the bug we hit on 2026-05-13:
    ``evo help`` listed ``better`` to an admin but ``evo help better``
    said "isn't available to you").
    """
    if role in cmd.available_to:
        return True
    if role == "admin" and "primary" in cmd.available_to:
        return True
    return False


def subcommands_for_role(role: Role) -> tuple[EvoSubcommand, ...]:
    """Return the subset visible to a given role.

    Admin is treated as a strict superset of primary (see
    :func:`role_can_run`). Without this bypass ``evo help`` would show
    admins only the commands that list ``"admin"`` literally in their
    ``available_to`` set, which is just ``claim``.
    """
    return tuple(sc for sc in _REGISTRY if role_can_run(role, sc))


def lookup(name: str) -> EvoSubcommand | None:
    """Look up a subcommand by name or alias. Case-insensitive."""
    needle = name.strip().lower()
    if not needle:
        return None
    for sc in _REGISTRY:
        if sc.name == needle or needle in sc.aliases:
            return sc
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedCommand:
    """The result of parsing a raw user message."""

    subcommand: str  # canonical name; "evo" for the bare-keyword case
    args: str        # remainder of the message after the subcommand token
    is_bare: bool    # True if the user typed just "evo" or "evolve"


# Natural-language phrase aliases: normalize the rest of the message after
# "evo" (or "evolve") to a canonical subcommand name. These cover the
# conversational triggers the spec calls out by name.
#
# Each entry is (phrase, canonical_subcommand). Phrases are matched
# whole-message only — i.e. the rest of the message after "evo " must
# equal the phrase exactly (after lowercasing + trailing-punctuation
# strip). We deliberately do NOT prefix-match: an earlier version did,
# and that caused "evo what's on the agenda" / "evo what's on tomorrow"
# / "evo whats on for team_bot_a" to silently route to the pod summary by
# starting with "what's on " or "whats on ". Whole-phrase matching keeps
# the conversational triggers tight.
#
# To support common variants we explicitly enumerate them. The short
# forms ("what's on", "whats on") are intentionally NOT listed — they
# overlap too readily with normal English.
_PHRASE_ALIASES: tuple[tuple[str, str], ...] = (
    # "what's on this week?" and natural variants
    ("what's on this week", "summary"),
    ("whats on this week", "summary"),
    ("what is on this week", "summary"),
    ("what's on for this week", "summary"),
    ("whats on for this week", "summary"),
    # plain "status" / "summary" — handled by single-token lookup via aliases
    # on the EvoSubcommand, but also listed here for completeness

    # "what can my bot do?" and natural variants → security audit handler.
    # V2.4-3: formerly consulted safety_summary (cut v2.2); now reads
    # the OpenClaw audit score + findings (measurable, not architectural claims).
    # Whole-phrase only — same strict matching policy as the "what's on" aliases.
    ("what can my bot do", "security"),
    ("what can this bot do", "security"),
    ("what can the bot do", "security"),
    ("what can it do", "security"),
)


def parse(raw_text: str) -> ParsedCommand | None:
    """Parse a raw user message into a subcommand + args.

    Returns ``None`` if the message is not an evo command at all.

    Recognizes:
      * ``evo``                     → bare (subcommand="evo", is_bare=True)
      * ``evolve``                  → bare (alias)
      * ``evo help``                → subcommand="help"
      * ``evo default better``      → subcommand="default", args="better"
      * ``evo, what's on this week?`` → subcommand="summary" (phrase alias)
      * ``evo status``              → subcommand="status" (alias for "summary")

    Matching is case-insensitive on the leading token. Trailing whitespace and
    a single trailing punctuation mark are tolerated for the bare case
    (``evo.``, ``evo!``, ``evo?``) since those are common Telegram slips.
    The leading "evo" token may be followed by a comma (``evo, help``) since
    that's a natural conversational phrasing.
    """
    text = raw_text.strip()
    if not text:
        return None

    # Strip a single trailing punctuation char so "evo." matches.
    stripped = text
    if stripped and stripped[-1] in ".!?,;":
        stripped = stripped[:-1].rstrip()

    parts = stripped.split(maxsplit=1)
    if not parts:
        return None
    head = parts[0].lower()
    # Allow trailing comma on the trigger word: "evo," → "evo".
    if head.endswith(","):
        head = head[:-1]
    if head not in ("evo", "evolve"):
        return None

    if len(parts) == 1:
        return ParsedCommand(subcommand="evo", args="", is_bare=True)

    rest = parts[1].strip()
    # Strip a leading comma from rest so "evo, help" → rest="help".
    if rest.startswith(","):
        rest = rest[1:].lstrip()

    rest_lower = rest.lower().rstrip(".!?,;").rstrip()

    # Try multi-word phrase aliases first. Whole-phrase match only —
    # see comment on _PHRASE_ALIASES for why prefix matching was removed.
    for phrase, canonical in _PHRASE_ALIASES:
        if rest_lower == phrase:
            return ParsedCommand(subcommand=canonical, args="", is_bare=False)

    sub_parts = rest.split(maxsplit=1)
    sub_name = sub_parts[0].lower()
    sub_args = sub_parts[1] if len(sub_parts) > 1 else ""
    return ParsedCommand(subcommand=sub_name, args=sub_args, is_bare=False)
