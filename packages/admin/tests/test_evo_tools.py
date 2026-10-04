"""tests/test_evo_tools.py — evo OC-native tool registry, Phase 1.0.

Covers the registry infrastructure + the first concrete tool
(``pod_state.signals.firing``). Mirrors the testing pattern in
``test_home_chat.py``: each test exercises one behavioral claim;
fixtures are tmp_path-based; no network.

The signal-store tool tests construct real Signal objects via
``signals.store.observe()`` against a tmp_path shared_dir, then invoke
the tool and assert on the projected output. This avoids mocking the
analyzer surface — better integration coverage at the cost of one
sys.path tweak in setup.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.evo import tools as _tools  # noqa: E402
from evolve_admin.evo.tools import pod_state_signals  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# RiskTier enum + Tool dataclass contract
# ─────────────────────────────────────────────────────────────────────────────


def test_risk_tier_values_are_stable():
    """Risk tier strings are part of the registry's API surface — used by
    the proxy's authority gate (Phase 4) and by TOOLS.md generation
    (§4.3). Asserting their values here turns any accidental rename
    into a test failure rather than a quiet downstream miscalibration."""
    assert _tools.RiskTier.READ.value == "read"
    assert _tools.RiskTier.WRITE_SAFE.value == "write_safe"
    assert _tools.RiskTier.WRITE_RISKY.value == "write_risky"
    assert _tools.RiskTier.DESTRUCTIVE.value == "destructive"


def test_tool_rejects_read_with_validate():
    """Read-tier tools must NOT define a validate function. The proxy's
    button-rendering logic (spec §5.2) skips read tools entirely; a
    read-with-validate would never have its validate called, masking
    a likely intent error. Enforce at construction time so the wrong
    shape can't enter the registry."""
    with pytest.raises(ValueError) as exc:
        _tools.Tool(
            name="bogus.read_with_validate",
            description="should fail",
            input_schema={"type": "object", "properties": {}},
            handler=lambda **kw: {},
            risk_tier=_tools.RiskTier.READ,
            validate=lambda **kw: {"ok": True},
        )
    assert "read-tier tools must not define validate" in str(exc.value)


def test_tool_rejects_action_without_validate():
    """Mirror: non-read tier (write_safe / write_risky / destructive)
    MUST have a validate function. Without it, the proxy can't run the
    dry-run gate before rendering a confirmation button (spec §5.2's
    third gate). Enforced at construction."""
    for tier in (
        _tools.RiskTier.WRITE_SAFE,
        _tools.RiskTier.WRITE_RISKY,
        _tools.RiskTier.DESTRUCTIVE,
    ):
        with pytest.raises(ValueError) as exc:
            _tools.Tool(
                name=f"bogus.action_no_validate.{tier.value}",
                description="should fail",
                input_schema={"type": "object", "properties": {}},
                handler=lambda **kw: {},
                risk_tier=tier,
                validate=None,
            )
        assert "requires a validate" in str(exc.value)


def test_tool_allows_action_with_validate():
    """Sanity: the construction guard fires ONLY on the wrong shapes;
    a well-formed action tool constructs fine."""
    t = _tools.Tool(
        name="bogus.action_ok",
        description="ok",
        input_schema={"type": "object", "properties": {}},
        handler=lambda **kw: {"ok": True},
        risk_tier=_tools.RiskTier.WRITE_SAFE,
        validate=lambda **kw: {"ok": True},
    )
    assert t.name == "bogus.action_ok"
    assert t.validate is not None


# ─────────────────────────────────────────────────────────────────────────────
# Registry behavior
# ─────────────────────────────────────────────────────────────────────────────


def _make_read_tool(name: str, marker: str) -> _tools.Tool:
    """Helper: build a uniquely-marked read-tier Tool for registry tests.
    The handler returns the marker so register/lookup round-trips can
    confirm they got the same Tool back."""
    return _tools.Tool(
        name=name,
        description=f"test tool {marker}",
        input_schema={"type": "object", "properties": {}},
        handler=lambda **kw: {"marker": marker},
        risk_tier=_tools.RiskTier.READ,
    )


def test_register_then_lookup_roundtrips():
    """Register a tool, then look it up by name — same object back."""
    t = _make_read_tool("test.roundtrip", "alpha")
    _tools.register(t)
    found = _tools.lookup("test.roundtrip")
    assert found is t


def test_lookup_unknown_returns_none():
    """Looking up a name that was never registered returns None.
    The proxy (Phase 4) relies on this for "did the LLM hallucinate a
    tool name?" detection — None means hallucinated, real Tool means
    real."""
    assert _tools.lookup("definitely.never.registered") is None


def test_register_replaces_same_name():
    """Idempotent-on-name semantics: re-registering with the same name
    overwrites the prior entry. Important during dev (modules
    re-import) and during the integration tests that may register a
    test-double tool over a real one."""
    _tools.register(_make_read_tool("test.replace_me", "v1"))
    _tools.register(_make_read_tool("test.replace_me", "v2"))
    found = _tools.lookup("test.replace_me")
    assert found is not None
    # Invoking the handler tells us WHICH version landed
    assert found.handler()["marker"] == "v2"


def test_tools_by_tier_filters_correctly():
    """tools_by_tier(tier) returns only matching-tier tools. Used by
    the proxy to enumerate "what's silently-executable under this
    authority level"."""
    # Register one per tier (write_safe needs validate; others don't except read)
    _tools.register(_tools.Tool(
        name="test.tier.read",
        description="read",
        input_schema={"type": "object", "properties": {}},
        handler=lambda **kw: {},
        risk_tier=_tools.RiskTier.READ,
    ))
    _tools.register(_tools.Tool(
        name="test.tier.ws",
        description="write_safe",
        input_schema={"type": "object", "properties": {}},
        handler=lambda **kw: {},
        risk_tier=_tools.RiskTier.WRITE_SAFE,
        validate=lambda **kw: {"ok": True},
    ))
    reads = _tools.tools_by_tier(_tools.RiskTier.READ)
    write_safes = _tools.tools_by_tier(_tools.RiskTier.WRITE_SAFE)
    read_names = {t.name for t in reads}
    write_safe_names = {t.name for t in write_safes}
    assert "test.tier.read" in read_names
    assert "test.tier.read" not in write_safe_names
    assert "test.tier.ws" in write_safe_names
    assert "test.tier.ws" not in read_names


def test_all_tools_returns_immutable_snapshot():
    """all_tools() returns a tuple, so callers can't accidentally
    mutate the registry from outside. Important because tools register
    at import time and external mutation would silently corrupt the
    OC manifest."""
    snapshot = _tools.all_tools()
    assert isinstance(snapshot, tuple)


# ─────────────────────────────────────────────────────────────────────────────
# OC manifest rendering
# ─────────────────────────────────────────────────────────────────────────────


def test_build_tool_manifest_shape():
    """The manifest must be a list of dicts, each with name +
    description + input_schema. These are the fields OC's tool-use
    protocol consumes. Risk tier and validate are Evolve-side and must
    NOT leak into the OC-facing manifest."""
    _tools.register(_tools.Tool(
        name="test.manifest_shape",
        description="check manifest shape",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        handler=lambda **kw: {},
        risk_tier=_tools.RiskTier.READ,
    ))
    manifest = _tools.build_tool_manifest()
    entry = next(e for e in manifest if e["name"] == "test.manifest_shape")
    assert set(entry.keys()) == {"name", "description", "input_schema"}
    assert "risk_tier" not in entry
    assert "validate" not in entry
    assert "handler" not in entry
    assert entry["input_schema"]["properties"]["x"]["type"] == "string"


def test_build_tool_manifest_includes_real_tool():
    """The actually-shipping pod_state.signals.firing tool must appear
    in the manifest. This is the regression that catches "module wasn't
    imported, registration didn't fire"."""
    manifest = _tools.build_tool_manifest()
    names = {e["name"] for e in manifest}
    assert "pod_state.signals.firing" in names


# ─────────────────────────────────────────────────────────────────────────────
# pod_state.signals.firing handler — real signal store integration
# ─────────────────────────────────────────────────────────────────────────────


def _seed_signal(shared_dir: Path, *, signature: str, producer: str,
                 type_: str, bot_id: str | None, severity: str,
                 title: str) -> str:
    """Helper: create a firing signal in shared_dir via the real store.
    Returns the new signal id.

    Note: signals.store.observe() takes its second arg as ``type``
    (Python keyword shadow, but that's the declared name). We accept
    ``type_`` here in the helper to avoid the shadow, then pass it
    through with the right kwarg name."""
    from signals import store as _store
    sig = _store.observe(
        shared_dir,
        signature=signature,
        producer=producer,
        type=type_,
        flavor="security",
        severity=severity,
        scope="bot" if bot_id else "pod",
        bot_id=bot_id,
        title=title,
        body=f"test body for {title}",
        details={"test": True},
    )
    return sig.id


def test_handler_returns_empty_on_empty_store(tmp_path):
    """No signals firing → count=0, signals=[], truncated=False.
    This is the all-quiet case the model should be able to answer
    'nothing is firing right now' from."""
    result = pod_state_signals._handler(shared_dir=tmp_path)
    assert result["count"] == 0
    assert result["signals"] == []
    assert result["truncated"] is False


def test_handler_lists_firing_signals(tmp_path):
    """Three signals seeded → all three returned with projected fields."""
    _seed_signal(
        tmp_path, signature="t:test:personal_bot:memory",
        producer="content_scan", type_="content_scan_file_disappeared",
        bot_id="personal_bot", severity="alert",
        title="personal_bot: MEMORY.md missing or unreadable",
    )
    _seed_signal(
        tmp_path, signature="t:test:personal_bot:perm",
        producer="permission_monitor", type_="perm_config_drift",
        bot_id="personal_bot", severity="warn",
        title="personal_bot: permission config drifted",
    )
    _seed_signal(
        tmp_path, signature="t:test:pod:drift",
        producer="deploy_drift_monitor", type_="deploy_drift",
        bot_id=None, severity="warn",
        title="6 bots are running an older Evolve version",
    )

    result = pod_state_signals._handler(shared_dir=tmp_path)
    assert result["count"] == 3
    assert result["truncated"] is False
    titles = {s["title"] for s in result["signals"]}
    assert "personal_bot: MEMORY.md missing or unreadable" in titles
    assert "personal_bot: permission config drifted" in titles
    # Pod-scoped signals are visible too (no bot filter applied)
    assert "6 bots are running an older Evolve version" in titles


def test_handler_filters_by_bot(tmp_path):
    """bot_id filter restricts to that bot. Pod-scoped signals NOT
    included (the iter_active filter is strict on bot_id match)."""
    _seed_signal(
        tmp_path, signature="t:test:personal_bot:a",
        producer="content_scan", type_="content_scan_match",
        bot_id="personal_bot", severity="alert", title="personal_bot thing",
    )
    _seed_signal(
        tmp_path, signature="t:test:team_bot_a:b",
        producer="content_scan", type_="content_scan_match",
        bot_id="team_bot_a", severity="alert", title="team_bot_a thing",
    )

    personal_bot_only = pod_state_signals._handler(
        shared_dir=tmp_path, bot_id="personal_bot",
    )
    assert personal_bot_only["count"] == 1
    assert personal_bot_only["signals"][0]["title"] == "personal_bot thing"

    team_bot_a_only = pod_state_signals._handler(
        shared_dir=tmp_path, bot_id="team_bot_a",
    )
    assert team_bot_a_only["count"] == 1
    assert team_bot_a_only["signals"][0]["title"] == "team_bot_a thing"


