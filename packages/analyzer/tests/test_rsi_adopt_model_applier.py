"""tests/test_rsi_adopt_model_applier.py — AdoptModel applier + keyed merge.

Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §Addendum (A).

Covers:
  - applier: create rung at position, extend existing rung, optional role
    re-point, cap seed (max default 5), idempotency, judge provider-diversity
    rejection, adopt-without-role (dormant rung), revert.
  - keyed catalog merge (Python read side): per-bot override wins by id,
    pod-only rung visible, roles/roleCaps merge by key.
  - end-to-end acceptance: discovery → AdoptModel proposal → approve with
    {role: max, cap: 1} → network.json models gains rung + role + cap →
    resolve_tier_chain (merged) routes max → fable.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.appliers import get_applier  # noqa: E402
from arbiter.appliers.adopt_model import set_network_io  # noqa: E402
from primary_bot import merge_model_catalog, resolve_tier_chain  # noqa: E402
from schema.proposal import AdoptModel  # noqa: E402


def _stub_network_io(initial: dict | None = None):
    """Return (read_fn, write_fn, store) where store['net'] is the network dict."""
    store: dict[str, dict] = {"net": dict(initial or {})}

    def read_fn() -> dict:
        # Deep-ish copy so the applier's in-process mutations don't leak into
        # the store except through write_fn (mirrors a real on-disk read).
        import copy
        return copy.deepcopy(store["net"])

    def write_fn(net: dict) -> None:
        import copy
        store["net"] = copy.deepcopy(net)

    return read_fn, write_fn, store


# ── Applier: create a new rung at the suggested position ──────────────────────

def test_apply_creates_rung_at_position():
    read_fn, write_fn, store = _stub_network_io({
        "models": {
            "rungs": [
                {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
                {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"},
            ],
        },
    })
    set_network_io(read_fn, write_fn)
    try:
        applier = get_applier("AdoptModel")
        action = AdoptModel(
            provider="anthropic", model_id="claude-fable-5",
            rung_slug="fable-class", position=2, cost_class="premium",
        )
        snap = applier.capture_snapshot(action, "<pod>")
        result = applier.apply(action, "<pod>")
        assert result.ok, result.message
        rungs = store["net"]["models"]["rungs"]
        ids = [r["id"] for r in rungs]
        assert ids == ["haiku-class", "sonnet-class", "fable-class"]
        assert rungs[2]["models"] == ["anthropic/claude-fable-5"]
        assert rungs[2]["costClass"] == "premium"
        # No role mapped (dormant adoption — the default).
        assert "roles" not in store["net"]["models"] or "max" not in store["net"]["models"].get("roles", {})
        # snapshot captured the prior (2-rung) catalog for revert.
        assert len(snap["prior_models"]["rungs"]) == 2
    finally:
        set_network_io(None, None)


def test_apply_extends_existing_rung_cluster():
    read_fn, write_fn, store = _stub_network_io({
        "models": {"rungs": [
            {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"},
        ]},
    })
    set_network_io(read_fn, write_fn)
    try:
        applier = get_applier("AdoptModel")
        action = AdoptModel(
            provider="openai", model_id="gpt-5.4",
            rung_slug="sonnet-class", position=0, cost_class="medium",
        )
        result = applier.apply(action, "<pod>")
        assert result.ok, result.message
        rungs = store["net"]["models"]["rungs"]
        assert len(rungs) == 1  # extended, not created
        assert rungs[0]["models"] == ["anthropic/claude-sonnet-4-6", "openai/gpt-5.4"]
    finally:
        set_network_io(None, None)


def test_apply_insert_before_places_upgrade_ahead_of_predecessor():
    """The version-freshness path passes insert_before=<predecessor>: the new
    model must land AHEAD of it so the resolver (which routes to the first
    cluster member, not the newest) actually moves to the new version. A plain
    append would leave the upgrade as a dead fallback."""
    read_fn, write_fn, store = _stub_network_io({
        "models": {"rungs": [
            {"id": "sonnet-class",
             "models": ["openai/gpt-5", "anthropic/claude-sonnet-4-5"],
             "costClass": "medium"},
        ]},
    })
    set_network_io(read_fn, write_fn)
    try:
        applier = get_applier("AdoptModel")
        action = AdoptModel(
            provider="anthropic", model_id="claude-sonnet-5",
            rung_slug="sonnet-class", position=0, cost_class="medium",
            insert_before="anthropic/claude-sonnet-4-5",
        )
        result = applier.apply(action, "<pod>")
        assert result.ok, result.message
        # Spliced just ahead of its predecessor; the OTHER provider's primacy
        # is preserved (only the predecessor it upgrades is displaced).
        assert store["net"]["models"]["rungs"][0]["models"] == [
            "openai/gpt-5", "anthropic/claude-sonnet-5", "anthropic/claude-sonnet-4-5",
        ]
        # Re-apply is a no-op (already ahead of the predecessor).
        again = applier.apply(action, "<pod>")
        assert again.ok and "no-op" in again.message.lower()
        assert store["net"]["models"]["rungs"][0]["models"] == [
            "openai/gpt-5", "anthropic/claude-sonnet-5", "anthropic/claude-sonnet-4-5",
        ]
    finally:
        set_network_io(None, None)


def test_apply_insert_before_promotes_a_prior_buggy_append():
    """If a model was previously appended BEHIND its predecessor (the pre-fix
    bug), re-applying with insert_before promotes it ahead — self-repair."""
    read_fn, write_fn, store = _stub_network_io({
        "models": {"rungs": [
            {"id": "sonnet-class",
             "models": ["anthropic/claude-sonnet-4-5", "anthropic/claude-sonnet-5"],
             "costClass": "medium"},
        ]},
    })
    set_network_io(read_fn, write_fn)
    try:
        applier = get_applier("AdoptModel")
        action = AdoptModel(
            provider="anthropic", model_id="claude-sonnet-5",
            rung_slug="sonnet-class", insert_before="anthropic/claude-sonnet-4-5",
        )
        result = applier.apply(action, "<pod>")
        assert result.ok, result.message
        assert store["net"]["models"]["rungs"][0]["models"] == [
            "anthropic/claude-sonnet-5", "anthropic/claude-sonnet-4-5",
        ]
    finally:
        set_network_io(None, None)


# ── Applier: role re-point + cap seed ─────────────────────────────────────────

def test_apply_maps_max_role_and_seeds_default_cap():
    read_fn, write_fn, store = _stub_network_io({"models": {"rungs": [], "roles": {}}})
    set_network_io(read_fn, write_fn)
    try:
        applier = get_applier("AdoptModel")
        action = AdoptModel(
            provider="anthropic", model_id="claude-fable-5",
            rung_slug="fable-class", position=0, cost_class="premium",
            role_mapping="max",  # cap omitted → default 5
        )
        result = applier.apply(action, "<pod>")
        assert result.ok, result.message
        models = store["net"]["models"]
        assert models["roles"]["max"] == "fable-class"
        assert models["roleCaps"]["max"]["maxPerDayPerBot"] == 5
    finally:
        set_network_io(None, None)


def test_apply_maps_max_role_with_explicit_cap():
    read_fn, write_fn, store = _stub_network_io({"models": {"rungs": [], "roles": {}}})
    set_network_io(read_fn, write_fn)
    try:
        applier = get_applier("AdoptModel")
        action = AdoptModel(
            provider="anthropic", model_id="claude-fable-5",
            rung_slug="fable-class", role_mapping="max", cap_per_day=1,
        )
        result = applier.apply(action, "<pod>")
        assert result.ok, result.message
        assert store["net"]["models"]["roleCaps"]["max"]["maxPerDayPerBot"] == 1
    finally:
        set_network_io(None, None)


def test_apply_dormant_adoption_no_role_no_cap():
    """role_mapping='none' (default) adopts the rung but maps no role and
    seeds no cap — a dormant catalog entry."""
    read_fn, write_fn, store = _stub_network_io({"models": {"rungs": []}})
    set_network_io(read_fn, write_fn)
    try:
        applier = get_applier("AdoptModel")
        action = AdoptModel(
            provider="anthropic", model_id="claude-fable-5",
            rung_slug="fable-class",
        )
        result = applier.apply(action, "<pod>")
        assert result.ok, result.message
        models = store["net"]["models"]
        assert [r["id"] for r in models["rungs"]] == ["fable-class"]
        assert "roles" not in models or not models.get("roles")
        assert "roleCaps" not in models
    finally:
        set_network_io(None, None)


# ── Applier: idempotency ──────────────────────────────────────────────────────

def test_apply_is_idempotent():
    read_fn, write_fn, store = _stub_network_io({"models": {"rungs": [], "roles": {}}})
    set_network_io(read_fn, write_fn)
    try:
        applier = get_applier("AdoptModel")
        action = AdoptModel(
            provider="anthropic", model_id="claude-fable-5",
            rung_slug="fable-class", role_mapping="max", cap_per_day=3,
        )
        r1 = applier.apply(action, "<pod>")
        assert r1.ok and not r1.details.get("noop")
        snapshot_after_first = dict(store["net"]["models"])
        r2 = applier.apply(action, "<pod>")
        assert r2.ok, r2.message
        assert r2.details.get("noop") is True, r2.message
        # State unchanged by the re-apply.
        assert store["net"]["models"] == snapshot_after_first
    finally:
        set_network_io(None, None)


# ── Applier: judge provider-diversity rejection ───────────────────────────────

def test_apply_rejects_judge_same_provider():
    """Mapping judge to a rung whose only models share the standard role's
    provider violates provider diversity → clean rejection, no write."""
    read_fn, write_fn, store = _stub_network_io({
        "models": {
            "rungs": [
                {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"},
            ],
            "roles": {"standard": "sonnet-class"},
        },
    })
    set_network_io(read_fn, write_fn)
    try:
        applier = get_applier("AdoptModel")
        # New anthropic model in a new rung mapped to judge — same provider as
        # standard (anthropic) → must reject.
        action = AdoptModel(
            provider="anthropic", model_id="claude-fable-5",
            rung_slug="fable-class", role_mapping="judge",
        )
        result = applier.apply(action, "<pod>")
        assert not result.ok
        assert result.details.get("fail_action") == "flag"
        assert "judge" in result.message
        # No write happened (judge validation runs before any persist).
        assert [r["id"] for r in store["net"]["models"]["rungs"]] == ["sonnet-class"]
    finally:
        set_network_io(None, None)


def test_apply_accepts_judge_cross_provider():
    read_fn, write_fn, store = _stub_network_io({
        "models": {
            "rungs": [
                {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"},
            ],
            "roles": {"standard": "sonnet-class"},
        },
    })
    set_network_io(read_fn, write_fn)
    try:
        applier = get_applier("AdoptModel")
        action = AdoptModel(
            provider="openai", model_id="gpt-5.4",
            rung_slug="judge-rung", role_mapping="judge",
        )
        result = applier.apply(action, "<pod>")
        assert result.ok, result.message
        roles = store["net"]["models"]["roles"]
        assert roles["judge"] == {"rung": "judge-rung", "provider": "not-standard"}
    finally:
        set_network_io(None, None)


def test_apply_judge_diversity_presumes_no_provider_for_bare_ids():
    """Bare model ids (no provider prefix) resolve to NO provider — the
    ModelRouter._providerOf / model_registry._provider_of convention — so
    a judge mapping is accepted when standard's rung is empty: the runtime
    resolver would pick the qualified judge model, and validation must
    accept exactly what the router resolves (no presumed default provider).
    """
    read_fn, write_fn, store = _stub_network_io({
        "models": {
            "rungs": [
                {"id": "sonnet-class", "models": [], "costClass": "medium"},
            ],
            "roles": {"standard": "sonnet-class"},
        },
    })
    set_network_io(read_fn, write_fn)
    try:
        applier = get_applier("AdoptModel")
        action = AdoptModel(
            provider="anthropic", model_id="claude-fable-5",
            rung_slug="fable-class", role_mapping="judge",
        )
        result = applier.apply(action, "<pod>")
        assert result.ok, result.message
        roles = store["net"]["models"]["roles"]
        assert roles["judge"] == {"rung": "fable-class", "provider": "not-standard"}
    finally:
        set_network_io(None, None)


# ── Applier: revert ───────────────────────────────────────────────────────────

def test_revert_restores_prior_models_block():
    read_fn, write_fn, store = _stub_network_io({
        "models": {"rungs": [{"id": "sonnet-class", "models": ["x"], "costClass": "medium"}]},
    })
    set_network_io(read_fn, write_fn)
    try:
        applier = get_applier("AdoptModel")
        action = AdoptModel(
            provider="anthropic", model_id="claude-fable-5", rung_slug="fable-class",
        )
        snap = applier.capture_snapshot(action, "<pod>")
        applier.apply(action, "<pod>")
        assert len(store["net"]["models"]["rungs"]) == 2
        rev = applier.revert(snap, "<pod>")
        assert rev.ok, rev.message
        assert [r["id"] for r in store["net"]["models"]["rungs"]] == ["sonnet-class"]
    finally:
        set_network_io(None, None)


# ── Keyed merge (Python read side) ────────────────────────────────────────────

def test_merge_override_wins_by_id():
    # Pure two-layer kernel (include_defaults=False) — exercises the by-id
    # override rule in isolation, without the code-default base layer folded in.
    base = {"rungs": [{"id": "sonnet-class", "models": ["old"], "costClass": "medium"}]}
    over = {"rungs": [{"id": "sonnet-class", "models": ["new"], "costClass": "medium"}]}
    m = merge_model_catalog(base, over, include_defaults=False)
    assert m["rungs"][0]["models"] == ["new"]


def test_merge_pod_only_rung_visible():
    base = {
        "rungs": [
            {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
            {"id": "fable-class", "models": ["anthropic/claude-fable-5"], "costClass": "premium"},
        ],
        "roles": {"max": "fable-class"},
    }
    over = {
        "rungs": [{"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"}],
        "roles": {"standard": "sonnet-class"},
    }
    # Pure two-layer kernel — assert the keyed-merge rules on these two layers
    # alone (the code-default base layer is verified separately).
    m = merge_model_catalog(base, over, include_defaults=False)
    ids = [r["id"] for r in m["rungs"]]
    assert "fable-class" in ids and "sonnet-class" in ids
    assert m["roles"] == {"max": "fable-class", "standard": "sonnet-class"}
    # max routes to the pod-only fable rung through the merged catalog.
    assert resolve_tier_chain({**m, "roles": {**m["roles"], "power": "fable-class"}}, "tier1") == ["anthropic/claude-fable-5"]


def test_merge_rolecaps_per_bot_wins():
    base = {"rungs": [{"id": "f", "models": ["x"]}], "roleCaps": {"max": {"maxPerDayPerBot": 5}, "power": {"maxPerDayPerBot": 9}}}
    over = {"rungs": [{"id": "s", "models": ["y"]}], "roleCaps": {"max": {"maxPerDayPerBot": 1}}}
    m = merge_model_catalog(base, over)
    assert m["roleCaps"]["max"]["maxPerDayPerBot"] == 1
    assert m["roleCaps"]["power"]["maxPerDayPerBot"] == 9


def test_merge_no_rungs_block_precedence():
    base = {"routing": {"enabled": False}}
    over = {"routing": {"enabled": True}}
    assert merge_model_catalog(base, over)["routing"]["enabled"] is True


# ── End-to-end acceptance: approve {role: max, cap: 1} closes the loop ─────────

def test_acceptance_adopt_max_then_resolves_via_merge():
    """The lifecycle the spec's §A.5 closes: an AdoptModel proposal approved
    with {role: max, cap: 1} edits the pod catalog; a per-bot file lacking the
    fable rung still resolves `max` -> fable via the keyed merge."""
    # Pod catalog before adoption (no fable).
    read_fn, write_fn, store = _stub_network_io({
        "models": {
            "rungs": [
                {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
                {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"},
            ],
            "roles": {"fast": "haiku-class", "standard": "sonnet-class"},
        },
    })
    set_network_io(read_fn, write_fn)
    try:
        applier = get_applier("AdoptModel")
        # Operator's approval choices ({role: max, cap: 1}) ride the action —
        # exactly what the /act endpoint patches on.
        action = AdoptModel(
            provider="anthropic", model_id="claude-fable-5",
            rung_slug="fable-class", position=2, cost_class="premium",
            role_mapping="max", cap_per_day=1,
        )
        result = applier.apply(action, "<pod>")
        assert result.ok, result.message

        pod_models = store["net"]["models"]
        # Catalog gained the rung at the right position + role + cap.
        assert [r["id"] for r in pod_models["rungs"]] == ["haiku-class", "sonnet-class", "fable-class"]
        assert pod_models["roles"]["max"] == "fable-class"
        assert pod_models["roleCaps"]["max"]["maxPerDayPerBot"] == 1

        # A per-bot file that does NOT carry the fable rung still resolves max
        # (tier1 maps power, but the new-shape resolver maps tierN→role; for
        # `max` we resolve the role directly through the merged catalog).
        per_bot = {
            "rungs": [
                {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
                {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"},
            ],
            "roles": {"fast": "haiku-class", "standard": "sonnet-class"},
        }
        merged = merge_model_catalog(pod_models, per_bot)
        # max -> fable-class rung visible despite the per-bot file omitting it.
        assert merged["roles"]["max"] == "fable-class"
        fable_rung = next(r for r in merged["rungs"] if r["id"] == "fable-class")
        assert fable_rung["models"] == ["anthropic/claude-fable-5"]
    finally:
        set_network_io(None, None)
