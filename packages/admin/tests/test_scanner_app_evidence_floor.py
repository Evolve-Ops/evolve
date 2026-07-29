"""App-evidence floor — stop minting infra/system/skill/zero-evidence as apps.

Background (operator-reported, live on a production bot): an app scan minted 20
"applications", ~11 of them false positives — Evolve platform infra scripts
(gateway/watchdog self-heal), OC system functions (session startup, memory
persistence), a bare skill config (Google OAuth), the manifest store itself,
and several zero-evidence clusters. #2705 only excluded ``evolve/`` directories;
it never touched infra *scripts*, identity/memory files, skill configs, or
zero-evidence clusters.

This pins the single principled abstraction — the **app-evidence floor**
(:func:`scanner._app_evidence_files`) — and the two surfaces it routes through:

  * Phase-2 discovery: a cluster with zero app-evidence is never minted
    (drop), UNLESS it is a recurring-behavior app matching a scheduled-action
    candidate (whose surface materializes in Phase 4).
  * L3 reconcile (RULE 3): a scanner-minted manifest already on disk whose
    evidence is entirely a #2705 non-app class is archived to ``_history/``.

The regression guard (legit apps survive) is as load-bearing as the drops:
every legit app from atlas's scan — including Communication Hub, which keeps
its Slack config because bot-authored orchestration rides on top — must remain.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import scanner as _scanner  # noqa: E402


# ════════════════════════════════════════════════════════════════════════════
# Unit: infra-script basename detection (fixes the :1315 empty-content bug)
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "path",
    [
        "gateway-selfheal.sh",
        "bin/gateway-selfheal.sh",
        "sentry_ping.sh",                  # underscore normalizes to hyphen
        "bin/health_ping.sh",
        "liveness-ping.sh",
        "log-trimmer.py",
        "scripts/turn-collector.py",
        "repo-puller.sh",
        "context-prune.sh",
        "myservice-selfheal.sh",           # -selfheal suffix (kept)
        "file: bin/gateway-selfheal.sh",   # tolerates LLM type-tag prefix
    ],
)
def test_is_infra_script_path_matches_infra(path):
    assert _scanner._is_infra_script_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "ops/task_manager.py",
        "ops/system_monitor.py",           # contains "monitor" — NOT infra
        "scripts/report_generator.py",     # contains "report" — NOT infra
        "ranch/ranch_ops.py",
        "scripts/daily_briefing.py",
        "legacy-scripts/communication_hub.py",
        "data/properties.json",
        "scripts/backup_review.py",        # "backup" as PREFIX, not -backup stem
        "",
    ],
)
def test_is_infra_script_path_rejects_real_app_scripts(path):
    assert not _scanner._is_infra_script_path(path)


def test_is_infrastructure_script_empty_content_now_catches_by_basename():
    """The :1315 bug: ``_is_infrastructure_script("", path)`` always returned
    False, so gateway-selfheal.sh reached the LLM. The basename check now
    fires even with empty content."""
    assert _scanner._is_infrastructure_script("", "bin/gateway-selfheal.sh")
    assert _scanner._is_infrastructure_script("", "bin/health_ping.sh")
    # Real app script with empty content is still not infra.
    assert not _scanner._is_infrastructure_script("", "ops/task_manager.py")


def test_is_infrastructure_script_content_signal_still_works():
    """Custom-named infra is still caught by its content (INFRA_SIGNALS)."""
    assert _scanner._is_infrastructure_script(
        "#!/bin/sh\nlaunchctl kickstart gateway\ngit push", "ops/weird_name.sh"
    )


# ════════════════════════════════════════════════════════════════════════════
# Unit: OC identity / manifest-store / skill-config / orchestration classifiers
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "path",
    [
        "AGENTS.md",
        "AGENTS.md#Memory",                # section anchor stripped
        "AGENTS.md#Session Startup",
        "MEMORY.md",
        "SOUL.md",
        "USER.md",
        "HEARTBEAT.md",
        "POD_CONDUCT.md",
        "INSTALLED_APPS.md",
        "openclaw.json",
    ],
)
def test_is_oc_identity_system_file_matches(path):
    assert _scanner._is_oc_identity_system_file(path)


@pytest.mark.parametrize(
    "path",
    ["ops/tasks.json", "memory/2026-05-09.md", "ranch/herd.json", "notes.md"],
)
def test_is_oc_identity_system_file_rejects(path):
    assert not _scanner._is_oc_identity_system_file(path)


@pytest.mark.parametrize(
    "path",
    ["manifests/i-34cfcab1.json", "manifests/app-cost.json", "/manifests/x.json"],
)
def test_is_manifest_store_path_matches(path):
    assert _scanner._is_manifest_store_path(path)


@pytest.mark.parametrize(
    "path",
    ["ops/data.json", "ranch/manifests_helper.py", "data/manifest.json"],
)
def test_is_manifest_store_path_rejects(path):
    # "manifests" must be the FIRST path component, not a substring.
    assert not _scanner._is_manifest_store_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "integrations/google-oauth.json",
        "config/slack-webhook.json",
        "creds/credentials.json",
        "auth/client_secret.json",
        "service-account.yaml",
    ],
)
def test_is_bare_skill_config_matches(path):
    assert _scanner._is_bare_skill_config(path)


@pytest.mark.parametrize(
    "path",
    [
        "ops/tasks.json",          # generic data file — not a credential
        "data/tokens.json",        # "tokens" is NOT in the credential token set
        "config/settings.json",
        "scripts/oauth_handler.py",  # a SCRIPT, not a config (orchestration)
    ],
)
def test_is_bare_skill_config_rejects(path):
    assert not _scanner._is_bare_skill_config(path)


def test_is_orchestration_script_distinguishes_bot_code_from_infra():
    assert _scanner._is_orchestration_script("legacy-scripts/communication_hub.py")
    assert _scanner._is_orchestration_script("ops/task_manager.py")
    assert not _scanner._is_orchestration_script("bin/gateway-selfheal.sh")
    assert not _scanner._is_orchestration_script("config/slack-webhook.json")


# ════════════════════════════════════════════════════════════════════════════
# Unit: _app_evidence_files — the floor itself
# ════════════════════════════════════════════════════════════════════════════


def test_app_evidence_floor_drops_each_nonapp_class():
    assert _scanner._app_evidence_files(["bin/gateway-selfheal.sh"]) == []
    assert _scanner._app_evidence_files(["bin/health_ping.sh"]) == []
    assert _scanner._app_evidence_files(["AGENTS.md#Memory"]) == []
    assert _scanner._app_evidence_files(["MEMORY.md"]) == []
    assert _scanner._app_evidence_files(["manifests/i-abc.json"]) == []
    assert _scanner._app_evidence_files(["integrations/google-oauth.json"]) == []
    assert _scanner._app_evidence_files([]) == []
    assert _scanner._app_evidence_files(["memory/turns-2026-05-09.jsonl"]) == []


def test_app_evidence_floor_keeps_real_files():
    assert _scanner._app_evidence_files(["ops/task_manager.py"]) == ["ops/task_manager.py"]
    assert _scanner._app_evidence_files(
        ["ops/real.py", "bin/gateway-selfheal.sh", "AGENTS.md"]
    ) == ["ops/real.py"]


def test_app_evidence_floor_skill_config_kept_when_orchestration_present():
    """Doctrine: a skill config becomes app evidence when bot-authored
    orchestration rides on top (Communication Hub keeps its Slack config)."""
    ev = _scanner._app_evidence_files(
        ["legacy-scripts/communication_hub.py", "config/slack-webhook.json"]
    )
    assert "legacy-scripts/communication_hub.py" in ev
    assert "config/slack-webhook.json" in ev


def test_app_evidence_floor_skill_config_dropped_without_orchestration():
    assert _scanner._app_evidence_files(["integrations/google-oauth.json"]) == []


def test_app_evidence_floor_is_order_independent():
    a = _scanner._app_evidence_files(
        ["config/slack-webhook.json", "legacy-scripts/communication_hub.py"]
    )
    b = _scanner._app_evidence_files(
        ["legacy-scripts/communication_hub.py", "config/slack-webhook.json"]
    )
    assert set(a) == set(b)


# ════════════════════════════════════════════════════════════════════════════
# Integration: Phase-2 LLM discovery — atlas's shape (DROP 11, KEEP the rest)
# ════════════════════════════════════════════════════════════════════════════


def _stub_response(payload: list[dict]) -> str:
    return f"Here are the apps:\n{json.dumps(payload)}\nDone."


# The 11 false-positive classes atlas's scan minted.
_FALSE_POSITIVES = [
    ("system-health-monitoring", "System Health Monitoring",
     ["bin/gateway-selfheal.sh", "bin/health_ping.sh"]),
    ("gateway-management", "Gateway Management", ["bin/gateway-selfheal.sh"]),
    ("watchdog-monitoring-system", "Watchdog Monitoring System", []),     # zero evidence
    ("gateway-self-healing", "Gateway Self-Healing", []),             # zero evidence
    ("infra-health-monitoring", "Infrastructure Health Monitoring",
     ["bin/health_ping.sh"]),
    ("session-startup", "Session Startup", ["AGENTS.md#Session Startup"]),
    ("persistent-memory-system", "Persistent Memory System", ["AGENTS.md#Memory"]),
    ("memory-persistence", "Memory Persistence", ["MEMORY.md"]),
    ("google-services-integration", "Google Services Integration",
     ["integrations/google-oauth.json"]),
    ("infra-manifest-tracking", "Infrastructure Manifest Tracking",
     ["manifests/i-34cfcab1.json"]),
    # A second gateway dupe with zero evidence — exercises the floor on a
    # confident phantom name.
    ("gateway-watcher", "Gateway Watcher", []),
]

# The legit apps that MUST survive (regression guard).
_LEGIT_APPS = [
    ("property-management", "Property Management",
     ["ops/property_manager.py", "data/properties.json"]),
    ("document-generator", "Document Generator", ["scripts/doc_generator.py"]),
    ("smart-budget-manager", "Smart Budget Manager",
     ["budget/budget_manager.py", "budget/transactions.json"]),
    ("task-management", "Task Management",
     ["ops/tasks/task_system.py", "ops/tasks/tasks.json"]),
    ("daily-operations-briefing", "Daily Operations Briefing",
     ["scripts/daily_briefing.py"]),
    ("stakeholder-workspace-manager", "Stakeholder Workspace Manager",
     ["ops/stakeholder_workspace.py"]),
    ("dropbox-synchronization", "Dropbox Synchronization",
     ["sync/dropbox_sync.py", "sync/dropbox-oauth.json"]),  # config + orchestration
    ("vineyard-operations", "Vineyard Operations",
     ["scripts/build_vineyard_db.py", "data/vineyard.db"]),
    ("ranch-operations-hub", "Ranch Operations Hub",
     ["ranch/ranch_ops.py", "ranch/herd.json"]),
    ("communication-messaging-hub", "Communication & Messaging Hub",
     ["legacy-scripts/communication_hub.py", "config/slack-webhook.json"]),
]


def _atlas_inventory(tmp_path: Path) -> "_scanner.WorkspaceInventory":
    inv = _scanner.WorkspaceInventory(workspace=tmp_path, bot_id="atlas")
    # One schedule-hinted candidate so the "Protein Reminder" behavior app is
    # exempt from the floor (its surface materializes in Phase 4).
    inv.scheduled_action_candidates = [
        {
            "file_path": "HEARTBEAT.md",
            "heading": "Protein Reminder",
            "body": "Every evening at 6 PM, remind to log protein intake.",
        }
    ]
    return inv


def _run_discovery(monkeypatch, tmp_path, payload):
    monkeypatch.setattr(
        _scanner, "_call_anthropic",
        lambda model, prompt, api_key, timeout=60: _stub_response(payload),
    )
    monkeypatch.setattr(_scanner, "_read_api_key", lambda bot_id, user=None: "sk-fake")
    inv = _atlas_inventory(tmp_path)
    return _scanner.llm_discover_applications(inv, model="tier3")


def _payload_from(specs):
    return [
        {"id": i, "name": n, "description": n, "confidence": 0.9,
         "evidence_files": ev}
        for (i, n, ev) in specs
    ]


def test_discovery_drops_all_false_positive_classes(monkeypatch, tmp_path):
    detected = _run_discovery(monkeypatch, tmp_path, _payload_from(_FALSE_POSITIVES))
    ids = {d.id for d in detected}
    for app_id, name, _ev in _FALSE_POSITIVES:
        assert app_id not in ids, f"floor failed to drop false positive {name!r}"
    assert detected == []


def test_discovery_keeps_all_legit_apps(monkeypatch, tmp_path):
    detected = _run_discovery(monkeypatch, tmp_path, _payload_from(_LEGIT_APPS))
    ids = {d.id for d in detected}
    for app_id, name, _ev in _LEGIT_APPS:
        assert app_id in ids, f"floor wrongly OVER-DROPPED legit app {name!r}"
    assert len(detected) == len(_LEGIT_APPS)


def test_discovery_full_atlas_shape_keeps_only_legit_plus_behavior(
    monkeypatch, tmp_path,
):
    """End-to-end: the full 22-cluster scan (11 FP + 10 legit + 1 behavior)
    yields exactly the 10 legit apps plus the Protein Reminder behavior app."""
    protein = ("protein-reminder", "Protein Reminder", [])  # zero file evidence
    payload = _payload_from(_FALSE_POSITIVES + _LEGIT_APPS + [protein])
    detected = _run_discovery(monkeypatch, tmp_path, payload)
    ids = {d.id for d in detected}

    expected = {a[0] for a in _LEGIT_APPS} | {"protein-reminder"}
    assert ids == expected
    # Communication Hub kept its bot-authored orchestration as evidence.
    comm = next(d for d in detected if d.id == "communication-messaging-hub")
    assert "legacy-scripts/communication_hub.py" in comm.evidence_files


def test_discovery_behavior_app_kept_only_when_candidate_matches(
    monkeypatch, tmp_path,
):
    """A zero-evidence app is kept ONLY if it matches a scheduled-action
    candidate. An unrelated zero-evidence cluster is still dropped."""
    payload = _payload_from([
        ("protein-reminder", "Protein Reminder", []),       # matches candidate
        ("mystery-widget", "Mystery Widget", []),           # no candidate → drop
    ])
    detected = _run_discovery(monkeypatch, tmp_path, payload)
    ids = {d.id for d in detected}
    assert "protein-reminder" in ids
    assert "mystery-widget" not in ids


def test_discovery_keeps_app_named_with_monitor(monkeypatch, tmp_path):
    """Auditor (d): a real app whose name contains 'monitor' and whose script
    is system_monitor.py must NOT collide with the infra-script basenames."""
    payload = _payload_from([
        ("system-monitor", "System Monitor", ["ops/system_monitor.py"]),
    ])
    detected = _run_discovery(monkeypatch, tmp_path, payload)
    assert {d.id for d in detected} == {"system-monitor"}


def test_discovery_keeps_app_with_identity_file_alongside_real_code(
    monkeypatch, tmp_path,
):
    """Auditor (a): an app whose evidence includes an identity file next to
    real code keeps the real code and survives."""
    payload = _payload_from([
        ("daily-logging", "Daily Logging", ["HEARTBEAT.md", "memory/2026-05-09.md"]),
    ])
    detected = _run_discovery(monkeypatch, tmp_path, payload)
    assert {d.id for d in detected} == {"daily-logging"}


# ════════════════════════════════════════════════════════════════════════════
# Integration: L3 reconcile RULE 3 — archive #2705 phantoms already on disk
# ════════════════════════════════════════════════════════════════════════════


def _write(caps: Path, stem: str, payload: dict) -> Path:
    p = caps / f"{stem}.json"
    p.write_text(json.dumps(payload, indent=2))
    return p


def _scanner_manifest(name: str, evidence: list[str], **extra) -> dict:
    base = {
        "id": name.lower().replace(" ", "-"),
        "name": name,
        "source": "discovered",
        "objective": f"{name} objective text (LLM-written)",  # phantoms HAVE prose
        "identity": {"purpose": f"{name} purpose"},
        "evidence_files": evidence,
        "scheduled_actions": [],
        "realized_files": [],
        "files": [],
    }
    base.update(extra)
    return base


@pytest.mark.parametrize(
    "name,evidence",
    [
        ("Gateway Management", ["bin/gateway-selfheal.sh"]),
        ("System Health Monitoring", ["bin/gateway-selfheal.sh", "bin/health_ping.sh"]),
        ("Session Startup", ["AGENTS.md#Session Startup"]),
        ("Memory Persistence", ["MEMORY.md"]),
        ("Persistent Memory System", ["AGENTS.md#Memory"]),
        ("Google Services Integration", ["integrations/google-oauth.json"]),
        ("Infrastructure Manifest Tracking", ["manifests/i-34cfcab1.json"]),
    ],
)
def test_l3_rule3_archives_each_phantom_class(tmp_path, name, evidence):
    caps = tmp_path / "manifests"
    caps.mkdir()
    stem = name.lower().replace(" ", "-").replace("&", "and")
    _write(caps, stem, _scanner_manifest(name, evidence))

    archived = _scanner._archive_platform_file_only_stubs(caps)

    assert len(archived) == 1, f"Rule 3 failed to archive phantom {name!r}"
    assert "no_app_evidence" in archived[0]
    assert not (caps / f"{stem}.json").exists()
    history = list((caps / "_history").glob(f"{stem}_no_app_evidence_*.json"))
    assert len(history) == 1


def test_l3_rule3_keeps_legit_app_with_real_files(tmp_path):
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _scanner_manifest("Property Management", ["ops/property_manager.py"])
    m["realized_files"] = [{"path": "ops/property_manager.py"},
                           {"path": "data/properties.json"}]
    _write(caps, "property-management", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == []
    assert (caps / "property-management.json").exists()


def test_l3_rule3_keeps_legit_app_with_identity_file_alongside_code(tmp_path):
    """Auditor (a): identity file in evidence + real code → kept."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _scanner_manifest("Daily Logging", ["HEARTBEAT.md", "ops/logger.py"])
    m["realized_files"] = [{"path": "ops/logger.py"}]
    _write(caps, "daily-logging", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == []
    assert (caps / "daily-logging.json").exists()


def test_l3_rule3_keeps_communication_hub_with_config_and_orchestration(tmp_path):
    """Auditor (b): a manifest with BOTH a skill config and orchestration must
    not be archived."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _scanner_manifest(
        "Communication Hub",
        ["legacy-scripts/communication_hub.py", "config/slack-webhook.json"],
    )
    m["realized_files"] = [{"path": "legacy-scripts/communication_hub.py"}]
    _write(caps, "communication-hub", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == []
    assert (caps / "communication-hub.json").exists()


def test_l3_rule3_keeps_behavior_app_with_vacuous_scheduled_actions(tmp_path):
    """Auditor (c): a recurring-behavior app (Protein Reminder) whose
    scheduled_actions are still 'vacuous' pre-install must NOT be archived,
    even though it cites HEARTBEAT.md (a #2705 identity class) and has no
    strict producer surface."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _scanner_manifest("Protein Reminder", ["HEARTBEAT.md#Protein Reminder"])
    m["scheduled_actions"] = [
        {"id": "protein", "mechanism": "unknown",
         "install": {"file": None, "plist_label": None, "command": None}},
    ]
    _write(caps, "protein-reminder", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == []
    assert (caps / "protein-reminder.json").exists()


def test_l3_rule3_keeps_operator_authored_manifest(tmp_path):
    """An operator-authored manifest is never archived, even if its evidence
    is entirely a #2705 non-app class."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _scanner_manifest("Operator Gateway App", ["bin/gateway-selfheal.sh"])
    m["source"] = "user_created"
    _write(caps, "operator-gateway", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == []
    assert (caps / "operator-gateway.json").exists()


def test_l3_rule3_does_not_fire_on_zero_evidence_manifest(tmp_path):
    """A truly empty manifest (no footprint, no evidence) is NOT a #2705
    class — Rule 3 requires at least one cited non-app file. Preserves the
    existing 'empty manifest is in-progress, keep it' contract."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _scanner_manifest("Watchdog Monitoring System", [])
    _write(caps, "watchdog-monitoring", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == []
    assert (caps / "watchdog-monitoring.json").exists()


def test_l3_rule3_does_not_disturb_platform_output_monitor(tmp_path):
    """A manifest pointing only at Tier-A platform OUTPUT (memory/turns) with
    operator content is NOT a #2705 class (those are excluded from
    _is_2705_nonapp_class) — Rule 3 leaves it to Rules 1/2, which keep it."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _scanner_manifest("Platform Watcher", [])
    m["realized_files"] = [{"path": "memory/turns-2026-05-09.jsonl"}]
    _write(caps, "platform-watcher", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == []
    assert (caps / "platform-watcher.json").exists()


# ════════════════════════════════════════════════════════════════════════════
# Integration: dedup backstop collapses no-app-evidence dupes sharing infra
# ════════════════════════════════════════════════════════════════════════════


def test_dedup_backstop_merges_gateway_dupes_sharing_infra_script(tmp_path):
    """The 5 gateway dupes share only gateway-selfheal.sh (not app evidence),
    so the >=50% overlap rule never fired. The both-no-app-evidence backstop
    collapses them."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write(caps, "gateway-management",
           _scanner_manifest("Gateway Management", ["bin/gateway-selfheal.sh"]))
    _write(caps, "system-health-monitoring",
           _scanner_manifest("System Health Monitoring", ["bin/gateway-selfheal.sh"]))

    merged = _scanner._dedup_manifests(caps)
    assert merged == 1
    survivors = [p for p in caps.glob("*.json")]
    assert len(survivors) == 1


def test_dedup_backstop_does_not_merge_distinct_legit_apps(tmp_path):
    """Two legit apps that each have real app evidence must NOT be merged by
    the backstop even when they share an infra script — the backstop only
    fires when BOTH manifests have zero app evidence. (Three files each with
    one shared keeps overlap at 33%, below the pre-existing 50% rule, so this
    isolates the backstop.)"""
    caps = tmp_path / "manifests"
    caps.mkdir()
    a = _scanner_manifest("Alpha App", ["ops/alpha.py"])
    a["realized_files"] = [{"path": "ops/alpha.py"}, {"path": "ops/alpha2.py"},
                           {"path": "bin/gateway-selfheal.sh"}]
    b = _scanner_manifest("Beta App", ["ops/beta.py"])
    b["realized_files"] = [{"path": "ops/beta.py"}, {"path": "ops/beta2.py"},
                           {"path": "bin/gateway-selfheal.sh"}]
    _write(caps, "alpha-app", a)
    _write(caps, "beta-app", b)

    merged = _scanner._dedup_manifests(caps)
    assert merged == 0
    assert len({p for p in caps.glob("*.json")}) == 2


# ════════════════════════════════════════════════════════════════════════════
# Adversarial-review regressions (fixes for the two-pass audit findings)
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "path",
    [
        "ops/photo_backup.py",       # "backup" is common in legit app names
        "ops/db_backup.sh",
        "ops/journal_backup.py",
        "config/inbox_backup.py",
    ],
)
def test_backup_named_app_is_not_infra_overdrop_guard(path):
    """Audit BUG 1: a "-backup" suffix must NOT classify legit backup apps as
    infrastructure. Over-dropping hits every bot."""
    assert not _scanner._is_infra_script_path(path)
    assert _scanner._app_evidence_files([path]) == [path]


def test_selfheal_suffix_still_infra():
    """The much-rarer "-selfheal" suffix is kept (gateway/watchdog self-heal)."""
    assert _scanner._is_infra_script_path("bin/myservice-selfheal.sh")
    assert _scanner._is_infra_script_path("bin/gateway-self-heal.sh")


def test_oc_system_phantom_denied_exemption_even_with_matching_candidate(
    monkeypatch, tmp_path,
):
    """Audit BUG 5: OC system-function phantoms must be dropped at Phase 2 even
    when a schedule-hinted identity section shares a token with the name (e.g.
    a "Memory" section saying 'review MEMORY.md daily'). The name-token denylist
    overrides the candidate match."""
    monkeypatch.setattr(
        _scanner, "_call_anthropic",
        lambda model, prompt, api_key, timeout=60: _stub_response(_payload_from([
            ("memory-persistence", "Memory Persistence", ["MEMORY.md"]),
            ("session-startup", "Session Startup", ["AGENTS.md#Session"]),
            ("persistent-memory-system", "Persistent Memory System", []),
            ("protein-reminder", "Protein Reminder", []),  # legit behavior — kept
        ])),
    )
    monkeypatch.setattr(_scanner, "_read_api_key", lambda bot_id, user=None: "sk-fake")
    inv = _scanner.WorkspaceInventory(workspace=tmp_path, bot_id="atlas")
    # Candidates that WOULD match the phantoms by a shared token, all
    # schedule-hinted (so they are genuine extracted candidates).
    inv.scheduled_action_candidates = [
        {"file_path": "AGENTS.md", "heading": "Memory",
         "body": "Review MEMORY.md and persistence state daily."},
        {"file_path": "AGENTS.md", "heading": "Session Startup",
         "body": "On startup each day, load the session context."},
        {"file_path": "HEARTBEAT.md", "heading": "Protein Reminder",
         "body": "Every evening at 6 PM, remind to log protein."},
    ]
    detected = _scanner.llm_discover_applications(inv, model="tier3")
    ids = {d.id for d in detected}
    assert "memory-persistence" not in ids
    assert "session-startup" not in ids
    assert "persistent-memory-system" not in ids
    assert ids == {"protein-reminder"}  # only the genuine behavior survives


def test_floor_behavior_exempt_majority_rule_keeps_legit_oc_token_names(
    monkeypatch, tmp_path,
):
    """Second-pass audit (2b): a legit zero-file behavior app whose name merely
    CONTAINS one OC-system token (a minority of the name) must be KEPT — the
    denylist is a majority test, not 'any token'. "Memory Lane Journal" (1/3),
    "Session Notes" (1/2), "Startup Advisor" (1/2) survive; the phantoms
    (2/2, 2/3) are still dropped."""
    monkeypatch.setattr(
        _scanner, "_call_anthropic",
        lambda model, prompt, api_key, timeout=60: _stub_response(_payload_from([
            ("memory-lane-journal", "Memory Lane Journal", []),     # 1/3 → keep
            ("session-notes", "Session Notes", []),                 # 1/2 → keep
            ("startup-advisor", "Startup Advisor", []),             # 1/2 → keep
            ("memory-persistence", "Memory Persistence", []),       # 2/2 → drop
            ("persistent-memory-system", "Persistent Memory System", []),  # 2/3 drop
        ])),
    )
    monkeypatch.setattr(_scanner, "_read_api_key", lambda bot_id, user=None: "sk-fake")
    inv = _scanner.WorkspaceInventory(workspace=tmp_path, bot_id="atlas")
    # A candidate that matches each app by a shared token so the exemption is
    # reachable for all of them — the denylist is the only thing that drops the
    # phantoms.
    inv.scheduled_action_candidates = [
        {"file_path": "HEARTBEAT.md", "heading": "Memory Lane Journal",
         "body": "Every morning, journal a memory."},
        {"file_path": "HEARTBEAT.md", "heading": "Session Notes",
         "body": "After each session daily, note takeaways."},
        {"file_path": "HEARTBEAT.md", "heading": "Startup Advisor",
         "body": "Daily startup advice digest."},
        {"file_path": "AGENTS.md", "heading": "Memory",
         "body": "Review memory and persistence state daily."},
    ]
    detected = _scanner.llm_discover_applications(inv, model="tier3")
    ids = {d.id for d in detected}
    assert "memory-lane-journal" in ids
    assert "session-notes" in ids
    assert "startup-advisor" in ids
    assert "memory-persistence" not in ids
    assert "persistent-memory-system" not in ids


@pytest.mark.parametrize(
    "path",
    [
        "manifests/i-34cfcab1.json",
        "workspace/manifests/i-1.json",          # workspace-prefixed
        "/Users/atlas/.openclaw/workspace/manifests/i-3.json",  # absolute
    ],
)
def test_manifest_store_path_matches_any_segment(path):
    assert _scanner._is_manifest_store_path(path)
    assert _scanner._app_evidence_files([path]) == []


@pytest.mark.parametrize(
    "path",
    ["data/manifest.json", "ops/manifests_helper.py"],  # not the store
)
def test_manifest_store_path_rejects_lookalikes(path):
    assert not _scanner._is_manifest_store_path(path)


def _write_spec(shared: Path, spec_id: str, version: str, payload: dict) -> None:
    d = shared / "gallery" / "local" / spec_id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{version}.json").write_text(json.dumps(payload))


def test_l3_rule3_keeps_v7arc_app_with_cli_spec_surface(tmp_path):
    """Audit BUG 2: a v7-arc Instance whose bound Spec declares a CLI surface +
    real files, but whose Instance is observational (empty realized_files,
    cites AGENTS.md), must NOT be archived. hydrate doesn't overlay
    interface_contract/files — _load_bound_spec does."""
    shared = tmp_path / "shared"
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_spec(shared, "s-expense", "1.0.0", {
        "id": "s-expense", "name": "Expense Tracker",
        "interface_contract": {"cli": [{"command": "expense add"}]},
        "realized_files": [{"path": "ops/expenses.py"}],
    })
    _write(caps, "i-expense", {
        "id": "i-expense", "manifest_shape": "v7-arc", "source": "discovered",
        "provenance": {"spec_id": "s-expense", "spec_version": "1.0.0"},
        "evidence_files": ["AGENTS.md"],   # observational instance
        "realized_files": [], "files": [], "scheduled_actions": [],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps, shared_dir=shared)
    assert archived == []
    assert (caps / "i-expense.json").exists()


def test_l3_rule3_archives_v7arc_phantom_with_empty_spec(tmp_path):
    """A v7-arc phantom whose Spec has no surface/files and whose evidence is an
    infra script IS archived — the Spec-aware check still retires phantoms."""
    shared = tmp_path / "shared"
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_spec(shared, "s-gw", "1.0.0", {"id": "s-gw", "name": "Gateway Watcher"})
    _write(caps, "i-gw", {
        "id": "i-gw", "manifest_shape": "v7-arc", "source": "discovered",
        "provenance": {"spec_id": "s-gw", "spec_version": "1.0.0"},
        "evidence_files": ["bin/gateway-selfheal.sh"],
        "realized_files": [], "files": [], "scheduled_actions": [],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps, shared_dir=shared)
    assert len(archived) == 1
    assert "no_app_evidence" in archived[0]
    assert not (caps / "i-gw.json").exists()


# ════════════════════════════════════════════════════════════════════════════
# Reconcile scheduled-actions / soft-surface discrimination (#2885 follow-up)
#
# The #2885 floor only blocked NEW mints; it could not retire the ~20 phantoms
# already on disk that carry a vacuous scheduled_actions[] list or a
# heartbeat_evidence anchor — the raw-presence guard shielded them forever.
# These fixtures mirror the EXACT shapes verified live on a production bot
# (anonymized): every phantom matched a Phase-4 candidate, so it has
# mechanism="unknown", install={} scheduled_actions anchored to generic
# AGENTS.md/POD_CONDUCT.md identity sections — the same sections legit apps
# cite. Discrimination is therefore by NAME (OC runtime-function) and by HARD
# non-app evidence (infra script / manifest store), never by the (shared,
# non-discriminating) scheduled-action target.
# ════════════════════════════════════════════════════════════════════════════


def _vacuous_sched(action_id: str, evidence_path: str, kind: str = "heartbeat") -> dict:
    """A v13 candidate-derived scheduled_action exactly as the live phantoms
    carry it: mechanism unknown, EMPTY install (so producer_surface_kinds sees
    no surface), source markdown carried in trigger.evidence_path."""
    return {
        "id": action_id,
        "mechanism": "unknown",
        "install": {},
        "installed_artifact": None,
        "trigger": {
            "kind": kind,
            "schedule": "Daily",
            "evidence_path": evidence_path,
            "evidence_locator": "🛠️ Installed Apps — USE THESE FOR THE THINGS THEY DO",
            "section_sha256": "deadbeef",
        },
        "inputs": [],
        "outputs": [],
        "summary": "extracted recurring-behavior candidate",
    }


def test_l3_rule3_archives_oc_system_phantom_with_vacuous_scheduled_actions(tmp_path):
    """THE BUG: 'Memory Persistence' cites only OC identity files (AGENTS.md /
    MEMORY.md) and carries 12 vacuous scheduled_actions. The pre-fix
    raw-presence guard shielded it; the OC-system name test now sweeps it."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _scanner_manifest("Memory Persistence", ["AGENTS.md", "MEMORY.md"])
    m["realized_files"] = [{"path": "AGENTS.md"}, {"path": "MEMORY.md"}]
    m["files"] = [{"path": "AGENTS.md"}, {"path": "MEMORY.md"}]
    m["scheduled_actions"] = [
        _vacuous_sched("memory-persistence-agents-md", "AGENTS.md"),
        _vacuous_sched("memory-persistence-memory", "AGENTS.md"),
    ]
    _write(caps, "memory-persistence", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert len(archived) == 1, "OC-system phantom with vacuous scheds must archive"
    assert not (caps / "memory-persistence.json").exists()


def test_l3_rule3_archives_oc_system_phantom_with_heartbeat_evidence(tmp_path):
    """'Session Startup' / 'Persistent Memory System' shape: a SOFT
    heartbeat_evidence anchor (section_anchors into AGENTS.md identity
    headings) is reported by producer_surface_kinds as a 'real' surface, and
    pre-fix that blocked archival. A soft surface must not shield an OC-system
    phantom — only a CONCRETE surface or a legit-behavior presentation does."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    for nm, stem in [("Session Startup", "session-startup"),
                     ("Persistent Memory System", "persistent-memory-system")]:
        m = _scanner_manifest(nm, [f"AGENTS.md ({nm} section)"])
        m["heartbeat_evidence"] = {
            "file_path": "AGENTS.md",
            "section_anchors": [f"{nm} section", "Memory"],
        }
        m["scheduled_actions"] = [_vacuous_sched(f"{stem}-x", "AGENTS.md")]
        _write(caps, stem, m)

    # Sanity: producer_surface_kinds DOES report heartbeat_evidence as a surface
    # (so the OLD any-surface gate would have kept these).
    sample = json.loads((caps / "session-startup.json").read_text())
    assert _scanner._has_real_producer_surface(sample)
    assert not _scanner._concrete_producer_surface_kinds(sample)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert len(archived) == 2, "heartbeat-anchored OC-system phantoms must archive"
    assert not (caps / "session-startup.json").exists()
    assert not (caps / "persistent-memory-system.json").exists()


def test_l3_rule3_archives_infra_phantom_with_noisy_cron_evidence(tmp_path):
    """'System Health Monitoring' / 'Gateway Management' shape: the infra-script
    citation is wrapped in a cron schedule + prose
    ('gateway-selfheal.sh (cron job */15)'). A naive Path().name read finds
    '15)' and misses the script, so #2885 silently counted it as app evidence
    and never fired. _citation_paths now extracts the real script path."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    health = _scanner_manifest(
        "System Health Monitoring",
        ["*/30 * * * * /Users/x/sentry_ping.sh (cron job)",
         "*/15 * * * * /Users/x/bin/gateway-selfheal.sh (cron job)"],
    )
    health["scheduled_actions"] = [_vacuous_sched("health-x", "AGENTS.md")]
    _write(caps, "system-health-monitoring", health)

    gw = _scanner_manifest("Gateway Management", ["gateway-selfheal.sh (cron job */15)"])
    gw["scheduled_actions"] = [
        _vacuous_sched("gw-tasks", "AGENTS.md"),
        _vacuous_sched("gw-conduct", "POD_CONDUCT.md"),
    ]
    _write(caps, "gateway-management", gw)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert len(archived) == 2, f"infra phantoms must archive, got {archived}"
    assert not (caps / "system-health-monitoring.json").exists()
    assert not (caps / "gateway-management.json").exists()


def test_l3_rule3_archives_manifest_store_phantom_with_scheduled_actions(tmp_path):
    """'Infrastructure Manifest Tracking': cites the manifests/ store (a HARD
    non-app class) and carries vacuous scheduled_actions anchored to AGENTS.md.
    The hard-class citation defeats the behavior shield even though the name is
    not OC-system."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _scanner_manifest(
        "Infrastructure Manifest Tracking",
        ["manifests/i-0b78ebf9.json", "manifests/i-2da5ca68.json"],
    )
    m["realized_files"] = [{"path": "manifests/i-0b78ebf9.json"}]
    m["scheduled_actions"] = [
        _vacuous_sched("imt-structure", "AGENTS.md"),
        _vacuous_sched("imt-conduct", "POD_CONDUCT.md"),
    ]
    _write(caps, "infrastructure-manifest-tracking", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert len(archived) == 1
    assert not (caps / "infrastructure-manifest-tracking.json").exists()


# ── Over-drop guards (the direction the auditor cares about) ─────────────────


def test_l3_rule3_keeps_legit_behavior_app_with_vacuous_scheds_and_heartbeat(tmp_path):
    """OVER-DROP GUARD: a legit hand-written recurring-behavior app ('Morning
    Briefing') lives in the SAME structural position as the OC-system phantoms
    — it cites only HEARTBEAT.md, carries a heartbeat_evidence anchor and
    vacuous scheduled_actions, has no concrete surface, no Spec, no app file.
    It is told apart purely by its non-OC-system NAME and must SURVIVE."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _scanner_manifest("Morning Briefing", ["HEARTBEAT.md#Morning Briefing"])
    m["heartbeat_evidence"] = {
        "file_path": "HEARTBEAT.md", "section_anchors": ["Morning Briefing"],
    }
    m["scheduled_actions"] = [_vacuous_sched("morning-briefing", "HEARTBEAT.md")]
    _write(caps, "morning-briefing", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == [], "legit non-OC behavior app must NOT be archived"
    assert (caps / "morning-briefing.json").exists()


def test_l3_rule3_keeps_minority_oc_token_behavior_app(tmp_path):
    """OVER-DROP GUARD: 'Memory Lane Journal' (1/3 OC tokens) is a legit
    behavior app — the name test is a MAJORITY test, not 'any token'. Same
    vacuous-sched + heartbeat shape as a phantom; survives on the name rule."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _scanner_manifest("Memory Lane Journal", ["HEARTBEAT.md#Memory Lane"])
    m["heartbeat_evidence"] = {
        "file_path": "HEARTBEAT.md", "section_anchors": ["Memory Lane"],
    }
    m["scheduled_actions"] = [_vacuous_sched("memory-lane", "HEARTBEAT.md")]
    _write(caps, "memory-lane-journal", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == [], "minority-OC-token behavior app must survive"
    assert (caps / "memory-lane-journal.json").exists()


def test_l3_rule3_keeps_oc_named_app_with_concrete_surface(tmp_path):
    """OVER-DROP GUARD: even an OC-system-named cluster is KEPT when it has a
    CONCRETE surface (a non-vacuous scheduled-action install here) — a concrete
    surface means the app is genuinely wired into the runtime. The name test
    only governs the soft/vacuous case."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _scanner_manifest("Memory Persistence", ["AGENTS.md"])
    m["scheduled_actions"] = [{
        "id": "mp-install", "mechanism": "launchd",
        "install": {"command": "python ops/membank.py", "plist_label": "com.x.membank"},
        "installed_artifact": "/Users/x/Library/LaunchAgents/com.x.membank.plist",
        "trigger": {"kind": "launchd", "schedule": "Hour=9"},
        "inputs": [], "outputs": [],
    }]
    _write(caps, "memory-persistence-real", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == [], "concrete-surface app must survive regardless of name"
    assert (caps / "memory-persistence-real.json").exists()


def test_l3_rule3_keeps_oc_named_instance_bound_to_real_spec(tmp_path):
    """OVER-DROP GUARD (auditor's 'bound to a real Spec'): an OC-named v7-arc
    Instance whose bound Spec carries a real CLI surface + files survives — the
    Spec's surface is folded in via _load_bound_spec / _comparable_view."""
    shared = tmp_path / "shared"
    caps = tmp_path / "manifests"
    caps.mkdir()
    _write_spec(shared, "s-mem", "1.0.0", {
        "id": "s-mem", "name": "Memory Manager",
        "interface_contract": {"cli": [{"command": "membank query"}]},
        "realized_files": [{"path": "ops/membank.py"}],
    })
    _write(caps, "i-mem", {
        "id": "i-mem", "name": "Memory Persistence", "manifest_shape": "v7-arc",
        "source": "discovered",
        "provenance": {"spec_id": "s-mem", "spec_version": "1.0.0"},
        "evidence_files": ["AGENTS.md"],
        "realized_files": [], "files": [],
        "scheduled_actions": [_vacuous_sched("i-mem-x", "AGENTS.md")],
    })

    archived = _scanner._archive_platform_file_only_stubs(caps, shared_dir=shared)
    assert archived == [], "Spec-bound OC-named instance must survive"
    assert (caps / "i-mem.json").exists()


def test_l3_rule3_archives_infra_phantom_with_concrete_launchd_surface(tmp_path):
    """Adversarial-review Concern 1: an infra phantom that gains an attributed
    LaunchAgent (a NON-vacuous launchd scheduled_action → CONCRETE surface)
    must still archive. A hard non-app citation (the gateway-selfheal.sh in
    evidence AND in the launchd install.command) defeats EVERY shield,
    including a concrete cron surface — an attributed infra LaunchAgent is not
    a real app surface."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _scanner_manifest("Gateway Management", ["bin/gateway-selfheal.sh"])
    m["scheduled_actions"] = [{
        "id": "gw-selfheal", "mechanism": "launchd",
        "install": {"command": "/bin/bash /Users/x/bin/gateway-selfheal.sh",
                    "plist_label": "com.evolve.gateway-selfheal"},
        "installed_artifact": "/Users/x/Library/LaunchAgents/com.evolve.gateway-selfheal.plist",
        "trigger": {"kind": "launchd", "schedule": "every 900 seconds"},
        "inputs": [], "outputs": [],
    }]
    m["cron_evidence"] = {"labels": ["com.evolve.gateway-selfheal"]}
    _write(caps, "gateway-management", m)

    # The launchd install IS a concrete surface (the OLD any-surface gate, and
    # an unguarded concrete branch, would have shielded it).
    assert _scanner._concrete_producer_surface_kinds(m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert len(archived) == 1, "infra phantom with concrete cron surface must archive"
    assert not (caps / "gateway-management.json").exists()


def test_l3_rule3_keeps_behavior_app_that_reads_a_credential_file(tmp_path):
    """Adversarial-review Concern 2 (over-drop): a legit recurring-behavior app
    that cites/reads a bare skill credential (google-oauth.json / a webhook
    config) must SURVIVE — a credential citation is not a HARD non-app class.
    The floor counts a skill config as evidence under orchestration, and a
    behavior app legitimately touches credentials."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    # (a) evidence cites the OAuth cred + a heartbeat anchor
    a = _scanner_manifest(
        "Morning Briefing", ["HEARTBEAT.md#Morning Briefing", "integrations/google-oauth.json"],
    )
    a["heartbeat_evidence"] = {"file_path": "HEARTBEAT.md", "section_anchors": ["Morning Briefing"]}
    a["scheduled_actions"] = [_vacuous_sched("morning-briefing", "HEARTBEAT.md")]
    _write(caps, "morning-briefing", a)
    # (b) scheduled_action READS a webhook config (input target)
    b = _scanner_manifest("Daily Slack Digest", ["HEARTBEAT.md#Slack Digest"])
    b["heartbeat_evidence"] = {"file_path": "HEARTBEAT.md", "section_anchors": ["Slack Digest"]}
    sa = _vacuous_sched("slack-digest", "HEARTBEAT.md")
    sa["inputs"] = [{"path": "config/slack-webhook.json"}]
    b["scheduled_actions"] = [sa]
    _write(caps, "daily-slack-digest", b)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == [], f"credential-touching behavior apps must survive, got {archived}"
    assert (caps / "morning-briefing.json").exists()
    assert (caps / "daily-slack-digest.json").exists()


def test_l3_rule3_archives_infra_runbook_doc_over_all_hard_infra(tmp_path):
    """Class 2 (Bite C, inverts the #2894 survivor): 'Infrastructure Health
    Monitoring' cites two HARD infra scripts (sentry_ping.sh +
    gateway-selfheal.sh) and a lone runbook .md, and its objective is
    infra-by-purpose. Pre-fix that .md made _app_evidence_files non-empty so
    Rule 3 never fired. A doc does NOT redeem an infra-by-purpose cluster whose
    every CODE/script citation is hard-infra — the .md is a runbook FOR the
    infra, so the cluster is no-app-evidence and ARCHIVES."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _scanner_manifest(
        "Infrastructure Health Monitoring",
        ["sentry_ping.sh", "bin/gateway-selfheal.sh",
         "operations/maintenance/watchdog-setup-instructions.md"],
    )
    # Infra-by-purpose objective (the live shape) — required by the Class-2
    # infra-purpose gate that spares legit doc apps citing one infra path.
    m["objective"] = ("Self-healing automation that maintains continuous operation "
                      "of the OpenClaw gateway through periodic liveness pinging.")
    m["realized_files"] = [{"path": "operations/maintenance/watchdog-setup-instructions.md"}]
    m["scheduled_actions"] = [_vacuous_sched("ihm-x", "AGENTS.md")]
    _write(caps, "infrastructure-health", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert len(archived) == 1, "infra-runbook .md must not rescue an all-hard-infra cluster"
    assert "no_app_evidence" in archived[0]
    assert not (caps / "infrastructure-health.json").exists()


def test_l3_rule3_keeps_doc_producing_app_with_no_hard_infra(tmp_path):
    """Class-2 over-drop guard: a legit doc-PRODUCING app (Daily Operations
    Briefing shape — operations/status-reports/*.md are its OUTPUT, no hard-infra
    script anywhere) must SURVIVE. The doc-redemption discount keys on 'every
    CODE citation is hard-infra'; with no hard-infra code present, the docs
    redeem normally."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _scanner_manifest(
        "Daily Operations Briefing",
        ["operations/status-reports/2026-03-15-daily.md",
         "operations/status-reports/CONSOLIDATION.md",
         "AGENTS.md#Session Startup"],
    )
    m["realized_files"] = [{"path": "operations/status-reports/2026-03-15-daily.md"}]
    _write(caps, "daily-briefing", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == [], f"doc-producing app with no hard-infra must survive, got {archived}"
    assert (caps / "daily-briefing.json").exists()


def test_l3_rule3_keeps_doc_plus_real_script_beside_infra(tmp_path):
    """Class-2 over-drop guard: a cluster with a doc AND a real .py producer
    beside an infra script is KEPT — the .py is a non-doc, non-hard-infra code
    file, so 'every CODE citation is hard-infra' is False and the docs redeem."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    m = _scanner_manifest(
        "Maintenance Logger",
        ["bin/gateway-selfheal.sh", "ops/maintenance_logger.py", "docs/runbook.md"],
    )
    m["realized_files"] = [{"path": "ops/maintenance_logger.py"}]
    _write(caps, "maintenance-logger", m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == [], f"real producer .py beside infra must survive, got {archived}"
    assert (caps / "maintenance-logger.json").exists()


# ── Unit: the new discrimination helpers ─────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("gateway-selfheal.sh (cron job */15)", ["gateway-selfheal.sh"]),
        ("*/30 * * * * /Users/x/sentry_ping.sh (cron job)", ["Users/x/sentry_ping.sh"]),
        ("AGENTS.md (Session Startup section)", ["AGENTS.md"]),
        ("AGENTS.md#Memory", ["AGENTS.md"]),
        ("scripts/tasks.py", ["scripts/tasks.py"]),     # clean → unchanged
        ("manifests/i-0b78ebf9.json", ["manifests/i-0b78ebf9.json"]),
        ("", []),
        ("every morning at 6 PM", []),                  # pure prose, no path
    ],
)
def test_citation_paths_extracts_real_path_from_noise(raw, expected):
    assert _scanner._citation_paths(raw) == expected


def test_citation_paths_noisy_infra_string_classified_as_non_app():
    """The end-to-end point: a noisy infra citation is NOT app evidence and IS
    a #2705 / hard non-app class once expanded."""
    for raw in ["gateway-selfheal.sh (cron job */15)",
                "*/30 * * * * /Users/x/sentry_ping.sh (cron job)"]:
        toks = _scanner._citation_paths(raw)
        assert _scanner._app_evidence_files(toks) == []
        assert any(_scanner._is_hard_nonapp_class(t) for t in toks)


@pytest.mark.parametrize(
    "path,is_hard",
    [
        ("bin/gateway-selfheal.sh", True),
        ("sentry_ping.sh", True),
        ("manifests/i-1.json", True),
        # OC identity — NOT hard (legit behavior apps cite it):
        ("AGENTS.md", False),
        ("HEARTBEAT.md", False),
        ("MEMORY.md", False),
        # Bare skill config — NOT hard: it's app evidence when orchestration
        # rides on top, and a legit behavior app may simply read a credential:
        ("integrations/google-oauth.json", False),
        ("config/slack-webhook.json", False),
        ("scripts/tasks.py", False),     # real app script
    ],
)
def test_is_hard_nonapp_class(path, is_hard):
    assert _scanner._is_hard_nonapp_class(path) is is_hard


@pytest.mark.parametrize(
    "name,is_oc",
    [
        ("Memory Persistence", True),
        ("Session Startup", True),
        ("Persistent Memory System", True),
        ("Memory Lane Journal", False),
        ("Session Notes", False),
        ("Morning Briefing", False),
        ("Gateway Management", False),
        ("", False),
    ],
)
def test_name_is_oc_system_function(name, is_oc):
    assert _scanner._name_is_oc_system_function(name) is is_oc


def test_scheduled_action_referenced_paths_pulls_targets():
    vac = _vacuous_sched("x", "AGENTS.md")
    assert "AGENTS.md" in _scanner._scheduled_action_referenced_paths(vac)
    launchd = {
        "install": {"command": "bash bin/gateway-selfheal.sh", "plist_label": "L"},
        "installed_artifact": "/Users/x/Library/LaunchAgents/L.plist",
        "trigger": {"kind": "launchd"}, "inputs": [], "outputs": [],
    }
    paths = _scanner._scheduled_action_referenced_paths(launchd)
    assert "bash bin/gateway-selfheal.sh" in paths
    # and the embedded script path is recovered after _citation_paths expansion
    expanded = [t for raw in paths for t in _scanner._citation_paths(raw)]
    assert any(_scanner._is_infra_script_path(t) for t in expanded)


# ── Unit: Bite C discrimination helpers (Class 1 + Class 2) ───────────────────


@pytest.mark.parametrize(
    "path,is_doc",
    [
        ("operations/maintenance/health-setup-instructions.md", True),
        ("README.rst", True),
        ("notes.txt", True),
        ("AGENTS.md#Memory", True),          # anchor stripped, still .md
        ("scripts/tasks.py", False),
        ("bin/gateway-selfheal.sh", False),
        ("integrations/google-oauth.json", False),
        ("", False),
    ],
)
def test_is_doc_evidence(path, is_doc):
    assert _scanner._is_doc_evidence(path) is is_doc


def test_doc_only_over_hard_infra_fires_on_all_hard_infra_plus_doc():
    """The Class-2 case: every CODE citation is hard-infra, only a doc remains
    as app evidence → the doc does not redeem."""
    ev = ["sentry_ping.sh", "bin/gateway-selfheal.sh",
          "operations/maintenance/watchdog-setup-instructions.md"]
    app_ev = _scanner._app_evidence_files(ev)
    assert app_ev == ["operations/maintenance/watchdog-setup-instructions.md"]
    assert _scanner._doc_only_over_hard_infra(ev, app_ev) is True


def test_doc_only_over_hard_infra_false_when_no_hard_infra_code():
    """Daily Briefing / Property Management shape: docs with NO hard-infra
    script → docs redeem (returns False)."""
    ev = ["operations/status-reports/2026-03-15-daily.md",
          "operations/status-reports/CONSOLIDATION.md"]
    app_ev = _scanner._app_evidence_files(ev)
    assert app_ev  # the docs ARE app evidence here
    assert _scanner._doc_only_over_hard_infra(ev, app_ev) is False


def test_doc_only_over_hard_infra_false_when_real_script_present():
    """A real .py producer beside an infra script → not all code is hard-infra
    → docs redeem (returns False)."""
    ev = ["bin/gateway-selfheal.sh", "ops/logger.py", "docs/runbook.md"]
    app_ev = _scanner._app_evidence_files(ev)
    assert "ops/logger.py" in app_ev
    assert _scanner._doc_only_over_hard_infra(ev, app_ev) is False


@pytest.mark.parametrize(
    "text,claims",
    [
        ("Liveness monitoring and health check system through periodic pings.", True),
        ("Automated gateway process management and recovery system.", True),
        ("Posts a briefing every morning at 6 AM.", True),
        ("Self-healing automation that runs on a schedule.", True),
        ("Sends a daily summary.", True),
        ("Reminder to drink water.", True),
        # Placeholder / non-recurring prose stays below the bar:
        ("Watchdog Monitoring System objective text (LLM-written)", False),
        ("Tracks property maintenance records and service providers.", False),
        ("A budget manager for ranch expenses.", False),
        ("", False),
    ],
)
def test_asserts_recurring_behavior(text, claims):
    assert _scanner._asserts_recurring_behavior(text) is claims


@pytest.mark.parametrize(
    "text,is_infra",
    [
        # Caught by HIGH-PRECISION terms only:
        ("Gateway Self-Healing", True),                         # self-heal
        ("Liveness monitoring and health check system", True),  # liveness
        ("ensuring stable OpenClaw gateway operations", True),  # openclaw
        ("the watchdog probe", True),                           # watchdog
        ("repo-puller keeps the checkout current", True),       # repo-pull
        ("self-recovery of the runtime", True),                 # self-recover
        # De-ambiguated: bare gateway/heartbeat/uptime are NOT infra signals,
        # so legit operator apps using those domain words are spared (the
        # adversarial-review over-drop):
        ("Heartbeat Tracker for resting heart rate", False),
        ("Payment Gateway Reconciler", False),
        ("Uptime dashboard for my website", False),
        # Operator-domain apps are NOT infra:
        ("Morning Briefing — a daily summary", False),
        ("Property Management for the ranch", False),
        ("Smart Budget Manager", False),
        ("", False),
    ],
)
def test_describes_pod_infra(text, is_infra):
    assert _scanner._describes_pod_infra(text) is is_infra


def test_describes_pod_infra_de_ambiguation_lets_legit_apps_survive_rule4(tmp_path):
    """Over-drop regression (adversarial review): a freshly-stubbed EMPTY app
    whose legit name contains a now-de-ambiguated infra-ish word AND asserts a
    schedule must SURVIVE — Rule 4's infra gate no longer fires on bare
    gateway/heartbeat/uptime."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    for stem, name, desc in [
        ("hb", "Heartbeat Tracker", "Logs the user's resting heart rate every morning."),
        ("pg", "Payment Gateway Reconciler", "Reconciles payment gateway transactions every night."),
    ]:
        m = _scanner_manifest(name, [])
        m["objective"] = ""
        m["description"] = desc
        _write(caps, stem, m)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == [], f"legit infra-ish-named apps must survive Rule 4, got {archived}"


def test_l3_rule3_keeps_doc_app_citing_one_infra_path_when_not_infra_named(tmp_path):
    """Over-drop regression (adversarial review): a legit doc-PRODUCING app that
    is NOT infra-by-purpose but merely CITES one infra script (or the manifest
    store) keeps its docs — the Class-2 discount is gated on an infra-purpose
    name/objective, which these lack."""
    caps = tmp_path / "manifests"
    caps.mkdir()
    # (a) Weekly report writer citing one infra script — operator-domain name.
    a = _scanner_manifest(
        "Weekly Ops Report",
        ["reports/2026-06-01.md", "reports/2026-06-08.md", "bin/gateway-selfheal.sh"],
    )
    a["objective"] = "Compiles a weekly operations report for the team."
    a["realized_files"] = [{"path": "reports/2026-06-01.md"}]
    _write(caps, "weekly-ops-report", a)
    # (b) Markdown catalog citing the manifest store — operator-domain name.
    b = _scanner_manifest(
        "App Catalog",
        ["catalog/index.md", "manifests/i-other.json"],
    )
    b["objective"] = "Maintains a human-readable catalog of installed apps."
    b["realized_files"] = [{"path": "catalog/index.md"}]
    _write(caps, "app-catalog", b)

    archived = _scanner._archive_platform_file_only_stubs(caps)
    assert archived == [], f"non-infra doc apps citing one infra path must survive, got {archived}"
    assert (caps / "weekly-ops-report.json").exists()
    assert (caps / "app-catalog.json").exists()


def test_manifest_prose_flattens_objective_dict_and_string():
    d = {"name": "X", "description": "desc text",
         "objective": {"primary": "primary text", "sub_objectives": ["sub one"]}}
    prose = _scanner._manifest_prose(d)
    assert "primary text" in prose and "sub one" in prose and "desc text" in prose
    s = {"name": "Y", "objective": "flat objective"}
    assert "flat objective" in _scanner._manifest_prose(s)


# ── Full-fleet regression fixture (anonymized live bot manifests) ─────────────
# Mirrors every manifest on the production bot verified live 2026-06-14
# (reserved tokens anonymized). Asserts the exact archive/survive partition:
# the 3 deterministic false-positives this bite kills retire; the 11 legit apps
# (incl. doc-producing apps, credential-touching apps, and a cluster that cites
# a hard manifest-store path but owns real scripts) survive untouched.


def _v7arc(name, realized, **extra):
    """A scanner-discovered v7-arc Instance with instance-level realized_files
    (the live survivor shape: ev=[], rf=[script])."""
    m = _scanner_manifest(name, [])
    m["manifest_shape"] = "v7-arc"
    m["provenance"] = {"spec_id": f"p-{name[:6].lower()}", "spec_version": "1.0"}
    m["realized_files"] = [{"path": p} for p in realized]
    m.update(extra)
    return m


def test_l3_full_fleet_regression_archives_3_keeps_11(tmp_path):
    caps = tmp_path / "manifests"
    caps.mkdir()

    # ── The 3 deterministic phantoms that MUST archive ──────────────────────
    # Class 1: incoherent zero-realization infra shells (empty everything, but
    # description asserts recurring behavior + infra-by-purpose).
    liveness = _scanner_manifest("Liveness Monitoring System", [])
    liveness["manifest_shape"] = "v7-arc"
    liveness["objective"] = ""
    liveness["description"] = ("Liveness monitoring and health check system that "
                               "tracks operational status through periodic pings.")
    _write(caps, "i-0b78ebf9", liveness)

    gateway = _scanner_manifest("Gateway Self-Healing", [])
    gateway["manifest_shape"] = "v7-arc"
    gateway["objective"] = ""
    gateway["description"] = ("Automated gateway process management and recovery "
                              "system ensuring stable OpenClaw gateway operations.")
    _write(caps, "i-5cd78794", gateway)

    # Class 2: infra-runbook .md beside two HARD infra scripts (no other code),
    # infra-by-purpose objective.
    infra_health = _scanner_manifest(
        "Infrastructure Health Monitoring",
        ["liveness_ping.sh", "bin/gateway-selfheal.sh",
         "operations/maintenance/health-setup-instructions.md"],
        source=None,
    )
    infra_health["objective"] = ("Self-healing automation maintaining continuous "
                                 "operation of the OpenClaw gateway via liveness pings.")
    infra_health["realized_files"] = [
        {"path": "operations/maintenance/health-setup-instructions.md"}]
    infra_health["scheduled_actions"] = [_vacuous_sched("ihm-x", "AGENTS.md")]
    _write(caps, "infrastructure-health", infra_health)

    # ── The 11 legit apps that MUST survive ─────────────────────────────────
    # Real realized scripts (v5/legacy shape).
    budget = _scanner_manifest("Smart Budget Manager", ["legacy-scripts/smart_budget_manager.py"])
    budget["realized_files"] = [{"path": "legacy-scripts/smart_budget_manager.py"}]
    budget["scheduled_actions"] = [_vacuous_sched("budget-x", "AGENTS.md")]
    _write(caps, "app-budget-management", budget)

    comms = _scanner_manifest(
        "Communication Hub",
        ["config/communication.json", "legacy-scripts/communication_hub.py",
         "legacy-scripts/heartbeat_slack_processor.py"],
    )
    comms["realized_files"] = [{"path": "legacy-scripts/communication_hub.py"},
                               {"path": "legacy-scripts/heartbeat_slack_processor.py"}]
    _write(caps, "app-communication-hub", comms)

    # Cites a HARD manifest-store path AND .txt, but owns a real script → kept.
    dropbox = _scanner_manifest(
        "Dropbox Synchronization",
        ["scripts/update_dropbox_index.py", "property/.dropbox_snapshot.txt",
         "manifests/app-dropbox-sync.json"],
    )
    dropbox["realized_files"] = [{"path": "scripts/update_dropbox_index.py"}]
    _write(caps, "app-dropbox-sync", dropbox)

    # Docs-only app (no hard-infra code) → docs redeem → kept.
    prop = _scanner_manifest(
        "Property Management",
        ["property/AGENDA.md", "property/PUNCH_LIST.md", "property/SERVICE_PROVIDERS.md"],
    )
    prop["realized_files"] = [{"path": "property/AGENDA.md"}]
    _write(caps, "app-property-management", prop)

    tasks = _scanner_manifest(
        "Task Management",
        ["AGENTS.md (Task Management section)", "scripts/tasks.py"],
    )
    tasks["realized_files"] = [{"path": "scripts/tasks.py"}]
    _write(caps, "app-task-management", tasks)

    # Doc-PRODUCING app: status-report .md outputs + identity anchor, no hard
    # infra → kept (the Class-2 over-drop guard, live shape).
    briefing = _scanner_manifest(
        "Daily Operations Briefing",
        ["operations/status-reports/2026-03-15-daily.md",
         "operations/status-reports/CONSOLIDATION.md", "AGENTS.md#Session Startup"],
    )
    briefing["realized_files"] = [{"path": "operations/status-reports/2026-03-15-daily.md"}]
    briefing["scheduled_actions"] = [_vacuous_sched("brief-x", "AGENTS.md")]
    _write(caps, "daily-briefing", briefing)

    # v7-arc Instances with instance-level realized scripts → concrete surface.
    _write(caps, "i-2da5ca68", _v7arc("Stakeholder Workspace Manager",
                                      ["ops/stakeholder_workspace.py"]))
    _write(caps, "i-3cde39b9", _v7arc("Google Services Integration",
                                      ["integrations/oauth_flow.py",
                                       "integrations/google_api.py"]))
    _write(caps, "i-512954ad", _v7arc("Vineyard Operations", ["ops/vineyard.py"]))
    _write(caps, "i-6c9c905d", _v7arc("Ranch Operations Hub", ["ops/ranch_hub.py"],
                                      scheduled_actions=[]))
    _write(caps, "i-6d27dde6", _v7arc("Document Generator",
                                      ["ops/doc_gen.py", "ops/templater.py"]))

    archived = _scanner._archive_platform_file_only_stubs(caps)

    archived_stems = sorted(a.split(" ", 1)[0] for a in archived)
    assert archived_stems == ["i-0b78ebf9", "i-5cd78794", "infrastructure-health"], (
        f"expected exactly the 3 phantoms archived, got {archived}")

    # Every survivor's file is still on disk.
    survivors = [
        "app-budget-management", "app-communication-hub", "app-dropbox-sync",
        "app-property-management", "app-task-management", "daily-briefing",
        "i-2da5ca68", "i-3cde39b9", "i-512954ad", "i-6c9c905d", "i-6d27dde6",
    ]
    for stem in survivors:
        assert (caps / f"{stem}.json").exists(), f"survivor {stem} was wrongly archived"
    # The 3 phantoms are gone from the live dir.
    for stem in ("i-0b78ebf9", "i-5cd78794", "infrastructure-health"):
        assert not (caps / f"{stem}.json").exists()
