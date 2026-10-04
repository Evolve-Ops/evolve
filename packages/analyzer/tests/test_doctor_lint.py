"""Tests for the doctor-pass lint pipeline (runner artifact → Signals).

Context: under OC 2026.9.2 the old nightly `openclaw doctor --fix` is a
pod-wide no-op — every bot fails "Doctor could not enter maintenance" in ~2s,
verified on all 9 bots of the reference pod from one 03:17 run. The job now
runs `doctor --lint --json` (read-only, no maintenance gate) and its findings
reach the operator as Signals instead of dying in a log.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import doctor_lint_signal as dls  # noqa: E402
import doctor_pass_runner as dpr  # noqa: E402


# Shape confirmed against OC 2026.9.2 on the reference pod.
SAMPLE = {
    "ok": False,
    "checksRun": 30,
    "checksSkipped": 30,
    "findings": [
        {
            "checkId": "core/doctor/security",
            "severity": "warning",
            "message": "WARNING: openclaw.json contains plaintext secret-bearing config fields.",
            "fixHint": "Migrate them to SecretRefs with openclaw secrets configure.",
        },
        {
            "checkId": "core/doctor/node-hosting-preconditions",
            "severity": "warning",
            "message": "Gateway is only bound to loopback.",
            "path": "gateway.bind",
            "requirement": "node-onboarding-url",
        },
    ],
}


# ── runner: artifact + summary ───────────────────────────────────────────────

def test_write_artifact_roundtrips_atomically(tmp_path):
    payload = {"bot_id": "team-bot-a", "findings": SAMPLE["findings"]}

    path = dpr.write_artifact(payload, home=tmp_path)

    assert path == tmp_path / dpr.ARTIFACT_RELPATH
    assert json.loads(path.read_text())["findings"] == SAMPLE["findings"]
    # No temp files left behind by the mkstemp+replace dance.
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_write_artifact_reports_failure_without_raising(tmp_path, capsys):
    """An unwritable workspace must not turn into a launchd service failure."""
    blocker = tmp_path / ".openclaw"
    blocker.write_text("not a directory")

    assert dpr.write_artifact({"x": 1}, home=tmp_path) is None
    assert "could not write" in capsys.readouterr().out


def test_summarize_counts_by_severity():
    assert dpr.summarize(SAMPLE["findings"]) == {"warning": 2}
    assert dpr.summarize([]) == {}
    assert dpr.summarize([{"severity": "error"}, {}]) == {"error": 1, "unknown": 1}


def test_runner_invokes_lint_not_fix():
    """The whole point of the change — pin it so a revert is loud.

    `--fix` cannot enter maintenance from a timer under OC 2026.9.2 and
    repairs nothing; `--lint --json` is read-only and actually reports.
    """
    src = Path(dpr.__file__).read_text()
    assert '"doctor", "--lint", "--json"' in src
    assert '"doctor", "--fix"' not in src


# ── converter: findings → Signals ────────────────────────────────────────────

class FakeStore:
    def __init__(self):
        self.observed: list[dict] = []
        self.swept: list[dict] = []

    def observe(self, shared_dir, **kw):
        self.observed.append(kw)

    def sweep_resolve(self, shared_dir, **kw):
        self.swept.append(kw)


def _patch(monkeypatch, store):
    import types
    fake_signals = types.ModuleType("signals")
    fake_store_mod = types.ModuleType("signals.store")
    for name in ("observe", "sweep_resolve"):
        setattr(fake_store_mod, name, getattr(store, name))
    fake_signals.store = fake_store_mod
    fake_schema = types.ModuleType("schema.signal")
    fake_schema.make_signature = lambda p, t, k: f"{p}/{t}/{k}"
    monkeypatch.setitem(sys.modules, "signals", fake_signals)
    monkeypatch.setitem(sys.modules, "signals.store", fake_store_mod)
    monkeypatch.setitem(sys.modules, "schema.signal", fake_schema)


def _artifact(home: Path, *, ran_at: str, findings: list[dict]) -> None:
    p = home / dls.ARTIFACT_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ran_at": ran_at, "findings": findings}))


def _now() -> datetime:
    return datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def test_findings_become_one_signal_each(tmp_path, monkeypatch):
    store = FakeStore()
    _patch(monkeypatch, store)
    home = tmp_path / "bot-a-home"
    _artifact(home, ran_at=_now().isoformat(), findings=SAMPLE["findings"])

    dls.emit_doctor_lint_signals(tmp_path, {"bot-a": home}, now=_now())

    assert len(store.observed) == 2
    ids = {o["details"]["check_id"] for o in store.observed}
    assert ids == {"core/doctor/security", "core/doctor/node-hosting-preconditions"}
    sec = next(o for o in store.observed
               if o["details"]["check_id"] == "core/doctor/security")
    assert sec["scope"] == "bot" and sec["bot_id"] == "bot-a"
    assert "SecretRefs" in sec["details"]["fix_steps"]
    # Keys OC omitted on this finding are dropped, not rendered as None.
    assert "config_path" not in sec["details"]


def test_signature_keys_on_check_id_not_message(tmp_path, monkeypatch):
    """A changed message must update the SAME Signal, not mint a new one.

    OC embeds mutable specifics (paths, counts) in `message`. Keying the
    signature on it would leave an un-resolvable Signal behind on every edit.
    """
    store = FakeStore()
    _patch(monkeypatch, store)
    home = tmp_path / "h"

    _artifact(home, ran_at=_now().isoformat(), findings=[SAMPLE["findings"][0]])
    dls.emit_doctor_lint_signals(tmp_path, {"bot-a": home}, now=_now())
    first = store.observed[0]["signature"]

    changed = dict(SAMPLE["findings"][0], message="WARNING: now 4 fields.")
    _artifact(home, ran_at=_now().isoformat(), findings=[changed])
    dls.emit_doctor_lint_signals(tmp_path, {"bot-a": home}, now=_now())

    assert store.observed[1]["signature"] == first


def test_two_findings_sharing_a_check_id_stay_distinct(tmp_path, monkeypatch):
    """Regression: one real lint returned two findings for the SAME checkId.

    Observed live on the reference pod — ``core/doctor/node-hosting-
    preconditions`` fired for both ``gateway.bind`` and
    ``plugins.entries.device-pair.enabled``. Keying the signature on checkId
    alone collapsed them into one Signal and dropped the second silently.
    """
    store = FakeStore()
    _patch(monkeypatch, store)
    home = tmp_path / "h"
    same_check = [
        {"checkId": "core/doctor/node-hosting-preconditions",
         "severity": "warning", "message": "Gateway is only bound to loopback.",
         "path": "gateway.bind"},
        {"checkId": "core/doctor/node-hosting-preconditions",
         "severity": "warning", "message": "The device-pair plugin is not enabled.",
         "path": "plugins.entries.device-pair.enabled"},
    ]
    _artifact(home, ran_at=_now().isoformat(), findings=same_check)

    dls.emit_doctor_lint_signals(tmp_path, {"bot-a": home}, now=_now())

    assert len(store.observed) == 2
    assert len({o["signature"] for o in store.observed}) == 2
    assert len(store.swept[0]["kept_signatures"]) == 2


def test_subject_prefers_path_then_target_then_empty():
    assert dls._subject({"path": "a.b", "target": "t"}) == "a.b"
    assert dls._subject({"target": "t"}) == "t"
    assert dls._subject({}) == ""


def test_cleared_findings_are_swept(tmp_path, monkeypatch):
    store = FakeStore()
    _patch(monkeypatch, store)
    home = tmp_path / "h"
    _artifact(home, ran_at=_now().isoformat(), findings=[])

    dls.emit_doctor_lint_signals(tmp_path, {"bot-a": home}, now=_now())

    assert store.observed == []
    assert store.swept[0]["kept_signatures"] == set()
    assert store.swept[0]["producer"] == dls.PRODUCER


def test_stale_artifact_raises_its_own_signal(tmp_path, monkeypatch):
    """A lint that stopped running is the failure this work exists to catch."""
    store = FakeStore()
    _patch(monkeypatch, store)
    home = tmp_path / "h"
    old = (_now() - timedelta(hours=dls.STALE_AFTER_HOURS + 1)).isoformat()
    _artifact(home, ran_at=old, findings=SAMPLE["findings"])

    dls.emit_doctor_lint_signals(tmp_path, {"bot-a": home}, now=_now())

    assert [o["type"] for o in store.observed] == ["doctor_lint_stale"]
    # Stale findings are not asserted as current conditions.
    assert len(store.observed) == 1
    assert "doctor-pass.bot-a" in store.observed[0]["body"]


def test_a_single_missed_night_is_not_stale(tmp_path, monkeypatch):
    store = FakeStore()
    _patch(monkeypatch, store)
    home = tmp_path / "h"
    recent = (_now() - timedelta(hours=dls.STALE_AFTER_HOURS - 1)).isoformat()
    _artifact(home, ran_at=recent, findings=SAMPLE["findings"])

    dls.emit_doctor_lint_signals(tmp_path, {"bot-a": home}, now=_now())

    assert {o["type"] for o in store.observed} == {"doctor_lint_warning"}


def test_missing_artifact_is_not_a_finding(tmp_path, monkeypatch):
    """Pre-first-run on a fresh bot must not fire before its first 03:17."""
    store = FakeStore()
    _patch(monkeypatch, store)

    dls.emit_doctor_lint_signals(
        tmp_path, {"bot-a": tmp_path / "never-ran"}, now=_now(),
    )

    assert store.observed == []
    assert len(store.swept) == 1


def test_unreadable_artifact_is_reported(tmp_path, monkeypatch):
    store = FakeStore()
    _patch(monkeypatch, store)
    home = tmp_path / "h"
    p = home / dls.ARTIFACT_RELPATH
    p.parent.mkdir(parents=True)
    p.write_text("{truncated")

    dls.emit_doctor_lint_signals(tmp_path, {"bot-a": home}, now=_now())

    assert [o["type"] for o in store.observed] == ["doctor_lint_unreadable"]


def test_missing_timestamp_counts_as_stale(tmp_path, monkeypatch):
    """"Can't show it ran" must never read as "fine"."""
    store = FakeStore()
    _patch(monkeypatch, store)
    home = tmp_path / "h"
    p = home / dls.ARTIFACT_RELPATH
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"findings": SAMPLE["findings"]}))

    dls.emit_doctor_lint_signals(tmp_path, {"bot-a": home}, now=_now())

    assert [o["type"] for o in store.observed] == ["doctor_lint_stale"]


def test_one_bad_bot_does_not_stop_the_others(tmp_path, monkeypatch):
    store = FakeStore()
    _patch(monkeypatch, store)
    bad, good = tmp_path / "bad", tmp_path / "good"
    (bad / dls.ARTIFACT_RELPATH).parent.mkdir(parents=True)
    (bad / dls.ARTIFACT_RELPATH).write_text("{nope")
    _artifact(good, ran_at=_now().isoformat(), findings=[SAMPLE["findings"][0]])

    dls.emit_doctor_lint_signals(
        tmp_path, {"bot-a": bad, "bot-b": good}, now=_now(),
    )

    assert {o["bot_id"] for o in store.observed} == {"bot-a", "bot-b"}


def test_missing_signals_package_is_a_quiet_noop(tmp_path, monkeypatch):
    """pod_report must never break on signal-side unavailability."""
    monkeypatch.setitem(sys.modules, "signals", None)
    dls.emit_doctor_lint_signals(tmp_path, {"bot-a": tmp_path}, now=_now())
