"""_install_launchd_expansion must pass --bot — expansion.py requires it.

Regression: the expansion launchd job (ai.openclaw.evolve.expansion.evolve)
was installed with no ProgramArguments beyond the script path, but
expansion.py's argparse marks ``--bot`` ``required=True``. Every Sunday fire
exited 2 ("the following arguments are required: --bot") and
cron_exit_monitor mirrored the non-zero exit into a firing
``cron_job_failed`` Signal that never cleared (the job could not succeed).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))


def test_expansion_install_passes_bot(monkeypatch):
    from evolve_admin import deploy

    captured: dict = {}
    monkeypatch.setattr(deploy, "_install_launchd", lambda **kw: captured.update(kw))

    result = deploy.DeployResult(bot_id="evolve", success=True)
    deploy._install_launchd_expansion("evolve", Path("/tmp/evolve"), result)

    assert captured["label"] == "ai.openclaw.evolve.expansion.evolve"
    assert captured["user"] == "evolve"
    # The fix: --bot is present (and matches the install's bot identity).
    assert captured["extra_args"] == ["--bot", "evolve"]
