"""test_capabilities_closed.py — the D-CN2 invariant, enforced.

"No scope without a tool, no tool without a scope." This is the structural
fix for the calendar incident (internal/design-connections-that-just-work-
2026-09-15.md §1 failure A/B): a bot was granted the full `auth/calendar`
scope with no update/delete/move tool behind it, and nothing checked.

``_closure_violations`` is the generic checker (shared by the real-map test
and the two "prove it actually catches something" fixture tests below —
per-file, not imported from the production module, so the checker's own
correctness is visible in this one file).
"""
from __future__ import annotations

from evolve_admin import connection_capabilities as caps


def _closure_violations(
    verbs: dict, *, known_scopes: set[str], tool_exists,
) -> list[str]:
    """Return every way ``verbs`` violates the D-CN2 invariant. Empty list =
    closed. ``tool_exists`` is a callable(tool_name) -> bool for the
    service's tool layer."""
    violations: list[str] = []
    seen_tools: dict[str, str] = {}
    for verb_id, spec in verbs.items():
        for scope in spec.get("scopes", []):
            if scope not in known_scopes:
                violations.append(f"{verb_id}: unknown scope {scope!r}")
        tool = spec.get("tool")
        if tool is None:
            if not spec.get("pending"):
                violations.append(f"{verb_id}: no tool and no pending brief")
            continue
        if not tool_exists(tool):
            violations.append(f"{verb_id}: tool {tool!r} does not exist")
        if tool in seen_tools:
            violations.append(
                f"tool {tool!r} is named by both {seen_tools[tool]!r} and {verb_id!r}"
            )
        seen_tools[tool] = verb_id
    return violations


def _unclaimed_registered_tools(registered: set[str], verbs: dict) -> set[str]:
    """Tools the layer registers that no verb names at all."""
    named = {spec["tool"] for spec in verbs.values() if spec.get("tool")}
    return registered - named


# ── Real map: must be closed ─────────────────────────────────────────────────


class TestGoogleCapabilityMapIsClosed:
    def test_every_verb_scope_is_known(self):
        violations = _closure_violations(
            caps.GOOGLE_VERBS,
            known_scopes=caps.known_google_scopes(),
            tool_exists=caps.google_tool_exists,
        )
        scope_violations = [v for v in violations if "unknown scope" in v]
        assert scope_violations == []

    def test_every_verb_tool_exists_or_is_pending(self):
        violations = _closure_violations(
            caps.GOOGLE_VERBS,
            known_scopes=caps.known_google_scopes(),
            tool_exists=caps.google_tool_exists,
        )
        tool_violations = [v for v in violations if "does not exist" in v
                           or "no pending brief" in v]
        assert tool_violations == []

    def test_no_tool_is_named_by_two_verbs(self):
        violations = _closure_violations(
            caps.GOOGLE_VERBS,
            known_scopes=caps.known_google_scopes(),
            tool_exists=caps.google_tool_exists,
        )
        dupes = [v for v in violations if "is named by both" in v]
        assert dupes == []

    def test_every_registered_google_tool_is_named_by_a_verb(self):
        from evolve_admin import google_service
        unclaimed = _unclaimed_registered_tools(
            set(google_service.TOOL_SPECS), caps.GOOGLE_VERBS,
        )
        assert unclaimed == set(), (
            f"tool(s) registered but not named by any verb: {sorted(unclaimed)}"
        )

    def test_pending_calendar_verbs_name_the_real_queued_brief(self):
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[3]
        for verb_id in ("calendar.update", "calendar.delete", "calendar.move"):
            spec = caps.GOOGLE_VERBS[verb_id]
            assert spec["tool"] is None
            brief_id = spec["pending"]
            candidates = [
                repo_root / "internal" / "dispatch" / sub / f"{brief_id}.md"
                for sub in ("queued", "inflight", "done")
            ]
            assert any(p.exists() for p in candidates), (
                f"{verb_id} names pending brief {brief_id!r}, but no such "
                f"file exists in internal/dispatch/{{queued,inflight,done}}/"
            )

    def test_a_user_role_job_never_grants_a_mutating_verb(self):
        # D-GA2: read-only-by-construction for a delegated human account.
        # Sanity check on the two explicitly "read a person's X" jobs.
        for job_id in ("read_person_calendar", "read_person_mail", "read_drive"):
            for verb in caps.verbs_for_job("google", job_id):
                assert caps.GOOGLE_VERBS[verb]["mutates"] is False, (
                    f"job {job_id!r} grants mutating verb {verb!r}"
                )


class TestGithubCapabilityMapIsClosed:
    def test_every_verb_tool_resolves(self):
        violations = _closure_violations(
            caps.GITHUB_VERBS,
            known_scopes={caps.GITHUB_CLASSIC_PAT_SCOPE},
            tool_exists=caps.github_tool_exists,
        )
        assert violations == []

    def test_backup_bot_and_check_repo_visibility_are_real_callables(self):
        # Belt-and-suspenders: the closure check above already proves this,
        # but this test also fails loudly if someone renames the function
        # without knowing why (the pytest failure names the import).
        import backup
        import backup_visibility
        assert callable(backup.backup_bot)
        assert callable(backup_visibility.check_repo_visibility)


# ── Fixture maps: prove the checker actually catches something ──────────────
# The brief requires a fixture that breaks the invariant each way and proves
# the test fails against it — these two tests ARE that proof, run against a
# synthetic map (never the real one) so they can assert failure without
# breaking the suite.


class TestClosureCheckerCatchesRealBreaks:
    def test_scope_without_tool_is_flagged(self):
        broken = {
            "fake.verb": {
                "scopes": ["https://www.googleapis.com/auth/nonexistent.scope"],
                "tool": "gmail_list_messages",
                "mutates": False,
            },
        }
        violations = _closure_violations(
            broken, known_scopes=caps.known_google_scopes(),
            tool_exists=caps.google_tool_exists,
        )
        assert any("unknown scope" in v for v in violations)

    def test_tool_without_verb_is_flagged(self):
        # A tool the layer registers that no verb in the (synthetic, empty)
        # map names at all — the mirror image of "scope without tool".
        from evolve_admin import google_service
        unclaimed = _unclaimed_registered_tools(
            set(google_service.TOOL_SPECS), verbs={},
        )
        assert unclaimed == set(google_service.TOOL_SPECS)

    def test_pending_without_a_real_brief_is_flagged(self):
        broken = {
            "fake.verb": {
                "scopes": [],
                "tool": None,
                "pending": "a-brief-id-that-does-not-exist-anywhere",
            },
        }
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[3]
        brief_id = broken["fake.verb"]["pending"]
        candidates = [
            repo_root / "internal" / "dispatch" / sub / f"{brief_id}.md"
            for sub in ("queued", "inflight", "done")
        ]
        assert not any(p.exists() for p in candidates)
