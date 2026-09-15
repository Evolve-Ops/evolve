"""Unit tests for tools/meta-dispatch-move.

The PM lane's one lane-state mutator (internal/meta-dispatch-procedure.md steps 1 and 4b).
It exists because raw `git mv` **fails outright on an untracked brief**, and a brief the PM
just wrote is normally untracked — on 2026-08-25 that burned one prepared chip per
30-minute tick, and the `git rm -f` on the other end destroyed a queued brief outright
(`alpha-7-price-from-catalog.md`, unrecoverable: no git object, no Trash entry).

So the two directions this file pins hardest are the two that cost something:
  * `launch` must work whether or not the brief is tracked, and report which path it took;
  * `done` must REFUSE to unlink an untracked marker whose `done/<id>.md` has not landed,
    because for an untracked file there is nothing to restore from.

The tool is an extensionless script under tools/, so we load it by path.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[3] / "tools" / "meta-dispatch-move"


def _load_tool():
    loader = importlib.machinery.SourceFileLoader("meta_dispatch_move", str(_TOOL))
    spec = importlib.util.spec_from_loader("meta_dispatch_move", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meta_dispatch_move"] = mod
    loader.exec_module(mod)
    return mod


MOD = _load_tool()


@pytest.fixture(autouse=True)
def _allow_tmp_write_dirs(monkeypatch):
    """Let this file's CLI tests point `--state-dir` / `--log-dir` at `tmp_path`.

    The granted CLI refuses either flag outside `~/.claude/` (see `check_write_root`);
    this env var is that refusal's one test-only escape hatch, and it is autouse because
    almost every test here writes to a tmp dir. The three tests that exercise the check
    itself — two refusals and the tilde expansion — `monkeypatch.delenv` it first, so the
    check runs for real rather than being permanently disabled for the suite.
    """
    monkeypatch.setenv(MOD.DIR_ESCAPE_ENV, "1")


BRIEF = """---
id: {id}
aspect: apps
title: "A brief"
privileged: false
created: 2026-08-23
pm: fable-cowork
---
WHY: pinned by tests.
"""


def _lane(tmp_path: Path) -> Path:
    root = tmp_path / "dispatch"
    for sub in ("queued", "inflight", "done"):
        (root / sub).mkdir(parents=True)
    return root


def _write(root: Path, sub: str, brief_id: str, body: str | None = None) -> Path:
    p = root / sub / ("%s.md" % brief_id)
    p.write_text(body if body is not None else BRIEF.format(id=brief_id))
    return p


def _git_repo(tmp_path: Path) -> Path:
    """A real repo — `git mv` / `ls-files` semantics are the thing under test, so
    stubbing git would pin the stub instead of the behaviour that broke."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(tmp_path), "config", k, v], check=True)
    return tmp_path


# ── launch ───────────────────────────────────────────────────────────────────


def test_launch_moves_untracked_brief_via_plain_move(tmp_path):
    """The regression that burned two chips: untracked is the NORMAL case."""
    root = _lane(tmp_path)
    src = _write(root, "queued", "some-brief")
    out = MOD.launch(root, "some-brief")
    assert out["method"] == "copy-verify-unlink"
    assert not src.exists()
    dst = root / "inflight" / "some-brief.md"
    assert dst.is_file() and "WHY: pinned by tests." in dst.read_text()


def test_launch_uses_git_mv_when_the_brief_is_tracked(tmp_path):
    repo = _git_repo(tmp_path)
    root = _lane(repo)
    _write(root, "queued", "tracked-brief")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add"], check=True)

    out = MOD.launch(root, "tracked-brief")
    assert out["method"] == "copy-verify-git-rm"
    assert (root / "inflight" / "tracked-brief.md").is_file()
    # the index moved too — otherwise step 1's later `git rm` would fail
    staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only"],
                            capture_output=True, text=True).stdout
    assert "inflight/tracked-brief.md" in staged


def test_launch_uses_git_mv_when_invoked_from_another_cwd(tmp_path, monkeypatch):
    """The path-resolution regression: git runs with cwd=<repo root>, the caller's paths
    come from --dir relative to the PROCESS cwd. When they differ, a tracked brief read as
    UNTRACKED and `launch` fell through to the plain move — leaving the index holding a
    phantom deletion at the old path. Assert on `method`, because the move itself succeeds
    either way; only the index tells you which path was taken."""
    repo = _git_repo(tmp_path)
    root = _lane(repo)
    _write(root, "queued", "elsewhere-brief")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add"], check=True)

    deep = repo / "sub" / "deeper"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    relative_root = Path("..") / ".." / "dispatch"
    assert not relative_root.is_absolute()

    out = MOD.launch(relative_root, "elsewhere-brief")
    # was the untracked path before the fix — assert on `method`, not on the move
    assert out["method"] == "copy-verify-git-rm"
    staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only"],
                            capture_output=True, text=True).stdout
    assert "inflight/elsewhere-brief.md" in staged
    # and the old path is not left dangling as a DELETION with the new one untracked.
    #
    # Stated as "the deletion is ACCOMPANIED", not as "no D line appears", because those
    # are not the same claim and only the first is the defect. git collapses the staged
    # pair into one `R` line when it detects a rename, and rename detection is
    # SIMILARITY-based (50% by default) — so on a 9-line fixture brief the front matter
    # `launch` legitimately writes (`body_sha256`, and since 2026-09-07 `dispatched` +
    # `dispatched_at`) is enough to push it under the threshold and split the same
    # staged pair into `A ` + `D `. That is a fixture-scale artifact, not a lane one: a
    # real brief is 40-90 body lines and three added front-matter lines move its
    # similarity by ~1%. The phantom this test was written for is a deletion with the
    # new path UNTRACKED (`??`) or absent from the index entirely, and both readings
    # below still catch it.
    status = _porcelain(repo)
    lines = status.splitlines()
    old_gone = [ln for ln in lines
                if ln[:2] in (" D", "D ") and "queued/elsewhere-brief.md" in ln]
    if old_gone:
        assert any(ln[:2] in ("A ", "AM", "M ") and "inflight/elsewhere-brief.md" in ln
                   for ln in lines), status
    assert not any(ln.startswith("??") and "inflight/elsewhere-brief.md" in ln
                   for ln in lines), status


def test_done_uses_git_rm_when_invoked_from_another_cwd(tmp_path, monkeypatch):
    """Same resolution bug on the other verb: a tracked marker misread as untracked would
    hit the durable-copy guard and REFUSE — safe, but wrong, and it strands the lane."""
    repo = _git_repo(tmp_path)
    root = _lane(repo)
    _write(root, "inflight", "clearable-brief")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add"], check=True)

    deep = repo / "sub" / "deeper"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    out = MOD.done(Path("..") / ".." / "dispatch", "clearable-brief")
    assert out["method"] == "git-rm"          # was a Refused before the fix
    assert not (root / "inflight" / "clearable-brief.md").exists()


def _porcelain(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                          capture_output=True, text=True).stdout


def test_launch_refuses_when_destination_exists(tmp_path):
    root = _lane(tmp_path)
    _write(root, "queued", "dupe")
    _write(root, "inflight", "dupe")
    with pytest.raises(MOD.Refused, match="already exists"):
        MOD.launch(root, "dupe")
    assert (root / "queued" / "dupe.md").is_file()   # source untouched


def test_launch_refuses_missing_brief(tmp_path):
    with pytest.raises(MOD.Refused, match="no queued brief"):
        MOD.launch(_lane(tmp_path), "nope")


@pytest.mark.parametrize("bad", ["Not-Kebab", "trailing-", "has_underscore", ""])
def test_launch_refuses_non_kebab_id(tmp_path, bad):
    with pytest.raises(MOD.Refused, match="kebab"):
        MOD.launch(_lane(tmp_path), bad)


def test_launch_refuses_when_front_matter_id_disagrees_with_stem(tmp_path):
    """The lane schema requires id == stem; moving a mismatch would land an entry the
    eligibility helper holds as malformed."""
    root = _lane(tmp_path)
    _write(root, "queued", "stem-name", body=BRIEF.format(id="different-id"))
    with pytest.raises(MOD.Refused, match="!= filename stem"):
        MOD.launch(root, "stem-name")
    assert (root / "queued" / "stem-name.md").is_file()


# ── done ─────────────────────────────────────────────────────────────────────


def test_done_refuses_to_delete_untracked_marker_without_durable_copy(tmp_path):
    """THE data-loss guard. An untracked brief has no git object and no Trash entry, so
    an unguarded unlink is unrecoverable — this is exactly how alpha-7 was destroyed."""
    root = _lane(tmp_path)
    marker = _write(root, "inflight", "only-copy")
    with pytest.raises(MOD.Refused, match="only copy"):
        MOD.done(root, "only-copy")
    assert marker.is_file(), "refusal must not delete anything"


def test_done_clears_untracked_marker_once_done_copy_exists(tmp_path):
    root = _lane(tmp_path)
    _write(root, "inflight", "carried")
    _write(root, "done", "carried")
    out = MOD.done(root, "carried")
    assert out["method"] == "plain"
    assert not (root / "inflight" / "carried.md").exists()
    assert (root / "done" / "carried.md").is_file()


def test_done_force_overrides_the_guard(tmp_path):
    root = _lane(tmp_path)
    _write(root, "inflight", "sacrificial")
    out = MOD.done(root, "sacrificial", force=True)
    assert out["ok"] and not (root / "inflight" / "sacrificial.md").exists()


def test_done_uses_git_rm_when_tracked_and_needs_no_durable_copy(tmp_path):
    """A tracked marker keeps its blob in history, so the guard is deliberately scoped
    to the untracked case and must NOT block the normal merged-chip cleanup."""
    repo = _git_repo(tmp_path)
    root = _lane(repo)
    _write(root, "inflight", "merged-chip")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add"], check=True)

    out = MOD.done(root, "merged-chip")           # no done/ copy present
    assert out["method"] == "git-rm"
    assert not (root / "inflight" / "merged-chip.md").exists()


def test_done_is_idempotent_when_marker_already_absent(tmp_path):
    """Step 1 re-runs every tick until the operator pulls; a second call must not error."""
    out = MOD.done(_lane(tmp_path), "gone-already")
    assert out["ok"] and out["method"] == "already-absent"


# ── CLI surface ──────────────────────────────────────────────────────────────


def test_cli_launch_exits_zero_and_emits_json(tmp_path, capsys):
    root = _lane(tmp_path)
    _write(root, "queued", "cli-brief")
    rc = MOD.main(["launch", "cli-brief", "--dir", str(root), "--json"])
    assert rc == 0
    import json as _json
    assert _json.loads(capsys.readouterr().out)["method"] == "copy-verify-unlink"


def test_cli_refusal_exits_two_and_changes_nothing(tmp_path, capsys):
    root = _lane(tmp_path)
    marker = _write(root, "inflight", "guarded")
    rc = MOD.main(["done", "guarded", "--dir", str(root)])
    assert rc == 2
    assert "only copy" in capsys.readouterr().err
    assert marker.is_file()


def test_cli_rejects_missing_lane_dir(tmp_path, capsys):
    rc = MOD.main(["launch", "x", "--dir", str(tmp_path / "nope")])
    assert rc == 2
    assert "no lane dir" in capsys.readouterr().err


# ── heartbeat ────────────────────────────────────────────────────────────────
#
# The other way this lane lost data: on 2026-08-27 a run replaced
# log/2026-08-26.jsonl with a one-line whole-file `Write` and destroyed three
# heartbeats. So what these tests pin is not "a line gets written" but "nothing
# already written can be lost", plus the clock the run does not otherwise have.

import datetime  # noqa: E402
import inspect  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402


def _utc(y, mo, d, h=0, mi=0, s=0):
    return datetime.datetime(y, mo, d, h, mi, s, tzinfo=datetime.timezone.utc)


def _lines(path: Path):
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_n_invocations_append_n_lines_and_never_touch_prior_bytes(tmp_path):
    """THE regression. Each call must EXTEND the file: every byte written by an earlier
    call is still there, at the same offset, afterwards."""
    log = tmp_path / "log"
    path = log / "2026-08-26.jsonl"
    prefixes = []
    for i in range(5):
        before = path.read_bytes() if path.exists() else b""
        prefixes.append(before)
        MOD.heartbeat(log, counts={"prepared": i}, now=_utc(2026, 8, 26, 12, i))
        after = path.read_bytes()
        assert after.startswith(before), "call %d shortened or rewrote the file" % i
        assert len(after) > len(before)

    recs = _lines(path)
    assert [r["prepared"] for r in recs] == [0, 1, 2, 3, 4]
    assert path.read_bytes().startswith(prefixes[-1])


def test_two_concurrent_writers_both_land_one_whole_line_each(tmp_path):
    """Two runs fired two minutes apart on 2026-08-26 (04:29:34Z / 04:31:26Z), so the fix
    has to survive real concurrency, not merely sequential calls. Separate PROCESSES —
    threads in one interpreter would share a file object and prove nothing about O_APPEND.
    """
    log = tmp_path / "log"
    log.mkdir()
    procs = [
        subprocess.Popen(
            [sys.executable, str(_TOOL), "heartbeat", "--log-dir", str(log),
             "--note", "writer-%d-%s" % (i, "x" * 200), "--prepared", str(i)],
            stdout=subprocess.DEVNULL)
        for i in range(8)
    ]
    for p in procs:
        assert p.wait() == 0

    written = sorted(log.glob("*.jsonl"))
    assert len(written) == 1, written
    raw = written[0].read_text()
    assert raw.endswith("\n")
    lines = raw.splitlines()
    assert len(lines) == 8
    notes = set()
    for ln in lines:
        rec = json.loads(ln)          # a spliced line would not parse at all
        notes.add(rec["note"])
    assert notes == {"writer-%d-%s" % (i, "x" * 200) for i in range(8)}


def test_the_filename_tracks_the_utc_date_across_midnight(tmp_path):
    """The old log was sharded on a guessed date: runs at 04:29/04:31/05:03Z on 08-26 wrote
    into 2026-08-25.jsonl. The clock is FROZEN here — coupling this to the wall clock would
    make it a test that passes 23 hours a day."""
    log = tmp_path / "log"
    before = MOD.heartbeat(log, now=_utc(2026, 8, 26, 23, 59, 59))
    after = MOD.heartbeat(log, now=_utc(2026, 8, 27, 0, 0, 1))

    assert Path(before["path"]).name == "2026-08-26.jsonl"
    assert Path(after["path"]).name == "2026-08-27.jsonl"
    # the stamp and the shard key come from the same clock, so they always agree
    assert before["record"]["run"] == "2026-08-26T23:59:59Z"
    assert after["record"]["run"] == "2026-08-27T00:00:01Z"


def test_a_local_time_stamp_is_converted_not_relabelled(tmp_path):
    """`18:56:00Z` in the old log was 18:55 LOCAL wearing a Z. A tz-aware non-UTC stamp
    must be converted; a naive one is refused rather than assumed to be UTC."""
    log = tmp_path / "log"
    minus7 = datetime.timezone(datetime.timedelta(hours=-7))
    out = MOD.heartbeat(log, now=datetime.datetime(2026, 8, 26, 18, 55, 28, tzinfo=minus7))
    assert out["record"]["run"] == "2026-08-27T01:55:28Z"
    assert Path(out["path"]).name == "2026-08-27.jsonl"

    with pytest.raises(MOD.Refused, match="timezone-aware"):
        MOD.heartbeat(log, now=datetime.datetime(2026, 8, 26, 18, 55, 28))


def test_heartbeat_creates_the_log_dir_when_absent(tmp_path):
    out = MOD.heartbeat(tmp_path / "deep" / "log", now=_utc(2026, 8, 27, 6, 0))
    assert Path(out["path"]).is_file()


def test_the_default_stamp_is_utc_now(tmp_path):
    """No `now` passed = the tool's own clock, which is the case the unattended run hits."""
    out = MOD.heartbeat(tmp_path / "log")
    stamped = datetime.datetime.strptime(out["record"]["run"], "%Y-%m-%dT%H:%M:%SZ")
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    assert abs((now - stamped).total_seconds()) < 120
    assert Path(out["path"]).name == now.strftime("%Y-%m-%d") + ".jsonl"


def test_json_output_matches_the_line_actually_appended(tmp_path):
    log = tmp_path / "log"
    rc = MOD.main(["heartbeat", "--log-dir", str(log), "--prepared", "1", "--reconciled",
                   "2", "--held", "3", "--invalid", "4", "--paused", "--json"])
    assert rc == 0
    out = json.loads(capsys_out())
    on_disk = Path(out["path"]).read_text()
    assert on_disk == out["line"] + "\n"
    assert json.loads(on_disk) == out["record"]
    assert out["record"]["prepared"] == 1 and out["record"]["reconciled"] == 2
    assert out["record"]["held"] == 3 and out["record"]["invalid"] == 4
    assert out["record"]["paused"] is True


_CAPSYS = {}


def capsys_out():
    return _CAPSYS["capsys"].readouterr().out


@pytest.fixture(autouse=True)
def _bind_capsys(capsys):
    _CAPSYS["capsys"] = capsys
    yield


def test_a_heartbeat_carries_the_counts_and_nothing_invented(tmp_path):
    out = MOD.heartbeat(tmp_path / "log", now=_utc(2026, 8, 27, 7, 30))
    assert out["record"] == {"run": "2026-08-27T07:30:00Z", "prepared": 0,
                             "reconciled": 0, "held": 0, "invalid": 0, "paused": False}


# ── --started / elapsed_s ────────────────────────────────────────────────────
# elapsed_s exists to tell two causes of a log gap apart. Measured 2026-08-29:
# heartbeats arrive in PAIRS 1.5-4 min apart (a real tick's cost) separated by holes
# of 207-681 min in which no tick ran at all. The hours-long "runs" that first looked
# like overruns were scheduled sessions the operator adopted and used interactively.
# So a small elapsed_s beside a gap means ticks were not RUNNING; a large one would
# mean a genuine overrun. What these tests pin is the fail-safe half: a bad --started
# costs the FIELD, never the LINE.


def test_started_adds_elapsed_and_echoes_the_raw_input(tmp_path):
    out = MOD.heartbeat(tmp_path / "log", started="2026-08-29T05:11:22Z",
                        now=_utc(2026, 8, 29, 5, 43, 43))
    assert out["record"]["elapsed_s"] == 1941          # 32m21s
    assert out["record"]["started"] == "2026-08-29T05:11:22Z"


def test_started_accepts_an_offset_and_normalizes_it_to_utc(tmp_path):
    out = MOD.heartbeat(tmp_path / "log", started="2026-08-28T22:11:22-07:00",
                        now=_utc(2026, 8, 29, 5, 43, 43))
    assert out["record"]["elapsed_s"] == 1941
    assert out["record"]["started"] == "2026-08-29T05:11:22Z"          # same instant


@pytest.mark.parametrize("bad", [
    "not-a-timestamp",
    "2026-08-29T05:11:22",        # naive — how local time wore a Z in the old log
    "2026-08-29T06:00:00Z",       # AFTER the run finished: would be a fiction
    "",
])
def test_an_unusable_started_costs_the_field_never_the_line(tmp_path, bad):
    log = tmp_path / "log"
    out = MOD.heartbeat(log, counts={"prepared": 1}, started=bad,
                        now=_utc(2026, 8, 29, 5, 43, 43))
    assert "elapsed_s" not in out["record"] and "started" not in out["record"]
    assert out["record"]["prepared"] == 1              # the heartbeat still landed
    written = (log / "2026-08-29.jsonl").read_text().splitlines()
    assert len(written) == 1 and json.loads(written[0])["run"] == "2026-08-29T05:43:43Z"


def test_started_survives_the_cli_and_a_bad_one_still_exits_zero(tmp_path):
    log = tmp_path / "log"
    assert MOD.main(["heartbeat", "--log-dir", str(log),
                     "--started", "2026-08-29T05:11:22Z"]) == 0
    assert MOD.main(["heartbeat", "--log-dir", str(log), "--started", "garbage"]) == 0
    lines = [json.loads(x) for x in
             sorted(log.glob("*.jsonl"))[0].read_text().splitlines()]
    assert len(lines) == 2                             # both runs kept their heartbeat
    assert "elapsed_s" in lines[0] and "elapsed_s" not in lines[1]


