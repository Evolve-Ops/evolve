"""tests/test_backup_signal.py — backup_signal monitor tests."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import backup_signal as bs  # noqa: E402
from signals import store as signals_store  # noqa: E402

_NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _hermetic_git_probes(monkeypatch):
    """Neutralize the real-git default probes (workspace drift + the
    [backup]-commit anchor) so tests never touch a real bot workspace.
    collect_for_bot late-binds these module attributes precisely so this
    single fixture keeps every test hermetic; tests that exercise the
    probes inject their own via ``drift_collector=`` /
    ``backup_commit_prober=``."""
    monkeypatch.setattr(bs, "_default_drift_collector", lambda _bot, _cfg: None)
    monkeypatch.setattr(bs, "_default_backup_commit_prober", lambda _bot, _cfg: None)


def _state(
    *,
    failures: int = 3,
    last_attempt: str | None = None,
    last_success: str | None = "2026-05-21T02:00:00Z",
    last_error: str = "push failed: read-only deploy key",
    status: str = "failed",
) -> dict:
    # Default last_attempt is dynamically fresh so the daemon-silence
    # check stays quiet in tests about other conditions (a fixed date
    # would silently cross the 26h threshold as the clock advances —
    # the clock-coupled-test hazard).
    if last_attempt is None:
        last_attempt = _iso(datetime.now(timezone.utc))
    return {
        "bot_id": "evolve",
        "last_attempt_at": last_attempt,
        "last_attempt_status": status,
        "last_success_at": last_success,
        "last_error": last_error,
        "consecutive_failures": failures,
    }


def _cfg(*, bot: str = "evolve", url: str = "git@github.com:evolve-ops/evolve-workspace.git") -> dict:
    return {"bots": {bot: {"backupRepoUrl": url}}}


# Defaults for the visibility hooks added in Phase 1. Tests that care
# about backup_failing only pass these so the visibility check stays
# silent. Tests that exercise visibility pass other values.
def _pat_ok(_config):
    return "test-pat"


def _private(_url, pat=None):
    return "private"


def _public(_url, pat=None):
    return "public"


def _unknown(_url, pat=None):
    return "unknown"


def _no_pat(_config):
    return None


# ── build_signal_for_failing_backup ──────────────────────────────────────────


def test_no_signal_below_threshold():
    """One or two flakes shouldn't wake the operator."""
    assert bs.build_signal_for_failing_backup("evolve", _state(failures=1), "url") is None
    assert bs.build_signal_for_failing_backup("evolve", _state(failures=2), "url") is None


def test_signal_fires_at_threshold():
    """At consecutive_failures == 3 the Signal should fire."""
    spec = bs.build_signal_for_failing_backup("evolve", _state(failures=3), "url")
    assert spec is not None
    assert spec["type"] == "backup_failing"
    assert spec["scope"] == "bot"
    assert spec["bot_id"] == "evolve"
    assert spec["severity"] == "warn"


def test_signal_escalates_to_alert_at_seven():
    """A week of bouncing is meaningful state loss — escalate severity."""
    spec = bs.build_signal_for_failing_backup("evolve", _state(failures=7), "url")
    assert spec["severity"] == "alert"
    spec_8 = bs.build_signal_for_failing_backup("evolve", _state(failures=8), "url")
    assert spec_8["severity"] == "alert"


def test_signal_body_includes_error():
    spec = bs.build_signal_for_failing_backup(
        "evolve",
        _state(last_error="push failed: read-only deploy key", failures=3),
        "git@github.com:evolve-ops/evolve-workspace.git",
    )
    assert "read-only deploy key" in spec["body"]
    assert "evolve-ops/evolve-workspace" in spec["body"]


def test_signal_body_truncates_long_error():
    spec = bs.build_signal_for_failing_backup(
        "evolve", _state(last_error="A" * 2000, failures=3), "url",
    )
    # 600-char limit in the body block.
    assert "A" * 600 in spec["body"]
    assert "A" * 601 not in spec["body"]


def test_signature_stable_per_bot():
    """Re-observing the same bot's failure must collapse onto one Signal."""
    a = bs.build_signal_for_failing_backup("evolve", _state(failures=3), "url")
    b = bs.build_signal_for_failing_backup("evolve", _state(failures=5), "url")
    assert a["signature"] == b["signature"]


def test_signatures_distinct_per_bot():
    a = bs.build_signal_for_failing_backup("evolve", _state(failures=3), "url")
    b = bs.build_signal_for_failing_backup("team_bot_a", _state(failures=3), "url")
    assert a["signature"] != b["signature"]


# ── Classified-cause early-fire (CLASSIFIED_FIRE_THRESHOLD = 1) ──────────────
#
# Regression net for the 2026-06-07 silent-2-day failure mode: when
# last_error matches a known cause pattern (backup_diagnostics.classify
# returns non-None), fire on the FIRST failure rather than waiting for
# the generic 3-failure threshold.

_SSH_KEY_MISMATCH_STDERR = (
    "push failed: identity_sign: private key "
    "/Users/team_bot_a/.ssh/evolve-backup-team_bot_a "
    "contents do not match public\n"
    "git@github.com: Permission denied (publickey).\n"
    "fatal: Could not read from remote repository."
)


