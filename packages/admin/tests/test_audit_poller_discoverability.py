"""Tests for the audit_poller discoverability gate + Signal title fix.

Two changes shipping together — both live in
``packages/admin/evolve_admin/applications/audit_poller.py``:

  (a) Signal title uses the manifest's human-readable ``display_name`` /
      ``name`` instead of the raw ``app_id``. v7-migrated and forge-minted
      apps have ``app_id`` of the form ``i-XXXXXXXX`` which is unreadable
      in the Alerts UI.

  (b) The five ``app_discoverability_*`` assertions stay in the per-app
      audit trail (already written by the bot-side runner) but never
      become Signals. They flag manifest content gaps that the scanner
      auto-repairs on its next pass (see scanner Pass D) or that require
      authoring repair — neither is well-served by pager-style Signals.

Tests use the real signals.store on a temp shared_dir.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterable

import pytest


_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))

from evolve_admin.applications import audit_poller  # noqa: E402


# ── Helpers (parallel to test_audit_poller.py) ──────────────────────────────


def _make_outbox(tmp_root: Path, bot_user: str) -> Path:
    outbox = tmp_root / "Users" / bot_user / ".openclaw" / "workspace" / "evolve" / "audit_outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    return outbox


def _write_finding(outbox: Path, *, bot_id: str, app_id: str,
                   assertion_id: str,
                   severity: str = "major",
                   summary: str = "structural finding",
                   evidence: dict | None = None,
                   record_id: str = "rec-1",
                   audit_run_id: str = "run-1") -> Path:
    rec = {
        "record_id": record_id,
        "audit_run_id": audit_run_id,
        "kind": "tier2_finding",
        "ts": "2026-06-07T00:00:00Z",
        "runner_version": "1.0.0",
        "producer": "app_structural_verifier",
        "bot_id": bot_id,
        "app_id": app_id,
        "signature": f"app_structural_verifier:{assertion_id}:{bot_id}:{app_id}:{record_id}",
        "assertion_id": assertion_id,
        "severity": severity,
        "summary": summary,
        "evidence": evidence or {},
    }
    p = outbox / f"{record_id}.json"
    p.write_text(json.dumps(rec, indent=2))
    return p


@pytest.fixture
def tmp_root(tmp_path: Path, monkeypatch) -> Path:
    """Re-point the poller's /Users paths into tmp_path."""
    def _audit_outbox(bot_user: str) -> Path:
        return tmp_path / "Users" / bot_user / ".openclaw" / "workspace" / "evolve" / "audit_outbox"

    def _audit_ingested(bot_user: str) -> Path:
        return _audit_outbox(bot_user) / "_ingested"

    monkeypatch.setattr(audit_poller, "_audit_outbox_dir", _audit_outbox)
    monkeypatch.setattr(audit_poller, "_audit_outbox_ingested", _audit_ingested)
    return tmp_path


class _StubManifest:
    """Minimal stand-in for ApplicationManifest — only the attrs the
    title resolver reads."""
    def __init__(self, *, display_name: str = "", name: str = "") -> None:
        self.display_name = display_name
        self.name = name


def _patch_load_manifest(monkeypatch, mapping: dict[tuple[str, str], _StubManifest | None]) -> None:
    """Stub the lazy ``from .manifest import load_manifest`` inside
    ``_app_display_name``. ``mapping`` is keyed by ``(app_id, bot_id)``.
    """
    from evolve_admin.applications import manifest as manifest_mod

    def _fake_load(application_id, bot_id, shared_dir):
        return mapping.get((application_id, bot_id))

    monkeypatch.setattr(manifest_mod, "load_manifest", _fake_load)


# ── (a) Signal title uses display_name → name → app_id fallback ─────────────


def test_signal_title_uses_manifest_display_name(
    tmp_root: Path, tmp_path: Path, monkeypatch,
) -> None:
    """When the manifest has a display_name, the Signal title shows it
    instead of the cryptic ``i-XXXXXXXX`` app_id."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "atlas")
    _write_finding(
        outbox, bot_id="atlas", app_id="i-0bcaa46e",
        assertion_id="file_missing", severity="critical",
        evidence={"path": "scripts/foo.py"},
    )
    _patch_load_manifest(monkeypatch, {
        ("i-0bcaa46e", "atlas"): _StubManifest(
            display_name="Atlas Daily Digest", name="atlas-daily-digest",
        ),
    })

    result = audit_poller.poll_bot("atlas", "atlas", shared)
    assert result.findings_ingested == 1

    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1
    body = json.loads(firing[0].read_text())
    # Reports-pass title humanization (2026-06-12): the title is
    # ``{display}: {plain phrase}``, NOT ``{display}: {assertion_id}``.
    assert body["title"] == "Atlas Daily Digest: a file the app needs is missing"
    # The cryptic app_id remains available in details for filtering.
    assert body["details"]["app_id"] == "i-0bcaa46e"


def test_signal_title_falls_back_to_name_when_display_name_empty(
    tmp_root: Path, tmp_path: Path, monkeypatch,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "atlas")
    _write_finding(
        outbox, bot_id="atlas", app_id="i-caaf6944",
        assertion_id="cron_not_in_crontab",
    )
    _patch_load_manifest(monkeypatch, {
        ("i-caaf6944", "atlas"): _StubManifest(
            display_name="", name="atlas-on-demand-research",
        ),
    })

    audit_poller.poll_bot("atlas", "atlas", shared)
    body = json.loads(next((shared / "signals" / "firing").glob("*.json")).read_text())
    # display_name="" → _app_display_name falls back to manifest.name, which
    # the humanized title then completes with the plain-English phrase.
    assert body["title"] == "atlas-on-demand-research: a scheduled job isn't installed"


def test_signal_title_says_an_app_when_manifest_missing(
    tmp_root: Path, tmp_path: Path, monkeypatch,
) -> None:
    """When the manifest can't be loaded (deleted between scan and ingest,
    permissions issue, etc.), the title leads with "An app" rather than
    leaking the cryptic ``i-XXXXXXXX`` app_id (reports-pass title fix). The
    id stays recoverable via ``details.app_id``. Regression guard against a
    NoneType crash + the old ``<id>: <type>`` illegible title."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_b")
    _write_finding(
        outbox, bot_id="team_bot_b", app_id="i-4136968b",
        assertion_id="file_sha_mismatch",
    )
    _patch_load_manifest(monkeypatch, {})  # no entry → returns None

    audit_poller.poll_bot("team_bot_b", "team_bot_b", shared)
    body = json.loads(next((shared / "signals" / "firing").glob("*.json")).read_text())
    assert body["title"] == "An app: a file changed since it was registered"
    assert body["details"]["app_id"] == "i-4136968b"  # id recoverable


