"""tests/test_monitor_gmail_integration_health.py — monitor (PR δ) coverage.

Covers the gmail_integration_health monitor end-to-end:

  - categorization → Signal type + severity mapping per spec §8.1
  - signature dedupe across probes (one Signal per (bot, category))
  - sweep_resolve on next clean probe
  - transient 5xx requires MIN_CONSECUTIVE_TRANSIENT consecutive failures
  - bots without google_integration are skipped cleanly
  - Path A / B bots are skipped at config-read (NotImplementedError
    would otherwise blow up the run)
  - reauth_contact carried through to Signal.details so the alerts
    subscriber can route the DM
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_PKG_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (_ANALYZER_DIR, _ADMIN_PKG_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import monitor_gmail_integration_health as monitor  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


def _network(bots: dict) -> dict:
    return {"bots": bots}


def _gi(
    *,
    mode: str = "service_account_dwd",
    workspace_domain: str = "example-corp.com",
    subject: str = "lex@example-corp.com",
    secret_ref: str = "google-sa-example-corp",
    scopes: list[str] | None = None,
    reauth_contact: dict | None = None,
) -> dict:
    out = {
        "mode": mode,
        "workspace_domain": workspace_domain,
        "subject": subject,
        "service_account_secret_ref": secret_ref,
        "scopes": scopes or ["https://www.googleapis.com/auth/gmail.readonly"],
    }
    if reauth_contact is not None:
        out["reauth_contact"] = reauth_contact
    return out


def _probe(result_by_bot: dict[str, dict]):
    """Build a probe_runner that returns canned results per bot.

    Accepts **kwargs so the fake stays compatible with the live
    google_preflight.run_preflight signature
    (``bot_scopes`` / ``subject`` / ``workspace_domain`` / ``network``).
    """
    def runner(bot_id, **_kwargs):
        return dict(result_by_bot[bot_id])
    return runner


def _ok_result() -> dict:
    return {
        "ok": True,
        "category": "ok",
        "hint": "",
        "error": None,
        "http_status": 200,
        "profile": {"emailAddress": "lex@example-corp.com"},
    }


def _fail_result(category: str, *, http_status: int | None = None, error: str | None = None) -> dict:
    return {
        "ok": False,
        "category": category,
        "hint": f"hint for {category}",
        "error": error or f"pre-flight call failed: {category} error",
        "http_status": http_status,
        "profile": None,
    }


# ── bots_with_google_integration ────────────────────────────────────────────


def test_skips_bots_without_google_integration():
    net = _network({
        "lex": {"google_integration": _gi()},
        "team_bot_a": {"role": "member"},  # no google_integration
    })
    assert monitor.bots_with_google_integration(net) == ["lex"]


def test_path_b_still_skipped_at_probe(tmp_path):
    """Phase A.4: Path A is now probed; Path B still raises NotImplementedError
    in load_credentials so the monitor skips it. Drops cleanly when path B
    lands."""
    net = _network({
        "lex":   {"google_integration": _gi()},
        "ada":   {"google_integration": _gi(mode="free_gmail_oauth")},
        "team_bot_b": {"google_integration": _gi(mode="workspace_user_oauth")},
    })
    # Probe stubbed for both lex (path C) and ada (path A); team_bot_b skipped.
    probe = _probe({"lex": _ok_result(), "ada": _ok_result()})
    state = {}
    specs, probed = monitor.collect(
        net, state=state, shared_dir=tmp_path, probe_runner=probe,
    )
    assert specs == []   # both ok
    assert probed == {"lex", "ada"}


# ── Signal-spec shape per category ──────────────────────────────────────────


@pytest.mark.parametrize(
    "category,expected_type,expected_severity",
    [
        ("dwd_unauthorized",   "dwd_unauthorized",   "alert"),
        ("scope_unauthorized", "scope_unauthorized", "alert"),
        ("subject_not_found",  "subject_not_found",  "alert"),
        ("key_revoked",        "sa_key_revoked",     "alert"),
        ("library_missing",    "library_missing",    "warn"),
        ("config_missing",     "config_missing",     "warn"),
        ("unknown",            "unknown_failure",    "warn"),
        # Phase A.4 — path-A only
        ("refresh_token_expired", "gmail_oauth_reauth_required", "alert"),
    ],
)
def test_each_category_maps_to_expected_signal_shape(
    tmp_path, category, expected_type, expected_severity,
):
    net = _network({"lex": {"google_integration": _gi()}})
    probe = _probe({"lex": _fail_result(category, http_status=401)})
    state = {}
    specs, _probed = monitor.collect(
        net, state=state, shared_dir=tmp_path, probe_runner=probe,
    )
    assert len(specs) == 1
    s = specs[0]
    assert s["producer"] == monitor.PRODUCER
    assert s["type"] == expected_type
    assert s["severity"] == expected_severity
    assert s["scope"] == "bot"
    assert s["bot_id"] == "lex"
    assert s["flavor"] == "maintenance"
    # Signature is stable per (bot, category) — the kept-set semantics
    # depend on this.
    assert f"lex/{category}" in s["signature"] or s["signature"].endswith(category)
    # The shared hint catalog provides the remediation text — Signal body
    # must include it so the operator can fix without leaving the alert.
    assert f"hint for {category}" in s["body"]
    # last_check_at + last_error_code + last_error_message in details
    # (spec §8.3).
    assert s["details"]["category"] == category
    assert s["details"]["last_check_at"]
    assert s["details"]["last_error_code"] == 401
    assert s["details"]["last_error_message"]


def test_reauth_contact_carried_into_signal_details(tmp_path):
    """spec §8.3: details.reauth_contact lets the alerts subscriber route
    the DM to the bot's configured channel without re-reading network.json."""
    contact = {"channel": "telegram", "user_external_id": "U-789"}
    net = _network({
        "lex": {"google_integration": _gi(reauth_contact=contact)},
    })
    probe = _probe({"lex": _fail_result("dwd_unauthorized", http_status=401)})
    state = {}
    specs, _probed = monitor.collect(
        net, state=state, shared_dir=tmp_path, probe_runner=probe,
    )
    assert specs[0]["details"]["reauth_contact"] == contact


