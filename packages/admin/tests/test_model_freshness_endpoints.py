"""tests/test_model_freshness_endpoints.py — Flask routes for the model-freshness feature.

Covers:
  D1 — POST /api/models/check-freshness returns advisories shaped per spec
  D2 — POST /api/models/update-tier rewrites the bot's tiers
  D3 — providers without keys are excluded from advisories
  GET /api/models/freshness-status reads the persisted summary

The endpoints lazy-import oc_cli.oc_full_config_get / oc_full_config_set / oc_keys_get
inside the handler. We monkeypatch those module-level functions so no subprocess
calls fire and the test runs against synthetic configs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Flask app with two synthetic bots, anthropic key on both, openai key on team_bot_a only."""
    from evolve_admin.web.server import create_app

    network = {
        "bots": {
            "team_bot_a": {"user": "team_bot_a"},
            "admin_bot": {"user": "admin_bot"},
            "team_bot_c": {"user": "team_bot_c"},
        },
        "sharedDir": str(tmp_path / "shared"),
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    (tmp_path / "shared").mkdir()

    # Synthetic bot configs.
    #   team_bot_a   — stale anthropic/tier2; tier3 ok; catalog only has 4-5 (no 4-6)
    #   admin_bot — anthropic-only, fully current
    #   team_bot_c — completely empty tiers (real-world case from Bug 2 report)
    from model_registry import RECOMMENDED
    rec_anthropic_t2 = RECOMMENDED["anthropic"]["tier2"]["model"]

    bot_state: dict = {
        "team_bot_a": {
            "tiers": {
                "tier1": {"models": [RECOMMENDED["anthropic"]["tier1"]["model"]]},
                "tier2": {"models": ["anthropic/claude-sonnet-4-2", "openai/gpt-4o"]},
                "tier3": {"models": [RECOMMENDED["anthropic"]["tier3"]["model"]]},
            },
            "catalog": [
                "anthropic/claude-sonnet-4-2",
                "anthropic/claude-haiku-4-5",
                "openai/gpt-4o",
            ],
        },
        "admin_bot": {
            "tiers": {
                "tier1": {"models": [RECOMMENDED["anthropic"]["tier1"]["model"]]},
                "tier2": {"models": [rec_anthropic_t2]},
                "tier3": {"models": [RECOMMENDED["anthropic"]["tier3"]["model"]]},
            },
            "catalog": [
                RECOMMENDED["anthropic"]["tier1"]["model"],
                rec_anthropic_t2,
                RECOMMENDED["anthropic"]["tier3"]["model"],
            ],
        },
        "team_bot_c": {
            "tiers": {},  # never configured — Bug 2's reproduction
            "catalog": ["anthropic/claude-haiku-4-5"],
        },
    }

    keys_state: dict = {
        "team_bot_a": {"keys": {
            "anthropic": {"api_key": True},
            "openai": {"api_key": True},
        }},
        "admin_bot": {"keys": {
            "anthropic": {"api_key": True},
            # no openai key for admin_bot
        }},
        "team_bot_c": {"keys": {
            "anthropic": {"api_key": True},
        }},
    }

    import oc_cli  # noqa

    def fake_full_config_get(bot_id, network_path=None):
        return {"bot": bot_id, **bot_state.get(bot_id, {})}

    def fake_full_config_set(bot_id, updates, network_path=None):
        # MERGE the named tiers into the bot's existing tiers — this mirrors
        # oc_model.json_full_config_set, which folds ONLY the tiers named in
        # the update into the rung store and leaves the rest untouched. (The
        # endpoints now send only the tiers they changed; a wholesale-replace
        # fake would silently drop the sibling tiers and mask that contract.)
        if "tiers" in updates:
            bot_state[bot_id].setdefault("tiers", {})
            bot_state[bot_id]["tiers"].update(updates["tiers"])
        if "catalog" in updates:
            bot_state[bot_id]["catalog"] = updates["catalog"]
        return {"bot": bot_id, **bot_state[bot_id], "generatedFallbacks": []}

    def fake_full_config_set_with_error(bot_id, updates, network_path=None):
        return fake_full_config_set(bot_id, updates, network_path), None

    def fake_keys_get(bot_id, network_path=None):
        return keys_state.get(bot_id, {"keys": {}})

    monkeypatch.setattr(oc_cli, "oc_full_config_get", fake_full_config_get)
    monkeypatch.setattr(oc_cli, "oc_full_config_set", fake_full_config_set)
    monkeypatch.setattr(
        oc_cli, "oc_full_config_set_with_error", fake_full_config_set_with_error
    )
    monkeypatch.setattr(oc_cli, "oc_keys_get", fake_keys_get)

    app = create_app(network_path)
    app.config["TESTING"] = True
    return {"app": app, "bot_state": bot_state, "keys_state": keys_state, "shared": tmp_path / "shared"}


# ── /api/models/freshness-status ──────────────────────────────────────────────


def test_freshness_status_empty_when_never_checked(app):
    with app["app"].test_client() as c:
        resp = c.get("/api/models/freshness-status")
        assert resp.status_code == 200
        assert resp.get_json() == {}


# ── /api/models/check-freshness (D1, D3) ──────────────────────────────────────


def test_check_freshness_reports_stale_tier(app):
    with app["app"].test_client() as c:
        resp = c.post("/api/models/check-freshness", json={})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["advisory_count"] >= 1

        # team_bot_a/anthropic/tier2 is stale → must appear
        team_bot_a_anthropic_t2 = [
            a for a in body["advisories"]
            if a["bot_id"] == "team_bot_a" and a["provider"] == "anthropic" and a["tier"] == "tier2"
        ]
        assert len(team_bot_a_anthropic_t2) == 1
        assert team_bot_a_anthropic_t2[0]["current_model"] == "anthropic/claude-sonnet-4-2"
        assert team_bot_a_anthropic_t2[0]["recommended_model"].startswith("anthropic/")
        assert "released" in team_bot_a_anthropic_t2[0]["recommended_released"] or len(
            team_bot_a_anthropic_t2[0]["recommended_released"]
        ) >= 8


def test_check_freshness_excludes_providers_without_keys(app):
    """admin_bot has no openai key — even if openai recommendation existed for its tiers,
    no openai advisory should appear for admin_bot."""
    with app["app"].test_client() as c:
        resp = c.post("/api/models/check-freshness", json={})
        body = resp.get_json()

        admin_bot_openai = [
            a for a in body["advisories"]
            if a["bot_id"] == "admin_bot" and a["provider"] == "openai"
        ]
        assert admin_bot_openai == []


def test_check_freshness_drift_credential_split(app):
    """Spec §Addendum 10 §B — Type-1 catalog drift is split by whether the bot
    is credentialed for the missing model's provider.

      - admin_bot: tier names openai/gpt-4o, NOT in catalog, admin_bot has no
        openai key → provider_credentialed=False (Reconcile can't help) +
        borrow_candidates attached.
      - team_bot_a: tier names openai/gpt-4o-mini, NOT in catalog, team_bot_a
        HAS an openai key → provider_credentialed=True (a reconcilable gap).
    """
    # admin_bot: a tier model from a provider it has no key for, missing from catalog.
    app["bot_state"]["admin_bot"]["tiers"]["tier2"] = {
        "models": ["openai/gpt-4o"],
    }
    # team_bot_a: a tier model from a provider it DOES hold a key for, missing
    # from catalog (gpt-4o-mini is not in team_bot_a's catalog).
    app["bot_state"]["team_bot_a"]["tiers"]["tier1"] = {
        "models": ["openai/gpt-4o-mini"],
    }

    with app["app"].test_client() as c:
        body = c.post("/api/models/check-freshness", json={}).get_json()
        drift = body.get("drift_findings") or []

        admin_gap = [
            d for d in drift
            if d["bot_id"] == "admin_bot"
            and d["kind"] == "tier_member_missing"
            and d["model_id"] == "openai/gpt-4o"
        ]
        assert len(admin_gap) == 1
        assert admin_gap[0]["provider_credentialed"] is False
        # The cred-gap finding carries borrow_candidates (a list) so the UI can
        # render "Copy openai from <bot>" without a second round-trip.
        assert isinstance(admin_gap[0].get("borrow_candidates"), list)

        team_bot_a_gap = [
            d for d in drift
            if d["bot_id"] == "team_bot_a"
            and d["kind"] == "tier_member_missing"
            and d["model_id"] == "openai/gpt-4o-mini"
        ]
        assert len(team_bot_a_gap) == 1
        assert team_bot_a_gap[0]["provider_credentialed"] is True
        # Reconcilable findings are NOT enriched with borrow_candidates.
        assert "borrow_candidates" not in team_bot_a_gap[0]


# ── §C tier-severity split (hard_break_tiers + tier_severity tagging) ─────────


def test_check_freshness_same_vendor_judge_is_advisory_not_hard_break(app):
    """Spec §Addendum 10 §C + soft-preference (2026-06-19) — provider diversity
    for judge is a recommendation, not a requirement:

      - admin_bot is anthropic-only → judge routes on anthropic (same vendor as
        Standard) → it is an ADVISORY, surfaced in advisory_tiers, NOT a hard
        break (a pure-resolution advisory with no drift finding).
      - team_bot_a holds anthropic + openai → judge resolves cross-vendor →
        neither a hard break nor an advisory.
    """
    with app["app"].test_client() as c:
        body = c.post("/api/models/check-freshness", json={}).get_json()
        hb = body.get("hard_break_tiers") or []
        adv = body.get("advisory_tiers") or []

        # admin_bot judge is NOT a hard break — it routes same-vendor.
        admin_judge_hb = [
            t for t in hb if t["bot_id"] == "admin_bot" and t["role"] == "judge"
        ]
        assert admin_judge_hb == [], "same-vendor judge must NOT be a hard break"

        # …it is surfaced as a soft advisory instead, carrying the doubled-up
        # provider for the cross-vendor nudge.
        admin_judge_adv = [
            t for t in adv if t["bot_id"] == "admin_bot" and t["role"] == "judge"
        ]
        assert len(admin_judge_adv) == 1, "admin_bot judge should be an advisory"
        assert admin_judge_adv[0].get("reason") == "same_vendor_as_standard"
        assert "same_vendor_provider" in admin_judge_adv[0]
        assert "borrow_candidates" in admin_judge_adv[0]

        # team_bot_a resolves cross-vendor — neither hard break nor advisory.
        team_judge_hb = [
            t for t in hb if t["bot_id"] == "team_bot_a" and t["role"] == "judge"
        ]
        team_judge_adv = [
            t for t in adv if t["bot_id"] == "team_bot_a" and t["role"] == "judge"
        ]
        assert team_judge_hb == [], "team_bot_a has a diverse provider — judge routes cross-vendor"
        assert team_judge_adv == [], "team_bot_a judge is cross-vendor — no advisory"


def test_check_freshness_tier_severity_tagging(app):
    """A credential-gap drift finding is tagged tier_severity from its role's
    classification: hard_break when the role won't route, dormant when it does.

    admin_bot's tiers all name openai/gpt-4o (no key, not in catalog) and carry
    no anthropic fallback, so the ladder roles can't route → the finding is
    tagged hard_break and the roles appear in the hard-break panel.
    """
    for tier in ("tier1", "tier2", "tier3"):
        app["bot_state"]["admin_bot"]["tiers"][tier] = {"models": ["openai/gpt-4o"]}

    with app["app"].test_client() as c:
        body = c.post("/api/models/check-freshness", json={}).get_json()
        drift = body.get("drift_findings") or []
        hb = body.get("hard_break_tiers") or []

        admin_gaps = [
            d for d in drift
            if d["bot_id"] == "admin_bot" and d.get("provider_credentialed") is False
        ]
        assert admin_gaps, "expected an admin_bot credential-gap finding"
        assert all(d.get("tier_severity") == "hard_break" for d in admin_gaps)

        admin_hb_roles = {t["role"] for t in hb if t["bot_id"] == "admin_bot"}
        assert {"power", "standard", "fast"} <= admin_hb_roles


def test_check_freshness_dormant_finding_when_role_still_routes(app):
    """A credential-gap entry whose role STILL routes (a credentialed model
    remains in the chain) is tagged dormant, not hard_break."""
    # tier2 names a credentialed in-catalog anthropic model PLUS an uncredentialed
    # openai model missing from catalog — standard still resolves to anthropic.
    from model_registry import RECOMMENDED
    app["bot_state"]["admin_bot"]["tiers"]["tier2"] = {
        "models": [RECOMMENDED["anthropic"]["tier2"]["model"], "openai/gpt-4o"],
    }
    app["bot_state"]["admin_bot"]["catalog"].append(
        RECOMMENDED["anthropic"]["tier2"]["model"],
    )

    with app["app"].test_client() as c:
        body = c.post("/api/models/check-freshness", json={}).get_json()
        drift = body.get("drift_findings") or []
        gap = [
            d for d in drift
            if d["bot_id"] == "admin_bot" and d["model_id"] == "openai/gpt-4o"
        ]
        assert len(gap) == 1
        assert gap[0]["tier_severity"] == "dormant"


def test_check_freshness_persists_summary(app):
    with app["app"].test_client() as c:
        c.post("/api/models/check-freshness", json={})

        # status endpoint should now reflect the saved summary
        resp = c.get("/api/models/freshness-status")
        body = resp.get_json()
        assert "checked_at" in body
        assert "advisories" in body


def test_check_freshness_no_advisories_when_all_current(app, monkeypatch):
    """If every bot uses the recommended model for every (provider × tier) it has a key for, no advisories."""
    from model_registry import RECOMMENDED
    # Set every bot to the full anthropic recommendation set
    full_anthropic = {
        t: {"models": [RECOMMENDED["anthropic"][t]["model"]]}
        for t in RECOMMENDED["anthropic"]
    }
    for bot_id in ("team_bot_a", "admin_bot", "team_bot_c"):
        app["bot_state"][bot_id]["tiers"] = {**full_anthropic}
    # Drop openai key from team_bot_a so we don't get openai-tier advisories either
    app["keys_state"]["team_bot_a"] = {"keys": {"anthropic": {"api_key": True}}}

    with app["app"].test_client() as c:
        resp = c.post("/api/models/check-freshness", json={})
        body = resp.get_json()
        assert body["advisory_count"] == 0
        assert body["advisories"] == []


def test_check_freshness_advises_empty_tier_bot(app):
    """Bug 2 reproduction: team_bot_c has no tiers configured. Pre-fix, the freshness
    check iterated over bot_tiers.items() and silently produced no advisories
    for team_bot_c at all — leaving the user with no way to bootstrap empty tiers
    via the Update button. Post-fix, team_bot_c gets one advisory per (provider × tier)
    in RECOMMENDED for which it holds a key."""
    from model_registry import RECOMMENDED
    with app["app"].test_client() as c:
        resp = c.post("/api/models/check-freshness", json={})
        body = resp.get_json()
        team_bot_c_advisories = [a for a in body["advisories"] if a["bot_id"] == "team_bot_c"]
        # team_bot_c has the anthropic key only, so we expect one advisory per anthropic tier
        assert len(team_bot_c_advisories) == len(RECOMMENDED["anthropic"])
        team_bot_c_tiers = {a["tier"] for a in team_bot_c_advisories}
        assert team_bot_c_tiers == set(RECOMMENDED["anthropic"].keys())
        # All team_bot_c advisories show current=None since the tiers are empty
        assert all(a["current_model"] is None for a in team_bot_c_advisories)


# ── /api/models/update-tier (D2) ──────────────────────────────────────────────


def test_update_tier_replaces_same_provider_entry(app):
    from model_registry import RECOMMENDED
    rec = RECOMMENDED["anthropic"]["tier2"]["model"]

    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier", json={
            "bot_id": "team_bot_a",
            "tier": "tier2",
            "provider": "anthropic",
            "model": rec,
        })
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["ok"] is True
        # The anthropic entry was replaced; the openai entry preserved
        assert body["models"] == [rec, "openai/gpt-4o"]

        # Underlying state was updated
        assert app["bot_state"]["team_bot_a"]["tiers"]["tier2"]["models"] == [rec, "openai/gpt-4o"]


