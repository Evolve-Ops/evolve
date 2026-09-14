"""tests/test_oc_model_preserve_write_cwd.py — the schema-validator spawn in
``_preserve_write`` must pass an explicit, traversable ``cwd``.

Incident (2026-06-10 canary): ``sudo evolve-admin deploy <bot>`` over ssh
died inside ``openclaw config validate`` before the CLI produced any output.
The spawn inherited the caller's CWD — the ssh admin's home directory,
untraversable for the invoking identity — and ``openclaw`` is a Node binary:
Node calls ``uv_cwd()`` during startup, which EACCESes on an untraversable
CWD (same shape as the documented ``sudo -u evolve python3`` gotcha; see
CLAUDE.md "cd /Users/<bot> (or /tmp) first").

Locked here: the validate spawn passes ``cwd=`` pointing at the config's
parent directory — guaranteed traversable by the invoking identity, since
``_preserve_write`` just created the temp file there.

The fake is installed via ``monkeypatch.setattr(oc_model.subprocess, "run",
…)`` — attribute patch on the shared ``subprocess`` module, not a string
patch of this module's name — so the interception survives code moves
(see test_deploy_scheduler_migration.py's guard pattern).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import oc_model  # noqa: E402


def test_validator_spawn_passes_explicit_safe_cwd(tmp_path, monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout='{"valid": true}', stderr="")

    monkeypatch.setattr(oc_model.subprocess, "run", fake_run)

    target = tmp_path / "bot-home" / ".openclaw" / "openclaw.json"
    oc_model._preserve_write({"agents": {"defaults": {}}}, target)

    assert captured["cmd"][:3] == ["openclaw", "config", "validate"]
    cwd = captured["kwargs"].get("cwd")
    assert cwd is not None, (
        "validator spawn must not inherit the caller's CWD — over ssh that's "
        "the admin's home and Node's uv_cwd() EACCESes before the CLI runs"
    )
    # The chosen safe cwd is the config's parent dir: the writer just landed
    # a temp file there, so the invoking identity can traverse it.
    assert cwd == str(target.parent)
    assert Path(cwd).is_dir()
    # And the write itself still completes.
    assert json.loads(target.read_text()) == {"agents": {"defaults": {}}}
