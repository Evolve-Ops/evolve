"""tests/test_backup_audit_signal.py — Phase 4a post-push audit."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import backup_audit_signal as bas  # noqa: E402
from signals import store as signals_store  # noqa: E402


def _cfg(*, bot: str = "team_bot_a", url: str = "git@github.com:cjalden/test.git", manifests=None):
    return {
        "bots": {bot: {"backupRepoUrl": url}},
        "_test_manifests": manifests or [],  # smuggle test manifests via config
    }


def _make_tree_lister(paths_per_bot: dict[str, list[str]]):
    """Stub for _default_list_tree — returns paths keyed by the workspace path."""
    def _lister(workspace):
        for bot_id, paths in paths_per_bot.items():
            if bot_id in str(workspace):
                return list(paths), None
        return [], None
    return _lister


def _make_workspace_resolver(tmp_path):
    """Each bot's 'workspace' is a per-bot tmp subdir we can create on demand."""
    def _resolve(bot_id):
        return tmp_path / bot_id / ".openclaw" / "workspace"
    return _resolve


# ─── audit_bot — single-bot inspection ───────────────────────────────────────


def test_audit_bot_no_workspace_returns_no_leaks_no_error(tmp_path):
    cfg = _cfg(bot="team_bot_a")
    leaks, err = bas.audit_bot(
        "team_bot_a", cfg,
        workspace_resolver=_make_workspace_resolver(tmp_path),
    )
    assert leaks == []
    assert err is None


def test_audit_bot_clean_tree_returns_no_leaks(tmp_path):
    cfg = _cfg(bot="team_bot_a")
    workspace = tmp_path / "team_bot_a" / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    leaks, err = bas.audit_bot(
        "team_bot_a", cfg,
        tree_lister=_make_tree_lister({"team_bot_a": ["SOUL.md", "notes/foo.md"]}),
        manifest_loader=lambda ws: [],
        workspace_resolver=_make_workspace_resolver(tmp_path),
    )
    assert err is None
    assert leaks == []  # everything defaults to cloud


def test_audit_bot_does_not_false_positive_on_evolve_backup_payload(tmp_path):
    """Regression for the 2026-05-29 review-session bug.

    With no operator-declared rules, ``evolve-backup/*`` paths (the cloud
    backup's own payload) must classify cloud and NOT show up as leaks.
    The original code had a built-in ``evolve-backup/ → ephemeral`` rule
    that caused this monitor to false-positive-alert on every healthy
    pod the moment Phase 4a landed. The rule is gone — no leaks here.
    """
    cfg = _cfg(bot="team_bot_a")
    workspace = tmp_path / "team_bot_a" / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    leaks, err = bas.audit_bot(
        "team_bot_a", cfg,
        tree_lister=_make_tree_lister({"team_bot_a": [
            "SOUL.md",
            "evolve-backup/state.json",
            "evolve-backup/openclaw.json",
            "evolve-backup/metrics/latest.json",
        ]}),
        manifest_loader=lambda ws: [],
        workspace_resolver=_make_workspace_resolver(tmp_path),
    )
    assert err is None
    assert leaks == []


def test_audit_bot_flags_local_paths_per_manifest(tmp_path):
    cfg = _cfg(bot="team_bot_a")
    workspace = tmp_path / "team_bot_a" / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    notes_manifest = {
        "id": "notes",
        "data_paths": [{"path": "notes/", "privacy": "local"}],
    }
    leaks, err = bas.audit_bot(
        "team_bot_a", cfg,
        tree_lister=_make_tree_lister({"team_bot_a": [
            "SOUL.md",
            "notes/2026.md",
            "notes/private/diary.md",
            "index/abc.json",
        ]}),
        manifest_loader=lambda ws: [notes_manifest],
        workspace_resolver=_make_workspace_resolver(tmp_path),
    )
    assert err is None
    assert {l["path"] for l in leaks} == {"notes/2026.md", "notes/private/diary.md"}
    assert all(l["classification"] == "local" for l in leaks)


def test_audit_bot_propagates_git_error(tmp_path):
    cfg = _cfg(bot="team_bot_a")
    workspace = tmp_path / "team_bot_a" / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    def angry_lister(ws):
        return [], "git ls-tree rc=128: fatal: bad object HEAD"
    leaks, err = bas.audit_bot(
        "team_bot_a", cfg,
        tree_lister=angry_lister,
        manifest_loader=lambda ws: [],
        workspace_resolver=_make_workspace_resolver(tmp_path),
    )
    assert leaks == []
    assert "git ls-tree rc=128" in (err or "")


