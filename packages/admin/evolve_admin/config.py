"""
Network config loader/writer used by both the CLI and web server.
All writes are atomic (write-to-temp + rename).
"""

from __future__ import annotations

import json
import os
import pwd  # noqa: F401 — kept as a patch target: tests patch
# evolve_admin.config.pwd.getpwnam, and (modules being singletons) that
# reaches the canonical evolve_config.bot_home's pwd lookup too.
import tempfile
from pathlib import Path
from typing import Any

from evolve_config import CANONICAL_NETWORK_JSON, CANONICAL_SHARED_DIR

# Default paths — platform-keyed via evolve_config/platform_profile
# (same strings on macOS; /var/lib/evolve on Linux).
DEFAULT_NETWORK_CONFIG = CANONICAL_NETWORK_JSON
DEFAULT_SHARED_DIR = CANONICAL_SHARED_DIR

# Well-known OpenClaw gateway ports — populated from network.json at runtime.
# No hardcoded bot names here; these are just common default port assignments
# that users can override per-bot in network.json.
# DEFAULT_FORGE_PORT removed — Forge bot deprecated, replaced by Sandbox (owned by evolve user)
KNOWN_BOTS: dict[str, int] = {}  # populated by callers from network config

# Pod invariants the dashboard surfaces in the keys table when missing.
# Override per-pod via network.json → podInvariantIntegrations.
# Brave was demoted from a pod invariant to an optional integration
# (2026-06-24): web search is optional, especially for the evo bot, and
# forcing it produced a perpetual "Setup required" row on fresh pods. GitHub
# stays invariant. The OLD default is retained below so the idempotent
# migration in ``_apply_pod_defaults`` can strip a brave entry that was baked
# into an existing network.json by the old default — without clobbering a
# deliberately-customized list.
DEFAULT_POD_INVARIANT_INTEGRATIONS: tuple[str, ...] = ("github",)

# The pre-demotion default. A network.json whose ``podInvariantIntegrations``
# equals this set (order-insensitive) is assumed to have inherited it from the
# old default rather than chosen it, so the migration narrows it to the new
# default. Any other list (reordered, extra providers, brave-only, ...) is
# treated as operator intent and left untouched.
_LEGACY_POD_INVARIANT_INTEGRATIONS: frozenset[str] = frozenset({"github", "brave"})

# TTL (seconds) for the discovered-PAT nonce returned by the github onboarding
# discover endpoint. Override per-pod via network.json → onboardingNonceTTLSeconds.
DEFAULT_ONBOARDING_NONCE_TTL_SECONDS: int = 600


def detect_system_timezone() -> str | None:
    """Return the host's IANA timezone, or None if detection fails.

    macOS symlinks ``/etc/localtime`` → ``/var/db/timezone/zoneinfo/<IANA>``;
    Linux uses ``/usr/share/zoneinfo/<IANA>``. Both formats split cleanly
    on the ``zoneinfo/`` segment. Result is validated through ``zoneinfo``
    so a malformed link surfaces as None rather than a bogus string.
    """
    try:
        link = os.readlink("/etc/localtime")
    except OSError:
        return None
    marker = "/zoneinfo/"
    idx = link.find(marker)
    if idx < 0:
        return None
    name = link[idx + len(marker):].strip("/")
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
    except Exception:
        return None
    return name


def resolve_pod_timezone(network: dict[str, Any]) -> str:
    """Resolve the effective pod timezone with auto-detect fallback.

    Order: explicit override (``network.timezone``) → system tz from
    ``/etc/localtime`` → "America/Los_Angeles" as the historical default.
    Use this everywhere the UI or server formats a timestamp; reading
    ``network["timezone"]`` directly skips the detect step.
    """
    explicit = (network.get("timezone") or "").strip()
    if explicit:
        return explicit
    detected = detect_system_timezone()
    if detected:
        return detected
    return "America/Los_Angeles"


def load_network(path: Path = DEFAULT_NETWORK_CONFIG) -> dict[str, Any]:
    """Load and return the network config, with sane defaults if missing."""
    if not path.exists():
        return _default_network()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"Cannot read network config at {path}: {e}") from e
    return _apply_pod_defaults(data)


# Existing installs predate the pod-level passphrase fields and have
# them missing or null. Without this fixup, ``evo claim`` and the wizard
# challenge phase reject every passphrase because ``admin_passphrase()``
# returns ``None``. Applied in-memory on every load so a restart of the
# admin server is enough — no manual edit required.
_POD_FIELD_DEFAULTS: dict[str, str] = {
    "admin_passphrase": "charles",
    "primary_passphrase": "darwin",
}


def _apply_pod_defaults(network: dict[str, Any]) -> dict[str, Any]:
    """Backfill pod-level defaults for fields that are missing or empty."""
    pod = network.get("pod")
    if not isinstance(pod, dict):
        pod = {}
        network["pod"] = pod
    for key, default in _POD_FIELD_DEFAULTS.items():
        value = pod.get(key)
        if not isinstance(value, str) or not value.strip():
            pod[key] = default
    _migrate_pod_invariant_integrations(network)
    return network


def _migrate_pod_invariant_integrations(network: dict[str, Any]) -> None:
    """Demote brave from pod-invariant on pods that inherited the OLD default.

    Changing ``DEFAULT_POD_INVARIANT_INTEGRATIONS`` only affects *new*
    network.json files; existing pods already have ``["github", "brave"]``
    baked in, so brave would keep showing as a forced "Setup required" row.

    This narrows the list to the new default ONLY when the existing list is
    exactly the old default ``{"github", "brave"}`` (order-insensitive, exact
    set match). A deliberately-customized list — reordered, brave-only, or with
    extra providers like ``["brave", "github", "tavily"]`` — is operator intent
    and left untouched. Idempotent: once narrowed, the set no longer matches the
    old default, so a re-run is a no-op. Applied in-memory on every load, so an
    admin-server restart is enough; ``save_network`` persists it on the next
    write.
    """
    current = network.get("podInvariantIntegrations")
    if not isinstance(current, list):
        return
    # Order-insensitive exact match against the pre-demotion default. Using a
    # set comparison (not list ==) so a reordered ["brave", "github"] is also
    # caught, while ["github", "brave", "tavily"] (a superset) is not.
    if {str(x) for x in current} == set(_LEGACY_POD_INVARIANT_INTEGRATIONS):
        network["podInvariantIntegrations"] = list(DEFAULT_POD_INVARIANT_INTEGRATIONS)


