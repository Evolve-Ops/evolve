"""The eight must-fixes of the #4281 review, one fixture group each.

Review: internal/dispatch/reviews/pr-4281.md. Brief:
internal/dispatch/done/hold-fix-4281-remove-refuses-on-unknown-and-pinned-never-falls-open.md

Every group is the fail-closed doctrine (D-CS7) applied to the runtime floor:
a control that cannot read its evidence says so and refuses, rather than
reading "could not tell" as the permissive answer. No sudo: the store's owner
seam is the test user, and every subprocess boundary is a fake.
"""

from __future__ import annotations

import json
import os

import pytest

from evolve_admin import oc_runtime as rt
from evolve_admin.oc_runtime import OcRuntimeError, PinResolutionError

from .test_oc_runtime import _install_fake, _network, runner

V_OLD, V_NEW = "2026.9.2", "2026.9.4"


@pytest.fixture
def shared(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "_store_owner_uid", os.geteuid)
    d = tmp_path / "evolve"
    d.mkdir()
    return d


@pytest.fixture
def units(tmp_path):
    d = tmp_path / "LaunchDaemons"
    d.mkdir()
    return d


# ── 1. remove refuses unless every bot's pin was POSITIVELY read ─────────────


def _row(value):
    return {"bots": {"team-bot-a": {"port": 19000, rt.PIN_KEY: value}}}


@pytest.mark.parametrize("network,why", [
    (None, "could not be read"),                                  # unreadable file
    ({}, "no bots table"),                                        # absent -> {}
    ({"bots": {"team-bot-a": "not-a-dict-at-all"}}, "team-bot-a: its registry row is a str"),
    (_row("v2026.9.2"), "team-bot-a: oc_version is 'v2026.9.2'"),
    (_row("2026.9.2 "), "team-bot-a: oc_version is '2026.9.2 '"),
    (_row(None), "team-bot-a: oc_version is None"),
])
def test_remove_refuses_when_it_cannot_tell_who_runs_the_version(shared, units, network, why):
    _install_fake(shared, V_OLD)
    with pytest.raises(OcRuntimeError, match="cannot tell which bots run it") as exc:
        rt.remove_version(V_OLD, network, shared_dir=shared, unit_dir=units)
    assert why in str(exc.value), "the refusal names the bot and the reason"
    assert rt.is_installed(V_OLD, shared), "the refusal must not have deleted it"


def test_remove_refuses_while_a_gateway_unit_still_execs_it(shared, units):
    # The half-finished repoint: registry moved on, plist still runs the old.
    _install_fake(shared, V_OLD)
    (units / "ai.openclaw.team-bot-a-gateway.plist").write_text(
        f"<string>{rt.version_entrypoint(V_OLD, shared)}</string>")
    net = _network("team-bot-a", **{"team-bot-a": V_NEW})
    with pytest.raises(OcRuntimeError, match="ai.openclaw.team-bot-a-gateway.plist still exec"):
        rt.remove_version(V_OLD, net, shared_dir=shared, unit_dir=units)
    assert rt.is_installed(V_OLD, shared)


def test_remove_refuses_when_the_unit_dir_cannot_be_read(shared, tmp_path):
    _install_fake(shared, V_OLD)
    with pytest.raises(OcRuntimeError, match="cannot list"):
        rt.remove_version(V_OLD, _network("team-bot-a"), shared_dir=shared,
                          unit_dir=tmp_path / "absent")
    assert rt.is_installed(V_OLD, shared)


def test_list_never_calls_a_version_removable_it_cannot_vouch_for(shared):
    _install_fake(shared, V_OLD)
    rows = rt.describe_store({"bots": {"team-bot-a": "junk"}}, shared_dir=shared)
    assert rows[0]["removable"] is None
    assert rt.unpinned_bots({"bots": {"team-bot-a": "junk"}}) == [], (
        "an unreadable pin is not 'unpinned'"
    )


