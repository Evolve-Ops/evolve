"""schema.proposal — Proposal v2 dataclasses.

Spec: docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md §3.

Every Proposal carries:
  - Identity (id, schema_version, created_at, bot_id)
  - Provenance (generator_id, dimension, trigger_observations, provenance)
  - Content (problem, action, claim, revert_on_failure, risk_tag)
  - Better Engine surfacing (approval_audience, urgency, admin_surface_summary,
    conversational_pitch, guardian_annotations)
  - Lifecycle (status, snoozed_until, history)
  - Integrity (signature)

Action is a discriminated union; serialization uses a ``kind`` tag.

L1 implements only a subset of Action variants (those needed for existing
proposals + the ones legacy migration produces). Other variants land as their
generators come online in later layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union
from uuid import uuid4

from evolve_util import now_iso_offset as _utc_now_iso

from schema.generator import Surface
from schema.provenance import Provenance


# ─────────────────────────────────────────────────────────────────────────────
# Type aliases (Python Literals for enum-like fields)
# ─────────────────────────────────────────────────────────────────────────────

Status = Literal[
    "draft",
    "pending",
    "approved_auto",
    "approved_human",
    "applied",
    "succeeded",
    "failed_reverted",
    "failed_flagged",
    "failed_revert_failed",
    "rejected",
    "dismissed",
    "snoozed",
    "superseded",
    "resolved_externally",
    # Slice 3 (2026-06-04): proposal has been handed off to its
    # dispatch_target (evo / a bot / forge). The target is doing the
    # work; on result callback the status transitions to one of
    # applied | failed_flagged | (or back to pending on cancel).
    # See docs/spec-take-this-on-evo-dispatch-2026-06-04.md.
    "dispatched",
]

# Slice 3 dispatch result outcome — narrow Literal so the result
# callback can't smuggle arbitrary strings into the proposal store.
DispatchOutcome = Literal["applied", "failed", "in_process"]

ApprovalAudience = Literal["pod_operator", "bot_primary_user", "both", "none"]

# Adjacency types — populated by Adjacency Explorer on emission (L4+).
# ``None`` for proposals from non-Adjacency Explorer generators.
AdjacencyType = Literal[
    "extend_same_cell",
    "adjacent_noun",
    "adjacent_verb",
    "chain_completion",
]

Urgency = Literal[
    "security_critical",
    "operational_urgent",
    "cost_alert",
    "substrate_warn",
    "improvement",
    "hygiene",
    "whimsy",
]

Direction = Literal["up", "down", "equal"]
Fallback = Literal["revert", "flag", "escalate"]

BlastRadius = Literal["local", "bot", "platform"]
Reversibility = Literal["auto", "manual", "none"]


PROPOSAL_SCHEMA_VERSION = 2

# Surfaces that are intentionally outside the autonomous-routing allowlist.
# Any proposal whose ``risk_tag.touches`` intersects this set routes to a human
# regardless of reversibility. See spec §3.5.
IRREVERSIBILITY_SURFACES: frozenset[str] = frozenset(
    {
        "auth",
        "tools",
        "channel_config",
        "gateway_core",
        "app_install",
        "app_removal",
        "bot_specialization",
    }
)


# ─────────────────────────────────────────────────────────────────────────────
# Action variants — discriminated union via ``kind`` tag
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ConfigPatch:
    """Patch a bot's ``openclaw.json`` (wrapping existing apply.py safety gates)."""

    target_path: str
    operation: Literal["set", "unset", "merge"]
    value: Any = None
    kind: Literal["ConfigPatch"] = "ConfigPatch"


@dataclass
class ManifestUpdate:
    """Patch an app manifest on a bot.

    ``operation`` describes what the patch does semantically so the applier
    and the security evaluator can reason about it:
      - ``add_tag`` / ``remove_tag``: edit capability_tags or similar
      - ``broaden_scope`` / ``narrow_scope``: change what the app handles
      - ``add_capability`` / ``remove_capability``: functional expansion/contraction
      - ``update_keywords``: edit keyword lists used for routing
      - ``set_fields``: set arbitrary top-level manifest fields from
        ``fields`` (e.g. ``{"test_exemption_reason": "..."}``).
        Used by guardians (e.g. test_gate_backfill) when there is a
        mechanically-correct fix that doesn't rewrite the app's
        capability surface.
      - ``add_files``: append new entries to the manifest's ``files``
        list (deduped against the current value at apply time). Used
        by the app-posture reflection's ``fold_orphan`` proposals to
        attach a previously-unmanifested workspace file to an existing
        app. ``fields`` carries one key — ``files: [path1, path2, ...]``
        — and the applier reads the current manifest at apply time
        before merging, so a proposal sitting in pending/ for days
        won't clobber concurrent file additions.
    """

    app_id: str
    operation: Literal[
        "add_tag",
        "remove_tag",
        "broaden_scope",
        "narrow_scope",
        "add_capability",
        "remove_capability",
        "update_keywords",
        "set_fields",
        "add_files",
    ]
    fields: dict = field(default_factory=dict)
    kind: Literal["ManifestUpdate"] = "ManifestUpdate"


@dataclass
class MemoryCurate:
    """Prune or consolidate entries in the bot's memory file.

    In L1 the applier is a stub; real implementation lands in L4+ Deprecator
    or when L3's Security Warden needs memory-write capability for redaction.
    """

    bot_id: str
    operation: Literal["prune_stale", "consolidate", "rewrite"]
    target_selector: str = ""
    replacement: str = ""
    kind: Literal["MemoryCurate"] = "MemoryCurate"


@dataclass
class AgentsAppend:
    """Append a section or note to AGENTS.md (additive only)."""

    bot_id: str
    section: str
    content: str
    kind: Literal["AgentsAppend"] = "AgentsAppend"


@dataclass
class InstallApp:
    """Install a gallery app. Always human-approval (touches app_install)."""

    app_id: str
    source: Literal["gallery", "custom"] = "gallery"
    kind: Literal["InstallApp"] = "InstallApp"


@dataclass
class WorkflowInstruction:
    """Write a markdown instruction file in the bot's workspace."""

    bot_id: str
    path: str
    content: str
    kind: Literal["WorkflowInstruction"] = "WorkflowInstruction"


@dataclass
class TierAdjustment:
    """Adjust tier routing for a bot or a class of sessions."""

    bot_id: str
    target_class: str
    new_tier: str
    kind: Literal["TierAdjustment"] = "TierAdjustment"


@dataclass
class VetoAnnotation:
    """Guardian-only: carries risk info; does not mutate state when applied."""

    reason: str
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    kind: Literal["VetoAnnotation"] = "VetoAnnotation"


@dataclass
class Investigation:
    """Surfaces the situation to a human; no state change on apply."""

    context: str
    kind: Literal["Investigation"] = "Investigation"


@dataclass
class DeprecateApp:
    """Mark an app deprecated (L6 Deprecator; stub in L1)."""

    app_id: str
    reason: Literal["unused", "replaced_by", "user_requested"]
    days_inactive: int = 0
    replacement_app_id: str | None = None
    kind: Literal["DeprecateApp"] = "DeprecateApp"


@dataclass
class SoulEdit:
    """Edit SOUL.md (L6 Persona Tuner; stub in L1).

    Always routes to human approval — SOUL changes are personality-defining.
    """

    bot_id: str
    target: Literal["soul", "agents"]
    operation: Literal["append_section", "replace_section", "refine_line"]
    content: str
    anchor: str | None = None
    rationale: str = ""
    kind: Literal["SoulEdit"] = "SoulEdit"


@dataclass
class ThrottleGenerator:
    """Reduce another generator's capacity (L6 Evolve Watchdog; stub in L1)."""

    generator_id: str
    throttle_type: Literal["reduce_cadence", "reduce_budget", "lower_confidence"]
    new_value: str
    reason: str = ""
    revert_after_days: int | None = None
    kind: Literal["ThrottleGenerator"] = "ThrottleGenerator"


@dataclass
class PauseGenerator:
    """Temporarily disable a generator (L6 Evolve Watchdog; stub in L1)."""

    generator_id: str
    reason: str = ""
    resume_after_days: int | None = None
    kind: Literal["PauseGenerator"] = "PauseGenerator"


