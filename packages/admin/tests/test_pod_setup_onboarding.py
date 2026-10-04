"""Unit tests for the pod-wide checks added to better_engine.onboarding (Phase A).

The existing ONBOARDING_TASKS list grew six new entries that surface as a
pod-scoped Setup checklist on the Getting Started page + Overview chip.
Each new task has a `check(state)` callable that reads from the
build_onboarding_state() bundle. Tests cover both:

  * The state bundle correctly extracts each pod-wide signal from
    network.json and the shared dir.
  * Each new check callable returns the right verdict given a state
    dict shape.

The seven pre-existing tasks (add_first_bot, run_health_check, scan_X,
review_X, configure_spend_threshold, configure_pod_reports,
first_security_audit) are covered by test_better_engine_tier1.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.better_engine.onboarding import (  # noqa: E402
    ONBOARDING_TASKS,
    build_onboarding_state,
)


# ── State bundle: primary_installed ───────────────────────────────────────


def test_primary_installed_false_when_network_has_no_primary(tmp_path):
    network = {"members": [], "bots": {}, "primary": None}
    state = build_onboarding_state(tmp_path, network)
    assert state["primary_installed"] is False


def test_primary_installed_false_when_primary_not_in_bots(tmp_path):
    """primary names a bot id but that id isn't in bots — partial bootstrap."""
    network = {"members": ["evo"], "bots": {}, "primary": "evo"}
    state = build_onboarding_state(tmp_path, network)
    assert state["primary_installed"] is False


def test_primary_installed_true_when_primary_registered(tmp_path):
    network = {
        "members": ["evo"],
        "bots": {"evo": {"role": "primary", "port": 19000}},
        "primary": "evo",
    }
    state = build_onboarding_state(tmp_path, network)
    assert state["primary_installed"] is True


# ── State bundle: pod_admins_claimed ──────────────────────────────────────


def test_pod_admins_claimed_false_when_admins_block_missing(tmp_path):
    network = {"primary": None, "bots": {}}
    state = build_onboarding_state(tmp_path, network)
    assert state["pod_admins_claimed"] is False


def test_pod_admins_claimed_false_when_all_lists_empty(tmp_path):
    network = {
        "primary": None,
        "bots": {},
        "pod": {"admins": {"external_ids": {}, "pod_users": []}},
    }
    state = build_onboarding_state(tmp_path, network)
    assert state["pod_admins_claimed"] is False


def test_pod_admins_claimed_true_when_external_id_present(tmp_path):
    network = {
        "primary": None,
        "bots": {},
        "pod": {"admins": {"external_ids": {"telegram": ["12345"]}, "pod_users": []}},
    }
    state = build_onboarding_state(tmp_path, network)
    assert state["pod_admins_claimed"] is True


def test_pod_admins_claimed_true_when_pod_user_present(tmp_path):
    network = {
        "primary": None,
        "bots": {},
        "pod": {"admins": {"external_ids": {}, "pod_users": ["pod-admin-user"]}},
    }
    state = build_onboarding_state(tmp_path, network)
    assert state["pod_admins_claimed"] is True


# ── Review M4 regression: stricter filter on stale-empty entries ──────────


def test_pod_admins_claimed_false_for_empty_string_channel_entries(tmp_path):
    """A per-channel list with only empty strings (legacy migration
    leftover) must NOT count as claimed. Pre-fix `any(ext_ids.values())`
    returned True for {"telegram": [""]} because the list itself is
    truthy."""
    network = {
        "primary": None, "bots": {},
        "pod": {"admins": {"external_ids": {"telegram": [""]}, "pod_users": []}},
    }
    state = build_onboarding_state(tmp_path, network)
    assert state["pod_admins_claimed"] is False


def test_pod_admins_claimed_false_for_none_entries(tmp_path):
    network = {
        "primary": None, "bots": {},
        "pod": {"admins": {"external_ids": {"telegram": [None]}, "pod_users": []}},
    }
    state = build_onboarding_state(tmp_path, network)
    assert state["pod_admins_claimed"] is False


def test_pod_admins_claimed_false_for_whitespace_only_pod_users(tmp_path):
    network = {
        "primary": None, "bots": {},
        "pod": {"admins": {"external_ids": {}, "pod_users": ["  ", ""]}},
    }
    state = build_onboarding_state(tmp_path, network)
    assert state["pod_admins_claimed"] is False