def test_update_tier_adds_new_model_to_catalog(app):
    """Bug 1 reproduction: team_bot_a's catalog has only sonnet-4-2 (not 4-6). When we
    update tier2 to the recommended sonnet-4-6, the new model must also land
    in agents.defaults.models — otherwise OC has no client config for it and
    silently falls back to whatever IS in the catalog."""
    from model_registry import RECOMMENDED
    rec = RECOMMENDED["anthropic"]["tier2"]["model"]
    assert rec not in app["bot_state"]["team_bot_a"]["catalog"]  # precondition

    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier", json={
            "bot_id": "team_bot_a",
            "tier": "tier2",
            "provider": "anthropic",
            "model": rec,
        })
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["catalog_added"] is True
        assert rec in body["catalog"]
        # State was actually updated
        assert rec in app["bot_state"]["team_bot_a"]["catalog"]
        # Old model was NOT removed (other tiers may still reference it; safe as fallback)
        assert "anthropic/claude-sonnet-4-2" in app["bot_state"]["team_bot_a"]["catalog"]


def test_update_tier_does_not_double_add_to_catalog(app):
    """If the new model is already in the catalog, don't re-append it (no-op)."""
    from model_registry import RECOMMENDED
    rec = RECOMMENDED["anthropic"]["tier3"]["model"]
    # tier3 already has the recommendation; catalog already includes it
    assert rec in app["bot_state"]["team_bot_a"]["catalog"]
    catalog_before = list(app["bot_state"]["team_bot_a"]["catalog"])

    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier", json={
            "bot_id": "team_bot_a",
            "tier": "tier3",
            "provider": "anthropic",
            "model": rec,
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["catalog_added"] is False
        assert app["bot_state"]["team_bot_a"]["catalog"] == catalog_before


