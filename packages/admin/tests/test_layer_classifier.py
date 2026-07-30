"""Layer classifier tests — PR 3 of the coherence + reconciliation framework.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §3.1, §3.5,
§7.1.

The classifier decides which of the nine v20 layer values a file belongs
to. Reconciliation severity (§3.2) depends on the layer — a missing
``code`` file is critical, a missing ``data`` file is info — so wrong
classifications produce wrong reconciliation behavior. Tests pin the
cascade against real-world filenames pulled from validation against the
production pod (the personal-bot, team-bot-a, security-bot fixtures
mirror what personal-bot, team-bot-a, security-bot actually have on disk).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.layer_classifier import (  # noqa: E402
    CLASSIFICATION_METHOD_DIRECTORY,
    CLASSIFICATION_METHOD_EXTENSION,
    CLASSIFICATION_METHOD_FILENAME,
    CLASSIFICATION_METHOD_LLM,
    CLASSIFICATION_METHOD_REFERENCE,
    CLASSIFICATION_METHOD_UNCLASSIFIED,
    CLASSIFICATION_METHOD_VOLATILE_PATH,
    VALID_LAYERS,
    apply_to_manifest,
    classify,
)


# ── Rule 1: volatile_paths ─────────────────────────────────────────────────

def test_volatile_paths_glob_wins_over_everything() -> None:
    """Operator-declared volatile_paths are authoritative — even when the
    filename or extension would suggest a different layer."""
    # journal.py looks like code by extension, but the operator declared
    # data/*.py as volatile data (rare but legal).
    result = classify(
        "data/journal.py",
        volatile_paths=[{"glob": "data/*.py", "layer": "data"}],
    )
    assert result.layer == "data"
    assert result.method == CLASSIFICATION_METHOD_VOLATILE_PATH
    assert result.confidence == "high"
    assert "data/*.py" in result.evidence


def test_volatile_paths_invalid_layer_ignored() -> None:
    """A volatile_paths entry with a layer outside the v20 enum is
    treated as misconfigured and skipped — falls through to subsequent
    rules. Prevents typos in operator-authored volatile_paths from
    silently mis-classifying files."""
    result = classify(
        "scripts/foo.py",
        volatile_paths=[{"glob": "scripts/*.py", "layer": "typo"}],
    )
    # Falls through to directory rule.
    assert result.layer == "code"
    assert result.method == CLASSIFICATION_METHOD_DIRECTORY


def test_volatile_paths_empty_list_no_effect() -> None:
    """An empty volatile_paths list / None doesn't affect classification."""
    for vp in (None, [], [{}], [{"glob": ""}]):
        result = classify("scripts/foo.py", volatile_paths=vp)
        assert result.layer == "code"
        assert result.method == CLASSIFICATION_METHOD_DIRECTORY


# ── Rule 2: filename pattern ────────────────────────────────────────────────

def test_behavior_doc_filenames_classify_correctly() -> None:
    """The bot's standing-instruction surfaces. Validation against personal-bot
    (personal-bot) confirmed these specific filenames are the canonical
    set across the production pod. Wrong classification of these caused
    real production noise (HEARTBEAT.md stamped 'state' in personal-bot's v19
    manifests)."""
    for filename in ("AGENTS.md", "HEARTBEAT.md", "SOUL.md",
                     "POD_CONDUCT.md", "USER.md", "MAGIC_COMMANDS.md",
                     "SECURITY_PROTOCOLS.md"):
        result = classify(filename)
        assert result.layer == "behavior_doc", (
            f"{filename} should be behavior_doc, got {result.layer}"
        )
        assert result.method == CLASSIFICATION_METHOD_FILENAME
        assert result.confidence == "high"


def test_reference_doc_filenames_classify_correctly() -> None:
    """Operator-facing docs that code doesn't load."""
    for filename in ("README.md", "LICENSE", "CHANGELOG.md",
                     "CONTRIBUTING.md", "SECURITY.md",
                     "INSTALLED_APPS.md", "RUNTIME_NOTES.md"):
        result = classify(filename)
        assert result.layer == "reference", (
            f"{filename} should be reference, got {result.layer}"
        )
        assert result.method == CLASSIFICATION_METHOD_FILENAME


