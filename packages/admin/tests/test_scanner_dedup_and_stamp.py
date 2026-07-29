"""Tests for the scanner's manifest dedup pass and stamp-time file filtering.

Three behaviours pinned here:

  1. ``_dedup_manifests`` hydrates v7-arc Instances before comparison so
     the bound Spec's name field participates in the heuristic. Without
     hydration, every v7-arc Instance was silently filtered out (the
     raw Instance carries no top-level ``name``) and the dedup pass
     became a no-op once a bot had migrated to v7-arc.

  2. v7-arc Instances bound to the same Spec (``provenance.spec_id``)
     are auto-merged regardless of name/file heuristics — same Spec
     means same app by definition.

  3. ``_stamp_discovered_files`` skips boilerplate/template files when
     expanding a ``directory:`` evidence hint. The 2026-05-27 admin_bot
     observation was that home/gitignore-template.md got claimed as
     a Home Repairs Log file because the LLM emitted a directory hint.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import scanner as _scanner  # noqa: E402


# ── _dedup_manifests: legacy shape ───────────────────────────────────────────


def _write_manifest(caps_dir: Path, app_id: str, **fields) -> Path:
    payload = {"id": app_id, **fields}
    p = caps_dir / f"{app_id}.json"
    p.write_text(json.dumps(payload, indent=2))
    return p


def test_dedup_merges_two_legacy_manifests_with_same_name(tmp_path):
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(
        caps, "home-repairs-log",
        name="Home Repairs Log",
        evidence_files=["home/home-repairs.md"],
        confidence=0.8,
    )
    _write_manifest(
        caps, "home-repairs-log-v2",
        name="Home Repairs Log",
        evidence_files=["home/electrical-panel.md"],
        confidence=0.9,
    )
    merged = _scanner._dedup_manifests(caps)
    assert merged == 1
    remaining = sorted(p.name for p in caps.glob("*.json"))
    assert len(remaining) == 1
    survivor = json.loads((caps / remaining[0]).read_text())
    # evidence_files union
    assert set(survivor["evidence_files"]) == {
        "home/home-repairs.md", "home/electrical-panel.md"
    }


def test_dedup_merges_name_variants_via_substring(tmp_path):
    """'Home Repairs' is a substring of 'Home Repairs Log' → sim=0.85."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(
        caps, "home-repairs",
        name="Home Repairs",
        evidence_files=["home/home-repairs.md"],
    )
    _write_manifest(
        caps, "home-repairs-log",
        name="Home Repairs Log",
        evidence_files=["home/different-file.md"],
    )
    merged = _scanner._dedup_manifests(caps)
    assert merged == 1


# ── _dedup_manifests: v7-arc shape ───────────────────────────────────────────


def _write_v7_arc_spec(shared_dir: Path, spec_id: str, name: str,
                       version: str = "2026.05.20-1.0") -> Path:
    spec_dir = shared_dir / "gallery" / "local" / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / f"{version}.json"
    spec_path.write_text(json.dumps({
        "spec_id": spec_id,
        "spec_version": version,
        "name": name,
        "description": f"{name} description",
    }, indent=2))
    return spec_path


def _write_v7_arc_instance(caps_dir: Path, instance_id: str, spec_id: str,
                            realized_paths: list[str],
                            spec_version: str = "2026.05.20-1.0") -> Path:
    payload = {
        "instance_id": instance_id,
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "provenance": {
            "spec_id": spec_id,
            "spec_version": spec_version,
        },
        "realized_files": [
            {"path": p, "file_id": f"f-{i:08x}", "logical_name": Path(p).stem}
            for i, p in enumerate(realized_paths)
        ],
        "status": "active",
    }
    p = caps_dir / f"{instance_id}.json"
    p.write_text(json.dumps(payload, indent=2))
    return p


def test_dedup_merges_v7_arc_instances_with_same_spec_id(tmp_path):
    shared = tmp_path / "shared"
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_v7_arc_spec(shared, "p-spec0001", "Home Repairs Log")

    _write_v7_arc_instance(caps, "i-1", "p-spec0001",
                           ["home/home-repairs.md"])
    _write_v7_arc_instance(caps, "i-2", "p-spec0001",
                           ["home/electrical-panel.md"])

    merged = _scanner._dedup_manifests(caps, shared_dir=shared)
    assert merged == 1

    # The survivor must carry the union of realized_files
    survivors = list(caps.glob("*.json"))
    assert len(survivors) == 1
    surv = json.loads(survivors[0].read_text())
    paths = {rf["path"] for rf in surv["realized_files"]}
    assert paths == {"home/home-repairs.md", "home/electrical-panel.md"}
    # Raw Instance shape preserved — no Spec-derived fields leaked into the
    # winner (no `name`, no `description`).
    assert "name" not in surv
    assert "description" not in surv