def test_a_note_is_optional_and_never_splits_a_record(tmp_path):
    log = tmp_path / "log"
    out = MOD.heartbeat(log, note="loss window\n2026-08-26", now=_utc(2026, 8, 27, 8, 0))
    raw = Path(out["path"]).read_text()
    assert raw.count("\n") == 1, "a newline inside the note must be escaped, not emitted"
    assert json.loads(raw)["note"] == "loss window\n2026-08-26"


@pytest.mark.parametrize("bad", [-1, True, "3", None])
def test_negative_or_non_int_counts_are_refused(tmp_path, bad):
    with pytest.raises(MOD.Refused, match="non-negative integer"):
        MOD.heartbeat(tmp_path / "log", counts={"prepared": bad},
                      now=_utc(2026, 8, 27, 9, 0))


def test_an_empty_note_is_refused_rather_than_logged_blank(tmp_path):
    with pytest.raises(MOD.Refused, match="empty"):
        MOD.heartbeat(tmp_path / "log", note="   ", now=_utc(2026, 8, 27, 9, 0))


def test_a_refusal_writes_nothing_at_all(tmp_path):
    log = tmp_path / "log"
    MOD.heartbeat(log, now=_utc(2026, 8, 27, 9, 0))
    path = log / "2026-08-27.jsonl"
    before = path.read_bytes()
    with pytest.raises(MOD.Refused):
        MOD.heartbeat(log, counts={"held": -4}, now=_utc(2026, 8, 27, 9, 30))
    assert path.read_bytes() == before


def test_heartbeat_has_no_truncating_code_path(tmp_path):
    """A structural guard, because the failure this fixes was a TRUNCATING writer that
    looked correct. The function must open O_APPEND and must never truncate or read."""
    src = inspect.getsource(MOD.heartbeat)
    code = src.replace(MOD.heartbeat.__doc__, "")     # the prose may DISCUSS O_TRUNC
    assert "os.O_APPEND" in code
    assert "O_TRUNC" not in code
    assert "read_text" not in code and "read_bytes" not in code
    assert "open(" not in code.replace("os.open(", "")   # no builtin open(..., "w")


def test_heartbeat_needs_no_lane_dir(tmp_path, monkeypatch):
    """The run logs a heartbeat even when it never touched the lane, and the CLI's
    lane-dir precondition must not stand between it and that line."""
    monkeypatch.chdir(tmp_path)                # no internal/dispatch here
    rc = MOD.main(["heartbeat", "--log-dir", str(tmp_path / "log")])
    assert rc == 0
    assert "appended:" in capsys_out()


def test_the_log_line_is_readable_by_the_operator_only(tmp_path):
    """Consistent with the rest of ~/.claude — the lane's own state, not world state."""
    out = MOD.heartbeat(tmp_path / "log", now=_utc(2026, 8, 27, 10, 0))
    assert (os.stat(out["path"]).st_mode & 0o077) == 0


# ── copy-verify-delete ───────────────────────────────────────────────────────
#
# The other half of the 2026-08-25 loss. `alpha-7-price-from-catalog.md` was moved by prose
# a model executed — read, write, done — and the write dropped the body, with nothing
# between it and the delete that could have noticed. So: write the copy, RE-READ IT FROM
# DISK, re-hash it, and only then remove the source. Any failure in between rolls back.

import sys as _sys                                          # noqa: E402
_sys.path.insert(0, str(_TOOL.parent))
import meta_dispatch_integrity as mdi                       # noqa: E402

STAMPED = """---
id: {id}
aspect: apps
title: "A brief"
privileged: false
created: 2026-08-23
pm: fable-cowork
body_sha256: {digest}
---
WHY: pinned by tests.
"""


def _stamped_brief(brief_id, body="WHY: pinned by tests."):
    return STAMPED.format(id=brief_id, digest=mdi.body_digest(body))


def test_launch_stamps_a_body_hash_when_the_pm_wrote_none(tmp_path):
    """The first transition is the last honest moment to record what the body WAS: a
    partial truncation with no recorded hash is undetectable in principle."""
    root = _lane(tmp_path)
    _write(root, "queued", "unstamped")
    out = MOD.launch(root, "unstamped")
    assert out["stamped"] is True
    moved = (root / "inflight" / "unstamped.md").read_text()
    assert "body_sha256: %s" % out["body_sha256"] in moved
    assert mdi.check(moved).ok


def test_launch_preserves_a_hash_the_pm_wrote_and_does_not_restamp(tmp_path):
    root = _lane(tmp_path)
    _write(root, "queued", "prestamped", body=_stamped_brief("prestamped"))
    out = MOD.launch(root, "prestamped")
    assert out["stamped"] is False
    assert (root / "inflight" / "prestamped.md").read_text().count("body_sha256") == 1


def test_launch_refuses_a_truncated_brief_and_changes_nothing(tmp_path):
    """STOP that entry — never proceed with a truncated brief, and never move it either:
    a move would put the damage one dir further from where it can be restored."""
    root = _lane(tmp_path)
    text = _stamped_brief("damaged").replace("WHY: pinned by tests.", "WHY: pinned")
    src = _write(root, "queued", "damaged", body=text)
    before = src.read_bytes()
    with pytest.raises(MOD.Refused) as e:
        MOD.launch(root, "damaged")
    assert "integrity" in str(e.value) and "STOP this entry" in str(e.value)
    assert src.read_bytes() == before
    assert not (root / "inflight" / "damaged.md").exists()


def test_launch_refuses_a_brief_whose_body_is_gone(tmp_path):
    root = _lane(tmp_path)
    _write(root, "queued", "hollow", body=BRIEF.format(id="hollow").replace(
        "WHY: pinned by tests.\n", ""))
    with pytest.raises(MOD.Refused) as e:
        MOD.launch(root, "hollow")
    assert "only the front matter survived" in str(e.value)


def test_launch_refuses_when_the_id_already_exists_in_another_lane_dir(tmp_path):
    """The mover must not CREATE the ambiguity that meta-dispatch-eligible reports."""
    root = _lane(tmp_path)
    _write(root, "queued", "twin")
    _write(root, "done", "twin")
    with pytest.raises(MOD.Refused) as e:
        MOD.launch(root, "twin")
    assert "two lane dirs" in str(e.value)
    assert (root / "queued" / "twin.md").is_file()


def test_a_failed_verify_removes_the_copy_and_leaves_the_source_intact(monkeypatch,
                                                                       tmp_path):
    """The one path that matters: the bytes that land on disk are not the bytes we wrote.
    The read-back must catch it while the source is still there — because for an untracked
    brief the source is the only copy that exists anywhere.

    Injected by truncating the destination immediately after it is put in place, which is
    the alpha-7 shape (front matter survived, body did not) rather than a stubbed hash.
    """
    root = _lane(tmp_path)
    src = _write(root, "queued", "unlucky")
    before = src.read_bytes()

    real_replace = MOD.os.replace

    def replace_then_truncate(a, b):
        real_replace(a, b)
        text = Path(b).read_text()
        Path(b).write_text(text[:text.index("---", 3) + 4])   # keep only the front matter

    monkeypatch.setattr(MOD.os, "replace", replace_then_truncate)
    with pytest.raises(MOD.Refused) as e:
        MOD.launch(root, "unlucky")
    assert "copy verification" in str(e.value)
    assert src.read_bytes() == before
    assert not (root / "inflight" / "unlucky.md").exists()


def test_a_short_write_never_reaches_the_destination(monkeypatch, tmp_path):
    """Caught while still staged in the `.part` file, so the destination is never even
    created — one step earlier than the read-back, and it too leaves the source alone."""
    root = _lane(tmp_path)
    src = _write(root, "queued", "clipped")
    before = src.read_bytes()

    real_write = MOD.os.write
    monkeypatch.setattr(MOD.os, "write", lambda fd, data: real_write(fd, data[:10]))
    with pytest.raises(MOD.Refused) as e:
        MOD.launch(root, "clipped")
    assert "short write" in str(e.value)
    assert src.read_bytes() == before
    assert not (root / "inflight" / "clipped.md").exists()
    assert list((root / "inflight").iterdir()) == []


def test_a_failed_source_delete_rolls_the_copy_back(monkeypatch, tmp_path):
    """`git rm` failing after a verified copy must not leave the brief in both dirs — the
    copy carried nothing the source does not, so removing it is a clean rollback."""
    repo = _git_repo(tmp_path)
    root = _lane(repo)
    src = _write(root, "queued", "stubborn")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add"], check=True)

    real_run = MOD._run

    def refuse_rm(args, cwd):
        if args[:2] == ["git", "rm"]:
            return subprocess.CompletedProcess(args, 1, "", "fatal: nope")
        return real_run(args, cwd)

    monkeypatch.setattr(MOD, "_run", refuse_rm)
    with pytest.raises(MOD.Refused) as e:
        MOD.launch(root, "stubborn")
    assert "git rm failed" in str(e.value)
    assert src.is_file()
    assert not (root / "inflight" / "stubborn.md").exists()


def test_launch_leaves_no_part_file_behind(tmp_path):
    root = _lane(tmp_path)
    _write(root, "queued", "tidy")
    MOD.launch(root, "tidy")
    assert [p.name for p in (root / "inflight").iterdir()] == ["tidy.md"]


def test_launch_refuses_bytes_that_are_not_utf8(tmp_path):
    """`errors="replace"` would substitute U+FFFD and copy the damage onward as if fine."""
    root = _lane(tmp_path)
    (root / "queued" / "binary.md").write_bytes(b"---\nid: binary\n---\n\xff\xfe body\n")
    with pytest.raises(MOD.Refused) as e:
        MOD.launch(root, "binary")
    assert "not valid UTF-8" in str(e.value)


# ── done: the durable copy must actually carry the brief ─────────────────────


def test_done_refuses_when_the_durable_copy_lost_the_body(tmp_path):
    """Existence was the old guard, and existence is not the property that matters: a
    `done/` entry that lost its body during the chip's own move satisfies it."""
    root = _lane(tmp_path)
    marker = _write(root, "inflight", "shipped")
    _write(root, "done", "shipped",
           body=BRIEF.format(id="shipped").replace("WHY: pinned by tests.\n", ""))
    with pytest.raises(MOD.Refused) as e:
        MOD.done(root, "shipped")
    assert "does not contain the brief body" in str(e.value)
    assert marker.is_file()


def test_done_allows_a_durable_copy_that_appends_an_outcome_note(tmp_path):
    """Containment, not equality: `ai-opt-bot-tabs-regression.md` carries an OPERATOR
    CONFIRMATION line and `alpha-6-…` an outcome note, both appended after the brief."""
    root = _lane(tmp_path)
    _write(root, "inflight", "noted")
    _write(root, "done", "noted",
           body=BRIEF.format(id="noted") + "\nOPERATOR CONFIRMATION 2026-08-27: shipped.\n")
    assert MOD.done(root, "noted")["ok"] is True
    assert not (root / "inflight" / "noted.md").exists()


def test_done_force_overrides_the_containment_guard(tmp_path):
    root = _lane(tmp_path)
    _write(root, "inflight", "forced")
    _write(root, "done", "forced", body=BRIEF.format(id="forced").replace(
        "WHY: pinned by tests.", "WHY: rewritten entirely."))
    with pytest.raises(MOD.Refused):
        MOD.done(root, "forced")
    assert MOD.done(root, "forced", force=True)["ok"] is True


# ── abandon: the last prose-move in the procedure, made executable ───────────


def test_abandon_moves_the_entry_and_records_the_outcome(tmp_path):
    """Procedure step 1's closed-unmerged branch used to say "write the entry into done/
    yourself" — a read-then-write file move performed as prose, on the one file with no
    other copy. Same shape as the loss it was left standing next to."""
    root = _lane(tmp_path)
    src = _write(root, "inflight", "dropped")
    out = MOD.abandon(root, "dropped", pr=3900)
    assert out["ok"] and not src.exists()
    text = (root / "done" / "dropped.md").read_text()
    assert "outcome: abandoned" in text and "pr: 3900" in text
    assert "WHY: pinned by tests." in text
    assert mdi.check(text).ok, "the outcome fields must not disturb the body hash"


def test_abandon_refuses_a_truncated_entry(tmp_path):
    root = _lane(tmp_path)
    text = _stamped_brief("wrecked").replace("WHY: pinned by tests.", "WHY:")
    _write(root, "inflight", "wrecked", body=text)
    with pytest.raises(MOD.Refused):
        MOD.abandon(root, "wrecked")
    assert not (root / "done" / "wrecked.md").exists()


def test_abandon_refuses_when_done_already_holds_the_id(tmp_path):
    root = _lane(tmp_path)
    _write(root, "inflight", "twin")
    _write(root, "done", "twin")
    with pytest.raises(MOD.Refused):
        MOD.abandon(root, "twin")


# ── verify: the read-only gate ───────────────────────────────────────────────


def test_verify_reports_an_intact_entry_and_touches_nothing(tmp_path):
    root = _lane(tmp_path)
    p = _write(root, "queued", "checkme")
    before = p.read_bytes()
    out = MOD.verify(root, "checkme")
    assert out["ok"] and out["state"] == "queued" and out["recorded"] is None
    assert out["computed"] == mdi.body_digest("WHY: pinned by tests.")
    assert p.read_bytes() == before


def test_verify_refuses_an_id_present_in_two_lane_dirs(tmp_path):
    root = _lane(tmp_path)
    _write(root, "inflight", "twin")
    _write(root, "done", "twin")
    with pytest.raises(MOD.Refused) as e:
        MOD.verify(root, "twin")
    assert "ambiguous" in str(e.value)


def test_verify_cli_exits_two_on_a_damaged_brief(tmp_path, capsys):
    root = _lane(tmp_path)
    _write(root, "inflight", "bad",
           body=_stamped_brief("bad").replace("WHY: pinned by tests.", "WHY: gone"))
    assert MOD.main(["verify", "bad", "--dir", str(root)]) == 2
    assert "integrity" in capsys.readouterr().err


# ── complete: the chip's own -> done/ move, the fourth transition ────────────


def test_complete_moves_a_queued_brief_to_done(tmp_path):
    root = _lane(tmp_path)
    src = _write(root, "queued", "shipped-it")
    out = MOD.complete(root, "shipped-it", pr=3830)
    assert out["ok"] and not src.exists()
    text = (root / "done" / "shipped-it.md").read_text()
    assert "pr: 3830" in text and "WHY: pinned by tests." in text
    assert mdi.check(text).ok


def test_complete_moves_an_inflight_brief_to_done(tmp_path):
    root = _lane(tmp_path)
    _write(root, "inflight", "from-flight")
    assert MOD.complete(root, "from-flight")["ok"] is True
    assert (root / "done" / "from-flight.md").is_file()


def test_complete_refuses_when_the_id_is_in_both_source_dirs(tmp_path):
    """Picking one would be picking which copy of the brief is the real one."""
    root = _lane(tmp_path)
    _write(root, "queued", "twin")
    _write(root, "inflight", "twin")
    with pytest.raises(MOD.Refused) as e:
        MOD.complete(root, "twin")
    assert "both queued/ and inflight/" in str(e.value)


def test_complete_refuses_a_truncated_brief(tmp_path):
    root = _lane(tmp_path)
    _write(root, "inflight", "hurt",
           body=_stamped_brief("hurt").replace("WHY: pinned by tests.", "WHY:"))
    with pytest.raises(MOD.Refused):
        MOD.complete(root, "hurt")
    assert not (root / "done" / "hurt.md").exists()


def test_complete_refuses_when_done_already_holds_the_id(tmp_path):
    root = _lane(tmp_path)
    _write(root, "queued", "again")
    _write(root, "done", "again")
    with pytest.raises(MOD.Refused):
        MOD.complete(root, "again")


# ── the commit-time half: staged content vs content on disk ─────────────────
#
# Both real losses on 2026-08-27 were MOVE-THEN-COMMIT, not loss during the move. Staging
# captures content at an instant: `git mv` (and the `git add` inside `_copy_verify_delete`)
# records the bytes as they stand right then, and every edit afterwards sits unstaged — so
# a later `git commit` writes the OLDER blob under the NEWER path. A hash taken at move
# time verifies clean the entire way, because at move time the content really was correct.
# `main` acquired this lane's own brief that way: the 36-line pre-amendment blob, no stamp,
# while the working tree held the amended 88-line file.


def _committed_lane(tmp_path):
    repo = _git_repo(tmp_path)
    root = _lane(repo)
    _write(root, "inflight", "drifty")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add"], check=True)
    return repo, root


def test_verify_reports_the_staged_body_alongside_the_one_on_disk(tmp_path):
    repo, root = _committed_lane(tmp_path)
    out = MOD.verify(root, "drifty")
    assert out["index"] == out["computed"] and out["index_matches"] is True


def test_verify_reports_untracked_entries_as_having_no_index_body(tmp_path):
    """The normal case for a brief the PM just wrote — not a divergence."""
    root = _lane(tmp_path)
    _write(root, "queued", "brand-new")
    out = MOD.verify(root, "brand-new")
    assert out["index"] is None and out["index_matches"] is None


def test_an_edit_made_after_staging_shows_up_as_an_index_divergence(tmp_path):
    repo, root = _committed_lane(tmp_path)
    p = root / "inflight" / "drifty.md"
    p.write_text(p.read_text() + "\n\nAMENDMENT: added after the move was staged.\n")
    out = MOD.verify(root, "drifty")
    assert out["index_matches"] is False
    assert out["index"] != out["computed"]


def test_require_index_match_refuses_so_the_stale_blob_is_never_committed(tmp_path):
    """The gate to run immediately before committing a lane change — the one moment the
    divergence becomes permanent."""
    repo, root = _committed_lane(tmp_path)
    p = root / "inflight" / "drifty.md"
    p.write_text(p.read_text() + "\n\nAMENDMENT: added after the move was staged.\n")
    with pytest.raises(MOD.Refused) as e:
        MOD.verify(root, "drifty", require_index_match=True)
    assert "STAGED with a different body" in str(e.value)
    # and `git add` clears it — the fix the message names
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    assert MOD.verify(root, "drifty", require_index_match=True)["index_matches"] is True


def test_require_index_match_is_satisfied_by_an_untracked_entry(tmp_path):
    """Nothing is staged, so nothing stale can be committed. Refusing here would make the
    gate unusable on exactly the briefs it is cheapest to protect."""
    root = _lane(tmp_path)
    _write(root, "queued", "brand-new")
    assert MOD.verify(root, "brand-new", require_index_match=True)["ok"] is True


def test_the_move_itself_stages_the_body_it_verified(tmp_path):
    """`_copy_verify_delete` stages the destination immediately, so the window in which a
    stale blob could be committed opens only if someone edits AFTER the move."""
    repo = _git_repo(tmp_path)
    root = _lane(repo)
    _write(root, "queued", "staged-right")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add"], check=True)
    out = MOD.launch(root, "staged-right")
    assert out["staged"] is True
    assert MOD.verify(root, "staged-right", require_index_match=True)["index_matches"] is True
    assert MOD.verify(root, "staged-right")["index"] == out["body_sha256"]


# ── bind: the `pr` field's writer at PR-OPEN ─────────────────────────────────
#
# The gap: `meta-dispatch-eligible --pm-verdict` answers `lane` from a lane entry carrying
# `pr: <n>`, else a meta-state chip row carrying that `pr`, else FALSE — and false is what
# licenses an auto-review of a PR whose own PM is its reviewer. Both sources were written
# only by the lane's scheduled runs (the dispatcher on merge, meta-reconcile on its 2h
# sweep), so nobody wrote either one when the PR OPENED. PR #3833 answered `lane: false`
# for 62 minutes on 2026-08-28 for exactly this reason.
#
# The two properties worth pinning hardest are the two that cost something if wrong:
#   * bind must write BOTH sources, and must not write one when the other conflicts;
#   * bind must REFUSE to re-bind, because a wrong binding makes `pm_verdict` open some
#     other PR's review file — a verdict about different code. An absent binding costs one
#     held sweep; a wrong one launders a verdict.


def _ledger(tmp_path: Path, aspect: str, chips: list) -> Path:
    d = tmp_path / "meta-state"
    d.mkdir(exist_ok=True)
    p = d / ("%s.json" % aspect)
    p.write_text(json.dumps({"aspect": aspect, "chips": chips}, indent=1) + "\n")
    return p


