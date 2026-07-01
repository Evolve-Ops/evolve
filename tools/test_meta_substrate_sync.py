"""Mechanical tests for tools/meta-substrate-sync (META:substrate Initiative 11, §16).

Drives the real bash tool via subprocess against throwaway tmp targets, using the live
Evolve checkout as the sync SOURCE (--source). No network, no live pod.

Run with:
  cd tools && python3 -m pytest test_meta_substrate_sync.py -v
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "tools" / "meta-substrate-sync"

# The manifest the tool installs (kept in sync with the tool's SUBSTRATE_* arrays).
EXPECTED_DOCS = [
    "docs/META-bootstrap.md",
    "docs/META-session-guide.md",
    "docs/meta-ledger-schema.md",
    "docs/meta-reconcile-procedure.md",
    "docs/meta-coherence-procedure.md",
    "docs/meta-system-setup.md",
    "docs/using-the-meta-system.md",
]
EXPECTED_TOOLS = [
    "tools/meta-config",
    "tools/meta_config.py",
    "tools/meta-inflight",
    "tools/meta-issue",
    "tools/meta-ledger-prune",
    "tools/meta-queue",
    "tools/meta-skills-sync",
    "tools/test_meta_config.py",
]
MANIFEST = EXPECTED_DOCS + EXPECTED_TOOLS
STARTER_REGISTRY = "docs/META-aspect-registry.md"
META_JSON = ".claude/meta.json"

# tools/preflight and tools/ui-style-lint are Evolve-specific and must NEVER be carried.
EXCLUDED = ["tools/preflight", "tools/ui-style-lint"]


def run(*args):
    """Invoke the tool with --source pointed at the live checkout; return CompletedProcess."""
    return subprocess.run(
        [str(TOOL), *args, "--source", str(REPO_ROOT)],
        capture_output=True,
        text=True,
    )


def _init_git_remote(path: Path, url: str) -> None:
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", url], check=True)


# --- dry-run ------------------------------------------------------------------------------

def test_dry_run_lists_actions_and_writes_nothing(tmp_path):
    r = run(str(tmp_path), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "would sync" in r.stdout
    # Every manifest file + both scaffolds appear as would-create actions.
    for rel in MANIFEST:
        assert rel in r.stdout, f"{rel} not listed in dry-run output"
    assert STARTER_REGISTRY in r.stdout
    assert META_JSON in r.stdout
    # Nothing was actually written.
    assert not (tmp_path / "docs").exists()
    assert not (tmp_path / "tools").exists()
    assert not (tmp_path / ".claude").exists()


# --- fresh apply --------------------------------------------------------------------------

def test_fresh_target_gets_docs_tools_and_scaffolds(tmp_path):
    _init_git_remote(tmp_path, "git@github.com:cjalden/calgraph.git")
    r = run(str(tmp_path))
    assert r.returncode == 0, r.stderr

    # Every manifest file landed with byte-identical content.
    for rel in MANIFEST:
        dst = tmp_path / rel
        assert dst.is_file(), f"{rel} not created"
        assert dst.read_bytes() == (REPO_ROOT / rel).read_bytes(), f"{rel} bytes differ"

    # Tools are executable.
    for rel in EXPECTED_TOOLS:
        assert (tmp_path / rel).stat().st_mode & 0o111, f"{rel} not executable"

    # meta.json scaffolded with the inferred slug + the dedicated registry path.
    meta = (tmp_path / META_JSON).read_text()
    assert "cjalden/calgraph" in meta
    assert STARTER_REGISTRY in meta
    # A pod-less consumer ships on merge (the resolver default is pod-canary, which it must
    # override so `merged` — not the pod-only `live` bucket — is the terminal state) (#3370).
    assert '"deploy_model": "ship-on-merge"' in meta
    # The OPTIONAL fields are OMITTED so a fresh project (no tools/preflight of its own — that's
    # Evolve's, never synced) degrades the chip DoD to "push, then poll gh pr checks" instead of
    # invoking a nonexistent command. memory_slug is derived by the resolver, not pinned.
    assert "preflight_cmd" not in meta
    assert "flaky_jobs_doc" not in meta
    assert "memory_slug" not in meta
    assert "tools/preflight" not in meta

    # A fresh, empty-of-Evolve-aspects starter registry landed at the registry_path.
    reg = (tmp_path / STARTER_REGISTRY).read_text()
    assert "OWN aspect registry" in reg
    assert "| Aspect | Spec | Memory | Deploy |" in reg
    # It must NOT carry Evolve's aspect rows (a data row is `| `<id>` — ...`).
    assert "`model-tiers`" not in reg
    assert "`substrate` — META substrate" not in reg
    # No LIVE aspect data rows (an example row may live inside an HTML comment — strip those).
    import re
    uncommented = re.sub(r"<!--.*?-->", "", reg, flags=re.DOTALL)
    assert not any(
        ln.lstrip().startswith("| `") for ln in uncommented.splitlines()
    ), "starter registry must have no live aspect data rows"


def test_excluded_tools_never_synced(tmp_path):
    run(str(tmp_path))
    for rel in EXCLUDED:
        assert not (tmp_path / rel).exists(), f"{rel} must be excluded from the manifest"


def test_synced_meta_config_resolves_from_target(tmp_path):
    """The synced resolver, run from the target, reads the target's own scaffolded config."""
    _init_git_remote(tmp_path, "https://github.com/evolve-ops/calgraph.git")
    run(str(tmp_path))
    out = subprocess.run(
        [sys.executable, "tools/meta-config", "repo_slug"],
        cwd=str(tmp_path), capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "cjalden/calgraph"
    # deploy_model resolves to the scaffolded ship-on-merge, not the pod-canary default.
    dm = subprocess.run(
        [sys.executable, "tools/meta-config", "deploy_model"],
        cwd=str(tmp_path), capture_output=True, text=True,
    )
    assert dm.returncode == 0, dm.stderr
    assert dm.stdout.strip() == "ship-on-merge"


# --- idempotence --------------------------------------------------------------------------

def test_second_run_is_byte_identical_noop(tmp_path):
    _init_git_remote(tmp_path, "git@github.com:cjalden/calgraph.git")
    run(str(tmp_path))
    snapshot = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    r2 = run(str(tmp_path))
    assert r2.returncode == 0, r2.stderr
    assert "0 created, 0 updated" in r2.stdout
    assert f"{len(MANIFEST)} unchanged" in r2.stdout
    assert "2 kept" in r2.stdout
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == snapshot, "second run must not change any bytes"


# --- never-deletes ------------------------------------------------------------------------

def test_never_deletes_target_local_files(tmp_path):
    run(str(tmp_path))
    local_tool = tmp_path / "tools" / "my-local-tool"
    local_doc = tmp_path / "docs" / "my-local-doc.md"
    local_tool.write_text("local")
    local_doc.write_text("mine")
    r = run(str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert local_tool.is_file() and local_tool.read_text() == "local"
    assert local_doc.is_file() and local_doc.read_text() == "mine"


# --- no-clobber of an existing target meta.json -------------------------------------------

def test_existing_meta_json_not_clobbered(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    custom = '{\n  "repo_slug": "someone/custom",\n  "registry_path": "docs/custom-registry.md"\n}\n'
    (claude / "meta.json").write_text(custom)
    r = run(str(tmp_path))
    assert r.returncode == 0, r.stderr
    # meta.json is left byte-for-byte and reported as kept.
    assert (claude / "meta.json").read_text() == custom
    assert "kept" in r.stdout
    # The starter registry follows the target's OWN registry_path, not the default.
    assert (tmp_path / "docs" / "custom-registry.md").is_file()
    assert not (tmp_path / STARTER_REGISTRY).exists()


def _meta_config_registry_path(target: Path) -> str:
    """What the SYNCED resolver reports as registry_path from the target (what the skills read)."""
    out = subprocess.run(
        [sys.executable, "tools/meta-config", "registry_path"],
        cwd=str(target), capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_registry_scaffolded_where_the_resolver_reads_it(tmp_path):
    """The load-bearing invariant: wherever meta_config resolves registry_path, a registry exists.

    Cross-checks the tool against the SAME resolver the skills use — no tool-vs-resolver drift.
    """
    _init_git_remote(tmp_path, "git@github.com:cjalden/calgraph.git")
    run(str(tmp_path))
    resolved = _meta_config_registry_path(tmp_path)
    assert resolved == STARTER_REGISTRY  # fresh scaffold writes registry_path explicitly
    assert (tmp_path / resolved).is_file(), "registry must exist at the path the skills read"


def test_existing_meta_omitting_registry_path_scaffolds_starter_no_orphan(tmp_path):
    """The former #1 orphan trap, now benign after the default moved off the doctrine guide:
    a pre-existing meta.json WITHOUT registry_path resolves (via meta_config's default) to
    docs/META-aspect-registry.md — the target's OWN starter path, NOT a synced doctrine doc —
    so the tool scaffolds the fresh starter registry there and does NOT warn.

    Before #3369 the default was docs/META-session-guide.md, a synced doc carrying Evolve's
    aspect table, so an omitted registry_path landed the skills on Evolve's aspects; the tool
    warned instead of scaffolding an unread orphan. #3369 moved DEFAULT_REGISTRY_PATH to
    docs/META-aspect-registry.md (== STARTER_REGISTRY), which is never synced into the target,
    so the omitted-registry_path case is no longer a misconfiguration — it lands on the
    target's own scaffolded registry."""
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "meta.json").write_text('{ "repo_slug": "x/y" }\n')
    r = run(str(tmp_path))
    assert r.returncode == 0, r.stderr
    # The default now resolves to the target's OWN starter registry, not Evolve's doctrine table.
    assert _meta_config_registry_path(tmp_path) == STARTER_REGISTRY
    # So the starter registry IS scaffolded where the resolver reads it — no orphan, no misconfig.
    assert (tmp_path / STARTER_REGISTRY).is_file()
    # And the tool does NOT warn: the omitted-registry_path case is no longer surfaced.
    assert "resolves registry_path to" not in r.stderr
    assert "ACTION NEEDED" not in r.stderr


def test_existing_meta_pointing_at_doctrine_warns_and_scaffolds_no_orphan(tmp_path):
    """The still-load-bearing orphan-guard: a pre-existing meta.json whose registry_path EXPLICITLY
    points at a synced doctrine doc (docs/META-session-guide.md — overwritten with Evolve's aspect
    table on every sync) would leave the skills reading Evolve's aspects. The tool must WARN and NOT
    scaffold an unread starter registry at docs/META-aspect-registry.md."""
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "meta.json").write_text(
        '{ "repo_slug": "x/y", "registry_path": "docs/META-session-guide.md" }\n'
    )
    r = run(str(tmp_path))
    assert r.returncode == 0, r.stderr
    # The resolver reads the guide (the misconfiguration the tool surfaces), NOT a project registry.
    assert _meta_config_registry_path(tmp_path) == "docs/META-session-guide.md"
    # No orphan registry was created at the starter path.
    assert not (tmp_path / STARTER_REGISTRY).exists()
    # The condition is surfaced loudly.
    assert "resolves registry_path to" in r.stderr
    assert "ACTION NEEDED" in r.stderr


def test_multiline_registry_path_resolved_via_meta_config(tmp_path):
    """Regression for the #2 divergence: a valid meta.json with registry_path split across lines
    is resolved by json.loads (via meta_config), so the tool scaffolds at the TRUE path — not the
    starter fallback a line-oriented sed reader would pick."""
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "meta.json").write_text(
        '{\n  "repo_slug":\n    "x/y",\n  "registry_path":\n    "docs/split.md"\n}\n'
    )
    r = run(str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert _meta_config_registry_path(tmp_path) == "docs/split.md"
    assert (tmp_path / "docs" / "split.md").is_file()
    assert not (tmp_path / STARTER_REGISTRY).exists()


def test_no_remote_uses_placeholder_slug(tmp_path):
    r = run(str(tmp_path))
    assert r.returncode == 0, r.stderr
    meta = (tmp_path / META_JSON).read_text()
    assert "REPLACE-ME" in meta
    assert "could not infer repo_slug" in r.stderr


# --- guardrails ---------------------------------------------------------------------------

def test_missing_target_is_usage_error():
    r = subprocess.run([str(TOOL)], capture_output=True, text=True)
    assert r.returncode == 2
    assert "missing <target-repo-path>" in r.stderr


def test_refuses_to_sync_into_source(tmp_path):
    r = run(str(REPO_ROOT))
    assert r.returncode == 2
    assert "into itself" in r.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