def test_dedup_merges_v7_arc_by_name_via_hydration(tmp_path):
    """Two v7-arc Instances on DIFFERENT Specs but with the same Spec.name
    must still merge via the name-similarity heuristic. Without hydration
    this case was silently skipped (raw Instance has no `name`)."""
    shared = tmp_path / "shared"
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_v7_arc_spec(shared, "p-spec0001", "Home Repairs Log")
    _write_v7_arc_spec(shared, "p-spec0002", "Home Repairs Log")

    _write_v7_arc_instance(caps, "i-1", "p-spec0001",
                           ["home/home-repairs.md"])
    _write_v7_arc_instance(caps, "i-2", "p-spec0002",
                           ["home/electrical-panel.md"])

    merged = _scanner._dedup_manifests(caps, shared_dir=shared)
    assert merged == 1


def test_dedup_skips_v7_arc_without_shared_dir(tmp_path):
    """No shared_dir → hydration skipped → fall back to file-overlap.

    Different files + no spec_id collision → no merge. This pins the
    safe fallback behaviour for callers that haven't been updated.
    """
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_v7_arc_instance(caps, "i-1", "p-spec0001",
                           ["home/home-repairs.md"])
    _write_v7_arc_instance(caps, "i-2", "p-spec0002",
                           ["home/electrical-panel.md"])
    merged = _scanner._dedup_manifests(caps)
    assert merged == 0


