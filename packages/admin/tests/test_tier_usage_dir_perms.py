"""The role-cap ledger's directory contract — {sharedDir}/cost/tier-usage/.

Found 2026-09-04: the tree was owned by a non-``evolve`` user at 0755, so
``ModelRouter._appendTierUsageRecord`` (running as each BOT user) got EACCES
on every bot but the pod's own ``evolve`` bot. The append is no-throw, and its
warn called a ``logger`` property that never existed, so the per-role daily
caps silently seeded 0 on every gateway restart and bounded nothing.

Same contract as ``proposals/`` and ``alerts/``: mode 1777 so ownership does
not decide write access, plus an explicit owner check so a bot winning the
mkdir race surfaces as drift instead of hiding.
"""

from __future__ import annotations

from pathlib import Path

from evolve_admin.tier_usage_dir_perms import (
    TIER_USAGE_DIR_MODE,
    TIER_USAGE_DIRS,
    check_tier_usage_dir,
)


class TestTierUsageDirPerms:
    def test_mode_is_sticky_world_writable(self):
        """1777, not 0755 — every bot daemon appends here as its own user."""
        assert TIER_USAGE_DIR_MODE == 0o1777

    def test_covers_both_levels_of_the_tree(self):
        """A bot creating tier-usage/{botId}/ needs write on the parent chain."""
        assert TIER_USAGE_DIRS == ("cost", "cost/tier-usage")

    def test_reports_an_absent_tree_as_drift_with_a_fix(self, tmp_path: Path):
        """create=True does not mkdir at CHECK time — it attaches the repair, so
        `ensure_pod_perms(check_only=True)` stays read-only and the hourly drift
        monitor can report without mutating the pod."""
        checks = check_tier_usage_dir(tmp_path)
        modes = [c for c in checks if c.category == "dir-mode"]
        assert not (tmp_path / "cost").exists()
        assert all(not c.ok and c.detail == "missing" for c in modes)
        assert all(c.apply is not None for c in modes)

    def test_applying_the_fix_creates_the_tree_at_1777(self, tmp_path: Path):
        """A pod with no capped-role transition yet still gets the contract,
        rather than leaving the mkdir to whichever bot races for it first."""
        for check in check_tier_usage_dir(tmp_path):
            if check.category == "dir-mode" and check.apply is not None:
                assert check.apply() is True
        leaf = tmp_path / "cost" / "tier-usage"
        assert leaf.is_dir()
        assert (leaf.stat().st_mode & 0o7777) == TIER_USAGE_DIR_MODE
        assert ((tmp_path / "cost").stat().st_mode & 0o7777) == TIER_USAGE_DIR_MODE

    def test_pairs_a_mode_check_with_an_owner_check_per_level(self, tmp_path: Path):
        checks = check_tier_usage_dir(tmp_path)
        modes = [c for c in checks if c.category == "dir-mode"]
        owners = [c for c in checks if c.category == "dir-owner"]
        assert len(modes) == len(TIER_USAGE_DIRS)
        assert len(owners) == len(TIER_USAGE_DIRS)
        targets = {Path(c.target).name for c in modes}
        assert targets == {"cost", "tier-usage"}

    def test_flags_a_too_narrow_mode_as_drift(self, tmp_path: Path):
        """The exact shape found on the reference pod: 0755, so only the
        dir-owning user could append."""
        (tmp_path / "cost" / "tier-usage").mkdir(parents=True)
        (tmp_path / "cost" / "tier-usage").chmod(0o755)
        checks = check_tier_usage_dir(tmp_path)
        leaf = [
            c for c in checks
            if c.category == "dir-mode" and Path(c.target).name == "tier-usage"
        ]
        assert len(leaf) == 1
        assert not leaf[0].ok

    def test_is_idempotent_on_a_correct_tree(self, tmp_path: Path):
        for check in check_tier_usage_dir(tmp_path):
            if check.category == "dir-mode" and check.apply is not None:
                check.apply()
        checks = check_tier_usage_dir(tmp_path)  # second pass sees no mode drift
        assert all(c.ok for c in checks if c.category == "dir-mode"), [
            (c.target, c.detail) for c in checks if not c.ok
        ]
        assert all(c.apply is None for c in checks if c.category == "dir-mode")