# ── (b) Discoverability assertions never become Signals ─────────────────────


_DISCOVERABILITY_ASSERTIONS = [
    "app_discoverability_no_invocation_model",
    "app_discoverability_no_how_to_use",
    "app_discoverability_thin_hint_words",
    "app_discoverability_no_example_triggers",
    "app_discoverability_no_cli",
]


@pytest.mark.parametrize("assertion_id", _DISCOVERABILITY_ASSERTIONS)
def test_discoverability_finding_is_trail_only(
    assertion_id: str, tmp_root: Path, tmp_path: Path, monkeypatch,
) -> None:
    """Every ``app_discoverability_*`` assertion is suppressed at the Signal
    layer. The record is still marked ingested (so the outbox file moves
    to ``_ingested/``) but no Signal is emitted to ``signals/firing/``.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "atlas")
    _write_finding(
        outbox, bot_id="atlas", app_id="atlas-daily-digest",
        assertion_id=assertion_id, severity="major",
        record_id=f"rec-{assertion_id}",
    )
    # The display-name resolver is consulted by other code paths in
    # _ingest_finding but should NOT be reached for trail-only assertions
    # (the gate returns before the title is built). Stub so a stray call
    # would be obvious.
    _patch_load_manifest(monkeypatch, {})

    result = audit_poller.poll_bot("atlas", "atlas", shared)

    # The record was ingested (counter bumps) and archived (file moved
    # out of the outbox into _ingested/).
    assert result.findings_ingested == 1
    assert result.files_processed == 1
    remaining = [p.name for p in outbox.iterdir() if p.is_file()]
    assert remaining == []
    ingested = list((outbox / "_ingested").rglob("*.json"))
    assert len(ingested) == 1

    # But no Signal landed in firing/.
    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert firing == [], (
        f"{assertion_id} should be trail-only — found {len(firing)} Signal(s) "
        f"in firing/: {[p.name for p in firing]}"
    )


def test_non_discoverability_finding_still_emits_signal(
    tmp_root: Path, tmp_path: Path, monkeypatch,
) -> None:
    """Regression guard: the gate must not catch unrelated assertions.
    ``file_missing`` is the canonical Tier 2 finding and must keep firing.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    _write_finding(
        outbox, bot_id="team_bot_a", app_id="team-bot-a-morning",
        assertion_id="file_missing", severity="critical",
        evidence={"path": "scripts/morning.py"},
    )
    _patch_load_manifest(monkeypatch, {
        ("team-bot-a-morning", "team_bot_a"): _StubManifest(
            display_name="Team Bot A Morning Brief",
        ),
    })

    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)
    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1


# ── Gate constants frozen ───────────────────────────────────────────────────


def test_discoverability_gate_constant_matches_structural_verifier() -> None:
    """If the structural verifier grows a new ``app_discoverability_*``
    assertion, this test fails so we remember to add it to the
    trail-only gate. Loads the constants list straight from the verifier
    and compares as sets.
    """
    from app_audit_structural import (  # noqa: E402
        check_discoverability,
    )

    # Build the empty-manifest case which should trigger every check
    # whose model in _USER_ROUTED_MODELS allows it. We use a minimal
    # user-routed manifest with all routing fields missing so the full
    # discoverability surface fires.
    findings = check_discoverability(
        {"id": "x", "name": "X", "usage": {"model": "user-initiated"}}, {},
    )
    surfaced = {f.assertion_id for f in findings}
    # Plus the model-absent variant.
    findings += check_discoverability({"id": "x", "name": "X"}, {})
    surfaced |= {f.assertion_id for f in findings}

    gated = audit_poller._DISCOVERABILITY_TRAIL_ONLY_ASSERTIONS
    missing = surfaced - gated
    assert not missing, (
        f"New discoverability assertion(s) not in the trail-only gate: "
        f"{sorted(missing)} — add them to "
        f"audit_poller._DISCOVERABILITY_TRAIL_ONLY_ASSERTIONS"
    )
