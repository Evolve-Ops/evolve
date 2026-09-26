"""Regression test for _bot_pubkey returning the canonical shared pubkey.

The 2026-06-07 incident: the GitHub backup wizard reported
"✓ reused — already has evolve deploy key" for 5 bots whose
backups were actually broken. Root cause was a priority-order
bug — _bot_pubkey read /Users/evolve/.ssh/evolve-backup-<bot>.pub
(stale per-bot files from the older per-bot model). Those files
held the OLD per-bot pubkeys, which WERE still registered on
GitHub as deploy keys. So the wizard's check succeeded against
the WRONG key, while the bot's ~/.ssh/ actually held the new
shared key (from a subsequent Distribute Key run) that wasn't
registered anywhere on GitHub.

PR #2357 patched this incrementally by changing _bot_pubkey's
lookup order (shared_dir mirror first, then legacy fallback).
The unification spec at internal/spec-backup-key-distribution-
unification-2026-06-08.md closes the bug class entirely — there
is no priority order because there is only ONE pubkey source
(the canonical pod-wide shared key). This test pins the new
contract: _bot_pubkey returns the canonical pubkey regardless
of bot_id.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


_CANONICAL_PUBKEY = "ssh-ed25519 AAAAC3CANONICAL_SHARED_KEY evolve-backup-shared\n"


def _bot_pubkey_from_closure(app):
    """Pull the closure-bound _bot_pubkey from the wizard route."""
    for rule in app.url_map.iter_rules():
        if rule.rule == "/api/admin/onboard/github":
            vf = app.view_functions[rule.endpoint]
            free_vars = vf.__code__.co_freevars
            for i, name in enumerate(free_vars):
                if name == "_bot_pubkey":
                    return vf.__closure__[i].cell_contents
    raise RuntimeError("_bot_pubkey not in closure of wizard route")


def test_bot_pubkey_returns_canonical_shared_pubkey(tmp_path, monkeypatch):
    """The canonical source exists; _bot_pubkey returns its pubkey
    regardless of which bot_id is requested. Same value for every bot.
    """
    from evolve_admin import backup_keys
    from evolve_admin.web import server as server_mod

    ssh_root = tmp_path / "evolve-ssh"
    ssh_root.mkdir()
    priv = ssh_root / "evolve-backup-shared"
    pub = ssh_root / "evolve-backup-shared.pub"
    priv.write_bytes(b"FAKE PRIVATE KEY")
    pub.write_text(_CANONICAL_PUBKEY)

    monkeypatch.setenv(backup_keys._ENV_PRIV, str(priv))
    monkeypatch.setenv(backup_keys._ENV_PUB, str(pub))

    app = server_mod.create_app(network_path=server_mod.DEFAULT_NETWORK_CONFIG)
    bot_pubkey = _bot_pubkey_from_closure(app)

    # Same canonical value for every bot — there is no per-bot pubkey
    # anymore, by design.
    for bot_id in ("team_bot_a", "team_bot_b", "team_bot_c"):
        result = bot_pubkey(bot_id)
        assert result == _CANONICAL_PUBKEY.strip(), (
            f"expected canonical pubkey for {bot_id}, got: {result!r}"
        )


def test_bot_pubkey_generates_canonical_source_if_missing(tmp_path, monkeypatch):
    """No canonical source present → _bot_pubkey generates one and
    returns its pubkey. Fresh-install onboarding before Distribute Key
    has been run depends on this path.
    """
    from evolve_admin import backup_keys
    from evolve_admin.web import server as server_mod

    ssh_root = tmp_path / "evolve-ssh"
    ssh_root.mkdir()
    priv = ssh_root / "evolve-backup-shared"
    pub = ssh_root / "evolve-backup-shared.pub"

    monkeypatch.setenv(backup_keys._ENV_PRIV, str(priv))
    monkeypatch.setenv(backup_keys._ENV_PUB, str(pub))
    assert not priv.exists()  # precondition — no source

    app = server_mod.create_app(network_path=server_mod.DEFAULT_NETWORK_CONFIG)
    bot_pubkey = _bot_pubkey_from_closure(app)

    result = bot_pubkey("team_bot_a")
    assert result and result.startswith("ssh-ed25519 ")
    assert priv.exists()  # _bot_pubkey called ensure_shared_source_generated
    assert pub.exists()
