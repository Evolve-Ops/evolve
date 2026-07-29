"""tests/test_w10g_daemon_layer.py — analyzer-side W10-G daemon-layer fixes.

Covers the residuals found on the round-5 live DO Ubuntu VPS fresh install
(diag-w10g-daemon-layer-2026-06-19):

  #3  get_shared_dir() called with no arg → TypeError. The two monitors
      (oc_substrate_monitor, home_artifacts_monitor) must pass the loaded
      config. Tests use the REAL get_shared_dir (which requires the arg) so
      the regression — a no-arg call — fails loudly here, not on the pod.

  #5a per-bot workspace paths must be platform-keyed (/home on Linux, not a
      hardcoded /Users). bot_evolve_dir's account-not-found fallback was the
      writer that landed a root-owned /Users/<bot>/workspace/evolve on Linux.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_profile():
    yield
    set_profile(None)


# ── #3: get_shared_dir must receive the loaded config ────────────────────────

def test_oc_substrate_main_passes_config_to_get_shared_dir(monkeypatch, tmp_path):
    """Regression for the no-arg get_shared_dir() TypeError. Uses the real
    get_shared_dir (arg-required); the old `get_shared_dir()` would raise."""
    import oc_substrate_monitor as osm

    monkeypatch.setattr(osm, "load_config", lambda *a, **k: {"sharedDir": str(tmp_path)})
    captured: dict = {}

    def fake_run(shared_dir, *, dry_run=False):
        captured["shared_dir"] = shared_dir
        return (set(), 0, 0)

    monkeypatch.setattr(osm, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["oc_substrate_monitor", "--dry-run"])

    osm.main()  # must NOT raise TypeError

    assert captured["shared_dir"] == tmp_path


def test_home_artifacts_main_passes_config_to_get_shared_dir(monkeypatch, tmp_path):
    import home_artifacts_monitor as ham

    monkeypatch.setattr(ham, "load_config", lambda *a, **k: {"sharedDir": str(tmp_path)})
    captured: dict = {}

    def fake_run(config, shared_dir, *, bot_filter=None, dry_run=False):
        captured["shared_dir"] = shared_dir
        return (set(), 0, 0)

    monkeypatch.setattr(ham, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["home_artifacts_monitor", "--dry-run"])

    ham.main()  # must NOT raise TypeError

    assert captured["shared_dir"] == tmp_path


def test_get_shared_dir_still_requires_config():
    """Lock the contract the monitors depend on: a no-arg call is an error."""
    from evolve_config import get_shared_dir

    with pytest.raises(TypeError):
        get_shared_dir()  # type: ignore[call-arg]


# ── #5a: bot_evolve_dir is platform-keyed (no /Users on Linux) ───────────────

_NONEXISTENT_BOT = "w10g_no_such_bot_account_xyz"


@pytest.mark.parametrize("modname", ["defer_queue", "manifest_reflex_queue"])
def test_bot_evolve_dir_no_users_on_linux(monkeypatch, modname):
    """Account-not-found fallback must resolve under the Linux home root,
    never a hardcoded /Users (which wrote root-owned dirs on the VPS)."""
    monkeypatch.delenv("EVOLVE_DEFER_HOME_OVERRIDE", raising=False)
    mod = __import__(modname)
    set_profile(LINUX)
    p = str(mod.bot_evolve_dir(_NONEXISTENT_BOT))
    assert "/Users/" not in p, p
    assert p.startswith("/home/"), p
    assert p.endswith(f"/{_NONEXISTENT_BOT}/.openclaw/workspace/evolve"), p


@pytest.mark.parametrize("modname", ["defer_queue", "manifest_reflex_queue"])
def test_bot_evolve_dir_macos_unchanged(monkeypatch, modname):
    """macOS byte-shape preserved: still /Users/<bot>/.openclaw/workspace/evolve."""
    monkeypatch.delenv("EVOLVE_DEFER_HOME_OVERRIDE", raising=False)
    mod = __import__(modname)
    set_profile(MACOS)
    p = str(mod.bot_evolve_dir(_NONEXISTENT_BOT))
    assert p == f"/Users/{_NONEXISTENT_BOT}/.openclaw/workspace/evolve", p


# ── round-6 #1a: cross-user queue files are group-rw (Linux ACL-mask unclamp) ─
#
# The defer-runner / manifest-reflex-runner run as the non-root ``evolve``
# service user and must read + rewrite each bot's queue. A file created at the
# bot's umask (0600) zeroes the Linux POSIX-ACL mask, capping evolve's inherited
# ACE to nothing → EACCES. The write helpers chmod the files 0660 so the mask
# stays wide. (The real bot-side writer is the TS tool, fixed in parallel; this
# pins the Python rewrite/append helpers + the contract.)

def _make_row(mod, modname):
    if modname == "defer_queue":
        return mod.new_row(bot_id="b", fires_at="2999-01-01T00:00:00+00:00",
                           mode="message", message="hi")
    return mod.new_row(bot_id="b", app_id="a", name="t", purpose="s")


@pytest.mark.parametrize("modname", ["defer_queue", "manifest_reflex_queue"])
def test_queue_rewrite_is_group_writable(monkeypatch, tmp_path, modname):
    """rewrite_queue must leave the queue file group-rw (0660) so the cross-user
    ACL mask isn't clamped on Linux."""
    monkeypatch.setenv("EVOLVE_DEFER_HOME_OVERRIDE", str(tmp_path))
    mod = __import__(modname)

    row = _make_row(mod, modname)
    mod.rewrite_queue("b", [row])

    p = mod.queue_path("b")
    assert p.exists()
    mode = p.stat().st_mode & 0o777
    assert mode & 0o060 == 0o060, f"queue not group-rw (mask would clamp): {oct(mode)}"


