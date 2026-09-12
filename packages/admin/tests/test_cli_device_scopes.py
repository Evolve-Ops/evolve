"""Regression tests for the CLI device-scope invariant (oc_cli_device).

Incident: the OC 2026.6 upgrade narrowed every bot's own CLI device to
``["operator.read"]`` in ``~/.openclaw/devices/paired.json`` — `openclaw
message send` + defer fires died pod-wide and the approval flow couldn't
self-serve (spec-gallery-delivery-convention-2026-06-11.md §6 step 0).

Run with: python3 -m pytest packages/admin/tests/test_cli_device_scopes.py -v
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import oc_cli_device as ocd  # noqa: E402
from evolve_admin.runtime import FakeScheduler, set_scheduler  # noqa: E402

# Real values captured from a live pod bot (public material only) — pins
# the derivation chain against what the OC 2026.6 gateway actually wrote.
LIVE_PUB_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VwAyEAps9rop9i+T762vL601H5n6pEAjzJ2IDPR4MzN96uHV4=\n"
    "-----END PUBLIC KEY-----\n"
)
LIVE_PUBLIC_KEY_B64URL = "ps9rop9i-T762vL601H5n6pEAjzJ2IDPR4MzN96uHV4"
LIVE_DEVICE_ID = "b646a88826485ff3c0177c7f120cc9e0bca554cdca02e62f5b1755b82950ddef"

FULL = list(ocd.CLI_DEVICE_SCOPES)
NARROWED = ["operator.read"]


def _cli_entry(scopes=None, approved=None, token_scopes=None, **extra) -> dict:
    entry = {
        "deviceId": LIVE_DEVICE_ID,
        "publicKey": LIVE_PUBLIC_KEY_B64URL,
        "platform": "darwin",
        "clientId": "cli",
        "clientMode": "probe",
        "role": "operator",
        "roles": ["operator"],
        "scopes": list(NARROWED if scopes is None else scopes),
        "approvedScopes": list(NARROWED if approved is None else approved),
        "tokens": {
            "operator": {
                "token": "tok",
                "role": "operator",
                "scopes": list(NARROWED if token_scopes is None else token_scopes),
                "createdAtMs": 1,
            }
        },
        "createdAtMs": 1,
        "approvedAtMs": 2,
    }
    entry.update(extra)
    return entry


# ── Fake privileged-op runner (the set_runner seam) ───────────────────────────

class FakeRunner:
    """Emulates the sudo cp/chown/chmod/mkdir/cat ritual on the real fs."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, capture_output=True, text=True, timeout=10):
        self.calls.append(list(argv))
        assert argv[0] == "sudo", f"non-sudo argv hit the runner: {argv}"
        cmd = Path(argv[1]).name
        rc, out, err = 0, "", ""
        if cmd == "cp":
            if argv[2].startswith("/tmp/"):
                # Pin the staging hygiene: the staged payload can hold an
                # Ed25519 PRIVATE key, so it must never be group/world-
                # readable while it sits in /tmp.
                mode = Path(argv[2]).stat().st_mode & 0o777
                assert mode == 0o600, f"staged file mode {oct(mode)} != 0600"
            shutil.copy(argv[2], argv[3])
        elif cmd == "mkdir":
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
        elif cmd in ("chown", "chmod"):
            pass  # ownership is meaningless inside tmp_path
        elif cmd == "cat":
            p = Path(argv[2])
            if p.exists():
                out = p.read_text()
            else:
                rc, err = 1, f"cat: {p}: No such file or directory"
        else:
            raise AssertionError(f"unexpected privileged command: {argv}")
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)


@pytest.fixture()
def bot_env(tmp_path, monkeypatch):
    """tmp .openclaw tree + runner/scheduler/resolver seams, all restored."""
    oc_dir = tmp_path / ".openclaw"
    oc_dir.mkdir()
    runner = FakeRunner()
    prev_runner = ocd.set_runner(runner)
    prev_resolver = ocd.set_oc_dir_resolver(lambda bot_user: oc_dir)
    scheduler = FakeScheduler()
    set_scheduler(scheduler)
    yield oc_dir, runner, scheduler
    ocd.set_runner(prev_runner)
    ocd.set_oc_dir_resolver(prev_resolver)
    set_scheduler(None)


