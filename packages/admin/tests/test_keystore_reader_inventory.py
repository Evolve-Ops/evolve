"""tests/test_keystore_reader_inventory.py — a tightening must name its readers.

The check PR #3696 did not have. #3696 clamped ``{shared}/keystore/.machine-key``
to 0640 ``evolve:wheel`` — correct, and it named the attacker it locked out. It
did not name ``analyzer/backup.py``, which runs as the BOT user and read that
key nightly. Nine bots stopped backing up and nothing fired for 23 days.

These tests walk ``secret_config_perms.KEYSTORE_READERS`` and fail when a mode
contract and the reader inventory disagree — in either direction.
"""

from __future__ import annotations

from evolve_admin.secret_config_perms import (
    KEYSTORE_BOT_READABLE_FILES,
    KEYSTORE_DIR_MODES,
    KEYSTORE_PROTECTED_MODES,
    KEYSTORE_READERS,
    KEYSTORE_SUBDIR,
    KEYSTORE_VAULT_GLOB,
    KEYSTORE_VAULT_MODE,
)

_VALID_ACCOUNTS = {"evolve", "evo", "bot", "root"}


def keystore_paths_under_mode_contract() -> tuple[str, ...]:
    """Every keystore path the module asserts a mode or dir-mode for.

    Derived from the module's own tables rather than restated, so a new mode
    table is covered the moment it is added.
    """
    paths = [relpath for relpath, _mode, _why in KEYSTORE_PROTECTED_MODES]
    paths += [relpath for relpath, _mode, _why in KEYSTORE_DIR_MODES]
    paths.append(KEYSTORE_VAULT_GLOB)
    return tuple(paths)


def mode_excludes_bot_accounts(mode: int) -> bool:
    """True when ``mode`` leaves an ordinary bot account with no read access.

    Keystore files are owned ``evolve`` and grouped to the admin group, and no
    ordinary bot is in that group on either platform — so the world triad is
    the only one that can carry a bot's read. No world-read bit means no bot
    read, whatever the file's name suggests.

    Deliberately ignores ACLs: a named ACE grants ONE account (``evo``), never
    the ``bot`` class, so an ACE can never make a bot-class claim true.
    """
    return not mode & 0o004


def test_every_mode_contracted_path_has_a_reader_entry():
    """Adding a path to a mode table without declaring who reads it fails here.

    An empty tuple is a legitimate answer ("nothing reads this any more" —
    evolve-signing.key). A MISSING key is not: that is the #3696 shape, where
    the question was never asked.
    """
    missing = [
        p for p in keystore_paths_under_mode_contract()
        if p not in KEYSTORE_READERS
    ]
    assert not missing, (
        f"keystore paths under a mode contract with no reader inventory entry: "
        f"{missing}. Add them to KEYSTORE_READERS — naming the accounts that "
        f"read each path is what makes a future tightening reviewable. If "
        f"nothing reads it, say so with an explicit empty tuple."
    )


def test_reader_accounts_come_from_the_known_vocabulary():
    for path, readers in KEYSTORE_READERS.items():
        for r in readers:
            assert r.account in _VALID_ACCOUNTS, (
                f"{path}: unknown reader account {r.account!r}; "
                f"expected one of {sorted(_VALID_ACCOUNTS)}"
            )
            assert r.via.strip(), f"{path}: reader {r.account!r} has no 'via'"


def test_no_bot_reader_is_claimed_on_a_path_that_excludes_bots():
    """The load-bearing assertion: a 'bot' reader on a bot-denying mode.

    This is #3696 stated as a check. If someone re-adds a bot reader to
    ``.machine-key`` (say, by reverting the verdict endpoint and going back to
    reading the PAT on the bot), this fails and names the contradiction
    instead of letting the backups go quiet.
    """
    modes = {relpath: mode for relpath, mode, _why in KEYSTORE_PROTECTED_MODES}
    modes.update({relpath: mode for relpath, mode, _why in KEYSTORE_DIR_MODES})
    modes[KEYSTORE_VAULT_GLOB] = KEYSTORE_VAULT_MODE

    violations = []
    for path, readers in KEYSTORE_READERS.items():
        mode = modes.get(path)
        if mode is None:
            continue
        if not mode_excludes_bot_accounts(mode):
            continue
        for r in readers:
            if r.account == "bot":
                violations.append((path, oct(mode), r.via))

    assert not violations, (
        "a bot account is listed as a reader of a path whose mode denies bot "
        f"accounts: {violations}. Either the mode is wrong or the reader is "
        "stale. Do NOT resolve this by widening the mode — that re-opens "
        "#3696. Route the read through an evolve-owned verdict instead."
    )


def test_machine_key_has_no_bot_reader():
    """Pins the fix: the bot must not be back on the direct-PAT read path.

    Since the verdict endpoint, a bot-user backup run asks the admin daemon
    whether its repo is private; it never decrypts the vault. If a 'bot'
    reader reappears here, the vault would have to be widened to make it
    true — the exact trade #3696 refused.
    """
    readers = KEYSTORE_READERS[f"{KEYSTORE_SUBDIR}/.machine-key"]
    assert readers, "machine-key must declare its (non-bot) readers"
    assert not [r for r in readers if r.account == "bot"]
    assert {r.account for r in readers} == {"evolve", "evo"}


def test_bot_readable_files_declare_a_bot_reader():
    """The other direction: the five 0644 files at the keystore root.

    These are why ``keystore/`` must stay 0755. If the inventory ever stops
    claiming a bot reader for them, the rationale for the loose root has
    silently evaporated and someone will tighten it (as #3700 did).
    """
    for name in KEYSTORE_BOT_READABLE_FILES:
        readers = KEYSTORE_READERS[f"{KEYSTORE_SUBDIR}/{name}"]
        assert [r for r in readers if r.account == "bot"], (
            f"{name} is in KEYSTORE_BOT_READABLE_FILES but the inventory "
            f"claims no bot reader — keystore/ stays 0755 for these files, "
            f"and that justification now has no support."
        )


def test_mode_excludes_bot_accounts_predicate():
    assert mode_excludes_bot_accounts(0o640)
    assert mode_excludes_bot_accounts(0o600)
    assert mode_excludes_bot_accounts(0o750)
    assert not mode_excludes_bot_accounts(0o644)
    assert not mode_excludes_bot_accounts(0o755)
