"""tests/test_cron_caps_filler.py — cron_caps_filler factory + observe tests.

Coverage matches the cache_ttl_tuner test shape:

* The factory turns a single ``perm_cron_uncapped_agent_turn`` Signal
  + a job dict into a well-formed ``UpsertCronJob`` Proposal that
  carries both caps in the payload (so the applier accepts it).
* The factory is idempotent on operator-set caps — it only fills the
  missing one rather than overwriting a manual value.
* The observe() entry point dispatches end-to-end against a tmp Signal
  store populated via the real signals.store.observe API, locating the
  matching job via a tmp jobs.json (per-bot home override).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.cron_caps_filler.observe import (  # noqa: E402
    CronCapsFillerContext,
    observe,
)
from generators.cron_caps_filler.signal_proposals import (  # noqa: E402
    DEFAULT_MAX_BUDGET_USD,
    DEFAULT_MAX_TURNS,
    make_uncapped_cron_proposal,
)
from schema.signal import make_signature  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _uncapped_signal(
    *,
    sig_id: str = "sig-cron-1",
    bot_id: str = "admin_bot",
    job_id: str = "j-morning-brief",
    name: str = "morning_brief",
    has_turn_cap: bool = False,
    has_budget_cap: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=sig_id,
        bot_id=bot_id,
        type="perm_cron_uncapped_agent_turn",
        severity="warn",
        details={
            "bot_id": bot_id,
            "job_id": job_id,
            "name": name,
            "has_turn_cap": has_turn_cap,
            "has_budget_cap": has_budget_cap,
        },
    )


def _uncapped_job(
    *,
    job_id: str = "j-morning-brief",
    name: str = "morning_brief",
    schedule: str = "0 7 * * *",
    payload_extra: dict | None = None,
) -> dict:
    payload = {"kind": "agentTurn", "message": "Run morning brief"}
    if payload_extra:
        payload.update(payload_extra)
    return {
        "id": job_id,
        "name": name,
        "enabled": True,
        "schedule": schedule,
        "payload": payload,
    }


def _seed_jobs_file(home_dir: Path, jobs: list[dict]) -> Path:
    """Write a per-bot home's .openclaw/cron/jobs.json for tests.

    Returns the path so callers can assert on it. The observer uses
    ``home_override`` to redirect ``permissions.writer.read_cron_jobs``
    here instead of the real ``/Users/<bot>/...`` path.
    """
    cron_dir = home_dir / ".openclaw" / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    target = cron_dir / "jobs.json"
    target.write_text(json.dumps({"jobs": jobs}, indent=2))
    return target


# ── Factory: make_uncapped_cron_proposal ─────────────────────────────────────


def test_factory_produces_well_formed_upsert_proposal():
    sig = _uncapped_signal()
    job = _uncapped_job()
    p = make_uncapped_cron_proposal(sig, job=job)

    assert p.bot_id == "admin_bot"
    assert p.generator_id == "cron_caps_filler"
    assert p.dimension == "safety"
    assert p.action.kind == "UpsertCronJob"
    assert p.motivating_signals == ["sig-cron-1"]
    assert p.trigger_observations == [
        "perm_cron_uncapped_agent_turn:admin_bot:j-morning-brief"
    ]
    assert p.approval_audience == "pod_operator"
    assert p.urgency == "hygiene"
    assert p.risk_tag.blast_radius == "bot"
    assert p.risk_tag.reversibility == "auto"
    assert p.risk_tag.touches == ["cron_config"]
    assert p.claim is None  # typed config edit; no metric verification
    # Phase C-2 humanized title (2026-06-04). Was "Add caps to <bot>
    # cron <name>"; now leads with "Add the standard caps" so the
    # default values are implied (the operator can still edit before
    # approving).
    assert p.admin_surface_summary.startswith(
        "Add the standard caps to admin_bot's"
    )
    assert "cron" in p.admin_surface_summary
    assert len(p.admin_surface_summary) <= 120


def test_factory_payload_carries_both_caps_when_both_missing():
    sig = _uncapped_signal()
    job = _uncapped_job()
    p = make_uncapped_cron_proposal(sig, job=job)

    new_job = p.action.job
    assert new_job["id"] == "j-morning-brief"
    payload = new_job["payload"]
    assert payload["maxTurns"] == DEFAULT_MAX_TURNS
    assert payload["maxBudgetUsd"] == DEFAULT_MAX_BUDGET_USD
    # Existing payload fields preserved
    assert payload["kind"] == "agentTurn"
    assert payload["message"] == "Run morning brief"


def test_factory_does_not_mutate_input_job():
    """Caller's job dict must be untouched — deep-copy contract."""
    sig = _uncapped_signal()
    job = _uncapped_job()
    payload_id = id(job["payload"])
    _ = make_uncapped_cron_proposal(sig, job=job)
    assert id(job["payload"]) == payload_id
    assert "maxTurns" not in job["payload"]
    assert "maxBudgetUsd" not in job["payload"]


