"""Refusal tests for pm-probe-cat — the PM probe's ONE privileged reader.

Brief: internal/dispatch/done/pm-mini-probe-readonly-mcp.md.

This program is what ``/etc/sudoers.d/pm-probe`` grants root for, and it is
the whole of the operand contract that the sudoers ``cat`` depth ladder only
pretended to enforce (sudoers(5) § Wildcards: argument wildcards cross ``/``
and spaces, so every rung was root ``cat`` of any file). Each of the four
things the ladder could not do has a test here:

  * ``..`` and anything that resolves outside the root set — refused AFTER
    ``realpath``, which is what makes a planted symlink useless;
  * a symlink at any component — refused, because ``sudo cat`` followed them
    and ``~/.openclaw`` and ``/tmp/pm`` are writable by non-admin users;
  * a second operand — refused, because ``cat a b`` was the ladder's easiest
    escape;
  * an oversized file — truncated at the cap, and SAID so.

The tests run the RENDERED artifact (the module with its root patterns filled
in from a platform profile) rather than the source module, because the
rendered file is what is installed and the source deliberately carries an
empty root set.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS  # noqa: E402

from evolve_admin import pm_probe_cat, pm_probe_install  # noqa: E402


def _rendered(roots: tuple[str, ...]) -> types.ModuleType:
    """The installed artifact, with ``roots`` as its root set."""
    src = pm_probe_install.render_wrapper(MACOS)
    mod = types.ModuleType("pm_probe_cat_rendered")
    exec(compile(src, "pm-probe-cat", "exec"), mod.__dict__)  # noqa: S102
    mod.ROOT_PATTERNS = roots
    return mod


@pytest.fixture()
def reader(tmp_path):
    """A rendered reader whose only allowed root is a tmp tree."""
    root = tmp_path / "pod"
    root.mkdir()
    # tmp_path itself can sit under a symlinked /tmp (macOS), so anchor the
    # pattern on the RESOLVED tree — the same thing the reader compares against.
    real = os.path.realpath(str(root))
    mod = _rendered((rf"{real}(?:/.*)?",))
    return mod, root


class _Out:
    """A stdout stand-in that keeps the bytes."""

    def __init__(self) -> None:
        self.data = b""

    def write(self, b: bytes) -> None:
        self.data += b

    def flush(self) -> None:
        pass


class _Err:
    def __init__(self) -> None:
        self.text = ""

    def write(self, s: str) -> None:
        self.text += s

    def flush(self) -> None:
        pass


def _run(mod, args: list[str]):
    out, err = _Out(), _Err()
    code = mod.main(args, out=out, err=err)
    return code, out.data, err.text


# ── The four refusals the ladder could not express ───────────────────────────


def test_reads_a_file_inside_the_root_set(reader) -> None:
    mod, root = reader
    (root / "network.json").write_text("{}\n")
    code, data, err = _run(mod, [str(root / "network.json")])
    assert (code, data, err) == (0, b"{}\n", "")


def test_a_traversing_operand_is_refused(reader) -> None:
    mod, root = reader
    code, data, err = _run(mod, [str(root / ".." / "escape.txt")])
    assert code == 2
    assert "path-traversal" in err
    assert data == b""


def test_an_operand_that_resolves_out_of_tree_is_refused(reader, tmp_path) -> None:
    """The ladder's real hole: the check has to run on the RESOLVED path.

    A bot user who can write ``~/.openclaw`` or ``/tmp/pm`` plants a link and
    waits for the PM to read it as root; here the link is inside the root set
    and its target is not."""
    mod, root = reader
    secret = tmp_path / "secret.txt"
    secret.write_text("master.passwd\n")
    (root / "innocent.json").symlink_to(secret)
    code, data, err = _run(mod, [str(root / "innocent.json")])
    assert code == 2
    assert "path-outside-allowlist" in err
    assert b"master.passwd" not in data


def test_a_symlink_swapped_in_after_resolution_is_refused(reader, tmp_path) -> None:
    """O_NOFOLLOW on every component closes the realpath→open race.

    Simulated by handing ``open_no_follow`` a path that is a symlink by the
    time it runs — which is exactly what an attacker who wins the race gets."""
    mod, root = reader
    target = root / "real.json"
    target.write_text("ok\n")
    link = root / "link.json"
    link.symlink_to(target)
    with pytest.raises(mod.Refused) as exc:
        mod.open_no_follow(str(link))
    assert exc.value.rule == "symlink"


def test_a_symlinked_directory_component_is_refused(reader, tmp_path) -> None:
    mod, root = reader
    real_dir = root / "real"
    real_dir.mkdir()
    (real_dir / "f.json").write_text("ok\n")
    (root / "linkdir").symlink_to(real_dir)
    with pytest.raises(mod.Refused) as exc:
        mod.open_no_follow(str(root / "linkdir" / "f.json"))
    assert exc.value.rule == "symlink"


def test_a_second_operand_is_refused(reader) -> None:
    """`sudo cat a b` was one space away under every rung of the ladder."""
    mod, root = reader
    (root / "a").write_text("a\n")
    code, data, err = _run(mod, [str(root / "a"), "/etc/passwd"])
    assert code == 2
    assert "operand-count" in err
    assert data == b""


def test_no_operand_is_refused(reader) -> None:
    mod, _ = reader
    code, _data, err = _run(mod, [])
    assert code == 2
    assert "operand-count" in err


def test_an_oversized_file_is_truncated_and_says_so(reader) -> None:
    mod, root = reader
    big = root / "big.log"
    big.write_bytes(b"a" * (mod.MAX_OUTPUT_BYTES * 2))
    code, data, err = _run(mod, [str(big)])
    assert code == 0
    assert len(data) == mod.MAX_OUTPUT_BYTES
    assert "truncated" in err


# ── The rest of the contract ─────────────────────────────────────────────────


def test_a_flag_shaped_operand_is_refused(reader) -> None:
    mod, _ = reader
    code, _data, err = _run(mod, ["--help"])
    assert code == 2
    assert "operand-flag" in err


def test_a_relative_operand_is_refused(reader) -> None:
    mod, _ = reader
    code, _data, err = _run(mod, ["network.json"])
    assert code == 2
    assert "relative-path" in err


def test_a_credential_shaped_operand_is_refused_before_it_is_opened(reader) -> None:
    """Refused on the NAME, so it holds whatever the file turns out to hold —
    unlike the output masking, which is a heuristic over the bytes."""
    mod, root = reader
    agent = root / "agent"
    agent.mkdir()
    (agent / "auth-profiles.json").write_text('{"apiKey": "sk-ant-real"}\n')
    code, data, err = _run(mod, [str(agent / "auth-profiles.json")])
    assert code == 2
    assert "secret-path" in err
    assert b"sk-ant-real" not in data


@pytest.mark.parametrize(
    "name", ["auth.json", "credentials.yaml", ".env", "server.pem", "machine.key", "id_rsa"],
)
def test_every_credential_glob_is_refused(reader, name) -> None:
    mod, root = reader
    (root / name).write_text("secret\n")
    code, _data, err = _run(mod, [str(root / name)])
    assert code == 2 and "secret-path" in err


def test_a_docs_private_path_is_refused(reader) -> None:
    mod, root = reader
    priv = root / "docs" / "private"
    priv.mkdir(parents=True)
    (priv / "notes.md").write_text("x\n")
    code, _data, err = _run(mod, [str(priv / "notes.md")])
    assert code == 2 and "secret-path" in err


def test_a_directory_operand_is_refused(reader) -> None:
    mod, root = reader
    code, _data, err = _run(mod, [str(root)])
    assert code == 2
    assert "not-a-regular-file" in err


def test_a_fifo_is_refused_rather_than_hanging_root(reader) -> None:
    mod, root = reader
    fifo = root / "pipe"
    os.mkfifo(fifo)
    code, _data, err = _run(mod, [str(fifo)])
    assert code == 2
    assert "not-a-regular-file" in err


def test_a_missing_file_is_refused(reader) -> None:
    mod, root = reader
    code, _data, err = _run(mod, [str(root / "nope.json")])
    assert code == 2
    assert "no-such-file" in err


# ── The rendered artifact ────────────────────────────────────────────────────


def test_the_source_module_grants_nothing_until_it_is_rendered() -> None:
    """Fail-closed: a copy that missed the render refuses everything."""
    assert pm_probe_cat.ROOT_PATTERNS == ()
    with pytest.raises(pm_probe_cat.Refused) as exc:
        pm_probe_cat.resolve_operand("/etc/passwd")
    assert exc.value.rule == "path-outside-allowlist"


@pytest.mark.parametrize("profile", [MACOS, LINUX], ids=["macos", "linux"])
def test_render_fills_the_root_set_from_the_platform_profile(profile) -> None:
    src = pm_probe_install.render_wrapper(profile)
    mod = types.ModuleType("r")
    exec(compile(src, "r", "exec"), mod.__dict__)  # noqa: S102
    assert mod.ROOT_PATTERNS == pm_probe_install.wrapper_root_patterns(profile)
    assert mod.ROOT_PATTERNS, "the rendered reader would refuse everything"


def test_macos_roots_carry_the_private_twins() -> None:
    """`/tmp` and `/var` are symlinks into `/private` on macOS, so every
    resolved path under them arrives with that prefix — a root set without the
    twins would refuse every `/var/log` read AFTER resolution."""
    pats = pm_probe_install.wrapper_root_patterns(MACOS)
    assert any(p.startswith("/private/var/log") for p in pats)
    assert any(p.startswith("/private/tmp/pm") for p in pats)


@pytest.mark.parametrize("profile", [MACOS, LINUX], ids=["macos", "linux"])
def test_root_patterns_track_the_platform_profile(profile) -> None:
    """One home for platform divergence: the reader's roots are derived, not
    a second copy of the path table that can drift from the pod's real one."""
    pats = pm_probe_install.wrapper_root_patterns(profile)
    joined = "\n".join(pats)
    assert profile.shared_dir_default in joined
    assert profile.user_home_root in joined
    assert profile.daemon_dir in joined


