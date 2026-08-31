"""tests/test_easy_setup_compute.py — easy-setup wizard pure compute.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 6 #2. The wizard
takes each role's DEFAULT rung cluster and reorders its ``models[]`` by the
operator's ranked provider preference (primary = preferred provider's model for
the tier, then the rest in preference order; providers absent from a tier's
cluster are simply not present). Filtered to credentialed providers. Judge keeps
its diversity form and must stay diversity-valid after the reorder.

Pure function — no HTTP, no real bot/user names; provider/model ids are the
catalog's own data (the only literal home is DEFAULT_MODEL_CATALOG).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import primary_bot  # noqa: E402
from primary_bot import (  # noqa: E402
    available_providers_for_resolution,
    compute_easy_setup_catalog,
    provider_of,
    resolve_role_with_availability,
    validate_pod_default_catalog,
    _resolve_judge_availability,
    _rung_models,
    _resolve_role_to_rung,
)


def _base():
    """The effective pod-default view (defaults ← pod, no pod override)."""
    return primary_bot.pod_default_catalog_view({})


def _role_models(catalog, role):
    rung = _resolve_role_to_rung(catalog, role)
    return _rung_models(catalog, rung) if rung else []


def test_preferred_provider_leads_each_tier():
    base = _base()
    # All three providers credentialed; prefer google, then openai, then anthropic.
    order = ["google", "openai", "anthropic"]
    cred = {"google", "openai", "anthropic"}
    out = compute_easy_setup_catalog(base, order, credentialed_providers=cred)

    # Fast (haiku-class) holds all three providers — google must lead.
    fast = _role_models(out, "fast")
    assert provider_of(fast[0]) == "google"
    # The remaining models follow in preference order (openai before anthropic).
    provs = [provider_of(m) for m in fast]
    # ranked subsequence must respect preference order
    ranks = [order.index(p) for p in provs if p in order]
    assert ranks == sorted(ranks)


def test_absent_provider_skipped_not_invented():
    base = _base()
    # Prefer google first, but max (fable-class) has only anthropic — google
    # is absent from that cluster, so it is simply not present; no model invented.
    order = ["google", "openai", "anthropic"]
    cred = {"google", "openai", "anthropic"}
    out = compute_easy_setup_catalog(base, order, credentialed_providers=cred)
    max_models = _role_models(out, "max")
    assert max_models, "max tier should still resolve to its catalog model"
    assert all(provider_of(m) == "anthropic" for m in max_models)


def test_filter_to_credentialed_only():
    base = _base()
    # Only openai credentialed — every tier keeps only openai models, others gone.
    order = ["openai"]
    cred = {"openai"}
    out = compute_easy_setup_catalog(base, order, credentialed_providers=cred)
    for rung in out["rungs"]:
        for m in rung["models"]:
            assert provider_of(m) == "openai"
    # A tier whose cluster has no openai model (e.g. max) is empty, not invented.
    assert _role_models(out, "max") == []


def test_judge_stays_diversity_valid_after_reorder():
    base = _base()
    # Anthropic-first preference would put anthropic at the head of standard;
    # judge must still find a non-anthropic model in its enriched sonnet-class
    # rung (openai/google/xai all live there now).
    order = ["anthropic", "openai", "google"]
    cred = {"anthropic", "openai", "google"}
    out = compute_easy_setup_catalog(base, order, credentialed_providers=cred)
    # Judge keeps its structured {rung, provider:"not-standard"} form.
    assert out["roles"]["judge"].get("provider") == "not-standard"
    # The full-catalog validator (which checks judge diversity) passes.
    assert validate_pod_default_catalog(out) is None


def test_single_provider_easy_setup_saves_with_soft_judge():
    base = _base()
    # Only anthropic credentialed: every tier (incl. judge's standard rung)
    # filters to anthropic, so cross-vendor judge diversity is UNSATISFIABLE.
    # Per the soft runtime stance (#3040), the validator must NOT reject a
    # single-provider candidate — it saves, and the gateway routes the judge
    # with a `same_vendor_as_standard` advisory rather than failing. (Previously
    # this was rejected; the credential-matched default view legitimately
    # produces single-provider candidates on single-provider pods.)
    order = ["anthropic"]
    cred = {"anthropic"}
    out = compute_easy_setup_catalog(base, order, credentialed_providers=cred)
    provs = {provider_of(m) for rung in out["rungs"] for m in rung["models"]}
    provs.discard(None)
    assert provs == {"anthropic"}, "easy-setup should filter to the one provider"
    assert validate_pod_default_catalog(out) is None


def test_role_caps_and_roles_carried_through():
    base = _base()
    out = compute_easy_setup_catalog(
        base, ["anthropic", "openai"], credentialed_providers={"anthropic", "openai"},
    )
    # roleCaps survive untouched (power/max daily caps).
    if base.get("roleCaps"):
        assert out["roleCaps"] == base["roleCaps"]
    # Every ladder role still maps to a rung id.
    for role in ("fast", "standard", "power", "max"):
        assert _resolve_role_to_rung(out, role)


def test_no_credentialed_filter_keeps_all_reordered():
    base = _base()
    out = compute_easy_setup_catalog(base, ["openai", "anthropic", "google"])
    # Without a credentialed filter, every catalog model survives, reordered.
    base_fast = set(_role_models(base, "fast"))
    out_fast = set(_role_models(out, "fast"))
    assert base_fast == out_fast
    # openai leads fast since it's preferred and present.
    assert provider_of(_role_models(out, "fast")[0]) == "openai"


def test_unranked_provider_trails_ranked():
    base = _base()
    # Only rank openai; anthropic/google unranked → openai leads, others trail
    # in their original catalog order.
    out = compute_easy_setup_catalog(
        base, ["openai"], credentialed_providers={"openai", "anthropic", "google"},
    )
    fast = _role_models(out, "fast")
    assert provider_of(fast[0]) == "openai"
    assert "openai" not in [provider_of(m) for m in fast[1:]]


# ── Addendum 7 item 13: enriched per-provider cluster completeness ─────────────

_FOUR = ("anthropic", "openai", "google", "xai")


def test_enriched_clusters_carry_all_four_providers_where_tier_appropriate():
    """Each non-max tier's default cluster names every major provider's latest
    tier-appropriate model; max (fable-class) stays anthropic-only (no peer
    frontier SKU yet). Addendum 7 item 13."""
    base = _base()
    for role in ("fast", "standard", "power"):
        provs = {provider_of(m) for m in _role_models(base, role)}
        assert set(_FOUR).issubset(provs), (
            f"{role} cluster missing providers: {set(_FOUR) - provs}"
        )
    # max is anthropic-only — no other provider may appear.
    max_provs = {provider_of(m) for m in _role_models(base, "max")}
    assert max_provs == {"anthropic"}


def test_easy_setup_populates_every_tier_with_each_provider_in_pref_order():
    """With a provider_order spanning every credentialed provider, easy-setup
    fills EVERY non-max tier with each provider's model, ordered by preference —
    not a sparse cluster. Addendum 7 item 13."""
    base = _base()
    order = ["google", "xai", "openai", "anthropic"]
    cred = set(order)
    out = compute_easy_setup_catalog(base, order, credentialed_providers=cred)
    pref = {p: i for i, p in enumerate(order)}
    for role in ("fast", "standard", "power"):
        models = _role_models(out, role)
        provs = [provider_of(m) for m in models]
        # every credentialed provider that is tier-appropriate is present
        assert set(provs) == set(order), f"{role}: {provs}"
        # and the cluster is ordered by the operator's preference
        ranks = [pref[p] for p in provs]
        assert ranks == sorted(ranks), f"{role} not in preference order: {provs}"
    # max stays anthropic-only (the wizard never invents peer-frontier models).
    assert {provider_of(m) for m in _role_models(out, "max")} == {"anthropic"}


# ── Addendum 7 item 14: judge secondary-first (provider diversity) ─────────────


def _resolve(out, role, cred):
    avail = available_providers_for_resolution(out, cred)
    if role == "judge":
        return _resolve_judge_availability(out, avail).get("model")
    return resolve_role_with_availability(out, role, avail).get("model")


def test_judge_leads_with_secondary_preference_provider():
    """After easy-setup with a provider_order, standard resolves to the PRIMARY
    preference and judge resolves to the SECONDARY preference (the highest-pref
    provider that is NOT standard's) — secondary-first, the diversity goal.
    Addendum 7 item 14."""
    base = _base()
    cred = set(_FOUR)
    # Exercise several preference orders; standard==order[0], judge==order[1].
    for order in (
        ["anthropic", "google", "openai", "xai"],
        ["openai", "anthropic", "google", "xai"],
        ["google", "xai", "anthropic", "openai"],
        ["xai", "openai", "google", "anthropic"],
    ):
        out = compute_easy_setup_catalog(base, order, credentialed_providers=cred)
        std_p = provider_of(_resolve(out, "standard", cred))
        judge_p = provider_of(_resolve(out, "judge", cred))
        assert std_p == order[0], f"{order}: standard={std_p}, expected primary"
        assert judge_p == order[1], (
            f"{order}: judge={judge_p}, expected secondary (order[1]); "
            "judge must lead with the highest-pref non-standard provider"
        )
        # And the not-standard form is preserved (never special-cased by name).
        assert out["roles"]["judge"].get("provider") == "not-standard"


# ── Pinned dated-snapshot dedupe (adopt-path residue) ──────────────────────────


def test_dated_snapshot_dropped_when_rolling_base_present():
    """A rung carrying both a pinned dated snapshot and its rolling base id
    (adopt-path residue from before discovery excluded dated aliases) keeps
    only the rolling base — the wizard never offers the same model twice."""
    base = _base()
    fast_rung = _resolve_role_to_rung(base, "fast")
    for rung in base["rungs"]:
        if rung["id"] == fast_rung:
            assert "anthropic/claude-haiku-4-5" in rung["models"]
            rung["models"] = ["anthropic/claude-haiku-4-5-20251001"] + rung["models"]
    out = compute_easy_setup_catalog(
        base, ["anthropic"], credentialed_providers={"anthropic"},
    )
    fast = _role_models(out, "fast")
    assert fast[0] == "anthropic/claude-haiku-4-5"
    assert "anthropic/claude-haiku-4-5-20251001" not in fast


def test_dated_snapshot_kept_without_base_sibling():
    """A pinned snapshot with NO rolling base id in the rung is a legitimate
    operator choice — the dedupe must not touch it."""
    base = _base()
    fast_rung = _resolve_role_to_rung(base, "fast")
    for rung in base["rungs"]:
        if rung["id"] == fast_rung:
            rung["models"] = [
                m for m in rung["models"] if m != "anthropic/claude-haiku-4-5"
            ]
            rung["models"].insert(0, "anthropic/claude-haiku-4-5-20251001")
    out = compute_easy_setup_catalog(
        base, ["anthropic"], credentialed_providers={"anthropic"},
    )
    assert "anthropic/claude-haiku-4-5-20251001" in _role_models(out, "fast")


def test_latest_suffix_is_not_a_dated_snapshot():
    """"-latest"/"-preview" suffixes are NOT dated snapshots — a rolling
    "-chat-latest" id must survive even when its stripped base is present."""
    base = _base()
    fast_rung = _resolve_role_to_rung(base, "fast")
    for rung in base["rungs"]:
        if rung["id"] == fast_rung:
            rung["models"] = rung["models"] + [
                "openai/gpt-5.3-chat", "openai/gpt-5.3-chat-latest",
            ]
    out = compute_easy_setup_catalog(
        base, ["openai"], credentialed_providers={"openai"},
    )
    fast = _role_models(out, "fast")
    assert "openai/gpt-5.3-chat-latest" in fast
    assert "openai/gpt-5.3-chat" in fast
