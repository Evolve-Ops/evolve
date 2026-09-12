"""Proof artifact for R2 — app_audit_tier3 proposal legibility + dedup.

Spawned by META:rsi (the RSI proposal-quality coordinator). Pins the
recommendation-legibility contract
(internal/design-recommendation-legibility-2026-06-12.md) on the
``app_audit_tier3`` generator:

  1. Legibility — every emitted Proposal carries a non-empty ``summary``,
     ``human_title``, and ``coalesce_key``; ``claim`` stays None (an
     Investigation finding has no falsifiable verify-after-apply predicate).
  2. Dedup grain — ``coalesce_key`` is ``app_audit_tier3:{bot}:{app}`` (NO
     audit_run_id), so N findings for one (bot, app) fold into ONE parent —
     and so do findings from *different runs*, even when supersede never
     fires. That cross-run fold is the property that stops the 118-empty-
     card pile-up the 2026-06-12 pod review found from recurring.
  3. Narrow suppression — ``dismiss_signature`` is the per-finding identity,
     never the (bot, app) coalesce grain, so dismissing one finding can't
     suppress all of an app's audit advisories.

Pure in-memory: real arbiter.store on a temp shared_dir, no Anthropic
calls, no LaunchDaemons, no real bot users.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))

from evolve_admin.applications import audit_poller  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_outbox(tmp_root: Path, bot_user: str) -> Path:
    outbox = (
        tmp_root / "Users" / bot_user
        / ".openclaw" / "workspace" / "evolve" / "audit_outbox"
    )
    outbox.mkdir(parents=True, exist_ok=True)
    return outbox


def _tier3_record(
    *,
    bot_id: str,
    app_id: str,
    record_id: str,
    audit_run_id: str = "run-1",
    category: str = "broken_path",
    severity: str = "major",
    description: str = "scripts/x.py invokes a venv python that may not exist",
    sig_suffix: str = "abc",
    outcome: str = "propose",
) -> dict:
    return {
        "record_id": record_id,
        "audit_run_id": audit_run_id,
        "kind": "tier3_finding",
        "ts": "2026-06-12T00:00:00Z",
        "runner_version": "1.1.0",
        "producer": "app_audit_tier3",
        "bot_id": bot_id,
        "app_id": app_id,
        "signature": f"app_audit_tier3:{category}:{bot_id}:{app_id}:{sig_suffix}",
        "obs_id": f"obs-{sig_suffix}",
        "category": category,
        "severity": severity,
        "description": description,
        "evidence": ["scripts/x.py:42"],
        "outcome": outcome,
        "rationale": "operator should review",
    }


def _write_tier3(outbox: Path, record: dict) -> Path:
    p = outbox / f"{record['record_id']}.json"
    p.write_text(json.dumps(record, indent=2))
    return p


@pytest.fixture
def tmp_root(tmp_path: Path, monkeypatch) -> Path:
    def _audit_outbox(bot_user: str) -> Path:
        return (
            tmp_path / "Users" / bot_user
            / ".openclaw" / "workspace" / "evolve" / "audit_outbox"
        )

    def _audit_ingested(bot_user: str) -> Path:
        return _audit_outbox(bot_user) / "_ingested"

    monkeypatch.setattr(audit_poller, "_audit_outbox_dir", _audit_outbox)
    monkeypatch.setattr(audit_poller, "_audit_outbox_ingested", _audit_ingested)
    return tmp_path


def _pending(shared: Path) -> list[dict]:
    pending = shared / "proposals" / "pending"
    if not pending.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(pending.glob("*.json"))]


# ── 1. Legibility fields are populated ───────────────────────────────────────


def test_tier3_proposal_carries_legible_fields(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """The card is no longer empty-fielded: summary + human_title +
    coalesce_key are all non-empty; claim stays None (correct for an
    Investigation finding)."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    _write_tier3(outbox, _tier3_record(
        bot_id="team_bot_a", app_id="journal", record_id="rec-1",
    ))

    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    pending = _pending(shared)
    assert len(pending) == 1
    body = pending[0]
    assert body["generator_id"] == "app_audit_tier3"
    # The four fields the pod review flagged as empty are now populated...
    assert (body.get("summary") or "").strip()
    assert (body.get("human_title") or "").strip()
    assert (body.get("coalesce_key") or "").strip()
    assert (body.get("dismiss_signature") or "").strip()
    # ...except claim, which MUST stay None — never fabricate a falsifiable
    # claim for an investigate-only finding.
    assert body.get("claim") is None


def test_tier3_summary_projects_description_not_category_headline(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """The card title is the terse category headline; the summary surfaces
    the finding's OWN description so the two lines aren't redundant."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    desc = "morning_brief.py raises SystemExit(1) when the gateway is down"
    _write_tier3(outbox, _tier3_record(
        bot_id="team_bot_a", app_id="journal", record_id="rec-1",
        category="behavior_mismatch", description=desc,
    ))

    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    body = _pending(shared)[0]
    # Title = category headline.
    assert body["problem"] == (
        "journal: code doesn't match what the manifest claims"
    )
    # Summary = the finding's description (distinct from the title).
    assert body["summary"] == desc
    assert body["summary"] != body["problem"]


# ── 2. Coalesce grain is (bot, app), and folds across runs ───────────────────


def test_tier3_coalesce_key_is_bot_app_grain(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """coalesce_key carries no audit_run_id component."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    _write_tier3(outbox, _tier3_record(
        bot_id="team_bot_a", app_id="journal", record_id="rec-1",
        audit_run_id="run-xyz",
    ))

    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    body = _pending(shared)[0]
    assert body["coalesce_key"] == "app_audit_tier3:team_bot_a:journal"
    assert "run-xyz" not in body["coalesce_key"]


