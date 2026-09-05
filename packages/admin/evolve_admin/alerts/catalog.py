"""Operator-facing alert catalog — Phase A1.

The catalog is the single source of truth for what operator-facing
events the Evolve install can notify about. Every existing emitter
(post-migration) and every new producer maps to one ``CatalogEvent``
keyed by an ``event_key`` string. The dispatcher consults the catalog
on every send to apply operator subscription preferences and render
the message body + actionability close-out.

See ``internal/spec-alert-subscriptions-2026-05-10.md`` for design.
Phase A1 lands the catalog module + entries + tests; subsequent phases
wire it into the dispatcher (A2), persist operator preferences (A3),
expose API routes (A4), and build the UI (B).
"""

from __future__ import annotations

import html
import string
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .dispatcher import Severity


# ── HTML escaping (Telegram parse_mode=HTML) ────────────────────────────────


def html_escape(value: Any) -> str:
    """HTML-escape an interpolated value for Telegram HTML parse mode.

    Telegram HTML mode treats ``<``, ``>``, ``&`` as special; producers
    must escape them in any *dynamic* content (bot ids, file paths,
    error messages, signal titles, etc.). Catalog ``body_template``s
    themselves are catalog-author-trusted and can contain raw HTML
    tags (``<b>``, ``<i>``, ``<code>``). The pattern is:

      - **template literal text → trusted** (catalog authors control it)
      - **interpolated payload value → untrusted** (escape it)

    This is the boundary helper for the second half: any value spliced
    into a message at render time goes through here first. Re-introduced
    in PR 2/3 of the dispatcher-safety rework — see PR #1393 for
    background on why the previous Markdown-mode setup was unsafe.
    """
    return html.escape("" if value is None else str(value), quote=False)


class _RawHTML(str):
    """Marker wrapper for a string that has already been HTML-escaped
    (or is producer-trusted raw HTML) and should NOT be re-escaped by
    ``_HtmlEscapingFormatter``. Used in ``render_event`` to splice an
    already-rendered ``cli_command`` / ``ui_breadcrumb`` / ``bot_command``
    into ``text_template`` without double-escaping the value (the
    inner-template substitutions were already escaped on the first pass).
    """


class _HtmlEscapingFormatter(string.Formatter):
    """``str.Formatter`` that HTML-escapes each substituted value AFTER
    applying its format spec — except for ``_RawHTML`` values, which
    pass through untouched.

    Why a custom Formatter instead of pre-escaping ``payload``: catalog
    templates use numeric format specs like ``${amount:.2f}``. Pre-
    converting the payload value to an HTML-escaped string loses the
    numeric type — ``"${5.96:.2f}"`` raises ``ValueError`` for an
    already-stringified value. With a Formatter override, ``5.96``
    formats as ``"5.96"`` first, *then* the escape runs on the
    formatted string. Two-step ordering preserves both format
    specifiers and HTML safety.
    """

    def format_field(self, value: Any, format_spec: str) -> str:
        if isinstance(value, _RawHTML):
            # Already escaped (or producer-trusted); inject verbatim.
            return value
        formatted = super().format_field(value, format_spec)
        return html_escape(formatted)


_HTML_FORMATTER = _HtmlEscapingFormatter()


# ── Categories (operator-facing groupings shown in the UI) ──────────────────


class Category(str, Enum):
    SECURITY = "security"
    COST = "cost"
    SYSTEM = "system"
    UPDATES = "updates"
    DECISIONS = "decisions"
    SUMMARIES = "summaries"
    # Messages ABOUT the alerting system itself — storm mode, the digest
    # envelope, the delivery/send-surface health, alert-repeat loops. Kept
    # distinct from SYSTEM (pod/runtime conditions) so an operator can tune
    # the alerting machinery's own chatter separately from real incidents.
    # Spec: internal/spec-subscription-completeness-2026-06-24.md.
    META = "meta"


# Display order for the Subscriptions UI tab — Security first
# (most operator-relevant), Reports last.
CATEGORY_DISPLAY_ORDER: tuple[Category, ...] = (
    Category.SECURITY,
    Category.COST,
    Category.SYSTEM,
    Category.UPDATES,
    Category.DECISIONS,
    Category.SUMMARIES,
    # Last — the alerting system's own chatter, below the substantive
    # incident/decision categories.
    Category.META,
)


# Display label + emoji per category, used by the Subscriptions UI.
CATEGORY_LABELS: dict[Category, tuple[str, str]] = {
    Category.SECURITY:  ("🛡️", "Security"),
    Category.COST:      ("💰", "Cost"),
    Category.SYSTEM:    ("🟢", "System health"),
    Category.UPDATES:   ("🔄", "Updates"),
    Category.DECISIONS: ("📋", "Decisions needed"),
    Category.SUMMARIES: ("📊", "Summaries"),
    Category.META:      ("🔧", "Alerting system"),
}


# ── Frequencies ─────────────────────────────────────────────────────────────


class Frequency(str, Enum):
    """How often (or by what discipline) an event reaches the operator.

    ``ONCE_PER_DAY_MAX`` / ``ONCE_PER_WEEK_MAX`` are deprecated — their
    "first event pushes, rest dropped" semantics is strictly worse than
    a digest (operator only sees one event from a busy day). Retained
    in the enum so saved subscriptions parse, but no catalog entry
    lists them in ``allowed_frequencies`` and they are migrated to
    digest at read time (see ``subscriptions.read_subscription``).

    ``OFF`` is no longer an ``allowed_frequencies`` value either. The
    per-subscription ``enabled`` toggle is the authoritative mute.
    """

    IMMEDIATE         = "immediate"          # push as soon as event fires
    ONCE_PER_DAY_MAX  = "once_per_day_max"   # deprecated → DAILY_DIGEST
    ONCE_PER_WEEK_MAX = "once_per_week_max"  # deprecated → WEEKLY_DIGEST
    DAILY_DIGEST      = "daily_digest"       # aggregated, flushed at digest_hour_local
    WEEKLY_DIGEST     = "weekly_digest"      # aggregated, flushed weekly
    DAILY             = "daily"              # natural daily cadence (e.g. pod_report)
    WEEKLY            = "weekly"             # natural weekly cadence (e.g. weekly_review)
    OFF               = "off"                # deprecated — see enabled flag


# Display label per frequency, used by the Subscriptions UI dropdown.
FREQUENCY_LABELS: dict[Frequency, str] = {
    Frequency.IMMEDIATE:         "Immediate",
    Frequency.ONCE_PER_DAY_MAX:  "Once per day max",
    Frequency.ONCE_PER_WEEK_MAX: "Once per week max",
    Frequency.DAILY_DIGEST:      "Daily digest",
    Frequency.WEEKLY_DIGEST:     "Weekly digest",
    Frequency.DAILY:             "Daily",
    Frequency.WEEKLY:            "Weekly",
    Frequency.OFF:               "Off",
}


# Deprecated → live frequency mapping. ``subscriptions.read_subscription``
# resolves these transparently so existing saved overrides keep working
# without an explicit migration pass.
LEGACY_FREQUENCY_MIGRATION: dict[Frequency, Frequency] = {
    Frequency.ONCE_PER_DAY_MAX:  Frequency.DAILY_DIGEST,
    Frequency.ONCE_PER_WEEK_MAX: Frequency.WEEKLY_DIGEST,
}


# Deprecated → live event-key mapping. When the catalog collapses a
# warn/critical pair into one entry, saved subscriptions referencing
# the dropped sibling key transparently resolve to the surviving key
# via ``subscriptions.read_subscription``. Producers should emit the
# surviving key directly.
LEGACY_KEY_MIGRATION: dict[str, str] = {
    # Collapsed 2026-05-29: warn/critical pairs merged. Severity is
    # now a producer-driven payload field (level_emoji/event_phrase/
    # threshold_clause/trail), so the operator picks one on/off for
    # the whole event class.
    "cost.burst_critical": "cost.burst_detected",
    "cost.heartbeat_session_bloat_critical": "cost.heartbeat_session_bloat",
    # Renamed 2026-06-01: the daily Bot digest was under REPORTS keyed
    # ``reports.adhoc_analyzer`` — internal name that confused operators
    # (it isn't ad-hoc, it's the scheduled daily digest). Consolidated
    # 2026-06-05 into ``summaries.daily_pod_report`` to give the
    # operator a single daily message. Both legacy keys resolve to the
    # surviving pod_report entry.
    "reports.adhoc_analyzer": "summaries.daily_pod_report",
    "summaries.daily_bot_digest": "summaries.daily_pod_report",
    # DELIBERATELY NOT MIGRATED (review.py retirement, 2026-08-14): the two
    # ``producer_source="review"`` rows (``security.proposal_quarantined``,
    # ``decisions.proposal_rejected``) were dropped outright rather than
    # mapped, because this table means "same event class, new key" — the
    # survivor inherits the legacy key's saved override (see
    # ``subscriptions.read_subscription``, the ``if not overrides`` branch).
    # The retired reviewer has no same-class survivor: a proposal the
    # security screen denies now routes to human review and surfaces via
    # ``decisions.proposal_ready`` (a different category, a different
    # operator Subscription), so grafting a stale ``security_findings``
    # override onto it would silently re-point the operator's preference at
    # an unrelated event. Inert beats mis-aimed. The operator-facing
    # ``security_findings`` group is unaffected — it keeps its live members
    # (five since security.audit_critical landed 2026-09-01).
}


# ── Action offer ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ActionOffer:
    """The actionability close-out at the bottom of a notification.

    Three modes, picked by the catalog entry. The renderer formats the
    mode-specific field (with payload substitution) and then formats
    ``text_template`` with the result.

    Mode priority when designing entries: ``bot`` > ``cli`` > ``ui``
    (operator effort, low to high). Entries that don't have a known
    fix omit the action entirely (use ``None``).
    """

    mode: str  # "bot" | "ui" | "cli"
    text_template: str
    bot_command: str | None = None
    ui_breadcrumb: str | None = None
    cli_command: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in ("bot", "ui", "cli"):
            raise ValueError(f"ActionOffer.mode must be bot/ui/cli, got {self.mode!r}")
        required = {"bot": "bot_command", "ui": "ui_breadcrumb", "cli": "cli_command"}
        field_name = required[self.mode]
        if not getattr(self, field_name):
            raise ValueError(
                f"ActionOffer mode={self.mode!r} requires {field_name!r}"
            )


# Default text templates per mode — entries usually inherit these and
# only override the mode-specific field. Catalog entries can override
# text_template directly when wording needs to vary.
DEFAULT_BOT_TEMPLATE = "Reply '{bot_command}' and I'll take care of it."
DEFAULT_UI_TEMPLATE  = "Open admin UI → {ui_breadcrumb}."
DEFAULT_CLI_TEMPLATE = "Run: <code>{cli_command}</code>"


def bot_action(command: str, *, text_template: str = DEFAULT_BOT_TEMPLATE) -> ActionOffer:
    return ActionOffer(mode="bot", text_template=text_template, bot_command=command)


def ui_action(breadcrumb: str, *, text_template: str = DEFAULT_UI_TEMPLATE) -> ActionOffer:
    return ActionOffer(mode="ui", text_template=text_template, ui_breadcrumb=breadcrumb)


def cli_action(command: str, *, text_template: str = DEFAULT_CLI_TEMPLATE) -> ActionOffer:
    return ActionOffer(mode="cli", text_template=text_template, cli_command=command)


# ── Catalog event ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CatalogEvent:
    """One operator-facing event type. Keyed by ``key`` (e.g.
    ``"cost.daily_threshold"``). Mapped to a producer (existing dispatcher
    source name). Renders to a body + optional action close-out.
    """

    key: str
    category: Category
    label: str
    description: str
    default_enabled: bool
    default_frequency: Frequency
    allowed_frequencies: tuple[Frequency, ...]
    severity: Severity
    producer_source: str
    body_template: str
    # Operator-facing Subscription membership (Phase 2 grouping layer).
    # The snake_case id of the OperatorSubscription this event belongs to
    # (see subscriptions_catalog.SUBSCRIPTIONS), or ``None`` for the
    # internal/removed events that are NOT operator subscriptions
    # (proposal_rejected, proposal_outcome_checkin, briefing_activated,
    # proposal_applied, and the meta.* senders once they land). Gating is
    # per-Subscription: a message's enabled state is resolved through this
    # id. An event with ``subscription=None`` is not group-gated — it
    # preserves its current dispatch behavior (internal safety-net) and is
    # hidden from the Configure UI. Required keyword (no default) so a new
    # catalog entry can't silently ship un-classified; the totality ratchet
    # (test_alerts_subscription_groups) additionally validates the id.
    subscription: str | None
    # Sample payload used by the Subscriptions UI's "Test" button. Must
    # contain every {placeholder} the body_template / action references.
    # Kept on the catalog entry so previews are deterministic and the
    # operator sees a realistic message before subscribing.
    sample_payload: dict[str, Any] = field(default_factory=dict)
    action: ActionOffer | None = None
    is_safety_critical: bool = False
    # Whether this event reaches the operator's CHAT channel at all.
    # ``True`` (default) preserves the normal dispatch path — the message
    # is rendered and pushed (subject to the operator's subscription /
    # frequency / cooldown gates). ``False`` is the "produce-but-don't-
    # notify" switch for the events that were internalized in the
    # subscription-completeness Phase-2 cleanup
    # (spec-subscription-completeness-2026-06-24.md, "Removed/internalized"):
    # the Signal still fires and any internal record it feeds still gets
    # written, but the dispatcher suppresses the standalone CHAT push.
    # Used only on ``subscription=None`` (internal/hidden-from-Configure)
    # events; the gate is reversible (flip back to True) without touching
    # producers. The dispatcher consults this via
    # ``catalog.notify_for(event_key)`` before the chat send. Note the two
    # internal-but-still-delivering events (``meta.digest`` — the digest
    # envelope IS the bundle delivery; ``meta.unclassified`` — the loud
    # safety-net fallback for an unmapped signal) deliberately keep
    # ``notify=True``.
    notify: bool = True
    # M2 honest-lifecycle flag (spec-proactive-delivery-monitor-2026-06-10
    # §9.1). For Signals mapped to this event, signal_notifier announces a
    # resolve even when the fire was never pushed (it healed inside the
    # debounce window) — rendered from the Signal's ``details.recovery``,
    # exactly once per signal id. The same flag opts the event into ONE
    # re-announce when an already-announced Signal escalates to alert
    # severity (the §9.2 "restart didn't work" follow-up — that promise
    # is binding, so the escalation can't stay chat-silent). Default off:
    # every other event keeps the orphan-recovery suppression and the
    # escalate-without-re-paging behavior.
    announce_unannounced_resolve: bool = False

    def __post_init__(self) -> None:
        # key must match category prefix
        prefix = f"{self.category.value}."
        if not self.key.startswith(prefix):
            raise ValueError(
                f"key {self.key!r} must start with {prefix!r} (category.value + '.')"
            )
        # default_frequency must be allowed
        if self.default_frequency not in self.allowed_frequencies:
            raise ValueError(
                f"event {self.key}: default_frequency={self.default_frequency} "
                f"not in allowed_frequencies={self.allowed_frequencies}"
            )
        # OFF / legacy "max" frequencies must NEVER appear in
        # allowed_frequencies. Muting goes through the enabled flag;
        # the "max" frequencies are migrated to DAILY_DIGEST/WEEKLY_DIGEST
        # transparently at read time.
        for f in (Frequency.OFF,
                  Frequency.ONCE_PER_DAY_MAX,
                  Frequency.ONCE_PER_WEEK_MAX):
            if f in self.allowed_frequencies:
                raise ValueError(
                    f"event {self.key}: Frequency.{f.name} is deprecated "
                    f"and must not appear in allowed_frequencies"
                )
        # subscription (if set) must reference a known operator Subscription.
        # None is allowed (internal/removed events that are not operator
        # subscriptions). A typo'd id is a hard error so it can't ship.
        if self.subscription is not None:
            from .subscriptions_catalog import SUBSCRIPTION_IDS
            if self.subscription not in SUBSCRIPTION_IDS:
                raise ValueError(
                    f"event {self.key}: subscription={self.subscription!r} is "
                    f"not a known operator Subscription id "
                    f"(see subscriptions_catalog.SUBSCRIPTIONS)"
                )