def test_dedup_v7_arc_merges_on_file_overlap_without_hydration(tmp_path):
    """When two Instances share most realized_files, dedup catches them
    via the path-overlap heuristic even without Spec hydration."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_v7_arc_instance(caps, "i-1", "p-spec0001",
                           ["home/home-repairs.md", "home/electrical-panel.md"])
    _write_v7_arc_instance(caps, "i-2", "p-spec0002",
                           ["home/home-repairs.md", "home/electrical-panel.md"])
    merged = _scanner._dedup_manifests(caps)
    assert merged == 1


# ── _merge_two_manifests: owned_by rewrite ───────────────────────────────────


def test_merge_rewrites_loser_owned_by_to_winner_pkg():
    """When dedup merges two manifests, every files[*] entry whose
    ``owned_by`` was the loser's pkg_id is rewritten to the winner's
    pkg_id — otherwise the loser's pkg_id survives in the merged
    record after the loser manifest gets unlinked, leaving stale
    ``owned_by`` tags. Regression for the 78-entry pod-wide stale-tag
    leak surfaced 2026-06-08 (a personal-bot biometric integration
    app et al)."""
    winner = {
        "id": "a",
        "name": "App A",
        "pkg_id": "p-WIN",
        "files": [
            {"file_id": "f-1", "path": "a/own.md", "owned_by": "p-WIN"},
        ],
    }
    loser = {
        "id": "b",
        "name": "App A",
        "pkg_id": "p-LOSE",
        "files": [
            {"file_id": "f-2", "path": "a/only-on-loser.md",
             "owned_by": "p-LOSE"},
            # A cross-app reference to a live third party MUST be preserved.
            {"file_id": "f-3", "path": "a/cross-app.md",
             "owned_by": "p-THIRD"},
        ],
    }
    merged = _scanner._merge_two_manifests(winner, loser)
    by_path = {f["path"]: f for f in merged["files"]}
    # Winner's own file untouched.
    assert by_path["a/own.md"]["owned_by"] == "p-WIN"
    # Loser-only file rewritten to winner.pkg_id.
    assert by_path["a/only-on-loser.md"]["owned_by"] == "p-WIN"
    # Cross-app reference preserved verbatim.
    assert by_path["a/cross-app.md"]["owned_by"] == "p-THIRD"


def test_merge_skips_rewrite_when_winner_or_loser_pkg_missing():
    """If either manifest has no pkg_id, the rewrite is skipped — the
    cleanup pass (scanner_repair.repair_stale_owned_by_in_dir) handles
    that case downstream."""
    winner = {"id": "a", "pkg_id": "", "files": [
        {"file_id": "f-1", "path": "x.md", "owned_by": "p-OLD"},
    ]}
    loser = {"id": "b", "pkg_id": "p-OLD", "files": [
        {"file_id": "f-2", "path": "y.md", "owned_by": "p-OLD"},
    ]}
    merged = _scanner._merge_two_manifests(winner, loser)
    by_path = {f["path"]: f for f in merged["files"]}
    # Both unchanged — winner has no pkg_id to rewrite to.
    assert by_path["x.md"]["owned_by"] == "p-OLD"
    assert by_path["y.md"]["owned_by"] == "p-OLD"


# ── _ev_path_keys: combined fields ───────────────────────────────────────────


def test_ev_path_keys_combines_evidence_realized_and_files():
    manifest = {
        "evidence_files": ["directory: home/", "scripts/foo.py"],
        "realized_files": [
            {"path": "home/log.md", "file_id": "f-1"},
            {"path": "scripts/foo.py", "file_id": "f-2"},
        ],
        "files": [{"path": "tests/test_foo.py"}],
    }
    keys = _scanner._ev_path_keys(manifest)
    assert "home" in keys                # stripped "directory: " tag
    assert "scripts/foo.py" in keys      # dedup across evidence + realized
    assert "home/log.md" in keys
    assert "tests/test_foo.py" in keys


# ── _stamp_discovered_files: template-file filter ────────────────────────────


def test_stamp_directory_expansion_skips_template_files(tmp_path):
    workspace = tmp_path / "workspace"
    home = workspace / "home"
    home.mkdir(parents=True)
    # Real app files
    (home / "home-repairs.md").write_text("# Home Repairs Log\n")
    (home / "electrical-panel.md").write_text("# Panel map\n")
    # Boilerplate / template files that should NOT be claimed
    (home / "gitignore-template.md").write_text("# Template\n")
    (home / "README.md").write_text("# Readme\n")
    (home / "LICENSE").write_text("MIT\n")
    (home / ".gitignore").write_text("*.log\n")

    manifest = {
        "id": "home-repairs-log",
        "name": "Home Repairs Log",
        "bot_id": "admin_bot",
        "evidence_files": ["directory: home/"],
        "files": [],
    }
    manifest_path = tmp_path / "home-repairs-log.json"
    manifest_path.write_text(json.dumps(manifest))

    logs: list[str] = []
    _scanner._stamp_discovered_files(
        manifest, workspace, manifest_path, log_collector=logs
    )

    final = json.loads(manifest_path.read_text())
    paths = {entry["path"] for entry in final.get("files", [])}
    # Real files registered
    assert "home/home-repairs.md" in paths
    assert "home/electrical-panel.md" in paths
    # Templates and boilerplate excluded
    assert "home/gitignore-template.md" not in paths
    assert "home/README.md" not in paths
    assert "home/LICENSE" not in paths
    assert "home/.gitignore" not in paths

    # Log surfaces what got skipped, so operators can see the filter at work.
    skipped_line = next(
        (line for line in logs if "skipped templates" in line), None
    )
    assert skipped_line is not None
    assert "gitignore-template.md" in skipped_line


def test_stamp_explicit_file_evidence_is_not_filtered(tmp_path):
    """If an app explicitly lists a template-looking file as evidence
    (e.g. an app whose job IS to manage README.md), that gets registered.
    The filter only applies to directory-expansion."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("# Readme\n")

    manifest = {
        "id": "readme-curator",
        "name": "Readme Curator",
        "bot_id": "admin_bot",
        "evidence_files": ["README.md"],  # explicit file, not directory
        "files": [],
    }
    manifest_path = tmp_path / "readme-curator.json"
    manifest_path.write_text(json.dumps(manifest))

    _scanner._stamp_discovered_files(manifest, workspace, manifest_path)

    final = json.loads(manifest_path.read_text())
    paths = {entry["path"] for entry in final.get("files", [])}
    assert "README.md" in paths


# ── _stamp_discovered_files: never-ownable ownership-policy gate ─────────────
#
# Phase 5 used to skip only {"manifests", ".openclaw", ".git"}. When the LLM's
# evidence pointed into the evolve/ telemetry tree (audit_outbox rec-*.json) or
# at an OpenClaw-standard file (AGENTS.md), the stamp pass minted file_ids and
# embedded _evolve markers into all of them — the bug that produced ~1,000
# false "orphan files" on each of two bots. The marker-writer now consults the
# shared can_app_own predicate before BOTH registering and stamping any path.


