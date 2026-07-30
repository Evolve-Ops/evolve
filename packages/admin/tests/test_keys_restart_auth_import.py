"""tests/test_keys_restart_auth_import.py — OC-SQLITE-AUTH-WRITE follow-up.

PR #3136 fixed the WRITE side of the OpenClaw per-agent sqlite auth store for
the PROVISION/DEPLOY paths only: OpenClaw 2026.6+ imports ``auth-profiles.json``
into ``openclaw-agent.sqlite`` on **agent-CLI init**, NOT on gateway start, so a
live key add/rotate + the operator's "Restart gateway" left the running agent on
a stale/empty key → ``No API key found``.

This pins the LIVE-edit wiring of
``oc_auth_provision.ensure_agent_auth_store_imported`` into the admin web layer:

  * ``api_admin_add_key`` (write-site, strategy A) — primes the bot's per-agent
    sqlite store right after a successful ``auth-profiles.json`` write, and only
    then (never on a failed write).
  * ``api_admin_restart_gateway`` (restart chokepoint, strategy B) — reconciles
    the sqlite store from ``auth-profiles.json`` BEFORE bouncing the gateway, so
    the operator's add / rotate / manual-edit + Restart-gateway sequence ends
    with the running agent holding the on-disk key. Every "requires_restart" UI
    prompt funnels through this one endpoint, so a single import-first covers all.

Both paths reuse the #3136 helper (the key NEVER reaches argv/stdin) and are
best-effort (a failed import never blocks the write or the restart). The
deploy/provision side is covered by ``test_oc_auth_store_import.py``.

NB: ``evolve_admin`` is imported LAZILY inside the fixtures/tests (never at
module top). Importing ``routes_admin`` at collection time perturbs the
``_restore_module_state`` autouse fixture's per-test import/cleanup cycle that
sibling files (e.g. test_onboarding) depend on — the same lazy-import discipline
``test_web_scheduler_seam`` uses for ``register_admin_routes``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for _p in (_ADMIN, _ANALYZER):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# A pinned absolute oc path so the import trigger's sudo argv is deterministic.
_OC_BIN = "/opt/homebrew/bin/openclaw"
# The bot's macOS account differs from its bot_id — proves _prime_auth_store
# resolves through the get_bot_user seam, not the bot_id literal.
_BOT = "atlas"
_USER = "atlas-acct"
# A realistic key that survives the _PLACEHOLDER_RE boundary check.
_KEY = "sk-ant-api03-LEAKCHECK-deadbeef-9999"
_IMPORT = "evolve_admin.oc_auth_provision.ensure_agent_auth_store_imported"


def _seed_network(tmp_path: Path) -> Path:
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "members": [_BOT],
        "bots": {_BOT: {"role": "member", "port": 19010, "user": _USER}},
        "sharedDir": str(tmp_path / "shared"),
    }))
    (tmp_path / "shared").mkdir(exist_ok=True)
    return p


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Lazy imports (see module docstring): never import routes_admin at collection.
    from evolve_admin.web import routes_admin as ra
    import evolve_admin.web.server as server_mod

    network_path = _seed_network(tmp_path)
    # _audit_log_entry is looked up at call time on the server module; stub it
    # so the endpoints don't touch the real audit log during the test.
    monkeypatch.setattr(server_mod, "_audit_log_entry", lambda *a, **k: None)
    app = Flask(__name__)
    ra.register_admin_routes(app, network_path)
    app.config["TESTING"] = True
    return app.test_client()


# ── api_admin_add_key (strategy A): prime sqlite after a successful write ─────


def test_add_key_triggers_auth_store_import(client):
    """A successful key write primes the bot's per-agent sqlite store exactly
    once, resolving the bot's macOS account (not the bot_id) via the seam."""
    from evolve_admin.web import routes_admin as ra
    calls: list = []
    with patch.object(ra, "_shared_read_auth_profiles", return_value={}), \
         patch.object(ra, "_shared_write_auth_profiles", return_value=True), \
         patch(_IMPORT,
               side_effect=lambda *a, **k: calls.append(a) or (True, "ok")):
        r = client.post(f"/api/admin/keys/{_BOT}/anthropic",
                        json={"key_value": _KEY, "key_type": "api_key"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["ok"] is True
    assert len(calls) == 1, calls
    assert calls[0] == (_BOT, _USER)


def test_add_key_no_import_on_failed_write(client):
    """A failed write returns 500 and must NOT prime the store — there is no new
    key on disk to import, and we don't want to mask the write failure."""
    from evolve_admin.web import routes_admin as ra
    calls: list = []
    with patch.object(ra, "_shared_read_auth_profiles", return_value={}), \
         patch.object(ra, "_shared_write_auth_profiles", return_value=False), \
         patch(_IMPORT, side_effect=lambda *a, **k: calls.append(a) or (True, "")):
        r = client.post(f"/api/admin/keys/{_BOT}/anthropic",
                        json={"key_value": _KEY})
    assert r.status_code == 500
    assert calls == []


def test_add_key_idempotent_no_duplicate_profiles(client):
    """Re-adding the same provider upserts the one canonical profile — no
    duplicate / churned entries across repeated adds."""
    from evolve_admin.web import routes_admin as ra
    store: dict = {}

    def fake_read(bot_id, network_path=None):
        return json.loads(json.dumps(store)) if store else {}

    def fake_write(bot_id, data, network_path=None):
        clean = {k: v for k, v in data.items() if not k.startswith("_")}
        store.clear()
        store.update(json.loads(json.dumps(clean)))
        return True

    with patch.object(ra, "_shared_read_auth_profiles", side_effect=fake_read), \
         patch.object(ra, "_shared_write_auth_profiles", side_effect=fake_write), \
         patch(_IMPORT, return_value=(True, "ok")):
        for _ in range(2):
            r = client.post(f"/api/admin/keys/{_BOT}/anthropic",
                            json={"key_value": _KEY})
            assert r.status_code == 200, r.get_json()

    assert list(store.get("profiles", {}).keys()) == ["anthropic:api_key"]


def test_add_key_never_leaks_key_to_subprocess(client):
    """End-to-end ps-leak guard: drive add-key with a REAL write + REAL import
    trigger (only the OS calls faked) and assert the key never lands on any
    subprocess argv or stdin — it reaches disk only via the /tmp staging file
    that ``sudo /bin/cp`` copies by PATH."""
    from evolve_admin.web import routes_admin as ra
    seen: list = []

    def fake_run(argv, *a, **k):
        seen.append({"argv": list(argv), "input": k.get("input")})
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run), \
         patch.object(ra, "_shared_read_auth_profiles", return_value={}), \
         patch("evolve_admin.deploy._openclaw_bin", return_value=_OC_BIN):
        r = client.post(f"/api/admin/keys/{_BOT}/anthropic",
                        json={"key_value": _KEY})

    assert r.status_code == 200, r.get_json()
    assert seen, "expected the write + import to issue subprocess calls"
    # ps-leak invariant (the point of THIS test): the key reaches disk only via
    # the /tmp staging file that `sudo /bin/cp` copies by PATH — it must never
    # appear on any subprocess argv, and on stdin ONLY for a paste-* fallback.
    # (Here the staged JSON isn't readable through the mocked cp, so the
    # verify-driven helper short-circuits with nothing to import — that the
    # import is WIRED into the route is pinned by
    # test_add_key_triggers_auth_store_import.)
    for c in seen:
        for tok in c["argv"]:
            assert _KEY not in tok, f"key leaked onto argv: {c['argv']}"
        is_paste = "paste-api-key" in c["argv"] or "paste-token" in c["argv"]
        if not is_paste:
            assert _KEY not in str(c.get("input") or ""), "key leaked onto stdin"


# ── api_admin_restart_gateway (strategy B): import BEFORE the kickstart ───────


def _fake_runtime(order: list):
    """A FakeRuntime whose gateway_restart records ordering against the import."""
    from runtime.agent_runtime import FakeRuntime
    fake = FakeRuntime()

    def _restart(bot_id, network_path=None):
        order.append("restart")
        return {"ok": True, "service": f"ai.openclaw.{bot_id}-gateway"}

    fake.gateway_restart = _restart  # type: ignore[method-assign]
    return fake


def test_restart_gateway_imports_before_kickstart(client):
    """The operator restart endpoint reconciles the sqlite store from
    auth-profiles.json BEFORE bouncing the gateway."""
    from runtime.agent_runtime import set_runtime
    order: list = []
    set_runtime(_fake_runtime(order))
    try:
        with patch(_IMPORT,
                   side_effect=lambda *a, **k: order.append("import") or (True, "")):
            r = client.post(f"/api/admin/gateway/{_BOT}/restart",
                            json={"confirm": True})
    finally:
        set_runtime(None)
    assert r.status_code == 200, r.get_json()
    assert order == ["import", "restart"], order


def test_restart_gateway_proceeds_when_import_fails(client):
    """A failed import is best-effort: the gateway still restarts (read-side
    oc_store + the durable on-disk JSON are the backstops)."""
    from runtime.agent_runtime import set_runtime
    order: list = []
    set_runtime(_fake_runtime(order))
    try:
        with patch(_IMPORT,
                   return_value=(False, "auth-store import trigger exit 1")):
            r = client.post(f"/api/admin/gateway/{_BOT}/restart",
                            json={"confirm": True})
    finally:
        set_runtime(None)
    assert r.status_code == 200, r.get_json()
    assert order == ["restart"]


def test_restart_gateway_requires_confirm_skips_import(client):
    """Guard the cheap path: a missing confirm is rejected up front, before any
    import or restart side effect."""
    from runtime.agent_runtime import set_runtime
    order: list = []
    set_runtime(_fake_runtime(order))
    try:
        with patch(_IMPORT,
                   side_effect=lambda *a, **k: order.append("import") or (True, "")):
            r = client.post(f"/api/admin/gateway/{_BOT}/restart", json={})
    finally:
        set_runtime(None)
    assert r.status_code == 400
    assert order == []
