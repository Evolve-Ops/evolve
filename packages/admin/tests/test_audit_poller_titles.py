"""tests/test_audit_poller_titles.py — Slice 2 (2026-06-04):
audit_poller emits plain-English titles driven by the audit
category, not the LLM's verbose description.

Operator feedback that motivated this:

> The title and test is really technical and hard to understand.
> As I understand it, the cron wasn't set up properly for the app.
> Or the scripts aren't wired properly. ... We need to spend more
> time when we generate these to be succinct in what the issue is
> ("scripts are not working" "scripts are not set up properly"
> whatever)

The mapping lives at ``_CATEGORY_HEADLINE`` in
``packages/admin/evolve_admin/applications/audit_poller.py``. Each
``Observation.category`` (from ``packages/analyzer/app_audit_tier3.py``)
gets a short phrase; the LLM's verbose ``description`` still lives in
the proposal body so detail is recoverable on expand.

Sibling: PR #2074/#2075 ships the dispatch flow (Slice 3 / Phase 3.1
+ 3.3); Phase 3.4 (separate chip) populates ``dispatch_target`` on
audit emissions so the operator gets a "Send to <bot>" button. Slice
2 (this file) handles the title humanization piece.
"""
from __future__ import annotations

import sys
from pathlib import Path


_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))

from evolve_admin.applications import audit_poller  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# _problem_line: category-driven plain-English title
# ─────────────────────────────────────────────────────────────────────────────


def test_broken_path_renders_plain_phrase():
    """A broken_path finding renders as
    ``{app_id}: a path or reference doesn't resolve``, NOT the
    verbose LLM description. The full description survives in the
    proposal body via _audit_context_builder."""
    record = {
        "app_id": "ea-pack",
        "category": "broken_path",
        "description": (
            "Both ea-morning.sh and ea-evening.sh invoke Python via "
            "/Users/Shared/evolve-venv/bin/python3, but this venv path "
            "is not guaranteed to exist on this host. The default "
            "/usr/bin/python3 is the safer choice for shell-launched "
            "scripts."
        ),
    }
    title = audit_poller._problem_line(record)
    assert title == "ea-pack: a path or reference doesn't resolve", (
        f"unexpected title: {title!r}"
    )


def test_behavior_mismatch_renders_plain_phrase():
    """behavior_mismatch → 'code doesn't match what the manifest claims'."""
    record = {
        "app_id": "ea-pack",
        "category": "behavior_mismatch",
        "description": (
            "The build spec states 'Fail silently with log entry' when "
            "the gateway is unreachable (constraints.boundaries[0]). In "
            "practice, morning_brief.py and evening_sweep.py both raise "
            "SystemExit(1) on gateway errors."
        ),
    }
    title = audit_poller._problem_line(record)
    assert title == "ea-pack: code doesn't match what the manifest claims"


def test_missing_functionality_renders_plain_phrase():
    """missing_functionality is the canonical 'manifest promises X
    but X isn't there' case the operator complained about."""
    record = {
        "app_id": "ea-pack",
        "category": "missing_functionality",
        "description": (
            "The manifest success_criteria.observable_outcomes[3] "
            "states 'Commitments captured in memory/contacts/{person}.md "
            "and surfaced as follow-ups within 24 hours.' The "
            "commitment_tracker.py --list-due mode exists and works, but "
            "there is no scheduled job (cron entry, launchd plist, or "
            "heartbeat hook) that actually calls it automatically."
        ),
    }
    title = audit_poller._problem_line(record)
    assert title == (
        "ea-pack: manifest claims a feature that isn't fully wired up"
    )


def test_manifest_drift_renders_plain_phrase():
    """manifest_drift → 'manifest is out of date with reality'."""
    record = {
        "app_id": "task-manager",
        "category": "manifest_drift",
        "description": (
            "The manifest lists `manifest.crons` as an empty array, "
            "but the build spec specifies that `task-check.sh` should "
            "be registered as a LaunchAgent plist running every hour "
            "during work hours."
        ),
    }
    title = audit_poller._problem_line(record)
    assert title == "task-manager: manifest is out of date with reality"


def test_dead_code_renders_plain_phrase():
    record = {
        "app_id": "ea-pack",
        "category": "dead_code",
        "description": (
            "ea_config() is defined in morning_brief.py and "
            "evening_sweep.py but is never called anywhere in those "
            "scripts. The function loads ea-pack configuration."
        ),
    }
    title = audit_poller._problem_line(record)
    assert title == "ea-pack: unused code path"


def test_code_smell_renders_plain_phrase():
    record = {
        "app_id": "task-manager",
        "category": "code_smell",
        "description": "TAG_ALIASES dict has 30+ entries, hard to scan.",
    }
    title = audit_poller._problem_line(record)
    assert title == "task-manager: code pattern looks risky"


# ─────────────────────────────────────────────────────────────────────────────
# Fallbacks for unknown categories
# ─────────────────────────────────────────────────────────────────────────────


def test_unknown_category_falls_back_to_slug():
    """If the audit grows a new category we haven't humanized yet,
    the title falls back to the slug (better than nothing — the
    missing entry is then obvious in the queue for follow-up)."""
    record = {
        "app_id": "task-manager",
        "category": "some_new_category",
        "description": "irrelevant",
    }
    title = audit_poller._problem_line(record)
    assert title == "task-manager: some_new_category"


