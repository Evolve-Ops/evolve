"""Tests for evolve_util — the blessed shared-primitive home (Phase 6.2).

Also pins the dup-primitive-lint contract: the repo must stay free of
local re-definitions of these primitives.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from evolve_util import (
    assert_safe_sudo_dest,
    atomic_write_json,
    atomic_write_text,
    now_iso,
    now_iso_micro,
    now_iso_offset,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


# ── atomic_write_text ────────────────────────────────────────────────────────


def test_atomic_write_text_roundtrip(tmp_path):
    p = tmp_path / "out.txt"
    atomic_write_text(p, "hello\n")
    assert p.read_text() == "hello\n"


def test_atomic_write_text_overwrites_existing(tmp_path):
    p = tmp_path / "out.txt"
    p.write_text("old")
    atomic_write_text(p, "new")
    assert p.read_text() == "new"


def test_atomic_write_text_leaves_no_temp_droppings(tmp_path):
    p = tmp_path / "out.txt"
    atomic_write_text(p, "x")
    assert [f.name for f in tmp_path.iterdir()] == ["out.txt"]


def test_atomic_write_text_failure_preserves_original_and_cleans_tmp(tmp_path):
    p = tmp_path / "out.txt"
    p.write_text("original")
    # Lone surrogate can't encode to UTF-8 — f.write raises AFTER the temp
    # file exists, exercising the unlink-on-failure path.
    with pytest.raises(UnicodeEncodeError):
        atomic_write_text(p, "x\udfff")
    assert p.read_text() == "original"
    assert [f.name for f in tmp_path.iterdir()] == ["out.txt"]


def test_atomic_write_json_unserializable_preserves_original(tmp_path):
    p = tmp_path / "out.json"
    p.write_text("{}")
    with pytest.raises(TypeError):
        atomic_write_json(p, {"k": object()})
    assert p.read_text() == "{}"
    assert [f.name for f in tmp_path.iterdir()] == ["out.json"]


def test_atomic_write_text_mode_chmod(tmp_path):
    p = tmp_path / "shared.txt"
    atomic_write_text(p, "x", mode=0o644)
    assert stat.S_IMODE(p.stat().st_mode) == 0o644


def test_atomic_write_text_default_mode_is_private(tmp_path):
    p = tmp_path / "private.txt"
    atomic_write_text(p, "x")
    # mkstemp default — owner-only
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


# ── atomic_write_json ────────────────────────────────────────────────────────


def test_atomic_write_json_roundtrip_and_defaults(tmp_path):
    p = tmp_path / "out.json"
    atomic_write_json(p, {"b": 1, "a": 2})
    raw = p.read_text()
    assert json.loads(raw) == {"b": 1, "a": 2}
    assert raw.startswith("{\n  ")          # indent=2
    assert raw.index('"b"') < raw.index('"a"')  # insertion order preserved


def test_atomic_write_json_sort_keys(tmp_path):
    p = tmp_path / "out.json"
    atomic_write_json(p, {"b": 1, "a": 2}, sort_keys=True)
    raw = p.read_text()
    assert raw.index('"a"') < raw.index('"b"')


# ── timestamps ───────────────────────────────────────────────────────────────


def test_now_iso_format():
    s = now_iso()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", s), s


def test_now_iso_offset_format():
    s = now_iso_offset()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", s), s


def test_now_iso_micro_format():
    s = now_iso_micro()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00", s), s


# ── assert_safe_sudo_dest (#3566 audit D-2) ──────────────────────────────────
#
# The gate every CLAUDE.md "/tmp staging + sudo /bin/cp" writer must call
# before shelling out. `cp` follows a symlink at the DEST and `chown`/`chmod`
# follow a symlink ARGUMENT, all as root — so an unchecked dest is a root-write
# primitive. Every check must use os.lstat; a `Path.exists()`/`is_file()`
# implementation would FOLLOW the link and wave the attack through, which is
# what test_..._symlink_to_regular_file_still_refused pins.


def test_safe_sudo_dest_allows_absent_dest(tmp_path):
    """A fresh destination is fine — cp creates a real file."""
    assert_safe_sudo_dest(tmp_path / "evolve-tiers.json")  # no raise


def test_safe_sudo_dest_allows_existing_regular_file(tmp_path):
    p = tmp_path / "evolve-tiers.json"
    p.write_text("{}")
    assert_safe_sudo_dest(p)  # no raise


def test_safe_sudo_dest_accepts_str_path(tmp_path):
    """Callers pass either a Path or a str; both must be checked, not one
    silently coerced into a no-op."""
    p = tmp_path / "evolve-tiers.json"
    p.symlink_to(tmp_path / "victim")
    with pytest.raises(PermissionError, match="SYMLINK"):
        assert_safe_sudo_dest(str(p))


def test_safe_sudo_dest_refuses_symlink_dest(tmp_path):
    """THE hazard: a symlink planted at the dest redirects a root cp onto its
    target. Refuse, and name the target so the operator can see what was
    aimed at."""
    victim = tmp_path / "victim.json"
    victim.write_text("secret")
    dest = tmp_path / "evolve-tiers.json"
    dest.symlink_to(victim)

    with pytest.raises(PermissionError) as exc:
        assert_safe_sudo_dest(dest)
    assert "SYMLINK" in str(exc.value)
    assert str(victim) in str(exc.value)
    # The victim is untouched — the gate must not itself resolve/write.
    assert victim.read_text() == "secret"


def test_safe_sudo_dest_symlink_to_regular_file_still_refused(tmp_path):
    """Regression pin for the lstat-vs-stat bug class. A symlink pointing at a
    perfectly ordinary regular file passes every ``Path.is_file()`` check —
    which is precisely why the implementation must lstat."""
    target = tmp_path / "ordinary.json"
    target.write_text("{}")
    dest = tmp_path / "evolve-tiers.json"
    dest.symlink_to(target)
    assert dest.is_file()  # the check a naive implementation would make
    with pytest.raises(PermissionError, match="SYMLINK"):
        assert_safe_sudo_dest(dest)


def test_safe_sudo_dest_refuses_dangling_symlink_dest(tmp_path):
    """A symlink to a not-yet-existing path is the *creation* variant: the root
    cp would MINT the target. FileNotFoundError from a following stat would
    read as "fresh dest, go ahead" — lstat sees the link."""
    dest = tmp_path / "evolve-tiers.json"
    dest.symlink_to(tmp_path / "does-not-exist.json")
    with pytest.raises(PermissionError, match="SYMLINK"):
        assert_safe_sudo_dest(dest)


def test_safe_sudo_dest_refuses_symlinked_parent(tmp_path):
    """``<link>/evolve-tiers.json`` redirects the whole write out of tree even
    when the leaf name has nothing planted at it."""
    real = tmp_path / "real-openclaw"
    real.mkdir()
    link = tmp_path / ".openclaw"
    link.symlink_to(real)
    with pytest.raises(PermissionError, match="symlink or not a directory"):
        assert_safe_sudo_dest(link / "evolve-tiers.json")


def test_safe_sudo_dest_refuses_non_regular_dest(tmp_path):
    """A fifo (or device) at the dest is not a config file; a root cp into one
    blocks or has side effects."""
    fifo = tmp_path / "evolve-tiers.json"
    os.mkfifo(fifo)
    with pytest.raises(PermissionError, match="not a regular file"):
        assert_safe_sudo_dest(fifo)


def test_safe_sudo_dest_refuses_hard_link_dest(tmp_path):
    """The variant that needs no symlink at all, and that every other check
    here waves through: a hard link IS a real regular file, and ``lstat``
    reports the VICTIM inode's uid and mode because there is no indirection to
    see through. A root ``chown`` through one transfers ownership of the victim
    inode permanently — and the drift detectors read the victim's ``uid=0`` as
    exactly the condition their repair exists to fix, so it is summoned rather
    than merely reachable. macOS lets an unprivileged user create the link
    against a file it neither owns nor can read; Linux blocks it under the
    default ``fs.protected_hardlinks=1``, and macOS is the primary pod."""
    victim = tmp_path / "victim.json"
    victim.write_text("secret")
    dest = tmp_path / "evolve-tiers.json"
    os.link(victim, dest)

    assert not dest.is_symlink()          # every symlink check passes it
    assert dest.stat().st_mode == victim.stat().st_mode  # …and it IS regular
    with pytest.raises(PermissionError) as exc:
        assert_safe_sudo_dest(dest)
    assert "HARD LINK" in str(exc.value)
    assert "SYMLINK" not in str(exc.value)  # distinct message: go find the other name
    assert victim.read_text() == "secret"


def test_safe_sudo_dest_allows_single_link_file(tmp_path):
    """The nlink check must not refuse an ordinary destination — a file written
    by ``cp`` or ``os.replace`` always has exactly one link."""
    p = tmp_path / "evolve-tiers.json"
    p.write_text("{}")
    assert p.stat().st_nlink == 1
    assert_safe_sudo_dest(p)  # no raise


def test_safe_sudo_dest_refuses_missing_parent(tmp_path):
    with pytest.raises(PermissionError, match="cannot verify"):
        assert_safe_sudo_dest(tmp_path / "nope" / "evolve-tiers.json")


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory perms")
def test_safe_sudo_dest_fails_closed_on_unreadable_dest(tmp_path):
    """DELIBERATE behaviour: when the dest cannot even be lstat'd (the 0700
    .openclaw clamp that secret_config_perms._reassert_evolve_read_acl exists
    to heal), refuse rather than issue a root cp we cannot verify — and say
    "cannot verify", NOT "SYMLINK", so an operator can tell an outage from an
    attack."""
    parent = tmp_path / "clamped"
    parent.mkdir()
    (parent / "evolve-tiers.json").write_text("{}")
    os.chmod(parent, 0o000)
    try:
        with pytest.raises(PermissionError) as exc:
            assert_safe_sudo_dest(parent / "evolve-tiers.json")
    finally:
        os.chmod(parent, 0o700)
    assert "cannot verify" in str(exc.value)
    assert "SYMLINK" not in str(exc.value)


# ── anchor= : the intermediate-component walk (#3566 audit D-2 residual) ─────
#
# Without an anchor the gate lstats exactly two things — the dest and its
# parent — so every component ABOVE the parent is resolved by the kernel while
# doing so. Sound for a FLAT relpath (`.openclaw/evolve-tiers.json`: the parent
# IS `.openclaw` and is checked); NOT sound for the nested entries in
# BOT_SECRET_CONFIG_RELPATHS, whose `agents` / `agents/main` / `workspace`
# components live inside the bot-owned tree. `anchor` names the shallowest
# directory the caller vouches for; everything strictly below it is walked.


def _bot_tree(tmp_path):
    """``<root>/team_bot_a/.openclaw`` — anchor is the bot home, one level above
    ``.openclaw`` (the home is root-owned; ``.openclaw`` and below is not).
    Placeholder bot names per docs/PLACEHOLDER_NAMING.md."""
    home = tmp_path / "team_bot_a"
    oc = home / ".openclaw"
    oc.mkdir(parents=True)
    return home, oc


@pytest.mark.parametrize(
    "rel",
    ["agents/main/agent/auth-profiles.json", "workspace/.git/config"],
    ids=["auth-profiles", "git-config"],
)
def test_safe_sudo_dest_refuses_symlinked_intermediate(tmp_path, rel):
    """THE finding. Both nested ``BOT_SECRET_CONFIG_RELPATHS`` entries, with the
    link planted at the FIRST bot-owned component (``agents`` / ``workspace``).

    Note what the two unanchored lstats see here: ``path.parent`` is a real
    directory and the dest is a real regular file — the pre-anchor gate has
    nothing to object to, which is exactly why this needed its own fix."""
    home, oc = _bot_tree(tmp_path)
    head, *tail = Path(rel).parts       # "agents" / "workspace", then the rest
    victim_tree = tmp_path / "victim-tree"
    victim_file = victim_tree.joinpath(*tail)
    victim_file.parent.mkdir(parents=True)
    victim_file.write_text("VICTIM")
    (oc / head).symlink_to(victim_tree)  # the plant, at an INTERMEDIATE component
    dest = oc / rel

    assert not dest.is_symlink() and dest.is_file()   # both unanchored lstats are happy
    assert_safe_sudo_dest(dest)                       # …and pass, unanchored

    with pytest.raises(PermissionError) as exc:
        assert_safe_sudo_dest(dest, anchor=home)
    assert "intermediate component" in str(exc.value)
    # Names the SHALLOWEST planted component — the one to go remove.
    assert str(oc / head) in str(exc.value)
    assert victim_file.read_text() == "VICTIM"


def test_safe_sudo_dest_refuses_symlinked_deep_intermediate(tmp_path):
    """The plant does not have to be at the first component: ``agents/main`` is
    equally bot-owned, and ``agents`` above it is a genuine directory."""
    home, oc = _bot_tree(tmp_path)
    (oc / "agents").mkdir()
    victim = tmp_path / "victim-main"
    (victim / "agent").mkdir(parents=True)
    (victim / "agent" / "auth-profiles.json").write_text("VICTIM")
    (oc / "agents" / "main").symlink_to(victim)
    dest = oc / "agents/main/agent/auth-profiles.json"

    with pytest.raises(PermissionError, match="intermediate component"):
        assert_safe_sudo_dest(dest, anchor=home)


def test_safe_sudo_dest_anchored_allows_real_nested_tree(tmp_path):
    """The legitimate shape must still pass — a false refusal here skips the
    0600 self-heal on the very files it exists for."""
    home, oc = _bot_tree(tmp_path)
    for rel in ("agents/main/agent/auth-profiles.json", "workspace/.git/config"):
        dest = oc / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("{}")
        assert_safe_sudo_dest(dest, anchor=home)  # no raise


def test_safe_sudo_dest_anchored_allows_flat_relpaths(tmp_path):
    """Flat dests keep passing anchored (absent and present both) — the walk is
    empty for them and the parent check does the work as before."""
    home, oc = _bot_tree(tmp_path)
    assert_safe_sudo_dest(oc / "evolve-tiers.json", anchor=home)  # absent
    (oc / "openclaw.json").write_text("{}")
    assert_safe_sudo_dest(oc / "openclaw.json", anchor=home)      # present


def test_safe_sudo_dest_anchored_still_refuses_symlinked_leaf(tmp_path):
    """The anchor ADDS a check; it must not displace the existing ones."""
    home, oc = _bot_tree(tmp_path)
    (oc / "agents" / "main" / "agent").mkdir(parents=True)
    victim = tmp_path / "victim.json"
    victim.write_text("secret")
    dest = oc / "agents/main/agent/auth-profiles.json"
    dest.symlink_to(victim)
    with pytest.raises(PermissionError, match="SYMLINK"):
        assert_safe_sudo_dest(dest, anchor=home)


def test_safe_sudo_dest_anchored_refuses_non_dir_intermediate(tmp_path):
    """A regular file where a directory belongs is not a symlink, and the walk
    must refuse it too rather than only checking for links."""
    home, oc = _bot_tree(tmp_path)
    (oc / "agents").write_text("not a directory")
    with pytest.raises(PermissionError, match="not a directory"):
        assert_safe_sudo_dest(oc / "agents/main/agent/auth-profiles.json", anchor=home)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory perms")
def test_safe_sudo_dest_anchored_fails_closed_on_unreadable_intermediate(tmp_path):
    """Same fail-closed rule as the dest itself, and the same distinct wording:
    "cannot verify" is an outage, "is a symlink" is an attack."""
    home, oc = _bot_tree(tmp_path)
    (oc / "agents" / "main" / "agent").mkdir(parents=True)
    (oc / "agents" / "main" / "agent" / "auth-profiles.json").write_text("{}")
    os.chmod(oc / "agents", 0o000)
    try:
        with pytest.raises(PermissionError) as exc:
            assert_safe_sudo_dest(
                oc / "agents/main/agent/auth-profiles.json", anchor=home
            )
    finally:
        os.chmod(oc / "agents", 0o700)
    assert "cannot verify" in str(exc.value)
    assert "is a symlink" not in str(exc.value)


def test_safe_sudo_dest_anchor_above_it_is_deliberately_NOT_checked(tmp_path):
    """THE compatibility contract of this design, pinned on purpose.

    A symlinked component AT OR ABOVE the anchor is ALLOWED. That is the whole
    reason this is an anchored walk rather than
    ``os.path.realpath(path) == str(path)``: realpath cannot tell a host shape
    (``/home -> /export/home``; macOS homes on a mounted volume) from an attack,
    and a false refusal here would stop the 0600 self-heal and the repair that
    lets a bot read its own routing config. Components above the anchor are
    root-owned by contract — a link there needs root already, so refusing on one
    buys nothing and costs availability.

    Both live pods resolve ``/Users`` and ``/home`` to themselves (checked
    2026-08-11), so this changes nothing on the current fleet; it is the install
    base this protects.
    """
    real_root = tmp_path / "real-volume"
    real_root.mkdir()
    home = real_root / "team_bot_a"
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "agents" / "main" / "agent").mkdir(parents=True)
    (home / ".openclaw" / "agents/main/agent/auth-profiles.json").write_text("{}")

    linked_root = tmp_path / "Users"          # the symlinked ancestor
    linked_root.symlink_to(real_root)
    anchor = linked_root / "team_bot_a"
    dest = anchor / ".openclaw" / "agents/main/agent/auth-profiles.json"

    assert linked_root.is_symlink()
    assert os.path.realpath(dest) != str(dest)   # a realpath== check would refuse
    assert_safe_sudo_dest(dest, anchor=anchor)   # …this does not


def test_safe_sudo_dest_refuses_path_outside_anchor(tmp_path):
    """Fail closed rather than walk nothing: a dest outside the anchor means the
    caller cannot say which components are attacker-writable, which is the one
    thing the anchor is for."""
    home, _ = _bot_tree(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "openclaw.json").write_text("{}")
    with pytest.raises(PermissionError, match="not under the trusted anchor"):
        assert_safe_sudo_dest(other / "openclaw.json", anchor=home)


def test_safe_sudo_dest_refuses_dotdot_and_relative_under_anchor(tmp_path):
    """``..`` makes the trusted prefix unprovable by string containment
    (``/home/a/../b`` is "under" ``/home/a`` textually), and a relative path has
    no provable prefix at all. Both refuse."""
    home, oc = _bot_tree(tmp_path)
    with pytest.raises(PermissionError, match=r"'\.\.' in path"):
        assert_safe_sudo_dest(oc / ".." / ".." / "openclaw.json", anchor=home)
    with pytest.raises(PermissionError, match="absolute paths"):
        assert_safe_sudo_dest(Path("team_bot_a/.openclaw/openclaw.json"), anchor="rel")


def test_safe_sudo_dest_anchor_is_opt_in(tmp_path):
    """The two flat-relpath callers (oc_model under the SYSTEM python,
    migrate_model_roles) must keep working against the unchanged signature — a
    required anchor would raise TypeError on a stale-module-cache pull, at the
    moment the gate is guarding a root cp."""
    import inspect

    sig = inspect.signature(assert_safe_sudo_dest)
    assert sig.parameters["anchor"].default is None
    assert sig.parameters["anchor"].kind is inspect.Parameter.KEYWORD_ONLY
    home, oc = _bot_tree(tmp_path)
    (oc / "agents" / "main" / "agent").mkdir(parents=True)
    (oc / "agents/main/agent/auth-profiles.json").write_text("{}")
    assert_safe_sudo_dest(oc / "agents/main/agent/auth-profiles.json")  # no raise


# ── dup-primitive-lint contract ──────────────────────────────────────────────


def test_dup_primitive_lint_repo_is_clean():
    """The gate the migration established: no local re-definitions of the
    shared primitives anywhere in production code. If this fails, someone
    added a `def _atomic_write` / `def _now_iso` / `def _bot_home` copy —
    import the blessed one from evolve_util / evolve_config instead."""
    lint = _REPO_ROOT / "tools" / "dup-primitive-lint"
    r = subprocess.run(
        [sys.executable, str(lint), "--all"],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, f"dup-primitive-lint found violations:\n{r.stdout}{r.stderr}"
