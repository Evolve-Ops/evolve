"""tests/test_generator_runner_wiring.py — verify every production
generator has a context factory.

This is a regression guard against the original "active in registry but
silently skipped at run time" bug: a charter could be loaded by the
registry while having no entry in ``_CONTEXT_FACTORIES``, leaving it
inert. The guard here is simple — every charter dir on disk that the
registry would load must have a matching factory.

If you add a new generator: add a charter, add code in
``packages/analyzer/generators/<id>/observe.py``, add a factory in
``generator_runner.py``, and add the generator id to
``_CONTEXT_FACTORIES``. This test will fail loudly if you forget the
last step.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generator_runner import _CONTEXT_FACTORIES  # noqa: E402


_GENERATORS_DIR = _ANALYZER_DIR / "generators"


def _read_cadence(charter_path: Path) -> str:
    """Read the ``cadence:`` line from a charter without depending on PyYAML.

    Charters are simple enough that a line scan is sufficient and keeps this
    test importable without yaml installed."""
    for line in charter_path.read_text().splitlines():
        s = line.strip()
        if s.startswith("cadence:"):
            return s.split(":", 1)[1].strip()
    return ""


def _shipped_generator_ids() -> set[str]:
    """Every generator dir under packages/analyzer/generators/ that ships
    a charter.yaml is a 'shipped' generator the registry will load.

    Excludes ``per_session`` generators — those run inside each bot at
    session_end (e.g. user_profile_inferrer), not in the central arbiter,
    so they don't need a context factory in this runner.
    """
    out: set[str] = set()
    for p in _GENERATORS_DIR.iterdir():
        if not p.is_dir():
            continue
        charter = p / "charter.yaml"
        if not charter.exists():
            continue
        if _read_cadence(charter) == "per_session":
            continue
        out.add(p.name)
    return out


def test_every_shipped_generator_has_a_context_factory():
    shipped = _shipped_generator_ids()
    wired = set(_CONTEXT_FACTORIES.keys())
    missing = shipped - wired
    assert not missing, (
        f"Generators with charters but no context factory in "
        f"_CONTEXT_FACTORIES: {sorted(missing)}. Wire them in "
        f"generator_runner.py or remove the charter."
    )


def test_no_context_factories_for_nonexistent_generators():
    """Catch typos in _CONTEXT_FACTORIES — every entry must correspond to
    a real charter on disk."""
    shipped = _shipped_generator_ids()
    wired = set(_CONTEXT_FACTORIES.keys())
    extras = wired - shipped
    assert not extras, (
        f"_CONTEXT_FACTORIES entries with no matching charter: "
        f"{sorted(extras)}. Did you delete a generator without removing "
        f"its factory?"
    )


@pytest.mark.parametrize("gen_id", sorted(_shipped_generator_ids()))
def test_factory_builds_a_context_without_raising(tmp_path, gen_id):
    """For each wired generator, the factory must build (or refuse to
    build with a clean None) for a sensible bot_id and shared_dir."""
    from datetime import datetime, timezone

    factory_entry = _CONTEXT_FACTORIES.get(gen_id)
    assert factory_entry is not None, f"{gen_id} has no factory"
    factory, per_bot = factory_entry

    bot_id = "team_bot_a" if per_bot else None
    network_config = {"members": ["team_bot_a"]}
    now = datetime.now(timezone.utc)

    # Should not raise. May return None if the factory's contract says
    # "this generator is per-bot but bot_id was None" or vice versa, but
    # for the canonical case (per_bot+bot_id, or pod_wide+None) we must
    # get back a usable context.
    ctx = factory(tmp_path, network_config, bot_id, {}, now)
    assert ctx is not None, (
        f"{gen_id} factory returned None for canonical inputs "
        f"(bot_id={bot_id!r}, per_bot={per_bot})"
    )