def test_audit_bot_includes_source_in_leak_entries(tmp_path):
    """Each leak entry records which rule classified it — useful for diagnostics."""
    cfg = _cfg(bot="team_bot_a")
    workspace = tmp_path / "team_bot_a" / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    notes_app = {
        "id": "notes-app",
        "data_paths": [{"path": "notes/", "privacy": "local"}],
    }
    leaks, _ = bas.audit_bot(
        "team_bot_a", cfg,
        tree_lister=_make_tree_lister({"team_bot_a": ["notes/leaked.md"]}),
        manifest_loader=lambda ws: [notes_app],
        workspace_resolver=_make_workspace_resolver(tmp_path),
    )
    assert leaks[0]["source"].startswith("app:notes-app:")


# ─── build_signal_for_leak — Signal shape ────────────────────────────────────


def test_signal_severity_and_scope():
    spec = bas.build_signal_for_leak("team_bot_a", [
        {"path": "notes/a.md", "classification": "local", "source": "x"},
    ])
    assert spec["severity"] == "alert"
    assert spec["scope"] == "bot"
    assert spec["bot_id"] == "team_bot_a"
    assert spec["type"] == "backup_classification_leak"


def test_signal_signature_stable_per_bot():
    s1 = bas.build_signal_for_leak("team_bot_a", [{"path": "a", "classification": "local", "source": "x"}])
    s2 = bas.build_signal_for_leak("team_bot_a", [{"path": "b", "classification": "local", "source": "x"}])
    # Same bot → same signature so re-observation merges.
    assert s1["signature"] == s2["signature"]
    # Different bot → different signature.
    s3 = bas.build_signal_for_leak("admin_bot", [{"path": "a", "classification": "local", "source": "x"}])
    assert s1["signature"] != s3["signature"]


def test_signal_body_groups_paths_by_classification():
    spec = bas.build_signal_for_leak("team_bot_a", [
        {"path": "notes/a.md",  "classification": "local",     "source": "x"},
        {"path": "notes/b.md",  "classification": "local",     "source": "x"},
        {"path": "cache/c.bin", "classification": "ephemeral", "source": "y"},
    ])
    assert "local" in spec["body"]
    assert "ephemeral" in spec["body"]
    assert "notes/a.md" in spec["body"]
    assert "cache/c.bin" in spec["body"]


def test_signal_caps_paths_in_body():
    """Body shouldn't dump 500 paths — operator gets a sample + a count."""
    leaks = [
        {"path": f"notes/{i}.md", "classification": "local", "source": "x"}
        for i in range(80)
    ]
    spec = bas.build_signal_for_leak("team_bot_a", leaks)
    # Body has at most _BODY_PATH_CAP path listings.
    listed = spec["body"].count("notes/")
    assert listed <= bas._BODY_PATH_CAP
    # Details still has the full list.
    assert len(spec["details"]["leaks"]) == 80
    # "… more not shown" footer present.
    assert "not shown" in spec["body"]


def test_signal_body_cap_message_accurate_when_split_across_classifications():
    """Regression for the 2026-05-29 review polish.

    When the body cap is consumed across both ``local`` and ``ephemeral``
    classifications, the "N more not shown" message must reflect the
    paths actually suppressed in EACH classification's loop iteration —
    not ``len(paths) - _BODY_PATH_CAP`` globally.
    """
    cap = bas._BODY_PATH_CAP
    # 20 local + 10 ephemeral, cap=25 → expect 20 local shown (none
    # hidden), then 5 ephemeral shown + "5 more not shown" for ephemeral.
    leaks = (
        [{"path": f"notes/{i}.md", "classification": "local", "source": "x"} for i in range(20)] +
        [{"path": f"cache/{i}.bin", "classification": "ephemeral", "source": "y"} for i in range(10)]
    )
    spec = bas.build_signal_for_leak("team_bot_a", leaks)
    body = spec["body"]
    # All 20 local shown (under cap).
    for i in range(20):
        assert f"notes/{i}.md" in body
    # First 5 ephemeral shown; remaining 5 not shown.
    for i in range(5):
        assert f"cache/{i}.bin" in body
    for i in range(5, 10):
        assert f"cache/{i}.bin" not in body
    # Elision message says "5 more", not "5 more" (matches actual suppression).
    assert "5 more not shown" in body


def test_signal_details_count_by_classification():
    spec = bas.build_signal_for_leak("team_bot_a", [
        {"path": "a", "classification": "local",     "source": "x"},
        {"path": "b", "classification": "local",     "source": "x"},
        {"path": "c", "classification": "ephemeral", "source": "y"},
    ])
    counts = spec["details"]["by_classification"]
    assert counts["local"] == 2
    assert counts["ephemeral"] == 1


def test_signal_body_includes_remediation_steps():
    spec = bas.build_signal_for_leak("team_bot_a", [
        {"path": "notes/a", "classification": "local", "source": "x"},
    ])
    assert "Decide if the classification is wrong" in spec["body"]
    assert "Rotate" in spec["body"]


# ─── run() — end-to-end with Signal store ────────────────────────────────────