def test_contract_filenames_classify_correctly() -> None:
    """Manifest + schema files are contract layer."""
    for filename in ("manifest.json", "schema.json", "schema.yaml",
                     "spec.json"):
        result = classify(filename)
        assert result.layer == "contract", (
            f"{filename} should be contract, got {result.layer}"
        )


def test_filename_glob_log_files() -> None:
    """*.log files are log layer regardless of directory."""
    for filename in ("app.log", "errors.log", "audit.log"):
        result = classify(filename)
        assert result.layer == "log"
        assert result.method == CLASSIFICATION_METHOD_FILENAME


def test_filename_glob_state_files() -> None:
    """Locks, pids, caches, etc. are state."""
    for filename in ("daemon.lock", "process.pid", "session.tmp",
                     "build.cache", "training.ckpt"):
        result = classify(filename)
        assert result.layer == "state", f"{filename} got {result.layer}"
        assert result.method == CLASSIFICATION_METHOD_FILENAME


def test_dated_journal_filename_is_data() -> None:
    """YYYY-MM-DD.md is the personal-bot journal pattern — clearly data,
    not behavior_doc, despite the .md extension. Filename glob rule
    catches this before the extension default would mis-classify."""
    for filename in ("2026-06-05.md", "2026-01-01.md", "2025-12-31.md"):
        result = classify(f"memory/{filename}")
        assert result.layer == "data", f"{filename} got {result.layer}"


# ── Rule 3: parent-directory walk ──────────────────────────────────────────

def test_parent_directory_scripts_is_code() -> None:
    """scripts/ directory → code layer, regardless of extension."""
    result = classify("scripts/check-cost.py")
    assert result.layer == "code"
    assert result.method == CLASSIFICATION_METHOD_DIRECTORY
    assert "scripts/" in result.evidence


def test_parent_directory_data_is_data() -> None:
    """data/ directory → data layer, even for files the extension would
    classify differently."""
    result = classify("data/observations.json")
    assert result.layer == "data"
    assert result.method == CLASSIFICATION_METHOD_DIRECTORY


def test_parent_directory_walks_to_grandparent() -> None:
    """When the immediate parent isn't in the map, walk up."""
    # util/ isn't in the map; scripts/ is.
    result = classify("scripts/util/helper.py")
    assert result.layer == "code"
    assert "scripts/" in result.evidence


def test_parent_directory_at_root_no_match() -> None:
    """A file at the workspace root with no parent directory falls
    through to the extension rule."""
    result = classify("rootfile.py")
    assert result.layer == "code"
    assert result.method == CLASSIFICATION_METHOD_EXTENSION


def test_procedures_dir_is_procedure_layer() -> None:
    """procedures/ → procedure layer (LLM-readable runbooks the
    heartbeat loads). Production calibration 2026-06-06: this layer
    name appeared in real manifests but wasn't in VALID_LAYERS, so
    every procedure file got reclassified."""
    result = classify("procedures/security-cve-scan.md")
    assert result.layer == "procedure"
    assert result.method == CLASSIFICATION_METHOD_DIRECTORY


def test_runbooks_dir_is_procedure_layer() -> None:
    result = classify("runbooks/incident-response.md")
    assert result.layer == "procedure"


def test_procedure_in_valid_layers() -> None:
    assert "procedure" in VALID_LAYERS


def test_parent_directory_log_directory() -> None:
    """logs/ → log layer."""
    result = classify("logs/audit.txt")
    assert result.layer == "log"
    assert result.method == CLASSIFICATION_METHOD_DIRECTORY


def test_parent_directory_content_directory() -> None:
    """content/ + articles/ + templates/ + drafts/ → content layer."""
    for path in ("content/article.md", "articles/topic.md",
                 "templates/email.md", "drafts/blog.md"):
        result = classify(path)
        assert result.layer == "content", f"{path} got {result.layer}"


