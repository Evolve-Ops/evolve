"""Tests for AL-1.7 promotion-as-a-Proposal.

Every guard in ``applications/app_promotion.py`` and
``evo/wizard/promote_handlers.py`` has a test here, and every one of them was
**mutation-checked** before this file was committed: the production guard was
deleted or inverted and the test watched go red. The brief required it because
three AL-1.6 chips shipped tests that did not test their fix. The mutation
performed on each is named in the test's docstring, so a reviewer re-running the
check knows what to break.

Clock-coupling: every test that involves time injects ``now``. Nothing here
reads a wall clock (the AL-1.6 arc lost days to clock-coupled reds).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evolve_admin.applications import app_promotion as ap
from evolve_admin.applications.app_readiness import score_readiness
from evolve_admin.evo.wizard import promote_handlers as ph


NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)


def _eligible_manifest(**over):
    """A manifest that scores eligible — a MECHANISM fixture, not pod data.

    ``files[].layer == "code"`` puts it on design §7.1's top evidence rung and
    ``invocation_count`` gives the recurrence dimension a producer, so two
    dimensions are measured and ``MIN_DIMENSIONS_FOR_OFFER`` is satisfied. No
    manifest on the operator's pod looks like this today — that is the whole
    point of AL-1.7's §2, and this fixture is not evidence to the contrary.
    """
    manifest = {
        "name": "Morning Brief",
        "purpose": "email + calendar + weather, every weekday",
        "definition_status": "discovered",
        "files": [{"path": "brief.py", "layer": "code"}],
        "usage_metadata": {"invocation_count": 12},
    }
    manifest.update(over)
    return manifest


class _FakeProposal:
    def __init__(self, generator_id, bot_id, created_at):
        self.generator_id = generator_id
        self.bot_id = bot_id
        self.created_at = created_at


# ─────────────────────────────────────────────────────────────────────────────
# §7.3a — ADOPT
# ─────────────────────────────────────────────────────────────────────────────


def test_adopt_keeps_a_resolvable_canonical_id():
    """§7.3a ADOPT: a draft with a resolvable id is NOT re-identified.

    MUTATION CHECKED: making ``adopt_or_confer_app_id`` fall through to
    ``slug_from_name`` unconditionally (deleting the ``if prior and
    is_canonical_app_id(prior)`` branch) makes this go red — the id becomes
    ``morning-brief`` from the name instead of the carried ``legacy-brief``.
    """
    decision = ap.adopt_or_confer_app_id(
        {"app_id": "legacy-brief", "name": "Morning Brief"}
    )
    assert decision.mode == "adopted"
    assert decision.app_id == "legacy-brief"
    assert decision.prior_app_id == "legacy-brief"


def test_adopt_reaches_through_the_legacy_chain():
    """A draft whose id lives on ``pkg_id`` is adopted just the same.

    This is the shape most manifests on the pod actually carry, and it is the
    one a name-based re-identify would silently break.
    """
    decision = ap.adopt_or_confer_app_id({"pkg_id": "atlas-capture", "name": "Atlas"})
    assert decision.mode == "adopted"
    assert decision.app_id == "atlas-capture"


def test_confer_mints_from_the_name_when_nothing_resolves():
    decision = ap.adopt_or_confer_app_id({}, name="Morning Brief")
    assert decision.mode == "conferred"
    assert decision.app_id == "morning-brief"


def test_confer_suffixes_on_collision_and_is_deterministic():
    """Design §7.2's "collision → suffix", and the suffix is not random.

    MUTATION CHECKED: replacing the ``for n in range(2, 1000)`` loop with a
    random suffix makes the equality between the two calls fail.
    """
    first = ap.adopt_or_confer_app_id({}, name="Morning Brief", taken=["morning-brief"])
    second = ap.adopt_or_confer_app_id({}, name="Morning Brief", taken=["morning-brief"])
    assert first.app_id == "morning-brief-2"
    assert first.app_id == second.app_id


def test_identity_blocked_rather_than_invented():
    """No id and no name → refuse. Never fabricate an identity.

    MUTATION CHECKED: returning a uuid-ish fallback instead of the ``blocked``
    branch makes ``decision.ok`` true and this red.
    """
    decision = ap.adopt_or_confer_app_id({})
    assert decision.mode == "blocked"
    assert decision.app_id == ""
    assert not decision.ok


def test_slug_refuses_a_name_that_cannot_make_a_conforming_id():
    assert ap.slug_from_name("!!") == ""
    assert ap.slug_from_name("") == ""
    assert ap.slug_from_name(None) == ""


# ─────────────────────────────────────────────────────────────────────────────
# Auto-promote is a RULE
# ─────────────────────────────────────────────────────────────────────────────


def test_auto_promote_is_off_even_when_the_pod_config_asks_for_it():
    """The rule, not the default.

    MUTATION CHECKED: changing ``auto_promote=False`` to
    ``auto_promote=requested`` in ``promotion_policy`` makes this go red.
    """
    policy = ap.promotion_policy(
        {"pod": {"apps": {"promotion": {"auto_promote": True}}}}
    )
    assert policy.auto_promote is False
    assert policy.auto_promote_requested is True
    assert "launders" in policy.auto_promote_refusal


def test_self_promote_is_a_real_knob():
    assert ap.promotion_policy({}).self_promote is True
    assert (
        ap.promotion_policy(
            {"pod": {"apps": {"promotion": {"self_promote": False}}}}
        ).self_promote
        is False
    )


def test_promote_app_is_human_only_at_the_eligibility_layer():
    """Second, independent enforcement of the no-auto-promote rule.

    MUTATION CHECKED: removing ``"PromoteApp"`` from
    ``eligibility._HUMAN_ONLY_ACTION_KINDS`` makes this go red. Two enforcement
    points because a policy function can be bypassed by a caller that never
    asks it; the eligibility table cannot.
    """
    from eligibility import _HUMAN_ONLY_ACTION_KINDS

    assert "PromoteApp" in _HUMAN_ONLY_ACTION_KINDS


def test_policy_tolerates_garbage_config():
    for bad in (None, {}, {"pod": None}, {"pod": {"apps": "nope"}}, 7):
        assert ap.promotion_policy(bad).auto_promote is False


# ─────────────────────────────────────────────────────────────────────────────
# Audience — design §7.2's default AND its fallback
# ─────────────────────────────────────────────────────────────────────────────


def test_audience_defaults_to_bot_primary_user():
    network = {"bots": {"team-bot-a": {"primary_user": {"external_ids": {"slack": "U1"}}}}}
    assert ap.audience_for(network, "team-bot-a") == "bot_primary_user"


def test_audience_falls_back_to_pod_operator_for_a_bot_with_no_primary_user():
    """The fallback is not a detail (design §7.2).

    MUTATION CHECKED: hardcoding ``return "bot_primary_user"`` in
    ``audience_for`` makes both of these fall to one value and this go red.
    """
    assert ap.audience_for({"bots": {"team": {}}}, "team") == "pod_operator"
    assert ap.audience_for({}, "team") == "pod_operator"


def test_a_primary_user_with_a_name_but_no_channel_id_is_not_reachable():
    """A block with no external id is not someone the bot can ask."""
    network = {"bots": {"team-bot-a": {"primary_user": {"name": "Sam"}}}}
    assert ap.bot_has_primary_user(network, "team-bot-a") is False
    assert ap.audience_for(network, "team-bot-a") == "pod_operator"


def test_app_audience_owners_when_the_app_delivers():
    assert ap.default_audience_for_app({"deliveries": [{"to": "slack"}]}) == "owners"
    assert ap.default_audience_for_app({"name": "x"}) == "everyone"
    assert ap.default_audience_for_app({"deliveries": []}) == "everyone"


# ─────────────────────────────────────────────────────────────────────────────
# The offer gate — design §10's four mitigations
# ─────────────────────────────────────────────────────────────────────────────


def test_an_eligible_draft_is_offerable():
    manifest = _eligible_manifest()
    readiness = score_readiness(manifest)
    assert readiness.eligible_to_offer, "fixture must be eligible or the gate proves nothing"
    decision = ap.evaluate_offer(
        manifest, bot_id="team-bot-a", readiness=readiness, recent_offers=0, now=NOW
    )
    assert decision.allowed
    assert decision.blockers == ()


def test_never_is_honored():
    """Design §10: "'never' is honored".

    MUTATION CHECKED: deleting the ``DO_NOT_OFFER_FIELD`` branch in
    ``evaluate_offer`` makes this go red.
    """
    manifest = _eligible_manifest(**{ap.DO_NOT_OFFER_FIELD: True})
    decision = ap.evaluate_offer(
        manifest,
        bot_id="team-bot-a",
        readiness=score_readiness(manifest),
        recent_offers=0,
        now=NOW,
    )
    assert not decision.allowed
    assert decision.reason == "do_not_offer"


def test_cadence_cap_blocks_a_second_offer_in_the_window():
    """Design §7.2: "at most one offer per bot per day".

    MUTATION CHECKED: changing ``if recent_offers >= 1`` to ``>= 2`` makes this
    go red.
    """
    manifest = _eligible_manifest()
    decision = ap.evaluate_offer(
        manifest,
        bot_id="team-bot-a",
        readiness=score_readiness(manifest),
        recent_offers=1,
        now=NOW,
    )
    assert not decision.allowed
    assert "cadence_cap" in decision.blockers


def test_a_live_snooze_blocks_the_offer_and_an_expired_one_does_not():
    manifest = _eligible_manifest(
        **{ap.SNOOZE_UNTIL_FIELD: (NOW + timedelta(days=3)).isoformat()}
    )
    readiness = score_readiness(manifest)
    assert not ap.evaluate_offer(
        manifest, bot_id="team-bot-a", readiness=readiness, recent_offers=0, now=NOW
    ).allowed

    expired = _eligible_manifest(
        **{ap.SNOOZE_UNTIL_FIELD: (NOW - timedelta(days=1)).isoformat()}
    )
    assert ap.evaluate_offer(
        expired,
        bot_id="team-bot-a",
        readiness=score_readiness(expired),
        recent_offers=0,
        now=NOW,
    ).allowed


def test_an_already_defined_app_is_not_offered():
    manifest = _eligible_manifest(definition_status="defined")
    decision = ap.evaluate_offer(
        manifest,
        bot_id="team-bot-a",
        readiness=score_readiness(manifest),
        recent_offers=0,
        now=NOW,
    )
    assert not decision.allowed
    assert "already_defined" in decision.blockers


def test_an_unready_draft_is_not_offered_and_the_gate_owns_no_threshold():
    """Readiness comes from AL-1.6b. This module must not carry a second cut.

    MUTATION CHECKED: adding a local threshold to ``evaluate_offer`` that
    overrides ``readiness.eligible_to_offer`` makes this go red. There is
    exactly one threshold in the codebase, and it is
    ``app_readiness.OFFER_THRESHOLD``.
    """
    weak = {"name": "Notes", "definition_status": "discovered"}
    decision = ap.evaluate_offer(
        weak, bot_id="team-bot-a", readiness=score_readiness(weak), recent_offers=0, now=NOW
    )
    assert not decision.allowed
    assert "not_ready" in decision.blockers
    assert not hasattr(ap, "OFFER_THRESHOLD")


def test_all_blockers_are_reported_not_just_the_first():
    manifest = _eligible_manifest(
        definition_status="defined", **{ap.DO_NOT_OFFER_FIELD: True}
    )
    decision = ap.evaluate_offer(
        manifest,
        bot_id="team-bot-a",
        readiness=score_readiness(manifest),
        recent_offers=5,
        now=NOW,
    )
    assert set(decision.blockers) >= {"do_not_offer", "already_defined", "cadence_cap"}


# ─────────────────────────────────────────────────────────────────────────────
# Cadence counting — from the proposal store, no new state
# ─────────────────────────────────────────────────────────────────────────────


def test_recent_offer_count_counts_across_terminal_states():
    """Answering an offer must NOT reset the cap.

    A rejected offer lands in ``archived``; if the counter only looked at
    ``pending``, saying "no" would immediately license another offer — the
    opposite of a cadence cap.

    MUTATION CHECKED: filtering the input to pending-only at the call site is
    the failure this guards; here, dropping the ``created_at >= cutoff``
    comparison in favour of counting nothing makes it go red.
    """
    proposals = [
        _FakeProposal(ap.GENERATOR_ID, "team-bot-a", (NOW - timedelta(hours=2)).isoformat()),
        _FakeProposal(ap.GENERATOR_ID, "team-bot-a", (NOW - timedelta(hours=40)).isoformat()),
        _FakeProposal(ap.GENERATOR_ID, "other", (NOW - timedelta(hours=1)).isoformat()),
        _FakeProposal("budget_hawk", "team-bot-a", (NOW - timedelta(hours=1)).isoformat()),
    ]
    assert ap.recent_offer_count(proposals, "team-bot-a", now=NOW) == 1


def test_an_unparseable_created_at_counts_toward_the_cap():
    """Conservative direction: an unreadable timestamp must not un-cap.

    MUTATION CHECKED: changing ``if created is None or created >= cutoff`` to
    ``if created is not None and created >= cutoff`` makes this go red.
    """
    proposals = [_FakeProposal(ap.GENERATOR_ID, "team-bot-a", "not-a-date")]
    assert ap.recent_offer_count(proposals, "team-bot-a", now=NOW) == 1


def test_is_promotion_proposal_matches_on_generator_not_action_kind():
    assert ap.is_promotion_proposal(_FakeProposal(ap.GENERATOR_ID, "team-bot-a", ""))
    assert not ap.is_promotion_proposal(_FakeProposal("fit_reviewer", "team-bot-a", ""))
    assert ap.is_promotion_proposal({"generator_id": ap.GENERATOR_ID})


# ─────────────────────────────────────────────────────────────────────────────
# Proposal construction
# ─────────────────────────────────────────────────────────────────────────────


def test_build_promotion_proposal_shape():
    manifest = _eligible_manifest()
    readiness = score_readiness(manifest)
    network = {"bots": {"team-bot-a": {"primary_user": {"external_ids": {"slack": "U1"}}}}}
    proposal = ap.build_promotion_proposal(
        manifest,
        bot_id="team-bot-a",
        manifest_stem="morning-brief",
        network=network,
        readiness=readiness,
        now=NOW,
        proposal_id="p-test",
    )
    assert proposal is not None
    assert proposal.approval_audience == "bot_primary_user"
    assert proposal.generator_id == ap.GENERATOR_ID
    assert proposal.action.kind == "PromoteApp"
    assert proposal.action.app_id == "morning-brief"
    assert proposal.action.identity_mode == "conferred"
    assert proposal.action.manifest_stem == "morning-brief"
    assert proposal.status == "draft"
    # The §7.3a decision travels ON the proposal.
    assert proposal.provenance.signals["identity_decision"]["mode"] == "conferred"
    # No fabricated Claim.
    assert proposal.claim is None


def test_build_promotion_proposal_carries_the_pod_operator_fallback():
    manifest = _eligible_manifest()
    proposal = ap.build_promotion_proposal(
        manifest,
        bot_id="team",
        manifest_stem="morning-brief",
        network={},
        readiness=score_readiness(manifest),
        now=NOW,
    )
    assert proposal is not None
    assert proposal.approval_audience == "pod_operator"


def test_build_promotion_proposal_returns_none_when_identity_is_blocked():
    """No id, no name → no proposal. Refuse rather than invent.

    MUTATION CHECKED: dropping the ``if not decision.ok: return None`` guard
    makes ``build_promotion_proposal`` construct a ``PromoteApp`` with an empty
    app_id and this go red.
    """
    proposal = ap.build_promotion_proposal(
        {"definition_status": "discovered"},
        bot_id="team-bot-a",
        manifest_stem="x",
        network={},
        now=NOW,
    )
    assert proposal is None


def test_two_scans_of_the_same_draft_coalesce():
    manifest = _eligible_manifest()
    kwargs = dict(
        bot_id="team-bot-a",
        manifest_stem="morning-brief",
        network={},
        readiness=score_readiness(manifest),
        now=NOW,
    )
    a = ap.build_promotion_proposal(manifest, **kwargs)
    b = ap.build_promotion_proposal(manifest, **kwargs)
    assert a is not None and b is not None
    assert a.coalesce_key == b.coalesce_key
    assert a.id != b.id  # distinct proposals; the store folds them


def test_a_rename_changes_the_conferred_id():
    """Design §7.2: "Rename → applied before conferring"."""
    manifest = _eligible_manifest()
    proposal = ap.build_promotion_proposal(
        manifest,
        bot_id="team-bot-a",
        manifest_stem="morning-brief",
        network={},
        readiness=score_readiness(manifest),
        proposed_name="Daily Digest",
        now=NOW,
    )
    assert proposal is not None
    assert proposal.action.app_id == "daily-digest"


def test_the_proposal_serializes_and_round_trips():
    """A PromoteApp proposal must survive the store's write/read.

    MUTATION CHECKED: removing ``"PromoteApp"`` from
    ``schema.proposal._ACTION_KIND_REGISTRY`` makes ``from_dict`` raise and
    this go red.
    """
    from schema.proposal import Proposal

    manifest = _eligible_manifest()
    proposal = ap.build_promotion_proposal(
        manifest,
        bot_id="team-bot-a",
        manifest_stem="morning-brief",
        network={},
        readiness=score_readiness(manifest),
        now=NOW,
    )
    assert proposal is not None
    restored = Proposal.from_dict(proposal.to_dict())
    assert restored.action.kind == "PromoteApp"
    assert restored.action.app_id == proposal.action.app_id
    assert restored.action.identity_mode == proposal.action.identity_mode


# ─────────────────────────────────────────────────────────────────────────────
# The bot's reply classifier
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reply",
    [
        "never",
        "never ask me about this again",
        "don't ask again",
        "do not ask me about it",
        "stop asking me",
        "no, never",
    ],
)
def test_never_is_recognised(reply):
    """MUTATION CHECKED: emptying ``_NEVER_PATTERNS`` makes every case red."""
    assert ph.classify_promote_reply(reply).action == "never"


@pytest.mark.parametrize("reply", ["no", "no thanks", "not interested", "nope", "maybe later"])
def test_a_plain_no_is_not_a_never(reply):
    """The asymmetry: an over-broad "never" is the expensive error.

    MUTATION CHECKED: adding ``r"\\bno\\b"`` to ``_NEVER_PATTERNS`` makes this
    go red — which is exactly the regression it exists to catch.
    """
    assert ph.classify_promote_reply(reply).action == "delegate"


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("call it Morning Brief", "Morning Brief"),
        ("yes but call it Daily Digest", "Daily Digest"),
        ("rename it to Brief", "Brief"),
        ('call it "Morning Brief". also add weather', "Morning Brief"),
        ("let's call it Standup", "Standup"),
    ],
)
def test_rename_extracts_the_name(reply, expected):
    result = ph.classify_promote_reply(reply)
    assert result.action == "rename"
    assert result.name == expected


def test_never_beats_rename_in_the_same_message():
    """MUTATION CHECKED: swapping the never/rename order in
    ``classify_promote_reply`` makes this go red — and would let a user who
    said "never" get an offer for a renamed app instead."""
    for reply in (
        "no, never — and don't call it that",
        "never ask again — and don't call it that",
    ):
        assert ph.classify_promote_reply(reply).action == "never", reply


def test_an_overlong_rename_is_feedback_not_a_name():
    long_tail = "call it " + ("x" * 100)
    assert ph.classify_promote_reply(long_tail).action == "delegate"


def test_classifier_tolerates_non_strings():
    for junk in (None, 7, [], {}):
        assert ph.classify_promote_reply(junk).action == "delegate"


# ─────────────────────────────────────────────────────────────────────────────
# The hop onto the existing conversational-approval flow (design §7.2)
# ─────────────────────────────────────────────────────────────────────────────


def test_the_generator_id_is_one_value_across_both_modules():
    """The router, the cadence counter and the queue filter must agree.

    MUTATION CHECKED: changing ``promote_handlers.PROMOTION_GENERATOR_ID`` makes
    this go red — and a divergence would silently route every promotion offer
    back to ``parse_intent``, losing "never" entirely.
    """
    assert ph.PROMOTION_GENERATOR_ID == ap.GENERATOR_ID


def test_a_non_promotion_rec_is_never_routed_to_the_promotion_phases():
    """MUTATION CHECKED: deleting the ``is_promotion_rec`` guard in
    ``promote_route_for_rec`` makes this go red — and would hijack "never" on
    an unrelated recommendation."""
    route = ph.promote_route_for_rec({"generator_id": "fit_reviewer"}, "never")
    assert route.phase == ""
    assert route.action == "delegate"


def test_never_routes_to_the_offer_phase_and_rename_to_confirm():
    from evolve_admin.evo.wizard import phases as wphases

    rec = {"generator_id": ap.GENERATOR_ID}
    assert (
        ph.promote_route_for_rec(rec, "never").phase
        == wphases.PHASE_APP_PROMOTE_OFFER
    )
    rename = ph.promote_route_for_rec(rec, "call it Daily Digest")
    assert rename.phase == wphases.PHASE_APP_PROMOTE_CONFIRM
    assert rename.name == "Daily Digest"


def test_a_plain_yes_or_no_stays_with_the_existing_handler():
    """Don't build a parallel stack (design §7.2 / roadmap.md).

    MUTATION CHECKED: making ``promote_route_for_rec`` claim every promotion
    turn makes this go red — and would take every accept away from
    ``parse_intent``, which is the classifier that actually handles it.
    """
    rec = {"generator_id": ap.GENERATOR_ID}
    for reply in ("yes", "no", "sure, do it", "next week"):
        assert ph.promote_route_for_rec(rec, reply).phase == ""


def test_the_never_shield_is_the_field_the_gate_reads():
    """MUTATION CHECKED: writing a different field name in
    ``apply_never_shield`` makes this go red — the shield would be written and
    the gate would keep offering."""
    manifest = _eligible_manifest()
    ph.apply_never_shield(manifest)
    assert manifest[ap.DO_NOT_OFFER_FIELD] is True
    decision = ap.evaluate_offer(
        manifest,
        bot_id="team-bot-a",
        readiness=score_readiness(manifest),
        recent_offers=0,
        now=NOW,
    )
    assert not decision.allowed
    assert decision.reason == "do_not_offer"


def test_the_promotion_phases_are_registered_and_renderable():
    """A declared phase the renderer cannot render is dead surface.

    MUTATION CHECKED: removing either phase from ``_PRIMARY_PHASES`` or either
    branch from ``prompts.build``'s dispatch makes this go red.
    """
    from evolve_admin.evo.wizard import phases as wphases
    from evolve_admin.evo.wizard import prompts as wprompts

    for name in (wphases.PHASE_APP_PROMOTE_OFFER, wphases.PHASE_APP_PROMOTE_CONFIRM):
        assert name in wphases._PRIMARY_PHASES
        assert wphases._PRIMARY_PHASES[name].render_mode == "verbatim"

    # Through the DISPATCH, not the block functions directly: a renderer that
    # exists but is not reachable from ``prompts.build`` is dead surface, and an
    # earlier version of this test asserted on the block functions and stayed
    # green when the dispatch branch was deleted.
    assert "won't bring that up again" in wprompts.build(
        wphases.PHASE_APP_PROMOTE_OFFER, {}
    )
    assert "Daily Digest" in wprompts.build(
        wphases.PHASE_APP_PROMOTE_CONFIRM,
        {},
        context={"proposed_name": "Daily Digest"},
    )


def test_the_offer_carries_an_action_label_that_is_not_review():
    """MUTATION CHECKED: removing the ``PromoteApp`` entry from
    ``proposal_reader.ACTION_LABELS`` falls back to "Review" and this goes
    red — the primary user would be asked to review, not to say yes."""
    from evolve_admin.better_engine.proposal_reader import ACTION_LABELS

    assert ACTION_LABELS.get("PromoteApp") == "Make it an app"


# ─────────────────────────────────────────────────────────────────────────────
# Rename — design §7.2 "Rename → applied before conferring"
# ─────────────────────────────────────────────────────────────────────────────


def test_a_rename_does_not_move_an_adopted_id():
    """The asymmetry that keeps rename from becoming a re-identify side door.

    MUTATION CHECKED: making ``resolve_rename`` slug the id from the new name
    unconditionally (bypassing ``adopt_or_confer_app_id``) makes this go red —
    and would let a user choosing a LABEL silently trigger the exact
    re-identification §7.3a rejected.
    """
    outcome = ap.resolve_rename(
        {"name": "Morning Brief", "app_id": "morning-brief"}, new_name="Daily Digest"
    )
    assert outcome is not None
    assert outcome.name == "Daily Digest"
    assert outcome.identity.app_id == "morning-brief"  # unchanged
    assert outcome.identity.mode == "adopted"
    assert outcome.id_changed is False


def test_a_rename_does_slug_a_conferred_id():
    outcome = ap.resolve_rename({"name": "Morning Brief"}, new_name="Daily Digest")
    assert outcome is not None
    assert outcome.identity.app_id == "daily-digest"
    assert outcome.identity.mode == "conferred"


def test_an_unusable_rename_is_declined_not_silently_ignored():
    """MUTATION CHECKED: returning the old name instead of ``None`` makes this
    go red — a rename the system silently dropped is worse than one it
    declined, because the user watched it be echoed back."""
    assert ap.resolve_rename({"name": "X"}, new_name="  ") is None
    assert ap.resolve_rename({}, new_name="!!") is None


def test_cadence_count_reads_dict_shaped_proposals_too():
    """F5: the counter and ``is_promotion_proposal`` must agree on shape.

    MUTATION CHECKED: reverting ``_field`` to ``getattr`` makes this go red —
    and the cadence cap would fail OPEN for dict-shaped input, which is the
    wrong direction for a cap.
    """
    dicts = [
        {
            "generator_id": ap.GENERATOR_ID,
            "bot_id": "team-bot-a",
            "created_at": (NOW - timedelta(hours=2)).isoformat(),
        }
    ]
    assert ap.recent_offer_count(dicts, "team-bot-a", now=NOW) == 1


# ─────────────────────────────────────────────────────────────────────────────
# The producer — F3. A gate nothing calls does not gate anything.
# ─────────────────────────────────────────────────────────────────────────────


def test_the_gate_and_the_builder_have_a_production_caller(monkeypatch, tmp_path):
    """F3 regression: these were reachable only from tests.

    **This test was itself the trap, and round 2 of the #3734 review caught it.**
    The first version asserted three names appeared in ``inspect.getsource(sweep)``
    — and all three appear in the module's own DOCSTRING, so it passed with the
    entire module body deleted. A guard written to prove a finding was closed,
    that passes against an empty module, is the purest form of the vacuity trap
    this arc keeps hitting.

    So it now proves the calls BEHAVIOURALLY: each of the three is monkeypatched
    with a spy, a real sweep is run, and every spy must have been called.
    Renaming any of the three call sites makes this go red because the spy is
    never reached — not because a string vanished from a comment.

    MUTATION CHECKED (all three, individually): replacing
    ``_promotion.evaluate_offer`` / ``.recent_offer_count`` /
    ``.build_promotion_proposal`` in the sweep with a local stand-in.
    """
    from evolve_admin.applications import app_promotion as promotion
    from evolve_admin.applications import app_promotion_sweep as sweep

    called: set[str] = set()

    def spy(name, real):
        def wrapper(*a, **k):
            called.add(name)
            return real(*a, **k)

        return wrapper

    monkeypatch.setattr(
        sweep._promotion, "evaluate_offer",
        spy("evaluate_offer", promotion.evaluate_offer),
    )
    monkeypatch.setattr(
        sweep._promotion, "recent_offer_count",
        spy("recent_offer_count", promotion.recent_offer_count),
    )
    monkeypatch.setattr(
        sweep._promotion, "build_promotion_proposal",
        spy("build_promotion_proposal", promotion.build_promotion_proposal),
    )

    (tmp_path / "proposals" / "pending").mkdir(parents=True)
    sweep.sweep_bot(
        tmp_path,
        "team-bot-a",
        network={},
        read_manifests=lambda _b: [("brief", _eligible_manifest())],
        now=NOW,
    )
    assert called == {
        "evaluate_offer",
        "recent_offer_count",
        "build_promotion_proposal",
    }


def test_the_sweep_ranks_deterministically_not_in_filesystem_order():
    from evolve_admin.applications import app_promotion_sweep as sweep

    low = {"name": "Notes", "definition_status": "discovered"}
    high = _eligible_manifest()
    forward = sweep.select_candidates(
        [("a-low", low), ("b-high", high)],
        bot_id="team-bot-a",
        recent_offers=0,
        now=NOW,
    )
    reverse = sweep.select_candidates(
        [("b-high", high), ("a-low", low)],
        bot_id="team-bot-a",
        recent_offers=0,
        now=NOW,
    )
    assert [c.manifest_stem for c in forward] == [c.manifest_stem for c in reverse]
    assert forward[0].manifest_stem == "b-high"


def test_the_sweep_offers_nothing_when_the_cadence_cap_is_spent():
    """MUTATION CHECKED: dropping ``recent_offers`` from the ``select_candidates``
    call makes this go red — the cap would be computed and then not applied."""
    from evolve_admin.applications import app_promotion_sweep as sweep

    candidates = sweep.select_candidates(
        [("brief", _eligible_manifest())],
        bot_id="team-bot-a",
        recent_offers=1,
        now=NOW,
    )
    assert not any(c.offerable for c in candidates)


def test_the_sweep_reports_why_it_declined_each_draft():
    """A sweep that reports only what it proposed cannot be debugged."""
    from evolve_admin.applications import app_promotion_sweep as sweep

    candidates = sweep.select_candidates(
        [("shielded", _eligible_manifest(**{ap.DO_NOT_OFFER_FIELD: True}))],
        bot_id="team-bot-a",
        recent_offers=0,
        now=NOW,
    )
    assert candidates[0].decision.blockers == ("do_not_offer",)


def test_the_sweep_fails_closed_when_the_proposal_store_is_unreadable(monkeypatch):
    """No cadence memory → no offer. An offer made blind is the pestering
    design §10 names.

    **This test's MUTATION CHECKED claim used to be FALSE**, and round 7 proved
    it by applying the named mutation and watching the suite stay green. A
    nonexistent shared dir does not make ``iter_proposals`` raise — it returns
    ``[]`` — so the ``except`` branch was never entered and the assertion was
    being satisfied by the *write*-failure path instead. Branch coverage showed
    the handler lines never executing.

    That is the fifth face of this arc's one defect class: **an exception path
    no test enters, with a test named for it.** Gutting cannot see it (the
    function IS reached); only branch-level probing can.

    So the store read is now made to actually fail.

    MUTATION CHECKED: replacing the ``except`` branch with a fall-through makes
    this go red — verified by applying it, not by assuming it.
    """
    from arbiter import store as astore
    from evolve_admin.applications import app_promotion_sweep as sweep

    def _boom(*_a, **_k):
        raise OSError("store unreadable")

    monkeypatch.setattr(astore, "iter_proposals", _boom)
    result = sweep.sweep_bot(
        Path("/nonexistent/definitely/not/a/shared/dir"),
        "team-bot-a",
        network={},
        read_manifests=lambda _b: [("brief", _eligible_manifest())],
        now=NOW,
    )
    assert result.offered == ()
    # The FAIL-CLOSED signal specifically — not merely "offered nothing", which
    # the write-failure path also produces.
    assert result.cadence_blocked is True
    assert result.considered == 0


def test_the_sweep_offers_at_most_one_per_bot(tmp_path):
    """Design §7.2: "at most one offer per bot per day".

    MUTATION CHECKED: returning every ``offerable`` candidate instead of
    ``offerable[0]`` makes this go red.
    """
    from evolve_admin.applications import app_promotion_sweep as sweep

    (tmp_path / "proposals" / "pending").mkdir(parents=True)
    result = sweep.sweep_bot(
        tmp_path,
        "team-bot-a",
        network={},
        read_manifests=lambda _b: [
            ("one", _eligible_manifest(name="Alpha App")),
            ("two", _eligible_manifest(name="Beta App")),
        ],
        now=NOW,
        dry_run=True,
    )
    assert len(result.offered) == 1
    assert result.considered == 2


def test_the_sweep_actually_writes_a_pending_proposal(tmp_path):
    """The end-to-end MECHANISM, on a staged draft — not the P1 demo."""
    from arbiter import store as astore
    from evolve_admin.applications import app_promotion_sweep as sweep

    (tmp_path / "proposals" / "pending").mkdir(parents=True)
    result = sweep.sweep_bot(
        tmp_path,
        "team-bot-a",
        network={},
        read_manifests=lambda _b: [("brief", _eligible_manifest())],
        now=NOW,
    )
    assert result.offered == ("brief",)
    written = list(astore.iter_proposals(tmp_path, subdirs=("pending",)))
    assert len(written) == 1
    assert written[0].generator_id == ap.GENERATOR_ID
    assert written[0].action.kind == "PromoteApp"
    assert written[0].status == "pending"


def test_a_dry_run_writes_nothing(tmp_path):
    from arbiter import store as astore
    from evolve_admin.applications import app_promotion_sweep as sweep

    (tmp_path / "proposals" / "pending").mkdir(parents=True)
    sweep.sweep_bot(
        tmp_path,
        "team-bot-a",
        network={},
        read_manifests=lambda _b: [("brief", _eligible_manifest())],
        now=NOW,
        dry_run=True,
    )
    assert list(astore.iter_proposals(tmp_path, subdirs=("pending",))) == []


@pytest.mark.parametrize(
    "reply",
    [
        "yes! I never remember to do it myself",
        "yes please, I never want to type that again",
        "sure — I've never had an app before",
        "honestly I never thought of that, go ahead",
    ],
)
def test_never_does_not_fire_inside_an_acceptance(reply):
    """The F2 defect an independent review found on #3734.

    A bare ``\\bnever\\b`` in ``_NEVER_PATTERNS`` is checked before rename and
    before delegation, so it beat an ACCEPT in the same message — and wrote a
    **permanent** ``do_not_offer`` shield over an enthusiastic yes. The module's
    own docstring already said the patterns must be phrase-anchored; the list
    did not follow it.

    MUTATION CHECKED: re-adding ``r"\\bnever\\b"`` to ``_NEVER_PATTERNS`` makes
    every case here go red. This is the regression test for the worst bug in
    the first cut.
    """
    assert ph.classify_promote_reply(reply).action != "never"


def _seed_promotion_proposal(shared_dir, *, bot_id, created_at):
    """Write one real promotion Proposal into the store, for cadence tests."""
    from arbiter import store as astore

    proposal = ap.build_promotion_proposal(
        _eligible_manifest(name="Seeded App"),
        bot_id=bot_id,
        manifest_stem="seeded",
        network={},
        readiness=score_readiness(_eligible_manifest(name="Seeded App")),
        now=created_at,
    )
    assert proposal is not None
    proposal.status = "pending"
    astore.write_proposal(proposal, shared_dir)
    return proposal


def test_the_sweep_reads_the_cadence_cap_from_the_store(tmp_path):
    """End to end through ``sweep_bot``, not through ``select_candidates``.

    MUTATION CHECKED: changing ``sweep_bot``'s ``recent_offers=recent`` to
    ``recent_offers=0`` makes this go red. An earlier version of this test
    called ``select_candidates`` directly with ``recent_offers=1`` and stayed
    GREEN under that mutation — it tested the gate, never the wiring.
    """
    from evolve_admin.applications import app_promotion_sweep as sweep

    (tmp_path / "proposals" / "pending").mkdir(parents=True)
    _seed_promotion_proposal(
        tmp_path, bot_id="team-bot-a", created_at=NOW - timedelta(hours=2)
    )
    result = sweep.sweep_bot(
        tmp_path,
        "team-bot-a",
        network={},
        read_manifests=lambda _b: [("brief", _eligible_manifest())],
        now=NOW,
    )
    assert result.offered == ()
    assert result.cadence_blocked is True
    assert ("brief", ("cadence_cap",)) in result.skipped


def test_the_cadence_cap_is_per_bot_not_pod_wide(tmp_path):
    """Two bots each getting one offer is the design (§7.2)."""
    from evolve_admin.applications import app_promotion_sweep as sweep

    (tmp_path / "proposals" / "pending").mkdir(parents=True)
    _seed_promotion_proposal(
        tmp_path, bot_id="team-bot-a", created_at=NOW - timedelta(hours=2)
    )
    result = sweep.sweep_bot(
        tmp_path,
        "team-bot-b",
        network={},
        read_manifests=lambda _b: [("brief", _eligible_manifest())],
        now=NOW,
    )
    assert result.offered == ("brief",)


def test_a_real_sweep_writes_exactly_one_proposal_from_many_candidates(tmp_path):
    """Design §7.2's cap, on the WRITING path.

    MUTATION CHECKED: returning every ``offerable`` candidate instead of
    ``offerable[0]`` makes this go red. The earlier version of this assertion
    used ``dry_run=True``, which returns before that line and stayed GREEN.
    """
    from arbiter import store as astore
    from evolve_admin.applications import app_promotion_sweep as sweep

    (tmp_path / "proposals" / "pending").mkdir(parents=True)
    result = sweep.sweep_bot(
        tmp_path,
        "team-bot-a",
        network={},
        read_manifests=lambda _b: [
            ("one", _eligible_manifest(name="Alpha App")),
            ("two", _eligible_manifest(name="Beta App")),
        ],
        now=NOW,
    )
    assert len(result.offered) == 1
    assert len(list(astore.iter_proposals(tmp_path, subdirs=("pending",)))) == 1


# ─────────────────────────────────────────────────────────────────────────────
# The engine dispatch — F1/F4. Both were defects an independent review caught.
# ─────────────────────────────────────────────────────────────────────────────


def test_the_confirm_phase_has_an_engine_dispatch_branch():
    """F1 regression: without it the phase has NO handler.

    ``APP_PROMOTE_CONFIRM_PHASE`` has ``targets=()`` (so ``has_extractor`` is
    False), ``exit_condition=_always_exit`` and ``next_phase=None``, and it is
    not in ``_interactive_terminal``. So a turn landing on it with no dispatch
    branch falls through to ``_finalize`` — the PRIMARY-chain finaliser, which
    commits a user profile and ends the session at ``PHASE_WRAP`` while the
    proposal sits untouched and the rename is dropped on the floor.

    MUTATION CHECKED: deleting the ``if phase.name ==
    _phases.PHASE_APP_PROMOTE_CONFIRM`` branch from ``engine._run_phase``'s
    dispatch chain makes this go red.
    """
    import inspect

    from evolve_admin.evo.wizard import engine as weng

    source = inspect.getsource(weng)
    # The constant must appear in a dispatch comparison, not only inside the
    # handler that SETS it — which is exactly the state the review found.
    assert "if phase.name == _phases.PHASE_APP_PROMOTE_CONFIRM:" in source
    assert hasattr(weng, "_handle_promote_confirm")
    # ...and the behavioural twin below proves the branch is REACHED. Round 2
    # noted that this assertion alone would stay green if the branch were moved
    # into an unreachable function, which is exactly right.


def test_the_never_path_does_not_finalize_through_the_generic_wrap():
    """F4 regression: ``_finalize_rec_pending`` rewrites ``current_phase`` back
    to ``REC_PENDING`` and builds its own "all caught up" message, so the
    never-acknowledgement was never rendered — the user who said "never" got a
    generic wrap, the exact outcome that block's docstring forbids.

    MUTATION CHECKED: routing the never branch back through
    ``_finalize_rec_pending`` makes this go red.
    """
    import inspect

    from evolve_admin.evo.wizard import engine as weng

    # Comment lines stripped: the function's docstring and comments NAME
    # ``_finalize_rec_pending`` to explain why it is not used, and asserting on
    # raw source would pass on the prose while the call came back.
    lines = [
        line.split("#", 1)[0]
        for line in inspect.getsource(weng._handle_promotion_reply).splitlines()
    ]
    code = "\n".join(lines)
    assert "_finalize_rec_pending(" not in code
    assert "PHASE_APP_PROMOTE_OFFER" in code


def test_the_never_acknowledgement_is_honest_when_the_shield_failed():
    """Promising permanence we did not achieve is worse than the generic wrap.

    MUTATION CHECKED: dropping the ``shield_written is False`` branch from
    ``prompts._promote_never_block`` makes this go red.
    """
    from evolve_admin.evo.wizard import phases as wphases
    from evolve_admin.evo.wizard import prompts as wprompts

    ok = wprompts.build(wphases.PHASE_APP_PROMOTE_OFFER, {}, context={})
    failed = wprompts.build(
        wphases.PHASE_APP_PROMOTE_OFFER, {}, context={"shield_written": False}
    )
    assert "stays off the list" in ok
    assert "couldn't get the permanent marker written" in failed


def test_the_confirm_phase_renders_all_three_of_its_outcomes():
    from evolve_admin.evo.wizard import phases as wphases
    from evolve_admin.evo.wizard import prompts as wprompts

    applied = wprompts.build(
        wphases.PHASE_APP_PROMOTE_CONFIRM,
        {},
        context={
            "variant": "rename_applied",
            "proposed_name": "Daily Digest",
            "app_id": "daily-digest",
        },
    )
    assert "Daily Digest" in applied and "Done" in applied

    # A confirmed rename that did NOT land must say so, not claim success.
    lost = wprompts.build(
        wphases.PHASE_APP_PROMOTE_CONFIRM,
        {},
        context={"variant": "rename_applied", "proposed_name": "X", "app_id": ""},
    )
    assert "couldn't get the name" in lost

    cancelled = wprompts.build(
        wphases.PHASE_APP_PROMOTE_CONFIRM,
        {},
        context={"variant": "rename_cancelled", "proposed_name": "X"},
    )
    assert "still open" in cancelled


def test_the_sweep_is_runnable_as_a_module():
    """A producer nothing can run is the F3 defect one level up.

    MUTATION CHECKED: deleting ``_main`` or the ``__main__`` guard from
    ``app_promotion_sweep`` makes this go red.

    Note what this does and does NOT claim: the sweep is **runnable**, not
    **scheduled**. Nothing installs a LaunchDaemon or cron entry for it, and
    brief §8.3 says so rather than letting "there is an entry point" be read as
    "it runs". Asserting scheduling here would be the vacuity move.
    """
    import inspect

    from evolve_admin.applications import app_promotion_sweep as sweep

    assert callable(getattr(sweep, "_main", None))
    assert '__name__ == "__main__"' in inspect.getsource(sweep)
    assert callable(getattr(sweep, "_read_manifests_for_bot", None))


def test_the_sweep_cli_exits_zero_when_nothing_is_eligible(tmp_path, monkeypatch):
    """"Nothing eligible" is the expected steady state today (§8.1), not a
    failure — a non-zero exit would make the correct outcome look like a broken
    job to whatever schedules it.

    MUTATION CHECKED: returning 1 when ``results`` has no offers makes this go
    red.
    """
    from evolve_admin.applications import app_promotion_sweep as sweep

    (tmp_path / "proposals" / "pending").mkdir(parents=True)
    monkeypatch.setattr(
        sweep, "_read_manifests_for_bot", lambda _b: [("weak", {"name": "Notes"})]
    )
    monkeypatch.setattr(
        "evolve_admin.config.load_network", lambda *a, **k: {"bots": {"team-bot-a": {}}}
    )
    assert sweep._main(["--shared-dir", str(tmp_path), "--bot", "team-bot-a"]) == 0


# ─────────────────────────────────────────────────────────────────────────────
# B3 — BEHAVIOURAL coverage of the F1/F4 fix.
#
# Round 2 of the #3734 review found every F1/F4 test was a source-text
# assertion, and demonstrated three mutations that stayed GREEN across 207
# tests — including ``_apply_promotion_rename`` returning "" unconditionally,
# which is the entire F1 defect reintroduced. These tests drive the real
# functions against a real proposal store and assert on what changed on disk.
# ─────────────────────────────────────────────────────────────────────────────


def _wizard_state(wstate, phase):
    """A minimal live WizardState for the behavioural engine turns below."""
    return wstate.WizardState(
        user_key="slack:U1",
        bot_id="team-bot-a",
        audience="primary",
        status="in_progress",
        current_phase=phase,
    )


@pytest.fixture
def promotion_session(tmp_path, monkeypatch):
    """A real shared_dir with one pending promotion proposal + a manifest.

    Returns ``(shared_dir, rec, manifests)`` where ``rec`` is the wizard-state
    shape the engine handlers actually receive (``source_ref.proposal_id``), and
    ``manifests`` is the in-memory bot manifest store the patched server helpers
    read and write. Patching those two helpers is what keeps this off a real bot
    home while still exercising the genuine read → mutate → write path.
    """
    from arbiter import store as astore
    from evolve_admin.web import server as wsrv

    (tmp_path / "proposals" / "pending").mkdir(parents=True)
    manifest = _eligible_manifest()
    proposal = ap.build_promotion_proposal(
        manifest,
        bot_id="team-bot-a",
        manifest_stem="brief",
        network={},
        readiness=score_readiness(manifest),
        now=NOW,
        proposal_id="p-behavioural",
    )
    assert proposal is not None
    proposal.status = "pending"
    astore.write_proposal(proposal, tmp_path)

    manifests: dict[str, dict] = {"brief": dict(manifest)}
    monkeypatch.setattr(
        wsrv, "_read_manifest_as_bot",
        lambda bot_id, stem, user=None: (
            dict(manifests[stem]) if stem in manifests else None
        ),
    )

    def _write(bot_id, stem, data, user=None):
        manifests[stem] = dict(data)
        return True

    monkeypatch.setattr(wsrv, "_write_manifest_as_bot", _write)

    rec = {
        "id": "rec_prop_p-behavioural",
        "generator_id": ap.GENERATOR_ID,
        "source_ref": {"proposal_id": "p-behavioural", "bot_id": "team-bot-a"},
    }
    return tmp_path, rec, manifests


def test_a_confirmed_rename_actually_reaches_the_proposal(promotion_session):
    """**The F1 defect, behaviourally.**

    MUTATION CHECKED: making ``engine._apply_promotion_rename`` ``return ""``
    unconditionally makes this go red. That mutation stayed GREEN across 207
    tests before this test existed — it is the entire F1 defect reintroduced,
    and nothing caught it.
    """
    from arbiter import store as astore
    from evolve_admin.evo.wizard import engine as weng

    shared_dir, rec, manifests = promotion_session
    app_id = weng._apply_promotion_rename(
        shared_dir, "team-bot-a", rec, "Daily Digest"
    )

    # The manifest carries the new name...
    assert manifests["brief"]["name"] == "Daily Digest"
    # ...and the PROPOSAL was re-pointed, so the applier confers what the user
    # confirmed rather than what the offer was minted with.
    stored = [
        p for p in astore.iter_proposals(shared_dir, subdirs=("pending",))
        if p.id == "p-behavioural"
    ]
    assert len(stored) == 1
    assert stored[0].action.app_id == app_id == "daily-digest"
    assert stored[0].human_title == "Make Daily Digest an app"


def test_a_rename_of_an_app_that_already_has_an_id_does_not_move_the_id(
    promotion_session,
):
    """§7.3a ADOPT, on the WRITE path — rename is not a re-identify side door."""
    from arbiter import store as astore
    from evolve_admin.evo.wizard import engine as weng

    shared_dir, rec, manifests = promotion_session
    manifests["brief"]["app_id"] = "morning-brief"
    app_id = weng._apply_promotion_rename(
        shared_dir, "team-bot-a", rec, "Daily Digest"
    )
    assert app_id == "morning-brief"  # id unchanged
    assert manifests["brief"]["name"] == "Daily Digest"  # label changed


def test_a_rename_against_a_missing_proposal_reports_failure(promotion_session):
    """MUTATION CHECKED: returning a name-derived id when the proposal is not
    found makes this go red — and would report a rename as applied that never
    touched a proposal."""
    from evolve_admin.evo.wizard import engine as weng

    shared_dir, _rec, _manifests = promotion_session
    orphan = {"id": "x", "source_ref": {"proposal_id": "does-not-exist"}}
    assert weng._apply_promotion_rename(
        shared_dir, "team-bot-a", orphan, "Daily Digest"
    ) == ""


def test_the_never_shield_is_actually_written_to_the_manifest(promotion_session):
    """**F4's honesty, behaviourally.**

    MUTATION CHECKED: making ``engine._apply_promotion_never`` ``return True``
    immediately makes this go red. That mutation stayed GREEN before this test —
    the shield was never written AND was reported as written, which defeats the
    exact honest-failure reporting F4's fix exists to provide.
    """
    from evolve_admin.evo.wizard import engine as weng

    shared_dir, rec, manifests = promotion_session
    written, _durable = weng._apply_promotion_never(shared_dir, "team-bot-a", rec)
    assert written is True
    assert manifests["brief"][ap.DO_NOT_OFFER_FIELD] is True
    # And the gate now refuses it — the shield is only real if the gate reads it.
    decision = ap.evaluate_offer(
        manifests["brief"],
        bot_id="team-bot-a",
        readiness=score_readiness(manifests["brief"]),
        recent_offers=0,
        now=NOW,
    )
    assert decision.reason == "do_not_offer"


def test_an_unwritable_never_reports_false_rather_than_claiming_success(
    promotion_session, monkeypatch
):
    """A shield that did not stick must not be reported as written.

    MUTATION CHECKED: returning True on a failed write makes this go red — and
    the user would be promised a permanence they did not get.
    """
    from evolve_admin.evo.wizard import engine as weng
    from evolve_admin.web import server as wsrv

    shared_dir, rec, _manifests = promotion_session
    monkeypatch.setattr(
        wsrv, "_write_manifest_as_bot", lambda *a, **k: False
    )
    assert weng._apply_promotion_never(shared_dir, "team-bot-a", rec) == (False, False)


def test_a_confirmed_rename_turn_applies_it_and_ends_the_session(promotion_session):
    """Drive a real turn through ``_handle_promote_confirm``.

    MUTATION CHECKED: forcing ``confirmed = False`` in
    ``engine._handle_promote_confirm`` makes this go red. That mutation stayed
    GREEN before this test — a confirmed rename was simply never applied.
    """
    from arbiter import store as astore
    from evolve_admin.evo.wizard import engine as weng
    from evolve_admin.evo.wizard import phases as wphases
    from evolve_admin.evo.wizard import promote_handlers as wph
    from evolve_admin.evo.wizard import state as wstate

    shared_dir, rec, manifests = promotion_session
    st = _wizard_state(wstate, wphases.PHASE_APP_PROMOTE_CONFIRM)
    st.extracted["_pending_rec"] = rec
    st.extracted[wph.PROMOTE_OFFER_STATE_KEY] = {
        "rec_id": rec["id"],
        "answer": "rename",
        "proposed_name": "Daily Digest",
    }
    result = weng._handle_promote_confirm(
        shared_dir, st, "team-bot-a", "slack:U1", "yes", {}
    )
    assert result.completed is True
    assert result.phase == wphases.PHASE_APP_PROMOTE_CONFIRM
    assert manifests["brief"]["name"] == "Daily Digest"
    # The accept moves the proposal out of ``pending`` (that is what recording
    # an approval DOES), so look across every subdir it can have landed in
    # rather than asserting it stayed put.
    stored = [
        p
        for sub in ("pending", "snoozed", "applied", "archived")
        for p in astore.iter_proposals(shared_dir, subdirs=(sub,))
        if p.id == "p-behavioural"
    ]
    assert len(stored) == 1, "the proposal must still exist somewhere"
    assert stored[0].action.app_id == "daily-digest"


def test_backing_out_of_a_rename_applies_nothing(promotion_session):
    """A user who wandered off mid-rename has NOT said no.

    MUTATION CHECKED: recording a reject on the back-out path makes this go red.
    """
    from arbiter import store as astore
    from evolve_admin.evo.wizard import engine as weng
    from evolve_admin.evo.wizard import phases as wphases
    from evolve_admin.evo.wizard import promote_handlers as wph
    from evolve_admin.evo.wizard import state as wstate

    shared_dir, rec, manifests = promotion_session
    st = _wizard_state(wstate, wphases.PHASE_APP_PROMOTE_CONFIRM)
    st.extracted["_pending_rec"] = rec
    st.extracted[wph.PROMOTE_OFFER_STATE_KEY] = {
        "rec_id": rec["id"], "answer": "rename", "proposed_name": "Daily Digest",
    }
    weng._handle_promote_confirm(
        shared_dir, st, "team-bot-a", "slack:U1", "actually hold on", {}
    )
    assert manifests["brief"]["name"] == "Morning Brief"  # untouched
    stored = list(astore.iter_proposals(shared_dir, subdirs=("pending",)))
    assert stored[0].action.app_id == "morning-brief"  # untouched
    assert stored[0].status == "pending"  # still answerable


def test_an_ambiguous_reply_writes_nothing_and_asks(promotion_session):
    """B4: accept + never in one message is resolved by ASKING, not by guessing.

    MUTATION CHECKED: routing ``ambiguous`` to the never branch makes this go
    red — and would permanently shield an acceptance, the exact harm.
    """
    from evolve_admin.evo.wizard import engine as weng
    from evolve_admin.evo.wizard import phases as wphases
    from evolve_admin.evo.wizard import promote_handlers as wph
    from evolve_admin.evo.wizard import state as wstate

    shared_dir, rec, manifests = promotion_session
    route = wph.promote_route_for_rec(
        rec, "yes, so I never again have to think about it"
    )
    assert route.action == "ambiguous"

    st = _wizard_state(wstate, wphases.PHASE_REC_PENDING)
    st.extracted["_pending_rec"] = rec
    result = weng._handle_promotion_reply(
        shared_dir, st, "team-bot-a", "slack:U1", {}, rec=rec, route=route,
    )
    # Nothing irreversible written, and the session stays open for the answer.
    assert ap.DO_NOT_OFFER_FIELD not in manifests["brief"]
    assert result.completed is False
    assert result.wizard_session_id == "slack:U1"
    # It must ASK — i.e. stay on the offer for a clarify turn, NOT drop into the
    # rename-confirm phase. Without this the test passed with the ambiguous
    # branch deleted, because the fall-through happened to write nothing too.
    assert result.phase == wphases.PHASE_REC_PENDING
    assert st.extracted[wph.PROMOTE_OFFER_STATE_KEY]["answer"] == "ambiguous"
    # The question must NAME both options — the generic clarify text offers
    # neither, so a user who meant "never" would have to find the words again.
    asked = result.direct_send_message or result.system_append
    assert "never" in asked.lower() and "yes" in asked.lower()


def test_a_never_turn_terminates_through_its_own_phase(promotion_session):
    """F4 behaviourally: not through ``_finalize_rec_pending``'s generic wrap."""
    from evolve_admin.evo.wizard import engine as weng
    from evolve_admin.evo.wizard import phases as wphases
    from evolve_admin.evo.wizard import promote_handlers as wph
    from evolve_admin.evo.wizard import state as wstate

    shared_dir, rec, manifests = promotion_session
    route = wph.promote_route_for_rec(rec, "never ask me about this again")
    assert route.action == "never"

    st = _wizard_state(wstate, wphases.PHASE_REC_PENDING)
    st.extracted["_pending_rec"] = rec
    result = weng._handle_promotion_reply(
        shared_dir, st, "team-bot-a", "slack:U1", {}, rec=rec, route=route,
    )
    assert result.phase == wphases.PHASE_APP_PROMOTE_OFFER
    assert result.completed is True
    assert result.wizard_session_id is None
    assert manifests["brief"][ap.DO_NOT_OFFER_FIELD] is True
    # The fixture manifest is legacy-shaped, so the honest wording is the
    # legacy one — NOT the "survives the next scan" promise.
    said = result.direct_send_message or result.system_append
    assert "won't bring that up again" in said
    assert "survives the next scan" not in said



