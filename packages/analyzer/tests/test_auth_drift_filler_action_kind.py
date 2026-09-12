"""tests/test_auth_drift_filler_action_kind.py — action-kind regression test.

Locks in the invariant that ``auth_drift_filler`` emits
``UpdatePermissionConfig`` (not generic ``ConfigPatch``) so its apply
path routes through ``UpdatePermissionConfigApplier`` →
``permissions.writer.write_openclaw_fields`` (the canonical /tmp + sudo
writer that works against bot-owned ``.openclaw/`` directories).

Background: the generic L1 ``ConfigPatchApplier`` uses
``tempfile.mkstemp(dir=path.parent)`` which fails on member bots whose
``/Users/<bot>/.openclaw/`` is bot-owned with evolve having only a
read-only ACL. The auth_drift_filler regression at PR #1316 silently
chose the wrong action kind; this test prevents a re-regression.

See ``internal/diagnosis-openclaw-json-write-regression-2026-05-21.md``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.auth_drift_filler.signal_proposals import (  # noqa: E402
    make_drift_proposals,
)
from schema.proposal import (  # noqa: E402
    ConfigPatch,
    UpdatePermissionConfig,
)


def _drift_signal(bot_id: str, field: str, *, expected, observed) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"sig-{bot_id}-{field}",
        bot_id=bot_id,
        type="perm_config_drift",
        severity="warn",
        details={"bot_id": bot_id, "diffs": {field: {"expected": expected, "observed": observed}}},
    )


def test_action_is_update_permission_config_not_config_patch():
    """Primary regression guard — auth_drift_filler must emit
    UpdatePermissionConfig actions. ConfigPatch routes to the generic
    L1 patcher which fails on bot-owned ``.openclaw/`` dirs."""
    sig = _drift_signal(
        bot_id="admin_bot", field="tools.exec.security",
        expected="deny", observed="full",
    )
    proposals = make_drift_proposals(sig)
    assert len(proposals) == 1
    p = proposals[0]
    assert isinstance(p.action, UpdatePermissionConfig), (
        f"auth_drift_filler must emit UpdatePermissionConfig actions; "
        f"got {type(p.action).__name__!r}. See "
        f"internal/diagnosis-openclaw-json-write-regression-2026-05-21.md"
    )
    assert not isinstance(p.action, ConfigPatch)
    assert p.action.kind == "UpdatePermissionConfig"


def test_action_bot_id_matches_seeded_signal():
    sig = _drift_signal(
        bot_id="team_bot_a", field="tools.exec.security",
        expected="deny", observed="full",
    )
    proposals = make_drift_proposals(sig)
    assert len(proposals) == 1
    assert proposals[0].action.bot_id == "team_bot_a"


def test_action_fields_carries_field_and_expected_value():
    """The fields dict is the applier's payload — must have exactly one
    entry mapping the drifted dotpath to its baseline (expected) value."""
    sig = _drift_signal(
        bot_id="admin_bot", field="tools.exec.security",
        expected="deny", observed="full",
    )
    proposals = make_drift_proposals(sig)
    p = proposals[0]
    assert p.action.fields == {"tools.exec.security": "deny"}


def test_action_has_no_target_path_attr():
    """``target_path`` is the ConfigPatch-specific routing field. Its
    absence is what keeps the apply dispatcher off the generic L1
    patcher path; assert it explicitly so a future refactor can't
    accidentally reintroduce it."""
    sig = _drift_signal(
        bot_id="admin_bot", field="tools.exec.security",
        expected="deny", observed="full",
    )
    proposals = make_drift_proposals(sig)
    p = proposals[0]
    assert not hasattr(p.action, "target_path")


def test_action_fields_value_deep_copied():
    """Deep-copy invariant inherited from cron_caps_filler: downstream
    applier code mutating a dict/list value cannot leak back into the
    signal's in-memory snapshot."""
    expected_obj = {"nested": ["a", "b"]}
    sig = _drift_signal(
        bot_id="admin_bot", field="tools.exec.security",
        expected=expected_obj, observed="something_else",
    )
    proposals = make_drift_proposals(sig)
    p = proposals[0]
    # Mutate the action payload; the signal's expected dict must remain.
    p.action.fields["tools.exec.security"]["nested"].append("c")
    assert expected_obj == {"nested": ["a", "b"]}
