"""ALPHA-3b — promotion ADOPTS across bots (operator decision D-I, half 2).

D-I decided that durable cross-bot identity converges **at the moment a human
vouches**. ALPHA-3a bought time with a reversible read-time CLAIM that two ids
are one app; this half stops the second id from being minted at all: promoting
a draft of an app a sibling bot already has DEFINED adopts that app's
``app_id`` instead of conferring a new one.

The fixture throughout is the audit's own subject (``audit-alpha-journey-2026-08``
B3): two "Morning Brief"s on two bots, which the pod-first Apps page rendered as
two rows each claiming "on 1 bot".

What each section pins:

  * the adoption itself, and that its reason names the source bot (the offer
    lands in the admin queue, where "why does this app have that id" has to be
    answerable without re-running anything)
  * every way it must NOT fire — no equivalent, a DRAFT equivalent, an
    equivalent on the promoting bot's own manifest list, evidence below
    ALPHA-3a's threshold
  * that D-H is untouched: a manifest with its own resolvable id still adopts
    THAT id, pod facts or no pod facts
  * that the rename hop carries the same facts, so a rename cannot silently
    move an id the offer adopted
  * that the wizard's confirm text stops claiming "the name becomes its
    permanent id" when the name is about to become a label instead
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import app_promotion as ap  # noqa: E402
from evolve_admin.applications import app_grouping as ag  # noqa: E402
from evolve_admin.applications import pod_apps  # noqa: E402

NOW = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)

#: The id the audit measured on the first bot's Morning Brief.
SIBLING_APP_ID = "p-befb87e0"

_BRIEF_FILES = [
    "/Users/personal-bot/.openclaw/workspace/apps/morning-brief/run.py",
    "/Users/personal-bot/.openclaw/workspace/apps/morning-brief/sources.py",
]


def _defined_brief(bot: str = "personal-bot", app_id: str = SIBLING_APP_ID) -> dict:
    """One bot's Morning Brief, already promoted. Facts, not a manifest."""
    return {
        app_id: {
            "name": "Morning Brief",
            "evidence": ag.evidence_signature(_BRIEF_FILES),
            "bots": {bot},
            "spec_version": 1,
        }
    }


def _brief_draft(**over) -> dict:
    """The SECOND bot's discovered draft of the same app.

    A true draft in D-H's sense — ``draft_id``, no ``app_id`` — so without
    ALPHA-3b it takes the conferral branch and mints ``morning-brief``.
    The evidence paths are that bot's own home, which is exactly why
    ``app_grouping`` compares the last two segments.
    """
    manifest = {
        "name": "Morning Brief",
        "definition_status": "discovered",
        "draft_id": "draft-b9e869ff0a64",
        "instance_id": "morning-brief-2",
        "files": [
            {"path": "/Users/team-bot-a/.openclaw/workspace/apps/morning-brief/run.py",
             "layer": "code"},
            {"path": "/Users/team-bot-a/.openclaw/workspace/apps/morning-brief/sources.py",
             "layer": "code"},
        ],
        "usage_metadata": {"invocation_count": 12},
    }
    manifest.update(over)
    return manifest


# ─────────────────────────────────────────────────────────────────────────────
# The adoption
# ─────────────────────────────────────────────────────────────────────────────


def test_the_second_bots_draft_adopts_the_first_bots_app_id():
    """D-I half 2, on the audit's own two Morning Briefs.

    MUTATION CHECKED: deleting the ``cross_bot_adoption`` call from
    ``adopt_or_confer_app_id``'s draft branch makes this go red, and the id it
    confers instead (``morning-brief``) is the second id for one app that
    ALPHA-3a's grouping exists to paper over.
    """
    decision = ap.adopt_or_confer_app_id(
        _brief_draft(),
        bot_id="team-bot-a",
        defined_apps=_defined_brief(),
    )

    assert decision.mode == "adopted"
    assert decision.app_id == SIBLING_APP_ID
    assert decision.adopted_from_bot == "personal-bot"
    assert decision.ok is True


