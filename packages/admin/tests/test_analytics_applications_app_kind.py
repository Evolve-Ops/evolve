"""Route test for /api/analytics/applications app_kind pass-through (D2).

The purpose/fit classifier (PR #2899) stamps every manifest with
``app_kind: "application" | "capability"``. The Apps grid filters capabilities
off the headline grid on this field (apps.js renderCapabilities), so the route
that feeds the page MUST surface it. This test pins the contract:

  - a manifest with no app_kind (legacy / un-classified) → "application"
    (the inert default — the over-route direction, hiding a real app, never
    happens by accident);
  - a manifest explicitly labeled "capability" → "capability";
  - a v7-arc Instance carrying the label survives hydrate → "capability".

Reuses the Flask-test-client + monkeypatched-manifest-list pattern; manifests
are written to tmp files and _list_manifests_as_bot is patched to return them,
so the test never touches a real bot home.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.web import routes_analytics  # noqa: E402
from evolve_admin.web.server import _register_analytics_routes  # noqa: E402

BOT = "admin_bot"


@pytest.fixture
def shared(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


@pytest.fixture
def app(tmp_path: Path, shared: Path):
    network_path = tmp_path / "network.json"
    network_path.write_text(
        json.dumps({
            "sharedDir": str(shared),
            "primary": "evolve",
            "members": [BOT],
        })
    )
    a = Flask(__name__)
    _register_analytics_routes(a, network_path)
    a.config["TESTING"] = True
    a.shared_dir = shared
    return a


def _write(dirpath: Path, name: str, manifest: dict) -> str:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / f"{name}.json"
    p.write_text(json.dumps(manifest))
    return str(p)


def _by_id(body: dict) -> dict:
    return {c["id"]: c for c in body[BOT]}


def test_app_kind_passthrough_legacy_explicit_and_v7arc(app, shared, tmp_path, monkeypatch):
    manifests_dir = tmp_path / "manifests"

    # 1. Legacy manifest — no app_kind field at all. Must default to application.
    legacy = _write(manifests_dir, "legacy-app", {
        "id": "legacy-app",
        "name": "Daily Digest",
        "description": "Sends a daily summary",
        "schema_version": 13,
    })

    # 2. Explicitly classified capability (a reusable skill).
    cap = _write(manifests_dir, "google-api", {
        "id": "google-api",
        "name": "Google Services Integration",
        "description": "Authenticated access to Google APIs",
        "schema_version": 25,
        "app_kind": "capability",
    })

    # 3. Explicitly classified application.
    appk = _write(manifests_dir, "comms-hub", {
        "id": "comms-hub",
        "name": "Communication Hub",
        "description": "Routes inbound messages",
        "schema_version": 25,
        "app_kind": "application",
    })

    # 4. v7-arc Instance carrying app_kind on the Instance — must survive
    #    hydrate (which overlays the bound Spec's presentation fields).
    spec_id = "skill-spec"
    spec_version = "1.0.0"
    _write(
        shared / "gallery" / "local" / spec_id,
        spec_version,
        {"name": "Skill From Spec", "description": "objective from spec",
         "objective": {"primary": "objective from spec"}},
    )
    v7 = _write(manifests_dir, "inst-1", {
        "instance_id": "inst-1",
        "manifest_shape": "v7-arc",
        "schema_version": 25,
        "app_kind": "capability",
        "provenance": {"spec_id": spec_id, "spec_version": spec_version},
        "realized_files": [],
    })

    monkeypatch.setattr(
        routes_analytics, "_list_manifests_as_bot",
        lambda bot, user=None: [legacy, cap, appk, v7],
    )

    with app.test_client() as c:
        resp = c.get(f"/api/analytics/applications?bot={BOT}")
    assert resp.status_code == 200
    caps = _by_id(resp.get_json())

    # Every entry carries an app_kind — the field is always present so the
    # client never has to guess.
    assert all("app_kind" in c for c in caps.values())

    # Inert default: a manifest with no app_kind stays an application.
    assert caps["legacy-app"]["app_kind"] == "application"
    # Explicit labels pass through verbatim.
    assert caps["comms-hub"]["app_kind"] == "application"
    assert caps["google-api"]["app_kind"] == "capability"
    # v7-arc Instance label survives hydrate.
    assert caps["inst-1"]["app_kind"] == "capability"
    # Hydrate still overlaid the Spec's presentation fields.
    assert caps["inst-1"]["name"] == "Skill From Spec"


# ── Canonical identity source: tile `description` ⟵ identity.purpose ─────────
# The Apps tile renders the route's `description` field; the detail/Edit modal
# (apps.js viewManifest) renders `identity.purpose`. They MUST resolve to one
# canonical string. The route prefers `identity.purpose` FIRST, falling back to
# the legacy top-level `description` only when no purpose exists. Regression for
# the atlas-article-capture divergence (a contaminated top-level `description`
# — "Member Management" residue from the #3095/#3108 conflation incident —
# rendered on the tile while the detail showed the correct article-capture
# purpose).


def test_tile_description_prefers_identity_purpose(app, tmp_path, monkeypatch):
    manifests_dir = tmp_path / "manifests"

    # 1. Both present + divergent (the atlas shape): tile must show purpose,
    #    NOT the stale/contaminated top-level description.
    contaminated = _write(manifests_dir, "atlas-article-capture", {
        "id": "atlas-article-capture",
        "name": "Atlas Article Capture",
        "description": "Member Management is a privacy-preserving system for "
                       "maintaining member records with cryptographic hashing.",
        "identity": {"purpose": "Surface ecosystem developments to an enthusiast "
                                 "community in a single daily message."},
    })

    # 2. Only top-level description (no identity) → legacy fallback.
    legacy = _write(manifests_dir, "legacy-only-desc", {
        "id": "legacy-only-desc",
        "name": "Legacy",
        "description": "Sends a daily summary",
    })

    # 3. identity present but purpose empty → fall back to description, not "".
    empty_purpose = _write(manifests_dir, "empty-purpose", {
        "id": "empty-purpose",
        "name": "Empty Purpose",
        "description": "Real description text",
        "identity": {"purpose": ""},
    })

    monkeypatch.setattr(
        routes_analytics, "_list_manifests_as_bot",
        lambda bot, user=None: [contaminated, legacy, empty_purpose],
    )
    with app.test_client() as c:
        resp = c.get(f"/api/analytics/applications?bot={BOT}")
    assert resp.status_code == 200
    caps = _by_id(resp.get_json())

    # Canonical: tile description == identity.purpose, NOT the contaminated text.
    assert caps["atlas-article-capture"]["description"] == (
        "Surface ecosystem developments to an enthusiast community in a "
        "single daily message."
    )
    assert "Member Management" not in caps["atlas-article-capture"]["description"]
    # Legacy fallback intact: no identity.purpose → top-level description.
    assert caps["legacy-only-desc"]["description"] == "Sends a daily summary"
    # Empty purpose falls through to description (not a blank tile).
    assert caps["empty-purpose"]["description"] == "Real description text"


def test_unrecognized_app_kind_is_not_dropped_from_the_grid(app, tmp_path, monkeypatch):
    """A garbage/unknown app_kind must NOT route a manifest off the grid.

    The client filter keys on app_kind == "capability" exactly; anything else
    stays an application. The route preserves whatever value is on disk, so the
    safe (never-hide-a-real-app) direction holds for unexpected values too.
    """
    manifests_dir = tmp_path / "manifests"
    weird = _write(manifests_dir, "weird", {
        "id": "weird",
        "name": "Weird",
        "app_kind": "service",  # not in the vocabulary
    })
    monkeypatch.setattr(
        routes_analytics, "_list_manifests_as_bot",
        lambda bot, user=None: [weird],
    )
    with app.test_client() as c:
        resp = c.get(f"/api/analytics/applications?bot={BOT}")
    caps = _by_id(resp.get_json())
    # Preserved verbatim; the client treats anything != "capability" as an app.
    assert caps["weird"]["app_kind"] == "service"
    assert caps["weird"]["app_kind"] != "capability"


# ── Agent-freelance "migrate to plugin_intercept" nudge pass-through ─────────
# spec-agent-freelance-bypass-phase2-2026-06-06. The route runs
# validate_bot_guidance per manifest and attaches a `freelance` summary ONLY
# for at-risk-shaped manifests still in agent_invokes mode (severity
# "warning"). apps.js renders a "migrate to plugin_intercept" chip off it.


def _at_risk_bot_guidance() -> list:
    return [
        {
            "section": "On-Demand Research",
            "content": (
                "When triggered, run exactly: python3 scripts/atlas_research.py "
                "/tmp/req.json. The script's reply is the response. Do NOT "
                "freelance with your general tools if the invocation fails."
            ),
        }
    ]


def test_freelance_warning_attached_for_at_risk_agent_invokes(app, tmp_path, monkeypatch):
    """An at-risk-shaped manifest in agent_invokes mode with no triggers gets a
    `freelance` summary at severity "warning" — the dashboard nudge source."""
    manifests_dir = tmp_path / "manifests"
    atrisk = _write(manifests_dir, "atlas-research", {
        "id": "atlas-research",
        "name": "Atlas Research",
        "bot_guidance": _at_risk_bot_guidance(),
        # agent_invokes is the default; leave invocation_mode unset to prove
        # the default path is what trips the warning.
    })
    monkeypatch.setattr(
        routes_analytics, "_list_manifests_as_bot",
        lambda bot, user=None: [atrisk],
    )
    with app.test_client() as c:
        resp = c.get(f"/api/analytics/applications?bot={BOT}")
    assert resp.status_code == 200
    f = _by_id(resp.get_json())["atlas-research"]["freelance"]
    assert f is not None
    assert f["severity"] == "warning"
    assert f["is_at_risk"] is True
    assert f["invocation_mode"] == "agent_invokes"
    assert "plugin_intercept" in f["message"]


def test_freelance_none_for_clean_manifest(app, tmp_path, monkeypatch):
    """A manifest with no at-risk-shaped prose carries `freelance: None` — no
    chip, no payload bloat."""
    manifests_dir = tmp_path / "manifests"
    clean = _write(manifests_dir, "daily-digest", {
        "id": "daily-digest",
        "name": "Daily Digest",
        "bot_guidance": [
            {"section": "Setup", "content": "Run the cron job at 9am daily."}
        ],
    })
    monkeypatch.setattr(
        routes_analytics, "_list_manifests_as_bot",
        lambda bot, user=None: [clean],
    )
    with app.test_client() as c:
        resp = c.get(f"/api/analytics/applications?bot={BOT}")
    caps = _by_id(resp.get_json())
    assert "freelance" in caps["daily-digest"]
    assert caps["daily-digest"]["freelance"] is None


def test_freelance_can_migrate_for_structured_known_protocol_triggers(app, tmp_path, monkeypatch):
    """An agent_invokes app whose structured triggers can be wired to
    plugin_intercept (resolvable script + registered protocol) gets
    `freelance.can_migrate: True` — the case where Make-reliable actually
    works. The no-trigger warning case (above) stays can_migrate: False."""
    manifests_dir = tmp_path / "manifests"
    mig = _write(manifests_dir, "atlas-mig", {
        "id": "atlas-mig",
        "name": "Atlas Migratable",
        "invocation_mode": "agent_invokes",
        "blueprint": {"files": [
            {"logical_name": "atlas_research",
             "expected_location": "scripts/atlas_research.py"}
        ]},
        "event_triggers": [{
            "id": "at_mention",
            "source": "telegram",
            "audience": "members",
            "invokes": "atlas_research",
            "match": {"channel": "telegram_group",
                      "pattern": r"@[A-Za-z][A-Za-z0-9_]{2,}\b"},
        }],
    })
    monkeypatch.setattr(
        routes_analytics, "_list_manifests_as_bot",
        lambda bot, user=None: [mig],
    )
    with app.test_client() as c:
        resp = c.get(f"/api/analytics/applications?bot={BOT}")
    f = _by_id(resp.get_json())["atlas-mig"]["freelance"]
    assert f is not None
    assert f["can_migrate"] is True
    assert f["invocation_mode"] == "agent_invokes"
    # severity stays the validator's honest value — "info" for a structured
    # (trigger_count > 0) app, NOT a fabricated "warning".
    assert f["severity"] == "info"


def test_freelance_none_for_plain_plugin_intercept(app, tmp_path, monkeypatch):
    """An app already in plugin_intercept mode carries `freelance: None` — no
    chip, nothing to migrate."""
    manifests_dir = tmp_path / "manifests"
    done = _write(manifests_dir, "atlas-done", {
        "id": "atlas-done",
        "name": "Atlas Done",
        "invocation_mode": "plugin_intercept",
        "event_triggers": [{
            "id": "at_mention",
            "source": "telegram",
            "audience": "members",
            "invokes": "atlas_research",
            "match": {"channel": "telegram_group",
                      "pattern": r"@[A-Za-z][A-Za-z0-9_]{2,}\b"},
            "invocation": {
                "script": "scripts/atlas_research.py",
                "stdout_protocol": "atlas_research",
                "on_failure": "silent",
            },
        }],
    })
    monkeypatch.setattr(
        routes_analytics, "_list_manifests_as_bot",
        lambda bot, user=None: [done],
    )
    with app.test_client() as c:
        resp = c.get(f"/api/analytics/applications?bot={BOT}")
    caps = _by_id(resp.get_json())
    assert caps["atlas-done"]["freelance"] is None


def test_freelance_none_for_plugin_intercept(app, tmp_path, monkeypatch):
    """An at-risk-shaped manifest that already opted into plugin_intercept (with
    a valid trigger) is the safe mode — no nudge."""
    manifests_dir = tmp_path / "manifests"
    migrated = _write(manifests_dir, "atlas-migrated", {
        "id": "atlas-migrated",
        "name": "Atlas Migrated",
        "bot_guidance": _at_risk_bot_guidance(),
        "invocation_mode": "plugin_intercept",
        "event_triggers": [{
            "id": "at_mention",
            "source": "telegram",
            "audience": "members",
            "invokes": "atlas_research",
            "match": {"channel": "telegram_group", "pattern": r"@\w+"},
            "invocation": {
                "script": "scripts/atlas_research.py",
                "stdout_protocol": "atlas_research",
                "on_failure": "silent",
            },
        }],
    })
    monkeypatch.setattr(
        routes_analytics, "_list_manifests_as_bot",
        lambda bot, user=None: [migrated],
    )
    with app.test_client() as c:
        resp = c.get(f"/api/analytics/applications?bot={BOT}")
    caps = _by_id(resp.get_json())
    assert caps["atlas-migrated"]["freelance"] is None
