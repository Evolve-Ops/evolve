"""tests/test_data_classification.py — Phase 3 classification resolver."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import data_classification as dc  # noqa: E402


# ─── _normalise_path ───────────────────────────────────────────────────────

def test_normalise_strips_leading_dot_slash():
    assert dc._normalise_path("./notes/foo.md") == "notes/foo.md"


def test_normalise_converts_backslashes():
    assert dc._normalise_path("notes\\foo.md") == "notes/foo.md"


def test_normalise_rejects_absolute_path():
    assert dc._normalise_path("/Users/Shared/foo") is None


def test_normalise_rejects_dotdot_traversal():
    assert dc._normalise_path("notes/../secrets") is None
    assert dc._normalise_path("../foo") is None


def test_normalise_rejects_empty():
    assert dc._normalise_path("") is None
    assert dc._normalise_path("   ") is None


# ─── ClassificationRule.matches ─────────────────────────────────────────────

def test_rule_matches_trailing_slash_prefix():
    r = dc.ClassificationRule(prefix="notes/", privacy="local", source="t")
    assert r.matches("notes/foo.md")
    assert r.matches("notes/sub/bar.md")
    assert r.matches("notes")  # exact name match (without slash) is allowed


def test_rule_does_not_match_sibling_prefix():
    """`notes/` must NOT match `notes2/...` — boundary safety."""
    r = dc.ClassificationRule(prefix="notes/", privacy="local", source="t")
    assert not r.matches("notes2/foo.md")
    assert not r.matches("notesfoo.md")


def test_rule_empty_prefix_is_universal():
    r = dc.ClassificationRule(prefix="", privacy="cloud", source="t")
    assert r.matches("anything/at/all.md")
    assert r.matches("foo")


# ─── classify with built-ins ────────────────────────────────────────────────

def test_classify_evolve_backup_files_are_cloud_eligible_by_default():
    """Regression for the review-session bug:

    The original built-in rule ``evolve-backup/ → ephemeral`` framed the
    directory as "recursion staging," but ``backup.py`` actually writes
    the redacted ``openclaw.json``, metrics, and run-state into that
    directory as the *cloud backup payload*. The built-in was stripping
    those files before push and the Phase 4a audit was false-firing on
    them. The rule is gone — these paths now classify cloud like
    anything else, which is correct.
    """
    resolver = dc.build_resolver()
    assert resolver.classify("evolve-backup/state.json") == "cloud"
    assert resolver.classify("evolve-backup/openclaw.json") == "cloud"
    assert resolver.classify("evolve-backup/metrics/latest.json") == "cloud"


def test_classify_unmatched_falls_back_to_default():
    resolver = dc.build_resolver()
    assert resolver.classify("notes/foo.md") == "cloud"  # fallback_default


def test_classify_custom_fallback_default():
    resolver = dc.build_resolver(fallback_default="local")
    assert resolver.classify("scratch.md") == "local"
    # No remaining built-ins — evolve-backup now follows the fallback too.
    assert resolver.classify("evolve-backup/state.json") == "local"


def test_classify_evolve_backup_honors_operator_override():
    """Operators can still declare evolve-backup as local/ephemeral if they want."""
    network = {"backup": {"data_paths": [
        {"path": "evolve-backup/", "privacy": "local"},
    ]}}
    resolver = dc.build_resolver(network=network)
    assert resolver.classify("evolve-backup/state.json") == "local"


# ─── classify with pod-wide rules ───────────────────────────────────────────

def test_pod_wide_data_path_classifies_directory():
    network = {"backup": {"data_paths": [
        {"path": "transcripts/", "privacy": "local"},
    ]}}
    resolver = dc.build_resolver(network=network)
    assert resolver.classify("transcripts/2026-05-28.md") == "local"
    assert resolver.classify("transcripts/") == "local"
    # Unrelated paths still default to cloud.
    assert resolver.classify("docs/readme.md") == "cloud"


def test_pod_default_for_unclassified_applies():
    network = {"backup": {
        "data_paths": [{"path": "index/", "privacy": "cloud"}],
        "default_for_unclassified": "local",
    }}
    resolver = dc.build_resolver(network=network)
    assert resolver.classify("notes/foo.md") == "local"  # default
    assert resolver.classify("index/abc.json") == "cloud"  # explicit


def test_pod_invalid_default_ignored():
    """A malformed default doesn't crash; falls back to caller's fallback_default."""
    network = {"backup": {"default_for_unclassified": "nonsense"}}
    resolver = dc.build_resolver(network=network, fallback_default="cloud")
    assert resolver.classify("notes/foo.md") == "cloud"


def test_pod_invalid_data_path_entry_skipped():
    """Bad entries shouldn't taint the rule set."""
    network = {"backup": {"data_paths": [
        {"path": "notes/", "privacy": "local"},
        {"privacy": "cloud"},                       # missing path
        {"path": "logs/"},                          # missing privacy
        "not-even-a-dict",                          # not a dict
        {"path": "/absolute/bad", "privacy": "local"},
        {"path": "foo/", "privacy": "nope"},        # invalid privacy
    ]}}
    resolver = dc.build_resolver(network=network)
    assert resolver.classify("notes/foo.md") == "local"
    # All the broken entries dropped silently.
    assert len([r for r in resolver.rules if r.source.startswith("pod:")]) == 1