def _bind_ledger(monkeypatch, tmp_path):
    """Point the tool's borrowed `resolve_ledger_dir` at a temp meta-state dir."""
    monkeypatch.setattr(MOD, "resolve_ledger_dir", lambda _d=None: str(tmp_path / "meta-state"))


def test_bind_writes_both_the_lane_entry_and_the_ledger_row(monkeypatch, tmp_path):
    root = _lane(tmp_path)
    entry = _write(root, "inflight", "chip-a")
    led = _ledger(tmp_path, "apps", [{"id": "chip-a", "pr": None, "bucket": "dispatched"}])
    _bind_ledger(monkeypatch, tmp_path)

    out = MOD.bind(root, "chip-a", pr=3833)

    assert out["entry_written"] is True and out["ledger_written"] is True
    assert "pr: 3833" in entry.read_text()
    assert json.loads(led.read_text())["chips"][0]["pr"] == 3833


def test_bind_leaves_the_body_and_its_hash_untouched(monkeypatch, tmp_path):
    """The whole reason `body_sha256` covers the body and not the file: the lane
    legitimately writes front-matter fields, and a bind is one of them."""
    root = _lane(tmp_path)
    entry = _write(root, "inflight", "chip-a")
    before = MOD.mdi.check(entry.read_text()).computed
    _ledger(tmp_path, "apps", [{"id": "chip-a", "pr": None}])
    _bind_ledger(monkeypatch, tmp_path)

    MOD.bind(root, "chip-a", pr=7)

    assert MOD.mdi.check(entry.read_text()).computed == before


def test_bind_is_idempotent(monkeypatch, tmp_path):
    """The chip may retry, and the dispatcher backstop re-runs every tick."""
    root = _lane(tmp_path)
    _write(root, "inflight", "chip-a")
    _ledger(tmp_path, "apps", [{"id": "chip-a", "pr": None}])
    _bind_ledger(monkeypatch, tmp_path)

    MOD.bind(root, "chip-a", pr=3833)
    again = MOD.bind(root, "chip-a", pr=3833)

    assert again["entry_written"] is False and again["ledger_written"] is False


def test_bind_refuses_to_rebind_the_entry_to_a_different_pr(monkeypatch, tmp_path):
    root = _lane(tmp_path)
    _write(root, "inflight", "chip-a")
    _ledger(tmp_path, "apps", [{"id": "chip-a", "pr": None}])
    _bind_ledger(monkeypatch, tmp_path)
    MOD.bind(root, "chip-a", pr=3833)

    with pytest.raises(MOD.Refused, match="already bound to PR #3833"):
        MOD.bind(root, "chip-a", pr=3999)


def test_a_conflicting_ledger_row_blocks_the_entry_write_too(monkeypatch, tmp_path):
    """Both sides are read before either is written. A split binding — entry saying one
    PR, ledger saying another — is the one state neither reader can report, because each
    consults only its own source and stops at the first answer."""
    root = _lane(tmp_path)
    entry = _write(root, "inflight", "chip-a")
    _ledger(tmp_path, "apps", [{"id": "chip-a", "pr": 3800}])
    _bind_ledger(monkeypatch, tmp_path)

    with pytest.raises(MOD.Refused, match="already bound to PR #3800"):
        MOD.bind(root, "chip-a", pr=3833)

    assert "pr:" not in entry.read_text()


def test_bind_works_with_no_lane_entry_in_this_checkout(monkeypatch, tmp_path):
    """The case that actually fires. `inflight/` is working-tree-local by construction and
    #3832 untracked it, so a chip running in a fresh worktree has no lane entry at all —
    the ledger is the only reachable source, and it is the checkout-independent one."""
    root = _lane(tmp_path)
    led = _ledger(tmp_path, "apps", [{"id": "chip-a", "pr": None}])
    _bind_ledger(monkeypatch, tmp_path)

    out = MOD.bind(root, "chip-a", pr=3833)

    assert out["entry"] is None and out["ledger_written"] is True
    assert json.loads(led.read_text())["chips"][0]["pr"] == 3833


def test_bind_refuses_when_nothing_records_the_id(monkeypatch, tmp_path):
    root = _lane(tmp_path)
    _ledger(tmp_path, "apps", [])
    _bind_ledger(monkeypatch, tmp_path)

    with pytest.raises(MOD.Refused, match="nothing to bind"):
        MOD.bind(root, "chip-a", pr=3833)


def test_bind_refuses_an_id_in_two_lane_dirs(monkeypatch, tmp_path):
    """Same reason every other verb does: two copies is two answers to 'where is this
    work', and binding one of them makes the ambiguity durable."""
    root = _lane(tmp_path)
    _write(root, "inflight", "chip-a")
    _write(root, "done", "chip-a")
    _bind_ledger(monkeypatch, tmp_path)

    with pytest.raises(MOD.Refused, match="more than one lane dir"):
        MOD.bind(root, "chip-a", pr=3833)


def test_bind_refuses_a_damaged_entry(monkeypatch, tmp_path):
    root = _lane(tmp_path)
    text = BRIEF.format(id="chip-a")
    stamped = MOD.mdi.stamp(text)[0]
    _write(root, "inflight", "chip-a", stamped.replace("WHY: pinned by tests.", "WHY:"))
    _ledger(tmp_path, "apps", [{"id": "chip-a", "pr": None}])
    _bind_ledger(monkeypatch, tmp_path)

    with pytest.raises(MOD.Refused, match="integrity:"):
        MOD.bind(root, "chip-a", pr=3833)


def test_bind_rejects_a_nonpositive_pr(tmp_path):
    with pytest.raises(MOD.Refused, match="positive PR number"):
        MOD.bind(_lane(tmp_path), "chip-a", pr=0)


# ── --files-from: the backstop that needs nothing from the chip ──────────────


def test_id_resolves_from_the_prs_own_changed_files(monkeypatch, tmp_path):
    """Every PM-lane brief's standing closer makes the chip carry it to done/ in its own
    PR, so `internal/dispatch/done/<id>.md` is in that PR's diff from the instant it
    opens — verified 4/4 on #3812/#3816/#3824/#3833."""
    root = _lane(tmp_path)
    led = _ledger(tmp_path, "apps", [{"id": "chip-a", "pr": None}])
    _bind_ledger(monkeypatch, tmp_path)

    out = MOD.bind(root, None, pr=3833, files=[
        "packages/admin/evolve_admin/applications/pod_apps.py",
        "internal/dispatch/done/chip-a.md",
    ])

    assert out["id"] == "chip-a"
    assert json.loads(led.read_text())["chips"][0]["pr"] == 3833


def test_files_arg_accepts_gh_json_and_plain_lines():
    gh = '{"files":[{"path":"internal/dispatch/done/chip-a.md","additions":1}]}'
    assert MOD.parse_files_arg(gh) == ["internal/dispatch/done/chip-a.md"]
    assert MOD.parse_files_arg("a/b.py\ninternal/dispatch/done/chip-a.md\n") == [
        "a/b.py", "internal/dispatch/done/chip-a.md"]


# ── `bind --files`: the inline form that replaces the stalling scratch file ──
#
# `files.json` existed only to bridge "the procedure forbids pipes": the run wrote
# `gh pr view --json files` output to ~/.claude/meta-dispatch/files.json with the `Write`
# tool, then pointed --files-from at it. That write is OUT-OF-CWD, and an out-of-cwd
# Write in a default-mode scheduled run raises the workspace-boundary approval prompt an
# unattended run cannot answer — 2 of this lane's measured multi-hour stalls were exactly
# that write. These pin the inline form as an exact substitute, so the file can stop
# being written at all.


def test_bind_files_inline_binds_the_same_as_files_from(monkeypatch, tmp_path):
    root = _lane(tmp_path)
    led = _ledger(tmp_path, "apps", [{"id": "chip-a", "pr": None}])
    _bind_ledger(monkeypatch, tmp_path)

    gh = '{"files":[{"path":"internal/dispatch/done/chip-a.md","additions":1}]}'
    assert MOD.main(["bind", "--pr", "3833", "--files", gh, "--dir", str(root)]) == 0
    assert json.loads(led.read_text())["chips"][0]["pr"] == 3833


def test_bind_files_and_files_from_together_is_a_usage_error(tmp_path, capsys):
    root = _lane(tmp_path)
    assert MOD.main(["bind", "--pr", "1", "--files", "a.md",
                     "--files-from", "-", "--dir", str(root)]) == 2
    assert "not both" in capsys.readouterr().err


def test_bind_with_no_id_and_no_files_says_which_flags_exist(tmp_path, capsys):
    root = _lane(tmp_path)
    assert MOD.main(["bind", "--pr", "1", "--dir", str(root)]) == 2
    assert "--files" in capsys.readouterr().err


def test_a_non_pm_lane_pr_is_named_as_such_not_guessed_at(tmp_path):
    with pytest.raises(MOD.Refused, match="not a PM-lane PR"):
        MOD.bind(_lane(tmp_path), None, pr=3830, files=["tools/meta-queue"])


def test_two_briefs_in_one_diff_refuse_rather_than_pick(tmp_path):
    with pytest.raises(MOD.Refused, match="name 2 lane briefs"):
        MOD.bind(_lane(tmp_path), None, pr=1, files=["internal/dispatch/done/a-one.md",
                                                     "internal/dispatch/inflight/b-two.md"])


def test_a_queued_only_diff_is_not_a_chip_claiming_its_brief(tmp_path):
    """The PM's own queueing PR touches `queued/<id>.md` and nothing else — #3830 does
    exactly this, at 110+/0-, while chip PR #3833 carries `done/<id>.md` at 0+/0-. If
    `queued/` counted, a queueing PR could bind an id whose real chip is in flight
    elsewhere, and since bind refuses to RE-bind, the wrong answer would win by arriving
    first."""
    with pytest.raises(MOD.Refused, match="not a PM-lane PR"):
        MOD.bind(_lane(tmp_path), None, pr=3830,
                 files=["internal/dispatch/queued/lane-launch-field-has-no-writeback.md"])


def test_the_path_pattern_does_not_match_a_reviews_file(tmp_path):
    """`reviews/pr-<n>.md` is in a PM-lane PR's diff too when the PM commits a verdict, and
    it is NOT a brief — matching it would bind a chip id of `pr-3816`."""
    with pytest.raises(MOD.Refused, match="not a PM-lane PR"):
        MOD.bind(_lane(tmp_path), None, pr=3816,
                 files=["internal/dispatch/reviews/pr-3816.md"])


# ── the orphan id: a chip closing out under an id nothing dispatched ─────────
#
# The #3849 defect and why exit 3 exists. PR #3849 closed out `dossier-module-synthesis`
# by ADDING `internal/dispatch/done/dossier-modules.md` — the brief verbatim, under an id
# no brief has ever had. Nothing was lost; the KEY broke, and the lane is keyed by id, so
# `pod-intelligence-shell` sat `depends-on-unmet` on a `chip:` that could never clear and
# the unbindable marker held one of three slots. `bind` RAISED the right sentence at the
# right moment and it was spent as noise: every refusal exited 2, and the procedure reads
# exit 2 as "not this lane's PR — change nothing, move on".
#
# So these tests pin the SPLIT, not the message: the orphan is exit 3 with a stable
# `reason`, and the benign "not a PM-lane PR" refusal keeps exit 2 byte-for-byte, because
# chips call `bind` on every PR open.


def test_the_3849_shape_is_named_as_an_orphan_and_names_both_ids(monkeypatch, tmp_path):
    """The fixture IS #3849: the lane holds `inflight/dossier-module-synthesis.md`, the
    PR's diff adds `done/dossier-modules.md`. The finding is only actionable if it names
    BOTH ids — the coined one, and the in-flight entry it orphaned."""
    root = _lane(tmp_path)
    _write(root, "inflight", "dossier-module-synthesis")
    _ledger(tmp_path, "reports", [{"id": "dossier-module-synthesis", "pr": None,
                                   "bucket": "dispatched"}])
    _bind_ledger(monkeypatch, tmp_path)

    with pytest.raises(MOD.OrphanId) as exc:
        MOD.bind(root, None, pr=3849,
                 files=["packages/admin/evolve_admin/reports/dossier.py",
                        "internal/dispatch/done/dossier-modules.md"])

    e = exc.value
    assert e.reason == "orphan-id" and e.brief_id == "dossier-modules"
    assert e.lane_state == "done" and e.resolved_from == "files"
    assert [c["id"] for c in e.candidates] == ["dossier-module-synthesis"]
    assert "dossier-modules" in str(e) and "dossier-module-synthesis" in str(e)
    assert "NEVER auto-repair" in str(e)


def test_the_orphan_refusal_is_exit_3_and_json_carries_a_stable_reason(monkeypatch,
                                                                      tmp_path, capsys):
    """The differentiation is what the caller consumes (procedure step 1), so it is pinned
    at the CLI boundary: a distinct status AND a machine-readable `reason`."""
    root = _lane(tmp_path)
    _write(root, "inflight", "dossier-module-synthesis")
    _ledger(tmp_path, "reports", [{"id": "dossier-module-synthesis", "pr": None}])
    _bind_ledger(monkeypatch, tmp_path)
    files = tmp_path / "files.json"
    files.write_text(json.dumps({"files": [
        {"path": "internal/dispatch/done/dossier-modules.md"}]}))

    rc = MOD.main(["bind", "--pr", "3849", "--files-from", str(files),
                   "--dir", str(root), "--json"])

    assert rc == MOD.EXIT_ORPHAN_ID == 3
    cap = capsys.readouterr()
    payload = json.loads(cap.out)
    assert payload["reason"] == "orphan-id" and payload["ok"] is False
    assert payload["id"] == "dossier-modules" and payload["lane_state"] == "done"
    assert payload["candidates"][0]["id"] == "dossier-module-synthesis"
    assert "refused:" in cap.err          # the human sentence is still on stderr


def test_a_non_pm_lane_pr_keeps_exit_2_and_its_message(tmp_path, capsys):
    """The REGRESSION GUARD for every chip-side caller. Chips run `bind` on every PR open;
    if the benign refusal's status changed meaning, that is a silent behaviour change in
    all of them. Exit 2, same sentence, and no JSON object on stdout."""
    root = _lane(tmp_path)
    files = tmp_path / "files.json"
    files.write_text("tools/meta-queue\n")

    rc = MOD.main(["bind", "--pr", "3830", "--files-from", str(files),
                   "--dir", str(root), "--json"])

    assert rc == MOD.EXIT_REFUSED == 2
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "not a PM-lane PR" in cap.err