def test_stamp_skips_never_ownable_direct_file_evidence(tmp_path):
    """Direct-file evidence pointing at platform telemetry / OC-standard files
    is neither registered NOR physically stamped, while an ordinary script
    evidence file is."""
    from evolve_admin.applications.provenance import parse_marker

    workspace = tmp_path / "workspace"
    # Platform telemetry: an audit_outbox record (never ownable — evolve/ tree).
    rec = workspace / "evolve" / "audit_outbox" / "_ingested" / "2026-06-09" / "rec-0001.json"
    rec.parent.mkdir(parents=True)
    rec_original = '{"event": "audit", "id": "rec-0001"}'
    rec.write_text(rec_original)
    # OpenClaw-standard identity file at the workspace root (never ownable).
    agents = workspace / "AGENTS.md"
    agents_original = "# Agent instructions\n"
    agents.write_text(agents_original)
    # An ordinary bot-authored script — legitimately ownable, must be stamped.
    foo = workspace / "scripts" / "foo.py"
    foo.parent.mkdir(parents=True)
    foo.write_text("print('hello')\n")

    manifest = {
        "id": "team-bot-a-app",
        "name": "Team Bot A App",
        "bot_id": "team-bot-a",
        "evidence_files": [
            "evolve/audit_outbox/_ingested/2026-06-09/rec-0001.json",
            "AGENTS.md",
            "scripts/foo.py",
        ],
        "files": [],
    }
    manifest_path = tmp_path / "team-bot-a-app.json"
    manifest_path.write_text(json.dumps(manifest))

    logs: list[str] = []
    _scanner._stamp_discovered_files(
        manifest, workspace, manifest_path, log_collector=logs
    )

    final = json.loads(manifest_path.read_text())
    paths = {entry["path"] for entry in final.get("files", [])}

    # Never-ownable paths are NOT registered.
    assert "evolve/audit_outbox/_ingested/2026-06-09/rec-0001.json" not in paths
    assert "AGENTS.md" not in paths
    # The ordinary script IS registered.
    assert "scripts/foo.py" in paths

    # Never-ownable paths are NOT physically stamped — content is untouched,
    # no marker embedded.
    assert rec.read_text() == rec_original
    assert "_evolve" not in json.loads(rec.read_text())
    assert agents.read_text() == agents_original
    assert parse_marker(rec) is None
    assert parse_marker(agents) is None
    # The ordinary script DID get a marker.
    foo_marker = parse_marker(foo)
    assert foo_marker is not None and foo_marker.is_valid()

    # The skip is surfaced at _slog granularity for both never-ownable paths.
    skip_lines = [ln for ln in logs if "never-ownable per ownership policy" in ln]
    assert any("rec-0001.json" in ln for ln in skip_lines)
    assert any("AGENTS.md" in ln for ln in skip_lines)


def test_stamp_directory_expansion_skips_never_ownable_children(tmp_path):
    """An OWNABLE directory hint (``scripts/``) that happens to contain a
    never-ownable child (an OC-standard AGENTS.md, the tasks.json substrate
    store) must register only the genuinely-ownable children — the
    never-ownable gate runs inside the directory walk too."""
    workspace = tmp_path / "workspace"
    scripts = workspace / "scripts"
    scripts.mkdir(parents=True)
    # Genuinely ownable child.
    (scripts / "runner.py").write_text("print('run')\n")
    # Never-ownable children that happen to live alongside it.
    agents = scripts / "AGENTS.md"
    agents.write_text("# stray standing instructions\n")
    tasks = scripts / "tasks.json"
    tasks.write_text('{"tasks": []}')

    manifest = {
        "id": "team-bot-b-app",
        "name": "Team Bot B App",
        "bot_id": "team-bot-b",
        "evidence_files": ["directory: scripts/"],
        "files": [],
    }
    manifest_path = tmp_path / "team-bot-b-app.json"
    manifest_path.write_text(json.dumps(manifest))

    logs: list[str] = []
    _scanner._stamp_discovered_files(
        manifest, workspace, manifest_path, log_collector=logs
    )

    final = json.loads(manifest_path.read_text())
    paths = {entry["path"] for entry in final.get("files", [])}
    # Ownable child registered.
    assert "scripts/runner.py" in paths
    # Never-ownable children neither registered…
    assert "scripts/AGENTS.md" not in paths
    assert "scripts/tasks.json" not in paths
    # …nor stamped.
    assert "_evolve" not in json.loads(tasks.read_text())
    assert agents.read_text() == "# stray standing instructions\n"
    # The skip is surfaced in the directory-expansion note.
    assert any("skipped never-ownable" in ln for ln in logs)


