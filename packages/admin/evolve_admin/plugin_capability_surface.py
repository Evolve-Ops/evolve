"""Declared-capability surface of the Evolve OC plugin, and the widening gate.

Companion to :mod:`evolve_admin.plugin_signature`, which answers *"is the
install tree the one we built?"*. This module answers a different question
the digests cannot: *"did the set of privileges the plugin asks for grow
since a human last looked?"*

Why it exists — [internal/spec-plugin-install-trust-2026-06-06.md](../../internal/spec-plugin-install-trust-2026-06-06.md) §4.2/§4.3:
``deploy.install_oc_plugin`` passes ``--accept-capabilities`` because OC
2026.9.2 makes the consent prompt terminal and deploy is headless. That flag
blanket-answers OC's own widening check (OC records an ``acceptedSurface`` per
install and re-prompts when the declared surface widens), so without something
on the Evolve side a capability added to the plugin is granted fleet-wide on
the next deploy with nobody in the loop. This module is that something.

**The digests do not cover this.** ``treeDigest`` hashes the manifest's
content, so it catches a capability added by *tampering with the install tree*
after the build. It cannot catch one added *in the source manifest and built
normally* — that produces a perfectly valid stamp. The threat here is drift in
our own repo, not a corrupted tree, so the baseline has to live somewhere a
human edits: :mod:`evolve_admin.plugin_capability_baseline`.

**Where enforcement actually bites.** ``tools/plugin-capability-lint`` (CI, and
in ``tools/preflight``) is the real gate: it fails the PR that widens the
surface without updating the baseline, making the addition an explicit,
reviewable act — the same shape as ``docs/public-manifest.yaml``'s allowlist.
The install-time check wired into ``verify_plugin_signature`` is the
defence-in-depth backstop for a pod whose deployed plugin is not the one CI
saw, and it is staged behind :data:`REQUIRE_CAPABILITY_BASELINE` for the
fail-closed reason spelled out there.

Extraction mirrors OC 2026.9.2's ``buildPluginCapabilitySummary().declared``
(``dist/capability-summary-*.js``) so the two agree on what a capability *is*.
Mirrored, not imported: reaching into OC's bundled dist would couple us to a
minified module id that changes every release. The mirror is pinned by
``test_surface_matches_openclaw_groups`` and by :data:`OC_SURFACE_SOURCE`.

One deliberate divergence, :data:`HOOK_GRANTS_GROUP`: OC files
``hooks.allowConversationAccess`` / ``allowPromptInjection`` under ``grants``
rather than ``declared``, so they are NOT part of its declared-surface hash.
They are exactly the privileges a reviewer cares about most, and they are
declared in the manifest we build, so this module tracks them as an eleventh
group. A reviewer reading the baseline sees them; OC's consent flow would not
have shown them.
"""

from __future__ import annotations

from typing import Any

OC_SURFACE_SOURCE = "openclaw 2026.9.2 (3928bad) dist/capability-summary-*.js"
"""The upstream revision this extraction was mirrored from.

Bump when re-verifying against a newer OC. The mirror is a copy of someone
else's definition, so the thing that keeps it honest is re-reading the source,
not the tests — they can only pin what we already believe.
"""

SURFACE_GROUPS: tuple[str, ...] = (
    "channels",
    "providers",
    "tools",
    "contracts",
    "hooks",
    "mcpServers",
    "cliCommands",
    "cliBackends",
    "skills",
    "dangerousConfigFlags",
)
"""OC's ``PLUGIN_DECLARED_SURFACE_GROUPS``, verbatim and in upstream order."""

HOOK_GRANTS_GROUP = "hookGrants"
"""Evolve's eleventh group — manifest-declared hook grants set to ``true``.

Not one of OC's declared-surface groups (see the module docstring): OC resolves
these as *grants*, from config and plugin origin, and they never reach its
consent hash. Tracked here because ``allowConversationAccess`` is the single
widest privilege the manifest can assert and a reviewer should not have to know
OC's internal taxonomy to see it appear.
"""

REVIEWED_MANIFEST_CONTRACT_FAMILIES: tuple[str, ...] = (
    "embeddedExtensionFactories",
    "agentToolResultMiddleware",
    "trustedToolPolicies",
    "externalAuthProviders",
    "embeddingProviders",
    "speechProviders",
    "realtimeTranscriptionProviders",
    "realtimeVoiceProviders",
    "mediaUnderstandingProviders",
    "transcriptSourceProviders",
    "documentExtractors",
    "imageGenerationProviders",
    "videoGenerationProviders",
    "musicGenerationProviders",
    "webContentExtractors",
    "webFetchProviders",
    "webSearchProviders",
    "workerProviders",
    "usageProviders",
    "migrationProviders",
    "gatewayMethodDispatch",
    "tools",
)
"""OC's ``REVIEWED_MANIFEST_CONTRACT_FAMILIES``, verbatim.

A contract family absent from this tuple contributes nothing to the surface —
which is upstream's choice, not ours. If OC adds a family and we do not mirror
it, a plugin could declare into it and widen with no entry here: that is what
:data:`OC_SURFACE_SOURCE` and its re-verification note exist to catch.
"""


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, list) else []