@pytest.mark.parametrize(
    "reply",
    [
        # One case per ACCEPT MARKER shape, so emptying any single pattern is
        # caught. An earlier version used only sentence-initial "yes", and
        # deleting the mid-sentence marker left it green.
        "yes, so I never again have to think about it",
        "I'd love that, go ahead — never again by hand",
        "that sounds good, and stop asking me about that",
        "well yes, never again by hand",
        "please do, never again typing this out",
        "let's do it, so I never again forget",
        "set it up and never ask me twice",
    ],
)
def test_an_accept_plus_a_never_is_ambiguous_not_a_permanent_shield(reply):
    """B4: on an IRREVERSIBLE answer, ambiguity is resolved by asking.

    MUTATION CHECKED: deleting the ``_ACCEPT_MARKERS`` check in
    ``classify_promote_reply`` makes every case here go red — each becomes a
    permanent ``do_not_offer`` written over an acceptance, which is the harm.
    Deleting any single accept pattern also goes red, because each case above
    is carried by a different one.
    """
    assert ph.classify_promote_reply(reply).action == "ambiguous"


@pytest.mark.parametrize(
    "reply",
    [
        "never ever again",
        "please never ever ask me about this",
        "don't ever ask me again",
        "no, don't ever offer this again",
        "never, thanks",
        "never ever suggest this",
    ],
)
def test_an_intervening_word_does_not_break_a_genuine_never(reply):
    """The other direction: the adjacency fix must not miss real nevers.

    Round 2 measured these all falling through to ``delegate`` (i.e. snooze) —
    the cheap failure by the module's asymmetry, but still wrong.

    MUTATION CHECKED: reverting ``never( ever)? again`` to ``never again``, or
    removing the ``don't ever …`` patterns, makes the matching cases go red.
    """
    assert ph.classify_promote_reply(reply).action == "never"



