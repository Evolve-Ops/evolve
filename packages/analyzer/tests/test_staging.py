"""Tests for analyzer/staging.py — TOCTOU-safe /tmp staging (roadmap 2.10)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from staging import secure_stage  # noqa: E402


def test_roundtrip_content_and_default_mode() -> None:
    p = secure_stage('{"a": 1}')
    try:
        assert p.read_text() == '{"a": 1}'
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert p.name.startswith("evolve-stage-")
        assert p.suffix == ".json"
        assert p.parent == Path("/tmp")
    finally:
        p.unlink(missing_ok=True)


def test_md_suffix_stays_inside_sudoers_glob() -> None:
    # The sudoers grants match /tmp/evolve-*.md — the staged name must too.
    p = secure_stage("# hi", suffix=".md")
    try:
        assert p.name.startswith("evolve-")
        assert p.name.endswith(".md")
    finally:
        p.unlink(missing_ok=True)


def test_names_are_unpredictable() -> None:
    a = secure_stage("x")
    b = secure_stage("x")
    try:
        assert a != b
    finally:
        a.unlink(missing_ok=True)
        b.unlink(missing_ok=True)


def test_explicit_mode_644() -> None:
    p = secure_stage("y", mode=0o644)
    try:
        assert stat.S_IMODE(p.stat().st_mode) == 0o644
    finally:
        p.unlink(missing_ok=True)


def test_cleanup_on_write_failure(monkeypatch) -> None:
    """If the write fails, the staged file must not be left behind."""
    import staging as staging_mod

    created: list[str] = []
    real_mkstemp = staging_mod.tempfile.mkstemp

    def tracking_mkstemp(**kwargs):
        fd, name = real_mkstemp(**kwargs)
        created.append(name)
        return fd, name

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(staging_mod.tempfile, "mkstemp", tracking_mkstemp)
    monkeypatch.setattr(staging_mod.os, "chmod", boom)
    try:
        secure_stage("z")
        raise AssertionError("expected OSError")
    except OSError:
        pass
    assert created and not os.path.exists(created[0])
