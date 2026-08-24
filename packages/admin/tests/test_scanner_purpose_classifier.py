"""Purpose/fit classifier (app-scan bite D1) — goal-application vs capability.

Offline + deterministic: the LLM call is the injected ``llm_fn`` seam; no test
ever touches the network. The eval fixture mirrors anonymized live personal-bot
manifest shapes (the Google-as-capability false positive the deterministic
floor cannot catch, the real goal-applications that must stay on the page) plus
the conservative-default and over-route guards.

Definition source: docs/applications-vs-skills.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import purpose_classifier as pc  # noqa: E402


# ════════════════════════════════════════════════════════════════════════════
# Stub seam — a "competent model" keyed on the app name parsed out of OUR prompt
# ════════════════════════════════════════════════════════════════════════════
#
# Keying on the extracted Name (rather than the whole prompt, which embeds the
# worked examples) tests two things at once: (1) our feature extraction surfaced
# the app's real identity into the prompt, and (2) our decision rule maps the
# model's verdict to the persisted label. What a real tier2 model *would* answer
# for each shape is the fixture's premise; this asserts our code's contract.


def _extract_classify_name(prompt: str) -> str:
    """Pull the ``Name:`` line out of the THE THING TO CLASSIFY section."""
    seg = prompt.split("THE THING TO CLASSIFY:", 1)[-1]
    for line in seg.splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            return line[len("Name:"):].strip()
    return ""


def _make_stub(verdicts_by_name: dict[str, tuple[str, float]]):
    """Return an llm_fn that answers per-app from ``verdicts_by_name``.

    Default for an unmapped name is a confident application verdict — the
    conservative direction, so a fixture gap can never silently produce a
    false 'capability'.
    """
    def stub(model: str, prompt: str, api_key: str) -> str:
        name = _extract_classify_name(prompt)
        kind, conf = verdicts_by_name.get(name, ("application", 0.9))
        return json.dumps(
            {"kind": kind, "confidence": conf, "rationale": f"verdict for {name}"}
        )
    return stub


def _classify(manifest, stub, *, bound_spec=None, extra_files=None):
    return pc.classify_app_kind(
        manifest,
        llm_fn=stub,
        model="resolved-tier2-model",
        api_key="sk-fake",
        bound_spec=bound_spec,
        extra_files=extra_files,
        model_tier="tier2",
    )


# ════════════════════════════════════════════════════════════════════════════
# Anonymized live personal-bot manifest shapes
# ════════════════════════════════════════════════════════════════════════════

# Google Services Integration — a v7-arc Instance (objective lives on the bound
# Spec; realized_files on the Instance). The exact shape that survives the floor
# (real owned .py + credentials) but is a bare CAPABILITY: "access Google APIs".
GOOGLE_INSTANCE = {
    "instance_id": "i-3cde39b9",
    "manifest_shape": "v7-arc",
    "provenance": {"spec_id": "google-services-integration", "spec_version": "2026.06.01-1.0"},
    "realized_files": [
        {"path": "oauth_setup.py"},
        {"path": "oauth_local.py"},
        {"path": "oauth_get_url.py"},
        {"path": "test_google_credentials.py"},
        {"path": "google_api_integration.py"},
        {"path": "sheets_integration.py"},
        {"path": "credentials/client_secret.json"},
        {"path": "credentials/token.json"},
    ],
}
GOOGLE_SPEC = {
    "spec_id": "google-services-integration",
    "name": "Google Services Integration",
    "objective": "OAuth-based integration with Google APIs (Sheets/Docs/Drive)",
    "description": "Authenticate and call Google Sheets/Docs/Drive APIs.",
}

# Communication & Messaging Hub — 12 owned slack_*.py implementing ranch-message
# receiving/queueing/routing/summarization. A real goal-APPLICATION.
COMMS_HUB = {
    "id": "app-communication-hub",
    "name": "Communication & Messaging Hub",
    "objective": "Receive, queue, route and summarize ranch messages across channels.",
    "files": [
        {"path": "communication_hub.py"},
        {"path": "slack_receive.py"},
        {"path": "slack_queue.py"},
        {"path": "slack_router.py"},
        {"path": "slack_summarize.py"},
    ],
}

# Dropbox Synchronization — keeps a KB index current. Narrow goal → APPLICATION
# (borderline; either label acceptable per the chip — we must not over-route it).
DROPBOX_SYNC = {
    "id": "app-dropbox-sync",
    "name": "Dropbox Synchronization",
    "objective": "Keep the knowledge-base index current from Dropbox.",
    "files": [
        {"path": "update_dropbox_index.py"},
        {"path": "dropbox_setup.py"},
    ],
}

# The legit ranch goal-applications that MUST stay on the page.
RANCH_APPS = {
    "Task Tracker": {
        "id": "app-task-tracker", "name": "Task Tracker",
        "objective": "Track ranch tasks and deadlines and report status.",
        "files": [{"path": "task_system.py"}, {"path": "tasks.json"}],
    },
    "Property Management": {
        "id": "app-property", "name": "Property Management",
        "objective": "Track properties, leases and maintenance across the ranch.",
        "files": [{"path": "property_manager.py"}, {"path": "properties.json"}],
    },
    "Vineyard Operations": {
        "id": "app-vineyard", "name": "Vineyard Operations",
        "objective": "Plan and log vineyard operations across the season.",
        "files": [{"path": "build_vineyard_db.py"}, {"path": "vineyard.db"}],
    },
    "Stakeholder Workspace": {
        "id": "app-stakeholder", "name": "Stakeholder Workspace",
        "objective": "Coordinate stakeholder updates and shared workspace docs.",
        "files": [{"path": "stakeholder_workspace.py"}],
    },
    "Smart Budget Manager": {
        "id": "app-budget", "name": "Smart Budget Manager",
        "objective": "Track ranch spend against budget and flag overruns.",
        "files": [{"path": "budget_manager.py"}, {"path": "transactions.json"}],
    },
    "Document Generator": {
        "id": "app-docs", "name": "Document Generator",
        "objective": "Generate ranch documents from templates on request.",
        "files": [{"path": "doc_generator.py"}],
    },
    "Daily Operations Briefing": {
        "id": "app-briefing", "name": "Daily Operations Briefing",
        "objective": "Deliver a daily ranch operations briefing each morning.",
        "files": [{"path": "daily_briefing.py"}],
        "scheduled_actions": [{"description": "Send the daily ops briefing at 6 AM"}],
    },
    "Ranch Operations Hub": {
        "id": "app-ranch-ops", "name": "Ranch Operations Hub",
        "objective": "Coordinate ranch operations across herds and fields.",
        "files": [{"path": "ranch_ops.py"}, {"path": "herd.json"}],
    },
}

# Synthetic clear cases.
BARE_SMS = {
    "id": "send-sms", "name": "Send SMS",
    "objective": "Send a text message via Twilio.",
    "files": [{"path": "send_sms.py"}],
}
CLEAR_APP = {
    "id": "deadline-watch", "name": "Deadline Watch",
    "objective": "Monitor calendar and email for upcoming commitments and surface them early.",
    "files": [{"path": "deadline_watch.py"}, {"path": "commitments.json"}],
    "scheduled_actions": [{"description": "Scan for deadlines every morning"}],
}

# ── Slice 2: pod system-functions the floor keeps but that operate the RUNTIME ─
# These are the real pod phantoms the binary classifier mislabeled application
# at 0.92–0.97. Their goal is keeping the bot alive/healthy/scheduled, not a
# user outcome — the new "system" verdict is their home.
SYSTEM_PHANTOMS = {
    "Operations Automation": {
        "id": "app-ops-automation", "name": "Operations Automation",
        "objective": "Self-healing infrastructure management for the pod.",
        "files": [{"path": "cron_monitor.py"}, {"path": "gateway_selfheal.py"},
                  {"path": "kit_task_check.py"}],
        "scheduled_actions": [{"description": "Check the gateway and self-heal every 15 min"}],
    },
    "System Heartbeat Monitor": {
        "id": "app-heartbeat-mon", "name": "System Heartbeat Monitor",
        "objective": "Monitor the bot's heartbeat and liveness and restart it if it stalls.",
        "files": [{"path": "heartbeat_monitor.py"}],
        "scheduled_actions": [{"description": "Check heartbeat every minute"}],
    },
    "Heartbeat Monitoring": {
        "id": "app-heartbeat", "name": "Heartbeat Monitoring",
        "objective": "Track the agent runtime's heartbeat and alert on missed beats.",
        "files": [{"path": "heartbeat.py"}],
    },
    "Nightly Reconciliation": {
        "id": "i-a78b16ed", "name": "Nightly Reconciliation",
        "objective": "Reconcile the pod's own state and manifests each night.",
        "files": [{"path": "reconcile.py"}],
        "scheduled_actions": [{"description": "Reconcile pod state nightly at 3 AM"}],
    },
    "Cost & Usage Monitoring": {
        "id": "i-74ce430d", "name": "Cost & Usage Monitoring",
        "objective": "Aggregate the pod's own LLM spend and consolidate warnings.",
        "files": [{"path": "cost_monitor.py"}, {"path": "warning_consolidation.py"}],
        "scheduled_actions": [{"description": "Summarize pod spend and warnings daily"}],
    },
}

# Over-route guard: a USER's monitoring app. It "monitors" on a schedule — the
# exact surface that tempts a 'system' label — but its subject is the user's
# greenhouse and its beneficiary is the farmer. Must stay APPLICATION.
GREENHOUSE_MONITOR = {
    "id": "app-greenhouse", "name": "Greenhouse Temperature Monitor",
    "objective": "Monitor the greenhouse temperature and alert the farmer on excursions.",
    "files": [{"path": "greenhouse_monitor.py"}, {"path": "readings.json"}],
    "scheduled_actions": [{"description": "Read the greenhouse sensor every 10 min"}],
}


# ════════════════════════════════════════════════════════════════════════════
# Eval table: shape → (model verdict) → expected persisted label
# ════════════════════════════════════════════════════════════════════════════
#
# The model verdict column is what a competent tier2 model returns for the shape
# (the fixture's premise). The expected column is what OUR code persists after
# the conservative decision rule.

_RANCH_VERDICTS = {name: ("application", 0.9) for name in RANCH_APPS}
# The premise verdict a competent tier2 model returns for each pod phantom.
_SYSTEM_VERDICTS = {name: ("system", 0.9) for name in SYSTEM_PHANTOMS}

_EVAL = [
    # (label, manifest, bound_spec, extra_files, model_verdict, expected_kind)
    ("google-capability", GOOGLE_INSTANCE, GOOGLE_SPEC, None,
     {"Google Services Integration": ("capability", 0.93)}, "capability"),
    ("comms-hub-application", COMMS_HUB, None, None,
     {"Communication & Messaging Hub": ("application", 0.92)}, "application"),
    ("dropbox-application", DROPBOX_SYNC, None, None,
     {"Dropbox Synchronization": ("application", 0.8)}, "application"),
    ("bare-sms-capability", BARE_SMS, None, None,
     {"Send SMS": ("capability", 0.95)}, "capability"),
    ("clear-app-application", CLEAR_APP, None, None,
     {"Deadline Watch": ("application", 0.95)}, "application"),
    # Slice 2 — the over-route guard: a user's monitoring app stays application.
    ("greenhouse-application", GREENHOUSE_MONITOR, None, None,
     {"Greenhouse Temperature Monitor": ("application", 0.9)}, "application"),
]
_EVAL += [
    (f"ranch-{m['id']}", m, None, None, _RANCH_VERDICTS, "application")
    for m in RANCH_APPS.values()
]
# Slice 2 — each pod phantom the model calls "system" persists as "system".
_EVAL += [
    (f"system-{m['id']}", m, None, None, _SYSTEM_VERDICTS, "system")
    for m in SYSTEM_PHANTOMS.values()
]


@pytest.mark.parametrize(
    "label,manifest,bound_spec,extra_files,verdicts,expected",
    _EVAL,
    ids=[row[0] for row in _EVAL],
)
def test_eval_table_shape_to_label(
    label, manifest, bound_spec, extra_files, verdicts, expected
):
    block = _classify(
        manifest, _make_stub(verdicts), bound_spec=bound_spec, extra_files=extra_files
    )
    assert block is not None, f"{label}: classifier no-op'd (no block stamped)"
    assert block["kind"] == expected, (
        f"{label}: got {block['kind']!r}, expected {expected!r}"
    )


def test_eval_no_legit_app_is_labeled_non_application():
    """Regression guard mirroring the floor's two-sided tests: across the full
    legit-app set (the over-route direction), NONE may be labeled capability OR
    system — including a user's monitoring app (Greenhouse) that looks
    system-shaped."""
    legit = [COMMS_HUB, DROPBOX_SYNC, CLEAR_APP, GREENHOUSE_MONITOR] + list(RANCH_APPS.values())
    verdicts = {"Communication & Messaging Hub": ("application", 0.92),
                "Dropbox Synchronization": ("application", 0.8),
                "Deadline Watch": ("application", 0.95),
                "Greenhouse Temperature Monitor": ("application", 0.9)}
    verdicts.update(_RANCH_VERDICTS)
    stub = _make_stub(verdicts)
    for m in legit:
        block = _classify(m, stub)
        assert block is not None
        assert block["kind"] == "application", (
            f"{m['name']} wrongly labeled {block['kind']}"
        )


# ════════════════════════════════════════════════════════════════════════════
# Slice 2 — the "system" verdict (pod/agent-runtime infrastructure)
# ════════════════════════════════════════════════════════════════════════════


def test_system_at_threshold_sticks():
    stub = _make_stub({"Operations Automation": ("system", pc.SYSTEM_MIN_CONFIDENCE)})
    block = _classify(SYSTEM_PHANTOMS["Operations Automation"], stub)
    assert block["kind"] == "system"


def test_system_just_below_threshold_downgrades_to_application():
    """A 'system' verdict below SYSTEM_MIN_CONFIDENCE downgrades to application —
    the conservative direction. The raw call is preserved for audit."""
    stub = _make_stub(
        {"Operations Automation": ("system", pc.SYSTEM_MIN_CONFIDENCE - 0.01)}
    )
    block = _classify(SYSTEM_PHANTOMS["Operations Automation"], stub)
    assert block["kind"] == "application"
    assert block.get("raw_kind") == "system"


def test_system_threshold_is_higher_than_capability():
    """The over-route guard is stricter for system than capability (system
    over-routes a 'monitoring' user-app, the failure this slice most prevents)."""
    assert pc.SYSTEM_MIN_CONFIDENCE > pc.CAPABILITY_MIN_CONFIDENCE


def test_low_confidence_system_on_real_app_downgrades():
    """A real user app the model wrongly calls 'system' at modest confidence
    stays an application — the threshold protects it (Greenhouse at 0.7)."""
    stub = _make_stub({"Greenhouse Temperature Monitor": ("system", 0.7)})
    block = _classify(GREENHOUSE_MONITOR, stub)
    assert block["kind"] == "application"
    assert block.get("raw_kind") == "system"


def test_parse_verdict_accepts_system_kind():
    v = pc.parse_verdict('{"kind":"system","confidence":0.9,"rationale":"runtime"}')
    assert v == {"kind": "system", "confidence": 0.9, "rationale": "runtime"}


def test_system_in_valid_app_kinds():
    assert pc.APP_KIND_SYSTEM == "system"
    assert pc.APP_KIND_SYSTEM in pc.VALID_APP_KINDS
    assert pc.VALID_APP_KINDS == {"application", "capability", "system"}


# ════════════════════════════════════════════════════════════════════════════
# Slice 2 — classifier_version stamping + version-aware reclassification gate
# ════════════════════════════════════════════════════════════════════════════


def test_block_stamps_current_classifier_version():
    block = pc.build_classification_block(
        {"kind": "system", "confidence": 0.9, "rationale": "r"}, "tier2"
    )
    assert block["classifier_version"] == pc.CLASSIFIER_VERSION
    assert pc.CLASSIFIER_VERSION >= 2


def test_classify_stamps_version_end_to_end():
    stub = _make_stub({"Operations Automation": ("system", 0.9)})
    block = _classify(SYSTEM_PHANTOMS["Operations Automation"], stub)
    assert block["classifier_version"] == pc.CLASSIFIER_VERSION


def test_needs_reclassification_absent_block():
    """No classification block → must be judged."""
    assert pc.needs_reclassification({"id": "x", "name": "X"}) is True
    assert pc.needs_reclassification({"id": "x", "classification": {}}) is True


def test_needs_reclassification_stale_version():
    """A v1/#2899 block (no classifier_version → treated as 0) and any block
    below the current version are re-judged — this is how the live pod phantoms
    get re-judged to 'system'."""
    legacy_2899 = {"classification": {"kind": "application", "confidence": 0.95,
                                      "classified_by": pc.CLASSIFIED_BY}}
    assert pc.needs_reclassification(legacy_2899) is True
    older = {"classification": {"kind": "application",
                                "classifier_version": pc.CLASSIFIER_VERSION - 1}}
    assert pc.needs_reclassification(older) is True


def test_needs_reclassification_current_version_skipped():
    """A block at the current version is NOT re-judged (no double-spend)."""
    current = {"classification": {"kind": "system",
                                  "classifier_version": pc.CLASSIFIER_VERSION}}
    assert pc.needs_reclassification(current) is False


def test_needs_reclassification_malformed_version_rejudged():
    """A non-integer classifier_version is treated as stale (re-judged), never
    crashes the gate."""
    bad = {"classification": {"kind": "system", "classifier_version": "two"}}
    assert pc.needs_reclassification(bad) is True
    bad2 = {"classification": "not-a-dict"}
    assert pc.needs_reclassification(bad2) is True


# ════════════════════════════════════════════════════════════════════════════
# Conservative defaults — the over-route direction is the failure to prevent
# ════════════════════════════════════════════════════════════════════════════


def test_low_confidence_capability_downgrades_to_application():
    """A real app the model wrongly calls capability with LOW confidence stays
    an application — the threshold protects it."""
    stub = _make_stub({"Communication & Messaging Hub": ("capability", 0.4)})
    block = _classify(COMMS_HUB, stub)
    assert block is not None
    assert block["kind"] == "application"
    # The model's raw call is preserved for audit.
    assert block.get("raw_kind") == "capability"


def test_capability_just_below_threshold_is_application():
    stub = _make_stub({"Send SMS": ("capability", pc.CAPABILITY_MIN_CONFIDENCE - 0.01)})
    block = _classify(BARE_SMS, stub)
    assert block["kind"] == "application"


def test_capability_at_threshold_is_capability():
    stub = _make_stub({"Send SMS": ("capability", pc.CAPABILITY_MIN_CONFIDENCE)})
    block = _classify(BARE_SMS, stub)
    assert block["kind"] == "capability"


def test_llm_error_returns_none():
    def boom(model, prompt, key):
        raise RuntimeError("transient API error")
    assert _classify(GOOGLE_INSTANCE, boom, bound_spec=GOOGLE_SPEC) is None


def test_llm_garbage_returns_none():
    assert _classify(GOOGLE_INSTANCE, lambda *_: "not json", bound_spec=GOOGLE_SPEC) is None


def test_llm_empty_returns_none():
    assert _classify(GOOGLE_INSTANCE, lambda *_: "", bound_spec=GOOGLE_SPEC) is None


def test_missing_kind_returns_none():
    stub = lambda *_: json.dumps({"confidence": 0.9, "rationale": "x"})  # noqa: E731
    assert _classify(BARE_SMS, stub) is None


def test_invalid_kind_returns_none():
    stub = lambda *_: json.dumps({"kind": "neither", "confidence": 0.9})  # noqa: E731
    assert _classify(BARE_SMS, stub) is None


def test_no_api_key_returns_none():
    assert pc.classify_app_kind(
        BARE_SMS, llm_fn=_make_stub({"Send SMS": ("capability", 0.95)}),
        model="m", api_key="",
    ) is None


def test_no_signal_returns_none():
    """A manifest with only a name and no objective/description/files is nothing
    to judge — keep it an application (default), don't spend an LLM call."""
    called = {"n": 0}

    def counting(model, prompt, key):
        called["n"] += 1
        return json.dumps({"kind": "capability", "confidence": 0.99})
    assert _classify({"name": "Mystery"}, counting) is None
    assert called["n"] == 0, "should not call the LLM when there is no signal"


