"""tests/test_evo_add_bot.py — the `evo add-bot` chain (M1 + M2 + M3).

Spec: internal/spec-add-bot-wizard-build-delta-2026-06-10.md §3 + §5 + §6 +
§7 + §9.

Exercises the conversational bot-creation wizard:

  * Registry: `add-bot` is admin-only; primaries and secondaries are
    refused before any session exists. `evo wizard` keeps its meaning.
  * Chain: AB_NEED → AB_AUDIENCE → AB_PURPOSE → AB_SHAPE → AB_BRIEFING
    → AB_CONSENT_TONE → AB_NAME → AB_PLAN → AB_CREDENTIALS →
    AB_PROVISION → AB_SMOKE_WRAP on the existing engine — agenda mode
    for the open phases, verbatim for the plan echo and the
    build/progress renders.
  * Purpose capture: on a confirmed plan, the new bot's ``purpose{}``
    block (effectiveness-layer §4 shape — same keys, same archetype
    enum, ``captured: "declared"``, ``confidence: 1.0``) is written into
    its network.json bot block via the in-place-mutation +
    ``network_dirty`` contract the route handler persists.
  * M2 build half: the credential walk (borrow / paste / skip, each
    validated deterministically — never via LLM extraction), the
    provision job with outcome-language progress, honest failure
    relay + retry/stop, and the smoke wrap that never leaves a broken
    bot running. All build machinery is seam-injected
    (provision_driver.set_*) so no test touches dscl / launchctl /
    real bots' files.
  * M3 defaults: the starter-app proposal from the captured role
    (accept / adjust-by-name / none / honest no-pack), the
    morning-briefing default-on with an explicit opt-out turn, the
    non-skippable consent + tone phase, and the finalize defaults —
    install jobs queued in order with consent seeded onto each app,
    ground rules written to the bot guide, briefing decision recorded
    in the bot's network.json block. Pack machinery is seam-injected
    (pack.set_pack_loader / pack_driver.set_install_runner).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from evolve_admin import external_ids as ext_ids  # noqa: E402


def _stub_extractor(user_message, targets, current_state):
    """Keyword-driven stub. Maps phrases in the user's message onto the
    target schema so tests can drive the phase machine without an LLM."""
    msg = (user_message or "").lower()
    names = {t.name for t in targets}
    out = {}
    if "ab_need" in names and "watch" in msg:
        out["ab_need"] = "watch the ecosystem and feed back research"
    if "ab_bot_kind" in names and "research assistant" in msg:
        out["ab_bot_kind"] = "research assistant"
    if "ab_mission" in names:
        if "weekly" in msg:
            out["ab_mission"] = (
                "Watch the ecosystem and post a weekly digest to the group."
            )
        elif "watch" in msg:
            out["ab_mission"] = (
                "Watch the ecosystem and post a daily digest to the group."
            )
    if "ab_archetype" in names and ("watch" in msg or "research" in msg):
        out["ab_archetype"] = "research-analyst"
    if "ab_audiences" in names and "group" in msg:
        out["ab_audiences"] = ["a group of enthusiasts", "the pod admin"]
    if "ab_audiences" in names and "just me" in msg:
        out["ab_audiences"] = ["just me"]
    if "ab_surface" in names and "telegram" in msg:
        if "direct messages" in msg:
            out["ab_surface"] = "Telegram direct messages"
        else:
            out["ab_surface"] = "a Telegram group"
    if "ab_surface" in names and "slack" in msg:
        out["ab_surface"] = "a Slack channel"
    if "ab_shared_feed" in names and "same content" in msg:
        out["ab_shared_feed"] = "one shared feed"
    if "ab_bot_name" in names:
        if "nova" in msg:
            out["ab_bot_name"] = "Nova"
        elif "lumen" in msg:
            out["ab_bot_name"] = "Lumen"
    if "ab_consent_notice" in names and "pinned" in msg:
        out["ab_consent_notice"] = (
            "This bot saves links the group shares and posts a daily "
            "digest; react 🤐 to keep a message out."
        )
    if "ab_consent_notice" in names and "keeps notes" in msg:
        out["ab_consent_notice"] = (
            "This bot keeps notes from our chats to do its job; say "
            "'stop keeping notes' any time."
        )
    if "ab_opt_out" in names and ("🤐" in msg or "react" in msg):
        out["ab_opt_out"] = "react 🤐 to a message to keep it out"
    if "ab_tone" in names and "short" in msg:
        out["ab_tone"] = "short and direct, no emoji"
    if "ab_tg_privacy" in names and "every message" in msg:
        out["ab_tg_privacy"] = "every message"
    return out


@pytest.fixture(autouse=True)
def install_stub():
    from evolve_admin.evo.wizard import extractor as _ext
    _ext.set_extractor(_stub_extractor)
    yield
    _ext.set_extractor(None)


@pytest.fixture(autouse=True)
def driver_seams():
    """Every build-machinery seam stubbed + synchronous, reset after.

    Defaults are safe no-ops; individual tests overwrite via
    ``set_provision_runner`` etc. for their scenario. ``set_force_sync``
    keeps the kickoff inline so poll-turn assertions never race a
    thread."""
    from evolve_admin.evo.wizard import provision_driver as pd

    pd.set_force_sync(True)
    pd.set_borrow_candidates_loader(lambda provider, network: ["admin_bot"])
    pd.set_provision_runner(lambda job: None)  # leaves status "running"
    pd.set_smoke_runner(lambda account: {"status": "ok", "summary": "ok"})
    pd.set_quarantine_runner(lambda account: (True, "stopped"))
    yield pd
    pd.set_force_sync(False)
    pd.set_borrow_candidates_loader(None)
    pd.set_provision_runner(None)
    pd.set_smoke_runner(None)
    pd.set_quarantine_runner(None)
    pd._JOBS.clear()


# The stub starter pack uses REAL gallery pkg_ids so the finalize path's
# queue (which loads the packages from the repo's builtin gallery) works
# end-to-end without network or seams beyond the install runner.
_PKG_TASK_MANAGER = "p-9bfa1c84"
_PKG_NOTE_TAKER = "p-f14e9562"
_PKG_CALENDAR_SYNC = "p-fe9acef3"
_PKG_MORNING_BRIEFING = "p-a9a74bf7"


def _stub_pack_loader(archetype, shared_dir):
    from evolve_admin.evo.wizard import pack as pk
    return pk.StarterPack(
        bundle_pkg_id="p-stub1234",
        bundle_name="Research starter set",
        apps=[
            pk.PackApp(pkg_id=_PKG_TASK_MANAGER, name="Task Manager",
                       blurb="keeps the project's open work", est_usd=0.40),
            pk.PackApp(pkg_id=_PKG_NOTE_TAKER, name="Meeting Note-taker",
                       blurb="turns meetings into notes", est_usd=0.40),
        ],
    )


@pytest.fixture(autouse=True)
def channel_seams():
    """Channel-connect machinery seam-injected, reset after. The default
    verifier accepts any token-shaped paste; the default connector
    records instead of touching openclaw.json / launchd."""
    from evolve_admin.evo.wizard import channel_driver as chd

    connected: list[tuple] = []
    chd.set_verify_token(lambda token: {
        "ok": True, "bot_username": "NovaBot", "error": None,
    })
    chd.set_connector(
        lambda account, token, verify_result: (
            connected.append((account, token, verify_result)),
            (True, None),
        )[1],
    )
    yield connected
    chd.set_verify_token(None)
    chd.set_connector(None)
    with chd._lock:
        chd._stash.clear()


@pytest.fixture(autouse=True)
def pack_seams():
    """Starter-pack machinery seam-injected + synchronous, reset after.
    The default loader returns a two-app pack; the install runner is a
    recording no-op (tests read ``installed`` for run order)."""
    from evolve_admin.evo.wizard import pack as pk
    from evolve_admin.evo.wizard import pack_driver as pkd

    installed: list[tuple] = []
    pk.set_pack_loader(_stub_pack_loader)
    pkd.set_force_sync(True)
    pkd.set_install_runner(
        lambda shared_dir, job_id, bot_id: installed.append(
            (shared_dir, job_id, bot_id),
        ),
    )
    yield installed
    pk.set_pack_loader(None)
    pkd.set_force_sync(False)
    pkd.set_install_runner(None)


_ALL_STAGES = [
    "validate_inputs", "create_macos_user", "create_openclaw_dir",
    "openclaw_onboard", "add_bot_to_network", "deploy_bot",
    "seed_model_config",
]


def _runner_success(calls=None):
    """Fake pipeline: every stage lands, smoke passes."""
    def runner(job):
        if calls is not None:
            calls.append(job)
        with job._lock:
            for stage in _ALL_STAGES:
                job.events.append((stage, "start", ""))
                job.events.append((stage, "ok", ""))
            job.smoke = {"status": "ok", "summary": "all checks passed"}
            job.status = "succeeded"
    return runner


def _runner_failure(calls=None):
    """Fake pipeline: fails at the Claude-connection stage, rolled back."""
    def runner(job):
        if calls is not None:
            calls.append(job)
        with job._lock:
            for stage in _ALL_STAGES[:3]:
                job.events.append((stage, "start", ""))
                job.events.append((stage, "ok", ""))
            job.events.append(("openclaw_onboard", "start", ""))
            job.events.append(("openclaw_onboard", "fail", "exited 1"))
            job.status = "failed"
            job.failed_stage = "openclaw_onboard"
            job.error = "exited 1: bad key"
            job.rollback_log = [
                "removed /Users/nova/.openclaw",
                "deleted macOS user nova",
            ]
    return runner


def _runner_smoke_fail(calls=None):
    """Fake pipeline: build lands but the final check fails."""
    def runner(job):
        if calls is not None:
            calls.append(job)
        with job._lock:
            for stage in _ALL_STAGES:
                job.events.append((stage, "start", ""))
                job.events.append((stage, "ok", ""))
            job.smoke = {"status": "fail", "summary": "not answering yet"}
            job.status = "succeeded"
    return runner


def _runner_still_running(calls=None):
    """Fake pipeline snapshot mid-flight: two stages done, third going."""
    def runner(job):
        if calls is not None:
            calls.append(job)
        with job._lock:
            for stage in _ALL_STAGES[:2]:
                job.events.append((stage, "start", ""))
                job.events.append((stage, "ok", ""))
            job.events.append((_ALL_STAGES[2], "start", ""))
        # status stays "running"
    return runner


def _admin_network(tmp_path, members=("admin_bot",)):
    """Network where telegram sender 9999 is a pod admin."""
    return {
        "members": list(members),
        "sharedDir": str(tmp_path),
        "pod": {"admins": {"external_ids": {"telegram": ["9999"]}}},
    }


def _dispatch_add_bot(network, sender="9999"):
    from evolve_admin.evo.dispatch import dispatch
    return dispatch(
        network,
        bot_id="admin_bot",
        channel="telegram",
        sender_external_id=sender,
        raw_text="evo add-bot",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Registry / role gating
# ─────────────────────────────────────────────────────────────────────────────


def test_registry_has_admin_only_add_bot():
    from evolve_admin.evo import subcommands as sc

    cmd = sc.lookup("add-bot")
    assert cmd is not None
    assert cmd.available_to == frozenset({"admin"})
    assert sc.role_can_run("admin", cmd) is True
    assert sc.role_can_run("primary", cmd) is False
    assert sc.role_can_run("secondary", cmd) is False


def test_help_lists_add_bot_for_admin_only():
    from evolve_admin.evo.subcommands import subcommands_for_role

    assert "add-bot" in {c.name for c in subcommands_for_role("admin")}
    assert "add-bot" not in {c.name for c in subcommands_for_role("primary")}


def test_wizard_subcommand_untouched():
    """`evo wizard` keeps its meaning — user setup for this bot, open to
    everyone. The two conversations must not share a name."""
    from evolve_admin.evo import subcommands as sc

    cmd = sc.lookup("wizard")
    assert cmd is not None
    assert sc.role_can_run("primary", cmd) is True
    assert sc.role_can_run("secondary", cmd) is True


def test_dispatch_refuses_non_admin(tmp_path):
    network = _admin_network(tmp_path)
    result = _dispatch_add_bot(network, sender="someone-else")
    assert result.role == "primary"  # generous fallback, but not admin
    assert result.wizard_session_id is None
    assert "isn't available" in (result.system_append or "")


def test_dispatch_starts_session_for_admin(tmp_path):
    network = _admin_network(tmp_path)
    result = _dispatch_add_bot(network)
    assert result.subcommand == "add-bot"
    assert result.role == "admin"
    assert result.wizard_session_id
    assert "[EVO ADD-BOT" in (result.system_append or "")
    # Agenda phase — the LLM converses; nothing is direct-sent.
    assert result.direct_send_message is None


def test_dispatch_resumes_in_flight_session(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    r1 = _dispatch_add_bot(network)
    wsid = r1.wizard_session_id
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="I want it to watch the ecosystem for me.",
        network=network,
    )
    assert r.phase == "ab_audience"
    # Re-entering `evo add-bot` resumes at the same phase, state intact.
    r2 = _dispatch_add_bot(network)
    assert r2.wizard_session_id == wsid
    assert "[EVO ADD-BOT" in (r2.system_append or "")
    st = _read_state(tmp_path, wsid)
    assert st.current_phase == "ab_audience"
    assert st.extracted.get("ab_need")


def _read_state(tmp_path, wsid):
    from evolve_admin.evo.wizard import state as _state
    st = _state.read_state(tmp_path, "admin_bot", wsid)
    assert st is not None
    return st


# ─────────────────────────────────────────────────────────────────────────────
# Phase definitions
# ─────────────────────────────────────────────────────────────────────────────


def test_phase_chain_traverses_need_to_smoke_wrap():
    from evolve_admin.evo.wizard import phases

    seen = []
    cur = phases.PHASE_AB_NEED
    while cur:
        seen.append(cur)
        ph = phases.get_phase(cur)
        assert ph is not None, f"unknown phase {cur}"
        cur = ph.next_phase
    assert seen == [
        phases.PHASE_AB_NEED,
        phases.PHASE_AB_AUDIENCE,
        phases.PHASE_AB_PURPOSE,
        phases.PHASE_AB_SHAPE,
        phases.PHASE_AB_BRIEFING,
        phases.PHASE_AB_CHANNEL,
        phases.PHASE_AB_CONSENT_TONE,
        phases.PHASE_AB_NAME,
        phases.PHASE_AB_PLAN,
        phases.PHASE_AB_CREDENTIALS,
        phases.PHASE_AB_PROVISION,
        phases.PHASE_AB_SMOKE_WRAP,
    ]
    names = [p.name for p in phases.all_add_bot_phases()]
    assert names == [
        "ab_need", "ab_audience", "ab_purpose", "ab_shape", "ab_briefing",
        "ab_channel", "ab_consent_tone", "ab_name", "ab_plan",
        "ab_credentials", "ab_provision", "ab_smoke_wrap",
    ]


def test_render_modes_per_spec_table():
    """Delta §3.2: agenda for the open phases AND the credential walk;
    verbatim for the plan echo, the build progress, and the wrap."""
    from evolve_admin.evo.wizard import phases

    assert phases.AB_PLAN_PHASE.render_mode == "verbatim"
    assert phases.AB_PROVISION_PHASE.render_mode == "verbatim"
    assert phases.AB_SMOKE_WRAP_PHASE.render_mode == "verbatim"
    for ph in (phases.AB_NEED_PHASE, phases.AB_AUDIENCE_PHASE,
               phases.AB_PURPOSE_PHASE, phases.AB_SHAPE_PHASE,
               phases.AB_BRIEFING_PHASE, phases.AB_CHANNEL_PHASE,
               phases.AB_CONSENT_TONE_PHASE,
               phases.AB_NAME_PHASE, phases.AB_CREDENTIALS_PHASE):
        assert ph.render_mode == "agenda"


def test_initial_phase_routes_add_bot_audience():
    from evolve_admin.evo.wizard import phases

    assert phases.initial_phase("add_bot") == phases.PHASE_AB_NEED
    # Existing routing untouched.
    assert phases.initial_phase("primary", "secondary") == phases.PHASE_CHALLENGE
    assert phases.initial_phase("primary", "admin") == phases.PHASE_GREET


def test_exit_conditions():
    from evolve_admin.evo.wizard import phases as p

    assert p.AB_NEED_PHASE.exit_condition({}) is False
    assert p.AB_NEED_PHASE.exit_condition({"ab_need": "watch things"}) is True

    assert p.AB_AUDIENCE_PHASE.exit_condition({}) is False
    assert p.AB_AUDIENCE_PHASE.exit_condition({"ab_audiences": ["me"]}) is True

    # Purpose needs BOTH a mission and a schema-valid archetype.
    assert p.AB_PURPOSE_PHASE.exit_condition({"ab_mission": "do x"}) is False
    assert p.AB_PURPOSE_PHASE.exit_condition(
        {"ab_mission": "do x", "ab_archetype": "definitely-not-real"}
    ) is False
    assert p.AB_PURPOSE_PHASE.exit_condition(
        {"ab_mission": "do x", "ab_archetype": "research-analyst"}
    ) is True

    # Name must yield a usable account name.
    assert p.AB_NAME_PHASE.exit_condition({"ab_bot_name": "!!!"}) is False
    assert p.AB_NAME_PHASE.exit_condition({"ab_bot_name": "Nova"}) is True


def test_consent_tone_exit_is_non_skippable():
    """Delta §7: never finish a bot with an audience without explicit
    consent + tone answers. Group bots additionally need the opt-out;
    Telegram group bots the privacy-mode answer."""
    from evolve_admin.evo.wizard import phases as p

    personal = {"ab_audiences": ["just me"], "ab_surface": "direct messages"}
    assert p.AB_CONSENT_TONE_PHASE.exit_condition({**personal}) is False
    assert p.AB_CONSENT_TONE_PHASE.exit_condition(
        {**personal, "ab_consent_notice": "keeps notes; say stop any time"},
    ) is False  # tone still missing
    assert p.AB_CONSENT_TONE_PHASE.exit_condition(
        {**personal, "ab_consent_notice": "keeps notes",
         "ab_tone": "short and direct"},
    ) is True

    group = {
        "ab_audiences": ["a Telegram group of enthusiasts", "the admin"],
        "ab_surface": "a Telegram group",
        "ab_consent_notice": "pinned intro; archives shared links",
        "ab_tone": "short and direct",
    }
    # Group audience: the opt-out gesture is required too.
    assert p.AB_CONSENT_TONE_PHASE.exit_condition({**group}) is False
    # Telegram group: privacy-mode answer required as well.
    assert p.AB_CONSENT_TONE_PHASE.exit_condition(
        {**group, "ab_opt_out": "react 🤐"},
    ) is False
    assert p.AB_CONSENT_TONE_PHASE.exit_condition(
        {**group, "ab_opt_out": "react 🤐", "ab_tg_privacy": "every message"},
    ) is True


def test_group_audience_and_telegram_helpers():
    from evolve_admin.evo.wizard import phases as p

    assert p.ab_group_audience({"ab_audiences": ["just me"]}) is False
    assert p.ab_group_audience({"ab_audiences": ["me", "the team"]}) is True
    assert p.ab_group_audience({"ab_audiences": ["my household"]}) is True
    assert p.ab_telegram_group_surface(
        {"ab_surface": "a Telegram group", "ab_audiences": ["a group"]},
    ) is True
    assert p.ab_telegram_group_surface(
        {"ab_surface": "Slack", "ab_audiences": ["the team"]},
    ) is False


def test_normalize_archetype_is_the_effectiveness_enum():
    from evolve_admin.evo.wizard.phases import AB_ARCHETYPES, normalize_archetype

    # Exactly the §4 enum — same keys, no parallel vocabulary.
    assert AB_ARCHETYPES == (
        "personal-assistant", "project-manager", "home-automation",
        "research-analyst", "customer-facing", "custom",
    )
    assert normalize_archetype("research-analyst") == "research-analyst"
    assert normalize_archetype("Personal Assistant") == "personal-assistant"
    assert normalize_archetype("project_manager") == "project-manager"
    assert normalize_archetype("librarian") is None
    assert normalize_archetype(None) is None


def test_account_name_for():
    """Underscores, never hyphens: the slug becomes the bot_id and
    macOS user, and bot_ids are [a-z][a-z0-9_]{1,31} everywhere
    (hyphens clash with launchd label namespacing)."""
    from evolve_admin.evo.wizard.phases import account_name_for

    assert account_name_for("Nova") == "nova"
    assert account_name_for("Field Notes") == "field_notes"
    assert account_name_for("field-notes") == "field_notes"
    assert account_name_for("  Atlas!  ") == "atlas"
    assert account_name_for("123") == ""  # must start with a letter
    assert account_name_for("!!!") == ""
    assert account_name_for("x") == ""  # bot_ids are 2+ chars
    assert account_name_for(None) == ""


# ─────────────────────────────────────────────────────────────────────────────
# Engine — full E2E run to a confirmed plan + persisted purpose
# ─────────────────────────────────────────────────────────────────────────────


def _drive_to_shape(tmp_path, network):
    """Walk a fresh add-bot session up to the starter-app proposal."""
    from evolve_admin.evo.wizard import engine

    r = _dispatch_add_bot(network)
    wsid = r.wizard_session_id
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message=(
            "I want a bot to watch the OpenClaw ecosystem and feed me "
            "research. More research assistant than co-pilot."
        ),
        network=network,
    )
    assert r.phase == "ab_audience"
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message=(
            "A Telegram group of enthusiasts, and me. Same content for "
            "both of us."
        ),
        network=network,
    )
    assert r.phase == "ab_purpose"
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="Yes, that's exactly it.",
        network=network,
    )
    assert r.phase == "ab_shape"
    return wsid, r


def _drive_to_plan(
    tmp_path, network, *, briefing_reply="yes", channel_reply="skip",
):
    """Walk a fresh add-bot session up to the rendered plan — accepting
    the proposed starter set, answering the briefing offer with
    ``briefing_reply``, and the channel offer (the surface is Telegram,
    so the offer fires) with ``channel_reply``."""
    from evolve_admin.evo.wizard import engine

    wsid, _ = _drive_to_shape(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    assert r.phase == "ab_briefing"
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message=briefing_reply, network=network,
    )
    assert r.phase == "ab_channel"
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message=channel_reply, network=network,
    )
    assert r.phase == "ab_consent_tone"
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message=(
            "Post a pinned intro; people can react 🤐 to keep a message "
            "out. Keep it short and direct, and it should see every "
            "message."
        ),
        network=network,
    )
    assert r.phase == "ab_name"
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="Nova. Let's go with Nova.",
        network=network,
    )
    assert r.phase == "ab_plan"
    return wsid, r


def test_full_run_renders_plan_then_persists_purpose(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid, r = _drive_to_plan(tmp_path, network)

    # The plan echo is verbatim — direct-sent, canonical text.
    assert r.completed is False
    body = r.direct_send_message or ""
    assert "Nova" in body
    assert "account: nova" in body
    assert "daily digest" in body
    assert "research assistant" in body
    assert "Nothing is built yet" in body
    # M3: the plan echoes the apps, the briefing default, and the
    # consent + tone decisions (delta §3.2).
    assert "Task Manager" in body and "Meeting Note-taker" in body
    assert "morning briefing each day around 7" in body
    assert "react 🤐" in body
    assert "short and direct" in body
    assert "every message" in body

    # Confirm → purpose lands in the new bot's network.json block,
    # exactly the effectiveness-layer §4 shape — and the session moves
    # into the credential walk (M2) instead of ending.
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    assert r.completed is False
    assert r.wizard_session_id == wsid
    assert r.network_dirty is True
    assert r.phase == "ab_credentials"

    purpose = network["bots"]["nova"]["purpose"]
    assert set(purpose.keys()) == {
        "archetype", "mission", "captured", "confidence", "reviewed_at",
    }
    assert purpose["archetype"] == "research-analyst"
    assert purpose["mission"] == (
        "Watch the ecosystem and post a daily digest to the group."
    )
    assert purpose["captured"] == "declared"
    assert purpose["confidence"] == 1.0
    assert purpose["reviewed_at"]  # stamped

    # The credential ask is agenda-mode (LLM-voiced) and lists the
    # borrow candidate the seam returned.
    assert r.direct_send_message is None
    assert "admin_bot" in (r.system_append or "")

    # Session is still live, parked at credentials.
    st = _read_state(tmp_path, wsid)
    assert st.status == "in_progress"
    assert st.current_phase == "ab_credentials"
    assert st.extracted["_ab_account"] == "nova"


def test_cancel_at_plan_writes_nothing(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_plan(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="cancel", network=network,
    )
    assert r.completed is True
    assert r.network_dirty is False
    assert "nova" not in (network.get("bots") or {})
    assert "Nothing was saved" in (r.direct_send_message or "")


def test_change_feedback_at_plan_updates_and_rerenders(tmp_path):
    """The delta's back-tracking path: "actually, make it weekly" at the
    plan updates the mission in place and re-renders — no restart."""
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_plan(tmp_path, network)

    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="actually, make it a weekly digest — watch the same stuff",
        network=network,
    )
    assert r.completed is False
    assert r.phase == "ab_plan"
    body = r.direct_send_message or ""
    assert "weekly digest" in body
    assert "Updated" in body

    # Confirm persists the corrected mission (and moves to credentials).
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="looks good", network=network,
    )
    assert r.phase == "ab_credentials"
    assert network["bots"]["nova"]["purpose"]["mission"] == (
        "Watch the ecosystem and post a weekly digest to the group."
    )


def test_unparseable_plan_reply_rerenders_with_hint(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_plan(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="hmm", network=network,
    )
    assert r.completed is False
    assert r.phase == "ab_plan"
    assert "didn't catch" in (r.direct_send_message or "")


def test_name_taken_routes_back_to_naming(tmp_path):
    """Confirming a plan whose account name already exists on the pod
    sends the conversation back to naming instead of clobbering."""
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path, members=("admin_bot", "nova"))
    wsid, _ = _drive_to_plan(tmp_path, network)

    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    assert r.completed is False
    assert r.phase == "ab_name"
    assert "already in use" in (r.system_append or "")
    assert "purpose" not in (network.get("bots") or {}).get("nova", {})

    # Pick a fresh name → straight back to the plan with it.
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="ok — Lumen then", network=network,
    )
    assert r.phase == "ab_plan"
    assert "Lumen" in (r.direct_send_message or "")

    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    assert r.phase == "ab_credentials"
    assert "purpose" in network["bots"]["lumen"]


def test_replanning_a_planned_bot_replaces_its_purpose(tmp_path):
    """A purpose-only bot block from a prior add-bot run is a *planned*
    bot, not a deployed one — re-planning the same name must not bounce
    to name-taken; the confirm replaces the purpose."""
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    network["bots"] = {
        "nova": {"purpose": {
            "archetype": "personal-assistant",
            "mission": "Old plan.",
            "captured": "declared",
            "confidence": 1.0,
            "reviewed_at": "2026-06-01T00:00:00Z",
        }},
    }
    wsid, _ = _drive_to_plan(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    assert r.phase == "ab_credentials"
    purpose = network["bots"]["nova"]["purpose"]
    assert purpose["archetype"] == "research-analyst"
    assert "daily digest" in purpose["mission"]


def test_no_user_profile_commit_on_finalize(tmp_path, monkeypatch):
    """The add-bot chain plans/builds a bot; it must never write a user
    profile for the caller (that's the setup wizard's job). Exercised
    through a full terminal path: confirm → skip the key → wrap."""
    from evolve_admin.evo.wizard import engine
    from evolve_admin.evo.wizard import user_profile_writer as _writer

    called = []
    monkeypatch.setattr(
        _writer, "commit", lambda **kw: called.append(kw),
    )
    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_plan(tmp_path, network)
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="skip", network=network,
    )
    assert r.completed is True
    assert called == []


# ─────────────────────────────────────────────────────────────────────────────
# M3 — starter-app proposal (AB_SHAPE)
# ─────────────────────────────────────────────────────────────────────────────


def test_shape_proposal_renders_apps_and_cost(tmp_path):
    network = _admin_network(tmp_path)
    _, r = _drive_to_shape(tmp_path, network)
    # Agenda phase: the LLM voices the proposal; nothing direct-sent.
    assert r.direct_send_message is None
    block = r.system_append or ""
    assert "Task Manager" in block and "Meeting Note-taker" in block
    assert "$0.80" in block  # 2 apps × $0.40 mid estimate
    st = _read_state(tmp_path, r.wizard_session_id)
    assert [a["pkg_id"] for a in st.extracted["ab_pack_apps"]] == [
        _PKG_TASK_MANAGER, _PKG_NOTE_TAKER,
    ]


def test_shape_drop_by_name_then_accept(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_shape(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="drop the note-taker", network=network,
    )
    assert r.phase == "ab_shape"
    assert "Updated" in (r.system_append or "")
    st = _read_state(tmp_path, wsid)
    assert [a["name"] for a in st.extracted["ab_pack_apps"]] == ["Task Manager"]
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    assert r.phase == "ab_briefing"


def test_shape_full_name_match_disambiguates(tmp_path):
    """"drop the pre-meeting brief" must hit Pre-Meeting Brief only —
    never also Meeting Note-taker via the shared word "meeting"."""
    from evolve_admin.evo.wizard import engine
    from evolve_admin.evo.wizard import pack as pk

    def loader(archetype, shared_dir):
        return pk.StarterPack(
            bundle_pkg_id="p-stub9999", bundle_name="PM set",
            apps=[
                pk.PackApp(pkg_id="p-aaa00001", name="Pre-Meeting Brief"),
                pk.PackApp(pkg_id="p-bbb00002", name="Meeting Note-taker"),
            ],
        )
    pk.set_pack_loader(loader)
    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_shape(tmp_path, network)
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="drop the pre-meeting brief", network=network,
    )
    st = _read_state(tmp_path, wsid)
    assert [a["name"] for a in st.extracted["ab_pack_apps"]] == [
        "Meeting Note-taker",
    ]


def test_shape_negated_want_drops(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_shape(tmp_path, network)
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="I don't want the task manager", network=network,
    )
    st = _read_state(tmp_path, wsid)
    assert [a["name"] for a in st.extracted["ab_pack_apps"]] == [
        "Meeting Note-taker",
    ]


def test_shape_none_starts_with_no_apps(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_shape(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="no apps", network=network,
    )
    assert r.phase == "ab_briefing"
    st = _read_state(tmp_path, wsid)
    assert st.extracted["ab_pack_apps"] == []


def test_shape_question_changes_nothing(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_shape(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="what does the task manager do?", network=network,
    )
    assert r.phase == "ab_shape"
    st = _read_state(tmp_path, wsid)
    assert len(st.extracted["ab_pack_apps"]) == 2  # untouched


def test_shape_no_pack_is_honest_and_advances(tmp_path):
    """§5 + §10 OQ4: archetypes without a pack get an honest 'no starter
    set' turn — never padding — and any reply moves along."""
    from evolve_admin.evo.wizard import engine
    from evolve_admin.evo.wizard import pack as pk

    pk.set_pack_loader(lambda archetype, shared_dir: None)
    network = _admin_network(tmp_path)
    wsid, r = _drive_to_shape(tmp_path, network)
    block = r.system_append or ""
    assert "isn't in the app gallery as a ready-made set yet" in block
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="ok, carry on", network=network,
    )
    assert r.phase == "ab_briefing"
    st = _read_state(tmp_path, wsid)
    assert st.extracted["ab_pack_apps"] == []


