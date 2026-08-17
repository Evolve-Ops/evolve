"""tests/test_model_upgrade_store.py — the auto-upgrade DURABLE STORES.

Spec: docs/spec-model-auto-upgrade-2026-07-30.md §Cadence / §Apply path /
§Rollback (Phase 3a — storage + schema only).

Everything here runs against ``tmp_path``; no store path literal appears
anywhere (the platform-path ratchet, and the fact that these stores are
``{shared_dir}``-relative by construction). **Nothing under test mutates any
tier config** — that is P3b, deliberately absent.

The matrix mirrors the invariants the phase exists to guarantee:
  - stamp-once: a re-detected upgrade keeps its ORIGINAL first-seen stamp
  - disappear-and-return: a listing blip does NOT restart the veto clock
  - the prune never removes a currently-detected upgrade's stamp
  - a degraded run does not prune at all (an empty listing is not absence)
  - skips read as empty from a missing / malformed file, never as an error
  - the skip key is per-(family, version) and the first-seen key per-rung —
    the two must NOT be interchangeable
  - the audit record round-trips, including the multi-scope shape and the
    held-back items with their reasons
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import model_auto_upgrade as mau  # noqa: E402
import model_upgrade_store as store  # noqa: E402

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)

# Synthetic provider/model names — TESTS are one of the three legal homes for
# a provider/model literal (spec-model-rungs-and-roles §Addendum 3.B).
PROV = "anthropic"
FAMILY = "claude-opus"
CUR = "claude-opus-4-8"
NEW = "claude-opus-5-0"


def _upgrade(rung: str = "power", latest: str = NEW, provider: str = PROV):
    """A minimal ``VersionUpgrade``-shaped row (the store reads it by
    attribute, exactly as the daily generator hands it over)."""
    return SimpleNamespace(
        provider=provider, family=FAMILY, current_model=CUR,
        latest_model=latest, rung_slug=rung, roles=["power"],
    )


# ── first-seen ledger: stamp-once ────────────────────────────────────────────


def test_first_seen_missing_ledger_reads_empty(tmp_path):
    """No ledger means no clock — never an error. ``compute_auto_upgrades``
    then leaves ``visible_days`` unknown and the floor unevaluated."""
    assert store.read_first_seen(tmp_path) == {}


def test_stamp_records_first_detection(tmp_path):
    key = mau.upgrade_key(PROV, "power", NEW)
    res = store.stamp_first_seen(tmp_path, [key], now=NOW)

    assert res.stamped == [key]
    assert res.kept == []
    assert store.read_first_seen(tmp_path) == {key: NOW.isoformat()}


def test_stamp_is_idempotent_and_never_overwrites(tmp_path):
    """Stamp-once. Re-detection on later days must leave the ORIGINAL stamp
    alone — the veto window is measured from first sight, not last sight."""
    key = mau.upgrade_key(PROV, "power", NEW)
    store.stamp_first_seen(tmp_path, [key], now=NOW)

    for day in (1, 2, 5):
        res = store.stamp_first_seen(
            tmp_path, [key], now=NOW + timedelta(days=day),
        )
        assert res.stamped == []
        assert res.kept == [key]

    assert store.read_first_seen(tmp_path)[key] == NOW.isoformat()


def test_disappear_and_return_keeps_the_original_stamp(tmp_path):
    """The failure mode that rules out deriving the clock from the freshness
    Signal's timestamp: a provider listing blip hides the upgrade for one
    run, and its return must NOT restart the veto window."""
    key = mau.upgrade_key(PROV, "power", NEW)
    store.stamp_first_seen(tmp_path, [key], now=NOW)

    # Day 1: the provider listing blipped — this key is not detected.
    store.stamp_first_seen(tmp_path, [], now=NOW + timedelta(days=1))
    assert store.read_first_seen(tmp_path)[key] == NOW.isoformat()

    # Day 2: it is back. Still the original stamp, so the floor still counts
    # from day 0 rather than from the recovery.
    res = store.stamp_first_seen(tmp_path, [key], now=NOW + timedelta(days=2))
    assert res.stamped == []
    assert store.read_first_seen(tmp_path)[key] == NOW.isoformat()


def test_visible_days_flows_into_the_engine_floor(tmp_path):
    """End-to-end shape check: what the ledger hands back is exactly what
    ``compute_auto_upgrades(first_seen=…)`` consumes, so the floor the ledger
    exists to power actually evaluates."""
    key = mau.upgrade_key(PROV, "power", NEW)
    store.stamp_first_seen(tmp_path, [key], now=NOW)
    first_seen = store.read_first_seen(tmp_path)

    assert mau._parse_iso(first_seen[key]) == NOW


# ── first-seen ledger: pruning ───────────────────────────────────────────────


def test_prune_drops_entries_absent_past_the_window(tmp_path):
    gone = mau.upgrade_key(PROV, "power", NEW)
    live = mau.upgrade_key(PROV, "standard", NEW)
    store.stamp_first_seen(tmp_path, [gone], now=NOW)

    later = NOW + timedelta(days=store.PRUNE_AFTER_DAYS + 1)
    res = store.stamp_first_seen(tmp_path, [live], now=later)

    assert res.pruned == [gone]
    assert set(store.read_first_seen(tmp_path)) == {live}


def test_prune_keeps_an_entry_still_inside_the_window(tmp_path):
    key = mau.upgrade_key(PROV, "power", NEW)
    store.stamp_first_seen(tmp_path, [key], now=NOW)

    store.stamp_first_seen(
        tmp_path, [], now=NOW + timedelta(days=store.PRUNE_AFTER_DAYS - 1),
    )
    assert store.read_first_seen(tmp_path)[key] == NOW.isoformat()


def test_prune_never_removes_a_currently_detected_upgrade(tmp_path):
    """The load-bearing prune invariant: dropping the stamp of an upgrade
    this run just saw would restart its veto window and re-open a decision
    the operator already had time to make.

    The prune is evaluated against the PRE-refresh snapshot, so this is a
    real exercise of the exemption rather than a pass by statement order: the
    detected key's own ``last_seen`` is far past the cutoff at the moment the
    prune looks at it, and only the exemption keeps it."""
    detected = mau.upgrade_key(PROV, "power", NEW)
    absent = mau.upgrade_key(PROV, "standard", NEW)
    store.stamp_first_seen(tmp_path, [detected, absent], now=NOW)

    # Both entries are now PRUNE_AFTER_DAYS+1 stale; one of them is detected.
    later = NOW + timedelta(days=store.PRUNE_AFTER_DAYS + 1)
    res = store.stamp_first_seen(tmp_path, [detected], now=later)

    assert res.pruned == [absent]
    assert store.read_first_seen(tmp_path) == {detected: NOW.isoformat()}


def test_prune_exemption_survives_the_most_hostile_cutoff(tmp_path):
    """Same invariant at ``prune_after_days=0`` — forget anything not seen
    right now. The detected key still keeps its ORIGINAL stamp."""
    detected = mau.upgrade_key(PROV, "power", NEW)
    absent = mau.upgrade_key(PROV, "standard", NEW)
    store.stamp_first_seen(tmp_path, [detected, absent], now=NOW)

    res = store.stamp_first_seen(
        tmp_path, [detected], now=NOW + timedelta(seconds=1), prune_after_days=0,
    )

    assert res.pruned == [absent]
    assert store.read_first_seen(tmp_path) == {detected: NOW.isoformat()}


def test_stamp_accepts_a_naive_clock(tmp_path):
    """A caller passing ``datetime.utcnow()`` must not blow up the prune's
    datetime arithmetic — in the generator path that raise is swallowed and
    the ledger would silently stop advancing."""
    key = mau.upgrade_key(PROV, "power", NEW)
    naive = NOW.replace(tzinfo=None)
    store.stamp_first_seen(tmp_path, [key], now=naive)
    store.stamp_first_seen(
        tmp_path, [], now=naive + timedelta(days=1),
    )

    assert store.read_first_seen(tmp_path)[key] == NOW.isoformat()


def test_prune_disabled_forgets_nothing(tmp_path):
    """``prune_after_days=None`` is the degraded-run escape hatch: a run whose
    detection was incomplete does not get to forget."""
    key = mau.upgrade_key(PROV, "power", NEW)
    store.stamp_first_seen(tmp_path, [key], now=NOW)

    store.stamp_first_seen(
        tmp_path, [],
        now=NOW + timedelta(days=store.PRUNE_AFTER_DAYS * 10),
        prune_after_days=None,
    )
    assert store.read_first_seen(tmp_path)[key] == NOW.isoformat()


def test_ledger_tolerates_a_flat_legacy_document(tmp_path):
    """A hand-written / pre-``last_seen`` ``{key: iso}`` file still reads, and
    its stamp is still immutable."""
    key = mau.upgrade_key(PROV, "power", NEW)
    path = store.first_seen_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({key: NOW.isoformat()}))

    assert store.read_first_seen(tmp_path) == {key: NOW.isoformat()}
    store.stamp_first_seen(tmp_path, [key], now=NOW + timedelta(days=3))
    assert store.read_first_seen(tmp_path)[key] == NOW.isoformat()


def test_ledger_tolerates_a_corrupt_document(tmp_path):
    path = store.first_seen_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")

    assert store.read_first_seen(tmp_path) == {}
    key = mau.upgrade_key(PROV, "power", NEW)
    store.stamp_first_seen(tmp_path, [key], now=NOW)
    assert store.read_first_seen(tmp_path) == {key: NOW.isoformat()}


def test_upgrade_keys_drops_unlocated_rows(tmp_path):
    """An upgrade with no rung has no apply target, so there is nothing for a
    veto window to protect."""
    keys = store.upgrade_keys([_upgrade(rung="power"), _upgrade(rung="")])

    assert keys == [mau.upgrade_key(PROV, "power", NEW)]


def test_upgrade_keys_are_per_rung(tmp_path):
    """The same version bump landing in two rungs is two decisions, each with
    its own clock."""
    keys = store.upgrade_keys([_upgrade(rung="power"), _upgrade(rung="standard")])

    assert len(set(keys)) == 2


# ── the daily writer (generator wiring) ──────────────────────────────────────


def _stamp_via_generator(tmp_path, upgrades, *, degraded=(), now=NOW):
    from generators.model_discovery.observe import _stamp_upgrade_first_seen

    ctx = SimpleNamespace(shared_dir=tmp_path, now=now)
    result = SimpleNamespace(upgrades=list(upgrades),
                             degraded_providers=list(degraded))
    _stamp_upgrade_first_seen(ctx, result)


def test_daily_generator_run_stamps_the_ledger(tmp_path):
    """The clock is started by the DAILY generator run, not by a page view —
    a pod whose operator never opens the card still gets a veto window."""
    _stamp_via_generator(tmp_path, [_upgrade()])

    assert store.read_first_seen(tmp_path) == {
        mau.upgrade_key(PROV, "power", NEW): NOW.isoformat(),
    }


def test_observe_signals_is_the_entry_point_that_stamps(tmp_path, monkeypatch):
    """The wiring itself, not just the helper: dropping the call from
    ``observe_signals`` must fail a test. The discovery pipeline is stubbed —
    this asserts the entry point, not the pipeline."""
    import importlib

    # NOT ``from generators.model_discovery import observe`` — the package
    # re-exports the observe() FUNCTION under that name.
    gen = importlib.import_module("generators.model_discovery.observe")

    result = SimpleNamespace(
        upgrades=[_upgrade()], degraded_providers=[], discoveries=[],
        uncovered_providers=[], enumerated_providers=[PROV],
        checked_at=NOW.isoformat(),
    )
    monkeypatch.setattr(gen, "_resolve_result", lambda _ctx: result)
    ctx = gen.ModelDiscoveryContext(bot_id=None, shared_dir=tmp_path, now=NOW)

    gen.observe_signals(ctx)

    assert store.read_first_seen(tmp_path) == {
        mau.upgrade_key(PROV, "power", NEW): NOW.isoformat(),
    }


def test_degraded_run_does_not_prune(tmp_path):
    """A provider whose listing call failed contributes no rows, so its
    entries look absent. A run that could not look does not get to forget."""
    key = mau.upgrade_key(PROV, "power", NEW)
    store.stamp_first_seen(tmp_path, [key], now=NOW)

    _stamp_via_generator(
        tmp_path, [],
        degraded=[{"provider": PROV, "reason": "http 500"}],
        now=NOW + timedelta(days=store.PRUNE_AFTER_DAYS + 5),
    )
    assert store.read_first_seen(tmp_path)[key] == NOW.isoformat()


def test_stamp_failure_never_sinks_the_run(tmp_path, monkeypatch):
    """Fail-open: a ledger write failure logs and returns. A missing stamp
    costs an upgrade a longer wait, never a premature apply."""
    def _boom(*_a, **_kw):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(store, "stamp_first_seen", _boom)
    _stamp_via_generator(tmp_path, [_upgrade()])  # must not raise

    assert store.read_first_seen(tmp_path) == {}


# ── skips ────────────────────────────────────────────────────────────────────


def test_skips_missing_file_is_no_skips_not_an_error(tmp_path):
    assert store.read_skips(tmp_path) == {}
    assert store.is_skipped(store.read_skips(tmp_path), FAMILY, NEW) is False


def test_skips_malformed_file_reads_as_no_skips(tmp_path):
    path = store.skips_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("]not json[")

    assert store.read_skips(tmp_path) == {}


def test_skips_drops_junk_entries_but_keeps_the_readable_ones(tmp_path):
    """A half-corrupt file still suppresses the versions it can still name."""
    good = store.skip_key(FAMILY, NEW)
    path = store.skips_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "skips": {
        good: {"family": FAMILY, "latest_model": NEW,
               "recorded_at": NOW.isoformat()},
        "junk": "not-an-object",
        "": {"family": FAMILY},
    }}))

    skips = store.read_skips(tmp_path)
    assert set(skips) == {good}


def test_record_and_clear_skip_round_trip(tmp_path):
    store.record_skip(tmp_path, FAMILY, NEW, actor="operator", now=NOW)
    skips = store.read_skips(tmp_path)

    assert store.is_skipped(skips, FAMILY, NEW) is True
    assert skips[store.skip_key(FAMILY, NEW)].actor == "operator"

    assert store.clear_skip(tmp_path, FAMILY, NEW) is True
    assert store.read_skips(tmp_path) == {}
    assert store.clear_skip(tmp_path, FAMILY, NEW) is False


def test_re_skipping_keeps_the_original_timestamp(tmp_path):
    """"When did we decline this?" must stay answerable."""
    store.record_skip(tmp_path, FAMILY, NEW, now=NOW)
    rec = store.record_skip(tmp_path, FAMILY, NEW, now=NOW + timedelta(days=30))

    assert rec.recorded_at == NOW.isoformat()


def test_skip_key_is_per_family_version_not_per_rung(tmp_path):
    """The two key spaces are deliberately different, and the contract is
    behavioural: one skip recorded from the Power-rung card row must suppress
    the SAME version in Standard. Declining a version declines the version."""
    power_row, standard_row = _upgrade(rung="power"), _upgrade(rung="standard")
    store.record_skip(tmp_path, power_row.family, power_row.latest_model, now=NOW)
    skips = store.read_skips(tmp_path)

    # One skip, both rows suppressed — the rung is not part of the identity.
    assert store.is_skipped(skips, power_row.family, power_row.latest_model) is True
    assert store.is_skipped(
        skips, standard_row.family, standard_row.latest_model,
    ) is True
    assert store.skip_key(power_row.family, power_row.latest_model) == \
        store.skip_key(standard_row.family, standard_row.latest_model)

    # …while the per-rung first-seen keys stay distinct from each other and
    # from the skip key: two veto clocks, one suppression.
    keys = store.upgrade_keys([power_row, standard_row])
    assert len(set(keys)) == 2
    assert store.skip_key(FAMILY, NEW) not in set(keys)


def test_skip_is_per_version_so_the_next_bump_surfaces_fresh(tmp_path):
    """Spec §Permanently-held: suppression is per-version, never per-family —
    a skipped 5.0 must not silence 5.1."""
    store.record_skip(tmp_path, FAMILY, NEW, now=NOW)
    skips = store.read_skips(tmp_path)

    assert store.is_skipped(skips, FAMILY, "claude-opus-5-1") is False


def test_skip_key_normalizes_qualified_and_cased_ids(tmp_path):
    assert store.skip_key(FAMILY, f"{PROV}/{NEW}") == store.skip_key(FAMILY, NEW)
    assert store.skip_key(FAMILY.upper(), NEW.upper()) == store.skip_key(FAMILY, NEW)


def test_skip_key_shape_is_family_colon_bare_version(tmp_path):
    """The literal shape is a contract: P3b reads it and Phase 4 writes it, so
    a silent change to the composition would desync the button from the guard.
    Note it carries no provider — declining a version declines it however the
    pod reaches that model."""
    assert store.skip_key(FAMILY, NEW) == f"{FAMILY}:{NEW}"


# ── audit records ────────────────────────────────────────────────────────────


def _batch(batch_id="20260804T120000Z-aaaaaa"):
    """One batch carrying the shapes rollback depends on: a MULTI-SCOPE apply
    (pod + a Custom bot's own tiers doc) with verbatim before/after lists, and
    the held-back items with their reasons."""
    return store.UpgradeBatch(
        batch_id=batch_id,
        ran_at=NOW.isoformat(),
        policy=mau.AutoUpgradePolicy(enabled=True, enabled_source="pod").to_dict(),
        applied=[store.AppliedUpgrade(
            upgrade_key=mau.upgrade_key(PROV, "power", NEW),
            provider=PROV, family=FAMILY,
            current_model=CUR, latest_model=NEW, rung_slug="power",
            scopes=[
                store.ScopeChange(
                    scope="pod", rung_slug="power",
                    models_before=[CUR, "openai/gpt-x"],
                    models_after=[NEW, CUR, "openai/gpt-x"],
                ),
                store.ScopeChange(
                    scope="team-bot-a", rung_slug="power",
                    models_before=[CUR],
                    models_after=[NEW, CUR],
                ),
            ],
        )],
        held=[store.HeldUpgrade(
            upgrade_key=mau.upgrade_key(PROV, "standard", "claude-sonnet-6"),
            provider=PROV, family="claude-sonnet",
            current_model="claude-sonnet-5", latest_model="claude-sonnet-6",
            rung_slug="standard",
            holds=[mau.HoldReason(
                code=mau.HOLD_COST_REGRESSION,
                message="costs more than what you run today",
                detail={"regressions": [{"axis": "input"}]},
            ).to_dict()],
        )],
        skipped_providers=[{"provider": "xai", "reason": "no listing available"}],
    )


def test_batch_round_trips_including_multi_scope_and_held(tmp_path):
    written = _batch()
    store.write_batch(tmp_path, written)
    read = store.read_batch(tmp_path, written.batch_id)

    assert read is not None
    assert read.to_dict() == written.to_dict()

    # Multi-scope survives as per-(entry, scope) with VERBATIM prior contents —
    # what makes rollback exact rather than reconstructive.
    entry = read.applied[0]
    assert [s.scope for s in entry.scopes] == ["pod", "team-bot-a"]
    assert entry.scopes[0].models_before == [CUR, "openai/gpt-x"]
    assert entry.scopes[1].models_before == [CUR]
    assert entry.scopes[0].models_after[0] == NEW  # spliced AHEAD (models[0] wins)

    # The anti-invisibility contract: held items keep their reasons.
    assert read.held[0].reason_codes == [mau.HOLD_COST_REGRESSION]
    assert read.held[0].durable is True
    assert read.skipped_providers == [
        {"provider": "xai", "reason": "no listing available"},
    ]


def test_batch_scope_labels_match_apply_editable_documents(tmp_path):
    """The record's scope labelling must agree with the writer's: the labels
    come from ``apply_editable_documents``, which is the enumeration of the
    documents an apply can actually edit."""
    network = {
        "bots": {"team-bot-a": {}},
        "models": {"rungs": [{"id": "power", "models": [CUR]}]},
    }
    labels = [
        label for label, _doc in mau.apply_editable_documents(
            network, None, {"team-bot-a": {"rungs": [{"id": "power", "models": [CUR]}]}},
        )
    ]

    assert labels == ["pod", "team-bot-a"]
    assert [s.scope for s in _batch().applied[0].scopes] == labels


def test_a_run_that_applied_nothing_still_records_what_it_considered(tmp_path):
    """Spec §Failure and safety posture — the 57-emitted/0-applied
    invisibility must not reappear one layer down."""
    empty = store.UpgradeBatch(
        batch_id="20260804T120000Z-bbbbbb",
        ran_at=NOW.isoformat(),
        policy=mau.DEFAULT_POLICY.to_dict(),
        held=_batch().held,
    )
    store.write_batch(tmp_path, empty)
    read = store.read_batch(tmp_path, empty.batch_id)

    assert read is not None
    assert read.applied_count == 0
    assert read.held[0].holds[0]["message"]


def test_latest_batch_is_the_most_recent(tmp_path):
    """``rollback-upgrade`` with no argument reverts the most recent batch."""
    for bid in ("20260801T090000Z-aaaaaa", "20260804T120000Z-bbbbbb",
                "20260802T090000Z-cccccc"):
        store.write_batch(tmp_path, _batch(batch_id=bid))

    latest = store.latest_batch(tmp_path)
    assert latest is not None
    assert latest.batch_id == "20260804T120000Z-bbbbbb"


def test_latest_batch_skips_a_corrupt_newest_record(tmp_path):
    """A corrupt newest file degrades to "roll back the one before it", never
    to "there is nothing to roll back"."""
    store.write_batch(tmp_path, _batch(batch_id="20260801T090000Z-aaaaaa"))
    store.batch_path(tmp_path, "20260804T120000Z-zzzzzz").write_text("{corrupt")

    latest = store.latest_batch(tmp_path)
    assert latest is not None
    assert latest.batch_id == "20260801T090000Z-aaaaaa"


def test_find_applied_locates_the_entry_for_rollback_by_id(tmp_path):
    key = mau.upgrade_key(PROV, "power", NEW)
    store.write_batch(tmp_path, _batch(batch_id="20260801T090000Z-aaaaaa"))
    store.write_batch(tmp_path, _batch(batch_id="20260804T120000Z-bbbbbb"))

    found = store.find_applied(tmp_path, key)
    assert found is not None
    batch, entry = found
    assert batch.batch_id == "20260804T120000Z-bbbbbb"   # most recent apply
    assert entry.upgrade_key == key

    assert store.find_applied(tmp_path, "nope:power:nope") is None


def test_read_batch_missing_is_none_not_an_error(tmp_path):
    assert store.read_batch(tmp_path, "nope") is None
    assert store.latest_batch(tmp_path) is None
    assert store.iter_batches(tmp_path) == []
    assert store.find_applied(tmp_path, "anything") is None


def test_shape_malformed_records_read_as_empty_never_raise(tmp_path):
    """JSON-valid but shape-wrong records must not raise. ``rollback-upgrade``
    is the one command whose job is to work when something has already gone
    wrong — a ``TypeError`` traceback out of the reader is the opposite of the
    degradation ``latest_batch`` promises."""
    bad = [
        {"batch_id": "b", "policy": "oops"},
        {"batch_id": "b", "policy": [1, 2, 3]},
        {"batch_id": "b", "applied": 5},
        {"batch_id": "b", "held": 7},
        {"batch_id": "b", "skipped_providers": 2},
        {"batch_id": "b", "applied": [{"scopes": 9}]},
        {"batch_id": "b", "applied": [{"scopes": [{"models_before": 3}]}]},
        {"batch_id": "b", "held": [{"holds": "nope"}]},
    ]
    for i, doc in enumerate(bad):
        bid = f"20260804T12000{i}Z-badbad"
        store.batch_path(tmp_path, bid).parent.mkdir(parents=True, exist_ok=True)
        store.batch_path(tmp_path, bid).write_text(json.dumps(doc))

        batch = store.read_batch(tmp_path, bid)
        assert batch is not None, doc
        assert isinstance(batch.policy, dict)
        assert isinstance(batch.applied, list)
        assert isinstance(batch.held, list)

    # And the aggregate readers walk the whole directory without raising.
    assert len(store.iter_batches(tmp_path)) == len(bad)
    assert store.latest_batch(tmp_path) is not None
    assert store.find_applied(tmp_path, "nope") is None


def test_a_shape_malformed_newest_record_does_not_hide_a_good_one(tmp_path):
    good = _batch(batch_id="20260801T090000Z-aaaaaa")
    store.write_batch(tmp_path, good)
    store.batch_path(tmp_path, "20260804T120000Z-zzzzzz").write_text(
        json.dumps({"batch_id": "z", "applied": "not-a-list"}),
    )

    assert store.find_applied(tmp_path, good.applied[0].upgrade_key) is not None


# ── audit records: the fields rollback correctness depends on ────────────────


def test_a_failed_scope_is_recorded_and_excluded_from_rollback(tmp_path):
    """A partial apply (pod ok, bot write failed) must not read as a clean
    two-scope apply — rolling ``models_before`` into a document the apply
    never touched would revert whatever the operator did there since."""
    entry = store.AppliedUpgrade(
        upgrade_key=mau.upgrade_key(PROV, "power", NEW),
        provider=PROV, family=FAMILY, current_model=CUR, latest_model=NEW,
        rung_slug="power",
        scopes=[
            store.ScopeChange(scope="pod", rung_slug="power",
                              models_before=[CUR], models_after=[NEW, CUR]),
            store.ScopeChange(scope="team-bot-a", rung_slug="power",
                              models_before=[CUR], models_after=[CUR],
                              status=store.SCOPE_FAILED,
                              error="EACCES writing evolve-tiers.json"),
        ],
    )
    batch = store.UpgradeBatch(
        batch_id="20260804T120000Z-partial", ran_at=NOW.isoformat(),
        applied=[entry],
    )
    store.write_batch(tmp_path, batch)
    read = store.read_batch(tmp_path, batch.batch_id)

    assert read is not None
    got = read.applied[0]
    # The failed scope survives in the record (the run says what it tried)…
    assert len(got.scopes) == 2
    assert got.scopes[1].error
    # …but is not a rollback target.
    assert [s.scope for s in got.applied_scopes] == ["pod"]


def test_a_created_rung_is_distinguishable_from_an_empty_one(tmp_path):
    """``models_before == []`` is otherwise ambiguous between "the rung
    existed and was empty" and "the rung did not exist"."""
    created = store.ScopeChange(
        scope="pod", rung_slug="power", models_before=[], models_after=[NEW],
        rung_created=True, cost_class="premium",
    )
    was_empty = store.ScopeChange(
        scope="pod", rung_slug="power", models_before=[], models_after=[NEW],
    )

    assert created.to_dict() != was_empty.to_dict()
    assert store.ScopeChange.from_dict(created.to_dict()).rung_created is True
    assert store.ScopeChange.from_dict(was_empty.to_dict()).rung_created is False


def test_a_rolled_back_batch_is_not_the_default_target_twice(tmp_path):
    """A bare ``rollback-upgrade`` resolves to the most recent batch every
    time; without the marker a second invocation silently re-reverts and
    undoes a deliberate re-adoption."""
    older = _batch(batch_id="20260801T090000Z-aaaaaa")
    newer = _batch(batch_id="20260804T120000Z-bbbbbb")
    store.write_batch(tmp_path, older)
    store.write_batch(tmp_path, newer)

    target = store.latest_batch(tmp_path, skip_rolled_back=True)
    assert target is not None and target.batch_id == newer.batch_id

    marked = store.mark_rolled_back(tmp_path, newer.batch_id, now=NOW)
    assert marked is not None and marked.rolled_back is True

    again = store.latest_batch(tmp_path, skip_rolled_back=True)
    assert again is not None and again.batch_id == older.batch_id
    # …and the unfiltered view still reports it, so history stays intact.
    assert store.latest_batch(tmp_path).batch_id == newer.batch_id
    assert store.find_applied(
        tmp_path, newer.applied[0].upgrade_key, skip_rolled_back=True,
    )[0].batch_id == older.batch_id


def test_re_marking_a_rollback_keeps_the_original_timestamp(tmp_path):
    batch = _batch()
    store.write_batch(tmp_path, batch)
    store.mark_rolled_back(tmp_path, batch.batch_id, now=NOW)
    again = store.mark_rolled_back(
        tmp_path, batch.batch_id, now=NOW + timedelta(days=1),
    )

    assert again is not None and again.rolled_back_at == NOW.isoformat()
    assert store.mark_rolled_back(tmp_path, "nope") is None


def test_new_batch_id_sorts_chronologically(tmp_path):
    early = store.new_batch_id(NOW)
    late = store.new_batch_id(NOW + timedelta(days=7))

    assert early < late


def test_held_from_decisions_projects_engine_rows(tmp_path):
    """P3b's anti-invisibility obligation is one call, not a hand-rolled
    projection at the call site."""
    decision = mau.UpgradeDecision(
        provider=PROV, family=FAMILY, current_model=CUR, latest_model=NEW,
        rung_slug="power",
        holds=[mau.HoldReason(code=mau.HOLD_TARGET_PRICE_UNKNOWN,
                              message="no published price yet")],
    )

    held = store.held_from_decisions([decision])
    assert len(held) == 1
    assert held[0].upgrade_key == mau.upgrade_key(PROV, "power", NEW)
    assert held[0].reason_codes == [mau.HOLD_TARGET_PRICE_UNKNOWN]
    assert held[0].durable is False


# ── posture: this bite mutates no tier config ────────────────────────────────


def test_store_writes_stay_inside_the_store_directory(tmp_path):
    """P3a is storage + schema only. Every write this module makes lands under
    ``{shared_dir}/model_upgrades/`` — no ``network.json``, no
    ``evolve-tiers.json``, no rung, no routing."""
    store.stamp_first_seen(tmp_path, [mau.upgrade_key(PROV, "power", NEW)], now=NOW)
    store.record_skip(tmp_path, FAMILY, NEW, now=NOW)
    store.write_batch(tmp_path, _batch())

    written = {p.relative_to(tmp_path).parts[0]
               for p in tmp_path.rglob("*") if p.is_file()}
    assert written == {store.STORE_DIRNAME}
