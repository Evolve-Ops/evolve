"""tests/test_oc_auth_store_import.py — OC-SQLITE-AUTH-WRITE (verify-driven).

OpenClaw 2026.6+ keeps each agent's credentials in a per-agent SQLite store
(``openclaw-agent.sqlite``). The original fix (#3136) assumed running
``openclaw models auth list`` as the bot user would import
``auth-profiles.json`` → sqlite "on agent-CLI init", and checked only the
command's EXIT CODE. On OC 2026.6.10 that auto-import does NOT fire: the command
exits 0 and leaves the store empty, so the helper returned ``(True, "imported")``
while the bot booted credential-less — a silent false-success.

These tests pin the corrected, VERIFY-DRIVEN mechanism:
  * ``oc_auth_provision.ensure_agent_auth_store_imported`` — trigger via
    ``models auth list``, VERIFY the store actually holds the providers in
    ``auth-profiles.json``, FALL BACK to ``paste-api-key`` / ``paste-token``
    (key on STDIN, never argv) for any still-missing profile, then re-verify and
    return a TRUTHFUL bool.
  * ``verify_default_model_authed`` / ``audit_bot_auth`` — the cheap (no
    dispatch) acceptance checks behind the ``pod_health_bot_auth`` Signal.
  * the wiring into the member gateway funnel + primary provisioning.

Read-side counterpart: ``evolve_admin.oc_store``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# A pinned absolute oc path so sudo command-matching is deterministic across
# whatever (if anything) is installed in the test env.
_OC_BIN = "/opt/homebrew/bin/openclaw"

_AGENT_RELDIR = ".openclaw/agents/main/agent"


# ── Fixtures + a programmable fake OpenClaw CLI ───────────────────────────────


def _write_auth_profiles(home: Path, profiles: dict) -> Path:
    """Write a real auth-profiles.json under the per-agent dir; return its path."""
    agent_dir = home / _AGENT_RELDIR
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "auth-profiles.json"
    path.write_text(json.dumps({"version": 1, "profiles": profiles}))
    return path


def _api_profile(provider="anthropic", profile_id=None, key="sk-ant-FAKE-KEY-0001"):
    pid = profile_id or f"{provider}:api"
    return pid, {"type": "api_key", "provider": provider, "key": key}


def _models_list_output(default_auth="yes", default_local="no") -> str:
    """A fixed-width ``models list`` table whose column offsets match the parser.

    The default model row carries ``default`` in its Tags column; ``<6`` padding
    puts the Auth cell exactly under the header's ``Auth`` and starts Tags at the
    header's ``Tags`` offset, so ``line[auth_col:tags_col]`` reads the Auth cell.
    """
    header = f"{'Model':<43}{'Input':<11}{'Ctx':<12}{'Local':<6}{'Auth':<6}{'Tags'}"
    row = (
        f"{'anthropic/claude-sonnet-4-6':<43}{'text+image':<11}{'1024k':<12}"
        f"{default_local:<6}{default_auth:<6}{'default,configured,alias:sonnet'}"
    )
    extra = (
        f"{'anthropic/claude-haiku-4-5':<43}{'text+image':<11}{'195k':<12}"
        f"{'no':<6}{'yes':<6}{''}"
    )
    return "\n".join([header, row, extra]) + "\n"


class FakeOc:
    """Records subprocess calls and answers like OpenClaw 2026.6.10.

    ``store`` is the set of profile-ids the sqlite store currently holds;
    ``models auth list`` reflects it, ``paste-*`` adds to it (when
    ``paste_populates``), so a re-verify after a paste sees the new profile —
    exactly the live behaviour the verify-driven fallback relies on. Also
    emulates ``sudo /bin/cp src dest`` by copying, so an e2e test that stages
    auth-profiles.json through cp lands a real file the helper can read.
    """

    def __init__(
        self,
        store_ids=None,
        *,
        paste_populates=True,
        list_rc=0,
        paste_rc=0,
        models_list_auth="yes",
    ):
        self.store = set(store_ids or [])
        self.paste_populates = paste_populates
        self.list_rc = list_rc
        self.paste_rc = paste_rc
        self.models_list_auth = models_list_auth
        self.calls: list[dict] = []

    def run(self, argv, *a, **k):
        argv = list(argv)
        self.calls.append({
            "argv": argv, "input": k.get("input"),
            "cwd": k.get("cwd"), "env": k.get("env"),
        })
        if argv[:2] == ["sudo", "/bin/cp"] and len(argv) >= 4:
            src, dest = Path(argv[-2]), Path(argv[-1])
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(src.read_bytes())
            except Exception:
                pass
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[-3:] == ["models", "auth", "list"]:
            return subprocess.CompletedProcess(
                argv, self.list_rc, self._auth_list_stdout(), "")
        if argv[-2:] == ["models", "list"]:
            return subprocess.CompletedProcess(
                argv, 0, _models_list_output(self.models_list_auth), "")
        if "paste-api-key" in argv or "paste-token" in argv:
            if self.paste_rc == 0 and self.paste_populates:
                pid = argv[argv.index("--profile-id") + 1]
                self.store.add(pid)
            return subprocess.CompletedProcess(argv, self.paste_rc, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _auth_list_stdout(self) -> str:
        head = ["Agent: main", "Auth state store: ~/.openclaw/…/agent.sqlite"]
        if not self.store:
            return "\n".join(head + ["Profiles:", "(none)"]) + "\n"
        lines = head + ["Profiles:"]
        for pid in sorted(self.store):
            prov = pid.split(":", 1)[0]
            lines.append(f"- {pid} [{prov}/api_key]")
        return "\n".join(lines) + "\n"

    @property
    def auth_list_calls(self):
        return [c for c in self.calls if c["argv"][-3:] == ["models", "auth", "list"]]

    @property
    def paste_calls(self):
        return [c for c in self.calls
                if "paste-api-key" in c["argv"] or "paste-token" in c["argv"]]


def _ensure(home, fake, bot_id="darwin", bot_user="darwin"):
    """Call ensure_agent_auth_store_imported with the fake oc + bin patched."""
    from evolve_admin import deploy, oc_auth_provision
    with patch.object(deploy, "_openclaw_bin", return_value=_OC_BIN), \
         patch("subprocess.run", side_effect=fake.run):
        return oc_auth_provision.ensure_agent_auth_store_imported(
            bot_id, bot_user, str(home))


# ── ensure_agent_auth_store_imported: nothing to provision ────────────────────


def test_no_auth_profiles_returns_true_without_subprocess(tmp_path):
    """No auth-profiles.json → nothing to guarantee → (True, …), and we must NOT
    spawn any subprocess (gateway-auth bot / json not written yet)."""
    fake = FakeOc()
    ok, msg = _ensure(tmp_path, fake)
    assert ok is True
    assert "nothing to provision" in msg
    assert fake.calls == []


def test_oauth_only_profiles_are_not_pasteable_and_return_true(tmp_path):
    """An auth-profiles.json whose only profile is an un-pasteable oauth/managed
    type is treated as 'nothing to provision' (success), never False."""
    _write_auth_profiles(tmp_path, {
        "anthropic:oauth": {"type": "oauth", "provider": "anthropic"},
    })
    fake = FakeOc()
    ok, msg = _ensure(tmp_path, fake)
    assert ok is True
    assert "no pasteable" in msg
    assert fake.paste_calls == []


# ── ensure: import-on-init already worked (no paste needed) ───────────────────


def test_import_on_init_already_populated_no_paste(tmp_path):
    """When `models auth list` already shows the expected profile (OC versions
    that DO import-on-init), we return True WITHOUT pasting."""
    pid, entry = _api_profile()
    _write_auth_profiles(tmp_path, {pid: entry})
    fake = FakeOc(store_ids={pid})  # store already populated
    ok, msg = _ensure(tmp_path, fake)
    assert ok is True
    assert "all 1 expected" in msg
    assert fake.paste_calls == []
    assert len(fake.auth_list_calls) == 1  # one verify read, no re-verify


# ── ensure: the 2026.6.10 fall-back path (the core fix) ───────────────────────


def test_import_exit0_but_empty_triggers_paste_then_succeeds(tmp_path):
    """The #3136 regression shape: `models auth list` exits 0 but the store is
    empty → we FALL BACK to paste-api-key (key on stdin) → re-verify sees the
    profile → return (True, …). The old code returned (True) on the empty store
    — this pins that it now actually provisions."""
    pid, entry = _api_profile(key="sk-ant-FAKE-SECRET-2222")
    _write_auth_profiles(tmp_path, {pid: entry})
    fake = FakeOc(store_ids=set())  # empty store, exit 0
    ok, msg = _ensure(tmp_path, fake)
    assert ok is True, msg
    assert "pasted 1" in msg
    # Exactly one paste, with the right shape, key ONLY on stdin.
    assert len(fake.paste_calls) == 1
    pc = fake.paste_calls[0]
    assert pc["argv"] == [
        "sudo", "--preserve-env=OPENCLAW_AGENT_DIR", "-H", "-u", "darwin",
        _OC_BIN, "models", "auth", "paste-api-key",
        "--provider", "anthropic", "--profile-id", pid,
    ], pc["argv"]
    assert pc["input"] == "sk-ant-FAKE-SECRET-2222\n"
    # Two list reads: the initial verify + the re-verify after paste.
    assert len(fake.auth_list_calls) == 2


def test_store_still_empty_after_paste_returns_false(tmp_path):
    """If paste runs but the store is STILL empty on re-verify, return
    (False, …) — the boolean must reflect the real end state, not exit-0."""
    pid, entry = _api_profile()
    _write_auth_profiles(tmp_path, {pid: entry})
    fake = FakeOc(store_ids=set(), paste_populates=False)  # paste is a no-op
    ok, msg = _ensure(tmp_path, fake)
    assert ok is False
    assert "still missing" in msg


def test_list_command_failure_returns_false_not_silent_true(tmp_path):
    """If the verify read can't run at all (non-zero exit), the end state is
    unverifiable → (False, …), never a silent True."""
    pid, entry = _api_profile()
    _write_auth_profiles(tmp_path, {pid: entry})
    fake = FakeOc(store_ids=set(), list_rc=3, paste_populates=False)
    ok, msg = _ensure(tmp_path, fake)
    assert ok is False
    assert "unreadable" in msg or "still missing" in msg


# ── ensure: multi-provider — no duplicate / no churn ──────────────────────────


def test_multiprovider_only_missing_is_pasted(tmp_path):
    """Two providers, one already in the store → only the MISSING one is pasted
    (no duplicate paste of the present one)."""
    a_pid, a_entry = _api_profile("anthropic", key="sk-ant-AAA")
    o_pid, o_entry = _api_profile("openai", profile_id="openai:api", key="sk-oai-BBB")
    _write_auth_profiles(tmp_path, {a_pid: a_entry, o_pid: o_entry})
    fake = FakeOc(store_ids={a_pid})  # anthropic already present, openai missing
    ok, msg = _ensure(tmp_path, fake)
    assert ok is True, msg
    assert len(fake.paste_calls) == 1
    assert fake.paste_calls[0]["argv"][-1] == o_pid  # only openai pasted


# ── Security: the key never reaches argv; stdin only for paste ────────────────


def test_key_never_on_argv_only_on_paste_stdin(tmp_path):
    """Across EVERY subprocess call the key never appears on argv; it appears on
    stdin ONLY for paste-* (the import trigger / re-verify reads carry no key)."""
    SENTINEL = "sk-ant-LEAK-SENTINEL-9999"
    pid, entry = _api_profile(key=SENTINEL)
    _write_auth_profiles(tmp_path, {pid: entry})
    fake = FakeOc(store_ids=set())
    ok, _msg = _ensure(tmp_path, fake)
    assert ok is True
    for c in fake.calls:
        assert all(SENTINEL not in tok for tok in c["argv"]), c["argv"]
    for c in fake.calls:
        is_paste = "paste-api-key" in c["argv"] or "paste-token" in c["argv"]
        if not is_paste:
            assert c["input"] is None, f"non-paste call carried stdin: {c['argv']}"
        else:
            assert c["input"] == SENTINEL + "\n"


def test_failure_detail_never_echoes_key_or_output(tmp_path):
    """A False return's detail string carries only counts — never the captured
    stdout/stderr (which can echo masked key suffixes)."""
    pid, entry = _api_profile(key="sk-ant-LEAK")
    _write_auth_profiles(tmp_path, {pid: entry})
    fake = FakeOc(store_ids=set(), paste_populates=False)
    ok, msg = _ensure(tmp_path, fake)
    assert ok is False
    assert "sk-ant-LEAK" not in msg and "LEAK" not in msg


def test_exception_is_swallowed_returns_false(tmp_path):
    """A subprocess exception during the verify read is caught; the helper never
    raises and returns a truthful False (unverifiable end state)."""
    from evolve_admin import deploy, oc_auth_provision
    pid, entry = _api_profile()
    _write_auth_profiles(tmp_path, {pid: entry})

    def boom(argv, *a, **k):
        raise subprocess.TimeoutExpired(argv, 30)

    with patch.object(deploy, "_openclaw_bin", return_value=_OC_BIN), \
         patch("subprocess.run", side_effect=boom):
        ok, msg = oc_auth_provision.ensure_agent_auth_store_imported(
            "darwin", "darwin", str(tmp_path))
    assert ok is False


# ── Command shape + cross-platform stability ──────────────────────────────────


def test_verify_read_command_shape(tmp_path):
    """The verify/trigger read runs `models auth list` AS THE BOT USER via
    --preserve-env, absolute oc path, cwd=bot home, OPENCLAW_AGENT_DIR set."""
    pid, entry = _api_profile()
    _write_auth_profiles(tmp_path, {pid: entry})
    fake = FakeOc(store_ids={pid})
    _ensure(tmp_path, fake, bot_user="darwin")
    lc = fake.auth_list_calls[0]
    assert lc["argv"] == [
        "sudo", "--preserve-env=OPENCLAW_AGENT_DIR", "-H", "-u", "darwin",
        _OC_BIN, "models", "auth", "list",
    ]
    assert "env" not in lc["argv"], "must not prefix with `env` (breaks sudo match)"
    assert lc["cwd"] == str(tmp_path)
    assert lc["env"]["OPENCLAW_AGENT_DIR"] == str(tmp_path / _AGENT_RELDIR)


@pytest.mark.parametrize("profile_name", ["macos", "linux"])
def test_command_shape_stable_cross_platform(profile_name, tmp_path):
    """Same command shape on macOS + Linux — only the resolved oc path / home
    differ (the Linux pod is where the bug was found)."""
    import platform_profile
    prof = platform_profile.MACOS if profile_name == "macos" else platform_profile.LINUX
    platform_profile.set_profile(prof)
    try:
        pid, entry = _api_profile()
        _write_auth_profiles(tmp_path, {pid: entry})
        fake = FakeOc(store_ids=set())
        ok, _ = _ensure(tmp_path, fake)
    finally:
        platform_profile.set_profile(None)
    assert ok is True
    pc = fake.paste_calls[0]
    assert pc["argv"][:5] == ["sudo", "--preserve-env=OPENCLAW_AGENT_DIR", "-H", "-u", "darwin"]
    assert "paste-api-key" in pc["argv"]


# ── verify_default_model_authed (cheap acceptance, no dispatch) ───────────────


def _verify_default(tmp_path, auth="yes", rc=0, no_default=False):
    from evolve_admin import deploy, oc_auth_provision

    def fake_run(argv, *a, **k):
        out = _models_list_output(auth)
        if no_default:
            out = out.replace(",configured,alias:sonnet", "").replace("default", "alias")
        return subprocess.CompletedProcess(argv, rc, out, "")

    with patch.object(deploy, "_openclaw_bin", return_value=_OC_BIN), \
         patch("subprocess.run", side_effect=fake_run):
        return oc_auth_provision.verify_default_model_authed("darwin", "darwin", str(tmp_path))


def test_verify_default_model_authed_yes(tmp_path):
    assert _verify_default(tmp_path, auth="yes") is True


def test_verify_default_model_authed_no(tmp_path):
    assert _verify_default(tmp_path, auth="no") is False


def test_verify_default_model_command_failure_is_none(tmp_path):
    assert _verify_default(tmp_path, rc=2) is None


def test_verify_default_model_no_default_row_is_none(tmp_path):
    assert _verify_default(tmp_path, no_default=True) is None


# ── audit_bot_auth (gated standing check behind the Signal) ───────────────────


def test_audit_bot_auth_gateway_auth_bot_is_ok_without_subprocess(tmp_path):
    """A bot with no pasteable key profiles is gateway-auth → 'ok' verdict, and
    we never spawn `models list` for it."""
    from evolve_admin import deploy, oc_auth_provision
    calls = []

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    with patch.object(deploy, "_openclaw_bin", return_value=_OC_BIN), \
         patch("subprocess.run", side_effect=fake_run):
        verdict, _ = oc_auth_provision.audit_bot_auth("darwin", "darwin", str(tmp_path))
    assert verdict == "ok"
    assert calls == []


def test_audit_bot_auth_keyauth_missing_is_missing(tmp_path):
    pid, entry = _api_profile()
    _write_auth_profiles(tmp_path, {pid: entry})
    from evolve_admin import deploy, oc_auth_provision
    with patch.object(deploy, "_openclaw_bin", return_value=_OC_BIN), \
         patch("subprocess.run",
               side_effect=lambda argv, *a, **k: subprocess.CompletedProcess(
                   argv, 0, _models_list_output("no"), "")):
        verdict, detail = oc_auth_provision.audit_bot_auth("darwin", "darwin", str(tmp_path))
    assert verdict == "missing"
    assert "Auth:no" in detail


def test_audit_bot_auth_keyauth_authed_is_ok(tmp_path):
    pid, entry = _api_profile()
    _write_auth_profiles(tmp_path, {pid: entry})
    from evolve_admin import deploy, oc_auth_provision
    with patch.object(deploy, "_openclaw_bin", return_value=_OC_BIN), \
         patch("subprocess.run",
               side_effect=lambda argv, *a, **k: subprocess.CompletedProcess(
                   argv, 0, _models_list_output("yes"), "")):
        verdict, _ = oc_auth_provision.audit_bot_auth("darwin", "darwin", str(tmp_path))
    assert verdict == "ok"


def test_audit_bot_auth_command_failure_is_unknown_not_missing(tmp_path):
    """A transient `models list` failure → 'unknown' (NOT 'missing') so the
    Signal does not flap on a one-off CLI error."""
    pid, entry = _api_profile()
    _write_auth_profiles(tmp_path, {pid: entry})
    from evolve_admin import deploy, oc_auth_provision
    with patch.object(deploy, "_openclaw_bin", return_value=_OC_BIN), \
         patch("subprocess.run",
               side_effect=lambda argv, *a, **k: subprocess.CompletedProcess(argv, 5, "", "")):
        verdict, _ = oc_auth_provision.audit_bot_auth("darwin", "darwin", str(tmp_path))
    assert verdict == "unknown"


# ── Wiring: member gateway funnel imports BEFORE bootstrap ────────────────────


def test_install_bot_gateway_plist_imports_before_bootstrap():
    """install_bot_gateway_plist must trigger the auth-store import BEFORE it
    bootstraps the gateway, so the started gateway reads the populated store."""
    from evolve_admin import deploy

    order: list[str] = []

    def fake_import(bot_id, bot_user, bot_home=None):
        order.append("import")
        return True, "ok"

    def fake_install_job(spec):
        order.append("bootstrap")
        return True, ""

    with patch("evolve_admin.oc_auth_provision.ensure_agent_auth_store_imported",
               side_effect=fake_import), \
         patch.object(deploy, "_install_job_ensuring_restart", side_effect=fake_install_job), \
         patch.object(deploy, "_ensure_gateway_mode_seeded"), \
         patch.object(deploy, "_wait_for_gateway_port", return_value=True), \
         patch.object(deploy, "_user_home", return_value=Path("/home/atlas")), \
         patch("subprocess.run",
               return_value=subprocess.CompletedProcess([], 0, "", "")), \
         patch("shutil.which", return_value="/usr/bin/node"):
        ok, detail = deploy.install_bot_gateway_plist("atlas", 19031, user="atlas")

    assert ok is True, detail
    assert order == ["import", "bootstrap"]


def test_install_bot_gateway_plist_warns_and_proceeds_when_import_fails(caplog):
    """A False (truthful) import is best-effort: the gateway still bootstraps,
    but a loud WARN is logged (no more silent false-success)."""
    import logging
    from evolve_admin import deploy

    order: list[str] = []

    with patch("evolve_admin.oc_auth_provision.ensure_agent_auth_store_imported",
               return_value=(False, "auth store still missing 1 of 1 profile(s)")), \
         patch.object(deploy, "_install_job_ensuring_restart",
                      side_effect=lambda spec: order.append("bootstrap") or (True, "")), \
         patch.object(deploy, "_ensure_gateway_mode_seeded"), \
         patch.object(deploy, "_wait_for_gateway_port", return_value=True), \
         patch.object(deploy, "_user_home", return_value=Path("/home/atlas")), \
         patch("subprocess.run",
               return_value=subprocess.CompletedProcess([], 0, "", "")), \
         patch("shutil.which", return_value="/usr/bin/node"):
        with caplog.at_level(logging.WARNING):
            ok, _detail = deploy.install_bot_gateway_plist("atlas", 19031, user="atlas")

    assert ok is True
    assert order == ["bootstrap"]
    assert any("auth store NOT provisioned" in r.message for r in caplog.records)


# ── Wiring: primary provisioning (_provision_evo_oc), full e2e ────────────────


def _drive_provision_evo_oc(tmp_path: Path, api_key="sk-ant-SECRET-LEAK-9999"):
    """Drive ``_provision_evo_oc`` with the FakeOc emulating cp + auth CLI, so
    the real write→verify→paste path runs end-to-end. Returns (ok, home, fake)."""
    from evolve_admin import setup_wizard
    import platform_profile

    platform_profile.set_profile(platform_profile.MACOS)
    home = tmp_path / "Users" / "evolve"
    fake = FakeOc(store_ids=set())  # empty store → fallback paste must populate it

    sched = MagicMock()
    sched.status.return_value = {"managed": False}
    sched.raw.return_value = (0, "", "")
    sched.restart.return_value = (True, "")

    auth_profiles = {
        "version": 1,
        "profiles": {
            "anthropic:api_key": {
                "type": "api_key", "provider": "anthropic", "key": api_key,
            }
        },
        "lastGood": {"anthropic": "anthropic:api_key"},
    }
    try:
        with patch("subprocess.run", side_effect=fake.run), \
             patch.object(setup_wizard, "user_home", lambda acct: home), \
             patch.object(setup_wizard, "_create_bot_account", return_value=True), \
             patch.object(setup_wizard, "_provision_evo_account", return_value=True), \
             patch.object(setup_wizard, "_log_admin_action"), \
             patch.object(setup_wizard, "_select_api_keys_for_evolve",
                          return_value=auth_profiles), \
             patch.object(setup_wizard, "_embedding_chain_for_credentials",
                          return_value=["openai"]), \
             patch.object(setup_wizard, "_evolve_openclaw_config",
                          return_value={"gateway": {"port": 19030,
                                                    "auth": {"token": "tok"}}}), \
             patch.object(setup_wizard, "_evolve_gateway_jobspec",
                          return_value=MagicMock()), \
             patch.object(setup_wizard, "render_launchd_plist", return_value="plist"), \
             patch.object(setup_wizard, "get_launchd_scheduler", return_value=sched), \
             patch("evolve_admin.deploy._openclaw_bin", return_value=_OC_BIN):
            ok = setup_wizard._provision_evo_oc(
                "testpod", tmp_path / "shared", "pod-admin", [], True,
                telegram_token="dummy", bot_id="evo", gateway_account="evolve",
            )
    finally:
        platform_profile.set_profile(None)
    return ok, home, fake


def test_provision_evo_oc_provisions_store_via_fallback(tmp_path):
    """The primary provisioning path stages auth-profiles.json, then (store empty)
    pastes the key so the store ends populated — provisioning succeeds."""
    ok, _home, fake = _drive_provision_evo_oc(tmp_path)
    assert ok is True
    # store empty → exactly one paste of the anthropic profile, then re-verify.
    assert len(fake.paste_calls) == 1
    assert fake.paste_calls[0]["argv"][-1] == "anthropic:api_key"
    assert "anthropic:api_key" in fake.store


def test_provision_evo_oc_never_leaks_key_to_argv(tmp_path):
    """End-to-end ps-leak guard: across EVERY subprocess the provider key never
    appears on argv; on stdin it appears ONLY for the paste-* call."""
    SENTINEL = "sk-ant-SECRET-LEAK-9999"
    ok, _home, fake = _drive_provision_evo_oc(tmp_path, api_key=SENTINEL)
    assert ok is True
    for c in fake.calls:
        for tok in c["argv"]:
            assert SENTINEL not in tok, f"API key leaked onto argv: {c['argv']}"
        is_paste = "paste-api-key" in c["argv"] or "paste-token" in c["argv"]
        if c.get("input") and not is_paste:
            assert SENTINEL not in str(c["input"]), f"key on non-paste stdin: {c['argv']}"
