"""ALPHA-1 (B1) — discovery runs on arrival and weekly, not only on a click.

``applications.sync.sync_bot`` used to have exactly two callers, both HTTP
routes behind the *Sync all bots* button. A stranger's pod's Discovered queue
was therefore empty forever. ``evolve_admin.app_discovery`` adds the two
producers that make it fill on its own, and these tests pin the properties
that decide whether it actually works and stays cheap:

  * the arrival scan runs **once per bot, ever** — a re-deploy must not
    re-scan (on the degraded pod of finding B2 the uncovered count never
    returns to zero, so an ungated hook would pay a full LLM scan on every
    ``deploy_bot`` — and the repo-puller redeploys);
  * a bot the scanner already stamped is never given an arrival scan, so
    shipping this to a live pod does not re-scan its whole fleet;
  * a scan failure **warns and the deploy stands** (observation-layer
    posture), and a bot that keeps failing stops being retried;
  * a release-canary deploy (``pod_side_effects=False``) skips entirely;
  * the weekly sweep visits the same bots the button does, survives one bad
    bot, and materializes through the Scheduler seam on BOTH platforms;
  * the sweep's label is in ``deploy.expected_plist_labels`` — without that
    the orphan-sweeper deletes the job on the next deploy.

Scope note on the two call sites inside ``deploy.py``: ``deploy_bot`` and
``install_evolve_infra_jobs`` cannot be executed in a unit test (both need a
provisioned pod, sudo, and real bot accounts), so those two are checked
STRUCTURALLY, by AST, against the enclosing function's body — which proves
the call exists where it is claimed to and under the guard it is claimed to
have, and does not prove it ran on a real pod.
"""

from __future__ import annotations

import ast
import json
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN = Path(__file__).resolve().parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for _p in (_ADMIN, _ANALYZER):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evolve_admin import app_discovery  # noqa: E402
from evolve_admin.app_discovery import (  # noqa: E402
    APP_DISCOVERY_SWEEP_LABEL,
    MAX_FIRST_SCAN_ATTEMPTS,
    STATE_FILENAME,
    first_scan_needed,
    first_scan_on_deploy,
    install_app_discovery_sweep,
    read_state,
    run_sweep,
    sweep_bot_ids,
)

_SYNC_TARGET = "evolve_admin.applications.sync.sync_bot"


def _Result():
    """A REAL ``DeployResult``, not a double.

    A hand-rolled stand-in whose ``success`` is only ever set in ``__init__``
    makes ``assert result.success is True`` unfalsifiable — the assertion this
    file makes most often, and the whole point of the non-fatal contract. The
    real object flips ``success`` in ``error()``, so the assertion can fail.
    """
    from evolve_admin.deploy import DeployResult
    return DeployResult(bot_id="team_bot_a", success=True)


def _lines(res) -> list[str]:
    """DeployResult records log lines as ``steps``; this is the read side."""
    return [str(s) for s in res.steps]


def _pod(tmp_path: Path, bots=("team_bot_a",)) -> Path:
    """Write a minimal network.json whose sharedDir is under tmp_path."""
    shared = tmp_path / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    net = shared / "network.json"
    net.write_text(json.dumps({
        "sharedDir": str(shared),
        "bots": {b: {"role": "member", "port": 18790 + i} for i, b in enumerate(bots)},
        "members": list(bots),
    }))
    return net


def _shared(net_path: Path) -> Path:
    return net_path.parent


@pytest.fixture(autouse=True)
def _fresh_scan_budget():
    """Reset the burst-scoped scan budget between tests.

    ``_budget_spent`` / ``_budget_last_charge`` are module globals. Every test
    that runs an arrival scan charges them (near-zero, since sync_bot is
    mocked), so without this the budget test would depend on collection order.
    """
    app_discovery._budget_spent = 0.0
    app_discovery._budget_last_charge = 0.0
    yield
    app_discovery._budget_spent = 0.0
    app_discovery._budget_last_charge = 0.0