def test_the_reason_names_the_source_bot_so_the_admin_queue_can_explain_it():
    """The queue shows a proposal, not a debugger. An operator reading "this
    app will be called p-befb87e0" needs the *why* on the record — otherwise
    the only way to answer it is to re-run the sweep against a pod that has
    since changed.

    MUTATION CHECKED: dropping the source bot from the reason string makes this
    go red.
    """
    decision = ap.adopt_or_confer_app_id(
        _brief_draft(), bot_id="team-bot-a", defined_apps=_defined_brief()
    )

    assert "personal-bot" in decision.reason
    assert SIBLING_APP_ID in decision.reason
    # And it travels onto the Proposal, where the queue reads it.
    proposal = ap.build_promotion_proposal(
        _brief_draft(),
        bot_id="team-bot-a",
        manifest_stem="morning-brief",
        network={},
        defined_apps=_defined_brief(),
        now=NOW,
    )
    assert proposal is not None
    assert proposal.action.app_id == SIBLING_APP_ID
    assert proposal.action.identity_mode == "adopted"
    signals = proposal.provenance.signals["identity_decision"]
    assert signals["mode"] == "adopted"
    assert signals["adopted_from_bot"] == "personal-bot"
    assert "personal-bot" in signals["reason"]


def test_a_manifest_with_no_identity_at_all_also_adopts_rather_than_conferring():
    """The OTHER conferral site. ``adopt_or_confer_app_id`` confers in two
    places — the true-draft branch and the tail, where nothing resolved — and a
    check wired into only one of them would leave a manifest with no ids at all
    minting a duplicate id for an app the pod already has.

    MUTATION CHECKED: removing the ``cross_bot_adoption`` call from the tail
    branch makes this go red with ``mode == "conferred"``.
    """
    no_identity = {
        "name": "Morning Brief",
        "definition_status": "discovered",
        "files": [{"path": p} for p in _BRIEF_FILES],
    }

    decision = ap.adopt_or_confer_app_id(
        no_identity, bot_id="team-bot-a", defined_apps=_defined_brief()
    )

    assert decision.mode == "adopted"
    assert decision.app_id == SIBLING_APP_ID


def test_more_than_one_equivalent_adopts_the_id_the_page_leads_with():
    """Two sibling bots, two ids, one app — the state ALPHA-3a groups into one
    row. Promotion adopts that row's LEAD (``app_grouping.lead_of``: most bots,
    then highest spec_version, then smallest id), so the id it converges on is
    the id the operator is already looking at rather than a coin flip.

    MUTATION CHECKED: picking ``sorted(matched)[0]`` instead of the lead makes
    this go red — ``p-049bf7ab`` sorts first but covers fewer bots.
    """
    pod = {
        "p-049bf7ab": {
            "name": "Morning Brief",
            "evidence": ag.evidence_signature(_BRIEF_FILES),
            "bots": {"personal-bot"},
            "spec_version": 1,
        },
        SIBLING_APP_ID: {
            "name": "Morning Brief",
            "evidence": ag.evidence_signature(_BRIEF_FILES),
            "bots": {"ops-bot", "family-bot"},
            "spec_version": 1,
        },
    }

    decision = ap.adopt_or_confer_app_id(
        _brief_draft(), bot_id="team-bot-a", defined_apps=pod
    )

    assert decision.app_id == SIBLING_APP_ID
    assert "p-049bf7ab" in decision.reason  # the other match is on the record


# ─────────────────────────────────────────────────────────────────────────────
# Every way it must NOT fire
# ─────────────────────────────────────────────────────────────────────────────


