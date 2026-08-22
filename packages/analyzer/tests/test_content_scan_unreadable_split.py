"""Regression: content_scan must not conflate "file deleted" with "evolve
can't read it".

`_read_text` returns an ``err`` that distinguishes the cause: ``"not_found"``
(FileNotFoundError = the file is genuinely gone) vs ``"sudo_rc=N"`` / a failed
PermissionError fallback / ``"timeout"`` / ``"os_error: …"`` (evolve CAN'T read
it, but it almost certainly still exists). The old code lumped ALL of these into
ONE ``content_scan_file_disappeared`` finding at ``alert`` severity titled
"… missing or unreadable" — so a transient ACL-mask lockout (evo-vps 2026-06-29:
evo's 7 identity docs fired with ``read_error=sudo_rc=1`` while the files existed
throughout) screamed at the operator exactly like a real deletion, and the
alert severity defeated the digest's flap-collapse.

The split:
  * not_found        → content_scan_file_disappeared (alert)  — really gone, page.
  * everything else  → content_scan_file_unreadable   (warn)  — access flap, digest.

Owner: META:reports (signal-producer quality).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from content_scan import catalog as _catalog
from content_scan import scanner as _scanner


def _scan_one(shared_dir: Path, absolute_path: Path):
    """Run a single-target scan and return its findings list."""
    _catalog.write_default_if_missing(shared_dir)
    cat = _catalog.load(shared_dir)
    allowlist = _scanner.effective_allowlist(cat)
    _result, findings = _scanner._scan_target(
        shared_dir=shared_dir,
        bot_id="botzo",
        file_relpath="USER.md",
        absolute_path=absolute_path,
        catalog=cat,
        allowlist=allowlist,
        catalog_sig=_scanner.catalog_signature(cat, allowlist),
        bot_suppressions=[],
    )
    return findings


def test_genuinely_absent_file_stays_alert_disappeared(tmp_path: Path) -> None:
    """A file that does not exist → content_scan_file_disappeared, alert, and
    the wording says "missing" (the RUNTIME_NOTES.md class — a real problem
    worth paging)."""
    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    absent = tmp_path / "homes" / "botzo" / ".openclaw" / "workspace" / "USER.md"
    # Parent dir absent too → path.read_text() raises FileNotFoundError →
    # _read_text returns err="not_found".

    findings = _scan_one(shared_dir, absent)

    assert len(findings) == 1
    f = findings[0]
    assert f["type"] == "content_scan_file_disappeared"
    assert f["severity"] == "alert"
    assert f["details"]["read_error"] == "not_found"
    # Wording is honest about deletion, never the access-failure framing.
    assert "missing" in f["title"].lower()
    assert "can't read" not in f["title"].lower()
    # Plain-language guard: no absolute path in operator-facing copy.
    assert str(absent) not in (f["title"] + " " + f["body"])
    assert f["details"]["absolute_path"] == str(absent)


@pytest.mark.parametrize(
    "err",
    ["sudo_rc=1", "timeout", "os_error: [Errno 5] I/O error", "sudo_error: boom"],
)
def test_unreadable_file_is_warn_unreadable_not_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: str
) -> None:
    """Every non-not_found read error → the NEW content_scan_file_unreadable
    finding at warn severity, with wording that does NOT claim the file is
    missing and the read_error preserved in details."""
    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    target = tmp_path / "USER.md"
    # The file is present on disk — this is an access failure, not a deletion.
    target.write_text("present but unreadable by evolve\n")

    # Simulate evolve being unable to read it (ACL-mask lockout, sudo denied,
    # timeout, transient I/O) — the file exists throughout.
    monkeypatch.setattr(_scanner, "_read_text", lambda *a, **k: (None, err))

    findings = _scan_one(shared_dir, target)

    assert len(findings) == 1
    f = findings[0]
    assert f["type"] == "content_scan_file_unreadable"
    assert f["severity"] == "warn"
    assert f["details"]["read_error"] == err
    # Crucially: the operator-facing wording must NOT say the file is missing.
    blob = (f["title"] + " " + f["body"]).lower()
    assert "missing" not in blob
    assert "can't read" in f["title"].lower() or "couldn't read" in f["body"].lower()
    # Plain-language guard (docs/voice-guide.md Tier-A): the title/body must
    # not leak the bare acronym "ACL", the raw read-error code, or the absolute
    # path — those belong in details only.
    assert "acl" not in blob
    assert err.lower() not in blob
    assert str(target) not in (f["title"] + " " + f["body"])
    # …but the raw cause IS preserved for the Tier-B detail view.
    assert f["details"]["absolute_path"] == str(target)


def test_unreadable_signal_resolves_when_file_becomes_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a file flips unreadable → readable, the unreadable signal must
    resolve cleanly via sweep_resolve (no orphaned firing signal). Both the
    disappeared and unreadable types are in _OWNED_TYPES, so a file moving
    between absent/unreadable/clean drops the stale type's signature from
    kept_signatures and sweep_resolve archives it on the same cycle."""
    pytest.importorskip("signals")
    from signals import store as _store

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    _catalog.write_default_if_missing(shared_dir)
    # Seed pod-wide files clean so the pod leg contributes nothing.
    cat = _catalog.load(shared_dir)
    for fname in cat.scope.scanned_pod_files:
        (shared_dir / fname).write_text("clean content\n")

    workspace = tmp_path / "homes" / "botzo" / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    for fname in cat.scope.scanned_files_per_bot:
        (workspace / fname).write_text("clean content\n")

    monkeypatch.setattr(
        _scanner, "_bot_home", lambda bot_id, config=None: workspace.parent.parent
    )

    # Cycle 1: USER.md is unreadable (everything else clean) → one firing
    # content_scan_file_unreadable signal.
    real_read = _scanner._read_text

    def _fail_user(path, **kwargs):
        if Path(path).name == "USER.md":
            return None, "sudo_rc=1"
        return real_read(path, **kwargs)

    monkeypatch.setattr(_scanner, "_read_text", _fail_user)
    _scanner.run(shared_dir, ["botzo"], {}, emit_signals=True)

    firing = [
        s for s in _store.iter_active(shared_dir, producer="content_scan", state="firing")
        if s.type == "content_scan_file_unreadable"
    ]
    assert len(firing) == 1, "unreadable file should raise exactly one firing signal"

    # Cycle 2: USER.md is readable again → the unreadable signal must resolve.
    monkeypatch.setattr(_scanner, "_read_text", real_read)
    _scanner.run(shared_dir, ["botzo"], {}, emit_signals=True)

    still_firing = [
        s for s in _store.iter_active(shared_dir, producer="content_scan", state="firing")
        if s.type == "content_scan_file_unreadable"
    ]
    assert not still_firing, (
        "unreadable signal did not resolve after the file became readable — "
        "sweep_resolve must archive it once its signature drops from kept"
    )


