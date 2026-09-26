"""The wizard's pod-state paths follow network.json's ``sharedDir``.

``evolve_admin.config.DEFAULT_SHARED_DIR`` is an import-time snapshot of the
*platform* default (``/Users/Shared/evolve`` on macOS, ``/var/lib/evolve`` on
Linux) and is blind to network.json's ``sharedDir`` key. Every wizard site that
reads or writes pod state under ``{shared_dir}`` must therefore resolve through
``evolve_config`` — ``_resolved_shared_dir()`` here — not through the constant.

The bug this pins: ``_log_admin_action`` resolved its audit-log path as
``DEFAULT_SHARED_DIR / "logs" / "admin-actions.jsonl"``, so on a pod with a
custom ``sharedDir`` the whole wizard audit trail (``write_evolve_sudoers``,
the ``_provision_evo_oc`` steps, the evo cutover steps) was written where
nothing reads — and, where the default path isn't writable, dropped entirely by
the function's by-design ``except Exception: pass``.

Companion to test_setup_wizard_linux_paths.py, whose scope note explains why
the ``DEFAULT_SHARED_DIR`` sites can't be asserted by pinning the LINUX
profile: the constant is frozen at import. These tests steer the *config*
instead (``EVOLVE_NETWORK`` → a tmp_path network.json), which is what a real
custom-sharedDir pod does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import setup_wizard  # noqa: E402


@pytest.fixture
def custom_shared_pod(tmp_path, monkeypatch):
    """A pod whose network.json sets a tmp_path-rooted custom ``sharedDir``.

    ``EVOLVE_NETWORK`` is the documented override in
    ``evolve_config.resolve_network_path``, so this exercises the real
    resolution chain (resolve_network_path → load_config → get_shared_dir)
    rather than stubbing it out. tmp_path-rooted so the autouse
    real-shared-dir guard in conftest.py stays quiet.
    """
    shared = tmp_path / "custom-shared"
    net = tmp_path / "network.json"
    net.write_text(json.dumps({
        "networkId": "custom-pod",
        "evolveVersion": "0.1.0",
        "sharedDir": str(shared),
    }))
    monkeypatch.setenv("EVOLVE_NETWORK", str(net))
    return shared


def test_resolved_shared_dir_honours_custom_shared_dir(custom_shared_pod):
    """The resolver returns network.json's sharedDir, not the platform default."""
    assert setup_wizard._resolved_shared_dir() == custom_shared_pod
    assert setup_wizard._resolved_shared_dir() != Path(setup_wizard.DEFAULT_SHARED_DIR)


def test_log_admin_action_writes_under_custom_shared_dir(custom_shared_pod):
    """The audit entry lands under ``{sharedDir}/logs/``, and nowhere else."""
    setup_wizard._log_admin_action(
        "write_evolve_sudoers", "ok", bot="evolve", initiated_by="wizard",
    )

    log_path = custom_shared_pod / "logs" / "admin-actions.jsonl"
    assert log_path.exists(), (
        "audit entry did not land under the pod's configured sharedDir — "
        f"_log_admin_action is still resolving through DEFAULT_SHARED_DIR "
        f"({setup_wizard.DEFAULT_SHARED_DIR})"
    )
    entry = json.loads(log_path.read_text().strip())
    assert entry["action"] == "write_evolve_sudoers"
    assert entry["bot"] == "evolve"
    assert entry["initiated_by"] == "wizard"
    assert entry["result"] == "ok"
    assert entry["ts"].endswith("Z")


def test_log_admin_action_appends(custom_shared_pod):
    """Repeat calls append rather than truncate (the trail is a JSONL log)."""
    setup_wizard._log_admin_action("evo_cutover_bootout", "ok", bot="evo")
    setup_wizard._log_admin_action("evo_cutover_copy_state", "ok", bot="evo")

    lines = (custom_shared_pod / "logs" / "admin-actions.jsonl").read_text().splitlines()
    assert [json.loads(ln)["action"] for ln in lines] == [
        "evo_cutover_bootout", "evo_cutover_copy_state",
    ]


def test_log_admin_action_never_raises_on_an_unwritable_dir(tmp_path, monkeypatch):
    """Best-effort semantics survive the reroute: an audit-log failure must
    never fail the wizard action that triggered it."""
    monkeypatch.setattr(
        setup_wizard, "_resolved_shared_dir",
        lambda *a, **kw: tmp_path / "nope" / "\0bad",
    )
    setup_wizard._log_admin_action("write_evolve_sudoers", "ok")  # must not raise


def test_resolved_shared_dir_falls_back_to_platform_default(tmp_path, monkeypatch):
    """With no resolvable config the resolver returns DEFAULT_SHARED_DIR —
    the exact path the callers used before it existed, so nothing that used
    to write somewhere starts writing nowhere."""
    monkeypatch.setenv("EVOLVE_NETWORK", str(tmp_path / "absent" / "network.json"))
    import evolve_config

    monkeypatch.setattr(evolve_config, "CANONICAL_NETWORK_JSON", tmp_path / "also-absent.json")
    assert setup_wizard._resolved_shared_dir() == Path(setup_wizard.DEFAULT_SHARED_DIR)