def _write_paired(oc_dir: Path, paired: dict | str) -> Path:
    devices = oc_dir / "devices"
    devices.mkdir(exist_ok=True)
    path = devices / "paired.json"
    path.write_text(paired if isinstance(paired, str) else json.dumps(paired))
    return path


def _plant_identity(oc_dir: Path) -> dict:
    """Generate a valid CLI identity and write it where the CLI keeps it.

    The full-invariant tests need one: 'healthy' means the *current*
    identity has an approved entry, so a paired.json keyed to a foreign
    deviceId (e.g. the LIVE fixture) reads as seed-drift by design.
    """
    identity = ocd.generate_identity(now_ms=1)
    identity_dir = oc_dir / "identity"
    identity_dir.mkdir(exist_ok=True)
    (identity_dir / "device.json").write_text(json.dumps(identity))
    return identity


# ── derivation chain (pinned to live-pod values) ─────────────────────────────

class TestKeyDerivation:
    def test_raw_key_extraction_matches_oc(self):
        raw = ocd.public_key_raw_from_pem(LIVE_PUB_PEM)
        assert ocd._b64url(raw) == LIVE_PUBLIC_KEY_B64URL

    def test_fingerprint_matches_oc_device_id(self):
        assert ocd.fingerprint_public_key(LIVE_PUB_PEM) == LIVE_DEVICE_ID

    def test_non_ed25519_pem_rejected(self):
        with pytest.raises(ValueError):
            ocd.public_key_raw_from_pem(
                "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"
            )

    def test_generated_identity_is_self_consistent(self):
        identity = ocd.generate_identity(now_ms=123)
        assert identity["version"] == 1
        assert identity["deviceId"] == ocd.fingerprint_public_key(identity["publicKeyPem"])
        assert ocd._identity_shape_ok(identity)
        assert ocd._identity_keypair_ok(identity)

    def test_identity_with_wrong_device_id_invalid(self):
        identity = ocd.generate_identity(now_ms=123)
        identity["deviceId"] = "f" * 64
        assert not ocd._identity_shape_ok(identity)

    def test_identity_with_mismatched_keypair_invalid(self):
        # Shape passes (public half is self-consistent) but the full
        # sign/verify self-check — run only before seeding — catches it.
        a = ocd.generate_identity(now_ms=1)
        b = ocd.generate_identity(now_ms=2)
        a["privateKeyPem"] = b["privateKeyPem"]
        assert ocd._identity_shape_ok(a)
        assert not ocd._identity_keypair_ok(a)


# ── repair_cli_device (pure, targeted) ───────────────────────────────────────

class TestRepairCliDevice:
    def test_narrowed_entry_widened_in_all_three_lists(self):
        paired = {LIVE_DEVICE_ID: _cli_entry()}
        out, notes = ocd.repair_cli_device(paired, LIVE_DEVICE_ID)
        entry = out[LIVE_DEVICE_ID]
        for scopes in (
            entry["scopes"], entry["approvedScopes"],
            entry["tokens"]["operator"]["scopes"],
        ):
            assert set(FULL) <= set(scopes)
        assert len(notes) == 3

    def test_already_full_is_a_noop(self):
        paired = {LIVE_DEVICE_ID: _cli_entry(FULL, FULL, FULL)}
        out, notes = ocd.repair_cli_device(paired, LIVE_DEVICE_ID)
        assert notes == []
        assert out == paired

    def test_extra_scopes_preserved(self):
        extra = NARROWED + ["operator.admin"]
        paired = {LIVE_DEVICE_ID: _cli_entry(extra, extra, extra)}
        out, _ = ocd.repair_cli_device(paired, LIVE_DEVICE_ID)
        entry = out[LIVE_DEVICE_ID]
        assert "operator.admin" in entry["scopes"]
        assert set(FULL) <= set(entry["approvedScopes"])

    def test_other_cli_devices_never_escalated(self):
        # An operator's laptop CLI deliberately approved read-only must
        # NOT be widened — only the bot's own (target) device is. Auto-
        # escalating foreign CLI devices would re-grant write+pairing on
        # every deploy against an explicit least-privilege decision.
        laptop = _cli_entry(deviceId="b" * 64)
        paired = {LIVE_DEVICE_ID: _cli_entry(), "b" * 64: laptop}
        out, notes = ocd.repair_cli_device(paired, LIVE_DEVICE_ID)
        assert out["b" * 64] == laptop
        assert all(note.startswith(LIVE_DEVICE_ID[:12]) for note in notes)

    def test_non_cli_target_entry_untouched(self):
        webui = {
            "deviceId": LIVE_DEVICE_ID, "clientId": "gateway-client",
            "role": "operator",
            "scopes": ["operator.admin"], "approvedScopes": ["operator.admin"],
        }
        out, notes = ocd.repair_cli_device({LIVE_DEVICE_ID: webui}, LIVE_DEVICE_ID)
        assert notes == []
        assert out[LIVE_DEVICE_ID] == webui

    def test_missing_target_is_a_noop(self):
        out, notes = ocd.repair_cli_device({}, LIVE_DEVICE_ID)
        assert notes == [] and out == {}

    def test_entry_without_tokens_block_tolerated(self):
        entry = _cli_entry()
        del entry["tokens"]
        out, notes = ocd.repair_cli_device({LIVE_DEVICE_ID: entry}, LIVE_DEVICE_ID)
        assert set(FULL) <= set(out[LIVE_DEVICE_ID]["scopes"])
        assert "tokens" not in out[LIVE_DEVICE_ID]

    def test_input_dict_not_mutated(self):
        paired = {LIVE_DEVICE_ID: _cli_entry()}
        before = json.dumps(paired, sort_keys=True)
        ocd.repair_cli_device(paired, LIVE_DEVICE_ID)
        assert json.dumps(paired, sort_keys=True) == before