# ─── classify with per-app rules ────────────────────────────────────────────

def test_app_data_paths_override_default():
    manifest = {
        "id": "notes-app",
        "data_paths": [
            {"path": "notes/", "privacy": "local"},
            {"path": "cache/", "privacy": "ephemeral"},
            {"path": "index/", "privacy": "cloud"},
        ],
    }
    resolver = dc.build_resolver(manifests=[manifest])
    assert resolver.classify("notes/2026-05-28.md") == "local"
    assert resolver.classify("cache/embeddings.bin") == "ephemeral"
    assert resolver.classify("index/abc.json") == "cloud"
    assert resolver.classify("uncovered/file.md") == "cloud"  # default


def test_app_files_privacy_applies_to_files_list():
    manifest = {
        "id": "diary",
        "app_files_privacy": "local",
        "files": ["diary/diary.py", "diary/AGENTS.md"],
    }
    resolver = dc.build_resolver(manifests=[manifest])
    assert resolver.classify("diary/diary.py") == "local"
    assert resolver.classify("diary/AGENTS.md") == "local"
    # An undeclared file in the same dir doesn't inherit — it depends on
    # whether the directory itself was declared in data_paths.
    assert resolver.classify("diary/new-file.md") == "cloud"


def test_app_files_v5_dict_format_supported():
    manifest = {
        "id": "diary",
        "app_files_privacy": "cloud",
        "files": [
            {"path": "diary/diary.py", "file_id": "abc"},
            {"path": "diary/AGENTS.md", "file_id": "def"},
        ],
    }
    resolver = dc.build_resolver(manifests=[manifest])
    assert resolver.classify("diary/diary.py") == "cloud"


def test_app_files_privacy_empty_string_means_no_rule():
    """Empty string is the 'not declared' marker — no rule contributed."""
    manifest = {
        "id": "diary",
        "app_files_privacy": "",
        "files": ["diary/diary.py"],
    }
    resolver = dc.build_resolver(manifests=[manifest])
    # Should fall through to default (cloud), no rule from this manifest.
    assert resolver.classify("diary/diary.py") == "cloud"
    assert not any(r.source.startswith("app:diary:app_files_privacy") for r in resolver.rules)


def test_v14_manifest_with_no_classification_contributes_nothing():
    """Backwards compat: pre-v15 manifests contribute no rules."""
    manifest = {
        "id": "legacy",
        "name": "Legacy",
        "files": ["legacy/main.py"],
        # No app_files_privacy, no data_paths, no default_for_unclassified
    }
    resolver = dc.build_resolver(manifests=[manifest])
    # No rules from this manifest, only built-ins.
    assert all(
        not r.source.startswith("app:legacy")
        for r in resolver.rules
    )


# ─── Longest-prefix precedence ──────────────────────────────────────────────

def test_longest_prefix_wins():
    manifest = {
        "id": "x",
        "data_paths": [
            {"path": "notes/", "privacy": "local"},
            {"path": "notes/public/", "privacy": "cloud"},
        ],
    }
    resolver = dc.build_resolver(manifests=[manifest])
    assert resolver.classify("notes/private.md") == "local"
    assert resolver.classify("notes/public/post.md") == "cloud"