def test_classified_cause_fires_on_first_failure():
    """Day-1 fire when the cause is a known pattern — no waiting for 3."""
    spec = bs.build_signal_for_failing_backup(
        "team_bot_a",
        _state(failures=1, last_error=_SSH_KEY_MISMATCH_STDERR),
        "git@github.com:example-org/team_bot_a-workspace.git",
    )
    assert spec is not None
    assert spec["type"] == "backup_failing"  # signature-stable type
    assert spec["details"]["classified_cause"] is not None
    assert spec["details"]["classified_cause"]["cause_id"] == "ssh_key_mismatch"


def test_classified_cause_title_carries_human_label():
    """Alerts-list headline should read the cause, not a bare counter."""
    spec = bs.build_signal_for_failing_backup(
        "team_bot_a",
        _state(failures=1, last_error=_SSH_KEY_MISMATCH_STDERR),
        "git@github.com:example-org/team_bot_a-workspace.git",
    )
    assert "SSH" in spec["title"] or "key" in spec["title"].lower()
    assert "1× in a row" not in spec["title"]  # not the generic counter form


def test_classified_cause_body_includes_fix_steps():
    spec = bs.build_signal_for_failing_backup(
        "team_bot_a",
        _state(failures=1, last_error=_SSH_KEY_MISMATCH_STDERR),
        "git@github.com:example-org/team_bot_a-workspace.git",
    )
    assert "Likely cause" in spec["body"]
    assert "Fix:" in spec["body"]
    assert "Distribute Key" in spec["body"]


def test_unclassified_failure_still_uses_generic_threshold():
    """Errors that don't match any known pattern stay on the 3-failure path."""
    weird_err = "unexpected git internal #42 — nothing matches this"
    assert bs.build_signal_for_failing_backup(
        "evolve", _state(failures=1, last_error=weird_err), "url",
    ) is None
    assert bs.build_signal_for_failing_backup(
        "evolve", _state(failures=2, last_error=weird_err), "url",
    ) is None
    spec = bs.build_signal_for_failing_backup(
        "evolve", _state(failures=3, last_error=weird_err), "url",
    )
    assert spec is not None
    assert spec["details"]["classified_cause"] is None


def test_signature_stable_across_classified_and_unclassified_transition():
    """If a bot's classified cause changes to unclassified (or vice
    versa), the Signal signature must NOT change — otherwise observe()
    would create a duplicate Signal instead of updating in place.
    """
    a = bs.build_signal_for_failing_backup(
        "team_bot_a",
        _state(failures=1, last_error=_SSH_KEY_MISMATCH_STDERR),
        "url",
    )
    b = bs.build_signal_for_failing_backup(
        "team_bot_a",
        _state(failures=3, last_error="unknown error #99"),
        "url",
    )
    assert a["signature"] == b["signature"]
    # And the cause field updates between them.
    assert a["details"]["classified_cause"] is not None
    assert b["details"]["classified_cause"] is None


# ── collect_for_bot ──────────────────────────────────────────────────────────


def test_collect_skips_when_no_backup_url():
    """Bots that opted out of backup don't get monitored — no false alarms."""
    out = bs.collect_for_bot(
        Path("/nope"),
        "personal_bot",
        {"bots": {"personal_bot": {}}},  # no backupRepoUrl
        state_loader=lambda _sd, _bot: _state(failures=10),
        pat_loader=_pat_ok, visibility_checker=_private,
    )
    assert out == []


def test_collect_skips_when_no_state_file():
    """Bot configured but never attempted — nothing to fire on."""
    out = bs.collect_for_bot(
        Path("/nope"), "evolve", _cfg(),
        state_loader=lambda _sd, _bot: None,
        pat_loader=_pat_ok, visibility_checker=_private,
    )
    assert out == []


def test_collect_skips_recovered_bot():
    """consecutive_failures == 0 means most recent attempt succeeded."""
    out = bs.collect_for_bot(
        Path("/nope"), "evolve", _cfg(),
        state_loader=lambda _sd, _bot: _state(failures=0, status="ok"),
        pat_loader=_pat_ok, visibility_checker=_private,
    )
    assert out == []


def test_collect_fires_when_failing():
    out = bs.collect_for_bot(
        Path("/nope"), "evolve", _cfg(),
        state_loader=lambda _sd, _bot: _state(failures=5),
        pat_loader=_pat_ok, visibility_checker=_private,
    )
    assert len(out) == 1
    assert out[0]["bot_id"] == "evolve"


# ── Phase 1 visibility signals ──────────────────────────────────────────────


def test_collect_fires_public_repo_signal():
    out = bs.collect_for_bot(
        Path("/nope"), "evolve", _cfg(),
        state_loader=lambda _sd, _bot: None,
        pat_loader=_pat_ok, visibility_checker=_public,
    )
    assert len(out) == 1
    assert out[0]["type"] == "backup_repo_public"
    assert out[0]["severity"] == "alert"
    assert out[0]["bot_id"] == "evolve"
    assert "public" in out[0]["body"].lower()


