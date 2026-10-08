"""tests/test_app_posture_reflect_proposals.py — PR7 structural proposals.

PR5 added narrative reflection; PR6 added transcript context. PR7 adds
the structured-proposal emission layer: the LLM emits a fenced YAML
block at the end of its response listing concrete suggestions
(merge/split/delete/fold), and the runner files them as Investigation
proposals to the existing arbiter pending/ subdir.

Tests pin:
  - YAML extraction from a real-shaped LLM response
  - Schema validation (kind, confidence, kind-specific fields)
  - Inventory grounding (no hallucinated app_ids / paths)
  - Confidence threshold filtering
  - Stable id construction → idempotent re-run
  - emit_proposals dedup via find_open_duplicate
  - end-to-end through reflect() with the gate flag
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))


def _fake_target():
    from infra_llm import InfraLLMTarget
    return InfraLLMTarget(
        provider="anthropic",
        model="anthropic/claude-haiku-4-5",
        api_key="sk-ant-fake-test-key",
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _make_posture(*, bot_id: str = "admin_bot", apps=None, orphans=None, signals=None):
    from app_posture_review import (
        BotPosture, ManifestSummary, OrphanFile, SignalSummary,
    )
    now = _now()
    manifests = []
    for app in (apps or []):
        if isinstance(app, str):
            manifests.append(ManifestSummary(
                app_id=app, name=app.title(), source="bot_created",
                status="active", purpose="", crons_count=0, updated_at="",
                is_recent=True, files=[],
            ))
        else:
            manifests.append(app)
    return BotPosture(
        bot_id=bot_id,
        generated_at=_iso(now),
        window_start=_iso(now - timedelta(days=7)),
        window_end=_iso(now),
        manifests=manifests,
        bot_created_signals=signals or [],
        unmanifested_signals=[],
        orphan_files=[
            OrphanFile(path=p, size=512, mtime_iso=_iso(now)) for p in (orphans or [])
        ],
        workspace_path=None,
        notes=[],
    )


def _llm_response_with_yaml(yaml_block: str) -> str:
    """Build an LLM response with markdown narrative + a fenced YAML
    proposals block, mirroring what the real prompt asks for."""
    return (
        "## Reflection\n\n"
        "### Clusters\nNone.\n\n"
        "### Splits\nNone.\n\n"
        "### Orphan dispositions\nNo orphans.\n\n"
        "### Missed signals\nNone.\n\n"
        "### Forward guidance for next week\n_(none)_\n\n"
        "### Structural proposals\n\n"
        "```yaml\n"
        + yaml_block.rstrip()
        + "\n```\n"
    )


# ── parse_proposals_yaml ─────────────────────────────────────────────────────


class TestParseProposalsYaml:
    def test_extracts_well_formed_proposals(self):
        from app_posture_reflect import parse_proposals_yaml
        text = _llm_response_with_yaml(
            "proposals:\n"
            "  - kind: merge_apps\n"
            "    confidence: 0.85\n"
            "    apps: [habits, notes]\n"
            "    rationale: heavy file overlap\n"
        )
        result = parse_proposals_yaml(text)
        assert result.parse_error is None
        assert len(result.candidates) == 1
        c = result.candidates[0]
        assert c.kind == "merge_apps"
        assert c.confidence == 0.85
        assert c.payload == {"apps": ["habits", "notes"]}
        assert c.rationale == "heavy file overlap"

    def test_handles_empty_proposals_list(self):
        """The LLM is instructed to emit `proposals: []` when nothing is
        worth filing. That's not an error — it's the success-no-output
        path."""
        from app_posture_reflect import parse_proposals_yaml
        text = _llm_response_with_yaml("proposals: []\n")
        result = parse_proposals_yaml(text)
        assert result.parse_error is None
        assert result.candidates == []

    def test_no_yaml_block_is_silent(self):
        """A response without the fenced block is treated as 'no
        proposals' — no parse error, just an empty candidate list."""
        from app_posture_reflect import parse_proposals_yaml
        text = "## Reflection\n\nbla bla\n"
        result = parse_proposals_yaml(text)
        assert result.parse_error is None
        assert result.candidates == []

    def test_drops_unknown_kinds(self):
        """The LLM might invent a 'kind: rebuild_universe' — silently
        skip rather than file a junk proposal."""
        from app_posture_reflect import parse_proposals_yaml
        text = _llm_response_with_yaml(
            "proposals:\n"
            "  - kind: merge_apps\n"
            "    confidence: 0.7\n"
            "    apps: [a, b]\n"
            "    rationale: ok\n"
            "  - kind: rebuild_universe\n"
            "    confidence: 0.99\n"
            "    rationale: dramatic\n"
        )
        result = parse_proposals_yaml(text)
        assert len(result.candidates) == 1
        assert result.candidates[0].kind == "merge_apps"

    def test_clamps_confidence_to_unit_interval(self):
        """LLM emits 1.5? Negative? Treat as 1.0/0.0 — never trust
        out-of-range numbers."""
        from app_posture_reflect import parse_proposals_yaml
        text = _llm_response_with_yaml(
            "proposals:\n"
            "  - kind: merge_apps\n"
            "    confidence: 1.5\n"
            "    apps: [a, b]\n"
            "    rationale: too high\n"
            "  - kind: split_app\n"
            "    confidence: -0.3\n"
            "    app: x\n"
            "    rationale: negative\n"
        )
        result = parse_proposals_yaml(text)
        confs = [c.confidence for c in result.candidates]
        assert confs == [1.0, 0.0]

    def test_malformed_yaml_returns_parse_error(self):
        from app_posture_reflect import parse_proposals_yaml
        text = (
            "## Reflection\n\n"
            "### Structural proposals\n\n"
            "```yaml\n"
            "proposals:\n"
            "  - kind: merge_apps\n"
            "    confidence: [unclosed\n"
            "```\n"
        )
        result = parse_proposals_yaml(text)
        assert result.parse_error is not None
        assert result.candidates == []

    def test_missing_proposals_key_returns_parse_error(self):
        from app_posture_reflect import parse_proposals_yaml
        text = _llm_response_with_yaml("not_proposals: 5\n")
        result = parse_proposals_yaml(text)
        assert result.parse_error is not None
        assert result.candidates == []

    def test_picks_last_yaml_block(self):
        """If the LLM stuffs an earlier yaml fence into the rationale
        somehow, take the LAST one — that's what the prompt specifies."""
        from app_posture_reflect import parse_proposals_yaml
        text = (
            "Earlier block:\n\n"
            "```yaml\n"
            "ignored: true\n"
            "```\n\n"
            "Later proposals:\n\n"
            "```yaml\n"
            "proposals:\n"
            "  - kind: split_app\n"
            "    confidence: 0.7\n"
            "    app: x\n"
            "    rationale: ok\n"
            "```\n"
        )
        result = parse_proposals_yaml(text)
        assert len(result.candidates) == 1
        assert result.candidates[0].kind == "split_app"


