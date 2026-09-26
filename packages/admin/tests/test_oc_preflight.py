"""Tests for the OpenClaw upgrade preflight (oc_preflight) and the install-flag
probe (oc_install_flags).

Chip: internal/dispatch/done/oc-upgrade-is-a-guarded-change.md, items 2, 4/7f
and the per-bot rows from item 7 (c, d, e).

Everything here runs against fixture configs and a FAKE target binary — no
network, no npm, no OpenClaw, no bots. The point of preflight is that it can
answer "what would this upgrade do" without doing any of it, so a test suite
that needed the real thing would be testing the wrong property.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from evolve_admin import oc_install_flags
from evolve_admin.oc_preflight import (
    BotPreflightRow,
    PreflightReport,
    channel_packages_stale,
    extract_invalid_keys,
    extract_model_ref_rewrites,
    fetch_target,
    ownership_orphans,
    preflight_bot,
    render_table,
    retired_allow_entries,
    workspace_outside_home,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _config(**over):
    """A plausible bot openclaw.json. Overrides merge at the top level."""
    cfg = {
        "agents": {
            "defaults": {
                "model": {
                    "primary": "anthropic/claude-sonnet-4-6",
                    "fallbacks": ["anthropic/claude-haiku-4-5"],
                },
            },
            "entries": {"main": {"workspace": "/Users/team-bot-a/.openclaw/workspace"}},
        },
        "modelPolicy": {"allow": ["anthropic/claude-sonnet-4-6"]},
    }
    cfg.update(over)
    return cfg


HOME = Path("/Users/team-bot-a")


def fake_runner(responses):
    """Build a runner that answers by matching a substring of the argv."""
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


# ── Item 7e: agent workspace outside the bot's home (RED, config-only) ───────


def test_workspace_inside_home_is_clean():
    assert workspace_outside_home(_config(), HOME) is None


def test_workspace_pointing_at_another_bots_home_is_caught():
    # The shape actually found on the pod: a provisioning leftover pointing at
    # a DIFFERENT bot's home. Accepted by the old runtime, fatal under 2026.9.
    cfg = _config(agents={
        "defaults": {"model": {"primary": "anthropic/claude-sonnet-4-6"}},
        "entries": {"main": {"workspace": "/Users/team-bot-b/.openclaw/workspace"}},
    })
    assert workspace_outside_home(cfg, HOME) == "/Users/team-bot-b/.openclaw/workspace"


def test_workspace_row_needs_no_target_binary():
    # This is the row that makes a bot unbootable, so it must survive a failed
    # fetch — target_cli=None is the "could not reach npm" path.
    cfg = _config(agents={
        "entries": {"main": {"workspace": "/tmp/somewhere-else"}},
    })
    row = preflight_bot(
        "team-bot-a", config=cfg, home=HOME, bot_user="team-bot-a",
        target_cli=None,
    )
    assert row.workspace_outside_home == "/tmp/somewhere-else"
    assert row.blocking is True


def test_workspace_outside_home_survives_a_garbage_entry():
    cfg = _config(agents={"entries": {"main": "not-a-dict", "other": {}}})
    assert workspace_outside_home(cfg, HOME) is None


# ── Item 7c: retired models doctor copies into modelPolicy.allow ─────────────


def test_retired_model_only_in_allow_list_is_reported():
    cfg = _config(modelPolicy={
        "allow": ["anthropic/claude-sonnet-4-6", "anthropic/claude-2-retired"],
    })
    assert retired_allow_entries(cfg, ["anthropic/claude-2-retired"]) == [
        "anthropic/claude-2-retired",
    ]


def test_retired_model_that_is_the_primary_is_NOT_reported():
    # Dropping it would change which model answers — the operator's call, not
    # preflight's. The brief is explicit: only if it is not primary or fallback.
    cfg = _config(
        agents={"defaults": {"model": {"primary": "anthropic/claude-2-retired"}}},
        modelPolicy={"allow": ["anthropic/claude-2-retired"]},
    )
    assert retired_allow_entries(cfg, ["anthropic/claude-2-retired"]) == []


def test_retired_model_that_is_a_fallback_is_NOT_reported():
    cfg = _config(
        agents={"defaults": {"model": {
            "primary": "anthropic/claude-sonnet-4-6",
            "fallbacks": ["anthropic/claude-2-retired"],
        }}},
        modelPolicy={"allow": ["anthropic/claude-2-retired"]},
    )
    assert retired_allow_entries(cfg, ["anthropic/claude-2-retired"]) == []


def test_bare_string_model_default_is_understood():
    # The old wizard format wrote a bare string instead of {primary, fallbacks}.
    cfg = _config(
        agents={"defaults": {"model": "anthropic/claude-2-retired"}},
        modelPolicy={"allow": ["anthropic/claude-2-retired"]},
    )
    assert retired_allow_entries(cfg, ["anthropic/claude-2-retired"]) == []


# ── Item 7d: ownership:explicit orphans channels ─────────────────────────────


def test_ownership_explicit_without_a_default_orphans():
    cfg = _config(agents={"ownership": "explicit", "entries": {"main": {}}})
    assert ownership_orphans(cfg) is True


def test_ownership_explicit_with_a_default_entry_is_fine():
    cfg = _config(agents={
        "ownership": "explicit", "entries": {"main": {"default": True}},
    })
    assert ownership_orphans(cfg) is False


def test_ownership_explicit_with_bindings_is_fine():
    # OC normalises to bindings-per-channel rather than a default marker; a bot
    # that already carries bindings is routing fine.
    cfg = _config(agents={
        "ownership": "explicit",
        "entries": {"main": {"bindings": [{"channel": "slack"}]}},
    })
    assert ownership_orphans(cfg) is False


def test_no_ownership_key_is_fine():
    assert ownership_orphans(_config()) is False


# ── Item 2: model-ref rewrites (RED, blocking) ───────────────────────────────


def test_model_ref_rewrite_is_extracted_with_from_and_to():
    doctor = {"findings": [{
        "checkId": "core/doctor/model-refs",
        "path": "agents.defaults.model.primary",
        "message": "Rewrite retired model ref `anthropic/claude-2-retired` to `anthropic/claude-sonnet-4-6`.",
    }]}
    rows = extract_model_ref_rewrites(doctor)
    assert len(rows) == 1
    assert rows[0]["from"] == "anthropic/claude-2-retired"
    assert rows[0]["to"] == "anthropic/claude-sonnet-4-6"
    assert rows[0]["path"] == "agents.defaults.model.primary"


def test_model_ref_finding_without_a_parseable_pair_is_still_reported():
    # "doctor will change something here and would not say what" is MORE
    # alarming than a clean pair, so it must never be dropped for being
    # unparseable.
    doctor = {"findings": [{
        "checkId": "core/doctor/model-refs",
        "path": "agents.defaults.model",
        "message": "Normalize noncanonical model references.",
    }]}
    rows = extract_model_ref_rewrites(doctor)
    assert len(rows) == 1
    assert "from" not in rows[0]
    assert rows[0]["detail"] == "Normalize noncanonical model references."


def test_unrelated_doctor_findings_are_ignored():
    doctor = {"findings": [
        {"checkId": "core/doctor/heartbeat-cadence-migration",
         "path": "cron/jobs.json",
         "message": "Create heartbeat monitor for agent \"main\" at 30m."},
        {"checkId": "core/doctor/node-hosting-preconditions",
         "path": "gateway.bind",
         "message": "Gateway is only bound to loopback."},
    ]}
    assert extract_model_ref_rewrites(doctor) == []


def test_modelpolicy_allow_findings_count_as_model_refs():
    doctor = {"findings": [{
        "checkId": "core/doctor/model-policy",
        "path": "modelPolicy.allow",
        "message": "Copy allow-list entries.",
    }]}
    assert len(extract_model_ref_rewrites(doctor)) == 1


def test_doctor_without_findings_is_clean():
    assert extract_model_ref_rewrites({"ok": True}) == []
    assert extract_model_ref_rewrites({"findings": "not-a-list"}) == []


# ── config validate parsing ──────────────────────────────────────────────────


def test_invalid_keys_and_retired_surfaces_are_separated():
    validate = {"valid": False, "warnings": [
        {"path": "meta.lastTouchedAt", "message": "unknown key"},
        {"path": "compaction.reserveTokensFloor", "message": "retired surface"},
    ], "errors": [{"path": "auth.cooldowns", "message": "not permitted"}]}
    invalid, retired = extract_invalid_keys(validate)
    assert "meta.lastTouchedAt" in invalid
    assert "auth.cooldowns" in invalid
    assert retired == ["compaction.reserveTokensFloor"]


# ── The whole row, against a fake target binary ──────────────────────────────


def test_preflight_bot_against_a_fake_target():
    run = fake_runner({
        "config validate": (0, json.dumps({
            "valid": False,
            "warnings": [{"path": "meta.lastTouchedAt", "message": "unknown key"}],
        }), ""),
        "doctor": (0, json.dumps({"findings": [{
            "path": "agents.defaults.model.primary",
            "message": "Rewrite `anthropic/claude-2-retired` to `anthropic/claude-sonnet-4-6`.",
        }]}), ""),
    })
    row = preflight_bot(
        "team-bot-a", config=_config(), home=HOME, bot_user="team-bot-a",
        target_cli=Path("/scratch/openclaw"), runner=run,
    )
    assert row.invalid_keys == ["meta.lastTouchedAt"]
    assert row.model_ref_rewrites[0]["to"] == "anthropic/claude-sonnet-4-6"
    assert row.blocking is True
    assert row.error is None


def test_the_target_is_run_AS_THE_BOT_with_its_own_home():
    # Item 7b: the user and home come from the registry, and the CLI dies with
    # an opaque uv_cwd EACCES if it is run any other way.
    run = fake_runner({
        "config validate": (0, "{}", ""),
        "doctor": (0, "{}", ""),
    })
    preflight_bot(
        "team-bot-a", config=_config(), home=Path("/Users/oddly-named-account"),
        bot_user="oddly-named-user", target_cli=Path("/scratch/openclaw"), runner=run,
    )
    for argv in run.calls:  # type: ignore[attr-defined]
        assert argv[:4] == ["sudo", "-H", "-u", "oddly-named-user"]
        assert "HOME=/Users/oddly-named-account" in argv


def test_preflight_NEVER_passes_fix_to_doctor():
    # The guardrail the whole chip exists to enforce. A preflight that mutates
    # is not a preflight.
    run = fake_runner({"config validate": (0, "{}", ""), "doctor": (0, "{}", "")})
    preflight_bot(
        "team-bot-a", config=_config(), home=HOME, bot_user="team-bot-a",
        target_cli=Path("/scratch/openclaw"), runner=run,
    )
    for argv in run.calls:  # type: ignore[attr-defined]
        assert "--fix" not in argv
    assert any("doctor" in a for argv in run.calls for a in argv)  # type: ignore[attr-defined]


def test_a_target_that_cannot_be_run_reports_error_not_clean():
    run = fake_runner({})  # everything returns rc=1 with no stdout
    row = preflight_bot(
        "team-bot-a", config=_config(), home=HOME, bot_user="team-bot-a",
        target_cli=Path("/scratch/openclaw"), runner=run,
    )
    assert row.error is not None
    assert row.clean is False, "'could not check' must never read as clean"


# ── Item 1: fail closed — every "could not look" shape blocks ────────────────
#
# internal/dispatch/reviews/pr-4277.md (D-CS7): `blocking` used to read only
# model_ref_rewrites/workspace_outside_home, so a failed npm install, an
# unreadable openclaw.json, a registry failure, or a doctor payload in a
# schema this code doesn't recognize all produced blocking=False, exit 0 —
# the class of bug this whole chip exists to close.


def test_a_row_with_a_bare_error_blocks():
    # e.g. "openclaw.json unreadable" or "registry lookup failed" — set
    # directly on the row, no model-ref/workspace finding attached.
    row = BotPreflightRow(bot_id="team-bot-a", error="openclaw.json unreadable")
    assert row.blocking is True
    report = PreflightReport(target_version="2026.9.4", rows=[row])
    assert report.blocking is True


def test_a_target_error_blocks_even_with_zero_rows():
    report = PreflightReport(
        target_version="2026.9.4",
        target_error="npm install openclaw@2026.9.4 failed: registry unreachable",
    )
    assert report.blocking is True


def test_doctor_json_with_an_unrecognized_schema_blocks():
    # The CLI returned SOMETHING, and it parsed as JSON, but it is not a
    # doctor payload this code recognizes (no `findings` list) — this must
    # read as "could not check", not as "doctor found nothing".
    run = fake_runner({
        "config validate": (0, "{}", ""),
        "doctor": (0, json.dumps({"status": "unexpected-shape"}), ""),
    })
    row = preflight_bot(
        "team-bot-a", config=_config(), home=HOME, bot_user="team-bot-a",
        target_cli=Path("/scratch/openclaw"), runner=run,
    )
    assert row.error is not None
    assert row.blocking is True
    assert row.clean is False


def test_a_registry_lookup_failure_is_a_blocking_row_in_the_full_run(monkeypatch):
    from evolve_admin import config as _cfg
    from evolve_admin.oc_preflight import preflight

    def _boom_bot_home(_bot_id, _network=None):
        raise KeyError("team-bot-a not in registry")

    monkeypatch.setattr(_cfg, "bot_home", _boom_bot_home)
    network = {"bots": {"team-bot-a": {}}}
    report = preflight(
        network, "2026.9.4", target_cli=Path("/scratch/openclaw"),
        read_config=lambda *_a: _config(),
    )
    assert len(report.rows) == 1
    assert report.rows[0].error is not None
    assert report.blocking is True


def test_the_existing_clean_fixture_still_passes_closed():
    # The other side of fail-closed: a report where every row genuinely
    # looked and found nothing must NOT block.
    report = PreflightReport(target_version="2026.9.4", rows=[
        BotPreflightRow(bot_id="team-bot-a"),
        BotPreflightRow(bot_id="team-bot-b"),
    ])
    assert report.blocking is False


def test_preflight_is_idempotent():
    run = fake_runner({
        "config validate": (0, json.dumps({"warnings": []}), ""),
        "doctor": (0, json.dumps({"findings": []}), ""),
    })
    kw = dict(
        config=_config(), home=HOME, bot_user="team-bot-a",
        target_cli=Path("/scratch/openclaw"), runner=run,
    )
    first = preflight_bot("team-bot-a", **kw)
    second = preflight_bot("team-bot-a", **kw)
    assert first == second


# ── Item 3/6/7: fetch_target — cleanup, argv, permissions ────────────────────


def _npm_fake(rc=0, out="", err=""):
    def run(argv):
        run.calls.append(list(argv))
        return rc, out, err
    run.calls = []  # type: ignore[attr-defined]
    return run


@pytest.fixture(autouse=True)
def _pin_non_root_euid(monkeypatch):
    # fetch_target branches on os.geteuid() == 0 (item 7 — drop root before
    # running npm's lifecycle scripts). Pinned to a non-root value for every
    # test in this module so a CI runner that happens to run as root (some
    # containerized runners do) can't silently change which argv these tests
    # observe; the root branch gets its own dedicated test below.
    import evolve_admin.oc_preflight as _pf
    monkeypatch.setattr(_pf.os, "geteuid", lambda: 501)


def test_fetch_target_removes_a_prefix_it_created_itself_on_exit(tmp_path, monkeypatch):
    # The mkdtemp prefix used to never be removed — every preflight run left
    # a full npm install on disk forever. It must be gone once the `with`
    # block exits, success or failure. `tempfile.mkdtemp` is redirected into
    # tmp_path (patching the TMPDIR env var is unreliable — CPython caches
    # gettempdir()'s answer the first time anything calls it).
    import tempfile as _tempfile
    created = tmp_path / "evolve-oc-preflight-fake"
    monkeypatch.setattr(_tempfile, "mkdtemp", lambda **kw: str(created))
    created.mkdir()
    run = _npm_fake(rc=1, err="network unreachable")
    with fetch_target("2026.9.4", runner=run) as (cli, err):
        assert cli is None
        assert err is not None
    assert not created.exists(), "scratch prefix was not removed"


def test_fetch_target_does_not_clean_a_caller_supplied_dest(tmp_path):
    dest = tmp_path / "scratch"
    dest.mkdir()
    marker = dest / "keep-me"
    marker.write_text("x")
    run = _npm_fake(rc=1, err="network unreachable")
    with fetch_target("2026.9.4", dest=dest, runner=run) as (cli, err):
        assert cli is None
        assert err is not None
    assert marker.exists(), "a caller-supplied dest must never be removed"


def test_fetch_target_argv_is_npm_install_prefix_never_brew_or_global(tmp_path):
    run = _npm_fake(rc=0)
    dest = tmp_path / "scratch"
    with fetch_target("2026.9.4", dest=dest, runner=run):
        pass
    assert len(run.calls) == 1  # type: ignore[attr-defined]
    argv = run.calls[0]  # type: ignore[attr-defined]
    assert argv[0] == "npm"
    assert argv[1] == "install"
    assert "--prefix" in argv
    assert argv[argv.index("--prefix") + 1] == str(dest)
    assert "brew" not in argv
    assert "-g" not in argv
    assert "--global" not in argv


def test_fetch_target_never_ignores_scripts():
    # Tried first and rejected: measured live against the pod, OpenClaw's
    # own CLI refuses to run without its lifecycle script having completed
    # ("package lifecycle is incomplete. Reinstall with package scripts
    # enabled, then retry.") — --ignore-scripts breaks the fetch outright,
    # so it must never be on the argv (item 7's actual fix is the
    # root-drop below, not this flag).
    run = _npm_fake(rc=0)
    with fetch_target("2026.9.4", runner=run):
        pass
    assert "--ignore-scripts" not in run.calls[0]  # type: ignore[attr-defined]


def test_fetch_target_drops_to_evolve_when_running_as_root(monkeypatch, tmp_path):
    # item 7: npm install may run as root under the CLI's own `sudo`
    # wrapper. An npm package's lifecycle scripts are arbitrary code, so the
    # install itself is handed to `evolve` (the daemon's own unprivileged
    # account) instead of running as root.
    import evolve_admin.oc_preflight as _pf
    monkeypatch.setattr(_pf.os, "geteuid", lambda: 0)
    monkeypatch.setattr(_pf.os, "chown", lambda *a, **kw: None)

    class _FakePasswd:
        pw_uid, pw_gid = 507, 20

    monkeypatch.setattr(_pf.pwd, "getpwnam", lambda name: _FakePasswd(), raising=False)
    run = _npm_fake(rc=0)
    dest = tmp_path / "scratch"
    with fetch_target("2026.9.4", dest=dest, runner=run):
        pass
    argv = run.calls[0]  # type: ignore[attr-defined]
    assert argv[:4] == ["sudo", "-H", "-u", "evolve"]
    assert "npm" in argv and "install" in argv


def test_fetch_target_falls_back_to_root_when_evolve_account_is_absent(monkeypatch, tmp_path):
    # A dev box or CI container running as root with no 'evolve' account —
    # the fetch still has to work, just without the privilege drop.
    import evolve_admin.oc_preflight as _pf
    monkeypatch.setattr(_pf.os, "geteuid", lambda: 0)

    def _boom(name):
        raise KeyError(name)

    monkeypatch.setattr(_pf.pwd, "getpwnam", _boom, raising=False)
    run = _npm_fake(rc=0)
    dest = tmp_path / "scratch"
    with fetch_target("2026.9.4", dest=dest, runner=run):
        pass
    argv = run.calls[0]  # type: ignore[attr-defined]
    assert argv[0] == "npm"


def test_fetch_target_prefix_is_traversable_by_a_bot_user(tmp_path):
    # The bot the target CLI is later run as (via sudo -u <bot>) must be
    # able to traverse this directory. mkdtemp's default 0700, owned by
    # whoever ran the fetch (root under sudo), blocks that outright — every
    # row would come back "Permission denied" before any check ran
    # (second-pass review, item 6). Real filesystem, real chmod — only the
    # npm install subprocess is faked, so this pins the permission fix
    # itself rather than the runner's behavior.
    dest = tmp_path / "scratch"
    run = _npm_fake(rc=0)
    with fetch_target("2026.9.4", dest=dest, runner=run):
        mode = stat.S_IMODE(dest.stat().st_mode)
        assert mode == 0o755, f"expected 0755 (traversable, not writable), got {oct(mode)}"


# ── Channel packages ─────────────────────────────────────────────────────────


def test_channel_packages_on_a_different_release_line_are_stale(tmp_path):
    pkg = tmp_path / ".openclaw" / "npm" / "node_modules" / "@openclaw" / "slack"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"version": "2026.7.1"}))
    assert channel_packages_stale(tmp_path, "2026.9.4") == ["@openclaw/slack@2026.7.1"]


def test_channel_packages_on_the_same_line_are_not_stale(tmp_path):
    pkg = tmp_path / ".openclaw" / "npm" / "node_modules" / "@openclaw" / "slack"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"version": "2026.9.1"}))
    assert channel_packages_stale(tmp_path, "2026.9.4") == []


def test_missing_npm_tree_is_not_an_error(tmp_path):
    assert channel_packages_stale(tmp_path, "2026.9.4") == []


# ── The table ────────────────────────────────────────────────────────────────


def test_render_table_marks_blocking_rows_and_says_it_is_blocked():
    report = PreflightReport(target_version="2026.9.4", rows=[
        BotPreflightRow(
            bot_id="team-bot-a",
            model_ref_rewrites=[{
                "path": "agents.defaults.model.primary",
                "from": "anthropic/claude-2-retired",
                "to": "anthropic/claude-sonnet-4-6",
                "detail": "",
            }],
        ),
        BotPreflightRow(bot_id="team-bot-b"),
    ])
    out = render_table(report)
    assert "RED" in out
    assert "anthropic/claude-2-retired -> anthropic/claude-sonnet-4-6" in out
    assert "BLOCKED" in out
    assert report.blocking is True
    assert report.model_ref_bots == ["team-bot-a"]


def test_a_clean_report_is_not_blocked():
    report = PreflightReport(target_version="2026.9.4", rows=[
        BotPreflightRow(bot_id="team-bot-a"),
    ])
    assert report.blocking is False
    assert "BLOCKED" not in render_table(report)
    assert "clean" in report.summary()


def test_report_json_carries_blocking_per_row():
    report = PreflightReport(target_version="2026.9.4", rows=[
        BotPreflightRow(bot_id="a", workspace_outside_home="/elsewhere"),
    ])
    data = report.to_json()
    assert data["blocking"] is True
    assert data["rows"][0]["blocking"] is True


# ── Item 4 / 7f: the install-flag probe ──────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_flag_cache():
    # Cleared directly rather than through a reset helper: a function whose
    # only caller is a test is dead code in the shipped module, and the
    # dead-code guard is right to say so.
    oc_install_flags._CACHE.clear()
    yield
    oc_install_flags._CACHE.clear()


def test_flags_are_probed_from_the_runtimes_own_help(monkeypatch):
    monkeypatch.setattr(
        oc_install_flags, "_probe_help",
        lambda _bin: "--force  overwrite\n--accept-capabilities  consent\n",
    )
    assert oc_install_flags.required_install_flags("/oc") == (
        "--force", "--accept-capabilities",
    )


def test_a_runtime_that_offers_neither_gets_neither(monkeypatch):
    # Passing a flag an older OC never heard of is itself a failure.
    monkeypatch.setattr(oc_install_flags, "_probe_help", lambda _bin: "usage: install\n")
    assert oc_install_flags.required_install_flags("/oc") == ()


def test_an_unreadable_probe_degrades_to_pre_2026_9_behaviour(monkeypatch):
    monkeypatch.setattr(oc_install_flags, "_probe_help", lambda _bin: None)
    assert oc_install_flags.required_install_flags("/oc") == ()
    assert oc_install_flags.probe_failed("/oc") is True


def test_the_probe_is_cached_per_binary(monkeypatch):
    calls = []

    def probe(b):
        calls.append(b)
        return "--accept-capabilities\n"

    monkeypatch.setattr(oc_install_flags, "_probe_help", probe)
    oc_install_flags.required_install_flags("/oc-a")
    oc_install_flags.required_install_flags("/oc-a")
    oc_install_flags.required_install_flags("/oc-b")
    assert calls == ["/oc-a", "/oc-b"], "one probe per distinct binary"


def test_probe_failure_is_cached_not_re_probed(monkeypatch):
    # Re-probing a broken binary once per bot turns one bad install into nine
    # slow ones, and the answer cannot change mid-deploy.
    calls = []

    def probe(b):
        calls.append(b)
        return None

    monkeypatch.setattr(oc_install_flags, "_probe_help", probe)
    assert oc_install_flags.required_install_flags("/oc") == ()
    assert oc_install_flags.probe_failed("/oc") is True
    assert oc_install_flags.probe_failed("/oc") is True
    assert calls == ["/oc"], "the failure is an answer and is cached like one"


def test_probe_failed_is_false_when_the_runtime_simply_needs_nothing(monkeypatch):
    monkeypatch.setattr(oc_install_flags, "_probe_help", lambda _b: "usage: install\n")
    assert oc_install_flags.required_install_flags("/oc") == ()
    assert oc_install_flags.probe_failed("/oc") is False, (
        "'needs no flags' and 'could not ask' must never collapse together"
    )
