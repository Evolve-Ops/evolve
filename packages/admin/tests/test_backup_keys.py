"""Unit tests for ``evolve_admin.backup_keys`` — the unified backup-key writer.

Pins the four-clause invariant from
``internal/spec-backup-key-distribution-unification-2026-06-08.md`` against
regression. The grep test at ``test_no_per_bot_backup_writer.py`` is the
companion sentinel that fails CI if a future commit reintroduces the
per-bot writer shape outside this module.

Tests use the module's env-var overrides (``EVOLVE_BACKUP_KEYS_*``) to
redirect canonical-path lookups at a tmpdir, so no sudo or real
``/Users/evolve/.ssh`` is required.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from evolve_admin import backup_keys


CURRENT_USER = subprocess.run(
    ["id", "-un"], capture_output=True, text=True, check=False,
).stdout.strip() or "nobody"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """Redirect every canonical-path lookup at a tmpdir.

    Yields the tmpdir root. The shared-key source lives under
    ``root/evolve-ssh/``; bot homes live under ``root/Users/``.
    """
    ssh_root = tmp_path / "evolve-ssh"
    ssh_root.mkdir()
    users_root = tmp_path / "Users"
    users_root.mkdir()

    monkeypatch.setenv(
        backup_keys._ENV_PRIV, str(ssh_root / "evolve-backup-shared"),
    )
    monkeypatch.setenv(
        backup_keys._ENV_PUB, str(ssh_root / "evolve-backup-shared.pub"),
    )
    monkeypatch.setenv(backup_keys._ENV_USERS_ROOT, str(users_root))
    monkeypatch.setenv(backup_keys._ENV_NO_SUDO, "1")
    yield tmp_path


def _generate_source(env_root: Path) -> tuple[bytes, str]:
    """Place a fake canonical source pair under the env's ssh root."""
    ssh_root = env_root / "evolve-ssh"
    priv = ssh_root / "evolve-backup-shared"
    pub = ssh_root / "evolve-backup-shared.pub"
    priv.write_bytes(b"-----BEGIN OPENSSH PRIVATE KEY-----\nFAKEPRIV\n-----END-----\n")
    pub_text = "ssh-ed25519 AAAAFAKEPUBLICBLOB evolve-backup-shared"
    pub.write_text(pub_text + "\n")
    return priv.read_bytes(), pub_text


def _make_bot_home(env_root: Path, bot_user: str) -> Path:
    """Pre-create a bot user's home dir under the env's users root."""
    home = env_root / "Users" / bot_user
    home.mkdir(parents=True, exist_ok=True)
    return home


def _shared_dir(env_root: Path) -> Path:
    d = env_root / "shared"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _network(env_root: Path, *, bots: dict, key_mode: str | None = None) -> dict:
    net = {
        "sharedDir": str(_shared_dir(env_root)),
        "bots": bots,
    }
    if key_mode:
        net["backup"] = {"key_mode": key_mode}
    return net


# ── canonical source ─────────────────────────────────────────────────────


def test_ensure_shared_source_generated_creates_pair(env):
    assert not backup_keys.canonical_source_exists()
    ok, generated, err = backup_keys.ensure_shared_source_generated()
    assert ok and generated and err is None
    assert backup_keys.canonical_source_exists()
    pub = backup_keys.read_canonical_pubkey()
    assert pub and pub.startswith("ssh-ed25519 ")


def test_ensure_shared_source_generated_is_idempotent(env):
    _generate_source(env)
    ok, generated, err = backup_keys.ensure_shared_source_generated()
    assert ok and not generated and err is None


# ── ensure_bot_in_sync — clauses 1-3 ─────────────────────────────────────


