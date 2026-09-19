"""tests/test_canary_sync_banner.py — the Overview update banner is
canary-aware.

Spec: internal/spec-state-store-and-deploy-resilience-2026-06-10.md (canary
release pipeline) + internal/spec-release-tiers-2026-05-16.md (the tier-aware
banner this extends).

Background: under ``pod.release.mode == "canary"`` the fleet is held on the
gated ``evolve-stable`` pointer while a newer candidate soaks. The legacy
banner did a blunt per-bot ``deployed == latest`` check, so it read that
*normal* skew as "N of M bots one version behind · [Update]" and false-nagged
for the whole soak window — and its [Update] button (sysmUpgrade → /api/upgrade)
would git-pull origin tip onto the fleet checkout, racing the pipeline.

These tests pin:
  * ``release_ui_view`` / ``canary_sync_view`` — the pure server-side helpers
    that recompute "synced" against the stable pointer and expose candidate
    soak state to the UI.
  * the JS canary renderer reads release state, shows soak progress with NO
    redeploy CTA, version-keys its acknowledge, and never calls sysmUpgrade.
  * ``/api/upgrade`` hard-refuses under canary (the server-side invariant that
    makes "[Update] never races the pipeline" hold regardless of client).
  * direct-mode wiring is left untouched.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import release_manager as rm  # noqa: E402

_WEB = _ADMIN_DIR / "evolve_admin" / "web"
_OVERVIEW_JS = _WEB / "static" / "js" / "pages" / "overview.js"
_SERVER_PY = _WEB / "server.py"
_INDEX_HTML = _WEB / "index.html"


# ── Pure server-side helpers ─────────────────────────────────────────────────


def test_release_ui_view_direct_mode_is_none():
    """Direct mode returns None so the banner keeps its legacy behavior —
    nothing about non-canary pods changes."""
    cfg = rm.ReleaseConfig(mode="direct", canary_bot="", soak_minutes=60)
    assert rm.release_ui_view(cfg, None) is None
    # Even with a populated release state, direct mode opts out entirely.
    state = rm.ReleaseState(stable={"sha": "a", "version": "2026.0611.2730"})
    assert rm.release_ui_view(cfg, state) is None


def test_release_ui_view_canary_soaking_shape():
    cfg = rm.ReleaseConfig(mode="canary", canary_bot="canary_bot", soak_minutes=45)
    state = rm.ReleaseState(
        stable={"sha": "stablesha", "version": "2026.0611.2730"},
        candidate={
            "sha": "deadbeefcafe1234",
            "soak_started_at": "2026-06-11T00:00:00+00:00",
            "state": "soaking",
        },
    )
    view = rm.release_ui_view(cfg, state)
    assert view["mode"] == "canary"
    assert view["canary_bot"] == "canary_bot"
    assert view["stable_version"] == "2026.0611.2730"
    assert view["soak_minutes"] == 45
    assert view["candidate"]["state"] == "soaking"
    assert view["candidate"]["soak_started_at"] == "2026-06-11T00:00:00+00:00"
    # Short sha only — the candidate VERSION is intentionally not resolved
    # (that needs a git call on every poll).
    assert view["candidate"]["sha"] == "deadbeefcafe"
    assert "version" not in view["candidate"]


def test_release_ui_view_canary_no_candidate():
    cfg = rm.ReleaseConfig(mode="canary", canary_bot="canary_bot", soak_minutes=60)
    state = rm.ReleaseState(stable={"sha": "s", "version": "2026.0611.2730"})
    view = rm.release_ui_view(cfg, state)
    assert view["candidate"] is None
    assert view["stable_version"] == "2026.0611.2730"


def test_release_ui_view_canary_missing_state_degrades():
    """No release.json yet (None) → still a canary view, just without a
    stable version. The banner then can't recompute sync and falls back to
    hiding rather than false-nagging."""
    cfg = rm.ReleaseConfig(mode="canary", canary_bot="", soak_minutes=60)
    view = rm.release_ui_view(cfg, None)
    assert view["mode"] == "canary"
    assert view["stable_version"] is None
    assert view["candidate"] is None


def test_canary_sync_view_keys_on_stable_pointer():
    """A bot on the stable pointer is synced; behind/ahead/never-deployed
    are not. This is the crux: a bot on gated stable is correctly deployed,
    not 'behind' the soaking candidate."""
    bot_sync = {
        "team_bot_a": {"deployed_version": "2026.0611.2730"},    # on stable
        "team_bot_b": {"deployed_version": "2026.0611.2700"},  # behind stable
        "canary_bot": {"deployed_version": "2026.0611.2760"},  # canary, ahead
        "fresh": {"deployed_version": None},               # never deployed
    }
    synced = rm.canary_sync_view(bot_sync, "2026.0611.2730")
    assert synced == {
        "team_bot_a": True,
        "team_bot_b": False,
        "canary_bot": False,   # ahead != stable; banner excludes the canary bot
        "fresh": False,
    }


def test_canary_sync_view_empty_inputs():
    assert rm.canary_sync_view({}, "2026.0611.2730") == {}
    assert rm.canary_sync_view(None, "2026.0611.2730") == {}


# ── JS banner source assertions ──────────────────────────────────────────────


def _overview_src() -> str:
    return _OVERVIEW_JS.read_text(encoding="utf-8")


def test_banner_routes_to_canary_renderer_on_release_mode():
    src = _overview_src()
    # The consolidated drawer reads the server-supplied release view and branches
    # on mode: canary → _drawerCanarySection, direct → _drawerDirectSection.
    assert "_statusData.release" in src
    assert re.search(r"rel\.mode\s*===\s*'canary'", src), \
        "drawer must branch into the canary section when release.mode is canary"
    assert "function renderUpdatesDrawer(" in src
    assert "function _drawerCanarySection(" in src


def test_canary_renderer_window_exported():
    """Inline-onclick handlers added to a page module must be window-exported
    or the ESLint suppressions-baseline gate (counts only shrink) fails."""
    src = _overview_src()
    assert "window.renderUpdatesDrawer = renderUpdatesDrawer" in src
    assert "window.ackCanaryLag = ackCanaryLag" in src
    assert "window.promoteRelease = promoteRelease" in src


def test_canary_renderer_shows_soak_progress_without_redeploy_cta():
    src = _overview_src()
    # The progress path keys off candidate state and shows a single soak ETA.
    assert "soaking" in src and "checking" in src
    # Isolate the canary section body and assert (a) it shows the soak ETA from
    # the git-backed snapshot (single source of truth — not a second client-side
    # computation), and (b) it never wires the racing redeploy handler.
    m = re.search(r"function _drawerCanarySection\(.*?\n}\n", src, re.S)
    assert m, "_drawerCanarySection body not found"
    body = m.group(0)
    assert "soak_minutes_remaining" in body, \
        "canary section must show the server-computed soak ETA, not recompute it"
    assert "sysmUpgrade" not in body, \
        "canary section must NOT call sysmUpgrade — it races the release pipeline"


def test_canary_promote_button_calls_release_endpoint():
    """The promote affordance is the "Make live now" button (plain-language
    relabel of the former "Complete soak now"), shown only once Gate 1 has
    passed (soaking). It POSTs to /api/release/promote — the admin server runs
    the promote in-process as the evolve user (no sudo), replacing the old
    `sudo evolve-admin release promote` CLI hint, which failed from the in-app
    Terminal (that PTY runs as the passwordless evolve service user)."""
    src = _overview_src()
    assert "Make live now" in src
    assert "promoteRelease(" in src
    assert "/api/release/promote" in src
    # The dead-end sudo CLI hint must be gone.
    assert "sudo evolve-admin release promote" not in src


def test_canary_lag_ack_is_version_keyed():
    """Acknowledge is keyed by the stable version, so a fixed lag stays
    dismissed but a NEW stable version re-surfaces the notice."""
    src = _overview_src()
    assert "evolve.releaseAck.canary." in src
    assert "function ackCanaryLag(" in src


# ── System tab "Bot Versions" table is canary-aware ──────────────────────────


def _index_src() -> str:
    return _INDEX_HTML.read_text(encoding="utf-8")


def test_sysm_bot_versions_table_is_canary_aware():
    """The System-tab Bot Versions render must read the release overlay and,
    under canary, NOT render a per-bot ⬆ upgrade button. Regression guard for
    the "No job ID returned" bug: the canary bot (running the soaking candidate
    ahead of stable by design) was flagged "outdated ⚠" with a ⬆ that POSTed to
    /api/upgrade, which the server 409-refuses under canary — surfacing as an
    opaque "No job ID returned" in the modal."""
    src = _index_src()
    # Reads the release overlay supplied by /api/status (statData.release).
    assert "statData && statData.release" in src
    assert "const canaryMode" in src
    assert "const canaryBot" in src
    # The canary bot gets its own non-alarming badge, not "outdated".
    assert "pod-ver-canary" in src
    # Isolate the canary branch of the evolve-version cell and prove it never
    # wires the racing redeploy handler. The whole point is no ⬆ under canary.
    m = re.search(r"if \(canaryMode\) \{.*?\} else if \(ver == null\)", src, re.S)
    assert m, "canary branch of the Bot Versions render not found"
    canary_branch = m.group(0)
    assert "sysmUpgrade" not in canary_branch, \
        "System tab must NOT offer a per-bot upgrade ⬆ under canary — it races " \
        "the release pipeline and the server hard-refuses it"


def test_sysm_upgrade_surfaces_server_reason_when_no_job():
    """A refusal that returns no jobId (canary 409, concurrency guard) must
    surface the server's own `error` instead of the opaque "No job ID
    returned" — api() resolves HTTP errors as parsed JSON, it does not throw."""
    src = _index_src()
    m = re.search(r"function sysmUpgrade\(botId\) \{.*?\n}\n", src, re.S)
    assert m, "sysmUpgrade body not found"
    body = m.group(0)
    # Guards on a missing jobId and falls back to the server-supplied error.
    assert "!r.jobId" in body
    assert "r.error" in body