def test_app_rule_overrides_pod_rule_on_equal_or_longer_prefix():
    network = {"backup": {"data_paths": [
        {"path": "notes/", "privacy": "local"},
    ]}}
    manifest = {
        "id": "notes-app",
        "data_paths": [
            {"path": "notes/", "privacy": "cloud"},  # overrides pod-wide for equal-length
        ],
    }
    resolver = dc.build_resolver(manifests=[manifest], network=network)
    assert resolver.classify("notes/foo.md") == "cloud"


# ─── explain() ──────────────────────────────────────────────────────────────

def test_explain_reports_matching_source():
    manifest = {
        "id": "diary",
        "data_paths": [{"path": "diary/private/", "privacy": "local"}],
    }
    resolver = dc.build_resolver(manifests=[manifest])
    privacy, source = resolver.explain("diary/private/2026.md")
    assert privacy == "local"
    assert source == "app:diary:data_paths"


def test_explain_reports_default_fallthrough():
    resolver = dc.build_resolver()
    privacy, source = resolver.explain("uncovered.md")
    assert privacy == "cloud"
    assert source == "fallthrough:default"


def test_explain_evolve_backup_falls_through_to_default():
    """Replaces the old test for the now-removed recursion-guard built-in.

    See test_classify_evolve_backup_files_are_cloud_eligible_by_default for
    the rationale.
    """
    resolver = dc.build_resolver()
    privacy, source = resolver.explain("evolve-backup/state.json")
    assert privacy == "cloud"
    assert source == "fallthrough:default"


# ─── load_bot_manifests ─────────────────────────────────────────────────────

def test_load_bot_manifests_reads_valid_json(tmp_path):
    workspace = tmp_path / "workspace"
    mdir = workspace / "manifests"
    mdir.mkdir(parents=True)
    (mdir / "notes.json").write_text(json.dumps({"id": "notes", "name": "Notes"}))
    (mdir / "diary.json").write_text(json.dumps({"id": "diary", "name": "Diary"}))
    out = dc.load_bot_manifests(workspace)
    assert sorted(m["id"] for m in out) == ["diary", "notes"]


def test_load_bot_manifests_skips_dotfiles_and_history(tmp_path):
    """Filter matches Apps tab's _glob_manifests semantics: skip dotfiles
    + files containing ``_history``. Other underscore-prefixed files
    (rare) come through."""
    workspace = tmp_path / "workspace"
    mdir = workspace / "manifests"
    mdir.mkdir(parents=True)
    (mdir / "notes.json").write_text(json.dumps({"id": "notes"}))
    (mdir / ".scan-status.json").write_text(json.dumps({"some": "state"}))
    (mdir / "_history.json").write_text(json.dumps({"id": "history"}))
    (mdir / "diary_history_archive.json").write_text(json.dumps({"id": "h2"}))
    out = dc.load_bot_manifests(workspace)
    assert sorted(m["id"] for m in out) == ["notes"]


def test_load_bot_manifests_includes_manifests_without_id(tmp_path):
    """Regression for the 2026-05-29 Data tab discovery bug.

    v7-arc Instance manifests carry their identity via the bound Spec,
    not at the top level. Pre-id-required legacy manifests don't have an
    ``id`` either. Both must come through with the id derived from the
    filename stem so the classification fields downstream code reads
    aren't silently lost.
    """
    workspace = tmp_path / "workspace"
    mdir = workspace / "manifests"
    mdir.mkdir(parents=True)
    (mdir / "good.json").write_text(json.dumps({"id": "good"}))
    (mdir / "v7-instance.json").write_text(json.dumps({
        "manifest_shape": "v7-arc",
        "data_paths": [{"path": "notes/", "privacy": "local"}],
        # No id; no name; v7-arc Instance shape.
    }))
    (mdir / "legacy.json").write_text(json.dumps({"name": "Legacy"}))
    out = dc.load_bot_manifests(workspace)
    ids = sorted(m["id"] for m in out)
    assert ids == ["good", "legacy", "v7-instance"]
    # The classification fields on the v7-arc instance survived.
    v7 = next(m for m in out if m["id"] == "v7-instance")
    assert v7["data_paths"][0]["path"] == "notes/"