def test_collect_fires_unverified_when_pat_missing():
    out = bs.collect_for_bot(
        Path("/nope"), "evolve", _cfg(),
        state_loader=lambda _sd, _bot: None,
        pat_loader=_no_pat, visibility_checker=_private,  # checker shouldn't be called
    )
    assert len(out) == 1
    assert out[0]["type"] == "backup_visibility_unverified"
    assert out[0]["severity"] == "warn"
    assert out[0]["details"]["reason"] == "missing_pat"
    assert "github.pat" in out[0]["body"].lower() or "pat" in out[0]["body"].lower()


def test_collect_fires_unverified_when_lookup_unknown():
    out = bs.collect_for_bot(
        Path("/nope"), "evolve", _cfg(),
        state_loader=lambda _sd, _bot: None,
        pat_loader=_pat_ok, visibility_checker=_unknown,
    )
    assert len(out) == 1
    assert out[0]["type"] == "backup_visibility_unverified"
    assert out[0]["details"]["reason"] == "lookup_failed"


def test_collect_emits_both_failing_and_public_when_both_apply():
    out = bs.collect_for_bot(
        Path("/nope"), "evolve", _cfg(),
        state_loader=lambda _sd, _bot: _state(failures=5),
        pat_loader=_pat_ok, visibility_checker=_public,
    )
    assert len(out) == 2
    types = {s["type"] for s in out}
    assert types == {"backup_failing", "backup_repo_public"}


def _key_present(_user, _bot):
    return True


def _key_missing(_user, _bot):
    return False


def _key_unknown(_user, _bot):
    return None


def test_collect_fires_ssh_key_missing_when_absent():
    out = bs.collect_for_bot(
        Path("/nope"), "evolve", _cfg(),
        state_loader=lambda _sd, _bot: None,
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_key_missing,
    )
    assert len(out) == 1
    assert out[0]["type"] == "backup_ssh_key_missing"
    assert out[0]["severity"] == "warn"
    assert out[0]["bot_id"] == "evolve"
    assert "evolve-backup-evolve" in out[0]["body"]


def test_collect_silent_when_ssh_key_check_unknown():
    """sudoers gap / can't-tell case must not fire a false positive."""
    out = bs.collect_for_bot(
        Path("/nope"), "evolve", _cfg(),
        state_loader=lambda _sd, _bot: None,
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_key_unknown,
    )
    assert out == []


def test_collect_silent_when_ssh_key_present():
    out = bs.collect_for_bot(
        Path("/nope"), "evolve", _cfg(),
        state_loader=lambda _sd, _bot: None,
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_key_present,
    )
    assert out == []


def test_collect_skips_ssh_key_check_when_no_backup_url():
    """Bots without backupRepoUrl shouldn't be probed at all — the checker
    must not be called and no signal must fire."""
    calls = []

    def _spy(user, bot):
        calls.append((user, bot))
        return False

    out = bs.collect_for_bot(
        Path("/nope"), "personal_bot",
        {"bots": {"personal_bot": {}}},
        state_loader=lambda _sd, _bot: None,
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_spy,
    )
    assert out == []
    assert calls == []


def test_ssh_key_missing_signal_resolves_to_correct_bot_user():
    """team_bot_b runs on the personal_bot_user account; the signal must
    reference the actual account name, not the bot_id (regression:
    bot_id ≠ account)."""
    config = {
        "bots": {
            "team_bot_b": {
                "user": "personal_bot_user",
                "backupRepoUrl": "git@github.com:x/team_bot_b.git",
            },
        },
    }
    captured: dict = {}

    def _spy(user, bot):
        captured["user"] = user
        captured["bot"] = bot
        return False

    out = bs.collect_for_bot(
        Path("/nope"), "team_bot_b", config,
        state_loader=lambda _sd, _bot: None,
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_spy,
    )
    assert captured == {"user": "personal_bot_user", "bot": "team_bot_b"}
    assert len(out) == 1
    # Platform-resolved home (/Users on macOS, /home on Linux CI) — the
    # assertion must match backup_signal's user_home() construction.
    from evolve_config import user_home

    expected_key = f"{user_home('personal_bot_user')}/.ssh/evolve-backup-team_bot_b"
    assert expected_key in out[0]["body"]
    assert out[0]["details"]["bot_user"] == "personal_bot_user"


def test_public_signal_signature_includes_bot_id():
    spec_a = bs.build_signal_for_public_repo("evolve", "git@github.com:x/evolve.git")
    spec_b = bs.build_signal_for_public_repo("team_bot_a", "git@github.com:x/team_bot_a.git")
    assert spec_a["signature"] != spec_b["signature"]


def test_public_signal_links_settings_url():
    spec = bs.build_signal_for_public_repo(
        "evolve", "git@github.com:evolve-ops/evolve-workspace.git",
    )
    assert "github.com/evolve-ops/evolve-workspace/settings" in spec["body"]
    assert spec["details"]["settings_url"] == (
        "https://github.com/evolve-ops/evolve-workspace/settings"
    )


# ── End-to-end run() ─────────────────────────────────────────────────────────