def test_handler_filters_by_severity(tmp_path):
    """severity filter passes through to iter_active. Only matching
    severity returned."""
    _seed_signal(
        tmp_path, signature="t:test:alert:a",
        producer="content_scan", type_="content_scan_match",
        bot_id="personal_bot", severity="alert", title="alert-tier",
    )
    _seed_signal(
        tmp_path, signature="t:test:warn:b",
        producer="content_scan", type_="content_scan_match",
        bot_id="personal_bot", severity="warn", title="warn-tier",
    )

    alerts = pod_state_signals._handler(
        shared_dir=tmp_path, severity="alert",
    )
    assert alerts["count"] == 1
    assert alerts["signals"][0]["title"] == "alert-tier"


def test_handler_filters_by_producer(tmp_path):
    """producer filter restricts to one producer."""
    _seed_signal(
        tmp_path, signature="t:test:cs:a",
        producer="content_scan", type_="content_scan_match",
        bot_id="personal_bot", severity="warn", title="from content_scan",
    )
    _seed_signal(
        tmp_path, signature="t:test:pm:b",
        producer="permission_monitor", type_="perm_config_drift",
        bot_id="personal_bot", severity="warn", title="from permission_monitor",
    )

    cs_only = pod_state_signals._handler(
        shared_dir=tmp_path, producer="content_scan",
    )
    assert cs_only["count"] == 1
    assert cs_only["signals"][0]["title"] == "from content_scan"


def test_handler_projection_omits_volatile_fields(tmp_path):
    """The projection strips out fields the model doesn't need (details,
    state_history, deliveries, motivated_proposals, config_hint,
    remediation, schema_version). These pollute the model's view; the
    projection picks just the operator-relevant identity + status."""
    _seed_signal(
        tmp_path, signature="t:test:proj:a",
        producer="content_scan", type_="content_scan_match",
        bot_id="personal_bot", severity="alert", title="projection check",
    )

    result = pod_state_signals._handler(shared_dir=tmp_path)
    sig = result["signals"][0]
    # Allowed fields (id, producer, type, severity, scope, bot_id,
    # title, first_observed_at, last_observed_at, observation_count;
    # incident_key only when present).
    allowed = {
        "id", "producer", "type", "severity", "scope", "bot_id",
        "title", "first_observed_at", "last_observed_at",
        "observation_count", "incident_key",
    }
    assert set(sig.keys()).issubset(allowed)
    # Critical exclusions — fields the model doesn't need
    assert "details" not in sig
    assert "state_history" not in sig
    assert "deliveries" not in sig
    assert "motivated_proposals" not in sig
    assert "schema_version" not in sig


def test_handler_caps_at_max_results(tmp_path):
    """Above the cap, results are truncated and ``truncated`` flips to
    True. The model uses this to decide whether to refine filters or
    raise max_results. ``count`` reports the PRE-cap total so the model
    knows the real magnitude."""
    for i in range(60):
        _seed_signal(
            tmp_path, signature=f"t:test:bulk:{i}",
            producer="content_scan", type_="content_scan_match",
            bot_id="personal_bot", severity="warn", title=f"bulk {i}",
        )

    capped = pod_state_signals._handler(shared_dir=tmp_path, max_results=20)
    assert capped["count"] == 60       # total matched, before cap
    assert len(capped["signals"]) == 20
    assert capped["truncated"] is True

    big = pod_state_signals._handler(shared_dir=tmp_path, max_results=100)
    assert big["count"] == 60
    assert len(big["signals"]) == 60
    assert big["truncated"] is False


def test_handler_max_results_clamped_to_safe_range(tmp_path):
    """max_results outside [1, 200] gets clamped silently. Tests both
    bounds: 0 → 1 (always show at least one), 9999 → 200 (cap on
    upper bound)."""
    for i in range(5):
        _seed_signal(
            tmp_path, signature=f"t:test:clamp:{i}",
            producer="content_scan", type_="content_scan_match",
            bot_id="personal_bot", severity="warn", title=f"clamp {i}",
        )

    # max_results=0 → effectively 1 (lower clamp)
    low = pod_state_signals._handler(shared_dir=tmp_path, max_results=0)
    assert len(low["signals"]) == 1
    assert low["truncated"] is True

    # max_results way beyond available — no truncation, returns all 5
    high = pod_state_signals._handler(shared_dir=tmp_path, max_results=9999)
    assert len(high["signals"]) == 5
    assert high["truncated"] is False


def test_handler_handles_missing_signals_dir_gracefully(tmp_path):
    """If shared_dir doesn't have signals/firing/ yet (fresh install),
    iter_active should yield nothing rather than crash. The tool
    returns the all-quiet shape."""
    # Don't seed anything — signals/firing/ won't exist
    assert not (tmp_path / "signals" / "firing").exists()
    result = pod_state_signals._handler(shared_dir=tmp_path)
    assert result["count"] == 0
    assert result["signals"] == []


def test_handler_filters_compose(tmp_path):
    """Multiple filters AND together. A signal must match all
    constraints to be returned."""
    _seed_signal(
        tmp_path, signature="t:test:combo:a",
        producer="content_scan", type_="content_scan_match",
        bot_id="personal_bot", severity="alert", title="personal_bot CS alert",
    )
    _seed_signal(
        tmp_path, signature="t:test:combo:b",
        producer="content_scan", type_="content_scan_match",
        bot_id="personal_bot", severity="warn", title="personal_bot CS warn",
    )
    _seed_signal(
        tmp_path, signature="t:test:combo:c",
        producer="content_scan", type_="content_scan_match",
        bot_id="team_bot_a", severity="alert", title="team_bot_a CS alert",
    )

    personal_bot_alerts = pod_state_signals._handler(
        shared_dir=tmp_path, bot_id="personal_bot", severity="alert",
    )
    assert personal_bot_alerts["count"] == 1
    assert personal_bot_alerts["signals"][0]["title"] == "personal_bot CS alert"


# ─────────────────────────────────────────────────────────────────────────────
# Tool registration end-to-end
# ─────────────────────────────────────────────────────────────────────────────


def test_pod_state_signals_firing_is_registered():
    """The tool was imported in __init__.py and should appear in the
    registry post-import. Regression test for 'forgot to import the
    module, registry is empty'."""
    found = _tools.lookup("pod_state.signals.firing")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.READ
    assert found.validate is None
    # Description must be substantive — not the empty string the test
    # double would have
    assert len(found.description) > 50


def test_tool_description_mentions_filters():
    """The tool description is what the LLM reads to decide whether to
    call this tool and with what args. It must mention the filter
    semantics so the model knows bot_id / severity / producer exist
    and what they mean."""
    found = _tools.lookup("pod_state.signals.firing")
    assert found is not None
    desc = found.description.lower()
    assert "bot_id" in desc
    assert "severity" in desc
    assert "producer" in desc


def test_tool_input_schema_well_formed():
    """The input_schema is JSON Schema; OC's tool-use protocol validates
    args against it. Check the basics: object type, properties for each
    filter, severity enum constrained to the real values."""
    found = _tools.lookup("pod_state.signals.firing")
    assert found is not None
    schema = found.input_schema
    assert schema["type"] == "object"
    assert "bot_id" in schema["properties"]
    assert "severity" in schema["properties"]
    assert "producer" in schema["properties"]
    assert "max_results" in schema["properties"]
    # additionalProperties=False forces the model to use only declared args
    assert schema["additionalProperties"] is False
    # severity enum matches what producer_severity.py declares
    assert set(schema["properties"]["severity"]["enum"]) == {
        "info", "warn", "alert",
    }


# ─────────────────────────────────────────────────────────────────────────────
# pod_state.signals.history — state-transition timeline
# ─────────────────────────────────────────────────────────────────────────────


def test_history_handler_empty_store_returns_empty(tmp_path):
    """No signals exist → no transitions to walk → empty list, no error."""
    result = pod_state_signals._history_handler(shared_dir=tmp_path)
    assert result["count"] == 0
    assert result["entries"] == []
    assert result["truncated"] is False


def test_history_handler_returns_transitions(tmp_path):
    """Seed a signal (which gets one creation transition), confirm the
    history tool returns that transition with identity fields attached."""
    _seed_signal(
        tmp_path, signature="t:test:hist:a",
        producer="content_scan", type_="content_scan_match",
        bot_id="personal_bot", severity="alert", title="hist test alert",
    )
    result = pod_state_signals._history_handler(shared_dir=tmp_path)
    assert result["count"] >= 1
    entry = result["entries"][0]
    # Identity bits flattened from the parent signal
    assert entry["title"] == "hist test alert"
    assert entry["producer"] == "content_scan"
    assert entry["type"] == "content_scan_match"
    # Transition itself
    assert entry["to_state"] == "firing"
    assert entry["from_state"] is None  # initial creation
    assert entry["at"]  # timestamp present


def test_history_handler_filters_by_producer(tmp_path):
    """producer filter restricts to transitions on signals from one producer."""
    _seed_signal(
        tmp_path, signature="t:test:hist:cs",
        producer="content_scan", type_="content_scan_match",
        bot_id="personal_bot", severity="warn", title="cs signal",
    )
    _seed_signal(
        tmp_path, signature="t:test:hist:pm",
        producer="permission_monitor", type_="perm_config_drift",
        bot_id="personal_bot", severity="warn", title="pm signal",
    )
    cs_only = pod_state_signals._history_handler(
        shared_dir=tmp_path, producer="content_scan",
    )
    titles = {e["title"] for e in cs_only["entries"]}
    assert titles == {"cs signal"}


def test_history_handler_filters_by_bot_id(tmp_path):
    """bot_id filter restricts to one bot's signal transitions."""
    _seed_signal(
        tmp_path, signature="t:test:hist:personal_bot",
        producer="content_scan", type_="content_scan_match",
        bot_id="personal_bot", severity="warn", title="personal_bot thing",
    )
    _seed_signal(
        tmp_path, signature="t:test:hist:team_bot_a",
        producer="content_scan", type_="content_scan_match",
        bot_id="team_bot_a", severity="warn", title="team_bot_a thing",
    )
    personal_bot_only = pod_state_signals._history_handler(
        shared_dir=tmp_path, bot_id="personal_bot",
    )
    titles = {e["title"] for e in personal_bot_only["entries"]}
    assert titles == {"personal_bot thing"}


def test_history_handler_sorts_newest_first(tmp_path):
    """Newest transition appears first in the list. Critical for the
    'what just happened?' use case — operator wants the latest, not
    the oldest, when scrolling."""
    from datetime import datetime, timezone, timedelta
    from signals import store as _store
    # Create two signals; the second observe call refreshes a different
    # one — we exploit the per-signal observation timestamp ordering
    # to know which transition is newest.
    _seed_signal(
        tmp_path, signature="t:test:order:older",
        producer="content_scan", type_="t", bot_id="personal_bot",
        severity="warn", title="older",
    )
    _seed_signal(
        tmp_path, signature="t:test:order:newer",
        producer="content_scan", type_="t", bot_id="personal_bot",
        severity="warn", title="newer",
    )
    result = pod_state_signals._history_handler(shared_dir=tmp_path)
    # First entry should be the most recent transition by 'at'
    ats = [e["at"] for e in result["entries"]]
    assert ats == sorted(ats, reverse=True)


