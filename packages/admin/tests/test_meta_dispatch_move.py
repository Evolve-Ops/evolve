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