def test_load_bot_manifests_skips_broken_json(tmp_path):
    workspace = tmp_path / "workspace"
    mdir = workspace / "manifests"
    mdir.mkdir(parents=True)
    (mdir / "good.json").write_text(json.dumps({"id": "good"}))
    (mdir / "broken.json").write_text("{ not valid")
    # Non-dict JSON (e.g. an array) is also skipped — not a manifest.
    (mdir / "list.json").write_text(json.dumps(["not", "a", "manifest"]))
    out = dc.load_bot_manifests(workspace)
    assert [m["id"] for m in out] == ["good"]


def test_load_bot_manifests_empty_when_no_dir(tmp_path):
    assert dc.load_bot_manifests(tmp_path / "nonexistent") == []


# ─── Integration: pre-v15 pod backs up everything ──────────────────────────

def test_pre_v15_pod_classifies_everything_as_cloud():
    """The architectural backwards-compat guarantee.

    With no v15 declarations anywhere, every workspace file classifies
    as cloud (including ``evolve-backup/*`` — that directory contains
    the backup payload that MUST go to cloud; see the
    ``_BUILTIN_RULES`` retraction note in data_classification.py).
    """
    # An empty network + a v14-shape manifest with no classification.
    legacy_manifest = {
        "id": "old",
        "name": "Old App",
        "files": ["old/old.py", "old/AGENTS.md"],
        "schema_version": 14,
    }
    resolver = dc.build_resolver(manifests=[legacy_manifest], network={})
    assert resolver.classify("old/old.py") == "cloud"
    assert resolver.classify("old/AGENTS.md") == "cloud"
    assert resolver.classify("notes/random.md") == "cloud"
    assert resolver.classify("SOUL.md") == "cloud"
    # No built-ins remain — evolve-backup follows the default.
    assert resolver.classify("evolve-backup/state.json") == "cloud"


# ─── Path edge cases ───────────────────────────────────────────────────────

def test_classify_absolute_path_returns_default():
    """Absolute paths can't be classified — fall through to default."""
    resolver = dc.build_resolver(fallback_default="local")
    assert resolver.classify("/Users/Shared/evolve/file") == "local"


def test_classify_dotdot_path_returns_default():
    resolver = dc.build_resolver(fallback_default="local")
    assert resolver.classify("notes/../escape") == "local"


def test_classify_handles_windows_separators():
    manifest = {
        "id": "x",
        "data_paths": [{"path": "notes/", "privacy": "local"}],
    }
    resolver = dc.build_resolver(manifests=[manifest])
    assert resolver.classify("notes\\foo.md") == "local"


# ─── Tier inference ─────────────────────────────────────────────────────────

def test_infer_tier_unclassified_when_nothing_set():
    assert dc.infer_tier({"id": "x"}) == "unclassified"


def test_infer_tier_unclassified_treats_empty_string_as_unset():
    assert dc.infer_tier({
        "id": "x",
        "app_files_privacy": "",
        "default_for_unclassified": "",
        "data_paths": [],
    }) == "unclassified"


def test_infer_tier_whole_app_local():
    assert dc.infer_tier({
        "app_files_privacy": "local",
        "default_for_unclassified": "local",
        "data_paths": [
            {"path": "notes/", "privacy": "local"},
            {"path": "cache/", "privacy": "local"},
        ],
    }) == "whole_app_local"


def test_infer_tier_whole_app_local_with_no_data_paths():
    assert dc.infer_tier({
        "app_files_privacy": "local",
        "default_for_unclassified": "local",
        "data_paths": [],
    }) == "whole_app_local"


def test_infer_tier_all_data_local():
    assert dc.infer_tier({
        "app_files_privacy": "cloud",
        "default_for_unclassified": "local",
        "data_paths": [{"path": "notes/", "privacy": "local"}],
    }) == "all_data_local"


def test_infer_tier_full_cloud():
    assert dc.infer_tier({
        "app_files_privacy": "cloud",
        "default_for_unclassified": "cloud",
        "data_paths": [{"path": "logs/", "privacy": "cloud"}],
    }) == "full_cloud"


