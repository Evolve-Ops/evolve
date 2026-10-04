"""Audit-driven re-pin sweep for post-migration OC installs.

Spec context: OC v2026.5.28+ migrated ``~/.openclaw/plugins/installs.json``
to ``installs.json.migrated``. The pre-existing file-based reconciler in
``deploy.ensure_plugin_config`` reads the legacy path and returns None,
so the re-pin loop becomes a no-op for migrated bots. The new sweep added
in this PR drives the re-pin from ``openclaw security audit --json``
output instead, cross-references plugin ids with ``openclaw plugins list
--json``, and re-installs each unpinned npm plugin at the live runtime
version with ``--force``.

Live trigger: 2026-06-06 test pod had codex unpinned across 5 bots
that had installed it pre-migration. The OC audit reported each one,
the operator-side file inspection looked clean
(installs.json.migrated showed ``spec=@openclaw/codex@2026.5.18``), and
the existing file-based reconciler did nothing because its read path
returned None.

These tests cover:
  - _parse_unpinned_finding_detail: detail-string → [(plugin_id, npm_pkg)]
    parsing. Multiple lines, missing detail, malformed lines, trailing
    whitespace.
  - _live_plugin_versions: openclaw plugins list --json → {id: version}.
    Tolerates non-zero exit, malformed JSON, missing keys, null versions.
  - _repin_unpinned_via_audit: end-to-end orchestration mocked. Confirms
    the function actually invokes install_externalized_plugin with the
    right (npm_pkg, version) for each unpinned plugin and surfaces per-
    plugin failures without raising.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy  # noqa: E402


# ── _parse_unpinned_finding_detail ───────────────────────────────────────────


class TestParseUnpinnedFindingDetail:
    """The detail format OC emits today is one ``- plugin_id (npm_pkg)`` line
    per unpinned plugin, after a header line. We tolerate that format and
    any future-but-similar shape; anything that doesn't match the line
    regex is silently dropped (caller doesn't care)."""

    def test_single_unpinned_entry(self):
        detail = (
            "Unpinned plugin index install records:\n"
            "- codex (@openclaw/codex)\n"
        )
        assert deploy._parse_unpinned_finding_detail(detail) == [
            ("codex", "@openclaw/codex"),
        ]

    def test_multiple_unpinned_entries(self):
        detail = (
            "Unpinned plugin index install records:\n"
            "- codex (@openclaw/codex)\n"
            "- slack (@openclaw/slack)\n"
            "- brave (@openclaw/brave-plugin)\n"
        )
        result = deploy._parse_unpinned_finding_detail(detail)
        assert ("codex", "@openclaw/codex") in result
        assert ("slack", "@openclaw/slack") in result
        assert ("brave", "@openclaw/brave-plugin") in result
        assert len(result) == 3

    def test_empty_detail_returns_empty(self):
        assert deploy._parse_unpinned_finding_detail("") == []

    def test_none_detail_returns_empty(self):
        assert deploy._parse_unpinned_finding_detail(None) == []  # type: ignore[arg-type]

    def test_header_only_no_entries_returns_empty(self):
        detail = "Unpinned plugin index install records:\n"
        assert deploy._parse_unpinned_finding_detail(detail) == []

    def test_malformed_lines_silently_skipped(self):
        detail = (
            "Unpinned plugin index install records:\n"
            "- codex (@openclaw/codex)\n"
            "not a list item\n"
            "- malformed without parens\n"
            "- slack (@openclaw/slack)\n"
        )
        result = deploy._parse_unpinned_finding_detail(detail)
        assert result == [
            ("codex", "@openclaw/codex"),
            ("slack", "@openclaw/slack"),
        ]

    def test_tolerates_whitespace(self):
        detail = (
            "Unpinned plugin index install records:\n"
            "  -  codex  (@openclaw/codex)  \n"
        )
        assert deploy._parse_unpinned_finding_detail(detail) == [
            ("codex", "@openclaw/codex"),
        ]


# ── _live_plugin_versions ────────────────────────────────────────────────────


class TestLivePluginVersions:
    """openclaw plugins list --json returns a payload with a top-level
    ``plugins`` array; each entry has ``id`` and ``version``. We tolerate
    every shape of malformed output."""

    def _patch_run(self, returncode: int = 0, stdout: str = ""):
        proc = MagicMock(returncode=returncode, stdout=stdout, stderr="")
        return patch.object(deploy.subprocess, "run", return_value=proc)

    def test_returns_id_to_version_map(self):
        payload = {
            "plugins": [
                {"id": "codex", "version": "2026.6.1"},
                {"id": "slack", "version": "2026.5.18"},
                {"id": "evolve", "version": "0.1.0"},
            ],
        }
        with self._patch_run(stdout=json.dumps(payload)):
            result = deploy._live_plugin_versions("team-bot-a")
        assert result == {
            "codex": "2026.6.1",
            "slack": "2026.5.18",
            "evolve": "0.1.0",
        }

    def test_skips_plugins_with_null_version(self):
        """Internal-only plugins (e.g. ``active-memory``) sometimes have
        version: null. Drop them — we have no pin target."""
        payload = {
            "plugins": [
                {"id": "codex", "version": "2026.6.1"},
                {"id": "active-memory", "version": None},
            ],
        }
        with self._patch_run(stdout=json.dumps(payload)):
            result = deploy._live_plugin_versions("team-bot-a")
        assert result == {"codex": "2026.6.1"}

    def test_returns_empty_on_nonzero_exit(self):
        with self._patch_run(returncode=1, stdout=""):
            assert deploy._live_plugin_versions("team-bot-a") == {}

    def test_returns_empty_on_malformed_json(self):
        with self._patch_run(stdout="not json at all"):
            assert deploy._live_plugin_versions("team-bot-a") == {}

    def test_returns_empty_on_missing_plugins_key(self):
        with self._patch_run(stdout=json.dumps({"plugins": None})):
            assert deploy._live_plugin_versions("team-bot-a") == {}


# ── _repin_unpinned_via_audit — orchestration ────────────────────────────────


class TestRepinUnpinnedViaAudit:
    """The orchestration runs three subprocesses (audit, plugins list,
    install per plugin) and reports per-plugin success/failure. Mock the
    boundary cleanly so the assertions read as intent rather than
    subprocess details."""

    def _make_audit_finding(self, detail: str) -> dict:
        return {
            "checkId": "plugins.installs_unpinned_npm_specs",
            "severity": "warn",
            "title": "Plugin index includes unpinned npm specs",
            "detail": detail,
            "remediation": "Pin install specs to exact versions.",
        }

    def test_repins_each_unpinned_plugin_at_live_version(self, monkeypatch):
        """Canonical success: audit reports codex unpinned, plugins list
        reports codex at 2026.6.1, install_externalized_plugin gets
        called with (npm_pkg=@openclaw/codex, version=2026.6.1) and
        returns ok."""
        audit_payload = {
            "findings": [
                self._make_audit_finding(
                    "Unpinned plugin index install records:\n"
                    "- codex (@openclaw/codex)\n",
                ),
            ],
        }
        plugins_payload = {
            "plugins": [{"id": "codex", "version": "2026.6.1"}],
        }

        # Two subprocess.run calls: audit first, then plugins list.
        run_results = [
            MagicMock(returncode=0, stdout=json.dumps(audit_payload), stderr=""),
            MagicMock(returncode=0, stdout=json.dumps(plugins_payload), stderr=""),
        ]
        monkeypatch.setattr(deploy.subprocess, "run",
                            MagicMock(side_effect=run_results))

        # install_externalized_plugin returns (ok, err); mock + capture args.
        captured: list[dict] = []

        def _fake_install(user, pkg, *, force, version, allow_unlisted=False):
            captured.append(
                {"user": user, "pkg": pkg, "force": force, "version": version,
                 "allow_unlisted": allow_unlisted},
            )
            return (True, "")

        monkeypatch.setattr(
            "evolve_admin.oc_neutralize.install_externalized_plugin",
            _fake_install,
        )

        ok_ids, failures = deploy._repin_unpinned_via_audit(
            "team-bot-a", "team-bot-a-user",
        )

        assert ok_ids == ["codex"]
        assert failures == []
        assert len(captured) == 1
        assert captured[0] == {
            "user": "team-bot-a-user",
            "pkg": "@openclaw/codex",
            "force": True,
            "version": "2026.6.1",
            # The Layer 1 provenance gate must WARN, not refuse, on a re-pin:
            # `pkg` here is parsed out of an audit finding's detail string, so
            # it comes from OC's install records rather than a repo constant.
            # internal/design-plugin-install-provenance-gate-2026-08-11.md §4.
            "allow_unlisted": True,
        }

    def test_no_unpinned_finding_short_circuits(self, monkeypatch):
        """If the audit returns no unpinned findings, we don't even
        bother calling plugins list — saves the ~5s subprocess."""
        audit_payload = {"findings": []}
        run_proc = MagicMock(
            returncode=0, stdout=json.dumps(audit_payload), stderr="",
        )
        run_mock = MagicMock(return_value=run_proc)
        monkeypatch.setattr(deploy.subprocess, "run", run_mock)

        ok_ids, failures = deploy._repin_unpinned_via_audit(
            "team-bot-a", "team-bot-a-user",
        )

        assert ok_ids == []
        assert failures == []
        # Exactly one subprocess call — the audit. Plugins-list was skipped.
        assert run_mock.call_count == 1

    def test_audit_failure_short_circuits(self, monkeypatch):
        """openclaw security audit returning non-zero exit → silent skip.
        Not our job to report OC-side audit failures here; the regular
        audit_oc_security pass surfaces those."""
        run_proc = MagicMock(returncode=1, stdout="", stderr="audit died")
        monkeypatch.setattr(deploy.subprocess, "run",
                            MagicMock(return_value=run_proc))
        assert deploy._repin_unpinned_via_audit(
            "team-bot-a", "team-bot-a-user",
        ) == ([], [])

    def test_skips_plugin_without_live_version(self, monkeypatch):
        """The audit flagged a plugin but plugins list doesn't report a
        version for it (maybe a stale install record left behind). Skip
        — we won't guess a pin — and surface the skip via failures."""
        audit_payload = {
            "findings": [
                self._make_audit_finding(
                    "- codex (@openclaw/codex)\n"
                    "- ghost (@openclaw/ghost)\n",
                ),
            ],
        }
        plugins_payload = {
            "plugins": [{"id": "codex", "version": "2026.6.1"}],
        }
        monkeypatch.setattr(deploy.subprocess, "run", MagicMock(side_effect=[
            MagicMock(returncode=0, stdout=json.dumps(audit_payload), stderr=""),
            MagicMock(returncode=0, stdout=json.dumps(plugins_payload), stderr=""),
        ]))

        captured = []

        def _fake_install(user, pkg, *, force, version, allow_unlisted=False):
            captured.append({"pkg": pkg, "version": version})
            return (True, "")

        monkeypatch.setattr(
            "evolve_admin.oc_neutralize.install_externalized_plugin",
            _fake_install,
        )

        ok_ids, failures = deploy._repin_unpinned_via_audit(
            "team-bot-a", "team-bot-a-user",
        )

        assert ok_ids == ["codex"]
        # ghost has no live version → reported as a failure
        assert any("ghost" in f for f in failures)
        assert len(captured) == 1
        assert captured[0]["pkg"] == "@openclaw/codex"

    def test_install_failure_surfaces_per_plugin(self, monkeypatch):
        """An installer that returns (False, err) for one plugin doesn't
        block the others — failure list grows by one entry, ok_ids
        proceeds with the rest."""
        audit_payload = {
            "findings": [
                self._make_audit_finding(
                    "- codex (@openclaw/codex)\n"
                    "- slack (@openclaw/slack)\n",
                ),
            ],
        }
        plugins_payload = {
            "plugins": [
                {"id": "codex", "version": "2026.6.1"},
                {"id": "slack", "version": "2026.5.18"},
            ],
        }
        monkeypatch.setattr(deploy.subprocess, "run", MagicMock(side_effect=[
            MagicMock(returncode=0, stdout=json.dumps(audit_payload), stderr=""),
            MagicMock(returncode=0, stdout=json.dumps(plugins_payload), stderr=""),
        ]))

        def _fake_install(user, pkg, *, force, version, allow_unlisted=False):
            if pkg == "@openclaw/slack":
                return (False, "phantom install blocking")
            return (True, "")

        monkeypatch.setattr(
            "evolve_admin.oc_neutralize.install_externalized_plugin",
            _fake_install,
        )

        ok_ids, failures = deploy._repin_unpinned_via_audit(
            "team-bot-a", "team-bot-a-user",
        )

        assert ok_ids == ["codex"]
        assert any("slack" in f and "phantom" in f for f in failures)

    def test_install_exception_caught_per_plugin(self, monkeypatch):
        """A bug or environment glitch that raises during install_externalized_plugin
        is caught per-plugin so the sweep keeps going for the rest."""
        audit_payload = {
            "findings": [
                self._make_audit_finding(
                    "- codex (@openclaw/codex)\n"
                    "- slack (@openclaw/slack)\n",
                ),
            ],
        }
        plugins_payload = {
            "plugins": [
                {"id": "codex", "version": "2026.6.1"},
                {"id": "slack", "version": "2026.5.18"},
            ],
        }
        monkeypatch.setattr(deploy.subprocess, "run", MagicMock(side_effect=[
            MagicMock(returncode=0, stdout=json.dumps(audit_payload), stderr=""),
            MagicMock(returncode=0, stdout=json.dumps(plugins_payload), stderr=""),
        ]))

        def _fake_install(user, pkg, *, force, version, allow_unlisted=False):
            if pkg == "@openclaw/codex":
                raise RuntimeError("simulated subprocess hang")
            return (True, "")

        monkeypatch.setattr(
            "evolve_admin.oc_neutralize.install_externalized_plugin",
            _fake_install,
        )

        ok_ids, failures = deploy._repin_unpinned_via_audit(
            "team-bot-a", "team-bot-a-user",
        )

        assert ok_ids == ["slack"]
        assert any(
            "codex" in f and "raised" in f and "simulated" in f
            for f in failures
        )