def test_sudo_enoent_classified_as_deleted_not_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely-deleted file behind a non-traversable parent dir must STILL
    page as content_scan_file_disappeared, not be downgraded to the warn-level
    unreadable type.

    On an ACL-degraded pod, the direct path.read_text() raises PermissionError
    (evolve can't traverse the parent to see the file is gone), so _read_text
    falls through to `sudo /bin/cat`. Root bypasses the DAC traverse check, so a
    real deletion surfaces there as an ENOENT on stderr ("No such file or
    directory") with rc!=0. _read_text must read that stderr and return
    "not_found" — otherwise a real deletion on exactly the pods this split is
    meant to help would be silently demoted from a page to a digest line."""
    target = tmp_path / "USER.md"  # does not exist

    class _R:
        returncode = 1
        stdout = ""
        stderr = "/bin/cat: /Users/botzo/.openclaw/workspace/USER.md: No such file or directory\n"

    # Force the PermissionError → sudo fallback path, and have sudo report ENOENT.
    orig_read_text = Path.read_text

    def _perm_denied(self, *a, **k):
        if self == target:
            raise PermissionError("EACCES on non-traversable parent")
        return orig_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _perm_denied)
    monkeypatch.setattr(_scanner.subprocess, "run", lambda *a, **k: _R())

    text, err = _scanner._read_text(target)
    assert text is None
    assert err == "not_found", (
        "sudo ENOENT must be classified as a real deletion (not_found), not a "
        f"transient access failure; got {err!r}"
    )

    # And the finding it drives is the alert-severity disappeared type.
    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    findings = _scan_one(shared_dir, target)
    assert len(findings) == 1
    assert findings[0]["type"] == "content_scan_file_disappeared"
    assert findings[0]["severity"] == "alert"


def test_disappeared_signal_resolves_when_file_becomes_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live evo-vps migration path: a file firing the alert-severity
    content_scan_file_disappeared that flips to merely-unreadable must resolve
    the stale alert and fire the warn-level unreadable instead — no orphaned
    alert keeps paging. This is the absent→unreadable transition the split's
    flap-collapse depends on (complement to the unreadable→readable test)."""
    pytest.importorskip("signals")
    from signals import store as _store

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    _catalog.write_default_if_missing(shared_dir)
    cat = _catalog.load(shared_dir)
    for fname in cat.scope.scanned_pod_files:
        (shared_dir / fname).write_text("clean content\n")

    workspace = tmp_path / "homes" / "botzo" / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    for fname in cat.scope.scanned_files_per_bot:
        (workspace / fname).write_text("clean content\n")
    monkeypatch.setattr(
        _scanner, "_bot_home", lambda bot_id, config=None: workspace.parent.parent
    )
    real_read = _scanner._read_text

    # Cycle 1: USER.md is genuinely gone → firing alert content_scan_file_disappeared.
    def _gone(path, **kwargs):
        if Path(path).name == "USER.md":
            return None, "not_found"
        return real_read(path, **kwargs)

    monkeypatch.setattr(_scanner, "_read_text", _gone)
    _scanner.run(shared_dir, ["botzo"], {}, emit_signals=True)
    disappeared = [
        s for s in _store.iter_active(shared_dir, producer="content_scan", state="firing")
        if s.type == "content_scan_file_disappeared"
    ]
    assert len(disappeared) == 1 and disappeared[0].severity == "alert"

    # Cycle 2: same file is now merely unreadable → the alert must resolve and
    # the warn-level unreadable fire in its place.
    def _unreadable(path, **kwargs):
        if Path(path).name == "USER.md":
            return None, "sudo_rc=1"
        return real_read(path, **kwargs)

    monkeypatch.setattr(_scanner, "_read_text", _unreadable)
    _scanner.run(shared_dir, ["botzo"], {}, emit_signals=True)

    active = list(_store.iter_active(shared_dir, producer="content_scan", state="firing"))
    assert not [s for s in active if s.type == "content_scan_file_disappeared"], (
        "stale disappeared alert must resolve once the file is only unreadable"
    )
    unreadable = [s for s in active if s.type == "content_scan_file_unreadable"]
    assert len(unreadable) == 1 and unreadable[0].severity == "warn"
