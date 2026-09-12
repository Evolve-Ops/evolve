"""pod_baseline.ordering — the safety partial order (Q7(a)).

Spec: internal/spec-pod-plane-2026-08-15.md, Q7(a) (decided 2026-08-22).

The tool_profile expectations here are transcribed from OpenClaw's own
``CORE_TOOL_PROFILES`` (deployed install, OC 2026.7.1-2,
``dist/tool-catalog-*.js``, built from ``src/agents/tool-catalog.ts`` — "Core
tool catalog and profile defaults. Drives built-in profile allowlists"), NOT
inferred from the profile names. The membership fixture below is the part
that makes the chain a reading rather than a guess: it is the upstream
allowlist relation, and the chains have to follow from it.
"""
import itertools

from pod_baseline.ordering import (
    INCOMPARABLE,
    LOOSER,
    SAFETY_CHAINS,
    TIGHTER,
    compare,
    has_ordering,
    safest,
)
from pod_baseline.schema import SURFACES

# OpenClaw's built-in tool profiles, transcribed from CORE_TOOL_PROFILES /
# CORE_TOOL_DEFINITIONS (OC 2026.7.1-2). Trimmed to the tools that decide the
# ordering: the full coding list is long and every extra entry is in coding
# only, so it cannot change any subset verdict below.
_OC_PROFILE_TOOLS = {
    "minimal": {"session_status"},
    "messaging": {
        "session_status", "sessions_list", "sessions_history", "sessions_send",
        "message", "bundle-mcp",
    },
    "coding": {
        "session_status", "sessions_list", "sessions_history", "sessions_send",
        "sessions_spawn", "read", "write", "edit", "exec", "process",
        "web_search", "cron", "bundle-mcp",
    },
    "full": {"*"},
}


def _oc_allows(profile: str, tool: str) -> bool:
    allow = _OC_PROFILE_TOOLS[profile]
    return "*" in allow or tool in allow


def _oc_is_subset(inner: str, outer: str) -> bool:
    return all(_oc_allows(outer, t) for t in _OC_PROFILE_TOOLS[inner])


# ── The tool_profile order is READ out of OC, not inferred ───────────────────

def test_oc_catalog_makes_coding_and_messaging_incomparable():
    # The load-bearing fact. Only messaging carries `message`; only coding
    # carries the filesystem/runtime set. Neither allowlist contains the
    # other, so neither profile is the tighter of the pair — which is why
    # tool_profile needs TWO chains and not one.
    assert not _oc_is_subset("coding", "messaging")
    assert not _oc_is_subset("messaging", "coding")
    assert compare("tool_profile", "coding", "messaging") == INCOMPARABLE
    assert compare("tool_profile", "messaging", "coding") == INCOMPARABLE


def test_chains_match_the_oc_allowlist_subset_relation():
    # Every ordered pair the chains claim must be a real allowlist subset
    # upstream, in that direction.
    for chain in SAFETY_CHAINS["tool_profile"]:
        for tighter, looser in itertools.combinations(chain, 2):
            assert _oc_is_subset(tighter, looser), (tighter, looser)
            assert compare("tool_profile", tighter, looser) == TIGHTER
            assert compare("tool_profile", looser, tighter) == LOOSER


def test_minimal_is_the_floor_and_full_the_ceiling():
    for profile in ("coding", "messaging", "full"):
        assert compare("tool_profile", "minimal", profile) == TIGHTER
    for profile in ("minimal", "coding", "messaging"):
        assert compare("tool_profile", "full", profile) == LOOSER


def test_custom_allow_is_on_no_chain():
    # An exclusive tools.allow list REPLACES the profile upstream; its
    # contents are arbitrary, so it is not knowably tighter or looser than
    # any named profile.
    for profile in ("minimal", "coding", "messaging", "full"):
        assert compare("tool_profile", "custom-allow", profile) == INCOMPARABLE
        assert compare("tool_profile", profile, "custom-allow") == INCOMPARABLE


# ── exec_policy / browser ────────────────────────────────────────────────────

def test_exec_policy_ladder():
    assert compare("exec_policy", "deny", "allowlist") == TIGHTER
    assert compare("exec_policy", "deny", "full") == TIGHTER
    assert compare("exec_policy", "allowlist", "full") == TIGHTER
    assert compare("exec_policy", "full", "deny") == LOOSER
    assert compare("exec_policy", "full", "full") == INCOMPARABLE  # equal