# ── Signature dedupe via signals.store.observe ──────────────────────────────


def test_signature_dedupe_one_signal_across_multiple_runs(tmp_path):
    """Same (bot, category) produces ONE firing Signal across repeated runs.

    This is the load-bearing property: a persistent 401 for 24 hours
    must not flood the operator with 48 alerts. signals.store.observe()
    handles the dedup via signature; this test confirms the monitor
    reuses the right signature.
    """
    net = _network({"lex": {"google_integration": _gi()}})
    probe = _probe({"lex": _fail_result("dwd_unauthorized", http_status=401)})

    # Three runs in a row.
    for _ in range(3):
        monitor.run(tmp_path, net, probe_runner=probe)

    firing = list(signals_store.iter_signals(tmp_path, subdirs=("firing",)))
    assert len(firing) == 1
    sig = firing[0]
    assert sig.producer == monitor.PRODUCER
    assert sig.type == "dwd_unauthorized"
    # observation_count tracks the bumps; we observed three times.
    assert sig.observation_count == 3


def test_different_category_fires_distinct_signal(tmp_path):
    """A 401-then-403 sequence resolves the first Signal and fires a new
    one — they're different root causes with different fixes."""
    net = _network({"lex": {"google_integration": _gi()}})
    # First run: 401
    monitor.run(tmp_path, net, probe_runner=_probe({
        "lex": _fail_result("dwd_unauthorized", http_status=401),
    }))
    # Second run: 403 — the 401 is no longer firing, the 403 is.
    monitor.run(tmp_path, net, probe_runner=_probe({
        "lex": _fail_result("scope_unauthorized", http_status=403),
    }))

    firing = list(signals_store.iter_signals(tmp_path, subdirs=("firing",)))
    assert len(firing) == 1
    assert firing[0].type == "scope_unauthorized"
    # The original 401 was sweep-resolved.
    archived = list(signals_store.iter_signals(tmp_path, subdirs=("archived",)))
    assert any(s.type == "dwd_unauthorized" and s.state == "resolved"
               for s in archived)


# ── Sweep-resolve on success ────────────────────────────────────────────────


def test_sweep_resolve_on_clean_probe(tmp_path):
    """A previously-firing Signal auto-archives when the next probe is ok."""
    net = _network({"lex": {"google_integration": _gi()}})
    # Fire a Signal.
    monitor.run(tmp_path, net, probe_runner=_probe({
        "lex": _fail_result("dwd_unauthorized", http_status=401),
    }))
    firing_before = list(signals_store.iter_signals(tmp_path, subdirs=("firing",)))
    assert len(firing_before) == 1

    # Next run: probe succeeds.
    _kept, _n_fired, n_resolved = monitor.run(
        tmp_path, net, probe_runner=_probe({"lex": _ok_result()}),
    )
    assert n_resolved == 1

    firing_after = list(signals_store.iter_signals(tmp_path, subdirs=("firing",)))
    assert firing_after == []
    archived = list(signals_store.iter_signals(tmp_path, subdirs=("archived",)))
    assert any(s.type == "dwd_unauthorized" and s.state == "resolved"
               for s in archived)