def test_run_fires_one_signal_per_failing_bot(tmp_path):
    states = {
        "evolve": _state(failures=4),
        "team_bot_a":    _state(failures=10),
        "admin_bot":  _state(failures=0, status="ok"),  # recovered — no signal
        "personal_bot":  None,                              # no state file
    }
    cfg = {
        "bots": {
            "evolve": {"backupRepoUrl": "git@github.com:x/evolve.git"},
            "team_bot_a":    {"backupRepoUrl": "git@github.com:x/team_bot_a.git"},
            "admin_bot":  {"backupRepoUrl": "git@github.com:x/admin_bot.git"},
            # personal_bot has no backupRepoUrl — opted out
        },
    }
    kept, n_fired, n_resolved = bs.run(
        tmp_path, cfg, bots=["evolve", "team_bot_a", "admin_bot", "personal_bot"],
        state_loader=lambda _sd, b: states.get(b),
        pat_loader=_pat_ok, visibility_checker=_private,
    )
    assert n_fired == 2
    assert n_resolved == 0
    assert len(kept) == 2
    sigs = list(signals_store.iter_active(tmp_path, producer="backup_signal"))
    bots = sorted(s.bot_id for s in sigs)
    assert bots == ["evolve", "team_bot_a"]


def test_run_sweep_resolve_archives_when_bot_recovers(tmp_path):
    cfg = {"bots": {"evolve": {"backupRepoUrl": "url"}}}

    # Pass 1: backup is failing.
    kept1, n1, _ = bs.run(
        tmp_path, cfg, bots=["evolve"],
        state_loader=lambda _sd, _bot: _state(failures=5),
        pat_loader=_pat_ok, visibility_checker=_private,
    )
    assert n1 == 1
    assert len(kept1) == 1

    # Pass 2: backup recovered (consecutive_failures dropped to 0).
    kept2, n_fired2, n_resolved2 = bs.run(
        tmp_path, cfg, bots=["evolve"],
        state_loader=lambda _sd, _bot: _state(failures=0, status="ok"),
        pat_loader=_pat_ok, visibility_checker=_private,
    )
    assert kept2 == set()
    assert n_fired2 == 0
    assert n_resolved2 == 1
    assert list(signals_store.iter_active(tmp_path, producer="backup_signal")) == []


def test_run_dry_run_does_not_write(tmp_path):
    cfg = {"bots": {"evolve": {"backupRepoUrl": "url"}}}
    _, n_fired, _ = bs.run(
        tmp_path, cfg, bots=["evolve"],
        state_loader=lambda _sd, _bot: _state(failures=5),
        pat_loader=_pat_ok, visibility_checker=_private,
        dry_run=True,
    )
    assert n_fired == 1
    assert list(signals_store.iter_active(tmp_path, producer="backup_signal")) == []


# ── End-to-end visibility coverage ──────────────────────────────────────────


def test_run_fires_public_repo_signal_in_store(tmp_path):
    cfg = {"bots": {"evolve": {"backupRepoUrl": "git@github.com:cjalden/repo.git"}}}
    kept, n_fired, _ = bs.run(
        tmp_path, cfg, bots=["evolve"],
        state_loader=lambda _sd, _bot: None,
        pat_loader=_pat_ok, visibility_checker=_public,
    )
    assert n_fired == 1
    sigs = list(signals_store.iter_active(tmp_path, producer="backup_signal"))
    assert len(sigs) == 1
    assert sigs[0].type == "backup_repo_public"
    assert sigs[0].severity == "alert"


def test_run_resolves_public_signal_when_repo_flips_private(tmp_path):
    cfg = {"bots": {"evolve": {"backupRepoUrl": "git@github.com:cjalden/repo.git"}}}

    # Pass 1: repo is public — Signal fires.
    bs.run(
        tmp_path, cfg, bots=["evolve"],
        state_loader=lambda _sd, _bot: None,
        pat_loader=_pat_ok, visibility_checker=_public,
    )
    sigs1 = list(signals_store.iter_active(tmp_path, producer="backup_signal"))
    assert len(sigs1) == 1
    assert sigs1[0].type == "backup_repo_public"

    # Pass 2: operator flipped repo private — sweep_resolve archives it.
    kept2, n_fired2, n_resolved2 = bs.run(
        tmp_path, cfg, bots=["evolve"],
        state_loader=lambda _sd, _bot: None,
        pat_loader=_pat_ok, visibility_checker=_private,
    )
    assert kept2 == set()
    assert n_fired2 == 0
    assert n_resolved2 == 1
    assert list(signals_store.iter_active(tmp_path, producer="backup_signal")) == []


# ─── Regression from the 2026-05-29 review session ───────────────────────────