def test_shape_cancel_ends_cleanly(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_shape(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="cancel", network=network,
    )
    assert r.completed is True
    assert "Nothing was saved" in (r.direct_send_message or "")


# ─────────────────────────────────────────────────────────────────────────────
# M3 — morning-briefing default (AB_BRIEFING)
# ─────────────────────────────────────────────────────────────────────────────


def test_briefing_yes_records_on(tmp_path):
    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_plan(tmp_path, network, briefing_reply="yes please")
    st = _read_state(tmp_path, wsid)
    assert st.extracted["ab_briefing"] == "on"


def test_briefing_decline_records_off_and_plan_shows_it(tmp_path):
    network = _admin_network(tmp_path)
    wsid, r = _drive_to_plan(tmp_path, network, briefing_reply="no thanks")
    st = _read_state(tmp_path, wsid)
    assert st.extracted["ab_briefing"] == "off"
    assert "no morning briefing" in (r.direct_send_message or "")


def test_briefing_gibberish_reprompts(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_shape(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    assert r.phase == "ab_briefing"
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="hmm, the weather?", network=network,
    )
    assert r.phase == "ab_briefing"
    assert "yes or no" in (r.system_append or "")


def test_briefing_skipped_without_surface(tmp_path):
    """Delta §3.2: the briefing turn is only offered when the bot has a
    place to deliver to — no surface, no turn, recorded off."""
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    r = _dispatch_add_bot(network)
    wsid = r.wizard_session_id
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="I want a bot to watch the ecosystem for research.",
        network=network,
    )
    # Audience without any surface keyword.
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="A group of enthusiasts, and me — same content.",
        network=network,
    )
    assert r.phase == "ab_purpose"
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="Yes — watch and research.", network=network,
    )
    assert r.phase == "ab_shape"
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    assert r.phase == "ab_consent_tone"
    st = _read_state(tmp_path, wsid)
    assert st.extracted["ab_briefing"] == "off"


