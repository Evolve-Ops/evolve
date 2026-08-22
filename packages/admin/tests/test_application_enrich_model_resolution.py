"""Regression tests for `evolve-admin application enrich` model resolution.

`cli._generate_full_manifest_with_llm` called `_resolve_tier3()` without ever
defining or importing it. Because the call sits inside a broad
``try/except Exception: return {}``, the resulting NameError was swallowed
silently — enrich never crashed, it just never invoked the LLM and every
manifest fell back to the non-enriched defaults.

The behavioral test below asserts the subprocess is actually *invoked* with a
resolved model string, which fails if any name in the invocation expression is
undefined. The pyflakes test guards the whole module against the same class of
bug.
"""

from __future__ import annotations

import io
import json
import subprocess
import types
from pathlib import Path

import pytest


def _fake_detected_app() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        name="Test App",
        description="A test application",
        evidence_files=[],
    )


def test_enrich_invokes_openclaw_with_resolved_model(monkeypatch):
    from evolve_admin import cli

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"identity": {"purpose": "x"}}), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    out = cli._generate_full_manifest_with_llm(_fake_detected_app(), "team-bot-a", None)

    # The except-Exception wrapper hides NameErrors — the real assertion is
    # that the subprocess was reached at all.
    assert calls, (
        "openclaw run was never invoked — an undefined name in the invocation "
        "expression raised before subprocess.run and was swallowed"
    )
    cmd = calls[0]
    assert cmd[:3] == ["openclaw", "run", "--model"]
    assert isinstance(cmd[3], str) and cmd[3], "resolved model must be a non-empty string"
    assert out == {"identity": {"purpose": "x"}}


def test_cli_module_has_no_undefined_names():
    pyflakes_api = pytest.importorskip("pyflakes.api")
    from pyflakes.reporter import Reporter

    from evolve_admin import cli

    out, err = io.StringIO(), io.StringIO()
    pyflakes_api.checkPath(str(Path(cli.__file__)), Reporter(out, err))
    undefined = [
        line for line in out.getvalue().splitlines() if "undefined name" in line
    ]
    assert not undefined, "\n".join(undefined)
