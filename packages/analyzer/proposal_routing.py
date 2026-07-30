"""proposal_routing — the single Python source of truth for "where does a
proposal surface in the admin UI".

A proposal that lands on the Recommendations **Pending** actionable inbox is
one whose operator-facing ``surface`` is ``"improvement"`` AND which is NOT
``informational`` (informational = an observation/FYI "look into it" item, which
the page tucks into a calmer Observations sub-block). Everything else —
``surface`` null/firing/drift/cleanup, or any informational item — routes to the
Alerts page or the Observations block, never to Pending.

**Altitude carve-out (capability ideas).** The capability generators
(``app_suggester`` et al.) emit their ideas as ``Investigation``-kind proposals,
which ``INFORMATIONAL_KINDS`` (``arbiter/apply.py``) marks ``informational=True``
— so the rule above would tuck a genuine "build this new capability" idea into
the calmer Observations block, never the actionable inbox. The merged
``altitude`` field is the discriminator: an ``improvement``-surface proposal at
``altitude >= 2`` is a high-altitude capability idea and DOES surface in Pending
even when ``informational`` is true. Generic Investigations (manifest_quality,
config nudges, …) sit at the default ``altitude`` 0 and stay in Observations, so
the carve-out promotes ambition without flooding the inbox. ``altitude`` is
resolved per-proposal-override → charter default → 0 (see the serve-time
``view["altitude"]`` derivation in ``routes_arbiter.py``).

This mirrors the client-side routing in
``packages/admin/evolve_admin/web/static/js/pages/self-improvement.js``
(``_isRecommendable`` + the ``!p.informational`` Inbox filter) and the header
summary pill in ``index.html`` (``loadPageSummary_self_improvement``). Those JS
predicates are the rendering mirror; THIS module is the source. Keep them in
sync — the JS carries a cross-reference comment back here.

Why it lives here: proposals are born in Python (``analyze.py`` mints them,
``review.py`` re-emits the "ready" alert after a security pass). Both emitters
must answer "will the operator actually find this in the Inbox?" the SAME way
the page does, so the "📋 N proposals ready → Recommendations → Inbox"
notification never points at an inbox the proposal categorically cannot appear
in. Having one predicate here prevents the literal from drifting across the two
emitters.
"""

from __future__ import annotations


def is_recommendable(proposal: dict) -> bool:
    """True if the proposal routes to Recommendations (surface=improvement)
    rather than to the Alerts page.

    Mirrors ``_isRecommendable`` in self-improvement.js
    (``p.surface === 'improvement'``). A proposal with ``surface`` null,
    ``firing``, ``drift``, or ``cleanup`` is NOT recommendable — it belongs on
    Alerts.
    """
    return proposal.get("surface") == "improvement"


def proposal_surfaces_in_pending_inbox(proposal: dict) -> bool:
    """True if the proposal lands in the Recommendations **actionable Inbox**
    — the destination the ``decisions.proposal_ready`` notification points the
    operator at ("Recommendations → Inbox").

    This is the recommendable set MINUS informational observations: it matches
    the page's Inbox (``recommendable.filter(p => !p.informational)`` in
    self-improvement.js) and the nav badge. An informational proposal IS
    recommendable but routes to the calmer Observations sub-block, not Pending —
    so a "ready → Pending" push for it would send the operator to an empty
    inbox.

    **Exception — the altitude carve-out (see module docstring):** a
    high-altitude capability idea (``surface == "improvement"`` AND
    ``altitude >= 2``) surfaces in Pending REGARDLESS of ``informational``, so a
    capability generator's ``Investigation``-kind idea reaches the actionable
    inbox instead of being tucked into Observations. ``altitude`` defaults to 0
    when absent, so generic Investigations are unaffected.
    """
    if not is_recommendable(proposal):
        return False
    if int(proposal.get("altitude") or 0) >= 2:
        return True
    return not proposal.get("informational")


def apply_legacy_investigation_routing(proposal: dict) -> dict:
    """Stamp the modern routing fields onto a legacy (pre-charter) analyze.py
    proposal so an ``type == "investigation"`` item routes to the
    Recommendations **Observations** sub-block instead of leaking to Alerts as a
    husk with ``surface`` null.

    Investigations are the "look into it" FYI kind (no automated bot change), so
    they get ``surface="improvement"`` (recommendable) AND ``informational=True``
    (routed to Observations, not the actionable Pending inbox). This matches the
    field shape the page's Observations triage expects for Investigation-kind
    proposals (see ``INFORMATIONAL_KINDS`` in ``arbiter/apply.py`` and the
    serve-time ``view["informational"]`` derivation in ``routes_arbiter.py``).

    Scoped narrowly to ``type == "investigation"``: other legacy
    ``category="alert"`` proposals (config_change, workflow_change) are left
    untouched so genuinely Alerts-bound husks are not pulled into
    Recommendations.

    Mutates and returns ``proposal`` for convenience.
    """
    if proposal.get("type") == "investigation":
        proposal["surface"] = "improvement"
        proposal["informational"] = True
    return proposal