# ─────────────────────────────────────────────────────────────────────────────
# Channel offer (AB_CHANNEL) — offer-now / auto-activate-later
# ─────────────────────────────────────────────────────────────────────────────


_TG_TOKEN = "123456789:" + "Aa0_-" * 7  # BotFather shape, 35 token chars


def _drive_to_channel(tmp_path, network, *, briefing_reply="yes"):
    from evolve_admin.evo.wizard import engine

    wsid, _ = _drive_to_shape(tmp_path, network)
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message=briefing_reply, network=network,
    )
    assert r.phase == "ab_channel"
    return wsid, r


def test_channel_offer_fires_for_telegram_surface(tmp_path):
    """The offer turn renders after the briefing decision when the
    captured surface is Telegram — with the paste-or-skip copy."""
    network = _admin_network(tmp_path)
    _, r = _drive_to_channel(tmp_path, network)
    block = r.system_append or ""
    assert "@BotFather" in block
    assert "skip" in block.lower()


def test_channel_offer_skipped_for_non_telegram_surface(tmp_path):
    """A surface without an in-conversation connect (Slack today) goes
    straight to consent — the wrap's auto-activate copy covers it."""
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    r = _dispatch_add_bot(network)
    wsid = r.wizard_session_id
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="I want a bot to watch the ecosystem for research.",
        network=network,
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="A group of enthusiasts on slack — same content.",
        network=network,
    )
    assert r.phase == "ab_purpose"
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="Yes — watch and research.", network=network,
    )
    assert r.phase == "ab_shape"
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    assert r.phase == "ab_consent_tone"