def test_render_refuses_a_profile_with_a_metacharacter_path() -> None:
    """The patterns are written verbatim into a regex; a `.` or `*` in a
    profile value would silently widen a root instead of failing."""
    bad = types.SimpleNamespace(
        name="macos", shared_dir_default="/Users/Shared/evolve.d",
        user_home_root="/Users", daemon_dir="/Library/LaunchDaemons",
    )
    with pytest.raises(ValueError):
        pm_probe_install.wrapper_root_patterns(bad)


def test_the_rendered_artifact_is_the_module_plus_one_line() -> None:
    """Nothing else differs, so reviewing the module reviews what root runs."""
    src = Path(pm_probe_cat.__file__).read_text()
    rendered = pm_probe_install.render_wrapper(MACOS)
    head_src, marker, tail_src = src.partition("ROOT_PATTERNS: tuple[str, ...] = ()\n")
    assert marker, "the placeholder line moved — render_wrapper would refuse"
    head_r, marker_r, tail_r = rendered.partition("ROOT_PATTERNS: tuple[str, ...] = (\n")
    assert marker_r
    close = tail_r.index(")\n")
    assert head_src == head_r
    assert tail_src == tail_r[close + 2:]


# ── Round two: the pod's interpreter, and hard links ─────────────────────────
#
# Both items of the 2026-09-09 Hold on PR #4079. They are tested here rather
# than in a new file because the contract they belong to is this one.