# ── _canon_name + match scoring (scan-time dedup) ───────────────────────────


def test_canon_name_collapses_drift():
    """The four common drift shapes all canonicalize to the same key."""
    assert _scanner._canon_name("app-biometric-integration") == "biometric-integration"
    assert _scanner._canon_name("biometric-integration") == "biometric-integration"
    assert _scanner._canon_name("Biometric Integration") == "biometric-integration"
    assert _scanner._canon_name("biometric_integration") == "biometric-integration"
    assert _scanner._canon_name("  BIOMETRIC--INTEGRATION  ") == "biometric-integration"


def test_canon_name_empty_inputs():
    assert _scanner._canon_name("") == ""
    assert _scanner._canon_name(None) == ""
    assert _scanner._canon_name("app-") == ""


def _detected(app_id: str, name: str, evidence_files=None) -> "_scanner.DetectedApplication":
    return _scanner.DetectedApplication(
        id=app_id,
        name=name,
        confidence=0.8,
        evidence_files=evidence_files or [],
        evidence_summary="",
        suggested_goals=[],
        suggested_tests=[],
        suggested_privacy=[],
    )


def test_match_detected_id_equals_existing_stem(tmp_path):
    """Pre-fix behaviour preserved: exact stem match still hits."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "email-assistant", name="Email Assistant")
    index = _scanner._build_existing_manifest_index(caps)
    det = _detected("email-assistant", "Email Assistant")
    match = _scanner._match_detected_to_existing(det, index, set())
    assert match is not None
    assert match["stem"] == "email-assistant"


def test_match_via_canon_strips_app_prefix(tmp_path):
    """Existing ``app-biometric-integration.json`` matches detected id
    ``biometric-integration`` — the central drift case."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "app-biometric-integration", name="Biometric Integration")
    index = _scanner._build_existing_manifest_index(caps)
    det = _detected("biometric-integration", "Biometric Integration")
    match = _scanner._match_detected_to_existing(det, index, set())
    assert match is not None
    assert match["stem"] == "app-biometric-integration"


def test_match_via_canon_handles_case_and_hyphen_drift(tmp_path):
    """LLM returns ``EmailAssistant`` while existing stem is
    ``app-email-assistant``. Both canonicalize to ``email-assistant``."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "app-email-assistant", name="Email Assistant")
    index = _scanner._build_existing_manifest_index(caps)
    det = _detected("EmailAssistant", "Email Assistant")
    match = _scanner._match_detected_to_existing(det, index, set())
    assert match is not None
    assert match["stem"] == "app-email-assistant"


def test_match_via_evidence_overlap_to_v7_arc_instance(tmp_path):
    """Detected ``email-assistant`` matches an existing v7-arc instance
    file ``i-34cfcab1.json`` whose realized files overlap with the
    detection's evidence_files. No name/id resemblance, evidence saves
    the day."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(
        caps, "i-34cfcab1",
        manifest_shape="v7-arc",
        instance_id="i-34cfcab1",
        provenance={"spec_id": "p-spec0001", "spec_version": "2026.05.20-1.0"},
        realized_files=[
            {"path": "email/inbox-watcher.py", "file_id": "f-1"},
            {"path": "email/reply-drafter.py", "file_id": "f-2"},
            {"path": "email/digest.md", "file_id": "f-3"},
        ],
    )
    index = _scanner._build_existing_manifest_index(caps)
    det = _detected(
        "email-assistant", "Email Assistant",
        evidence_files=[
            "email/inbox-watcher.py",
            "email/reply-drafter.py",
            "email/digest.md",
        ],
    )
    match = _scanner._match_detected_to_existing(det, index, set())
    assert match is not None
    assert match["stem"] == "i-34cfcab1"


def test_match_genuinely_new_app_returns_none(tmp_path):
    """No id/name/evidence overlap → caller treats as new."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(
        caps, "app-email-assistant",
        name="Email Assistant",
        evidence_files=["email/inbox.py"],
    )
    index = _scanner._build_existing_manifest_index(caps)
    det = _detected(
        "calendar-sync", "Calendar Sync",
        evidence_files=["calendar/google-sync.py"],
    )
    match = _scanner._match_detected_to_existing(det, index, set())
    assert match is None


def test_match_does_not_double_claim(tmp_path):
    """Two detected apps with the same canonical key cannot both claim
    the same existing manifest."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "app-shared-name", name="Shared Name")
    index = _scanner._build_existing_manifest_index(caps)
    claimed: set = set()
    det_a = _detected("shared-name", "Shared Name")
    det_b = _detected("shared-name", "Shared Name")
    match_a = _scanner._match_detected_to_existing(det_a, index, claimed)
    assert match_a is not None
    claimed.add(match_a["path"])
    match_b = _scanner._match_detected_to_existing(det_b, index, claimed)
    assert match_b is None