@pytest.mark.parametrize("modname", ["defer_queue", "manifest_reflex_queue"])
def test_queue_append_is_group_writable(monkeypatch, tmp_path, modname):
    """append_row (Python/test path) must leave the queue file group-rw too."""
    monkeypatch.setenv("EVOLVE_DEFER_HOME_OVERRIDE", str(tmp_path))
    mod = __import__(modname)

    row = _make_row(mod, modname)
    mod.append_row(row)

    p = mod.queue_path("b")
    assert p.exists()
    mode = p.stat().st_mode & 0o777
    assert mode & 0o060 == 0o060, f"queue not group-rw: {oct(mode)}"


# ── round-6 #1a: the real (TS) bot-side writers set the same mode ─────────────

@pytest.mark.parametrize("rel", [
    "src/tools/DeferTool.ts",
    "src/tools/RecordApplicationTool.ts",
    "dist/tools/DeferTool.js",
    "dist/tools/RecordApplicationTool.js",
])
def test_ts_queue_writers_chmod_group_writable(rel):
    """The bot creates the queue via the TS tool (fs.appendFileSync), not the
    Python helper — so the at-source 0660 chmod must live in the plugin too,
    in both the TS source and the committed dist."""
    plugin = _ANALYZER_DIR.parent / "plugin"
    src = (plugin / rel).read_text()
    assert "chmodSync" in src and "0o660" in src, (
        f"{rel} missing the group-rw chmod that unclamps the Linux ACL mask")


# ── round-8: proposal_synthesizer entrypoint must die LOUD, never mute ────────

def _inject_synthesize(monkeypatch, main_fn):
    """Register a fake ``proposal_synthesizer.synthesize`` so the entrypoint's
    ``from proposal_synthesizer.synthesize import main`` resolves to ``main_fn``
    (or raises ImportError when ``main_fn`` is None — the failure case)."""
    parent = sys.modules.get("proposal_synthesizer") or type(sys)("proposal_synthesizer")
    monkeypatch.setitem(sys.modules, "proposal_synthesizer", parent)
    submod = type(sys)("proposal_synthesizer.synthesize")
    if main_fn is not None:
        submod.main = main_fn
    monkeypatch.setitem(sys.modules, "proposal_synthesizer.synthesize", submod)
    parent.synthesize = submod