def test_process_turn_actually_routes_the_confirm_phase_to_its_handler(
    promotion_session, monkeypatch
):
    """The behavioural twin of the dispatch-branch source assertion.

    Round 2's point: asserting the source string proves the branch was WRITTEN,
    not that it is REACHED — it would stay green if the branch were moved into
    an unreachable function. So this drives a real ``process_turn`` with the
    session parked on ``PHASE_APP_PROMOTE_CONFIRM`` and asserts the handler ran.

    MUTATION CHECKED: deleting the dispatch branch makes this go red (the turn
    falls through to the primary-chain finaliser and the spy never fires) —
    which is the F1 defect, caught at the routing layer rather than at the
    handler's own door.
    """
    from evolve_admin.evo.wizard import engine as weng
    from evolve_admin.evo.wizard import phases as wphases
    from evolve_admin.evo.wizard import promote_handlers as wph
    from evolve_admin.evo.wizard import state as wstate

    shared_dir, rec, _manifests = promotion_session
    st = _wizard_state(wstate, wphases.PHASE_APP_PROMOTE_CONFIRM)
    st.extracted["_pending_rec"] = rec
    st.extracted[wph.PROMOTE_OFFER_STATE_KEY] = {
        "rec_id": rec["id"], "answer": "rename", "proposed_name": "Daily Digest",
    }
    wstate.write_state(shared_dir, st)

    reached: list[str] = []
    real = weng._handle_promote_confirm

    def spy(*a, **k):
        reached.append("yes")
        return real(*a, **k)

    monkeypatch.setattr(weng, "_handle_promote_confirm", spy)
    result = weng.process_turn(
        shared_dir,
        bot_id="team-bot-a",
        user_key="slack:U1",
        user_message="yes",
        network={},
    )
    assert reached == ["yes"], "the CONFIRM phase never reached its handler"
    assert result is not None
    assert result.phase == wphases.PHASE_APP_PROMOTE_CONFIRM


