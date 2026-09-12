"""tests/conftest.py — Worktree path setup.

The ``evolve_admin`` package is installed editably (pointing at the
*main* repo's ``packages/admin``), which means a plain ``import
evolve_admin`` resolves through a meta-path finder to the main repo
rather than to this worktree. That's fine for everyday use, but it
breaks tests that exercise admin-side code added in this worktree:
edits never get tested.

This file pre-binds ``evolve_admin`` to the worktree's copy so
subsequent imports inside tests pick up the in-progress changes. It
runs once at pytest session start. Mirrors the equivalent file in
``packages/analyzer/tests/conftest.py``.

Safe to leave in place after the worktree merges back to main — when
the editable install and worktree paths agree, this is a no-op rebind
that returns the same module.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Tests must never touch a developer's (or CI runner's) real login
# keychain — force every keystore write/read onto the Fernet file vault,
# which the tests point at tmp_path-scoped shared dirs. The keystore
# honors this env var in _keychain_available() (roadmap 2.8).
os.environ.setdefault("EVOLVE_KEYSTORE_NO_KEYCHAIN", "1")

# Admin auth is ON BY DEFAULT (roadmap 2.6) — a create_app() pod enforces the
# device-cookie gate unless opted out. The hundreds of web tests build
# unpaired test apps and expect open access, so disable enforcement suite-wide
# via the env escape. Tests that exercise the auth gate itself
# (test_admin_auth*, test_csrf) clear this in a local fixture.
os.environ.setdefault("EVOLVE_ADMIN_AUTH_DISABLED", "1")

_TESTS_DIR = Path(__file__).resolve().parent
_ADMIN_DIR = _TESTS_DIR.parent
_WORKTREE_ADMIN = _ADMIN_DIR / "evolve_admin"
_WORKTREE_ANALYZER = _ADMIN_DIR.parent / "analyzer"

# Same shadow problem, analyzer flavour: evolve-analyzer is editable-installed
# (compat .pth) pointing at the MAIN checkout, so admin code paths that do
# plain `import audit` / `from signals import ...` would load main-repo
# analyzer code instead of this worktree's. Prepending the worktree's
# analyzer dir wins over the site-packages .pth entry.
if _WORKTREE_ANALYZER.is_dir() and str(_WORKTREE_ANALYZER) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_ANALYZER))

# Pin the MACOS profile at conftest-LOAD (collection time), not just via the
# session/per-test fixtures below. Some modules resolve a platform-keyed
# constant at IMPORT (e.g. deploy.VENV_PYTHON/PLUGIN_INSTALL_DIR =
# get_profile().…, mirroring evolve_config.CANONICAL_SHARED_DIR) and ~30 admin
# test modules import deploy at top level — i.e. during collection, before any
# fixture runs. On the Linux CI runner those constants would otherwise cache
# the LINUX values and break the suite's macOS-shape assertions (the same
# ordering problem the session-scoped pin below was added for, one phase
# earlier). conftest.py is imported before the test modules it guards, so this
# pin is active when they import. A real pod has no conftest, so production
# resolution is unaffected. (Linux deploy-port W7.)
from platform_profile import MACOS as _MACOS  # noqa: E402
from platform_profile import set_profile as _set_profile  # noqa: E402

_set_profile(_MACOS)


def _prebind_evolve_admin() -> None:
    """Force-bind evolve_admin to the worktree, ahead of the editable install."""
    if not _WORKTREE_ADMIN.is_dir():
        return

    existing = sys.modules.get("evolve_admin")
    if existing is not None:
        try:
            existing_path = Path(existing.__file__ or "").resolve().parent
        except Exception:
            existing_path = None
        if existing_path != _WORKTREE_ADMIN:
            for mod_name in [
                m for m in list(sys.modules)
                if m == "evolve_admin" or m.startswith("evolve_admin.")
            ]:
                del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(
        "evolve_admin",
        _WORKTREE_ADMIN / "__init__.py",
        submodule_search_locations=[str(_WORKTREE_ADMIN)],
    )
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules["evolve_admin"] = module
    spec.loader.exec_module(module)


_prebind_evolve_admin()


# ── Module-state isolation ──────────────────────────────────────────────────
#
# Some tests (e.g. test_install_opik_companion_bootstrap.py) deliberately
# delete every ``evolve_admin.*`` entry from ``sys.modules`` and reimport so
# they can monkeypatch ``__globals__`` of a worktree-loaded function. That
# mutation leaks: other test modules already bound names like
# ``get_bot_skills`` to the *original* module objects at collection time, and
# ``unittest.mock.patch("evolve_admin.skills.inventory._pwd")`` then patches
# the *new* object in ``sys.modules`` — the patch silently no-ops because
# the test's imported callable still resolves through the original module.
# Symptom: ~54 unrelated tests fail with mismatched-state errors.
#
# Same family of bug: tests that do ``module.subprocess.run = fake`` are
# really mutating the global ``subprocess`` module's ``run`` attribute
# (modules are singletons), so the next test that calls ``subprocess.run``
# gets the fake.
#
# Fix: snapshot both before each test and restore after.

@pytest.fixture(autouse=True)
def _restore_module_state():
    """Restore evolve_admin sys.modules entries and subprocess.run after each test."""
    module_snapshot: dict[str, object] = {
        name: mod
        for name, mod in sys.modules.items()
        if name == "evolve_admin" or name.startswith("evolve_admin.")
    }
    subprocess_run_snapshot = subprocess.run
    sys_path_snapshot = list(sys.path)

    yield

    current = {
        name for name in sys.modules
        if name == "evolve_admin" or name.startswith("evolve_admin.")
    }
    for name in current - module_snapshot.keys():
        del sys.modules[name]
        # Also drop the parent's attribute pointing at this submodule.
        # Python's ``from package import submodule`` short-circuits in
        # ``_handle_fromlist`` when the parent already has the attribute —
        # if we leave a zombie attribute behind, the next test's
        # ``from evolve_admin.evo.wizard import engine`` returns the OLD
        # engine module (and its captured submodule references) instead of
        # re-importing fresh. That's the root cause of the wizard/engine
        # isolation failures: ``engine._extractor`` keeps pointing at a
        # long-dead extractor instance even though sys.modules has a fresh
        # one, so ``set_extractor(stub)`` on the fresh module never reaches
        # the engine's bound reference.
        parent_name, _, attr = name.rpartition(".")
        parent_mod = sys.modules.get(parent_name) or module_snapshot.get(parent_name)
        if parent_mod is not None:
            try:
                delattr(parent_mod, attr)
            except AttributeError:
                pass
    for name, mod in module_snapshot.items():
        if sys.modules.get(name) is not mod:
            sys.modules[name] = mod
            # Re-bind the submodule attribute on its parent so it matches
            # the restored sys.modules entry. Counterpart to the cleanup
            # above: if the test imported new submodules (which got pulled
            # out by the loop above), their parent module may have stale
            # attributes pointing at the new instances; here we either
            # restore the original or leave it unset, whichever matches
            # the snapshot.
            parent_name, _, attr = name.rpartition(".")
            parent_mod = sys.modules.get(parent_name)
            if parent_mod is not None:
                try:
                    setattr(parent_mod, attr, mod)
                except (AttributeError, TypeError):
                    pass

    if subprocess.run is not subprocess_run_snapshot:
        subprocess.run = subprocess_run_snapshot

    if sys.path != sys_path_snapshot:
        sys.path[:] = sys_path_snapshot


@pytest.fixture(autouse=True, scope="session")
def _pin_macos_platform_profile_session():
    """Session-wide macOS profile pin — covers MODULE-scoped fixtures.

    pytest instantiates broader-scoped fixtures before function-scoped
    ones, so a module-scoped fixture that renders platform-keyed content
    (e.g. test_sudoers_delivery_heal's ``sudoers_content``) runs BEFORE
    the per-test pin below. Without this session pin, those renders
    resolve the LINUX profile on CI runners and cache the wrong platform
    for the whole module.
    """
    from platform_profile import MACOS, set_profile

    set_profile(MACOS)
    yield
    set_profile(None)


@pytest.fixture(autouse=True)
def _pin_macos_platform_profile():
    """Per-test macOS re-assert + cleanup of in-test LINUX overrides.

    The admin suite's path-shape assertions were authored against macOS
    (/Users, /Library/LaunchDaemons) but CI executes on Linux runners —
    without the pin, profile-aware code correctly resolves the Linux
    profile there and the suite asserts the wrong platform's behavior.
    Linux-behavior tests opt out by calling set_profile(LINUX) (or
    passing platform=) inside the test body; teardown restores MACOS —
    never None — so later module-fixture windows stay pinned too.
    """
    from platform_profile import MACOS, set_profile

    set_profile(MACOS)
    yield
    set_profile(MACOS)


# ── Real-shared-dir write guard ─────────────────────────────────────────────
#
# A test must never write under the pod's canonical shared dir. On a
# maintainer's Mac that is the REAL /Users/Shared/evolve, so a test that
# resolves a ``{shared_dir}``-rooted path from a fixture network.json which
# omits ``sharedDir`` silently mutates local pod state — and then reads it
# back on the next run.
#
# This class is nastily invisible in two directions at once. It is green on
# Linux CI (no /Users/Shared, so the write raises, a broad ``except OSError``
# swallows it, and the read sees nothing), and on macOS the failure is
# DELAYED: the polluting run passes, and a later run fails with an error
# about the polluted subsystem that says nothing about test isolation.
#
# It has already recurred. PR #3984 (2026-09-03) fixed exactly this in
# test_gmail_send_attachments.py, per file; test_mcp_bridge_hot_reload.py
# then reproduced it, because a per-file fix does not retire a class. Hence
# a guard rather than a third one-off.
#
# Mechanism: an audit hook BLOCKS every write under either canonical root
# (both platforms' roots, so the guard does not depend on which profile is
# active) and records it. Blocking alone would be a silent behaviour change,
# so an autouse fixture additionally FAILS any test that attempted one --
# unless its file is in real-shared-dir-baseline.txt. Recording and
# asserting separately is deliberate: much of the offending code swallows
# OSError, so an exception raised from the hook cannot be relied on to
# surface: the assertion is what makes it visible.
#
# The baseline is a ratchet, not an amnesty: it is the set of files that
# already did this when the guard landed. Never add to it to make a new
# test pass -- give the test a tmp_path-rooted ``sharedDir``. Removing a
# file from it as you fix it is the burn-down.
#
# KNOWN GAP -- subprocess writes are invisible to an audit hook, and at least
# one writer turns a blocked write into a real one. ``config.save_network``
# falls back to ``sudo /bin/cp`` precisely when the in-process copy raises
# PermissionError, which is what this guard raises; CI runners have
# passwordless sudo, so the refusal is converted into a write that lands and
# that nothing here records. That is the most likely way a Linux runner comes
# to hold a real {shared_dir}/network.json, which every later ``load_config()``
# in the same shard then migrates and writes back to -- so ONE escaped write
# reddens whichever unrelated tests happen to be packed after it. Closing it
# means auditing ``subprocess.Popen`` args for guarded roots; not done yet
# because the blast radius is the whole suite.
#
# Escape hatch for debugging: EVOLVE_TESTS_ALLOW_REAL_SHARED_DIR=1.

_GUARD_OFF = os.environ.get("EVOLVE_TESTS_ALLOW_REAL_SHARED_DIR") == "1"
_BASELINE_FILE = _TESTS_DIR / "real-shared-dir-baseline.txt"


def _guarded_roots() -> tuple[str, ...]:
    """Both platforms' canonical shared dirs, so the guard is profile-agnostic."""
    from platform_profile import LINUX as _LINUX
    from platform_profile import MACOS as _MACOS
    return tuple(
        os.path.realpath(p.shared_dir_default) for p in (_MACOS, _LINUX)
    )


_ROOTS = _guarded_roots()
_BASELINED: set[str] = set()
if _BASELINE_FILE.exists():
    _BASELINED = {
        line.strip() for line in _BASELINE_FILE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }

_current_test: dict[str, str] = {"nodeid": "", "file": ""}
_shared_dir_writes: dict[str, set[str]] = {}
#: path -> "thread | frame > frame > frame" for every blocked write, so the failure
#: names the WRITER, not just the victim. The hook attributes a write to whichever
#: test is running when it happens; a thread leaked by an earlier test (an autosave,
#: a watcher) therefore fails an innocent later test with a message that says
#: nothing about who wrote. 2026-09-12: seven tests in one shard were blamed for
#: fourteen writes of network.json.tmp none of them made.
_shared_dir_writers: dict[str, set[str]] = {}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


def _guarded_path(path: object) -> str | None:
    try:
        p = os.fspath(path)  # type: ignore[arg-type]
    except TypeError:
        return None
    if not isinstance(p, str) or "evolve" not in p:
        # Cheap reject on the RAW path: every guarded root contains "evolve",
        # and this runs on every open() in the suite, so it must stay ahead of
        # the realpath() below. Known gap: a RELATIVE path whose spelling has
        # no "evolve" is rejected here even though cwd could put it under a
        # guarded root. That needs a test to chdir into the shared dir, which
        # nothing here does; the alternative is realpath() on every open.
        return None
    ap = os.path.realpath(os.path.abspath(p))
    for root in _ROOTS:
        if ap == root or ap.startswith(root + os.sep):
            return ap
    return None


def _writer_origin(limit: int = 4) -> str:
    """'<thread> | file:line fn > file:line fn' for the innermost non-library frames."""
    import threading
    import traceback
    frames = []
    for fr in reversed(traceback.extract_stack()[:-2]):
        fn = fr.filename.replace("\\", "/")
        if "/conftest.py" in fn or "/threading.py" in fn or "/site-packages/" in fn \
                or "/lib/python" in fn:
            continue
        frames.append(f"{Path(fn).name}:{fr.lineno} {fr.name}")
        if len(frames) >= limit:
            break
    return f"{threading.current_thread().name} | " + " > ".join(frames)


def _shared_dir_audit_hook(event: str, args: tuple) -> None:
    if event == "open":
        path, mode, flags = args
        if not ((mode and any(c in mode for c in "wax+"))
                or bool((flags or 0) & _WRITE_FLAGS)):
            return
    elif event in ("os.mkdir", "os.rename", "os.remove", "os.rmdir",
                   "os.truncate", "os.symlink", "os.link", "shutil.rmtree",
                   "shutil.move", "shutil.copyfile"):
        pass
    else:
        return
    for arg in (args if event != "open" else (args[0],)):
        ap = _guarded_path(arg)
        if ap is None:
            continue
        _shared_dir_writes.setdefault(_current_test["nodeid"], set()).add(ap)
        _shared_dir_writers.setdefault(ap, set()).add(_writer_origin())
        raise PermissionError(
            f"[shared-dir-guard] blocked a test write to the real pod shared "
            f"dir: {ap}"
        )


if not _GUARD_OFF:
    sys.addaudithook(_shared_dir_audit_hook)


def pytest_runtest_protocol(item, nextitem):  # noqa: D103
    _current_test["nodeid"] = item.nodeid
    _current_test["file"] = Path(str(getattr(item, "fspath", item.nodeid))).name
    return None  # let the default protocol run


@pytest.fixture(autouse=True)
def _no_real_shared_dir_writes():
    """Fail any test that wrote under the canonical shared dir."""
    yield
    if _GUARD_OFF:
        return
    nodeid = _current_test["nodeid"]
    paths = _shared_dir_writes.pop(nodeid, None)
    if not paths or _current_test["file"] in _BASELINED:
        return
    listed = "\n  ".join(
        f"{ap}\n      written by: " + "; ".join(sorted(_shared_dir_writers.get(ap, ())))
        for ap in sorted(paths))
    pytest.fail(
        f"{nodeid} wrote to the REAL pod shared dir (the write was blocked):\n"
        f"  {listed}\n\n"
        "If 'written by' names a thread other than MainThread, or a file other than "
        "this test's, the WRITER is an earlier test that leaked a background writer "
        "into this worker -- fix that test, not this one.\n"
        "A test must not touch {shared_dir} on the host. This is almost always "
        "a fixture network.json with no 'sharedDir' key: the resolver then "
        "falls back to config.DEFAULT_SHARED_DIR. Fix it by setting\n"
        '    "sharedDir": str(tmp_path / "shared")\n'
        "in the network dict the code under test receives (per-test tmp_path, "
        "not tmp_path.parent, so tests in one file cannot collide either).\n"
        "This is invisible on Linux CI and self-poisoning locally -- see "
        "PR #3984 and packages/admin/tests/real-shared-dir-baseline.txt.",
        pytrace=False,
    )
