"""
evolve_admin.oauth.providers — Provider registry for OAuth integrations.

Each provider wraps a per-skill install flow (e.g. gog_install.py) and exposes
a uniform interface that the orchestrator uses to check satisfaction and build
missing-integration descriptors.  New providers are a single new file here —
no other code changes required.

The ``Provider`` dataclass is the interface contract.  Every field is required.
Providers register at import time by appending to ``PROVIDER_REGISTRY``.

Adding a new provider (e.g. V2.1-2 Slack):
  1. Create ``providers/slack_provider.py``.
  2. Populate a ``Provider`` instance and append to ``PROVIDER_REGISTRY``.
  3. Import it at the bottom of this file.

The orchestrator iterates ``PROVIDER_REGISTRY`` on every call — no restart
required as long as the module has been imported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Provider:
    """Interface contract for a single OAuth provider.

    Fields
    ------
    integration_ids : frozenset[str]
        The integration ``id`` values (from a manifest's
        ``requirements.integrations[].id``) that this provider satisfies.
        For GOG: ``frozenset({"gog"})``.  A Slack provider would use
        ``frozenset({"slack"})``.

    skill_id : str
        The canonical skill id backing this provider.  Passed through to the
        missing-item dict so the UI can route to ``/api/skills/install/<skill_id>``.

    is_satisfied : Callable[[str], bool]
        ``is_satisfied(bot_id) → bool`` — True if the bot already has this
        provider fully configured (no missing OAuth steps).  Receives the
        injectable readers via a closure if needed for testability.

    build_missing_item : Callable[[str, dict], dict | None]
        ``build_missing_item(bot_id, req) → dict | None`` — Returns the
        missing-integration descriptor dict (same shape as the ``missing[]``
        list in ``evaluate_install_prerequisites``), or ``None`` if the
        integration is satisfied.  ``req`` is the raw requirements entry from
        the manifest (carries ``display_name``, ``reason``, etc.).

    action_url : Callable[[str], str]
        ``action_url(bot_id) → str`` — The URL the operator visits to complete
        OAuth for this provider on the given bot.

    action_label : str
        Human-readable button label, Plex-test friendly.
        Example: ``"Set up Gmail & Calendar"``.
    """

    integration_ids: frozenset[str]
    skill_id: str
    is_satisfied: Callable[[str], bool]
    build_missing_item: Callable[[str, dict], "dict | None"]
    action_url: Callable[[str], str]
    action_label: str


# ── Registry ──────────────────────────────────────────────────────────────────
# Populated at import time by each providers/*.py module.
# The orchestrator iterates this list on every evaluate_install_prerequisites call.

PROVIDER_REGISTRY: list[Provider] = []


def find_provider(integration_id: str) -> "Provider | None":
    """Look up the provider responsible for an integration id.

    Returns the first matching provider, or None if no provider claims the id.
    O(N × |integration_ids|) — trivially fast for the handful of providers
    we expect in v2.x.
    """
    for provider in PROVIDER_REGISTRY:
        if integration_id in provider.integration_ids:
            return provider
    return None


# ── Provider registrations ────────────────────────────────────────────────────
# Import each provider module here.  Each module appends to PROVIDER_REGISTRY
# at import time.  Order matters only if two providers claim the same
# integration_id (first wins) — which should never happen.

from . import gog_provider  # noqa: E402, F401  — registration side-effect
from . import gmail_provider  # noqa: E402, F401  — registration side-effect (V2.1-3)
from . import calendar_provider  # noqa: E402, F401  — registration side-effect (V2.1-3)
from . import slack_provider  # noqa: E402, F401  — registration side-effect (V2.1-2)
from . import obsidian_provider  # noqa: E402, F401  — registration side-effect (V2.1-5)
# imessage_provider — RE-ENABLED 2026-06-04 with the bundled-plugin rewire
# (PR following docs/openclaw-coverage-audit-2026-06-04.md). Brought back
# alongside the catalog re-add; the orchestrator's "needs gog first" path
# is not triggered for kind=local_system skills, so the V2.1-6-era 202
# awaiting_oauth bug does not recur. The provider exists to register the
# iMessage skill with the install orchestrator so /api/skills/install/
# imessage routes through the same dispatcher as the other channels.
from . import imessage_provider  # noqa: E402, F401  — registration side-effect
from . import whatsapp_provider  # noqa: E402, F401  — registration side-effect (2026-06-04)
from . import signal_provider    # noqa: E402, F401  — registration side-effect (2026-06-04; pending licensing review)
from . import discord_provider  # noqa: E402, F401  — registration side-effect (V2.3-2)
from . import telegram_provider  # noqa: E402, F401  — registration side-effect (V2.3-1)
