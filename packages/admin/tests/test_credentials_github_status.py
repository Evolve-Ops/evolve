"""Tests for the GitHub three-purposes status endpoint + Credentials UI row.

Endpoint under test:
  GET /api/credentials/github/status   summary of backup + intake + general
                                       access for the Credentials sub-tab

UI under test:
  The GitHub row in #integrations-keys-credentials with its three
  Set Up / Manage buttons and the JS loader that hydrates it.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

_INDEX_HTML = _ADMIN_DIR / "evolve_admin" / "web" / "index.html"

_PLUGINS_JS = _ADMIN_DIR / "evolve_admin" / "web"/ "static" / "js" / "pages" / "plugins.js"
from evolve_admin.intake import permissions as perms  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_perm_caches():
    perms.clear_caches()
    yield
    perms.clear_caches()


def _build_app(tmp_path, network, *, keystore_values=None, transport=None):
    """Wire a Flask app + the evo_routes blueprint with stubbed I/O.

    keystore_values: {slot: value} — used by the stub KeystoreManager.
                     Any slot not in the dict returns None.
    transport: optional (method, url, headers, body) -> (status, json_dict)
               stub for the GitHub /user permissions call.
    """
    from flask import Flask
    from evolve_admin.web.evo_routes import register_evo_routes

    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    kv = dict(keystore_values or {})

    class _StubMgr:
        def __init__(self, *a, **kw):
            pass
        def get_value(self, slot):
            return kv.get(slot)

    patches = [patch("evolve_admin.keystore.KeystoreManager", _StubMgr)]
    if transport is not None:
        patches.append(patch.object(perms, "_default_transport", transport))

    for p in patches:
        p.start()

    app = Flask(__name__)
    app.config["TESTING"] = True
    register_evo_routes(app, network_path)

    def _stop_all():
        for p in patches:
            p.stop()

    return app, _stop_all


# ─── Backend: status endpoint ────────────────────────────────────────────


def test_status_counts_configured_backup_bots(tmp_path):
    """configured_count + total_bots reflect bots.<id>.backupRepoUrl."""
    network = {
        "sharedDir": str(tmp_path),
        "bots": {
            "team_bot_a":    {"backupRepoUrl": "git@github.com:user/team_bot_a.git"},
            "team_bot_c":  {"backupRepoUrl": "git@github.com:user/team_bot_c.git"},
            "admin_bot":  {"backupRepoUrl": "git@github.com:user/admin_bot.git"},
            "team_bot_b":   {},
            "personal_bot":  {"port": 9000},
        },
    }
    app, stop = _build_app(tmp_path, network)
    try:
        body = app.test_client().get("/api/credentials/github/status").get_json()
    finally:
        stop()

    assert body["backup"]["total_bots"] == 5
    assert body["backup"]["configured_count"] == 3
    by_bot = {b["bot"]: b["configured"] for b in body["backup"]["bot_status"]}
    assert by_bot == {
        "team_bot_a": True, "team_bot_c": True, "admin_bot": True,
        "team_bot_b": False, "personal_bot": False,
    }


def test_status_empty_bots(tmp_path):
    """No bots → empty bot_status, zero counts, endpoint still 200."""
    network = {"sharedDir": str(tmp_path)}
    app, stop = _build_app(tmp_path, network)
    try:
        r = app.test_client().get("/api/credentials/github/status")
    finally:
        stop()
    assert r.status_code == 200
    body = r.get_json()
    assert body["backup"]["total_bots"] == 0
    assert body["backup"]["configured_count"] == 0
    assert body["backup"]["bot_status"] == []


def test_status_intake_configured_with_login(tmp_path):
    """When github_intake holds a valid PAT, login resolves via /user."""
    def _tx(method, url, headers, body):
        if url.endswith("/user"):
            return 200, {"login": "cjalden"}
        return 500, {}

    network = {"sharedDir": str(tmp_path), "bots": {}}
    app, stop = _build_app(
        tmp_path, network,
        keystore_values={"github_intake": "ghp_validtoken"},
        transport=_tx,
    )
    try:
        body = app.test_client().get("/api/credentials/github/status").get_json()
    finally:
        stop()

    assert body["intake"]["configured"] is True
    assert body["intake"]["login"] == "cjalden"
    assert body["intake"]["slot"] == "github_intake"


def test_status_intake_unconfigured_returns_null_login(tmp_path):
    """No token in the keystore → configured=False, login=None."""
    network = {"sharedDir": str(tmp_path), "bots": {}}
    app, stop = _build_app(tmp_path, network, keystore_values={})
    try:
        body = app.test_client().get("/api/credentials/github/status").get_json()
    finally:
        stop()

    assert body["intake"]["configured"] is False
    assert body["intake"]["login"] is None
    assert body["intake"]["slot"] == "github_intake"


def test_status_intake_token_but_validation_fails(tmp_path):
    """Token present but /user returns 401 → configured=True, login=None.

    The UI distinguishes 'saved but invalid' (warn the operator) from 'not
    set up at all' (start the setup flow) on the login field, not on
    configured.
    """
    def _tx(method, url, headers, body):
        if url.endswith("/user"):
            return 401, {"message": "Bad credentials"}
        return 500, {}

    network = {"sharedDir": str(tmp_path), "bots": {}}
    app, stop = _build_app(
        tmp_path, network,
        keystore_values={"github_intake": "ghp_expired"},
        transport=_tx,
    )
    try:
        body = app.test_client().get("/api/credentials/github/status").get_json()
    finally:
        stop()

    assert body["intake"]["configured"] is True
    assert body["intake"]["login"] is None


def test_status_handles_keystore_exception(tmp_path):
    """A blown-up keystore (missing vault dir, etc.) doesn't crash the
    page — intake reports unconfigured rather than 500."""
    from flask import Flask
    from evolve_admin.web.evo_routes import register_evo_routes

    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "sharedDir": str(tmp_path),
        "bots": {"team_bot_a": {"backupRepoUrl": "x"}},
    }))

    class _ExplodingMgr:
        def __init__(self, *a, **kw):
            raise RuntimeError("keystore unreadable")

    with patch("evolve_admin.keystore.KeystoreManager", _ExplodingMgr):
        app = Flask(__name__)
        app.config["TESTING"] = True
        register_evo_routes(app, network_path)
        r = app.test_client().get("/api/credentials/github/status")

    assert r.status_code == 200
    body = r.get_json()
    assert body["intake"]["configured"] is False
    assert body["intake"]["login"] is None
    # Backup counting still works — it doesn't depend on the keystore.
    assert body["backup"]["configured_count"] == 1


def test_status_general_access_unconfigured(tmp_path):
    """Empty keystore → pod.configured=False, no overrides. The shape
    is always present (pod sub-dict + overrides list) regardless of
    state so the UI never has to defensive-code a missing key."""
    network = {"sharedDir": str(tmp_path)}
    app, stop = _build_app(tmp_path, network)
    try:
        body = app.test_client().get("/api/credentials/github/status").get_json()
    finally:
        stop()
    ga = body["general_access"]
    assert ga["pod"]["configured"] is False
    assert ga["pod"]["login"] is None
    assert ga["pod"]["slot"] == "github_general_access"
    assert ga["overrides"] == []


def test_status_general_access_pod_configured(tmp_path):
    """github_general_access slot set + /user resolves → pod.configured
    + login surfaced. This is the canonical 'POD tab shows the pod-wide
    PAT' read path."""
    def _tx(method, url, headers, body):
        if url.endswith("/user"):
            return 200, {"login": "cjalden"}
        return 500, {}

    network = {"sharedDir": str(tmp_path), "bots": {}}
    app, stop = _build_app(
        tmp_path, network,
        keystore_values={"github_general_access": "ghp_podtoken"},
        transport=_tx,
    )
    try:
        body = app.test_client().get("/api/credentials/github/status").get_json()
    finally:
        stop()

    ga = body["general_access"]
    assert ga["pod"]["configured"] is True
    assert ga["pod"]["login"] == "cjalden"
    assert ga["overrides"] == []


