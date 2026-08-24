"""install_profile.py — Resolve feature-profile and per-feature flags from install.json.

This is the gating layer for "power features" — capabilities that ship in the
codebase but should not run on every install. The motivating example is
``upstream_issues_watcher``: a normal household install shouldn't poll GitHub for
upstream maintainer replies, but a developer install should.

Resolution order for whether a named feature is enabled:

  1. ``install.json::features.<name>.enabled`` — explicit per-feature override
     (always wins, in either direction)
  2. The profile default for that feature, based on
     ``install.json::feature_profile`` (one of ``standard`` | ``developer`` |
     ``minimal``)
  3. ``False`` — features are off unless something opts them on

The profile defaults are declared in :data:`PROFILE_DEFAULTS` below. To add a new
power feature, add its name to the ``developer`` set (and any other profile that
should default it on). The catalog is intentionally small — most features
shouldn't need feature-gating at all; only ones that meaningfully change what the
install does to its environment.

See ``internal/spec-upstream-issue-watcher-2026-05-22.md`` for the rationale.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

# ── Canonical paths ──────────────────────────────────────────────────────────

CANONICAL_SHARED_DIR = Path("/Users/Shared/evolve")
DEFAULT_INSTALL_JSON = CANONICAL_SHARED_DIR / "install.json"

# ── Profile vocabulary ───────────────────────────────────────────────────────

FeatureProfile = Literal["standard", "developer", "minimal"]
VALID_PROFILES: tuple[FeatureProfile, ...] = ("standard", "developer", "minimal")
DEFAULT_PROFILE: FeatureProfile = "standard"

# Per-profile default feature sets. A feature listed here is enabled-by-default
# for installs on that profile (unless explicitly overridden via
# install.json::features.<name>.enabled = false).
#
# IMPORTANT: keep this catalog narrow. A feature only belongs here if it has a
# real "should this install be doing this?" question. Routine
# monitors/dashboards/etc. don't need gating.
PROFILE_DEFAULTS: dict[FeatureProfile, frozenset[str]] = {
    "standard": frozenset(),
    "developer": frozenset({
        "upstream_issues_watcher",
        # inbound_issues_watcher polls tracked-target repos where the operator
        # holds maintainer/triage perms and captures new third-party issues as
        # Intakes for the Triage queue UI (Phase 4 of the Issue Inbox). Same
        # rationale as upstream_issues_watcher for being developer-tier: only
        # installs whose operator is actively maintaining a public-ish repo
        # benefit from it.
        "inbound_issues_watcher",
    }),
    "minimal": frozenset(),
}


# ── Public API ───────────────────────────────────────────────────────────────


def get_feature_profile(install_json: Path = DEFAULT_INSTALL_JSON) -> FeatureProfile:
    """Return the active feature profile for this install.

    Falls back to :data:`DEFAULT_PROFILE` when install.json is missing,
    unreadable, or the ``feature_profile`` field is absent / unrecognized.
    """
    data = _read_install_json(install_json)
    raw = data.get("feature_profile") if isinstance(data, dict) else None
    if isinstance(raw, str) and raw in VALID_PROFILES:
        return raw  # type: ignore[return-value]
    return DEFAULT_PROFILE


def is_feature_enabled(
    feature_name: str,
    install_json: Path = DEFAULT_INSTALL_JSON,
) -> bool:
    """Return True if ``feature_name`` is enabled on this install.

    Resolution: explicit override > profile default > False.
    """
    data = _read_install_json(install_json)
    if not isinstance(data, dict):
        # No install.json — treat as a fresh install with default profile.
        return feature_name in PROFILE_DEFAULTS[DEFAULT_PROFILE]

    # 1. Explicit per-feature override always wins.
    features = data.get("features")
    if isinstance(features, dict):
        entry = features.get(feature_name)
        if isinstance(entry, dict) and "enabled" in entry:
            return bool(entry["enabled"])

    # 2. Profile default.
    profile = get_feature_profile(install_json)
    return feature_name in PROFILE_DEFAULTS[profile]


def get_feature_config(
    feature_name: str,
    install_json: Path = DEFAULT_INSTALL_JSON,
) -> dict[str, Any]:
    """Return the per-feature config block from install.json, or ``{}``.

    Use this for non-enable-flag settings like ``poll_interval_minutes``,
    ``repos``, ``audience``, etc. The caller is responsible for applying
    defaults to missing keys.
    """
    data = _read_install_json(install_json)
    if not isinstance(data, dict):
        return {}
    features = data.get("features")
    if not isinstance(features, dict):
        return {}
    entry = features.get(feature_name)
    if not isinstance(entry, dict):
        return {}
    return entry


# ── Internals ────────────────────────────────────────────────────────────────


def _read_install_json(path: Path) -> dict[str, Any] | None:
    """Read install.json; return None on any failure (missing / unreadable / malformed).

    Deliberately permissive — gating decisions must never crash. A missing or
    corrupt install.json should fall through to safe defaults (profile=standard,
    no power features on).
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
