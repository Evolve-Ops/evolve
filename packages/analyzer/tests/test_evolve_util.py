"""Tests for evolve_util — the blessed shared-primitive home (Phase 6.2).

Also pins the dup-primitive-lint contract: the repo must stay free of
local re-definitions of these primitives.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from evolve_util import (
    atomic_write_json,
    atomic_write_text,
    now_iso,
    now_iso_micro,
    now_iso_offset,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


# ── atomic_write_text ────────────────────────────────────────────────────────


def test_atomic_write_text_roundtrip(tmp_path):
    p = tmp_path / "out.txt"
    atomic_write_text(p, "hello\n")
    assert p.read_text() == "hello\n"


def test_atomic_write_text_overwrites_existing(tmp_path):
    p = tmp_path / "out.txt"
    p.write_text("old")
    atomic_write_text(p, "new")
    assert p.read_text() == "new"


def test_atomic_write_text_leaves_no_temp_droppings(tmp_path):
    p = tmp_path / "out.txt"
    atomic_write_text(p, "x")
    assert [f.name for f in tmp_path.iterdir()] == ["out.txt"]


def test_atomic_write_text_failure_preserves_original_and_cleans_tmp(tmp_path):
    p = tmp_path / "out.txt"
    p.write_text("original")
    # Lone surrogate can't encode to UTF-8 — f.write raises AFTER the temp
    # file exists, exercising the unlink-on-failure path.
    with pytest.raises(UnicodeEncodeError):
        atomic_write_text(p, "x\udfff")
    assert p.read_text() == "original"
    assert [f.name for f in tmp_path.iterdir()] == ["out.txt"]


def test_atomic_write_json_unserializable_preserves_original(tmp_path):
    p = tmp_path / "out.json"
    p.write_text("{}")
    with pytest.raises(TypeError):
        atomic_write_json(p, {"k": object()})
    assert p.read_text() == "{}"
    assert [f.name for f in tmp_path.iterdir()] == ["out.json"]


def test_atomic_write_text_mode_chmod(tmp_path):
    p = tmp_path / "shared.txt"
    atomic_write_text(p, "x", mode=0o644)
    assert stat.S_IMODE(p.stat().st_mode) == 0o644


def test_atomic_write_text_default_mode_is_private(tmp_path):
    p = tmp_path / "private.txt"
    atomic_write_text(p, "x")
    # mkstemp default — owner-only
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


# ── atomic_write_json ────────────────────────────────────────────────────────


def test_atomic_write_json_roundtrip_and_defaults(tmp_path):
    p = tmp_path / "out.json"
    atomic_write_json(p, {"b": 1, "a": 2})
    raw = p.read_text()
    assert json.loads(raw) == {"b": 1, "a": 2}
    assert raw.startswith("{\n  ")          # indent=2
    assert raw.index('"b"') < raw.index('"a"')  # insertion order preserved


def test_atomic_write_json_sort_keys(tmp_path):
    p = tmp_path / "out.json"
    atomic_write_json(p, {"b": 1, "a": 2}, sort_keys=True)
    raw = p.read_text()
    assert raw.index('"a"') < raw.index('"b"')


# ── timestamps ───────────────────────────────────────────────────────────────


def test_now_iso_format():
    s = now_iso()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", s), s


def test_now_iso_offset_format():
    s = now_iso_offset()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", s), s


def test_now_iso_micro_format():
    s = now_iso_micro()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00", s), s


# ── dup-primitive-lint contract ──────────────────────────────────────────────


def test_dup_primitive_lint_repo_is_clean():
    """The gate the migration established: no local re-definitions of the
    shared primitives anywhere in production code. If this fails, someone
    added a `def _atomic_write` / `def _now_iso` / `def _bot_home` copy —
    import the blessed one from evolve_util / evolve_config instead."""
    lint = _REPO_ROOT / "tools" / "dup-primitive-lint"
    r = subprocess.run(
        [sys.executable, str(lint), "--all"],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, f"dup-primitive-lint found violations:\n{r.stdout}{r.stderr}"