def test_status_general_access_lists_per_bot_overrides(tmp_path):
    """A bot with its own github-<bot_id> slot value appears in the
    overrides list with its resolved login. Bots without an override
    are absent — the list is short by design."""
    logins = {
        "ghp_podtoken": "cjalden",
        "ghp_atlasoverride": "atlas-bot",
    }

    def _tx(method, url, headers, body):
        if not url.endswith("/user"):
            return 500, {}
        auth = (headers or {}).get("Authorization") or ""
        for tok, login in logins.items():
            if tok in auth:
                return 200, {"login": login}
        return 401, {"message": "bad creds"}

    network = {
        "sharedDir": str(tmp_path),
        "bots": {
            "atlas": {"backupRepoUrl": "x"},
            "team_bot_a": {"backupRepoUrl": "x"},
        },
    }
    app, stop = _build_app(
        tmp_path, network,
        keystore_values={
            "github_general_access": "ghp_podtoken",
            "github-atlas": "ghp_atlasoverride",
            # team_bot_a has no override
        },
        transport=_tx,
    )
    try:
        body = app.test_client().get("/api/credentials/github/status").get_json()
    finally:
        stop()

    ga = body["general_access"]
    assert ga["pod"]["configured"] is True
    assert ga["pod"]["login"] == "cjalden"
    assert len(ga["overrides"]) == 1
    o = ga["overrides"][0]
    assert o["bot"] == "atlas"
    assert o["login"] == "atlas-bot"
    assert o["slot"] == "github-atlas"


