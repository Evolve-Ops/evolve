"""Browser smoke test fixtures — spin up the admin server in a subprocess.

Phase 0 §4.4 baseline. The admin web UI is a single-page Flask app; this
boots it against a throwaway network.json so Playwright can drive a real
browser at it.

The server runs in a subprocess (not Flask's test_client) because
Playwright drives a real browser and needs real HTTP. We avoid
in-process threading because Flask's dev server isn't reentrant under
the kind of concurrent load Playwright generates.

Browser selection: pytest-playwright's `--browser` flag chooses which
engine(s) to run; default (no flag) is chromium only. CI runs each of
chromium/webkit/firefox in its own matrix job (see
.github/workflows/browser-smoke.yml) so the session-scoped
`admin_server` subprocess only ever serves one engine.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.error import URLError
from urllib.request import urlopen

import pytest


@dataclass
class _AdminServerState:
    """Server handle exposed to fixtures that need more than the URL.

    ``base_url`` is what tests usually want (string form). ``network_path``
    is exposed for tests that mutate the live config between assertions —
    e.g. the §4.1.d HTTPS-banner test which flips ``adminBaseUrl``
    between http:// and https:// to drive the show/hide logic.
    ``/api/network`` re-reads from disk on every request (see
    ``evolve_admin.config.load_network``), so a write + page reload is
    enough to observe the change; no server restart required.
    """

    base_url: str
    network_path: Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ADMIN_DIR = REPO_ROOT / "packages" / "admin"
ANALYZER_DIR = REPO_ROOT / "packages" / "analyzer"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, timeout_s: float = 30.0) -> None:
    """Poll until the admin server answers /api/version or timeout."""
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2.0) as resp:
                if resp.status == 200:
                    return
        except (URLError, ConnectionResetError, OSError) as e:
            last_err = e
        time.sleep(0.25)
    raise RuntimeError(
        f"admin server at {url} did not respond within {timeout_s}s "
        f"(last error: {last_err!r})"
    )


@pytest.fixture(scope="session")
def _admin_server_state(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_AdminServerState]:
    """Boot the evolve_admin Flask server in a subprocess.

    The shared bring-up. Tests typically pull ``admin_server`` (a string
    URL); tests that need to mutate network.json mid-run pull
    ``network_path`` instead. Both are thin wrappers around this fixture
    so the subprocess only starts once per session.
    """
    work = tmp_path_factory.mktemp("evolve-browser-smoke")
    shared = work / "evolve"
    (shared / "signals" / "firing").mkdir(parents=True)
    (shared / "signals" / "snoozed").mkdir(parents=True)
    (shared / "signals" / "archived").mkdir(parents=True)

    network_path = work / "network.json"
    template = json.loads((FIXTURES_DIR / "network.json").read_text())
    template["sharedDir"] = str(shared)
    network_path.write_text(json.dumps(template, indent=2))

    port = _free_port()
    env = os.environ.copy()
    # PYTHONPATH wins over the editable install — point at the worktree
    # copy so test runs in worktrees exercise local changes, matching
    # how packages/admin/tests/conftest.py rebinds evolve_admin.
    pythonpath_extra = [str(ADMIN_DIR), str(ANALYZER_DIR)]
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        pythonpath_extra + ([existing] if existing else [])
    )
    # Quieter logs in test runs.
    env.setdefault("EVOLVE_LOG_LEVEL", "WARNING")
    # Admin auth is on by default (roadmap 2.6); the browser smoke harness
    # never pairs, so disable enforcement via the test-only env escape (the
    # same one packages/admin/tests/conftest.py sets). Without this the SPA
    # redirects every page to /pair and every test 401s.
    env["EVOLVE_ADMIN_AUTH_DISABLED"] = "1"

    # Route subprocess stdout/stderr through a log file rather than
    # subprocess.PIPE. Werkzeug's per-request access log writes one line
    # per HTTP hit and pytest does not drain the pipe — the 64KB kernel
    # buffer fills after a few hundred requests, which blocks Werkzeug
    # request-handler threads on their stdout write, which causes every
    # subsequent HTTP request to time out at 30s. Symptom: ~30 tests
    # pass, then every following test fails with a Page.goto /
    # APIRequestContext.get timeout. Funneling to a file unblocks the
    # writer and still preserves the diagnostic for the startup-failure
    # path below.
    log_path = work / "admin-server.log"
    log_fh = log_path.open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "evolve_admin.web.run",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--network",
            str(network_path),
        ],
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        text=True,
    )

    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(f"{base_url}/api/version", timeout_s=30.0)
    except Exception:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            pass
        try:
            log_fh.close()
            out = log_path.read_text()
        except Exception:
            out = ""
        raise RuntimeError(
            f"admin server failed to start on {base_url}\n"
            f"--- subprocess output ---\n{out}\n--- end ---"
        )

    yield _AdminServerState(base_url=base_url, network_path=network_path)

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    try:
        log_fh.close()
    except Exception:
        pass
    shutil.rmtree(work, ignore_errors=True)


@pytest.fixture(scope="session")
def admin_server(_admin_server_state: _AdminServerState) -> str:
    """Base URL of the admin server (string form)."""
    return _admin_server_state.base_url


@pytest.fixture
def network_path(_admin_server_state: _AdminServerState) -> Iterator[Path]:
    """Path to the live network.json the admin server is reading.

    Function-scoped so each test that mutates the file gets a clean
    restore at teardown — even when an assertion fails. The admin
    server re-reads on every /api/network request, so a write here is
    observable in the browser on the next page reload.
    """
    path = _admin_server_state.network_path
    original = path.read_text()
    try:
        yield path
    finally:
        path.write_text(original)