def test_factory_preserves_existing_turn_cap():
    """Operator set maxTurns manually; only the missing budget cap fills."""
    sig = _uncapped_signal(has_turn_cap=True, has_budget_cap=False)
    job = _uncapped_job(payload_extra={"maxTurns": 50})
    p = make_uncapped_cron_proposal(sig, job=job)

    payload = p.action.job["payload"]
    assert payload["maxTurns"] == 50  # operator value preserved
    assert payload["maxBudgetUsd"] == DEFAULT_MAX_BUDGET_USD


def test_factory_preserves_existing_budget_cap():
    """Same shape inverted: existing budgetUsd kept; turn cap filled."""
    sig = _uncapped_signal(has_turn_cap=False, has_budget_cap=True)
    job = _uncapped_job(payload_extra={"budgetUsd": 5.0})
    p = make_uncapped_cron_proposal(sig, job=job)

    payload = p.action.job["payload"]
    assert payload["maxTurns"] == DEFAULT_MAX_TURNS
    # Operator's budget cap value preserved verbatim (not normalized)
    assert payload["budgetUsd"] == 5.0
    # Filler did NOT also set maxBudgetUsd — operator's value already
    # satisfies the applier's "either name" check, so we leave it alone.
    assert "maxBudgetUsd" not in payload


def test_factory_honors_custom_defaults():
    sig = _uncapped_signal()
    job = _uncapped_job()
    p = make_uncapped_cron_proposal(
        sig, job=job,
        default_max_turns=5,
        default_max_budget_usd=0.25,
    )
    payload = p.action.job["payload"]
    assert payload["maxTurns"] == 5
    assert payload["maxBudgetUsd"] == 0.25
    # Provenance reflects what we set
    assert p.provenance.signals["default_max_turns"] == 5
    assert p.provenance.signals["default_max_budget_usd"] == 0.25
    assert p.provenance.signals["set_max_turns"] is True
    assert p.provenance.signals["set_max_budget_usd"] is True


def test_factory_handles_dict_signal_form():
    sig_dict = {
        "id": "sig-d-1",
        "bot_id": "team_bot_a",
        "type": "perm_cron_uncapped_agent_turn",
        "details": {"bot_id": "team_bot_a", "job_id": "j-x"},
    }
    p = make_uncapped_cron_proposal(sig_dict, job=_uncapped_job(job_id="j-x"))
    assert p.bot_id == "team_bot_a"
    assert p.motivating_signals == ["sig-d-1"]


def test_factory_refuses_job_without_id():
    sig = _uncapped_signal()
    bad = _uncapped_job()
    del bad["id"]
    try:
        make_uncapped_cron_proposal(sig, job=bad)
    except ValueError as exc:
        assert "id" in str(exc)
    else:
        raise AssertionError("expected ValueError for missing id")


def test_factory_problem_mentions_what_it_set():
    """The operator-facing ``problem`` field names the caps being added
    so the chat paraphrase / Recommendations row is meaningful — a
    proposal that just says 'fix the thing' wastes the operator's time."""
    sig = _uncapped_signal()
    job = _uncapped_job()
    p = make_uncapped_cron_proposal(sig, job=job)
    text = p.problem
    assert f"maxTurns={DEFAULT_MAX_TURNS}" in text
    assert f"maxBudgetUsd=${DEFAULT_MAX_BUDGET_USD:.2f}" in text
    assert "morning_brief" in text  # job name surfaces
    assert "admin_bot" in text          # bot id surfaces