def test_status_skips_non_dict_bot_entries(tmp_path):
    """Defensive: a bot entry that isn't a dict (legacy schemas, partial
    edits) is skipped instead of crashing the endpoint."""
    network = {
        "sharedDir": str(tmp_path),
        "bots": {
            "team_bot_a": {"backupRepoUrl": "git@github.com:u/team_bot_a.git"},
            "broken": "this should be a dict but isn't",
        },
    }
    app, stop = _build_app(tmp_path, network)
    try:
        body = app.test_client().get("/api/credentials/github/status").get_json()
    finally:
        stop()
    bots = {b["bot"] for b in body["backup"]["bot_status"]}
    assert bots == {"team_bot_a"}
    assert body["backup"]["total_bots"] == 1
    assert body["backup"]["configured_count"] == 1


# ─── Concurrency + total-deadline (non-blocking resolution) ──────────────


def test_status_resolutions_run_concurrently(tmp_path):
    """The per-token GitHub /user login resolutions fan out concurrently
    rather than running serially. Four configured tokens whose /user calls
    each sleep must complete in ~one call's time, not the sum.

    Regression guard for the original serial loop, where a slow GitHub
    made the POD subtab (which fires only this endpoint) feel broken —
    up to N×timeout end to end.
    """
    call_count = {"n": 0}
    lock = threading.Lock()
    logins_by_token = {
        "ghp_intake": "octo-intake",
        "ghp_pod": "octo-pod",
        "ghp_atlas": "octo-atlas",
        "ghp_teamb": "octo-teamb",
    }

    def _tx(method, url, headers, body):
        if not url.endswith("/user"):
            return 500, {}
        with lock:
            call_count["n"] += 1
        time.sleep(0.5)
        # Map each token to a distinct login so we can assert the result is
        # routed back to the right slot (no cross-wiring under fan-out).
        auth = (headers or {}).get("Authorization") or ""
        for tok, login in logins_by_token.items():
            if tok in auth:
                return 200, {"login": login}
        return 401, {"message": "bad creds"}

    network = {
        "sharedDir": str(tmp_path),
        "bots": {
            "atlas": {"backupRepoUrl": "x"},
            "team_bot_b": {"backupRepoUrl": "y"},
        },
    }
    app, stop = _build_app(
        tmp_path, network,
        keystore_values={
            "github_intake": "ghp_intake",
            "github_general_access": "ghp_pod",
            "github-atlas": "ghp_atlas",
            "github-team_bot_b": "ghp_teamb",
        },
        transport=_tx,
    )
    try:
        t0 = time.monotonic()
        body = app.test_client().get("/api/credentials/github/status").get_json()
        elapsed = time.monotonic() - t0
    finally:
        stop()

    # Every token was resolved (nothing silently skipped) ...
    assert call_count["n"] == 4
    # ... but concurrently: serial would be ~4×0.5s = 2.0s; concurrent ≈ 0.5s.
    assert elapsed < 1.3, f"resolutions did not run concurrently (took {elapsed:.2f}s)"

    # Response shape unchanged + each login routed to the correct slot.
    assert body["intake"]["configured"] is True
    assert body["intake"]["login"] == "octo-intake"
    assert body["general_access"]["pod"]["configured"] is True
    assert body["general_access"]["pod"]["login"] == "octo-pod"
    ovr = {o["bot"]: o["login"] for o in body["general_access"]["overrides"]}
    assert ovr == {"atlas": "octo-atlas", "team_bot_b": "octo-teamb"}