def test_run_with_narrow_bots_does_not_clobber_other_bots_signals(tmp_path):
    """``--bot team_bot_a`` run must not auto-resolve admin_bot's still-firing Signal.

    Before this fix, the sweep_resolve at the end of run() walked every
    Signal under producer ``backup_signal`` and resolved any not in
    ``kept``. When invoked with bots=[team_bot_a], ``kept`` only carried team_bot_a's
    signatures so admin_bot's backup_repo_public Signal got mass-archived.
    """
    cfg = {
        "bots": {
            "team_bot_a":   {"backupRepoUrl": "git@github.com:cjalden/team_bot_a.git"},
            "admin_bot": {"backupRepoUrl": "git@github.com:cjalden/admin_bot.git"},
        },
    }
    # Pass 1: full scan — admin_bot's repo is public, fires backup_repo_public.
    bs.run(
        tmp_path, cfg, bots=["team_bot_a", "admin_bot"],
        state_loader=lambda _sd, _bot: None,
        pat_loader=_pat_ok,
        visibility_checker=(lambda url, pat=None: "public" if "admin_bot" in url else "private"),
    )
    actives = list(signals_store.iter_active(tmp_path, producer="backup_signal"))
    admin_bot_sigs = [s for s in actives if s.bot_id == "admin_bot"]
    assert len(admin_bot_sigs) == 1
    assert admin_bot_sigs[0].type == "backup_repo_public"

    # Pass 2: narrow scan on team_bot_a only (team_bot_a's repo private — nothing to fire).
    # admin_bot's still-firing Signal MUST survive the bounded sweep.
    bs.run(
        tmp_path, cfg, bots=["team_bot_a"],
        state_loader=lambda _sd, _bot: None,
        pat_loader=_pat_ok,
        visibility_checker=_private,
    )
    survivors = list(signals_store.iter_active(tmp_path, producer="backup_signal"))
    admin_bot_survivors = [s for s in survivors if s.bot_id == "admin_bot"]
    assert len(admin_bot_survivors) == 1, (
        "narrow --bot run mass-resolved admin_bot's still-firing backup_repo_public"
    )


# ── Pod-wide coalescing for missing PAT (2026-06-03 Alerts review) ──────────


def test_run_coalesces_missing_pat_into_one_pod_scope_signal(tmp_path):
    """Without a PAT, every bot with a backup repo would otherwise fire
    its own ``backup_visibility_unverified`` Signal (8 bots = 8 rows in
    the Alerts UI). The fix is a single pod-scope ``backup_pat_missing``
    Signal listing all affected bots — one operator action (set the PAT)
    clears all of them."""
    cfg = {
        "bots": {
            "evolve":     {"backupRepoUrl": "git@github.com:x/evolve.git"},
            "team_bot_a": {"backupRepoUrl": "git@github.com:x/team_bot_a.git"},
            "admin_bot":  {"backupRepoUrl": "git@github.com:x/admin_bot.git"},
        },
    }
    kept, n_fired, _ = bs.run(
        tmp_path, cfg, bots=["evolve", "team_bot_a", "admin_bot"],
        state_loader=lambda _sd, _bot: None,
        pat_loader=_no_pat,  # missing PAT — would otherwise fire 3 per-bot signals
        visibility_checker=_private,
    )
    sigs = list(signals_store.iter_active(tmp_path, producer="backup_signal"))
    # Exactly ONE Signal — pod scope, with all three bots enumerated.
    assert len(sigs) == 1, (
        f"missing PAT should coalesce to ONE pod-scope signal; got {len(sigs)}"
    )
    sig = sigs[0]
    assert sig.type == "backup_pat_missing"
    assert sig.scope == "pod"
    assert sig.bot_id is None
    assert sorted(sig.details["bot_ids"]) == ["admin_bot", "evolve", "team_bot_a"]
    assert sig.details["bot_count"] == 3
    # No per-bot backup_visibility_unverified signals remain.
    per_bot = [s for s in sigs if s.type == "backup_visibility_unverified"]
    assert per_bot == []
    assert n_fired == 1
    assert kept == {sig.signature}


def test_run_pod_missing_pat_signal_resolves_when_pat_is_added(tmp_path):
    """Once an operator sets the PAT, the pod-scope signal sweep-resolves
    on the next full run — the one-action-fixes-all promise. Uses the
    members-driven full-sweep path (bots=None) because narrow runs
    intentionally leave pod-scope signals alone (they can't know whether
    bots outside the narrow set still have missing PAT)."""
    cfg = {
        "members": ["evolve", "team_bot_a"],
        "bots": {
            "evolve":     {"backupRepoUrl": "git@github.com:x/evolve.git"},
            "team_bot_a": {"backupRepoUrl": "git@github.com:x/team_bot_a.git"},
        },
    }
    # Pass 1: no PAT — pod signal fires.
    bs.run(
        tmp_path, cfg,  # bots=None → full sweep from members
        state_loader=lambda _sd, _bot: None,
        pat_loader=_no_pat, visibility_checker=_private,
    )
    assert len(list(signals_store.iter_active(tmp_path, producer="backup_signal"))) == 1

    # Pass 2: PAT is now set, repos are private — full sweep resolves the pod signal.
    kept, n_fired, n_resolved = bs.run(
        tmp_path, cfg,
        state_loader=lambda _sd, _bot: None,
        pat_loader=_pat_ok, visibility_checker=_private,
    )
    assert n_fired == 0
    assert n_resolved == 1
    assert kept == set()
    assert list(signals_store.iter_active(tmp_path, producer="backup_signal")) == []