@dataclass
class RetireOrphan:
    """Retire a workspace orphan file: archive its content and exclude
    it from future posture reviews.

    Used by app_posture_reflection's ``delete_orphan`` proposals. The
    applier:

      1. Reads the file at /Users/<bot>/.openclaw/workspace/<path>
         (evolve has ACL read on .openclaw/).
      2. Copies the content to {shared_dir}/app_posture/<bot>/
         orphan_archive/<YYYY-MM-DD>-<basename> (evolve owns
         shared_dir).
      3. Appends ``path`` to {shared_dir}/app_posture/<bot>/
         orphan_exclusions.json so future weekly reviews skip it.

    The file in the workspace is **not unlinked** — evolve has no
    delete grant on bot workspaces, and physical removal would require
    new infrastructure (per-bot launchd helper or new sudoers grant).
    Retire-and-exclude is the safe, reversible analog: the orphan
    stops appearing in reflection and there's a content snapshot
    operators can recover from. Bots can clean up the actual file at
    their own pace if they want.

    Reversible via the standard arbiter revert path: the applier
    captures the prior exclusions list in its snapshot and revert
    removes the path from exclusions (the archive copy stays — it's
    cheap to leave around and useful for audit).
    """

    bot_id: str
    path: str  # workspace-relative path (e.g. "ops/loose-notes.md")
    kind: Literal["RetireOrphan"] = "RetireOrphan"


@dataclass
class BuildApp:
    """Build a new app from a fully-formed manifest, dispatched to the forge.

    The proposal carries the complete manifest. The applier persists the
    manifest to ``{shared_dir}/applications/{bot_id}/{app_id}.json`` and
    creates a ``ForgeJob`` (synthetic ``chat-<hex>`` pkg_id, install step
    sequence) which is then run asynchronously. The proposal lands in
    ``applied`` while forge runs; a forge-status sweep transitions it to
    ``succeeded`` or ``failed_flagged`` based on the final ForgeJob state.

    Distinct from ``InstallApp`` which targets an *existing* gallery
    package — BuildApp creates a brand-new app that doesn't exist yet.
    """

    bot_id: str
    app_id: str  # slug; used as manifest filename + ForgeJob.app_id
    app_name: str  # human-readable
    manifest: dict  # full manifest payload, written verbatim to disk
    kind: Literal["BuildApp"] = "BuildApp"


@dataclass
class InstallMcpServer:
    """Install an MCP server on a bot from a catalog entry.

    Spec: docs/spec-mcp-administration-2026-05-10.md §3.5 + §5.2.
    The applier generates a wrapper script under {shared_dir}/mcp/launchers/,
    writes the server into the bot's openclaw.json mcp.servers, and
    updates the pod allowlist. RevertPlan is a paired RemoveMcpServer.

    extra_args carries per-install positional arguments appended to the
    catalog entry's args. The wrapper script ends in ``"$@"`` so anything
    in ``mcp.servers[server_id].args`` flows straight through to the real
    binary. Used by per-skill wrappers that bind a catalog server to a
    user-supplied target — the Obsidian skill installs ``catalog_id="filesystem"``
    with ``extra_args=[vault_path]`` so the bot's filesystem MCP is
    scoped to the vault directory rather than the whole disk.
    """

    bot_id: str
    server_id: str  # also the key used in mcp.servers
    catalog_id: str
    env_bindings: dict = field(default_factory=dict)  # var_name -> "keystore:<key>"
    extra_args: list[str] = field(default_factory=list)  # appended to entry.args at install
    kind: Literal["InstallMcpServer"] = "InstallMcpServer"


@dataclass
class RemoveMcpServer:
    """Remove an MCP server from a bot's openclaw.json and the allowlist."""

    bot_id: str
    server_id: str
    kind: Literal["RemoveMcpServer"] = "RemoveMcpServer"


@dataclass
class UpdateMcpServerConfig:
    """Mutate env bindings for an already-installed MCP server.

    Phase B scope: env_bindings only. Changes to command/args/transport
    require a remove + install pair (because they cross the catalog-entry
    boundary).
    """

    bot_id: str
    server_id: str
    env_bindings: dict = field(default_factory=dict)
    kind: Literal["UpdateMcpServerConfig"] = "UpdateMcpServerConfig"


@dataclass
class EnablePluginEntry:
    """Enable an OpenClaw plugin entry on a bot.

    Spec: docs/spec-plugin-inventory-2026-05-10.md §3.4. Sets
    ``plugins.entries.<id>.enabled = true`` (creating the entry shell
    if needed) and triggers a gateway restart. Revert reinstates the
    prior enabled flag.
    """

    bot_id: str
    plugin_name: str
    kind: Literal["EnablePluginEntry"] = "EnablePluginEntry"


@dataclass
class DisablePluginEntry:
    """Disable an OpenClaw plugin entry on a bot.

    Sets ``enabled = false`` and preserves the entry's other config so
    a re-enable is cheap. The applier refuses if the plugin is in the
    baseline's required_plugins set for that bot.
    """

    bot_id: str
    plugin_name: str
    kind: Literal["DisablePluginEntry"] = "DisablePluginEntry"


@dataclass
class UpdatePluginAllowDeny:
    """Replace a bot's ``plugins.allow`` / ``plugins.deny`` lists.

    Either field may be ``None`` to mean "leave this side untouched";
    pass an empty list to clear. The applier validates that the
    proposed allow list doesn't remove a baseline-required plugin and
    doesn't add a baseline-denied plugin.
    """

    bot_id: str
    allow: list[str] | None = None
    deny: list[str] | None = None
    kind: Literal["UpdatePluginAllowDeny"] = "UpdatePluginAllowDeny"


@dataclass
class AddSignalCollection:
    """Proposal to extend evolve's own observation layer.

    Spec: docs/spec-proposal-synthesizer-2026-05-10.md §6.3.

    Emitted by the proposal synthesizer when investigation revealed that
    information evolve doesn't currently collect would have been
    load-bearing for a candidate's evaluation. The target is **evolve
    itself**, not a bot — applying this proposal means an engineer
    writes a new monitor that emits the named signal type.

    Routed with ``bot_id="<pod>"`` and ``dimension="observability"``.
    No reverse-applier today: an engineer reads the proposal, writes
    the monitor, ships it. The Proposal lands in ``applied`` when the
    operator marks it done.
    """

    producer: str  # which monitor should emit ("cost_watchdog", new ID, etc.)
    signal_type: str  # new signal type name (e.g. "tool_use_pattern")
    description: str  # what the synthesizer needed and couldn't get
    suggested_data_shape: dict = field(default_factory=dict)
    motivating_candidate_ids: list[str] = field(default_factory=list)
    estimated_impact: str = ""  # one-line: which prior candidates would have benefited
    kind: Literal["AddSignalCollection"] = "AddSignalCollection"


@dataclass
class UpdatePluginConfig:
    """Mutate the ``config`` block of an OpenClaw plugin entry on a bot.

    Spec: docs/spec-plugin-inventory-2026-05-10.md §3.4.

    Three operations:
      - ``set_keys`` — assign each key in ``fields`` into the plugin's
        config block, leaving other keys untouched. Nested paths use
        dot notation ("webSearch.apiKey").
      - ``unset_keys`` — remove each top-level key in ``fields`` from
        the plugin's config block (``fields`` values are ignored).
      - ``replace_block`` — replace the plugin's entire config block
        with ``fields``. High-impact; operator should confirm.

    Secrets routed via the keystore should still land in plugin config
    by-value today (the keystore wrapper-script pattern is MCP-only).
    Operators rotate by issuing a new UpdatePluginConfig with the
    fresh value.
    """

    bot_id: str
    plugin_name: str
    operation: Literal["set_keys", "unset_keys", "replace_block"]
    fields: dict = field(default_factory=dict)
    kind: Literal["UpdatePluginConfig"] = "UpdatePluginConfig"


@dataclass
class UpdatePluginLoadPaths:
    """Mutate a bot's ``plugins.load.paths`` list.

    Operations:
      - ``add_path`` / ``remove_path`` — single-path mutation against
        the bot's current list. The applier validates additions
        against a small whitelist of trusted directories (spec §5.4);
        non-whitelist additions require an explicit
        UpdatePluginBaseline.set_pod_default to expand the
        expected_load_paths first.

    Per-bot load_path changes are rare; the common case is updating
    the pod-wide expected_load_paths via UpdatePluginBaseline.
    """

    bot_id: str
    operation: Literal["add_path", "remove_path"]
    path: str = ""
    kind: Literal["UpdatePluginLoadPaths"] = "UpdatePluginLoadPaths"


