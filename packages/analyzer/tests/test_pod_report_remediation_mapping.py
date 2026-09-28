"""Tests for the pod_report→remediation mapping (Phase 4 PR-2).

Pins:
  - metrics_outage → install_infra_jobs remediation (fixes the case where
    the per-bot metrics writer LaunchDaemons aren't installed)
  - Other pod_report signal_types stay un-remediated (pod_silent,
    gateway_down, audit_critical etc. need operator judgment)
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER = Path(__file__).parent.parent
if str(_ANALYZER) not in sys.path:
    sys.path.insert(0, str(_ANALYZER))

from pod_report import _pod_report_remediation  # noqa: E402


def test_metrics_outage_gets_install_infra_jobs():
    rem = _pod_report_remediation("metrics_outage")
    assert rem is not None
    assert rem.kind == "install_infra_jobs"
    assert rem.params == {}
    assert rem.label
    # The confirm text should call out the idempotency + escape hatch
    # (the install-infra-jobs path doesn't help if the daemons are
    # installed but crashing — operator needs to know that).
    assert "install-infra-jobs" in rem.confirm.lower() or "infra" in rem.confirm.lower()
    assert ("crash" in rem.confirm.lower()
            or "stderr" in rem.confirm.lower()
            or "log" in rem.confirm.lower()), (
        "confirm text should mention the daemon-crashing escape hatch"
    )


def test_pod_silent_does_not_auto_remediate():
    """Genuine pod silence (vs. metrics writer outage) needs investigation."""
    assert _pod_report_remediation("pod_silent") is None


def test_gateway_down_does_not_auto_remediate():
    """Manual investigation case — could be many causes."""
    assert _pod_report_remediation("gateway_down") is None


def test_audit_critical_does_not_auto_remediate():
    """Cross-producer audit findings have their own remediation logic in
    audit.py; pod_report shouldn't double-attach."""
    assert _pod_report_remediation("audit_critical") is None


def test_unknown_signal_type_does_not_auto_remediate():
    """Forward-compat: a new pod_report signal_type doesn't crash here;
    it just doesn't get a remediation until the mapping is extended."""
    assert _pod_report_remediation("brand_new_kind") is None