def test_status_returns_configured_when_resolution_times_out(tmp_path):
    """When a login resolution can't finish within the overall deadline,
    the endpoint still returns the ``configured`` booleans promptly and
    reports the unresolved login as None. The row must never hang on a
    slow-but-alive GitHub connection (urllib's timeout is per-socket-op,
    not a ceiling on the whole call)."""
    from evolve_admin.web import evo_routes as evo_routes_mod

    release = threading.Event()

    def _tx(method, url, headers, body):
        if not url.endswith("/user"):
            return 500, {}
        # Block well past the (patched) deadline. Released in finally so the
        # background worker thread exits cleanly instead of leaking.
        release.wait(timeout=5)
        return 200, {"login": "cjalden"}

    network = {"sharedDir": str(tmp_path), "bots": {}}
    app, stop = _build_app(
        tmp_path, network,
        keystore_values={"github_general_access": "ghp_slowtoken"},
        transport=_tx,
    )
    with patch.object(evo_routes_mod, "_GITHUB_STATUS_RESOLVE_DEADLINE_S", 0.3):
        try:
            t0 = time.monotonic()
            body = app.test_client().get("/api/credentials/github/status").get_json()
            elapsed = time.monotonic() - t0

            # configured derives from token presence — never blocked on the GET.
            assert body["general_access"]["pod"]["configured"] is True
            # login timed out → None (best-effort enrichment, not a hang).
            assert body["general_access"]["pod"]["login"] is None
            # Returned at ~the deadline, not the 5s socket wait.
            assert elapsed < 1.5, f"endpoint blocked on slow resolution ({elapsed:.2f}s)"
        finally:
            release.set()  # unblock the straggler worker so its thread exits
            stop()


def test_status_deadline_does_not_drop_fast_resolutions(tmp_path):
    """A tight deadline must not penalize tokens that resolve quickly: a
    fast login still surfaces even when the deadline is short. Guards
    against an off-by-one where ``wait``'s done-set is mishandled."""
    from evolve_admin.web import evo_routes as evo_routes_mod

    def _tx(method, url, headers, body):
        if url.endswith("/user"):
            return 200, {"login": "cjalden"}
        return 500, {}

    network = {"sharedDir": str(tmp_path), "bots": {}}
    app, stop = _build_app(
        tmp_path, network,
        keystore_values={"github_general_access": "ghp_fast"},
        transport=_tx,
    )
    with patch.object(evo_routes_mod, "_GITHUB_STATUS_RESOLVE_DEADLINE_S", 0.5):
        try:
            body = app.test_client().get("/api/credentials/github/status").get_json()
        finally:
            stop()
    assert body["general_access"]["pod"]["configured"] is True
    assert body["general_access"]["pod"]["login"] == "cjalden"


# ─── Frontend markup contract ────────────────────────────────────────────


def _read_html() -> str:
    return _INDEX_HTML.read_text() + "\n" + _PLUGINS_JS.read_text()


def _credentials_block(html: str) -> str:
    start = html.find('id="integrations-keys-credentials"')
    assert start > 0, "credentials sub-tab not found in index.html"
    # Find the start of this subtab-page div, then the next subtab-page.
    div_start = html.rfind('<div class="subtab-page"', 0, start)
    assert div_start >= 0
    next_div = html.find('<div class="subtab-page"', start + 10)
    return html[div_start:next_div if next_div > 0 else len(html)]


def test_github_row_container_exists():
    """The GitHub row container is present and sits inside the
    Credentials sub-tab so the rest of the loader logic finds it."""
    block = _credentials_block(_read_html())
    assert 'id="ik-github-row"' in block
    # Heading is the canonical entry-point label, not the per-bot row.
    assert re.search(r'class="section-head">\s*GitHub\b', block) is not None


