"""Service-worker shell-fingerprint regression guard.

The ``/sw.js`` handler prepends a short hash of ``index.html`` to the
served body so an HTML-only change (i.e. nearly every UI PR) flips the
SW byte stream. The browser uses that byte diff to decide whether to
fire ``updatefound`` — without the fingerprint, an HTML-only deploy
would leave any open tab on its launch-time JS forever, because sw.js
itself is byte-identical between versions.

Two contracts to pin:

  1. The served sw.js contains a ``pwa-shell-fingerprint:`` line whose
     hash matches the deployed ``index.html``.
  2. Two requests with different ``index.html`` content produce
     different fingerprints. (If they didn't, the whole point of the
     stamping is moot.)

Both run via the Flask test client — no browser, no network.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


_FINGERPRINT_RE = re.compile(r"^// pwa-shell-fingerprint: ([0-9a-f]+)$", re.MULTILINE)


@pytest.fixture()
def client(tmp_path):
    from evolve_admin.web.server import create_app

    network = {
        "bots": {},
        "sharedDir": str(tmp_path / "shared"),
    }
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps(network))
    (tmp_path / "shared").mkdir()

    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _fingerprint_from(body: str) -> str:
    m = _FINGERPRINT_RE.search(body)
    assert m, (
        "Served sw.js is missing the pwa-shell-fingerprint stamp. "
        "Without it, an HTML-only deploy can't trigger updatefound — "
        "open tabs would stay on the launch-time SPA shell."
    )
    return m.group(1)


def test_sw_js_carries_shell_fingerprint(client):
    """The fingerprint line is present, on its own line, and ASCII-hex."""
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    fp = _fingerprint_from(body)
    assert len(fp) >= 8, f"fingerprint suspiciously short: {fp!r}"
    # Must come before the SW's own code — placement matters because the
    # browser's byte-diff sees the whole script and the comment is the
    # cheapest way to flip the stream without touching sw.js source.
    fp_idx = body.index(f"pwa-shell-fingerprint: {fp}")
    sw_code_idx = body.index("self.addEventListener")
    assert fp_idx < sw_code_idx, (
        "fingerprint must precede the SW source so a byte diff is "
        "guaranteed even if sw.js itself is unchanged"
    )


def test_sw_fingerprint_changes_with_index_html(client, monkeypatch):
    """Different index.html bytes → different fingerprint. If this
    breaks, HTML-only deploys silently skip the update toast."""
    static_dir = Path(__file__).parent.parent / "evolve_admin" / "web"
    original_index = (static_dir / "index.html").read_bytes()

    # First request: capture the fingerprint for the real shell.
    resp_a = client.get("/sw.js")
    fp_a = _fingerprint_from(resp_a.get_data(as_text=True))

    # Patch index.html on disk to a different body, then re-request.
    # Restore in finally so a test crash doesn't leave the worktree
    # with a clobbered shell.
    altered = (static_dir / "index.html")
    try:
        altered.write_bytes(original_index + b"\n<!-- regression probe -->\n")
        resp_b = client.get("/sw.js")
        fp_b = _fingerprint_from(resp_b.get_data(as_text=True))
    finally:
        altered.write_bytes(original_index)

    assert fp_a != fp_b, (
        f"fingerprint did not change when index.html changed "
        f"({fp_a} == {fp_b}) — the stamping isn't actually reading the "
        f"file fresh on each request"
    )


def test_sw_response_still_has_no_cache_headers(client):
    """The fingerprint is only useful if intermediaries don't pin a
    stale sw.js past a deploy. Regression guard for the existing
    Cache-Control contract."""
    resp = client.get("/sw.js")
    assert resp.headers.get("Cache-Control") == "no-cache, must-revalidate"
    assert resp.headers.get("Service-Worker-Allowed") == "/"


_CACHE_VERSION_RE = re.compile(r"const CACHE_VERSION = '([^']+)';")


def test_sw_fingerprint_changes_with_js_module(client):
    """Different JS-MODULE bytes → different fingerprint.

    This is the gap that caused the fleet-promote lockout: the shell
    references modules by stable filename, so hashing index.html alone
    left a module-only deploy (the common UI PR) byte-identical — no
    ``updatefound``, stale module cache served forever. The fingerprint
    must fold the module bytes in too.
    """
    pages_dir = (
        Path(__file__).parent.parent / "evolve_admin" / "web"
        / "static" / "js" / "pages"
    )
    # Pick any real page module deterministically.
    module = sorted(pages_dir.glob("*.js"))[0]
    original = module.read_bytes()

    fp_a = _fingerprint_from(client.get("/sw.js").get_data(as_text=True))
    try:
        module.write_bytes(original + b"\n// regression probe\n")
        fp_b = _fingerprint_from(client.get("/sw.js").get_data(as_text=True))
    finally:
        module.write_bytes(original)

    assert fp_a != fp_b, (
        f"fingerprint did not change when a JS module changed "
        f"({fp_a} == {fp_b}) — a module-only deploy would not flip the "
        f"SW byte stream, so open tabs would keep serving the stale "
        f"cached module (the fleet-promote lockout)."
    )


def test_sw_cache_version_is_stamped_per_build(client):
    """The served sw.js must carry a build-specific cache version, not the
    raw ``__PWA_CACHE_VERSION__`` placeholder.

    The SW's ``activate`` handler deletes every cache whose name isn't the
    current ``CACHE_VERSION``. If the placeholder shipped unsubstituted,
    every build would share one cache name and a deploy could never flush
    the prior build's shell/modules — the stale-cache lockout. Pin that
    the server stamps it and that it tracks the asset fingerprint.
    """
    body = client.get("/sw.js").get_data(as_text=True)
    m = _CACHE_VERSION_RE.search(body)
    assert m, "served sw.js has no CACHE_VERSION assignment"
    cache_version = m.group(1)
    assert cache_version != "__PWA_CACHE_VERSION__", (
        "CACHE_VERSION placeholder was not substituted — activate() can't "
        "flush prior builds, so a deploy keeps serving stale cached assets."
    )
    fp = _fingerprint_from(body)
    assert cache_version == f"evolve-pwa-{fp}", (
        f"cache version {cache_version!r} should track the asset "
        f"fingerprint {fp!r} so each asset-changing build gets a fresh "
        f"cache that activate() swaps to."
    )