@dataclass
class UpdatePluginBaseline:
    """Mutate ``{shared_dir}/policy/plugin-baseline.json``.

    Phase B exposes two operations:
      - ``set_pod_default`` — replace fields in ``pod_default`` (keys
        from ``fields`` are assigned; unspecified keys preserved).
      - ``set_bot_override`` — replace the per-bot override for
        ``bot_id`` with ``fields`` (or remove if ``fields`` is empty).

    Both run through human approval; security_warden rejects removals
    of denylist entries.
    """

    operation: Literal["set_pod_default", "set_bot_override"]
    bot_id: str = ""
    fields: dict = field(default_factory=dict)
    kind: Literal["UpdatePluginBaseline"] = "UpdatePluginBaseline"


@dataclass
class EnableWebhookIngress:
    """Configure + enable a bot's openclaw.json hooks block.

    Spec: docs/spec-hook-governance-2026-05-10.md §3.5. Operator-
    initiated. Sets hooks.enabled=true with the supplied token /
    path / agent allowlist / session-key-prefix allowlist /
    initial mappings / optional transformsDir. The applier refuses
    if the token field is empty or allowed_agent_ids is empty
    (spec auto-reject — ingress without a token + agent allowlist
    is unbounded blast radius).
    """

    bot_id: str
    token: str
    path: str = "/hooks"
    allowed_agent_ids: list[str] = field(default_factory=list)
    allowed_session_key_prefixes: list[str] = field(default_factory=list)
    mappings: list[dict] = field(default_factory=list)
    transforms_dir: str = ""
    max_body_bytes: int = 65536
    kind: Literal["EnableWebhookIngress"] = "EnableWebhookIngress"


@dataclass
class DisableWebhookIngress:
    """Set hooks.enabled=false on a bot. Keeps the rest of the config
    block intact for audit trail; remove the block entirely with a
    separate UpdateHookBaseline operation if desired.
    """

    bot_id: str
    kind: Literal["DisableWebhookIngress"] = "DisableWebhookIngress"


@dataclass
class UpdateWebhookMapping:
    """Add / remove / replace a single mapping in hooks.mappings[].

    Operations:
      - ``add``: append ``mapping`` to the list
      - ``remove``: drop the mapping whose ``id`` matches ``mapping_id``
      - ``replace``: replace the mapping with matching ``id`` with ``mapping``
    """

    bot_id: str
    operation: Literal["add", "remove", "replace"]
    mapping_id: str = ""  # required for remove / replace
    mapping: dict = field(default_factory=dict)
    kind: Literal["UpdateWebhookMapping"] = "UpdateWebhookMapping"


@dataclass
class UpdatePluginHookPolicy:
    """Set or clear a plugin's hook policy flags on a bot.

    ``allow_conversation_access`` / ``allow_prompt_injection`` are
    optional — pass None to leave that side untouched. The applier
    refuses to set ``allow_prompt_injection=true`` for any plugin
    not in the baseline's ``trusted_prompt_mutators`` allowlist.
    """

    bot_id: str
    plugin_name: str
    allow_conversation_access: bool | None = None
    allow_prompt_injection: bool | None = None
    kind: Literal["UpdatePluginHookPolicy"] = "UpdatePluginHookPolicy"


@dataclass
class UpdateExecApproval:
    """Add or revoke an approved-command pattern in a bot's exec-approvals.json.

    Spec: docs/spec-permission-posture-2026-05-10.md §3.4.

    Operations:
      - ``add`` — write a raw pattern into ``agents.<agent_id>.approvals``
        (or ``defaults`` when scope='defaults').
      - ``revoke`` — remove every raw entry whose canonical form matches
        the provided ``pattern`` (canonical or raw both accepted).

    Auto-reject (spec §5.4): refuses ``add`` if the canonical form of
    the pattern matches a denylist regex. Operators wanting to bypass
    a denylist rule must first edit the baseline.
    """

    bot_id: str
    operation: Literal["add", "revoke"]
    pattern: str  # raw for add, canonical-or-raw for revoke
    agent_id: str = "main"
    scope: Literal["agent", "defaults"] = "agent"
    kind: Literal["UpdateExecApproval"] = "UpdateExecApproval"


@dataclass
class UpsertCronJob:
    """Add or modify a cron job in a bot's .openclaw/cron/jobs.json.

    Spec: docs/spec-permission-posture-2026-05-10.md §3.4 + §5.4.

    ``job`` is the full job dict (id, name, enabled, schedule, payload).
    Existing jobs with the same id are replaced; otherwise a new job is
    appended.

    Auto-reject:
      - Refuses an ``agentTurn`` payload missing both turn-cap and
        budget-cap (open question §10.1 — caps are required at the
        payload level when the baseline says so).
      - Refuses a payload that matches the cron denylist.
    """

    bot_id: str
    job: dict = field(default_factory=dict)
    kind: Literal["UpsertCronJob"] = "UpsertCronJob"


@dataclass
class RemoveCronJob:
    """Remove a cron job by id from a bot's .openclaw/cron/jobs.json."""

    bot_id: str
    job_id: str = ""
    kind: Literal["RemoveCronJob"] = "RemoveCronJob"


@dataclass
class UpdatePermissionBaseline:
    """Mutate {shared_dir}/policy/permission-baseline.json.

    Spec: docs/spec-permission-posture-2026-05-10.md §3.4.

    Operations:
      - ``set_pod_default`` — assign each dotpath in ``fields`` into
        ``pod_default.permission_config``; other keys preserved.
      - ``set_bot_override`` — replace ``per_bot_overrides[bot_id]``'s
        permission_config block with ``fields`` (or remove if empty).
      - ``set_denylist_patterns`` — set the approval or cron denylist
        list. ``fields = {surface: 'approvals'|'cron', patterns: [...]}``.
        Auto-reject refuses removals (only additions allowed in v1).

    The baseline file is under ``{shared_dir}``, owned by the evolve
    user — direct writes, no /tmp staging.
    """

    operation: Literal["set_pod_default", "set_bot_override", "set_denylist_patterns"]
    bot_id: str = ""
    fields: dict = field(default_factory=dict)
    kind: Literal["UpdatePermissionBaseline"] = "UpdatePermissionBaseline"


@dataclass
class UpdatePermissionConfig:
    """Mutate one or more permission-config fields in a bot's openclaw.json.

    Spec: docs/spec-permission-posture-2026-05-10.md §3.4.

    `fields` is a dict mapping dotpath → new value, restricted to the
    PermissionConfig field set (tools.exec.security, tools.exec.ask,
    tools.exec.host, tools.fs.workspaceOnly, tools.web.search.enabled,
    tools.web.fetch.enabled, commands.native, commands.nativeSkills,
    commands.elevated, commands.useAccessGroups, commands.ownerAllowFrom).
    Other paths are rejected. Sandbox posture is intentionally omitted —
    see permissions/baseline.py for the OC-schema rationale (valid path is
    agents.defaults.sandbox.mode, not a top-level boolean).

    Inline auto-reject guards refuse:
      - `tools.exec.ask` set to "off" on any bot.
      - composite combo `security=full + ask=off`.
    Operators who genuinely want those postures must edit the baseline
    first (deliberate, audited).
    """

    bot_id: str
    fields: dict = field(default_factory=dict)
    kind: Literal["UpdatePermissionConfig"] = "UpdatePermissionConfig"


@dataclass
class UpdateAutonomyPosture:
    """Set a bot's per-integration autonomy posture (the ladder rung).

    Spec: docs/spec-autonomy-ladder-2026-06-10.md §3.2 (Phase B). The
    applier writes through ``autonomy.store.set_posture`` with
    ``set_by: proposal:<id>`` and re-renders enforcement — the same
    single write path the Permissions-tab UI uses.

    ``expected_current_rung`` is REQUIRED in spirit and enforced by the
    applier: it is both the CAS guard (a posture that moved since the
    proposal was authored fails loudly instead of clobbering a newer
    decision) and the direction witness — every auto-approve lane
    decides promotion-vs-demotion from (expected_current_rung → rung),
    and the CAS guarantees that direction still holds at apply time.

    **Upward promotion is permanently excluded from every auto-approve
    lane** (spec §3.2: "auto-apply is forbidden for promotions,
    including any future low-risk auto-approve lane"): see
    ``arbiter.routing.is_autonomous_eligible``,
    ``eligibility.classify_proposal``, and evo's
    ``action.proposal.apply`` validate. Demotion proposals may use
    whatever auto-lanes exist — narrowing is always safe to apply.

    ``rules`` is required non-empty iff ``rung`` is
    ``autonomous_within_rules`` (validated by the store against the
    closed §1.3 vocabulary).
    """

    bot_id: str
    integration_id: str
    rung: str
    rules: dict = field(default_factory=dict)
    expected_current_rung: "str | None" = None
    note: str = ""
    kind: Literal["UpdateAutonomyPosture"] = "UpdateAutonomyPosture"