def test_oc_remove_refuses_on_an_unreadable_network_json(shared, tmp_path):
    from click.testing import CliRunner

    from evolve_admin import ocadmin

    _install_fake(shared, V_OLD)
    net_path = tmp_path / "network.json"
    net_path.write_text("{ truncated")
    real = rt.store_root
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rt, "store_root", lambda sd=None: real(shared))
        res = CliRunner().invoke(ocadmin.menu_group, ["remove", V_OLD],
                                 obj={"network_path": net_path})
    assert res.exit_code == 1
    out = " ".join(res.output.split())  # rich wraps at the terminal width
    assert "cannot read" in out and "cannot tell which bots run it" in out
    assert rt.is_installed(V_OLD, shared)


# ── 2 + 3. a pinned bot never renders on the global binary ──────────────────


def test_a_resolution_failure_is_a_named_refusal(shared, monkeypatch):
    _install_fake(shared, V_NEW)

    def boom(*a, **k):
        raise OSError("disk went away")

    monkeypatch.setattr(rt, "version_entrypoint", boom)
    with pytest.raises(PinResolutionError) as exc:
        rt.resolve_gateway_runtime("team-bot-a", _network("team-bot-a", **{"team-bot-a": V_NEW}),
                                   platform_name="macos", shared_dir=shared)
    assert str(exc.value) == f"team-bot-a: pinned to {V_NEW}, cannot resolve: disk went away"


def test_a_pin_to_a_version_not_installed_is_the_same_refusal(shared):
    with pytest.raises(PinResolutionError, match=f"team-bot-a: pinned to {V_NEW}, cannot "
                                                 f"resolve: not installed at"):
        rt.resolve_gateway_runtime("team-bot-a", _network("team-bot-a", **{"team-bot-a": V_NEW}),
                                   platform_name="macos", shared_dir=shared)


def test_the_renderer_writes_no_plist_for_a_refused_pin(shared, monkeypatch):
    # The deploy step: (False, reason) and not one subprocess — the plist is
    # not written, not bootstrapped; cli's _deploy_step then exits 1.
    from evolve_admin import deploy

    real = rt.store_root
    monkeypatch.setattr(rt, "store_root", lambda sd=None: real(shared))

    def no_subprocess(*a, **k):
        raise AssertionError(f"a refused pin must not reach a subprocess: {a}")

    monkeypatch.setattr(deploy.subprocess, "run", no_subprocess)
    monkeypatch.setattr(deploy, "_install_job_ensuring_restart", no_subprocess)
    ok, detail = deploy.install_bot_gateway_plist(
        "team-bot-a", 19000, user="team-bot-a",
        network=_network("team-bot-a", **{"team-bot-a": V_NEW}),
    )
    assert ok is False
    assert detail.startswith(f"team-bot-a: pinned to {V_NEW}, cannot resolve: not installed")


# ── 4. the store root is root-owned, real, and npm runs no scripts ──────────


def test_a_fresh_store_root_is_created_0755(shared):
    root = rt.ensure_store_root(shared)
    assert oct(root.stat().st_mode & 0o777) == "0o755"


def test_a_symlinked_store_root_is_refused(shared, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    rt.store_root(shared).symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OcRuntimeError, match="not a real directory"):
        rt.ensure_store_root(shared)


def test_a_store_root_someone_else_owns_is_refused(shared, monkeypatch):
    rt.store_root(shared).mkdir()
    other = os.geteuid() + 1  # the process is "root"; the dir is the test user's
    monkeypatch.setattr(rt, "_store_owner_uid", lambda: other)
    monkeypatch.setattr(rt.os, "geteuid", lambda: other)
    with pytest.raises(OcRuntimeError, match="owned by uid"):
        rt.ensure_store_root(shared)


def test_a_world_writable_store_root_is_refused(shared):
    rt.store_root(shared).mkdir()
    os.chmod(rt.store_root(shared), 0o777)
    with pytest.raises(OcRuntimeError, match="world-writable"):
        rt.ensure_store_root(shared)


def test_the_store_is_not_written_without_root(shared, monkeypatch):
    monkeypatch.setattr(rt, "_store_owner_uid", lambda: os.geteuid() + 1)
    with pytest.raises(OcRuntimeError, match="must be written as root"):
        rt.ensure_store_root(shared)
    assert not rt.store_root(shared).exists()


def _npm(shared, view):
    def run(argv):
        run.calls.append(list(argv))
        if argv[:2] == ["npm", "view"]:
            return view
        if argv[:2] == ["npm", "install"]:
            _install_fake(shared, V_NEW)
            return 0, "", ""
        return 0, V_NEW, ""

    run.calls = []
    return run


