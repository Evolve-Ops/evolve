"""B1 progressive disclosure — the apps reference file + stub.

Spec: docs/spec-evolve-overhead-budget-2026-07-31.md Phase B1. The coherence
vocab, weekly app posture, and app-repair playbook (~64% of the session_start
injection) ship via ONE on-demand workspace file plus a compact inline stub.

Pins:
  1. The reference doc contains all provided sections and the read-on-demand
     framing.
  2. The stub names the relpath and instructs reading before app work.
  3. ensure_apps_reference: writes, is idempotent (unchanged content skips
     the rewrite), and returns None on an unwritable destination (caller
     falls back to inlining — content is never silently lost).
  4. build_session_prefix: with the stub and inline_coherence_vocab=False,
     the vocab body is absent and the stub present; default arguments keep
     the legacy inline behavior byte-for-byte.
"""

from __future__ import annotations

from pathlib import Path

import session_surface as ss


def test_reference_doc_contains_sections() -> None:
    doc = ss.build_apps_reference_doc("VOCAB BODY", "POSTURE BODY", "REPAIR BODY")
    assert "VOCAB BODY" in doc and "POSTURE BODY" in doc and "REPAIR BODY" in doc
    assert "Read this when" in doc
    # Absent sections are simply omitted, not rendered as empty gaps.
    slim = ss.build_apps_reference_doc("VOCAB BODY", "", "")
    assert "POSTURE" not in slim


def test_stub_names_relpath_and_instructs_reading() -> None:
    stub = ss.build_apps_reference_stub(
        ss.APPS_REFERENCE_RELPATH, ["app-finding vocabulary"])
    assert str(ss.APPS_REFERENCE_RELPATH) in stub
    assert "READ" in stub
    assert stub.startswith("[APPS REFERENCE")


def test_ensure_apps_reference_writes_and_is_idempotent(tmp_path: Path) -> None:
    dest = ss.ensure_apps_reference(tmp_path, "content v1\n")
    assert dest is not None and dest.read_text() == "content v1\n"
    mtime = dest.stat().st_mtime_ns
    # Unchanged content: no rewrite (mtime stable).
    assert ss.ensure_apps_reference(tmp_path, "content v1\n") == dest
    assert dest.stat().st_mtime_ns == mtime
    # Changed content: rewritten.
    ss.ensure_apps_reference(tmp_path, "content v2\n")
    assert dest.read_text() == "content v2\n"


def test_ensure_apps_reference_unwritable_returns_none(tmp_path: Path) -> None:
    blocker = tmp_path / "evolve"
    blocker.write_text("not a directory")  # mkdir under it must fail
    assert ss.ensure_apps_reference(tmp_path, "content\n") is None


def test_prefix_stub_mode_drops_vocab_and_keeps_stub() -> None:
    stub = ss.build_apps_reference_stub(
        ss.APPS_REFERENCE_RELPATH, ["app-finding vocabulary"])
    out = ss.build_session_prefix(
        apps_reference_stub=stub, inline_coherence_vocab=False)
    assert "[APPS REFERENCE" in out
    if ss.COHERENCE_VOCAB:
        assert ss.COHERENCE_VOCAB not in out
    # Conduct summary always leads regardless of mode.
    assert out.startswith(ss.POD_CONDUCT_SUMMARY)


def test_prefix_default_mode_is_legacy_inline() -> None:
    out = ss.build_session_prefix()
    assert "[APPS REFERENCE" not in out
    if ss.COHERENCE_VOCAB:
        assert ss.COHERENCE_VOCAB in out