def test_history_handler_caps_at_max_results(tmp_path):
    """Cap clips, truncated flag flips, count reports the pre-cap total."""
    for i in range(20):
        _seed_signal(
            tmp_path, signature=f"t:test:histcap:{i}",
            producer="content_scan", type_="t",
            bot_id="personal_bot", severity="warn", title=f"hist cap {i}",
        )
    capped = pod_state_signals._history_handler(
        shared_dir=tmp_path, max_results=5,
    )
    assert capped["count"] == 20
    assert len(capped["entries"]) == 5
    assert capped["truncated"] is True


def test_signals_history_tool_is_registered():
    found = _tools.lookup("pod_state.signals.history")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.READ
    assert found.validate is None


# ─────────────────────────────────────────────────────────────────────────────
# pod_state.proposals.pending — Better Engine queue
# ─────────────────────────────────────────────────────────────────────────────


def _seed_proposal(shared_dir, *, proposal_id, bot_id, generator_id,
                   dimension, problem, urgency, status, summary=""):
    """Helper: write a Proposal directly to shared_dir/proposals/<status>/.

    Uses arbiter.store.write_proposal so the on-disk shape matches what
    iter_proposals expects (vs. hand-crafting JSON, which could drift).

    The Investigation action is the simplest valid Action variant —
    just needs a context string. Provenance needs a non-empty technique
    string per its __post_init__ guard."""
    from arbiter import store as arbiter_store
    from schema.proposal import Proposal, Investigation, RiskTag
    from schema.provenance import Provenance

    p = Proposal(
        id=proposal_id,
        bot_id=bot_id,
        generator_id=generator_id,
        dimension=dimension,
        trigger_observations=[],
        provenance=Provenance(technique="test"),
        problem=problem,
        action=Investigation(context=problem),
        risk_tag=RiskTag(blast_radius="local", reversibility="reversible"),
        urgency=urgency,
        admin_surface_summary=summary,
        status=status,
    )
    arbiter_store.write_proposal(p, shared_dir, subdir=status)
    return p


def test_proposals_pending_empty(tmp_path):
    from evolve_admin.evo.tools import pod_state_proposals
    result = pod_state_proposals._pending_handler(shared_dir=tmp_path)
    assert result == {"count": 0, "proposals": [], "truncated": False}


def test_proposals_pending_lists_pending_only(tmp_path):
    """A pending proposal appears; a snoozed one does not."""
    from evolve_admin.evo.tools import pod_state_proposals
    _seed_proposal(
        tmp_path, proposal_id="p-pending-1",
        bot_id="team_bot_a", generator_id="cost_capper",
        dimension="cost", problem="Team_bot_a cache TTL too short",
        urgency="improvement", status="pending",
        summary="Raise team_bot_a cache TTL from 1h to 4h",
    )
    _seed_proposal(
        tmp_path, proposal_id="p-snoozed-1",
        bot_id="team_bot_a", generator_id="cost_capper",
        dimension="cost", problem="Snoozed proposal",
        urgency="improvement", status="snoozed",
        summary="Snoozed thing",
    )
    result = pod_state_proposals._pending_handler(shared_dir=tmp_path)
    assert result["count"] == 1
    assert result["proposals"][0]["id"] == "p-pending-1"
    assert result["proposals"][0]["summary"] == "Raise team_bot_a cache TTL from 1h to 4h"


def test_proposals_pending_filters_by_bot(tmp_path):
    from evolve_admin.evo.tools import pod_state_proposals
    _seed_proposal(
        tmp_path, proposal_id="p-team_bot_a",
        bot_id="team_bot_a", generator_id="g", dimension="cost",
        problem="team_bot_a", urgency="improvement", status="pending",
    )
    _seed_proposal(
        tmp_path, proposal_id="p-admin_bot",
        bot_id="admin_bot", generator_id="g", dimension="cost",
        problem="admin_bot", urgency="improvement", status="pending",
    )
    team_bot_a = pod_state_proposals._pending_handler(
        shared_dir=tmp_path, bot_id="team_bot_a",
    )
    assert {p["id"] for p in team_bot_a["proposals"]} == {"p-team_bot_a"}


def test_proposals_pending_filters_by_urgency(tmp_path):
    from evolve_admin.evo.tools import pod_state_proposals
    _seed_proposal(
        tmp_path, proposal_id="p-urgent",
        bot_id="team_bot_a", generator_id="g", dimension="cost",
        problem="hot", urgency="urgent", status="pending",
    )
    _seed_proposal(
        tmp_path, proposal_id="p-improve",
        bot_id="team_bot_a", generator_id="g", dimension="cost",
        problem="meh", urgency="improvement", status="pending",
    )
    urgent = pod_state_proposals._pending_handler(
        shared_dir=tmp_path, urgency="urgent",
    )
    assert {p["id"] for p in urgent["proposals"]} == {"p-urgent"}


def test_proposals_projection_drops_verbose_fields(tmp_path):
    """Projection must not leak the deep Proposal blobs (action,
    provenance, trigger_observations, revisions). Those are not
    LLM-useful and bloat prompts."""
    from evolve_admin.evo.tools import pod_state_proposals
    _seed_proposal(
        tmp_path, proposal_id="p-proj",
        bot_id="team_bot_a", generator_id="g", dimension="cost",
        problem="projection test", urgency="improvement",
        status="pending", summary="proj",
    )
    result = pod_state_proposals._pending_handler(shared_dir=tmp_path)
    p = result["proposals"][0]
    # Verbose fields that should NOT appear
    for forbidden in ("action", "provenance", "trigger_observations",
                      "revisions", "history", "guardian_annotations",
                      "conflicts_with", "motivating_signals",
                      "signature", "schema_version"):
        assert forbidden not in p, f"projection leaked {forbidden}"


def test_proposals_snoozed_lists_snoozed_only(tmp_path):
    from evolve_admin.evo.tools import pod_state_proposals
    _seed_proposal(
        tmp_path, proposal_id="p-pending",
        bot_id="team_bot_a", generator_id="g", dimension="cost",
        problem="p", urgency="improvement", status="pending",
    )
    _seed_proposal(
        tmp_path, proposal_id="p-snoozed",
        bot_id="team_bot_a", generator_id="g", dimension="cost",
        problem="s", urgency="improvement", status="snoozed",
    )
    result = pod_state_proposals._snoozed_handler(shared_dir=tmp_path)
    assert {p["id"] for p in result["proposals"]} == {"p-snoozed"}


def test_proposals_tools_registered():
    assert _tools.lookup("pod_state.proposals.pending") is not None
    assert _tools.lookup("pod_state.proposals.snoozed") is not None
    assert _tools.lookup("pod_state.proposals.in_process") is not None


# ─────────────────────────────────────────────────────────────────────────────
# pod_state.proposals.in_process — applied + manual-completion kinds
# ─────────────────────────────────────────────────────────────────────────────
#
# Added 2026-05-20 after a transcript where the operator was looking at
# the In Process tab on the Recommendations page and asked evo to mark
# a proposal complete. Evo only had .pending — couldn't see applied-
# status items at all.


def _seed_applied_proposal(
    shared_dir,
    *,
    proposal_id: str,
    bot_id: str,
    summary: str = "",
    action_kind: str = "Investigation",
):
    """Seed a proposal in the applied/ subdir with a specific action
    kind. Investigation (manual-completion) → should appear in
    in_process. ConfigPatch (auto-completion) → should NOT."""
    from arbiter import store as arbiter_store
    from schema.proposal import Proposal, Investigation, WorkflowInstruction, ConfigPatch, RiskTag
    from schema.provenance import Provenance

    if action_kind == "Investigation":
        action = Investigation(context=summary or "test")
    elif action_kind == "WorkflowInstruction":
        action = WorkflowInstruction(
            bot_id=bot_id, path="workspace/instructions.md",
            content="# do this",
        )
    elif action_kind == "ConfigPatch":
        action = ConfigPatch(
            target_path="team_bot_a.config.foo",
            operation="set",
            value="bar",
        )
    else:
        raise ValueError(f"unsupported action_kind for test: {action_kind}")

    p = Proposal(
        id=proposal_id,
        bot_id=bot_id,
        generator_id="test",
        dimension="cost",
        trigger_observations=[],
        provenance=Provenance(technique="test"),
        problem=summary or "test problem",
        action=action,
        risk_tag=RiskTag(blast_radius="local", reversibility="reversible"),
        urgency="improvement",
        admin_surface_summary=summary,
        status="applied",
    )
    arbiter_store.write_proposal(p, shared_dir, subdir="applied")
    return p


def test_proposals_in_process_empty(tmp_path):
    from evolve_admin.evo.tools import pod_state_proposals
    result = pod_state_proposals._in_process_handler(shared_dir=tmp_path)
    assert result == {"count": 0, "proposals": [], "truncated": False}


def test_proposals_in_process_returns_investigation(tmp_path):
    """An applied Investigation proposal → appears in in_process.
    This is the core case — the operator's transcript was about
    exactly this kind of proposal (cve-scan test-gate investigation)."""
    from evolve_admin.evo.tools import pod_state_proposals
    _seed_applied_proposal(
        tmp_path, proposal_id="p-inv-1",
        bot_id="evolve",
        summary="security-cve-scan has no tests defined",
        action_kind="Investigation",
    )
    result = pod_state_proposals._in_process_handler(shared_dir=tmp_path)
    assert result["count"] == 1
    p = result["proposals"][0]
    assert p["id"] == "p-inv-1"
    assert p["bot_id"] == "evolve"
    # action_kind is augmented on the projection so the model can tell
    # Investigation from WorkflowInstruction without a separate call.
    assert p["action_kind"] == "Investigation"
    assert p["status"] == "applied"


def test_proposals_in_process_returns_workflow_instruction(tmp_path):
    """WorkflowInstruction is also manual-completion → shows up."""
    from evolve_admin.evo.tools import pod_state_proposals
    _seed_applied_proposal(
        tmp_path, proposal_id="p-wf-1",
        bot_id="team_bot_a",
        summary="add caps to team_bot_a cron jobs",
        action_kind="WorkflowInstruction",
    )
    result = pod_state_proposals._in_process_handler(shared_dir=tmp_path)
    assert result["count"] == 1
    assert result["proposals"][0]["action_kind"] == "WorkflowInstruction"


def test_proposals_in_process_excludes_auto_completion_kinds(tmp_path):
    """ConfigPatch is auto-promoted by apply.py — it shouldn't surface
    in in_process even when its status is applied. If it did, the
    operator would see proposals on the In Process tab that they
    have no manual action for, then click 'Mark complete' on a
    proposal that gets auto-cleared anyway. Confusing."""
    from evolve_admin.evo.tools import pod_state_proposals
    _seed_applied_proposal(
        tmp_path, proposal_id="p-config-auto",
        bot_id="team_bot_a",
        summary="auto-applied config patch",
        action_kind="ConfigPatch",
    )
    result = pod_state_proposals._in_process_handler(shared_dir=tmp_path)
    assert result["count"] == 0, (
        "ConfigPatch (auto-completion kind) leaked into in_process. "
        "Only manual-completion kinds belong here."
    )


