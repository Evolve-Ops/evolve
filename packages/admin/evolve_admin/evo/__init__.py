"""Evo subcommand surface.

Spec: internal/spec-evo-wizard-2026-05-05.md.

This package owns the user-facing ``evo`` keyword surface. The plugin detects
``evo``/``evolve`` (with an optional subcommand token) and posts to
``/api/evo/dispatch``; this package resolves the calling user's role
(primary/secondary), looks up the subcommand handler, and returns either a
``systemAppend`` block for the bot's LLM to echo or a ``direct_message`` for
the plugin to send out-of-band (e.g. via Telegram Bot API).

v1 scope (this commit):
  * Subcommand registry + ``evo help`` rendering.
  * Identity resolution (with a fallback that treats every sender as primary
    when ``network.json`` has no ``primary_user`` block recorded for the bot).
  * Stubs for ``wizard``, ``guide``, ``apps``, ``profile``, ``default``,
    ``continuity``.
  * Bare ``evo`` and ``evo better`` are NOT routed through this dispatcher;
    the plugin keeps its existing direct-Telegram path for those, to avoid
    regressing the legacy keyword flow. A follow-up PR refactors them through
    here once the wizard engine lands.
"""