# ── inventory grounding ──────────────────────────────────────────────────────


class TestCandidateRefsKnown:
    def test_merge_apps_passes_when_all_known(self):
        from app_posture_reflect import (
            ProposalCandidate, _candidate_refs_known,
        )
        posture = _make_posture(apps=["habits", "notes"])
        c = ProposalCandidate(
            kind="merge_apps", confidence=0.8, rationale="",
            payload={"apps": ["habits", "notes"]},
        )
        assert _candidate_refs_known(c, posture) is True

    def test_merge_apps_drops_when_one_app_unknown(self):
        from app_posture_reflect import (
            ProposalCandidate, _candidate_refs_known,
        )
        posture = _make_posture(apps=["habits"])
        c = ProposalCandidate(
            kind="merge_apps", confidence=0.8, rationale="",
            payload={"apps": ["habits", "frobinator"]},
        )
        assert _candidate_refs_known(c, posture) is False

    def test_merge_apps_requires_at_least_two(self):
        from app_posture_reflect import (
            ProposalCandidate, _candidate_refs_known,
        )
        posture = _make_posture(apps=["habits"])
        c = ProposalCandidate(
            kind="merge_apps", confidence=0.8, rationale="",
            payload={"apps": ["habits"]},
        )
        assert _candidate_refs_known(c, posture) is False

    def test_split_app_passes_when_known(self):
        from app_posture_reflect import (
            ProposalCandidate, _candidate_refs_known,
        )
        posture = _make_posture(apps=["tracker"])
        c = ProposalCandidate(
            kind="split_app", confidence=0.8, rationale="",
            payload={"app": "tracker"},
        )
        assert _candidate_refs_known(c, posture) is True

    def test_delete_orphan_passes_when_path_known(self):
        from app_posture_reflect import (
            ProposalCandidate, _candidate_refs_known,
        )
        posture = _make_posture(orphans=["loose.md"])
        c = ProposalCandidate(
            kind="delete_orphan", confidence=0.8, rationale="",
            payload={"path": "loose.md"},
        )
        assert _candidate_refs_known(c, posture) is True

    def test_delete_orphan_drops_when_path_unknown(self):
        from app_posture_reflect import (
            ProposalCandidate, _candidate_refs_known,
        )
        posture = _make_posture(orphans=["loose.md"])
        c = ProposalCandidate(
            kind="delete_orphan", confidence=0.8, rationale="",
            payload={"path": "nonexistent.md"},
        )
        assert _candidate_refs_known(c, posture) is False

    def test_fold_orphan_requires_both_path_and_app_known(self):
        from app_posture_reflect import (
            ProposalCandidate, _candidate_refs_known,
        )
        posture = _make_posture(apps=["habits"], orphans=["loose.md"])
        good = ProposalCandidate(
            kind="fold_orphan", confidence=0.8, rationale="",
            payload={"path": "loose.md", "into_app": "habits"},
        )
        assert _candidate_refs_known(good, posture) is True

        bad = ProposalCandidate(
            kind="fold_orphan", confidence=0.8, rationale="",
            payload={"path": "loose.md", "into_app": "frobinator"},
        )
        assert _candidate_refs_known(bad, posture) is False


