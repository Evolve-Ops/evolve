"""Tests for the app-identity ownership predicate (``can_app_own``).

Background: a scanner run stamped ``_evolve`` provenance markers onto ~1,043
audit-telemetry files on each of two bots under
``evolve/audit_outbox/_ingested/.../rec-*.json`` plus OC-standard files like
``AGENTS.md`` — paths that can never legitimately be owned by an application.
``can_app_own`` is the ONE shared predicate the marker-writer, reflect reader,
and file_index will consume so the answer is identical everywhere.

These tests pin: (a) the never-ownable classes return False, (b) ordinary app
files return True, and (c) the predicate AGREES with scanner's existing
exclusion helpers on every overlapping case (proving no divergent copy).

Spec: docs/spec-app-identity-ledger-2026-06-27.md (§4.A, §6).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import scanner as _scanner  # noqa: E402
from evolve_admin.applications.app_ownership_policy import (  # noqa: E402
    can_app_own,
)


# ── Never-ownable: the evolve/ telemetry tree ────────────────────────────────

# The literal file that triggered this work, plus a representative path under
# each enumerated subtree of evolve/.
_TELEMETRY_PATHS = [
    "evolve/audit_outbox/_ingested/2026-06-07/rec-ec4f1860.json",
    "evolve/audit_inbox/req-1234.json",
    "evolve/audits/2026-06-07/structural.json",
    "evolve/skill_audits/slack.json",
    "evolve/provider_audits/anthropic.json",
    "evolve/investigations/inv-99.json",
    "evolve/signals/firing/sig-abc.json",
    "evolve/manifests/i-deadbeef.json",
    "evolve/forge/inbox/j-1.json",
    "evolve/logs/2026-06-07.jsonl",
    "evolve/rec-hints.json",
    "evolve-backup/openclaw.json",
    "memory/turns-2026-06-07.jsonl",
]


@pytest.mark.parametrize("rel", _TELEMETRY_PATHS)
def test_telemetry_paths_never_ownable(rel: str) -> None:
    assert can_app_own(rel) is False


def test_audit_outbox_marker_target_is_never_ownable() -> None:
    # The exact shape of the ~1,043 wrongly-stamped files on atlas.
    rel = "evolve/audit_outbox/_ingested/2026-06-07/rec-ec4f1860.json"
    assert can_app_own(rel) is False
    # A trailing ``<tag>:`` prefix and ``#anchor`` (how scanner emits evidence)
    # must normalize to the same verdict.
    assert can_app_own("file: " + rel) is False
    assert can_app_own(rel + "#frag") is False


# ── Never-ownable: reserved dirs ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "rel",
    [
        "manifests/i-deadbeef.json",
        "manifests/.scan-status.json",
        "manifests/_history/old.json",
        "workspace/manifests/i-x.json",  # manifests as a non-leading component
        ".openclaw/openclaw.json",
        ".git/config",
    ],
)
def test_reserved_dirs_never_ownable(rel: str) -> None:
    assert can_app_own(rel) is False


# ── Never-ownable: OpenClaw-reserved + task-substrate filenames ──────────────

@pytest.mark.parametrize(
    "rel",
    [
        "AGENTS.md",
        "HEARTBEAT.md",
        "IDENTITY.md",
        "BOOTSTRAP.md",
        "SOUL.md",
        "USER.md",
        "MEMORY.md",
        "POD_CONDUCT.md",
        # nested still reserved — matched on basename
        "home/AGENTS.md",
        "deep/nested/dir/SOUL.md",
        # case-insensitive
        "agents.md",
        # task-substrate standard files
        "TASKS.md",
        "TAGS.md",
        "tasks.json",
        "home/TASKS.md",
    ],
)
def test_reserved_filenames_never_ownable(rel: str) -> None:
    assert can_app_own(rel) is False


# ── Ownable: ordinary application files ──────────────────────────────────────

@pytest.mark.parametrize(
    "rel",
    [
        "scripts/tasks.py",      # NOT tasks.json — an app's own orchestration
        "home/journal.py",
        "communication_hub/hub.py",
        "data/notes.md",
        "config/settings.json",  # generic app config, not a reserved name
        "tags.py",               # NOT tags.md
        "evolve_helpers.py",     # 'evolve' as a filename stem, not a dir component
        "src/evolver/run.py",    # 'evolver' != 'evolve' component
    ],
)
def test_ordinary_app_files_ownable(rel: str) -> None:
    assert can_app_own(rel) is True


# ── Never-ownable: generated-runtime / secret file classes (atlas) ───────────

# These surfaced as wrongly-CLAIMED paths on ``atlas`` — runtime state / crypto
# material / generated indices an app's process writes, never authored source.
# A Fix=stamp on them would corrupt a secret or store file.
@pytest.mark.parametrize(
    "rel",
    [
        # C2 — cryptographic salt / secret material.
        "atlas/.capture-salt",
        "atlas/member-hash-salt.bin",
        "deep/nested/some-salt.key",
        # C2 — append-only runtime log streams.
        "atlas/capture-log.jsonl",
        "atlas/research-log.jsonl",
        "logs/app-log.json",
        "audit.log.jsonl",
        # C3 — generated archive telemetry index.
        "archive/index.json",
        "data/archive/index.json",
    ],
)
def test_generated_runtime_and_secret_paths_never_ownable(rel: str) -> None:
    assert can_app_own(rel) is False


# Precision boundary: these LOOK similar but are legitimate app files and MUST
# stay ownable. The over-removal guard depends on each rule being anchored so it
# cannot strip a real claim.
@pytest.mark.parametrize(
    "rel",
    [
        "atlas/llm-config.json",   # genuinely-ownable app config (a deleted one
                                   # stays missing_file — a manual decision)
        "catalog.json",           # 'log' is mid-word, not a log stream
        "changelog.jsonl",        # 'log' preceded by 'e', not a separator
        "dialog.json",            # 'log' mid-word
        "salt_recipe.py",         # 'salt' token but ordinary source extension
        "basalt.json",            # 'salt' substring inside a word, .json data
        "archive/article.md",     # real archived content, not the index
        "data/index.json",        # index.json NOT under an archive/ component
        "src/log.py",             # a logging module, not a .json/.jsonl stream
    ],
)
def test_lookalike_app_files_stay_ownable(rel: str) -> None:
    assert can_app_own(rel) is True


def test_name_kwarg_is_accepted_and_inert() -> None:
    # ``name`` is forward-compat surface; it must not change today's answer.
    assert can_app_own("home/journal.py", name="Daily Journal") is True
    assert can_app_own("AGENTS.md", name="Daily Journal") is False


def test_empty_and_tag_only_inputs_not_ownable() -> None:
    assert can_app_own("") is False
    assert can_app_own("   ") is False
    assert can_app_own("file:") is False
    assert can_app_own("/") is False


# ── Agreement with scanner's existing exclusion (no divergent copy) ──────────

# Every overlapping case where scanner already has an opinion: can_app_own must
# return False exactly when scanner classifies the path as platform-written /
# manifest-store / OC-identity. This is what proves the policy reuses scanner's
# canonical predicates rather than drifting from them.
_SCANNER_EXCLUDED = [
    "evolve/audit_outbox/_ingested/2026-06-07/rec-ec4f1860.json",
    "evolve/audit_inbox/req-1234.json",
    "evolve/audits/2026-06-07/structural.json",
    "evolve/skill_audits/slack.json",
    "evolve/provider_audits/anthropic.json",
    "evolve/investigations/inv-99.json",
    "memory/turns-2026-06-07.jsonl",
    "manifests/.scan-status.json",
    "manifests/_history/old.json",
    "AGENTS.md",
    "home/SOUL.md",
]


@pytest.mark.parametrize("rel", _SCANNER_EXCLUDED)
def test_agrees_with_scanner_exclusion(rel: str) -> None:
    scanner_excludes = (
        _scanner._is_platform_written_path(rel)
        or _scanner._is_manifest_store_path(rel)
        or _scanner._is_oc_identity_system_file(rel)
    )
    assert scanner_excludes is True, "test path should be scanner-excluded"
    assert can_app_own(rel) is False


@pytest.mark.parametrize("rel", ["scripts/tasks.py", "home/journal.py"])
def test_agrees_with_scanner_on_ownable(rel: str) -> None:
    scanner_excludes = (
        _scanner._is_platform_written_path(rel)
        or _scanner._is_manifest_store_path(rel)
        or _scanner._is_oc_identity_system_file(rel)
    )
    assert scanner_excludes is False
    assert can_app_own(rel) is True
