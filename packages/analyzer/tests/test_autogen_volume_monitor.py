"""Tests for autogen_volume_monitor — the runtime auto-gen disk-volume backstop.

Pinned behavior:
  - A directory over its file OR byte budget fires one autogen_volume_exceeded
    Signal naming the dir, current-vs-budget, and top sub-path contributors.
  - Under budget → no signal (sweep-resolves any existing).
  - Budget precedence: root default < named code default < operator override.
  - The nested Linux deploy checkout + hidden dirs are pruned from shared
    surfaces (measuring the git tree would false-fire).
  - Per-bot workspace/evolve surfaces are bot-scoped; the sweep is restricted to
    scanned scopes so an unreadable bot keeps its still-firing Signals.
  - Full store round-trip: over-budget → fires; on drain → sweep-resolves.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import autogen_volume_monitor as avm  # noqa: E402
import platform_profile  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────


def _populate(dir: Path, *, n_files: int, size: int = 1) -> None:
    """Create ``n_files`` files of ``size`` bytes under ``dir``."""
    dir.mkdir(parents=True, exist_ok=True)
    blob = b"x" * size
    for i in range(n_files):
        (dir / f"f{i}.json").write_bytes(blob)


def _pod_surface(path: Path) -> avm.Surface:
    return avm.Surface(name=path.name, path=path, scope="pod")


# ── measurement ────────────────────────────────────────────────────────────────


def test_measure_surface_counts_recursively_and_attributes_children(tmp_path: Path):
    surface_dir = tmp_path / "audit_outbox"
    _populate(surface_dir / "_ingested" / "2026-06-28", n_files=10, size=100)
    _populate(surface_dir / "live", n_files=2, size=50)
    (surface_dir / "loose.json").write_bytes(b"z" * 7)

    usage = avm.measure_surface(_pod_surface(surface_dir))
    assert usage is not None
    assert usage.file_count == 13  # 10 + 2 + 1 loose
    assert usage.total_bytes == 10 * 100 + 2 * 50 + 7
    # _ingested is the dominant contributor.
    top = usage.top_contributors()
    assert top[0]["name"] == "_ingested"
    assert top[0]["file_count"] == 10
    # Loose top-level files are attributed to a synthetic bucket.
    assert "(files at top level)" in usage.children
    assert usage.children["(files at top level)"] == (1, 7)


def test_measure_surface_missing_dir_is_none(tmp_path: Path):
    assert avm.measure_surface(_pod_surface(tmp_path / "nope")) is None


# ── budget breach / signal construction ────────────────────────────────────────


def test_over_file_budget_fires(tmp_path: Path):
    d = tmp_path / "infra_audit_outbox"
    _populate(d / "_ingested", n_files=12, size=1)
    usage = avm.measure_surface(_pod_surface(d))
    budget = avm.Budget(max_files=5, max_bytes=None, source="test")
    spec = avm.build_signal_spec(usage, budget)
    assert spec is not None
    assert spec["type"] == avm.SIGNAL_TYPE
    assert spec["producer"] == avm.PRODUCER
    assert spec["severity"] == "warn"
    assert spec["scope"] == "pod"
    assert spec["details"]["breach_axes"] == ["files"]
    assert spec["details"]["file_count"] == 12
    # Top contributor named in the body for attribution.
    assert "_ingested" in spec["body"]
    assert "infra_audit_outbox" in spec["title"]


def test_over_byte_budget_fires(tmp_path: Path):
    d = tmp_path / "blob_dir"
    _populate(d, n_files=3, size=1000)
    usage = avm.measure_surface(_pod_surface(d))
    budget = avm.Budget(max_files=None, max_bytes=100, source="test")
    spec = avm.build_signal_spec(usage, budget)
    assert spec is not None
    assert spec["details"]["breach_axes"] == ["bytes"]
    assert spec["details"]["magnitude"] == 1


def test_both_axes_breach_is_magnitude_2(tmp_path: Path):
    d = tmp_path / "big"
    _populate(d, n_files=10, size=1000)
    usage = avm.measure_surface(_pod_surface(d))
    budget = avm.Budget(max_files=5, max_bytes=100, source="test")
    spec = avm.build_signal_spec(usage, budget)
    assert spec["details"]["breach_axes"] == ["files", "bytes"]
    assert spec["details"]["magnitude"] == 2


def test_within_budget_is_none(tmp_path: Path):
    d = tmp_path / "ok"
    _populate(d, n_files=3, size=10)
    usage = avm.measure_surface(_pod_surface(d))
    budget = avm.Budget(max_files=100, max_bytes=10_000, source="test")
    assert avm.build_signal_spec(usage, budget) is None


def test_unbounded_axis_never_breaches(tmp_path: Path):
    d = tmp_path / "huge"
    _populate(d, n_files=100, size=1)
    usage = avm.measure_surface(_pod_surface(d))
    budget = avm.Budget(max_files=None, max_bytes=None, source="test")
    assert avm.build_signal_spec(usage, budget) is None


def test_bot_scoped_signal_carries_bot_id(tmp_path: Path):
    d = tmp_path / "audit_outbox"
    _populate(d, n_files=10, size=1)
    surface = avm.Surface(name="audit_outbox", path=d, scope="bot", bot_id="atlas")
    usage = avm.measure_surface(surface)
    spec = avm.build_signal_spec(usage, avm.Budget(max_files=3, max_bytes=None))
    assert spec["scope"] == "bot"
    assert spec["bot_id"] == "atlas"
    assert "atlas" in spec["title"]
    assert spec["signature"] == avm.make_signature(
        avm.PRODUCER, avm.SIGNAL_TYPE, "workspace:atlas:audit_outbox"
    )


# ── budget resolution ──────────────────────────────────────────────────────────


def test_named_default_tightens_audit_surfaces():
    s = avm.Surface(name="audit_outbox", path=Path("/x"), scope="bot")
    b = avm.resolve_budget(s, {}, {})
    assert b.max_files == avm.NAMED_BUDGETS["audit_outbox"].max_files
    assert b.source == "named:audit_outbox"


def test_root_default_applies_to_unnamed_surface():
    pod = avm.resolve_budget(avm.Surface(name="proposals", path=Path("/x"), scope="pod"), {}, {})
    assert pod.max_files == avm.SHARED_DEFAULT.max_files
    work = avm.resolve_budget(avm.Surface(name="manifests", path=Path("/x"), scope="bot"), {}, {})
    assert work.max_files == avm.WORKSPACE_DEFAULT.max_files


def test_operator_override_wins_over_named_default():
    s = avm.Surface(name="audit_outbox", path=Path("/x"), scope="bot")
    cfg = {"budgets": {"audit_outbox": {"max_files": 99, "max_bytes": None}}}
    b = avm.resolve_budget(s, cfg, {})
    assert b.max_files == 99
    assert b.max_bytes is None  # explicit null = unbounded
    assert b.source == "override:audit_outbox"


def test_operator_root_default_override():
    s = avm.Surface(name="something_new", path=Path("/x"), scope="pod")
    cfg = {"shared_default": {"max_files": 7}}
    b = avm.resolve_budget(s, cfg, {})
    assert b.max_files == 7
    # max_bytes absent from override → inherits the code default.
    assert b.max_bytes == avm.SHARED_DEFAULT.max_bytes


# ── surface enumeration ────────────────────────────────────────────────────────


def test_enumerate_shared_prunes_nested_checkout_and_hidden(tmp_path: Path, monkeypatch):
    # Simulate the Linux nested layout: deploy checkout is a CHILD of shared_dir.
    shared = tmp_path / "evolve"
    (shared / "proposals").mkdir(parents=True)
    (shared / "repo" / "packages").mkdir(parents=True)  # the nested git checkout
    (shared / ".lock_dir").mkdir()

    # Point the profile's deploy_checkout_default at our fake nested checkout.
    fake = platform_profile.PlatformProfile(
        **{**platform_profile.LINUX.__dict__,
           "shared_dir_default": str(shared),
           "deploy_checkout_default": str(shared / "repo")}
    )
    monkeypatch.setattr(avm, "get_profile", lambda *_a, **_k: fake)

    surfaces = avm.enumerate_shared_surfaces(shared)
    names = {s.name for s in surfaces}
    assert "proposals" in names
    assert "repo" not in names          # nested deploy checkout pruned
    assert ".lock_dir" not in names      # hidden pruned


def test_enumerate_bot_surfaces_uses_workspace_evolve(tmp_path: Path, monkeypatch):
    home = tmp_path / "atlas"
    evolve = home / ".openclaw" / "workspace" / "evolve"
    (evolve / "audit_outbox").mkdir(parents=True)
    (evolve / "manifests").mkdir()
    monkeypatch.setattr(avm, "bot_home", lambda bot_id, config=None: home)

    surfaces = avm.enumerate_bot_surfaces({"members": ["atlas"]})
    assert {s.name for s in surfaces} == {"audit_outbox", "manifests"}
    assert all(s.scope == "bot" and s.bot_id == "atlas" for s in surfaces)


# ── full store round-trip ──────────────────────────────────────────────────────


def test_run_fires_then_sweep_resolves_on_drain(tmp_path: Path, monkeypatch):
    from signals import store as signals_store  # lazy: shard-pollution trap

    shared = tmp_path / "shared"
    shared.mkdir()
    leak = shared / "infra_audit_outbox"
    _populate(leak / "_ingested", n_files=12, size=1)  # tight budget via override below
    config = {
        "members": [],
        "footprint": {"autogen_volume": {"budgets": {"infra_audit_outbox": {"max_files": 5}}}},
    }

    kept, n_fired, n_resolved = avm.run(config, shared)
    assert n_fired == 1
    active = [s for s in signals_store.iter_active(shared, producer=avm.PRODUCER)]
    assert len(active) == 1
    assert active[0].type == avm.SIGNAL_TYPE
    assert active[0].severity == "warn"

    # Drain the leak: now under budget → next run sweep-resolves it.
    for f in (leak / "_ingested").glob("*.json"):
        f.unlink()
    kept2, n_fired2, n_resolved2 = avm.run(config, shared)
    assert n_fired2 == 0
    assert n_resolved2 >= 1
    assert [s for s in signals_store.iter_active(shared, producer=avm.PRODUCER)] == []


def test_run_dry_run_writes_nothing(tmp_path: Path):
    from signals import store as signals_store

    shared = tmp_path / "shared"
    leak = shared / "infra_audit_outbox"
    _populate(leak, n_files=10, size=1)
    config = {"members": [], "footprint": {"autogen_volume": {"budgets": {"infra_audit_outbox": {"max_files": 2}}}}}

    kept, n_fired, n_resolved = avm.run(config, shared, dry_run=True)
    assert n_fired == 1
    assert n_resolved == 0
    # Nothing persisted.
    assert [s for s in signals_store.iter_active(shared, producer=avm.PRODUCER)] == []


def test_exclude_list_skips_surface(tmp_path: Path):
    shared = tmp_path / "shared"
    d = shared / "noisy"
    _populate(d, n_files=50, size=1)
    config = {
        "members": [],
        "footprint": {"autogen_volume": {
            "shared_default": {"max_files": 1},
            "exclude": ["noisy"],
        }},
    }
    specs, scanned = avm.collect(config, shared)
    assert specs == []
    assert None in scanned  # pod was scanned


# ── registry wiring (the CI-reddening requirements) ────────────────────────────


def test_registered_in_protection_registry():
    from signals import protection_registry as pr
    entry = pr.get(avm.PRODUCER)
    assert entry is not None
    assert "packages/analyzer/autogen_volume_monitor.py" in entry.emits_from
    assert entry.is_none and entry.justification  # NONE requires a justification


def test_registered_in_producer_liveness_registry():
    import monitor_coverage as mc
    label = f"ai.openclaw.evolve.{Path(avm.__file__).stem}"
    assert label in mc.SIGNAL_PRODUCER_MONITORS
