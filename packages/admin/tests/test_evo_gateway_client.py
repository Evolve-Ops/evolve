"""tests/test_evo_gateway_client.py — admin→evo gateway CLIENT credential.

EVO-SEP-GW-CRED. Spec: docs/spec-evo-account-separation-2026-05-25.md.

After the evo-account separation the admin daemon (``evolve``) and the primary
bot's gateway run as different Unix accounts (gateway = ``evo`` on a fresh Linux
pod, or macOS after the E.2.b cutover). The admin UI's home-chat dispatches
``openclaw agent`` inheriting ``HOME=/home/evolve``, so the CLI reads
``/home/evolve/.openclaw/openclaw.json`` for its gateway CLIENT credentials — a
fresh evolve home has none → ``GatewayCredentialsRequiredError`` on every turn.
``evo_gateway_client`` writes a minimal client config whose
``gateway.auth.token`` + ``gateway.port`` MATCH the primary gateway, and
self-heals it on every deploy.

These tests pin: the evolve-OWNED 0600 write (direct, no ``sudo cp`` — the file
is under evolve's own home), the merge-don't-clobber behaviour over a macOS
post-cutover stale file, the EVO-PRIMARY gating (legacy evolve-primary and
macOS-pre-cutover are untouched), the provision wiring through
``_provision_evo_oc`` (Linux writes / macOS no-ops), the ``ensure_pod_perms``
drift→repair self-heal, idempotency, and that no detail string leaks the token.

EVO-SEP-GW-CRED-MODE (from the #3135 post-merge audit) adds: the predicate also
requires raw mode 0600 when the file is present, so a correct-token file that
drifted 0600→0644 (the deploy ``a+rX`` / Linux ACL-mask re-widen class) reads as
drift and is repaired with a TARGETED chmod (contents byte-identical, token
intact) — both at the unit level and pod-wide through ``ensure_pod_perms``.

Placeholder names per docs/PLACEHOLDER_NAMING.md; no real tokens.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin import deploy, evo_gateway_client as gw  # noqa: E402
import primary_bot  # noqa: E402


_GW_TOKEN = "gw-token-placeholder-aaaa"
_OTHER_TOKEN = "gw-token-placeholder-bbbb"


@pytest.fixture
def evolve_home(tmp_path, monkeypatch):
    """Point the module's ``evolve`` home-root at a tmp tree and return the
    evolve account's .openclaw dir (created)."""
    root = tmp_path / "home"
    monkeypatch.setattr(gw, "_user_home", lambda acct: root / acct)
    d = root / "evolve" / ".openclaw"
    d.mkdir(parents=True)
    return root


def _seed_primary(root: Path, *, user: str = "evo", token: str = _GW_TOKEN,
                  port: int = 19030) -> Path:
    """Create the primary gateway's openclaw.json under ``root/<user>``."""
    home = root / user
    ocd = home / ".openclaw"
    ocd.mkdir(parents=True, exist_ok=True)
    (ocd / "openclaw.json").write_text(json.dumps(
        {"gateway": {"port": port, "mode": "local",
                     "auth": {"mode": "token", "token": token}}}))
    return home


# ── write_evolve_gateway_client — the evolve-OWNED 0600 writer ─────────────────