# ── observe() end-to-end ──────────────────────────────────────────────────────


def _write_signal(
    shared_dir: Path,
    *,
    bot_id: str,
    job_id: str,
    name: str = "morning_brief",
    has_turn_cap: bool = False,
    has_budget_cap: bool = False,
) -> str:
    sig = signals_store.observe(
        shared_dir,
        signature=make_signature(
            "permission_monitor", "perm_cron_uncapped_agent_turn",
            f"{bot_id}:{job_id}",
        ),
        producer="permission_monitor",
        type="perm_cron_uncapped_agent_turn",
        flavor="security",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id}: cron job {name!r} missing caps",
        details={
            "bot_id": bot_id, "job_id": job_id, "name": name,
            "has_turn_cap": has_turn_cap,
            "has_budget_cap": has_budget_cap,
        },
    )
    return sig.id


def test_observe_emits_one_proposal_per_uncapped_signal(tmp_path):
    home = tmp_path / "homes" / "admin_bot"
    _seed_jobs_file(home, [_uncapped_job(job_id="j-a"), _uncapped_job(job_id="j-b", name="hourly")])

    _write_signal(tmp_path, bot_id="admin_bot", job_id="j-a", name="morning_brief")
    _write_signal(tmp_path, bot_id="admin_bot", job_id="j-b", name="hourly")

    proposals = observe(
        CronCapsFillerContext(
            bot_id="admin_bot",
            shared_dir=tmp_path,
            home_override=home,
        )
    )
    assert len(proposals) == 2
    job_ids = {p.action.job["id"] for p in proposals}
    assert job_ids == {"j-a", "j-b"}
    for p in proposals:
        assert p.bot_id == "admin_bot"
        assert p.generator_id == "cron_caps_filler"
        assert p.action.kind == "UpsertCronJob"
        assert p.motivating_signals  # cross-link populated


def test_observe_filters_by_bot_id(tmp_path):
    admin_bot_home = tmp_path / "homes" / "admin_bot"
    team_bot_c_home = tmp_path / "homes" / "team_bot_c"
    _seed_jobs_file(admin_bot_home, [_uncapped_job(job_id="j-s")])
    _seed_jobs_file(team_bot_c_home, [_uncapped_job(job_id="j-r")])

    _write_signal(tmp_path, bot_id="admin_bot", job_id="j-s")
    _write_signal(tmp_path, bot_id="team_bot_c", job_id="j-r")

    admin_bot_props = observe(
        CronCapsFillerContext(
            bot_id="admin_bot", shared_dir=tmp_path, home_override=admin_bot_home,
        )
    )
    team_bot_c_props = observe(
        CronCapsFillerContext(
            bot_id="team_bot_c", shared_dir=tmp_path, home_override=team_bot_c_home,
        )
    )
    assert len(admin_bot_props) == 1
    assert admin_bot_props[0].bot_id == "admin_bot"
    assert len(team_bot_c_props) == 1
    assert team_bot_c_props[0].bot_id == "team_bot_c"


def test_observe_ignores_other_producers(tmp_path):
    """Signals from other producers must not be consumed."""
    home = tmp_path / "homes" / "admin_bot"
    _seed_jobs_file(home, [_uncapped_job()])

    # Write a signal with the same type but a different producer — must be ignored.
    signals_store.observe(
        tmp_path,
        signature=make_signature(
            "fake_producer", "perm_cron_uncapped_agent_turn",
            "admin_bot:j-morning-brief",
        ),
        producer="fake_producer",
        type="perm_cron_uncapped_agent_turn",
        flavor="security",
        severity="warn",
        scope="bot",
        bot_id="admin_bot",
        title="impostor",
        details={"bot_id": "admin_bot", "job_id": "j-morning-brief"},
    )
    proposals = observe(
        CronCapsFillerContext(
            bot_id="admin_bot", shared_dir=tmp_path, home_override=home,
        )
    )
    assert proposals == []