def test_a_healthy_chip_pr_still_binds_and_exits_zero(monkeypatch, tmp_path, capsys):
    """`done/<id>.md` with a matching lane entry is the NORMAL closer, and it must be
    untouched by the guard — a detector that fires on healthy chips halts the lane."""
    root = _lane(tmp_path)
    _write(root, "inflight", "chip-a")
    _ledger(tmp_path, "apps", [{"id": "chip-a", "pr": None}])
    _bind_ledger(monkeypatch, tmp_path)
    files = tmp_path / "files.json"
    files.write_text("internal/dispatch/done/chip-a.md\n")

    rc = MOD.main(["bind", "--pr", "3833", "--files-from", str(files),
                   "--dir", str(root), "--json"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["id"] == "chip-a"


def test_a_ledger_only_id_is_not_an_orphan(monkeypatch, tmp_path):
    """The ledger fallback WORKING is not a defect. `inflight/` is working-tree-local, so
    a chip in a fresh worktree legitimately has no lane entry at all — the checkout-
    independent chip row is the evidence, and it binds."""
    root = _lane(tmp_path)
    led = _ledger(tmp_path, "apps", [{"id": "chip-a", "pr": None}])
    _bind_ledger(monkeypatch, tmp_path)

    out = MOD.bind(root, None, pr=3833, files=["internal/dispatch/done/chip-a.md"])

    assert out["entry"] is None and out["ledger_written"] is True
    assert json.loads(led.read_text())["chips"][0]["pr"] == 3833


def test_an_orphan_with_no_resembling_entry_still_refuses_as_one(monkeypatch, tmp_path):
    """Candidates are the poke's second half, not its precondition. An unknown id with
    nothing resembling it is still an unknown id, and must not degrade to the benign
    refusal just because the tool cannot suggest an owner."""
    root = _lane(tmp_path)
    _write(root, "inflight", "wholly-unrelated-work")
    _ledger(tmp_path, "apps", [])
    _bind_ledger(monkeypatch, tmp_path)

    with pytest.raises(MOD.OrphanId) as exc:
        MOD.bind(root, None, pr=3849, files=["internal/dispatch/done/dossier-modules.md"])

    assert exc.value.candidates == []
    assert "No in-flight entry or unbound chip row resembles it" in str(exc.value)


def test_an_orphan_id_passed_on_the_command_line_is_still_an_orphan(monkeypatch, tmp_path):
    """The chip-side call form (`bind <id> --pr <n>`). It coins the id itself, so it is the
    FIRST caller that can be told, and it gets the same reason with `lane_state: None`."""
    root = _lane(tmp_path)
    _write(root, "inflight", "dossier-module-synthesis")
    _ledger(tmp_path, "reports", [{"id": "dossier-module-synthesis", "pr": None}])
    _bind_ledger(monkeypatch, tmp_path)

    with pytest.raises(MOD.OrphanId) as exc:
        MOD.bind(root, "dossier-modules", pr=3849)

    assert exc.value.resolved_from == "argument" and exc.value.lane_state is None
    assert exc.value.candidates[0]["id"] == "dossier-module-synthesis"


def test_the_orphan_refusal_writes_nothing(monkeypatch, tmp_path):
    """`Refused` means nothing changed, and `OrphanId` is a `Refused`. The in-flight entry
    it names must not acquire the PR it declined to bind — that would be the auto-repair
    the brief forbids, performed by accident."""
    root = _lane(tmp_path)
    entry = _write(root, "inflight", "dossier-module-synthesis")
    led = _ledger(tmp_path, "reports", [{"id": "dossier-module-synthesis", "pr": None}])
    _bind_ledger(monkeypatch, tmp_path)
    before = entry.read_text()

    with pytest.raises(MOD.OrphanId):
        MOD.bind(root, None, pr=3849, files=["internal/dispatch/done/dossier-modules.md"])

    assert entry.read_text() == before
    assert json.loads(led.read_text())["chips"][0]["pr"] is None
    assert not (root / "done" / "dossier-modules.md").exists()


def test_orphan_candidates_ignore_ids_that_share_nothing(tmp_path):
    """An unranked list of every open chip is noise, and noise is how the first alarm was
    lost — a candidate with no token in common is not offered at all."""
    root = _lane(tmp_path)
    _write(root, "inflight", "banner-url-test-asserts-source-text")
    assert MOD._orphan_candidates(root, "dossier-modules", ledger_dir=str(tmp_path)) == []


def test_orphan_candidates_rank_the_closest_id_first(tmp_path):
    """Ranking is share-of-tokens, singularized — `dossier-modules` shares `dossier` with
    one candidate and `dossier` + `module` with the real owner, which is the difference
    between a weak match and the obvious one."""
    root = _lane(tmp_path)
    for cand in ("dossier-edition-zero", "dossier-module-synthesis"):
        _write(root, "inflight", cand)

    ranked = MOD._orphan_candidates(root, "dossier-modules", ledger_dir=str(tmp_path))

    assert [c["id"] for c in ranked] == ["dossier-module-synthesis", "dossier-edition-zero"]
    assert ranked[0]["shared_tokens"] == ["dossier", "module"]


# ── the binding is readable by the tool that gates on it ─────────────────────


def test_the_ledger_binding_is_what_pm_verdict_reads(monkeypatch, tmp_path):
    """The end-to-end property. Binding is pointless unless `meta-dispatch-eligible`'s
    `_ledger_pm_lane_row` — keyed on `pr`, and requiring a `note` naming the lane — finds
    what bind wrote. This pins the two writers to one contract rather than to each other's
    prose."""
    eligible = _TOOL.parent / "meta-dispatch-eligible"
    loader = importlib.machinery.SourceFileLoader("mde", str(eligible))
    spec = importlib.util.spec_from_loader("mde", loader)
    mde = importlib.util.module_from_spec(spec)
    loader.exec_module(mde)

    root = _lane(tmp_path)
    _ledger(tmp_path, "apps", [{"id": "chip-a", "pr": None, "privileged": False,
                                "note": "PM lane: internal/dispatch/inflight/chip-a.md"}])
    _bind_ledger(monkeypatch, tmp_path)
    MOD.bind(root, "chip-a", pr=3833)

    found = mde._ledger_pm_lane_row(3833, ledger_dir=str(tmp_path / "meta-state"))
    assert found is not None and found["row"]["id"] == "chip-a"


# ── `now`: the run's start stamp comes from the heartbeat's own clock ─────────
#
# Added by the bash-clock-skew diagnosis (2026-08-31). PR #3874 suspended `--started`
# after reading a 4h23m `elapsed_s` as `date -u` and the helpers disagreeing. They never
# did: the gap was real elapsed time (both ticks blocked mid-run on a `Write` for ~4h).
# `now` removes the argument rather than a skew — one process stamps both ends, so a
# future long `elapsed_s` cannot be explained away as a second clock again.


def test_now_and_heartbeat_stamp_from_the_same_clock(monkeypatch, tmp_path):
    """The property the verb exists for: `now` and `heartbeat`'s `run` are the SAME
    function, so `elapsed_s` has one clock at both ends by construction — not by two
    stamps happening to agree."""
    fixed = _utc(2026, 8, 31, 5, 46, 39)
    monkeypatch.setattr(MOD, "_utc_now", lambda: fixed)

    assert MOD.now_stamp()["now"] == "2026-08-31T05:46:39Z"
    rec = MOD.heartbeat(tmp_path / "log")["record"]
    assert rec["run"] == MOD.now_stamp()["now"]


def test_now_output_round_trips_into_elapsed_s(tmp_path):
    """`now`'s string must be directly usable as `--started` — that is the whole
    contract between step 0 and step 7. A format the elapsed parser rejects would
    silently cost the field (it drops unusable values rather than refusing)."""
    started = MOD.now_stamp()["now"]
    rec = MOD.heartbeat(tmp_path / "log", started=started)["record"]
    assert rec["started"] == started
    assert rec["elapsed_s"] >= 0


def test_now_stamp_is_utc_aware_and_second_precision(monkeypatch):
    """A naive or local stamp is how local time ended up wearing a `Z` in the old log;
    `_elapsed_seconds` rejects naive input, so `now` must always emit a real UTC `Z`."""
    local = datetime.datetime(2026, 8, 30, 22, 34, 2, 500000,
                              tzinfo=datetime.timezone(datetime.timedelta(hours=-7)))
    monkeypatch.setattr(MOD, "_utc_now", lambda: local)
    assert MOD.now_stamp()["now"] == "2026-08-31T05:34:02Z"


def test_cli_now_prints_the_bare_stamp_and_exits_zero(capsys):
    """Step 0 captures `<START>` from stdout, so the plain form must be the stamp and
    nothing else — no label, no trailing prose to strip."""
    assert MOD.main(["now"]) == 0
    out = capsys.readouterr().out.strip()
    assert datetime.datetime.strptime(out, "%Y-%m-%dT%H:%M:%SZ")


def test_cli_now_needs_no_lane_dir(monkeypatch, capsys):
    """`now` must run before the lane-dir check. A scheduled tick calls it as its FIRST
    act — from a cwd whose `internal/dispatch/` may not exist — and an exit 2 there
    would cost the run its start stamp for a reason unrelated to the clock."""
    monkeypatch.chdir(Path(__file__).parent)
    assert MOD.main(["now", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


# ── start: the START of a chip finally has a writer ──────────────────────────
#
# The gap, and it is `bind` one field over: the fields that record DISPATCH have a writer
# (procedure step 4b writes `dispatched`, `session`, `launch: prepared`, and the ledger
# chip row); the fields that record STARTING had none. Step 4b can only ever write
# `prepared`, by construction — the dispatcher's run ends before the operator clicks — and
# nothing in tools/, the skills, or the procedure docs ever wrote `launch: started`.
#
# What it cost, 2026-08-27: `tools/meta-queue`'s click-pending derivation reads "bucket ∈
# {dispatched, stalled} + task_id + ZERO start evidence + dispatched before today". A chip
# the operator HAD clicked satisfies every clause the day after dispatch, because the one
# field that would falsify it was unwritten. The lane reported a running chip as awaiting a
# click for ~20 minutes and a scheduled tick poked "one tap to start it"; the same gap the
# same day minted a real duplicate. The reconciler's inverse guard blocks the AUTOMATED
# relaunch; group (E) is the invitation to do it by hand, and a human click on an
# already-running chip is the identical duplicate with no guard in the path.
#
# The two properties worth pinning hardest:
#   * `start` must never CREATE — no lane entry, no ledger row. That, not prose, is what
#     keeps it from becoming a second dispatch channel (spec §7);
#   * a default-branch name must be REFUSED. `branch` is both the click-pending evidence
#     and the reconciler's `git ls-remote` relaunch target, so recording `main` is worse
#     than recording nothing: permanently "started", permanently un-relaunchable.


def _started_lane(tmp_path: Path, state: str = "inflight") -> tuple:
    root = _lane(tmp_path)
    entry = _write(root, state, "chip-a", BRIEF.format(id="chip-a").replace(
        "pm: fable-cowork\n",
        "pm: fable-cowork\ndispatched: 2026-08-30\nsession: task_abc\n"
        "branch: null\nlaunch: prepared\n"))
    return root, entry


def test_start_writes_both_the_lane_entry_and_the_ledger_row(monkeypatch, tmp_path):
    root, entry = _started_lane(tmp_path)
    led = _ledger(tmp_path, "substrate",
                  [{"id": "chip-a", "branch": None, "bucket": "dispatched"}])
    _bind_ledger(monkeypatch, tmp_path)

    out = MOD.start(root, "chip-a", branch="claude/x-1")

    assert out["entry_written"] is True and out["ledger_written"] is True
    text = entry.read_text()
    assert "branch: claude/x-1" in text and "launch: started" in text
    assert "launch: prepared" not in text
    assert json.loads(led.read_text())["chips"][0]["branch"] == "claude/x-1"


def test_start_reads_dispatchers_branch_null_as_ABSENT(monkeypatch, tmp_path):
    """Step 4b writes `branch: null` when the branch is not yet known. A reader that saw
    that as a value would treat the dispatcher's own placeholder as start evidence — the
    exact inversion this verb exists to remove."""
    root, entry = _started_lane(tmp_path)
    _bind_ledger(monkeypatch, tmp_path)

    assert MOD._read_fm_str(entry.read_text(), "branch") is None
    MOD.start(root, "chip-a", branch="claude/x-1")
    assert MOD._read_fm_str(entry.read_text(), "branch") == "claude/x-1"


def test_start_leaves_the_body_and_its_hash_untouched(monkeypatch, tmp_path):
    root, entry = _started_lane(tmp_path)
    before = MOD.mdi.check(entry.read_text()).computed
    _bind_ledger(monkeypatch, tmp_path)

    MOD.start(root, "chip-a", branch="claude/x-1")

    assert MOD.mdi.check(entry.read_text()).computed == before


def test_start_is_idempotent(monkeypatch, tmp_path):
    """A chip session can start, resume, and be messaged — the call must be safe to
    repeat, and safe on an entry that already carries the values."""
    root, _ = _started_lane(tmp_path)
    _ledger(tmp_path, "substrate", [{"id": "chip-a", "branch": None}])
    _bind_ledger(monkeypatch, tmp_path)

    MOD.start(root, "chip-a", branch="claude/x-1")
    again = MOD.start(root, "chip-a", branch="claude/x-1")

    assert again["entry_written"] is False and again["ledger_written"] is False
    assert again["branch"] == "claude/x-1"


def test_start_refuses_to_repoint_an_existing_branch(monkeypatch, tmp_path):
    """Like `bind`: the reconciler `ls-remote`s this value to decide whether a chip may be
    relaunched, so silently repointing it moves the relaunch guard onto a branch nobody
    chose. A human resolves it."""
    root, _ = _started_lane(tmp_path)
    _ledger(tmp_path, "substrate", [{"id": "chip-a", "branch": None}])
    _bind_ledger(monkeypatch, tmp_path)
    MOD.start(root, "chip-a", branch="claude/x-1")

    with pytest.raises(MOD.Refused, match="refusing to re-point"):
        MOD.start(root, "chip-a", branch="claude/x-2")


def test_start_a_conflicting_ledger_row_blocks_the_entry_write_too(monkeypatch, tmp_path):
    """Both sides are read before either is written. A split record — entry naming one
    branch, ledger another — is the one state neither reader can report."""
    root, entry = _started_lane(tmp_path)
    _ledger(tmp_path, "substrate", [{"id": "chip-a", "branch": "claude/other"}])
    _bind_ledger(monkeypatch, tmp_path)

    with pytest.raises(MOD.Refused, match="already records branch 'claude/other'"):
        MOD.start(root, "chip-a", branch="claude/x-1")

    assert "launch: prepared" in entry.read_text()


@pytest.mark.parametrize("bad", ["main", "master", "MAIN", "trunk", "HEAD"])
def test_start_refuses_a_default_branch_name(monkeypatch, tmp_path, bad):
    """The load-bearing guard. `branch` is read as click-pending start evidence AND as the
    reconciler's `git ls-remote` relaunch target, so `main` would make the chip read as
    started forever and un-relaunchable forever — strictly worse than an absent field."""
    root, entry = _started_lane(tmp_path)
    _ledger(tmp_path, "substrate", [{"id": "chip-a", "branch": None}])
    _bind_ledger(monkeypatch, tmp_path)

    with pytest.raises(MOD.Refused, match="refusing to record"):
        MOD.start(root, "chip-a", branch=bad)

    assert "launch: prepared" in entry.read_text()


def test_start_never_creates_a_ledger_row_or_a_lane_entry(monkeypatch, tmp_path):
    """THE LINE that keeps this from becoming a second dispatch channel (spec §7), drawn
    structurally rather than by prose: an id the queue never dispatched has neither
    source, and `start` refuses instead of inserting one."""
    root = _lane(tmp_path)
    led = _ledger(tmp_path, "substrate", [])
    _bind_ledger(monkeypatch, tmp_path)

    with pytest.raises(MOD.Refused, match="nothing to start"):
        MOD.start(root, "chip-a", branch="claude/x-1")

    assert json.loads(led.read_text())["chips"] == []
    assert list((root / "inflight").iterdir()) == []


def test_start_leaves_a_queued_entry_alone(monkeypatch, tmp_path):
    """Only an entry already in `inflight/` is written. A brief in `queued/` is one the
    dispatcher has not taken, so stamping start evidence onto it would state something
    false about the lane's own state — and `queued/` is committed, so it would state it
    to every checkout."""
    root, entry = _started_lane(tmp_path, state="queued")
    _ledger(tmp_path, "substrate", [{"id": "chip-a", "branch": None}])
    _bind_ledger(monkeypatch, tmp_path)

    out = MOD.start(root, "chip-a", branch="claude/x-1")

    assert out["entry_written"] is False and "queued/" in out["entry_skipped"]
    assert "launch: prepared" in entry.read_text()
    assert out["ledger_written"] is True          # the reachable half still lands


def test_start_works_with_no_lane_entry_in_this_checkout(monkeypatch, tmp_path):
    """The case that actually fires, and the reason the ledger is the load-bearing half:
    `inflight/` is working-tree-local by construction (#3832), so a chip running in its
    own worktree has no lane entry at all."""
    root = _lane(tmp_path)
    led = _ledger(tmp_path, "substrate", [{"id": "chip-a", "branch": None}])
    _bind_ledger(monkeypatch, tmp_path)

    out = MOD.start(root, "chip-a", branch="claude/x-1")

    assert out["entry"] is None and out["ledger_written"] is True
    assert json.loads(led.read_text())["chips"][0]["branch"] == "claude/x-1"


def test_start_refuses_an_id_in_two_lane_dirs(monkeypatch, tmp_path):
    root = _lane(tmp_path)
    _write(root, "inflight", "chip-a")
    _write(root, "done", "chip-a")
    _bind_ledger(monkeypatch, tmp_path)

    with pytest.raises(MOD.Refused, match="more than one lane dir"):
        MOD.start(root, "chip-a", branch="claude/x-1")


def test_start_refuses_a_damaged_entry(monkeypatch, tmp_path):
    root = _lane(tmp_path)
    stamped = MOD.mdi.stamp(BRIEF.format(id="chip-a"))[0]
    _write(root, "inflight", "chip-a", stamped.replace("WHY: pinned by tests.", "WHY:"))
    _ledger(tmp_path, "substrate", [{"id": "chip-a", "branch": None}])
    _bind_ledger(monkeypatch, tmp_path)

    with pytest.raises(MOD.Refused, match="integrity:"):
        MOD.start(root, "chip-a", branch="claude/x-1")


def test_start_defaults_the_branch_to_this_checkouts_head(monkeypatch, tmp_path):
    """The chip should not have to name its own branch — it is already standing on it."""
    repo = _git_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "claude/head-1"],
                   check=True)
    root = _lane(tmp_path)
    led = _ledger(tmp_path, "substrate", [{"id": "chip-a", "branch": None}])
    _bind_ledger(monkeypatch, tmp_path)

    out = MOD.start(root, "chip-a")

    assert out["branch"] == "claude/head-1"
    assert json.loads(led.read_text())["chips"][0]["branch"] == "claude/head-1"


def test_cli_start_exits_zero_and_reports_what_it_wrote(monkeypatch, tmp_path, capsys):
    root, _ = _started_lane(tmp_path)
    _ledger(tmp_path, "substrate", [{"id": "chip-a", "branch": None}])
    _bind_ledger(monkeypatch, tmp_path)

    assert MOD.main(["start", "chip-a", "--dir", str(root),
                     "--branch", "claude/x-1", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verb"] == "start" and out["branch"] == "claude/x-1"
    assert out["entry_written"] is True and out["ledger_written"] is True


def test_cli_start_refuses_with_exit_2_and_changes_nothing(monkeypatch, tmp_path, capsys):
    root, entry = _started_lane(tmp_path)
    _ledger(tmp_path, "substrate", [{"id": "chip-a", "branch": None}])
    _bind_ledger(monkeypatch, tmp_path)

    assert MOD.main(["start", "chip-a", "--dir", str(root), "--branch", "main"]) == 2
    assert "refused" in capsys.readouterr().err
    assert "launch: prepared" in entry.read_text()


# ── both directions, end to end: what the operator's queue actually shows ────
#
# The non-negotiable pair. Group (E) is a real protection against silently-lost work, so a
# fix that empties it has broken the thing it was meant to repair. These two run the chip
# rows through `tools/meta-queue`'s REAL classifier rather than re-asserting the writer's
# own output, because the defect was never in either tool alone — it was that the field one
# reads had no writer.


def _load_queue():
    path = _TOOL.parent / "meta-queue"
    loader = importlib.machinery.SourceFileLoader("meta_queue_e2e", str(path))
    spec = importlib.util.spec_from_loader("meta_queue_e2e", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _click_pending_row(chip_id):
    """A row that satisfies every clause of the click-pending derivation."""
    return {"id": chip_id, "title": "a chip", "task_id": "task_abc", "pr": None,
            "branch": None, "bucket": "dispatched", "dispatched": "2026-08-30"}


def test_a_started_chip_leaves_group_E_and_an_unclicked_one_stays(monkeypatch, tmp_path):
    """BOTH directions, proven against the real classifier. `started` was clicked and
    ran `start`; `unclicked` never was, and MUST still be surfaced for the operator."""
    mq = _load_queue()
    today = datetime.date(2026, 9, 1)
    root = _lane(tmp_path)
    led = _ledger(tmp_path, "substrate",
                  [_click_pending_row("started"), _click_pending_row("unclicked")])
    _bind_ledger(monkeypatch, tmp_path)

    MOD.start(root, "started", branch="claude/started-1")

    rows = {c["id"]: c for c in json.loads(led.read_text())["chips"]}
    assert mq.classify_chip(rows["started"], today) is None
    assert mq.classify_chip(rows["unclicked"], today)[0] == "click_pending"


def test_start_clears_a_bucket_the_reconciler_already_stamped(monkeypatch, tmp_path):
    """The sequence that actually happens: the 2-hour sweep writes `bucket: click-pending`
    back BEFORE the operator clicks. Without forward recovery in the projection, the chip
    keeps rendering in (E) — inviting the second click — until the next sweep, which is
    the ~20-minute window that produced the spurious poke on 2026-08-27."""
    mq = _load_queue()
    today = datetime.date(2026, 9, 1)
    root = _lane(tmp_path)
    stamped = _click_pending_row("started")
    stamped["bucket"] = "click-pending"
    led = _ledger(tmp_path, "substrate", [stamped, _click_pending_row("unclicked")])
    _bind_ledger(monkeypatch, tmp_path)

    MOD.start(root, "started", branch="claude/started-1")

    rows = {c["id"]: c for c in json.loads(led.read_text())["chips"]}
    assert mq.classify_chip(rows["started"], today) is None
    assert mq.classify_chip(rows["unclicked"], today)[0] == "click_pending"


# ── repair-queued-copy: the two self-healing duplicate shapes (D-PM8) ─────────
#
# The lane produces one duplicate shape ON PURPOSE. `launch` is a working-tree deletion of
# `queued/<id>.md` plus an untracked `inflight/` marker, so the queued copy is still on
# `main` until the chip's PR renames it — and every pull or branch switch puts it back.
# The lane then read one id in two dirs and dispatched nothing until a human deleted the
# file: four times between 2026-09-01 and 09-03. This verb is the repair; the tests below
# pin far harder on what it REFUSES, because the failure it must never have is deleting a
# brief that exists nowhere else.


def _stamped(root: Path, sub: str, brief_id: str, body: str = "WHY: pinned by tests.",
             pr: int | None = None, stamp: bool = True) -> Path:
    """A lane entry with the body_sha256 `launch` would have stamped into it."""
    text = "---\nid: %s\naspect: apps\ntitle: \"A brief\"\nprivileged: false\n" % brief_id
    if pr is not None:
        text += "pr: %d\n" % pr
    text += "created: 2026-08-23\npm: fable-cowork\n---\n%s\n" % body
    if stamp:
        text, _ = MOD.mdi.stamp(text)
    p = root / sub / ("%s.md" % brief_id)
    p.write_text(text)
    return p


def test_a_queued_copy_restored_over_an_inflight_marker_is_re_deleted(tmp_path):
    """The pull/checkout shape, and the whole reason the verb exists."""
    root = _lane(tmp_path)
    queued = _stamped(root, "queued", "restored-chip", stamp=False)   # main's copy: unstamped
    marker = _stamped(root, "inflight", "restored-chip")              # launch stamped this

    r = MOD.repair_queued_copy(root, "restored-chip")

    assert r["ok"] and r["method"] == "unlink"
    assert r["shape"] == MOD.mdi.REPAIR_RESTORED
    assert not queued.exists(), "the restored queued copy is gone"
    assert marker.exists(), "the in-flight marker is never touched"
    assert r["log"] == ("re-deleted queued copy of restored-chip "
                        "(restored by checkout/pull)")


def test_a_queued_copy_with_a_different_body_stays_a_conflict(tmp_path):
    """The amend flow depends on this: a brief edited after dispatch has genuinely
    diverged from the chip that is running against the older text, and the lane must keep
    saying so rather than quietly deleting one of the two versions."""
    root = _lane(tmp_path)
    queued = _stamped(root, "queued", "amended-chip", body="WHY: the AMENDED brief.",
                      stamp=False)
    _stamped(root, "inflight", "amended-chip", body="WHY: the brief as dispatched.")

    with pytest.raises(MOD.Refused) as e:
        MOD.repair_queued_copy(root, "amended-chip")

    assert "self-healing shapes" in str(e.value)
    assert queued.exists(), "a refusal leaves the lane exactly as found"


def test_a_queued_copy_superseded_by_a_merged_done_entry_is_re_deleted(tmp_path):
    """The #3964 shape: the merge landed the `done/` entry while this checkout's branch
    still carried the queued copy. The bodies are identical — `complete` moves the file
    verbatim — which is what makes the deletion provably lossless."""
    root = _lane(tmp_path)
    queued = _stamped(root, "queued", "merged-chip", stamp=False)
    done = _stamped(root, "done", "merged-chip", pr=3964)

    r = MOD.repair_queued_copy(root, "merged-chip")

    assert r["shape"] == MOD.mdi.REPAIR_SUPERSEDED and r["evidence"] == "body-sha256"
    assert r["pr"] == 3964
    assert not queued.exists() and done.exists()
    assert r["log"] == "re-deleted queued copy of merged-chip (superseded by done/, PR #3964)"


def test_a_done_entry_that_only_CONTAINS_the_queued_body_is_never_deleted(tmp_path):
    """F1 on #3982, with the reviewer's own fixture.

    Containment was chosen so an APPENDED outcome note would not stop a healed lane. But
    an appended note and a genuinely NEW brief queued under a completed id produce the
    same bytes — as does an amended brief that lost a paragraph — so containment repaired
    all three. Unattended, every 30 minutes, leaving one log line: a re-queued brief could
    never be dispatched and nobody was told. A strict prefix test does not separate them
    either (this fixture IS a prefix), so the shape refuses and the operator decides.
    """
    root = _lane(tmp_path)
    queued = _stamped(root, "queued", "shrunk", body="WHY: do part one.", stamp=False)
    done = _stamped(root, "done", "shrunk",
                    body="WHY: do part one.\nAND part two.", pr=99)

    with pytest.raises(MOD.Refused) as e:
        MOD.repair_queued_copy(root, "shrunk")

    assert queued.exists(), "the queued brief is still there for the operator to read"
    assert done.exists()
    assert MOD.mdi.POKE_DONE_DIVERGED in str(e.value)
    assert "NEW brief was queued under a completed id" in str(e.value)


def test_a_done_entry_without_a_pr_stays_a_conflict(tmp_path):
    """The `pr` is what says a PR actually carried this brief to `done/`. Without one the
    done entry may be hand-filed, and the queued copy is not provably the stale half."""
    root = _lane(tmp_path)
    queued = _stamped(root, "queued", "unbound-chip", stamp=False)
    _stamped(root, "done", "unbound-chip")

    with pytest.raises(MOD.Refused):
        MOD.repair_queued_copy(root, "unbound-chip")

    assert queued.exists()


def test_a_lone_queued_brief_is_never_deleted(tmp_path):
    """The failure this verb must never have. An ordinary queued brief is the one file in
    the lane with no other copy — it is exactly what was destroyed on 2026-08-25."""
    root = _lane(tmp_path)
    queued = _stamped(root, "queued", "ordinary-chip", stamp=False)

    with pytest.raises(MOD.Refused) as e:
        MOD.repair_queued_copy(root, "ordinary-chip")

    assert "only copy of a brief" in str(e.value)
    assert queued.exists()


def test_an_id_in_all_three_dirs_is_a_real_ambiguity(tmp_path):
    root = _lane(tmp_path)
    queued = _stamped(root, "queued", "triple-chip", stamp=False)
    _stamped(root, "inflight", "triple-chip")
    _stamped(root, "done", "triple-chip", pr=1)

    with pytest.raises(MOD.Refused) as e:
        MOD.repair_queued_copy(root, "triple-chip")

    assert "three copies" in str(e.value)
    assert queued.exists()


def test_repair_never_touches_the_inflight_or_done_file(tmp_path):
    """Structural, not remembered: the verb has no parameter for the other path. Pinned
    byte-for-byte so a future refactor that "helpfully" rewrites the keeper is caught."""
    root = _lane(tmp_path)
    _stamped(root, "queued", "keep-chip", stamp=False)
    marker = _stamped(root, "inflight", "keep-chip")
    before = marker.read_bytes()

    MOD.repair_queued_copy(root, "keep-chip")

    assert marker.read_bytes() == before


def test_repair_is_idempotent_on_an_absent_queued_copy(tmp_path):
    """A run that dies mid-step must be safe to re-run, so the dispatcher may call this on
    any id it is unsure about."""
    root = _lane(tmp_path)
    _stamped(root, "inflight", "gone-chip")

    r = MOD.repair_queued_copy(root, "gone-chip")

    assert r["ok"] and r["method"] == "already-absent" and r["log"] is None


def test_repair_unlinks_rather_than_staging_a_deletion(tmp_path):
    """Plain unlink, never `git rm`. The tracked deletion of this path is the dispatcher's
    OWN pending edit — unlinking restores the unstaged-deletion state `launch` left, while
    a staged deletion is how lane bookkeeping ends up committed into somebody else's PR."""
    repo = _git_repo(tmp_path)
    root = _lane(repo)
    _stamped(root, "queued", "tracked-chip", stamp=False)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "queued"], check=True)
    _stamped(root, "inflight", "tracked-chip")

    MOD.repair_queued_copy(root, "tracked-chip")

    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--",
                             "dispatch/queued/tracked-chip.md"],
                            capture_output=True, text=True).stdout
    assert status.startswith(" D"), "deletion must be UNSTAGED, got %r" % status


def test_a_corrupt_copy_is_refused_rather_than_repaired(tmp_path):
    """The comparison IS the safety argument, so a body that cannot be read is never a
    body that can be shown to survive elsewhere."""
    root = _lane(tmp_path)
    queued = root / "queued" / "broken-chip.md"
    queued.write_text("---\nid: broken-chip\naspect: apps\ntitle: x\nprivileged: false\n"
                      "created: 2026-08-23\npm: fable-cowork\n---\n")   # body gone
    _stamped(root, "inflight", "broken-chip")

    with pytest.raises(MOD.Refused) as e:
        MOD.repair_queued_copy(root, "broken-chip")

    assert "integrity" in str(e.value)
    assert queued.exists()


def test_repair_cli_prints_exactly_one_log_line(tmp_path):
    root = _lane(tmp_path)
    _stamped(root, "queued", "cli-chip", stamp=False)
    _stamped(root, "inflight", "cli-chip")

    out = subprocess.run([sys.executable, str(_TOOL), "repair-queued-copy", "cli-chip",
                          "--dir", str(root)], capture_output=True, text=True)

    assert out.returncode == 0
    assert out.stdout.splitlines() == [
        "re-deleted queued copy of cli-chip (restored by checkout/pull)"]


# ── land (D-PM10: the dispatcher's own moves, made durable) ───────────────────


def _land_repo(tmp_path: Path):
    """A repo with an `origin` the tests can advance independently of the checkout.

    Two real repos and a real fetch, because the whole point of `land` is what happens
    when the working checkout and its base DISAGREE — a stub would pin the stub.
    """
    origin = tmp_path / "origin"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work = _git_repo(tmp_path / "work")
    root = work / "internal" / "dispatch"
    for sub in ("queued", "inflight", "done", "reviews"):
        (root / sub).mkdir(parents=True)
        (root / sub / ".gitkeep").write_text("")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(work), "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(origin)],
                   check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "-u", "origin", "main"],
                   check=True)
    # A bare `git init` points HEAD at whatever its default branch name is, so a later
    # `git clone` of this origin checks out nothing ("remote HEAD refers to nonexistent
    # ref"). Point it at main so the second-session clone in _advance_origin is real.
    subprocess.run(["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"],
                   check=True)
    return work, root, origin