def test_match_realistic_production_fixture(tmp_path):
    """Integration: real production stems (17 ``app-*`` + 3 ``i-*``)
    matched against a fresh LLM-style detection set. Pre-fix this
    classified 13/18 as new; post-fix all 18 must match the existing
    manifests, leaving 0 new."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    # 17 app-* stems pulled from a real bot's manifest dir (2026-06-08).
    legacy_stems = [
        ("app-alert-system", "Alert System"),
        ("app-backup-system", "Backup System"),
        ("app-beta-testing-coordinator", "Beta Testing Coordinator"),
        ("app-biometric-integration", "Biometric Integration"),
        ("app-cost-monitoring", "Cost Monitoring"),
        ("app-daily-briefing", "Daily Briefing"),
        ("app-daily-cost-check", "Daily Cost Check"),
        ("app-daily-logging", "Daily Logging"),
        ("app-document-generation", "Document Generation"),
        ("app-evolve-framework", "Evolve Framework"),
        ("app-home-repairs-log", "Home Repairs Log"),
        ("app-job-search-tracker", "Job Search Tracker"),
        ("app-manifest-inventory", "Manifest Inventory"),
        ("app-marie-campaign-manager", "Marie Campaign Manager"),
        ("app-novel-writing", "Novel Writing"),
        ("app-pex-project-management", "PEX Project Management"),
        ("app-slack-integration", "Slack Integration"),
    ]
    for stem, name in legacy_stems:
        _write_manifest(caps, stem, name=name,
                        evidence_files=[f"{stem.replace('app-', '')}/main.py"])
    # 3 v7-arc instances whose stems bear no resemblance to LLM ids,
    # so they only get matched via evidence overlap.
    _write_manifest(
        caps, "i-34cfcab1",
        manifest_shape="v7-arc",
        instance_id="i-34cfcab1",
        realized_files=[{"path": "morning-pipeline/runner.py", "file_id": "f-1"},
                        {"path": "morning-pipeline/digest.md", "file_id": "f-2"}],
    )
    _write_manifest(
        caps, "i-3f53d00c",
        manifest_shape="v7-arc",
        instance_id="i-3f53d00c",
        realized_files=[{"path": "audit/sweep.py", "file_id": "f-3"}],
    )
    _write_manifest(
        caps, "i-dd3336e6",
        manifest_shape="v7-arc",
        instance_id="i-dd3336e6",
        realized_files=[{"path": "weather/forecast-fetch.py", "file_id": "f-4"}],
    )

    # LLM produces slugs WITHOUT the app- prefix. Three of them are
    # the v7-arc instance apps, identified only by evidence overlap.
    detected = [
        _detected(stem.replace("app-", ""), name,
                  evidence_files=[f"{stem.replace('app-', '')}/main.py"])
        for stem, name in legacy_stems
    ]
    detected += [
        _detected("morning-briefing", "Morning Briefing",
                  evidence_files=["morning-pipeline/runner.py",
                                  "morning-pipeline/digest.md"]),
        _detected("workspace-audit", "Workspace Audit",
                  evidence_files=["audit/sweep.py"]),
        _detected("weather-forecast", "Weather Forecast",
                  evidence_files=["weather/forecast-fetch.py"]),
    ]

    index = _scanner._build_existing_manifest_index(caps)
    claimed: set = set()
    matched, new = 0, 0
    for d in detected:
        m = _scanner._match_detected_to_existing(d, index, claimed)
        if m is None:
            new += 1
        else:
            matched += 1
            claimed.add(m["path"])

    assert matched == 20
    assert new == 0


# ── dedup_existing_manifests: one-time cleanup mode ─────────────────────────


def test_dedup_existing_merges_app_prefix_duplicates(tmp_path):
    """Two manifests for the same app (one with the ``app-`` prefix,
    one without) — the cleanup mode merges them, keeping the older
    one and archiving the newer one to ``_history/``."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(
        caps, "app-email-assistant",
        name="Email Assistant",
        evidence_files=["email/inbox.py"],
        created_at="2026-04-01T00:00:00Z",
        description="Operator-edited description",  # only on the older one
    )
    _write_manifest(
        caps, "email-assistant",
        name="Email Assistant",
        evidence_files=["email/digest.md"],
        created_at="2026-06-01T00:00:00Z",
        confidence=0.92,  # only on the newer one
    )
    result = _scanner.dedup_existing_manifests(caps)
    assert result["merged"] == 1
    # Older winner survives at original path
    survivors = sorted(p.name for p in caps.glob("*.json") if "_history" not in str(p))
    assert survivors == ["app-email-assistant.json"]
    surv = json.loads((caps / "app-email-assistant.json").read_text())
    # Operator-edited description preserved
    assert surv["description"] == "Operator-edited description"
    # Newer-only field filled into the older winner
    assert surv["confidence"] == 0.92
    # Loser archived, not deleted
    archived = list((caps / "_history").glob("email-assistant_deduped_*.json"))
    assert len(archived) == 1


