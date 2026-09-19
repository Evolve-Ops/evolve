"""evolve_admin.skills — skill-shaped install flows over the existing OpenClaw
plugin + provider machinery.

Skills are the audience's vocabulary (per
:doc:`project_openclaw_market_reality_may2026`). OpenClaw + ClawHub talk in
plugins and providers; this package wraps both behind a single "install a
skill" abstraction so the UI and templates can drive setup without knowing
which plugin / OAuth provider / keystore profile is involved.

Sub-modules:
- :mod:`evolve_admin.skills.gog_install` — Gmail + Calendar (GOG) install flow (Spec 11 / A3)
- :mod:`evolve_admin.skills.obsidian_install` — Obsidian vault filesystem skill (V1.5-2)
- :mod:`evolve_admin.skills.obsidian_helpers` — vault read/write helpers (V1.5-2)
- :mod:`evolve_admin.skills.inventory` — per-bot + pod-level inventory (Spec 12)

Inventory API is re-exported at package level so callers and tests can
``from evolve_admin.skills import SkillEntry, ...`` as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from platform_profile import PlatformProfile

from . import inventory as _inventory  # noqa: F401  (also enables `skills.inventory.X` patching)
from .inventory import (  # noqa: F401
    SkillEntry,
    SkillInventory,
    get_bot_skills,
    get_pod_skills,
    _PLUGIN_DISPLAY,
    _OAUTH_PROVIDERS,
    _read_oc_json,
    _resolve_plugin_status,
    _resolve_mcp_status,
    _read_app_skill_deps,
    _pwd,
    subprocess,
)


def supported_on_host(
    entry: Mapping[str, Any],
    profile: "PlatformProfile | None" = None,
) -> bool:
    """True when a catalog entry may be OFFERED on this pod host's platform.

    Channel-matrix platform honesty (internal/design-linux-port-2026-06-10.md
    §8): a Linux pod must never render a dead iMessage affordance. The
    platform constraint is catalog DATA — a ``platforms: [<profile name>,
    ...]`` field on the skill's ``SKILL_REGISTRY_ENTRY`` (absent or empty
    means platform-neutral, the default for every channel but iMessage).
    Offering surfaces (the catalog list/detail routes, install begin
    routes, any wizard channel step) call this helper instead of naming
    skills, per the no-provider-literals-in-logic convention.

    This gates *offering* only. Inventory/status surfaces stay
    unfiltered — they report what is actually installed, and a pod
    migrated from macOS may truthfully carry channel state the current
    host can't mint.
    """
    platforms = entry.get("platforms")
    if not platforms:
        return True
    if profile is None:
        from platform_profile import get_profile

        profile = get_profile()
    return profile.name in platforms