def test_parent_directory_docs_is_reference() -> None:
    """docs/ → reference layer."""
    result = classify("docs/design.md")
    assert result.layer == "reference"
    assert result.method == CLASSIFICATION_METHOD_DIRECTORY


def test_parent_directory_memory_is_data() -> None:
    """memory/ → data layer. personal-bot's pattern."""
    result = classify("memory/2026-06-05.md")
    # Filename glob (dated journal) wins first, but memory/ would also
    # catch this — either way it's data.
    assert result.layer == "data"


# ── Rule 4: extension fallback ─────────────────────────────────────────────

def test_extension_code_files() -> None:
    """Code extensions at the workspace root (no directory match)."""
    for filename in ("helper.py", "build.sh", "client.js",
                     "types.ts", "loader.rb"):
        result = classify(filename)
        assert result.layer == "code", f"{filename} got {result.layer}"
        assert result.method == CLASSIFICATION_METHOD_EXTENSION


def test_extension_config_files() -> None:
    """Config extensions at the workspace root, no reference-graph data."""
    for filename in ("settings.yaml", "config.toml", "options.ini"):
        result = classify(filename)
        assert result.layer == "config", f"{filename} got {result.layer}"


def test_extension_data_files() -> None:
    """Data extensions (.jsonl, .csv, .sqlite)."""
    for filename in ("events.jsonl", "users.csv", "store.sqlite"):
        result = classify(filename)
        assert result.layer == "data", f"{filename} got {result.layer}"
        assert result.method == CLASSIFICATION_METHOD_EXTENSION


def test_extension_md_default_is_reference() -> None:
    """Unknown .md files (no filename pattern, no directory match) fall
    to reference. Safer default than behavior_doc — promoting later is
    cheap, demoting after the chip fires is expensive."""
    result = classify("notes.md")
    assert result.layer == "reference"


def test_extension_unknown_extension_is_unclassified() -> None:
    """An extension we don't know about falls through to unclassified."""
    result = classify("unknown.xyz")
    assert result.layer is None
    assert result.method == CLASSIFICATION_METHOD_UNCLASSIFIED


# ── Rule 5: reference-graph refinement ─────────────────────────────────────

def test_json_demoted_to_data_when_no_code_reference() -> None:
    """A .json file at the root with no code referencing it gets demoted
    from the config default to data. Spec §3.4: 'classifier may demote
    to data if no code reads it.'"""
    result = classify(
        "observations.json",
        code_file_contents={"scripts/main.py": "print('hello')"},
    )
    assert result.layer == "data"
    assert result.method == CLASSIFICATION_METHOD_REFERENCE
    assert "demoted" in result.evidence


def test_json_stays_config_when_code_references_it() -> None:
    """A .json file referenced by a code file stays config — code is
    reading it as structured config."""
    result = classify(
        "settings.json",
        code_file_contents={
            "scripts/main.py": "json.load(open('settings.json'))"
        },
    )
    assert result.layer == "config"
    assert result.method == CLASSIFICATION_METHOD_EXTENSION


def test_json_matches_by_basename_when_code_uses_relative_path() -> None:
    """The reference check matches both the full path and the basename
    so code that's chdir'd into the data directory still resolves."""
    result = classify(
        "data/lookup.json",
        code_file_contents={
            "scripts/main.py": "open('lookup.json').read()"
        },
    )
    # data/ directory rule fires before extension fallback, so this
    # is data (not config). The reference rule doesn't apply because
    # the directory match already classified it.
    assert result.layer == "data"


def test_md_promoted_to_content_when_code_reads_it() -> None:
    """An .md file that code reads at runtime (article, template, prompt)
    gets promoted from reference to content."""
    result = classify(
        "article.md",
        code_file_contents={
            "scripts/digest.py": "open('article.md').read()"
        },
    )
    assert result.layer == "content"
    assert result.method == CLASSIFICATION_METHOD_REFERENCE
    assert "promoted" in result.evidence