def test_no_equivalent_confers_exactly_as_before():
    """The unchanged half of the sentence. A pod with a different app in it
    must leave conferral byte-identical."""
    pod = {
        "p-otherapp": {
            "name": "Expense Tracker",
            "evidence": ag.evidence_signature(["expenses/track.py"]),
            "bots": {"personal-bot"},
            "spec_version": 1,
        }
    }

    with_pod = ap.adopt_or_confer_app_id(
        _brief_draft(), bot_id="team-bot-a", defined_apps=pod
    )
    without_pod = ap.adopt_or_confer_app_id(_brief_draft(), bot_id="team-bot-a")

    assert with_pod.mode == "conferred"
    assert with_pod.app_id == "morning-brief"
    assert with_pod == without_pod


def test_a_draft_never_adopts_from_another_bots_DRAFT():
    """D-I's own words: identity converges when a human VOUCHES. A draft
    carries no vouch, so spreading its id would spread an identity nobody has
    approved — and would do it silently, on two manifests at once.

    The rule is enforced by what ``defined_app_facts`` collects, so this test
    goes through that reader rather than hand-building the facts: a fixture
    that just omits the draft would prove nothing about the filter.

    MUTATION CHECKED: dropping the ``definition_status == DEFINED`` filter in
    ``pod_apps.defined_app_facts`` makes this go red.
    """
    sibling_draft = {
        "name": "Morning Brief",
        "definition_status": "discovered",
        "draft_id": "draft-11111111",
        "app_id": "p-notvouched",
        "files": [{"path": p} for p in _BRIEF_FILES],
    }
    facts = pod_apps.defined_app_facts(
        ["personal-bot"],
        read_manifests=lambda bot: [("morning-brief", sibling_draft)],
        shared_dir=Path("/nonexistent-shared-dir"),
    )
    assert facts == {}

    decision = ap.adopt_or_confer_app_id(
        _brief_draft(), bot_id="team-bot-a", defined_apps=facts
    )
    assert decision.mode == "conferred"
    assert decision.app_id == "morning-brief"


def test_an_equivalent_on_the_promoting_bot_itself_is_not_adopted_from():
    """ALPHA-3a's no-shared-bot clause, doing its second job.

    Two records on ONE bot are a duplicate, which is the deduplication work.
    Adopting there would merge two things the operator can still see apart —
    and it would do it on the strength of a claim that was written to answer a
    different question.

    MUTATION CHECKED: dropping the ``bots`` intersection from
    ``app_grouping.equivalent`` makes this go red.
    """
    decision = ap.adopt_or_confer_app_id(
        _brief_draft(),
        bot_id="team-bot-a",
        defined_apps=_defined_brief(bot="team-bot-a"),
    )

    assert decision.mode == "conferred"
    assert decision.app_id == "morning-brief"


def test_a_bot_that_already_carries_the_sibling_id_does_not_adopt_it_twice(tmp_path):
    """The invariant this could most plausibly break: ONE manifest per app per
    bot.

    Bot A has Morning Brief defined; bot B has a draft of it AND, separately, a
    manifest already carrying A's id (a gallery install, a copied manifest, a
    previous promotion). Adopting again would leave bot B with two manifests
    resolving to one app_id — a row whose per-bot panels collide, and the
    duplicate-record condition that belongs to deduplication rather than here.

    Nothing special guards it: ``defined_app_facts`` collects the bots of EVERY
    install of an app id, defined or not, so the id's ``bots`` set already
    contains the promoting bot and ALPHA-3a's no-shared-bot clause refuses. The
    test exists because that is a property of how the facts are built, and a
    later "optimization" that collected only the defined installs' bots would
    take it away silently.
    """
    defined = {
        "name": "Morning Brief",
        "definition_status": "defined",
        "app_id": SIBLING_APP_ID,
        "files": [{"path": p} for p in _BRIEF_FILES],
    }
    already_here = dict(defined, definition_status="discovered")
    manifests = {
        "personal-bot": [("morning-brief", defined)],
        "team-bot-a": [("morning-brief", already_here),
                       ("morning-brief-2", _brief_draft())],
    }

    facts = pod_apps.defined_app_facts(
        ["personal-bot", "team-bot-a"],
        read_manifests=lambda bot: manifests.get(bot, []),
        shared_dir=tmp_path,
    )
    assert facts[SIBLING_APP_ID]["bots"] == {"personal-bot", "team-bot-a"}

    decision = ap.adopt_or_confer_app_id(
        _brief_draft(), bot_id="team-bot-a", defined_apps=facts
    )
    assert decision.mode == "conferred"


