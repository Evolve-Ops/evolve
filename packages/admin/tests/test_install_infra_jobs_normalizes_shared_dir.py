"""tests/test_install_infra_jobs_normalizes_shared_dir.py

Regression for the gap that left ``proposals/applied/`` and peers at
``drwxrwxrwt security_bot:wheel`` (1777, sticky) on every existing install
after PR #1019: ``install_evolve_infra_jobs`` did not call
``deploy_shared_dir``, so the dir-mode fix only applied during a full
``evolve-admin deploy <bot>`` run — never via the typical
``install-infra-jobs`` refresh path.

This pins the contract: every install-infra-jobs invocation calls
deploy_shared_dir BEFORE any per-daemon installer fires.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy  # noqa: E402


def test_install_evolve_infra_jobs_invokes_deploy_shared_dir_before_daemons(tmp_path):
    """Sabotage the first per-daemon installer to raise. If the test
    sees deploy_shared_dir was called before that exception propagated,
    the ordering contract is satisfied — install-infra-jobs normalized
    the shared dir before touching any daemon plist.
    """
    calls: list[Path] = []

    def fake_deploy_shared_dir(shared_dir, dry_run=False):
        calls.append(Path(shared_dir))
        return deploy.DeployResult(bot_id="shared", success=True)

    def boom(*args, **kwargs):
        raise RuntimeError("stop_after_shared_dir_normalize")

    with patch.object(deploy, "deploy_shared_dir", side_effect=fake_deploy_shared_dir), \
         patch.object(deploy, "_install_launchd_analyze", side_effect=boom):
        with pytest.raises(RuntimeError, match="stop_after_shared_dir_normalize"):
            deploy.install_evolve_infra_jobs(
                evolve_dir=tmp_path / "evolve_home",
                shared_dir=tmp_path / "shared",
            )

    # Despite the sabotage, deploy_shared_dir must have run before the
    # exception propagated — proving it's called BEFORE per-daemon work.
    assert len(calls) == 1, (
        "install_evolve_infra_jobs must call deploy_shared_dir before "
        "any per-daemon installer; this guarantees the dir-mode fixes "
        "(PR #1019) apply during install-infra-jobs runs"
    )
    assert calls[0] == tmp_path / "shared"