def _advance_origin(tmp_path: Path, origin: Path, mutate) -> None:
    """Commit something to origin/main from a SEPARATE clone, as another session would."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "--branch", "main", str(origin), str(other)],
                   check=True)
    for k, v in (("user.email", "o@example.com"), ("user.name", "o")):
        subprocess.run(["git", "-C", str(other), "config", k, v], check=True)
    mutate(other / "internal" / "dispatch")
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-qm", "other session"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "HEAD:main"],
                   check=True)


def test_land_reports_clean_when_the_checkout_changed_nothing(tmp_path):
    work, root, _ = _land_repo(tmp_path)
    res = _load_tool().land(root, dry_run=True)
    assert res["landed"] is False
    assert res["reason"] == "clean"
    assert res["changed"] == []


def test_land_sees_a_queued_to_inflight_move_as_one_add_and_one_delete(tmp_path):
    work, root, _ = _land_repo(tmp_path)
    mod = _load_tool()
    _write(root, "queued", "brief-a")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "queue it"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:main"],
                   check=True)
    mod.launch(root, "brief-a")          # the tick's own move
    res = mod.land(root, dry_run=True)
    assert res["reason"] == "dry-run"
    assert res["adds"] == ["internal/dispatch/inflight/brief-a.md"]
    assert res["deletes"] == ["internal/dispatch/queued/brief-a.md"]


def test_land_does_not_delete_files_this_checkout_merely_never_pulled(tmp_path):
    """The defect this verb was nearly shipped with.

    A dispatcher checkout is routinely behind. If the change set were computed against
    the BASE, every file committed by someone else since would read as "absent here,
    therefore deleted" and an unattended tick would land the deletion.
    """
    work, root, origin = _land_repo(tmp_path)
    _advance_origin(tmp_path, origin,
                    lambda d: (d / "reviews" / "pr-1.md").write_text("a review\n"))
    res = _load_tool().land(root, dry_run=True)
    assert res["reason"] == "clean", res
    assert res["deletes"] == [], "a file this checkout never pulled is not a deletion"


def test_land_refuses_a_path_the_base_moved_since_head(tmp_path):
    """The resurrection guard: base retired a brief, this checkout still edits it."""
    work, root, origin = _land_repo(tmp_path)
    mod = _load_tool()
    _write(root, "queued", "brief-b")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "queue b"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:main"],
                   check=True)

    def retire(d: Path) -> None:                       # another session finishes it
        (d / "done" / "brief-b.md").write_text((d / "queued" / "brief-b.md").read_text())
        (d / "queued" / "brief-b.md").unlink()
    _advance_origin(tmp_path, origin, retire)

    (root / "queued" / "brief-b.md").write_text(
        (root / "queued" / "brief-b.md").read_text() + "\nlocal edit\n")
    with pytest.raises(mod.Refused) as e:
        mod.land(root, dry_run=True)
    assert "behind" in str(e.value)
    assert "queued/brief-b.md" in str(e.value)


def test_land_refuses_an_entry_whose_body_lost_its_stamp(tmp_path):
    """A truncated brief must not be made durable — landing it spreads the loss."""
    work, root, _ = _land_repo(tmp_path)
    mod = _load_tool()
    p = _write(root, "queued", "brief-c")
    stamped, _ = mod.mdi.stamp(p.read_text())
    p.write_text(stamped)
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "stamped"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:main"],
                   check=True)
    head, _, _ = p.read_text().partition("\n---\n")
    p.write_text(head + "\n---\nTOTALLY DIFFERENT BODY\n")
    with pytest.raises(mod.Refused) as e:
        mod.land(root, dry_run=True)
    assert "integrity" in str(e.value).lower()


def test_land_ignores_paths_outside_the_four_state_dirs(tmp_path):
    """`git add <dir>` sweeps strays; this must not. A withdrawn brief and a README
    edit sitting in the dispatch dir are the operator's business, not a tick's."""
    work, root, _ = _land_repo(tmp_path)
    (root / "_withdrawn-something.md").write_text("not a lane entry\n")
    (root / "README.md").write_text("docs\n")
    res = _load_tool().land(root, dry_run=True)
    assert res["reason"] == "clean"
    assert res["changed"] == []


def test_land_never_touches_head_the_index_or_the_working_tree(tmp_path):
    """It builds its tree in a temp index, so a session mid-commit here is safe."""
    work, root, _ = _land_repo(tmp_path)
    mod = _load_tool()
    _write(root, "queued", "brief-d")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "queue d"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:main"],
                   check=True)
    mod.launch(root, "brief-d")
    before_head = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                                 capture_output=True, text=True).stdout
    before_status = _porcelain(work)
    mod.land(root, dry_run=True)
    assert subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout == before_head
    assert _porcelain(work) == before_status


def test_land_holds_an_inflight_position_once_the_chip_has_a_pr(tmp_path):
    """The #3828 residual guard.

    If `main` shows a brief in `inflight/`, a chip PR branched when `main` still had it
    in `queued/` applies its `queued/ -> done/` rename as a plain ADD and leaves the id
    in two dirs. So a brief that already has a chip PR keeps its position local — its
    own PR is the record.
    """
    work, root, _ = _land_repo(tmp_path)
    mod = _load_tool()
    _write(root, "queued", "brief-e")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "queue e"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:main"],
                   check=True)
    mod.launch(root, "brief-e")
    mod.bind(root, "brief-e", pr=4242)          # the chip opened its PR

    res = mod.land(root, dry_run=True)
    assert "internal/dispatch/inflight/brief-e.md" in res["skipped_inflight"]
    assert "internal/dispatch/inflight/brief-e.md" not in res["adds"]
    # ...and crucially the queued/ deletion is held back too, or the brief would be
    # deleted from the repo with its only copy in an uncommitted working tree.
    assert "internal/dispatch/queued/brief-e.md" not in res["deletes"]
    assert "internal/dispatch/queued/brief-e.md" in res["held_for_chip"]


def test_land_does_carry_a_prepared_but_unclicked_brief(tmp_path):
    """The gap this verb exists for: prepared, no PR, so nothing else will ever carry it."""
    work, root, _ = _land_repo(tmp_path)
    mod = _load_tool()
    _write(root, "queued", "brief-f")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "queue f"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:main"],
                   check=True)
    mod.launch(root, "brief-f")                 # prepared; never clicked, no PR

    res = mod.land(root, dry_run=True)
    assert res["adds"] == ["internal/dispatch/inflight/brief-f.md"]
    assert res["deletes"] == ["internal/dispatch/queued/brief-f.md"]
    assert res["skipped_inflight"] == []


import json  # noqa: E402

mdm = MOD


# ── steering tag: carrying a brief's `why` into its chip row ─────────────────
#
# Measured 2026-09-07: 581 of 648 chip rows carried no `why` key and ZERO carried
# `hardening:`, so the doctrine's ">=5 chips on one audit-ref" stop condition had
# never had an input. Nothing mechanical moved a tag from a brief to a ledger —
# `/launch`'s prose gate was the only path and the scheduled lane does not run it.

_TAGGED = ("---\n"
           "id: c1\naspect: substrate\ntitle: t\nprivileged: false\n"
           "created: 2026-09-07\npm: fable-cowork\n"
           "---\n"
           "WHY: hardening:#3566-D2 — the eighth per-site symlink gate.\n"
           "Body.\n")


def test_steering_tag_reads_the_why_line():
    why, source = mdm.steering_tag(_TAGGED)
    assert why == "hardening:#3566-D2 — the eighth per-site symlink gate."
    assert source == "why-line"


def test_steering_tag_prefers_the_front_matter_field():
    text = _TAGGED.replace("pm: fable-cowork\n", "pm: fable-cowork\nwhy: roadmap:doc §2a\n")
    why, source = mdm.steering_tag(text)
    assert why == "roadmap:doc §2a"
    assert source == "front-matter"


def test_steering_tag_normalises_the_tag_case_only():
    why, _ = mdm.steering_tag(_TAGGED.replace("WHY: hardening:", "WHY: Hardening:"))
    assert why.startswith("hardening:")


def test_prose_why_is_untagged_and_never_inferred():
    """Every brief in the live lane opens `WHY:` with prose like this. Classifying it
    would be the tool deciding roadmap-vs-defect-vs-hardening — the judgement the tag
    exists to record."""
    prose = _TAGGED.replace(
        "WHY: hardening:#3566-D2 — the eighth per-site symlink gate.",
        "WHY: operator, 2026-09-04, on the PoC bot: the Plugins tab shows Enabled.")
    assert mdm.steering_tag(prose) == (None, "untagged")


def test_a_bare_tag_with_no_ref_is_untagged():
    assert mdm.steering_tag(_TAGGED.replace("hardening:#3566-D2 — the eighth per-site "
                                            "symlink gate.", "hardening:"))[0] is None


def _why_ledger(tmp_path, row):
    d = tmp_path / "meta-state"
    d.mkdir(exist_ok=True)
    (d / "substrate.json").write_text(json.dumps({"chips": [row]}), encoding="utf-8")
    return d


def test_carry_why_writes_the_row(tmp_path):
    d = _why_ledger(tmp_path, {"id": "c1", "bucket": "dispatched"})
    result = {}
    mdm._carry_why("c1", _TAGGED, result, ledger_dir=str(d))
    assert result["why_written"] is True
    row = json.loads((d / "substrate.json").read_text())["chips"][0]
    assert row["why"].startswith("hardening:#3566-D2")


def test_carry_why_never_overwrites_a_stated_why(tmp_path):
    """A human's classification outranks a re-derivation from the file."""
    d = _why_ledger(tmp_path, {"id": "c1", "bucket": "dispatched", "why": "defect:mine"})
    result = {}
    mdm._carry_why("c1", _TAGGED, result, ledger_dir=str(d))
    assert result["why_written"] is False
    assert json.loads((d / "substrate.json").read_text())["chips"][0]["why"] == "defect:mine"


def test_carry_why_on_an_untagged_brief_writes_nothing_and_says_so(tmp_path):
    d = _why_ledger(tmp_path, {"id": "c1", "bucket": "dispatched"})
    prose = _TAGGED.replace("hardening:#3566-D2 — the eighth per-site symlink gate.",
                            "the operator asked for it.")
    result = {}
    mdm._carry_why("c1", prose, result, ledger_dir=str(d))
    assert result == {"why": None, "why_source": "untagged", "why_written": False}
    assert "why" not in json.loads((d / "substrate.json").read_text())["chips"][0]


def test_carry_why_is_a_noop_when_no_row_exists_yet(tmp_path):
    """The launch-time case: the dispatcher writes the row AFTER the move, so launch
    reports the tag and a later `bind`/`start` backfills it."""
    result = {}
    mdm._carry_why("nosuchchip", _TAGGED, result, ledger_dir=str(tmp_path / "meta-state"))
    assert result["why"].startswith("hardening:") and result["why_written"] is False


def test_the_untagged_summary_line_is_loud():
    assert "UNTAGGED" in mdm._why_line({"why": None, "why_source": "untagged"})
    assert "hardening:#1" in mdm._why_line(
        {"why": "hardening:#1", "why_source": "why-line", "why_written": True})
    assert mdm._why_line({}) == ""          # verbs that never ran _carry_why stay quiet


# ── one standing branch, an updated PR, and a fast-forward at tick start ──────
#
# WHY THESE EXIST. `land` used to push `lane/state-<UTC stamp>`, so every tick that moved
# anything opened a NEW PR — and the reconciler would merge none of them, because a lane
# PR has no chip row and its headless unledgered-orphan rule refuses rather than guesses.
# The two shapes compose into a flood: 19 byte-identical one-line PRs over eleven sweeps
# (#4046-#4065, 2026-09-05/06), and five two days before that. Separately, every merge on
# main left the dispatcher's clone behind, so the next tick refused to land ("Pull, then
# land") until a human typed `git pull` — a human step in an unattended loop.


class _FakeGh:
    """A `gh` on PATH that answers from a scripted table and RECORDS every call.

    A real `gh` cannot run here (no network, no repo on GitHub), and mocking
    `subprocess.run` module-wide would also swallow the git calls that are the point of
    the fixture. A tiny executable on PATH keeps `land`'s own subprocess plumbing —
    argument shapes, exit codes, stdout parsing — under test.
    """

    def __init__(self, tmp_path: Path, open_prs=None, create_url="https://x/pr/9"):
        self.dir = tmp_path / "fakebin"
        self.dir.mkdir(exist_ok=True)
        self.calls = self.dir / "calls.txt"
        self.calls.write_text("")
        state = self.dir / "state.json"
        state.write_text(json.dumps({"open": open_prs or [], "url": create_url}))
        (self.dir / "gh").write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, pathlib\n"
            "here = pathlib.Path(__file__).parent\n"
            "argv = sys.argv[1:]\n"
            "with (here / 'calls.txt').open('a') as f:\n"
            "    f.write('\\x1f'.join(argv) + '\\n')\n"
            "st = json.loads((here / 'state.json').read_text())\n"
            "if argv[:3] == ['pr', 'list', '--head']:\n"
            "    head = argv[3]\n"
            "    print(json.dumps([{k: v for k, v in r.items() if k in ('number', 'url')}\n"
            "                      for r in st['open'] if r['headRefName'] == head]))\n"
            "elif argv[:2] == ['pr', 'list']:\n"
            "    print(json.dumps(st['open']))\n"
            "elif argv[:2] == ['pr', 'create']:\n"
            "    print(st['url'])\n"
            "elif argv[:2] == ['pr', 'edit']:\n"
            "    print('edited')\n"
            "else:\n"
            "    sys.exit(9)\n")
        (self.dir / "gh").chmod(0o755)

    def recorded(self):
        return [ln.split("\x1f") for ln in self.calls.read_text().splitlines() if ln]

    def install(self, monkeypatch):
        monkeypatch.setenv("PATH", "%s:%s" % (self.dir, os.environ.get("PATH", "")))
        return self