def test_update_tier_bootstraps_empty_tier(app):
    """End-to-end Bug 2 + Bug 1: team_bot_c has no tiers and no anthropic models in catalog.
    After update, team_bot_c's tier1 should be set AND the model should be in the catalog."""
    from model_registry import RECOMMENDED
    rec = RECOMMENDED["anthropic"]["tier1"]["model"]
    assert app["bot_state"]["team_bot_c"]["tiers"] == {}
    assert rec not in app["bot_state"]["team_bot_c"]["catalog"]

    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier", json={
            "bot_id": "team_bot_c",
            "tier": "tier1",
            "provider": "anthropic",
            "model": rec,
        })
        assert resp.status_code == 200, resp.get_json()
        # Tier1 created with the new model
        assert app["bot_state"]["team_bot_c"]["tiers"]["tier1"]["models"] == [rec]
        # Catalog now includes it
        assert rec in app["bot_state"]["team_bot_c"]["catalog"]


def test_update_tier_appends_when_no_same_provider_entry(app):
    from model_registry import RECOMMENDED
    rec = RECOMMENDED["openai"]["tier3"]["model"]

    # tier3 currently only has anthropic; updating openai/tier3 should APPEND
    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier", json={
            "bot_id": "team_bot_a",
            "tier": "tier3",
            "provider": "openai",
            "model": rec,
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert "anthropic/claude-haiku-4-5" in body["models"]
        assert rec in body["models"]


