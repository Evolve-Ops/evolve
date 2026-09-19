"""tests/test_app_permission_review_phase_c.py — Phase C-3 content + gates.

Spec: internal/spec-proposal-drafting-protocol-2026-06-04.md.

Three things the migration owes the protocol:

1. **Operator-first content per finding category.** Each of the three
   categories (unused / missing / overkill) populates summary +
   explanation + a Tier-5 paste-to-bot instruction.
2. **Per-finding dismiss signature.** Granularity is per
   (kind, app_id, entry_kind, entry_value) so dismissing one specific
   stale exec entry does not suppress findings on other entries.
3. **observe()-level suppression.** Preloaded once per run; per-finding
   membership check skips emission when the signature is dismissed.

We exercise the content helper directly + run observe() with a
manifest fixture to confirm the suppression gate fires at the right
granularity.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_THIS_FILE = Path(__file__).resolve()
_ANALYZER_DIR = _THIS_FILE.parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter import dismissals  # noqa: E402
from generators.app_permission_review.consolidation import (  # noqa: E402
    OUTCOME_AS_IS,
    OUTCOME_MOVE_TO_SIBLING,
    OUTCOME_SIBLING_DECLARES,
    ConsolidatedFinding,
)
from generators.app_permission_review.observe import (  # noqa: E402
    AppPermissionReviewContext,
    observe,
)
from generators.app_permission_review.proposals import (  # noqa: E402
    _phase_c_content_for,
    dismiss_signature_for_finding,
)
from generators.app_permission_review.review import (  # noqa: E402
    KIND_EGRESS_MISSING_DECLARATION,
    KIND_EXEC_OVERKILL_WILDCARD,
    KIND_EXEC_UNUSED,
    KIND_FS_READ_UNUSED,
    Finding,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _finding(
    *,
    kind: str = KIND_EXEC_UNUSED,
    bot_id: str = "team_bot_a",
    app_id: str = "i-task",
    app_name: str = "Task App",
    entry_kind: str = "exec",
    entry_value: str = "scripts/ghost.py",
    severity: str = "info",
    rationale: str = "no references found in scripts",
    meta: dict | None = None,
) -> Finding:
    return Finding(
        kind=kind,
        bot_id=bot_id,
        app_id=app_id,
        app_name=app_name,
        entry_kind=entry_kind,
        entry_value=entry_value,
        severity=severity,
        rationale=rationale,
        meta=meta or {},
    )


def _cf(f: Finding, *, outcome: str = OUTCOME_AS_IS, sibling_apps=()) -> ConsolidatedFinding:
    return ConsolidatedFinding(finding=f, outcome=outcome, sibling_apps=list(sibling_apps))


def _make_bot(
    tmp_path: Path,
    bot_id: str,
    *,
    manifests: dict[str, dict] | None = None,
    workspace_files: dict[str, str] | None = None,
) -> Path:
    home = tmp_path / bot_id
    (home / ".openclaw" / "workspace" / "manifests").mkdir(parents=True)
    for app_id, m in (manifests or {}).items():
        m.setdefault("id", app_id)
        (home / ".openclaw" / "workspace" / "manifests" / f"{app_id}.json").write_text(
            json.dumps(m)
        )
    for rel, body in (workspace_files or {}).items():
        full = home / ".openclaw" / "workspace" / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(body)
    return home


# ─────────────────────────────────────────────────────────────────────────────
# Per-category content
# ─────────────────────────────────────────────────────────────────────────────


class TestContentPerCategory:
    def test_unused_finding_returns_tier_5_paste_to_bot(self):
        c = _phase_c_content_for(_cf(_finding(kind=KIND_EXEC_UNUSED)))
        assert c["summary"]
        assert c["explanation"]
        # Tier 5 — Investigation default button, manual_instruction populated.
        assert c["action_label"] is None
        assert c["manual_instruction"]
        # The summary names the operator question ("removing the
        # declaration narrows what the app is allowed to do"), not the
        # rule.
        assert "Task App" in c["summary"]
        assert "ghost.py" in c["summary"]

    def test_missing_finding_returns_tier_5_paste_to_bot(self):
        c = _phase_c_content_for(_cf(_finding(
            kind=KIND_EGRESS_MISSING_DECLARATION,
            entry_kind="network_egress",
            entry_value="api.example.com",
        )))
        assert c["summary"]
        # The Tier-5 instruction asks the bot to confirm before adding.
        assert "leftover" in c["manual_instruction"].lower() or (
            "live code" in c["manual_instruction"].lower()
        )

    def test_overkill_finding_returns_tier_5_paste_to_bot(self):
        c = _phase_c_content_for(_cf(_finding(
            kind=KIND_EXEC_OVERKILL_WILDCARD,
            entry_kind="exec",
            entry_value="*",
        )))
        assert c["summary"]
        # Trade-off: "leave the wildcard" if uncertain.
        text = c["manual_instruction"].lower()
        assert "wildcard" in text or "list" in text


class TestVoiceRules:
    """Length budgets + trade-off check across all categories."""

    @pytest.mark.parametrize(
        "kind,entry_kind,entry_value",
        [
            (KIND_EXEC_UNUSED, "exec", "scripts/ghost.py"),
            (KIND_FS_READ_UNUSED, "fs_read", "/tmp/cache"),
            (KIND_EGRESS_MISSING_DECLARATION, "network_egress", "api.example.com"),
            (KIND_EXEC_OVERKILL_WILDCARD, "exec", "*"),
        ],
    )
    def test_summary_within_budget(self, kind, entry_kind, entry_value):
        c = _phase_c_content_for(_cf(_finding(
            kind=kind, entry_kind=entry_kind, entry_value=entry_value,
        )))
        assert len(c["summary"]) <= 400

    @pytest.mark.parametrize(
        "kind,entry_kind,entry_value",
        [
            (KIND_EXEC_UNUSED, "exec", "scripts/ghost.py"),
            (KIND_EGRESS_MISSING_DECLARATION, "network_egress", "api.example.com"),
            (KIND_EXEC_OVERKILL_WILDCARD, "exec", "*"),
        ],
    )
    def test_explanation_within_budget(self, kind, entry_kind, entry_value):
        c = _phase_c_content_for(_cf(_finding(
            kind=kind, entry_kind=entry_kind, entry_value=entry_value,
        )))
        assert len(c["explanation"]) <= 1500

    @pytest.mark.parametrize(
        "kind", [
            KIND_EXEC_UNUSED,
            KIND_EGRESS_MISSING_DECLARATION,
            KIND_EXEC_OVERKILL_WILDCARD,
        ],
    )
    def test_explanation_names_trade_off(self, kind):
        c = _phase_c_content_for(_cf(_finding(kind=kind)))
        text = c["explanation"].lower()
        assert any(
            phrase in text
            for phrase in ("what could go wrong", "trade-off", "downside", "risk")
        )


class TestOutcomeSuffix:
    """Consolidation outcome should append context to the Summary so the
    operator sees sibling-declares / move-to-sibling context up front."""

    def test_sibling_declares_appends_note(self):
        c = _phase_c_content_for(_cf(
            _finding(),
            outcome=OUTCOME_SIBLING_DECLARES,
            sibling_apps=["app-b"],
        ))
        assert "sibling" in c["summary"].lower()
        assert "app-b" in c["summary"]

    def test_move_to_sibling_appends_note(self):
        c = _phase_c_content_for(_cf(
            _finding(),
            outcome=OUTCOME_MOVE_TO_SIBLING,
            sibling_apps=["app-b"],
        ))
        assert "moving the declaration" in c["summary"].lower() or (
            "move" in c["summary"].lower()
        )


# ─────────────────────────────────────────────────────────────────────────────
# Per-finding dismiss-signature granularity
# ─────────────────────────────────────────────────────────────────────────────


class TestDismissSignatureGranularity:
    def test_different_entry_values_get_different_signatures(self):
        a = dismiss_signature_for_finding(
            kind=KIND_EXEC_UNUSED, app_id="i-task",
            entry_kind="exec", entry_value="scripts/a.py",
        )
        b = dismiss_signature_for_finding(
            kind=KIND_EXEC_UNUSED, app_id="i-task",
            entry_kind="exec", entry_value="scripts/b.py",
        )
        assert a != b

    def test_different_apps_get_different_signatures(self):
        a = dismiss_signature_for_finding(
            kind=KIND_EXEC_UNUSED, app_id="i-task-a",
            entry_kind="exec", entry_value="scripts/x.py",
        )
        b = dismiss_signature_for_finding(
            kind=KIND_EXEC_UNUSED, app_id="i-task-b",
            entry_kind="exec", entry_value="scripts/x.py",
        )
        assert a != b

    def test_different_kinds_get_different_signatures(self):
        a = dismiss_signature_for_finding(
            kind=KIND_EXEC_UNUSED, app_id="i-task",
            entry_kind="exec", entry_value="scripts/x.py",
        )
        b = dismiss_signature_for_finding(
            kind=KIND_EXEC_OVERKILL_WILDCARD, app_id="i-task",
            entry_kind="exec", entry_value="scripts/x.py",
        )
        assert a != b


# ─────────────────────────────────────────────────────────────────────────────
# observe()-level suppression gate
# ─────────────────────────────────────────────────────────────────────────────


def _run(home: Path, bot_id: str, shared_dir: Path, **kw):
    return observe(AppPermissionReviewContext(
        bot_id=bot_id,
        shared_dir=shared_dir,
        home_override=home,
        **kw,
    ))


class TestObserveSuppression:
    def test_observe_emits_when_no_dismiss(self, tmp_path):
        home = _make_bot(
            tmp_path, "team_bot_a",
            manifests={
                "i-task": {
                    "name": "Task App",
                    "files": [{"path": "scripts/real.py", "layer": "script"}],
                    "permissions": {
                        "exec": ["scripts/real.py", "scripts/ghost.py"],
                    },
                },
            },
            workspace_files={"scripts/real.py": "# real"},
        )
        shared = tmp_path / "shared"
        out = _run(home, "team_bot_a", shared)
        # At least one proposal — the ghost.py unused finding.
        assert any(
            p.dismiss_signature.endswith("scripts/ghost.py") for p in out
        )

    def test_observe_suppresses_dismissed_finding(self, tmp_path):
        """Dismiss the ghost.py finding signature; observe must skip
        emission of that one but keep emitting other findings."""
        home = _make_bot(
            tmp_path, "team_bot_a",
            manifests={
                "i-task": {
                    "name": "Task App",
                    "files": [{"path": "scripts/real.py", "layer": "script"}],
                    "permissions": {
                        # Two ghost entries; we'll dismiss one and keep
                        # the other.
                        "exec": [
                            "scripts/real.py",
                            "scripts/ghost.py",
                            "scripts/another_ghost.py",
                        ],
                    },
                },
            },
            workspace_files={"scripts/real.py": "# real"},
        )
        shared = tmp_path / "shared"
        # Dismiss ghost.py for this bot.
        dismissals.record_dismissal(
            shared,
            signature=dismiss_signature_for_finding(
                kind=KIND_EXEC_UNUSED, app_id="i-task",
                entry_kind="exec", entry_value="scripts/ghost.py",
            ),
            bot_id="team_bot_a",
            scope="kind",
        )
        out = _run(home, "team_bot_a", shared)
        # ghost.py is suppressed; another_ghost.py still emits.
        ghost_signatures = [p.dismiss_signature for p in out]
        assert not any(s.endswith("scripts/ghost.py") for s in ghost_signatures), (
            f"ghost.py should be suppressed; got {ghost_signatures}"
        )
        assert any(
            s.endswith("scripts/another_ghost.py") for s in ghost_signatures
        ), (
            f"another_ghost.py should still surface; got {ghost_signatures}"
        )

    def test_observe_consult_dismissals_false_bypasses_gate(self, tmp_path):
        home = _make_bot(
            tmp_path, "team_bot_a",
            manifests={
                "i-task": {
                    "name": "Task App",
                    "files": [{"path": "scripts/real.py", "layer": "script"}],
                    "permissions": {
                        "exec": ["scripts/real.py", "scripts/ghost.py"],
                    },
                },
            },
            workspace_files={"scripts/real.py": "# real"},
        )
        shared = tmp_path / "shared"
        dismissals.record_dismissal(
            shared,
            signature=dismiss_signature_for_finding(
                kind=KIND_EXEC_UNUSED, app_id="i-task",
                entry_kind="exec", entry_value="scripts/ghost.py",
            ),
            bot_id="team_bot_a",
            scope="kind",
        )
        out = _run(home, "team_bot_a", shared, consult_dismissals=False)
        # Gate bypassed — ghost.py still emits.
        assert any(p.dismiss_signature.endswith("scripts/ghost.py") for p in out)

    def test_observe_per_bot_suppression(self, tmp_path):
        """A dismiss for team_bot_a must not suppress for another bot."""
        # team_bot_a — has ghost.py finding, dismissed.
        home_a = _make_bot(
            tmp_path, "team_bot_a",
            manifests={
                "i-task": {
                    "name": "Task App",
                    "files": [{"path": "scripts/real.py", "layer": "script"}],
                    "permissions": {
                        "exec": ["scripts/real.py", "scripts/ghost.py"],
                    },
                },
            },
            workspace_files={"scripts/real.py": "# real"},
        )
        # ellie — same finding shape; should NOT be suppressed by team_bot_a's dismiss.
        home_b = _make_bot(
            tmp_path, "ellie",
            manifests={
                "i-task": {
                    "name": "Task App",
                    "files": [{"path": "scripts/real.py", "layer": "script"}],
                    "permissions": {
                        "exec": ["scripts/real.py", "scripts/ghost.py"],
                    },
                },
            },
            workspace_files={"scripts/real.py": "# real"},
        )
        shared = tmp_path / "shared"
        dismissals.record_dismissal(
            shared,
            signature=dismiss_signature_for_finding(
                kind=KIND_EXEC_UNUSED, app_id="i-task",
                entry_kind="exec", entry_value="scripts/ghost.py",
            ),
            bot_id="team_bot_a",
            scope="kind",
        )
        out_a = _run(home_a, "team_bot_a", shared)
        out_b = _run(home_b, "ellie", shared)
        assert not any(
            p.dismiss_signature.endswith("scripts/ghost.py") for p in out_a
        )
        assert any(
            p.dismiss_signature.endswith("scripts/ghost.py") for p in out_b
        )