def test_github_row_appears_before_api_keys_section():
    """Spec: GitHub row sits *above* the API Keys block — placing it
    below would hide the canonical entry point behind the long per-bot
    table that operators currently scroll past."""
    block = _credentials_block(_read_html())
    gh_idx = block.find('id="ik-github-row"')
    # Anchor on the section-head element so we match the actual heading,
    # not a stray "API Keys" mention in a comment block.
    m = re.search(r'class="section-head">\s*API Keys', block)
    assert gh_idx > 0 and m is not None
    assert gh_idx < m.start(), "GitHub row must precede API Keys section"


def test_setup_handlers_present():
    """The setup CTAs route to their respective flows. The renderer is
    now POD/per-bot aware, so the IDs only appear when the POD tab is
    selected — but the handler symbols must exist in the JS regardless.

    Pins the deep-link contract: backup → onboard modal,
    intake/general-access → intake-token modal (slot-agnostic).
    """
    html = _read_html()
    # POD-mode IDs for the intake + general CTAs. These render only when
    # the POD tab is the active selection.
    assert 'id="ik-github-intake-setup"' in html
    assert 'id="ik-github-general-setup"' in html
    # The backup helper still exists. Its DOM id moved (the per-bot row
    # uses a different button than the pod summary row) but the function
    # symbol is the contract — pin it.
    assert "_ikGithubBackupSetup(" in html
    # Intake + general both route through the slot-agnostic intake-token
    # modal. The pod-wide general-access slot is the v1 contract.
    assert "_inboxOpenIntakeTokenModal(" in html
    assert "'github_general_access'" in html or '"github_general_access"' in html


def test_backup_helper_calls_openonboardmodal_with_github():
    """The backup helper must route to openOnboardModal('github', ...) —
    that's the existing flow's entry point and the contract we're
    deep-linking into."""
    html = _read_html()
    m = re.search(
        r"function\s+_ikGithubBackupSetup\s*\([^)]*\)\s*\{[^}]*openOnboardModal\s*\(\s*['\"]github['\"]",
        html,
    )
    assert m is not None, "_ikGithubBackupSetup must call openOnboardModal('github', ...)"


def test_loader_hits_the_status_endpoint():
    """ikLoadGithubStatus reads /api/credentials/github/status — pin the
    URL so a server-side rename doesn't silently blank the row."""
    html = _read_html()
    m = re.search(
        r"function\s+ikLoadGithubStatus\s*\([^)]*\)\s*\{[\s\S]*?/api/credentials/github/status",
        html,
    )
    assert m is not None, "ikLoadGithubStatus must fetch /api/credentials/github/status"


def test_loader_is_wired_into_initial_load():
    """The Promise.all in the Integrations page loader must include
    ikLoadGithubStatus, otherwise the row stays at 'Loading…' until
    the operator hits Sync."""
    html = _read_html()
    assert "ikLoadGithubStatus()" in html
    # And the Sync button refresh path should re-fetch it too.
    m = re.search(
        r"function\s+ikRefreshKeys\s*\([^)]*\)\s*\{[\s\S]*?ikLoadGithubStatus\s*\(",
        html,
    )
    assert m is not None, "ikRefreshKeys must also call ikLoadGithubStatus"


# ─── POD tab + sentinel handling ─────────────────────────────────────────


def test_pod_tab_is_prepended_to_bot_strip():
    """The tab strip starts with a POD tab keyed on the __pod__ sentinel.
    Without this, pod-wide credentials get rendered under whichever bot
    happens to be selected — misleading for issue tracking and general
    code access, which aren't per-bot at all."""
    js = _PLUGINS_JS.read_text()
    # The sentinel constant is the contract.
    assert "IK_POD_SENTINEL = '__pod__'" in js
    # The renderer prepends a POD tab in loadIntegrationsKeys().
    m = re.search(
        r"function\s+loadIntegrationsKeys\s*\([^)]*\)[\s\S]*?data-bot=\"\$\{IK_POD_SENTINEL\}\"",
        js,
    )
    assert m is not None, "loadIntegrationsKeys must prepend a POD tab"


