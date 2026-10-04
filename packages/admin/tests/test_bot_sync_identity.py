"""tests/test_bot_sync_identity.py — the synced / outdated decision is based on
MONOTONIC commit identity, never on the non-monotonic version string.

Root cause this pins (the 2026-06-25 incident): ``_compute_version`` builds
``YYYY.MMDD.<PR#>`` and the PR number is assigned at PR *creation*, so a
later-merged PR can carry a *lower* number. When the repo-puller advanced the
tip #3272 → #3269 (a later commit, lower number), the per-bot ``synced ==
(deployed == current)`` string check flipped to False AND the Maintenance badge
offered an "upgrade" to v3269 — a LOWER number than the v3272 already deployed.

The fix bases ``synced`` / ``relation`` on the commit sha (exact identity) and
``commit_count`` (``git rev-list --count HEAD`` — strictly increasing along the
ff-only deploy history), so a bot that is genuinely behind can never be told
"current is a lower number than you have".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy  # noqa: E402


# ── classify_sync — the pure decision ────────────────────────────────────────

CUR_SHA = "f" * 40
CUR_COUNT = 5269


def test_classify_synced_on_same_sha():
    synced, rel = deploy.classify_sync(
        "2026.0625.3269", CUR_SHA, CUR_COUNT, CUR_SHA, CUR_COUNT)
    assert synced is True
    assert rel == "synced"


def test_classify_behind_by_commit_count():
    # Older commit: fewer commits in history → behind, regardless of PR#.
    synced, rel = deploy.classify_sync(
        "2026.0620.3000", "a" * 40, 5200, CUR_SHA, CUR_COUNT)
    assert synced is False
    assert rel == "behind"


def test_classify_non_monotonic_pr_is_still_behind_not_ahead():
    """THE bug: deployed PR# (3272) is HIGHER than current PR# (3269), but the
    deployed commit is OLDER (lower commit_count). Identity must report 'behind'
    — the lexical PR compare would have called it 'ahead'/'newer' and rendered a
    nonsensical downgrade. Proven here: a higher PR# never masks a real lag."""
    synced, rel = deploy.classify_sync(
        "2026.0625.3272", "b" * 40, 5260,   # deployed: higher PR#, FEWER commits
        CUR_SHA, CUR_COUNT)                  # current:  lower PR#, MORE commits
    assert synced is False
    assert rel == "behind", "a higher PR# must not hide that the bot is older"


def test_classify_ahead_when_bot_has_more_commits():
    """Reverse: the bot redeployed onto newer code before the admin daemon
    restarted onto it. The bot is AHEAD, not outdated — must never be flagged
    for a manual 'upgrade'."""
    synced, rel = deploy.classify_sync(
        "2026.0625.3300", "c" * 40, 5300, CUR_SHA, CUR_COUNT)
    assert synced is False
    assert rel == "ahead"


def test_classify_never_deployed():
    synced, rel = deploy.classify_sync(None, None, None, CUR_SHA, CUR_COUNT)
    assert synced is False
    assert rel == "never"


def test_classify_legacy_stamp_same_version_is_synced(monkeypatch):
    """A stamp written before the sha field existed: fall back to version-string
    equality. Equal string → synced."""
    monkeypatch.setattr(deploy, "EVOLVE_VERSION", "2026.0625.3269")
    synced, rel = deploy.classify_sync(
        "2026.0625.3269", None, None, CUR_SHA, CUR_COUNT)
    assert synced is True
    assert rel == "synced"


def test_classify_legacy_stamp_mismatch_is_unknown_not_behind(monkeypatch):
    """Legacy stamp that differs: we have no identity to order by, so the
    direction is 'unknown' — NEVER 'behind'/'outdated'. This is what keeps the
    harsh ⚠+⬆ affordance off a stamp we can't actually compare."""
    monkeypatch.setattr(deploy, "EVOLVE_VERSION", "2026.0625.3269")
    synced, rel = deploy.classify_sync(
        "2026.0625.3272", None, None, CUR_SHA, CUR_COUNT)
    assert synced is False
    assert rel == "unknown"


def test_classify_git_absent_on_admin_falls_back(monkeypatch):
    """If the admin server can't resolve its own sha (no .git), the current_sha
    is empty → fall back to version-string equality, mismatch → unknown."""
    monkeypatch.setattr(deploy, "EVOLVE_VERSION", "2026.0625.3269")
    synced, rel = deploy.classify_sync(
        "2026.0625.3272", "b" * 40, 5260, "", None)
    assert synced is False
    assert rel == "unknown"