def test_run_narrow_bot_run_leaves_pod_missing_pat_signal_alone(tmp_path):
    """A narrow ``--bot xxx`` run must not sweep-resolve the pod-scope
    backup_pat_missing signal — it has no visibility into whether other
    bots in the pod still have the missing-PAT condition. Same rationale
    as the existing narrow-run safety net for per-bot signals."""
    cfg = {
        "bots": {
            "evolve":     {"backupRepoUrl": "git@github.com:x/evolve.git"},
            "team_bot_a": {"backupRepoUrl": "git@github.com:x/team_bot_a.git"},
        },
    }
    # Pre-populate the pod-scope signal as if a prior full sweep emitted it.
    pod_spec = bs.build_signal_for_pod_missing_pat(
        {"evolve": "git@github.com:x/evolve.git",
         "team_bot_a": "git@github.com:x/team_bot_a.git"}
    )
    signals_store.observe(tmp_path, **pod_spec)
    assert len(list(signals_store.iter_active(tmp_path, producer="backup_signal"))) == 1

    # Narrow run on team_bot_a (PAT now set, repo private) — no per-bot
    # findings, AND the pod signal must survive because this narrow run
    # didn't scan every bot.
    kept, n_fired, n_resolved = bs.run(
        tmp_path, cfg, bots=["team_bot_a"],
        state_loader=lambda _sd, _bot: None,
        pat_loader=_pat_ok, visibility_checker=_private,
    )
    survivors = list(signals_store.iter_active(tmp_path, producer="backup_signal"))
    assert len(survivors) == 1
    assert survivors[0].type == "backup_pat_missing"
    assert n_resolved == 0


def test_run_pod_missing_pat_does_not_suppress_per_bot_failing_backup(tmp_path):
    """The coalesce only fires for the missing-PAT case. A bot that is
    ALSO failing nightly backups still gets its own per-bot
    ``backup_failing`` Signal — the two are independent conditions and
    the operator needs both visible."""
    cfg = {
        "bots": {
            "evolve":     {"backupRepoUrl": "git@github.com:x/evolve.git"},
            "team_bot_a": {"backupRepoUrl": "git@github.com:x/team_bot_a.git"},
        },
    }
    states = {"evolve": _state(failures=5), "team_bot_a": None}
    kept, n_fired, _ = bs.run(
        tmp_path, cfg, bots=["evolve", "team_bot_a"],
        state_loader=lambda _sd, b: states.get(b),
        pat_loader=_no_pat, visibility_checker=_private,
    )
    sigs = sorted(
        signals_store.iter_active(tmp_path, producer="backup_signal"),
        key=lambda s: s.type,
    )
    # Two firings: the per-bot failing-backup Signal for evolve, plus the
    # pod-wide PAT missing Signal covering both bots.
    types = [s.type for s in sigs]
    assert "backup_failing" in types
    assert "backup_pat_missing" in types
    failing = next(s for s in sigs if s.type == "backup_failing")
    pod = next(s for s in sigs if s.type == "backup_pat_missing")
    assert failing.bot_id == "evolve"
    assert failing.scope == "bot"
    assert pod.bot_id is None
    assert pod.scope == "pod"
    assert n_fired == 2


def test_run_dry_run_coalesces_missing_pat_without_writing(tmp_path):
    cfg = {"bots": {"evolve": {"backupRepoUrl": "url"}}}
    _, n_fired, _ = bs.run(
        tmp_path, cfg, bots=["evolve"],
        state_loader=lambda _sd, _bot: None,
        pat_loader=_no_pat, visibility_checker=_private,
        dry_run=True,
    )
    # Counts the would-fire pod signal but no write.
    assert n_fired == 1
    assert list(signals_store.iter_active(tmp_path, producer="backup_signal")) == []


# ── Daemon-silence signal (the 2026-07-28 incident, blind spot #2) ───────────
#
# The backup_failing check keys off failure counts in state.json, so a
# daemon that never runs at all (unloaded plist, wedged launchd job)
# fired nothing while the Backup row went 10 days stale. The silence
# check anchors on last_attempt_at, falling back to the workspace's last
# [backup] commit when the state file was never written.


def test_silence_quiet_when_attempt_fresh():
    rs = {"last_attempt_at": _iso(_NOW - timedelta(hours=2))}
    assert bs.build_signal_for_daemon_silence("team_bot_a", rs, "url", now=_NOW) is None


def test_silence_quiet_just_under_threshold():
    rs = {"last_attempt_at": _iso(_NOW - timedelta(hours=25))}
    assert bs.build_signal_for_daemon_silence("team_bot_a", rs, "url", now=_NOW) is None


def test_silence_fires_past_threshold():
    rs = {"last_attempt_at": _iso(_NOW - timedelta(hours=30))}
    spec = bs.build_signal_for_daemon_silence("team_bot_a", rs, "url", now=_NOW)
    assert spec is not None
    assert spec["type"] == "backup_daemon_silent"
    assert spec["severity"] == "warn"
    assert spec["scope"] == "bot"
    assert spec["bot_id"] == "team_bot_a"
    # Remediation names the per-bot launchd job and the redeploy path.
    assert "launchctl print system/ai.evolve.team_bot_a.backup" in spec["body"]
    assert spec["details"]["anchor_source"] == "last_attempt_at"
    assert spec["details"]["state_file_missing"] is False


