"""Sysadmin alert dispatcher and signal-store-driven notifier.

See ``internal/spec-alert-notifier-2026-05-09.md`` for design. The single
public entrypoint is :func:`alerts.dispatcher.send` — every source that
pushes to the operator's chat channel routes through it so the operator
has one place to mute, throttle, or audit pod-wide notifications.
"""
