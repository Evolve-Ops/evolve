"""tests/test_model_auto_upgrade.py — the auto-upgrade ELIGIBILITY engine.

Spec: internal/spec-model-auto-upgrade-2026-07-30.md (Phase 1 — ships dark).

Everything here runs against SYNTHETIC listings + synthetic pricing: the spec
makes steps 2-6 of the apply path pure precisely so the eligibility decision is
testable without touching config, and these tests hold that line — no file is
written, no config is mutated, no network call is made.

The matrix mirrors the spec's own guards:
  - a same-family, same-price, GA, single-rung upgrade → ELIGIBLE
  - an unpriced target → HELD (unknown price is never parity — the load-bearing
    rule; the 2026-07-31 cost runaway was a cost question answered by silence)
  - a repriced-upward target → HELD, and the reason names both prices
  - a preview / dated-snapshot target → HELD as not-GA
  - a predecessor in two rungs → HELD as ambiguous
  - a provider whose listing failed → reported UNCHECKED, never "up to date"
  - version ranking is NUMERIC (…-10 beats …-4-5), never lexicographic
  - the pod toggle governs Use-defaults bots and does NOT reach Custom ones,
    with migration-residue Custom reading differently from operator-chosen
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import model_auto_upgrade as mau  # noqa: E402
import model_discovery as md  # noqa: E402

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)

# Synthetic provider/model names — TESTS are one of the three legal homes for a
# provider/model literal (spec §Addendum 3.B); the engine itself holds none.
PROV = "anthropic"
CUR = "claude-opus-4-8"
NEW = "claude-opus-5-0"
RUNG = "opus-class"


def _lm(model_id: str, provider: str = PROV, **kw) -> md.ListedModel:
    return md.ListedModel(
        provider=provider,
        model_id=model_id,
        qualified_id=f"{provider}/{model_id}",
        capabilities=kw.pop("capabilities", ["chat"]),
        **kw,
    )


def _network(*, pod_models: dict | None = None, bots: dict | None = None) -> dict:
    return {
        "models": pod_models if pod_models is not None else {
            "rungs": [
                {"id": RUNG, "costClass": "high",
                 "models": [f"{PROV}/{CUR}"]},
            ],
            "roles": {"power": RUNG},
        },
        "bots": bots or {},
    }


def _pricing(*rows: tuple[str, float, float]) -> dict:
    return {
        "models": [
            {"provider": PROV, "model_id": mid, "input_cost_per_token": inp,
             "output_cost_per_token": out, "source": "litellm"}
            for mid, inp, out in rows
        ],
    }


def _upgrade(current: str = CUR, latest: str = NEW, rung: str = RUNG):
    return md.VersionUpgrade(
        provider=PROV,
        family=md._family_of(current),
        current_model=f"{PROV}/{current}",
        latest_model=f"{PROV}/{latest}",
        rung_slug=rung,
        roles=["power"],
    )


def _run(**kw):
    """compute_auto_upgrades with the synthetic defaults, bot docs INJECTED so
    no per-bot file is ever read."""
    kw.setdefault("network", _network())
    kw.setdefault("listing_by_provider", {PROV: [_lm(CUR), _lm(NEW)]})
    kw.setdefault("pricing_cache", _pricing((CUR, 15e-6, 75e-6), (NEW, 15e-6, 75e-6)))
    kw.setdefault("upgrades", [_upgrade()])
    kw.setdefault("bot_tier_docs", {})
    kw.setdefault("now", NOW)
    network = kw.pop("network")
    return mau.compute_auto_upgrades(network, **kw)


def _codes(decision) -> list[str]:
    return [h.code for h in decision.holds]


# ── The happy path ────────────────────────────────────────────────────────────

def test_same_price_ga_single_rung_upgrade_is_eligible():
    rep = _run()
    assert len(rep.eligible) == 1 and not rep.held
    d = rep.eligible[0]
    assert d.latest_model.endswith(NEW)
    # Eligible on the guards that EXIST — liveness (spec condition 6) is Phase 2
    # and must stay visibly pending so Phase 3 cannot apply on a half-checked
    # verdict.
    assert d.pending_guards == ("liveness",)


def test_cheaper_target_is_eligible():
    rep = _run(pricing_cache=_pricing((CUR, 15e-6, 75e-6), (NEW, 9e-6, 40e-6)))
    assert len(rep.eligible) == 1


def test_ships_dark_would_auto_apply_is_false_until_a_scope_enables_it():
    rep = _run()
    d = rep.eligible[0]
    assert d.eligible is True
    assert d.would_auto_apply is False        # nothing is enabled anywhere
    assert d.enabled_scopes == []
    assert "turn automatic updates on" in d.summary


def test_pod_toggle_on_makes_the_pod_scope_apply_it():
    net = _network()
    net["models"]["autoUpgrade"] = {"enabled": True}
    rep = _run(network=net)
    d = rep.eligible[0]
    assert d.enabled_scopes == ["pod"] and d.would_auto_apply is True
    assert d.summary == "Would update automatically."


# ── Cost guard — the load-bearing one ─────────────────────────────────────────

def test_unpriced_target_is_never_eligible():
    """Absence of a price is not evidence of parity (spec §Cost guard)."""
    rep = _run(pricing_cache=_pricing((CUR, 15e-6, 75e-6)))
    assert not rep.eligible and len(rep.held) == 1
    d = rep.held[0]
    assert mau.HOLD_TARGET_PRICE_UNKNOWN in _codes(d)
    assert NEW in d.primary_hold.message
    assert "not evidence of the same price" in d.primary_hold.message


def test_empty_pricing_cache_holds_rather_than_waves_through():
    rep = _run(pricing_cache=None)
    assert not rep.eligible
    assert mau.HOLD_TARGET_PRICE_UNKNOWN in _codes(rep.held[0])


def test_unpriced_predecessor_holds_too():
    rep = _run(pricing_cache=_pricing((NEW, 15e-6, 75e-6)))
    assert mau.HOLD_CURRENT_PRICE_UNKNOWN in _codes(rep.held[0])


def test_repricing_upward_is_held_and_names_both_prices():
    rep = _run(pricing_cache=_pricing((CUR, 15e-6, 75e-6), (NEW, 18e-6, 75e-6)))
    d = rep.held[0]
    assert mau.HOLD_COST_REGRESSION in _codes(d)
    hold = next(h for h in d.holds if h.code == mau.HOLD_COST_REGRESSION)
    assert "$15.00/1M" in hold.message and "$18.00/1M" in hold.message
    assert "input" in hold.message
    # Durable: it will not resolve itself, so the card may nag once then quiet.
    assert hold.durable is True
    assert d.cost_evidence["axes"]["input"] == {"current": 15e-6, "latest": 18e-6}


def test_output_axis_regression_is_caught_too():
    rep = _run(pricing_cache=_pricing((CUR, 15e-6, 75e-6), (NEW, 15e-6, 90e-6)))
    hold = next(h for h in rep.held[0].holds if h.code == mau.HOLD_COST_REGRESSION)
    assert "output" in hold.message


def test_require_cost_non_regression_off_waives_the_guard_but_records_it():
    net = _network()
    net["models"]["autoUpgrade"] = {
        "enabled": True, "requireCostNonRegression": False,
    }
    rep = _run(network=net,
               pricing_cache=_pricing((CUR, 15e-6, 75e-6), (NEW, 18e-6, 75e-6)))
    d = rep.eligible[0]
    assert d.cost_evidence["waived"] is True
    assert d.cost_evidence["waived_holds"][0]["code"] == mau.HOLD_COST_REGRESSION


# ── GA guard ──────────────────────────────────────────────────────────────────

def test_preview_target_is_held_as_not_ga():
    prev = f"{NEW}-preview"
    rep = _run(
        listing_by_provider={PROV: [_lm(CUR), _lm(prev)]},
        upgrades=[_upgrade(latest=prev)],
        pricing_cache=_pricing((CUR, 15e-6, 75e-6), (prev, 15e-6, 75e-6)),
    )
    d = rep.held[0]
    assert mau.HOLD_TARGET_NOT_GA in _codes(d)
    assert "preview release" in d.primary_hold.message


def test_ga_marker_match_is_token_exact_not_substring():
    # "express" contains "exp" but is not a pre-release marker.
    assert mau.prerelease_markers("model-express-5") == []
    assert mau.prerelease_markers("model-5-exp") == ["exp"]


def test_dated_snapshot_of_a_listed_base_is_held():
    snap = f"{NEW}-20260901"
    rep = _run(
        listing_by_provider={PROV: [_lm(CUR), _lm(NEW), _lm(snap)]},
        upgrades=[_upgrade(latest=snap)],
        pricing_cache=_pricing((CUR, 15e-6, 75e-6), (snap, 15e-6, 75e-6)),
    )
    assert mau.HOLD_TARGET_SNAPSHOT_ALIAS in _codes(rep.held[0])


def test_require_ga_off_lets_a_preview_through():
    prev = f"{NEW}-preview"
    net = _network()
    net["models"]["autoUpgrade"] = {"enabled": True, "requireGA": False}
    rep = _run(
        network=net,
        listing_by_provider={PROV: [_lm(CUR), _lm(prev)]},
        upgrades=[_upgrade(latest=prev)],
        pricing_cache=_pricing((CUR, 15e-6, 75e-6), (prev, 15e-6, 75e-6)),
    )
    assert len(rep.eligible) == 1


# ── Placement guard (condition 7) ─────────────────────────────────────────────

def test_predecessor_in_two_rungs_is_ambiguous_and_held():
    net = _network(pod_models={
        "rungs": [
            {"id": RUNG, "costClass": "high", "models": [f"{PROV}/{CUR}"]},
            {"id": "max-class", "costClass": "premium",
             "models": [f"{PROV}/{CUR}"]},
        ],
        "roles": {"power": RUNG, "max": "max-class"},
    })
    rep = _run(network=net)
    d = rep.held[0]
    assert mau.HOLD_RUNG_AMBIGUOUS in _codes(d)
    assert "more than one tier" in d.primary_hold.message


def test_a_bots_own_second_rung_makes_the_predecessor_ambiguous():
    """The ambiguity can come from a BOT's catalog, not just the pod's — the
    occupancy walk covers every catalog the pod routes from."""
    docs = {"custom": {
        "rungs": [
            {"id": RUNG, "costClass": "high", "models": [f"{PROV}/{CUR}"]},
            {"id": "max-class", "costClass": "premium",
             "models": [f"{PROV}/{CUR}"]},
        ],
        "roles": {"power": RUNG, "max": "max-class"},
    }}
    rep = _run(
        network=_network(bots={"custom": {"user": "custom"}}),
        bot_tier_docs=docs,
    )
    assert mau.HOLD_RUNG_AMBIGUOUS in _codes(rep.held[0])


def test_unlocated_predecessor_is_held():
    rep = _run(upgrades=[_upgrade(rung="")])
    assert mau.HOLD_RUNG_UNKNOWN in _codes(rep.held[0])


def test_model_no_scope_owns_is_held_as_scope_unknown():
    """Reachable through the merged catalog but written into no document an
    apply could edit → not an automatic update, however good the version is."""
    rep = _run(network=_network(pod_models={}))
    assert mau.HOLD_SCOPE_UNKNOWN in _codes(rep.held[0])


# ── Same-provider / same-family / strictly-newer (conditions 1-3) ─────────────

def test_cross_family_candidate_is_refused_even_if_injected():
    other = "claude-sonnet-9"
    rep = _run(
        listing_by_provider={PROV: [_lm(CUR), _lm(other)]},
        upgrades=[_upgrade(latest=other)],
        pricing_cache=_pricing((CUR, 15e-6, 75e-6), (other, 3e-6, 15e-6)),
    )
    assert mau.HOLD_FAMILY_MISMATCH in _codes(rep.held[0])


def test_cross_provider_candidate_is_refused_even_if_injected():
    up = md.VersionUpgrade(
        provider=PROV, family=md._family_of(CUR),
        current_model=f"{PROV}/{CUR}", latest_model=f"other/{NEW}",
        rung_slug=RUNG, roles=["power"],
    )
    rep = _run(upgrades=[up])
    assert mau.HOLD_PROVIDER_MISMATCH in _codes(rep.held[0])


def test_version_ranking_is_numeric_not_lexicographic():
    """``claude-sonnet-10`` must beat ``claude-sonnet-4-5``: a lexicographic
    compare sorts "10" below "4" and would report the newer version as older."""
    cur, new = "claude-sonnet-4-5", "claude-sonnet-10"
    net = _network(pod_models={
        "rungs": [{"id": "sonnet-class", "costClass": "medium",
                   "models": [f"{PROV}/{cur}"]}],
        "roles": {"standard": "sonnet-class"},
    })
    rep = _run(
        network=net,
        listing_by_provider={PROV: [_lm(cur), _lm(new)]},
        pricing_cache=_pricing((cur, 3e-6, 15e-6), (new, 3e-6, 15e-6)),
        upgrades=[_upgrade(current=cur, latest=new, rung="sonnet-class")],
    )
    assert len(rep.eligible) == 1, [h.code for h in rep.held[0].holds]


def test_older_candidate_is_refused_as_not_newer():
    rep = _run(
        listing_by_provider={PROV: [_lm(CUR)]},
        upgrades=[_upgrade(current=NEW, latest=CUR)],
        pricing_cache=_pricing((CUR, 15e-6, 75e-6), (NEW, 15e-6, 75e-6)),
    )
    assert mau.HOLD_NOT_NEWER in _codes(rep.held[0])


# ── Degraded listings — rule 2 ────────────────────────────────────────────────

def test_failed_listing_is_reported_unchecked_not_up_to_date():
    rep = _run(
        listing_by_provider={},
        upgrades=[],
        listing_degraded=[{"provider": PROV, "reason": "HTTP 503"}],
    )
    assert rep.degraded is True
    skipped = rep.skipped_providers[0]
    assert skipped.provider == PROV and "503" in skipped.reason
    assert skipped.model_count >= 1
    assert "not the same as being up to date" in skipped.message


def test_empty_listing_for_a_pod_provider_is_also_unchecked():
    """An EMPTY list is not "nothing newer" — the silent-monitor-drift lesson."""
    rep = _run(listing_by_provider={PROV: []}, upgrades=[])
    assert [p.provider for p in rep.skipped_providers] == [PROV]


def test_healthy_listing_reports_no_skipped_providers():
    assert _run().skipped_providers == []


# ── Every held item is reported ───────────────────────────────────────────────

def test_a_run_with_nothing_eligible_still_reports_what_it_considered():
    rep = _run(pricing_cache=None)
    assert rep.eligible == [] and rep.considered == 1
    payload = rep.to_dict()
    assert payload["considered"] == 1
    assert len(payload["held"]) == 1
    row = payload["held"][0]
    assert row["holds"] and row["holds"][0]["code"] and row["holds"][0]["message"]
    assert row["eligible"] is False and row["wouldAutoApply"] is False


def test_upgrade_key_is_per_rung_and_per_version():
    a = mau.upgrade_key(PROV, RUNG, f"{PROV}/{NEW}")
    assert a == mau.upgrade_key(PROV, RUNG, NEW)
    assert a != mau.upgrade_key(PROV, "max-class", NEW)
    assert a != mau.upgrade_key(PROV, RUNG, "claude-opus-5-1")


# ── Visibility floor (spec §Cadence; ledger is a Phase-3 input) ───────────────

def test_upgrade_younger_than_min_visible_days_is_held():
    key = mau.upgrade_key(PROV, RUNG, NEW)
    rep = _run(first_seen={key: (NOW - timedelta(days=2)).isoformat()})
    d = rep.held[0]
    assert mau.HOLD_NOT_VISIBLE_LONG_ENOUGH in _codes(d)
    assert d.visible_days == 2.0
    assert "5 days to go" in d.primary_hold.message


def test_upgrade_past_the_floor_is_eligible():
    key = mau.upgrade_key(PROV, RUNG, NEW)
    rep = _run(first_seen={key: (NOW - timedelta(days=9)).isoformat()})
    assert len(rep.eligible) == 1


def test_no_ledger_means_no_visibility_verdict_not_a_silent_pass_or_fail():
    d = _run().eligible[0]
    assert d.visible_days is None


# ── Scope + the migration-era Custom hazard (spec §Scope) ────────────────────

def _two_bot_network(**auto) -> dict:
    net = _network(bots={"follower": {"user": "follower"},
                         "custom": {"user": "custom"},
                         "legacy": {"user": "legacy"}})
    if auto:
        net["models"]["autoUpgrade"] = auto
    return net


def _docs(custom_auto: dict | None = None) -> dict:
    """follower = Use-pod-defaults (no rungs); custom = deliberately Custom
    (its own, different rung content); legacy = Custom by MIGRATION — its rungs
    are a byte-copy of the pod default, so it resolves identically."""
    pod_rung = {"id": RUNG, "costClass": "high", "models": [f"{PROV}/{CUR}"]}
    custom_doc: dict = {
        "rungs": [{"id": RUNG, "costClass": "high",
                   "models": [f"{PROV}/{CUR}", f"{PROV}/claude-opus-4-7"]}],
        "roles": {"power": RUNG},
    }
    if custom_auto is not None:
        custom_doc["autoUpgrade"] = custom_auto
    return {
        "follower": {},
        "custom": custom_doc,
        "legacy": {"rungs": [dict(pod_rung)], "roles": {"power": RUNG}},
    }


def test_pod_toggle_governs_use_defaults_bots_and_not_custom_ones():
    rep = _run(network=_two_bot_network(enabled=True), bot_tier_docs=_docs())
    gov = rep.governance()
    assert gov["podEnabled"] is True
    assert gov["botsFollowingPod"] == ["follower"]
    assert gov["botsCustomOperatorChosen"] == ["custom"]
    # The migration one-shot made this bot Custom; no operator ever chose it.
    assert gov["botsCustomMigrationResidue"] == ["legacy"]
    assert "follower" in gov["note"] and "custom" in gov["note"]
    assert "left over from the tier migration" in gov["note"]


def test_migration_residue_reads_differently_from_a_deliberate_custom_bot():
    rep = _run(network=_two_bot_network(enabled=True), bot_tier_docs=_docs())
    by_id = {s.scope_id: s for s in rep.scopes}
    assert by_id["custom"].custom_origin == "operator-chosen"
    assert by_id["legacy"].custom_origin == "migration-residue"
    assert "reset it to pod defaults" in by_id["legacy"].note
    assert "keeps its own automatic-update setting" in by_id["custom"].note


def test_pod_toggle_does_not_reach_a_custom_bot():
    rep = _run(network=_two_bot_network(enabled=True), bot_tier_docs=_docs())
    by_id = {s.scope_id: s for s in rep.scopes}
    assert by_id["pod"].enabled is True
    assert by_id["follower"].enabled is True        # inherits the pod wholesale
    assert by_id["custom"].enabled is False         # its own scope, never set
    assert by_id["custom"].policy.enabled_source == "code-default"


def test_a_custom_bot_owns_its_toggle_and_inherits_the_pods_cadence():
    net = _two_bot_network(enabled=False, minVisibleDays=10, applyDay="thursday")
    rep = _run(network=net, bot_tier_docs=_docs(custom_auto={"enabled": True}))
    scope = {s.scope_id: s for s in rep.scopes}["custom"]
    assert scope.enabled is True and scope.policy.enabled_source == "bot"
    assert scope.policy.min_visible_days == 10 and scope.policy.apply_day == "thursday"


def test_an_upgrade_in_a_custom_bots_rung_is_attributed_to_that_bot():
    net = _two_bot_network(enabled=False)
    net["models"] = {"roles": {"power": RUNG}}       # pod pins no models itself
    rep = _run(network=net, bot_tier_docs=_docs(custom_auto={"enabled": True}))
    d = (rep.eligible + rep.held)[0]
    assert set(d.scopes) == {"custom", "legacy"}
    assert d.enabled_scopes == ["custom"]


def test_policy_defaults_survive_a_config_with_no_block():
    p = mau.pod_policy({"models": {}})
    assert p.enabled is False and p.enabled_source == "code-default"
    assert p.require_cost_non_regression is True and p.require_ga is True
    assert p.min_visible_days == 7


def test_junk_policy_values_fall_back_to_the_safe_default():
    p = mau.pod_policy({"models": {"autoUpgrade": {
        "enabled": "true", "minVisibleDays": -3, "applyDay": 7,
    }}})
    assert p.enabled is False           # a string is not a decision to mutate
    assert p.min_visible_days == 7
    assert p.apply_day == "tuesday"


# ── Nothing mutates ───────────────────────────────────────────────────────────

def test_engine_does_not_mutate_the_config_it_reads():
    import copy

    net = _two_bot_network(enabled=True)
    docs = _docs(custom_auto={"enabled": True})
    before_net, before_docs = copy.deepcopy(net), copy.deepcopy(docs)
    _run(network=net, bot_tier_docs=docs)
    assert net == before_net and docs == before_docs


def test_row_naming_a_rung_the_predecessor_does_not_occupy_is_held():
    """Defence in depth: an injected row whose ``rung_slug`` disagrees with
    where the predecessor actually sits is an unconfirmable apply target."""
    rep = _run(upgrades=[_upgrade(rung="not-a-real-rung")])
    assert mau.HOLD_RUNG_UNKNOWN in _codes(rep.held[0])


def test_require_ga_off_also_waives_the_dated_snapshot_hold():
    """Spec condition 4 covers pre-release markers AND snapshot aliases, so the
    one knob that waives it waives both."""
    snap = f"{NEW}-20260901"
    net = _network()
    net["models"]["autoUpgrade"] = {"enabled": True, "requireGA": False}
    rep = _run(
        network=net,
        listing_by_provider={PROV: [_lm(CUR), _lm(NEW), _lm(snap)]},
        upgrades=[_upgrade(latest=snap)],
        pricing_cache=_pricing((CUR, 15e-6, 75e-6), (snap, 15e-6, 75e-6)),
    )
    assert len(rep.eligible) == 1


# ── Occupancy is measured where the WRITE lands, not on the merged catalog ────
# Regression pin for the review finding: the shipped DEFAULT_MODEL_CATALOG puts
# one model in several rungs on purpose (each rung carries the same
# cross-provider alternates — e.g. openai/gpt-4.1 sits in both sonnet-class
# and opus-class). Judging condition 7 against the merged view therefore
# reported an ordinary real upgrade as "sits in more than one tier" while its
# own Update button had exactly one target.

def _stock_standard_model() -> str:
    """A model the shipped catalog puts in BOTH sonnet-class and opus-class."""
    from primary_bot import DEFAULT_MODEL_CATALOG

    rungs = {r["id"]: list(r.get("models") or []) for r in DEFAULT_MODEL_CATALOG["rungs"]}
    shared = set(rungs["sonnet-class"]) & set(rungs["opus-class"])
    assert shared, "catalog no longer shares cross-provider alternates across rungs"
    return sorted(shared)[0]


def test_a_stock_ladder_upgrade_is_not_falsely_ambiguous():
    stock = _stock_standard_model()
    cur = md._bare_id(stock)
    prov = stock.split("/", 1)[0]
    new = f"{md._family_of(cur)}-99"
    net = {
        "models": {
            "rungs": [{"id": "sonnet-class", "costClass": "medium",
                       "models": [stock]}],
            "roles": {"standard": "sonnet-class"},
        },
        "bots": {},
    }
    up = md.VersionUpgrade(
        provider=prov, family=md._family_of(cur),
        current_model=stock, latest_model=f"{prov}/{new}",
        rung_slug="sonnet-class", roles=["standard"],
    )
    rep = _run(
        network=net,
        listing_by_provider={prov: [_lm(cur, prov), _lm(new, prov)]},
        pricing_cache={"models": [
            {"provider": prov, "model_id": m, "input_cost_per_token": 3e-6,
             "output_cost_per_token": 15e-6, "source": "litellm"}
            for m in (cur, new)
        ]},
        upgrades=[up],
    )
    assert not rep.held, [h.code for h in rep.held[0].holds]
    assert len(rep.eligible) == 1


def test_two_scopes_each_with_one_rung_is_not_ambiguous():
    """The pod and a Custom bot each placing the model in their own single rung
    is two unambiguous applies, not an ambiguity."""
    docs = {"custom": {
        "rungs": [{"id": RUNG, "costClass": "high", "models": [f"{PROV}/{CUR}"]}],
        "roles": {"power": RUNG},
    }}
    rep = _run(
        network=_network(bots={"custom": {"user": "custom"}}),
        bot_tier_docs=docs,
    )
    assert len(rep.eligible) == 1
    assert set(rep.eligible[0].scopes) == {"pod", "custom"}


# ── Cost-band backstop (previously untested) ─────────────────────────────────

def test_band_regression_holds_even_when_the_per_axis_compare_passes():
    """A same-or-cheaper price that nonetheless lands in a costlier CLASS is a
    repricing of the line, not a version bump."""
    import model_cost_bands

    rep = _run(pricing_cache=_pricing((CUR, 15e-6, 75e-6), (NEW, 15e-6, 75e-6)))
    assert len(rep.eligible) == 1          # baseline: bands equal
    bands = {CUR: model_cost_bands.COST_BANDS[0], NEW: model_cost_bands.COST_BANDS[-1]}
    orig = model_cost_bands.resolve_band

    def _fake(provider, model_id, **kw):
        return model_cost_bands.BandResolution(
            band=bands.get(model_id), source="pricing", evidence={})

    model_cost_bands.resolve_band = _fake
    try:
        rep = _run(pricing_cache=_pricing((CUR, 15e-6, 75e-6), (NEW, 15e-6, 75e-6)))
    finally:
        model_cost_bands.resolve_band = orig
    d = rep.held[0]
    assert mau.HOLD_COST_BAND_REGRESSION in _codes(d)
    assert next(h for h in d.holds
                if h.code == mau.HOLD_COST_BAND_REGRESSION).durable is True


def test_unplaceable_band_says_the_prices_are_known_when_they_are():
    """The reason must not tell an operator their model is unpriced while the
    card is showing them its price."""
    import model_cost_bands

    orig = model_cost_bands.resolve_band
    model_cost_bands.resolve_band = lambda p, m, **kw: (
        model_cost_bands.BandResolution(band=None, source="unknown", evidence={}))
    try:
        rep = _run(pricing_cache=_pricing((CUR, 2e-6, 8e-6), (NEW, 2e-6, 8e-6)))
    finally:
        model_cost_bands.resolve_band = orig
    hold = next(h for h in rep.held[0].holds if h.code == mau.HOLD_COST_BAND_UNKNOWN)
    assert hold.detail["priced"] is True
    assert "is priced" in hold.message and "does not cost more" in hold.message


def test_negative_or_boolean_prices_are_not_treated_as_prices():
    cache = {"models": [
        {"provider": PROV, "model_id": CUR, "input_cost_per_token": 15e-6,
         "output_cost_per_token": 75e-6, "source": "litellm"},
        {"provider": PROV, "model_id": NEW, "input_cost_per_token": -1,
         "output_cost_per_token": True, "source": "litellm"},
    ]}
    assert mau.HOLD_TARGET_PRICE_UNKNOWN in _codes(_run(pricing_cache=cache).held[0])


# ── Degraded / ignored scope reporting ───────────────────────────────────────

def test_an_unreadable_bot_is_never_reported_as_following_the_pod():
    net = _two_bot_network(enabled=True)
    scopes = mau.resolve_scopes(net, _docs(), unreadable_bots={"follower"})
    by_id = {s.scope_id: s for s in scopes}
    assert by_id["follower"].readable is False
    assert "Could not read follower's model config" in by_id["follower"].note
    rep = mau.AutoUpgradeReport(computed_at="", scopes=scopes)
    gov = rep.governance()
    assert gov["botsFollowingPod"] == []
    assert gov["botsUnreadable"] == ["follower"]
    assert "unknown — not a yes" in gov["note"]


def test_a_use_defaults_bot_carrying_its_own_toggle_is_reported_not_dropped():
    """The key does nothing while the bot follows the pod — say so rather than
    silently discarding a setting the operator wrote."""
    net = _two_bot_network(enabled=False)
    docs = _docs()
    docs["follower"] = {"autoUpgrade": {"enabled": True}}
    rep = _run(network=net, bot_tier_docs=docs)
    scope = {s.scope_id: s for s in rep.scopes}["follower"]
    assert scope.enabled is False and scope.ignored_own_toggle is True
    assert "its own automatic-update setting is ignored" in scope.note
    assert rep.governance()["botsWithIgnoredToggle"] == ["follower"]


# ── Message quality ──────────────────────────────────────────────────────────

def test_prerelease_reason_reads_grammatically_for_every_marker():
    for token in mau._PRERELEASE_TOKENS:
        target = f"{NEW}-{token}"
        rep = _run(
            listing_by_provider={PROV: [_lm(CUR), _lm(target)]},
            upgrades=[_upgrade(latest=target)],
            pricing_cache=_pricing((CUR, 15e-6, 75e-6), (target, 15e-6, 75e-6)),
        )
        msg = next(h for h in rep.held[0].holds
                   if h.code == mau.HOLD_TARGET_NOT_GA).message
        assert " a a" not in msg and " a e" not in msg and " a i" not in msg
        assert "release, not a general one" in msg


def test_countdown_never_reads_zero_days_to_go_while_still_held():
    key = mau.upgrade_key(PROV, RUNG, NEW)
    rep = _run(first_seen={key: (NOW - timedelta(days=6, hours=15)).isoformat()})
    msg = next(h for h in rep.held[0].holds
               if h.code == mau.HOLD_NOT_VISIBLE_LONG_ENOUGH).message
    assert "about 1 day to go" in msg


def test_durable_reflects_every_hold_not_just_the_first():
    """An item held for a transient reason AND a repricing is still permanently
    held once the transient one clears — Phase 4's suppression keys on this."""
    rep = _run(
        upgrades=[_upgrade(rung="")],  # transient: unlocated
        pricing_cache=_pricing((CUR, 15e-6, 75e-6), (NEW, 18e-6, 75e-6)),
    )
    d = rep.held[0]
    assert d.primary_hold.code == mau.HOLD_RUNG_UNKNOWN
    assert d.primary_hold.durable is False
    assert mau.HOLD_COST_REGRESSION in _codes(d) and d.durable is True


def test_upgrade_key_is_case_insensitive_on_the_provider():
    assert mau.upgrade_key("Anthropic", RUNG, NEW) == mau.upgrade_key(PROV, RUNG, NEW)