@dataclass
class UpdateContentScanCatalog:
    """Mutate {shared_dir}/policy/content-scan-patterns.json.

    Spec: docs/spec-prompt-injection-scanner-2026-05-10.md §3.5.

    Three operations:
      - ``add_pattern`` — fields = {pattern: {...one Pattern dict...}}
      - ``remove_pattern`` — fields = {pattern_id: "..."}
      - ``set_evolve_markers_allowlist`` — fields = {markers: [...]}

    The catalog is observation-only — adding/removing patterns changes what
    the scanner flags, but no bot config is touched and no gateway restart
    is needed.
    """

    operation: Literal["add_pattern", "remove_pattern", "set_evolve_markers_allowlist"]
    fields: dict = field(default_factory=dict)
    kind: Literal["UpdateContentScanCatalog"] = "UpdateContentScanCatalog"


@dataclass
class UpdateHookBaseline:
    """Mutate {shared_dir}/policy/hook-baseline.json.

    Three operations:
      - ``set_webhook_ingress`` — fields = {enabled, rationale}
      - ``set_plugin_policy`` — fields = {plugin_name, allow_conversation_access,
        allow_prompt_injection, rationale}; replaces the named plugin's
        expectation (or adds if absent).
      - ``set_trusted_mutators`` — fields = {plugins: [...]}; replaces the
        trusted_prompt_mutators allowlist.

    Phase B's auto-reject (mirroring security_warden patterns) refuses
    set_plugin_policy with allow_prompt_injection=true unless
    plugin_name is already in trusted_prompt_mutators (closing the
    "add yourself to the trust list and then enable injection" loop).
    """

    operation: Literal["set_webhook_ingress", "set_plugin_policy", "set_trusted_mutators"]
    fields: dict = field(default_factory=dict)
    kind: Literal["UpdateHookBaseline"] = "UpdateHookBaseline"


@dataclass
class ReconcileModelCatalog:
    """Reconcile a bot's `agents.defaults.models` catalog with its tier
    definitions and (optionally) the RECOMMENDED registry for credentialed
    providers.

    Background: OpenClaw treats `agents.defaults.models` as the runtime
    whitelist. Tier entries naming non-catalog models are silently
    dropped at runtime — the tier slot effectively disappears.
    Discovered 2026-05-28 with team_bot_a, whose catalog had only Anthropic
    models but tiers referenced google/xai/openai.

    `add_models` lists model_ids the applier should add to the catalog.
    The applier reads the bot's current catalog, appends the missing
    entries (deduped), and writes back via `oc_full_config_set` —
    NEVER replaces existing catalog entries, so operators don't lose
    manual additions when this auto-applies.

    Sources of `add_models`:
      - `catalog_tier_drift` generator (the primary producer) — fills
        in models that tiers reference but catalog doesn't have
      - operators via /api/wizard/seed-model-config — bootstraps a
        sensible default catalog for a credentialed provider
    """

    bot_id: str
    add_models: list = field(default_factory=list)
    kind: Literal["ReconcileModelCatalog"] = "ReconcileModelCatalog"


@dataclass
class AdoptModel:
    """Adopt a discovered model into the pod's rung catalog.

    Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §Addendum (A).
    Emitted by the ``model_discovery`` generator (replacing the prior
    Investigation) when a provider's live listing surfaces a model that
    sits in no rung. Accepting the proposal is the operator action that
    edits ``network.json::models.rungs`` / ``roles`` / ``roleCaps``.

    Generator-authored fields (the suggestion):
      - ``provider`` / ``model_id`` — model identity (``qualified_id`` is
        ``"{provider}/{model_id}"`` and re-derived by the applier).
      - ``rung_slug``      — suggested rung slug (family heuristic, e.g.
        ``"fable-class"``). If the slug already exists in the pod catalog,
        the applier extends that rung's cluster rather than creating one.
      - ``position``       — suggested cost-rank insertion index into the
        pod ``rungs`` array (0 = cheapest end). Clamped to ``[0, len]``.
      - ``cost_class``     — suggested ``costClass`` for a newly-created
        rung (``"low" | "medium" | "high" | "premium"``). Ignored when
        extending an existing rung.
      - ``evidence``       — listing fields (context window, max output,
        capability flags) the heuristic cited.

    Operator choices, patched onto the action at approval time by the
    ``/act`` endpoint (NEVER pre-selected by the generator — adopting a
    frontier model and arming it are separate deliberate acts):
      - ``role_mapping``   — ``"none"`` (default; dormant catalog entry)
        or a role id (``fast`` | ``standard`` | ``power`` | ``max`` |
        ``judge``) to re-point at the new rung.
      - ``cap_per_day``    — when mapping ``max``/``power``, the seed for
        ``roleCaps.<role>.maxPerDayPerBot`` (default 5 for ``max`` when
        omitted). ``None`` means "leave roleCaps untouched".
    """

    provider: str
    model_id: str
    rung_slug: str
    position: int = 0
    cost_class: str = "medium"
    evidence: dict = field(default_factory=dict)
    # Operator choices (approval-time). Defaults = adopt-as-dormant.
    role_mapping: str = "none"
    cap_per_day: int | None = None
    # Cluster placement when EXTENDING an existing rung. Default ``None`` =
    # append (a newly-discovered alternative joins as the lowest-priority
    # fallback, never preempting the operator's chosen primary). The version-
    # freshness path sets this to the predecessor's qualified id so the newer
    # version is spliced in AHEAD of the model it upgrades — otherwise the
    # resolver (which routes to the FIRST credentialed member of ``models[]``,
    # NOT the newest) would keep routing to the predecessor and the "update to
    # latest" would be a silent no-op. Ignored when a rung is being created.
    insert_before: str | None = None
    kind: Literal["AdoptModel"] = "AdoptModel"


# Allowed dotpath whitelist for UpdateAgentDefaults. The applier refuses
# any path outside this set. Empty as of Phase 4b of the cost-cap
# normalization (spec: docs/spec-cost-caps-2026-06-05.md): the two knobs
# that previously routed through this applier (cacheRetention,
# sessionBudgetCapUsd) now live in better-engine-config and are written
# via /api/arbiter/bot-setup directly. No automated RSI path for either
# knob today; a future generator that wants to tune them would propose
# to BE config via a new applier, not via the sandbox-override pattern.
#
# The action type + the eligibility metadata stay (eligibility.py still
# decides it's low-risk-decidable in case a future PR widens the
# whitelist), but the applier currently has nothing to apply.
_UPDATE_AGENT_DEFAULTS_ALLOWED_DOTPATHS: frozenset[str] = frozenset()


# Type-validation table for each allowed dotpath. Empty post-Phase-4b
# (see comment on _UPDATE_AGENT_DEFAULTS_ALLOWED_DOTPATHS above).
_UPDATE_AGENT_DEFAULTS_VALUE_VALIDATORS: dict[str, object] = {}


@dataclass
class UpdateAgentDefaults:
    """Mutate a member-bot's ``agents.defaults.*`` knobs via the sandbox
    overrides file, with a narrow whitelist of allowed dotpaths.

    Spec: docs/spec-cache-retention-tunable-2026-05-30 (PR A) for the
    motivating knob; docs/spec-openclaw-json-derived-artifact-2026-05-24.md
    §7 for the L2 applier pattern (overrides → materialize → kickstart).

    Motivating incident: team-bot-a session 3d5cde22 (2026-05-29) cost
    $7.67 — 92% from Anthropic prompt-cache writes that TTL'd at the
    5-minute default before each reuse on a human-paced Slack DM
    cadence. PR A exposed the ``cacheRetention`` knob to operators;
    this action lets a generator (PR F's ``cache_ttl_tuner``) flip the
    knob from "short" to "long" autonomously when telemetry says it
    will help.

    ``fields`` is a dict mapping dotpath → value. Today only one
    dotpath is whitelisted (``agents.defaults.models.*.params.cacheRetention``);
    the ``*`` wildcard is intentional and signals fan-out semantics to
    the materializer (one logical override → every Anthropic model in
    the catalog gets the value). Widening the whitelist is a future PR.

    Value validation is per-dotpath: cacheRetention accepts the enum
    ``"short"`` | ``"long"``. The applier refuses any other value.
    """

    bot_id: str
    fields: dict = field(default_factory=dict)
    kind: Literal["UpdateAgentDefaults"] = "UpdateAgentDefaults"