def test_ensure_bot_in_sync_distributes_to_empty_home(env):
    """First call on a clean bot home writes priv + pub + mirror."""
    src_priv, src_pub = _generate_source(env)
    _make_bot_home(env, CURRENT_USER)
    net = _network(env, bots={"bot-a": {"user": CURRENT_USER}})

    r = backup_keys.ensure_bot_in_sync("bot-a", CURRENT_USER, net)

    assert r.status == backup_keys.BotSyncStatus.DISTRIBUTED, r.error
    bot_priv = env / "Users" / CURRENT_USER / ".ssh" / "evolve-backup-bot-a"
    bot_pub = bot_priv.with_suffix(".pub")
    assert bot_priv.read_bytes() == src_priv
    assert bot_pub.read_text().strip() == src_pub
    mirror = Path(net["sharedDir"]) / "pubkeys" / "bot-a.pub"
    assert mirror.read_text().strip() == src_pub


def test_ensure_bot_in_sync_aligned_is_noop(env):
    """Pre-aligned state returns ALIGNED without rewriting."""
    _generate_source(env)
    _make_bot_home(env, CURRENT_USER)
    net = _network(env, bots={"bot-a": {"user": CURRENT_USER}})

    backup_keys.ensure_bot_in_sync("bot-a", CURRENT_USER, net)
    bot_priv = env / "Users" / CURRENT_USER / ".ssh" / "evolve-backup-bot-a"
    mtime_before = bot_priv.stat().st_mtime_ns

    r = backup_keys.ensure_bot_in_sync("bot-a", CURRENT_USER, net)
    assert r.status == backup_keys.BotSyncStatus.ALIGNED
    assert bot_priv.stat().st_mtime_ns == mtime_before  # not rewritten


def test_ensure_bot_in_sync_fixes_priv_drift(env):
    """Drift in clause 1 (private key) → DISTRIBUTED."""
    src_priv, _ = _generate_source(env)
    _make_bot_home(env, CURRENT_USER)
    net = _network(env, bots={"bot-a": {"user": CURRENT_USER}})
    backup_keys.ensure_bot_in_sync("bot-a", CURRENT_USER, net)
    bot_priv = env / "Users" / CURRENT_USER / ".ssh" / "evolve-backup-bot-a"
    bot_priv.write_bytes(b"DRIFTED PRIVATE KEY")

    r = backup_keys.ensure_bot_in_sync("bot-a", CURRENT_USER, net)
    assert r.status == backup_keys.BotSyncStatus.DISTRIBUTED, r.error
    assert bot_priv.read_bytes() == src_priv


def test_ensure_bot_in_sync_fixes_pub_drift(env):
    """Drift in clause 2 (pub) → DISTRIBUTED."""
    _, src_pub = _generate_source(env)
    _make_bot_home(env, CURRENT_USER)
    net = _network(env, bots={"bot-a": {"user": CURRENT_USER}})
    backup_keys.ensure_bot_in_sync("bot-a", CURRENT_USER, net)
    bot_pub = env / "Users" / CURRENT_USER / ".ssh" / "evolve-backup-bot-a.pub"
    bot_pub.write_text("ssh-ed25519 DRIFTEDPUBKEY whatever")

    r = backup_keys.ensure_bot_in_sync("bot-a", CURRENT_USER, net)
    assert r.status == backup_keys.BotSyncStatus.DISTRIBUTED, r.error
    assert bot_pub.read_text().strip() == src_pub


def test_ensure_bot_in_sync_fixes_mirror_drift(env):
    """Drift in clause 3 (mirror) → DISTRIBUTED."""
    _, src_pub = _generate_source(env)
    _make_bot_home(env, CURRENT_USER)
    net = _network(env, bots={"bot-a": {"user": CURRENT_USER}})
    backup_keys.ensure_bot_in_sync("bot-a", CURRENT_USER, net)
    mirror = Path(net["sharedDir"]) / "pubkeys" / "bot-a.pub"
    mirror.write_text("ssh-ed25519 DRIFTEDMIRROR something\n")

    r = backup_keys.ensure_bot_in_sync("bot-a", CURRENT_USER, net)
    assert r.status == backup_keys.BotSyncStatus.DISTRIBUTED, r.error
    assert mirror.read_text().strip() == src_pub


