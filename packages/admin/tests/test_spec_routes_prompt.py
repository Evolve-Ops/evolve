"""tests/test_spec_routes_prompt.py

Regression tests for ``_SPEC_SYSTEM_PROMPT`` in ``spec_routes.py`` — the
system prompt that drives the LLM-backed app spec generator.

Bug history: PR #1862 added path-C MCP bridge tools (gmail_send,
drive_write_file, calendar_create_event) for Google services, and PR
#1864 wired the integration. But the spec-gen prompt was never updated
to teach the LLM about these tools. Result: every app spec generated
after PR #1862 referenced the legacy bearer-token-via-openclaw.json
pattern, which is absent on path-C bots — so forged apps would be
dead-on-arrival at runtime for any bot configured with path C only.

This test pins the load-bearing fragments of the prompt that teach the
LLM about the new auth path. A future refactor that drops them must
loudly fail here.
"""

from __future__ import annotations

import sys
from pathlib import Path


_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))


def test_spec_system_prompt_names_all_three_path_c_mcp_tools():
    """All three Google MCP tools must be named in the prompt. Without
    this, the LLM can't reference them in the generated build_spec."""
    from evolve_admin.web import spec_routes

    prompt = spec_routes._SPEC_SYSTEM_PROMPT
    for tool in ("gmail_send", "drive_write_file", "calendar_create_event"):
        assert tool in prompt, (
            f"Spec-gen prompt is missing path-C MCP tool name {tool!r}. "
            f"The LLM cannot generate apps that target this tool without "
            f"knowing its name. See packages/admin/evolve_admin/mcp_bridge/"
            f"google_tools.py for the registered tool set."
        )


def test_spec_system_prompt_names_google_integration_config_key():
    """The decision rule path-C-vs-legacy hinges on the LLM knowing the
    config field name. Without it, the LLM can't reason about which auth
    path applies to the target bot."""
    from evolve_admin.web import spec_routes

    prompt = spec_routes._SPEC_SYSTEM_PROMPT
    assert "google_integration" in prompt, (
        "Spec-gen prompt is missing the 'google_integration' config key — "
        "the field name the LLM must check to decide path-C vs legacy. "
        "See packages/admin/evolve_admin/google_auth.py for the schema."
    )


def test_spec_system_prompt_warns_against_legacy_pattern_for_path_c():
    """The prompt should call out the legacy bearer-token-via-openclaw.json
    pattern as a MUST NOT for path-C bots. Without the explicit warning,
    the LLM defaults to the legacy pattern (which it has lots of training
    data for) and silently produces dead-on-arrival apps."""
    from evolve_admin.web import spec_routes

    prompt = spec_routes._SPEC_SYSTEM_PROMPT
    # The anti-pattern callout must reference openclaw.json AND mark it as
    # legacy / must-not.
    assert "openclaw.json" in prompt, (
        "Spec-gen prompt is missing reference to openclaw.json — needed "
        "to identify the legacy auth-token pattern as an anti-pattern for "
        "path-C bots."
    )
    # Either the word "MUST NOT" or "legacy" should appear in context;
    # we check for the legacy framing.
    assert "legacy" in prompt.lower(), (
        "Spec-gen prompt should mark the openclaw.json bearer-token "
        "pattern explicitly as 'legacy' to discourage the LLM from "
        "defaulting to it for path-C bots."
    )


def test_spec_system_prompt_documents_draft_dont_commit_pattern():
    """For path-C apps, the recommended architecture is: app produces
    structured drafts, bot reads the drafts and invokes the MCP tool.
    The prompt should document this so apps don't try to import urllib
    and call Gmail directly."""
    from evolve_admin.web import spec_routes

    prompt = spec_routes._SPEC_SYSTEM_PROMPT
    # The pattern name or its essence must appear.
    has_pattern = (
        "DRAFT-DON'T-COMMIT" in prompt
        or "draft-don't-commit" in prompt.lower()
        or (
            "draft" in prompt.lower()
            and "approve" in prompt.lower()
            and "bot" in prompt.lower()
        )
    )
    assert has_pattern, (
        "Spec-gen prompt should describe the draft-don't-commit pattern "
        "(app produces drafts → operator/user approves → bot invokes MCP "
        "tool to send/upload/create). Without this guidance the LLM may "
        "generate apps that send mail without operator approval."
    )


def test_spec_system_prompt_documents_requirements_declaration_for_google_apps():
    """Apps that use Google services should declare their auth requirements
    so the install-time requirements check can flag missing setup before
    runtime. The prompt should guide the LLM toward emitting the right
    requirements.integrations block."""
    from evolve_admin.web import spec_routes

    prompt = spec_routes._SPEC_SYSTEM_PROMPT
    # Both auth-mode IDs should be named so the LLM knows which to emit.
    assert "google_path_c" in prompt, (
        "Spec-gen prompt should name the 'google_path_c' requirement id "
        "so the LLM emits the right requirements.integrations entry."
    )
    assert '"id": "gmail"' in prompt or "'id': 'gmail'" in prompt or "id: gmail" in prompt or "{\"id\": \"gmail\"" in prompt, (
        "Spec-gen prompt should also name the legacy 'gmail' requirement "
        "id so the LLM can declare flexible apps that support both auth "
        "paths."
    )