# ── Server-side guard + status overlay ───────────────────────────────────────


def _server_src() -> str:
    return _SERVER_PY.read_text(encoding="utf-8")


def test_canary_upgrade_block_payload():
    """`canary_upgrade_block` is the invariant behind "[Update] never races
    the pipeline" — None in direct mode, a 409 payload pointing at promote in
    canary mode."""
    assert rm.canary_upgrade_block({"pod": {"release": {"mode": "direct"}}}) is None
    assert rm.canary_upgrade_block({}) is None  # default mode = direct
    blk = rm.canary_upgrade_block(
        {"pod": {"release": {"mode": "canary", "canary_bot": "canary_bot"}}})
    assert blk is not None
    assert blk["release_mode"] == "canary"
    assert "release promote" in blk["error"]


def test_server_calls_extracted_release_helpers():
    """The hot-hazard server.py delegates to the release_manager helpers
    rather than inlining the logic (keeps the file under its no-growth cap)."""
    src = _server_src()
    assert "apply_release_status" in src
    assert "canary_upgrade_block" in src


def test_apply_release_status_direct_mode_no_overlay():
    """Direct mode: sets version + per-bot sync from get_bot_sync_status, but
    adds NO `release` key and does not recompute against a stable pointer."""
    data = {"bots": {"team_bot_a": {"role": "member"}, "team_bot_b": {"role": "member"}}}
    bot_sync = {
        "team_bot_a": {"deployed_version": "2026.0611.2730", "synced": True},
        "team_bot_b": {"deployed_version": "2026.0611.2700", "synced": False},
    }
    rm.apply_release_status(
        data, {"pod": {"release": {"mode": "direct"}}}, "/tmp/nope",
        bot_sync, "2026.0611.2760")
    assert data["evolve_current_version"] == "2026.0611.2760"
    assert data["bots"]["team_bot_a"]["evolve_synced"] is True
    assert data["bots"]["team_bot_b"]["evolve_synced"] is False
    assert data["any_bots_out_of_sync"] is True
    assert "release" not in data           # direct mode → no canary overlay
    assert "latest_release" in data        # tier metadata key always set


