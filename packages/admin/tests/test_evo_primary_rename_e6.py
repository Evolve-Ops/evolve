"""Phase E.6 regression tests — the evolve→evo primary-bot-id completion.

See docs/spec-evo-account-separation-2026-05-25.md §"Phase E.6". The primary
bot's id is ``evo`` (there is no ``evolve`` BOT, only the ``evolve`` service
USER). These tests pin the behaviors that were split-brained and caused a
production gateway crash-loop + monitor blindness:

  - Item 3: pod-report member lists exclude the RESOLVED primary, not the
    literal "evolve" (which leaked the primary into member lists on an evo pod).
  - Item 2: exec-policy primary detection derives from the resolved
    primary_bot_id, not a hardcoded ``bot_id == "evolve"``.
  - Item 4: the stale-gateway reaper discovers + filters gateway units by the
    resolved primary, on both platforms — closing the gap that let the stale
    ``ai.openclaw.evolve-gateway`` survive a fresh evo install.

Imports are lazy / module-local to keep the admin-shard import graph clean
(see the "module-level routes_admin import pollutes a shard" note).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── Item 3: pod_member_bots excludes the RESOLVED primary ─────────────────────


def test_pod_member_bots_excludes_evo_primary():
    """On an evo-primary pod, the primary "evo" must NOT appear in the per-member
    breakdown — the old ``m != "evolve"`` filter left it in (leak)."""
    from evolve_admin.evo.handlers._shared import pod_member_bots

    net = {
        "primary": "evo",
        "members": ["evo", "team_bot_a", "team_bot_b"],
        "bots": {"evo": {"role": "primary"}},
    }
    assert pod_member_bots(net) == ["team_bot_a", "team_bot_b"]


def test_pod_member_bots_excludes_legacy_evolve_primary():
    """A legacy pod whose primary resolves to "evolve" still excludes it —
    byte-identical to the old hardcoded filter."""
    from evolve_admin.evo.handlers._shared import pod_member_bots

    net = {"members": ["evolve", "team_bot_a"], "bots": {"evolve": {"role": "primary"}}}
    assert pod_member_bots(net) == ["team_bot_a"]


def test_pod_member_bots_legacy_shape_falls_back_to_evolve():
    """Legacy shape: "evolve" listed in members with no primary/role/bots entry
    (resolver returns None) — falls back to excluding the literal "evolve",
    byte-identical to the old hardcoded filter."""
    from evolve_admin.evo.handlers._shared import pod_member_bots

    net = {"members": ["admin_bot", "team_bot_a", "evolve"]}
    assert pod_member_bots(net) == ["admin_bot", "team_bot_a"]


def test_pod_member_bots_no_primary_no_evolve_keeps_all():
    """No primary resolvable and no "evolve" member: the evolve fallback is a
    no-op, so every member is kept."""
    from evolve_admin.evo.handlers._shared import pod_member_bots

    net = {"members": ["team_bot_a", "team_bot_b"], "bots": {}}
    assert pod_member_bots(net) == ["team_bot_a", "team_bot_b"]


# ── Item 2: exec-policy primary detection via resolved primary_bot_id ──────────


def test_exec_policy_evo_primary_classified_via_resolved_id():
    """An evo-primary bot (deny + apps) with role unset is still classified as
    primary via the resolved primary_bot_id → UNKNOWN (post-E.4), not member RED."""
    from evolve_admin import upstream_version as uv

    r = uv.evaluate_exec_policy_compliance(
        "evo",
        openclaw_config={"tools": {"exec": {"security": "deny"}}},
        exec_approvals=None,
        has_installed_apps=True,
        primary_bot_id="evo",
    )
    assert r.state == uv.STATE_UNKNOWN
    assert r.compliant is None


def test_exec_policy_no_longer_hardcodes_evolve_as_primary():
    """A bot literally named "evolve" that is NOT the resolved primary is now
    treated as a member (deny + apps → RED), proving the hardcoded
    ``bot_id == "evolve"`` primary heuristic is gone."""
    from evolve_admin import upstream_version as uv

    r = uv.evaluate_exec_policy_compliance(
        "evolve",  # not the primary on this pod
        openclaw_config={"tools": {"exec": {"security": "deny"}}},
        exec_approvals=None,
        has_installed_apps=True,
        primary_bot_id="evo",  # the real primary is evo
    )
    assert r.state == uv.STATE_RED
    assert r.compliant is False


def test_exec_policy_role_primary_still_wins_without_resolved_id():
    """role="primary" alone still classifies as primary even when no
    primary_bot_id is threaded (configured-pod path is unchanged)."""
    from evolve_admin import upstream_version as uv

    r = uv.evaluate_exec_policy_compliance(
        "evo",
        openclaw_config={"tools": {"exec": {"security": "deny"}}},
        exec_approvals=None,
        role="primary",
        has_installed_apps=True,
    )
    assert r.state == uv.STATE_UNKNOWN


# ── Item 4: stale-gateway reaper discovery + filter ───────────────────────────


def test_find_orphaned_gateways_reaps_stale_evolve_on_evo_pod(monkeypatch):
    """An evo-primary pod with a leftover ``evolve-gateway`` flags exactly that
    stale unit (and nothing the pod still expects)."""
    from evolve_admin import gateway_reaper as gr

    net = {
        "primary": "evo",
        "members": ["evo", "team_bot_a"],
        "bots": {"evo": {"role": "primary"}},
    }
    monkeypatch.setattr(
        gr,
        "installed_gateway_labels",
        lambda: [
            "ai.openclaw.evo-gateway",
            "ai.openclaw.team_bot_a-gateway",
            "ai.openclaw.evolve-gateway",  # stale, left by the rename
        ],
    )
    assert gr.find_orphaned_gateway_labels(net) == ["ai.openclaw.evolve-gateway"]


def test_find_orphaned_gateways_legacy_evolve_pod_keeps_evolve(monkeypatch):
    """A legacy evolve-primary pod keeps ``evolve-gateway`` (it IS the primary's
    gateway) — no false reap, byte-identical posture."""
    from evolve_admin import gateway_reaper as gr

    net = {"members": ["evolve", "team_bot_a"], "bots": {"evolve": {"role": "primary"}}}
    monkeypatch.setattr(
        gr,
        "installed_gateway_labels",
        lambda: ["ai.openclaw.evolve-gateway", "ai.openclaw.team_bot_a-gateway"],
    )
    assert gr.find_orphaned_gateway_labels(net) == []


def test_find_orphaned_gateways_no_primary_reaps_nothing(monkeypatch):
    """Conservative: when no primary resolves we never reap a gateway (we can't
    tell which one is the legit primary)."""
    from evolve_admin import gateway_reaper as gr

    net = {"members": ["team_bot_a"], "bots": {}}
    monkeypatch.setattr(
        gr,
        "installed_gateway_labels",
        lambda: ["ai.openclaw.evolve-gateway", "ai.openclaw.team_bot_a-gateway"],
    )
    assert gr.find_orphaned_gateway_labels(net) == []


def test_installed_gateway_labels_macos_glob(tmp_path):
    """macOS discovery globs ``ai.openclaw.*-gateway.plist`` only — NOT the
    infra (``ai.openclaw.evolve.*``) or per-bot-app (``ai.evolve.*``) shapes the
    general orphan sweep already owns."""
    from evolve_admin import gateway_reaper as gr

    (tmp_path / "ai.openclaw.evo-gateway.plist").write_text("x")
    (tmp_path / "ai.openclaw.evolve-gateway.plist").write_text("x")
    (tmp_path / "ai.openclaw.evolve.heal.evo.plist").write_text("x")  # infra, not gw
    (tmp_path / "ai.evolve.evolve.admin-ui.plist").write_text("x")  # app, not gw

    got = gr.installed_gateway_labels(profile_name="macos", launchd_dir=tmp_path)
    assert got == ["ai.openclaw.evo-gateway", "ai.openclaw.evolve-gateway"]


def test_installed_gateway_labels_linux_systemd_glob(tmp_path):
    """Linux discovery globs ``/etc/systemd/system/ai.openclaw.*-gateway.service``
    — the path the general (macOS-only) orphan sweep never scanned, which is why
    the stale unit survived on the live Linux pod."""
    from evolve_admin import gateway_reaper as gr

    (tmp_path / "ai.openclaw.evo-gateway.service").write_text("x")
    (tmp_path / "ai.openclaw.evolve-gateway.service").write_text("x")
    (tmp_path / "ai.evolve.evolve.heal.service").write_text("x")  # infra, not gw

    got = gr.installed_gateway_labels(profile_name="linux", systemd_dir=tmp_path)
    assert got == ["ai.openclaw.evo-gateway", "ai.openclaw.evolve-gateway"]


def test_reap_orphaned_gateways_dry_run_reports_without_removing(monkeypatch):
    """Dry-run reports the intended set and never calls scheduler.remove."""
    import runtime.scheduler as _sched
    from evolve_admin import gateway_reaper as gr

    called = {"remove": 0}

    class _FakeScheduler:
        def remove(self, label, **_kw):
            called["remove"] += 1
            return True, ""

    monkeypatch.setattr(_sched, "get_scheduler", lambda: _FakeScheduler())
    removed, failures = gr.reap_orphaned_gateways(
        ["ai.openclaw.evolve-gateway"], dry_run=True
    )
    assert removed == ["ai.openclaw.evolve-gateway"]
    assert failures == []
    assert called["remove"] == 0


def test_reap_orphaned_gateways_removes_via_scheduler(monkeypatch):
    """Live reap routes each label through the scheduler seam and partitions
    successes/failures."""
    import runtime.scheduler as _sched
    from evolve_admin import gateway_reaper as gr

    class _FakeScheduler:
        def remove(self, label, **_kw):
            if label.endswith("evolve-gateway"):
                return True, ""
            return False, "boom"

    monkeypatch.setattr(_sched, "get_scheduler", lambda: _FakeScheduler())
    removed, failures = gr.reap_orphaned_gateways(
        ["ai.openclaw.evolve-gateway", "ai.openclaw.broken-gateway"]
    )
    assert removed == ["ai.openclaw.evolve-gateway"]
    assert failures == ["ai.openclaw.broken-gateway — boom"]