def test_update_tier_rejects_arbitrary_model(app):
    """The endpoint should refuse to write a model that isn't the current recommendation."""
    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier", json={
            "bot_id": "team_bot_a",
            "tier": "tier2",
            "provider": "anthropic",
            "model": "anthropic/claude-sonnet-NOT-A-REAL-MODEL",
        })
        assert resp.status_code == 400


def test_update_tier_400_when_missing_fields(app):
    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier", json={"bot_id": "team_bot_a"})
        assert resp.status_code == 400


def test_freshness_advisory_disappears_after_update(app):
    """End-to-end: check shows team_bot_a/anthropic/tier2 stale; after update, re-check shows it gone."""
    from model_registry import RECOMMENDED
    rec = RECOMMENDED["anthropic"]["tier2"]["model"]

    with app["app"].test_client() as c:
        first = c.post("/api/models/check-freshness", json={}).get_json()
        team_bot_a_t2_before = [
            a for a in first["advisories"]
            if a["bot_id"] == "team_bot_a" and a["tier"] == "tier2" and a["provider"] == "anthropic"
        ]
        assert len(team_bot_a_t2_before) == 1

        c.post("/api/models/update-tier", json={
            "bot_id": "team_bot_a", "tier": "tier2",
            "provider": "anthropic", "model": rec,
        })

        second = c.post("/api/models/check-freshness", json={}).get_json()
        team_bot_a_t2_after = [
            a for a in second["advisories"]
            if a["bot_id"] == "team_bot_a" and a["tier"] == "tier2" and a["provider"] == "anthropic"
        ]
        assert team_bot_a_t2_after == []


# ── Provider diversity advisories ─────────────────────────────────────────────


def test_check_freshness_includes_diversity_for_single_provider_bots(app):
    """admin_bot and team_bot_c have only anthropic; both should get a diversity
    advisory. team_bot_a has anthropic + openai → no advisory."""
    with app["app"].test_client() as c:
        body = c.post("/api/models/check-freshness", json={}).get_json()
        adv_by_bot = {a["bot_id"]: a for a in (body.get("diversity_advisories") or [])}
        assert "team_bot_a" not in adv_by_bot
        assert "admin_bot" in adv_by_bot
        assert "team_bot_c" in adv_by_bot
        adv = adv_by_bot["admin_bot"]
        assert adv["current_providers"] == ["anthropic"]
        assert "anthropic" not in adv["suggested_providers"]
        # Every suggested provider must have a borrow_candidates entry, even
        # when empty — the UI iterates the dict directly.
        for prov in adv["suggested_providers"]:
            assert prov in adv["borrow_candidates"]
            assert isinstance(adv["borrow_candidates"][prov], list)
        assert "fallback" in adv["reasons"]
        assert "judge" in adv["reasons"]
        # Diversity count tracks the filtered list (no dismissals yet).
        assert body["diversity_count"] == len(adv_by_bot)


def test_check_freshness_no_diversity_when_bot_has_two_providers(app):
    """team_bot_a has anthropic + openai; no advisory."""
    with app["app"].test_client() as c:
        body = c.post("/api/models/check-freshness", json={}).get_json()
        diversity = body.get("diversity_advisories") or []
        bots = [a["bot_id"] for a in diversity]
        assert "team_bot_a" not in bots


def test_dismiss_diversity_advisory_hides_it(app):
    """After POST /freshness-advisory/dismiss for admin_bot, the next check no
    longer includes admin_bot in diversity_advisories."""
    with app["app"].test_client() as c:
        before = c.post("/api/models/check-freshness", json={}).get_json()
        bots_before = {a["bot_id"] for a in (before.get("diversity_advisories") or [])}
        assert "admin_bot" in bots_before

        r = c.post("/api/models/freshness-advisory/dismiss", json={
            "type": "diversity", "key": "admin_bot",
        })
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["ok"] is True

        # Re-running the check must not surface admin_bot's diversity advisory.
        after = c.post("/api/models/check-freshness", json={}).get_json()
        bots_after = {a["bot_id"] for a in (after.get("diversity_advisories") or [])}
        assert "admin_bot" not in bots_after
        # Other single-provider bots still surface.
        assert "team_bot_c" in bots_after
        # The dismissed bot is reported separately so the UI can show a reset link.
        assert "admin_bot" in after["diversity_dismissed_bots"]


def test_reset_diversity_dismissals_brings_them_back(app):
    """POST /freshness-advisory/reset clears all dismissals."""
    with app["app"].test_client() as c:
        c.post("/api/models/check-freshness", json={})
        c.post("/api/models/freshness-advisory/dismiss", json={
            "type": "diversity", "key": "admin_bot",
        })

        r = c.post("/api/models/freshness-advisory/reset", json={"type": "diversity"})
        assert r.status_code == 200
        assert r.get_json()["dismissals"].get("diversity", {}) == {}

        after = c.post("/api/models/check-freshness", json={}).get_json()
        bots_after = {a["bot_id"] for a in (after.get("diversity_advisories") or [])}
        assert "admin_bot" in bots_after


def test_dismiss_endpoint_rejects_unknown_type(app):
    with app["app"].test_client() as c:
        r = c.post("/api/models/freshness-advisory/dismiss", json={
            "type": "not_a_real_type", "key": "admin_bot",
        })
        assert r.status_code == 400


def test_dismiss_endpoint_requires_key(app):
    with app["app"].test_client() as c:
        r = c.post("/api/models/freshness-advisory/dismiss", json={
            "type": "diversity",
        })
        assert r.status_code == 400


# ── Borrow candidates (read-only) ─────────────────────────────────────────────


def test_borrow_candidates_requires_provider(app):
    with app["app"].test_client() as c:
        r = c.get("/api/admin/keys/borrow-candidates")
        assert r.status_code == 400


def test_borrow_candidates_returns_provider_key_and_empty_list_when_no_files(app):
    """Without on-disk auth-profiles files in the test fixture, no candidates
    are surfaced — but the endpoint should still respond 200 with the shape
    the frontend dropdown expects.
    """
    with app["app"].test_client() as c:
        r = c.get("/api/admin/keys/borrow-candidates?provider=anthropic")
        assert r.status_code == 200
        body = r.get_json()
        assert body["provider"] == "anthropic"
        assert isinstance(body["candidates"], list)


def test_borrow_endpoint_rejects_self_borrow(app):
    with app["app"].test_client() as c:
        r = c.post(
            "/api/admin/keys/admin_bot/anthropic/borrow",
            json={"from_bot": "admin_bot"},
        )
        assert r.status_code == 400