def test_sweep_resolve_scoped_to_probed_bots(tmp_path):
    """When --bot narrows the run, sweep_resolve only touches that bot's
    Signals — otherwise we'd mass-resolve every other bot's still-firing
    signals (kept_signatures only carries the narrowed bot's set)."""
    net = _network({
        "lex":  {"google_integration": _gi()},
        "kira": {"google_integration": _gi(subject="kira@example-corp.com")},
    })
    # Fire on both bots.
    monitor.run(tmp_path, net, probe_runner=_probe({
        "lex":  _fail_result("dwd_unauthorized", http_status=401),
        "kira": _fail_result("dwd_unauthorized", http_status=401),
    }))
    assert len(list(signals_store.iter_signals(
        tmp_path, subdirs=("firing",)))) == 2

    # Re-probe lex only with success; kira's Signal must stay firing.
    monitor.run(
        tmp_path, net,
        probe_runner=_probe({"lex": _ok_result()}),
        only_bot="lex",
    )
    firing = list(signals_store.iter_signals(tmp_path, subdirs=("firing",)))
    assert len(firing) == 1
    assert firing[0].bot_id == "kira"


# ── Transient 5xx: MIN_CONSECUTIVE_TRANSIENT throttling ─────────────────────


def test_transient_failure_holds_until_threshold(tmp_path):
    """spec §8.1: 5xx / network errors don't fire until we've seen
    MIN_CONSECUTIVE_TRANSIENT in a row. Avoids paging on single-blip
    Google availability dips."""
    net = _network({"lex": {"google_integration": _gi()}})
    probe = _probe({"lex": _fail_result("transient", http_status=503)})

    # First N-1 runs: no Signal yet.
    for i in range(monitor.MIN_CONSECUTIVE_TRANSIENT - 1):
        kept, n_fired, _ = monitor.run(tmp_path, net, probe_runner=probe)
        assert n_fired == 0, f"run {i+1} fired prematurely"

    # Nth run: now fires.
    kept, n_fired, _ = monitor.run(tmp_path, net, probe_runner=probe)
    assert n_fired == 1
    firing = list(signals_store.iter_signals(tmp_path, subdirs=("firing",)))
    assert len(firing) == 1
    assert firing[0].type == "transient_failure"


def test_transient_counter_resets_on_success(tmp_path):
    """A clean probe between transient failures resets the counter so we
    don't accumulate flap into a false-positive fire."""
    net = _network({"lex": {"google_integration": _gi()}})
    transient = _probe({"lex": _fail_result("transient", http_status=503)})
    ok = _probe({"lex": _ok_result()})

    # 2 transient → 1 ok → 2 transient. Should not fire because each run
    # of 2 is below MIN_CONSECUTIVE_TRANSIENT (3).
    for _ in range(2):
        monitor.run(tmp_path, net, probe_runner=transient)
    monitor.run(tmp_path, net, probe_runner=ok)
    for _ in range(2):
        monitor.run(tmp_path, net, probe_runner=transient)
    assert list(signals_store.iter_signals(tmp_path, subdirs=("firing",))) == []


def test_transient_counter_resets_on_non_transient_category(tmp_path):
    """A non-transient probe between transient runs resets the counter.

    After 2 transient + 1 non-transient (which fires the non-transient
    Signal AND resets the counter), the next 2 transient runs must stay
    below threshold — proving the counter genuinely zeroed rather than
    continuing from 2.
    """
    net = _network({"lex": {"google_integration": _gi()}})
    transient = _probe({"lex": _fail_result("transient", http_status=503)})
    auth = _probe({"lex": _fail_result("dwd_unauthorized", http_status=401)})

    # 2 transient (below threshold) — counter = 2, nothing fires.
    monitor.run(tmp_path, net, probe_runner=transient)
    monitor.run(tmp_path, net, probe_runner=transient)
    # 401 fires the dwd_unauthorized Signal and resets the counter.
    monitor.run(tmp_path, net, probe_runner=auth)
    firing_a = list(signals_store.iter_signals(tmp_path, subdirs=("firing",)))
    assert [s.type for s in firing_a] == ["dwd_unauthorized"]

    # 2 more transient. If the counter had carried (2+1+2=5 ≥ 3) we'd
    # see a transient_failure Signal here; we shouldn't, because the
    # 401 reset it.
    monitor.run(tmp_path, net, probe_runner=transient)
    n_fired = monitor.run(tmp_path, net, probe_runner=transient)[1]
    assert n_fired == 0, "transient counter did not reset on non-transient run"


# ── Empty / no-op cases ─────────────────────────────────────────────────────


def test_no_google_bots_is_clean_no_op(tmp_path):
    net = _network({"team_bot_a": {"role": "member"}})
    kept, n_fired, n_resolved = monitor.run(
        tmp_path, net, probe_runner=_probe({}),
    )
    assert kept == set()
    assert n_fired == 0
    assert n_resolved == 0