def test_browser_ladder():
    assert compare("browser", "off", "on") == TIGHTER
    assert compare("browser", "on", "off") == LOOSER


# ── Surfaces with no ordering, and values on no chain ────────────────────────

def test_cost_and_provenance_surfaces_have_no_ordering():
    # Q7(a): context_profile is a cost axis, model_policy a binary
    # provenance flag. Neither may ever produce a direction.
    assert not has_ordering("context_profile")
    assert not has_ordering("model_policy")
    for surface in ("context_profile", "model_policy"):
        for a, b in (("custom", "pod-defaults"), ("lean", "balanced")):
            assert compare(surface, a, b) == INCOMPARABLE
            assert compare(surface, b, a) == INCOMPARABLE


def test_unset_is_on_no_chain_of_any_surface():
    # "The knob is absent" means UPSTREAM's default governs, and that can
    # move under the fleet with an OC release — so it is not a fixed point
    # and cannot be placed against one.
    for surface, other in (
        ("exec_policy", "full"), ("exec_policy", "deny"),
        ("browser", "on"), ("browser", "off"),
        ("tool_profile", "coding"), ("tool_profile", "full"),
    ):
        assert compare(surface, "unset", other) == INCOMPARABLE
        assert compare(surface, other, "unset") == INCOMPARABLE


def test_unknown_surface_never_produces_a_direction():
    assert compare("", "deny", "full") == INCOMPARABLE
    assert compare("plugin_set", "deny", "full") == INCOMPARABLE
    assert not has_ordering("plugin_set")


# ── Structural invariants ────────────────────────────────────────────────────

def test_chains_are_mutually_consistent():
    # Where two chains of a surface share a pair, they must agree on its
    # order. compare() degrades a disagreement to INCOMPARABLE rather than
    # picking a winner, so a bad edit would go quiet — this is what makes
    # it loud instead.
    for surface, chains in SAFETY_CHAINS.items():
        for a_chain, b_chain in itertools.combinations(chains, 2):
            for x, y in itertools.combinations(set(a_chain) & set(b_chain), 2):
                assert (a_chain.index(x) < a_chain.index(y)) == (
                    b_chain.index(x) < b_chain.index(y)
                ), (surface, x, y)


def test_every_chain_surface_is_a_real_surface_and_has_no_repeats():
    for surface, chains in SAFETY_CHAINS.items():
        assert surface in SURFACES
        for chain in chains:
            assert len(chain) == len(set(chain)), (surface, chain)
            assert len(chain) >= 2, (surface, chain)


def test_comparison_is_antisymmetric_across_every_chain_value():
    for surface, chains in SAFETY_CHAINS.items():
        values = {v for chain in chains for v in chain}
        for a, b in itertools.permutations(values, 2):
            forward, back = compare(surface, a, b), compare(surface, b, a)
            if forward == TIGHTER:
                assert back == LOOSER, (surface, a, b)
            elif forward == LOOSER:
                assert back == TIGHTER, (surface, a, b)
            else:
                assert back == INCOMPARABLE, (surface, a, b)


# ── safest() — the seed tiebreak ─────────────────────────────────────────────

def test_safest_picks_the_strictly_tightest_candidate():
    assert safest("exec_policy", ["full", "deny", "allowlist"]) == "deny"
    assert safest("exec_policy", ["full", "allowlist"]) == "allowlist"
    assert safest("browser", ["on", "off"]) == "off"
    assert safest("tool_profile", ["full", "coding", "minimal"]) == "minimal"


def test_safest_returns_none_when_the_ordering_cannot_decide():
    # Mutually incomparable candidates...
    assert safest("tool_profile", ["coding", "messaging"]) is None
    # ...a candidate on no chain...
    assert safest("exec_policy", ["full", "unset"]) is None
    # ...and a surface with no ordering at all.
    assert safest("model_policy", ["custom", "pod-defaults"]) is None


def test_safest_of_a_single_candidate_is_that_candidate():
    assert safest("exec_policy", ["full"]) == "full"
    assert safest("model_policy", ["custom", "custom"]) == "custom"
