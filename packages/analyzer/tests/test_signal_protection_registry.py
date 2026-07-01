"""Tests for the fresh-pod alert-protection coverage registry + lint.

Spec: docs/spec-transient-signal-suppression-2026-06-23.md (§ "Protection by
construction").

The registry (`signals.protection_registry`) declares the fresh-pod posture for
every operator-facing producer; `tools/signal-protection-lint` enforces that no
new Signal producer ships without one. These tests pin the required proof
artifacts:

  * a NEW unregistered store.observe() producer call site → lint FAILS;
  * ALL current producers → lint PASSES (registry backfilled, strict reconcile);
  * a `none`+justification producer → PASSES (not over-gated);
  * a `none` WITHOUT justification → FAILS.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

from signals import protection_registry as registry
from signals.protection_registry import (
    KIND_SIGNAL,
    MATURITY,
    NONE,
    SETTLE,
    ProducerProtection,
)

_TOOL = Path(__file__).resolve().parents[3] / "tools" / "signal-protection-lint"


def _load_tool():
    loader = importlib.machinery.SourceFileLoader("signal_protection_lint", str(_TOOL))
    spec = importlib.util.spec_from_loader("signal_protection_lint", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["signal_protection_lint"] = mod
    loader.exec_module(mod)
    return mod


lint = _load_tool()


# ── Registry internal consistency ────────────────────────────────────────────
def test_registry_is_internally_consistent():
    assert registry.consistency_errors() == []


def test_registry_has_substantive_backfill():
    # Sanity floor: the backfill enumerated the real producer landscape, not a
    # token entry or two.
    assert len(registry.PROTECTIONS) >= 80


def test_every_claimed_file_exists_on_disk():
    # A renamed/deleted producer module would silently un-claim its observe site;
    # pin the emits_from paths to real files.
    repo_root = _TOOL.resolve().parents[1]
    missing = [
        f for f in registry.claimed_files() if not (repo_root / f).is_file()
    ]
    assert missing == [], f"emits_from references nonexistent files: {missing}"


def test_gaps_are_all_none_posture():
    for e in registry.gaps():
        assert e.is_none, f"{e.producer}: gap=True but not a 'none' posture"
        assert e.justification.strip(), f"{e.producer}: gap entry missing justification"


# ── Proof: none + justification rule (the escape hatch) ──────────────────────
def test_none_with_justification_passes():
    e = ProducerProtection(
        "critical_thing",
        (NONE,),
        KIND_SIGNAL,
        justification="Gateway down must page immediately, fresh pod or not.",
    )
    assert registry.entry_errors(e) == []


def test_none_without_justification_fails():
    e = ProducerProtection("loud_thing", (NONE,), KIND_SIGNAL)  # no justification
    errs = registry.entry_errors(e)
    assert errs and "requires a justification" in errs[0]


def test_none_cannot_combine_with_a_gate():
    e = ProducerProtection(
        "confused", (NONE, SETTLE), KIND_SIGNAL, justification="x"
    )
    errs = registry.entry_errors(e)
    assert any("NONE is exclusive" in m for m in errs)


def test_gated_producer_needs_no_justification():
    e = ProducerProtection("gated_thing", (SETTLE, MATURITY), KIND_SIGNAL)
    assert registry.entry_errors(e) == []


# ── Proof: all current producers pass; an unregistered site fails ────────────
def test_all_current_producers_pass_file_coverage():
    targets = lint._iter_production_py()
    assert lint.find_unregistered(targets) == []


def test_strict_name_reconciliation_passes():
    # Every PRODUCER_SEVERITY producer + every dispatcher source is registered.
    assert lint.reconcile_missing() == []


def test_lint_all_strict_exit_zero(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["signal-protection-lint", "--all", "--strict"])
    assert lint.main() == 0


def test_new_unregistered_observe_site_fails(tmp_path):
    fixture = tmp_path / "fresh_monitor.py"
    fixture.write_text(
        "from signals import store as signals_store\n"
        "def run(shared_dir):\n"
        "    signals_store.observe(shared_dir, producer='brand_new', "
        "type='x', state='firing')\n",
        encoding="utf-8",
    )
    found = lint.find_unregistered([fixture])
    assert len(found) == 1
    rel, lines = found[0]
    assert rel.endswith("fresh_monitor.py")
    assert lines == [3]


def test_generator_mod_observe_is_not_a_signal_emit(tmp_path):
    # `proposals = mod.observe(ctx)` (single positional, no kwargs) returns
    # Proposals — it must NOT be mistaken for a store emit, or every generator
    # file would false-trip the lint.
    fixture = tmp_path / "gen.py"
    fixture.write_text(
        "def run(mod, ctx):\n"
        "    proposals = mod.observe(ctx)\n"
        "    return proposals\n",
        encoding="utf-8",
    )
    assert lint.observe_lines(fixture) == []
    assert lint.find_unregistered([fixture]) == []


def test_observe_detection_ignores_comments_and_docstrings(tmp_path):
    fixture = tmp_path / "doc.py"
    fixture.write_text(
        '"""This module calls signals.store.observe(shared_dir, **spec) a lot."""\n'
        "def run():\n"
        "    # signals_store.observe(shared_dir, **spec)  <- not a real call\n"
        "    return 1\n",
        encoding="utf-8",
    )
    assert lint.observe_lines(fixture) == []


def test_store_module_is_exempt():
    # signals/store.py defines observe()/observe_from_opik() — it is the API,
    # not a producer, and must never trip the lint.
    store_py = Path(registry.__file__).with_name("store.py")
    assert lint._is_exempt(store_py)


def test_gaps_are_reported(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["signal-protection-lint", "--gaps"])
    assert lint.main() == 0
    out = capsys.readouterr().out
    assert "gap" in out.lower()
    # spot-check a known gap producer surfaces with its owner note
    assert "weekly_bot_trends" in out