def test_a_hard_link_is_refused_even_though_nothing_is_a_symlink(reader, tmp_path):
    """The channel the symlink refusals do NOT close.

    A symlink refusal stops a bot-writable tree from POINTING at a root-only
    file. It does nothing about a second NAME for that file's inode: `ln`
    creates one inside the allowlisted tree with no link to follow, so
    `realpath` resolves to a path under the root set and the `O_NOFOLLOW` walk
    finds nothing to refuse. Every earlier check passes and root reads the file.
    """
    mod, root = reader
    secret = tmp_path / "root-only.txt"
    secret.write_text("the thing the bot may not read\n")
    planted = root / "innocent.conf"
    os.link(secret, planted)  # a real hard link, not a symlink

    assert not planted.is_symlink(), "precondition: nothing here is a symlink"
    assert planted.stat().st_ino == secret.stat().st_ino, "precondition: same inode"

    code, data, err = _run(mod, [str(planted)])
    assert code == 2, f"the hard link was READ: {data!r}"
    assert "hardlink" in err
    assert data == b"", "a refusal must not emit the file's bytes"


def test_a_single_linked_file_in_the_same_tree_still_reads(reader):
    """The positive control for the check above.

    A refusal test whose file simply cannot be read proves nothing, so this
    asserts the ordinary path still works: same tree, same reader, one link.
    """
    mod, root = reader
    ordinary = root / "ordinary.conf"
    ordinary.write_text("readable\n")
    assert ordinary.stat().st_nlink == 1

    code, data, err = _run(mod, [str(ordinary)])
    assert code == 0, err
    assert data == b"readable\n"