@pytest.fixture(autouse=True)
def _unscanned_bots(tmp_path):
    """Default: no bot carries a scanner ``.scan-status.json``.

    ``_has_scanner_status`` reads the real ``/Users/<bot>/.openclaw`` tree;
    point ``bot_home`` at an empty tmp dir so the tests describe a pod Evolve
    has never scanned unless a test says otherwise.
    """
    homes = tmp_path / "homes"
    with patch.object(app_discovery, "bot_home", lambda b, network=None: homes / b):
        yield homes


def _mark_scanned(homes: Path, bot_id: str) -> None:
    d = homes / bot_id / ".openclaw" / "workspace" / "manifests"
    d.mkdir(parents=True, exist_ok=True)
    (d / ".scan-status.json").write_text('{"status": "done"}')


def _ok_sync(**over):
    base = {"bot_id": "team_bot_a", "path": "escalated", "discovered_count": 2,
            "reason": "Found 9 uncovered code file(s)/dir(s) → ran full scan; discovered 2 app(s)"}
    base.update(over)
    return base


# ── 1. the arrival scan runs, once ────────────────────────────────────────────


def test_arrival_scan_runs_on_first_deploy(tmp_path):
    net = _pod(tmp_path)
    res = _Result()
    with patch(_SYNC_TARGET, return_value=_ok_sync()) as sync:
        ran = first_scan_on_deploy("team_bot_a", net, res)

    assert ran is True
    assert sync.call_count == 1, "the arrival scan did not call sync_bot"
    # It scanned the right bot against the pod's own sharedDir.
    assert sync.call_args.args[0] == "team_bot_a"
    assert sync.call_args.args[1] == _shared(net)
    state = read_state(_shared(net), "team_bot_a")
    assert state["first_scan"]["ok"] is True
    assert state["first_scan"]["discovered_count"] == 2
    assert any("first scan of team_bot_a done" in ln for ln in _lines(res))


def test_arrival_scan_does_not_rerun_on_redeploy(tmp_path):
    """The idempotency that keeps a degraded pod from paying a full LLM scan
    on every deploy — sync_bot is called exactly once across two deploys."""
    net = _pod(tmp_path)
    with patch(_SYNC_TARGET, return_value=_ok_sync()) as sync:
        first_scan_on_deploy("team_bot_a", net, _Result())
        second = first_scan_on_deploy("team_bot_a", net, _Result())

    assert second is False
    assert sync.call_count == 1, (
        f"the arrival scan ran again on re-deploy ({sync.call_count} calls) — "
        "every repo-puller redeploy would pay a scan"
    )


def test_bot_already_scanned_by_the_scanner_gets_no_arrival_scan(tmp_path, _unscanned_bots):
    """An existing pod's bots carry ``.scan-status.json``; the deploy that
    introduces this module must not re-scan the whole fleet."""
    net = _pod(tmp_path)
    _mark_scanned(_unscanned_bots, "team_bot_a")
    needed, why = first_scan_needed(_shared(net), "team_bot_a")
    assert needed is False and "already scanned" in why

    with patch(_SYNC_TARGET, return_value=_ok_sync()) as sync:
        assert first_scan_on_deploy("team_bot_a", net, _Result()) is False
    assert sync.call_count == 0


def test_unreadable_workspace_still_gets_an_arrival_scan(tmp_path, _unscanned_bots):
    """The suppression gate fails toward SCANNING, not toward silence.

    On Linux/Py3.12 a bare stat under a clamped ``.openclaw`` raises
    PermissionError (#3184). The `.openclaw` convention normally answers such a
    probe with ``exists_or_unreachable`` ("unreachable ⇒ present"), which here
    would mean "already scanned" — and would restore the B1 defect on exactly
    one platform, invisibly. This pins the inversion.
    """
    net = _pod(tmp_path)

    class _Clamped(type(Path(tmp_path))):
        def is_file(self):
            raise PermissionError(13, "Permission denied")

    with patch.object(app_discovery, "bot_home", lambda b, network=None: _Clamped(tmp_path)):
        assert app_discovery._has_scanner_status("team_bot_a") is False
        assert first_scan_needed(_shared(net), "team_bot_a")[0] is True

    with patch.object(app_discovery, "bot_home", side_effect=RuntimeError("corrupt network.json")):
        assert app_discovery._has_scanner_status("team_bot_a") is False