def test_channel_token_paste_stashes_and_never_lands_in_state(tmp_path):
    """A validated token is held in memory only: the state records the
    choice, the state FILE on disk never contains the secret — the same
    rule as the API key at AB_CREDENTIALS."""
    from evolve_admin.evo.wizard import channel_driver as chd
    from evolve_admin.evo.wizard import engine
    from evolve_admin.evo.wizard import state as _state

    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_channel(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message=f"here you go: {_TG_TOKEN}", network=network,
    )
    assert r.phase == "ab_consent_tone"
    st = _read_state(tmp_path, wsid)
    assert st.extracted["ab_channel_connect"] == "ready"
    assert st.extracted["_ab_channel"] == "telegram"
    assert st.extracted["_ab_channel_username"] == "NovaBot"
    # The secret is in the stash, not the state file.
    raw = _state.state_path(tmp_path, "admin_bot", wsid).read_text()
    assert _TG_TOKEN not in raw
    assert chd.pop_token("admin_bot", wsid)["token"] == _TG_TOKEN


def test_channel_invalid_token_reprompts(tmp_path):
    from evolve_admin.evo.wizard import channel_driver as chd
    from evolve_admin.evo.wizard import engine

    chd.set_verify_token(lambda token: {
        "ok": False, "error": "unauthorized", "bot_username": None,
    })
    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_channel(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message=_TG_TOKEN, network=network,
    )
    assert r.phase == "ab_channel"
    assert "didn't accept that token" in (r.system_append or "")
    st = _read_state(tmp_path, wsid)
    assert "ab_channel_connect" not in st.extracted