# ── stable id construction ──────────────────────────────────────────────────


class TestCandidateId:
    def test_same_inputs_produce_same_id(self):
        from app_posture_reflect import (
            ProposalCandidate, _candidate_id,
        )
        c1 = ProposalCandidate(
            kind="merge_apps", confidence=0.8, rationale="x",
            payload={"apps": ["habits", "notes"]},
        )
        c2 = ProposalCandidate(
            kind="merge_apps", confidence=0.7, rationale="different",
            payload={"apps": ["habits", "notes"]},  # same apps
        )
        # Confidence and rationale are not part of the id — only refs.
        assert _candidate_id(c1, "admin_bot") == _candidate_id(c2, "admin_bot")

    def test_apps_order_does_not_affect_id(self):
        """Re-running over the same week shouldn't dedupe to a new id
        just because the LLM listed apps in a different order."""
        from app_posture_reflect import (
            ProposalCandidate, _candidate_id,
        )
        c1 = ProposalCandidate(
            kind="merge_apps", confidence=0.8, rationale="",
            payload={"apps": ["habits", "notes"]},
        )
        c2 = ProposalCandidate(
            kind="merge_apps", confidence=0.8, rationale="",
            payload={"apps": ["notes", "habits"]},
        )
        assert _candidate_id(c1, "admin_bot") == _candidate_id(c2, "admin_bot")

    def test_different_bot_ids_produce_different_ids(self):
        from app_posture_reflect import (
            ProposalCandidate, _candidate_id,
        )
        c = ProposalCandidate(
            kind="split_app", confidence=0.8, rationale="",
            payload={"app": "tracker"},
        )
        assert _candidate_id(c, "admin_bot") != _candidate_id(c, "team_bot_a")


# ── emit_proposals ──────────────────────────────────────────────────────────