def test_infer_tier_some_data_local_when_mixed_privacies():
    """Mixed cloud + local data_paths → operator-authored → some_data_local."""
    assert dc.infer_tier({
        "app_files_privacy": "cloud",
        "default_for_unclassified": "local",
        "data_paths": [
            {"path": "notes/", "privacy": "local"},
            {"path": "index/", "privacy": "cloud"},
        ],
    }) == "some_data_local"


def test_infer_tier_some_data_local_when_ephemeral_present():
    """ephemeral is a deliberate operator choice → some_data_local."""
    assert dc.infer_tier({
        "app_files_privacy": "cloud",
        "default_for_unclassified": "cloud",
        "data_paths": [{"path": "cache/", "privacy": "ephemeral"}],
    }) == "some_data_local"


def test_infer_tier_some_data_local_when_default_mismatches_paths():
    """default=cloud but a data_path is local → mixed intent."""
    assert dc.infer_tier({
        "app_files_privacy": "cloud",
        "default_for_unclassified": "cloud",
        "data_paths": [{"path": "notes/", "privacy": "local"}],
    }) == "some_data_local"


# ─── Tier application ───────────────────────────────────────────────────────

def test_apply_tier_whole_app_local_rewrites_all_paths():
    m = {
        "app_files_privacy": "cloud",
        "default_for_unclassified": "cloud",
        "data_paths": [
            {"path": "notes/", "privacy": "cloud"},
            {"path": "index/", "privacy": "cloud", "note": "search index"},
        ],
    }
    out = dc.apply_tier_to_manifest(m, "whole_app_local")
    assert out["app_files_privacy"] == "local"
    assert out["default_for_unclassified"] == "local"
    assert all(p["privacy"] == "local" for p in out["data_paths"])
    # Preserved fields like ``note`` survive the rewrite.
    assert out["data_paths"][1]["note"] == "search index"


def test_apply_tier_all_data_local_keeps_app_files_cloud():
    m = {
        "app_files_privacy": "",
        "default_for_unclassified": "",
        "data_paths": [{"path": "notes/", "privacy": "cloud"}],
    }
    out = dc.apply_tier_to_manifest(m, "all_data_local")
    assert out["app_files_privacy"] == "cloud"
    assert out["default_for_unclassified"] == "local"
    assert out["data_paths"][0]["privacy"] == "local"


def test_apply_tier_full_cloud_clears_local_paths():
    m = {
        "app_files_privacy": "local",
        "default_for_unclassified": "local",
        "data_paths": [{"path": "notes/", "privacy": "local"}],
    }
    out = dc.apply_tier_to_manifest(m, "full_cloud")
    assert out["app_files_privacy"] == "cloud"
    assert out["default_for_unclassified"] == "cloud"
    assert out["data_paths"][0]["privacy"] == "cloud"


def test_apply_tier_some_data_local_is_noop():
    m = {
        "app_files_privacy": "cloud",
        "default_for_unclassified": "local",
        "data_paths": [{"path": "notes/", "privacy": "local"}],
    }
    out = dc.apply_tier_to_manifest(m, "some_data_local")
    assert out["app_files_privacy"] == "cloud"
    assert out["default_for_unclassified"] == "local"
    assert out["data_paths"] == [{"path": "notes/", "privacy": "local"}]


def test_apply_tier_does_not_mutate_input():
    m = {
        "app_files_privacy": "cloud",
        "data_paths": [{"path": "notes/", "privacy": "cloud"}],
    }
    dc.apply_tier_to_manifest(m, "whole_app_local")
    # original untouched
    assert m["app_files_privacy"] == "cloud"
    assert m["data_paths"][0]["privacy"] == "cloud"


def test_apply_tier_round_trips_through_infer():
    """Applying a tier, then inferring the tier from the result, returns the same tier."""
    base = {
        "id": "x",
        "app_files_privacy": "cloud",
        "default_for_unclassified": "cloud",
        "data_paths": [
            {"path": "a/", "privacy": "cloud"},
            {"path": "b/", "privacy": "cloud"},
        ],
    }
    for tier in (
        dc.TIER_WHOLE_APP_LOCAL,
        dc.TIER_ALL_DATA_LOCAL,
        dc.TIER_FULL_CLOUD,
    ):
        out = dc.apply_tier_to_manifest(base, tier)
        assert dc.infer_tier(out) == tier, f"round-trip failed for {tier}"