def test_silence_escalates_to_alert_after_a_week():
    rs = {"last_attempt_at": _iso(_NOW - timedelta(days=10))}
    spec = bs.build_signal_for_daemon_silence("team_bot_a", rs, "url", now=_NOW)
    assert spec["severity"] == "alert"


def test_silence_signature_stable_per_bot():
    rs1 = {"last_attempt_at": _iso(_NOW - timedelta(hours=30))}
    rs2 = {"last_attempt_at": _iso(_NOW - timedelta(days=9))}
    a = bs.build_signal_for_daemon_silence("team_bot_a", rs1, "url", now=_NOW)
    b = bs.build_signal_for_daemon_silence("team_bot_a", rs2, "url", now=_NOW)
    assert a["signature"] == b["signature"]
    c = bs.build_signal_for_daemon_silence("team_bot_b", rs1, "url", now=_NOW)
    assert c["signature"] != a["signature"]


def test_silence_missing_state_anchors_on_backup_commit():
    """No state.json at all, but the workspace has an old [backup] commit
    — proof backups used to run. That's silence, and the body says the
    daemon recorded nothing."""
    spec = bs.build_signal_for_daemon_silence(
        "team_bot_a", None, "url",
        now=_NOW, last_backup_commit_at=_iso(_NOW - timedelta(days=3)),
    )
    assert spec is not None
    assert spec["type"] == "backup_daemon_silent"
    assert spec["details"]["state_file_missing"] is True
    assert spec["details"]["anchor_source"] == "last_backup_commit"
    assert "no attempt" in spec["body"].lower()


def test_silence_no_anchor_stays_quiet():
    """Fresh bot: no state file AND no [backup] commit ever — there is no
    evidence the backup system ever ran, so firing 'silent' would spam
    every freshly-configured pod. Seed-once: no anchor, no fire."""
    assert bs.build_signal_for_daemon_silence(
        "team_bot_a", None, "url", now=_NOW, last_backup_commit_at=None,
    ) is None


def test_silence_unparseable_attempt_falls_back_then_fails_safe():
    """A garbage last_attempt_at with no commit anchor must not fire
    (fail-safe); with an old commit anchor it fires on that evidence."""
    rs = {"last_attempt_at": "not-a-timestamp"}
    assert bs.build_signal_for_daemon_silence(
        "team_bot_a", rs, "url", now=_NOW,
    ) is None
    spec = bs.build_signal_for_daemon_silence(
        "team_bot_a", rs, "url",
        now=_NOW, last_backup_commit_at=_iso(_NOW - timedelta(days=2)),
    )
    assert spec is not None
    assert spec["details"]["anchor_source"] == "last_backup_commit"


def test_silence_fresh_backup_commit_keeps_quiet():
    """State file missing but a [backup] commit landed hours ago — the
    daemon is committing (state write regressed?); that is not silence."""
    assert bs.build_signal_for_daemon_silence(
        "team_bot_a", None, "url",
        now=_NOW, last_backup_commit_at=_iso(_NOW - timedelta(hours=5)),
    ) is None


def test_collect_fires_silence_for_missing_state_with_commit_evidence():
    out = bs.collect_for_bot(
        Path("/nope"), "team_bot_a",
        {"bots": {"team_bot_a": {"backupRepoUrl": "url"}}},
        state_loader=lambda _sd, _bot: None,
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_key_unknown,
        backup_commit_prober=lambda _bot, _cfg: _iso(_NOW - timedelta(days=4)),
        now=_NOW,
    )
    assert len(out) == 1
    assert out[0]["type"] == "backup_daemon_silent"


def test_collect_fires_both_failing_and_silent_when_both_hold():
    """A daemon that recorded failures and then went silent shows both
    conditions — the operator needs both visible."""
    rs = _state(failures=5, last_attempt=_iso(_NOW - timedelta(days=3)))
    out = bs.collect_for_bot(
        Path("/nope"), "team_bot_a",
        {"bots": {"team_bot_a": {"backupRepoUrl": "url"}}},
        state_loader=lambda _sd, _bot: rs,
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_key_unknown,
        now=_NOW,
    )
    types = sorted(s["type"] for s in out)
    assert types == ["backup_daemon_silent", "backup_failing"]


def test_collect_does_not_probe_commits_when_attempt_parses():
    """The git prober is only consulted when state.json can't anchor the
    check — a clean fresh attempt must not spawn a git subprocess."""
    calls = []

    def _spy(bot, _cfg):
        calls.append(bot)
        return None

    out = bs.collect_for_bot(
        Path("/nope"), "team_bot_a",
        {"bots": {"team_bot_a": {"backupRepoUrl": "url"}}},
        state_loader=lambda _sd, _bot: _state(failures=0, status="ok"),
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_key_unknown,
        backup_commit_prober=_spy,
        now=_NOW,
    )
    assert out == []
    assert calls == []


# ── Unreadable state is NOT silence (fail-safe sentinel) ─────────────────────