def test_ensure_bot_in_sync_refuses_without_source(env):
    """No canonical source → NO_SOURCE; nothing written."""
    _make_bot_home(env, CURRENT_USER)
    net = _network(env, bots={"bot-a": {"user": CURRENT_USER}})

    r = backup_keys.ensure_bot_in_sync("bot-a", CURRENT_USER, net)
    assert r.status == backup_keys.BotSyncStatus.NO_SOURCE
    assert (env / "Users" / CURRENT_USER / ".ssh").exists() is False


def test_ensure_bot_in_sync_missing_user(env):
    """bot_user not in /etc/passwd → MISSING_USER; nothing written."""
    _generate_source(env)
    net = _network(env, bots={"bot-a": {"user": "no-such-user-xyz-2026"}})

    r = backup_keys.ensure_bot_in_sync("bot-a", "no-such-user-xyz-2026", net)
    assert r.status == backup_keys.BotSyncStatus.MISSING_USER


# ── ensure_deploy_key_registered — clause 4 ──────────────────────────────


def test_ensure_deploy_key_already_present():
    pub = "ssh-ed25519 AAAACANONICAL evolve-shared"
    api = MagicMock()
    api.return_value = (200, [{"key": "ssh-ed25519 AAAACANONICAL trailing-comment"}], {})

    r = backup_keys.ensure_deploy_key_registered(
        "tok", "owner", "repo", pub, "bot-a", github_api=api,
    )
    assert r.already_present and not r.added and r.error is None
    api.assert_called_once_with("GET", "/repos/owner/repo/keys", "tok", None)


def test_ensure_deploy_key_adds_when_absent():
    pub = "ssh-ed25519 AAAACANONICAL evolve-shared"
    api = MagicMock()
    api.side_effect = [
        (200, [], {}),
        (201, {"id": 42}, {}),
    ]

    r = backup_keys.ensure_deploy_key_registered(
        "tok", "owner", "repo", pub, "bot-a", github_api=api,
    )
    assert r.added and not r.already_present and r.error is None
    # Second call was POST with title containing the bot id.
    post = api.call_args_list[1]
    assert post.args[0] == "POST"
    assert post.args[3]["title"] == "evolve-backup-shared-bot-a"
    assert post.args[3]["read_only"] is False


def test_ensure_deploy_key_propagates_get_error():
    api = MagicMock()
    api.return_value = (401, {"message": "Bad credentials"}, {})

    r = backup_keys.ensure_deploy_key_registered(
        "bad-tok", "owner", "repo", "ssh-ed25519 X y", "bot-a", github_api=api,
    )
    assert r.error and "401" in r.error
    assert not r.added and not r.already_present


def test_ensure_deploy_key_propagates_post_error():
    api = MagicMock()
    api.side_effect = [
        (200, [], {}),
        (422, {"message": "key is already in use"}, {}),
    ]

    r = backup_keys.ensure_deploy_key_registered(
        "tok", "owner", "repo", "ssh-ed25519 X y", "bot-a", github_api=api,
    )
    assert r.error and "422" in r.error


def test_ensure_deploy_key_read_only_and_title_override():
    """The repo-puller deploy key registers READ-ONLY (it only pulls) with its
    own title — the DURABLE-VPS-BOOTSTRAP auto-register path. The defaults
    (write access + backup title) stay intact for the backup reconciler."""
    pub = "ssh-ed25519 AAAAPULLER pod"
    api = MagicMock()
    api.side_effect = [
        (200, [], {}),
        (201, {"id": 7}, {}),
    ]

    r = backup_keys.ensure_deploy_key_registered(
        "tok", "owner", "repo", pub, "repo-puller",
        github_api=api, read_only=True, title="evolve repo-puller (pod-x)",
    )
    assert r.added and r.error is None
    post = api.call_args_list[1]
    assert post.args[0] == "POST"
    assert post.args[3]["read_only"] is True
    assert post.args[3]["title"] == "evolve repo-puller (pod-x)"


