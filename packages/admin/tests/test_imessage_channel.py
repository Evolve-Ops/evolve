"""imessage_channel — the D-IM1..5 contract, run with fake time and no Mac.

Spec: internal/design-imessage-channel-2026-09-29.md. Fixtures are fictional
(placeholder names — docs/PLACEHOLDER_NAMING.md).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from evolve_admin import connections as conn
from evolve_admin import imessage_channel as ic

BOT = "personal-bot"
USER = "personal-bot-user"
HANDLE = "personal-bot@example.com"
KID = "+15555550101"
PARENT = "+15555550102"


class FakePod:
    """A pod the seams talk to: an openclaw.json, a Messages session, a
    gateway that comes up ``gateway_up_after_s`` seconds after a restart, and
    a clock that only ``sleep`` advances."""

    def __init__(self, tmp_path: Path, *, cfg: dict | None = None, signed_in_at: float = 0.0,
                 gateway_up_after_s: float = 9.0, messages_running: bool = True,
                 send_ok: bool = True):
        self.t = 0.0
        self.cfg = cfg if cfg is not None else {"channels": {"telegram": {"enabled": True, "botToken": "t"}},
                                                "plugins": {"entries": {"telegram": {"enabled": True}}}}
        self.signed_in_at = signed_in_at
        self.gateway_up_after_s = gateway_up_after_s
        self.restarted_at: float | None = None
        self.messages_running = messages_running
        self.send_ok = send_ok
        self.sent: list[tuple[str, str, str]] = []
        self.writes = 0
        self.handle_reads = 0
        self.connections = tmp_path / "connections.json"

    def seams(self) -> ic.Seams:
        def handle(_u, _b):
            self.handle_reads += 1
            return HANDLE if self.t >= self.signed_in_at else None

        def write(_b, cfg):
            self.cfg = copy.deepcopy(cfg)
            self.writes += 1
            return True, None

        def restart(_b):
            self.restarted_at = self.t
            return True, None

        def status(_b):
            up = self.restarted_at is not None and self.t >= self.restarted_at + self.gateway_up_after_s
            return {"connected": True} if up else {"connected": False, "error": "probe_failed"}

        def send(b, to, text):
            self.sent.append((b, to, text))
            return (True, None) if self.send_ok else (False, "send_failed")

        def sleep(s):
            self.t += s

        return ic.Seams(
            read_config=lambda _b: (copy.deepcopy(self.cfg), None),
            write_config=write, restart_gateway=restart,
            signed_in_handle=handle, messages_running=lambda _u: self.messages_running,
            channel_status=status, send=send,
            home_for_user=lambda u: Path("/Users") / u,
            clock=lambda: self.t, sleep=sleep, now=lambda: "2026-09-30T00:00:00+00:00",
        )


@pytest.fixture
def pod(tmp_path):
    return FakePod(tmp_path)


def _connect(pod: FakePod, allow=(KID,), **kw):
    return ic.connect(bot_id=BOT, macos_user=USER, allow_from=list(allow),
                      connections_file=pod.connections, seams=pod.seams(), **kw)


# ── row → config derivation ─────────────────────────────────────────────────


def _row(allow):
    return ic.new_row(bot_id=BOT, handle=HANDLE, macos_user=USER, allow_from=allow)


def test_derived_block_always_carries_the_allowlist_policy():
    block = ic.derive_channel_block(_row([KID, PARENT]), home_for_user=lambda u: Path("/Users") / u)
    assert block["dmPolicy"] == "allowlist"
    assert block["allowFrom"] == [KID, PARENT]
    assert block["groupPolicy"] == "disabled"
    assert block["dbPath"] == f"/Users/{USER}/Library/Messages/chat.db"
    assert "handle" not in block  # not an OC key: the channel schema is closed


def test_empty_allowlist_receives_nothing_never_open():
    block = ic.derive_channel_block(_row([]), home_for_user=lambda u: Path("/Users") / u)
    assert block["dmPolicy"] == "allowlist"
    assert block["allowFrom"] == []


def test_wildcard_and_malformed_senders_are_refused_not_dropped():
    for bad in ("*", "555-0101", "not a handle"):
        with pytest.raises(ic.ImessageChannelError):
            ic.normalize_allow_from([bad])


def test_senders_are_normalised_and_deduped():
    assert ic.normalize_allow_from(["+1 (555) 555-0101", KID, "Kid@Example.COM"]) == [KID, "kid@example.com"]


def test_merge_preserves_extras_strips_group_keys_and_stale_handle():
    cfg = {"channels": {"imessage": {"handle": "old", "mediaMaxMb": 5, "groupAllowFrom": ["*"],
                                     "groups": {"x": {}}, "accounts": {"a": {"dmPolicy": "open"}}}}}
    out = ic.merge_into_config(cfg, _row([KID]), home_for_user=lambda u: Path("/Users") / u)
    blk = out["channels"]["imessage"]
    assert blk["mediaMaxMb"] == 5
    assert not {"handle", "groupAllowFrom", "groups", "accounts"} & set(blk)
    assert out["plugins"]["entries"]["imessage"]["enabled"] is True
    assert ic.open_policy_findings(out) == []


# ── open policy ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("blk", [
    {"dmPolicy": "open"},
    {"dmPolicy": "allowlist", "allowFrom": ["*"]},
    {"dmPolicy": "allowlist", "groupPolicy": "open"},
    {"dmPolicy": "allowlist", "accounts": {"x": {"dmPolicy": "open"}}},
])
def test_open_policy_shapes_are_found(blk):
    assert ic.open_policy_findings({"channels": {"imessage": blk}})


def test_connect_refuses_when_an_open_policy_is_already_on_disk(tmp_path):
    pod = FakePod(tmp_path, cfg={"channels": {"imessage": {"dmPolicy": "open"}}})
    res = _connect(pod)
    assert (res.ok, res.stage) == (False, "open_policy")
    assert pod.writes == 0 and pod.restarted_at is None and not pod.sent


def test_connect_never_writes_an_open_policy(pod):
    assert _connect(pod).ok
    assert pod.cfg["channels"]["imessage"]["dmPolicy"] == "allowlist"


# ── handle is read back, never typed ────────────────────────────────────────


def test_connect_takes_no_handle_argument_and_uses_the_readback(pod):
    import inspect
    assert "handle" not in inspect.signature(ic.connect).parameters
    res = _connect(pod)
    assert res.row is not None and res.row["handle"] == HANDLE


def test_connect_stops_at_sign_in_when_messages_reports_no_handle(tmp_path):
    pod = FakePod(tmp_path, signed_in_at=10**9)
    res = _connect(pod)
    assert (res.ok, res.stage) == (False, "sign_in")
    assert USER in res.detail and pod.writes == 0


# ── wizard timing (fake time) ───────────────────────────────────────────────


def test_wizard_end_to_end_is_under_three_minutes(tmp_path):
    # The operator takes 45s to sign in (the by-hand step); the gateway needs
    # 20s after the restart. Sign-in wait + connect must stay inside the budget.
    pod = FakePod(tmp_path, signed_in_at=45.0, gateway_up_after_s=20.0)
    s = pod.seams()
    handle = ic.wait_for_signin(BOT, USER, seams=s)
    assert handle == HANDLE
    res = ic.connect(bot_id=BOT, macos_user=USER, allow_from=[KID],
                     connections_file=pod.connections, seams=s)
    assert res.ok, res.detail
    assert pod.t < ic.WIZARD_BUDGET_S
    assert res.first_message_to == KID
    assert pod.sent == [(BOT, KID, f"{BOT} is here — reply to say hi")]


def test_a_gateway_that_never_comes_up_gives_up_inside_the_budget(tmp_path):
    pod = FakePod(tmp_path, gateway_up_after_s=10**9)
    res = _connect(pod)
    assert (res.ok, res.stage) == (False, "probe")
    assert pod.t <= ic.CHANNEL_UP_TIMEOUT_S + ic.CHANNEL_UP_POLL_INTERVAL_S
    assert not pod.sent


def test_wait_for_signin_gives_up_at_its_timeout(tmp_path):
    pod = FakePod(tmp_path, signed_in_at=10**9)
    assert ic.wait_for_signin(BOT, USER, seams=pod.seams(), timeout_s=30) is None
    assert 30 <= pod.t < 30 + ic.SIGNIN_POLL_INTERVAL_S + 1


# ── the probe reds on each condition ────────────────────────────────────────


def _connected(pod) -> dict:
    assert _connect(pod).ok
    row = ic.get_row(BOT, pod.connections)
    assert row is not None
    return row


def _names(res):
    return {c.name for c in res.checks if not c.ok}


def test_probe_is_green_after_connect(pod):
    row = _connected(pod)
    pod.t += 60
    assert ic.probe(row, seams=pod.seams()).ok
    assert row["health"]["state"] == "verified" and row["health"]["channel_up"] is True


def test_probe_reds_when_signed_out(pod):
    row = _connected(pod)
    pod.signed_in_at = 10**9
    assert "signed_in" in _names(ic.probe(row, seams=pod.seams()))


def test_probe_reds_when_signed_in_as_a_different_account(pod):
    row = _connected(pod)
    row["handle"] = "someone-else@example.com"
    assert "signed_in" in _names(ic.probe(row, seams=pod.seams()))


def test_probe_reds_when_messages_is_not_running(pod):
    row = _connected(pod)
    pod.messages_running = False
    assert "messages_running" in _names(ic.probe(row, seams=pod.seams()))


def test_probe_reds_when_the_channel_is_down(pod):
    row = _connected(pod)
    pod.restarted_at = pod.t + 10**6
    assert "channel_up" in _names(ic.probe(row, seams=pod.seams()))


def test_probe_reds_on_drift_and_shows_the_diff(pod):
    row = _connected(pod)
    pod.cfg["channels"]["imessage"]["allowFrom"].append("+15555550199")
    res = ic.probe(row, seams=pod.seams())
    assert "config_matches_row" in _names(res)
    diff = next(c.detail for c in res.checks if c.name == "config_matches_row")
    assert "allowFrom" in diff and "+15555550199" in diff


def test_probe_reds_and_refuses_on_an_open_policy_found_on_disk(pod):
    row = _connected(pod)
    pod.cfg["channels"]["imessage"]["dmPolicy"] = "open"
    res = ic.probe(row, seams=pod.seams())
    assert res.refuse and not res.ok and "dm_policy" in _names(res)


def test_a_missing_dm_policy_is_drift_not_a_pass(pod):
    row = _connected(pod)
    del pod.cfg["channels"]["imessage"]["dmPolicy"]  # OC would default to "pairing"
    assert "config_matches_row" in _names(ic.probe(row, seams=pod.seams()))


def test_run_probe_persists_health_and_reason(pod):
    _connected(pod)
    pod.messages_running = False
    res, row = ic.run_probe(BOT, pod.connections, seams=pod.seams())
    stored = ic.get_row(BOT, pod.connections)
    assert res is not None and stored is not None
    assert not res.ok and stored["health"]["state"] == "failed"
    assert "Messages is not running" in stored["health"]["reason"]
    assert "failed: Messages is not running" in conn.health_display(stored["health"])


# ── Telegram retire offer is opt-in ─────────────────────────────────────────


def test_telegram_binding_survives_by_default(pod):
    assert _connect(pod).ok
    assert pod.cfg["channels"]["telegram"]["botToken"] == "t"
    assert pod.cfg["plugins"]["entries"]["telegram"]["enabled"] is True


def test_telegram_binding_is_retired_only_when_accepted(pod):
    res = _connect(pod, retire_telegram=True)
    assert res.ok and res.telegram_retired
    assert "telegram" not in pod.cfg["channels"]
    assert "telegram" not in pod.cfg["plugins"]["entries"]
    assert "imessage" in pod.cfg["channels"]


# ── shared mode ─────────────────────────────────────────────────────────────


def test_shared_mode_overlap_is_refused_with_the_number_named(pod):
    first = ic.new_row(bot_id="bot-a", handle=HANDLE, macos_user="pod-admin-user",
                       allow_from=[KID], apple_id_mode="shared")
    conn.add_connection(first, pod.connections)
    res = ic.connect(bot_id="bot-b", macos_user="pod-admin-user", allow_from=[KID, PARENT],
                     connections_file=pod.connections, apple_id_mode="shared", seams=pod.seams())
    assert (res.ok, res.stage) == (False, "validate")
    assert KID in res.detail and "bot-a" in res.detail
    assert pod.writes == 0


def test_shared_mode_disjoint_allowlists_connect(pod):
    conn.add_connection(ic.new_row(bot_id="bot-a", handle=HANDLE, macos_user="pod-admin-user",
                                   allow_from=[KID], apple_id_mode="shared"), pod.connections)
    res = ic.connect(bot_id="bot-b", macos_user="pod-admin-user", allow_from=[PARENT],
                     connections_file=pod.connections, apple_id_mode="shared", seams=pod.seams())
    assert res.ok and res.row is not None and res.row["apple_id_mode"] == "shared"


def test_own_mode_ignores_other_bots_allowlists(pod):
    conn.add_connection(ic.new_row(bot_id="bot-a", handle=HANDLE, macos_user="pod-admin-user",
                                   allow_from=[KID], apple_id_mode="shared"), pod.connections)
    assert _connect(pod, allow=[KID]).ok  # this bot: own Apple ID


# ── the row ─────────────────────────────────────────────────────────────────


def test_row_shape_and_capabilities(pod):
    row = _connected(pod)
    assert row["service"] == "imessage" and row["scope"] == "bot"
    assert row["grant_scope"] == "standing" and row["migrated"] is False
    assert row["apple_id_mode"] == "own" and row["host"] == "native" and row["groups"] is False
    assert set(row["capabilities"]) == {"imessage.receive", "imessage.send", "imessage.read-history"}
    assert set(row["health"]) >= {"signed_in", "channel_up", "last_inbound", "last_outbound"}
    assert row["health"]["last_outbound"] == "2026-09-30T00:00:00+00:00"
    json.dumps(row)  # the registry file is plain JSON


def test_reconnect_keeps_one_row_per_bot(pod):
    _connect(pod)
    _connect(pod, allow=(KID, PARENT))
    rows = ic.all_rows(pod.connections)
    assert len(rows) == 1 and rows[0]["allow_from"] == [KID, PARENT]


def test_groups_cannot_be_turned_on():
    with pytest.raises(ic.ImessageChannelError):
        ic.new_row(bot_id=BOT, handle=HANDLE, macos_user=USER, groups=True)


def test_empty_allowlist_connects_but_says_nobody_can_text_it(pod):
    res = _connect(pod, allow=())
    assert res.ok and not pod.sent and "receives nothing" in res.detail


def test_first_message_failure_is_reported_not_swallowed(tmp_path):
    pod = FakePod(tmp_path, send_ok=False)
    res = _connect(pod)
    assert (res.ok, res.stage) == (False, "first_message")


# ── pre-fill ────────────────────────────────────────────────────────────────


def test_prefill_uses_only_recorded_imessage_ids():
    net = {"bots": {BOT: {"primary_user": {"external_ids": {"telegram": ["111"], "imessage": [KID]}}}},
           "pod": {"admins": {"external_ids": {"imessage": [PARENT], "telegram": ["222"]}}}}
    assert ic.prefill_allow_from(net, BOT) == [KID, PARENT]
    assert ic.prefill_allow_from({"bots": {BOT: {}}}, BOT) == []


# ── keeper launch agent ─────────────────────────────────────────────────────


def test_keeper_plist_keeps_messages_open_and_writes_nothing():
    import plistlib
    spec = plistlib.loads(ic.keeper_plist_content(USER).encode())
    assert spec["Label"] == f"ai.openclaw.imessage-keeper.{USER}"
    assert spec["LimitLoadToSessionType"] == "Aqua" and spec["RunAtLoad"] is True
    assert spec["ProgramArguments"] == ["/usr/bin/open", "-g", "-a", "Messages"]
    # No shell, no status file: the sign-in is read from the pod, not a bot home.
    assert "imessage-status" not in ic.keeper_plist_content(USER)


@pytest.mark.parametrize("staged", [
    "/tmp/evolve-imsg-keeper-/../../Users/admin/Library/LaunchAgents/x.plist",
    "/tmp/../tmp/evolve-imsg-keeper-abc.plist",
    "/tmp/sub/evolve-imsg-keeper-abc.plist",
    "tmp/evolve-imsg-keeper-abc.plist",
    "/tmp/evolve-other-abc.plist",
])
def test_a_staged_keeper_path_that_could_leave_tmp_is_refused(staged):
    assert not ic.staged_keeper_path_ok(staged)


def test_the_mkstemp_name_is_an_accepted_staged_path():
    assert ic.staged_keeper_path_ok("/tmp/evolve-imsg-keeper-k2j_9x0a.plist")


def test_install_keeper_refuses_before_cp_when_the_staged_path_carries_dotdot(monkeypatch):
    import tempfile
    calls = []
    real = tempfile.mkstemp

    def evil_mkstemp(**kw):
        fd, path = real()
        return fd, "/tmp/evolve-imsg-keeper-/../../" + path.lstrip("/") + ".plist"

    monkeypatch.setattr(tempfile, "mkstemp", evil_mkstemp)
    ok, err = ic.install_keeper(USER, run=lambda argv, **_: calls.append(argv),
                                home_for_user=lambda u: Path("/Users") / u)
    assert not ok and "refused" in (err or "")
    assert calls == []  # never reached sudo cp


def test_install_keeper_stages_copies_and_bootstraps_in_the_users_domain():
    calls = []

    class R:
        returncode, stdout, stderr = 0, "501\n", ""

    def run(argv, **_):
        calls.append(argv)
        return R()

    ok, err = ic.install_keeper(USER, run=run, home_for_user=lambda u: Path("/Users") / u)
    assert ok and err is None
    flat = [" ".join(c) for c in calls]
    assert any(f.startswith("sudo /bin/cp /tmp/evolve-imsg-keeper-") for f in flat)
    assert any("bootstrap gui/501" in f for f in flat)


# ── the sign-in is read from the pod, never a bot-writable file ─────────────


def _forge_status_file(home: Path, handle: str) -> Path:
    """What the bot's own agent can do: it owns workspace/evolve."""
    f = home / ".openclaw" / "workspace" / "evolve" / "imessage-status.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"handle": handle}))
    return f


