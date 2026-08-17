"""tests/test_cost_profiles_prune_cache_coherence.py — prune ttl ≤ cache ttl.

Regression lock for the 2026-07-31 cost investigation.

``contextPruning.mode = "cache-ttl"`` reads like "keep context for ttl". It is
the opposite. OC SKIPS pruning while the prompt cache is still warm, because
pruning rewrites the prefix and would discard a live cache::

    if (ttlMs > 0 && Date.now() - lastTouch < ttlMs) return;   // don't prune yet

So ``ttl`` means "how long to wait before pruning is free", and the correct
value is the effective prompt-cache TTL. OC's own default is 300s, matching
Anthropic's 5-minute default cache.

The ``balanced`` profile — and deploy.py's ``_BALANCED_COST_DEFAULTS``, which
mirrors it and is what every bot actually deploys with — carried ``"4h"``.
Anthropic's longest cache TTL is 1 hour, so 4h was incoherent under EVERY
possible cache configuration: tool results went untrimmed for the 3h window
after the cache died, and each expiry re-wrote all of that untrimmed bulk.

Observed consequence on the reference pod: a 24.8x cache-traffic-to-content
ratio, and 59% of one day's spend going to cache writes and reads rather than
to the tokens the work actually produced.

Locked here:
  1. No built-in profile sets a prune ttl above Anthropic's 1h cache maximum.
  2. deploy.py's deploy-time default obeys the same bound — it is the value
     that actually ships, so a profile-only fix would not have helped.
  3. A profile that opts into `cache_retention: "long"` prunes no earlier than
     the cache it just extended (pruning while warm throws away the cache).
  4. The two sources agree with each other.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import cost_profiles  # noqa: E402

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Effective prompt-cache TTL per cache_retention value. Anthropic offers exactly
# two: the 5-minute default, and 1 hour via cache_control.ttl = "1h".
_CACHE_TTL_SECONDS = {"long": 3600, "short": 300, None: 300}


def _parse_ttl(value: str) -> int:
    """Parse OC's ``<n><unit>`` duration into seconds."""
    m = re.fullmatch(r"(\d+)\s*([smhd])", str(value).strip())
    assert m, f"unparseable ttl {value!r}"
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2)]


def _prune_ttl(settings: dict) -> str | None:
    cp = settings.get("contextPruning") or {}
    if cp.get("mode") != "cache-ttl":
        return None  # pruning disabled — ttl is inert
    return cp.get("ttl")


def _deploy_defaults() -> dict:
    from evolve_admin.deploy import _BALANCED_COST_DEFAULTS

    return _BALANCED_COST_DEFAULTS


@pytest.mark.parametrize("profile_name", sorted(cost_profiles.BUILTIN_PROFILES))
def test_builtin_profile_prune_ttl_within_cache_maximum(profile_name):
    """No profile may wait longer to prune than a cache can possibly live."""
    settings = cost_profiles.BUILTIN_PROFILES[profile_name]["settings"]
    ttl = _prune_ttl(settings)
    if ttl is None:
        return  # pruning off (unrestricted-debug) — nothing to bound

    assert _parse_ttl(ttl) <= cost_profiles.MAX_COHERENT_PRUNE_TTL_SECONDS, (
        f"profile {profile_name!r} sets contextPruning.ttl={ttl!r}, above "
        f"Anthropic's 1h cache maximum — pruning would never fire while the "
        f"cache is dead, and each expiry re-writes untrimmed tool results"
    )


@pytest.mark.parametrize("profile_name", sorted(cost_profiles.BUILTIN_PROFILES))
def test_builtin_profile_prune_ttl_equals_its_own_cache_ttl(profile_name):
    """Prune ttl must EQUAL the cache ttl the profile configured — not merely
    fall inside a range.

    Tightened from `>=` on 2026-07-31. The loose bound admitted the exact
    regression it was meant to stop: reverting `cache_retention` to "short"
    while leaving a 1h prune ttl passed `>=` (1h >= 5min) and passed the 1h
    maximum, yet left the cache dying 12x more often than pruning was allowed
    to run — untrimmed context re-written on every expiry.

    Both directions are wrong:
      prune < cache  -> prunes while warm, discards a paid-for cache
      prune > cache  -> cache dies first, untrimmed bulk re-written repeatedly
    """
    settings = cost_profiles.BUILTIN_PROFILES[profile_name]["settings"]
    ttl = _prune_ttl(settings)
    if ttl is None:
        return

    cache_ttl = _CACHE_TTL_SECONDS[settings.get("cache_retention")]
    assert _parse_ttl(ttl) == cache_ttl, (
        f"profile {profile_name!r} prunes after {ttl} but configured a "
        f"{cache_ttl}s cache. These are not independently choosable — use "
        f"cost_profiles.prune_ttl_for_cache_retention()"
    )


@pytest.mark.parametrize("retention,expected", [
    ("long", "1h"), ("short", "5m"), (None, "5m"),
])
def test_derivation_covers_every_cache_retention_value(retention, expected):
    """The derivation is the single source of truth — pin its table."""
    assert cost_profiles.prune_ttl_for_cache_retention(retention) == expected


def test_profiles_use_the_derivation_not_a_hand_picked_value():
    """Every profile's prune ttl must be exactly what the derivation returns
    for that profile's own cache_retention. Guards against someone editing one
    of the two numbers and not the other."""
    for name, profile in cost_profiles.BUILTIN_PROFILES.items():
        settings = profile["settings"]
        ttl = _prune_ttl(settings)
        if ttl is None:
            continue
        expected = cost_profiles.prune_ttl_for_cache_retention(
            settings.get("cache_retention"))
        assert ttl == expected, f"{name}: prune ttl {ttl!r} != derived {expected!r}"


def test_default_cache_retention_is_gated_pending_phase_0():
    """`long` is an unvalidated bet until the Phase 0 experiment answers.

    Held at "short" deliberately — see the comment on _DEFAULT_CACHE_RETENTION
    and docs/incident-post-mortem-2026-07-31-cost-containment.md §2. Flipping
    this is a decision, not a tidy-up; this test exists so it cannot be flipped
    by accident.
    """
    assert cost_profiles._DEFAULT_CACHE_RETENTION == "short"


def test_deploy_default_prune_ttl_within_cache_maximum():
    """The deploy-time default is what actually ships to every bot.

    The 2026-07-31 defect lived in BOTH cost_profiles and deploy.py. Fixing
    only the profile would have left every deployed bot on the bad value,
    because deploy.py's constant is what `deploy` writes.
    """
    ttl = _prune_ttl(_deploy_defaults())
    assert ttl is not None, "deploy default unexpectedly disables pruning"
    assert _parse_ttl(ttl) <= cost_profiles.MAX_COHERENT_PRUNE_TTL_SECONDS


def test_deploy_default_agrees_with_balanced_profile():
    """deploy.py mirrors the balanced profile — they must not drift apart."""
    balanced = cost_profiles.BUILTIN_PROFILES["balanced"]["settings"]
    assert _prune_ttl(_deploy_defaults()) == _prune_ttl(balanced)


def test_the_specific_regression_4h_is_rejected():
    """Pin the exact bad value, so a revert fails loudly rather than silently."""
    assert _parse_ttl("4h") > cost_profiles.MAX_COHERENT_PRUNE_TTL_SECONDS
    for name, profile in cost_profiles.BUILTIN_PROFILES.items():
        assert _prune_ttl(profile["settings"]) != "4h", f"{name} regressed to 4h"
    assert _prune_ttl(_deploy_defaults()) != "4h", "deploy default regressed to 4h"
