"""tests/test_resolve_cross_vendor.py — the J2 cross-vendor judge derivation.

Design: internal/design-judge-role-collapse-2026-08-21.md §5.2 (phase J2).

``resolve_cross_vendor(against_role, *, catalog, credentialed)`` derives the
judge model at the call site: the first credentialed model in ``against_role``'s
resolved rung whose provider differs from the resolved model's provider — or
``None`` when no such model exists. ``None`` is meaningful (a single-provider
pod gets NO cross-vendor judge; callers decide what that means), so the tests
pin both directions:

  - multi-provider chain → the first credentialed different-provider model in
    the rung's ``models[]`` order (which IS the operator's provider_order rank:
    easy-setup sorts the chain at write time — see
    ``_reorder_models_by_preference``);
  - single-provider pod → ``None``;
  - ``against_role`` parameterization (open question 1 — decided: take the
    argument) including the degradation-aware walk;
  - shared-fixture parity with ModelRouter.resolveCrossVendor lives in
    ``test_model_availability_parity.py``.

Provider names in this file are fixture DATA (fake providers ``pa``/``pb``/
``pc``), not literals in logic.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from primary_bot import resolve_cross_vendor  # noqa: E402


def _catalog() -> dict:
    """A three-provider catalog with distinct chains per rung. ``models[]``
    order stands in for the operator's provider_order rank (pa > pb > pc)."""
    return {
        "rungs": [
            {"id": "low-rung", "models": ["pb/small-b", "pa/small-a", "pc/small-c"]},
            {"id": "mid-rung", "models": ["pa/mid-a", "pb/mid-b", "pc/mid-c"]},
            {"id": "top-rung", "models": ["pa/top-a"]},
        ],
        "roles": {
            "fast": "low-rung",
            "standard": "mid-rung",
            "power": "top-rung",
            "max": "top-rung",
        },
    }


def test_multi_provider_picks_first_credentialed_different_provider():
    # standard resolves pa/mid-a; the walk skips pa, lands on pb (rank order).
    got = resolve_cross_vendor(
        catalog=_catalog(), credentialed={"pa", "pb", "pc"}
    )
    assert got == "pb/mid-b"


def test_uncredentialed_provider_is_skipped_in_rank_order():
    # pb holds no key: the walk must pass over pb/mid-b and land on pc.
    got = resolve_cross_vendor(
        catalog=_catalog(), credentialed={"pa", "pc"}
    )
    assert got == "pc/mid-c"


def test_single_provider_pod_returns_none():
    # Only pa credentialed → no different-provider model exists. None is the
    # meaningful answer, not a failure (design §5.2).
    got = resolve_cross_vendor(catalog=_catalog(), credentialed={"pa"})
    assert got is None


def test_against_role_parameterization():
    # against fast: fast resolves pb/small-b (first in ITS chain), so the
    # derivation diffs against pb and returns the pa model — a different
    # answer than against standard, from the same catalog.
    got = resolve_cross_vendor(
        "fast", catalog=_catalog(), credentialed={"pa", "pb", "pc"}
    )
    assert got == "pa/small-a"


def test_walk_covers_the_rung_resolution_landed_in():
    # max's rung is pa-only; with pa uncredentialed, max degrades to power
    # then standard (same top-rung) then resolves in mid-rung. The walk must
    # cover THAT rung — the chain that actually produces the judged work.
    got = resolve_cross_vendor(
        "max", catalog=_catalog(), credentialed={"pb", "pc"}
    )
    # max degrades into mid-rung, resolving pb/mid-b → cross-vendor is pc.
    assert got == "pc/mid-c"


def test_unresolvable_against_role_returns_none():
    # Nothing credentialed: against_role cannot resolve, so there is nothing
    # to diff against.
    got = resolve_cross_vendor(catalog=_catalog(), credentialed=set())
    assert got is None


def test_none_credentialed_fails_open():
    # Credential state unknown (presentation readers): the walk treats every
    # provider as available, mirroring _resolve_judge_availability.
    got = resolve_cross_vendor(catalog=_catalog(), credentialed=None)
    assert got == "pb/mid-b"


def test_empty_catalog_returns_none():
    assert resolve_cross_vendor(catalog={}, credentialed={"pa"}) is None