class TestWriteEvolveGatewayClient:

    def test_fresh_write_lands_0600_with_token_and_port(self, evolve_home):
        assert gw.write_evolve_gateway_client(19030, _GW_TOKEN) is True
        p = gw.evolve_client_oc_path()
        cfg = json.loads(p.read_text())
        assert cfg["gateway"]["auth"]["token"] == _GW_TOKEN
        assert cfg["gateway"]["auth"]["mode"] == "token"
        assert cfg["gateway"]["port"] == 19030
        # Token-bearing → 0600 (the central invariant of this fix).
        assert stat.S_IMODE(p.stat().st_mode) == 0o600

    def test_merge_preserves_other_keys_and_reconciles_token(self, evolve_home):
        """macOS post-cutover stale file: a full config already exists. The
        writer MERGES — reconciles the gateway token, preserves everything else."""
        p = gw.evolve_client_oc_path()
        p.write_text(json.dumps({
            "gateway": {"port": 19030, "auth": {"mode": "token", "token": _OTHER_TOKEN}},
            "agents": {"defaults": {"model": "x"}},
            "plugins": {"entries": {"evolve": {"enabled": True}}},
        }))
        assert gw.write_evolve_gateway_client(19030, _GW_TOKEN) is True
        cfg = json.loads(p.read_text())
        assert cfg["gateway"]["auth"]["token"] == _GW_TOKEN  # reconciled
        assert cfg["agents"] == {"defaults": {"model": "x"}}  # preserved
        assert cfg["plugins"]["entries"]["evolve"]["enabled"] is True  # preserved

    def test_empty_token_refused(self, evolve_home):
        assert gw.write_evolve_gateway_client(19030, "") is False
        assert not gw.evolve_client_oc_path().exists()

    def test_none_port_falls_back_to_default(self, evolve_home):
        assert gw.write_evolve_gateway_client(None, _GW_TOKEN) is True
        cfg = json.loads(gw.evolve_client_oc_path().read_text())
        assert cfg["gateway"]["port"] == 19030

    def test_malformed_existing_is_rewritten_minimal(self, evolve_home):
        p = gw.evolve_client_oc_path()
        p.write_text("{ this is not json")
        assert gw.write_evolve_gateway_client(19030, _GW_TOKEN) is True
        cfg = json.loads(p.read_text())
        assert cfg["gateway"]["auth"]["token"] == _GW_TOKEN
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


# ── provision_evolve_gateway_client — the back-compat gate ────────────────────

class TestProvisionGate:

    def test_macos_pre_cutover_account_is_noop(self, evolve_home):
        """gateway_account == admin 'evolve' (legacy / macOS pre-cutover): one
        shared openclaw.json, so NO separate client config is written."""
        assert gw.provision_evolve_gateway_client(19030, _GW_TOKEN, "evolve") is True
        assert not gw.evolve_client_oc_path().exists(), "must not write on the legacy/macOS-pre-cutover shape"

    def test_evo_primary_account_writes(self, evolve_home):
        assert gw.provision_evolve_gateway_client(19030, _GW_TOKEN, "evo") is True
        cfg = json.loads(gw.evolve_client_oc_path().read_text())
        assert cfg["gateway"]["auth"]["token"] == _GW_TOKEN


# ── primary_gateway_credential — resolving the live token ─────────────────────

class TestPrimaryGatewayCredential:

    def test_reads_token_and_port_from_primary_home(self, evolve_home, monkeypatch):
        home = _seed_primary(evolve_home, user="evo", token=_GW_TOKEN, port=19030)
        monkeypatch.setattr(primary_bot, "primary_bot_home", lambda n: home)
        port, token = gw.primary_gateway_credential({"primary": "evo"})
        assert (port, token) == (19030, _GW_TOKEN)

    def test_absent_primary_config_is_none(self, evolve_home, monkeypatch):
        monkeypatch.setattr(primary_bot, "primary_bot_home", lambda n: evolve_home / "evo")
        assert gw.primary_gateway_credential({"primary": "evo"}) == (None, None)


# ── check_evolve_gateway_client — the drift self-heal ─────────────────────────

