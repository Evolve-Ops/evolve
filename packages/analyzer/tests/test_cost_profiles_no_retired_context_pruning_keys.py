"""Golden-JSON regression lock for
breaker-notify-survives-openclaw-config-validation item 2.

OpenClaw 2026.9.2 retired ``agents.defaults.contextPruning.keepLastAssistants``
outright (a strict-schema ``Unrecognized key``, not a rename — "tier-eval
tranche" retirement, canonical built-in defaults now apply). Both
``cost_profiles.py``'s built-in profiles and ``deploy.py``'s deploy-time
default used to write it, which is how it ended up baked into the ``evolve``
service account's own openclaw.json and blocked the notify path's ``openclaw``
CLI invocation from starting at all.

Same two-sources-must-agree shape as
``test_cost_profiles_prune_cache_coherence.py``'s prune-ttl lock: a
profile-only fix would have left every already-shipped default (deploy.py's)
still writing the bad key.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import cost_profiles  # noqa: E402

_RETIRED_KEY = "keepLastAssistants"


def _deploy_defaults() -> dict:
    from evolve_admin.deploy import _BALANCED_COST_DEFAULTS

    return _BALANCED_COST_DEFAULTS


@pytest.mark.parametrize("profile_name", sorted(cost_profiles.BUILTIN_PROFILES))
def test_builtin_profile_does_not_emit_the_retired_key(profile_name):
    settings = cost_profiles.BUILTIN_PROFILES[profile_name]["settings"]
    cp = settings.get("contextPruning") or {}
    assert _RETIRED_KEY not in cp, (
        f"profile {profile_name!r} still writes contextPruning.{_RETIRED_KEY}, "
        f"which OpenClaw 2026.9.2 rejects as an unrecognized key"
    )


def test_deploy_default_does_not_emit_the_retired_key():
    """deploy.py is what actually ships to every bot — fixing only the
    profile would leave every deployed bot on the bad default."""
    cp = _deploy_defaults().get("contextPruning") or {}
    assert _RETIRED_KEY not in cp
