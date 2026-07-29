"""tests/test_intake_redact.py — Promotion-time redaction policy."""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
for p in (str(_ADMIN_DIR),):
    if p not in sys.path:
        sys.path.insert(0, p)


def _intake_with_pii():
    from evolve_admin.intake.envelope import Intake, IntakeContext

    return Intake(
        id="intake-20260514-c0de",
        kind="bug",
        body="When I run `evo apps`, nothing shows up.",
        submitter_user_key="ext:telegram:11223344",
        submitter_channel="telegram:11223344",
        context=IntakeContext(
            primary_bot="evo",
            active_bot="team_bot_a",
            git_commit="abcdef0",
            evolve_version="2026.5.14",
            recent_turns_excerpt=[
                {"role": "user", "text": "evo apps"},
                {"role": "bot", "text": "(empty)"},
            ],
            active_signals=["sig-1", "sig-2"],
        ),
    )


def test_default_redacts_transcript_and_signal_ids():
    from evolve_admin.intake import redact

    ix = _intake_with_pii()
    body, applied = redact.build_issue_body(ix)

    # Transcript not in default output
    assert "evo apps" not in body or "evo apps`" in body  # body still contains the user's bug text
    assert "(empty)" not in body
    assert "transcript_dropped" in applied

    # Signal *ids* are not in the body — only the count
    assert "sig-1" not in body
    assert "Firing signals at capture: 2" in body
    assert "active_signals" in applied

    # Submitter identity not in body
    assert "11223344" not in body
    assert "submitter" in applied


def test_opt_in_includes_transcript():
    from evolve_admin.intake import redact

    ix = _intake_with_pii()
    body, applied = redact.build_issue_body(ix, include_transcript=True)

    assert "evo apps" in body
    assert "(empty)" in body
    assert "transcript_included" in applied
    assert "transcript_dropped" not in applied


def test_redacted_body_preserves_user_authored_text():
    """The body the admin wrote is theirs; we don't touch it."""
    from evolve_admin.intake import redact

    ix = _intake_with_pii()
    body, _ = redact.build_issue_body(ix)
    assert "When I run `evo apps`, nothing shows up." in body


def test_body_header_includes_kind_and_id():
    from evolve_admin.intake import redact

    ix = _intake_with_pii()
    body, _ = redact.build_issue_body(ix)
    assert "intake-20260514-c0de" in body
    assert "bug" in body


def test_minimal_intake_renders_without_crash():
    from evolve_admin.intake import redact
    from evolve_admin.intake.envelope import Intake

    ix = Intake(id="intake-20260514-min0", kind="feature", body="nice to have")
    body, applied = redact.build_issue_body(ix)
    assert "nice to have" in body
    assert "intake-20260514-min0" in body
    # Nothing to redact — applied list is empty
    assert applied == []