def test_state_file_is_0644_so_both_writers_can_read_it(tmp_path):
    """deploy runs as root (CLI) or as ``evolve`` (admin UI). mkstemp creates
    0600 and os.replace carries that onto the dest — the pin is what keeps the
    record readable to the other writer."""
    net = _pod(tmp_path)
    with patch(_SYNC_TARGET, return_value=_ok_sync()):
        first_scan_on_deploy("team_bot_a", net, _Result())
    p = _shared(net) / "team_bot_a" / STATE_FILENAME
    assert stat.S_IMODE(p.stat().st_mode) == 0o644, oct(p.stat().st_mode)


# ── 2. failure is non-fatal, and bounded ──────────────────────────────────────


def test_scan_failure_warns_and_never_fails_the_deploy(tmp_path):
    net = _pod(tmp_path)
    res = _Result()
    with patch(_SYNC_TARGET, side_effect=RuntimeError("workspace unreadable")):
        ran = first_scan_on_deploy("team_bot_a", net, res)

    assert ran is False
    assert res.success is True, "a discovery failure flipped the deploy to failed"
    assert any(ln.startswith("[warn]") and "first scan" in ln for ln in _lines(res))
    assert read_state(_shared(net), "team_bot_a")["first_scan"] == {
        **read_state(_shared(net), "team_bot_a")["first_scan"],
        "ok": False, "attempts": 1,
    }


def test_repeated_failures_stop_being_retried(tmp_path):
    """A permanently-broken workspace must not re-attempt on every deploy —
    an attempt that fails LATE (after the scan ran) is not cheap."""
    net = _pod(tmp_path)
    with patch(_SYNC_TARGET, side_effect=RuntimeError("boom")) as sync:
        for _ in range(MAX_FIRST_SCAN_ATTEMPTS + 3):
            first_scan_on_deploy("team_bot_a", net, _Result())

    assert sync.call_count == MAX_FIRST_SCAN_ATTEMPTS, (
        f"retried {sync.call_count}x against a cap of {MAX_FIRST_SCAN_ATTEMPTS}"
    )
    needed, why = first_scan_needed(_shared(net), "team_bot_a")
    assert needed is False and "not retrying" in why


def test_unrecordable_state_warns_into_the_deploy_log(tmp_path, _unscanned_bots):
    """An unwritable ``{shared}/{bot}/`` is the one way the once-ever guarantee
    and the retry cap both stop bounding anything — so it has to be VISIBLE.

    Real trigger: a root ``sudo evolve-admin deploy`` creates the dir, then an
    admin-UI deploy runs as ``evolve`` and cannot mkstemp in it. The hook must
    (a) still run the scan, (b) leave the deploy standing, (c) say so in the
    deploy log an operator reads — not only in a logger nobody tails.
    """
    net = _pod(tmp_path)
    res = _Result()
    with patch(_SYNC_TARGET, return_value=_ok_sync()) as sync, \
         patch.object(app_discovery, "_write_state", return_value=False):
        ran = first_scan_on_deploy("team_bot_a", net, res)

    assert ran is True and sync.call_count == 1
    assert res.success is True, "an unrecordable scan flipped the deploy to failed"
    assert any(
        ln.startswith("[warn]") and "could not record the first scan" in ln
        for ln in _lines(res)
    ), _lines(res)


def test_real_write_state_returns_false_instead_of_raising(tmp_path):
    """``_write_state``'s contract is best-effort: it reports failure by
    RETURN VALUE and never raises — which is why the hook can rely on the
    boolean rather than on a try/except that would never fire."""
    unwritable = tmp_path / "ro"
    unwritable.mkdir()
    unwritable.chmod(0o500)
    try:
        assert app_discovery._write_state(unwritable, "team_bot_a", {"x": 1}) is False
    finally:
        unwritable.chmod(0o700)