def test_dedup_existing_picks_older_created_at(tmp_path):
    """Older created_at wins regardless of which order the pair is
    enumerated in — operator-edited content lives in the older file."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    # Same canonical name, deliberately ordered NEWER first by stem so
    # the i<j enumeration would naturally pick the wrong one without
    # the created_at tiebreaker.
    _write_manifest(
        caps, "aaa-newer-stem",
        name="Shared Capability",
        created_at="2026-06-01T00:00:00Z",
    )
    _write_manifest(
        caps, "zzz-older-stem",
        name="Shared Capability",
        created_at="2026-01-01T00:00:00Z",
    )
    result = _scanner.dedup_existing_manifests(caps)
    assert result["merged"] == 1
    survivors = sorted(p.name for p in caps.glob("*.json") if "_history" not in str(p))
    assert survivors == ["zzz-older-stem.json"]


def test_dedup_existing_clean_dir_is_noop(tmp_path):
    """Three genuinely distinct manifests — cleanup must touch nothing."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "app-foo", name="Foo", evidence_files=["foo/a.py"])
    _write_manifest(caps, "app-bar", name="Bar", evidence_files=["bar/b.py"])
    _write_manifest(caps, "app-baz", name="Baz", evidence_files=["baz/c.py"])
    result = _scanner.dedup_existing_manifests(caps)
    assert result["merged"] == 0
    assert sorted(p.name for p in caps.glob("*.json")) == [
        "app-bar.json", "app-baz.json", "app-foo.json",
    ]