def test_observe_ignores_unrelated_signal_types(tmp_path):
    """Other permission_monitor signal types (denylist match, silent add)
    must not produce caps proposals."""
    home = tmp_path / "homes" / "admin_bot"
    _seed_jobs_file(home, [_uncapped_job()])

    signals_store.observe(
        tmp_path,
        signature=make_signature(
            "permission_monitor", "perm_cron_denylist_match",
            "admin_bot:j-morning-brief:denylist",
        ),
        producer="permission_monitor",
        type="perm_cron_denylist_match",
        flavor="security",
        severity="alert",
        scope="bot",
        bot_id="admin_bot",
        title="admin_bot: denylist match",
        details={"bot_id": "admin_bot", "job_id": "j-morning-brief"},
    )
    proposals = observe(
        CronCapsFillerContext(
            bot_id="admin_bot", shared_dir=tmp_path, home_override=home,
        )
    )
    assert proposals == []


def test_observe_skips_signal_when_job_not_in_jobs_file(tmp_path):
    """Operator removed the job between signal emission and now —
    the filler must silently skip (let signal_monitor's sweep_resolve
    clean up the stale signal next pass)."""
    home = tmp_path / "homes" / "admin_bot"
    # Empty jobs file: signal says j-gone exists, jobs.json says otherwise.
    _seed_jobs_file(home, [])

    _write_signal(tmp_path, bot_id="admin_bot", job_id="j-gone")

    proposals = observe(
        CronCapsFillerContext(
            bot_id="admin_bot", shared_dir=tmp_path, home_override=home,
        )
    )
    assert proposals == []


def test_observe_returns_empty_when_no_signals(tmp_path):
    home = tmp_path / "homes" / "admin_bot"
    _seed_jobs_file(home, [_uncapped_job()])
    proposals = observe(
        CronCapsFillerContext(
            bot_id="admin_bot", shared_dir=tmp_path, home_override=home,
        )
    )
    assert proposals == []


def test_observe_honors_per_bot_default_caps(tmp_path):
    """The ctx's default_max_turns / default_max_budget_usd flow through
    to the produced proposal — operator can dial the caps down without
    touching the generator code."""
    home = tmp_path / "homes" / "admin_bot"
    _seed_jobs_file(home, [_uncapped_job()])
    _write_signal(tmp_path, bot_id="admin_bot", job_id="j-morning-brief")

    proposals = observe(
        CronCapsFillerContext(
            bot_id="admin_bot",
            shared_dir=tmp_path,
            home_override=home,
            default_max_turns=3,
            default_max_budget_usd=0.10,
        )
    )
    assert len(proposals) == 1
    payload = proposals[0].action.job["payload"]
    assert payload["maxTurns"] == 3
    assert payload["maxBudgetUsd"] == 0.10


# ── Phase 5 — config_intent integration ──────────────────────────────────────


def _record_uncapped_intent(shared_dir, bot_id, job_id, *,
                            reason="long-running batch — uncapped by design"):
    """Drop a config_intent record that flags the job as intentionally
    uncapped — the canonical shape the observer's intent gate looks for."""
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'admin'))
    from evolve_admin.config_intent import set_intent
    return set_intent(
        bot_id=bot_id,
        field_path=f"commands.cron.{job_id}.caps",
        value=None,
        reason=reason,
        set_by="pod_admin (intent test)",
        shared_dir=shared_dir,
    )


def test_observe_suppresses_proposal_when_intent_marks_job_uncapped(tmp_path):
    """Operator recorded that job j-morning-brief is intentionally
    uncapped. The generator must NOT emit an UpsertCronJob proposal
    for it — that would be context-free noise per the memory entry
    feedback_generators_consider_intent."""
    home = tmp_path / "homes" / "admin_bot"
    _seed_jobs_file(home, [_uncapped_job(job_id="j-morning-brief")])
    _write_signal(tmp_path, bot_id="admin_bot", job_id="j-morning-brief")
    _record_uncapped_intent(tmp_path, "admin_bot", "j-morning-brief")

    proposals = observe(
        CronCapsFillerContext(
            bot_id="admin_bot", shared_dir=tmp_path, home_override=home,
        )
    )
    assert proposals == [], (
        "Intent matched — generator should have skipped the proposal"
    )


def test_observe_still_emits_when_no_intent_recorded(tmp_path):
    """Sanity: without an intent, the legacy proposal path runs."""
    home = tmp_path / "homes" / "admin_bot"
    _seed_jobs_file(home, [_uncapped_job(job_id="j-morning-brief")])
    _write_signal(tmp_path, bot_id="admin_bot", job_id="j-morning-brief")

    proposals = observe(
        CronCapsFillerContext(
            bot_id="admin_bot", shared_dir=tmp_path, home_override=home,
        )
    )
    assert len(proposals) == 1


