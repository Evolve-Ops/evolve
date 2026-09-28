"""tests/test_app_promotion_generator.py — the promotion offer's SCHEDULER.

Brief: ``internal/build-AL-1.7-promotion.md`` §8.3 step 4 — *"Wire the sweep to
something that runs it… A producer with no caller is not a producer."*

AL-1.7 shipped the whole promotion path and then left it unscheduled: the sweep
was runnable (``python3 -m …app_promotion_sweep``) and nothing ran it, so with
every other blocker cleared the chain would still have minted nothing, forever.
The brief named the fix it wanted — *"giving the sweep a charter is the natural
way to also solve step 4's scheduling"* — and this is that charter's tests.

**What has to be proved here is the WIRING, not the arithmetic.** The gate, the
ranking, the cadence cap and the Proposal are all tested at their own layer in
``packages/admin/tests/test_app_promotion.py``, against ~200 tests and eight
review rounds. Re-asserting any of that here would be theatre. What no test
anywhere could show before this file is that **something calls it on a
schedule** — so the load-bearing test is
``test_the_scheduled_path_actually_writes_a_promotion_proposal``, which enters
at ``generator_runner.run_one_generator`` (the runner's own entry point, same
registry / context-factory / ingest path the daily sweep uses) and asserts on a
Proposal that reached the store.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import generator_runner
from registry.charter_loader import load_charter_from_yaml

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "packages" / "admin"))

GEN_ID = "app_promotion"
BOT = "team_bot_a"
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
CHARTER_PATH = _REPO / "packages" / "analyzer" / "generators" / GEN_ID / "charter.yaml"


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    (s / "proposals" / "pending").mkdir(parents=True)
    return s


@pytest.fixture
def network() -> dict:
    return {
        "networkId": "test-net",
        "members": [BOT],
        "bots": {
            BOT: {
                "role": "member", "user": BOT,
                # A primary user the bot can actually reach — design §7.2's
                # audience turns on an external id, not on a display name.
                "primary_user": {"external_ids": {"slack": "U1"}},
            },
        },
    }


def _eligible_manifest(**over) -> dict:
    """A staged draft that clears the gate — a MECHANISM fixture, not a claim
    about the pod. On the operator's pod ``eligible_to_offer`` is 0 (brief
    §8.1, re-measured 2026-08-21), and nothing here moves a threshold to change
    that: this manifest earns its readiness with real evidence fields."""
    m = {
        "id": "morning-brief",
        "name": "Morning Brief",
        "bot_id": BOT,
        "definition_status": "discovered",
        "purpose": "Email, calendar and weather, every weekday morning.",
        "files": [
            {"path": "scripts/morning_brief.py", "layer": "implementation"},
        ],
        "cron_jobs": [{"schedule": "0 7 * * 1-5", "command": "morning_brief"}],
        "usage_metadata": {"invocation_count": 12},
    }
    m.update(over)
    return m


def _stub_reader(monkeypatch, manifests):
    """Point the generator's manifest reader at a fixture instead of a bot home.

    Patched on ``app_promotion_sweep`` itself — the module attribute the
    production context resolves through — rather than injected on the context,
    so the test drives the same lookup production does.
    """
    from evolve_admin.applications import app_promotion_sweep as sweep

    monkeypatch.setattr(sweep, "_read_manifests_for_bot", lambda _b: list(manifests))


# ── The charter ──────────────────────────────────────────────────────────────


def test_the_charter_loads_and_declares_a_scheduled_cadence():
    """A charter that does not load leaves the generator inert — the registry
    skips it with a load-error Signal and the sweep is unscheduled again.

    MUTATION CHECKED: setting ``cadence: on_demand`` makes this go red, which is
    the difference between "scheduled" and "runnable if someone remembers".
    """
    charter, fingerprint = load_charter_from_yaml(CHARTER_PATH)
    assert charter.id == GEN_ID
    assert charter.cadence == "daily"
    assert fingerprint
    # A promotion offer is a question put to a person; the sweep going quiet is
    # the cadence cap working, not the question expiring.
    assert charter.resolves_when_silent is False


def test_the_charter_admits_a_promotion_and_refuses_anything_else():
    """The invariants are checked BEHAVIOURALLY, through the real ingest
    checker — a charter's ``allowlist`` asserted as a list of strings proves
    only that YAML parses.

    MUTATION CHECKED: widening the allowlist to include ``ConfigPatch`` makes
    the second half go red.
    """
    from arbiter.ingest import _check_invariant
    from schema.proposal import ConfigPatch, PromoteApp

    charter, _ = load_charter_from_yaml(CHARTER_PATH)
    invariant = charter.invariant_by_id("action_kind_allowed")
    assert invariant is not None

    promotion = _proposal_with(PromoteApp(
        app_id="morning-brief", manifest_stem="morning-brief",
        identity_mode="adopted", app_audience="everyone",
    ))
    passed, _ = _check_invariant(promotion, invariant, None)
    assert passed

    other = _proposal_with(
        ConfigPatch(target_path="x.json", operation="set", value=1)
    )
    passed, detail = _check_invariant(other, invariant, None)
    assert not passed and "ConfigPatch" in detail


def test_the_charter_refuses_an_autonomous_promotion():
    """Design §4 / §7.2: promotion IS the operator vouch, so a promotion
    Proposal that named no human audience would be the bulk auto-promote the
    apps spec forbids — arriving through the generator rather than the policy.

    MUTATION CHECKED: deleting the ``promotion_is_never_autonomous`` invariant
    from the charter makes this go red.
    """
    from arbiter.ingest import _check_invariant
    from schema.proposal import PromoteApp

    charter, _ = load_charter_from_yaml(CHARTER_PATH)
    invariant = charter.invariant_by_id("promotion_is_never_autonomous")
    assert invariant is not None

    autonomous = _proposal_with(
        PromoteApp(
            app_id="morning-brief", manifest_stem="morning-brief",
            identity_mode="adopted", app_audience="everyone",
        ),
        approval_audience="none",
    )
    passed, _ = _check_invariant(autonomous, invariant, None)
    assert not passed


def _proposal_with(action, *, approval_audience="bot_primary_user"):
    from schema.proposal import Proposal, RiskTag, new_proposal_id
    from schema.provenance import Provenance

    return Proposal(
        id=new_proposal_id(),
        bot_id=BOT,
        generator_id=GEN_ID,
        dimension="applications",
        trigger_observations=["t"],
        provenance=Provenance(technique="test", signals={}, confidence=1.0),
        problem="p",
        action=action,
        risk_tag=RiskTag(blast_radius="bot", reversibility="auto",
                         touches=["app_definition"]),
        claim=None,
        approval_audience=approval_audience,
        urgency="improvement",
    )


# ── The context factory ──────────────────────────────────────────────────────


def test_the_generator_is_registered_as_a_per_bot_generator(network):
    """Registration is what the runner iterates; without it the charter loads,
    the runner logs "no context factory wired" and nothing runs — the failure
    mode this whole file exists to prevent, one layer along.

    MUTATION CHECKED: removing the ``_CONTEXT_FACTORIES`` entry makes this red;
    so does flipping ``per_bot`` to False (a pod-wide promotion sweep would ask
    one bot's question of every bot).
    """
    entry = generator_runner._CONTEXT_FACTORIES.get(GEN_ID)
    assert entry is not None, "app_promotion has no context factory wired"
    factory, per_bot = entry
    assert per_bot is True

    ctx = factory(Path("/tmp/shared"), network, BOT, {}, NOW)
    assert ctx is not None
    assert ctx.bot_id == BOT
    assert ctx.network is network
    assert ctx.now == NOW
    # Pod-wide invocation must decline rather than invent a bot.
    assert factory(Path("/tmp/shared"), network, None, {}, NOW) is None


# ── observe() ────────────────────────────────────────────────────────────────


def test_observe_returns_the_offer_for_an_eligible_draft(shared_dir, network, monkeypatch):
    """MUTATION CHECKED: making ``observe`` return ``[]`` unconditionally, and
    separately gutting ``plan_offer``, both make this go red."""
    from generators.app_promotion.observe import PromotionOfferContext, observe

    _stub_reader(monkeypatch, [("morning-brief", _eligible_manifest())])
    out = observe(PromotionOfferContext(
        bot_id=BOT, shared_dir=shared_dir, network=network, now=NOW,
    ))

    assert len(out) == 1
    proposal = out[0]
    assert proposal.generator_id == GEN_ID
    assert proposal.action.kind == "PromoteApp"
    assert proposal.approval_audience == "bot_primary_user"
    # Draft, not pending: the runner owns the transition through ingest.
    assert proposal.status == "draft"


def test_observe_writes_nothing_itself(shared_dir, network, monkeypatch):
    """The generator contract is "return proposals", not "write them" — a
    generator that also wrote would bypass dedup, the charter invariants and the
    operator's rejection cooldown.

    MUTATION CHECKED: calling ``sweep_bot`` instead of ``plan_offer`` in
    ``observe`` makes this go red (and would double-write every offer).
    """
    from arbiter import store as astore
    from generators.app_promotion.observe import PromotionOfferContext, observe

    _stub_reader(monkeypatch, [("morning-brief", _eligible_manifest())])
    observe(PromotionOfferContext(
        bot_id=BOT, shared_dir=shared_dir, network=network, now=NOW,
    ))

    assert list(astore.iter_proposals(shared_dir, subdirs=("pending",))) == []


@pytest.mark.parametrize("manifest,why", [
    (_eligible_manifest(do_not_offer=True), "the user said never"),
    (_eligible_manifest(promotion_snoozed_until="2026-12-01T00:00:00Z"), "snoozed"),
    (_eligible_manifest(definition_status="defined"), "already defined"),
    (_eligible_manifest(usage_metadata={"invocation_count": 0}, files=[]), "not ready"),
])
def test_observe_offers_nothing_when_the_gate_says_no(
    shared_dir, network, monkeypatch, manifest, why,
):
    """The scheduled path must obey every gate the CLI path obeys — the whole
    point of sharing ``plan_offer`` rather than re-deriving eligibility here.

    MUTATION CHECKED: bypassing ``evaluate_offer`` in ``select_candidates``
    makes all four go red.
    """
    from generators.app_promotion.observe import PromotionOfferContext, observe

    _stub_reader(monkeypatch, [("morning-brief", manifest)])
    assert observe(PromotionOfferContext(
        bot_id=BOT, shared_dir=shared_dir, network=network, now=NOW,
    )) == [], why


def test_observe_never_raises_when_a_bots_manifests_are_unreadable(
    shared_dir, network, monkeypatch,
):
    """One unreadable bot must not cost the other bots their sweep.

    MUTATION CHECKED: removing the ``except`` in ``observe`` makes this go red.
    """
    from evolve_admin.applications import app_promotion_sweep as sweep
    from generators.app_promotion.observe import PromotionOfferContext, observe

    def _boom(_b):
        raise OSError("manifests unreadable")

    monkeypatch.setattr(sweep, "_read_manifests_for_bot", _boom)
    assert observe(PromotionOfferContext(
        bot_id=BOT, shared_dir=shared_dir, network=network, now=NOW,
    )) == []


# ── The wiring, end to end — the test this file exists for ───────────────────


def test_the_scheduled_path_actually_writes_a_promotion_proposal(
    shared_dir, network, monkeypatch,
):
    """**§8.3 step 4, proved rather than asserted.**

    Enters at ``generator_runner.run_one_generator`` — the runner's own entry
    point, using the same registry load, context factory, ``observe`` import and
    ``arbiter.ingest`` path the daily ``run_generators`` sweep uses — and
    asserts a promotion Proposal reached the store in ``pending``.

    Every prior test in this file could pass with the generator unreachable by
    the runner: this one cannot. It is the direct answer to round 1's finding
    (*a gate with no caller*) at the level the finding kept recurring at.

    MUTATION CHECKED, all run in this session: removing the
    ``_CONTEXT_FACTORIES`` entry; renaming the charter file; and making
    ``observe`` return ``[]``. All three red.
    """
    from arbiter import store as astore

    _stub_reader(monkeypatch, [("morning-brief", _eligible_manifest())])

    ingested = generator_runner.run_one_generator(
        shared_dir, network, GEN_ID, bot_id=BOT,
    )
    assert ingested == 1, "the scheduled path ingested nothing"

    written = list(astore.iter_proposals(shared_dir, subdirs=("pending",)))
    assert len(written) == 1
    proposal = written[0]
    assert proposal.generator_id == GEN_ID
    assert proposal.action.kind == "PromoteApp"
    assert proposal.status == "pending"
    # Design §7.2: the person asked is the bot's own user, not the pod admin.
    assert proposal.approval_audience == "bot_primary_user"


def test_a_second_run_the_same_day_does_not_stack_a_second_offer(
    shared_dir, network, monkeypatch,
):
    """Two runs, two eligible drafts, ONE offer in the store.

    **This test cannot fail on any single guard being removed, and that is
    recorded rather than repaired.** Three independent mechanisms hold it, and
    each was disabled in this session to find out which:

    * the cadence cap (``recent_offers`` → 0): **still green** — deterministic
      ranking re-picks the same best draft on run 2, so nothing new is minted;
    * ``arbiter.ingest``'s fingerprint dedup, destabilised: **still green** —
      ``find_open_duplicate`` merges the re-offer anyway;
    * ``coalesce_key`` / ``dismiss_signature`` emptied, with the cap also off:
      **still green** — the fingerprint path merges it.

    Two earlier versions of this test claimed "MUTATION CHECKED: the cadence
    cap" and were **false** — the first ran one draft twice, the second added a
    second draft, and neither could fail the way it was named. A cap and a dedup
    are not the same guarantee (dedup stops the same offer twice; the cap stops
    pestering *about anything*), so the cap is proved by the next test, at the
    layer where it decides.

    Kept because the user-visible property is worth pinning — one question, one
    card — and because "held by three things, none of them individually
    necessary" is a fact about this path a future reader should not have to
    rediscover.
    """
    from arbiter import store as astore

    _stub_reader(monkeypatch, [
        ("morning-brief", _eligible_manifest(name="Morning Brief")),
        ("evening-wrap", _eligible_manifest(
            id="evening-wrap", name="Evening Wrap",
            usage_metadata={"invocation_count": 5},
        )),
    ])

    generator_runner.run_one_generator(shared_dir, network, GEN_ID, bot_id=BOT)
    generator_runner.run_one_generator(shared_dir, network, GEN_ID, bot_id=BOT)

    offers = [
        p for subdir in ("pending", "snoozed", "applied", "archived")
        for p in astore.iter_proposals(shared_dir, subdirs=(subdir,))
        if p.generator_id == GEN_ID
    ]
    assert len(offers) == 1, (
        f"two offers stacked for one bot: {[o.action.app_id for o in offers]}"
    )


def test_the_cadence_cap_is_read_from_the_store_not_from_memory(
    shared_dir, network, monkeypatch,
):
    """An offer already in the store suppresses today's, in a FRESH process.

    The scheduled path and the operator's CLI run in different processes, so a
    cap held in memory would let each of them spend the same day once. This
    test seeds the store the way the other path would have, then runs the
    generator with no prior in-process state.

    MUTATION CHECKED: forcing ``recent_offers=0`` makes this go red.
    """
    from arbiter import store as astore
    from evolve_admin.applications import app_promotion as ap
    from generators.app_promotion.observe import PromotionOfferContext, observe

    seeded = ap.build_promotion_proposal(
        _eligible_manifest(),
        bot_id=BOT, manifest_stem="morning-brief", network=network, now=NOW,
    )
    seeded.status = "pending"
    astore.write_proposal(seeded, shared_dir)

    _stub_reader(monkeypatch, [
        ("evening-wrap", _eligible_manifest(id="evening-wrap", name="Evening Wrap")),
    ])
    assert observe(PromotionOfferContext(
        bot_id=BOT, shared_dir=shared_dir, network=network, now=NOW,
    )) == [], "a stored offer from today must suppress the next one"


def test_the_registered_generator_is_the_one_track_record_logs_about(shared_dir, network, monkeypatch):
    """The charter's id must equal the ``generator_id`` on the Proposal.

    Before this charter existed, ``arbiter.track_record`` logged "generator not
    loaded" every time one of these was accepted — the acceptance was recorded
    against nothing. A mismatch here would reproduce that silently.

    MUTATION CHECKED: changing ``id:`` in the charter makes this go red.
    """
    from arbiter import store as astore

    charter, _ = load_charter_from_yaml(CHARTER_PATH)
    _stub_reader(monkeypatch, [("morning-brief", _eligible_manifest())])
    generator_runner.run_one_generator(shared_dir, network, GEN_ID, bot_id=BOT)

    written = list(astore.iter_proposals(shared_dir, subdirs=("pending",)))
    assert written and written[0].generator_id == charter.id

    record = json.loads((shared_dir / "generators" / f"{GEN_ID}.json").read_text())
    assert record["id"] == charter.id
    assert record["status"] == "active", (
        "a bootstrapped record must be active, or the daily sweep skips it"
    )