def _seed_queued(work: Path, root: Path, brief_id: str) -> None:
    """Commit + push one queued brief, so the tick's own move is the only delta."""
    _write(root, "queued", brief_id)
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "queue %s" % brief_id],
                   check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:main"],
                   check=True)


def test_land_pushes_the_standing_branch_not_a_stamped_one(tmp_path, monkeypatch):
    work, root, _ = _land_repo(tmp_path)
    mod = _load_tool()
    _seed_queued(work, root, "brief-s1")
    mod.launch(root, "brief-s1")
    _FakeGh(tmp_path).install(monkeypatch)

    res = mod.land(root)
    assert res["branch"] == mod.LANE_STATE_BRANCH == "lane/state"
    assert subprocess.run(["git", "-C", str(work), "ls-remote", "--heads", "origin",
                           "refs/heads/lane/state"],
                          capture_output=True, text=True).stdout.strip()


def test_a_second_tick_force_pushes_the_same_branch_and_reuses_the_pr(tmp_path,
                                                                     monkeypatch):
    """THE FLOOD, prevented. Two ticks, two lane deltas, ONE pull request."""
    work, root, _ = _land_repo(tmp_path)
    mod = _load_tool()
    _seed_queued(work, root, "brief-s2")
    _seed_queued(work, root, "brief-s3")
    gh = _FakeGh(tmp_path).install(monkeypatch)

    mod.launch(root, "brief-s2")
    first = mod.land(root)
    assert first["pr_reused"] is False

    # The PR the first tick opened is now open on the standing branch.
    (gh.dir / "state.json").write_text(json.dumps(
        {"open": [{"number": 4046, "headRefName": "lane/state",
                   "url": "https://x/pr/4046", "createdAt": "2026-09-05T00:00:00Z"}],
         "url": "https://x/pr/9"}))
    mod.launch(root, "brief-s3")
    second = mod.land(root)

    assert second["pr_reused"] is True and second["pr"] == 4046
    assert second["forced"] is True, "the standing branch is replaced, so the push forces"
    creates = [c for c in gh.recorded() if c[:2] == ["pr", "create"]]
    assert len(creates) == 1, "a second `gh pr create` is the flood: %r" % creates
    assert any(c[:2] == ["pr", "edit"] for c in gh.recorded())


def test_land_refuses_to_force_push_over_a_commit_it_did_not_write(tmp_path, monkeypatch):
    """The lease alone CANNOT protect this, and the first version of these tests only
    asserted that the flag string was present — which passes while the branch is
    discarded anyway. `--force-with-lease` asks "is the remote where I last saw it"; it
    says nothing about WHOSE commit is there. So a fix-forward chip's CI fix pushed onto
    `lane/state` is exactly what the next tick would erase, with `forced: true` and no
    warning. The trailer is what tells the previous TICK's commit (safe to replace) from
    somebody else's (never)."""
    work, root, _ = _land_repo(tmp_path)
    mod = _load_tool()
    _seed_queued(work, root, "brief-lease")
    mod.launch(root, "brief-lease")
    _FakeGh(tmp_path).install(monkeypatch)
    mod.land(root, open_pr=False)                       # tick 1 creates lane/state
    theirs = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()

    # Somebody pushes a commit of their own onto the standing branch.
    subprocess.run(["git", "-C", str(work), "push", "-q", "-f", "origin",
                    "%s:refs/heads/lane/state" % theirs], check=True)
    _write(root, "queued", "brief-lease2")
    with pytest.raises(mod.Refused) as e:
        mod.land(root, open_pr=False)
    assert "carries no" in str(e.value) and mod.LAND_TRAILER in str(e.value)
    # ...and their commit is still the branch tip.
    assert subprocess.run(["git", "-C", str(work), "ls-remote", "--heads", "origin",
                           "refs/heads/lane/state"], capture_output=True,
                          text=True).stdout.startswith(theirs)


def test_the_lease_names_the_sha_read_before_the_work_not_after(tmp_path, monkeypatch):
    """Read at push time the lease is "whatever the remote holds right now" — it matches
    on every push but a sub-second race, so it is decorative. It has to name the sha this
    run based its decisions on."""
    work, root, _ = _land_repo(tmp_path)
    mod = _load_tool()
    _seed_queued(work, root, "brief-lease3")
    mod.launch(root, "brief-lease3")
    _FakeGh(tmp_path).install(monkeypatch)
    first = mod.land(root, open_pr=False)

    seen, real = {}, mod._run

    def spy(args, cwd):
        if args[:2] == ["git", "push"]:
            seen["args"] = list(args)
        return real(args, cwd)
    monkeypatch.setattr(mod, "_run", spy)
    _write(root, "queued", "brief-lease4")
    second = mod.land(root, open_pr=False)

    assert second["replaced"] == first["commit"], "the lease must name tick 1's commit"
    assert ("--force-with-lease=refs/heads/lane/state:%s" % first["commit"]
            in seen["args"]), seen["args"]


def test_land_never_lands_a_review_file(tmp_path, monkeypatch):
    """Producer and qualifier have to agree. While `land` swept `reviews/` in, one loose
    review file in the dev checkout rode onto the standing branch and made the PR
    permanently unqualifiable — one PR that never merges and, being one, never doubles,
    so is never re-poked either."""
    work, root, _ = _land_repo(tmp_path)
    mod = _load_tool()
    (root / "reviews" / "pr-4242.md").write_text("Verdict: PASS — the PM's artifact.\n")
    _seed_queued(work, root, "brief-rv")
    mod.launch(root, "brief-rv")
    res = mod.land(root, dry_run=True)
    assert all("/reviews/" not in p for p in res["changed"]), res["changed"]
    assert "reviews" not in mod.LANE_STATE_DIRS


def test_a_gh_lookup_failure_never_becomes_a_second_pr(tmp_path, monkeypatch):
    """"Never a second PR" has to survive a flaky `gh`: a lookup that returns None for
    both "no PR open" and "gh failed" routes the failure straight into `gh pr create`."""
    work, root, _ = _land_repo(tmp_path)
    mod = _load_tool()
    _seed_queued(work, root, "brief-flaky")
    mod.launch(root, "brief-flaky")
    gh = _FakeGh(tmp_path).install(monkeypatch)
    (gh.dir / "gh").write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        "here = pathlib.Path(__file__).parent\n"
        "with (here / 'calls.txt').open('a') as f:\n"
        "    f.write('\\x1f'.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['pr', 'list']:\n"
        "    sys.stderr.write('gh: API rate limit exceeded\\n'); sys.exit(1)\n"
        "print('https://x/pr/1')\n")
    (gh.dir / "gh").chmod(0o755)

    res = mod.land(root)
    assert res["landed"] is True, "the branch still carries the commit"
    assert res.get("pr_error")
    assert not any(c[:2] == ["pr", "create"] for c in gh.recorded()), gh.recorded()


def test_a_greenlight_is_titled_a_greenlight_and_never_bookkeeping(tmp_path):
    """#4046 read "dispatcher bookkeeping" and its one line was `operator_greenlight:
    true` on a privileged brief — an authorization dressed as housekeeping."""
    mod = _load_tool()
    changes = [{"path": "internal/dispatch/queued/priv-brief.md", "id": "priv-brief",
                "keys": ["operator_greenlight"], "before": {"privileged": "true"},
                "after": {"privileged": "true", "operator_greenlight": "true"}}]
    subject = mod.land_subject(mod.classify_lane_changes(changes), 1)
    assert subject == "lane: greenlight priv-brief"
    assert "bookkeeping" not in subject.lower()
    _, body = mod._land_message({"internal/dispatch/queued/priv-brief.md": "sha"}, [],
                                "origin/main", "20260906T000000Z", changes=changes)
    assert "**Operator greenlight**" in body
    assert mod.LAND_TRAILER in body


@pytest.mark.parametrize("changes,expected", [
    ([{"path": "p", "id": "chip-a", "keys": ["pr"], "before": {},
       "after": {"pr": "4100"}}], "lane: bind chip-a -> #4100"),
    ([{"path": "p", "id": "chip-b", "keys": ["launch"], "before": {"launch": "prepared"},
       "after": {"launch": "started"}}], "lane: state — 1 lane change"),
])
def test_the_title_prefix_follows_the_content(changes, expected):
    mod = _load_tool()
    assert mod.land_subject(mod.classify_lane_changes(changes), 1) == expected


def test_a_greenlight_outranks_a_bind_in_the_same_land(tmp_path):
    """Consequence order, not diff order: the most consequential write names the PR."""
    mod = _load_tool()
    changes = [
        {"path": "a", "id": "chip-a", "keys": ["pr"], "before": {}, "after": {"pr": "1"}},
        {"path": "b", "id": "chip-g", "keys": ["operator_greenlight"], "before": {},
         "after": {"operator_greenlight": "true"}},
    ]
    assert mod.land_subject(mod.classify_lane_changes(changes), 2).startswith(
        "lane: greenlight chip-g")


def test_the_body_names_the_front_matter_keys_each_file_moved(tmp_path, monkeypatch):
    work, root, _ = _land_repo(tmp_path)
    mod = _load_tool()
    _seed_queued(work, root, "brief-k")
    mod.launch(root, "brief-k")                  # stamps dispatched/session/launch...
    _FakeGh(tmp_path).install(monkeypatch)
    res = mod.land(root, dry_run=True)
    assert res["subject"].startswith("lane: state")

    # ...and the same delta, rendered, names the keys rather than a diff.
    changes = [{"path": "internal/dispatch/inflight/brief-k.md", "id": "brief-k",
                "keys": ["body_sha256", "launch"], "before": {}, "after": {}}]
    _, body = mod._land_message({"internal/dispatch/inflight/brief-k.md": "s"}, [],
                                "origin/main", "T", changes=changes)
    assert "(front matter: body_sha256, launch)" in body


def test_the_commit_carries_the_dispatcher_trailer(tmp_path, monkeypatch):
    """The qualifier's identity check reads this; the git AUTHOR is the operator's."""
    work, root, _ = _land_repo(tmp_path)
    mod = _load_tool()
    _seed_queued(work, root, "brief-t")
    mod.launch(root, "brief-t")
    _FakeGh(tmp_path).install(monkeypatch)
    res = mod.land(root, open_pr=False)
    msg = subprocess.run(["git", "-C", str(work), "log", "-1", "--format=%B",
                          res["commit"]], capture_output=True, text=True).stdout
    assert mod.LAND_TRAILER in msg
    assert mod.LAND_TRAILER == mod.mdi.LAND_TRAILER, "one definition, two readers"


# ── the re-arming poke ───────────────────────────────────────────────────────


@pytest.mark.parametrize("n,bucket", [(0, 0), (1, 1), (2, 2), (3, 2), (4, 4),
                                      (7, 4), (8, 8), (19, 16)])
def test_the_poke_signature_re_arms_only_on_a_doubling(n, bucket):
    """Edge-triggering announced the first PR and then went silent through eighteen
    more. Bucketing by doubling re-arms the signature at 1, 2, 4, 8, 16 — logarithmic
    in the pile, not one notification per tick."""
    mod = _load_tool()
    assert mod.poke_bucket(n) == bucket


def test_a_pile_that_merely_persists_is_still_poked_exactly_once():
    mod = _load_tool()
    assert mod.poke_bucket(5) == mod.poke_bucket(6) == mod.poke_bucket(7) == 4


def test_one_open_lane_pr_is_the_healthy_state_and_is_not_poked(tmp_path):
    """The standing branch's whole point is that exactly one lane PR is open. A poke
    that fires on the healthy state is a poke the operator learns to ignore."""
    mod = _load_tool()
    assert mod.lane_pr_poke_signature(0, None) is None
    assert mod.lane_pr_poke_signature(1, 3) is None


def test_a_standing_pr_the_sweep_never_merges_is_poked_and_re_arms_as_it_ages(tmp_path):
    """One PR unmerged for a day is ~twelve refusals in a row: the rule is not firing.
    Keyed by an age bucket so it keeps re-arming instead of being reported once."""
    mod = _load_tool()
    assert mod.lane_pr_poke_signature(1, 23) is None
    assert mod.lane_pr_poke_signature(1, 24) == "lane-state-pr-stale:24h"
    assert mod.lane_pr_poke_signature(1, 47) == "lane-state-pr-stale:24h"
    assert mod.lane_pr_poke_signature(1, 48) == "lane-state-pr-stale:48h"
    assert mod.lane_pr_poke_signature(1, 200) == "lane-state-pr-stale:192h"


@pytest.mark.parametrize("n,sig", [(2, 2), (3, 2), (4, 4), (19, 16)])
def test_a_pile_of_two_or_more_is_the_flood_shape_and_re_arms_on_doubling(n, sig):
    mod = _load_tool()
    assert mod.lane_pr_poke_signature(n, 1) == "lane-state-prs-unmerged:%d" % sig


def test_the_census_counts_legacy_per_tick_branches_too(tmp_path, monkeypatch):
    """The 19 that are already open are `lane/state-<stamp>`, not `lane/state`."""
    work, root, _ = _land_repo(tmp_path)
    mod = _load_tool()
    assert mod.is_lane_state_branch("lane/state")
    assert mod.is_lane_state_branch("lane/state-20260905T101112Z")
    assert not mod.is_lane_state_branch("claude/some-chip")
    _FakeGh(tmp_path, open_prs=[
        {"number": 4046, "headRefName": "lane/state-20260905T000000Z",
         "createdAt": "2026-09-05T00:00:00Z", "url": "u1"},
        {"number": 4065, "headRefName": "lane/state",
         "createdAt": "2026-09-06T00:00:00Z", "url": "u2"},
        {"number": 4070, "headRefName": "claude/unrelated",
         "createdAt": "2026-09-06T00:00:00Z", "url": "u3"},
    ]).install(monkeypatch)
    census = mod._lane_pr_census(work, now=_utc(2026, 9, 6, 12))
    assert census["open"] == 2
    assert census["oldest_hours"] == 36
    assert census["poke_signature"] == "lane-state-prs-unmerged:2"   # 2 = a pile
    assert mod.lane_pr_line(census) == "lane PRs open: 2 (oldest 36h)"


def test_the_report_line_is_emitted_on_a_clean_tick_too(tmp_path, monkeypatch):
    """A pile is least visible on exactly the quiet ticks, so the census runs anyway."""
    work, root, _ = _land_repo(tmp_path)
    mod = _load_tool()
    _FakeGh(tmp_path, open_prs=[
        {"number": 4046, "headRefName": "lane/state", "createdAt": "2026-09-05T00:00:00Z",
         "url": "u"}]).install(monkeypatch)
    res = mod.land(root)
    assert res["reason"] == "clean"
    assert res["lane_prs"]["open"] == 1
    assert res["lane_pr_line"].startswith("lane PRs open: 1")


def test_a_gh_failure_costs_the_census_never_the_land(tmp_path, monkeypatch):
    work, root, _ = _land_repo(tmp_path)
    mod = _load_tool()
    broken = tmp_path / "brokenbin"
    broken.mkdir()
    (broken / "gh").write_text("#!/bin/sh\necho 'gh: not logged in' >&2\nexit 1\n")
    (broken / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", "%s:%s" % (broken, os.environ.get("PATH", "")))
    res = mod.land(root)
    assert res["reason"] == "clean"
    assert res["lane_prs"]["error"]
    assert "unknown" in res["lane_pr_line"]


# ── sync: the fast-forward precondition table ────────────────────────────────


def _sync_repo(tmp_path: Path):
    work, root, origin = _land_repo(tmp_path)
    subprocess.run(["git", "-C", str(work), "branch",
                    "--set-upstream-to=origin/main", "main"], check=True)
    return work, root, origin


def test_sync_pulls_on_main_with_a_clean_tree(tmp_path):
    work, root, origin = _sync_repo(tmp_path)
    mod = _load_tool()
    _advance_origin(tmp_path, origin,
                    lambda d: (d / "reviews" / "pr-1.md").write_text("a review\n"))
    before = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    res = mod.sync(root)
    assert res["pulled"] is True
    assert res["after"] != before
    assert (root / "reviews" / "pr-1.md").is_file()


def test_sync_pulls_with_only_the_ticks_own_lane_delta_dirty(tmp_path):
    """The delta the dispatcher itself wrote is not somebody else's work — and it
    survives the fast-forward."""
    work, root, origin = _sync_repo(tmp_path)
    mod = _load_tool()
    _seed_queued(work, root, "brief-ff")
    mod.launch(root, "brief-ff")                  # queued/ deleted, inflight/ untracked
    _advance_origin(tmp_path, origin,
                    lambda d: (d / "reviews" / "pr-2.md").write_text("r\n"))
    res = mod.sync(root)
    assert res["pulled"] is True, res
    assert (root / "inflight" / "brief-ff.md").is_file(), "the lane delta survived"


def test_sync_refuses_on_a_feature_branch(tmp_path):
    work, root, _ = _sync_repo(tmp_path)
    mod = _load_tool()
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", "claude/some-chip"],
                   check=True)
    res = mod.sync(root)
    assert res["pulled"] is False and res["reason"] == "wrong-branch"
    assert "claude/some-chip" in res["message"]


def test_sync_refuses_on_a_staged_index(tmp_path):
    work, root, _ = _sync_repo(tmp_path)
    mod = _load_tool()
    (work / "somefile.txt").write_text("half a commit\n")
    subprocess.run(["git", "-C", str(work), "add", "somefile.txt"], check=True)
    res = mod.sync(root)
    assert res["pulled"] is False and res["reason"] == "staged"
    assert "somefile.txt" in res["message"]


def test_a_staged_lane_move_is_the_ticks_own_and_does_not_block_the_pull(tmp_path):
    """`launch` on a TRACKED brief stages the move (`git rm` + `git add`). An unscoped
    "index must be clean" would refuse on exactly the ticks this verb exists for."""
    work, root, origin = _sync_repo(tmp_path)
    mod = _load_tool()
    _seed_queued(work, root, "brief-staged")
    mod.launch(root, "brief-staged")
    staged = subprocess.run(["git", "-C", str(work), "diff", "--cached", "--name-only"],
                            capture_output=True, text=True).stdout
    assert "internal/dispatch/" in staged, "fixture premise: the move IS staged"
    _advance_origin(tmp_path, origin,
                    lambda d: (d / "reviews" / "pr-9.md").write_text("r\n"))
    assert mod.sync(root)["pulled"] is True


def test_sync_refuses_when_a_tracked_file_outside_the_lane_is_modified(tmp_path):
    work, root, _ = _sync_repo(tmp_path)
    mod = _load_tool()
    (work / "README.md").write_text("original\n")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "readme"], check=True)
    (work / "README.md").write_text("a human is mid-edit\n")
    res = mod.sync(root)
    assert res["pulled"] is False and res["reason"] == "dirty"
    assert "README.md" in res["message"]


def test_sync_reports_the_path_when_the_same_file_moved_upstream(tmp_path):
    """The one case a fast-forward cannot carry: base moved the very file this
    checkout is holding a local edit to. Refuse, and NAME it."""
    work, root, origin = _sync_repo(tmp_path)
    mod = _load_tool()
    _seed_queued(work, root, "brief-clash")
    _advance_origin(tmp_path, origin, lambda d: (d / "queued" / "brief-clash.md")
                    .write_text("---\nid: brief-clash\n---\nupstream rewrote it\n"))
    (root / "queued" / "brief-clash.md").write_text(
        "---\nid: brief-clash\n---\nlocal edit\n")
    res = mod.sync(root)
    assert res["pulled"] is False and res["reason"] == "pull-failed"
    assert "brief-clash" in " ".join(res["paths"]) or "brief-clash" in res["message"]


def test_sync_never_touches_anything_when_it_refuses(tmp_path):
    work, root, _ = _sync_repo(tmp_path)
    mod = _load_tool()
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", "feature"],
                   check=True)
    before_head = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                                 capture_output=True, text=True).stdout
    before_status = _porcelain(work)
    mod.sync(root)
    assert subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout == before_head
    assert _porcelain(work) == before_status


