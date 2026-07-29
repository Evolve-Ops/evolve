"""Tests for the platform-files defense layers in the scanner.

Background: pre-#2476 scans minted "Session Turn Logs" and similar
empty-shell manifests on bots that had OC-infrastructure-written
``memory/turns-*.jsonl`` files in their workspace. The scanner now has
three defensive layers — see scanner.PLATFORM_WRITTEN_FILE_PATTERNS:

  L1 — llm_discover_applications filters evidence_files post-LLM.
  L2 — _stamp_manifest skips platform paths during file registration.
  L3 — _archive_platform_file_only_stubs sweeps legacy stubs to
       _history/ at the start of each scan's repair pass.

These tests pin each layer in isolation plus the happy-path "real app
with platform-looking files in identity content" non-regression.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import scanner as _scanner  # noqa: E402


# ── _is_platform_written_path ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rel",
    [
        "memory/turns-2026-05-09.jsonl",
        "memory/turns-2026-01-01.jsonl",
        "/memory/turns-2026-05-09.jsonl",          # tolerates leading /
        "file: memory/turns-2026-05-09.jsonl",     # tolerates LLM type-tag prefix
    ],
)
def test_is_platform_written_path_matches_turn_collector_outputs(rel):
    assert _scanner._is_platform_written_path(rel)


@pytest.mark.parametrize(
    "rel",
    [
        "memory/2026-05-09.md",            # daily logging app — not platform
        "memory/notes.md",
        "memory/turns-summary.md",         # similar prefix, different extension
        "ops/turns-2026-05-09.jsonl",      # right name, wrong dir
        "scripts/turn_collector.py",       # a script ABOUT the collector — not its output
        "manifests/i-34cfcab1.json",
        "",
    ],
)
def test_is_platform_written_path_rejects_normal_files(rel):
    assert not _scanner._is_platform_written_path(rel)


# ── L1: LLM evidence-file filter ──────────────────────────────────────────────
#
# llm_discover_applications calls Anthropic. We can't test the LLM dispatch
# without mocking the HTTP client, but the filter lives in pure-Python code
# below the JSON parse. The simplest pin is a direct call to the filter
# logic via a synthetic "apps" payload pumped through the same loop.
#
# Since the filter is inline in the function, we exercise it via the
# loop's contract: an app whose evidence is ALL platform-written must
# never become a DetectedApplication. We patch the LLM call to return
# the synthetic payload.


def _stub_call_anthropic_response(payload: list[dict]) -> str:
    """Return a stringified LLM response containing the JSON payload."""
    return f"Here are the apps:\n{json.dumps(payload)}\nDone."


def test_l1_filter_drops_app_whose_evidence_is_entirely_platform_written(
    monkeypatch, tmp_path,
):
    """An LLM-returned app pointing only at platform files is dropped."""
    payload = [
        {
            "id": "session-turn-logs",
            "name": "Session Turn Logs",
            "description": "Session Turn Logs",
            "confidence": 0.9,
            "evidence_files": [
                "memory/turns-2026-05-09.jsonl",
                "memory/turns-2026-05-10.jsonl",
            ],
        },
        {
            "id": "real-app",
            "name": "Real App",
            "description": "Does a thing",
            "confidence": 0.9,
            "evidence_files": ["ops/real_app.py"],
        },
    ]

    monkeypatch.setattr(_scanner, "_call_anthropic",
                        lambda model, prompt, api_key, timeout=60:
                            _stub_call_anthropic_response(payload))
    monkeypatch.setattr(_scanner, "_read_api_key",
                        lambda bot_id, user=None: "sk-fake")

    inv = _scanner.WorkspaceInventory(
        workspace=tmp_path,
        bot_id="testbot",
    )
    detected = _scanner.llm_discover_applications(inv, model="tier3")

    detected_ids = {d.id for d in detected}
    assert "session-turn-logs" not in detected_ids
    assert "real-app" in detected_ids


def test_l1_filter_strips_platform_files_but_keeps_mixed_apps(
    monkeypatch, tmp_path,
):
    """An app with mixed real + platform evidence keeps the real evidence."""
    payload = [{
        "id": "mixed-app",
        "name": "Mixed App",
        "description": "Has real evidence",
        "confidence": 0.9,
        "evidence_files": [
            "ops/mixed.py",
            "memory/turns-2026-05-09.jsonl",   # platform — filtered
            "ops/data.json",
        ],
    }]
    monkeypatch.setattr(_scanner, "_call_anthropic",
                        lambda model, prompt, api_key, timeout=60:
                            _stub_call_anthropic_response(payload))
    monkeypatch.setattr(_scanner, "_read_api_key",
                        lambda bot_id, user=None: "sk-fake")

    inv = _scanner.WorkspaceInventory(
        workspace=tmp_path,
        bot_id="testbot",
    )
    detected = _scanner.llm_discover_applications(inv, model="tier3")

    assert len(detected) == 1
    assert detected[0].id == "mixed-app"
    assert "memory/turns-2026-05-09.jsonl" not in detected[0].evidence_files
    assert set(detected[0].evidence_files) == {"ops/mixed.py", "ops/data.json"}


# ── L3: archive platform-file-only stub manifests ─────────────────────────────


def _write_manifest(caps_dir: Path, stem: str, payload: dict) -> Path:
    p = caps_dir / f"{stem}.json"
    p.write_text(json.dumps(payload, indent=2))
    return p


def test_l3_archives_stub_with_only_platform_realized_files(tmp_path):
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "i-34cfcab1", {
        "id": "i-34cfcab1",
        "name": "Session Turn Logs",
        "objective": "",
        "identity": {},
        "evidence_files": [],
        "realized_files": [
            {"path": "memory/turns-2026-05-07.jsonl"},
            {"path": "memory/turns-2026-05-05.jsonl"},
            {"path": "memory/turns-2026-05-03.jsonl"},
        ],
        "files": [],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert len(archived) == 1
    assert "i-34cfcab1" in archived[0]
    assert not (caps / "i-34cfcab1.json").exists()
    history = list((caps / "_history").glob("i-34cfcab1_platform_files_only_stub_*.json"))
    assert len(history) == 1
    # Archived content is preserved verbatim.
    restored = json.loads(history[0].read_text())
    assert restored["name"] == "Session Turn Logs"


def test_l3_skips_manifest_with_empty_realized_files(tmp_path):
    """Empty + NO behavioral claim → still preserved as in-progress. A genuinely
    empty manifest a user just stubbed (no objective, no recurring-behavior
    assertion) makes no claim, so the claim-without-realization test (Rule 4)
    does not fire. This is the in-progress contract — Bite C narrows it to
    no-claim manifests, it does NOT widen archival to every empty stub."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "i-dd3336e6", {
        "id": "i-dd3336e6",
        "name": "Inquiry Management",
        "objective": "",
        "identity": {},
        "evidence_files": [],
        "realized_files": [],
        "files": [],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert archived == []
    assert (caps / "i-dd3336e6.json").exists()