def test_pod_mode_skips_per_bot_loaders():
    """In POD mode, the page must NOT fan out per-bot loaders. Otherwise
    every navigation to POD hammers MCP/Plugins/Hooks/Embeddings endpoints
    with __pod__ as the bot id and gets 404s back."""
    js = _PLUGINS_JS.read_text()
    # The pod-mode branch short-circuits before the per-bot Promise.all.
    m = re.search(
        r"if\s*\(\s*_ikIsPod\(\)\s*\)\s*\{[\s\S]{0,400}?ikLoadGithubStatus\(\)[\s\S]{0,80}?return",
        js,
    )
    assert m is not None, "POD mode must short-circuit before the per-bot Promise.all"


def test_pod_mode_class_gates_per_bot_only_sections():
    """The .ik-pod-mode CSS class hides per-bot sections (API Keys table,
    onboard banner) so they don't show under the POD tab. Markup needs
    the ik-per-bot-only class on those wrappers, and the CSS rule needs
    to be present."""
    html = _INDEX_HTML.read_text()
    css = (_ADMIN_DIR / "evolve_admin" / "web" / "static" / "css" / "base.css").read_text()
    # The Credentials sub-tab wraps the per-bot block with the gating class.
    block = _credentials_block(html + "\n" + _PLUGINS_JS.read_text())
    assert 'class="ik-per-bot-only"' in block
    # The CSS rule hides it under the pod-mode parent class.
    assert ".ik-pod-mode .ik-per-bot-only" in css


# ─── github_install resolver ─────────────────────────────────────────────


def test_resolve_github_token_prefers_per_bot_override():
    """resolve_github_token returns the override when set, even if pod
    is also set. Without this precedence, an operator can't temporarily
    grant a single bot a narrower-scoped PAT."""
    from evolve_admin.skills import github_install as gh

    store = {
        gh.POD_KEYSTORE_SLOT: "ghp_pod",
        gh.keystore_slot_for("atlas"): "ghp_atlas_override",
    }
    token, source = gh.resolve_github_token(
        "atlas", read_keystore_slot=store.get,
    )
    assert token == "ghp_atlas_override"
    assert source == "override"


def test_resolve_github_token_falls_back_to_pod():
    """When the per-bot slot is empty, the pod-wide slot is the answer.
    This is the canonical 'configure once for the pod' path."""
    from evolve_admin.skills import github_install as gh

    store = {gh.POD_KEYSTORE_SLOT: "ghp_pod"}
    token, source = gh.resolve_github_token(
        "team_bot_a", read_keystore_slot=store.get,
    )
    assert token == "ghp_pod"
    assert source == "pod"


def test_resolve_github_token_returns_none_when_both_empty():
    """Both slots empty → (None, 'none'). Caller can decide whether to
    prompt the operator or treat the bot as un-installable."""
    from evolve_admin.skills import github_install as gh

    token, source = gh.resolve_github_token(
        "team_bot_a", read_keystore_slot=lambda _s: None,
    )
    assert token is None
    assert source == "none"


def test_resolve_github_token_ignores_blank_strings():
    """A registered-but-blank slot should be treated as unset. The
    keystore allows ``set_value(slot, "")`` to revoke without removing
    the registration; the resolver must honor that as 'not set'."""
    from evolve_admin.skills import github_install as gh

    store = {
        gh.keystore_slot_for("atlas"): "   ",  # whitespace-only
        gh.POD_KEYSTORE_SLOT: "ghp_pod",
    }
    token, source = gh.resolve_github_token(
        "atlas", read_keystore_slot=store.get,
    )
    assert token == "ghp_pod"
    assert source == "pod"


def test_resolve_github_token_tolerates_reader_exceptions():
    """A reader that raises (keychain unavailable, etc.) folds to None
    instead of propagating — the operator should see 'not configured'
    rather than a 500 on the Credentials page."""
    from evolve_admin.skills import github_install as gh

    def _exploding(_slot):
        raise RuntimeError("keychain unreadable")

    token, source = gh.resolve_github_token(
        "atlas", read_keystore_slot=_exploding,
    )
    assert token is None
    assert source == "none"