class TestEmitProposals:
    def test_files_high_confidence_proposals(self, tmp_path):
        from app_posture_reflect import emit_proposals, ProposalCandidate
        from arbiter.store import iter_proposals

        posture = _make_posture(apps=["habits", "notes"])
        candidates = [
            ProposalCandidate(
                kind="merge_apps", confidence=0.85, rationale="merge them",
                payload={"apps": ["habits", "notes"]},
            ),
        ]
        summary = emit_proposals(candidates, posture, tmp_path)
        assert summary["filed"] == 1
        assert summary["dropped_low_confidence"] == 0
        assert summary["dropped_unknown_refs"] == 0

        # The proposal landed in pending/.
        proposals = list(iter_proposals(tmp_path, subdirs=("pending",)))
        assert len(proposals) == 1
        p = proposals[0]
        assert p.bot_id == "admin_bot"
        assert p.generator_id == "app_posture_reflection"
        assert p.dimension == "app_posture"
        assert p.action.kind == "Investigation"
        assert "habits" in p.action.context
        assert "notes" in p.action.context
        assert p.urgency == "improvement"
        assert p.approval_audience == "pod_operator"
        assert p.status == "pending"

    def test_drops_below_min_confidence(self, tmp_path):
        from app_posture_reflect import emit_proposals, ProposalCandidate
        posture = _make_posture(apps=["a", "b"])
        candidates = [
            ProposalCandidate(
                kind="merge_apps", confidence=0.4, rationale="",
                payload={"apps": ["a", "b"]},
            ),
        ]
        summary = emit_proposals(candidates, posture, tmp_path)
        assert summary["filed"] == 0
        assert summary["dropped_low_confidence"] == 1

    def test_drops_unknown_refs(self, tmp_path):
        from app_posture_reflect import emit_proposals, ProposalCandidate
        posture = _make_posture(apps=["habits"])  # only habits exists
        candidates = [
            ProposalCandidate(
                kind="merge_apps", confidence=0.9, rationale="",
                payload={"apps": ["habits", "ghost"]},
            ),
        ]
        summary = emit_proposals(candidates, posture, tmp_path)
        assert summary["filed"] == 0
        assert summary["dropped_unknown_refs"] == 1

    def test_dedups_when_same_proposal_already_pending(self, tmp_path):
        """Re-running the reflection over the same week with the same
        suggestions must not produce duplicate proposals."""
        from app_posture_reflect import emit_proposals, ProposalCandidate
        posture = _make_posture(apps=["a", "b"])
        candidates = [
            ProposalCandidate(
                kind="merge_apps", confidence=0.85, rationale="",
                payload={"apps": ["a", "b"]},
            ),
        ]
        # First run files it.
        summary1 = emit_proposals(candidates, posture, tmp_path)
        assert summary1["filed"] == 1

        # Second run — same posture, same candidates — must dedup.
        summary2 = emit_proposals(candidates, posture, tmp_path)
        assert summary2["filed"] == 0
        assert summary2["deduped"] == 1

    def test_handles_empty_candidate_list(self, tmp_path):
        from app_posture_reflect import emit_proposals
        posture = _make_posture()
        summary = emit_proposals([], posture, tmp_path)
        assert summary == {
            "filed": 0,
            "dropped_low_confidence": 0,
            "dropped_unknown_refs": 0,
            "deduped": 0,
            "errors": 0,
        }

    def test_orphan_dispositions_round_trip(self, tmp_path):
        """End-to-end check on the orphan-handling kinds — we previously
        only tested merge_apps."""
        from app_posture_reflect import emit_proposals, ProposalCandidate
        from arbiter.store import iter_proposals

        posture = _make_posture(apps=["habits"], orphans=["loose.md", "stale.txt"])
        candidates = [
            ProposalCandidate(
                kind="fold_orphan", confidence=0.8, rationale="data file",
                payload={"path": "loose.md", "into_app": "habits"},
            ),
            ProposalCandidate(
                kind="delete_orphan", confidence=0.7, rationale="redundant",
                payload={"path": "stale.txt"},
            ),
        ]
        summary = emit_proposals(candidates, posture, tmp_path)
        assert summary["filed"] == 2
        proposals = sorted(
            iter_proposals(tmp_path, subdirs=("pending",)),
            key=lambda p: p.id,
        )
        kinds = [p.provenance.signals.get("kind") for p in proposals]
        assert sorted(kinds) == ["delete_orphan", "fold_orphan"]


# ── reflect() with proposal emission ────────────────────────────────────────


