"""The easy-setup wizard's Custom flip must not silently kill auto-upgrade.

#3566 audit finding E-1.

``routes_easy_setup``'s bot scope writes ``{"rungs", "roles", "roleCaps"}``
wholesale — no ``tiers`` key. Carrying ``rungs`` is what makes a bot Custom
(``primary_bot.bot_has_custom_tiers``), and ``model_auto_upgrade.bot_policy``
does NOT give a Custom bot the pod's ``enabled``: a Custom bot that never set
the key resolves to the CODE default, ``false`` (lifecycle rule 1,
spec-model-auto-upgrade-2026-07-30 §Scope). Both carry mechanisms used to be
gated on a ``tiers`` key, so the wizard flipped bots to Custom with auto-upgrade
resolving to ``false`` while the pod said ``true``.

**Every assertion here goes through ``bot_policy``, never through key
presence/absence.** Key-absence and "follows the pod" coincide only while the
bot is non-Custom — which is exactly the premise this bug breaks.

The two gates are widened together (a rungs write, not a ``tiers`` key), and
they only work together: without ``oc_cli``'s injection there is no pod block
for ``oc_model``'s carry to consume. So these tests drive the REAL seam —
``oc_cli.oc_full_config_set_with_error`` with the subprocess boundary replaced
by an in-process call into ``oc_model.json_full_config_set``, which is what the
sudo'd ``oc_model.py config set`` invocation does on the far side.

Lifecycle rule 2 is pinned alongside: the bare "Reset to pod defaults" payload
(``{"rungs": [], "roles": {}, "roleCaps": {}, "autoUpgrade": {}}``) rides this
same seam, and re-seeding a pod block there would mean a reset no longer returns
the bot to following the pod in full.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import model_auto_upgrade  # noqa: E402
import oc_cli  # noqa: E402
import oc_model  # noqa: E402

#: The pod has auto-upgrade ON with a non-default cadence, so a bot that ends up
#: on the code default is distinguishable from one that inherited.
POD_MODELS = {"autoUpgrade": {"enabled": True, "applyDay": "tuesday"}}

#: What ``_easy_setup_compute`` hands the bot-scope write: a full rung set.
WIZARD_CATALOG = {
    "rungs": [{"id": "haiku-class", "models": ["z/z"], "costClass": "low"}],
    "roles": {"fast": "haiku-class", "standard": "haiku-class"},
    "roleCaps": {},
}
#: Exactly what ``routes_admin_config`` sends for "Reset to pod defaults".
RESET = {"rungs": [], "roles": {}, "roleCaps": {}, "autoUpgrade": {}}


@pytest.fixture(autouse=True)
def _clear_bots_cache():
    oc_cli._bots_cache = None
    oc_cli._bots_cache_path = None
    yield
    oc_cli._bots_cache = None
    oc_cli._bots_cache_path = None


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A bot on Use-pod-defaults (no tiers file) plus a pod that has the block."""
    home = tmp_path / "home-bot"
    (home / ".openclaw").mkdir(parents=True)
    oc_json = home / ".openclaw" / "openclaw.json"
    oc_json.write_text(json.dumps({
        "agents": {"defaults": {"model": {
            "primary": "anthropic/claude-haiku-4-5", "fallbacks": [],
        }}},
    }))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        oc_model, "_preserve_write", lambda data, p: p.write_text(json.dumps(data))
    )

    network_path = tmp_path / "network.json"
    network = {
        "bots": {"b": {"user": "b", "role": "member"}},
        "sharedDir": str(tmp_path / "shared"),
        "models": dict(POD_MODELS),
    }
    network_path.write_text(json.dumps(network))

    def _shim(cmd, *a, **kw):
        """Stand in for the sudo'd ``oc_model.py config set`` subprocess.

        Shape-asserted before indexing: any OTHER subprocess that wandered into
        this flow (the ``sudo /bin/cat`` read fallbacks, ``_save_tiers_file``'s
        PermissionError path) would otherwise be silently reported as a
        successful config-set, or raise a confusing ValueError from ``index``.
        """
        assert "config" in cmd and "set" in cmd, f"unexpected subprocess: {cmd}"
        i = cmd.index("set")
        updates = json.loads(cmd[i + 2])
        # 5th positional, exactly as ``oc_model.py``'s __main__ reads it — the
        # fixture's bot is role=member, and role steers the tierCascade the
        # flat openclaw.json chain is recomputed from.
        role = cmd[i + 3] if len(cmd) > i + 3 else None
        oc_model.json_full_config_set(
            bot="b", updates=updates, oc_json_path=oc_json, role=role,
        )

        class _P:
            returncode = 0
            stdout = json.dumps({"bot": "b", "primary": "", "fallbacks": []})
            stderr = ""
        return _P()

    monkeypatch.setattr(oc_cli.subprocess, "run", _shim)
    monkeypatch.setattr(oc_cli, "_should_sudo", lambda user: False)
    return {
        "network": network,
        "network_path": str(network_path),
        "tiers_path": home / ".openclaw" / "evolve-tiers.json",
    }


