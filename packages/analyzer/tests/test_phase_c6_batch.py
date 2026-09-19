"""tests/test_phase_c6_batch.py — Phase C-6 batch (3 generators).

Spec: internal/spec-proposal-drafting-protocol-2026-06-04.md.

Three medium-sized generators in one sweep:

  - auth_drift_filler       per-field dismiss (one signature per drifted
                            field name) — dismissing tools.fs.workspaceOnly
                            should NOT suppress findings on
                            tools.shellExec on the same bot.
  - bot_config_integrity    catalog_tier_drift is per-bot (single rollup
                            proposal per scan); catalog_provider_coverage
                            is per-provider (dismissing google does NOT
                            suppress xai).
  - manifest_quality        per-(kind, app) — dismissing stale for app X
                            should NOT suppress validation_error for
                            app X, or stale for app Y.

The tests focus on what's new: content fields present + signature
granularity + observe() suppression gate. Existing fixtures already
exercise the proposal structure; we don't re-pin those.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_THIS_FILE = Path(__file__).resolve()
_ANALYZER_DIR = _THIS_FILE.parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter import dismissals  # noqa: E402
from schema.signal import make_signature  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# auth_drift_filler
# ─────────────────────────────────────────────────────────────────────────────


adf_sp = importlib.import_module("generators.auth_drift_filler.signal_proposals")
adf_obs = importlib.import_module("generators.auth_drift_filler.observe")


def _drift_signal(bot_id="team_bot_a", diffs=None):
    if diffs is None:
        diffs = {
            "tools.fs.workspaceOnly": {
                "expected": True, "observed": False,
            },
        }
    return {
        "id": "sig-drift-1",
        "bot_id": bot_id,
        "details": {"diffs": diffs},
    }


class TestAuthDriftFillerContent:
    def test_proposal_carries_content_and_per_field_signature(self):
        ps = adf_sp.make_drift_proposals(_drift_signal())
        assert len(ps) == 1
        p = ps[0]
        assert p.summary
        assert len(p.summary) <= 400
        assert p.explanation
        assert len(p.explanation) <= 1500
        assert p.action_label == "Restore baseline"
        assert p.manual_path == "Permissions → team_bot_a"
        assert p.dismiss_signature == (
            "auth_drift_filler:perm_config_drift:tools.fs.workspaceOnly"
        )

    def test_distinct_fields_get_distinct_signatures(self):
        ps = adf_sp.make_drift_proposals(_drift_signal(diffs={
            "tools.fs.workspaceOnly": {"expected": True, "observed": False},
            "tools.shellExec.allowList": {"expected": [], "observed": ["*"]},
        }))
        sigs = {p.dismiss_signature for p in ps}
        assert len(sigs) == 2

    def test_observe_suppresses_one_field_without_affecting_others(
        self, tmp_path,
    ):
        # Write the drift signal via the real signals store.
        signals_store.observe(
            tmp_path,
            signature=make_signature(
                "permission_monitor", "perm_config_drift", "team_bot_a",
            ),
            producer="permission_monitor",
            type="perm_config_drift",
            flavor="maintenance",
            severity="warn",
            scope="bot",
            bot_id="team_bot_a",
            title="team_bot_a: perm_config_drift",
            details={
                "diffs": {
                    "tools.fs.workspaceOnly": {
                        "expected": True, "observed": False,
                    },
                    "tools.shellExec.allowList": {
                        "expected": [], "observed": ["*"],
                    },
                },
            },
        )
        # Dismiss only the workspaceOnly field.
        dismissals.record_dismissal(
            tmp_path,
            signature=adf_sp.dismiss_signature_for_field(
                "tools.fs.workspaceOnly",
            ),
            bot_id="team_bot_a",
            scope="kind",
        )
        out = adf_obs.observe(adf_obs.AuthDriftFillerContext(
            bot_id="team_bot_a", shared_dir=tmp_path,
        ))
        sigs = {p.dismiss_signature for p in out}
        # workspaceOnly suppressed; shellExec.allowList survives.
        assert adf_sp.dismiss_signature_for_field(
            "tools.fs.workspaceOnly"
        ) not in sigs
        assert adf_sp.dismiss_signature_for_field(
            "tools.shellExec.allowList"
        ) in sigs


# ─────────────────────────────────────────────────────────────────────────────
# bot_config_integrity
# ─────────────────────────────────────────────────────────────────────────────


bci_tier = importlib.import_module(
    "generators.bot_config_integrity.checks.catalog_tier_drift",
)
bci_provider = importlib.import_module(
    "generators.bot_config_integrity.checks.catalog_provider_coverage",
)


class TestBotConfigIntegrityContent:
    def test_tier_drift_proposal_carries_content(self, monkeypatch):
        """When the model_catalog helper reports tier-member-missing
        findings, the emitted proposal carries Phase C-6 fields."""
        from types import SimpleNamespace

        # Stub find_catalog_drift to return one tier-missing finding.
        def _stub_find(*args, **kwargs):
            return [SimpleNamespace(
                kind="tier_member_missing",
                model_id="google/gemini-2.5-pro",
            )]

        monkeypatch.setattr(
            "evolve_admin.model_catalog.find_catalog_drift", _stub_find,
        )
        ctx = SimpleNamespace(bot_id="team_bot_a", shared_dir=Path("/tmp"))
        ps = bci_tier.run(ctx, {"catalog": [], "tiers": {}})
        assert len(ps) == 1
        p = ps[0]
        assert p.summary
        assert p.explanation
        assert p.action_label == "Reconcile catalog"
        assert p.dismiss_signature == bci_tier.DISMISS_SIGNATURE

    def test_provider_coverage_per_provider_signature(self):
        """The provider-coverage check builds a per-provider signature
        so dismissing google does not suppress xai."""
        a = bci_provider.dismiss_signature_for_provider("google")
        b = bci_provider.dismiss_signature_for_provider("xai")
        assert a != b
        assert "google" in a
        assert "xai" in b


# ─────────────────────────────────────────────────────────────────────────────
# manifest_quality
# ─────────────────────────────────────────────────────────────────────────────


mq_sp = importlib.import_module("generators.manifest_quality.signal_proposals")
mq_obs = importlib.import_module("generators.manifest_quality.observe")


def _manifest_signal(sig_type, bot_id="team_bot_a", items=None):
    if items is None:
        items = [{"app_id": "task-app", "message": "120 days old"}]
    return {
        "id": f"sig-{sig_type}",
        "bot_id": bot_id,
        "details": {"items": items},
    }


class TestManifestQualityContent:
    def test_stale_proposal_carries_content_and_per_app_signature(self):
        ps = mq_sp.make_stale_proposal(_manifest_signal("stale"))
        assert len(ps) == 1
        p = ps[0]
        assert p.summary
        assert p.explanation
        assert p.action_label == "Open Applications tab"
        assert "task-app" in p.manual_path
        assert p.dismiss_signature == "manifest_quality:stale:task-app"

    def test_test_failing_proposal_is_tier_5_paste_to_bot(self):
        ps = mq_sp.make_test_failing_proposal(
            _manifest_signal("test_failing", items=[
                {"app_id": "task-app", "message": "exit 1"},
            ])
        )
        assert len(ps) == 1
        p = ps[0]
        assert p.summary
        assert p.manual_instruction  # Tier 5
        assert "task-app" in p.manual_instruction
        assert p.dismiss_signature == "manifest_quality:test_failing:task-app"

    def test_validation_error_carries_content(self):
        ps = mq_sp.make_validation_error_proposal(
            _manifest_signal("validation_error", items=[
                {"app_id": "task-app", "message": "schema_version invalid"},
            ])
        )
        assert len(ps) == 1
        p = ps[0]
        assert p.summary
        assert p.action_label == "Open Applications tab"
        assert (
            p.dismiss_signature == "manifest_quality:validation_error:task-app"
        )


class TestManifestQualitySignatureGranularity:
    def test_same_kind_different_apps_distinct(self):
        a = mq_sp.dismiss_signature_for("stale", "task-app")
        b = mq_sp.dismiss_signature_for("stale", "memo-app")
        assert a != b

    def test_different_kinds_same_app_distinct(self):
        a = mq_sp.dismiss_signature_for("stale", "task-app")
        b = mq_sp.dismiss_signature_for("validation_error", "task-app")
        assert a != b


class TestManifestQualityObserveSuppression:
    def test_observe_suppresses_one_kind_without_affecting_others(
        self, tmp_path,
    ):
        # Emit a stale signal AND a validation_error signal for task-app.
        for sig_type in ("stale", "validation_error"):
            signals_store.observe(
                tmp_path,
                signature=make_signature(
                    "compliance_scan", sig_type, "team_bot_a",
                ),
                producer="compliance_scan",
                type=sig_type,
                flavor="maintenance",
                severity="warn",
                scope="bot",
                bot_id="team_bot_a",
                title=f"team_bot_a: {sig_type}",
                details={"items": [
                    {"app_id": "task-app", "message": "test message"},
                ]},
            )
        # Dismiss only the stale finding for task-app.
        dismissals.record_dismissal(
            tmp_path,
            signature=mq_sp.dismiss_signature_for("stale", "task-app"),
            bot_id="team_bot_a",
            scope="kind",
        )
        out = mq_obs.observe(mq_obs.ManifestQualityContext(
            bot_id="team_bot_a", shared_dir=tmp_path,
        ))
        sigs = {p.dismiss_signature for p in out}
        # stale:task-app suppressed; validation_error:task-app survives.
        assert "manifest_quality:stale:task-app" not in sigs
        assert "manifest_quality:validation_error:task-app" in sigs

    def test_dismiss_one_app_does_not_suppress_other_apps(self, tmp_path):
        signals_store.observe(
            tmp_path,
            signature=make_signature(
                "compliance_scan", "stale", "team_bot_a",
            ),
            producer="compliance_scan",
            type="stale",
            flavor="maintenance",
            severity="warn",
            scope="bot",
            bot_id="team_bot_a",
            title="team_bot_a: stale",
            details={"items": [
                {"app_id": "task-app", "message": "120 days"},
                {"app_id": "memo-app", "message": "100 days"},
            ]},
        )
        dismissals.record_dismissal(
            tmp_path,
            signature=mq_sp.dismiss_signature_for("stale", "task-app"),
            bot_id="team_bot_a",
            scope="kind",
        )
        out = mq_obs.observe(mq_obs.ManifestQualityContext(
            bot_id="team_bot_a", shared_dir=tmp_path,
        ))
        sigs = {p.dismiss_signature for p in out}
        assert "manifest_quality:stale:task-app" not in sigs
        assert "manifest_quality:stale:memo-app" in sigs