def test_sync_dry_run_reports_the_verdict_and_pulls_nothing(tmp_path):
    work, root, origin = _sync_repo(tmp_path)
    mod = _load_tool()
    _advance_origin(tmp_path, origin,
                    lambda d: (d / "reviews" / "pr-3.md").write_text("r\n"))
    res = mod.sync(root, dry_run=True)
    assert res["pulled"] is False and res["reason"] == "dry-run"
    assert not (root / "reviews" / "pr-3.md").exists()


def test_sync_cli_exits_zero_on_a_refusal_it_can_explain(tmp_path):
    """A refusal is the tick's NORMAL outcome on a dirty dev checkout, not a failure —
    a non-zero exit would read as a broken helper and stop the run."""
    work, root, _ = _sync_repo(tmp_path)
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", "feature"],
                   check=True)
    out = subprocess.run([sys.executable, str(_TOOL), "sync", "--dir", str(root),
                          "--json"], capture_output=True, text=True)
    assert out.returncode == 0
    assert json.loads(out.stdout)["reason"] == "wrong-branch"


def test_sync_refuses_on_a_detached_head(tmp_path):
    """`git rev-parse --abbrev-ref HEAD` answers the literal "HEAD" when detached, so
    without a case for it the refusal names a branch that does not exist."""
    work, root, _ = _sync_repo(tmp_path)
    mod = _load_tool()
    sha = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "--detach", sha],
                   check=True)
    res = mod.sync(root)
    assert res["pulled"] is False and res["reason"] == "detached"
    assert res["head"] is None


# ── state ────────────────────────────────────────────────────────────────────
#
# The verb exists for a STALL, not a loss. All three scheduled procedures ended by
# writing `~/.claude/meta-<lane>/last-seen.json` with the `Write` tool, and the
# harness classes `~/.claude/**` as sensitive: it raises its own approval prompt
# there regardless of any grant, `additionalDirectories`, or `bypassPermissions`.
# An unattended tick has nobody to click it, so it did its real work and then hung
# on the bookkeeping, holding the scheduler's slot (operator, 2026-09-04). So what
# these tests pin is the three properties that make an argument a safe replacement
# for a file write: nothing already in the file is lost, nothing malformed is
# written, and the run does not have to supply a clock it does not have.


def _state(tmp_path):
    return tmp_path / "lane"


def _read_state(tmp_path):
    return json.loads((_state(tmp_path) / "last-seen.json").read_text(encoding="utf-8"))


def test_state_writes_the_object_and_creates_the_dir(tmp_path):
    out = MOD.state("dispatch", {"poked": ["1:a"], "prepared_but_unmoved": []},
                    state_dir_override=_state(tmp_path))
    assert Path(out["path"]).is_file()
    assert _read_state(tmp_path)["poked"] == ["1:a"]


def test_state_stamps_last_run_in_utc_only_when_absent(tmp_path):
    """The run has no clock — there is no `date` grant and the harness hands it a date
    with no time — so a stamp it composes itself is a guess. But a caller that HAS one
    (the dispatcher keeps `now` from meta-dispatch-eligible; the reconciler's `last_run`
    is a whole prose paragraph opening with a timestamp) must have it honoured verbatim."""
    out = MOD.state("dispatch", {"poked": []}, state_dir_override=_state(tmp_path),
                    now=_utc(2026, 9, 7, 17, 5, 5))
    assert out["stamped_last_run"] is True
    assert _read_state(tmp_path)["last_run"] == "2026-09-07T17:05:05Z"

    prose = "2026-09-07T17:2xZ - sweep 269. ACTED: 0 merged / 0 relaunched."
    out = MOD.state("reconcile", {"last_run": prose, "poked": []},
                    state_dir_override=_state(tmp_path), now=_utc(2026, 9, 7, 18, 0, 0))
    assert out["stamped_last_run"] is False
    assert _read_state(tmp_path)["last_run"] == prose


def test_state_refuses_malformed_json_and_writes_nothing(tmp_path):
    d = _state(tmp_path)
    MOD.state("dispatch", {"poked": ["1:a"]}, state_dir_override=d)
    before = (d / "last-seen.json").read_bytes()
    for bad in ("{nope", "[1,2]", '"a string"', "null", "17"):
        with pytest.raises(MOD.Refused):
            MOD._parse_state_arg(bad, "json")
    assert (d / "last-seen.json").read_bytes() == before


def test_state_refuses_a_known_key_of_the_wrong_shape(tmp_path):
    """The validation earns its place on the KNOWN keys only: a `poked` written as a
    string breaks the edge-triggered poke silently, because every reader treats an
    unusable value as 'nothing poked yet' and re-pokes."""
    with pytest.raises(MOD.Refused, match="`poked` must be list"):
        MOD.state("dispatch", {"poked": "1:a"}, state_dir_override=_state(tmp_path))
    with pytest.raises(MOD.Refused, match="`last_run` must be str"):
        MOD.state("coherence", {"last_run": {"t": 1}}, state_dir_override=_state(tmp_path))
    assert not (_state(tmp_path) / "last-seen.json").exists()


def test_state_keeps_keys_no_schema_knows_about(tmp_path):
    """The live reconcile file carries 24 keys, 15 of them dated one-off operator notes.
    A writer that dropped what it did not recognize would destroy most of the file."""
    out = MOD.state("reconcile",
                    {"last_run": "x", "sweep257_operator_directed": "do not relaunch",
                     "grants_verdict": {"meta-queue": "installed"}},
                    state_dir_override=_state(tmp_path))
    assert out["state"]["sweep257_operator_directed"] == "do not relaunch"
    assert _read_state(tmp_path)["grants_verdict"] == {"meta-queue": "installed"}


def test_merge_appends_one_signature_without_dropping_the_rest(tmp_path):
    """THE case the flag exists for. Step 4b adds one `prepared_but_unmoved` entry and
    step 6 one poke signature; a Read-then-Write to add one key is the shape this verb
    was built to remove, so `--merge` has to be able to do it blind."""
    d = _state(tmp_path)
    MOD.state("dispatch", {"last_run": "2026-09-07T17:00:00Z", "poked": ["1:a", "1:b"],
                           "prepared_but_unmoved": []}, state_dir_override=d)
    out = MOD.state("dispatch", {"poked": ["1:c"]}, merge=True, state_dir_override=d)

    assert out["state"]["poked"] == ["1:a", "1:b", "1:c"]       # appended, order kept
    assert out["state"]["last_run"] == "2026-09-07T17:00:00Z"   # untouched
    assert out["state"]["prepared_but_unmoved"] == []           # untouched
    assert out["stamped_last_run"] is False


def test_merge_is_idempotent_and_merges_dicts_shallowly(tmp_path):
    """A tick that re-runs the same merge must not double the signature: the state file
    is read by the edge-triggered poke, and a duplicate reads as a second finding."""
    d = _state(tmp_path)
    MOD.state("reconcile", {"last_run": "t", "poked": ["red:1"],
                            "fix_counts": {"4083": 1}}, state_dir_override=d)
    MOD.state("reconcile", {"poked": ["red:1"]}, merge=True, state_dir_override=d)
    out = MOD.state("reconcile", {"fix_counts": {"4088": 1}}, merge=True,
                    state_dir_override=d)
    assert out["state"]["poked"] == ["red:1"]
    assert out["state"]["fix_counts"] == {"4083": 1, "4088": 1}


def test_merge_refuses_rather_than_dropping_an_unreadable_file(tmp_path):
    """A merge into a file that does not parse would silently drop every key it holds —
    and this file is mostly keys no schema knows about. Refuse; the repair is a
    deliberate whole-object `--json` write, not a side effect of a one-key append."""
    d = _state(tmp_path)
    d.mkdir(parents=True)
    (d / "last-seen.json").write_text("{ truncated", encoding="utf-8")
    with pytest.raises(MOD.Refused, match="does not parse"):
        MOD.state("dispatch", {"poked": ["1:a"]}, merge=True, state_dir_override=d)
    assert (d / "last-seen.json").read_text() == "{ truncated"

    out = MOD.state("dispatch", {"poked": ["1:a"]}, state_dir_override=d)
    assert out["replaced_unparseable"] is True and _read_state(tmp_path)["poked"] == ["1:a"]


def test_state_writes_atomically_via_rename(tmp_path):
    """Several readers on a 30-minute and 2-hour cadence, no lock: a partial write is a
    file that parses as nothing, which every reader treats as 'no evidence' and skips."""
    import inspect
    src = inspect.getsource(MOD._write_state_file)
    assert "os.replace" in src and "mkstemp" in src
    # ...and the temp file is created in the DESTINATION dir, or the rename is not atomic
    assert "dir=str(path.parent)" in src

    d = _state(tmp_path)
    MOD.state("dispatch", {"poked": []}, state_dir_override=d)
    assert [q.name for q in d.iterdir()] == ["last-seen.json"]   # no .tmp left behind


def test_state_refuses_an_unknown_lane(tmp_path):
    with pytest.raises(MOD.Refused, match="unknown lane"):
        MOD.state("nope", {}, state_dir_override=_state(tmp_path))


def test_pm_landing_is_not_derived_from_the_lane_name():
    """Every other lane lives at ~/.claude/meta-<lane>/; `pm-landing` does not. Deriving
    the path would write ~/.claude/meta-pm-landing/ and leave the real file unread."""
    assert MOD.state_dir("pm-landing").name == "pm-landing"
    assert MOD.state_dir("dispatch").name == "meta-dispatch"


def test_state_cli_prints_what_it_wrote_and_refuses_with_exit_2(tmp_path, capsys):
    d = str(_state(tmp_path))
    assert MOD.main(["state", "--lane", "coherence", "--state-dir", d,
                     "--json", '{"signatures": ["drift:apps"]}']) == 0
    printed = capsys.readouterr().out
    assert '"drift:apps"' in printed and "wrote (replace)" in printed

    assert MOD.main(["state", "--lane", "coherence", "--state-dir", d,
                     "--merge", '{"signatures": ["orphan:claude/x"]}']) == 0
    assert MOD.main(["state", "--lane", "coherence", "--state-dir", d,
                     "--json", "{oops"]) == 2
    capsys.readouterr()
    assert _read_state(tmp_path)["signatures"] == ["drift:apps", "orphan:claude/x"]


