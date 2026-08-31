"""autonomy.catalog — kind semantics and integration bindings, as data.

Spec: internal/spec-autonomy-ladder-2026-06-10.md §1.2 (rung semantics per
kind), §1.3 (rules vocabulary), §5.1 (defaults ship in code).

Per the no-provider-literals rule, provider/server names live ONLY in
the data tables here (``INTEGRATION_BINDINGS``); all logic operates on
kinds and verbs. Adding a new email-capable MCP server is a one-entry
data edit; adding a new kind (Phase C) is a new ``KindSpec``.

Phase A ships the email kind only. The §5.1 default-rung table for the
other kinds is recorded in ``DEFAULT_RUNG_BY_KIND`` so the shipped
defaults are code even before their renderers exist.

Vocabulary (this module is the normative home — see spec §8 OQ-2,
decided 2026-06-10):

  - ``integration_id`` — the bot's ``mcp.servers.<id>`` key, which is
    also the ``mcp_admin.catalog`` ``CatalogEntry.id`` for vetted
    servers. One id space across inventory, allowlist, and posture.
  - ``rung`` — ``draft_only | act_with_approval | autonomous_within_rules``.
  - verbs — kind-scoped action classes (email: ``send``, ``forward``,
    ``delete``, ``draft``); rules.never entries use these names.
  - enforcement surfaces — ``mcp_tool_allowlist`` (mechanical: OC's
    global ``tools.deny`` list) and ``bot_guidance`` (procedural:
    session_surface systemAppend).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, Mapping


# ── Rungs ─────────────────────────────────────────────────────────────────────

RUNG_DRAFT_ONLY = "draft_only"
RUNG_ACT_WITH_APPROVAL = "act_with_approval"
RUNG_AUTONOMOUS = "autonomous_within_rules"

# Ladder order, narrowest first. Promotion = move right; demotion = move left.
RUNGS: tuple[str, ...] = (
    RUNG_DRAFT_ONLY,
    RUNG_ACT_WITH_APPROVAL,
    RUNG_AUTONOMOUS,
)

# Operator labels are the ONLY rung form on primary surfaces (spec §1.1,
# Plex test). "rung"/"posture"/"ladder" stay spec/code vocabulary.
RUNG_LABELS: dict[str, str] = {
    RUNG_DRAFT_ONLY: "Drafts only",
    RUNG_ACT_WITH_APPROVAL: "Asks first",
    RUNG_AUTONOMOUS: "Acts within limits",
}

# Closed rules vocabulary (spec §1.3). Keys outside this set are
# validation errors, not round-tripped extras — a typoed key would
# silently weaken a rung-3 rules block.
RULES_KEYS: tuple[str, ...] = (
    "reach_allow", "scope_allow", "actions_per_day", "never",
)

# §5.1 default-rung table, code-shipped. Phase A only renders email,
# but the defaults are product data, not per-pod config.
DEFAULT_RUNG_BY_KIND: dict[str, str] = {
    "email": RUNG_DRAFT_ONLY,
    "calendar": RUNG_DRAFT_ONLY,
    "messaging": RUNG_DRAFT_ONLY,
    "file_store": RUNG_DRAFT_ONLY,
    "code_hosting": RUNG_ACT_WITH_APPROVAL,
}
DEFAULT_RUNG_FALLBACK = RUNG_DRAFT_ONLY  # anything unrecognized w/ outward actions


# ── Kind specs ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KindSpec:
    """One integration kind's ladder semantics, fully data-driven.

    ``verb_patterns`` classifies a server's tool names (bare names, no
    ``mcp__`` prefix) into kind verbs via fnmatch, evaluated in
    ``verb_precedence`` order — first match wins. Tools matching no
    pattern are read/prepare-tier and never denied.

    ``denied_verbs_by_rung`` is the mechanical render: which verb
    classes the MCP tool denylist blocks at each rung. ``never_verbs``
    are folded into every rung (email ``delete`` is denied even at
    "Acts within limits" — spec §1.4).
    """

    kind: str
    operator_noun: str                       # "email" — primary-surface noun
    default_rung: str
    never_verbs: tuple[str, ...]
    verb_precedence: tuple[str, ...]
    verb_patterns: Mapping[str, tuple[str, ...]]
    # Verbs whose presence on a server makes it ladder-eligible (an
    # integration with none of these has no outward-action surface and
    # gets no ladder row — spec §1.2 "read-only integrations are off
    # the ladder").
    outward_verbs: tuple[str, ...]
    denied_verbs_by_rung: Mapping[str, tuple[str, ...]]
    # Operator copy: what the bot can/can't do at each rung. Used by the
    # expanded UI row AND the promotion confirmation (same strings, spec §4.1).
    rung_meanings: Mapping[str, str]
    # What changes when promoting INTO this rung (confirmation dialog copy).
    promotion_consequences: Mapping[str, str]
    # systemAppend guidance per rung (the procedural surface, spec §2.3).
    guidance: Mapping[str, str]
    # Enforcement honesty (spec §2.4): is the rung's PRIMARY barrier a
    # wall (mechanical — tool denial) or a sign (procedural — guidance
    # + monitoring)? Never-verb denies are mechanical at every rung;
    # this field is about the rung's defining constraint. The UI maps
    # mechanical → "enforced", procedural → "instructed and monitored".
    rung_enforcement_mode: Mapping[str, str] = field(default_factory=dict)
    rules_keys: tuple[str, ...] = field(default=RULES_KEYS)


EMAIL_KIND = KindSpec(
    kind="email",
    operator_noun="email",
    default_rung=RUNG_DRAFT_ONLY,
    never_verbs=("delete",),
    verb_precedence=("delete", "forward", "send", "draft"),
    verb_patterns={
        "delete": (
            "delete_*", "*_delete", "*_delete_*",
            "trash_*", "*_trash", "*_trash_*",
        ),
        "forward": ("forward_*", "*_forward", "*_forward_*"),
        "send": ("send_*", "*_send", "*_send_*"),
        "draft": ("draft_*", "*_draft", "*_draft_*", "create_draft*"),
    },
    outward_verbs=("send", "forward", "delete"),
    denied_verbs_by_rung={
        RUNG_DRAFT_ONLY: ("send", "forward", "delete"),
        RUNG_ACT_WITH_APPROVAL: ("delete",),
        RUNG_AUTONOMOUS: ("delete",),
    },
    rung_meanings={
        RUNG_DRAFT_ONLY: (
            "Reads, labels, files, and summarizes email, and prepares "
            "drafts. It never sends, forwards, or deletes — you review "
            "and send."
        ),
        RUNG_ACT_WITH_APPROVAL: (
            "Prepares drafts and can send email, but asks you first — "
            "every message waits for your OK. It never deletes email."
        ),
        RUNG_AUTONOMOUS: (
            "Sends email on its own, within the limits you set: who it "
            "may write to and how many sends per day. It never deletes "
            "email."
        ),
    },
    promotion_consequences={
        RUNG_ACT_WITH_APPROVAL: (
            "Your assistant will be able to send email after you give "
            "an OK for each message. It still never deletes email."
        ),
        RUNG_AUTONOMOUS: (
            "Your assistant will be able to send email without asking, "
            "only to the recipients you allow and within the daily "
            "limit you set. It still never deletes email."
        ),
    },
    guidance={
        RUNG_DRAFT_ONLY: (
            "You are at \"Drafts only\" for email: read, label, file, "
            "and summarize messages, and prepare drafts for a person "
            "to review and send. Never send, forward, or delete email — "
            "and never move a message to Trash, which counts as "
            "deleting it. Send, delete, and trash tools are blocked for "
            "you; if a blocked tool call fails, report it — do not look "
            "for another way to send."
        ),
        RUNG_ACT_WITH_APPROVAL: (
            "You are at \"Asks first\" for email: you may send a "
            "message ONLY after an explicit go-ahead from your user "
            "for that specific message, in the conversation where you "
            "were asked. Show the draft, wait for the OK, then send. "
            "Never delete email, including moving it to Trash — delete "
            "and trash tools are blocked for you."
        ),
        RUNG_AUTONOMOUS: (
            "You are at \"Acts within limits\" for email: you may send "
            "without asking, but only within the operator's rules "
            "(allowed recipients, daily send limit) listed below. "
            "Anything outside those rules needs an explicit OK first. "
            "Never delete email, including moving it to Trash — delete "
            "and trash tools are blocked for you."
        ),
    },
    rung_enforcement_mode={
        # "Drafts only" denies every send/forward tool — a wall.
        RUNG_DRAFT_ONLY: "mechanical",
        # OC has no per-MCP-tool ask gate (spec §8 OQ-1, re-checked
        # 2026-06-11): "Asks first" leaves send reachable and the
        # asking is instruction. A sign, honestly labeled.
        RUNG_ACT_WITH_APPROVAL: "procedural",
        # Phase B's rung-3 daily cap is a best-effort mechanical
        # BACKSTOP (ledger-counted, pause rendered by an evolve-side
        # pass within minutes), not a wall: enforcement lags the
        # evaluation cadence and depends on the bot-side ledger
        # observing tool calls at all. The honest badge stays
        # "instructed and monitored" (spec §2.4 + Appendix B).
        RUNG_AUTONOMOUS: "procedural",
    },
)


KIND_SPECS: dict[str, KindSpec] = {
    EMAIL_KIND.kind: EMAIL_KIND,
}


# ── Integration bindings (provider names live HERE only) ─────────────────────

# How a bot exposes an integration's tools — the two discovery sources
# (spec §5.2, plugin-Gmail addition 2026-06-26):
#   - ``mcp_server``: tools come from an ``mcp.servers.<id>`` entry; the
#     known surface is the vetted catalog's ``advertised_tools`` and OC
#     denies them as ``mcp__<id>__<tool>``.
#   - ``plugin``: tools are provided bare by an Evolve plugin and listed
#     in ``tools.alsoAllow`` (e.g. plugin Gmail — ``gmail_*``); the
#     per-bot surface IS those ``alsoAllow`` entries and OC denies them
#     by their BARE tool name (deny wins over alsoAllow — oc-config
#     schema, global ``tools.deny``). The same email ladder semantics
#     apply; only discovery + the deny-entry spelling differ.
SOURCE_MCP = "mcp_server"
SOURCE_PLUGIN = "plugin"


@dataclass(frozen=True)
class IntegrationBinding:
    """Maps one known integration id to a ladder kind.

    ``integration_id`` is the bot's ``mcp.servers`` key / mcp_admin
    catalog id (spec §8 OQ-2, decided) for ``mcp_server`` sources, or a
    documented synthetic id for ``plugin`` sources (the plugin tools
    have no mcp.servers key). ``tool_filter`` scopes which of a
    multi-suite surface's tools belong to this kind — google_workspace
    exposes calendar/drive/docs tools too, and only its email surface
    is governed by the email ladder row in Phase A.

    ``source`` selects discovery + deny spelling (see ``SOURCE_*``).
    ``known_tools`` is the declared tool surface for a ``plugin`` source
    (the ``mcp_server`` analogue is the vetted catalog's
    ``advertised_tools``, read live by ``known_server_tools``); empty
    for ``mcp_server`` bindings.
    """

    integration_id: str
    kind: str
    display_name: str
    tool_filter: tuple[str, ...]
    source: str = SOURCE_MCP
    known_tools: tuple[str, ...] = ()


# Declared plugin Gmail tool surface — mirrors evolve_admin
# ``google_service.TOOL_SPECS`` gmail_* entries (the upstream source of
# truth; a parity test in the admin package guards drift). Listed as
# data here so analyzer-side discovery/render stay deterministic and free
# of a cross-package import. Of these, ``gmail_send`` (send),
# ``gmail_delete_message`` (delete), and ``gmail_trash_message`` (delete)
# classify as outward verbs and get denied per rung; the rest are
# read/label/modify-tier.
#
# Label-trash residual — CLOSED for Evolve-owned tools (2026-06-26, spec
# §1.4): applying the Gmail TRASH label is a recoverable delete, i.e. the
# ``delete`` verb. A tool-granular deny list can't block it at the
# argument level on a general labeling tool, so the fix splits the
# capability: ``google_service.gmail_label_message`` now REFUSES the TRASH
# label and routes callers to the dedicated ``gmail_trash_message``, which
# is in this surface, classifies as ``delete`` (``*_trash_*``), and is
# therefore denied at every rung like ``gmail_delete_message``. The
# never-delete wall is now honest for BOTH delete forms on the plugin and
# admin-bridge paths (the only Evolve-owned email tools). For third-party
# email MCP servers Evolve does not own, the same kind-semantics hold but
# mechanical enforcement requires a named denial target (a trash tool) to
# exist — a general label tool there falls back to procedural enforcement
# (spec §2.4 honesty), since Evolve neither owns the tool nor proxies it.
_PLUGIN_GMAIL_TOOLS: tuple[str, ...] = (
    "gmail_list_messages",
    "gmail_get_message",
    "gmail_list_labels",
    "gmail_send",
    "gmail_label_message",
    "gmail_archive_message",
    "gmail_delete_message",
    "gmail_trash_message",
    "gmail_mark_read",
    "gmail_mark_unread",
)

# Synthetic id for the plugin-provided Gmail integration. Distinct from
# the mcp.servers id ``google_workspace`` so ``binding_for`` resolves the
# correct source per bot (a bot uses one or the other), while the shared
# ``display_name`` keeps the operator-facing label identical.
PLUGIN_GMAIL_INTEGRATION_ID = "google_workspace_plugin"


INTEGRATION_BINDINGS: dict[str, IntegrationBinding] = {
    "google_workspace": IntegrationBinding(
        integration_id="google_workspace",
        kind="email",
        display_name="Google Workspace",
        tool_filter=("*gmail*",),
    ),
    PLUGIN_GMAIL_INTEGRATION_ID: IntegrationBinding(
        integration_id=PLUGIN_GMAIL_INTEGRATION_ID,
        kind="email",
        display_name="Google Workspace",
        tool_filter=("gmail_*",),
        source=SOURCE_PLUGIN,
        known_tools=_PLUGIN_GMAIL_TOOLS,
    ),
}


# ── Pure helpers (logic on kinds/verbs only) ─────────────────────────────────


def binding_for(integration_id: str) -> IntegrationBinding | None:
    return INTEGRATION_BINDINGS.get(integration_id)


def kind_spec(kind: str) -> KindSpec | None:
    return KIND_SPECS.get(kind)


def kind_tools(binding: IntegrationBinding, server_tools: list[str]) -> list[str]:
    """The subset of a server's tools that belong to the binding's kind."""
    return [
        t for t in server_tools
        if any(fnmatchcase(t, pat) for pat in binding.tool_filter)
    ]


def classify_tool(spec: KindSpec, tool_name: str) -> str | None:
    """Classify a bare tool name into a kind verb, or None (read-tier).

    Precedence order matters: ``send_draft`` is a send, while
    ``draft_reply`` is a draft — see ``verb_precedence``.
    """
    for verb in spec.verb_precedence:
        for pat in spec.verb_patterns.get(verb, ()):
            if fnmatchcase(tool_name, pat):
                return verb
    return None


def is_ladder_eligible(
    spec: KindSpec, binding: IntegrationBinding, server_tools: list[str],
) -> bool:
    """True when the integration has an outward-action surface for this
    kind. No outward tools ⇒ no ladder row (no dead affordances)."""
    return any(
        classify_tool(spec, t) in spec.outward_verbs
        for t in kind_tools(binding, server_tools)
    )


def denied_verbs(
    spec: KindSpec,
    rung: str,
    rules: Mapping[str, Any] | None,
    *,
    paused: bool = False,
) -> set[str]:
    """Verb classes the mechanical surface denies at this rung: the
    rung's table, plus the kind's never-verbs, plus rules.never.

    ``paused=True`` is the rung-3 daily-cap state (spec §1.3): hitting
    ``rules.actions_per_day`` pauses outward actions for the day, so
    every outward verb is denied on top of the rung's normal slice —
    "no outward effect" until the day rolls over, exactly the bottom
    rung's guarantee, without rewriting the posture itself.
    """
    verbs = set(spec.denied_verbs_by_rung.get(rung, ()))
    verbs.update(spec.never_verbs)
    if paused:
        verbs.update(spec.outward_verbs)
    for extra in (rules or {}).get("never", []) or []:
        if isinstance(extra, str):
            verbs.add(extra)
    return verbs


def expected_denied_tools(
    spec: KindSpec,
    binding: IntegrationBinding,
    server_tools: list[str],
    rung: str,
    rules: Mapping[str, Any] | None = None,
    *,
    paused: bool = False,
) -> list[str]:
    """Bare tool names the renderer must deny for this posture."""
    deny = denied_verbs(spec, rung, rules, paused=paused)
    return sorted(
        t for t in kind_tools(binding, server_tools)
        if classify_tool(spec, t) in deny
    )


def oc_deny_prefix(integration_id: str) -> str:
    """Ownership boundary in OC's global ``tools.deny`` list for an
    ``mcp_server`` integration: every entry the renderer owns carries
    this prefix (OpenClaw's ``mcp__<server>__<tool>`` naming).

    Plugin sources have no single prefix (their tools are bare names);
    ownership for them goes through :func:`deny_entry_is_owned`.
    """
    return f"mcp__{integration_id}__"


def oc_deny_entry(integration_id: str, tool_name: str) -> str:
    """The ``tools.deny`` spelling for one tool of an integration.

    ``mcp_server`` → ``mcp__<id>__<tool>``; ``plugin`` → the bare
    ``<tool>`` name (OC matches plugin tools in ``tools.deny`` by their
    registered bare name, the same namespace ``tools.alsoAllow`` uses to
    re-expose them — google_tools_policy)."""
    binding = binding_for(integration_id)
    if binding is not None and binding.source == SOURCE_PLUGIN:
        return tool_name
    return f"{oc_deny_prefix(integration_id)}{tool_name}"


def deny_entry_is_owned(integration_id: str, entry: str) -> bool:
    """Does a live ``tools.deny`` entry fall under this integration's
    ownership boundary (the entries the renderer may replace wholesale)?

    ``mcp_server`` → entries under the ``mcp__<id>__`` prefix.
    ``plugin`` → bare tool names matching the binding's ``tool_filter``
    (excluding any ``mcp__`` entry, so a co-resident MCP server's denies
    are never claimed). Unknown ids fall back to the mcp prefix rule,
    preserving the pre-plugin behavior for arbitrary integrations.
    """
    if not isinstance(entry, str):
        return False
    binding = binding_for(integration_id)
    if binding is not None and binding.source == SOURCE_PLUGIN:
        if entry.startswith("mcp__"):
            return False
        return any(fnmatchcase(entry, pat) for pat in binding.tool_filter)
    return entry.startswith(oc_deny_prefix(integration_id))


def known_server_tools(integration_id: str) -> list[str]:
    """Known tool surface for an integration.

    For ``plugin`` sources the surface is the binding's declared
    ``known_tools``. For ``mcp_server`` sources it is the vetted
    catalog's ``advertised_tools`` (health probes don't capture
    tools/list yet); when a catalog entry grows tools, the coherence
    check re-derives from the same source, flags the gap as
    ``autonomy_posture_drift``, and the next render adopts it — spec §3.4
    "MCP server changed its tool surface".
    """
    binding = binding_for(integration_id)
    if binding is not None and binding.known_tools:
        return list(binding.known_tools)
    try:
        from mcp_admin.catalog import default_entries
    except ImportError:  # pragma: no cover — partial installs
        return []
    for entry in default_entries():
        if entry.id == integration_id:
            return list(entry.advertised_tools)
    return []


def discover_ladder_integrations(
    config: Mapping[str, Any],
) -> list[tuple[str, IntegrationBinding, list[str]]]:
    """Enumerate a bot's ladder-eligible integrations and their per-bot
    tool surface, across both discovery sources (spec §5.2 + plugin
    addition).

    Returns ``(integration_id, binding, tools)`` triples:
      - **MCP servers** — each ``mcp.servers.<id>`` key with a known
        binding; ``tools`` is the vetted catalog surface
        (:func:`known_server_tools`).
      - **Plugin tools** — for each plugin-source binding, the bot's
        actual ``tools.alsoAllow`` entries matching the binding's
        ``tool_filter``; ``tools`` is that real per-bot subset, so a bot
        exposing only read tools stays off the ladder.

    A plugin source is suppressed when an MCP binding of the SAME kind
    was already discovered on this bot — the live MCP wiring wins and a
    kind is never double-counted. Eligibility (an outward tool present)
    is left to the caller via :func:`is_ladder_eligible`.
    """
    out: list[tuple[str, IntegrationBinding, list[str]]] = []
    seen_kinds: set[str] = set()

    mcp = config.get("mcp") if isinstance(config, Mapping) else None
    servers = (mcp or {}).get("servers") if isinstance(mcp, Mapping) else None
    if isinstance(servers, Mapping):
        for sid in sorted(servers.keys()):
            binding = binding_for(str(sid))
            if binding is None:
                continue
            out.append((str(sid), binding, known_server_tools(str(sid))))
            seen_kinds.add(binding.kind)

    tools_section = config.get("tools") if isinstance(config, Mapping) else None
    raw_allow = (tools_section or {}).get("alsoAllow") if isinstance(tools_section, Mapping) else None
    also_allow = [t for t in raw_allow if isinstance(t, str)] if isinstance(raw_allow, list) else []
    for binding in INTEGRATION_BINDINGS.values():
        if binding.source != SOURCE_PLUGIN or binding.kind in seen_kinds:
            continue
        matched = kind_tools(binding, also_allow)
        if matched:
            out.append((binding.integration_id, binding, matched))
            seen_kinds.add(binding.kind)

    return out


def next_rung_up(rung: str) -> str | None:
    idx = RUNGS.index(rung) if rung in RUNGS else -1
    if idx < 0 or idx + 1 >= len(RUNGS):
        return None
    return RUNGS[idx + 1]


def next_rung_down(rung: str) -> str | None:
    idx = RUNGS.index(rung) if rung in RUNGS else -1
    if idx <= 0:
        return None
    return RUNGS[idx - 1]


def is_promotion(from_rung: str | None, to_rung: str) -> bool:
    """True when the move widens autonomy (None = first deliberate set;
    treated as promotion only if landing above the bottom rung)."""
    from_idx = RUNGS.index(from_rung) if from_rung in RUNGS else 0
    to_idx = RUNGS.index(to_rung) if to_rung in RUNGS else 0
    return to_idx > from_idx


def action_is_promotion(expected_current_rung: Any, to_rung: Any) -> bool:
    """Direction witness for ``UpdateAutonomyPosture`` actions — the
    predicate every auto-approve lane keys its permanent upward
    exclusion on (spec §3.2).

    FAIL CLOSED: a missing or unrecognized rung on either side reads as
    a promotion. An action that can't prove it narrows must be treated
    as widening — the asymmetry is the whole point of the carve-out.
    The applier's CAS on ``expected_current_rung`` guarantees the
    direction computed here still holds at apply time.
    """
    if to_rung not in RUNGS or expected_current_rung not in RUNGS:
        return True
    return RUNGS.index(to_rung) > RUNGS.index(expected_current_rung)


def validate_rules(spec: KindSpec, rung: str, rules: Mapping[str, Any] | None) -> list[str]:
    """Validate a rules block against the closed vocabulary (spec §1.3).

    Returns a list of errors; empty = valid. Rung 3 is invalid without
    a non-empty rules block; rungs 1-2 must carry an empty block (the
    rules belong to "Acts within limits" — storing them earlier would
    imply limits that nothing enforces or displays).
    """
    errors: list[str] = []
    rules = rules or {}
    if rung not in RUNGS:
        return [f"unknown rung {rung!r} (expected one of {RUNGS})"]
    if rung != RUNG_AUTONOMOUS:
        if rules:
            errors.append(
                f"rules block is only valid at rung {RUNG_AUTONOMOUS!r}; "
                f"got rules at {rung!r}"
            )
        return errors

    if not rules:
        errors.append(
            f"rung {RUNG_AUTONOMOUS!r} requires a non-empty rules block"
        )
        return errors

    unknown = sorted(set(rules.keys()) - set(spec.rules_keys))
    if unknown:
        errors.append(f"unknown rules key(s): {unknown} (allowed: {list(spec.rules_keys)})")

    for list_key in ("reach_allow", "scope_allow"):
        if list_key in rules:
            val = rules[list_key]
            if (not isinstance(val, list) or not val
                    or not all(isinstance(x, str) and x.strip() for x in val)):
                errors.append(f"rules.{list_key} must be a non-empty list of strings")

    if "actions_per_day" in rules:
        apd = rules["actions_per_day"]
        if not isinstance(apd, int) or isinstance(apd, bool) or apd <= 0:
            errors.append("rules.actions_per_day must be a positive integer")

    if "never" in rules:
        never = rules["never"]
        known_verbs = set(spec.verb_patterns.keys())
        if not isinstance(never, list) or not all(
            isinstance(v, str) and v in known_verbs for v in never
        ):
            errors.append(
                f"rules.never entries must be kind verbs ({sorted(known_verbs)})"
            )

    return errors


def guidance_for(
    spec: KindSpec, rung: str, rules: Mapping[str, Any] | None = None,
) -> str:
    """The procedural-surface text for a posture (session systemAppend)."""
    base = spec.guidance.get(rung, "")
    if not base:
        return ""
    if rung == RUNG_AUTONOMOUS and rules:
        lines = [base, "Operator rules:"]
        reach = rules.get("reach_allow")
        if isinstance(reach, list) and reach:
            lines.append(f"  - Allowed recipients: {', '.join(map(str, reach))}")
        scope = rules.get("scope_allow")
        if isinstance(scope, list) and scope:
            lines.append(f"  - Allowed scope: {', '.join(map(str, scope))}")
        apd = rules.get("actions_per_day")
        if isinstance(apd, int) and not isinstance(apd, bool) and apd > 0:
            lines.append(f"  - Daily send limit: {apd}")
        never = rules.get("never")
        if isinstance(never, list) and never:
            lines.append(f"  - Never: {', '.join(map(str, never))}")
        return "\n".join(lines)
    return base