def test_apply_release_status_canary_recomputes_against_stable(tmp_path):
    """Canary mode: writes a stable pointer + soaking candidate to release.json,
    then proves `synced` is recomputed against the stable pointer (on-stable
    True, behind False) and the canary bot is excluded from the out-of-sync
    flag even though it runs ahead."""
    shared = tmp_path
    rm.save_release_state(
        rm.ReleaseState(
            stable={"sha": "s", "version": "2026.0611.2730"},
            candidate={"sha": "cand1234abcd", "state": "soaking",
                       "soak_started_at": "2026-06-11T00:00:00+00:00"},
        ),
        shared,
    )
    network = {"pod": {"release": {"mode": "canary", "canary_bot": "canary_bot",
                                   "soak_minutes": 60}}}
    data = {"bots": {
        "team_bot_a": {"role": "member"},   # on stable
        "team_bot_b": {"role": "member"},   # behind stable
        "canary_bot": {"role": "member"},   # ahead (soaking candidate)
    }}
    bot_sync = {
        "team_bot_a": {"deployed_version": "2026.0611.2730", "synced": False},
        "team_bot_b": {"deployed_version": "2026.0611.2700", "synced": False},
        "canary_bot": {"deployed_version": "2026.0611.2760", "synced": True},
    }
    rm.apply_release_status(data, network, shared, bot_sync, "2026.0611.2760")
    # release overlay surfaced for the banner
    assert data["release"]["mode"] == "canary"
    assert data["release"]["stable_version"] == "2026.0611.2730"
    assert data["release"]["candidate"]["state"] == "soaking"
    # synced recomputed against the STABLE pointer
    assert data["bots"]["team_bot_a"]["evolve_synced"] is True   # on stable
    assert data["bots"]["team_bot_b"]["evolve_synced"] is False  # behind stable
    assert data["bots"]["canary_bot"]["evolve_synced"] is False  # ahead != stable
    # ...but the canary bot is excluded from the out-of-sync flag; only the
    # genuinely-behind team_bot_b should trip it.
    assert data["any_bots_out_of_sync"] is True