def test_evidence_below_alpha_3as_threshold_does_not_adopt():
    """The threshold is ALPHA-3a's, imported, not a second number.

    A same-named app whose files do not overlap is a different app — the case
    the name gate alone would merge. Promotion must be at least as strict as
    the page, because its answer is durable and the page's is withdrawable.
    """
    unrelated = {
        SIBLING_APP_ID: {
            "name": "Morning Brief",
            "evidence": ag.evidence_signature(["digest/other.py", "digest/more.py"]),
            "bots": {"personal-bot"},
            "spec_version": 1,
        }
    }

    decision = ap.adopt_or_confer_app_id(
        _brief_draft(), bot_id="team-bot-a", defined_apps=unrelated
    )

    assert decision.mode == "conferred"


def test_the_claim_promotion_uses_IS_the_claim_the_page_groups_with():
    """One definition or the two halves of D-I eventually disagree about an id
    that is by then durable. ``cluster_app_ids`` and ``cross_bot_adoption``
    both route through ``app_grouping.equivalent``.

    The test pins the CALL rather than the answer: it moves ALPHA-3a's
    threshold and watches promotion move with it.

    MUTATION CHECKED: re-stating the comparison inside ``cross_bot_adoption``
    with its own copy of the 0.5 makes this go red — the second half stays
    ``conferred`` after the threshold drops.
    """
    half_overlapping = {
        SIBLING_APP_ID: {
            "name": "Morning Brief",
            # 1 shared of 3 union → 0.33: under 0.5, over 0.25.
            "evidence": ag.evidence_signature(
                ["morning-brief/run.py", "morning-brief/extra.py"]
            ),
            "bots": {"personal-bot"},
            "spec_version": 1,
        }
    }
    draft = _brief_draft()

    assert ap.adopt_or_confer_app_id(
        draft, bot_id="team-bot-a", defined_apps=half_overlapping
    ).mode == "conferred"

    original = ag.EVIDENCE_SIMILARITY_THRESHOLD
    try:
        ag.EVIDENCE_SIMILARITY_THRESHOLD = 0.25
        assert ap.adopt_or_confer_app_id(
            draft, bot_id="team-bot-a", defined_apps=half_overlapping
        ).mode == "adopted"
    finally:
        ag.EVIDENCE_SIMILARITY_THRESHOLD = original


# ─────────────────────────────────────────────────────────────────────────────
# D-H is not disturbed
# ─────────────────────────────────────────────────────────────────────────────


def test_a_backfilled_manifest_still_adopts_its_OWN_id_even_beside_a_pod_match():
    """D-H, byte-unchanged. The 74 backfilled manifests carry an ``app_id`` and
    no ``draft_id``; promotion adopts what they already have. ALPHA-3b must not
    reach them at all — re-pointing one at a sibling's id would be exactly the
    re-identification D-H closed, arriving through a new door.

    MUTATION CHECKED: moving the ``cross_bot_adoption`` call above the
    resolvable-id branch makes this go red.
    """
    backfilled = {
        "name": "Morning Brief",
        "definition_status": "discovered",
        "app_id": "p-df9d99a3",
        "pkg_id": "p-df9d99a3",
        "files": [{"path": p} for p in _BRIEF_FILES],
    }

    decision = ap.adopt_or_confer_app_id(
        backfilled, bot_id="team-bot-a", defined_apps=_defined_brief()
    )

    assert decision.mode == "adopted"
    assert decision.app_id == "p-df9d99a3"
    assert decision.prior_app_id == "p-df9d99a3"
    assert decision.adopted_from_bot == ""
    # And with no pod facts at all, the identical answer.
    assert ap.adopt_or_confer_app_id(backfilled) == decision