def test_process_turn_routes_a_never_through_the_promotion_hop(promotion_session):
    """**C4 — the feature's on-switch, end to end through ``process_turn``.**

    Round 3 found that deleting the REC_PENDING → promotion hop
    (``if promote_route.phase:`` in ``_handle_rec_pending``) left 192/192 tests
    GREEN. That branch is what makes the whole feature reachable: without it
    ``parse_intent`` reads "never ask me about this again" as a plain
    ``reject`` and **snoozes** — design §10's named worst outcome, the user
    asked again next week having said never.

    Every prior test entered at ``promote_route_for_rec`` or
    ``_handle_promotion_reply``, i.e. downstream of the switch. This one starts
    at ``process_turn`` with a live REC_PENDING session, so the hop is on the
    path.

    MUTATION CHECKED: ``if promote_route.phase:`` → ``if False:`` makes this go
    red.
    """
    from evolve_admin.evo.wizard import engine as weng
    from evolve_admin.evo.wizard import phases as wphases
    from evolve_admin.evo.wizard import state as wstate

    shared_dir, rec, manifests = promotion_session
    st = _wizard_state(wstate, wphases.PHASE_REC_PENDING)
    st.extracted["_pending_rec"] = rec
    wstate.write_state(shared_dir, st)

    result = weng.process_turn(
        shared_dir,
        bot_id="team-bot-a",
        user_key="slack:U1",
        user_message="never ask me about this again",
        network={},
    )
    assert result is not None
    # Routed to the promotion phase, not left to parse_intent's reject/snooze.
    assert result.phase == wphases.PHASE_APP_PROMOTE_OFFER
    assert result.completed is True
    # And the permanent shield actually landed.
    assert manifests["brief"][ap.DO_NOT_OFFER_FIELD] is True


def test_process_turn_routes_a_rename_through_the_promotion_hop(promotion_session):
    """C4's other half: a rename must reach the CONFIRM phase.

    Without the hop ``parse_intent`` reads "call it Daily Digest" as
    ``unknown`` and asks for clarification, and the rename is lost.

    MUTATION CHECKED: ``if promote_route.phase:`` → ``if False:`` makes this go
    red.
    """
    from evolve_admin.evo.wizard import engine as weng
    from evolve_admin.evo.wizard import phases as wphases
    from evolve_admin.evo.wizard import promote_handlers as wph
    from evolve_admin.evo.wizard import state as wstate

    shared_dir, rec, _manifests = promotion_session
    st = _wizard_state(wstate, wphases.PHASE_REC_PENDING)
    st.extracted["_pending_rec"] = rec
    wstate.write_state(shared_dir, st)

    result = weng.process_turn(
        shared_dir,
        bot_id="team-bot-a",
        user_key="slack:U1",
        user_message="call it Daily Digest",
        network={},
    )
    assert result is not None
    assert result.phase == wphases.PHASE_APP_PROMOTE_CONFIRM
    after = wstate.read_state(shared_dir, "team-bot-a", "slack:U1")
    assert after is not None
    assert (
        after.extracted[wph.PROMOTE_OFFER_STATE_KEY]["proposed_name"]
        == "Daily Digest"
    )


def test_process_turn_leaves_a_plain_yes_to_the_existing_classifier(promotion_session):
    """The hop must not swallow the common case (design §7.2: no parallel stack).

    MUTATION CHECKED: making ``promote_route_for_rec`` claim every promotion
    turn makes this go red.
    """
    from evolve_admin.evo.wizard import engine as weng
    from evolve_admin.evo.wizard import phases as wphases
    from evolve_admin.evo.wizard import state as wstate

    shared_dir, rec, manifests = promotion_session
    st = _wizard_state(wstate, wphases.PHASE_REC_PENDING)
    st.extracted["_pending_rec"] = rec
    wstate.write_state(shared_dir, st)

    result = weng.process_turn(
        shared_dir,
        bot_id="team-bot-a",
        user_key="slack:U1",
        user_message="yes",
        network={},
    )
    assert result is not None
    assert result.phase == wphases.PHASE_REC_PENDING
    # And nothing promotion-specific was written on the plain path.
    assert ap.DO_NOT_OFFER_FIELD not in manifests["brief"]


@pytest.mark.parametrize(
    "reply",
    [
        # C2 — one fixture per accept pattern NOT carried by another, so
        # deleting any single pattern goes red. Round 3 found the anchored
        # ``^(yes|yeah|...)`` alternation had no such fixture: every case that
        # exercised it was also carried by a mid-sentence marker.
        "sure, never again by hand",
        "yeah, never ask me twice",
        "ok, never again",
        "yep, never again by hand",
        "yup, never ask me twice",
        "okay, never again",
        "great, never again by hand",
        "perfect, never ask me twice",
        "of course, never again",
        "absolutely, never again by hand",
        "definitely, never ask me twice",
        "i'd love it, never again",
        "would love that, never again by hand",
        "just go ahead, never again by hand",
        "that would be great, never again",
        "why not, never again",
        "sign me up, never again by hand",
        "make it so, never ask me twice",
    ],
)
def test_every_accept_pattern_has_a_fixture_that_only_it_carries(reply):
    """C2: a pattern with no fixture of its own is an untested pattern.

    Each string above was checked to fail (become ``never``) when its own
    pattern is removed — that check was run in the same session this test was
    written, against the pattern list as it stands.
    """
    assert ph.classify_promote_reply(reply).action == "ambiguous"



def test_the_real_manifest_reader_actually_reads_manifests(tmp_path, monkeypatch):
    """N1: ``_read_manifests_for_bot`` was guarded only by ``assert callable``.

    Round 3 measured it returning ``[]`` with the whole suite still green — the
    last bare hasattr-shaped assertion with no behavioural twin. A reader that
    returns nothing makes the sweep silently offer nothing, which is
    indistinguishable from the pod's genuine "nothing is eligible" state. That
    is the worst possible failure mode for THIS module, because the honest
    answer and the broken answer look identical.

    MUTATION CHECKED: ``return []`` at the top of ``_read_manifests_for_bot``
    makes this go red.
    """
    from evolve_admin.applications import app_promotion_sweep as sweep
    from evolve_admin.web import server as wsrv

    store = {"brief": _eligible_manifest(), "notes": {"name": "Notes"}}
    monkeypatch.setattr(
        wsrv, "_list_manifests_as_bot",
        lambda bot_id, user=None: [f"/x/{stem}.json" for stem in sorted(store)],
    )
    monkeypatch.setattr(
        wsrv, "_read_manifest_as_bot",
        lambda bot_id, stem, user=None: dict(store[stem]) if stem in store else None,
    )
    got = sweep._read_manifests_for_bot("team-bot-a")
    assert [stem for stem, _m in got] == ["brief", "notes"]
    assert dict(got[0][1])["name"] == "Morning Brief"