def test_reference_graph_skipped_when_contents_not_passed() -> None:
    """When the caller doesn't pass code_file_contents, rule 5 is
    skipped — no false positives from missing data."""
    result = classify("settings.json")
    assert result.layer == "config"
    assert result.method == CLASSIFICATION_METHOD_EXTENSION


# ── Rule 6: LLM fallback hook ──────────────────────────────────────────────

def test_llm_fallback_off_by_default() -> None:
    """PR 3 ships with use_llm=False. The hook returns unclassified
    without actually invoking an LLM (PR 4+ wires the dispatch)."""
    result = classify("unknown.xyz")
    assert result.layer is None
    assert result.method == CLASSIFICATION_METHOD_UNCLASSIFIED


def test_llm_fallback_returns_llm_method_when_enabled() -> None:
    """With use_llm=True, the method becomes ``llm_fallback`` so the
    caller can distinguish 'we tried but couldn't' from 'we never
    tried.' PR 3 still doesn't call an LLM (the wiring is the hook
    only)."""
    result = classify("unknown.xyz", use_llm=True)
    assert result.layer is None
    assert result.method == CLASSIFICATION_METHOD_LLM


# ── Real-world validation cases (from spec §17) ────────────────────────────

def test_scout_heartbeat_md_correctly_classified() -> None:
    """personal-bot's HEARTBEAT.md was stamped 'state' in v19 — the legacy
    mis-classification that motivated PR 3. Validate that the new
    classifier puts it in behavior_doc."""
    result = classify("HEARTBEAT.md")
    assert result.layer == "behavior_doc"


def test_marie_content_files_correctly_classified() -> None:
    """Marie/*.md content files were stamped 'state' in personal-bot's v19
    manifests — another legacy mis-classification PR 3 fixes."""
    # The marie/ directory isn't in our default map (it's project-
    # specific), so this falls to the extension default of reference.
    # When the manifest declares marie/*.md as a volatile content path,
    # rule 1 fires and overrides — that's the operator's escape hatch.
    result = classify(
        "marie/article.md",
        volatile_paths=[{"glob": "marie/*.md", "layer": "content"}],
    )
    assert result.layer == "content"
    assert result.method == CLASSIFICATION_METHOD_VOLATILE_PATH


def test_personal_bot_daily_journal_classified_as_data() -> None:
    """memory/2026-06-05.md — personal-bot's daily journal pattern.
    Falls to data via filename glob OR directory rule (either way)."""
    result = classify("memory/2026-06-05.md")
    assert result.layer == "data"


# ── apply_to_manifest integration ──────────────────────────────────────────

def test_apply_to_manifest_classifies_every_file() -> None:
    """The manifest-level wrapper iterates files[] and applies the
    cascade to each."""
    manifest = {
        "files": [
            {"path": "scripts/main.py", "layer": "script"},
            {"path": "HEARTBEAT.md", "layer": "state"},
            {"path": "data/journal.md", "layer": "state"},
            {"path": "config/settings.yaml"},  # no prior layer
        ],
        "volatile_paths": [],
    }
    changes = apply_to_manifest(manifest)
    # Three changed: script→code, state→behavior_doc, state→data
    # config/settings.yaml had no prior layer, so it's added but not
    # in the changes list (changes only includes actual transitions).
    paths_changed = {c[0] for c in changes}
    assert "scripts/main.py" in paths_changed
    assert "HEARTBEAT.md" in paths_changed
    assert "data/journal.md" in paths_changed
    # New classifications applied to the manifest:
    by_path = {f["path"]: f for f in manifest["files"]}
    assert by_path["scripts/main.py"]["layer"] == "code"
    assert by_path["HEARTBEAT.md"]["layer"] == "behavior_doc"
    assert by_path["data/journal.md"]["layer"] == "data"
    assert by_path["config/settings.yaml"]["layer"] == "config"


