"""Platform-keyed path literals the wizard writes into ``network.json``.

8.3 Linux port (docs/design-linux-port-2026-06-10.md §6 + the wizard-port
census docs/census-linux-wizard-remainder-2026-06-11.md, Step 14 / W3).

Originally this file pinned ``security.rulesFile`` to the operator-chosen
shared dir on both profiles: the wizard used to hardcode a macOS
``/Users/Shared/evolve/security_rules.json``, which was latent on macOS but a
real defect under ``EVOLVE_PLATFORM=linux`` (a Linux pod's shared dir is
``/var/lib/evolve``, so the configured rules file could never be found).

``rulesFile`` — with ``mode`` and ``autoRejectRisk`` — was retired 2026-08-14
along with the last of review.py's advertising surfaces (#3641 deleted the
reviewer itself). The W3 regression it guarded is now structurally impossible
rather than merely correct: the block the wizard builds contains no path at
all. These tests pin that stronger property, so a future field that
reintroduces a platform literal here fails at PR time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin import setup_wizard  # noqa: E402


# ── _build_security_section: shape + platform invariance ─────────────────────


def test_security_block_is_botid_only():
    """The review.py knobs are gone; ``botId`` is the one live field.

    It is read by ``evolve-admin repair-security_bot`` (default target),
    ``deploy._pod_readable_users``, and the wizard's own primary-context-bot
    exclusion — so it must keep shipping, defaulting to "no security bot".
    """
    sec = setup_wizard._build_security_section()
    assert sec == {"botId": None}


@pytest.mark.parametrize("retired", ["mode", "autoRejectRisk", "rulesFile"])
def test_retired_review_knobs_are_not_written(retired):
    """A fresh pod must not be given a knob that configures nothing.

    Each of these named review.py's behavior. Re-adding one to the builder
    without a live consumer re-creates the phantom-gate class #3641 closed.
    """
    assert retired not in setup_wizard._build_security_section()


@pytest.mark.parametrize("profile", [MACOS, LINUX])
def test_security_block_carries_no_platform_path(profile):
    """W3, generalized: no value in this block may be a filesystem path, so
    a macOS literal cannot leak into a Linux pod's network.json.

    (conftest's autouse fixture restores the MACOS pin on teardown.)
    """
    set_profile(profile)
    sec = setup_wizard._build_security_section()
    for key, value in sec.items():
        assert not isinstance(value, (str, Path)) or not str(value).startswith("/"), (
            f"security.{key} is an absolute path ({value!r}) — derive it from "
            f"the operator-chosen shared dir, never a platform literal"
        )


def test_security_block_is_platform_invariant():
    """The block is a fixed shape: byte-identical on both profiles."""
    set_profile(MACOS)
    mac = setup_wizard._build_security_section()
    set_profile(LINUX)
    lin = setup_wizard._build_security_section()
    assert mac == lin