def test_tier3_multiple_findings_one_bot_app_coalesce_to_one_card(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """Three findings for one (bot, app) fold into ONE parent + 2
    sub_findings — the card-count collapse the contract requires."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    for i in range(3):
        _write_tier3(outbox, _tier3_record(
            bot_id="team_bot_a", app_id="journal",
            record_id=f"rec-{i}", sig_suffix=f"s{i}",
        ))

    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    pending = _pending(shared)
    assert len(pending) == 1
    assert len(pending[0].get("sub_findings") or []) == 2
    # The parent title stays count-agnostic (the UI badge carries the count).
    assert pending[0]["human_title"] == "journal: audit findings"


def test_tier3_cross_run_findings_coalesce_even_without_supersede(
    tmp_path: Path,
) -> None:
    """The resilience proof: with supersede DISABLED, two findings from
    different audit runs for the same (bot, app) still fold into ONE card.

    This is the property the per-run coalesce grain lacked — it leaned on
    supersede for cross-run dedup, so a missed supersede piled a fresh card
    per run (the 118-empty-card symptom). ``superseded_runs=None`` skips the
    supersede branch entirely, isolating the coalesce behavior."""
    shared = tmp_path / "shared"
    shared.mkdir()

    # Run 1 finding — supersede disabled (superseded_runs=None).
    ok1 = audit_poller._ingest_tier3_finding(
        _tier3_record(
            bot_id="team_bot_a", app_id="journal",
            record_id="rec-r1", audit_run_id="run-1", sig_suffix="r1",
        ),
        shared,
        superseded_runs=None,
    )
    # Run 2 finding for the SAME (bot, app) — also supersede-disabled.
    ok2 = audit_poller._ingest_tier3_finding(
        _tier3_record(
            bot_id="team_bot_a", app_id="journal",
            record_id="rec-r2", audit_run_id="run-2", sig_suffix="r2",
        ),
        shared,
        superseded_runs=None,
    )
    assert ok1 and ok2

    pending = _pending(shared)
    # ONE card, not two — coalescing alone (no supersede) collapsed the runs.
    assert len(pending) == 1
    assert len(pending[0].get("sub_findings") or []) == 1
    assert pending[0]["coalesce_key"] == "app_audit_tier3:team_bot_a:journal"


# ── 3. Dismiss signature is narrow (per-finding, not the coalesce grain) ─────


def test_tier3_dismiss_signature_is_per_finding_not_coalesce_grain(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """dismiss_signature keys on the finding identity — NOT the (bot, app)
    coalesce_key — so dismissing one finding never suppresses all of the
    app's audit advisories (the deploy-note hazard)."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team_bot_a")
    rec = _tier3_record(
        bot_id="team_bot_a", app_id="journal", record_id="rec-1",
        category="broken_path", sig_suffix="deadbeef",
    )
    _write_tier3(outbox, rec)

    audit_poller.poll_bot("team_bot_a", "team_bot_a", shared)

    body = _pending(shared)[0]
    # Equals the finding's own signature...
    assert body["dismiss_signature"] == rec["signature"]
    # ...and is strictly narrower than the coalesce grain.
    assert body["dismiss_signature"] != body["coalesce_key"]
    assert body["coalesce_key"] not in body["dismiss_signature"]


# ── _summary_line / _clamp_summary unit behavior ─────────────────────────────


def test_summary_line_prefers_explicit_then_description_then_headline() -> None:
    # 1. Explicit producer summary wins.
    assert audit_poller._summary_line(
        {"app_id": "j", "category": "broken_path",
         "summary": "explicit one", "description": "ignored"}
    ) == "explicit one"
    # 2. Falls back to the description.
    assert audit_poller._summary_line(
        {"app_id": "j", "category": "broken_path", "description": "the desc"}
    ) == "the desc"
    # 3. No prose at all → degrades to the category headline (never empty).
    assert audit_poller._summary_line(
        {"app_id": "j", "category": "broken_path"}
    ) == "j: a path or reference doesn't resolve"


def test_clamp_summary_is_sentence_aware_and_word_bounded() -> None:
    # Short text passes through, whitespace collapsed.
    assert audit_poller._clamp_summary("a   b\n c") == "a b c"
    # Multi-sentence text over budget keeps whole leading sentences only.
    long = ("First sentence is short. " + "padding " * 60 + "tail.")
    out = audit_poller._clamp_summary(long)
    assert out == "First sentence is short."
    # A single over-budget sentence is hard-truncated on a word boundary
    # with an ellipsis — never a clipped mid-word fragment.
    one = "word " * 100
    clamped = audit_poller._clamp_summary(one.strip())
    assert len(clamped) <= audit_poller._SUMMARY_MAX_CHARS + 1  # +1 for "…"
    assert clamped.endswith("…")
    assert "wor…" not in clamped  # no mid-word cut