class TestCheckEvolveGatewayClient:

    def _net(self, primary="evo", user="evo"):
        return {"primary": primary, "bots": {primary: {"user": user, "role": "primary"}}}

    def test_legacy_evolve_primary_is_skipped_ok_no_apply(self, evolve_home, monkeypatch):
        """admin == primary (legacy evolve-primary): single openclaw.json, the
        check passes with NO apply — the file is never touched."""
        monkeypatch.setattr(primary_bot, "primary_bot_user", lambda n: "evolve")
        c = gw.check_evolve_gateway_client(self._net("evolve", "evolve"))
        assert c.ok and c.apply is None
        assert not gw.evolve_client_oc_path().exists()

    def test_unresolved_primary_is_skipped(self, evolve_home, monkeypatch):
        monkeypatch.setattr(primary_bot, "primary_bot_user", lambda n: None)
        c = gw.check_evolve_gateway_client({})
        assert c.ok and c.apply is None

    def test_evo_primary_missing_client_is_drift_and_apply_repairs(self, evolve_home, monkeypatch):
        home = _seed_primary(evolve_home, token=_GW_TOKEN)
        monkeypatch.setattr(primary_bot, "primary_bot_user", lambda n: "evo")
        monkeypatch.setattr(primary_bot, "primary_bot_home", lambda n: home)

        c = gw.check_evolve_gateway_client(self._net())
        assert not c.ok and c.apply is not None
        assert c.apply() is True

        p = gw.evolve_client_oc_path()
        assert json.loads(p.read_text())["gateway"]["auth"]["token"] == _GW_TOKEN
        assert stat.S_IMODE(p.stat().st_mode) == 0o600

    def test_matching_token_is_ok_no_apply(self, evolve_home, monkeypatch):
        home = _seed_primary(evolve_home, token=_GW_TOKEN)
        monkeypatch.setattr(primary_bot, "primary_bot_user", lambda n: "evo")
        monkeypatch.setattr(primary_bot, "primary_bot_home", lambda n: home)
        p = gw.evolve_client_oc_path()
        p.write_text(json.dumps(
            {"gateway": {"port": 19030, "auth": {"mode": "token", "token": _GW_TOKEN}}}))
        p.chmod(0o600)  # token-bearing → the predicate now also requires 0600
        c = gw.check_evolve_gateway_client(self._net())
        assert c.ok, c.detail
        assert c.apply is None and c.fix_description == ""

    def test_mismatched_token_is_drift(self, evolve_home, monkeypatch):
        home = _seed_primary(evolve_home, token=_GW_TOKEN)
        monkeypatch.setattr(primary_bot, "primary_bot_user", lambda n: "evo")
        monkeypatch.setattr(primary_bot, "primary_bot_home", lambda n: home)
        gw.evolve_client_oc_path().write_text(json.dumps(
            {"gateway": {"port": 19030, "auth": {"mode": "token", "token": _OTHER_TOKEN}}}))
        c = gw.check_evolve_gateway_client(self._net())
        assert not c.ok and c.apply is not None
        assert c.apply() is True
        assert json.loads(gw.evolve_client_oc_path().read_text())["gateway"]["auth"]["token"] == _GW_TOKEN

    def test_correct_token_wrong_mode_is_drift_and_chmod_repairs(self, evolve_home, monkeypatch):
        """The EVO-SEP-GW-CRED-MODE gap: a CORRECT-token client config that
        drifted 0600→0644 (the deploy ``a+rX`` / Linux ACL-mask re-widen class)
        was reported ``ok`` by the mode-blind #3135 predicate, leaving the
        gateway token group/world-readable. The predicate must now FAIL on the
        wrong mode, and the apply must repair it to 0600 with a TARGETED chmod —
        leaving the token + contents byte-identical (no rewrite/churn)."""
        home = _seed_primary(evolve_home, token=_GW_TOKEN)
        monkeypatch.setattr(primary_bot, "primary_bot_user", lambda n: "evo")
        monkeypatch.setattr(primary_bot, "primary_bot_home", lambda n: home)
        p = gw.evolve_client_oc_path()
        # Correct token + port, but a non-gateway key present and mode 0644 — the
        # exact shape the audit confirmed live (ok=True, apply=None) pre-fix.
        p.write_text(json.dumps(
            {"gateway": {"port": 19030, "auth": {"mode": "token", "token": _GW_TOKEN}},
             "agents": {"defaults": {"model": "keep-me"}}}))
        p.chmod(0o644)
        before = p.read_text()

        c = gw.check_evolve_gateway_client(self._net())
        assert not c.ok, "0644 token-bearing client config must read as drift"
        assert c.apply is not None and c.fix_description
        assert c.apply() is True

        # Repaired to 0600; contents byte-identical (chmod, not a rewrite).
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert p.read_text() == before
        cfg = json.loads(p.read_text())
        assert cfg["gateway"]["auth"]["token"] == _GW_TOKEN  # token intact
        assert cfg["agents"] == {"defaults": {"model": "keep-me"}}  # contents intact

    def test_predicate_passes_only_at_0600(self, evolve_home, monkeypatch):
        """The same correct-token file is drift at 0644/0640 and ok only at 0600."""
        home = _seed_primary(evolve_home, token=_GW_TOKEN)
        monkeypatch.setattr(primary_bot, "primary_bot_user", lambda n: "evo")
        monkeypatch.setattr(primary_bot, "primary_bot_home", lambda n: home)
        p = gw.evolve_client_oc_path()
        p.write_text(json.dumps(
            {"gateway": {"port": 19030, "auth": {"mode": "token", "token": _GW_TOKEN}}}))
        for bad in (0o644, 0o640, 0o604, 0o660):
            p.chmod(bad)
            assert not gw.check_evolve_gateway_client(self._net()).ok, f"mode {oct(bad)} must be drift"
        p.chmod(0o600)
        assert gw.check_evolve_gateway_client(self._net()).ok

    def test_mode_drift_detail_is_token_free_and_names_mode(self, evolve_home, monkeypatch):
        """The mode-drift detail/fix never leak the token and DO name the mode."""
        home = _seed_primary(evolve_home, token=_GW_TOKEN)
        monkeypatch.setattr(primary_bot, "primary_bot_user", lambda n: "evo")
        monkeypatch.setattr(primary_bot, "primary_bot_home", lambda n: home)
        p = gw.evolve_client_oc_path()
        p.write_text(json.dumps(
            {"gateway": {"port": 19030, "auth": {"mode": "token", "token": _GW_TOKEN}}}))
        p.chmod(0o644)
        c = gw.check_evolve_gateway_client(self._net())
        assert _GW_TOKEN not in c.detail and _GW_TOKEN not in c.fix_description
        assert "0o644" in c.detail and "0o600" in c.detail  # mode words only
        assert "chmod 600" in c.fix_description

    def test_port_mismatch_is_drift(self, evolve_home, monkeypatch):
        home = _seed_primary(evolve_home, token=_GW_TOKEN, port=19030)
        monkeypatch.setattr(primary_bot, "primary_bot_user", lambda n: "evo")
        monkeypatch.setattr(primary_bot, "primary_bot_home", lambda n: home)
        gw.evolve_client_oc_path().write_text(json.dumps(
            {"gateway": {"port": 9999, "auth": {"mode": "token", "token": _GW_TOKEN}}}))
        c = gw.check_evolve_gateway_client(self._net())
        assert not c.ok

    def test_unresolvable_primary_token_skips_without_write(self, evolve_home, monkeypatch):
        """The primary gateway's config exists but has no token yet (not fully
        provisioned): the check reports informationally and writes nothing."""
        home = evolve_home / "evo"
        (home / ".openclaw").mkdir(parents=True)
        (home / ".openclaw" / "openclaw.json").write_text(json.dumps({"gateway": {"port": 19030}}))
        monkeypatch.setattr(primary_bot, "primary_bot_user", lambda n: "evo")
        monkeypatch.setattr(primary_bot, "primary_bot_home", lambda n: home)
        c = gw.check_evolve_gateway_client(self._net())
        assert c.ok and c.apply is None
        assert not gw.evolve_client_oc_path().exists()

    def test_idempotent_apply_no_churn(self, evolve_home, monkeypatch):
        home = _seed_primary(evolve_home, token=_GW_TOKEN)
        monkeypatch.setattr(primary_bot, "primary_bot_user", lambda n: "evo")
        monkeypatch.setattr(primary_bot, "primary_bot_home", lambda n: home)
        gw.check_evolve_gateway_client(self._net()).apply()
        p = gw.evolve_client_oc_path()
        first = p.read_text()
        # A second pass sees a match → no apply, file byte-identical.
        c = gw.check_evolve_gateway_client(self._net())
        assert c.ok and c.apply is None
        assert p.read_text() == first

    def test_token_never_leaks_in_detail_or_fix(self, evolve_home, monkeypatch):
        home = _seed_primary(evolve_home, token=_GW_TOKEN)
        monkeypatch.setattr(primary_bot, "primary_bot_user", lambda n: "evo")
        monkeypatch.setattr(primary_bot, "primary_bot_home", lambda n: home)
        gw.evolve_client_oc_path().write_text(json.dumps(
            {"gateway": {"port": 19030, "auth": {"mode": "token", "token": _OTHER_TOKEN}}}))
        c = gw.check_evolve_gateway_client(self._net())
        assert _GW_TOKEN not in c.detail and _GW_TOKEN not in c.fix_description
        assert _OTHER_TOKEN not in c.detail and _OTHER_TOKEN not in c.fix_description


