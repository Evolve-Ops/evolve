"""Hook governance — inventory monitor + baseline + signals.

Spec: internal/spec-hook-governance-2026-05-10.md.

Phase A (this package): observation-only drift monitor that reads
both of OpenClaw's hook surfaces from each bot's openclaw.json:

  - Webhook ingress (``openclaw.json → hooks``): inbound HTTP
    automation surface; bearer-token-gated; RCE-shape risk if the
    token leaks (cf. CVE-2025-59536). Today: not configured on
    any bot.

  - Plugin typed hooks (``openclaw.json → plugins.entries.<id>.hooks``):
    per-plugin policies for allowConversationAccess /
    allowPromptInjection. Today: every bot has the ``evolve``
    plugin with allowConversationAccess=true (set by deploy.py
    since OC 2026.4.29). No other plugin has hook policies.

Phase B will add proposal-driven mutation + transformsDir content
audit when ingress is actually enabled. Phase C is the operator-
facing "enable a webhook" workflow once needed.

The silent-disable detection (hook_plugin_policy_silent_disable)
is the load-bearing signal: it catches the April 2026 incident
class where an OC upgrade silently dropped the evolve plugin's
allowConversationAccess flag.
"""
