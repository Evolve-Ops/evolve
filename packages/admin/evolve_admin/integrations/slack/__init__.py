"""evolve_admin.integrations.slack — Slack policy validator + probe.

Phase 1 of docs/spec-slack-policy-2026-05-13.md. Reads each bot's existing
openclaw.json (and workspace .env when the bot token isn't in
``channels.slack``) and runs a set of checks that map to the silent-failure
modes that have repeatedly broken Slack-integrated bots in the pod (most
recently team_bot_a on 2026-05-12). Phase 2 will introduce a ``slack-policy.json``
upstream of openclaw.json and migrate these checks to consume it; the
check codes (``SLK001``..``SLK010``) stay stable across that migration.

Two entry points:

- :func:`doctor.run_doctor` — synchronous, per-bot, returns a
  :class:`doctor.DoctorResult` for use by the CLI, pre-deploy hook, and
  future write-path gate.
- :func:`probe.run_probe` — runs the doctor across every bot in the pod
  and mirrors findings into the Signal store via
  ``signals.store.observe`` + ``sweep_resolve``.

The Slack HTTP client (:mod:`.slack_client`) is intentionally minimal
(urllib, no external deps) — the doctor needs only ``auth.test``,
``users.conversations``, ``conversations.list``, ``conversations.info``,
and ``users.info`` to satisfy every Phase 1 check.
"""