def test_deploy_all_stops_scanning_once_the_time_budget_is_spent(tmp_path, monkeypatch):
    """``deploy --all`` on a freshly-adopted 8-bot pod must not serialize eight
    escalated scans into one foreground deploy. Deferred bots write NO record,
    so the next deploy (or the weekly sweep) still picks them up."""
    bots = tuple(f"bot_{i}" for i in range(8))
    net = _pod(tmp_path, bots=bots)
    monkeypatch.setattr(app_discovery, "FIRST_SCAN_BUDGET_SECONDS", 100.0)

    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(app_discovery.time, "time", lambda: clock["t"])

    def _slow_scan(bot_id, *a, **k):
        clock["t"] += 40.0          # each escalated scan costs 40s
        return _ok_sync(bot_id=bot_id)

    res = _Result()
    with patch(_SYNC_TARGET, side_effect=_slow_scan) as sync:
        ran = [first_scan_on_deploy(b, net, res) for b in bots]

    assert sync.call_count == 3, (
        f"budget 100s / 40s per scan should admit 3 bots, admitted {sync.call_count}"
    )
    assert ran[:3] == [True, True, True] and not any(ran[3:])
    # Deferred bots are unrecorded — the next deploy retries them.
    for b in bots[3:]:
        assert read_state(_shared(net), b) == {}
        assert first_scan_needed(_shared(net), b)[0] is True
    assert any("deferred" in ln for ln in _lines(res))


def test_budget_is_per_burst_not_per_process(tmp_path, monkeypatch):
    """The long-lived admin server must not stop scanning forever.

    ``ai.evolve.evolve.admin-ui`` is a daemon whose ``POST /api/deploy`` route
    calls ``deploy_bot`` in-process. A process-scoped budget would spend itself
    once and defer every arrival scan for the rest of that daemon's uptime —
    days or weeks — while the deploy log kept promising the scan would happen
    on a later deploy. An idle gap starts a new burst.
    """
    bots = tuple(f"bot_{i}" for i in range(4))
    net = _pod(tmp_path, bots=bots)
    monkeypatch.setattr(app_discovery, "FIRST_SCAN_BUDGET_SECONDS", 60.0)
    monkeypatch.setattr(app_discovery, "FIRST_SCAN_BUDGET_IDLE_RESET_SECONDS", 600.0)

    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(app_discovery.time, "time", lambda: clock["t"])

    def _slow_scan(bot_id, *a, **k):
        clock["t"] += 70.0
        return _ok_sync(bot_id=bot_id)

    with patch(_SYNC_TARGET, side_effect=_slow_scan) as sync:
        assert first_scan_on_deploy("bot_0", net, _Result()) is True     # spends 70s
        assert first_scan_on_deploy("bot_1", net, _Result()) is False    # over budget
        clock["t"] += 601.0                                              # operator goes away
        assert first_scan_on_deploy("bot_1", net, _Result()) is True     # new burst
    assert sync.call_count == 2, (
        "the budget did not reset after an idle gap — a long-lived admin "
        "server would stop arrival-scanning permanently"
    )


def test_canary_deploy_skips_the_arrival_scan(tmp_path):
    """A candidate build must not spend a full scan, or mint drafts an
    operator will see, before it has been promoted."""
    net = _pod(tmp_path)
    with patch(_SYNC_TARGET, return_value=_ok_sync()) as sync:
        assert first_scan_on_deploy(
            "team_bot_a", net, _Result(), pod_side_effects=False,
        ) is False
    assert sync.call_count == 0
    assert not (_shared(net) / "team_bot_a" / STATE_FILENAME).exists()


# ── 3. the weekly sweep ───────────────────────────────────────────────────────


