"""AL-1.2 Lane B *producer* — the claim-minting ``openclaw`` shim.

The consumer (``plugin/src/apps/scheduledAttribution.ts``) has been live since
2026-08-15 and was re-verified end to end on the canary bot on 2026-08-20; what
it had never seen was a claim file written by anything but a human. This module
is the producer, and these tests exercise the REAL generated bash — rendered,
written to disk, and executed against a stub ``openclaw`` — rather than
asserting on the template string. A shim that reads right and behaves wrong is
exactly the failure mode that costs an app its delivery.

The two properties that matter, in order:

1. **Fail-open.** Every non-happy path must still exec the real openclaw with
   the ORIGINAL argv. An unattributed turn is a missing row; a broken cron is
   the user's app going dark (atlas, two weeks of empty digests).
2. **Never invent an app id.** ``app_id`` keys ``usage-by-app.json``, so an id
   outside the safe charset is refused, not repaired.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import app_run_shim, install_helpers  # noqa: E402
from evolve_admin.applications.app_run_shim import (  # noqa: E402
    ENV_APP_ID,
    ENV_CLAIM_DIR,
    ENV_LABEL,
    SHIM_NAME,
    app_run_env,
    ensure_app_run_shim,
    render_shim,
    shim_dir_for,
)
from evolve_admin.applications.install_helpers import (  # noqa: E402
    APP_RUNS_DIR_NAME,
    install_launchd_command_action,
    write_app_run_claim,
)

_STUB_OPENCLAW = """#!/bin/bash
printf '%s\\n' "$@" > "$EVOLVE_STUB_ARGV_OUT"
"""


@pytest.fixture()
def rig(tmp_path: Path):
    """A workspace with the real generated shim, a stub ``openclaw`` behind it
    on PATH, and a claim dir — i.e. the app-cron environment the plist builds."""
    workspace = tmp_path / "workspace"
    shim_dir = ensure_app_run_shim(workspace)
    assert shim_dir, "shim must be written"

    real_dir = tmp_path / "bin"
    real_dir.mkdir()
    (real_dir / "openclaw").write_text(_STUB_OPENCLAW)
    (real_dir / "openclaw").chmod(0o755)

    claim_dir = tmp_path / "shared" / "canary_bot" / APP_RUNS_DIR_NAME
    argv_out = tmp_path / "argv.txt"

    def run(*args: str, env_overrides: dict | None = None):
        env = {
            **os.environ,
            "PATH": f"{shim_dir}:{real_dir}:/usr/bin:/bin",
            ENV_APP_ID: "morning-briefing",
            ENV_LABEL: "ai.evolve.canary_bot.morning-briefing",
            ENV_CLAIM_DIR: str(claim_dir),
            "EVOLVE_STUB_ARGV_OUT": str(argv_out),
        }
        env.pop("EVOLVE_APP_RUN_SHIM_ACTIVE", None)
        for k, v in (env_overrides or {}).items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        proc = subprocess.run(
            [str(Path(shim_dir) / SHIM_NAME), *args],
            capture_output=True, text=True, env=env,
        )
        argv = argv_out.read_text().splitlines() if argv_out.exists() else None
        return proc, argv

    def claims() -> list[Path]:
        return sorted(claim_dir.glob("*.json")) if claim_dir.exists() else []

    return mock.Mock(workspace=workspace, shim_dir=shim_dir, claim_dir=claim_dir,
                     run=run, claims=claims)


# ── the generated shim ───────────────────────────────────────────────────────


def test_rendered_shim_is_valid_bash(tmp_path: Path) -> None:
    body = render_shim("/some/shim/dir")
    script = tmp_path / "openclaw"
    script.write_text(body)
    # ``bash -n`` is the gate that would have caught a quoting slip before it
    # reached a pod (the apostrophe-in-heredoc class).
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    assert "__EVOLVE_SHIM_DIR__" not in body, "shim dir token must be substituted"
    assert "'/some/shim/dir'" in body


def test_shim_is_executable_and_atomically_refreshed(tmp_path: Path) -> None:
    d = ensure_app_run_shim(tmp_path / "ws")
    shim = Path(d) / SHIM_NAME
    # 0755 pinned: mkstemp mints 0600 and os.replace carries it onto the dest,
    # which would leave launchd a shim the bot cannot exec.
    assert stat.S_IMODE(shim.stat().st_mode) == 0o755
    assert ensure_app_run_shim(tmp_path / "ws") == d  # idempotent
    assert list(Path(d).iterdir()) == [shim], "no temp files left behind"


def test_ensure_shim_returns_empty_on_failure(tmp_path: Path) -> None:
    # A file where the shim dir should be — the caller must get "" and install
    # the cron unchanged rather than raise.
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "evolve").write_text("not a dir")
    assert ensure_app_run_shim(tmp_path / "ws") == ""


# ── the happy path: mint, claim, inject ──────────────────────────────────────


def test_agent_run_mints_claim_and_injects_session_id(rig) -> None:
    proc, argv = rig.run("agent", "--local", "-m", "ping")
    assert proc.returncode == 0

    claims = rig.claims()
    assert len(claims) == 1, "exactly one claim per openclaw agent invocation"
    sid = claims[0].stem

    # --session-id is spliced immediately AFTER the subcommand, so a command
    # with trailing positional args keeps its shape.
    assert argv == ["agent", "--session-id", sid, "--local", "-m", "ping"]

    claim = json.loads(claims[0].read_text())
    assert claim["app_id"] == "morning-briefing"
    assert claim["label"] == "ai.evolve.canary_bot.morning-briefing"
    assert claim["ts"]
    assert stat.S_IMODE(claims[0].stat().st_mode) == 0o644
    # The operator's proof that the producer fired lands in the cron's err log.
    assert f"claimed session {sid}" in proc.stderr


def test_minted_session_id_is_a_uuid_both_readers_accept(rig) -> None:
    rig.run("agent", "-m", "ping")
    sid = rig.claims()[0].stem
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", sid)
    # The Python writer's guard...
    assert install_helpers._CLAIM_SESSION_ID_RE.match(sid)
    # ...and the plugin consumer's SAFE_SESSION_ID, read from its source so
    # this test fails if that guard ever tightens away from what we mint.
    src = (_ADMIN_DIR.parent / "plugin" / "src" / "apps" / "scheduledAttribution.ts").read_text()
    m = re.search(r"const SAFE_SESSION_ID = /(.+)/;", src)
    assert m, "consumer's SAFE_SESSION_ID guard not found"
    assert re.fullmatch(m.group(1).replace("\\\\", "\\"), sid)


def test_claim_shape_matches_the_python_writer(rig, tmp_path: Path) -> None:
    """One contract, two writers. The shim writes claims in bash (it runs
    inside the app's cron process); ``write_app_run_claim`` is the Python-side
    writer of the same file. Pin them to the same keys so a change to one is a
    visible failure on the other."""
    rig.run("agent", "-m", "ping")
    shim_claim = json.loads(rig.claims()[0].read_text())

    out = write_app_run_claim(tmp_path / "py", "canary_bot", "a" * 8, "morning-briefing",
                              label="ai.evolve.canary_bot.morning-briefing")
    py_claim = json.loads(Path(out["path"]).read_text())

    assert set(shim_claim) == set(py_claim)
    assert shim_claim["app_id"] == py_claim["app_id"]
    assert shim_claim["label"] == py_claim["label"]


def test_two_agent_runs_mint_two_distinct_claims(rig) -> None:
    """Claims are single-use, so a script that shells out twice must get two —
    the reason the shim mints per invocation and not once per cron fire."""
    rig.run("agent", "-m", "one")
    rig.run("agent", "-m", "two")
    claims = rig.claims()
    assert len({c.stem for c in claims}) == 2


# ── fail-open: every other path execs the real openclaw, argv untouched ──────


@pytest.mark.parametrize(
    "args, overrides, why",
    [
        (("message", "send", "--to", "x"), {}, "not an agent run"),
        (("--version",), {}, "no subcommand at all"),
        (("agent", "--session-id", "abc", "-m", "p"), {}, "caller pinned its own session"),
        (("agent", "--session-key=app:x", "-m", "p"), {}, "caller pinned a session key"),
        (("agent", "-m", "p"), {ENV_APP_ID: None}, "no app id in env"),
        (("agent", "-m", "p"), {ENV_APP_ID: "bad id; rm -rf /"}, "app id outside charset"),
        (("agent", "-m", "p"), {ENV_CLAIM_DIR: None}, "no claim dir in env"),
        (("agent", "-m", "p"), {"EVOLVE_APP_RUN_SHIM_ACTIVE": "1"}, "re-entrant call"),
    ],
)
def test_non_claimable_invocations_pass_through_untouched(rig, args, overrides, why) -> None:
    proc, argv = rig.run(*args, env_overrides=overrides)
    assert proc.returncode == 0, why
    assert argv == list(args), f"argv must be untouched when {why}"
    assert rig.claims() == [], f"no claim must be written when {why}"


def test_unwritable_claim_dir_still_runs_the_app(rig, tmp_path: Path) -> None:
    """The whole fail-open contract in one case: attribution cannot cost the
    user their app."""
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a dir")
    proc, argv = rig.run("agent", "-m", "ping",
                         env_overrides={ENV_CLAIM_DIR: str(blocked / "app-runs")})
    assert proc.returncode == 0
    assert argv == ["agent", "-m", "ping"]


def test_no_real_openclaw_exits_127_without_recursing(rig, tmp_path: Path) -> None:
    proc, _ = rig.run("agent", "-m", "ping",
                      env_overrides={"PATH": f"{rig.shim_dir}:/usr/bin:/bin"})
    assert proc.returncode == 127
    assert "no 'openclaw' on PATH" in proc.stderr


def test_unsafe_label_is_dropped_but_the_claim_still_lands(rig) -> None:
    proc, argv = rig.run("agent", "-m", "ping", env_overrides={ENV_LABEL: 'x" bad'})
    assert proc.returncode == 0
    claim = json.loads(rig.claims()[0].read_text())
    assert claim["app_id"] == "morning-briefing"   # attribution survives
    assert claim["label"] == ""                    # the unsafe field does not


# ── app_run_env — the plist-side wiring ──────────────────────────────────────


def test_app_run_env_prepends_shim_and_exports_the_claim_env(tmp_path: Path) -> None:
    out = app_run_env(
        {"PATH": "/opt/homebrew/bin:/usr/bin", "TZ": "UTC"},
        workspace=tmp_path / "ws", shared_dir=tmp_path / "shared",
        bot_id="canary_bot", app_id="morning-briefing",
        label="ai.evolve.canary_bot.morning-briefing",
    )
    shim_dir = shim_dir_for(tmp_path / "ws")
    assert out["PATH"] == f"{shim_dir}:/opt/homebrew/bin:/usr/bin"
    assert out["TZ"] == "UTC"                      # app-supplied env preserved
    assert out[ENV_APP_ID] == "morning-briefing"
    assert out[ENV_CLAIM_DIR] == str(tmp_path / "shared" / "canary_bot" / APP_RUNS_DIR_NAME)
    assert out[ENV_LABEL] == "ai.evolve.canary_bot.morning-briefing"
    # Idempotent: re-wiring an already-wired env doesn't duplicate the entry.
    again = app_run_env(out, workspace=tmp_path / "ws", shared_dir=tmp_path / "shared",
                        bot_id="canary_bot", app_id="morning-briefing")
    assert again["PATH"] == out["PATH"]


def test_app_run_env_claim_dir_is_the_dir_the_consumer_reads(tmp_path: Path) -> None:
    """The producer and the plugin must agree on the path, and the plugin's
    half is a TypeScript constant — read it rather than restating it."""
    src = (_ADMIN_DIR.parent / "plugin" / "src" / "apps" / "scheduledAttribution.ts").read_text()
    m = re.search(r'CLAIM_DIR_NAME = "([^"]+)"', src)
    assert m and m.group(1) == APP_RUNS_DIR_NAME


@pytest.mark.parametrize("app_id", ["", "   ", "has space", "../escape", "a/b", ".dotted"])
def test_app_run_env_refuses_unsafe_app_ids_rather_than_repairing_them(
    tmp_path: Path, app_id: str,
) -> None:
    env = {"PATH": "/usr/bin"}
    out = app_run_env(env, workspace=tmp_path / "ws", shared_dir=tmp_path / "shared",
                      bot_id="canary_bot", app_id=app_id)
    assert out == env, "a mangled app id would name an app that does not exist"
    assert not (tmp_path / "ws").exists(), "no shim written for a refused app id"


def test_app_run_env_returns_env_unchanged_when_the_shim_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_run_shim, "ensure_app_run_shim", lambda ws: "")
    env = {"PATH": "/usr/bin"}
    assert app_run_env(env, workspace=tmp_path / "ws", shared_dir=tmp_path / "s",
                       bot_id="b", app_id="morning-briefing") == env


# ── install_launchd_command_action — the plist actually carries it ───────────


def _patch_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    stub = mock.MagicMock(return_value={"ok": True, "artifact": "/L/x.plist",
                                        "error": "", "loaded": True})
    monkeypatch.setattr(install_helpers, "install_scheduled_jobspec", stub)
    monkeypatch.setattr(install_helpers, "load_network",
                        lambda: {"sharedDir": str(tmp_path / "shared"),
                                 "bots": {"canary_bot": {"user": "canary_bot"}}})
    monkeypatch.setattr(install_helpers, "get_bot_user", lambda bot_id, net: "canary_bot")
    monkeypatch.setattr(install_helpers, "user_home", lambda user: tmp_path / "home")
    (tmp_path / "home" / ".openclaw" / "workspace" / "scripts").mkdir(parents=True)
    (tmp_path / "home" / ".openclaw" / "workspace" / "scripts" / "cron.sh").write_text("#!/bin/bash\n")
    return stub


def _env_of(stub: mock.MagicMock) -> dict:
    return dict(stub.call_args.args[1].env or {})


def test_installed_app_cron_carries_the_shim(monkeypatch, tmp_path: Path) -> None:
    stub = _patch_install(monkeypatch, tmp_path)
    ws = tmp_path / "home" / ".openclaw" / "workspace"

    result = install_launchd_command_action(
        bot_id="canary_bot", action_id="morning-briefing",
        label="ai.evolve.${bot_id}.morning-briefing",
        command="/bin/bash ${workspace}/scripts/cron.sh",
        schedule={"cron": {"Hour": 7, "Minute": 0}},
        env={"TZ": "UTC"},
        app_id="morning-briefing",
    )
    assert result["ok"] is True

    env = _env_of(stub)
    shim_dir = shim_dir_for(ws)
    assert env["PATH"].split(":")[0] == shim_dir, "the shim must win over the real openclaw"
    assert "/opt/homebrew/bin" in env["PATH"], "the 2026-06-22 exit-127 PATH is preserved"
    assert env["TZ"] == "UTC"
    assert env[ENV_APP_ID] == "morning-briefing"
    assert env[ENV_CLAIM_DIR] == str(tmp_path / "shared" / "canary_bot" / APP_RUNS_DIR_NAME)
    assert env[ENV_LABEL] == "ai.evolve.canary_bot.morning-briefing"
    assert (Path(shim_dir) / SHIM_NAME).exists()


def test_install_without_app_id_is_byte_identical_to_before(monkeypatch, tmp_path: Path) -> None:
    """No app id → no wiring at all. The launchd_python_signal mechanism and
    any non-app caller must see the pre-AL-1.2 plist."""
    stub = _patch_install(monkeypatch, tmp_path)
    install_launchd_command_action(
        bot_id="canary_bot", action_id="x", label="ai.evolve.canary_bot.x",
        command="/bin/bash ${workspace}/scripts/cron.sh",
        schedule={"every_minutes": 15}, env={"TZ": "UTC"},
    )
    env = _env_of(stub)
    assert env == {"TZ": "UTC", "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}
    assert not (tmp_path / "home" / ".openclaw" / "workspace" / "evolve").exists()


def test_install_survives_an_unwritable_shim_dir(monkeypatch, tmp_path: Path) -> None:
    stub = _patch_install(monkeypatch, tmp_path)
    monkeypatch.setattr(app_run_shim, "ensure_app_run_shim", lambda ws: "")
    result = install_launchd_command_action(
        bot_id="canary_bot", action_id="x", label="ai.evolve.canary_bot.x",
        command="/bin/bash ${workspace}/scripts/cron.sh",
        schedule={"every_minutes": 15}, app_id="morning-briefing",
    )
    assert result["ok"] is True, "attribution wiring must never block an install"
    assert ENV_APP_ID not in _env_of(stub)


# ── the fleet heal — existing app crons must pick the shim up ────────────────
#
# Without this the producer reaches only apps installed AFTER it shipped, and
# every app cron already on the fleet stays unattributed forever ("shipped ≠
# live"). The heal is the same one that repaired the 2026-06-22 exit-127 PATH,
# widened by one reason.


class _FakeManifest:
    def __init__(self, actions, app_id=""):
        self.scheduled_actions = actions
        self.id = app_id


def _action(plist_path: str) -> dict:
    return {
        "id": "morning-briefing", "mechanism": "launchd",
        "installed_artifact": plist_path,
        "install": {
            "plist_label": "ai.evolve.${bot_id}.morning-briefing",
            "command": "/bin/bash /Users/canary_bot/.openclaw/workspace/scripts/cron.sh",
            "schedule": {"cron": {"Hour": 7, "Minute": 0}},
        },
    }


def _plist_with_path(path_value: str) -> str:
    return ("<plist><dict><key>EnvironmentVariables</key><dict>"
            f"<key>PATH</key><string>{path_value}</string></dict></dict></plist>")


def _patch_repair(monkeypatch, manifests, tmp_path: Path):
    monkeypatch.setattr(install_helpers, "bot_home",
                        lambda b, n=None: tmp_path / "home")
    import evolve_admin.applications.manifest as _m
    monkeypatch.setattr(_m, "list_manifests", lambda sd, bot: manifests)
    calls: list = []
    monkeypatch.setattr(install_helpers, "install_launchd_command_action",
                        lambda *a, **k: (calls.append((a, k)), {"ok": True})[1])
    return calls


def test_repair_heals_an_app_cron_that_has_a_path_but_no_shim(tmp_path, monkeypatch) -> None:
    plist = tmp_path / "p.plist"
    plist.write_text(_plist_with_path("/opt/homebrew/bin:/usr/bin:/bin"))
    calls = _patch_repair(
        monkeypatch, [_FakeManifest([_action(str(plist))], app_id="morning-briefing")], tmp_path)

    rep = install_helpers.repair_app_cron_env_paths(["canary_bot"], network={}, bootstrap=False)

    assert rep["healed"] == ["ai.evolve.canary_bot.morning-briefing"]
    assert rep["reason"]["ai.evolve.canary_bot.morning-briefing"] == \
        "no app-run attribution shim on PATH"
    assert calls[0][1]["app_id"] == "morning-briefing"


def test_repair_skips_an_app_cron_that_already_has_the_shim(tmp_path, monkeypatch) -> None:
    """Idempotent — otherwise every deploy would bootout/bootstrap every app
    cron on the pod."""
    ws = tmp_path / "home" / ".openclaw" / "workspace"
    plist = tmp_path / "p.plist"
    plist.write_text(_plist_with_path(f"{shim_dir_for(ws)}:/opt/homebrew/bin:/usr/bin"))
    calls = _patch_repair(
        monkeypatch, [_FakeManifest([_action(str(plist))], app_id="morning-briefing")], tmp_path)

    rep = install_helpers.repair_app_cron_env_paths(["canary_bot"], network={}, bootstrap=False)

    assert rep["checked"] == 1
    assert rep["missing"] == [] and rep["healed"] == [] and calls == []


def test_repair_leaves_an_app_with_no_id_alone(tmp_path, monkeypatch) -> None:
    """A manifest with no id has nothing to attribute to — no shim, and no
    pointless re-install loop."""
    plist = tmp_path / "p.plist"
    plist.write_text(_plist_with_path("/opt/homebrew/bin:/usr/bin"))
    calls = _patch_repair(monkeypatch, [_FakeManifest([_action(str(plist))])], tmp_path)

    rep = install_helpers.repair_app_cron_env_paths(["canary_bot"], network={}, bootstrap=False)

    assert rep["missing"] == [] and calls == []