# ── Renderer ────────────────────────────────────────────────────────────────


def render_event(event: CatalogEvent, payload: dict[str, Any]) -> str:
    """Format a catalog event into its final operator-facing message.

    Substitutes ``{placeholder}`` keys in ``body_template`` with values
    from ``payload``. If the event has an action, also formats the
    mode-specific field (substituting from payload) and appends the
    action line on a new line.

    Every rendered message ends with a subscription-id footer:
    ``\\n\\nsubscription: <event.key>``. The operator can grep for this
    key on the Subscriptions configure tab to find the exact toggle
    (frequency / on-off) that produced the message — without it, there's
    no way to navigate from a chat message back to the setting it came
    from.

    **HTML safety** — every payload value is HTML-escaped before
    substitution so the resulting message is safe to send with
    ``parse_mode="HTML"``. Catalog ``body_template``s themselves are
    trusted (authored by us) and may contain raw HTML tags like
    ``<b>``, ``<i>``, ``<code>``. Interpolated values from payload
    (which can include arbitrary user/bot data) are escaped — a
    payload of ``{"bot_id": "<script>"}`` renders as ``&lt;script&gt;``,
    not as a script tag.
    """
    body = _HTML_FORMATTER.vformat(event.body_template, (), payload)
    if event.action is not None:
        # Resolve the mode-specific field first, then format the
        # text_template around it. Mode-specific templates (cli_command,
        # bot_command, ui_breadcrumb) are catalog-author-trusted and
        # may contain HTML tags; only their ``{placeholder}``
        # substitutions get HTML-escaped.
        #
        # Two-stage: stage 1 produces a *resolved* mode value that
        # already has its dynamic payload escaped. Stage 2 inserts that
        # resolved value into text_template, where the resolved value
        # is then injected raw (it's been pre-escaped + catalog text
        # is trusted). To do that we add the resolved value via a
        # standard format pass with the field already-safe.
        ctx = dict(payload)
        if event.action.mode == "cli":
            assert event.action.cli_command is not None
            resolved = _HTML_FORMATTER.vformat(
                event.action.cli_command, (), payload
            )
            ctx["cli_command"] = _RawHTML(resolved)
        elif event.action.mode == "ui":
            assert event.action.ui_breadcrumb is not None
            resolved = _HTML_FORMATTER.vformat(
                event.action.ui_breadcrumb, (), payload
            )
            ctx["ui_breadcrumb"] = _RawHTML(resolved)
        elif event.action.mode == "bot":
            assert event.action.bot_command is not None
            resolved = _HTML_FORMATTER.vformat(
                event.action.bot_command, (), payload
            )
            ctx["bot_command"] = _RawHTML(resolved)
        action_line = _HTML_FORMATTER.vformat(event.action.text_template, (), ctx)
        body = f"{body}\n{action_line}"
    return f"{body}\n\n{_subscription_footer(event)}"


# Public so producers that bypass render_event (legacy ``message=``
# callers) can still append a consistent footer if they want one.
def subscription_footer(event_key: str) -> str:
    """Return the standard "subscription: <key>" footer.

    Single source of truth for the footer format. Kept short so it
    doesn't crowd the message body; the prefix ``subscription:`` is the
    grep anchor operators use on the Subscriptions configure tab.
    """
    return f"subscription: {event_key}"


def _subscription_footer(event: "CatalogEvent") -> str:
    return subscription_footer(event.key)


# ── Catalog ─────────────────────────────────────────────────────────────────


# Frequency presets per typical event shape — kept as constants so the
# catalog reads cleanly. Edit a preset when an entire class of events
# should change behavior; edit per-entry when one event differs.
#
# Single-option presets render as static text in the UI (no dropdown);
# multi-option presets render as a dropdown.
_F_IMMEDIATE_ONLY    = (Frequency.IMMEDIATE,)
_F_IMMEDIATE_OR_DAILY_DIGEST = (Frequency.IMMEDIATE, Frequency.DAILY_DIGEST)
_F_IMMEDIATE_OR_WEEKLY_DIGEST = (Frequency.IMMEDIATE, Frequency.WEEKLY_DIGEST)
_F_WEEKLY_DIGEST_ONLY = (Frequency.WEEKLY_DIGEST,)
_F_NATURAL_WEEKLY    = (Frequency.WEEKLY,)
_F_NATURAL_DAILY     = (Frequency.DAILY,)


# ── default_frequency derivation (workstream D1) ────────────────────────────
# Spec: internal/spec-subscription-digest-default-2026-06-28.md.
#
# Motivation: on 2026-06-28 the evo-vps pod pushed ~40 notifications to the
# operator, of which one was actionable; the rest were benign, flap-prone
# events (security.audit_finding, security.config_drift) and meta.unclassified
# junk that the catalog DEFAULTED to IMMEDIATE. The digest machinery already
# existed — the leak was the loud default. We now reserve IMMEDIATE for
# must-act events and default everything else to the operator's daily digest.
#
# default_frequency is DERIVED from the single rule below rather than hand-
# picked per entry; ``test_default_frequency_matches_derivation_rule`` pins
# every catalog key so a new event can't silently re-introduce a loud default.
# The operator can still raise any event back to IMMEDIATE per-event (every
# digest-default event keeps IMMEDIATE in its allowed_frequencies), and an
# event that should page for everyone earns a slot in MUST_PAGE_ALLOWLIST.

# Curated set of events that must page immediately even though their severity
# is below CRITICAL and they are not flagged is_safety_critical — the
# enforcement / liveness transitions an operator genuinely needs in the
# moment. Kept small: an event earns a slot only when a deferred digest would
# cause real harm (a gateway down, a hard cap or breaker firing).
#
# NOTE: the genuine-unauthorized-tamper class from the drift-taxonomy L2 work
# will be added here when it lands. Until then security.config_drift is a
# benign-flap event (it fires on routine out-of-deploy edits) and digests like
# the rest; the tamper variant is what will warrant an immediate page.
MUST_PAGE_ALLOWLIST: frozenset[str] = frozenset({
    "system.gateway_state_change",
    "system.gateway_autorestart_failed",
    "cost.hard_cap_hit",
    "cost.breaker_tripped",
    "cost.gateway_stopped",
})


def derive_default_frequency(
    *,
    key: str,
    severity: Severity,
    is_safety_critical: bool,
    allowed_frequencies: tuple[Frequency, ...],
) -> Frequency:
    """The single rule deciding an event's default delivery cadence.

    IMMEDIATE iff the event is must-act — ``severity == CRITICAL``, flagged
    ``is_safety_critical``, or listed in ``MUST_PAGE_ALLOWLIST``. Otherwise
    the event defaults to the quietest cadence it permits:

      * a single-option event keeps that option — this is where the natural
        cadences fall out unchanged: the daily pod report (DAILY), the weekly
        reviews (WEEKLY), the weekly-digest-only update notices
        (WEEKLY_DIGEST). SUMMARIES keep their natural summary cadence for
        exactly this reason;
      * else DAILY_DIGEST when allowed — the flood default;
      * else WEEKLY_DIGEST when allowed — immediate-or-weekly update notices
        settle into the weekly bundle rather than pushing;
      * else IMMEDIATE — the event declared itself immediate-only
        (``allowed_frequencies == (IMMEDIATE,)``) and cannot be deferred.

    The returned value is always a member of ``allowed_frequencies``, so it
    satisfies ``CatalogEvent.__post_init__``'s default-in-allowed invariant
    (must-act events all keep IMMEDIATE in their allowed set).
    """
    if (
        severity == Severity.CRITICAL
        or is_safety_critical
        or key in MUST_PAGE_ALLOWLIST
    ):
        return Frequency.IMMEDIATE
    if len(allowed_frequencies) == 1:
        return allowed_frequencies[0]
    if Frequency.DAILY_DIGEST in allowed_frequencies:
        return Frequency.DAILY_DIGEST
    if Frequency.WEEKLY_DIGEST in allowed_frequencies:
        return Frequency.WEEKLY_DIGEST
    return Frequency.IMMEDIATE


