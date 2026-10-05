"""tests/test_phase_c8_batch.py — Phase C-8 watchdog/workspace batch.

Spec: internal/spec-proposal-drafting-protocol-2026-06-04.md.

Four small/medium generators:
  - sysadmin_watchdog        single ACL-drift Proposal factory
  - persona_tuner            per-cluster (noun, verb) signature
  - workspace_inventory      per-path / per-cron signature
  - workspace_security       per-path signature

Each gets a smoke check on content + signature granularity.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

_THIS_FILE = Path(__file__).resolve()
_ANALYZER_DIR = _THIS_FILE.parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))


# ─────────────────────────────────────────────────────────────────────────────
# sysadmin_watchdog
# ─────────────────────────────────────────────────────────────────────────────


sw_prop = importlib.import_module("generators.sysadmin_watchdog.proposals")


class TestSysadminWatchdog:
    def test_acl_drift_carries_content_and_signature(self):
        p = sw_prop.make_acl_drift_fix(
            "team_bot_a",
            acl_config_path="/tmp/whatever",
            admin_summary="ACL drifted",
        )
        assert p.summary and p.explanation
        assert p.action_label == "Restore evolve ACL"
        assert p.dismiss_signature == sw_prop.DISMISS_SIG_ACL_DRIFT


# ─────────────────────────────────────────────────────────────────────────────
# persona_tuner
# ─────────────────────────────────────────────────────────────────────────────


pt_obs = importlib.import_module("generators.persona_tuner.observe")


class TestPersonaTuner:
    def test_per_cluster_signatures_distinct(self):
        a = pt_obs.dismiss_signature_for_cluster("food", "cooking")
        b = pt_obs.dismiss_signature_for_cluster("travel", "planning")
        assert a != b
        assert "food" in a and "cooking" in a


# ─────────────────────────────────────────────────────────────────────────────
# workspace_inventory
# ─────────────────────────────────────────────────────────────────────────────


wi_sp = importlib.import_module("generators.workspace_inventory.signal_proposals")


def _wi_signal(sig_type, items):
    return SimpleNamespace(
        id=f"sig-{sig_type}",
        bot_id="team_bot_a",
        type=sig_type,
        details={"items": items},
    )


class TestWorkspaceInventory:
    def test_unregistered_script_per_path_signature(self):
        a = wi_sp.dismiss_signature_for_script("scripts/a.py")
        b = wi_sp.dismiss_signature_for_script("scripts/b.py")
        assert a != b

    def test_script_proposal_content(self):
        ps = wi_sp.make_unregistered_script_proposal(
            _wi_signal("unregistered_script", [
                {"path": "scripts/x.py", "message": "found by scanner"},
            ])
        )
        assert len(ps) == 1
        p = ps[0]
        assert p.summary and p.explanation
        assert p.action_label == "Open Applications tab"
        assert "scripts/x.py" in p.dismiss_signature

    def test_cron_proposal_carries_content(self):
        ps = wi_sp.make_unregistered_cron_proposal(
            _wi_signal("unregistered_cron", [
                {"cron": "0 9 * * * echo hi", "message": "found"},
            ])
        )
        assert len(ps) == 1
        p = ps[0]
        assert p.summary and p.dismiss_signature.startswith(
            "workspace_inventory:unregistered_cron:"
        )


# ─────────────────────────────────────────────────────────────────────────────
# workspace_security
# ─────────────────────────────────────────────────────────────────────────────


ws_sp = importlib.import_module("generators.workspace_security.signal_proposals")


class TestWorkspaceSecurity:
    def test_per_path_signatures_distinct(self):
        a = ws_sp.dismiss_signature_for_path("memory/a.md")
        b = ws_sp.dismiss_signature_for_path("memory/b.md")
        assert a != b

    def test_misplaced_secret_carries_content(self):
        ps = ws_sp.make_misplaced_secret_proposal(SimpleNamespace(
            id="sig-1",
            bot_id="team_bot_a",
            details={"items": [
                {"path": "memory/leak.md", "message": "AWS_SECRET-shape"},
            ]},
        ))
        assert len(ps) == 1
        p = ps[0]
        assert p.summary and p.explanation
        assert p.action_label == "Open Compliance subtab"
        assert "memory/leak.md" in p.dismiss_signature