# ════════════════════════════════════════════════════════════════════════════
# Determinism + feature extraction + parsing units
# ════════════════════════════════════════════════════════════════════════════


def test_deterministic_repeated_runs_identical():
    stub = _make_stub({"Google Services Integration": ("capability", 0.93)})
    runs = [
        _classify(GOOGLE_INSTANCE, stub, bound_spec=GOOGLE_SPEC) for _ in range(5)
    ]
    assert all(r == runs[0] for r in runs)


def test_features_merge_instance_and_bound_spec():
    """The Google live case: objective on the Spec, realized_files on the
    Instance — both must reach the prompt features."""
    feat = pc.classification_features(GOOGLE_INSTANCE, GOOGLE_SPEC)
    assert feat["name"] == "Google Services Integration"
    assert "OAuth" in feat["objective"]
    assert "oauth_setup.py" in feat["files"]
    assert "credentials/token.json" in feat["files"]


def test_features_extra_files_supplements_at_mint():
    """At mint time files[] is empty; evidence files arrive via extra_files."""
    minted = {"name": "Task Tracker", "objective": "Track tasks."}
    feat = pc.classification_features(minted, None, extra_files=["task_system.py", "tasks.json"])
    assert feat["files"] == ["task_system.py", "tasks.json"]


def test_features_dedup_and_string_entries():
    m = {"name": "X", "objective": "o", "files": ["a.py", {"path": "a.py"}, {"path": "b.py"}]}
    feat = pc.classification_features(m)
    assert feat["files"] == ["a.py", "b.py"]