def test_apply_to_manifest_preserves_pr1_legacy_layer_capture() -> None:
    """If PR 1's migration already captured ``_legacy_layer = 'script'``,
    PR 3's classifier MUST NOT overwrite that record with a more
    recent value. The original capture is the canonical trail."""
    manifest = {
        "files": [
            # PR 1 migrated this from layer='script' to layer='code'
            # and stamped _legacy_layer='script'. The on-disk state
            # post-migration is the input here.
            {"path": "scripts/main.py", "layer": "code",
             "_legacy_layer": "script"},
        ],
        "volatile_paths": [],
    }
    apply_to_manifest(manifest)
    # _legacy_layer still records the original v19 value.
    assert manifest["files"][0]["_legacy_layer"] == "script"


def test_apply_to_manifest_captures_legacy_on_first_change() -> None:
    """When the classifier changes a layer for the first time, the
    prior value is captured as ``_legacy_layer`` so the trail can
    reconstruct what changed."""
    manifest = {
        "files": [
            # Suppose the legacy stamper put HEARTBEAT.md at 'state'
            # and PR 1's migration didn't touch it (since 'state' is
            # a valid v20 layer; only 'script' is auto-mapped).
            {"path": "HEARTBEAT.md", "layer": "state"},
        ],
        "volatile_paths": [],
    }
    apply_to_manifest(manifest)
    assert manifest["files"][0]["layer"] == "behavior_doc"
    assert manifest["files"][0]["_legacy_layer"] == "state"


def test_apply_to_manifest_stamps_classification_method_on_every_entry() -> None:
    """Every files[*] entry gains a classification_method field so the
    trail can attribute decisions even when the layer didn't change."""
    manifest = {
        "files": [
            {"path": "scripts/main.py", "layer": "code"},  # no change
        ],
        "volatile_paths": [],
    }
    apply_to_manifest(manifest)
    assert manifest["files"][0]["classification_method"] == \
        CLASSIFICATION_METHOD_DIRECTORY


def test_apply_to_manifest_handles_unclassifiable_without_clobbering() -> None:
    """When the classifier can't classify (no rule fires), the entry's
    existing layer must be preserved — not nulled out."""
    manifest = {
        "files": [
            {"path": "unknown.xyz", "layer": "data"},
        ],
        "volatile_paths": [],
    }
    changes = apply_to_manifest(manifest)
    assert changes == []   # no transition recorded
    assert manifest["files"][0]["layer"] == "data"
    # But the method is still stamped so the trail knows we tried.
    assert manifest["files"][0]["classification_method"] == \
        CLASSIFICATION_METHOD_UNCLASSIFIED


def test_apply_to_manifest_idempotent() -> None:
    """Calling apply_to_manifest twice on the same manifest yields the
    same result. Phase 5.5 (scanner integration) re-runs on every scan
    pass; idempotency means no churn."""
    manifest = {
        "files": [
            {"path": "scripts/main.py", "layer": "script"},
            {"path": "HEARTBEAT.md", "layer": "state"},
        ],
        "volatile_paths": [],
    }
    apply_to_manifest(manifest)
    snapshot = [dict(f) for f in manifest["files"]]
    changes = apply_to_manifest(manifest)
    assert changes == []
    assert manifest["files"] == snapshot


# ── Layer enum invariants ──────────────────────────────────────────────────

def test_every_classifier_output_is_in_valid_layers() -> None:
    """Sanity guard: every layer the classifier ever returns must be in
    VALID_LAYERS. Catches a typo in any of the rule data tables."""
    sample_paths = [
        "scripts/foo.py", "HEARTBEAT.md", "AGENTS.md", "README.md",
        "data/x.json", "logs/audit.log", "manifest.json",
        "content/article.md", "settings.yaml", "events.jsonl",
        "build.lock", "config/options.toml",
    ]
    for path in sample_paths:
        result = classify(path)
        if result.layer is not None:
            assert result.layer in VALID_LAYERS, (
                f"path {path!r} got layer {result.layer!r} not in VALID_LAYERS"
            )