def test_ensure_deploy_key_422_already_in_use_includes_user_key_hint():
    """The 2026-06-08 failure mode: shared key hits 422 'already in
    use' because GitHub allows the same key as a deploy key on at
    most one repo. The error message must mention the user-key
    alternative so the operator + future readers understand the fix
    direction.
    """
    api = MagicMock()
    api.side_effect = [
        (200, [], {}),
        (422, {"message": "key is already in use"}, {}),
    ]
    r = backup_keys.ensure_deploy_key_registered(
        "tok", "owner", "repo", "ssh-ed25519 X y", "bot-a", github_api=api,
    )
    assert r.error
    assert "user_ssh_key" in r.error or "user-account" in r.error


# ── ensure_user_ssh_key_registered — correct registration for shared model ──


def test_ensure_user_ssh_key_already_present():
    """A pubkey already on /user/keys is silently a no-op success."""
    pub = "ssh-ed25519 AAAACANONICAL evolve-shared"
    api = MagicMock()
    api.return_value = (
        200,
        [{"key": "ssh-ed25519 AAAACANONICAL trailing-comment", "title": "old"}],
        {},
    )
    r = backup_keys.ensure_user_ssh_key_registered(
        "tok", pub, "evolve-backup-shared", github_api=api,
    )
    assert r.already_present and not r.added and r.error is None
    assert r.bot_id == ""  # pod-wide, no per-bot scope
    api.assert_called_once_with("GET", "/user/keys", "tok", None)


def test_ensure_user_ssh_key_posts_when_absent():
    pub = "ssh-ed25519 AAAACANONICAL evolve-shared"
    api = MagicMock()
    api.side_effect = [
        (200, [], {}),
        (201, {"id": 99}, {}),
    ]
    r = backup_keys.ensure_user_ssh_key_registered(
        "tok", pub, "evolve-backup-shared", github_api=api,
    )
    assert r.added and not r.already_present and r.error is None
    # Second call was POST /user/keys with the title.
    post = api.call_args_list[1]
    assert post.args[0] == "POST"
    assert post.args[1] == "/user/keys"
    assert post.args[3] == {"title": "evolve-backup-shared", "key": pub}


def test_ensure_user_ssh_key_surfaces_get_failure():
    api = MagicMock()
    api.return_value = (401, {"message": "Bad credentials"}, {})
    r = backup_keys.ensure_user_ssh_key_registered(
        "bad", "ssh-ed25519 X y", "t", github_api=api,
    )
    assert r.error and "401" in r.error
    assert not r.added


def test_ensure_user_ssh_key_surfaces_post_failure():
    api = MagicMock()
    api.side_effect = [
        (200, [], {}),
        (422, {"message": "key is invalid"}, {}),
    ]
    r = backup_keys.ensure_user_ssh_key_registered(
        "tok", "ssh-ed25519 X y", "t", github_api=api,
    )
    assert r.error and "422" in r.error
    assert not r.added


# ── discover_pod ─────────────────────────────────────────────────────────