CATALOG: tuple[CatalogEvent, ...] = (
    # ── 🛡️ Security ─────────────────────────────────────────────────────────
    CatalogEvent(
        key="security.audit_finding",
        subscription="security_findings",
        category=Category.SECURITY,
        label="New audit finding",
        description="The security audit found a new issue on one of your bots. Cost issues go to the Cost category.",
        default_enabled=True,
        # ERROR-severity audit findings flap (the 2026-06-28 flood was almost
        # entirely these), so they digest by default (workstream D1). A genuine
        # CRITICAL finding (e.g. world-readable creds) is carried by the
        # CRITICAL security.audit_critical event below, which stays IMMEDIATE.
        # Safety is preserved through the security_findings group
        # (is_safety_critical there), so muting still warns; the per-event flag
        # is dropped so the derivation rule can route this to the digest.
        #
        # 2026-09-01: this comment used to name security.cve_finding as the
        # carrier for audit criticals. That was never true — cve_finding's only
        # producer is the daily CVE scan (producer_source="security_cve_scan"),
        # and audit.py has annotated its CRITICAL page with THIS key since the
        # Phase-D producer sweep (#942), i.e. from before D1 flipped it to the
        # digest. The consequence was that audit's page-on-transition CRITICAL
        # batch resolved to DAILY_DIGEST on a default pod and reached the
        # operator up to ~24h late. security.audit_critical is the carrier the
        # comment always assumed existed.
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.ERROR,
        producer_source="audit",
        body_template=(
            "🛡️ New security finding\n"
            "{bot_id}: {check_title}"
        ),
        sample_payload={
            "bot_id": "team_bot_a",
            "check_title": "gateway loopback auth missing",
        },
        action=cli_action("sudo evolve-admin deploy --bot {bot_id}"),
    ),
    CatalogEvent(
        key="security.audit_critical",
        subscription="security_findings",
        category=Category.SECURITY,
        label="Critical security audit finding",
        description=(
            "The security audit found a CRITICAL issue on your pod — exposed "
            "credentials, a bot account holding admin rights, sudoers drift. "
            "You get one message when a finding first appears (or re-appears "
            "after being fixed); a finding that is still open just stays on "
            "the Alerts page instead of messaging you again."
        ),
        default_enabled=True,
        # Why this is a SEPARATE event rather than a severity flag on
        # security.audit_finding: delivery cadence is a property of the catalog
        # EVENT, not of the individual message, so one key cannot both digest
        # the ERROR-severity flap traffic (D1's whole point) and page its
        # criticals. audit.py annotates only its CRITICAL page-on-transition
        # batch with this key; signal_notifier keeps routing every audit Signal
        # (warn and alert alike) to the security.audit_finding umbrella, so the
        # digest still carries the standing board and this key stays rare.
        #
        # IMMEDIATE is earned by severity=CRITICAL through
        # derive_default_frequency. A MUST_PAGE_ALLOWLIST slot would be
        # redundant, not dangerous: that frozenset is read at exactly one site
        # in production code — derive_default_frequency below — so for a
        # CRITICAL entry it changes nothing. It is NOT a rate-breaker control.
        # (An earlier draft of this comment said a slot "additionally bypasses
        # the global rate breaker"; that was false, and is the same
        # believed-but-unchecked-comment failure this event exists to fix.)
        #
        # Rate-cap bypass is a SEPARATE mechanism — dispatcher._is_bypass_alert
        # — which this event qualifies for three times over: the producer
        # passes severity=CRITICAL, the entry is is_safety_critical, and the
        # entry's own severity is CRITICAL. So this page rides
        # alerts.rate_cap.critical_max_per_window (30/hr/channel) rather than
        # the normal max_per_window (10/hr), and storm mode does not touch it.
        # Past 30 bypass-class alerts in the rolling hour it batches to the
        # daily digest rather than dropping.
        #
        # This entry does not change how the breaker CLASSIFIES the send —
        # audit has always passed severity=CRITICAL. It does change whether the
        # breaker is consulted at all: the catalog gate previously returned
        # DEFERRED upstream of the breaker call site, so the page now enters
        # the channel's arrival and bypass_sends windows for the first time
        # (at most one arrival per page, but the arrival window feeds storm
        # hysteresis for every other alert on that channel).
        #
        # Immediate-only (no digest option), matching security.cve_finding: the
        # volume this key can produce is bounded by R-1's transition gate (page
        # on newly-firing only), so there is no flood for a digest option to
        # protect against, and offering one would re-create exactly the routing
        # hole this event was added to close. Muting is still available — via
        # the safety-critical security_findings group, which warns first.
        #
        # Migration note: an operator override on security.audit_finding does
        # NOT carry here, by design. A per-event enabled:false was already
        # inert for grouped events (_resolve_enabled makes the group
        # authoritative). A legacy frequency:"off" override — reachable on pods
        # old enough to predate #963 — still silences the umbrella and does not
        # silence this key. That is the intended reading: muting the digest
        # umbrella is not a request to stop hearing about criticals, and the
        # group toggle is the control for that. Deliberately NOT wired through
        # LEGACY_KEY_MIGRATION, which maps RETIRED keys to survivors: the
        # umbrella is very much alive, and grafting its override onto this key
        # would re-point the page at the muted setting and undo the fix.
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.CRITICAL,
        is_safety_critical=True,
        producer_source="audit",
        # audit.py pre-renders the batch and passes it as message=, so this
        # template only drives the Subscriptions "Test" preview. It mirrors
        # the real header + bullet shape built in dispatch_findings.
        body_template=(
            "🔴 <b>Evolve Security Audit — CRITICAL Findings</b>\n"
            "\n"
            "{findings}"
        ),
        sample_payload={
            "findings": (
                "• team_bot_a openclaw.json is world-readable (mode 644)\n"
                "• /etc/sudoers.d/evolve changed since baseline"
            ),
        },
        action=ui_action("Alerts → Active"),
    ),
    CatalogEvent(
        key="security.cve_finding",
        subscription="security_findings",
        category=Category.SECURITY,
        label="New security vulnerability (CVE) affecting your pod",
        description=(
            "The daily CVE scan matched a newly-published vulnerability "
            "against software your pod runs (OpenClaw, macOS). Review and "
            "patch the affected component, or reply 'mute <CVE-id>' if it "
            "doesn't apply to your setup."
        ),
        default_enabled=True,
        # CVE findings are security-critical — IMMEDIATE only, never a
        # deferrable digest (matches the high-severity security pattern:
        # config_drift, autonomy_demoted, breaker trips). The producer
        # (security_cve_scan) also keeps a direct-Telegram fallback, so a
        # broken admin package can't silence a finding either way.
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        # The producer sends CRITICAL when any finding in the batch is
        # critical, WARNING otherwise; CRITICAL is the honest ceiling for the
        # catalog metadata operators see (pod_state.subscriptions, the
        # Subscriptions UI). It is NOT what earns this event its rate-cap
        # bypass, and flipping it would change no gate's outcome here:
        # _is_bypass_alert checks is_safety_critical BEFORE it falls through
        # to the entry's own severity, and this event carries that flag. The
        # entry-severity branch is the deciding one only for an event that is
        # CRITICAL *without* the flag: today system.pod_delivery_regression
        # and system.send_surface_broken (both produced via signal_notifier,
        # whose _SEVERITY_MAP tops out at ERROR — so for those two the
        # entry's severity is the ONLY thing keeping a regression out of the
        # storm digest), plus cost.hard_cap_hit, whose entry is retained but
        # whose producer was removed 2026-05-28 (see spend_alert.py).
        severity=Severity.CRITICAL,
        producer_source="security_cve_scan",
        # security_cve_scan pre-renders the operator message and passes it
        # as message=; this template mirrors finalize.render_alert so the
        # Subscriptions "Test" preview matches what actually gets sent.
        # {severity_emoji} is 🔴 (critical) / ⚠️ (warning) — both approved
        # headers — picked per finding by the producer.
        body_template=(
            "{severity_emoji} Security finding — {cve_id}\n"
            "{plain_summary}\n"
            "Affects: {affects_package} {affects_range}\n"
            "{installed_line}\n"
            "Source: {source_url}\n"
            "Reply 'mute {cve_id}' to stop alerting on this finding."
        ),
        sample_payload={
            "severity_emoji": "🔴",
            "cve_id": "CVE-2026-12345",
            "plain_summary": (
                "A crafted request can crash the gateway before it "
                "authenticates the caller."
            ),
            "affects_package": "openclaw",
            "affects_range": "< 2026.5.9",
            "installed_line": "Installed: OpenClaw 2026.5.7",
            "source_url": "https://github.com/advisories/GHSA-xxxx-xxxx-xxxx",
        },
        action=ui_action("Alerts → Active"),
        is_safety_critical=True,
    ),
    CatalogEvent(
        key="security.config_drift",
        subscription="security_findings",
        category=Category.SECURITY,
        label="Bot configuration changed unexpectedly",
        description="A bot's settings or instructions drifted from the deployed state — changed outside the normal deploy flow.",
        default_enabled=True,
        # Benign-flap event: fires on routine out-of-deploy edits, so it
        # digests by default (workstream D1). The genuine-unauthorized-tamper
        # variant from drift-taxonomy L2 will page immediately once it lands —
        # see MUST_PAGE_ALLOWLIST. allowed_frequencies keeps IMMEDIATE so an
        # operator can still opt this bot back into immediate paging.
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.ERROR,
        producer_source="heal",
        body_template=(
            "🛡️ Config drift on {bot_id}\n"
            "A protected configuration file changed outside the deploy flow.\n"
            "Drift: {drift_summary}"
        ),
        sample_payload={
            "bot_id": "admin_bot",
            "drift_summary": "openclaw.json modified outside deploy",
        },
        # Earlier catalog suggested "evolve-admin deploy --bot {bot_id}"
        # as the fix — but deploy doesn't update the heal baseline, so
        # operators ran it, drift persisted, and the alert re-fired (see
        # 2026-05-17 thread). The actual remedy is "Accept current state
        # as baseline" in Security → Backup config; that commits the
        # current openclaw.json into evolve-backup/openclaw.json so the
        # next heal check sees no diff. Operators who want to *revert*
        # rather than accept can use the Apps page to roll back.
        action=ui_action("Security → Backup config → Accept as baseline"),
    ),
    CatalogEvent(
        key="security.autonomy_posture_drift",
        subscription="autonomy",
        category=Category.SECURITY,
        label="Bot's allowed actions out of sync with its setting",
        description=(
            "What a bot is actually allowed to do on an integration "
            "(e.g. send email) no longer matches the level set on the "
            "Permissions page — the setting is currently a description, "
            "not a guarantee."
        ),
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="permission_monitor",
        body_template=(
            "🛡️ Autonomy setting out of sync on {bot_id}\n"
            "{integration_label}: enforcement no longer matches "
            "\"{rung_label}\".\n"
            "Re-select the level on Security → Permissions → Autonomy "
            "to re-apply it."
        ),
        sample_payload={
            "bot_id": "personal-bot",
            "integration_label": "email (Google Workspace)",
            "rung_label": "Drafts only",
        },
        action=ui_action("Security → Permissions → Autonomy"),
        is_safety_critical=True,
    ),
    CatalogEvent(
        key="security.autonomy_review",
        subscription="autonomy",
        category=Category.SECURITY,
        label="Bot can act more freely than the default — confirm it",
        description=(
            "A bot was observed able to act on an integration more "
            "freely than the shipped default (e.g. send email without "
            "asking), and nobody has recorded that as a deliberate "
            "choice yet. Nothing was changed — this asks you to keep "
            "or restrict it."
        ),
        default_enabled=True,
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.INFO,
        producer_source="permission_monitor",
        body_template=(
            "🛡️ {bot_id} can use {integration_label} at "
            "\"{rung_label}\" — wider than the default\n"
            "Inferred from the live configuration, not set by anyone. "
            "Confirm or restrict it on Security → Permissions → "
            "Autonomy; the bot keeps working either way."
        ),
        sample_payload={
            "bot_id": "personal-bot",
            "integration_label": "email (Google Workspace)",
            "rung_label": "Asks first",
        },
        action=ui_action("Security → Permissions → Autonomy"),
    ),
    CatalogEvent(
        key="security.autonomy_limit_hit",
        subscription="autonomy",
        category=Category.SECURITY,
        label="Bot reached its daily action limit",
        description=(
            "A bot reached the daily limit you set for an integration "
            "(e.g. email sends per day) and is paused from acting there "
            "until tomorrow. Reading and drafting continue."
        ),
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="permission_monitor",
        body_template=(
            "⚠️ {bot_id} hit its daily limit on {integration_label}\n"
            "{count} of {cap} allowed actions used today — paused there "
            "until tomorrow. Reading and drafting continue.\n"
            "Raise the limit on Security → Permissions → Autonomy if "
            "this keeps happening."
        ),
        sample_payload={
            "bot_id": "personal-bot",
            "integration_label": "email (Google Workspace)",
            "count": 10,
            "cap": 10,
        },
        action=ui_action("Security → Permissions → Autonomy"),
        is_safety_critical=True,
    ),
    CatalogEvent(
        key="security.autonomy_demoted",
        subscription="autonomy",
        category=Category.SECURITY,
        label="Bot's freedom automatically reduced after an incident",
        description=(
            "Evolve stepped a bot down from acting on its own to asking "
            "first on one integration, because it kept pushing past its "
            "daily limit or a critical security finding named that "
            "integration. One click restores the previous level after "
            "you confirm."
        ),
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.ERROR,
        producer_source="permission_monitor",
        # 🔴, deliberately NOT ⚡: the breaker emoji is scoped to cost
        # breaker trips; an autonomy step-down is a safety alert
        # (spec-autonomy-ladder §3.3 — the ⚡ carve-out does not apply).
        body_template=(
            "🔴 {bot_id} now asks first on {integration_label}\n"
            "Evolve reduced what it can do on its own because {reason}.\n"
            "Review the alert on the Alerts page — it has a one-click "
            "restore (asks you to confirm)."
        ),
        sample_payload={
            "bot_id": "personal-bot",
            "integration_label": "email (Google Workspace)",
            "reason": (
                "it kept attempting actions after its daily limit "
                "paused it"
            ),
        },
        action=ui_action("Alerts → restore, or Security → Permissions → Autonomy"),
        is_safety_critical=True,
    ),
    CatalogEvent(
        key="security.autonomy_promotion_candidate",
        subscription="autonomy",
        category=Category.SECURITY,
        label="Bot has a clean track record — could be allowed more",
        description=(
            "A bot has been acting on an integration with your OK, "
            "cleanly and consistently, for a sustained stretch. Nothing "
            "changes — a suggestion to allow more may appear on the "
            "Improvements page, where applying it is your decision."
        ),
        default_enabled=True,
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.INFO,
        producer_source="permission_monitor",
        body_template=(
            "🛡️ {bot_id} has a clean track record on {integration_label}\n"
            "{actions} actions over {span_days} days, each with a "
            "go-ahead, no incidents. A suggestion to allow more may "
            "appear on the Improvements page — nothing changes unless "
            "you approve it."
        ),
        sample_payload={
            "bot_id": "personal-bot",
            "integration_label": "email (Google Workspace)",
            "actions": 12,
            "span_days": 9,
        },
        action=ui_action("Improvements page (when the suggestion appears)"),
    ),
    # RETIRED 2026-08-14 — ``security.proposal_quarantined`` (safety-critical,
    # IMMEDIATE, member of ``security_findings``). Its sole emitter was
    # review.py, deleted by #3641; the deny mandate now lives in
    # ``arbiter/security_screen.py``, which is a pure predicate and pages
    # nobody. A denied proposal is not silently swallowed: the autonomy gate
    # routes it to human review, where ``decisions.proposal_ready`` announces
    # it, and the plugin approve route answers a clicking operator
    # synchronously with ``security_screen_denied``. Dropped rather than
    # LEGACY_KEY_MIGRATION-mapped — see the note on that table for why.
    CatalogEvent(
        key="security.key_rotation_overdue",
        subscription="security_findings",
        category=Category.SECURITY,
        label="API key rotation overdue",
        description="One or more bots haven't rotated their API key in a while.",
        default_enabled=True,
        default_frequency=Frequency.WEEKLY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_WEEKLY_DIGEST,
        severity=Severity.WARNING,
        producer_source="cost",
        body_template=(
            "🛡️ Key rotation overdue\n"
            "These bots haven't rotated API keys in {threshold_days}+ days: {stale_bots}."
        ),
        sample_payload={
            "stale_bots": "team_bot_a, admin_bot",
            "threshold_days": 90,
        },
        action=cli_action("sudo evolve-admin keys sync --all"),
    ),
    # ── 💰 Cost ──────────────────────────────────────────────────────────────
    CatalogEvent(
        key="cost.daily_threshold",
        subscription="cost_warnings",
        category=Category.COST,
        label="Daily spend over soft threshold",
        description="A bot's daily spend went above its warning threshold (no enforcement; just a heads-up).",
        default_enabled=True,
        # spend_alert already de-dupes per-bot per-day via its
        # source-level 24h cooldown, so IMMEDIATE here is naturally
        # rate-limited; operators can pick DAILY_DIGEST if they prefer
        # all spend events bundled at digest hour.
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="spend_alert",
        body_template=(
            "💰 {bot_id}'s daily spend hit ${amount:.2f}\n"
            "Threshold: ${threshold:.2f}."
        ),
        sample_payload={"bot_id": "admin_bot", "amount": 5.96, "threshold": 5.00},
        action=ui_action("Cost"),
    ),
    CatalogEvent(
        key="cost.hard_cap_hit",
        subscription="cost_enforcement",
        category=Category.COST,
        label="Daily spend cap reached (enforced)",
        description="A bot hit its daily hard cap. Enforcement kicked in — the bot was downgraded for the rest of the day.",
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.CRITICAL,
        producer_source="spend_alert",
        body_template=(
            "🔴 {bot_id} hit its daily spending cap\n"
            "Spent ${spend:.2f} of the ${cap:.2f} limit. {action_label}."
        ),
        sample_payload={
            "bot_id": "admin_bot", "spend": 12.50, "cap": 10.00,
            "action_label": "Downgraded to tier 3 for rest of day",
        },
        action=ui_action("Cost → Spending Caps"),
    ),
    CatalogEvent(
        key="cost.breaker_tripped",
        subscription="cost_enforcement",
        category=Category.COST,
        label="Cost breaker auto-tripped (auto-turns paused)",
        description=(
            "Per-bot L1 cost breaker auto-tripped after spend crossed the "
            "configured cap. Heartbeats and other auto-turns are disabled "
            "for the trip duration (default 24h). User-driven turns "
            "continue to work — exec passthrough during trip is the design "
            "intent so the operator can still talk to the bot and direct "
            "remediation. This is a system-state change, not just an "
            "alert; the operator should know immediately."
        ),
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.CRITICAL,
        producer_source="breakers_runner",
        body_template=(
            "⚡ <b>{bot_id} breaker TRIPPED</b> ({breaker_type})\n"
            "{reason}\n"
            "Auto-turns disabled for ~{duration_hours}h "
            "(until {expires_at_short} UTC). "
            "User-driven turns still work.\n"
            "trip_id: <code>{trip_id_short}</code>"
        ),
        sample_payload={
            "bot_id": "security_bot",
            "breaker_type": "cost",
            "reason": "Spent $5.54 of $5.00 daily cap",
            "expires_at_short": "tomorrow 03:14",
            "duration_hours": 24,
            "trip_id_short": "a1b2c3d4",
        },
        action=ui_action("Cost → Spending Caps"),
        is_safety_critical=True,
    ),
    CatalogEvent(
        key="cost.tier_downgrade_active",
        subscription="cost_enforcement",
        category=Category.COST,
        label="Tier downgrade active (cost)",
        description=(
            "Per-bot tier_downgrade cap auto-fired: the bot's primary "
            "model was switched to a tier-3 model for the rest of the day. "
            "Auto-reverts at midnight. Phase 6 of the 2026-06 cost-cap "
            "normalization; spec: internal/spec-cost-caps-2026-06-05.md."
        ),
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.WARNING,
        producer_source="spend_alert",
        body_template=(
            "⚠️ <b>{bot_id} tier-downgraded</b> (cost)\n"
            "{reason}\n"
            "Primary model: <code>{tier3_model}</code>"
        ),
        sample_payload={
            "bot_id": "team_bot_a",
            "tier3_model": "anthropic/claude-haiku-4-5",
            "reason": (
                "Spent $8.10 of $8.00 tier_downgrade cap; switched primary "
                "model to anthropic/claude-haiku-4-5. Auto-reverts at midnight."
            ),
        },
        action=ui_action("Settings → Bots → Cost & caps"),
    ),
    CatalogEvent(
        # Named ``cost.gateway_stopped`` (not ``cost.l2_*``) so gitleaks'
        # generic-api-key entropy filter doesn't false-positive: the ``l2``
        # digit pushes the value past the 3.5 entropy floor, while a
        # digit-free identifier stays under. Also semantically truer to
        # what L2 actually does — ``launchctl bootout`` on the gateway.
        key="cost.gateway_stopped",
        subscription="cost_enforcement",
        category=Category.COST,
        label="L2 cost breaker tripped (gateway stopped)",
        description=(
            "Per-bot L2 cost breaker auto-tripped: the bot's gateway was "
            "stopped entirely via launchctl bootout. No chat, no background. "
            "Manual reset required — there is no auto-revert. Phase 6 of "
            "the 2026-06 cost-cap normalization; spec: "
            "internal/spec-cost-caps-2026-06-05.md."
        ),
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.CRITICAL,
        producer_source="spend_alert",
        body_template=(
            "🔴 <b>{bot_id} L2 BREAKER TRIPPED</b>\n"
            "{reason}\n"
            "trip_id: <code>{trip_id_short}</code>"
        ),
        sample_payload={
            "bot_id": "team_bot_a",
            "breaker_type": "cost_l2",
            "reason": (
                "Spent $30.00 of $25.00 L2 cap; gateway stopped. "
                "Manual reset required: 'evolve-admin breaker reset team_bot_a cost_l2'."
            ),
            "trip_id_short": "a1b2c3d4",
        },
        action=ui_action("Cost → Spending Caps"),
        is_safety_critical=True,
    ),
    CatalogEvent(
        key="cost.burst_detected",
        subscription="cost_warnings",
        category=Category.COST,
        label="Short-window cost burst",
        description=(
            "Per-bot spend crossed the burst threshold inside a short "
            "rolling window (default $5 in 60 min). Fires once per "
            "(bot, hour) — re-fires if the window straddles a new hour. "
            "Severity escalates at 3× the threshold (likely runaway loop); "
            "the producer pre-renders the title line so a single "
            "subscription covers both tiers."
        ),
        default_enabled=True,
        # Bursts are time-sensitive — only delivery modes that surface
        # them immediately make sense. Digest would defeat the purpose.
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.WARNING,
        producer_source="spend_alert",
        # Producer fills {level_emoji} / {event_phrase} / {threshold_clause}
        # per severity tier so one entry covers both warn and critical
        # without a sibling catalog event. See spend_alert.send_burst_alert.
        body_template=(
            "{level_emoji} {bot_id} {event_phrase}: ${cost:.2f} in "
            "{window_minutes}m {threshold_clause}\n"
            "Biggest session id={top_session_id} channel={top_session_channel} "
            "cost=${top_session_cost:.2f}.{top2_line}"
        ),
        sample_payload={
            "bot_id": "security_bot", "cost": 6.02, "threshold": 5.0,
            "window_minutes": 60,
            "level_emoji": "💰", "event_phrase": "burst",
            "threshold_clause": "(threshold $5.00)",
            "top_session_id": "51bac282", "top_session_channel": "heartbeat",
            "top_session_cost": 4.30,
            "top2_line": "\nNext: session 891abda1 channel=heartbeat $1.20.",
        },
        action=ui_action("Cost → Bot detail"),
    ),
    CatalogEvent(
        key="cost.heartbeat_session_bloat",
        subscription="cost_warnings",
        category=Category.COST,
        label="Heartbeat ran too many turns",
        description=(
            "A heartbeat session ran more turns than the configured "
            "threshold (default >5). Correct-shape heartbeats are 1-3 "
            "turns; >5 is structurally wrong (typically a retry storm — "
            "exec-approval timeout, tool-deny loop, fallback walk). "
            "Fires regardless of cost: catches the 2026-05-20 security_bot "
            "shape even under the haiku-primary workaround where the "
            "dollar surface stays cheap. Severity escalates above 30 "
            "turns (runaway-loop tier); the producer pre-renders the "
            "emoji + trail so one subscription covers both tiers."
        ),
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.WARNING,
        producer_source="cost_watchdog",
        # session_id rendered with :.8 precision spec so the operator
        # sees a short prefix in chat while the Signal store + dispatch
        # payload carry the full UUID (cost_watchdog._dispatch_via_catalog
        # passes the full id; the template truncates for display).
        # {level_emoji} / {trail} populated by the producer per tier.
        body_template=(
            "{level_emoji} {bot_id}: heartbeat session {session_id:.8} ran "
            "{turn_count} turns (threshold {warn_threshold})\n"
            "Cost ${cost:.2f} on {models}.{trail}"
        ),
        sample_payload={
            "bot_id": "security_bot",
            "session_id": "51bac282-394c-4f8f-89cc-3db6e85cfb77",
            "turn_count": 16,
            "warn_threshold": 5,
            "cost": 0.42,
            "models": "claude-haiku-4-5",
            "level_emoji": "💰",
            "trail": "",
        },
        action=ui_action("Cost → Bot detail"),
    ),
    CatalogEvent(
        key="cost.session_budget_exceeded",
        subscription="cost_enforcement",
        category=Category.COST,
        label="Session paused: cost cap reached",
        description=(
            "A session crossed its per-session cost cap "
            "(agents.defaults.sessionBudgetCapUsd). The next turn on that "
            "session is rejected with a budget-exceeded message until the "
            "operator raises the cap or clears the breaker file. Distinct "
            "from the per-bot daily cap — this is per-session, fired by the "
            "plugin's SessionCostMonitor and echoed into Signals by "
            "session_cost_monitor."
        ),
        default_enabled=True,
        # Active block on user activity — immediate is the only mode that
        # makes sense; digest would hide the fact that the user's session
        # is currently rejected.
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.ERROR,
        producer_source="session_cost_monitor",
        body_template=(
            "🔴 {bot_id} session paused: ${cost_usd:.4f} over ${cap_usd:.2f} cap\n"
            "Session {session_id_short} — next turn rejected until the cap "
            "is raised or the breaker file is cleared."
        ),
        sample_payload={
            "bot_id": "team_bot_a",
            "cost_usd": 5.2134,
            "cap_usd": 5.00,
            "session_id_short": "51bac282",
        },
        action=ui_action("Cost → Bot detail"),
    ),
    CatalogEvent(
        key="cost.forge_session_cap",
        subscription="cost_enforcement",
        category=Category.COST,
        label="A forge run hit its cost/message cap",
        description=(
            "A forge dispatch was refused before running because its "
            "projected worst-case cost exceeded the per-turn / per-dispatch "
            "cap, or its message loop hit the session message cap "
            "(network.json::bots.<bot>.forge.*). The dispatch is blocked "
            "until the cap is raised — emitted by forge_cost_guard."
        ),
        default_enabled=True,
        # An active block on a forge run; immediate so the operator can
        # raise the cap and retry rather than discover it in a digest.
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="forge_cost_guard",
        body_template=(
            "⚠️ Forge run capped on {bot_id}\n"
            "{summary}"
        ),
        sample_payload={
            "bot_id": "team_bot_a",
            "summary": (
                "forge per-dispatch worst-case $108.20 exceeds cap $100.00 — "
                "dispatch refused."
            ),
        },
        action=ui_action("Cost → Bot detail"),
    ),
    CatalogEvent(
        key="cost.weekly_summary",
        subscription="cost_warnings",
        category=Category.COST,
        label="Weekly spend summary",
        description="Pod-wide rollup of last week's spend.",
        default_enabled=True,
        default_frequency=Frequency.WEEKLY,
        allowed_frequencies=_F_NATURAL_WEEKLY,
        severity=Severity.INFO,
        producer_source="spend_alert",
        body_template=(
            "📊 Weekly spend ({iso_week})\n"
            "Total: ${total:.2f}\n"
            "{per_bot_breakdown}"
        ),
        sample_payload={
            "iso_week": "2026-W19",
            "total": 42.50,
            "per_bot_breakdown": "  admin_bot: $20.00\n  team_bot_a: $22.50",
        },
        action=None,  # informational
    ),
    # Session-economics efficiency notes (session_economics → cache_invalidation
    # _elevated / cache_hit_rate_low / bot_unused). Heads-up tuning signals about
    # how efficiently sessions spend — e.g. "100% of cached turns invalidated"
    # (prompt-cache TTL too short), a low warm-cache hit rate, or a bot that
    # isn't being used. No enforcement, nothing broke; these are cost-shaped
    # observations the operator may want to act on at leisure. DAILY_DIGEST by
    # default (digest-default rule) — classified out of meta.unclassified by the
    # 2026-06-28 flood triage (D4), where the cache-invalidation stat was a loud
    # contributor.
    CatalogEvent(
        key="cost.session_economics",
        subscription="cost_warnings",
        category=Category.COST,
        label="Session efficiency note (cache, usage)",
        description=(
            "A heads-up about how efficiently your bots' sessions spend — for "
            "example the prompt cache being invalidated more than it helps "
            "(TTL too short), a low cache hit rate, or a bot that's barely "
            "being used. Nothing is broken and nothing was enforced; it's a "
            "tuning hint, so it's batched into your daily digest."
        ),
        default_enabled=True,
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.INFO,
        producer_source="session_economics",
        body_template=(
            "💰 Session efficiency note\n"
            "{summary}"
        ),
        sample_payload={
            "summary": (
                "team_bot_a: 100% of cache-participating turns were "
                "invalidated yesterday — the prompt-cache TTL may be too "
                "short to pay off."
            ),
        },
        action=ui_action("Cost → Sessions"),
    ),
    # ── 🟢 System health ─────────────────────────────────────────────────────
    CatalogEvent(
        key="system.gateway_state_change",
        subscription="pod_health",
        category=Category.SYSTEM,
        label="Bot gateway came up or went down",
        description="A bot's gateway transitioned between online and offline.",
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.ERROR,
        producer_source="signal_notifier",
        body_template=(
            "{emoji} {bot_id}'s gateway is {state_phrase}"
        ),
        sample_payload={"emoji": "🔴", "bot_id": "team_bot_a", "state_phrase": "down"},
        action=cli_action(
            "sudo launchctl kickstart -k system/ai.openclaw.{bot_id}-gateway"
        ),
    ),
    CatalogEvent(
        key="system.gateway_autorestart_failed",
        subscription="pod_health",
        category=Category.SYSTEM,
        label="Bot gateway didn't auto-restart",
        description="A bot's gateway went down and the auto-restart job couldn't bring it back after retries.",
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.ERROR,
        producer_source="heal",
        body_template=(
            "🔴 {bot_id}'s gateway is still down — auto-restart failed\n"
            "heal.py couldn't bring the gateway back up on port {port}."
        ),
        sample_payload={"bot_id": "team_bot_a", "port": 18789},
        action=cli_action(
            "sudo launchctl kickstart -k system/ai.openclaw.{bot_id}-gateway"
        ),
    ),
    CatalogEvent(
        key="system.sudoers_refresh_failed",
        subscription="pod_health",
        category=Category.SYSTEM,
        label="Sudo grants need a manual refresh",
        description=(
            "A pulled change updated the sudo-grant template, but the repo "
            "puller can't self-install /etc/sudoers.d/evolve — it runs as the "
            "evolve service user, which deliberately has no grant to rewrite "
            "its own sudoers (that would let a compromise of evolve escalate "
            "to root). New grants stay dormant, and features that depend on "
            "them silently break, until an operator runs the refresh command."
        ),
        default_enabled=True,
        # IMMEDIATE, not digest: this is a deterministic, operator-actionable
        # maintenance alert (run one command), not a flap-prone host warning.
        # Routing it to system.watchdog_event (DAILY_DIGEST) is exactly why the
        # 2026-06-10 firing signal sat undelivered for 24h+ (deliveries: []).
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.WARNING,
        producer_source="signal_notifier",
        body_template=(
            "⚠️ Sudo grants need a manual refresh\n"
            "{title} — new grants from the latest pull are dormant until you "
            "run the command below."
        ),
        sample_payload={"title": "repo-puller sudoers auto-refresh failed"},
        action=cli_action("sudo evolve-admin refresh-sudoers"),
    ),
    CatalogEvent(
        key="system.credential_store_unreadable",
        subscription="pod_health",
        category=Category.SYSTEM,
        label="Evolve can't read a bot's saved credentials",
        description=(
            "A bot's saved provider credentials are on disk but Evolve is "
            "locked out of the folder that holds them, and Evolve's own "
            "automatic permission repair did not fix it. Until it is fixed, "
            "anything that needs that bot's provider key reports \"no provider "
            "credentialed\" — on the pod's primary bot that means every "
            "background feature that calls an AI model quietly stops working."
        ),
        default_enabled=True,
        # IMMEDIATE, not digest — same reasoning as system.sudoers_refresh_failed
        # directly above. This alert is only ever written AFTER Evolve tried its
        # own repair and failed, so it is deterministic (not a flap) and
        # operator-actionable (run one command). Routing it to the digest-default
        # system.watchdog_event bucket would reproduce the 2026-06-10 shape where
        # a firing signal sat undelivered for 24h+ with deliveries: [].
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.ERROR,
        producer_source="signal_notifier",
        body_template=(
            "🔴 Evolve can't read {bot_id}'s saved credentials\n"
            "Evolve tried to repair the folder permissions itself and it still "
            "can't get in, so this won't clear on its own."
        ),
        sample_payload={"bot_id": "team_bot_a"},
        action=cli_action("sudo evolve-admin ensure-pod-perms"),
    ),
    CatalogEvent(
        key="system.watchdog_event",
        subscription="pod_health",
        category=Category.SYSTEM,
        label="System warning (disk, network, host)",
        description="A host- or pod-level system warning — disk, memory, network, or dependency probe.",
        default_enabled=True,
        # Watchdog events are flap-prone — default to digest so the
        # operator gets one summary rather than a stream during noisy
        # periods. Switch to IMMEDIATE for a louder posture.
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="signal_notifier",
        body_template=(
            "⚠️ Watchdog: {title}"
        ),
        sample_payload={"title": "admin_bot disk over 90% (94.2GB / 100GB)"},
        action=ui_action("Alerts → Active"),
    ),
    CatalogEvent(
        key="system.daemon_error_spike",
        subscription="pod_health",
        category=Category.SYSTEM,
        label="Service is logging errors at a high rate",
        description="A background service started logging errors well above its normal rate.",
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.ERROR,
        producer_source="signal_notifier",
        body_template=(
            "🔴 {daemon} is logging errors at an elevated rate\n"
            "{summary}."
        ),
        sample_payload={"daemon": "admin-ui", "summary": "12 errors in last 5 min"},
        action=ui_action("Alerts → Active"),
    ),
    CatalogEvent(
        key="system.repo_puller_wedged",
        subscription="pod_health",
        category=Category.SYSTEM,
        label="Code-pull service is stuck",
        description="The job that pulls new code onto the pod is stuck — updates aren't landing on the running deploy.",
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.ERROR,
        producer_source="repo_puller",
        body_template=(
            "🔴 repo-puller is wedged on {repo_name}\n"
            "The deploy is no longer pulling new code from origin/main.\n"
            # Full path, not just the filename — the records live under
            # {shared_dir}/repo-puller/incidents/, not in the repo, so a
            # bare name gives the paged operator nothing to open.
            "Record: {incident_path}\n"
            "Error: {error_summary}"
        ),
        sample_payload={
            "repo_name": "evolve-repo",
            "issue_filename": "wedge-2026-05-10.md",
            "incident_path": "/Users/Shared/evolve/repo-puller/incidents/wedge-2026-05-10.md",
            "error_summary": "untracked file conflict",
            # Sample value mirrors what _resolve_pod_context produces when
            # adminBaseUrl + admin_user are both set — the most common
            # configured state. Operators at the deploy box get "".
            "ssh_prefix": "ssh you@your-pod-host ",
        },
        # {ssh_prefix} is injected by dispatcher._resolve_pod_context:
        # empty when the operator is at the deploy box, "ssh <target> "
        # (note the trailing space) when they're remote. Avoids the
        # earlier hardcoded "ssh mini" — that alias only existed in the
        # original developer's ~/.ssh/config.
        action=cli_action(
            "{ssh_prefix}sudo launchctl kickstart -k system/ai.evolve.evolve.repo-puller"
        ),
    ),
    CatalogEvent(
        key="system.repo_puller_recovered",
        subscription="pod_health",
        category=Category.SYSTEM,
        label="Code-pull service is back",
        description="The job that pulls new code onto the pod is healthy again after a wedge — useful as a closing bracket to the wedged alert so an operator knows when the deploy is current.",
        default_enabled=True,
        # Recovery is a single transition, not a recurring state — only
        # fires when a previously-firing wedge Signal resolves on a
        # successful pull. IMMEDIATE only (DAILY_DIGEST would defeat the
        # "closing bracket" purpose).
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.INFO,
        producer_source="repo_puller",
        body_template=(
            "🟢 repo-puller is back on {repo_name}\n"
            "Pull --ff-only succeeded after a wedge cleared.\n"
            "{advanced_summary}"
        ),
        sample_payload={
            "repo_name": "evolve-repo",
            # advanced_summary is built by the producer so the same template
            # handles "advanced N commits" and "already up to date" cases
            # without an empty newline.
            "advanced_summary": "Advanced 3 commits (now on d98597c5).",
        },
        action=None,
    ),
    CatalogEvent(
        key="system.stalled_cron",
        subscription="pod_health",
        category=Category.SYSTEM,
        label="Scheduled job hasn't run",
        description="A scheduled background job has gone silent past its expected interval.",
        default_enabled=True,
        # cron_alert source-level cooldown is already 24h per cron, so
        # IMMEDIATE here is naturally at-most-daily; operator can pick
        # DAILY_DIGEST to bundle multiple stalled crons.
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="cron_alert",
        body_template=(
            "⚠️ {bot_id}'s {label} cron has stalled\n"
            "{reason}."
        ),
        sample_payload={
            "bot_id": "team_bot_a",
            "label": "ai.evolve.team_bot_a.measure",
            "reason": "last run 51h ago (threshold: 2d)",
        },
        action=cli_action("sudo launchctl kickstart -k system/{label}"),
    ),
    # v20 — app scheduled work failures (spec-app-coherence-and-reconciliation
    # -2026-06-05.md §17.3 + Q32). Distinct from system.stalled_cron above:
    # stalled_cron is "schedule hasn't fired in a while" (producer cron_alert);
    # this event is "schedule fires but the actual work fails" (producer
    # app_structural_verifier, assertion_id ∈ {openclaw_cron_error,
    # openclaw_cron_skipped, openclaw_cron_delivery_failure}). Validation
    # against the production pod (personal-bot, team-bot-a, team-bot-c, security-bot, evolve) surfaced
    # 14 of 16 scheduled jobs in this failure mode — including team-bot-a-task-worker's
    # multi-month delivery-credentials silence and evolve's CVE-scan no-route.
    CatalogEvent(
        key="system.app_scheduled_work_failure",
        subscription="app_delivery",
        category=Category.SYSTEM,
        label="App scheduled work is failing",
        description=(
            "One of an app's scheduled tasks is failing: the cron fires but "
            "the work errors out, gets silently skipped, or completes "
            "nominally but can't deliver its output. Detected by the Tier-2 "
            "structural verifier reading openclaw cron run status."
        ),
        default_enabled=True,
        # Tier-2 runs every 6h; signal-dedup means the operator gets one
        # message per (cron, failure-shape) until it resolves or they snooze.
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="app_structural_verifier",
        body_template=(
            "⚠️ {bot_id}: {app_id} scheduled work is failing\n"
            "{summary}"
        ),
        sample_payload={
            "bot_id": "team-bot-a",
            "app_id": "team-bot-a-task-management",
            "summary": (
                "openclaw cron 'team-bot-a-task-worker' last run reported error: "
                "Slack API credentials aren't configured in the fallback "
                "messenger."
            ),
            # ``job_id`` is interpolated into the action template; the
            # audit_poller forwards it from the Finding evidence
            # (assertion_id "openclaw_cron_*" carries it in
            # evidence["openclaw_cron_id"], which the poller exposes as
            # ``job_id`` in the catalog payload).
            "job_id": "ae1bfa20-c256-45f4-8c7b-decf5f9446ad",
        },
        # Operator-actionable remediation is per-finding; the catalog
        # provides a generic pointer to investigate. The literal job-id
        # comes from the Signal's evidence; the action text uses brace
        # placeholders that the rendering pipeline tolerates (vs the
        # angle-bracket form which the HTML validator parses as a tag).
        action=cli_action("openclaw cron runs --id {job_id} --limit 5"),
    ),
    # App scripts that FAIL when the agent invokes them (the "(agent) failed"
    # exec chip leaking into chat). Producer: app_script_failure_audit — the
    # sibling daily transcript walk to agent_bypass_audit. Boundary with the
    # two events above: app_scheduled_work_failure is the structural
    # verifier's openclaw-cron last-run-state finding; this is the chat-leaked
    # agent-invoked exec failure. Motivating case: a real scripts/tasks.py
    # failure on bot personal-bot leaked into a chat thread and no Signal fired.
    # Spec: internal/spec-agent-freelance-bypass-2026-06-05.md.
    CatalogEvent(
        key="system.app_script_failure",
        subscription="app_delivery",
        category=Category.SYSTEM,
        label="An app's script is failing when the bot runs it",
        description=(
            "An installed app tells the bot to run a script in response to a "
            "message, but the script is erroring out when invoked — the "
            "\"(agent) failed\" marker shows up in the chat. The bot then "
            "answers on its own, skipping the app's controls (scope limits, "
            "source grounding, opt-out, rate limits). Fix by migrating the "
            "app to plugin_intercept (so the script runs deterministically) "
            "and fixing the script itself."
        ),
        default_enabled=True,
        # Daily audit + signal dedup → one message per (bot, app) until it
        # clears or the operator snoozes; digest available for a quieter
        # posture.
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="app_script_failure_audit",
        body_template=(
            "⚠️ {bot_id}: {app_id}'s script keeps failing\n"
            "{summary}"
        ),
        sample_payload={
            "bot_id": "personal-bot",
            "app_id": "personal-bot-task-manager",
            "summary": (
                "scripts/tasks.py failed on 2 of 5 sessions in the last 24h "
                "(e.g. 'run python3 scripts/tasks.py (agent) failed'). The bot "
                "answered without it, bypassing the app's controls. Fix: "
                "migrate the app to plugin_intercept and fix the script."
            ),
        },
        action=ui_action("Bots → {bot_id} → Apps → {app_id}"),
    ),
    # Proactive-delivery monitor (U2.1, spec-proactive-delivery-monitor-
    # 2026-06-10.md §7 + §10.4). Boundary with the event above:
    # app_scheduled_work_failure is Tier-2's chronic last-run-state finding
    # on a 6-hour cadence; these two are the delivery monitor's per-window
    # outcomes ("today's 7:00 briefing by 7:30"). Body copy follows
    # operator-message-style (Plex test — no producer/launchd/path jargon).
    # PR 2 of the spec adds the M2 announce_unannounced_resolve flag and
    # the 🟢 recovery rendering on the missed event.
    CatalogEvent(
        key="system.app_delivery_missed",
        subscription="app_delivery",
        category=Category.SYSTEM,
        label="A scheduled app delivery didn't arrive",
        description=(
            "A scheduled user-facing app (a briefing, sweep, or watcher) "
            "missed its delivery window — either it never ran, or it ran "
            "but its message didn't reach you."
        ),
        default_enabled=True,
        # One Signal per action (not per window) + notifier debounce keep
        # this to one message per unreliable app until it recovers.
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="delivery_monitor",
        body_template=(
            "⚠️ {app_name} didn't arrive\n"
            "{bot_id}'s {schedule_human} {app_name} didn't go out as "
            "scheduled.\n"
            "Check {bot_id}'s Apps page for details."
        ),
        sample_payload={
            "app_name": "Morning Briefing",
            "bot_id": "personal-bot",
            "schedule_human": "07:00",
        },
        action=ui_action("Bots → {bot_id} → Apps"),
        # M2 (§9.1): a miss the heal fixes inside the notifier debounce
        # must still produce its single truthful 🟢 ("late today, now
        # delivered") — silent self-healing is the exact behavior users
        # said they distrust. Also re-announces the one warn→alert
        # escalation ("the restart didn't work" — §9.2's binding
        # follow-up promise).
        announce_unannounced_resolve=True,
    ),
    CatalogEvent(
        key="system.app_delivery_unmeasurable",
        subscription="app_delivery",
        category=Category.SYSTEM,
        label="Can't confirm an app's deliveries",
        description=(
            "Evolve couldn't check whether a scheduled app's recent "
            "deliveries happened — the records it needs aren't readable. "
            "The app may still be arriving normally."
        ),
        default_enabled=True,
        # §9.3 noise discipline: a single broken probe lives on the Alerts
        # page; chat hears about it via the daily digest only after the
        # monitor escalates (3 consecutive blind windows → warn).
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="delivery_monitor",
        body_template=(
            "⚠️ Can't confirm {app_name} is being delivered\n"
            "Evolve couldn't check {bot_id}'s recent scheduled deliveries — "
            "the records it needs aren't readable. {app_name} may still be "
            "arriving normally.\n"
            "Check the Alerts page for details."
        ),
        sample_payload={
            "app_name": "Morning Briefing",
            "bot_id": "personal-bot",
        },
        action=ui_action("Alerts → Active"),
    ),
    # Pod-scope delivery regression (Wave-5 of the 2026-06-11 P0): the
    # delivery monitor's per-app misses crossing the many-apps-many-bots
    # threshold inside one rolling day. One platform story instead of N
    # app stories; live copy comes from the Signal body (which names the
    # OpenClaw update only when one was actually installed recently).
    CatalogEvent(
        key="system.pod_delivery_regression",
        subscription="app_delivery",
        category=Category.SYSTEM,
        label="Bot messages may not be getting through (pod-wide)",
        description=(
            "Scheduled deliveries are being missed across several apps and "
            "several bots in the same day — a platform-level problem (most "
            "often a software update), not individual app problems."
        ),
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.CRITICAL,
        producer_source="delivery_monitor",
        body_template=(
            "🔴 Messages from your bots may not be getting through\n"
            "{missed_count} scheduled deliveries across {bot_count} of your "
            "bots were missed in the last day — that pattern points at the "
            "platform, not the individual apps.\n"
            "The individual misses are on the Alerts page."
        ),
        sample_payload={
            "missed_count": "6",
            "bot_count": "3",
        },
        action=ui_action("Alerts → Active"),
    ),
    # Post-OC-upgrade send-surface verification failed (`evolve-admin
    # menu upgrade` runs the probe right after the install). Distinct
    # from the event above: this is the at-upgrade-time detection; the
    # pod_delivery_regression event is the day-after drift backstop.
    CatalogEvent(
        key="system.send_surface_broken",
        subscription="app_delivery",
        category=Category.SYSTEM,
        label="A software update broke bot messaging",
        description=(
            "The check that runs right after an OpenClaw update found the "
            "message-sending path broken — scheduled messages will fail on "
            "every bot until it's fixed or rolled back."
        ),
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.CRITICAL,
        producer_source="send_surface_probe",
        body_template=(
            "🔴 Messages from your bots may not be getting through\n"
            "The OpenClaw update to {new_version} broke the path your bots "
            "use to send scheduled messages.\n"
            "Roll back: {rollback_command}"
        ),
        sample_payload={
            "new_version": "2026.6.1",
            "rollback_command": "sudo evolve-admin menu upgrade --help",
        },
        action=ui_action("Alerts → Active"),
    ),
    # system.test_runner_failures retired 2026-06-08 — app-test surface
    # killed per internal/decision-app-tests-2026-06-08.md. No producer can
    # emit this any more.
    CatalogEvent(
        key="system.manifest_validation_failed",
        subscription="app_delivery",
        category=Category.SYSTEM,
        label="Application manifest has errors",
        description="An application manifest failed validation — one or more apps may not load correctly until fixed.",
        default_enabled=True,
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="validate",
        body_template=(
            "⚠️ Manifest validation failed\n"
            "{summary}"
        ),
        sample_payload={"summary": "admin_bot: 3 unresolved manifest entries"},
        action=ui_action("Apps → Manifest issues"),
    ),
    CatalogEvent(
        key="system.identity_doc_missing",
        subscription="bot_config",
        category=Category.SYSTEM,
        label="A bot's identity/setup files are missing",
        description=(
            "The content scan found a required identity or setup file "
            "(USER / IDENTITY / README / SOUL / MEMORY / HEARTBEAT / TOOLS) "
            "genuinely missing on a bot — it looks deleted, so the bot may "
            "be mis-configured until the file is restored. (A file evolve "
            "simply can't read is the separate, quieter "
            "“can't read a bot's setup file” alert.)"
        ),
        default_enabled=True,
        # Structural/content findings flap as bots are re-set-up; digest
        # by default, IMMEDIATE for a louder posture.
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="content_scan",
        body_template=(
            "⚠️ Setup file missing on {bot_id}\n"
            "{summary}"
        ),
        sample_payload={
            "bot_id": "team_bot_a",
            "summary": "USER.md is missing",
        },
        action=ui_action("Alerts → Active"),
    ),
    CatalogEvent(
        key="system.bot_file_unreadable",
        subscription="bot_config",
        category=Category.SYSTEM,
        label="Evolve can't read a bot's setup file",
        description=(
            "The content scan couldn't read one of a bot's setup files — but "
            "the file is almost certainly still there. This is usually a "
            "short-lived permission or access hiccup that clears on its own, "
            "not a deleted or tampered file. If it keeps happening, the evolve "
            "user may have lost read access to that bot's files."
        ),
        default_enabled=True,
        # Access flaps come and go on their own — batch into the digest, never
        # page. Keeping this below alert severity is what lets the digest
        # collapse its fire/clear churn instead of screaming each cycle.
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.INFO,
        producer_source="content_scan",
        body_template=(
            "🔧 Couldn't read a setup file on {bot_id}\n"
            "{summary}"
        ),
        sample_payload={
            "bot_id": "team_bot_a",
            "summary": "Evolve couldn't read USER.md — likely a short-lived permission hiccup",
        },
        action=ui_action("Alerts → Active"),
    ),
    CatalogEvent(
        key="system.oc_cli_misinvocation",
        subscription="bot_config",
        category=Category.SYSTEM,
        label="OpenClaw CLI flag changed upstream",
        description=(
            "Evolve is calling the OpenClaw CLI with an outdated argument "
            "shape — almost always means OpenClaw renamed or required a "
            "flag in a recent release and Evolve hasn't caught up. Evolve "
            "needs a code update; the operator typically just files a bug. "
            "Fires once per caller/subcommand/pattern; re-fires weekly if "
            "still broken."
        ),
        default_enabled=True,
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="oc_cli",
        body_template=(
            "⚠️ <code>{caller_module}</code> → <code>openclaw {oc_subcommand}</code> shape mismatch\n"
            "Pattern: {pattern_key}\n"
            "Stderr: {stderr_snip}"
        ),
        sample_payload={
            "caller_module": "spend_alert",
            "oc_subcommand": "message send",
            "pattern_key": "required_option",
            "stderr_snip": "error: required option '-t, --target <dest>' not specified",
        },
        action=ui_action("Alerts → Active"),
    ),
    # Self-Signal fired by the upstream_issues_watcher monitor when its
    # gh CLI auth is missing/expired. Stable dedup_key so repeated polls
    # produce one Signal, not a flood — operator runs the documented
    # one-time auth-login command and the next tick clears it.
    CatalogEvent(
        key="system.upstream_issues_watcher_auth",
        subscription="upstream_tracking",
        category=Category.SYSTEM,
        label="GitHub issue tracker needs auth",
        description=(
            "The job that watches GitHub issues you filed can't reach "
            "GitHub — its auth is missing or expired. One-time sign-in "
            "command fixes it."
        ),
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.WARNING,
        producer_source="upstream_issues_watcher",
        body_template=(
            "⚠️ Upstream issues watcher can't reach GitHub\n"
            "{message}"
        ),
        sample_payload={
            "message": "gh CLI reports: not authenticated. Run gh auth login",
        },
        action=cli_action(
            "sudo -u evolve gh auth login --hostname github.com "
            "--git-protocol https --web"
        ),
    ),
    # Plugin-monitor umbrella. plugin_monitor emits 11 distinct signal
    # types covering security-flavored drift (denied/load_path/
    # install_source), config drift, allow-list drift, and operational
    # gaps (required-missing, openclaw.json missing). Bundled under one
    # catalog event so the operator picks plugin posture in/out as a
    # single category — the per-type fan-out is visible on the Alerts
    # page, not in chat subscriptions.
    CatalogEvent(
        key="system.plugin_health_issue",
        subscription="bot_config",
        category=Category.SYSTEM,
        label="Bot plugin health issue",
        description=(
            "A bot's plugin posture has a problem — missing required "
            "plugin, denied plugin enabled, unauthorized install source, "
            "allow-list drift, config drift, or load-path anomaly. "
            "Severity varies by type (alert / warn / info); the operator "
            "controls them as one subscription."
        ),
        default_enabled=True,
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="plugin_monitor",
        body_template=(
            "⚠️ Plugin issue: {title}"
        ),
        sample_payload={
            "title": "team_bot_a: required plugin 'evolve-plugin' is not enabled",
        },
        action=ui_action("Alerts → Active"),
    ),
    # Plugin cascade-telemetry silently failing (cascade_audit →
    # plugin_telemetry_failure). A bot is alive (its plugin is updating
    # recent-transcripts.json) but zero cascade telemetry spans landed today —
    # the signature of a write-access problem on the bot's spans/ dir. Real
    # plugin-posture degradation (no shadow verdicts / labels / anomaly
    # detection for that bot until fixed), but a maintenance-tier one with a
    # known one-command fix, so DAILY_DIGEST by default rather than a page.
    # Classified out of meta.unclassified by the 2026-06-28 flood triage (D4).
    CatalogEvent(
        key="system.plugin_telemetry_silent",
        subscription="bot_config",
        category=Category.SYSTEM,
        label="A bot's plugin stopped recording telemetry",
        description=(
            "A bot is alive but its plugin isn't writing the cascade "
            "telemetry that powers cost attribution, shadow verdicts and "
            "anomaly detection — almost always a write-permission problem on "
            "the bot's spans folder. One redeploy fixes it. Batched into your "
            "daily digest; it's degraded background data, not an outage."
        ),
        default_enabled=True,
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="cascade_audit",
        body_template=(
            "🟢 Plugin telemetry silent\n"
            "{summary}"
        ),
        sample_payload={
            "summary": (
                "team_bot_a is alive but wrote zero cascade telemetry spans "
                "today. Fix: sudo evolve-admin deploy team_bot_a."
            ),
        },
        action=ui_action("Alerts → Active"),
    ),
    # exec_outcome_watchdog umbrella. Covers four detector families —
    # tool_error_burst (annotation-derived), exec_denied,
    # approval_timeout, preflight_block. All four are flavors of "the
    # bot tried to do something and the system blocked it" from the
    # operator's POV; bundled under one subscription so the operator
    # mutes the whole class together.
    CatalogEvent(
        key="system.exec_outcome_failure",
        subscription="bot_config",
        category=Category.SYSTEM,
        label="Bot exec outcomes failing",
        description=(
            "A bot's tool/exec invocations are failing in patterns the "
            "operator should see — tool-error bursts, exec-policy "
            "denials, approval timeouts, or OC preflight blocks. The "
            "exec_outcome_investigator generator attributes the cause "
            "in a follow-up Proposal."
        ),
        default_enabled=True,
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="exec_outcome_watchdog",
        body_template=(
            "⚠️ Exec outcomes: {title}"
        ),
        sample_payload={
            "title": "team_bot_a: 12 tool errors over 7d across 4 sessions",
        },
        action=ui_action("Alerts → Active"),
    ),
    # stuck_proposal_monitor — pod-scoped, single signal type. Fires
    # when one or more approved proposals have been sitting in
    # proposals/approved/ for more than threshold_days (default 7)
    # without progressing to applied/archived. The 2026-05-20
    # heal-team_bot_a quarantine incident is the reference failure mode —
    # see the producer module for the narrative.
    CatalogEvent(
        key="system.stuck_proposal",
        subscription="pod_health",
        category=Category.SYSTEM,
        label="Approved proposal hasn't progressed",
        description=(
            "One or more approved self-improvement proposals have been "
            "sitting in proposals/approved/ without moving to applied "
            "or archived. The apply pipeline is either broken, gated "
            "on something, or skipping these proposals for an "
            "idempotency reason."
        ),
        default_enabled=True,
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="stuck_proposal_monitor",
        body_template=(
            "⚠️ Stuck proposals: {title}"
        ),
        sample_payload={
            "title": "3 approved proposals stuck >7d without progressing",
        },
        action=ui_action("Proposals → Approved"),
    ),
    # Post-wrap install honesty (U1 activation fix, 2026-06-11): an app
    # install that fails AFTER the add-bot wrap (or any queued gallery
    # install) used to die silently in the job log — the operator heard
    # a promise and never heard it break. This event is the loud half.
    CatalogEvent(
        key="system.app_install_failed",
        subscription="app_delivery",
        category=Category.SYSTEM,
        label="App install didn't finish",
        description=(
            "An app that was being installed on one of your bots didn't "
            "make it — including installs queued by the add-a-bot "
            "conversation after its wrap-up message."
        ),
        default_enabled=True,
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="app_install",
        body_template=(
            "⚠️ {bot_id}: the {app_name} app didn't finish installing\n"
            "{reason}"
        ),
        sample_payload={
            "bot_id": "team_bot_a",
            "app_name": "Morning Briefing",
            "reason": (
                "team_bot_a has no place to send messages yet — connect "
                "a channel and the briefing sets itself up."
            ),
        },
        action=ui_action("Improve → Forge Jobs"),
    ),
    # ── 🔄 Updates ───────────────────────────────────────────────────────────
    # Two variants, chosen by update_watcher based on the safe-upgrade
    # preflight verdict at detection time:
    #   • openclaw_available — preflight passed; recommend `oc upgrade`
    #   • openclaw_blocked   — preflight found blockers; explain + point
    #     at `oc safe-upgrade --target X` for the full report
    # Bare `npm install -g openclaw` is never recommended — it leaves
    # rogue user-LaunchAgents behind. See ocadmin.oc_upgrade.
    CatalogEvent(
        key="updates.openclaw_available",
        subscription="updates",
        category=Category.UPDATES,
        label="New OpenClaw release (safe to upgrade)",
        description="A newer OpenClaw release is available and passes the safe-upgrade preflight on this pod.",
        default_enabled=True,
        default_frequency=Frequency.WEEKLY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_WEEKLY_DIGEST,
        severity=Severity.INFO,
        producer_source="update_watcher",
        body_template=(
            "🔄 OpenClaw {new_version} is available\n"
            "You're on {current_version}. Passed the safe-upgrade preflight."
        ),
        sample_payload={"new_version": "2026.5.7", "current_version": "2026.4.29"},
        action=cli_action("sudo evolve-admin menu upgrade"),
    ),
    CatalogEvent(
        key="updates.openclaw_blocked",
        subscription="updates",
        category=Category.UPDATES,
        label="New OpenClaw release (upgrade blocked)",
        description="A newer OpenClaw release is available but the safe-upgrade preflight found problems that have to be fixed first.",
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.WARNING,
        producer_source="update_watcher",
        body_template=(
            "⚠️ OpenClaw {new_version} is available — but Evolve needs work first\n"
            "You're on {current_version}.\n"
            "{blocker_summary}"
        ),
        sample_payload={
            "new_version": "2026.5.7",
            "current_version": "2026.4.29",
            "blocker_summary": (
                "Blockers:\n"
                "  • Node 25.9 below required 26.0\n"
                "  • rogue ai.openclaw.gateway.plist in team_bot_a's LaunchAgents"
            ),
        },
        action=cli_action(
            "sudo evolve-admin menu safe-upgrade --target {new_version}",
            text_template="Full preflight report: <code>{cli_command}</code>",
        ),
    ),
    CatalogEvent(
        key="updates.evolve_repo",
        subscription="updates",
        category=Category.UPDATES,
        label="New Evolve code available",
        description="The Evolve repository has new commits on main since the last notification.",
        default_enabled=False,  # opt-in; many will find this noisy
        default_frequency=Frequency.WEEKLY_DIGEST,
        allowed_frequencies=_F_WEEKLY_DIGEST_ONLY,
        severity=Severity.INFO,
        producer_source="update_watcher",
        body_template=(
            "🔄 A new version of Evolve is available\n"
            "{commits_summary}"
        ),
        sample_payload={"commits_summary": "5 new updates since the last check"},
        action=None,
    ),
    # Tracked upstream-bug closure. update_watcher polls a configured list of
    # GitHub issues (in {shared_dir}/update_watcher/tracked_upstream_issues.json
    # or the embedded default seed) once per daily tick; when an issue we care
    # about closes, the operator gets a heads-up so they can re-attempt
    # whatever was blocked on the upstream fix.
    CatalogEvent(
        key="updates.upstream_issue_resolved",
        subscription="upstream_tracking",
        category=Category.UPDATES,
        label="Tracked GitHub bug was fixed",
        description="A GitHub issue Evolve was watching (upstream bug you depend on) was closed — re-check whatever was blocked on it.",
        default_enabled=True,
        default_frequency=Frequency.WEEKLY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_WEEKLY_DIGEST,
        severity=Severity.INFO,
        producer_source="update_watcher",
        body_template=(
            "🟢 Upstream issue resolved: {repo}#{number}\n"
            "{title}\n"
            "{hint}\n"
            "Closed at {closed_at}. {url}"
        ),
        sample_payload={
            "repo": "openclaw/openclaw",
            "number": 82301,
            "title": "stock→externalized plugin migration is unblockable on 5.12",
            "hint": "When this closes, the 4.29→5.12 upgrade may no longer require the manual config-neutralize dance — re-check via `sudo evolve-admin menu safe-upgrade`.",
            "closed_at": "2026-05-20T14:32:00Z",
            "url": "https://github.com/openclaw/openclaw/issues/82301",
        },
        action=None,
    ),
    CatalogEvent(
        key="updates.plugin_available",
        subscription="updates",
        category=Category.UPDATES,
        label="Bot plugin update available",
        description="A plugin (or one of its dependencies) installed on one of your bots has a newer release.",
        default_enabled=False,
        default_frequency=Frequency.WEEKLY_DIGEST,
        allowed_frequencies=_F_WEEKLY_DIGEST_ONLY,
        severity=Severity.INFO,
        producer_source="update_watcher",
        body_template=(
            "🔄 {plugin} {new_version} is available for {bot_id}\n"
            "Currently on {old_version}."
        ),
        sample_payload={
            "bot_id": "team_bot_a", "plugin": "evolve-plugin",
            "old_version": "1.2.0", "new_version": "1.3.0",
        },
        action=None,
    ),
    # Sibling of updates.upstream_issue_resolved — that one fires on
    # state transitions; this one fires on any new activity (most often
    # a maintainer comment) on issues the operator filed against
    # configured upstream repos. Auto-discovers issues via gh CLI rather
    # than the tracked-issues seed file. Lives behind the
    # ``upstream_issues_watcher`` feature gate; defaults to off so a
    # standard household install never subscribes. Per
    # feedback_message_style_team_bot_a_like: short header + one fact per line +
    # link, no Security/CRITICAL label.
    CatalogEvent(
        key="updates.upstream_issue_activity",
        subscription="upstream_tracking",
        category=Category.UPDATES,
        label="New activity on a GitHub bug you filed",
        description=(
            "Someone (usually a maintainer) commented on or updated a "
            "GitHub issue you filed against a project Evolve is configured "
            "to watch. Developer-profile feature — defaults to off."
        ),
        default_enabled=False,  # opt-in; not every operator files issues
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.INFO,
        producer_source="upstream_issues_watcher",
        body_template=(
            "🔄 {repo}{issue} — {actor}\n"
            "{title}\n"
            "{body_short}\n"
            "{url}"
        ),
        sample_payload={
            "repo": "openclaw/openclaw",
            "issue": "#84820",
            "title": "Unclosed FileHandle on session JSONL lock crashes gateway on Node ≥24",
            "actor": "oc-maintainer",
            "event_kind": "new_comment",
            "body_short": "Thanks for the detailed report. We tried several current-main reproductions on AWS Linux with Node 25.9.0 …",
            "url": "https://github.com/openclaw/openclaw/issues/84820#issuecomment-12345",
        },
        action=None,
    ),
    # Surface drift-diff: when a new OpenClaw release is detected, update_watcher
    # snapshots the candidate's surface (CLI commands/flags, bundled extensions,
    # channel catalog, config-schema keys) from the same tarball safe_upgrade
    # fetches and diffs it against the installed OC. Catches the "what actually
    # changed" gap that let a breaking auth-store migration slip through a
    # routine version bump (internal/openclaw-coverage-audit-2026-06-04.md F1/F2).
    # WARNING when a changed surface is one Evolve declares it depends on
    # (oc_deps.OC_DEPENDENCIES), INFO otherwise. Signal-store-only producer —
    # chat delivery flows through signal_notifier, never direct dispatch.
    CatalogEvent(
        key="updates.openclaw_surface_drift",
        subscription="updates",
        category=Category.UPDATES,
        label="New OpenClaw release changed surfaces Evolve uses",
        description=(
            "A newer OpenClaw release changed its surface (CLI commands, bundled "
            "extensions, channel catalog, or config-schema keys). Warns when a "
            "changed surface is one Evolve depends on — that's a signal Evolve "
            "may need updating before adopting the release. The full diff is "
            "written under the pod's update_watcher/oc-surface-diffs/ directory."
        ),
        default_enabled=True,
        default_frequency=Frequency.WEEKLY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_WEEKLY_DIGEST,
        severity=Severity.INFO,
        producer_source="oc_surface_drift",
        body_template=(
            "🔄 OpenClaw {candidate_version} changed surfaces Evolve uses\n"
            "{summary}\n"
            "Installed: {current_version}"
        ),
        sample_payload={
            "candidate_version": "2026.6.5",
            "current_version": "2026.6.1",
            "summary": (
                "2 extension change(s), 1 channel change(s) — "
                "Evolve depends on 1 of the changed surface(s)"
            ),
        },
        action=None,
    ),
    # Bot-vs-admin version skew (deploy_drift_monitor → deploy_drift). A bot
    # is running Evolve code newer or older than the admin server — usually a
    # mid-rollout transient that SELF-CLEARS on the next pull, so this must
    # never page. DAILY_DIGEST by default (the digest-default rule, spec-
    # subscription-digest-default-2026-06-28): the operator sees it batched if
    # it persists, and a transient skew clears before the digest even flushes.
    # Classified out of meta.unclassified by the 2026-06-28 flood triage (D4):
    # version-skew rows were a noisy contributor to the loud catch-all.
    CatalogEvent(
        key="updates.version_skew",
        subscription="updates",
        category=Category.UPDATES,
        label="A bot is on a different Evolve version than the server",
        description=(
            "One or more bots are running Evolve code that's newer or older "
            "than the admin server — normal for a few minutes mid-rollout, "
            "and it clears itself once the slower side pulls. Batched into "
            "your daily digest, never an immediate ping, because it almost "
            "always self-resolves before you'd act on it."
        ),
        default_enabled=True,
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.INFO,
        producer_source="deploy_drift_monitor",
        body_template=(
            "🔄 Bots on a different Evolve version\n"
            "{summary}"
        ),
        sample_payload={
            "summary": (
                "2 bots are 1 release behind the admin server "
                "(2026.0628.3330). They'll catch up on the next pull."
            ),
        },
        action=ui_action("Updates → Releases"),
    ),
    # ── 📋 Decisions needed ──────────────────────────────────────────────────
    CatalogEvent(
        key="decisions.proposal_ready",
        subscription="needs_decision",
        category=Category.DECISIONS,
        label="Self-improvement proposal ready",
        description="One or more self-improvement proposals are waiting for your review.",
        default_enabled=True,
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.INFO,
        producer_source="analyze",
        body_template=(
            "📋 {count} new proposal{s} ready for review\n"
            "{titles}"
        ),
        sample_payload={
            "count": 3, "s": "s",
            "titles": "admin_bot: trim AGENTS.md size · team_bot_a: lower context cap · team_bot_b: enable test gate",
        },
        # Breadcrumb points at the real destination: the Recommendations
        # page's Inbox tab. There is no "Proposals → Pending" surface — the
        # actionable queue is Recommendations → Inbox. See
        # proposal_routing.py for the routing contract this string must match.
        action=ui_action("Recommendations → Inbox"),
    ),
    CatalogEvent(
        key="decisions.proposal_applied",
        subscription=None,
        # Internalized (Phase 2 chip 3): the daily pod report already
        # covers applied proposals, so the standalone chat message is
        # dropped. KEEP the admin-UI receipt + apply-result record.
        notify=False,
        category=Category.DECISIONS,
        label="Self-improvement change applied",
        description="A self-improvement proposal was applied (either succeeded or rolled back).",
        default_enabled=False,  # visible in admin UI; opt-in for chat
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.INFO,
        producer_source="apply",
        body_template=(
            "📋 {bot_id}'s proposal {proposal_id} was {action}\n"
            "{problem_summary}."
        ),
        sample_payload={
            "action": "applied", "bot_id": "admin_bot",
            "proposal_id": "prop_abc1234",
            "problem_summary": "Reduce context tokens by ~30%",
        },
        action=ui_action("Proposals → Recent"),
    ),
    # RETIRED 2026-08-14 — ``decisions.proposal_rejected``. Already
    # internalized (``subscription=None``, ``notify=False``): never exposed
    # in Configure, never pushed to chat. Its sole emitter was review.py,
    # deleted by #3641, so the row had no producer and no operator surface
    # left. The internal records it used to accompany are untouched — a
    # rejected proposal still lands in ``proposals/rejected/`` and the
    # signal-tuning ``feedback.jsonl`` is written by the Signal store /
    # proposal-rejection routes, never by this dispatch.
    CatalogEvent(
        key="decisions.proposal_outcome_checkin",
        subscription=None,
        # Internalized (Phase 2 chip 3): the "did this help?" follow-up
        # is dropped as a standalone chat message.
        notify=False,
        category=Category.DECISIONS,
        label="\"Did this self-improvement help?\" follow-up",
        description="A few days after applying a self-improvement change, Evolve asks whether it actually helped.",
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.INFO,
        producer_source="outcome",
        body_template=(
            "📋 How did {bot_id}'s change work out?\n"
            "Applied {apply_date}: {summary}."
        ),
        sample_payload={
            "bot_id": "admin_bot", "summary": "Reduce context tokens",
            "apply_date": "2026-05-09",
        },
        action=bot_action("yes / no / details"),
    ),
    CatalogEvent(
        key="decisions.forge_job_ready",
        subscription="needs_decision",
        category=Category.DECISIONS,
        label="App build ready to approve",
        description="A Forge build (new app or app update) is ready for your review and approval.",
        default_enabled=True,
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.INFO,
        producer_source="forge_engine",
        # identity: see applications.app_identity.resolve_app_id — ``app_id``
        # and ``pkg_id`` are two DISTINCT payload keys that forge_engine
        # already emits side by side, and the operator reading this needs
        # both: which app was built, and which package version it built into.
        # Collapsing them would change a rendered notification body, which is
        # a producer contract, not an internal read.
        body_template=(
            "📋 Forge {job_type} job ready\n"
            "{app_id} for {bot_id} ({pkg_id})."
        ),
        sample_payload={
            "app_id": "my-app", "pkg_id": "my-app@1.0.0",
            "bot_id": "team_bot_a", "job_type": "create",
        },
        action=ui_action("Apps → Forge → {app_id}"),
    ),
    # Briefing auto-activation (U1 activation fix, 2026-06-11): a bot
    # whose morning-briefing decision was recorded before it had any
    # channel gets the briefing installed automatically the moment its
    # first messaging channel connects. This is the honest receipt for
    # that automatic action — the other half of the wizard's "it
    # switches itself on when you connect a place to chat" promise.
    CatalogEvent(
        key="decisions.briefing_activated",
        subscription=None,
        # Internalized (Phase 2 chip 3): this confirms a user's own
        # in-app action (their recorded briefing choice switching on once
        # a channel connects), so the confirmation chat message is
        # dropped. The producer (pack_driver) shares its notification
        # helper with the system.app_install_failed branch, which MUST
        # keep firing — so the gate is applied here (notify=False) rather
        # than ripping the elif branch out of the producer. The auto-
        # activation itself still happens; only the receipt message stops.
        notify=False,
        category=Category.DECISIONS,
        label="Morning briefing switched on",
        description=(
            "A bot's recorded morning-briefing choice activated by "
            "itself once the bot got its first place to send messages."
        ),
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.INFO,
        producer_source="app_install",
        body_template=(
            "🟢 {bot_id}'s morning briefing is set up — the first one "
            "lands at the next {time}.{route_note}"
        ),
        sample_payload={
            "bot_id": "team_bot_a",
            "time": "07:00",
            "route_note": "",
        },
        action=ui_action("Bots → {bot_id}"),
    ),
    # ── 📊 Summaries ─────────────────────────────────────────────────────────
    # Consolidated 2026-06-05: the daily Bot digest (formerly
    # ``summaries.daily_bot_digest``) was merged into this entry to
    # give the operator a single daily message. The pod report now
    # opens with a Pod usage line, surfaces per-bot operational chips
    # (breaker trips, today's cost spikes) alongside the pod-level
    # audit/liveness/trending content, and closes with newly-arrived
    # self-improvement items since the last run — split into actionable
    # Recommendations and awareness-only Findings.
    # ``summaries.daily_bot_digest`` is in LEGACY_KEY_MIGRATION.
    CatalogEvent(
        key="summaries.daily_pod_report",
        subscription="daily_report",
        category=Category.SUMMARIES,
        label="Daily pod report",
        description=(
            "Once-daily pod-wide summary: yesterday's usage + spend, "
            "today's operational issues (breaker trips, gateway "
            "outages, audit findings, anomalies vs. 30-day baseline), "
            "and newly-arrived self-improvement items since the last "
            "run — split into actionable Recommendations (decisions "
            "waiting for you) and awareness-only Findings. Standing "
            "setup tasks (pair WhatsApp, scan needed, version drift) "
            "stay in the admin UI tile view — the report is for what "
            "changed today."
        ),
        default_enabled=True,
        default_frequency=Frequency.DAILY,
        allowed_frequencies=_F_NATURAL_DAILY,
        severity=Severity.INFO,
        producer_source="pod_report",
        body_template=(
            "📊 Pod report — {label}\n"
            "{summary}"
        ),
        sample_payload={
            "label": "Daily",
            "summary": (
                "Pod: 287 turns yesterday, $4.83 ($21.18 7d)\n\n"
                "🔴 security_bot — breaker tripped (L1 cost breaker active — auto turns suspended)"
            ),
        },
        action=None,
    ),
    CatalogEvent(
        key="summaries.weekly_rsi_review",
        subscription="weekly_summaries",
        category=Category.SUMMARIES,
        label="Weekly self-improvement review",
        description="Weekly roll-up of how the self-improvement system is doing (Sundays).",
        default_enabled=True,
        default_frequency=Frequency.WEEKLY,
        allowed_frequencies=_F_NATURAL_WEEKLY,
        severity=Severity.INFO,
        producer_source="weekly_review",
        body_template=(
            "📊 Weekly RSI review\n"
            "{summary}"
        ),
        sample_payload={
            "summary": "12 proposals reviewed, 8 applied, 4 rejected",
        },
        action=None,
    ),
    CatalogEvent(
        key="summaries.weekly_bot_trends",
        subscription="weekly_summaries",
        category=Category.SUMMARIES,
        label="Weekly bot trends",
        # Sibling to summaries.daily_pod_report — that one carries today's
        # news + current-state chips; this one carries 7-day-window trend
        # chips (sustained cost-rate change, sustained pushback,
        # unexpected billing accumulated over the week) that the daily
        # report stopped surfacing per PR #1996. Silent when no bot has
        # any 7d-horizon chip firing.
        description="Weekly roll-up of 7-day trend chips per bot (Sundays): sustained cost spikes, sustained pushback, unexpected billing. Silent when no bot is trending.",
        default_enabled=True,
        default_frequency=Frequency.WEEKLY,
        allowed_frequencies=_F_NATURAL_WEEKLY,
        severity=Severity.INFO,
        producer_source="weekly_bot_trends",
        body_template=(
            "📊 Bot trends — Weekly\n"
            "{summary}"
        ),
        sample_payload={
            "summary": "team_bot_a: cost spike — $25.44 vs $11.40 prior 7d",
        },
        action=None,
    ),
    # ── 🔧 Alerting system (meta.*) ──────────────────────────────────────
    # Messages ABOUT the alerting machinery itself. Introduced by the
    # subscription-completeness keystone (spec-subscription-completeness-
    # 2026-06-24) so EVERY dispatched message carries a subscription handle —
    # storm-mode notices, the digest envelope, the send-proof, alert-repeat
    # loops, dispatcher health, the dev-health KPI note, and the totality
    # catch-all. is_safety_critical
    # is set ONLY on the dispatcher-FAILURE class (meta.dispatcher_health):
    # if alert delivery itself is broken, the operator must not be able to
    # silently mute it. All others are free toggles.
    CatalogEvent(
        key="meta.storm_mode",
        subscription="alerting_system",
        category=Category.META,
        label="Alert storm — batching into your digest",
        description=(
            "Too many alerts fired in a short window, so Evolve is batching "
            "non-critical alerts into your daily digest to keep this chat from "
            "flooding (critical alerts still come through). You get one notice "
            "when batching starts plus an hourly reminder until it subsides. "
            "Mute this to silence the storm reminders themselves — batching "
            "still happens, you just won't be told."
        ),
        default_enabled=True,
        # The rate breaker owns the cadence (one per episode + hourly
        # heartbeat); IMMEDIATE only so a digest can't swallow the very
        # notice explaining why the digest got busy.
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.WARNING,
        producer_source="alert_rate_breaker",
        body_template=(
            "⚡ Alert storm — batching into your daily digest\n"
            "{attempts} alerts fired in the {window_phrase}. Non-critical "
            "alerts go to your digest until the rate subsides."
        ),
        sample_payload={
            "attempts": "22",
            "window_phrase": "last hour",
        },
        action=ui_action("Alerts → Active"),
    ),
    CatalogEvent(
        key="meta.digest",
        subscription=None,
        category=Category.META,
        label="Alert digest bundle",
        description=(
            "The bundled digest of alerts you chose to receive on a schedule "
            "(daily/weekly) rather than immediately, plus the recovery notice "
            "Evolve sends when a backlog accumulated during a delivery outage. "
            "The individual alerts inside are already governed by their own "
            "subscriptions; this handle controls the digest envelope itself."
        ),
        default_enabled=True,
        # The envelope is delivered on the operator's digest schedule by the
        # digest dispatcher; this entry is IMMEDIATE-only so binding it to
        # the send() path cannot re-enqueue the bundle into its own queue
        # (only daily/weekly-digest frequencies re-enqueue).
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.INFO,
        producer_source="digest_dispatcher",
        body_template=(
            "📋 Alert digest\n"
            "{summary}"
        ),
        sample_payload={
            "summary": "3 alerts batched since yesterday — see the Alerts page.",
        },
        action=ui_action("Alerts → Active"),
    ),
    CatalogEvent(
        key="meta.send_probe",
        subscription=None,
        # Internalized (Phase 2 chip 3): drop the green-light "delivery
        # still works" all-clear. The FAILURE path is the separate,
        # always-on system.send_surface_broken event, which stays loud.
        # The probe still RUNS (the send is the test); only the success
        # message is suppressed.
        notify=False,
        category=Category.META,
        label="Delivery proof after a software update",
        description=(
            "After an OpenClaw update, Evolve sends one real message as proof "
            "your bots can still deliver scheduled messages — the message IS "
            "the test. The matching FAILURE alert is the separate, "
            "always-on 'A software update broke bot messaging'. Mute this to "
            "drop just the green all-clear."
        ),
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.INFO,
        producer_source="send_surface_probe",
        body_template=(
            "🟢 OpenClaw update check\n"
            "Updated to {new_version}. This message is itself the proof your "
            "bots can still send you their scheduled messages — nothing to do."
        ),
        sample_payload={"new_version": "2026.6.1"},
        action=None,
    ),
    CatalogEvent(
        key="meta.alert_repeat_loop",
        subscription=None,
        # Internalized (Phase 2 chip 3): the same-alert-on-repeat
        # detection stays an internal noise-detection signal, but it no
        # longer reaches the operator as a standalone chat message.
        notify=False,
        category=Category.META,
        label="An alert is firing on repeat",
        description=(
            "The alert-loop monitor noticed the same alert being sent over and "
            "over — usually a flapping condition or a producer stuck re-firing. "
            "It points at noise in the alerting itself, not a new incident."
        ),
        default_enabled=True,
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="alerts_loop_monitor",
        body_template=(
            "⚠️ Alert on repeat\n"
            "{summary}"
        ),
        sample_payload={
            "summary": "host_health sent the same warning 8× in the last hour.",
        },
        action=ui_action("Alerts → Active"),
    ),
    CatalogEvent(
        key="meta.dispatcher_health",
        subscription="alerting_system",
        category=Category.META,
        label="Alert delivery itself is failing",
        description=(
            "The alert dispatcher is failing to deliver messages — a broken "
            "send path means OTHER alerts may be silently not reaching you. "
            "This is the alarm on the alarm system, so it stays on by "
            "default and warns before you mute it."
        ),
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=_F_IMMEDIATE_ONLY,
        severity=Severity.CRITICAL,
        producer_source="alerts_loop_monitor",
        body_template=(
            "🔴 Alert delivery is failing\n"
            "{summary}"
        ),
        sample_payload={
            "summary": "12 dispatch failures in the last hour — alerts may not be reaching you.",
        },
        action=ui_action("Alerts → Dispatcher health"),
        # The ONLY meta.* entry that is safety-critical: a dead delivery
        # path silently swallows every other alert.
        is_safety_critical=True,
    ),
    # Repo dev-health KPIs (code_quality_monitor → revert_rate_high /
    # fix_heavy_scope / sameday_fix_on_feat). Commit-cadence observations about
    # Evolve's OWN development process — a fix-heavy scope (fix:feat ratio ≥2x),
    # an elevated revert rate, or a same-day fix on a fresh feature. Useful as a
    # background trend, never an incident. Internal (subscription=None, hidden
    # from the Configure groups — it isn't a pod condition the operator tunes),
    # but DAILY_DIGEST so it's batched, never a loud page. Classified out of
    # meta.unclassified by the 2026-06-28 flood triage (D4).
    CatalogEvent(
        key="meta.dev_health",
        subscription=None,
        category=Category.META,
        label="Evolve development-health note",
        description=(
            "A background note about Evolve's own development process — a "
            "code area that's seeing a lot of fixes relative to new features, "
            "an elevated revert rate, or a same-day fix on a just-shipped "
            "feature. It's a trend worth glancing at, not something to act on "
            "now, so it's batched into your daily digest."
        ),
        default_enabled=True,
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.INFO,
        producer_source="code_quality_monitor",
        body_template=(
            "🔧 Dev-health note\n"
            "{summary}"
        ),
        sample_payload={
            "summary": (
                "`backup` is fix-heavy — a 2.2x fix:feat ratio over the last "
                "14 days."
            ),
        },
        action=None,
    ),
    CatalogEvent(
        key="meta.unclassified",
        subscription=None,
        category=Category.META,
        label="Uncategorized alert",
        description=(
            "A safety-net handle for any alert that hasn't been mapped to a "
            "specific category yet. It's batched into your daily digest by "
            "default — never a loud immediate ping — so an unmapped monitor "
            "still reaches you without flooding chat, and its appearance here "
            "is a prompt for Evolve to give it a proper category. You can mute "
            "it, but you may also be muting future un-mapped alerts."
        ),
        default_enabled=True,
        # Digest-default (spec-subscription-digest-default-2026-06-28): the
        # residual catch-all is batched, NOT loud-immediate. An unmapped
        # producer still reaches the operator (via the daily digest) so the
        # deny-list-by-default "no silent monitor" guarantee holds, but it no
        # longer contributes to the immediate-push flood the 2026-06-28 triage
        # measured on evo-vps. Specific contributors are classified into real
        # classes above; whatever is left lands here, digested.
        default_frequency=Frequency.DAILY_DIGEST,
        allowed_frequencies=_F_IMMEDIATE_OR_DAILY_DIGEST,
        severity=Severity.WARNING,
        producer_source="signal_notifier",
        body_template=(
            "🔧 Alert\n"
            "{title}"
        ),
        sample_payload={"title": "An unmapped monitor reported a condition."},
        action=ui_action("Alerts → Active"),
    ),
)