# ── check_cli_device_scopes (read-only assessment) ───────────────────────────

class TestCheck:
    def test_not_bootstrapped_is_informational_pass(self, bot_env):
        oc_dir, _, _ = bot_env
        shutil.rmtree(oc_dir)
        chk = ocd.check_cli_device_scopes("team-bot-a")
        assert chk.ok and not chk.fixable

    def test_full_scopes_pass(self, bot_env):
        oc_dir, _, _ = bot_env
        identity = _plant_identity(oc_dir)
        _write_paired(oc_dir, {
            identity["deviceId"]: _cli_entry(FULL, FULL, FULL,
                                             deviceId=identity["deviceId"]),
        })
        chk = ocd.check_cli_device_scopes("team-bot-a")
        assert chk.ok

    def test_narrowed_scopes_flag_repair(self, bot_env):
        oc_dir, _, _ = bot_env
        identity = _plant_identity(oc_dir)
        _write_paired(oc_dir, {
            identity["deviceId"]: _cli_entry(deviceId=identity["deviceId"]),
        })
        chk = ocd.check_cli_device_scopes("team-bot-a")
        assert not chk.ok and chk.fixable and chk.needs_repair
        assert not chk.needs_seed

    def test_missing_file_flags_seed(self, bot_env):
        chk = ocd.check_cli_device_scopes("team-bot-a")
        assert not chk.ok and chk.fixable and chk.needs_seed

    def test_no_cli_entry_flags_seed(self, bot_env):
        oc_dir, _, _ = bot_env
        _plant_identity(oc_dir)
        _write_paired(oc_dir, {"x": {"clientId": "gateway-client", "scopes": []}})
        chk = ocd.check_cli_device_scopes("team-bot-a")
        assert not chk.ok and chk.needs_seed

    def test_rotated_identity_with_only_stale_entries_flags_seed(self, bot_env):
        # A full-scope entry for an OLD identity isn't health: the current
        # identity would still file doomed pairing requests on every CLI use.
        oc_dir, _, _ = bot_env
        _plant_identity(oc_dir)  # current identity, unpaired
        _write_paired(oc_dir, {LIVE_DEVICE_ID: _cli_entry(FULL, FULL, FULL)})
        chk = ocd.check_cli_device_scopes("team-bot-a")
        assert not chk.ok and chk.needs_seed and not chk.needs_repair

    def test_narrowed_laptop_cli_does_not_drift(self, bot_env):
        # The invariant is anchored on the bot's OWN identity; a foreign
        # CLI device an operator narrowed on purpose is not drift.
        oc_dir, _, _ = bot_env
        identity = _plant_identity(oc_dir)
        did = identity["deviceId"]
        _write_paired(oc_dir, {
            did: _cli_entry(FULL, FULL, FULL, deviceId=did),
            "b" * 64: _cli_entry(deviceId="b" * 64),  # narrowed laptop CLI
        })
        chk = ocd.check_cli_device_scopes("team-bot-a")
        assert chk.ok

    def test_invalid_identity_reported_but_not_fixable(self, bot_env):
        # Present-but-invalid identity may be a future OC format — auto-
        # regenerating would fight the CLI on every pass. Drift, no apply.
        oc_dir, _, _ = bot_env
        identity = ocd.generate_identity(now_ms=1)
        identity["deviceId"] = "f" * 64
        identity_dir = oc_dir / "identity"
        identity_dir.mkdir()
        (identity_dir / "device.json").write_text(json.dumps(identity))
        chk = ocd.check_cli_device_scopes("team-bot-a")
        assert not chk.ok and not chk.fixable

    def test_foreign_record_on_current_device_id_not_fixable(self, bot_env):
        # The current identity's deviceId is occupied by a non-cli record
        # (e.g. paired under another role) — overwriting it could clobber
        # a legitimate pairing, so: drift, operator attention, no apply.
        oc_dir, _, _ = bot_env
        identity = _plant_identity(oc_dir)
        _write_paired(oc_dir, {
            identity["deviceId"]: {
                "deviceId": identity["deviceId"],
                "clientId": "gateway-client", "role": "operator",
                "scopes": ["operator.admin"],
            },
        })
        chk = ocd.check_cli_device_scopes("team-bot-a")
        assert not chk.ok and not chk.fixable

    def test_malformed_json_fails_safe(self, bot_env):
        oc_dir, _, _ = bot_env
        _write_paired(oc_dir, "{not json")
        chk = ocd.check_cli_device_scopes("team-bot-a")
        assert not chk.ok and not chk.fixable

    def test_non_dict_json_fails_safe(self, bot_env):
        oc_dir, _, _ = bot_env
        _write_paired(oc_dir, "[1, 2]")
        chk = ocd.check_cli_device_scopes("team-bot-a")
        assert not chk.ok and not chk.fixable