def _write(env, updates: dict) -> None:
    result, err = oc_cli.oc_full_config_set_with_error(
        "b", updates, network_path=env["network_path"],
    )
    assert err is None, err
    assert result is not None


def _resolved(env) -> model_auto_upgrade.AutoUpgradePolicy:
    """The bot's RESOLVED auto-upgrade policy, the only thing that matters.

    ``custom`` is derived from the file the same way
    ``primary_bot.bot_has_custom_tiers`` derives it — non-empty ``rungs``.
    """
    path = env["tiers_path"]
    doc = json.loads(path.read_text()) if path.exists() else {}
    rungs = doc.get("rungs")
    custom = isinstance(rungs, list) and bool(rungs)
    pod = model_auto_upgrade.pod_policy(env["network"])
    return model_auto_upgrade.bot_policy(pod, doc, custom=custom)


def test_the_premise_a_use_defaults_bot_follows_the_pod(env):
    """Baseline: before the wizard runs, the bot rides the pod's toggle."""
    assert _resolved(env).enabled is True
    assert _resolved(env).enabled_source == "pod"


def test_wizard_custom_flip_leaves_the_bot_following_the_pod(env):
    """E-1. Fails on origin/main: the bot ends Custom with ``enabled`` False.

    The write carries no ``tiers`` key, so both the ``oc_cli`` injection and the
    ``oc_model`` carry used to skip it and the resolved policy fell to the code
    default while the pod said ``true``.
    """
    _write(env, dict(WIZARD_CATALOG))

    policy = _resolved(env)
    assert policy.enabled is True, (
        "wizard flipped the bot to Custom and auto-upgrade silently went off"
    )
    assert policy.apply_day == "tuesday", "pod cadence must ride along too"
    # And it really is Custom now — the flip happened, we just kept the posture.
    assert json.loads(env["tiers_path"].read_text())["rungs"]


def test_wizard_explicit_toggle_still_wins_over_the_pod(env):
    """The wizard's own ``auto_upgrade_enabled`` is an explicit decision.

    It merges ON TOP of the carried pod block, so the operator's ``false``
    sticks while the pod's subordinate knobs are still inherited.

    HONEST SCOPE: this holds on both sides of the fix and cannot observe the
    carry. ``bot_policy`` layers the SUBORDINATE knobs code ← pod ← bot on its
    own, so ``apply_day`` reads "tuesday" whether or not the pod block was
    seeded onto the file; only ``enabled`` is withheld from a Custom bot, and
    here the caller set it explicitly. The difference the carry makes is
    on-disk (``{"enabled": false}`` vs ``{"enabled": false, "applyDay":
    "tuesday"}``), and this file deliberately does not assert on key presence.
    Keep it as a REGRESSION GUARD: the widened trigger must not let the carry
    stomp an explicit caller value.
    """
    _write(env, {**WIZARD_CATALOG, "autoUpgrade": {"enabled": False}})

    policy = _resolved(env)
    assert policy.enabled is False
    assert policy.enabled_source == "bot", "the wizard's choice, not the pod's"
    assert policy.apply_day == "tuesday", "pod cadence still inherited"


def test_wizard_explicit_enable_against_a_pod_that_is_off(env, tmp_path):
    """The mirror case — pod off, wizard on — must not be swallowed."""
    env["network"]["models"] = {"autoUpgrade": {"enabled": False}}
    Path(env["network_path"]).write_text(json.dumps(env["network"]))

    _write(env, {**WIZARD_CATALOG, "autoUpgrade": {"enabled": True}})

    policy = _resolved(env)
    assert policy.enabled is True
    assert policy.enabled_source == "bot"