def test_borrow_endpoint_rejects_unknown_source(app):
    with app["app"].test_client() as c:
        r = c.post(
            "/api/admin/keys/admin_bot/anthropic/borrow",
            json={"from_bot": "no_such_bot"},
        )
        assert r.status_code == 400


def test_borrow_endpoint_requires_from_bot(app):
    with app["app"].test_client() as c:
        r = c.post(
            "/api/admin/keys/admin_bot/anthropic/borrow",
            json={},
        )
        assert r.status_code == 400


# ── /api/models/update-tier-bulk (Apply All) ──────────────────────────────────
#
# The bulk endpoint exists so the UI's "Apply All" button can dispatch a
# 50-advisory batch as a single round-trip instead of 50 sequential
# /update-tier calls. The single-shot endpoint shells `sudo -u {user}
# python3 oc_model.py` twice per call (read + write); batching is the
# difference between ~30s and ~2min for a full refresh.


def test_update_tier_bulk_applies_all_updates(app):
    """Two updates across two bots → both land + response shape sane."""
    from model_registry import RECOMMENDED
    sonnet = RECOMMENDED["anthropic"]["tier2"]["model"]
    # team_bot_a needs the sonnet-4-2 → sonnet-4-6 fix
    # team_bot_c is empty — bootstrap tier1 (mirrors the per-row test)
    opus = RECOMMENDED["anthropic"]["tier1"]["model"]

    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier-bulk", json={
            "updates": [
                {"bot_id": "team_bot_a", "tier": "tier2",
                 "provider": "anthropic", "model": sonnet},
                {"bot_id": "team_bot_c", "tier": "tier1",
                 "provider": "anthropic", "model": opus},
            ],
        })
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["ok"] is True
        assert body["applied"] == 2
        assert body["failed"] == 0
        assert body["bots_touched"] == 2
        assert len(body["results"]) == 2
        assert all(r["success"] for r in body["results"])

        # Underlying state actually changed
        assert app["bot_state"]["team_bot_a"]["tiers"]["tier2"]["models"][0] == sonnet
        assert app["bot_state"]["team_bot_c"]["tiers"]["tier1"]["models"] == [opus]


def test_update_tier_bulk_groups_writes_by_bot(app, monkeypatch):
    """The whole point of this endpoint: per-bot batching. Three updates
    that target team_bot_a should result in ONE oc_full_config_set call
    for team_bot_a, not three. We instrument the fake to count calls."""
    from model_registry import RECOMMENDED
    import oc_cli

    # Make team_bot_a need 3 separate updates by deflating its tiers
    app["bot_state"]["team_bot_a"]["tiers"] = {
        "tier1": {"models": ["anthropic/claude-opus-3-stale"]},
        "tier2": {"models": ["anthropic/claude-sonnet-4-2"]},
        "tier3": {"models": ["anthropic/claude-haiku-3-stale"]},
    }

    call_counts = {"get": 0, "set": 0}
    original_get = oc_cli.oc_full_config_get
    original_set = oc_cli.oc_full_config_set_with_error

    def counting_get(bot_id, network_path=None):
        call_counts["get"] += 1
        return original_get(bot_id, network_path)

    def counting_set(bot_id, updates, network_path=None):
        call_counts["set"] += 1
        return original_set(bot_id, updates, network_path)

    monkeypatch.setattr(oc_cli, "oc_full_config_get", counting_get)
    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", counting_set)

    updates = [
        {"bot_id": "team_bot_a", "tier": tier, "provider": "anthropic",
         "model": RECOMMENDED["anthropic"][tier]["model"]}
        for tier in ("tier1", "tier2", "tier3")
    ]
    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier-bulk", json={"updates": updates})
        assert resp.status_code == 200
        assert resp.get_json()["applied"] == 3

    # 3 updates, 1 bot → 1 get + 1 set, NOT 3 each
    assert call_counts["get"] == 1, f"Expected 1 get call, saw {call_counts['get']}"
    assert call_counts["set"] == 1, f"Expected 1 set call, saw {call_counts['set']}"


def test_update_tier_bulk_rejects_arbitrary_model(app):
    """One bad entry rejects the whole batch — partial validation would
    leave callers guessing which entries got written. Matches the
    single-shot endpoint's contract."""
    from model_registry import RECOMMENDED
    sonnet = RECOMMENDED["anthropic"]["tier2"]["model"]
    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier-bulk", json={
            "updates": [
                {"bot_id": "team_bot_a", "tier": "tier2",
                 "provider": "anthropic", "model": sonnet},
                {"bot_id": "team_bot_a", "tier": "tier1",
                 "provider": "anthropic", "model": "anthropic/fake-not-real"},
            ],
        })
        assert resp.status_code == 400
        # First update must NOT have applied — whole batch rejected
        assert app["bot_state"]["team_bot_a"]["tiers"]["tier2"]["models"][0] != sonnet


def test_update_tier_bulk_per_bot_failure_isolated(app, monkeypatch):
    """If reading bot A's config fails, bot B's updates should still
    apply — the failure is bounded to the affected bot's results."""
    from model_registry import RECOMMENDED
    import oc_cli

    original_get = oc_cli.oc_full_config_get

    def picky_get(bot_id, network_path=None):
        if bot_id == "team_bot_a":
            return None  # simulate config read failure
        return original_get(bot_id, network_path)

    monkeypatch.setattr(oc_cli, "oc_full_config_get", picky_get)

    sonnet = RECOMMENDED["anthropic"]["tier2"]["model"]
    opus = RECOMMENDED["anthropic"]["tier1"]["model"]
    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier-bulk", json={
            "updates": [
                {"bot_id": "team_bot_a", "tier": "tier2",
                 "provider": "anthropic", "model": sonnet},
                {"bot_id": "team_bot_c", "tier": "tier1",
                 "provider": "anthropic", "model": opus},
            ],
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is False  # at least one failure
        assert body["applied"] == 1
        assert body["failed"] == 1
        # team_bot_c's update DID land
        assert app["bot_state"]["team_bot_c"]["tiers"]["tier1"]["models"] == [opus]
        # team_bot_a result row has the read failure
        team_a = [r for r in body["results"] if r["bot_id"] == "team_bot_a"]
        assert len(team_a) == 1
        assert team_a[0]["success"] is False
        assert "could not read" in team_a[0]["error"].lower()


def test_update_tier_bulk_400_on_empty_list(app):
    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier-bulk", json={"updates": []})
        assert resp.status_code == 400


def test_update_tier_bulk_400_on_missing_updates_key(app):
    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier-bulk", json={})
        assert resp.status_code == 400