def test_a_forged_status_file_never_signs_the_bot_in(tmp_path, monkeypatch):
    from evolve_admin.skills import imessage_install as ii
    forged = _forge_status_file(tmp_path / USER, HANDLE)
    monkeypatch.setattr(ic, "user_home", lambda u: tmp_path / u)
    monkeypatch.setattr(ii, "_probe_oc_channel_status", lambda _b, **_: {"connected": False, "error": "probe_timeout"})
    assert forged.exists()
    assert ic._default_signed_in_handle(USER, BOT) is None


def test_a_forged_status_file_leaves_the_probe_red_with_the_pods_reason(tmp_path, monkeypatch):
    from evolve_admin.skills import imessage_install as ii
    _forge_status_file(tmp_path / USER, HANDLE)
    monkeypatch.setattr(ic, "user_home", lambda u: tmp_path / u)
    monkeypatch.setattr(ii, "_probe_oc_channel_status", lambda _b, **_: {"connected": False, "error": "probe_timeout"})
    row = _row([KID])
    cfg = ic.merge_into_config({}, row, home_for_user=lambda u: tmp_path / u)
    seams = ic.Seams(read_config=lambda _b: (cfg, None), messages_running=lambda _u: True,
                     channel_status=lambda _b: {"connected": False, "error": "probe_timeout"},
                     home_for_user=lambda u: tmp_path / u)
    res = ic.probe(row, seams=seams)
    signed = next(c for c in res.checks if c.name == "signed_in")
    assert not res.ok and not signed.ok
    assert "read from the pod: probe_timeout" in signed.detail