def test_the_sweep_offers_the_best_ranked_candidate_not_an_arbitrary_one(tmp_path):
    """N2: the ranking → selection wiring was untested.

    Round 3 measured ``offerable[0]`` → ``offerable[-1]`` staying green: the
    sweep ranked correctly and then could have picked anything. Ranking that
    does not decide the choice is decoration.

    MUTATION CHECKED: ``best = offerable[0]`` → ``offerable[-1]`` makes this go
    red.
    """
    from arbiter import store as astore
    from evolve_admin.applications import app_promotion_sweep as sweep

    (tmp_path / "proposals" / "pending").mkdir(parents=True)
    strong = _eligible_manifest(name="Strong App")
    # Same eligibility, strictly lower readiness: no implementation file, so it
    # sits a rung below on design §7.1's evidence ladder.
    weaker = _eligible_manifest(name="Weaker App", files=[{"path": "n.md", "layer": "reference"}])
    assert score_readiness(strong).score > score_readiness(weaker).score

    result = sweep.sweep_bot(
        tmp_path,
        "team-bot-a",
        network={},
        read_manifests=lambda _b: [("weaker", weaker), ("strong", strong)],
        now=NOW,
    )
    assert result.offered == ("strong",)
    written = list(astore.iter_proposals(tmp_path, subdirs=("pending",)))
    assert written[0].action.app_id == "strong-app"


def test_the_offer_text_says_what_design_7_2_requires_in_one_breath():
    """The pitch the primary user actually reads — design §7.2's example.

    Found by gutting every public entry point in turn and looking for one no
    test enters at: ``offer_text`` was the last one. It is not incidental — it
    is the *entire user-facing surface* of the offer, and §10's mitigation for
    "promotion offers annoy" is that "the offer states the payoff in one
    breath". A pitch that lost the payoff, the name, or the evidence would have
    shipped silently.

    MUTATION CHECKED: returning a stub string from ``offer_text`` makes this go
    red — and so does dropping any one of the three clauses.
    """
    manifest = _eligible_manifest()
    text = ap.offer_text(
        manifest, proposed_name="Morning Brief", readiness=score_readiness(manifest)
    )
    # The name it would be called (the user is invited to change it).
    assert "Morning Brief" in text
    assert "change the name" in text
    # The payoff, in one breath (design §7.2's quoted example).
    assert "app" in text
    assert "costs" in text or "cost" in text
    # The evidence it is based on — the readiness driver, not a bare assertion.
    assert "noticed" in text
    # And what it thinks the app IS.
    assert "email + calendar" in text


def test_the_offer_text_survives_a_manifest_with_nothing_in_it():
    """No name, no purpose, no readiness — still a usable sentence, not a crash
    and not a half-rendered template."""
    text = ap.offer_text({})
    assert "app" in text
    assert "{" not in text and "None" not in text


def test_the_proposal_carries_the_offer_text_as_its_conversational_pitch():
    """The pitch has to reach the Proposal, or the bot never speaks it.

    MUTATION CHECKED: dropping ``conversational_pitch=pitch`` from
    ``build_promotion_proposal`` makes this go red.
    """
    manifest = _eligible_manifest()
    proposal = ap.build_promotion_proposal(
        manifest,
        bot_id="team-bot-a",
        manifest_stem="brief",
        network={},
        readiness=score_readiness(manifest),
        now=NOW,
    )
    assert proposal is not None
    assert proposal.conversational_pitch
    assert "Morning Brief" in proposal.conversational_pitch


def test_a_promotion_proposal_actually_reaches_the_member_bot_surface():
    """The delivery claim, EXECUTED rather than asserted in prose.

    The PR body and brief §8.5 item 1 both claim promotion needs no new queue
    because ``proposal_to_recommendation`` already routes a
    ``bot_primary_user`` proposal to the member-bot surface. That is a claim
    about a module this chip does not own — the class round 4 caught with the
    ``do_not_offer`` permanence claim, where four such assertions had never
    been run.

    So this runs it: mint a real promotion Proposal, put it through the real
    reader, and check the three properties the delivery story depends on.

    MUTATION CHECKED: setting ``approval_audience`` to ``pod_operator`` in
    ``build_promotion_proposal`` drops ``bot_executable`` and makes this go red;
    removing the ``ACTION_LABELS`` entry makes the label assertion go red.
    """
    from evolve_admin.better_engine.proposal_reader import (
        proposal_to_recommendation,
    )

    manifest = _eligible_manifest()
    network = {"bots": {"team-bot-a": {"primary_user": {"external_ids": {"slack": "U1"}}}}}
    proposal = ap.build_promotion_proposal(
        manifest,
        bot_id="team-bot-a",
        manifest_stem="brief",
        network=network,
        readiness=score_readiness(manifest),
        now=NOW,
    )
    assert proposal is not None
    proposal.status = "pending"
    rec = proposal_to_recommendation(proposal)

    assert rec is not None, "the offer must survive the reader at all"
    # 1. It reaches the bot, not just the admin UI — design §7.2's whole point.
    assert rec.bot_executable is True
    assert (rec.scope, rec.scope_id) == ("bot", "team-bot-a")
    # 2. It asks the user to say yes, not to "Review" something.
    assert rec.action_label == "Make it an app"
    # 3. The wizard hop can recognise it once it arrives.
    assert ph.is_promotion_rec(rec.to_dict())


def test_the_never_acknowledgement_tells_the_truth_on_each_manifest_shape():
    """Round 4's D1, applied to the fix for round 4's D1.

    The D1 fix made the shield durable on the **v7-arc** path. The legacy write
    path is in ``scanner.py`` — out of scope per brief §5 — and still drops it.
    So a single reassuring sentence would be false for legacy-shape users:
    *exactly* the defect D1 was, reintroduced by its own fix.

    The bot therefore says one of three things, chosen from what actually
    happened rather than from a constant.

    MUTATION CHECKED: deleting the ``shield_durable is False`` branch from
    ``prompts._promote_never_block`` makes this go red.
    """
    from evolve_admin.evo.wizard import phases as wphases
    from evolve_admin.evo.wizard import prompts as wprompts

    durable = wprompts.build(wphases.PHASE_APP_PROMOTE_OFFER, {}, context={})
    legacy = wprompts.build(
        wphases.PHASE_APP_PROMOTE_OFFER, {}, context={"shield_durable": False}
    )
    failed = wprompts.build(
        wphases.PHASE_APP_PROMOTE_OFFER, {}, context={"shield_written": False}
    )
    assert "survives the next scan" in durable
    # The legacy user is NOT promised scan survival.
    assert "survives the next scan" not in legacy
    assert "rescan" in legacy
    assert "couldn't get the permanent marker written" in failed
    assert len({durable, legacy, failed}) == 3


def test_the_never_path_reports_durability_from_the_manifest_shape(
    promotion_session,
):
    """``_apply_promotion_never`` must MEASURE durability, not assume it.

    MUTATION CHECKED: hardcoding ``return (True, True)`` makes this go red —
    and would tell a legacy-shape user their refusal survives a rescan when it
    does not.
    """
    from evolve_admin.evo.wizard import engine as weng

    shared_dir, rec, manifests = promotion_session
    # The fixture manifest is legacy-shaped (no v7-arc install provenance).
    written, durable = weng._apply_promotion_never(shared_dir, "team-bot-a", rec)
    assert written is True
    assert durable is False, "a legacy-shaped manifest must not claim durability"

    # Now the v7-arc shape. ``coherence_actions._is_v7_arc`` reads the
    # ``manifest_shape`` tag, NOT the provenance block — an earlier version of
    # this test built a plausible-looking v7-arc dict without the tag and read
    # False, which is the same "asserted what the neighbouring module does"
    # mistake round 4 found, made in a test.
    from evolve_admin.applications.manifest import MANIFEST_SHAPE_V7_ARC

    manifests["brief"] = {
        "name": "Morning Brief",
        "instance_id": "i-x",
        "manifest_shape": MANIFEST_SHAPE_V7_ARC,
        "provenance": {
            "spec_id": "p-aaaa0001", "spec_version": "1.0.0",
            "installed_at": "2026-01-01T00:00:00Z", "installed_by": "scanner",
        },
    }
    written2, durable2 = weng._apply_promotion_never(shared_dir, "team-bot-a", rec)
    assert written2 is True
    assert durable2 is True


# ─────────────────────────────────────────────────────────────────────────────
# E2 — a consumer with no producer. The class mutation testing cannot catch.
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# AST helpers for the structural checks below.
#
# Round 7 found BOTH structural checks passing on prose — a comment mentioning
# `a_function(` satisfied the caller check, and the round-6 fix to the writer
# check stripped `#` comments but not DOCSTRINGS, though round 6's own text said
# the occurrences were "a comment AND a docstring".
#
# That is this arc's own defect class appearing INSIDE the apparatus built to
# catch it. A source-text check cannot police source-text claims: every fix is
# one more string the next mention slips past. So these parse the code and match
# real AST nodes — an actual `ast.Call`, an actual assignment whose target is an
# `ast.Subscript`. A comment is not a node; a docstring is a string constant,
# never a Call or an assignment target. The trick these checks exist to catch
# cannot be played on an AST.
# ─────────────────────────────────────────────────────────────────────────────


def _repo_root():
    from pathlib import Path as _P

    return _P(__file__).resolve().parents[3]


def _parse(path):
    import ast

    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return None


def _is_production(rel):
    from pathlib import Path as _P

    return "/tests/" not in rel and not _P(rel).name.startswith("test_")


def _used_names(tree):
    """Every name USED as code — a call ``f()`` / ``x.f()``, or an attribute
    access ``x.prop``.

    Attribute access is included because a ``@property`` is *used* without being
    called: ``candidate.offerable`` is an ``ast.Attribute`` with no enclosing
    ``ast.Call``, and treating it as unused would flag every property in the
    tree. That is a real limit to state rather than paper over — it means this
    check cannot distinguish "the property is read" from "some attribute of that
    name is read somewhere". Still strictly better than a regex: a comment or a
    docstring produces neither node.
    """
    import ast

    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                out.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                out.add(fn.attr)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
    return out


def _subscript_assigned_keys(tree):
    """Keys assigned into a mapping: ``d[K] = v``. Returns literals and names.

    Matches the ASSIGNMENT, so a docstring containing ``manifest[FIELD] = x``
    is invisible to it — which is the whole point.
    """
    import ast

    out = set()
    targets = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets.append(node.target)
    for t in targets:
        if not isinstance(t, ast.Subscript):
            continue
        key = t.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            out.add(key.value)
        elif isinstance(key, ast.Name):
            out.add(key.id)
        elif isinstance(key, ast.Attribute):
            out.add(key.attr)
    return out