def test_cli_refuses_a_write_dir_outside_dot_claude_with_exit_2(
        tmp_path, monkeypatch, capsys):
    """The grant is `Bash(python3 tools/meta-dispatch-move:*)` — a wildcard over
    ARGUMENTS — so a `--state-dir` a run supplies is a path a run supplies, which is the
    one shape this verb exists to remove. Both destination flags are pinned under
    `~/.claude/` and refuse with exit 2, writing nothing, when they are not.
    """
    monkeypatch.delenv(MOD.DIR_ESCAPE_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    outside = tmp_path / "anywhere"
    assert MOD.main(["state", "--lane", "dispatch", "--state-dir", str(outside),
                     "--json", '{"poked": ["1:a"]}']) == 2
    assert not outside.exists(), "refused, so it must not even have created the dir"
    assert "outside" in capsys.readouterr().err

    log = tmp_path / "elsewhere"
    assert MOD.main(["heartbeat", "--log-dir", str(log)]) == 2
    assert not log.exists()
    assert "outside" in capsys.readouterr().err


def test_cli_refuses_a_symlink_out_of_dot_claude(tmp_path, monkeypatch):
    """Resolved on both sides, so a link planted inside the root does not launder a
    path past a literal-prefix check."""
    monkeypatch.delenv(MOD.DIR_ESCAPE_ENV, raising=False)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    escape = tmp_path / "escape"
    escape.mkdir()
    (home / ".claude" / "bolthole").symlink_to(escape)

    assert MOD.main(["state", "--lane", "dispatch",
                     "--state-dir", str(home / ".claude" / "bolthole"),
                     "--json", '{"poked": []}']) == 2
    assert not (escape / "last-seen.json").exists()


def test_cli_expands_a_tilde_write_dir_under_home(tmp_path, monkeypatch):
    """`meta-reconcile` step 7 passes `--log-dir '~/.claude/meta-reconcile/log'` in
    SINGLE QUOTES, so the shell hands the tilde through literally; the tool must expand
    it or the reconcile log lands at `<cwd>/~/.claude/...` on its first granted run.
    Same for `--state-dir`. Both are checked with the root check LIVE — a tilde form
    resolves under `$HOME/.claude/`, so it needs no escape hatch.
    """
    monkeypatch.delenv(MOD.DIR_ESCAPE_ENV, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)

    assert MOD.main(["heartbeat", "--log-dir", "~/.claude/meta-reconcile/log"]) == 0
    logs = sorted((home / ".claude" / "meta-reconcile" / "log").glob("*.jsonl"))
    assert len(logs) == 1, "the tilde must expand under $HOME, not become a directory"

    assert MOD.main(["state", "--lane", "reconcile",
                     "--state-dir", "~/.claude/meta-reconcile",
                     "--json", '{"poked": ["1:a"]}']) == 0
    assert (home / ".claude" / "meta-reconcile" / "last-seen.json").is_file()
    assert not (tmp_path / "~").exists()


def test_state_cli_requires_exactly_one_of_json_or_merge(tmp_path):
    d = str(_state(tmp_path))
    for argv in (["state", "--lane", "dispatch", "--state-dir", d],
                 ["state", "--lane", "dispatch", "--state-dir", d,
                  "--json", "{}", "--merge", "{}"]):
        with pytest.raises(SystemExit) as e:
            MOD.main(argv)
        assert e.value.code == 2


def test_heartbeat_counts_vocabulary_can_be_replaced_wholesale(tmp_path):
    """meta-reconcile's run log counts merged/relaunched/reviewed/fixed/queued, and it
    lives under ~/.claude/meta-reconcile/log/ — a dir the `Write` tool cannot reach
    without stalling. Without this the 'no Write under ~/.claude/meta-<lane>/' rule
    would be one that sweep could not actually follow."""
    log = tmp_path / "rlog"
    out = MOD.heartbeat(log, counts={"merged": 0, "relaunched": 1, "reviewed": 0,
                                     "fixed": 0, "queued": 3},
                        now=_utc(2026, 9, 7, 17, 30))
    rec = out["record"]
    assert rec["relaunched"] == 1 and rec["queued"] == 3
    assert not set(MOD.COUNTS) & set(rec)          # the dispatcher's four are not invented

    # ...and the dispatcher's own call is unchanged, flags and all.
    out = MOD.heartbeat(log, counts={"prepared": 1}, now=_utc(2026, 9, 7, 17, 31))
    assert [k for k in out["record"] if k in MOD.COUNTS] == list(MOD.COUNTS)


def test_heartbeat_cli_takes_the_vocabulary_as_one_json_argument(tmp_path):
    log = tmp_path / "rlog"
    assert MOD.main(["heartbeat", "--log-dir", str(log),
                     "--counts", '{"merged": 2, "queued": 0}']) == 0
    assert MOD.main(["heartbeat", "--log-dir", str(log), "--counts", "{bad"]) == 2
    lines = _lines(sorted(log.iterdir())[0])
    assert len(lines) == 1 and lines[0]["merged"] == 2


# ── the procedures say what the tools now enforce ────────────────────────────
#
# These are grep pins, the same shape as the ones in test_meta_dispatch_eligible.py:
# the rule lives half in code and half in prose, and the prose half is what the
# unattended run actually reads. A rule split across two documents is a rule that
# loses half of itself.

_PROCEDURES = ("meta-dispatch", "meta-reconcile", "meta-coherence", "pm-landing")


def _procedure(name: str) -> str:
    return (Path(__file__).resolve().parents[3] / "internal"
            / ("%s-procedure.md" % name)).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", _PROCEDURES)
def test_no_procedure_calls_archive_session(name):
    """`mcp__ccd_session_mgmt__archive_session` is hard-coded to require explicit approval
    regardless of permission mode, so a headless call parks the session on a modal nobody
    can answer — with the run's work already done. The 2026-08-28 probe that said
    otherwise no longer reproduces. A procedure may still DISCUSS the tool (each says why
    it stopped calling it); what it may not do is instruct a run to call it."""
    doc = _procedure(name)
    # Pin the CALL SHAPES, not the token, and not a line. Every one of these files is
    # hand-wrapped prose, so a per-line negation test fails on the wrap alone (the pm-landing
    # amendment puts the tool name on a line whose "do NOT" sits two lines above it) — the
    # same trap as `feedback_hand_wrapped_prose_line_is_a_verdict_candidate`.
    for shape in ('archive_session("self")', "archive_session(session_id",
                  "archive_session('self')", "archive this session"):
        assert shape not in doc, shape
    if "archive_session" in doc:            # it may still EXPLAIN why it stopped calling it
        assert "NO SELF-ARCHIVE" in doc


@pytest.mark.parametrize("name", _PROCEDURES)
def test_no_procedure_instructs_a_write_under_its_own_state_dir(name):
    """The harness prompts on `~/.claude/**` writes regardless of grants, and an
    unattended prompt is a stall — so the instruction has to be gone from the prose, not
    merely blocked by the hook. A line may still say `Write` while forbidding it, so the
    pin is on lines that pair a state path with the tool without a negation."""
    doc = _procedure(name)
    lane = "pm-landing" if name == "pm-landing" else name
    state_path = "~/.claude/%s/" % lane
    offenders = []
    for line in doc.splitlines():
        if state_path not in line:
            continue
        if "`Write`" not in line and "Write tool" not in line and "with the `Write`" not in line:
            continue
        if any(w in line for w in ("NEVER", "never", "NEITHER", "neither", "NOT one",
                                   "not an option", "no longer", "retired",
                                   "Do not", "do NOT")):
            continue
        offenders.append(line.strip()[:160])
    assert not offenders, offenders


@pytest.mark.parametrize("name", _PROCEDURES)
def test_every_procedure_names_the_granted_state_verb(name):
    lane = "pm-landing" if name == "pm-landing" else name.replace("meta-", "")
    assert "meta-dispatch-move state --lane %s" % lane in _procedure(name)


# ── `dispatched` / `dispatched_at` (the tray-card timestamps, 2026-09-07) ────
#
# WHY THE FIELDS EXIST (operator, 2026-09-07): with the dispatcher ticking round the
# clock, a prepared card sits among dozens of quiet tick sessions with nothing on it
# saying when it appeared, so cards get buried. `dispatched: <DATE>` answers "which day"
# and cannot answer "which tile". These pin the writer; the readers are pinned in
# test_meta_dispatch_eligible.py (the waiting line) and test_meta_queue.py (group E).

import re as _re                                             # noqa: E402


def test_launch_writes_dispatched_and_dispatched_at_from_one_instant(tmp_path):
    """The two stamps are the CARD's identity in a tray that gets 24 tick tiles a day.

    They are written HERE and not by the run's own `Edit` because an unattended run has
    no clock: the harness hands it a date with no time, so a DATE it could write unaided
    and a TIME it could only guess. Taking both from ONE instant is also what stops them
    disagreeing about the date across a UTC midnight.
    """
    root = _lane(tmp_path)
    _write(root, "queued", "timed-brief")
    now = datetime.datetime(2026, 9, 7, 21, 4, 22, tzinfo=datetime.timezone.utc)

    out = MOD.launch(root, "timed-brief", now=now)

    assert out["dispatched_at"] == "2026-09-07T21:04:22Z"
    assert out["dispatched"] == "2026-09-07"
    moved = (root / "inflight" / "timed-brief.md").read_text()
    assert "dispatched: 2026-09-07" in moved
    assert "dispatched_at: 2026-09-07T21:04:22Z" in moved


def test_launch_dispatched_at_is_utc_iso8601_z_suffixed(tmp_path):
    """One spelling, because three tools parse it (mover writes, eligible renders the
    waiting line, meta-queue renders group (E)). A naive or offset-bearing stamp would
    read as unusable to `mdi.parse_dispatch_stamp` and cost every reader the age."""
    root = _lane(tmp_path)
    _write(root, "queued", "iso-brief")
    out = MOD.launch(root, "iso-brief")

    raw = out["dispatched_at"]
    assert _re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", raw), raw
    parsed = mdi.parse_dispatch_stamp(raw)
    assert parsed is not None and parsed.tzinfo is not None
    assert parsed.utcoffset() == datetime.timedelta(0)


def test_launch_stamps_from_a_non_utc_instant_by_converting_not_relabelling(tmp_path):
    """A caller's aware non-UTC `now` is CONVERTED. Relabelling it would put a local
    wall clock behind a `Z`, which is exactly how a 7-hour offset gets reported as
    elapsed time — the failure `_elapsed_seconds` refuses naive stamps to avoid."""
    root = _lane(tmp_path)
    _write(root, "queued", "offset-brief")
    tz = datetime.timezone(datetime.timedelta(hours=-7))
    now = datetime.datetime(2026, 9, 7, 18, 30, 0, tzinfo=tz)   # == 2026-09-08T01:30Z

    out = MOD.launch(root, "offset-brief", now=now)

    assert out["dispatched_at"] == "2026-09-08T01:30:00Z"
    assert out["dispatched"] == "2026-09-08"     # the UTC date, from the same instant


def test_launch_dispatch_stamps_do_not_change_the_body_hash(tmp_path):
    """Front-matter-only, so `body_sha256` is untouched — the property the whole hash
    rests on (a check that fired on intact briefs would be switched off in a week).
    Belt AND braces: the mover's own copy-verify-delete re-hashes and would refuse."""
    root = _lane(tmp_path)
    src = _write(root, "queued", "hash-brief")
    before_body = mdi.split_front_matter(src.read_text()).body
    before_digest = mdi.body_digest(before_body)

    out = MOD.launch(root, "hash-brief")

    moved = (root / "inflight" / "hash-brief.md").read_text()
    assert "dispatched_at:" in moved
    assert mdi.split_front_matter(moved).body == before_body
    assert mdi.body_digest(mdi.split_front_matter(moved).body) == before_digest
    assert out["body_sha256"] == before_digest
    assert mdi.check(moved).ok


def test_launch_reports_the_local_time_the_chip_title_carries(tmp_path):
    """The run copies `chip_title_time` into the step-4d poke rather than composing it.
    Local, because the operator's question is which tile in THEIR tray."""
    root = _lane(tmp_path)
    _write(root, "queued", "local-brief")
    now = datetime.datetime(2026, 9, 7, 21, 4, 22, tzinfo=datetime.timezone.utc)

    out = MOD.launch(root, "local-brief", now=now)

    assert out["chip_title_time"] == mdi.local_hhmm("2026-09-07T21:04:22Z")
    assert _re.fullmatch(r"\d{2}:\d{2}", out["chip_title_time"])
    assert out["chip_title_tz"]


def test_launch_dispatch_stamps_survive_the_tracked_path_too(tmp_path):
    """The two move methods must agree: a tracked brief goes through `git mv` + a
    rewrite, an untracked one through copy-verify-unlink, and a field written on only
    one of those paths is a field that vanishes for half the lane."""
    repo = _git_repo(tmp_path)
    root = _lane(repo)
    src = _write(root, "queued", "tracked-brief")
    subprocess.run(["git", "-C", str(repo), "add", str(src)], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add"], check=True)

    out = MOD.launch(root, "tracked-brief")

    assert out["method"] == "copy-verify-git-rm"
    moved = (root / "inflight" / "tracked-brief.md").read_text()
    assert "dispatched_at: %s" % out["dispatched_at"] in moved
    assert mdi.check(moved).ok


# ── launch --headless (D-PM6′) ───────────────────────────────────────────────
#
# The verb now performs the whole of procedure step 4 for a non-privileged brief: cut a
# worktree, start a `claude -p --bg` session in it, move the entry, stamp the start. The
# `claude` binary itself is the only thing stubbed (`_run_claude`) — every guardrail above
# it runs for real, because the guardrails are what these tests are for.


PRIV_BRIEF = """---
id: {id}
aspect: apps
title: "A privileged brief"
privileged: true
operator_greenlight: true
created: 2026-08-23
pm: fable-cowork
---
WHY: pinned by tests.
"""


class _FakeClaude:
    """Records the invocation and answers with a launch banner carrying a session id."""

    def __init__(self, stdout="Starting background service…\nagent_9f2c11ab started\n",
                 returncode=0, boom=None):
        self.calls = []
        self.stdout, self.returncode, self.boom = stdout, returncode, boom

    def __call__(self, argv, cwd):
        self.calls.append({"argv": list(argv), "cwd": Path(cwd)})
        if self.boom is not None:
            raise self.boom
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


def _headless_repo(tmp_path, monkeypatch, *, prereqs_ok=True, claude=None):
    """A real git repo with one commit on `main` and a lane dir inside it."""
    repo = _git_repo(tmp_path)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(repo), "add", "seed.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
    root = repo / "internal" / "dispatch"
    for sub in ("queued", "inflight", "done"):
        (root / sub).mkdir(parents=True)
    pre = (MOD.mdh.Prereqs(True, (), "") if prereqs_ok else
           MOD.mdh.Prereqs(False, (MOD.mdh.MISSING_GRANT,), "no grant here"))
    monkeypatch.setattr(MOD.mdh, "check_prereqs", lambda **kw: pre)
    fake = claude if claude is not None else _FakeClaude()
    monkeypatch.setattr(MOD, "_run_claude", fake)
    return repo, root, fake


def _fm(path: Path, key: str):
    return MOD._read_fm_str(path.read_text(), key)


def test_headless_launch_moves_the_entry_and_stamps_the_start(tmp_path, monkeypatch):
    repo, root, fake = _headless_repo(tmp_path, monkeypatch)
    _write(root, "queued", "open-work")
    r = MOD.launch(root, "open-work", headless=True)

    assert r["moved"] is True and r["headless"] is True
    assert r["route"] == "headless"
    assert r["session"] == "agent_9f2c11ab"
    entry = root / "inflight" / "open-work.md"
    assert entry.is_file()
    assert not (root / "queued" / "open-work.md").exists()
    assert _fm(entry, "launch") == "headless"
    assert _fm(entry, "session") == "agent_9f2c11ab"
    assert _fm(entry, "started") == r["started"]
    assert _fm(entry, "dispatched")


def test_headless_launch_never_writes_a_branch_into_the_entry(tmp_path, monkeypatch):
    """The chip writes `branch` at branch-cut via `meta-dispatch-move start`, which REFUSES
    to re-point a branch already recorded. A value guessed here would turn every chip that
    named its own branch into a refusal a human has to resolve."""
    repo, root, fake = _headless_repo(tmp_path, monkeypatch)
    _write(root, "queued", "open-work")
    MOD.launch(root, "open-work", headless=True)
    assert _fm(root / "inflight" / "open-work.md", "branch") is None


def test_the_chips_own_start_still_records_its_branch_afterwards(tmp_path, monkeypatch):
    """End-to-end on the one interaction that could have broken: `launch --headless` writes
    `launch: headless`, and the chip's own `start` moves it forward to `started` + branch
    without a conflict."""
    repo, root, fake = _headless_repo(tmp_path, monkeypatch)
    _write(root, "queued", "open-work")
    MOD.launch(root, "open-work", headless=True)
    MOD.start(root, "open-work", branch="claude/whatever-the-chip-chose")
    entry = root / "inflight" / "open-work.md"
    assert _fm(entry, "launch") == "started"
    assert _fm(entry, "branch") == "claude/whatever-the-chip-chose"


def test_the_prompt_is_the_entry_body_verbatim(tmp_path, monkeypatch):
    """The HARD RULE, unchanged by the transport: never prepend, append, summarize or
    'clarify' the brief. `claude --bg` has ONE prompt channel and the brief owns it, so the
    title stays where every reader already looks for it — the lane entry and the chip row.

    Asserted on the LAST element rather than on a flag's argument: the prompt moved from
    `-p <prompt>` to a bare positional when CLI v2.1.268 made `--bg` and `--print`
    conflict, and a test that indexes off a flag name re-breaks on the next such change
    while this one keeps meaning "the prompt is the brief body, whole"."""
    repo, root, fake = _headless_repo(tmp_path, monkeypatch)
    body = "WHY: a specific reason.\n\n1. **Do the thing.**\n2. Open the PR.\n"
    text = BRIEF.format(id="open-work").replace("WHY: pinned by tests.\n", body)
    _write(root, "queued", "open-work", text)
    MOD.launch(root, "open-work", headless=True)
    argv = fake.calls[0]["argv"]
    # The canonical body — the exact bytes `body_sha256` covers, so "verbatim" means the
    # same string the integrity gate would refuse a change to.
    expected = MOD.mdi.check(text).body
    assert expected.startswith("WHY: a specific reason.")
    assert argv[-1] == expected
    assert "-p" not in argv and "--print" not in argv, (
        "--bg and --print conflict on CLI v2.1.268 — the prompt is the positional")


def test_the_session_runs_in_a_fresh_worktree_never_the_operators_checkout(tmp_path,
                                                                          monkeypatch):
    """THE guardrail. A headless session runs with `--dangerously-skip-permissions`, so its
    cwd is the whole blast radius; pointed at the dev checkout it would branch-switch and
    commit underneath whatever the operator is doing there."""
    repo, root, fake = _headless_repo(tmp_path, monkeypatch)
    _write(root, "queued", "open-work")
    r = MOD.launch(root, "open-work", headless=True)
    cwd = fake.calls[0]["cwd"].resolve()
    assert cwd != repo.resolve()
    assert cwd == Path(r["worktree"]).resolve()
    assert cwd.is_dir() and (cwd / ".git").exists()
    listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                            capture_output=True, text=True).stdout
    assert str(cwd) in listed


def test_the_worktree_is_cut_on_a_branch_not_detached(tmp_path, monkeypatch):
    """A detached HEAD makes the chip's own `start` refuse ('cannot read a branch from HEAD
    here'), which would leave every headless chip unable to record that it started."""
    repo, root, fake = _headless_repo(tmp_path, monkeypatch)
    _write(root, "queued", "open-work")
    r = MOD.launch(root, "open-work", headless=True)
    head = subprocess.run(["git", "-C", r["worktree"], "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    assert head == r["branch"] != "HEAD"


# ── never both: a card OR a session ──────────────────────────────────────────


def test_headless_is_refused_on_a_privileged_brief(tmp_path, monkeypatch):
    """Refused outright, never silently downgraded to a card: D-PM1 and D-PM2 make the
    operator the gate at both ends of a privileged brief, and a quiet fallback would hide
    a caller that had misread the lane."""
    repo, root, fake = _headless_repo(tmp_path, monkeypatch)
    _write(root, "queued", "priv-work", PRIV_BRIEF.format(id="priv-work"))
    with pytest.raises(MOD.Refused) as e:
        MOD.launch(root, "priv-work", headless=True)
    assert "privileged" in str(e.value)
    assert fake.calls == [], "no session may be started for a privileged brief"
    assert (root / "queued" / "priv-work.md").is_file(), "and nothing may move"


@pytest.mark.parametrize("field", [None, "privileged: True", "privileged: yes"])
def test_the_privilege_read_fails_closed(tmp_path, monkeypatch, field):
    """An absent `privileged` field, or any spelling other than an explicit false/no, is
    privileged: this read is the last gate before an unattended session."""
    repo, root, fake = _headless_repo(tmp_path, monkeypatch)
    text = BRIEF.format(id="open-work")
    text = text.replace("privileged: false\n", "" if field is None else field + "\n")
    _write(root, "queued", "open-work", text)
    with pytest.raises(MOD.Refused) as e:
        MOD.launch(root, "open-work", headless=True)
    assert "privileged" in str(e.value)
    assert fake.calls == []
    assert (root / "queued" / "open-work.md").is_file()


def test_an_existing_worktree_is_refused_never_force_removed(tmp_path, monkeypatch):
    """A previous headless launch may still be working in the path; its uncommitted
    work is not the mover's to destroy. A human clears it."""
    repo, root, fake = _headless_repo(tmp_path, monkeypatch)
    _write(root, "queued", "open-work")
    wt = repo / MOD.HEADLESS_WORKTREE_SUBDIR / (MOD.HEADLESS_WORKTREE_PREFIX + "open-work")
    wt.mkdir(parents=True)
    (wt / "in-progress.txt").write_text("uncommitted work\n")
    with pytest.raises(MOD.Refused) as e:
        MOD.launch(root, "open-work", headless=True)
    assert str(wt) in str(e.value)
    assert (wt / "in-progress.txt").read_text() == "uncommitted work\n"
    assert fake.calls == []
    assert (root / "queued" / "open-work.md").is_file()


def test_a_launch_timeout_leaves_the_worktree_standing(tmp_path, monkeypatch):
    """A timeout means a session may already be running in the worktree — refuse without
    dropping it (the other error types still clean up)."""
    boom = subprocess.TimeoutExpired(cmd="claude", timeout=120)
    repo, root, fake = _headless_repo(tmp_path, monkeypatch, claude=_FakeClaude(boom=boom))
    _write(root, "queued", "open-work")
    with pytest.raises(MOD.Refused) as e:
        MOD.launch(root, "open-work", headless=True)
    wt = repo / MOD.HEADLESS_WORKTREE_SUBDIR / (MOD.HEADLESS_WORKTREE_PREFIX + "open-work")
    assert "timed out" in str(e.value) and str(wt) in str(e.value)
    assert wt.is_dir(), "the worktree is left in place after a timeout"
    assert (root / "queued" / "open-work.md").is_file()


def test_a_privileged_brief_still_launches_the_ordinary_way(tmp_path, monkeypatch):
    repo, root, fake = _headless_repo(tmp_path, monkeypatch)
    _write(root, "queued", "priv-work", PRIV_BRIEF.format(id="priv-work"))
    r = MOD.launch(root, "priv-work")
    assert r["moved"] is True and r["headless"] is False
    assert (root / "inflight" / "priv-work.md").is_file()
    assert _fm(root / "inflight" / "priv-work.md", "launch") is None, (
        "the dispatcher writes `launch: prepared` after spawn_task, not the mover")
    assert fake.calls == []


def test_a_second_launch_is_refused_when_the_entry_already_has_a_session(tmp_path,
                                                                        monkeypatch):
    """`dst.exists()` catches the second launch of a brief already in `inflight/`; this
    catches the other shape — the entry put back into `queued/` by a pull or a branch
    switch with its dispatch fields intact."""
    repo, root, fake = _headless_repo(tmp_path, monkeypatch)
    text = BRIEF.format(id="open-work").replace("privileged: false",
                                                "privileged: false\nsession: agent_earlier")
    _write(root, "queued", "open-work", text)
    with pytest.raises(MOD.Refused) as e:
        MOD.launch(root, "open-work", headless=True)
    assert "agent_earlier" in str(e.value)
    assert fake.calls == []


def test_the_second_launch_refusal_applies_to_the_ordinary_path_too(tmp_path, monkeypatch):
    repo, root, fake = _headless_repo(tmp_path, monkeypatch)
    text = BRIEF.format(id="open-work").replace("privileged: false",
                                                "privileged: false\nsession: task_earlier")
    _write(root, "queued", "open-work", text)
    with pytest.raises(MOD.Refused):
        MOD.launch(root, "open-work")
    assert (root / "queued" / "open-work.md").is_file()


def test_a_null_session_is_not_a_prior_dispatch(tmp_path, monkeypatch):
    """`session: null` is what the lane writes for 'no session', so it must not read as
    one — otherwise a brief could never be launched after any tool had touched the field."""
    repo, root, fake = _headless_repo(tmp_path, monkeypatch)
    text = BRIEF.format(id="open-work").replace("privileged: false",
                                                "privileged: false\nsession: null")
    _write(root, "queued", "open-work", text)
    assert MOD.launch(root, "open-work", headless=True)["moved"] is True


# ── the prerequisite fallback ────────────────────────────────────────────────


def test_a_missing_prerequisite_falls_back_and_moves_nothing(tmp_path, monkeypatch):
    """Exit 0, `moved: false`. Nothing is moved BECAUSE the caller must then run the
    unchanged step 4a/4b — spawn_task first, then a plain launch — and reversing that order
    is how a chip reaches the tray with its brief still in `queued/`."""
    repo, root, fake = _headless_repo(tmp_path, monkeypatch, prereqs_ok=False)
    _write(root, "queued", "open-work")
    r = MOD.launch(root, "open-work", headless=True)
    assert r["ok"] is True and r["moved"] is False
    assert r["route"] == "prepared"
    assert r["note"] == "headless: unavailable (grant)"
    assert r["missing_prerequisites"] == ["grant"]
    assert fake.calls == []
    assert (root / "queued" / "open-work.md").is_file()
    assert not (root / "inflight" / "open-work.md").exists()


def test_the_fallback_is_not_a_refusal(tmp_path, monkeypatch):
    """Exit 0, not 2. Exit 2 triggers the procedure's 'STOP that entry' protocol, and a
    missing operator grant is a routing fact, not a damaged brief."""
    repo, root, fake = _headless_repo(tmp_path, monkeypatch, prereqs_ok=False)
    _write(root, "queued", "open-work")
    rc = MOD.main(["launch", "open-work", "--headless", "--dir", str(root), "--json"])
    assert rc == 0


def test_after_the_fallback_a_plain_launch_still_works(tmp_path, monkeypatch):
    repo, root, fake = _headless_repo(tmp_path, monkeypatch, prereqs_ok=False)
    _write(root, "queued", "open-work")
    MOD.launch(root, "open-work", headless=True)
    assert MOD.launch(root, "open-work")["moved"] is True


# ── failures around the session ──────────────────────────────────────────────


def test_a_failed_session_start_removes_the_worktree_and_leaves_the_lane_alone(
        tmp_path, monkeypatch):
    repo, root, _ = _headless_repo(tmp_path, monkeypatch,
                                   claude=_FakeClaude(stdout="boom", returncode=1))
    _write(root, "queued", "open-work")
    with pytest.raises(MOD.Refused) as e:
        MOD.launch(root, "open-work", headless=True)
    assert "lane untouched" in str(e.value)
    assert (root / "queued" / "open-work.md").is_file()
    assert not (root / "inflight" / "open-work.md").exists()
    listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                            capture_output=True, text=True).stdout
    assert "meta-chip-open-work" not in listed


def test_a_missing_claude_binary_is_a_refusal_not_a_traceback(tmp_path, monkeypatch):
    repo, root, _ = _headless_repo(tmp_path, monkeypatch,
                                   claude=_FakeClaude(boom=FileNotFoundError("claude")))
    _write(root, "queued", "open-work")
    with pytest.raises(MOD.Refused):
        MOD.launch(root, "open-work", headless=True)
    assert (root / "queued" / "open-work.md").is_file()


def test_an_unrecognised_session_id_costs_the_id_and_nothing_else(tmp_path, monkeypatch):
    """Inventing an id the CLI never issued is the #3849 orphan-id shape — a lane keyed on
    a value nothing else holds. `launch: headless` is what every downstream reader gates
    on, so a missing id is survivable and a fabricated one is not."""
    repo, root, _ = _headless_repo(tmp_path, monkeypatch,
                                   claude=_FakeClaude(stdout="ok, running\n"))
    _write(root, "queued", "open-work")
    r = MOD.launch(root, "open-work", headless=True)
    assert r["session"] is None
    entry = root / "inflight" / "open-work.md"
    assert "session: null" in entry.read_text()
    assert _fm(entry, "session") is None, "`null` reads back as absent, as it should"
    assert _fm(entry, "launch") == "headless"


def test_a_move_failure_after_the_session_started_names_the_session(tmp_path, monkeypatch):
    """The 2026-08-25 mistake was recording an unmoved brief without the handle of the
    thing it had already started."""
    repo, root, fake = _headless_repo(tmp_path, monkeypatch)
    _write(root, "queued", "open-work")

    def boom(*a, **kw):
        raise MOD.Refused("short write staging the destination")
    monkeypatch.setattr(MOD, "_copy_verify_delete", boom)
    with pytest.raises(MOD.Refused) as e:
        MOD.launch(root, "open-work", headless=True)
    assert "agent_9f2c11ab" in str(e.value)
    assert "prepared_but_unmoved" in str(e.value)


def test_the_body_hash_is_stamped_on_the_headless_path_too(tmp_path, monkeypatch):
    repo, root, fake = _headless_repo(tmp_path, monkeypatch)
    _write(root, "queued", "open-work")
    r = MOD.launch(root, "open-work", headless=True)
    assert r["stamped"] is True
    assert MOD.mdi.check((root / "inflight" / "open-work.md").read_text()).ok


def test_the_cli_reports_a_headless_launch_in_json(tmp_path, monkeypatch, capsys):
    repo, root, fake = _headless_repo(tmp_path, monkeypatch)
    _write(root, "queued", "open-work")
    assert MOD.main(["launch", "open-work", "--headless", "--dir", str(root),
                     "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["headless"] is True and out["session"] == "agent_9f2c11ab"
