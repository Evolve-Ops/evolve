"""fit_review.archetypes — per-archetype targeting playbooks.

Each declared archetype (see ``bot_purpose.ARCHETYPES``) carries a *playbook*:
the set of activity domains that archetype is *for*. The targeting step scores
each observed noun against the declared archetype's playbook to decide whether a
recurring activity is **purpose-aligned** (``confirmed``) or merely **emergent**.

The domain vocabulary here is deliberately the same string space as two things
it must line up with, so the cross-reference in ``targeting.py`` is a direct set
operation rather than a fuzzy keyword match:

  * **observation nouns** — the rolled-up ``noun`` axis of the observation tuples
    (``task-management``, ``document-generation``, ``slack-comms``, ``calendar``,
    ``home-management``, ``health-fitness``, ``email`` …), and
  * **gallery ``application_tags``** — the tags on the real installable apps
    in ``gallery/index.json`` (mirrored into ``packages/gallery/catalog.json``,
    which is a generated projection of the index).

Because the live observation store already emits nouns drawn from this shared
vocabulary (verified read-only on the pod 2026-06-12; see spec §1.1), a
"purpose-aligned, above-floor, gallery-matched" need is a clean set
intersection: ``aligned_domains ∩ observed_nouns ∩ app.application_tags``.

Playbooks are intentionally conservative — they list the domains an archetype
*is for*, not every domain it *could touch*. A domain the playbook omits surfaces
as ``emergent``, which the targeting floor handles separately (spec §3.3). The
``custom`` archetype has no canonical playbook: a bot with a freeform mission but
no recognized archetype yields no ``confirmed`` alignment, only ``emergent`` —
exactly right, since we have no declared lens to anchor a capability claim.
"""

from __future__ import annotations

# Domain → set-of-domains playbooks. Keys MUST be members of
# ``bot_purpose.ARCHETYPES``. Each value is the frozenset of activity domains the
# archetype is declared to serve. Extend by adding an archetype + its domains
# here (and in ``bot_purpose.ARCHETYPES``); no other code change is needed.
_PLAYBOOKS: dict[str, frozenset[str]] = {
    "personal-assistant": frozenset(
        {
            "calendar",
            "email",
            "task-management",
            "travel",
            "home-management",
            "health-fitness",
            "reminders",
            "scheduling",
        }
    ),
    "project-manager": frozenset(
        {
            "task-management",
            "document-generation",
            "slack-comms",
            "calendar",
            "email",
            "ops",
            "status-reporting",
            "coordination",
        }
    ),
    "home-automation": frozenset(
        {
            "home-management",
            "task-management",
            "calendar",
            "reminders",
            "scheduling",
        }
    ),
    "research-analyst": frozenset(
        {
            "research",
            "document-generation",
            "data-analysis",
            "summarization",
            "task-management",
        }
    ),
    "customer-facing": frozenset(
        {
            "email",
            "slack-comms",
            "document-generation",
            "task-management",
            "support",
        }
    ),
    # No canonical playbook — a freeform-mission bot has no declared lens, so
    # nothing is "confirmed". Kept explicit so the membership check is total.
    "custom": frozenset(),
}


def aligned_domains_for(archetype: str | None) -> frozenset[str]:
    """Return the domains a declared archetype is for, or empty for unknown.

    Lowercases + strips the archetype. An archetype outside the playbook table
    (including ``custom`` and any not-yet-given-a-playbook value) yields an empty
    set, which makes every noun ``emergent`` — the conservative default.
    """
    key = (archetype or "").strip().lower()
    return _PLAYBOOKS.get(key, frozenset())


def has_playbook(archetype: str | None) -> bool:
    """True if the archetype has a non-empty canonical playbook."""
    return bool(aligned_domains_for(archetype))


def composed_playbook_for(archetype: str | None):
    """Base domain playbook + best-practice enrichment — what the reflection runner reads.

    The base ``aligned_domains`` (above) is the cheap targeting lens. This adds the
    edr-market-intel-distilled best-practice capability patterns ("what a good bot
    of this kind actually does") that the L2 reflection step uses as its "what good
    looks like" reference. The corpus lives in its own module
    (``playbook_bestpractices``) so it composes onto — rather than inline-bloats —
    this file; the merge is base-preserving (targeting domains are never widened
    here). Returns a ``playbook_bestpractices.ComposedPlaybook``. Lazy import keeps
    this a single self-contained call and avoids any import-order coupling.
    """
    from fit_review.playbook_bestpractices import compose_playbook

    return compose_playbook(archetype, aligned_domains_for(archetype))