# ── ensure_pod_perms wiring — pod-wide self-heal ──────────────────────────────

class TestEnsurePodPermsWiring:

    def _write_net(self, tmp_path, shared, primary="evo", user="evo"):
        net = {"networkId": "test-pod", "sharedDir": str(shared),
               "members": [], "bots": {primary: {"user": user, "role": "primary"}},
               "primary": primary}
        np = tmp_path / "network.json"
        np.write_text(json.dumps(net))
        return np

    def test_drift_surfaces_and_repairs_through_ensure_pod_perms(
            self, tmp_path, evolve_home, monkeypatch):
        shared = tmp_path / "shared"
        shared.mkdir()
        home = _seed_primary(evolve_home, token=_GW_TOKEN)
        monkeypatch.setattr(primary_bot, "primary_bot_user", lambda n: "evo")
        monkeypatch.setattr(primary_bot, "primary_bot_home", lambda n: home)
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")
        np = self._write_net(tmp_path, shared)

        # check_only: drift is reported, nothing written.
        res = deploy.ensure_pod_perms(bot_id=None, network_path=np, check_only=True)
        hits = [c for c in res.checks if c.category == "evo-gw-client"]
        assert hits and not hits[0].ok, (
            f"expected an evo-gw-client drift; categories: "
            f"{sorted({c.category for c in res.checks})}")
        assert not gw.evolve_client_oc_path().exists()

        # apply: the self-heal writes the matching client credential, 0600.
        res2 = deploy.ensure_pod_perms(bot_id=None, network_path=np, check_only=False)
        p = gw.evolve_client_oc_path()
        assert p.exists()
        assert json.loads(p.read_text())["gateway"]["auth"]["token"] == _GW_TOKEN
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert any("evo-gw-client" in a for a in res2.applied)

    def test_mode_drift_repaired_through_ensure_pod_perms(
            self, tmp_path, evolve_home, monkeypatch):
        """A correct-token client config at 0644 is reported as drift AND
        repaired to 0600 by the pod-wide ``ensure_pod_perms`` pass — the
        recurring 0600 re-assert the evolve service account's secret config never
        got from ``check_bot_secret_modes`` (which iterates only members/bots).
        This is the pod-wide coverage the EVO-SEP-GW-CRED-MODE fix delivers
        without extending the bot-secret sweep into the service account."""
        shared = tmp_path / "shared"
        shared.mkdir()
        home = _seed_primary(evolve_home, token=_GW_TOKEN)
        monkeypatch.setattr(primary_bot, "primary_bot_user", lambda n: "evo")
        monkeypatch.setattr(primary_bot, "primary_bot_home", lambda n: home)
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")
        np = self._write_net(tmp_path, shared)

        # Seed a CORRECT-token client config but world/group-readable (0644).
        p = gw.evolve_client_oc_path()
        p.write_text(json.dumps(
            {"gateway": {"port": 19030, "auth": {"mode": "token", "token": _GW_TOKEN}}}))
        p.chmod(0o644)

        # check_only: the mode drift is reported, nothing changed.
        res = deploy.ensure_pod_perms(bot_id=None, network_path=np, check_only=True)
        hits = [c for c in res.checks if c.category == "evo-gw-client"]
        assert hits and not hits[0].ok
        assert stat.S_IMODE(p.stat().st_mode) == 0o644  # untouched in check_only

        # apply: re-asserted to 0600 with the token intact.
        res2 = deploy.ensure_pod_perms(bot_id=None, network_path=np, check_only=False)
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert json.loads(p.read_text())["gateway"]["auth"]["token"] == _GW_TOKEN
        assert any("evo-gw-client" in a for a in res2.applied)

    def test_legacy_evolve_primary_no_drift_no_write(self, tmp_path, evolve_home, monkeypatch):
        shared = tmp_path / "shared"
        shared.mkdir()
        monkeypatch.setattr(primary_bot, "primary_bot_user", lambda n: "evolve")
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")
        np = self._write_net(tmp_path, shared, primary="evolve", user="evolve")
        res = deploy.ensure_pod_perms(bot_id=None, network_path=np, check_only=False)
        hits = [c for c in res.checks if c.category == "evo-gw-client"]
        assert hits and hits[0].ok  # present, passing, no repair
        assert not gw.evolve_client_oc_path().exists()