def test_proposals_in_process_excludes_pending(tmp_path):
    """Pending Investigation proposals belong in .pending, not
    .in_process. Separation enforced by the subdir filter."""
    from evolve_admin.evo.tools import pod_state_proposals
    _seed_proposal(
        tmp_path, proposal_id="p-pending-inv",
        bot_id="team_bot_a", generator_id="g", dimension="cost",
        problem="not yet accepted", urgency="improvement",
        status="pending",
    )
    result = pod_state_proposals._in_process_handler(shared_dir=tmp_path)
    assert result["count"] == 0


def test_proposals_pending_excludes_applied_investigation(tmp_path):
    """Symmetric guard: an applied Investigation should NOT appear in
    .pending. If this regresses, evo would 'find' the proposal via
    .pending and try to .apply it (already applied) instead of
    .mark_complete-ing it. Cross-tool isolation matters."""
    from evolve_admin.evo.tools import pod_state_proposals
    _seed_applied_proposal(
        tmp_path, proposal_id="p-applied-inv",
        bot_id="evolve",
        summary="already accepted",
        action_kind="Investigation",
    )
    result = pod_state_proposals._pending_handler(shared_dir=tmp_path)
    assert result["count"] == 0


def test_proposals_in_process_filters_by_bot(tmp_path):
    from evolve_admin.evo.tools import pod_state_proposals
    _seed_applied_proposal(
        tmp_path, proposal_id="p-evolve",
        bot_id="evolve", summary="evolve thing",
        action_kind="Investigation",
    )
    _seed_applied_proposal(
        tmp_path, proposal_id="p-team_bot_a",
        bot_id="team_bot_a", summary="team_bot_a thing",
        action_kind="Investigation",
    )
    result = pod_state_proposals._in_process_handler(
        shared_dir=tmp_path, bot_id="evolve",
    )
    assert {p["id"] for p in result["proposals"]} == {"p-evolve"}


def test_proposals_in_process_filters_by_proposal_id_for_verify(tmp_path):
    """verify_via after action.proposal.mark_complete passes the
    proposal_id; count=0 confirms it left in_process."""
    from evolve_admin.evo.tools import pod_state_proposals
    _seed_applied_proposal(
        tmp_path, proposal_id="p-target",
        bot_id="evolve", summary="target",
        action_kind="Investigation",
    )
    found = pod_state_proposals._in_process_handler(
        shared_dir=tmp_path, proposal_id="p-target",
    )
    assert found["count"] == 1
    # Verify "did it leave" case — proposal_id that's not present
    missing = pod_state_proposals._in_process_handler(
        shared_dir=tmp_path, proposal_id="p-not-here",
    )
    assert missing["count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# pod_state.bots — per-bot status snapshot
# ─────────────────────────────────────────────────────────────────────────────


def _write_network_json(tmp_path: Path, **overrides):
    """Helper: write a minimal valid network.json into tmp_path/network.json.
    Tests pass that path to the tool to avoid real-network-state coupling."""
    network = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path),
        "primary": "evolve",
        "members": ["evolve", "team_bot_a"],
        "bots": {
            "evolve": {"role": "primary", "port": 19030},
            "team_bot_a": {"role": "member", "port": 19031},
        },
    }
    network.update(overrides)
    p = tmp_path / "network.json"
    p.write_text(json.dumps(network))
    return p


def test_bots_handler_lists_all(tmp_path):
    from evolve_admin.evo.tools import pod_state_bots
    network_path = _write_network_json(tmp_path)
    result = pod_state_bots._handler(network_path=network_path)
    assert result["count"] == 2
    ids = {b["bot_id"] for b in result["bots"]}
    assert ids == {"evolve", "team_bot_a"}
    assert result["primary"] == "evolve"
    assert result["network_id"] == "test-pod"


def test_bots_handler_filters_by_bot_id(tmp_path):
    from evolve_admin.evo.tools import pod_state_bots
    network_path = _write_network_json(tmp_path)
    result = pod_state_bots._handler(
        network_path=network_path, bot_id="team_bot_a",
    )
    assert result["count"] == 1
    assert result["bots"][0]["bot_id"] == "team_bot_a"
    assert result["bots"][0]["role"] == "member"


def test_bots_handler_unknown_bot_returns_error(tmp_path):
    from evolve_admin.evo.tools import pod_state_bots
    network_path = _write_network_json(tmp_path)
    result = pod_state_bots._handler(
        network_path=network_path, bot_id="no-such-bot",
    )
    assert result["count"] == 0
    assert "not found" in result.get("error", "")


def test_bots_handler_status_offline_when_no_metric(tmp_path):
    """A bot with no live=True and no last_metric_date is offline.
    Confirms the projection's derived-status logic."""
    from evolve_admin.evo.tools import pod_state_bots
    network_path = _write_network_json(tmp_path)
    result = pod_state_bots._handler(
        network_path=network_path, bot_id="team_bot_a",
    )
    # No live gateway in test → status is offline
    assert result["bots"][0]["status"] == "offline"


def test_bots_tool_registered():
    found = _tools.lookup("pod_state.bots")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.READ


# ─── tile_chips enrichment ─────────────────────────────────────────────────


def test_bots_includes_tile_chips_when_computable(tmp_path, monkeypatch):
    """Each bot in the response carries ``tile_chips`` — the same health
    pills the admin UI dashboard renders. Closes the gap where evo
    didn't know about UI-visible state like 'scan needed'."""
    from evolve_admin.evo.tools import pod_state_bots

    # Stub compute_tile_data so the test doesn't require a real
    # applications/.scan-status.json setup. Each bot gets a known chip
    # we assert on. Verifies the projection runs per-bot and the chips
    # land on the right bot.
    def fake_compute(*, shared_dir, bot_id, bot_data, network):
        return {
            "health_chips": [
                {
                    "id": "scan_needed",
                    "severity": "warn",
                    "label": "scan needed",
                    "detail": f"never scanned ({bot_id})",
                    "nav": "capabilities",  # stripped by projector
                },
            ],
        }
    monkeypatch.setattr(pod_state_bots, "_compute_chips_for_bot", lambda *a, **k: fake_compute(shared_dir=a[0], bot_id=a[1], bot_data=a[2], network=a[3])["health_chips"])

    network_path = _write_network_json(tmp_path)
    result = pod_state_bots._handler(network_path=network_path)
    assert result["count"] == 2
    for bot in result["bots"]:
        assert "tile_chips" in bot, f"bot {bot['bot_id']} missing tile_chips"
        chips = bot["tile_chips"]
        assert len(chips) == 1
        chip = chips[0]
        # All expected fields survived the projection
        assert chip["id"] == "scan_needed"
        assert chip["severity"] == "warn"
        assert chip["label"] == "scan needed"
        assert chip["detail"] == f"never scanned ({bot['bot_id']})"
        # nav stripped — model doesn't navigate, doesn't need it
        assert "nav" not in chip


def test_bots_omits_tile_chips_when_not_computable(tmp_path, monkeypatch):
    """When chip computation isn't possible (analyzer unavailable / per-bot
    failure), the field is omitted entirely — distinguishable from
    'computed and all clear' (which is an empty list)."""
    from evolve_admin.evo.tools import pod_state_bots
    monkeypatch.setattr(
        pod_state_bots, "_compute_chips_for_bot",
        lambda *a, **k: None,
    )
    network_path = _write_network_json(tmp_path)
    result = pod_state_bots._handler(network_path=network_path)
    for bot in result["bots"]:
        assert "tile_chips" not in bot, (
            f"bot {bot['bot_id']} got tile_chips when computation returned None — "
            "absence is the signal for 'not computed'"
        )


def test_bots_tile_chips_per_bot_filter(tmp_path, monkeypatch):
    """When the caller filters to a single bot via bot_id, that bot's
    chips still come through (regression: an earlier draft only ran the
    chip computation in the all-bots branch)."""
    from evolve_admin.evo.tools import pod_state_bots

    def fake_chips(shared_dir, bot_id, bot_data, network):
        return [{"id": "cost_spike", "severity": "warn",
                 "label": "cost spike", "detail": f"hit on {bot_id}"}]
    monkeypatch.setattr(pod_state_bots, "_compute_chips_for_bot", fake_chips)

    network_path = _write_network_json(tmp_path)
    result = pod_state_bots._handler(
        network_path=network_path, bot_id="evolve",
    )
    assert result["count"] == 1
    assert result["bots"][0]["bot_id"] == "evolve"
    assert result["bots"][0]["tile_chips"][0]["id"] == "cost_spike"
    assert "hit on evolve" in result["bots"][0]["tile_chips"][0]["detail"]


def test_bots_tile_chips_failure_isolated_per_bot(tmp_path, monkeypatch):
    """One bot's chip-computation failure must not break the whole
    response — the other bots still get their chips, and the failing bot
    gets the field omitted. Mirrors the defensive pattern the admin
    server's /api/status uses."""
    from evolve_admin.evo.tools import pod_state_bots

    def selective_chips(shared_dir, bot_id, bot_data, network):
        if bot_id == "team_bot_a":
            return None  # simulate per-bot failure
        return [{"id": "scan_needed", "severity": "warn",
                 "label": "scan needed", "detail": "ok"}]
    monkeypatch.setattr(pod_state_bots, "_compute_chips_for_bot", selective_chips)

    network_path = _write_network_json(tmp_path)
    result = pod_state_bots._handler(network_path=network_path)
    by_id = {b["bot_id"]: b for b in result["bots"]}
    assert "tile_chips" in by_id["evolve"]
    assert by_id["evolve"]["tile_chips"][0]["id"] == "scan_needed"
    assert "tile_chips" not in by_id["team_bot_a"]


def test_compute_chips_returns_none_when_tile_metrics_unavailable(
    tmp_path, monkeypatch
):
    """The helper itself is defensive — ImportError on tile_metrics
    collapses to None, not a raised exception (so /api/status-equivalent
    callers can keep going). Simulates the analyzer-not-on-sys.path
    edge case the docstring warns about."""
    from evolve_admin.evo.tools import pod_state_bots
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tile_metrics":
            raise ImportError("tile_metrics unavailable in this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    chips = pod_state_bots._compute_chips_for_bot(
        shared_dir=tmp_path, bot_id="evolve", bot_data={}, network={},
    )
    assert chips is None


# ─────────────────────────────────────────────────────────────────────────────
# pod_state.host — host snapshot
# ─────────────────────────────────────────────────────────────────────────────


def test_host_handler_returns_snapshot():
    """Calls the real host_health.collect_host_health. Result is either
    available=True with metrics, or available=False with a reason (when
    psutil isn't installed)."""
    from evolve_admin.evo.tools import pod_state_host
    result = pod_state_host._handler()
    # Either path is valid; check we got the expected keys for the
    # available case
    assert "available" in result
    if result["available"]:
        # The projection should include the headline percentages
        for key in ("cpu_percent", "cpu_status",
                    "memory_percent", "memory_status",
                    "disk_percent", "disk_status"):
            assert key in result, f"missing {key}"


def test_host_handler_projection_excludes_byte_counters():
    """The projection drops the verbose byte-counters
    (total_bytes / used_bytes / free_bytes / available_bytes). Those
    are not LLM-useful — the model just needs the percentage + status."""
    from evolve_admin.evo.tools import pod_state_host
    result = pod_state_host._handler()
    for forbidden in ("total_bytes", "used_bytes", "free_bytes",
                      "available_bytes"):
        assert forbidden not in result


def test_host_tool_registered():
    found = _tools.lookup("pod_state.host")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.READ
    # No filters — host is singular
    assert found.input_schema["properties"] == {}