def test_prompt_embeds_classify_target_not_only_examples():
    feat = pc.classification_features(GOOGLE_INSTANCE, GOOGLE_SPEC)
    prompt = pc.build_prompt(feat)
    assert _extract_classify_name(prompt) == "Google Services Integration"
    # Worked examples are present (anchors the judgment)...
    assert "Communication & Messaging Hub" in prompt
    # ...but the target's own files are in the THING-TO-CLASSIFY section.
    seg = prompt.split("THE THING TO CLASSIFY:", 1)[-1]
    assert "sheets_integration.py" in seg


def test_parse_verdict_clamps_confidence():
    assert pc.parse_verdict('{"kind":"capability","confidence":5}')["confidence"] == 1.0
    assert pc.parse_verdict('{"kind":"capability","confidence":-3}')["confidence"] == 0.0


def test_parse_verdict_non_finite_confidence_is_zero():
    """NaN/Infinity confidence must NOT sail past the capability threshold —
    min(1.0, nan) is nan in Python. Treated as 0.0 (conservative)."""
    for token in ("NaN", "Infinity", "-Infinity"):
        v = pc.parse_verdict(f'{{"kind":"capability","confidence":{token}}}')
        assert v["confidence"] == 0.0, token


def test_non_finite_confidence_capability_downgrades_to_application():
    """End-to-end: a capability verdict with NaN confidence becomes an
    application (the over-route guard holds against pathological model output)."""
    block = _classify(BARE_SMS, lambda *_: '{"kind":"capability","confidence":NaN}')
    assert block is not None
    assert block["kind"] == "application"


def test_parse_verdict_tolerates_surrounding_text():
    v = pc.parse_verdict('Here is my answer:\n{"kind":"application","confidence":0.8}\nDone.')
    assert v == {"kind": "application", "confidence": 0.8, "rationale": ""}


def test_build_block_records_tier_and_author():
    block = pc.build_classification_block(
        {"kind": "capability", "confidence": 0.9, "rationale": "r"}, "tier2"
    )
    assert block["model_tier"] == "tier2"
    assert block["classified_by"] == pc.CLASSIFIED_BY
    assert "raw_kind" not in block  # not downgraded