class TestReflectWithProposalEmission:
    def test_emits_when_flag_on(self, monkeypatch, tmp_path):
        from app_posture_reflect import reflect
        from arbiter.store import iter_proposals

        canned = _llm_response_with_yaml(
            "proposals:\n"
            "  - kind: merge_apps\n"
            "    confidence: 0.85\n"
            "    apps: [habits, notes]\n"
            "    rationale: file overlap\n"
        )
        monkeypatch.setattr("app_posture_reflect._resolve_target", lambda b: _fake_target())
        monkeypatch.setattr("app_posture_reflect._call_llm", lambda *a, **kw: canned)

        posture = _make_posture(apps=["habits", "notes"])
        result = reflect(posture, shared_dir=tmp_path, emit_proposals_enabled=True)
        assert result.ok is True
        assert result.proposals_summary is not None
        assert result.proposals_summary["filed"] == 1

        proposals = list(iter_proposals(tmp_path, subdirs=("pending",)))
        assert len(proposals) == 1

    def test_does_not_emit_when_flag_off_but_reports_would_file(self, monkeypatch, tmp_path):
        """Flag off → no proposals filed but the summary tells the
        operator how many would have been. This makes it safe to soak
        the parse output before turning emission on."""
        from app_posture_reflect import reflect
        from arbiter.store import iter_proposals

        canned = _llm_response_with_yaml(
            "proposals:\n"
            "  - kind: merge_apps\n"
            "    confidence: 0.85\n"
            "    apps: [habits, notes]\n"
            "    rationale: file overlap\n"
        )
        monkeypatch.setattr("app_posture_reflect._resolve_target", lambda b: _fake_target())
        monkeypatch.setattr("app_posture_reflect._call_llm", lambda *a, **kw: canned)

        posture = _make_posture(apps=["habits", "notes"])
        result = reflect(
            posture, shared_dir=tmp_path, emit_proposals_enabled=False,
        )
        assert result.ok is True
        assert list(iter_proposals(tmp_path, subdirs=("pending",))) == []
        # But the summary surfaces what would have been filed.
        assert result.proposals_summary is not None
        assert result.proposals_summary["filed"] == 0
        assert result.proposals_summary["would_file"] == 1


# ── runner integration ──────────────────────────────────────────────────────


class TestFoldOrphanEmitsManifestUpdate:
    """PR8: fold_orphan candidates now produce a ManifestUpdate(add_files)
    Action instead of Investigation. The applier auto-applies on operator
    approval (append path to files[], deduped), as opposed to the
    Investigation path where the operator acts manually.

    Other kinds (merge_apps, split_app, delete_orphan) stay Investigation
    until their own appliers land in future PRs."""

    def test_fold_orphan_emits_manifest_update_action(self, tmp_path):
        from app_posture_reflect import emit_proposals, ProposalCandidate
        from arbiter.store import iter_proposals

        posture = _make_posture(apps=["habits"], orphans=["loose.md"])
        candidates = [
            ProposalCandidate(
                kind="fold_orphan", confidence=0.8, rationale="data file",
                payload={"path": "loose.md", "into_app": "habits"},
            ),
        ]
        emit_proposals(candidates, posture, tmp_path)

        proposals = list(iter_proposals(tmp_path, subdirs=("pending",)))
        assert len(proposals) == 1
        p = proposals[0]
        # Action is ManifestUpdate, not Investigation.
        assert p.action.kind == "ManifestUpdate"
        assert p.action.operation == "add_files"
        assert p.action.app_id == "habits"
        assert p.action.fields == {"files": ["loose.md"]}
        # Reversibility flips to "auto" since the applier has a revert
        # path — a meaningful difference from Investigation's "manual".
        assert p.risk_tag.reversibility == "auto"

    def test_fold_orphan_admin_summary_includes_rationale(self, tmp_path):
        """ManifestUpdate has no Investigation.context to render the
        rationale into; the emitter folds rationale into
        admin_surface_summary so operators see it on the alerts UI tile
        without opening the proposal detail."""
        from app_posture_reflect import emit_proposals, ProposalCandidate
        from arbiter.store import iter_proposals

        posture = _make_posture(apps=["habits"], orphans=["loose.md"])
        emit_proposals(
            [ProposalCandidate(
                kind="fold_orphan", confidence=0.8,
                rationale="clearly a data file for habits",
                payload={"path": "loose.md", "into_app": "habits"},
            )],
            posture, tmp_path,
        )
        p = list(iter_proposals(tmp_path, subdirs=("pending",)))[0]
        assert "data file for habits" in p.admin_surface_summary

    def test_remaining_investigation_kinds(self, tmp_path):
        """merge_apps and split_app still produce Investigation actions
        until their own appliers exist (PR9 graduated delete_orphan to
        RetireOrphan and PR8 graduated fold_orphan to ManifestUpdate)."""
        from app_posture_reflect import emit_proposals, ProposalCandidate
        from arbiter.store import iter_proposals

        posture = _make_posture(apps=["habits", "notes", "tracker"])
        emit_proposals(
            [
                ProposalCandidate(
                    kind="merge_apps", confidence=0.85, rationale="",
                    payload={"apps": ["habits", "notes"]},
                ),
                ProposalCandidate(
                    kind="split_app", confidence=0.7, rationale="",
                    payload={"app": "tracker"},
                ),
            ],
            posture, tmp_path,
        )
        for p in iter_proposals(tmp_path, subdirs=("pending",)):
            assert p.action.kind == "Investigation"
            assert p.risk_tag.reversibility == "manual"

    def test_fold_orphan_action_round_trips_through_applier(self, tmp_path, monkeypatch):
        """End-to-end: the ManifestUpdate(add_files) action produced by
        the emitter is exactly the shape the applier expects, so an
        operator-approved proposal really does append the file. This is
        the load-bearing PR8 contract."""
        import json
        from app_posture_reflect import emit_proposals, ProposalCandidate
        from arbiter.store import iter_proposals
        from arbiter.appliers import get_applier
        from arbiter.appliers.manifest_update import set_shared_dir

        # Wire the applier at this tmp_path so its _manifest_path lookups
        # find our manifest.
        set_shared_dir(tmp_path)

        # Pre-existing manifest the orphan should fold into.
        apps_dir = tmp_path / "applications" / "admin_bot"
        apps_dir.mkdir(parents=True)
        (apps_dir / "habits.json").write_text(json.dumps({
            "id": "habits", "name": "Habits", "bot_id": "admin_bot",
            "status": "active", "files": ["ops/existing.py"],
        }))

        posture = _make_posture(apps=["habits"], orphans=["loose.md"])
        emit_proposals(
            [ProposalCandidate(
                kind="fold_orphan", confidence=0.85, rationale="",
                payload={"path": "loose.md", "into_app": "habits"},
            )],
            posture, tmp_path,
        )

        # Simulate operator approval — apply the proposal's action.
        proposal = next(iter_proposals(tmp_path, subdirs=("pending",)))
        result = get_applier("ManifestUpdate").apply(proposal.action, "admin_bot")

        try:
            assert result.ok, f"apply failed: {result.message}"
            data = json.loads((apps_dir / "habits.json").read_text())
            assert data["files"] == ["ops/existing.py", "loose.md"]
        finally:
            # Restore the canonical shared_dir so other tests aren't
            # contaminated by our override.
            from pathlib import Path as _P
            set_shared_dir(_P("/Users/Shared/evolve"))