def _unreadable_loader(_sd, _bot):
    return bs.UNREADABLE


def test_collect_unreadable_fires_unreadable_not_silence():
    out = bs.collect_for_bot(
        Path("/nope"), "team_bot_a",
        {"bots": {"team_bot_a": {"backupRepoUrl": "url"}}},
        state_loader=_unreadable_loader,
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_key_unknown,
        # Even with commit evidence of past backups, unreadable state
        # must fire the blind-spot signal, never "silent".
        backup_commit_prober=lambda _bot, _cfg: _iso(_NOW - timedelta(days=9)),
        now=_NOW,
    )
    assert len(out) == 1
    assert out[0]["type"] == "backup_state_unreadable"
    assert out[0]["severity"] == "warn"


def test_run_unreadable_protects_existing_signals_from_sweep(tmp_path):
    """Blind tick: while state.json is unreadable, the bot's existing
    backup_failing Signal must survive the sweep (we can't confirm it
    cleared), and the unreadable Signal fires. Once the state reads
    cleanly and healthy, both auto-archive."""
    cfg = {"bots": {"team_bot_a": {"backupRepoUrl": "url"}}}

    # Pass 1: failing backup observed normally.
    bs.run(
        tmp_path, cfg, bots=["team_bot_a"],
        state_loader=lambda _sd, _bot: _state(failures=5),
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_key_unknown,
        now=_NOW,
    )
    types = {s.type for s in signals_store.iter_active(tmp_path, producer="backup_signal")}
    assert types == {"backup_failing"}

    # Pass 2: state goes unreadable — failing survives, unreadable fires.
    bs.run(
        tmp_path, cfg, bots=["team_bot_a"],
        state_loader=_unreadable_loader,
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_key_unknown,
        now=_NOW,
    )
    types = {s.type for s in signals_store.iter_active(tmp_path, producer="backup_signal")}
    assert types == {"backup_failing", "backup_state_unreadable"}, (
        "unreadable state must not sweep-resolve the still-unconfirmed backup_failing"
    )

    # Pass 3: state reads cleanly and healthy — everything archives.
    _, n_fired, n_resolved = bs.run(
        tmp_path, cfg, bots=["team_bot_a"],
        state_loader=lambda _sd, _bot: _state(failures=0, status="ok"),
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_key_unknown,
        now=_NOW,
    )
    assert n_fired == 0
    assert n_resolved == 2
    assert list(signals_store.iter_active(tmp_path, producer="backup_signal")) == []


def test_read_run_state_distinguishes_missing_from_unreadable(tmp_path, monkeypatch):
    """FileNotFoundError → None (genuinely no attempt recorded);
    EACCES / corrupt JSON → UNREADABLE (monitor blind)."""
    import evolve_config

    workspace = tmp_path / "home" / ".openclaw" / "workspace"
    (workspace / "evolve-backup").mkdir(parents=True)
    monkeypatch.setattr(
        evolve_config, "bot_home", lambda _bot, _cfg=None: tmp_path / "home",
    )
    monkeypatch.setattr(evolve_config, "load_config", lambda *_a, **_k: {})

    # Missing file → None.
    assert bs._read_run_state(tmp_path, "team_bot_a") is None

    # Corrupt JSON → UNREADABLE.
    state_path = workspace / "evolve-backup" / "state.json"
    state_path.write_text("{not json", encoding="utf-8")
    assert bs._read_run_state(tmp_path, "team_bot_a") is bs.UNREADABLE

    # Valid JSON → dict.
    state_path.write_text('{"consecutive_failures": 0}', encoding="utf-8")
    assert bs._read_run_state(tmp_path, "team_bot_a") == {"consecutive_failures": 0}


# ── End-to-end silence through the store ─────────────────────────────────────


def test_run_silence_fires_and_resolves_on_recovery(tmp_path):
    cfg = {"bots": {"team_bot_a": {"backupRepoUrl": "url"}}}

    # Pass 1: daemon silent (attempt 3 days old).
    stale = _state(failures=0, status="ok",
                   last_attempt=_iso(_NOW - timedelta(days=3)))
    kept, n_fired, _ = bs.run(
        tmp_path, cfg, bots=["team_bot_a"],
        state_loader=lambda _sd, _bot: stale,
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_key_unknown,
        now=_NOW,
    )
    assert n_fired == 1
    sigs = list(signals_store.iter_active(tmp_path, producer="backup_signal"))
    assert len(sigs) == 1
    assert sigs[0].type == "backup_daemon_silent"

    # Pass 2: daemon recovered (fresh attempt) — sweep archives it.
    fresh = _state(failures=0, status="ok",
                   last_attempt=_iso(_NOW - timedelta(hours=1)))
    kept2, n_fired2, n_resolved2 = bs.run(
        tmp_path, cfg, bots=["team_bot_a"],
        state_loader=lambda _sd, _bot: fresh,
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_key_unknown,
        now=_NOW,
    )
    assert kept2 == set()
    assert n_fired2 == 0
    assert n_resolved2 == 1
    assert list(signals_store.iter_active(tmp_path, producer="backup_signal")) == []