def test_channel_skip_is_first_class(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_channel(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="don't have it", network=network,
    )
    assert r.phase == "ab_consent_tone"
    st = _read_state(tmp_path, wsid)
    assert st.extracted["ab_channel_connect"] == "skipped"


def test_channel_question_reprompts(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_channel(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="what's a token?", network=network,
    )
    assert r.phase == "ab_channel"
    assert "paste" in (r.system_append or "").lower()


def test_cancel_after_token_paste_drops_the_stash(tmp_path):
    """A cancelled plan leaves no secret behind: the in-memory token
    stash is cleared with the session."""
    from evolve_admin.evo.wizard import channel_driver as chd
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_plan(tmp_path, network, channel_reply=_TG_TOKEN)
    assert chd._stash  # token held for finalize
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="cancel", network=network,
    )
    assert r.completed is True
    assert chd.pop_token("admin_bot", wsid) is None


# ─────────────────────────────────────────────────────────────────────────────
# M3 — consent + tone (AB_CONSENT_TONE)
# ─────────────────────────────────────────────────────────────────────────────


def test_consent_tone_holds_until_answered(tmp_path):
    """Non-skippable: vague replies don't advance the phase."""
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_shape(tmp_path, network)
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="skip", network=network,
    )
    assert r.phase == "ab_consent_tone"
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="whatever you think is fine", network=network,
    )
    assert r.phase == "ab_consent_tone"  # no answers extracted, no advance


# ─────────────────────────────────────────────────────────────────────────────
# M3 — finalize defaults: installs queued, guide written, briefing recorded
# ─────────────────────────────────────────────────────────────────────────────


def _drive_to_built(
    tmp_path, network, driver_seams, *,
    briefing_reply="yes", channel_reply="skip",
):
    """Full happy path: plan → confirm → paste key → build succeeds →
    poll turn finalizes. Returns (wsid, final TurnResult)."""
    from evolve_admin.evo.wizard import engine

    driver_seams.set_provision_runner(_runner_success())
    wsid, _ = _drive_to_plan(
        tmp_path, network,
        briefing_reply=briefing_reply, channel_reply=channel_reply,
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    assert r.phase == "ab_credentials"
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="sk-ant-api03-test1234567890", network=network,
    )
    assert r.phase == "ab_provision"
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="status?", network=network,
    )
    assert r.completed is True
    return wsid, r


def test_finalize_queues_pack_plus_briefing_with_consent_seeds(
    tmp_path, driver_seams, pack_seams,
):
    """The proof-artifact path (delta §9 M3): starter pack installed via
    the dependency machinery, briefing queued (default-on), consent
    seeded onto every app's job, guide written, decision recorded."""
    from evolve_admin.applications.forge_jobs import load_job

    network = _admin_network(tmp_path)
    _, r = _drive_to_built(tmp_path, network, driver_seams)

    # Installs ran sequentially, foundations first, briefing (and its
    # calendar foundation) folded in because the default stayed on.
    job_ids = [job_id for (_sd, job_id, bot) in pack_seams]
    bots = {bot for (_sd, _j, bot) in pack_seams}
    assert bots == {"nova"}
    jobs = [load_job(j, tmp_path) for j in job_ids]
    assert [j.pkg_id for j in jobs] == [
        _PKG_TASK_MANAGER, _PKG_NOTE_TAKER,
        _PKG_CALENDAR_SYNC, _PKG_MORNING_BRIEFING,
    ]
    # Consent answers ride every install job (manifest v24 seeds).
    for j in jobs:
        seed = j.context_snapshot.get("privacy_seed")
        assert seed is not None
        assert "🤐" in seed["consent_notice"]
        assert seed["opt_out_command"]
        scoping = j.context_snapshot.get("audience_scoping_seed")
        assert scoping["operator"] == "open"
        assert scoping["approved_surfaces"] == ["telegram_group"]
        assert j.context_snapshot.get("build_spec")  # forge needs the spec

    # Briefing decision recorded on the new bot's network block.
    briefing = network["bots"]["nova"]["briefing"]
    assert briefing["enabled"] is True
    assert briefing["time"] == "07:00"
    assert briefing["decided_at"]
    assert r.network_dirty is True

    # Ground rules in the bot guide: tone + audience frontmatter,
    # consent posture in the body.
    from evolve_admin.evo import guide as _guide
    g = _guide.read_guide(tmp_path, "nova")
    assert g is not None
    assert g.frontmatter["tone"] == "short and direct, no emoji"
    assert "group of enthusiasts" in g.frontmatter["audience"]
    assert "🤐" in g.body
    assert "every message" in g.body

    # The mission + briefing time reach the briefing job's build spec
    # (M4 finding 8b — the {mission} pass-through).
    briefing_job = next(j for j in jobs if j.pkg_id == _PKG_MORNING_BRIEFING)
    spec = briefing_job.context_snapshot["build_spec"]
    assert "Operator-provided settings" in spec
    assert "Watch the ecosystem and post a daily digest" in spec
    assert "delivery_time: 07:00" in spec

    # The wrap reports all of it, honestly and in plain words. The
    # channel was skipped, so the briefing promise is the auto-activate
    # one — never "lands by 7" on a bot with no place to chat.
    body = r.direct_send_message or ""
    assert "being built now" in body
    assert "Morning Briefing" in body
    assert "morning briefing is saved and ready" in body
    assert "switches itself on" in body
    assert "lands by 7" not in body
    assert "ground rules" in body


def test_finalize_optout_run_honors_decline(
    tmp_path, driver_seams, pack_seams,
):
    """The second proof artifact: a run with the briefing declined — no
    briefing app queued, the decline recorded, nothing nags."""
    from evolve_admin.applications.forge_jobs import load_job

    network = _admin_network(tmp_path)
    _, r = _drive_to_built(
        tmp_path, network, driver_seams, briefing_reply="no thanks",
    )

    jobs = [load_job(j, tmp_path) for (_sd, j, _b) in pack_seams]
    pkgs = [j.pkg_id for j in jobs]
    assert _PKG_MORNING_BRIEFING not in pkgs
    assert _PKG_CALENDAR_SYNC not in pkgs  # only rode along for the briefing
    assert pkgs == [_PKG_TASK_MANAGER, _PKG_NOTE_TAKER]

    briefing = network["bots"]["nova"]["briefing"]
    assert briefing["enabled"] is False
    body = r.direct_send_message or ""
    assert "No morning briefing, as you asked" in body
    assert "nothing will nag you" in body


def test_finalize_briefing_already_in_pack_not_duplicated(
    tmp_path, driver_seams, pack_seams,
):
    """A pack that already carries the briefing app (the EA-pack case)
    must not queue it twice when the default stays on."""
    from evolve_admin.evo.wizard import pack as pk
    from evolve_admin.applications.forge_jobs import load_job

    def loader(archetype, shared_dir):
        return pk.StarterPack(
            bundle_pkg_id="p-stub5678", bundle_name="EA set",
            apps=[
                pk.PackApp(pkg_id=_PKG_CALENDAR_SYNC, name="Calendar Sync"),
                pk.PackApp(pkg_id=_PKG_MORNING_BRIEFING, name="Morning Briefing"),
            ],
        )
    pk.set_pack_loader(loader)
    network = _admin_network(tmp_path)
    _drive_to_built(tmp_path, network, driver_seams)
    pkgs = [load_job(j, tmp_path).pkg_id for (_sd, j, _b) in pack_seams]
    assert pkgs == [_PKG_CALENDAR_SYNC, _PKG_MORNING_BRIEFING]


