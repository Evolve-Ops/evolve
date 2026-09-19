"""pack.py — archetype starter packs for the `evo add-bot` chain (M3).

Spec: internal/spec-add-bot-wizard-build-delta-2026-06-10.md §5 (Option A,
decided): a starter pack is a meta-spec bundle package in the gallery —
the EA-Pack pattern — that declares its apps as ``app_dependencies`` and
ships no scripts. This module is the wizard's read side: given the
archetype captured in AB_PURPOSE, find the role's bundle, resolve its
dependency list into concrete apps, and price the build so the AB_SHAPE
proposal can say "Four apps, about eight minutes and $1.60 — OK?".

Discovery is data-driven, not a code map: bundles tag themselves
``starter-pack`` plus ``archetype-<archetype>`` in their
``application_tags``, and this module looks both tags up in the gallery
tag index. Adding or retargeting a pack is a gallery edit, no code
change — exactly the Option A rationale.

Honest packs (§5 + §10 OQ4, decided): archetypes whose apps don't exist
in the gallery yet get NO pack and an honest reason the AB_SHAPE prompt
relays — never padding with unrelated apps.

Seam: :func:`set_pack_loader` substitutes the gallery lookup in tests.
"""

# identity: see applications.app_identity.resolve_app_id (AL-1.4b). ``pkg_id`` is the gallery package key of a pack
# member — read off a PACK BUNDLE definition and its declared dependencies, not
# off an installed manifest. It names what to install, so there is nothing on
# disk yet to resolve.

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


# Conservative per-app figures for the conversational preview when the
# real estimator can't run (fresh pod, analyzer import unavailable).
# Builds run one at a time, a couple of minutes each — the preview says
# so rather than promising the §5 example's two-minute total.
ESTIMATE_MINUTES_PER_APP = 2
FALLBACK_APP_USD = 0.40

# The briefing default's gallery apps (delta §6): the Morning Briefing
# behavior and its required data foundation. Finalize ensures these
# queue when the default stays on and the chosen pack didn't already
# include them.
MORNING_BRIEFING_PKG_ID = "p-a9a74bf7"
CALENDAR_SYNC_PKG_ID = "p-fe9acef3"

# Honest fallback when a pack-bearing archetype's bundle can't be loaded
# (sparse or broken gallery) — say so rather than fail the wizard.
NO_PACK_FALLBACK = (
    "the starter set couldn't be loaded just now — apps can be added "
    "any time later, in conversation or from the admin page."
)

# Honest no-pack reasons, keyed by archetype (delta §5 starter map).
# These are relayed by the AB_SHAPE agenda prompt in the bot's voice —
# plain words, no padding, no fake "coming soon" dates.
NO_PACK_REASONS: dict[str, str] = {
    "research-analyst": (
        "the research toolset (a daily digest, link capture, on-demand "
        "research) isn't in the app gallery as a ready-made set yet — "
        "say what doesn't exist plainly. The bot can still have apps "
        "built for it one at a time later, in conversation or from the "
        "admin page."
    ),
    "home-automation": (
        "there's no ready-made app set for home automation yet — say so "
        "plainly rather than padding with unrelated apps. Apps can be "
        "added any time later."
    ),
    "customer-facing": (
        "there's no ready-made starter set for customer-facing bots — "
        "the right apps depend on the business. The app gallery on the "
        "admin page is the place to browse once the bot exists."
    ),
    "custom": (
        "custom bots don't get a preset app list — the app gallery on "
        "the admin page is the place to browse once the bot exists."
    ),
}


@dataclass
class PackApp:
    """One concrete app a starter pack resolves to."""

    pkg_id: str
    name: str
    blurb: str = ""
    est_usd: float = FALLBACK_APP_USD


@dataclass
class StarterPack:
    """A resolved starter pack: the bundle plus its concrete apps."""

    bundle_pkg_id: str
    bundle_name: str
    apps: list[PackApp] = field(default_factory=list)
    # Declared dependencies that don't resolve to a gallery package —
    # surfaced honestly in the proposal, never silently dropped.
    missing: list[str] = field(default_factory=list)


# ── Seam ──────────────────────────────────────────────────────────────────────