# ── ensure_cli_device_scopes (repair + seed + restart) ───────────────────────

class TestEnsure:
    def test_narrowed_repaired_on_disk_and_gateway_restarted(self, bot_env):
        oc_dir, _, scheduler = bot_env
        identity = _plant_identity(oc_dir)
        did = identity["deviceId"]
        path = _write_paired(oc_dir, {did: _cli_entry(deviceId=did)})
        outcome = ocd.ensure_cli_device_scopes("team-bot-a")
        assert outcome.ok and outcome.changed
        entry = json.loads(path.read_text())[did]
        for scopes in (
            entry["scopes"], entry["approvedScopes"],
            entry["tokens"]["operator"]["scopes"],
        ):
            assert set(FULL) <= set(scopes)
        assert ("restart", "ai.openclaw.team-bot-a-gateway") in scheduler.calls

    def test_already_correct_writes_nothing_restarts_nothing(self, bot_env):
        oc_dir, runner, scheduler = bot_env
        identity = _plant_identity(oc_dir)
        did = identity["deviceId"]
        _write_paired(oc_dir, {did: _cli_entry(FULL, FULL, FULL, deviceId=did)})
        outcome = ocd.ensure_cli_device_scopes("team-bot-a")
        assert outcome.ok and not outcome.changed
        assert runner.calls == []
        assert scheduler.calls == []

    def test_restart_false_defers_to_caller(self, bot_env):
        oc_dir, _, scheduler = bot_env
        identity = _plant_identity(oc_dir)
        did = identity["deviceId"]
        _write_paired(oc_dir, {did: _cli_entry(deviceId=did)})
        outcome = ocd.ensure_cli_device_scopes("team-bot-a", restart=False)
        assert outcome.ok and outcome.changed
        assert scheduler.calls == []

    def test_seed_leaves_stale_entries_alone(self, bot_env):
        # Unpaired current identity + a narrowed entry from an old
        # rotation: the seed adds the current entry and does NOT touch
        # the stale one (it may equally be a narrowed foreign CLI).
        oc_dir, _, _ = bot_env
        identity = _plant_identity(oc_dir)
        stale = _cli_entry()
        path = _write_paired(oc_dir, {LIVE_DEVICE_ID: stale})
        outcome = ocd.ensure_cli_device_scopes("team-bot-a")
        assert outcome.ok and outcome.changed
        paired = json.loads(path.read_text())
        assert paired[LIVE_DEVICE_ID] == stale
        assert paired[identity["deviceId"]]["approvedScopes"] == FULL

    def test_apply_time_reread_bails_on_malformed(self, bot_env, monkeypatch):
        # The write path re-reads paired.json at apply time; if the file
        # turned unparseable in the window (gateway mid-write, transient
        # read failure) the rewrite must be refused — coercing to {} would
        # clobber every other paired device.
        oc_dir, runner, scheduler = bot_env
        identity = _plant_identity(oc_dir)
        did = identity["deviceId"]
        path = _write_paired(oc_dir, {did: _cli_entry(deviceId=did)})
        raw = path.read_text()

        monkeypatch.setattr(
            ocd, "check_cli_device_scopes",
            lambda bot_user: ocd.CliDeviceCheck(
                ok=False, detail="narrowed", needs_repair=True),
        )
        path.write_text("{torn write")
        outcome = ocd.ensure_cli_device_scopes("team-bot-a")
        assert not outcome.ok and not outcome.changed
        assert "refusing to rewrite" in outcome.detail
        assert path.read_text() == "{torn write"
        assert runner.calls == [] and scheduler.calls == []
        path.write_text(raw)  # restore for fixture teardown symmetry

    def test_seed_from_existing_identity(self, bot_env):
        oc_dir, _, _ = bot_env
        identity = ocd.generate_identity(now_ms=1)
        identity_dir = oc_dir / "identity"
        identity_dir.mkdir()
        (identity_dir / "device.json").write_text(json.dumps(identity))

        outcome = ocd.ensure_cli_device_scopes("team-bot-a")
        assert outcome.ok and outcome.changed

        paired = json.loads((oc_dir / "devices" / "paired.json").read_text())
        entry = paired[identity["deviceId"]]
        assert entry["clientId"] == "cli"
        assert entry["publicKey"] == ocd._b64url(
            ocd.public_key_raw_from_pem(identity["publicKeyPem"]))
        assert entry["scopes"] == FULL and entry["approvedScopes"] == FULL
        # No tokens block — the gateway mints one within the approved
        # baseline on first connect (OC ensureDeviceToken contract).
        assert "tokens" not in entry
        # Identity untouched — the CLI keeps any valid identity it finds.
        assert json.loads((identity_dir / "device.json").read_text()) == identity

    def test_seed_generates_identity_when_absent(self, bot_env):
        oc_dir, _, _ = bot_env
        outcome = ocd.ensure_cli_device_scopes("team-bot-a")
        assert outcome.ok and outcome.changed

        identity = json.loads((oc_dir / "identity" / "device.json").read_text())
        assert ocd._identity_shape_ok(identity)
        assert ocd._identity_keypair_ok(identity)
        paired = json.loads((oc_dir / "devices" / "paired.json").read_text())
        assert identity["deviceId"] in paired
        assert paired[identity["deviceId"]]["approvedScopes"] == FULL

    def test_invalid_identity_never_clobbered(self, bot_env):
        # An unrecognized identity file (possibly a future OC format) must
        # not be regenerated — that would fight the CLI on every deploy.
        oc_dir, runner, scheduler = bot_env
        identity_dir = oc_dir / "identity"
        identity_dir.mkdir()
        broken = ocd.generate_identity(now_ms=1)
        broken["deviceId"] = "f" * 64
        raw = json.dumps(broken)
        (identity_dir / "device.json").write_text(raw)

        outcome = ocd.ensure_cli_device_scopes("team-bot-a")
        assert not outcome.ok and not outcome.changed
        assert (identity_dir / "device.json").read_text() == raw
        assert not (oc_dir / "devices" / "paired.json").exists()
        assert runner.calls == [] and scheduler.calls == []

    def test_seed_preserves_other_paired_devices(self, bot_env):
        oc_dir, _, _ = bot_env
        webui = {"deviceId": "x", "clientId": "gateway-client",
                 "scopes": ["operator.admin"]}
        _write_paired(oc_dir, {"x": webui})
        outcome = ocd.ensure_cli_device_scopes("team-bot-a")
        assert outcome.ok and outcome.changed
        paired = json.loads((oc_dir / "devices" / "paired.json").read_text())
        assert paired["x"] == webui
        assert len(paired) == 2

    def test_malformed_paired_json_never_rewritten(self, bot_env):
        oc_dir, runner, scheduler = bot_env
        path = _write_paired(oc_dir, "{corrupt")
        outcome = ocd.ensure_cli_device_scopes("team-bot-a")
        assert not outcome.ok and not outcome.changed
        assert path.read_text() == "{corrupt"
        assert runner.calls == [] and scheduler.calls == []

    def test_staging_files_cleaned_up(self, bot_env):
        oc_dir, runner, _ = bot_env
        _write_paired(oc_dir, {LIVE_DEVICE_ID: _cli_entry()})
        ocd.ensure_cli_device_scopes("team-bot-a")
        staged = [argv[2] for argv in runner.calls if Path(argv[1]).name == "cp"]
        assert staged, "expected a /tmp-staged cp"
        for src in staged:
            assert src.startswith("/tmp/evolve-device-"), src  # sudoers grant pin
            assert not Path(src).exists()