def test_finalize_connects_channel_and_promises_honestly(
    tmp_path, driver_seams, pack_seams, channel_seams,
):
    """The offer-now path end to end: token pasted in conversation,
    connected at finalize (after the installs queue), the wrap promises
    the briefing on the real channel. Group audience → no recipient
    claim, and the wrap says the remaining step out loud."""
    network = _admin_network(tmp_path)
    _, r = _drive_to_built(
        tmp_path, network, driver_seams, channel_reply=_TG_TOKEN,
    )

    assert channel_seams == [
        ("nova", _TG_TOKEN, {"bot_username": "NovaBot"}),
    ]
    body = r.direct_send_message or ""
    assert "connected to Telegram" in body
    assert "@NovaBot" in body
    assert "lands on Telegram at the next 7" in body
    # Group audience: the admin is not auto-claimed as the recipient.
    assert "primary_user" not in network["bots"]["nova"]
    assert "Set" in body and "primary user" in body
    assert "say hello to Nova on Telegram" in body


def test_finalize_claims_admin_recipient_for_single_person_bot(
    tmp_path, driver_seams, pack_seams, channel_seams,
):
    """A just-me bot created by the admin gets the admin recorded as
    its primary user on the connected channel — the delivery route the
    briefing resolves works on day one, and the wrap doesn't ask for a
    step that already happened."""
    from evolve_admin.evo.wizard import engine

    driver_seams.set_provision_runner(_runner_success())
    network = _admin_network(tmp_path)
    r = _dispatch_add_bot(network)
    wsid = r.wizard_session_id
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="I want a bot to watch the ecosystem for research.",
        network=network,
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="Just me — telegram direct messages.",
        network=network,
    )
    assert r.phase == "ab_purpose"
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="Yes — watch and research.", network=network,
    )
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    assert r.phase == "ab_channel"
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message=_TG_TOKEN, network=network,
    )
    assert r.phase == "ab_consent_tone"
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message=(
            "It keeps notes from our chats; I can say stop keeping "
            "notes. Keep it short and direct."
        ),
        network=network,
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="Nova. Let's go with Nova.", network=network,
    )
    assert r.phase == "ab_plan"
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="sk-ant-api03-test1234567890", network=network,
    )
    # The real pipeline's add-to-pod stage registers the new member; the
    # fake runner doesn't touch the network, so mirror that here — the
    # recipient claim requires membership.
    network["members"].append("nova")
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="status?", network=network,
    )
    assert r.completed is True
    assert r.network_dirty is True
    # the pod admin's recorded id, in the list shape writers emit (M1-B2)
    assert ext_ids.ids_for(
        network["bots"]["nova"]["primary_user"], "telegram"
    ) == ["9999"]
    body = r.direct_send_message or ""
    assert "lands on Telegram at the next 7" in body
    assert "primary user" not in body  # nothing left to ask


def test_finalize_lost_token_is_said_plainly(
    tmp_path, driver_seams, pack_seams, channel_seams,
):
    """The daemon-restart case: the operator pasted a token but the
    in-memory stash didn't survive to finalize. The wrap says so —
    never a silent unkept promise; auto-activation covers the retry."""
    from evolve_admin.evo.wizard import channel_driver as chd

    network = _admin_network(tmp_path)
    driver_seams.set_provision_runner(_runner_success())
    wsid, _ = _drive_to_plan(tmp_path, network, channel_reply=_TG_TOKEN)
    # Simulate the restart: the state file survives, the stash doesn't.
    with chd._lock:
        chd._stash.clear()
    from evolve_admin.evo.wizard import engine
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="sk-ant-api03-test1234567890", network=network,
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="status?", network=network,
    )
    assert r.completed is True
    assert channel_seams == []  # nothing was connected
    body = r.direct_send_message or ""
    assert "didn't survive a restart" in body
    assert "NOT connected" in body
    assert "switches itself on" in body  # the honest briefing promise


def test_finalize_channel_failure_is_said_plainly(
    tmp_path, driver_seams, pack_seams, channel_seams,
):
    from evolve_admin.evo.wizard import channel_driver as chd

    chd.set_connector(lambda account, token, vr: (False, "config write failed"))
    network = _admin_network(tmp_path)
    _, r = _drive_to_built(
        tmp_path, network, driver_seams, channel_reply=_TG_TOKEN,
    )
    body = r.direct_send_message or ""
    assert "did NOT finish" in body
    assert "config write failed" in body
    assert "switches itself on" in body


def test_finalize_skip_run_queues_nothing(tmp_path, driver_seams, pack_seams):
    """Skipping the key still builds nothing and queues nothing — the
    M2 paused contract is unchanged by M3."""
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid, _ = _drive_to_plan(tmp_path, network)
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="skip", network=network,
    )
    assert r.completed is True
    assert pack_seams == []
    assert "briefing" not in network["bots"]["nova"]
    assert set(network["bots"]["nova"].keys()) == {"purpose"}


# ─────────────────────────────────────────────────────────────────────────────
# Copy — the Plex test, enforced on the user-visible renders
# ─────────────────────────────────────────────────────────────────────────────


def test_verbatim_renders_pass_the_plex_test():
    """Every verbatim line is a primary surface (delta §8): no internal
    jargon. Agenda blocks are LLM guidance, not user-visible — only the
    canonical renders are checked here. Dynamic fields (error details,
    smoke summaries) are exempt by construction — the fixtures keep
    them neutral so this test lints exactly the static copy."""
    from evolve_admin.evo.wizard import prompts

    extracted = {
        "ab_bot_name": "Nova",
        "ab_mission": "Watch the ecosystem and post a daily digest.",
        "ab_archetype": "research-analyst",
        "ab_audiences": ["a group of enthusiasts"],
        "ab_surface": "a Telegram group",
        "_ab_account": "nova",
        # M3 plan fields — apps, briefing, consent + tone.
        "_ab_pack": {
            "bundle_name": "Research starter set",
            "apps": [{"pkg_id": "p-x", "name": "Task Manager",
                      "blurb": "keeps tasks", "est_usd": 0.4}],
            "missing": [],
            "none_reason": "",
            "est_usd": 0.4,
            "est_minutes": 2,
        },
        "ab_pack_apps": [{"pkg_id": "p-x", "name": "Task Manager"}],
        "ab_briefing": "on",
        "ab_consent_notice": (
            "This bot saves links the group shares; react 🤐 to keep a "
            "message out."
        ),
        "ab_opt_out": "react 🤐 to a message to keep it out",
        "ab_tone": "short and direct, no emoji",
        "ab_tg_privacy": "every message",
    }
    snap_running = {
        "display_name": "Nova",
        "credential_source": "paste",
        "events": [
            ("validate_inputs", "ok", ""),
            ("create_macos_user", "ok", ""),
            ("create_openclaw_dir", "start", ""),
        ],
    }
    snap_failed = {
        "display_name": "Nova",
        "credential_source": "paste",
        "events": [
            ("validate_inputs", "ok", ""),
            ("create_macos_user", "ok", ""),
            ("openclaw_onboard", "fail", ""),
        ],
        "failed_stage": "openclaw_onboard",
        "error": "the key was not accepted",
        "rollback_log": [
            "removed /Users/nova/.openclaw",
            "deleted macOS user nova",
            "removed nova from network.json (kept the saved plan)",
        ],
    }
    snap_done = {
        "display_name": "Nova",
        "credential_source": "borrow:admin_bot",
        "events": [(s, "ok", "") for s in (
            "validate_inputs", "create_macos_user", "create_openclaw_dir",
            "openclaw_onboard", "add_bot_to_network", "deploy_bot",
            "seed_model_config",
        )],
    }
    renders = (
        prompts.render_add_bot_plan(extracted),
        prompts.render_add_bot_cancelled(),
        prompts.render_add_bot_provision_kickoff(extracted),
        prompts.render_add_bot_provision_progress(snap_running),
        prompts.render_add_bot_provision_failed(snap_failed),
        prompts.render_add_bot_provision_lost(extracted, in_members=False),
        prompts.render_add_bot_provision_lost(extracted, in_members=True),
        prompts.render_add_bot_success_wrap(extracted, snap_done),
        prompts.render_add_bot_success_wrap(
            extracted, snap_done,
            queued={
                "apps": ["Task Manager", "Morning Briefing"],
                "apps_failed": ["Calendar Sync"],
                "est_minutes": 4,
                "briefing": "on",
                "guide_written": True,
            },
        ),
        prompts.render_add_bot_success_wrap(
            extracted, snap_done,
            queued={"briefing": "off", "guide_failed": True},
        ),
        # Channel-connect wrap variants (offer-now / auto-activate-later):
        # connected (with + without a recorded recipient), the hookup
        # failure, and the restart-eaten token — each said plainly.
        prompts.render_add_bot_success_wrap(
            {**extracted, "_ab_channel_username": "NovaBot"}, snap_done,
            queued={
                "apps": ["Task Manager"], "est_minutes": 2,
                "briefing": "on", "guide_written": True,
                "channel": {"status": "connected", "channel": "telegram",
                            "recipient_recorded": True},
            },
        ),
        prompts.render_add_bot_success_wrap(
            extracted, snap_done,
            queued={
                "briefing": "on",
                "channel": {"status": "connected", "channel": "telegram",
                            "recipient_recorded": False},
            },
        ),
        prompts.render_add_bot_success_wrap(
            extracted, snap_done,
            queued={
                "briefing": "on",
                "channel": {"status": "failed", "channel": "telegram",
                            "error": "the settings write didn't land"},
            },
        ),
        prompts.render_add_bot_success_wrap(
            extracted, snap_done,
            queued={
                "briefing": "on",
                "channel": {"status": "lost", "channel": "telegram"},
            },
        ),
        prompts.render_add_bot_smoke_failed(
            extracted, summary="it is not answering yet",
        ),
        prompts.render_add_bot_quarantined(extracted, ok=True),
        prompts.render_add_bot_quarantined(extracted, ok=False, detail=""),
        prompts.render_add_bot_paused(
            extracted, reason_line="No problem — skipping the key for now.",
        ),
    )
    banned = ("provision", "forge", "manifest", "gateway", "dispatch",
              "archetype", "rsi", "onboard", "plist", "launchctl",
              "rollback", "rolled back", "dscl", "uid", "stage",
              "pipeline", "quarantine")
    for body in renders:
        low = body.lower()
        for word in banned:
            assert word not in low, f"jargon {word!r} in: {body[:160]}..."