def test_apply_release_status_canary_bot_alone_does_not_trip_out_of_sync(tmp_path):
    """Isolation: when every member is on stable and ONLY the canary runs
    ahead, the out-of-sync flag stays False — the canary's deliberate lead
    must not read as fleet drift (the false-nag this whole change fixes)."""
    rm.save_release_state(
        rm.ReleaseState(stable={"sha": "s", "version": "2026.0611.2730"}), tmp_path)
    network = {"pod": {"release": {"mode": "canary", "canary_bot": "canary_bot"}}}
    data = {"bots": {"team_bot_a": {"role": "member"},
                     "canary_bot": {"role": "member"}}}
    bot_sync = {
        "team_bot_a": {"deployed_version": "2026.0611.2730", "synced": True},
        "canary_bot": {"deployed_version": "2026.0611.2760", "synced": True},
    }
    rm.apply_release_status(data, network, tmp_path, bot_sync, "2026.0611.2760")
    assert data["bots"]["canary_bot"]["evolve_synced"] is False  # ahead != stable
    assert data["any_bots_out_of_sync"] is False  # canary excluded → no nag


def test_apply_release_status_corrupt_threads_flag_and_freezes(tmp_path):
    """D-8: a corrupt release.json must surface `release.corrupt = True` (so the
    Overview cell renders pipeline-halted instead of reading transient lag as a
    loud over-alert) WITHOUT changing the freeze behavior — corruption still
    degrades to no stable pointer, so the canary per-bot recompute is skipped
    exactly as before (read-only surfacing)."""
    # Write an unparseable release.json → load_release_state raises
    # ReleaseStateCorrupt, which apply_release_status catches.
    (rm.release_state_path(tmp_path)).write_text("{ this is not json", encoding="utf-8")
    network = {"pod": {"release": {"mode": "canary", "canary_bot": "canary_bot",
                                   "soak_minutes": 60}}}
    data = {"bots": {"team_bot_a": {"role": "member"}}}
    bot_sync = {"team_bot_a": {"deployed_version": "2026.0611.2700", "synced": False}}
    rm.apply_release_status(data, network, tmp_path, bot_sync, "2026.0611.2760")
    # corrupt flag surfaced on the overlay…
    assert data["release"]["corrupt"] is True
    # …but no stable pointer (degraded), so the canary recompute is skipped —
    # the per-bot synced value keeps its pre-canary input (freeze unchanged).
    assert data["release"]["stable_version"] is None
    assert data["bots"]["team_bot_a"]["evolve_synced"] is False