def test_no_pod_facts_means_no_behaviour_change_anywhere():
    """Every pre-ALPHA-3b caller passes neither argument. That path has to be
    the function D-H shipped, not a version of it that happens to agree."""
    for manifest in (_brief_draft(), {"name": "Morning Brief"}, {}):
        assert ap.adopt_or_confer_app_id(manifest) == ap.adopt_or_confer_app_id(
            manifest, bot_id="team-bot-a", defined_apps={}
        )


# ─────────────────────────────────────────────────────────────────────────────
# The rename hop — the id must not move behind the user
# ─────────────────────────────────────────────────────────────────────────────


def test_renaming_away_from_the_siblings_name_ENDS_the_adoption():
    """The adoption is a claim about the app, and the claim's first clause is
    the name. A user who renames the draft to something the sibling app is not
    called has said these are different things — so the id goes back to being
    conferred, and ``id_changed`` says so rather than keeping an id whose
    justification has gone.

    This is also why the echo (below) resolves the identity with the NEW name
    instead of reporting the mode the offer was minted with.
    """
    outcome = ap.resolve_rename(
        _brief_draft(),
        new_name="Morning Brief AM",
        current_app_id=SIBLING_APP_ID,
        bot_id="team-bot-a",
        defined_apps=_defined_brief(),
    )

    assert outcome is not None
    assert outcome.name == "Morning Brief AM"
    assert outcome.identity.mode == "conferred"
    assert outcome.identity.app_id == "morning-brief-am"
    assert outcome.id_changed is True


def test_renaming_to_the_same_name_keeps_the_adoption():
    """The other side of the same coin: a cosmetic rename ("morning brief" →
    "Morning Brief") must not disturb an adopted identity.

    ``resolve_rename`` re-runs the identity decision, so it must re-run it
    against the SAME pod — or the offer's adoption is undone by the very
    confirmation turn that exists to protect the user's id from moving.

    MUTATION CHECKED: dropping ``bot_id`` / ``defined_apps`` from
    ``resolve_rename``'s call into ``adopt_or_confer_app_id`` makes this go red
    — the id becomes a freshly conferred ``morning-brief``.
    """
    outcome = ap.resolve_rename(
        _brief_draft(),
        new_name="morning brief",
        current_app_id=SIBLING_APP_ID,
        bot_id="team-bot-a",
        defined_apps=_defined_brief(),
    )

    assert outcome is not None
    assert outcome.identity.mode == "adopted"
    assert outcome.identity.app_id == SIBLING_APP_ID
    assert outcome.id_changed is False


# ─────────────────────────────────────────────────────────────────────────────
# The reader, and the sweep that pays for it
# ─────────────────────────────────────────────────────────────────────────────


def test_defined_app_facts_collects_the_defined_half_of_the_pod(tmp_path):
    """The facts the adoption is decided against are the ones the Apps page
    groups with — same ``_collect``, same ``_grouping_facts`` — filtered to
    defined. A separate assembly here would be a second answer to "what apps
    does this pod have"."""
    defined = {
        "name": "Morning Brief",
        "definition_status": "defined",
        "app_id": SIBLING_APP_ID,
        "files": [{"path": p} for p in _BRIEF_FILES],
    }
    manifests = {
        "personal-bot": [("morning-brief", defined)],
        "team-bot-a": [("morning-brief-2", _brief_draft())],
    }

    facts = pod_apps.defined_app_facts(
        ["personal-bot", "team-bot-a"],
        read_manifests=lambda bot: manifests.get(bot, []),
        shared_dir=tmp_path,
    )

    assert list(facts) == [SIBLING_APP_ID]
    assert facts[SIBLING_APP_ID]["bots"] == {"personal-bot"}
    assert facts[SIBLING_APP_ID]["name"] == "Morning Brief"


