"""tests/test_tiers_drift_declaration_writers.py — tier writers declare ``tiers:``.

spec-delta-digest-audit-noise-2026-08-25 D3. ``heal.detect_backup_drift_keys``
namespaces every ``evolve-tiers.json`` diff as ``tiers:<top-level key>`` and
credits only what a writer declared in the audit log's ``oc_keys``. Before this
change no writer in the repo emitted a ``tiers:``-prefixed name — every tier
endpoint declared a bare ``{"agents"}`` (an *openclaw.json* key), the
user-tier-override endpoint declared a bare ``{"tiers"}`` that matches nothing
the detector emits, and the freshness/bulk endpoints declared nothing at all.
Result: an authorized Evolve write was permanently indistinguishable from a
hand edit, on every bot with an ``evolve-tiers.json``.

The writer-side half (the diff that produces ``tiersKeysWritten``, and that a
hand edit still fires) is pinned in
``packages/analyzer/tests/test_tiers_drift_declaration.py``. This file pins the
PLUMBING: each route that writes evolve-tiers.json forwards the writer's report
into its audit entry, and a new writer module can't quietly skip it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# The keys a representative write reports as landed. Every route below sends a
# different payload; the point is not WHICH keys, it is that whatever the
# writer reports reaches the audit entry.
_WRITTEN = ["autoUpgrade", "rungs"]
_EXPECTED = {"tiers:autoUpgrade", "tiers:rungs"}


@pytest.fixture
def app_and_audit(tmp_path, monkeypatch):
    """Flask app whose config-set seam reports ``tiersKeysWritten``, with the
    audit writer captured."""
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "members": ["a_bot"],
        "sharedDir": str(shared),
        "bots": {"a_bot": {"role": "member"}},
        "models": {"autoUpgrade": {"enabled": True}},
    }))

    audit_calls: list[dict] = []

    def _result(bot_id):
        return {
            "ok": True, "bot": bot_id,
            "tiersKeysWritten": list(_WRITTEN),
            "routing": {"backgroundRole": "fast"},
            "cascade": {"enabled": True},
            "userTierOverride": {"defaultTier": "fast"},
        }

    import oc_cli
    monkeypatch.setattr(
        oc_cli, "oc_full_config_set_with_error",
        lambda bot_id, updates, **kw: (_result(bot_id), None),
    )
    monkeypatch.setattr(
        oc_cli, "oc_full_config_set",
        lambda bot_id, updates, **kw: _result(bot_id),
    )
    monkeypatch.setattr(
        srv, "_audit_log_entry",
        lambda action, bot_id, details, oc_keys=None: audit_calls.append({
            "action": action, "oc_keys": set(oc_keys or ()),
        }),
    )

    import primary_bot
    monkeypatch.setattr(
        primary_bot, "materialize_bot_tier_override",
        lambda net, bot_id: {
            "rungs": [{"id": "low-class", "costClass": "low",
                       "models": ["prov_a/m-small"]}],
            "roles": {"fast": "low-class"},
            "roleCaps": {},
        },
    )
    monkeypatch.setattr(primary_bot, "bot_has_custom_tiers", lambda net, b: True)

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, audit_calls


# ``(label, method, path, body)`` — one call per evolve-tiers.json-writing
# route reachable without a live openclaw CLI.
_ROUTES = [
    ("tier_mode", "put", "/api/admin/config/a_bot/tier-mode", {"mode": "custom"}),
    ("cascade", "put", "/api/admin/config/a_bot/cascade", {"enabled": True}),
    ("fallback", "put", "/api/admin/config/a_bot/fallback",
     {"fallbackMode": "tiered"}),
    ("routing", "put", "/api/admin/config/a_bot/routing",
     {"routing": {"backgroundRole": "fast"}}),
    ("user_tier_override", "put",
     "/api/admin/config/a_bot/user-tier-override", {"defaultTier": "fast"}),
    ("auto_upgrade", "put", "/api/models/auto-upgrade",
     {"scope": "a_bot", "enabled": True}),
]


@pytest.mark.parametrize("label,method,path,body", _ROUTES,
                         ids=[r[0] for r in _ROUTES])
def test_route_declares_the_tiers_keys_the_writer_reported(
    app_and_audit, label, method, path, body,
):
    """Each tier-writing route forwards ``tiersKeysWritten`` into ``oc_keys``.

    Without this the write is authorized but undeclared, and heal reports it as
    unexplained drift on every tick, forever — 287 of 292 dispatcher rows on
    the mini in one day.
    """
    app, audit_calls = app_and_audit
    with app.test_client() as c:
        resp = getattr(c, method)(path, json=body)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert audit_calls, f"{label}: the write recorded no audit entry at all"
    declared = set().union(*(a["oc_keys"] for a in audit_calls))
    assert _EXPECTED <= declared, (
        f"{label}: audit entry declared {sorted(declared)} — missing the "
        f"tiers: names the writer reported ({_WRITTEN}). heal namespaces "
        f"evolve-tiers.json drift per key; a bare 'agents' (or a bare 'tiers') "
        f"credits nothing."
    )
    # The openclaw.json side is still declared — a tier write recomputes
    # agents.defaults.model, and dropping that would trade one permanent alarm
    # for another.
    assert "agents" in declared


# ── Source ratchet: a new writer module can't skip the forwarding ───────────

_ADMIN_PKG = _ADMIN_DIR / "evolve_admin"

# Modules that call the config-set seam but legitimately declare no
# ``tiers:`` key. Each entry needs a reason; an unexplained addition here is
# how the namespace silently reverts to unexplainable.
_EXEMPT = {
    # ``evolve-admin models reconcile-catalog`` writes ``{"catalog": ...}``
    # only — an openclaw.json ``agents`` change, never an evolve-tiers.json
    # key — and records no audit entry of its own. (Its openclaw-side
    # declaration gap is real but predates D3 and belongs to the bare-key
    # namespace, not this one.)
    "cli.py",
}

# A real USE of the runtime seam — attribute access (``_rt.full_config_set``,
# whether called inline or bound to a local first) — not a docstring mention of
# the name, which is how ``evo/`` modules that only describe the seam used to
# trip this scan.
_SEAM_CALL = re.compile(r"\.full_config_set(_with_error)?\b")


def _writer_modules() -> list[str]:
    out = []
    for p in sorted(_ADMIN_PKG.rglob("*.py")):
        rel = p.relative_to(_ADMIN_PKG).as_posix()
        if rel in _EXEMPT:
            continue
        if _SEAM_CALL.search(p.read_text(errors="replace")):
            out.append(rel)
    return out


def test_the_ratchet_actually_finds_the_writers():
    """The scan is a substring match; if it silently matched nothing the
    ratchet above would pass vacuously forever. Pin the known writers."""
    found = set(_writer_modules())
    for rel in ("web/routes_admin_config.py", "web/model_tier_update_routes.py",
                "web/routes_easy_setup.py", "provisioning.py", "deploy.py",
                "model_swap_cli.py"):
        assert rel in found, f"{rel} should be detected as a config-set writer"


def test_every_tiers_writer_module_forwards_the_declaration():
    """RATCHET. Every module that writes through the config-set seam must
    reference ``tier_write_oc_keys`` — the one helper that turns the writer's
    report into a ``tiers:<key>`` declaration.

    Coarse by design: it fails on a NEW writer module that never wires the
    declaration, which is precisely the regression that produced today's live
    finding (the freshness/bulk endpoints shipped with no ``oc_keys`` at all).
    A module that genuinely writes nothing into evolve-tiers.json belongs in
    ``_EXEMPT``, with a reason.
    """
    missing = [
        rel for rel in _writer_modules()
        if "tier_write_oc_keys" not in (_ADMIN_PKG / rel).read_text(errors="replace")
    ]
    assert not missing, (
        f"these modules write config through the seam but never declare the "
        f"evolve-tiers.json keys they land: {missing}. Union "
        f"``tier_write_oc_keys(<write result>, {{'agents'}})`` into the audit "
        f"entry's oc_keys, or add the module to _EXEMPT with a reason."
    )


def test_no_writer_declares_the_bare_tiers_key():
    """``oc_keys={"tiers"}`` was a real declaration in the tree that credited
    nothing — heal namespaces per key (``tiers:userTierOverride``), so the bare
    name never matched. Keep it from coming back."""
    offenders = []
    pat = re.compile(r"oc_keys\s*=\s*\{\s*[\"']tiers[\"']\s*\}")
    for p in sorted(_ADMIN_PKG.rglob("*.py")):
        if pat.search(p.read_text(errors="replace")):
            offenders.append(p.relative_to(_ADMIN_PKG).as_posix())
    assert not offenders, (
        f"bare oc_keys={{'tiers'}} credits nothing — heal emits "
        f"'tiers:<key>'. Found in: {offenders}"
    )
