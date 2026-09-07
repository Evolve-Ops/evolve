"""tests/test_gallery_preflight_path_c.py

Coverage for the path-C google_integration requirements check and the
alternatives mechanism in ``preflight_check`` (and its inner
``_check_integration`` / ``_check_one_integration``).

Bug history: before PR 2a, the resolver only understood
``openclaw.json → ...`` check_paths. Any app that declared a path-C
requirement (check_path ``network.json → bots[bot].google_integration``)
returned ``unknown`` from the resolver and never showed up correctly
in the install-modal preflight. PR 2a adds:

  1. Native handling for the path-C check_path shape (this file).
  2. An ``alternatives`` list on each integration requirement; the
     resolver returns satisfied if EITHER the primary check_path
     matches OR any alternative does. Makes legacy gallery apps
     installable on path-C bots without separate manifests.

These tests pin both pieces against regression.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest


_ADMIN = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN))


# ─── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def shared_dir(tmp_path):
    return tmp_path / "shared"


def _network_with_path_c(bot_id: str = "lex", **overrides) -> dict:
    """A network.json dict with a well-formed path-C google_integration
    on the named bot. Overrides apply to the google_integration dict."""
    gi = {
        "mode": "service_account_dwd",
        "workspace_domain": "example-corp.com",
        "subject": f"{bot_id}@example-corp.com",
        "service_account_secret_ref": "google-sa-example-corp",
        "scopes": [
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/calendar",
        ],
    }
    gi.update(overrides)
    return {"bots": {bot_id: {"role": "member", "google_integration": gi}}}


def _path_c_req(**overrides) -> dict:
    """A path-C-style integration requirement, as the spec-gen prompt
    teaches the LLM to emit."""
    req = {
        "id": "google_path_c",
        "display_name": "Google (path-C)",
        "required": True,
        "check_path": "network.json → bots[bot].google_integration",
    }
    req.update(overrides)
    return req


def _legacy_gmail_req(**overrides) -> dict:
    """A legacy Gmail integration requirement, as existing gallery
    apps declare it today."""
    req = {
        "id": "gmail",
        "display_name": "Gmail",
        "required": True,
        "check_path": "openclaw.json → integrations.gmail",
    }
    req.update(overrides)
    return req


def _run_preflight(pkg_reqs: list[dict], bot_id: str, network: dict, shared_dir) -> dict:
    """Invoke preflight_check with the given integration requirements +
    a mocked load_network."""
    from evolve_admin.applications import gallery

    pkg = {"pkg_id": "test-pkg", "requirements": {"integrations": pkg_reqs}}

    # Patch the inner _check_one_integration's lazy `from .. import config`
    # by patching the actual module attribute.
    with patch.object(gallery, "load_gallery_package", return_value=pkg), \
         patch("evolve_admin.config.load_network", return_value=network):
        return gallery.preflight_check("test-pkg", bot_id, shared_dir)


# ─── path-C check ────────────────────────────────────────────────────────────


def test_path_c_satisfied_when_block_well_formed(shared_dir):
    result = _run_preflight(
        [_path_c_req()], "lex", _network_with_path_c("lex"), shared_dir
    )
    r = result["requirements"][0]
    assert r["state"] == "satisfied", r["message"]
    assert "service-account" in r["message"]
    assert "lex@example-corp.com" in r["message"]


def test_path_c_missing_when_bot_absent_from_network(shared_dir):
    result = _run_preflight([_path_c_req()], "ghost", {"bots": {}}, shared_dir)
    r = result["requirements"][0]
    assert r["state"] == "missing"
    assert "no entry in network.json" in r["message"]


def test_path_c_missing_when_no_google_integration_block(shared_dir):
    result = _run_preflight(
        [_path_c_req()], "lex", {"bots": {"lex": {"role": "member"}}}, shared_dir,
    )
    r = result["requirements"][0]
    assert r["state"] == "missing"
    assert "no 'google_integration' block" in r["message"]


def test_path_c_missing_when_wrong_mode(shared_dir):
    network = _network_with_path_c("lex", mode="workspace_user_oauth")
    result = _run_preflight([_path_c_req()], "lex", network, shared_dir)
    r = result["requirements"][0]
    assert r["state"] == "missing"
    assert "service_account_dwd" in r["message"]


def test_path_c_missing_when_subject_missing(shared_dir):
    network = _network_with_path_c("lex", subject=None)
    # Remove the key entirely
    del network["bots"]["lex"]["google_integration"]["subject"]
    result = _run_preflight([_path_c_req()], "lex", network, shared_dir)
    r = result["requirements"][0]
    assert r["state"] == "missing"
    assert "subject" in r["message"]


def test_path_c_missing_when_secret_ref_missing(shared_dir):
    network = _network_with_path_c("lex")
    del network["bots"]["lex"]["google_integration"]["service_account_secret_ref"]
    result = _run_preflight([_path_c_req()], "lex", network, shared_dir)
    r = result["requirements"][0]
    assert r["state"] == "missing"
    assert "service_account_secret_ref" in r["message"]


def test_path_c_missing_when_scopes_empty(shared_dir):
    network = _network_with_path_c("lex", scopes=[])
    result = _run_preflight([_path_c_req()], "lex", network, shared_dir)
    r = result["requirements"][0]
    assert r["state"] == "missing"
    assert "scopes" in r["message"].lower()


def test_path_c_required_scopes_satisfied_when_all_present(shared_dir):
    req = _path_c_req(
        required_scopes=[
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/drive.file",
        ]
    )
    result = _run_preflight([req], "lex", _network_with_path_c("lex"), shared_dir)
    assert result["requirements"][0]["state"] == "satisfied"


def test_path_c_required_scopes_missing_when_subset_unmet(shared_dir):
    req = _path_c_req(
        required_scopes=[
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/drive",  # full drive — bot only has drive.file
        ]
    )
    result = _run_preflight([req], "lex", _network_with_path_c("lex"), shared_dir)
    r = result["requirements"][0]
    assert r["state"] == "missing"
    assert "drive" in r["message"]


# ─── alternatives mechanism ──────────────────────────────────────────────────


def test_alternatives_primary_satisfied_means_alternatives_ignored(shared_dir):
    """If the primary check passes, the resolver doesn't consult alternatives.
    This matters because some bots may satisfy BOTH paths; we shouldn't
    bias the message toward one or the other."""
    network = _network_with_path_c("lex")
    # Also configure the legacy gmail integration in openclaw.json — but
    # the resolver shouldn't reach openclaw.json since path-C primary will
    # satisfy first. Use path-C as primary here.
    result = _run_preflight([_path_c_req()], "lex", network, shared_dir)
    r = result["requirements"][0]
    assert r["state"] == "satisfied"
    # No "via alternative" prefix — primary satisfied directly.
    assert "via alternative" not in r["message"]


def test_alternatives_primary_missing_alternative_satisfies(shared_dir):
    """Legacy primary (no openclaw.json gmail) + path-C alternative on a
    path-C-configured bot → satisfied via alternative."""
    legacy = _legacy_gmail_req()
    legacy["alternatives"] = [_path_c_req(id="google_path_c")]
    result = _run_preflight([legacy], "lex", _network_with_path_c("lex"), shared_dir)
    r = result["requirements"][0]
    assert r["state"] == "satisfied"
    assert "via alternative" in r["message"]
    assert "google_path_c" in r["message"]


def test_alternatives_all_missing_returns_missing_with_alts_named(shared_dir):
    """Primary missing + alternatives also missing → missing, with the
    alternative IDs surfaced in the message so the operator sees what
    auth paths the app would have accepted."""
    legacy = _legacy_gmail_req()
    legacy["alternatives"] = [_path_c_req(id="google_path_c")]
    # network has no path-C for this bot AND no openclaw.json gmail.
    result = _run_preflight([legacy], "ghost", {"bots": {}}, shared_dir)
    r = result["requirements"][0]
    assert r["state"] == "missing"
    # Resolver must surface the alternative IDs so the operator knows
    # what other auth paths would have worked.
    assert "alternatives" in r["message"].lower()
    assert "google_path_c" in r["message"]


def test_no_alternatives_field_preserves_legacy_behavior(shared_dir):
    """Existing manifests don't have an 'alternatives' field. Resolver
    must treat that identically to 'alternatives: []' — no behavior
    change for legacy manifests."""
    legacy = _legacy_gmail_req()
    # No 'alternatives' key at all.
    assert "alternatives" not in legacy
    result = _run_preflight([legacy], "ghost", {"bots": {}}, shared_dir)
    r = result["requirements"][0]
    assert r["state"] == "missing"
    # Message should NOT mention alternatives at all when none were declared.
    assert "alternative" not in r["message"].lower()


# ─── gallery manifest migration: legacy Google apps declare path-C alt ───────

@pytest.mark.parametrize("manifest_rel,integration_id", [
    ("gallery/email-integration/p-341576fa.json", "gmail"),
    ("gallery/email-triage/p-41e4c5f4.json", "gmail"),
    ("gallery/calendar-sync/p-fe9acef3.json", "google_calendar"),
    ("gallery/pre-meeting-brief/p-2c7a9b6e.json", "google_calendar"),
])
def test_legacy_gallery_apps_declare_path_c_alternative(manifest_rel, integration_id):
    """Regression guard: the Google-using gallery apps must each declare a
    path-C google_integration alternative on their primary Google integration
    requirement. Without this, those apps fail preflight on path-C-only bots
    (like the travel-concierge bot) even though path C satisfies their actual
    Google needs.

    PR 2a originally added the alternatives to four legacy specs (email-integration,
    email-triage, calendar-sync, ea-pack). The EA Pack rewrite to a meta-spec
    (2026-06-05) moved its google_calendar dependency to Pre-Meeting Brief —
    which inherits the same path-C alternative requirement. Future edits to
    any spec on this list must preserve the path-C alternative; this test
    fails if any drop it."""
    import json

    _REPO_ROOT = Path(__file__).resolve().parents[3]
    path = _REPO_ROOT / manifest_rel
    data = json.loads(path.read_text())

    integrations = data.get("requirements", {}).get("integrations", [])
    matching = [i for i in integrations if i.get("id") == integration_id]
    assert matching, (
        f"{manifest_rel} no longer declares an '{integration_id}' integration "
        f"requirement at all. If this is intentional, update the test."
    )
    integ = matching[0]
    alternatives = integ.get("alternatives") or []
    assert alternatives, (
        f"{manifest_rel}: integration '{integration_id}' must declare a "
        f"path-C 'alternatives' entry so the app installs on bots that use "
        f"the service-account + DwD auth path. PR 2a added it; do not drop."
    )
    path_c_alts = [a for a in alternatives if a.get("id") == "google_path_c"]
    assert path_c_alts, (
        f"{manifest_rel}: 'alternatives' is set but no entry with "
        f"id='google_path_c' is present. The path-C alternative is the "
        f"only one this PR added; if you renamed it, also update this test "
        f"and the resolver in applications/gallery.py::_check_one_integration."
    )
    alt = path_c_alts[0]
    assert alt.get("check_path") == "network.json → bots[bot].google_integration", (
        f"{manifest_rel}: path-C alternative's check_path is {alt.get('check_path')!r}, "
        f"expected 'network.json → bots[bot].google_integration'. The resolver only "
        f"recognizes that exact path; a typo here silently disables the alternative."
    )
    assert alt.get("required_scopes"), (
        f"{manifest_rel}: path-C alternative must declare required_scopes so the "
        f"resolver can verify the bot's google_integration.scopes includes them."
    )