def test_install_runs_no_lifecycle_script_and_records_the_integrity(shared):
    view = (0, json.dumps({"version": V_NEW, "dist.integrity": "sha512-xyz",
                           "dist.tarball": "https://registry.npmjs.org/t.tgz"}), "")
    run = _npm(shared, view)
    rt.install_version(V_NEW, shared_dir=shared, runner=run)
    npm = next(c for c in run.calls if c[:2] == ["npm", "install"])
    assert "--ignore-scripts" in npm
    assert npm[npm.index("--registry") + 1] == rt.NPM_REGISTRY
    record = json.loads((rt.version_dir(V_NEW, shared) / rt.INSTALL_RECORD).read_text())
    assert record["integrity"] == "sha512-xyz" and record["registry"] == rt.NPM_REGISTRY


def test_install_refuses_when_the_registry_gives_no_integrity(shared):
    run = _npm(shared, (0, json.dumps({"version": V_NEW}), ""))
    with pytest.raises(OcRuntimeError, match="integrity cannot be recorded"):
        rt.install_version(V_NEW, shared_dir=shared, runner=run)
    assert not any(c[:2] == ["npm", "install"] for c in run.calls)


# ── the CLI harness for 5–8 ─────────────────────────────────────────────────


def _cli(args, network, tmp_path, monkeypatch, shared, *, probe=(True, "reports it, healthy"),
         validated=(True, "validated")):
    from click.testing import CliRunner

    from evolve_admin import deploy, ocadmin

    net_path = tmp_path / "network.json"
    net_path.write_text(json.dumps(network))
    real = rt.store_root
    monkeypatch.setattr(rt, "store_root", lambda sd=None: real(shared))
    monkeypatch.setattr(rt, "validation_verdict", lambda v, sd=None: validated)
    executed: list[str] = []

    def fake_plist(bot_id, port, user=None, network=None):
        on_disk = json.loads(net_path.read_text())
        assert rt.pinned_version(bot_id, on_disk).version == args[2], "registry first"
        assert rt.pinned_version(bot_id, network).version == args[2], "rendered from the NEW pin"
        executed.append("restart")
        return True, "installed and bootstrapped"

    def fake_probe(version, *, cli, user, health, **kw):
        assert cli == str(rt.version_bin_dir(version, shared) / "openclaw")
        executed.append("probe")
        return probe

    real_save = __import__("evolve_admin.config", fromlist=["save_network"]).save_network

    def save(data, path):
        executed.append("registry")
        real_save(data, path)

    monkeypatch.setattr(deploy, "install_bot_gateway_plist", fake_plist)
    monkeypatch.setattr(deploy, "get_bot_port", lambda b, n: 19000)
    monkeypatch.setattr(deploy, "get_bot_user", lambda b, n: b)
    monkeypatch.setattr("evolve_admin.config.save_network", save)
    monkeypatch.setattr(rt, "probe_gateway", fake_probe)
    res = CliRunner().invoke(ocadmin.menu_group, list(args), obj={"network_path": net_path})
    return res, executed


# ── 5. the printed plan is exactly the executed steps ───────────────────────


def test_oc_pin_executes_exactly_the_plan_it_prints(tmp_path, monkeypatch, shared):
    _install_fake(shared, V_NEW)
    net = _network("team-bot-a")
    res, executed = _cli(["pin", "team-bot-a", V_NEW], net, tmp_path, monkeypatch, shared)
    assert res.exit_code == 0, res.output
    printed = [ln.split(".", 1)[1].split(":", 1)[0].strip()
               for ln in res.output.splitlines() if ln.startswith("    ") and ". " in ln]
    planned = [s.kind for s in rt.plan_pin("team-bot-a", V_NEW, net, shared_dir=shared).steps]
    assert printed == planned == executed == ["registry", "restart", "probe"]


# ── 6. an unvalidated version needs --override, and the override is recorded ─


