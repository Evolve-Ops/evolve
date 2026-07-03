"""bot_config_integrity — pod-wide checker for structural openclaw.json bugs.

Scope
=====

This generator catches structural integrity issues in a bot's
openclaw.json that:

  - The operator can't tell are broken by looking at the bot's behaviour
    (they only surface as runtime failures later — atlas's "no openai key",
    team_bot_a's silently-dropped google/xai/openai tier entries).
  - Aren't covered by an existing Signal-producing monitor.
  - Have a known mechanical fix the operator just needs to approve.

It's the natural home for "did setup actually finish?" / "are these
two related fields in sync?" / "is there an obviously missing default?"
class of checks. Each check is registered in
``bot_config_integrity.checks`` and emits zero or more Proposals from
the same daily scan; the dispatcher in ``observe.py`` reads each bot's
openclaw.json once and fans out to every check.

Architecture
============

  generators/bot_config_integrity/
  ├── __init__.py            this file
  ├── charter.yaml           one charter covering all checks
  ├── observe.py             dispatcher — reads openclaw.json, runs checks
  └── checks/
      ├── __init__.py        explicit registry of check functions
      ├── catalog_tier_drift.py
      └── (future)           one file per added check

Adding a new check means:
  1. Drop ``checks/<name>.py`` with a ``run(ctx, cfg) -> list[Proposal]``
     function.
  2. Add it to the ``CHECKS`` list in ``checks/__init__.py``.
  3. If the check needs a new typed Action, add it to
     ``schema.proposal`` + write an applier in ``arbiter.appliers``;
     expand the charter's ``action_kind_allowed`` invariant.

No new scheduler entry, no new context factory, no new tests scaffold.
The dispatcher does the per-bot iteration; each check is pure.

Currently shipped checks
========================

  - **catalog_tier_drift** (correctness, ~0.95 confidence)
    Tier definitions reference models not in ``agents.defaults.models``;
    OC silently drops them at runtime. Discovered with team_bot_a 2026-05-28.
    Emits ``ReconcileModelCatalog`` to add the missing models.

  - **catalog_provider_coverage** (suggestion, ~0.65 confidence)
    Bot has auth-profile credentials for an LLM provider but no catalog
    models from that provider — the operator can't use the provider's
    models even though they're paying for the key. Emits
    ``ReconcileModelCatalog`` to add the RECOMMENDED tier1/2/3 entries
    for that provider. Lower confidence because the operator may have
    deliberately omitted them.

Naming
======

Picked ``bot_config_integrity`` over alternatives (``config_checker``,
``openclaw_invariants``, ``structural_audit``) because:

  - "integrity" signals the failure mode (broken in subtle ways) rather
    than the action (auditing). This is the bug class operators care
    about — "my bot looks set up but doesn't work right."
  - "bot_config" scopes correctly: only per-bot openclaw.json. Pod-wide
    settings (baseline, network.json) are out of scope; they have their
    own generators / monitors.
"""