def test_empty_category_falls_back_to_generic_phrase():
    """A record missing a category renders as ``{app}: structural
    finding`` rather than crashing or rendering ``{app}: ``."""
    record = {
        "app_id": "task-manager",
        "description": "irrelevant",
    }
    title = audit_poller._problem_line(record)
    assert title == "task-manager: structural finding"


def test_missing_app_id_renders_unknown():
    """When the record has no app_id (e.g. malformed input), title
    leads with ``unknown`` rather than ``None`` or crash."""
    record = {"category": "broken_path", "description": "x"}
    title = audit_poller._problem_line(record)
    assert title == "unknown: a path or reference doesn't resolve"


# ─────────────────────────────────────────────────────────────────────────────
# Title length budget — fits in the proposal card without ellipsis
# ─────────────────────────────────────────────────────────────────────────────


def test_all_category_titles_fit_120_chars():
    """``admin_surface_summary`` is truncated to 120 chars at write
    time (Proposal field). Every category headline must fit comfortably
    inside that budget with a reasonable app_id prefix (~30 chars)."""
    # Use a realistic-length app_id to bound the headline. Bot id is a
    # placeholder per docs/PLACEHOLDER_NAMING.md.
    APP = "unified-task-system-prod-bot-team-a"  # 35 chars
    for category in audit_poller._CATEGORY_HEADLINE:
        title = audit_poller._problem_line(
            {"app_id": APP, "category": category, "description": ""}
        )
        assert len(title) <= 120, (
            f"category {category!r} title too long: {len(title)} chars: "
            f"{title!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# No verbose description leaks into the title
# ─────────────────────────────────────────────────────────────────────────────


def test_long_description_does_not_leak_into_title():
    """The pre-Slice-2 behavior was to dump the LLM's description
    verbatim into the title, truncated to 200 chars. Slice 2 ignores
    the description entirely for the title. Pin that: a 500-char
    description should leave NO description text in the title."""
    record = {
        "app_id": "ea-pack",
        "category": "broken_path",
        "description": "x" * 500,
    }
    title = audit_poller._problem_line(record)
    assert "x" not in title.split(":")[1], (
        "verbose description leaked into the title — Slice 2 should "
        "use the category headline only, leaving the description for "
        "the proposal body"
    )


# ─────────────────────────────────────────────────────────────────────────────
# _structural_finding_title: human Signal titles for the structural verifier
#
# Reports review 2026-06-12: 100+ firing Signals had titles that were just the
# signature echoed back — ``i-0bcaa46e: app_discoverability_no_cli`` — an
# ``<id>: <assertion_id>`` string no operator can parse. _structural_finding_title
# renders ``{App name}: {plain-English phrase}`` instead, and substitutes
# "An app" when the manifest display name can't be loaded so the cryptic
# ``i-XXXXXXXX`` app_id never leads the title.
# ─────────────────────────────────────────────────────────────────────────────


def test_structural_title_is_human_not_id_type():
    """The exact case from the review: a discoverability finding on an app
    whose display name resolves renders a human one-liner, NOT
    ``i-0bcaa46e: app_discoverability_no_cli``."""
    title = audit_poller._structural_finding_title(
        "Job Search Tracker", "app_discoverability_no_cli"
    )
    assert title == "Job Search Tracker: no CLI command to invoke it"
    # The illegible <id>: <type> shape is gone.
    assert "app_discoverability_no_cli" not in title
    assert not title.startswith("i-")


def test_structural_title_without_display_name_says_an_app():
    """When the manifest can't be loaded (display_name=""), the title leads
    with "An app" rather than leaking the cryptic app_id. The id stays
    recoverable via details.app_id, which the caller still populates."""
    title = audit_poller._structural_finding_title(
        "", "scheduled_action_orphan_install"
    )
    assert title == (
        "An app: an installed job points at a removed scheduled action"
    )
    assert "i-" not in title  # no hex app_id leaked


def test_structural_title_unknown_assertion_falls_back_generic():
    """A freshly-added assertion we haven't humanized yet renders a generic
    structural phrase — never the raw slug echoed as the whole title."""
    title = audit_poller._structural_finding_title(
        "Recipe Box", "some_brand_new_assertion"
    )
    assert title == "Recipe Box: a structural check failed"
    assert "some_brand_new_assertion" not in title


def test_structural_title_never_echoes_the_assertion_slug():
    """For every mapped assertion_id, the rendered phrase is human prose, not
    the slug — the core fix. (Catches a future entry that accidentally maps a
    slug to itself.)"""
    for assertion_id in audit_poller._ASSERTION_HEADLINE:
        title = audit_poller._structural_finding_title("App", assertion_id)
        phrase = title.split(": ", 1)[1]
        assert phrase != assertion_id
        assert "_" not in phrase, (
            f"phrase for {assertion_id!r} looks like a slug: {phrase!r}"
        )


def test_all_structural_titles_fit_120_chars():
    """The Signal title clamps for the Alerts card; every headline must fit
    comfortably with a realistic app name (placeholder per
    docs/PLACEHOLDER_NAMING.md)."""
    APP = "Unified Task System (team-a)"  # ~28 chars, realistic
    for assertion_id in audit_poller._ASSERTION_HEADLINE:
        title = audit_poller._structural_finding_title(APP, assertion_id)
        assert len(title) <= 120, (
            f"assertion {assertion_id!r} title too long: {len(title)} chars: "
            f"{title!r}"
        )