def test_update_tier_bulk_accepts_oc_resolved_model_not_in_RECOMMENDED(app, monkeypatch):
    """Regression: 2026-06-07 incident. The freshness check uses
    resolve_current_model() — which consults the OC live alias map first
    and the static RECOMMENDED dict second. When OC's signals advance
    (e.g. "claude-opus-4-7" → "claude-opus-4-8") the static dict goes
    stale by one release, and the check correctly returns the new model.
    The original validator only checked RECOMMENDED, rejecting the
    operator-clicks-Update path with "not the current recommendation"
    even though the UI had just shown the row as recommended.

    Fix: validation must call the SAME resolver as the check. This test
    pins that by monkey-patching the resolver to return a model that
    isn't in RECOMMENDED, then asserting the writer accepts it.
    """
    import model_registry

    def fake_resolve(provider, tier, **kw):
        if provider == "anthropic" and tier == "tier1":
            # Pretend OC's alias map points at the next-release model.
            # Not in our static RECOMMENDED — would have been rejected pre-fix.
            return ("anthropic/claude-opus-4-8-next", "oc_alias", "v2026.6.x", "")
        # Fall through to whatever the real resolver returns
        return _real_resolve(provider, tier, **kw)

    _real_resolve = model_registry.resolve_current_model
    monkeypatch.setattr(model_registry, "resolve_current_model", fake_resolve)

    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier-bulk", json={
            "updates": [
                {"bot_id": "team_bot_a", "tier": "tier1",
                 "provider": "anthropic",
                 "model": "anthropic/claude-opus-4-8-next"},
            ],
        })
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["applied"] == 1
        # Underlying state actually has the new model
        assert "anthropic/claude-opus-4-8-next" in (
            app["bot_state"]["team_bot_a"]["tiers"]["tier1"]["models"]
        )


def test_update_tier_accepts_oc_resolved_model_not_in_RECOMMENDED(app, monkeypatch):
    """Single-shot endpoint mirror of the bulk regression above. The
    pre-fix /update-tier had the same latent bug — it just hadn't
    tripped until OC's alias map advanced past the static fallback."""
    import model_registry

    def fake_resolve(provider, tier, **kw):
        if provider == "anthropic" and tier == "tier1":
            return ("anthropic/claude-opus-4-8-next", "oc_alias", "v2026.6.x", "")
        return _real_resolve(provider, tier, **kw)

    _real_resolve = model_registry.resolve_current_model
    monkeypatch.setattr(model_registry, "resolve_current_model", fake_resolve)

    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier", json={
            "bot_id": "team_bot_a", "tier": "tier1",
            "provider": "anthropic",
            "model": "anthropic/claude-opus-4-8-next",
        })
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["ok"] is True


def test_update_tier_bulk_adds_to_catalog_once_per_bot(app):
    """Catalog accumulator runs ONCE per bot (not once per advisory):
    if team_bot_a gets 3 model-additions, the post-write catalog
    should have all 3, deduped against the pre-existing entries."""
    from model_registry import RECOMMENDED
    # Force team_bot_a to need new entries in 3 tiers
    app["bot_state"]["team_bot_a"]["tiers"] = {
        "tier1": {"models": ["anthropic/claude-opus-3-stale"]},
        "tier2": {"models": ["anthropic/claude-sonnet-4-2"]},
        "tier3": {"models": ["anthropic/claude-haiku-3-stale"]},
    }
    app["bot_state"]["team_bot_a"]["catalog"] = ["anthropic/claude-sonnet-4-2"]

    new_models = [RECOMMENDED["anthropic"][t]["model"]
                  for t in ("tier1", "tier2", "tier3")]
    updates = [
        {"bot_id": "team_bot_a", "tier": tier, "provider": "anthropic",
         "model": m}
        for tier, m in zip(("tier1", "tier2", "tier3"), new_models)
    ]
    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier-bulk", json={"updates": updates})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["catalog_added_count"] == 3
        for m in new_models:
            assert m in app["bot_state"]["team_bot_a"]["catalog"]


# ── False-success / non-persisting write (model-tiers bug, 2026-06-27) ────────
#
# Live symptom: "Apply All" reported a green "Applied 1 update" toast, yet the
# freshness advisory re-fired after the auto re-check. Root cause: on the
# rungs/roles tier shape, several legacy tier keys collapse onto one rung —
# `standard`(tier2) and `judge`(tier0) both map to `sonnet-class`. The endpoints
# sent the FULL synthesized tiers dict; oc_model folds each legacy tier into its
# rung last-writer-wins, so the unchanged `judge` tier clobbered `standard`'s
# freshly-added google model. The subprocess exited 0, so the write "succeeded"
# while the model never landed. Two fixes, both pinned below: (1) send only the
# changed tiers; (2) verify the model landed post-write instead of trusting rc=0.


def _new_shape_store(monkeypatch, app, store):
    """Wire oc_cli get/set against a per-bot NEW-shape (rungs/roles) tier store,
    using the real oc_model fold/synthesize so the rung-collision is exercised
    for real. ``store`` maps bot_id → tiers_file dict (rungs+roles)."""
    import oc_cli
    import oc_model

    def shape_get(bot_id, network_path=None):
        tf = store.get(bot_id, {})
        return {
            "bot": bot_id,
            "tiers": oc_model.synthesize_legacy_tiers(tf),
            "catalog": ["anthropic/claude-sonnet-4-6"],
        }

    def shape_set_err(bot_id, updates, network_path=None):
        tf = store.setdefault(bot_id, {})
        if "tiers" in updates:
            # The genuine writer path: fold legacy tier updates into rungs.
            oc_model.apply_tiers_update_new_shape(tf, updates["tiers"])
        return (
            {
                "bot": bot_id,
                "tiers": oc_model.synthesize_legacy_tiers(tf),
                "catalog": updates.get("catalog", ["anthropic/claude-sonnet-4-6"]),
                "generatedFallbacks": [],
            },
            None,
        )

    monkeypatch.setattr(oc_cli, "oc_full_config_get", shape_get)
    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", shape_set_err)
    monkeypatch.setattr(
        oc_cli, "oc_keys_get",
        lambda bot_id, network_path=None: {
            "keys": {"anthropic": {"api_key": True}, "google": {"api_key": True}},
            "source": "sqlite",
        },
    )


def _rung_models(store, bot_id, rung_id):
    for r in store[bot_id]["rungs"]:
        if r["id"] == rung_id:
            return r["models"]
    return []