# ── ensure_pod_perms wiring ──────────────────────────────────────────────────

class TestDeployWiring:
    def test_check_maps_to_perm_check_shapes(self, bot_env):
        from evolve_admin.deploy import _check_cli_device_scopes

        oc_dir, _, _ = bot_env
        identity = _plant_identity(oc_dir)
        did = identity["deviceId"]
        _write_paired(oc_dir, {did: _cli_entry(FULL, FULL, FULL, deviceId=did)})
        ok_check = _check_cli_device_scopes("team-bot-a", "team-bot-a")
        assert ok_check.ok and ok_check.category == "cli-device"

        _write_paired(oc_dir, {did: _cli_entry(deviceId=did)})
        drift = _check_cli_device_scopes("team-bot-a", "team-bot-a")
        assert not drift.ok and callable(drift.apply)
        assert "widen" in drift.fix_description

    def test_malformed_has_no_apply_and_no_fix_description(self, bot_env):
        # The drift Signal embeds fix_description verbatim — advertising
        # a fix that ensure will refuse sends operators in circles.
        from evolve_admin.deploy import _check_cli_device_scopes

        oc_dir, _, _ = bot_env
        _write_paired(oc_dir, "{corrupt")
        check = _check_cli_device_scopes("team-bot-a", "team-bot-a")
        assert not check.ok and check.apply is None
        assert check.fix_description == ""

    def test_apply_repairs_and_reports_success(self, bot_env):
        from evolve_admin.deploy import _check_cli_device_scopes

        oc_dir, _, scheduler = bot_env
        identity = _plant_identity(oc_dir)
        did = identity["deviceId"]
        path = _write_paired(oc_dir, {did: _cli_entry(deviceId=did)})
        check = _check_cli_device_scopes("team-bot-a", "team-bot-a")
        assert check.apply() is True
        entry = json.loads(path.read_text())[did]
        assert set(FULL) <= set(entry["scopes"])
        assert ("restart", "ai.openclaw.team-bot-a-gateway") in scheduler.calls

    def test_probe_exception_is_informational_drift(self, bot_env, monkeypatch):
        from evolve_admin.deploy import _check_cli_device_scopes

        monkeypatch.setattr(
            ocd, "check_cli_device_scopes",
            lambda bot_user: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        check = _check_cli_device_scopes("team-bot-a", "team-bot-a")
        assert not check.ok and check.apply is None


# ── platform-aware home resolution ───────────────────────────────────────────

def test_default_oc_dir_is_not_hardcoded_to_users():
    """The invariant must fire on Linux pods too (homes under /home —
    this PR ships the /home/* sudoers grants for it). _oc_dir goes
    through evolve_config.user_home: pwd-first, then the platform
    profile's user_home_root — never a literal /Users/."""
    from platform_profile import get_profile

    resolved = ocd._oc_dir("nonexistent-bot-xyz")
    assert resolved == Path(get_profile().user_home_root) / "nonexistent-bot-xyz" / ".openclaw"


# ── seam guard ───────────────────────────────────────────────────────────────

def test_subprocess_only_reachable_through_runner_seam():
    """patch('<module>.subprocess.run') fakes break silently when code moves
    (see memory feedback); the module must route every spawn through _run so
    set_runner() interception is total. The single allowed mention is the
    seam's default binding."""
    src = Path(ocd.__file__).read_text()
    assert src.count("subprocess.run") == 1