def test_apply_tier_raises_on_unknown():
    with pytest.raises(ValueError):
        dc.apply_tier_to_manifest({}, "made_up_tier")


# ─── stamp_per_bot_default ─────────────────────────────────────────────────


def test_stamp_no_op_when_bot_has_no_default():
    """No default in network.json → manifest unchanged."""
    m = {"id": "x"}
    out = dc.stamp_per_bot_default(
        m, bot_id="team_bot_a", network={"bots": {"team_bot_a": {}}},
    )
    assert out is m  # untouched, same object


def test_stamp_no_op_when_bot_missing_from_network():
    m = {"id": "x"}
    out = dc.stamp_per_bot_default(
        m, bot_id="ghost", network={"bots": {"team_bot_a": {"backup_default_tier": "full_cloud"}}},
    )
    assert out is m


def test_stamp_no_op_when_default_is_some_data_local():
    """``some_data_local`` template is a no-op anyway — skip."""
    m = {"id": "x"}
    out = dc.stamp_per_bot_default(
        m, bot_id="team_bot_a",
        network={"bots": {"team_bot_a": {"backup_default_tier": "some_data_local"}}},
    )
    assert out is m


def test_stamp_no_op_when_manifest_already_classified():
    """Don't clobber operator-authored classification on a rescan."""
    m = {
        "id": "x",
        "data_paths": [{"path": "notes/", "privacy": "local"}],
    }
    out = dc.stamp_per_bot_default(
        m, bot_id="team_bot_a",
        network={"bots": {"team_bot_a": {"backup_default_tier": "full_cloud"}}},
    )
    assert out["data_paths"][0]["privacy"] == "local"  # unchanged


def test_stamp_no_op_on_unknown_tier_value():
    """Defensive: misspelt tier in network.json doesn't stamp anything."""
    m = {"id": "x"}
    out = dc.stamp_per_bot_default(
        m, bot_id="team_bot_a",
        network={"bots": {"team_bot_a": {"backup_default_tier": "huge_local_typo"}}},
    )
    assert out is m


def test_stamp_applies_whole_app_local_to_fresh_manifest():
    m = {"id": "x", "files": ["x/main.py"]}
    out = dc.stamp_per_bot_default(
        m, bot_id="team_bot_a",
        network={"bots": {"team_bot_a": {"backup_default_tier": "whole_app_local"}}},
    )
    assert out is not m  # returned a copy
    assert out["app_files_privacy"] == "local"
    assert out["default_for_unclassified"] == "local"


def test_stamp_applies_all_data_local_to_fresh_manifest():
    m = {"id": "x"}
    out = dc.stamp_per_bot_default(
        m, bot_id="team_bot_a",
        network={"bots": {"team_bot_a": {"backup_default_tier": "all_data_local"}}},
    )
    assert out["app_files_privacy"] == "cloud"
    assert out["default_for_unclassified"] == "local"


def test_stamp_applies_full_cloud_to_fresh_manifest():
    m = {"id": "x"}
    out = dc.stamp_per_bot_default(
        m, bot_id="team_bot_a",
        network={"bots": {"team_bot_a": {"backup_default_tier": "full_cloud"}}},
    )
    assert out["app_files_privacy"] == "cloud"
    assert out["default_for_unclassified"] == "cloud"


def test_stamp_robust_to_malformed_network_config():
    """Network without a ``bots`` dict, or with non-dict bot entry → no-op."""
    m = {"id": "x"}
    assert dc.stamp_per_bot_default(m, bot_id="team_bot_a", network={}) is m
    assert dc.stamp_per_bot_default(m, bot_id="team_bot_a", network={"bots": "not-a-dict"}) is m
    assert dc.stamp_per_bot_default(m, bot_id="team_bot_a", network={"bots": {"team_bot_a": "scalar"}}) is m
    assert dc.stamp_per_bot_default(m, bot_id="team_bot_a", network=None) is m