def test_an_unreadable_pod_confers_rather_than_guessing(tmp_path, monkeypatch):
    """``pod_defined_app_facts`` fails toward CONFERRING.

    A missed adoption costs a duplicate id that ALPHA-3a still collapses on the
    page and a later promotion can still converge. A made-up adoption hands one
    bot's app the identity of another's on the strength of a half-read pod.

    MUTATION CHECKED: letting the exception escape makes this go red by
    breaking the sweep entirely — which is the third, worst option.
    """
    from evolve_admin.applications import app_promotion_sweep as sweep

    def _boom(_bot):
        raise OSError("manifests unreadable")

    facts = sweep.pod_defined_app_facts(
        tmp_path,
        network={"bots": {"personal-bot": {}}},
        read_manifests=_boom,
    )
    # pod_apps swallows a per-bot read failure with a warning, so the pod comes
    # back EMPTY rather than partially true — either way, conferral.
    assert facts == {}
    assert ap.adopt_or_confer_app_id(
        _brief_draft(), bot_id="team-bot-a", defined_apps=facts
    ).mode == "conferred"


def test_the_sweep_actually_threads_the_pods_facts_into_the_offer(tmp_path):
    """The wire, not the function. ALPHA-3b is worth nothing if ``plan_offer``
    mints its proposal without the pod — the same "no production caller" defect
    the round-1 review of #3734 found one layer down.

    MUTATION CHECKED: removing ``defined_apps=`` from ``plan_offer``'s
    ``build_promotion_proposal`` call makes this go red with
    ``action.app_id == "morning-brief"``.
    """
    from evolve_admin.applications import app_promotion_sweep as sweep

    defined = {
        "name": "Morning Brief",
        "definition_status": "defined",
        "app_id": SIBLING_APP_ID,
        "files": [{"path": p} for p in _BRIEF_FILES],
    }
    manifests = {
        "personal-bot": [("morning-brief", defined)],
        "team-bot-a": [("morning-brief-2", _brief_draft())],
    }
    network = {
        "bots": {
            "personal-bot": {},
            "team-bot-a": {"primary_user": {"external_ids": {"slack": "U1"}}},
        }
    }

    result, proposal = sweep.plan_offer(
        tmp_path,
        "team-bot-a",
        network=network,
        read_manifests=lambda bot: manifests.get(bot, []),
        now=NOW,
    )

    assert result.offered == ("morning-brief-2",), result.to_dict()
    assert proposal is not None
    assert proposal.action.app_id == SIBLING_APP_ID
    assert proposal.action.identity_mode == "adopted"


# ─────────────────────────────────────────────────────────────────────────────
# What the user is told
# ─────────────────────────────────────────────────────────────────────────────


def test_the_rename_echo_stops_promising_a_permanent_id_when_it_is_adopting():
    """Design §7.2 gives a rename its own confirmation turn *because* the name
    was about to become a durable id. Under an adoption it is not — the id is
    the sibling's — so the echo must not ask the user to weigh a decision that
    is not on this turn.

    MUTATION CHECKED: deleting the ``identity_mode == "adopted"`` branch in
    ``prompts._promote_rename_block`` makes this go red.
    """
    from evolve_admin.evo.wizard.prompts import _promote_rename_block

    adopted = _promote_rename_block({
        "variant": "rename",
        "proposed_name": "Morning Brief",
        "identity_mode": "adopted",
        "adopted_from_bot": "personal-bot",
    })

    assert "Morning Brief" in adopted
    assert "personal-bot" in adopted
    assert "permanent id" not in adopted

    conferred = _promote_rename_block({
        "variant": "rename",
        "proposed_name": "Morning Brief",
        "identity_mode": "conferred",
    })
    assert "permanent id" in conferred
    # No answer at all renders what every draft rendered before ALPHA-3b.
    assert _promote_rename_block(
        {"variant": "rename", "proposed_name": "Morning Brief"}
    ) == conferred


# ─────────────────────────────────────────────────────────────────────────────
# The wizard hop — the echo has to be told, and the write has to hold
# ─────────────────────────────────────────────────────────────────────────────