def save_network(data: dict[str, Any], path: Path = DEFAULT_NETWORK_CONFIG) -> None:
    """Atomically write network config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Stage in /tmp — /Users/Shared/evolve/network.json may be owned by root
    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix="evolve-network-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        try:
            import shutil, subprocess
            shutil.copy2(tmp, path)
            os.chmod(path, 0o644)  # world-readable — network.json has no secrets
        except PermissionError:
            import subprocess
            r = subprocess.run(["sudo", "/bin/cp", tmp, str(path)], capture_output=True)
            if r.returncode != 0:
                raise PermissionError(f"sudo /bin/cp failed: {r.stderr.decode().strip()}")
            subprocess.run(["sudo", "/bin/chmod", "644", str(path)], capture_output=True)
        # Always assert evolve:<gid-0 group> ownership so the health check
        # passes regardless of who ran the wizard or last wrote the file.
        # The chown BINARY and the gid-0 admin group both route through
        # platform_profile (W7): `/usr/sbin/chown`+`wheel` are macOS-only — on
        # Linux the binary is `/usr/bin/chown` and gid 0 is `root` (a literal
        # `evolve:wheel` chown HARD-fails on Ubuntu, and the macOS binary path
        # never matches the Linux NOPASSWD grant → sudo prompts → no TTY on
        # the admin daemon → silent failure on this daemon-critical path).
        from platform_profile import get_profile
        _prof = get_profile()
        subprocess.run(
            ["sudo", _prof.chown, f"evolve:{_prof.admin_group}", str(path)],
            capture_output=True, check=False,
        )
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    # ── Sync app-audit pod_config to every bot ───────────────────────────────
    # The Tier-3 audit runner on each bot reads its cadence + calibration
    # settings from /Users/<bot>/.openclaw/workspace/evolve/pod_config.json.
    # Pushing the sync here means a single network.json save propagates
    # policy changes to every bot's next audit tick. Best-effort: a failure
    # to sync one bot doesn't roll back the network save.
    try:
        from .applications.audit_pod_config import sync_all_pods
        sync_all_pods(data)
    except Exception:
        # Don't let an import / sync failure mask the network.json write.
        # The next deploy_bot for the affected bot will rewrite pod_config.
        pass


def _default_network() -> dict[str, Any]:
    return {
        "networkId": "my-network",
        # Sibling Evolve pods for the admin-UI hub switcher (multi-pod M2).
        # Top-level (pod *identity/topology*, not a pod-internal setting).
        # Each entry is {name, adminBaseUrl} only — names + URLs, NEVER
        # tokens (the v1 invariant). Empty ⇒ single-pod, no switcher
        # dropdown. Maintained manually in v1 (no live sync — §7 non-goal).
        # See docs/design-multi-pod-2026-06-11.md §3.1.
        "peers": [],
        "members": [],
        # The pod's primary bot — handles sysadmin alerts & proposals.
        # Resolution falls through ``primary_bot.primary_bot_id`` which
        # honors this field, then any bot with role=="primary", then a
        # legacy fallback to "evolve". Wizard fills this in.
        "primary": None,
        "sharedDir": str(DEFAULT_SHARED_DIR),
        # IANA tz name for UI rendering, or absent to auto-detect from
        # /etc/localtime (resolve_pod_timezone). Storage stays UTC;
        # clients format to the resolved zone. Override per-pod when
        # the system tz isn't right (e.g. mini in PT but UI in UTC).
        "thresholds": {
            "dailySpendAlertUsd": 5.0,       # alert if any bot spends > $5/day
            "weeklySpendAlertUsd": 20.0,     # alert if pod spends > $20/week
            "dailySpendCapUsd": None,        # hard cap — None = no cap; enforcement action fires when hit
            "weeklySpendCapUsd": None,       # hard cap for pod weekly total
            "spendCapAction": "downgrade-tier",  # alert-only | downgrade-tier | pause-crons | suspend-bot
            "maxSessionContextTokens": 100000,   # flag sessions exceeding this input-token total
        },
        "classifiers": {
            "tier": {
                "tier": "tier3",  # resolved at runtime via models.py
                "fallback": "keyword",
            },
            "judge": {
                "tier": "tier0",  # cross-model judge, resolved at runtime
            },
        },
        "alerts": {
            "channel": "telegram",
            "chatId": "",
        },
        # Pod-level identity for the evo wizard surface. Passphrases are
        # how users self-claim admin or primary status without needing
        # shell access to run ``evolve-admin evo claim-primary`` first.
        # Spec: docs/spec-evo-wizard-2026-05-05.md §3.2.2.
        # Pod admin tells primary users "type `evo claim darwin`" and the
        # system records them. Treated as soft secrets — the audit log
        # captures every claim so misuse is traceable, and the admin can
        # rotate via ``evolve-admin evo set-passphrase`` any time.
        "pod": {
            "admin_passphrase": "charles",
            "primary_passphrase": "darwin",
            # Pod-wide admins (not per-bot like primary_user); admin
            # access propagates to every bot in the pod.
            "admins": {
                # {channel: [external_id, ...]} — a LIST per channel
                # (invariant 6: one person, many platform ids). Legacy
                # per-bot blocks may still hold a bare string; read this
                # field ONLY through
                # ``evolve_admin.external_ids.read_external_ids``, which
                # tolerates both shapes, and write it only through that
                # module's writers, which always emit the list.
                "external_ids": {},
                "pod_users": [],     # [pod_user, ...]
            },
        },
        "bots": {},
        # Integrations every bot in the pod is expected to have. The dashboard
        # surfaces missing rows for these and offers guided onboarding.
        # Override per-pod to swap providers (e.g. ["github", "tavily"]).
        "podInvariantIntegrations": list(DEFAULT_POD_INVARIANT_INTEGRATIONS),
        # TTL (seconds) for the discovered-PAT nonce returned by the github
        # onboarding discover endpoint. The browser holds the nonce; the
        # actual PAT stays server-side. Past this window the nonce is rejected
        # (operator must re-Verify in the modal).
        "onboardingNonceTTLSeconds": 600,
        # ── Forge engine configuration ─────────────────────────────────────────
        # Learnable parameters (critique_rounds, min_improvement_confidence) live
        # in calibration_defaults/forge.json and are NOT repeated here.
        # This block contains operator policy — deployment decisions, not learned values.
        "forge": {
            # Model tiers for builder and critic phases (deployment config, not calibration)
            "builder_model_tier": "tier2",  # workhorse model for build phase
            "critic_model_tier": "tier2",   # model for critique phase (can be a stronger tier)
            # Automatically approve minor-version forge improvements if tests pass.
            # Default false — operator reviews everything. Opt-in per-network.
            "auto_approve_minor": False,
            # Max forge jobs that can run concurrently per bot
            "max_parallel_jobs": 1,
        },
        # ── App testing configuration ──────────────────────────────────────────
        # Pod-wide policy for periodic re-runs of per-app tests after forge.
        # Each manifest may override via its `test_cadence` field; null there
        # means "inherit this default". See docs/spec-app-testing-2026-05-07.md.
        # Valid cadences: off | on_change | light | strict.
        # Scheduler is gated on `scheduler_enabled` so PR 4 can land dark.
        "app_testing": {
            "default_cadence": "light",
            "scheduler_enabled": False,
            # Hard cap on test runs per scheduler tick.  Prevents a pod with
            # many due apps from chewing wall-clock or LLM budget in one pass;
            # apps over the cap are deferred to the next tick (sorted by
            # overdueness so the oldest-unrun ones win first).
            "max_runs_per_tick": 10,
            # Second-line ceiling on LLM judge calls fired by behavioral
            # runs in one tick.  A tick may run max_runs_per_tick manifests,
            # but if their combined test_cases would exceed this ceiling the
            # behavioral suite is truncated mid-flight and the rest defer
            # to the next content-changed tick.  Smoke runs are unaffected.
            # See docs/spec-behavioral-runs-2026-05-07.md §5.
            "max_judge_calls_per_tick": 50,
            # Tier used for the behavioral judge.  Resolved via
            # models.resolve_tier; defaults to tier3 (Haiku) — cheap and
            # adequate for "does this output match the expected behavior"
            # judgments.  Distinct from network.classifiers.judge.tier
            # (which is for cross-model classifier audits and defaults to
            # tier0 / OpenAI).  Operators wanting more rigour can point
            # this at tier2.  See docs/spec-behavioral-runs-2026-05-07.md §3.
            "judge_tier": "tier3",
        },
        # ── App audit configuration ────────────────────────────────────────────
        # Pod-wide policy for the Tier-3 semantic audit (the LLM-driven
        # half — Tier-2 structural runs unconditionally and isn't governed
        # by these settings). See docs/spec-app-audit-2026-05-16.md §6.
        # Cadence resolution: per-app manifest.audit_cadence → per-bot
        # bot_cadence[<bot>] → pod default_cadence → built-in monthly.
        "app_audit": {
            # Default cadence applied to every bot's apps unless overridden.
            # One of: never | quarterly | monthly | weekly | daily.
            "default_cadence": "monthly",
            # Per-bot overrides, e.g. {"team_bot_a": "weekly"} to raise scrutiny on
            # one load-bearing bot without editing every manifest.
            "bot_cadence": {},
            # Calibration mode: when true, Stage 3b's auto_fix outcome is
            # demoted to propose at the orchestration layer. Default ON
            # for v1 so operators see every proposed change before the
            # system can act on its own.  Spec §5.1.
            "calibration_mode": True,
            # When the Tier-2 verifier emits a `critical` finding, the
            # bot's next Tier-3 tick also audits that app even if it isn't
            # due by cadence.  Surface symptom of deeper rot.  Spec §3.4.
            "audit_on_critical_structural": True,
            # Model tier used for Stage 3a / Stage 3b LLM calls.  Resolved
            # via models.resolve_tier — same pattern as the test judge tier.
            "tier3_tier": "tier2",
            # Rate ceilings (spec §5.3) — applied per audit run per app.
            "ceilings": {
                # Max auto_fix transformations executed per audit run per app.
                "max_auto_fix_per_run": 3,
                # Max Proposals raised per audit run per app.
                "max_proposals_per_run": 5,
                # Combined input-token cap across Stage 3a + 3b for one app.
                "max_tokens_per_audit": 100000,
            },
        },
        # ── Pod-wide infrastructure audit ──────────────────────────────────────
        # Admin-side audit covering managed daemons, sudoers, ACLs,
        # network.json schema, repo-puller freshness, and signal-store
        # retention. Runs on the admin's scheduler tick on the configured
        # cadence (default daily). The runner reads this block directly
        # from network.json — it's admin-side, no per-bot sync needed.
        # See docs/spec-audit-extensions-2026-05-17.md §4.3.
        "infra_audit": {
            # off | daily | weekly | manual
            "default_cadence": "daily",
            # Demote auto_fix outcomes to propose at the orchestration layer.
            # Default ON for v1 — no automated infra remediation yet.
            "calibration_mode": True,
            # Model tier for the Discovery + Triage two-stage LLM dispatch.
            # The runner uses the admin's own credentials (pod-wide, not
            # per-bot, since this isn't bot work).
            "tier3_tier": "tier2",
            # Per-element overrides — e.g. {"acls": "off"} to silence ACL
            # checks on a pod where the operator has accepted known drift.
            "element_overrides": {},
            "ceilings": {
                "max_proposals_per_run": 5,
                "max_tokens_per_audit": 100000,
            },
        },
        # ── evo telemetry (opt-in upload) ──────────────────────────────────────
        # Per spec §14.3 — when evo encounters an operator request that doesn't
        # fit any existing tool, it records a structured ``tool_gap`` to
        # {shared_dir}/observations/tool_gaps.jsonl. Records ALWAYS write
        # locally (useful for the operator's own debugging). Upload is opt-in.
        #
        # Per ``feedback_user_observation_optout``, observation features ship
        # with a user-flippable DNT switch + wipe path. Wipe is via the future
        # ``evolve-admin wipe-telemetry`` CLI; the function is already exposed
        # (evolve_admin.evo.tool_gaps.wipe_tool_gaps).
        "evo_telemetry": {
            # tool_gaps: "off" (default) | "aggregated"
            # - "off"        — records stay local; never upload
            # - "aggregated" — periodic anonymized rollup to Evolve's
            #                  telemetry endpoint (kind/scope/frequency/
            #                  fingerprint only; no bot ids, no chat content)
            "tool_gaps": "off",
        },
    }


def get_bot_workspace(bot_id: str, user: str | None = None) -> Path | None:
    """Return the workspace path for a bot by reading its openclaw.json.

    When ``user`` isn't passed, the macOS account is resolved from
    ``network.json`` (the ``bots.<bot_id>.user`` field) so bots whose
    bot_id differs from their account name (e.g. ``team_bot_b`` runs as
    ``personal_bot_user``) still resolve correctly. Falls back to ``bot_id`` only
    when no network mapping is available — preserves the original
    behavior for setups where they match.

    Reads directly with a sudo /bin/cat fallback; never uses sudo -u <bot>
    since the bot account may not exist under the bot_id name.
    """
    import subprocess
    if user is None:
        try:
            actual_user = get_bot_user(bot_id, load_network())
        except Exception:
            actual_user = bot_id
    else:
        actual_user = user
    oc_json = user_home(actual_user) / ".openclaw/openclaw.json"
    oc_text: str | None = None
    try:
        oc_text = oc_json.read_text()
    except PermissionError:
        r = subprocess.run(["sudo", "-n", "/bin/cat", str(oc_json)],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            oc_text = r.stdout
    except OSError:
        pass
    if oc_text:
        try:
            ws = json.loads(oc_text).get("agents", {}).get("defaults", {}).get("workspace")
            if ws:
                return Path(ws)
        except (json.JSONDecodeError, Exception):
            pass
    return user_home(actual_user) / ".openclaw/workspace"


# Canonical bot-identity resolution lives in evolve_config (the analyzer
# package — the bottom of the dependency graph). These wrappers keep the
# admin-flavoured contract (network is required-or-load_network'd here,
# not load_config'd there) and the evolve_admin.config import path that
# ~everything admin-side and many tests use. dup-primitive-lint allowlists
# exactly this delegation.

def get_bot_user(bot_id: str, network: dict[str, Any]) -> str:
    """Return the actual macOS username for a bot (may differ from bot_id)."""
    from evolve_config import get_bot_user as _canonical
    return _canonical(bot_id, network)


def is_planned_bot_block(cfg: Any) -> bool:
    """True when a network.json bot block describes a *planned* bot —
    one whose only key is ``purpose`` (written by the add-bot wizard's
    confirmed plan, spec docs/spec-add-bot-wizard-build-delta-2026-06-10.md
    §4). Planned bots have no account, port, or deploy state: they may be
    re-planned, and provisioning *graduates* them into real members
    rather than treating the name as taken. Single source of truth for
    that predicate — the wizard engine, provisioning validation, and
    add_bot registration all consult it."""
    return isinstance(cfg, dict) and set(cfg.keys()) <= {"purpose"}


def bot_user_for(bot_id: str) -> str:
    """Self-loading convenience over get_bot_user for call sites that only
    have a bot_id in hand. Swallows network-load failures to bot_id —
    callers are typically display/subprocess paths that should degrade,
    not raise."""
    try:
        return get_bot_user(bot_id, load_network())
    except Exception:
        return bot_id


# ── Reserved service / assistant accounts (EVO-SEP-S5) ────────────────────────
#
# These OS account names must NEVER be removed through the bot-lifecycle paths
# (retire-bot / delete-bot / remove-evolve). They are pod infrastructure, not
# member bots:
#
#   * ``evolve`` — the Unix service user the admin-ui, repo-puller, mcp-bridge,
#     and backup LaunchDaemons all run as. It also owns the pod-infrastructure
#     launchd/systemd namespace ``ai.evolve.evolve.*``. Deleting it removes the
#     admin server's own account + home; tearing out its daemons bricks the pod.
#   * ``evo`` — the separated assistant / primary account introduced by
#     docs/spec-evo-account-separation-2026-05-25.md.
#
# ``evolve`` is the non-negotiable entry. This frozenset is the single source of
# truth so the removal guards (retire.py, cli.py) and their tests agree.
RESERVED_BOT_IDS: frozenset[str] = frozenset({"evolve", "evo"})


# Canonical default purpose for each reserved/built-in bot. Unlike member bots
# — whose purpose the operator declares — a system assistant's purpose is fixed
# by what the account IS, so we seed it once (never overwriting an operator
# edit; see ``ensure_reserved_bot_purposes``). This is the single home for these
# identity strings; they are owned by the ``evo-asst`` aspect, so edit them here
# rather than scattering copies. ``archetype`` is the fixed ``custom`` (these
# don't fit the member-bot archetype playbooks); ``mission`` is one tight line.
RESERVED_BOT_PURPOSES: dict[str, dict[str, str]] = {
    "evo": {
        "archetype": "custom",
        "mission": "System-administration assistant to the operator of this Evolve pod.",
    },
    "evolve": {
        "archetype": "custom",
        "mission": "Pod administration and self-improvement orchestrator for this Evolve pod.",
    },
}


def ensure_reserved_bot_purposes(network: "dict[str, Any]") -> bool:
    """Seed each reserved bot present in ``network['bots']`` a default purpose.

    Self-heal for the built-in assistants (``evo`` / ``evolve``): a system
    assistant's purpose is intrinsic, so its Setup-checklist ``purpose`` row
    should read Done out of the box instead of nagging the operator to declare
    what the account already is. Idempotent — only seeds when the bot has no
    purpose yet, so an operator-edited purpose is never overwritten.

    Mutates ``network`` in place. Returns True iff it changed anything (the
    caller persists via ``save_network`` only when this returns True). Wires in
    on both fresh pods (the setup wizard's network build) and live pods (the
    admin Setup-checklist read path — ``web.routes_setup_checklist``), so the
    already-running ``evo`` backfills with no manual step. See
    :data:`RESERVED_BOT_PURPOSES`.
    """
    bots = network.get("bots")
    if not isinstance(bots, dict):
        return False
    # Lazy import — bot_purpose lives in the analyzer package; importing it at
    # module top would couple config.py (loaded everywhere) to the analyzer
    # path resolution. Mirrors the lazy-import pattern used elsewhere here.
    try:
        from bot_purpose import has_purpose, set_bot_purpose
    except Exception:
        return False
    from datetime import datetime, timezone
    reviewed_at = datetime.now(timezone.utc).isoformat()
    changed = False
    for bot_id, default_purpose in RESERVED_BOT_PURPOSES.items():
        if bot_id not in bots:
            continue
        if has_purpose(network, bot_id):
            continue
        set_bot_purpose(
            network, bot_id, dict(default_purpose),
            captured="declared", reviewed_at=reviewed_at,
        )
        changed = True
    return changed


def is_reserved_account(bot_id: str, network: "dict[str, Any] | None" = None) -> bool:
    """True when ``bot_id`` — or the macOS user it resolves to — names a
    protected service/assistant account (see :data:`RESERVED_BOT_IDS`).

    Resolving the macOS user too closes the alias hole: a bot whose id is
    innocuous but whose ``bots[<id>].user`` points at ``evolve``/``evo`` would
    otherwise slip past a name-only check and still tear out a reserved account
    on delete. Network-resolution failures degrade to the id-only check rather
    than raising — callers are privileged removal guards that must fail closed
    on the literal name regardless of whether network.json is readable.
    """
    if not bot_id:
        return False
    if bot_id in RESERVED_BOT_IDS:
        return True
    try:
        user = get_bot_user(bot_id, network) if network is not None else bot_user_for(bot_id)
    except Exception:
        user = bot_id
    return user in RESERVED_BOT_IDS


# Pod-infrastructure launchd/systemd namespace. The daemons that run as the
# ``evolve`` service user (admin-ui, repo-puller, mcp-bridge, backup, heal,
# audit, …) all live under this prefix. A *per-bot* label must never collide
# with it — that collision is the EVO-SEP-S5 footgun: a bot literally named
# ``evolve`` makes ``ai.evolve.{bot_id}.backup`` resolve to the real pod-infra
# ``ai.evolve.evolve.backup`` daemon, so a per-bot teardown would boot out pod
# infrastructure. See docs/spec-evo-account-separation-2026-05-25.md.
POD_INFRA_LABEL_PREFIX = "ai.evolve.evolve."


def is_pod_infra_label(label: str) -> bool:
    """True if ``label`` is in the pod-infrastructure namespace
    (``ai.evolve.evolve.*``).

    Accepts either the bare launchd label or a systemd unit name
    (``<label>.service`` / ``<label>.timer``) — the prefix is identical on both
    profiles, so the check is platform-agnostic (macOS and Linux).
    """
    return label.startswith(POD_INFRA_LABEL_PREFIX)


def bot_home(bot_id: str, network: "dict[str, Any] | None" = None) -> Path:
    """Resolve a bot's macOS home directory.

    bot_id (logical name in network.json) may differ from the macOS account
    name (when one bot lives on a personal/shared account). Resolves the user
    via get_bot_user and looks up the home dir via pwd, falling back to
    /Users/{user}.
    """
    if network is None:
        network = load_network()
    from evolve_config import bot_home as _canonical
    return _canonical(bot_id, network)


def user_home(user: str) -> Path:
    """Resolve an OS ACCOUNT name's home (pwd-first, profile-keyed
    construction fallback). Companion to bot_home() for call sites that
    already hold the resolved account name — never pass a bot_id here."""
    from evolve_config import user_home as _canonical
    return _canonical(user)


def scanner_python() -> str:
    """Interpreter for the application_scanner.py subprocess.

    MUST be the venv python that has evolve_admin / evolve_analyzer
    installed (editable) — NOT /usr/bin/python3, which on macOS is the
    system Python 3.9.6 with none of the packaged modules. Since
    evolve_admin became a real package (6.1, #2592) the scanner's
    ``import evolve_admin`` fails under system python, so every bot's
    scan dies at ``ModuleNotFoundError: No module named 'evolve_admin'``.

    Sourced from ``platform_profile.get_profile().venv_python`` — the SAME
    table the §7a sudoers grant renders from (setup_wizard._render_evolve_sudoers).
    web/server.py calls this for the runtime ``sudo -u <bot> <python>
    scanner.py`` invocation; the grant renders the identical path. sudo
    matches the command as a literal string, so if these two drift by even
    one character sudo demands a password and the scan fails silently —
    routing both through the one profile value is what prevents that.
    """
    from platform_profile import get_profile

    return get_profile().venv_python


def chown_bin() -> str:
    """Absolute path of the ``chown`` binary for this platform.

    ``/usr/sbin/chown`` on macOS, ``/usr/bin/chown`` on Linux — sourced from
    ``platform_profile.get_profile().chown``, the SAME table the §7a sudoers
    grant renders from (setup_wizard._render_evolve_sudoers). sudo matches a
    NOPASSWD command as a literal string, so a hardcoded macOS path in a
    ``sudo chown`` argv fails to match the Linux grant → sudo prompts for a
    password → no TTY on the admin server → "sudo: a terminal is required to
    read the password". Route every ``sudo chown`` argv through this so the
    consumer and the grant can't drift.
    """
    from platform_profile import get_profile

    return get_profile().chown


# Default auto-approve threshold for forge installs (USD). When the
# projected mid cost is ≤ this value the install dispatches without
# operator confirmation; above it requires an explicit click. Override
# per-bot via network.json::bots[bot].forge_auto_approve_under_usd.
DEFAULT_FORGE_AUTO_APPROVE_UNDER_USD = 5.0


def get_forge_auto_approve_threshold(
    bot_id: str, network: dict[str, Any]
) -> float:
    """Per-bot forge-install auto-approve threshold from network.json.

    Reads ``bots[bot_id].forge_auto_approve_under_usd``. Returns the
    default (``DEFAULT_FORGE_AUTO_APPROVE_UNDER_USD``) when unset or
    unparseable. Zero or negative values mean "always confirm" — they
    push the threshold below any plausible projection, so every install
    surfaces the modal.
    """
    bot_cfg = (network.get("bots") or {}).get(bot_id) or {}
    raw = bot_cfg.get("forge_auto_approve_under_usd")
    if raw is None:
        return float(DEFAULT_FORGE_AUTO_APPROVE_UNDER_USD)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_FORGE_AUTO_APPROVE_UNDER_USD)


def get_forge_install_exempt_from_daily_cap(
    bot_id: str, network: dict[str, Any]
) -> bool:
    """Whether operator-confirmed forge installs bypass daily_cap_usd.

    Reads ``bots[bot_id].forge_install_exempt_from_daily_cap``. Defaults
    to True: the operator already saw and confirmed the projected cost
    via the spec-review / install-modal flow, so a tight daily_cap_usd
    (set to protect against *unattended* runaway like heartbeat or cron
    loops) should not also block the operator's explicit action.

    Set to False per-bot when the operator wants the cap to apply
    universally — e.g. on a member-shared bot where even operator
    actions should respect the budget.
    """
    bot_cfg = (network.get("bots") or {}).get(bot_id) or {}
    raw = bot_cfg.get("forge_install_exempt_from_daily_cap")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    try:
        return bool(int(raw))
    except (TypeError, ValueError):
        return True


def _coerce_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    try:
        return bool(int(raw))
    except (TypeError, ValueError):
        return default


def get_forge_require_explicit_dispatch(
    bot_id: str, network: dict[str, Any]
) -> bool:
    """Whether the wizard's Approve action should stop at ``queued`` instead
    of auto-dispatching the forge job.

    The default ``False`` is the one-click flow: clicking "Approve & Build"
    in the spec wizard creates the manifest, creates the forge job, and
    immediately dispatches the bot — single decision, single click.

    Setting ``True`` opts back into the two-step gate (approve → review on
    the Forge Jobs page → dispatch). Useful for operators who want to
    inspect the queued job (model, projected cost, target bot) on the
    Forge Jobs page before dispatching the work.

    Resolution order (first hit wins):
      1. ``bots[bot_id].forge.require_explicit_dispatch``
      2. ``bots[bot_id].forge_require_explicit_dispatch`` (flat alias)
      3. ``forge.require_explicit_dispatch`` (pod-wide)
      4. Default ``False``.
    """
    bot_cfg = (network.get("bots") or {}).get(bot_id) or {}
    bot_forge = bot_cfg.get("forge") if isinstance(bot_cfg.get("forge"), dict) else {}
    if bot_forge and "require_explicit_dispatch" in bot_forge:
        return _coerce_bool(bot_forge.get("require_explicit_dispatch"), False)
    if "forge_require_explicit_dispatch" in bot_cfg:
        return _coerce_bool(bot_cfg.get("forge_require_explicit_dispatch"), False)
    pod_forge = network.get("forge") if isinstance(network.get("forge"), dict) else {}
    if pod_forge and "require_explicit_dispatch" in pod_forge:
        return _coerce_bool(pod_forge.get("require_explicit_dispatch"), False)
    return False


def get_bot_port(bot_id: str, network: dict[str, Any]) -> int | None:
    """Return the gateway port for a bot from network config."""
    bot_cfg = network.get("bots", {}).get(bot_id, {})
    port = bot_cfg.get("port")
    if port:
        return int(port)
    # Try reading from the bot's own openclaw.json
    actual_user = get_bot_user(bot_id, network)
    oc_json = user_home(actual_user) / ".openclaw/openclaw.json"
    try:
        cfg = json.loads(oc_json.read_text())
        return cfg.get("gateway", {}).get("port")
    except (json.JSONDecodeError, OSError):
        pass
    return None


# ── Pod context: SSH target, repo URL, host labels ───────────────────────────
#
# Centralized resolution for operator-environment values that vary per
# install. Any code path that emits an operator-facing string with an
# SSH target, repo URL, or hostname embedded should pull it from here,
# not hardcode it. The previous round of fixes (commit e0f126bb) wired
# this into dispatcher.send / catalog action rendering; subsequent
# adopters (repo_puller, health, cli, feedback, tunnel) import these
# helpers directly.


def resolve_pod_context(network: "dict[str, Any] | None") -> "dict[str, str]":
    """Compute pod-derived placeholder values for operator-facing strings.

    Returns a dict that callers can either `.format(**ctx)` into a
    template directly or merge into a larger payload before rendering.
    Fields:

    - ``ssh_prefix`` — empty string when the operator is at the deploy
      box (``pod.ssh_target = ""``), else ``"ssh <target> "`` (trailing
      space included so ``{ssh_prefix}sudo …`` reads correctly in both
      cases). Resolution order:
        1. ``network.pod.ssh_target`` if set — verbatim. Supports SSH
           ``Host`` aliases like just ``"mini"`` for operators who
           configure ``~/.ssh/config``.
        2. ``network.admin_user@<host from adminBaseUrl>`` — auto-derived
           default that works for anyone with SSH access to the pod box.
        3. Empty string — when neither admin_user nor adminBaseUrl is
           available, drop the SSH wrapper so the rendered command at
           least runs locally.
    - ``ssh_target_label`` — short human-readable label for SSH key
      comments and GitHub deploy-key titles. Same resolution order;
      no trailing space; no leading ``ssh ``. Falls back to
      ``"evolve-pod"`` when nothing is configured.
    - ``admin_user`` — copied from ``network.admin_user`` for callers
      that want it directly.

    Empty defaults instead of None so callers using ``str.format(**ctx)``
    never KeyError on a placeholder they reference.
    """
    if not isinstance(network, dict):
        return {"ssh_prefix": "", "ssh_target_label": "evolve-pod", "admin_user": ""}

    admin_user = str(network.get("admin_user") or "")
    pod = network.get("pod") or {}
    explicit = (pod.get("ssh_target") if isinstance(pod, dict) else None)

    if isinstance(explicit, str):
        # Explicit empty string means "I'm at the deploy box, no SSH" — honor it.
        ssh_prefix = f"ssh {explicit} " if explicit else ""
        ssh_target_label = explicit or "deploy-box"
    else:
        host = ""
        admin_url = network.get("adminBaseUrl") or ""
        if isinstance(admin_url, str) and admin_url:
            import re
            m = re.match(r"https?://([^/:]+)", admin_url)
            if m:
                host = m.group(1)
        if admin_user and host:
            ssh_prefix = f"ssh {admin_user}@{host} "
            ssh_target_label = f"{admin_user}@{host}"
        else:
            ssh_prefix = ""
            ssh_target_label = "evolve-pod"
    return {
        "ssh_prefix": ssh_prefix,
        "ssh_target_label": ssh_target_label,
        "admin_user": admin_user,
    }


def resolve_admin_base_url(network: "dict[str, Any] | None" = None) -> str:
    """Return the admin server's external base URL with no trailing slash.

    The single source of truth for any operator-facing URL that points at
    the admin UI (alert messages, setup-wizard output, README hints,
    push-notification deep links). Callers must not build their own
    URL — pass through this helper so the next config change is one edit.

    Resolution order (mirrors ``resolve_pod_context`` style):

      1. ``EVOLVE_ADMIN_BASE_URL`` env var — explicit operator override,
         useful for dev tunneling and CI fixtures.
      2. ``network.adminBaseUrl`` from network.json — the canonical
         field the setup wizard writes.
      3. Derived default: ``http://<hostname>:5050`` where ``<hostname>``
         is the short form of ``socket.gethostname()`` (matches what the
         wizard prints for the laptop-side SSH tunnel one-liner), or
         ``localhost`` if hostname lookup fails.

    Scheme is preserved verbatim from the configured value — this helper
    does NOT force ``https://``. The HTTPS-on-LAN flip happens in a
    separate phase (PWA spec §4.1); callers should not assume scheme.
    """
    env_override = os.environ.get("EVOLVE_ADMIN_BASE_URL", "").strip()
    if env_override:
        return env_override.rstrip("/")

    if isinstance(network, dict):
        configured = network.get("adminBaseUrl")
        if isinstance(configured, str) and configured.strip():
            return configured.strip().rstrip("/")

    try:
        import socket
        hostname = socket.gethostname().split(".")[0] or "localhost"
    except OSError:
        hostname = "localhost"
    return f"http://{hostname}:5050"


def validate_peers(raw: Any) -> "tuple[list[str], list[dict[str, str]]]":
    """Validate + normalize a ``network.json::peers`` value (multi-pod M2).

    The hub's peers list is the *links-only* sibling registry: each entry
    is ``{name, adminBaseUrl}`` and **nothing else** — names + URLs, never
    tokens (the hard v1 invariant; review checks this). See
    docs/design-multi-pod-2026-06-11.md §3.1(a).

    Returns ``(errors, cleaned)``:

      * ``errors`` — human-readable strings, one per malformed entry (with
        its index). Empty when every entry validates.
      * ``cleaned`` — the valid entries as ``{name, adminBaseUrl}`` dicts,
        ``adminBaseUrl`` with any trailing slash stripped (the same resolved
        shape :func:`resolve_admin_base_url` emits — scheme+host[:port]).

    A non-list ``raw`` (or absent ⇒ ``None``) yields ``([], [])`` — single-pod
    is the valid empty case, not an error. Invalid *entries* are reported in
    ``errors`` and dropped from ``cleaned`` so a switcher consumer never
    blocks on a malformed row (links-only, degrade gracefully).
    """
    from urllib.parse import urlsplit

    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [f"peers must be a list, got {type(raw).__name__}"], []

    errors: list[str] = []
    cleaned: list[dict[str, str]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            errors.append(f"peers[{i}] must be an object")
            continue
        # Reject any field beyond {name, adminBaseUrl} — the v1 invariant is
        # that peers carry NO tokens / extra payload. Surfacing the stray key
        # as an error keeps a future M3-shaped entry from silently riding in.
        extra = set(entry) - {"name", "adminBaseUrl"}
        if extra:
            errors.append(
                f"peers[{i}] has unexpected keys {sorted(extra)} "
                "(v1 entries are {name, adminBaseUrl} only — no tokens)"
            )
            continue
        name = entry.get("name")
        url = entry.get("adminBaseUrl")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"peers[{i}] missing non-empty 'name'")
            continue
        if not isinstance(url, str) or not url.strip():
            errors.append(f"peers[{i}] ('{name}') missing non-empty 'adminBaseUrl'")
            continue
        parsed = urlsplit(url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            errors.append(
                f"peers[{i}] ('{name}') adminBaseUrl must be an http(s) URL: {url!r}"
            )
            continue
        cleaned.append({"name": name.strip(), "adminBaseUrl": url.strip().rstrip("/")})
    return errors, cleaned


def resolve_peers(network: "dict[str, Any] | None") -> "list[dict[str, str]]":
    """Return the valid sibling-pod entries from a loaded network config.

    Convenience over :func:`validate_peers` for read paths (the
    ``/api/peers`` route): drops malformed entries silently so the switcher
    renders whatever is well-formed and never blocks on a bad row. Callers
    wanting to *report* the malformed entries use :func:`validate_peers`.
    """
    if not isinstance(network, dict):
        return []
    _errors, cleaned = validate_peers(network.get("peers"))
    return cleaned


def resolve_pod_host(network: "dict[str, Any] | None" = None) -> tuple[str, str]:
    """Return ``(scheme, host)`` for the pod's external URL emitter.

    Sibling to :func:`resolve_admin_base_url`. Preserves the URL scheme
    from the configured admin base URL instead of guessing it from host
    shape. Used by handover and audit-poller URL builders so deep links
    match whatever scheme the operator configured in ``adminBaseUrl``.

    Resolution order:

      1. ``network.pod.public_host`` — operator override.
      2. ``network.tunnel.remote_host`` — operator override.
      3. Scheme + netloc parsed from :func:`resolve_admin_base_url`.
      4. ``("http", "localhost:5050")`` last-resort fallback.

    For (1) and (2): a bare hostname (no ``://``) inherits the scheme
    from :func:`resolve_admin_base_url`. A value that includes a scheme
    (e.g. ``"https://override.example.com"``) uses that scheme verbatim.
    See ``docs/spec-pwa-phase0-https-2026-05-18.md`` §3.3.
    """
    from urllib.parse import urlsplit

    base = resolve_admin_base_url(network if isinstance(network, dict) else None)
    base_parsed = urlsplit(base)
    base_scheme = base_parsed.scheme or "http"
    base_netloc = base_parsed.netloc or "localhost:5050"

    def _from_override(value: str) -> tuple[str, str]:
        value = value.strip().rstrip("/")
        if "://" in value:
            parsed = urlsplit(value)
            return parsed.scheme or base_scheme, parsed.netloc or value
        return base_scheme, value

    if isinstance(network, dict):
        pod = network.get("pod") or {}
        if isinstance(pod, dict):
            explicit = pod.get("public_host")
            if isinstance(explicit, str) and explicit.strip():
                return _from_override(explicit)
        tunnel = network.get("tunnel") or {}
        if isinstance(tunnel, dict):
            remote = tunnel.get("remote_host")
            if isinstance(remote, str) and remote.strip():
                return _from_override(remote)

    return base_scheme, base_netloc


def resolve_repo_url(network: "dict[str, Any] | None" = None) -> str:
    """Return the deploy repo's git remote URL.

    Resolution order:
      1. ``network.pod.repo_url`` if set in network.json
      2. ``git remote get-url origin`` from the deploy checkout
         (``/Users/Shared/evolve-repo`` by convention)
      3. ``""`` as last-resort fallback — the caller decides what to do
         (typically degrade gracefully: skip a URL in an info message,
         or fall back to ``"your repo"`` in instructions)

    No hardcoded ``evolve-ops/evolve`` — that was the dev fork. Different
    pod installs run from different forks (e.g. ``evolve-ops/evolve``);
    every operator-facing URL needs to reflect the actual remote.
    """
    if isinstance(network, dict):
        pod = network.get("pod") or {}
        if isinstance(pod, dict):
            url = pod.get("repo_url")
            if isinstance(url, str) and url:
                return url
    # Fallback: ask git on the deploy checkout.
    #
    # ``safe.directory`` mirrors repo_puller._git: the durable deploy checkout
    # is owned by the ``evolve`` user, but this resolver runs as root inside
    # ``sudo evolve-admin setup`` — git's CVE-2022-24765 guard then refuses
    # ``remote get-url`` with "detected dubious ownership" and exits non-zero,
    # so the probe returned ``""`` and the wizard's Step-14 check fired a FALSE
    # "no 'origin' remote" on a correctly-cloned, deploy-key-authed VPS while
    # the puller (which runs AS evolve) worked fine (live evolve-vsp-pod,
    # FIND-D). Setting safe.directory to the queried path is the same single,
    # explicit repo the puller trusts — not arbitrary user-owned repos.
    import subprocess
    from platform_profile import get_profile
    for candidate in (get_profile().deploy_checkout_default, str(Path(__file__).resolve().parents[3])):
        try:
            r = subprocess.run(
                ["git", "-c", f"safe.directory={candidate}",
                 "-C", candidate, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode == 0:
                url = r.stdout.strip()
                if url:
                    # Normalize for operator-facing display: SSH form
                    # becomes HTTPS, and any trailing ``.git`` is dropped
                    # so clickable links in messages don't include the
                    # bare suffix (looks ugly and isn't needed for the
                    # ``/settings/keys/new`` URL we append in instructions).
                    if url.startswith("git@github.com:"):
                        path = url[len("git@github.com:"):].rstrip("/")
                        if path.endswith(".git"):
                            path = path[:-len(".git")]
                        return f"https://github.com/{path}"
                    if url.endswith(".git"):
                        url = url[:-len(".git")]
                    return url
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
    return ""
