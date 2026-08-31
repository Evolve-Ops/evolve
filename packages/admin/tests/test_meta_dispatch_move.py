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
    # and the old path is not left dangling as a DELETION with the new one untracked
    # (a rename entry legitimately names both paths — a phantom deletion does not)
    status = _porcelain(repo)
    assert not any(ln[:2] in (" D", "D ") and "queued/elsewhere-brief.md" in ln
                   for ln in status.splitlines()), status
    assert not any(ln.startswith("??") and "inflight/elsewhere-brief.md" in ln
                   for ln in status.splitlines()), status


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