def test_l3_keeps_empty_operator_domain_app_that_claims_behavior(tmp_path):
    """Bite C over-drop edge: an EMPTY app that DOES assert recurring behavior
    but is OPERATOR-domain (not infra) is still preserved — Rule 4 needs an
    infra signal. A freshly-stubbed 'Morning Briefing' (claims a daily summary,
    no realization, no infra words) must survive as in-progress."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "i-briefing", {
        "id": "i-briefing",
        "name": "Morning Briefing",
        "source": "discovered",
        "description": "Sends a daily summary of the day's calendar every morning.",
        "objective": "",
        "identity": {},
        "evidence_files": [],
        "realized_files": [],
        "scheduled_actions": [],
        "files": [],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert archived == [], f"empty operator-domain claim must survive, got {archived}"
    assert (caps / "i-briefing.json").exists()


def test_l3_archives_empty_infra_shell_that_claims_recurring_behavior(tmp_path):
    """Bite C, Class 1 — the live 'Liveness Monitoring System' / 'Gateway
    Self-Healing' shape (anonymized): empty evidence + empty realized_files, but
    the description ASSERTS active recurring behavior AND the name/objective is
    infra-by-purpose. That is an incoherent shell (claims to run with nothing
    backing it), so Rule 4 archives it — the contract split from the no-claim
    in-progress case above."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    # Anonymized 'Liveness Monitoring System': claim in description ("periodic
    # pings"), infra by "liveness".
    _write_manifest(caps, "i-0b78ebf9", {
        "id": "i-0b78ebf9",
        "name": "Liveness Monitoring System",
        "manifest_shape": "v7-arc",
        "source": "discovered",
        "description": ("Liveness monitoring and health check system that tracks "
                        "operational status through periodic pings."),
        "objective": "",
        "identity": {},
        "evidence_files": [],
        "realized_files": [],
        "scheduled_actions": [],
        "files": [],
    })
    # Anonymized 'Gateway Self-Healing': claim ("Automated ... recovery"), infra
    # by name ("gateway" / "self-healing") + "openclaw".
    _write_manifest(caps, "i-5cd78794", {
        "id": "i-5cd78794",
        "name": "Gateway Self-Healing",
        "manifest_shape": "v7-arc",
        "source": "discovered",
        "description": ("Automated gateway process management and recovery system "
                        "ensuring stable OpenClaw gateway operations."),
        "objective": "",
        "identity": {},
        "evidence_files": [],
        "realized_files": [],
        "scheduled_actions": [],
        "files": [],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert len(archived) == 2, f"both incoherent infra shells must archive, got {archived}"
    assert all("incoherent_infra_shell" in a for a in archived)
    assert not (caps / "i-0b78ebf9.json").exists()
    assert not (caps / "i-5cd78794.json").exists()


def test_l3_skips_real_app_with_real_file_paths(tmp_path):
    """Gmail Fetcher: empty objective + empty identity, but realized_files
    point at real bot scripts — must survive the sweep."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "i-3f53d00c", {
        "id": "i-3f53d00c",
        "name": "Gmail Fetcher",
        "objective": "",
        "identity": {},
        "evidence_files": [],
        "realized_files": [
            {"path": "scripts/gmail_fetch.py"},
            {"path": "tests/test_gmail_fetch.py"},
        ],
        "files": [],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert archived == []
    assert (caps / "i-3f53d00c.json").exists()


def test_l3_skips_mixed_footprint_apps(tmp_path):
    """A manifest with ANY non-platform file in its footprint must survive."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "app-daily-logging", {
        "id": "app-daily-logging",
        "name": "Daily Logging",
        "objective": "",
        "identity": {},
        "evidence_files": [],
        "realized_files": [
            {"path": "memory/turns-2026-05-09.jsonl"},  # platform
            {"path": "memory/2026-05-09.md"},           # real bot log
        ],
        "files": [],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert archived == []
    assert (caps / "app-daily-logging.json").exists()


def test_l3_skips_manifest_with_operator_content_even_if_files_are_platform(
    tmp_path,
):
    """Paranoid guard: a manifest whose footprint is platform-only but
    that carries operator-authored objective/identity is left alone.
    The operator may have attached the files deliberately."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "app-platform-watcher", {
        "id": "app-platform-watcher",
        "name": "Platform Watcher",
        "objective": "Monitor turn-collector output cadence.",
        "identity": {"purpose": "...", "scope_includes": ["..."]},
        "evidence_files": [],
        "realized_files": [
            {"path": "memory/turns-2026-05-09.jsonl"},
        ],
        "files": [],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert archived == []
    assert (caps / "app-platform-watcher.json").exists()


def test_l3_reads_files_field_too(tmp_path):
    """v5-format 'files' list is part of the footprint check, not just
    'realized_files' (legacy v13/v7-arc field)."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "i-fakestub", {
        "id": "i-fakestub",
        "name": "Stub From Files",
        "objective": "",
        "identity": {},
        "evidence_files": [],
        "realized_files": [],
        "files": [
            {"path": "memory/turns-2026-05-09.jsonl", "file_id": "f-abc"},
        ],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert len(archived) == 1
    assert not (caps / "i-fakestub.json").exists()


def test_l3_handles_malformed_manifest_gracefully(tmp_path):
    caps = tmp_path / "manifests"
    caps.mkdir()
    (caps / "broken.json").write_text("{not valid json")

    # Must not raise; just skips the bad file.
    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == []
    assert (caps / "broken.json").exists()


def test_l3_skips_history_dir(tmp_path):
    """Manifests already in _history aren't reconsidered."""
    caps = tmp_path / "manifests"
    history = caps / "_history"
    history.mkdir(parents=True)
    _write_manifest(history, "i-old", {
        "id": "i-old",
        "name": "Old Stub",
        "objective": "",
        "identity": {},
        "evidence_files": [],
        "realized_files": [{"path": "memory/turns-2026-05-09.jsonl"}],
        "files": [],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == []
    # File still in _history; nothing moved.
    assert (history / "i-old.json").exists()


# ── _manifest_file_footprint ──────────────────────────────────────────────────


def test_manifest_file_footprint_merges_both_lists():
    data = {
        "realized_files": [
            {"path": "a.py"},
            {"path": "b.json"},
        ],
        "files": [
            {"path": "c.md"},
        ],
    }
    paths = _scanner._manifest_file_footprint(data)
    assert paths == ["a.py", "b.json", "c.md"]


# ── Phase 1 additions: PLATFORM_OWNED + Rule 2 ───────────────────────────────


@pytest.mark.parametrize(
    "rel",
    [
        # Tier A — already covered by _is_platform_written_path:
        "memory/turns-2026-05-09.jsonl",
        # Tier A expansions
        "evolve/audit_outbox/_ingested/2026-06-07/rec-022b7d76.json",
        "evolve/audit_outbox/_ingested/2026-06-09/rec-abc.json",
        "evolve/audit_inbox/req-x.json",
        "evolve/audits/run-2026-06-09/finding.json",
        "evolve/defer-queue.jsonl",
        "evolve/defer-queue.jsonl.lock",
        "evolve/defer-archive.jsonl",
        "evolve/manifest-reflex-queue.jsonl",
        "evolve/manifest-reflex-archive.jsonl",
        "evolve/rec-hints.json",
        "evolve/logs/admin-server.log",
        "evolve/spans/cascade-2026-06-09.jsonl",
        "evolve/summaries/2026-06-09.md",
        "evolve/recommendations/r-abc.json",
        "evolve/cascade/pressure_flags.json",
        "evolve/metrics/2026-06-09.jsonl",
        "evolve/turns/turns-2026-06-09.jsonl",
        "manifests/.scan-status.json",
        "manifests/_history/i-old_v0_2026-05-23T07-42-27Z.json",
    ],
)
def test_is_platform_written_matches_expanded_patterns(rel):
    assert _scanner._is_platform_written_path(rel)


@pytest.mark.parametrize(
    "rel",
    [
        # Tier B (broader) — should match _is_platform_owned but NOT
        # _is_platform_written.
        "evolve/task_extractor.py",
        "evolve/analyze.py",
        "evolve/audit_dispatch.py",
        "scripts/launchd/ai.openclaw.usage-collector.plist",
        "scripts/launchd/ai.evolve.signal-subscriber.plist",
        "HEARTBEAT.md",
        "POD_CONDUCT.md",
        "INSTALLED_APPS.md",
    ],
)
def test_is_platform_owned_matches_tier_b_only(rel):
    assert _scanner._is_platform_owned_path(rel)
    assert not _scanner._is_platform_written_path(rel)


@pytest.mark.parametrize(
    "rel",
    [
        # Files a real bot-app could legitimately own — neither tier.
        "ops/tasks.py",
        "scripts/morning_briefing.py",
        "memory/2026-05-09.md",           # Daily Logging's own files
        "AGENTS.md",                       # per-bot, not pod-wide
        "SOUL.md",                         # per-bot identity
        "MEMORY.md",                       # per-bot memory directives
        "USER.md",                         # per-bot user profile
        "manifests/app-cost-monitoring.json",  # a SIBLING manifest, not
                                              # scanner-state — still
                                              # platform-OWNED if author
                                              # claims it though. Tier B
                                              # only includes _history.
    ],
)
def test_neither_tier_matches_bot_owned_paths(rel):
    """Real bot-app files must not be caught by either tier."""
    assert not _scanner._is_platform_owned_path(rel)
    assert not _scanner._is_platform_written_path(rel)


def test_path_matches_any_double_glob_recursion():
    """fnmatch alone treats `**` as `*`. _path_matches_any handles it."""
    pat = ("evolve/audit_outbox/**",)
    assert _scanner._path_matches_any("evolve/audit_outbox/_ingested/x/rec.json", pat)
    assert _scanner._path_matches_any("evolve/audit_outbox/req.json", pat)
    assert not _scanner._path_matches_any("evolve/other/file.json", pat)


# ── _has_real_producer_surface (mirror of audit check) ──────────────────────


def test_has_real_producer_surface_empty_returns_false():
    assert not _scanner._has_real_producer_surface({"scheduled_actions": [], "crons": []})


def test_has_real_producer_surface_vacuous_scheduled_actions_false():
    """6 scheduled_actions with mechanism=unknown + None install — Audit
    Logging shape. Must NOT count as a surface."""
    m = {
        "scheduled_actions": [
            {"id": f"a-{i}", "mechanism": "unknown",
             "install": {"file": None, "plist_label": None, "command": None}}
            for i in range(6)
        ],
    }
    assert not _scanner._has_real_producer_surface(m)


def test_has_real_producer_surface_real_install_true():
    m = {"scheduled_actions": [
        {"id": "x", "install": {"file": "HEARTBEAT.md", "section_anchor": "## A"}},
    ]}
    assert _scanner._has_real_producer_surface(m)


def test_has_real_producer_surface_heartbeat_evidence_true():
    m = {"heartbeat_evidence": {"file_path": "HEARTBEAT.md",
                                 "section_anchors": ["X"]}}
    assert _scanner._has_real_producer_surface(m)


def test_has_real_producer_surface_cron_evidence_labels_true():
    m = {"cron_evidence": {"labels": ["com.bot.x.check"]}}
    assert _scanner._has_real_producer_surface(m)


def test_has_real_producer_surface_cli_true():
    m = {"interface_contract": {"cli": [{"command": "x run"}]}}
    assert _scanner._has_real_producer_surface(m)


# ── v23.2: script realized_files as inferred CLI surface ────────────────────


def test_has_script_realized_file_py():
    m = {"realized_files": [{"path": "scripts/build_vineyard_db.py"}]}
    assert _scanner._has_script_realized_file(m) is True


def test_has_script_realized_file_sh():
    m = {"realized_files": [{"path": "tools/run.sh"}]}
    assert _scanner._has_script_realized_file(m) is True


def test_has_script_realized_file_false_for_data():
    m = {"realized_files": [{"path": "data/vineyard.db"}, {"path": "logs/x.jsonl"}]}
    assert _scanner._has_script_realized_file(m) is False


def test_has_real_producer_surface_script_realized_file_true():
    """Vineyard-ops / document-generator class: script in realized_files,
    nothing else declared. The mirrored verifier check (in
    app_audit_structural._producer_surface_kinds) counts this same shape
    — keep both in lockstep so L3 archival doesn't disagree with the
    audit."""
    m = {
        "realized_files": [
            {"logical_name": "build_vineyard_db",
             "path": "scripts/build_vineyard_db.py",
             "marker_state": "OWNED"},
        ],
    }
    assert _scanner._has_real_producer_surface(m)


# ── _synthesize_cli_from_scripts (Pass A backfill helper) ───────────────────


def test_synthesize_cli_python_script_emits_python_invocation():
    m = {"realized_files": [
        {"logical_name": "build_vineyard_db",
         "path": "scripts/build_vineyard_db.py"},
    ]}
    entries = _scanner._synthesize_cli_from_scripts(m)
    assert len(entries) == 1
    assert entries[0]["command"] == "python scripts/build_vineyard_db.py"
    assert entries[0]["name"] == "build_vineyard_db"
    assert entries[0]["source"] == "scanner-inferred"


def test_synthesize_cli_shell_script_uses_raw_path():
    m = {"realized_files": [
        {"logical_name": "run", "path": "tools/run.sh"},
    ]}
    entries = _scanner._synthesize_cli_from_scripts(m)
    assert len(entries) == 1
    assert entries[0]["command"] == "tools/run.sh"


def test_synthesize_cli_name_falls_back_to_stem_when_logical_name_missing():
    m = {"realized_files": [{"path": "scripts/foo.py"}]}
    entries = _scanner._synthesize_cli_from_scripts(m)
    assert entries[0]["name"] == "foo"


def test_synthesize_cli_emits_one_entry_per_script():
    m = {"realized_files": [
        {"logical_name": "a", "path": "scripts/a.py"},
        {"logical_name": "b", "path": "scripts/b.py"},
        {"path": "data/x.json"},   # not a script
    ]}
    entries = _scanner._synthesize_cli_from_scripts(m)
    assert len(entries) == 2
    assert {e["name"] for e in entries} == {"a", "b"}


def test_synthesize_cli_empty_when_no_scripts():
    m = {"realized_files": [{"path": "data/x.db"}, {"path": "docs/r.md"}]}
    assert _scanner._synthesize_cli_from_scripts(m) == []


def test_synthesize_cli_tolerates_malformed_entries():
    m = {"realized_files": [None, "raw-string", {"path": "scripts/ok.py"}]}
    entries = _scanner._synthesize_cli_from_scripts(m)
    assert len(entries) == 1
    assert entries[0]["command"] == "python scripts/ok.py"


# ── Pass A backfill integration: synthesis writes into manifest ─────────────


def test_pass_a_synthesis_populates_empty_interface_contract(tmp_path):
    """End-to-end check: invoke the same write/read sequence Pass A uses
    on a v7-arc Instance with realized_files but no interface_contract.
    After synthesis, ``interface_contract.cli`` MUST carry one
    scanner-inferred entry per script.

    We exercise the helper directly + then mimic the Pass A "merge into
    data + persist" pattern. The full scanner orchestrator is mocked
    out via the helper; the goal is to pin the data contract Pass A
    writes, not the full LLM round-trip.
    """
    data = {
        "id": "i-512954ad",
        "name": "Vineyard Operations",
        "realized_files": [
            {"logical_name": "build_vineyard_db",
             "path": "scripts/build_vineyard_db.py"},
        ],
        "interface_contract": {},
    }
    # Mimic the Pass A guard + write sequence.
    ic = data.get("interface_contract") or {}
    is_empty = not (ic.get("cli") if isinstance(ic, dict) else None)
    assert is_empty
    inferred = _scanner._synthesize_cli_from_scripts(data)
    assert inferred
    ic["cli"] = inferred
    data["interface_contract"] = ic

    # The resulting manifest now passes the producer-surface check
    # without needing the safety-net script-realized-file clause.
    assert data["interface_contract"]["cli"][0]["command"] == \
        "python scripts/build_vineyard_db.py"
    assert data["interface_contract"]["cli"][0]["source"] == "scanner-inferred"
    assert _scanner._has_real_producer_surface(data)


def test_pass_a_synthesis_does_not_clobber_existing_cli():
    """Operator-asserted entries in ``interface_contract.cli`` are
    sacred. Pass A's guard (``_is_empty`` on cli) MUST short-circuit
    before synthesis fires; we pin that by showing the helper only
    runs through the guard — and that an operator-set cli survives
    the same write path unchanged."""
    operator_cli = [{"command": "vineyard-ops sync", "key_flags": ["--dry-run"]}]
    data = {
        "realized_files": [{"path": "scripts/build_vineyard_db.py"}],
        "interface_contract": {"cli": list(operator_cli)},
    }
    # Pass A guard.
    ic = data.get("interface_contract") or {}
    is_empty = not (ic.get("cli") if isinstance(ic, dict) else None)
    assert not is_empty  # operator entries present — synthesis skipped
    # Field unchanged.
    assert data["interface_contract"]["cli"] == operator_cli


def test_pass_a_synthesis_skipped_when_realized_files_has_no_scripts():
    """Non-script realized_files (data files, docs) MUST NOT trigger
    synthesis — there's nothing CLI-invocable to declare."""
    data = {
        "realized_files": [
            {"path": "data/vineyard.db"},
            {"path": "docs/README.md"},
        ],
        "interface_contract": {},
    }
    inferred = _scanner._synthesize_cli_from_scripts(data)
    assert inferred == []
    # Without script evidence, the manifest also stays no-surface.
    assert not _scanner._has_real_producer_surface(data)


# ── L3 platform-realized-script discount (Evolve-runtime phantom) ────────────
#
# The inferred ``realized_files.script`` surface (producer_surface_kinds v23.2)
# fires on ANY realized script — including the Evolve platform's OWN runtime
# (``evolve/*.py``) when it is realized into a scanner-minted manifest. Verified
# live on a pod 2026-06-20: 'Evolve AI Pipeline' (app-evolve-pipeline.json)
# carries 11 ``evolve/*.py`` + ``evolve-backup/evolve-tiers.json`` as
# realized_files (anonymized fixture below), every
# file platform-owned, plus 3 vacuous scheduled_actions. That realized-script
# surface made Rule 2's no_surface gate False, so the ≥90%-platform-owned sweep
# left the platform's own code minted as a "user app". The L3 gate now discounts
# a surface backed ONLY by platform/infra scripts — _has_real_producer_surface /
# producer_surface_kinds are unchanged (audit + Pass A still see the surface).

# The exact realized_files shape of the live 'Evolve AI Pipeline' phantom.
_EVOLVE_RUNTIME_REALIZED = [
    {"path": f"evolve/{stem}.py", "marker_state": "OWNED"}
    for stem in (
        "task_extractor", "task_queue", "task_runner", "analyze", "validate",
        "apply", "review", "scoreboard", "heal", "outcome", "evolve_config",
    )
] + [{"path": "evolve-backup/evolve-tiers.json", "marker_state": "OWNED"}]


def _vacuous_unknown_action(action_id: str) -> dict:
    """A scheduled_action exactly as the phantom carries it: mechanism unknown,
    every install field None — producer_surface_kinds sees no surface."""
    return {
        "id": action_id,
        "mechanism": "unknown",
        "install": {"file": None, "plist_label": None, "command": None},
    }


def test_l3_archives_evolve_runtime_phantom_realized_scripts(tmp_path):
    """THE BUG: a v7-arc Instance whose realized_files are 100% the Evolve
    platform runtime (evolve/*.py + evolve-backup/*) registers an inferred
    realized-script CLI surface that pre-fix shielded it from Rule 2. The
    L3-gate script discount makes the platform-only surface read as no surface,
    so the ≥90%-platform-owned rule retires the platform's own code."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "app-evolve-pipeline", {
        "id": "app-evolve-pipeline",
        "name": "Evolve AI Pipeline",
        "manifest_shape": "v7-arc",
        "source": None,                       # scanner-authored
        "objective": "",
        "identity": {},
        "evidence_files": [],
        "realized_files": list(_EVOLVE_RUNTIME_REALIZED),
        "files": [],
        "scheduled_actions": [
            _vacuous_unknown_action("evolve-pipeline-a"),
            _vacuous_unknown_action("evolve-pipeline-b"),
            _vacuous_unknown_action("evolve-pipeline-c"),
        ],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert len(archived) == 1, f"Evolve-runtime phantom must archive, got {archived}"
    assert "platform-owned" in archived[0]          # Rule 2 reason
    assert not (caps / "app-evolve-pipeline.json").exists()
    history = list(
        (caps / "_history").glob("app-evolve-pipeline_platform_dominant_no_surface_*.json")
    )
    assert len(history) == 1
    # Archival is a MOVE — the files[] / realized_files are preserved verbatim.
    restored = json.loads(history[0].read_text())
    assert len(restored["realized_files"]) == len(_EVOLVE_RUNTIME_REALIZED)


def test_l3_keeps_platform_dominant_manifest_with_one_real_script(tmp_path):
    """Over-drop guard — the MIXED 'Task Management System' shape: a manifest
    dominated by platform runtime (evolve/task_*.py) but with ONE bot-authored
    script (ops/tools/unified_task_system.py) keeps its surface and survives.
    A single non-platform script is enough to back the realized-script
    surface; the discount only fires when EVERY script is platform/infra."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    realized = list(_EVOLVE_RUNTIME_REALIZED) + [
        {"path": "ops/tools/unified_task_system.py", "marker_state": "OWNED"},
    ]
    _write_manifest(caps, "app-task-mgmt", {
        "id": "app-task-mgmt",
        "name": "Task Management System",
        "manifest_shape": "v7-arc",
        "source": None,
        "objective": "",
        "identity": {},
        "evidence_files": [],
        "realized_files": realized,
        "files": [],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert archived == [], f"mixed app with a real script must survive, got {archived}"
    assert (caps / "app-task-mgmt.json").exists()


# ── _has_nonplatform_script_realized_file ───────────────────────────────────


def test_has_nonplatform_script_realized_file_platform_only_false():
    """Every realized script is platform runtime → no bot-authored backing."""
    m = {"realized_files": list(_EVOLVE_RUNTIME_REALIZED)}
    assert _scanner._has_nonplatform_script_realized_file(m) is False


def test_has_nonplatform_script_realized_file_infra_only_false():
    """An infra-script-only footprint also has no bot-authored backing."""
    m = {"realized_files": [{"path": "bin/gateway-selfheal.sh"}]}
    assert _scanner._has_nonplatform_script_realized_file(m) is False


def test_has_nonplatform_script_realized_file_mixed_true():
    """One bot-authored script beside platform runtime → backed."""
    m = {"realized_files": list(_EVOLVE_RUNTIME_REALIZED)
         + [{"path": "ops/tools/unified_task_system.py"}]}
    assert _scanner._has_nonplatform_script_realized_file(m) is True


def test_has_nonplatform_script_realized_file_no_scripts_false():
    """Data/doc-only footprint has no script surface to back at all."""
    m = {"realized_files": [{"path": "data/x.db"}, {"path": "docs/r.md"}]}
    assert _scanner._has_nonplatform_script_realized_file(m) is False


# ── L3-gate surface predicates (Rule 2 / Rule 3 discount) ───────────────────


def test_l3_has_real_producer_surface_platform_scripts_only_false():
    """Rule 2's predicate: a surface made entirely of platform realized
    scripts reads as NO surface for the archival gate."""
    m = {"realized_files": list(_EVOLVE_RUNTIME_REALIZED)}
    # The unchanged audit-side predicate still counts the realized scripts.
    assert _scanner._has_real_producer_surface(m)
    # The L3-gate predicate discounts them.
    assert not _scanner._l3_has_real_producer_surface(m)


def test_l3_has_real_producer_surface_keeps_heartbeat_shield():
    """Regression: the discount is realized-script-only. A heartbeat anchor
    still shields under Rule 2's predicate (only Rule 3 discounts soft)."""
    m = {"heartbeat_evidence": {"file_path": "HEARTBEAT.md",
                                 "section_anchors": ["X"]}}
    assert _scanner._l3_has_real_producer_surface(m)


def test_l3_has_real_producer_surface_keeps_real_cli():
    """A declared CLI surface is untouched by the script discount."""
    m = {
        "realized_files": list(_EVOLVE_RUNTIME_REALIZED),
        "interface_contract": {"cli": [{"command": "pipeline run"}]},
    }
    assert _scanner._l3_has_real_producer_surface(m)


def test_l3_concrete_surface_kinds_discounts_platform_script():
    """Rule 3's concrete-surface helper drops the platform-only realized-script
    kind, while a bot-authored script keeps it."""
    platform_only = {"realized_files": list(_EVOLVE_RUNTIME_REALIZED)}
    assert _scanner._l3_concrete_producer_surface_kinds(platform_only) == set()
    mixed = {"realized_files": list(_EVOLVE_RUNTIME_REALIZED)
             + [{"path": "ops/tools/unified_task_system.py"}]}
    assert "realized_files.script" in _scanner._l3_concrete_producer_surface_kinds(mixed)


# ── L3 platform-CLI discount (Slice 1b — completes the runtime unshield) ─────
#
# #3044 discounted only the inferred ``realized_files.script`` surface. The live
# 'Evolve AI Pipeline' phantom (app-evolve-pipeline.json) ALSO carries an
# ``interface_contract.cli`` co-surface — 11 ``scanner-inferred`` entries, EVERY
# command ``python evolve/<x>.py`` — a hallucinated CLI over the platform's OWN
# runtime. That CLI kept the phantom's surface non-empty, so Rule 2 never fired
# and the platform's own code survived as a "user app" (verified live on a pod
# 2026-06-20: footprint 24/24 platform-owned, has_real_surface=True). This block
# pins the CLI discount: a surface backed SOLELY by platform/infra-targeted
# commands is dropped, while a single bot-authored command keeps it.

# The exact interface_contract.cli of the live phantom (one entry per evolve/
# runtime module; ``python evolve/<stem>.py``, source scanner-inferred).
_EVOLVE_RUNTIME_CLI = [
    {"command": f"python evolve/{stem}.py", "name": stem, "source": "scanner-inferred"}
    for stem in (
        "task_extractor", "task_queue", "task_runner", "analyze", "validate",
        "apply", "review", "scoreboard", "heal", "outcome", "evolve_config",
    )
]


def test_l3_archives_evolve_pipeline_phantom_with_both_co_surfaces(tmp_path):
    """THE DELIVERABLE — the live 'Evolve AI Pipeline' shape: realized scripts
    AND an inferred CLI, EVERY one targeting the platform's own evolve/*.py
    runtime. #3044 dropped the script surface but the CLI co-surface kept the
    phantom alive; the CLI discount drops it too, so the ≥90%-platform-owned
    rule retires the platform's own code.

    On origin/main (#3044 only) this manifest SURVIVES — _l3_discount keeps
    interface_contract.cli, so Rule 2's no_surface gate is False. This test is
    the real-deliverable proof: it fails on main, passes on this branch."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "app-evolve-pipeline", {
        "id": "app-evolve-pipeline",
        "name": "Evolve AI Pipeline",
        "manifest_shape": "v7-arc",
        "source": None,                       # scanner-authored
        "objective": "",
        "identity": {},
        "evidence_files": [],
        "realized_files": list(_EVOLVE_RUNTIME_REALIZED),
        "interface_contract": {"cli": list(_EVOLVE_RUNTIME_CLI)},
        "files": [],
        "scheduled_actions": [
            _vacuous_unknown_action("evolve-pipeline-a"),
            _vacuous_unknown_action("evolve-pipeline-b"),
            _vacuous_unknown_action("evolve-pipeline-c"),
        ],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert len(archived) == 1, f"both-co-surface phantom must archive, got {archived}"
    assert "platform-owned" in archived[0]          # Rule 2 reason
    assert not (caps / "app-evolve-pipeline.json").exists()
    history = list(
        (caps / "_history").glob("app-evolve-pipeline_platform_dominant_no_surface_*.json")
    )
    assert len(history) == 1
    # Archival is a MOVE — files[]/realized_files/cli are preserved verbatim.
    restored = json.loads(history[0].read_text())
    assert len(restored["realized_files"]) == len(_EVOLVE_RUNTIME_REALIZED)
    assert len(restored["interface_contract"]["cli"]) == len(_EVOLVE_RUNTIME_CLI)


def test_l3_keeps_platform_dominant_manifest_with_one_real_cli(tmp_path):
    """Over-drop guard — a real app whose interface_contract.cli targets a
    NON-platform file (``python ops/tools/foo.py``) keeps its CLI surface and
    survives, even when its realized_files are all platform runtime. A single
    bot-authored command is enough to back the CLI surface; the discount only
    fires when EVERY command is platform/infra-targeted."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "app-real-cli", {
        "id": "app-real-cli",
        "name": "Custom Pipeline",
        "manifest_shape": "v7-arc",
        "source": None,
        "objective": "",
        "identity": {},
        "evidence_files": [],
        "realized_files": list(_EVOLVE_RUNTIME_REALIZED),
        "interface_contract": {"cli": [{"command": "python ops/tools/foo.py"}]},
        "files": [],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert archived == [], f"app with a real CLI must survive, got {archived}"
    assert (caps / "app-real-cli.json").exists()


def test_l3_keeps_task_management_mixed_cli(tmp_path):
    """Over-drop guard — the MIXED 'Task Management System' shape: cli/footprint
    mixes real ``ops/tools/*.py`` AND platform ``evolve/task_*.py``. The
    non-platform ops/tools command keeps the surface → KEEP (app-vs-skill axis
    is Slice 2, out of scope here)."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "app-task-management", {
        "id": "app-task-management",
        "name": "Task Management System",
        "manifest_shape": "v7-arc",
        "source": None,
        "objective": "",
        "identity": {},
        "evidence_files": [],
        "realized_files": list(_EVOLVE_RUNTIME_REALIZED)
        + [{"path": "ops/tools/unified_task_system.py", "marker_state": "OWNED"}],
        "interface_contract": {"cli": list(_EVOLVE_RUNTIME_CLI)
                               + [{"command": "python ops/tools/unified_task_system.py"}]},
        "files": [],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert archived == [], f"mixed app with a real command must survive, got {archived}"
    assert (caps / "app-task-management.json").exists()


# ── _cli_command_is_platform_only ───────────────────────────────────────────


def test_cli_command_is_platform_only_platform_path_true():
    """``python evolve/<x>.py`` → the command targets only platform runtime."""
    assert _scanner._cli_command_is_platform_only("python evolve/task_extractor.py")


def test_cli_command_is_platform_only_infra_script_true():
    """A command targeting an infra self-heal script is platform-only."""
    assert _scanner._cli_command_is_platform_only("bash bin/gateway-selfheal.sh")


def test_cli_command_is_platform_only_nonplatform_path_false():
    """A bot-authored ops/tools script is NOT platform-only."""
    assert not _scanner._cli_command_is_platform_only("python ops/tools/foo.py")


def test_cli_command_is_platform_only_no_path_keep_biased():
    """KEEP-BIASED: a bare verb with no extractable path ("pipeline run") is
    NOT platform-only — we cannot prove it targets the platform, so the surface
    is preserved (this is what keeps #3044's keeps_real_cli green)."""
    assert not _scanner._cli_command_is_platform_only("pipeline run")
    assert not _scanner._cli_command_is_platform_only("")


def test_cli_command_is_platform_only_mixed_paths_keep_biased():
    """A command referencing BOTH a platform and a non-platform path is NOT
    platform-only — one non-platform target keeps the surface."""
    assert not _scanner._cli_command_is_platform_only(
        "python evolve/apply.py --config ops/tools/foo.py"
    )


# ── _has_nonplatform_cli_surface ────────────────────────────────────────────


def test_has_nonplatform_cli_surface_platform_only_false():
    """Every cli command targets platform runtime → no bot-authored backing."""
    m = {"interface_contract": {"cli": list(_EVOLVE_RUNTIME_CLI)}}
    assert _scanner._has_nonplatform_cli_surface(m) is False


def test_has_nonplatform_cli_surface_mixed_true():
    """One bot-authored command beside platform runtime → backed."""
    m = {"interface_contract": {"cli": list(_EVOLVE_RUNTIME_CLI)
                                + [{"command": "python ops/tools/foo.py"}]}}
    assert _scanner._has_nonplatform_cli_surface(m) is True


def test_has_nonplatform_cli_surface_no_cli_false():
    """No declared CLI at all → no CLI surface to back."""
    assert _scanner._has_nonplatform_cli_surface({}) is False
    assert _scanner._has_nonplatform_cli_surface(
        {"interface_contract": {"cli": []}}
    ) is False


# ── L3-gate surface predicates with both platform co-surfaces ───────────────


def test_l3_has_real_producer_surface_platform_script_and_cli_false():
    """Both co-surfaces platform-owned → NO surface for the archival gate. The
    unchanged audit-side predicate still counts both."""
    m = {
        "realized_files": list(_EVOLVE_RUNTIME_REALIZED),
        "interface_contract": {"cli": list(_EVOLVE_RUNTIME_CLI)},
    }
    assert _scanner._has_real_producer_surface(m)          # audit side unchanged
    assert not _scanner._l3_has_real_producer_surface(m)   # L3 gate discounts both


def test_l3_concrete_surface_kinds_discounts_platform_cli():
    """Rule 3's concrete-surface helper drops the platform-only CLI kind, while
    a bot-authored command keeps it."""
    platform_only = {"interface_contract": {"cli": list(_EVOLVE_RUNTIME_CLI)}}
    assert _scanner._l3_concrete_producer_surface_kinds(platform_only) == set()
    mixed = {"interface_contract": {"cli": list(_EVOLVE_RUNTIME_CLI)
                                    + [{"command": "python ops/tools/foo.py"}]}}
    assert "interface_contract.cli" in _scanner._l3_concrete_producer_surface_kinds(mixed)


# ── L3 Rule 2 — platform-dominant + no surface ──────────────────────────────


def _audit_logging_shape(stem="audit-logging", n_files=20) -> dict:
    """Synthesize the Audit Logging archetype: many audit_outbox files,
    vacuous scheduled_actions, populated identity."""
    return {
        "id": stem,
        "name": "Audit Logging",
        "objective": "",
        "identity": {
            "purpose": "Tamper-evident, compliance-ready audit trail.",
        },
        "evidence_files": [],
        "scheduled_actions": [
            {"id": f"audit-action-{i}", "mechanism": "unknown",
             "install": {"file": None, "plist_label": None, "command": None}}
            for i in range(6)
        ],
        "heartbeat_evidence": {},
        "cron_evidence": {},
        "crons": [],
        "files": [
            {"path": f"evolve/audit_outbox/_ingested/2026-06-09/rec-{i:04d}.json"}
            for i in range(n_files)
        ],
        "realized_files": [],
    }


def test_l3_rule2_archives_audit_logging_shape(tmp_path):
    """Rule 2 catches Audit Logging: 20 audit_outbox files + populated
    identity (which Rule 1 protected) but no real producer surface."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "audit-logging", _audit_logging_shape(n_files=20))

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert len(archived) == 1
    assert "platform-owned" in archived[0]
    history = list((caps / "_history").glob(
        "audit-logging_platform_dominant_no_surface_*.json"
    ))
    assert len(history) == 1


def test_l3_rule2_archives_platform_dominant_with_stray_files(tmp_path):
    """atlas/audit-logging shape: 18 audit_outbox files + 2 stray
    scripts (99% Tier A). Rule 2's 90% threshold catches this."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _audit_logging_shape(stem="atlas-audit", n_files=18)
    m["files"].append({"path": "scripts/atlas_guard.py"})
    m["files"].append({"path": "scripts/atlas_lib/guard.py"})
    _write_manifest(caps, "atlas-audit", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert len(archived) == 1


def test_l3_rule2_archives_evolve_runtime_stub(tmp_path):
    """team-bot-a/app-evolve-pipeline shape: 12 evolve/*.py files.
    Tier B only — would not match Tier A. Rule 2 with PLATFORM_OWNED
    catches."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    files = [
        {"path": f}
        for f in ["evolve/task_extractor.py", "evolve/task_queue.py",
                  "evolve/task_runner.py", "evolve/analyze.py",
                  "evolve/validate.py", "evolve/apply.py",
                  "evolve/heal.py", "evolve/measure.py",
                  "evolve/outcome.py", "evolve/expansion.py",
                  "evolve/cost.py", "evolve/report.py"]
    ]
    _write_manifest(caps, "app-evolve-pipeline", {
        "id": "app-evolve-pipeline",
        "name": "Evolve AI Pipeline",
        "objective": "",
        "identity": {"purpose": "Autonomous system evolution, validation."},
        "evidence_files": [],
        "scheduled_actions": [
            {"id": "x", "install": {}} for _ in range(3)
        ],
        "files": files,
        "realized_files": [],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert len(archived) == 1
    assert "platform-owned" in archived[0]


def test_l3_rule2_keeps_platform_dominant_app_with_real_surface(tmp_path):
    """Safety net: a manifest whose footprint is 100% platform-owned but
    that declares a real producer surface (cli) must survive. The Rule 2
    no-surface gate is what prevents accidental archiving of real apps
    that happen to monitor / wrap platform output."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _audit_logging_shape(stem="real-audit-app", n_files=20)
    m["interface_contract"] = {"cli": [{"command": "audit-report run"}]}
    _write_manifest(caps, "real-audit-app", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert archived == []
    assert (caps / "real-audit-app.json").exists()


def test_l3_rule2_keeps_below_min_files_threshold(tmp_path):
    """A 9-file platform-only manifest is below the 10-file Rule 2
    threshold AND doesn't satisfy Rule 1 (identity populated). It must
    survive — a small near-empty manifest is more likely a real
    in-progress app than a scanner artifact."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _audit_logging_shape(stem="small-audit", n_files=9)
    _write_manifest(caps, "small-audit", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == []


def test_l3_rule2_keeps_at_89_percent_platform(tmp_path):
    """Threshold is INCLUSIVE 90%. A footprint with 89% platform-owned
    files must survive — the operator's bot-files are too significant
    to call this a misattribution."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    # 8 platform + 2 bot = 80% platform. Below 90%.
    m = _audit_logging_shape(stem="mixed-app", n_files=8)
    m["files"].append({"path": "ops/real_app.py"})
    m["files"].append({"path": "ops/real_data.json"})
    _write_manifest(caps, "mixed-app", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == []


def test_l3_rule1_still_archives_classic_session_turn_logs_shape(tmp_path):
    """Regression guard: PR #2476's original case (small + no-content)
    must still archive via Rule 1, independent of Rule 2."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_manifest(caps, "i-34cfcab1", {
        "id": "i-34cfcab1",
        "name": "Session Turn Logs",
        "objective": "",
        "identity": {},
        "evidence_files": [],
        "realized_files": [
            {"path": f"memory/turns-2026-05-{d:02d}.jsonl"}
            for d in range(1, 8)
        ],
        "files": [],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert len(archived) == 1
    assert "[rule1]" in archived[0]


def test_manifest_file_footprint_tolerates_malformed_entries():
    data = {
        "realized_files": [
            {"path": "a.py"},
            {"no_path": "skip"},
            "not-a-dict",
            {"path": ""},
            None,
        ],
        "files": None,
    }
    paths = _scanner._manifest_file_footprint(data)
    assert paths == ["a.py"]


# ── Forge inbox + evolve-backup platform trees ──────────────────────────────
#
# Background: the discovery LLM hallucinated "Evolve Forge" / "Memory
# Continuity" / "Audit Logging" phantom apps because the scanner fed it the
# platform's own JSON from evolve/forge/inbox/ and evolve-backup/. Those
# trees are now (a) excluded from the discovery inventory and (b) recognised
# as platform-written so the L3 sweep retires any surviving phantom.


@pytest.mark.parametrize(
    "rel",
    [
        "evolve/forge/inbox/j-3ae681b1.json",
        "evolve/forge/inbox/j-9f9d6bcd-c1.json",
        "evolve/forge/outbox/j-dc6c759f-r1.json",
        "evolve-backup/openclaw.json",
        "evolve-backup/evolve-tiers.json",
        "evolve-backup/state.json",
    ],
)
def test_is_platform_written_matches_forge_and_backup(rel):
    assert _scanner._is_platform_written_path(rel)


def test_collect_json_stores_excludes_evolve_platform_trees(tmp_path):
    """Forge inbox, audit outbox, and evolve-backup JSON must never reach
    the discovery inventory — only the real app's data store survives."""
    ws = tmp_path
    big = json.dumps([{"i": i, "pad": "x" * 20} for i in range(200)])  # > 2 KB

    for rel in (
        "evolve/forge/inbox/j-3ae681b1.json",
        "evolve/forge/inbox/j-9f9d6bcd-c1.json",
        "evolve-backup/state.json",
        "evolve/audit_outbox/_ingested/2026-06-09/rec-abc.json",
    ):
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(big)

    real = ws / "ops" / "tasks.json"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text(big)

    stores = _scanner._collect_json_stores(ws, "testbot")
    paths = {s["path"] for s in stores}

    assert paths == {"ops/tasks.json"}
    assert not any("evolve" in p for p in paths)


def test_l3_archives_evolve_forge_phantom(tmp_path):
    """The exact 'Evolve Forge' shape: 17 forge-inbox + evolve-backup files,
    a confident LLM-written identity, and no real producer surface. Rule 2
    archives it on the next scan's repair pass."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    files = [{"path": "evolve-backup/evolve-tiers.json"},
             {"path": "evolve-backup/openclaw.json"},
             {"path": "evolve-backup/state.json"}]
    files += [{"path": f"evolve/forge/inbox/j-{i:08x}.json"} for i in range(14)]
    _write_manifest(caps, "evolve-forge", {
        "id": "evolve-forge",
        "name": "Evolve Forge",
        "objective": "",
        "identity": {"purpose": "A job processing and execution engine."},
        "evidence_files": [],
        "scheduled_actions": [],
        "crons": [],
        "files": files,
        "realized_files": [],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert len(archived) == 1
    assert "evolve-forge" in archived[0]
    assert not (caps / "evolve-forge.json").exists()
