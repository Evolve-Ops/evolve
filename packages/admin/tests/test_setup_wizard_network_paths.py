"""Platform-keyed path literals the wizard writes into ``network.json``.

8.3 Linux port (docs/design-linux-port-2026-06-10.md §6 + the wizard-port
census docs/census-linux-wizard-remainder-2026-06-11.md, Step 14 / W3).

The wizard's ``network.json`` builder used to hardcode a macOS
``/Users/Shared/evolve/security_rules.json`` for ``security.rulesFile`` —
latent on macOS, but a real defect under ``EVOLVE_PLATFORM=linux``: the
security reviewer (``analyzer/review.py``) reads that exact string and a
Linux pod's shared dir is ``/var/lib/evolve``, so the configured rules file
could never be found.

These tests pin ``security.rulesFile`` to the operator-chosen shared dir on
BOTH profiles: byte-identical to today's value on macOS, and correctly
``/var/lib/evolve``-derived on Linux — matching review.py's own default so
producer (wizard) and consumer (reviewer) agree on every platform.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, get_profile, set_profile  # noqa: E402

from evolve_admin import setup_wizard  # noqa: E402


# ── _build_security_section: the rulesFile derivation ────────────────────────


def test_macos_rulesfile_is_unchanged_literal():
    """macOS byte-identity: the value the wizard has always written."""
    sec = setup_wizard._build_security_section(Path("/Users/Shared/evolve"))
    assert sec["rulesFile"] == "/Users/Shared/evolve/security_rules.json"


def test_linux_rulesfile_is_shared_dir_derived():
    """Under the Linux gate the rulesFile follows the FHS shared dir, with
    no macOS ``/Users/`` path leaking into a Linux pod's network.json."""
    sec = setup_wizard._build_security_section(Path("/var/lib/evolve"))
    assert sec["rulesFile"] == "/var/lib/evolve/security_rules.json"
    assert "/Users/" not in sec["rulesFile"]


def test_rulesfile_tracks_any_operator_chosen_dir():
    """Step 13 lets the operator pick a non-default shared dir; the rules
    file follows it rather than snapping back to a canonical literal."""
    sec = setup_wizard._build_security_section(Path("/opt/custom/evolve"))
    assert sec["rulesFile"] == "/opt/custom/evolve/security_rules.json"


@pytest.mark.parametrize(
    "profile,expected_root",
    [(MACOS, "/Users/Shared/evolve"), (LINUX, "/var/lib/evolve")],
)
def test_rulesfile_matches_review_consumer_default_per_profile(profile, expected_root):
    """End-to-end contract on both profiles: the wizard-written rulesFile
    equals the security reviewer's own fallback (``shared_dir /
    "security_rules.json"`` — analyzer/review.py), so the path the producer
    writes is exactly the path the consumer looks for.

    (conftest's autouse fixture restores the MACOS pin on teardown.)
    """
    set_profile(profile)
    shared_dir = Path(get_profile().shared_dir_default)
    sec = setup_wizard._build_security_section(shared_dir)
    # producer == consumer fallback default
    assert sec["rulesFile"] == str(shared_dir / "security_rules.json")
    # and it resolves to the right platform root
    assert sec["rulesFile"] == f"{expected_root}/security_rules.json"


def test_static_security_fields_are_platform_invariant():
    """Only rulesFile is platform-derived; the rest of the block is a fixed
    shape and must be byte-identical regardless of profile."""
    mac = setup_wizard._build_security_section(Path("/Users/Shared/evolve"))
    lin = setup_wizard._build_security_section(Path("/var/lib/evolve"))
    for block in (mac, lin):
        assert block["mode"] == "primary"
        assert block["botId"] is None
        assert block["autoRejectRisk"] == ["high", "critical"]
    # The two blocks differ in exactly one key — rulesFile.
    assert {k: v for k, v in mac.items() if k != "rulesFile"} == {
        k: v for k, v in lin.items() if k != "rulesFile"
    }