def test_unknown_bot_filter_returns_clean(tmp_path):
    net = _network({"lex": {"google_integration": _gi()}})
    kept, n_fired, n_resolved = monitor.run(
        tmp_path, net,
        probe_runner=_probe({"lex": _ok_result()}),
        only_bot="ghost",
    )
    assert (kept, n_fired, n_resolved) == (set(), 0, 0)


# ── Dry-run mode ────────────────────────────────────────────────────────────


def test_dry_run_doesnt_write_signals(tmp_path):
    net = _network({"lex": {"google_integration": _gi()}})
    probe = _probe({"lex": _fail_result("dwd_unauthorized", http_status=401)})
    kept, n_fired, n_resolved = monitor.run(
        tmp_path, net, probe_runner=probe, dry_run=True,
    )
    assert n_fired == 1
    assert n_resolved == 0
    # No signal files on disk.
    assert list(signals_store.iter_signals(
        tmp_path, subdirs=("firing", "snoozed", "archived"))) == []


# ── Probe-runner failure handling ───────────────────────────────────────────


def test_monitor_passes_scope_subject_domain_to_probe_runner(tmp_path):
    """The monitor must forward each bot's configured scopes + subject +
    workspace_domain so the shared run_preflight picks a scope-matched
    probe and produces subject-aware hints (PR #1934 helpers).
    """
    seen_kwargs: dict = {}
    def recording_probe(bot_id, **kwargs):
        seen_kwargs.update(kwargs)
        return _ok_result()

    scopes = [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/calendar.readonly",
    ]
    net = _network({
        "lex": {"google_integration": _gi(
            scopes=scopes,
            subject="lex@example-corp.com",
            workspace_domain="example-corp.com",
        )},
    })
    monitor.run(tmp_path, net, probe_runner=recording_probe)

    assert seen_kwargs.get("bot_scopes") == scopes
    assert seen_kwargs.get("subject") == "lex@example-corp.com"
    assert seen_kwargs.get("workspace_domain") == "example-corp.com"


def test_probe_runner_exception_treated_as_unknown(tmp_path):
    """If the probe_runner itself throws (it shouldn't, but...), the
    monitor classifies it as unknown_failure and continues with the
    other bots rather than crashing the entire run."""
    def crashing_probe(bot_id, **_kwargs):
        if bot_id == "lex":
            raise RuntimeError("simulated probe crash")
        return _ok_result()

    net = _network({
        "lex":  {"google_integration": _gi()},
        "kira": {"google_integration": _gi(subject="kira@example-corp.com")},
    })
    kept, n_fired, _ = monitor.run(
        tmp_path, net, probe_runner=crashing_probe,
    )
    assert n_fired == 1
    firing = list(signals_store.iter_signals(tmp_path, subdirs=("firing",)))
    assert len(firing) == 1
    assert firing[0].bot_id == "lex"
    assert firing[0].type == "unknown_failure"


# ── Phase A.4: Path-A specific signal shape ─────────────────────────────────


def test_path_a_refresh_token_expired_routes_to_personal_wizard(tmp_path):
    """A free_gmail_oauth bot with refresh_token_expired fires a
    gmail_oauth_reauth_required Signal with a deep link to the
    Personal-Gmail wizard's re-consent endpoint."""
    net = _network({
        "ada": {"google_integration": _gi(mode="free_gmail_oauth")},
    })
    probe = _probe({
        "ada": _fail_result("refresh_token_expired", http_status=400),
    })
    state = {}
    specs, _probed = monitor.collect(
        net, state=state, shared_dir=tmp_path, probe_runner=probe,
    )
    assert len(specs) == 1
    s = specs[0]
    assert s["type"] == "gmail_oauth_reauth_required"
    assert s["severity"] == "alert"
    assert s["details"]["mode"] == "free_gmail_oauth"
    assert s["details"]["remediation_url"] == (
        "/api/wizard/google/personal/reconsent?bot=ada"
    )
    assert "runbook-google-oauth-personal" in s["details"]["runbook_url"]


def test_path_c_signal_still_points_at_spec_section(tmp_path):
    """Path C bots get the Path-C runbook deep link, not the personal one."""
    net = _network({
        "lex": {"google_integration": _gi(mode="service_account_dwd")},
    })
    probe = _probe({"lex": _fail_result("dwd_unauthorized", http_status=401)})
    state = {}
    specs, _probed = monitor.collect(
        net, state=state, shared_dir=tmp_path, probe_runner=probe,
    )
    assert len(specs) == 1
    s = specs[0]
    assert s["details"]["mode"] == "service_account_dwd"
    assert "spec-google-integration-paths" in s["details"]["remediation_url"]
    assert "runbook-path-c" in s["details"]["runbook_url"]