def test_resolution_is_not_a_python_310_api(reader):
    """Item 1, pinned where it can be read rather than only where it runs.

    `os.path.realpath(..., strict=True)` is 3.10+. Under this file's own
    `#!/usr/bin/python3` shebang the pod runs the macOS Command Line Tools
    interpreter — 3.9.6 — so the keyword raised `TypeError` before the reader
    refused anything: the privileged half of the door did not execute at all on
    the one machine the sudoers grant exists for.
    """
    import re

    src = pm_probe_install.render_wrapper(MACOS)
    # The CALL, not the string: the module explains in a comment why the keyword
    # is gone, and a bare substring match reds on the explanation. Pinning the
    # call site is also the thing that would actually break the pod.
    assert not re.search(r"realpath\([^)]*strict\s*=", src), (
        "strict= is a 3.10+ keyword of os.path.realpath and the pod runs 3.9.6"
    )
    assert src.startswith("#!/usr/bin/python3 -I\n"), (
        "the reader runs as root: -I keeps PYTHON* env vars and the user site "
        "directory out of a root interpreter"
    )


def test_a_missing_path_is_still_refused_without_strict(reader):
    """What `strict=True` was doing, still done.

    Non-strict `realpath` returns a path for something that does not exist, so
    dropping the keyword without replacing its effect would have turned a
    refusal into an `open` attempt. The explicit existence check is what keeps
    `no-such-file` a refusal.
    """
    mod, root = reader
    code, data, err = _run(mod, [str(root / "not-here.conf")])
    assert code == 2
    assert "no-such-file" in err
    assert data == b""


_POD_PYTHON = "/usr/bin/python3"


@pytest.mark.skipif(
    not os.path.exists(_POD_PYTHON),
    reason=f"{_POD_PYTHON} is absent — the pod's interpreter is not here to test against",
)
def test_the_rendered_artifact_executes_under_the_pod_interpreter(tmp_path):
    """The evidence the Hold asked for, and the reason a unit test was not it.

    The defect was that the INSTALLED file failed under the POD's interpreter.
    A test against the source module runs on whatever Python pytest is using —
    3.10+ here — and would have stayed green through the entire outage. So this
    writes the rendered artifact to disk, marks it executable, and runs it
    through `/usr/bin/python3` as a subprocess, exactly as sudo does.

    It SKIPS loudly when that interpreter is absent rather than passing by
    default: a test that silently passes where it cannot check anything is the
    shape this whole PR is about.
    """
    import subprocess

    pod = tmp_path / "pod"
    pod.mkdir()
    real = os.path.realpath(str(pod))
    target = pod / "hello.conf"
    target.write_text("rendered-and-run\n")

    src = pm_probe_install.render_wrapper(MACOS).replace(
        "ROOT_PATTERNS: tuple[str, ...] = (",
        f"ROOT_PATTERNS: tuple[str, ...] = (\n    r'{real}(?:/.*)?',",
        1,
    )
    artifact = tmp_path / "pm-probe-cat"
    artifact.write_text(src)
    artifact.chmod(0o755)

    proc = subprocess.run(
        [_POD_PYTHON, str(artifact), str(target)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (
        f"the rendered artifact failed under {_POD_PYTHON} "
        f"({sys.version_info.major}.{sys.version_info.minor} is what pytest runs): "
        f"{proc.stderr!r}"
    )
    assert proc.stdout == "rendered-and-run\n"

    # And the refusals still refuse under that interpreter — a reader that runs
    # but has stopped refusing is worse than one that tracebacks.
    outside = subprocess.run(
        [_POD_PYTHON, str(artifact), "/etc/hosts"],
        capture_output=True, text=True, timeout=30,
    )
    assert outside.returncode == 2
    assert "path-outside-allowlist" in outside.stderr