# ─────────────────────────────────────────────────────────────────────────────
# pod_state.audit — cached OC security-audit results
# ─────────────────────────────────────────────────────────────────────────────


def test_audit_empty_cache_returns_stale_marker():
    """No audit run yet → stale=true, count=0. The model should be
    able to tell the operator audits aren't gathered yet rather than
    reporting clean state."""
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()
    result = pod_state_audit._handler()
    assert result["count"] == 0
    assert result["audits"] == []
    assert result["stale"] is True


def test_audit_returns_per_bot_summary():
    """A populated cache yields per-bot projections with counts +
    findings_top."""
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()
    # Simulate the admin-server background thread populating the cache
    audit_state._state["data"]["team_bot_a"] = {
        "summary": {"critical": 1, "warn": 2, "info": 5},
        "findings": [
            {
                "checkId": "auth.weak_token",
                "severity": "critical",
                "title": "Gateway auth token is weak",
                "category": "auth",
                "remediation": "Regenerate token via evolve-admin",
            },
            {
                "checkId": "session.no_isolation",
                "severity": "warning",
                "title": "Sessions not isolated",
                "category": "session",
                "remediation": "Enable isolation on heartbeats",
            },
        ],
    }
    audit_state._state["cached_at"] = 1234567890.0
    try:
        result = pod_state_audit._handler()
        assert result["count"] == 1
        assert result["stale"] is False
        team_bot_a = result["audits"][0]
        assert team_bot_a["bot_id"] == "team_bot_a"
        assert team_bot_a["available"] is True
        assert team_bot_a["critical"] == 1
        assert team_bot_a["warn"] == 2
        assert team_bot_a["info"] == 5
        assert team_bot_a["findings_total"] == 2
        # Severity-sorted: critical comes first
        assert team_bot_a["findings_top"][0]["severity"] == "critical"
        assert team_bot_a["findings_top"][0]["checkId"] == "auth.weak_token"
    finally:
        audit_state.reset()


def test_audit_unavailable_passes_through_error():
    """When a bot's audit returned 'unavailable' (subprocess failed),
    the projection surfaces error_type + error so the model can explain
    why."""
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()
    audit_state._state["data"]["personal_bot"] = {
        "unavailable": True,
        "error_type": "config_invalid",
        "error": "OC validation failed: unknown root key 'sandbox'",
    }
    audit_state._state["cached_at"] = 1234567890.0
    try:
        result = pod_state_audit._handler()
        personal_bot = result["audits"][0]
        assert personal_bot["available"] is False
        assert personal_bot["error_type"] == "config_invalid"
        assert "sandbox" in personal_bot["error"]
    finally:
        audit_state.reset()


def test_audit_filter_by_bot_id():
    """bot_id filter returns just that bot."""
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()
    audit_state._state["data"]["team_bot_a"] = {
        "summary": {"critical": 0, "warn": 0, "info": 0}, "findings": [],
    }
    audit_state._state["data"]["admin_bot"] = {
        "summary": {"critical": 0, "warn": 1, "info": 0}, "findings": [],
    }
    audit_state._state["cached_at"] = 1.0
    try:
        result = pod_state_audit._handler(bot_id="admin_bot")
        assert result["count"] == 1
        assert result["audits"][0]["bot_id"] == "admin_bot"
        assert result["audits"][0]["warn"] == 1
    finally:
        audit_state.reset()


def test_audit_unknown_bot_in_filter_returns_error():
    """bot_id filter for a bot not in the cache returns count=0 + an
    error explaining why."""
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()
    result = pod_state_audit._handler(bot_id="no-such-bot")
    assert result["count"] == 0
    assert "no cached audit" in result["error"]


def test_audit_findings_capped_at_10():
    """Findings list is capped at 10. findings_total + findings_truncated
    let the model know how many got dropped."""
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()
    audit_state._state["data"]["spammy"] = {
        "summary": {"critical": 0, "warn": 0, "info": 50},
        "findings": [
            {
                "checkId": f"info.{i}",
                "severity": "info",
                "title": f"finding {i}",
                "category": "config",
            }
            for i in range(50)
        ],
    }
    audit_state._state["cached_at"] = 1.0
    try:
        result = pod_state_audit._handler()
        spammy = result["audits"][0]
        assert len(spammy["findings_top"]) == 10
        assert spammy["findings_total"] == 50
        assert spammy["findings_truncated"] is True
    finally:
        audit_state.reset()


def test_audit_tool_registered():
    assert _tools.lookup("pod_state.audit") is not None


# ─────────────────────────────────────────────────────────────────────────────
# pod_state.audit — cross-process HTTP fallback path
# ─────────────────────────────────────────────────────────────────────────────
#
# In production the tool runs in the MCP server child-process, which does
# NOT share memory with the admin server. ``audit_state._state`` in that
# process is permanently empty — the writer (background audit thread)
# lives in the admin server process and never reaches across the
# boundary. So the tool falls through to ``GET /api/security/audit`` on
# the admin server's HTTP surface.
#
# These tests stub ``urllib.request.urlopen`` so we exercise the HTTP
# branch without standing up a real Flask app. ``audit_state.reset()``
# in each test forces the in-process path to miss, mimicking production.


class _FakeUrlopenResp:
    """Mimics urllib's HTTPResponse just enough for the tool's reader."""

    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _seed_network(tmp_path, admin_base_url: str = "http://test-host:5050"):
    """Drop a minimal network.json so resolve_admin_base_url picks up
    the configured URL. Returns the path."""
    import json
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "networkId": "test-pod",
        "adminBaseUrl": admin_base_url,
    }))
    return p


def test_audit_falls_through_to_http_when_in_process_empty(monkeypatch, tmp_path):
    """The production case: module-global is empty (we're in the MCP
    server's process), HTTP fetch succeeds, projection works."""
    import json
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()  # ensure in-process miss
    network_path = _seed_network(tmp_path)

    captured_urls: list[str] = []

    def _fake_urlopen(req, timeout=None):
        captured_urls.append(req.full_url)
        return _FakeUrlopenResp(json.dumps({
            "data": {
                "team_bot_a": {
                    "summary": {"critical": 0, "warn": 0, "info": 1},
                    "findings": [{
                        "checkId": "plugin.unpinned_npm",
                        "severity": "info",
                        "title": "Plugin index includes unpinned npm specs",
                        "category": "supply-chain",
                        "remediation": "Pin to exact versions",
                    }],
                },
            },
            "cached_at": 1234567890.0,
            "running": False,
        }))

    monkeypatch.setattr(
        pod_state_audit.urllib.request, "urlopen", _fake_urlopen,
    )
    result = pod_state_audit._handler(network_path=network_path)
    assert result["source"] == "admin_server"
    assert result["stale"] is False
    assert result["count"] == 1
    assert result["audits"][0]["bot_id"] == "team_bot_a"
    assert result["audits"][0]["info"] == 1
    assert result["audits"][0]["findings_top"][0]["checkId"] == "plugin.unpinned_npm"
    # The URL must hit the configured adminBaseUrl, not some default.
    assert captured_urls == ["http://test-host:5050/api/security/audit"]


def test_audit_prefers_in_process_over_http(monkeypatch, tmp_path):
    """When the in-process cache IS populated (test mode / single-process
    invocation), the tool should use it WITHOUT making an HTTP call."""
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()
    audit_state._state["data"]["team_bot_a"] = {
        "summary": {"critical": 0, "warn": 0, "info": 0},
        "findings": [],
    }
    audit_state._state["cached_at"] = 99.0
    network_path = _seed_network(tmp_path)

    called = {"http": False}

    def _explode(req, timeout=None):
        called["http"] = True
        raise AssertionError("HTTP path should not be hit when in-process has data")

    monkeypatch.setattr(
        pod_state_audit.urllib.request, "urlopen", _explode,
    )
    try:
        result = pod_state_audit._handler(network_path=network_path)
        assert result["source"] == "in_process"
        assert result["stale"] is False
        assert called["http"] is False
    finally:
        audit_state.reset()


def test_audit_http_unreachable_returns_stale_with_clear_error(monkeypatch, tmp_path):
    """When the admin server is down (production crash, dev not running),
    the tool returns stale=true with an actionable error rather than
    fabricating clean state."""
    import urllib.error
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()
    network_path = _seed_network(tmp_path)

    def _refuse(req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(
        pod_state_audit.urllib.request, "urlopen", _refuse,
    )
    result = pod_state_audit._handler(network_path=network_path)
    assert result["stale"] is True
    assert result["count"] == 0
    assert result["source"] == "none"
    assert "unreachable" in result["error"].lower()
    assert "Connection refused" in result["error"]


def test_audit_http_returns_500_returns_stale_with_clear_error(monkeypatch, tmp_path):
    """HTTPError from the admin server (5xx, 4xx) surfaces clearly."""
    import urllib.error
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()
    network_path = _seed_network(tmp_path)

    def _fivexx(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 500, "Internal Server Error", {}, None,
        )

    monkeypatch.setattr(
        pod_state_audit.urllib.request, "urlopen", _fivexx,
    )
    result = pod_state_audit._handler(network_path=network_path)
    assert result["stale"] is True
    assert "HTTP 500" in result["error"]


def test_audit_no_network_path_surfaces_clear_error(monkeypatch):
    """When the bridge doesn't inject network_path (rare — bridge bug
    or direct call) the tool returns stale with an error that points
    a developer at the cause rather than silently appearing healthy."""
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()
    # No urlopen monkeypatch — the HTTP branch shouldn't be entered
    # because network_path is None.
    result = pod_state_audit._handler()  # no network_path
    assert result["stale"] is True
    assert result["count"] == 0
    assert "network_path" in (result.get("error") or "").lower() \
        or "admin base URL" in (result.get("error") or "")


def test_audit_http_filter_by_bot_id_returns_one(monkeypatch, tmp_path):
    """bot_id filter still works through the HTTP path."""
    import json
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()
    network_path = _seed_network(tmp_path)

    def _fake_urlopen(req, timeout=None):
        return _FakeUrlopenResp(json.dumps({
            "data": {
                "team_bot_a": {"summary": {"critical": 0, "warn": 0, "info": 1},
                        "findings": []},
                "admin_bot": {"summary": {"critical": 1, "warn": 0, "info": 0},
                          "findings": []},
            },
            "cached_at": 1.0,
        }))

    monkeypatch.setattr(
        pod_state_audit.urllib.request, "urlopen", _fake_urlopen,
    )
    result = pod_state_audit._handler(
        bot_id="admin_bot", network_path=network_path,
    )
    assert result["count"] == 1
    assert result["audits"][0]["bot_id"] == "admin_bot"
    assert result["audits"][0]["critical"] == 1
    assert result["source"] == "admin_server"


def test_audit_http_filter_unknown_bot_includes_admin_error(monkeypatch, tmp_path):
    """When HTTP fetch succeeded but the bot_id isn't in the payload,
    the error should NOT claim 'admin restarted' — it should just say
    the bot isn't in the cache."""
    import json
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()
    network_path = _seed_network(tmp_path)

    def _fake_urlopen(req, timeout=None):
        return _FakeUrlopenResp(json.dumps({
            "data": {"team_bot_a": {"summary": {"info": 0}, "findings": []}},
            "cached_at": 1.0,
        }))

    monkeypatch.setattr(
        pod_state_audit.urllib.request, "urlopen", _fake_urlopen,
    )
    result = pod_state_audit._handler(
        bot_id="not-a-bot", network_path=network_path,
    )
    assert result["count"] == 0
    assert "no cached audit" in result["error"]
    # in_process is None (we reset), so source is admin_server because
    # HTTP succeeded (just didn't have the requested bot).
    assert result["source"] == "admin_server"


def test_audit_http_returns_non_json_surfaces_clear_error(monkeypatch, tmp_path):
    """A misconfigured admin server (returning HTML or text) shouldn't
    crash the tool — it should surface a clear error."""
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()
    network_path = _seed_network(tmp_path)

    monkeypatch.setattr(
        pod_state_audit.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeUrlopenResp("<html>oops</html>"),
    )
    result = pod_state_audit._handler(network_path=network_path)
    assert result["stale"] is True
    assert "non-JSON" in result["error"] or "JSON" in result["error"]


# ─────────────────────────────────────────────────────────────────────────────
# pod_state.audit — disk-mirror path (Sprint 2b)
# ─────────────────────────────────────────────────────────────────────────────
#
# The audit cache moved from a process-local module-global to a disk
# mirror at {shared_dir}/security/audit-cache.json. The MCP server's
# child process now reads the disk file directly — no RPC + no
# in-memory coupling. These tests exercise the disk path end-to-end.


def _write_disk_cache(shared_dir, data, cached_at=1234567890.0):
    """Write a fake audit-cache.json to disk for tests."""
    import json as _json
    cache_dir = shared_dir / "security"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "audit-cache.json").write_text(
        _json.dumps({"data": data, "cached_at": cached_at}),
    )


