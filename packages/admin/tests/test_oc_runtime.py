"""Tests for the versioned OpenClaw runtime store and per-bot pins.

Chip: internal/dispatch/done/oc-runtime-versioned-per-bot.md
Design: internal/design-oc-upgrade-safety-2026-09-08.md §2 row 1.

The property under test throughout is *reversibility*. On 2026-09-07 a single
Homebrew cask moved under nine launchd jobs and the previous version was gone
the same instant, so there was no way back. Every assertion here is ultimately
about keeping the way back open: versions installed beside each other, a pin
per bot, and a refusal to delete anything a bot is running.

Nothing here shells out. The store is a tmp_path tree and the one subprocess
boundary is injected.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from evolve_admin import oc_runtime as rt
from evolve_admin.oc_runtime import OcRuntimeError


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def shared(tmp_path, monkeypatch):
    """A `{shared_dir}` whose sibling `-oc` store is empty.

    The store must be root-owned in production; here the owner seam is the
    test user, so nothing needs sudo. Validation defaults to "validated" so
    the tests that are not about the gate are not gated by it.
    """
    monkeypatch.setattr(rt, "_store_owner_uid", os.geteuid)
    monkeypatch.setattr(rt, "validation_verdict", lambda v, sd=None: (True, "validated"))
    d = tmp_path / "evolve"
    d.mkdir()
    return d


def _install_fake(shared, version):
    """Materialise a version in the store as a real install would leave it."""
    entry = rt.version_entrypoint(version, shared)
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("// fake openclaw\n")
    (entry.parent.parent / "package.json").write_text(json.dumps({"version": version}))
    rt.version_bin_dir(version, shared).mkdir(parents=True, exist_ok=True)
    return entry


def _network(*bots, **pins):
    net = {"bots": {b: {"port": 19000 + i} for i, b in enumerate(bots)}}
    for bot, version in pins.items():
        net["bots"][bot][rt.PIN_KEY] = version
    return net


def runner(responses):
    calls = []

    def run(argv):
        calls.append(list(argv))
        joined = " ".join(str(a) for a in argv)
        for needle, resp in responses.items():
            if needle in joined:
                return resp
        return 1, "", f"unexpected argv: {joined}"

    run.calls = calls  # type: ignore[attr-defined]
    return run


_VIEW_OK = json.dumps({"version": "2026.9.4", "dist.integrity": "sha512-abc",
                       "dist.tarball": "https://registry.npmjs.org/openclaw/-/openclaw-2026.9.4.tgz"})


# ── Version strings are an allowlist, because they become paths and argv ─────


@pytest.mark.parametrize("good", ["2026.9.4", "2026.9.4-rc1", "1.0", "2026.10.0+build7"])
def test_real_versions_are_accepted(good):
    assert rt.valid_version(good)


@pytest.mark.parametrize("bad", [
    "", "latest", "next", "../etc", "2026.9.4/../..", "/abs/path",
    "-rf", "a b", "..", "2026.9.4;rm -rf /", "v2026.9.4",
])
def test_anything_that_is_not_one_exact_version_is_refused(bad):
    # These become a DIRECTORY NAME under a root-owned prefix and an npm
    # argument, so the policy is an allowlist, not sanitising.
    assert not rt.valid_version(bad)
    with pytest.raises(OcRuntimeError):
        rt.version_dir(bad, "/tmp/x")


def test_dist_tags_are_refused_because_a_tag_is_not_a_pin():
    # `latest` moves. A pin that can move is not a pin, and the rollback it
    # promises would silently become a no-op.
    assert not rt.valid_version("latest")


# ── The store ────────────────────────────────────────────────────────────────


def test_store_is_a_sibling_of_shared_dir_not_inside_it(shared):
    # A runtime is not pod state: a backup restore of {shared_dir} must never
    # swap the binaries the gateways are executing.
    root = rt.store_root(shared)
    assert root.name == "evolve-oc"
    assert root.parent == shared.parent
    assert shared not in root.parents and root != shared


def test_installed_versions_lists_only_usable_ones(shared):
    _install_fake(shared, "2026.9.2")
    _install_fake(shared, "2026.9.4")
    # A directory with no entrypoint is a half-install, not a version.
    (rt.store_root(shared) / "2026.9.9").mkdir(parents=True)
    (rt.store_root(shared) / "not-a-version").mkdir(parents=True)
    assert rt.installed_versions(shared) == ["2026.9.2", "2026.9.4"]


def test_versions_sort_numerically_not_lexically(shared):
    for v in ["2026.9.2", "2026.9.10", "2026.9.9"]:
        _install_fake(shared, v)
    # A string sort would put 2026.9.10 before 2026.9.2 and hand the operator
    # the wrong rollback target on exactly the release that needs one.
    assert rt.installed_versions(shared) == ["2026.9.2", "2026.9.9", "2026.9.10"]


def test_an_absent_store_is_empty_not_an_error(tmp_path):
    assert rt.installed_versions(tmp_path / "nothing") == []


# ── Install ──────────────────────────────────────────────────────────────────


def test_install_uses_a_private_prefix_and_never_links(shared, monkeypatch):
    calls = []

    def fake(argv):
        calls.append(list(argv))
        if argv[:2] == ["npm", "view"]:
            return 0, _VIEW_OK, ""
        if argv[0] == "npm":
            _install_fake(shared, "2026.9.4")
            return 0, "", ""
        return 0, "2026.9.4", ""

    rt.install_version("2026.9.4", shared_dir=shared, runner=fake)
    npm = next(c for c in calls if c[:2] == ["npm", "install"])
    assert "--prefix" in npm and "-g" in npm, "-g is what gives lib/node_modules + bin"
    assert str(rt.version_dir("2026.9.4", shared)) in npm
    assert "openclaw@2026.9.4" in npm, "the exact version, never a tag"
    assert not any("link" in " ".join(c) for c in calls), (
        "a linked version is a global version — the thing this replaces"
    )


def test_install_is_idempotent(shared):
    _install_fake(shared, "2026.9.4")
    run = runner({"--version": (0, "2026.9.4", "")})
    rt.install_version("2026.9.4", shared_dir=shared, runner=run)
    assert not any(c[0] == "npm" for c in run.calls), (  # type: ignore[attr-defined]
        "an already-installed version is verified, not reinstalled"
    )


def test_a_present_but_unrunnable_version_is_refused_not_returned(shared):
    _install_fake(shared, "2026.9.4")
    run = runner({"--version": (1, "", "SyntaxError")})
    with pytest.raises(OcRuntimeError, match="does not run"):
        rt.install_version("2026.9.4", shared_dir=shared, runner=run)


def test_install_that_produces_no_entrypoint_is_refused(shared):
    run = runner({"npm view": (0, _VIEW_OK, ""), "npm": (0, "", "")})  # nothing on disk
    with pytest.raises(OcRuntimeError, match="missing"):
        rt.install_version("2026.9.4", shared_dir=shared, runner=run)


# ── Remove refuses while pinned — the invariant rollback rests on ────────────


def test_remove_refuses_while_a_bot_pins_the_version(shared):
    _install_fake(shared, "2026.9.2")
    net = _network("team-bot-a", "team-bot-b", **{"team-bot-a": "2026.9.2"})
    with pytest.raises(OcRuntimeError, match="pinned by team-bot-a"):
        rt.remove_version("2026.9.2", net, shared_dir=shared, unit_dir=shared)
    assert rt.is_installed("2026.9.2", shared), "the refusal must not have deleted it"


def test_remove_works_once_nothing_pins_it(shared):
    _install_fake(shared, "2026.9.2")
    _install_fake(shared, "2026.9.4")
    net = _network("team-bot-a", **{"team-bot-a": "2026.9.4"})
    rt.remove_version("2026.9.2", net, shared_dir=shared, unit_dir=shared)
    assert rt.installed_versions(shared) == ["2026.9.4"]


def test_remove_names_every_holder_so_the_operator_can_act(shared):
    _install_fake(shared, "2026.9.2")
    net = _network("a", "b", "c", **{"a": "2026.9.2", "c": "2026.9.2"})
    with pytest.raises(OcRuntimeError) as exc:
        rt.remove_version("2026.9.2", net, shared_dir=shared, unit_dir=shared)
    assert "a, c" in str(exc.value)


# ── Pins ─────────────────────────────────────────────────────────────────────


def test_an_unpinned_bot_reports_unpinned_not_a_guess():
    # Guessing here is how a pod ends up believing it has a rollback it has not.
    assert rt.pinned_version("team-bot-a", _network("team-bot-a")) == rt.UNPINNED


def test_a_garbage_pin_value_reads_as_unknown_not_unpinned():
    # "Could not read the pin" is not "no pin" (D-CS7): as unpinned, it made
    # the version the bot runs look removable.
    net = _network("team-bot-a")
    net["bots"]["team-bot-a"][rt.PIN_KEY] = "../../etc"
    assert rt.pinned_version("team-bot-a", net).is_unknown


def test_set_pin_is_pure(shared):
    net = _network("team-bot-a")
    out = rt.set_pin("team-bot-a", "2026.9.4", net)
    assert out["bots"]["team-bot-a"][rt.PIN_KEY] == "2026.9.4"
    assert rt.pinned_version("team-bot-a", net) == rt.UNPINNED, "input must not be mutated"


def test_set_pin_refuses_a_bot_the_registry_does_not_have():
    with pytest.raises(OcRuntimeError, match="not a bot"):
        rt.set_pin("ghost", "2026.9.4", _network("team-bot-a"))


# ── plan_pin: refusal, and the order that matters ────────────────────────────


def test_pin_refuses_a_version_that_is_not_installed(shared):
    net = _network("team-bot-a")
    with pytest.raises(OcRuntimeError, match="not installed"):
        rt.plan_pin("team-bot-a", "2026.9.4", net, shared_dir=shared)


def test_pin_plan_names_only_steps_the_command_runs(shared):
    # The plan used to print a `channels` step nothing executed — a plan that
    # prints steps it does not run is worse than no plan. The command's own
    # run is compared against this list in test_oc_runtime_fail_closed.py.
    _install_fake(shared, "2026.9.4")
    plan = rt.plan_pin("team-bot-a", "2026.9.4", _network("team-bot-a"), shared_dir=shared)
    assert [s.kind for s in plan.steps] == ["registry", "restart", "probe"]
    assert plan.steps[-1].kind == "probe", "'did it come back' before the next bot"


def test_pin_plan_touches_exactly_one_gateway(shared):
    # "never restart more than one gateway at a time" is the standing guardrail;
    # a plan that named a second bot would be a plan that breaks it.
    _install_fake(shared, "2026.9.4")
    others = ["team-bot-b", "team-bot-c"]
    plan = rt.plan_pin("team-bot-a", "2026.9.4",
                       _network("team-bot-a", *others), shared_dir=shared)
    restart = [s for s in plan.steps if s.kind == "restart"]
    assert len(restart) == 1
    assert "team-bot-a" in restart[0].detail
    whole_plan = " ".join(s.detail for s in plan.steps)
    for other in others:
        assert other not in whole_plan, f"the plan must not touch {other}"


def test_rollback_is_a_pin_with_the_same_steps(shared):
    _install_fake(shared, "2026.9.2")
    _install_fake(shared, "2026.9.4")
    net = _network("team-bot-a", **{"team-bot-a": "2026.9.4"})
    back = rt.plan_pin("team-bot-a", "2026.9.2", net, shared_dir=shared)
    forward = rt.plan_pin("team-bot-a", "2026.9.4",
                          _network("team-bot-a", **{"team-bot-a": "2026.9.2"}),
                          shared_dir=shared)
    assert back.is_rollback is True
    assert forward.is_rollback is False
    assert [s.kind for s in back.steps] == [s.kind for s in forward.steps], (
        "a rollback that runs different code than an upgrade is a path that is "
        "only exercised on the worst day it will ever have"
    )


def test_first_pin_of_an_unpinned_bot_is_not_a_rollback(shared):
    _install_fake(shared, "2026.9.4")
    plan = rt.plan_pin("team-bot-a", "2026.9.4", _network("team-bot-a"), shared_dir=shared)
    assert plan.previous is None
    assert plan.is_rollback is False


# ── What the plist gets ──────────────────────────────────────────────────────


def test_a_pinned_bot_execs_its_own_version(shared):
    _install_fake(shared, "2026.9.4")
    net = _network("team-bot-a", **{"team-bot-a": "2026.9.4"})
    got = rt.resolve_gateway_runtime(
        "team-bot-a", net, platform_name="macos", shared_dir=shared,
    )
    assert got.pinned is True
    assert got.entrypoint == str(rt.version_entrypoint("2026.9.4", shared))
    assert "/opt/homebrew" not in got.entrypoint


def test_an_unpinned_bot_gets_exactly_todays_global_resolution(shared):
    got = rt.resolve_gateway_runtime(
        "team-bot-a", _network("team-bot-a"), platform_name="macos", shared_dir=shared,
    )
    assert got.pinned is False
    assert got.entrypoint in rt.GLOBAL_OC_CANDIDATES
    assert got.env == {} and got.path_prefix is None, (
        "a pod that never ran `oc adopt` must be completely unaffected"
    )


def test_no_network_at_all_is_the_global_answer(shared):
    got = rt.resolve_gateway_runtime(
        "team-bot-a", None, platform_name="macos", shared_dir=shared,
    )
    assert got.pinned is False


def test_a_pinned_bot_gets_its_own_bin_first_on_PATH(shared):
    # Otherwise the gateway and the `openclaw` children it spawns (doctor,
    # plugins, config) run different builds — the split the pins exist to
    # remove, and far harder to see than a wrong plist.
    _install_fake(shared, "2026.9.4")
    net = _network("team-bot-a", **{"team-bot-a": "2026.9.4"})
    got = rt.resolve_gateway_runtime(
        "team-bot-a", net, platform_name="macos", shared_dir=shared,
    )
    assert got.path_prefix == str(rt.version_bin_dir("2026.9.4", shared))
    assert got.env["OPENCLAW_VERSION_PIN"] == "2026.9.4"


def test_linux_default_never_bakes_a_homebrew_path():
    # A /opt/homebrew ExecStart on a Linux pod 203/EXEC crash-loops.
    assert "/opt/homebrew" not in rt.global_entrypoint("linux")


def test_an_unreadable_pin_refuses_instead_of_falling_back_to_the_global(shared):
    # The global binary may be the very version the bot is held off. A bot
    # that does not start is visible; one quietly on the wrong build is not.
    broken = {"bots": {"team-bot-a": "not-a-dict-at-all"}}
    with pytest.raises(rt.PinResolutionError, match="team-bot-a: pin unreadable"):
        rt.resolve_gateway_runtime(
            "team-bot-a", broken, platform_name="macos", shared_dir=shared,
        )


# ── Adopt ────────────────────────────────────────────────────────────────────


def test_adopt_plans_every_unpinned_bot_in_a_stable_order(shared):
    # Deliberately supplied out of order, so the sort is what is under test.
    net = _network("team-bot-c", "admin-bot", "personal-bot")
    plan = rt.plan_adopt(net, "2026.9.2", shared_dir=shared)
    assert plan.restart_order == ["admin-bot", "personal-bot", "team-bot-c"], (
        "sorted, so a resumed adopt repeats the same sequence — an operator "
        "watching bot 4 of 9 needs the list not to reshuffle"
    )
    assert plan.copy_needed is True


def test_adopt_skips_bots_already_on_that_version(shared):
    net = _network("a", "b", **{"a": "2026.9.2"})
    plan = rt.plan_adopt(net, "2026.9.2", shared_dir=shared)
    assert plan.restart_order == ["b"]
    assert plan.already_pinned == ["a"]


def _global_install(tmp_path, version="2026.9.2"):
    src_prefix = tmp_path / "homebrew"
    pkg = src_prefix / "lib" / "node_modules" / "openclaw"
    (pkg / "dist").mkdir(parents=True)
    (pkg / "dist" / "index.js").write_text("// global\n")
    (pkg / "package.json").write_text(json.dumps(
        {"version": version, "bin": {"openclaw": "openclaw.mjs"}}))
    return src_prefix, pkg


def test_adopt_copies_and_leaves_the_original_in_place(shared, tmp_path):
    src_prefix, pkg = _global_install(tmp_path)
    rt.adopt_copy(src_prefix, "2026.9.2", shared_dir=shared,
                  runner=runner({"--version": (0, "2026.9.2", "")}))
    assert rt.is_installed("2026.9.2", shared)
    assert (pkg / "dist" / "index.js").exists(), (
        "an adopt that moved the global install would be the same irreversible "
        "fleet-wide switch this design abolishes, done by the design itself"
    )


def test_adopt_refuses_to_overwrite_an_installed_version(shared, tmp_path):
    _install_fake(shared, "2026.9.2")
    src_prefix, _pkg = _global_install(tmp_path)
    with pytest.raises(OcRuntimeError, match="already exists"):
        rt.adopt_copy(src_prefix, "2026.9.2", shared_dir=shared)


def test_adopt_with_no_global_install_says_so(shared, tmp_path):
    with pytest.raises(OcRuntimeError, match="no OpenClaw install"):
        rt.adopt_copy(tmp_path / "empty", "2026.9.2", shared_dir=shared)


# ── The `oc list` surface ────────────────────────────────────────────────────


def test_describe_store_marks_in_use_versions_unremovable(shared):
    _install_fake(shared, "2026.9.2")
    _install_fake(shared, "2026.9.4")
    net = _network("a", "b", **{"a": "2026.9.4", "b": "2026.9.4"})
    rows = {r["version"]: r for r in rt.describe_store(net, shared_dir=shared)}
    assert rows["2026.9.4"]["pinned_by"] == ["a", "b"]
    assert rows["2026.9.4"]["removable"] is False
    assert rows["2026.9.2"]["removable"] is True


def test_unpinned_bots_are_reported_because_they_have_no_rollback():
    net = _network("a", "b", **{"a": "2026.9.4"})
    assert rt.unpinned_bots(net) == ["b"]


# ── The bot-side CLI resolution ──────────────────────────────────────────────


def test_a_pinned_bots_cli_resolves_to_its_own_version(shared):
    _install_fake(shared, "2026.9.4")
    net = _network("team-bot-a", **{"team-bot-a": "2026.9.4"})
    got = rt.bot_openclaw_bin("team-bot-a", net, shared_dir=shared)
    assert got == str(rt.version_bin_dir("2026.9.4", shared) / "openclaw")


def test_an_unpinned_bots_cli_is_None_meaning_use_the_global(shared):
    assert rt.bot_openclaw_bin("team-bot-a", _network("team-bot-a"), shared_dir=shared) is None


# ── Linux twin ───────────────────────────────────────────────────────────────


def test_the_store_has_a_linux_twin(tmp_path):
    # Every path comes from the platform profile; a macOS-only store would put
    # /Users/Shared into a systemd unit on a Linux pod.
    linux_shared = Path("/var/lib/evolve")
    assert rt.store_root(linux_shared) == Path("/var/lib/evolve-oc")
    macos_shared = Path("/Users/Shared/evolve")
    assert rt.store_root(macos_shared) == Path("/Users/Shared/evolve-oc")


def test_pinned_entrypoints_are_platform_neutral(tmp_path):
    # The pinned path is derived from the store root, so it inherits the
    # platform answer rather than hardcoding one.
    entry = rt.version_entrypoint("2026.9.4", Path("/var/lib/evolve"))
    assert str(entry).startswith("/var/lib/evolve-oc/2026.9.4/")
    assert "/Users/Shared" not in str(entry)


# ── The CLI verbs ────────────────────────────────────────────────────────────
#
# Driven through click so the commands are exercised the way an operator
# reaches them — by name, through the group — rather than as bare functions.


def _invoke(args, network, tmp_path, monkeypatch, shared):
    from click.testing import CliRunner

    from evolve_admin import ocadmin

    net_path = tmp_path / "network.json"
    net_path.write_text(json.dumps(network))
    # Capture the real function BEFORE patching — a lambda that calls the name
    # it is replacing recurses forever.
    _real_store_root = rt.store_root
    monkeypatch.setattr(rt, "store_root", lambda sd=None: _real_store_root(shared))
    monkeypatch.setattr(rt, "global_prefix", lambda name: _global_install(tmp_path)[0])
    monkeypatch.setattr(rt, "verify_version", lambda v, **kw: (True, v))
    return CliRunner().invoke(
        ocadmin.menu_group, list(args), obj={"network_path": net_path},
    )


def test_oc_list_names_versions_and_who_pins_them(tmp_path, monkeypatch, shared):
    _install_fake(shared, "2026.9.2")
    _install_fake(shared, "2026.9.4")
    net = _network("team-bot-a", "team-bot-b", **{"team-bot-a": "2026.9.4"})
    res = _invoke(["list"], net, tmp_path, monkeypatch, shared)
    assert res.exit_code == 0, res.output
    assert "2026.9.4" in res.output and "team-bot-a" in res.output


def test_oc_list_calls_out_bots_with_no_rollback(tmp_path, monkeypatch, shared):
    _install_fake(shared, "2026.9.4")
    net = _network("team-bot-a", "team-bot-b", **{"team-bot-a": "2026.9.4"})
    res = _invoke(["list"], net, tmp_path, monkeypatch, shared)
    assert "not pinned" in res.output
    assert "team-bot-b" in res.output


def test_oc_remove_refuses_and_exits_nonzero_while_pinned(tmp_path, monkeypatch, shared):
    _install_fake(shared, "2026.9.4")
    net = _network("team-bot-a", **{"team-bot-a": "2026.9.4"})
    res = _invoke(["remove", "2026.9.4"], net, tmp_path, monkeypatch, shared)
    assert res.exit_code == 1
    assert "pinned by team-bot-a" in res.output
    assert rt.is_installed("2026.9.4", shared), "the refusal must not have deleted it"


def test_oc_pin_dry_run_prints_the_plan_and_changes_nothing(
    tmp_path, monkeypatch, shared,
):
    _install_fake(shared, "2026.9.4")
    net = _network("team-bot-a")
    res = _invoke(["pin", "team-bot-a", "2026.9.4", "--dry-run"],
                  net, tmp_path, monkeypatch, shared)
    assert res.exit_code == 0, res.output
    assert "registry" in res.output and "restart" in res.output
    saved = json.loads((tmp_path / "network.json").read_text())
    assert rt.pinned_version("team-bot-a", saved) == rt.UNPINNED, "--dry-run must not write"


def test_oc_pin_refuses_an_uninstalled_version(tmp_path, monkeypatch, shared):
    res = _invoke(["pin", "team-bot-a", "2026.9.9"],
                  _network("team-bot-a"), tmp_path, monkeypatch, shared)
    assert res.exit_code == 1
    assert "not installed" in res.output


def test_oc_pin_names_a_rollback_as_a_rollback(tmp_path, monkeypatch, shared):
    _install_fake(shared, "2026.9.2")
    _install_fake(shared, "2026.9.4")
    net = _network("team-bot-a", **{"team-bot-a": "2026.9.4"})
    res = _invoke(["pin", "team-bot-a", "2026.9.2", "--dry-run"],
                  net, tmp_path, monkeypatch, shared)
    assert "Rolling back" in res.output


def test_oc_adopt_dry_run_lists_the_per_bot_commands(tmp_path, monkeypatch, shared):
    from evolve_admin import ocadmin

    monkeypatch.setattr(ocadmin, "_installed_version", lambda: "2026.9.2")
    res = _invoke(["adopt", "--dry-run"], _network("team-bot-c", "admin-bot"),
                  tmp_path, monkeypatch, shared)
    assert res.exit_code == 0, res.output
    assert "admin-bot" in res.output and "team-bot-c" in res.output


def test_oc_adopt_refuses_when_it_cannot_name_the_installed_version(
    tmp_path, monkeypatch, shared,
):
    # Adopting a version Evolve cannot name would write an unusable pin.
    from evolve_admin import ocadmin

    monkeypatch.setattr(ocadmin, "_installed_version", lambda: None)
    res = _invoke(["adopt"], _network("a"), tmp_path, monkeypatch, shared)
    assert res.exit_code == 1
    assert "Cannot determine" in res.output


def test_oc_adopt_never_restarts_the_fleet_for_you(tmp_path, monkeypatch, shared):
    # One bot at a time, with a human watching, is the whole lesson of
    # 2026-09-07 — so adopt prints commands rather than running them.
    from evolve_admin import ocadmin

    monkeypatch.setattr(ocadmin, "_installed_version", lambda: "2026.9.2")
    res = _invoke(["adopt"], _network("a", "b"), tmp_path, monkeypatch, shared)
    assert "oc pin a 2026.9.2" in res.output
    assert "oc pin b 2026.9.2" in res.output
    assert "one at a time" in res.output.lower()