class TestDeleteOrphanEmitsRetireOrphan:
    """PR9: delete_orphan candidates now produce a RetireOrphan Action
    instead of Investigation. The applier archives the file content +
    appends the path to the bot's orphan_exclusions list (the workspace
    file itself is NOT unlinked — evolve has no delete grant on bot
    workspaces; physical removal is out of scope here)."""

    def test_delete_orphan_emits_retire_orphan_action(self, tmp_path):
        from app_posture_reflect import emit_proposals, ProposalCandidate
        from arbiter.store import iter_proposals

        posture = _make_posture(orphans=["stale.md"])
        emit_proposals(
            [ProposalCandidate(
                kind="delete_orphan", confidence=0.8, rationale="redundant",
                payload={"path": "stale.md"},
            )],
            posture, tmp_path,
        )
        proposals = list(iter_proposals(tmp_path, subdirs=("pending",)))
        assert len(proposals) == 1
        p = proposals[0]
        assert p.action.kind == "RetireOrphan"
        assert p.action.path == "stale.md"
        assert p.action.bot_id == "admin_bot"
        # Reversibility flips to "auto" since the applier captures a
        # snapshot of the exclusions list and revert restores it.
        assert p.risk_tag.reversibility == "auto"
        assert "bot_workspace" in p.risk_tag.touches

    def test_delete_orphan_admin_summary_includes_rationale(self, tmp_path):
        """RetireOrphan has no Investigation.context to render the
        rationale into; emitter folds rationale into
        admin_surface_summary so operators see it on the alerts UI tile."""
        from app_posture_reflect import emit_proposals, ProposalCandidate
        from arbiter.store import iter_proposals

        posture = _make_posture(orphans=["stale.md"])
        emit_proposals(
            [ProposalCandidate(
                kind="delete_orphan", confidence=0.8,
                rationale="not used since 2024",
                payload={"path": "stale.md"},
            )],
            posture, tmp_path,
        )
        p = list(iter_proposals(tmp_path, subdirs=("pending",)))[0]
        assert "not used since 2024" in p.admin_surface_summary

    def test_delete_orphan_problem_text_says_retire(self, tmp_path):
        """The action archives + excludes rather than deleting (evolve
        has no delete grant on bot workspaces). The headline should
        read "retire", not "delete", so operators don't expect physical
        removal."""
        from app_posture_reflect import emit_proposals, ProposalCandidate
        from arbiter.store import iter_proposals

        posture = _make_posture(orphans=["stale.md"])
        emit_proposals(
            [ProposalCandidate(
                kind="delete_orphan", confidence=0.8, rationale="",
                payload={"path": "stale.md"},
            )],
            posture, tmp_path,
        )
        p = list(iter_proposals(tmp_path, subdirs=("pending",)))[0]
        assert "retire" in p.problem.lower()
        assert "delete" not in p.problem.lower()

    def test_delete_orphan_round_trips_through_applier(self, tmp_path, monkeypatch):
        """End-to-end: emit a RetireOrphan proposal, simulate operator
        approval by applying the action, and verify the exclusions
        list updates so the next posture review skips the path."""
        import json
        from app_posture_reflect import emit_proposals, ProposalCandidate
        from arbiter.appliers import get_applier
        from arbiter.appliers.retire_orphan import (
            load_exclusions, set_shared_dir,
        )
        from arbiter.store import iter_proposals

        # Wire the applier at this tmp_path.
        set_shared_dir(tmp_path)

        # A fake workspace with the orphan present.
        fake_workspace = tmp_path / "admin_bot-home" / ".openclaw" / "workspace"
        fake_workspace.mkdir(parents=True)
        (fake_workspace / "stale.md").write_text("orphan content\n")

        import arbiter.appliers.retire_orphan as ro
        monkeypatch.setattr(ro, "_bot_workspace", lambda b: fake_workspace)

        posture = _make_posture(orphans=["stale.md"])
        emit_proposals(
            [ProposalCandidate(
                kind="delete_orphan", confidence=0.85, rationale="",
                payload={"path": "stale.md"},
            )],
            posture, tmp_path,
        )

        # Simulate operator approval — apply the proposal's action.
        proposal = next(iter_proposals(tmp_path, subdirs=("pending",)))
        result = get_applier("RetireOrphan").apply(proposal.action, "admin_bot")

        try:
            assert result.ok, f"apply failed: {result.message}"
            # Exclusions list now contains the path.
            assert load_exclusions("admin_bot", shared_dir=tmp_path) == {"stale.md"}
            # Archive file exists with original content.
            archive = list(
                (tmp_path / "app_posture" / "admin_bot" / "orphan_archive").glob("*")
            )
            assert len(archive) == 1
            assert archive[0].read_text() == "orphan content\n"
            # Workspace file untouched.
            assert (fake_workspace / "stale.md").exists()
        finally:
            from pathlib import Path as _P
            set_shared_dir(_P("/Users/Shared/evolve"))