def test_classify_divergent_same_count_is_unknown():
    synced, rel = deploy.classify_sync(
        "2026.0625.3269", "a" * 40, CUR_COUNT, CUR_SHA, CUR_COUNT)
    assert synced is False
    assert rel == "unknown"


# ── get_bot_sync_status — integration over install.json ──────────────────────


def _patch_current(monkeypatch, *, sha=CUR_SHA, count=CUR_COUNT,
                   version="2026.0625.3269"):
    monkeypatch.setattr(deploy, "EVOLVE_COMMIT_SHA", sha)
    monkeypatch.setattr(deploy, "EVOLVE_COMMIT_COUNT", count)
    monkeypatch.setattr(deploy, "EVOLVE_VERSION", version)


def test_sync_status_non_monotonic_reports_behind_with_current_higher_count(monkeypatch):
    """End-to-end: a bot stamped at the higher PR# (3272) but an OLDER commit is
    reported behind, and current_version/current_sha point at the truly-newer
    commit — so no surface can render 'outdated to a lower number': the decision
    field is identity, not the version label."""
    _patch_current(monkeypatch)
    network = {"members": ["team_bot_a"]}
    install = {"bot_versions": {"team_bot_a": {
        "version": "2026.0625.3272", "sha": "b" * 40, "commit_count": 5260,
        "deployed_at": "2026-06-24T00:00:00+00:00"}}}
    out = deploy.get_bot_sync_status(network, install)["team_bot_a"]
    assert out["synced"] is False
    assert out["relation"] == "behind"
    assert out["deployed_version"] == "2026.0625.3272"   # display only
    assert out["current_version"] == "2026.0625.3269"    # display only
    assert out["current_sha"] == CUR_SHA
    # The bot really is on the older commit; its count is below current's.
    assert install["bot_versions"]["team_bot_a"]["commit_count"] < CUR_COUNT


def test_sync_status_same_commit_is_synced(monkeypatch):
    _patch_current(monkeypatch)
    network = {"members": ["team_bot_a"]}
    install = {"bot_versions": {"team_bot_a": {
        "version": "2026.0625.3269", "sha": CUR_SHA, "commit_count": CUR_COUNT}}}
    out = deploy.get_bot_sync_status(network, install)["team_bot_a"]
    assert out["synced"] is True
    assert out["relation"] == "synced"


def test_sync_status_never_deployed(monkeypatch):
    _patch_current(monkeypatch)
    out = deploy.get_bot_sync_status({"members": ["fresh"]}, {"bot_versions": {}})
    assert out["fresh"]["synced"] is False
    assert out["fresh"]["relation"] == "never"
    assert out["fresh"]["deployed_version"] is None


def test_sync_status_no_outdated_to_lower_number_invariant(monkeypatch):
    """The invariant the whole change exists to guarantee: whenever a bot is
    reported NOT synced with a known direction of 'behind', the current commit
    is strictly newer (higher commit_count) than the deployed one. There is no
    input under which 'behind' coincides with current being an older commit."""
    _patch_current(monkeypatch)
    # Deployed higher PR#, fewer commits (the trap) → behind, current newer.
    network = {"members": ["b"]}
    install = {"bot_versions": {"b": {
        "version": "2026.0625.9999", "sha": "b" * 40, "commit_count": 5000}}}
    out = deploy.get_bot_sync_status(network, install)["b"]
    assert out["relation"] == "behind"
    assert install["bot_versions"]["b"]["commit_count"] < deploy.EVOLVE_COMMIT_COUNT


# ── deploy_stamp — the write side carries the identity ───────────────────────


def test_deploy_stamp_carries_sha_and_count(monkeypatch):
    monkeypatch.setattr(deploy, "EVOLVE_VERSION", "2026.0625.3269")
    monkeypatch.setattr(deploy, "EVOLVE_COMMIT_SHA", CUR_SHA)
    monkeypatch.setattr(deploy, "EVOLVE_COMMIT_COUNT", CUR_COUNT)
    rec = deploy.deploy_stamp("2026-06-25T00:00:00+00:00")
    assert rec["version"] == "2026.0625.3269"
    assert rec["sha"] == CUR_SHA
    assert rec["commit_count"] == CUR_COUNT
    assert rec["deployed_at"] == "2026-06-25T00:00:00+00:00"


def test_deploy_stamp_omits_identity_when_git_absent(monkeypatch):
    """No git context → stamp degrades to version-only (back-compat shape), and
    a later sync check falls back to string equality rather than crashing."""
    monkeypatch.setattr(deploy, "EVOLVE_VERSION", "2026.0625.0")
    monkeypatch.setattr(deploy, "EVOLVE_COMMIT_SHA", "")
    monkeypatch.setattr(deploy, "EVOLVE_COMMIT_COUNT", None)
    rec = deploy.deploy_stamp()
    assert rec["version"] == "2026.0625.0"
    assert "sha" not in rec
    assert "commit_count" not in rec
    assert "deployed_at" in rec