def test_update_tier_bulk_standard_survives_judge_rung_collision(app, monkeypatch):
    """The live bug, end-to-end against the real rung fold: a google update to
    `standard`(tier2) must land in `sonnet-class` even though `judge`(tier0)
    shares that rung. Pre-fix the full-dict write let the unchanged judge tier
    clobber it; the model never reached the rung despite a success response."""
    import model_registry

    store = {
        "team_bot_a": {
            "rungs": [
                {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
                {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"},
                {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
            ],
            "roles": {
                "fast": "haiku-class",
                "standard": "sonnet-class",
                "power": "opus-class",
                "judge": {"rung": "sonnet-class", "provider": "not-standard"},
            },
        },
    }
    _new_shape_store(monkeypatch, app, store)

    # The resolver must name the google model as the recommendation for
    # (google, tier2) so validation + the advisory agree.
    _real = model_registry.resolve_current_model

    def fake_resolve(provider, tier, **kw):
        if provider == "google" and tier == "tier2":
            return ("google/gemini-3.1-pro-preview", "oc_alias", "v2026.6", "")
        return _real(provider, tier, **kw)

    monkeypatch.setattr(model_registry, "resolve_current_model", fake_resolve)

    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier-bulk", json={"updates": [
            {"bot_id": "team_bot_a", "tier": "tier2",
             "provider": "google", "model": "google/gemini-3.1-pro-preview"},
        ]})
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["applied"] == 1, body
        assert body["failed"] == 0, body

    # The decisive assertion: google actually reached the shared sonnet-class
    # rung. Pre-fix the judge tier clobbered it back to anthropic-only.
    sonnet = _rung_models(store, "team_bot_a", "sonnet-class")
    assert "google/gemini-3.1-pro-preview" in sonnet, sonnet
    assert "anthropic/claude-sonnet-4-6" in sonnet, sonnet


def test_update_tier_sends_only_changed_tier(app, monkeypatch):
    """The single endpoint must send ONLY the tier it changed — never the full
    synthesized tiers dict (which is what enabled the sibling-rung clobber)."""
    import oc_cli
    from model_registry import RECOMMENDED
    rec = RECOMMENDED["anthropic"]["tier2"]["model"]

    captured: dict = {}

    def capturing(bot_id, updates, network_path=None):
        captured["tiers"] = updates.get("tiers")
        return oc_cli.oc_full_config_set(bot_id, updates, network_path), None

    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", capturing)

    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier", json={
            "bot_id": "team_bot_a", "tier": "tier2",
            "provider": "anthropic", "model": rec,
        })
        assert resp.status_code == 200, resp.get_json()

    assert set((captured["tiers"] or {}).keys()) == {"tier2"}, captured["tiers"]


def test_update_tier_bulk_sends_only_changed_tiers(app, monkeypatch):
    """Bulk mirror: a one-tier batch sends just that tier, not all four."""
    import oc_cli
    from model_registry import RECOMMENDED
    sonnet = RECOMMENDED["anthropic"]["tier2"]["model"]

    captured: dict = {}

    def capturing(bot_id, updates, network_path=None):
        captured[bot_id] = updates.get("tiers")
        return oc_cli.oc_full_config_set(bot_id, updates, network_path), None

    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", capturing)

    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier-bulk", json={"updates": [
            {"bot_id": "team_bot_a", "tier": "tier2",
             "provider": "anthropic", "model": sonnet},
        ]})
        assert resp.status_code == 200, resp.get_json()

    assert set((captured.get("team_bot_a") or {}).keys()) == {"tier2"}, captured


def test_update_tier_reports_failure_when_write_does_not_persist(app, monkeypatch):
    """Truthfulness (single): the setter returns a truthy result but the tier
    still lacks the model (silent non-persist — the live clobber symptom, or a
    perms/schema reject that exits 0). The endpoint must 500 with a real
    'did not persist' error, NOT a false 200/ok."""
    import oc_cli
    from model_registry import RECOMMENDED
    sonnet = RECOMMENDED["anthropic"]["tier2"]["model"]

    def nonpersist(bot_id, updates, network_path=None):
        # Echo the bot's UNCHANGED tiers — the write didn't take.
        return (
            {"bot": bot_id, "tiers": app["bot_state"][bot_id]["tiers"],
             "catalog": app["bot_state"][bot_id]["catalog"]},
            None,
        )

    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", nonpersist)

    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier", json={
            "bot_id": "team_bot_a", "tier": "tier2",
            "provider": "anthropic", "model": sonnet,
        })
        assert resp.status_code == 500, resp.get_json()
        assert "did not persist" in resp.get_json()["error"]


def test_update_tier_bulk_reports_failure_when_write_does_not_persist(app, monkeypatch):
    """Truthfulness (bulk): a non-persisting write surfaces as failed>0 / a
    failed result row, not a green 'Applied N' summary."""
    import oc_cli
    from model_registry import RECOMMENDED
    sonnet = RECOMMENDED["anthropic"]["tier2"]["model"]

    def nonpersist(bot_id, updates, network_path=None):
        return (
            {"bot": bot_id, "tiers": app["bot_state"][bot_id]["tiers"],
             "catalog": app["bot_state"][bot_id]["catalog"], "generatedFallbacks": []},
            None,
        )

    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", nonpersist)

    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier-bulk", json={"updates": [
            {"bot_id": "team_bot_a", "tier": "tier2",
             "provider": "anthropic", "model": sonnet},
        ]})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is False, body
        assert body["applied"] == 0, body
        assert body["failed"] == 1, body
        assert body["results"][0]["success"] is False
        assert "did not persist" in body["results"][0]["error"]


def test_update_tier_bulk_surfaces_real_setter_error(app, monkeypatch):
    """When the setter itself fails (perms denied / subprocess error), the real
    message is surfaced on the failed rows instead of a generic 'write failed' —
    the operator can see WHY (e.g. a real permission-denied or subprocess
    timeout on a bot's tier file) instead of a blind 'check server logs'."""
    import oc_cli
    from model_registry import RECOMMENDED
    sonnet = RECOMMENDED["anthropic"]["tier2"]["model"]

    def erroring(bot_id, updates, network_path=None):
        return None, "[Errno 13] Permission denied: '/Users/x/.openclaw/evolve-tiers.json'"

    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", erroring)

    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier-bulk", json={"updates": [
            {"bot_id": "team_bot_a", "tier": "tier2",
             "provider": "anthropic", "model": sonnet},
        ]})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["failed"] == 1
        assert "Permission denied" in body["results"][0]["error"]


# ── Loud-fail guard: credential_store_unreadable advisory ─────────────────────