def test_run_proposal_synthesizer_loud_on_import_failure(monkeypatch, capsys):
    """Round-8: on the round-7 live pod proposal_synthesizer was the lone failed
    daemon with EMPTY stderr+journal — a death before logging was wired. The
    entrypoint now guards the module import and prints a labelled traceback + a
    distinct non-zero exit, so round-8's live run captures the cause instead of
    dying mute (mirrors mcp-bridge #4's loud-hardening)."""
    import importlib
    rps = importlib.import_module("run_proposal_synthesizer")
    _inject_synthesize(monkeypatch, None)  # no main attr → import raises ImportError
    rc = rps.main()
    assert rc == 70, rc
    assert "FATAL during import" in capsys.readouterr().err


def test_run_proposal_synthesizer_passes_argv_and_returns_int(monkeypatch):
    """Happy path: argv after the script name is forwarded to the wrapped main
    and its return code is propagated as an int."""
    import importlib
    rps = importlib.import_module("run_proposal_synthesizer")
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        return 0

    _inject_synthesize(monkeypatch, fake_main)
    monkeypatch.setattr(sys, "argv", ["run_proposal_synthesizer.py", "--shared-dir", "/x"])
    rc = rps.main()
    assert rc == 0
    assert seen["argv"] == ["--shared-dir", "/x"], seen


def test_run_proposal_synthesizer_loud_on_run_failure(monkeypatch, capsys):
    """A raise from inside the wrapped main() is also surfaced loud, not mute."""
    import importlib
    rps = importlib.import_module("run_proposal_synthesizer")

    def boom(argv):
        raise RuntimeError("kaboom")

    _inject_synthesize(monkeypatch, boom)
    monkeypatch.setattr(sys, "argv", ["run_proposal_synthesizer.py"])
    rc = rps.main()
    assert rc == 70
    assert "FATAL during synthesize.main()" in capsys.readouterr().err


# ── round-9 #2: synthesizer resolves the key from the PRIMARY bot, not evolve ──

def _write_primary_auth_profiles(home: Path, key: str) -> None:
    """Plant a canonical auth-profiles.json under a primary bot's home."""
    auth = home / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    import json
    auth.write_text(json.dumps(
        {"profiles": {"anthropic:api": {"type": "api_key", "key": key}}}
    ))


def test_synthesizer_resolves_key_from_primary_bot_on_linux(monkeypatch, tmp_path):
    """Round-9 #2: run as the evolve SERVICE user, the synthesizer's key resolver
    must read the PRIMARY bot's auth-profiles — on a fresh Linux pod the evolve
    account has NO OpenClaw instance, so the key lives only in /home/<primary>.
    The pre-fix code hardcoded read_bot_anthropic_key("evolve", …) → "no key" →
    exit 1 on every fire even though the key was present in the primary's home."""
    set_profile(LINUX)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import primary_bot
    from proposal_synthesizer.synthesizer import resolve_evolve_anthropic_key

    # The primary (evo) home holds the key; the evolve service account does NOT.
    primary_home = tmp_path / "home" / "evo"
    _write_primary_auth_profiles(primary_home, "sk-ant-primary-key")
    evolve_home = tmp_path / "home" / "evolve"
    (evolve_home / ".openclaw").mkdir(parents=True, exist_ok=True)  # exists, but no key

    network = {"primary": "evo", "bots": {"evo": {"user": "evo"}}}
    monkeypatch.setattr(primary_bot, "primary_bot_home", lambda net: primary_home)

    assert resolve_evolve_anthropic_key(network) == "sk-ant-primary-key"


def test_synthesizer_key_resolver_no_key_when_primary_absent(monkeypatch, tmp_path):
    """No key anywhere (no env, primary has no auth-profiles) → empty string, so
    synthesize.main surfaces the clear "no key" error rather than crashing."""
    set_profile(LINUX)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import primary_bot
    from proposal_synthesizer.synthesizer import resolve_evolve_anthropic_key

    primary_home = tmp_path / "home" / "evo"
    (primary_home / ".openclaw").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(primary_bot, "primary_bot_home", lambda net: primary_home)

    assert resolve_evolve_anthropic_key({"primary": "evo"}) == ""
