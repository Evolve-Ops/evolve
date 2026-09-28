"""
test_pod_templates_provisioner.py — Unit tests for the pod-template provisioner
(Bite 2 — the provision half).

Everything here runs against **fixtures**: synthetic ``network.json`` files in a
pytest ``tmp_path`` and in-memory ``PodTemplate`` objects. No test touches a real
``/Users/`` path or a real ``network.json``, and the per-bot planner is injected
as a lightweight fake so nothing reads an ``openclaw.json`` or shells out to sudo.

The invariants under the microscope (the auditor-grade contract):
  * ``apply`` never clobbers an existing bot or existing pod config (collision
    skip + fill-missing).
  * ``dry_run=True`` is byte-for-byte side-effect-free.
  * ``apply`` → ``apply`` is idempotent (no duplicate bots, no config drift).
  * one bot's planning failure degrades per-item; the network.json write is
    atomic (temp + ``os.replace``).

Run with:
    python3 -m pytest packages/admin/tests/test_pod_templates_provisioner.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.pod_templates import (  # noqa: E402
    PodTemplateError,
    pod_template_from_dict,
    provision_pod_template,
)


# ── Fixtures / helpers ───────────────────────────────────────────────────────────


def _pod_template(*, name="test-pod", pod=None, bots=None):
    """Build a PodTemplate in memory (no disk)."""
    raw = {
        "schema_version": 1,
        "name": name,
        "display_name": name,
        "description": "fixture pod-template",
        "pod": pod or {},
        "bots": bots or [],
    }
    return pod_template_from_dict(raw, name=name)


def _write_network(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "network.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def _read_network(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _fake_planner(*, bot_template, bot_id, vars, templates_dir):
    """Stand-in for bot_templates.build_plan — never touches the filesystem."""
    return SimpleNamespace(
        template_name=bot_template,
        bot_id=bot_id,
        vars=dict(vars),
        validation_errors=[],
        error=None,
        ok=True,
        plan=SimpleNamespace(applications=()),
    )


def _raising_planner(*, bot_template, bot_id, vars, templates_dir):
    raise RuntimeError("simulated planner blow-up")


# A baseline pod: one existing primary bot. Bot ids scrubbed to team-bot-*.
def _baseline_network() -> dict:
    return {
        "networkId": "test-pod",
        "members": ["team-bot-primary"],
        "primary": "team-bot-primary",
        "bots": {
            "team-bot-primary": {
                "role": "primary",
                "port": 19000,
                "multiUser": False,
            }
        },
        "pod": {"release": {"mode": "canary"}},
        "models": {"rungs": [{"name": "fast"}]},
        "podInvariantIntegrations": ["github"],
    }


# ── Dry-run plan correctness ─────────────────────────────────────────────────────


def test_dry_run_plans_create_and_skip(tmp_path):
    net = _write_network(tmp_path, _baseline_network())
    template = _pod_template(
        bots=[
            {"bot_id": "team-bot-primary", "bot_template": "x", "role": "primary"},
            {"bot_id": "team-bot-new", "bot_template": "morning-briefing",
             "role": "member"},
        ]
    )
    result = provision_pod_template(
        template, dry_run=True, network_path=net, plan_bot_fn=_fake_planner
    )
    assert result.ok
    assert result.applied is False
    assert [it.bot_id for it in result.created] == ["team-bot-new"]
    assert [it.bot_id for it in result.skipped] == ["team-bot-primary"]
    created = result.created[0]
    assert created.action == "create"
    assert created.port == 19001  # next free after the existing 19000
    assert created.plan is not None  # bot-template plan attached
    assert result.skipped[0].action == "skip-existing"


def test_dry_run_writes_nothing(tmp_path):
    net = _write_network(tmp_path, _baseline_network())
    before = net.read_bytes()
    template = _pod_template(
        bots=[{"bot_id": "team-bot-new", "bot_template": "x", "role": "member"}]
    )
    result = provision_pod_template(
        template, dry_run=True, network_path=net, plan_bot_fn=_fake_planner
    )
    # The plan shows a change is pending, but disk is byte-for-byte untouched...
    assert result.created
    assert net.read_bytes() == before
    # ...and no stray temp file was left behind.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "network.json"]
    assert leftovers == []


# ── Apply: create missing ────────────────────────────────────────────────────────


def test_apply_registers_missing_bot(tmp_path):
    net = _write_network(tmp_path, _baseline_network())
    template = _pod_template(
        bots=[{"bot_id": "team-bot-new", "bot_template": "x", "role": "member"}]
    )
    result = provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    assert result.applied is True
    saved = _read_network(net)
    assert saved["bots"]["team-bot-new"] == {
        "role": "member",
        "port": 19001,
        "multiUser": False,
    }
    assert "team-bot-new" in saved["members"]
    # Existing bot untouched.
    assert saved["bots"]["team-bot-primary"]["port"] == 19000
    # Atomic write left no temp file.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "network.json"]
    assert leftovers == []


def test_apply_no_changes_does_not_write(tmp_path):
    net = _write_network(tmp_path, _baseline_network())
    before = net.read_bytes()
    # Template references only the already-present bot, with a matching seed.
    template = _pod_template(
        pod={"release_mode": "canary"},
        bots=[{"bot_id": "team-bot-primary", "bot_template": "x",
               "role": "primary"}],
    )
    result = provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    assert result.created == []
    assert result.pod_config_changes == []
    assert result.applied is False  # nothing to write
    assert net.read_bytes() == before


# ── Collision: never clobber an existing bot ─────────────────────────────────────


def test_existing_bot_never_clobbered(tmp_path):
    base = _baseline_network()
    # Operator added a custom field + a non-default port to the existing bot.
    base["bots"]["team-bot-primary"]["port"] = 19042
    base["bots"]["team-bot-primary"]["customField"] = "operator-set"
    net = _write_network(tmp_path, base)
    # Template references the same id with a DIFFERENT role + a template.
    template = _pod_template(
        bots=[{"bot_id": "team-bot-primary", "bot_template": "would-clobber",
               "role": "member"}]
    )
    result = provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    saved = _read_network(net)
    entry = saved["bots"]["team-bot-primary"]
    assert entry["role"] == "primary"          # NOT downgraded to member
    assert entry["port"] == 19042              # NOT reset
    assert entry["customField"] == "operator-set"  # preserved
    assert result.skipped[0].action == "skip-existing"
    assert result.created == []


def test_planned_bot_is_skipped_not_graduated(tmp_path):
    base = _baseline_network()
    base["bots"]["team-bot-planned"] = {"purpose": "a planned-but-undeployed bot"}
    net = _write_network(tmp_path, base)
    template = _pod_template(
        bots=[{"bot_id": "team-bot-planned", "bot_template": "x",
               "role": "member"}]
    )
    result = provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    saved = _read_network(net)
    # Planned block left exactly as-is — graduation is the wizard's job.
    assert saved["bots"]["team-bot-planned"] == {"purpose": "a planned-but-undeployed bot"}
    assert result.skipped[0].action == "skip-planned"
    assert result.created == []


# ── Pod-wide seed: fill-missing vs overwrite ─────────────────────────────────────


def test_seed_fill_missing_does_not_overwrite(tmp_path):
    base = _baseline_network()
    base["pod"]["release"]["mode"] = "direct"   # operator chose direct
    base["models"] = {"rungs": [{"name": "operator-rung"}]}  # operator models
    net = _write_network(tmp_path, base)
    template = _pod_template(
        pod={
            "release_mode": "canary",  # different — must NOT overwrite
            "model_tiers": {
                "rungs": [{"name": "template-rung"}],  # present key — preserve
                "roles": {"fast": "haiku"},            # missing key — add
            },
        }
    )
    result = provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    saved = _read_network(net)
    # release.mode untouched.
    assert saved["pod"]["release"]["mode"] == "direct"
    # models.rungs preserved; models.roles added.
    assert saved["models"]["rungs"] == [{"name": "operator-rung"}]
    assert saved["models"]["roles"] == {"fast": "haiku"}
    paths = {c.path for c in result.pod_config_changes}
    assert "models.roles" in paths
    assert "pod.release.mode" not in paths
    assert "models.rungs" not in paths


def test_seed_overwrite_replaces_blocks(tmp_path):
    base = _baseline_network()
    base["pod"]["release"]["mode"] = "direct"
    base["models"] = {"rungs": [{"name": "operator-rung"}]}
    net = _write_network(tmp_path, base)
    template = _pod_template(
        pod={
            "release_mode": "canary",
            "model_tiers": {"rungs": [{"name": "template-rung"}]},
        }
    )
    result = provision_pod_template(
        template, dry_run=False, network_path=net, overwrite_pod_config=True,
        plan_bot_fn=_fake_planner,
    )
    saved = _read_network(net)
    assert saved["pod"]["release"]["mode"] == "canary"
    assert saved["models"] == {"rungs": [{"name": "template-rung"}]}
    actions = {(c.path, c.action) for c in result.pod_config_changes}
    assert ("pod.release.mode", "replace") in actions
    assert ("models", "replace") in actions


def test_seed_fills_into_empty_pod(tmp_path):
    net = _write_network(tmp_path, {"networkId": "fresh", "bots": {}})
    template = _pod_template(
        pod={
            "release_mode": "canary",
            "model_tiers": {"rungs": [{"name": "fast"}]},
            "integrations": {"github": {"scope": "pod"}},
        }
    )
    provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    saved = _read_network(net)
    assert saved["pod"]["release"]["mode"] == "canary"
    assert saved["models"] == {"rungs": [{"name": "fast"}]}
    assert saved["podInvariantIntegrations"] == ["github"]


def test_existing_integration_list_not_overwritten(tmp_path):
    base = _baseline_network()  # podInvariantIntegrations = ["github"]
    net = _write_network(tmp_path, base)
    template = _pod_template(
        pod={"integrations": {"slack": {"scope": "pod"}}}
    )
    result = provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    saved = _read_network(net)
    # Non-empty existing list is atomic — fill-missing leaves it alone.
    assert saved["podInvariantIntegrations"] == ["github"]
    assert all(
        c.path != "podInvariantIntegrations" for c in result.pod_config_changes
    )


def test_bot_scope_integration_surfaces_manual_warning(tmp_path):
    net = _write_network(tmp_path, {"networkId": "fresh", "bots": {}})
    template = _pod_template(
        pod={"integrations": {"notion": {"scope": "bot", "bots": ["team-bot-a"]}}}
    )
    result = provision_pod_template(
        template, dry_run=True, network_path=net, plan_bot_fn=_fake_planner
    )
    assert any("manual: connect integration 'notion'" in w for w in result.warnings)


# ── No-template bots ─────────────────────────────────────────────────────────────


def test_no_template_bot_with_apps_warns_manual(tmp_path):
    net = _write_network(tmp_path, _baseline_network())
    template = _pod_template(
        bots=[{"bot_id": "team-bot-new", "bot_template": None,
               "installed_apps": ["app_a", "app_b"], "role": "member"}]
    )
    result = provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    item = result.created[0]
    assert item.plan is None
    assert any("manual: install apps" in w for w in item.warnings)
    assert any("app_a" in w for w in item.warnings)
    # Still registered.
    assert _read_network(net)["bots"]["team-bot-new"]["role"] == "member"


def test_no_template_bot_no_apps_registers_bare(tmp_path):
    net = _write_network(tmp_path, _baseline_network())
    template = _pod_template(
        bots=[{"bot_id": "team-bot-bare", "bot_template": None, "role": "member"}]
    )
    result = provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    item = result.created[0]
    assert any("bare roster member" in w for w in item.warnings)


# ── Idempotency ──────────────────────────────────────────────────────────────────


def test_apply_twice_is_idempotent(tmp_path):
    net = _write_network(tmp_path, _baseline_network())
    template = _pod_template(
        pod={"model_tiers": {"roles": {"fast": "haiku"}}},
        bots=[
            {"bot_id": "team-bot-primary", "bot_template": "x", "role": "primary"},
            {"bot_id": "team-bot-new", "bot_template": "morning-briefing",
             "role": "member"},
        ],
    )
    r1 = provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    assert r1.applied is True
    assert [it.bot_id for it in r1.created] == ["team-bot-new"]
    after_first = net.read_bytes()

    # Second apply: nothing new to do.
    r2 = provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    assert r2.created == []
    assert r2.pod_config_changes == []
    assert r2.applied is False
    # No drift: file identical after the second run.
    assert net.read_bytes() == after_first
    # And exactly one entry for the created bot (no duplicate).
    saved = _read_network(net)
    assert list(saved["bots"]).count("team-bot-new") == 1
    assert saved["members"].count("team-bot-new") == 1


def test_dry_run_then_apply_consistent(tmp_path):
    net = _write_network(tmp_path, _baseline_network())
    template = _pod_template(
        bots=[{"bot_id": "team-bot-new", "bot_template": "x", "role": "member"}]
    )
    dry = provision_pod_template(
        template, dry_run=True, network_path=net, plan_bot_fn=_fake_planner
    )
    applied = provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    assert [it.bot_id for it in dry.created] == [it.bot_id for it in applied.created]
    assert dry.created[0].port == applied.created[0].port


# ── Per-item degradation + atomicity ─────────────────────────────────────────────


def test_one_bot_planner_failure_does_not_abort(tmp_path):
    net = _write_network(tmp_path, _baseline_network())

    def _mixed_planner(*, bot_template, bot_id, vars, templates_dir):
        if bot_id == "team-bot-broken":
            raise RuntimeError("boom")
        return _fake_planner(
            bot_template=bot_template, bot_id=bot_id, vars=vars,
            templates_dir=templates_dir,
        )

    template = _pod_template(
        bots=[
            {"bot_id": "team-bot-broken", "bot_template": "bad", "role": "member"},
            {"bot_id": "team-bot-ok", "bot_template": "good", "role": "member"},
        ]
    )
    result = provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_mixed_planner
    )
    # Both registered; broken bot carries the failure as a warning, not an abort.
    ids = {it.bot_id for it in result.created}
    assert ids == {"team-bot-broken", "team-bot-ok"}
    broken = next(it for it in result.created if it.bot_id == "team-bot-broken")
    assert any("could not build bot-template plan" in w for w in broken.warnings)
    # network.json written with BOTH new bots — no half-write.
    saved = _read_network(net)
    assert "team-bot-broken" in saved["bots"]
    assert "team-bot-ok" in saved["bots"]


def test_multiple_new_bots_get_distinct_ports(tmp_path):
    net = _write_network(tmp_path, _baseline_network())  # existing port 19000
    template = _pod_template(
        bots=[
            {"bot_id": "team-bot-b", "bot_template": None, "role": "member"},
            {"bot_id": "team-bot-c", "bot_template": None, "role": "member"},
        ]
    )
    result = provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    ports = sorted(it.port for it in result.created)
    assert ports == [19001, 19002]
    saved = _read_network(net)
    assert saved["bots"]["team-bot-b"]["port"] != saved["bots"]["team-bot-c"]["port"]


def test_primary_pointer_not_clobbered(tmp_path):
    net = _write_network(tmp_path, _baseline_network())  # primary already set
    template = _pod_template(
        bots=[{"bot_id": "team-bot-new-primary", "bot_template": None,
               "role": "primary"}]
    )
    result = provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    saved = _read_network(net)
    assert saved["primary"] == "team-bot-primary"  # NOT re-homed
    item = result.created[0]
    assert any("primary is already" in w for w in item.warnings)


def test_string_port_on_existing_bot_no_collision(tmp_path):
    # A migrated/hand-edited entry whose port is a numeric *string* must still be
    # counted as used — an int-only guard would hand the new bot a colliding port.
    base = _baseline_network()
    base["bots"]["team-bot-primary"]["port"] = "19000"  # string, not int
    net = _write_network(tmp_path, base)
    template = _pod_template(
        bots=[{"bot_id": "team-bot-new", "bot_template": None, "role": "member"}]
    )
    result = provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    assert result.created[0].port == 19001  # NOT 19000
    saved = _read_network(net)
    assert saved["bots"]["team-bot-new"]["port"] == 19001


def test_seed_extra_keys_warn_not_silently_dropped(tmp_path):
    net = _write_network(tmp_path, {"networkId": "fresh", "bots": {}})
    # An unknown pod-wide key the loader preserves into PodSeed.extra.
    template = _pod_template(
        pod={"release_mode": "canary", "future_pod_knob": {"x": 1}}
    )
    result = provision_pod_template(
        template, dry_run=True, network_path=net, plan_bot_fn=_fake_planner
    )
    assert any("future_pod_knob" in w for w in result.warnings)


def test_primary_pointer_filled_when_absent(tmp_path):
    net = _write_network(tmp_path, {"networkId": "fresh", "bots": {}})
    template = _pod_template(
        bots=[{"bot_id": "team-bot-lead", "bot_template": None, "role": "primary"}]
    )
    provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    assert _read_network(net)["primary"] == "team-bot-lead"


# ── Validation gate ──────────────────────────────────────────────────────────────


def test_invalid_template_with_secret_refused_no_write(tmp_path):
    net = _write_network(tmp_path, _baseline_network())
    before = net.read_bytes()
    # A leaked GitHub PAT under a bot override → validator's secret sweep errors.
    template = _pod_template(
        bots=[{
            "bot_id": "team-bot-leaky",
            "bot_template": "x",
            "role": "member",
            "overrides": {"api_token": "ghp_" + "a" * 36},
        }]
    )
    result = provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    assert result.ok is False
    assert result.validation_errors
    assert result.applied is False
    assert result.created == []
    assert net.read_bytes() == before  # refused → nothing written


# ── network.json IO edge cases ───────────────────────────────────────────────────


def test_missing_network_file_treated_as_fresh_pod(tmp_path):
    net = tmp_path / "network.json"  # does not exist
    template = _pod_template(
        bots=[{"bot_id": "team-bot-a", "bot_template": None, "role": "primary"}]
    )
    result = provision_pod_template(
        template, dry_run=False, network_path=net, plan_bot_fn=_fake_planner
    )
    assert result.applied is True
    assert net.exists()
    assert _read_network(net)["bots"]["team-bot-a"]["port"] == 19000


def test_malformed_network_json_raises(tmp_path):
    net = tmp_path / "network.json"
    net.write_text("[not, an, object]", encoding="utf-8")
    template = _pod_template(bots=[])
    with pytest.raises(PodTemplateError):
        provision_pod_template(
            template, dry_run=True, network_path=net, plan_bot_fn=_fake_planner
        )


def test_bots_not_a_mapping_raises(tmp_path):
    net = _write_network(tmp_path, {"networkId": "x", "bots": ["not", "a", "map"]})
    template = _pod_template(
        bots=[{"bot_id": "team-bot-a", "bot_template": None, "role": "member"}]
    )
    with pytest.raises(PodTemplateError):
        provision_pod_template(
            template, dry_run=True, network_path=net, plan_bot_fn=_fake_planner
        )


def test_summary_lines_render(tmp_path):
    net = _write_network(tmp_path, _baseline_network())
    template = _pod_template(
        bots=[
            {"bot_id": "team-bot-primary", "bot_template": "x", "role": "primary"},
            {"bot_id": "team-bot-new", "bot_template": "y", "role": "member"},
        ]
    )
    result = provision_pod_template(
        template, dry_run=True, network_path=net, plan_bot_fn=_fake_planner
    )
    text = "\n".join(result.summary_lines())
    assert "DRY-RUN" in text
    assert "team-bot-new" in text
    assert "team-bot-primary" in text
    assert "--apply" in text  # next-step hint