class TestMotivatingSignalLinkage:
    """PR10: emit_proposals now populates Proposal.motivating_signals
    with the ids of bot_created_app signals from the posture. PR7 left
    this empty because SignalSummary didn't carry the id; PR10 threads
    it through."""

    def _signal_summary(self, *, signal_id: str = "sig-12345", app_id: str = "habits"):
        from app_posture_review import SignalSummary
        return SignalSummary(
            id=signal_id,
            type="bot_created_app",
            signature=f"bot_created_app:admin_bot:{app_id}",
            title=f"admin_bot built {app_id}",
            body="",
            first_observed_at=_iso(_now()),
            last_observed_at=_iso(_now()),
            observation_count=1,
            bot_id="admin_bot",
            details={"app_id": app_id, "session_id": "sess-1"},
        )

    def test_proposal_carries_signal_ids(self, tmp_path):
        from app_posture_reflect import emit_proposals, ProposalCandidate
        from arbiter.store import iter_proposals

        posture = _make_posture(
            apps=["habits", "notes"],
            signals=[
                self._signal_summary(signal_id="sig-aaa", app_id="habits"),
                self._signal_summary(signal_id="sig-bbb", app_id="notes"),
            ],
        )
        emit_proposals(
            [ProposalCandidate(
                kind="merge_apps", confidence=0.85, rationale="",
                payload={"apps": ["habits", "notes"]},
            )],
            posture, tmp_path,
        )
        p = list(iter_proposals(tmp_path, subdirs=("pending",)))[0]
        # All bot_created signal ids appear on the proposal.
        assert sorted(p.motivating_signals) == ["sig-aaa", "sig-bbb"]

    def test_caps_at_eight_signals(self, tmp_path):
        """If the bot was busy this week, motivating_signals could grow
        unbounded. Cap at 8 — same window the emitter already uses for
        trigger_observations."""
        from app_posture_reflect import emit_proposals, ProposalCandidate
        from arbiter.store import iter_proposals

        signals = [
            self._signal_summary(signal_id=f"sig-{i:03d}", app_id=f"app-{i}")
            for i in range(15)
        ]
        posture = _make_posture(apps=["habits", "notes"], signals=signals)
        emit_proposals(
            [ProposalCandidate(
                kind="merge_apps", confidence=0.85, rationale="",
                payload={"apps": ["habits", "notes"]},
            )],
            posture, tmp_path,
        )
        p = list(iter_proposals(tmp_path, subdirs=("pending",)))[0]
        assert len(p.motivating_signals) == 8

    def test_skips_empty_signal_ids(self, tmp_path):
        """Older fixtures / synthetic tests may construct SignalSummary
        without an id (e.g. before PR10). Don't include empty strings
        in motivating_signals — the arbiter rejects them."""
        from app_posture_reflect import emit_proposals, ProposalCandidate
        from arbiter.store import iter_proposals

        signals = [
            self._signal_summary(signal_id="", app_id="habits"),  # empty id
            self._signal_summary(signal_id="sig-real", app_id="notes"),
        ]
        posture = _make_posture(apps=["habits", "notes"], signals=signals)
        emit_proposals(
            [ProposalCandidate(
                kind="merge_apps", confidence=0.85, rationale="",
                payload={"apps": ["habits", "notes"]},
            )],
            posture, tmp_path,
        )
        p = list(iter_proposals(tmp_path, subdirs=("pending",)))[0]
        assert p.motivating_signals == ["sig-real"]

    def test_no_signals_means_empty_list(self, tmp_path):
        from app_posture_reflect import emit_proposals, ProposalCandidate
        from arbiter.store import iter_proposals

        posture = _make_posture(apps=["habits", "notes"])
        emit_proposals(
            [ProposalCandidate(
                kind="merge_apps", confidence=0.85, rationale="",
                payload={"apps": ["habits", "notes"]},
            )],
            posture, tmp_path,
        )
        p = list(iter_proposals(tmp_path, subdirs=("pending",)))[0]
        assert p.motivating_signals == []


