"""Actionable vs informational proposal classification (Effectiveness-Layer §11).

Observation/FYI kinds ("look into it") must be distinguishable from automated,
verifiable bot actions so the actionable queue stays free of them.
"""

from __future__ import annotations

import importlib

apply_mod = importlib.import_module("arbiter.apply")


def test_observation_kinds_are_informational():
    for kind in ("Investigation", "VetoAnnotation", "WorkflowInstruction", "AddSignalCollection"):
        assert apply_mod.is_informational_kind(kind) is True, kind


def test_automated_action_kinds_are_not_informational():
    for kind in (
        "ConfigPatch", "TierAdjustment", "InstallApp", "AgentsAppend", "BuildApp",
        "ManifestUpdate", "MemoryCurate", "DeprecateApp", "SoulEdit", "RetireOrphan",
    ):
        assert apply_mod.is_informational_kind(kind) is False, kind


def test_unknown_or_empty_kind_is_not_informational():
    assert apply_mod.is_informational_kind("") is False
    assert apply_mod.is_informational_kind("SomethingBrandNew") is False


def test_informational_set_is_the_single_source_of_truth():
    # the helper and the exported set agree
    assert all(apply_mod.is_informational_kind(k) for k in apply_mod.INFORMATIONAL_KINDS)