def test_dedup_existing_three_way_collapse(tmp_path):
    """Three manifests all claim the same canonical app — all three
    should collapse to the oldest, with two archives."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(
        caps, "app-cost-monitoring",
        name="Cost Monitoring",
        created_at="2026-01-15T00:00:00Z",
        evidence_files=["cost/watchdog.py"],
    )
    _write_manifest(
        caps, "cost-monitoring",
        name="Cost Monitoring",
        created_at="2026-03-01T00:00:00Z",
        evidence_files=["cost/spend-alert.py"],
    )
    _write_manifest(
        caps, "CostMonitoring",
        name="Cost Monitoring",
        created_at="2026-05-01T00:00:00Z",
        evidence_files=["cost/burst-detector.py"],
    )
    result = _scanner.dedup_existing_manifests(caps)
    assert result["merged"] == 2
    survivors = [p.name for p in caps.glob("*.json") if "_history" not in str(p)]
    assert survivors == ["app-cost-monitoring.json"]
    archived = sorted(p.name for p in (caps / "_history").glob("*.json"))
    assert len(archived) == 2


# ── Thin v7-arc Instance dedup-on-adoption (atlas Task Manager regression) ──
#
# A thin v7-arc Instance carries id=null/name=null at top level; the
# human-facing name lives in the bound gallery Spec and only materializes
# after hydration. Before the fix, _build_existing_manifest_index read the
# raw JSON, so the index entry had an empty name_canon — leaving only the
# instance-id filename stem (which resembles no detected app) to match on.
# When the LLM-detected id/name drifted from that stem, NO pass matched and
# Phase 4 minted a DUPLICATE manifest for an app that already existed
# (atlas: task-manager.json + task_manager.json, 2026-06-16). The fix
# hydrates Instances inside the index builder, mirroring _dedup_manifests.


def _write_builtin_v7_arc_spec(shared_dir: Path, spec_id: str, name: str,
                               version: str = "2026.05.20-1.0") -> Path:
    """Write a gallery BUILTIN Spec (the tier the atlas Task Manager was
    bound to) carrying the human-facing name."""
    spec_dir = shared_dir / "gallery" / "builtin" / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / f"{version}.json"
    spec_path.write_text(json.dumps({
        "spec_id": spec_id,
        "spec_version": version,
        "name": name,
        "description": f"{name} description",
    }, indent=2))
    return spec_path


def _write_thin_v7_arc_instance(caps_dir: Path, filename_stem: str,
                                spec_id: str, realized_paths: list[str],
                                spec_version: str = "2026.05.20-1.0") -> Path:
    """Write a THIN v7-arc Instance with top-level id/name EXPLICITLY null,
    filed under an instance-id-style stem (``i-…``) that resembles no
    detected app — so the only post-hydration matching signal is the
    Spec-derived name. Mirrors how a gallery-installed Instance lands."""
    payload = {
        "instance_id": filename_stem,
        "id": None,
        "name": None,
        "schema_version": 14,
        "manifest_shape": "v7-arc",
        "provenance": {"spec_id": spec_id, "spec_version": spec_version},
        "realized_files": [
            {"path": p, "file_id": f"f-{i:08x}", "logical_name": Path(p).stem}
            for i, p in enumerate(realized_paths)
        ],
        "status": "active",
    }
    p = caps_dir / f"{filename_stem}.json"
    p.write_text(json.dumps(payload, indent=2))
    return p


def test_thin_v7_arc_instance_index_is_hydrated(tmp_path):
    """The dedup index hydrates a thin v7-arc Instance: the bound Spec's
    name becomes a non-empty ``name_canon`` and the Instance's realized
    files a non-empty ``evidence_set``. Without ``shared_dir`` the raw view
    leaves ``name_canon`` empty — the blind-index state that defeated the
    matcher."""
    shared = tmp_path / "shared"
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_builtin_v7_arc_spec(shared, "p-9bfa1c84", "Task Manager")
    _write_thin_v7_arc_instance(
        caps, "i-7c8a9b2d", "p-9bfa1c84", ["task-manager/tasks.md"],
    )

    # Pre-fix (raw) view: name_canon empty — nothing but the i-… stem.
    raw_index = _scanner._build_existing_manifest_index(caps)
    assert len(raw_index) == 1
    assert raw_index[0]["name_canon"] == ""

    # Post-fix (hydrated) view: name_canon + evidence_set both populated.
    index = _scanner._build_existing_manifest_index(caps, shared_dir=shared)
    assert len(index) == 1
    assert index[0]["name_canon"] == "task-manager"
    assert index[0]["evidence_set"]  # realized_files surfaced
    # The on-disk ``data`` stays RAW — no Spec name leaked into the Instance.
    assert index[0]["data"].get("name") is None


def test_thin_v7_arc_instance_matches_drifted_detection(tmp_path):
    """Regression for the atlas Task Manager duplicate (2026-06-16): a thin
    v7-arc Instance bound to a Spec named "Task Manager" must match a freshly
    detected app whose id drifted to ``task_manager`` (underscore) and whose
    file surface does NOT overlap the Instance's realized_files. Hydration
    surfaces the Spec name so canonical-key pass (c) hits. The pre-fix raw
    index returns None, so Phase 4 would mint a second manifest for the same
    logical app."""
    shared = tmp_path / "shared"
    caps = tmp_path / "manifests"
    caps.mkdir()
    inst = _write_thin_v7_arc_instance(
        caps, "i-7c8a9b2d", "p-9bfa1c84", ["task-manager/tasks.md"],
    )
    _write_builtin_v7_arc_spec(shared, "p-9bfa1c84", "Task Manager")

    # Drifted detection: underscore id, no evidence overlap with the
    # Instance's realized_files — so evidence-overlap pass (d) cannot help;
    # only the hydrated Spec name can.
    det = _detected("task_manager", "Task Manager")

    # Pre-fix: blind raw index → no match → a duplicate would be minted.
    raw_index = _scanner._build_existing_manifest_index(caps)
    assert _scanner._match_detected_to_existing(det, raw_index, set()) is None

    # Post-fix: hydrated index → matches the existing Instance, no dupe.
    index = _scanner._build_existing_manifest_index(caps, shared_dir=shared)
    match = _scanner._match_detected_to_existing(det, index, set())
    assert match is not None
    assert match["path"] == inst