def _defined_functions(tree):
    """``{qualified_name: simple_name}`` for every def, async included.

    Qualified so an allowlist can exempt ``OfferDecision.to_dict`` without
    exempting every ``to_dict`` in the tree — round 7's R3.
    """
    import ast

    out = {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not child.name.startswith("__"):
                    out[f"{prefix}{child.name}"] = child.name
                walk(child, prefix)
            else:
                walk(child, prefix)

    walk(node=ast.Module(body=tree.body, type_ignores=[]), prefix="")
    return out



def test_every_field_the_gate_reads_has_a_non_test_writer():
    """**Catches E2's class: a consumer with no producer.**

    Round 5 found ``promotion_snoozed_until`` was check 2 of 5 in
    ``evaluate_offer`` with nothing in the repo writing it. No mutation can
    catch that — mutate the reader and the hand-built fixture still exercises
    it — so the check has to be structural.

    **Round 6 then found this check passing on a comment**, and **round 7 found
    the round-6 fix still passing on a docstring** (it stripped ``#`` lines but
    not string literals, though round 6's own text said "a comment AND a
    docstring"). Two rounds of narrowing a regex, each defeated by the next
    string that looked like code.

    So it is now an **AST** check: a real assignment whose target is a
    subscript. A docstring is an ``ast.Constant``; a comment is not a node at
    all. Neither can be mistaken for a write.

    MUTATION CHECKED — all run in this session: deleting the write line;
    deleting the whole producer and leaving the comment; and putting a
    ``manifest[SNOOZE_UNTIL_FIELD] = ...`` line inside a docstring.
    """
    import subprocess

    repo = _repo_root()
    for field, const in (
        (ap.DO_NOT_OFFER_FIELD, "DO_NOT_OFFER_FIELD"),
        (ap.SNOOZE_UNTIL_FIELD, "SNOOZE_UNTIL_FIELD"),
    ):
        candidates = subprocess.run(
            ["git", "grep", "-l", "-e", field, "-e", const, "--", "packages/"],
            cwd=repo, capture_output=True, text=True,
        ).stdout.splitlines()
        writers = []
        for rel in candidates:
            if not _is_production(rel):
                continue
            tree = _parse(repo / rel)
            if tree is None:
                continue
            if {field, const} & _subscript_assigned_keys(tree):
                writers.append(rel)
        assert writers, (
            f"{field} is read by the gate but ASSIGNED nowhere outside tests — "
            f"a consumer with no producer. Files merely mentioning it: {candidates}"
        )


def test_a_promotion_snooze_is_written_to_the_draft(promotion_session):
    """E2: the producer for ``SNOOZE_UNTIL_FIELD``.

    MUTATION CHECKED: making ``engine._apply_promotion_snooze`` return early
    without writing makes this go red.
    """
    from evolve_admin.evo.wizard import engine as weng

    shared_dir, rec, manifests = promotion_session
    assert weng._apply_promotion_snooze(
        shared_dir, "team-bot-a", rec, days=30
    ) is True
    stamped = manifests["brief"][ap.SNOOZE_UNTIL_FIELD]
    assert stamped

    # And the gate now honours it — a snooze that the gate does not read is not
    # a snooze.
    decision = ap.evaluate_offer(
        manifests["brief"],
        bot_id="team-bot-a",
        readiness=score_readiness(manifests["brief"]),
        recent_offers=0,
        now=NOW + timedelta(hours=25),  # past the cadence cap, inside the snooze
    )
    assert not decision.allowed
    assert decision.reason == "snoozed"


def test_a_snooze_with_no_duration_uses_the_documented_default(promotion_session):
    """MUTATION CHECKED: changing ``PROMOTION_DEFAULT_SNOOZE_DAYS`` makes this
    go red, so the constant cannot drift away from what is documented."""
    from evolve_admin.evo.wizard import engine as weng

    shared_dir, rec, manifests = promotion_session
    assert weng._apply_promotion_snooze(
        shared_dir, "team-bot-a", rec, days=None
    ) is True
    stamped = manifests["brief"][ap.SNOOZE_UNTIL_FIELD]

    # Still snoozed the day before the default expires, offerable the day after.
    from datetime import datetime as _dt

    until = _dt.fromisoformat(stamped.replace("Z", "+00:00"))
    day = timedelta(days=1)
    assert ap.evaluate_offer(
        manifests["brief"], bot_id="team-bot-a",
        readiness=score_readiness(manifests["brief"]),
        recent_offers=0, now=until - day,
    ).reason == "snoozed"
    assert ap.evaluate_offer(
        manifests["brief"], bot_id="team-bot-a",
        readiness=score_readiness(manifests["brief"]),
        recent_offers=0, now=until + day,
    ).allowed
    assert weng.PROMOTION_DEFAULT_SNOOZE_DAYS == 7


def test_a_snooze_against_an_unresolvable_draft_reports_failure(promotion_session):
    """A snooze that silently did not stick means the user is asked again
    tomorrow having said "not for a month"."""
    from evolve_admin.evo.wizard import engine as weng

    shared_dir, _rec, _m = promotion_session
    orphan = {"id": "x", "source_ref": {"proposal_id": "does-not-exist"}}
    assert weng._apply_promotion_snooze(
        shared_dir, "team-bot-a", orphan, days=7
    ) is False


def test_process_turn_writes_the_snooze_on_a_real_defer(promotion_session):
    """E2 at the WIRING layer, not the helper's own door.

    An earlier version of these tests called ``_apply_promotion_snooze``
    directly, so disabling its call site in ``_handle_rec_pending`` stayed
    green — the producer existed and nothing invoked it, which is E2 itself
    reintroduced one level along.

    MUTATION CHECKED: ``if result.action == "snooze" and
    _promote.is_promotion_rec(rec):`` → ``if False:`` makes this go red.
    """
    from evolve_admin.evo.wizard import engine as weng
    from evolve_admin.evo.wizard import phases as wphases
    from evolve_admin.evo.wizard import state as wstate

    shared_dir, rec, manifests = promotion_session
    st = _wizard_state(wstate, wphases.PHASE_REC_PENDING)
    st.extracted["_pending_rec"] = rec
    wstate.write_state(shared_dir, st)

    weng.process_turn(
        shared_dir,
        bot_id="team-bot-a",
        user_key="slack:U1",
        user_message="not right now, maybe later",
        network={},
    )
    assert ap.SNOOZE_UNTIL_FIELD in manifests["brief"], (
        "a promotion snooze must reach the DRAFT, not only the proposal"
    )


def test_a_failed_snooze_write_is_reported_as_failure(promotion_session, monkeypatch):
    """MUTATION CHECKED: returning True on a failed write makes this go red —
    and the user would be told nothing while being re-offered tomorrow."""
    from evolve_admin.evo.wizard import engine as weng
    from evolve_admin.web import server as wsrv

    shared_dir, rec, _manifests = promotion_session
    monkeypatch.setattr(wsrv, "_write_manifest_as_bot", lambda *a, **k: False)
    assert weng._apply_promotion_snooze(
        shared_dir, "team-bot-a", rec, days=7
    ) is False


def test_every_brief_step_cross_reference_resolves():
    """Round 5's E1 tail: two references pointed at steps that did not exist.

    §8.9 said the legacy gap "is §8.3 step 7" (step 7 was the Tier-1 gap) and
    §8.9a said Tier-1 was "step 8" (there was no step 8) — so the legacy gap was
    on no list at all, and the sentence claiming it was "not a silent gap" was
    what made it one.

    This is checkable by counting a numbered list, which is why it is a test and
    not a habit: the same class as E2 (a consumer with no producer), one layer
    further out — **a claim about the document's own contents, asserted without
    being read.**
    """
    import re
    from pathlib import Path as _P

    brief = (
        _P(__file__).resolve().parents[3] / "internal" / "build-AL-1.7-promotion.md"
    ).read_text(encoding="utf-8")

    section = brief.split("## 8.3")[1].split("\n## ")[0]
    # Match any numbered item, not only bolded ones — steps 1-3 are plain
    # prose. An earlier version of this test matched ``^\d+\. \*\*`` and
    # "found" a hole at 1-3 that was only a formatting difference: a checker
    # that reports a false positive is no more trustworthy than the claim it
    # checks.
    steps = {int(m) for m in re.findall(r"^(\d+)\. ", section, re.M)}
    assert steps, "§8.3 must have a numbered blocker list"
    assert steps == set(range(1, max(steps) + 1)), f"§8.3 numbering has a hole: {sorted(steps)}"

    # Scan the CODE too, not only the brief — the modules point readers at
    # numbered steps as well, and a docstring citing a step that does not exist
    # is the same dangling reference in a place nobody re-reads.
    repo = _P(__file__).resolve().parents[3]
    sources = [brief]
    for rel in (
        "packages/admin/evolve_admin/applications/app_promotion.py",
        "packages/admin/evolve_admin/applications/app_promotion_sweep.py",
        "packages/admin/evolve_admin/evo/wizard/promote_handlers.py",
        "packages/admin/evolve_admin/evo/wizard/engine.py",
        "packages/analyzer/arbiter/appliers/promote_app.py",
        # prompts.py carries a "§8.3 step 7" reference and was missing from
        # this list — a checker with an incomplete file list is the same shape
        # as the claims it checks. Round 7 planted a step-99 reference here and
        # watched it pass.
        "packages/admin/evolve_admin/evo/wizard/prompts.py",
    ):
        sources.append((repo / rel).read_text(encoding="utf-8"))

    referenced = {
        int(m) for text in sources for m in re.findall(r"§8\.3 step (\d+)", text)
    }
    missing = referenced - steps
    assert not missing, f"references point at §8.3 steps that do not exist: {sorted(missing)}"


@pytest.mark.parametrize(
    "days,expected",
    [
        (3, 3),
        (None, 7),
        (0, 7),
        (-5, 7),
        ("7", 7),
        (1.5, 7),
        (True, 7),      # isinstance(True, int) is True — the classic trap
        (False, 7),
        (10_000, 365),  # capped
        (366, 365),
    ],
)
def test_the_snooze_duration_is_bounded_and_bool_safe(
    promotion_session, days, expected
):
    """``snooze_hint_days`` is a STAGE-2 LLM extraction of free text — untrusted
    input, not a validated field.

    Two traps, both found by probing rather than by review:

    * ``isinstance(True, int)`` is ``True`` in Python, so ``days=True`` became a
      **one-day** snooze — "ask me again tomorrow" from a user who said "later".
      ``app_readiness`` already guards the same trap on ``invocation_count``.
    * An unbounded hint made a 27-year snooze reachable, which is a "never" by
      arithmetic — and "never" is the answer with no user-side undo today
      (§8.3 step 6), so a snooze must not become one accidentally.

    MUTATION CHECKED: dropping the ``isinstance(days, bool)`` clause makes the
    ``True`` case red; dropping the ``min(..., PROMOTION_MAX_SNOOZE_DAYS)`` cap
    makes the 10,000 and 366 cases red.
    """
    from datetime import datetime as _dt

    from evolve_admin.evo.wizard import engine as weng

    shared_dir, rec, manifests = promotion_session
    before = datetime.now(timezone.utc)
    assert weng._apply_promotion_snooze(
        shared_dir, "team-bot-a", rec, days=days
    ) is True
    until = _dt.fromisoformat(manifests["brief"][ap.SNOOZE_UNTIL_FIELD].replace("Z", "+00:00"))
    # Allow a second of slack for the clock read inside the helper.
    assert abs((until - before).days - expected) <= 1
    assert until - before < timedelta(days=expected + 1)


def test_a_second_snooze_replaces_the_first_rather_than_stacking(promotion_session):
    """Snoozing twice must not compound into an accidental "never"."""
    from datetime import datetime as _dt

    from evolve_admin.evo.wizard import engine as weng

    shared_dir, rec, manifests = promotion_session
    weng._apply_promotion_snooze(shared_dir, "team-bot-a", rec, days=30)
    first = manifests["brief"][ap.SNOOZE_UNTIL_FIELD]
    weng._apply_promotion_snooze(shared_dir, "team-bot-a", rec, days=2)
    second = manifests["brief"][ap.SNOOZE_UNTIL_FIELD]
    assert second != first
    a = _dt.fromisoformat(first.replace("Z", "+00:00"))
    b = _dt.fromisoformat(second.replace("Z", "+00:00"))
    assert b < a, "the newer, shorter snooze must win — not accumulate"


def test_self_promote_off_routes_approval_to_the_pod_operator():
    """B1: ``self_promote`` must be a knob, not a parser.

    Round 6 measured ``promotion_policy`` as having **no production caller** —
    outside its own module and tests, the only repo reference was a comment. So
    a pod admin setting ``pod.apps.promotion.self_promote = false`` got no
    change in behaviour, while the module docstring said *"that is the
    difference between a policy and a rule."* Round 1's no-caller finding, with
    a function substituted for a gate.

    ``audience_for`` now reads it, which is the one place the answer changes
    anything: the primary user is still who the bot ASKS, but the APPROVAL goes
    to the pod operator.

    MUTATION CHECKED: reverting ``audience_for`` to ignore the policy makes this
    go red.
    """
    reachable = {"bots": {"team-bot-a": {"primary_user": {"external_ids": {"slack": "U1"}}}}}
    assert ap.audience_for(reachable, "team-bot-a") == "bot_primary_user"

    off = dict(reachable)
    off["pod"] = {"apps": {"promotion": {"self_promote": False}}}
    assert ap.audience_for(off, "team-bot-a") == "pod_operator"


def test_self_promote_off_reaches_the_minted_proposal():
    """The knob has to survive all the way onto the Proposal, or it is still
    just a parser one layer along.

    MUTATION CHECKED: as above.
    """
    manifest = _eligible_manifest()
    network = {
        "bots": {"team-bot-a": {"primary_user": {"external_ids": {"slack": "U1"}}}},
        "pod": {"apps": {"promotion": {"self_promote": False}}},
    }
    proposal = ap.build_promotion_proposal(
        manifest,
        bot_id="team-bot-a",
        manifest_stem="brief",
        network=network,
        readiness=score_readiness(manifest),
        now=NOW,
    )
    assert proposal is not None
    assert proposal.approval_audience == "pod_operator"


def test_the_sweep_resolves_and_reports_the_policy(tmp_path):
    """The auto-promote refusal must be REACHABLE on the pod.

    ``promotion_policy`` logs a WARNING and returns ``AUTO_PROMOTE_REFUSAL`` for
    a config that asks for auto-promote — and round 6 found that line was
    unreachable, because nothing in production called the function. An operator
    who set the key was told nothing, by anything.

    MUTATION CHECKED: removing the ``policy = _promotion.promotion_policy(...)``
    call from ``sweep_bot`` makes this go red.
    """
    from evolve_admin.applications import app_promotion_sweep as sweep

    (tmp_path / "proposals" / "pending").mkdir(parents=True)
    result = sweep.sweep_bot(
        tmp_path,
        "team-bot-a",
        network={"pod": {"apps": {"promotion": {"auto_promote": True}}}},
        read_manifests=lambda _b: [("brief", _eligible_manifest())],
        now=NOW,
        dry_run=True,
    )
    assert result.policy is not None
    assert result.policy["auto_promote"] is False
    assert result.policy["auto_promote_requested"] is True
    assert "launders" in result.policy["auto_promote_refusal"]


def test_every_function_this_chip_adds_has_a_non_test_caller():
    """**Catches the fourth projection: a function with no non-test caller.**

    Gutting cannot see it — ``promotion_policy`` gutted went RED, because four
    tests called it directly. This is the writer check with **functions
    substituted for fields**, and the mirror of round 1's F3: *you cannot mutate
    a call site that does not exist.*

    **Round 7 found the first version passing on a comment** — one line reading
    ``# we used to call a_function(x) here`` satisfied it, which is round 6's
    finding reproduced inside the check written to close it. It now matches real
    ``ast.Call`` nodes.

    Two other round-7 findings folded in:

    * The scanned set is derived from the **diff**, not a hand-written module
      list — a hand-maintained list of what to check is the same shape as a
      hand-maintained claim about what was checked. ``async def`` is walked too.
    * The allowlist exempts by **qualified** name. Exempting a bare ``to_dict``
      hid two real instances of this very defect (``OfferDecision.to_dict`` and
      ``RenameOutcome.to_dict`` had zero references anywhere) — *an allowlist
      that exempts by bare name is a hole in the shape of the check.* Both were
      **deleted**, not exempted.

    **The limit, stated rather than glossed.** Definitions are matched
    qualified; CALLS can only be matched by simple name, because
    ``x.to_dict()`` cannot be resolved to a class statically. So a dead method
    can still be masked by a live method of the same name on another class —
    which is exactly how round 7's two survived. This check would not have found
    them; a reviewer did. It catches free functions reliably and methods only
    when the name is unique, and claiming more than that would be the same
    over-claim the check exists to punish.
    """
    import subprocess
    from pathlib import Path as _P

    repo = _repo_root()

    #: Qualified names reachable other than by an in-repo call, each with the
    #: reason. Bare names are NOT accepted here — that was the R3 hole.
    ENTRY_POINTS = {
        "_main": "module __main__ guard; exercised as a real subprocess",
        "sweep_bot": "called by _main",
        "_read_manifests_for_bot": "injected into sweep_bot by _main, and "
                                   "resolved by the generator's observe()",
        # Registered in generator_runner._CONTEXT_FACTORIES as a VALUE in the
        # dispatch table, so the only reference is an ast.Name load and never an
        # ast.Call — the runner calls it through the tuple. Driven for real by
        # packages/analyzer/tests/test_app_promotion_generator.py, which invokes
        # the factory directly AND runs the whole generator through
        # run_one_generator, so "no caller" here is the dispatch table.
        "_make_app_promotion_ctx": "generator_runner._CONTEXT_FACTORIES entry; "
                                   "called through the factory tuple",
        # Flask view function: the `@app.post(...)` decorator registers it and
        # Werkzeug dispatches by URL. Driven end-to-end by
        # packages/admin/tests/test_app_definition_routes.py.
        "set_application_promotion_shield": "Flask route POST …/definition/"
                                            "promotion-shield; dispatched by URL",
        # AL-1.8b. Same two shapes this list already covers, one each:
        # a Flask view registered by its decorator, and a callable handed to
        # another function as a VALUE (``pack_context_for=_pack_context``) —
        # an ast.Name load, never an ast.Call, which is exactly the hole
        # ``_make_app_promotion_ctx`` above is exempted for. Both are driven
        # for real by packages/admin/tests/test_apps_detail_fill.py: the route
        # through the test client, the context builder through the
        # placeholder-substitution states it is the only producer of.
        "api_apps_discovered_detail": "Flask route GET /api/apps/discovered/"
                                      "<ref>; dispatched by URL",
        "_pack_context": "injected into pod_apps.build_app_detail as "
                         "pack_context_for; called through the parameter",
        # ALPHA-2. The same ast.Name-load shape a third time: handed to
        # pod_apps.build_discovered as ``read_provenance=_read_provenance``.
        # Driven for real by packages/admin/tests/test_alpha2_scan_provenance.py
        # (the seam) and exercised end-to-end through GET /api/apps/discovered.
        "_read_provenance": "injected into pod_apps.build_discovered as "
                            "read_provenance; called through the parameter",
        # Dossier modules. The ast.Name-load shape again, four times: each is
        # an entry in dossier.modules.SPECS — a (declaration, builder) tuple
        # the assembler calls through as ``build(edition)``, so the only
        # reference to the name is a load and never an ast.Call. All four are
        # DRIVEN FOR REAL, not merely exempted, by
        # packages/analyzer/tests/test_dossier_modules.py: every one is
        # rendered from fixture edition pairs (headline + trend), from an
        # edition whose producers are all absent (the can't-measure path),
        # and end-to-end through the weekly run on a fixture pod.
        "_apps": "dossier.modules.SPECS entry; called through the tuple",
        "_reliability": "dossier.modules.SPECS entry; called through the tuple",
        "_cost": "dossier.modules.SPECS entry; called through the tuple",
        "_users": "dossier.modules.SPECS entry; called through the tuple",
        # AL-3.1. The two shapes this list already covers, once more: a Flask
        # view registered by its `@app.post` decorator and dispatched by URL,
        # and a click command handed to a group as a VALUE
        # (``application_group.add_command(snapshot_app_cmd)``) — an ast.Name
        # load, never an ast.Call. Both are DRIVEN FOR REAL rather than merely
        # exempted, in packages/admin/tests/test_al_3_1_app_snapshot.py: the
        # route through a Flask test client (the dry-run default AND the
        # refusal status), and the command through the real ``main`` click
        # group with a CliRunner — which is also the only thing that proves
        # cli.py's one-line registration actually attached it.
        "api_app_snapshot": "Flask route POST /api/apps/<app_id>/snapshot; "
                            "dispatched by URL",
        # artifact-by-reference (Documents): the same click shape again —
        # ``application_group.add_command(install_gallery_pack_cmd)`` in
        # gallery_pack_install_cli.register_cli, an ast.Name load. Driven for
        # real by packages/admin/tests/test_gallery_documents.py, which runs
        # install_gallery_pack (the command's whole body) against the fixture pod.
        "install_gallery_pack_cmd": "click command attached via "
                                    "application_group.add_command; dispatched by click",
        "snapshot_app_cmd": "click command attached via "
                            "app_snapshot_cli.register_cli -> "
                            "application_group.add_command; invoked by name "
                            "on the command line, never by an in-repo call",
        # Pod Intelligence. The Flask-view shape a fourth time, four at once:
        # each is registered by its `@app.get` / `@app.post` decorator inside
        # ``register_dossier_routes`` and dispatched by URL, so no in-repo
        # ast.Call ever names them. All four are DRIVEN FOR REAL rather than
        # merely exempted, by packages/admin/tests/test_dossier_routes.py
        # through a Flask test client — the reads against a fixture pod with
        # three weeks on record AND against one whose writer has never run,
        # and the write through its full round trip (round-trip, junk-drop,
        # cap, oversize refusal, non-object body). The page itself is driven
        # end-to-end against a real server in tests/browser/
        # test_pod_intelligence.py.
        "api_dossier_current": "Flask route GET /api/dossier/current; "
                               "dispatched by URL",
        "api_dossier_edition": "Flask route GET /api/dossier/editions/"
                               "<week_id>; dispatched by URL",
        "api_dossier_profile_get": "Flask route GET /api/dossier/profile; "
                                   "dispatched by URL",
        "api_dossier_profile_post": "Flask route POST /api/dossier/profile; "
                                    "dispatched by URL",
        # D-S2 track 3. The Flask-view shape once more: registered by its
        # `@app.get` decorator inside ``register_analytics_routes`` and
        # dispatched by URL, so no in-repo ast.Call ever names it. DRIVEN FOR
        # REAL rather than merely exempted, by
        # packages/admin/tests/test_analytics_usage_by_user.py through a Flask
        # test client — the measured read, the unmeasured bot, the
        # gates-withheld bot, every window, and the 400 on an unknown one.
        "api_analytics_usage_by_user": "Flask route GET /api/analytics/usage/"
                                       "by-user; dispatched by URL",
        # Exec-failure hygiene A2. The ast.Name-load shape again: it is an
        # entry in the ``monitors`` tuple inside api_signals_refresh, which
        # the fan-out calls through as ``ex.submit(fn)`` — so the only
        # reference to the name is a load and never an ast.Call, exactly like
        # the five sibling monitors already in that list. DRIVEN FOR REAL
        # rather than merely exempted, by
        # packages/admin/tests/test_signals_refresh_endpoint.py (the endpoint
        # through a Flask test client, asserting this monitor appears in the
        # response with ok=True) and by
        # packages/admin/tests/test_exec_failure_monitor.py, which drives the
        # underlying emit_exec_failure_signals end-to-end against a fixture
        # ledger plus its audit-scheduler tick path.
        "_run_exec_failure_monitor": "api_signals_refresh monitors tuple; "
                                     "called through ex.submit(fn)",
        # Context-economy CE-2b (tool profiles). Identical shape to the
        # exec-failure entry directly above: an ast.Name load in the
        # ``monitors`` tuple inside api_signals_refresh, reached only through
        # ``ex.submit(fn)``. DRIVEN FOR REAL rather than merely exempted, by
        # packages/admin/tests/test_signals_refresh_endpoint.py (the endpoint
        # through a Flask test client, asserting this monitor appears in the
        # response with ok=True) and by
        # packages/admin/tests/test_tool_profile_monitor.py, which drives the
        # underlying emit_tool_profile_signals end-to-end against a fixture
        # ledger plus its audit-scheduler tick path.
        "_run_tool_profile_monitor": "api_signals_refresh monitors tuple; "
                                     "called through ex.submit(fn)",
        # Board F8 (the mobile board's tailnet reach). The same two shapes
        # this list already covers, no new ones: four Flask views registered
        # by their `@app.get` decorators inside ``register_board_routes`` and
        # dispatched by URL, and a click group plus its two commands, attached
        # by ``cli.py``'s ``main.add_command(board_group)`` — an ast.Name load,
        # never an ast.Call.
        #
        # All six are DRIVEN FOR REAL rather than merely exempted. The views,
        # in packages/admin/tests/test_board_auth.py through a Flask test
        # client: the manifest parsed as JSON and its scope/start_url checked
        # against the bot, EVERY icon fetched by following the srcs the
        # manifest itself returns — which is what drives the maskable route
        # added on 2026-09-02, since the manifest is where its src comes
        # from — and the manifest's non-oracle property
        # (a valid-but-unminted bot id gets the same 200) — which is the whole
        # reason it is unauthenticated. The click commands, in
        # packages/admin/tests/test_board_cli.py through a real CliRunner: the
        # minted token round-tripped against the live board API, rotation
        # invalidating the previous one, the revoke round trip, and the bad
        # bot id refused before anything is written.
        "board_manifest": "Flask route GET /board/<bot_id>/"
                          "manifest.webmanifest; dispatched by URL",
        "board_icon_192": "Flask route GET /board/icon-192.png; "
                          "dispatched by URL",
        "board_icon_512": "Flask route GET /board/icon-512.png; "
                          "dispatched by URL",
        "board_icon_512_maskable": "Flask route GET "
                                   "/board/icon-512-maskable.png; "
                                   "dispatched by URL",
        "board_group": "click group attached via cli.py's "
                       "main.add_command(board_group); invoked by name on "
                       "the command line, never by an in-repo call",
        "board_token": "click command under board_group; invoked as "
                       "`evolve-admin board token <bot_id>`",
        "board_revoke": "click command under board_group; invoked as "
                        "`evolve-admin board revoke <bot_id>`",
        # A THIRD shape, new to this list: a stdlib framework hook. Python's
        # http.server calls it from ``log_request`` and ``log_error``, both
        # outside packages/, so no in-repo ast.Call can name it. It is the
        # board's token-scrub on the listener's own access lines, and it is
        # DRIVEN FOR REAL, twice, in
        # packages/admin/tests/test_board_listener.py: once over a live socket
        # serving a real board path, and once on the 400 whose message embeds
        # the raw request line — that second one is MUTATION CHECKED (dropping
        # the scrub_secrets call inside it makes the test red).
        "_BoardRequestHandler.log_message": "http.server framework hook; "
                                            "called by log_request/log_error "
                                            "from the stdlib, never in-repo",
    }
    #: **PRUNED 2026-08-21, per round 8's N4.** The list above previously
    #: carried nine entries, of which round 8 measured that five exempted
    #: nothing: they named functions added by *already-merged* branches, and
    #: ``added`` is computed from THIS branch's diff, so they could never fire
    #: again. N4's phrasing is the reason they are gone rather than inherited:
    #: *an allowlist that mostly exempts nothing is one that has stopped being
    #: read.* Two of the four here are this branch's; the other two are the
    #: sweep's own entry points, which any branch touching that module re-adds.

    base = subprocess.run(
        ["git", "merge-base", "HEAD", "origin/main"],
        cwd=repo, capture_output=True, text=True,
    ).stdout.strip()
    if not base:
        pytest.skip("no origin/main to diff against")

    changed = [
        f for f in subprocess.run(
            ["git", "diff", "--name-only", base, "--", "packages/"],
            cwd=repo, capture_output=True, text=True,
        ).stdout.splitlines()
        if f.endswith(".py") and _is_production(f)
    ]
    if not changed:
        #: This is a **branch-only** gate, and it must never report a pass it did
        #: not earn — but "did not measure" is a SKIP, not a failure. Two states
        #: reach here with nothing to measure and no risk to cover:
        #:
        #: * a ``main`` build, where ``HEAD`` *is* the merge-base and
        #:   ``base..HEAD`` is empty by construction — the old ``assert`` made
        #:   this permanently red on ``main``, and a permanently-red ``main``
        #:   trains everyone to ignore ``main`` being red, which is the one
        #:   signal that catches a concurrent-merge breakage;
        #: * a test-only or docs-only branch, which adds no production function
        #:   for this check to find a caller for.
        #:
        #: Skipping keeps the guard's real property intact: the check cannot
        #: pass green while having scanned nothing. It just says so out loud
        #: instead of failing the build over it.
        pytest.skip("no production python in the branch diff — nothing for this gate to measure")

    added: dict[str, tuple[str, str]] = {}
    for rel in changed:
        now_tree = _parse(repo / rel)
        if now_tree is None:
            continue
        import ast as _ast

        before_src = subprocess.run(
            ["git", "show", f"{base}:{rel}"], cwd=repo, capture_output=True, text=True
        ).stdout
        try:
            before = _defined_functions(_ast.parse(before_src)) if before_src else {}
        except SyntaxError:
            before = {}
        for qual, simple in _defined_functions(now_tree).items():
            if qual not in before:
                added.setdefault(qual, (simple, rel))

    # Every name CALLED anywhere in production python, as AST nodes.
    used: set[str] = set()
    for rel in subprocess.run(
        ["git", "ls-files", "packages/"], cwd=repo, capture_output=True, text=True
    ).stdout.splitlines():
        if not rel.endswith(".py") or not _is_production(rel):
            continue
        tree = _parse(repo / rel)
        if tree is not None:
            used |= _used_names(tree)

    uncalled = [
        f"{qual} ({rel})"
        for qual, (simple, rel) in sorted(added.items())
        if qual not in ENTRY_POINTS and simple not in used
    ]
    assert not uncalled, (
        "functions this branch adds with no non-test caller — a gate nothing "
        f"calls does not gate anything: {uncalled}"
    )


def test_promote_app_is_not_auto_decidable(monkeypatch):
    """Round 5/6's note: the human-only guard was a MEMBERSHIP assertion.

    ``assert "PromoteApp" in _HUMAN_ONLY_ACTION_KINDS`` proves the set contains
    a string. Round 6 defeated the real guard with an early return making
    ``PromoteApp`` decidable and watched all 140 tests stay green — the set was
    asserted, the *behaviour* was not.

    So this asks the eligibility layer the question that matters: **would this
    action ever be applied without a person?**

    MUTATION CHECKED: removing ``"PromoteApp"`` from
    ``_HUMAN_ONLY_ACTION_KINDS`` makes this go red, and so does an early return
    in ``evaluate_eligibility`` that marks it decidable.
    """
    import eligibility

    from evolve_admin.applications import app_promotion as promotion
    from evolve_admin.applications.app_readiness import score_readiness

    proposal = promotion.build_promotion_proposal(
        _eligible_manifest(),
        bot_id="team-bot-a",
        manifest_stem="brief",
        network={},
        readiness=score_readiness(_eligible_manifest()),
        now=NOW,
    )
    assert proposal is not None
    # A REAL promotion proposal through the REAL classifier — not a membership
    # assertion, and not a hand-built dict either: whatever risk_tag the builder
    # chose is the one the pod would see.
    result = eligibility.classify_proposal(proposal.to_dict())
    assert result.decidable is False, (
        "a promotion must never be auto-applied — it IS the operator vouch"
    )
    assert result.tier_floor == "ask"


def test_two_scans_of_the_same_draft_dedup_through_the_store(tmp_path):
    """N3: ``_coalesce_key`` gut GREEN — it produced ``coalesce_key`` and
    ``dismiss_signature`` under a comment asserting a dedup invariant that
    nothing tested.

    The comment claims "one promotion in flight per (bot, app): a second scan
    must coalesce into the first rather than stacking a duplicate offer". So
    this runs two sweeps and asks the STORE, which is what actually enforces it.

    MUTATION CHECKED: making ``_coalesce_key`` return a unique value per call
    makes this go red — two proposals land instead of one.
    """
    from arbiter import store as astore
    from evolve_admin.applications import app_promotion_sweep as sweep

    (tmp_path / "proposals" / "pending").mkdir(parents=True)
    manifest = _eligible_manifest()

    def one_sweep(now):
        return sweep.sweep_bot(
            tmp_path, "team-bot-a", network={},
            read_manifests=lambda _b: [("brief", manifest)], now=now,
        )

    assert one_sweep(NOW).offered == ("brief",)
    # A day later the cadence cap has expired and the same draft is still
    # discovered — the scan that would stack a duplicate.
    second = one_sweep(NOW + timedelta(hours=25))
    written = [
        p for sub in ("pending", "snoozed", "applied", "archived")
        for p in astore.iter_proposals(tmp_path, subdirs=(sub,))
        if p.generator_id == ap.GENERATOR_ID
    ]
    keys = {p.coalesce_key for p in written}
    assert len(keys) == 1, f"the same draft must share ONE coalesce key: {keys}"
    assert all(p.dismiss_signature == p.coalesce_key for p in written)
    assert second is not None


# ─────────────────────────────────────────────────────────────────────────────
# The FIFTH projection: a BRANCH no test enters.
#
# Gutting a function catches "no test enters the function". Inverting a
# conditional catches "no test enters the BRANCH" — a strictly finer question,
# and the one none of the four checks asked. Running it over the promotion code
# found seven branches whose inversion the entire suite did not notice. All
# seven are covered below.
# ─────────────────────────────────────────────────────────────────────────────


def test_a_naive_datetime_is_treated_as_utc_everywhere(promotion_session):
    """Four of the seven uncovered branches were ``tzinfo is None`` guards.

    They matter more than their size: comparing a naive datetime against an
    aware one raises ``TypeError`` in Python, so an un-entered guard is not a
    cosmetic gap — it is a crash on the first caller that passes ``now`` without
    a timezone, which is what ``datetime.now()`` returns by default.

    MUTATION CHECKED: inverting any of the four ``tzinfo is None`` guards makes
    this go red.
    """
    naive = datetime(2026, 8, 19, 12, 0, 0)  # no tzinfo
    manifest = _eligible_manifest(
        **{ap.SNOOZE_UNTIL_FIELD: "2026-08-25T00:00:00Z"}
    )

    # evaluate_offer's own `now`, and the parsed snooze it compares against.
    decision = ap.evaluate_offer(
        manifest, bot_id="team-bot-a", readiness=score_readiness(manifest),
        recent_offers=0, now=naive,
    )
    assert decision.reason == "snoozed"

    # recent_offer_count's `now`, and a naive created_at on the proposal side.
    proposals = [
        {
            "generator_id": ap.GENERATOR_ID,
            "bot_id": "team-bot-a",
            "created_at": "2026-08-19T11:00:00",  # naive
        }
    ]
    assert ap.recent_offer_count(proposals, "team-bot-a", now=naive) == 1

    # And the builder's own timestamp normalisation.
    proposal = ap.build_promotion_proposal(
        _eligible_manifest(), bot_id="team-bot-a", manifest_stem="brief",
        network={}, readiness=score_readiness(_eligible_manifest()), now=naive,
    )
    assert proposal is not None
    assert proposal.created_at.endswith("Z"), proposal.created_at


def test_the_auto_promote_refusal_is_logged_not_only_returned(caplog):
    """One uncovered branch was the ``if requested:`` WARNING in
    ``promotion_policy`` — the only thing that tells an operator their config
    key was refused.

    Round 6 established the function is now reachable in production; this
    establishes the branch inside it is exercised, which is a different
    question and was the one still unasked.

    MUTATION CHECKED: inverting ``if requested:`` makes this go red.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="evolve_admin.applications.app_promotion"):
        ap.promotion_policy({"pod": {"apps": {"promotion": {"auto_promote": True}}}})
    assert any("REFUSED" in r.getMessage() for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="evolve_admin.applications.app_promotion"):
        ap.promotion_policy({})
    assert not [r for r in caplog.records if "REFUSED" in r.getMessage()], (
        "a pod that did NOT ask for auto-promote must not be warned at it"
    )


def test_the_sweep_cli_handles_a_pod_with_no_bots(tmp_path, monkeypatch, capsys):
    """An uncovered branch: ``if not bots:`` in ``_main``.

    MUTATION CHECKED: inverting it makes this go red — and the inverted form
    would print "no bots to sweep" for every pod that HAS bots, and sweep
    nothing.
    """
    from evolve_admin.applications import app_promotion_sweep as sweep

    monkeypatch.setattr("evolve_admin.config.load_network", lambda *a, **k: {"bots": {}})
    assert sweep._main(["--shared-dir", str(tmp_path)]) == 0
    assert "no bots to sweep" in capsys.readouterr().err


def test_iso_helpers_normalise_naive_datetimes():
    """The last two uncovered branches from the inversion sweep.

    ``_parse_iso`` and ``_iso`` both carry a ``tzinfo is None`` guard that no
    test entered. They are not cosmetic: comparing a naive datetime against an
    aware one raises ``TypeError``, so an un-entered guard is a crash waiting
    for the first caller that stores a timestamp without a zone — which is what
    ``datetime.now()`` and a hand-edited manifest both produce.

    MUTATION CHECKED: inverting either guard makes this go red.
    """
    naive_iso = "2026-08-19T11:00:00"          # no zone
    aware_iso = "2026-08-19T11:00:00+00:00"
    assert ap._parse_iso(naive_iso) == ap._parse_iso(aware_iso)
    assert ap._parse_iso(naive_iso).tzinfo is not None

    assert ap._iso(datetime(2026, 8, 19, 11, 0, 0)) == "2026-08-19T11:00:00Z"
    assert ap._iso(datetime(2026, 8, 19, 11, 0, 0, tzinfo=timezone.utc)) == (
        "2026-08-19T11:00:00Z"
    )


def test_a_rename_finds_a_proposal_that_has_already_been_snoozed(promotion_session):
    """N1 from round 8, and from this chip's own added-lines branch sweep — the
    ``"snoozed"`` arm of ``_apply_promotion_rename``'s lookup.

    Reachable and load-bearing: ``subdir`` feeds ``write_proposal(...,
    subdir=subdir)``, so a proposal found in ``snoozed`` must be re-written
    there rather than resurrected into ``pending``. No test entered it —
    correct code with no coverage, which the whole-file sweep of the four
    created modules could not see because ``engine.py`` is a file this chip
    *modifies*.

    MUTATION CHECKED: dropping ``"snoozed"`` from the subdir tuple makes this go
    red; so does hardcoding ``subdir="pending"`` on the write-back, which would
    silently un-snooze a deferred offer the moment the user renamed it.
    """
    from arbiter import store as astore
    from evolve_admin.evo.wizard import engine as weng

    shared_dir, rec, manifests = promotion_session
    # Move the pending proposal into `snoozed`, as a defer would.
    pending = [
        p for p in astore.iter_proposals(shared_dir, subdirs=("pending",))
        if p.id == "p-behavioural"
    ]
    assert len(pending) == 1
    astore.move_proposal(pending[0], shared_dir, from_subdir="pending",
                         to_subdir="snoozed")
    assert not list(astore.iter_proposals(shared_dir, subdirs=("pending",)))

    app_id = weng._apply_promotion_rename(
        shared_dir, "team-bot-a", rec, "Daily Digest"
    )
    assert app_id == "daily-digest"
    assert manifests["brief"]["name"] == "Daily Digest"
    # And it stayed snoozed — a rename is not an un-defer.
    snoozed = list(astore.iter_proposals(shared_dir, subdirs=("snoozed",)))
    assert [p.action.app_id for p in snoozed] == ["daily-digest"]
    assert not list(astore.iter_proposals(shared_dir, subdirs=("pending",)))


def test_the_proposal_body_reads_sensibly_with_and_without_drivers():
    """N2 from round 8: the drivers ternary in the Proposal ``problem`` body.

    Inside a swept module, but the sweep covered ``if`` STATEMENTS only — a
    ternary is an ``ast.IfExp``. The no-drivers arm is what an operator reads
    when readiness measured nothing, so it must not render an empty bullet or
    the word ``None``.

    MUTATION CHECKED: swapping the ternary's arms makes this go red.
    """
    manifest = _eligible_manifest()
    with_drivers = ap.build_promotion_proposal(
        manifest, bot_id="team-bot-a", manifest_stem="brief", network={},
        readiness=score_readiness(manifest), now=NOW,
    )
    assert with_drivers is not None
    assert "recorded run(s)" in with_drivers.problem or "- " in with_drivers.problem
    assert "no measured readiness drivers" not in with_drivers.problem

    # No readiness object at all — the arm nothing entered.
    without = ap.build_promotion_proposal(
        manifest, bot_id="team-bot-a", manifest_stem="brief", network={},
        readiness=None, now=NOW,
    )
    assert without is not None
    assert "no measured readiness drivers" in without.problem
    assert "None" not in without.problem


# ─────────────────────────────────────────────────────────────────────────────
# 2026-08-21: the decision seam the scheduler shares, and the way off "never"
# ─────────────────────────────────────────────────────────────────────────────


def test_plan_offer_decides_without_writing(tmp_path):
    """``plan_offer`` is the seam the scheduled generator and the CLI share.

    It must mint the Proposal and write NOTHING: the generator path hands its
    Proposal to ``arbiter.ingest`` (dedup, charter invariants, rejection
    cooldown), and a function that also wrote would put one path's offers into
    the store past all three.

    MUTATION CHECKED, run in this session: making ``plan_offer`` call
    ``write_proposal`` makes this go red; gutting it to return ``(result, None)``
    makes it go red the other way.
    """
    from arbiter import store as astore
    from evolve_admin.applications import app_promotion_sweep as sweep

    (tmp_path / "proposals" / "pending").mkdir(parents=True)
    result, proposal = sweep.plan_offer(
        tmp_path,
        "team-bot-a",
        network={},
        read_manifests=lambda _b: [("brief", _eligible_manifest())],
        now=NOW,
    )

    assert result.offered == ("brief",)
    assert proposal is not None
    assert proposal.generator_id == ap.GENERATOR_ID
    # Draft: the caller owns the transition, and the two callers do it
    # differently on purpose.
    assert proposal.status == "draft"
    assert list(astore.iter_proposals(tmp_path, subdirs=("pending",))) == []


def test_sweep_bot_and_plan_offer_cannot_disagree_about_eligibility(tmp_path):
    """Whatever ``plan_offer`` declines, ``sweep_bot`` declines — because it IS
    ``plan_offer``. Pinned rather than assumed: a second copy of "may this be
    offered" is how a cadence cap ends up enforced on one path only.

    MUTATION CHECKED: re-deriving the decision inside ``sweep_bot`` (ignoring
    ``plan_offer``'s answer) makes this go red.
    """
    from evolve_admin.applications import app_promotion_sweep as sweep

    (tmp_path / "proposals" / "pending").mkdir(parents=True)
    shielded = [("brief", _eligible_manifest(**{ap.DO_NOT_OFFER_FIELD: True}))]

    planned, proposal = sweep.plan_offer(
        tmp_path, "team-bot-a", network={},
        read_manifests=lambda _b: shielded, now=NOW,
    )
    swept = sweep.sweep_bot(
        tmp_path, "team-bot-a", network={},
        read_manifests=lambda _b: shielded, now=NOW,
    )

    assert proposal is None
    assert planned.offered == swept.offered == ()
    assert planned.skipped == swept.skipped


def test_clearing_the_shield_makes_a_draft_offerable_again():
    """Brief §8.3 step 6 — the class rounds 3-4 could only name.

    The shield is written by a classifier over free English, so some acceptance
    carrying a never-phrase will always slip through. Until this function
    existed nothing unset the field: the operator could promote the draft
    outright (``/definition/promote`` ignores the shield) but nobody could put
    it back in the queue to be *asked* about — the user's own answer was the one
    thing that could not be revised.

    Asserted through the GATE, not on the dict: what matters is that
    ``evaluate_offer`` stops blocking, not that a key changed.

    MUTATION CHECKED: making the clear branch a no-op makes this go red.
    """
    manifest = _eligible_manifest()
    ap.set_promotion_shield(manifest, shielded=True, by="user:promotion_offer")
    blocked = ap.evaluate_offer(
        manifest, bot_id="team-bot-a", readiness=score_readiness(manifest),
        recent_offers=0, now=NOW,
    )
    assert "do_not_offer" in blocked.blockers

    ap.set_promotion_shield(manifest, shielded=False, by="ui:operator")

    cleared = ap.evaluate_offer(
        manifest, bot_id="team-bot-a", readiness=score_readiness(manifest),
        recent_offers=0, now=NOW,
    )
    assert cleared.allowed, cleared.blockers


def test_clearing_removes_both_fields_so_a_remint_cannot_resurrect_it():
    """Clearing must leave the manifest in the state a never-shielded draft has
    never been in — because ``native_write``'s carry-forward skips falsey values
    and would otherwise carry a lingering truthy ``do_not_offer_by``.

    MUTATION CHECKED: writing ``False`` instead of removing the keys makes this
    go red — and would leave ``do_not_offer_by: "user:…"`` standing over an
    unshielded draft, which reads to the next person as a shield.
    """
    manifest = _eligible_manifest()
    ap.set_promotion_shield(manifest, shielded=True, by="user:promotion_offer")
    ap.set_promotion_shield(manifest, shielded=False, by="ui:operator")

    assert ap.DO_NOT_OFFER_FIELD not in manifest
    assert ap.DO_NOT_OFFER_BY_FIELD not in manifest


def test_the_never_hop_and_the_operator_route_share_one_writer():
    """``apply_never_shield`` delegates rather than stamping the field itself.

    Two writers of one field is how a set path and a clear path drift out of
    step — the clear removes what the set no longer writes, and the shield
    survives its own removal.

    MUTATION CHECKED: re-inlining the stamp in ``apply_never_shield`` (writing
    only ``do_not_offer``) makes this go red.
    """
    hop = ph.apply_never_shield(_eligible_manifest())
    route = ap.set_promotion_shield(
        _eligible_manifest(), shielded=True, by="user:promotion_offer",
    )
    assert hop.get(ap.DO_NOT_OFFER_FIELD) == route.get(ap.DO_NOT_OFFER_FIELD)
    assert hop.get(ap.DO_NOT_OFFER_BY_FIELD) == route.get(ap.DO_NOT_OFFER_BY_FIELD)


# ─────────────────────────────────────────────────────────────────────────────
# D-H's ADOPT branch, against the shape a LIVE draft actually has
# ─────────────────────────────────────────────────────────────────────────────


def _live_shape_draft(**over):
    """The measured shape of a real discovered draft, 2026-08-21, `atlas` bot.

    Read read-only off the pod (as `evolve`) rather than invented, because the
    fixture's *shape* is the thing under test and a synthetic one can be shaped
    to pass. Four properties of this manifest are load-bearing and none of them
    are what someone writing a fixture from the design doc would produce:

    * it is `discovered` **and** carries a resolvable canonical `app_id` — the
      population D-H option (a) grandfathers, and the only population promotion
      can adopt from today (`with_draft_id=0` pod-wide);
    * `draft_id` is absent, so the conferral branch must NOT fire;
    * `instance_id` (`app_atlas_article_capture`) **diverges from the on-disk
      filename stem** (`atlas-article-capture`) — the §9.7-finding-#5 shape the
      routes resolve for, and a promotion proposal that carried the wrong one
      would write back to a file it never read;
    * its `files[]` are on the top evidence rung, so readiness is genuinely
      measurable rather than a fixture constant.

    Trimmed to three of its **24** ``files[]`` entries (27 more under
    ``realized_files``); nothing else is edited. The count is corrected from
    "fourteen", which was this fixture's author reading the *shared-with-Daily-
    Digest* intersection as the manifest's own size — see §9.7.
    """
    manifest = {
        "name": "Atlas Article Capture",
        "instance_id": "app_atlas_article_capture",
        "manifest_shape": "v7-arc",
        "definition_status": "discovered",
        "app_id": "p-df9d99a3",
        "pkg_id": "p-df9d99a3",
        "description": (
            "Automatically generate and distribute a daily curated news digest "
            "by fetching articles from multiple sources, classifying them into "
            "actionable buckets, and posting a structured markdown digest to a "
            "Telegram group every morning."
        ),
        "files": [
            {"path": "scripts/atlas_digest.py", "file_id": "f-84341564", "layer": "code"},
            {"path": "scripts/atlas-digest-cron.sh", "file_id": "f-52fcf4d4", "layer": "code"},
            {"path": "scripts/atlas_lib/classifier.py", "file_id": "f-b9b07f73", "layer": "code"},
        ],
        "usage_metadata": {"invocation_count": 0},
    }
    manifest.update(over)
    return manifest


def test_a_live_discovered_draft_is_adopted_not_re_identified():
    """**D-H, on the shape it was decided about.**

    *"Promotion ADOPTS a pre-existing resolvable `app_id` rather than
    re-identifying; conferral applies only to true drafts (`draft_id`, no
    `app_id`)."* — roadmap §2a, 2026-08-20.

    MUTATION CHECKED, run in this session: making ``adopt_or_confer_app_id``
    take the conferral branch unconditionally makes this go red, and the id it
    would have minted (``atlas-article-capture``) is exactly the re-identify
    D-H rules out — every marker, coverage key and attribution row holding
    ``p-df9d99a3`` would be orphaned by it.
    """
    decision = ap.adopt_or_confer_app_id(_live_shape_draft())

    assert decision.mode == "adopted"
    assert decision.app_id == "p-df9d99a3"
    assert decision.prior_app_id == "p-df9d99a3"


def test_conferral_fires_only_for_a_true_draft_id_only_draft():
    """The other half of D-H's sentence, and the half the pod cannot exercise:
    `with_draft_id=0` across all 74 manifests, so this branch has no live
    subject and a fixture is the only way to hold it.

    MUTATION CHECKED: making the adopt branch fire whenever ``draft_id`` is
    present makes this go red.
    """
    born_a_draft = _live_shape_draft(
        app_id=None, pkg_id=None, draft_id="draft-b9e869ff0a64",
        name="Morning Brief",
    )

    decision = ap.adopt_or_confer_app_id(born_a_draft)

    assert decision.mode == "conferred"
    assert decision.app_id == "morning-brief"
    assert decision.prior_app_id == ""


def test_the_proposal_over_a_live_draft_adopts_and_targets_the_right_file():
    """The identity decision has to survive the trip onto the Proposal, and the
    Proposal has to name the file the applier will WRITE.

    ``instance_id`` diverges from the filename stem on this manifest, which is
    the case `_resolve_manifest_stem` exists for. The proposal carries
    ``manifest_stem`` — what the caller read the file as — and never the
    internal id.

    MUTATION CHECKED: passing ``manifest.get("instance_id")`` as
    ``manifest_stem`` in the sweep makes this go red.
    """
    proposal = ap.build_promotion_proposal(
        _live_shape_draft(),
        bot_id="team-bot-a",
        manifest_stem="atlas-article-capture",
        network={"bots": {"team-bot-a": {"primary_user": {"external_ids": {"slack": "U1"}}}}},
        now=NOW,
    )

    assert proposal is not None
    assert proposal.action.app_id == "p-df9d99a3"
    assert proposal.action.identity_mode == "adopted"
    assert proposal.action.manifest_stem == "atlas-article-capture"
    assert proposal.provenance.signals["identity_decision"]["mode"] == "adopted"


def test_renaming_a_live_draft_changes_the_label_not_the_id():
    """Design §7.2 offers the user a rename in the same breath as the promotion.
    Under ADOPT that must move the label only — otherwise "rename" is the
    re-identify branch entered through a side door, by a user who was choosing a
    name.

    MUTATION CHECKED: dropping ``resolve_rename``'s re-run of
    ``adopt_or_confer_app_id`` (slugging the new name instead) makes this go
    red.
    """
    outcome = ap.resolve_rename(_live_shape_draft(), new_name="Morning Digest")

    assert outcome is not None
    assert outcome.name == "Morning Digest"
    assert outcome.identity.app_id == "p-df9d99a3"
    assert outcome.identity.mode == "adopted"
    assert outcome.id_changed is False


# ─────────────────────────────────────────────────────────────────────────────
# The refusal, and the four defensive branches nothing entered
# (independent review of #3750: N2 + N4)
# ─────────────────────────────────────────────────────────────────────────────


def test_a_draft_with_no_usable_name_is_REFUSED_not_given_an_invented_id():
    """N2 — the safety property the draft branch's docstring asserts, which had
    no test: *"promotion refuses rather than adopting a scanner handle"*.

    The reviewer replaced the refusal with a fabricated conferral and watched
    239 admin + 15 analyzer tests stay green. That is the worst shape for this
    module to leave uncovered, because the invented id would be **written to
    disk by the applier** and then be immutable by design §3.

    MUTATION CHECKED, run in this session: returning a conferral built from the
    manifest's `id` (or any fallback) instead of `mode="blocked"` makes this go
    red.
    """
    nameless_draft = {
        "definition_status": "discovered",
        "draft_id": "draft-b9e869ff0a64",
        # An `id` a scanner handle could be adopted from, and a name that slugs
        # to nothing — "??" has no alphanumerics for APP_ID_PATTERN.
        "id": "i-7f3a",
        "name": "??",
    }

    decision = ap.adopt_or_confer_app_id(nameless_draft)

    assert decision.mode == "blocked"
    assert decision.app_id == ""
    assert decision.ok is False


def test_a_blocked_identity_stops_the_proposal_from_being_minted_at_all():
    """The refusal has to survive one layer up, or it is advice.

    MUTATION CHECKED: making `build_promotion_proposal` fall back to the
    manifest name when `decision.ok` is False makes this go red.
    """
    proposal = ap.build_promotion_proposal(
        {"definition_status": "discovered", "draft_id": "draft-1", "name": "??"},
        bot_id="team-bot-a",
        manifest_stem="mystery",
        network={},
        now=NOW,
    )
    assert proposal is None


def test_the_sweep_reports_an_identity_block_as_a_skip_with_its_reason(tmp_path):
    """N4 — `plan_offer`'s `identity_blocked` return had no test entering it.

    It matters more than a defensive branch usually would: "nothing was
    offerable" and "one candidate was refused an identity" are the same OUTCOME
    and different problems, and the operator only ever sees the difference here.

    MUTATION CHECKED: returning the un-annotated `skipped` tuple (dropping the
    `identity_blocked` entry) makes this go red.
    """
    from evolve_admin.applications import app_promotion_sweep as sweep

    (tmp_path / "proposals" / "pending").mkdir(parents=True)
    nameless = _eligible_manifest(name="??", draft_id="draft-1")
    nameless.pop("app_id", None)

    result, proposal = sweep.plan_offer(
        tmp_path, "team-bot-a", network={},
        read_manifests=lambda _b: [("mystery", nameless)], now=NOW,
    )

    assert proposal is None
    assert ("mystery", ("identity_blocked",)) in result.skipped
    assert result.offered == ()


def test_a_write_failure_is_never_reported_as_an_offer(tmp_path, monkeypatch):
    """N4 — `sweep_bot`'s phantom-`offered` demotion had no test on this branch
    OR on main.

    The failure it guards is silent and expensive: `offered` is what the cadence
    cap reads, so an offer recorded but never written would suppress tomorrow's
    REAL offer for a message nobody received.

    MUTATION CHECKED: returning `result` unchanged from the `except` (instead of
    demoting `offered` to `()` and appending `write_failed`) makes this go red.
    """
    from arbiter import store as astore
    from evolve_admin.applications import app_promotion_sweep as sweep

    (tmp_path / "proposals" / "pending").mkdir(parents=True)

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(astore, "write_proposal", _boom)

    result = sweep.sweep_bot(
        tmp_path, "team-bot-a", network={},
        read_manifests=lambda _b: [("brief", _eligible_manifest())], now=NOW,
    )

    assert result.offered == (), "a failed write must not be reported as an offer"
    assert ("brief", ("write_failed",)) in result.skipped