def test_discover_pod_classifies_each_status(env):
    """One bot in each of ALIGNED, SHARED_DISK_ONLY, LEGACY_PER_BOT, DRIFTED."""
    _, src_pub = _generate_source(env)
    src_pub_blob = " ".join(src_pub.split()[:2])
    _make_bot_home(env, CURRENT_USER)

    bots = {
        "bot-aligned": {"user": CURRENT_USER, "backupRepoUrl": "git@github.com:o/r-aligned.git"},
        "bot-disk-only": {"user": CURRENT_USER, "backupRepoUrl": "git@github.com:o/r-disk.git"},
        "bot-legacy": {"user": CURRENT_USER, "backupRepoUrl": "git@github.com:o/r-legacy.git"},
        "bot-drifted": {"user": CURRENT_USER, "backupRepoUrl": "git@github.com:o/r-drift.git"},
        "bot-no-url": {"user": CURRENT_USER},
    }
    net = _network(env, bots=bots)

    # Put bot-aligned, bot-disk-only into shared-aligned state.
    for bot_id in ("bot-aligned", "bot-disk-only"):
        backup_keys.ensure_bot_in_sync(bot_id, CURRENT_USER, net)

    # bot-legacy: write a unique pair on disk.
    legacy_priv = env / "Users" / CURRENT_USER / ".ssh" / "evolve-backup-bot-legacy"
    legacy_pub = legacy_priv.with_suffix(".pub")
    legacy_pub_text = "ssh-ed25519 AAAALEGACYBLOB evolve-bot-legacy"
    legacy_priv.write_bytes(b"LEGACY PRIV BYTES")
    legacy_pub.write_text(legacy_pub_text + "\n")

    # bot-drifted: only priv on disk, no pub.
    drift_priv = env / "Users" / CURRENT_USER / ".ssh" / "evolve-backup-bot-drifted"
    drift_priv.write_bytes(b"DRIFTED BYTES")

    fake_api = MagicMock()

    def api_responder(method, path, token, body):
        if path == "/repos/o/r-aligned/keys":
            return (200, [{"key": src_pub_blob + " trailing"}], {})
        if path == "/repos/o/r-disk/keys":
            return (200, [], {})  # no shared key on repo
        if path == "/repos/o/r-legacy/keys":
            return (200, [{"key": " ".join(legacy_pub_text.split()[:2])}], {})
        if path == "/repos/o/r-drift/keys":
            return (200, [], {})
        return (404, None, {})

    fake_api.side_effect = api_responder

    rows = backup_keys.discover_pod(net, token="ghp_x", github_api=fake_api)
    by_id = {r.bot_id: r for r in rows}

    assert by_id["bot-aligned"].status == backup_keys.DiscoveryStatus.ALIGNED
    assert by_id["bot-disk-only"].status == backup_keys.DiscoveryStatus.SHARED_DISK_ONLY
    assert by_id["bot-legacy"].status == backup_keys.DiscoveryStatus.LEGACY_PER_BOT
    assert by_id["bot-drifted"].status == backup_keys.DiscoveryStatus.DRIFTED
    assert by_id["bot-no-url"].status == backup_keys.DiscoveryStatus.NO_BACKUP_URL


def test_discover_pod_without_source_reports_no_source(env):
    bots = {"bot-a": {"user": CURRENT_USER, "backupRepoUrl": "git@github.com:o/r.git"}}
    net = _network(env, bots=bots)
    _make_bot_home(env, CURRENT_USER)

    rows = backup_keys.discover_pod(net)
    assert rows[0].status == backup_keys.DiscoveryStatus.NO_SOURCE


# ── reconcile_pod ────────────────────────────────────────────────────────


def test_reconcile_force_distribute_full_happy_path(env):
    """force_distribute=True + token + saver → all bots aligned + sentinel written.

    Post-2026-06-08 architecture: registration is a SINGLE POST to
    /user/keys (the user-account SSH key path), not one /repos/.../keys
    POST per bot. The same key cannot be a deploy key on multiple
    repos at once — GitHub returns 422 "already in use" — so the
    per-repo approach was structurally broken.
    """
    _make_bot_home(env, CURRENT_USER)
    bots = {
        "bot-a": {"user": CURRENT_USER, "backupRepoUrl": "git@github.com:org/bot-a-backup.git"},
        "bot-b": {"user": CURRENT_USER, "backupRepoUrl": "git@github.com:org/bot-b-backup.git"},
    }
    net = _network(env, bots=bots)

    saved: dict[str, dict] = {}

    def saver(updated):
        saved["net"] = dict(updated)

    posts: list = []

    def api_responder(method, path, token, body):
        if method == "POST" and path == "/user/keys":
            posts.append((path, body["title"]))
            return (201, {"id": len(posts)}, {})
        # GET /user/keys → empty (key not yet registered)
        return (200, [], {})

    fake_api = MagicMock(side_effect=api_responder)

    report = backup_keys.reconcile_pod(
        net,
        force_distribute=True,
        github_token="ghp_token",
        network_save=saver,
        github_api=fake_api,
    )

    assert report.error is None
    assert report.canonical_pubkey is not None
    assert report.canonical_pubkey_generated  # we generated it lazily
    assert all(r.status == backup_keys.BotSyncStatus.DISTRIBUTED for r in report.distributed)
    # Exactly one user-key registration result, bot_id="" indicates
    # pod-wide scope. Per-bot URL-parse skips would also appear here
    # but the URLs are valid in this test.
    user_key_results = [r for r in report.registered if r.bot_id == ""]
    assert len(user_key_results) == 1
    assert user_key_results[0].added and user_key_results[0].error is None
    assert all(r.error is None for r in report.registered)
    assert report.sentinel_written
    assert saved["net"]["backup"]["key_mode"] == "shared"
    # ONE pod-wide POST to /user/keys, not N per-repo POSTs.
    assert posts == [("/user/keys", "evolve-backup-shared")]


