"""Regression test for the wizard verify/run consistency.

The 2026-06-07 follow-up incident: the wizard's verify step set
``has_evolve_pubkey: true`` by title-substring match ("evolve" in
title), and the UI rendered "✓ Will reuse (already has evolve deploy
key)" in green. The wizard's actual run step compared key BYTES
against the local pubkey via ``_ensure_deploy_key``. When the local
pubkey was the NEW shared key (post-Distribute) but the GitHub deploy
key was an OLD per-bot key titled "evolve-<bot>", the verify lied
and the run produced ``collision: repo exists without evolve deploy
key``. The collision banner offered no [Reuse] affordance because
verify had already declared reuse for that bot.

Fix: verify must use byte comparison against ``_bot_pubkey(bot_id)``
— the same source-of-truth ``_ensure_deploy_key`` uses at run time.

This test pins the byte-match in source so a future refactor doesn't
silently restore the title heuristic.
"""
from __future__ import annotations

from pathlib import Path


def test_verify_uses_byte_match_against_local_pubkey():
    """Pin: _verify_github_pat_for_bot must compare key bytes
    against the value returned by _bot_pubkey. Title-substring is
    only a defensive fallback when bot_id is missing.
    """
    web_dir = Path(__file__).resolve().parent.parent / "evolve_admin" / "web"
    src = "\n".join(p.read_text() for p in sorted(web_dir.glob("*.py")))
    # Slice _verify_github_pat_for_bot.
    start = src.find("def _verify_github_pat_for_bot(")
    assert start > 0
    end = src.find("\n    def ", start + 1)
    body = src[start:end] if end > 0 else src[start:]
    # Required pieces of the byte-match path.
    assert "_bot_pubkey(bot_id)" in body, (
        "verify must compute the local pubkey via _bot_pubkey(bot_id) "
        "to byte-match against the GitHub-registered deploy keys; the "
        "pre-2026-06-07 title heuristic produced false-positive 'reuse' "
        "in front of a wizard run that then collision'd."
    )
    assert 'target_blob' in body
    # Title heuristic remains as a defensive fallback when bot_id is
    # not passed (legacy callers). It must be inside an `else` branch,
    # not the primary path.
    assert "if target_blob:" in body, (
        "byte-match must be the primary check; title heuristic is the "
        "fallback when no local pubkey is available"
    )


def test_verify_call_site_passes_bot_id():
    """The route handler that drives verify must pass bot_id so the
    byte match works. A future edit that drops bot_id silently
    regresses to the title-substring fallback.
    """
    web_dir = Path(__file__).resolve().parent.parent / "evolve_admin" / "web"
    src = "\n".join(p.read_text() for p in sorted(web_dir.glob("*.py")))
    assert "_verify_github_pat_for_bot(\n                tok, login or None, repo_name, bot_id=bot_id," in src, (
        "call site must pass bot_id keyword arg so byte-match path is taken"
    )
