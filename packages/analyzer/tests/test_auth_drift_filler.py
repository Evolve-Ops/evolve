"""tests/test_auth_drift_filler.py — auth_drift_filler factory + observe tests.

Same shape as test_cron_caps_filler — factory tests over hand-rolled
SimpleNamespace signal fixtures, observe() end-to-end against a tmp
Signal store populated via signals.store.observe.

Distinct from cron_caps_filler: this generator fans one signal out
into N proposals (one per drifted field), so the tests verify both
the per-field shape AND the cardinality.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.auth_drift_filler.observe import (  # noqa: E402
    AuthDriftFillerContext,
    observe,
)
from generators.auth_drift_filler.signal_proposals import (  # noqa: E402
    make_drift_proposals,
)
from schema.signal import make_signature  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────


_DEFAULT_DIFFS = {
    "tools.exec.security": {"expected": "low", "observed": "full"},
    "tools.fs.workspaceOnly": {"expected": True, "observed": False},
}


def _drift_signal(
    *,
    sig_id: str = "sig-drift-1",
    bot_id: str = "admin_bot",
    diffs: dict[str, dict] | None = None,
) -> SimpleNamespace:
    # Sentinel-style: None → defaults; {} → caller wants explicitly empty.
    if diffs is None:
        diffs = dict(_DEFAULT_DIFFS)
    return SimpleNamespace(
        id=sig_id,
        bot_id=bot_id,
        type="perm_config_drift",
        severity="warn",
        details={"bot_id": bot_id, "diffs": diffs},
    )


# ── make_drift_proposals — factory unit tests ────────────────────────────────


def test_factory_emits_one_proposal_per_drifted_field():
    sig = _drift_signal()
    proposals = make_drift_proposals(sig)
    assert len(proposals) == 2
    # Each proposal carries a single-field fields dict (one-per-drift
    # cardinality so the operator can selectively accept).
    field_names: set[str] = set()
    for p in proposals:
        keys = list(p.action.fields.keys())
        assert len(keys) == 1
        field_names.add(keys[0])
    assert field_names == {"tools.exec.security", "tools.fs.workspaceOnly"}


def test_factory_action_targets_bot_via_bot_id():
    """UpdatePermissionConfig identifies the target bot by bot_id; the
    applier resolves the bot home itself. No target_path is needed
    (and emitting one would route to the wrong applier — see
    internal/diagnosis-openclaw-json-write-regression-2026-05-21.md)."""
    sig = _drift_signal(bot_id="team_bot_a")
    proposals = make_drift_proposals(sig)
    for p in proposals:
        assert p.action.kind == "UpdatePermissionConfig"
        assert p.action.bot_id == "team_bot_a"
        # Must NOT carry a ConfigPatch-shaped target_path.
        assert not hasattr(p.action, "target_path")


def test_factory_value_is_baseline_expected():
    """The UpdatePermissionConfig field value must be the baseline
    (expected) value — that's what restores the bot to known-good.
    Trivial but critical."""
    sig = _drift_signal(diffs={
        "tools.exec.security": {"expected": "low", "observed": "full"},
    })
    proposals = make_drift_proposals(sig)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.action.kind == "UpdatePermissionConfig"
    assert p.action.bot_id == "admin_bot"
    assert p.action.fields == {"tools.exec.security": "low"}


def test_factory_proposal_shape():
    """Every proposal must carry the standard provenance + risk_tag +
    motivating_signals link — confirms the proposal slot in the OC
    audit chain stays well-formed for the applier."""
    sig = _drift_signal(diffs={
        "tools.fs.workspaceOnly": {"expected": True, "observed": False},
    })
    proposals = make_drift_proposals(sig)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.bot_id == "admin_bot"
    assert p.generator_id == "auth_drift_filler"
    assert p.dimension == "safety"
    assert p.motivating_signals == ["sig-drift-1"]
    assert p.trigger_observations == [
        "perm_config_drift:admin_bot:tools.fs.workspaceOnly"
    ]
    assert p.approval_audience == "pod_operator"
    assert p.urgency == "hygiene"
    assert p.risk_tag.blast_radius == "bot"
    assert p.risk_tag.reversibility == "auto"
    assert p.risk_tag.touches == ["auth_config"]
    assert p.claim is None
    # Phase C-6 (2026-06-04) humanized title — drops the raw value
    # pair from the title (it goes to Summary + Details).
    assert p.admin_surface_summary.startswith("Restore admin_bot's")
    assert "tools.fs.workspaceOnly" in p.admin_surface_summary
    assert len(p.admin_surface_summary) <= 120


def test_factory_problem_prose_includes_observed_and_baseline():
    sig = _drift_signal(diffs={
        "tools.exec.security": {"expected": "low", "observed": "full"},
    })
    proposals = make_drift_proposals(sig)
    text = proposals[0].problem
    assert "tools.exec.security" in text
    assert "'full'" in text
    assert "'low'" in text
    assert "admin_bot" in text


def test_factory_skips_field_when_baseline_is_none():
    """A field with no baseline opinion (expected=None) has nothing
    to restore; skip rather than emit a no-op proposal."""
    sig = _drift_signal(diffs={
        "tools.exec.host": {"expected": None, "observed": "all"},
    })
    proposals = make_drift_proposals(sig)
    assert proposals == []


def test_factory_skips_field_when_expected_equals_observed():
    """Defensive: producer shouldn't include identical pairs, but if a
    future iteration does, the filler must not emit a no-op."""
    sig = _drift_signal(diffs={
        "tools.exec.security": {"expected": "full", "observed": "full"},
    })
    proposals = make_drift_proposals(sig)
    assert proposals == []


def test_factory_handles_empty_diffs_map():
    sig = _drift_signal(diffs={})
    assert make_drift_proposals(sig) == []


def test_factory_handles_missing_details():
    sig = SimpleNamespace(
        id="sig-malformed", bot_id="admin_bot",
        type="perm_config_drift", details=None,
    )
    assert make_drift_proposals(sig) == []


def test_factory_handles_dict_signal_form():
    sig_dict = {
        "id": "sig-d-1",
        "bot_id": "team_bot_a",
        "type": "perm_config_drift",
        "details": {
            "diffs": {
                "tools.exec.security": {"expected": "low", "observed": "full"},
            },
        },
    }
    proposals = make_drift_proposals(sig_dict)
    assert len(proposals) == 1
    assert proposals[0].bot_id == "team_bot_a"
    assert proposals[0].motivating_signals == ["sig-d-1"]


def test_factory_handles_boolean_and_none_values_in_prose():
    """Boolean / None values format readably in problem prose (so the
    operator paraphrase doesn't render 'observed=False' as 'observed=0'
    or surface raw Python repr quirks)."""
    sig = _drift_signal(diffs={
        "tools.fs.workspaceOnly": {"expected": True, "observed": False},
    })
    proposals = make_drift_proposals(sig)
    text = proposals[0].problem
    assert "true" in text or "True" in text
    assert "false" in text or "False" in text


# ── observe() end-to-end ──────────────────────────────────────────────────────


def _write_drift_signal(
    shared_dir: Path,
    *,
    bot_id: str,
    diffs: dict[str, dict],
    signature_salt: str = "",
) -> str:
    # In production a bot has at most one open perm_config_drift signal
    # per scope. Tests that need *multiple* such signals use the salt to
    # synthesize distinct signatures and exercise the per-signal loop.
    scope = f"{bot_id}:{signature_salt}" if signature_salt else bot_id
    sig = signals_store.observe(
        shared_dir,
        signature=make_signature("permission_monitor", "perm_config_drift", scope),
        producer="permission_monitor",
        type="perm_config_drift",
        flavor="security",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id}: permission config drifted",
        details={"bot_id": bot_id, "diffs": diffs},
    )
    return sig.id


def test_observe_fans_one_signal_into_n_proposals(tmp_path):
    _write_drift_signal(
        tmp_path, bot_id="admin_bot",
        diffs={
            "tools.exec.security": {"expected": "low", "observed": "full"},
            "tools.fs.workspaceOnly": {"expected": True, "observed": False},
            "commands.useAccessGroups": {"expected": True, "observed": False},
        },
    )
    proposals = observe(AuthDriftFillerContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert len(proposals) == 3
    for p in proposals:
        assert p.bot_id == "admin_bot"
        assert p.generator_id == "auth_drift_filler"


def test_observe_filters_by_bot_id(tmp_path):
    _write_drift_signal(
        tmp_path, bot_id="admin_bot",
        diffs={"tools.exec.security": {"expected": "low", "observed": "full"}},
    )
    _write_drift_signal(
        tmp_path, bot_id="team_bot_a",
        diffs={"tools.fs.workspaceOnly": {"expected": True, "observed": False}},
    )
    admin_bot = observe(AuthDriftFillerContext(bot_id="admin_bot", shared_dir=tmp_path))
    team_bot_a = observe(AuthDriftFillerContext(bot_id="team_bot_a", shared_dir=tmp_path))
    assert len(admin_bot) == 1
    assert admin_bot[0].bot_id == "admin_bot"
    assert len(team_bot_a) == 1
    assert team_bot_a[0].bot_id == "team_bot_a"


def test_observe_ignores_other_signal_types(tmp_path):
    """A different permission_monitor signal (e.g. perm_cron_uncapped)
    must not trigger an auth-drift proposal."""
    signals_store.observe(
        tmp_path,
        signature=make_signature(
            "permission_monitor", "perm_cron_uncapped_agent_turn",
            "admin_bot:j-x",
        ),
        producer="permission_monitor",
        type="perm_cron_uncapped_agent_turn",
        flavor="security",
        severity="warn",
        scope="bot",
        bot_id="admin_bot",
        title="impostor",
        details={"bot_id": "admin_bot", "job_id": "j-x"},
    )
    proposals = observe(AuthDriftFillerContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert proposals == []


def test_observe_ignores_other_producers(tmp_path):
    """A different producer emitting a perm_config_drift-shaped signal
    must not be consumed — protects against signature namespace
    collisions or future producers reusing the type name."""
    signals_store.observe(
        tmp_path,
        signature=make_signature("test", "perm_config_drift", "admin_bot"),
        producer="test",
        type="perm_config_drift",
        flavor="security",
        severity="warn",
        scope="bot",
        bot_id="admin_bot",
        title="impostor",
        details={
            "bot_id": "admin_bot",
            "diffs": {
                "tools.exec.security": {"expected": "low", "observed": "full"},
            },
        },
    )
    proposals = observe(AuthDriftFillerContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert proposals == []


def test_observe_returns_empty_when_no_signals(tmp_path):
    proposals = observe(AuthDriftFillerContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert proposals == []


def test_observe_swallows_per_signal_error(tmp_path, monkeypatch):
    """One malformed signal must not torpedo the rest of the run.

    Note: ``auth_drift_filler.__init__`` re-exports the function
    ``observe`` from the ``observe`` submodule, shadowing the module
    name on the package. To patch the real module's
    ``make_drift_proposals`` symbol, we go through ``sys.modules``.
    """
    import sys as _sys
    _write_drift_signal(
        tmp_path, bot_id="admin_bot",
        diffs={"tools.exec.security": {"expected": "low", "observed": "full"}},
        signature_salt="A",
    )
    _write_drift_signal(
        tmp_path, bot_id="admin_bot",
        diffs={"tools.fs.workspaceOnly": {"expected": True, "observed": False}},
        signature_salt="B",
    )

    call_count = {"n": 0}
    real_factory = make_drift_proposals

    def flaky(sig, **kwargs):
        # **kwargs absorbs the new shared_dir keyword observe() now
        # forwards (config-intent investigate-before-propose); the test
        # is pinning resilience, not the call signature.
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("synthetic")
        return real_factory(sig, **kwargs)

    obs_mod = _sys.modules["generators.auth_drift_filler.observe"]
    monkeypatch.setattr(obs_mod, "make_drift_proposals", flaky)

    proposals = observe(AuthDriftFillerContext(bot_id="admin_bot", shared_dir=tmp_path))
    # One signal blew up; the other produced one proposal.
    assert len(proposals) == 1
