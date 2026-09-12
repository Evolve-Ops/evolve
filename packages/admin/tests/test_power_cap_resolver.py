"""tests/test_power_cap_resolver.py — the Python twin of the gateway's cap read.

``power_cap.resolve_effective_power_cap`` answers "what Power cap would the
gateway enforce for this bot?" on the admin side. It is a MIRROR of
``ModelRouter._mergeRoleCaps`` + ``_roleCap`` (ModelRouter.ts), and a mirror
that drifts is worse than no mirror — it would let the CLI report a cap
reaching routing that does not.

So these cases are not hand-picked expectations. Each precedence case and each
constant is either (a) pinned against ModelRouter.ts by parsing the source, or
(b) a value measured by running the compiled router itself. The three
precedence numbers below (10 / 25 / 3) were produced by calling the real
``mergeModelCatalog`` + ``synthesizeRungsRoles`` out of packages/plugin/dist
with these exact three configs, then reproduced here.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
_MODEL_ROUTER_TS = (
    _ADMIN_DIR.parent / "plugin" / "src" / "observer" / "ModelRouter.ts"
)
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _ts_default_power_cap() -> int:
    """``DEFAULT_MODEL_CATALOG.roleCaps.power.maxPerDayPerBot``, from the TS.

    Parsed rather than restated so a product-default bump on the plugin side
    reddens this suite instead of leaving the admin resolver quietly answering
    the old number.
    """
    src = _MODEL_ROUTER_TS.read_text()
    block = src[src.index("export const DEFAULT_MODEL_CATALOG"):]
    block = block[block.index("roleCaps:"):]
    m = re.search(r"power:\s*\{\s*maxPerDayPerBot:\s*(\d+)", block)
    assert m, f"DEFAULT_MODEL_CATALOG.roleCaps.power no longer parses:\n{block[:400]}"
    return int(m.group(1))


# ── the constants, pinned across the language boundary ───────────────────────

def test_the_python_default_matches_the_shipped_typescript_default():
    from evolve_admin.power_cap import default_power_cap

    assert default_power_cap() == _ts_default_power_cap()


def test_the_default_catalog_really_ships_a_power_cap():
    """The load-bearing fact behind this whole chip.

    ``DEFAULT_MODEL_CATALOG`` is folded in as the BASE layer of the merge the
    router routes from, so this entry existing at all is what makes the merged
    ``roleCaps`` non-empty for every bot — which is what makes
    ``userTierOverride.dailyCap`` unreachable everywhere, not just on migrated
    pods. If this entry is ever removed, the legacy fold comes back to life and
    the reasoning in ``power_cap``'s docstring needs re-deriving.
    """
    from primary_bot import DEFAULT_MODEL_CATALOG

    assert DEFAULT_MODEL_CATALOG["roleCaps"]["power"]["maxPerDayPerBot"] > 0


# ── precedence, measured against the compiled router ─────────────────────────

def test_a_bot_that_defines_nothing_gets_the_code_default():
    from evolve_admin.power_cap import resolve_effective_power_cap

    assert resolve_effective_power_cap({}, {}) == _ts_default_power_cap()


def test_the_legacy_dailyCap_alone_does_not_reach_routing():
    """The finding this chip exists to close, stated as a test.

    A bot whose ONLY cap is ``userTierOverride.dailyCap: 3`` — which is exactly
    what ``models cap`` produced before this change, on a pod with no roleCaps
    anywhere — still routes on the code default. Verified against the compiled
    router: ``mergeModelCatalog({}, {userTierOverride:{dailyCap:3}})`` yields
    ``roleCaps.power.maxPerDayPerBot: 10``, so ``_mergeRoleCaps`` returns that
    explicit block and never reaches its legacy branch.
    """
    from evolve_admin.power_cap import resolve_effective_power_cap

    resolved = resolve_effective_power_cap({}, {"userTierOverride": {"dailyCap": 3}})
    assert resolved == _ts_default_power_cap()
    assert resolved != 3


def test_an_explicit_role_cap_wins_over_the_legacy_key():
    from evolve_admin.power_cap import resolve_effective_power_cap

    assert resolve_effective_power_cap({}, {
        "userTierOverride": {"dailyCap": 3},
        "roleCaps": {"power": {"maxPerDayPerBot": 25}},
    }) == 25


def test_the_pod_layer_is_read_when_the_bot_has_no_cap():
    from evolve_admin.power_cap import resolve_effective_power_cap

    assert resolve_effective_power_cap(
        {"models": {"roleCaps": {"power": {"maxPerDayPerBot": 4}}}}, {},
    ) == 4


def test_the_bot_layer_wins_over_the_pod_layer():
    from evolve_admin.power_cap import resolve_effective_power_cap

    assert resolve_effective_power_cap(
        {"models": {"roleCaps": {"power": {"maxPerDayPerBot": 4}}}},
        {"roleCaps": {"power": {"maxPerDayPerBot": 9}}},
    ) == 9


# ── sanitize: the file is bot-owned, so its numbers are untrusted ────────────

def test_an_out_of_range_cap_reads_as_the_default_not_a_clamp():
    """Mirrors ``sanitizeDailyCap``: out-of-range returns the product default,
    NOT a boundary clamp. A bot forging 1e9 gets 10, not 100."""
    from evolve_admin.power_cap import resolve_effective_power_cap

    default = _ts_default_power_cap()
    for bad in (1e9, -1, float("nan"), float("inf"), "20", True, None):
        assert resolve_effective_power_cap(
            {}, {"roleCaps": {"power": {"maxPerDayPerBot": bad}}},
        ) == default, f"{bad!r} should have read as the default"


def test_zero_is_a_real_cap_not_a_missing_one():
    """0 is the documented "role disabled" sentinel — it must survive the
    resolver, or "Power off" would silently read as "Power at 10/day"."""
    from evolve_admin.power_cap import resolve_effective_power_cap

    assert resolve_effective_power_cap(
        {}, {"roleCaps": {"power": {"maxPerDayPerBot": 0}}},
    ) == 0


def test_a_float_cap_truncates_like_the_gateway():
    from evolve_admin.power_cap import sanitize_daily_cap

    assert sanitize_daily_cap(7.9, 10) == 7


# ── the read-merge, which is what keeps a sibling cap alive ──────────────────

def test_setting_the_power_cap_keeps_every_other_role_cap():
    """``roleCaps`` is a WHOLESALE replace on the write side. A writer that
    sends only ``power`` deletes a customized bot's ``max`` cap — widening
    Fable spend as a side effect of lowering Opus spend."""
    from evolve_admin.power_cap import merge_power_cap

    merged = merge_power_cap({"max": {"maxPerDayPerBot": 2}}, 7)
    assert merged == {
        "max": {"maxPerDayPerBot": 2},
        "power": {"maxPerDayPerBot": 7},
    }


def test_the_merge_does_not_mutate_the_block_it_was_given():
    """The caller's block is config read back off disk; mutating it in place
    would leave the pre-write view of the config silently wrong."""
    from evolve_admin.power_cap import merge_power_cap

    original = {"power": {"maxPerDayPerBot": 1}}
    merge_power_cap(original, 7)
    assert original == {"power": {"maxPerDayPerBot": 1}}


def test_the_merge_keeps_sibling_fields_on_the_power_entry():
    from evolve_admin.power_cap import merge_power_cap

    merged = merge_power_cap({"power": {"maxPerDayPerBot": 1, "note": "x"}}, 7)
    assert merged["power"] == {"maxPerDayPerBot": 7, "note": "x"}
