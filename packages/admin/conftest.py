"""conftest.py — CI quarantine hook for packages/admin.

Reads ``ci-quarantine.txt`` at the repo root and deselects every listed
test during collection.  This lets the blocking CI gate run the full suite
while skipping the known-baseline failures — so only *new* breakage fails
the build.

Format of ci-quarantine.txt:
  <package>/<pytest-node-id>  # reason comment
Lines starting with '#' and blank lines are ignored.
Package prefix "admin/" is stripped; the remainder is the node-id for this
package.  Lines with other package prefixes are skipped silently.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


def _load_quarantine() -> frozenset[str]:
    quarantine_file = Path(__file__).resolve().parents[2] / "ci-quarantine.txt"
    if not quarantine_file.exists():
        return frozenset()
    ids: set[str] = set()
    for raw in quarantine_file.read_text().splitlines():
        line = raw.split("#")[0].strip()
        if not line:
            continue
        if not line.startswith("admin/"):
            continue
        # strip the "admin/" prefix to get the pytest node-id for this package
        ids.add(line[len("admin/"):])
    return frozenset(ids)


_QUARANTINED = _load_quarantine()


def _shard() -> tuple[int, int] | None:
    """Return (shard_id, num_shards) from EVOLVE_SHARD_ID / EVOLVE_NUM_SHARDS,
    or None when sharding is off (num_shards <= 1 / unset / malformed)."""
    try:
        num = int(os.environ.get("EVOLVE_NUM_SHARDS", "1"))
        sid = int(os.environ.get("EVOLVE_SHARD_ID", "0"))
    except ValueError:
        return None
    return (sid, num) if num > 1 and 0 <= sid < num else None


# Per-file weights = measured pytest *call* time in seconds (capped suite,
# 2026-06-10). Call time (not call+setup) because per-test setup over-counts
# shared module/session fixtures — summing it inflates total ~1.75× over real
# wall. These are the heavy files shard balancing must spread out; without them
# crc32 hashing clustered the slow ones and left one shard ~80s behind. Files
# not listed are weighted by test-count × _AVG_S (the tail's per-test cost is
# roughly uniform, so count is a fine proxy). Refreshing is optional — stale
# numbers only nudge balance, never correctness. test_install.py alone is ~19%
# of all admin call time and sets the per-shard floor (~92s).
_FILE_WEIGHT_S = {
    "tests/test_install.py": 92.0,
    "tests/test_tile_metrics.py": 30.5,
    "tests/test_recovery.py": 26.8,
    "tests/test_cost_opt_tiles_endpoint.py": 20.0,
    "tests/test_home_chat.py": 17.9,
    "tests/test_api_breakers.py": 16.8,
    "tests/test_customizations_endpoints.py": 15.5,
    "tests/test_inbox_policy_routes.py": 13.9,
    "tests/test_routes_identity.py": 12.1,
    "tests/test_evo_tools.py": 11.6,
    "tests/test_keys_api_probes.py": 10.7,
    "tests/test_signals_refresh_endpoint.py": 10.0,
    "tests/test_engine_tier_override_endpoint.py": 9.7,
    "tests/test_arbiter_endpoints.py": 9.5,
    "tests/test_repo_puller.py": 9.2,
    "tests/test_admin_ui_chat_slash_rejection.py": 9.1,
    "tests/test_evo_backup_tools.py": 7.6,
    "tests/test_routes_bot_users.py": 5.6,
    "tests/test_inbox_repos_routes.py": 5.5,
    "tests/test_scanner_backfill.py": 4.8,
    "tests/test_better_engine_tier1.py": 4.4,
    "tests/test_unix_socket_server.py": 4.2,
    "tests/test_bot_rename.py": 4.2,
    "tests/test_cli_keystore_get.py": 4.2,
}
_AVG_S = 0.010  # call seconds/test for files not in _FILE_WEIGHT_S (tail ≈ uniform):
# the unlisted files total ~119s of call time over ~11.9k tests. With this the
# listed weights + tail are both in real call-seconds, so greedy balances by
# actual time. test_install.py (~92s) is ~20% of the suite, so it nearly fills
# one shard alone — that file is the 4-shard floor (~92s + collection).


def pytest_collection_modifyitems(config, items):
    # 1) Quarantine: mark known-baseline failures as skipped.
    if _QUARANTINED:
        for item in items:
            # item.nodeid is relative to the rootdir (packages/admin)
            if item.nodeid in _QUARANTINED:
                item.add_marker("skip")

    # 2) Sharding: when EVOLVE_NUM_SHARDS > 1, keep only this shard's slice so a
    #    matrix of N CI jobs partitions the ~12.6k-test suite N ways.
    #
    #    Whole files (not individual node ids) are assigned to shards, so every
    #    test in a module lands together — the same locality the old
    #    `--dist loadscope` gave xdist, which protects order-dependent tests
    #    (shared module state, a file one test writes that another reads).
    #
    #    Files are greedily bin-packed by weight (heaviest first → currently
    #    lightest shard) so wall-clock is balanced by *time*, not file count.
    #    Plain crc32 hashing clustered the slow git/subprocess files, leaving one
    #    shard ~80s behind. The assignment is deterministic across shard
    #    processes — every shard sees the same items + weights + filename
    #    tie-break, so they agree without coordination (no PYTHONHASHSEED salt
    #    issues). Quarantined items still get sharded; they stay skipped in
    #    whichever shard owns their file.
    sh = _shard()
    if sh is not None:
        sid, num = sh

        by_file: dict[str, list] = {}
        for item in items:
            by_file.setdefault(item.nodeid.split("::", 1)[0], []).append(item)

        def _weight(test_file: str) -> float:
            return _FILE_WEIGHT_S.get(test_file, len(by_file[test_file]) * _AVG_S)

        loads = [0.0] * num
        owner: dict[str, int] = {}
        for test_file in sorted(by_file, key=lambda f: (-_weight(f), f)):
            target = min(range(num), key=lambda i: (loads[i], i))
            owner[test_file] = target
            loads[target] += _weight(test_file)

        kept, dropped = [], []
        for test_file, file_items in by_file.items():
            (kept if owner[test_file] == sid else dropped).extend(file_items)
        if dropped:
            config.hook.pytest_deselected(items=dropped)
        items[:] = kept


# ── Test-speed guard: collapse production poll-loop sleeps ──────────────────
# Several admin code paths (deploy.install_oc_plugin, its version-match
# preflight + gateway-kickstart wait, service/mcp_service startup polls, …)
# sleep inside ``for _ in range(N): time.sleep(s)`` loops while waiting on a
# gateway/endpoint that never answers under test. Left real, those loops cost
# ~30s per test — the 11 install-preflight tests in test_install.py alone burned
# ~5.5 min serially (two unstubbed 30s poll loops in install_oc_plugin).
#
# We CAP (not zero) the sleep so a test that spins up a real thread/server still
# gets a scheduler yield, while 30s poll loops collapse to milliseconds. Opt out
# with ``@pytest.mark.real_sleep`` on the rare test that asserts real timing.
#
# Note: only ``time.sleep`` (the module attribute) is patched; the handful of
# daemons that do ``from time import sleep`` bind the name locally and are
# unaffected — those run inside long-lived services, not the hot test paths.
_REAL_SLEEP = time.sleep
_MAX_TEST_SLEEP = 0.01  # seconds


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_sleep: run the test with real time.sleep (skip the sleep cap)",
    )


@pytest.fixture(autouse=True)
def _cap_test_sleeps(request, monkeypatch):
    if request.node.get_closest_marker("real_sleep"):
        return
    monkeypatch.setattr(time, "sleep", lambda s=0: _REAL_SLEEP(min(s, _MAX_TEST_SLEEP)))