# ── Lookups ─────────────────────────────────────────────────────────────────


_BY_KEY: dict[str, CatalogEvent] = {e.key: e for e in CATALOG}


def by_key(key: str) -> CatalogEvent | None:
    """Return the catalog event with that key, or None if unknown."""
    return _BY_KEY.get(key)


def by_category(category: Category) -> tuple[CatalogEvent, ...]:
    return tuple(e for e in CATALOG if e.category == category)


def all_keys() -> tuple[str, ...]:
    return tuple(e.key for e in CATALOG)


def known_producer_sources() -> frozenset[str]:
    """Set of all producer_source values referenced by the catalog.

    Used by tests to confirm every catalog entry maps to a recognized
    source, and by the dispatcher to validate ``catalog_event`` keys
    against known sources during routing.
    """
    return frozenset(e.producer_source for e in CATALOG)


def subscription_for(event_key: str) -> str | None:
    """Return the operator Subscription id an event belongs to.

    Resolves legacy/retired keys first (mirrors ``subscriptions.
    read_subscription``) so a stale ``catalog_event`` stamped by a
    producer still resolves to its group. Returns ``None`` when the
    event is internal/removed (``subscription=None``) OR when the key is
    unknown to the catalog — both cases mean "no operator group governs
    this", which the dispatcher treats as un-group-gated.
    """
    resolved = LEGACY_KEY_MIGRATION.get(event_key, event_key)
    entry = _BY_KEY.get(resolved)
    if entry is None:
        return None
    return entry.subscription


