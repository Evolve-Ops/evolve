"""tests/test_evo_delimiters.py — visual delimiters around evo direct-send
content.

Until OC's ``before_agent_reply`` hook fires for Telegram user-message
triggers (currently gated on ``trigger === "cron"`` in pi-embedded
2026.4.29 / 5.12 — see hook-runner discovery in PR), the plugin can't
suppress the LLM's chime-in alongside direct-sent messages. The mitigation
is twofold:

  1. Frame direct-sent content with visible delimiters so the operator
     can distinguish trustworthy from hallucinated.
  2. Inject a stay-silent directive via ``before_prompt_build``'s
     ``appendSystemContext`` (which OC actually consumes — unlike
     systemAppend on before_model_resolve which it silently drops).

This file covers (1) — the Python side. The plugin TS adds the
matching directive referencing these delimiters.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────


def test_wrap_adds_open_and_close_delimiters():
    from evolve_admin.evo import _delimiters as d
    body = "Hello, world."
    wrapped = d.wrap(body)
    assert wrapped.startswith(d.EVO_DELIMITER_OPEN)
    assert wrapped.endswith(d.EVO_DELIMITER_CLOSE)
    assert body in wrapped


def test_wrap_preserves_body_content_intact():
    """The body content inside the delimiters is untouched — important
    for markdown / code / URLs that the user must see exactly."""
    from evolve_admin.evo import _delimiters as d
    body = (
        "**Step 1 of 4**\n\n"
        "Paste: `https://example.com/api/admin/onboard/google/callback`\n"
        "Or reply `cancel` to abort."
    )
    wrapped = d.wrap(body)
    assert body in wrapped


def test_wrap_with_title_folds_into_opener():
    """A title argument replaces the default ``evo`` label inside the
    open delimiter — ``═══ evo commands ═══`` instead of the
    ``═══ evo ═══`` + ``**evo commands**`` visual stutter. Close
    delimiter stays unchanged (it's a structural marker, not a label)."""
    from evolve_admin.evo import _delimiters as d
    wrapped = d.wrap("body line", title="evo commands")
    assert wrapped.startswith("═══ evo commands ═══")
    assert wrapped.endswith(d.EVO_DELIMITER_CLOSE)
    # Default opener no longer appears literally — it's been replaced.
    assert d.EVO_DELIMITER_OPEN not in wrapped


def test_wrap_with_empty_or_whitespace_title_falls_back_to_default():
    """Defensive: empty / whitespace title must NOT produce
    ``═══  ═══`` (broken visual). Fall back to the default opener."""
    from evolve_admin.evo import _delimiters as d
    assert d.wrap("body", title="").startswith(d.EVO_DELIMITER_OPEN)
    assert d.wrap("body", title="   ").startswith(d.EVO_DELIMITER_OPEN)
    assert d.wrap("body", title=None).startswith(d.EVO_DELIMITER_OPEN)


def test_is_wrapped_recognizes_custom_title_form():
    """Idempotency must hold for the custom-title form too — otherwise
    ``DispatchResult.__post_init__`` would double-wrap on a TurnResult
    that was already wrapped with a custom title."""
    from evolve_admin.evo import _delimiters as d
    wrapped = d.wrap("body", title="evo commands")
    assert d.is_wrapped(wrapped) is True
    # Negative: a body that merely contains ═══ in passing text is not wrapped.
    assert d.is_wrapped("see ═══ this is not a delimiter") is False


def test_dispatch_result_threads_title_through_wrap():
    """Handler sets ``direct_send_title``; the wrap in __post_init__
    uses it. This is the surface that `evo help`'s renderer relies on
    to fold ``**evo commands**`` into the delimiter row."""
    from evolve_admin.evo.dispatch import DispatchResult
    r = DispatchResult(
        subcommand="help",
        role="primary",
        mode="speak",
        direct_send_message="  • evo help — show available commands",
        direct_send_title="evo commands",
    )
    assert r.direct_send_message.startswith("═══ evo commands ═══")


def test_wrap_is_idempotent_via_is_wrapped_check():
    """``is_wrapped`` allows callers to skip re-wrapping — the wizard
    + dispatch __post_init__ both run on the same body sometimes
    (TurnResult → DispatchResult), and we don't want nested delimiters."""
    from evolve_admin.evo import _delimiters as d
    once = d.wrap("body")
    assert d.is_wrapped(once) is True
    assert d.is_wrapped("plain text") is False
    assert d.is_wrapped("") is False


def test_wrap_passes_through_empty():
    """Empty body → empty result; safe to call from auto-routing code
    that may run with no body."""
    from evolve_admin.evo import _delimiters as d
    assert d.wrap("") == ""
    assert d.wrap(None) is None


# ─────────────────────────────────────────────────────────────────────────────
# TurnResult auto-wrap (wizard direct-send)
# ─────────────────────────────────────────────────────────────────────────────


def test_turn_result_auto_wraps_direct_send_message():
    """Constructing a TurnResult on a verbatim phase auto-wraps the
    body with delimiters so the user can spot the canonical content."""
    from evolve_admin.evo.wizard import engine as _engine
    from evolve_admin.evo.wizard import phases as _phases
    from evolve_admin.evo import _delimiters as d

    body = "**Step 1 of 4 — admin URL**"
    r = _engine.TurnResult(
        system_append=body,
        wizard_session_id="ext:telegram:1",
        phase=_phases.PHASE_GOOGLE_SETUP_ADMIN_URL,
        completed=False,
    )
    assert r.direct_send_message is not None
    assert r.direct_send_message.startswith(d.EVO_DELIMITER_OPEN)
    assert r.direct_send_message.endswith(d.EVO_DELIMITER_CLOSE)
    assert body in r.direct_send_message


def test_turn_result_does_not_double_wrap():
    """Idempotent — a body that's already wrapped doesn't get re-wrapped."""
    from evolve_admin.evo.wizard import engine as _engine
    from evolve_admin.evo.wizard import phases as _phases
    from evolve_admin.evo import _delimiters as d

    pre_wrapped = d.wrap("inner body")
    r = _engine.TurnResult(
        system_append="x",  # ignored when direct_send_message is explicit
        direct_send_message=pre_wrapped,
        wizard_session_id=None,
        phase=_phases.PHASE_GOOGLE_SETUP_INTRO,
        completed=False,
    )
    # Count occurrences — should be exactly one open delimiter
    assert r.direct_send_message.count(d.EVO_DELIMITER_OPEN) == 1


def test_agenda_phase_does_not_auto_wrap():
    """Agenda phases don't direct-send (the LLM engages naturally), so
    nothing to wrap. direct_send_message stays None."""
    from evolve_admin.evo.wizard import engine as _engine
    from evolve_admin.evo.wizard import phases as _phases

    r = _engine.TurnResult(
        system_append="LLM agenda content",
        wizard_session_id="ext:telegram:1",
        phase=_phases.PHASE_GREET,
        completed=False,
    )
    assert r.direct_send_message is None


# ─────────────────────────────────────────────────────────────────────────────
# DispatchResult auto-wrap (non-wizard evo subcommand direct-send)
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_result_auto_wraps_direct_send_message():
    """``evo help`` etc. populate direct_send_message; the wrap fires
    in __post_init__ regardless of which handler set the field."""
    from evolve_admin.evo.dispatch import DispatchResult
    from evolve_admin.evo import _delimiters as d

    r = DispatchResult(
        subcommand="help",
        role="primary",
        mode="speak",
        direct_send_message="**evo commands**\n  • evo help — show…",
    )
    assert r.direct_send_message.startswith(d.EVO_DELIMITER_OPEN)
    assert r.direct_send_message.endswith(d.EVO_DELIMITER_CLOSE)


def test_dispatch_result_with_no_direct_send_unchanged():
    """When direct_send_message is None (wizard-start dispatch paths
    typically), no wrap fires; the field stays None."""
    from evolve_admin.evo.dispatch import DispatchResult

    r = DispatchResult(
        subcommand="wizard",
        role="primary",
        mode="speak",
        system_append="agenda systemAppend",
    )
    assert r.direct_send_message is None


# ─────────────────────────────────────────────────────────────────────────────
# Heads-up note in wizard intro prompts
# ─────────────────────────────────────────────────────────────────────────────


def test_setup_google_intro_includes_heads_up():
    """``evo setup-google`` is multi-turn and is where the LLM-chime-in
    pain is most visible. Intro carries the heads-up so the operator
    knows what to expect."""
    from evolve_admin.evo.wizard.prompts import render_google_setup_intro
    from evolve_admin.evo import _delimiters as d
    body = render_google_setup_intro(bot_id="admin_bot")
    assert d.EVO_HEADS_UP_NOTE in body


def test_app_create_describe_intro_includes_heads_up():
    """``evo app create`` is also multi-turn; same heads-up."""
    from evolve_admin.evo.wizard.prompts import render_app_create_describe_intro
    from evolve_admin.evo import _delimiters as d
    body = render_app_create_describe_intro()
    assert d.EVO_HEADS_UP_NOTE in body