def test_sweep_visits_every_bot_in_the_pod(tmp_path):
    """The sweep's enumeration mirrors ``POST /api/applications/sync/pod``'s
    (``network["bots"]`` keys) — with one deliberate divergence, pinned by
    ``test_sweep_skips_planned_bots`` below. This asserts the enumeration, not
    the route; nothing here calls HTTP."""
    net = _pod(tmp_path, bots=("team_bot_a", "personal_bot", "admin_bot"))
    with patch(_SYNC_TARGET, side_effect=lambda b, *a, **k: _ok_sync(bot_id=b)) as sync:
        roll = run_sweep(net, log=lambda _m: None)

    assert sync.call_count == 3
    assert roll["bots"] == 3 and roll["escalated"] == 3 and roll["discovered"] == 6
    assert roll["errors"] == 0


def test_sweep_skips_planned_bots(tmp_path):
    """A planned bot (``{"purpose": …}`` only) has no account or workspace —
    scanning it can only raise.

    This is the ONE place the sweep deliberately diverges from *Sync all bots*:
    ``api_applications_sync_pod`` does not filter planned blocks and collects
    the resulting per-bot error into its response, which is the right shape for
    a button an operator just pressed and the wrong one for a weekly unattended
    job whose only reader is a log."""
    net = _pod(tmp_path, bots=("team_bot_a",))
    data = json.loads(net.read_text())
    data["bots"]["not_yet"] = {"purpose": "someday"}
    net.write_text(json.dumps(data))

    assert sweep_bot_ids(data) == ["team_bot_a"]
    with patch(_SYNC_TARGET, side_effect=lambda b, *a, **k: _ok_sync(bot_id=b)) as sync:
        run_sweep(net, log=lambda _m: None)
    assert [c.args[0] for c in sync.call_args_list] == ["team_bot_a"]


def test_one_bad_bot_does_not_end_the_sweep(tmp_path):
    net = _pod(tmp_path, bots=("a_bot", "b_bot", "c_bot"))

    def _flaky(bot_id, *a, **k):
        if bot_id == "b_bot":
            raise RuntimeError("EACCES on workspace")
        return _ok_sync(bot_id=bot_id)

    with patch(_SYNC_TARGET, side_effect=_flaky) as sync:
        roll = run_sweep(net, log=lambda _m: None)

    assert sync.call_count == 3, "the sweep stopped at the failing bot"
    assert roll["errors"] == 1 and roll["discovered"] == 4


def test_sweep_always_prints_a_summary_line(tmp_path):
    """A sweep that visited zero bots is a finding, not silence — the summary
    is what monitor-style log watching (and an operator) reads."""
    net = _pod(tmp_path, bots=())
    lines: list[str] = []
    with patch(_SYNC_TARGET) as sync:
        run_sweep(net, log=lines.append)
    assert sync.call_count == 0
    assert any("sweep done — bots=0" in ln for ln in lines)


def test_sweep_entrypoint_reports_failure_in_its_exit_code(tmp_path, capsys):
    """The unit's exit status has to mean something: 0 for a completed sweep
    (including one that legitimately visited no bots — ``load_network``
    degrades a missing network to an empty pod rather than raising, so the
    printed ``bots=0`` is the only tell), non-zero when the sweep itself
    could not run."""
    assert app_discovery.main(["--network", str(_pod(tmp_path, bots=()))]) == 0
    assert "bots=0" in capsys.readouterr().out

    with patch.object(app_discovery, "run_sweep", side_effect=RuntimeError("no shared dir")):
        assert app_discovery.main(["--network", str(tmp_path / "nope.json")]) == 1
    assert "sweep aborted" in capsys.readouterr().err


# ── 4. the job installs through the seam, on both platforms ───────────────────


class _Runner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append([str(a) for a in argv])
        return (0, "", "")


@pytest.fixture
def _seam_reset():
    from evolve_admin.runtime import set_scheduler
    yield
    set_scheduler(None)