def test_oc_pin_refuses_an_unvalidated_version(tmp_path, monkeypatch, shared):
    _install_fake(shared, V_NEW)
    res, executed = _cli(["pin", "team-bot-a", V_NEW], _network("team-bot-a"),
                         tmp_path, monkeypatch, shared, validated=(False, "untested"))
    assert res.exit_code == 1
    out = " ".join(res.output.split())
    assert "untested" in out and "--override" in out
    assert executed == [], "nothing ran"


def test_oc_pin_override_proceeds_and_is_audited(tmp_path, monkeypatch, shared):
    _install_fake(shared, V_NEW)
    res, executed = _cli(["pin", "team-bot-a", V_NEW, "--override"], _network("team-bot-a"),
                         tmp_path, monkeypatch, shared, validated=(False, "untested"))
    assert res.exit_code == 0, res.output
    line = json.loads((rt.store_root(shared) / "pins.jsonl").read_text().splitlines()[-1])
    assert line["override"] is True and line["validation"] == "untested"
    assert (line["bot"], line["to"]) == ("team-bot-a", V_NEW)


# ── 7. adopt copies what it says it copies ──────────────────────────────────


def test_oc_adopt_copies_then_says_so_in_the_past_tense(tmp_path, monkeypatch, shared):
    from evolve_admin import ocadmin

    from .test_oc_runtime import _global_install

    src, pkg = _global_install(tmp_path, V_OLD)
    monkeypatch.setattr(ocadmin, "_installed_version", lambda: V_OLD)
    monkeypatch.setattr(rt, "global_prefix", lambda name: src)
    monkeypatch.setattr(rt, "verify_version", lambda v, **kw: (True, v))
    res, _ = _cli(["adopt"], _network("team-bot-a"), tmp_path, monkeypatch, shared)
    assert res.exit_code == 0, res.output
    assert rt.is_installed(V_OLD, shared), "the documented `oc pin` now finds it"
    assert (rt.version_bin_dir(V_OLD, shared) / "openclaw").is_symlink()
    assert "copied into the store and verified" in " ".join(res.output.split())
    assert pkg.exists(), "copied, never moved"


def test_adopt_refuses_a_tree_that_is_another_version(shared, tmp_path):
    from .test_oc_runtime import _global_install

    src, _ = _global_install(tmp_path, V_NEW)
    with pytest.raises(OcRuntimeError, match="not 2026.9.2"):
        rt.adopt_copy(src, V_OLD, shared_dir=shared)
    assert not rt.version_dir(V_OLD, shared).exists()


def test_an_adopt_copy_that_does_not_run_keeps_nothing(shared, tmp_path):
    from .test_oc_runtime import _global_install

    src, _ = _global_install(tmp_path, V_OLD)
    with pytest.raises(OcRuntimeError, match="does not run"):
        rt.adopt_copy(src, V_OLD, shared_dir=shared, runner=runner({"--version": (1, "", "boom")}))
    assert not rt.version_dir(V_OLD, shared).exists(), "a half-copy would read as installed"


# ── 8. "restarted on <v>" only after a probe saw it ─────────────────────────


def test_no_probe_match_means_outcome_unknown_and_nonzero(tmp_path, monkeypatch, shared):
    _install_fake(shared, V_NEW)
    res, executed = _cli(["pin", "team-bot-a", V_NEW], _network("team-bot-a"), tmp_path,
                         monkeypatch, shared, probe=(False, f"gateway reports {V_OLD}"))
    assert res.exit_code == 1
    out = " ".join(res.output.split())
    assert f"team-bot-a: restart outcome unknown — gateway reports {V_OLD}" in out
    assert "restarted on" not in res.output


@pytest.mark.parametrize("status,healthy,ok", [
    (f"Gateway version: {V_NEW}\n", (True, "plugin loaded"), True),
    (f"Gateway version: {V_OLD}\n", (True, "plugin loaded"), False),   # old process
    (f"Gateway version: {V_NEW}\n", (False, "HTTP 502"), False),       # up, not serving
    ("", (True, "plugin loaded"), False),                              # no answer
])
def test_probe_needs_the_version_and_health(status, healthy, ok):
    got, detail = rt.probe_gateway(
        V_NEW, cli="/x/openclaw", user="team-bot-a", health=lambda: healthy,
        runner=runner({"gateway status": (0, status, "")}), attempts=2, sleep=lambda s: None,
    )
    assert got is ok, detail