# ── round-trip: a deploy_stamp written now reads back as synced ──────────────


def test_stamp_then_status_roundtrips_to_synced(monkeypatch):
    _patch_current(monkeypatch)
    stamp = deploy.deploy_stamp()
    install = {"bot_versions": {"team_bot_a": stamp}}
    out = deploy.get_bot_sync_status({"members": ["team_bot_a"]}, install)
    assert out["team_bot_a"]["synced"] is True
    assert out["team_bot_a"]["relation"] == "synced"


# ── fix #2: the two surfaces reconcile + the direct-mode lag window is calm ───

import re  # noqa: E402

_WEB = _ADMIN_DIR / "evolve_admin" / "web"
_OVERVIEW_JS = (_WEB / "static" / "js" / "pages" / "overview.js").read_text(encoding="utf-8")
_INDEX_HTML = (_WEB / "index.html").read_text(encoding="utf-8")


def test_apply_release_status_passes_relation_through():
    """Both surfaces read from the one server-side sync source. The Overview
    needs the identity-based `relation` to route 'ahead' out of the lag count
    and 'behind' to the calm affordance."""
    from evolve_admin import release_manager as rm
    data = {"bots": {"team_bot_a": {"role": "member"}}}
    bot_sync = {"team_bot_a": {"deployed_version": "2026.0625.3272",
                               "synced": False, "relation": "behind"}}
    rm.apply_release_status(
        data, {"pod": {"release": {"mode": "direct"}}}, "/tmp/nope",
        bot_sync, "2026.0625.3269")
    assert data["bots"]["team_bot_a"]["evolve_relation"] == "behind"
    assert data["bots"]["team_bot_a"]["evolve_synced"] is False


def test_overview_direct_mode_behind_never_shows_up_to_date():
    """The reconcile fix: in direct mode a non-zero lag count must classify as
    'Updating…' (rank 10.5), not fall through to 'Up to date'. Without it the
    Overview said up-to-date while Maintenance said outdated (the contradiction
    that surfaces in the non-monotonic case, where `latest` doesn't resolve)."""
    src = _OVERVIEW_JS
    # The new rank fires on direct-mode lag independent of `latest`.
    assert re.search(r"mode\s*!==\s*'canary'\s*&&\s*nBehind", src), \
        "direct-mode nBehind must be classified before the up-to-date fallback"
    assert "lag-redeploying" in src and "Updating…" in src
    # And the up-to-date rank is the LAST return, after the lag rank — so a
    # behind fleet can never reach it.
    up_idx = src.index("'up-to-date'")
    # The final direct-mode lag rank must appear before up-to-date in source.
    lag_idx = src.rindex("'lag-redeploying'")
    assert lag_idx < up_idx


def test_overview_lagstate_excludes_ahead_bots():
    """An 'ahead' bot (newer commit than the admin server) must not inflate the
    behind count — else it would falsely trip the 'updating' banner."""
    assert re.search(r"evolve_relation\s*!==\s*'ahead'", _OVERVIEW_JS)


def test_maintenance_direct_behind_badge_is_calm_no_upgrade():
    """The Maintenance System badge: the direct-mode behind branch shows the
    calm '· updating' badge with NO ⚠ and NO ⬆ (sysmUpgrade) — the puller
    auto-redeploys, and a manual ⬆ would race it AND, in the non-monotonic
    case, point at a lower version number."""
    src = _INDEX_HTML
    # Isolate the direct-mode tail of the evolve-version cell: from the
    # ver==null branch through the end of the if/else chain.
    m = re.search(r"\} else if \(ver == null\) \{.*?\n      \}\n", src, re.S)
    assert m, "direct-mode branch of the System tab version cell not found"
    branch = m.group(0)
    # The behind/updating affordance exists…
    assert "· updating" in branch
    # …and after the `synced` check, the behind/ahead branches must not RENDER a
    # manual ⬆ or the harsh ⚠. Strip comment lines first — the comments
    # deliberately *mention* ⚠/⬆ to explain why they're omitted from the markup.
    behind_part = branch.split("} else if (synced) {", 1)[1]
    code_lines = [ln for ln in behind_part.splitlines()
                  if not ln.lstrip().startswith("//")]
    code = "\n".join(code_lines)
    assert "sysmUpgrade" not in code, \
        "direct-mode behind/ahead must not offer a manual ⬆ — the puller heals it"
    assert "⚠" not in code, "behind/ahead must not show the harsh ⚠ in direct mode"