_LEAK_MANIFEST = {
    "id": "notes-app",
    "data_paths": [{"path": "notes/", "privacy": "local"}],
}


def test_run_skips_bots_without_backup_url(tmp_path):
    cfg = {"bots": {"team_bot_a": {}}}  # no backupRepoUrl
    kept, n_fired, _ = bas.run(
        tmp_path, cfg, bots=["team_bot_a"],
        tree_lister=_make_tree_lister({"team_bot_a": ["notes/leaked.md"]}),
        manifest_loader=lambda ws: [_LEAK_MANIFEST],
        workspace_resolver=_make_workspace_resolver(tmp_path),
    )
    assert n_fired == 0
    assert kept == set()


def test_run_fires_signal_when_leak_present(tmp_path):
    cfg = _cfg(bot="team_bot_a")
    workspace = tmp_path / "team_bot_a" / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    kept, n_fired, _ = bas.run(
        tmp_path, cfg, bots=["team_bot_a"],
        tree_lister=_make_tree_lister({"team_bot_a": [
            "SOUL.md", "notes/leaked.md",
        ]}),
        manifest_loader=lambda ws: [_LEAK_MANIFEST],
        workspace_resolver=_make_workspace_resolver(tmp_path),
    )
    assert n_fired == 1
    sigs = list(signals_store.iter_active(tmp_path, producer="backup_audit_signal"))
    assert len(sigs) == 1
    assert sigs[0].type == "backup_classification_leak"
    assert sigs[0].severity == "alert"


def test_run_sweep_resolves_when_leak_cleared(tmp_path):
    cfg = _cfg(bot="team_bot_a")
    workspace = tmp_path / "team_bot_a" / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)

    # Pass 1: leak present.
    bas.run(
        tmp_path, cfg, bots=["team_bot_a"],
        tree_lister=_make_tree_lister({"team_bot_a": ["notes/leaked.md"]}),
        manifest_loader=lambda ws: [_LEAK_MANIFEST],
        workspace_resolver=_make_workspace_resolver(tmp_path),
    )
    assert len(list(signals_store.iter_active(tmp_path, producer="backup_audit_signal"))) == 1

    # Pass 2: operator scrubbed the file.
    kept, n_fired, n_resolved = bas.run(
        tmp_path, cfg, bots=["team_bot_a"],
        tree_lister=_make_tree_lister({"team_bot_a": ["SOUL.md"]}),
        manifest_loader=lambda ws: [],
        workspace_resolver=_make_workspace_resolver(tmp_path),
    )
    assert kept == set()
    assert n_fired == 0
    assert n_resolved == 1
    assert list(signals_store.iter_active(tmp_path, producer="backup_audit_signal")) == []


def test_run_dry_run_does_not_write(tmp_path):
    cfg = _cfg(bot="team_bot_a")
    workspace = tmp_path / "team_bot_a" / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    _, n_fired, _ = bas.run(
        tmp_path, cfg, bots=["team_bot_a"], dry_run=True,
        tree_lister=_make_tree_lister({"team_bot_a": ["notes/leaked.md"]}),
        manifest_loader=lambda ws: [_LEAK_MANIFEST],
        workspace_resolver=_make_workspace_resolver(tmp_path),
    )
    assert n_fired == 1
    assert list(signals_store.iter_active(tmp_path, producer="backup_audit_signal")) == []


def test_run_per_bot_audit_failure_does_not_blank_other_bots(tmp_path):
    cfg = {
        "bots": {
            "team_bot_a":   {"backupRepoUrl": "git@github.com:x/team_bot_a.git"},
            "admin_bot": {"backupRepoUrl": "git@github.com:x/admin_bot.git"},
        },
    }
    (tmp_path / "team_bot_a" / ".openclaw" / "workspace").mkdir(parents=True)
    (tmp_path / "admin_bot" / ".openclaw" / "workspace").mkdir(parents=True)

    def lister(ws):
        if "team_bot_a" in str(ws):
            return [], "git ls-tree rc=128: synthetic failure"
        if "admin_bot" in str(ws):
            return ["notes/leaked.md"], None
        return [], None

    kept, n_fired, _ = bas.run(
        tmp_path, cfg, bots=["team_bot_a", "admin_bot"],
        tree_lister=lister,
        manifest_loader=lambda ws: [_LEAK_MANIFEST],
        workspace_resolver=_make_workspace_resolver(tmp_path),
    )
    # admin_bot's audit still fired despite team_bot_a's failure.
    assert n_fired == 1
    sigs = list(signals_store.iter_active(tmp_path, producer="backup_audit_signal"))
    assert len(sigs) == 1
    assert sigs[0].bot_id == "admin_bot"