def test_audit_reads_from_disk_when_in_process_empty(tmp_path):
    """The disk mirror is the canonical cross-process path. With
    in-process empty AND the disk file present, the tool should read
    from disk."""
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()
    _write_disk_cache(tmp_path, {
        "team_bot_a": {
            "summary": {"critical": 0, "warn": 0, "info": 1},
            "findings": [{
                "checkId": "plugin.unpinned_npm",
                "severity": "info",
                "title": "Plugin index includes unpinned npm specs",
                "category": "supply-chain",
                "remediation": "Pin to exact versions",
            }],
        },
    })
    result = pod_state_audit._handler(shared_dir=tmp_path)
    assert result["source"] == "disk"
    assert result["stale"] is False
    assert result["count"] == 1
    assert result["audits"][0]["bot_id"] == "team_bot_a"
    assert result["audits"][0]["info"] == 1


def test_audit_disk_path_beats_http_path(monkeypatch, tmp_path):
    """When BOTH the disk file and an admin server are reachable, the
    tool should prefer disk (cheaper, no RPC). The HTTP path should
    not even be invoked."""
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()
    _write_disk_cache(tmp_path, {
        "team_bot_a": {"summary": {"critical": 0, "warn": 0, "info": 0},
                "findings": []},
    })
    network_path = _seed_network(tmp_path)

    called = {"http": False}

    def _explode(req, timeout=None):
        called["http"] = True
        raise AssertionError("HTTP path should not be hit when disk has data")

    monkeypatch.setattr(
        pod_state_audit.urllib.request, "urlopen", _explode,
    )
    result = pod_state_audit._handler(
        network_path=network_path, shared_dir=tmp_path,
    )
    assert result["source"] == "disk"
    assert called["http"] is False


def test_audit_in_process_beats_disk(tmp_path):
    """When the in-process module-global has data AND a disk file
    exists, in-process wins (tests rely on this — they poke _state
    directly and expect to read it back without disk interference)."""
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()
    # Both populated — in-process should win.
    audit_state._state["data"]["team_bot_a"] = {
        "summary": {"critical": 1, "warn": 0, "info": 0}, "findings": [],
    }
    audit_state._state["cached_at"] = 99.0
    _write_disk_cache(tmp_path, {
        "team_bot_a": {"summary": {"critical": 999, "warn": 0, "info": 0},
                "findings": []},  # different — never read
    })
    try:
        result = pod_state_audit._handler(shared_dir=tmp_path)
        assert result["source"] == "in_process"
        assert result["audits"][0]["critical"] == 1
    finally:
        audit_state.reset()


def test_audit_disk_falls_through_to_http_when_missing(monkeypatch, tmp_path):
    """If the disk file doesn't exist (admin server hasn't run yet or
    shared_dir mismatch), the tool falls through to HTTP."""
    import json as _json
    from evolve_admin.evo.tools import pod_state_audit
    from evolve_admin import audit_state
    audit_state.reset()
    network_path = _seed_network(tmp_path)
    # Disk file does NOT exist (we don't call _write_disk_cache).

    def _fake_urlopen(req, timeout=None):
        return _FakeUrlopenResp(_json.dumps({
            "data": {"team_bot_a": {"summary": {"info": 1}, "findings": []}},
            "cached_at": 1.0,
        }))

    monkeypatch.setattr(
        pod_state_audit.urllib.request, "urlopen", _fake_urlopen,
    )
    result = pod_state_audit._handler(
        network_path=network_path, shared_dir=tmp_path,
    )
    assert result["source"] == "admin_server"


def test_audit_state_persist_writes_atomic_file(tmp_path):
    """``audit_state.persist`` must write the in-memory state to disk
    atomically (temp file + rename), so a reader between writes never
    sees a partial JSON file."""
    import json as _json
    from evolve_admin import audit_state
    audit_state.reset()
    audit_state._state["data"]["team_bot_a"] = {
        "summary": {"critical": 0, "warn": 0, "info": 2},
        "findings": [],
    }
    audit_state._state["cached_at"] = 555.0
    try:
        audit_state.persist(tmp_path)
        path = tmp_path / "security" / "audit-cache.json"
        assert path.exists()
        loaded = _json.loads(path.read_text())
        assert loaded["data"]["team_bot_a"]["summary"]["info"] == 2
        assert loaded["cached_at"] == 555.0
    finally:
        audit_state.reset()


def test_audit_state_hydrate_loads_disk_into_memory(tmp_path):
    """``audit_state.hydrate`` reads the disk mirror back into the
    in-memory ``_state`` so a restarted admin server picks up where it
    left off."""
    from evolve_admin import audit_state
    audit_state.reset()
    _write_disk_cache(tmp_path, {
        "admin_bot": {"summary": {"critical": 0, "warn": 1, "info": 0},
                  "findings": []},
    }, cached_at=42.0)
    try:
        audit_state.hydrate(tmp_path)
        assert "admin_bot" in audit_state._state["data"]
        assert audit_state._state["cached_at"] == 42.0
    finally:
        audit_state.reset()


def test_audit_state_hydrate_silent_on_missing(tmp_path):
    """Hydrate is idempotent + silent when there's no disk file
    yet. Safe to call at server startup before any audits have run."""
    from evolve_admin import audit_state
    audit_state.reset()
    audit_state.hydrate(tmp_path)  # must not raise
    assert audit_state._state["data"] == {}
    assert audit_state._state["cached_at"] is None


def test_audit_state_snapshot_disk_path_isolated_from_memory(tmp_path):
    """``snapshot(shared_dir=X)`` reads disk without touching
    ``_state``. Tests that need fresh in-memory state must
    explicitly reset; this test confirms the snapshot itself is
    read-only on the in-memory side."""
    from evolve_admin import audit_state
    audit_state.reset()
    _write_disk_cache(tmp_path, {
        "personal_bot": {"summary": {"critical": 0, "warn": 0, "info": 0},
                  "findings": []},
    })
    snap = audit_state.snapshot(tmp_path)
    assert "personal_bot" in snap["data"]
    # In-memory should still be empty — snapshot read disk, not memory.
    assert audit_state._state["data"] == {}


# ─────────────────────────────────────────────────────────────────────────────
# config.bot — bot's openclaw.json (summarized + secrets redacted)
# ─────────────────────────────────────────────────────────────────────────────


def test_config_bot_redact_walk_matches_secret_paths():
    """The _walk_redact / _is_redacted_path machinery correctly catches
    the documented secret paths."""
    from evolve_admin.evo.tools.config_bot import _walk_redact
    cfg = {
        "gateway": {
            "port": 19030,
            "auth": {"mode": "token", "token": "supersecret-token-abc"},
        },
        "channels": {
            "telegram": {
                "botToken": "tg-secret-123",
                "enabled": True,
            },
            "slack": {
                "signingSecret": "slack-secret",
                "botToken": "xoxb-secret",
            },
        },
        "plugins": {
            "entries": {
                "evolve": {
                    "enabled": True,
                    "config": {"api_key": "ak-secret", "harmless": "value"},
                },
            },
        },
        "tools": {
            "exec": {"security": "deny", "ask": "on-miss"},
        },
    }
    redacted = _walk_redact(cfg)
    # Secrets are sentinel'd
    assert redacted["gateway"]["auth"]["token"].startswith("[redacted")
    assert redacted["channels"]["telegram"]["botToken"].startswith("[redacted")
    assert redacted["channels"]["slack"]["signingSecret"].startswith("[redacted")
    assert redacted["channels"]["slack"]["botToken"].startswith("[redacted")
    assert redacted["plugins"]["entries"]["evolve"]["config"]["api_key"].startswith("[redacted")
    # Non-secret fields stay verbatim
    assert redacted["gateway"]["port"] == 19030
    assert redacted["channels"]["telegram"]["enabled"] is True
    assert redacted["plugins"]["entries"]["evolve"]["config"]["harmless"] == "value"
    assert redacted["tools"]["exec"]["security"] == "deny"