def test_apply_release_status_healthy_overlay_carries_d8_deltas(tmp_path):
    """The healthy canary overlay carries the D-8 deltas: corrupt False, the
    nested stable.promoted_at mirror, and pin None when not frozen."""
    rm.save_release_state(
        rm.ReleaseState(stable={"sha": "s", "version": "2026.0611.2730",
                                "promoted_at": "2026-06-11T00:00:00+00:00"}),
        tmp_path,
    )
    network = {"pod": {"release": {"mode": "canary", "canary_bot": "canary_bot"}}}
    data = {"bots": {"team_bot_a": {"role": "member"}}}
    bot_sync = {"team_bot_a": {"deployed_version": "2026.0611.2730", "synced": True}}
    rm.apply_release_status(data, network, tmp_path, bot_sync, "2026.0611.2760")
    assert data["release"]["corrupt"] is False
    assert data["release"]["stable"]["promoted_at"] == "2026-06-11T00:00:00+00:00"
    assert data["release"]["pin"] is None


# ── Behavioral: /api/upgrade hard-refuses under canary ───────────────────────


def _make_client(tmp_path, *, release_mode: str):
    """Flask test client over a minimal pod whose network.json sets the
    given release mode. Auth is off on a fresh shared dir and CSRF only
    applies to cookie-authed requests, so a plain POST reaches the handler.
    """
    import json as _json

    from evolve_admin.web.server import create_app

    shared = tmp_path / "shared"
    shared.mkdir()
    network = {
        "members": ["team_bot_a"],
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "sharedDir": str(shared),
        "pod": {"release": {"mode": release_mode, "canary_bot": "canary_bot"}},
    }
    net_file = tmp_path / "network.json"
    net_file.write_text(_json.dumps(network))
    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    return app.test_client()


def test_api_upgrade_returns_409_in_canary_mode(tmp_path, monkeypatch):
    """The redeploy must be refused under canary BEFORE any job spawns —
    this is the load-bearing safety property (no git-pull race)."""
    # Belt-and-suspenders: ensure the auth gate is open regardless of env.
    monkeypatch.setattr(
        "evolve_admin.web.server.admin_auth.is_auth_enabled",
        lambda *a, **kw: False,
    )
    client = _make_client(tmp_path, release_mode="canary")
    resp = client.post("/api/upgrade", json={})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body.get("release_mode") == "canary"
    assert "release promote" in body.get("error", "")


def test_api_upgrade_dry_run_exempt_from_guard_source():
    """The guard skips dry-run (it mutates nothing): pin `if not dry_run` so
    a future refactor can't accidentally block harmless previews."""
    src = _server_src()
    m = re.search(r"def api_upgrade\(\).*?job_id = _new_job", src, re.S)
    assert m, "api_upgrade preamble not found"
    preamble = m.group(0)
    assert "if not dry_run:" in preamble