def test_pod_admins_claimed_true_with_one_real_id_among_empties(tmp_path):
    """Mixed entries — one real id alongside empties still counts."""
    network = {
        "primary": None, "bots": {},
        "pod": {"admins": {
            "external_ids": {"telegram": ["", None, "12345"]},
            "pod_users": [],
        }},
    }
    state = build_onboarding_state(tmp_path, network)
    assert state["pod_admins_claimed"] is True


# ── 2026-06-04 follow-up: setup-checklist scope cleanup ──────────────────


def test_setup_checklist_no_longer_includes_obsolete_tasks():
    """Operator review: three tasks didn't belong on the pod-setup
    checklist:

      * first_security_audit — proposal flow is system output, not
        operator setup; also kept showing pending forever on a real pod.
      * pod_conduct_authored — POD_CONDUCT.md is repo-distributed and
        NOT editable via the admin UI; nagging operators to "author" a
        file they can't edit is wrong.
      * first_gallery_app — installing an app is "using the pod," not
        "setting up the pod."

    All three removed entirely from ONBOARDING_TASKS in this cleanup.
    """
    removed = {
        "first_security_audit",
        "pod_conduct_authored",
        "first_gallery_app",
    }
    present = {t.id for t in ONBOARDING_TASKS}
    overlap = removed & present
    assert not overlap, f"these tasks should be removed: {sorted(overlap)}"


def test_pod_tiers_configured_task_replaces_them():
    """New task added in the same cleanup: the pod's AI tier config
    (primary bot's evolve-tiers.json resolving a tier2 model binding; the
    former tier0/judge requirement died with the judge-role collapse).
    Operator-requested: 'tiers in AI optimization' belongs on the pod
    setup checklist."""
    task = next(
        (t for t in ONBOARDING_TASKS if t.id == "pod_tiers_configured"),
        None,
    )
    assert task is not None, "pod_tiers_configured task missing"
    assert task.per_bot is False
    assert "scope:pod" in task.tags
    # Action routes to AI Optimization page where tiers are set
    assert task.action == "open_ai_optimization"


def test_pod_tiers_configured_false_without_primary(tmp_path):
    """No primary bot installed → no pod-tier config possible."""
    network = {"primary": None, "bots": {}, "members": []}
    state = build_onboarding_state(tmp_path, network)
    assert state["pod_tiers_configured"] is False


def test_pod_tiers_configured_false_when_tiers_file_missing(tmp_path, monkeypatch):
    """Primary installed but never visited AI Optimization → no tiers
    file → pending."""
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bot, net=None: tmp_path / "Users" / bot,
    )
    network = {
        "primary": "evo",
        "bots": {"evo": {"role": "primary", "port": 19000}},
        "members": ["evo"],
    }
    state = build_onboarding_state(tmp_path, network)
    assert state["pod_tiers_configured"] is False


def test_pod_tiers_configured_true_when_primary_has_tier2_and_stale_tier0(
        tmp_path, monkeypatch):
    """Happy path: primary's evolve-tiers.json has tier2 (workhorse); a stale
    legacy tier0 entry is read-compat noise (the judge role is gone) and must
    not affect the verdict."""
    import json as _json
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bot, net=None: tmp_path / "Users" / bot,
    )
    primary_oc = tmp_path / "Users" / "evo" / ".openclaw"
    primary_oc.mkdir(parents=True)
    (primary_oc / "evolve-tiers.json").write_text(_json.dumps({
        "tiers": {
            "tier2": {"models": ["anthropic/claude-sonnet-4.5"]},
            "tier0": {"models": ["openai/gpt-4o"]},
        },
    }))
    network = {
        "primary": "evo",
        "bots": {"evo": {"role": "primary", "port": 19000}},
        "members": ["evo"],
    }
    state = build_onboarding_state(tmp_path, network)
    assert state["pod_tiers_configured"] is True