def test_the_handle_is_read_from_channels_status(monkeypatch):
    from evolve_admin.skills import imessage_install as ii
    monkeypatch.setattr(ii, "_probe_oc_channel_status", lambda _b, **_: {"connected": True, "account": "Personal-Bot@Example.com"})
    assert ic._default_signed_in_handle(USER, BOT) == HANDLE


# ── health control: known_good / known_bad replay ───────────────────────────

_CONTROL_FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "controls" / "health__check_imessage_channels"


def _replay_health_fixture(name: str, tmp_path: Path, monkeypatch):
    from evolve_admin import health
    from evolve_admin.skills import imessage_install as ii
    fx = json.loads((_CONTROL_FIXTURES / name).read_text())
    homes = lambda u: tmp_path / "home" / u  # noqa: E731
    r = fx["row"]
    row = ic.new_row(bot_id=r["bot_id"], handle=r["handle"], macos_user=r["macos_user"], allow_from=r["allow_from"])
    network = {"sharedDir": str(tmp_path / "shared"), "bots": {r["bot_id"]: {}}}
    path = conn.connections_path(network)
    path.parent.mkdir(parents=True)
    ic.save_row(row, path)
    _forge_status_file(homes(r["macos_user"]), fx["forged_status_file"]["handle"])
    monkeypatch.setattr(ic, "user_home", homes)
    # The real sign-in read-back runs; only the gateway CLI under it is faked.
    monkeypatch.setattr(ii, "_probe_oc_channel_status", lambda _b, **_: dict(fx["channel_status"]))
    cfg = ic.merge_into_config({}, row, home_for_user=homes)
    seams = ic.Seams(read_config=lambda _b: (copy.deepcopy(cfg), None),
                     messages_running=lambda _u: fx["messages_running"],
                     channel_status=lambda _b: dict(fx["channel_status"]),
                     home_for_user=homes, now=lambda: "2026-10-01T00:00:00+00:00")
    report = health.HealthReport()
    health._check_imessage_channels(report, network, seams=seams)
    (only,) = [c for c in report.checks if c.category == "channels"]
    return only, fx["expect"], health


def test_health_control_known_good_fixture_passes(tmp_path, monkeypatch):
    c, want, health = _replay_health_fixture("known_good.json", tmp_path, monkeypatch)
    assert c.status == health.PASS == want["status"]
    assert c.name == want["name"] and want["message_contains"] in c.detail


def test_health_control_known_bad_fixture_fails_despite_a_forged_status_file(tmp_path, monkeypatch):
    c, want, health = _replay_health_fixture("known_bad.json", tmp_path, monkeypatch)
    assert c.status == health.FAIL == want["status"]
    assert c.name == want["name"] and want["message_contains"] in c.detail


def test_health_control_warns_when_the_registry_cannot_be_probed(monkeypatch):
    from evolve_admin import health

    def boom(_path, **_):
        raise OSError("connections.json unreadable")

    monkeypatch.setattr(ic, "probe_all", boom)
    report = health.HealthReport()
    health._check_imessage_channels(report, {"sharedDir": "/nonexistent"})
    (only,) = [c for c in report.checks if c.category == "channels"]
    assert only.status == health.WARN and "unreadable" in only.detail