import pytest  # noqa: E402


@pytest.fixture
def adopted_offer(tmp_path, monkeypatch):
    """A live pending offer whose identity was ADOPTED from a sibling bot.

    Real proposal store, real manifest read/write path (the two server helpers
    are patched onto an in-memory store, the shape ``test_app_promotion.py``'s
    ``promotion_session`` established) and the pod reader patched at its I/O
    seam so no test touches a bot home.
    """
    from arbiter import store as astore
    from evolve_admin.applications import app_promotion_sweep as sweep
    from evolve_admin import config as aconfig
    from evolve_admin.web import server as wsrv

    (tmp_path / "proposals" / "pending").mkdir(parents=True)
    manifest = _brief_draft()
    proposal = ap.build_promotion_proposal(
        manifest,
        bot_id="team-bot-a",
        manifest_stem="brief",
        network={},
        defined_apps=_defined_brief(),
        now=NOW,
        proposal_id="p-alpha3b",
    )
    assert proposal is not None
    assert proposal.action.app_id == SIBLING_APP_ID
    proposal.status = "pending"
    astore.write_proposal(proposal, tmp_path)

    manifests: dict[str, dict] = {"brief": dict(manifest)}
    monkeypatch.setattr(
        wsrv, "_read_manifest_as_bot",
        lambda bot_id, stem, user=None: (
            dict(manifests[stem]) if stem in manifests else None
        ),
    )
    monkeypatch.setattr(
        wsrv, "_write_manifest_as_bot",
        lambda bot_id, stem, data, user=None: (manifests.__setitem__(stem, dict(data)), True)[1],
    )
    monkeypatch.setattr(aconfig, "load_network", lambda *a, **k: {"bots": {}})
    seen: list = []

    def _facts(shared_dir, *, network, read_manifests=None):
        seen.append(Path(shared_dir))
        return _defined_brief()

    monkeypatch.setattr(sweep, "pod_defined_app_facts", _facts)

    rec = {
        "id": "rec_prop_p-alpha3b",
        "generator_id": ap.GENERATOR_ID,
        "source_ref": {"proposal_id": "p-alpha3b", "bot_id": "team-bot-a"},
    }
    return tmp_path, rec, manifests, seen


def test_the_echo_is_told_the_identity_the_confirm_hop_will_actually_land_on(
    adopted_offer,
):
    """The wire from the engine to the sentence.

    Without it ``prompts._promote_rename_block``'s adopted branch is a render
    nothing reaches — the "asserted but never executed" shape this repo keeps
    finding. The ctx is resolved with the NEW name, so it also tracks a rename
    that ends the adoption.

    MUTATION CHECKED: returning ``{}`` from ``engine._promotion_identity_ctx``
    makes this go red.
    """
    from evolve_admin.evo.wizard import engine as weng

    shared_dir, rec, _manifests, seen = adopted_offer

    kept = weng._promotion_identity_ctx(shared_dir, "team-bot-a", rec, "Morning Brief")
    assert kept == {"identity_mode": "adopted", "adopted_from_bot": "personal-bot"}
    # It read THIS pod, through the one reader the sweep uses.
    assert seen and seen[0] == shared_dir

    ended = weng._promotion_identity_ctx(shared_dir, "team-bot-a", rec, "Sunrise Notes")
    assert ended["identity_mode"] == "conferred"
    assert ended["adopted_from_bot"] == ""


