"""Evo wizard engine — multi-turn primary-user onboarding.

Spec: internal/spec-evo-wizard-2026-05-05.md §4.

Architecture (hybrid state-machine + LLM):

  * State machine owns the agenda — which phase, what's been extracted,
    when to advance. Persisted at ``{shared_dir}/wizard/{bot_id}/{user_key}.json``
    via :mod:`evo.wizard.state`.
  * The bot's normal LLM does the talking (in the bot's voice), prompted
    each turn with a systemAppend block built by :mod:`evo.wizard.prompts`.
  * A separate, server-side LLM call extracts structured fields from the
    user's reply (:mod:`evo.wizard.extractor`). This call is decoupled from
    the bot's voice — extraction failures don't pollute output.

5a MVP scope (this slice):

  * 3 primary phases: Greet (kickoff + extract name) → About you (gather
    role, environment, current_tooling) → Wrap (commit profile, end)
  * No secondary wizard, no Platform tour, no Goals, no Gallery recs, no
    Guide drafting, no Forge. Those come in slice 5b.
  * No pause/skip/resume yet — only ``in_progress`` and ``completed``.
"""