def test_observe_suppression_is_per_job(tmp_path):
    """An intent for job A must not suppress job B."""
    home = tmp_path / "homes" / "admin_bot"
    _seed_jobs_file(home, [
        _uncapped_job(job_id="j-a"),
        _uncapped_job(job_id="j-b", name="hourly"),
    ])
    _write_signal(tmp_path, bot_id="admin_bot", job_id="j-a")
    _write_signal(tmp_path, bot_id="admin_bot", job_id="j-b", name="hourly")
    _record_uncapped_intent(tmp_path, "admin_bot", "j-a")

    proposals = observe(
        CronCapsFillerContext(
            bot_id="admin_bot", shared_dir=tmp_path, home_override=home,
        )
    )
    job_ids = {p.action.job["id"] for p in proposals}
    assert job_ids == {"j-b"}, (
        f"Only j-b should propose (j-a intent-explained); got {job_ids}"
    )


def test_observe_does_not_suppress_when_intent_value_differs_from_none(
    tmp_path,
):
    """An intent recorded with a NON-None value (e.g. operator
    accidentally wrote True instead of None) is treated as 'doesn't
    match the uncapped-by-design semantic' and the proposal still
    fires. Better to over-emit than to suppress on a malformed
    intent."""
    home = tmp_path / "homes" / "admin_bot"
    _seed_jobs_file(home, [_uncapped_job(job_id="j-morning-brief")])
    _write_signal(tmp_path, bot_id="admin_bot", job_id="j-morning-brief")
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'admin'))
    from evolve_admin.config_intent import set_intent
    set_intent(
        bot_id="admin_bot",
        field_path="commands.cron.j-morning-brief.caps",
        value=True,  # NOT None — doesn't match the suppression semantics
        reason="misrecorded",
        set_by="pod_admin",
        shared_dir=tmp_path,
    )

    proposals = observe(
        CronCapsFillerContext(
            bot_id="admin_bot", shared_dir=tmp_path, home_override=home,
        )
    )
    assert len(proposals) == 1


def test_observe_suppresses_when_intent_still_valid_false_branch_NOT_reached(
    tmp_path, monkeypatch,
):
    """Phase 5 plugin-coupled validity check hook: when
    intent_still_valid returns False (e.g. depends_on.plugin removed),
    suppress should NOT fire — the generator emits as if no intent
    existed. Today intent_still_valid always returns True so this is
    a forward-compatibility wiring test exercised via monkeypatch."""
    home = tmp_path / "homes" / "admin_bot"
    _seed_jobs_file(home, [_uncapped_job(job_id="j-morning-brief")])
    _write_signal(tmp_path, bot_id="admin_bot", job_id="j-morning-brief")
    _record_uncapped_intent(tmp_path, "admin_bot", "j-morning-brief")

    sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'admin'))
    from evolve_admin import config_intent as _ci
    monkeypatch.setattr(_ci, "intent_still_valid", lambda *a, **kw: False)

    proposals = observe(
        CronCapsFillerContext(
            bot_id="admin_bot", shared_dir=tmp_path, home_override=home,
        )
    )
    assert len(proposals) == 1, (
        "Intent is recorded but stale — generator must fall through to "
        "its legacy propose path so the operator gets a refreshed prompt."
    )


def test_observe_consult_config_intent_false_disables_gate(tmp_path):
    """Operator can opt the gate off (e.g. to force re-check after an
    intent-recording bug). The legacy 'always propose' path runs even
    when a matching intent exists."""
    home = tmp_path / "homes" / "admin_bot"
    _seed_jobs_file(home, [_uncapped_job(job_id="j-morning-brief")])
    _write_signal(tmp_path, bot_id="admin_bot", job_id="j-morning-brief")
    _record_uncapped_intent(tmp_path, "admin_bot", "j-morning-brief")

    proposals = observe(
        CronCapsFillerContext(
            bot_id="admin_bot", shared_dir=tmp_path, home_override=home,
            consult_config_intent=False,
        )
    )
    assert len(proposals) == 1

