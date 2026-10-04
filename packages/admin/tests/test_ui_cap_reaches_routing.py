"""tests/test_ui_cap_reaches_routing.py — the cap set on the AI Optimization
page is the cap the gateway enforces.

The twin of ``test_models_cap_reaches_routing`` for the OTHER surface.
``evolve-admin models cap`` was fixed on 2026-09-10 (PR #4023 + its Hold);
``PUT /api/admin/config/<bot>/user-tier-override`` was deliberately left
behind in that change because the mirror question was unsettled, and the spec
addendum named it as still broken. It writes the same block for the same
reason, so it now goes through the same door.

What each half of the defect looked like from an operator's seat:

* **routing** — set a cap on the page, get a 200, and route on the code
  default of 10 forever, because the gateway reads
  ``roleCaps.power.maxPerDayPerBot`` and ``DEFAULT_MODEL_CATALOG`` ships that
  key in code (``power_cap`` carries the measurement);
* **the admin chat Power gate** — set ``{enabled: false, dailyCap: 0}`` on the
  page and still get a working Power chip in admin chat, because
  ``home_chat_routes._read_user_tier_override`` reads only
  ``{sharedDir}/{bot}/tiers.json`` and the endpoint wrote no mirror.

Both fail against 722ff178 (the CLI-only fix).

The pod fixture is the one the CLI suites drive: the ``sudo -u <bot> python3
oc_model.py`` shell-outs are redirected to IN-PROCESS calls of the very
functions they run, so the file the endpoint writes is a real
``evolve-tiers.json`` with real merge semantics, and the resolver reads it
back off disk rather than out of a stub's return value.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ._tier_pod import (  # noqa: E402 — must follow the sys.path bootstrap
    BOT,
    make_pod,
    mirror as _mirror,
    tiers_file as _tiers_file,
)

_URL = f"/api/admin/config/{BOT}/user-tier-override"


@pytest.fixture
def pod(tmp_path, monkeypatch):
    """The CLI suites' pod, plus the admin-server audit seam stubbed.

    ``make_pod`` already captures ``provisioning._record_audit`` (the CLI's
    declaration). The endpoint declares through ``server._audit_log_entry``
    instead, which appends to the REAL operator log at the canonical shared
    dir — a path that exists on a maintainer's Mac. Capture it here for the
    same reason.
    """
    import evolve_admin.web.server as srv

    p = make_pod(tmp_path, monkeypatch)
    p["ui_audits"] = []
    monkeypatch.setattr(
        srv, "_audit_log_entry",
        lambda action, bot_id, details, oc_keys=None: p["ui_audits"].append(
            (action, bot_id, details, oc_keys),
        ),
    )
    return p


@pytest.fixture
def client(pod):
    from evolve_admin.web.server import create_app

    app = create_app(pod["network_path"])
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _put(client, body: dict):
    return client.put(_URL, json=body)


def _effective_cap(pod) -> int:
    """The cap the gateway would enforce, from the bot's real config on disk.

    Goes through the ONE Python resolver (``power_cap``), the twin of
    ``ModelRouter._roleCap`` — not a hand-walk of the JSON here, which is how
    a test ends up agreeing with a writer that both readers disagree with.
    """
    from evolve_admin.power_cap import resolve_effective_power_cap

    return resolve_effective_power_cap(
        json.loads(pod["network_path"].read_text()), _tiers_file(pod),
    )


def _seed_role_caps(pod, caps: dict) -> None:
    """Give the bot a canonical ``roleCaps`` block before the request.

    The migrated-pod shape: ``migrate_model_roles`` lifted every bot's
    ``dailyCap`` into ``roleCaps.power.maxPerDayPerBot`` fleet-wide on
    2026-08-15, and "Customize this bot" materializes one too.
    """
    path = pod["home"] / ".openclaw" / "evolve-tiers.json"
    doc = json.loads(path.read_text()) if path.exists() else {}
    doc["roleCaps"] = caps
    path.write_text(json.dumps(doc, indent=2))


# ── the routing claim, on both config shapes ─────────────────────────────────

def test_the_cap_reaches_routing_on_a_config_that_already_has_roleCaps(pod, client):
    """The case the chip names: a bot whose ``roleCaps`` block already exists.

    Against 722ff178 the seeded 25 stands and the operator's 3 — accepted with
    a 200 — is ignored entirely, because the endpoint wrote only the legacy
    key that block shadows.
    """
    _seed_role_caps(pod, {"power": {"maxPerDayPerBot": 25}})
    assert _put(client, {"dailyCap": 3}).status_code == 200
    assert _effective_cap(pod) == 3


def test_the_cap_reaches_routing_on_a_config_with_no_roleCaps_block(pod, client):
    """The un-migrated shape. Still broken against 722ff178: the router folds
    ``DEFAULT_MODEL_CATALOG`` in as its base layer, so the merged block is
    non-empty for every bot and resolves to the code default of 10, not 3."""
    assert _put(client, {"dailyCap": 3}).status_code == 200
    assert _effective_cap(pod) == 3


def test_zero_reaches_routing_as_zero(pod, client):
    """``dailyCap: 0`` is the "stop Power turns" sentinel. Resolving it as a
    missing value would read as 10/day — the opposite of what was asked."""
    assert _put(client, {"dailyCap": 0}).status_code == 200
    assert _effective_cap(pod) == 0


def test_the_response_reports_what_the_gateway_will_enforce(pod, client):
    """Not the number the caller sent. Echoing that back is exactly what both
    surfaces did while routing used a different one, so the page has no way to
    show the operator a cap that is merely what they typed."""
    body = _put(client, {"dailyCap": 3}).get_json()
    assert body["effectivePowerCap"] == 3
    assert body["userTierOverride"]["dailyCap"] == 3


def test_the_reported_cap_is_read_back_not_echoed(pod, client, monkeypatch):
    """The permanent detector for this bug class, the twin of the CLI's.

    Nothing in a well-formed config can make the two diverge (the bot layer
    wins the merge and the endpoint range-checks the value first), so the
    resolver is stubbed to force one. What is pinned is that the endpoint
    READS the answer rather than asserting it.
    """
    from evolve_admin import power_cap

    monkeypatch.setattr(
        power_cap, "resolve_effective_power_cap", lambda network, doc: 9,
    )
    assert _put(client, {"dailyCap": 3}).get_json()["effectivePowerCap"] == 9


def test_an_unresolvable_cap_is_reported_as_unknown_not_as_a_failed_write(
    pod, client, monkeypatch,
):
    """The write has already landed by then; a resolver that cannot run must
    not turn it into a 500. ``null`` means unknown — never 0, which is the
    "Power disabled" value and would read as the opposite of the truth."""
    from evolve_admin import power_cap

    def _boom(network, doc):
        raise RuntimeError("primary_bot is not importable here")

    monkeypatch.setattr(power_cap, "resolve_effective_power_cap", _boom)
    resp = _put(client, {"dailyCap": 3})
    assert resp.status_code == 200
    assert resp.get_json()["effectivePowerCap"] is None
    assert _tiers_file(pod)["userTierOverride"]["dailyCap"] == 3


# ── the read-merge, and the other keys that must survive it ──────────────────

def test_setting_the_power_cap_does_not_drop_the_max_cap(pod, client):
    """``roleCaps`` is a WHOLESALE replace at the write seam. A non-merging
    writer would delete ``max`` here — raising this bot's Fable ceiling from 2
    back to the product default as a side effect of LOWERING its Opus cap."""
    _seed_role_caps(pod, {
        "power": {"maxPerDayPerBot": 25}, "max": {"maxPerDayPerBot": 2},
    })
    assert _put(client, {"dailyCap": 3}).status_code == 200
    assert _tiers_file(pod)["roleCaps"]["max"] == {"maxPerDayPerBot": 2}


def test_a_cap_that_cannot_read_the_current_config_refuses_to_write(pod, client, monkeypatch):
    """Fail closed, and fail closed on BOTH keys. A degraded read looks like
    "this bot has no roleCaps"; merging onto that would replace the block."""
    import oc_cli

    _seed_role_caps(pod, {"max": {"maxPerDayPerBot": 2}})
    monkeypatch.setattr(
        oc_cli, "oc_full_config_get", lambda bot_id, network_path=None: None,
    )
    resp = _put(client, {"dailyCap": 3})
    assert resp.status_code == 500
    assert "refusing to replace it" in resp.get_json()["error"]
    assert _tiers_file(pod)["roleCaps"] == {"max": {"maxPerDayPerBot": 2}}
    assert "userTierOverride" not in _tiers_file(pod)


def test_a_non_cap_write_does_not_touch_roleCaps(pod, client):
    """The coupling is keyed on ``dailyCap``, not on the endpoint. A
    defaultTier write — the only thing the page sends today — must not drag a
    ``roleCaps`` replace along with it."""
    _seed_role_caps(pod, {"max": {"maxPerDayPerBot": 2}})
    assert _put(client, {"defaultTier": "standard"}).status_code == 200
    assert _tiers_file(pod)["roleCaps"] == {"max": {"maxPerDayPerBot": 2}}


# ── the mirror question, settled ─────────────────────────────────────────────

def test_the_write_is_mirrored_for_the_admin_chat_power_gate(pod, client):
    """``home_chat_routes._read_user_tier_override`` reads
    ``{sharedDir}/{bot}/tiers.json`` and nothing else. Until this endpoint
    mirrored, an opt-out set on the page left the Power chip working."""
    assert _put(client, {"enabled": False, "dailyCap": 0}).status_code == 200
    assert _mirror(pod)["userTierOverride"] == {"enabled": False, "dailyCap": 0}


def test_the_mirror_is_what_the_chat_gate_actually_reads(pod, client):
    """Through the real reader, not by re-parsing the file here — the mirror
    is only worth writing if that function returns the operator's value, and
    its uid-trust gate could have clamped it (the admin server writes the
    mirror as its own uid, which is the reader's own uid, so it does not)."""
    from evolve_admin.web.home_chat_routes import _read_user_tier_override

    _put(client, {"enabled": False, "dailyCap": 0})
    assert _read_user_tier_override(pod["shared"], BOT) == {
        "enabled": False, "dailyCap": 0,
    }


# ── heal drift accounting ────────────────────────────────────────────────────

def test_the_write_is_declared_under_this_surface_s_own_action(pod, client):
    """Two surfaces, two action names: an operator reading the audit log can
    tell a page click from a CLI run. The shared writer therefore declares
    nothing for this caller (``audit_action=None``) — exactly one entry, and
    it is this one."""
    _put(client, {"dailyCap": 3})
    assert pod["audits"] == []
    assert len(pod["ui_audits"]) == 1
    action, bot_id, details, _ = pod["ui_audits"][0]
    assert action == "config.user_tier_override.set"
    assert bot_id == BOT
    assert details == {"dailyCap": 3}


def test_the_coupled_key_is_declared_too(pod, client):
    """evolve-tiers.json drift is namespaced ``tiers:<key>``. The write now
    lands ``roleCaps`` as well, so an undeclared ``tiers:roleCaps`` would show
    up as an unexplained hand edit on heal's next cycle — forever."""
    _put(client, {"dailyCap": 3})
    oc_keys = pod["ui_audits"][0][3] or set()
    assert {"tiers:userTierOverride", "tiers:roleCaps"} <= oc_keys