# ── provision wiring through _provision_evo_oc ────────────────────────────────

def _drive_provision(tmp_path, monkeypatch, *, profile_const, gateway_account):
    """Drive ``_provision_evo_oc`` with host-touching calls mocked, on the given
    platform profile + gateway account. Returns (ok, evolve_home_root)."""
    import platform_profile
    from evolve_admin import setup_wizard

    platform_profile.set_profile(profile_const)
    root = tmp_path / "home"
    (root / gateway_account / ".openclaw").mkdir(parents=True, exist_ok=True)
    (root / "evolve" / ".openclaw").mkdir(parents=True, exist_ok=True)

    def fake_run(argv, *a, **k):
        argv = list(argv)
        # The Step-H read-back of the gateway account's openclaw.json: return a
        # config carrying the live gateway token so gw_token is populated.
        if len(argv) >= 3 and argv[1].endswith("cat") and str(argv[-1]).endswith(
                f"{gateway_account}/.openclaw/openclaw.json"):
            return subprocess.CompletedProcess(
                argv, 0,
                stdout=json.dumps({"gateway": {"port": 19030,
                                               "auth": {"token": _GW_TOKEN}}}),
                stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    sched = MagicMock()
    sched.status.return_value = {"managed": False}
    sched.raw.return_value = (0, "", "")
    sched.restart.return_value = (True, "")
    auth_profiles = {"version": 1, "profiles": {"anthropic": {"provider": "anthropic"}}, "lastGood": {}}

    monkeypatch.setattr(gw, "_user_home", lambda acct: root / acct)

    try:
        with patch("subprocess.run", side_effect=fake_run), \
             patch.object(setup_wizard, "user_home", lambda acct: root / acct), \
             patch.object(setup_wizard, "_create_bot_account", return_value=True), \
             patch.object(setup_wizard, "_provision_evo_account", return_value=True), \
             patch.object(setup_wizard, "_log_admin_action"), \
             patch.object(setup_wizard, "_select_api_keys_for_evolve", return_value=auth_profiles), \
             patch.object(setup_wizard, "_embedding_chain_for_credentials", return_value=["openai"]), \
             patch.object(setup_wizard, "_evolve_openclaw_config",
                          return_value={"gateway": {"port": 19030, "auth": {"token": _GW_TOKEN}}}), \
             patch.object(setup_wizard, "_evolve_gateway_jobspec", return_value=MagicMock()), \
             patch.object(setup_wizard, "render_launchd_plist", return_value="plist"), \
             patch.object(setup_wizard, "get_launchd_scheduler", return_value=sched), \
             patch.object(deploy, "_install_job_ensuring_restart", return_value=(True, "")):
            ok = setup_wizard._provision_evo_oc(
                "testpod", tmp_path / "shared", "pod-admin", [], True,
                telegram_token="dummy", bot_id="evo", gateway_account=gateway_account,
            )
    finally:
        platform_profile.set_profile(None)
    return ok, root


class TestProvisionWiring:

    def test_linux_evo_primary_writes_client_credential_0600(self, tmp_path, monkeypatch):
        """Fresh Linux evo-primary: _provision_evo_oc writes the evolve client
        openclaw.json with the gateway token, mode 0600 — the live bug fix."""
        import platform_profile
        ok, root = _drive_provision(tmp_path, monkeypatch,
                                    profile_const=platform_profile.LINUX,
                                    gateway_account="evo")
        assert ok is True
        client = root / "evolve" / ".openclaw" / "openclaw.json"
        assert client.exists(), "evolve client openclaw.json must be written on the Linux evo-primary path"
        cfg = json.loads(client.read_text())
        assert cfg["gateway"]["auth"]["token"] == _GW_TOKEN
        assert cfg["gateway"]["port"] == 19030
        assert stat.S_IMODE(client.stat().st_mode) == 0o600

    def test_macos_legacy_account_writes_no_client_credential(self, tmp_path, monkeypatch):
        """macOS pre-cutover (gateway_account == 'evolve'): the gateway IS the
        admin account, so NO separate client config is written during provision
        (the cutover copy + deploy self-heal own that case). No double-write."""
        import platform_profile
        ok, root = _drive_provision(tmp_path, monkeypatch,
                                    profile_const=platform_profile.MACOS,
                                    gateway_account="evolve")
        assert ok is True
        client = root / "evolve" / ".openclaw" / "openclaw.json"
        assert not client.exists(), "must not write a separate client config when gateway_account == 'evolve'"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