def test_run_multiple_bots_with_leaks_each_get_own_signal(tmp_path):
    cfg = {
        "bots": {
            "team_bot_a":   {"backupRepoUrl": "git@github.com:x/team_bot_a.git"},
            "admin_bot": {"backupRepoUrl": "git@github.com:x/admin_bot.git"},
        },
    }
    (tmp_path / "team_bot_a" / ".openclaw" / "workspace").mkdir(parents=True)
    (tmp_path / "admin_bot" / ".openclaw" / "workspace").mkdir(parents=True)
    paths = {
        "team_bot_a":   ["notes/k.md"],
        "admin_bot": ["notes/s.md"],
    }
    _, n_fired, _ = bas.run(
        tmp_path, cfg, bots=["team_bot_a", "admin_bot"],
        tree_lister=_make_tree_lister(paths),
        manifest_loader=lambda ws: [_LEAK_MANIFEST],
        workspace_resolver=_make_workspace_resolver(tmp_path),
    )
    assert n_fired == 2
    sigs = list(signals_store.iter_active(tmp_path, producer="backup_audit_signal"))
    by_bot = sorted(s.bot_id for s in sigs)
    assert by_bot == ["admin_bot", "team_bot_a"]


# ─── Regressions from the 2026-05-29 review session ──────────────────────────


def test_run_with_narrow_bots_does_not_clobber_other_bots_signals(tmp_path):
    """``--bot team_bot_a`` run must not auto-resolve admin_bot's still-firing leak Signal.

    Before this fix, sweep_resolve walked every Signal under producer
    ``backup_audit_signal`` and resolved any not in ``kept``. ``kept`` only
    held team_bot_a's signatures, so admin_bot's real, firing leak got mass-resolved.
    """
    cfg = {
        "bots": {
            "team_bot_a":   {"backupRepoUrl": "git@github.com:x/team_bot_a.git"},
            "admin_bot": {"backupRepoUrl": "git@github.com:x/admin_bot.git"},
        },
    }
    (tmp_path / "team_bot_a" / ".openclaw" / "workspace").mkdir(parents=True)
    (tmp_path / "admin_bot" / ".openclaw" / "workspace").mkdir(parents=True)

    # Pass 1: full pod scan — admin_bot fires a leak.
    bas.run(
        tmp_path, cfg, bots=["team_bot_a", "admin_bot"],
        tree_lister=_make_tree_lister({
            "team_bot_a":   ["SOUL.md"],
            "admin_bot": ["notes/leaked.md"],
        }),
        manifest_loader=lambda ws: [_LEAK_MANIFEST],
        workspace_resolver=_make_workspace_resolver(tmp_path),
    )
    assert len(list(signals_store.iter_active(tmp_path, producer="backup_audit_signal"))) == 1

    # Pass 2: narrow scan on team_bot_a only. admin_bot's leak Signal MUST survive.
    bas.run(
        tmp_path, cfg, bots=["team_bot_a"],  # explicit narrow
        tree_lister=_make_tree_lister({"team_bot_a": ["SOUL.md"]}),
        manifest_loader=lambda ws: [_LEAK_MANIFEST],
        workspace_resolver=_make_workspace_resolver(tmp_path),
    )
    survivors = list(signals_store.iter_active(tmp_path, producer="backup_audit_signal"))
    assert len(survivors) == 1
    assert survivors[0].bot_id == "admin_bot"


def test_run_audit_error_preserves_existing_leak_signal(tmp_path):
    """A transient git error on the audit run must NOT archive a firing leak.

    Before this fix, if audit_bot returned err (e.g. ``git ls-tree rc=128:
    bad object HEAD``), the run() loop did ``continue`` without adding the
    bot's signature to ``kept``. The subsequent sweep_resolve then archived
    the previously-firing leak Signal because no signature kept it alive.
    """
    cfg = _cfg(bot="team_bot_a")
    workspace = tmp_path / "team_bot_a" / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)

    # Pass 1: fire a real leak.
    bas.run(
        tmp_path, cfg, bots=["team_bot_a"],
        tree_lister=_make_tree_lister({"team_bot_a": ["notes/leaked.md"]}),
        manifest_loader=lambda ws: [_LEAK_MANIFEST],
        workspace_resolver=_make_workspace_resolver(tmp_path),
    )
    assert len(list(signals_store.iter_active(tmp_path, producer="backup_audit_signal"))) == 1

    # Pass 2: audit fails on the same bot. Existing Signal MUST survive.
    def angry_lister(ws):
        return [], "git ls-tree rc=128: synthetic error"

    bas.run(
        tmp_path, cfg, bots=["team_bot_a"],
        tree_lister=angry_lister,
        manifest_loader=lambda ws: [_LEAK_MANIFEST],
        workspace_resolver=_make_workspace_resolver(tmp_path),
    )
    survivors = list(signals_store.iter_active(tmp_path, producer="backup_audit_signal"))
    assert len(survivors) == 1, "transient error archived a real firing leak"