def test_plan_render_shows_role_in_plain_words():
    from evolve_admin.evo.wizard import prompts

    body = prompts.render_add_bot_plan({
        "ab_bot_name": "Nova",
        "ab_mission": "Keep the household running.",
        "ab_archetype": "home-automation",
        "ab_audiences": ["the household"],
    })
    assert "home automation helper" in body
    assert "home-automation" not in body


# ─────────────────────────────────────────────────────────────────────────────
# M2 — credential walk (borrow / paste / skip / cancel)
# ─────────────────────────────────────────────────────────────────────────────


def _drive_to_credentials(tmp_path, network):
    """Plan + confirm: lands the session at AB_CREDENTIALS."""
    from evolve_admin.evo.wizard import engine

    wsid, _ = _drive_to_plan(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="yes", network=network,
    )
    assert r.phase == "ab_credentials"
    return wsid


def test_credentials_skip_keeps_plan_and_builds_nothing(tmp_path, driver_seams):
    from evolve_admin.evo.wizard import engine

    calls = []
    driver_seams.set_provision_runner(_runner_success(calls))
    network = _admin_network(tmp_path)
    wsid = _drive_to_credentials(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="skip for now", network=network,
    )
    assert r.completed is True
    assert calls == []  # nothing built
    body = r.direct_send_message or ""
    assert "plan is saved" in body
    assert "evo add-bot" in body
    # The planned bot survives in network.json shape (purpose only).
    assert set(network["bots"]["nova"].keys()) == {"purpose"}


def test_credentials_cancel_keeps_plan(tmp_path, driver_seams):
    from evolve_admin.evo.wizard import engine

    calls = []
    driver_seams.set_provision_runner(_runner_success(calls))
    network = _admin_network(tmp_path)
    wsid = _drive_to_credentials(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="cancel", network=network,
    )
    assert r.completed is True
    assert calls == []
    assert "purpose" in network["bots"]["nova"]


def test_credentials_paste_validates_shape(tmp_path, driver_seams):
    """A non-Claude key gets a plain correction, not a doomed build."""
    from evolve_admin.evo.wizard import engine

    calls = []
    driver_seams.set_provision_runner(_runner_success(calls))
    network = _admin_network(tmp_path)
    wsid = _drive_to_credentials(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="sk-proj-abcdef1234567890", network=network,
    )
    assert r.completed is False
    assert r.phase == "ab_credentials"
    assert calls == []
    assert "sk-ant-" in (r.system_append or "")


def test_credentials_paste_starts_build_key_never_persisted(
    tmp_path, driver_seams,
):
    from evolve_admin.evo.wizard import engine

    calls = []
    driver_seams.set_provision_runner(_runner_success(calls))
    network = _admin_network(tmp_path)
    wsid = _drive_to_credentials(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="here you go: sk-ant-api03-test1234567890",
        network=network,
    )
    assert r.phase == "ab_provision"
    assert r.completed is False
    assert "Nova" in (r.direct_send_message or "")
    # The runner got the key, in memory…
    assert len(calls) == 1
    assert calls[0]._provider_api_key == "sk-ant-api03-test1234567890"
    assert calls[0].account == "nova"
    # …and the on-disk wizard state never holds it.
    st = _read_state(tmp_path, wsid)
    state_blob = str(st.to_dict())
    assert "sk-ant-api03-test1234567890" not in state_blob
    assert st.extracted["_ab_credential_choice"] == "paste"


def test_credentials_borrow_from_candidate(tmp_path, driver_seams):
    from evolve_admin.evo.wizard import engine

    calls = []
    driver_seams.set_provision_runner(_runner_success(calls))
    network = _admin_network(tmp_path)
    wsid = _drive_to_credentials(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="use admin_bot's key", network=network,
    )
    assert r.phase == "ab_provision"
    assert len(calls) == 1
    assert calls[0]._borrow_from == "admin_bot"
    assert calls[0]._provider_api_key is None
    st = _read_state(tmp_path, wsid)
    assert st.extracted["_ab_credential_choice"] == "borrow:admin_bot"


def test_credentials_borrow_unknown_bot_corrected(tmp_path, driver_seams):
    from evolve_admin.evo.wizard import engine

    calls = []
    driver_seams.set_provision_runner(_runner_success(calls))
    network = _admin_network(tmp_path, members=("admin_bot", "keyless_bot"))
    wsid = _drive_to_credentials(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="borrow keyless_bot's key", network=network,
    )
    assert r.phase == "ab_credentials"
    assert calls == []
    assert "keyless_bot doesn't have a key I can copy" in (r.system_append or "")


def test_credentials_bare_borrow_with_single_candidate(tmp_path, driver_seams):
    from evolve_admin.evo.wizard import engine

    calls = []
    driver_seams.set_provision_runner(_runner_success(calls))
    network = _admin_network(tmp_path)
    wsid = _drive_to_credentials(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="just borrow one", network=network,
    )
    assert r.phase == "ab_provision"
    assert calls and calls[0]._borrow_from == "admin_bot"


def test_credentials_gibberish_reprompts(tmp_path, driver_seams):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid = _drive_to_credentials(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="what's a key?", network=network,
    )
    assert r.phase == "ab_credentials"
    assert r.completed is False


# ─────────────────────────────────────────────────────────────────────────────
# M2 — build phase: progress relay, success, honest failure, retry/stop
# ─────────────────────────────────────────────────────────────────────────────


def _drive_to_provision(tmp_path, network, driver_seams, runner):
    from evolve_admin.evo.wizard import engine

    driver_seams.set_provision_runner(runner)
    wsid = _drive_to_credentials(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="sk-ant-api03-test1234567890", network=network,
    )
    assert r.phase == "ab_provision"
    return wsid


def test_provision_progress_in_outcome_language(tmp_path, driver_seams):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid = _drive_to_provision(
        tmp_path, network, driver_seams, _runner_still_running(),
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="status?", network=network,
    )
    assert r.completed is False
    assert r.phase == "ab_provision"
    body = r.direct_send_message or ""
    assert "✓ Checks passed" in body
    assert "✓ Bot account created" in body
    assert "setting up its home folder" in body
    # Outcome language only — never internal stage names.
    assert "create_macos_user" not in body
    assert "openclaw" not in body.lower()


def test_provision_cancel_while_running_is_honest(tmp_path, driver_seams):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid = _drive_to_provision(
        tmp_path, network, driver_seams, _runner_still_running(),
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="cancel", network=network,
    )
    # Mid-build there's nothing safe to cancel — the wizard says so and
    # keeps reporting rather than pretending.
    assert r.completed is False
    assert "mid-build" in (r.direct_send_message or "")


