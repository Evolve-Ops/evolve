"""Tests for evolve_admin.fd_limits — the in-process soft NOFILE raise.

Part of the 2026-07-28 incident response: launchd's 256 soft default let
werkzeug request bursts hit EMFILE and kill the unix-socket accept path.
The rlimit is process-global state, so every test monkeypatches the
``resource`` calls instead of mutating the real limit.
"""
from __future__ import annotations

import logging
import resource

from evolve_admin.fd_limits import DEFAULT_NOFILE_TARGET, raise_nofile_limit


def test_raises_soft_limit_to_target_capped_by_hard(monkeypatch):
    calls: list = []
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (256, 10240))
    monkeypatch.setattr(
        resource, "setrlimit", lambda which, pair: calls.append((which, pair)),
    )
    assert raise_nofile_limit(4096) == (4096, 10240)
    assert calls == [(resource.RLIMIT_NOFILE, (4096, 10240))]


def test_target_clamped_to_hard_limit(monkeypatch):
    calls: list = []
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (256, 1024))
    monkeypatch.setattr(
        resource, "setrlimit", lambda which, pair: calls.append((which, pair)),
    )
    assert raise_nofile_limit(4096) == (1024, 1024)
    assert calls == [(resource.RLIMIT_NOFILE, (1024, 1024))]


def test_infinite_hard_limit_does_not_min_to_negative(monkeypatch):
    """RLIM_INFINITY is -1 on Linux — a naive min(target, hard) would try
    to SET a negative soft limit. Must use the target outright."""
    calls: list = []
    monkeypatch.setattr(
        resource, "getrlimit", lambda _which: (256, resource.RLIM_INFINITY),
    )
    monkeypatch.setattr(
        resource, "setrlimit", lambda which, pair: calls.append((which, pair)),
    )
    assert raise_nofile_limit(4096) == (4096, resource.RLIM_INFINITY)
    assert calls == [
        (resource.RLIMIT_NOFILE, (4096, resource.RLIM_INFINITY)),
    ]


def test_never_lowers_an_already_higher_soft_limit(monkeypatch):
    calls: list = []
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (8192, 10240))
    monkeypatch.setattr(
        resource, "setrlimit", lambda which, pair: calls.append((which, pair)),
    )
    assert raise_nofile_limit(4096) == (8192, 10240)
    assert calls == []  # untouched


def test_setrlimit_failure_is_logged_not_raised(monkeypatch, caplog):
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (256, 10240))

    def _boom(_which, _pair):
        raise ValueError("not allowed to raise the hard limit")

    monkeypatch.setattr(resource, "setrlimit", _boom)
    with caplog.at_level(logging.WARNING, logger="evolve_admin.fd_limits"):
        assert raise_nofile_limit(4096) is None
    assert any("could not raise NOFILE" in r.getMessage() for r in caplog.records)


def test_default_target_matches_jobspec_cap():
    """The in-process raise and the launchd/systemd job-file cap must agree
    — a drifted pair would make the effective limit deploy-order-dependent."""
    from evolve_admin import deploy
    assert DEFAULT_NOFILE_TARGET == 4096
    spec = deploy._admin_ui_jobspec("ai.evolve.evolve.admin-ui")
    assert spec.soft_file_limit == DEFAULT_NOFILE_TARGET
