"""tests/test_user_tier_prefs.py — per-user-per-bot tier-prefs storage
(audit #69 Phase C).

The module under test (``evolve_admin.evo.user_tier_prefs``) owns
``{sharedDir}/{botId}/user-tier-prefs.json``. The plugin reads it
on every turn to apply per-user defaults BEFORE the operator's
bot-wide ``userTierOverride.defaultTier`` (Phase A).

Locked here:
  * get_user_pref returns None for missing entries / missing file
  * set_user_pref writes the canonical shape with ISO-8601 timestamp
  * set_user_pref('auto') DELETES the entry — keeps the file from
    accumulating tombstones forever
  * Partial-merge: setting user A's pref doesn't clobber user B's
  * Validation: unknown choices raise ValueError; empty user_key raises
  * Atomic write: temp-file + os.replace; reader never sees half-written
  * list_user_prefs returns the full canonical map
  * derive_user_key_for_caller delegates to the canonical helper
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ── get_user_pref ─────────────────────────────────────────────────────────


def test_get_user_pref_returns_none_when_file_missing(tmp_path):
    from evolve_admin.evo.user_tier_prefs import get_user_pref

    # Pristine tmp dir; no file present
    assert get_user_pref(tmp_path, "admin_bot", "ext:telegram:alice") is None


def test_get_user_pref_returns_none_when_user_absent(tmp_path):
    from evolve_admin.evo.user_tier_prefs import (
        get_user_pref, set_user_pref,
    )

    set_user_pref(tmp_path, "admin_bot", "ext:telegram:alice", "fast")
    assert get_user_pref(tmp_path, "admin_bot", "ext:telegram:bob") is None


def test_get_user_pref_returns_choice_when_set(tmp_path):
    from evolve_admin.evo.user_tier_prefs import (
        get_user_pref, set_user_pref,
    )

    set_user_pref(tmp_path, "admin_bot", "ext:telegram:alice", "power")
    assert get_user_pref(tmp_path, "admin_bot", "ext:telegram:alice") == "power"


def test_get_user_pref_handles_malformed_json(tmp_path):
    from evolve_admin.evo.user_tier_prefs import get_user_pref

    bot_dir = tmp_path / "admin_bot"
    bot_dir.mkdir()
    (bot_dir / "user-tier-prefs.json").write_text("{not valid json")
    # Fail-soft: malformed → None (caller falls back to operator default)
    assert get_user_pref(tmp_path, "admin_bot", "ext:telegram:alice") is None


def test_get_user_pref_normalizes_uppercase(tmp_path):
    """An entry written outside the canonical writer (operator hand-
    edit, future-version drift) with an uppercase choice should still
    be accepted — case-insensitive defense in depth."""
    from evolve_admin.evo.user_tier_prefs import get_user_pref

    bot_dir = tmp_path / "admin_bot"
    bot_dir.mkdir()
    (bot_dir / "user-tier-prefs.json").write_text(json.dumps({
        "users": {"ext:telegram:alice": {"defaultTier": "POWER"}},
    }))
    assert get_user_pref(tmp_path, "admin_bot", "ext:telegram:alice") == "power"


def test_get_user_pref_rejects_unknown_choice_at_read(tmp_path):
    """A file written by a future version with an unknown choice
    must NOT be returned to the plugin — the plugin's tier mapping
    would either crash or do the wrong thing. Reader returns None;
    caller falls through to operator default."""
    from evolve_admin.evo.user_tier_prefs import get_user_pref

    bot_dir = tmp_path / "admin_bot"
    bot_dir.mkdir()
    (bot_dir / "user-tier-prefs.json").write_text(json.dumps({
        "users": {"ext:telegram:alice": {"defaultTier": "turbo-mega"}},
    }))
    assert get_user_pref(tmp_path, "admin_bot", "ext:telegram:alice") is None


def test_get_user_pref_empty_user_key(tmp_path):
    from evolve_admin.evo.user_tier_prefs import get_user_pref

    assert get_user_pref(tmp_path, "admin_bot", "") is None


# ── set_user_pref ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("choice", ["fast", "standard", "power"])
def test_set_user_pref_writes_canonical_shape(tmp_path, choice):
    from evolve_admin.evo.user_tier_prefs import set_user_pref

    entry = set_user_pref(
        tmp_path, "admin_bot", "ext:telegram:alice", choice,
        now=datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc),
    )
    # Canonical post-migration field is ``defaultRole`` (the model-role
    # migration renamed ``defaultTier`` -> ``defaultRole``).
    assert entry == {
        "defaultRole": choice,
        "updated_at": "2026-05-29T12:00:00+00:00",
    }

    # And verify the file on disk has the same shape. Exact-equality locks
    # the canonical post-migration shape: the writer persists ``defaultRole``
    # only, never the legacy ``defaultTier`` alias.
    path = tmp_path / "admin_bot" / "user-tier-prefs.json"
    data = json.loads(path.read_text())
    assert data == {
        "users": {
            "ext:telegram:alice": {
                "defaultRole": choice,
                "updated_at": "2026-05-29T12:00:00+00:00",
            },
        },
    }


def test_set_user_pref_case_insensitive(tmp_path):
    from evolve_admin.evo.user_tier_prefs import set_user_pref

    entry = set_user_pref(tmp_path, "admin_bot", "ext:telegram:alice", "POWER")
    assert entry["defaultRole"] == "power"


def test_set_user_pref_auto_deletes_entry(tmp_path):
    """'auto' means 'stop applying my pref' — the entry is REMOVED so
    the file doesn't accumulate tombstones forever. Subsequent
    get_user_pref returns None, falling through to operator default."""
    from evolve_admin.evo.user_tier_prefs import (
        get_user_pref, set_user_pref,
    )

    set_user_pref(tmp_path, "admin_bot", "ext:telegram:alice", "power")
    assert get_user_pref(tmp_path, "admin_bot", "ext:telegram:alice") == "power"

    set_user_pref(tmp_path, "admin_bot", "ext:telegram:alice", "auto")
    assert get_user_pref(tmp_path, "admin_bot", "ext:telegram:alice") is None

    # File still exists with empty users map (preserves canonical shape)
    path = tmp_path / "admin_bot" / "user-tier-prefs.json"
    data = json.loads(path.read_text())
    assert data["users"] == {}


def test_set_user_pref_auto_on_absent_user_is_silent_noop(tmp_path):
    """Setting auto for a user who never had an entry shouldn't raise
    or write a tombstone — silent no-op."""
    from evolve_admin.evo.user_tier_prefs import set_user_pref

    entry = set_user_pref(tmp_path, "admin_bot", "ext:telegram:alice", "auto")
    assert entry == {}


def test_set_user_pref_partial_merge_preserves_other_users(tmp_path):
    """Multi-user invariant — alice setting her pref must NOT clobber
    bob's. Critical for Phase C's whole-point use case."""
    from evolve_admin.evo.user_tier_prefs import (
        get_user_pref, set_user_pref,
    )

    set_user_pref(tmp_path, "admin_bot", "ext:telegram:alice", "power")
    set_user_pref(tmp_path, "admin_bot", "ext:telegram:bob", "fast")
    set_user_pref(tmp_path, "admin_bot", "ext:telegram:carol", "standard")

    assert get_user_pref(tmp_path, "admin_bot", "ext:telegram:alice") == "power"
    assert get_user_pref(tmp_path, "admin_bot", "ext:telegram:bob") == "fast"
    assert get_user_pref(tmp_path, "admin_bot", "ext:telegram:carol") == "standard"

    # Update alice; bob and carol untouched
    set_user_pref(tmp_path, "admin_bot", "ext:telegram:alice", "fast")
    assert get_user_pref(tmp_path, "admin_bot", "ext:telegram:alice") == "fast"
    assert get_user_pref(tmp_path, "admin_bot", "ext:telegram:bob") == "fast"
    assert get_user_pref(tmp_path, "admin_bot", "ext:telegram:carol") == "standard"


def test_set_user_pref_rejects_unknown_choice(tmp_path):
    from evolve_admin.evo.user_tier_prefs import set_user_pref

    with pytest.raises(ValueError, match="unknown choice"):
        set_user_pref(tmp_path, "admin_bot", "ext:telegram:alice", "turbo")


def test_set_user_pref_rejects_empty_user_key(tmp_path):
    from evolve_admin.evo.user_tier_prefs import set_user_pref

    with pytest.raises(ValueError, match="user_key"):
        set_user_pref(tmp_path, "admin_bot", "", "fast")


def test_set_user_pref_atomic_write(tmp_path):
    """No half-written file visible to readers. We can't easily
    simulate a crash mid-write, but we can verify that NO .tmp files
    linger after a successful write (rename moved them) — the atomic-
    write contract."""
    from evolve_admin.evo.user_tier_prefs import set_user_pref

    set_user_pref(tmp_path, "admin_bot", "ext:telegram:alice", "power")
    bot_dir = tmp_path / "admin_bot"
    tmp_files = list(bot_dir.glob("*.tmp"))
    assert tmp_files == [], (
        f"atomic write left tmp files behind: {tmp_files}"
    )


def test_set_user_pref_creates_missing_bot_dir(tmp_path):
    """First write on a freshly-installed bot should create the
    {sharedDir}/{botId}/ subdir if missing."""
    from evolve_admin.evo.user_tier_prefs import set_user_pref

    # Bot dir doesn't exist yet
    assert not (tmp_path / "newbot").exists()
    set_user_pref(tmp_path, "newbot", "ext:telegram:alice", "fast")
    assert (tmp_path / "newbot" / "user-tier-prefs.json").exists()


# ── list_user_prefs ───────────────────────────────────────────────────────


def test_list_user_prefs_empty(tmp_path):
    from evolve_admin.evo.user_tier_prefs import list_user_prefs

    assert list_user_prefs(tmp_path, "admin_bot") == {}


def test_list_user_prefs_returns_canonical_map(tmp_path):
    from evolve_admin.evo.user_tier_prefs import (
        list_user_prefs, set_user_pref,
    )

    set_user_pref(tmp_path, "admin_bot", "ext:telegram:alice", "power")
    set_user_pref(tmp_path, "admin_bot", "ext:telegram:bob", "fast")

    listed = list_user_prefs(tmp_path, "admin_bot")
    assert set(listed.keys()) == {"ext:telegram:alice", "ext:telegram:bob"}
    assert listed["ext:telegram:alice"]["defaultRole"] == "power"
    assert listed["ext:telegram:bob"]["defaultRole"] == "fast"


def test_list_user_prefs_filters_malformed_entries(tmp_path):
    """Defense in depth — an entry with no role field (neither
    ``defaultRole`` nor the legacy ``defaultTier``, e.g. corrupted by a
    future-version writer) is silently dropped from the listing.
    Plugin / UI never has to see partial shapes."""
    from evolve_admin.evo.user_tier_prefs import list_user_prefs

    bot_dir = tmp_path / "admin_bot"
    bot_dir.mkdir()
    (bot_dir / "user-tier-prefs.json").write_text(json.dumps({
        "users": {
            "good": {"defaultTier": "fast"},
            "missing-tier": {"updated_at": "..."},
            "not-a-dict": "junk",
        },
    }))

    listed = list_user_prefs(tmp_path, "admin_bot")
    assert set(listed.keys()) == {"good"}


# ── derive_user_key_for_caller ────────────────────────────────────────────


def test_derive_user_key_for_caller_delegates_to_identity():
    """Thin convenience wrapper — same behavior as the canonical helper."""
    from evolve_admin.evo.user_tier_prefs import derive_user_key_for_caller

    network = {"pod": {"members": {"admin_bot": {}}}}
    key = derive_user_key_for_caller(
        network, bot_id="admin_bot",
        channel="telegram", sender_external_id="12345",
    )
    assert key == "ext:telegram:12345"


def test_derive_user_key_for_caller_anon_fallback():
    """No channel + no sender → anon:<bot_id>."""
    from evolve_admin.evo.user_tier_prefs import derive_user_key_for_caller

    network = {"pod": {"members": {"admin_bot": {}}}}
    key = derive_user_key_for_caller(
        network, bot_id="admin_bot",
        channel=None, sender_external_id=None,
    )
    assert key == "anon:admin_bot"