def test_check_freshness_loud_advisory_when_no_readable_store(app, monkeypatch):
    """When the presence reader returns source="none" for EVERY bot (no readable
    OpenClaw credential store at all — e.g. the store schema moved), the check
    surfaces a critical ``credential_store_unreadable`` advisory rather than a
    silent ∅ that masquerades as "no credentialed providers"."""
    import oc_cli

    def all_none(bot_id, network_path=None):
        return {"bot": bot_id, "keys": {}, "source": "none",
                "error": "no readable OpenClaw credential store (schema may have changed)"}

    monkeypatch.setattr(oc_cli, "oc_keys_get", all_none)

    with app["app"].test_client() as c:
        body = c.post("/api/models/check-freshness", json={}).get_json()

    adv = body.get("credential_store_unreadable")
    assert adv is not None
    assert adv["severity"] == "critical"  # all three bots blind
    assert set(adv["bots"]) == {"team_bot_a", "admin_bot", "team_bot_c"}
    assert "credential store" in adv["message"].lower()


def test_check_freshness_single_unreadable_bot_is_warning(app, monkeypatch):
    """A single unreadable bot (others fine) is a warning, not critical."""
    import oc_cli

    def one_none(bot_id, network_path=None):
        if bot_id == "team_bot_c":
            return {"bot": bot_id, "keys": {}, "source": "none", "error": "x"}
        return {"bot": bot_id, "keys": {"anthropic": {"api_key": True}}, "source": "sqlite"}

    monkeypatch.setattr(oc_cli, "oc_keys_get", one_none)

    with app["app"].test_client() as c:
        body = c.post("/api/models/check-freshness", json={}).get_json()

    adv = body.get("credential_store_unreadable")
    assert adv is not None
    assert adv["severity"] == "warning"
    assert adv["bots"] == ["team_bot_c"]


def test_check_freshness_no_advisory_when_store_readable(app, monkeypatch):
    """A readable store (source != "none") with real providers yields NO
    loud advisory, and credentialed providers flow through normally."""
    import oc_cli

    def all_sqlite(bot_id, network_path=None):
        return {"bot": bot_id, "keys": {"anthropic": {"api_key": True}}, "source": "sqlite"}

    monkeypatch.setattr(oc_cli, "oc_keys_get", all_sqlite)

    with app["app"].test_client() as c:
        body = c.post("/api/models/check-freshness", json={}).get_json()

    assert body.get("credential_store_unreadable") is None
    assert "anthropic" in (body.get("providers_checked") or [])


# ── model-swap ledger (design-model-swap-behavior-guard-2026-08-19) ───────────
#
# Both tier-write endpoints record what the rung held BEFORE the write. That
# record is the operator's one-command undo AND the input model_swap_watch
# needs to know when each bot's behavior boundary is. The 2026-08-14 incident
# had neither: the write verified the model STRING landed and stopped there.
#
# These exercise the real ledger writer — no monkeypatch on record_swap — so a
# lazy-import that silently resolves to nothing would fail here rather than
# passing on a mocked seam.


def _ledger_rows(app):
    from model_swap_ledger import read_swaps

    return read_swaps(app["shared"])


def test_update_tier_records_the_previous_model(app):
    from model_registry import RECOMMENDED
    rec = RECOMMENDED["anthropic"]["tier2"]["model"]
    before = app["bot_state"]["team_bot_a"]["tiers"]["tier2"]["models"][:]

    with app["app"].test_client() as c:
        assert c.post("/api/models/update-tier", json={
            "bot_id": "team_bot_a", "tier": "tier2",
            "provider": "anthropic", "model": rec,
        }).status_code == 200

    rows = _ledger_rows(app)
    assert len(rows) == 1
    assert rows[0]["bot_id"] == "team_bot_a"
    assert rows[0]["tier"] == "tier2"
    assert rows[0]["source"] == "admin_ui_single"
    # The PRE-write models — captured before staging, not read back after.
    assert rows[0]["previous_models"] == before
    assert rec in rows[0]["new_models"]
    assert rec not in rows[0]["previous_models"]


def test_update_tier_bulk_records_each_bot_and_tier(app):
    from model_registry import RECOMMENDED
    rec_t2 = RECOMMENDED["anthropic"]["tier2"]["model"]
    before = app["bot_state"]["team_bot_a"]["tiers"]["tier2"]["models"][:]

    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier-bulk", json={"updates": [
            {"bot_id": "team_bot_a", "tier": "tier2",
             "provider": "anthropic", "model": rec_t2},
        ]})
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["applied"] == 1

    rows = _ledger_rows(app)
    assert [(r["bot_id"], r["tier"], r["source"]) for r in rows] == [
        ("team_bot_a", "tier2", "admin_ui_bulk")
    ]
    assert rows[0]["previous_models"] == before


def test_reapplying_the_same_model_records_nothing(app):
    """A no-op write is not a behavioral boundary — recording it would hand
    model_swap_watch a change instant at which nothing changed."""
    from model_registry import RECOMMENDED
    rec = RECOMMENDED["anthropic"]["tier2"]["model"]

    with app["app"].test_client() as c:
        c.post("/api/models/update-tier", json={
            "bot_id": "team_bot_a", "tier": "tier2",
            "provider": "anthropic", "model": rec,
        })
        c.post("/api/models/update-tier", json={
            "bot_id": "team_bot_a", "tier": "tier2",
            "provider": "anthropic", "model": rec,
        })

    assert len(_ledger_rows(app)) == 1, "the second, identical write is a no-op"


def test_rejected_write_records_no_swap(app):
    """A 400 never touched the config; the ledger must not claim it did."""
    with app["app"].test_client() as c:
        assert c.post("/api/models/update-tier", json={
            "bot_id": "team_bot_a", "tier": "tier2",
            "provider": "anthropic", "model": "anthropic/not-a-real-model",
        }).status_code == 400

    assert _ledger_rows(app) == []


def test_ledger_write_failure_does_not_fail_the_apply(app, monkeypatch):
    """The config write already succeeded. Reporting it as a failure because
    a log line didn't land would be strictly worse than losing the line."""
    import model_swap_ledger

    monkeypatch.setattr(
        model_swap_ledger, "record_swap",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    from model_registry import RECOMMENDED
    rec = RECOMMENDED["anthropic"]["tier2"]["model"]

    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier", json={
            "bot_id": "team_bot_a", "tier": "tier2",
            "provider": "anthropic", "model": rec,
        })
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
    assert app["bot_state"]["team_bot_a"]["tiers"]["tier2"]["models"][0] == rec