def test_provision_success_wraps_with_passing_smoke(tmp_path, driver_seams):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid = _drive_to_provision(
        tmp_path, network, driver_seams, _runner_success(),
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="how's it going?", network=network,
    )
    assert r.completed is True
    assert r.wizard_session_id is None
    assert r.phase == "ab_smoke_wrap"
    body = r.direct_send_message or ""
    assert "Nova" in body
    assert "up and running" in body
    assert "✓ Bot account created" in body
    assert "final check" in body
    st = _read_state(tmp_path, wsid)
    assert st.status == "completed"
    # Job record is released.
    from evolve_admin.evo.wizard import provision_driver as pd
    assert pd.get_job("admin_bot", wsid) is None


def test_provision_failure_is_relayed_honestly(tmp_path, driver_seams):
    """Failure honesty rule (delta §3.2): what failed and what was
    cleaned up, no 'fixed itself' framing — then retry or stop."""
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid = _drive_to_provision(
        tmp_path, network, driver_seams, _runner_failure(),
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="status?", network=network,
    )
    assert r.completed is False
    assert r.phase == "ab_provision"
    body = r.direct_send_message or ""
    assert "couldn't finish building" in body
    assert "connecting it to Claude didn't work" in body
    # Truthful technical detail preserved.
    assert "bad key" in body
    # Cleanup relayed in plain words.
    assert "removed the bot's account" in body
    assert "removed its home folder setup" in body
    assert "plan is still saved" in body
    assert "**retry**" in body and "**stop**" in body


def test_provision_failure_retry_reruns_with_same_inputs(
    tmp_path, driver_seams,
):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    calls = []
    # First run fails…
    def failing_then_recording(job):
        calls.append(job)
        _runner_failure()(job)
    wsid = _drive_to_provision(
        tmp_path, network, driver_seams, failing_then_recording,
    )
    engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="status?", network=network,
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="retry", network=network,
    )
    assert r.phase == "ab_provision"
    assert len(calls) == 2
    # The retry reuses the same in-memory credential — no re-ask.
    assert calls[1]._provider_api_key == calls[0]._provider_api_key


def test_provision_failure_stop_keeps_plan(tmp_path, driver_seams):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid = _drive_to_provision(
        tmp_path, network, driver_seams, _runner_failure(),
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="stop", network=network,
    )
    assert r.completed is True
    assert "plan is saved" in (r.direct_send_message or "")


def test_provision_failure_reasserts_missing_planned_purpose(
    tmp_path, driver_seams,
):
    """Belt to the pipeline-rollback suspender: if the planned block
    didn't survive the failed run, the failure turn restores it and
    flags network as dirty."""
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid = _drive_to_provision(
        tmp_path, network, driver_seams, _runner_failure(),
    )
    network["bots"].pop("nova", None)  # simulate an interrupted rollback
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="status?", network=network,
    )
    assert r.network_dirty is True
    assert network["bots"]["nova"]["purpose"]["archetype"] == "research-analyst"


def test_provision_lost_job_is_honest_and_recovers(tmp_path, driver_seams):
    """Admin server restarted mid-build: no job record. The wizard says
    so plainly, reads what it can from network.json, and routes retry
    back through the credential walk (the key lived only in memory)."""
    from evolve_admin.evo.wizard import engine
    from evolve_admin.evo.wizard import provision_driver as pd

    network = _admin_network(tmp_path)
    wsid = _drive_to_provision(
        tmp_path, network, driver_seams, _runner_still_running(),
    )
    pd._JOBS.clear()  # the restart
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="how's it going?", network=network,
    )
    assert r.completed is False
    body = r.direct_send_message or ""
    assert "lost track" in body
    assert "not in your pod" in body
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="retry", network=network,
    )
    assert r.phase == "ab_credentials"
    assert "key again" in (r.system_append or "")


# ─────────────────────────────────────────────────────────────────────────────
# M2 — smoke wrap: never leave a broken bot
# ─────────────────────────────────────────────────────────────────────────────


def _drive_to_smoke_fail(tmp_path, network, driver_seams):
    from evolve_admin.evo.wizard import engine

    wsid = _drive_to_provision(
        tmp_path, network, driver_seams, _runner_smoke_fail(),
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="status?", network=network,
    )
    assert r.completed is False
    assert r.phase == "ab_smoke_wrap"
    body = r.direct_send_message or ""
    assert "did NOT pass its final check" in body
    assert "not answering yet" in body
    assert "try again" in body and "switch it off" in body
    return wsid


def test_smoke_fail_then_retry_passes(tmp_path, driver_seams):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid = _drive_to_smoke_fail(tmp_path, network, driver_seams)
    driver_seams.set_smoke_runner(
        lambda account: {"status": "ok", "summary": "answering now"},
    )
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="try again", network=network,
    )
    assert r.completed is True
    assert "up and running" in (r.direct_send_message or "")


def test_smoke_fail_then_switch_off_quarantines(tmp_path, driver_seams):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    quarantined = []
    driver_seams.set_quarantine_runner(
        lambda account: (quarantined.append(account) or True, "stopped"),
    )
    wsid = _drive_to_smoke_fail(tmp_path, network, driver_seams)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="switch it off", network=network,
    )
    assert r.completed is True
    assert quarantined == ["nova"]
    assert "switched off" in (r.direct_send_message or "")


def test_smoke_fail_cancel_also_quarantines(tmp_path, driver_seams):
    """Cancel at the smoke gate may not silently leave a broken bot
    running — it counts as switch-off."""
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    quarantined = []
    driver_seams.set_quarantine_runner(
        lambda account: (quarantined.append(account) or True, "stopped"),
    )
    wsid = _drive_to_smoke_fail(tmp_path, network, driver_seams)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="cancel", network=network,
    )
    assert r.completed is True
    assert quarantined == ["nova"]


def test_smoke_fail_quarantine_failure_reported_honestly(
    tmp_path, driver_seams,
):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    driver_seams.set_quarantine_runner(lambda account: (False, "no perms"))
    wsid = _drive_to_smoke_fail(tmp_path, network, driver_seams)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="switch it off", network=network,
    )
    assert r.completed is True
    body = r.direct_send_message or ""
    assert "could NOT confirm it stopped" in body


# ─────────────────────────────────────────────────────────────────────────────
# M2 — driver plumbing
# ─────────────────────────────────────────────────────────────────────────────


def test_driver_snapshot_never_exposes_credentials(driver_seams):
    from evolve_admin.evo.wizard import provision_driver as pd

    job = pd.start(
        bot_id="admin_bot", user_key="pod:tester", account="nova",
        display_name="Nova", provider_api_key="sk-ant-secret123456",
    )
    snap = job.snapshot()
    assert "sk-ant-secret123456" not in str(snap)


def test_driver_seam_defaults_resolve_real_targets():
    """Guard test (per the subprocess-patch-fakes lesson): the seam
    defaults lazily import these names — if a refactor moves them, this
    fails loudly instead of the default silently breaking in prod."""
    from evolve_admin.web.wizard_routes import (  # noqa: F401
        borrow_candidates, read_provider_key_from_bot,
    )
    from evolve_admin.provisioning import provision_bot  # noqa: F401
    from evolve_admin.wizard_verify import check_agent_dry_run  # noqa: F401
    from evolve_admin.deploy import (  # noqa: F401
        _scheduler_launchctl, per_bot_gateway_plist_label,
    )
    from evolve_admin.config import (  # noqa: F401
        DEFAULT_NETWORK_CONFIG, is_planned_bot_block, load_network,
    )


def test_provision_failure_does_not_clobber_foreign_block(
    tmp_path, driver_seams,
):
    """If a parallel session registered the same name while our build
    was failing, the failure turn must NOT overwrite that full entry
    with a purpose-only block."""
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid = _drive_to_provision(
        tmp_path, network, driver_seams, _runner_failure(),
    )
    network["bots"]["nova"] = {"role": "member", "port": 19044}
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="status?", network=network,
    )
    assert r.network_dirty is False
    assert network["bots"]["nova"] == {"role": "member", "port": 19044}


def test_borrow_read_failure_renders_plainly(tmp_path, driver_seams):
    """The driver's pre-stage failure (borrowed key unreadable) renders
    in outcome language, not the internal stage key."""
    from evolve_admin.evo.wizard import engine

    def borrow_fail_runner(job):
        with job._lock:
            job.status = "failed"
            job.failed_stage = "borrow_credential"
            job.error = "could not read a usable key from admin_bot"
    network = _admin_network(tmp_path)
    driver_seams.set_provision_runner(borrow_fail_runner)
    wsid = _drive_to_credentials(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="use admin_bot's key", network=network,
    )
    assert r.phase == "ab_provision"
    r = engine.process_turn(
        tmp_path, bot_id="admin_bot", user_key=wsid,
        user_message="status?", network=network,
    )
    body = r.direct_send_message or ""
    assert "reading the key to copy didn't work" in body
    assert "borrow_credential" not in body
    assert "Nothing had been created yet" in body