def test_a_confirmed_rename_does_not_move_an_adopted_id(adopted_offer):
    """The write path. A user who restyles the label of an adopted app must end
    up with the label changed and the sibling's id intact — the offer said the
    app would join that one, and the confirmation turn must not quietly undo
    it.

    The rename that DOES end an adoption (a genuinely different name) is
    covered above; there the echo the user answered said so too, because the
    engine resolves the echo with the new name.

    MUTATION CHECKED: dropping ``defined_apps`` from
    ``engine._apply_promotion_rename``'s ``resolve_rename`` call makes this go
    red — the proposal is re-pointed at a freshly conferred ``morning-brief``.
    """
    from arbiter import store as astore
    from evolve_admin.evo.wizard import engine as weng

    shared_dir, rec, manifests, _seen = adopted_offer

    app_id = weng._apply_promotion_rename(
        shared_dir, "team-bot-a", rec, "morning brief"
    )

    assert manifests["brief"]["name"] == "morning brief"
    stored = [
        p for p in astore.iter_proposals(shared_dir, subdirs=("pending",))
        if p.id == "p-alpha3b"
    ]
    assert len(stored) == 1
    assert app_id == SIBLING_APP_ID
    assert stored[0].action.app_id == SIBLING_APP_ID
    assert stored[0].action.identity_mode == "adopted"


def test_the_rename_turn_actually_renders_the_honest_sentence(adopted_offer):
    """End to end through a real turn: engine → ctx → prompt → the words the
    user reads.

    The two halves are each tested above; this is the one that fails if they
    are never joined — an honest render nothing passes ``identity_mode`` to is
    the "asserted but never executed" shape, and it would leave the user told
    that a name they are choosing becomes a permanent id when it does not.

    MUTATION CHECKED: dropping the ``**_promotion_identity_ctx(...)`` spread
    from the CONFIRM handler's re-rename echo makes this go red.
    """
    from evolve_admin.evo.wizard import engine as weng
    from evolve_admin.evo.wizard import phases as wphases
    from evolve_admin.evo.wizard import promote_handlers as wph
    from evolve_admin.evo.wizard import state as wstate

    shared_dir, rec, _manifests, _seen = adopted_offer
    st = wstate.WizardState(
        user_key="slack:U1",
        bot_id="team-bot-a",
        audience="primary",
        status="in_progress",
        current_phase=wphases.PHASE_APP_PROMOTE_CONFIRM,
    )
    st.extracted["_pending_rec"] = rec
    st.extracted[wph.PROMOTE_OFFER_STATE_KEY] = {
        "rec_id": rec["id"], "answer": "rename", "proposed_name": "Sunrise Notes",
    }

    result = weng._handle_promote_confirm(
        shared_dir, st, "team-bot-a", "slack:U1", 'call it "Morning Brief"', {}
    )

    assert result.completed is False  # still choosing
    assert "Morning Brief" in result.system_append
    assert "personal-bot" in result.system_append
    assert "permanent id" not in result.system_append


def test_the_FIRST_rename_echo_is_honest_too(adopted_offer):
    """The other echo — the one on the offer turn, which is where most renames
    are heard. Same spread, same sentence, and it needs its own test because
    "both call sites were updated" is exactly the kind of claim that is true
    until someone edits one of them.

    MUTATION CHECKED: dropping the ``**_promotion_identity_ctx(...)`` spread
    from ``_handle_promotion_reply``'s rename branch makes this go red.
    """
    from evolve_admin.evo.wizard import engine as weng
    from evolve_admin.evo.wizard import phases as wphases
    from evolve_admin.evo.wizard import promote_handlers as wph
    from evolve_admin.evo.wizard import state as wstate

    shared_dir, rec, _manifests, _seen = adopted_offer
    route = wph.promote_route_for_rec(rec, 'call it "Morning Brief"')
    assert route.action == "rename"

    st = wstate.WizardState(
        user_key="slack:U1",
        bot_id="team-bot-a",
        audience="primary",
        status="in_progress",
        current_phase=wphases.PHASE_REC_PENDING,
    )
    st.extracted["_pending_rec"] = rec
    result = weng._handle_promotion_reply(
        shared_dir, st, "team-bot-a", "slack:U1", {}, rec=rec, route=route,
    )

    assert result.phase == wphases.PHASE_APP_PROMOTE_CONFIRM
    said = result.direct_send_message or result.system_append
    assert "personal-bot" in said
    assert "permanent id" not in said