Action = Union[
    ConfigPatch,
    ManifestUpdate,
    MemoryCurate,
    AgentsAppend,
    InstallApp,
    WorkflowInstruction,
    TierAdjustment,
    VetoAnnotation,
    Investigation,
    DeprecateApp,
    SoulEdit,
    ThrottleGenerator,
    PauseGenerator,
    RetireOrphan,
    BuildApp,
    InstallMcpServer,
    RemoveMcpServer,
    UpdateMcpServerConfig,
    EnablePluginEntry,
    DisablePluginEntry,
    UpdatePluginAllowDeny,
    UpdatePluginBaseline,
    UpdatePluginConfig,
    UpdatePluginLoadPaths,
    EnableWebhookIngress,
    DisableWebhookIngress,
    UpdateWebhookMapping,
    UpdatePluginHookPolicy,
    UpdateHookBaseline,
    AddSignalCollection,
    UpdateContentScanCatalog,
    UpdatePermissionConfig,
    UpdateAutonomyPosture,
    UpdateExecApproval,
    UpsertCronJob,
    RemoveCronJob,
    UpdatePermissionBaseline,
    ReconcileModelCatalog,
    AdoptModel,
    UpdateAgentDefaults,
]

# Registry of Action kinds → classes, for serialization + applier dispatch.
_ACTION_KIND_REGISTRY: dict[str, type] = {
    "ConfigPatch": ConfigPatch,
    "ManifestUpdate": ManifestUpdate,
    "MemoryCurate": MemoryCurate,
    "AgentsAppend": AgentsAppend,
    "InstallApp": InstallApp,
    "WorkflowInstruction": WorkflowInstruction,
    "TierAdjustment": TierAdjustment,
    "VetoAnnotation": VetoAnnotation,
    "Investigation": Investigation,
    "DeprecateApp": DeprecateApp,
    "SoulEdit": SoulEdit,
    "ThrottleGenerator": ThrottleGenerator,
    "PauseGenerator": PauseGenerator,
    "RetireOrphan": RetireOrphan,
    "BuildApp": BuildApp,
    "InstallMcpServer": InstallMcpServer,
    "RemoveMcpServer": RemoveMcpServer,
    "UpdateMcpServerConfig": UpdateMcpServerConfig,
    "EnablePluginEntry": EnablePluginEntry,
    "DisablePluginEntry": DisablePluginEntry,
    "UpdatePluginAllowDeny": UpdatePluginAllowDeny,
    "UpdatePluginBaseline": UpdatePluginBaseline,
    "UpdatePluginConfig": UpdatePluginConfig,
    "UpdatePluginLoadPaths": UpdatePluginLoadPaths,
    "EnableWebhookIngress": EnableWebhookIngress,
    "DisableWebhookIngress": DisableWebhookIngress,
    "UpdateWebhookMapping": UpdateWebhookMapping,
    "UpdatePluginHookPolicy": UpdatePluginHookPolicy,
    "UpdateHookBaseline": UpdateHookBaseline,
    "AddSignalCollection": AddSignalCollection,
    "UpdateContentScanCatalog": UpdateContentScanCatalog,
    "UpdatePermissionConfig": UpdatePermissionConfig,
    "UpdateAutonomyPosture": UpdateAutonomyPosture,
    "UpdateExecApproval": UpdateExecApproval,
    "UpsertCronJob": UpsertCronJob,
    "RemoveCronJob": RemoveCronJob,
    "UpdatePermissionBaseline": UpdatePermissionBaseline,
    "ReconcileModelCatalog": ReconcileModelCatalog,
    "AdoptModel": AdoptModel,
    "UpdateAgentDefaults": UpdateAgentDefaults,
}


def action_from_dict(data: dict) -> Action:
    """Construct the right Action variant from a dict with a ``kind`` tag."""
    kind = data.get("kind")
    if kind not in _ACTION_KIND_REGISTRY:
        raise ValueError(f"Unknown Action kind: {kind!r}")
    cls = _ACTION_KIND_REGISTRY[kind]
    # Drop unknown fields to be forgiving of small schema drift
    field_names = {f for f in cls.__dataclass_fields__}
    filtered = {k: v for k, v in data.items() if k in field_names}
    return cls(**filtered)


def action_to_dict(action: Action) -> dict:
    """Serialize an Action variant to a dict."""
    result = {}
    for name in action.__dataclass_fields__:  # type: ignore[attr-defined]
        value = getattr(action, name)
        result[name] = value
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Claim and revert plan
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Claim:
    """A proposal's falsifiable claim.

    After the applier executes, the verify daemon (L2) resolves ``metric`` at
    ``apply_time + window_days``, computes the delta, and determines whether
    ``direction × magnitude`` held.

    Fallback:
      - ``revert``: if claim failed, apply the RevertPlan
      - ``flag``: leave the change; surface for human review (terminal state
        ``failed_flagged``)
      - ``escalate``: revert and surface to operator (emit an escalation
        proposal)
    """

    metric: str
    direction: Direction
    magnitude: float
    window_days: int
    baseline: float
    fallback: Fallback = "revert"
    # When True, the applier overwrites ``baseline`` with a live metric reading
    # at apply time (arbiter.apply._fill_apply_time_baseline), instead of trusting
    # the value the generator authored at creation. For claims whose true
    # pre-change baseline is only knowable when the change actually lands — e.g.
    # Budget Hawk's cost.daily_usd, where the proposal may queue for days before
    # applying. Generators that author a real creation-time baseline (Efficiency
    # Hawk, Cache TTL Tuner) leave this False so their baseline is preserved.
    baseline_at_apply: bool = False

    def __post_init__(self) -> None:
        if not self.metric:
            raise ValueError("Claim.metric must be non-empty")
        if self.window_days <= 0:
            raise ValueError(f"Claim.window_days must be > 0; got {self.window_days}")
        if self.magnitude < 0:
            raise ValueError(
                f"Claim.magnitude must be >= 0; got {self.magnitude}"
            )

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "direction": self.direction,
            "magnitude": self.magnitude,
            "window_days": self.window_days,
            "baseline": self.baseline,
            "fallback": self.fallback,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Claim":
        return cls(
            metric=data["metric"],
            direction=data["direction"],
            magnitude=float(data["magnitude"]),
            window_days=int(data["window_days"]),
            baseline=float(data["baseline"]),
            fallback=data.get("fallback", "revert"),
        )