def test_pod_tiers_configured_true_via_defaults_when_only_tier2_set(
        tmp_path, monkeypatch):
    """Workhorse picked → configured-via-defaults.

    Post-Addendum-2 (#2561): the DEFAULT_MODEL_CATALOG ships in code as the
    base layer of the keyed merge. A fresh pod works out of the box —
    onboarding must NOT nag the operator to pick a tier the defaults already
    satisfy. Only an EXPLICIT-but-empty tier flags (see the ai_optim_tiers
    broken-config tests); an omitted one does not. (Only tier2 is required —
    the tier0/judge half of this check died with the judge-role collapse.)
    """
    import json as _json
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bot, net=None: tmp_path / "Users" / bot,
    )
    primary_oc = tmp_path / "Users" / "evo" / ".openclaw"
    primary_oc.mkdir(parents=True)
    (primary_oc / "evolve-tiers.json").write_text(_json.dumps({
        "tiers": {
            "tier2": {"models": ["anthropic/claude-sonnet-4.5"]},
        },
    }))
    network = {
        "primary": "evo",
        "bots": {"evo": {"role": "primary", "port": 19000}},
        "members": ["evo"],
    }
    state = build_onboarding_state(tmp_path, network)
    assert state["pod_tiers_configured"] is True


# ── State bundle: https_enabled ───────────────────────────────────────────


def test_https_enabled_false_when_no_admin_url(tmp_path):
    network = {"primary": None, "bots": {}}
    state = build_onboarding_state(tmp_path, network)
    assert state["https_enabled"] is False


def test_https_enabled_false_when_http_scheme(tmp_path):
    network = {"primary": None, "bots": {}, "adminBaseUrl": "http://pod-admin-user.example.com:5050"}
    state = build_onboarding_state(tmp_path, network)
    assert state["https_enabled"] is False


def test_https_enabled_true_when_https_scheme(tmp_path):
    network = {"primary": None, "bots": {}, "adminBaseUrl": "https://pod.tailnet.ts.net"}
    state = build_onboarding_state(tmp_path, network)
    assert state["https_enabled"] is True


# pod_conduct_authored state-bundle signal removed 2026-06-04 — see
# `test_setup_checklist_no_longer_includes_obsolete_tasks` for context.
# POD_CONDUCT.md is repo-distributed, not editable from the admin UI.


# ── State bundle: github_dev_pat_configured ───────────────────────────────


def test_github_dev_pat_false_when_intake_missing(tmp_path):
    network = {"primary": None, "bots": {}}
    state = build_onboarding_state(tmp_path, network)
    assert state["github_dev_pat_configured"] is False


def test_github_dev_pat_true_for_v1_shape(tmp_path):
    """Legacy single-target shape: owner+repo at top level of intake.github."""
    network = {
        "primary": None, "bots": {},
        "intake": {"github": {"owner": "ops", "repo": "evolve", "token_slot": "github_intake"}},
    }
    state = build_onboarding_state(tmp_path, network)
    assert state["github_dev_pat_configured"] is True


def test_github_dev_pat_true_for_v2_shape(tmp_path):
    """Multi-target shape: intake.github.targets non-empty."""
    network = {
        "primary": None, "bots": {},
        "intake": {"github": {
            "default": "evolve",
            "targets": {"evolve": {"owner": "ops", "repo": "evolve"}},
        }},
    }
    state = build_onboarding_state(tmp_path, network)
    assert state["github_dev_pat_configured"] is True


def test_github_dev_pat_false_when_v2_targets_empty_dict(tmp_path):
    network = {
        "primary": None, "bots": {},
        "intake": {"github": {"targets": {}}},
    }
    state = build_onboarding_state(tmp_path, network)
    assert state["github_dev_pat_configured"] is False


# first_gallery_app_installed state-bundle signal removed 2026-06-04
# along with the corresponding task — operator review classified app
# install as "using the pod," not "setting up the pod."


# ── Task-registry shape ──────────────────────────────────────────────────


def test_pod_tasks_registered():
    """Every pod-wide task is in the registry with the expected id.
    Updated 2026-06-04: dropped pod_conduct_authored + first_gallery_app
    (operator review); added pod_tiers_configured."""
    ids = {t.id for t in ONBOARDING_TASKS}
    expected = {
        "primary_installed",
        "pod_admins_claimed",
        "https_enabled",
        "github_dev_pat",
        "pod_tiers_configured",
    }
    assert expected.issubset(ids), f"missing: {expected - ids}"


def test_pod_tasks_carry_scope_pod_tag():
    """All pod tasks are pod-scoped — tag matters for the UI filter that
    splits 'pod setup' from 'per-bot setup' rows on the Getting Started
    page."""
    new_ids = {
        "primary_installed", "pod_admins_claimed", "https_enabled",
        "github_dev_pat", "pod_tiers_configured", "messaging_channel_installed",
    }
    for task in ONBOARDING_TASKS:
        if task.id in new_ids:
            assert "scope:pod" in task.tags, f"{task.id} missing scope:pod tag"