def test_bare_reset_still_returns_the_bot_to_the_pod(env):
    """LIFECYCLE RULE 2 — the trap in widening the gate.

    The reset payload carries ``rungs`` (empty) and rides the same seam. An
    empty list must NOT count as a rungs write, or the reset would re-seed the
    pod block as the bot's OWN and the bot would stay Custom-shaped in posture
    instead of following the pod in full.

    NOT the load-bearing guard, despite reading like one:
    ``_file_is_new_shape`` independently blocks the carry once the reset has
    popped ``rungs``, so this stays green even if BOTH emptiness checks were
    dropped from the widened gates. The assertion that actually catches that
    mistake is ``test_reset_is_not_handed_a_pod_block_at_the_transport_layer``
    below — the two are not interchangeable, do not delete it as redundant.
    """
    _write(env, dict(WIZARD_CATALOG))       # become Custom first
    _write(env, dict(RESET))                 # …then reset

    policy = _resolved(env)
    assert policy.enabled is True
    assert policy.enabled_source == "pod", (
        "reset re-seeded a pod block as the bot's own — lifecycle rule 2 broken"
    )
    # Physically: no rungs, and no autoUpgrade block of its own.
    doc = json.loads(env["tiers_path"].read_text())
    assert not doc.get("rungs")
    assert "autoUpgrade" not in doc


def test_bare_reset_follows_the_pod_even_when_the_pod_flips_off(env):
    """Stronger form of rule 2: after a reset the bot tracks the pod's CURRENT
    value, which a stale re-seeded copy could not do."""
    _write(env, dict(WIZARD_CATALOG))
    _write(env, dict(RESET))

    env["network"]["models"] = {"autoUpgrade": {"enabled": False}}
    assert _resolved(env).enabled is False


def test_reset_is_not_handed_a_pod_block_at_the_transport_layer(env):
    """Belt-and-braces on the OTHER gate: ``oc_cli`` must not even inject."""
    seen: dict = {}

    def _capture(cmd, *a, **kw):
        seen.update(json.loads(cmd[cmd.index("set") + 2]))

        class _P:
            returncode = 0
            stdout = json.dumps({"bot": "b", "primary": "", "fallbacks": []})
            stderr = ""
        return _P()

    import unittest.mock
    with unittest.mock.patch.object(oc_cli.subprocess, "run", _capture):
        oc_cli.oc_full_config_set_with_error(
            "b", dict(RESET), network_path=env["network_path"],
        )
    assert oc_model.POD_AUTO_UPGRADE_KEY not in seen


def test_roles_only_write_does_not_trigger_the_carry(env):
    """A roles-only payload cannot flip a bot to Custom, so it must not carry.

    ``_wholesale_rungs_landed`` is narrower than the pre-existing
    ``_wholesale_landed`` (which a roles replacement also sets), but not
    OBSERVABLY so: a roles-only landing needs the rungs to have come from disk,
    which makes ``_non_custom_at_some_point`` False anyway. The narrow flag is
    defence in depth, not a behavior this test can pin apart. What it does pin
    is the outcome.
    """
    _write(env, {"roles": {"fast": "haiku-class"}})

    doc = json.loads(env["tiers_path"].read_text())
    assert "autoUpgrade" not in doc, "carried a pod block onto a non-Custom bot"
    assert _resolved(env).enabled_source == "pod"


def test_migration_residue_bot_is_not_silently_switched_on(env):
    """The cohort ``_non_custom_at_some_point`` now single-handedly protects.

    ``migrate-model-roles`` leaves a bot Custom (rungs present) with NO
    ``autoUpgrade`` block, so it resolves to the code default ``false`` — the
    spec's §"migration-era hazard" cohort. Before this change a wholesale rungs
    write never reached the carry at all; now it does, and only
    ``_non_custom_at_some_point`` stops the pod's ``enabled: true`` from being
    seeded on. Re-running the wizard must not turn auto-upgrade ON behind the
    operator's back — that is a config change nobody asked for, in the
    direction that costs money.
    """
    env["tiers_path"].write_text(json.dumps({
        "rungs": [{"id": "sonnet-class", "models": ["old/s"], "costClass": "medium"}],
        "roles": {"standard": "sonnet-class"},
    }))

    _write(env, dict(WIZARD_CATALOG))

    policy = _resolved(env)
    assert policy.enabled is False, "wizard silently switched auto-upgrade ON"
    assert policy.enabled_source == "code-default"
    assert "autoUpgrade" not in json.loads(env["tiers_path"].read_text())


def test_an_already_custom_bot_keeps_its_own_posture(env):
    """No flip, no carry — re-running the wizard on a Custom bot that turned
    auto-upgrade off must not resurrect the pod's ``enabled``."""
    _write(env, {**WIZARD_CATALOG, "autoUpgrade": {"enabled": False}})
    _write(env, {
        "rungs": [{"id": "sonnet-class", "models": ["a/b"], "costClass": "medium"}],
        "roles": {"standard": "sonnet-class"},
        "roleCaps": {},
    })

    policy = _resolved(env)
    assert policy.enabled is False, "re-ran the wizard and lost the bot's own off"
    assert policy.enabled_source == "bot"