def notify_for(event_key: str) -> bool:
    """Return whether an event reaches the operator's chat channel.

    Resolves legacy/retired keys first (mirrors ``subscription_for``) so a
    stale ``catalog_event`` stamped by a producer still resolves to the
    surviving entry's ``notify`` flag. An unknown key returns ``True`` —
    the safe (louder) default, matching the dispatcher's fail-open posture
    for un-mapped events: a typo must never silently swallow a message.
    """
    resolved = LEGACY_KEY_MIGRATION.get(event_key, event_key)
    entry = _BY_KEY.get(resolved)
    if entry is None:
        return True
    return entry.notify


def events_for_subscription(subscription_id: str) -> tuple[CatalogEvent, ...]:
    """Return every CatalogEvent that is a member of a Subscription, in
    catalog order. Empty tuple for an unknown id."""
    return tuple(e for e in CATALOG if e.subscription == subscription_id)


def subscribed_events() -> tuple[CatalogEvent, ...]:
    """Every event with a non-None subscription (operator-facing).

    Excludes the internal/removed events (``subscription=None``). Used by
    the Configure API and the totality ratchet.
    """
    return tuple(e for e in CATALOG if e.subscription is not None)


def internal_events() -> tuple[CatalogEvent, ...]:
    """Every event with ``subscription=None`` — internal / removed,
    hidden from the Configure UI and not group-gated."""
    return tuple(e for e in CATALOG if e.subscription is None)


__all__ = (
    "Category", "Frequency", "Severity",
    "ActionOffer", "CatalogEvent",
    "bot_action", "ui_action", "cli_action",
    "render_event",
    "CATALOG", "by_key", "by_category", "all_keys",
    "known_producer_sources",
    "subscription_for", "notify_for", "events_for_subscription",
    "subscribed_events", "internal_events",
    "CATEGORY_DISPLAY_ORDER", "CATEGORY_LABELS", "FREQUENCY_LABELS",
)