@dataclass
class RevertPlan:
    """How to undo an applied proposal if its claim fails.

    Captured at apply time by the applier. Generators never fill this in
    themselves; they only declare ``fallback`` on the Claim.
    """

    before_snapshot: dict
    revert_action: Action
    expires_at: str  # ISO8601; after this, snapshot is GC'd on success path

    def to_dict(self) -> dict:
        return {
            "before_snapshot": dict(self.before_snapshot),
            "revert_action": action_to_dict(self.revert_action),
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RevertPlan":
        return cls(
            before_snapshot=dict(data.get("before_snapshot") or {}),
            revert_action=action_from_dict(data["revert_action"]),
            expires_at=data["expires_at"],
        )


# ─────────────────────────────────────────────────────────────────────────────
# Risk tag
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class RiskTag:
    """Blast radius + reversibility + surfaces touched. Drives routing."""

    blast_radius: BlastRadius
    reversibility: Reversibility
    touches: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Normalize touches to a list of unique, lowercase strings
        self.touches = sorted(set(t.lower() for t in self.touches))

    def touches_irreversibility_surface(self) -> bool:
        """True if any touched surface is in IRREVERSIBILITY_SURFACES."""
        return any(t in IRREVERSIBILITY_SURFACES for t in self.touches)

    def to_dict(self) -> dict:
        return {
            "blast_radius": self.blast_radius,
            "reversibility": self.reversibility,
            "touches": list(self.touches),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RiskTag":
        return cls(
            blast_radius=data["blast_radius"],
            reversibility=data["reversibility"],
            touches=list(data.get("touches") or []),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Guardian annotation (§3.4 of L3 spec; lives on the Proposal from L1)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class GuardianAnnotation:
    """Non-blocking risk annotation attached by a guardian during veto pass."""

    guardian_id: str
    severity: Literal["low", "medium", "high", "critical"]
    reason: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "guardian_id": self.guardian_id,
            "severity": self.severity,
            "reason": self.reason,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GuardianAnnotation":
        return cls(
            guardian_id=data["guardian_id"],
            severity=data["severity"],
            reason=data["reason"],
            details=dict(data.get("details") or {}),
        )


@dataclass
class ConflictAnnotation:
    """Flags that this proposal conflicts with another open proposal.

    Detected by the L5 referee; populated on both sides of the conflict pair.
    """

    with_proposal_id: str
    conflict_type: Literal[
        "touches_overlap", "metric_direction_opposite", "exclusive_choice"
    ]
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "with_proposal_id": self.with_proposal_id,
            "conflict_type": self.conflict_type,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ConflictAnnotation":
        return cls(
            with_proposal_id=data["with_proposal_id"],
            conflict_type=data["conflict_type"],
            description=data.get("description", ""),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Status transition audit trail
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class StatusTransition:
    """An entry in a proposal's append-only history."""

    from_status: Status
    to_status: Status
    at: str  # ISO8601 UTC
    actor: str  # "arbiter" | "user" | "verify_daemon" | "snooze_wake" | generator_id
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "from_status": self.from_status,
            "to_status": self.to_status,
            "at": self.at,
            "actor": self.actor,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StatusTransition":
        return cls(
            from_status=data["from_status"],
            to_status=data["to_status"],
            at=data["at"],
            actor=data["actor"],
            reason=data.get("reason", ""),
        )


# ─────────────────────────────────────────────────────────────────────────────
# ProposalRevision — operator-driven iteration history
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ProposalRevision:
    """One iteration on a proposal driven by operator feedback.

    Recorded when a user types feedback on an in-process proposal and the
    bot's LLM revises ``problem``, ``admin_surface_summary``, and (for
    Investigation/WorkflowInstruction) the action context. The fingerprint
    stays stable across revisions because the structural fields (action.kind,
    target, trigger_observations) are not modified.

    Each revision captures both the prior text and the feedback so the
    iteration history is fully auditable from a single ``Proposal`` record.
    """

    at: str
    actor: str  # "user:<id>" | "user"
    feedback: str
    prior_problem: str
    prior_admin_surface_summary: str
    prior_action_summary: str  # short rendering of the action's prose fields

    def to_dict(self) -> dict:
        return {
            "at": self.at,
            "actor": self.actor,
            "feedback": self.feedback,
            "prior_problem": self.prior_problem,
            "prior_admin_surface_summary": self.prior_admin_surface_summary,
            "prior_action_summary": self.prior_action_summary,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProposalRevision":
        return cls(
            at=str(data.get("at", "")),
            actor=str(data.get("actor", "")),
            feedback=str(data.get("feedback", "")),
            prior_problem=str(data.get("prior_problem", "")),
            prior_admin_surface_summary=str(
                data.get("prior_admin_surface_summary", "")
            ),
            prior_action_summary=str(data.get("prior_action_summary", "")),
        )


# ─────────────────────────────────────────────────────────────────────────────
# DispatchResult / DispatchState — Slice 3 (Take-this-on → evo)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class DispatchResult:
    """The outcome reported by a dispatch target.

    Written by the target (evo / a bot / forge) via
    POST /api/arbiter/proposals/<id>/dispatch/result. The status
    transition that follows is decided by ``outcome``:

      outcome="applied"     -> proposal status: dispatched -> applied
      outcome="failed"      -> proposal status: dispatched -> failed_flagged
      outcome="in_process"  -> proposal status: dispatched -> applied
                               (operator picks it up via the In Process tab —
                                same semantics as today's manual-completion
                                kinds; ``message`` carries the target's notes
                                so the operator inherits context.)

    Spec: docs/spec-take-this-on-evo-dispatch-2026-06-04.md.
    """

    outcome: DispatchOutcome
    reported_at: str
    message: str = ""
    # Optional structured summary of what the target changed. Free-form;
    # the UI renders the list as bullet points in the proposal modal.
    applied_changes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "reported_at": self.reported_at,
            "message": self.message,
            "applied_changes": list(self.applied_changes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DispatchResult":
        return cls(
            outcome=data["outcome"],
            reported_at=str(data.get("reported_at", "")),
            message=str(data.get("message", "")),
            applied_changes=list(data.get("applied_changes") or []),
        )


@dataclass
class DispatchState:
    """Runtime state of a dispatched proposal.

    Populated when the operator clicks "Have evo fix this" (or
    similar). The proposal status transitions to ``dispatched``;
    this struct carries the context needed to track + cancel + audit
    the in-flight session.
    """

    target: str  # resolved target: "evo" | "<bot_id>" | "forge"
    dispatched_at: str  # ISO timestamp of dispatch
    message: str  # the message sent verbatim (possibly operator-overridden)
    # The target's session id, returned by the session-start mechanism.
    # None until the target acks (Phase 3.3 wires this).
    session_id: str | None = None
    # The result, written by the target via /dispatch/result.
    # None while the dispatch is still in flight.
    result: DispatchResult | None = None
    # Whether the operator cancelled the dispatch via /dispatch/cancel.
    # When True, the proposal already transitioned back to ``pending``;
    # this flag is for audit-log accuracy.
    cancelled_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "dispatched_at": self.dispatched_at,
            "message": self.message,
            "session_id": self.session_id,
            "result": self.result.to_dict() if self.result else None,
            "cancelled_at": self.cancelled_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DispatchState":
        result_data = data.get("result")
        return cls(
            target=str(data["target"]),
            dispatched_at=str(data.get("dispatched_at", "")),
            message=str(data.get("message", "")),
            session_id=data.get("session_id"),
            result=(
                DispatchResult.from_dict(result_data) if result_data else None
            ),
            cancelled_at=data.get("cancelled_at"),
        )


# ─────────────────────────────────────────────────────────────────────────────
# ValueEstimate — the value half of the altitude/value axis (Fit Reviewer Bite 2)
# ─────────────────────────────────────────────────────────────────────────────


ValueTier = Literal["low", "medium", "high"]


@dataclass
class ValueEstimate:
    """A deterministic, honest value tier for a proposal.

    Spec: docs/spec-fit-reviewer-2026-06-12.md §5.1 / §3.6 (Gate B).

    ``tier`` is **computed**, never LLM-asserted; ``basis`` is the
    human-readable trace of that computation; ``evidence_refs`` point at the
    cited observations / sessions that ground it.

    There is deliberately **no dollar field** — capability value is not
    dollar-denominated, and fabricating one is the 138 sin. Dollar value, when
    a generator can cite a real price, lives in the separate ``value_line``
    (Bite 3, strict cite-or-don't). ValueEstimate is nullable: a proposal with
    no computed value tier carries ``None``.
    """

    tier: ValueTier
    basis: str
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "basis": self.basis,
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ValueEstimate":
        return cls(
            tier=data["tier"],
            basis=str(data.get("basis", "")),
            evidence_refs=list(data.get("evidence_refs") or []),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Proposal — the central type
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Proposal:
    """A Better Engine proposal.

    Spec: docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md §3.1.
    """

    # ── Identity ────────────────────────────────────────────────────────────
    id: str
    bot_id: str

    # ── Provenance ──────────────────────────────────────────────────────────
    generator_id: str
    dimension: str
    trigger_observations: list[str]
    provenance: Provenance

    # ── Content ─────────────────────────────────────────────────────────────
    problem: str
    action: Action
    risk_tag: RiskTag
    claim: Claim | None = None
    revert_on_failure: RevertPlan | None = None

    # ── Better Engine surfacing ─────────────────────────────────────────────
    approval_audience: ApprovalAudience = "none"
    urgency: Urgency = "improvement"
    admin_surface_summary: str = ""
    conversational_pitch: str | None = None
    guardian_annotations: list[GuardianAnnotation] = field(default_factory=list)
    conflicts_with: list[ConflictAnnotation] = field(default_factory=list)

    # ── Lifecycle ───────────────────────────────────────────────────────────
    status: Status = "draft"
    snoozed_until: str | None = None
    history: list[StatusTransition] = field(default_factory=list)

    # ── Iteration ───────────────────────────────────────────────────────────
    # Operator-driven refine cycles via /api/arbiter/proposals/<id>/refine.
    # The bot's LLM revises prose fields based on user feedback; structural
    # fields (action.kind, fingerprint inputs) stay stable. Empty list for
    # proposals that haven't been refined.
    revisions: list[ProposalRevision] = field(default_factory=list)

    # ── Adjacency Explorer (L4+) ────────────────────────────────────────────
    adjacency_type: AdjacencyType | None = None

    # ── Cross-link to motivating Signals ────────────────────────────────────
    # Spec: docs/spec-alerts-signal-store-2026-05-07.md §9.
    # Required on new proposals (each generator populates from the Signals
    # that triggered it); existing proposals keep an empty list — schema
    # does not reject empty for backwards-compat. The denormalized inverse
    # lives on Signal.motivated_proposals.
    motivating_signals: list[str] = field(default_factory=list)

    # ── Estimated savings (PR H) ────────────────────────────────────────────
    # Generator-supplied dollar-per-week savings estimate. Most generators
    # leave this None — only those with a clear money model and access to
    # the relevant cost data populate it (today: cache_ttl_tuner). The
    # arbiter's ranking weighs this as a bonus on top of urgency × authority,
    # capped so a big savings number can't outrank a security_critical
    # proposal. The UI renders a chip "est. ~$5/wk" when this is set and
    # above the display floor.
    #
    # Heuristic by definition — generators document the calculation in code
    # comments and the proposal context. Backward-compat: old proposals on
    # disk without this field load as None.
    estimated_savings_usd: float | None = None

    # ── Slice 3 dispatch (Take-this-on → evo) ───────────────────────────────
    # Optional dispatch target set by the generator at emission time. When
    # set, the UI offers a one-click "Have evo fix this" / "Send to {bot_id}"
    # / "Have forge handle this" button that hands the finding to the target
    # instead of moving to In Process. None means operator-only (today's
    # behavior).
    #
    # Allowed values: "evo" | "<bot_id>" | "forge" | None.
    # Spec: docs/spec-take-this-on-evo-dispatch-2026-06-04.md.
    dispatch_target: str | None = None

    # The message the target receives on dispatch. Generator writes this
    # at emission time; defaults to a server-generated synthesis of title
    # + problem if None. Operator sees this in the pre-dispatch
    # confirmation modal.
    dispatch_message: str | None = None

    # Runtime state of an in-flight dispatch. None until the operator
    # clicks the dispatch button (POST /api/arbiter/proposals/<id>/dispatch
    # populates this). When non-None, the proposal's status is either
    # ``dispatched`` (in flight) or ``applied|failed_flagged`` (target
    # reported). When the operator cancels, status reverts to ``pending``
    # but this state stays for audit; ``cancelled_at`` is set.
    dispatch_state: DispatchState | None = None

    # ── Operator-facing content (Phase A 2026-06-04 protocol) ───────────────
    # Spec: docs/spec-proposal-drafting-protocol-2026-06-04.md.
    #
    # Generators that haven't been migrated to the new shape leave these
    # None; the proposal-detail renderer falls back to today's layout
    # (admin_surface_summary as the heading, problem as the body). When
    # ``summary`` is set, the renderer switches to the five-section
    # operator-first layout: Title / Summary / Proposed Action /
    # Explanation / Details.
    #
    # ── Tier 1-2 (Summary / Explanation) ──
    summary: str | None = None
    explanation: str | None = None

    # ── Tier 3 (action label + manual UI path) ──
    # Button verb that replaces the generic "Take this on" / "Act" label
    # when set. Examples: "Switch to long-window caching", "Add the cron",
    # "Apply this change". Auto-derived for dispatch_target proposals
    # ("Have evo fix this" / "Send to {bot_id}").
    action_label: str | None = None
    # Operator-facing path to the equivalent manual control in the UI,
    # named in the UI's terms ("Cost Optimization → Team Bot A",
    # "Maintenance → Cron Jobs"). Rendered as a sentence after the
    # action button. None means no manual UI path exists.
    manual_path: str | None = None

    # ── Tier 3+ fallback actions ──
    # When no auto-apply or UI path applies, the generator can offer:
    #
    #   cli_command:        a fully-vetted shell command. Rendered as a
    #                       copy-button code block. The generator owns
    #                       the string — no placeholders, ready to paste.
    #   manual_instruction: copy-paste-ready instructions for the
    #                       operator to give to the bot in chat, or
    #                       steps to do offline. Markdown-ish prose
    #                       rendered as a code block with copy button.
    cli_command: str | None = None
    manual_instruction: str | None = None

    # ── Per-finding surface override (RSI eligibility, 2026-06-05) ──
    # Spec: docs/spec-rsi-proposal-eligibility-2026-06-05.md.
    #
    # When set, this overrides the generator's charter-level
    # ``surface`` for routing purposes only (Recommendations vs.
    # Alerts). The override exists because some generators emit a
    # mix of RSI-shaped findings (belong on Recommendations) and
    # anomaly / maintenance findings (belong on Alerts) from the
    # same module — ``efficiency_hawk`` is the canonical example.
    # A factory that produces an anomaly finding sets this to
    # ``"firing"`` (or drift / cleanup); a factory producing a
    # capability proposal leaves it None so the charter wins.
    #
    # Backward-compat: pre-override proposals load with None and the
    # renderer falls back to ``charter.surface`` exactly as before.
    surface: Surface | None = None

    # ── Dismiss-based suppression ──
    # Stable per-finding signature. Used by the Dismiss action to
    # suppress re-emission of the same finding for a TTL. Defaults to
    # trigger_observations[0] when None, but generators are encouraged
    # to set this explicitly so a future change to the observation
    # shape doesn't invalidate prior dismissals.
    dismiss_signature: str | None = None
    # Scope of the Dismiss suppression. "kind" (default) suppresses
    # future findings with the same signature; "instance" only
    # suppresses this specific proposal id. Generators that can't
    # compute a stable per-finding signature set this to "instance".
    dismiss_scope: Literal["kind", "instance"] = "kind"

    # ── Server-side coalescing (2026-06-09) ─────────────────────────────────
    # Spec: docs/spec-recommendations-rework-2026-06-02.md (Phase 1
    # coalesce + humanize). When a generator emits N findings that share
    # a root cause (e.g. 9 missing network_egress hosts on one app, 5
    # tier3 findings from one audit run), it sets the same
    # ``coalesce_key`` on each. ``arbiter.store.write_proposal`` then
    # folds the 2nd..Nth findings into the first as ``sub_findings``
    # entries instead of writing N separate Proposal files. Sub-findings
    # are deduped by trigger_observation so re-runs don't re-append.
    #
    # Operator-facing title. Preferred by the admin UI over
    # ``admin_surface_summary``. Generators that coalesce should set this
    # to a per-root-cause phrase (e.g. "{app}: undeclared network egress
    # hosts") that reads the same whether 1 or 9 sub-findings collapse
    # into the parent.
    coalesce_key: str | None = None
    human_title: str | None = None
    sub_findings: list[dict] = field(default_factory=list)

    # ── Decision-grounding value line (2026-06-12, Bite 3) ───────────────────
    # Design: docs/design-recommendation-legibility-2026-06-12.md
    # §"decision-grounding". A terse, pod-grounded, CITED one-liner the card
    # shows under the title so the operator can decide, not just read — e.g.
    # "your `power` role ran 320 calls/wk on `claude-opus-4-8`; this model is
    # ~30% cheaper". Strict cite-or-don't: only generators that can back the
    # claim with real pod data + a real cited price populate it (today:
    # model_discovery). The heavy derivation lives in the proposal body /
    # drill-down; this is the headline. Backward-compat: old proposals on disk
    # without this field load as None and the card shows no value line.
    value_line: str | None = None

    # ── Altitude + value axis (2026-06-12, Fit Reviewer Bite 2) ──────────────
    # Spec: docs/spec-fit-reviewer-2026-06-12.md §5. Altitude is the
    # value/ambition tier, ORTHOGONAL to ``urgency`` (which is about
    # *attention*, not *ambition*): L0 hygiene/substrate · L1 optimization ·
    # L2 capability · L3 strategic. Deterministic, never LLM-asserted.
    #
    # Default 0 (L0) so every existing generator stays valid without a change.
    # A generator opts up via its charter ``altitude`` default (resolved onto
    # the view at list time, same pattern as ``surface``); a producer that
    # knows the altitude of a specific finding stamps it here directly (the Fit
    # Reviewer poller sets L2 on its InstallApp). The Improvements rail leads
    # with the highest altitude and folds all L0 into one collapsed
    # "Maintenance" digest, so a config nudge never buries a capability idea.
    altitude: int = 0
    # Honest, computed value tier (never a fabricated number, never a dollar).
    # None on proposals that don't carry one (every generator today); the Fit
    # Reviewer populates it from deterministic targeting support (§3.6 Gate B).
    value_estimate: ValueEstimate | None = None

    # ── Misc ────────────────────────────────────────────────────────────────
    schema_version: int = PROPOSAL_SCHEMA_VERSION
    created_at: str = field(default_factory=_utc_now_iso)

    # ── Integrity ───────────────────────────────────────────────────────────
    signature: str = ""  # HMAC; filled by evolve_config.sign_proposal on write

    # ── Helpers ─────────────────────────────────────────────────────────────

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Proposal.id must be non-empty")
        if not self.bot_id:
            raise ValueError("Proposal.bot_id must be non-empty")
        if not self.generator_id:
            raise ValueError("Proposal.generator_id must be non-empty")
        if not self.dimension:
            raise ValueError("Proposal.dimension must be non-empty")
        # Claim ↔ revert_on_failure consistency is enforced at apply time, not
        # at construction. A proposal emitted without a claim (e.g. an
        # Investigation or a guardian VetoAnnotation) is legitimate at L1.

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "bot_id": self.bot_id,
            "generator_id": self.generator_id,
            "dimension": self.dimension,
            "trigger_observations": list(self.trigger_observations),
            "provenance": self.provenance.to_dict(),
            "problem": self.problem,
            "action": action_to_dict(self.action),
            "claim": self.claim.to_dict() if self.claim else None,
            "revert_on_failure": (
                self.revert_on_failure.to_dict() if self.revert_on_failure else None
            ),
            "risk_tag": self.risk_tag.to_dict(),
            "approval_audience": self.approval_audience,
            "urgency": self.urgency,
            "admin_surface_summary": self.admin_surface_summary,
            "conversational_pitch": self.conversational_pitch,
            "guardian_annotations": [a.to_dict() for a in self.guardian_annotations],
            "conflicts_with": [c.to_dict() for c in self.conflicts_with],
            "status": self.status,
            "snoozed_until": self.snoozed_until,
            "history": [h.to_dict() for h in self.history],
            "revisions": [r.to_dict() for r in self.revisions],
            "adjacency_type": self.adjacency_type,
            "motivating_signals": list(self.motivating_signals),
            "estimated_savings_usd": self.estimated_savings_usd,
            # Slice 3 dispatch state — always serialized (None when
            # the proposal isn't dispatchable) so clients can branch
            # on dispatch_target without a defensive null check on
            # the field itself.
            "dispatch_target": self.dispatch_target,
            "dispatch_message": self.dispatch_message,
            "dispatch_state": (
                self.dispatch_state.to_dict() if self.dispatch_state else None
            ),
            # Phase A operator-first content (always serialized — None
            # when the generator hasn't migrated, so the client can
            # branch on `summary is null` without defensive checks).
            "summary": self.summary,
            "explanation": self.explanation,
            "action_label": self.action_label,
            "manual_path": self.manual_path,
            "cli_command": self.cli_command,
            "manual_instruction": self.manual_instruction,
            "dismiss_signature": self.dismiss_signature,
            "dismiss_scope": self.dismiss_scope,
            # Coalescing fields — always serialized so the admin server
            # can branch on presence without a defensive null check.
            "coalesce_key": self.coalesce_key,
            "human_title": self.human_title,
            "sub_findings": list(self.sub_findings),
            # Decision-grounding value line — always serialized so the admin
            # server can render it without a defensive null check. None means
            # "no value line" (every pre-2026-06-12 proposal).
            "value_line": self.value_line,
            # Altitude + value axis — always serialized so the admin server can
            # sort/fold without a defensive null check. altitude defaults to 0
            # (L0); value_estimate is None when no value tier was computed.
            "altitude": self.altitude,
            "value_estimate": (
                self.value_estimate.to_dict() if self.value_estimate else None
            ),
            # Per-finding surface override — always serialized so the
            # admin server can branch on its presence without a defensive
            # null check. None means "use charter.surface" (today's
            # behavior for every pre-2026-06-05 proposal).
            "surface": self.surface,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Proposal":
        return cls(
            id=data["id"],
            schema_version=int(data.get("schema_version", PROPOSAL_SCHEMA_VERSION)),
            created_at=data.get("created_at", _utc_now_iso()),
            bot_id=data["bot_id"],
            generator_id=data["generator_id"],
            dimension=data["dimension"],
            trigger_observations=list(data.get("trigger_observations") or []),
            provenance=Provenance.from_dict(data["provenance"]),
            problem=data.get("problem", ""),
            action=action_from_dict(data["action"]),
            claim=Claim.from_dict(data["claim"]) if data.get("claim") else None,
            revert_on_failure=(
                RevertPlan.from_dict(data["revert_on_failure"])
                if data.get("revert_on_failure")
                else None
            ),
            risk_tag=RiskTag.from_dict(data["risk_tag"]),
            approval_audience=data.get("approval_audience", "none"),
            urgency=data.get("urgency", "improvement"),
            admin_surface_summary=data.get("admin_surface_summary", ""),
            conversational_pitch=data.get("conversational_pitch"),
            guardian_annotations=[
                GuardianAnnotation.from_dict(a)
                for a in (data.get("guardian_annotations") or [])
            ],
            conflicts_with=[
                ConflictAnnotation.from_dict(c)
                for c in (data.get("conflicts_with") or [])
            ],
            status=data.get("status", "draft"),
            snoozed_until=data.get("snoozed_until"),
            history=[
                StatusTransition.from_dict(h) for h in (data.get("history") or [])
            ],
            revisions=[
                ProposalRevision.from_dict(r) for r in (data.get("revisions") or [])
            ],
            adjacency_type=data.get("adjacency_type"),
            motivating_signals=list(data.get("motivating_signals") or []),
            estimated_savings_usd=(
                float(data["estimated_savings_usd"])
                if data.get("estimated_savings_usd") is not None
                else None
            ),
            # Slice 3 — backward-compat: proposals on disk without the
            # dispatch fields load as None / today's behavior.
            dispatch_target=data.get("dispatch_target"),
            dispatch_message=data.get("dispatch_message"),
            dispatch_state=(
                DispatchState.from_dict(data["dispatch_state"])
                if data.get("dispatch_state")
                else None
            ),
            # Phase A operator-first content — backward-compat: pre-
            # migration proposals load with these as None, and the
            # renderer falls back to the legacy admin_surface_summary
            # + problem layout.
            summary=data.get("summary"),
            explanation=data.get("explanation"),
            action_label=data.get("action_label"),
            manual_path=data.get("manual_path"),
            cli_command=data.get("cli_command"),
            manual_instruction=data.get("manual_instruction"),
            dismiss_signature=data.get("dismiss_signature"),
            dismiss_scope=data.get("dismiss_scope") or "kind",
            # Coalescing fields — pre-2026-06-09 proposals load with
            # None / [] so the renderer falls back to today's layout.
            coalesce_key=data.get("coalesce_key"),
            human_title=data.get("human_title"),
            sub_findings=list(data.get("sub_findings") or []),
            # Decision-grounding value line — pre-2026-06-12 proposals have no
            # key; missing → None → the card shows no value line.
            value_line=data.get("value_line"),
            # Altitude + value axis — pre-2026-06-12 proposals have no keys;
            # missing/None altitude → 0 (L0), missing value_estimate → None.
            altitude=int(data.get("altitude") or 0),
            value_estimate=(
                ValueEstimate.from_dict(data["value_estimate"])
                if data.get("value_estimate")
                else None
            ),
            # Per-finding surface override — pre-2026-06-05 proposals
            # have no key; missing → None → charter.surface wins.
            surface=data.get("surface"),
            signature=data.get("signature", ""),
        )


def new_proposal_id() -> str:
    """Generate a fresh proposal ID."""
    return str(uuid4())
