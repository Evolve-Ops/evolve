"""Evo OC-native tool registry.

This package is the source of truth for the tools evo (the bot) can invoke
via OpenClaw tool-use. Each tool is a Python adapter that wraps existing
Evolve code (signal store, arbiter, deploy actions, etc.) and exposes it
through OC's standard tool-use protocol.

Architecture (per ``docs/spec-evo-oc-native-2026-05-19.md``):

* Tools are the **direct-access** surface for evo — admin UI proxy + the
  evolve bot's own Telegram channel. The indirect cross-bot keyword path
  (member bots' ``evo X`` plugin dispatcher) historically did NOT route
  through this tool registry; once the Phase 2 authorization framework
  (``authorization.py``) lands, it CAN safely route through here for the
  subset of tools opened to non-admin callers via
  ``authorization_scope = "user" | "anyone"``. The framework's default
  remains ``admin`` — every existing tool stays gated to admin callers
  until explicitly opened.

* Each tool declares its **risk tier**: ``read | write_safe | write_risky
  | destructive``. The admin-UI proxy uses the tier plus the operator's
  current authority setting to decide whether to execute silently or
  render a confirmation button. ``read`` tools never need confirmation.

* Each tool also declares its **authorization scope**: ``admin | user |
  anyone``. The MCP dispatcher uses the scope plus the caller's
  ``CallerIdentity`` (admin_ui / telegram_operator / cross_bot_member) to
  decide whether to invoke the handler or return a refusal envelope. See
  ``evolve_admin.evo.tools.authorization`` for the framework + the gate.

* Action tools (anything not ``read``) MUST expose a ``validate(**args)``
  function that confirms the action is currently possible — bot exists,
  signal still firing, proposal still pending, etc. This is the third
  gate in the spec's §5.2 button-rendering flow: if validate fails before
  a confirmation button renders, the operator never sees a button that
  would have errored. Read tools have no validate (they just return data,
  errors surface to the model, model recovers in prose).

* Tool definitions are also rendered into ``TOOLS.md`` (per spec §3.4 +
  §4.3) so the file describing the tool surface matches the running
  registry. The generator lives in ``evolve_admin.evo.tools.tools_md``
  and reads from this registry.

Registration shape: each tool module imports ``register`` from this
package and registers a ``Tool`` at import time. ``build_tool_manifest()``
then renders the registry into the JSON shape OC expects under
``agents.defaults.tools.custom`` in evo's openclaw.json. The
``evolve-admin sync-evo-tools`` CLI command (added in a follow-on PR)
writes the manifest into evo's config and triggers a gateway reload.

This file is Phase 1.0 of the OC-native rebuild. Phase 1.1+ adds more
tools; Phase 2 wires the manifest into openclaw.json; Phase 4 wires
the admin UI proxy to invoke tools via OC tool-use.

Memory references:
* ``project_evo_oc_native_architecture`` — overall direction
* ``feedback_prelaunch_architect_properly`` — build it right
* ``project_alerts_signal_store`` — signal store conventions
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal


# Phase 2 authorization framework — coarse-grained tier the dispatcher uses to
# decide whether a non-admin caller (cross-bot member) can invoke a tool.
#
# * ``admin``  — admin-only. Pod-wide state changes, security/keystore writes,
#                deploys, audit mutes, plugin installs, host commands. The
#                default for every tool; only flipped to ``user``/``anyone``
#                with deliberate per-tool judgment in a follow-up PR.
# * ``user``   — per-operator preference scoped to one human's view (their
#                own notification subscriptions, their own pinned/snoozed
#                bookmarks). Non-admin callers may invoke these.
# * ``anyone`` — read-only queries with no PII / no secret exposure (status,
#                signal listings, available-action introspection). Even an
#                unprivileged cross-bot caller may invoke.
#
# Defaults are conservative: every existing tool stays admin-only until
# explicitly opened. See ``docs/audit-evo-tool-coverage-2026-06-02.md``
# §Authorization-tier classification for the audit that motivated this. See
# also memory notes ``feedback_ui_authorization_presumed`` and
# ``project_evo_oc_native_architecture``.
AuthorizationScope = Literal["admin", "user", "anyone"]


class RiskTier(str, Enum):
    """How dangerous is invoking this tool?

    * ``read`` — pure reads of pod state / config. No side effects.
      Always executes silently regardless of the operator's authority
      tier; the model gets the data and continues.

    * ``write_safe`` — small reversible writes: snooze/dismiss a signal,
      log an investigation, queue an app-audit. The blast radius is
      bounded and any wrong move is trivially undoable. Auto-executes
      under ``auto-small`` and ``auto`` authority tiers; renders a
      confirm button under ``ask``.

    * ``write_risky`` — changes that touch code, configs, or
      provisioned resources: redeploy a bot, restart a gateway, install
      a gallery app, accept a proposal. Auto-executes only under
      ``auto`` authority; otherwise renders a confirm button.

    * ``destructive`` — irreversible or hard-to-reverse: remove a bot,
      reject a proposal terminally, security-baseline reset. ALWAYS
      renders a confirm button regardless of authority tier. The
      operator must explicitly click confirm.

    Annotation lives on the Tool itself, not on the authority tier — so
    the same authority setting maps to different gates per tool, not
    per category. Adding a new tool means choosing its tier honestly.
    """

    READ = "read"
    WRITE_SAFE = "write_safe"
    WRITE_RISKY = "write_risky"
    DESTRUCTIVE = "destructive"


# Validate functions return this shape. ``ok`` is True if the action can
# proceed now; ``reason`` is operator-facing prose when ok is False (the
# proxy shows it in evo's reply instead of rendering a button that would
# have errored). Convention chosen over a custom exception to keep
# validate stateless and easy to compose.
ValidateResult = dict[str, Any]   # {"ok": bool, "reason"?: str, "context"?: dict}


@dataclass(frozen=True)
class Tool:
    """One tool evo can invoke via OC tool-use.

    ``handler`` is a Python callable accepting kwargs that match
    ``input_schema``'s top-level properties. It returns a JSON-serializable
    dict; the proxy passes that dict back to the OC gateway as the
    tool-result.

    ``validate`` (required for non-``read`` tiers) is a Python callable
    with the same signature as ``handler`` but returning a
    ``ValidateResult`` — the proxy calls it before showing a confirmation
    button. Read-tier tools must NOT define validate.

    Handlers may accept any kwargs, but must declare an ``Injected``
    annotation (TBD — for now we pass shared_dir / network_path
    explicitly via partial application at registration time; that path
    keeps the model-facing signature clean while letting Python tools
    see runtime context).
    """

    name: str                      # dotted, e.g. "pod_state.signals.firing"
    description: str               # one-paragraph; goes into the OC tool definition + TOOLS.md
    input_schema: dict             # JSON schema for the args the model sends
    handler: Callable[..., dict]   # **args -> dict
    risk_tier: RiskTier
    validate: Callable[..., ValidateResult] | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)   # for grouping in TOOLS.md
    # Phase 2 authorization framework. Conservative default = "admin" so
    # every existing tool stays gated for non-admin callers (cross-bot
    # members) until a follow-up PR explicitly opens it. ``risk_tier``
    # answers "how bad if this misfires?"; ``authorization_scope``
    # answers "who is allowed to invoke it?" — independent axes that
    # both feed into the dispatcher's decision.
    authorization_scope: AuthorizationScope = "admin"

    def __post_init__(self) -> None:
        # Validate the validate contract on construction so the wrong
        # shape can't slip into the registry.
        if self.risk_tier == RiskTier.READ and self.validate is not None:
            raise ValueError(
                f"Tool '{self.name}': read-tier tools must not define validate "
                f"(reads always execute; nothing to gate)."
            )
        if self.risk_tier != RiskTier.READ and self.validate is None:
            raise ValueError(
                f"Tool '{self.name}': non-read tier '{self.risk_tier.value}' "
                f"requires a validate() function. The proxy uses validate to "
                f"avoid rendering a button that would fail at execution time."
            )
        if self.authorization_scope not in ("admin", "user", "anyone"):
            raise ValueError(
                f"Tool '{self.name}': authorization_scope must be one of "
                f"'admin', 'user', 'anyone'; got {self.authorization_scope!r}."
            )


# Module-level registry. Mutated by ``register()`` at import time of each
# tool module. Tuples are used at read time (``all_tools()``) so callers
# can't accidentally mutate the registry from outside.
_REGISTRY: list[Tool] = []


def register(tool: Tool) -> None:
    """Add a tool to the registry, replacing any prior entry with the
    same name. Called at import time from each tool's module.

    Idempotent on name — registering a Tool with name X overwrites any
    previously-registered Tool with name X. This makes re-importing
    modules during tests safe.
    """
    _REGISTRY[:] = [t for t in _REGISTRY if t.name != tool.name] + [tool]


def all_tools() -> tuple[Tool, ...]:
    """Snapshot of the current registry. Tuple so callers can iterate
    safely without worrying about concurrent registrations."""
    return tuple(_REGISTRY)


def lookup(name: str) -> Tool | None:
    """Find a tool by exact name. Returns None if not registered.

    Used by the admin-UI proxy to map an OC tool-call back to a
    Python handler. Also used by the OC tool-use validator and by
    tests."""
    for t in _REGISTRY:
        if t.name == name:
            return t
    return None


def tools_by_tier(tier: RiskTier) -> tuple[Tool, ...]:
    """All registered tools at a given risk tier. Used by the proxy's
    authority gate when deciding whether to auto-execute or render a
    confirm button."""
    return tuple(t for t in _REGISTRY if t.risk_tier == tier)


def build_tool_manifest() -> list[dict]:
    """Render the registry as a list of OC custom-tool definitions,
    suitable for writing into evo's openclaw.json under
    ``agents.defaults.tools.custom``.

    Each entry has the shape OC's tool-use protocol expects:

        {
          "name": "pod_state.signals.firing",
          "description": "...",
          "input_schema": {...JSON schema...},
        }

    Risk-tier and validate are Evolve-side concerns and don't appear in
    the OC-facing manifest — the OC gateway just needs to know the tool
    exists and what args to send. The proxy (Phase 4) does the
    tier-aware gating between OC and the tool's Python handler.

    The exact OC wire format is verified against
    ``docs.openclaw.ai/gateway/config-tools.md`` and adjusted here when
    OC's schema changes. Keeping the renderer in one place means tool
    modules don't have to know OC's schema.
    """
    return [_render_for_oc(t) for t in _REGISTRY]


def _render_for_oc(tool: Tool) -> dict:
    """Render one Tool in OC's custom-tools JSON shape.

    Single point where OC's schema is encoded. If OC adds/renames
    fields in a future release, this is the only function to update.
    """
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }


# Import side-effects: registering the built-in tools. Modules are
# imported in dependency order — read tools first (no inter-tool deps),
# action tools after. Each module calls ``register(...)`` at import time.
#
# When adding a new tool: create a new module under this package,
# implement the Tool, call register() at module scope, and add the
# import below. The tool ships in the next ``evolve-admin sync-evo-tools``
# pass.

from . import pod_state_signals    # noqa: E402, F401  -- registers pod_state.signals.{firing,history}
from . import pod_state_proposals  # noqa: E402, F401  -- registers pod_state.proposals.{pending,snoozed}
from . import pod_state_bots       # noqa: E402, F401  -- registers pod_state.bots
from . import pod_state_host       # noqa: E402, F401  -- registers pod_state.host
from . import pod_state_audit      # noqa: E402, F401  -- registers pod_state.audit
from . import pod_state_usage      # noqa: E402, F401  -- registers pod_state.usage (Phase 1.5d)
from . import pod_state_errors     # noqa: E402, F401  -- registers pod_state.errors (Phase 1.5d)
from . import pod_state_rollbacks  # noqa: E402, F401  -- registers pod_state.rollbacks (Phase 1.5d)
from . import pod_state_app_scan_status  # noqa: E402, F401  -- registers pod_state.app_scan_status (diagnoses the scan_needed tile chip)
from . import pod_state_backup     # noqa: E402, F401  -- registers pod_state.backup_status (paired with action.bot.backup_workspace)
from . import pod_state_config_drift  # noqa: E402, F401  -- registers pod_state.config_drift (Security → Backups subtab enumeration)
from . import pod_state_home_narrative  # noqa: E402, F401  -- registers pod_state.home_narrative (Home-page narrative banner cache; sibling of pod_state.signals.firing — see diagnosis-evo-briefing-context-gap-2026-05-26)
from . import pod_state_subscriptions  # noqa: E402, F401  -- registers pod_state.subscriptions (operator notification subscriptions; closes hallucination gap diagnosed in diagnosis-evo-subscription-awareness-2026-06-02)
from . import config_bot           # noqa: E402, F401  -- registers config.bot
from . import config_network       # noqa: E402, F401  -- registers config.network
from . import action_signal        # noqa: E402, F401  -- registers action.signal.{snooze,dismiss}
from . import action_proposal      # noqa: E402, F401  -- registers action.proposal.{snooze,reject}
from . import action_proposal_apply  # noqa: E402, F401  -- registers action.proposal.apply (Phase 1.4 linchpin)
from . import action_proposal_refine  # noqa: E402, F401  -- registers action.proposal.refine (wraps /api/arbiter/proposals/<id>/refine)
from . import action_autonomy      # noqa: E402, F401  -- registers action.autonomy.set (autonomy ladder Phase B, spec §3.1 chat front door)
from . import action_dispatch       # noqa: E402, F401  -- registers pod_state.dispatches + action.dispatch.acknowledge (Phase 3.3b target-side polling for evo)
from . import action_cost          # noqa: E402, F401  -- registers action.cost.{set_bot_cap,clear_bot_cap,clear_enforcement}
from . import action_recovery      # noqa: E402, F401  -- registers pod_state.rollback_points + action.bot.{rollback,reverse_rollback}
from . import action_bot           # noqa: E402, F401  -- registers action.bot.{restart,redeploy,remove} (1.4 step 2 + 1.5a)
from . import action_bot_pin_plugin  # noqa: E402, F401  -- registers action.bot.pin_plugin_version (closes the 2026-05-26 unpinned-npm-specs tool gap)
from . import action_bot_auto_memory  # noqa: E402, F401  -- registers action.bot.set_auto_memory (per-bot OC plugins.slots.memory kill-switch)
from . import action_infra         # noqa: E402, F401  -- registers action.infra.daemon_restart (closes infra_daemon_down action gap)
from . import action_app           # noqa: E402, F401  -- registers action.app.{install,audit} + pod_state.forge_job
from . import action_pod           # noqa: E402, F401  -- registers action.pod.{pause_all,resume_all} + pod_state.pause_state (Phase 1.5a)
from . import action_breakers      # noqa: E402, F401  -- registers action.{bot,pod}.{trip,reset}_breaker + pod_state.breakers (circuit breakers Phase 4a)
from . import action_plugin        # noqa: E402, F401  -- registers action.plugin.{enable,disable} (wraps /api/plugins-admin/propose-* endpoints)
from . import action_security      # noqa: E402, F401  -- registers action.security.accept_drift (wraps /api/security/accept-drift)
from . import action_subscriptions  # noqa: E402, F401  -- registers action.subscriptions.set (wraps alerts.subscriptions.write_subscription — closes hallucination gap diagnosed in diagnosis-evo-subscription-awareness-2026-06-02)
from . import action_scan          # noqa: E402, F401  -- registers action.scan.run (umbrella for Pattern A one-shot scan/refresh triggers — closes 8 gaps from docs/audit-evo-tool-coverage-2026-06-02.md)
from . import action_keys          # noqa: E402, F401  -- registers action.keys.{add,rotate,remove} (closes 10 Pattern B gaps from docs/audit-evo-tool-coverage-2026-06-02.md — keystore touches via admin-ui HTTP)
from . import action_alerts        # noqa: E402, F401  -- registers action.subscriptions.{test,reset} + action.alerts.{set_digest_hour,send_test_report} (closes 4 Reports/Alerts gaps from audit-evo-tool-coverage-2026-06-02)
from . import action_errors        # noqa: E402, F401  -- registers action.errors.dismiss (closes Settings → Errors banner-dismiss gap)
from . import action_models        # noqa: E402, F401  -- registers action.models.check_freshness (closes Improve → AI Optimization "Check Now" gap)
from . import action_forge         # noqa: E402, F401  -- registers action.forge.{approve,reject} (closes 2 of 4 Forge gaps; dispatch/cancel deferred)
from . import action_audit         # noqa: E402, F401  -- registers action.audit.{run,mute,unmute} (closes Security → Audit gap from audit-evo-tool-coverage-2026-06-02; mute path = MuteAuditFinding backlog row)
from . import action_feedback      # noqa: E402, F401  -- registers action.feedback.file_issue (closes 2026-06-07 gap: evo on the Feedback page couldn't actually file an issue, only draft text)
from . import action_roster        # noqa: E402, F401  -- registers action.roster.{set_role,block,unblock} + action.channel.set_newcomer_mode (Phase C.1 of user-roster spec — cross-bot admin via evo)
from . import meta_tools           # noqa: E402, F401  -- registers meta.tools (reliability lever #5)
from . import evo_telemetry        # noqa: E402, F401  -- registers action.evo.log_tool_gap + pod_state.tool_gaps (Phase 1.5b §14.3)
from . import pod_state_cost_caps  # noqa: E402, F401  -- registers pod_state.{cost_caps,cost_remediation_status} (Phase 9 of 2026-06 cost-cap normalization)
from . import help_search          # noqa: E402, F401  -- registers evolve_help_search + evolve_help_read (grounds tray how-it-works answers in docs/help via the same BM25 index the /api/evo/help/* routes use)