def test_sweep_installs_as_a_launchd_plist_on_macos(tmp_path, _seam_reset):
    from evolve_admin import deploy
    from evolve_admin.runtime import LaunchdScheduler, set_scheduler

    set_scheduler(LaunchdScheduler(plist_dir=tmp_path, use_sudo=False, runner=_Runner()))
    install_app_discovery_sweep(deploy.DeployResult(bot_id="evolve", success=True))

    plist = tmp_path / f"{APP_DISCOVERY_SWEEP_LABEL}.plist"
    assert plist.exists(), [p.name for p in tmp_path.iterdir()]
    import plistlib
    spec = plistlib.loads(plist.read_bytes())
    assert spec["StartCalendarInterval"] == {"Weekday": 0, "Hour": 5, "Minute": 15}
    assert spec["UserName"] == "evolve"
    assert not spec.get("RunAtLoad"), "a weekly sweep must not fire on every deploy"
    # The program is the module entry point, jitter-wrapped.
    argv = " ".join(spec["ProgramArguments"])
    assert "-m evolve_admin.app_discovery" in argv


def test_sweep_installs_as_a_systemd_unit_on_linux(tmp_path, _seam_reset):
    from platform_profile import LINUX, set_profile

    from evolve_admin import deploy
    from evolve_admin.runtime import SystemdScheduler, set_scheduler

    set_profile(LINUX)
    set_scheduler(SystemdScheduler(unit_dir=tmp_path, use_sudo=False, runner=_Runner()))
    try:
        install_app_discovery_sweep(deploy.DeployResult(bot_id="evolve", success=True))
    finally:
        set_profile(None)

    units = {p.name: p.read_text() for p in tmp_path.iterdir()}
    service = f"{APP_DISCOVERY_SWEEP_LABEL}.service"
    timer = f"{APP_DISCOVERY_SWEEP_LABEL}.timer"
    assert service in units and timer in units, (
        f"a calendar job must render a service AND a timer: {sorted(units)}"
    )
    # Same facts the macOS twin pins — a bare pair of files says nothing.
    assert "User=evolve" in units[service], units[service]
    assert "-m evolve_admin.app_discovery" in units[service], units[service]
    assert "OnCalendar=Sun *-*-* 05:15:00" in units[timer], units[timer]
    assert "RandomizedDelaySec=300s" in units[timer], units[timer]


def test_sweep_label_is_expected_so_the_orphan_sweeper_spares_it(tmp_path):
    """Any label absent from ``expected_plist_labels`` is deleted as an orphan
    on the next deploy — the job would install and then be swept away."""
    from evolve_admin import deploy

    labels = deploy.expected_plist_labels({
        "bots": {"team_bot_a": {"port": 18789}},
        "members": ["team_bot_a"],
        "sharedDir": str(tmp_path),
    })
    assert APP_DISCOVERY_SWEEP_LABEL in labels


# ── 5. the two deploy.py call sites (structural — see the module docstring) ───


def _fn(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in deploy.py")


def _calls(node: ast.AST) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            out.add(n.func.id)
    return out


@pytest.fixture(scope="module")
def _deploy_ast() -> ast.Module:
    from evolve_admin import deploy
    return ast.parse(Path(deploy.__file__).read_text())


def test_deploy_bot_runs_the_arrival_scan_guarded(_deploy_ast):
    """The hook is inside ``deploy_bot`` and inside a ``not dry_run and
    result.success`` guard — a dry run and a failed deploy must not scan."""
    fn = _fn(_deploy_ast, "deploy_bot")
    guarded = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.If) and "first_scan_on_deploy" in _calls(n)
    ]
    assert guarded, "deploy_bot does not call first_scan_on_deploy"
    # Compare the WHOLE unparsed guard, not substrings: `if dry_run and
    # result.success:` — the exact inversion this test exists to catch —
    # contains both "dry_run" and "result.success".
    assert ast.unparse(guarded[0].test) == "not dry_run and result.success", (
        ast.unparse(guarded[0].test)
    )


def test_infra_jobs_installs_the_sweep(_deploy_ast):
    fn = _fn(_deploy_ast, "install_evolve_infra_jobs")
    assert "install_app_discovery_sweep" in _calls(fn), (
        "install_evolve_infra_jobs never installs the weekly sweep — the job "
        "would only ever exist on a pod whose operator installed it by hand"
    )