class TestRunOnceProposalEmission:
    def test_emit_proposals_override_threads_through(self, monkeypatch, tmp_path):
        """`run_once(..., emit_proposals_override=True)` should result in
        proposals being filed when the LLM emits them."""
        import app_posture_review as apr
        monkeypatch.setattr(apr, "_resolve_bot_workspace", lambda b: None)

        # Synthesize a manifest so the inventory has content.
        apps_dir = tmp_path / "applications" / "admin_bot"
        apps_dir.mkdir(parents=True)
        for app in ("habits", "notes"):
            (apps_dir / f"{app}.json").write_text(json.dumps({
                "id": app, "bot_id": "admin_bot", "source": "bot_created",
                "status": "active", "files": [],
                "updated_at": _iso(_now() - timedelta(days=1)),
            }))

        canned = _llm_response_with_yaml(
            "proposals:\n"
            "  - kind: merge_apps\n"
            "    confidence: 0.85\n"
            "    apps: [habits, notes]\n"
            "    rationale: file overlap\n"
        )
        monkeypatch.setattr("app_posture_reflect._resolve_target", lambda b: _fake_target())
        monkeypatch.setattr("app_posture_reflect._call_llm", lambda *a, **kw: canned)

        cfg = {"bots": {"admin_bot": {}}, "sharedDir": str(tmp_path)}
        totals = apr.run_once(
            cfg, reflect_override=True, emit_proposals_override=True,
        )
        assert totals["proposals_filed"] == 1

        from arbiter.store import iter_proposals
        proposals = list(iter_proposals(tmp_path, subdirs=("pending",)))
        assert len(proposals) == 1
        assert proposals[0].generator_id == "app_posture_reflection"