def test_pod_tasks_each_have_actionable_deeplink():
    """Each pod task must declare a non-empty action string. The UI uses
    this to route the Go button (open page / launch wizard). An empty
    action would render a row with no clear next step."""
    new_ids = {
        "primary_installed", "pod_admins_claimed", "https_enabled",
        "github_dev_pat", "pod_tiers_configured", "messaging_channel_installed",
    }
    for task in ONBOARDING_TASKS:
        if task.id in new_ids:
            assert task.action, f"{task.id} has empty action"


def test_pod_tasks_are_not_per_bot():
    """All pod tasks are pod-wide; per_bot must be False so they don't
    get instantiated per-member (which would create N copies of 'install
    primary')."""
    new_ids = {
        "primary_installed", "pod_admins_claimed", "https_enabled",
        "github_dev_pat", "pod_tiers_configured", "messaging_channel_installed",
    }
    for task in ONBOARDING_TASKS:
        if task.id in new_ids:
            assert task.per_bot is False, f"{task.id} should be pod-scoped"


def test_messaging_channel_installed_false_with_no_members(tmp_path):
    """Empty pod → no members to check → channel-installed is False."""
    network = {"primary": None, "bots": {}, "members": []}
    state = build_onboarding_state(tmp_path, network)
    assert state["messaging_channel_installed"] is False


def test_messaging_channel_installed_false_when_no_pairing_files(
        tmp_path, monkeypatch):
    """Bots registered but no channel credentials → False."""
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bot, net=None: tmp_path / "Users" / bot,
    )
    network = {
        "primary": "evo",
        "bots": {"evo": {"role": "primary"}},
        "members": ["evo"],
    }
    state = build_onboarding_state(tmp_path, network)
    assert state["messaging_channel_installed"] is False


def test_messaging_channel_installed_true_when_any_bot_has_pairing_file(
        tmp_path, monkeypatch):
    """Slack pairing.json on the primary bot → channel installed."""
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bot, net=None: tmp_path / "Users" / bot,
    )
    creds = tmp_path / "Users" / "evo" / ".openclaw" / "credentials"
    creds.mkdir(parents=True)
    (creds / "slack-pairing.json").write_text('{"version": 1, "requests": []}')
    network = {
        "primary": "evo",
        "bots": {"evo": {"role": "primary"}},
        "members": ["evo"],
    }
    state = build_onboarding_state(tmp_path, network)
    assert state["messaging_channel_installed"] is True


def test_messaging_channel_installed_true_for_any_of_four_channels(
        tmp_path, monkeypatch):
    """Detector accepts telegram / slack / discord / whatsapp pairing
    files. Any one of them on any one bot flips the signal True."""
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bot, net=None: tmp_path / "Users" / bot,
    )
    for channel in ("telegram", "slack", "discord", "whatsapp"):
        # Fresh tmp_path for each channel — exercise one at a time
        sub_creds = tmp_path / "Users" / channel / ".openclaw" / "credentials"
        sub_creds.mkdir(parents=True)
        (sub_creds / f"{channel}-pairing.json").write_text("{}")
        network = {
            "primary": channel,
            "bots": {channel: {}}, "members": [channel],
        }
        state = build_onboarding_state(tmp_path, network)
        assert state["messaging_channel_installed"] is True, f"{channel} should pass"


def test_pod_tasks_check_fns_consult_state_bundle():
    """Each pod task's check(state) reads from the correct state key.
    Smoke-test: a state bundle with the matching flag flips the check
    True; a state without it stays False."""
    task_by_id = {t.id: t for t in ONBOARDING_TASKS}
    mapping = {
        "primary_installed": "primary_installed",
        "pod_admins_claimed": "pod_admins_claimed",
        "https_enabled": "https_enabled",
        "github_dev_pat": "github_dev_pat_configured",
        "pod_tiers_configured": "pod_tiers_configured",
        "messaging_channel_installed": "messaging_channel_installed",
    }
    for task_id, state_key in mapping.items():
        task = task_by_id[task_id]
        assert task.check({state_key: True}) is True, f"{task_id} check should be True"
        assert task.check({state_key: False}) is False, f"{task_id} check should be False"
        assert task.check({}) is False, f"{task_id} check on empty state should be False"
