"""Pinning tests for audit.py's openclaw.json perms check.

Security fix 2026-06-12 — openclaw.json holds the gateway token + every
messaging-channel bot token, so it MUST be 0600. This SUPERSEDES the earlier
PR #1377 reasoning that "0644 is fine because /Users/<bot>/ is mode 700":
deploy.py forces the home dir to **0755** (for shell access), so a 0644 inner
file IS world-readable on a multi-user box — the exact live exposure
(2026-06-12) this fix closes.

The contract now:
  - audit.py treats ONLY 0600 as OK; any other mode (0644, 0666, …) is a
    finding and is corrected to 0600.
  - The corrective chmod uses 600, backed by the new §4 sudoers grant
    (`chmod 600 .../openclaw.json`) in setup_wizard._render_evolve_sudoers.
    The legacy `chmod 644 openclaw.json` grant (§13) was removed in the same
    change, so a corrective `chmod 644` would now be a denied no-op.

These tests pin that contract (source-string checks, matching this file's
original style; the behavioural enforcement lives in audit.py and the writer
self-heal is covered by packages/admin/tests/test_oc_config_secret_mode.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

_AUDIT_PY = _ANALYZER_DIR / "audit.py"


@pytest.fixture(scope="module")
def audit_source() -> str:
    assert _AUDIT_PY.exists(), f"{_AUDIT_PY} not found"
    return _AUDIT_PY.read_text(encoding="utf-8")


def test_audit_requires_0600(audit_source: str):
    """The perms enforcement block must treat ONLY 0600 as OK — any other
    mode (0644 included) triggers a corrective chmod (unless it is a Linux
    ACL-mask artifact, see test_audit_acl_masked_perms.py §5). The gate is
    expressed as ``perms == "0600"`` (the OK branch); everything else corrects.
    Pre-2026-06-24 the same contract read ``perms != "0600"``."""
    assert 'perms == "0600"' in audit_source, (
        "audit.py no longer enforces 0600-only for openclaw.json. It is a "
        "token-bearing file and 0644 is world-readable (home is 0755, not "
        "0700) — anything but 0600 must be flagged and corrected."
    )


def test_audit_corrective_chmod_uses_600(audit_source: str):
    """The corrective chmod must use 600 — backed by the new §4 sudoers grant.
    A `chmod 644` on openclaw.json would now be a denied no-op (the §13 644
    grant was removed)."""
    assert '"/bin/chmod", "600", str(oc_path)' in audit_source, (
        "audit.py's corrective chmod no longer uses 600. openclaw.json must be "
        "0600 and the §4 grant covers chmod 600 for it."
    )
    # The legacy corrective `chmod 644` must be gone — there is no chmod-644
    # grant for openclaw.json anymore, and 644 is the vulnerable mode.
    assert '"/bin/chmod", "644"' not in audit_source, (
        "audit.py still issues `sudo /bin/chmod 644 <openclaw.json>`. That mode "
        "is world-readable on a 0755 home and has no sudoers grant anymore."
    )