def test_config_bot_handler_returns_projection(tmp_path):
    """End-to-end: read a fake openclaw.json from a tmp_path-based bot
    home, confirm the projection has the expected shape and secrets
    are redacted."""
    from evolve_admin.evo.tools import config_bot

    # Build a network.json + a fake bot's openclaw.json.
    network = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path),
        "primary": "evolve",
        "members": ["fakebot"],
        "bots": {"fakebot": {"role": "member", "port": 19031, "user": "fakebot"}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # bot_home() will look up the user's pwd entry; falls back to
    # /Users/{user} if pwd doesn't know about them. So we put the
    # openclaw.json there.
    bot_workspace = Path(f"/Users/fakebot") / ".openclaw"
    if not bot_workspace.exists():
        # On the dev machine we can't create /Users/fakebot, so let's
        # just verify the handler's response on missing-config.
        result = config_bot._handler(network_path=network_path, bot_id="fakebot")
        assert result["bot_id"] == "fakebot"
        assert result["available"] is False
        # Tri-state: the config is either genuinely absent or unreadable
        # (permission) — never silently "could not read" with both folded.
        assert result["read_state"] in ("absent", "indeterminate")
        assert result["error"]
        return
    # If we got here, we can write a real file (unlikely in CI but
    # supported)
    bot_workspace.mkdir(parents=True, exist_ok=True)
    oc_config = {
        "gateway": {"port": 19031, "auth": {"mode": "token", "token": "secret"}},
        "agents": {"defaults": {
            "model": {"primary": "anthropic/claude-sonnet-4-6"},
            "heartbeat": {"isolatedSession": True},
        }},
        "channels": {"telegram": {"enabled": True, "botToken": "tg"}},
        "plugins": {"entries": {"evolve": {"enabled": True, "version": "1.0"}}},
        "tools": {"exec": {"security": "deny"}},
    }
    (bot_workspace / "openclaw.json").write_text(json.dumps(oc_config))
    result = config_bot._handler(network_path=network_path, bot_id="fakebot")
    assert result["available"] is True
    cfg = result["config"]
    assert cfg["gateway"]["port"] == 19031
    assert cfg["agents"]["model_primary"] == "anthropic/claude-sonnet-4-6"
    assert cfg["channels"]["telegram"]["enabled"] is True
    # botToken redacted
    assert cfg["channels"]["telegram"]["auth_marker"].startswith("[redacted")
    assert cfg["plugins"]["evolve"]["enabled"] is True
    assert cfg["tools"]["exec_security"] == "deny"


def test_config_bot_handler_missing_bot_returns_error(tmp_path):
    """Unknown bot or unreadable config returns available=false + error."""
    from evolve_admin.evo.tools import config_bot
    network_path = _write_network_json(tmp_path)
    result = config_bot._handler(
        network_path=network_path, bot_id="no-such-bot",
    )
    # bot_home() succeeds even for unknown bot (falls back to /Users/{user}),
    # then the openclaw.json read fails → available=false
    assert result["available"] is False


def test_config_bot_handler_requires_bot_id(tmp_path):
    """Empty bot_id is an error, not a default-all behavior."""
    from evolve_admin.evo.tools import config_bot
    result = config_bot._handler(network_path=tmp_path, bot_id="")
    assert "bot_id is required" in result.get("error", "")


def test_config_bot_tool_registered():
    found = _tools.lookup("config.bot")
    assert found is not None
    # Required arg
    assert found.input_schema["required"] == ["bot_id"]


# ─────────────────────────────────────────────────────────────────────────────
# config.network — pod-level network.json (summarized, secrets redacted)
# ─────────────────────────────────────────────────────────────────────────────


def test_config_network_projection_redacts_alerts_chat_id():
    """alerts.chatId is replaced with chat_configured: true/false."""
    from evolve_admin.evo.tools.config_network import _project_network
    net = {
        "networkId": "test-pod",
        "sharedDir": "/Users/Shared/evolve",
        "primary": "evolve",
        "members": ["evolve", "team_bot_a"],
        "bots": {
            "evolve": {"role": "primary", "port": 19030, "user": "evolve"},
            "team_bot_a": {"role": "member", "port": 19031, "user": "team_bot_a"},
        },
        "alerts": {
            "channel": "telegram",
            "chatId": "1234567890",  # operator privacy
        },
    }
    projected = _project_network(net)
    assert projected["network_id"] == "test-pod"
    assert projected["primary"] == "evolve"
    assert set(projected["members"]) == {"evolve", "team_bot_a"}
    # chatId redacted to chat_configured
    assert projected["alerts"]["channel"] == "telegram"
    assert projected["alerts"]["chat_configured"] is True
    assert "chatId" not in projected["alerts"]


def test_config_network_projection_keeps_bot_static_config():
    """Per-bot static config (role/port/user) appears but tokens don't."""
    from evolve_admin.evo.tools.config_network import _project_network
    net = {
        "networkId": "p",
        "members": ["x"],
        "bots": {
            "x": {
                "role": "member", "port": 19032, "user": "xuser",
                # If someone ever stashed a secret here, it shouldn't surface
                "secret_token": "BAD",
            },
        },
    }
    projected = _project_network(net)
    assert projected["bots"]["x"]["role"] == "member"
    assert projected["bots"]["x"]["port"] == 19032
    assert projected["bots"]["x"]["user"] == "xuser"
    # We hand-pick fields; secret_token (unrecognized) doesn't survive
    assert "secret_token" not in projected["bots"]["x"]


def test_config_network_projection_includes_pod_report_thresholds():
    """pod_report.thresholds is an operator tuning surface; should be
    visible so the model can answer 'what's our cost-anomaly factor?'."""
    from evolve_admin.evo.tools.config_network import _project_network
    net = {
        "networkId": "p",
        "members": ["x"],
        "bots": {"x": {"role": "member"}},
        "pod_report": {
            "thresholds": {"cost_anomaly_factor": 2.5, "pod_silent_session_floor": 0},
        },
    }
    projected = _project_network(net)
    assert projected["pod_report"]["thresholds"]["cost_anomaly_factor"] == 2.5


def test_config_network_handler_missing_file(tmp_path):
    """When network.json is absent, return available=false + error."""
    from evolve_admin.evo.tools import config_network
    result = config_network._handler(network_path=tmp_path / "nope.json")
    # load_network() may either raise FileNotFoundError or return defaults
    # depending on its implementation; the tool either way must surface
    # the result cleanly.
    if not result.get("available"):
        assert "error" in result
    else:
        # If load_network returned defaults, the projection should still
        # be sane (no crash)
        assert "config" in result


def test_config_network_tool_registered():
    found = _tools.lookup("config.network")
    assert found is not None
    # No required args
    assert found.input_schema.get("required", []) == []


# ─────────────────────────────────────────────────────────────────────────────
# meta.tools — registry introspection (Reliability lever #5)
# ─────────────────────────────────────────────────────────────────────────────


def test_meta_tools_registered():
    """The introspection tool itself is in the registry. Without
    this, evo can't answer 'what can you do?' from live data."""
    found = _tools.lookup("meta.tools")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.READ
    # No required args — the model can call meta.tools() bare.
    assert found.input_schema.get("required", []) == []


# Phase 2 authorization framework: meta.tools now filters by
# caller scope. These pre-Phase-2 tests exercised the legacy "all
# tools" view, which post-Phase-2 corresponds to an admin caller —
# every test below passes an explicit admin CallerIdentity so the
# original assertions stay valid. The cross-bot path is covered in
# ``test_tool_authorization.py``.
def _admin_caller():
    from evolve_admin.evo.tools.authorization import CallerIdentity
    return CallerIdentity(surface="admin_ui")


def test_meta_tools_returns_full_registry_when_unfiltered():
    """Bare call → every registered tool comes back, projected to the
    model-facing shape."""
    from evolve_admin.evo.tools import meta_tools
    result = meta_tools._handler(caller_identity=_admin_caller())
    assert result["count"] >= 13   # 12 from prior phases + meta.tools itself
    names = {t["name"] for t in result["tools"]}
    # Core tools exist
    assert "pod_state.bots" in names
    assert "action.signal.snooze" in names
    assert "meta.tools" in names   # the tool finds itself
    # Each entry has the projection fields
    for t in result["tools"]:
        assert set(t.keys()) >= {
            "name", "description", "risk_tier", "tags",
            "input_schema", "requires_validate",
        }


def test_meta_tools_filter_by_prefix():
    """``prefix='pod_state.'`` returns only the pod_state tools.
    Lets the model scope answers without a full registry dump."""
    from evolve_admin.evo.tools import meta_tools
    result = meta_tools._handler(
        prefix="pod_state.", caller_identity=_admin_caller(),
    )
    names = {t["name"] for t in result["tools"]}
    assert names, "prefix filter returned no tools"
    assert all(n.startswith("pod_state.") for n in names)
    assert result["filters_applied"] == {"prefix": "pod_state."}


def test_meta_tools_filter_by_risk_tier():
    """``risk_tier='read'`` returns only side-effect-free tools — the
    set evo can call without staging for confirmation."""
    from evolve_admin.evo.tools import meta_tools
    result = meta_tools._handler(
        risk_tier="read", caller_identity=_admin_caller(),
    )
    assert all(t["risk_tier"] == "read" for t in result["tools"])
    # Should NOT include any write_safe action tools
    names = {t["name"] for t in result["tools"]}
    assert "action.signal.snooze" not in names
    assert "action.proposal.snooze" not in names


def test_meta_tools_unknown_risk_tier_passes_through():
    """Unknown risk_tier values shouldn't crash — the operator might
    paste a typo. We silently ignore the filter and surface what we
    did via filters_applied so the model can mention it."""
    from evolve_admin.evo.tools import meta_tools
    result = meta_tools._handler(
        risk_tier="not-a-real-tier", caller_identity=_admin_caller(),
    )
    # All tools come back (filter ignored)
    assert result["count"] >= 13
    # And the response says so
    assert result["filters_applied"].get("risk_tier_ignored") == "not-a-real-tier"


def test_meta_tools_filter_by_tag():
    """``tag='signal'`` returns only tools tagged signal — chips,
    actions, etc. all use tags for categorization."""
    from evolve_admin.evo.tools import meta_tools
    result = meta_tools._handler(tag="signal", caller_identity=_admin_caller())
    for t in result["tools"]:
        assert "signal" in t["tags"], (
            f"tool {t['name']} returned but doesn't have 'signal' tag: "
            f"{t['tags']}"
        )


def test_meta_tools_filters_combine_with_AND():
    """Multiple filters AND together: prefix + risk_tier returns only
    tools matching both."""
    from evolve_admin.evo.tools import meta_tools
    result = meta_tools._handler(
        prefix="action.", risk_tier="write_safe",
        caller_identity=_admin_caller(),
    )
    for t in result["tools"]:
        assert t["name"].startswith("action.")
        assert t["risk_tier"] == "write_safe"


def test_meta_tools_requires_validate_flag_correct():
    """``requires_validate`` mirrors the construction-time invariant:
    True iff the tool has a validate function. Read tools shouldn't;
    non-read tools must."""
    from evolve_admin.evo.tools import meta_tools
    result = meta_tools._handler(caller_identity=_admin_caller())
    for t in result["tools"]:
        if t["risk_tier"] == "read":
            assert t["requires_validate"] is False, (
                f"read-tier tool {t['name']} reports requires_validate=True"
            )
        else:
            assert t["requires_validate"] is True, (
                f"{t['risk_tier']} tool {t['name']} reports requires_validate=False"
            )


def test_meta_tools_input_schema_is_present():
    """Each projected tool carries its full input_schema so the model
    can construct calls correctly without round-tripping through
    pattern-matching. Sanity: every entry has a dict-shaped schema."""
    from evolve_admin.evo.tools import meta_tools
    result = meta_tools._handler(caller_identity=_admin_caller())
    for t in result["tools"]:
        assert isinstance(t["input_schema"], dict)
        # Every tool's schema should declare `type: object` at the top
        assert t["input_schema"].get("type") == "object", (
            f"tool {t['name']} input_schema isn't 'type: object': "
            f"{t['input_schema']}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1.5d: pod_state.usage / pod_state.rollbacks / pod_state.errors
# ─────────────────────────────────────────────────────────────────────────────


def test_usage_tool_registered_as_read():
    from evolve_admin.evo.tools import lookup, RiskTier
    t = lookup("pod_state.usage")
    assert t is not None
    assert t.risk_tier == RiskTier.READ
    assert t.validate is None  # read tools never declare validate


def test_rollbacks_tool_registered_as_read():
    from evolve_admin.evo.tools import lookup, RiskTier
    t = lookup("pod_state.rollbacks")
    assert t is not None
    assert t.risk_tier == RiskTier.READ


def test_errors_tool_registered_as_read():
    from evolve_admin.evo.tools import lookup, RiskTier
    t = lookup("pod_state.errors")
    assert t is not None
    assert t.risk_tier == RiskTier.READ


def test_usage_handler_returns_zero_rollup_on_fresh_pod(tmp_path):
    """No metrics files yet → all-zero rollup. Empty-pod shape must
    still be well-formed (caller checks ok=True before reading fields).
    When no bot has a readable turn dir, the wrapper falls back to the
    legacy aggregate-based rollup and labels source=lagged_metrics."""
    from evolve_admin.evo.tools import pod_state_usage
    np = _write_network_json(tmp_path)
    result = pod_state_usage._handler(network_path=np)
    assert result["ok"] is True
    assert result["pod_total_7d"] == 0
    assert result["pod_total_28d"] == 0
    assert result["currency"] == "USD"
    # Both bots show up with their own zero rows.
    assert set(result["by_bot"].keys()) == {"evolve", "team_bot_a"}
    # New intraday + daily-array fields exist even in fallback mode so
    # downstream callers don't need to branch on shape.
    assert result["source"] == "lagged_metrics"
    assert result["pod_total_today"] == 0.0
    assert result["pod_total_yesterday"] == 0.0
    assert result["pod_daily_7d"] == []
    assert result["pod_daily_28d"] == []
    assert "as_of" in result and "today" in result


def test_usage_handler_filters_by_bot_id(tmp_path):
    from evolve_admin.evo.tools import pod_state_usage
    np = _write_network_json(tmp_path)
    result = pod_state_usage._handler(network_path=np, bot_id="team_bot_a")
    assert result["ok"] is True
    assert set(result["by_bot"].keys()) == {"team_bot_a"}


def test_usage_handler_live_jsonl_reports_today_yesterday_and_arrays(tmp_path):
    """When ``{shared_dir}/<bot>/turns/turns-<date>.jsonl`` exists, the
    handler reads from raw turn JSONL (same source as Usage Summary).
    Verifies intraday today figures, yesterday full-day figures, and
    that daily_7d / daily_28d are oldest→newest with today last."""
    import json
    from datetime import date, timedelta
    from evolve_admin.evo.tools import pod_state_usage

    np = _write_network_json(tmp_path)
    today = date.today()
    yest = today - timedelta(days=1)

    def _write_turns(bot: str, d: date, turns: list[dict]) -> None:
        td = tmp_path / bot / "turns"
        td.mkdir(parents=True, exist_ok=True)
        f = td / f"turns-{d.isoformat()}.jsonl"
        f.write_text(
            "\n".join(json.dumps({**t, "ts": f"{d.isoformat()}T12:00:00Z"}) for t in turns)
        )

    # team_bot_a: $1.23 today (1 turn), $4.50 yesterday (3 turns)
    _write_turns("team_bot_a", today, [{"cost": 1.23, "session_id": "s-team_bot_a-1"}])
    _write_turns("team_bot_a", yest, [
        {"cost": 1.50, "session_id": "s-team_bot_a-2"},
        {"cost": 1.50, "session_id": "s-team_bot_a-2"},
        {"cost": 1.50, "session_id": "s-team_bot_a-3"},
    ])
    # evolve: $0.10 today, no turns yesterday
    _write_turns("evolve", today, [{"cost": 0.10, "session_id": "s-evo-1"}])

    result = pod_state_usage._handler(network_path=np)
    assert result["ok"] is True
    assert result["source"] == "live_jsonl"
    assert result["today"] == today.isoformat()

    team_bot_a = result["by_bot"]["team_bot_a"]
    assert team_bot_a["usd_today"] == 1.23
    assert team_bot_a["usd_yesterday"] == 4.50
    assert team_bot_a["turns_today"] == 1
    assert team_bot_a["turns_yesterday"] == 3
    # daily_7d is oldest→newest, today is the last entry
    assert len(team_bot_a["daily_7d"]) == 7
    assert team_bot_a["daily_7d"][-1]["date"] == today.isoformat()
    assert team_bot_a["daily_7d"][-1]["usd"] == 1.23
    assert team_bot_a["daily_7d"][-2]["date"] == yest.isoformat()
    assert team_bot_a["daily_7d"][-2]["usd"] == 4.50
    assert len(team_bot_a["daily_28d"]) == 28

    # Pod totals add across bots
    assert result["pod_total_today"] == pytest.approx(1.33, abs=0.001)
    assert result["pod_total_yesterday"] == pytest.approx(4.50, abs=0.001)
    # Pod daily arrays present and ordered
    assert result["pod_daily_7d"][-1]["date"] == today.isoformat()
    assert result["pod_daily_7d"][-1]["usd"] == pytest.approx(1.33, abs=0.001)


def test_usage_handler_live_jsonl_filtered_to_one_bot(tmp_path):
    """bot_id filter still works on the live path."""
    import json
    from datetime import date
    from evolve_admin.evo.tools import pod_state_usage

    np = _write_network_json(tmp_path)
    today = date.today()
    td = tmp_path / "team_bot_a" / "turns"
    td.mkdir(parents=True, exist_ok=True)
    (td / f"turns-{today.isoformat()}.jsonl").write_text(
        json.dumps({"cost": 2.50, "ts": f"{today.isoformat()}T08:00:00Z"})
    )

    result = pod_state_usage._handler(network_path=np, bot_id="team_bot_a")
    assert result["ok"] is True
    assert set(result["by_bot"].keys()) == {"team_bot_a"}
    assert result["by_bot"]["team_bot_a"]["usd_today"] == 2.50
    assert result["source"] == "live_jsonl"


def test_usage_handler_unknown_bot_errors(tmp_path):
    from evolve_admin.evo.tools import pod_state_usage
    np = _write_network_json(tmp_path)
    result = pod_state_usage._handler(network_path=np, bot_id="ghost")
    assert result["ok"] is False
    assert "not registered" in result["error"]


def test_usage_handler_malformed_network_json(tmp_path):
    """A malformed network.json surfaces the read failure to the
    operator. (A *missing* network.json is handled by config.load_network's
    default-network fallback — covered by the empty-rollup test above.)"""
    from evolve_admin.evo.tools import pod_state_usage
    np = tmp_path / "network.json"
    np.write_text("{ not valid json")
    result = pod_state_usage._handler(network_path=np)
    assert result["ok"] is False
    assert "read failed" in result["error"]


def test_rollbacks_handler_empty_on_fresh_pod(tmp_path):
    from evolve_admin.evo.tools import pod_state_rollbacks
    np = _write_network_json(tmp_path)
    result = pod_state_rollbacks._handler(network_path=np)
    assert result["ok"] is True
    assert result["count"] == 0
    assert result["rollbacks"] == []


def test_rollbacks_handler_caps_limit_at_100(tmp_path):
    """Defensive: even if the model asks for limit=10_000, we clamp."""
    from evolve_admin.evo.tools import pod_state_rollbacks
    np = _write_network_json(tmp_path)
    result = pod_state_rollbacks._handler(
        network_path=np, limit=10_000,
    )
    assert result["ok"] is True
    assert result["limit"] == 100


def test_rollbacks_handler_clamps_limit_floor(tmp_path):
    from evolve_admin.evo.tools import pod_state_rollbacks
    np = _write_network_json(tmp_path)
    result = pod_state_rollbacks._handler(network_path=np, limit=-5)
    assert result["ok"] is True
    assert result["limit"] == 1


def test_rollbacks_handler_reads_persisted_records(tmp_path):
    """Write a fake rollback record into rollback_dir and verify the
    tool surfaces it. Confirms the wiring against recovery.list_rollback_history."""
    from evolve_admin.evo.tools import pod_state_rollbacks
    np = _write_network_json(tmp_path)
    # The rollback dir lives under shared_dir/recovery/rollbacks/.
    rd = tmp_path / "recovery" / "rollbacks"
    rd.mkdir(parents=True)
    (rd / "rb-1.json").write_text(json.dumps({
        "rollback_id": "rb-1",
        "bot_id": "team_bot_a",
        "target_commit": "abc123",
        "started_at": "2026-05-19T01:00:00Z",
        "finished_at": "2026-05-19T01:00:30Z",
        "ok": True,
        "message": "rolled back team_bot_a",
        "pre_rollback_config": {"big": "blob"},   # should be trimmed
        "post_rollback_config": {"big": "blob"},  # should be trimmed
    }))
    result = pod_state_rollbacks._handler(network_path=np, bot_id="team_bot_a")
    assert result["ok"] is True
    assert result["count"] == 1
    entry = result["rollbacks"][0]
    assert entry["rollback_id"] == "rb-1"
    assert entry["bot_id"] == "team_bot_a"
    # Bulky fields trimmed; flag set instead.
    assert "pre_rollback_config" not in entry
    assert "post_rollback_config" not in entry
    assert entry["has_pre_rollback_snapshot"] is True


def test_rollbacks_handler_unknown_bot(tmp_path):
    from evolve_admin.evo.tools import pod_state_rollbacks
    np = _write_network_json(tmp_path)
    result = pod_state_rollbacks._handler(network_path=np, bot_id="ghost")
    assert result["ok"] is False
    assert "not registered" in result["error"]


def test_errors_handler_returns_note_when_no_heal_status(tmp_path):
    """Fresh pod: no status/<bot>.json files. Tool must still return a
    structured response with an explanatory note per bot rather than
    erroring."""
    from evolve_admin.evo.tools import pod_state_errors
    np = _write_network_json(tmp_path)
    result = pod_state_errors._handler(network_path=np)
    assert result["ok"] is True
    assert result["count_bots"] == 2
    assert result["total_recent_errors"] == 0
    for b in result["bots"]:
        assert b["recent_errors"] == []
        assert "note" in b


def test_errors_handler_reads_heal_status(tmp_path):
    from evolve_admin.evo.tools import pod_state_errors
    np = _write_network_json(tmp_path)
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    (status_dir / "team_bot_a.json").write_text(json.dumps({
        "ts": "2026-05-19T03:00:00Z",
        "gateway_reachable": True,
        "recent_errors": [
            "[2026-05-19T02:58:00Z] ECONNRESET on outbound webhook",
            "[2026-05-19T02:59:00Z] webhook retry succeeded",
        ],
        "recent_errors_ts": [
            "2026-05-19T02:58:00Z", "2026-05-19T02:59:00Z",
        ],
        "last_error": "[2026-05-19T02:59:00Z] webhook retry succeeded",
        "last_error_ts": "2026-05-19T02:59:00Z",
    }))
    result = pod_state_errors._handler(network_path=np, bot_id="team_bot_a")
    assert result["ok"] is True
    assert result["count_bots"] == 1
    team_bot_a = result["bots"][0]
    assert team_bot_a["bot_id"] == "team_bot_a"
    assert len(team_bot_a["recent_errors"]) == 2
    assert team_bot_a["last_error_ts"] == "2026-05-19T02:59:00Z"
    assert team_bot_a["status_ts"] == "2026-05-19T03:00:00Z"


def test_errors_handler_unknown_bot(tmp_path):
    from evolve_admin.evo.tools import pod_state_errors
    np = _write_network_json(tmp_path)
    result = pod_state_errors._handler(network_path=np, bot_id="ghost")
    assert result["ok"] is False
    assert "not registered" in result["error"]


def test_errors_handler_aggregates_across_bots(tmp_path):
    """Pod-wide call: total_recent_errors is the sum over per-bot lists."""
    from evolve_admin.evo.tools import pod_state_errors
    np = _write_network_json(tmp_path)
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    (status_dir / "team_bot_a.json").write_text(json.dumps({
        "ts": "2026-05-19T03:00:00Z",
        "recent_errors": ["err-a", "err-b"],
    }))
    (status_dir / "evolve.json").write_text(json.dumps({
        "ts": "2026-05-19T03:00:00Z",
        "recent_errors": ["err-c"],
    }))
    result = pod_state_errors._handler(network_path=np)
    assert result["ok"] is True
    assert result["count_bots"] == 2
    assert result["total_recent_errors"] == 3