PackLoader = Callable[[str, Path], Optional[StarterPack]]
_pack_loader: PackLoader | None = None


def set_pack_loader(fn: PackLoader | None) -> None:
    global _pack_loader
    _pack_loader = fn


def load_starter_pack(
    archetype: str,
    shared_dir: Path,
    *,
    network: dict | None = None,
) -> StarterPack | None:
    """Resolve the starter pack for ``archetype``, or ``None`` when the
    role has no bundle (the honest-pack archetypes). Best-effort: any
    lookup failure returns ``None`` and the wizard says "no pack" —
    a broken gallery must not break bot creation."""
    loader = _pack_loader
    if loader is not None:
        return loader(archetype, shared_dir)
    try:
        return _default_load_starter_pack(archetype, shared_dir, network=network)
    except Exception:
        return None


def _first_sentence(text: str, cap: int = 110) -> str:
    s = (text or "").strip().split(". ")[0].strip().rstrip(".")
    if len(s) > cap:
        s = s[: cap - 1].rstrip() + "…"
    return s


def _default_load_starter_pack(
    archetype: str,
    shared_dir: Path,
    *,
    network: dict | None = None,
) -> StarterPack | None:
    from ...applications.gallery import load_gallery_package, load_tags_index

    tags = load_tags_index(shared_dir)
    starter = set(tags.get("starter-pack") or [])
    for_archetype = set(tags.get(f"archetype-{archetype}") or [])
    matches = sorted(starter & for_archetype)
    if not matches:
        return None
    bundle = load_gallery_package(matches[0], shared_dir)
    if not isinstance(bundle, dict):
        return None

    pack = StarterPack(
        # identity: see resolve_app_id — the bundle and its declared deps name gallery packages to install.
        bundle_pkg_id=str(bundle.get("pkg_id") or matches[0]),
        bundle_name=str(bundle.get("display_name") or bundle.get("name") or "starter set"),
    )
    for dep in bundle.get("app_dependencies") or []:
        if not isinstance(dep, dict):
            continue
        dep_pkg_id = str(dep.get("pkg_id") or "")
        dep_name = str(dep.get("display_name") or dep_pkg_id)
        dep_pkg = load_gallery_package(dep_pkg_id, shared_dir) if dep_pkg_id else None
        if not isinstance(dep_pkg, dict):
            pack.missing.append(dep_name)
            continue
        pack.apps.append(PackApp(
            pkg_id=dep_pkg_id,
            name=str(dep_pkg.get("display_name") or dep_name),
            blurb=_first_sentence(str(dep_pkg.get("description") or "")),
            est_usd=_estimate_app_usd(dep_pkg, network=network),
        ))
    return pack


def _estimate_app_usd(pkg: dict, *, network: dict | None = None) -> float:
    """Mid-point USD projection for one app's build, via the shared
    install-cost estimator (same table the gallery install modal uses).
    Falls back to a flat figure when the estimator is unavailable or
    returns the estimate-unavailable flag — the preview must never
    silently claim $0."""
    try:
        from install_cost_estimator import estimate_install_cost  # type: ignore[import-not-found]
    except ImportError:
        return FALLBACK_APP_USD
    try:
        est = estimate_install_cost(
            "new-bot",
            str(pkg.get("build_spec") or ""),
            network=network or {},
        )
        mid = float(getattr(est, "mid_usd", 0.0) or 0.0)
    except Exception:
        return FALLBACK_APP_USD
    return round(mid, 2) if mid > 0 else FALLBACK_APP_USD


def estimate_minutes(app_count: int) -> int:
    """Build-time preview: the apps build one after another, a couple of
    minutes each."""
    return max(ESTIMATE_MINUTES_PER_APP, int(math.ceil(app_count * ESTIMATE_MINUTES_PER_APP)))


def estimate_usd(apps: list[dict]) -> float:
    """Sum the per-app mid estimates carried on the proposal dicts."""
    total = 0.0
    for a in apps:
        try:
            total += float(a.get("est_usd") or 0.0)
        except (TypeError, ValueError):
            continue
    return round(total or (FALLBACK_APP_USD * len(apps)), 2)