def declared_surface(manifest: dict) -> dict[str, list[str]]:
    """Extract the plugin's declared capability surface from its manifest.

    Returns every group in :data:`SURFACE_GROUPS` plus
    :data:`HOOK_GRANTS_GROUP`, each a sorted list of strings — groups the
    manifest says nothing about come back as ``[]`` rather than missing, so a
    comparison never has to distinguish "absent" from "empty".

    Tolerant of manifest shapes it does not recognize (a scalar where a list
    belongs contributes nothing) because this runs on the install path: a
    malformed manifest must not raise here. It is already a fail-closed
    condition for the digest check, which runs first and owns that verdict.
    """
    contracts = manifest.get("contracts")
    contracts = contracts if isinstance(contracts, dict) else {}
    hooks = manifest.get("hooks")

    channels = _as_list(manifest.get("channels"))
    if not channels:
        channel = manifest.get("channel")
        if isinstance(channel, dict) and isinstance(channel.get("id"), str):
            channels = [channel["id"]]

    providers: list[str] = []
    for p in _as_list(manifest.get("providers")):
        if isinstance(p, str):
            providers.append(p)
        elif isinstance(p, dict) and isinstance(p.get("id"), str):
            providers.append(p["id"])

    tool_metadata = manifest.get("toolMetadata")
    tools = set(x for x in _as_list(contracts.get("tools")) if isinstance(x, str))
    if isinstance(tool_metadata, dict):
        tools |= set(tool_metadata.keys())

    cli_commands = [
        c["name"] for c in _as_list(manifest.get("cliCommands"))
        if isinstance(c, dict) and isinstance(c.get("name"), str)
    ]

    config_contracts = manifest.get("configContracts")
    dangerous = []
    if isinstance(config_contracts, dict):
        dangerous = [
            f["path"] for f in _as_list(config_contracts.get("dangerousFlags"))
            if isinstance(f, dict) and isinstance(f.get("path"), str)
        ]

    mcp = manifest.get("mcpServers")

    surface = {
        "channels": channels,
        "providers": providers,
        "tools": sorted(tools),
        "contracts": sorted({
            f"{family}: {item}"
            for family in REVIEWED_MANIFEST_CONTRACT_FAMILIES
            for item in _as_list(contracts.get(family))
            if isinstance(item, str)
        }),
        # OC reads manifest.hooks as an ARRAY of hook names. Evolve's manifest
        # uses the object form ({"allowConversationAccess": true}), which
        # declares no hook names — those are grants, captured below.
        "hooks": [h for h in _as_list(hooks) if isinstance(h, str)],
        "mcpServers": sorted(mcp.keys()) if isinstance(mcp, dict) else [],
        "cliCommands": cli_commands,
        "cliBackends": [x for x in _as_list(manifest.get("cliBackends")) if isinstance(x, str)],
        "skills": [x for x in _as_list(manifest.get("skills")) if isinstance(x, str)],
        "dangerousConfigFlags": dangerous,
        HOOK_GRANTS_GROUP: (
            sorted(k for k, v in hooks.items() if v is True)
            if isinstance(hooks, dict) else []
        ),
    }
    return {k: sorted(v) for k, v in surface.items()}


def diff_widening(
    baseline: dict[str, list[str]], current: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Entries present in ``current`` but not in ``baseline``, by group.

    Only widening is reported. A surface that *shrinks* is safe by
    construction — fewer privileges asked for — so it is never an error here;
    :func:`diff_narrowing` reports it separately so the baseline can be
    ratcheted down deliberately rather than drifting looser than reality.

    An unknown group in ``current`` is reported whole. That is the case where
    OC added a surface group and this module was updated to mirror it while the
    baseline still predates it — the entries are genuinely unreviewed.
    """
    widened = {}
    for group, items in current.items():
        added = sorted(set(items) - set(baseline.get(group) or []))
        if added:
            widened[group] = added
    return widened


def diff_narrowing(
    baseline: dict[str, list[str]], current: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Entries in ``baseline`` the plugin no longer declares, by group."""
    removed = {}
    for group, items in baseline.items():
        gone = sorted(set(items or []) - set(current.get(group) or []))
        if gone:
            removed[group] = gone
    return removed


def format_diff(diff: dict[str, list[str]]) -> str:
    """One-line-per-group rendering for operator-facing error text."""
    return "; ".join(
        f"{group}: {', '.join(items)}" for group, items in sorted(diff.items())
    )