def test_reconcile_force_distribute_user_key_failure_skips_sentinel(env):
    """If the pod-wide user-key registration fails, the sentinel is NOT written.

    Post-2026-06-08 this is the only failure mode for clause 4 — one
    GitHub API call covers all bots, so either it succeeds and every
    bot is authenticated, or it fails and nothing is.
    """
    _make_bot_home(env, CURRENT_USER)
    bots = {
        "bot-a": {"user": CURRENT_USER, "backupRepoUrl": "git@github.com:org/a.git"},
        "bot-b": {"user": CURRENT_USER, "backupRepoUrl": "git@github.com:org/b.git"},
    }
    net = _network(env, bots=bots)

    saved: dict[str, dict] = {}

    def saver(updated):
        saved["net"] = dict(updated)

    def api_responder(method, path, token, body):
        if path == "/user/keys" and method == "GET":
            return (401, {"message": "Bad credentials"}, {})
        return (200, [], {})

    fake_api = MagicMock(side_effect=api_responder)
    report = backup_keys.reconcile_pod(
        net,
        force_distribute=True,
        github_token="ghp_token",
        network_save=saver,
        github_api=fake_api,
    )

    assert report.error is None
    # Disk writes for both bots happen even though GitHub registration fails.
    assert all(r.status == backup_keys.BotSyncStatus.DISTRIBUTED for r in report.distributed)
    user_key_results = [r for r in report.registered if r.bot_id == ""]
    assert len(user_key_results) == 1
    assert user_key_results[0].error and "401" in user_key_results[0].error
    # Critically: no sentinel written; the pod is not yet fully migrated.
    assert not report.sentinel_written
    assert "backup" not in saved.get("net", {})


def test_reconcile_discover_only_when_force_distribute_false(env):
    """Read-only mode does no disk writes and does not generate the source."""
    bots = {"bot-a": {"user": CURRENT_USER, "backupRepoUrl": "git@github.com:o/r.git"}}
    net = _network(env, bots=bots)
    _make_bot_home(env, CURRENT_USER)

    report = backup_keys.reconcile_pod(net, force_distribute=False)

    assert report.canonical_pubkey is None
    assert not report.canonical_pubkey_generated
    assert not backup_keys.canonical_source_exists()
    assert report.discovered  # discovery rows present
    assert not report.distributed
    assert not report.registered
    assert not report.sentinel_written


# ── repo URL parsing ─────────────────────────────────────────────────────


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:owner/repo.git", ("owner", "repo")),
    ("git@github.com:owner/repo", ("owner", "repo")),
    ("https://github.com/owner/repo.git", ("owner", "repo")),
    ("https://github.com/owner/repo", ("owner", "repo")),
    ("https://ghp_xxx@github.com/owner/repo.git", ("owner", "repo")),
    ("", None),
    ("not-a-url", None),
    ("https://gitlab.com/owner/repo.git", None),
])
def test_parse_github_repo_url(url, expected):
    assert backup_keys.parse_github_repo_url(url) == expected
